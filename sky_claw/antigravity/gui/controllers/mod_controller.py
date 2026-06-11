"""ModController — gestión del ciclo de vida de mods y detección de conflictos.

RESTRICCIÓN: CERO NiceGUI. Solo manipula AppState y EventBus.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sky_claw.antigravity.gui.gui_event_adapter import EventBus, EventType, SkyClawEvent

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
        """Navega a la sección de mods (mismo contrato que NavigationController).

        Publica el evento en lugar de depender de NavigationController: la
        fuente de verdad es ``NAVIGATION_REQUESTED``, no quién lo emite.
        """
        _logger.info("Navegación solicitada: sección Mods")
        self.app_state.active_section = "Mods"
        self.event_bus.publish(
            SkyClawEvent(
                type=EventType.NAVIGATION_REQUESTED,
                data={"section": "Mods"},
                source="mod_controller",
            )
        )

    def handle_mod_click(self, mod_name: str) -> None:
        """Registra la selección de un mod (sin navegar — la vista decide).

        Muta ``AppState.selected_mod`` y publica ``MOD_SELECTED``;
        ``ReactiveState`` lo refleja en el store para la futura vista de
        detalle y notifica al usuario.
        """
        _logger.info("Mod seleccionado: %s", mod_name)
        self.app_state.selected_mod = mod_name
        self.event_bus.publish(
            SkyClawEvent(
                type=EventType.MOD_SELECTED,
                data={"name": mod_name},
                source="mod_controller",
            )
        )

    # ── EventBus subscribers ───────────────────────────────────────────────────

    def handle_mod_added(self, event: SkyClawEvent) -> None:
        """Reacciona al evento MOD_ADDED desde el daemon."""
        _logger.info("Mod añadido al sistema: %s", event.data.get("name"))

    def handle_conflict_detected(self, event: SkyClawEvent) -> None:
        """Reacciona al evento CONFLICT_DETECTED desde el daemon."""
        _logger.warning("Conflicto detectado: %s", event.data.get("description", "desconocido"))
