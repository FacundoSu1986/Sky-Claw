"""Componente de tarjeta de estadística.

Tarjeta visual para mostrar métricas y estadísticas con soporte
para binding reactivo del valor.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

# Paleta Nordic — la tarjeta es pergamino (claro), así que el texto va en tinta
# oscura y el ícono en un cartucho de madera para que el oro/ámbar resalte.
COLORS = {
    "accent_wood_dark": "#3e2723",
    "accent_wood": "#5d4037",
    "accent_amber": "#ff9d00",
}


def create_stat_card(
    title: str,
    value_var: Any,
    subtitle: str = "",
    icon_svg: str = "",
    trend: str | None = None,
    trend_positive: bool = True,
    on_click: Callable | None = None,
) -> ui.element:
    """Crea una tarjeta de estadística con bind reactivo.

    Args:
        title: Título de la estadística
        value_var: Variable reactiva para binding del valor
        subtitle: Subtítulo opcional
        icon_svg: SVG del icono como string
        trend: Texto de tendencia (ej. "+12%")
        trend_positive: True si la tendencia es positiva (verde), False si negativa (rojo)
        on_click: Callback opcional al hacer clic en la tarjeta

    Returns:
        ui.element: El elemento contenedor de la tarjeta
    """
    # Tinta legible sobre pergamino (verde/rojo claros desaparecían en el crema).
    trend_color = "text-green-700" if trend_positive else "text-red-700"

    with (
        ui.element("div")
        .classes("sky-parchment-card p-6")
        .on("mouseenter", lambda: ui.run_javascript("playSkyrimSound('hover')")) as card
    ):
        with ui.row().classes("items-center justify-between mb-4"):
            if icon_svg:
                ui.html(f"""
                    <div class="w-12 h-12 rounded-xl flex items-center justify-center border"
                         style="background: linear-gradient(135deg, {COLORS["accent_wood_dark"]}, {COLORS["accent_wood"]});
                                border-color: {COLORS["accent_amber"]};">
                        {icon_svg}
                    </div>
                """)
            ui.label(title).classes("text-[#5a4a38] text-sm")

        value_label = ui.label().classes("text-[#2c2016] text-4xl font-bold mb-2")
        value_label.bind_text_from(
            value_var,
            "_value",
            backward=lambda v: str(int(v) if isinstance(v, (int, float)) else v),
        )

        if subtitle or trend:
            with ui.row().classes("items-center justify-between"):
                if subtitle:
                    ui.label(subtitle).classes("text-[#5a4a38] text-xs")
                if trend:
                    ui.label(trend).classes(f"text-xs font-semibold {trend_color}")

        if on_click:
            card.on("click", on_click)

    return card
