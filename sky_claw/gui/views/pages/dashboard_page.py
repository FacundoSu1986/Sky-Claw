"""Página principal del dashboard.

Orquesta todas las secciones del dashboard para formar la vista completa.
VIEW PURO - Sin lógica de negocio, solo composición de vistas.

Esta página es un "presentador" que:
1. Recibe todos los datos necesarios como parámetros
2. Recibe callbacks para eventos del usuario
3. Compone las secciones visuales
"""

from collections.abc import Callable
from typing import Any

from nicegui import ui

from ..layout.header import create_header
from ..layout.sidebar import create_sidebar
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

    Example:
        >>> from sky_claw.gui.models.app_state import get_app_state
        >>> state = get_app_state()
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
        ... )
    """
    # Layout principal: Sidebar + Content
    with ui.element("div").classes("flex min-h-screen sky-stone-bg"):
        # Sidebar de navegación
        create_sidebar(
            on_navigate=callbacks.get("on_navigate"),
        )

        # Área de contenido principal
        with ui.element("div").classes("flex-1 flex flex-col sky-main-content"):
            # Header
            create_header()

            # Contenido scrolleable con fondo gradiente
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
                # Sección de estadísticas
                create_stats_section(stats)

                # Sección de features
                create_features_section(
                    on_feature_click=callbacks.get("on_feature_click"),
                )

                # Grid de 2 columnas: Mods Preview + Chat Preview
                with ui.element("div").classes("grid grid-cols-2 gap-8 mb-8"):
                    # Preview de mods recientes
                    create_mods_preview(
                        mods=mods,
                        on_view_all=callbacks.get("on_view_all_mods"),
                        on_mod_click=callbacks.get("on_mod_click"),
                    )

                    # Preview del chat con el agente
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
