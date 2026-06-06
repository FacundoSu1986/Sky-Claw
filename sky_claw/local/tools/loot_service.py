"""LootSortingService — LOOT load-order sorting under the distributed lock.

Audit #190 fast-follow. LOOT ``--sort`` rewrites the shared load order
(``plugins.txt`` / ``loadorder.txt``), exactly the serializable state the other
mutating runners (xEdit, Synthesis, DynDOLOD) already guard with
:class:`SnapshotTransactionLock`. The real-execution path previously called the
deprecated, lock-free ``ModdingToolsAgent.run_loot``; this service closes that
gap so a real sort serializes against:

* another concurrent LOOT sort, and
* the dry-run preview chain (which snapshots and force-reverts the same load
  order) — both share :data:`LOAD_ORDER_RESOURCE_ID`.

**Snapshot rollback is intentionally deferred** (``target_files=[]``): the
concrete file LOOT rewrites is environment-dependent (LOOT runs as a subprocess
with ``--game-path``, outside the MO2 VFS) and is not reliably resolvable today,
so snapshotting it blindly would be a false safety net. The protection that
applies with certainty now is *serialization*; the snapshot can be added once
the load-order path is resolvable (or LOOT is run through the MO2 VFS).
"""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.local.loot.cli import (
    LOOTConfig,
    LOOTNotFoundError,
    LOOTRunner,
    LOOTTimeoutError,
)

if TYPE_CHECKING:
    from sky_claw.antigravity.core.models import LootExecutionParams
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
    from sky_claw.antigravity.security.path_validator import PathValidator

logger = logging.getLogger(__name__)

#: Shared lock resource id for the Skyrim load order (``plugins.txt`` /
#: ``loadorder.txt``). Used by this service AND the dry-run preview chain so a
#: real sort and a preview serialize on the load order instead of racing.
LOAD_ORDER_RESOURCE_ID = "load-order"

#: Default LOOT timeout in seconds. Preserves the prior ``run_loot`` allowance
#: (120s) rather than ``LOOTRunner``'s 60s default, so a slow masterlist update
#: or a large load order completing between 60 and 120s is not falsely timed out.
_DEFAULT_LOOT_TIMEOUT_SECONDS = 120


class LootSortingService:
    """Run LOOT's load-order sort under the shared distributed lock.

    Dependencies are injected (DI). ``loot_runner`` is built lazily from
    ``path_resolver`` on first use because tool paths may be unconfigured at
    construction time (mirrors ``SynthesisPipelineService._ensure_*``); it can
    also be injected directly for tests.
    """

    RESOURCE_ID: str = LOAD_ORDER_RESOURCE_ID
    AGENT_ID: str = "loot-sorting-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        path_resolver: PathResolutionService,
        path_validator: PathValidator | None = None,
        loot_exe: pathlib.Path | None = None,
        timeout: int = _DEFAULT_LOOT_TIMEOUT_SECONDS,
        loot_runner: LOOTRunner | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._path_validator = path_validator
        self._loot_exe = loot_exe
        self._timeout = timeout
        self._loot_runner = loot_runner

    def _ensure_loot_runner(self) -> LOOTRunner:
        """Lazily build the LOOTRunner, resolving the LOOT exe + game path on first use.

        The LOOT executable is taken from (in order) the injected ``loot_exe``,
        the path resolver (``LOOT_EXE``), then a bare ``loot.exe`` last resort —
        so a configured/discovered install is honored instead of always assuming
        ``loot.exe`` is on the cwd/PATH.
        """
        if self._loot_runner is not None:
            return self._loot_runner

        game_path = self._path_resolver.get_skyrim_path()
        if game_path is None:
            raise LOOTNotFoundError("Cannot run LOOT: SKYRIM_PATH is not configured.")

        loot_exe = self._loot_exe or self._path_resolver.get_loot_exe() or pathlib.Path("loot.exe")

        self._loot_runner = LOOTRunner(
            LOOTConfig(loot_exe=loot_exe, game_path=game_path, timeout=self._timeout),
            path_validator=self._path_validator,
        )
        return self._loot_runner

    async def sort_load_order(self, params: LootExecutionParams | None = None) -> dict[str, Any]:
        """Sort the load order under the load-order lock.

        Always returns a serializable ``dict`` for known failure modes (lock
        contention, missing LOOT, timeout) so the tool strategy can forward it
        verbatim instead of propagating an exception.
        """
        update_masterlist = bool(getattr(params, "update_masterlist", True))

        try:
            runner = self._ensure_loot_runner()
        except LOOTNotFoundError as exc:
            logger.error("LOOT runner unavailable: %s", exc)
            return {"status": "error", "success": False, "logs": str(exc)}

        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=self.RESOURCE_ID,
                agent_id=self.AGENT_ID,
                target_files=[],  # snapshot deferred — see module docstring
                metadata={"source": "loot_sorting", "update_masterlist": update_masterlist},
            ):
                result = await runner.sort(update_masterlist=update_masterlist)
        except LockAcquisitionError as exc:
            logger.warning("Lock contention on '%s': %s", self.RESOURCE_ID, exc)
            return {
                "status": "error",
                "success": False,
                "logs": f"Could not acquire load-order lock '{self.RESOURCE_ID}': {exc}",
            }
        except (LOOTNotFoundError, LOOTTimeoutError) as exc:
            logger.error("LOOT sort failed: %s", exc)
            return {"status": "error", "success": False, "logs": str(exc)}

        return {
            "status": "success" if result.success else "error",
            "success": result.success,
            "return_code": result.return_code,
            "sorted_plugins": result.sorted_plugins,
            "warnings": result.warnings,
            "errors": result.errors,
            "logs": result.raw_stdout or "",
        }
