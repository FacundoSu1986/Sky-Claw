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

**Snapshot rollback (T-06):** los ``target_files`` se resuelven con
:class:`LoadOrderFileResolver` — la unión de ``plugins.txt``/``loadorder.txt``
existentes en LOCALAPPDATA (LOOT corre fuera del VFS con ``--game-path``), el
profile de MO2 y un override explícito. Un sort que lanza (timeout) o sale con
error restaura el snapshot; si no se encuentra ningún candidato (entorno no
configurado), se degrada a serialización-sola con warning, el comportamiento
previo al T-06.
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
from sky_claw.local.mo2.load_order import LoadOrderFileResolver

if TYPE_CHECKING:
    from sky_claw.antigravity.core.models import LootExecutionParams
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
    from sky_claw.antigravity.security.path_validator import PathValidator
    from sky_claw.local.loot.parser import LOOTResult
    from sky_claw.local.validators.preflight import PreflightService

logger = logging.getLogger(__name__)

#: Shared lock resource id for the Skyrim load order (``plugins.txt`` /
#: ``loadorder.txt``). Used by this service AND the dry-run preview chain so a
#: real sort and a preview serialize on the load order instead of racing.
LOAD_ORDER_RESOURCE_ID = "load-order"

#: Default LOOT timeout in seconds. Preserves the prior ``run_loot`` allowance
#: (120s) rather than ``LOOTRunner``'s 60s default, so a slow masterlist update
#: or a large load order completing between 60 and 120s is not falsely timed out.
_DEFAULT_LOOT_TIMEOUT_SECONDS = 120


class _LootSortFailedError(Exception):
    """Interno: un sort con exit non-zero debe lanzar DENTRO del lock para que
    ``SnapshotTransactionLock.__aexit__`` restaure el load order; el resultado
    original viaja en la excepción para armar la respuesta al caller."""

    def __init__(self, result: LOOTResult) -> None:
        super().__init__(f"LOOT sort failed with return code {result.return_code}")
        self.result = result


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
        path_resolver: PathResolutionService | None = None,
        path_validator: PathValidator | None = None,
        loot_exe: pathlib.Path | None = None,
        timeout: int = _DEFAULT_LOOT_TIMEOUT_SECONDS,
        loot_runner: LOOTRunner | None = None,
        load_order_resolver: LoadOrderFileResolver | None = None,
        preflight: PreflightService | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._path_validator = path_validator
        self._loot_exe = loot_exe
        self._timeout = timeout
        self._loot_runner = loot_runner
        self._load_order_resolver = load_order_resolver
        self._preflight = preflight

    def _ensure_load_order_resolver(self) -> LoadOrderFileResolver:
        """Construye perezosamente el resolver de load order (mismo patrón que
        ``_ensure_loot_runner``): MO2 root/profile del path resolver si están
        configurados; LOCALAPPDATA lo toma el resolver de su entorno."""
        if self._load_order_resolver is not None:
            return self._load_order_resolver

        mo2_root: pathlib.Path | None = None
        profile = "Default"
        if self._path_resolver is not None:
            mo2_root = self._path_resolver.get_mo2_path()
            if mo2_root is not None:
                profile = self._path_resolver.get_active_profile()

        self._load_order_resolver = LoadOrderFileResolver(mo2_root=mo2_root, profile=profile)
        return self._load_order_resolver

    def _ensure_loot_runner(self) -> LOOTRunner:
        """Lazily build the LOOTRunner, resolving the LOOT exe + game path on first use.

        The LOOT executable is taken from (in order) the injected ``loot_exe``,
        the path resolver (``LOOT_EXE``), then a bare ``loot.exe`` last resort —
        so a configured/discovered install is honored instead of always assuming
        ``loot.exe`` is on the cwd/PATH.
        """
        if self._loot_runner is not None:
            return self._loot_runner

        if self._path_resolver is None:
            raise LOOTNotFoundError("Cannot run LOOT: no loot_runner injected and no path_resolver configured.")

        game_path = self._path_resolver.get_skyrim_path()
        if game_path is None:
            raise LOOTNotFoundError("Cannot run LOOT: SKYRIM_PATH is not configured.")

        loot_exe = self._loot_exe or self._path_resolver.get_loot_exe() or pathlib.Path("loot.exe")

        self._loot_runner = LOOTRunner(
            LOOTConfig(loot_exe=loot_exe, game_path=game_path, timeout=self._timeout),
            path_validator=self._path_validator,
        )
        return self._loot_runner

    async def sort_load_order(
        self,
        params: LootExecutionParams | None = None,
        *,
        update_masterlist: bool | None = None,
        override_preflight: bool = False,
    ) -> dict[str, Any]:
        """Sort the load order under the load-order lock.

        Always returns a serializable ``dict`` for known failure modes (lock
        contention, missing LOOT, timeout) so the caller can forward it verbatim
        instead of propagating an exception.

        ``update_masterlist`` takes precedence when given (the agent tool passes
        ``False`` to preserve its no-network behavior); otherwise it falls back
        to ``params.update_masterlist`` (LootExecutionParams default is True).

        Un preflight en ROJO (T-15: p.ej. symlinks + LOOT <0.29, el escenario
        de LOOT ciego ante el VFS) bloquea el sort salvo ``override_preflight``
        explícito (flujo HITL); el reporte viaja en la respuesta.
        """
        if update_masterlist is None:
            update_masterlist = bool(getattr(params, "update_masterlist", True))

        if self._preflight is not None and not override_preflight:
            preflight_report = await self._preflight.run()
            if preflight_report.blocks_mutations:
                detail = (
                    "Preflight en rojo: el sort de LOOT quedó bloqueado. "
                    + "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                )
                logger.warning("%s", detail)
                return {
                    "status": "error",
                    "success": False,
                    "message": detail,
                    "logs": detail,
                    "preflight": preflight_report.to_dict(),
                }

        try:
            runner = self._ensure_loot_runner()
        except LOOTNotFoundError as exc:
            logger.error("LOOT runner unavailable: %s", exc)
            return {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}

        # T-06: snapshotear lo que LOOT realmente puede reescribir. Sin
        # candidatos (entorno no configurado) se degrada a serialización-sola,
        # el comportamiento previo — el resolver ya lo dejó logueado.
        load_order = self._ensure_load_order_resolver().resolve()
        target_files = list(load_order.files)
        rolled_back = False

        # Referencia al lock fuera del with: rolled_back se deriva del resultado
        # REAL del rollback (tx.rollback_completed) — un restore fallido en la
        # ruta de excepción solo se loguea, así que bool(target_files) mentiría
        # (review Codex PR #238).
        tx = SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.RESOURCE_ID,
            agent_id=self.AGENT_ID,
            target_files=target_files,
            metadata={
                "source": "loot_sorting",
                "update_masterlist": update_masterlist,
                "load_order_sources": list(load_order.sources),
            },
        )
        try:
            async with tx:
                result = await runner.sort(update_masterlist=update_masterlist)
                if not result.success:
                    # Lanzar DENTRO del lock para que __aexit__ restaure el snapshot.
                    raise _LootSortFailedError(result)
        except LockAcquisitionError as exc:
            logger.warning("Lock contention on '%s': %s", self.RESOURCE_ID, exc)
            detail = f"Could not acquire load-order lock '{self.RESOURCE_ID}': {exc}"
            return {"status": "error", "success": False, "message": detail, "logs": detail}
        except _LootSortFailedError as exc:
            result = exc.result
            rolled_back = tx.rollback_completed
        except (LOOTNotFoundError, LOOTTimeoutError) as exc:
            logger.error("LOOT sort failed: %s", exc)
            return {
                "status": "error",
                "success": False,
                "message": str(exc),
                "logs": str(exc),
                "rolled_back": tx.rollback_completed,
            }

        # Contrato compartido (deuda #5): ``message`` canónico junto a los campos
        # estructurados; en éxito queda vacío (el consumidor arma su copy). En
        # fallo, incluir raw_stderr: LOOT puede salir non-zero con el error solo
        # en stderr no estructurado (errors=[] del parser) — review Codex #222.
        message = (
            ""
            if result.success
            else ("; ".join(str(e) for e in result.errors) or result.raw_stderr or result.raw_stdout or "")
        )
        return {
            "status": "success" if result.success else "error",
            "success": result.success,
            "message": message,
            "return_code": result.return_code,
            "sorted_plugins": result.sorted_plugins,
            "warnings": result.warnings,
            "errors": result.errors,
            "logs": result.raw_stdout or "",
            "rolled_back": rolled_back,
        }
