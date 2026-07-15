from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Any, Literal

from sky_claw.antigravity.comms.interface import InterfaceAgent
from sky_claw.antigravity.core.contracts import PathValidatorProtocol
from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.antigravity.core.event_bus import CoreEventBus, Event, create_bus_with_dlq
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
from sky_claw.antigravity.orchestrator.tool_strategies.middleware import HitlGateMiddleware
from sky_claw.antigravity.orchestrator.watcher_daemon import WatcherDaemon
from sky_claw.antigravity.orchestrator.ws_event_streamer import LangGraphEventStreamer
from sky_claw.antigravity.scraper.scraper_agent import ScraperAgent
from sky_claw.antigravity.security.hitl import HITLGuard
from sky_claw.antigravity.security.network_gateway import NetworkGateway
from sky_claw.local.assets import AssetConflictDetector, AssetConflictReport
from sky_claw.local.mo2.grass_profile import GrassProfileManager
from sky_claw.local.mo2.vfs import MO2Controller
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.grass_cache_service import GrassCacheService, GrassRuntimeDeps
from sky_claw.local.tools.loot_service import LootSortingService
from sky_claw.local.tools.pandora_service import PandoraPipelineService
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from sky_claw.local.tools.wrye_bash_runner import (
    WryeBashConfig,
    WryeBashExecutionError,
    WryeBashRunner,
)
from sky_claw.local.tools.xedit_service import XEditPipelineService
from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer, ConflictReport

logger = logging.getLogger(__name__)


def _read_active_plugins_blocking(modlist_path: pathlib.Path) -> list[str]:
    """Lee el load order del perfil y devuelve sus plugins habilitados.

    PT-1 (S-6): aislada para envolverla en ``asyncio.to_thread`` desde el guard
    async y no bloquear el event loop.

    ``modlist_path`` se usa solamente para localizar el directorio del perfil:
    ``modlist.txt`` enumera *mods*, no el load order. ``plugins.txt`` contiene
    las entradas habilitadas (``*``) y tiene precedencia; ``loadorder.txt`` es
    el fallback para perfiles donde el primero no existe.
    """
    for filename, source in (("plugins.txt", "plugins_txt"), ("loadorder.txt", "loadorder")):
        load_order_path = modlist_path.parent / filename
        try:
            if load_order_path.is_file():
                return parse_active_plugins(
                    load_order_path.read_text(encoding="utf-8-sig", errors="replace"),
                    source=source,
                )
        except OSError as exc:
            logger.warning("No se pudo leer el load order %s: %s", load_order_path, exc)
    return []


security_logger = logging.getLogger(f"{__name__}.security")

# FASE 1.5: Constante para directorio de staging de backups
BACKUP_STAGING_DIR = ".skyclaw_backups/"

#: Timeout (s) del análisis profundo de xEdit: el default de 120s mata escaneos
#: de load orders grandes que legítimamente tardan varios minutos (review Codex
#: #226). 15 min cubre perfiles pesados sin colgar la UI indefinidamente.
DEEP_SCAN_TIMEOUT_SECONDS = 900

PluginListSource = Literal["loadorder", "plugins_txt"]


def parse_active_plugins(load_order_text: str, *, source: PluginListSource = "loadorder") -> list[str]:
    """Extrae los plugins del load order (``loadorder.txt`` / ``plugins.txt``).

    Seam puro (testeable sin supervisor). Formato de esos archivos de MO2/Skyrim
    SE: un plugin por línea, en orden de carga; se ignoran vacíos y comentarios
    (``#``). ``loadorder.txt`` no marca habilitados: sus plugins válidos se
    conservan tal cual. ``plugins.txt`` sí usa ``*`` como marca de habilitado,
    por lo que las líneas sin ``*`` se descartan. NO confundir con
    ``modlist.txt``, que lista *mods* con prefijos ``+/-`` (review Copilot
    #226). Se conservan solo ``.esp/.esm/.esl`` — ``.esl`` incluido porque
    xEdit también reporta conflictos entre plugins ligeros.
    """
    plugins: list[str] = []
    for raw in load_order_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if source == "plugins_txt":
            if not line.startswith("*"):
                continue
            name = line[1:].strip()
        else:
            name = line
        if name.lower().endswith((".esp", ".esm", ".esl")):
            plugins.append(name)
    return plugins


class SupervisorAgent:
    def __init__(
        self,
        profile_name: str = "Default",
        *,
        hitl_guard: HITLGuard | None = None,
        lifecycle=None,  # DatabaseLifecycleManager | None — evita import circular en runtime
        path_validator: PathValidatorProtocol | None = None,
        gateway: NetworkGateway | None = None,
    ):
        self.db = DatabaseAgent()
        # C2: reutilizar el NetworkGateway del AppContext cuando se inyecta, para
        # NO duplicar caché DNS pinning ni reglas de egress (dos políticas que
        # podrían divergir). ``is not None`` explícito (no ``or``): un gateway
        # inyectado nunca debe descartarse por ser "falsy". None crea el propio
        # (tests/standalone — backward compat).
        self.gateway = gateway if gateway is not None else NetworkGateway()
        self.scraper = ScraperAgent(self.db, gateway=self.gateway)
        self.tools = ModdingToolsAgent()
        self.interface = InterfaceAgent()
        self.profile_name = profile_name
        # M-01.1: lifecycle compartido para journal/locks/DLQ (DI opcional;
        # None conserva los fallbacks directos pre-M-01 en tests/standalone).
        self._db_lifecycle = lifecycle
        self.state_graph = create_supervisor_state_graph(profile_name=self.profile_name)
        self.event_streamer = LangGraphEventStreamer(self.state_graph, self.interface)

        # FASE 1.5: Inicializar componentes de rollback (también inicializa _path_validator)
        self._init_rollback_components()

        # Sprint-1.5: PathResolutionService — resolución stateless de rutas.
        # Blocker 3: MO2 path resolution must validate against the *modding*
        # sandbox (mo2_root/install_dir — the validator AppContext injects),
        # NOT the backup-only rollback validator above, which would reject
        # every real MO2 path and abort the GUI agent bootstrap.
        self._path_resolver = self._make_path_resolver(path_validator)

        # Resolver ruta de modlist: MO2_PATH env var > auto-detección > fallback WSL2
        self.modlist_path = str(self._path_resolver.resolve_modlist_path(self.profile_name))

        # Sprint-1 + P1.2: production-grade event bus via factory, so the
        # supervisor always boots with a real DLQ wired up.  The factory
        # constructs CoreEventBus(require_dlq=True, dlq=DLQManager(...)) — a
        # misconfiguration that returns dlq=None aborts at construction
        # instead of silently dropping events under backpressure.
        self._event_bus = create_bus_with_dlq(lifecycle=self._db_lifecycle)
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
            journal=self.journal,
            path_resolver=self._path_resolver,
            event_bus=self._event_bus,
        )

        # Audit #190: LOOT --sort rewrites the shared load order, so the real
        # sort runs under the load-order lock (LOOTRunner built lazily from the
        # path resolver, like the other tool services).
        self._loot_service = LootSortingService(
            lock_manager=self._lock_manager,
            snapshot_manager=self.snapshot_manager,
            path_resolver=self._path_resolver,
            path_validator=self._path_validator,
            # T-26 (review Codex PR #243): cablear el journal de producción para
            # que el Ritual de LOOT emita el ActionManifest antes de mutar —
            # sin esto el guard era test-only. Espeja los servicios hermanos.
            journal=self.journal,
        )

        # Follow-up A: Pandora regenera behavior graphs, así que la corrida real va
        # bajo el lock de behavior-graphs (runner construido perezosamente desde el
        # resolver, igual que los otros servicios de tools).
        self._pandora_service = PandoraPipelineService(
            lock_manager=self._lock_manager,
            snapshot_manager=self.snapshot_manager,
            path_resolver=self._path_resolver,
        )

        # Grass cache: orquestador del Stage 8 (NGIO). El runner de xEdit de la
        # Fase A se resuelve perezosamente vía el servicio de xEdit; las
        # dependencias de Fases B/C (perfil MO2, game path, overwrite/Grass) las
        # arma _build_grass_dependencies como PROVIDER LAZY: se resuelven al
        # ejecutar el ritual, no en __init__, porque en la GUI MO2_PATH/
        # SKYRIM_PATH se hidratan DESPUÉS de construir el supervisor (review
        # Codex #301). El servicio devuelve error accionable del contrato si
        # todavía no están configuradas.
        self._grass_cache_service = GrassCacheService(
            lock_manager=self._lock_manager,
            snapshot_manager=self.snapshot_manager,
            journal=self.journal,
            event_bus=self._event_bus,
            xedit_runner_provider=self._xedit_service.ensure_xedit_runner,
            runtime_deps_provider=self._build_grass_dependencies,
        )

        # Lazy init para runners legacy que aún no son servicios puros (WryeBash, AssetDetector)
        self._asset_detector: AssetConflictDetector | None = None
        self._wrye_bash_runner: WryeBashRunner | None = None

        # Strangler Fig: dispatcher para herramientas migradas. Las branches del match/case
        # delegan progresivamente a este dispatcher. Se construye al final del __init__
        # para garantizar que todos los services y agents ya están listos.
        # Fail-closed: sin hitl_guard, las tools destructivas se DENIEGAN.
        if hitl_guard is None:
            logger.warning(
                "SupervisorAgent sin hitl_guard — las tools destructivas serán "
                "DENEGADAS (fail-closed). Inyectar AppContext.hitl para habilitar HITL."
            )
        # T-27b·2: el flow de promoción del sandbox necesita el guard crudo
        # (no solo el middleware) para la aprobación post-run del diff.
        self._hitl_guard = hitl_guard
        self._tool_dispatcher = build_orchestration_dispatcher(
            self,
            hitl_gate=HitlGateMiddleware(hitl_guard=hitl_guard),
        )

        # M-1 FIX: Wire StateGraphIntegration para activar cortacircuitos cognitivo y HITL.
        # Los callbacks se registran en el grafo y los nodos los invocan vía wrapper.
        self._graph_integration = StateGraphIntegration(self.state_graph)
        self._graph_integration.connect_supervisor(self)

    def _make_path_resolver(self, sandbox_validator: PathValidatorProtocol | None) -> PathResolutionService:
        """Build the MO2 path resolver.

        MO2 path resolution must validate against the *modding* sandbox roots
        (``mo2_root``, ``install_dir``, …) — the broad validator that
        ``AppContext`` constructs and injects. The rollback ``_path_validator``
        is intentionally scoped to the backup directory only and would reject
        every real MO2 path (Blocker 3: the GUI supervisor never bootstrapped).
        Prefer the injected sandbox validator; fall back to the rollback
        validator only when none is provided (standalone / tests).
        """
        resolution_validator = sandbox_validator if sandbox_validator is not None else self._path_validator
        # El ritual de grass escribe en <MO2>/profiles y <MO2>/mods, así que
        # necesita el MISMO validator de modding que el resolver (no el rollback
        # backup-only, que rechazaría esos paths — review Codex #301).
        self._modding_validator = resolution_validator
        return PathResolutionService(
            path_validator=resolution_validator,
            profile_name=self.profile_name,
        )

    def _init_rollback_components(self) -> None:
        """FASE 1.5 + SUP-04: Inicializa los componentes de resiliencia para rollback.

        Delega la construcción al factory method ``create_rollback_components``
        para desacoplar ``SupervisorAgent`` de la creación concreta de dependencias.
        """
        components: RollbackComponents = create_rollback_components(
            BACKUP_STAGING_DIR,
            lifecycle=self._db_lifecycle,
        )
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
        # ARC-01 + SUP-05 + H-2: supervisar los loops de los demonios JUNTO con la
        # interfaz. Antes, el TaskGroup envolvía daemon.start() (fire-and-forget):
        # sus hijos completaban en microsegundos y los loops reales quedaban
        # huérfanos, con sus excepciones perdidas en el handler del event loop.
        try:
            await self._run_daemons_and_interface()
        finally:
            # Sprint-1: Detener event bus después de los demonios
            await self._event_bus.stop()
            # Sprint-2: Cerrar lock manager
            await self._lock_manager.close()
            # FASE 1.5: Cerrar journal al terminar
            await self.journal.close()
            await self.db.close()

    async def _run_daemons_and_interface(self) -> None:
        """Corre los loops de los demonios + la interfaz con fail-fast real (H-2).

        Los tres loops de demonio y ``_run_interface_isolated`` corren como
        tareas supervisadas. Con ``FIRST_COMPLETED``:

        - Si un loop de demonio escapa su ``except`` interno (BaseException que no
          sea Cancelled, o bug en el propio handler), su tarea termina con
          excepción: cancelamos al resto y la propagamos (fail-fast colectivo).
        - Si la interfaz retorna normalmente (error de red recuperable ya
          absorbido por ``_run_interface_isolated``), cancelamos los demonios y
          salimos con gracia — preservando el apagado ordenado previo.
        """
        daemon_tasks = [
            asyncio.create_task(self._maintenance_daemon.run(), name="daemon-maintenance"),
            asyncio.create_task(self._telemetry_daemon.run(), name="daemon-telemetry"),
            asyncio.create_task(self._watcher_daemon.run(), name="daemon-watcher"),
        ]
        interface_task = asyncio.create_task(self._run_interface_isolated(), name="interface")
        all_tasks = [*daemon_tasks, interface_task]

        try:
            done, _pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
            # Propagar la primera excepción real (no Cancelled) de las tareas
            # que terminaron. Un retorno normal de la interfaz no propaga nada.
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            for task in all_tasks:
                task.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)

    async def _run_interface_isolated(self) -> None:
        """Run ``interface.connect()`` inside a TaskGroup, splitting recoverable
        network errors from programming bugs.

        P1 §3.1 — the previous blanket ``except* Exception`` absorbed everything
        and turned bugs (AttributeError, TypeError, ValueError) into silent log
        lines. Now:

        - Recoverable network-layer errors (ConnectionError, TimeoutError,
          OSError) are logged WARNING and swallowed so the supervisor's
          ``finally`` block can drain its daemons cleanly.
        - Anything else is a programming bug — logged CRITICAL and re-raised
          so the failure surfaces to the caller instead of being absorbed.
        """
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.interface.connect())
        except* (ConnectionError, TimeoutError, OSError) as eg:
            for exc in eg.exceptions:
                logger.warning(
                    "Supervisor interface — recoverable error: %s",
                    exc,
                    exc_info=exc,
                )
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.critical(
                    "Supervisor interface — unexpected error (re-raising): %s",
                    exc,
                    exc_info=exc,
                )
            raise

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

    def _build_grass_dependencies(self) -> GrassRuntimeDeps | None:
        """Provider lazy de las deps de Fases B/C del ritual de grass (Stage 8).

        Lo llama el :class:`GrassCacheService` al EJECUTAR el ritual (no en
        ``__init__``): en la GUI, ``MO2_PATH``/``SKYRIM_PATH`` se hidratan
        después de construir el supervisor, así que resolverlas antes daría
        ``None`` permanente (review Codex #301). Devuelve ``None`` si todavía
        faltan — el servicio responde con su error de contrato accionable y se
        reintenta en la próxima corrida.

        Usa el validator de MODDING (``_modding_validator``, el mismo que el
        path resolver), no el rollback backup-only que rechazaría
        ``<MO2>/profiles`` y ``<MO2>/mods``; y clona el perfil ACTIVO
        (``self.profile_name``), no el ``Default`` por defecto.
        """
        mo2_root = self._path_resolver.get_mo2_path()
        game_path = self._path_resolver.get_skyrim_path()
        if mo2_root is None or game_path is None:
            return None
        mo2 = MO2Controller(mo2_root, self._modding_validator)
        profile_manager = GrassProfileManager(
            mo2_root,
            self._modding_validator,
            source_profile=self.profile_name,
            controller=mo2,
        )
        return GrassRuntimeDeps(
            profile_manager=profile_manager,
            mo2=mo2,
            game_path=game_path,
            overwrite_grass_dir=mo2_root / "overwrite" / "Grass",
        )

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
            # PT-1 (S-6): leer el modlist (I/O de archivo) en un thread para no
            # bloquear el event loop; el parseo con criterio L-1 vive en el helper.
            active_plugins = await asyncio.to_thread(_read_active_plugins_blocking, modlist_path)

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
    def event_bus(self) -> CoreEventBus:
        """Acceso de solo lectura al CoreEventBus interno.

        Permite que consumidores externos (p. ej. el bridge de telemetría de la
        GUI) se suscriban a tópicos publicados por los demonios sin acoplarse al
        campo privado ``_event_bus``.
        """
        return self._event_bus

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

    async def scan_record_conflicts(
        self,
        profile: str | None = None,
        plugins: list[str] | None = None,
    ) -> ConflictReport:
        """FASE 6: análisis PROFUNDO de conflictos a nivel record vía xEdit (read-only).

        A diferencia de ``scan_asset_conflicts`` (escaneo liviano del VFS), corre
        xEdit como subproceso — es lento y requiere SSEEdit instalado — y detecta
        los conflictos de records que causan CTDs. El puente
        ``persist_record_conflicts`` lleva el reporte a la tabla ``conflicts``.

        Args:
            profile: perfil MO2 a inspeccionar (por defecto, el activo).
            plugins: lista explícita de plugins; si es ``None`` se lee el load
                order del perfil (``loadorder.txt``, fallback ``plugins.txt``).

        Returns:
            ``ConflictReport`` con los pares de plugins en disputa (vacío si no
            hay plugins activos).

        Raises:
            RuntimeError: si faltan SKYRIM_PATH o XEDIT_PATH, o si xEdit falla.
        """
        import pathlib

        from sky_claw.local.xedit.runner import XEditRunner

        profile = profile or self._path_resolver.get_active_profile()
        if plugins is None:
            # El load order de plugins vive en loadorder.txt (fallback plugins.txt),
            # siblings de modlist.txt en el dir del perfil — NO en modlist.txt, que
            # lista mods (review Copilot #226). utf-8-sig tolera BOM.
            profile_dir = self._path_resolver.resolve_modlist_path(profile).parent
            plugins = []
            for candidate in ("loadorder.txt", "plugins.txt"):
                lo_path = profile_dir / candidate
                if lo_path.exists():
                    plugins = parse_active_plugins(
                        lo_path.read_text(encoding="utf-8-sig"),
                        source="plugins_txt" if candidate == "plugins.txt" else "loadorder",
                    )
                    if plugins:
                        break
        if not plugins:
            logger.info("Análisis profundo: sin plugins activos en perfil '%s'.", profile)
            return ConflictReport(total_conflicts=0, critical_conflicts=0)

        game_path = self._path_resolver.get_skyrim_path()
        xedit_path = self._path_resolver.get_xedit_path()
        if game_path is None or xedit_path is None:
            raise RuntimeError("El análisis profundo requiere SKYRIM_PATH y XEDIT_PATH configurados.")

        xedit_runner = XEditRunner(
            xedit_path=xedit_path,
            game_path=game_path,
            output_dir=pathlib.Path(BACKUP_STAGING_DIR) / "patches",
            timeout=DEEP_SCAN_TIMEOUT_SECONDS,
        )
        logger.info("Análisis profundo de conflictos: %d plugins, perfil '%s'.", len(plugins), profile)
        return await ConflictAnalyzer().analyze(plugins, xedit_runner)

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
