"""Tests del Follow-up A — PandoraPipelineService (cobertura de lock).

Ancla el contrato de que la generación de animaciones de Pandora corre bajo el lock
distribuido compartido (``SnapshotTransactionLock``), serializándola contra otras
corridas concurrentes. Espeja el estilo de fixtures de ``test_loot_service.py``.
Como la salida de Pandora es dependiente del entorno (subproceso con ``cwd``), el
snapshot se difiere (``target_files=[]``) — la protección que aplica con certeza es la
serialización, igual que en ``LootSortingService``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.pandora_runner import PandoraExecutionError, PandoraResult
from sky_claw.local.tools.pandora_service import (
    BEHAVIOR_GRAPHS_RESOURCE_ID,
    PandoraPipelineService,
)

if TYPE_CHECKING:
    import pathlib


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
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    d = tmp_path / "snapshots"
    d.mkdir()
    mgr = FileSnapshotManager(snapshot_dir=d)
    await mgr.initialize()
    return mgr


def _runner_returning(result: PandoraResult | None = None) -> MagicMock:
    runner = MagicMock()
    runner.run_pandora = AsyncMock(
        return_value=result or PandoraResult(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=1.0)
    )
    return runner


def _make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
) -> PandoraPipelineService:
    return PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        pandora_runner=runner,
    )


@pytest.mark.asyncio
async def test_run_returns_success(lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager) -> None:
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.generate_animations()

    assert result["success"] is True
    assert result["return_code"] == 0
    runner.run_pandora.assert_awaited_once()


@pytest.mark.asyncio
async def test_holds_lock_during_run(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Mientras Pandora corre, el lock de behavior-graphs lo tiene este servicio."""
    seen: dict[str, object] = {}

    async def on_run() -> PandoraResult:
        seen["info"] = await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID)
        return PandoraResult(success=True, return_code=0, stdout="", stderr="", duration_seconds=0.1)

    runner = MagicMock()
    runner.run_pandora = AsyncMock(side_effect=on_run)
    svc = _make_service(lock_manager, snapshot_manager, runner)

    await svc.generate_animations()

    info = seen["info"]
    assert info is not None
    assert info.agent_id == PandoraPipelineService.AGENT_ID  # type: ignore[attr-defined]
    # Lock liberado al salir del context transaccional.
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_serializes_when_lock_already_held(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un holder en competencia del lock bloquea la corrida (serialización)."""
    await lock_manager.acquire_lock(BEHAVIOR_GRAPHS_RESOURCE_ID, "other-runner", ttl=30.0)
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert "lock" in result["logs"].lower()
    runner.run_pandora.assert_not_awaited()  # nunca corrió — no se pudo tomar el lock


@pytest.mark.asyncio
async def test_releases_lock_on_runner_failure(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Si Pandora lanza a mitad de corrida, el lock igual se libera (sin leak)."""
    runner = MagicMock()
    runner.run_pandora = AsyncMock(side_effect=PandoraExecutionError("boom"))
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_unsuccessful_result_maps_to_error_status(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un PandoraResult con success=False (p.ej. timeout) → status error."""
    runner = _runner_returning(
        PandoraResult(success=False, return_code=-1, stdout="", stderr="timeout", duration_seconds=2.0)
    )
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_builds_runner_from_resolver(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Sin runner inyectado, el servicio resuelve el Pandora exe + game path."""
    pandora_exe = tmp_path / "Pandora.exe"
    pandora_exe.touch()
    game_path = tmp_path / "Skyrim"
    game_path.mkdir()

    resolver = MagicMock()
    resolver.get_pandora_exe = MagicMock(return_value=pandora_exe)
    resolver.get_skyrim_path = MagicMock(return_value=game_path)

    captured: dict[str, object] = {}

    class _FakeRunner:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        async def run_pandora(self) -> PandoraResult:
            return PandoraResult(success=True, return_code=0, stdout="", stderr="", duration_seconds=0.1)

    svc = PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=resolver,
    )
    with patch("sky_claw.local.tools.pandora_service.PandoraRunner", _FakeRunner):
        result = await svc.generate_animations()

    assert result["success"] is True
    cfg = captured["config"]
    assert cfg.pandora_exe == pandora_exe  # type: ignore[attr-defined]
    assert cfg.game_path == game_path  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_paths_returns_error_without_locking(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Sin Pandora exe / game path resueltos → error dict, sin tomar el lock."""
    resolver = MagicMock()
    resolver.get_pandora_exe = MagicMock(return_value=None)
    resolver.get_skyrim_path = MagicMock(return_value=None)
    svc = PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=resolver,
    )

    result = await svc.generate_animations()

    assert result["success"] is False
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None
