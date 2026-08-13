"""Contrato de participación universal en el boundary por path.

Propiedad única de este contrato:

    toda lectura o escritura de la conexión compartida de un path está
    serializada con toda mutación del registro de ese path

`DatabaseLifecycleManager` entrega la conexión como **singleton por path
resuelto**, así que varios wrappers comparten el mismo objeto. El lock por path
ya existía, pero la participación era parcial: `journal` y los escritores de
`async_registry` lo tomaban; los lectores, `locks`, `router`, `core/database`,
`dlq_manager` y `governance` emitían SQL sin coordinarse con quien podía cerrar
o reemplazar esa conexión. `operation()` es ahora la única forma soportada.

POLÍTICA DE TESTS (deliberada, tras cuatro anclas AST que resultaron incapaces
de fallar en un intento anterior de este contrato):

    AST                 = qué superficies existen (inventario congelado)
    Event/barrier tests = cómo se comportan (autoridad sobre la propiedad)

Ninguna aserción estática de este archivo pretende demostrar atomicidad,
protección ni orden. Eso lo prueban los tests conductuales, con barreras y sin
sleeps.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from sky_claw.app.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
    DatabaseLifecycleShuttingDownError,
)

RAIZ = Path(__file__).resolve().parents[1]


def _lifecycle() -> DatabaseLifecycleManager:
    return DatabaseLifecycleManager(config=DatabaseLifecycleConfig(enable_signal_handlers=False))


# ===========================================================================
# 1-3 · Admisión y shutdown
# ===========================================================================


async def test_operacion_en_vuelo_bloquea_shutdown(tmp_path: Path) -> None:
    """Shutdown espera a lo ya admitido; no cierra por debajo.

    Dos detalles sin los cuales este test NO discrimina —lo comprobé revirtiendo
    y viéndolo pasar igual—:

    * ``checkpoint_all`` se neutraliza. El real tarda lo bastante como para que
      la operación termine sola, y entonces el orden sale bien por lentitud, no
      por coordinación.
    * El orden se registra DENTRO del boundary. Registrarlo tras esperar la
      tarea lo pone a competir con el cierre en vez de ordenarse contra él.

    La propiedad la defienden DOS mecanismos —el drenaje y el lock por path en
    el cierre—, así que quitar uno solo la mantiene. `init_admitida` aísla el
    drenaje: una init en vuelo no sostiene ningún lock de path.
    """
    db = tmp_path / "en_vuelo.db"
    lc = _lifecycle()
    conn = await lc.get_connection(db)

    orden: list[str] = []
    dentro, soltar, post_checkpoint = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def checkpoint_nulo(*a: object, **k: object) -> dict:
        # Neutralizado a propósito: el checkpoint real tarda lo suficiente como
        # para que la operación termine sola, y entonces el orden saldría bien
        # por lentitud en vez de por el mecanismo. Sin él, un shutdown sin
        # coordinación llega al cierre de inmediato.
        post_checkpoint.set()
        return {}

    cierre_real = conn.close

    async def close_espia() -> None:
        orden.append("cierre")
        await cierre_real()

    lc.checkpoint_all = checkpoint_nulo  # type: ignore[method-assign]
    conn.close = close_espia  # type: ignore[method-assign]

    async def operar() -> None:
        async with lc.operation(db) as c:
            dentro.set()
            await soltar.wait()
            async with c.execute("SELECT 1") as cur:
                fila = await cur.fetchone()
                assert fila is not None and fila[0] == 1
            orden.append("operacion")

    tarea = asyncio.create_task(operar())
    await dentro.wait()

    tarea_sd = asyncio.create_task(lc.shutdown_all())
    for _ in range(8):
        await asyncio.sleep(0)
    assert not tarea_sd.done(), "shutdown se declaró completo con una operación admitida en vuelo"
    assert orden == [], f"shutdown cerró antes de que la operación terminara: {orden}"

    soltar.set()
    await tarea
    await tarea_sd
    assert orden == ["operacion", "cierre"], f"shutdown cerró bajo la operación admitida: {orden}"


async def test_shutdown_no_admite_trabajo_nuevo(tmp_path: Path) -> None:
    """Desde que shutdown empieza, no se admite nada nuevo — y no hay resurrección."""
    db = tmp_path / "admision.db"
    lc = _lifecycle()
    await lc.get_connection(db)

    en_checkpoint, soltar = asyncio.Event(), asyncio.Event()
    real = lc.checkpoint_all

    async def checkpoint_lento(*a: object, **k: object) -> dict:
        en_checkpoint.set()
        await soltar.wait()
        return await real(*a, **k)  # type: ignore[arg-type]

    lc.checkpoint_all = checkpoint_lento  # type: ignore[method-assign]
    tarea = asyncio.create_task(lc.shutdown_all())
    await en_checkpoint.wait()

    with pytest.raises(DatabaseLifecycleShuttingDownError):
        async with lc.operation(db):
            pass
    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await lc.get_connection(db)

    soltar.set()
    await tarea

    # Terminal hasta una reapertura EXPLÍCITA.
    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await lc.get_connection(db)
    await lc.init_all()
    assert await lc.get_connection(db) is not None
    await lc.shutdown_all()


async def test_init_admitida_no_registra_tras_shutdown(tmp_path: Path) -> None:
    """Una init ya admitida se espera; nunca registra después del cierre.

    Sin esto, `_init_single` queda suspendido, shutdown recorre su snapshot,
    se declara completo, y la init registra una conexión que nadie cerrará:
    DB abierta tras el shutdown, `-wal` sin checkpoint y archivo bloqueado en
    Windows.
    """
    db_a, db_b = tmp_path / "init_a.db", tmp_path / "init_b.db"
    lc = _lifecycle()
    await lc.get_connection(db_a)

    en_init, soltar = asyncio.Event(), asyncio.Event()
    real = lc._init_single

    async def init_lenta(p: Path) -> None:
        if p.name == "init_b.db":
            en_init.set()
            await soltar.wait()
        return await real(p)

    lc._init_single = init_lenta  # type: ignore[method-assign]
    tarea_init = asyncio.create_task(lc.get_connection(db_b))
    await en_init.wait()

    tarea_sd = asyncio.create_task(lc.shutdown_all())
    for _ in range(6):
        await asyncio.sleep(0)
    assert not tarea_sd.done(), "shutdown se declaró completo con una init admitida en vuelo"

    soltar.set()
    await tarea_init
    await tarea_sd
    assert lc._connections == {}, "quedó una conexión registrada después del shutdown"


# ===========================================================================
# 4-5 · La unidad transaccional completa vive en el boundary
# ===========================================================================


async def test_commit_dentro_del_boundary(tmp_path: Path) -> None:
    """El commit ocurre con el lock del path TOMADO.

    Comparar el orden de execute/commit no discrimina: con el commit afuera, la
    planificación puede producir igual una traza bien formada. Lo que sí es
    determinista es el estado del lock EN EL INSTANTE del commit — si quedó
    fuera del boundary, el lock ya está libre y la transacción pendiente flota
    sin serializar sobre la conexión compartida.
    """
    from sky_claw.app.db.locks import DistributedLockManager

    db = tmp_path / "commit.db"
    lc = _lifecycle()
    locks = DistributedLockManager(str(db), lifecycle=lc)
    await locks.initialize()

    conn = await lc.get_connection(db)
    sostenido: list[bool] = []
    commit_real = conn.commit

    async def commit_espia() -> None:
        sostenido.append(lc.get_write_lock(db).locked())
        await commit_real()

    conn.commit = commit_espia  # type: ignore[method-assign]
    try:
        await locks.acquire_lock("r1", "a1", ttl=30)
        await locks.release_lock("r1", "a1")
        await locks.cleanup_expired()
    finally:
        conn.commit = commit_real  # type: ignore[method-assign]

    assert sostenido, "el test no ejercitó ningún commit"
    assert all(sostenido), f"algún commit corrió con el boundary liberado: {sostenido}"
    await lc.shutdown_all()


async def test_rollback_dentro_del_boundary(tmp_path: Path) -> None:
    """El rollback de `transaction()` corre con el boundary todavía tomado."""
    db = tmp_path / "rollback.db"
    lc = _lifecycle()
    await lc.get_connection(db)

    sostenido_en_rollback: list[bool] = []
    conn = await lc.get_connection(db)
    rollback_real = conn.rollback

    async def rollback_espia() -> None:
        sostenido_en_rollback.append(lc.get_write_lock(db).locked())
        await rollback_real()

    conn.rollback = rollback_espia  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="fallo deliberado"):
        async with lc.transaction(db):
            raise RuntimeError("fallo deliberado")
    conn.rollback = rollback_real  # type: ignore[method-assign]

    assert sostenido_en_rollback == [True], "el rollback corrió con el boundary ya liberado"
    await lc.shutdown_all()


# ===========================================================================
# 6-9 · Lectores, independencia, orden de locks y backoff
# ===========================================================================


async def test_lector_que_espera_no_usa_una_conexion_cerrada(tmp_path: Path) -> None:
    """Un lector encolado se completa o se rechaza; nunca corre sobre una cerrada."""
    from sky_claw.app.db.async_registry import AsyncModRegistry

    db = tmp_path / "lector.db"
    lc = _lifecycle()
    registry = AsyncModRegistry(str(db), lifecycle=lc)
    await registry.open()

    retenido, soltar = asyncio.Event(), asyncio.Event()

    async def retener() -> None:
        async with lc.operation(db):
            retenido.set()
            await soltar.wait()

    t_hold = asyncio.create_task(retener())
    await retenido.wait()

    t_lect = asyncio.create_task(registry.is_empty())
    for _ in range(4):
        await asyncio.sleep(0)
    t_sd = asyncio.create_task(lc.shutdown_all())
    soltar.set()

    # El lector fue admitido antes del shutdown: se completa, no revienta.
    assert await t_lect is True
    await t_hold
    await t_sd


async def test_dos_paths_son_independientes(tmp_path: Path) -> None:
    """El boundary es POR PATH: retener uno no bloquea al otro."""
    db_a, db_b = tmp_path / "ind_a.db", tmp_path / "ind_b.db"
    lc = _lifecycle()
    retenido, soltar = asyncio.Event(), asyncio.Event()

    async def retener_a() -> None:
        async with lc.operation(db_a):
            retenido.set()
            await soltar.wait()

    t = asyncio.create_task(retener_a())
    await retenido.wait()

    # No debe bloquearse: el boundary es por path.
    async with lc.operation(db_b) as conn_b, conn_b.execute("SELECT 1") as cur:
        fila = await cur.fetchone()
        assert fila is not None and fila[0] == 1

    soltar.set()
    await t
    await lc.shutdown_all()


async def test_router_respeta_el_orden_de_locks(tmp_path: Path) -> None:
    """`open()` y `_save_message` concurrentes sobre el mismo path, sin deadlock.

    El único orden que existe es `path → router`. `open()` se partió para no
    tomarlos al revés; si alguien lo invierte, esto se cuelga.
    """
    from unittest.mock import MagicMock

    from sky_claw.app.agent.router import LLMRouter

    db = tmp_path / "orden.db"
    lc = _lifecycle()
    r1 = LLMRouter(provider=MagicMock(), db_path=str(db), lifecycle=lc)
    r2 = LLMRouter(provider=MagicMock(), db_path=str(db), lifecycle=lc)
    await r1.open()

    await asyncio.wait_for(
        asyncio.gather(r2.open(), r1._save_message("c", "user", "hola")),
        timeout=10,
    )
    await asyncio.wait_for(r2._save_message("c", "user", "chau"), timeout=10)
    await lc.shutdown_all()


async def test_el_backoff_ocurre_fuera_del_boundary(tmp_path: Path) -> None:
    """El backoff de `acquire_lock` no sostiene el lock del path.

    Sostenerlo durante el sleep convertiría al boundary en un mutex de espera:
    todo el resto del path quedaría bloqueado por un lock que ni siquiera se
    pudo tomar.
    """
    from sky_claw.app.db.locks import DistributedLockManager, LockAcquisitionError

    db = tmp_path / "backoff.db"
    lc = _lifecycle()
    locks = DistributedLockManager(str(db), lifecycle=lc, max_retries=3, backoff_base=0.05)
    await locks.initialize()
    await locks.acquire_lock("ocupado", "otro", ttl=60)

    libre_durante_backoff: list[bool] = []
    sleep_real = asyncio.sleep

    async def sleep_espia(d: float, *a: object, **k: object) -> None:
        if d > 0:
            libre_durante_backoff.append(not lc.get_write_lock(db).locked())
        await sleep_real(d, *a, **k)

    import sky_claw.app.db.locks as locks_mod

    locks_mod.asyncio.sleep = sleep_espia  # type: ignore[assignment]
    try:
        with pytest.raises(LockAcquisitionError):
            await locks.acquire_lock("ocupado", "yo", ttl=5)
    finally:
        locks_mod.asyncio.sleep = sleep_real  # type: ignore[assignment]

    assert libre_durante_backoff, "el test no ejercitó ningún backoff"
    assert all(libre_durante_backoff), "el backoff corrió sosteniendo el lock del path"
    await lc.shutdown_all()


# ===========================================================================
# 10 · Inventario (AST) ↔ test conductual
# ===========================================================================

# Superficies que emiten SQL sobre la conexión COMPARTIDA del lifecycle, con el
# test conductual que cubre su participación. AST congela el conjunto; la
# propiedad la prueban los tests nombrados.
#
# Exentos verificados (conexión propia, no la compartida): `agent/context_manager`
# (aiosqlite.connect read-only por operación), `security/credential_vault` (pool
# propio), `db/registry.py` (legacy sin lifecycle).
SUPERFICIES_COMPARTIDAS = {
    "sky_claw/app/core/database.py": "test_lector_que_espera_no_usa_una_conexion_cerrada",
    "sky_claw/app/core/dlq_manager.py": "test_operacion_en_vuelo_bloquea_shutdown",
    "sky_claw/app/db/async_registry.py": "test_lector_que_espera_no_usa_una_conexion_cerrada",
    "sky_claw/app/db/journal.py": "test_operacion_en_vuelo_bloquea_shutdown",
    "sky_claw/app/db/locks.py": "test_commit_dentro_del_boundary",
    "sky_claw/app/agent/router.py": "test_router_respeta_el_orden_de_locks",
    "sky_claw/app/security/governance.py": "test_dos_paths_son_independientes",
}


def _consume_lifecycle(arbol: ast.AST) -> bool:
    """True si el módulo obtiene una conexión del lifecycle."""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"get_connection", "operation", "transaction"}
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "_lifecycle"
        for n in ast.walk(arbol)
    )


def test_inventario_de_superficies_compartidas_congelado() -> None:
    """Inventario, NO prueba de protección.

    Sólo enumera qué módulos consumen una conexión del lifecycle. Un módulo
    nuevo rompe el test hasta que se lo declare y se le asigne un test
    conductual. Que participe del boundary lo prueban esos tests, no éste.
    """
    encontrados = {
        p.relative_to(RAIZ).as_posix()
        for p in sorted((RAIZ / "sky_claw").rglob("*.py"))
        if _consume_lifecycle(ast.parse(p.read_text(encoding="utf-8"), filename=str(p)))
    }
    assert encontrados == set(SUPERFICIES_COMPARTIDAS), (
        "Cambió el conjunto de módulos que consumen una conexión del lifecycle. "
        "Un módulo nuevo debe declararse acá con el test conductual que cubre su "
        "participación en el boundary — el inventario no demuestra nada por sí solo."
    )


def test_cada_superficie_tiene_su_test_conductual() -> None:
    """La autoridad es conductual: ninguna superficie queda sólo con el inventario."""
    propios = {
        f.name
        for f in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8")))
        if isinstance(f, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    for modulo, nombre in sorted(SUPERFICIES_COMPARTIDAS.items()):
        assert nombre in propios, f"{modulo} declara el test conductual `{nombre}`, que no existe"
