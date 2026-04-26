"""Tests para CoreEventBus y la integración con TelemetryDaemon.

Sprint 1: Strangler Fig — validación del bus de eventos core.
"""

from __future__ import annotations

import asyncio

import pytest

from sky_claw.core.event_bus import CoreEventBus, Event
from tests.polling_utils import poll_until

# ---------------------------------------------------------------------------
# CoreEventBus unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_delivers_to_exact_subscriber() -> None:
    """Suscripción exacta recibe el evento publicado."""
    bus = CoreEventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test.topic", handler)
    await bus.start()
    try:
        await bus.publish(Event(topic="test.topic", payload={"key": "value"}))
        await poll_until(lambda: len(received) == 1)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].payload == {"key": "value"}
    assert received[0].topic == "test.topic"


@pytest.mark.asyncio
async def test_wildcard_subscription() -> None:
    """Patrón fnmatch '*' matchea cualquier substring bajo el prefijo."""
    bus = CoreEventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("system.telemetry.*", handler)
    await bus.start()
    try:
        # Match — fnmatch '*' matchea cualquier substring (incluye puntos)
        await bus.publish(Event(topic="system.telemetry.cpu", payload={"v": 10}))
        await bus.publish(Event(topic="system.telemetry.cpu.core0", payload={"v": 5}))
        # No match — diferente rama
        await bus.publish(Event(topic="system.logs.error", payload={"msg": "x"}))

        await poll_until(lambda: len(received) == 2)
    finally:
        await bus.stop()

    assert len(received) == 2
    assert received[0].topic == "system.telemetry.cpu"
    assert received[1].topic == "system.telemetry.cpu.core0"


@pytest.mark.asyncio
async def test_multi_level_wildcard() -> None:
    """Patrón fnmatch '*' matchea múltiples niveles (incluye sub-tópicos)."""
    bus = CoreEventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("logs.*", handler)
    await bus.start()
    try:
        await bus.publish(Event(topic="logs.info", payload={}))
        await bus.publish(Event(topic="logs.error.critical", payload={}))
        await bus.publish(Event(topic="logs.agent.trace.detailed", payload={}))

        await poll_until(lambda: len(received) == 3)
    finally:
        await bus.stop()

    assert len(received) == 3


@pytest.mark.asyncio
async def test_multiple_subscribers_same_topic() -> None:
    """Múltiples suscriptores reciben el mismo evento."""
    bus = CoreEventBus()
    calls = {"h1": 0, "h2": 0}

    async def h1(e: Event) -> None:
        calls["h1"] += 1

    async def h2(e: Event) -> None:
        calls["h2"] += 1

    bus.subscribe("t", h1)
    bus.subscribe("t", h2)
    await bus.start()
    try:
        await bus.publish(Event(topic="t", payload={}))
        await poll_until(lambda: all(v == 1 for v in calls.values()))
    finally:
        await bus.stop()

    assert calls["h1"] == 1
    assert calls["h2"] == 1


@pytest.mark.asyncio
async def test_unsubscribe() -> None:
    """Un suscriptor deja de recibir eventos tras desuscribirse."""
    bus = CoreEventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("t", handler)
    await bus.start()
    try:
        await bus.publish(Event(topic="t", payload={"n": 1}))
        await poll_until(lambda: len(received) == 1)

        bus.unsubscribe("t", handler)
        await bus.publish(Event(topic="t", payload={"n": 2}))
        await asyncio.sleep(0.05)  # Breve espera para asegurar que NO llega
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].payload == {"n": 1}


@pytest.mark.asyncio
async def test_handler_exception_does_not_crash_bus() -> None:
    """Una excepción en un handler no impide que otros reciban el evento."""
    bus = CoreEventBus()
    received: list[Event] = []

    async def bad_handler(e: Event) -> None:
        raise RuntimeError("boom")

    async def good_handler(e: Event) -> None:
        received.append(e)

    bus.subscribe("t", bad_handler)
    bus.subscribe("t", good_handler)
    await bus.start()
    try:
        await bus.publish(Event(topic="t", payload={}))
        await poll_until(lambda: len(received) == 1)
    finally:
        await bus.stop()

    assert len(received) == 1


@pytest.mark.asyncio
async def test_publish_without_start_raises_error() -> None:
    """Publicar antes de start() lanza RuntimeError."""
    bus = CoreEventBus()
    with pytest.raises(RuntimeError, match="bus is not running"):
        await bus.publish(Event(topic="t", payload={}))


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    """Llamar a stop() múltiples veces es seguro."""
    bus = CoreEventBus()
    await bus.start()
    await bus.stop()
    await bus.stop()  # No debe lanzar nada
