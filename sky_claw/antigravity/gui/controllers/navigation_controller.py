"""NavigationController — gestión de navegación entre secciones y acciones CTA.

RESTRICCIÓN: CERO NiceGUI. Solo manipula AppState.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sky_claw.antigravity.gui.gui_event_adapter import EventType, SkyClawEvent
from sky_claw.antigravity.gui.models.app_state import NAV_SECTIONS

if TYPE_CHECKING:
    from sky_claw.antigravity.gui.gui_event_adapter import EventBus
    from sky_claw.antigravity.gui.models.app_state import AppState

_logger = logging.getLogger("SkyClaw.NavigationController")

#: Destinos de las CTA del dashboard. El copy del CTA ancla la semántica:
#: "Get Started" → gestionar mods; "Watch Demo" promete "say goodbye to
#: conflicts" → muestra el área de conflictos.
CTA_PRIMARY_SECTION = "Mods"
CTA_SECONDARY_SECTION = "Conflicts"

#: Feature cards (features_section.py) → sección que las materializa.
FEATURE_SECTIONS: dict[str, str] = {
    "Smart Search": "Mods",
    "Conflict Resolution": "Conflicts",
    "Zero-Trust Security": "Settings",
}


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
        """Cambia la sección activa y lo anuncia al viewmodel.

        Muta ``AppState.active_section`` (dominio puro) y publica
        ``NAVIGATION_REQUESTED``; ``ReactiveState`` lo traduce a
        ``store.set("active_section", ...)`` que re-renderiza ``main_page``.
        Secciones desconocidas se ignoran con warning (fail-safe: un typo en
        una vista no puede dejar la UI en un estado imposible).
        """
        if section not in NAV_SECTIONS:
            _logger.warning("Sección de navegación desconocida (ignorada): %r", section)
            return
        _logger.info("Navegación a sección: %s", section)
        self.app_state.active_section = section
        self.event_bus.publish(
            SkyClawEvent(
                type=EventType.NAVIGATION_REQUESTED,
                data={"section": section},
                source="navigation_controller",
            )
        )

    def handle_cta_primary(self) -> None:
        """CTA "Get Started" → gestión de mods (la acción central de la app)."""
        _logger.info("CTA primario activado → %s", CTA_PRIMARY_SECTION)
        self.handle_navigation(CTA_PRIMARY_SECTION)

    def handle_cta_secondary(self) -> None:
        """CTA "Watch Demo" → área de conflictos (el diferenciador del copy)."""
        _logger.info("CTA secundario activado → %s", CTA_SECONDARY_SECTION)
        self.handle_navigation(CTA_SECONDARY_SECTION)

    def handle_feature_click(self, feature_id: str) -> None:
        """Lleva la feature card a la sección que la materializa."""
        target = FEATURE_SECTIONS.get(feature_id)
        if target is None:
            _logger.warning("Feature card sin sección mapeada (ignorada): %r", feature_id)
            return
        _logger.info("Feature activada: %s → %s", feature_id, target)
        self.handle_navigation(target)
