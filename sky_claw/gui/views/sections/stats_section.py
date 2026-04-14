"""Sección de estadísticas del dashboard.

Muestra tarjetas con métricas clave: mods activos, actualizaciones pendientes,
conflictos y almacenamiento usado.

VIEW PURO - Sin lógica de negocio, solo presentación.
Recibe datos como parámetros, NO accede directamente al estado.
"""

from typing import Any

from nicegui import ui

from ..components import create_stat_card

# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "accent_violet": "#8b5cf6",
    "accent_cyan": "#06b6d4",
}


def create_stats_section(stats: dict[str, Any]) -> None:
    """Sección de estadísticas del dashboard.

    Muestra 4 tarjetas con métricas clave en un grid de 4 columnas.

    Args:
        stats: Diccionario con claves:
            - active_mods: Variable reactiva con número de mods activos
            - pending_updates: Variable reactiva con actualizaciones pendientes
            - conflicts_count: Variable reactiva con conteo de conflictos
            - storage_used: Variable reactiva con almacenamiento usado (GB)

    Example:
        >>> from sky_claw.gui.models.app_state import get_app_state
        >>> state = get_app_state()
        >>> create_stats_section({
        ...     'active_mods': state.active_mods,
        ...     'pending_updates': state.pending_updates,
        ...     'conflicts_count': state.conflicts_count,
        ...     'storage_used': state.storage_used,
        ... })
    """
    with ui.element("div").classes("grid grid-cols-4 gap-6 mb-8"):
        # Tarjeta: Active Mods
        create_stat_card(
            title="Active Mods",
            value_var=stats.get("active_mods", 0),
            subtitle="from last week",
            icon_svg=f'''
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
                     stroke="{COLORS["accent_violet"]}" stroke-width="2">
                    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2
                             0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2
                             2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                </svg>
            ''',
            trend="↑ 12%",
            trend_positive=True,
        )

        # Tarjeta: Pending Updates
        create_stat_card(
            title="Pending",
            value_var=stats.get("pending_updates", 0),
            subtitle="Updates available",
            icon_svg="""
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
                     stroke="#eab308" stroke-width="2">
                    <circle cx="12" cy="12" r="10"/>
                    <polyline points="12 6 12 12 16 14"/>
                </svg>
            """,
        )

        # Tarjeta: Conflicts
        create_stat_card(
            title="Conflicts",
            value_var=stats.get("conflicts_count", 0),
            subtitle="Needs attention",
            icon_svg="""
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
                     stroke="#ef4444" stroke-width="2">
                    <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2
                             2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>
                    <line x1="12" y1="9" x2="12" y2="13"/>
                    <line x1="12" y1="17" x2="12.01" y2="17"/>
                </svg>
            """,
        )

        # Tarjeta: Storage
        create_stat_card(
            title="Storage",
            value_var=stats.get("storage_used", 0),
            subtitle="GB used",
            icon_svg=f'''
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
                     stroke="{COLORS["accent_cyan"]}" stroke-width="2">
                    <rect x="2" y="4" width="20" height="16" rx="2"/>
                    <path d="M6 8h.01"/>
                    <path d="M10 8h.01"/>
                    <path d="M14 8h.01"/>
                </svg>
            ''',
        )
