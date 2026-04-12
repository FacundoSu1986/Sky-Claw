"""Sección de características/features del dashboard.

Muestra tarjetas con las características principales de la aplicación
en un grid de 3 columnas.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from typing import Callable, Optional
from nicegui import ui

from ..components import create_feature_card


# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "accent_violet": "#8b5cf6",
    "accent_cyan": "#06b6d4",
}


def create_features_section(
    on_feature_click: Optional[Callable[[str], None]] = None,
) -> None:
    """Sección de features/características de la app.

    Muestra 3 tarjetas con las características principales en un grid de 3 columnas.

    Args:
        on_feature_click: Callback opcional que recibe el nombre del feature
                         cuando se hace clic en una tarjeta.

    Example:
        >>> def handle_feature_click(feature_name: str):
        ...     print(f"Feature clicked: {feature_name}")
        >>> create_features_section(on_feature_click=handle_feature_click)
    """
    with ui.element("div").classes("mb-8"):
        ui.label("Core Features").classes("text-white text-xl font-bold mb-6")

        with ui.element("div").classes("grid grid-cols-3 gap-6"):
            # Feature: Smart Search
            create_feature_card(
                title="Smart Search",
                description="Natural language search across thousands of "
                "Skyrim mods with AI-powered recommendations",
                icon_svg=f'''
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                         stroke="{COLORS["accent_violet"]}" stroke-width="2">
                        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>
                        <path d="M11 8v6"/><path d="M8 11h6"/>
                    </svg>
                ''',
                badge="NEW",
                badge_type="violet",
                on_click=lambda: (
                    on_feature_click("Smart Search") if on_feature_click else None
                ),
            )

            # Feature: Conflict Resolution
            create_feature_card(
                title="Conflict Resolution",
                description="Automatically detect and resolve mod conflicts "
                "using advanced dependency analysis",
                icon_svg="""
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                         stroke="#ef4444" stroke-width="2">
                        <path d="M12 9v4"/><path d="M12 17h.01"/>
                        <path d="M3.44 18.54l7.04-12.15a2 2 0 0 1 3.04
                                 0l7.04 12.15a2 2 0 0 1-1.52 2.93H4.96a2
                                 2 0 0 1-1.52-2.93z"/>
                    </svg>
                """,
                on_click=lambda: (
                    on_feature_click("Conflict Resolution")
                    if on_feature_click
                    else None
                ),
            )

            # Feature: Zero-Trust Security
            create_feature_card(
                title="Zero-Trust Security",
                description="HITL protection with Telegram integration for "
                "approving external downloads safely",
                icon_svg=f'''
                    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
                         stroke="{COLORS["accent_cyan"]}" stroke-width="2">
                        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                        <path d="m9 12 2 2 4-4"/>
                    </svg>
                ''',
                on_click=lambda: (
                    on_feature_click("Zero-Trust Security")
                    if on_feature_click
                    else None
                ),
            )
