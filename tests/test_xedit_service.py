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

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.core.event_payloads import (
    XEditPatchCompletedPayload,
    XEditPatchStartedPayload,
)
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools import xedit_service as xedit_service_mod
from sky_claw.local.tools.xedit_service import XEditPipelineService
from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.patch_orchestrator import PatchingError, PatchResult

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

    resolver.get_xedit_path = MagicMock(return_value=xedit_exe)
    resolver.get_skyrim_path = MagicMock(return_value=game_path)
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
    resolver.get_xedit_path = MagicMock(return_value=None)
    resolver.get_skyrim_path = MagicMock(return_value=None)

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


class _RollbackFailedLock:
    """Lock de frontera: el cuerpo falla y la restauración no se completa."""

    rollback_completed = False

    async def __aenter__(self) -> _RollbackFailedLock:
        # El lock real siempre expone ``snapshots`` (lo lee el ActionManifest
        # T-26); atributo de INSTANCIA para no compartir una lista mutable entre
        # casos (review Copilot #303).
        self.snapshots: list = []
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("Disk full"), PatchingError("xEdit crashed")])
async def test_execute_patch_no_marca_rollback_si_la_restauracion_falla(
    failure: Exception,
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """El journal y el evento no pueden declarar recuperación inexistente."""
    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock(side_effect=failure)
    mock_orchestrator._strategies = []
    service = make_service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)

    with (
        patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator),
        patch.object(xedit_service_mod, "SnapshotTransactionLock", return_value=_RollbackFailedLock()),
    ):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is False
    mock_journal.mark_transaction_rolled_back.assert_not_awaited()
    completed_call = next(
        c for c in mock_event_bus.publish.call_args_list if c.args[0].topic == "xedit.patch.completed"
    )
    assert completed_call.args[0].payload["rolled_back"] is False


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


# =============================================================================
# Tests: XEditPipelineService — dry_run / preview (plan-only)
# =============================================================================


def _resolver_without_tool_paths() -> MagicMock:
    """A path resolver with no configured tool paths (preview must still work)."""
    resolver = MagicMock()
    resolver.get_xedit_path = MagicMock(return_value=None)
    resolver.get_skyrim_path = MagicMock(return_value=None)
    return resolver


@pytest.mark.asyncio
async def test_execute_patch_dry_run_previews_without_running_xedit(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    target_plugin: pathlib.Path,
) -> None:
    """dry_run=True returns a plan-only preview and never runs xEdit (no mutation).

    The xEdit patch stage is plan-only (matrix): the mutating script is NOT
    executed, so the target plugin must stay byte-identical and no journal
    transaction is opened.  Crucially the preview must work even with the
    xEdit binary absent — a real patch would error there.
    """
    report = ConflictReport(
        total_conflicts=3,
        critical_conflicts=1,
        plugin_pairs=[
            PluginConflictPair(
                plugin_a="A.esm",
                plugin_b="B.esp",
                conflicts=[
                    RecordConflict(
                        form_id="00001234",
                        editor_id="WeapX",
                        record_type="WEAP",
                        winner="A.esm",
                        losers=["B.esp"],
                        severity="warning",
                    ),
                    RecordConflict(
                        form_id="0000ABCD",
                        editor_id="NpcY",
                        record_type="NPC_",
                        winner="A.esm",
                        losers=["B.esp"],
                        severity="critical",
                    ),
                ],
            ),
            PluginConflictPair(
                plugin_a="A.esm",
                plugin_b="C.esp",
                conflicts=[
                    RecordConflict(
                        form_id="00005678",
                        editor_id="ArmZ",
                        record_type="ARMO",
                        winner="A.esm",
                        losers=["C.esp"],
                        severity="warning",
                    ),
                ],
            ),
        ],
    )

    original_bytes = target_plugin.read_bytes()

    service = XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=_resolver_without_tool_paths(),
        event_bus=mock_event_bus,
    )

    result = await service.execute_patch(report, target_plugin, dry_run=True)

    assert result["status"] == "dry_run_preview"
    change_set = result["change_set"]
    assert change_set["stage"] == "xedit"
    assert change_set["executed_for_real"] is False

    conflicts = change_set["conflicts"]
    assert conflicts["target_plugin"] == target_plugin.name
    assert conflicts["total_conflicts"] == 3
    assert conflicts["critical"] == 1
    assert conflicts["minor"] == 2
    assert conflicts["proposed_resolution"] == "execute_xedit_script"
    # Only the single critical conflict is surfaced as a pair.
    assert len(conflicts["pairs"]) == 1
    assert conflicts["pairs"][0]["record_type"] == "NPC_"
    assert conflicts["pairs"][0]["winner"] == "A.esm"

    # No-mutation invariants.
    assert target_plugin.read_bytes() == original_bytes
    mock_journal.begin_transaction.assert_not_called()
    mock_event_bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_execute_patch_dry_run_merged_patch_when_no_critical(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    target_plugin: pathlib.Path,
) -> None:
    """With no critical conflicts the proposed resolution is a merged patch."""
    report = ConflictReport(
        total_conflicts=1,
        critical_conflicts=0,
        plugin_pairs=[
            PluginConflictPair(
                plugin_a="A.esm",
                plugin_b="B.esp",
                conflicts=[
                    RecordConflict(
                        form_id="00000001",
                        editor_id="LvlA",
                        record_type="LVLI",
                        winner="A.esm",
                        losers=["B.esp"],
                        severity="warning",
                    ),
                ],
            ),
        ],
    )

    service = XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=_resolver_without_tool_paths(),
        event_bus=mock_event_bus,
    )

    result = await service.execute_patch(report, target_plugin, dry_run=True)

    conflicts = result["change_set"]["conflicts"]
    assert conflicts["proposed_resolution"] == "create_merged_patch"
    assert conflicts["pairs"] == []  # no critical conflicts to surface
    mock_event_bus.publish.assert_not_called()


# =============================================================================
# T-26/T-28: caja negra de vuelo (ActionManifest + FlightReport) — ADR 0002
#
# xEdit es el segundo Ritual productor tras LOOT. Ambos entry points mutantes
# (execute_patch y quick_auto_clean) persisten la caja negra: manifiesto
# fail-closed ANTES de mutar, informe best-effort DESPUÉS del commit. Los tests
# corren contra un OperationJournal REAL (como test_loot_service.py) porque el
# informe se compone leyendo la caja negra ya persistida.
# =============================================================================


@pytest.fixture
async def real_journal(tmp_path: pathlib.Path):  # noqa: ANN201
    """OperationJournal real sobre una DB temporal (espejo de test_loot_service)."""
    from sky_claw.antigravity.db.journal import OperationJournal

    j = OperationJournal(tmp_path / "xedit_journal.db")
    await j.open()
    yield j  # type: ignore[misc]
    await j.close()


async def _ops_ultima_tx(journal):  # noqa: ANN001, ANN202
    (ultima,) = await journal.list_recent_transactions(limit=1)
    return await journal.get_operations_by_transaction(ultima.transaction_id)


async def _manifiesto_ultima_tx(journal):  # noqa: ANN001, ANN202
    """El op del ActionManifest (no el del FlightReport, discriminado por ``kind``)."""
    from sky_claw.antigravity.orchestrator.preview.action_manifest import ActionManifest

    metas = [
        e.metadata
        for e in await _ops_ultima_tx(journal)
        if e.metadata and e.metadata.get("ritual_id") and e.metadata.get("kind") != "flight_report"
    ]
    assert len(metas) == 1
    return ActionManifest.model_validate(metas[0])


async def _informe_ultima_tx(journal):  # noqa: ANN001, ANN202
    from sky_claw.antigravity.orchestrator.preview.flight_report import FlightReport

    informes = [
        FlightReport.model_validate(e.metadata)
        for e in await _ops_ultima_tx(journal)
        if e.metadata and e.metadata.get("kind") == "flight_report"
    ]
    return informes


def _service_real_journal(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    journal: object,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    preflight: object | None = None,
) -> XEditPipelineService:
    return XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,  # type: ignore[arg-type]
        path_resolver=mock_path_resolver,
        event_bus=mock_event_bus,
        preflight=preflight,  # type: ignore[arg-type]
    )


def _orchestrator_exitoso(target_plugin: pathlib.Path) -> AsyncMock:
    result = PatchResult(
        success=True,
        output_path=target_plugin,
        records_patched=4,
        conflicts_resolved=1,
        xedit_exit_code=0,
        warnings=(),
        error=None,
    )
    orch = AsyncMock()
    orch.resolve = AsyncMock(return_value=result)
    orch._strategies = []
    return orch


# ---- execute_patch -----------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_patch_emite_manifiesto_antes_de_ejecutar(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Un patch exitoso persiste un ActionManifest con archivos y plan de rollback
    (tool=xEdit) ANTES de mutar (T-26)."""
    orch = _orchestrator_exitoso(target_plugin)
    service = _service_real_journal(lock_manager, snapshot_manager, real_journal, mock_path_resolver, mock_event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=orch):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is True
    manifest = await _manifiesto_ultima_tx(real_journal)
    assert manifest.tool == "xEdit"
    assert str(target_plugin) in manifest.files_touched
    assert len(manifest.rollback_plan) >= 1
    assert manifest.rollback_plan[0].original_path == str(target_plugin)


@pytest.mark.asyncio
async def test_execute_patch_sin_manifiesto_no_muta(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Si la emisión del manifiesto falla, xEdit no se ejecuta (fail-closed T-26)
    y la TX no queda PENDING."""
    from sky_claw.antigravity.db.journal import TransactionStatus

    orch = _orchestrator_exitoso(target_plugin)
    service = _service_real_journal(lock_manager, snapshot_manager, real_journal, mock_path_resolver, mock_event_bus)

    with (
        patch.object(service, "_ensure_patch_orchestrator", return_value=orch),
        patch.object(real_journal, "persist_action_manifest", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is False
    assert "manifiesto" in (result.get("message", "") + result.get("error", "")).lower()
    orch.resolve.assert_not_awaited()
    recientes = await real_journal.list_recent_transactions(limit=10)
    assert all(t.status != TransactionStatus.PENDING for t in recientes)


@pytest.mark.asyncio
async def test_execute_patch_exitoso_persiste_informe_de_vuelo(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """Tras un patch exitoso queda un FlightReport commiteado en la misma TX (T-28)."""
    orch = _orchestrator_exitoso(target_plugin)
    service = _service_real_journal(lock_manager, snapshot_manager, real_journal, mock_path_resolver, mock_event_bus)

    with patch.object(service, "_ensure_patch_orchestrator", return_value=orch):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is True
    informes = await _informe_ultima_tx(real_journal)
    assert len(informes) == 1
    assert informes[0].degraded is False
    assert informes[0].tool == "xEdit"
    assert informes[0].transaction_status == "committed"


@pytest.mark.asyncio
async def test_execute_patch_fallo_del_informe_no_rompe_el_patch(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """El informe es post-vuelo best-effort: si falla, el patch exitoso no se rompe;
    el manifiesto pre-vuelo queda persistido igual."""
    orch = _orchestrator_exitoso(target_plugin)
    service = _service_real_journal(lock_manager, snapshot_manager, real_journal, mock_path_resolver, mock_event_bus)

    with (
        patch.object(service, "_ensure_patch_orchestrator", return_value=orch),
        patch.object(real_journal, "persist_flight_report", AsyncMock(side_effect=OSError("disk full"))),
    ):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is True
    manifest = await _manifiesto_ultima_tx(real_journal)
    assert manifest.tool == "xEdit"


@pytest.mark.asyncio
async def test_execute_patch_fallo_de_commit_no_rompe_el_patch(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    mock_conflict_report: ConflictReport,
    target_plugin: pathlib.Path,
) -> None:
    """El patch ya mutó: un fallo del commit del journal es best-effort y NO
    convierte el Ritual exitoso en error (misma disciplina que LOOT, review
    Copilot #303)."""
    orch = _orchestrator_exitoso(target_plugin)
    service = _service_real_journal(lock_manager, snapshot_manager, real_journal, mock_path_resolver, mock_event_bus)

    with (
        patch.object(service, "_ensure_patch_orchestrator", return_value=orch),
        patch.object(real_journal, "commit_transaction", AsyncMock(side_effect=OSError("disk full"))),
    ):
        result = await service.execute_patch(mock_conflict_report, target_plugin)

    assert result["success"] is True
    orch.resolve.assert_awaited_once()


# ---- quick_auto_clean --------------------------------------------------------


def _preparar_dirty_masters(mock_path_resolver: MagicMock) -> pathlib.Path:
    """Crea Data/Update.esm en el árbol del juego del resolver y lo devuelve."""
    game_path = mock_path_resolver.get_skyrim_path()
    data_dir = game_path / "Data"
    data_dir.mkdir(exist_ok=True)
    master = data_dir / "Update.esm"
    master.write_bytes(b"TES4")
    return master


def _preflight_verde() -> MagicMock:
    reporte = MagicMock()
    reporte.blocks_mutations = False
    reporte.status.value = "green"
    reporte.to_dict = MagicMock(return_value={})
    pf = MagicMock()
    pf.run = AsyncMock(return_value=reporte)
    return pf


def _runner_clean(success: bool) -> MagicMock:
    resultado = MagicMock()
    resultado.success = success
    resultado.exit_code = 0 if success else 3
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=resultado)
    return runner


@pytest.mark.asyncio
async def test_quick_auto_clean_emite_caja_negra(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
) -> None:
    """QuickAutoClean exitoso persiste manifiesto (masters como target_files) +
    informe commiteado, tool=SSEEdit (T-26/T-28)."""
    master = _preparar_dirty_masters(mock_path_resolver)
    service = _service_real_journal(
        lock_manager, snapshot_manager, real_journal, mock_path_resolver, mock_event_bus, preflight=_preflight_verde()
    )
    runner = _runner_clean(success=True)

    with patch.object(service, "_ensure_xedit_runner", return_value=runner):
        result = await service.quick_auto_clean()

    assert result["success"] is True
    assert result["cleaned"] == ["Update.esm"]
    manifest = await _manifiesto_ultima_tx(real_journal)
    assert manifest.tool == "SSEEdit"
    assert str(master) in manifest.files_touched
    informes = await _informe_ultima_tx(real_journal)
    assert len(informes) == 1
    assert informes[0].transaction_status == "committed"


@pytest.mark.asyncio
async def test_quick_auto_clean_sin_manifiesto_no_limpia(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
) -> None:
    """Fail-closed: si el manifiesto no se emite, no se limpia ningún master."""
    _preparar_dirty_masters(mock_path_resolver)
    service = _service_real_journal(
        lock_manager, snapshot_manager, real_journal, mock_path_resolver, mock_event_bus, preflight=_preflight_verde()
    )
    runner = _runner_clean(success=True)

    with (
        patch.object(service, "_ensure_xedit_runner", return_value=runner),
        patch.object(real_journal, "persist_action_manifest", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        result = await service.quick_auto_clean()

    assert result["success"] is False
    runner.quick_auto_clean.assert_not_awaited()


@pytest.mark.asyncio
async def test_quick_auto_clean_fallo_marca_rollback(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
) -> None:
    """Si xEdit falla, se hace rollback, la TX se marca rolled-back y no queda un
    informe de vuelo de éxito."""
    from sky_claw.antigravity.db.journal import TransactionStatus

    _preparar_dirty_masters(mock_path_resolver)
    service = _service_real_journal(
        lock_manager, snapshot_manager, real_journal, mock_path_resolver, mock_event_bus, preflight=_preflight_verde()
    )
    runner = _runner_clean(success=False)

    with patch.object(service, "_ensure_xedit_runner", return_value=runner):
        result = await service.quick_auto_clean()

    assert result["success"] is False
    recientes = await real_journal.list_recent_transactions(limit=10)
    assert all(t.status != TransactionStatus.PENDING for t in recientes)
    (ultima,) = await real_journal.list_recent_transactions(limit=1)
    assert ultima.status == TransactionStatus.ROLLED_BACK
