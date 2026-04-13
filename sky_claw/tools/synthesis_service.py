"""SynthesisPipelineService — servicio dedicado para el pipeline de Synthesis.

Extraído de ``supervisor.py`` como parte del Sprint 2 (Strangler Fig).
Reemplaza el manejo manual de snapshots/rollback con
:class:`SnapshotTransactionLock` para atomicidad y seguridad concurrente.

Diseño (LÓGICA, ARQUITECTURA, PREVENCIÓN):

LÓGICA:
    El pipeline de Synthesis genera un ESP (``Synthesis.esp``) combinando
    múltiples patchers. Si la ejecución falla o el ESP resultante está
    corrupto, se restaura automáticamente el snapshot previo.

ARQUITECTURA:
    Todas las dependencias se inyectan vía constructor (DI). El servicio
    no posee infraestructura propia — sólo coordina.

PREVENCIÓN:
    Las excepciones de dominio (``SynthesisExecutionError``,
    ``SynthesisValidationError``) burbujean hacia el ``__aexit__`` del
    ``SnapshotTransactionLock`` para activar rollback automático.
    No se captura ``Exception`` genérica dentro del context manager.
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
    SynthesisPipelineStartedPayload,
    SynthesisPipelineCompletedPayload,
)
from sky_claw.core.path_resolver import PathResolutionService
from sky_claw.db.journal import OperationJournal
from sky_claw.db.locks import DistributedLockManager, SnapshotTransactionLock
from sky_claw.db.snapshot_manager import FileSnapshotManager
from sky_claw.tools.patcher_pipeline import PatcherPipeline
from sky_claw.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisExecutionError,
    SynthesisResult,
    SynthesisRunner,
    SynthesisValidationError,
)

logger = logging.getLogger("SkyClaw.SynthesisService")

# Directorio de staging de backups (compartido con supervisor)
_BACKUP_STAGING_DIR = ".skyclaw_backups/"


class SynthesisPipelineService:
    """Servicio dedicado para la ejecución del pipeline de Synthesis.

    Coordina ``SynthesisRunner``, ``PatcherPipeline``,
    ``SnapshotTransactionLock`` y ``CoreEventBus`` para ejecutar
    el pipeline con protección transaccional y observabilidad.
    """

    RESOURCE_ID: str = "Synthesis.esp"
    AGENT_ID: str = "synthesis-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal,
        path_resolver: PathResolutionService,
        event_bus: CoreEventBus,
        pipeline_config_path: pathlib.Path | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._path_resolver = path_resolver
        self._event_bus = event_bus
        self._pipeline_config_path = pipeline_config_path or (
            pathlib.Path(_BACKUP_STAGING_DIR) / "synthesis_pipeline.json"
        )

        # Lazy init — paths may not be available at construction time
        self._synthesis_runner: SynthesisRunner | None = None
        self._patcher_pipeline: PatcherPipeline | None = None

    # ------------------------------------------------------------------
    # Lazy initialization
    # ------------------------------------------------------------------

    def _ensure_synthesis_runner(self) -> SynthesisRunner:
        """Inicializa lazily el SynthesisRunner validando paths del entorno.

        Returns:
            SynthesisRunner inicializado.

        Raises:
            SynthesisExecutionError: Si las variables de entorno son inválidas.
        """
        if self._synthesis_runner is not None:
            return self._synthesis_runner

        game_path_str = os.environ.get("SKYRIM_PATH", "")
        mo2_path_str = os.environ.get("MO2_PATH", "")
        synthesis_exe_str = os.environ.get("SYNTHESIS_EXE", "")

        game_path = self._path_resolver.validate_env_path(game_path_str, "SKYRIM_PATH")
        mo2_path = self._path_resolver.validate_env_path(mo2_path_str, "MO2_PATH")
        synthesis_exe = self._path_resolver.validate_env_path(
            synthesis_exe_str, "SYNTHESIS_EXE"
        )

        if not game_path or not mo2_path or not synthesis_exe:
            raise SynthesisExecutionError(
                "Cannot initialize SynthesisRunner: "
                "SKYRIM_PATH, MO2_PATH, and SYNTHESIS_EXE environment variables "
                "must be valid paths"
            )

        if not synthesis_exe.exists():
            raise SynthesisExecutionError(
                f"Synthesis executable not found: {synthesis_exe}"
            )

        output_path = mo2_path / "overwrite"
        if not output_path.exists():
            output_path = mo2_path / "mods" / "Synthesis Output"

        config = SynthesisConfig(
            game_path=game_path,
            mo2_path=mo2_path,
            output_path=output_path,
            synthesis_exe=synthesis_exe,
            timeout_seconds=300,
        )

        self._synthesis_runner = SynthesisRunner(config)
        logger.info(
            "SynthesisRunner inicializado: game=%s, output=%s",
            game_path,
            output_path,
        )
        return self._synthesis_runner

    def _ensure_patcher_pipeline(self) -> PatcherPipeline:
        """Inicializa lazily el PatcherPipeline.

        Returns:
            PatcherPipeline inicializado.
        """
        if self._patcher_pipeline is not None:
            return self._patcher_pipeline

        if self._pipeline_config_path.exists():
            self._patcher_pipeline = PatcherPipeline.from_json(
                self._pipeline_config_path
            )
            logger.info(
                "PatcherPipeline cargado desde %s: %d patchers",
                self._pipeline_config_path,
                len(self._patcher_pipeline),
            )
        else:
            self._patcher_pipeline = PatcherPipeline(
                pipeline_config_path=self._pipeline_config_path
            )
            logger.info("PatcherPipeline inicializado vacío")

        return self._patcher_pipeline

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------

    async def execute_pipeline(
        self,
        patcher_ids: list[str] | None = None,
        create_snapshot: bool = True,
    ) -> dict[str, Any]:
        """Ejecuta el pipeline de Synthesis con protección transaccional.

        Usa ``SnapshotTransactionLock`` para garantizar atomicidad:
        si la ejecución falla o el ESP resultante está corrupto, el
        snapshot se restaura automáticamente.

        Args:
            patcher_ids: IDs de patchers a ejecutar. Si es ``None``,
                         usa los patchers habilitados del pipeline.
            create_snapshot: Si ``True``, crea snapshot antes de ejecutar.

        Returns:
            Diccionario con los campos de ``SynthesisResult``.
        """
        t0 = time.monotonic()

        # --- Early init ---
        try:
            runner = self._ensure_synthesis_runner()
            pipeline = self._ensure_patcher_pipeline()
        except SynthesisExecutionError as exc:
            logger.error("Error inicializando Synthesis: %s", exc)
            return self._error_dict(str(exc))

        if patcher_ids is None:
            enabled = pipeline.get_enabled_patchers()
            patcher_ids = [p.patcher_id for p in enabled]

        if not patcher_ids:
            logger.warning("No hay patchers para ejecutar")
            return self._error_dict("No patchers configured or enabled")

        target_esp = runner._config.output_path / "Synthesis.esp"

        # --- Publish started event ---
        started_payload = SynthesisPipelineStartedPayload(
            patcher_ids=tuple(patcher_ids),
            target_esp=str(target_esp),
            snapshot_enabled=create_snapshot,
        )
        await self._event_bus.publish(
            Event(
                topic="synthesis.pipeline.started",
                payload=started_payload.to_log_dict(),
                source=self.AGENT_ID,
            )
        )

        # --- Journal transaction ---
        tx_id = await self._journal.begin_transaction(
            description="synthesis_pipeline",
            agent_id=self.AGENT_ID,
        )

        # --- Transactional execution ---
        target_files: list[pathlib.Path] = []
        if create_snapshot and target_esp.exists():
            target_files = [target_esp]

        result: SynthesisResult
        rolled_back = False

        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=self.RESOURCE_ID,
                agent_id=self.AGENT_ID,
                target_files=target_files,
                metadata={"source": "synthesis_pipeline", "patchers": patcher_ids},
            ):
                result = await runner.run_pipeline(patcher_ids)

                # Validar ESP DENTRO del context manager para activar rollback
                if result.success and result.output_esp:
                    is_valid = await runner.validate_synthesis_esp(result.output_esp)
                    if not is_valid:
                        raise SynthesisValidationError(
                            "ESP validation failed: corrupted or invalid",
                            esp_path=result.output_esp,
                        )

                # Pipeline failure → raise para activar rollback
                if not result.success:
                    raise SynthesisExecutionError(
                        "; ".join(result.errors) if result.errors else "Pipeline failed",
                        return_code=result.return_code,
                        stderr=result.stderr,
                    )

            # Normal exit — commit journal
            await self._journal.commit_transaction(tx_id)

        except (SynthesisExecutionError, SynthesisValidationError) as exc:
            # __aexit__ ya restauró los snapshots
            rolled_back = bool(target_files)
            await self._journal.mark_transaction_rolled_back(tx_id)
            logger.error("Pipeline Synthesis falló: %s", exc)
            result = SynthesisResult(
                success=False,
                output_esp=target_esp if target_esp.exists() else None,
                return_code=getattr(exc, "return_code", None) or -1,
                stdout="",
                stderr=str(exc),
                patchers_executed=[],
                errors=[str(exc)],
            )

        duration = time.monotonic() - t0

        # --- Publish completed event ---
        completed_payload = SynthesisPipelineCompletedPayload(
            patcher_ids=tuple(patcher_ids),
            target_esp=str(target_esp),
            success=result.success,
            patchers_executed=tuple(result.patchers_executed),
            errors=tuple(result.errors),
            duration_seconds=round(duration, 3),
            rolled_back=rolled_back,
        )
        await self._event_bus.publish(
            Event(
                topic="synthesis.pipeline.completed",
                payload=completed_payload.to_log_dict(),
                source=self.AGENT_ID,
            )
        )

        if result.success:
            logger.info(
                "Pipeline Synthesis exitoso: %s (%d patchers, %.1fs)",
                result.output_esp,
                len(result.patchers_executed),
                duration,
            )

        return self._result_to_dict(result)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_to_dict(result: SynthesisResult) -> dict[str, Any]:
        """Convierte SynthesisResult a dict serializable."""
        raw = dataclasses.asdict(result)
        # Path → str para serialización
        if raw.get("output_esp") is not None:
            raw["output_esp"] = str(raw["output_esp"])
        return raw

    @staticmethod
    def _error_dict(message: str) -> dict[str, Any]:
        """Construye un dict de error para retornos tempranos."""
        return {
            "success": False,
            "output_esp": None,
            "return_code": -1,
            "stdout": "",
            "stderr": message,
            "patchers_executed": [],
            "errors": [message],
        }
