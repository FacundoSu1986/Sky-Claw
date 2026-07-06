"""Tests for LootSortingService — LOOT lock coverage (audit #190).

Anchors the contract that LOOT load-order sorting runs under the shared
distributed lock (``SnapshotTransactionLock``), serializing it against
concurrent sorts and the dry-run preview chain (which snapshots/reverts the
same load order). Mirrors the fixture style of ``test_synthesis_service.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.cli import LOOTTimeoutError
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver, LoadOrderPaths
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID, LootSortingService

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


def _runner_returning(result: LOOTResult | None = None) -> MagicMock:
    runner = MagicMock()
    runner.sort = AsyncMock(
        return_value=result or LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm", "Update.esm"])
    )
    return runner


def _resolver_vacio() -> MagicMock:
    """Resolver de load order sin candidatos: evita que los tests toquen los
    plugins.txt reales de la máquina (LOCALAPPDATA) vía el resolver por defecto."""
    resolver = MagicMock()
    resolver.resolve.return_value = LoadOrderPaths(files=(), sources=())
    return resolver


def _make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
    load_order_resolver: object | None = None,
) -> LootSortingService:
    return LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        loot_runner=runner,
        load_order_resolver=load_order_resolver or _resolver_vacio(),
    )


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_sort_runs_and_returns_success(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.sort_load_order()

    assert result["success"] is True
    assert result["sorted_plugins"] == ["Skyrim.esm", "Update.esm"]
    runner.sort.assert_awaited_once()


@pytest.mark.asyncio
async def test_holds_load_order_lock_during_sort(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """While LOOT sorts, the shared load-order lock is held by this service."""
    seen: dict[str, object] = {}

    async def on_sort(**_kwargs: object) -> LOOTResult:
        seen["info"] = await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID)
        return LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"])

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=on_sort)
    svc = _make_service(lock_manager, snapshot_manager, runner)

    await svc.sort_load_order()

    info = seen["info"]
    assert info is not None
    assert info.agent_id == LootSortingService.AGENT_ID  # type: ignore[attr-defined]
    # Lock released once the transaction context exits.
    assert await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_serializes_when_load_order_lock_already_held(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """A competing holder of the load-order lock blocks the sort (serialization)."""
    await lock_manager.acquire_lock(LOAD_ORDER_RESOURCE_ID, "other-runner", ttl=30.0)
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.sort_load_order()

    assert result["success"] is False
    assert "lock" in result["logs"].lower()
    runner.sort.assert_not_awaited()  # never ran — lock could not be acquired


@pytest.mark.asyncio
async def test_releases_lock_on_runner_failure(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """If LOOT raises mid-run, the lock is still released (no leak)."""
    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=LOOTTimeoutError(60))
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.sort_load_order()

    assert result["success"] is False
    assert await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_forwards_update_masterlist_flag(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)
    params = MagicMock(update_masterlist=True)

    await svc.sort_load_order(params)

    runner.sort.assert_awaited_once_with(update_masterlist=True)


@pytest.mark.asyncio
async def test_builds_runner_from_resolver_with_preserved_timeout(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """With no injected runner, the service resolves the LOOT exe via the path
    resolver and preserves the prior 120s timeout (not LOOTRunner's 60s default)."""
    loot_exe = tmp_path / "loot.exe"
    loot_exe.touch()
    game_path = tmp_path / "Skyrim"
    game_path.mkdir()

    resolver = MagicMock()
    resolver.get_loot_exe = MagicMock(return_value=loot_exe)
    resolver.get_skyrim_path = MagicMock(return_value=game_path)
    # Sin MO2 configurado: el resolver de load order no debe inventar rutas.
    resolver.get_mo2_path = MagicMock(return_value=None)

    captured: dict[str, object] = {}

    class _FakeRunner:
        def __init__(self, config: object, path_validator: object = None) -> None:
            captured["config"] = config

        async def sort(self, *, update_masterlist: bool = False) -> LOOTResult:
            return LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"])

    svc = LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=resolver,
    )
    with patch("sky_claw.local.tools.loot_service.LOOTRunner", _FakeRunner):
        result = await svc.sort_load_order()

    assert result["success"] is True
    cfg = captured["config"]
    assert cfg.loot_exe == loot_exe  # type: ignore[attr-defined]
    assert cfg.game_path == game_path  # type: ignore[attr-defined]
    assert cfg.timeout == 120  # type: ignore[attr-defined]


def test_preview_chain_shares_load_order_resource_id() -> None:
    """The dry-run preview must lock the SAME resource id so a real sort and a
    preview serialize (preview's force-rollback can't clobber a concurrent sort)."""
    from sky_claw.antigravity.orchestrator.preview import chain_preview_service

    assert chain_preview_service._RESOURCE_ID == LOAD_ORDER_RESOURCE_ID


# =============================================================================
# T-06: snapshot real del load order (rollback si LOOT corrompe el orden)
# =============================================================================

_CONTENIDO_ORIGINAL = "Skyrim.esm\nOriginal.esp\n"


def _preparar_load_order(tmp_path: pathlib.Path) -> tuple[LoadOrderFileResolver, pathlib.Path]:
    """Crea un plugins.txt/loadorder.txt reales y un resolver apuntándoles."""
    load_order_dir = tmp_path / "load_order"
    load_order_dir.mkdir()
    plugins = load_order_dir / "plugins.txt"
    plugins.write_text(_CONTENIDO_ORIGINAL, encoding="utf-8")
    (load_order_dir / "loadorder.txt").write_text(_CONTENIDO_ORIGINAL, encoding="utf-8")
    return LoadOrderFileResolver(explicit_dir=load_order_dir), plugins


@pytest.mark.asyncio
async def test_restaura_load_order_si_loot_lanza_a_mitad(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Si LOOT muere (timeout) después de mutar plugins.txt, se restaura el original."""
    resolver, plugins = _preparar_load_order(tmp_path)

    async def sort_corrupto(**_kwargs: object) -> LOOTResult:
        plugins.write_text("CORRUPTO\n", encoding="utf-8")
        raise LOOTTimeoutError(60)

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort_corrupto)
    svc = _make_service(lock_manager, snapshot_manager, runner, load_order_resolver=resolver)

    result = await svc.sort_load_order()

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert plugins.read_text(encoding="utf-8") == _CONTENIDO_ORIGINAL


@pytest.mark.asyncio
async def test_restaura_load_order_si_loot_sale_con_error(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Un exit non-zero de LOOT también dispara la restauración del snapshot."""
    resolver, plugins = _preparar_load_order(tmp_path)

    async def sort_fallido(**_kwargs: object) -> LOOTResult:
        plugins.write_text("CORRUPTO\n", encoding="utf-8")
        return LOOTResult(return_code=1, errors=["cyclic interaction detected"])

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort_fallido)
    svc = _make_service(lock_manager, snapshot_manager, runner, load_order_resolver=resolver)

    result = await svc.sort_load_order()

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "cyclic interaction detected" in result["message"]
    assert plugins.read_text(encoding="utf-8") == _CONTENIDO_ORIGINAL


@pytest.mark.asyncio
async def test_restore_fallido_no_reporta_rollback(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Si el restore del snapshot falla, rolled_back debe ser False.

    La ruta de excepción del lock loguea el fallo de restore sin re-lanzar
    (para no enmascarar el error original de LOOT); el servicio no puede
    derivar rolled_back de bool(target_files) — review Codex PR #238.
    """
    resolver, plugins = _preparar_load_order(tmp_path)

    async def sort_fallido(**_kwargs: object) -> LOOTResult:
        plugins.write_text("CORRUPTO\n", encoding="utf-8")
        return LOOTResult(return_code=1, errors=["boom"])

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort_fallido)
    svc = _make_service(lock_manager, snapshot_manager, runner, load_order_resolver=resolver)

    with patch.object(
        snapshot_manager,
        "restore_snapshot",
        AsyncMock(side_effect=OSError("archivo bloqueado")),
    ):
        result = await svc.sort_load_order()

    assert result["success"] is False
    assert result["rolled_back"] is False
    # El archivo quedó como LOOT lo dejó — el caller debe saberlo.
    assert plugins.read_text(encoding="utf-8") == "CORRUPTO\n"


@pytest.mark.asyncio
async def test_sort_exitoso_conserva_los_cambios(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Un sort exitoso NO revierte: el orden nuevo escrito por LOOT se conserva."""
    resolver, plugins = _preparar_load_order(tmp_path)
    orden_nuevo = "Skyrim.esm\nReordenado.esp\n"

    async def sort_exitoso(**_kwargs: object) -> LOOTResult:
        plugins.write_text(orden_nuevo, encoding="utf-8")
        return LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm", "Reordenado.esp"])

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort_exitoso)
    svc = _make_service(lock_manager, snapshot_manager, runner, load_order_resolver=resolver)

    result = await svc.sort_load_order()

    assert result["success"] is True
    assert result["rolled_back"] is False
    assert plugins.read_text(encoding="utf-8") == orden_nuevo
