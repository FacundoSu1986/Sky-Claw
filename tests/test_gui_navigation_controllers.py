"""PR-5 (obs #216): GUI navigation/CTA/feature callbacks must act, not stub.

Controllers are NiceGUI-free by contract ("CERO NiceGUI"): they mutate the pure
``AppState`` and publish ``SkyClawEvent``s; the ReactiveState viewmodel
subscribes and updates the store, which re-renders ``main_page``. These tests
exercise the controllers against that contract with a recorder bus (the real
``EventBus`` is a process-wide singleton — unsuitable for isolated units).
"""

from __future__ import annotations

from pathlib import Path

from sky_claw.antigravity.gui.controllers.mod_controller import ModController
from sky_claw.antigravity.gui.controllers.navigation_controller import (
    CTA_PRIMARY_SECTION,
    CTA_SECONDARY_SECTION,
    FEATURE_SECTIONS,
    NavigationController,
)
from sky_claw.antigravity.gui.gui_event_adapter import EventType
from sky_claw.antigravity.gui.models.app_state import NAV_SECTIONS, AppState


class _RecorderBus:
    """Minimal stand-in for the EventBus singleton: records published events."""

    def __init__(self) -> None:
        self.published = []

    def publish(self, event) -> None:
        self.published.append(event)

    def subscribe(self, *_args, **_kwargs) -> None:  # ModController subscribes on init
        return None


def _make_nav() -> tuple[NavigationController, AppState, _RecorderBus]:
    state = AppState(config_path=Path("test-config.json"))
    bus = _RecorderBus()
    return NavigationController(app_state=state, event_bus=bus), state, bus


def _make_mod() -> tuple[ModController, AppState, _RecorderBus]:
    state = AppState(config_path=Path("test-config.json"))
    bus = _RecorderBus()
    return ModController(app_state=state, event_bus=bus), state, bus


# ── NavigationController ───────────────────────────────────────────────────────


def test_handle_navigation_updates_state_and_publishes():
    nav, state, bus = _make_nav()
    nav.handle_navigation("Mods")

    assert state.active_section == "Mods"
    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.type is EventType.NAVIGATION_REQUESTED
    assert event.data["section"] == "Mods"


def test_handle_navigation_rejects_unknown_section():
    nav, state, bus = _make_nav()
    nav.handle_navigation("NotASection")

    assert state.active_section == "Dashboard"  # unchanged default
    assert bus.published == []


def test_cta_primary_navigates_to_mods():
    nav, state, bus = _make_nav()
    nav.handle_cta_primary()
    assert CTA_PRIMARY_SECTION == "Mods"
    assert state.active_section == CTA_PRIMARY_SECTION
    assert bus.published[0].data["section"] == CTA_PRIMARY_SECTION


def test_cta_secondary_navigates_to_conflicts():
    nav, state, bus = _make_nav()
    nav.handle_cta_secondary()
    assert CTA_SECONDARY_SECTION == "Conflicts"
    assert state.active_section == CTA_SECONDARY_SECTION


def test_feature_click_maps_every_feature_to_a_known_section():
    # The mapping itself must stay consistent with the canonical sections.
    assert set(FEATURE_SECTIONS.values()) <= set(NAV_SECTIONS)

    for feature_id, target in FEATURE_SECTIONS.items():
        nav, state, bus = _make_nav()
        nav.handle_feature_click(feature_id)
        assert state.active_section == target
        assert bus.published[0].data["section"] == target


def test_feature_click_unknown_id_is_noop():
    nav, state, bus = _make_nav()
    nav.handle_feature_click("Unknown Feature")
    assert state.active_section == "Dashboard"
    assert bus.published == []


# ── ModController ──────────────────────────────────────────────────────────────


def test_view_all_mods_navigates_to_mods_section():
    mod, state, bus = _make_mod()
    mod.handle_view_all_mods()

    assert state.active_section == "Mods"
    assert bus.published[0].type is EventType.NAVIGATION_REQUESTED
    assert bus.published[0].data["section"] == "Mods"


def test_dashboard_section_dispatch_data_is_consistent():
    """The content dispatcher's placeholder set and the mods adapter must stay
    aligned with the canonical sections / build_mod_list contract."""
    from sky_claw.antigravity.gui.views.pages.dashboard_page import (
        _PLACEHOLDER_SECTIONS,
        mods_for_list,
    )

    # Every placeholder is a real sidebar section; Dashboard/Mods render content.
    assert set(_PLACEHOLDER_SECTIONS) <= set(NAV_SECTIONS)
    assert set(NAV_SECTIONS) - set(_PLACEHOLDER_SECTIONS) == {"Dashboard", "Mods"}

    # Dashboard mods (status-based) map to build_mod_list's enabled-based rows.
    rows = mods_for_list(
        [
            {"name": "Ordinator", "status": "conflict", "size_mb": 45},
            {"name": "Lux Via", "status": "inactive", "version": "1.5"},
            {},
        ]
    )
    assert rows[0] == {"name": "Ordinator", "enabled": True, "version": ""}
    assert rows[1] == {"name": "Lux Via", "enabled": False, "version": "1.5"}
    assert rows[2]["name"] == "Mod desconocido"


def test_mod_click_selects_mod_without_navigating():
    mod, state, bus = _make_mod()
    mod.handle_mod_click("Ordinator")

    assert state.selected_mod == "Ordinator"
    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.type is EventType.MOD_SELECTED
    assert event.data["name"] == "Ordinator"
    # Selection is not navigation — the view decides how to present it.
    assert state.active_section == "Dashboard"
