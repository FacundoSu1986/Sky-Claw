"""Sky-Claw NiceGUI Forge — assembly point.

Single-source-of-truth reactive composition: the wizard and the dashboard
share the process-wide :class:`ReactiveStore`, so a Wizard submission
that flips ``first_run`` immediately re-renders the page in the same
session via ``@ui.refreshable``.

Architecture
============
* MODEL  ``models.app_state.AppState`` (pure, thread-safe)
* STATE  ``state.reactive_store.ReactiveStore`` (subscribers + ui.refreshable)
* VIEWMODEL  ``ReactiveState`` (proxies that read/write the store)
* CONTROLLERS  ``controllers.*`` (business logic)
* VIEWS  ``views.*`` (pure visual code)

The module exposes :func:`setup_app` (registers controllers, EventBus,
static assets) and :func:`set_runtime_context` (called once by the entry
mode with the live ``AppContext`` so the page can resolve the config
path).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nicegui import app, ui

from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.antigravity.gui.agent_communication import AgentCommunicationClient
from sky_claw.antigravity.gui.controllers import (
    ChatController,
    ModController,
    NavigationController,
)
from sky_claw.antigravity.gui.controllers.ritual_runner import (
    STORE_KEY_PENDING_HITL,
    STORE_KEY_RITUAL_FEEDBACK,
    run_ritual,
    run_ritual_install,
)
from sky_claw.antigravity.gui.gui_event_adapter import (
    EventBus,
    EventType,
    SkyClawEvent,
    event_bus,
)
from sky_claw.antigravity.gui.gui_helpers import _load_css
from sky_claw.antigravity.gui.models.app_state import AppState, enrich_conflicts, get_app_state
from sky_claw.antigravity.gui.setup_wizard import SetupWizardModal
from sky_claw.antigravity.gui.state import ReactiveStore, get_store
from sky_claw.antigravity.gui.task_tracking import create_tracked_task
from sky_claw.antigravity.gui.views import render_dashboard
from sky_claw.antigravity.gui.views.forge_dashboard import (
    STORE_KEY_ENV,
    _hitl_modal_panel,
    _ritual_feedback_panel,
    modo_local_enabled,
)
from sky_claw.config import Config

logger = logging.getLogger(__name__)


def _gui_dir() -> Path:
    """Resolve the GUI asset directory, handling PyInstaller onefile bundles.

    In a frozen exe the Python modules live inside the PYZ archive (no real
    directory on disk), so ``Path(__file__).parent`` points at a path that
    does not exist and ``add_static_files`` raises. Mirror the resolver in
    :mod:`sky_claw.antigravity.web.app` and read the bundled assets from
    ``sys._MEIPASS`` instead (these are declared in ``sky_claw.spec`` datas).
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "sky_claw" / "antigravity" / "gui"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


_CSS_PATH = _gui_dir() / "styles.css"
_ASSETS_PATH = _gui_dir() / "assets"


# ── Runtime context ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class RuntimeContext:
    """Live runtime references injected by the launching mode."""

    app_context: Any
    config_path: Path
    supervisor: Any | None = None


_RUNTIME_KEY = "runtime"
_FIRST_RUN_KEY = "first_run"


def set_runtime_context(
    app_context: Any,
    config_path: Path,
    supervisor: Any | None = None,
) -> None:
    """Publish the live AppContext + config path into the reactive store."""
    store = get_store()
    store.set(
        _RUNTIME_KEY,
        RuntimeContext(app_context=app_context, config_path=config_path, supervisor=supervisor),
    )


def get_runtime_context() -> RuntimeContext | None:
    return get_store().get(_RUNTIME_KEY)


# ── Database (UI-side seed agent) ─────────────────────────────────────────────

_db_agent: DatabaseAgent | None = None


def get_db_agent() -> DatabaseAgent:
    """Lazy initialiser — avoids module-level instantiation outside async context."""
    global _db_agent
    if _db_agent is None:
        _db_agent = DatabaseAgent()
    return _db_agent


# ── Reactive proxies ──────────────────────────────────────────────────────────


class _StoreProxy:
    """Adapts ``store.get(key)/store.set(key, value)`` to ``.get()/.set(v)``.

    Preserves the API previously exposed by ``_ReactiveVar`` so existing
    call sites (counters, flags) keep working without changes.  The
    ``_value`` property lets NiceGUI's ``bind_text_from(var, "_value")``
    observe the current value via ``getattr`` polling.
    """

    __slots__ = ("_key", "_store")

    def __init__(self, store: ReactiveStore, key: str, initial: Any) -> None:
        self._store = store
        self._key = key
        if store.get(key) is None:
            store.set(key, initial)

    @property
    def _value(self) -> Any:
        return self._store.get(self._key)

    def get(self) -> Any:
        return self._store.get(self._key)

    def set(self, value: Any) -> None:
        self._store.set(self._key, value)


class ReactiveState:
    """ViewModel: proxies the reactive store and subscribes to the EventBus."""

    def __init__(
        self,
        app_state: AppState | None = None,
        event_bus_instance: EventBus | None = None,
        store: ReactiveStore | None = None,
    ) -> None:
        self._app_state = app_state or get_app_state_instance()
        self._store = store or get_store()

        self.active_mods = _StoreProxy(self._store, "active_mods", 0)
        self.pending_updates = _StoreProxy(self._store, "pending_updates", 0)
        self.conflicts_count = _StoreProxy(self._store, "conflicts_count", 0)
        self.storage_used = _StoreProxy(self._store, "storage_used", 0.0)
        self.is_agent_connected = _StoreProxy(self._store, "is_agent_connected", False)
        self.is_loading = _StoreProxy(self._store, "is_loading", False)
        # Parte 5: navegación y selección (escritos por los handlers de eventos)
        self.active_section = _StoreProxy(self._store, "active_section", "Dashboard")
        self.selected_mod = _StoreProxy(self._store, "selected_mod", None)

        if event_bus_instance:
            event_bus_instance.subscribe(EventType.MOD_ADDED, self.handle_mod_added)
            event_bus_instance.subscribe(EventType.CONFLICT_DETECTED, self.handle_conflict_detected)
            event_bus_instance.subscribe(EventType.LLM_RESPONSE, self._handle_llm_notification)
            event_bus_instance.subscribe(EventType.AGENT_STATUS_CHANGE, self._handle_agent_status)
            event_bus_instance.subscribe(EventType.NAVIGATION_REQUESTED, self._handle_navigation_requested)
            event_bus_instance.subscribe(EventType.MOD_SELECTED, self._handle_mod_selected)

    @property
    def is_thinking(self) -> bool:
        return self._app_state.is_thinking

    @property
    def wizard_step(self) -> int:
        return self._app_state.wizard_step

    @wizard_step.setter
    def wizard_step(self, value: int) -> None:
        self._app_state.wizard_step = value

    def add_chat_message(self, role: str, content: str) -> None:
        self._app_state.add_chat_message(role, content)

    def clear_chat_messages(self) -> None:
        self._app_state.clear_chat_messages()

    def get_chat_messages(self) -> list[dict[str, str]]:
        return self._app_state._chat_messages.copy()

    def get_message_count(self) -> int:
        return self._app_state.get_message_count()

    async def update_from_db(self) -> None:
        try:
            all_mods = await get_db_agent().get_mods()
            active = [m for m in all_mods if m.get("status") == "active"]
            self.active_mods.set(len(active))
            self.pending_updates.set(sum(1 for m in active if m.get("needs_update", False)))
            total_size = sum(m.get("size_mb", 0) for m in active)
            self.storage_used.set(round(total_size / 1024, 1))
        except Exception as exc:
            logger.error("Error actualizando estado desde DB: %s", exc)
        await self.refresh_conflicts()

    async def refresh_conflicts(self) -> None:
        """Refresca SOLO los datos de conflictos (contador + lista enriquecida).

        No toca ``active_mods``/``pending_updates``/``storage_used``: con un
        registry MO2 vivo esos los escribe ``_gui_mod_update_loop``, y pisarlos
        desde la DB GUI haría saltar las stats a valores viejos al resolver un
        conflicto (review Codex en #220). Los mods se leen solo para mapear
        nombres en ``enrich_conflicts``.
        """
        try:
            all_mods = await get_db_agent().get_mods()
            conflicts = await get_db_agent().get_conflicts(resolved=False)
            self.conflicts_count.set(len(conflicts))
            self._store.set("conflicts_list", enrich_conflicts(conflicts, all_mods))
        except Exception as exc:
            logger.error("Error refrescando conflictos desde DB: %s", exc)

    def notify(self, message: str, type: str = "info") -> None:
        ui.notify(message, type=type)

    def handle_mod_added(self, event: SkyClawEvent) -> None:
        self.active_mods.set(self.active_mods.get() + 1)
        self.notify(f"Mod '{event.data.get('name')}' added!", type="positive")

    def handle_conflict_detected(self, event: SkyClawEvent) -> None:
        # Refresca contador Y lista desde la DB (misma fuente) para que el badge
        # y la pantalla de Conflictos no diverjan (review Codex en #220). Es
        # seguro crear la tarea acá: el EventBus despacha los callbacks con
        # loop.call_soon_threadsafe, así que corren dentro del loop de NiceGUI.
        create_tracked_task(self.refresh_conflicts(), name="gui-conflicts-refresh")
        self.notify(
            f"Conflict: {event.data.get('description', 'Unknown')}",
            type="warning",
        )

    def on_connection_change(self, connected: bool) -> None:
        self.is_agent_connected.set(connected)

    def _handle_llm_notification(self, event: SkyClawEvent) -> None:
        response = event.data.get("response", event.data.get("text", ""))
        self.notify(f"AI: {response[:80]}...", type="info")

    def _handle_agent_status(self, event: SkyClawEvent) -> None:
        self.is_loading.set(event.data.get("is_thinking", False))

    def _handle_navigation_requested(self, event: SkyClawEvent) -> None:
        """Parte 5: el store re-renderiza main_page (sidebar activo incluido)."""
        section = event.data.get("section")
        if section:
            self.active_section.set(section)

    def _handle_mod_selected(self, event: SkyClawEvent) -> None:
        """Parte 5: refleja la selección para la futura vista de detalle."""
        name = event.data.get("name")
        if name:
            self.selected_mod.set(name)
            self.notify(f"Mod seleccionado: {name}", type="info")


# ── Singletons ────────────────────────────────────────────────────────────────

_app_state: AppState | None = None
_state: ReactiveState | None = None
_chat_controller: ChatController | None = None
_mod_controller: ModController | None = None
_nav_controller: NavigationController | None = None


def get_app_state_instance() -> AppState:
    global _app_state
    if _app_state is None:
        _app_state = get_app_state()
    return _app_state


def get_state() -> ReactiveState:
    global _state, _app_state
    if _state is None:
        if _app_state is None:
            _app_state = get_app_state_instance()
        _state = ReactiveState(
            app_state=_app_state,
            event_bus_instance=event_bus,
            store=get_store(),
        )
    return _state


# ── Daemon WebSocket bridge ───────────────────────────────────────────────────


def _handle_daemon_message(data: dict[str, Any]) -> None:
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
    global agent_client
    if agent_client is None:
        agent_client = AgentCommunicationClient(
            daemon_url=DAEMON_WS_URL,
            on_message=_handle_daemon_message,
            on_connection_change=get_state().on_connection_change,
        )
    return agent_client


# ── Wizard / Dashboard gate ───────────────────────────────────────────────────


def _is_first_run(config_path: Path) -> bool:
    """Resolve ``first_run`` once and mirror it in the reactive store."""
    store = get_store()
    cached = store.get(_FIRST_RUN_KEY)
    if cached is not None:
        return bool(cached)
    try:
        cfg = Config(config_path)
        first = bool(cfg._data.get("first_run", True))
    except Exception:
        logger.exception("Could not read config at %s; assuming first_run=True", config_path)
        first = True
    store.set(_FIRST_RUN_KEY, first)
    return first


async def _on_wizard_complete() -> None:
    """Wizard ``on_complete`` callback: flip the flag and refresh the page."""
    get_store().set(_FIRST_RUN_KEY, False)
    ui.notify("Configuración guardada — bienvenido a Sky-Claw", type="positive")


# ── Sección Ajustes ───────────────────────────────────────────────────────────

_IDENTITY_LOADED_KEY = "identity_loaded"


def _ensure_identity_loaded(config_path: Path) -> None:
    """Carga nombre/rol del usuario desde el TOML a AppState (una sola vez).

    Cierra A3 de punta a punta: el header es data-driven desde AppState y acá
    AppState se puebla desde la config persistida (editable en Ajustes).
    """
    store = get_store()
    if store.get(_IDENTITY_LOADED_KEY):
        return
    try:
        cfg = Config(config_path)
        state = get_app_state_instance()
        state.user_display_name = str(cfg._data.get("user_display_name") or state.user_display_name)
        state.user_role = str(cfg._data.get("user_role") or state.user_role)
    except Exception:
        logger.exception("No se pudo cargar la identidad desde %s; se usan los defaults", config_path)
    store.set(_IDENTITY_LOADED_KEY, True)


def save_settings(
    config_path: Path,
    payload: dict[str, str],
    app_state: AppState | None = None,
) -> str | None:
    """Valida y persiste los Ajustes; devuelve el mensaje de error o ``None``.

    Convenciones (mismas del hub de configuración clásico y el wizard):
    - Secretos ("" = no cambiar): solo los valores no vacíos van a keyring.
    - Provider / chat id / identidad van al TOML vía ``Config.save()``.
    - La identidad se refleja en ``AppState`` para que el header (A3) la pinte
      en el próximo render sin reiniciar.
    """
    import keyring

    from sky_claw.antigravity.gui.setup_wizard import validate_credentials

    provider = (payload.get("llm_provider") or "").strip()
    api_key = (payload.get("llm_api_key") or "").strip()
    telegram_token = (payload.get("telegram_bot_token") or "").strip()
    telegram_chatid = (payload.get("telegram_chat_id") or "").strip()

    error = validate_credentials(provider, api_key, telegram_token, telegram_chatid, require_api_key=False)
    if error is not None:
        return error

    # Cambiar a un provider cloud requiere su clave: la tipeada ahora o el slot
    # {provider}_api_key ya guardado. Sin esto, AppContext caería a la
    # llm_api_key genérica — la del provider ANTERIOR — y el arranque fallaría
    # (review Codex en #221).
    if provider in ("anthropic", "deepseek", "openai") and not api_key:
        try:
            has_slot = bool(keyring.get_password("sky_claw", f"{provider}_api_key"))
        except Exception:
            has_slot = False
        if not has_slot:
            return f"Ingresá la API Key de {provider}: no hay una clave guardada para ese proveedor"

    # Secretos → keyring, solo si el usuario tipeó algo (vacío = conservar).
    secrets_map = {
        "llm_api_key": api_key,
        f"{provider}_api_key": api_key,
        "nexus_api_key": (payload.get("nexus_api_key") or "").strip(),
        "search_api_key": (payload.get("search_api_key") or "").strip(),
        "telegram_bot_token": telegram_token,
    }
    try:
        for key, value in secrets_map.items():
            if value:
                keyring.set_password("sky_claw", key, value)
    except Exception as exc:
        logger.exception("Error guardando secretos en keyring")
        return f"Error guardando claves: {exc}"

    # Config (TOML): provider + chat id + identidad.
    name = (payload.get("user_display_name") or "").strip()
    role = (payload.get("user_role") or "").strip()
    try:
        cfg = Config(config_path)
        cfg._data["llm_provider"] = provider
        # El chat id NO es secreto: se persiste siempre, vacío incluido, para
        # poder quitar el destino de notificaciones desde Ajustes (Codex #221).
        cfg._data["telegram_chat_id"] = telegram_chatid.replace("@", "")
        if name:
            cfg._data["user_display_name"] = name
        if role:
            cfg._data["user_role"] = role
        cfg.save()
    except Exception as exc:
        logger.exception("Error guardando configuración de Ajustes")
        return f"Error guardando configuración: {exc}"

    state = app_state or get_app_state_instance()
    if name:
        state.user_display_name = name
    if role:
        state.user_role = role
    return None


def _build_settings_data(config_path: Path) -> dict[str, Any]:
    """Arma los datos que la pantalla de Ajustes muestra (view pura).

    Provider/chat id salen del TOML; el estado de cada secreto (badge
    Configurada/Sin clave) se consulta en keyring sin exponer el valor.
    """
    import keyring

    app_state = get_app_state_instance()
    provider, chat_id = "deepseek", ""
    try:
        cfg = Config(config_path)
        provider = str(cfg._data.get("llm_provider") or "deepseek")
        chat_id = str(cfg._data.get("telegram_chat_id") or "")
    except Exception:
        logger.exception("No se pudo leer la config para Ajustes; se muestran defaults")

    def _configured(key: str) -> bool:
        try:
            return bool(keyring.get_password("sky_claw", key))
        except Exception:
            return False

    key_status: dict[str, bool] = {
        key: _configured(key) for key in ("nexus_api_key", "search_api_key", "telegram_bot_token")
    }
    # El resto del sistema considera configurado el slot {provider}_api_key
    # (p. ej. app_context._is_configured), así que el badge también lo mira —
    # no solo la clave genérica llm_api_key (review Copilot en #221).
    key_status["llm_api_key"] = _configured("llm_api_key") or _configured(f"{provider}_api_key")
    return {
        "identity": {"name": app_state.user_display_name, "role": app_state.user_role},
        "provider": provider,
        "telegram_chat_id": chat_id,
        "key_status": key_status,
    }


@ui.refreshable
def main_page() -> None:
    """Single page that gates between Wizard and Dashboard via the store."""
    ui.dark_mode().enable()
    # Wire the Nordic theme on BOTH the wizard and the dashboard. Previously only
    # the wizard called _load_css(), so the dashboard rendered with the bare
    # Quasar defaults — the entire Skyrim stylesheet was absent. Idempotent, so
    # the @ui.refreshable re-runs don't stack duplicate <style> tags.
    _load_css()

    runtime = get_runtime_context()
    if runtime is None:
        ui.label("Inicializando contexto…").classes("p-8 text-lg")
        return

    if _is_first_run(runtime.config_path):
        wizard = SetupWizardModal(
            config_path=runtime.config_path,
            on_complete=_on_wizard_complete,
        )
        wizard.build()
        return

    state = get_state()
    stats = {
        "active_mods": state.active_mods,
        "pending_updates": state.pending_updates,
        "conflicts_count": state.conflicts_count,
        "storage_used": state.storage_used,
    }

    # Use live mod data from the store (written by _gui_mod_update_loop).
    # Fall back to placeholder only until the first DB refresh arrives.
    mods: list[dict[str, Any]] = get_store().get("mods_list") or [
        {"name": "Skyrim 202X", "status": "active", "size_mb": 2400},
        {"name": "Immersive Armors", "status": "active", "size_mb": 156},
        {"name": "Lux Via", "status": "update", "size_mb": 89},
        {"name": "Ordinator", "status": "conflict", "size_mb": 45},
    ]

    chat_messages = (
        _chat_controller.prepare_messages_for_view(state._app_state._chat_messages)
        if _chat_controller is not None
        else []
    )

    callbacks: dict[str, Any] = {}
    if _chat_controller is not None:
        callbacks["on_send_message"] = lambda msg: create_tracked_task(
            _chat_controller.handle_send_message(msg), name="gui-send-message"
        )
    if _mod_controller is not None:
        callbacks["on_view_all_mods"] = _mod_controller.handle_view_all_mods
        callbacks["on_mod_click"] = _mod_controller.handle_mod_click
    if _nav_controller is not None:
        callbacks["on_navigate"] = _nav_controller.handle_navigation
        callbacks["on_cta_primary"] = _nav_controller.handle_cta_primary
        callbacks["on_cta_secondary"] = _nav_controller.handle_cta_secondary
        callbacks["on_feature_click"] = _nav_controller.handle_feature_click

        # A1: buscar desde el header guarda el término y navega a "Mods"; la
        # pantalla lo lee del store y pre-filtra la lista (reusa _filter_mods).
        def _on_search(query: str) -> None:
            get_store().set("mods_search_query", query)
            _nav_controller.handle_navigation("Mods")

        callbacks["on_search"] = _on_search

    # Fase 2: Rituales dispatch through the supervisor (HITL-gated); approvals are
    # answered from the GUI modal. Both are fire-and-forget tracked tasks.
    # Read THIS client's Modo local toggle at click time (the click handler has
    # client context); run_ritual arms it for just this dispatch.
    callbacks["on_ritual_run"] = lambda tool_key: create_tracked_task(
        run_ritual(
            tool_key,
            supervisor=runtime.supervisor,
            store=get_store(),
            auto_approve=modo_local_enabled(),
        ),
        name="gui-ritual-run",
    )

    # Follow-up C: the "Instalar" button (Ritual in "No instalado" state) downloads
    # the tool via ToolsInstaller. Download approval is parked in the GUI modal
    # (category="download") and never auto-approved by Modo local.
    callbacks["on_ritual_install"] = lambda tool_key: create_tracked_task(
        run_ritual_install(
            tool_key,
            app_context=runtime.app_context,
            store=get_store(),
        ),
        name="gui-ritual-install",
    )

    def _on_hitl_respond(request_id: str, approved: bool) -> None:
        guard = getattr(runtime.app_context, "hitl", None)
        if guard is not None:
            create_tracked_task(guard.respond(request_id, approved), name="gui-hitl-respond")

    callbacks["on_hitl_respond"] = _on_hitl_respond

    # Sección Conflictos: "Resolver" marca el conflicto como resuelto en la DB y
    # refresca SOLO los datos de conflictos (refresh_conflicts) — no las stats de
    # mods, que con registry vivo las escribe _gui_mod_update_loop (Codex #220).
    def _on_conflict_resolve(conflict_id: int) -> None:
        async def _resolve() -> None:
            await get_db_agent().resolve_conflict(conflict_id)
            await get_state().refresh_conflicts()

        create_tracked_task(_resolve(), name="gui-conflict-resolve")

    callbacks["on_conflict_resolve"] = _on_conflict_resolve

    # F5: "Detectar disputas" corre el escaneo liviano de assets del VFS
    # (AssetConflictDetector, sin xEdit) en un thread, persiste los pares
    # nuevos en la tabla conflicts y refresca la pantalla. El resultado se
    # informa por el toast de rituales (panel refreshable existente).
    def _on_conflict_scan() -> None:
        async def _scan() -> None:
            import asyncio

            from sky_claw.antigravity.core.conflict_persistence import persist_asset_conflicts

            store = get_store()
            try:
                detector = runtime.supervisor.asset_detector  # lazy; valida paths de MO2
                reports = await asyncio.to_thread(detector.detect_conflicts)
                nuevos = await persist_asset_conflicts(reports, get_db_agent())
                await get_state().refresh_conflicts()
            except Exception as exc:
                logger.exception("Fallo la detección de disputas de assets")
                store.set(
                    STORE_KEY_RITUAL_FEEDBACK,
                    {"text": f"La detección de disputas falló: {exc}", "type": "negative"},
                )
                return
            texto = (
                f"{nuevos} disputa(s) nueva(s) detectada(s)."
                if nuevos
                else f"Sin disputas nuevas ({len(reports)} solapamiento(s) ya registrados o ninguno)."
            )
            store.set(STORE_KEY_RITUAL_FEEDBACK, {"text": texto, "type": "positive" if not nuevos else "warning"})

        if runtime.supervisor is None:
            ui.notify("El daemon no está inicializado todavía.", type="warning")
            return
        create_tracked_task(_scan(), name="gui-conflict-scan")

    callbacks["on_conflict_scan"] = _on_conflict_scan

    # Sección Ajustes: guardar valida + persiste (keyring/TOML) y re-renderiza
    # para que el header refleje la identidad nueva al instante.
    def _on_settings_save(payload: dict[str, str]) -> None:
        error = save_settings(runtime.config_path, payload)
        if error is not None:
            ui.notify(error, type="negative")
        else:
            # Honesto con el estado real: el router LLM vivo no se recarga en
            # caliente desde acá (eso vive en frontend_bridge._do_llm_reload);
            # provider/clave aplican al reiniciar (review Codex en #221).
            ui.notify(
                "Ajustes guardados — los cambios de proveedor/clave aplican al reiniciar Sky-Claw",
                type="positive",
            )
            main_page.refresh()

    callbacks["on_settings_save"] = _on_settings_save

    # A3: identidad del header data-driven desde el estado, sembrado desde el
    # TOML la primera vez (editable en Ajustes).
    _ensure_identity_loaded(runtime.config_path)
    app_state = get_app_state_instance()
    identity = {"name": app_state.user_display_name, "role": app_state.user_role}

    active_section = get_store().get("active_section") or "Dashboard"
    render_dashboard(
        stats=stats,
        mods=mods,
        chat_messages=chat_messages,
        is_thinking=state.is_thinking,
        callbacks=callbacks,
        active_section=active_section,
        identity=identity,
        search_query=get_store().get("mods_search_query") or "",
        conflicts_list=get_store().get("conflicts_list") or [],
        settings=_build_settings_data(runtime.config_path) if active_section == "Settings" else None,
    )


@ui.page("/")
def _page_root() -> None:
    main_page()


# ── App setup ─────────────────────────────────────────────────────────────────


def setup_app() -> None:
    """Configure NiceGUI app: assets, controllers, EventBus, store wiring."""
    global _chat_controller, _mod_controller, _nav_controller

    app.add_static_files("/static", str(_CSS_PATH.parent))
    app.add_static_files("/assets", str(_ASSETS_PATH))

    async def _seed_db() -> None:
        await get_db_agent().init_db()
        mods = await get_db_agent().get_mods()
        if not mods:
            await get_db_agent().add_mod("Skyrim 202X", "9.0", 2400, "Nexusmods")
            await get_db_agent().add_mod("Immersive Armors", "8.1", 156, "Nexusmods")
            await get_db_agent().add_mod("Lux Via", "1.5", 89, "Nexusmods")
        await get_state().update_from_db()

    app.on_startup(_seed_db)

    # Defer EventBus.start() into the running NiceGUI loop. Calling it eagerly
    # here (before ui.run()) leaves _loop=None, so the processor thread silently
    # drops every event — navigation, chat rendering and the thinking spinner
    # all go dead. Subscriptions below are loop-independent and stay eager.
    app.on_startup(event_bus.start)

    app_state = get_app_state_instance()
    _chat_controller = ChatController(
        app_state=app_state,
        event_bus=event_bus,
        agent_client_factory=get_agent_client,
    )
    _mod_controller = ModController(app_state=app_state, event_bus=event_bus)
    _nav_controller = NavigationController(app_state=app_state, event_bus=event_bus)

    # Subscribe the page to the gate-driving keys so the Wizard→Dashboard
    # transition is instantaneous in the same session.
    # Also refresh on is_loading changes so the chat panel re-renders
    # when the user sends a message or the assistant responds.
    store = get_store()
    store.subscribe(_FIRST_RUN_KEY, main_page.refresh)
    store.subscribe(_RUNTIME_KEY, main_page.refresh)
    store.subscribe("is_loading", main_page.refresh)
    store.subscribe("mods_list", main_page.refresh)
    # Parte 5: re-render al navegar (el sidebar pinta la sección activa).
    store.subscribe("active_section", main_page.refresh)
    # A1: re-render cuando cambia el término de búsqueda del header, aun si la
    # sección activa no cambia (buscar estando ya en "Mods").
    store.subscribe("mods_search_query", main_page.refresh)
    # Conflictos: re-render de la pantalla al refrescar la lista (alta/resolución).
    store.subscribe("conflicts_list", main_page.refresh)
    # Re-render el indicador "DAEMON CONECTADO" del sidebar cuando el WS conecta/cae.
    store.subscribe("is_agent_connected", main_page.refresh)
    # Phase 1: re-render los Rituales cuando el escaneo de entorno publica el
    # snapshot (Disponible / No instalado).
    #
    # Las claves de telemetría (sys_cpu/gpu/ram) siguen SIN suscribirse a
    # main_page.refresh a propósito: refrescar la página entera a 1 Hz reiniciaría
    # el input del chat. El latido en vivo de las vitals/HUD se resuelve a nivel de
    # componente — un ``ui.timer`` en render_forge_dashboard refresca solo los
    # @ui.refreshable de Vitalidad y del HUD cada LIVE_REFRESH_SECONDS, sin tocar el
    # chat (cierra el follow-up de Codex #3 en #209).
    store.subscribe(STORE_KEY_ENV, main_page.refresh)

    # Fase 2: refresh the HITL modal, the result toast, and the "Modo local" toggle
    # through their own @ui.refreshable panels — NOT main_page.refresh — so opening
    # a prompt or showing a result never resets the chat input.
    store.subscribe(STORE_KEY_PENDING_HITL, _hitl_modal_panel.refresh)
    store.subscribe(STORE_KEY_RITUAL_FEEDBACK, _ritual_feedback_panel.refresh)
    # The "Modo local" toggle now lives in per-client app.storage.client, so it
    # refreshes from its own click/F8 handlers (client context) — no store key.

    app.on_startup(lambda: get_agent_client().start())

    async def _cleanup() -> None:
        event_bus.stop()
        client = agent_client
        if client is not None:
            await client.stop()

    app.on_shutdown(_cleanup)


def cleanup() -> None:
    event_bus.stop()
