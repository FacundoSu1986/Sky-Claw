"""Componente de ítem de lista de mod.

Ítem visual para mostrar información de un mod en una lista.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from collections.abc import Callable

from nicegui import ui


def create_mod_list_item(
    name: str,
    status: str,
    size: str,
    on_click: Callable | None = None,
) -> ui.element:
    """Crea un ítem de lista de mod.

    Args:
        name: Nombre del mod
        status: Estado del mod ('active', 'update', 'conflict', 'inactive')
        size: Tamaño del mod (ej. "125 MB")
        on_click: Callback opcional al hacer clic en el ítem

    Returns:
        ui.element: El elemento contenedor del ítem
    """
    status_config = {
        "active": ("bg-green-500", "Active"),
        "update": ("bg-yellow-500", "Update"),
        "conflict": ("bg-red-500", "Conflict"),
        "inactive": ("bg-gray-500", "Inactive"),
    }
    status_color, status_label = status_config.get(status, ("bg-gray-500", status))

    with ui.element("div").classes(
        "flex items-center justify-between py-3 border-b border-[#1f2937] "
        "hover:bg-[#1f2937]/30 transition-colors cursor-pointer"
    ) as item:
        with ui.row().classes("items-center gap-3 flex-1"):
            ui.html("""
                <div class="w-10 h-10 rounded-lg bg-[#1f2937] flex items-center justify-center">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
                         stroke="#9ca3af" stroke-width="2">
                        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0
                                 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0
                                 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0
                                 21 16z"/>
                    </svg>
                </div>
            """)
            with ui.column():
                ui.label(name).classes("text-white text-sm font-medium")
                ui.label(size).classes("text-[#6b7280] text-xs")

        ui.label(status_label).classes(f"{status_color} px-2 py-0.5 rounded-full text-xs font-medium text-white")

        if on_click:
            item.on("click", on_click)

    return item
