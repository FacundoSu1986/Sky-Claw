import os
import asyncio
import logging
import pathlib

from sky_claw.core.database import DatabaseAgent
from sky_claw.scraper.scraper_agent import ScraperAgent
from sky_claw.core.windows_interop import ModdingToolsAgent
from sky_claw.comms.interface import InterfaceAgent
from sky_claw.core.models import LootExecutionParams, HitlApprovalRequest
from sky_claw.core.schemas import ScrapingQuery
from sky_claw.orchestrator.state_graph import create_supervisor_state_graph
from sky_claw.orchestrator.ws_event_streamer import LangGraphEventStreamer

# FASE 1.5: Imports de componentes de rollback
from sky_claw.db.journal import OperationJournal
from sky_claw.db.rollback_manager import RollbackManager
from sky_claw.db.snapshot_manager import FileSnapshotManager, SnapshotInfo

# FASE 2: Imports de componentes de parcheo transaccional
from sky_claw.xedit.patch_orchestrator import (
    PatchOrchestrator,
    PatchPlan,
    PatchResult,
    PatchingError,
    PatchStrategyType,
)
from sky_claw.xedit.conflict_analyzer import ConflictReport
from sky_claw.xedit.runner import XEditRunner, ScriptExecutionResult
from sky_claw.security.path_validator import PathValidator
from sky_claw.orchestrator.maintenance_daemon import (
    MaintenanceDaemon,
    get_max_backup_size_mb,
)
from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon
from sky_claw.orchestrator.watcher_daemon import WatcherDaemon

# FASE 3: Imports de componentes de Synthesis
from sky_claw.tools.synthesis_runner import (
    SynthesisRunner,
    SynthesisConfig,
    SynthesisResult,
    SynthesisExecutionError,
)
from sky_claw.tools.patcher_pipeline import (
    PatcherPipeline,
)

# FASE 6: Imports de componentes de Wrye Bash
from sky_claw.tools.wrye_bash_runner import (
    WryeBashRunner,
    WryeBashConfig,
    WryeBashExecutionError,
)
from sky_claw.xedit.conflict_analyzer import ConflictAnalyzer

# FASE 4: Imports de componentes de DynDOLOD/TexGen
from sky_claw.tools.dyndolod_runner import (
    DynDOLODRunner,
    DynDOLODConfig,
    DynDOLODPipelineResult,
    DynDOLODExecutionError,
    DynDOLODTimeoutError,
)

# FASE 5: Imports de componentes de detección de conflictos de assets
from sky_claw.assets import AssetConflictDetector, AssetConflictReport

logger = logging.getLogger("SkyClaw.Supervisor")
security_logger = logging.getLogger("SkyClaw.Security")

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
        # ARC-01: Demonios extraídos del Supervisor
        self._maintenance_daemon = MaintenanceDaemon(
            snapshot_manager=self.snapshot_manager,
        )
        self._telemetry_daemon = TelemetryDaemon(
            emit_event=self.interface.send_event,
        )
        self._watcher_daemon = WatcherDaemon(
            modlist_path=self.modlist_path,
            profile_name=self.profile_name,
            db=self.db,
            on_change=self._trigger_proactive_analysis,
        )
        # FASE 2: Inicializar orquestador de parches
        self._init_patch_orchestrator()

        # Resolver ruta de modlist: MO2_PATH env var > auto-detección > fallback WSL2
        self.modlist_path = str(self._resolve_modlist_path(self.profile_name))

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

        # FASE 3: Synthesis integration (lazy init)
        self._synthesis_runner: SynthesisRunner | None = None
        self._patcher_pipeline: PatcherPipeline | None = None

        # FASE 4: DynDOLOD/TexGen integration (lazy init)
        self._dyndolod_runner: DynDOLODRunner | None = None

        # FASE 5: Asset conflict detector (lazy init)
        self._asset_detector: AssetConflictDetector | None = None

        # FASE 6: Wrye Bash integration (lazy init)
        self._wrye_bash_runner: WryeBashRunner | None = None

        logger.info("PatchOrchestrator será inicializado lazy bajo demanda")

    def _validate_env_path(self, path_str: str, var_name: str) -> pathlib.Path | None:
        """Valida un path de variable de entorno con PathValidator.

        CRIT-003: Mitigación para variables de entorno sin validación.

        Args:
            path_str: String del path a validar.
            var_name: Nombre de la variable de entorno (para logging).

        Returns:
            Path validado o None si la validación falla.
        """
        if not path_str:
            return None

        try:
            # Intentar validar con PathValidator
            validated_path = self._path_validator.validate(path_str)
            return validated_path
        except Exception as e:
            security_logger.warning(
                "%s inválido (posible intento de path traversal): %s - Error: %s",
                var_name,
                path_str,
                e,
            )
            return None

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
        xedit_path = self._validate_env_path(xedit_path_str, "XEDIT_PATH")
        game_path = self._validate_env_path(game_path_str, "SKYRIM_PATH")

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

    async def start(self):
        await self.db.init_db()
        # FASE 1.5: Abrir journal de operaciones
        await self.journal.open()
        await self.snapshot_manager.initialize()

        # Vincular con la señal de ejecución de la interfaz
        self.interface.register_command_callback(self.handle_execution_signal)

        logger.info(
            "SupervisorAgent inicializado: IPC y Watcher listos. Lanzando TaskGroup de fondo..."
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
            # FASE 1.5: Cerrar journal al terminar
            await self.journal.close()
            await self.db.close()

    async def _trigger_proactive_analysis(self):
        """Lógica inyectada al flujo ReAct sin prompt del usuario."""
        logger.info("Analizando topología del Load Order por cambio detectado...")
        # Aquí se inyectaría la llamada real a la herramienta de parsing local.
        pass

    async def handle_execution_signal(self, payload: dict):
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

            # FASE 3: Synthesis Pipeline Integration
            case "execute_synthesis_pipeline":
                return await self.execute_synthesis_pipeline(**payload_dict)

            # FASE 4: DynDOLOD/TexGen Pipeline Integration
            case "generate_lods":
                return await self.execute_dyndolod_pipeline(**payload_dict)

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

        except Exception as e:
            logger.exception("Error crítico durante rollback: %s", e)
            return {"success": False, "error": str(e)}

    # FASE 1.5: Obtener RollbackManager para inyección en SyncEngine
    def get_rollback_manager(self) -> RollbackManager:
        """Retorna el RollbackManager para inyección de dependencias."""
        return self.rollback_manager

    # =========================================================================
    # FASE 3: Synthesis Pipeline Integration
    # =========================================================================

    def _ensure_synthesis_runner(self) -> SynthesisRunner:
        """FASE 3: Asegura que el SynthesisRunner esté inicializado.

        Usa inicialización lazy porque SynthesisRunner requiere paths que
        pueden no estar disponibles al instanciar SupervisorAgent.

        Returns:
            SynthesisRunner inicializado.

        Raises:
            SynthesisExecutionError: Si no se puede inicializar.
        """
        if self._synthesis_runner is not None:
            return self._synthesis_runner

        # Obtener paths desde entorno o config
        game_path_str = os.environ.get("SKYRIM_PATH", "")
        mo2_path_str = os.environ.get("MO2_PATH", "")
        synthesis_exe_str = os.environ.get("SYNTHESIS_EXE", "")

        # CRIT-003: Validar paths antes de usar
        game_path = self._validate_env_path(game_path_str, "SKYRIM_PATH")
        mo2_path = self._validate_env_path(mo2_path_str, "MO2_PATH")
        synthesis_exe = self._validate_env_path(synthesis_exe_str, "SYNTHESIS_EXE")

        # Si la validación falla, no continuar
        if not game_path or not mo2_path or not synthesis_exe:
            raise SynthesisExecutionError(
                "Cannot initialize SynthesisRunner: "
                "SKYRIM_PATH, MO2_PATH, and SYNTHESIS_EXE environment variables must be valid paths"
            )

        if not synthesis_exe.exists():
            raise SynthesisExecutionError(
                f"Synthesis executable not found: {synthesis_exe}"
            )

        # Directorio de salida (overwrite de MO2 o directorio de mods)
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
        """FASE 3: Asegura que el PatcherPipeline esté inicializado.

        Returns:
            PatcherPipeline inicializado.
        """
        if self._patcher_pipeline is not None:
            return self._patcher_pipeline

        # Cargar desde archivo de configuración si existe
        pipeline_path = pathlib.Path(BACKUP_STAGING_DIR) / "synthesis_pipeline.json"

        if pipeline_path.exists():
            self._patcher_pipeline = PatcherPipeline.from_json(pipeline_path)
            logger.info(
                "PatcherPipeline cargado desde %s: %d patchers",
                pipeline_path,
                len(self._patcher_pipeline),
            )
        else:
            self._patcher_pipeline = PatcherPipeline(pipeline_config_path=pipeline_path)
            logger.info("PatcherPipeline inicializado vacío")

        return self._patcher_pipeline

    def _ensure_dyndolod_runner(self) -> DynDOLODRunner:
        """FASE 4: Asegura que el DynDOLODRunner esté inicializado.

        Usa inicialización lazy porque DynDOLODRunner requiere paths que
        pueden no estar disponibles al instanciar SupervisorAgent.

        Variables de entorno requeridas:
        - DYNDLOD_EXE: Ruta a DynDOLODx64.exe
        - TEXGEN_EXE: Ruta a TexGenx64.exe (opcional)
        - SKYRIM_PATH: Ruta al directorio de Skyrim SE/AE
        - MO2_PATH: Ruta al directorio de MO2
        - MO2_MODS_PATH: Ruta a la carpeta mods de MO2

        Returns:
            DynDOLODRunner inicializado.

        Raises:
            DynDOLODExecutionError: Si no se puede inicializar.
        """
        if self._dyndolod_runner is not None:
            return self._dyndolod_runner

        # Obtener paths desde entorno o config
        game_path_str = os.environ.get("SKYRIM_PATH", "")
        mo2_path_str = os.environ.get("MO2_PATH", "")
        mo2_mods_path_str = os.environ.get("MO2_MODS_PATH", "")
        dyndolod_exe_str = os.environ.get("DYNDLOD_EXE", "")
        texgen_exe_str = os.environ.get("TEXGEN_EXE", "")

        # CRIT-003: Validar paths antes de usar
        game_path = self._validate_env_path(game_path_str, "SKYRIM_PATH")
        mo2_path = self._validate_env_path(mo2_path_str, "MO2_PATH")
        mo2_mods_path = self._validate_env_path(mo2_mods_path_str, "MO2_MODS_PATH")
        dyndolod_exe = self._validate_env_path(dyndolod_exe_str, "DYNDLOD_EXE")
        texgen_exe = (
            self._validate_env_path(texgen_exe_str, "TEXGEN_EXE")
            if texgen_exe_str
            else None
        )

        # Si la validación falla, no continuar
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

        self._dyndolod_runner = DynDOLODRunner(config)

        logger.info(
            "DynDOLODRunner inicializado: game=%s, dyndolod=%s",
            game_path,
            dyndolod_exe,
        )

        return self._dyndolod_runner

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

        game_path = self._validate_env_path(game_path_str, "SKYRIM_PATH")
        mo2_path = self._validate_env_path(mo2_path_str, "MO2_PATH")
        wrye_bash_path = self._validate_env_path(wrye_bash_path_str, "WRYE_BASH_PATH")

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
            modlist_path = self._resolve_modlist_path(profile)
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
        except Exception as exc:
            logger.error(
                "[M-04] Error inesperado durante validación de plugins: %s", exc
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

        # PASO 3: Registrar en journal
        try:
            await self.journal.log_operation(
                agent_id="wrye_bash_runner",
                operation_type="bashed_patch_generation",
                file_path="Bashed Patch, 0.esp",
                details={
                    "success": result.success,
                    "return_code": result.return_code,
                    "duration_seconds": result.duration_seconds,
                    "profile": active_profile,
                },
            )
        except Exception as journal_exc:
            logger.warning(
                "[FASE-6] No se pudo registrar Bashed Patch en journal: %s",
                journal_exc,
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
            mo2_mods_path = self._get_mo2_mods_path()
            profile = self._get_active_profile()
            self._asset_detector = AssetConflictDetector(mo2_mods_path, profile)
            logger.info(
                "AssetConflictDetector inicializado: mods=%s, profile=%s",
                mo2_mods_path,
                profile,
            )
        return self._asset_detector

    def _sync_detect_mo2_path(self):
        """Versión síncrona de auto-detección de MO2.

        Best-effort detection for common Windows installations.  Returns the
        first directory that contains ``ModOrganizer.exe``, or ``None`` if no
        installation could be found.
        """
        for raw in [
            r"C:\Modding\MO2",
            r"D:\Modding\MO2",
            r"E:\Modding\MO2",
            r"C:\MO2Portable",
            r"D:\MO2Portable",
            r"C:\Games\MO2",
            r"D:\Games\MO2",
        ]:
            p = pathlib.Path(raw)
            if (p / "ModOrganizer.exe").exists():
                return p
        la = os.environ.get("LOCALAPPDATA")
        if la:
            md = pathlib.Path(la) / "ModOrganizer"
            if md.is_dir():
                for c in md.iterdir():
                    if c.is_dir() and (c / "ModOrganizer.exe").exists():
                        return c
        for pf in [r"C:\Program Files", r"C:\Program Files (x86)"]:
            c = pathlib.Path(pf) / "Mod Organizer 2"
            if (c / "ModOrganizer.exe").exists():
                return c
        return None

    def _resolve_modlist_path(self, profile: str) -> pathlib.Path:
        """Resolves the modlist.txt path for a given MO2 profile.

        Priority order:
        1. ``MO2_PATH`` environment variable (validated with PathValidator for
           CRIT-003 compliance).
        2. Auto-detection via :meth:`_sync_detect_mo2_path` (best-effort for
           common Windows installations).
        3. WSL2 path ``/mnt/c/Modding/MO2`` as last-resort fallback.

        Args:
            profile: MO2 profile name.

        Returns:
            Path to the ``modlist.txt`` file for the given profile.
        """
        # 1. MO2_PATH environment variable takes precedence
        mo2_base_str = os.environ.get("MO2_PATH", "")
        if mo2_base_str:
            validated_base = self._validate_env_path(mo2_base_str, "MO2_PATH")
            if validated_base:
                return validated_base / "profiles" / profile / "modlist.txt"
            logger.warning(
                "MO2_PATH='%s' rechazado por validación de seguridad (CRIT-003). "
                "Intentando auto-detección.",
                mo2_base_str,
            )

        # 2. Best-effort auto-detection for common Windows installations
        mo2_base = self._sync_detect_mo2_path()
        if mo2_base:
            return pathlib.Path(mo2_base) / "profiles" / profile / "modlist.txt"

        # 3. Fallback: WSL2 default path as last resort
        logger.warning(
            "MO2_PATH no configurado y auto-detección falló para perfil '%s'. "
            "Usando fallback WSL2. Configure la variable de entorno MO2_PATH "
            "para evitar este aviso.",
            profile,
        )
        return pathlib.Path("/mnt/c/Modding/MO2") / "profiles" / profile / "modlist.txt"

    def _get_mo2_mods_path(self) -> pathlib.Path:
        """FASE 5: Obtiene la ruta al directorio de mods de MO2.

        Intenta obtener la ruta desde variables de entorno o usar auto-detección.

        Returns:
            Path al directorio de mods de MO2.

        Raises:
            RuntimeError: Si no se puede detectar la ruta de MO2.
        """

        # Intentar obtener desde variable de entorno MO2_MODS_PATH
        mo2_mods_path_str = os.environ.get("MO2_MODS_PATH", "")
        if mo2_mods_path_str:
            validated_path = self._validate_env_path(mo2_mods_path_str, "MO2_MODS_PATH")
            if validated_path and validated_path.exists():
                return validated_path

        # Intentar construir desde MO2_PATH
        mo2_base_str = os.environ.get("MO2_PATH", "")
        if mo2_base_str:
            validated_base = self._validate_env_path(mo2_base_str, "MO2_PATH")
            if validated_base and validated_base.exists():
                mods_path = validated_base / "mods"
                if mods_path.exists():
                    return mods_path

        # Fallback: usar auto-detección síncrona.
        # Both inside and outside a running event loop we use the sync path
        # to avoid the deprecated get_event_loop() / run_until_complete() trap.
        mo2_base = self._sync_detect_mo2_path()
        if mo2_base:
            mods_path = mo2_base / "mods"
            if mods_path.exists():
                return mods_path

        raise RuntimeError(
            "No se pudo detectar la ruta de MO2. "
            "Configure MO2_PATH o MO2_MODS_PATH en las variables de entorno."
        )

    def _get_active_profile(self) -> str:
        """FASE 5: Obtiene el nombre del perfil activo de MO2.

        Returns:
            Nombre del perfil activo o 'Default' si no se puede determinar.
        """
        # Intentar obtener desde variable de entorno
        profile = os.environ.get("MO2_PROFILE", "")
        if profile:
            return profile

        # Usar el perfil almacenado en la instancia
        if hasattr(self, "profile_name") and self.profile_name:
            return self.profile_name

        return "Default"

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
        except Exception as e:
            logger.error(f"Error durante escaneo de conflictos: {e}")
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
        except Exception as e:
            logger.error(f"Error generando reporte JSON de conflictos: {e}")
            raise

    async def execute_synthesis_pipeline(
        self,
        patcher_ids: list[str] | None = None,
        create_snapshot: bool = True,
    ) -> SynthesisResult:
        """
        FASE 3: Ejecuta el pipeline de Synthesis con protección transaccional.

        Lógica crítica (Fail-safe):
        1. Si create_snapshot=True, usar FileSnapshotManager para crear
           snapshot del Synthesis.esp existente
        2. Ejecutar SynthesisRunner.run_pipeline()
        3. Si falla o el ESP está corrupto, restaurar snapshot automáticamente
        4. Registrar operación en OperationJournal

        Args:
            patcher_ids: Lista de IDs de patchers a ejecutar. Si es None,
                         usa los patchers habilitados del pipeline.
            create_snapshot: Si True, crea snapshot antes de ejecutar.

        Returns:
            SynthesisResult con el resultado de la ejecución.
        """
        logger.info(
            "Iniciando pipeline Synthesis: patchers=%s, snapshot=%s",
            len(patcher_ids) if patcher_ids else "pipeline",
            create_snapshot,
        )

        # Asegurar que los componentes estén inicializados
        try:
            runner = self._ensure_synthesis_runner()
            pipeline = self._ensure_patcher_pipeline()
        except SynthesisExecutionError as e:
            logger.error("Error inicializando Synthesis: %s", e)
            return SynthesisResult(
                success=False,
                output_esp=None,
                return_code=-1,
                stdout="",
                stderr=str(e),
                patchers_executed=[],
                errors=[str(e)],
            )

        # Determinar patchers a ejecutar
        if patcher_ids is None:
            enabled_patchers = pipeline.get_enabled_patchers()
            patcher_ids = [p.patcher_id for p in enabled_patchers]

        if not patcher_ids:
            logger.warning("No hay patchers para ejecutar")
            return SynthesisResult(
                success=False,
                output_esp=None,
                return_code=0,
                stdout="",
                stderr="No patchers to execute",
                patchers_executed=[],
                errors=["No patchers configured or enabled"],
            )

        # PASO 1: Crear snapshot del ESP existente si existe
        snapshot_path: pathlib.Path | None = None
        target_esp = runner._config.output_path / "Synthesis.esp"

        if create_snapshot and target_esp.exists():
            try:
                snapshot_path = await self.snapshot_manager.create_snapshot(
                    target_esp,
                    metadata={"source": "synthesis_pipeline", "patchers": patcher_ids},
                )
                logger.debug("Snapshot creado: %s", snapshot_path)
            except Exception as e:
                logger.warning("No se pudo crear snapshot: %s", e)
                # Continuar sin snapshot - no es fatal

        # PASO 2: Ejecutar pipeline
        try:
            result = await runner.run_pipeline(patcher_ids)
        except SynthesisExecutionError as e:
            logger.error("Error ejecutando Synthesis: %s", e)
            result = SynthesisResult(
                success=False,
                output_esp=None,
                return_code=e.return_code or -1,
                stdout="",
                stderr=e.stderr or str(e),
                patchers_executed=[],
                errors=[str(e)],
            )

        # PASO 3: Verificar resultado y rollback si es necesario
        if not result.success and snapshot_path and target_esp.exists():
            logger.warning("Pipeline falló, ejecutando rollback...")
            await self._rollback_synthesis_on_failure(snapshot_path, target_esp)

        # Validar ESP generado si el resultado fue exitoso
        if result.success and result.output_esp:
            is_valid = await runner.validate_synthesis_esp(result.output_esp)
            if not is_valid:
                logger.error("ESP generado está corrupto: %s", result.output_esp)
                if snapshot_path:
                    await self._rollback_synthesis_on_failure(snapshot_path, target_esp)
                result = SynthesisResult(
                    success=False,
                    output_esp=result.output_esp,
                    return_code=result.return_code,
                    stdout=result.stdout,
                    stderr=result.stderr + "\nESP validation failed",
                    patchers_executed=result.patchers_executed,
                    errors=result.errors
                    + ["ESP validation failed: corrupted or invalid"],
                )

        # PASO 4: Registrar en journal
        await self._log_synthesis_result(result, patcher_ids)

        if result.success:
            logger.info(
                "Pipeline Synthesis exitoso: %s (%d patchers)",
                result.output_esp,
                len(result.patchers_executed),
            )
        else:
            logger.error(
                "Pipeline Synthesis falló: %s",
                "; ".join(result.errors) if result.errors else "Unknown error",
            )

        return result

    async def _rollback_synthesis_on_failure(
        self,
        snapshot_path: pathlib.Path,
        target: pathlib.Path,
    ) -> None:
        """Ejecuta rollback automático en caso de fallo de Synthesis.

        Args:
            snapshot_path: Path al snapshot creado antes del fallo.
            target: Path al archivo ESP que necesita restauración.
        """
        logger.warning("Iniciando rollback de Synthesis para %s", target.name)

        try:
            await self.snapshot_manager.restore_snapshot(snapshot_path, target)
            logger.info("Rollback de Synthesis completado exitosamente")
        except Exception as rollback_error:
            # Esto es CRÍTICO - el archivo puede estar corrupto
            logger.critical(
                "ROLLBACK de Synthesis FALLÓ para %s: %s. El archivo puede estar corrupto!",
                target.name,
                rollback_error,
            )

    async def _log_synthesis_result(
        self,
        result: SynthesisResult,
        patcher_ids: list[str],
    ) -> None:
        """Registra el resultado de Synthesis en el journal.

        Args:
            result: Resultado de la ejecución.
            patcher_ids: IDs de patchers ejecutados.
        """
        try:
            await self.journal.log_operation(
                agent_id="synthesis_runner",
                operation_type="synthesis_pipeline",
                file_path=str(result.output_esp) if result.output_esp else "",
                details={
                    "success": result.success,
                    "return_code": result.return_code,
                    "patchers_requested": patcher_ids,
                    "patchers_executed": result.patchers_executed,
                    "errors": result.errors,
                },
            )
            logger.debug("Operación de Synthesis registrada en journal")
        except Exception as e:
            # No fallar la operación si el logging falla
            logger.warning(
                "No se pudo registrar operación de Synthesis en journal: %s", e
            )

    # =========================================================================
    # FASE 4: DynDOLOD/TexGen Pipeline Integration
    # =========================================================================

    async def execute_dyndolod_pipeline(
        self,
        preset: str = "Medium",
        run_texgen: bool = True,
        create_snapshot: bool = True,
        texgen_args: list[str] | None = None,
        dyndolod_args: list[str] | None = None,
    ) -> DynDOLODPipelineResult:
        """
        FASE 4: Ejecuta el pipeline completo de generación de LODs.

        Flujo transaccional:
        1. Crear snapshot (si create_snapshot=True)
        2. Ejecutar TexGen → Empaquetar → Registrar en DB
        3. Ejecutar DynDOLOD → Empaquetar → Registrar en DB
        4. Si falla, ejecutar rollback

        Args:
            preset: Nivel de calidad (Low, Medium, High)
            run_texgen: Si True, ejecuta TexGen antes de DynDOLOD
            create_snapshot: Si True, crea snapshot para rollback
            texgen_args: Argumentos adicionales para TexGen
            dyndolod_args: Argumentos adicionales para DynDOLOD

        Returns:
            DynDOLODPipelineResult con resultados completos
        """

        logger.info(
            "Iniciando pipeline DynDOLOD: preset=%s, texgen=%s, snapshot=%s",
            preset,
            run_texgen,
            create_snapshot,
        )

        try:
            # 1. Asegurar que el runner está inicializado
            try:
                runner = self._ensure_dyndolod_runner()
            except DynDOLODExecutionError as e:
                logger.error("Error inicializando DynDOLOD: %s", e)
                return DynDOLODPipelineResult(
                    success=False,
                    texgen_result=None,
                    dyndolod_result=None,
                    texgen_mod_path=None,
                    dyndolod_mod_path=None,
                    errors=[str(e)],
                )

            # 2. Crear snapshot de los archivos de salida existentes si existen
            snapshot_info = None
            dyndolod_output_path = runner._config.mo2_mods_path / "DynDOLOD Output"
            runner._config.mo2_mods_path / "TexGen Output"

            if create_snapshot:
                # Snapshot del DynDOLOD.esp si existe
                dyndolod_esp = dyndolod_output_path / "DynDOLOD.esp"
                if dyndolod_esp.exists():
                    try:
                        snapshot_info = await self.snapshot_manager.create_snapshot(
                            dyndolod_esp,
                            metadata={"source": "dyndolod_pipeline", "preset": preset},
                        )
                        logger.debug(
                            "Snapshot creado para DynDOLOD.esp: %s", snapshot_info
                        )
                    except Exception as e:
                        logger.warning("No se pudo crear snapshot: %s", e)
                        # Continuar sin snapshot - no es fatal

            # 3. Registrar inicio de operación en journal
            await self._log_dyndolod_start(preset, run_texgen)

            # 4. Ejecutar pipeline completo
            logger.info(
                "Starting DynDOLOD pipeline (preset: %s, texgen: %s)",
                preset,
                run_texgen,
            )
            result = await runner.run_full_pipeline(
                run_texgen=run_texgen,
                preset=preset,
                texgen_args=texgen_args,
                dyndolod_args=dyndolod_args,
            )

            # 4. Registrar mods en la base de datos si fueron exitosos
            if result.success:
                # Registrar TexGen Output
                if (
                    result.texgen_mod_path
                    and result.texgen_result
                    and result.texgen_result.success
                ):
                    try:
                        await self.db.add_mod(
                            name="TexGen Output",
                            version="1.0.0",
                            source="sky_claw_dyndolod_pipeline",
                        )
                        logger.info("TexGen Output registered in database")
                    except Exception as e:
                        logger.warning(
                            "Could not register TexGen Output in database: %s", e
                        )

                # Registrar DynDOLOD Output
                if (
                    result.dyndolod_mod_path
                    and result.dyndolod_result
                    and result.dyndolod_result.success
                ):
                    try:
                        await self.db.add_mod(
                            name="DynDOLOD Output",
                            version="1.0.0",
                            source="sky_claw_dyndolod_pipeline",
                        )
                        logger.info("DynDOLOD Output registered in database")
                    except Exception as e:
                        logger.warning(
                            "Could not register DynDOLOD Output in database: %s", e
                        )

                # Registrar éxito en journal
                await self._log_dyndolod_result(result, preset, success=True)

                logger.info(
                    "Pipeline DynDOLOD exitoso: texgen=%s, dyndolod=%s",
                    result.texgen_mod_path,
                    result.dyndolod_mod_path,
                )
            else:
                # Pipeline falló - registrar errores
                logger.error("DynDOLOD pipeline failed: %s", "; ".join(result.errors))
                await self._log_dyndolod_result(result, preset, success=False)

            return result

        except DynDOLODTimeoutError as e:
            logger.error("DynDOLOD pipeline timeout: %s", e)
            if snapshot_info:
                await self._rollback_dyndolod_on_failure(
                    snapshot_info, "dyndolod_timeout"
                )
            raise
        except DynDOLODExecutionError as e:
            logger.error("DynDOLOD execution error: %s", e)
            if snapshot_info:
                await self._rollback_dyndolod_on_failure(
                    snapshot_info, "dyndolod_execution_error"
                )
            raise
        except Exception as e:
            logger.exception("Unexpected error in DynDOLOD pipeline: %s", e)
            if snapshot_info:
                await self._rollback_dyndolod_on_failure(
                    snapshot_info, "dyndolod_unexpected_error"
                )
            raise

    async def _log_dyndolod_start(self, preset: str, run_texgen: bool) -> None:
        """Registra el inicio del pipeline DynDOLOD en el journal.

        Args:
            preset: Nivel de calidad del preset.
            run_texgen: Si se ejecuta TexGen.
        """
        try:
            await self.journal.log_operation(
                agent_id="dyndolod_runner",
                operation_type="dyndolod_pipeline_start",
                file_path="",
                details={
                    "preset": preset,
                    "run_texgen": run_texgen,
                },
            )
            logger.debug("Inicio de DynDOLOD pipeline registrado en journal")
        except Exception as e:
            logger.warning("No se pudo registrar inicio de DynDOLOD en journal: %s", e)

    async def _log_dyndolod_result(
        self,
        result: DynDOLODPipelineResult,
        preset: str,
        success: bool,
    ) -> None:
        """Registra el resultado del pipeline DynDOLOD en el journal.

        Args:
            result: Resultado de la ejecución.
            preset: Nivel de calidad del preset.
            success: Si la operación fue exitosa.
        """
        try:
            await self.journal.log_operation(
                agent_id="dyndolod_runner",
                operation_type="dyndolod_pipeline_complete"
                if success
                else "dyndolod_pipeline_failed",
                file_path=str(result.dyndolod_mod_path)
                if result.dyndolod_mod_path
                else "",
                details={
                    "success": success,
                    "preset": preset,
                    "texgen_success": result.texgen_result.success
                    if result.texgen_result
                    else False,
                    "dyndolod_success": result.dyndolod_result.success
                    if result.dyndolod_result
                    else False,
                    "texgen_mod_path": str(result.texgen_mod_path)
                    if result.texgen_mod_path
                    else None,
                    "dyndolod_mod_path": str(result.dyndolod_mod_path)
                    if result.dyndolod_mod_path
                    else None,
                    "errors": result.errors,
                },
            )
            logger.debug("Resultado de DynDOLOD registrado en journal")
        except Exception as e:
            logger.warning(
                "No se pudo registrar resultado de DynDOLOD en journal: %s", e
            )

    async def _rollback_dyndolod_on_failure(
        self,
        snapshot_info: "SnapshotInfo",
        reason: str,
    ) -> None:
        """Ejecuta rollback automático en caso de fallo del pipeline DynDOLOD.

        Args:
            snapshot_info: Información del snapshot creado antes del fallo.
            reason: Razón del rollback para logging.
        """
        logger.warning("Iniciando rollback de DynDOLOD pipeline (razón: %s)", reason)

        try:
            # Restaurar estado desde snapshot
            dyndolod_esp = pathlib.Path(snapshot_info.original_path)
            await self.snapshot_manager.restore_snapshot(
                pathlib.Path(snapshot_info.snapshot_path),
                dyndolod_esp,
            )
            logger.info("Rollback de DynDOLOD completado exitosamente")
        except Exception as rollback_error:
            # Esto es CRÍTICO - los archivos pueden estar corruptos
            logger.critical(
                "ROLLBACK de DynDOLOD FALLÓ (snapshot: %s): %s. "
                "Los archivos pueden estar en estado inconsistente!",
                snapshot_info.snapshot_path,
                rollback_error,
            )

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
        except Exception as e:
            logger.error("Fallo al crear snapshot: %s", e)
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
                    target_plugins=[p.plugin_a for p in report.plugin_pairs[:1]]
                    if report.plugin_pairs
                    else [],
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
                        error=None
                        if script_result.exit_code == 0
                        else script_result.stderr,
                    )
                except Exception as script_error:
                    logger.error("Error ejecutando script xEdit: %s", script_error)
                    # PASO C (fallo): Rollback automático
                    await self._rollback_on_failure(snapshot_path, target_plugin)
                    raise PatchingError(
                        f"Ejecución de parche falló, rollback ejecutado: {script_error}"
                    ) from script_error

        except PatchingError:
            # Re-lanzar errores de parcheo conocidos
            raise
        except Exception as e:
            logger.error("Error durante parcheo: %s", e)
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
        except Exception as rollback_error:
            # Esto es CRÍTICO - el archivo puede estar corrupto
            logger.critical(
                "ROLLBACK FALLÓ para %s: %s. El archivo puede estar corrupto!",
                target.name,
                rollback_error,
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
        try:
            await self.journal.log_operation(
                agent_id="patch_orchestrator",
                operation_type="patch_execution",
                file_path=str(target_plugin),
                details={
                    "total_conflicts": report.total_conflicts,
                    "critical_conflicts": report.critical_conflicts,
                    "records_patched": result.records_patched,
                    "conflicts_resolved": result.conflicts_resolved,
                    "output_path": str(result.output_path)
                    if result.output_path
                    else None,
                    "warnings": result.warnings,
                },
            )
            logger.debug("Operación de parcheo registrada en journal")
        except Exception as e:
            # No fallar la operación si el logging falla
            logger.warning("No se pudo registrar operación en journal: %s", e)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    supervisor = SupervisorAgent()
    # asyncio.run(supervisor.start()) # En producción
