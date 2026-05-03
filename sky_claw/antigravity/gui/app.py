"""
Sky-Claw GUI — Dashboard Premium Dark Spatial + Nordic v3.0

Arquitectura:
- ReactiveState con ui.core.variable() (sin @ui.refreshable)
- CSS externo sky-* namespaced (styles.css)
- Quasar ripple nativo (.props('ripple'))
- Queue-based message passing (ctx.logic_queue / ctx.gui_queue)
- SetupWizardModal como overlay sobre DashboardGUI (sin ruta /setup separada)
- localStorage autosave para borradores (campos no sensibles)
- Tema Nórdico / Rúnico con WCAG 2.2 AAA
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
import queue
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

import keyring
from nicegui import app, ui

from .icons import (
    _ICON_ANVIL,
    _ICON_CART,
    _ICON_CHAT,
    _ICON_LAYERS,
    _ICON_ROCKET,
    _ICON_SETTINGS,
)
from .models.app_state import AppState, get_app_state
from .views.actions import build_actions_panel
from .views.advanced import build_advanced_panel

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# ─── Path al CSS externo ───────────────────────────────────────────────
CSS_PATH = Path(__file__).parent / "styles.css"
ASSETS_PATH = Path(__file__).parent / "assets"

# Registrar archivos estáticos para NiceGUI
app.add_static_files("/assets", str(ASSETS_PATH))

MAX_CHAT_MESSAGES = 500


def _load_css() -> None:
    """Carga el CSS externo una vez por página."""
    if CSS_PATH.exists():
        ui.add_css(CSS_PATH.read_text(encoding="utf-8"))


# =============================================================================
# STRATEGY PATTERN: MANEJO DE MENSAJES DE COLA
# =============================================================================


class MessageHandlerStrategy(abc.ABC):
    @abc.abstractmethod
    def handle(self, gui: DashboardGUI, data: Any) -> None:
        pass


class ResponseHandler(MessageHandlerStrategy):
    def handle(self, gui: DashboardGUI, data: Any) -> None:
        gui.append_chat_message(str(data), is_user=False)


class ModlistHandler(MessageHandlerStrategy):
    def handle(self, gui: DashboardGUI, data: Any) -> None:
        gui.update_mod_list(data)


class SuccessHandler(MessageHandlerStrategy):
    def handle(self, gui: DashboardGUI, data: Any) -> None:
        gui.append_chat_message(str(data), is_user=False, style="success")


class ErrorHandler(MessageHandlerStrategy):
    def handle(self, gui: DashboardGUI, data: Any) -> None:
        gui.append_chat_message(str(data), is_user=False, style="error")


# =============================================================================
# SETUP WIZARD MODAL — Overlay sobre el Dashboard (Nordic / Parchment)
# =============================================================================


class SetupWizardModal:
    """Wizard de credenciales renderizado como modal overlay sobre el dashboard.

    - Auto-abre si las credenciales están vacías.
    - NO se cierra al hacer clic en el backdrop.
    - Autoguarda borradores no sensibles en localStorage.
    - Solo se cierra al guardar exitosamente.
    """

    VALID_PROVIDERS = {"anthropic", "deepseek", "ollama"}

    def __init__(
        self,
        config_path: Path,
        on_complete: Callable,
        app_state: AppState | None = None,
    ) -> None:
        self._config_path = config_path
        self._on_complete = on_complete
        self._state = app_state or get_app_state()
        self._overlay_el: ui.element | None = None
        # Input references (ahora delegadas a AppState)
        # Draft fields (non-sensitive) for localStorage
        self._draft_fields: dict[str, ui.input] = {}

    def build(self) -> None:
        """Renderiza el overlay fijo sobre el dashboard."""
        # Overlay backdrop — NO cierra al hacer clic
        self._overlay_el = ui.element("div").classes("sky-wizard-overlay")
        self._state.wizard_overlay = self._overlay_el

        with self._overlay_el:
            # Modal container — optimizado
            modal = ui.element("div").classes("sky-wizard-modal")
            # Nota: Eliminado el lambda vacío en 'click' para prevenir overhead de WebSocket.

            with modal:
                # Header
                with ui.row().classes("items-center justify-between w-full mb-4"):
                    with ui.row().classes("items-center gap-3"):
                        ui.html(f"""
                            <div style="width:36px;height:36px;border-radius:8px;display:flex;
                                        align-items:center;justify-content:center;
                                        background:linear-gradient(135deg, #C8A84E, #8B7332);">
                                {_ICON_SETTINGS}
                            </div>
                        """)
                        ui.label("ASISTENTE DE CONFIGURACIÓN").classes("sky-wizard-title")
                    step_label = ui.label("Paso 1 de 2").classes("sky-wizard-step")
                    self._state.register_ui_element("step_label", step_label)

                # Progress bar
                with (
                    ui.element("div")
                    .classes("w-full mb-4")
                    .style("height:4px; background:rgba(255,255,255,0.08); border-radius:2px; overflow:hidden;")
                ):
                    progress_bar = ui.element("div").style(
                        "width:50%; height:100%; background:var(--sky-gold); "
                        "border-radius:2px; transition:width 0.3s ease;"
                    )
                    self._state.register_ui_element("progress_bar", progress_bar)

                # Description
                ui.label(
                    "Parece que sus credenciales están vacías. Complete los siguientes campos "
                    "para inicializar el sistema Sky-Claw. Los datos se guardan localmente como borradores."
                ).classes("sky-wizard-description mb-5")

                # ── Step 1 ──
                step1_container = ui.column().classes("w-full gap-4")
                self._state.register_ui_element("step1_container", step1_container)
                with step1_container:
                    # API Key
                    with ui.column().classes("w-full gap-1"):
                        ui.label("CLAVE API DE OPERACIONES").classes("sky-wizard-label")
                        api_key_input = (
                            ui.input(
                                placeholder="sk-... o clave del proveedor",
                            )
                            .classes("w-full")
                            .props(
                                'type=password dark standout="bg-transparent" '
                                'input-class="sky-wizard-input" color=amber maxlength=512'
                            )
                        )
                        self._state.register_ui_element("api_key_input", api_key_input)
                        ui.label("Usa tu API Key de producción").classes("sky-wizard-hint")

                    # Telegram ID
                    with ui.column().classes("w-full gap-1"):
                        ui.label("ID DE TELEGRAM").classes("sky-wizard-label")
                        telegram_id_input = (
                            ui.input(
                                placeholder="@usuario_id",
                            )
                            .classes("w-full")
                            .props(
                                'dark standout="bg-transparent" input-class="sky-wizard-input" color=amber maxlength=32'
                            )
                        )
                        self._state.register_ui_element("telegram_id_input", telegram_id_input)
                        ui.label("ID único de tu cuenta de Telegram").classes("sky-wizard-hint")
                        self._draft_fields["telegram_chatid"] = telegram_id_input

                    # Frecuencia
                    with ui.column().classes("w-full gap-1"):
                        ui.label("FRECUENCIA (MS)").classes("sky-wizard-label")
                        frequency_input = (
                            ui.input(
                                placeholder="5000",
                                value="5000",
                            )
                            .classes("w-full")
                            .props(
                                'dark standout="bg-transparent" input-class="sky-wizard-input" color=amber maxlength=10'
                            )
                        )
                        self._state.register_ui_element("frequency_input", frequency_input)
                        ui.label("Frecuencia de monitoreos en milisegundos").classes("sky-wizard-hint")
                        self._draft_fields["frequency_ms"] = frequency_input

                # ── Step 2 (hidden initially) ──
                step2_container = ui.column().classes("w-full gap-4")
                step2_container.style("display: none;")
                self._state.register_ui_element("step2_container", step2_container)
                with step2_container:
                    # Provider
                    with ui.column().classes("w-full gap-1"):
                        ui.label("PROVEEDOR IA").classes("sky-wizard-label")
                        provider_toggle = (
                            ui.toggle(
                                ["anthropic", "deepseek", "ollama"],
                                value="deepseek",
                            )
                            .classes("w-full")
                            .props("color=amber")
                        )
                        self._state.register_ui_element("provider_toggle", provider_toggle)

                    # Nexus Key
                    with ui.column().classes("w-full gap-1"):
                        ui.label("NEXUS MODS API KEY").classes("sky-wizard-label")
                        nexus_input = (
                            ui.input(
                                placeholder="Opcional — para descargas automáticas",
                            )
                            .classes("w-full")
                            .props(
                                'type=password dark standout="bg-transparent" '
                                'input-class="sky-wizard-input" color=amber maxlength=512'
                            )
                        )
                        self._state.register_ui_element("nexus_input", nexus_input)

                    # Telegram Token
                    with ui.column().classes("w-full gap-1"):
                        ui.label("TELEGRAM BOT TOKEN").classes("sky-wizard-label")
                        telegram_token_input = (
                            ui.input(
                                placeholder="Opcional — para notificaciones HITL",
                            )
                            .classes("w-full")
                            .props(
                                'type=password dark standout="bg-transparent" '
                                'input-class="sky-wizard-input" color=amber maxlength=512'
                            )
                        )
                        self._state.register_ui_element("telegram_token_input", telegram_token_input)

                # ── CTA Button ──
                with ui.row().classes("w-full justify-end gap-3 mt-4"):
                    back_btn = (
                        ui.button(
                            "Atrás",
                            on_click=self._go_step1,
                        )
                        .classes("px-5 py-3 rounded-xl font-semibold")
                        .props("ripple flat no-caps")
                        .style("color: var(--sky-parchment-text); display: none;")
                    )
                    self._state.register_ui_element("back_btn", back_btn)

                    next_btn = (
                        ui.button(
                            "Siguiente",
                            on_click=self._go_step2,
                        )
                        .classes("sky-wizard-cta px-6 py-3 rounded-xl text-lg")
                        .props("ripple no-caps")
                    )
                    self._state.register_ui_element("next_btn", next_btn)

                    submit_btn = (
                        ui.button(
                            on_click=self._on_submit,
                        )
                        .classes("sky-wizard-cta px-6 py-3 rounded-xl text-lg")
                        .props("ripple no-caps")
                        .style("display: none;")
                    )
                    self._state.register_ui_element("submit_btn", submit_btn)
                    with submit_btn:
                        ui.html(f'<span style="margin-right:8px;">{_ICON_ROCKET}</span>')
                        ui.label("Inicializar Sistema")

        # Attach localStorage autosave handlers
        for field_name, input_el in self._draft_fields.items():
            input_el.on(
                "update:model-value",
                lambda e, fn=field_name: self._save_draft(fn, e.args),
            )

        # Restore drafts from localStorage
        ui.timer(0.3, self._restore_drafts, once=True)

        # Prevent backdrop from propagating clicks
        ui.run_javascript("""
            document.querySelector('.sky-wizard-overlay')?.addEventListener('click', function(e) {
                e.stopPropagation();
            });
        """)

    def _go_step2(self) -> None:
        self._step = 2
        self._step_label.set_text("Paso 2 de 2")
        self._progress_bar.style(
            "width:100%; height:100%; background:var(--sky-gold); border-radius:2px; transition:width 0.3s ease;"
        )
        self._step1_container.style("display: none;")
        self._step2_container.style("display: flex;")
        self._next_btn.style("display: none;")
        self._back_btn.style("display: block; color: var(--sky-parchment-text);")
        self._submit_btn.style("display: flex;")

    def _go_step1(self) -> None:
        self._step = 1
        self._step_label.set_text("Paso 1 de 2")
        self._progress_bar.style(
            "width:50%; height:100%; background:var(--sky-gold); border-radius:2px; transition:width 0.3s ease;"
        )
        self._step1_container.style("display: flex;")
        self._step2_container.style("display: none;")
        self._next_btn.style("display: block;")
        self._back_btn.style("display: none;")
        self._submit_btn.style("display: none;")

    async def _on_submit(self) -> None:
        provider = self._provider_toggle.value if self._provider_toggle else "deepseek"
        api_key = self._api_key_input.value.strip() if self._api_key_input else ""
        nexus_key = self._nexus_input.value.strip() if self._nexus_input else ""
        telegram_token = self._telegram_token_input.value.strip() if self._telegram_token_input else ""
        telegram_chatid = self._telegram_id_input.value.strip() if self._telegram_id_input else ""

        await self._validate_and_save(
            provider=provider,
            api_key=api_key,
            nexus_key=nexus_key,
            telegram_token=telegram_token,
            telegram_chatid=telegram_chatid,
        )

    async def _validate_and_save(
        self,
        provider: str,
        api_key: str,
        nexus_key: str,
        telegram_token: str,
        telegram_chatid: str,
    ) -> None:
        if provider not in self.VALID_PROVIDERS:
            ui.notify("Proveedor no válido", type="negative")
            return

        if provider in ("anthropic", "deepseek") and not api_key:
            ui.notify("API Key requerida para este proveedor", type="negative")
            return

        if len(api_key) > 512:
            ui.notify("Máximo 512 caracteres en API Key", type="negative")
            return

        if telegram_token and ":" not in telegram_token:
            ui.notify("Token Telegram inválido — debe contener ':'", type="negative")
            return

        if telegram_chatid and not telegram_chatid.replace("@", "").replace("-", "").isdigit():
            ui.notify("Chat ID debe ser numérico", type="negative")
            return

        try:
            key_map = {
                f"{provider}_api_key": api_key,
                "nexus_api_key": nexus_key,
                "telegram_bot_token": telegram_token,
            }
            if api_key:
                key_map["llm_api_key"] = api_key

            for k, v in key_map.items():
                if v:
                    keyring.set_password("sky_claw", k, v)

            existing_ws = keyring.get_password("sky_claw", "ws_auth_token")
            if not existing_ws:
                keyring.set_password("sky_claw", "ws_auth_token", secrets.token_hex(32))

            from sky_claw.config import Config

            cfg = Config(self._config_path)
            cfg._data["llm_provider"] = provider
            cfg._data["first_run"] = False
            if telegram_chatid:
                cfg._data["telegram_chat_id"] = telegram_chatid.replace("@", "")
            cfg.save()

            # Clear localStorage drafts
            await self._clear_drafts()

            logger.info("Setup completado — provider=%s", provider)

            # Remove overlay from DOM
            if self._overlay_el:
                self._overlay_el.delete()

            await self._on_complete()

        except Exception as e:
            logger.exception("Error guardando configuración:")
            ui.notify(f"Error guardando configuración: {e}", type="negative")

    # ── localStorage Draft Autosave ──────────────────────────────────

    def _save_draft(self, field_name: str, value: Any) -> None:
        val = str(value) if value else ""
        ui.run_javascript(f'localStorage.setItem("skyclaw_draft_{field_name}", {json.dumps(val)})')

    async def _restore_drafts(self) -> None:
        for field_name, input_el in self._draft_fields.items():
            try:
                val = await ui.run_javascript(f'localStorage.getItem("skyclaw_draft_{field_name}")')
                if val:
                    input_el.value = val
            except Exception:
                pass

    async def _clear_drafts(self) -> None:
        await ui.run_javascript("""
            Object.keys(localStorage)
                .filter(k => k.startsWith("skyclaw_draft_"))
                .forEach(k => localStorage.removeItem(k))
        """)


# =============================================================================
# SETUP PAGE — Legacy wrapper (mantiene compatibilidad con __main__.py)
# =============================================================================


class SetupPage:
    """Legacy: redirige internamente al wizard modal. Mantenido para compat."""

    VALID_PROVIDERS = {"anthropic", "deepseek", "ollama"}

    def __init__(self, config_path: Path, on_complete: Callable) -> None:
        self._config_path = config_path
        self._on_complete = on_complete

    def build(self) -> None:
        _load_css()
        with (
            ui.element("div")
            .classes("w-full min-h-screen flex items-center justify-center relative")
            .style("background: var(--sky-bg-primary); font-family: var(--sky-font-family);")
        ):
            ui.html('<div class="sky-glow-overlay" style="opacity: 0.1;"></div>')
            wizard = SetupWizardModal(self._config_path, self._on_complete)
            wizard.build()


# =============================================================================
# DASHBOARD GUI — Nordic Theme
# =============================================================================


class DashboardGUI:
    """Dashboard premium con sidebar, stats, widgets, chat — tema Nórdico/Rúnico."""

    def __init__(self, ctx: Any, app_state: AppState | None = None) -> None:
        self.ctx = ctx
        self._state = app_state or get_app_state()

        self._running: bool = True
        self._is_thinking: bool = False
        self._message_elements: list = []
        self._chat_container: Any = None
        self._chat_scroll: Any = None
        self._chat_input: Any = None
        self._thinking_label: Any = None
        self._mod_container: Any = None
        self._stat_labels: dict[str, Any] = {}
        self._health_banner: Any = None
        self._actions_container: Any = None
        self._env_snapshot: Any = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._status_dot: Any = None
        self._status_label: Any = None

        self.handlers: dict[str, Any] = {
            "response": ResponseHandler(),
            "modlist": ModlistHandler(),
            "success": SuccessHandler(),
            "error": ErrorHandler(),
        }
        self._state.handlers = self.handlers  # Sincronizar con AppState

    def build(self) -> None:
        _load_css()

        with (
            ui.element("div")
            .classes("w-full min-h-screen flex relative")
            .style("background: transparent; font-family: var(--f-body); z-index: 1;")
        ):
            self._build_sidebar()

            with ui.column().classes("flex-1 min-h-screen overflow-auto sky-main-content"):
                self._build_header()

                with ui.column().classes("p-6 gap-6 flex-1"):
                    # Row 2: Stats cards (PROCESADOR + CARGA TAREAS)
                    with ui.row().classes("w-full gap-6"):
                        self._build_stat_summary("PROCESADOR", "116", _ICON_ANVIL)
                        self._build_stat_summary("CARGA TAREAS", "3,130 ops/s", _ICON_CART)

                    # ── Row 3.5: HERRAMIENTAS — Semantic Actions (v4.0) ──
                    self._build_tools_section()

                    # Row 4: Mod panel + Chat panel
                    with ui.row().classes("w-full gap-6 flex-1"):
                        self._build_mod_panel()
                        self._build_chat_panel()

                # Footer
                self._build_footer()

        # Auto-open wizard if credentials are empty
        if self._should_show_wizard():
            config_path = getattr(self.ctx, "config_path", None)
            if config_path:
                wizard = SetupWizardModal(
                    config_path=config_path,
                    on_complete=self._on_wizard_complete,
                )
                wizard.build()

        # Timers
        ui.timer(0.1, self._poll_queue)
        ui.timer(0.5, self._load_initial_mods, once=True)

    def _should_show_wizard(self) -> bool:
        """Verifica si las credenciales están vacías para mostrar el wizard."""
        try:
            config_path = getattr(self.ctx, "config_path", None)
            if not config_path:
                return False
            from sky_claw.config import Config

            cfg = Config(config_path)
            if cfg._data.get("first_run", True):
                return True
            provider = cfg._data.get("llm_provider", "ollama")
            if provider == "ollama":
                return False
            key = keyring.get_password("sky_claw", f"{provider}_api_key")
            return not bool(key)
        except Exception:
            return False

    async def _on_wizard_complete(self) -> None:
        ui.notify("Sistema inicializado correctamente", type="positive")

    # ── Sidebar ───────────────────────────────────────────────────────
    def _build_sidebar(self) -> None:
        with ui.element("div").classes("w-64 min-h-screen flex flex-col shrink-0 sky-sidebar"):
            # Logo
            with (
                ui.element("div")
                .classes("p-6 relative")
                .style("border-bottom: 1px solid var(--forge-edge); z-index:1;"),
                ui.row().classes("items-center gap-3"),
            ):
                ui.html(f"""
                        <div style="width:36px;height:36px;border-radius:50%;display:grid;place-items:center;
                                    background: radial-gradient(circle, #3a2813 0%, #1a0f07 70%);
                                    border: 1px solid var(--iron-rim);
                                    box-shadow: 0 0 14px var(--gold-mist);"
                             class="sky-glow-static">
                            <span style="color: var(--gold-hot); filter: drop-shadow(0 0 3px var(--gold-soft));">
                              {_ICON_LAYERS}
                            </span>
                        </div>
                    """)
                with ui.column().classes("gap-0"):
                    ui.label("SKY-CLAW").classes("sky-brand-name")
                    ui.label("TECHNICAL OPERATIONS").classes("sky-brand-sub")

            # Nav
            with ui.column().classes("flex-1 p-4 gap-1"):
                nav_items = [
                    ("DASHBOARD", "dashboard", True),
                    ("HERRAMIENTAS", "herramientas", False),
                    ("MÓDULOS", "reservas", False),
                    ("NOTICIAS", "periodicos", False),
                    ("AJUSTES", "settings", False),
                ]

                for text, view, is_active in nav_items:
                    cls = "w-full text-left px-4 py-3 sky-nav-item"
                    if is_active:
                        cls += " sky-nav-item--active"
                    ui.button(
                        text,
                        on_click=lambda v=view: self._navigate(v),
                    ).classes(cls).props("ripple flat no-caps")

            # Status LLM
            with (
                ui.element("div")
                .classes("p-4 relative")
                .style("border-top: 1px solid var(--forge-edge); background: rgba(0,0,0,0.25); z-index:1;"),
                ui.row().classes("items-center gap-2"),
            ):
                ui.html(
                    '<span style="width:9px;height:9px;border-radius:50%;'
                    "background:var(--emerald);box-shadow:0 0 8px var(--emerald-glow);"
                    'animation:sky-breathe 2.4s ease-in-out infinite;display:inline-block;"></span>'
                )
                self._status_label = ui.label("OPERATOR · CONECTADO").style(
                    "color: var(--parchment); font-family: var(--f-display); "
                    "font-size: 0.72rem; letter-spacing: 0.18em;"
                )

    def _navigate(self, view: str) -> None:
        if view == "settings":
            self._open_settings_dialog()

    # ── Header ────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        with ui.element("div").classes("h-16 flex items-center justify-between px-6 shrink-0 sky-header"):
            with ui.column().classes("gap-0"):
                ui.label("OPERACIONES TÉCNICAS").classes("sky-header-title")

            # Header actions
            with ui.row().classes("items-center gap-3"):
                # Status pill
                with ui.element("div").classes("sky-status-pill"):
                    ui.html('<span class="pulse"></span>')
                    ui.label("OPERATIVO")

                for label_text in ["Protocolos", "Alerta", "Ayuda"]:
                    ui.button(label_text).classes("sky-chip").props("ripple flat no-caps")

                # Avatar seal
                with ui.element("div").classes("sky-avatar-seal"):
                    ui.label("SC")

    # ── Stat Summary Cards ────────────────────────────────────────────
    def _build_stat_summary(self, title: str, value: str, icon_svg: str) -> None:
        with (
            ui.element("div").classes("sky-widget-panel flex-1 p-5 sky-card-hover sky-animate-in"),
            ui.row().classes("items-center justify-between w-full"),
        ):
            with ui.column().classes("gap-2"):
                ui.label(title).classes("sky-metric-label")
                ui.label(value).classes("sky-metric-value")
            ui.html(f'<div class="sky-metric-icon">{icon_svg}</div>')

    # ── Tools Section — Semantic Actions v4.0 ─────────────────────────
    def _build_tools_section(self) -> None:
        """Build the 'Herramientas' section with health banner + action buttons."""
        with ui.element("div").classes("sky-widget-panel w-full p-4 sky-animate-in"):
            with ui.row().classes("items-center gap-2 mb-3"):
                ui.label("ᚦ").classes("sky-active-rune").style(
                    "font-family: var(--f-rune); font-size: 1.5rem; line-height: 1;"
                )
                ui.label("HERRAMIENTAS").classes("sky-section-title")

            self._health_banner = ui.element("div").classes("sky-health-banner sky-health-banner--warning")
            with self._health_banner:
                ui.label("🟡 Escaneando el entorno...").classes("sky-health-text")

            self._actions_container = ui.element("div")
            with self._actions_container:
                build_actions_panel(
                    snapshot=self._env_snapshot,
                    on_action=self._on_tool_action,
                    on_prepare_game=self._on_prepare_game,
                    on_install_tool=self._on_install_tool,
                )

            build_advanced_panel(self._env_snapshot)

        ui.timer(0.3, self._run_env_scan, once=True)

    async def _run_env_scan(self) -> None:
        """Run the environment scanner and update the UI."""
        try:
            from sky_claw.local.discovery.scanner import EnvironmentScanner

            scanner = EnvironmentScanner()
            self._env_snapshot = await scanner.scan()
        except Exception as exc:
            logger.error("Environment scan failed: %s", exc)
            return

        snap = self._env_snapshot
        if snap and self._health_banner:
            self._health_banner.clear()
            banner_map = {
                "ready": "sky-health-banner--ready",
                "needs_setup": "sky-health-banner--warning",
                "critical": "sky-health-banner--critical",
            }
            bc = banner_map.get(snap.health_status.value, "sky-health-banner--warning")
            self._health_banner.classes(
                remove="sky-health-banner--ready sky-health-banner--warning sky-health-banner--critical"
            )
            self._health_banner.classes(add=bc)
            with self._health_banner:
                for msg in snap.health_messages[:5]:
                    ui.label(msg).classes("sky-health-text")

        if self._actions_container:
            self._actions_container.clear()
            with self._actions_container:
                build_actions_panel(
                    snapshot=self._env_snapshot,
                    on_action=self._on_tool_action,
                    on_prepare_game=self._on_prepare_game,
                    on_install_tool=self._on_install_tool,
                )

    async def _on_tool_action(self, tool_key: str) -> None:
        """Handle click on a semantic action button."""
        names = {
            "loot": "Ordenar mods",
            "xedit": "Limpiar archivos",
            "wrye_bash": "Crear parche",
            "pandora": "Generar animaciones",
            "dyndolod": "Optimizar gráficos",
        }
        name = names.get(tool_key, tool_key)
        with contextlib.suppress(RuntimeError):
            ui.notify(f"▶ Ejecutando: {name}...", type="info", position="top")
        if hasattr(self, "ctx") and hasattr(self.ctx, "logic_queue"):
            self.ctx.logic_queue.put_nowait(
                {
                    "type": "tool_action",
                    "tool": tool_key,
                    "message": f"/run {tool_key}",
                }
            )
        self.append_chat_message(f"🔧 {name}...", is_user=False, style="info")

    async def _on_prepare_game(self) -> None:
        """Handle the master 'Preparar Juego' button."""
        with contextlib.suppress(RuntimeError):
            ui.notify("🚀 Preparando juego...", type="positive", position="top")
        self.append_chat_message(
            "🚀 **Preparar Juego** — Secuencia completa:\n"
            "1. Backup automático\n2. Ordenar mods (LOOT)\n"
            "3. Limpiar archivos (xEdit)\n4. Crear parche (Wrye Bash)\n"
            "5. Generar animaciones (Pandora)\n6. Optimizar gráficos (DynDOLOD)",
            is_user=False,
            style="info",
        )

    async def _on_install_tool(self, tool_key: str) -> None:
        """Handle request to install a missing tool."""
        if self._env_snapshot:
            for m in self._env_snapshot.missing:
                if m.name.lower().replace(" ", "_") == tool_key:
                    with contextlib.suppress(RuntimeError):
                        ui.notify(f"Abriendo descarga de {m.name}...", type="info")
                    ui.navigate.to(m.download_url, new_tab=True)
                    return
        with contextlib.suppress(RuntimeError):
            ui.notify(f"Instalación de {tool_key} pendiente", type="warning")

    # ── Mod Panel ─────────────────────────────────────────────────────
    def _build_mod_panel(self) -> None:
        with ui.column().classes("w-1/2 sky-widget-panel overflow-hidden sky-animate-in--delay-1 sky-animate-in"):
            with (
                ui.row()
                .classes("items-center justify-between p-4")
                .style("border-bottom: 1px solid var(--sky-surface-border);")
            ):
                with ui.row().classes("items-center gap-2"):
                    ui.label("ᚱ").classes("sky-active-rune").style(
                        "font-family: var(--f-rune); font-size: 1.4rem; line-height: 1;"
                    )
                    ui.label("MODS INSTALADOS").classes("sky-section-title")
                with ui.row().classes("gap-2"):
                    ui.button(
                        "Actualizar",
                        on_click=self._update_all,
                    ).classes("sky-btn-forge px-3 py-1 text-xs").props("ripple flat no-caps")
                    ui.button(
                        "Escanear",
                        on_click=self._scan_all,
                    ).classes("sky-btn-forge px-3 py-1 text-xs").props("ripple flat no-caps")

            with ui.scroll_area().classes("flex-1 sky-scrollbar").style("height: 300px;"):
                self._mod_container = ui.column().classes("w-full")

    def update_mod_list(self, mods: list[str]) -> None:
        if not self._mod_container:
            return
        self._mod_container.clear()

        if "active_mods" in self._stat_labels:
            self._stat_labels["active_mods"].set_text(str(len(mods)))

        with self._mod_container:
            for i, mod in enumerate(mods, 1):
                with (
                    ui.row()
                    .classes("w-full items-center py-3 px-4 transition-colors")
                    .style("border-bottom: 1px solid var(--sky-surface-border);")
                ):
                    ui.html("""
                        <div style="width:32px;height:32px;border-radius:8px;display:flex;
                                    align-items:center;justify-content:center;flex-shrink:0;
                                    background:var(--sky-bg-elevated);">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
                                 stroke="#94A3B8" stroke-width="2">
                                <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                            </svg>
                        </div>
                    """)
                    with ui.column().classes("flex-1 min-w-0 ml-3"):
                        ui.label(mod).style("color: var(--sky-text-primary); font-size:0.875rem; font-weight:500;")
                        ui.label(f"#{i:03d}").style("color: var(--sky-text-muted); font-size:0.7rem;")
                    ui.label("Activo").classes("sky-badge sky-badge--success shrink-0")

    def _update_all(self) -> None:
        self.append_chat_message("Iniciando actualización de mods...", is_user=False, style="success")
        self.ctx.logic_queue.put(("chat", "/update_mods"))

    def _scan_all(self) -> None:
        self.append_chat_message("Iniciando escaneo de VFS...", is_user=False, style="success")
        self.ctx.logic_queue.put(("chat", "/scan"))

    # ── Chat Panel ────────────────────────────────────────────────────
    def _build_chat_panel(self) -> None:
        with ui.column().classes("w-1/2 sky-widget-panel overflow-hidden sky-animate-in--delay-2 sky-animate-in flex"):
            # Header
            with (
                ui.element("div")
                .classes("p-4")
                .style(
                    "border-bottom: 1px solid var(--forge-edge); "
                    "background: linear-gradient(180deg, rgba(40,27,16,0.6), rgba(20,13,8,0));"
                ),
                ui.row().classes("items-center gap-3"),
            ):
                ui.html(f"""
                        <div style="width:40px;height:40px;border-radius:50%;display:grid;place-items:center;
                                    background: radial-gradient(circle, #3a2813 0%, #1a0f07 70%);
                                    border: 1px solid var(--iron-rim);
                                    box-shadow: 0 0 14px var(--gold-mist);"
                             class="sky-glow-static">
                            <span style="color: var(--gold-hot); filter: drop-shadow(0 0 3px var(--gold-soft));">
                              {_ICON_CHAT}
                            </span>
                        </div>
                    """)
                with ui.column().classes("gap-0"):
                    ui.label("ASISTENTE IA").classes("sky-section-title")
                    ui.label("Sky-Claw · Operaciones").style(
                        "color: var(--parchment-mute); font-family: var(--f-mono); "
                        "font-size: 0.7rem; letter-spacing: 0.08em;"
                    )

            # Messages
            self._chat_scroll = ui.scroll_area().classes("flex-1 sky-scrollbar").style("height: 260px;")
            with self._chat_scroll:
                self._chat_container = ui.column().classes("w-full p-4 gap-2")

            # Thinking label
            with ui.row().classes("px-4 py-2 items-center gap-2").style("min-height: 24px;"):
                self._thinking_label = (
                    ui.label("Procesando...")
                    .style("color: var(--sky-text-muted); font-size:0.75rem; display:none;")
                    .classes("animate-pulse")
                )

            # Input bar
            with ui.row().classes("p-4 items-center gap-2").style("border-top: 1px solid var(--sky-surface-border);"):
                self._chat_input = (
                    ui.input(
                        placeholder="Escribí tu mensaje...",
                    )
                    .classes("flex-1")
                    .props('dark standout="bg-transparent" input-class="sky-input-spatial" color=amber')
                )
                self._chat_input.on("keydown.enter", self._send_message)

                ui.button(
                    "Enviar",
                    on_click=self._send_message,
                ).classes("sky-wizard-cta px-5 py-2").props("ripple no-caps")

    def append_chat_message(self, text: str, is_user: bool = False, style: str = "normal") -> None:
        self._hide_thinking()

        while len(self._message_elements) >= MAX_CHAT_MESSAGES:
            oldest = self._message_elements.pop(0)
            with contextlib.suppress(ValueError, KeyError, AttributeError):
                self._chat_container.remove(oldest)
                oldest.delete()

        style_map = {
            "normal": "sky-chat-message--assistant" if not is_user else "sky-chat-message--user",
            "success": "sky-chat-message--success",
            "error": "sky-chat-message--error",
            "info": "sky-chat-message--info",
        }
        cls = style_map.get(style, style_map["normal"])
        if is_user:
            cls = "sky-chat-message--user"

        with self._chat_container:
            el = ui.element("div").classes(f"sky-chat-message {cls}")
            with el:
                ui.label(text).style(
                    "color: var(--sky-text-primary); font-size:0.875rem; line-height:1.6; word-break:break-word;"
                )

        self._message_elements.append(el)
        if self._chat_scroll:
            self._chat_scroll.scroll_to(percent=1.0)

    def _show_thinking(self) -> None:
        if self._is_thinking:
            return
        self._is_thinking = True
        if self._thinking_label:
            self._thinking_label.style("color: var(--sky-text-muted); font-size:0.75rem; display:block;")
        if self._chat_scroll:
            self._chat_scroll.scroll_to(percent=1.0)

    def _hide_thinking(self) -> None:
        if not self._is_thinking:
            return
        self._is_thinking = False
        if self._thinking_label:
            self._thinking_label.style("color: var(--sky-text-muted); font-size:0.75rem; display:none;")

    def _send_message(self) -> None:
        if not self._chat_input:
            return
        text = self._chat_input.value.strip()
        if not text:
            return

        self.append_chat_message(text, is_user=True)
        self._chat_input.value = ""
        self._show_thinking()

        # FIX: Validar logic_queue antes de enviar
        if not hasattr(self.ctx, "logic_queue") or self.ctx.logic_queue is None:
            logger.error("logic_queue no disponible — mensaje perdido")
            self.append_chat_message(
                "⚠️ Error interno: canal de comunicación no inicializado.",
                is_user=False,
                style="error",
            )
            self._hide_thinking()
            return
        self.ctx.logic_queue.put(("chat", text))

    # ── Footer ────────────────────────────────────────────────────────
    def _build_footer(self) -> None:
        with (
            ui.element("div")
            .classes("px-6 py-3 flex items-center justify-between")
            .style("border-top: 1px solid var(--forge-edge); background: rgba(0,0,0,0.25);")
        ):
            ui.label("\u00a9 2026 Sky-Claw Technical Operations Hub. Todos los derechos reservados.").style(
                "color: var(--parchment-mute); font-family: var(--f-mono); font-size: 0.68rem; letter-spacing: 0.06em;"
            )

            with ui.row().classes("gap-4"):
                for link_text in [
                    "Protocolos de Seguridad",
                    "API Docs",
                    "Soporte Operativo",
                ]:
                    ui.label(link_text).style(
                        "color: var(--parchment-mute); font-family: var(--f-display); "
                        "font-size: 0.68rem; letter-spacing: 0.14em; "
                        "text-transform: uppercase; cursor: pointer;"
                    )

    # ── Queue Polling ─────────────────────────────────────────────────
    def _poll_queue(self) -> None:
        # FIX: getattr defensivo para evitar AttributeError si _running no existe
        if not getattr(self, "_running", False):
            return
        gui_queue = getattr(self.ctx, "gui_queue", None)
        if gui_queue is None:
            return
        try:
            while True:
                msg_type, data = gui_queue.get_nowait()
                handler = self.handlers.get(msg_type)
                if handler:
                    handler.handle(self, data)
                else:
                    logger.warning("Mensaje desconocido en cola UI: '%s'", msg_type)
        except queue.Empty:
            pass
        except AttributeError as e:
            logger.error("AttributeError en _poll_queue (atributo faltante): %s", e)
        except Exception:
            logger.exception("Error procesando cola GUI:")

    # ── Initial Mods ──────────────────────────────────────────────────
    async def _load_initial_mods(self) -> None:
        try:
            mods_dicts = await self.ctx.registry.search_mods("")
            mods = [m["name"] for m in mods_dicts]
            self.update_mod_list(mods)
            self.append_chat_message(
                "Sky-Claw inicializado. Conexión con Nexus/MO2 establecida.",
                is_user=False,
                style="success",
            )
        except Exception as e:
            logger.error("Fallo cargando mods iniciales: %s", e)
            self.append_chat_message(
                f"Error accediendo a la DB: {e}",
                is_user=False,
                style="error",
            )

    # ── Settings Dialog ───────────────────────────────────────────────
    def _open_settings_dialog(self) -> None:
        with (
            ui.dialog() as dialog,
            ui.card().style(
                "background: #0f172a !important; z-index: 9999; "
                "border: 1px solid var(--sky-border); border-radius: 1rem; "
                "padding: 1.5rem; width: 480px;"
            ),
        ):
            ui.add_head_html("""
<style>
body {
    background-color: var(--sky-bg-primary) !important;
    background-image: url('assets/stone_bg.png') !important;
    background-size: 500px !important;
    background-repeat: repeat !important;
    background-attachment: fixed !important;
    color: #f8fafc;
    font-family: 'Futura', 'Trajan Pro', serif;
}

/* Forzar transparencia en los contenedores de NiceGUI para dejar ver el fondo */
#q-app, .q-layout, .q-page-container, .q-page {
    background: transparent !important;
}
</style>
""")
            ui.label("CONFIGURACIÓN").style(
                "color: var(--sky-gold); font-weight:700; font-size:1.1rem; letter-spacing:0.1em; margin-bottom:1rem;"
            )

            # Provider
            ui.label("PROVEEDOR LLM").classes("sky-wizard-label")
            provider_input = (
                ui.toggle(
                    ["anthropic", "deepseek", "ollama"],
                    value=(getattr(self.ctx, "_args", None) and getattr(self.ctx._args, "provider", "deepseek"))
                    or "deepseek",
                )
                .classes("w-full mb-3")
                .props("color=amber")
            )

            # API Key
            ui.label("API KEY").classes("sky-wizard-label")
            api_key_input = (
                ui.input(
                    placeholder="Nueva API key (dejar vacío para no cambiar)",
                )
                .classes("w-full mb-3")
                .props(
                    'type=password dark standout="bg-transparent" '
                    'input-class="sky-wizard-input" color=amber maxlength=512'
                )
            )

            # Nexus
            ui.label("NEXUS API KEY").classes("sky-wizard-label")
            nexus_input = (
                ui.input(
                    placeholder="Nueva Nexus key (dejar vacío para no cambiar)",
                )
                .classes("w-full mb-3")
                .props(
                    'type=password dark standout="bg-transparent" '
                    'input-class="sky-wizard-input" color=amber maxlength=512'
                )
            )

            # Telegram
            ui.label("TELEGRAM BOT TOKEN").classes("sky-wizard-label")
            tg_token_input = (
                ui.input(
                    placeholder="Nuevo token (dejar vacío para no cambiar)",
                )
                .classes("w-full mb-3")
                .props(
                    'type=password dark standout="bg-transparent" '
                    'input-class="sky-wizard-input" color=amber maxlength=512'
                )
            )

            ui.label("TELEGRAM CHAT ID").classes("sky-wizard-label")
            tg_chatid_input = (
                ui.input(
                    placeholder="Nuevo chat ID",
                )
                .classes("w-full mb-4")
                .props('dark standout="bg-transparent" input-class="sky-wizard-input" color=amber maxlength=512')
            )

            status_label = ui.label("").classes("text-sm mb-2")

            with ui.row().classes("w-full justify-end gap-3"):
                ui.button("Cancelar", on_click=dialog.close).classes("px-4 py-2 rounded-lg").props(
                    "ripple flat no-caps"
                ).style("color: var(--sky-text-secondary); border: 1px solid var(--sky-surface-border);")

                async def _save_settings() -> None:
                    try:
                        new_provider = provider_input.value
                        new_api_key = api_key_input.value.strip()
                        new_nexus = nexus_input.value.strip()
                        new_tg_token = tg_token_input.value.strip()
                        new_tg_chatid = tg_chatid_input.value.strip()

                        if new_tg_token and ":" not in new_tg_token:
                            status_label.set_text("Token Telegram inválido")
                            status_label.style("color: var(--sky-error);")
                            return
                        if new_tg_chatid and not new_tg_chatid.isdigit():
                            status_label.set_text("Chat ID debe ser numérico")
                            status_label.style("color: var(--sky-error);")
                            return

                        if new_api_key:
                            keyring.set_password("sky_claw", f"{new_provider}_api_key", new_api_key)
                            keyring.set_password("sky_claw", "llm_api_key", new_api_key)
                        if new_nexus:
                            keyring.set_password("sky_claw", "nexus_api_key", new_nexus)
                        if new_tg_token:
                            keyring.set_password("sky_claw", "telegram_bot_token", new_tg_token)

                        from sky_claw.config import Config

                        cfg = Config(self.ctx.config_path)
                        cfg._data["llm_provider"] = new_provider
                        if new_tg_chatid:
                            cfg._data["telegram_chat_id"] = new_tg_chatid
                        cfg.save()

                        await self._hot_reload_provider(new_provider, new_api_key)

                        if new_tg_token:
                            await self._hot_reload_telegram(new_tg_token, new_tg_chatid)

                        status_label.set_text("Configuración guardada")
                        status_label.style("color: var(--sky-success);")
                        self.append_chat_message(
                            f"Proveedor cambiado a {new_provider}",
                            is_user=False,
                            style="success",
                        )

                    except Exception as e:
                        logger.exception("Error guardando settings:")
                        status_label.set_text(f"Error: {e}")
                        status_label.style("color: var(--sky-error);")

                save_btn = (
                    ui.button("Guardar").classes("sky-wizard-cta px-6 py-2 rounded-xl font-semibold").props("ripple")
                )

                async def _on_save_click() -> None:
                    save_btn.disable()
                    save_btn.set_text("Guardando...")
                    try:
                        await _save_settings()
                    finally:
                        save_btn.enable()
                        save_btn.set_text("Guardar")

                save_btn.on("click", _on_save_click)

        dialog.open()

    async def _hot_reload_provider(self, provider: str, api_key: str) -> None:
        try:
            from sky_claw.antigravity.agent.providers import create_provider

            if hasattr(self.ctx.router, "_provider_lock"):
                async with self.ctx.router._provider_lock:
                    new_prov = create_provider(provider_name=provider)
                    self.ctx.router._provider = new_prov
            else:
                new_prov = create_provider(provider_name=provider)
                self.ctx.router._provider = new_prov
            logger.info("Provider hot-reloaded to %s", provider)
        except Exception as e:
            logger.error("Hot-reload provider failed: %s", e)

    async def _hot_reload_telegram(self, token: str, chat_id: str) -> None:
        try:
            if self.ctx.polling:
                await self.ctx.polling.stop()

            from sky_claw.antigravity.comms.telegram_polling import TelegramPolling
            from sky_claw.antigravity.comms.telegram_sender import TelegramSender

            self.ctx.sender = TelegramSender(
                bot_token=token, gateway=self.ctx.network.gateway, session=self.ctx.session
            )
            cid = int(chat_id) if chat_id else None
            if cid:
                from sky_claw.antigravity.comms.telegram import TelegramWebhook

                webhook_handler = TelegramWebhook(
                    router=self.ctx.router,
                    sender=self.ctx.sender,
                    session=self.ctx.session,
                    hitl=self.ctx.hitl,
                )
                self.ctx.polling = TelegramPolling(
                    token=token,
                    webhook_handler=webhook_handler,
                    gateway=self.ctx.network.gateway,
                    session=self.ctx.session,
                    authorized_chat_id=cid,
                )
                self.ctx._track_task(self.ctx.polling.start(), name="telegram-polling")
            logger.info("Telegram hot-reloaded")
        except Exception as e:
            logger.error("Hot-reload Telegram failed: %s", e)
