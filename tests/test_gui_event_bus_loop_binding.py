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
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.gui.gui_event_adapter import EventBus, EventType, SkyClawEvent


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
    # _running must stay False so a later start() (with a loop) can still bind —
    # otherwise an early/eager call would permanently wedge the bus.
    assert fresh_bus._running is False
    assert any("loop" in r.message.lower() for r in caplog.records), "start() without a loop must log an ERROR"


async def test_start_recovers_after_a_loopless_start(fresh_bus):
    """A failed (loopless) start() must not wedge the bus: a later start() binds.

    Guards the defense-in-depth gap — if start() marked itself running before
    capturing the loop, the correct on_startup call would early-return and the
    bus would drop events forever.
    """
    # First call happens via a thread with no running loop → must be a no-op.
    import threading

    def _loopless_start() -> None:
        fresh_bus.start()

    t = threading.Thread(target=_loopless_start)
    t.start()
    t.join()
    assert fresh_bus._loop is None and fresh_bus._running is False

    # Now start() from inside the live loop must succeed and dispatch.
    received: list[SkyClawEvent] = []
    fresh_bus.subscribe(EventType.NAVIGATION_REQUESTED, received.append)
    fresh_bus.start()
    assert fresh_bus._loop is not None and fresh_bus._running is True

    fresh_bus.publish(SkyClawEvent(type=EventType.NAVIGATION_REQUESTED, data={"section": "Conflicts"}))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if received:
            break
    assert len(received) == 1


def test_setup_app_defers_event_bus_start_to_on_startup(monkeypatch):
    """``setup_app`` must register ``event_bus.start`` via ``app.on_startup``.

    Pre-fix, setup_app called ``event_bus.start()`` eagerly (outside the loop) —
    this test fails against that and passes once it is deferred.
    """
    import sky_claw.app.gui.sky_claw_gui as gui

    fake_app = MagicMock(name="nicegui.app")
    monkeypatch.setattr(gui, "app", fake_app)
    monkeypatch.setattr(gui, "get_app_state_instance", lambda: MagicMock(name="AppState"))
    monkeypatch.setattr(gui, "get_store", lambda: MagicMock(name="ReactiveStore"))

    # Keep the process-wide singleton's subscriber list clean across the suite.
    # Deep-copy the inner lists: setup_app appends to them, and a shallow dict
    # copy would share those list objects, so the restore below would be a no-op.
    saved_subs = {key: list(callbacks) for key, callbacks in gui.event_bus._subscribers.items()}
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


async def test_setup_app_cierra_el_database_agent_de_la_ui(monkeypatch) -> None:
    """El shutdown debe cerrar y soltar la conexión SQLite propia de la GUI."""
    import sky_claw.app.gui.sky_claw_gui as gui

    db_agent = MagicMock(name="DatabaseAgent")
    db_agent.close = AsyncMock()

    monkeypatch.setattr(gui, "_db_agent", db_agent)

    await gui.close_db_agent()

    db_agent.close.assert_awaited_once()
    assert gui._db_agent is None


async def test_cleanup_no_propaga_si_falla_el_cliente(monkeypatch, caplog) -> None:
    """Un fallo en un paso de _cleanup no debe abortar la cadena ni propagar la excepción.

    Este cambio de aserción (antes afirmaba que propagaba RuntimeError) es deliberado
    y es el corazón del fix D1: un handler de on_shutdown nunca puede lanzar, o
    NiceGUI corta su bucle de apagado sin correr los handlers siguientes.
    """
    import sky_claw.app.gui.sky_claw_gui as gui

    fake_app = MagicMock(name="nicegui.app")
    client = MagicMock(name="AgentClient")
    client.stop = AsyncMock(side_effect=RuntimeError("fallo del cliente"))

    monkeypatch.setattr(gui, "app", fake_app)
    monkeypatch.setattr(gui, "event_bus", MagicMock(name="event_bus"))
    monkeypatch.setattr(gui, "agent_client", client)
    monkeypatch.setattr(gui, "get_app_state_instance", lambda: MagicMock(name="AppState"))
    monkeypatch.setattr(gui, "get_store", lambda: MagicMock(name="ReactiveStore"))

    gui.setup_app()
    handler = fake_app.on_shutdown.call_args_list[0].args[0]

    with caplog.at_level(logging.ERROR):
        await handler()

    client.stop.assert_awaited_once()
    assert "fallo del cliente" in caplog.text


async def test_setup_app_no_aborta_shutdown_si_falla_database_close(monkeypatch, caplog) -> None:
    """Un cierre SQLite fallido conserva ownership sin bloquear handlers posteriores."""
    import sky_claw.app.gui.sky_claw_gui as gui

    db_agent = MagicMock(name="DatabaseAgent")
    db_agent.close = AsyncMock(side_effect=RuntimeError("fallo de cierre SQLite"))

    monkeypatch.setattr(gui, "_db_agent", db_agent)

    with caplog.at_level(logging.ERROR):
        await gui.close_db_agent()

    db_agent.close.assert_awaited_once()
    assert gui._db_agent is db_agent
    assert "No se pudo cerrar DatabaseAgent de la UI" in caplog.text


def _cleanup_handler(gui, monkeypatch, *, client=None) -> Any:
    """Registra ``setup_app`` sobre un ``app`` falso y devuelve su handler de shutdown."""
    fake_app = MagicMock(name="nicegui.app")
    monkeypatch.setattr(gui, "app", fake_app)
    monkeypatch.setattr(gui, "event_bus", MagicMock(name="event_bus"))
    monkeypatch.setattr(gui, "agent_client", client)
    monkeypatch.setattr(gui, "get_app_state_instance", lambda: MagicMock(name="AppState"))
    monkeypatch.setattr(gui, "get_store", lambda: MagicMock(name="ReactiveStore"))

    gui.setup_app()
    handlers = [call.args[0] for call in fake_app.on_shutdown.call_args_list if call.args]
    assert len(handlers) == 1
    return handlers[0]


async def test_cleanup_no_propaga_si_falla_el_event_bus(monkeypatch, caplog) -> None:
    """Un ``event_bus.stop()`` roto tampoco puede truncar la cadena de handlers.

    Y el paso que falla no puede saltear los siguientes: el cliente igual se para.
    """
    import sky_claw.app.gui.sky_claw_gui as gui

    client = MagicMock(name="AgentClient")
    client.stop = AsyncMock()

    handler = _cleanup_handler(gui, monkeypatch, client=client)

    bus = MagicMock(name="event_bus")
    bus.stop.side_effect = RuntimeError("fallo del bus")
    monkeypatch.setattr(gui, "event_bus", bus)

    with caplog.at_level(logging.ERROR):
        await handler()  # no debe lanzar

    client.stop.assert_awaited_once()
    assert "fallo del bus" in caplog.text


async def test_cleanup_no_cierra_el_database_agent(monkeypatch) -> None:
    """El cierre de la DB se movió a ``_teardown_runtime``, después de ``ctx.stop()``.

    Cerrarla en ``_cleanup`` (handler #1) dejaba viva la conexión del
    ``SupervisorAgent`` sobre el MISMO ``sky_claw_state.db``, así que SQLite no
    borraba los sidecars y ``shutdown_all`` avisaba "checkpoint incompleto" en
    cada apagado limpio.
    """
    import sky_claw.app.gui.sky_claw_gui as gui

    db_agent = MagicMock(name="DatabaseAgent")
    db_agent.close = AsyncMock()

    handler = _cleanup_handler(gui, monkeypatch)
    monkeypatch.setattr(gui, "_db_agent", db_agent)

    await handler()

    db_agent.close.assert_not_awaited()
    assert gui._db_agent is db_agent


async def test_close_db_agent_sin_agente_es_un_noop(monkeypatch) -> None:
    """Si la GUI nunca abrió su DB, el paso de teardown no debe hacer nada."""
    import sky_claw.app.gui.sky_claw_gui as gui

    monkeypatch.setattr(gui, "_db_agent", None)

    await gui.close_db_agent()

    assert gui._db_agent is None


async def test_cleanup_acalla_los_productores_antes_de_drenar(monkeypatch) -> None:
    """Codex P1 en #371: hay que quiesce a los productores ANTES de la foto del drenado.

    ``ReactiveState.handle_conflict_detected`` crea ``gui-conflicts-refresh``
    (que toca la DB). Si el drenado saca su foto de ``_BACKGROUND_TASKS`` antes
    de parar el ``event_bus`` y el cliente, un evento entregado en esa ventana
    crea una task que el drenado nunca vio: ``_cleanup`` retorna,
    ``_teardown_runtime`` cierra la DB y vuelve la pérdida de escrituras que
    este cambio venía a arreglar.
    """
    import sky_claw.app.gui.sky_claw_gui as gui

    orden: list[str] = []

    async def _drain(*_args, **_kwargs) -> None:
        orden.append("drain")

    async def _client_stop() -> None:
        orden.append("client.stop")

    client = MagicMock(name="AgentClient")
    client.stop = _client_stop

    handler = _cleanup_handler(gui, monkeypatch, client=client)

    bus = MagicMock(name="event_bus")
    bus.stop.side_effect = lambda: orden.append("event_bus.stop")
    monkeypatch.setattr(gui, "event_bus", bus)
    monkeypatch.setattr(gui, "drain_tracked_tasks", _drain)

    await handler()

    assert orden == ["client.stop", "event_bus.stop", "drain"], (
        "el drenado sacó su foto antes de acallar a los productores: un evento "
        f"en esa ventana crea una task que nadie espera. Orden real: {orden}"
    )
