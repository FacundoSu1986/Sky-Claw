"""Rollback honesto en SynthesisPipelineService (misma lección que #295).

Antes el servicio inferían ``rolled_back`` con flags (``in_lock_context`` +
``bool(target_files)``): declaraba recuperación aunque la restauración del
snapshot hubiera fallado en ``__aexit__``. Ahora consulta el resultado REAL
del lock (``tx_lock.rollback_completed``), igual que ``xedit_service``.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools import synthesis_service as synthesis_service_mod
from sky_claw.local.tools.synthesis_runner import SynthesisResult, SynthesisRunner
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_path / "test_locks.db",
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr  # type: ignore[misc]
    await mgr.close()


@pytest.fixture
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    d = tmp_path / "snapshots"
    d.mkdir()
    mgr = FileSnapshotManager(snapshot_dir=d)
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
    (mo2_path / "overwrite").mkdir()
    synthesis_exe = tmp_path / "Synthesis.exe"
    synthesis_exe.touch()
    resolver.get_skyrim_path = MagicMock(return_value=game_path)
    resolver.get_mo2_path = MagicMock(return_value=mo2_path)
    resolver.get_synthesis_exe = MagicMock(return_value=synthesis_exe)
    return resolver


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    return bus


def _env_de_tools(tmp_path: pathlib.Path) -> dict[str, str]:
    """El runner lazy resuelve estas rutas también desde el entorno."""
    return {
        "SKYRIM_PATH": str(tmp_path / "Skyrim"),
        "MO2_PATH": str(tmp_path / "MO2"),
        "SYNTHESIS_EXE": str(tmp_path / "Synthesis.exe"),
    }


@pytest.fixture
def servicio(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=mock_event_bus,
        pipeline_config_path=tmp_path / "nonexistent_pipeline.json",
    )


def _resultado_fallido() -> SynthesisResult:
    return SynthesisResult(
        success=False,
        output_esp=None,
        return_code=1,
        stdout="",
        stderr="Patcher failed",
        patchers_executed=[],
        errors=["Patcher execution error"],
    )


class _LockRestauracionFallida:
    """Lock de frontera: el cuerpo falla y la restauración NO se completa."""

    rollback_completed = False

    async def __aenter__(self) -> _LockRestauracionFallida:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


def _payload_completed(mock_event_bus: AsyncMock) -> dict:
    completed = next(
        c for c in mock_event_bus.publish.call_args_list if c.args[0].topic == "synthesis.pipeline.completed"
    )
    return completed.args[0].payload


@pytest.mark.asyncio
async def test_no_declara_rollback_si_la_restauracion_falla(
    servicio: SynthesisPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Restore fallido → rolled_back=False y la TX queda PENDIENTE (manual)."""
    (tmp_path / "MO2" / "overwrite" / "Synthesis.esp").touch()  # target_files no vacío

    with (
        patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=_resultado_fallido()),
        patch.object(synthesis_service_mod, "SnapshotTransactionLock", return_value=_LockRestauracionFallida()),
        patch.dict("os.environ", _env_de_tools(tmp_path)),
    ):
        out = await servicio.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    # La TX no se marca rolled_back: nada se restauró — queda para recuperación manual.
    mock_journal.mark_transaction_rolled_back.assert_not_awaited()
    assert _payload_completed(mock_event_bus)["rolled_back"] is False


@pytest.mark.asyncio
async def test_declara_rollback_cuando_la_restauracion_ocurrio(
    servicio: SynthesisPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Con el lock real y snapshot restaurado, rolled_back=True + TX marcada."""
    esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    esp.write_bytes(b"TES4 original")

    with (
        patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=_resultado_fallido()),
        patch.dict("os.environ", _env_de_tools(tmp_path)),
    ):
        out = await servicio.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)
    assert _payload_completed(mock_event_bus)["rolled_back"] is True
    assert esp.read_bytes() == b"TES4 original"  # el snapshot restauró


@pytest.mark.asyncio
async def test_fallo_fuera_del_lock_no_declara_rollback(
    servicio: SynthesisPipelineService,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    tmp_path: pathlib.Path,
) -> None:
    """Si commit_transaction falla (lock ya liberado, mutación APLICADA), no
    hubo restauración: rolled_back debe ser False."""
    esp = tmp_path / "MO2" / "overwrite" / "Synthesis.esp"
    esp.touch()
    mock_journal.commit_transaction = AsyncMock(side_effect=OSError("db caída"))
    resultado_ok = SynthesisResult(
        success=True,
        output_esp=esp,
        return_code=0,
        stdout="OK",
        stderr="",
        patchers_executed=["patcher_a"],
        errors=[],
    )

    with (
        patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock, return_value=resultado_ok),
        patch.object(SynthesisRunner, "validate_synthesis_esp", new_callable=AsyncMock, return_value=True),
        patch.dict("os.environ", _env_de_tools(tmp_path)),
    ):
        out = await servicio.execute_pipeline(patcher_ids=["patcher_a"])

    assert out["success"] is False
    assert _payload_completed(mock_event_bus)["rolled_back"] is False
    # La mutación quedó aplicada sin commit: TX pendiente para recuperación manual.
    mock_journal.mark_transaction_rolled_back.assert_not_awaited()
