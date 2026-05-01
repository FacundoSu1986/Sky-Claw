"""DashboardGUI — Nordic theme premium dashboard."""

from __future__ import annotations

import contextlib
import logging
import queue
from typing import Any

from nicegui import ui

from .gui_helpers import MAX_CHAT_MESSAGES, _load_css
from .icons import (
    _ICON_ANVIL,
    _ICON_CART,
    _ICON_CHAT,
    _ICON_LAYERS,
)
from .message_handlers import (
    ErrorHandler,
    MessageHandlerStrategy,
    ModlistHandler,
    ResponseHandler,
    SuccessHandler,
)
from .setup_wizard import SetupWizardModal
from .views.actions import build_actions_panel
from .views.advanced import build_advanced_panel

logger = logging.getLogger(__name__)


# =============================================================================
# DASHBOARD GUI — Nordic Theme
# =============================================================================


class DashboardGUI:
    """Dashboard premium con sidebar, stats, widgets, chat — tema Nórdico/Rúnico."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._running = True
        self._message_elements: list = []
        self._thinking_el: ui.element | None = None
        self._chat_container: ui.column | None = None
        self._chat_scroll: ui.scroll_area | None = None
        self._mod_container: ui.column | None = None
        self._chat_input: ui.input | None = None
        self._bg_tasks: set = set()
        self._env_snapshot = None  # EnvironmentSnapshot (set on scan)

        self._stat_labels: dict[str, ui.label] = {}
        self._status_dot: ui.element | None = None
        self._status_label: ui.label | None = None
        self._thinking_label: ui.label | None = None
        self._is_thinking = False

        self.handlers: dict[str, MessageHandlerStrategy] = {
            "response": ResponseHandler(),
            "modlist": ModlistHandler(),
            "success": SuccessHandler(),
            "error": ErrorHandler(),
        }

    def build(self) -> None:
        _load_css()

        with (
            ui.element("div")
            .classes("w-full min-h-screen flex")
            .style("background: transparent; font-family: var(--sky-font-family);")
        ):
            self._build_sidebar()

            with ui.column().classes("flex-1 min-h-screen overflow-auto sky-main-content"):
                self._build_header()

                with ui.column().classes("p-6 gap-6 flex-1"):
                    # Row 1: Line chart + Server status
                    with ui.row().classes("w-full gap-6"):
                        self._build_line_chart_widget()
                        self._build_server_status_widget()

                    # Row 2: Stats cards (PROCESADOR + CARGA TAREAS)
                    with ui.row().classes("w-full gap-6"):
                        self._build_stat_summary("PROCESADOR", "116", _ICON_ANVIL)
                        self._build_stat_summary("CARGA TAREAS", "3,130 ops/s", _ICON_CART)

                    # Row 3: Bar chart with metrics
                    self._build_bar_chart_widget()

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
            import keyring

            key = keyring.get_password("sky_claw", f"{provider}_api_key")
            return not bool(key)
        except Exception:
            return False

    async def _on_wizard_complete(self) -> None:
        ui.notify("Sistema inicializado correctamente", type="positive")

    # ── Sidebar ───────────────────────────────────────────────────────
    def _build_sidebar(self) -> None:
        with (
            ui.element("div")
            .classes("w-64 min-h-screen flex flex-col shrink-0 sky-sidebar")
            .style("background: var(--sky-bg-secondary); border-right: 1px solid var(--sky-border);")
        ):
            # Logo
            with (
                ui.element("div").classes("p-6").style("border-bottom: 1px solid var(--sky-border);"),
                ui.row().classes("items-center gap-3"),
            ):
                ui.html(f"""
                        <div style="width:40px;height:40px;border-radius:12px;display:flex;
                                    align-items:center;justify-content:center;
                                    background:linear-gradient(135deg, #C8A84E, #8B7332);"
                             class="sky-glow-static">
                            {_ICON_LAYERS}
                        </div>
                    """)
                with ui.column().classes("gap-0"):
                    ui.label("SKY-CLAW").style(
                        "color: var(--sky-gold); font-weight:800; font-size:1.1rem; letter-spacing:0.15em;"
                    )
                    ui.label("Technical Operations").style("color: var(--sky-text-muted); font-size:0.65rem;")

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
                    if is_active:
                        active_style = (
                            "background: rgba(200,168,78,0.1); "
                            "border-left: 2px solid var(--sky-gold); "
                            "color: var(--sky-gold);"
                        )
                    else:
                        active_style = "color: var(--sky-text-secondary);"

                    ui.button(
                        text,
                        on_click=lambda v=view: self._navigate(v),
                    ).classes("w-full text-left px-4 py-3 rounded-xl transition-all duration-200").props(
                        "ripple flat no-caps"
                    ).style(active_style)

            # Status LLM
            with (
                ui.element("div").classes("p-4").style("border-top: 1px solid var(--sky-border);"),
                ui.row().classes("items-center gap-2"),
            ):
                self._status_dot = ui.element("div").classes("sky-status-dot sky-status-dot--connected")
                self._status_label = ui.label("Conectado").style("color: var(--sky-text-muted); font-size:0.75rem;")

    def _navigate(self, view: str) -> None:
        if view == "settings":
            self._open_settings_dialog()

    # ── Header ────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        with (
            ui.element("div")
            .classes("h-16 flex items-center justify-between px-6 shrink-0")
            .style("background: var(--sky-bg-secondary); border-bottom: 1px solid var(--sky-border);")
        ):
            with ui.column().classes("gap-0"):
                ui.label("OPERACIONES TÉCNICAS").style(
                    "color: var(--sky-text-primary); font-weight:700; font-size:1.1rem; letter-spacing:0.1em;"
                )

            # Header actions
            with ui.row().classes("items-center gap-3"):
                for label_text in ["Protocolos", "Alerta", "Ayuda"]:
                    ui.button(label_text).classes("px-3 py-1 rounded-lg text-xs").props("ripple flat no-caps").style(
                        "color: var(--sky-text-secondary); border: 1px solid var(--sky-surface-border);"
                    )

                # Avatar
                with ui.element("div").style(
                    "width:36px; height:36px; border-radius:50%; display:flex; "
                    "align-items:center; justify-content:center; cursor:pointer; "
                    "background: linear-gradient(135deg, var(--sky-gold), #8B7332); "
                    "color: #1C1714; font-weight:700; font-size:0.8rem;"
                ):
                    ui.label("SC")

    # ── Line Chart Widget ─────────────────────────────────────────────
    def _build_line_chart_widget(self) -> None:
        with ui.element("div").classes("sky-widget-panel flex-1"):
            with ui.element("div").classes("sky-widget-header"):
                ui.label("RENDIMIENTO").classes("sky-widget-title")
                ui.button("Real time").classes("px-2 py-1 rounded text-xs").props("ripple flat no-caps").style(
                    "color: var(--sky-text-muted); border: 1px solid var(--sky-surface-border);"
                )

            with ui.element("div").classes("p-4").style("height:200px; position:relative;"):
                # Simulated line chart via SVG
                ui.html("""
                    <svg width="100%" height="100%" viewBox="0 0 400 160" preserveAspectRatio="none"
                         style="overflow:visible;">
                        <!-- Grid lines -->
                        <line x1="0" y1="0" x2="400" y2="0" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>
                        <line x1="0" y1="40" x2="400" y2="40" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>
                        <line x1="0" y1="80" x2="400" y2="80" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>
                        <line x1="0" y1="120" x2="400" y2="120" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>
                        <line x1="0" y1="160" x2="400" y2="160" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>

                        <!-- Data line -->
                        <polyline fill="none" stroke="#C8A84E" stroke-width="2.5"
                                  points="0,120 30,100 60,80 90,60 120,90 150,50 180,70 210,40 240,60 270,30 300,50 330,45 360,35 400,25"
                                  stroke-linejoin="round" stroke-linecap="round"/>

                        <!-- Glow under line -->
                        <defs>
                            <linearGradient id="lineGrad" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%" stop-color="#C8A84E" stop-opacity="0.3"/>
                                <stop offset="100%" stop-color="#C8A84E" stop-opacity="0"/>
                            </linearGradient>
                        </defs>
                        <polygon fill="url(#lineGrad)"
                                 points="0,120 30,100 60,80 90,60 120,90 150,50 180,70 210,40 240,60 270,30 300,50 330,45 360,35 400,25 400,160 0,160"/>

                        <!-- Y-axis labels -->
                        <text x="-5" y="8" fill="#94A3B8" font-size="10" text-anchor="end">100</text>
                        <text x="-5" y="48" fill="#94A3B8" font-size="10" text-anchor="end">75</text>
                        <text x="-5" y="88" fill="#94A3B8" font-size="10" text-anchor="end">50</text>
                        <text x="-5" y="128" fill="#94A3B8" font-size="10" text-anchor="end">25</text>
                        <text x="-5" y="165" fill="#94A3B8" font-size="10" text-anchor="end">0</text>
                    </svg>
                """)

    # ── Server Status Widget ──────────────────────────────────────────
    def _build_server_status_widget(self) -> None:
        with ui.element("div").classes("sky-widget-panel").style("width: 280px; flex-shrink:0;"):
            with ui.element("div").classes("sky-widget-header"):
                ui.label("ESTADO SERVIDORES").classes("sky-widget-title")

            with ui.column().classes("p-3 gap-2"):
                servers = [
                    ("SERVER 1", "Online", "#22c55e"),
                    ("SERVER 2", "Alerta", "#eab308"),
                    ("SERVER 3", "Alerta", "#ef4444"),
                    ("SERVER 4", "Online", "#22c55e"),
                ]
                for name, status, color in servers:
                    with (
                        ui.row()
                        .classes("items-center justify-between w-full px-3 py-2 rounded-lg")
                        .style("background: var(--sky-bg-elevated);")
                    ):
                        with ui.row().classes("items-center gap-2"):
                            ui.html(f"""
                                <div style="width:10px;height:10px;border-radius:50%;
                                            background:{color};
                                            box-shadow: 0 0 6px {color};"></div>
                            """)
                            ui.label(name).style("color: var(--sky-text-primary); font-size:0.8rem; font-weight:600;")
                        ui.label(status).style(f"color:{color}; font-size:0.7rem; font-weight:500;")

    # ── Stat Summary Cards ────────────────────────────────────────────
    def _build_stat_summary(self, title: str, value: str, icon_svg: str) -> None:
        with (
            ui.element("div").classes("sky-widget-panel flex-1 p-5 sky-card-hover"),
            ui.row().classes("items-center justify-between"),
        ):
            with ui.column().classes("gap-1"):
                ui.label(title).style(
                    "color: var(--sky-text-muted); font-size:0.7rem; "
                    "font-weight:600; letter-spacing:0.1em; text-transform:uppercase;"
                )
                ui.label(value).classes("sky-metric-value")
            ui.html(f"""
                    <div style="width:48px;height:48px;border-radius:12px;display:flex;
                                align-items:center;justify-content:center;
                                background:linear-gradient(135deg, rgba(200,168,78,0.12), rgba(6,182,212,0.12));
                                border: 1px solid rgba(200,168,78,0.2);">
                        {icon_svg}
                    </div>
                """)

    # ── Bar Chart Widget ──────────────────────────────────────────────
    def _build_bar_chart_widget(self) -> None:
        with ui.element("div").classes("sky-widget-panel w-full"):
            with ui.element("div").classes("p-4"):
                # Bar chart
                bar_data = [72, 45, 88, 55, 92, 38, 78, 60, 85, 50, 95, 42]
                with ui.row().classes("items-end justify-center gap-3").style("height:180px;"):
                    for height_pct in bar_data:
                        h = int(height_pct * 1.6)
                        ui.element("div").classes("sky-bar").style(f"height:{h}px; width:24px;")

            # Metrics footer
            with (
                ui.row().classes("w-full justify-around p-4").style("border-top: 1px solid var(--sky-surface-border);")
            ):
                metrics = [
                    ("MEDIA", "64.2%"),
                    ("PICO MÁX", "98.1%"),
                    ("PICO MÍN", "12.4%"),
                    ("ANOMALÍAS", "0"),
                ]
                for label_text, val in metrics:
                    with ui.column().classes("items-center gap-1"):
                        ui.label(val).style("color: var(--sky-text-primary); font-size:1.1rem; font-weight:700;")
                        ui.label(label_text).classes("sky-metric-label")

            # Bottom buttons
            with ui.row().classes("w-full justify-end gap-2 px-4 pb-3"):
                ui.button("Menú").classes("px-3 py-1 rounded-lg text-xs").props("ripple flat no-caps").style(
                    "color: var(--sky-text-secondary); border: 1px solid var(--sky-surface-border);"
                )
                ui.button("Salir").classes("px-3 py-1 rounded-lg text-xs").props("ripple flat no-caps").style(
                    "color: var(--sky-text-secondary); border: 1px solid var(--sky-surface-border);"
                )

    # ── Tools Section — Semantic Actions v4.0 ─────────────────────────
    def _build_tools_section(self) -> None:
        """Build the 'Herramientas' section with health banner + action buttons."""
        with ui.element("div").classes("sky-widget-panel w-full p-4 sky-animate-in"):
            with ui.row().classes("items-center gap-2 mb-3"):
                ui.label("ᚦ").style("font-size:1.4rem; color: var(--sky-amber);")
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
                    ui.notify(f"Abriendo descarga de {m.name}...", type="info")
                    ui.navigate.to(m.download_url, new_tab=True)
                    return
        ui.notify(f"Instalación de {tool_key} pendiente", type="warning")

    # ── Mod Panel ─────────────────────────────────────────────────────
    def _build_mod_panel(self) -> None:
        with ui.column().classes("w-1/2 sky-widget-panel overflow-hidden sky-animate-in--delay-1 sky-animate-in"):
            with (
                ui.row()
                .classes("items-center justify-between p-4")
                .style("border-bottom: 1px solid var(--sky-surface-border);")
            ):
                ui.label("Mods Instalados").style("color: var(--sky-text-primary); font-weight:700;")
                with ui.row().classes("gap-2"):
                    ui.button(
                        "Actualizar",
                        on_click=self._update_all,
                    ).classes("px-3 py-1 rounded-lg text-xs").props("ripple flat no-caps").style(
                        "color: var(--sky-text-secondary); border: 1px solid var(--sky-surface-border);"
                    )
                    ui.button(
                        "Escanear",
                        on_click=self._scan_all,
                    ).classes("px-3 py-1 rounded-lg text-xs").props("ripple flat no-caps").style(
                        "color: var(--sky-text-secondary); border: 1px solid var(--sky-surface-border);"
                    )

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
                    "border-bottom: 1px solid var(--sky-surface-border); "
                    "background: linear-gradient(135deg, rgba(200,168,78,0.08), rgba(6,182,212,0.08));"
                ),
                ui.row().classes("items-center gap-3"),
            ):
                ui.html(f"""
                        <div style="width:40px;height:40px;border-radius:12px;display:flex;
                                    align-items:center;justify-content:center;
                                    background:linear-gradient(135deg, #C8A84E, #06b6d4);"
                             class="sky-glow-static">
                            {_ICON_CHAT}
                        </div>
                    """)
                with ui.column().classes("gap-0"):
                    ui.label("Asistente IA").style("color: var(--sky-text-primary); font-weight:700;")
                    ui.label("Escribiendo...").style("color: var(--sky-text-muted); font-size:0.75rem; display:none;")

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
                ).classes("sky-wizard-cta px-5 py-2 rounded-xl font-semibold").props("ripple")

    def append_chat_message(self, text: str, is_user: bool = False, style: str = "normal") -> None:
        self._hide_thinking()

        while len(self._message_elements) >= MAX_CHAT_MESSAGES:
            oldest = self._message_elements.pop(0)
            with contextlib.suppress(ValueError, KeyError):
                self._chat_container.remove(oldest)

        style_map = {
            "normal": "sky-chat-message--assistant" if not is_user else "sky-chat-message--user",
            "success": "sky-chat-message--success",
            "error": "sky-chat-message--error",
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
        self.ctx.logic_queue.put(("chat", text))

    # ── Footer ────────────────────────────────────────────────────────
    def _build_footer(self) -> None:
        with (
            ui.element("div")
            .classes("px-6 py-3 flex items-center justify-between")
            .style("border-top: 1px solid var(--sky-surface-border);")
        ):
            ui.label("\u00a9 2026 Sky-Claw Technical Operations Hub. Todos los derechos reservados.").style(
                "color: var(--sky-text-muted); font-size:0.7rem;"
            )

            with ui.row().classes("gap-4"):
                for link_text in [
                    "Protocolos de Seguridad",
                    "API Docs",
                    "Soporte Operativo",
                ]:
                    ui.label(link_text).style("color: var(--sky-text-muted); font-size:0.7rem; cursor:pointer;")

    # ── Queue Polling ─────────────────────────────────────────────────
    def _poll_queue(self) -> None:
        if not self._running:
            return
        try:
            while True:
                msg_type, data = self.ctx.gui_queue.get_nowait()
                handler = self.handlers.get(msg_type)
                if handler:
                    handler.handle(self, data)
                else:
                    logger.warning("Mensaje desconocido en cola UI: '%s'", msg_type)
        except queue.Empty:
            pass
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
                "background: var(--sky-bg-card); border: 1px solid var(--sky-border); "
                "border-radius: 1rem; padding: 1.5rem; width: 480px;"
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

                        import keyring

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

                ui.button("Guardar", on_click=_save_settings).classes(
                    "sky-wizard-cta px-6 py-2 rounded-xl font-semibold"
                ).props("ripple")

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

            self.ctx.sender = TelegramSender(bot_token=token, gateway=self.ctx.gateway, session=self.ctx.session)
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
                    gateway=self.ctx.gateway,
                    session=self.ctx.session,
                    authorized_chat_id=cid,
                )
                self.ctx._track_task(self.ctx.polling.start(), name="telegram-polling")
            logger.info("Telegram hot-reloaded")
        except Exception as e:
            logger.error("Hot-reload Telegram failed: %s", e)
