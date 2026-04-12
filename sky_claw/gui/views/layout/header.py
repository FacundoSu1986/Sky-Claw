"""Componente de header.

Cabecera de la aplicación con título, búsqueda y avatar de usuario.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from typing import Optional, Callable
from nicegui import ui

# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "accent_violet": "#8b5cf6",
    "accent_pink": "#ec4899",
}


def create_header(
    title: str = "Dashboard",
    subtitle: Optional[str] = None,
    user_initials: str = "DS",
    search_placeholder: str = "Search mods, conflicts, or ask me anything...",
    on_search: Optional[Callable[[str], None]] = None,
    on_avatar_click: Optional[Callable] = None,
) -> ui.element:
    """Crea el header de la aplicación.

    Args:
        title: Título principal del header
        subtitle: Subtítulo opcional (ej. "Welcome back, Dragonborn")
        user_initials: Iniciales del usuario para el avatar
        search_placeholder: Placeholder del campo de búsqueda
        on_search: Callback de búsqueda, recibe el texto de búsqueda
        on_avatar_click: Callback al hacer clic en el avatar

    Returns:
        ui.element: El elemento contenedor del header
    """
    with ui.element("div").classes(
        "h-16 bg-[#0a0a0a] border-b border-[#1f2937] flex items-center "
        "justify-between px-6"
    ) as header:
        with ui.column():
            ui.label(title).classes("text-white font-bold text-xl")
            if subtitle:
                ui.label(subtitle).classes("text-[#6b7280] text-xs")

        with ui.element("div").classes("flex-1 max-w-md mx-8"):
            with ui.element("div").classes(
                "relative bg-[#0f0f0f] border border-[#1f2937] rounded-xl "
                "overflow-hidden sky-input-premium"
            ):
                search_input = ui.input(
                    placeholder=search_placeholder,
                    value="",
                ).classes(
                    "w-full px-4 py-3 bg-transparent border-none text-white "
                    "placeholder-[#6b7280] focus:outline-none"
                )

                if on_search:
                    search_input.on(
                        "keydown.enter",
                        lambda e: on_search(e.value) if e.value else None,
                    )

        with ui.row().classes("items-center gap-4"):
            with (
                ui.element("div")
                .classes(
                    "w-10 h-10 rounded-full flex items-center justify-center "
                    "text-white font-bold cursor-pointer sky-card-hover"
                )
                .style(
                    f"background: linear-gradient(135deg, "
                    f"{COLORS['accent_violet']}, {COLORS['accent_pink']});"
                ) as avatar
            ):
                ui.label(user_initials)

                if on_avatar_click:
                    avatar.on("click", on_avatar_click)

    return header
