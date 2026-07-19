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
from sky_claw.local.validators.preflight import (
    PreflightCheck,
    PreflightReport,
    PreflightStatus,
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


class _FakePreflight:
    """Preflight inyectable: ``run()`` devuelve un reporte fijo (gate de T-16c·1)."""

    def __init__(self, report: PreflightReport) -> None:
        self._report = report
        self.ran = False

    async def run(self) -> PreflightReport:
        self.ran = True
        return self._report


def _make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_path: pathlib.Path,
    runner: MagicMock,
    preflight: object | None = None,
) -> XEditPipelineService:
    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game_path)
    svc = XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=AsyncMock(),
        path_resolver=resolver,
        event_bus=AsyncMock(),
        preflight=preflight,  # type: ignore[arg-type]  # fake duck-typed en tests
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
async def test_runner_quick_auto_clean_exit_cero_con_error_parseado_falla(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xedit = tmp_path / "SSEEdit.exe"
    xedit.touch()
    game = tmp_path / "Skyrim"
    game.mkdir()
    runner = XEditRunner(xedit_path=xedit, game_path=game)

    async def fake_run_capture(
        _args: list[str],
        **_kwargs: object,
    ) -> tuple[bytes, bytes, int]:
        return b"Error: fallo al guardar Update.esm\n", b"", 0

    monkeypatch.setattr("sky_claw.local.xedit.runner.run_capture", fake_run_capture)

    result = await runner.quick_auto_clean("Update.esm")

    assert result.exit_code == 0
    assert result.errors == ["fallo al guardar Update.esm"]
    assert result.success is False


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


# =============================================================================
# T-16c·1: gate de preflight en quick_auto_clean
# =============================================================================


def _permissions_report(status: PreflightStatus, summary: str) -> PreflightReport:
    """Reporte con un solo check de permisos en el estado pedido."""
    return PreflightReport(
        status=status,
        checks=(PreflightCheck(name="write_permissions", status=status, summary=summary, details=()),),
    )


@pytest.mark.asyncio
async def test_preflight_red_blocks_clean_without_locking_and_surfaces_report(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    # Un preflight ROJO (p. ej. Data sin permisos) frena la limpieza ANTES de
    # tocar nada: no invoca el runner, no toma el lock, y surface el semáforo.
    game = _game_with_masters(tmp_path, ("Update.esm",))
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=_ok_result())
    red = _permissions_report(PreflightStatus.RED, "Data sin permisos de escritura.")
    svc = _make_service(lock_manager, snapshot_manager, game, runner, preflight=_FakePreflight(red))

    result = await svc.quick_auto_clean()

    assert result["success"] is False
    assert result["reason"] == "PreflightBlocked"
    assert result["preflight"]["status"] == "red"
    runner.quick_auto_clean.assert_not_awaited()
    assert await lock_manager.get_lock_info(XEDIT_CLEAN_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_preflight_yellow_cleans_and_surfaces_report(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    # Amarillo advierte pero no bloquea: la limpieza corre y el reporte se surface.
    game = _game_with_masters(tmp_path, ("Update.esm",))
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=_ok_result())
    yellow = _permissions_report(PreflightStatus.YELLOW, "Escritura verificada con advertencias.")
    svc = _make_service(lock_manager, snapshot_manager, game, runner, preflight=_FakePreflight(yellow))

    result = await svc.quick_auto_clean()

    assert result["success"] is True
    assert result["cleaned"] == ["Update.esm"]
    assert result["preflight"]["status"] == "yellow"
    runner.quick_auto_clean.assert_awaited_once_with("Update.esm")


@pytest.mark.asyncio
async def test_preflight_yellow_surfaced_even_when_clean_fails(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    # Review Codex #288 (P2): un preflight amarillo se surface incluso si la
    # limpieza falla después (el runner devuelve error → rollback), en vez de
    # perderse el warning ya computado en el path de error.
    game = _game_with_masters(tmp_path, ("Update.esm",))
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(
        return_value=ScriptExecutionResult(success=False, exit_code=2, stdout="", stderr="boom", records_processed=0)
    )
    yellow = _permissions_report(PreflightStatus.YELLOW, "Escritura verificada con advertencias.")
    svc = _make_service(lock_manager, snapshot_manager, game, runner, preflight=_FakePreflight(yellow))

    result = await svc.quick_auto_clean()

    assert result["success"] is False  # la limpieza falló y se hizo rollback
    assert result["preflight"]["status"] == "yellow"  # pero el warning se surface igual


@pytest.mark.asyncio
async def test_preflight_green_does_not_attach_report(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    # Verde no ensucia el result con el reporte (mismo criterio que loot_service).
    game = _game_with_masters(tmp_path, ("Update.esm",))
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=_ok_result())
    green = _permissions_report(PreflightStatus.GREEN, "Escritura verificada en 1 ruta(s).")
    svc = _make_service(lock_manager, snapshot_manager, game, runner, preflight=_FakePreflight(green))

    result = await svc.quick_auto_clean()

    assert result["success"] is True
    assert "preflight" not in result


@pytest.mark.asyncio
async def test_preflight_builds_permissions_over_data_and_no_loot_version(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    # Smoke de construcción (sin inyectar): el preflight de xEdit prueba escritura
    # sobre Data y NO cablea la versión de LOOT (irrelevante para limpiar DLCs).
    game = _game_with_masters(tmp_path, ("Update.esm",))
    runner = MagicMock()
    runner.quick_auto_clean = AsyncMock(return_value=_ok_result())
    svc = _make_service(lock_manager, snapshot_manager, game, runner)  # sin preflight → lo construye

    preflight = svc._ensure_preflight()
    assert preflight is not None
    report = await preflight.run()
    names = {c.name for c in report.checks}
    assert "write_permissions" in names
    assert "loot_version" not in names  # xEdit no mide la versión de LOOT
    # Data de tmp es escribible → el sensor de permisos no bloquea.
    assert report.blocks_mutations is False
