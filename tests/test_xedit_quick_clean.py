"""Tests del Follow-up B — "Limpiar Archivos" (SSEEdit QuickAutoClean).

Cubre el runner (construcción del comando ``-quickclean``) y el servicio
(``XEditPipelineService.quick_auto_clean``): detección de los DLC oficiales sucios
presentes, limpieza secuencial bajo ``SnapshotTransactionLock`` (snapshot para
rollback), serialización ante lock tomado y manejo de fallos.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.xedit_service import (
    XEDIT_CLEAN_RESOURCE_ID,
    XEditPipelineService,
)
from sky_claw.local.xedit.runner import (
    ScriptExecutionResult,
    XEditRunner,
    XEditValidationError,
)

if TYPE_CHECKING:
    import pathlib


# =============================================================================
# Fixtures
# =============================================================================


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
    return FileSnapshotManager(snapshot_dir=d)


def _ok_result() -> ScriptExecutionResult:
    return ScriptExecutionResult(success=True, exit_code=0, stdout="", stderr="", records_processed=3)


def _game_with_masters(tmp_path: pathlib.Path, masters: tuple[str, ...]) -> pathlib.Path:
    game = tmp_path / "Skyrim"
    data = game / "Data"
    data.mkdir(parents=True)
    for m in masters:
        (data / m).write_bytes(b"TES4")
    return game


def _make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_path: pathlib.Path,
    runner: MagicMock,
) -> XEditPipelineService:
    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game_path)
    svc = XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=AsyncMock(),
        path_resolver=resolver,
        event_bus=AsyncMock(),
    )
    svc._xedit_runner = runner  # inyectar runner — evita resolver el exe real
    return svc


# =============================================================================
# Runner: comando -quickclean
# =============================================================================


@pytest.mark.asyncio
async def test_runner_quick_auto_clean_builds_quickclean_command(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xedit = tmp_path / "SSEEdit.exe"
    xedit.touch()
    game = tmp_path / "Skyrim"
    game.mkdir()
    runner = XEditRunner(xedit_path=xedit, game_path=game)

    captured: dict[str, object] = {}

    async def fake_run_capture(args: list[str], timeout: float | None = None, cwd: str | None = None):
        captured["args"] = args
        return (b"Cleaning Update.esm... Processed 5 records", b"", 0)

    monkeypatch.setattr("sky_claw.local.xedit.runner.run_capture", fake_run_capture)

    res = await runner.quick_auto_clean("Update.esm")

    assert res.success is True
    assert res.exit_code == 0
    args = captured["args"]
    assert args[0] == str(xedit)
    # El flag real de QuickAutoClean es -quickautoclean (no el inexistente -quickclean),
    # y -autoexit es obligatorio para que el proceso headless cierre y no cuelgue.
    assert "-quickautoclean" in args
    assert "-autoexit" in args
    assert "-autoload" in args
    assert "-quickclean" not in args
    # -D: apunta al directorio Data real (donde viven los masters oficiales).
    assert f"-D:{game / 'Data'}" in args
    assert args[-1] == "Update.esm"


@pytest.mark.asyncio
async def test_runner_quick_auto_clean_rejects_bad_plugin_name(tmp_path: pathlib.Path) -> None:
    xedit = tmp_path / "SSEEdit.exe"
    xedit.touch()
    game = tmp_path / "Skyrim"
    game.mkdir()
    runner = XEditRunner(xedit_path=xedit, game_path=game)

    with pytest.raises(XEditValidationError):
        await runner.quick_auto_clean("evil & rm -rf /.esm")


# =============================================================================
# Servicio: quick_auto_clean
# =============================================================================


@pytest.mark.asyncio
async def test_cleans_existing_official_masters_sequentially(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    game = _game_with_masters(tmp_path, ("Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"))
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=_ok_result())
    svc = _make_service(lock_manager, snapshot_manager, game, runner)

    result = await svc.quick_auto_clean()

    assert result["success"] is True
    assert set(result["cleaned"]) == {"Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"}
    assert runner.quick_auto_clean.await_count == 4


@pytest.mark.asyncio
async def test_skips_masters_not_present(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    game = _game_with_masters(tmp_path, ("Update.esm",))  # solo uno presente
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=_ok_result())
    svc = _make_service(lock_manager, snapshot_manager, game, runner)

    result = await svc.quick_auto_clean()

    assert result["success"] is True
    assert result["cleaned"] == ["Update.esm"]
    runner.quick_auto_clean.assert_awaited_once_with("Update.esm")


@pytest.mark.asyncio
async def test_no_masters_present_is_success_with_empty_cleaned(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    game = _game_with_masters(tmp_path, ())  # Data vacío
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=_ok_result())
    svc = _make_service(lock_manager, snapshot_manager, game, runner)

    result = await svc.quick_auto_clean()

    assert result["success"] is True
    assert result["cleaned"] == []
    runner.quick_auto_clean.assert_not_awaited()


@pytest.mark.asyncio
async def test_holds_clean_lock_during_run_and_releases_after(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    game = _game_with_masters(tmp_path, ("Update.esm",))
    seen: dict[str, object] = {}

    async def on_clean(plugin: str) -> ScriptExecutionResult:
        seen["info"] = await lock_manager.get_lock_info(XEDIT_CLEAN_RESOURCE_ID)
        return _ok_result()

    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(side_effect=on_clean)
    svc = _make_service(lock_manager, snapshot_manager, game, runner)

    await svc.quick_auto_clean()

    assert seen["info"] is not None
    # Lock liberado al salir del context transaccional.
    assert await lock_manager.get_lock_info(XEDIT_CLEAN_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_serializes_when_clean_lock_already_held(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    await lock_manager.acquire_lock(XEDIT_CLEAN_RESOURCE_ID, "other-runner", ttl=30.0)
    game = _game_with_masters(tmp_path, ("Update.esm",))
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=_ok_result())
    svc = _make_service(lock_manager, snapshot_manager, game, runner)

    result = await svc.quick_auto_clean()

    assert result["success"] is False
    assert "lock" in result["logs"].lower()
    runner.quick_auto_clean.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_failure_rolls_back_and_reports_error(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    game = _game_with_masters(tmp_path, ("Update.esm", "Dawnguard.esm"))
    runner = MagicMock()
    # El primero falla (exit != 0) → debe abortar y hacer rollback.
    runner.quick_auto_clean = AsyncMock(
        return_value=ScriptExecutionResult(success=False, exit_code=2, stdout="", stderr="boom", records_processed=0)
    )
    svc = _make_service(lock_manager, snapshot_manager, game, runner)

    result = await svc.quick_auto_clean()

    assert result["success"] is False
    assert await lock_manager.get_lock_info(XEDIT_CLEAN_RESOURCE_ID) is None  # lock liberado


@pytest.mark.asyncio
async def test_missing_game_path_returns_error_without_locking(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=None)
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock()
    svc = XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=AsyncMock(),
        path_resolver=resolver,
        event_bus=AsyncMock(),
    )
    svc._xedit_runner = runner

    result = await svc.quick_auto_clean()

    assert result["success"] is False
    runner.quick_auto_clean.assert_not_awaited()
    assert await lock_manager.get_lock_info(XEDIT_CLEAN_RESOURCE_ID) is None
