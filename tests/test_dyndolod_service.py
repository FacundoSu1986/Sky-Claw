"""Tests for DynDOLODPipelineService.

Sprint 2, Fase 3: Validates transactional pipeline execution, event
publication, journal lifecycle, and rollback on unexpected errors.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import pathlib
import time
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sky_claw.local.tools.dyndolod_runner
from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    LockInfo,
)
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
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


@pytest.mark.asyncio
async def test_cobertura_de_staging_no_confirmada_no_es_critica(
    service: DynDOLODPipelineService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """La cobertura cruda pendiente es una limitación conocida, no un fallo de rollback."""
    rollback = MagicMock(rollback_completed=True)

    with caplog.at_level(logging.DEBUG, logger="SkyClaw.DynDOLODPipelineService"):
        rolled_back = await service._cerrar_tx_tras_rollback(
            42,
            [rollback],
            journal_committed=False,
            mutation_started=True,
            mutation_coverage_complete=False,
            contexto="fallo del pipeline",
        )

    assert rolled_back is False
    assert caplog.records[-1].levelno == logging.WARNING
    assert "staging" in caplog.records[-1].message
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    service._journal.mark_transaction_rolled_back.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_cancelacion_durante_commit_sella_todos_los_outputs_antes_de_liberar_lock(
    service: DynDOLODPipelineService,
    mock_lock_manager: AsyncMock,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """El commit de dos outputs es una sola unidad terminal pese a cancelaciones repetidas."""
    import threading

    from sky_claw.local.tools import _dir_rollback

    mods = tmp_path / "mods"
    dyndolod_dir = mods / "DynDOLOD Output"
    texgen_dir = mods / "TexGen Output"
    for output_dir in (dyndolod_dir, texgen_dir):
        output_dir.mkdir(parents=True)
        (output_dir / "old.txt").write_text("OLD", encoding="utf-8")

    runner = _mock_runner_with_output(mods)

    async def _pipeline_ok(**_kwargs: object) -> DynDOLODPipelineResult:
        for output_dir in (dyndolod_dir, texgen_dir):
            output_dir.mkdir(parents=True)
            (output_dir / "new.txt").write_text("NEW", encoding="utf-8")
        return _make_success_result(run_texgen=True)

    runner.run_full_pipeline = AsyncMock(side_effect=_pipeline_ok)
    runner.validate_dyndolod_output = AsyncMock(return_value=True)
    service._runner = runner

    rmtree_real = _dir_rollback.rmtree_link_aware
    primer_discard_empezo = threading.Event()
    permitir_primer_discard = threading.Event()
    orden: list[str] = []

    def _rmtree_controlado(ruta: pathlib.Path, *, limpiar_readonly: bool = False) -> None:
        if ruta.name.startswith("DynDOLOD Output.rollback-"):
            orden.append("dyndolod_discard_empezo")
            primer_discard_empezo.set()
            assert permitir_primer_discard.wait(5.0), "el test no liberó el primer discard"
            rmtree_real(ruta, limpiar_readonly=limpiar_readonly)
            orden.append("dyndolod_discard_termino")
            return
        if ruta.name.startswith("TexGen Output.rollback-"):
            orden.append("texgen_discard_empezo")
            rmtree_real(ruta, limpiar_readonly=limpiar_readonly)
            orden.append("texgen_discard_termino")
            return
        rmtree_real(ruta, limpiar_readonly=limpiar_readonly)

    async def _release_lock(*_args: object, **_kwargs: object) -> bool:
        orden.append("lock_liberado")
        return True

    mock_lock_manager.release_lock.side_effect = _release_lock

    with patch.object(_dir_rollback, "rmtree_link_aware", _rmtree_controlado):
        task = asyncio.create_task(service.execute(preset="High", run_texgen=True, create_snapshot=True))
        try:
            assert await asyncio.to_thread(primer_discard_empezo.wait, 5.0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            permitir_primer_discard.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert orden.index("dyndolod_discard_termino") < orden.index("texgen_discard_empezo")
    assert orden.index("texgen_discard_termino") < orden.index("lock_liberado")
    for output_dir in (dyndolod_dir, texgen_dir):
        assert (output_dir / "new.txt").read_text(encoding="utf-8") == "NEW"
        assert not (output_dir / "old.txt").exists()
    assert not list(mods.glob("*.rollback-*"))
    mock_journal.commit_transaction.assert_awaited_once_with(42)
    mock_journal.mark_transaction_rolled_back.assert_not_called()
    mock_lock_manager.release_lock.assert_awaited_once()
    assert not _dir_rollback._background_tasks


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
    assert result["assisted"] is True  # contrato único: la etapa es asistida también en preflight rojo
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
    """El sensor de permisos sondea el empaquetado (mods/*), el staging crudo bajo
    la raíz administrada única (game/Sky-Claw/DynDOLOD) donde la herramienta
    escribe con ``-o:``, y el dir del exe donde escribe su log e INI
    (review #311 F2, reescrito con la raíz única)."""
    resolver = _resolver_para_permisos(tmp_path)
    svc = _svc(resolver, mock_lock_manager, mock_snapshot_manager, mock_journal, mock_event_bus)
    mo2 = tmp_path / "MO2"
    exe_dir = tmp_path / "DynDOLOD"
    game = tmp_path / "Skyrim"
    root = game / "Sky-Claw" / "DynDOLOD"

    targets = svc._permission_targets()

    # Empaquetado bajo mods/
    assert mo2 / "mods" in targets
    assert mo2 / "mods" / "DynDOLOD Output" in targets
    assert mo2 / "mods" / "TexGen Output" in targets
    # Staging crudo bajo la raíz administrada única
    assert root in targets
    assert root / "DynDOLOD_Output" in targets
    assert root / "TexGen_Output" in targets
    # El primer ancestro EXISTENTE se sondea para poder CREAR la raíz en el primer
    # run: `root.parent` (game/Sky-Claw) tampoco existe todavía, y el checker se
    # salta las rutas inexistentes.
    assert game in targets
    # Dir del exe: el log que `_leer_log` lee para dar el veredicto vive ahí.
    assert exe_dir in targets
    assert exe_dir / "Logs" in targets
    # La adivinanza de raíces de STAGING ajenas (mo2/exe) murió con -o:
    assert mo2 / "DynDOLOD_Output" not in targets
    assert mo2 / "TexGen_Output" not in targets
    assert exe_dir / "DynDOLOD_Output" not in targets
    assert exe_dir / "TexGen_Output" not in targets


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
    mock_config.output_root = tmp_path / "DynDOLOD"
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
    # Staging crudo bajo la raíz administrada única (subcarpetas + la raíz)
    assert str(tmp_path / "DynDOLOD" / "DynDOLOD_Output") in manifest.files_touched
    assert str(tmp_path / "DynDOLOD" / "TexGen_Output") in manifest.files_touched
    assert str(tmp_path / "DynDOLOD") in manifest.files_touched
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


# =============================================================================
# U-06 — post-check por LOG (los binarios son GUI: no escriben a stdout)
#
# Los casos de arriba mockean el proceso; acá se ejercita el runner REAL con
# logs escritos en tmp_path: un exit code 0 con línea de error en el log (o sin
# artefacto en el -o:) NO es éxito — el falso verde que U-06 persigue.
# =============================================================================


class _EjecucionFalsa:
    """Fake de ``_execute_process``: resultado fijo y captura del argv.

    ``al_ejecutar`` corre DENTRO de la llamada, que es donde la herramienta real
    escribe su salida. Los tests que crean el staging antes del ``with`` modelan
    una corrida ANTERIOR, no ésta: la distinción es justamente lo que el gate de
    frescura decide.
    """

    def __init__(self, return_code: int = 0, al_ejecutar: Callable[[], None] | None = None) -> None:
        """Fija el código de salida y, opcionalmente, qué escribe la corrida."""
        self.return_code = return_code
        self.al_ejecutar = al_ejecutar
        self.argvs: list[list[str]] = []

    async def __call__(self, *args: object, **kwargs: object) -> tuple[str, str, int, float]:
        self.argvs.append(list(kwargs["args"]))
        if self.al_ejecutar is not None:
            self.al_ejecutar()
        return "", "", self.return_code, 1.0


def _escribir_salida(directorio: pathlib.Path, nombre: str, contenido: bytes = b"\x00") -> None:
    """Escribe un archivo en el staging, como hace la herramienta DURANTE la corrida.

    Los tests lo pasan por ``al_ejecutar`` en vez de crear el staging antes del
    ``with``: la diferencia entre "hay un artefacto" y "esta corrida produjo un
    artefacto" es justo lo que decide el gate de frescura, así que un test que
    lo pre-crea estaría afirmando el caso equivocado.
    """
    directorio.mkdir(parents=True, exist_ok=True)
    (directorio / nombre).write_bytes(contenido)


def _runner_texgen(tmp_path: pathlib.Path) -> tuple[DynDOLODConfig, DynDOLODRunner]:
    """Runner real con config mínima: game + exes existen, Logs/ controlable."""
    game = tmp_path / "game"
    game.mkdir()
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir()
    dyndolod_exe = exe_dir / "DynDOLODx64.exe"
    dyndolod_exe.touch()
    texgen_exe = exe_dir / "TexGenx64.exe"
    texgen_exe.touch()
    config = DynDOLODConfig(
        game_path=game,
        mo2_path=tmp_path / "MO2",
        mo2_mods_path=tmp_path / "MO2" / "mods",
        dyndolod_exe=dyndolod_exe,
        texgen_exe=texgen_exe,
    )
    return config, DynDOLODRunner(config)


def _escribir_log(tmp_path: pathlib.Path, tool: str, contenido: str) -> pathlib.Path:
    log = tmp_path / "DynDOLOD" / "Logs" / f"{tool}_SSE_log.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(contenido, encoding="utf-8")
    return log


@pytest.mark.asyncio
async def test_exit_cero_con_error_en_log_no_es_exito(tmp_path: pathlib.Path) -> None:
    """U-06: exit 0 + línea de error en el log real → success=False (TexGen)."""
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_log(tmp_path, "TexGen", "[00:00:01] Error: object LOD generation failed\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "a.dds"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is False
    assert result.errors == ["object LOD generation failed"]
    assert fake.argvs[0][0] == "-sse"


@pytest.mark.asyncio
async def test_exit_cero_con_error_en_log_no_es_exito_dyndolod(tmp_path: pathlib.Path) -> None:
    """El mismo criterio para DynDOLOD: exit 0 + log en error → success=False."""
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:10] Fatal: exception while processing\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert result.errors == ["exception while processing"]


@pytest.mark.asyncio
async def test_exit_cero_sin_artefacto_no_es_exito(tmp_path: pathlib.Path) -> None:
    """U-06: exit 0 sin ``DynDOLOD.esp`` en el -o: → success=False."""
    _, runner = _runner_texgen(tmp_path)
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:05] LOD generation completed\n")

    fake = _EjecucionFalsa(return_code=0)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert result.output_path is not None  # el staging se resuelve aunque no exista


@pytest.mark.asyncio
async def test_exit_cero_con_artefacto_y_log_limpio_es_exito(tmp_path: pathlib.Path) -> None:
    """Contracara: exit 0 + DynDOLOD.esp + log sin errores → success=True."""
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:05] LOD generation completed\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="High")

    assert result.success is True
    assert result.output_path == staging
    assert result.warnings == []


@pytest.mark.asyncio
async def test_warning_en_log_no_tumba_el_exito(tmp_path: pathlib.Path) -> None:
    """Un warning (no error) con artefacto y exit 0 sigue siendo éxito."""
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_log(tmp_path, "TexGen", "[00:00:02] Warning: 2 LOD textures skipped\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "a.dds"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is True
    assert result.warnings == ["2 LOD textures skipped"]


@pytest.mark.asyncio
async def test_log_ausente_no_es_fallo_si_el_artefacto_existe(tmp_path: pathlib.Path) -> None:
    """Sin log (el proceso no lo flusheó) el gate es el artefacto: success=True."""
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(staging, "a.dds"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is True
    assert result.errors == []


@pytest.mark.asyncio
async def test_la_herramienta_puede_escribir_directo_en_la_raiz(tmp_path: pathlib.Path) -> None:
    """Interpretación B de -o: (escribe directo, sin subcarpeta): fallback acotado.

    El candidato ``root/<StagingName>`` no existe pero ``root`` sí tiene el
    artefacto: la resolución no puede perder la salida real del run.
    """
    config, runner = _runner_texgen(tmp_path)
    root = config.output_root
    assert root is not None

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(root, "DynDOLOD.esp"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True
    assert result.output_path == root


@pytest.mark.asyncio
async def test_texgen_no_acepta_el_staging_de_dyndolod_como_salida(tmp_path: pathlib.Path) -> None:
    """Regresión (review PR #440, Major): el staging previo de DynDOLOD en la
    raíz NO puede pasar por salida de TexGen.

    Si ``root/TexGen_Output`` no existe y la raíz contiene ``DynDOLOD_Output``
    de una corrida anterior, un fallback a ``root`` lo aceptaría como salida de
    TexGen y el empaquetado copiaría el staging de DynDOLOD dentro de
    "TexGen Output". TexGen se resuelve SOLO bajo ``root/TexGen_Output``:
    sin salida real, fail-closed.
    """
    config, runner = _runner_texgen(tmp_path)
    root = config.output_root
    assert root is not None
    stale = root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    stale.mkdir(parents=True)
    (stale / "DynDOLOD.esp").write_text("stale", encoding="utf-8")
    _escribir_log(tmp_path, "TexGen", "[00:00:02] TexGen completed\n")

    fake = _EjecucionFalsa(return_code=0)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.output_path == root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    assert result.success is False  # sin salida de TexGen real: fail-closed


# =============================================================================
# U-06 (parte 2) — FRESCURA: el artefacto tiene que ser de ESTA corrida
#
# El gate de artefacto por sí solo no distingue la salida de este run de la que
# dejó el anterior: el staging bajo `-o:` no se limpia ni entra al move-aside
# (`dyndolod_service`: mutation_coverage_complete = False). Con exit 0 —lo que
# devuelve una app GUI que el operador cerró a mitad— y sin log que lo desmienta,
# el artefacto viejo se reportaba como éxito y el empaquetado copiaba LODs
# obsoletos como resultado fresco. El falso verde de U-06 movido de "exit code"
# a "artefacto sin fecha".
# =============================================================================


@pytest.mark.asyncio
async def test_staging_previo_sin_escritura_no_es_exito_texgen(tmp_path: pathlib.Path) -> None:
    """Staging poblado por una corrida ANTERIOR + exit 0 + sin log → success=False.

    Es la secuencia exacta del hallazgo: corrida 1 deja ``TexGen_Output`` con
    texturas; en la corrida 2 el operador cierra la ventana a los dos minutos, la
    GUI sale con 0 y no flushea log. Nada en el disco cambió, así que no hay
    salida nueva que empaquetar.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    staging.mkdir(parents=True)
    (staging / "vieja.dds").write_bytes(b"\x00")

    fake = _EjecucionFalsa(return_code=0)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_texgen()

    assert result.success is False
    assert any("corrida" in e for e in result.errors), result.errors


@pytest.mark.asyncio
async def test_staging_previo_sin_escritura_no_es_exito_dyndolod(tmp_path: pathlib.Path) -> None:
    """El hermano: un ``DynDOLOD.esp`` viejo tampoco alcanza.

    El gate de `validate_dyndolod_output` exige el plugin como archivo, pero un
    plugin de la corrida anterior lo satisface igual — la frescura es el único
    criterio que los separa.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    staging.mkdir(parents=True)
    (staging / "DynDOLOD.esp").write_text("de la corrida anterior", encoding="utf-8")

    fake = _EjecucionFalsa(return_code=0)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert any("corrida" in e for e in result.errors), result.errors


@pytest.mark.asyncio
async def test_escritura_durante_la_corrida_si_es_exito(tmp_path: pathlib.Path) -> None:
    """Contracara: el staging viejo existe pero la corrida escribe encima → éxito.

    Sin esta mitad, el gate de frescura sería un "fallá siempre que haya staging
    previo", que rompería el caso normal de regenerar LODs sobre una salida
    anterior — el flujo habitual del operador.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    staging.mkdir(parents=True)
    (staging / "DynDOLOD.esp").write_text("vieja", encoding="utf-8")

    def _regenerar() -> None:
        """La herramienta reescribe el plugin, como en una corrida completa."""
        (staging / "DynDOLOD.esp").write_text("recien generada", encoding="utf-8")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_regenerar)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True
    assert result.errors == []


@pytest.mark.asyncio
async def test_staging_previo_recien_escrito_tampoco_pasa(tmp_path: pathlib.Path) -> None:
    """Regresión (review PR #441): el staging previo NO pasa ni recién escrito.

    La primera versión de este gate comparaba el mtime del artefacto contra el
    reloj de pared con 2 s de holgura, para no dar falso rojo por la granularidad
    de FAT32. Esa holgura era una ventana: un staging que la corrida anterior
    dejó uno o dos segundos antes pasaba por fresco y el falso verde volvía. Acá
    el staging se escribe inmediatamente antes de lanzar —sin envejecerlo— y
    tiene que fallar igual, porque la comparación es del árbol contra sí mismo
    (mtime contra mtime), no contra un reloj externo.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    _escribir_salida(staging, "DynDOLOD.esp", b"de la corrida anterior")

    fake = _EjecucionFalsa(return_code=0)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert any("corrida" in e for e in result.errors), result.errors


@pytest.mark.asyncio
async def test_elige_el_candidato_fresco_cuando_los_dos_coexisten(tmp_path: pathlib.Path) -> None:
    """Regresión (review PR #441): con ambos candidatos en disco gana el FRESCO.

    Resolver el path primero y validarlo después descartaba una corrida buena:
    ``_find_dyndolod_output`` prefiere ``root/DynDOLOD_Output`` si existe no
    vacío, así que un staging viejo ahí ganaba sobre el ``DynDOLOD.esp`` que la
    corrida acababa de escribir directo en la raíz (interpretación B de ``-o:``),
    y el veredicto salía rojo sobre una salida real. Falla cerrado, pero pierde
    30+ minutos de generación.
    """
    config, runner = _runner_texgen(tmp_path)
    root = config.output_root
    assert root is not None
    viejo = root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    _escribir_salida(viejo, "DynDOLOD.esp", b"corrida anterior")
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:05] LOD generation completed\n")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=lambda: _escribir_salida(root, "DynDOLOD.esp", b"fresco"))
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True
    assert result.output_path == root, "debe elegir la salida de ESTA corrida, no el staging viejo"
    assert result.errors == []


@pytest.mark.asyncio
async def test_corrida_abortada_sin_regenerar_el_esp_no_es_exito(tmp_path: pathlib.Path) -> None:
    """Regresión (review adversarial PR #441): escrituras parciales NO son veredicto.

    El escenario que sobrevivía a la primera versión del gate: el staging conserva
    la salida completa de la corrida 1; en la corrida 2 DynDOLOD alcanza a
    sobrescribir LODs dentro de ``meshes/`` y el operador cierra la GUI antes de
    que se regenere el plugin. Sale con código 0, el log no tiene línea de error,
    y el árbol "cambió" — así que firmar el ÁRBOL daba fresco y
    ``_validar_salida_dyndolod`` aceptaba el ``DynDOLOD.esp`` de la corrida
    anterior. Se empaquetaba un árbol a medio generar con un plugin ajeno.

    Por eso la firma es del artefacto que DECIDE el veredicto, no de sus vecinos.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    meshes = staging / "meshes" / "lod"
    meshes.mkdir(parents=True)
    (staging / "DynDOLOD.esp").write_text("de la corrida anterior", encoding="utf-8")
    (meshes / "tree.nif").write_bytes(b"\x00")
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:30] Building LOD meshes\n")

    def _abortar_a_mitad() -> None:
        """Escribe LODs y crea directorios, pero NUNCA regenera el plugin."""
        (meshes / "tree.nif").write_bytes(b"\x01\x02")
        (staging / "textures" / "lod").mkdir(parents=True)

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_abortar_a_mitad)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False
    assert any("corrida" in e for e in result.errors), result.errors


@pytest.mark.asyncio
async def test_reescritura_con_el_mismo_mtime_sigue_siendo_fresca(tmp_path: pathlib.Path) -> None:
    """Regresión (CI Windows py3.12): el mtime solo no ve todos los cambios.

    El reloj de archivos de Windows avanza de a ~15 ms, así que una reescritura
    que cae en el mismo tick que la firma previa deja el mtime IDÉNTICO y una
    firma de solo-mtime la declara "no regenerada" — falso rojo sobre una corrida
    buena. Acá se fuerza el mtime a ser el mismo antes y después: lo que separa
    las dos versiones es el tamaño, y la firma tiene que verlo.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    esp = staging / "DynDOLOD.esp"
    _escribir_salida(staging, "DynDOLOD.esp", b"corta")
    congelado = esp.stat().st_mtime
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:05] LOD generation completed\n")

    def _regenerar_en_el_mismo_tick() -> None:
        """Reescribe con contenido distinto pero el mismo mtime exacto."""
        esp.write_bytes(b"contenido regenerado, mas largo")
        os.utime(esp, (congelado, congelado))

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_regenerar_en_el_mismo_tick)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert esp.stat().st_mtime == congelado, "el test debe dejar el mtime intacto"
    assert result.success is True, "una reescritura real no puede depender del tick del reloj"
    assert result.errors == []


@pytest.mark.asyncio
async def test_firma_previa_ilegible_no_cuenta_como_artefacto_fresco(tmp_path: pathlib.Path) -> None:
    """Regresión (review adversarial PR #441): lo no verificable no es fresco.

    El centinela de la firma colapsaba dos cosas distintas: "no hay artefacto" y
    "no pude sondearlo". Si la firma PREVIA fallaba por un error transitorio
    —permisos, antivirus, volumen de red— sobre un ``.esp`` que sí existía, el
    post-check lo leía sin problema, lo veía distinto del centinela y lo daba por
    fresco: exit 0 + esp rancio + log sin errores → LODs obsoletos empaquetados
    como resultado nuevo. El falso verde reconstituido por un ``except``.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    _escribir_salida(staging, "DynDOLOD.esp", b"de la corrida anterior")
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:05] LOD generation completed\n")

    stat_real = pathlib.Path.stat
    ilegible = {"activo": True}

    def _stat_falible(self: pathlib.Path, *args: object, **kwargs: object) -> os.stat_result:
        """Falla al sondear el .esp SOLO durante la firma previa."""
        if ilegible["activo"] and self.name == "DynDOLOD.esp":
            raise PermissionError("el antivirus tiene el archivo tomado")
        return stat_real(self, *args, **kwargs)  # type: ignore[arg-type]

    def _corrida_que_no_escribe() -> None:
        """El proceso sale con 0 sin regenerar nada; el .esp vuelve a ser legible."""
        ilegible["activo"] = False

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_corrida_que_no_escribe)
    with patch.object(pathlib.Path, "stat", _stat_falible), patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is False, "un artefacto no verificable no puede reportarse como fresco"
    assert any("verificar" in e for e in result.errors), result.errors


@pytest.mark.asyncio
async def test_artefacto_previo_con_mtime_adelantado_no_descarta_la_corrida(tmp_path: pathlib.Path) -> None:
    """Regresión (review PR #441): la frescura es "cambió", no "creció".

    Comparar por ``>`` asumía que el mtime solo puede aumentar. Un snapshot
    restaurado, una copia con timestamps preservados (``copy2``, extracción de un
    archivo) o un reloj corrido dejan un ``DynDOLOD.esp`` con fecha adelantada;
    la corrida real lo reescribe con la fecha de hoy —MENOR— y el veredicto salía
    rojo sobre una salida buena. La comparación es por desigualdad.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    esp = staging / "DynDOLOD.esp"
    _escribir_salida(staging, "DynDOLOD.esp", b"restaurada de un snapshot")
    futuro = time.time() + 86400
    os.utime(esp, (futuro, futuro))
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:05] LOD generation completed\n")

    def _regenerar_con_fecha_de_hoy() -> None:
        """La corrida real reescribe el plugin: mtime de ahora, menor que el previo."""
        esp.write_text("regenerada", encoding="utf-8")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_regenerar_con_fecha_de_hoy)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True, "un mtime previo adelantado no puede descartar una corrida real"
    assert result.errors == []


@pytest.mark.asyncio
async def test_esp_regenerado_dentro_de_un_arbol_existente_es_exito(tmp_path: pathlib.Path) -> None:
    """Contracara: regenerar sobre una salida previa es el flujo NORMAL del operador.

    Sin esta mitad, el gate sería "fallá siempre que haya staging previo", que
    rompe el caso habitual de rehacer LODs encima de una corrida anterior.
    """
    config, runner = _runner_texgen(tmp_path)
    assert config.output_root is not None
    staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    meshes = staging / "meshes" / "lod"
    meshes.mkdir(parents=True)
    (staging / "DynDOLOD.esp").write_text("vieja", encoding="utf-8")
    (meshes / "tree.nif").write_bytes(b"\x00")
    _escribir_log(tmp_path, "DynDOLOD", "[00:00:45] LOD generation completed\n")

    def _regenerar() -> None:
        """Corrida completa: reescribe los LODs Y el plugin."""
        (meshes / "tree.nif").write_bytes(b"\x01\x02")
        (staging / "DynDOLOD.esp").write_text("regenerada", encoding="utf-8")

    fake = _EjecucionFalsa(return_code=0, al_ejecutar=_regenerar)
    with patch.object(runner, "_execute_process", fake):
        result = await runner.run_dyndolod(preset="Medium")

    assert result.success is True
    assert result.errors == []


# =============================================================================
# ANCLA de la familia del post-check
#
# La propiedad, enunciada como mecanismo y no como recordatorio de proceso:
#
#   Todo post-check de una etapa asistida decide sobre evidencia de ESTA corrida,
#   leída sin bloquear el event loop.
#
# Los dos hallazgos que este ancla ataja llegaron juntos y por la misma vía: el
# post-check nuevo se escribió al lado de `_package_output_as_mod` —que ya cruzaba
# a un hilo con un comentario explicando por qué— y no lo siguió; y el gate de
# artefacto se escribió sin fecha, así que la salida de la corrida anterior lo
# satisfacía igual. Un caso escrito a mano para cada uno no ataja al tercero: acá
# se DETECTA la familia por AST y se congela, así que un post-check nuevo rompe
# el ancla hasta que declare su frontera de hilo y su gate de frescura.
# =============================================================================


def _clase_runner_ast() -> ast.ClassDef:
    """AST de ``DynDOLODRunner``, leído del fuente real del módulo."""
    fuente = pathlib.Path(sky_claw.local.tools.dyndolod_runner.__file__).read_text(encoding="utf-8")
    return next(n for n in ast.parse(fuente).body if isinstance(n, ast.ClassDef) and n.name == "DynDOLODRunner")


def _llamadas(nodo: ast.AST) -> set[str]:
    """Nombres de atributo llamados dentro de ``nodo`` (``self.x()`` → ``x``)."""
    return {n.func.attr for n in ast.walk(nodo) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def _corre_en_hilo(nodo: ast.AST, metodo: str) -> bool:
    """¿Hay un ``asyncio.to_thread(self.<metodo>, …)`` dentro de ``nodo``?

    Mira la llamada COMPLETA —función y argumento— en vez de constatar que los
    dos nombres aparecen sueltos: una implementación que corre ``self.<metodo>()``
    en el event loop y threadea cualquier otra cosa satisface lo segundo y no lo
    primero (review CodeRabbit, PR #441).
    """
    return any(
        isinstance(llamada.func, ast.Attribute)
        and llamada.func.attr == "to_thread"
        and any(isinstance(arg, ast.Attribute) and arg.attr == metodo for arg in llamada.args)
        for llamada in ast.walk(nodo)
        if isinstance(llamada, ast.Call)
    )


def _referencias(nodo: ast.AST) -> set[str]:
    """Atributos MENCIONADOS dentro de ``nodo``, se llamen o no.

    Hace falta porque el método que cruza a un hilo viaja como *argumento*
    (``to_thread(self._post_check, …)``), no como llamada: mirar solo los
    ``Call`` lo daría por ausente.
    """
    return {n.attr for n in ast.walk(nodo) if isinstance(n, ast.Attribute)}


def _miembros_de_la_familia(cls: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Miembros que sondean el disco para dar el veredicto. Predicado ÚNICO.

    Vive en un solo lugar porque los dos anclas —el congelamiento del conjunto y
    el chequeo de que ninguno sea ``async``— tienen que hablar del MISMO
    universo. Con el predicado duplicado, agregar un prefijo en uno y no en el
    otro deja al miembro nuevo fuera del ancla de event loop sin que falle nadie:
    la clase de defecto que estos tests existen para atajar, dentro de los tests
    mismos (review CodeRabbit, PR #441).
    """
    return {
        m.name: m
        for m in cls.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (
            m.name.startswith(("_find_", "_leer_", "_parse_log", "_valida", "validate_", "_firma"))
            or m.name in {"_post_check", "_candidatos_de_salida", "_tiene_artefacto"}
        )
    }


def test_la_familia_del_post_check_esta_congelada() -> None:
    """El conjunto de métodos que sondean el disco para dar el veredicto.

    Igualdad LITERAL: agregar un `_find_*`/`_leer_*`/`_valida*` sin cablearlo al
    post-check rompe acá. Es el mismo instrumento que `RITUAL_TOOL_MAP` en
    `test_ritual_dispatch.py` — enumerar, no muestrear.
    """
    cls = _clase_runner_ast()
    familia = {
        m.name
        for m in cls.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (
            m.name.startswith(("_find_", "_leer_", "_parse_log", "_valida", "validate_", "_firma"))
            or m.name in {"_post_check", "_candidatos_de_salida", "_tiene_artefacto"}
        )
    }
    assert familia == {
        "_post_check",
        "_candidatos_de_salida",
        "_tiene_artefacto",
        "_firmas_de_salida",
        "_firma_de_veredicto",
        "_leer_log",
        "_parse_log",
        "_find_texgen_output",
        "_find_dyndolod_output",
        "validate_dyndolod_output",
        "_validar_salida_dyndolod",
    }


def test_la_familia_del_post_check_no_corre_en_el_event_loop() -> None:
    """Cada miembro es síncrono, salvo el wrapper público que delega a un hilo.

    Un miembro ``async def`` que toque disco directo es I/O bloqueante en el hilo
    del loop (P2 de ``coding_conventions.md``): congela la UI de NiceGUI mientras
    lee un log que llega a decenas de MB.
    """
    cls = _clase_runner_ast()
    miembros = {
        m.name: m
        for m in cls.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (
            m.name.startswith(("_find_", "_leer_", "_parse_log", "_valida", "validate_", "_firma"))
            or m.name in {"_post_check", "_candidatos_de_salida", "_tiene_artefacto"}
        )
    }

    asincronicos = {n for n, m in miembros.items() if isinstance(m, ast.AsyncFunctionDef)}
    # `validate_dyndolod_output` es API pública (la consume el servicio): sigue
    # siendo async, pero delega. Cualquier otro async en la familia es el defecto.
    assert asincronicos == {"validate_dyndolod_output"}
    assert "to_thread" in _llamadas(miembros["validate_dyndolod_output"])


def test_los_lanzadores_cruzan_el_post_check_a_un_hilo() -> None:
    """Los DOS lanzadores llevan el post-check a `to_thread`. Enumerados, no elegidos.

    `run_texgen` y `run_dyndolod` son la pareja hermana clásica de este repo: el
    defecto original del contrato CLI fue exactamente un fix que aterrizó en uno
    y no en el otro.
    """
    cls = _clase_runner_ast()
    lanzadores = {
        m.name: m for m in cls.body if isinstance(m, ast.AsyncFunctionDef) and m.name in {"run_texgen", "run_dyndolod"}
    }
    assert set(lanzadores) == {"run_texgen", "run_dyndolod"}

    for nombre, nodo in lanzadores.items():
        assert _corre_en_hilo(nodo, "_post_check"), (
            f"{nombre} debe ejecutar _post_check vía asyncio.to_thread. Afirmar que "
            "to_thread y _post_check aparecen sueltos en el mismo método no alcanza: "
            "pasa igual si el post-check corre en el loop y se threadea otra cosa."
        )


def test_el_post_check_consulta_el_gate_de_frescura() -> None:
    """El veredicto pasa por la frescura: sin esto el artefacto viejo vuelve a pasar.

    Congela el cableado, no el comportamiento (eso lo cubren los tests de U-06
    parte 2): si alguien saca la llamada, el falso verde se reconstituye entero y
    los tests de comportamiento son los únicos que lo verían — este ancla lo dice
    en el lugar donde se rompe.
    """
    cls = _clase_runner_ast()
    post_check = next(m for m in cls.body if isinstance(m, ast.FunctionDef) and m.name == "_post_check")
    assert "_firma_de_veredicto" in _llamadas(post_check)

    # Y la firma PREVIA se toma en los dos lanzadores, antes de lanzar: sin ella
    # no hay contra qué comparar y la frescura degrada a "existe el artefacto".
    for nombre in ("run_texgen", "run_dyndolod"):
        lanzador = next(m for m in cls.body if isinstance(m, ast.AsyncFunctionDef) and m.name == nombre)
        assert "_firmas_de_salida" in _referencias(lanzador), f"{nombre} no toma la firma previa del staging"


# =============================================================================
# Etapa 9 ASISTIDA — evento, resultado y chequeo de rutas de config
# =============================================================================


@pytest.mark.asyncio
async def test_started_event_anuncia_etapa_asistida(
    service: DynDOLODPipelineService,
    mock_event_bus: AsyncMock,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """El started event lleva ``assisted=True`` e instrucciones para el operador.

    La etapa 9 no tiene modo desatendido (preset y worldspaces son botones de la
    GUI): el evento debe decírselo a quien lo consuma, con qué hacer.
    """
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_success_result())
    mock_runner.validate_dyndolod_output = AsyncMock(return_value=True)
    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    mock_runner._config = mock_config
    service._runner = mock_runner

    await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    started_event = mock_event_bus.publish.call_args_list[0][0][0]
    payload = started_event.payload
    assert payload["assisted"] is True
    assert payload["operator_instructions"].strip() != ""


@pytest.mark.asyncio
async def test_resultado_exitoso_lleva_assisted(
    service: DynDOLODPipelineService,
    tmp_path: pathlib.Path,
) -> None:
    """El dict que consume el panel anuncia la etapa asistida (canal visible)."""
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
    assert result["assisted"] is True


@pytest.mark.asyncio
async def test_ruta_de_config_faltante_bloquea_antes_del_lock(
    service: DynDOLODPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Un ``plugins_file`` declarado que no existe frena el run ANTES del lock.

    El mismo fallo adentro costaría un diálogo modal de la herramienta y horas
    de timeout. ``data_dir`` existe; el que falta es el plugins_file declarado.
    """
    mock_runner = AsyncMock(spec=DynDOLODRunner)
    mock_runner.run_full_pipeline = AsyncMock(return_value=_make_success_result())
    mock_config = MagicMock()
    mock_config.mo2_mods_path = tmp_path / "mods"
    (mock_config.mo2_mods_path / "DynDOLOD Output").mkdir(parents=True)
    (tmp_path / "Data").mkdir()
    mock_config.data_dir = tmp_path / "Data"
    mock_config.ini_dir = None
    mock_config.plugins_file = tmp_path / "no_existe" / "Plugins.txt"
    mock_runner._config = mock_config
    service._runner = mock_runner

    result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    assert "Plugins.txt" in result["message"]
    mock_runner.run_full_pipeline.assert_not_awaited()
    mock_journal.begin_transaction.assert_not_awaited()
    mock_journal.commit_transaction.assert_not_called()
