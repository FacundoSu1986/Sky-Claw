"""Reactive state primitives for the NiceGUI Forge interface.

Exposes ``ReactiveStore``, a single source of truth for cross-component
state with explicit subscribe/notify semantics designed to integrate
with NiceGUI's ``@ui.refreshable`` decorator.
"""

from __future__ import annotations

from sky_claw.antigravity.gui.state.reactive_store import (
    ReactiveStore,
    bind_label,
    get_store,
    reset_store_for_tests,
)

__all__ = [
    "ReactiveStore",
    "bind_label",
    "get_store",
    "reset_store_for_tests",
]
