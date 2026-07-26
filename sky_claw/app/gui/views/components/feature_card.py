"""Componente de tarjeta de feature.

Tarjeta visual para mostrar características/funcionalidades con badge opcional.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from __future__ import annotations

from nicegui import ui

# Paleta Nordic — tarjeta de pergamino: tinta oscura + cartucho de madera/ámbar.
COLORS = {
    "accent_wood_dark": "#3e2723",
    "accent_wood": "#5d4037",
    "accent_amber": "#ff9d00",
}


def create_feature_card(
    title: str,
    description: str,
    icon_svg: str,
    badge: str | None = None,
    badge_type: str = "info",
    on_click: callable | None = None,
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
                         style="background: linear-gradient(135deg, {COLORS["accent_wood_dark"]}, {COLORS["accent_wood"]});
                                border-color: {COLORS["accent_amber"]};">
                        {icon_svg}
                    </div>
                """)
                with ui.column():
                    ui.label(title).classes("text-[#2c2016] font-bold text-lg")
                    if badge:
                        ui.label(badge).classes(badge_class)
            ui.label(description).classes("text-[#5a4a38] text-sm leading-relaxed")

        if on_click:
            card.on("click", on_click)

    return card
