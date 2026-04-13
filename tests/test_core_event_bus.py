"""Tests para CoreEventBus y la integración con TelemetryDaemon.

Sprint 1: Strangler Fig — validación del bus de eventos core.
"""

from __future__ import annotations

import asyncio
import pytest

from sky_claw.core.event_bus import CoreEventBus, Event


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
        await asyncio.sleep(0.05)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].payload == {"key": "value"}
    assert received[0].topic == "test.topic"


@pytest.mark.asyncio
async def test_wildcard_subscription() -> None:
    """Patrón wildcard matchea tópicos del mismo nivel, no otros."""
    bus = CoreEventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("system.telemetry.*", handler)
    await bus.start()
    try:
        await bus.publish(Event(topic="system.telemetry.metrics", payload={"a": 1}))
        await bus.publish(Event(topic="system.telemetry.health", payload={"b": 2}))
        await bus.publish(Event(topic="system.other", payload={"c": 3}))
        await asyncio.sleep(0.05)
    finally:
        await bus.stop()

    assert len(received) == 2
    topics = {e.topic for e in received}
    assert topics == {"system.telemetry.metrics", "system.telemetry.health"}


@pytest.mark.asyncio
async def test_faulty_subscriber_does_not_block_others() -> None:
    """Un subscriber que lanza excepción no impide que otros reciban el evento."""
    bus = CoreEventBus()
    received: list[Event] = []

    async def faulty_handler(event: Event) -> None:
        raise RuntimeError("boom")

    async def good_handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test.topic", faulty_handler)
    bus.subscribe("test.topic", good_handler)
    await bus.start()
    try:
        await bus.publish(Event(topic="test.topic", payload={}))
        await asyncio.sleep(0.05)
    finally:
        await bus.stop()

    assert len(received) == 1


@pytest.mark.asyncio
async def test_unsubscribe_removes_callback() -> None:
    """Callback desuscrito no recibe eventos posteriores."""
    bus = CoreEventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test.topic", handler)
    bus.unsubscribe("test.topic", handler)
    await bus.start()
    try:
        await bus.publish(Event(topic="test.topic", payload={}))
        await asyncio.sleep(0.05)
    finally:
        await bus.stop()

    assert len(received) == 0


@pytest.mark.asyncio
async def test_publish_no_subscribers_no_error() -> None:
    """Publicar sin suscriptores no lanza excepción."""
    bus = CoreEventBus()
    await bus.start()
    try:
        await bus.publish(Event(topic="orphan.topic", payload={"data": True}))
        await asyncio.sleep(0.05)
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_separate_instances_isolated() -> None:
    """Dos instancias de CoreEventBus son independientes."""
    bus_a = CoreEventBus()
    bus_b = CoreEventBus()
    received_a: list[Event] = []
    received_b: list[Event] = []

    async def handler_a(event: Event) -> None:
        received_a.append(event)

    async def handler_b(event: Event) -> None:
        received_b.append(event)

    bus_a.subscribe("topic", handler_a)
    bus_b.subscribe("topic", handler_b)
    await bus_a.start()
    await bus_b.start()
    try:
        await bus_a.publish(Event(topic="topic", payload={"from": "a"}))
        await asyncio.sleep(0.05)
    finally:
        await bus_a.stop()
        await bus_b.stop()

    assert len(received_a) == 1
    assert len(received_b) == 0


def test_event_timestamp_auto_populated() -> None:
    """timestamp_ms se autogenera como epoch positivo."""
    import time

    before = int(time.time() * 1000)
    event = Event(topic="t", payload={})
    after = int(time.time() * 1000)

    assert before <= event.timestamp_ms <= after


# ---------------------------------------------------------------------------
# Integración TelemetryDaemon → CoreEventBus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_daemon_publishes_to_bus() -> None:
    """TelemetryDaemon publica métricas reales al CoreEventBus."""
    from sky_claw.orchestrator.telemetry_daemon import TelemetryDaemon

    bus = CoreEventBus()
    received: list[Event] = []

    async def capture(event: Event) -> None:
        received.append(event)

    bus.subscribe("system.telemetry.*", capture)
    daemon = TelemetryDaemon(event_bus=bus, interval=0.1)

    await bus.start()
    await daemon.start()
    try:
        await asyncio.sleep(0.35)
    finally:
        await daemon.stop()
        await bus.stop()

    assert len(received) >= 1
    event = received[0]
    assert event.topic == "system.telemetry.metrics"
    assert event.source == "telemetry-daemon"
    assert "cpu" in event.payload
    assert "ram_mb" in event.payload
    assert "ram_percent" in event.payload
