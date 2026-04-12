"""Componente de tarjeta de feature.

Tarjeta visual para mostrar características/funcionalidades con badge opcional.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from typing import Optional
from nicegui import ui

# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "accent_violet": "#8b5cf6",
}


def create_feature_card(
    title: str,
    description: str,
    icon_svg: str,
    badge: Optional[str] = None,
    badge_type: str = "info",
    on_click: Optional[callable] = None,
) -> ui.element:
    """Crea una tarjeta de feature con badge opcional.

    Args:
        title: Título del feature
        description: Descripción del feature
        icon_svg: SVG del icono como string
        badge: Texto del badge opcional (ej. "NEW", "BETA")
        badge_type: Tipo de badge para estilizado ('info', 'success', 'warning', 'error')
        on_click: Callback opcional al hacer clic en la tarjeta

    Returns:
        ui.element: El elemento contenedor de la tarjeta
    """
    badge_class = f"sky-badge sky-badge--{badge_type}"

    with (
        ui.element("div")
        .classes("sky-parchment-card p-6 relative overflow-hidden")
        .on("mouseenter", lambda: ui.run_javascript("playSkyrimSound('hover')")) as card
    ):
        ui.html('<div class="sky-glow-overlay"></div>')
        with ui.column().classes("relative z-10"):
            with ui.row().classes("items-center gap-3 mb-4"):
                ui.html(f"""
                    <div class="w-14 h-14 rounded-2xl flex items-center justify-center border"
                         style="background: linear-gradient(135deg, {COLORS["accent_violet"]}20, {COLORS["accent_violet"]}05);
                                border-color: {COLORS["accent_violet"]}30;">
                        {icon_svg}
                    </div>
                """)
                with ui.column():
                    ui.label(title).classes("text-white font-bold text-lg")
                    if badge:
                        ui.label(badge).classes(badge_class)
            ui.label(description).classes("text-[#9ca3af] text-sm leading-relaxed")

        if on_click:
            card.on("click", on_click)

    return card
