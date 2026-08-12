"""Contrato de invalidación de conexiones compartidas del lifecycle.

Por qué existe: `DatabaseLifecycleManager` entrega una conexión **singleton por
path resuelto**, así que varios wrappers comparten el mismo objeto. Hasta este
contrato el lifecycle podía *expulsar* una conexión (`evict_connection`) pero no
*invalidarla*: el que la expulsaba se enteraba, y los otros cuatro wrappers que
la tenían cacheada seguían emitiendo SQL contra una conexión cerrada. Nada en el
árbol podía contestar "¿mi conexión cacheada sigue siendo la válida de este
path?".

Las dos propiedades que se anclan acá, enunciadas como propiedades del
mecanismo y no como recordatorio de proceso:

    la invalidación de una conexión compartida sólo protege si TODOS los que la
    cachean la consultan antes de reusarla

    ceder ownership es un acto posterior al cierre, no simultáneo: si el cierre
    falla, el lifecycle conserva la conexión porque es la única referencia con
    la que shutdown puede reintentar la limpieza

Los tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): `test_ancla_wrappers_que_cachean_conexion` congela por AST el
conjunto de módulos que guardan una conexión del lifecycle en un atributo, con
su estado de adopción. Un wrapper nuevo rompe el ancla hasta que se le escriba
su receta — y la receta lo mete en los tests de comportamiento de abajo.
"""

from __future__ import annotations

import ast
import asyncio
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from sky_claw.app.core.database import DatabaseAgent
from sky_claw.app.core.db_lifecycle import (
    DatabaseLifecycleManager,
    DatabasePathPoisonedError,
)
from sky_claw.app.db.journal import OperationJournal
from sky_claw.app.db.locks import DistributedLockManager

RAIZ = Path(__file__).resolve().parents[1]


# ===========================================================================
# Ancla enumerativa
# ===========================================================================

# Módulos que guardan una conexión del lifecycle en un atributo de instancia y,
# por lo tanto, pueden quedarse con una referencia obsoleta. Congelado por
# igualdad: un wrapper nuevo rompe el test hasta que se decida su estado.
#
# `revalida`: consume `refresh_connection()` antes de reusar lo cacheado.
# `refetch`:  vuelve a pedir la conexión al lifecycle en cada uso, así que es
#             inmune por construcción — verificado por comportamiento en
#             `test_database_agent_repide_la_conexion_en_cada_uso`, no por
#             confianza en este comentario.
WRAPPERS_QUE_CACHEAN_CONEXION = {
    "sky_claw/app/agent/router.py": "revalida",
    "sky_claw/app/core/database.py": "refetch",
    "sky_claw/app/db/async_registry.py": "revalida",
    "sky_claw/app/db/journal.py": "revalida",
    "sky_claw/app/db/locks.py": "revalida",
}

_OBTENCION = {"get_connection", "refresh_connection"}


def _cachea_conexion(arbol: ast.AST) -> bool:
    """True si el módulo asigna a un atributo de ``self`` una conexión del lifecycle."""
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Assign):
            continue
        valor = nodo.value
        if not isinstance(valor, ast.Await):
            continue
        llamada = valor.value
        if not isinstance(llamada, ast.Call) or not isinstance(llamada.func, ast.Attribute):
            continue
        if llamada.func.attr not in _OBTENCION:
            continue
        for destino in nodo.targets:
            if (
                isinstance(destino, ast.Attribute)
                and isinstance(destino.value, ast.Name)
                and destino.value.id == "self"
            ):
                return True
    return False


def test_ancla_wrappers_que_cachean_conexion() -> None:
    """El conjunto de wrappers que cachean conexión está congelado y clasificado."""
    encontrados = set()
    for archivo in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        if _cachea_conexion(arbol):
            encontrados.add(archivo.relative_to(RAIZ).as_posix())

    assert encontrados == set(WRAPPERS_QUE_CACHEAN_CONEXION), (
        "Cambió el conjunto de wrappers que cachean una conexión del lifecycle. "
        "Un wrapper nuevo debe declarar si revalida contra el lifecycle "
        "('revalida') o si re-pide la conexión en cada uso ('refetch'), y sumar "
        "su caso a los tests de comportamiento de este archivo."
    )


def test_ancla_los_que_revalidan_llaman_al_lifecycle() -> None:
    """Los clasificados 'revalida' consumen la decisión, no la reimplementan."""
    for ruta, estado in sorted(WRAPPERS_QUE_CACHEAN_CONEXION.items()):
        if estado != "revalida":
            continue
        fuente = (RAIZ / ruta).read_text(encoding="utf-8")
        assert "refresh_connection(" in fuente, (
            f"{ruta} cachea una conexión del lifecycle y está declarado como "
            "'revalida', pero no llama a refresh_connection(): quedaría con una "
            "referencia obsoleta cuando otro wrapper ponga la conexión en cuarentena."
        )


# ===========================================================================
# Helpers
# ===========================================================================


def _lifecycle(db: Path) -> DatabaseLifecycleManager:
    return DatabaseLifecycleManager(db_paths=[db])


async def _romper_rollback(conn: aiosqlite.Connection) -> None:
    """Hace fallar el rollback de *conn* (dispara la cuarentena real)."""

    async def _falla() -> None:
        raise sqlite3.OperationalError("rollback imposible")

    conn.rollback = _falla  # type: ignore[method-assign]


async def _provocar_cuarentena(
    lifecycle: DatabaseLifecycleManager,
    db: Path,
    conn: aiosqlite.Connection,
) -> None:
    """Cuarentena por la ruta de producción: transacción que falla y no puede deshacerse."""
    await _romper_rollback(conn)
    with pytest.raises(BaseException):  # noqa: B017,PT011 - el tipo lo decide el encadenado
        async with lifecycle.transaction(db):
            raise RuntimeError("boom")


# ===========================================================================
# T1 — invalidación entre hermanos (mismo tipo de wrapper)
# ===========================================================================


async def test_t1_un_journal_hermano_detecta_la_referencia_obsoleta(tmp_path: Path) -> None:
    """j2 nunca se cierra a mano: debe detectar solo que su conexión ya no vale."""
    db = tmp_path / "journal.db"
    lifecycle = _lifecycle(db)
    j1 = OperationJournal(str(db), lifecycle=lifecycle)
    j2 = OperationJournal(str(db), lifecycle=lifecycle)
    await j1.open()
    await j2.open()

    compartida = j1._db
    assert compartida is not None
    assert j2._db is compartida, "precondición: ambos journals comparten la conexión"

    await _provocar_cuarentena(lifecycle, db, compartida)

    # Nadie tocó j2. Su siguiente operación tiene que funcionar sobre una
    # conexión nueva, no explotar contra la cerrada.
    tx = await j2.begin_transaction("después de la cuarentena")
    assert tx > 0

    assert j2._db is not compartida, "j2 siguió con la conexión invalidada"
    assert not compartida._running, "la conexión vieja debería estar cerrada"

    await lifecycle.shutdown_all()


# ===========================================================================
# T2 — otra familia de wrapper (el mecanismo no es del Journal)
# ===========================================================================


async def test_t2_otra_familia_de_wrapper_tambien_reengancha(tmp_path: Path) -> None:
    """Un DistributedLockManager sobre el mismo path también detecta la invalidación."""
    db = tmp_path / "compartida.db"
    lifecycle = _lifecycle(db)
    journal = OperationJournal(str(db), lifecycle=lifecycle)
    locks = DistributedLockManager(str(db), lifecycle=lifecycle)
    await journal.open()
    await locks.initialize()

    compartida = journal._db
    assert compartida is not None
    assert locks._conn is compartida, "precondición: journal y locks comparten conexión"

    await _provocar_cuarentena(lifecycle, db, compartida)

    # El lock manager no se enteró de nada y no fue cerrado.
    adquirido = await locks.acquire_lock("recurso", "duenio")
    assert adquirido is not None
    assert locks._conn is not compartida

    await lifecycle.shutdown_all()


# ===========================================================================
# T3 — el cierre también falla: fail-closed y ownership conservado
# ===========================================================================


async def test_t3_cierre_fallido_deja_el_path_envenenado(tmp_path: Path) -> None:
    """close() falla ⇒ invalidada igual, ownership retenido, sin reemplazo."""
    db = tmp_path / "envenenada.db"
    lifecycle = _lifecycle(db)
    j1 = OperationJournal(str(db), lifecycle=lifecycle)
    j2 = OperationJournal(str(db), lifecycle=lifecycle)
    await j1.open()
    await j2.open()

    compartida = j1._db
    assert compartida is not None

    cierre_real = compartida.close

    async def _cierre_que_falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    compartida.close = _cierre_que_falla  # type: ignore[method-assign]

    await _provocar_cuarentena(lifecycle, db, compartida)

    clave = str(db.resolve())

    # (a) el path quedó fail-closed
    assert lifecycle.is_path_poisoned(db)
    # (b) ownership conservado: shutdown/recovery todavía la conoce
    assert clave in lifecycle.managed_paths
    assert lifecycle._connections[clave] is compartida
    # (c) NO se entrega un reemplazo
    with pytest.raises(DatabasePathPoisonedError):
        await lifecycle.get_connection(db)
    # (d) el hermano cacheado no puede seguir usándola en silencio
    assert not lifecycle.is_connection_current(db, compartida)
    with pytest.raises(DatabasePathPoisonedError):
        await j2.begin_transaction("no debería entrar")

    # (e) evict_connection no puede robarle el ownership al path envenenado:
    #     lo dejaría fail-closed para siempre y sin nadie que reintente el cierre.
    assert lifecycle.evict_connection(db, expected_connection=compartida) is False
    assert lifecycle._connections[clave] is compartida

    # (f) recovery: cuando el cierre por fin funciona, el veneno se levanta.
    compartida.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()
    assert not lifecycle.is_path_poisoned(db)
    assert clave not in lifecycle.managed_paths


async def test_t3b_el_error_de_cierre_viaja_en_la_excepcion(tmp_path: Path) -> None:
    """El diagnóstico no se tapa: el fallo de cierre queda accesible."""
    db = tmp_path / "diag.db"
    lifecycle = _lifecycle(db)
    conn = await lifecycle.get_connection(db)

    async def _cierre_que_falla() -> None:
        raise sqlite3.OperationalError("disco lleno")

    cierre_real = conn.close
    conn.close = _cierre_que_falla  # type: ignore[method-assign]

    close_error = await lifecycle.quarantine_connection(db, conn)
    assert isinstance(close_error, sqlite3.OperationalError)

    with pytest.raises(DatabasePathPoisonedError) as excinfo:
        await lifecycle.get_connection(db)
    assert excinfo.value.close_error is close_error
    assert "disco lleno" in str(excinfo.value)

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# T4 — un rollback normal no invalida nada
# ===========================================================================


async def test_t4_rollback_exitoso_no_invalida_la_conexion(tmp_path: Path) -> None:
    """El camino feliz no puede pagar el costo del camino de recuperación."""
    db = tmp_path / "ok.db"
    lifecycle = _lifecycle(db)
    j1 = OperationJournal(str(db), lifecycle=lifecycle)
    j2 = OperationJournal(str(db), lifecycle=lifecycle)
    await j1.open()
    await j2.open()
    compartida = j1._db
    assert compartida is not None

    with pytest.raises(RuntimeError):
        async with lifecycle.transaction(db):
            raise RuntimeError("falla con rollback sano")

    assert not lifecycle.is_path_poisoned(db)
    assert lifecycle.is_connection_current(db, compartida)

    await j2.begin_transaction("sigue igual")
    assert j2._db is compartida, "una conexión sana no debe reemplazarse"
    assert j1._db is compartida

    await lifecycle.shutdown_all()


# ===========================================================================
# T5 — paths independientes
# ===========================================================================


async def test_t5_envenenar_un_path_no_afecta_a_otro(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    lifecycle = DatabaseLifecycleManager(db_paths=[a, b])
    conn_a = await lifecycle.get_connection(a)
    conn_b = await lifecycle.get_connection(b)

    async def _cierre_que_falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    cierre_real = conn_a.close
    conn_a.close = _cierre_que_falla  # type: ignore[method-assign]
    await lifecycle.quarantine_connection(a, conn_a)

    assert lifecycle.is_path_poisoned(a)
    assert not lifecycle.is_path_poisoned(b)
    assert lifecycle.is_connection_current(b, conn_b)
    assert await lifecycle.get_connection(b) is conn_b

    conn_a.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# T6 — aliasing: dos formas del mismo path resuelto comparten estado
# ===========================================================================


async def test_t6_un_alias_del_mismo_path_comparte_la_invalidacion(tmp_path: Path) -> None:
    """Misma canonicalización que get_connection/get_write_lock."""
    directo = tmp_path / "alias.db"
    (tmp_path / "sub").mkdir()
    alias = tmp_path / "sub" / ".." / "alias.db"
    assert directo.resolve() == alias.resolve()

    lifecycle = DatabaseLifecycleManager(db_paths=[directo])
    conn = await lifecycle.get_connection(directo)
    assert await lifecycle.get_connection(alias) is conn
    assert lifecycle.get_write_lock(alias) is lifecycle.get_write_lock(directo)

    async def _cierre_que_falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    cierre_real = conn.close
    conn.close = _cierre_que_falla  # type: ignore[method-assign]
    await lifecycle.quarantine_connection(directo, conn)

    assert lifecycle.is_path_poisoned(alias), "el alias quedó sin proteger"
    assert not lifecycle.is_connection_current(alias, conn)
    with pytest.raises(DatabasePathPoisonedError):
        await lifecycle.get_connection(alias)

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# T7 — concurrencia: dos detecciones simultáneas, un solo reemplazo
# ===========================================================================


async def test_t7_dos_wrappers_no_abren_dos_reemplazos(tmp_path: Path) -> None:
    """El singleton por path sigue valiendo cuando la invalidación es concurrente."""
    db = tmp_path / "carrera.db"
    lifecycle = _lifecycle(db)
    j1 = OperationJournal(str(db), lifecycle=lifecycle)
    j2 = OperationJournal(str(db), lifecycle=lifecycle)
    await j1.open()
    await j2.open()
    compartida = j1._db
    assert compartida is not None

    await _provocar_cuarentena(lifecycle, db, compartida)

    # Contar aperturas reales a partir de acá.
    init_real = lifecycle._init_single
    aperturas = 0
    listos = asyncio.Event()

    async def _init_contado(path: Path) -> None:
        nonlocal aperturas
        aperturas += 1
        # Ceder el control dentro de la sección crítica: sin `_init_lock`, la
        # segunda coroutine entraría acá y abriría un segundo reemplazo.
        await listos.wait()
        await init_real(path)

    lifecycle._init_single = _init_contado  # type: ignore[method-assign]

    tarea1 = asyncio.create_task(j1._ensure_connected())
    tarea2 = asyncio.create_task(j2._ensure_connected())
    await asyncio.sleep(0)
    listos.set()
    conn1, conn2 = await asyncio.gather(tarea1, tarea2)

    assert aperturas == 1, f"se abrieron {aperturas} reemplazos para el mismo path"
    assert conn1 is conn2
    assert conn1 is not compartida

    lifecycle._init_single = init_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# Verificación de la exención declarada en el ancla
# ===========================================================================


async def test_database_agent_repide_la_conexion_en_cada_uso(tmp_path: Path) -> None:
    """`core/database.py` está exento por 'refetch': acá se verifica, no se afirma.

    Escribir el racional de una exclusión no cuenta como verificarla
    (`AGENTS.md`), así que la inmunidad estructural de `DatabaseAgent` —vuelve a
    pedirle la conexión al lifecycle en cada `_get_conn()`— se demuestra.
    """
    db = tmp_path / "agent.db"
    agent = DatabaseAgent(str(db))
    await agent.init_db()
    lifecycle = agent._lifecycle
    assert lifecycle is not None

    primera = await agent._get_conn()
    await lifecycle.quarantine_connection(db, primera)

    segunda = await agent._get_conn()
    assert segunda is not primera, "DatabaseAgent reusó una conexión invalidada"
    assert lifecycle.is_connection_current(db, segunda)

    await agent.close()
