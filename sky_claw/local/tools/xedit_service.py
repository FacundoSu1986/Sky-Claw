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
from sky_claw.local.xedit.runner import ScriptExecutionResult, XEditError, XEditRunner

if TYPE_CHECKING:
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.validators.preflight import PreflightReport, PreflightService

logger = logging.getLogger(__name__)


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service`` (T-16b): un semáforo verde no ensucia el
    dict; amarillo/rojo viajan como ``result["preflight"]`` para que el panel vivo
    (``_ritual_preflight_panel``) los renderice.
    """
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


_BACKUP_STAGING_DIR = ".skyclaw_backups/"

#: Lock resource id para la limpieza QuickAutoClean (serializa contra otras corridas).
XEDIT_CLEAN_RESOURCE_ID = "xedit-quickclean"

#: DLC oficiales sucios de Skyrim SE/AE que QuickAutoClean limpia (ITM/deleted refs).
#: Skyrim.esm NO se limpia. Viven en el directorio Data del juego (no son mods MO2).
_OFFICIAL_DIRTY_MASTERS: tuple[str, ...] = (
    "Update.esm",
    "Dawnguard.esm",
    "HearthFires.esm",
    "Dragonborn.esm",
)


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
        preflight: PreflightService | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._path_resolver = path_resolver
        self._event_bus = event_bus

        # Lazy init — paths may not be available at construction time
        self._xedit_runner: XEditRunner | None = None
        self._patch_orchestrator: PatchOrchestrator | None = None
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        self._preflight = preflight

    # ------------------------------------------------------------------
    # Lazy initialization (migrado de SupervisorAgent._ensure_patch_orchestrator)
    # ------------------------------------------------------------------

    def _ensure_xedit_runner(self) -> XEditRunner:
        """Inicializa lazily el :class:`XEditRunner` validando paths del entorno.

        CRIT-003: Valida XEDIT_PATH y SKYRIM_PATH antes de usar. Compartido por
        el parcheo (``_ensure_patch_orchestrator``) y la limpieza
        (``quick_auto_clean``).

        Raises:
            PatchingError: Si las variables de entorno son inválidas.
        """
        if self._xedit_runner is not None:
            return self._xedit_runner

        xedit_path = self._path_resolver.get_xedit_path()
        game_path = self._path_resolver.get_skyrim_path()

        if not xedit_path or not game_path:
            raise PatchingError(
                "Cannot initialize XEditRunner: XEDIT_PATH and SKYRIM_PATH environment variables must be valid paths"
            )

        if not xedit_path.exists():
            raise PatchingError(f"xEdit executable not found: {xedit_path}")

        self._xedit_runner = XEditRunner(
            xedit_path=xedit_path,
            game_path=game_path,
            output_dir=pathlib.Path(_BACKUP_STAGING_DIR) / "patches",
        )
        return self._xedit_runner

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

        self._ensure_xedit_runner()

        from sky_claw.antigravity.db.rollback_manager import RollbackManager

        self._patch_orchestrator = PatchOrchestrator(
            xedit_runner=self._xedit_runner,
            snapshot_manager=self._snapshot_manager,
            rollback_manager=RollbackManager(
                journal=self._journal,
                snapshot_manager=self._snapshot_manager,
            ),
        )

        logger.info("PatchOrchestrator inicializado: runner=%s", self._xedit_runner)
        return self._patch_orchestrator

    # ------------------------------------------------------------------
    # Patch execution (migrado de SupervisorAgent.resolve_conflict_with_patch)
    # ------------------------------------------------------------------

    async def execute_patch(
        self,
        report: ConflictReport,
        target_plugin: pathlib.Path,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Ejecuta un parche xEdit con protección transaccional completa.

        Usa ``SnapshotTransactionLock`` para garantizar atomicidad:
        si la ejecución falla, el snapshot se restaura automáticamente
        via ``__aexit__``.

        Args:
            report: ConflictReport con los conflictos detectados.
            target_plugin: Path al plugin objetivo del parcheo.
            dry_run: Si True, NO ejecuta xEdit; devuelve un preview plan-only
                (``status="dry_run_preview"`` + ``change_set``) sin tocar disco.

        Returns:
            Diccionario serializable con los campos de ``PatchResult``, o el
            preview plan-only cuando ``dry_run=True``.
        """
        if dry_run:
            return await self._preview_patch(report, target_plugin)

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
        # M-7: bindear el lock para reportar el resultado REAL del rollback
        # (tx_lock.rollback_completed) en vez de hardcodear rolled_back=True.
        tx_lock: SnapshotTransactionLock | None = None

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
            ) as tx_lock:
                in_lock_context = True
                tx_id = await self._journal.begin_transaction(
                    description="xedit_patch",
                    agent_id=self.AGENT_ID,
                )

                # Resolver conflictos (genera plan)
                result = await orchestrator.resolve(report)

                # Ejecutar script si el orquestador lo requiere. El enrutado usa
                # el strategy_type del plan SELECCIONADO (T-04): el código previo
                # adivinaba mirando strategies[0], que no es la estrategia elegida.
                # Fallback EXECUTE_XEDIT_SCRIPT para PatchResult sin strategy_type.
                strategy_type = result.strategy_type or PatchStrategyType.EXECUTE_XEDIT_SCRIPT
                delegated_to_wrye_bash = strategy_type is PatchStrategyType.DELEGATE_BASHED_PATCH
                if delegated_to_wrye_bash:
                    # ADR 0001: leveled lists van al Bashed Patch; acá no se
                    # ejecuta xEdit. El caller encadena generate_bashed_patch.
                    logger.info(
                        "Plan delegado al Bashed Patch (Wrye Bash); no se ejecuta xEdit: %s",
                        result.output_path,
                    )
                elif result.success and result.output_path and self._xedit_runner is not None:
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
                        strategy_type=strategy_type,
                    )

                # Lanzar DENTRO del context manager para activar rollback automático
                if result is not None and not result.success:
                    raise PatchingError(f"xEdit falló con código {result.xedit_exit_code}: {result.error}")

            # Normal exit — lock context exited without error
            in_lock_context = False
            if tx_id is not None:
                await self._journal.commit_transaction(tx_id)

        except PatchingError as exc:
            # __aexit__ ya intentó restaurar el snapshot. M-7: reportar el
            # resultado REAL — rollback_completed es False si el restore falló
            # silenciosamente, dejando los masters en estado parcial.
            rolled_back = bool(tx_lock and tx_lock.rollback_completed)
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
    # QuickAutoClean (Follow-up B) — limpieza de los DLC oficiales sucios
    # ------------------------------------------------------------------

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight de xEdit (T-16c·1).

        Tailored a ``quick_auto_clean``, que reescribe los DLC oficiales en el
        directorio ``Data`` del juego: los sensores relevantes son **permisos de
        escritura sobre ``Data``** (si no es escribible, la limpieza muere a mitad)
        y **symlinks/junctions** en las rutas crudas del juego/MO2. NO cablea la
        versión de LOOT ni masters/límites/overwrite — irrelevantes para limpiar
        masters base. Reusa las primitivas compartidas (``VfsHealthChecker``,
        ``WritePermissionsChecker``); no toca ``loot_service``. Devuelve ``None``
        si no hay ruta de juego (sin gate, mismo criterio que loot).
        """
        if self._preflight is not None:
            return self._preflight

        game = self._path_resolver.get_skyrim_path()
        if not isinstance(game, pathlib.Path):
            return None

        # Import perezoso (anti-ciclo: validators.preflight llega a tools._process).
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.vfs_health import VfsHealthChecker
        from sky_claw.local.validators.write_permissions import WritePermissionsChecker

        # Rutas CRUDAS para el sensor de symlinks (las resueltas ya los siguieron).
        raw_game = self._path_resolver.get_skyrim_path_raw()
        raw_mo2 = self._path_resolver.get_mo2_path_raw()
        raw_game = raw_game if isinstance(raw_game, pathlib.Path) else None
        raw_mo2 = raw_mo2 if isinstance(raw_mo2, pathlib.Path) else None
        vfs_checker = None
        if raw_game is not None or raw_mo2 is not None:
            vfs_checker = VfsHealthChecker(game_path=raw_game, mo2_root=raw_mo2, scan_mods_dir=False)

        data_dir = game / "Data"

        def _permissions():
            # Re-probe por llamada (freshness): un cambio de permisos entre corridas se ve.
            return WritePermissionsChecker(targets=[data_dir]).check()

        # omit_unconfigured: xEdit solo cablea vfs + permisos; sin esto el panel
        # mostraría "no configurado" para LOOT/masters/límites/overwrite (ruido).
        self._preflight = PreflightService(
            vfs_checker=vfs_checker,
            permissions_check=_permissions,
            omit_unconfigured=True,
        )
        return self._preflight

    async def quick_auto_clean(self) -> dict[str, Any]:
        """Limpia los DLC oficiales sucios con SSEEdit QuickAutoClean.

        Corre ``-quickclean`` sobre los masters oficiales presentes en el directorio
        ``Data`` del juego (Update/Dawnguard/HearthFires/Dragonborn) en secuencia,
        bajo un único :class:`SnapshotTransactionLock`: snapshotea los masters para
        rollback automático y serializa contra otras corridas. Si la limpieza de
        cualquiera falla, el ``__aexit__`` del lock restaura **todos** los masters
        (operación atómica).

        Nunca propaga: los modos de fallo conocidos (paths faltantes, contención de
        lock, fallo de xEdit) se devuelven como un ``dict`` serializable para que el
        dispatcher lo reenvíe verbatim. La aprobación HITL la dueña el gate del
        dispatcher (``quick_auto_clean`` ∈ ``DESTRUCTIVE_TOOL_PATTERNS``).
        """
        game_path = self._path_resolver.get_skyrim_path()
        if game_path is None:
            detail = "SKYRIM_PATH no está configurado."
            return {"status": "error", "success": False, "message": detail, "logs": detail}

        # Preflight brutal ANTES de tocar nada (T-16c·1): un semáforo ROJO (p. ej.
        # Data sin permisos de escritura) cancela la limpieza sin tomar el lock ni
        # invocar xEdit. Amarillo/verde no bloquean; el reporte se surface al panel.
        preflight = self._ensure_preflight()
        preflight_report = None
        if preflight is not None:
            preflight_report = await preflight.run()
            if preflight_report.blocks_mutations:
                red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                logger.warning("QuickAutoClean bloqueado por preflight en rojo: %s", red)
                return {
                    "status": "error",
                    "success": False,
                    "reason": "PreflightBlocked",
                    "message": f"Preflight en rojo, limpieza cancelada: {red}",
                    "logs": red,
                    "preflight": preflight_report.to_dict(),
                }

        try:
            runner = self._ensure_xedit_runner()
        except PatchingError as exc:
            logger.error("XEditRunner no disponible para QuickAutoClean: %s", exc)
            return {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}

        data_dir = game_path / "Data"
        targets = [data_dir / master for master in _OFFICIAL_DIRTY_MASTERS if (data_dir / master).is_file()]
        if not targets:
            logger.info("QuickAutoClean: no se encontraron DLC oficiales en %s", data_dir)
            # Contrato: message vacío en éxito; el detalle informativo va en logs.
            return _attach_preflight(
                {
                    "status": "success",
                    "success": True,
                    "message": "",
                    "cleaned": [],
                    "logs": "No se encontraron DLC oficiales para limpiar.",
                },
                preflight_report,
            )

        cleaned: list[str] = []
        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=XEDIT_CLEAN_RESOURCE_ID,
                agent_id=self.AGENT_ID,
                target_files=targets,
                metadata={"source": "xedit_quickclean", "masters": [p.name for p in targets]},
            ):
                for path in targets:
                    result = await runner.quick_auto_clean(path.name)
                    if not result.success:
                        # Lanzar DENTRO del context activa el rollback automático.
                        raise PatchingError(f"QuickAutoClean falló para {path.name} (exit {result.exit_code}).")
                    cleaned.append(path.name)
        except LockAcquisitionError as exc:
            logger.warning("Lock contention on '%s': %s", XEDIT_CLEAN_RESOURCE_ID, exc)
            detail = f"No se pudo adquirir el lock '{XEDIT_CLEAN_RESOURCE_ID}': {exc}"
            return {"status": "error", "success": False, "message": detail, "logs": detail}
        except (PatchingError, XEditError) as exc:
            # __aexit__ ya restauró los snapshots (rollback de todos los masters).
            logger.error("QuickAutoClean falló; rollback aplicado: %s", exc)
            return {
                "status": "error",
                "success": False,
                "message": str(exc),
                "cleaned": [],
                "rolled_back": True,
                "logs": str(exc),
            }

        logger.info("QuickAutoClean exitoso: %s", cleaned)
        return _attach_preflight(
            {"status": "success", "success": True, "message": "", "cleaned": cleaned},
            preflight_report,
        )

    # ------------------------------------------------------------------
    # Dry-run / preview (plan-only)
    # ------------------------------------------------------------------

    async def _preview_patch(
        self,
        report: ConflictReport,
        target_plugin: pathlib.Path,
    ) -> dict[str, Any]:
        """Plan-only dry-run: describe the patch that WOULD be generated.

        The mutating xEdit script is never executed (matrix: xEdit patch is
        plan-only), so nothing is touched on disk and no journal transaction is
        opened.  The preview is built purely from the already-computed,
        read-only ``ConflictReport`` and therefore does NOT require the xEdit
        binary — a real patch would fail when the executable is missing, a
        preview must not.
        """
        # Local import to avoid an import-time cycle (local.tools -> orchestrator).
        from sky_claw.antigravity.orchestrator.preview.manifest import (
            ConflictPair,
            ConflictPreview,
            StageChangeSet,
        )

        critical_pairs = [
            ConflictPair(
                winner=conflict.winner,
                losers=list(conflict.losers),
                record_type=conflict.record_type,
                form_id=conflict.form_id,
            )
            for pair in report.plugin_pairs
            for conflict in pair.conflicts
            if conflict.severity == "critical"
        ]
        has_critical = report.critical_conflicts > 0
        proposed = "execute_xedit_script" if has_critical else "create_merged_patch"
        would_output = "SkyClaw_CriticalPatch.esp" if has_critical else "SkyClaw_MergedPatch.esp"

        conflicts = ConflictPreview(
            target_plugin=target_plugin.name,
            total_conflicts=report.total_conflicts,
            critical=report.critical_conflicts,
            minor=max(0, report.total_conflicts - report.critical_conflicts),
            pairs=critical_pairs,
            proposed_resolution=proposed,
        )
        change_set = StageChangeSet(
            stage="xedit",
            executed_for_real=False,
            files_touched=[would_output],
            conflicts=conflicts,
            summary=(
                f"Would generate {would_output} resolving {report.total_conflicts} "
                f"conflict(s) ({report.critical_conflicts} critical) — xEdit not run."
            ),
        )
        logger.info("xEdit dry-run preview: %s", change_set.summary)
        return {
            "status": "dry_run_preview",
            "message": change_set.summary,
            "change_set": change_set.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_to_dict(result: PatchResult) -> dict[str, Any]:
        """Convierte PatchResult a dict serializable.

        Añade el ``message`` canónico del contrato compartido (deuda #5): vacío
        en éxito, y el detalle de ``error`` en fallo.
        """
        raw = dataclasses.asdict(result)
        if raw.get("output_path") is not None:
            raw["output_path"] = str(raw["output_path"])
        if raw.get("strategy_type") is not None:
            raw["strategy_type"] = result.strategy_type.value  # type: ignore[union-attr]
        raw["message"] = "" if raw.get("success") else str(raw.get("error") or "")
        return raw

    @staticmethod
    def _error_dict(message: str) -> dict[str, Any]:
        """Construye un dict de error para retornos tempranos."""
        return {
            "success": False,
            "message": message,
            "output_path": None,
            "records_patched": 0,
            "conflicts_resolved": 0,
            "xedit_exit_code": -1,
            "warnings": [],
            "error": message,
        }
