"""Tests for ChainPreviewService — the dry-run of the LOOT->xEdit->DynDOLOD chain.

The headline guarantee is **zero permanent mutation**: the chain runs for real
inside a single ``SnapshotTransactionLock(force_rollback=True)`` and every target
file is byte-identical afterwards.  We also verify rollback on a mid-stage crash
and clean teardown (no orphan lock) on cancellation.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.antigravity.orchestrator.preview.chain_preview_service import ChainPreviewService
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolver_without_paths() -> MagicMock:
    """Path resolver with no configured tool paths (services' dry_run needs no binary)."""
    resolver = MagicMock()
    for getter in (
        "get_skyrim_path",
        "get_mo2_path",
        "get_mo2_mods_path",
        "get_dyndolod_exe",
        "get_texgen_exe",
        "get_xedit_path",
    ):
        setattr(resolver, getter, MagicMock(return_value=None))
    return resolver


def _critical_report() -> ConflictReport:
    return ConflictReport(
        total_conflicts=1,
        critical_conflicts=1,
        plugin_pairs=[
            PluginConflictPair(
                plugin_a="A.esm",
                plugin_b="B.esp",
                conflicts=[
                    RecordConflict(
                        form_id="00000001",
                        editor_id="NpcX",
                        record_type="NPC_",
                        winner="A.esm",
                        losers=["B.esp"],
                        severity="critical",
                    ),
                ],
            ),
        ],
    )


async def _build_service(
    tmp_path: pathlib.Path,
    *,
    loot_sort: Callable[[], Awaitable[LOOTResult]],
    analyze: Callable[..., Awaitable[ConflictReport]],
    event_bus: AsyncMock,
) -> tuple[ChainPreviewService, DistributedLockManager]:
    lock_mgr = DistributedLockManager(tmp_path / "locks.db", default_ttl=10.0)
    await lock_mgr.initialize()
    snap_mgr = FileSnapshotManager(snapshot_dir=tmp_path / "snaps")
    await snap_mgr.initialize()

    loot_runner = MagicMock()
    loot_runner.sort = AsyncMock(side_effect=loot_sort)

    analyzer = MagicMock()
    analyzer.validate_load_order_limit = MagicMock(return_value=None)
    analyzer.analyze = AsyncMock(side_effect=analyze)

    journal = AsyncMock()

    service = ChainPreviewService(
        lock_manager=lock_mgr,
        snapshot_manager=snap_mgr,
        journal=journal,
        path_resolver=_resolver_without_paths(),
        path_validator=PathValidator(roots=[tmp_path]),
        event_bus=event_bus,
        loot_runner=loot_runner,
        xedit_runner=MagicMock(),
        conflict_analyzer=analyzer,
    )
    return service, lock_mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_chain_no_mutation_and_full_manifest(tmp_path: pathlib.Path) -> None:
    """The star test: the chain runs for real but every target file is reverted.

    LOOT rewrites plugins.txt inside the transaction; on exit force_rollback
    restores it byte-for-byte.  The manifest still captures the would-be diff,
    conflicts, and LOD plan.
    """
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*B.esp\n*A.esm\n", encoding="utf-8")
    original_bytes = plugins_txt.read_bytes()

    async def loot_sort() -> LOOTResult:
        # LOOT reorders AND writes the file (the mutation we must revert).
        plugins_txt.write_text("*A.esm\n*B.esp\n", encoding="utf-8")
        return LOOTResult(
            return_code=0,
            sorted_plugins=["A.esm", "B.esp"],
            warnings=["LOOT moved 2 plugins"],
        )

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        return _critical_report()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        manifest = await service.preview_chain(
            workflow_id="wf-1",
            load_order_file=plugins_txt,
            target_plugin=pathlib.Path("SkyClaw_Patch.esp"),
            dyndolod_preset="High",
            run_texgen=True,
        )

        # --- ZERO permanent mutation: byte-identical after the preview. ---
        assert plugins_txt.read_bytes() == original_bytes

        # --- Manifest captured the full chain. ---
        assert manifest.workflow_id == "wf-1"
        assert manifest.stage_names() == ["loot", "xedit", "dyndolod"]

        loot_stage = manifest.stages[0]
        assert loot_stage.executed_for_real is True
        assert loot_stage.load_order_diff is not None
        assert loot_stage.load_order_diff.before == ["B.esp", "A.esm"]
        assert loot_stage.load_order_diff.after == ["A.esm", "B.esp"]

        xedit_stage = manifest.stages[1]
        assert xedit_stage.executed_for_real is False
        assert xedit_stage.conflicts is not None
        assert xedit_stage.conflicts.critical == 1
        assert xedit_stage.conflicts.proposed_resolution == "execute_xedit_script"

        dyndolod_stage = manifest.stages[2]
        assert dyndolod_stage.executed_for_real is False
        assert dyndolod_stage.lod_plan is not None
        assert dyndolod_stage.lod_plan.preset == "High"

        # Lock released, manifest published for the Operations Hub.
        assert await lock_mgr.get_lock_info("chain-preview") is None
        event_bus.publish.assert_awaited()
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_chain_reverts_when_a_stage_crashes(tmp_path: pathlib.Path) -> None:
    """If xEdit scan crashes after LOOT wrote the order, the file is restored."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*B.esp\n*A.esm\n", encoding="utf-8")
    original_bytes = plugins_txt.read_bytes()

    async def loot_sort() -> LOOTResult:
        plugins_txt.write_text("*A.esm\n*B.esp\n", encoding="utf-8")
        return LOOTResult(return_code=0, sorted_plugins=["A.esm", "B.esp"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        raise RuntimeError("xEdit scan exploded")

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        with pytest.raises(RuntimeError, match="xEdit scan exploded"):
            await service.preview_chain(workflow_id="wf-2", load_order_file=plugins_txt)

        # Load order restored despite LOOT having rewritten it before the crash.
        assert plugins_txt.read_bytes() == original_bytes
        assert await lock_mgr.get_lock_info("chain-preview") is None
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_chain_cancellation_leaves_no_orphan(tmp_path: pathlib.Path) -> None:
    """Cancelling mid-preview reverts the file and leaves no orphan lock."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*B.esp\n*A.esm\n", encoding="utf-8")
    original_bytes = plugins_txt.read_bytes()

    async def loot_sort() -> LOOTResult:
        plugins_txt.write_text("*A.esm\n*B.esp\n", encoding="utf-8")
        return LOOTResult(return_code=0, sorted_plugins=["A.esm", "B.esp"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        await asyncio.sleep(10)  # cancelled here, after LOOT mutated the file
        return _critical_report()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        task = asyncio.create_task(service.preview_chain(workflow_id="wf-3", load_order_file=plugins_txt))
        await asyncio.sleep(0.1)  # let LOOT run and the scan begin
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert plugins_txt.read_bytes() == original_bytes
        assert await lock_mgr.get_lock_info("chain-preview") is None
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_chain_fails_fast_when_load_order_missing(tmp_path: pathlib.Path) -> None:
    """If the load-order file does not exist, preview must fail fast.

    Otherwise the transaction can't snapshot it, LOOT could create it, and
    force_rollback would not remove the new file → no-mutation violated.
    """
    missing = tmp_path / "does_not_exist.txt"

    async def loot_sort() -> LOOTResult:
        return LOOTResult(return_code=0, sorted_plugins=[])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        return _critical_report()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        with pytest.raises(FileNotFoundError):
            await service.preview_chain(workflow_id="wf-missing", load_order_file=missing)

        # Nothing ran: no LOOT, no event, no orphan lock.
        service._loot_runner.sort.assert_not_awaited()
        event_bus.publish.assert_not_awaited()
        assert await lock_mgr.get_lock_info("chain-preview") is None
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_chain_event_payload_has_id(tmp_path: pathlib.Path) -> None:
    """The published ops.hitl.preview event must carry an `id` so the Operations
    Hub router enqueues it (it drops HITL events lacking id/conflict_id)."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*A.esm\n", encoding="utf-8")

    async def loot_sort() -> LOOTResult:
        return LOOTResult(return_code=0, sorted_plugins=["A.esm"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        return _critical_report()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        await service.preview_chain(workflow_id="wf-id", load_order_file=plugins_txt)

        event_bus.publish.assert_awaited()
        published = event_bus.publish.await_args.args[0]
        assert published.payload.get("id") == "wf-id"
        assert published.payload.get("workflow_id") == "wf-id"
    finally:
        await lock_mgr.close()
