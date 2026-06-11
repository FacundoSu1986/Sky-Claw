"""Página principal del dashboard.

Orquesta todas las secciones del dashboard para formar la vista completa.
VIEW PURO - Sin lógica de negocio, solo composición de vistas.

Esta página es un "presentador" que:
1. Recibe todos los datos necesarios como parámetros
2. Recibe callbacks para eventos del usuario
3. Compone las secciones visuales
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from sky_claw.antigravity.gui.models.app_state import NAV_SECTIONS

from ..layout.header import create_header
from ..layout.sidebar import create_sidebar
from ..mod_list import build_mod_list
from ..sections.chat_preview import create_chat_preview
from ..sections.cta_section import create_cta_section
from ..sections.features_section import create_features_section
from ..sections.mods_preview import create_mods_preview
from ..sections.stats_section import create_stats_section

# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "glow_violet": "#8b5cf6",
    "glow_cyan": "#06b6d4",
}


def render_dashboard(
    stats: dict[str, Any],
    mods: list[dict[str, Any]],
    chat_messages: list[dict[str, Any]],
    is_thinking: bool,
    callbacks: dict[str, Callable],
    active_section: str = "Dashboard",
) -> None:
    """Renderiza la página completa del dashboard.

    Compone todas las secciones del dashboard en el layout principal:
    - Sidebar (navegación lateral)
    - Header (encabezado)
    - Stats Section (estadísticas)
    - Features Section (características)
    - Mods Preview + Chat Preview (grid 2 columnas)
    - CTA Section (call-to-action)

    Args:
        stats: Estadísticas para la sección de stats con claves:
            - active_mods: Variable reactiva con número de mods activos
            - pending_updates: Variable reactiva con actualizaciones pendientes
            - conflicts_count: Variable reactiva con conteo de conflictos
            - storage_used: Variable reactiva con almacenamiento usado (GB)
        mods: Lista de mods para preview, cada uno con:
            - name: str - Nombre del mod
            - status: str - Estado ('active', 'update', 'conflict', 'inactive')
            - size_mb: int/float - Tamaño en MB
        chat_messages: Mensajes del chat, cada uno con:
            - content: str - Contenido del mensaje
            - is_user: bool - True si es del usuario
            - timestamp: str - Timestamp del mensaje
        is_thinking: Estado de procesamiento del agente
        callbacks: Dict con callbacks:
            - on_send_message: Callable[[str], None] - Envío de mensaje chat
            - on_view_all_mods: Callable - Ver todos los mods
            - on_mod_click: Callable[[str], None] - Clic en un mod
            - on_navigate: Callable[[str], None] - Navegación
            - on_cta_primary: Callable - Acción principal CTA
            - on_cta_secondary: Callable - Acción secundaria CTA (opcional)
            - on_feature_click: Callable[[str], None] - Clic en feature (opcional)
            - on_mod_toggle: Callable[[str, bool], Awaitable] - Toggle de mod en
              la sección Mods (opcional; sin él los switches se muestran
              deshabilitados)
        active_section: Sección activa de ``NAV_SECTIONS`` (Parte 5). Decide el
            highlight del sidebar Y el contenido del área principal:
            "Dashboard" → home, "Mods" → lista completa, resto → placeholder.
            En producción la provee el store (``store.get("active_section")``).

    Example:
        Los ``stats`` son los proxies reactivos del viewmodel (``ReactiveState``,
        vía ``get_state()`` en ``sky_claw_gui``), no el ``AppState`` puro:

        >>> state = get_state()  # ReactiveState (sky_claw_gui)
        >>> render_dashboard(
        ...     stats={
        ...         'active_mods': state.active_mods,
        ...         'pending_updates': state.pending_updates,
        ...         'conflicts_count': state.conflicts_count,
        ...         'storage_used': state.storage_used,
        ...     },
        ...     mods=[{'name': 'Test Mod', 'status': 'active', 'size_mb': 100}],
        ...     chat_messages=[],
        ...     is_thinking=False,
        ...     callbacks={
        ...         'on_send_message': lambda msg: print(f"Send: {msg}"),
        ...         'on_view_all_mods': lambda: print("View all"),
        ...         'on_mod_click': lambda name: print(f"Mod: {name}"),
        ...         'on_navigate': lambda page: print(f"Navigate: {page}"),
        ...         'on_cta_primary': lambda: print("Start!"),
        ...         'on_cta_secondary': lambda: print("Demo!"),
        ...     },
        ...     active_section="Dashboard",
        ... )
    """
    # Layout principal: Sidebar + Content
    with ui.element("div").classes("flex min-h-screen sky-stone-bg"):
        # Sidebar de navegación — la sección activa la decide el store
        # (Parte 5: NAVIGATION_REQUESTED → ReactiveState → re-render).
        create_sidebar(
            on_navigate=callbacks.get("on_navigate"),
            nav_items=[(section, section == active_section) for section in NAV_SECTIONS],
        )

        # Área de contenido principal
        with ui.element("div").classes("flex-1 flex flex-col sky-main-content"):
            # Header
            create_header()

            # Contenido scrolleable con fondo gradiente — la sección activa
            # decide QUÉ se renderiza (Parte 5: navegar cambia el contenido,
            # no solo el highlight del sidebar).
            with (
                ui.element("div")
                .classes("flex-1 p-8 overflow-y-auto sky-scrollbar")
                .style(
                    f"background: radial-gradient(ellipse at top, "
                    f"{COLORS['glow_violet']}12, transparent 50%), "
                    f"radial-gradient(ellipse at bottom right, "
                    f"{COLORS['glow_cyan']}8, transparent 50%);"
                )
            ):
                _render_active_section(
                    active_section,
                    stats=stats,
                    mods=mods,
                    chat_messages=chat_messages,
                    is_thinking=is_thinking,
                    callbacks=callbacks,
                )


# ── Parte 5: contenido por sección ─────────────────────────────────────────────

#: Secciones del sidebar sin vista dedicada todavía (placeholder honesto).
_PLACEHOLDER_SECTIONS: tuple[str, ...] = ("Conflicts", "Downloads", "Settings")


def mods_for_list(mods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapta los mods del dashboard (``status``) al contrato de ``build_mod_list``
    (``enabled``) — formateo visual simple permitido por las reglas de views/."""
    return [
        {
            "name": m.get("name", "Mod desconocido"),
            "enabled": m.get("status") != "inactive",
            "version": str(m.get("version", "")),
        }
        for m in mods
    ]


def _render_active_section(
    active_section: str,
    *,
    stats: dict[str, Any],
    mods: list[dict[str, Any]],
    chat_messages: list[dict[str, Any]],
    is_thinking: bool,
    callbacks: dict[str, Callable],
) -> None:
    """Despacha el contenido del área principal según la sección activa."""
    if active_section == "Mods":
        build_mod_list(
            mods=mods_for_list(mods),
            on_toggle=callbacks.get("on_mod_toggle"),
        )
        return
    if active_section in _PLACEHOLDER_SECTIONS:
        _render_placeholder_section(active_section, callbacks)
        return
    # Default: Dashboard (home)
    _render_home_sections(
        stats=stats,
        mods=mods,
        chat_messages=chat_messages,
        is_thinking=is_thinking,
        callbacks=callbacks,
    )


def _render_placeholder_section(section: str, callbacks: dict[str, Callable]) -> None:
    """Sección sin vista dedicada: placeholder explícito + vuelta al Dashboard.

    Mejor un placeholder honesto que re-renderizar silenciosamente el home
    (el usuario sabría que navegó pero vería el mismo contenido).
    """
    with ui.element("div").classes("flex flex-col items-center justify-center py-24 gap-4"):
        ui.icon("construction", size="3rem").classes("text-[#8b5cf6]")
        ui.label(section).classes("text-white text-3xl font-bold")
        ui.label("Esta sección llega en una próxima iteración.").classes("text-[#9ca3af]")
        on_navigate = callbacks.get("on_navigate")
        if on_navigate:
            ui.button(
                "Volver al Dashboard",
                on_click=lambda: on_navigate("Dashboard"),
            ).props("unelevated no-caps")


def render_dashboard_page_content(
    stats: dict[str, Any],
    mods: list[dict[str, Any]],
    chat_messages: list[dict[str, Any]],
    is_thinking: bool,
    callbacks: dict[str, Callable],
) -> None:
    """Renderiza solo el contenido del dashboard (sin sidebar ni header).

    Útil para integración en layouts personalizados o testing.

    Args:
        Ver render_dashboard() para descripción completa de parámetros.
    """
    # Contenido con fondo gradiente
    with (
        ui.element("div")
        .classes("flex-1 p-8 overflow-y-auto sky-scrollbar")
        .style(
            f"background: radial-gradient(ellipse at top, "
            f"{COLORS['glow_violet']}12, transparent 50%), "
            f"radial-gradient(ellipse at bottom right, "
            f"{COLORS['glow_cyan']}8, transparent 50%);"
        )
    ):
        _render_home_sections(
            stats=stats,
            mods=mods,
            chat_messages=chat_messages,
            is_thinking=is_thinking,
            callbacks=callbacks,
        )


def _render_home_sections(
    *,
    stats: dict[str, Any],
    mods: list[dict[str, Any]],
    chat_messages: list[dict[str, Any]],
    is_thinking: bool,
    callbacks: dict[str, Callable],
) -> None:
    """Secciones del home del dashboard (compartidas por ambos renderers)."""
    # Sección de estadísticas
    create_stats_section(stats)

    # Sección de features
    create_features_section(
        on_feature_click=callbacks.get("on_feature_click"),
    )

    # Grid de 2 columnas: Mods Preview + Chat Preview
    with ui.element("div").classes("grid grid-cols-2 gap-8 mb-8"):
        create_mods_preview(
            mods=mods,
            on_view_all=callbacks.get("on_view_all_mods"),
            on_mod_click=callbacks.get("on_mod_click"),
        )

        create_chat_preview(
            messages=chat_messages,
            is_thinking=is_thinking,
            on_send_message=callbacks.get("on_send_message"),
        )

    # Sección de Call-to-Action
    create_cta_section(
        on_primary_action=callbacks.get("on_cta_primary", lambda: None),
        on_secondary_action=callbacks.get("on_cta_secondary"),
    )
