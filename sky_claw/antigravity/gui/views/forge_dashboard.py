"""Forge Panel — faithful port of the HiFi "Panel del Draconato" mockup.

Ports the standalone design (Sky-Claw Forge.dc.html) to the live NiceGUI shell.
The mockup is inline-styled (not class-based), so we replicate the exact inline
styles here for pixel fidelity. Decorative chrome (hero veils, embers, brackets,
SVGs, runic lines) is emitted via ``ui.html``; interactive controls (nav, Preparar
Juego, Ver Todo, chat) are real elements wired to the existing callbacks.

Keyframes used by the inline styles (scAurora/scEmber/scPulse/scShimmer/scBlink)
live in styles.css §14. Entry point :func:`render_forge_dashboard` keeps the same
signature as the old ``render_dashboard`` so the wiring in ``sky_claw_gui`` is
untouched.
"""

from __future__ import annotations

import contextlib
import html as _html
from collections.abc import Callable
from typing import Any

from nicegui import app, ui

from sky_claw.antigravity.gui.controllers.ritual_runner import (
    CLIENT_KEY_AUTO_APPROVE,
    RITUAL_INSTALLER_MAP,
    STORE_KEY_PENDING_HITL,
    STORE_KEY_RITUAL_FEEDBACK,
)
from sky_claw.antigravity.gui.state import get_store

# Stash for the HITL respond callback so the module-level modal refreshable can
# reach it (set per render in render_forge_dashboard, like the live panels read
# the global store). Keyed to keep the indirection explicit.
_HITL_CALLBACKS: dict[str, Callable] = {}

ACCENT = "#c8a86a"
ACCENT_BRIGHT = "#ecd9a8"
GLOW = "rgba(200,168,106,.45)"
RED = "#d8584e"
RED_SOFT = "#e88a82"
FROST = "#86b9d4"
GREEN = "#5f9c6b"

# ── Reactive-store keys for live system data (written by the bootloader) ────────
# Telemetry percentages (0-100) sampled by TelemetryDaemon → CoreEventBus →
# the GUI store. ``sys_gpu`` is ``None`` when no NVIDIA GPU/pynvml is present.
STORE_KEY_CPU = "sys_cpu"
STORE_KEY_GPU = "sys_gpu"
STORE_KEY_RAM = "sys_ram"
# EnvironmentSnapshot produced by EnvironmentScanner.scan() at startup.
STORE_KEY_ENV = "environment_snapshot"

# Heartbeat (seconds) for the component-level live refresh of the Vitalidad bars
# and the header HUD. A ``ui.timer`` re-renders ONLY those two containers at this
# cadence so CPU/GPU/RAM visibly pulse without a full page refresh (which would
# reset the chat input). Telemetry itself is sampled at ~1 Hz upstream; 2.5s keeps
# the bars lively while staying cheap (Codex review #3 on #209).
LIVE_REFRESH_SECONDS = 2.5

# ── Navigation (label, section key, lucide path, tone) ─────────────────────────
_NAV: list[dict[str, str]] = [
    {"label": "Panel", "key": "Dashboard", "d": "M3 9.5 12 3l9 6.5V20a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"},
    {
        "label": "Mods",
        "key": "Mods",
        "d": "M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z",
    },
    {
        "label": "Conflictos",
        "key": "Conflicts",
        "d": "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
    },
    {"label": "Descargas", "key": "Downloads", "d": "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"},
    {
        "label": "Ajustes",
        "key": "Settings",
        "d": "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
    },
]

# ── Rituales de la Forja (rune · label · desc · técnico · tono) ─────────────────
#: ``tool`` maps each ritual to its EnvironmentScanner key (scanner.py tool_defs)
#: so availability is derived from a real disk scan, not hardcoded.
_RITUALS: list[dict[str, str]] = [
    {
        "rune": "ᚠ",
        "label": "Ordenar Mods",
        "desc": "Organiza el orden de carga para evitar conflictos.",
        "tech": "LOOT",
        "tool": "loot",
        "tone": ACCENT,
    },
    {
        "rune": "ᚱ",
        "label": "Limpiar Archivos",
        "desc": "Elimina registros sucios de los plugins oficiales.",
        "tech": "SSEEdit",
        "tool": "xedit",
        "tone": ACCENT,
    },
    {
        "rune": "ᛞ",
        "label": "Crear Parche",
        "desc": "Genera un parche de compatibilidad entre tus mods.",
        "tech": "Wrye Bash",
        "tool": "wrye_bash",
        "tone": ACCENT,
    },
    {
        "rune": "ᛏ",
        "label": "Generar Animaciones",
        "desc": "Actualiza los grafos de comportamiento del juego.",
        "tech": "Pandora",
        "tool": "pandora",
        "tone": "#9c7a40",
    },
    {
        "rune": "ᛗ",
        "label": "Optimizar Gráficos",
        "desc": "Genera LODs para el rendimiento visual a distancia.",
        "tech": "DynDOLOD",
        "tool": "dyndolod",
        "tone": ACCENT,
    },
]

_STAT_RUNES = {"mods": "ᛗ", "pending": "ᛒ", "conflicts": "ᚤ", "space": "ᛜ"}


def _e(s: Any) -> str:
    return _html.escape(str(s))


def _cb(callbacks: dict[str, Callable], name: str) -> Callable | None:
    fn = callbacks.get(name)
    return fn if callable(fn) else None


def _fmt_pct(value: float | None) -> str:
    """Format a 0-100 metric as ``"NN%"``, or ``"N/D"`` when unknown (None)."""
    if value is None:
        return "N/D"
    return f"{int(round(value))}%"


def _initials(name: str) -> str:
    """Deriva hasta 2 letras de iniciales del nombre visible (A3).

    Con una sola palabra toma sus dos primeras letras; con varias, la inicial de
    las dos primeras. Cae a ``"?"`` cuando no hay nombre — así el avatar deja de
    ser el literal "DS" hardcodeado y pasa a derivarse del estado.
    """
    words = str(name or "").split()
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _identity_html(name: str, role: str) -> str:
    """Construye el bloque de identidad del header (A3), data-driven.

    Seam puro (sin ``ui.*``): reemplaza los literales "Dovahkiin" / "Maestro de
    la Forja" / "DS" por ``name`` / ``role`` y las iniciales calculadas. Escapa
    todo el contenido para no inyectar HTML.
    """
    return (
        '<div style="display:flex; align-items:center; gap:11px; padding-left:16px; border-left:1px solid rgba(200,168,106,.16);">'
        '<div style="text-align:right;">'
        f"<div style=\"font-family:'Cinzel',serif; font-size:13px; color:#e6dcc4; letter-spacing:.04em;\">{_e(name)}</div>"
        f"<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:11.5px; color:#8a7f6a;\">{_e(role)}</div></div>"
        "<div style=\"width:42px; height:42px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-family:'Cinzel',serif; font-weight:700; font-size:15px; color:#1a120c; background:radial-gradient(circle at 38% 32%, #f0d79a, #c8a86a 62%, #8a6c38); border:1.5px solid #f0d79a; box-shadow:0 0 16px rgba(200,168,106,.45);\">"
        f"{_e(_initials(name))}</div></div>"
    )


def _vital_bar_width(value: float | None) -> int:
    """Clamp a metric to a 0-100 bar width; unknown metrics render empty."""
    if value is None:
        return 0
    return max(0, min(100, int(round(value))))


def _ritual_status(snapshot: Any, tool_key: str) -> str:
    """Derive a ritual's tool state from the environment snapshot.

    Returns ``"available"`` / ``"missing"`` when a scan has run, or
    ``"unknown"`` before the first :class:`EnvironmentScanner` snapshot lands
    in the store — so the UI never claims a tool is installed without proof.
    """
    if snapshot is None:
        return "unknown"
    has_tool = getattr(snapshot, "has_tool", None)
    if not callable(has_tool):
        return "unknown"
    return "available" if has_tool(tool_key) else "missing"


def _derive_status(m: dict[str, Any]) -> str:
    """Map a mod row to a display status.

    Live registry rows (from ``ctx.registry.search_mods``) carry
    ``installed``/``enabled_in_vfs`` rather than ``status``, so defaulting to
    "active" would paint disabled/uninstalled mods as enabled (Codex review on
    #208). Honour an explicit ``status`` first, then the registry flags, and fall
    back to the safe "inactive".
    """
    raw = m.get("status")
    if raw:
        return str(raw)
    if "enabled_in_vfs" in m or "installed" in m:
        return "active" if m.get("enabled_in_vfs") else "inactive"
    return "inactive"


# ═══════════════════════════════════════════════════════════════════════════════
def render_forge_dashboard(
    stats: dict[str, Any],
    mods: list[dict[str, Any]],
    chat_messages: list[dict[str, Any]],
    is_thinking: bool,
    callbacks: dict[str, Callable],
    active_section: str = "Dashboard",
    identity: dict[str, str] | None = None,
    search_query: str = "",
    conflicts_list: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    downloads: dict[str, Any] | None = None,
    resolved_conflicts: list[dict[str, Any]] | None = None,
) -> None:
    """Render the full Forge shell (sidebar + header + scroll content).

    ``identity`` alimenta el bloque de usuario del header (A3) y ``search_query``
    pre-filtra la pantalla de Mods cuando el usuario busca desde el header (A1).
    """
    active = int(_safe(stats, "active_mods"))
    pending = int(_safe(stats, "pending_updates"))
    conflicts = int(_safe(stats, "conflicts_count"))
    storage = _safe(stats, "storage_used")
    connected = bool(get_store().get("is_agent_connected"))

    root = (
        "--sky-accent:#c8a86a; position:relative; display:flex; min-height:100vh; width:100%;"
        "font-family:'EB Garamond',Georgia,serif; color:#e8e2d4;"
        "background:radial-gradient(130% 62% at 50% -12%, rgba(64,156,131,.18), transparent 56%),"
        "linear-gradient(180deg, rgba(8,11,15,.93), rgba(7,9,12,.975)), url('/assets/stone_bg.png');"
        "background-size:auto,auto,440px; background-attachment:fixed; -webkit-font-smoothing:antialiased;"
    )
    # Fase 2: expose the HITL respond callback to the module-level modal panel.
    _HITL_CALLBACKS["respond"] = _cb(callbacks, "on_hitl_respond")
    with ui.element("div").style(root):
        # Live heartbeat for the vitals bars + header HUD. Refresh ONLY those two
        # @ui.refreshable containers (never the whole page), so CPU/GPU/RAM pulse
        # while the chat input keeps its text. Created inside the page slot so it is
        # torn down on navigation / full refresh — timers never pile up (Codex #3).
        ui.timer(LIVE_REFRESH_SECONDS, lambda: (_vitals_panel.refresh(), _hud_panel.refresh()))
        # Keyboard shortcut (F8) to flip "Modo local" without reaching for the mouse.
        ui.keyboard(on_key=_on_modo_local_key)
        ui.html(
            '<div style="position:absolute; inset:0; pointer-events:none; z-index:0;'
            " box-shadow:inset 0 0 220px 50px rgba(0,0,0,.72);"
            ' background:radial-gradient(135% 95% at 50% -5%, transparent 58%, rgba(0,0,0,.4));"></div>'
        )
        # Overlays driven by the store (own refreshables → never reset the chat).
        _hitl_modal_panel()
        _ritual_feedback_panel()
        _sidebar(active, conflicts, pending, active_section, callbacks, connected)
        with ui.element("div").style(
            "position:relative; z-index:2; flex:1; min-width:0; display:flex; flex-direction:column;"
        ):
            _header(active_section, callbacks, identity, search_query)
            with ui.element("div").classes("sc-scroll").style("flex:1; overflow-y:auto; padding:26px 30px 40px;"):
                if active_section in ("Dashboard", "Panel"):
                    _hero(active, conflicts, callbacks)
                    _stats(active, pending, conflicts, storage)
                    _rituales(callbacks)
                    with ui.element("div").style(
                        "display:grid; grid-template-columns:repeat(auto-fit,minmax(440px,1fr)); gap:22px;"
                    ):
                        _orden_carga(mods, callbacks)
                        _asistente(chat_messages, is_thinking, callbacks)
                    _footer_rune()
                elif active_section == "Mods":
                    _mods_screen(mods, callbacks, search_query)
                elif active_section == "Conflicts":
                    _conflicts_screen(conflicts_list or [], callbacks, resolved_conflicts or [])
                elif active_section == "Settings":
                    _settings_screen(settings or {}, callbacks)
                elif active_section == "Downloads":
                    _downloads_screen(downloads or {}, callbacks)
                else:
                    _placeholder(active_section, callbacks)


def _safe(stats: dict[str, Any], key: str) -> float:
    v = stats.get(key)
    try:
        return float(v.get()) if hasattr(v, "get") else float(v or 0)
    except Exception:
        return 0.0


# ── SIDEBAR ────────────────────────────────────────────────────────────────────
def _sidebar(
    active: int, conflicts: int, pending: int, section: str, callbacks: dict[str, Callable], connected: bool
) -> None:
    aside = (
        "position:relative; z-index:3; width:266px; flex-shrink:0; display:flex; flex-direction:column;"
        "background:linear-gradient(176deg,#241710 0%,#1a120c 46%,#0d0907 100%);"
        "border-right:1px solid rgba(200,168,106,.32); box-shadow:inset -1px 0 0 rgba(255,255,255,.03), 14px 0 40px -20px rgba(0,0,0,.9);"
    )
    counts = {"Mods": active, "Conflicts": conflicts, "Downloads": pending}
    with ui.element("aside").style(aside):
        ui.html(
            '<div style="position:absolute; top:0; right:0; bottom:0; width:1px;'
            ' background:linear-gradient(180deg,transparent,#c8a86a,transparent); opacity:.55;"></div>'
        )
        # Brand
        ui.html(
            '<div style="padding:22px 20px 18px; border-bottom:1px solid rgba(200,168,106,.16); display:flex; align-items:center; gap:13px;">'
            '<div style="position:relative; width:46px; height:46px; flex-shrink:0; border-radius:50%; display:flex; align-items:center; justify-content:center;'
            " background:radial-gradient(circle at 50% 38%, #2a1d12, #0c0805); border:1.5px solid #c8a86a;"
            ' box-shadow:0 0 18px rgba(200,168,106,.45), inset 0 0 12px rgba(0,0,0,.7);">'
            '<svg width="30" height="30" viewBox="0 0 48 48" fill="none">'
            '<path d="M5 24C13 14 35 14 43 24C35 34 13 34 5 24Z" fill="#0a0705" stroke="#c8a86a" stroke-width="1.5"/>'
            '<ellipse cx="24" cy="24" rx="9" ry="9" fill="url(#scIris)"/>'
            '<path d="M24 15C26.6 18.2 26.6 29.8 24 33C21.4 29.8 21.4 18.2 24 15Z" fill="#120a06"/>'
            '<defs><radialGradient id="scIris" cx="50%" cy="42%" r="60%"><stop offset="0%" stop-color="#ffd071"/>'
            '<stop offset="55%" stop-color="#d49a36"/><stop offset="100%" stop-color="#7a531f"/></radialGradient></defs></svg></div>'
            '<div style="min-width:0;">'
            '<div style="font-family:\'Cinzel\',serif; font-weight:800; font-size:19px; letter-spacing:.16em; color:#f1e6cf; line-height:1;">SKY<span style="color:#c8a86a;">·</span>CLAW</div>'
            "<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12.5px; color:#9a7f4f; margin-top:3px;\">Forja del Dovahkiin</div>"
            "</div></div>"
        )
        # Connection — driven by the real websocket state (Codex review on #208);
        # a hardcoded green pulse would give a false health signal when offline.
        if connected:
            dot = "background:#5f9c6b; box-shadow:0 0 9px #5f9c6b; animation:scPulse 2.6s ease-in-out infinite;"
            border, label, label_color = "rgba(95,156,107,.28)", "DAEMON CONECTADO", "#bfcfb6"
        else:
            dot = "background:#857c69;"
            border, label, label_color = "rgba(200,168,106,.18)", "DAEMON DESCONECTADO", "#a39a85"
        ui.html(
            f'<div style="margin:14px 18px 4px; padding:9px 12px; display:flex; align-items:center; gap:10px; background:rgba(0,0,0,.28); border:1px solid {border}; border-radius:3px;">'
            f'<span style="width:9px; height:9px; border-radius:50%; {dot}"></span>'
            '<div style="flex:1; min-width:0;">'
            f"<div style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.12em; color:{label_color};\">{label}</div>"
            "<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; color:#6f7a68; margin-top:1px;\">ws://localhost:8765</div>"
            "</div></div>"
        )
        ui.html(
            '<div style="display:flex; align-items:center; justify-content:center; gap:.7em; margin:14px 18px 6px; color:#c8a86a; opacity:.85;'
            ' font-family:\'Noto Sans Runic\',serif; font-size:13px; letter-spacing:.5em; text-shadow:0 0 10px rgba(200,168,106,.45);" aria-hidden="true">ᚠ&nbsp;ᚱ&nbsp;ᚷ</div>'
        )
        # Nav
        with ui.element("nav").style("flex:1; padding:6px 14px; overflow-y:auto;"):
            ui.html(
                "<div style=\"font-family:'Cinzel',serif; font-size:10px; font-weight:600; letter-spacing:.28em; color:#6b6151; padding:6px 12px 12px;\">NAVEGACIÓN</div>"
            )
            on_nav = _cb(callbacks, "on_navigate")
            for item in _NAV:
                _nav_item(item, item["key"] == section, counts.get(item["key"]), on_nav)
        # Vitality
        _vitals()


def _nav_item(item: dict[str, str], is_active: bool, count: int | None, on_nav: Callable | None) -> None:
    row_bg = "rgba(200,168,106,.1)" if is_active else "transparent"
    icon_color = ACCENT_BRIGHT if is_active else "#9a917d"
    label_color = "#f1e6cf" if is_active else "#c4bca8"
    marker = "1" if is_active else "0"
    btn = ui.element("button").style(
        f"position:relative; width:100%; display:flex; align-items:center; gap:13px; padding:11px 14px; margin-bottom:3px;"
        f"border:none; border-radius:4px; cursor:pointer; text-align:left; background:{row_bg}; transition:background .25s;"
    )
    if on_nav:
        btn.on("click", lambda _=None, k=item["key"]: on_nav(k))
    count_html = (
        f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; color:#9a917d; opacity:.85;\">{_e(count)}</span>"
        if count
        else ""
    )
    with btn:
        ui.html(
            f'<span style="position:absolute; left:0; top:18%; bottom:18%; width:2.5px; border-radius:2px;'
            f' background:linear-gradient(180deg,transparent,#ecd9a8,transparent); opacity:{marker}; box-shadow:0 0 10px rgba(200,168,106,.45);"></span>'
            f'<svg viewBox="0 0 24 24" fill="none" stroke="{icon_color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" width="18" height="18" style="flex-shrink:0;"><path d="{item["d"]}"></path></svg>'
            f"<span style=\"flex:1; font-family:'Cinzel',serif; font-size:14px; font-weight:500; letter-spacing:.06em; color:{label_color};\">{_e(item['label'])}</span>"
            f"{count_html}"
        )


def _vitals_html() -> str:
    """Build the inner HTML for the Vitalidad panel from live store telemetry.

    Pure seam (no ``ui.*`` calls, fully testable): reads ``sys_cpu/gpu/ram`` and
    renders the bars, painting "N/D" when a metric is ``None`` (e.g. GPU on a box
    with no NVIDIA GPU/pynvml) instead of a fabricated number. The live timer in
    :func:`render_forge_dashboard` re-runs this via :func:`_vitals_panel` so the
    bars visibly pulse without resetting the chat input.
    """
    store = get_store()
    rows = [
        ("Procesador", store.get(STORE_KEY_CPU), "#5f9c6b", "#2f5036"),
        ("Gráficos", store.get(STORE_KEY_GPU), "#c8a86a", "#6a5026"),
        ("Memoria", store.get(STORE_KEY_RAM), "#86b9d4", "#3a5a6a"),
    ]
    bars = "".join(
        f'<div style="margin-bottom:11px;">'
        f'<div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px;">'
        f"<span style=\"font-family:'EB Garamond',serif; font-size:12.5px; color:#b8b1a0;\">{lbl}</span>"
        f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:{col};\">{_fmt_pct(val)}</span></div>"
        f'<div style="height:5px; border-radius:3px; background:rgba(255,255,255,.06); overflow:hidden; box-shadow:inset 0 1px 2px rgba(0,0,0,.5);">'
        f'<div style="height:100%; width:{_vital_bar_width(val)}%; border-radius:3px; background:linear-gradient(90deg,{dim},{col}); box-shadow:0 0 8px {col};"></div></div></div>'
        for lbl, val, col, dim in rows
    )
    return (
        '<div style="padding:16px 20px 18px; border-top:1px solid rgba(200,168,106,.16);">'
        "<div style=\"font-family:'Cinzel',serif; font-size:10px; font-weight:600; letter-spacing:.22em; color:#6b6151; margin-bottom:12px;\">VITALIDAD DEL SISTEMA</div>"
        f"{bars}"
        '<div style="margin-top:14px; display:flex; align-items:center; justify-content:space-between;">'
        "<span style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; color:#5f5849;\">v2.0 · NORDIC</span>"
        '<span style="font-family:\'Noto Sans Runic\',serif; font-size:12px; color:#c8a86a; opacity:.85;" aria-hidden="true">ᛞᚱᚪᚷᚩᚾ</span></div></div>'
    )


@ui.refreshable
def _vitals_panel() -> None:
    """Refreshable wrapper around :func:`_vitals_html` (refreshed by the timer)."""
    ui.html(_vitals_html())


def _vitals() -> None:
    _vitals_panel()


def _hud_html() -> str:
    """Build the header's GPU·CPU telemetry HUD from the live store.

    Pure seam (testable): mirrors ``sys_gpu``/``sys_cpu`` as ``"NN%"`` / ``"N/D"``.
    Re-rendered by the live timer via :func:`_hud_panel` so the HUD pulses in step
    with the Vitalidad bars, independent of full page refreshes.
    """
    store = get_store()
    gpu_hud = _fmt_pct(store.get(STORE_KEY_GPU))
    cpu_hud = _fmt_pct(store.get(STORE_KEY_CPU))
    return (
        "<div style=\"display:flex; gap:14px; font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#857c69;\">"
        f'<span>GPU <b style="color:#c8a86a;">{_e(gpu_hud)}</b></span><span>CPU <b style="color:#c8a86a;">{_e(cpu_hud)}</b></span></div>'
    )


@ui.refreshable
def _hud_panel() -> None:
    """Refreshable wrapper around :func:`_hud_html` (refreshed by the timer)."""
    ui.html(_hud_html())


# ── MODO LOCAL (HITL auto-approve toggle) ────────────────────────────────────────
def modo_local_enabled() -> bool:
    """Read THIS client's "Modo local" toggle from per-connection storage.

    Lives in ``app.storage.client`` (server-side, one entry per browser
    connection, auto-cleared on disconnect) so one window's choice never affects
    another client. Falls back to ``False`` (fail-closed) when there is no client
    context — e.g. unit tests or a background task (Codex review on #211).
    """
    try:
        return bool(app.storage.client.get(CLIENT_KEY_AUTO_APPROVE, False))
    except Exception:
        return False


def _set_modo_local(value: bool) -> None:
    # Suppress: no client context (unit tests / background) — nothing to persist.
    with contextlib.suppress(Exception):
        app.storage.client[CLIENT_KEY_AUTO_APPROVE] = bool(value)


def _toggle_auto_approve() -> None:
    """Flip this client's "Modo local" auto-approve flag (per-connection)."""
    _set_modo_local(not modo_local_enabled())


def _on_modo_local_key(e: Any) -> None:
    """F8 toggles Modo local. Guarded so non-F8 keys and key-ups are ignored."""
    action = getattr(e, "action", None)
    if action is None or not getattr(action, "keydown", False) or getattr(action, "repeat", False):
        return
    if str(getattr(e, "key", "")) == "F8":
        _toggle_auto_approve()
        _modo_local_panel.refresh()


@ui.refreshable
def _modo_local_panel() -> None:
    """Header toggle: 🔓 Modo local (auto-aprobar) / 🔒 Confirmar (default).

    When ON, destructive Ritual approvals are auto-granted so the operator at the
    PC isn't prompted each time; OFF shows the Aprobar/Denegar modal. Default OFF
    (fail-closed), per-client, resets on disconnect. Atajo: F8.
    """
    on = modo_local_enabled()
    if on:
        icon, label, color, border, title = (
            "🔓",
            "Modo local",
            "#9bbf8e",
            "rgba(95,156,107,.5)",
            "Auto-aprobando acciones (estás en la PC). F8 para alternar.",
        )
    else:
        icon, label, color, border, title = (
            "🔒",
            "Confirmar",
            "#a39a85",
            "rgba(200,168,106,.28)",
            "Pide confirmación antes de cada acción. F8 para alternar.",
        )
    btn = ui.element("button").style(
        f"display:flex; align-items:center; gap:6px; padding:7px 11px; cursor:pointer; border-radius:5px;"
        f"font-family:'Cinzel',serif; font-size:10.5px; letter-spacing:.06em; color:{color};"
        f"background:rgba(62,39,35,.4); border:1px solid {border};"
    )
    btn.props(f'title="{_e(title)}"')
    btn.on("click", lambda _=None: (_toggle_auto_approve(), _modo_local_panel.refresh()))
    with btn:
        ui.html(f'<span aria-hidden="true">{icon}</span><span>{_e(label)}</span>')


# ── HITL APPROVAL MODAL + RITUAL FEEDBACK (store-driven overlays) ─────────────────
def _respond_hitl(request_id: str, approved: bool) -> None:
    """Clear the pending prompt and forward the decision to the HITL guard."""
    get_store().set(STORE_KEY_PENDING_HITL, None)
    fn = _HITL_CALLBACKS.get("respond")
    if callable(fn):
        fn(request_id, approved)


def _hitl_modal_visible(pending: dict[str, Any] | None, active_section: str) -> bool:
    """Seam puro: si corresponde mostrar el modal HITL global.

    En Descargas se suprime: el overlay full-screen taparía la Puerta de
    Aprobación inline, que muestra la misma solicitud con más contexto (la
    URL de la descarga, que el modal no tiene) — review Codex #224.
    """
    return bool(pending) and active_section != "Downloads"


@ui.refreshable
def _hitl_modal_panel() -> None:
    """Overlay asking the operator to approve/deny a destructive Ritual.

    Rendered only while ``pending_hitl`` is set (the GUI HITL bridge parks it
    there when Modo local is off) and the active section is not Downloads —
    there the inline gate takes over. Buttons forward the decision through the
    ``on_hitl_respond`` callback; the guard's timeout still auto-denies.
    """
    pending = get_store().get(STORE_KEY_PENDING_HITL)
    if not _hitl_modal_visible(pending, str(get_store().get("active_section") or "")):
        return
    request_id = str(pending.get("request_id", ""))
    reason = str(pending.get("reason", "") or "Esta acción requiere tu aprobación.")
    detail = str(pending.get("detail", "") or "")
    overlay = (
        "position:fixed; inset:0; z-index:1000; display:flex; align-items:center; justify-content:center;"
        "background:rgba(7,9,12,.72); backdrop-filter:blur(3px);"
    )
    card = (
        "max-width:460px; width:90%; padding:24px 26px; border-radius:6px; color:#e8e2d4;"
        "background:linear-gradient(168deg, rgba(30,22,14,.98), rgba(14,10,7,.99)); border:1px solid rgba(200,168,106,.4);"
        "box-shadow:0 30px 70px -20px rgba(0,0,0,.9), inset 0 1px 0 rgba(255,255,255,.05);"
    )
    detail_html = (
        f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:#8a8270; margin-bottom:16px; word-break:break-word;\">{_e(detail)}</div>"
        if detail
        else '<div style="margin-bottom:16px;"></div>'
    )
    with ui.element("div").style(overlay), ui.element("div").style(card):
        ui.html(
            '<div style="display:flex; align-items:center; gap:10px; margin-bottom:12px;">'
            '<span style="font-size:20px;" aria-hidden="true">🛡️</span>'
            "<span style=\"font-family:'Cinzel',serif; font-weight:700; font-size:15px; letter-spacing:.1em; color:#f1e6cf;\">APROBACIÓN REQUERIDA</span></div>"
            f"<div style=\"font-family:'EB Garamond',serif; font-size:14px; line-height:1.5; color:#d8cfba; margin-bottom:8px;\">{_e(reason)}</div>"
            f"{detail_html}"
            "<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:11.5px; color:#857c69; margin-bottom:14px;\">Tip: activá «Modo local» (F8) para no aprobar cada acción mientras estés en la PC.</div>"
        )
        with ui.element("div").style("display:flex; gap:11px; justify-content:flex-end;"):
            deny = ui.element("button").style(
                "padding:9px 18px; cursor:pointer; font-family:'Cinzel',serif; font-size:12px; letter-spacing:.08em;"
                "color:#e88a82; background:rgba(0,0,0,.3); border:1px solid rgba(197,82,74,.5); border-radius:4px;"
            )
            deny.on("click", lambda _=None, rid=request_id: _respond_hitl(rid, False))
            with deny:
                ui.html("Denegar")
            ok = ui.element("button").style(
                "padding:9px 18px; cursor:pointer; font-family:'Cinzel',serif; font-weight:700; font-size:12px; letter-spacing:.08em;"
                "color:#1c130a; background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40); border:1.5px solid #f6e6bd; border-radius:4px;"
            )
            ok.on("click", lambda _=None, rid=request_id: _respond_hitl(rid, True))
            with ok:
                ui.html("Aprobar")


@ui.refreshable
def _ritual_feedback_panel() -> None:
    """Dismissible toast (bottom-right) showing the last Ritual's result."""
    fb = get_store().get(STORE_KEY_RITUAL_FEEDBACK)
    if not fb:
        return
    text = str(fb.get("text", ""))
    kind = str(fb.get("type", "info"))
    accent = {"positive": GREEN, "negative": RED, "warning": "#e0b341"}.get(kind, ACCENT)
    wrap = (
        "position:fixed; right:22px; bottom:22px; z-index:1000; max-width:380px; display:flex; align-items:flex-start; gap:10px;"
        "padding:13px 15px; border-radius:5px; color:#e8e2d4;"
        f"background:linear-gradient(168deg, rgba(30,22,14,.97), rgba(14,10,7,.98)); border:1px solid {accent};"
        "box-shadow:0 18px 40px -18px rgba(0,0,0,.85);"
    )
    with ui.element("div").style(wrap):
        ui.html(
            f'<span style="width:8px; height:8px; margin-top:5px; flex-shrink:0; border-radius:50%; background:{accent}; box-shadow:0 0 7px {accent};"></span>'
            f"<span style=\"flex:1; font-family:'EB Garamond',serif; font-size:13px; line-height:1.4;\">{_e(text)}</span>"
        )
        x = ui.element("button").style(
            "cursor:pointer; background:none; border:none; color:#8a8270; font-size:15px; line-height:1; padding:0 2px;"
        )
        x.on(
            "click", lambda _=None: (get_store().set(STORE_KEY_RITUAL_FEEDBACK, None), _ritual_feedback_panel.refresh())
        )
        with x:
            ui.html("&times;")


# ── HEADER ─────────────────────────────────────────────────────────────────────
def _header(
    section: str,
    callbacks: dict[str, Callable],
    identity: dict[str, str] | None = None,
    initial_query: str = "",
) -> None:
    identity = identity or {}
    name = identity.get("name") or "Dovahkiin"
    role = identity.get("role") or "Maestro de la Forja"
    titles = {
        "Dashboard": ("PANEL DEL DRACONATO", f"Tu salón, {name} — todo en su sitio."),
        "Mods": ("ARSENAL DE LA FORJA", "Cada mod, montando guardia."),
        "Conflicts": ("DISPUTAS EN LA FORJA", "Juzga cada conflicto."),
        "Downloads": ("PUERTA DE APROBACIÓN", "El guardián aguarda."),
        "Settings": ("CÁMARA DE AJUSTES", "Afina la forja."),
    }
    title, sub = titles.get(section, titles["Dashboard"])
    hdr = (
        "position:sticky; top:0; z-index:20; height:74px; flex-shrink:0; display:flex; align-items:center; gap:16px; padding:0 22px;"
        "background:linear-gradient(180deg, rgba(26,18,12,.96), rgba(11,14,20,.92)); border-bottom:1px solid rgba(62,39,35,.9);"
        "box-shadow:0 8px 26px -14px rgba(0,0,0,.9); backdrop-filter:blur(8px);"
    )
    with ui.element("header").style(hdr):
        ui.html(
            '<div style="position:absolute; left:0; right:0; bottom:-1px; height:1px; background:linear-gradient(90deg,transparent,rgba(200,168,106,.45),transparent);"></div>'
            f'<div style="min-width:0; flex-shrink:1;"><div style="font-family:\'Cinzel\',serif; font-weight:700; font-size:16px; letter-spacing:.1em; color:#f1e6cf; line-height:1.15; white-space:nowrap;">{_e(title)}</div>'
            f"<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12px; color:#897f6a; margin-top:2px;\">{_e(sub)}</div></div>"
        )
        # Buscador real (A1): al presionar Enter dispara ``on_search`` — el
        # cableado en sky_claw_gui guarda el término y navega a "Mods", donde
        # ``build_mod_list`` lo consume para pre-filtrar (reusa ``_filter_mods``).
        with ui.element("div").style(
            "flex:1 1 160px; min-width:140px; max-width:400px; margin:0 16px; position:relative;"
        ):
            ui.html(
                '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#7a7159" stroke-width="2" stroke-linecap="round" style="position:absolute; left:14px; top:50%; transform:translateY(-50%); z-index:1; pointer-events:none;"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>'
            )
            search = (
                ui.input(placeholder="Busca en los archivos arcanos…", value=initial_query)
                .props('borderless dense dark input-style="color:#e8e2d4; font-family:EB Garamond,serif"')
                .style(
                    "width:100%; padding:0 14px 0 36px; background:rgba(62,39,35,.45);"
                    " border:1px solid rgba(200,168,106,.28); border-radius:5px;"
                    " box-shadow:inset 0 2px 6px rgba(0,0,0,.45);"
                )
            )
            on_search = _cb(callbacks, "on_search")
            if on_search:
                search.on("keydown.enter", lambda _=None: on_search((search.value or "").strip()))
        # right cluster (telemetry HUD + settings + user). The HUD is split into
        # its own @ui.refreshable so the live timer can pulse GPU/CPU without
        # re-rendering the settings button + user avatar.
        with ui.element("div").style("display:flex; align-items:center; gap:14px;"):
            _hud_panel()
            _modo_local_panel()
            # Botón de Ajustes (A2): antes era un <button> decorativo sin handler.
            # Ahora navega a la sección "Settings" vía el callback de navegación.
            gear = (
                ui.element("button")
                .props('title="Ajustes"')
                .style(
                    "width:40px; height:40px; display:flex; align-items:center; justify-content:center;"
                    " background:rgba(62,39,35,.4); border:1px solid rgba(200,168,106,.22); border-radius:5px; cursor:pointer;"
                )
            )
            on_navigate = _cb(callbacks, "on_navigate")
            if on_navigate:
                gear.on("click", lambda _=None: on_navigate("Settings"))
            with gear:
                ui.html(
                    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#c2b48f" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg>'
                )
            # Identidad (A3): data-driven desde el estado (nombre/rol + iniciales
            # calculadas), en vez de los literales "Dovahkiin"/"DS" hardcodeados.
            ui.html(_identity_html(name, role))


# ── HERO ───────────────────────────────────────────────────────────────────────
def _hero(active: int, conflicts: int, callbacks: dict[str, Callable]) -> None:
    integrity = max(60, 100 - conflicts * 5)
    estado = "ESTABLE" if conflicts == 0 else ("VIGILANTE" if conflicts < 5 else "EN DISPUTA")
    estado_color = "#7fc08c" if conflicts < 5 else RED_SOFT
    sec = (
        "position:relative; overflow:hidden; border-radius:5px; min-height:354px; display:flex; align-items:flex-end;"
        "padding:38px 40px; margin-bottom:26px; border:1px solid rgba(200,168,106,.3);"
        "box-shadow:0 26px 60px -24px rgba(0,0,0,.85), inset 0 0 90px rgba(0,0,0,.45);"
    )
    with ui.element("section").style(sec):
        ui.html(
            "<div style=\"position:absolute; inset:0; background:url('/assets/alduin_menace_bg.jpg') center 32%/cover; transform:scale(1.04);\"></div>"
            '<div style="position:absolute; inset:0; background:linear-gradient(92deg, rgba(8,10,14,.85) 0%, rgba(8,10,14,.8) 34%, rgba(8,10,14,.32) 62%, rgba(10,12,16,.6) 100%);"></div>'
            '<div style="position:absolute; inset:0; background:linear-gradient(0deg, rgba(7,9,12,.96) 2%, transparent 42%);"></div>'
            '<div style="position:absolute; left:-10%; right:-10%; top:-46%; height:80%; pointer-events:none; background:radial-gradient(60% 100% at 50% 100%, rgba(70,180,150,.3), transparent 70%); filter:blur(26px); animation:scAurora 13s ease-in-out infinite;"></div>'
            + "".join(
                f'<span style="position:absolute; left:{lx}%; bottom:{by}px; width:3px; height:3px; border-radius:50%; background:#ffb347; box-shadow:0 0 7px #ff9d00; animation:scEmber {du}s linear {dly}s infinite;"></span>'
                for lx, by, du, dly in [
                    (64, 30, 6.5, 0.2),
                    (72, 20, 7.8, 1.4),
                    (80, 40, 5.6, 2.6),
                    (58, 18, 8.4, 3.3),
                    (88, 26, 7.0, 4.1),
                ]
            )
            + "".join(
                f'<span style="position:absolute; {pos} width:22px; height:22px; {brd} opacity:.7;"></span>'
                for pos, brd in [
                    ("left:14px; top:14px;", "border-top:1.5px solid #c8a86a; border-left:1.5px solid #c8a86a;"),
                    ("right:14px; top:14px;", "border-top:1.5px solid #c8a86a; border-right:1.5px solid #c8a86a;"),
                    ("left:14px; bottom:14px;", "border-bottom:1.5px solid #c8a86a; border-left:1.5px solid #c8a86a;"),
                    (
                        "right:14px; bottom:14px;",
                        "border-bottom:1.5px solid #c8a86a; border-right:1.5px solid #c8a86a;",
                    ),
                ]
            )
        )
        with ui.element("div").style("position:relative; z-index:2; max-width:680px;"):
            ui.html(
                '<div style="display:flex; align-items:center; gap:12px; margin-bottom:14px;">'
                '<span style="height:1px; width:34px; background:linear-gradient(90deg,transparent,#c8a86a);"></span>'
                '<span style="font-family:\'Noto Sans Runic\',serif; font-size:14px; letter-spacing:.42em; color:#ecd9a8; opacity:.85; text-shadow:0 0 12px rgba(200,168,106,.45);" aria-hidden="true">ᛞᚱᚪᚷᚩᚾᛒᚩᚱᚾ</span></div>'
                '<div style="filter:drop-shadow(0 3px 14px rgba(0,0,0,.7));"><h1 style="margin:0; font-family:\'Cinzel\',serif; font-weight:900; font-size:52px; line-height:1.0; letter-spacing:.03em; background:linear-gradient(178deg,#f7ecce 8%,#e3c98f 48%,#a47f48 100%); -webkit-background-clip:text; background-clip:text; color:transparent;">SALVE,<br>DOVAHKIIN</h1></div>'
                f'<p style="margin:16px 0 26px; max-width:520px; font-family:\'EB Garamond\',serif; font-size:17px; line-height:1.55; color:#d8cfba;">Tu forja está despierta. <span style="color:#ecd9a8; font-weight:600;">{_e(active)} mods</span> montan guardia sobre Tamriel y el orden de carga aguarda tu palabra.</p>'
            )
            with ui.element("div").style("display:flex; flex-wrap:wrap; align-items:center; gap:18px;"):
                ui.html(
                    '<div style="flex:1; min-width:240px; padding:13px 16px; background:rgba(8,11,15,.6); border:1px solid rgba(200,168,106,.28); border-radius:4px; backdrop-filter:blur(4px);">'
                    '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">'
                    "<span style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.16em; color:#b6ab90;\">ESTADO DE LA FORJA</span>"
                    f"<span style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.1em; color:{estado_color};\">◆ {_e(estado)}</span></div>"
                    '<div style="height:7px; border-radius:4px; background:rgba(255,255,255,.07); overflow:hidden; box-shadow:inset 0 1px 2px rgba(0,0,0,.6);">'
                    f'<div style="height:100%; width:{integrity}%; border-radius:4px; background:linear-gradient(90deg,#8a6c38,#ecd9a8); box-shadow:0 0 10px rgba(200,168,106,.45); background-size:200% 100%; animation:scShimmer 3.5s linear infinite;"></div></div>'
                    f"<div style=\"display:flex; justify-content:space-between; margin-top:7px; font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#8a8270;\"><span>Integridad {integrity}%</span><span>{_e(conflicts)} conflictos</span></div></div>"
                )
                prepare = _cb(callbacks, "on_cta_primary")
                btn = ui.element("button").style(
                    "display:flex; align-items:center; gap:11px; padding:15px 26px; cursor:pointer; font-family:'Cinzel',serif;"
                    "font-weight:700; font-size:15px; letter-spacing:.12em; color:#1c130a;"
                    "background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40); border:1.5px solid #f6e6bd; border-radius:4px;"
                    "box-shadow:0 0 24px rgba(200,168,106,.45), inset 0 1px 0 rgba(255,255,255,.5); transition:transform .2s, box-shadow .2s;"
                )
                if prepare:
                    btn.on("click", lambda _=None: prepare())
                with btn:
                    ui.html(
                        '<span style="font-family:\'Noto Sans Runic\',serif; font-size:18px;" aria-hidden="true">ᚦ</span> PREPARAR JUEGO'
                    )


# ── STAT PLAQUES ───────────────────────────────────────────────────────────────
def _stats(active: int, pending: int, conflicts: int, storage: float) -> None:
    # No fabricated trend badges ("↑ 5%", "nuevo", "↓ 2"): there's no historical
    # series to compute deltas from, so the plaques show only the real current
    # value plus a static descriptor.
    defs = [
        (_STAT_RUNES["mods"], ACCENT, "MODS ACTIVOS", f"{active}", "", "guardando Tamriel"),
        (_STAT_RUNES["pending"], ACCENT, "PENDIENTES", f"{pending}", "", "esperan renovación"),
        (_STAT_RUNES["conflicts"], RED, "CONFLICTOS", f"{conflicts}", "", "requieren tu juicio"),
        (_STAT_RUNES["space"], FROST, "ESPACIO", f"{storage:.1f}", "GB", "de 200 GB en disco"),
    ]
    cards = ""
    for rune, tone, label, value, unit, sub in defs:
        cards += (
            '<div style="position:relative; overflow:hidden; padding:20px; border-radius:4px;'
            " background:linear-gradient(162deg, rgba(26,32,40,.82), rgba(11,14,19,.9)); border:1px solid rgba(200,168,106,.2);"
            ' box-shadow:0 16px 34px -18px rgba(0,0,0,.8), inset 0 1px 0 rgba(255,255,255,.04);">'
            f'<div style="position:absolute; right:-14px; top:-18px; font-family:\'Noto Sans Runic\',serif; font-size:78px; color:{tone}; opacity:.08;" aria-hidden="true">{rune}</div>'
            '<div style="display:flex; align-items:center; gap:11px; margin-bottom:16px;">'
            f'<div style="width:42px; height:42px; flex-shrink:0; display:flex; align-items:center; justify-content:center; border-radius:7px; background:linear-gradient(140deg,#3a2c1c,#221913); border:1px solid {tone}; box-shadow:0 0 12px {tone}55;">'
            f'<span style="font-family:\'Noto Sans Runic\',serif; font-size:20px; color:{tone}; text-shadow:0 0 8px {tone}88;" aria-hidden="true">{rune}</span></div>'
            f"<span style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.05em; line-height:1.2; color:#a39a85;\">{_e(label)}</span></div>"
            '<div style="display:flex; align-items:flex-end; gap:6px;">'
            f"<span style=\"font-family:'Spline Sans Mono',monospace; font-weight:600; font-size:38px; line-height:1; color:#f1ead8;\">{_e(value)}</span>"
            f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:13px; color:#8a8270; margin-bottom:5px;\">{_e(unit)}</span></div>"
            '<div style="margin-top:11px;">'
            f"<span style=\"font-family:'EB Garamond',serif; font-size:12.5px; color:#857c69;\">{_e(sub)}</span></div></div>"
        )
    ui.html(
        f'<div style="display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin-bottom:30px;">{cards}</div>'
    )


# ── RITUALES DE LA FORJA ───────────────────────────────────────────────────────
def _rituales(callbacks: dict[str, Callable]) -> None:
    ui.html(
        '<div style="display:flex; align-items:center; gap:16px; margin-bottom:6px;">'
        "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:17px; letter-spacing:.2em; color:#e7d6ad;\">RITUALES DE LA FORJA</h2>"
        '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span></div>'
        "<p style=\"margin:0 0 18px; font-family:'EB Garamond',serif; font-style:italic; font-size:14px; color:#8a8068;\">El Motor Invisible — cinco herramientas legendarias, un solo gesto.</p>"
    )
    snapshot = get_store().get(STORE_KEY_ENV)
    on_ritual_run = _cb(callbacks, "on_ritual_run")
    on_ritual_install = _cb(callbacks, "on_ritual_install")
    with ui.element("div").style(
        "display:grid; grid-template-columns:repeat(auto-fit,minmax(186px,1fr)); gap:14px; margin-bottom:30px;"
    ):
        for r in _RITUALS:
            _ritual_card(r, _ritual_status(snapshot, r["tool"]), on_ritual_run, on_ritual_install)


# Per-state chrome for a ritual card. "unknown" = scan hasn't landed yet, so we
# stay honest (neutral, no Ejecutar/Instalar claim) until the snapshot arrives.
_RITUAL_STATE_STYLE: dict[str, dict[str, str]] = {
    "available": {
        "opacity": "1",
        "dot": GREEN,
        "label": "Disponible",
        "color": "#9bbf8e",
        "btn_label": "Ejecutar",
        "btn_style": "color:#d8c69a; border-color:rgba(156,122,64,.5);",
    },
    "missing": {
        "opacity": "0.62",
        "dot": "#9c7a40",
        "label": "No instalado",
        "color": "#b8946a",
        "btn_label": "Instalar",
        "btn_style": "color:#ffb05a; border-color:rgba(200,100,20,.5);",
    },
    "unknown": {
        "opacity": "0.78",
        "dot": "#857c69",
        "label": "Verificando…",
        "color": "#a39a85",
        "btn_label": "Ejecutar",
        "btn_style": "color:#a39a85; border-color:rgba(120,120,120,.4);",
    },
}


def _ritual_card(
    r: dict[str, str],
    state: str = "unknown",
    on_ritual_run: Callable | None = None,
    on_ritual_install: Callable | None = None,
) -> None:
    tone = r["tone"]
    style = _RITUAL_STATE_STYLE.get(state, _RITUAL_STATE_STYLE["unknown"])
    opacity = style["opacity"]
    status_dot = style["dot"]
    status_label = style["label"]
    status_color = style["color"]
    btn_label = style["btn_label"]
    btn_style = style["btn_style"]
    card = (
        f"position:relative; display:flex; flex-direction:column; gap:9px; padding:18px 16px; border-radius:4px; opacity:{opacity};"
        "background:linear-gradient(168deg, rgba(30,22,14,.9), rgba(14,10,7,.92)); border:1px solid rgba(200,168,106,.22);"
        "box-shadow:0 14px 30px -16px rgba(0,0,0,.8), inset 0 1px 0 rgba(255,255,255,.04); transition:transform .25s, border-color .25s;"
    )
    with ui.element("div").style(card):
        ui.html(
            f'<div style="display:flex; align-items:center; justify-content:center; width:48px; height:48px; border-radius:50%; background:radial-gradient(circle at 50% 38%, #2c1f13, #0d0805); border:1.5px solid {tone}; box-shadow:inset 0 0 10px rgba(0,0,0,.7), 0 0 12px {tone}55;">'
            f'<span style="font-family:\'Noto Sans Runic\',serif; font-size:23px; color:{tone}; text-shadow:0 0 9px {tone}88;" aria-hidden="true">{r["rune"]}</span></div>'
            f"<div style=\"font-family:'Cinzel',serif; font-weight:600; font-size:14.5px; letter-spacing:.03em; color:#ecdfc2; line-height:1.2;\">{_e(r['label'])}</div>"
            f"<div style=\"flex:1; font-family:'EB Garamond',serif; font-size:13px; line-height:1.42; color:#9b927e;\">{_e(r['desc'])}</div>"
            f'<div style="display:flex; align-items:center; gap:7px; padding-top:2px;"><span style="width:7px; height:7px; border-radius:50%; background:{status_dot}; box-shadow:0 0 6px {status_dot};"></span>'
            f"<span style=\"flex:1; font-family:'EB Garamond',serif; font-size:12px; color:{status_color};\">{status_label}</span></div>"
        )
        b = ui.element("button").style(
            f"margin-top:2px; padding:8px 10px; cursor:pointer; font-family:'Cinzel',serif; font-size:11.5px; font-weight:600;"
            f"letter-spacing:.1em; background:rgba(0,0,0,.3); border:1px solid; border-radius:4px; {btn_style}"
        )
        # Fase 2: an available tool runs for real through the supervisor dispatcher
        # (HITL-gated). Follow-up C: a "missing" tool with an auto-installer downloads
        # it through ToolsInstaller (download approval parks the GUI modal). Tools with
        # no installer (Wrye Bash / DynDOLOD) keep the honest interim notice.
        if state == "available" and on_ritual_run is not None:
            b.on("click", lambda _=None, tool=r["tool"]: on_ritual_run(tool))
        elif state == "missing" and on_ritual_install is not None and r["tool"] in RITUAL_INSTALLER_MAP:
            b.on("click", lambda _=None, tool=r["tool"]: on_ritual_install(tool))
        else:
            b.on(
                "click",
                lambda _=None, lbl=r["label"], tech=r["tech"]: ui.notify(
                    f"{lbl} ({tech}): disponible en la próxima iteración.", type="info"
                ),
            )
        with b:
            ui.html(btn_label)
        ui.html(
            f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; color:#6e6655; text-align:right;\">{_e(r['tech'])}</div>"
        )


# ── ORDEN DE CARGA ─────────────────────────────────────────────────────────────
def _orden_carga(mods: list[dict[str, Any]], callbacks: dict[str, Callable]) -> None:
    sec = (
        "position:relative; border-radius:5px; background:linear-gradient(180deg, rgba(30,22,16,.5), rgba(11,14,19,.86));"
        "border:1px solid rgba(62,39,35,.95); box-shadow:0 20px 44px -22px rgba(0,0,0,.85), inset 0 0 40px rgba(0,0,0,.4); overflow:hidden;"
    )
    with ui.element("section").style(sec):
        head = (
            "display:flex; align-items:center; gap:12px; padding:17px 20px; border-bottom:1px solid rgba(200,168,106,.16);"
            "background:linear-gradient(180deg, rgba(62,39,35,.34), transparent);"
        )
        with ui.element("div").style(head):
            ui.html(
                "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:15px; letter-spacing:.14em; color:#ecdfc2;\">ORDEN DE CARGA</h2>"
                f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:#c8a86a; padding:2px 9px; border:1px solid #c8a86a; border-radius:99px; background:rgba(200,168,106,.15); box-shadow:0 0 10px rgba(200,168,106,.45);\">{_e(len(mods))}</span>"
                '<span style="flex:1;"></span>'
            )
            view_all = _cb(callbacks, "on_view_all_mods")
            vb = ui.element("button").style(
                "font-family:'Cinzel',serif; font-size:12px; letter-spacing:.08em; color:#ecd9a8; background:none; border:none; cursor:pointer; text-decoration:underline; text-underline-offset:3px;"
            )
            with vb:
                ui.html("Ver Todo")
            if view_all:
                vb.on("click", lambda _=None: view_all())
        with (
            ui.element("div")
            .classes("sc-scroll")
            .style("max-height:392px; overflow-y:auto; overflow-x:hidden; padding:6px 10px;")
        ):
            on_mod = _cb(callbacks, "on_mod_click")
            for idx, m in enumerate(mods[:30], 1):
                _mod_row(idx, m, on_mod)


def _mod_row(idx: int, m: dict[str, Any], on_mod: Callable | None) -> None:
    status = _derive_status(m)
    name = str(m.get("name", "Mod desconocido"))
    size_mb = float(m.get("size_mb", 0) or 0)
    size = f"{size_mb / 1024:.1f} GB" if size_mb > 1024 else f"{size_mb:.0f} MB"
    ver = str(m.get("version", "") or "—")
    dot, status_label, name_color = {
        "active": (GREEN, "Activo", "#d9cfb6"),
        "update": ("#e0b341", "Update", "#e7d6ad"),
        "conflict": (RED, "Conflicto", "#e7d6ad"),
        "inactive": ("#6e6655", "Inactivo", "#8a8270"),
    }.get(status, (GREEN, "Activo", "#d9cfb6"))
    is_conflict = status == "conflict"
    conflict_badge = (
        "<span style=\"font-family:'Cinzel',serif; font-size:9.5px; letter-spacing:.08em; color:#e88a82; padding:1px 7px; border:1px solid rgba(197,82,74,.6); border-radius:99px; background:rgba(197,82,74,.14); flex-shrink:0;\">CONFLICTO</span>"
        if is_conflict
        else ""
    )
    row = ui.element("div").style(
        "display:flex; align-items:center; gap:13px; padding:11px 12px; border-radius:4px; border-bottom:1px solid rgba(200,168,106,.07); transition:background .2s; cursor:pointer;"
    )
    if on_mod:
        row.on("click", lambda _=None, n=name: on_mod(n))
    with row:
        ui.html(
            f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:#6e6655; width:18px; flex-shrink:0;\">{idx}</span>"
            f'<span style="width:9px; height:9px; flex-shrink:0; border-radius:50%; background:{dot}; box-shadow:0 0 7px {dot};"></span>'
            '<div style="flex:1; min-width:0;"><div style="display:flex; align-items:center; gap:9px;">'
            f"<span style=\"font-family:'Cinzel',serif; font-size:14px; color:{name_color}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;\">{_e(name)}</span>{conflict_badge}</div>"
            f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#7d7563; margin-top:2px;\">{_e(ver)} · {_e(size)}</div></div>"
            f"<span style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12px; color:{dot}; width:74px; text-align:right; flex-shrink:0;\">{status_label}</span>"
        )


# ── ASISTENTE ARCANO ───────────────────────────────────────────────────────────
def _asistente(chat_messages: list[dict[str, Any]], is_thinking: bool, callbacks: dict[str, Callable]) -> None:
    sec = (
        "position:relative; border-radius:5px; overflow:hidden; display:flex; flex-direction:column;"
        "background:#d8bf98 url('/assets/parchment.png') center/cover; border:2px solid #5c4a2a;"
        "box-shadow:0 20px 44px -20px rgba(0,0,0,.85), inset 0 0 60px rgba(70,48,20,.35);"
    )
    with ui.element("section").style(sec):
        ui.html(
            '<div style="position:absolute; inset:5px; border:1px solid rgba(92,74,42,.35); pointer-events:none; border-radius:3px;"></div>'
            '<div style="position:relative; display:flex; align-items:center; gap:12px; padding:15px 18px; border-bottom:1px solid rgba(92,74,42,.45); background:linear-gradient(180deg, rgba(92,74,42,.22), transparent);">'
            '<div style="width:40px; height:40px; flex-shrink:0; border-radius:50%; display:flex; align-items:center; justify-content:center; background:radial-gradient(circle at 50% 35%, #3a2c1a, #16100a); border:1.5px solid #c8a86a; box-shadow:0 0 12px rgba(200,168,106,.45);">'
            '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#ecd9a8" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 0 1 10 10c0 5.5-4.5 10-10 10S2 17.5 2 12"/><path d="M12 7v5"/><path d="M12 16h.01"/></svg></div>'
            '<div style="flex:1;"><div style="font-family:\'Cinzel\',serif; font-weight:700; font-size:15px; letter-spacing:.06em; color:#2c2016;">Asistente Arcano</div>'
            "<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12px; color:#6b5536;\">Forjado con DeepSeek</div></div>"
            '<span style="font-family:\'Noto Sans Runic\',serif; font-size:15px; color:#5c4a2a; opacity:.85;" aria-hidden="true">ᚺᚢᛗ</span></div>'
        )
        # Messages
        with (
            ui.element("div")
            .classes("sc-scroll")
            .style(
                "position:relative; flex:1; min-height:212px; max-height:300px; overflow-y:auto; padding:16px 18px; display:flex; flex-direction:column; gap:13px;"
            )
        ):
            if not chat_messages:
                _chat_bubble("¡Salve, Dovahkiin! Soy tu Asistente Arcano. ¿Qué deseas forjar hoy?", is_user=False)
            for c in chat_messages:
                _chat_bubble(str(c.get("content", "")), is_user=bool(c.get("is_user", False)))
            if is_thinking:
                ui.html(
                    "<div style=\"display:flex; align-items:center; gap:8px; color:#6b5536; font-family:'EB Garamond',serif; font-style:italic; font-size:13px;\">"
                    '<span style="display:inline-flex; gap:3px;">'
                    '<span style="width:5px; height:5px; border-radius:50%; background:#6b5536; animation:scBlink 1.2s infinite;"></span>'
                    '<span style="width:5px; height:5px; border-radius:50%; background:#6b5536; animation:scBlink 1.2s .2s infinite;"></span>'
                    '<span style="width:5px; height:5px; border-radius:50%; background:#6b5536; animation:scBlink 1.2s .4s infinite;"></span></span>'
                    "Consultando los pergaminos…</div>"
                )
        # Input row
        with ui.element("div").style(
            "position:relative; display:flex; gap:9px; padding:13px 16px; border-top:1px solid rgba(92,74,42,.45);"
        ):
            on_send = _cb(callbacks, "on_send_message")
            chat_input = (
                ui.input(placeholder="Habla, y escucharé…")
                .props("dense borderless")
                .style(
                    "flex:1; padding:4px 13px; font-family:'EB Garamond',serif; font-size:14px; color:#2c2016;"
                    "background:rgba(255,255,255,.32); border:1px solid rgba(92,74,42,.5); border-radius:5px;"
                )
            )

            def _send(_=None) -> None:
                # Preserve the optimistic-clear + rollback contract from
                # create_chat_preview: on a daemon/network failure the text is
                # restored and the user is notified, not silently erased (Codex #208).
                from .sections.chat_preview import _try_send_with_rollback

                original = chat_input.value or ""
                msg = original.strip()
                if not msg or not on_send:
                    return
                chat_input.value = ""
                _try_send_with_rollback(
                    msg,
                    on_send=on_send,
                    restore_fn=lambda text: setattr(chat_input, "value", text),
                    notify_fn=lambda text: ui.notify(text, type="negative"),
                    original_text=original,
                )

            chat_input.on("keydown.enter", _send)
            sb = ui.element("button").style(
                "width:44px; flex-shrink:0; display:flex; align-items:center; justify-content:center; cursor:pointer;"
                "background:linear-gradient(180deg,#5c4a2a,#3e321d); border:1px solid #2a2012; border-radius:5px; box-shadow:inset 0 1px 0 rgba(255,255,255,.12);"
            )
            sb.on("click", _send)
            with sb:
                ui.html(
                    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#f0e4cc" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>'
                )


def _chat_bubble(text: str, is_user: bool) -> None:
    if is_user:
        wrap = "display:flex; justify-content:flex-end;"
        who, who_color, who_align = "TÚ", "#6b5536", "text-align:right;"
        bubble = "background:rgba(92,74,42,.92); color:#f3ead4; border-radius:9px 9px 2px 9px;"
    else:
        wrap = "display:flex; justify-content:flex-start;"
        who, who_color, who_align = "ASISTENTE", "#7a5f30", ""
        bubble = "background:rgba(255,250,240,.5); color:#2c2016; border:1px solid rgba(92,74,42,.3); border-radius:9px 9px 9px 2px;"
    ui.html(
        f'<div style="{wrap}"><div style="max-width:82%;">'
        f"<div style=\"font-family:'Cinzel',serif; font-size:9.5px; letter-spacing:.12em; color:{who_color}; margin-bottom:4px; {who_align}\">{who}</div>"
        f"<div style=\"padding:9px 13px; font-family:'EB Garamond',serif; font-size:14px; line-height:1.45; {bubble}\">{_e(text)}</div></div></div>"
    )


def _footer_rune() -> None:
    ui.html(
        '<div style="display:flex; align-items:center; justify-content:center; gap:18px; margin-top:34px; opacity:.85;">'
        '<span style="height:1px; width:90px; background:linear-gradient(90deg,transparent,rgba(200,168,106,.4));"></span>'
        '<span style="font-family:\'Noto Sans Runic\',serif; font-size:13px; letter-spacing:.45em; color:#c8a86a; text-shadow:0 0 10px rgba(200,168,106,.45);" aria-hidden="true">ᚦᚢ&nbsp;&nbsp;ᚠᚢᛋ&nbsp;&nbsp;ᚱᚩ&nbsp;&nbsp;ᛞᚪ</span>'
        '<span style="height:1px; width:90px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span></div>'
        "<div style=\"text-align:center; margin-top:10px; font-family:'EB Garamond',serif; font-style:italic; font-size:12.5px; color:#5f5849;\">«Que tu orden de carga sea estable y tu juego, eterno.»</div>"
    )


def _mods_screen(mods: list[dict[str, Any]], callbacks: dict[str, Callable], search_query: str = "") -> None:
    """Full mod-management screen (search + toggles).

    Reuses ``build_mod_list`` so the existing ``on_mod_toggle`` controls stay
    reachable from the Forge shell instead of the "próxima iteración" placeholder
    (Codex P1 on #208). ``search_query`` pre-filtra la lista cuando el usuario
    llega desde el buscador del header (A1).
    """
    from .mod_list import build_mod_list

    ui.html(
        '<div style="display:flex; align-items:center; gap:16px; margin-bottom:14px;">'
        "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:17px; letter-spacing:.2em; color:#e7d6ad;\">ARSENAL DE LA FORJA</h2>"
        '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span></div>'
    )
    adapted = [
        {
            "name": m.get("name", "Mod desconocido"),
            "enabled": _derive_status(m) != "inactive",
            "version": str(m.get("version", "") or ""),
        }
        for m in mods
    ]
    build_mod_list(mods=adapted, on_toggle=callbacks.get("on_mod_toggle"), initial_query=search_query)


def _conflicts_screen(
    conflicts: list[dict[str, Any]],
    callbacks: dict[str, Callable],
    resolved: list[dict[str, Any]] | None = None,
) -> None:
    """Pantalla de Conflictos: lista real de disputas + detección + resolver.

    Reemplaza el placeholder "próxima iteración" por los conflictos sin resolver
    (``conflicts_list`` del store, ya enriquecido con nombres de mods vía
    ``enrich_conflicts``). "Detectar disputas" (F5) corre el escaneo de assets
    del VFS y persiste los pares nuevos; "Resolver" abre un modal para anotar
    cómo se zanjó y dispara ``on_conflict_resolve(id, resolution)`` (F3). Las
    disputas ya resueltas se listan abajo con su nota (``resolved``).
    """
    resolved = resolved or []
    with ui.element("div").style("display:flex; align-items:center; gap:16px; margin-bottom:14px;"):
        ui.html(
            "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:17px; letter-spacing:.2em; color:#e7d6ad;\">DISPUTAS EN LA FORJA</h2>"
            f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:12px; color:{RED_SOFT};\">{len(conflicts)}</span>"
            '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span>'
        )
        on_scan = _cb(callbacks, "on_conflict_scan")
        if on_scan is not None:
            scan_btn = ui.element("button").style(
                "flex-shrink:0; padding:9px 18px; cursor:pointer; font-family:'Cinzel',serif; font-size:12px;"
                " letter-spacing:.06em; color:#1c130a; background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40);"
                " border:1.5px solid #f6e6bd; border-radius:4px;"
            )
            with scan_btn:
                ui.html("Detectar disputas")
            scan_btn.on("click", lambda _=None: on_scan())
        # F6: análisis profundo de records vía xEdit (lento, requiere SSEEdit).
        # Estilo secundario para distinguirlo del escaneo liviano de arriba.
        on_deep = _cb(callbacks, "on_deep_conflict_scan")
        if on_deep is not None:
            deep_btn = ui.element("button").style(
                "flex-shrink:0; padding:9px 18px; cursor:pointer; font-family:'Cinzel',serif; font-size:12px;"
                " letter-spacing:.06em; color:#e7d6ad; background:rgba(62,39,35,.5);"
                " border:1.5px solid rgba(200,168,106,.5); border-radius:4px;"
            )
            with deep_btn:
                ui.html("Análisis profundo (xEdit)")
            deep_btn.on("click", lambda _=None: on_deep())
    if not conflicts:
        with ui.element("div").style(
            "display:flex; flex-direction:column; align-items:center; justify-content:center; padding:70px 0; gap:12px;"
        ):
            ui.html(
                f"<div style=\"font-family:'Noto Sans Runic',serif; font-size:48px; color:{GREEN}; opacity:.55;\">ᚦ</div>"
                "<div style=\"font-family:'EB Garamond',serif; font-style:italic; color:#8a8068;\">No hay disputas — tu orden de carga está en paz.</div>"
            )
    else:
        on_resolve = _cb(callbacks, "on_conflict_resolve")
        with ui.element("div").style("display:flex; flex-direction:column; gap:10px;"):
            for c in conflicts:
                _conflict_row(c, on_resolve)

    # Las resueltas se muestran aunque no queden disputas activas (historial).
    _resolved_section(resolved)


def _resolved_section(resolved: list[dict[str, Any]]) -> None:
    """Historial de disputas ya resueltas, con la nota de cómo se zanjaron (F3)."""
    if not resolved:
        return
    with ui.element("div").style("display:flex; align-items:center; gap:16px; margin:26px 0 12px;"):
        ui.html(
            "<h3 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:14px; letter-spacing:.18em; color:#8fae86;\">RESUELTAS</h3>"
            f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:{GREEN};\">{len(resolved)}</span>"
            '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(95,156,107,.35),transparent);"></span>'
        )
    with ui.element("div").style("display:flex; flex-direction:column; gap:7px;"):
        for c in resolved:
            ui.html(_resolved_row_html(c))


def _resolved_row_html(c: dict[str, Any]) -> str:
    """Seam puro: fila de una disputa resuelta (mods + tipo + nota) como HTML."""
    nota = c.get("resolution")
    nota_html = _e(nota) if nota else '<span style="font-style:italic; color:#6f7a67;">Sin nota</span>'
    line = (
        "display:flex; align-items:center; gap:14px; padding:11px 16px; border-radius:4px;"
        "background:rgba(38,50,40,.32); border:1px solid rgba(95,156,107,.24);"
    )
    return (
        f'<div style="{line}">'
        f'<span style="width:8px; height:8px; border-radius:50%; background:{GREEN}; flex-shrink:0;"></span>'
        '<div style="flex:1; min-width:0;">'
        f"<div style=\"font-family:'Cinzel',serif; font-size:13px; color:#d9d2c0;\">{_e(c.get('mod_a', '?'))}"
        f' <span style="color:#8fae86;">✓</span> {_e(c.get("mod_b", "?"))}'
        f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#7d8a75; margin-left:8px;\">{_e(c.get('type', ''))}</span></div>"
        f"<div style=\"font-family:'EB Garamond',serif; font-size:12.5px; color:#a7b0a0; margin-top:2px;\">{nota_html}</div>"
        "</div></div>"
    )


def _conflict_row(c: dict[str, Any], on_resolve: Callable | None) -> None:
    subtitle = str(c.get("type") or "Conflicto")
    if c.get("detected_at"):
        subtitle += f" · {c.get('detected_at')}"
    row = (
        "display:flex; align-items:center; gap:16px; padding:14px 18px; border-radius:5px;"
        "background:rgba(62,39,35,.35); border:1px solid rgba(216,88,78,.35); box-shadow:inset 0 1px 0 rgba(255,255,255,.03);"
    )
    with ui.element("div").style(row):
        ui.html(
            f'<span style="width:9px; height:9px; border-radius:50%; background:{RED}; box-shadow:0 0 9px {RED}; flex-shrink:0;"></span>'
            '<div style="flex:1; min-width:0;">'
            f"<div style=\"font-family:'Cinzel',serif; font-size:14px; color:#f1e6cf;\">{_e(c.get('mod_a', '?'))}"
            f' <span style="color:{RED_SOFT};">⚔</span> '
            f"{_e(c.get('mod_b', '?'))}</div>"
            f"<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12px; color:#8a7f6a; margin-top:2px;\">{_e(subtitle)}</div>"
            "</div>"
        )
        if on_resolve is not None:
            btn = ui.element("button").style(
                "flex-shrink:0; padding:8px 16px; cursor:pointer; font-family:'Cinzel',serif; font-size:12px; letter-spacing:.06em; color:#1c130a;"
                "background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40); border:1.5px solid #f6e6bd; border-radius:4px;"
            )
            with btn:
                ui.html("Resolver")
            btn.on("click", lambda _=None, cc=c: _open_resolve_dialog(cc, on_resolve))


def _open_resolve_dialog(c: dict[str, Any], on_resolve: Callable) -> None:
    """Modal para anotar CÓMO se resolvió la disputa antes de confirmar (F3).

    La nota es opcional (Enter/Confirmar con el campo vacío resuelve sin nota),
    y se persiste vía ``on_conflict_resolve(id, resolution)``.
    """
    cid = c.get("id")
    card = (
        "min-width:420px; max-width:92vw; padding:22px 24px; border-radius:6px; color:#e8e2d4;"
        "background:linear-gradient(168deg, rgba(30,22,14,.98), rgba(14,10,7,.99)); border:1px solid rgba(200,168,106,.4);"
    )
    with ui.dialog() as dialog, ui.element("div").style(card):
        ui.html(
            "<div style=\"font-family:'Cinzel',serif; font-weight:700; font-size:15px; letter-spacing:.1em; color:#f1e6cf; margin-bottom:6px;\">RESOLVER DISPUTA</div>"
            f"<div style=\"font-family:'EB Garamond',serif; font-size:13px; color:#c9c0aa; margin-bottom:14px;\">{_e(c.get('mod_a', '?'))} ⚔ {_e(c.get('mod_b', '?'))}</div>"
        )
        note = (
            ui.textarea(
                placeholder="¿Cómo se resolvió? (opcional — p. ej. «parche en xEdit», «orden ajustado con LOOT»)"
            )
            .props("outlined autogrow dense")
            .style("width:100%; margin-bottom:16px;")
        )
        with ui.element("div").style("display:flex; gap:11px; justify-content:flex-end;"):
            cancel = ui.element("button").style(
                "padding:9px 18px; cursor:pointer; font-family:'Cinzel',serif; font-size:12px; letter-spacing:.08em;"
                "color:#c9c0aa; background:rgba(0,0,0,.3); border:1px solid rgba(200,168,106,.35); border-radius:4px;"
            )
            with cancel:
                ui.html("Cancelar")
            cancel.on("click", lambda _=None: dialog.close())
            ok = ui.element("button").style(
                "padding:9px 18px; cursor:pointer; font-family:'Cinzel',serif; font-weight:700; font-size:12px; letter-spacing:.08em;"
                "color:#1c130a; background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40); border:1.5px solid #f6e6bd; border-radius:4px;"
            )
            with ok:
                ui.html("Resolver")

            def _confirm(_: Any = None) -> None:
                on_resolve(cid, str(note.value or "").strip())
                dialog.close()

            ok.on("click", _confirm)
    dialog.open()


_SETTINGS_SECRET_FIELDS: list[tuple[str, str, str]] = [
    ("llm_api_key", "CLAVE API DEL PROVEEDOR", "Dejar vacío para no cambiar"),
    ("nexus_api_key", "NEXUS MODS API KEY", "Opcional — descargas automáticas"),
    ("search_api_key", "BRAVE SEARCH API KEY", "Opcional — búsqueda por descripción"),
    ("telegram_bot_token", "TELEGRAM BOT TOKEN", "Opcional — notificaciones HITL"),
]

_LBL = "font-family:'Cinzel',serif; font-size:11px; font-weight:600; letter-spacing:.18em; color:#b8a87e;"
_HINT = "font-family:'EB Garamond',serif; font-style:italic; font-size:11.5px; color:#8a7f6a;"


def _settings_screen(settings: dict[str, Any], callbacks: dict[str, Callable]) -> None:
    """Pantalla de Ajustes: identidad + proveedor IA + claves (persistidas).

    Reemplaza el placeholder "próxima iteración" al que navegaba el engranaje
    (A2). Reutiliza la semántica del wizard: secretos con "vacío = no cambiar"
    (badge Configurada/Sin clave desde keyring), provider/chat id/identidad al
    TOML vía ``on_settings_save`` → ``save_settings``.
    """
    identity = settings.get("identity") or {}
    key_status: dict[str, bool] = settings.get("key_status") or {}
    on_save = _cb(callbacks, "on_settings_save")
    inputs: dict[str, Any] = {}

    ui.html(
        '<div style="display:flex; align-items:center; gap:16px; margin-bottom:18px;">'
        "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:17px; letter-spacing:.2em; color:#e7d6ad;\">CÁMARA DE AJUSTES</h2>"
        '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span></div>'
    )

    panel = (
        "display:flex; flex-direction:column; gap:14px; padding:20px 22px; margin-bottom:18px; border-radius:5px;"
        "background:rgba(62,39,35,.3); border:1px solid rgba(200,168,106,.25);"
    )

    def _text_field(key: str, label: str, value: str = "", hint: str = "", password: bool = False) -> None:
        with ui.element("div").style("display:flex; flex-direction:column; gap:3px;"):
            badge = ""
            if password:
                configured = key_status.get(key, False)
                color, txt = ("#7fc08c", "Configurada") if configured else ("#a39a85", "Sin clave")
                badge = (
                    f" <span style=\"font-family:'Spline Sans Mono',monospace; font-size:10px;"
                    f' color:{color};">[{txt}]</span>'
                )
            ui.html(f'<span style="{_LBL}">{_e(label)}{badge}</span>')
            inp = (
                ui.input(value=value)
                .classes("w-full")
                .props("dense outlined dark" + (" type=password" if password else ""))
            )
            if hint:
                ui.html(f'<span style="{_HINT}">{_e(hint)}</span>')
            inputs[key] = inp

    # ── Identidad (cierra A3: el header pinta estos valores) ──
    with ui.element("section").style(panel):
        ui.html(f'<span style="{_LBL}">IDENTIDAD DEL DOVAHKIIN</span>')
        # Misma semántica que los secretos: save_settings ignora los vacíos,
        # así que el hint lo hace explícito (review Copilot en #221).
        _text_field(
            "user_display_name", "NOMBRE VISIBLE", str(identity.get("name") or ""), hint="Dejar vacío para no cambiar"
        )
        _text_field("user_role", "TÍTULO / ROL", str(identity.get("role") or ""), hint="Dejar vacío para no cambiar")

    # ── Proveedor IA + claves ──
    with ui.element("section").style(panel):
        ui.html(f'<span style="{_LBL}">PROVEEDOR DE IA</span>')
        provider_toggle = ui.toggle(
            ["anthropic", "deepseek", "openai", "ollama"],
            value=str(settings.get("provider") or "deepseek"),
        ).props("color=amber")
        for key, label, hint in _SETTINGS_SECRET_FIELDS:
            _text_field(key, label, hint=hint, password=True)
        # A diferencia de los secretos, el chat id se persiste tal cual (vaciar
        # el campo quita el destino de notificaciones).
        _text_field(
            "telegram_chat_id",
            "TELEGRAM CHAT ID",
            str(settings.get("telegram_chat_id") or ""),
            hint="Vaciar para quitar el destino de notificaciones",
        )

    # ── Guardar ──
    if on_save is not None:

        def _collect_and_save(_: Any = None) -> None:
            payload = {key: str(inp.value or "") for key, inp in inputs.items()}
            payload["llm_provider"] = str(provider_toggle.value or "")
            on_save(payload)

        btn = ui.element("button").style(
            "align-self:flex-start; padding:11px 24px; cursor:pointer; font-family:'Cinzel',serif;"
            " letter-spacing:.08em; color:#1c130a; background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40);"
            " border:1.5px solid #f6e6bd; border-radius:4px;"
        )
        with btn:
            ui.html("Guardar Ajustes")
        btn.on("click", _collect_and_save)


_STATUS_COLORS = {"ok": GREEN, "success": GREEN, "registered": GREEN, "failed": RED, "error": RED}


def _downloads_screen(downloads: dict[str, Any], callbacks: dict[str, Callable]) -> None:
    """Pantalla de Descargas: aprobación HITL pendiente + registro de actividad.

    Reemplaza el último placeholder "próxima iteración". La "Puerta de
    Aprobación" muestra inline la solicitud de descarga parkeada en
    ``STORE_KEY_PENDING_HITL`` (incluida la URL, que el modal global no
    muestra) con Aprobar/Denegar vía ``on_hitl_respond``; el "Registro de la
    Puerta" lista el ``task_log`` real del registry (instalaciones, updates,
    syncs y descargas fallidas).
    """
    pending = downloads.get("pending") or None
    history: list[dict[str, Any]] = downloads.get("history") or []
    on_respond = _cb(callbacks, "on_hitl_respond")

    # ── Puerta de aprobación ──
    ui.html(
        '<div style="display:flex; align-items:center; gap:16px; margin-bottom:14px;">'
        "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:17px; letter-spacing:.2em; color:#e7d6ad;\">PUERTA DE APROBACIÓN</h2>"
        '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span></div>'
    )
    if pending:
        card = (
            "display:flex; flex-direction:column; gap:10px; padding:18px 20px; margin-bottom:22px; border-radius:5px;"
            "background:rgba(62,39,35,.4); border:1px solid rgba(200,168,106,.45); box-shadow:0 0 18px rgba(200,168,106,.18);"
        )
        with ui.element("div").style(card):
            url = str(pending.get("url") or "")
            url_html = (
                f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:11.5px; color:#86b9d4;"
                f' word-break:break-all;">{_e(url)}</div>'
                if url
                else ""
            )
            ui.html(
                f"<div style=\"font-family:'Cinzel',serif; font-size:14px; color:#f1e6cf;\">{_e(pending.get('reason', 'Descarga pendiente'))}</div>"
                f"<div style=\"font-family:'EB Garamond',serif; font-size:12.5px; color:#b8b1a0;\">{_e(pending.get('detail', ''))}</div>"
                f"{url_html}"
            )
            if on_respond is not None:
                rid = str(pending.get("request_id") or "")
                with ui.element("div").style("display:flex; gap:12px; margin-top:4px;"):
                    deny = ui.element("button").style(
                        "padding:9px 18px; cursor:pointer; font-family:'Cinzel',serif; font-size:12px;"
                        f" letter-spacing:.06em; color:{RED_SOFT}; background:rgba(216,88,78,.12);"
                        f" border:1.5px solid rgba(216,88,78,.5); border-radius:4px;"
                    )
                    with deny:
                        ui.html("Denegar")
                    deny.on("click", lambda _=None, r=rid: on_respond(r, False))
                    approve = ui.element("button").style(
                        "padding:9px 18px; cursor:pointer; font-family:'Cinzel',serif; font-size:12px;"
                        " letter-spacing:.06em; color:#1c130a; background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40);"
                        " border:1.5px solid #f6e6bd; border-radius:4px;"
                    )
                    with approve:
                        ui.html("Aprobar")
                    approve.on("click", lambda _=None, r=rid: on_respond(r, True))
    else:
        ui.html(
            '<div style="padding:26px 0 34px; text-align:center;">'
            f"<div style=\"font-family:'Noto Sans Runic',serif; font-size:40px; color:{GREEN}; opacity:.5;\">ᛒ</div>"
            "<div style=\"font-family:'EB Garamond',serif; font-style:italic; color:#8a8068;\">El guardián descansa — no hay descargas esperando aprobación.</div></div>"
        )

    # ── Registro de la Puerta ──
    ui.html(
        '<div style="display:flex; align-items:center; gap:16px; margin:8px 0 14px;">'
        "<h3 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:14px; letter-spacing:.18em; color:#c9b998;\">REGISTRO DE LA PUERTA</h3>"
        f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:#857c69;\">{len(history)}</span>"
        '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.25),transparent);"></span></div>'
    )
    if not history:
        ui.html(
            "<div style=\"font-family:'EB Garamond',serif; font-style:italic; color:#8a8068; padding:8px 0 20px;\">"
            "Aún no hay actividad registrada — instalaciones, actualizaciones y sincronizaciones aparecerán acá.</div>"
        )
        return
    with ui.element("div").style("display:flex; flex-direction:column; gap:7px;"):
        for row in history:
            ui.html(_task_log_row_html(row))


def _task_log_row_html(row: dict[str, Any]) -> str:
    """Seam puro: fila del Registro de la Puerta como HTML (testeable sin UI)."""
    status = str(row.get("status") or "")
    color = _STATUS_COLORS.get(status.lower(), "#c2b48f")
    subject = str(row.get("mod_name") or row.get("detail") or "")
    line = (
        "display:flex; align-items:center; gap:14px; padding:10px 16px; border-radius:4px;"
        "background:rgba(62,39,35,.28); border:1px solid rgba(200,168,106,.16);"
    )
    return (
        f'<div style="{line}">'
        f'<span style="width:8px; height:8px; border-radius:50%; background:{color}; flex-shrink:0;"></span>'
        f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:{color}; min-width:88px;\">{_e(row.get('action', ''))}</span>"
        f"<span style=\"flex:1; min-width:0; font-family:'EB Garamond',serif; font-size:13px; color:#d9d2c0;"
        f' overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{_e(subject)}</span>'
        f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#857c69;\">{_e(row.get('created_at', ''))}</span>"
        "</div>"
    )


def _placeholder(section: str, callbacks: dict[str, Callable]) -> None:
    with ui.element("div").style(
        "display:flex; flex-direction:column; align-items:center; justify-content:center; padding:90px 0; gap:14px;"
    ):
        ui.html(
            f"<div style=\"font-family:'Noto Sans Runic',serif; font-size:54px; color:#c8a86a; opacity:.5;\">ᛟ</div>"
            f"<div style=\"font-family:'Cinzel',serif; font-size:24px; letter-spacing:.1em; color:#e7d6ad;\">{_e(section)}</div>"
            "<div style=\"font-family:'EB Garamond',serif; font-style:italic; color:#8a8068;\">Esta cámara de la forja llega en la próxima iteración.</div>"
        )
        on_nav = _cb(callbacks, "on_navigate")
        if on_nav:
            b = ui.element("button").style(
                "margin-top:8px; padding:11px 22px; cursor:pointer; font-family:'Cinzel',serif; letter-spacing:.08em; color:#1c130a;"
                "background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40); border:1.5px solid #f6e6bd; border-radius:4px;"
            )
            with b:
                ui.html("Volver al Panel")
            b.on("click", lambda _=None: on_nav("Dashboard"))
