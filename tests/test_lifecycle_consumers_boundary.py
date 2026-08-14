"""PR-1b1 — los consumidores simples usan el boundary durante TODO su SQL.

Propiedad demostrada (carrera ejecutada, no ancla sintáctica): cuando un
consumidor migrado tiene una operación en vuelo sobre la conexión compartida,
``shutdown_all()`` **espera** a que esa operación libere el boundary antes de
cerrar — a diferencia del patrón viejo (``get_connection()`` + SQL suelto), en
el que el shutdown cerraba la conexión debajo del SQL.

Consumidores en scope: ``DatabaseAgent`` (lecturas), ``DLQManager._connect()``
y ``GovernanceManager`` (operaciones de cache). Además, política EXPLÍCITA de
``DatabaseLifecycleShuttingDownError`` en Governance.

Sincronización con ``asyncio.Event`` (barreras) + ``ceder()`` (vaciar la cola
de listos del loop). Sin sleeps.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sky_claw.app.core.database import DatabaseAgent
from sky_claw.app.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.app.core.dlq_manager import DLQManager
from sky_claw.app.security.governance import GovernanceManager

DEADLINE = 5.0


async def ceder(veces: int = 40) -> None:
    """Vacía la cola de listos del loop (no es sleep de sincronización)."""
    for _ in range(veces):
        await asyncio.sleep(0)


def _mgr(*paths: Path) -> DatabaseLifecycleManager:
    return DatabaseLifecycleManager(
        db_paths=list(paths),
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )


# ---------------------------------------------------------------------------
# DatabaseAgent — lecturas bajo _read_operation()
# ---------------------------------------------------------------------------


async def test_databaseagent_lectura_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """Una lectura de DatabaseAgent en vuelo (``_read_operation``) mantiene la
    admisión: el shutdown no puede completarse hasta que la lectura sale."""
    agent = DatabaseAgent(str(tmp_path / "agent.db"))
    await agent.init_db()
    lifecycle = agent._lifecycle
    assert lifecycle is not None

    entro, puerta = asyncio.Event(), asyncio.Event()

    async def lectura_suspendida() -> None:
        async with agent._read_operation():
            entro.set()
            await puerta.wait()

    t_op = asyncio.create_task(lectura_suspendida())
    await entro.wait()
    # PROPIEDAD PR-1b1 (discriminante): la admisión sigue ACTIVA mientras la
    # lectura usa la conexión. La reversión a get_connection() —que suelta la
    # admisión antes del yield— deja _admitted en 0 y rompe esta prueba.
    assert lifecycle._admitted >= 1, "la lectura soltó la admisión antes de terminar el SQL"
    t_sd = asyncio.create_task(lifecycle.shutdown_all())
    await ceder()
    assert not t_sd.done(), "shutdown se completó con una lectura admitida en vuelo"

    puerta.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert lifecycle._admitted == 0


async def test_databaseagent_get_memory_end_to_end_sigue_leyendo(tmp_path: Path) -> None:
    """Guard de no-regresión: la lectura pública sigue devolviendo el valor."""
    agent = DatabaseAgent(str(tmp_path / "agent.db"))
    await agent.init_db()
    try:
        await agent.set_memory("clave", "valor", 1.0)
        assert await agent.get_memory("clave") == "valor"
        assert await agent.get_memory("ausente") is None
    finally:
        await agent.close()


# ---------------------------------------------------------------------------
# DLQManager — _connect() managed
# ---------------------------------------------------------------------------


async def test_dlq_connect_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """Un caller dentro de ``DLQManager._connect()`` mantiene la admisión."""
    path = tmp_path / "dlq.db"
    lifecycle = _mgr(path)
    dlq = DLQManager(db_path=path, handler_resolver={}.get, lifecycle=lifecycle)

    entro, puerta = asyncio.Event(), asyncio.Event()

    async def uso_suspendido() -> None:
        async with dlq._connect():
            entro.set()
            await puerta.wait()

    t_op = asyncio.create_task(uso_suspendido())
    await entro.wait()
    assert lifecycle._admitted >= 1, "_connect() soltó la admisión antes de terminar el SQL"
    t_sd = asyncio.create_task(lifecycle.shutdown_all())
    await ceder()
    assert not t_sd.done(), "shutdown se completó con _connect() admitido en vuelo"

    puerta.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert lifecycle._admitted == 0


# ---------------------------------------------------------------------------
# GovernanceManager — boundary + política de shutdown
# ---------------------------------------------------------------------------


def _gov(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> GovernanceManager:
    # El autouse _reset_governance_singleton del conftest aísla el singleton.
    gov = GovernanceManager.get_instance(base_path=str(tmp_path))
    gov.set_lifecycle(lifecycle)
    return gov


async def test_governance_operacion_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """``is_scanned_and_clean`` en vuelo (suspendida en ``_ensure_schema``, que
    corre DENTRO de ``operation()``) mantiene la admisión."""
    lifecycle = _mgr()
    gov = _gov(tmp_path, lifecycle)
    archivo = tmp_path / "f.bin"
    archivo.write_bytes(b"contenido")

    entro, puerta = asyncio.Event(), asyncio.Event()
    ensure_real = gov._ensure_schema

    async def ensure_con_barrera(db: object) -> None:
        entro.set()
        await puerta.wait()
        await ensure_real(db)

    gov._ensure_schema = ensure_con_barrera  # type: ignore[method-assign]

    t_op = asyncio.create_task(gov.is_scanned_and_clean(str(archivo)))
    await entro.wait()
    assert lifecycle._admitted >= 1, "la operación de Governance soltó la admisión antes de terminar el SQL"
    t_sd = asyncio.create_task(lifecycle.shutdown_all())
    await ceder()
    assert not t_sd.done(), "shutdown se completó con una operación de Governance admitida en vuelo"

    puerta.set()
    resultado = await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert resultado is False  # el hash no está cacheado
    assert lifecycle._admitted == 0


async def test_governance_is_scanned_durante_shutdown_es_fail_closed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Llegada tardía: con el lifecycle ya cerrado, ``is_scanned_and_clean``
    captura ``DatabaseLifecycleShuttingDownError`` y devuelve False (fail-closed)."""
    lifecycle = _mgr()
    gov = _gov(tmp_path, lifecycle)
    await lifecycle.shutdown_all()  # _shutting_down: rechaza admisión nueva

    archivo = tmp_path / "f.bin"
    archivo.write_bytes(b"contenido")

    with caplog.at_level("WARNING"):
        resultado = await gov.is_scanned_and_clean(str(archivo))

    assert resultado is False
    assert any("shutdown del lifecycle" in r.message for r in caplog.records), (
        "no se registró la política explícita de shutdown (fail-closed)"
    )


async def test_governance_update_durante_shutdown_no_persiste(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Llegada tardía: ``update_scan_result`` no persiste ni propaga; loggea la
    política. Al reabrir con ``init_all()``, el hash no quedó registrado."""
    lifecycle = _mgr()
    gov = _gov(tmp_path, lifecycle)
    await lifecycle.shutdown_all()

    archivo = tmp_path / "f.bin"
    archivo.write_bytes(b"contenido")

    with caplog.at_level("WARNING"):
        await gov.update_scan_result(str(archivo), [{"rule": "x"}], "CLEAN")  # no debe lanzar

    assert any("shutdown del lifecycle" in r.message for r in caplog.records), (
        "no se registró la política explícita de shutdown (no persistido)"
    )

    # Reabrir explícitamente y confirmar que NO se persistió nada.
    await lifecycle.init_all()
    try:
        assert await gov.is_scanned_and_clean(str(archivo)) is False
    finally:
        await lifecycle.shutdown_all()
