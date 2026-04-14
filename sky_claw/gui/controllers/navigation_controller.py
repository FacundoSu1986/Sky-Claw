"""NavigationController — gestión de navegación entre secciones y acciones CTA.

RESTRICCIÓN: CERO NiceGUI. Solo manipula AppState.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.gui.event_bus import EventBus
    from sky_claw.gui.models.app_state import AppState

_logger = logging.getLogger("SkyClaw.NavigationController")


class NavigationController:
    """
    Gestiona cambios de sección, acciones CTA y clics en feature cards.

    Dependencias inyectadas:
        app_state: Estado de dominio puro.
        event_bus: Bus de eventos Observer (reservado para navegación futura).
    """

    def __init__(self, app_state: AppState, event_bus: EventBus) -> None:
        self.app_state = app_state
        self.event_bus = event_bus

    # ── Public callbacks — wired to views via DI ───────────────────────────────

    def handle_navigation(self, section: str) -> None:
        """Callback del sidebar para cambiar de sección."""
        _logger.info("Navegación a sección: %s", section)
        # TODO: Implementar cambio de sección/página (Parte 5)

    def handle_cta_primary(self) -> None:
        """Callback para la acción CTA principal."""
        _logger.info("CTA primario activado")
        # TODO: Implementar acción principal (Parte 5)

    def handle_cta_secondary(self) -> None:
        """Callback para la acción CTA secundaria."""
        _logger.info("CTA secundario activado")
        # TODO: Implementar acción secundaria (Parte 5)

    def handle_feature_click(self, feature_id: str) -> None:
        """Callback cuando se hace clic en una feature card."""
        _logger.info("Feature activada: %s", feature_id)
        # TODO: Mostrar detalle de feature (Parte 5)
