"""Tests for DynDOLODPipelineService.

Sprint 2, Fase 3: Validates transactional pipeline execution, event
publication, journal lifecycle, and rollback on unexpected errors.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    LockInfo,
)
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODExecutionError,
    DynDOLODPipelineResult,
    DynDOLODRunner,
    DynDOLODTimeoutError,
    ToolExecutionResult,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.validators.preflight import (
    PreflightCheck,
    PreflightReport,
    PreflightStatus,
)

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
    dyndolod_output: pathlib.Path | None = pathlib.Path("/tmp/DynDOLOD_Output"),
) -> DynDOLODPipelineResult:
    """Construye un ``DynDOLODPipelineResult`` exitoso.

    ``dyndolod_output`` permite el caso U-06 de ``_find_dyndolod_output`` que no
    ubicó la salida (``None``); su default conserva el comportamiento previo.
    ``ToolExecutionResult`` es frozen, así que hay que fijarlo al construir.
    """
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
        output_path=dyndolod_output,
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
async def test_execute_domain_error_sin_snapshot_deja_tx_pendiente(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Sin snapshot, un error posterior al runner no puede afirmar rollback."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_failure_result())

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert result["rolled_back"] is False

    mock_journal.mark_transaction_rolled_back.assert_not_called()
    mock_journal.commit_transaction.assert_not_called()

    # Completed event emitted with error
    completed_calls = [
        c for c in mock_event_bus.publish.call_args_list if c[0][0].topic == "pipeline.dyndolod.completed"
    ]
    assert len(completed_calls) == 1
    assert completed_calls[0][0][0].payload["success"] is False


@pytest.mark.asyncio
async def test_execute_timeout_error_deja_tx_pendiente(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Un timeout puede haber tocado staging no protegido: la TX queda pendiente."""
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
    assert result["rolled_back"] is False
    mock_journal.mark_transaction_rolled_back.assert_not_called()


# =============================================================================
# PREVENCIÓN T11: Unexpected exception safety net
# =============================================================================


@pytest.mark.asyncio
async def test_unexpected_oserror_reports_pending_and_emits_completed(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Un OSError post-run deja PENDING si las mutaciones no están cubiertas."""
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
    assert result["rolled_back"] is False
    assert "Disk full" in result["errors"][0]

    mock_journal.mark_transaction_rolled_back.assert_not_called()
    mock_journal.commit_transaction.assert_not_called()

    # Completed event emitted with error details
    completed_calls = [
        c for c in mock_event_bus.publish.call_args_list if c[0][0].topic == "pipeline.dyndolod.completed"
    ]
    assert len(completed_calls) == 1
    payload = completed_calls[0][0][0].payload
    assert payload["success"] is False
    assert payload["rolled_back"] is False


@pytest.mark.asyncio
async def test_unexpected_error_no_intenta_cerrar_journal_sin_cobertura(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Sin cobertura completa ni siquiera intenta el cierre best-effort del journal."""
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(side_effect=RuntimeError("Unexpected crash"))

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert result["rolled_back"] is False
    mock_journal.mark_transaction_rolled_back.assert_not_called()

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
async def test_validation_failure_deja_tx_pendiente(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """La validación falla después del runner: staging puede quedar parcial."""
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
    assert result["rolled_back"] is False
    mock_journal.mark_transaction_rolled_back.assert_not_called()


@pytest.mark.asyncio
async def test_exit_cero_sin_output_path_no_se_reporta_como_exito(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """U-06: exit 0 pero SIN directorio de salida localizable no es éxito.

    ``_find_dyndolod_output`` devuelve ``None`` cuando no encuentra el output en
    ninguna de sus ubicaciones candidatas (solo loguea un warning). El guard de
    validación estaba encadenado a ``and result.dyndolod_result.output_path``, así
    que con ``None`` la validación se SALTEABA entera y el ritual commiteaba el
    journal reportando éxito — falso verde por exit-code (U-06) sobre un estado
    donde no se sabe si DynDOLOD escribió algo.
    """
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_success_result(dyndolod_output=None))
    mock_runner.validate_dyndolod_output = AsyncMock(return_value=True)

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="High", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert result["rolled_back"] is False
    mock_journal.commit_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_not_called()
    # Sin path no hay nada que validar: el fallo es anterior a la validación.
    mock_runner.validate_dyndolod_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_cero_sin_dyndolod_result_no_se_reporta_como_exito(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """U-06 (review Qodo): la otra mitad del guard encadenado.

    Hoy ``run_full_pipeline`` computa ``success`` exigiendo ``dyndolod_result is
    not None``, así que este estado NO es alcanzable — pero el tipo lo permite, y
    el criterio del repo (U-11) es no reportar éxito sobre un estado indeterminado
    solo porque "no debería pasar". Sin este guard, un ``dyndolod_result`` en
    ``None`` volvería a saltear la validación entera y commitear como éxito.
    """
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(
        return_value=DynDOLODPipelineResult(
            success=True,
            texgen_result=None,
            dyndolod_result=None,
            errors=[],
        )
    )
    mock_runner.validate_dyndolod_output = AsyncMock(return_value=True)

    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config

    service._runner = mock_runner

    result = await service.execute(preset="High", run_texgen=False, create_snapshot=False)

    assert result["success"] is False
    assert result["rolled_back"] is False
    mock_journal.commit_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_not_called()
    mock_runner.validate_dyndolod_output.assert_not_awaited()


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
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """El output vuelve, pero staging no cubierto impide afirmar rollback total."""
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
    assert result["rolled_back"] is False
    mock_journal.mark_transaction_rolled_back.assert_not_called()
    # Directorio original restaurado byte-a-byte; el parcial se descartó.
    assert (output_dir / "sentinel.esp").read_text(encoding="utf-8") == "ORIGINAL"
    assert (output_dir / "textures" / "a.dds").read_bytes() == b"\xde\xad"
    assert not (output_dir / "partial.esp").exists()
    assert not list(mods.glob("DynDOLOD Output.rollback-*"))  # sin backups huérfanos
    # El .esp ya NO se snapshotea vía FileSnapshotManager (target_files=[]).
    mock_snapshot_manager.create_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_staging_parcial_deja_tx_pendiente_aunque_output_se_restaure(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """No se declara rollback total sobre staging que el move-aside no protege."""
    mods = tmp_path / "mods"
    output_dir = mods / "DynDOLOD Output"
    output_dir.mkdir(parents=True)
    (output_dir / "original.esp").write_text("ORIGINAL", encoding="utf-8")
    mo2 = tmp_path / "MO2"
    mo2.mkdir()
    staging = mo2 / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    tools = tmp_path / "DynDOLOD"
    tools.mkdir()

    runner = _mock_runner_with_output(mods)
    runner._config.mo2_path = mo2
    runner._config.dyndolod_exe = tools / "DynDOLODx64.exe"

    async def _pipeline_parcial(**_kwargs: object) -> DynDOLODPipelineResult:
        output_dir.mkdir(parents=True)
        (output_dir / "partial.esp").write_text("PARTIAL", encoding="utf-8")
        staging.mkdir()
        (staging / "partial.txt").write_text("STAGING", encoding="utf-8")
        raise DynDOLODExecutionError("fallo tras escribir staging")

    runner.run_full_pipeline = AsyncMock(side_effect=_pipeline_parcial)
    service._runner = runner

    result = await service.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is False
    assert result["rolled_back"] is False
    assert (output_dir / "original.esp").read_text(encoding="utf-8") == "ORIGINAL"
    assert not (output_dir / "partial.esp").exists()
    assert (staging / "partial.txt").read_text(encoding="utf-8") == "STAGING"
    mock_journal.mark_transaction_rolled_back.assert_not_called()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_falla", [False, True], ids=["restore_confirmado", "restore_fallido"])
async def test_cancelacion_durante_enter_refleja_undo_en_journal(
    service: DynDOLODPipelineService,
    mock_lock_manager: AsyncMock,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
    restore_falla: bool,
) -> None:
    """DynDOLOD registra el rollback antes de entrar y respeta su outcome real."""
    import threading
    from collections.abc import Callable

    from sky_claw.local.tools import _dir_rollback
    from sky_claw.local.tools._dir_rollback import DirectoryRollback

    mods = tmp_path / "mods"
    output_dir = mods / "DynDOLOD Output"
    output_dir.mkdir(parents=True)
    (output_dir / "sentinel.esp").write_text("ORIGINAL", encoding="utf-8")
    runner = _mock_runner_with_output(mods)
    runner.run_full_pipeline = AsyncMock()
    service._runner = runner

    rename_real = pathlib.Path.rename
    move_empezo = threading.Event()
    permitir_move = threading.Event()
    capturados: list[DirectoryRollback] = []

    class _RollbackCapturado(DirectoryRollback):
        def __init__(
            self,
            target_dir: pathlib.Path,
            *,
            enabled: bool = True,
            should_rollback: Callable[[], bool] | None = None,
        ) -> None:
            super().__init__(target_dir, enabled=enabled, should_rollback=should_rollback)
            capturados.append(self)

    def _rename_controlado(self: pathlib.Path, dst: pathlib.Path) -> pathlib.Path:
        if self == output_dir:
            move_empezo.set()
            assert permitir_move.wait(5.0), "el test no liberó el move-aside"
            return rename_real(self, dst)
        if restore_falla and self.name.startswith(f"{output_dir.name}.rollback-") and dst == output_dir:
            raise PermissionError("restore bloqueado persistentemente")
        return rename_real(self, dst)

    with (
        patch("sky_claw.local.tools.dyndolod_service.DirectoryRollback", _RollbackCapturado),
        patch.object(pathlib.Path, "rename", _rename_controlado),
        patch.object(_dir_rollback, "_FS_BACKOFF_SECONDS", 0.0),
    ):
        task = asyncio.create_task(service.execute(preset="Medium", run_texgen=False, create_snapshot=True))
        assert await asyncio.to_thread(move_empezo.wait, 5.0)
        task.cancel()
        permitir_move.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert len(capturados) == 1
    assert capturados[0].rollback_completed is (not restore_falla)
    runner.run_full_pipeline.assert_not_awaited()
    backups = list(mods.glob("DynDOLOD Output.rollback-*"))
    if restore_falla:
        assert not output_dir.exists()
        assert len(backups) == 1
        mock_journal.mark_transaction_rolled_back.assert_not_called()
    else:
        assert (output_dir / "sentinel.esp").read_text(encoding="utf-8") == "ORIGINAL"
        assert not backups
        mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(42)
    assert not _dir_rollback._background_tasks
    mock_lock_manager.release_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelacion_post_commit_no_remarca_journal_ni_revierte_output(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Una cancelación del flight report ocurre después del punto de no retorno."""
    mods = tmp_path / "mods"
    output_dir = mods / "DynDOLOD Output"
    output_dir.mkdir(parents=True)
    (output_dir / "old.esp").write_text("OLD", encoding="utf-8")
    runner = _mock_runner_with_output(mods)

    async def _pipeline_ok(**_kwargs: object) -> DynDOLODPipelineResult:
        output_dir.mkdir(parents=True)
        (output_dir / "new.esp").write_text("NEW", encoding="utf-8")
        return _make_success_result(run_texgen=False)

    runner.run_full_pipeline = AsyncMock(side_effect=_pipeline_ok)
    runner.validate_dyndolod_output = AsyncMock(return_value=True)
    service._runner = runner

    with (
        patch.object(service, "_emit_flight_report", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await service.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    mock_journal.commit_transaction.assert_awaited_once_with(42)
    mock_journal.mark_transaction_rolled_back.assert_not_called()
    assert (output_dir / "new.esp").read_text(encoding="utf-8") == "NEW"
    assert not (output_dir / "old.esp").exists()
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

    async def test_fija_cwd_explicito_para_el_subproceso(self) -> None:
        from sky_claw.local.tools import dyndolod_runner as ddl

        proc = MagicMock()
        proc.stdout = _EOFStream()
        proc.stderr = _EOFStream()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)

        config = MagicMock(timeout_seconds=3600, heartbeat_interval=60)
        runner = ddl.DynDOLODRunner(config)
        cwd = pathlib.Path.cwd()

        with patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)) as create_process:
            await runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "DynDOLOD")

        assert create_process.call_args.kwargs["cwd"] == cwd

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

    async def test_salida_normal_con_nieto_cierra_el_job(self) -> None:
        """U-07: en la salida NORMAL con un nieto que heredó el pipe (drain colgado),
        el proceso se mete en un Job Object kill-on-close y se CIERRA (``close_job``)
        tras drenar. En Windows eso aniquila al nieto huérfano que ``kill_and_reap``
        ya no alcanza (el padre salió y el nieto se reparentó). Espiamos ambos helpers;
        contra el código previo (sin job) el patch de estos símbolos ni existía."""
        from sky_claw.local.tools import dyndolod_runner as ddl

        proc = MagicMock()
        proc.stdout = _EOFStream()
        proc.stderr = _HangingStream()  # nieto que sobrevive al padre
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)  # el proceso "salió" con éxito

        config = MagicMock()
        config.timeout_seconds = 3600
        config.heartbeat_interval = 60
        runner = ddl.DynDOLODRunner(config)

        assign_spy = MagicMock(return_value=4242)
        close_spy = MagicMock()
        with (
            patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(ddl, "_DRAIN_GRACE_SECONDS", 0.1),
            patch.object(ddl, "assign_kill_on_close_job", assign_spy),
            patch.object(ddl, "close_job", close_spy),
        ):
            await asyncio.wait_for(
                runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "DynDOLOD"),
                timeout=5.0,
            )

        assign_spy.assert_called_once_with(proc.pid)
        close_spy.assert_called_once_with(4242)


# =============================================================================
# Cancelación externa — limpieza de proceso y drains
# =============================================================================


class _DrainUntilReleased:
    """Stream que permite observar si el runner cancela el drain."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()

    async def read(self, _n: int) -> bytes:
        try:
            await self.release.wait()
            return b""
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.finished.set()


class TestExecuteProcessCancellation:
    async def test_cancelacion_externa_mata_proceso_y_cancela_drains(self) -> None:
        """Un shutdown no deja DynDOLOD ni sus readers vivos en segundo plano."""
        from sky_claw.local.tools import dyndolod_runner as ddl

        wait_started = asyncio.Event()
        process_reaped = asyncio.Event()
        stdout = _DrainUntilReleased()
        stderr = _DrainUntilReleased()
        proc = MagicMock()
        proc.pid = None
        proc.returncode = None
        proc.stdout = stdout
        proc.stderr = stderr

        async def _wait() -> int:
            wait_started.set()
            await process_reaped.wait()
            proc.returncode = -9
            return -9

        def _kill() -> None:
            process_reaped.set()

        proc.wait = _wait
        proc.kill = MagicMock(side_effect=_kill)
        config = MagicMock(timeout_seconds=3600, heartbeat_interval=60)
        runner = ddl.DynDOLODRunner(config)

        with patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "DynDOLOD"))
            try:
                await wait_started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                proc.kill.assert_called_once()
                assert stdout.cancelled.is_set()
                assert stderr.cancelled.is_set()
            finally:
                stdout.release.set()
                stderr.release.set()
                await stdout.finished.wait()
                await stderr.finished.wait()


# =============================================================================
# T-16c·3: gate de preflight en DynDOLOD (antes de un run de 30+ min / GBs)
# =============================================================================


class _FakePreflight:
    """Preflight inyectable: ``run()`` devuelve un reporte fijo."""

    def __init__(self, report: PreflightReport) -> None:
        self._report = report
        self.ran = False

    async def run(self) -> PreflightReport:
        self.ran = True
        return self._report


def _perm_report(status: PreflightStatus, summary: str) -> PreflightReport:
    """Reporte con un solo check de permisos (el failure mode típico de DynDOLOD:
    el dir de salida read-only) en el estado pedido."""
    return PreflightReport(
        status=status,
        checks=(PreflightCheck(name="write_permissions", status=status, summary=summary, details=()),),
    )


def _svc_with_preflight(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    preflight: object,
) -> DynDOLODPipelineService:
    return DynDOLODPipelineService(
        lock_manager=mock_lock_manager,
        snapshot_manager=mock_snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=mock_event_bus,
        preflight=preflight,  # type: ignore[arg-type]  # fake duck-typed en tests
    )


def _wire_runner(service: DynDOLODPipelineService, tmp_path: pathlib.Path) -> AsyncMock:
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_success_result())
    mock_runner.validate_dyndolod_output = AsyncMock(return_value=True)
    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config
    service._runner = mock_runner
    return mock_runner


@pytest.mark.asyncio
async def test_preflight_red_blocks_dyndolod_without_running(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Un preflight ROJO (p.ej. el dir de salida sin permisos) frena DynDOLOD ANTES
    de tocar nada: no adquiere lock, no abre transacción, no corre el pipeline."""
    red = _perm_report(PreflightStatus.RED, "Data/output sin permisos de escritura.")
    svc = _svc_with_preflight(
        mock_lock_manager, mock_snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus, _FakePreflight(red)
    )
    runner = _wire_runner(svc, tmp_path)

    result = await svc.execute(preset="High", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert result["reason"] == "PreflightBlocked"
    assert result["preflight"]["status"] == "red"
    runner.run_full_pipeline.assert_not_awaited()
    mock_journal.begin_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_yellow_no_bloquea_y_surface(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Un preflight AMARILLO no bloquea el run, pero se adjunta al result para el panel."""
    yellow = _perm_report(PreflightStatus.YELLOW, "Overwrite con residuos.")
    svc = _svc_with_preflight(
        mock_lock_manager,
        mock_snapshot_manager,
        mock_journal,
        mock_path_resolver,
        mock_event_bus,
        _FakePreflight(yellow),
    )
    _wire_runner(svc, tmp_path)

    result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is True
    assert result["preflight"]["status"] == "yellow"
    mock_journal.commit_transaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_preflight_green_no_ensucia_el_result(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Un preflight VERDE no agrega la clave ``preflight`` (comportamiento actual intacto)."""
    green = _perm_report(PreflightStatus.GREEN, "Escritura verificada.")
    svc = _svc_with_preflight(
        mock_lock_manager,
        mock_snapshot_manager,
        mock_journal,
        mock_path_resolver,
        mock_event_bus,
        _FakePreflight(green),
    )
    _wire_runner(svc, tmp_path)

    result = await svc.execute(preset="Low", run_texgen=False, create_snapshot=False)

    assert result["success"] is True
    assert "preflight" not in result


@pytest.mark.asyncio
async def test_ensure_preflight_construye_sensores_con_mo2_resoluble(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Con game/MO2 resolubles, ``_ensure_preflight`` arma un PreflightService real
    (no None) — el gate deja de ser un no-op."""
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    mo2 = tmp_path / "MO2"
    (mo2 / "mods").mkdir(parents=True)
    (mo2 / "overwrite").mkdir()

    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game)
    resolver.get_mo2_path = MagicMock(return_value=mo2)
    resolver.get_mo2_mods_path = MagicMock(return_value=mo2 / "mods")
    resolver.get_skyrim_path_raw = MagicMock(return_value=game)
    resolver.get_mo2_path_raw = MagicMock(return_value=mo2)
    resolver.get_active_profile = MagicMock(return_value="Default")

    svc = DynDOLODPipelineService(
        lock_manager=mock_lock_manager,
        snapshot_manager=mock_snapshot_manager,
        journal=mock_journal,
        path_resolver=resolver,
        event_bus=mock_event_bus,
    )

    preflight = svc._ensure_preflight()
    assert preflight is not None  # sensores cableados, no un no-op


def _resolver_para_permisos(tmp_path: pathlib.Path) -> MagicMock:
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    mo2 = tmp_path / "MO2"
    (mo2 / "mods").mkdir(parents=True)
    exe = tmp_path / "DynDOLOD" / "DynDOLODx64.exe"
    exe.parent.mkdir(parents=True)
    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game)
    resolver.get_mo2_path = MagicMock(return_value=mo2)
    resolver.get_mo2_mods_path = MagicMock(return_value=mo2 / "mods")
    resolver.get_dyndolod_exe = MagicMock(return_value=exe)
    resolver.get_skyrim_path_raw = MagicMock(return_value=game)
    resolver.get_mo2_path_raw = MagicMock(return_value=mo2)
    resolver.get_active_profile = MagicMock(return_value="Default")
    return resolver


def _svc(resolver: MagicMock, mock_lock_manager, mock_snapshot_manager, mock_journal, mock_event_bus):
    return DynDOLODPipelineService(
        lock_manager=mock_lock_manager,
        snapshot_manager=mock_snapshot_manager,
        journal=mock_journal,
        path_resolver=resolver,
        event_bus=mock_event_bus,
    )


def test_permission_targets_incluye_staging_y_empaquetado(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """El sensor de permisos sondea el empaquetado (mods/*) Y el staging crudo
    (raíz MO2 + dir del exe) donde DynDOLOD escribe primero (review #311 F2)."""
    resolver = _resolver_para_permisos(tmp_path)
    svc = _svc(resolver, mock_lock_manager, mock_snapshot_manager, mock_journal, mock_event_bus)
    mo2 = tmp_path / "MO2"
    exe_dir = tmp_path / "DynDOLOD"

    targets = svc._permission_targets()

    # Empaquetado bajo mods/
    assert mo2 / "mods" in targets
    assert mo2 / "mods" / "DynDOLOD Output" in targets
    assert mo2 / "mods" / "TexGen Output" in targets
    # Staging crudo bajo la raíz MO2 y el dir del exe
    assert mo2 / "DynDOLOD_Output" in targets
    assert mo2 / "TexGen_Output" in targets
    assert exe_dir / "DynDOLOD_Output" in targets
    assert exe_dir / "TexGen_Output" in targets


def test_permission_targets_incluye_output_inexistente_freshness(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Freshness (review #311 F1): la ruta del output empaquetado está en los
    targets aunque el dir NO exista al construir — así un output creado read-only
    en un run posterior se sondea (el checker se salta los inexistentes)."""
    resolver = _resolver_para_permisos(tmp_path)
    svc = _svc(resolver, mock_lock_manager, mock_snapshot_manager, mock_journal, mock_event_bus)
    output = tmp_path / "MO2" / "mods" / "DynDOLOD Output"
    assert not output.exists()  # todavía no existe (primer run / limpiado)

    assert output in svc._permission_targets()  # igual está en la lista de sondeo


# =============================================================================
# T-26/T-28: caja negra de vuelo en DynDOLOD (4.º productor tras LOOT/xEdit/Synthesis)
# =============================================================================


@pytest.fixture
async def real_journal(tmp_path: pathlib.Path):  # noqa: ANN201
    from sky_claw.app.db.journal import OperationJournal

    j = OperationJournal(tmp_path / "dyndolod_journal.db")
    await j.open()
    yield j  # type: ignore[misc]
    await j.close()


async def _ops_ultima_tx(journal):  # noqa: ANN001, ANN202
    (ultima,) = await journal.list_recent_transactions(limit=1)
    return await journal.get_operations_by_transaction(ultima.transaction_id)


async def _manifiesto_ultima_tx(journal):  # noqa: ANN001, ANN202
    from sky_claw.app.orchestrator.preview.action_manifest import ActionManifest

    metas = [
        e.metadata
        for e in await _ops_ultima_tx(journal)
        if e.metadata and e.metadata.get("ritual_id") and e.metadata.get("kind") != "flight_report"
    ]
    assert len(metas) == 1
    return ActionManifest.model_validate(metas[0])


async def _informe_ultima_tx(journal):  # noqa: ANN001, ANN202
    from sky_claw.app.orchestrator.preview.flight_report import FlightReport

    return [
        FlightReport.model_validate(e.metadata)
        for e in await _ops_ultima_tx(journal)
        if e.metadata and e.metadata.get("kind") == "flight_report"
    ]


def _svc_real_journal(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    real_journal: object,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
) -> DynDOLODPipelineService:
    return DynDOLODPipelineService(
        lock_manager=mock_lock_manager,
        snapshot_manager=mock_snapshot_manager,
        journal=real_journal,  # type: ignore[arg-type]
        path_resolver=mock_path_resolver,
        event_bus=mock_event_bus,
    )


def _wire_runner_ok(service: DynDOLODPipelineService, tmp_path: pathlib.Path) -> AsyncMock:
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_success_result())
    mock_runner.validate_dyndolod_output = AsyncMock(return_value=True)
    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    mock_config.mo2_path = tmp_path / "MO2"
    mock_config.dyndolod_exe = tmp_path / "DynDOLOD" / "DynDOLODx64.exe"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config
    service._runner = mock_runner
    return mock_runner


@pytest.mark.asyncio
async def test_dyndolod_persiste_manifiesto_y_informe(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Un run exitoso persiste un ActionManifest (tool=DynDOLOD, con el mod de
    salida en files_touched) ANTES de mutar y un FlightReport DESPUÉS (T-26/T-28)."""
    svc = _svc_real_journal(mock_lock_manager, mock_snapshot_manager, real_journal, mock_path_resolver, mock_event_bus)
    _wire_runner_ok(svc, tmp_path)

    result = await svc.execute(preset="High", run_texgen=True, create_snapshot=False)

    assert result["success"] is True
    manifest = await _manifiesto_ultima_tx(real_journal)
    assert manifest.tool == "DynDOLOD"
    # Empaquetado bajo mods/
    assert str(tmp_path / "mods" / "DynDOLOD Output") in manifest.files_touched
    # Staging crudo bajo la raíz MO2 y el dir del exe (review #312)
    assert str(tmp_path / "MO2" / "DynDOLOD_Output") in manifest.files_touched
    assert str(tmp_path / "DynDOLOD" / "DynDOLOD_Output") in manifest.files_touched
    informes = await _informe_ultima_tx(real_journal)
    assert len(informes) == 1
    assert informes[0].transaction_status == "committed"


@pytest.mark.asyncio
async def test_dyndolod_sin_manifiesto_no_ejecuta(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Si la persistencia del manifiesto falla, el pipeline NO corre (fail-closed):
    reason=ActionManifestFailed y la TX se marca rolled_back."""
    svc = _svc_real_journal(mock_lock_manager, mock_snapshot_manager, real_journal, mock_path_resolver, mock_event_bus)
    runner = _wire_runner_ok(svc, tmp_path)

    with patch.object(real_journal, "persist_action_manifest", AsyncMock(side_effect=RuntimeError("boom"))):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    runner.run_full_pipeline.assert_not_awaited()  # fail-closed: no se mutó nada
    assert result["success"] is False
    assert result["reason"] == "ActionManifestFailed"


@pytest.mark.asyncio
async def test_dyndolod_informe_falla_no_rompe_run(
    mock_lock_manager: AsyncMock,
    mock_snapshot_manager: AsyncMock,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """El FlightReport es best-effort: un fallo al persistirlo NO rompe un run ya
    exitoso (misma disciplina que LOOT/xEdit)."""
    svc = _svc_real_journal(mock_lock_manager, mock_snapshot_manager, real_journal, mock_path_resolver, mock_event_bus)
    _wire_runner_ok(svc, tmp_path)

    with patch.object(real_journal, "persist_flight_report", AsyncMock(side_effect=OSError("disk full"))):
        result = await svc.execute(preset="Low", run_texgen=False, create_snapshot=False)

    assert result["success"] is True  # el informe roto no tumba el run
    manifest = await _manifiesto_ultima_tx(real_journal)
    assert manifest.tool == "DynDOLOD"


# =============================================================================
# U-06 — validate_dyndolod_output sobre el runner REAL (no mockeado)
#
# Todos los tests de servicio de arriba mockean ``validate_dyndolod_output``, así
# que su cuerpo real nunca se ejercía. Estos lo cubren directamente.
# =============================================================================


def _runner_para_validacion() -> DynDOLODRunner:
    """Runner con config falsa: ``validate_dyndolod_output`` solo mira el path."""
    return DynDOLODRunner(MagicMock())


@pytest.mark.asyncio
async def test_validate_output_falla_si_falta_dyndolod_esp(tmp_path: pathlib.Path) -> None:
    """U-06: un ``.esp`` cualquiera NO alcanza — hay que exigir ``DynDOLOD.esp``.

    El docstring de la función ya prometía "Contiene DynDOLOD.esp", pero su
    ausencia solo logueaba un warning y seguía hasta ``return True``: un run que
    dejó ESPs sueltos (o el output de otra tool) se validaba como bueno y el
    pipeline construía encima de LODs inexistentes.
    """
    output = tmp_path / "DynDOLOD_Output"
    output.mkdir()
    (output / "Otro.esp").touch()  # hay un .esp, pero NO es el que importa

    assert await _runner_para_validacion().validate_dyndolod_output(output) is False


@pytest.mark.asyncio
async def test_validate_output_acepta_salida_con_dyndolod_esp(tmp_path: pathlib.Path) -> None:
    """Contracara del anterior: con ``DynDOLOD.esp`` presente sí valida (no se
    endureció de más)."""
    output = tmp_path / "DynDOLOD_Output"
    output.mkdir()
    (output / "DynDOLOD.esp").touch()

    assert await _runner_para_validacion().validate_dyndolod_output(output) is True


@pytest.mark.asyncio
async def test_validate_output_rechaza_un_directorio_llamado_dyndolod_esp(tmp_path: pathlib.Path) -> None:
    """Review CodeRabbit: ``exists()`` acepta un DIRECTORIO llamado ``DynDOLOD.esp``.

    Ese output se validaría y empaquetaría sin contener el plugin, que es el mismo
    falso verde que U-06 ataca. Hay que exigir un archivo regular (``is_file()``).
    """
    output = tmp_path / "DynDOLOD_Output"
    output.mkdir()
    (output / "Otro.esp").touch()  # pasa la guarda de "hay algún .esp"
    (output / "DynDOLOD.esp").mkdir()  # ...pero el "plugin" es un directorio

    assert await _runner_para_validacion().validate_dyndolod_output(output) is False


@pytest.mark.asyncio
async def test_validate_output_rechaza_dir_inexistente_o_vacio(tmp_path: pathlib.Path) -> None:
    """Guardas previas intactas tras el endurecimiento (regresión)."""
    runner = _runner_para_validacion()

    assert await runner.validate_dyndolod_output(tmp_path / "no_existe") is False

    vacio = tmp_path / "vacio"
    vacio.mkdir()
    assert await runner.validate_dyndolod_output(vacio) is False

    sin_esp = tmp_path / "sin_esp"
    sin_esp.mkdir()
    (sin_esp / "textura.dds").touch()
    assert await runner.validate_dyndolod_output(sin_esp) is False
