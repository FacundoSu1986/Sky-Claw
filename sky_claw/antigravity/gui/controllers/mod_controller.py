"""ModController — gestión del ciclo de vida de mods y detección de conflictos.

RESTRICCIÓN: CERO NiceGUI. Solo manipula AppState y EventBus.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sky_claw.antigravity.gui.event_bus import EventBus, EventType, SkyClawEvent

if TYPE_CHECKING:
    from sky_claw.antigravity.gui.models.app_state import AppState

_logger = logging.getLogger("SkyClaw.ModController")


class ModController:
    """
    Responde a eventos de instalación y conflictos de mods.
    Expone callbacks para la vista (selección, navegación a lista).

    Dependencias inyectadas:
        app_state: Estado de dominio puro.
        event_bus: Bus de eventos Observer.
    """

    def __init__(self, app_state: AppState, event_bus: EventBus) -> None:
        self.app_state = app_state
        self.event_bus = event_bus
        event_bus.subscribe(EventType.MOD_ADDED, self.handle_mod_added)
        event_bus.subscribe(EventType.CONFLICT_DETECTED, self.handle_conflict_detected)

    # ── Public callbacks — wired to views via DI ───────────────────────────────

    def handle_view_all_mods(self) -> None:
        """Callback de la vista para navegar a la lista completa de mods."""
        _logger.info("Navegación solicitada: página de mods")
        # TODO: Implementar navegación a página de mods (Parte 5)

    def handle_mod_click(self, mod_name: str) -> None:
        """Callback de la vista cuando se selecciona un mod."""
        _logger.info("Mod seleccionado: %s", mod_name)
        # TODO: Implementar vista de detalle del mod (Parte 5)

    # ── EventBus subscribers ───────────────────────────────────────────────────

    def handle_mod_added(self, event: SkyClawEvent) -> None:
        """Reacciona al evento MOD_ADDED desde el daemon."""
        _logger.info("Mod añadido al sistema: %s", event.data.get("name"))

    def handle_conflict_detected(self, event: SkyClawEvent) -> None:
        """Reacciona al evento CONFLICT_DETECTED desde el daemon."""
        _logger.warning("Conflicto detectado: %s", event.data.get("description", "desconocido"))
