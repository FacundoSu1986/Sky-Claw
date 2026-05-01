"""Sección de preview de mods recientes del dashboard.

Muestra una lista de mods recientes con su estado y tamaño.
Los datos se reciben como parámetro, NO se obtienen directamente.

VIEW PURO - Sin lógica de negocio, solo presentación.
Separada de la lógica de obtención de datos.
"""

from collections.abc import Callable
from typing import Any

from nicegui import ui

from ..components import create_cta_button, create_mod_list_item


def create_mods_preview(
    mods: list[dict[str, Any]],
    on_view_all: Callable | None = None,
    on_mod_click: Callable[[str], None] | None = None,
    title: str = "Recent Mods",
    empty_message: str = "No mods found",
) -> None:
    """Preview de mods recientes.

    Muestra una lista de mods en un contenedor con bordes redondeados.
    Cada mod muestra nombre, estado y tamaño.

    Args:
        mods: Lista de diccionarios con claves:
            - name: str - Nombre del mod
            - status: str - Estado ('active', 'update', 'conflict', 'inactive')
            - size_mb: int/float - Tamaño en MB (se convierte a GB si > 1024)
        on_view_all: Callback para botón "View All" (opcional)
        on_mod_click: Callback cuando se hace clic en un mod, recibe el nombre (opcional)
        title: Título de la sección (default: "Recent Mods")
        empty_message: Mensaje cuando no hay mods (default: "No mods found")

    Example:
        >>> mods_data = [
        ...     {'name': 'Skyrim 202X', 'status': 'active', 'size_mb': 2400},
        ...     {'name': 'Immersive Armors', 'status': 'active', 'size_mb': 156},
        ...     {'name': 'Lux Via', 'status': 'update', 'size_mb': 89},
        ... ]
        >>> create_mods_preview(
        ...     mods=mods_data,
        ...     on_view_all=lambda: print("Viewing all mods"),
        ...     on_mod_click=lambda name: print(f"Clicked: {name}"),
        ... )
    """
    with ui.element("div").classes("bg-[#0f0f0f] border border-[#1f2937] rounded-2xl p-6"):
        # Header con título y botón "View All"
        with ui.row().classes("items-center justify-between mb-6"):
            ui.label(title).classes("text-white font-bold text-lg")

            if on_view_all:
                create_cta_button(
                    text="View All",
                    on_click=on_view_all,
                    variant="secondary",
                ).classes("px-4 py-2 rounded-lg text-sm")

        # Lista de mods o mensaje vacío
        if mods:
            for mod in mods:
                # Convertir tamaño a GB si es necesario
                size_mb = mod.get("size_mb", 0)
                size = f"{size_mb / 1024:.1f} GB" if size_mb > 1024 else f"{size_mb} MB"

                # Crear item del mod
                mod_name = mod.get("name", "Unknown")
                create_mod_list_item(
                    name=mod_name,
                    status=mod.get("status", "inactive"),
                    size=size,
                    on_click=lambda m=mod_name: on_mod_click(m) if on_mod_click else None,
                )
        else:
            ui.label(empty_message).classes("text-[#6b7280] text-center py-4")
