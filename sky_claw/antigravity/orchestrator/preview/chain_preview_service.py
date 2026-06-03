"""ChainPreviewService — dry-run of the full LOOT->xEdit->DynDOLOD chain.

This is the differentiator feature: it produces a typed :class:`PreviewManifest`
of everything the chain *would* change, **without permanently mutating a single
file**.  The whole chain runs inside ONE
``SnapshotTransactionLock(force_rollback=True)`` so that, no matter which stages
touched disk, every target file is reverted on the way out.

Hybrid technique (per the tool x dry-run matrix):
  * **LOOT** runs for real (it rewrites ``plugins.txt``); the lock reverts it.
  * **xEdit** runs a read-only conflict scan; the mutating patch is plan-only.
  * **DynDOLOD** is plan-only — the most expensive stage is never launched.

The force-rollback lock is the universal safety net for any stage that does
touch disk, which is what the no-mutation invariant test pins down.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import TYPE_CHECKING

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.db.locks import DistributedLockManager, SnapshotTransactionLock
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.antigravity.orchestrator.preview.manifest import (
    LoadOrderDiff,
    PreviewManifest,
    StageChangeSet,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.xedit_service import XEditPipelineService

if TYPE_CHECKING:
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.security.path_validator import PathValidator
    from sky_claw.local.loot.cli import LOOTRunner
    from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer
    from sky_claw.local.xedit.runner import XEditRunner

logger = logging.getLogger("SkyClaw.ChainPreviewService")

#: Topic the Operations Hub WebSocket fan-out forwards to the browser.
PREVIEW_TOPIC = "ops.hitl.preview"

#: Resource id for the chain-wide transaction lock.
_RESOURCE_ID = "chain-preview"


class ChainPreviewService:
    """Produce a :class:`PreviewManifest` for the LOOT->xEdit->DynDOLOD chain.

    All collaborators are injected (DI via Protocols / concrete types).  The
    xEdit and DynDOLOD pipeline services are built internally from the shared
    dependencies; the chain-specific runners (LOOT, xEdit) and the conflict
    analyzer are injected so they can be mocked in tests and wired from the
    supervisor in production.
    """

    AGENT_ID = "chain-preview-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal,
        path_resolver: PathResolutionService,
        path_validator: PathValidator,
        event_bus: CoreEventBus,
        loot_runner: LOOTRunner,
        xedit_runner: XEditRunner,
        conflict_analyzer: ConflictAnalyzer,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_validator = path_validator
        self._event_bus = event_bus
        self._loot_runner = loot_runner
        self._xedit_runner = xedit_runner
        self._conflict_analyzer = conflict_analyzer

        # Reuse the per-stage dry_run paths added in the pipeline services so
        # the chain never duplicates the plan-only logic.
        self._xedit_service = XEditPipelineService(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            journal=journal,
            path_resolver=path_resolver,
            event_bus=event_bus,
        )
        self._dyndolod_service = DynDOLODPipelineService(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            journal=journal,
            path_resolver=path_resolver,
            event_bus=event_bus,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def preview_chain(
        self,
        *,
        workflow_id: str,
        load_order_file: pathlib.Path,
        plugins_for_scan: list[str] | None = None,
        target_plugin: pathlib.Path | None = None,
        dyndolod_preset: str = "Medium",
        run_texgen: bool = True,
    ) -> PreviewManifest:
        """Run the chain in a force-rollback transaction and return the manifest.

        Args:
            workflow_id: Correlation id for this preview.
            load_order_file: The load-order file LOOT writes (e.g. ``plugins.txt``).
                Snapshotted on entry and reverted on exit.
            plugins_for_scan: Plugin names to scan for conflicts; defaults to the
                LOOT-sorted order so xEdit reads exactly what LOOT produced.
            target_plugin: The would-be patch target (preview only; never written).
            dyndolod_preset: LOD quality preset for the DynDOLOD estimate.
            run_texgen: Whether the (estimated) DynDOLOD run would include TexGen.

        Returns:
            A :class:`PreviewManifest`.  ``load_order_file`` is byte-identical to
            its pre-call state when this returns (or raises).
        """
        # Zero-trust: every path must resolve inside the sandbox before use.
        safe_load_order = self._path_validator.validate(load_order_file)
        target = target_plugin or pathlib.Path("SkyClaw_Patch.esp")

        manifest_warnings: list[str] = []
        loot_stage: StageChangeSet | None = None
        xedit_stage: StageChangeSet | None = None
        dyndolod_stage: StageChangeSet | None = None

        # One transaction over the whole chain.  force_rollback=True reverts the
        # load-order file on a CLEAN exit too — that is the dry-run guarantee.
        async with SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=_RESOURCE_ID,
            agent_id=self.AGENT_ID,
            target_files=[safe_load_order],
            force_rollback=True,
            metadata={"workflow_id": workflow_id, "preview": True},
        ):
            loot_stage = await self._preview_loot(safe_load_order)
            sorted_order = loot_stage.load_order_diff.after if loot_stage.load_order_diff else []

            # Read-only 255-plugin guard against the LOOT-sorted order.
            manifest_warnings.extend(self._plugin_limit_warnings(sorted_order))

            # xEdit reads the freshly LOOT-sorted order (the chain dependency).
            scan_plugins = plugins_for_scan if plugins_for_scan is not None else sorted_order
            xedit_stage = await self._preview_xedit(scan_plugins, target)

            dyndolod_stage = await self._preview_dyndolod(preset=dyndolod_preset, run_texgen=run_texgen)

        # Lock exited -> load order reverted.  Assemble + publish the manifest.
        manifest = PreviewManifest(
            workflow_id=workflow_id,
            stages=[loot_stage, xedit_stage, dyndolod_stage],
            load_order_diff=loot_stage.load_order_diff,
            warnings=manifest_warnings,
            summary=(
                "Dry-run preview of LOOT->xEdit->DynDOLOD. No files were modified; approve to run the chain for real."
            ),
        )
        await self._publish_preview(manifest)
        logger.info("Chain preview ready (workflow=%s): %s", workflow_id, manifest.stage_names())
        return manifest

    # ------------------------------------------------------------------
    # Per-stage previews
    # ------------------------------------------------------------------

    async def _preview_loot(self, load_order_file: pathlib.Path) -> StageChangeSet:
        """Run LOOT for real (it writes the order); the lock reverts the file."""
        before = await self._read_plugin_order(load_order_file)
        result = await self._loot_runner.sort()
        after = list(result.sorted_plugins) if result.sorted_plugins else before

        return StageChangeSet(
            stage="loot",
            executed_for_real=True,
            files_touched=[str(load_order_file)],
            load_order_diff=LoadOrderDiff.from_orders(before, after),
            warnings=list(result.warnings),
            summary=f"LOOT would reorder {len(after)} plugin(s); file reverted after preview.",
        )

    async def _preview_xedit(self, plugins: list[str], target_plugin: pathlib.Path) -> StageChangeSet:
        """Run the read-only conflict scan, then plan (not run) the patch."""
        report = await self._conflict_analyzer.analyze(plugins, self._xedit_runner)
        result = await self._xedit_service.execute_patch(report, target_plugin, dry_run=True)
        return StageChangeSet.model_validate(result["change_set"])

    async def _preview_dyndolod(self, *, preset: str, run_texgen: bool) -> StageChangeSet:
        """Estimate the LODs DynDOLOD would generate (plan-only, never launched)."""
        result = await self._dyndolod_service.execute(preset=preset, run_texgen=run_texgen, dry_run=True)
        return StageChangeSet.model_validate(result["change_set"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _plugin_limit_warnings(self, plugins: list[str]) -> list[str]:
        """Read-only 254/4096 plugin-pool check; returns warnings, never raises."""
        try:
            self._conflict_analyzer.validate_load_order_limit(plugins)
        except RuntimeError as exc:
            return [str(exc)]
        return []

    @staticmethod
    async def _read_plugin_order(path: pathlib.Path) -> list[str]:
        """Parse a plugins.txt-style file into plugin names (off the event loop)."""
        if not path.exists():
            return []
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        order: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # A leading '*' marks an enabled plugin in plugins.txt.
            order.append(line.lstrip("*").strip())
        return order

    async def _publish_preview(self, manifest: PreviewManifest) -> None:
        """Fan the manifest out to the Operations Hub via the event bus."""
        await self._event_bus.publish(
            Event(
                topic=PREVIEW_TOPIC,
                payload=manifest.model_dump(mode="json"),
                source=self.AGENT_ID,
            )
        )
