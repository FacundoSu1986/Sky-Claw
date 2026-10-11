"""#474 — un ``open()`` fallido no deja el sentinel de apertura puesto.

La propiedad del mecanismo (no del caso): *un consumidor que cachea su conexión
detrás de ``if self._conn is not None: return`` sólo puede publicarla en ese
atributo cuando la apertura terminó entera.* Si la publica antes y algo falla
después (PRAGMA, quick_check, DDL, migración, cancelación), el reintento es un
no-op que devuelve un objeto "abierto" sin schema, y en la rama standalone la
conexión propia queda además abierta y sin dueño.

#473 lo arregló en la rama lifecycle de ``LLMRouter`` y ``DistributedLockManager``
y sólo para ``sqlite3.OperationalError``. #474 es el hermano standalone, y los
demás consumidores de la familia tenían la misma forma.

Este archivo enumera en vez de muestrear (ver "La regla que más se viola" en
``AGENTS.md``):

* la familia se detecta por AST —todo método async de ``sky_claw/`` con un
  sentinel ``if self.X is not None: return`` que abre SQLite (``aiosqlite.connect``
  o ``self._lifecycle.operation``)— y se congela contra ``RECETAS``: un consumidor
  nuevo rompe el ancla hasta que se le escribe su receta, y la receta lo mete en
  la matriz de comportamiento;
* la matriz recorre consumidor × rama (standalone / lifecycle) × clase de fallo
  (error SQLite, error no-SQLite, cancelación).
"""

from __future__ import annotations

import ast
import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from sky_claw.app.agent.router import LLMRouter
from sky_claw.app.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.app.db.async_registry import AsyncModRegistry
from sky_claw.app.db.journal import JournalConnectionError, OperationJournal
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.registry import ModRegistry

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

# ---------------------------------------------------------------------------
# Detector de la familia (AST)
# ---------------------------------------------------------------------------


def _atributo_sentinel(nodo: ast.AST) -> str | None:
    """``if self.X is not None: return`` → ``"X"``; cualquier otra cosa → ``None``."""
    if not isinstance(nodo, ast.If) or nodo.orelse:
        return None
    test = nodo.test
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.IsNot)
        and isinstance(test.left, ast.Attribute)
        and isinstance(test.left.value, ast.Name)
        and test.left.value.id == "self"
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        return None
    if len(nodo.body) != 1 or not isinstance(nodo.body[0], ast.Return) or nodo.body[0].value is not None:
        return None
    return test.left.attr


def _abre_sqlite(metodo: ast.AsyncFunctionDef) -> bool:
    """True si el método abre SQLite: ``aiosqlite.connect`` o ``self._lifecycle.operation``."""
    for nodo in ast.walk(metodo):
        if not isinstance(nodo, ast.Attribute):
            continue
        base = nodo.value
        if nodo.attr == "connect" and isinstance(base, ast.Name) and base.id == "aiosqlite":
            return True
        if nodo.attr == "operation" and isinstance(base, ast.Attribute) and base.attr == "_lifecycle":
            return True
    return False


def _familia_detectada() -> set[str]:
    familia: set[str] = set()
    for archivo in sorted(PAQUETE.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        relativo = archivo.relative_to(RAIZ).as_posix()
        for clase in (n for n in ast.walk(arbol) if isinstance(n, ast.ClassDef)):
            for metodo in clase.body:
                if not isinstance(metodo, ast.AsyncFunctionDef) or not _abre_sqlite(metodo):
                    continue
                atributos = {a for n in ast.walk(metodo) if (a := _atributo_sentinel(n))}
                familia.update(f"{relativo}::{clase.name}.{metodo.name}:{a}" for a in atributos)
    return familia


# ---------------------------------------------------------------------------
# Recetas: cómo construir, abrir, sondear y cerrar a cada miembro
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Receta:
    construir: Callable[[Path, DatabaseLifecycleManager | None], Any]
    abrir: Callable[[Any], Awaitable[None]]
    cerrar: Callable[[Any], Awaitable[None]]
    #: Prueba funcional después del reintento: ejercita el schema de verdad.
    sondear: Callable[[Any], Awaitable[None]]
    atributo: str
    admite_lifecycle: bool


async def _sondear_router(router: LLMRouter) -> None:
    await router._save_message("c1", "user", "ok")
    assert [m["content"] for m in await router._load_context("c1")] == ["ok"]


async def _sondear_locks(manager: DistributedLockManager) -> None:
    info = await manager.acquire_lock("sentinela", "agente-a")
    assert info.agent_id == "agente-a"


async def _sondear_async_registry(registry: AsyncModRegistry) -> None:
    await registry.upsert_mod(474, "Sonda")
    fila = await registry.get_mod(474)
    assert fila is not None and fila["name"] == "Sonda"


async def _sondear_journal(journal: OperationJournal) -> None:
    assert isinstance(await journal.begin_transaction("sonda"), int)


async def _sondear_registry(registry: ModRegistry) -> None:
    await registry.upsert_mod(474, "Sonda")
    fila = await registry.get_mod(474)
    assert fila is not None and fila["name"] == "Sonda"


RECETAS: dict[str, Receta] = {
    "sky_claw/app/agent/router.py::LLMRouter.open:_conn": Receta(
        construir=lambda db, lc: LLMRouter(db_path=str(db), api_key="test-api-key-123", lifecycle=lc),
        abrir=lambda o: o.open(),
        cerrar=lambda o: o.close(),
        sondear=_sondear_router,
        atributo="_conn",
        admite_lifecycle=True,
    ),
    # La guarda vive en ``open()``; el sentinel y el SQL, en ``_abrir()``. La receta
    # ejercita la entrada pública.
    "sky_claw/app/db/async_registry.py::AsyncModRegistry._abrir:_conn": Receta(
        construir=lambda db, lc: AsyncModRegistry(db, lifecycle=lc),
        abrir=lambda o: o.open(),
        cerrar=lambda o: o.close(),
        sondear=_sondear_async_registry,
        atributo="_conn",
        admite_lifecycle=True,
    ),
    "sky_claw/app/db/journal.py::OperationJournal.open:_db": Receta(
        construir=lambda db, lc: OperationJournal(db, lifecycle=lc),
        abrir=lambda o: o.open(),
        cerrar=lambda o: o.close(),
        sondear=_sondear_journal,
        atributo="_db",
        admite_lifecycle=True,
    ),
    "sky_claw/app/db/locks.py::DistributedLockManager.initialize:_conn": Receta(
        construir=lambda db, lc: DistributedLockManager(db, lifecycle=lc),
        abrir=lambda o: o.initialize(),
        cerrar=lambda o: o.close(),
        sondear=_sondear_locks,
        atributo="_conn",
        admite_lifecycle=True,
    ),
    # Legacy sin usos en producción (ver test_db_connection_invariant.py), pero
    # la propiedad es la misma y no tiene lifecycle.
    "sky_claw/app/db/registry.py::ModRegistry.open:_conn": Receta(
        construir=lambda db, lc: ModRegistry(db),
        abrir=lambda o: o.open(),
        cerrar=lambda o: o.close(),
        sondear=_sondear_registry,
        atributo="_conn",
        admite_lifecycle=False,
    ),
}

#: Clases de fallo: SQLite (el único que cubría #473), no-SQLite y cancelación
#: (``BaseException`` en 3.11: un ``except Exception`` no la ve).
FALLOS: dict[str, type[BaseException]] = {
    "sqlite": sqlite3.OperationalError,
    "no-sqlite": OSError,
    "cancelacion": asyncio.CancelledError,
}

MATRIZ = [
    pytest.param(clave, rama, fallo, id=f"{clave.split('::')[1]}-{rama}-{fallo}")
    for clave, receta in RECETAS.items()
    for rama in ("standalone", "lifecycle")
    if rama == "standalone" or receta.admite_lifecycle
    for fallo in FALLOS
]

MATRIZ_CONCURRENCIA = [
    pytest.param(clave, rama, id=f"{clave.split('::')[1]}-{rama}")
    for clave, receta in RECETAS.items()
    for rama in ("standalone", "lifecycle")
    if rama == "standalone" or receta.admite_lifecycle
]


# ---------------------------------------------------------------------------
# Inyección del fallo
# ---------------------------------------------------------------------------


class _DDLQueFallaUnaVez:
    """``executescript`` que falla la PRIMERA vez (en cualquier conexión) y luego delega."""

    def __init__(self, error: type[BaseException], pausa: float = 0.0) -> None:
        self.error = error
        self.llamadas = 0
        self.pausa = pausa

    def envolver(self, conn: aiosqlite.Connection) -> None:
        real = conn.executescript

        async def executescript(sql: str) -> Any:
            self.llamadas += 1
            if self.llamadas == 1:
                if self.pausa > 0:
                    await asyncio.sleep(self.pausa)
                raise self.error("DDL falló (simulado #474)")
            return await real(sql)

        conn.executescript = executescript  # type: ignore[method-assign]


async def _esta_cerrada(conn: aiosqlite.Connection) -> bool:
    try:
        async with conn.execute("SELECT 1"):
            pass
    except ValueError:  # aiosqlite: "no active connection"
        return True
    return False


@pytest.fixture()
def conexiones_standalone(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[aiosqlite.Connection]]:
    """Registra toda conexión abierta con ``aiosqlite.connect`` durante el test."""
    creadas: list[aiosqlite.Connection] = []
    conectar_real = aiosqlite.connect

    def conectar(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        conn = conectar_real(*args, **kwargs)
        creadas.append(conn)
        return conn

    monkeypatch.setattr(aiosqlite, "connect", conectar)
    yield creadas


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_la_familia_con_sentinel_de_apertura_esta_congelada() -> None:
    # Preparar / Actuar
    detectada = _familia_detectada()

    # Verificar — igualdad literal: un consumidor nuevo con sentinel necesita su
    # receta (y entra solo a la matriz); uno que desaparece obliga a sacarla.
    assert detectada == set(RECETAS)


def test_el_detector_reconoce_el_sentinel_y_descarta_variantes() -> None:
    # Preparar
    fuente = (
        "class A:\n"
        "    async def open(self):\n"
        "        if self._conn is not None:\n"
        "            return\n"
        "        self._conn = await aiosqlite.connect('x')\n"
        "class B:\n"
        "    async def open(self):\n"
        "        if self._conn is not None:\n"
        "            return 1\n"
        "        self._conn = await aiosqlite.connect('x')\n"
        "class C:\n"
        "    async def start(self):\n"
        "        if self._server is not None:\n"
        "            return\n"
        "        self._server = await serve()\n"
    )
    arbol = ast.parse(fuente)

    # Actuar
    resultado = {
        clase.name: (
            _abre_sqlite(clase.body[0]),  # type: ignore[arg-type]
            {a for n in ast.walk(clase) if (a := _atributo_sentinel(n))},
        )
        for clase in arbol.body
        if isinstance(clase, ast.ClassDef)
    }

    # Verificar
    assert resultado == {"A": (True, {"_conn"}), "B": (True, set()), "C": (False, {"_server"})}


@pytest.mark.parametrize(("clave", "rama", "fallo"), MATRIZ)
async def test_un_open_fallido_no_deja_el_sentinel_ni_la_conexion(
    clave: str,
    rama: str,
    fallo: str,
    tmp_path: Path,
    conexiones_standalone: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Preparar
    receta = RECETAS[clave]
    db = tmp_path / "sentinel.db"
    ddl = _DDLQueFallaUnaVez(FALLOS[fallo])
    lifecycle: DatabaseLifecycleManager | None = None
    conn_lifecycle: aiosqlite.Connection | None = None
    if rama == "lifecycle":
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
        )
        await lifecycle.init_all()
        conn_lifecycle = await lifecycle.get_connection(db)
        ddl.envolver(conn_lifecycle)
    else:
        # Se envuelve cada conexión que el consumidor abra por su cuenta.
        conectar_registrador = aiosqlite.connect

        def conectar_con_ddl_fallido(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
            conn = conectar_registrador(*args, **kwargs)
            ddl.envolver(conn)
            return conn

        monkeypatch.setattr(aiosqlite, "connect", conectar_con_ddl_fallido)
    objeto = receta.construir(db, lifecycle)

    try:
        # Actuar — primer intento: el DDL falla.
        with pytest.raises(BaseException) as info:
            await receta.abrir(objeto)

        # Verificar — el fallo propagado es el inyectado (o lo envuelve).
        error = info.value
        assert isinstance(error, FALLOS[fallo]) or isinstance(error.__cause__, FALLOS[fallo]), repr(error)
        assert ddl.llamadas == 1
        assert getattr(objeto, receta.atributo) is None, "el sentinel quedó puesto tras un open fallido"
        if rama == "standalone":
            abiertas = [c for c in conexiones_standalone if not await _esta_cerrada(c)]
            assert not abiertas, "la conexión propia del open fallido quedó abierta y sin dueño"
        else:
            assert conn_lifecycle is not None
            assert not await _esta_cerrada(conn_lifecycle), "el consumidor cerró una conexión del lifecycle"

        # Actuar — reintento: tiene que ejecutar el schema de verdad.
        await receta.abrir(objeto)

        # Verificar
        assert ddl.llamadas >= 2, "el reintento fue un no-op (sentinel prematuro)"
        assert getattr(objeto, receta.atributo) is not None
        await receta.sondear(objeto)
    finally:
        await receta.cerrar(objeto)
        if lifecycle is not None:
            await lifecycle.shutdown_all()
        # Higiene: en rojo, una conexión fugada no debe colgar el proceso.
        for conn in conexiones_standalone:
            if not await _esta_cerrada(conn):
                await conn.close()


@pytest.mark.parametrize("rama", ["standalone", "lifecycle"])
@pytest.mark.parametrize("fallo", ["cancelacion", "no-sqlite"])
async def test_journal_open_fallido_durante_sweep_despeja_sentinel_y_permite_reintento(
    rama: str,
    fallo: str,
    tmp_path: Path,
    conexiones_standalone: list[aiosqlite.Connection],
) -> None:
    # Preparar
    db = tmp_path / "journal_sweep.db"
    lifecycle: DatabaseLifecycleManager | None = None
    conn_lifecycle: aiosqlite.Connection | None = None
    if rama == "lifecycle":
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
        )
        await lifecycle.init_all()
        conn_lifecycle = await lifecycle.get_connection(db)

    journal = OperationJournal(db, lifecycle=lifecycle)
    sweep_real = journal.sweep_stale_pending
    llamadas_sweep = 0

    async def sweep_inyectado(*args: Any, **kwargs: Any) -> list[int]:
        nonlocal llamadas_sweep
        llamadas_sweep += 1
        if llamadas_sweep == 1:
            raise FALLOS[fallo]("Fallo simulado durante sweep de arranque")
        return await sweep_real(*args, **kwargs)

    journal.sweep_stale_pending = sweep_inyectado  # type: ignore[method-assign]

    try:
        # Actuar — primer intento: el sweep falla.
        with pytest.raises(FALLOS[fallo]):
            await journal.open()

        # Verificar
        assert llamadas_sweep == 1
        assert journal._db is None, "el sentinel _db quedó puesto tras fallo en sweep_stale_pending"
        if rama == "standalone":
            abiertas = [c for c in conexiones_standalone if not await _esta_cerrada(c)]
            assert not abiertas, "la conexión propia quedó abierta tras fallo en sweep_stale_pending"
        else:
            assert conn_lifecycle is not None
            assert not await _esta_cerrada(conn_lifecycle), "se cerró la conexión del lifecycle tras fallo en sweep"

        # Actuar — reintento: debe ejecutar el sweep y completar la apertura.
        await journal.open()

        # Verificar
        assert llamadas_sweep >= 2, "el reintento fue un no-op"
        assert journal._db is not None
        assert isinstance(await journal.begin_transaction("sonda_sweep"), int)
    finally:
        await journal.close()
        if lifecycle is not None:
            await lifecycle.shutdown_all()
        for conn in conexiones_standalone:
            if not await _esta_cerrada(conn):
                await conn.close()


# ---------------------------------------------------------------------------
# Concurrencia e invariante de no-publicación prematura
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("clave", "rama"), MATRIZ_CONCURRENCIA)
async def test_open_concurrente_serializa_y_no_publica_sentinel_prematuro(
    clave: str,
    rama: str,
    tmp_path: Path,
    conexiones_standalone: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Preparar
    receta = RECETAS[clave]
    db = tmp_path / "concurrente.db"
    lifecycle: DatabaseLifecycleManager | None = None
    conn_lifecycle: aiosqlite.Connection | None = None
    ddl_llamadas = 0
    objeto: Any = None

    def envolver_espia_ddl(conn: aiosqlite.Connection) -> None:
        real = conn.executescript

        async def executescript(sql: str) -> Any:
            nonlocal ddl_llamadas
            ddl_llamadas += 1
            # Invariante clave de no-publicación prematura:
            # Mientras se ejecuta el DDL, el sentinel del objeto NO debe estar publicado.
            if objeto is not None:
                assert getattr(objeto, receta.atributo) is None, (
                    f"El atributo {receta.atributo} se publicó antes de terminar el DDL"
                )
            # Yield para permitir que coroutines concurrentes intenten entrar y
            # queden bloqueadas esperando _conn_lock.
            await asyncio.sleep(0.01)
            return await real(sql)

        conn.executescript = executescript  # type: ignore[method-assign]

    if rama == "lifecycle":
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
        )
        await lifecycle.init_all()
        conn_lifecycle = await lifecycle.get_connection(db)
        envolver_espia_ddl(conn_lifecycle)
    else:
        conectar_real = aiosqlite.connect

        def conectar_con_espia(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
            conn = conectar_real(*args, **kwargs)
            envolver_espia_ddl(conn)
            return conn

        monkeypatch.setattr(aiosqlite, "connect", conectar_con_espia)

    objeto = receta.construir(db, lifecycle)

    try:
        # Actuar: 5 llamadas concurrentes a open()
        tareas = [asyncio.create_task(receta.abrir(objeto)) for _ in range(5)]
        await asyncio.gather(*tareas)

        # Verificar:
        # 1. El DDL se ejecutó exactamente una vez (el ganador serializado creó el schema,
        #    los otros 4 esperaron en _conn_lock y salieron por el sentinel ya publicado).
        assert ddl_llamadas == 1, f"Se esperaba exactamente 1 ejecución de DDL, hubo {ddl_llamadas}"
        # 2. El sentinel quedó publicado al terminar.
        assert getattr(objeto, receta.atributo) is not None
        # 3. La sonda funcional responde correctamente.
        await receta.sondear(objeto)
    finally:
        await receta.cerrar(objeto)
        if lifecycle is not None:
            await lifecycle.shutdown_all()
        for conn in conexiones_standalone:
            if not await _esta_cerrada(conn):
                await conn.close()


@pytest.mark.parametrize(("clave", "rama"), MATRIZ_CONCURRENCIA)
async def test_open_concurrente_con_primer_fallo_permite_a_la_segunda_abrir_limpio(
    clave: str,
    rama: str,
    tmp_path: Path,
    conexiones_standalone: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Preparar
    receta = RECETAS[clave]
    db = tmp_path / "concurrente_fallo.db"
    ddl = _DDLQueFallaUnaVez(sqlite3.OperationalError, pausa=0.01)
    lifecycle: DatabaseLifecycleManager | None = None
    if rama == "lifecycle":
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
        )
        await lifecycle.init_all()
        conn_lifecycle = await lifecycle.get_connection(db)
        ddl.envolver(conn_lifecycle)
    else:
        conectar_real = aiosqlite.connect

        def conectar_con_ddl_fallido(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
            conn = conectar_real(*args, **kwargs)
            ddl.envolver(conn)
            return conn

        monkeypatch.setattr(aiosqlite, "connect", conectar_con_ddl_fallido)

    objeto = receta.construir(db, lifecycle)

    try:
        # Actuar: Dos llamadas concurrentes. La primera en entrar pausará y fallará en DDL.
        # La segunda espera en _conn_lock; cuando entra, debe encontrar el sentinel
        # en None, correr su propia inicialización y completarla con éxito.
        t1 = asyncio.create_task(receta.abrir(objeto))
        t2 = asyncio.create_task(receta.abrir(objeto))
        resultados = await asyncio.gather(t1, t2, return_exceptions=True)

        # Verificar: exactamente una falló y exactamente una tuvo éxito.
        errores = [r for r in resultados if isinstance(r, BaseException)]
        exitos = [r for r in resultados if r is None]
        assert len(errores) == 1, f"Se esperaba 1 fallo, hubo {len(errores)}: {errores}"
        assert len(exitos) == 1, f"Se esperaba 1 éxito, hubo {len(exitos)}"

        # El objeto debe haber quedado completamente abierto y funcional.
        assert getattr(objeto, receta.atributo) is not None
        await receta.sondear(objeto)
    finally:
        await receta.cerrar(objeto)
        if lifecycle is not None:
            await lifecycle.shutdown_all()
        for conn in conexiones_standalone:
            if not await _esta_cerrada(conn):
                await conn.close()


@pytest.mark.parametrize("rama", ["standalone", "lifecycle"])
async def test_journal_ensure_connected_concurrente_serializa_open(
    rama: str,
    tmp_path: Path,
    conexiones_standalone: list[aiosqlite.Connection],
) -> None:
    # Preparar
    db = tmp_path / "journal_ensure.db"
    lifecycle: DatabaseLifecycleManager | None = None
    if rama == "lifecycle":
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
        )
        await lifecycle.init_all()

    journal = OperationJournal(db, lifecycle=lifecycle)
    try:
        # Actuar: varias coroutines llaman a _ensure_connected concurrentemente
        # cuando la base aún está cerrada.
        conns = await asyncio.gather(*(journal._ensure_connected() for _ in range(5)))
        # Verificar: todas obtienen la misma conexión abierta y funcional.
        assert all(c is conns[0] for c in conns)
        assert journal._db is not None
        assert isinstance(await journal.begin_transaction("tx_ensure"), int)
    finally:
        await journal.close()
        if lifecycle is not None:
            await lifecycle.shutdown_all()
        for conn in conexiones_standalone:
            if not await _esta_cerrada(conn):
                await conn.close()


async def test_journal_descartar_apertura_fallida_preserva_lock_de_lifecycle(
    tmp_path: Path,
) -> None:
    # Preparar
    db = tmp_path / "journal_lifecycle_lock.db"
    lifecycle = DatabaseLifecycleManager(
        db_paths=[db],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )
    await lifecycle.init_all()
    conn_lifecycle = await lifecycle.get_connection(db)
    ddl = _DDLQueFallaUnaVez(sqlite3.OperationalError)
    ddl.envolver(conn_lifecycle)

    lock_compartido = lifecycle.get_write_lock(db)
    journal = OperationJournal(db, lifecycle=lifecycle)

    try:
        # Actuar: primera apertura falla
        with pytest.raises(JournalConnectionError):
            await journal.open()

        # Verificar: _descartar_apertura_fallida NO reemplazó _lock con un Lock() huérfano
        assert journal._lock is lock_compartido, (
            "El lock compartido del lifecycle fue pisado por un asyncio.Lock() local"
        )
        assert journal._db is None

        # Reintento exitoso
        await journal.open()
        assert journal._lock is lock_compartido
        assert journal._db is not None
        assert isinstance(await journal.begin_transaction("tx_lifecycle_lock"), int)
    finally:
        await journal.close()
        await lifecycle.shutdown_all()
