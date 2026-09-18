"""#592.1 — semántica de preservado_para_deployment en el cierre de TX."""

from __future__ import annotations

import logging
import pathlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.journal import JournalTransactionError
from sky_claw.app.db.locks import DistributedLockManager, LockInfo
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

# Sólo helpers (funciones, no fixtures): el `service` se construye localmente abajo.
from tests.test_dyndolod_service import (
    _pipeline_con_texgen_current,
    _texgen_que_genera_current,
)


@pytest.fixture
def service() -> DynDOLODPipelineService:
    """Fixture local autosuficiente (#594 review P2).

    Réplica mínima del `service` de `tests/test_dyndolod_service.py`. Se define
    acá — en vez de reusarla vía `pytest_plugins = ("tests.test_dyndolod_service",)`
    — porque esa directiva a nivel de módulo es sensible al ORDEN de colección: si
    pytest ya importó ese módulo como test normal antes de procesar este (p. ej.
    `pytest tests/test_dyndolod_service.py tests/test_dyndolod_preserved_rollback_tx.py`),
    sus fixtures no quedan registradas como plugin y los cuatro tests fallan con
    `fixture 'service' not found`. Un fixture local es determinista en cualquier
    orden sin registrar otro módulo de tests como plugin ni ampliar el blast radius
    a un conftest compartido. Importar las fixtures tampoco sirve: el linter (F811)
    trata el parámetro `service` de cada test como redefinición del símbolo
    importado. Si el constructor de `DynDOLODPipelineService` cambia, F-02 —que
    ejercita `execute()` completo— rompe en voz alta, así que la copia no puede
    driftar en silencio.
    """
    lock_info = LockInfo(
        resource_id="dyndolod-pipeline",
        agent_id="dyndolod-pipeline-service",
        acquired_at=time.time(),
        expires_at=time.time() + 3600.0,
    )
    lock_manager = AsyncMock(spec=DistributedLockManager)
    # acquire/get_lock_info COHERENTES (mismo objeto) para el fencing assert_owned.
    lock_manager.acquire_lock = AsyncMock(return_value=lock_info)
    lock_manager.get_lock_info = AsyncMock(return_value=lock_info)
    lock_manager.release_lock = AsyncMock(return_value=True)

    snapshot_manager = AsyncMock(spec=FileSnapshotManager)
    snapshot_manager.create_snapshot = AsyncMock(
        return_value=SnapshotInfo(
            snapshot_id="snap-001",
            original_path="/mods/DynDOLOD Output/DynDOLOD.esp",
            snapshot_path="/snapshots/snap-001",
            checksum="abc123",
            size_bytes=1024,
            created_at=MagicMock(),
            metadata=None,
        )
    )
    snapshot_manager.restore_snapshot = AsyncMock(return_value=True)

    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=42)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    journal.log_operation = AsyncMock()
    journal.consultar_handoff_activo = AsyncMock(return_value=None)
    journal.transacciones_que_nombran = AsyncMock(return_value=[])

    path_resolver = MagicMock()
    path_resolver.get_skyrim_path = MagicMock(return_value=None)
    path_resolver.get_mo2_path = MagicMock(return_value=None)
    path_resolver.get_mo2_instance_data_root = MagicMock(return_value=None)
    path_resolver.get_mo2_mods_path = MagicMock(return_value=None)
    path_resolver.get_dyndolod_exe = MagicMock(return_value=None)
    path_resolver.get_texgen_exe = MagicMock(return_value=None)

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    return DynDOLODPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        path_resolver=path_resolver,
        event_bus=event_bus,
        mo2_profile="Default",  # D2: el gate de perfil exige identidad de dueño
    )


def _protector(target: pathlib.Path, *, rollback_completed: bool) -> MagicMock:
    protector = MagicMock()
    protector.target = target
    protector.rollback_completed = rollback_completed
    return protector


@pytest.mark.asyncio
async def test_cerrar_tx_con_mutacion_preservada_no_marca_rolled_back(
    service: DynDOLODPipelineService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R1+R2 (#592.1): preservado vivo ⇒ TX no rolled back y log deliberado.

    El resto de protectores puede haber cerrado bien. Eso no autoriza afirmar
    rollback total: hay una mutación viva por diseño.
    """
    preservado = pathlib.Path("/mods/TexGen Output")
    dir_rollbacks = [
        _protector(preservado, rollback_completed=False),
        _protector(pathlib.Path("/work/TexGen"), rollback_completed=True),
    ]

    with caplog.at_level(logging.DEBUG, logger="SkyClaw.DynDOLODPipelineService"):
        rolled_back = await service._cerrar_tx_tras_rollback(
            42,
            dir_rollbacks,
            journal_committed=False,
            mutation_started=True,
            mutation_coverage_complete=True,
            contexto="error de dominio",
            preservado_para_deployment=preservado,
        )

    assert rolled_back is False
    service._journal.mark_transaction_rolled_back.assert_not_awaited()
    mensajes = [r.getMessage() for r in caplog.records]
    assert any("PRESERVADA a propósito" in msg and "PENDIENTE" in msg for msg in mensajes)
    assert not any("rollback INCOMPLETO" in msg for msg in mensajes)


@pytest.mark.asyncio
async def test_cerrar_tx_sin_preservado_con_rollback_incompleto_sigue_fail_closed(
    service: DynDOLODPipelineService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3 (#592.1): sin preservación, un rollback roto sigue siendo incompleto."""
    vivo = pathlib.Path("/mods/DynDOLOD Output")
    dir_rollbacks = [_protector(vivo, rollback_completed=False)]

    with caplog.at_level(logging.DEBUG, logger="SkyClaw.DynDOLODPipelineService"):
        rolled_back = await service._cerrar_tx_tras_rollback(
            42,
            dir_rollbacks,
            journal_committed=False,
            mutation_started=True,
            mutation_coverage_complete=True,
            contexto="error de dominio",
        )

    assert rolled_back is False
    service._journal.mark_transaction_rolled_back.assert_not_awaited()
    criticos = [r for r in caplog.records if "rollback INCOMPLETO" in r.getMessage()]
    assert criticos
    assert criticos[0].levelno == logging.CRITICAL
    assert not any("PRESERVADA a propósito" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cerrar_tx_sin_preservado_con_rollback_completo_marca_rolled_back(
    service: DynDOLODPipelineService,
) -> None:
    """R4 (#592.1): sin mutación viva, el cierre histórico se conserva."""
    cerrado = pathlib.Path("/mods/DynDOLOD Output")
    dir_rollbacks = [_protector(cerrado, rollback_completed=True)]

    rolled_back = await service._cerrar_tx_tras_rollback(
        42,
        dir_rollbacks,
        journal_committed=False,
        mutation_started=True,
        mutation_coverage_complete=True,
        contexto="error de dominio",
    )

    assert rolled_back is True
    service._journal.mark_transaction_rolled_back.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_certificacion_fallida_no_reporta_preservacion_como_rollback_incompleto(
    service: DynDOLODPipelineService,
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-02 / #592.1: preservar + certificar mal no cierra la TX como rolled back.

    El camino productivo descarta hoy el path de `_preservar_mod_de_texgen` y el
    cierre transaccional cuenta esa mutación viva como rollback roto. El artifact
    sigue en disco a propósito: la TX queda PENDIENTE y el log no puede pedir
    que se auditen backups como si el restore hubiera fallado.
    """
    _config, runner, staging, mod_texgen = _pipeline_con_texgen_current(tmp_path, service)
    service._journal.crear_handoff_de_deployment = AsyncMock(
        side_effect=JournalTransactionError("handoff no durable", transaction_id=42)
    )

    with (
        caplog.at_level(logging.WARNING, logger="SkyClaw.DynDOLODPipelineService"),
        patch.object(runner, "_execute_process", _texgen_que_genera_current(tmp_path, staging)),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["success"] is False
    assert result["rolled_back"] is False
    assert "needs_deployment" not in result
    assert (mod_texgen / "textures" / "a.dds").read_bytes() == b"CURRENT!"
    service._journal.mark_transaction_rolled_back.assert_not_awaited()
    service._journal.commit_transaction.assert_not_awaited()
    mensajes = [r.getMessage() for r in caplog.records]
    assert any("PENDIENTE con una mutación PRESERVADA a propósito" in msg for msg in mensajes)
    assert not any("rollback INCOMPLETO" in msg for msg in mensajes)
