"""Sección de Call-to-Action del dashboard.

Muestra una sección destacada con botones de acción principales
para guiar al usuario hacia las acciones clave.

VIEW PURO - Sin lógica de negocio, solo presentación.
Recibe callbacks como parámetros, NO ejecuta lógica directamente.
"""

from collections.abc import Callable

from nicegui import ui

from ..components import create_cta_button

# Colores del tema (extraídos del monolito para mantener invariante visual)
COLORS = {
    "accent_violet": "#8b5cf6",
    "accent_cyan": "#06b6d4",
}


def create_cta_section(
    on_primary_action: Callable,
    on_secondary_action: Callable | None = None,
    primary_text: str = "Get Started",
    secondary_text: str = "Watch Demo",
    title: str = "Ready to transform your modding experience?",
    description: str = "Sky-Claw uses advanced AI to search, install, and manage "
    "your Skyrim mods. Say goodbye to conflicts and hello to a "
    "perfectly optimized load order.",
    badge_text: str = "VERSION 2.0",
) -> None:
    """Sección de Call-to-Action con botones principales.

    Muestra una sección visualmente destacada con gradiente y efectos de blur,
    un badge de versión, título, descripción y dos botones de acción.

    Args:
        on_primary_action: Callback para el botón principal (obligatorio)
        on_secondary_action: Callback para el botón secundario (opcional)
        primary_text: Texto del botón principal (default: "Get Started")
        secondary_text: Texto del botón secundario (default: "Watch Demo")
        title: Título principal de la sección
        description: Descripción bajo el título
        badge_text: Texto del badge de versión

    Example:
        >>> def on_start():
        ...     print("Starting...")
        >>> def on_demo():
        ...     print("Opening demo...")
        >>> create_cta_section(
        ...     on_primary_action=on_start,
        ...     on_secondary_action=on_demo,
        ... )
    """
    with (
        ui.element("div")
        .classes("relative rounded-3xl p-12 overflow-hidden")
        .style(
            f"background: linear-gradient(135deg, {COLORS['accent_violet']}10, "
            f"#0f0f0f, {COLORS['accent_cyan']}10);"
            f"border: 1px solid {COLORS['accent_violet']}30;"
        )
    ):
        # Efectos de blur decorativos (background)
        ui.html("""
            <div style="position:absolute;top:0;left:25%;width:384px;
                 height:384px;background:rgba(139,92,246,0.2);
                 border-radius:50%;filter:blur(128px);
                 pointer-events:none;"></div>
            <div style="position:absolute;bottom:0;right:25%;width:384px;
                 height:384px;background:rgba(6,182,212,0.1);
                 border-radius:50%;filter:blur(128px);
                 pointer-events:none;"></div>
        """)

        with ui.column().classes("relative z-10 items-center text-center"):
            # Badge de versión
            ui.label(badge_text).classes(
                "px-4 py-1 rounded-full text-sm font-semibold mb-6 sky-badge sky-badge--violet"
            )

            # Título principal
            ui.label(title).classes("text-white text-5xl font-bold leading-tight mb-6")

            # Descripción
            ui.label(description).classes("text-[#9ca3af] text-lg max-w-2xl mb-10 leading-relaxed")

            # Botones de acción
            with ui.row().classes("items-center gap-4"):
                # Botón principal
                create_cta_button(
                    text=primary_text,
                    on_click=on_primary_action,
                    variant="primary",
                    icon_svg="""
                        <svg width="24" height="24" viewBox="0 0 24 24"
                             fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>
                        </svg>
                    """,
                )

                # Botón secundario
                if on_secondary_action:
                    create_cta_button(
                        text=secondary_text,
                        on_click=on_secondary_action,
                        variant="secondary",
                        icon_svg="""
                            <svg width="20" height="20" viewBox="0 0 24 24"
                                 fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"/>
                            </svg>
                        """,
                    )
