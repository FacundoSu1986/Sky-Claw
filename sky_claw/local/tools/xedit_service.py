"""XEditPipelineService — servicio dedicado para parcheo transaccional xEdit.

Extraído de ``supervisor.py`` como parte del Sprint 2 Fase 4 (Strangler Fig).
Reemplaza el manejo manual de snapshots/rollback con
:class:`SnapshotTransactionLock` para atomicidad y seguridad concurrente.

Regla T11: toda excepción dentro del context manager activa rollback automático
vía ``__aexit__``. El bloque ``except Exception`` exterior marca el journal y
retorna un dict de error serializable — nunca propaga hacia el Supervisor.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import time
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.core.event_payloads import (
    XEditPatchCompletedPayload,
    XEditPatchStartedPayload,
)
from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.local.xedit.conflict_analyzer import ConflictReport
from sky_claw.local.xedit.patch_orchestrator import (
    PatchingError,
    PatchOrchestrator,
    PatchPlan,
    PatchResult,
    PatchStrategyType,
)
from sky_claw.local.xedit.runner import ScriptExecutionResult, XEditRunner

if TYPE_CHECKING:
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager

logger = logging.getLogger(__name__)

_BACKUP_STAGING_DIR = ".skyclaw_backups/"


class XEditPipelineService:
    """Servicio dedicado para la ejecución de parches xEdit transaccionales.

    Coordina ``PatchOrchestrator``, ``XEditRunner``,
    ``SnapshotTransactionLock`` y ``CoreEventBus`` para ejecutar
    parches con protección transaccional y observabilidad.
    """

    AGENT_ID: str = "xedit-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal,
        path_resolver: PathResolutionService,
        event_bus: CoreEventBus,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._path_resolver = path_resolver
        self._event_bus = event_bus

        # Lazy init — paths may not be available at construction time
        self._xedit_runner: XEditRunner | None = None
        self._patch_orchestrator: PatchOrchestrator | None = None

    # ------------------------------------------------------------------
    # Lazy initialization (migrado de SupervisorAgent._ensure_patch_orchestrator)
    # ------------------------------------------------------------------

    def _ensure_patch_orchestrator(self) -> PatchOrchestrator:
        """Inicializa lazily el PatchOrchestrator validando paths del entorno.

        CRIT-003: Valida XEDIT_PATH y SKYRIM_PATH antes de usar.

        Returns:
            PatchOrchestrator inicializado.

        Raises:
            PatchingError: Si las variables de entorno son inválidas.
        """
        if self._patch_orchestrator is not None:
            return self._patch_orchestrator

        xedit_path_str = os.environ.get("XEDIT_PATH", "")
        game_path_str = os.environ.get("SKYRIM_PATH", "")

        xedit_path = self._path_resolver.validate_env_path(xedit_path_str, "XEDIT_PATH")
        game_path = self._path_resolver.validate_env_path(game_path_str, "SKYRIM_PATH")

        if not xedit_path or not game_path:
            raise PatchingError(
                "Cannot initialize PatchOrchestrator: "
                "XEDIT_PATH and SKYRIM_PATH environment variables must be valid paths"
            )

        if not xedit_path.exists():
            raise PatchingError(f"xEdit executable not found: {xedit_path}")

        self._xedit_runner = XEditRunner(
            xedit_path=xedit_path,
            game_path=game_path,
            output_dir=pathlib.Path(_BACKUP_STAGING_DIR) / "patches",
        )

        from sky_claw.antigravity.db.rollback_manager import RollbackManager

        self._patch_orchestrator = PatchOrchestrator(
            xedit_runner=self._xedit_runner,
            snapshot_manager=self._snapshot_manager,
            rollback_manager=RollbackManager(
                journal=self._journal,
                snapshot_manager=self._snapshot_manager,
            ),
        )

        logger.info(
            "PatchOrchestrator inicializado: xedit=%s, game=%s",
            xedit_path,
            game_path,
        )
        return self._patch_orchestrator

    # ------------------------------------------------------------------
    # Patch execution (migrado de SupervisorAgent.resolve_conflict_with_patch)
    # ------------------------------------------------------------------

    async def execute_patch(
        self,
        report: ConflictReport,
        target_plugin: pathlib.Path,
    ) -> dict[str, Any]:
        """Ejecuta un parche xEdit con protección transaccional completa.

        Usa ``SnapshotTransactionLock`` para garantizar atomicidad:
        si la ejecución falla, el snapshot se restaura automáticamente
        via ``__aexit__``.

        Args:
            report: ConflictReport con los conflictos detectados.
            target_plugin: Path al plugin objetivo del parcheo.

        Returns:
            Diccionario serializable con los campos de ``PatchResult``.
        """
        t0 = time.monotonic()

        # --- Early init ---
        try:
            orchestrator = self._ensure_patch_orchestrator()
        except PatchingError as exc:
            logger.error("Error inicializando PatchOrchestrator: %s", exc)
            return self._error_dict(str(exc))

        # --- Publish started event ---
        started_payload = XEditPatchStartedPayload(
            target_plugin=str(target_plugin),
            total_conflicts=report.total_conflicts,
        )
        await self._event_bus.publish(
            Event(
                topic="xedit.patch.started",
                payload=started_payload.to_log_dict(),
                source=self.AGENT_ID,
            )
        )

        # --- Transactional execution ---
        result: PatchResult | None = None
        rolled_back = False
        tx_id: int | None = None
        in_lock_context = False

        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=target_plugin.name,
                agent_id=self.AGENT_ID,
                target_files=[target_plugin],
                metadata={
                    "source": "xedit_patch",
                    "plugin": str(target_plugin),
                    "total_conflicts": report.total_conflicts,
                },
            ):
                in_lock_context = True
                tx_id = await self._journal.begin_transaction(
                    description="xedit_patch",
                    agent_id=self.AGENT_ID,
                )

                # Resolver conflictos (genera plan)
                result = await orchestrator.resolve(report)

                # Ejecutar script si el orquestador lo requiere
                if result.success and result.output_path and self._xedit_runner is not None:
                    strategy_type = PatchStrategyType.CREATE_MERGED_PATCH
                    if orchestrator._strategies:
                        strategy_name = orchestrator._strategies[0].__class__.__name__
                        if strategy_name == "ExecuteXEditScript":
                            strategy_type = PatchStrategyType.EXECUTE_XEDIT_SCRIPT
                        elif strategy_name == "ForwardDeclaration":
                            strategy_type = PatchStrategyType.FORWARD_DECLARATION

                    plan = PatchPlan(
                        strategy_type=strategy_type,
                        target_plugins=([p.plugin_a for p in report.plugin_pairs[:1]] if report.plugin_pairs else []),
                        output_plugin=str(result.output_path),
                        form_ids=[],
                        estimated_records=result.records_patched,
                        requires_hitl=False,
                    )

                    script_result: ScriptExecutionResult = await self._xedit_runner.execute_patch(plan)
                    result = PatchResult(
                        success=script_result.exit_code == 0,
                        output_path=result.output_path,
                        records_patched=script_result.records_processed,
                        conflicts_resolved=len(report.plugin_pairs),
                        xedit_exit_code=script_result.exit_code,
                        warnings=tuple(script_result.warnings),
                        error=(None if script_result.exit_code == 0 else script_result.stderr),
                    )

                # Lanzar DENTRO del context manager para activar rollback automático
                if result is not None and not result.success:
                    raise PatchingError(f"xEdit falló con código {result.xedit_exit_code}: {result.error}")

            # Normal exit — lock context exited without error
            in_lock_context = False
            if tx_id is not None:
                await self._journal.commit_transaction(tx_id)

        except PatchingError as exc:
            # __aexit__ ya restauró el snapshot
            rolled_back = True
            if tx_id is not None:
                await self._journal.mark_transaction_rolled_back(tx_id)
            logger.error("Parcheo xEdit falló: %s", exc)
            result = PatchResult(
                success=False,
                output_path=None,
                records_patched=0,
                conflicts_resolved=0,
                xedit_exit_code=-1,
                warnings=(),
                error=str(exc),
            )

        except LockAcquisitionError as exc:
            logger.warning("Lock contention para %s: %s", target_plugin.name, exc)
            result = PatchResult(
                success=False,
                output_path=None,
                records_patched=0,
                conflicts_resolved=0,
                xedit_exit_code=-1,
                warnings=(),
                error=f"Lock contention: {exc}",
            )

        except Exception as exc:
            rolled_back = in_lock_context
            if tx_id is not None:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as rollback_exc:
                    logger.critical(
                        "Failed to mark journal TX %d rolled back after unexpected error: %s",
                        tx_id,
                        rollback_exc,
                        exc_info=True,
                    )
            logger.error("Unexpected exception in xedit patch pipeline: %s", exc, exc_info=True)
            result = PatchResult(
                success=False,
                output_path=None,
                records_patched=0,
                conflicts_resolved=0,
                xedit_exit_code=-1,
                warnings=(),
                error=f"Unexpected error: {exc}",
            )

        duration = time.monotonic() - t0

        # --- Publish completed event ---
        assert result is not None
        completed_payload = XEditPatchCompletedPayload(
            target_plugin=str(target_plugin),
            total_conflicts=report.total_conflicts,
            success=result.success,
            records_patched=result.records_patched,
            conflicts_resolved=result.conflicts_resolved,
            duration_seconds=round(duration, 3),
            rolled_back=rolled_back,
        )
        await self._event_bus.publish(
            Event(
                topic="xedit.patch.completed",
                payload=completed_payload.to_log_dict(),
                source=self.AGENT_ID,
            )
        )

        if result.success:
            logger.info(
                "Parcheo xEdit exitoso: %s (%d records, %d conflictos, %.1fs)",
                target_plugin.name,
                result.records_patched,
                result.conflicts_resolved,
                duration,
            )

        return self._result_to_dict(result)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_to_dict(result: PatchResult) -> dict[str, Any]:
        """Convierte PatchResult a dict serializable."""
        raw = dataclasses.asdict(result)
        if raw.get("output_path") is not None:
            raw["output_path"] = str(raw["output_path"])
        return raw

    @staticmethod
    def _error_dict(message: str) -> dict[str, Any]:
        """Construye un dict de error para retornos tempranos."""
        return {
            "success": False,
            "output_path": None,
            "records_patched": 0,
            "conflicts_resolved": 0,
            "xedit_exit_code": -1,
            "warnings": [],
            "error": message,
        }
