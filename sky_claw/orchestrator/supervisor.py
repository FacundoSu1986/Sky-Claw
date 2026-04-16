import asyncio
import logging
import os
import pathlib

# FASE 5: Imports de componentes de detección de conflictos de assets
from sky_claw.assets import AssetConflictDetector, AssetConflictReport
from sky_claw.comms.interface import InterfaceAgent
from sky_claw.core.database import DatabaseAgent
from sky_claw.core.event_bus import CoreEventBus, Event
from sky_claw.core.models import HitlApprovalRequest, LootExecutionParams
from sky_claw.core.path_resolver import PathResolutionService
from sky_claw.core.schemas import ScrapingQuery
from sky_claw.core.windows_interop import ModdingToolsAgent

# FASE 1.5: Imports de componentes de rollback
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

# FASE 4: DynDOLODPipelineService (extraído del Supervisor — Sprint 2)
from sky_claw.tools.dyndolod_service import DynDOLODPipelineService

# FASE 3: SynthesisPipelineService (extraído del Supervisor — Sprint 2)
from sky_claw.tools.synthesis_service import SynthesisPipelineService

# FASE 6: Imports de componentes de Wrye Bash
from sky_claw.tools.wrye_bash_runner import (
    WryeBashConfig,
    WryeBashExecutionError,
    WryeBashRunner,
)
from sky_claw.xedit.conflict_analyzer import ConflictAnalyzer, ConflictReport

# FASE 2: Imports de componentes de parcheo transaccional
from sky_claw.xedit.patch_orchestrator import (
    PatchingError,
    PatchOrchestrator,
    PatchPlan,
    PatchResult,
    PatchStrategyType,
)
from sky_claw.xedit.runner import ScriptExecutionResult, XEditRunner

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
        self.modlist_path = str(
            self._path_resolver.resolve_modlist_path(self.profile_name)
        )

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
        # Sprint-2: SynthesisPipelineService — extraído del Supervisor
        self._synthesis_service = SynthesisPipelineService(
            lock_manager=self._lock_manager,
            snapshot_manager=self.snapshot_manager,
            journal=self.journal,
            path_resolver=self._path_resolver,
            event_bus=self._event_bus,
            pipeline_config_path=pathlib.Path(BACKUP_STAGING_DIR)
            / "synthesis_pipeline.json",
        )

        # Sprint-2: DynDOLODPipelineService — extraído del Supervisor
        self._dyndolod_service = DynDOLODPipelineService(
            lock_manager=self._lock_manager,
            snapshot_manager=self.snapshot_manager,
            journal=self.journal,
            path_resolver=self._path_resolver,
            event_bus=self._event_bus,
        )

        # FASE 2: Inicializar orquestador de parches
        self._init_patch_orchestrator()

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
        self.rollback_manager = RollbackManager(
            journal=self.journal, snapshot_manager=self.snapshot_manager
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

    def _init_patch_orchestrator(self) -> None:
        """FASE 2: Inicializa el orquestador de parches transaccionales.

        Crea la instancia de PatchOrchestrator con dependencias inyectadas:
        - XEditRunner: Para ejecución de scripts xEdit
        - FileSnapshotManager: Para snapshots antes de modificaciones
        - RollbackManager: Para reversión en caso de fallo
        """
        # XEditRunner requiere paths configurados - usar lazy initialization
        self._xedit_runner: XEditRunner | None = None
        self._patch_orchestrator: PatchOrchestrator | None = None

        # FASE 5: Asset conflict detector (lazy init)
        self._asset_detector: AssetConflictDetector | None = None

        # FASE 6: Wrye Bash integration (lazy init)
        self._wrye_bash_runner: WryeBashRunner | None = None

        logger.info("PatchOrchestrator será inicializado lazy bajo demanda")

    def _ensure_patch_orchestrator(self) -> PatchOrchestrator:
        """Asegura que el PatchOrchestrator esté inicializado.

        Usa inicialización lazy porque XEditRunner requiere paths que
        pueden no estar disponibles al instanciar SupervisorAgent.

        CRIT-003: Valida XEDIT_PATH y SKYRIM_PATH antes de usar.

        Returns:
            PatchOrchestrator inicializado.

        Raises:
            PatchingError: Si no se puede inicializar (falta configuración).
        """
        if self._patch_orchestrator is not None:
            return self._patch_orchestrator

        # Obtener paths de xEdit y juego desde entorno o config
        xedit_path_str = os.environ.get("XEDIT_PATH", "")
        game_path_str = os.environ.get("SKYRIM_PATH", "")

        # CRIT-003: Validar paths antes de usar
        xedit_path = self._path_resolver.validate_env_path(xedit_path_str, "XEDIT_PATH")
        game_path = self._path_resolver.validate_env_path(game_path_str, "SKYRIM_PATH")

        # Si la validación falla, no continuar
        if not xedit_path or not game_path:
            raise PatchingError(
                "Cannot initialize PatchOrchestrator: "
                "XEDIT_PATH and SKYRIM_PATH environment variables must be valid paths"
            )

        if not xedit_path.exists():
            raise PatchingError(f"xEdit executable not found: {xedit_path}")

        # Crear XEditRunner
        self._xedit_runner = XEditRunner(
            xedit_path=xedit_path,
            game_path=game_path,
            output_dir=pathlib.Path(BACKUP_STAGING_DIR) / "patches",
        )

        # Crear PatchOrchestrator con dependencias
        self._patch_orchestrator = PatchOrchestrator(
            xedit_runner=self._xedit_runner,
            snapshot_manager=self.snapshot_manager,
            rollback_manager=self.rollback_manager,
        )

        logger.info(
            "PatchOrchestrator inicializado: xedit=%s, game=%s",
            xedit_path,
            game_path,
        )

        return self._patch_orchestrator

    async def start(self) -> None:
        await self.db.init_db()
        # FASE 1.5: Abrir journal de operaciones
        await self.journal.open()
        await self.snapshot_manager.initialize()
        # Sprint-2: Inicializar lock manager (requiere async init)
        await self._lock_manager.initialize()

        # Vincular con la señal de ejecución de la interfaz
        self.interface.register_command_callback(self.handle_execution_signal)

        logger.info(
            "SupervisorAgent inicializado: IPC y Watcher listos. Lanzando TaskGroup de fondo..."
        )

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
                logger.error(
                    "TaskGroup del Supervisor — sub-error: %s", exc, exc_info=exc
                )
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
            logger.info(
                "Análisis proactivo disparado manualmente desde la GUI (sin evento de bus)."
            )
        # Aquí se inyectaría la llamada real a la herramienta de parsing local.

    async def handle_execution_signal(self, payload: dict[str, object]) -> None:
        """Reacciona a la señal de ignición forzada desde la GUI."""
        logger.info(
            "Ignición forzada desde GUI detectada. Despertando demonio proactivo."
        )
        await self._trigger_proactive_analysis()

    # FASE 1.5: Worker de pruning pasivo
    async def dispatch_tool(self, tool_name: str, payload_dict: dict) -> dict:
        """
        Enrutador estricto. El LLM devuelve 'tool_name' y 'payload_dict'.
        Se valida con Pydantic inmediatamente.
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
                    result = await self._synthesis_service.execute_pipeline(
                        **payload_dict
                    )
                except Exception as exc:
                    logger.exception(
                        "RCA: Falló execute_synthesis_pipeline; se convierte la excepción a error dict."
                    )
                    return {
                        "status": "error",
                        "reason": "SynthesisPipelineExecutionFailed",
                        "details": str(exc),
                    }

                if not isinstance(result, dict):
                    logger.error(
                        "RCA: execute_synthesis_pipeline devolvió un tipo inválido: %s",
                        type(result).__name__,
                    )
                    return {
                        "status": "error",
                        "reason": "InvalidSynthesisPipelineResult",
                    }

                return result

            # FASE 4: DynDOLOD/TexGen Pipeline — delegado a DynDOLODPipelineService
            case "generate_lods":
                return await self._dyndolod_service.execute(**payload_dict)

            # FASE 5: Asset Conflict Detection Integration
            case "scan_asset_conflicts":
                return self.scan_asset_conflicts()

            case "scan_asset_conflicts_json":
                return self.scan_asset_conflicts_json()

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
    async def execute_rollback(self, agent_id: str) -> dict:
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
        wrye_bash_path = self._path_resolver.validate_env_path(
            wrye_bash_path_str, "WRYE_BASH_PATH"
        )

        if not game_path or not mo2_path or not wrye_bash_path:
            raise WryeBashExecutionError(
                "Cannot initialize WryeBashRunner: "
                "SKYRIM_PATH, MO2_PATH, and WRYE_BASH_PATH environment variables must be valid paths"
            )

        if not wrye_bash_path.exists():
            raise WryeBashExecutionError(
                f"Wrye Bash executable not found: {wrye_bash_path}"
            )

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

    async def _run_plugin_limit_guard(self, profile: str) -> dict:
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
                        if line.startswith("+") and line.lower().endswith(
                            (".esp", ".esm")
                        ):
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
    ) -> dict:
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

        # PASO 3: Registrar resultado mediante logging estructurado
        # (La persistencia transaccional se maneja vía commit_transaction/mark_transaction_rolled_back)
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
            logger.error(
                f"Error generando reporte JSON de conflictos: {e}", exc_info=True
            )
            raise

    # =========================================================================
    # FASE 2: Parcheo Transaccional
    # =========================================================================

    async def resolve_conflict_with_patch(
        self,
        report: ConflictReport,
        target_plugin: pathlib.Path,
    ) -> PatchResult:
        """FASE 2: Resuelve conflictos aplicando un parche con protocolo transaccional.

        Protocolo (CRÍTICO - orden estricto):

        Paso A: Crear punto de restauración
            - Invocar FileSnapshotManager.create_snapshot(target_plugin)
            - Guardar snapshot_path para rollback

        Paso B: Ejecutar parcheo
            - Invocar PatchOrchestrator.resolve(report)
            - Si retorna plan con script, ejecutar via XEditRunner.execute_patch()

        Paso C: Verificar resultado
            - Si xedit_exit_code != 0:
                - Disparar RollbackManager.restore(snapshot_path)
                - Lanzar PatchExecutionError
            - Si xedit_exit_code == 0:
                - Confirmar éxito
                - Loggear operación en journal

        Args:
            report: ConflictReport con los conflictos detectados.
            target_plugin: Path al plugin objetivo del parcheo.

        Returns:
            PatchResult con el resultado de la operación.

        Raises:
            PatchingError: Si falla la creación del snapshot.
            PatchingError: Si falla la ejecución del parche (después de rollback).
        """
        logger.info(
            "Iniciando parcheo transaccional para %s (%d conflictos)",
            target_plugin.name,
            report.total_conflicts,
        )

        # PASO A: Crear snapshot antes de modificación
        try:
            snapshot_path = await self.snapshot_manager.create_snapshot(target_plugin)
            logger.debug("Snapshot creado: %s", snapshot_path)
        except (OSError, RuntimeError) as e:
            logger.error("Fallo al crear snapshot: %s", e, exc_info=True)
            raise PatchingError(f"Snapshot falló: {e}") from e

        # PASO B: Ejecutar parcheo
        try:
            # Asegurar que el orquestador esté inicializado
            orchestrator = self._ensure_patch_orchestrator()

            # Resolver conflictos (genera plan)
            result = await orchestrator.resolve(report)

            # Si el resultado es exitoso y hay output_path, ejecutar script
            if result.success and result.output_path and self._xedit_runner is not None:
                # Determinar el tipo de estrategia desde el orquestador
                strategy_type = PatchStrategyType.CREATE_MERGED_PATCH
                if orchestrator._strategies:
                    first_strategy = orchestrator._strategies[0]
                    strategy_name = first_strategy.__class__.__name__
                    if strategy_name == "ExecuteXEditScript":
                        strategy_type = PatchStrategyType.EXECUTE_XEDIT_SCRIPT
                    elif strategy_name == "ForwardDeclaration":
                        strategy_type = PatchStrategyType.FORWARD_DECLARATION

                # Crear PatchPlan desde el resultado para ejecutar
                plan = PatchPlan(
                    strategy_type=strategy_type,
                    target_plugins=(
                        [p.plugin_a for p in report.plugin_pairs[:1]]
                        if report.plugin_pairs
                        else []
                    ),
                    output_plugin=str(result.output_path),
                    form_ids=[],
                    estimated_records=result.records_patched,
                    requires_hitl=False,
                )

                try:
                    script_result: ScriptExecutionResult = (
                        await self._xedit_runner.execute_patch(plan)
                    )

                    # Actualizar resultado con datos de ejecución
                    result = PatchResult(
                        success=script_result.exit_code == 0,
                        output_path=result.output_path,
                        records_patched=script_result.records_processed,
                        conflicts_resolved=len(report.plugin_pairs),
                        xedit_exit_code=script_result.exit_code,
                        warnings=script_result.warnings,
                        error=(
                            None
                            if script_result.exit_code == 0
                            else script_result.stderr
                        ),
                    )
                except (OSError, RuntimeError) as script_error:
                    logger.error(
                        "Error ejecutando script xEdit: %s", script_error, exc_info=True
                    )
                    # PASO C (fallo): Rollback automático
                    await self._rollback_on_failure(snapshot_path, target_plugin)
                    raise PatchingError(
                        f"Ejecución de parche falló, rollback ejecutado: {script_error}"
                    ) from script_error

        except PatchingError:
            # Re-lanzar errores de parcheo conocidos
            raise
        except (OSError, RuntimeError) as e:
            logger.error("Error durante parcheo: %s", e, exc_info=True)
            # PASO C (fallo): Rollback automático
            await self._rollback_on_failure(snapshot_path, target_plugin)
            raise PatchingError(f"Parcheo falló, rollback ejecutado: {e}") from e

        # PASO C (verificación): Verificar código de salida
        if result.xedit_exit_code != 0:
            logger.error(
                "xEdit retornó código de salida non-zero: %d",
                result.xedit_exit_code,
            )
            await self._rollback_on_failure(snapshot_path, target_plugin)
            raise PatchingError(
                f"xEdit falló con código {result.xedit_exit_code}: {result.error}"
            )

        # Éxito: Loggear en journal
        await self._log_patch_success(report, target_plugin, result)

        logger.info(
            "Parcheo exitoso: %d records procesados, %d conflictos resueltos",
            result.records_patched,
            result.conflicts_resolved,
        )

        return result

    async def _rollback_on_failure(
        self,
        snapshot_path: pathlib.Path,
        target: pathlib.Path,
    ) -> None:
        """Ejecuta rollback automático en caso de fallo de parcheo.

        Este método es crítico para la integridad del sistema. Si el rollback
        falla, se loggea como CRITICAL porque el archivo puede estar corrupto.

        Args:
            snapshot_path: Path al snapshot creado antes del fallo.
            target: Path al archivo objetivo que necesita restauración.
        """
        logger.warning("Iniciando rollback para %s", target.name)

        try:
            await self.snapshot_manager.restore_snapshot(snapshot_path, target)
            logger.info("Rollback completado exitosamente")
        except (OSError, RuntimeError) as rollback_error:
            # Esto es CRÍTICO - el archivo puede estar corrupto
            logger.critical(
                "ROLLBACK FALLÓ para %s: %s. El archivo puede estar corrupto!",
                target.name,
                rollback_error,
                exc_info=True,
            )
            # Re-lanzar para notificar al caller
            raise PatchingError(
                f"Rollback falló críticamente: {rollback_error}"
            ) from rollback_error

    async def _log_patch_success(
        self,
        report: ConflictReport,
        target_plugin: pathlib.Path,
        result: PatchResult,
    ) -> None:
        """Registra una operación de parcheo exitosa en el journal.

        Args:
            report: Reporte de conflictos resueltos.
            target_plugin: Plugin modificado.
            result: Resultado del parcheo.
        """
        # Persistencia transaccional ya manejada por commit_transaction/mark_transaction_rolled_back.
        # Registrar resultado mediante logging estructurado para observabilidad.
        logger.info(
            "Operación de parcheo registrada",
            extra={
                "agent_id": "patch_orchestrator",
                "operation_type": "patch_execution",
                "file_path": str(target_plugin),
                "total_conflicts": report.total_conflicts,
                "critical_conflicts": report.critical_conflicts,
                "records_patched": result.records_patched,
                "conflicts_resolved": result.conflicts_resolved,
                "output_path": (
                    str(result.output_path) if result.output_path else None
                ),
                "warnings": result.warnings,
            },
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    supervisor = SupervisorAgent()
    # asyncio.run(supervisor.start()) # En producción
