"""Regression guard for the GUI EventBus loop-binding bug (Issue A).

The GUI ``event_bus`` (``gui_event_adapter.EventBus``) dispatches subscriber
callbacks onto an asyncio loop captured at ``start()`` time via
``asyncio.get_running_loop()``. If ``start()`` is invoked outside a running
loop, ``_loop`` stays ``None`` and the processor thread silently drops *every*
event — which killed navigation, chat rendering and the "thinking" spinner in
the frozen exe (``No hay event loop activo, descartando evento: ...``).

Root cause was the call site: ``setup_app`` invoked ``event_bus.start()``
synchronously (before ``ui.run()`` started the NiceGUI loop). The fix defers it
to ``app.on_startup(event_bus.start)`` so the loop is live when it runs.

These tests pin three properties:
1. ``start()`` inside a running loop binds the loop and actually dispatches.
2. ``start()`` without a running loop is *loud* (logs ERROR), not silent.
3. ``setup_app`` registers ``event_bus.start`` via ``app.on_startup`` instead of
   calling it eagerly.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from sky_claw.antigravity.gui.gui_event_adapter import EventBus, EventType, SkyClawEvent


@pytest.fixture
def fresh_bus():
    """Yield an isolated EventBus instance (the real one is a process singleton)."""
    saved = EventBus._instance
    EventBus._instance = None
    bus = EventBus()  # brand-new singleton, independent state
    try:
        yield bus
    finally:
        bus.stop()
        EventBus._instance = saved


async def test_start_inside_loop_binds_and_dispatches(fresh_bus):
    """Started within a running loop, the bus binds it and delivers events."""
    received: list[SkyClawEvent] = []
    fresh_bus.subscribe(EventType.NAVIGATION_REQUESTED, received.append)

    fresh_bus.start()  # we are inside the pytest-asyncio event loop

    assert fresh_bus._loop is not None, "start() inside a loop must bind _loop"

    fresh_bus.publish(SkyClawEvent(type=EventType.NAVIGATION_REQUESTED, data={"section": "Mods"}))

    # The processor thread enqueues via call_soon_threadsafe; give the loop ticks.
    for _ in range(100):
        await asyncio.sleep(0.01)
        if received:
            break

    assert len(received) == 1, "event was dropped — loop not bound / dispatch broken"
    assert received[0].data["section"] == "Mods"


def test_start_without_loop_logs_error_and_leaves_loop_none(fresh_bus, caplog):
    """Without a running loop, start() must fail *loudly* (ERROR), not silently.

    This is the defense-in-depth guard: if the eager-call regression is ever
    reintroduced, the app screams in the logs instead of degrading silently.
    """
    with caplog.at_level(logging.ERROR, logger="SkyClaw.EventBus"):
        fresh_bus.start()  # plain sync context — no running loop

    assert fresh_bus._loop is None
    assert any("loop" in r.message.lower() for r in caplog.records), "start() without a loop must log an ERROR"


def test_setup_app_defers_event_bus_start_to_on_startup(monkeypatch):
    """``setup_app`` must register ``event_bus.start`` via ``app.on_startup``.

    Pre-fix, setup_app called ``event_bus.start()`` eagerly (outside the loop) —
    this test fails against that and passes once it is deferred.
    """
    import sky_claw.antigravity.gui.sky_claw_gui as gui

    fake_app = MagicMock(name="nicegui.app")
    monkeypatch.setattr(gui, "app", fake_app)
    monkeypatch.setattr(gui, "get_app_state_instance", lambda: MagicMock(name="AppState"))
    monkeypatch.setattr(gui, "get_store", lambda: MagicMock(name="ReactiveStore"))

    # Keep the process-wide singleton's subscriber list clean across the suite.
    saved_subs = dict(gui.event_bus._subscribers)
    try:
        gui.setup_app()

        registered = [call.args[0] for call in fake_app.on_startup.call_args_list if call.args]
        assert gui.event_bus.start in registered, (
            "setup_app must defer event_bus.start() to app.on_startup, not call it eagerly"
        )
    finally:
        gui.event_bus._subscribers.clear()
        gui.event_bus._subscribers.update(saved_subs)
        gui.event_bus.stop()
