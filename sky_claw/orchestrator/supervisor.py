import asyncio
import logging
import os
import pathlib
from typing import Any

from sky_claw.assets import AssetConflictDetector, AssetConflictReport
from sky_claw.comms.interface import InterfaceAgent
from sky_claw.core.database import DatabaseAgent
from sky_claw.core.event_bus import CoreEventBus, Event
from sky_claw.core.models import HitlApprovalRequest, LootExecutionParams
from sky_claw.core.path_resolver import PathResolutionService
from sky_claw.core.schemas import ScrapingQuery
from sky_claw.core.windows_interop import ModdingToolsAgent
from sky_claw.db.journal import OperationJournal
from sky_claw.db.locks import DistributedLockManager
from sky_claw.db.rollback_manager import RollbackManager
from sky_claw.db.snapshot_manager import FileSnapshotManager
from sky_claw.orchestrator.maintenance_daemon import (
    MaintenanceDaemon,
    get_max_backup_size_mb,
)
from sky_claw.orchestrator.state_graph import create_supervisor_state_graph
from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon
from sky_claw.orchestrator.watcher_daemon import WatcherDaemon
from sky_claw.orchestrator.ws_event_streamer import LangGraphEventStreamer
from sky_claw.scraper.scraper_agent import ScraperAgent
from sky_claw.security.path_validator import PathValidator
from sky_claw.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.tools.synthesis_service import SynthesisPipelineService
from sky_claw.tools.wrye_bash_runner import (
    WryeBashConfig,
    WryeBashExecutionError,
    WryeBashRunner,
)
from sky_claw.tools.xedit_service import XEditPipelineService
from sky_claw.xedit.conflict_analyzer import ConflictAnalyzer, ConflictReport

logger = logging.getLogger(__name__)
security_logger = logging.getLogger(f"{__name__}.security")

# FASE 1.5: Constante para directorio de staging de backups
BACKUP_STAGING_DIR = ".skyclaw_backups/"


class SupervisorAgent:
    def __init__(self, profile_name: str = "Default"):
        self.db = DatabaseAgent()
        self.scraper = ScraperAgent(self.db)
        self.tools = ModdingToolsAgent()
        self.interface = InterfaceAgent()
        self.profile_name = profile_name
        self.state_graph = create_supervisor_state_graph(profile_name=self.profile_name)
        self.event_streamer = LangGraphEventStreamer(self.state_graph, self.interface)

        # FASE 1.5: Inicializar componentes de rollback (también inicializa _path_validator)
        self._init_rollback_components()

        # Sprint-1.5: PathResolutionService — resolución stateless de rutas
        self._path_resolver = PathResolutionService(
            path_validator=self._path_validator,
            profile_name=self.profile_name,
        )

        # Resolver ruta de modlist: MO2_PATH env var > auto-detección > fallback WSL2
        self.modlist_path = str(self._path_resolver.resolve_modlist_path(self.profile_name))

        # Sprint-1: Core event bus (instanciable, no singleton)
        self._event_bus = CoreEventBus()
        # ARC-01: Demonios extraídos del Supervisor
        self._maintenance_daemon = MaintenanceDaemon(
            snapshot_manager=self.snapshot_manager,
        )
        self._telemetry_daemon = TelemetryDaemon(
            event_bus=self._event_bus,
        )
        self._watcher_daemon = WatcherDaemon(
            modlist_path=self.modlist_path,
            profile_name=self.profile_name,
            db=self.db,
            event_bus=self._event_bus,
        )

        # Sprint-2: Inicializar Servicios Extraídos (Strangler Fig)
        self._synthesis_service = SynthesisPipelineService(
            lock_manager=self._lock_manager,
            snapshot_manager=self.snapshot_manager,
            journal=self.journal,
            path_resolver=self._path_resolver,
            event_bus=self._event_bus,
            pipeline_config_path=pathlib.Path(BACKUP_STAGING_DIR) / "synthesis_pipeline.json",
        )

        self._dyndolod_service = DynDOLODPipelineService(
            lock_manager=self._lock_manager,
            snapshot_manager=self.snapshot_manager,
            journal=self.journal,
            path_resolver=self._path_resolver,
            event_bus=self._event_bus,
        )

        self._xedit_service = XEditPipelineService(
            lock_manager=self._lock_manager,
            snapshot_manager=self.snapshot_manager,
            path_resolver=self._path_resolver,
            event_bus=self._event_bus,
        )

        # Lazy init para runners legacy que aún no son servicios puros (WryeBash, AssetDetector)
        self._asset_detector: AssetConflictDetector | None = None
        self._wrye_bash_runner: WryeBashRunner | None = None

    def _init_rollback_components(self) -> None:
        """FASE 1.5: Inicializa los componentes de resiliencia para rollback.

        Crea las instancias de:
        - OperationJournal: Para registrar operaciones de archivos
        - FileSnapshotManager: Para capturar snapshots antes de modificaciones
        - RollbackManager: Para coordinar la reversión de operaciones
        """
        # Directorio de backups relativo al VFS
        backup_dir = pathlib.Path(BACKUP_STAGING_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Inicializar componentes
        self.journal = OperationJournal(db_path=backup_dir / "journal.db")
        self.snapshot_manager = FileSnapshotManager(
            snapshot_dir=backup_dir / "snapshots", max_size_mb=get_max_backup_size_mb()
        )

        # El orquestador y vfs son delegados al xedit_service
        self.rollback_manager = RollbackManager(
            db=self.db,
            snapshot_manager=self.snapshot_manager,
            orchestrator=self._xedit_service._orchestrator,
            vfs=self._xedit_service._orchestrator.vfs,
        )

        # Sprint-2: DistributedLockManager para bloqueos concurrentes
        self._lock_manager = DistributedLockManager(
            db_path=backup_dir / "locks.db",
        )

        # CRIT-003: PathValidator para validación de variables de entorno
        self._path_validator = PathValidator(roots=[backup_dir])

        logger.info(
            "Componentes de rollback inicializados: backup_dir=%s, max_size_mb=%d",
            backup_dir,
            get_max_backup_size_mb(),
        )

    async def start(self) -> None:
        await self.db.init_db()
        # FASE 1.5: Abrir journal de operaciones
        await self.journal.open()
        await self.snapshot_manager.initialize()
        # Sprint-2: Inicializar lock manager (requiere async init)
        await self._lock_manager.initialize()

        # Vincular con la señal de ejecución de la interfaz
        self.interface.register_command_callback(self.handle_execution_signal)

        logger.info("SupervisorAgent inicializado: IPC y Watcher listos. Lanzando TaskGroup de fondo...")

        # Sprint-1: Iniciar event bus y suscribir bridge de telemetría
        await self._event_bus.start()
        self._event_bus.subscribe(
            "system.telemetry.*",
            self._bridge_telemetry_to_ws,
        )
        # Sprint-1.5: Suscribir al evento de cambio en modlist
        self._event_bus.subscribe(
            "system.modlist.changed",
            self._trigger_proactive_analysis,
        )
        # ARC-01: Iniciar demonios extraídos
        await self._maintenance_daemon.start()
        await self._telemetry_daemon.start()
        await self._watcher_daemon.start()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.interface.connect())
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.error("TaskGroup del Supervisor — sub-error: %s", exc, exc_info=exc)
        finally:
            # ARC-01: Detener demonios extraídos (LIFO)
            await self._watcher_daemon.stop()
            await self._telemetry_daemon.stop()
            await self._maintenance_daemon.stop()
            # Sprint-1: Detener event bus después de los demonios
            await self._event_bus.stop()
            # Sprint-2: Cerrar lock manager
            await self._lock_manager.close()
            # FASE 1.5: Cerrar journal al terminar
            await self.journal.close()
            await self.db.close()

    async def _bridge_telemetry_to_ws(self, event: Event) -> None:
        """Strangler Fig: reenvía eventos del bus al transporte WebSocket legacy."""
        await self.interface.send_event("telemetry", event.payload)

    async def _trigger_proactive_analysis(self, event: Event | None = None) -> None:
        """Maneja eventos de cambio en modlist publicados por WatcherDaemon.

        Suscrito al tópico ``system.modlist.changed`` del CoreEventBus.
        Extrae el payload estructurado para logging y futura lógica de análisis.
        También puede ser invocado manualmente desde la GUI sin evento.

        Args:
            event: Evento con payload :class:`ModlistChangedPayload`, o None
                   cuando se dispara manualmente desde la GUI.
        """
        if event is not None:
            logger.info(
                "Analizando topología del Load Order por cambio detectado — profile=%s, mtime=%.1f->%.1f",
                event.payload.get("profile_name", "unknown"),
                event.payload.get("previous_mtime", 0.0),
                event.payload.get("current_mtime", 0.0),
            )
        else:
            logger.info("Análisis proactivo disparado manualmente desde la GUI (sin evento de bus).")
        # Aquí se inyectaría la llamada real a la herramienta de parsing local.

    async def handle_execution_signal(self, payload: dict[str, object]) -> None:
        """Reacciona a la señal de ignición forzada desde la GUI."""
        logger.info("Ignición forzada desde GUI detectada. Despertando demonio proactivo.")
        await self._trigger_proactive_analysis(Event(topic="system.manual.trigger", payload=payload))

    # FASE 1.5: Worker de pruning pasivo
    async def dispatch_tool(self, tool_name: str, payload_dict: dict[str, Any]) -> dict[str, Any]:
        """
        Enrutador estricto. El LLM devuelve 'tool_name' y 'payload_dict'.
        Se valida con Pydantic inmediatamente.

        Puntos de inyección (Sprints recientes):
        - generate_lods redirige a DynDOLODPipelineService
        - execute_synthesis_pipeline redirige a SynthesisPipelineService
        - resolve_conflict_with_patch redirige a XEditPipelineService
        """
        match tool_name:
            case "query_mod_metadata":
                # Crear ScrapingQuery validado con Pydantic
                query = ScrapingQuery(**payload_dict)
                result = await self.scraper.query_nexus(query)
                # Convertir ModMetadata a dict para compatibilidad con la interfaz
                return result.model_dump()

            case "execute_loot_sorting":
                params = LootExecutionParams(**payload_dict)
                # Requiere HITL si implica cambios destructivos o sobreescritura de metadatos sensibles
                hitl_req = HitlApprovalRequest(
                    action_type="destructive_xedit",  # Reusado conceptualmente
                    reason="Se va a reordenar el Load Order, lo que podría afectar partidas guardadas.",
                    context_data={"profile": params.profile_name},
                )
                decision = await self.interface.request_hitl(hitl_req)

                if decision == "approved":
                    return await self.tools.run_loot(params)
                else:
                    return {
                        "status": "aborted",
                        "reason": "Usuario denegó la operación.",
                    }

            # FASE 3: Synthesis Pipeline (delegado a SynthesisPipelineService)
            case "execute_synthesis_pipeline":
                try:
                    pipeline_result = await self._synthesis_service.execute_pipeline(**payload_dict)
                except Exception as exc:
                    logger.exception("RCA: Falló execute_synthesis_pipeline; se convierte la excepción a error dict.")
                    return {
                        "status": "error",
                        "reason": "SynthesisPipelineExecutionFailed",
                        "details": str(exc),
                    }

                if not isinstance(pipeline_result, dict):
                    logger.error(
                        "RCA: execute_synthesis_pipeline devolvió un tipo inválido: %s",
                        type(pipeline_result).__name__,
                    )
                    return {
                        "status": "error",
                        "reason": "InvalidSynthesisPipelineResult",
                    }

                return pipeline_result

            # Sprint-2 Fase 4: xEdit Patch (delegado a XEditPipelineService)
            case "resolve_conflict_with_patch":
                try:
                    target_plugin = pathlib.Path(payload_dict["target_plugin"])
                    report = ConflictReport(**payload_dict["report"])
                    patch_result = await self._xedit_service.execute_patch(
                        target_plugin=target_plugin,
                        report=report,
                    )
                except Exception as exc:
                    logger.exception("RCA: Falló resolve_conflict_with_patch; se convierte la excepción a error dict.")
                    return {
                        "status": "error",
                        "reason": "XEditPatchExecutionFailed",
                        "details": str(exc),
                    }

                if not isinstance(patch_result, dict):
                    logger.error(
                        "RCA: resolve_conflict_with_patch devolvió un tipo inválido: %s",
                        type(patch_result).__name__,
                    )
                    return {
                        "status": "error",
                        "reason": "InvalidXEditPatchResult",
                    }

                return patch_result

            # FASE 4: DynDOLOD/TexGen Pipeline — delegado a DynDOLODPipelineService
            case "generate_lods":
                return await self._dyndolod_service.execute(**payload_dict)

            # FASE 5: Asset Conflict Detection Integration
            case "scan_asset_conflicts":
                import dataclasses

                conflicts = self.scan_asset_conflicts()
                return {"status": "success", "conflicts": [dataclasses.asdict(c) for c in conflicts]}

            case "scan_asset_conflicts_json":
                return {"status": "success", "json_report": self.scan_asset_conflicts_json()}

            # FASE 6: Wrye Bash Bashed Patch Integration
            case "generate_bashed_patch":
                return await self.execute_wrye_bash_pipeline(**payload_dict)

            # M-04/M-05: Plugin Limit Validation
            case "validate_plugin_limit":
                profile = payload_dict.get("profile", self.profile_name)
                return await self._run_plugin_limit_guard(profile)

            case _:
                logger.error(f"RCA: LLM alucinó la herramienta '{tool_name}'.")
                return {"status": "error", "reason": "ToolNotFound"}

    # FASE 1.5: Método para ejecutar rollback
    async def execute_rollback(self, agent_id: str) -> dict[str, Any]:
        """FASE 1.5: Ejecuta rollback de la última operación de un agente.

        Args:
            agent_id: ID del agente cuya operación debe revertirse

        Returns:
            Resultado del rollback con estado y detalles
        """
        logger.info("Iniciando rollback para agente: %s", agent_id)

        try:
            result = await self.rollback_manager.undo_last_operation(agent_id)

            if result.success:
                logger.info(
                    "Rollback exitoso: %d archivos restaurados, %d eliminados",
                    result.entries_restored,
                    result.files_deleted,
                )
            else:
                logger.error("Rollback falló para agente %s", agent_id)

            return {
                "success": result.success,
                "transaction_id": result.transaction_id,
                "entries_restored": result.entries_restored,
                "files_deleted": result.files_deleted,
                "errors": result.errors,
            }

        except (OSError, RuntimeError) as e:
            logger.exception("Error crítico durante rollback: %s", e)
            return {"success": False, "error": str(e)}

    # FASE 1.5: Obtener RollbackManager para inyección en SyncEngine
    def get_rollback_manager(self) -> RollbackManager:
        """Retorna el RollbackManager para inyección de dependencias."""
        return self.rollback_manager

    # =========================================================================
    # FASE 6: Wrye Bash Integration
    # =========================================================================

    def _ensure_wrye_bash_runner(self) -> WryeBashRunner:
        """FASE 6: Asegura que el WryeBashRunner esté inicializado.

        Variables de entorno requeridas:
        - WRYE_BASH_PATH: Ruta a Wrye Bash (bash.exe o bash.py)
        - SKYRIM_PATH: Ruta al directorio de Skyrim SE/AE
        - MO2_PATH: Ruta al directorio de MO2

        Returns:
            WryeBashRunner inicializado.

        Raises:
            WryeBashExecutionError: Si no se puede inicializar.
        """
        if self._wrye_bash_runner is not None:
            return self._wrye_bash_runner

        game_path_str = os.environ.get("SKYRIM_PATH", "")
        mo2_path_str = os.environ.get("MO2_PATH", "")
        wrye_bash_path_str = os.environ.get("WRYE_BASH_PATH", "")

        game_path = self._path_resolver.validate_env_path(game_path_str, "SKYRIM_PATH")
        mo2_path = self._path_resolver.validate_env_path(mo2_path_str, "MO2_PATH")
        wrye_bash_path = self._path_resolver.validate_env_path(wrye_bash_path_str, "WRYE_BASH_PATH")

        if not game_path or not mo2_path or not wrye_bash_path:
            raise WryeBashExecutionError(
                "Cannot initialize WryeBashRunner: "
                "SKYRIM_PATH, MO2_PATH, and WRYE_BASH_PATH environment variables must be valid paths"
            )

        if not wrye_bash_path.exists():
            raise WryeBashExecutionError(f"Wrye Bash executable not found: {wrye_bash_path}")

        config = WryeBashConfig(
            wrye_bash_path=wrye_bash_path,
            game_path=game_path,
            mo2_path=mo2_path,
        )
        self._wrye_bash_runner = WryeBashRunner(config)

        logger.info(
            "WryeBashRunner inicializado: game=%s, bash=%s",
            game_path,
            wrye_bash_path,
        )
        return self._wrye_bash_runner

    async def _run_plugin_limit_guard(self, profile: str) -> dict[str, Any]:
        """M-04/M-05: Gate preventivo — valida el límite de 254 plugins.

        Recorre el modlist activo y llama a ConflictAnalyzer.validate_load_order_limit().
        Si el límite se excede, retorna un dict de error con los detalles.
        Este guard debe invocarse ANTES de ejecutar DynDOLOD, Synthesis o Wrye Bash.

        Args:
            profile: Nombre del perfil MO2 a inspeccionar.

        Returns:
            dict con ``valid=True`` si el límite no se excede,
            o ``valid=False, error=<mensaje detallado>`` si lo supera.
        """
        logger.info(
            "[M-04] Ejecutando validación de límite de plugins para perfil '%s'...",
            profile,
        )
        try:
            active_plugins: list[str] = []
            modlist_path = self._path_resolver.resolve_modlist_path(profile)
            # Leer modlist (si existe) para extraer plugins habilitados
            if modlist_path.exists():
                with open(modlist_path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line.startswith("+") and line.lower().endswith((".esp", ".esm")):
                            active_plugins.append(line[1:])

            analyzer = ConflictAnalyzer()
            analyzer.validate_load_order_limit(active_plugins)
        except RuntimeError as exc:
            logger.critical(
                "[M-04] Plugin limit EXCEEDED para perfil '%s': %s",
                profile,
                exc,
            )
            return {
                "valid": False,
                "profile": profile,
                "plugin_count": len(active_plugins),
                "limit": 254,
                "error": str(exc),
            }
        except (ValueError, OSError) as exc:
            logger.error(
                "[M-04] Error inesperado durante validación de plugins: %s",
                exc,
                exc_info=True,
            )
            return {"valid": False, "error": str(exc)}

        logger.info(
            "[M-04] Plugin limit OK: %d / 254 activos en perfil '%s'.",
            len(active_plugins),
            profile,
        )
        return {
            "valid": True,
            "profile": profile,
            "plugin_count": len(active_plugins),
            "limit": 254,
        }

    async def execute_wrye_bash_pipeline(
        self,
        profile: str | None = None,
        validate_limit: bool = True,
    ) -> dict[str, Any]:
        """FASE 6: Genera el Bashed Patch con Wrye Bash.

        Flujo:
        1. [M-04] Validación de límite de plugins (gate preventivo)
        2. Ejecutar WryeBashRunner.generate_bashed_patch()
        3. Registrar resultado en OperationJournal

        NOTA: La generación del Bashed Patch NO modifica archivos de mod
        existentes — crea/sobreescribe únicamente 'Bashed Patch, 0.esp'.
        Por esta razón no se crea snapshot del load order previo;
        únicamente se registra la operación en el journal.

        Args:
            profile: Perfil MO2 a usar (default: self.profile_name).
            validate_limit: Si True, ejecuta el guard M-04 antes de proceder.

        Returns:
            dict con ``success``, ``return_code``, ``stdout``, ``stderr``.
        """
        active_profile = profile or self.profile_name

        logger.info(
            "[FASE-6] Iniciando generación de Bashed Patch para perfil '%s'.",
            active_profile,
        )

        # PASO 0: Gate preventivo M-04 — validar límite de plugins
        if validate_limit:
            guard_result = await self._run_plugin_limit_guard(active_profile)
            if not guard_result.get("valid", True):
                logger.error(
                    "[FASE-6] Abortando Bashed Patch: validación M-04 falló. %s",
                    guard_result.get("error"),
                )
                return {
                    "success": False,
                    "aborted_by": "plugin_limit_guard",
                    "plugin_count": guard_result.get("plugin_count"),
                    "error": guard_result.get("error"),
                }

        # PASO 1: Asegurar runner inicializado
        try:
            runner = self._ensure_wrye_bash_runner()
        except WryeBashExecutionError as exc:
            logger.error("[FASE-6] Error inicializando WryeBashRunner: %s", exc)
            return {"success": False, "error": str(exc)}

        # PASO 2: Ejecutar generación del Bashed Patch
        try:
            result = await runner.generate_bashed_patch()
        except WryeBashExecutionError as exc:
            logger.error("[FASE-6] WryeBashExecutionError: %s", exc)
            return {"success": False, "error": str(exc)}

        # PASO 3: Registrar resultado mediante logging estructurado.
        # Nota: este flujo NO usa OperationJournal; la observabilidad se
        # logra exclusivamente vía logger.info con extra={} estructurado.
        logger.info(
            "[FASE-6] Bashed Patch result logged",
            extra={
                "agent_id": "wrye_bash_runner",
                "operation_type": "bashed_patch_generation",
                "file_path": "Bashed Patch, 0.esp",
                "success": result.success,
                "return_code": result.return_code,
                "duration_seconds": result.duration_seconds,
                "profile": active_profile,
            },
        )

        if result.success:
            logger.info(
                "[FASE-6] Bashed Patch generado exitosamente en %.1fs.",
                result.duration_seconds,
            )
        else:
            logger.error(
                "[FASE-6] Wrye Bash retornó código %d. stderr: %s",
                result.return_code,
                result.stderr[:500],
            )

        return {
            "success": result.success,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": result.duration_seconds,
        }

    # =========================================================================
    # FASE 5: Asset Conflict Detection Integration
    # =========================================================================

    @property
    def asset_detector(self) -> AssetConflictDetector:
        """FASE 5: Inicialización lazy del AssetConflictDetector.

        Returns:
            AssetConflictDetector inicializado.

        Raises:
            RuntimeError: Si no se puede detectar la ruta de MO2.
        """
        if self._asset_detector is None:
            mo2_mods_path = self._path_resolver.get_mo2_mods_path()
            profile = self._path_resolver.get_active_profile()
            self._asset_detector = AssetConflictDetector(mo2_mods_path, profile)
            logger.info(
                "AssetConflictDetector inicializado: mods=%s, profile=%s",
                mo2_mods_path,
                profile,
            )
        return self._asset_detector

    def scan_asset_conflicts(self) -> list[AssetConflictReport]:
        """FASE 5: Herramienta READ-ONLY para escanear conflictos de assets.

        Escanea el VFS de MO2 y detecta archivos "loose" sobrescritos.

        Returns:
            Lista de AssetConflictReport con todos los conflictos detectados.

        SECURITY: Esta herramienta es estrictamente READ-ONLY.
        No modifica, mueve ni oculta archivos.
        """
        logger.info("Iniciando escaneo de conflictos de assets...")
        try:
            conflicts = self.asset_detector.detect_conflicts()
            logger.info(f"Detectados {len(conflicts)} conflictos de assets")
            return conflicts
        except (OSError, RuntimeError) as e:
            logger.error(f"Error durante escaneo de conflictos: {e}", exc_info=True)
            raise

    def scan_asset_conflicts_json(self) -> str:
        """FASE 5: Herramienta READ-ONLY que devuelve el reporte en formato JSON.

        Returns:
            JSON string estructurado con el reporte completo de conflictos.

        SECURITY: Esta herramienta es estrictamente READ-ONLY.
        """
        logger.info("Generando reporte JSON de conflictos de assets...")
        try:
            json_report = self.asset_detector.scan_to_json()
            logger.info("Reporte JSON de conflictos generado exitosamente")
            return json_report
        except (OSError, RuntimeError) as e:
            logger.error(f"Error generando reporte JSON de conflictos: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    supervisor = SupervisorAgent()
    # asyncio.run(supervisor.start()) # En producción
