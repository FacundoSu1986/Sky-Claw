"""Tests for SynthesisPipelineService.

Sprint 2 (Fase 2): Validates the extracted synthesis service using
SnapshotTransactionLock for transactional protection, event bus
integration, and proper journal lifecycle.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
)
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.synthesis_runner import (
    SynthesisResult,
    SynthesisRunner,
)
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

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
    await mgr.initialize()
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
    game_path = tmp_path / "Skyrim"
    game_path.mkdir()
    mo2_path = tmp_path / "MO2"
    mo2_path.mkdir()
    overwrite = mo2_path / "overwrite"
    overwrite.mkdir()
    synthesis_exe = tmp_path / "Synthesis.exe"
    synthesis_exe.touch()

    resolver.get_skyrim_path = MagicMock(return_value=game_path)
    resolver.get_mo2_path = MagicMock(return_value=mo2_path)
    resolver.get_synthesis_exe = MagicMock(return_value=synthesis_exe)
    return resolver


@pytest.fixture
async def event_bus() -> CoreEventBus:
    bus = CoreEventBus()
    await bus.start()
    yield bus  # type: ignore[misc]
    await bus.stop()


@pytest.fixture
def synthesis_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=event_bus,
        pipeline_config_path=tmp_path / "nonexistent_pipeline.json",
    )


def _make_success_result(output_esp: pathlib.Path) -> SynthesisResult:
    """Helper to build a successful SynthesisResult."""
    return SynthesisResult(
        success=True,
        output_esp=output_esp,
        return_code=0,
        stdout="OK",
        stderr="",
        patchers_executed=["patcher_a", "patcher_b"],
        errors=[],
    )


def _make_failure_result() -> SynthesisResult:
    """Helper to build a failed SynthesisResult."""
    return SynthesisResult(
        success=False,
        output_esp=None,
        return_code=1,
        stdout="",
        stderr="Patcher failed",
        patchers_executed=[],
        errors=["Patcher execution error"],
    )


# =============================================================================
# T1: Happy path
# =============================================================================


@pytest.mark.asyncio
async def test_happy_path_pipeline_succeeds(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Pipeline runs successfully, journal committed, events published."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()

    result = _make_success_result(output_esp)

    with (
        patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=result),
        patch.object(
            SynthesisRunner,
            "validate_synthesis_esp",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await synthesis_service.execute_pipeline(patcher_ids=["patcher_a", "patcher_b"])

    assert out["success"] is True
    assert out["patchers_executed"] == ["patcher_a", "patcher_b"]
    assert isinstance(out["output_esp"], str)
    mock_journal.begin_transaction.assert_awaited_once()
    mock_journal.commit_transaction.assert_awaited_once_with(1)
    mock_journal.mark_transaction_rolled_back.assert_not_awaited()


# =============================================================================
# T2: Pipeline fails — automatic rollback
# =============================================================================


@pytest.mark.asyncio
async def test_pipeline_failure_triggers_rollback(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """On pipeline failure, ESP is restored and journal rolled back."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    original_content = b"original ESP content"
    output_esp.write_bytes(original_content)

    async def _corrupting_pipeline(*args: object, **kwargs: object) -> SynthesisResult:
        """Simulate a pipeline that corrupts the file before failing."""
        output_esp.write_bytes(b"CORRUPTED_BY_PIPELINE")
        return _make_failure_result()

    with (
        patch.object(
            SynthesisRunner,
            "run_pipeline",
            new_callable=AsyncMock,
            side_effect=_corrupting_pipeline,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    # File should be restored to original content after rollback
    assert output_esp.read_bytes() == original_content
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)
    mock_journal.commit_transaction.assert_not_awaited()


# =============================================================================
# T3: ESP validation fails — rollback triggered
# =============================================================================


@pytest.mark.asyncio
async def test_esp_validation_failure_triggers_rollback(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Corrupt ESP detected during validation triggers rollback."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    original_content = b"good ESP before run"
    output_esp.write_bytes(original_content)

    async def _corrupting_success_pipeline(*args: object, **kwargs: object) -> SynthesisResult:
        """Simulate a pipeline that corrupts the file but reports success."""
        output_esp.write_bytes(b"CORRUPTED_ESP_OUTPUT")
        return _make_success_result(output_esp)

    with (
        patch.object(
            SynthesisRunner,
            "run_pipeline",
            new_callable=AsyncMock,
            side_effect=_corrupting_success_pipeline,
        ),
        patch.object(
            SynthesisRunner,
            "validate_synthesis_esp",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    assert "validation failed" in out["errors"][0].lower() or "corrupted" in out["errors"][0].lower()
    # File should be restored to original content after rollback
    assert output_esp.read_bytes() == original_content
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)


# =============================================================================
# T4: No patchers — early return
# =============================================================================


@pytest.mark.asyncio
async def test_no_patchers_early_return(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Empty patcher list returns error without acquiring lock or journal."""
    with patch.dict(
        "os.environ",
        {
            "SKYRIM_PATH": str(tmp_path / "Skyrim"),
            "MO2_PATH": str(tmp_path / "MO2"),
            "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
        },
    ):
        out = await synthesis_service.execute_pipeline(patcher_ids=[])

    assert out["success"] is False
    assert "No patchers" in out["errors"][0]
    mock_journal.begin_transaction.assert_not_awaited()


# =============================================================================
# T5: Runner init failure
# =============================================================================


@pytest.mark.asyncio
async def test_runner_init_failure(
    synthesis_service: SynthesisPipelineService,
    mock_path_resolver: MagicMock,
    mock_journal: AsyncMock,
) -> None:
    """Invalid env paths return error dict without lock or journal."""
    mock_path_resolver.get_skyrim_path = MagicMock(return_value=None)
    mock_path_resolver.get_mo2_path = MagicMock(return_value=None)
    mock_path_resolver.get_synthesis_exe = MagicMock(return_value=None)

    out = await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    assert "Cannot initialize" in out["stderr"]
    mock_journal.begin_transaction.assert_not_awaited()


# =============================================================================
# T6: create_snapshot=False
# =============================================================================


@pytest.mark.asyncio
async def test_create_snapshot_false_no_rollback(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """With create_snapshot=False, lock is acquired but no file restoration on failure."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.write_bytes(b"original")

    fail_result = _make_failure_result()

    with (
        patch.object(
            SynthesisRunner,
            "run_pipeline",
            new_callable=AsyncMock,
            return_value=fail_result,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await synthesis_service.execute_pipeline(
            patcher_ids=["patcher_a"],
            create_snapshot=False,
        )

    assert out["success"] is False
    # File NOT restored because snapshot was disabled
    # (content may have been modified by pipeline — we just check the test doesn't crash)
    mock_journal.mark_transaction_rolled_back.assert_awaited_once()


# =============================================================================
# T7: First run — target ESP doesn't exist
# =============================================================================


@pytest.mark.asyncio
async def test_first_run_esp_not_exists(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """When target ESP doesn't exist yet, snapshot is skipped and pipeline runs."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    # Don't create the file — simulating first run

    result = _make_success_result(output_esp)

    with (
        patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=result),
        patch.object(
            SynthesisRunner,
            "validate_synthesis_esp",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is True
    mock_journal.commit_transaction.assert_awaited_once()


# =============================================================================
# T8: Event verification
# =============================================================================


@pytest.mark.asyncio
async def test_events_published(
    synthesis_service: SynthesisPipelineService,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    """Both started and completed events are published with correct topics."""
    received: list[Event] = []
    completed_event = asyncio.Event()

    async def _capture_event(e: Event) -> None:
        received.append(e)
        if e.topic == "synthesis.pipeline.completed":
            completed_event.set()

    event_bus.subscribe("synthesis.pipeline.*", _capture_event)

    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()
    result = _make_success_result(output_esp)

    with (
        patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=result),
        patch.object(
            SynthesisRunner,
            "validate_synthesis_esp",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    # Wait deterministically for the completed event to be dispatched
    await asyncio.wait_for(completed_event.wait(), timeout=5.0)

    topics = [e.topic for e in received]
    assert "synthesis.pipeline.started" in topics
    assert "synthesis.pipeline.completed" in topics

    completed = next(e for e in received if e.topic == "synthesis.pipeline.completed")
    assert completed.payload["success"] is True
    assert completed.payload["rolled_back"] is False
    assert completed.source == "synthesis-service"


# =============================================================================
# T9: Lock contention
# =============================================================================


@pytest.mark.asyncio
async def test_lock_contention(
    synthesis_service: SynthesisPipelineService,
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
) -> None:
    """Pre-acquired lock from another agent returns error dict — no exception raised."""
    # Pre-acquire the lock
    await lock_manager.acquire_lock("Synthesis.esp", "other-agent")

    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()
    run_result = _make_success_result(output_esp)

    with (
        patch.object(
            SynthesisRunner,
            "run_pipeline",
            new_callable=AsyncMock,
            return_value=run_result,
        ),
        patch.object(
            SynthesisRunner,
            "validate_synthesis_esp",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    assert any("Lock contention" in e for e in out["errors"])


# =============================================================================
# T10: Journal transaction lifecycle
# =============================================================================


@pytest.mark.asyncio
async def test_journal_transaction_lifecycle_success(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """On success: begin_transaction → commit_transaction."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()
    result = _make_success_result(output_esp)

    with (
        patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=result),
        patch.object(
            SynthesisRunner,
            "validate_synthesis_esp",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    mock_journal.begin_transaction.assert_awaited_once_with(
        description="synthesis_pipeline",
        agent_id="synthesis-service",
    )
    mock_journal.commit_transaction.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_journal_transaction_lifecycle_failure(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """On failure: begin_transaction → mark_transaction_rolled_back."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()
    fail_result = _make_failure_result()

    with (
        patch.object(
            SynthesisRunner,
            "run_pipeline",
            new_callable=AsyncMock,
            return_value=fail_result,
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    mock_journal.begin_transaction.assert_awaited_once_with(
        description="synthesis_pipeline",
        agent_id="synthesis-service",
    )
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)
    mock_journal.commit_transaction.assert_not_awaited()


# =============================================================================
# T11: Unexpected exception marks journal rolled back
# =============================================================================


@pytest.mark.asyncio
async def test_unexpected_exception_marks_journal_rolled_back(
    synthesis_service: SynthesisPipelineService,
    mock_journal: AsyncMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    """An unexpected OSError inside the lock context marks journal rolled back.

    Regression test for the journal transaction leak: if run_pipeline() or
    validate_synthesis_esp() raises an exception that is NOT a domain error
    (SynthesisExecutionError / SynthesisValidationError / LockAcquisitionError),
    the journal transaction must still be marked rolled back and the service
    must return an error dict instead of propagating the exception.
    Additionally, synthesis.pipeline.completed must always be published (success=False).
    """
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()

    received: list[Event] = []
    completed_event = asyncio.Event()

    async def _capture_event(e: Event) -> None:
        received.append(e)
        if e.topic == "synthesis.pipeline.completed":
            completed_event.set()

    event_bus.subscribe("synthesis.pipeline.*", _capture_event)

    with (
        patch.object(
            SynthesisRunner,
            "run_pipeline",
            new_callable=AsyncMock,
            return_value=_make_success_result(output_esp),
        ),
        patch.object(
            SynthesisRunner,
            "validate_synthesis_esp",
            new_callable=AsyncMock,
            side_effect=OSError("disk read error"),
        ),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await synthesis_service.execute_pipeline(patcher_ids=["patcher_a"])

    # Service must return an error dict — not propagate the exception
    assert out["success"] is False
    assert "Unexpected error" in out["errors"][0]

    # Journal transaction started inside the lock MUST be marked rolled back
    mock_journal.begin_transaction.assert_awaited_once()
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)
    mock_journal.commit_transaction.assert_not_awaited()

    # synthesis.pipeline.completed MUST always be published, even on unexpected errors
    await asyncio.wait_for(completed_event.wait(), timeout=5.0)
    completed = next(e for e in received if e.topic == "synthesis.pipeline.completed")
    assert completed.payload["success"] is False
