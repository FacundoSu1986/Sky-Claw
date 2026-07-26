"""Shared NiceGUI bootloader for GUI and web modes."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sky_claw.antigravity.gui.gui_event_adapter import EventType, SkyClawEvent
from sky_claw.antigravity.gui.gui_event_adapter import event_bus as gui_event_bus
from sky_claw.antigravity.gui.sky_claw_gui import get_state, set_runtime_context, setup_app
from sky_claw.antigravity.gui.state import ReactiveStore, get_store
from sky_claw.antigravity.gui.views.forge_dashboard import (
    STORE_KEY_CPU,
    STORE_KEY_ENV,
    STORE_KEY_GPU,
    STORE_KEY_RAM,
)
from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.app_context import AppContext, _resolve_config_path_static, start_full
from sky_claw.logging_config import correlation_id_var, install_loop_exception_handler

logger = logging.getLogger("sky_claw")

# P1 §3.2 — bounded LLM call. The GUI shows a snappy error rather than
# hanging on an unresponsive provider.
_GUI_CHAT_TIMEOUT_SECONDS: float = 30.0

# Gracia del watchdog de cierre: cuánto se espera a que vuelva el último
# cliente antes de apagar. Tiene que cubrir un F5 y una reconexión de socket
# (segundos), sin dejar el proceso zombi tanto como para que el usuario
# reabra la app y choque contra el puerto ocupado.
_EXIT_WATCHDOG_GRACE_SECONDS: float = 15.0


def _make_telemetry_store_bridge(store: ReactiveStore):
    """Build a CoreEventBus subscriber that mirrors telemetry into the GUI store.

    Phase 1 ("Panel con datos reales"): ``TelemetryDaemon`` publishes
    ``system.telemetry.*`` Events on the supervisor's CoreEventBus. The GUI,
    however, reads its live vitals/HUD from the reactive store. This bridge
    closes that gap so the panel shows real CPU/RAM/GPU instead of hardcoded
    placeholders. GPU stays ``None`` ("N/D") when unavailable.
    """

    async def _bridge(event) -> None:
        payload = event.payload or {}
        store.set(STORE_KEY_CPU, payload.get("cpu"))
        store.set(STORE_KEY_RAM, payload.get("ram_percent"))
        store.set(STORE_KEY_GPU, payload.get("gpu"))

    return _bridge


def _install_gui_hitl_bridge(ctx: AppContext, store: ReactiveStore) -> None:
    """Route ``tool_execution`` + ``download`` + ``sandbox_promotion`` HITL approvals to the GUI.

    Composes over the AppContext's existing notify closure: ``tool_execution``
    prompts are handled by the GUI — auto-approved when the "Modo local" toggle is
    on, otherwise parked in the store so the page shows an Aprobar/Denegar modal.
    ``download`` prompts (the "Instalar" button — Follow-up C) are also parked in the
    modal but are **never** auto-approved (network egress is always confirmed by
    hand); ídem ``sandbox_promotion`` (T-27b·2: el diff post-run de un sandbox se
    revisa siempre — auto-promover lo vaciaría de sentido). Every other category
    still flows to the original (Telegram) closure, and the guard's timeout keeps
    the fail-closed auto-deny when nobody answers.
    """
    from sky_claw.antigravity.gui.controllers.ritual_runner import (
        STORE_KEY_PENDING_HITL,
        make_gui_hitl_notify,
        ritual_auto_approve_armed,
    )

    guard = ctx.hitl
    if guard is None:
        logger.warning("No HITLGuard on AppContext — GUI ritual approval unavailable")
        return
    # Wrap (not replace) the original closure so Telegram scope approvals keep
    # working; tool_execution + download are intercepted for the GUI. M-9: el getter
    # lee un ContextVar scoped a la task del dispatch (ritual_auto_approve_armed),
    # no un flag global del store, así que la aprobación automática queda acotada a
    # exactamente la task del ritual que la armó — nunca un tool_execution
    # concurrente de Telegram/LLM/API.
    original_notify = guard._notify
    guard._notify = make_gui_hitl_notify(
        respond=guard.respond,
        set_pending=lambda payload: store.set(STORE_KEY_PENDING_HITL, payload),
        auto_approve_getter=ritual_auto_approve_armed,
        delegate=original_notify,
    )


def _build_environment_scanner(ctx: AppContext):
    """Build an :class:`EnvironmentScanner` seeded from the user's configured paths.

    Reads ``skyrim_path`` plus the tool executables (``loot_exe``/``xedit_exe``/
    ``pandora_exe``) from the on-disk config so setups with manual paths report
    those tools as installed in the Rituales — instead of every tool falling to
    "No instalado" when Skyrim isn't auto-detected (follow-up #2 / #209).

    A config-read failure degrades gracefully to a bare (auto-detect-only)
    scanner; the scan must never block GUI startup.
    """
    from sky_claw.config import Config
    from sky_claw.local.discovery.scanner import EnvironmentScanner

    try:
        cfg = Config(ctx.config_path)
    except Exception:
        logger.exception("Could not load config for environment scan; using auto-detect only")
        return EnvironmentScanner()

    skyrim_path = (getattr(cfg, "skyrim_path", "") or "").strip() or None
    # Map scanner tool keys (scanner.py tool_defs) → configured exe keys. Blank
    # config values are dropped by the scanner, so pass them through untouched.
    tool_path_cfg_keys = {"loot": "loot_exe", "xedit": "xedit_exe", "pandora": "pandora_exe"}
    tool_paths = {key: getattr(cfg, cfg_key, "") for key, cfg_key in tool_path_cfg_keys.items()}
    return EnvironmentScanner(skyrim_path=skyrim_path, tool_paths=tool_paths)


# Map EnvironmentScanner snapshot fields → the env vars the supervisor's
# PathResolutionService reads (it is env-only by design). Mirrors the resolver's
# exact names, including the existing ``DYNDLOD_EXE`` spelling.
_SNAPSHOT_TOOL_ENV: dict[str, str] = {
    "loot": "LOOT_EXE",
    "wrye_bash": "WRYE_BASH_PATH",
    "dyndolod": "DYNDLOD_EXE",
    "pandora": "PANDORA_EXE",
    "xedit": "XEDIT_PATH",
}


def _hydrate_tool_env_from_snapshot(snapshot) -> None:
    """Export the scan's resolved tool paths into ``os.environ`` for the dispatcher.

    The Rituales dispatch through the supervisor, whose ``PathResolutionService``
    resolves Skyrim/LOOT/Wrye Bash/DynDOLOD **only** from ``os.environ`` — nothing
    hydrates those from the wizard/TOML config. Without this bridge a Ritual marked
    "Disponible" (from a config/auto-detected path) would dispatch and then fail
    with a missing-path error (Codex P1 on #211). The ``EnvironmentScanner`` already
    resolved these exes, so seed the env from the same source of truth.

    Uses ``setdefault`` so an explicit operator-set env var always wins, and only
    non-secret tool paths are written (H-04 removed ``os.environ`` mutation for
    *secrets*, not for tool locations).
    """
    if snapshot is None:
        return
    skyrim = getattr(snapshot, "skyrim", None)
    skyrim_path = getattr(skyrim, "path", None) if skyrim is not None else None
    if skyrim_path:
        os.environ.setdefault("SKYRIM_PATH", str(skyrim_path))
    # MO2_PATH too: Wrye Bash and DynDOLOD also resolve MO2_PATH (and derive
    # MO2_MODS_PATH = MO2_PATH/mods) from the env, so without this they'd still
    # hit a missing-path error even with the tool exe set.
    mo2 = getattr(snapshot, "mo2", None)
    mo2_path = getattr(mo2, "path", None) if mo2 is not None else None
    if mo2_path:
        os.environ.setdefault("MO2_PATH", str(mo2_path))
    tools = getattr(snapshot, "tools", None) or {}
    for tool_key, env_name in _SNAPSHOT_TOOL_ENV.items():
        info = tools.get(tool_key)
        exe_path = getattr(info, "exe_path", None) if info is not None else None
        if exe_path:
            os.environ.setdefault(env_name, str(exe_path))


async def _run_environment_scan(scanner, store: ReactiveStore) -> None:
    """Run a one-shot environment scan and publish the snapshot to the store.

    Drives the Ritual cards' real Available/No instalado state. A scan failure
    must never crash GUI startup — it is logged and the store key is left unset
    (the cards then show the honest "Verificando…" state).

    The scanner's probes are synchronous filesystem I/O (``Path.exists``,
    ``iterdir``, ``shutil.which``, ``_find_tool``), so awaiting ``scan()`` on the
    NiceGUI loop would block the page + ``/ws/ui`` startup on a slow/unavailable
    drive — and its internal ``wait_for`` timeout can't fire while sync calls
    run. We offload the whole scan onto a worker thread (its own event loop) so
    the UI loop stays responsive (Codex review on #209).
    """
    try:
        snapshot = await asyncio.to_thread(lambda: asyncio.run(scanner.scan()))
    except Exception:
        logger.exception("Environment scan failed; ritual availability stays unknown")
        return
    store.set(STORE_KEY_ENV, snapshot)
    # Bridge the resolved tool paths to the env the dispatcher's resolver reads,
    # so an "available" Ritual can actually run (Codex P1 on #211).
    _hydrate_tool_env_from_snapshot(snapshot)


async def _dispatch_chat_to_router(ctx: AppContext, text: str) -> None:
    """Send a chat turn through ``ctx.router`` and publish the result as a GUI event.

    Wraps the LLM call in ``asyncio.wait_for`` so a hung provider surfaces a
    clean error event in the UI within :data:`_GUI_CHAT_TIMEOUT_SECONDS`
    seconds instead of freezing the logic loop.
    """
    try:
        response = await asyncio.wait_for(
            ctx.router.chat(text, ctx.session, chat_id="gui-session"),
            timeout=_GUI_CHAT_TIMEOUT_SECONDS,
        )
        gui_event_bus.publish(
            SkyClawEvent(
                type=EventType.LLM_RESPONSE,
                data={"response": response},
                source="logic_loop",
            )
        )
    except TimeoutError:
        logger.warning(
            "GUI chat timed out after %.0fs — provider may be unresponsive",
            _GUI_CHAT_TIMEOUT_SECONDS,
        )
        gui_event_bus.publish(
            SkyClawEvent(
                type=EventType.LLM_RESPONSE,
                data={"response": f"⚠️ Timeout: el proveedor no respondió en {_GUI_CHAT_TIMEOUT_SECONDS:.0f}s."},
                source="logic_loop",
            )
        )
    except Exception as e:
        logger.exception("Logic error in chat: %s", e)
        gui_event_bus.publish(
            SkyClawEvent(
                type=EventType.LLM_RESPONSE,
                data={"response": f"⚠️ Error: {type(e).__name__}: {e}"},
                source="logic_loop",
            )
        )


async def _gui_logic_loop(ctx: AppContext) -> None:
    """Process chat messages from the GUI in a background task.

    Reads ``ctx.logic_queue`` items placed by callers that prefer the
    direct-router path (e.g. integration scripts).  The modern GUI chat
    path uses ChatController → EventBus instead; this loop is kept as the
    offline-fallback processor so both paths co-exist.
    """
    consecutive_errors = 0
    while True:
        try:
            # asyncio.Queue → cancelable de forma nativa. No usar
            # ``to_thread(ctx.logic_queue.get)``: bloquearía un worker no-daemon
            # del pool que nadie despierta, congelando el apagado (P0-1).
            item = await ctx.logic_queue.get()
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                logger.warning("Malformed item in logic_queue: %r", item)
                continue
            if item[0] == "chat":
                text = item[1]
                if not ctx.router:
                    gui_event_bus.publish(
                        SkyClawEvent(
                            type=EventType.LLM_RESPONSE,
                            data={"response": "⚠️ Router no inicializado. Completá el setup primero."},
                            source="logic_loop",
                        )
                    )
                    continue
                correlation_id_var.set(str(uuid.uuid4()))
                # P1 §3.2 — extracted, timeout-bounded, individually testable.
                await _dispatch_chat_to_router(ctx, text)
                consecutive_errors = 0
        except asyncio.CancelledError:
            # Re-lanzar (no ``break``) para que la task quede realmente
            # cancelled() y el apagado la vea drenada, no completada por azar.
            raise
        except Exception as e:
            consecutive_errors += 1
            backoff = min(2**consecutive_errors, 30)
            logger.exception("GUI logic loop error (backoff=%ds): %s", backoff, e)
            await asyncio.sleep(backoff)


async def _gui_mod_update_loop(ctx: AppContext) -> None:
    """Periodically refresh active-mod count and mod list in the reactive store."""
    from sky_claw.antigravity.gui.state import get_store

    while True:
        try:
            if ctx.registry:
                mods_dicts = await ctx.registry.search_mods("")
                get_state().active_mods.set(len(mods_dicts))
                get_store().set("mods_list", mods_dicts)
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error updating modlist in GUI: %s", exc)
            await asyncio.sleep(5)


class ExitWatchdog:
    """Apaga la app cuando el último cliente se va y no vuelve.

    Cierra el círculo del post-mortem del WinError 10048. Las PRs #361→#367
    hicieron que el proceso PUEDA morir limpio; faltaba qué se lo pide. En el
    ``.exe`` no hay nada:

    - ``sky_claw.spec`` compila con ``console=False`` (subsystem
      ``WINDOWS_GUI``): sin consola adjunta no existe ``CTRL_CLOSE_EVENT``, así
      que ``SetConsoleCtrlHandler`` no tiene a qué engancharse.
    - ``ui.run`` corre con ``show=True`` y SIN ``native=True``: abre el
      navegador del sistema, y cerrar esa pestaña no le manda nada al proceso.

    Sin este watchdog ``app.on_shutdown`` nunca corre en el ``.exe``: el
    proceso queda vivo reteniendo 8080/8765 y el arranque siguiente falla con
    ``WinError 10048``.

    El período de gracia es imprescindible: un F5, una reconexión de socket o
    navegar entre páginas desconectan al cliente momentáneamente, y apagar al
    primer disconnect cerraría la app cada vez que el usuario refresca.

    Dependencias inyectadas (``contar_clientes``/``apagar``) para que la lógica
    sea testeable sin levantar NiceGUI.
    """

    def __init__(
        self,
        *,
        grace_seconds: float,
        contar_clientes: Callable[[], int],
        apagar: Callable[[], None],
    ) -> None:
        self._grace_seconds = grace_seconds
        self._contar_clientes = contar_clientes
        self._apagar = apagar
        self._cuenta_regresiva: asyncio.Task[None] | None = None

    def cliente_conectado(self, _client: object = None) -> None:
        """Volvió alguien: cancela la cuenta regresiva en curso, si la hay."""
        self.cancelar()

    def cliente_desconectado(self, _client: object = None) -> None:
        """Arranca la cuenta regresiva (idempotente: no apila varias)."""
        if self._cuenta_regresiva is not None and not self._cuenta_regresiva.done():
            return
        self._cuenta_regresiva = asyncio.create_task(self._esperar_y_apagar(), name="gui-exit-watchdog")

    def cancelar(self) -> None:
        """Detiene la cuenta regresiva (reconexión, o apagado ya iniciado por otra vía)."""
        if self._cuenta_regresiva is not None:
            self._cuenta_regresiva.cancel()
            self._cuenta_regresiva = None

    async def _esperar_y_apagar(self) -> None:
        try:
            await asyncio.sleep(self._grace_seconds)
            # Re-chequeo AUTORITATIVO: el conteo al vencer es el que manda. Si
            # un cliente se reconectó sin que cliente_conectado() llegara a
            # cancelar (carrera), esto lo salva.
            if self._contar_clientes() > 0:
                logger.debug("Watchdog de cierre: hay clientes vivos al vencer la gracia, no se apaga")
                return
            logger.info(
                "Watchdog de cierre: sin clientes tras %.1fs, apagando la app",
                self._grace_seconds,
            )
            self._apagar()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Watchdog de cierre falló; la app sigue viva")


async def _teardown_runtime(runtime: Mapping[str, Any]) -> None:
    """Cierra el runtime de la GUI. Cada paso corre aunque el anterior falle.

    P1-1: esto era una cadena plana. ``AuthTokenManager.revoke()`` hace un
    ``unlink`` desnudo —sin el ``suppress(OSError)`` que sí protege a su
    ``generate()``— así que un ``PermissionError`` en Windows (antivirus,
    handle abierto) se propagaba y se llevaba puestos los dos pasos que
    realmente importan: ``runner.cleanup()``, que libera el 8765, y
    ``ctx.stop()``, que suelta DB y locks. El resultado es el WinError 10048
    del post-mortem entrando por otra puerta.

    Cada fallo se loguea con stack trace: el apagado no puede abortar, pero
    tampoco puede quedar en silencio.

    Está a nivel de módulo, y no como closure de ``run_nicegui``, para que sea
    importable desde los tests — la cadena vieja no tenía forma de probarse.
    """
    auth_manager = runtime.get("auth_manager")
    runner = runtime.get("aiohttp_runner")
    ctx = runtime.get("ctx")

    pasos: list[tuple[str, Callable[[], Awaitable[None] | None]]] = []
    if auth_manager is not None:
        pasos.append(("stop_rotation", auth_manager.stop_rotation))
        pasos.append(("revoke", auth_manager.revoke))
    if runner is not None:
        pasos.append(("aiohttp runner cleanup", runner.cleanup))
    if ctx is not None:
        pasos.append(("AppContext stop", ctx.stop))

    for nombre, paso in pasos:
        try:
            resultado = paso()
            if inspect.isawaitable(resultado):
                await resultado
        except Exception:
            logger.exception("Paso de apagado '%s' falló; se continúa con los siguientes", nombre)


def run_nicegui(
    args,
    *,
    port: int,
    title: str,
    show: bool = True,
    exit_on_last_client: bool = False,
) -> None:
    """Start the NiceGUI server. Bootstraps AppContext and calls ui.run().

    ``exit_on_last_client`` arma el :class:`ExitWatchdog`: al irse el último
    cliente y no volver dentro del período de gracia, se dispara
    ``app.shutdown()``. Default ``False`` a propósito — el modo web es un
    SERVIDOR y no puede morirse porque se fue el último navegador; solo el
    modo GUI (el .exe de escritorio) lo activa.
    """
    from nicegui import app, ui

    config_path = _resolve_config_path_static(args)

    setup_app()

    _runtime: dict[str, object] = {}

    async def _bootstrap() -> None:
        if "ctx" in _runtime:
            return
        # Ítem 4 sub-3: la ruta GUI no pasa por __main__._main, así que instala
        # acá su propio loop-exception-handler (corre dentro del loop de NiceGUI)
        # para que las excepciones async no manejadas fluyan por el pipeline
        # JSON + redacción en vez del stderr por defecto de asyncio.
        install_loop_exception_handler()
        ctx = await start_full(args)
        _runtime["ctx"] = ctx

        # M-01.1: el supervisor comparte el DatabaseLifecycleManager del
        # AppContext para journal/locks/DLQ (shutdown coordinado + pragmas).
        supervisor = SupervisorAgent(
            hitl_guard=ctx.hitl,
            lifecycle=ctx.lifecycle.manager,
            path_validator=ctx.sandbox_validator,
            # C2: compartir el único NetworkGateway del AppContext (misma caché DNS
            # pinning + reglas de egress que el router/tools), sin duplicar política.
            gateway=ctx.network.gateway,
            loot_runner=ctx.vfs_loot_runner,
            # La GUI es el path productivo: nunca degrada a un subprocess LOOT
            # standalone si el bridge/broker no quedo disponible.
            require_vfs=True,
            # Fase 1 AI-assisted: callable lazy que resuelve router/sesión del
            # AppContext EN CADA llamada (el router se monta en start_full,
            # después de construir el supervisor; el hot-swap de provider se ve
            # sin recablear). Stack lock-only → el advisor degrada a manual_only.
            patch_advisor_llm=ctx.make_patch_advisor_llm(),
        )
        _runtime["supervisor"] = supervisor

        # Phase 1: bridge the supervisor's telemetry onto the GUI store so the
        # Vitalidad bars + header HUD render real CPU/RAM/GPU. subscribe() only
        # appends to the bus' subscription list, so it is safe to register before
        # supervisor.start() boots the bus.
        store = get_store()
        supervisor.event_bus.subscribe("system.telemetry.*", _make_telemetry_store_bridge(store))

        # Fase 2: route destructive-tool (Ritual) approvals to the GUI so the
        # "Modo local" toggle / Aprobar-Denegar modal can satisfy the HITL gate.
        _install_gui_hitl_bridge(ctx, store)

        ctx._track_task(supervisor.start(), name="supervisor-daemon")
        ctx._track_task(_gui_logic_loop(ctx), name="gui-logic-loop")
        ctx._track_task(_gui_mod_update_loop(ctx), name="gui-mod-update")

        # Phase 1: one-shot environment scan → Ritual availability (LOOT/SSEEdit/…).
        # Seeded from the user's configured paths so manual setups report tools as
        # installed (follow-up #2 / #209) rather than relying on auto-detection.
        ctx._track_task(_run_environment_scan(_build_environment_scanner(ctx), store), name="gui-env-scan")

        # aiohttp sub-server for /api/chat + /ws/ui (GUI↔daemon chat).
        # Runs on 8765 so external scripts and AgentCommunicationClient
        # can reach the chat endpoint without conflicting with NiceGUI.
        from aiohttp import web as aiohttp_web

        from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager
        from sky_claw.antigravity.web.app import WebApp

        auth_manager = AuthTokenManager()
        await asyncio.to_thread(auth_manager.generate)
        await auth_manager.start_rotation()
        _runtime["auth_manager"] = auth_manager

        web_app_inst = WebApp(router=ctx.router, session=ctx.session, auth_manager=auth_manager)
        aiohttp_app = web_app_inst.create_app()
        runner = aiohttp_web.AppRunner(aiohttp_app)
        await runner.setup()
        site = aiohttp_web.TCPSite(runner, "127.0.0.1", 8765)
        await site.start()
        _runtime["aiohttp_runner"] = runner
        logger.info("aiohttp API server started on 127.0.0.1:8765 (/api/chat)")

        set_runtime_context(app_context=ctx, config_path=config_path, supervisor=supervisor)
        logger.info("Sky-Claw Daemon Core initialised; runtime context published.")

    app.on_startup(_bootstrap)

    if exit_on_last_client:
        from nicegui import Client

        def _contar_clientes_vivos() -> int:
            # ``has_socket_connection`` y no ``len(Client.instances)``: NiceGUI
            # conserva la instancia un rato después del disconnect, así que
            # contar instancias daría clientes fantasma y el apagado nunca
            # se dispararía.
            return sum(1 for c in Client.instances.values() if c.has_socket_connection)

        watchdog = ExitWatchdog(
            grace_seconds=_EXIT_WATCHDOG_GRACE_SECONDS,
            contar_clientes=_contar_clientes_vivos,
            apagar=app.shutdown,
        )
        _runtime["exit_watchdog"] = watchdog
        app.on_connect(watchdog.cliente_conectado)
        app.on_disconnect(watchdog.cliente_desconectado)
        logger.info(
            "Watchdog de cierre armado: la app se apaga %.0fs después de que se vaya el último cliente",
            _EXIT_WATCHDOG_GRACE_SECONDS,
        )

    async def _shutdown() -> None:
        # Frena la cuenta regresiva antes del teardown: si el apagado lo
        # disparó otra vía, una task del watchdog viva sería una task de fondo
        # más que drenar (y llamaría app.shutdown() sobre un server que ya está
        # cerrando).
        reloj = _runtime.get("exit_watchdog")
        if isinstance(reloj, ExitWatchdog):
            reloj.cancelar()
        await _teardown_runtime(_runtime)

    app.on_shutdown(_shutdown)

    ui.run(title=title, dark=True, show=show, reload=False, port=port, host="127.0.0.1")
