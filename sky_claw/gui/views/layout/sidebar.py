"""Componente de sidebar.

Barra lateral de navegación con estado de conexión del agente.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from collections.abc import Callable

from nicegui import ui

# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "accent_violet": "#8b5cf6",
    "accent_cyan": "#06b6d4",
}


def create_sidebar(
    on_navigate: Callable[[str], None] | None = None,
    is_agent_connected: bool = False,
    nav_items: list[tuple[str, bool]] | None = None,
) -> ui.element:
    """Crea el sidebar con navegación y estado de conexión.

    Args:
        on_navigate: Callback de navegación, recibe el nombre de la sección
        is_agent_connected: True si el agente está conectado
        nav_items: Lista de tuplas (nombre_item, está_activo).
                   Por defecto: [('Dashboard', True), ('Mods', False), ...]

    Returns:
        ui.element: El elemento contenedor del sidebar
    """
    if nav_items is None:
        nav_items = [
            ("Dashboard", True),
            ("Mods", False),
            ("Conflicts", False),
            ("Downloads", False),
            ("Settings", False),
        ]

    status_text = "Connected" if is_agent_connected else "Disconnected"
    status_dot_class = "sky-connection-dot--connected" if is_agent_connected else ""

    with ui.element("div").classes(
        "w-64 h-screen bg-[#0a0a0a] border-r border-[#1f2937] flex flex-col sky-sidebar"
    ) as sidebar:
        # Logo
        with (
            ui.element("div").classes("p-6 border-b border-[#1f2937]"),
            ui.row().classes("items-center gap-3"),
        ):
            ui.html(f"""
                    <div class="w-10 h-10 rounded-xl flex items-center justify-center sky-glow-static"
                         style="background: linear-gradient(135deg,
                                {COLORS["accent_violet"]}, {COLORS["accent_cyan"]});">
                        <svg width="24" height="24" viewBox="0 0 24 24"
                             fill="none" stroke="white" stroke-width="2">
                            <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                            <path d="M2 17l10 5 10-5"/>
                            <path d="M2 12l10 5 10-5"/>
                        </svg>
                    </div>
                """)
            with ui.column():
                ui.label("Sky-Claw").classes("text-white font-bold text-lg")
                # Connection indicator
                with ui.row().classes("items-center gap-1"):
                    ui.html(
                        f'<span class="sky-connection-dot {status_dot_class}"></span>'
                    )
                    ui.label(status_text).classes("text-[#6b7280] text-xs")

        # Navigation
        with ui.element("div").classes("flex-1 p-4"):
            ui.label("NAVIGATION").classes(
                "text-[#6b7280] text-xs font-semibold tracking-wider mb-4 px-4"
            )
            for text, active in nav_items:
                active_class = (
                    "bg-[#8b5cf6]/10 border-l-2 border-[#8b5cf6]" if active else ""
                )
                text_class = (
                    "text-white" if active else "text-[#9ca3af] hover:text-white"
                )
                with (
                    ui.button()
                    .classes(
                        f"w-full flex items-center gap-3 px-4 py-3 rounded-xl "
                        f"text-left transition-all duration-200 hover:bg-[#1f2937] "
                        f"{active_class}"
                    )
                    .props("ripple flat") as nav_btn
                ):
                    ui.label(text).classes(f"font-medium {text_class}")

                    if on_navigate:
                        nav_btn.on("click", lambda t=text: on_navigate(t))

    return sidebar
