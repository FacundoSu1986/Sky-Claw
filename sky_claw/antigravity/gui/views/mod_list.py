"""Mod list view — toggle-based mod management with Nordic styling.

Renders the list of installed mods with on/off switches, search bar,
and visual status indicators. Designed for novice users.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nicegui import ui

from sky_claw.antigravity.gui.task_tracking import create_tracked_task

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sky_claw.local.mo2.vfs import MO2Controller  # noqa: F401

logger = logging.getLogger(__name__)


def _filter_mods(mods: list[dict[str, Any]], term: str) -> list[dict[str, Any]]:
    """Filtra mods por coincidencia parcial (case-insensitive) en el nombre.

    Seam puro (sin NiceGUI) reutilizado por el buscador del header (A1) y por el
    input de la propia lista, para que ambos apliquen exactamente la misma
    semántica de filtrado. Término vacío ⇒ devuelve la lista intacta.
    """
    needle = (term or "").strip().lower()
    if not needle:
        return mods
    return [m for m in mods if needle in m.get("name", "").lower()]


def build_mod_list(
    mods: list[dict[str, Any]],
    on_toggle: Callable[[str, bool], Awaitable[None]] | None = None,
    on_search: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    initial_query: str = "",
) -> None:
    """Build the mod list panel with toggles and search.

    Args:
        mods: List of mod dicts with keys: name, enabled, version, nexus_id (optional).
        on_toggle: Callback(mod_name, new_enabled_state).
        on_search: Callback(search_term) -> filtered mods.
        initial_query: Término de búsqueda inicial (p. ej. tecleado en el buscador
            del header, A1). Pre-rellena el input y pre-filtra la lista.
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
        ui.input(placeholder="🔍 Buscar mod...", value=initial_query)
        .classes("sky-modlist-search w-full")
        .props("dense outlined dark")
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

    # Render inicial ya filtrado por el término que llega del header (A1).
    _render_mods(_filter_mods(mods, initial_query))

    # ── Search filtering ──────────────────────────────────────────────
    def _on_search_change(e: Any) -> None:
        _render_mods(_filter_mods(mods, e.value or ""))

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
                lambda e, name=mod_name: create_tracked_task(on_toggle(name, e.value), name=f"gui-toggle-{name}"),
            )
        else:
            # Sin handler el switch sería un control engañoso (NiceGUI lo
            # togglearía client-side sin persistir nada) — se muestra el
            # estado pero deshabilitado hasta que el caller cablee on_toggle.
            switch.props("disable")

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
