"""DynDOLODPipelineService — servicio transaccional para generación de LODs.

Extrae la lógica de ``execute_dyndolod_pipeline`` desde ``supervisor.py``
hacia un servicio con inyección de dependencias, locking multi-recurso
y eventos de ciclo de vida.

Sprint 2, Fase 3: Strangler Fig — desacoplamiento de ``supervisor.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import pathlib
import time
from typing import Any

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.antigravity.core.path_resolver import PathResolutionService
from sky_claw.antigravity.db.journal import OperationJournal
from sky_claw.antigravity.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools._dir_rollback import DirectoryRollback
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODExecutionError,
    DynDOLODPipelineResult,
    DynDOLODRunner,
    DynDOLODTimeoutError,
)

logger = logging.getLogger("SkyClaw.DynDOLODPipelineService")


class DynDOLODPipelineService:
    """Servicio transaccional para el pipeline DynDOLOD (TexGen + DynDOLOD).

    Encapsula la lógica de ejecución con:
    - Locking multi-recurso vía :class:`SnapshotTransactionLock`
    - Snapshots automáticos para rollback
    - Registro de operaciones en :class:`OperationJournal`
    - Eventos de ciclo de vida en :class:`CoreEventBus`

    Args:
        lock_manager: Gestor de locks distribuidos.
        snapshot_manager: Gestor de snapshots de archivos.
        journal: Journal de operaciones para trazabilidad.
        path_resolver: Servicio de resolución de rutas validadas.
        event_bus: Bus de eventos para publicación de ciclo de vida.
    """

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

        # Lazy init — runner requiere env vars que pueden no existir aún.
        self._runner: DynDOLODRunner | None = None

    # ------------------------------------------------------------------
    # Lazy initialization
    # ------------------------------------------------------------------

    def _ensure_runner(self) -> DynDOLODRunner:
        """Inicializa el :class:`DynDOLODRunner` bajo demanda.

        Variables de entorno requeridas:
        - ``DYNDLOD_EXE``: Ruta a DynDOLODx64.exe
        - ``TEXGEN_EXE``: Ruta a TexGenx64.exe (opcional)
        - ``SKYRIM_PATH``: Ruta al directorio de Skyrim SE/AE
        - ``MO2_PATH``: Ruta al directorio de MO2
        - ``MO2_MODS_PATH``: Ruta a la carpeta mods de MO2

        Returns:
            DynDOLODRunner inicializado.

        Raises:
            DynDOLODExecutionError: Si faltan variables de entorno requeridas.
        """
        if self._runner is not None:
            return self._runner

        game_path = self._path_resolver.get_skyrim_path()
        mo2_path = self._path_resolver.get_mo2_path()
        mo2_mods_path = self._path_resolver.get_mo2_mods_path()
        dyndolod_exe = self._path_resolver.get_dyndolod_exe()
        texgen_exe = self._path_resolver.get_texgen_exe()

        if not game_path or not mo2_path or not mo2_mods_path or not dyndolod_exe:
            raise DynDOLODExecutionError(
                "Cannot initialize DynDOLODRunner: "
                "SKYRIM_PATH, MO2_PATH, MO2_MODS_PATH, and DYNDLOD_EXE "
                "environment variables must be valid paths"
            )

        if not dyndolod_exe.exists():
            raise DynDOLODExecutionError(f"DynDOLOD executable not found: {dyndolod_exe}")

        config = DynDOLODConfig(
            game_path=game_path,
            mo2_path=mo2_path,
            mo2_mods_path=mo2_mods_path,
            dyndolod_exe=dyndolod_exe,
            texgen_exe=texgen_exe,
        )

        self._runner = DynDOLODRunner(config)
        logger.info(
            "DynDOLODRunner inicializado: game=%s, dyndolod=%s",
            game_path,
            dyndolod_exe,
        )
        return self._runner

    # ------------------------------------------------------------------
    # Pipeline principal
    # ------------------------------------------------------------------

    async def execute(
        self,
        preset: str = "Medium",
        run_texgen: bool = True,
        create_snapshot: bool = True,
        texgen_args: list[str] | None = None,
        dyndolod_args: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Ejecuta el pipeline completo de generación de LODs.

        Flujo transaccional:
        1. Publicar evento ``pipeline.dyndolod.started``
        2. Adquirir lock + snapshot vía :class:`SnapshotTransactionLock`
        3. Comenzar transacción en journal
        4. Ejecutar TexGen (si ``run_texgen=True``) → DynDOLOD
        5. Validar salida de DynDOLOD
        6. Commit en journal y publicar evento ``pipeline.dyndolod.completed``

        Args:
            preset: Nivel de calidad (Low, Medium, High).
            run_texgen: Si True, ejecuta TexGen antes de DynDOLOD.
            create_snapshot: Si True, crea snapshot para rollback.
            texgen_args: Argumentos adicionales para TexGen.
            dyndolod_args: Argumentos adicionales para DynDOLOD.
            dry_run: Si True, NO ejecuta TexGen/DynDOLOD; devuelve una estimación
                plan-only (``status="dry_run_preview"`` + ``change_set``) sin
                lock, journal ni eventos. DynDOLOD es la etapa más cara (GBs,
                30+ min), por eso el preview nunca la ejecuta.

        Returns:
            Diccionario con resultado del pipeline, o el preview plan-only
            cuando ``dry_run=True``.
        """
        if dry_run:
            return await self._preview(preset=preset, run_texgen=run_texgen)

        start_time = time.monotonic()
        rolled_back = False
        tx_id: int | None = None

        logger.info(
            "Iniciando pipeline DynDOLOD: preset=%s, texgen=%s, snapshot=%s",
            preset,
            run_texgen,
            create_snapshot,
        )

        # 1. Publicar evento de inicio
        await self._publish_started(preset=preset, run_texgen=run_texgen)

        # 2. Inicializar runner
        try:
            runner = self._ensure_runner()
        except DynDOLODExecutionError as exc:
            logger.error("Error inicializando DynDOLOD: %s", exc)
            duration = time.monotonic() - start_time
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=False,
            )
            return {
                "success": False,
                "message": str(exc),
                "errors": [str(exc)],
                "duration_seconds": duration,
            }

        # DD-1: Directorios regenerados a proteger con rollback move-aside.
        # El backend de snapshots es copy-based/solo-archivos y ``Output/`` puede
        # pesar varios GB; renombrar el dir aparte es O(1) y da rollback real
        # byte-a-byte (evita que un fallo deje texturas/meshes parciales). El
        # move-aside del dir subsume el ``.esp``, así que el lock transaccional ya
        # no snapshotea archivos (``target_files=[]``).
        mods_path = runner._config.mo2_mods_path
        rollback_dirs: list[pathlib.Path] = []
        if create_snapshot:
            rollback_dirs.append(mods_path / runner.DYNDOLLOD_MOD_NAME)
            if run_texgen:
                rollback_dirs.append(mods_path / runner.TEXGEN_MOD_NAME)

        # 3. Ejecutar bajo lock transaccional + rollback de directorios.
        # AsyncExitStack: el lock se adquiere primero y se libera último; los
        # DirectoryRollback se restauran ANTES de soltar el lock.
        try:
            async with contextlib.AsyncExitStack() as tx_stack:
                await tx_stack.enter_async_context(
                    SnapshotTransactionLock(
                        lock_manager=self._lock_manager,
                        snapshot_manager=self._snapshot_manager,
                        resource_id="dyndolod-pipeline",
                        agent_id="dyndolod-pipeline-service",
                        target_files=[],
                        metadata={"preset": preset, "run_texgen": run_texgen},
                    )
                )
                for output_dir in rollback_dirs:
                    await tx_stack.enter_async_context(DirectoryRollback(output_dir))

                # Comenzar transacción en journal DENTRO del lock
                tx_id = await self._journal.begin_transaction(
                    description=f"DynDOLOD pipeline (preset={preset}, texgen={run_texgen})",
                    agent_id="dyndolod-pipeline-service",
                )

                # Ejecutar pipeline
                result = await runner.run_full_pipeline(
                    run_texgen=run_texgen,
                    preset=preset,
                    texgen_args=texgen_args,
                    dyndolod_args=dyndolod_args,
                )

                # Validar salida de DynDOLOD si fue exitoso
                if result.success and result.dyndolod_result and result.dyndolod_result.output_path:
                    is_valid = await runner.validate_dyndolod_output(result.dyndolod_result.output_path)
                    if not is_valid:
                        msg = "DynDOLOD output validation failed"
                        logger.error(msg)
                        raise DynDOLODExecutionError(msg)

                if not result.success:
                    errors_str = "; ".join(result.errors) if result.errors else "Unknown error"
                    raise DynDOLODExecutionError(f"DynDOLOD pipeline failed: {errors_str}")

                # Commit en journal
                await self._journal.commit_transaction(tx_id)

                duration = time.monotonic() - start_time
                await self._log_result(result, preset, success=True)
                texgen_ok = result.texgen_result.success if result.texgen_result else False
                dyndolod_ok = result.dyndolod_result.success if result.dyndolod_result else False
                await self._publish_completed(
                    preset=preset,
                    run_texgen=run_texgen,
                    success=True,
                    texgen_success=texgen_ok,
                    dyndolod_success=dyndolod_ok,
                    errors=(),
                    duration_seconds=duration,
                    rolled_back=False,
                )

                logger.info(
                    "Pipeline DynDOLOD exitoso: texgen=%s, dyndolod=%s (%.1fs)",
                    result.texgen_mod_path,
                    result.dyndolod_mod_path,
                    duration,
                )

                # Normalizar pathlib.Path → str de forma recursiva para compatibilidad JSON/WS.
                def normalize_for_serialization(obj: Any) -> Any:
                    if isinstance(obj, pathlib.Path):
                        return str(obj)
                    if isinstance(obj, dict):
                        return {k: normalize_for_serialization(v) for k, v in obj.items()}
                    if isinstance(obj, list):
                        return [normalize_for_serialization(v) for v in obj]
                    return obj

                result_dict = normalize_for_serialization(dataclasses.asdict(result))

                return {
                    "success": True,
                    "message": "",
                    **result_dict,
                    "duration_seconds": duration,
                }

        except LockAcquisitionError as exc:
            duration = time.monotonic() - start_time
            logger.error("No se pudo adquirir lock para DynDOLOD pipeline: %s", exc)
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(f"Lock acquisition failed: {exc}",),
                duration_seconds=duration,
                rolled_back=False,
            )
            return {
                "success": False,
                "message": f"Lock acquisition failed: {exc}",
                "errors": [f"Lock acquisition failed: {exc}"],
                "duration_seconds": duration,
            }

        except (DynDOLODExecutionError, DynDOLODTimeoutError) as exc:
            rolled_back = True
            duration = time.monotonic() - start_time
            logger.error("DynDOLOD pipeline domain error: %s", exc)

            if tx_id is not None:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as journal_exc:
                    logger.error(
                        "Failed to mark TX %d as rolled back: %s",
                        tx_id,
                        journal_exc,
                        exc_info=True,
                    )

            await self._log_result_error(preset, str(exc))
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=rolled_back,
            )
            return {
                "success": False,
                "message": str(exc),
                "errors": [str(exc)],
                "duration_seconds": duration,
                "rolled_back": rolled_back,
            }

        except asyncio.CancelledError:
            # Cancelación de task — hacer cleanup mínimo y re-lanzar.
            duration = time.monotonic() - start_time
            logger.warning("DynDOLOD pipeline cancelled after %.1fs", duration)
            if tx_id is not None:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as journal_exc:
                    # Aislar el fallo secundario para no enmascarar la
                    # cancelación; traceback al log como sus handlers hermanos.
                    logger.error(
                        "Failed to mark TX %d as rolled back on cancel: %s",
                        tx_id,
                        journal_exc,
                        exc_info=True,
                    )
            raise

        except Exception as exc:
            # PREVENCIÓN T11: Red de seguridad final — NUNCA dejar TX en PENDING
            rolled_back = True
            duration = time.monotonic() - start_time
            logger.error(
                "Unexpected error in DynDOLOD pipeline: %s",
                exc,
                exc_info=True,
            )

            if tx_id is not None:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as journal_exc:
                    logger.error(
                        "Failed to mark TX %d as rolled back after unexpected error: %s",
                        tx_id,
                        journal_exc,
                        exc_info=True,
                    )

            await self._log_result_error(preset, str(exc))
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=rolled_back,
            )
            return {
                "success": False,
                "message": str(exc),
                "errors": [str(exc)],
                "duration_seconds": duration,
                "rolled_back": rolled_back,
            }

    # ------------------------------------------------------------------
    # Dry-run / preview (plan-only estimate)
    # ------------------------------------------------------------------

    async def _preview(self, *, preset: str, run_texgen: bool) -> dict[str, Any]:
        """Plan-only dry-run: estimate the LODs DynDOLOD WOULD generate.

        The TexGen/DynDOLOD executables are never launched (matrix: DynDOLOD is
        plan-only — it is the most expensive stage), so nothing is locked,
        journaled, or written.  Output directories are derived from the path
        resolver alone, so no DynDOLOD binary is required to preview.
        """
        # Local import to avoid an import-time cycle (local.tools -> orchestrator).
        from sky_claw.antigravity.orchestrator.preview.manifest import LODPlan, StageChangeSet

        mo2_mods_path = self._path_resolver.get_mo2_mods_path()
        dyndolod_dir = str(mo2_mods_path / "DynDOLOD Output") if mo2_mods_path else "DynDOLOD Output"

        would_generate = ["DynDOLOD.esp"]
        output_dirs = [dyndolod_dir]
        if run_texgen:
            texgen_dir = str(mo2_mods_path / "TexGen Output") if mo2_mods_path else "TexGen Output"
            would_generate.append("TexGen textures")
            output_dirs.append(texgen_dir)

        lod_plan = LODPlan(
            preset=preset,
            would_generate=would_generate,
            # The exact asset count is unknowable without running DynDOLOD; the
            # estimate is intentionally 0 and flagged as such in the warnings.
            estimated_assets=0,
            output_dirs=output_dirs,
        )
        change_set = StageChangeSet(
            stage="dyndolod",
            executed_for_real=False,
            files_touched=output_dirs,
            lod_plan=lod_plan,
            warnings=["LOD asset count is an estimate; TexGen/DynDOLOD are not run in preview."],
            summary=(
                f"Would generate LODs (preset={preset}, texgen={run_texgen}) into {dyndolod_dir} — DynDOLOD not run."
            ),
        )
        logger.info("DynDOLOD dry-run preview: %s", change_set.summary)
        return {
            "status": "dry_run_preview",
            "message": change_set.summary,
            "change_set": change_set.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------
    # Eventos
    # ------------------------------------------------------------------

    async def _publish_started(self, *, preset: str, run_texgen: bool) -> None:
        """Publica evento de inicio del pipeline."""
        payload = DynDOLODPipelineStartedPayload(
            preset=preset,
            run_texgen=run_texgen,
        )
        await self._event_bus.publish(
            Event(
                topic="pipeline.dyndolod.started",
                payload=payload.to_log_dict(),
                source="dyndolod-pipeline-service",
            )
        )

    async def _publish_completed(
        self,
        *,
        preset: str,
        run_texgen: bool,
        success: bool,
        texgen_success: bool,
        dyndolod_success: bool,
        errors: tuple[str, ...],
        duration_seconds: float,
        rolled_back: bool,
    ) -> None:
        """Publica evento de finalización del pipeline."""
        payload = DynDOLODPipelineCompletedPayload(
            preset=preset,
            run_texgen=run_texgen,
            success=success,
            texgen_success=texgen_success,
            dyndolod_success=dyndolod_success,
            errors=errors,
            duration_seconds=duration_seconds,
            rolled_back=rolled_back,
        )
        await self._event_bus.publish(
            Event(
                topic="pipeline.dyndolod.completed",
                payload=payload.to_log_dict(),
                source="dyndolod-pipeline-service",
            )
        )

    # ------------------------------------------------------------------
    # Journal helpers
    # ------------------------------------------------------------------

    async def _log_result(
        self,
        result: DynDOLODPipelineResult,
        preset: str,
        *,
        success: bool,
    ) -> None:
        """Registra el resultado del pipeline mediante logging estructurado.

        El outcome transaccional ya queda persistido por
        ``commit_transaction``/``mark_transaction_rolled_back``; este helper
        añade un log estructurado con detalles del pipeline para observabilidad.
        """
        logger.info(
            "DynDOLOD pipeline result",
            extra={
                "agent_id": "dyndolod-pipeline-service",
                "operation_type": ("dyndolod_pipeline_complete" if success else "dyndolod_pipeline_failed"),
                "file_path": (str(result.dyndolod_mod_path) if result.dyndolod_mod_path else ""),
                "success": success,
                "preset": preset,
                "texgen_success": (result.texgen_result.success if result.texgen_result else False),
                "dyndolod_success": (result.dyndolod_result.success if result.dyndolod_result else False),
                "errors": result.errors,
            },
        )

    async def _log_result_error(self, preset: str, error_msg: str) -> None:
        """Registra un error del pipeline mediante logging estructurado."""
        logger.error(
            "DynDOLOD pipeline failed",
            extra={
                "agent_id": "dyndolod-pipeline-service",
                "operation_type": "dyndolod_pipeline_failed",
                "file_path": "",
                "success": False,
                "preset": preset,
                "error": error_msg,
            },
        )
