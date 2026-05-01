"""Advanced panel — collapsible technical details for power users.

Shows real tool names, paths, versions, and raw logs.
Collapsed by default. Accessible via "Ajustes Avanzados" toggle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nicegui import ui

if TYPE_CHECKING:
    from sky_claw.local.discovery.environment import EnvironmentSnapshot

logger = logging.getLogger(__name__)


def build_advanced_panel(snapshot: EnvironmentSnapshot | None) -> None:
    """Build the advanced/technical panel (collapsed by default).

    Shows tool paths, versions, and environment details that
    power users might want but novices should never need.
    """
    with (
        ui.expansion(
            "⚙ AJUSTES AVANZADOS",
            icon="settings",
        )
        .classes("sky-advanced-panel w-full")
        .props("dense dark")
    ):
        if snapshot is None:
            ui.label("Escaneando el entorno...").classes("sky-text-muted")
            return

        # ── Skyrim Info ───────────────────────────────────────────────
        with ui.element("div").classes("sky-advanced-section"):
            ui.label("SKYRIM").classes("sky-advanced-title")
            if snapshot.skyrim:
                _info_row("Edición", snapshot.skyrim.edition.value)
                _info_row("Versión", snapshot.skyrim.version or "No detectada")
                _info_row("Ruta", str(snapshot.skyrim.path))
                _info_row("Tienda", snapshot.skyrim.store.upper())
            else:
                ui.label("No detectado").classes("sky-text-warning")

        ui.separator().classes("sky-separator-subtle")

        # ── MO2 Info ──────────────────────────────────────────────────
        with ui.element("div").classes("sky-advanced-section"):
            ui.label("MOD ORGANIZER 2").classes("sky-advanced-title")
            if snapshot.mo2:
                _info_row("Ruta", str(snapshot.mo2.path))
                _info_row("Perfiles", ", ".join(snapshot.mo2.profiles))
            else:
                ui.label("No detectado — usando carpeta Data directamente").classes("sky-text-warning")

        ui.separator().classes("sky-separator-subtle")

        # ── Tools Detail ──────────────────────────────────────────────
        with ui.element("div").classes("sky-advanced-section"):
            ui.label("HERRAMIENTAS DETECTADAS").classes("sky-advanced-title")

            if snapshot.tools:
                for _key, tool in snapshot.tools.items():
                    with ui.row().classes("sky-advanced-tool-row"):
                        ui.icon("check_circle", color="positive", size="1rem")
                        ui.label(tool.name).classes("sky-advanced-tool-name")
                        ui.label(str(tool.exe_path)).classes("sky-advanced-tool-path")

            if snapshot.missing:
                ui.label("HERRAMIENTAS FALTANTES").classes("sky-advanced-title q-mt-md")
                for miss in snapshot.missing:
                    with ui.row().classes("sky-advanced-tool-row"):
                        ui.icon("warning", color="warning", size="1rem")
                        ui.label(miss.name).classes("sky-advanced-tool-name")
                        ui.link("Descargar →", miss.download_url, new_tab=True).classes("sky-advanced-link")

        ui.separator().classes("sky-separator-subtle")

        # ── Health Messages ───────────────────────────────────────────
        with ui.element("div").classes("sky-advanced-section"):
            ui.label("REGISTRO DEL SISTEMA").classes("sky-advanced-title")
            for msg in snapshot.health_messages:
                ui.label(msg).classes("sky-log-entry")


def _info_row(label: str, value: str) -> None:
    """Render a key-value pair in the advanced panel."""
    with ui.row().classes("sky-info-row items-center"):
        ui.label(f"{label}:").classes("sky-info-label")
        ui.label(value).classes("sky-info-value")
