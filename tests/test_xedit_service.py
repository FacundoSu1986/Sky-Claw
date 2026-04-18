"""Tests for XEditPipelineService.

Sprint 2 (Fase 4): Validates the extracted xEdit service using
SnapshotTransactionLock for transactional protection, event bus
integration, and proper journal lifecycle (Regla T11).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from sky_claw.core.event_bus import CoreEventBus, Event
from sky_claw.core.event_payloads import (
    XEditPatchCompletedPayload,
    XEditPatchStartedPayload,
)
from sky_claw.db.locks import DistributedLockManager
from sky_claw.db.snapshot_manager import FileSnapshotManager
from sky_claw.tools.xedit_service import XEditPipelineService
from sky_claw.xedit.conflict_analyzer import ConflictReport
from sky_claw.xedit.patch_orchestrator import PatchingError, PatchResult

if TYPE_CHECKING:
    import pathlib


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_lock_db(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "test_locks.db"


@pytest.fixture
async def lock_manager(tmp_lock_db: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_lock_db,
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr  # type: ignore[misc]
    await mgr.close()


@pytest.fixture
def snapshot_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


@pytest.fixture
async def snapshot_manager(snapshot_dir: pathlib.Path) -> FileSnapshotManager:
    mgr = FileSnapshotManager(snapshot_dir=snapshot_dir)
    return mgr


@pytest.fixture
def mock_journal() -> AsyncMock:
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=1)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    return journal


@pytest.fixture
def mock_path_resolver(tmp_path: pathlib.Path) -> MagicMock:
    resolver = MagicMock()
    xedit_exe = tmp_path / "xEdit.exe"
    xedit_exe.touch()
    game_path = tmp_path / "Skyrim"
    game_path.mkdir()

    def fake_validate(path_str: str, var_name: str) -> pathlib.Path | None:
        mapping = {
            "XEDIT_PATH": xedit_exe,
            "SKYRIM_PATH": game_path,
        }
        return mapping.get(var_name)

    resolver.validate_env_path = MagicMock(side_effect=fake_validate)
    return resolver


@pytest.fixture
async def event_bus() -> CoreEventBus:
    bus = CoreEventBus()
    await bus.start()
    yield bus  # type: ignore[misc]
    await bus.stop()


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_conflict_report() -> ConflictReport:
    report = MagicMock(spec=ConflictReport)
    report.total_conflicts = 2
    report.critical_conflicts = 0
    report.plugin_pairs = []
    return report


@pytest.fixture
def target_plugin(tmp_path: pathlib.Path) -> pathlib.Path:
    plugin = tmp_path / "TestMod.esp"
    plugin.write_bytes(b"TES4")
    return plugin


def make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock | CoreEventBus,
) -> XEditPipelineService:
    return XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=mock_event_bus,
    )


# =============================================================================
# Tests: Event Payloads (absorbed from test_xedit_payloads_temp.py)
# =============================================================================


def test_started_payload_is_immutable() -> None:
    """frozen=True debe impedir mutación tras construcción."""
    p = XEditPatchStartedPayload(target_plugin="ModA.esp", total_conflicts=3)
    with pytest.raises(ValidationError):
        p.target_plugin = "changed"


def test_completed_payload_rolled_back_field() -> None:
    """El campo rolled_back refleja si hubo rollback automático."""
    p = XEditPatchCompletedPayload(
        target_plugin="ModA.esp",
        total_conflicts=3,
        success=False,
        records_patched=0,
        conflicts_resolved=0,
        duration_seconds=0.5,
        rolled_back=True,
    )
    assert p.rolled_back is True
    assert p.success is False


def test_payloads_to_log_dict_contains_expected_keys() -> None:
    """to_log_dict() expone todos los campos públicos del payload."""
    p = XEditPatchStartedPayload(target_plugin="ModA.esp", total_conflicts=5)
    d = p.to_log_dict()
    assert "target_plugin" in d
    assert "total_conflicts" in d
    assert "started_at" in d


# =============================================================================
# Tests: XEditPipelineService — init failures
# =============================================================================


@pytest.mark.asyncio
async def test_execute_patch_returns_error_when_xedit_path_missing(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Si XEDIT_PATH no está configurado, retorna error dict sin crash ni journal TX."""
    resolver = MagicMock()
    resolver.validate_env_path = MagicMock(return_value=None)

    service = XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=resolver,
        event_bus=mock_event_bus,
    )

    result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is False
    assert "XEDIT_PATH" in result["error"]
    mock_journal.begin_transaction.assert_not_called()
    # No events should be published — early return before publish_started
    mock_event_bus.publish.assert_not_called()


# =============================================================================
# Tests: XEditPipelineService — happy path (mocked event bus)
# =============================================================================


@pytest.mark.asyncio
async def test_execute_patch_success_publishes_events(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Un patch exitoso publica started + completed events y hace commit al journal."""
    mock_patch_result = PatchResult(
        success=True,
        output_path=target_plugin,
        records_patched=5,
        conflicts_resolved=2,
        xedit_exit_code=0,
        warnings=(),
        error=None,
    )
    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock(return_value=mock_patch_result)
    mock_orchestrator._strategies = []

    service = make_service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is True
    assert result["records_patched"] == 5
    assert mock_event_bus.publish.call_count == 2

    calls = mock_event_bus.publish.call_args_list
    topics = [call.args[0].topic for call in calls]
    assert "xedit.patch.started" in topics
    assert "xedit.patch.completed" in topics

    mock_journal.begin_transaction.assert_awaited_once_with(
        description="xedit_patch",
        agent_id="xedit-service",
    )
    mock_journal.commit_transaction.assert_awaited_once_with(1)
    mock_journal.mark_transaction_rolled_back.assert_not_called()


# =============================================================================
# Tests: XEditPipelineService — failure paths
# =============================================================================


@pytest.mark.asyncio
async def test_execute_patch_failure_marks_rollback_and_publishes_completed(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Si el parche falla, marca rollback en journal y publica completed con rolled_back=True."""
    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock(side_effect=PatchingError("xEdit crashed"))
    mock_orchestrator._strategies = []

    service = make_service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is False
    assert "xEdit crashed" in result["error"]

    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)
    mock_journal.commit_transaction.assert_not_called()

    calls = mock_event_bus.publish.call_args_list
    completed_call = next(c for c in calls if c.args[0].topic == "xedit.patch.completed")
    assert completed_call.args[0].payload["rolled_back"] is True
    assert completed_call.args[0].payload["success"] is False


@pytest.mark.asyncio
async def test_execute_patch_unexpected_exception_marks_rollback(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Una excepción inesperada dentro del lock activa rollback y retorna error dict (T11).

    Regresión: si orchestrator.resolve() lanza una excepción NO-dominio
    (OSError en lugar de PatchingError/LockAcquisitionError), el journal
    debe marcarse rolled_back y el servicio debe retornar un dict de error
    en lugar de propagar la excepción.
    """
    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock(side_effect=OSError("Disk full"))
    mock_orchestrator._strategies = []

    service = make_service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is False
    assert "Disk full" in result["error"]
    assert "Unexpected error" in result["error"]

    mock_journal.begin_transaction.assert_awaited_once()
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)
    mock_journal.commit_transaction.assert_not_called()

    # completed event must still be published even on unexpected error
    calls = mock_event_bus.publish.call_args_list
    completed_call = next(c for c in calls if c.args[0].topic == "xedit.patch.completed")
    assert completed_call.args[0].payload["success"] is False
    assert completed_call.args[0].payload["rolled_back"] is True


# =============================================================================
# Tests: Real event bus integration
# =============================================================================


@pytest.mark.asyncio
async def test_execute_patch_publishes_events_via_real_bus(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Los eventos xedit.patch.* se despachan correctamente por el bus real."""
    received: list[Event] = []
    completed_event = asyncio.Event()

    async def _capture(e: Event) -> None:
        received.append(e)
        if e.topic == "xedit.patch.completed":
            completed_event.set()

    event_bus.subscribe("xedit.patch.*", _capture)

    mock_patch_result = PatchResult(
        success=True,
        output_path=target_plugin,
        records_patched=3,
        conflicts_resolved=1,
        xedit_exit_code=0,
        warnings=(),
        error=None,
    )
    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock(return_value=mock_patch_result)
    mock_orchestrator._strategies = []

    service = make_service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator):
        await service.execute_patch(mock_conflict_report, target_plugin)

    await asyncio.wait_for(completed_event.wait(), timeout=5.0)

    topics = [e.topic for e in received]
    assert "xedit.patch.started" in topics
    assert "xedit.patch.completed" in topics

    completed = next(e for e in received if e.topic == "xedit.patch.completed")
    assert completed.payload["success"] is True
    assert completed.payload["rolled_back"] is False
    assert completed.source == "xedit-service"


# =============================================================================
# Tests: Lock contention
# =============================================================================


@pytest.mark.asyncio
async def test_execute_patch_lock_contention_returns_error(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Un lock pre-adquirido por otro agente retorna error dict sin propagar excepción."""
    # Pre-acquire the lock to simulate contention
    await lock_manager.acquire_lock(target_plugin.name, "other-agent")

    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock()  # should never be called
    mock_orchestrator._strategies = []

    service = make_service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is False
    assert "Lock contention" in result["error"]
    mock_orchestrator.resolve.assert_not_called()


# =============================================================================
# Tests: Journal transaction lifecycle
# =============================================================================


@pytest.mark.asyncio
async def test_journal_transaction_lifecycle_success(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """En éxito: begin_transaction -> commit_transaction."""
    mock_patch_result = PatchResult(
        success=True,
        output_path=target_plugin,
        records_patched=1,
        conflicts_resolved=1,
        xedit_exit_code=0,
        warnings=(),
        error=None,
    )
    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock(return_value=mock_patch_result)
    mock_orchestrator._strategies = []

    service = make_service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator):
        await service.execute_patch(mock_conflict_report, target_plugin)

    mock_journal.begin_transaction.assert_awaited_once_with(
        description="xedit_patch",
        agent_id="xedit-service",
    )
    mock_journal.commit_transaction.assert_awaited_once_with(1)
    mock_journal.mark_transaction_rolled_back.assert_not_called()


@pytest.mark.asyncio
async def test_journal_transaction_lifecycle_failure(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """En fallo: begin_transaction -> mark_transaction_rolled_back."""
    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock(side_effect=PatchingError("boom"))
    mock_orchestrator._strategies = []

    service = make_service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator):
        await service.execute_patch(mock_conflict_report, target_plugin)

    mock_journal.begin_transaction.assert_awaited_once_with(
        description="xedit_patch",
        agent_id="xedit-service",
    )
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)
    mock_journal.commit_transaction.assert_not_called()
