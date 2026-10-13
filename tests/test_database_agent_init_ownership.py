"""Regresión #719: ownership de ``DatabaseAgent.init_db()`` / ``close()``.

Contrato bajo prueba (propiedad del mecanismo, no de un caller):

* ``init_db()`` publica ``_lifecycle``/``_conn`` SOLO cuando el esquema
  terminó: un fallo de ``init_all``, de ``get_connection``, del DDL o una
  cancelación a mitad de camino no deja una conexión "inicializada" a medias.
* Todo manager creado por ESTA instancia lo cierra su propietaria ante un
  fallo; si ese cierre falla, la referencia NO se pierde: queda en
  ``_lifecycle`` (con ``_conn is None``) para poder reintentar el cierre.
* Un reintento tras un fallo descarta primero el manager retenido y recién
  entonces crea uno nuevo: nunca hay un manager huérfano sin referencia.
* Dos ``init_db()`` exitosos consecutivos son idempotentes: conservan el
  MISMO manager y la MISMA conexión, sin cerrar ni reemplazar nada.
* ``close()`` es repetible tras éxito, tras fallo y sin inicializar, y ante
  un fallo de shutdown conserva el ownership para el reintento. Con
  lifecycle presente delega en él y no cierra además la conexión por su
  cuenta (esa conexión es del lifecycle, no de este agente).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import pytest

from sky_claw.app.core.database import DatabaseAgent
from sky_claw.app.core.db_lifecycle import DatabaseLifecycleManager

_TABLAS = {"scraper_state", "agent_memory", "mods", "conflicts", "activity_log"}


async def _esperar(evento: asyncio.Event) -> None:
    """Espera un punto de sincronización con watchdog (sin sleeps)."""
    await asyncio.wait_for(evento.wait(), timeout=5)


class _LifecycleRastreado(DatabaseLifecycleManager):
    """Lifecycle real que cuenta sus shutdowns y puede fallarlos a pedido."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_paths=[db_path])
        self.shutdowns = 0
        self.fallos_shutdown_restantes = 0

    async def shutdown_all(self) -> None:
        self.shutdowns += 1
        if self.fallos_shutdown_restantes > 0:
            self.fallos_shutdown_restantes -= 1
            raise RuntimeError("fallo de shutdown inducido")
        await super().shutdown_all()


class _Fabrica:
    """Fábrica inyectable vía ``DatabaseLifecycleManager`` que registra creados."""

    def __init__(self, db_path: Path, clase: type[_LifecycleRastreado] = _LifecycleRastreado) -> None:
        self._db_path = db_path
        self._clase = clase
        self.creados: list[_LifecycleRastreado] = []

    def __call__(self, **_kwargs: object) -> _LifecycleRastreado:
        manager = self._clase(self._db_path)
        self.creados.append(manager)
        return manager


def _instalar_fabrica(monkeypatch: pytest.MonkeyPatch, fabrica: _Fabrica) -> None:
    monkeypatch.setattr("sky_claw.app.core.database.DatabaseLifecycleManager", fabrica)


async def _tablas_de(db_path: Path) -> set[str]:
    async with (
        aiosqlite.connect(db_path) as conn,
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor,
    ):
        return {fila[0] for fila in await cursor.fetchall()}


# ---------------------------------------------------------------------------
# Fallo en init_all: no publica nada y cierra el manager que creó
# ---------------------------------------------------------------------------


async def test_fallo_en_init_all_no_publica_y_cierra_el_manager_creado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "init-all-falla.db"

    class LifecycleFallaInit(_LifecycleRastreado):
        async def init_all(self) -> None:
            raise sqlite3.OperationalError("fallo init_all inducido")

    fabrica = _Fabrica(db_path, LifecycleFallaInit)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    with pytest.raises(sqlite3.OperationalError, match="fallo init_all inducido"):
        await agente.init_db()

    assert agente._conn is None
    assert agente._lifecycle is None
    assert len(fabrica.creados) == 1
    assert fabrica.creados[0].shutdowns == 1


async def test_fallo_en_get_connection_no_publica_y_cierra_el_manager_creado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "get-connection-falla.db"

    class LifecycleFallaGetConnection(_LifecycleRastreado):
        async def get_connection(self, _path: Path | str) -> aiosqlite.Connection:
            raise OSError("fallo get_connection inducido")

    fabrica = _Fabrica(db_path, LifecycleFallaGetConnection)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    with pytest.raises(OSError, match="fallo get_connection inducido"):
        await agente.init_db()

    assert agente._conn is None
    assert agente._lifecycle is None
    assert fabrica.creados[0].shutdowns == 1


# ---------------------------------------------------------------------------
# Fallo de DDL: nada publicado DURANTE ni DESPUÉS; el reintento completa
# ---------------------------------------------------------------------------


async def test_fallo_ddl_no_publica_conexion_y_el_reintento_completa_el_esquema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "ddl-falla.db"

    class LifecycleFallaDDL(_LifecycleRastreado):
        debe_fallar = True

        @asynccontextmanager
        async def transaction(
            self,
            transaction_path: Path | str,
        ) -> AsyncGenerator[aiosqlite.Connection, None]:
            async with super().transaction(transaction_path) as conn:
                execute_original = conn.execute

                async def execute_con_fallo(sql: str, *args: object) -> aiosqlite.Cursor:
                    if type(self).debe_fallar and "CREATE TABLE IF NOT EXISTS conflicts" in sql:
                        raise sqlite3.OperationalError("fallo DDL inducido")
                    return await execute_original(sql, *args)

                monkeypatch.setattr(conn, "execute", execute_con_fallo)
                try:
                    yield conn
                finally:
                    monkeypatch.setattr(conn, "execute", execute_original)

    fabrica = _Fabrica(db_path, LifecycleFallaDDL)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    with pytest.raises(sqlite3.OperationalError, match="fallo DDL inducido"):
        await agente.init_db()

    # Nada quedó publicado como inicializado ni el manager quedó huérfano.
    assert agente._conn is None
    assert agente._lifecycle is None
    assert fabrica.creados[0].shutdowns == 1
    assert fabrica.creados[0].managed_paths == []
    assert not (_TABLAS & await _tablas_de(db_path))

    # El reintento abre un manager nuevo y completa REALMENTE el esquema.
    LifecycleFallaDDL.debe_fallar = False
    await agente.init_db()
    try:
        assert len(fabrica.creados) == 2
        assert agente._lifecycle is fabrica.creados[1]
        assert await _tablas_de(db_path) >= _TABLAS
        await agente.set_memory("clave", "valor", 1.0)
        assert await agente.get_memory("clave") == "valor"
    finally:
        await agente.close()


async def test_cancelacion_durante_el_ddl_no_publica_y_permite_reintentar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "ddl-cancelado.db"
    ddl_en_curso = asyncio.Event()
    liberar_ddl = asyncio.Event()
    estado = {"bloquear": True}

    class LifecycleDDLLento(_LifecycleRastreado):
        @asynccontextmanager
        async def transaction(
            self,
            transaction_path: Path | str,
        ) -> AsyncGenerator[aiosqlite.Connection, None]:
            async with super().transaction(transaction_path) as conn:
                execute_original = conn.execute

                async def execute_lento(sql: str, *args: object) -> aiosqlite.Cursor:
                    if estado["bloquear"] and "CREATE TABLE IF NOT EXISTS conflicts" in sql:
                        ddl_en_curso.set()
                        await liberar_ddl.wait()
                    return await execute_original(sql, *args)

                monkeypatch.setattr(conn, "execute", execute_lento)
                try:
                    yield conn
                finally:
                    monkeypatch.setattr(conn, "execute", execute_original)

    fabrica = _Fabrica(db_path, LifecycleDDLLento)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    tarea = asyncio.create_task(agente.init_db())
    try:
        await _esperar(ddl_en_curso)
        # A mitad del DDL todavía no hay conexión publicada como inicializada.
        assert agente._conn is None

        tarea.cancel("cancelación durante el DDL")
        liberar_ddl.set()
        with pytest.raises(asyncio.CancelledError, match="cancelación durante el DDL"):
            await tarea

        assert agente._conn is None
        assert agente._lifecycle is None
        assert fabrica.creados[0].shutdowns == 1

        estado["bloquear"] = False
        await agente.init_db()
        assert await _tablas_de(db_path) >= _TABLAS
    finally:
        liberar_ddl.set()
        if not tarea.done():
            tarea.cancel()
            await asyncio.gather(tarea, return_exceptions=True)
        await agente.close()


# ---------------------------------------------------------------------------
# El cierre de limpieza también falla: el ownership se conserva
# ---------------------------------------------------------------------------


async def test_shutdown_fallido_tras_fallo_de_init_conserva_ownership_y_el_reintento_no_huerfana(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "shutdown-falla.db"
    estado = {"fallar_init": True}

    class LifecycleInitFragil(_LifecycleRastreado):
        async def init_all(self) -> None:
            if estado["fallar_init"]:
                raise sqlite3.OperationalError("fallo init_all inducido")
            await super().init_all()

    fabrica = _Fabrica(db_path, LifecycleInitFragil)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))
    # Preconfigurar el primer manager creado para que su limpieza falle una vez.
    original_call = fabrica.__call__

    def fabrica_con_shutdown_fragil(**kwargs: object) -> _LifecycleRastreado:
        manager = original_call(**kwargs)
        if len(fabrica.creados) == 1:
            manager.fallos_shutdown_restantes = 1
        return manager

    monkeypatch.setattr(
        "sky_claw.app.core.database.DatabaseLifecycleManager",
        fabrica_con_shutdown_fragil,
    )

    with pytest.raises(sqlite3.OperationalError, match="fallo init_all inducido"):
        await agente.init_db()

    retenido = fabrica.creados[0]
    # El shutdown de limpieza falló: la referencia NO se perdió en silencio.
    assert retenido.shutdowns == 1
    assert agente._lifecycle is retenido
    assert agente._conn is None

    # El reintento primero cierra el retenido y recién entonces crea otro.
    estado["fallar_init"] = False
    await agente.init_db()
    try:
        assert retenido.shutdowns == 2
        assert len(fabrica.creados) == 2
        assert agente._lifecycle is fabrica.creados[1]
        assert await _tablas_de(db_path) >= _TABLAS
    finally:
        await agente.close()


# ---------------------------------------------------------------------------
# Idempotencia: dos init exitosos no reemplazan ni fugan el manager previo
# ---------------------------------------------------------------------------


async def test_doble_init_exitoso_conserva_el_mismo_manager_y_conexion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "doble-init.db"
    fabrica = _Fabrica(db_path)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    await agente.init_db()
    try:
        lifecycle_inicial = agente._lifecycle
        conn_inicial = agente._conn

        await agente.init_db()

        assert len(fabrica.creados) == 1
        assert agente._lifecycle is lifecycle_inicial
        assert agente._conn is conn_inicial
        assert fabrica.creados[0].shutdowns == 0
    finally:
        await agente.close()


async def test_init_tras_close_exitoso_crea_un_manager_nuevo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "reinit-tras-close.db"
    fabrica = _Fabrica(db_path)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    await agente.init_db()
    await agente.close()
    assert agente._lifecycle is None
    assert agente._conn is None

    await agente.init_db()
    try:
        assert len(fabrica.creados) == 2
        assert agente._lifecycle is fabrica.creados[1]
        await agente.set_memory("clave", "valor", 1.0)
        assert await agente.get_memory("clave") == "valor"
    finally:
        await agente.close()


# ---------------------------------------------------------------------------
# close(): repetible, seguro tras fallo y sin cerrar recursos ajenos
# ---------------------------------------------------------------------------


async def test_close_sin_inicializar_y_repetido_es_noop(tmp_path: Path) -> None:
    agente = DatabaseAgent(str(tmp_path / "sin-init.db"))

    await agente.close()
    await agente.close()

    assert agente._lifecycle is None
    assert agente._conn is None


async def test_close_fallido_conserva_ownership_y_el_reintento_cierra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "close-falla.db"
    fabrica = _Fabrica(db_path)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))
    await agente.init_db()
    lifecycle = fabrica.creados[0]
    lifecycle.fallos_shutdown_restantes = 1

    with pytest.raises(RuntimeError, match="fallo de shutdown inducido"):
        await agente.close()

    assert agente._lifecycle is lifecycle

    await agente.close()
    assert agente._lifecycle is None
    assert agente._conn is None
    # Y repetir tras el éxito sigue siendo seguro.
    await agente.close()


async def test_dos_init_concurrentes_crean_un_solo_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dos ``init_db()`` simultáneos no crean dos managers (uno quedaría huérfano)."""
    db_path = tmp_path / "init-concurrente.db"
    init_en_curso = asyncio.Event()
    liberar_init = asyncio.Event()

    class LifecycleInitLento(_LifecycleRastreado):
        async def init_all(self) -> None:
            init_en_curso.set()
            await liberar_init.wait()
            await super().init_all()

    fabrica = _Fabrica(db_path, LifecycleInitLento)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    primera = asyncio.create_task(agente.init_db())
    segunda = asyncio.create_task(agente.init_db())
    try:
        await _esperar(init_en_curso)
        liberar_init.set()
        await asyncio.gather(primera, segunda)

        assert len(fabrica.creados) == 1
        assert agente._lifecycle is fabrica.creados[0]
        assert fabrica.creados[0].shutdowns == 0
        await agente.set_memory("clave", "valor", 1.0)
        assert await agente.get_memory("clave") == "valor"
    finally:
        liberar_init.set()
        await asyncio.gather(primera, segunda, return_exceptions=True)
        await agente.close()


async def test_close_durante_un_init_en_curso_espera_y_cierra_lo_publicado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``close()`` no declara el cierre completo mientras un init sigue en vuelo."""
    db_path = tmp_path / "close-durante-init.db"
    init_en_curso = asyncio.Event()
    liberar_init = asyncio.Event()

    class LifecycleInitLento(_LifecycleRastreado):
        async def init_all(self) -> None:
            init_en_curso.set()
            await liberar_init.wait()
            await super().init_all()

    fabrica = _Fabrica(db_path, LifecycleInitLento)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    tarea_init = asyncio.create_task(agente.init_db())
    try:
        await _esperar(init_en_curso)
        tarea_close = asyncio.create_task(agente.close())
        # El cierre no puede completarse antes que la inicialización en vuelo.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(tarea_close), timeout=0.05)

        liberar_init.set()
        await tarea_init
        await tarea_close

        assert agente._lifecycle is None
        assert agente._conn is None
        assert fabrica.creados[0].shutdowns == 1
    finally:
        liberar_init.set()
        await asyncio.gather(tarea_init, return_exceptions=True)
        await agente.close()


async def test_cancelacion_durante_la_limpieza_se_observa_hasta_el_final_y_manda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelar durante el shutdown de limpieza no lo aborta ni traga la cancelación."""
    db_path = tmp_path / "cancel-en-limpieza.db"
    limpieza_en_curso = asyncio.Event()
    liberar_limpieza = asyncio.Event()
    limpieza_terminada = asyncio.Event()

    class LifecycleLimpiezaLenta(_LifecycleRastreado):
        async def init_all(self) -> None:
            raise sqlite3.OperationalError("fallo init_all inducido")

        async def shutdown_all(self) -> None:
            self.shutdowns += 1
            limpieza_en_curso.set()
            await liberar_limpieza.wait()
            await DatabaseLifecycleManager.shutdown_all(self)
            limpieza_terminada.set()

    fabrica = _Fabrica(db_path, LifecycleLimpiezaLenta)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))

    tarea = asyncio.create_task(agente.init_db())
    try:
        await _esperar(limpieza_en_curso)
        tarea.cancel("cancelación durante la limpieza")
        await asyncio.sleep(0)
        # La limpieza blindada sigue en curso: la tarea aún no terminó.
        assert not tarea.done()

        liberar_limpieza.set()
        with pytest.raises(asyncio.CancelledError, match="cancelación durante la limpieza") as exc_info:
            await tarea

        # La cancelación manda sobre el error de init y este queda como causa.
        assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
        assert limpieza_terminada.is_set()
        assert agente._lifecycle is None
        assert agente._conn is None
    finally:
        liberar_limpieza.set()
        if not tarea.done():
            tarea.cancel()
            await asyncio.gather(tarea, return_exceptions=True)


async def test_close_con_lifecycle_delega_y_no_cierra_la_conexion_por_su_cuenta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La conexión gestionada es del lifecycle: ``close()`` no la cierra dos veces."""
    db_path = tmp_path / "close-delega.db"
    fabrica = _Fabrica(db_path)
    _instalar_fabrica(monkeypatch, fabrica)
    agente = DatabaseAgent(str(db_path))
    await agente.init_db()
    conn = await agente._get_conn()
    cierres_directos = 0
    close_original = conn.close

    async def close_espiado() -> None:
        nonlocal cierres_directos
        cierres_directos += 1
        await close_original()

    monkeypatch.setattr(conn, "close", close_espiado)

    await agente.close()

    # El único cierre pasa por shutdown_all() del lifecycle (1), sin un
    # cierre directo adicional del agente sobre la misma conexión.
    assert cierres_directos == 1
    assert agente._lifecycle is None
    assert agente._conn is None
