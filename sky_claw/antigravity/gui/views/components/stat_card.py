"""Componente de tarjeta de estadística.

Tarjeta visual para mostrar métricas y estadísticas con soporte
para binding reactivo del valor.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from collections.abc import Callable
from typing import Any

from nicegui import ui

# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "accent_violet": "#8b5cf6",
    "accent_cyan": "#06b6d4",
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
    trend_color = "text-green-400" if trend_positive else "text-red-400"

    with (
        ui.element("div")
        .classes("sky-parchment-card p-6")
        .on("mouseenter", lambda: ui.run_javascript("playSkyrimSound('hover')")) as card
    ):
        with ui.row().classes("items-center justify-between mb-4"):
            if icon_svg:
                ui.html(f"""
                    <div class="w-12 h-12 rounded-xl flex items-center justify-center border"
                         style="background: linear-gradient(135deg, {COLORS["accent_violet"]}20, {COLORS["accent_cyan"]}20);
                                border-color: {COLORS["accent_violet"]}30;">
                        {icon_svg}
                    </div>
                """)
            ui.label(title).classes("text-[#9ca3af] text-sm")

        value_label = ui.label().classes("text-white text-4xl font-bold mb-2")
        value_label.bind_text_from(
            value_var,
            "_value",
            backward=lambda v: str(int(v) if isinstance(v, (int, float)) else v),
        )

        if subtitle or trend:
            with ui.row().classes("items-center justify-between"):
                if subtitle:
                    ui.label(subtitle).classes("text-[#6b7280] text-xs")
                if trend:
                    ui.label(trend).classes(f"text-xs font-semibold {trend_color}")

        if on_click:
            card.on("click", on_click)

    return card
