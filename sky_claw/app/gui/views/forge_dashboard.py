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

from sky_claw.app.gui.controllers.ritual_runner import (
    CLIENT_KEY_AUTO_APPROVE,
    RITUAL_INSTALLER_MAP,
    RITUAL_TOOL_MAP,
    STORE_KEY_RITUAL_FEEDBACK,
    STORE_KEY_RITUAL_PREFLIGHT,
    diagnostico_parallax_de,
    limpiar_diagnostico_parallax,
    lineas_del_diagnostico_parallax,
    resolve_ritual_resume_action,
    resolve_visible_pending_hitl,
    ritual_en_curso,
)
from sky_claw.app.gui.gui_helpers import _FRAGMENTO
from sky_claw.app.gui.icons import (
    _ICON_LOCK,
    _ICON_SHIELD_CHECK,
    _ICON_SWORDS,
    _ICON_UNLOCK,
    _icon_dragon_eye,
)
from sky_claw.app.gui.state import get_store

from .sections import create_preflight_panel

# Stash for the HITL respond callback so the module-level modal refreshable can
# reach it (set per render in render_forge_dashboard, like the live panels read
# the global store). Keyed to keep the indirection explicit.
_HITL_CALLBACKS: dict[str, Callable] = {}

# F-001 (post-D2): el MISMO patrón de stash para la acción de Resume del panel
# de feedback — el botón "Continuar DynDOLOD" no llama al service directamente:
# invoca el callback que el page cablea a ``run_ritual_resume`` (mismo
# dispatcher, mismo HITL, mismo single-flight).
_RITUAL_RESUME_CALLBACKS: dict[str, Callable] = {}

# Guard en app.storage.client (P1-7): render_forge_dashboard puede re-ejecutarse
# muchas veces en la vida de una pestaña — main_page es @ui.refreshable y hay
# ~10 claves del store suscriptas a su refresh (active_section, mods_list, etc).
# Sin este guard, cada re-render apilaba otro handler de on_connect que nunca se
# libera, y una reconexión terminaba disparando _hitl_modal_panel.refresh una
# vez por cada render que hubo en vez de una sola (review de PR #373).
_HITL_RECONNECT_HOOK_KEY = "hitl_reconnect_hook_armado"

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
# Preferencia persistida «Modo local por defecto» (Ajustes → ``config.toml``).
# La publica ``sky_claw_gui.cargar_modo_local_defecto`` y su ÚNICO lector de
# decisión es ``modo_local_enabled`` (ancla AST en test_modo_local_persistente.py).
CONFIG_KEY_MODO_LOCAL_DEFECTO = "gui_auto_approve_rituals"
STORE_KEY_MODO_LOCAL_DEFECTO = "modo_local_defecto"
# Preferencia APARTE para las descargas de red (egress): «Modo local»/F8 no la
# enciende. Único lector de decisión: ``descargas_auto_enabled``.
CONFIG_KEY_DESCARGAS_AUTO = "gui_auto_approve_downloads"
STORE_KEY_DESCARGAS_AUTO = "descargas_auto_defecto"
#: Clave de ``config.toml`` → clave del store, de TODAS las preferencias de
#: aprobación. Congelada por igualdad literal en test_modo_local_persistente.py:
#: persistencia, carga y validación iteran esta tabla, así que una preferencia
#: nueva no puede quedar a medio cablear.
PREFERENCIAS_DE_APROBACION: dict[str, str] = {
    CONFIG_KEY_MODO_LOCAL_DEFECTO: STORE_KEY_MODO_LOCAL_DEFECTO,
    CONFIG_KEY_DESCARGAS_AUTO: STORE_KEY_DESCARGAS_AUTO,
}

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
    # SKSE no es un ritual que se "ejecute" como los otros cinco: es el runtime que
    # el juego carga. Vive igual en esta grilla porque es la ÚNICA superficie donde
    # el operador puede aprobar su instalación (`RITUAL_INSTALLER_MAP["skse"]`), y sin
    # tarjeta ese instalador es código muerto — nadie puede apretarlo. Que no tenga
    # entrada en `RITUAL_TOOL_MAP` es lo que hace que `_ritual_action` no le prometa
    # un botón "Ejecutar" cuando ya está instalado.
    {
        "rune": "ᛊ",
        "label": "Extender el Motor",
        "desc": "Instala SKSE, requerido por la mayoría de los mods con scripts.",
        "tech": "SKSE",
        "tool": "skse",
        "tone": "#9c7a40",
    },
    # Community Shaders comparte la naturaleza de SKSE en la grilla: es un
    # instalador aprobado desde la GUI (`RITUAL_INSTALLER_MAP["community_shaders"]`)
    # y sin tarjeta ese instalador es código muerto. La detección es por
    # sentinel del mod bajo mo2_root/mods (caso especial del scanner): el VFS
    # de MO2 no es visible. Tampoco tiene entrada en `RITUAL_TOOL_MAP`: nada
    # de "Ejecutar" cuando ya está instalado.
    {
        "rune": "ᚱ",
        "label": "Renderizado Moderno",
        "desc": "Instala Community Shaders y sus dependencias (SKSE + sin ENB + preloader de Engine Fixes).",
        "tech": "Community Shaders",
        "tool": "community_shaders",
        "tone": "#6f9c7a",
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
        '<div class="sc-identity-text" style="text-align:right; white-space:nowrap;">'
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

    Devuelve ``"available"`` / ``"missing"`` / ``"present_unverified"`` cuando
    corrió un escaneo, o ``"unknown"`` antes de que el primer snapshot de
    :class:`EnvironmentScanner` aterrice en la store — así la UI nunca afirma que
    una herramienta está instalada sin prueba.

    ``"present_unverified"`` hoy solo lo puede producir SKSE: con la versión exacta
    de Skyrim ilegible, el scanner degrada su detección a "loader + algún DLL de
    runtime" (presencia) y nadie probó el build. Colapsarlo en ``available`` le
    mostraría al operador un cartel de compatibilidad que #491 rechazó.
    """
    if snapshot is None:
        return "unknown"
    has_tool = getattr(snapshot, "has_tool", None)
    if not callable(has_tool):
        return "unknown"
    if not has_tool(tool_key):
        return "missing"
    if tool_key == "skse" and _skse_present_but_unverified(snapshot):
        return "present_unverified"
    return "available"


def _skse_present_but_unverified(snapshot: Any) -> bool:
    """Lee la señal que calcula :class:`EnvironmentSnapshot` (evidencia ya escaneada).

    ``getattr`` defensivo: la store puede traer objetos que no son snapshots reales
    antes del primer scan, y ahí la señal no existe — sin evidencia no se degrada.
    """
    probe = getattr(snapshot, "skse_present_but_unverified", None)
    if not callable(probe):
        return False
    return bool(probe())


def _ritual_action(tool_key: str, state: str) -> str:
    """Qué ofrece el botón de una tarjeta: ``run``, ``install``, ``installed`` o ``none``.

    Se deriva de los dos mapas de cableado en vez de un flag por tarjeta, para que una
    tarjeta nunca prometa un botón que no tiene a dónde ir:

    * ``run`` exige estar en ``RITUAL_TOOL_MAP`` (el dispatcher que lo ejecuta).
    * ``install`` exige estar en ``RITUAL_INSTALLER_MAP`` (el método de ``ToolsInstaller``).
    * ``installed`` es un tool presente que Sky-Claw no ejecuta: no hay acción, pero
      tampoco es "falta algo".
    * ``none`` es el resto: sin scan todavía, o faltante sin auto-instalador.

    SKSE es el caso que motiva las dos ramas del medio: tiene instalador y **no** tiene
    —ni puede tener— dispatcher, porque es un runtime que carga el juego, no una tool
    que Sky-Claw ejecute. Con el estado genérico, una tarjeta ``available`` cableaba el
    botón a ``on_ritual_run`` y prometía "Ejecutar" sobre un dispatcher inexistente.

    ``installed`` y ``none`` están separados a propósito, aunque ninguno dispare una
    acción: colapsarlos mandaba el botón "Instalado" a la rama del aviso interino de
    :func:`_ritual_card`, que notifica "disponible en la próxima iteración" — sobre un
    componente que ya está instalado (revisión adversarial de #429).
    """
    if state == "available":
        return "run" if tool_key in RITUAL_TOOL_MAP else "installed"
    if state == "present_unverified":
        # Presencia sin compatibilidad probada: la única acción honesta es el seam
        # de verificación (`ensure_skse`), que en el caso idempotente devuelve
        # PRESENT_BUT_UNVERIFIED sin HITL, sin egress y sin writes — y el runner lo
        # publica como `warning`, no como éxito verde.
        return "verify" if tool_key in RITUAL_INSTALLER_MAP else "none"
    if state == "missing" and tool_key in RITUAL_INSTALLER_MAP:
        return "install"
    return "none"


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
    perfil_mo2: str | None = None,
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

    # Shell de aplicación a pantalla completa: alto fijo al viewport y el scroll
    # vive DENTRO del contenido (.sc-scroll). Con ``min-height`` la página entera
    # scrolleaba y el sidebar se estiraba con el contenido, así que la Vitalidad
    # del Sistema quedaba ~1000px debajo del pliegue en el Panel.
    root = (
        "--sky-accent:#c8a86a; position:relative; display:flex; height:100vh; width:100%; overflow:hidden;"
        "font-family:'EB Garamond',Georgia,serif; color:#e8e2d4;"
        "background:radial-gradient(130% 62% at 50% -12%, rgba(64,156,131,.18), transparent 56%),"
        "linear-gradient(180deg, rgba(8,11,15,.93), rgba(7,9,12,.975)), url('/assets/stone_bg.png');"
        "background-size:auto,auto,440px; background-attachment:fixed; -webkit-font-smoothing:antialiased;"
    )
    # Fase 2: expose the HITL respond callback to the module-level modal panel.
    _HITL_CALLBACKS["respond"] = _cb(callbacks, "on_hitl_respond")
    # F-001 (post-D2): expone la acción de Resume al panel de feedback. El
    # callback lo cablea el page (sky_claw_gui) a run_ritual_resume — la GUI
    # expresa intención y nada más.
    _RITUAL_RESUME_CALLBACKS["resume"] = _cb(callbacks, "on_ritual_resume")
    with ui.element("div").classes("sc-shell").style(root):
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
        # Tras un F5 con una aprobación pendiente, el primer render corre ANTES
        # del handshake, así que todavía no hay tab_id y el modal no se dibuja.
        # Nada más lo despertaría: el store no cambia, y es su cambio lo que
        # dispara el refresh. Re-renderizar al conectar recupera el modal en vez
        # de dejar la aprobación huérfana hasta su timeout.
        _refrescar_hitl_al_conectar()
        # Pila de avisos: un solo ancla fija y los paneles apilados en flujo. Con
        # dos ``position:fixed`` a offsets fijos (22px / 96px) un toast de varias
        # líneas quedaba debajo del semáforo de preflight.
        with ui.element("div").style(
            "position:fixed; right:22px; bottom:22px; z-index:1000; width:min(520px, calc(100vw - 44px));"
            "display:flex; flex-direction:column; align-items:stretch; pointer-events:none;"
        ):
            _ritual_preflight_panel()
            _ritual_feedback_panel()
        _parallax_diagnostico_panel()
        _sidebar(active, conflicts, pending, active_section, callbacks, connected, perfil_mo2)
        with ui.element("div").style(
            "position:relative; z-index:2; flex:1; min-width:0; min-height:0; display:flex; flex-direction:column;"
        ):
            _header(active_section, callbacks, identity, search_query)
            with (
                ui.element("div")
                .classes("sc-scroll")
                .style("flex:1; min-height:0; overflow-y:auto; padding:28px 32px 44px;")
            ):
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
    active: int,
    conflicts: int,
    pending: int,
    section: str,
    callbacks: dict[str, Callable],
    connected: bool,
    perfil_mo2: str | None = None,
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
            f"{_icon_dragon_eye(iris_id='scIris-sidebar')}</div>"
            '<div style="min-width:0;">'
            '<div style="font-family:\'Cinzel\',serif; font-weight:800; font-size:19px; letter-spacing:.16em; color:#f1e6cf; line-height:1;">SKY<span style="color:#c8a86a;">·</span>CLAW</div>'
            "<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12.5px; color:#9a7f4f; margin-top:3px;\">Forja del Dovahkiin</div>"
            "</div></div>"
        )
        # Connection — driven by the real websocket state (Codex review on #208);
        # a hardcoded green pulse would give a false health signal when offline.
        # sc-deco: la clase marcadora de movimiento reducido viaja junto a la
        # animación inline (contrato congelado en test_gui_theme_contracts.py).
        if connected:
            dot = 'class="sc-deco" style="width:9px; height:9px; border-radius:50%; background:#5f9c6b; box-shadow:0 0 9px #5f9c6b; animation:scPulse 2.6s ease-in-out infinite;"'
            border, label, label_color = "rgba(95,156,107,.28)", "DAEMON CONECTADO", "#bfcfb6"
        else:
            dot = 'style="width:9px; height:9px; border-radius:50%; background:#857c69;"'
            border, label, label_color = "rgba(200,168,106,.18)", "DAEMON DESCONECTADO", "#a39a85"
        ui.html(
            f'<div style="margin:14px 18px 4px; padding:9px 12px; display:flex; align-items:center; gap:10px; background:rgba(0,0,0,.28); border:1px solid {border}; border-radius:3px;">'
            f"<span {dot}></span>"
            '<div style="flex:1; min-width:0;">'
            f"<div style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.12em; color:{label_color};\">{label}</div>"
            "<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; color:#8a927f; margin-top:1px;\">ws://localhost:8765</div>"
            "</div></div>"
        )
        perfil_html = _perfil_mo2_html(perfil_mo2)
        if perfil_html:
            ui.html(perfil_html)
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


def _perfil_mo2_html(perfil: str | None) -> str:
    """Seam puro: el perfil de MO2 sobre el que opera ESTA sesión, o ``""`` si no se conoce.

    Es el perfil de la sesión (``AppContext.mo2_profile``, el que usan los
    rituales), no el del escaneo de arranque. Sin dato no se dibuja nada: un
    «Default» inventado le nombraría al operador un perfil que quizá no es.
    """
    nombre = str(perfil or "").strip()
    if not nombre:
        return ""
    return (
        '<div style="margin:8px 18px 0; padding:8px 12px; display:flex; align-items:baseline; gap:9px;'
        ' background:rgba(0,0,0,.28); border:1px solid rgba(200,168,106,.18); border-radius:4px;">'
        "<span style=\"flex-shrink:0; font-family:'Spline Sans Mono',monospace; font-size:9.5px; letter-spacing:.14em; color:#8a7f6a;\">PERFIL MO2</span>"
        f"<span style=\"flex:1; min-width:0; font-family:'Spline Sans Mono',monospace; font-size:11.5px; color:#e6dcc4;"
        f' overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="{_e(nombre)}">{_e(nombre)}</span></div>'
    )


def _nav_item(item: dict[str, str], is_active: bool, count: int | None, on_nav: Callable | None) -> None:
    # El fondo sólo va inline en el ítem activo: el inactivo lo deja a la clase
    # ``sc-nav`` para que su :hover (styles.css) no quede pisado por un inline.
    row_bg = (
        "background:linear-gradient(90deg, rgba(200,168,106,.17), rgba(200,168,106,.04));"
        " box-shadow:inset 0 0 0 1px rgba(200,168,106,.18);"
        if is_active
        else ""
    )
    icon_color = ACCENT_BRIGHT if is_active else "#9a917d"
    label_color = "#f1e6cf" if is_active else "#c4bca8"
    marker = "1" if is_active else "0"
    btn = (
        ui.element("button")
        .classes("sc-nav")
        .style(
            f"position:relative; width:100%; display:flex; align-items:center; gap:13px; padding:11px 14px; margin-bottom:3px;"
            f"border:none; border-radius:4px; cursor:pointer; text-align:left; {row_bg}"
        )
    )
    if on_nav:
        btn.on("click", lambda _=None, k=item["key"]: on_nav(k))
    # Contador como pastilla: Conflictos lo pinta en carmesí (pide juicio);
    # el resto en oro tenue. Un 0 no se muestra — el sidebar no grita "nada".
    tone = RED_SOFT if item["key"] == "Conflicts" else "#c8b88f"
    edge = "rgba(216,88,78,.45)" if item["key"] == "Conflicts" else "rgba(200,168,106,.28)"
    count_html = (
        f"<span style=\"flex-shrink:0; min-width:24px; padding:1px 7px; text-align:center; font-family:'Spline Sans Mono',monospace;"
        f' font-size:10.5px; line-height:1.5; color:{tone}; border:1px solid {edge}; border-radius:99px; background:rgba(0,0,0,.28);">{_e(count)}</span>'
        if count
        else ""
    )
    with btn:
        ui.html(
            f'<span style="position:absolute; left:0; top:18%; bottom:18%; width:2.5px; border-radius:2px;'
            f' background:linear-gradient(180deg,transparent,#ecd9a8,transparent); opacity:{marker}; box-shadow:0 0 10px rgba(200,168,106,.45);"></span>'
            f'<svg viewBox="0 0 24 24" fill="none" stroke="{icon_color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" width="18" height="18" style="flex-shrink:0;"><path d="{item["d"]}"></path></svg>'
            f"<span style=\"flex:1; min-width:0; font-family:'Cinzel',serif; font-size:14px; font-weight:500; letter-spacing:.06em; color:{label_color};\">{_e(item['label'])}</span>"
            f"{count_html}"
        ).style(_FRAGMENTO)


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
        "<span style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; color:#8a8270;\">v2.0 · NORDIC</span>"
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
        '<div class="sc-hud" style="display:flex; gap:14px; white-space:nowrap; font-family:\'Spline Sans Mono\',monospace; font-size:10.5px; color:#857c69;">'
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
        sesion = app.storage.client.get(CLIENT_KEY_AUTO_APPROVE)
    except Exception:
        return False
    return resolver_modo_local(sesion, get_store().get(STORE_KEY_MODO_LOCAL_DEFECTO))


def descargas_auto_enabled() -> bool:
    """True si el operador eligió en Ajustes no confirmar las descargas que él inicia.

    Sólo el booleano ``True`` arma (fail-closed). No hay toggle de sesión: es
    egress de red y se decide en Ajustes, no con un atajo de teclado.
    """
    return get_store().get(STORE_KEY_DESCARGAS_AUTO) is True


def resolver_modo_local(sesion: object, defecto: object) -> bool:
    """Seam puro: la elección explícita de la sesión gana; si no hay, la preferencia persistida.

    ``sesion`` es lo que el toggle (F8) escribió en ESTA conexión, o ``None`` si
    el operador no lo tocó. ``defecto`` es la preferencia de Ajustes. Sólo el
    booleano ``True`` arma la auto-aprobación: cualquier otro valor —ausente,
    string, número— deja el gate pidiendo confirmación (fail-closed).
    """
    if sesion is not None:
        return sesion is True
    return defecto is True


def tab_id_de(client: object) -> str | None:
    """Identidad ESTABLE de la pestaña de ``client``, o ``None`` sin handshake.

    ``tab_id`` y NO ``client.id``: este último es un ``uuid4()`` por instancia y
    cada carga de página crea un Client nuevo, así que scopear por él hacía
    desaparecer el modal al apretar F5 y dejaba la aprobación huérfana en el
    backend hasta su timeout. El ``tab_id`` lo manda el navegador en el
    handshake —por eso NiceGUI tiene ``old_tab_id`` + ``copy_tab`` para migrar
    el storage de la pestaña— y sobrevive la recarga.

    Devuelve ``None`` antes del handshake: no hay identidad todavía y no se
    inventa una. Seam puro para poder testear sin NiceGUI.
    """
    tab_id = getattr(client, "tab_id", None)
    return str(tab_id) if tab_id else None


def current_tab_id() -> str | None:
    """Identidad de ESTA pestaña, o ``None`` sin contexto de cliente.

    P1-7: identifica al dueño de una aprobación HITL. Mismo patrón defensivo que
    :func:`modo_local_enabled` — sin contexto (tests unitarios, task de fondo)
    devuelve ``None``, que :func:`resolve_pending_hitl` mapea a «visible para
    todas».
    """
    try:
        return tab_id_de(ui.context.client)
    except Exception:
        return None


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
    """Header toggle: candado abierto «Modo local» (auto-aprobar) / cerrado «Confirmar» (default).

    When ON, destructive Ritual approvals are auto-granted so the operator at the
    PC isn't prompted each time; OFF shows the Aprobar/Denegar modal. Default OFF
    (fail-closed), per-client, resets on disconnect. Atajo: F8.
    """
    on = modo_local_enabled()
    if on:
        icon, label, color, border, title = (
            _ICON_UNLOCK,
            "Modo local",
            "#9bbf8e",
            "rgba(95,156,107,.5)",
            "Auto-aprobando acciones (estás en la PC). F8 para alternar.",
        )
    else:
        icon, label, color, border, title = (
            _ICON_LOCK,
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
        ui.html(f'<span style="display:inline-flex;">{icon}</span><span>{_e(label)}</span>').style(_FRAGMENTO)


# ── HITL APPROVAL MODAL + RITUAL FEEDBACK (store-driven overlays) ─────────────────
def _respond_hitl(request_id: str, approved: bool) -> None:
    """Forward the captured request ID to the page-level guarded callback."""
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


def _refrescar_al_conectar() -> None:
    """Handler del handshake: re-renderiza TODO lo que el shell resuelve por dueño de pestaña.

    Antes del handshake ``current_tab_id()`` es ``None``, así que el primer render
    tras un F5 no ve ni la aprobación HITL ni la acción de resume que le pertenecen
    a esta pestaña. El modal y la grilla de rituales son los dos refreshables que
    leen ese estado (ancla que los enumera en test_ritual_en_curso.py).
    """
    _hitl_modal_panel.refresh()
    _rituales.refresh()


def _refrescar_hitl_al_conectar() -> None:
    """Re-renderiza el modal HITL cuando la pestaña completa su handshake.

    ``tab_id`` recién existe después del handshake (NiceGUI lo setea con el
    valor que manda el navegador), pero el contenido de la página se construye
    antes. Sin este re-render, un F5 con una aprobación pendiente resolvía la
    identidad como ``None`` en el único render que ocurre, y el modal no volvía
    a aparecer nunca — el store no cambia, y es su cambio lo que dispara el
    refresh.

    Se registra UNA sola vez por cliente (``app.storage.client``): un handler
    de ``on_connect`` que se apila en cada re-render nunca se libera solo, y
    ``main_page`` (``@ui.refreshable``) re-ejecuta este render en cada cambio de
    ~10 claves distintas del store durante la vida de la pestaña.

    Suprime: sin contexto de cliente (tests unitarios) no hay nada que enganchar.
    """
    with contextlib.suppress(Exception):
        client_storage = app.storage.client
        if client_storage.get(_HITL_RECONNECT_HOOK_KEY):
            return
        client_storage[_HITL_RECONNECT_HOOK_KEY] = True
        ui.context.client.on_connect(_refrescar_al_conectar)


def _es_descarga(pending: dict[str, Any]) -> bool:
    """Una solicitud es egress de red por categoría o —envelope legacy sin categoría— por traer URL."""
    return str(pending.get("category", "")) == "download" or bool(pending.get("url"))


def _hitl_solicitud_html(pending: dict[str, Any]) -> str:
    """Seam puro: el cuerpo de UNA solicitud HITL, igual en toda superficie que la muestre.

    Lo consumen el modal global y la Puerta de Descargas (ancla que enumera los
    llamadores en test_pantallas_forge.py): antes cada uno armaba su HTML y el
    modal no mostraba el origen de la descarga.

    * Banda + rótulo de categoría: una descarga es salida a internet (frost); el
      resto, una acción sobre la instalación (ámbar). El rótulo es la señal y el
      color sólo lo acompaña. La banda es ``position:absolute``: el contenedor
      que la reciba tiene que ser ``position:relative``.
    * Motivo en prosa y, aparte, lo técnico (detalle y URL) en una caja mono que
      se escanea de un vistazo. Todo escapado: son textos que eligió un tercero.
    """
    reason = str(pending.get("reason", "") or "Esta acción requiere tu aprobación.")
    banda, rotulo = (FROST, "DESCARGA DE RED") if _es_descarga(pending) else ("#e0a13c", "ACCIÓN SOBRE TU INSTALACIÓN")
    lineas_tecnicas = "".join(
        f'<div style="color:{color};">{_e(texto)}</div>'
        for texto, color in (
            (str(pending.get("detail", "") or ""), "#b8b1a0"),
            (str(pending.get("url", "") or ""), FROST),
        )
        if texto
    )
    caja_tecnica = (
        "<div style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; line-height:1.55; margin-bottom:16px;"
        " padding:9px 11px; border-radius:4px; background:rgba(0,0,0,.34); border:1px solid rgba(200,168,106,.16);"
        f' word-break:break-word;">{lineas_tecnicas}</div>'
        if lineas_tecnicas
        else '<div style="margin-bottom:16px;"></div>'
    )
    return (
        f'<div style="position:absolute; left:0; right:0; top:0; height:3px; background:{banda};"></div>'
        f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; letter-spacing:.14em; color:{banda}; margin-bottom:8px;\">{rotulo}</div>"
        f"<div style=\"font-family:'EB Garamond',serif; font-size:14px; line-height:1.5; color:#d8cfba; margin-bottom:8px;\">{_e(reason)}</div>"
        f"{caja_tecnica}"
    )


@ui.refreshable
def _hitl_modal_panel() -> None:
    """Overlay asking the operator to approve/deny a destructive Ritual.

    Rendered only while ``pending_hitl`` is set (the GUI HITL bridge parks it
    there when Modo local is off) and the active section is not Downloads —
    there the inline gate takes over. Buttons forward the decision through the
    ``on_hitl_respond`` callback; the guard's timeout still auto-denies.
    """
    # P1-7: la aprobación de ESTE cliente (la del Ritual que él lanzó), o una sin
    # dueño. Nunca la de otra pestaña: era accionable desde cualquier sesión.
    pending = resolve_visible_pending_hitl(get_store(), current_tab_id())
    if not _hitl_modal_visible(pending, str(get_store().get("active_section") or "")):
        return
    request_id = str(pending.get("request_id", ""))
    # El tip tiene que ser cierto para ESTA solicitud: «Modo local» no aprueba descargas.
    tip = (
        "Tip: para no confirmar las descargas que iniciás vos con «Instalar», Ajustes → Aprobaciones."
        if _es_descarga(pending)
        else "Tip: activá «Modo local» (F8) para no aprobar cada acción mientras estés en la PC; "
        "para dejarlo fijo, Ajustes → Aprobaciones."
    )
    overlay = (
        "position:fixed; inset:0; z-index:1000; display:flex; align-items:center; justify-content:center;"
        "background:rgba(7,9,12,.72); backdrop-filter:blur(3px);"
    )
    card = (
        "position:relative; overflow:hidden; max-width:520px; width:90%; padding:26px 26px 22px; border-radius:6px; color:#e8e2d4;"
        "background:linear-gradient(168deg, rgba(26,32,40,.98), rgba(11,14,19,.99)); border:1px solid rgba(200,168,106,.4);"
        "box-shadow:0 30px 70px -20px rgba(0,0,0,.9), inset 0 1px 0 rgba(255,255,255,.05);"
    )
    with ui.element("div").style(overlay), ui.element("div").style(card):
        ui.html(
            '<div style="display:flex; align-items:center; gap:10px; margin-bottom:12px;">'
            f'<span style="color:#ecd9a8; display:inline-flex;" aria-hidden="true">{_ICON_SHIELD_CHECK}</span>'
            "<span style=\"font-family:'Cinzel',serif; font-weight:700; font-size:15px; letter-spacing:.1em; color:#f1e6cf;\">APROBACIÓN REQUERIDA</span></div>"
            f"{_hitl_solicitud_html(pending)}"
            f"<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:11.5px; color:#857c69; margin-bottom:14px;\">{_e(tip)}</div>"
        )
        with ui.element("div").style("display:flex; gap:11px; justify-content:flex-end;"):
            deny = ui.element("button").classes("sc-btn sc-btn--danger").style("padding:9px 18px;")
            deny.on("click", lambda _=None, rid=request_id: _respond_hitl(rid, False))
            with deny:
                ui.html("Denegar")
            ok = ui.element("button").classes("sc-btn sc-btn--gold").style("padding:9px 18px;")
            ok.on("click", lambda _=None, rid=request_id: _respond_hitl(rid, True))
            with ok:
                ui.html("Aprobar")


def _resume_disponible() -> bool:
    """True si el page cableó el callback de resume (sin él no se ofrece el botón)."""
    return callable(_RITUAL_RESUME_CALLBACKS.get("resume"))


def _despachar_resume(accion: dict[str, Any]) -> None:
    """ÚNICO camino por el que la vista despacha una acción de resume (toast y tarjeta).

    Cierra el aviso y transporta ``tool_key``/``payload`` tal como los derivó el
    seam de ownership: la vista no decide qué intención se despacha (F-001), y el
    ``tool_key`` sale del envelope de la corrida, nunca de un literal (R-005).
    """
    fn = _RITUAL_RESUME_CALLBACKS.get("resume")
    # El payload viaja tal como lo derivó el seam (ancla AST en test_resume_etapa6_asistida.py).
    payload_de_la_accion = dict(accion.get("payload") or {})
    # Cierra el aviso, pero NO consume el resultado propio: eso lo hace
    # ``run_ritual`` después de sus guards. Si el runner rechaza el resume
    # (single-flight tomado, daemon no listo) la acción tiene que seguir ahí.
    get_store().set(STORE_KEY_RITUAL_FEEDBACK, None)
    if callable(fn):
        fn(str(accion.get("tool_key", "")), payload_de_la_accion)


@ui.refreshable
def _ritual_feedback_panel() -> None:
    """Dismissible toast (bottom-right) showing the last Ritual's result.

    F-001 (post-D2): cuando el RESULTADO estructural dice
    ``needs_deployment`` (flag, nunca texto), el toast además ofrece la acción
    explícita "Continuar DynDOLOD" — que despacha ``generate_lods`` con
    ``run_texgen=False`` por el MISMO dispatcher, sin tocar el service desde
    la vista y sin duplicar lógica de handoff.
    """
    fb = get_store().get(STORE_KEY_RITUAL_FEEDBACK)
    if not fb:
        return
    text = str(fb.get("text", ""))
    kind = str(fb.get("type", "info"))
    accent = {"positive": GREEN, "negative": RED, "warning": "#e0b341"}.get(kind, ACCENT)
    wrap = (
        "margin-top:10px; pointer-events:auto; display:flex; align-items:flex-start; gap:10px;"
        "padding:13px 15px; border-radius:5px; color:#e8e2d4;"
        f"background:linear-gradient(168deg, rgba(30,22,14,.97), rgba(14,10,7,.98)); border:1px solid {accent};"
        "box-shadow:0 18px 40px -18px rgba(0,0,0,.85);"
    )
    with ui.element("div").style(wrap):
        ui.html(
            f'<span style="width:8px; height:8px; margin-top:5px; flex-shrink:0; border-radius:50%; background:{accent}; box-shadow:0 0 7px {accent};"></span>'
            f"<span style=\"flex:1; font-family:'EB Garamond',serif; font-size:14px; line-height:1.5;\">{_e(text)}</span>"
        ).style(_FRAGMENTO)
        # F-001: la acción de Resume se deriva del dict ESTRUCTURAL del último
        # resultado — el panel no parsea el message ni decide por strings.
        # R-004/R-005: se resuelve por el seam de ownership — sólo la pestaña
        # dueña la ve — y el tool_key sale del envelope publicado por la
        # corrida, nunca de un literal acá (ancla AST en test_resume_hardening).
        accion = resolve_ritual_resume_action(get_store(), current_tab_id())
        if accion is not None:
            detalle = _e(str(accion.get("detail", "")))
            if _resume_disponible():
                r = (
                    ui.element("button")
                    .classes("sc-btn sc-btn--gold")
                    .style("margin-top:0; padding:6px 12px; font-size:11px; letter-spacing:.06em;")
                )
                r.on("click", lambda _=None, a=accion: _despachar_resume(a))
                with r:
                    ui.html(str(accion.get("label", "Continuar")))
                if detalle:
                    # El detalle lo redacta el seam y ya dice QUÉ falta y SOBRE QUÉ
                    # artefacto. El rótulo «TexGen Output listo en:» era de la etapa 9 y
                    # se imprimía sobre cualquier tool con acción de resume — con la
                    # etapa 6 habría anunciado un TexGen inexistente junto a un .esp.
                    ui.html(
                        f"<div style=\"font-family:'EB Garamond',serif; font-size:11px; color:#8a8270; line-height:1.3;\">"
                        f"{detalle}</div>"
                    )
        x = ui.element("button").style(
            "cursor:pointer; background:none; border:none; color:#8a8270; font-size:15px; line-height:1; padding:0 2px;"
        )
        x.on(
            "click",
            # La × cierra el AVISO, no la acción: «Ya construí el parche — verificar»
            # y «Continuar DynDOLOD» son controles obligatorios del flujo y siguen
            # disponibles en la tarjeta del ritual hasta que se despachen o una
            # corrida nueva de esta pestaña los reemplace.
            lambda _=None: (
                get_store().set(STORE_KEY_RITUAL_FEEDBACK, None),
                _ritual_feedback_panel.refresh(),
            ),
        )
        with x:
            ui.html("&times;")


@ui.refreshable
def _ritual_preflight_panel() -> None:
    """Semáforo de preflight del último Ritual, como panel dismissible (T-16b).

    Renderiza el reporte que el dispatch adjuntó (``result["preflight"]``) con
    :func:`create_preflight_panel` — hoy solo el sort de LOOT corre preflight. Un
    reporte rojo significa que el gate de ``loot_service`` ya frenó el sort; este
    panel lo hace visible. Se muestra solo mientras ``ritual_preflight`` esté set;
    el botón ``×`` lo limpia (mismo patrón que :func:`_ritual_feedback_panel`).
    """
    report = get_store().get(STORE_KEY_RITUAL_PREFLIGHT)
    if not report:
        return
    wrap = "margin-top:10px; pointer-events:auto; display:flex; align-items:flex-start; gap:8px;"
    with ui.element("div").style(wrap):
        with ui.element("div").style("flex:1; min-width:0;"):
            create_preflight_panel(report)
        x = ui.element("button").style(
            "cursor:pointer; background:none; border:none; color:#8a8270; font-size:15px; line-height:1; padding:0 2px;"
        )
        x.on(
            "click",
            lambda _=None: (get_store().set(STORE_KEY_RITUAL_PREFLIGHT, None), _ritual_preflight_panel.refresh()),
        )
        with x:
            ui.html("&times;")


@ui.refreshable
def _parallax_diagnostico_panel() -> None:
    """Resultado del diagnóstico de parallax de ESTA pestaña, como panel descartable (NP-A0i, #817).

    Se muestra solo mientras la pestaña tenga un diagnóstico vigente en su slot del store; el botón ``×`` limpia SOLO ese slot. El texto
    (nombres de mods y rutas que eligió un tercero) se dibuja con ``ui.label``, que lo escapa: nunca como HTML.
    """
    entrada = diagnostico_parallax_de(get_store(), current_tab_id())
    if not entrada:
        return
    lineas = lineas_del_diagnostico_parallax(entrada.get("resultado"))
    wrap = (
        "position:fixed; right:22px; top:84px; z-index:1000; width:min(560px, calc(100vw - 44px));"
        "max-height:70vh; display:flex; align-items:flex-start; gap:8px;"
    )
    card = (
        "flex:1; min-width:0; max-height:70vh; overflow:auto; padding:14px 16px; border-radius:10px;"
        "background:rgba(14,12,9,.96); border:1px solid rgba(200,168,106,.35);"
    )
    with ui.element("div").style(wrap):
        with ui.element("div").style(card):
            ui.label(f"Diagnóstico de parallax · {entrada.get('mod', '')}").style(
                "font-family:'Cinzel',serif; font-weight:700; font-size:13px; letter-spacing:.12em; color:#e7d6ad; margin-bottom:8px;"
            )
            for linea in lineas:
                ui.label(linea).style("font-size:12.5px; line-height:1.5; color:#cfc5ad; word-break:break-word;")
        x = ui.element("button").style(
            "cursor:pointer; background:none; border:none; color:#8a8270; font-size:15px; line-height:1; padding:0 2px;"
        )
        x.on(
            "click",
            lambda _=None: (
                limpiar_diagnostico_parallax(get_store(), current_tab_id()),
                _parallax_diagnostico_panel.refresh(),
            ),
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
            f'<div style="min-width:0; flex-shrink:1; overflow:hidden;"><div style="font-family:\'Cinzel\',serif; font-weight:700; font-size:16px; letter-spacing:.1em; color:#f1e6cf; line-height:1.15; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{_e(title)}</div>'
            f"<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12px; color:#897f6a; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;\">{_e(sub)}</div></div>"
        ).style(_FRAGMENTO)
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

#: Estado de la forja → (color del sello, variante de la barra). D3: la barra
#: ya no es dorada a fuego fijo — habla del estado. Fija también el color del
#: sello por estado (previamente VIGILANTE compartía el verde de ESTABLE).
_ESTADO_FORJA: dict[str, tuple[str, str]] = {
    "ESTABLE": ("#7fc08c", "estable"),
    "VIGILANTE": ("#e0a13c", "vigilante"),
    "EN DISPUTA": (RED_SOFT, "disputa"),
}


def _estado_de_forja(conflicts: int) -> tuple[str, str, str]:
    """Deriva ``(estado, color del sello, variante de barra)`` del conteo de conflictos.

    Los umbrales viven acá y no dentro del f-string del hero para que un test
    pueda enumerarlos: la barra sólo es señal si su color SALE del dato.
    """
    estado = "ESTABLE" if conflicts == 0 else ("VIGILANTE" if conflicts < 5 else "EN DISPUTA")
    color, variante = _ESTADO_FORJA[estado]
    return estado, color, variante


def _integridad_html(conflicts: int) -> str:
    """Panel «ESTADO DE LA FORJA» del hero: sello, barra reactiva y cifras (D3).

    Seam puro (sin llamadas a ``ui.*``, completamente testeable de forma aislada),
    igual que :func:`_vitals_html` y :func:`_hud_html`. La barra NO trae fondo
    inline —su color entero vive en ``.sc-bar--<variante>`` de styles.css—, así que
    la clase dinámica es la responsable de su visualización: emitir el panel desde
    acá deja que el ancla lo verifique sobre el HTML real en vez de inventariar
    el mapeo.
    """
    integrity = max(60, 100 - conflicts * 5)
    estado, estado_color, bar_variant = _estado_de_forja(conflicts)
    return (
        '<div style="flex:1; min-width:240px; padding:13px 16px; background:rgba(8,11,15,.6); border:1px solid rgba(200,168,106,.28); border-radius:4px; backdrop-filter:blur(4px);">'
        '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">'
        "<span style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.16em; color:#b6ab90;\">ESTADO DE LA FORJA</span>"
        f"<span style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.1em; color:{estado_color};\">◆ {_e(estado)}</span></div>"
        '<div style="height:7px; border-radius:4px; background:rgba(255,255,255,.07); overflow:hidden; box-shadow:inset 0 1px 2px rgba(0,0,0,.6);">'
        f'<div class="sc-deco sc-bar--{bar_variant}" style="height:100%; width:{integrity}%; border-radius:4px; background-size:200% 100%; animation:scShimmer 3.5s linear infinite;"></div></div>'
        f"<div style=\"display:flex; justify-content:space-between; margin-top:7px; font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#8a8270;\"><span>Integridad {integrity}%</span><span>{_e(conflicts)} conflictos</span></div></div>"
    )


#: Posición + espejo de cada esquina del marco del hero. Una sola filigrana
#: dibujada para la esquina superior izquierda; las otras tres son la misma
#: pieza reflejada, así el marco no puede quedar asimétrico por edición.
_ESQUINAS_HERO: tuple[tuple[str, str], ...] = (
    ("left:12px; top:12px;", "none"),
    ("right:12px; top:12px;", "scaleX(-1)"),
    ("left:12px; bottom:12px;", "scaleY(-1)"),
    ("right:12px; bottom:12px;", "scale(-1,-1)"),
)


def _filigrana_esquina(pos: str, espejo: str) -> str:
    """Esquina ornamental del marco del hero (SVG, sin raster ni emoji).

    Doble filete en escuadra, diagonal con rombo engarzado y remates en los
    extremos — el vocabulario del rombo de estado (◆) llevado al marco.
    Decorativa: ``aria-hidden`` y sin animación (no entra a la política de
    movimiento reducido).
    """
    return (
        f'<svg aria-hidden="true" width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="#c8a86a"'
        f' stroke-linecap="round" style="position:absolute; {pos} transform:{espejo}; opacity:.85;'
        ' filter:drop-shadow(0 0 4px rgba(200,168,106,.45)); pointer-events:none;">'
        '<path d="M1.5 36 V1.5 H36" stroke-width="1.5"/>'
        '<path d="M7.5 24 V7.5 H24" stroke-width="1" opacity=".55"/>'
        '<path d="M1.5 1.5 L8 8" stroke-width="1.1" opacity=".7"/>'
        '<path d="M11.5 7 L16 11.5 L11.5 16 L7 11.5 Z" fill="#c8a86a" stroke="none"/>'
        '<path d="M24 7.5 q6 0 8 -4" stroke-width="1" opacity=".45"/>'
        '<path d="M7.5 24 q0 6 -4 8" stroke-width="1" opacity=".45"/>'
        '<circle cx="36" cy="1.5" r="1.7" fill="#ecd9a8" stroke="none"/>'
        '<circle cx="1.5" cy="36" r="1.7" fill="#ecd9a8" stroke="none"/>'
        "</svg>"
    )


def _hero(active: int, conflicts: int, callbacks: dict[str, Callable]) -> None:
    sec = (
        "position:relative; overflow:hidden; border-radius:5px; min-height:236px; display:flex; align-items:flex-end;"
        "padding:26px 36px; margin-bottom:22px; border:1px solid rgba(200,168,106,.3);"
        "box-shadow:0 26px 60px -24px rgba(0,0,0,.85), inset 0 0 90px rgba(0,0,0,.45);"
    )
    with ui.element("section").style(sec):
        ui.html(
            "<div style=\"position:absolute; inset:0; background:url('/assets/alduin_menace_bg.jpg') center 32%/cover; transform:scale(1.04);\"></div>"
            '<div style="position:absolute; inset:0; background:linear-gradient(92deg, rgba(8,10,14,.85) 0%, rgba(8,10,14,.8) 34%, rgba(8,10,14,.32) 62%, rgba(10,12,16,.6) 100%);"></div>'
            '<div style="position:absolute; inset:0; background:linear-gradient(0deg, rgba(7,9,12,.96) 2%, transparent 42%);"></div>'
            '<div class="sc-deco" style="position:absolute; left:-10%; right:-10%; top:-46%; height:80%; pointer-events:none; background:radial-gradient(60% 100% at 50% 100%, rgba(70,180,150,.3), transparent 70%); filter:blur(26px); animation:scAurora 13s ease-in-out infinite;"></div>'
            + "".join(
                f'<span class="sc-ember" style="position:absolute; left:{lx}%; bottom:{by}px; width:3px; height:3px; border-radius:50%; background:#ffb347; box-shadow:0 0 7px #ff9d00; animation:scEmber {du}s linear {dly}s infinite;"></span>'
                for lx, by, du, dly in [
                    (64, 30, 6.5, 0.2),
                    (72, 20, 7.8, 1.4),
                    (80, 40, 5.6, 2.6),
                    (58, 18, 8.4, 3.3),
                    (88, 26, 7.0, 4.1),
                ]
            )
            + "".join(_filigrana_esquina(pos, espejo) for pos, espejo in _ESQUINAS_HERO)
        )
        with ui.element("div").style("position:relative; z-index:2; max-width:680px;"):
            ui.html(
                '<div style="display:flex; align-items:center; gap:12px; margin-bottom:8px;">'
                '<span style="height:1px; width:34px; background:linear-gradient(90deg,transparent,#c8a86a);"></span>'
                '<span style="font-family:\'Noto Sans Runic\',serif; font-size:14px; letter-spacing:.42em; color:#ecd9a8; opacity:.85; text-shadow:0 0 12px rgba(200,168,106,.45);" aria-hidden="true">ᛞᚱᚪᚷᚩᚾᛒᚩᚱᚾ</span></div>'
                '<div style="filter:drop-shadow(0 3px 14px rgba(0,0,0,.7));"><h1 style="margin:0; font-family:\'Cinzel\',serif; font-weight:900; font-size:clamp(28px,3.1vw,40px); line-height:1.05; letter-spacing:.03em; background:linear-gradient(178deg,#f7ecce 8%,#e3c98f 48%,#a47f48 100%); -webkit-background-clip:text; background-clip:text; color:transparent;">SALVE, DOVAHKIIN</h1></div>'
                f'<p style="margin:10px 0 18px; max-width:560px; font-family:\'EB Garamond\',serif; font-size:15.5px; line-height:1.5; color:#d8cfba;">Tu forja está despierta. <span style="color:#ecd9a8; font-weight:600;">{_e(active)} mods</span> montan guardia sobre Tamriel y el orden de carga aguarda tu palabra.</p>'
            )
            with ui.element("div").style("display:flex; flex-wrap:wrap; align-items:center; gap:18px;"):
                ui.html(_integridad_html(conflicts)).style(_FRAGMENTO)
                prepare = _cb(callbacks, "on_cta_primary")
                btn = (
                    ui.element("button")
                    .classes("sc-btn sc-btn--gold")
                    .style(
                        "display:flex; align-items:center; gap:11px; padding:15px 26px;"
                        "font-size:15px; letter-spacing:.12em;"
                        "box-shadow:0 0 24px rgba(200,168,106,.45), inset 0 1px 0 rgba(255,255,255,.5);"
                        # La transición inline extiende la de .sc-btn: sin "filter" acá,
                        # el brightness del hover cambiaba sin transición.
                        "transition:filter .2s ease, transform .2s, box-shadow .2s;"
                    )
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
        (_STAT_RUNES["space"], FROST, "ESPACIO", f"{storage:.1f}", "GB", "en mods activos"),
    ]
    cards = ""
    for rune, tone, label, value, unit, sub in defs:
        cards += (
            '<div style="position:relative; overflow:hidden; padding:20px; border-radius:4px;'
            " background:linear-gradient(162deg, rgba(26,32,40,.82), rgba(11,14,19,.9)); border:1px solid rgba(200,168,106,.2);"
            ' box-shadow:0 16px 34px -18px rgba(0,0,0,.8), inset 0 1px 0 rgba(255,255,255,.04);">'
            f'<span style="position:absolute; left:16px; right:16px; top:0; height:1px; background:linear-gradient(90deg,transparent,{tone},transparent); opacity:.55;" aria-hidden="true"></span>'
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
#: Insignias OPERATIVAS de una tarjeta (lo que está pasando ahora), aparte del
#: estado de instalación de ``_RITUAL_STATE_STYLE``. Congeladas por igualdad en
#: test_ritual_en_curso.py. El texto es la señal; el color sólo la matiza.
_INSIGNIAS_OPERATIVAS: dict[str, dict[str, str]] = {
    "ejecutando": {"texto": "EJECUTANDO…", "color": FROST},
    "instalando": {"texto": "INSTALANDO…", "color": FROST},
    "accion": {"texto": "ACCIÓN REQUERIDA", "color": "#e0a13c"},
}


def _estado_operativo_de_tarjeta(
    tool_key: str, en_curso: dict[str, str] | None, accion: dict[str, Any] | None
) -> dict[str, Any]:
    """Seam puro: qué muestra UNA tarjeta dado lo que corre y la acción pendiente.

    * ``bloqueada``: hay un single-flight tomado (por cualquier tool) — ningún
      botón de la grilla puede lanzar otro.
    * ``insignia``: la fase, sólo en la tarjeta de la tool que corre; si no,
      «acción requerida» cuando la acción de resume es de ESTA tool.
    * ``accion``: la acción de resume, sólo en la tarjeta de su tool.
    """
    propia = accion if accion is not None and accion.get("tool_key") == tool_key else None
    insignia: str | None = None
    if en_curso is not None and en_curso.get("tool_key") == tool_key and en_curso.get("fase") in _INSIGNIAS_OPERATIVAS:
        insignia = en_curso["fase"]
    elif propia is not None:
        insignia = "accion"
    return {"insignia": insignia, "bloqueada": en_curso is not None, "accion": propia}


@ui.refreshable
def _rituales(callbacks: dict[str, Callable]) -> None:
    ui.html(
        '<div style="display:flex; align-items:center; gap:16px; margin-bottom:6px;">'
        "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:17px; letter-spacing:.2em; color:#e7d6ad;\">RITUALES DE LA FORJA</h2>"
        '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span></div>'
        # El conteo sale de `_RITUALS` y no de un literal: la copia decía "cinco" y se
        # quedó desactualizada apenas la grilla creció.
        f"<p style=\"margin:0 0 18px; font-family:'EB Garamond',serif; font-style:italic; font-size:14px; color:#8a8068;\">El Motor Invisible — {len(_RITUALS)} herramientas legendarias, un solo gesto.</p>"
    )
    snapshot = get_store().get(STORE_KEY_ENV)
    on_ritual_run = _cb(callbacks, "on_ritual_run")
    on_ritual_install = _cb(callbacks, "on_ritual_install")
    # Refreshable propio: el estado «en curso» y la acción de resume cambian sin
    # re-renderizar la página (que resetearía el input del chat).
    en_curso = ritual_en_curso(get_store())
    accion = resolve_ritual_resume_action(get_store(), current_tab_id())
    with ui.element("div").style(
        "display:grid; grid-template-columns:repeat(auto-fill,minmax(232px,1fr)); gap:16px; margin-bottom:32px;"
    ):
        for r in _RITUALS:
            _ritual_card(
                r,
                _ritual_status(snapshot, r["tool"]),
                on_ritual_run,
                on_ritual_install,
                _estado_operativo_de_tarjeta(r["tool"], en_curso, accion),
            )


# Per-state chrome for a ritual card. "unknown" = scan hasn't landed yet, so we
# stay honest (neutral, no Ejecutar/Instalar claim) until the snapshot arrives.
# La atenuación de los estados no-listos va SOLO a lo decorativo
# (``deco_opacity`` sobre el medallón rúnico y un borde más tenue) — nunca a la
# tarjeta entera: un ``opacity`` global también bajaba el contraste del texto
# (#8a8270 componía ~2.5:1 con opacity .62, bajo WCAG AA; revisión Codex #522).
_RITUAL_STATE_STYLE: dict[str, dict[str, str]] = {
    "available": {
        "deco_opacity": "1",
        "card_border": "rgba(200,168,106,.22)",
        "dot": GREEN,
        "label": "Disponible",
        "color": "#9bbf8e",
        "btn_label": "Ejecutar",
        "btn_style": "color:#d8c69a; border-color:rgba(156,122,64,.5);",
    },
    "present_unverified": {
        "deco_opacity": "1",
        "card_border": "rgba(200,168,106,.22)",
        "dot": "#e0a13c",
        "label": "Presente, sin verificar",
        "color": "#d9b078",
        "btn_label": "Verificar",
        "btn_style": "color:#ffb05a; border-color:rgba(200,140,20,.5);",
    },
    "missing": {
        "deco_opacity": ".55",
        "card_border": "rgba(200,168,106,.12)",
        "dot": "#9c7a40",
        "label": "No instalado",
        "color": "#b8946a",
        "btn_label": "Instalar",
        "btn_style": "color:#ffb05a; border-color:rgba(200,100,20,.5);",
    },
    "unknown": {
        "deco_opacity": ".8",
        "card_border": "rgba(200,168,106,.16)",
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
    operativo: dict[str, Any] | None = None,
) -> None:
    tone = r["tone"]
    operativo = operativo or {"insignia": None, "bloqueada": False, "accion": None}
    insignia = _INSIGNIAS_OPERATIVAS.get(operativo["insignia"] or "")
    bloqueada = bool(operativo["bloqueada"])
    accion_resume = operativo["accion"]
    style = _RITUAL_STATE_STYLE.get(state, _RITUAL_STATE_STYLE["unknown"])
    deco_opacity = style["deco_opacity"]
    card_border = style["card_border"]
    status_dot = style["dot"]
    status_label = style["label"]
    status_color = style["color"]
    btn_label = style["btn_label"]
    btn_style = style["btn_style"]
    action = _ritual_action(r["tool"], state)
    # Un tool detectado que Sky-Claw no ejecuta (SKSE) está instalado y listo, pero no
    # hay nada que "ejecutar": el label genérico del estado mentiría.
    if action == "installed":
        btn_label = "Instalado"
        # El botón queda como indicador, no como acción: sin esto el cursor prometía
        # un click que no lleva a ninguna parte.
        btn_style += " cursor:default;"
    card = (
        f"position:relative; display:flex; flex-direction:column; gap:9px; padding:18px 18px 16px; border-radius:5px;"
        f"background:linear-gradient(168deg, rgba(30,22,14,.9), rgba(14,10,7,.92)); border:1px solid {card_border};"
        "box-shadow:0 14px 30px -16px rgba(0,0,0,.8), inset 0 1px 0 rgba(255,255,255,.04);"
    )
    with ui.element("div").classes("sc-card").style(card):
        ui.html(
            '<div style="display:flex; align-items:flex-start; justify-content:space-between; gap:10px;">'
            f'<div style="display:flex; align-items:center; justify-content:center; width:48px; height:48px; flex-shrink:0; border-radius:50%; background:radial-gradient(circle at 50% 38%, #2c1f13, #0d0805); border:1.5px solid {tone}; box-shadow:inset 0 0 10px rgba(0,0,0,.7), 0 0 12px {tone}55; opacity:{deco_opacity};">'
            f'<span style="font-family:\'Noto Sans Runic\',serif; font-size:23px; color:{tone}; text-shadow:0 0 9px {tone}88;" aria-hidden="true">{r["rune"]}</span></div>'
            f"<span style=\"margin-top:4px; padding:2px 8px; font-family:'Spline Sans Mono',monospace; font-size:10px; letter-spacing:.02em; color:#a89c80;"
            f' border:1px solid rgba(200,168,106,.2); border-radius:99px; background:rgba(0,0,0,.25); white-space:nowrap;">{_e(r["tech"])}</span></div>'
            f"<div style=\"margin-top:3px; font-family:'Cinzel',serif; font-weight:600; font-size:14.5px; letter-spacing:.03em; color:#ecdfc2; line-height:1.2;\">{_e(r['label'])}</div>"
            f"<div style=\"flex:1; font-family:'EB Garamond',serif; font-size:13px; line-height:1.42; color:#9b927e;\">{_e(r['desc'])}</div>"
            f'<div style="display:flex; align-items:center; gap:7px; padding-top:2px;"><span style="width:7px; height:7px; border-radius:50%; background:{status_dot}; box-shadow:0 0 6px {status_dot};"></span>'
            f"<span style=\"flex:1; font-family:'Spline Sans Mono',monospace; font-size:11px; letter-spacing:.02em; color:{status_color};\">{status_label}</span></div>"
        ).style(_FRAGMENTO)
        if insignia is not None:
            # El pulso es decoración (.sc-deco → se apaga con reduced-motion); el
            # estado lo dice el TEXTO de la insignia.
            ui.html(
                f'<div style="display:flex; align-items:center; gap:7px; padding:5px 9px; border-radius:4px;'
                f' background:rgba(0,0,0,.32); border:1px solid {insignia["color"]}66;">'
                f'<span class="sc-deco" style="color:{insignia["color"]}; font-size:10px; animation:scPulse 1.6s ease-in-out infinite;" aria-hidden="true">◆</span>'
                f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:10.5px; letter-spacing:.08em; color:{insignia['color']};\">{insignia['texto']}</span></div>"
            )
        if accion_resume is not None and _resume_disponible():
            # Control obligatorio del flujo (Fase 2 de Wrye Bash / handoff de
            # DynDOLOD) en un lugar que no se descarta: mismo despachador que el toast.
            fase2 = ui.element("button").classes("sc-btn sc-btn--gold").style("padding:8px 10px; font-size:11px;")
            if bloqueada:
                fase2.props("disabled")
            fase2.on("click", lambda _=None, a=accion_resume: _despachar_resume(a))
            with fase2:
                ui.html(_e(str(accion_resume.get("label", "Continuar"))))
            detalle_resume = str(accion_resume.get("detail", "") or "")
            if detalle_resume:
                ui.html(
                    f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; line-height:1.4; color:#8a8270; word-break:break-word;\">{_e(detalle_resume)}</div>"
                )
        b = (
            ui.element("button")
            .classes("sc-ritual-btn")
            .style(
                f"margin-top:2px; padding:8px 10px; cursor:pointer; font-family:'Cinzel',serif; font-size:11.5px; font-weight:600;"
                f"letter-spacing:.1em; background:rgba(0,0,0,.3); border:1px solid; border-radius:4px; {btn_style}"
            )
        )
        if bloqueada:
            # Hay un single-flight tomado: un segundo click sólo devolvería el aviso
            # «ya hay un ritual en curso». El botón lo dice sin tener que apretarlo.
            b.props("disabled")
        # Fase 2: an available tool runs for real through the supervisor dispatcher
        # (HITL-gated). Follow-up C: a "missing" tool with an auto-installer downloads
        # it through ToolsInstaller (download approval parks the GUI modal). Tools with
        # no installer (Wrye Bash / DynDOLOD) keep the honest interim notice.
        if action == "run" and on_ritual_run is not None:
            b.on("click", lambda _=None, tool=r["tool"]: on_ritual_run(tool))
        elif action == "install" and on_ritual_install is not None:
            b.on("click", lambda _=None, tool=r["tool"]: on_ritual_install(tool))
        elif action == "verify" and on_ritual_install is not None:
            # Mismo seam que "Instalar": `ensure_skse` decide entre verificar (caso
            # idempotente → PRESENT_BUT_UNVERIFIED publicado como warning, sin HITL/
            # egress/writes) o instalar de verdad. La tarjeta no promete resultado.
            b.on("click", lambda _=None, tool=r["tool"]: on_ritual_install(tool))
        elif action == "installed":
            # Rama propia, y no el aviso interino de abajo: ese le diría "disponible en
            # la próxima iteración" a un componente que YA está instalado.
            b.on(
                "click",
                lambda _=None, lbl=r["label"], tech=r["tech"]: ui.notify(
                    f"{lbl} ({tech}) ya está instalado: lo carga el juego, no hay nada que ejecutar.",
                    type="positive",
                ),
            )
        else:
            b.on(
                "click",
                lambda _=None, lbl=r["label"], tech=r["tech"]: ui.notify(
                    f"{lbl} ({tech}): disponible en la próxima iteración.", type="info"
                ),
            )
        with b:
            ui.html(btn_label)


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
            ).style(_FRAGMENTO)
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
    row = (
        ui.element("div")
        .classes("sc-row")
        .style(
            "display:flex; align-items:center; gap:13px; padding:11px 12px; border-radius:4px; border-bottom:1px solid rgba(200,168,106,.07); cursor:pointer;"
        )
    )
    if on_mod:
        row.on("click", lambda _=None, n=name: on_mod(n))
    with row:
        ui.html(
            f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:#8a8270; width:18px; flex-shrink:0;\">{idx}</span>"
            f'<span style="width:9px; height:9px; flex-shrink:0; border-radius:50%; background:{dot}; box-shadow:0 0 7px {dot};"></span>'
            '<div style="flex:1; min-width:0;"><div style="display:flex; align-items:center; gap:9px;">'
            f"<span style=\"font-family:'Cinzel',serif; font-size:14px; color:{name_color}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;\">{_e(name)}</span>{conflict_badge}</div>"
            f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#9a917d; margin-top:2px;\">{_e(ver)} · {_e(size)}</div></div>"
            f"<span style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12px; color:{dot}; width:74px; text-align:right; flex-shrink:0;\">{status_label}</span>"
        ).style(_FRAGMENTO)


# ── ASISTENTE ARCANO ───────────────────────────────────────────────────────────
def _asistente(chat_messages: list[dict[str, Any]], is_thinking: bool, callbacks: dict[str, Callable]) -> None:
    sec = (
        "position:relative; border-radius:5px; overflow:hidden; display:flex; flex-direction:column;"
        # Superficie oscura del tema (vidrio y acero): el pergamino claro era la única
        # pieza clara del shell y rompía el contraste del modo oscuro.
        "background:linear-gradient(168deg, rgba(26,32,40,.9), rgba(11,14,19,.95));"
        " border:1px solid rgba(200,168,106,.26);"
        "box-shadow:0 20px 44px -20px rgba(0,0,0,.85), inset 0 1px 0 rgba(255,255,255,.04);"
    )
    with ui.element("section").style(sec):
        ui.html(
            '<div style="position:absolute; inset:5px; border:1px solid rgba(200,168,106,.08); pointer-events:none; border-radius:3px;"></div>'
            '<div style="position:relative; display:flex; align-items:center; gap:12px; padding:15px 18px; border-bottom:1px solid rgba(200,168,106,.16); background:linear-gradient(180deg, rgba(200,168,106,.07), transparent);">'
            '<div style="width:40px; height:40px; flex-shrink:0; border-radius:50%; display:flex; align-items:center; justify-content:center; background:radial-gradient(circle at 50% 35%, #3a2c1a, #16100a); border:1.5px solid #c8a86a; box-shadow:0 0 12px rgba(200,168,106,.45);">'
            '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#ecd9a8" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 0 1 10 10c0 5.5-4.5 10-10 10S2 17.5 2 12"/><path d="M12 7v5"/><path d="M12 16h.01"/></svg></div>'
            '<div style="flex:1;"><div style="font-family:\'Cinzel\',serif; font-weight:700; font-size:15px; letter-spacing:.06em; color:#ecdfc2;">Asistente Arcano</div>'
            "<div style=\"font-family:'EB Garamond',serif; font-style:italic; font-size:12px; color:#8a8068;\">Consejero de la forja</div></div>"
            '<span style="font-family:\'Noto Sans Runic\',serif; font-size:15px; color:#c8a86a; opacity:.7;" aria-hidden="true">ᚺᚢᛗ</span></div>'
        )
        # Messages
        with (
            ui.element("div")
            .classes("sc-scroll")
            .style(
                "position:relative; flex:1; min-height:212px; max-height:420px; overflow-y:auto; padding:16px 18px; display:flex; flex-direction:column; gap:13px;"
            )
        ):
            if not chat_messages:
                _chat_bubble("¡Salve, Dovahkiin! Soy tu Asistente Arcano. ¿Qué deseas forjar hoy?", is_user=False)
            for c in chat_messages:
                _chat_bubble(str(c.get("content", "")), is_user=bool(c.get("is_user", False)))
            if is_thinking:
                ui.html(
                    "<div style=\"display:flex; align-items:center; gap:8px; color:#a39a85; font-family:'EB Garamond',serif; font-style:italic; font-size:13px;\">"
                    '<span style="display:inline-flex; gap:3px;">'
                    '<span class="sc-deco" style="width:5px; height:5px; border-radius:50%; background:#c8a86a; animation:scBlink 1.2s infinite;"></span>'
                    '<span class="sc-deco" style="width:5px; height:5px; border-radius:50%; background:#c8a86a; animation:scBlink 1.2s .2s infinite;"></span>'
                    '<span class="sc-deco" style="width:5px; height:5px; border-radius:50%; background:#c8a86a; animation:scBlink 1.2s .4s infinite;"></span></span>'
                    "Consultando los pergaminos…</div>"
                )
        # Input row
        with ui.element("div").style(
            "position:relative; display:flex; gap:9px; padding:13px 16px; border-top:1px solid rgba(200,168,106,.16);"
        ):
            on_send = _cb(callbacks, "on_send_message")
            chat_input = (
                ui.input(placeholder="Habla, y escucharé…")
                .classes("sc-chat-input")
                .props(
                    'dense borderless dark input-style="color:#e8e2d4; font-family:EB Garamond,serif; font-size:14.5px"'
                )
                .style(
                    "flex:1; padding:4px 13px;"
                    "background:rgba(0,0,0,.34); border:1px solid rgba(200,168,106,.26); border-radius:5px;"
                    "box-shadow:inset 0 2px 6px rgba(0,0,0,.45);"
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
                "background:linear-gradient(180deg,#f3dca0,#c8a86a 58%,#9c7a40); border:1px solid #f6e6bd; border-radius:5px; box-shadow:inset 0 1px 0 rgba(255,255,255,.12);"
            )
            sb.on("click", _send)
            with sb:
                ui.html(
                    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#1c130a" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>'
                )


def _chat_bubble(text: str, is_user: bool) -> None:
    if is_user:
        wrap = "display:flex; justify-content:flex-end;"
        who, who_color, who_align = "TÚ", "#a39a85", "text-align:right;"
        bubble = (
            "background:rgba(200,168,106,.14); color:#f1e6cf; border:1px solid rgba(200,168,106,.32);"
            " border-radius:9px 9px 2px 9px;"
        )
    else:
        wrap = "display:flex; justify-content:flex-start;"
        who, who_color, who_align = "ASISTENTE", "#c8a86a", ""
        bubble = (
            "background:rgba(255,255,255,.045); color:#d8cfba; border:1px solid rgba(255,255,255,.09);"
            " border-radius:9px 9px 9px 2px;"
        )
    ui.html(
        f'<div style="{wrap}"><div style="max-width:82%;">'
        f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; letter-spacing:.1em; color:{who_color}; margin-bottom:4px; {who_align}\">{who}</div>"
        f"<div style=\"padding:9px 13px; font-family:'EB Garamond',serif; font-size:14px; line-height:1.45; {bubble}\">{_e(text)}</div></div></div>"
    )


def _footer_rune() -> None:
    ui.html(
        '<div style="display:flex; align-items:center; justify-content:center; gap:18px; margin-top:34px; opacity:.85;">'
        '<span style="height:1px; width:90px; background:linear-gradient(90deg,transparent,rgba(200,168,106,.4));"></span>'
        '<span style="font-family:\'Noto Sans Runic\',serif; font-size:13px; letter-spacing:.45em; color:#c8a86a; text-shadow:0 0 10px rgba(200,168,106,.45);" aria-hidden="true">ᚦᚢ&nbsp;&nbsp;ᚠᚢᛋ&nbsp;&nbsp;ᚱᚩ&nbsp;&nbsp;ᛞᚪ</span>'
        '<span style="height:1px; width:90px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span></div>'
        "<div style=\"text-align:center; margin-top:10px; font-family:'EB Garamond',serif; font-style:italic; font-size:12.5px; color:#8a8270;\">«Que tu orden de carga sea estable y tu juego, eterno.»</div>"
    )


def _mods_screen(mods: list[dict[str, Any]], callbacks: dict[str, Callable], search_query: str = "") -> None:
    """Full mod-management screen (search + toggles).

    Reuses ``build_mod_list`` so the existing ``on_mod_toggle`` controls stay
    reachable from the Forge shell instead of the "próxima iteración" placeholder
    (Codex P1 on #208). ``search_query`` pre-filtra la lista cuando el usuario
    llega desde el buscador del header (A1).
    """
    from .mod_list import build_mod_list

    # El título de la sección ya lo da el header del shell: acá sólo va la acción.
    # Chequeo on-demand de actualizaciones en Nexus → reactiva el badge del
    # sidebar (pending_updates). Espejo de "Detectar disputas" de Conflictos.
    on_check = _cb(callbacks, "on_check_updates")
    if on_check is not None:
        with ui.element("div").style("display:flex; justify-content:flex-end; margin-bottom:6px;"):
            upd_btn = (
                ui.element("button")
                .classes("sc-btn sc-btn--gold")
                .style("flex-shrink:0; padding:9px 18px; font-size:12px; letter-spacing:.06em;")
            )
            with upd_btn:
                ui.html("Buscar actualizaciones")
            upd_btn.on("click", lambda _=None: on_check())
    adapted = [
        {
            "name": m.get("name", "Mod desconocido"),
            "enabled": _derive_status(m) != "inactive",
            "version": str(m.get("version", "") or ""),
        }
        for m in mods
    ]
    build_mod_list(
        mods=adapted,
        on_toggle=callbacks.get("on_mod_toggle"),
        initial_query=search_query,
        on_diagnose=callbacks.get("on_mod_diagnose"),
    )


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
            "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:17px; letter-spacing:.2em; color:#e7d6ad;\">SIN RESOLVER</h2>"
            f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:12px; color:{RED_SOFT};\">◆ {len(conflicts)}</span>"
            '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span>'
        ).style(_FRAGMENTO)
        on_scan = _cb(callbacks, "on_conflict_scan")
        if on_scan is not None:
            scan_btn = (
                ui.element("button")
                .classes("sc-btn sc-btn--gold")
                .style("flex-shrink:0; padding:9px 18px; font-size:12px; letter-spacing:.06em;")
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
            f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:{GREEN};\">◆ {len(resolved)}</span>"
            '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(95,156,107,.35),transparent);"></span>'
        ).style(_FRAGMENTO)
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
            f'<span style="color:{RED}; font-size:12px; flex-shrink:0;" aria-hidden="true">◆</span>'
            '<div style="flex:1; min-width:0;">'
            f"<div style=\"font-family:'Cinzel',serif; font-size:14px; color:#f1e6cf;\">{_e(c.get('mod_a', '?'))}"
            f'<span style="color:{RED_SOFT}; display:inline-flex; align-items:center; vertical-align:-2px; margin:0 4px;" aria-hidden="true">{_ICON_SWORDS}</span>'
            f"{_e(c.get('mod_b', '?'))}</div>"
            f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#8a7f6a; margin-top:3px;\">{_e(subtitle)}</div>"
            "</div>"
        ).style(_FRAGMENTO)
        if on_resolve is not None:
            btn = (
                ui.element("button")
                .classes("sc-btn sc-btn--gold")
                .style("flex-shrink:0; padding:8px 16px; font-size:12px; letter-spacing:.06em;")
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
        "background:linear-gradient(168deg, rgba(26,32,40,.98), rgba(11,14,19,.99)); border:1px solid rgba(200,168,106,.4);"
    )
    with ui.dialog() as dialog, ui.element("div").style(card):
        ui.html(
            "<div style=\"font-family:'Cinzel',serif; font-weight:700; font-size:15px; letter-spacing:.1em; color:#f1e6cf; margin-bottom:6px;\">RESOLVER DISPUTA</div>"
            f"<div style=\"font-family:'EB Garamond',serif; font-size:13px; color:#c9c0aa; margin-bottom:14px;\">{_e(c.get('mod_a', '?'))}"
            f'<span style="color:{RED_SOFT}; display:inline-flex; align-items:center; vertical-align:-2px; margin:0 4px;" aria-hidden="true">{_ICON_SWORDS}</span>'
            f"{_e(c.get('mod_b', '?'))}</div>"
        )
        note = (
            ui.textarea(
                placeholder="¿Cómo se resolvió? (opcional — p. ej. «parche en xEdit», «orden ajustado con LOOT»)"
            )
            .props("outlined autogrow dense")
            .style("width:100%; margin-bottom:16px;")
        )
        with ui.element("div").style("display:flex; gap:11px; justify-content:flex-end;"):
            cancel = (
                ui.element("button")
                .classes("sc-btn sc-btn--ghost")
                .style("padding:9px 18px; font-size:12px; letter-spacing:.08em;")
            )
            with cancel:
                ui.html("Cancelar")
            cancel.on("click", lambda _=None: dialog.close())
            ok = (
                ui.element("button")
                .classes("sc-btn sc-btn--gold")
                .style("padding:9px 18px; font-size:12px; letter-spacing:.08em;")
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


def _entorno_secciones(snapshot: Any) -> list[tuple[str, list[tuple[str, str]]]]:
    """Seam puro: lo que el escaneo de entorno encontró, como secciones de ``(etiqueta, valor)``.

    Sin snapshot real devuelve ``[]`` —no se afirma nada antes del primer
    escaneo—; con snapshot, lo que NO se detectó se dice explícitamente en vez
    de omitirse. ``getattr`` defensivo: la store puede traer objetos que no son
    snapshots (tests, arranque).
    """
    tools = getattr(snapshot, "tools", None)
    if not isinstance(tools, dict):
        return []
    skyrim = getattr(snapshot, "skyrim", None)
    mo2 = getattr(snapshot, "mo2", None)
    filas_skyrim = (
        [
            ("Edición", str(getattr(skyrim.edition, "value", skyrim.edition))),
            ("Versión", str(skyrim.version) or "No detectada"),
            ("Ruta", str(skyrim.path)),
        ]
        if skyrim is not None
        else [("Estado", "No detectado")]
    )
    filas_mo2 = (
        [("Ruta", str(mo2.path)), ("Perfiles", ", ".join(str(p) for p in mo2.profiles) or "Ninguno")]
        if mo2 is not None
        else [("Estado", "No detectado")]
    )
    filas_tools = [(str(t.name), str(t.exe_path)) for t in tools.values()]
    filas_tools += [(str(m.name), "No detectada") for m in getattr(snapshot, "missing", None) or []]
    return [
        ("SKYRIM", filas_skyrim),
        ("MOD ORGANIZER 2", filas_mo2),
        ("HERRAMIENTAS", filas_tools or [("Estado", "Ninguna detectada")]),
    ]


def _entorno_html(secciones: list[tuple[str, list[tuple[str, str]]]]) -> str:
    """Seam puro: las secciones del entorno como HTML (rutas en mono, todo escapado)."""
    bloques = ""
    for titulo, filas in secciones:
        filas_html = "".join(
            '<div style="display:flex; gap:12px; padding:3px 0;">'
            f"<span style=\"flex:0 0 96px; font-family:'EB Garamond',serif; font-size:12.5px; color:#8a7f6a;\">{_e(etiqueta)}</span>"
            f"<span style=\"flex:1; min-width:0; font-family:'Spline Sans Mono',monospace; font-size:11px; color:#d8cfba; word-break:break-all;\">{_e(valor)}</span>"
            "</div>"
            for etiqueta, valor in filas
        )
        bloques += (
            '<div style="margin-top:14px;">'
            f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; letter-spacing:.14em; color:#b8a87e; margin-bottom:4px;\">{_e(titulo)}</div>"
            f"{filas_html}</div>"
        )
    return bloques


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

    panel = (
        "display:flex; flex-direction:column; gap:14px; padding:20px 22px; margin-bottom:18px; border-radius:5px;"
        "max-width:760px; background:rgba(62,39,35,.3); border:1px solid rgba(200,168,106,.25);"
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
        # Receta .sc-toggle (styles.css). toggle-color/toggle-text-color = sc-tema,
        # un nombre SIN clase en Quasar: con el default (primary) la opción activa
        # recibe .bg-primary/.text-white, que son !important en la capa
        # quasar_importants de NiceGUI 3 y ningún !important sin capa del tema les
        # puede ganar (ancla en tests/test_gui_theme_contracts.py).
        provider_toggle = (
            ui.toggle(
                ["anthropic", "deepseek", "openai", "ollama"],
                value=str(settings.get("provider") or "deepseek"),
            )
            .classes("sc-toggle")
            .props("unelevated toggle-color=sc-tema toggle-text-color=sc-tema")
            .style("align-self:flex-start;")
        )
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

    # ── Aprobaciones (preferencia persistida de «Modo local») ──
    with ui.element("section").style(panel):
        ui.html(f'<span style="{_LBL}">APROBACIONES</span>')
        aprobaciones_toggle = (
            ui.toggle(
                {False: "Confirmar cada ritual", True: "Aprobar rituales automáticamente"},
                value=settings.get("modo_local_defecto") is True,
            )
            .classes("sc-toggle")
            .props("unelevated toggle-color=sc-tema toggle-text-color=sc-tema")
            .style("align-self:flex-start;")
        )
        ui.html(
            f'<span style="{_HINT}">Arranca cada sesión de esta PC en «Modo local» (F8 lo alterna al vuelo).</span>'
        )
        descargas_toggle = (
            ui.toggle(
                {False: "Confirmar cada descarga", True: "Aprobar descargas automáticamente"},
                value=settings.get("descargas_auto") is True,
            )
            .classes("sc-toggle")
            .props("unelevated toggle-color=sc-tema toggle-text-color=sc-tema")
            .style("align-self:flex-start;")
        )
        ui.html(
            f'<span style="{_HINT}">Con esto, «Instalar» descarga de internet sin preguntar. Vale sólo para lo que '
            "iniciás vos desde esta pantalla: las descargas que pida el agente o Telegram siguen pidiendo "
            "confirmación, igual que la promoción de un sandbox y la configuración de TexGen/DynDOLOD.</span>"
        )

    # ── Guardar ──
    if on_save is not None:

        def _collect_and_save(_: Any = None) -> None:
            payload = {key: str(inp.value or "") for key, inp in inputs.items()}
            payload["llm_provider"] = str(provider_toggle.value or "")
            payload[CONFIG_KEY_MODO_LOCAL_DEFECTO] = "true" if aprobaciones_toggle.value is True else "false"
            payload[CONFIG_KEY_DESCARGAS_AUTO] = "true" if descargas_toggle.value is True else "false"
            on_save(payload)

        btn = (
            ui.element("button")
            .classes("sc-btn sc-btn--gold")
            .style("align-self:flex-start; padding:11px 24px; letter-spacing:.08em;")
        )
        with btn:
            ui.html("Guardar Ajustes")
        btn.on("click", _collect_and_save)

    # ── Entorno detectado (sólo lectura) ──
    entorno = _entorno_html(_entorno_secciones(get_store().get(STORE_KEY_ENV)))
    if entorno:
        with ui.element("section").style(panel + "margin-top:22px;"):
            ui.html(
                f'<span style="{_LBL}">ENTORNO DETECTADO</span>'
                f'<div style="{_HINT} margin-top:3px;">Sólo lectura: lo que encontró el escaneo de arranque.</div>'
                f"{entorno}"
            )


_STATUS_COLORS = {"ok": GREEN, "success": GREEN, "registered": GREEN, "failed": RED, "error": RED}

# Etiqueta legible de estado en el registro de la Puerta (D6): el color y el rombo
# son decoración, la etiqueta es la señal. Cubre los estados del mapa de color;
# el fallback es el valor crudo del backend (nunca inventado).
_STATUS_LABELS: dict[str, str] = {
    "ok": "OK",
    "success": "OK",
    "registered": "REGISTRADO",
    "failed": "FALLÓ",
    "error": "ERROR",
}


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
        "<h2 style=\"margin:0; font-family:'Cinzel',serif; font-weight:700; font-size:17px; letter-spacing:.2em; color:#e7d6ad;\">SOLICITUD PENDIENTE</h2>"
        '<span style="flex:1; height:1px; background:linear-gradient(90deg,rgba(200,168,106,.4),transparent);"></span></div>'
    )
    if pending:
        card = (
            "position:relative; overflow:hidden; display:flex; flex-direction:column; max-width:760px;"
            " padding:22px 22px 18px; margin-bottom:22px; border-radius:5px;"
            "background:linear-gradient(168deg, rgba(26,32,40,.9), rgba(11,14,19,.95)); border:1px solid rgba(200,168,106,.4);"
        )
        with ui.element("div").style(card):
            # Misma solicitud, mismo dibujo que el modal global (seam compartido).
            ui.html(_hitl_solicitud_html(pending))
            if on_respond is not None:
                rid = str(pending.get("request_id") or "")
                with ui.element("div").style("display:flex; gap:12px; justify-content:flex-end;"):
                    deny = (
                        ui.element("button")
                        .classes("sc-btn sc-btn--danger")
                        .style("padding:9px 18px; font-size:12px; letter-spacing:.06em;")
                    )
                    with deny:
                        ui.html("Denegar")
                    deny.on("click", lambda _=None, r=rid: on_respond(r, False))
                    approve = (
                        ui.element("button")
                        .classes("sc-btn sc-btn--gold")
                        .style("padding:9px 18px; font-size:12px; letter-spacing:.06em;")
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
    # El rombo ◆ reemplaza al dot CSS plano: ya no es una suerte de punto decorativo
    # sino el vocabulario de estado compartido con el hero y los badges de Disputas
    # (D6). POR ACCESIBILIDAD el rombo es decorativo (`aria-hidden="true"`) y la
    # señal semántica real es el texto a su lado (status visible). El color se
    # reserva como redundancia (quien ve hueco ya tiene qué es).
    # Mapa de estado → etiqueta legible: el color solo la matiza.
    label = _STATUS_LABELS.get(status.lower(), status)

    return (
        f'<div style="{line}">'
        f'<span style="color:{color}; flex-shrink:0; font-size:11px;" aria-hidden="true">◆</span>'
        f"<span style=\"font-family:'Spline Sans Mono',monospace; font-size:11px; color:{color}; min-width:88px;\">{_e(row.get('action', ''))}</span>"
        f'<span style="font-family:\'Spline Sans Mono\',monospace; font-size:10.5px; color:{color};" aria-hidden="false">[{_e(label)}]</span>'
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
            b = (
                ui.element("button")
                .classes("sc-btn sc-btn--gold")
                .style("margin-top:8px; padding:11px 22px; letter-spacing:.08em;")
            )
            with b:
                ui.html("Volver al Panel")
            b.on("click", lambda _=None: on_nav("Dashboard"))
