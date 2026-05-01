"""Mod list view — toggle-based mod management with Nordic styling.

Renders the list of installed mods with on/off switches, search bar,
and visual status indicators. Designed for novice users.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nicegui import ui

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sky_claw.local.mo2.vfs import MO2Controller  # noqa: F401

logger = logging.getLogger(__name__)


def build_mod_list(
    mods: list[dict[str, Any]],
    on_toggle: Callable[[str, bool], Awaitable[None]] | None = None,
    on_search: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
) -> None:
    """Build the mod list panel with toggles and search.

    Args:
        mods: List of mod dicts with keys: name, enabled, version, nexus_id (optional).
        on_toggle: Callback(mod_name, new_enabled_state).
        on_search: Callback(search_term) -> filtered mods.
    """
    # ── Header ────────────────────────────────────────────────────────
    with (
        ui.element("div").classes("sky-modlist-header"),
        ui.row().classes("items-center justify-between w-full"),
    ):
        ui.label("MODS INSTALADOS").classes("sky-section-title")
        ui.badge(str(len(mods))).classes("sky-badge-count")

    # ── Search Bar ────────────────────────────────────────────────────
    search_input = (
        ui.input(placeholder="🔍 Buscar mod...").classes("sky-modlist-search w-full").props("dense outlined dark")
    )

    # ── Mod List Container ────────────────────────────────────────────
    mod_container = ui.element("div").classes("sky-modlist-container")

    def _render_mods(mod_list: list[dict[str, Any]]) -> None:
        mod_container.clear()
        with mod_container:
            if not mod_list:
                ui.label(
                    "No hay mods instalados todavía. Arrastra un archivo .zip o .7z aquí para instalar uno."
                ).classes("sky-modlist-empty")
                return

            for mod in mod_list:
                _build_mod_row(mod, on_toggle)

    _render_mods(mods)

    # ── Search filtering ──────────────────────────────────────────────
    def _on_search_change(e: Any) -> None:
        term = e.value.strip().lower() if e.value else ""
        if not term:
            _render_mods(mods)
        else:
            filtered = [m for m in mods if term in m.get("name", "").lower()]
            _render_mods(filtered)

    search_input.on("update:model-value", _on_search_change)


def _build_mod_row(
    mod: dict[str, Any],
    on_toggle: Callable[[str, bool], Awaitable[None]] | None,
) -> None:
    """Render a single mod row with a toggle switch."""
    mod_name = mod.get("name", "Mod desconocido")
    is_enabled = mod.get("enabled", True)
    version = mod.get("version", "")

    with ui.element("div").classes("sky-mod-row" + (" sky-mod-row--disabled" if not is_enabled else "")):
        # Toggle
        switch = ui.switch(value=is_enabled).classes("sky-mod-toggle")
        if on_toggle:
            switch.on(
                "update:model-value",
                lambda e, name=mod_name: asyncio.create_task(on_toggle(name, e.value)),
            )

        # Name + version
        with ui.element("div").classes("sky-mod-info"):
            ui.label(mod_name).classes("sky-mod-name")
            if version:
                ui.label(f"v{version}").classes("sky-mod-version")

        # Status indicator
        if is_enabled:
            ui.icon("check_circle", size="1.2rem").classes("sky-mod-status-ok")
        else:
            ui.icon("remove_circle_outline", size="1.2rem").classes("sky-mod-status-off")
