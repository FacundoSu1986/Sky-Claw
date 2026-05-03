"""WebSocket bridge: CoreEventBus → Operations Hub clients.

Architecture
------------
``OperationsHubWSHandler`` subscribes to a set of topic patterns on
``CoreEventBus``.  When an event matches, it is serialised as JSON and
broadcast to every currently-connected WebSocket client.

On connect, a *snapshot* frame is delivered immediately so the client can
initialise its UI without waiting for the first real event.

The handler implements the aiohttp callable protocol so it can be registered
directly as a route handler::

    app.router.add_get("/api/status", handler)

Use ``register_operations_hub_routes`` as a convenient factory.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event

if TYPE_CHECKING:
    from aiohttp.web_ws import WebSocketResponse

logger = logging.getLogger(__name__)

# Default set of CoreEventBus topic patterns forwarded to clients.
# Uses fnmatch glob syntax (``*`` matches any substring including dots).
DEFAULT_FORWARDED_PATTERNS: tuple[str, ...] = (
    "ops.log.*",
    "ops.process.*",
    "ops.telemetry.*",
    "ops.conflict.*",
    "ops.hitl.*",
)

_STATUS_ROUTE = "/api/status"


class OperationsHubWSHandler:
    """Fan out ``CoreEventBus`` events to all connected WebSocket clients.

    Parameters
    ----------
    event_bus:
        The running :class:`~sky_claw.antigravity.core.event_bus.CoreEventBus`
        to subscribe to.
    forwarded_patterns:
        Tuple of fnmatch-style topic patterns whose matching events are
        forwarded to clients.  Defaults to ``DEFAULT_FORWARDED_PATTERNS``.
    """

    def __init__(
        self,
        event_bus: CoreEventBus,
        *,
        forwarded_patterns: tuple[str, ...] = DEFAULT_FORWARDED_PATTERNS,
    ) -> None:
        self._bus = event_bus
        self._patterns = forwarded_patterns
        self._clients: set[WebSocketResponse] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to all forwarded patterns on the event bus."""
        for pattern in self._patterns:
            self._bus.subscribe(pattern, self._on_bus_event)
        logger.debug(
            "OperationsHubWSHandler started — forwarding patterns: %s",
            self._patterns,
        )

    async def stop(self) -> None:
        """Unsubscribe from the bus and close all active WebSocket connections."""
        for pattern in self._patterns:
            self._bus.unsubscribe(pattern, self._on_bus_event)
        for ws in list(self._clients):
            with __import__("contextlib").suppress(Exception):
                await ws.close()
        self._clients.clear()
        logger.debug("OperationsHubWSHandler stopped")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client_count(self) -> int:
        """Number of currently connected WebSocket clients."""
        return len(self._clients)

    # ------------------------------------------------------------------
    # aiohttp route handler protocol
    # ------------------------------------------------------------------

    async def __call__(self, request: web.Request) -> WebSocketResponse:
        """Handle a WebSocket upgrade; run the receive loop until disconnect."""
        ws: WebSocketResponse = web.WebSocketResponse()
        await ws.prepare(request)

        self._clients.add(ws)
        logger.debug("WS client connected — total=%d", self.client_count)

        # Deliver initial snapshot so the client can paint its initial state.
        await ws.send_str(json.dumps({"event_type": "snapshot", "payload": {"connected": True}}))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_client_message(ws, msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self._clients.discard(ws)
            logger.debug("WS client disconnected — total=%d", self.client_count)

        return ws

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _handle_client_message(self, ws: WebSocketResponse, raw: str) -> None:
        """Process an inbound text frame from a client."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        action = data.get("action")
        if action == "ping":
            await ws.send_str(json.dumps({"event_type": "pong"}))

    async def _on_bus_event(self, event: Event) -> None:
        """CoreEventBus subscriber — broadcast the event to all clients."""
        if not self._clients:
            return
        frame = json.dumps(
            {
                "event_type": event.topic,
                "payload": event.payload,
                "source": event.source,
            }
        )
        dead: set[WebSocketResponse] = set()
        for ws in list(self._clients):
            try:
                await ws.send_str(frame)
            except Exception:
                logger.debug("Failed to send to client — marking for removal")
                dead.add(ws)
        self._clients -= dead


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


def register_operations_hub_routes(
    app: web.Application,
    event_bus: CoreEventBus,
    *,
    forwarded_patterns: tuple[str, ...] = DEFAULT_FORWARDED_PATTERNS,
) -> OperationsHubWSHandler:
    """Register the ``/api/status`` WebSocket route and return the handler.

    Parameters
    ----------
    app:
        The :class:`aiohttp.web.Application` to mount the route on.
    event_bus:
        Running ``CoreEventBus`` instance.
    forwarded_patterns:
        Override the default forwarded topic patterns.

    Returns
    -------
    OperationsHubWSHandler
        The created handler (call :meth:`~OperationsHubWSHandler.start`
        before the server begins accepting connections).
    """
    handler = OperationsHubWSHandler(event_bus, forwarded_patterns=forwarded_patterns)

    # Wrap in a plain ``async def`` so aiohttp's route dispatcher recognises
    # the handler as a coroutine function (``asyncio.iscoroutinefunction``
    # returns ``False`` for callable *objects* even when their ``__call__``
    # is ``async``).  The wrapper keeps ``handler`` accessible on the returned
    # object for lifecycle management.
    async def _ws_route(request: web.Request) -> web.StreamResponse:
        return await handler(request)

    app.router.add_get(_STATUS_ROUTE, _ws_route)
    return handler
