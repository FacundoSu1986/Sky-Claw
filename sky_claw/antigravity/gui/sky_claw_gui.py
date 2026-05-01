"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          SKY-CLAW GUI v2.1 — PUNTO DE ENSAMBLAJE (ASSEMBLY POINT)          ║
║      Refactoring Fase 4: MVVM completo con Inyección de Dependencias       ║
╚══════════════════════════════════════════════════════════════════════════════╝

Arquitectura MVVM:
  • VIEW:        sky_claw.antigravity.gui.views          (componentes visuales puros)
  • VIEWMODEL:   ReactiveState               (variables NiceGUI reactivas)
  • MODEL:       sky_claw.antigravity.gui.models         (AppState puro, thread-safe)
  • CONTROLLERS: sky_claw.antigravity.gui.controllers    (lógica de negocio aislada)

Este archivo es un Dependency Injector: instancia estado, controladores y vistas.
NO contiene lógica de negocio propia.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nicegui import app, ui

# ── Core imports ───────────────────────────────────────────────────────────────
from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.antigravity.gui.agent_communication import AgentCommunicationClient

# ── Controller imports ─────────────────────────────────────────────────────────
from sky_claw.antigravity.gui.controllers import ChatController, ModController, NavigationController
from sky_claw.antigravity.gui.gui_event_adapter import EventBus, EventType, SkyClawEvent, event_bus
from sky_claw.antigravity.gui.models.app_state import AppState, get_app_state

# ── View imports ───────────────────────────────────────────────────────────────
from sky_claw.antigravity.gui.views import render_dashboard

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN Y CONSTANTES
# ═══════════════════════════════════════════════════════════════════════════════

# Resolve paths relative to this module
_CSS_PATH = Path(__file__).resolve().parent / "styles.css"
_ASSETS_PATH = Path(__file__).resolve().parent / "assets"


# ═══════════════════════════════════════════════════════════════════════════════
# CAPA DE ACCESO A DATOS — DB AGENT
# ═══════════════════════════════════════════════════════════════════════════════

_db_agent: DatabaseAgent | None = None


def get_db_agent() -> DatabaseAgent:
    """Lazy initializer — avoids module-level instantiation outside async context."""
    global _db_agent
    if _db_agent is None:
        _db_agent = DatabaseAgent()
    return _db_agent


# ═══════════════════════════════════════════════════════════════════════════════
# VIEWMODEL — REACTIVE STATE (NiceGUI variables)
# ═══════════════════════════════════════════════════════════════════════════════


class _ReactiveVar:
    """Thin wrapper that mimics the .get()/.set() API expected by ReactiveState.

    NiceGUI does NOT expose a public ``ui.core.variable`` API.
    Reactive bindings are achieved via ``.bind_text_from(obj, 'attr')`` against
    plain Python attributes or via ``ui.refreshable``.  Since the dashboard
    currently reads these values eagerly at render time (no declarative bind),
    a simple mutable box is sufficient.  When proper binding is needed, replace
    this with a dict-backed reactive store and call ``ui.update()`` after mutations.
    """

    __slots__ = ("_value",)

    def __init__(self, initial_value: Any) -> None:
        self._value = initial_value

    def get(self) -> Any:
        return self._value

    def set(self, value: Any) -> None:
        self._value = value


class ReactiveState:
    """
    ViewModel: variables reactivas NiceGUI. Se suscribe al EventBus para
    actualizar la UI en respuesta a eventos del dominio.

    NO contiene lógica de negocio — solo sincroniza estado con la capa visual.
    """

    def __init__(
        self,
        app_state: AppState | None = None,
        event_bus_instance: EventBus | None = None,
    ) -> None:
        self._app_state = app_state or get_app_state_instance()

        # Variables UI reactivas — backed by plain _ReactiveVar boxes.
        # Call .get() to read, .set(value) to write (same API as before).
        self.active_mods = _ReactiveVar(0)
        self.pending_updates = _ReactiveVar(0)
        self.conflicts_count = _ReactiveVar(0)
        self.storage_used = _ReactiveVar(0.0)
        self.is_agent_connected = _ReactiveVar(False)
        self.is_loading = _ReactiveVar(False)

        # Suscribirse al EventBus para sincronizar la UI
        if event_bus_instance:
            event_bus_instance.subscribe(EventType.MOD_ADDED, self.handle_mod_added)
            event_bus_instance.subscribe(EventType.CONFLICT_DETECTED, self.handle_conflict_detected)
            event_bus_instance.subscribe(EventType.LLM_RESPONSE, self._handle_llm_notification)
            event_bus_instance.subscribe(EventType.AGENT_STATUS_CHANGE, self._handle_agent_status)

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def is_thinking(self) -> bool:
        """Lee el estado de procesamiento desde AppState."""
        return self._app_state.is_thinking

    @property
    def wizard_step(self) -> int:
        """Expone wizard_step de AppState."""
        return self._app_state.wizard_step

    @wizard_step.setter
    def wizard_step(self, value: int) -> None:
        self._app_state.wizard_step = value

    # ── Chat helpers ────────────────────────────────────────────────────────────

    def add_chat_message(self, role: str, content: str) -> None:
        self._app_state.add_chat_message(role, content)

    def clear_chat_messages(self) -> None:
        self._app_state.clear_chat_messages()

    def get_chat_messages(self) -> list[dict[str, str]]:
        return self._app_state._chat_messages.copy()

    def get_message_count(self) -> int:
        return self._app_state.get_message_count()

    # ── DB sync ─────────────────────────────────────────────────────────────────

    async def update_from_db(self) -> None:
        """Async pull from consolidated DB para inicializar variables reactivas."""
        try:
            mods = await get_db_agent().get_mods(status="active")
            conflicts = await get_db_agent().get_conflicts(resolved=False)
            self.active_mods.set(len(mods))
            self.conflicts_count.set(len(conflicts))
            self.pending_updates.set(sum(1 for m in mods if m.get("needs_update", False)))
            total_size = sum(m.get("size_mb", 0) for m in mods)
            self.storage_used.set(round(total_size / 1024, 1))
        except Exception as exc:
            logging.error("Error actualizando estado desde DB: %s", exc)

    # ── UI event handlers ───────────────────────────────────────────────────────

    def notify(self, message: str, type: str = "info") -> None:
        ui.notify(message, type=type)

    def handle_mod_added(self, event: SkyClawEvent) -> None:
        """Actualiza contador de mods activos y notifica al usuario."""
        self.active_mods.set(self.active_mods.get() + 1)
        self.notify(f"Mod '{event.data.get('name')}' added!", type="positive")

    def handle_conflict_detected(self, event: SkyClawEvent) -> None:
        """Actualiza contador de conflictos y notifica al usuario."""
        self.conflicts_count.set(self.conflicts_count.get() + 1)
        self.notify(
            f"Conflict: {event.data.get('description', 'Unknown')}",
            type="warning",
        )

    def on_connection_change(self, connected: bool) -> None:
        self.is_agent_connected.set(connected)

    def _handle_llm_notification(self, event: SkyClawEvent) -> None:
        """Muestra notificación UI cuando llega respuesta del LLM."""
        response = event.data.get("response", event.data.get("text", ""))
        self.notify(f"AI: {response[:80]}...", type="info")

    def _handle_agent_status(self, event: SkyClawEvent) -> None:
        """Actualiza variable reactiva is_loading al cambiar estado del agente."""
        self.is_loading.set(event.data.get("is_thinking", False))


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCES — ASSEMBLY POINT
# ═══════════════════════════════════════════════════════════════════════════════

_app_state: AppState | None = None
_state: ReactiveState | None = None
_chat_controller: ChatController | None = None
_mod_controller: ModController | None = None
_nav_controller: NavigationController | None = None


def get_app_state_instance() -> AppState:
    """Lazy initializer para AppState centralizado."""
    global _app_state
    if _app_state is None:
        _app_state = get_app_state()
    return _app_state


def get_state() -> ReactiveState:
    """Lazy initializer — debe llamarse dentro del contexto NiceGUI."""
    global _state, _app_state
    if _state is None:
        if _app_state is None:
            _app_state = get_app_state_instance()
        _state = ReactiveState(app_state=_app_state, event_bus_instance=event_bus)
    return _state


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT COMMUNICATION — WebSocket bridge al daemon
# ═══════════════════════════════════════════════════════════════════════════════


def _handle_daemon_message(data: dict[str, Any]) -> None:
    """Traduce mensajes WS del daemon a eventos EventBus."""
    msg_type = data.get("type", "")

    if msg_type == "agent_result":
        action = data.get("action", "")
        if action == "install_complete":
            event_bus.publish(
                SkyClawEvent(
                    type=EventType.MOD_ADDED,
                    data=data.get("payload", {}),
                    source="daemon",
                )
            )
        elif action == "conflict_found":
            event_bus.publish(
                SkyClawEvent(
                    type=EventType.CONFLICT_DETECTED,
                    data=data.get("payload", {}),
                    source="daemon",
                )
            )
    elif msg_type == "response":
        event_bus.publish(
            SkyClawEvent(
                type=EventType.LLM_RESPONSE,
                data=data.get("payload", {}),
                source="daemon",
            )
        )
    elif msg_type == "broadcast":
        event_bus.publish(
            SkyClawEvent(
                type=EventType.EVENT_BROADCAST,
                data=data,
                source="daemon",
            )
        )


_DEFAULT_DAEMON_WS_URL = "ws://localhost:8765/ws/ui"
DAEMON_WS_URL = _DEFAULT_DAEMON_WS_URL

agent_client: AgentCommunicationClient | None = None


def get_agent_client() -> AgentCommunicationClient:
    """Lazy initializer — deferred until NiceGUI context is ready."""
    global agent_client
    if agent_client is None:
        agent_client = AgentCommunicationClient(
            daemon_url=DAEMON_WS_URL,
            on_message=_handle_daemon_message,
            on_connection_change=get_state().on_connection_change,
        )
    return agent_client


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PAGE — Orquestador Delgado (Dependency Injector)
# ═══════════════════════════════════════════════════════════════════════════════


@ui.page("/")
def main_page():
    """
    Página principal — solo ensambla estado, controladores y vistas.
    No contiene lógica de negocio: delega todo a los controladores.
    """
    ui.dark_mode().enable()

    ui.add_head_html("""
        <link rel="stylesheet" href="/static/styles.css">
        <script>
            const sounds = {
                'click': 'https://www.soundjay.com/buttons/button-16.mp3',
                'hover': 'https://www.soundjay.com/buttons/button-20.mp3',
                'success': 'https://www.soundjay.com/buttons/button-09.mp3'
            };
            function playSkyrimSound(type) {
                const audio = new Audio(sounds[type]);
                audio.volume = 0.2;
                audio.play();
            }
        </script>
    """)

    state = get_state()

    stats = {
        "active_mods": state.active_mods,
        "pending_updates": state.pending_updates,
        "conflicts_count": state.conflicts_count,
        "storage_used": state.storage_used,
    }

    # Datos de preview (sample hasta que DB async esté disponible en la vista)
    sample_mods = [
        {"name": "Skyrim 202X", "status": "active", "size_mb": 2400},
        {"name": "Immersive Armors", "status": "active", "size_mb": 156},
        {"name": "Lux Via", "status": "update", "size_mb": 89},
        {"name": "Ordinator", "status": "conflict", "size_mb": 45},
    ]

    chat_messages = _chat_controller.prepare_messages_for_view(state._app_state._chat_messages)

    # Inyección de dependencias: métodos de controladores como callbacks de vistas
    callbacks = {
        "on_send_message": lambda msg: asyncio.create_task(_chat_controller.handle_send_message(msg)),
        "on_view_all_mods": _mod_controller.handle_view_all_mods,
        "on_mod_click": _mod_controller.handle_mod_click,
        "on_navigate": _nav_controller.handle_navigation,
        "on_cta_primary": _nav_controller.handle_cta_primary,
        "on_cta_secondary": _nav_controller.handle_cta_secondary,
        "on_feature_click": _nav_controller.handle_feature_click,
    }

    render_dashboard(
        stats=stats,
        mods=sample_mods,
        chat_messages=chat_messages,
        is_thinking=state.is_thinking,
        callbacks=callbacks,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# APP SETUP & ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


def setup_app() -> None:
    """Configura la aplicación e instancia controladores con DI."""
    global _chat_controller, _mod_controller, _nav_controller

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Serve CSS and Assets
    app.add_static_files("/static", str(_CSS_PATH.parent))
    app.add_static_files("/assets", str(_ASSETS_PATH))

    # Initialize DB and seed sample data
    async def _init():
        await get_db_agent().init_db()
        mods = await get_db_agent().get_mods()
        if not mods:
            await get_db_agent().add_mod("Skyrim 202X", "9.0", 2400, "Nexusmods")
            await get_db_agent().add_mod("Immersive Armors", "8.1", 156, "Nexusmods")
            await get_db_agent().add_mod("Lux Via", "1.5", 89, "Nexusmods")
        await get_state().update_from_db()

    app.on_startup(_init)

    # Start event bus
    event_bus.start()

    # Instanciar controladores con Inyección de Dependencias
    app_state = get_app_state_instance()
    _chat_controller = ChatController(
        app_state=app_state,
        event_bus=event_bus,
        agent_client_factory=get_agent_client,
    )
    _mod_controller = ModController(app_state=app_state, event_bus=event_bus)
    _nav_controller = NavigationController(app_state=app_state, event_bus=event_bus)

    # Start agent communication client — deferred until NiceGUI loop is live
    app.on_startup(lambda: get_agent_client().start())

    # Cleanup on shutdown
    async def _cleanup():
        event_bus.stop()
        await get_agent_client().stop()

    app.on_shutdown(_cleanup)


def cleanup() -> None:
    event_bus.stop()


if __name__ in {"__main__", "__mp_main__"}:
    setup_app()

    try:
        ui.run(
            title="Sky-Claw — Mod Manager v2.1",
            port=8080,
            host="127.0.0.1",
            reload=True,
            show=True,
            dark=True,
            favicon="🔮",
        )
    except KeyboardInterrupt:
        cleanup()
