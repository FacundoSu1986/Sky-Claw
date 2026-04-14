"""Semantic action buttons — "The Invisible Engine" control panel.

Renders the 6 action buttons that abstract away technical tool names.
Each button shows:
- A rune icon + friendly action name (Spanish)
- Current status (available / not installed / running)
- Last execution time (if available)

Follows the Nordic/Skyrim aesthetic: parchment cards, golden accents,
stone borders.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nicegui import ui

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sky_claw.discovery.environment import EnvironmentSnapshot

logger = logging.getLogger(__name__)


# ── Rune symbols for Nordic aesthetic ─────────────────────────────────
_RUNE_SORT = "ᚠ"  # Fehu — order, wealth
_RUNE_CLEAN = "ᚱ"  # Raido — journey, cleansing
_RUNE_PATCH = "ᛞ"  # Dagaz — breakthrough, synthesis
_RUNE_ANIM = "ᛏ"  # Tiwaz — warrior, animation
_RUNE_LOD = "ᛗ"  # Mannaz — human, landscape
_RUNE_PREPARE = "ᚦ"  # Thurisaz — Thor's hammer, power


# ── Action Definitions ────────────────────────────────────────────────

_ACTION_DEFS: list[dict[str, Any]] = [
    {
        "key": "loot",
        "rune": _RUNE_SORT,
        "label": "Ordenar Mods",
        "description": "Organiza automáticamente el orden de carga para evitar conflictos",
        "technical_name": "LOOT",
        "icon": "sort",
    },
    {
        "key": "xedit",
        "rune": _RUNE_CLEAN,
        "label": "Limpiar Archivos",
        "description": "Elimina registros sucios de los plugins oficiales",
        "technical_name": "SSEEdit",
        "icon": "cleaning_services",
    },
    {
        "key": "wrye_bash",
        "rune": _RUNE_PATCH,
        "label": "Crear Parche",
        "description": "Genera un parche de compatibilidad entre tus mods",
        "technical_name": "Wrye Bash",
        "icon": "build",
    },
    {
        "key": "pandora",
        "rune": _RUNE_ANIM,
        "label": "Generar Animaciones",
        "description": "Actualiza los grafos de comportamiento de las animaciones",
        "technical_name": "Pandora",
        "icon": "animation",
    },
    {
        "key": "dyndolod",
        "rune": _RUNE_LOD,
        "label": "Optimizar Gráficos",
        "description": "Genera LODs para mejorar el rendimiento visual a distancia",
        "technical_name": "DynDOLOD",
        "icon": "landscape",
    },
]


def build_actions_panel(
    snapshot: EnvironmentSnapshot | None,
    on_action: Callable[[str], Awaitable[None]] | None = None,
    on_prepare_game: Callable[[], Awaitable[None]] | None = None,
    on_install_tool: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Build the semantic action buttons panel.

    Args:
        snapshot: Current environment scan results.
        on_action: Callback when user clicks an individual action button.
        on_prepare_game: Callback for the "Preparar Juego" master button.
        on_install_tool: Callback when user clicks "Instalar" for a missing tool.
    """
    # ── Master Button: "Preparar Juego" ───────────────────────────────
    with ui.element("div").classes("sky-action-master"):
        btn = (
            ui.button(
                f"{_RUNE_PREPARE}  PREPARAR JUEGO",
            )
            .classes("sky-action-master-btn")
            .props("unelevated no-caps")
        )
        if on_prepare_game:
            btn.on("click", lambda: asyncio.create_task(on_prepare_game()))

        ui.label("Ejecuta toda la secuencia de optimización en orden").classes(
            "sky-action-master-desc"
        )

    ui.separator().classes("sky-separator")

    # ── Individual Tool Buttons ─────────────────────────────────────
    with ui.element("div").classes("sky-actions-grid"):
        for action_def in _ACTION_DEFS:
            _build_single_action(
                action_def,
                snapshot,
                on_action,
                on_install_tool,
            )


def _build_single_action(
    action_def: dict[str, Any],
    snapshot: EnvironmentSnapshot | None,
    on_action: Callable[[str], Awaitable[None]] | None,
    on_install_tool: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Build a single action button card."""
    key = action_def["key"]
    is_available = snapshot.has_tool(key) if snapshot else False
    is_missing = not is_available

    # Card container
    card_class = "sky-action-card"
    if is_missing:
        card_class += " sky-action-card--disabled"

    with ui.element("div").classes(card_class):
        # Rune + Label row
        with ui.row().classes("sky-action-header items-center no-wrap"):
            ui.label(action_def["rune"]).classes("sky-action-rune")
            ui.label(action_def["label"]).classes("sky-action-label")

        # Description
        ui.label(action_def["description"]).classes("sky-action-desc")

        # Status badge + Button
        with ui.row().classes("sky-action-footer items-center justify-between"):
            if is_available:
                ui.badge("Disponible", color="positive").props("outline")
                btn = (
                    ui.button("Ejecutar")
                    .classes("sky-action-btn")
                    .props("unelevated dense no-caps")
                )
                if on_action:
                    btn.on("click", lambda k=key: asyncio.create_task(on_action(k)))
            else:
                ui.badge("No instalado", color="warning").props("outline")
                btn = (
                    ui.button("Instalar")
                    .classes("sky-action-btn sky-action-btn--install")
                    .props("unelevated dense no-caps")
                )
                if on_install_tool:
                    btn.on(
                        "click", lambda k=key: asyncio.create_task(on_install_tool(k))
                    )

        # Technical name (subtle)
        ui.label(action_def["technical_name"]).classes("sky-action-tech-name")
