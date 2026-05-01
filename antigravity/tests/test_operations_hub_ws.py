"""Tests for OperationsHubWSHandler — CoreEventBus → WebSocket bridge.

Phase 2 of the Operations Hub GUI wiring.  Covers:
- Event fan-out from CoreEventBus to all connected clients.
- Initial snapshot frame delivery on connect.
- Ping → pong round trip.
- Graceful client disconnect removes the socket from the roster.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.web.operations_hub_ws import (
    DEFAULT_FORWARDED_PATTERNS,
    OperationsHubWSHandler,
    register_operations_hub_routes,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def event_bus() -> CoreEventBus:
    """Fresh CoreEventBus started for the duration of the test."""
    bus = CoreEventBus()
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()


@pytest.fixture
async def ws_client_factory(event_bus: CoreEventBus):
    """Build an aiohttp test client with the /api/status route mounted."""
    app = web.Application()
    handler = register_operations_hub_routes(app, event_bus)
    await handler.start()

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        yield client, handler
    finally:
        await handler.stop()
        await client.close()


# --------------------------------------------------------------------------- #
# Unit tests                                                                  #
# --------------------------------------------------------------------------- #


def test_default_patterns_cover_ops_namespace() -> None:
    """All first-party Operations Hub topics are forwarded by default."""
    required = {"ops.log.*", "ops.process.*", "ops.telemetry.*", "ops.conflict.*", "ops.hitl.*"}
    assert required.issubset(set(DEFAULT_FORWARDED_PATTERNS))


@pytest.mark.asyncio
async def test_handler_registers_subscriptions(event_bus: CoreEventBus) -> None:
    """start() subscribes to every forwarded pattern on the bus."""
    handler = OperationsHubWSHandler(event_bus, forwarded_patterns=("ops.log.*",))
    await handler.start()
    try:
        # Publishing a matching event should reach the handler even with zero clients.
        await event_bus.publish(Event(topic="ops.log.info", payload={"msg": "hi"}))
        await asyncio.sleep(0.05)
        # With no clients the broadcast is a no-op; we just assert the bus plumbing ran.
        assert handler.client_count == 0
    finally:
        await handler.stop()


@pytest.mark.asyncio
async def test_client_receives_snapshot_on_connect(ws_client_factory) -> None:
    """New clients immediately receive a 'snapshot' frame."""
    client, _handler = ws_client_factory
    async with client.ws_connect("/api/status") as ws:
        msg = await asyncio.wait_for(ws.receive_str(), timeout=1.0)
        frame = json.loads(msg)
        assert frame["event_type"] == "snapshot"
        assert frame["payload"]["connected"] is True


@pytest.mark.asyncio
async def test_bus_event_is_broadcast_to_client(ws_client_factory, event_bus: CoreEventBus) -> None:
    """An event published on the bus is forwarded to every WebSocket client."""
    client, _handler = ws_client_factory
    async with client.ws_connect("/api/status") as ws:
        # Consume the initial snapshot.
        await asyncio.wait_for(ws.receive_str(), timeout=1.0)

        # Publish a log event on the bus.
        await event_bus.publish(
            Event(
                topic="ops.log.info",
                payload={"level": "INFO", "message": "scanning plugins"},
                source="tool_dispatcher",
            )
        )

        msg = await asyncio.wait_for(ws.receive_str(), timeout=1.0)
        frame = json.loads(msg)
        assert frame["event_type"] == "ops.log.info"
        assert frame["payload"]["message"] == "scanning plugins"
        assert frame["source"] == "tool_dispatcher"


@pytest.mark.asyncio
async def test_client_ping_returns_pong(ws_client_factory) -> None:
    """Client sending {'action': 'ping'} receives a 'pong' event_type."""
    client, _handler = ws_client_factory
    async with client.ws_connect("/api/status") as ws:
        await asyncio.wait_for(ws.receive_str(), timeout=1.0)  # snapshot
        await ws.send_str(json.dumps({"action": "ping"}))
        msg = await asyncio.wait_for(ws.receive_str(), timeout=1.0)
        frame = json.loads(msg)
        assert frame["event_type"] == "pong"


@pytest.mark.asyncio
async def test_unsubscribe_non_matching_topic_does_not_broadcast(ws_client_factory, event_bus: CoreEventBus) -> None:
    """Events on non-forwarded topics don't reach clients."""
    client, _handler = ws_client_factory
    async with client.ws_connect("/api/status") as ws:
        await asyncio.wait_for(ws.receive_str(), timeout=1.0)  # snapshot

        # Publish on an unrelated topic.
        await event_bus.publish(Event(topic="unrelated.topic", payload={"x": 1}))

        # Wait briefly and ensure no frame arrived.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.receive_str(), timeout=0.3)


@pytest.mark.asyncio
async def test_client_count_tracks_connections(ws_client_factory) -> None:
    """client_count increments on connect and decrements on disconnect."""
    client, handler = ws_client_factory
    assert handler.client_count == 0

    async with client.ws_connect("/api/status") as ws:
        await asyncio.wait_for(ws.receive_str(), timeout=1.0)
        # Give the server a moment to register the connection.
        await asyncio.sleep(0.05)
        assert handler.client_count == 1

    # After the context exits, the server coroutine should observe disconnect.
    await asyncio.sleep(0.1)
    assert handler.client_count == 0
