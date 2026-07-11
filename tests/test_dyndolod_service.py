"""Tests for DynDOLODPipelineService.

Sprint 2, Fase 3: Validates transactional pipeline execution, event
publication, journal lifecycle, and rollback on unexpected errors.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus
from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    LockInfo,
)
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODExecutionError,
    DynDOLODPipelineResult,
    DynDOLODRunner,
    DynDOLODTimeoutError,
    ToolExecutionResult,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_lock_manager() -> AsyncMock:
    mgr = AsyncMock(spec=DistributedLockManager)
    mgr.acquire_lock = AsyncMock(
        return_value=LockInfo(
            resource_id="dyndolod-pipeline",
            agent_id="dyndolod-pipeline-service",
            acquired_at=1000.0,
            expires_at=1600.0,
        )
    )
    mgr.release_lock = AsyncMock(return_value=True)
    return mgr


@pytest.fixture
def mock_snapshot_manager() -> AsyncMock:
    mgr = AsyncMock(spec=FileSnapshotManager)
    mgr.create_snapshot = AsyncMock(
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
    mgr.restore_snapshot = AsyncMock(return_value=True)
    return mgr


@pytest.fixture
def mock_journal() -> AsyncMock:
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=42)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    journal.log_operation = AsyncMock()
    return journal


@pytest.fixture
def mock_path_resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=None)
    resolver.get_mo2_path = MagicMock(return_value=None)
    resolver.get_mo2_mods_path = MagicMock(return_value=None)
    resolver.get_dyndolod_exe = MagicMock(return_value=None)
    resolver.get_texgen_exe = MagicMock(return_value=None)
    return resolver


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def service(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
) -> DynDOLODPipelineService:
    return DynDOLODPipelineService(
        lock_manager=mock_lock_manager,
        snapshot_manager=mock_snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=mock_event_bus,
    )


def _make_success_result(
    *,
    run_texgen: bool = True,
    texgen_mod: pathlib.Path | None = None,
    dyndolod_mod: pathlib.Path | None = None,
) -> DynDOLODPipelineResult:
    """Helper to build a successful DynDOLODPipelineResult."""
    texgen_result = (
        ToolExecutionResult(
            success=True,
            tool_name="TexGen",
            return_code=0,
            stdout="OK",
            stderr="",
            output_path=pathlib.Path("/tmp/TexGen_Output"),
            duration_seconds=10.0,
        )
        if run_texgen
        else None
    )

    dyndolod_result = ToolExecutionResult(
        success=True,
        tool_name="DynDOLOD",
        return_code=0,
        stdout="OK",
        stderr="",
        output_path=pathlib.Path("/tmp/DynDOLOD_Output"),
        duration_seconds=30.0,
    )

    return DynDOLODPipelineResult(
        success=True,
        texgen_result=texgen_result,
        dyndolod_result=dyndolod_result,
        texgen_mod_path=texgen_mod or pathlib.Path("/mods/TexGen Output"),
        dyndolod_mod_path=dyndolod_mod or pathlib.Path("/mods/DynDOLOD Output"),
        errors=[],
    )


def _make_failure_result() -> DynDOLODPipelineResult:
    """Helper to build a failed DynDOLODPipelineResult."""
    return DynDOLODPipelineResult(
        success=False,
        texgen_result=None,
        dyndolod_result=None,
        errors=["TexGen failed"],
    )


# =============================================================================
# Happy path
# =============================================================================


@pytest.mark.asyncio
async def test_execute_success_publishes_events(
    service: DynDOLODPipelineService,
    mock_event_bus: AsyncMock,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Successful pipeline publishes started and completed events."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_success_result())
    mock_runner.validate_dyndolod_output = AsyncMock(return_value=True)

    # Provide _config for path resolution
    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="High", run_texgen=True, create_snapshot=False)

    assert result["success"] is True

    # Verify started + completed events published
    assert mock_event_bus.publish.call_count == 2
    started_event = mock_event_bus.publish.call_args_list[0][0][0]
    completed_event = mock_event_bus.publish.call_args_list[1][0][0]
    assert started_event.topic == "pipeline.dyndolod.started"
    assert completed_event.topic == "pipeline.dyndolod.completed"
    assert completed_event.payload["success"] is True

    # Journal committed
    mock_journal.begin_transaction.assert_called_once()
    mock_journal.commit_transaction.assert_called_once_with(42)
    mock_journal.mark_transaction_rolled_back.assert_not_called()


@pytest.mark.asyncio
async def test_execute_success_returns_pipeline_data(
    service: DynDOLODPipelineService,
    tmp_path: pathlib.Path,
) -> None:
    """Successful execution returns dataclass fields in the dict."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_success_result())
    mock_runner.validate_dyndolod_output = AsyncMock(return_value=True)

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is True
    assert "duration_seconds" in result


# =============================================================================
# Domain error handling
# =============================================================================


@pytest.mark.asyncio
async def test_execute_domain_error_marks_rollback(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """DynDOLODExecutionError inside the lock triggers journal rollback."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_failure_result())

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert result["rolled_back"] is True

    # Journal transaction rolled back
    mock_journal.mark_transaction_rolled_back.assert_called_once_with(42)
    mock_journal.commit_transaction.assert_not_called()

    # Completed event emitted with error
    completed_calls = [
        c for c in mock_event_bus.publish.call_args_list if c[0][0].topic == "pipeline.dyndolod.completed"
    ]
    assert len(completed_calls) == 1
    assert completed_calls[0][0][0].payload["success"] is False


@pytest.mark.asyncio
async def test_execute_timeout_error_marks_rollback(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """DynDOLODTimeoutError triggers journal rollback."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(
        side_effect=DynDOLODTimeoutError(timeout_seconds=14400, tool_name="DynDOLOD")
    )

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert result["rolled_back"] is True
    mock_journal.mark_transaction_rolled_back.assert_called_once_with(42)


# =============================================================================
# PREVENCIÓN T11: Unexpected exception safety net
# =============================================================================


@pytest.mark.asyncio
async def test_unexpected_oserror_marks_rollback_and_emits_completed(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Unexpected OSError marks TX rolled back and emits completed event.

    Lección T11: NUNCA dejar una transacción en estado PENDING.
    """
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(side_effect=OSError("Disk full during validation"))

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="Low", run_texgen=False, create_snapshot=False)

    # Must NOT raise — returns error dict
    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "Disk full" in result["errors"][0]

    # Journal rollback called
    mock_journal.mark_transaction_rolled_back.assert_called_once_with(42)
    mock_journal.commit_transaction.assert_not_called()

    # Completed event emitted with error details
    completed_calls = [
        c for c in mock_event_bus.publish.call_args_list if c[0][0].topic == "pipeline.dyndolod.completed"
    ]
    assert len(completed_calls) == 1
    payload = completed_calls[0][0][0].payload
    assert payload["success"] is False
    assert payload["rolled_back"] is True


@pytest.mark.asyncio
async def test_unexpected_error_with_journal_failure(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Even if journal.mark_transaction_rolled_back fails, the service still returns error dict."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(side_effect=RuntimeError("Unexpected crash"))
    mock_journal.mark_transaction_rolled_back = AsyncMock(side_effect=OSError("Journal DB locked"))

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    # Must NOT raise even with double failure
    result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert result["rolled_back"] is True

    # Completed event still emitted
    completed_calls = [
        c for c in mock_event_bus.publish.call_args_list if c[0][0].topic == "pipeline.dyndolod.completed"
    ]
    assert len(completed_calls) == 1


# =============================================================================
# Lock contention
# =============================================================================


@pytest.mark.asyncio
async def test_lock_acquisition_failure_returns_error(
    service: DynDOLODPipelineService,
    mock_lock_manager: AsyncMock,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """LockAcquisitionError returns error dict without touching journal."""
    mock_lock_manager.acquire_lock = AsyncMock(
        side_effect=LockAcquisitionError("dyndolod-pipeline", "dyndolod-pipeline-service")
    )

    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config
    service._runner = mock_runner

    result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert "Lock acquisition failed" in result["errors"][0]

    # Journal never started
    mock_journal.begin_transaction.assert_not_called()
    mock_journal.commit_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_not_called()


# =============================================================================
# Init failure
# =============================================================================


@pytest.mark.asyncio
async def test_runner_init_failure_returns_error(
    service: DynDOLODPipelineService,
    mock_event_bus: AsyncMock,
) -> None:
    """If _ensure_runner raises, execute returns error dict with events."""
    with patch.dict("os.environ", {}, clear=True):
        result = await service.execute(preset="Medium", run_texgen=True)

    assert result["success"] is False
    assert len(result["errors"]) > 0

    # Both started and completed events still published
    assert mock_event_bus.publish.call_count == 2


# =============================================================================
# Validation failure
# =============================================================================


@pytest.mark.asyncio
async def test_validation_failure_triggers_rollback(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Failed DynDOLOD output validation triggers journal rollback."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_success_result())
    mock_runner.validate_dyndolod_output = AsyncMock(return_value=False)

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="High", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert result["rolled_back"] is True
    mock_journal.mark_transaction_rolled_back.assert_called_once_with(42)


# =============================================================================
# Tests: DynDOLODPipelineService — dry_run / preview (plan-only estimate)
# =============================================================================


@pytest.mark.asyncio
async def test_execute_dry_run_estimates_lods_without_running(
    service: DynDOLODPipelineService,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_journal: AsyncMock,
    mock_lock_manager: AsyncMock,
) -> None:
    """dry_run=True returns a plan-only LOD estimate; runs no exe, touches nothing.

    DynDOLOD is the most expensive stage (GBs, 30+ min) so the preview must NOT
    run it: no runner, no lock, no journal transaction, no events.  The estimate
    is derived from the resolver paths alone (no DynDOLOD binary required).
    """
    mods = pathlib.Path("/mods")
    mock_path_resolver.get_mo2_mods_path = MagicMock(return_value=mods)

    result = await service.execute(preset="High", run_texgen=True, dry_run=True)

    assert result["status"] == "dry_run_preview"
    change_set = result["change_set"]
    assert change_set["stage"] == "dyndolod"
    assert change_set["executed_for_real"] is False

    plan = change_set["lod_plan"]
    assert plan["preset"] == "High"
    assert "DynDOLOD.esp" in plan["would_generate"]
    assert any("DynDOLOD Output" in d for d in plan["output_dirs"])

    # The whole expensive path is skipped: nothing locked, journaled, or emitted.
    mock_lock_manager.acquire_lock.assert_not_awaited()
    mock_journal.begin_transaction.assert_not_awaited()
    mock_event_bus.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_dry_run_without_texgen_omits_texgen(
    service: DynDOLODPipelineService,
    mock_path_resolver: MagicMock,
) -> None:
    """run_texgen=False keeps TexGen out of the estimated outputs."""
    mock_path_resolver.get_mo2_mods_path = MagicMock(return_value=pathlib.Path("/mods"))

    result = await service.execute(preset="Medium", run_texgen=False, dry_run=True)

    plan = result["change_set"]["lod_plan"]
    assert plan["preset"] == "Medium"
    assert not any("TexGen" in item for item in plan["would_generate"])


# =============================================================================
# DD-1: rollback move-aside del directorio DynDOLOD Output/
# =============================================================================


def _mock_runner_with_output(mods: pathlib.Path) -> AsyncMock:
    """Runner mock con _config real y los nombres de mod expuestos."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_config = MagicMock()
    mock_config.mo2_mods_path = mods
    mock_runner._config = mock_config
    # spec=DynDOLODRunner deja los class attrs como Mock; fijarlos a los strings reales.
    mock_runner.DYNDOLLOD_MOD_NAME = "DynDOLOD Output"
    mock_runner.TEXGEN_MOD_NAME = "TexGen Output"
    return mock_runner


@pytest.mark.asyncio
async def test_directory_rollback_restores_output_on_failure(
    service: DynDOLODPipelineService,
    mock_snapshot_manager: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """create_snapshot=True: un fallo del pipeline restaura DynDOLOD Output/ intacto."""
    mods = tmp_path / "mods"
    output_dir = mods / "DynDOLOD Output"
    output_dir.mkdir(parents=True)
    (output_dir / "sentinel.esp").write_text("ORIGINAL", encoding="utf-8")
    (output_dir / "textures").mkdir()
    (output_dir / "textures" / "a.dds").write_bytes(b"\xde\xad")

    runner = _mock_runner_with_output(mods)

    async def _fake_pipeline(**_kwargs: object) -> DynDOLODPipelineResult:
        # El move-aside dejó el dir movido; el runner "regenera" un parcial y falla.
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "partial.esp").write_text("PARTIAL", encoding="utf-8")
        raise DynDOLODExecutionError("boom durante la generación")

    runner.run_full_pipeline = AsyncMock(side_effect=_fake_pipeline)
    service._runner = runner

    result = await service.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is False
    assert result["rolled_back"] is True
    # Directorio original restaurado byte-a-byte; el parcial se descartó.
    assert (output_dir / "sentinel.esp").read_text(encoding="utf-8") == "ORIGINAL"
    assert (output_dir / "textures" / "a.dds").read_bytes() == b"\xde\xad"
    assert not (output_dir / "partial.esp").exists()
    assert not list(mods.glob("DynDOLOD Output.rollback-*"))  # sin backups huérfanos
    # El .esp ya NO se snapshotea vía FileSnapshotManager (target_files=[]).
    mock_snapshot_manager.create_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_directory_rollback_discards_backup_on_success(
    service: DynDOLODPipelineService,
    tmp_path: pathlib.Path,
) -> None:
    """create_snapshot=True: en éxito queda el output nuevo y el backup se descarta."""
    mods = tmp_path / "mods"
    output_dir = mods / "DynDOLOD Output"
    output_dir.mkdir(parents=True)
    (output_dir / "old.esp").write_text("OLD", encoding="utf-8")

    runner = _mock_runner_with_output(mods)

    async def _fake_pipeline(**_kwargs: object) -> DynDOLODPipelineResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "new.esp").write_text("NEW", encoding="utf-8")
        return _make_success_result(run_texgen=False)

    runner.run_full_pipeline = AsyncMock(side_effect=_fake_pipeline)
    runner.validate_dyndolod_output = AsyncMock(return_value=True)
    service._runner = runner

    result = await service.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is True
    assert (output_dir / "new.esp").read_text(encoding="utf-8") == "NEW"
    assert not (output_dir / "old.esp").exists()  # el output previo se descartó (regenerado)
    assert not list(mods.glob("DynDOLOD Output.rollback-*"))


# =============================================================================
# S-4: drain con cota en el path de éxito
# =============================================================================


class _EOFStream:
    """StreamReader falso que devuelve EOF de inmediato."""

    async def read(self, _n: int) -> bytes:
        return b""


class _HangingStream:
    """StreamReader falso que nunca emite EOF (simula un nieto que heredó el pipe)."""

    async def read(self, _n: int) -> bytes:
        await asyncio.Event().wait()  # se bloquea para siempre
        return b""  # pragma: no cover


class TestExecuteProcessDrainGrace:
    """S-4: el branch de salida normal no debe colgarse si un drain nunca ve EOF.

    Si DynDOLOD/TexGen deja un nieto que hereda el pipe y sobrevive al padre, el
    write-end nunca cierra y `_drain` no recibe EOF. Sin una cota, el `gather` del
    path de éxito colgaría `_execute_process` pasado incluso el timeout global.
    """

    async def test_drain_colgado_en_exito_retorna_dentro_del_grace(self) -> None:
        from sky_claw.local.tools import dyndolod_runner as ddl

        proc = MagicMock()
        proc.stdout = _EOFStream()
        proc.stderr = _HangingStream()  # este drain nunca termina
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)  # el proceso "salió" con éxito

        config = MagicMock()
        config.timeout_seconds = 3600
        config.heartbeat_interval = 60
        runner = ddl.DynDOLODRunner(config)

        with (
            patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(ddl, "_DRAIN_GRACE_SECONDS", 0.1),
        ):
            # Con el fix retorna dentro del grace (0.1s); sin él, el gather del
            # path de éxito cuelga y este wait_for externo dispararía TimeoutError.
            stdout, stderr, return_code, _duration = await asyncio.wait_for(
                runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "DynDOLOD"),
                timeout=5.0,
            )

        assert return_code == 0
        assert stdout == ""  # EOF inmediato
        assert stderr == ""  # drain cancelado tras el grace → output parcial
