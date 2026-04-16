"""DynDOLODPipelineService — servicio transaccional para generación de LODs.

Extrae la lógica de ``execute_dyndolod_pipeline`` desde ``supervisor.py``
hacia un servicio con inyección de dependencias, locking multi-recurso
y eventos de ciclo de vida.

Sprint 2, Fase 3: Strangler Fig — desacoplamiento de ``supervisor.py``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import time
from typing import Any

from sky_claw.core.event_bus import CoreEventBus, Event
from sky_claw.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.core.path_resolver import PathResolutionService
from sky_claw.db.journal import OperationJournal
from sky_claw.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.db.snapshot_manager import FileSnapshotManager
from sky_claw.tools.dyndolod_runner import (
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

        game_path_str = os.environ.get("SKYRIM_PATH", "")
        mo2_path_str = os.environ.get("MO2_PATH", "")
        mo2_mods_path_str = os.environ.get("MO2_MODS_PATH", "")
        dyndolod_exe_str = os.environ.get("DYNDLOD_EXE", "")
        texgen_exe_str = os.environ.get("TEXGEN_EXE", "")

        game_path = self._path_resolver.validate_env_path(game_path_str, "SKYRIM_PATH")
        mo2_path = self._path_resolver.validate_env_path(mo2_path_str, "MO2_PATH")
        mo2_mods_path = self._path_resolver.validate_env_path(
            mo2_mods_path_str, "MO2_MODS_PATH"
        )
        dyndolod_exe = self._path_resolver.validate_env_path(
            dyndolod_exe_str, "DYNDLOD_EXE"
        )
        texgen_exe = (
            self._path_resolver.validate_env_path(texgen_exe_str, "TEXGEN_EXE")
            if texgen_exe_str
            else None
        )

        if not game_path or not mo2_path or not mo2_mods_path or not dyndolod_exe:
            raise DynDOLODExecutionError(
                "Cannot initialize DynDOLODRunner: "
                "SKYRIM_PATH, MO2_PATH, MO2_MODS_PATH, and DYNDLOD_EXE "
                "environment variables must be valid paths"
            )

        if not dyndolod_exe.exists():
            raise DynDOLODExecutionError(
                f"DynDOLOD executable not found: {dyndolod_exe}"
            )

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

        Returns:
            Diccionario con resultado del pipeline.
        """
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
                "errors": [str(exc)],
                "duration_seconds": duration,
            }

        # Determinar archivos a proteger con snapshot
        dyndolod_output_path = runner._config.mo2_mods_path / "DynDOLOD Output"
        dyndolod_esp = dyndolod_output_path / "DynDOLOD.esp"

        target_files: list[pathlib.Path] = []
        if create_snapshot:
            if dyndolod_esp.exists():
                target_files.append(dyndolod_esp)
            if dyndolod_output_path.exists() and dyndolod_output_path.is_dir():
                target_files.append(dyndolod_output_path)

        # 3. Ejecutar bajo lock transaccional
        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id="dyndolod-pipeline",
                agent_id="dyndolod-pipeline-service",
                target_files=target_files,
                metadata={"preset": preset, "run_texgen": run_texgen},
            ):
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
                if (
                    result.success
                    and result.dyndolod_result
                    and result.dyndolod_result.output_path
                ):
                    is_valid = await runner.validate_dyndolod_output(
                        result.dyndolod_result.output_path
                    )
                    if not is_valid:
                        msg = "DynDOLOD output validation failed"
                        logger.error(msg)
                        raise DynDOLODExecutionError(msg)

                if not result.success:
                    errors_str = (
                        "; ".join(result.errors) if result.errors else "Unknown error"
                    )
                    raise DynDOLODExecutionError(
                        f"DynDOLOD pipeline failed: {errors_str}"
                    )

                # Commit en journal
                await self._journal.commit_transaction(tx_id)

                duration = time.monotonic() - start_time
                await self._log_result(result, preset, success=True)
                texgen_ok = (
                    result.texgen_result.success if result.texgen_result else False
                )
                dyndolod_ok = (
                    result.dyndolod_result.success if result.dyndolod_result else False
                )
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

                return {
                    "success": True,
                    **dataclasses.asdict(result),
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
                "errors": [str(exc)],
                "duration_seconds": duration,
                "rolled_back": rolled_back,
            }

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
                "errors": [str(exc)],
                "duration_seconds": duration,
                "rolled_back": rolled_back,
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
                "operation_type": (
                    "dyndolod_pipeline_complete"
                    if success
                    else "dyndolod_pipeline_failed"
                ),
                "file_path": (
                    str(result.dyndolod_mod_path) if result.dyndolod_mod_path else ""
                ),
                "success": success,
                "preset": preset,
                "texgen_success": (
                    result.texgen_result.success if result.texgen_result else False
                ),
                "dyndolod_success": (
                    result.dyndolod_result.success if result.dyndolod_result else False
                ),
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
