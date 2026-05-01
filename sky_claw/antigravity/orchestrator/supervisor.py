import asyncio
import logging
import pathlib
from typing import Any

from sky_claw.antigravity.comms.interface import InterfaceAgent
from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.core.models import HitlApprovalRequest
from sky_claw.antigravity.core.path_resolver import PathResolutionService
from sky_claw.antigravity.core.windows_interop import ModdingToolsAgent
from sky_claw.antigravity.db.rollback_manager import RollbackManager
from sky_claw.antigravity.orchestrator.maintenance_daemon import (
    MaintenanceDaemon,
)
from sky_claw.antigravity.orchestrator.rollback_factory import (
    RollbackComponents,
    create_rollback_components,
)
from sky_claw.antigravity.orchestrator.state_graph import (
    StateGraphIntegration,
    create_supervisor_state_graph,
)
from sky_claw.antigravity.orchestrator.telemetry_daemon import TelemetryDaemon
from sky_claw.antigravity.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from sky_claw.antigravity.orchestrator.watcher_daemon import WatcherDaemon
from sky_claw.antigravity.orchestrator.ws_event_streamer import LangGraphEventStreamer
from sky_claw.antigravity.scraper.scraper_agent import ScraperAgent
from sky_claw.local.assets import AssetConflictDetector, AssetConflictReport
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from sky_claw.local.tools.wrye_bash_runner import (
    WryeBashConfig,
    WryeBashExecutionError,
    WryeBashRunner,
)
from sky_claw.local.tools.xedit_service import XEditPipelineService
from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer

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

        # Strangler Fig: dispatcher para herramientas migradas. Las branches del match/case
        # delegan progresivamente a este dispatcher. Se construye al final del __init__
        # para garantizar que todos los services y agents ya están listos.
        self._tool_dispatcher = build_orchestration_dispatcher(self)

        # M-1 FIX: Wire StateGraphIntegration para activar cortacircuitos cognitivo y HITL.
        # Los callbacks se registran en el grafo y los nodos los invocan vía wrapper.
        self._graph_integration = StateGraphIntegration(self.state_graph)
        self._graph_integration.connect_supervisor(self)

    def _init_rollback_components(self) -> None:
        """FASE 1.5 + SUP-04: Inicializa los componentes de resiliencia para rollback.

        Delega la construcción al factory method ``create_rollback_components``
        para desacoplar ``SupervisorAgent`` de la creación concreta de dependencias.
        """
        components: RollbackComponents = create_rollback_components(BACKUP_STAGING_DIR)
        self.journal = components.journal
        self.snapshot_manager = components.snapshot_manager
        self.rollback_manager = components.rollback_manager
        self._lock_manager = components.lock_manager
        self._path_validator = components.path_validator

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
        # ARC-01 + SUP-05: Iniciar demonios con TaskGroup para fail-fast colectivo.
        # Si un daemon falla al iniciar, los demás se cancelan automáticamente.
        async with asyncio.TaskGroup() as daemon_tg:
            daemon_tg.create_task(self._maintenance_daemon.start())
            daemon_tg.create_task(self._telemetry_daemon.start())
            daemon_tg.create_task(self._watcher_daemon.start())

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

    async def dispatch_tool(self, tool_name: str, payload_dict: dict[str, Any]) -> dict[str, Any]:
        """Enrutador estricto. Delega al OrchestrationToolDispatcher.

        El dispatcher mapea `tool_name` a una `ToolStrategy` registrada (ver
        :func:`sky_claw.antigravity.orchestrator.tool_dispatcher.build_orchestration_dispatcher`)
        y aplica la cadena de middleware correspondiente (Pydantic dentro de
        la strategy, ErrorWrapping + DictResultGuard alrededor según se registren).

        Si el LLM alucina un `tool_name` no registrado, devuelve el contrato
        legacy preservado verbatim: ``{"status": "error", "reason": "ToolNotFound"}``.
        """
        return await self._tool_dispatcher.dispatch(tool_name, payload_dict)

    def _create_hitl_request(self, hitl_request: dict[str, Any]) -> HitlApprovalRequest:
        """Convierte un dict de HITL del grafo de estados a un HitlApprovalRequest.

        Bridge entre el ``StateGraphState.hitl_request`` (dict plano del grafo)
        y el contrato Pydantic que espera :meth:`InterfaceAgent.request_hitl`.

        Args:
            hitl_request: Dict con ``action_type``, ``reason`` y metadatos
                opcionales como ``context_data`` inyectados por los callbacks
                del grafo (``_on_dispatching``, etc.).

        Returns:
            Instancia validada de :class:`HitlApprovalRequest`.
        """
        payload = dict(hitl_request)
        payload.setdefault("action_type", "circuit_breaker_halt")
        payload.setdefault("reason", "")
        payload.setdefault("context_data", {})
        return HitlApprovalRequest.model_validate(payload)

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

        # SUP-06: Capturar Exception genérica para garantizar contrato dict de retorno
        except Exception as e:
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

        game_path = self._path_resolver.get_skyrim_path()
        mo2_path = self._path_resolver.get_mo2_path()
        wrye_bash_path = self._path_resolver.get_wrye_bash_path()

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
