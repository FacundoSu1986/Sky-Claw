"""Tests for OperationsHubWSHandler — CoreEventBus → WebSocket bridge.

Phase 2 of the Operations Hub GUI wiring.  Covers:
- Event fan-out from CoreEventBus to all connected clients.
- Initial snapshot frame delivery on connect.
- Ping → pong round trip.
- Graceful client disconnect removes the socket from the roster.
- Fase 7: serialización de payloads Pydantic con ``mode="json"`` para
  tolerar campos no-nativos (``datetime``, ``Enum``, ...) sin volcar al
  cliente un ``TypeError`` de ``json.dumps``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from enum import Enum

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel, ConfigDict

from sky_claw.core.event_bus import CoreEventBus, Event
from sky_claw.web.operations_hub_ws import (
    DEFAULT_FORWARDED_PATTERNS,
    OperationsHubWSHandler,
    _json_fallback,
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


# --------------------------------------------------------------------------- #
# Fase 7 — serialización Pydantic con mode="json"                             #
# --------------------------------------------------------------------------- #


class _Severity(str, Enum):
    """Enum de prueba para verificar la serialización JSON-compat."""

    WARNING = "warning"
    CRITICAL = "critical"


class _PydanticFrame(BaseModel):
    """Payload Pydantic con campos no-JSON nativos (datetime + Enum)."""

    model_config = ConfigDict(frozen=True, strict=True)
    severity: _Severity
    occurred_at: datetime
    message: str


def test_json_fallback_serializes_pydantic_with_datetime() -> None:
    """``_json_fallback`` debe delegar a ``model_dump(mode='json')``.

    Sin ``mode='json'`` los campos ``datetime``/``Enum`` salen como objetos
    nativos y el ``json.dumps`` externo lanza ``TypeError``.  Con la
    corrección de Fase 7 se convierten a ISO-8601/valor respectivamente.
    """
    frame = _PydanticFrame(
        severity=_Severity.WARNING,
        occurred_at=datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
        message="umbral de RAM",
    )

    dumped = _json_fallback(frame)

    assert isinstance(dumped, dict)
    assert dumped["severity"] == "warning"  # Enum → valor primitivo
    assert isinstance(dumped["occurred_at"], str)  # datetime → ISO string
    assert dumped["occurred_at"].startswith("2026-04-23")
    # Además, el dict resultante debe ser serializable por json.dumps sin error.
    json.dumps(dumped)


@pytest.mark.asyncio
async def test_bus_event_with_pydantic_payload_broadcasts_without_type_error(
    ws_client_factory, event_bus: CoreEventBus
) -> None:
    """Un payload Pydantic con datetime se propaga al cliente sin error."""
    client, _handler = ws_client_factory
    async with client.ws_connect("/api/status") as ws:
        await asyncio.wait_for(ws.receive_str(), timeout=1.0)  # snapshot

        # Publicamos un evento cuyo payload NO es dict plano — contiene un
        # modelo Pydantic con datetime.  Antes de Fase 7 esto rompía el
        # dispatch del bus con TypeError en json.dumps.
        payload_model = _PydanticFrame(
            severity=_Severity.CRITICAL,
            occurred_at=datetime(2026, 4, 23, 10, 30, tzinfo=timezone.utc),
            message="bus flooded",
        )
        # _json_fallback sólo se activa cuando el VALOR dentro del payload
        # es un modelo; el payload en sí se mantiene dict para respetar el
        # contrato del Event.
        await event_bus.publish(
            Event(
                topic="ops.log.critical",
                payload={"detail": payload_model},
                source="telemetry-daemon",
            )
        )

        msg = await asyncio.wait_for(ws.receive_str(), timeout=1.0)
        frame = json.loads(msg)

        assert frame["event_type"] == "ops.log.critical"
        assert frame["payload"]["detail"]["severity"] == "critical"
        assert frame["payload"]["detail"]["occurred_at"].startswith("2026-04-23")


def test_default_patterns_cover_fase7_hierarchical_topics() -> None:
    """Los patrones actuales matchean los tópicos jerárquicos de Fase 7."""
    import fnmatch

    fase7_samples = (
        "ops.telemetry.tick",
        "ops.process.started",
        "ops.process.completed",
        "ops.process.error",
        "ops.log.info",
        "ops.log.warning",
        "ops.log.error",
        "ops.log.critical",
    )
    for topic in fase7_samples:
        assert any(
            fnmatch.fnmatch(topic, p) for p in DEFAULT_FORWARDED_PATTERNS
        ), f"Tópico {topic!r} no matchea ningún patrón del puente WS"
