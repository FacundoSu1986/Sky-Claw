"""Componentes de botones CTA.

Botones de llamada a la acción con variantes visuales.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from collections.abc import Callable

from nicegui import ui


def create_cta_button(
    text: str,
    on_click: Callable,
    variant: str = "primary",
    icon_svg: str | None = None,
) -> ui.button:
    """Crea un botón CTA con variantes visuales.

    Args:
        text: Texto del botón
        on_click: Callback al hacer clic
        variant: Variante visual ('primary', 'secondary', 'ghost')
        icon_svg: SVG del icono opcional como string

    Returns:
        ui.button: El botón creado
    """
    if variant == "primary":
        button_classes = (
            "sky-btn-cta px-8 py-4 rounded-full text-white font-semibold text-lg flex items-center gap-3 cursor-pointer"
        )
    elif variant == "secondary":
        button_classes = (
            "sky-btn-secondary px-6 py-3 rounded-xl text-white font-medium flex items-center gap-2 cursor-pointer"
        )
    else:
        button_classes = (
            "px-4 py-2 text-[#9ca3af] hover:text-white hover:bg-[#1f2937] "
            "rounded-lg cursor-pointer transition-all duration-200"
        )

    button = ui.button().classes(button_classes).props("ripple")
    with button:
        if icon_svg:
            ui.html(f'<span class="mr-2">{icon_svg}</span>')
        ui.label(text)
        button.on("click", lambda: [on_click(), ui.run_javascript("playSkyrimSound('click')")])
    return button
