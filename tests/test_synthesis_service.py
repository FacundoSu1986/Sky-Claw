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
from sky_claw.local.validators.preflight import (
    PreflightCheck,
    PreflightReport,
    PreflightStatus,
)

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


# =============================================================================
# T-27b·1: costura de output_path + run sandboxeado
# =============================================================================


class TestOutputPathInyectable:
    """T-27b·1: el destino de salida es inyectable para que el sandbox de T-27
    pueda redirigir la escritura de Synthesis a `SandboxClone.overwrite_copy`."""

    def _service(self, mock_path_resolver: MagicMock, output_path: pathlib.Path | None = None):
        return SynthesisPipelineService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=AsyncMock(),
            path_resolver=mock_path_resolver,
            event_bus=MagicMock(),
            output_path=output_path,
        )

    def test_override_redirige_el_runner(self, mock_path_resolver: MagicMock, tmp_path: pathlib.Path) -> None:
        destino = tmp_path / "sandbox" / "overwrite"
        destino.mkdir(parents=True)

        svc = self._service(mock_path_resolver, output_path=destino)
        runner = svc._ensure_synthesis_runner()

        assert runner._config.output_path == destino

    def test_sin_override_conserva_el_overwrite_real(
        self, mock_path_resolver: MagicMock, tmp_path: pathlib.Path
    ) -> None:
        """Regresión: los call sites existentes (supervisor) no cambian."""
        svc = self._service(mock_path_resolver)
        runner = svc._ensure_synthesis_runner()

        assert runner._config.output_path == tmp_path / "MO2" / "overwrite"


@pytest.mark.asyncio
async def test_run_sandboxeado_no_toca_el_overwrite_real(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    """El test rojo (2) de T-27 (mitad Synthesis): un run contra el sandbox NO
    toca el `mo2/overwrite` real; su salida queda en la copia y aparece en el
    `diff()` (TECHNICAL_REVIEW_TASKS.md T-27, review Codex #241)."""
    from sky_claw.local.mo2.profile_sandbox import ProfileSandbox

    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "plugins.txt").write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n")

    sandbox = ProfileSandbox(mo2_root=mo2, sandbox_root=tmp_path / "sandbox")
    clone = await sandbox.clone()

    servicio = SynthesisPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=event_bus,
        pipeline_config_path=tmp_path / "nonexistent_pipeline.json",
        output_path=clone.overwrite_copy,
    )

    output_esp = clone.overwrite_copy / "Synthesis.esp"

    async def _run_pipeline(self: SynthesisRunner, patcher_ids: list[str]) -> SynthesisResult:
        # El runner escribe donde su config le dice — que debe ser el clon.
        (self._config.output_path / "Synthesis.esp").write_bytes(b"TES4")
        return _make_success_result(self._config.output_path / "Synthesis.esp")

    with (
        patch.object(SynthesisRunner, "run_pipeline", _run_pipeline),
        patch.object(SynthesisRunner, "validate_synthesis_esp", new_callable=AsyncMock, return_value=True),
    ):
        out = await servicio.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is True
    assert output_esp.exists()  # la salida quedó en la copia
    assert list((mo2 / "overwrite").iterdir()) == []  # el real, intacto

    diff = await sandbox.diff(clone)
    assert any(c.area == "overwrite" and c.relative_path == "Synthesis.esp" and c.kind == "added" for c in diff.changes)


@pytest.mark.asyncio
async def test_fallo_sandboxeado_preserva_la_evidencia(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    """review Codex #258 (P2): con output_path del sandbox y un Synthesis.esp
    previo en el clon, un fallo del pipeline NO debe disparar el rollback del
    snapshot del servicio — el diff debe mostrar la salida parcial (evidencia
    para el operador), no el estado pre-run. Dentro del sandbox, el clon ES el
    mecanismo de rollback (discard)."""
    from sky_claw.local.mo2.profile_sandbox import ProfileSandbox

    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "plugins.txt").write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n")
    # Un Synthesis.esp previo en el overwrite real → se clona al sandbox y el
    # servicio lo snapshotearia (create_snapshot=True default) si no fuera
    # por el bypass sandboxeado.
    (mo2 / "overwrite" / "Synthesis.esp").write_bytes(b"VIEJO")

    sandbox = ProfileSandbox(mo2_root=mo2, sandbox_root=tmp_path / "sandbox")
    clone = await sandbox.clone()

    servicio = SynthesisPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=event_bus,
        pipeline_config_path=tmp_path / "nonexistent_pipeline.json",
        output_path=clone.overwrite_copy,
    )

    async def _run_pipeline(self: SynthesisRunner, patcher_ids: list[str]) -> SynthesisResult:
        # El pipeline escribe salida parcial y después falla.
        (self._config.output_path / "Synthesis.esp").write_bytes(b"PARCIAL")
        return _make_failure_result()

    with patch.object(SynthesisRunner, "run_pipeline", _run_pipeline):
        out = await servicio.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    # La evidencia se preserva: el clon conserva la salida parcial, NO el
    # snapshot restaurado del estado previo.
    assert (clone.overwrite_copy / "Synthesis.esp").read_bytes() == b"PARCIAL"
    diff = await sandbox.diff(clone)
    assert any(
        c.area == "overwrite" and c.relative_path == "Synthesis.esp" and c.kind == "modified" for c in diff.changes
    )


# =============================================================================
# T-16c·2: gate de preflight en Synthesis (STAGE 7 del pipeline)
# =============================================================================


class _FakePreflight:
    """Preflight inyectable: ``run()`` devuelve un reporte fijo (gate de T-16c·2)."""

    def __init__(self, report: PreflightReport) -> None:
        self._report = report
        self.ran = False

    async def run(self) -> PreflightReport:
        self.ran = True
        return self._report


def _limits_report(status: PreflightStatus, summary: str) -> PreflightReport:
    """Reporte con un solo check de límites de plugins (el failure mode típico
    de Synthesis: >254 masters) en el estado pedido."""
    return PreflightReport(
        status=status,
        checks=(PreflightCheck(name="plugin_limits", status=status, summary=summary, details=()),),
    )


def _svc_with_preflight(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
    preflight: object,
) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=event_bus,
        pipeline_config_path=tmp_path / "nonexistent_pipeline.json",
        preflight=preflight,  # type: ignore[arg-type]  # fake duck-typed en tests
    )


@pytest.mark.asyncio
async def test_preflight_red_blocks_synthesis_without_running(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    # Un preflight ROJO (p.ej. 254 masters excedidos, o output sin permisos) frena
    # Synthesis ANTES de tocar nada: no corre el pipeline, no abre transacción.
    red = _limits_report(PreflightStatus.RED, "255 plugins full: excede el límite de slots del engine (254).")
    svc = _svc_with_preflight(
        lock_manager, snapshot_manager, mock_journal, mock_path_resolver, event_bus, tmp_path, _FakePreflight(red)
    )

    with patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock) as run_mock:
        out = await svc.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    assert out["reason"] == "PreflightBlocked"
    assert out["preflight"]["status"] == "red"
    run_mock.assert_not_awaited()
    mock_journal.begin_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_yellow_runs_and_surfaces(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    yellow = _limits_report(PreflightStatus.YELLOW, "250/254 plugins full: cerca del límite.")
    svc = _svc_with_preflight(
        lock_manager, snapshot_manager, mock_journal, mock_path_resolver, event_bus, tmp_path, _FakePreflight(yellow)
    )
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()

    with (
        patch.object(
            SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=_make_success_result(output_esp)
        ),
        patch.object(SynthesisRunner, "validate_synthesis_esp", new_callable=AsyncMock, return_value=True),
    ):
        out = await svc.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is True
    assert out["preflight"]["status"] == "yellow"  # el warning se surface, no bloquea


@pytest.mark.asyncio
async def test_preflight_green_does_not_attach(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    green = _limits_report(PreflightStatus.GREEN, "Load order dentro de límites.")
    svc = _svc_with_preflight(
        lock_manager, snapshot_manager, mock_journal, mock_path_resolver, event_bus, tmp_path, _FakePreflight(green)
    )
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()

    with (
        patch.object(
            SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=_make_success_result(output_esp)
        ),
        patch.object(SynthesisRunner, "validate_synthesis_esp", new_callable=AsyncMock, return_value=True),
    ):
        out = await svc.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is True
    assert "preflight" not in out


@pytest.mark.asyncio
async def test_ensure_preflight_builds_permissions_over_output_no_loot_version(
    synthesis_service: SynthesisPipelineService,
) -> None:
    # Smoke de construcción (sin inyectar): el preflight de Synthesis prueba
    # escritura sobre el output y NO cablea la versión de LOOT (irrelevante).
    preflight = synthesis_service._ensure_preflight()
    assert preflight is not None
    report = await preflight.run()
    names = {c.name for c in report.checks}
    assert "write_permissions" in names
    assert "loot_version" not in names
    assert report.blocks_mutations is False


def _bare_service(resolver: MagicMock) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=AsyncMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
    )


def test_build_modlist_checks_uses_mo2_profile_load_order(tmp_path: pathlib.Path) -> None:
    # review Codex #306: masters/límites se cablean desde el plugins.txt del
    # PERFIL MO2 activo (profiles/<perfil>/plugins.txt).
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    mo2 = tmp_path / "MO2"
    (mo2 / "mods" / "ModA").mkdir(parents=True)
    (mo2 / "mods" / "ModA" / "A.esp").write_bytes(b"TES4")
    (mo2 / "overwrite").mkdir()
    profile_dir = mo2 / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "plugins.txt").write_bytes(b"\xef\xbb\xbf*A.esp\r\n")

    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game)
    resolver.get_mo2_path = MagicMock(return_value=mo2)
    resolver.get_active_profile = MagicMock(return_value="Default")

    masters, limits = _bare_service(resolver)._build_modlist_checks(game, mo2)

    assert masters is not None and limits is not None  # cableado desde el perfil MO2


def test_build_modlist_checks_ignores_localappdata_without_mo2_profile(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # review Codex #306: sin plugins.txt en el perfil MO2, el gate NO se cae al
    # %LOCALAPPDATA% global que reescribe LOOT — valida solo el perfil activo.
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    mo2 = tmp_path / "MO2"
    (mo2 / "mods").mkdir(parents=True)
    (mo2 / "overwrite").mkdir()
    (mo2 / "profiles" / "Default").mkdir(parents=True)  # perfil SIN plugins.txt
    # Un plugins.txt global que el resolver de unión hubiera tomado primero.
    lad_game = tmp_path / "LocalAppData" / "Skyrim Special Edition"
    lad_game.mkdir(parents=True)
    (lad_game / "plugins.txt").write_bytes(b"\xef\xbb\xbf*Global.esp\r\n")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game)
    resolver.get_mo2_path = MagicMock(return_value=mo2)
    resolver.get_active_profile = MagicMock(return_value="Default")

    # Sin perfil resoluble → (None, None), NO se cae a LOCALAPPDATA.
    assert _bare_service(resolver)._build_modlist_checks(game, mo2) == (None, None)


# =============================================================================
# T-26 (ADR 0002): ActionManifest en Synthesis (tercer productor tras LOOT y
# xEdit). El entry point mutante (execute_pipeline) persiste el manifiesto
# fail-closed ANTES de mutar. El FlightReport (T-28) NO se emite acá: Synthesis
# corre SIEMPRE en sandbox con commit diferido a la promoción, así que el cierre
# post-vuelo con las rutas reales pertenece al promotion flow (follow-up, review
# #309). Los tests corren contra un OperationJournal REAL (como
# test_loot_service/test_xedit_service).
# =============================================================================


@pytest.fixture
async def real_journal(tmp_path: pathlib.Path):  # noqa: ANN201
    """OperationJournal real sobre una DB temporal (espejo de test_xedit_service)."""
    from sky_claw.antigravity.db.journal import OperationJournal

    j = OperationJournal(tmp_path / "synthesis_journal.db")
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

    return [
        FlightReport.model_validate(e.metadata)
        for e in await _ops_ultima_tx(journal)
        if e.metadata and e.metadata.get("kind") == "flight_report"
    ]


def _svc_real_journal(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    journal: object,
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,  # type: ignore[arg-type]
        path_resolver=mock_path_resolver,
        event_bus=event_bus,
        pipeline_config_path=tmp_path / "nonexistent_pipeline.json",
    )


@pytest.mark.asyncio
async def test_black_box_persiste_manifiesto(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    """Un pipeline exitoso persiste un ActionManifest (tool=Synthesis, con el ESP
    de salida en files_touched) ANTES de mutar (T-26). El FlightReport NO se emite
    en execute_pipeline (sandbox con commit diferido → follow-up del promotion flow)."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()
    svc = _svc_real_journal(lock_manager, snapshot_manager, real_journal, mock_path_resolver, event_bus, tmp_path)

    with (
        patch.object(
            SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=_make_success_result(output_esp)
        ),
        patch.object(SynthesisRunner, "validate_synthesis_esp", new_callable=AsyncMock, return_value=True),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await svc.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is True
    manifest = await _manifiesto_ultima_tx(real_journal)
    assert manifest.tool == "Synthesis"
    assert str(output_esp) in manifest.files_touched
    # T-28 fuera de scope acá: no se emite informe en execute_pipeline (review #309).
    assert await _informe_ultima_tx(real_journal) == []


@pytest.mark.asyncio
async def test_sin_manifiesto_no_ejecuta_patchers(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    """Si la persistencia del manifiesto falla, el pipeline NO corre (fail-closed):
    ningún patcher se ejecuta y el resultado trae reason=ActionManifestFailed (T-26)."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()
    svc = _svc_real_journal(lock_manager, snapshot_manager, real_journal, mock_path_resolver, event_bus, tmp_path)
    run_pipeline = AsyncMock(return_value=_make_success_result(output_esp))

    with (
        patch.object(real_journal, "persist_action_manifest", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(SynthesisRunner, "run_pipeline", run_pipeline),
        patch.object(SynthesisRunner, "validate_synthesis_esp", new_callable=AsyncMock, return_value=True),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await svc.execute_pipeline(patcher_ids=["patcher_a"])

    run_pipeline.assert_not_awaited()  # fail-closed: no se mutó nada
    assert out["success"] is False
    assert out["reason"] == "ActionManifestFailed"


@pytest.mark.asyncio
async def test_fallo_del_manifiesto_no_propaga_si_el_rollback_marking_falla(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    real_journal,  # noqa: ANN001
    mock_path_resolver: MagicMock,
    event_bus: CoreEventBus,
    tmp_path: pathlib.Path,
) -> None:
    """Si el journal ya falló al persistir el manifiesto Y vuelve a fallar al marcar
    la TX rolled_back, execute_pipeline igual devuelve ActionManifestFailed sin
    propagar la excepción secundaria (guardado, review #309)."""
    output_esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    output_esp.touch()
    svc = _svc_real_journal(lock_manager, snapshot_manager, real_journal, mock_path_resolver, event_bus, tmp_path)
    run_pipeline = AsyncMock(return_value=_make_success_result(output_esp))

    with (
        patch.object(real_journal, "persist_action_manifest", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(real_journal, "mark_transaction_rolled_back", AsyncMock(side_effect=RuntimeError("also down"))),
        patch.object(SynthesisRunner, "run_pipeline", run_pipeline),
        patch.object(SynthesisRunner, "validate_synthesis_esp", new_callable=AsyncMock, return_value=True),
        patch.dict(
            "os.environ",
            {
                "SKYRIM_PATH": str(tmp_path / "Skyrim"),
                "MO2_PATH": str(tmp_path / "MO2"),
                "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
            },
        ),
    ):
        out = await svc.execute_pipeline(patcher_ids=["patcher_a"])

    run_pipeline.assert_not_awaited()
    assert out["success"] is False
    assert out["reason"] == "ActionManifestFailed"  # la excepción secundaria no lo enmascaró
