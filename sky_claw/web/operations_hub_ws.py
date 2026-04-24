"""Operations Hub WebSocket bridge — streams CoreEventBus events to browser clients.

This module wires the Sky-Claw :class:`CoreEventBus` to the new Operations Hub
frontend via a single WebSocket endpoint (``/api/status``).  It subscribes to
the relevant topic patterns, serialises each event as JSON, and broadcasts the
payload to every connected browser session.

Design notes
------------
* **Instanciable, no singleton.** One handler per :class:`WebApp` instance,
  matching the project convention for testability.
* **Back-pressure aware.** Writes are best-effort; a slow or dead client never
  blocks the bus — its socket is discarded on the next broadcast.
* **Reconnect-friendly.** On connect the client receives a ``snapshot``
  message so it can render an immediate skeleton before events flow.
* **Heartbeat.** aiohttp's built-in ``heartbeat`` parameter sends PING frames
  every ``HEARTBEAT_SECONDS`` so dead TCP connections are detected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Final

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    from sky_claw.core.event_bus import CoreEventBus, Event
    from sky_claw.security.auth_token_manager import AuthTokenManager

logger = logging.getLogger(__name__)

# Heartbeat cadence for browser WebSocket clients (seconds).
HEARTBEAT_SECONDS: Final[float] = 30.0

# Topic patterns forwarded to the Operations Hub.  New Sky-Claw modules should
# publish under the ``ops.*`` namespace; the legacy topics listed below are
# bridged so existing producers work without modification.
DEFAULT_FORWARDED_PATTERNS: Final[tuple[str, ...]] = (
    # Native Operations Hub topics (preferred for new code).
    "ops.log.*",
    "ops.process.*",
    "ops.telemetry.*",
    "ops.conflict.*",
    "ops.hitl.*",
    # Legacy topics bridged for current producers.
    "system.telemetry.*",
    "system.modlist.*",
    "synthesis.pipeline.*",
    "xedit.patch.*",
    "pipeline.dyndolod.*",
)


class OperationsHubWSHandler:
    """WebSocket broadcaster bridging :class:`CoreEventBus` → browser clients.

    Args:
        event_bus: The running :class:`CoreEventBus` instance whose events
            should be forwarded to browsers.
        forwarded_patterns: Tuple of ``fnmatch`` topic patterns to subscribe
            to.  Defaults to :data:`DEFAULT_FORWARDED_PATTERNS`.
        heartbeat_seconds: Cadence of aiohttp's WebSocket PING frames.
    """

    def __init__(
        self,
        event_bus: CoreEventBus,
        *,
        forwarded_patterns: tuple[str, ...] = DEFAULT_FORWARDED_PATTERNS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        auth_manager: AuthTokenManager | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._patterns = forwarded_patterns
        self._heartbeat = heartbeat_seconds
        self._auth_manager = auth_manager
        self._clients: set[web.WebSocketResponse] = set()
        self._clients_lock = asyncio.Lock()
        self._started = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Register the fan-out subscriber on :class:`CoreEventBus`.

        Idempotent — a duplicate call is logged and ignored.  Call :meth:`stop`
        before re-starting if the bus instance changes.
        """
        if self._started:
            logger.warning("OperationsHubWSHandler.start() called twice; ignoring")
            return
        for pattern in self._patterns:
            self._event_bus.subscribe(pattern, self._on_bus_event)
        self._started = True
        logger.info(
            "OperationsHubWSHandler started (patterns=%d)",
            len(self._patterns),
        )

    async def stop(self) -> None:
        """Unsubscribe from the bus and close every active client socket."""
        if not self._started:
            return
        for pattern in self._patterns:
            self._event_bus.unsubscribe(pattern, self._on_bus_event)
        async with self._clients_lock:
            stale = list(self._clients)
            self._clients.clear()
        for ws in stale:
            try:
                await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"shutdown")
            except (ConnectionResetError, RuntimeError) as exc:
                logger.debug("WS close raised during shutdown, ignoring: %s", exc)
        self._started = False
        logger.info("OperationsHubWSHandler stopped")

    # ------------------------------------------------------------------ #
    # Bus → clients                                                       #
    # ------------------------------------------------------------------ #

    async def _on_bus_event(self, event: Event) -> None:
        """CoreEventBus subscriber that fans-out one event to every client.

        Dead/closed sockets are pruned lazily rather than raising.
        """
        frame = {
            "event_type": event.topic,
            "payload": event.payload,
            "timestamp_ms": event.timestamp_ms,
            "source": event.source,
        }
        message = json.dumps(frame, default=_json_fallback)
        await self._broadcast(message)

    async def _broadcast(self, message: str) -> None:
        """Send ``message`` to all connected clients; discard dead ones."""
        dead: list[web.WebSocketResponse] = []
        async with self._clients_lock:
            snapshot = tuple(self._clients)
        for ws in snapshot:
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_str(message)
            except (ConnectionResetError, RuntimeError) as exc:
                logger.debug("WS send failed, dropping client: %s", exc)
                dead.append(ws)
        if dead:
            async with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)

    # ------------------------------------------------------------------ #
    # aiohttp route handler                                               #
    # ------------------------------------------------------------------ #

    def _validate_ws_auth(self, request: web.Request) -> bool:
        if self._auth_manager is None:
            return True
        token = request.headers.get("X-Auth-Token", "")
        if not token:
            return False
        return self._auth_manager.validate(token)

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        """aiohttp handler mounted at ``/api/status``.

        Upgrades the HTTP request, registers the socket, pushes an initial
        ``snapshot`` frame, and then forwards any client commands (``ping``,
        etc.) while the connection stays open.
        """
        if not self._validate_ws_auth(request):
            logger.warning("Operations Hub WS auth rejected (remote=%s)", request.remote)
            ws_reject = web.WebSocketResponse()
            await ws_reject.prepare(request)
            await ws_reject.close(
                code=aiohttp.WSCloseCode.POLICY_VIOLATION,
                message=b"Authentication required",
            )
            return ws_reject
        ws = web.WebSocketResponse(heartbeat=self._heartbeat)
        await ws.prepare(request)

        async with self._clients_lock:
            self._clients.add(ws)
            client_count = len(self._clients)
        logger.info(
            "Operations Hub WS client connected (remote=%s, total=%d)",
            request.remote,
            client_count,
        )

        # Send initial snapshot so the UI can leave its empty state immediately.
        await ws.send_str(
            json.dumps(
                {
                    "event_type": "snapshot",
                    "payload": {"connected": True, "forwarded_patterns": list(self._patterns)},
                    "timestamp_ms": int(time.time() * 1000),
                    "source": "operations_hub_ws",
                }
            )
        )

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_client_message(ws, msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.warning(
                        "Operations Hub WS error from %s: %s",
                        request.remote,
                        ws.exception(),
                    )
        finally:
            async with self._clients_lock:
                self._clients.discard(ws)
                remaining = len(self._clients)
            logger.info(
                "Operations Hub WS client disconnected (remote=%s, remaining=%d)",
                request.remote,
                remaining,
            )

        return ws

    async def _handle_client_message(self, ws: web.WebSocketResponse, raw: str) -> None:
        """Parse one client frame; respond to ping, ignore unknown actions."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Operations Hub WS: ignoring non-JSON frame (%d bytes)", len(raw))
            return
        if not isinstance(data, dict):
            return
        action = data.get("action")
        if action == "ping":
            await ws.send_str(
                json.dumps(
                    {
                        "event_type": "pong",
                        "payload": {},
                        "timestamp_ms": int(time.time() * 1000),
                        "source": "operations_hub_ws",
                    }
                )
            )
        # Future actions (subscribe / command dispatch) land here.

    # ------------------------------------------------------------------ #
    # Introspection (used by tests)                                       #
    # ------------------------------------------------------------------ #

    @property
    def client_count(self) -> int:
        """Number of currently connected WebSocket clients."""
        return len(self._clients)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_fallback(obj: object) -> object:
    """Best-effort JSON fallback for payload objects that are not built-ins."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()  # pydantic BaseModel
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(obj)  # type: ignore[arg-type]
    return repr(obj)


def register_operations_hub_routes(
    app: web.Application,
    event_bus: CoreEventBus,
    *,
    auth_manager: AuthTokenManager | None = None,
    route_path: str = "/api/status",
    forwarded_patterns: tuple[str, ...] = DEFAULT_FORWARDED_PATTERNS,
) -> OperationsHubWSHandler:
    """Wire the Operations Hub WebSocket route onto an aiohttp application.

    Args:
        app: The aiohttp :class:`~aiohttp.web.Application` to mutate.
        event_bus: The running :class:`CoreEventBus` instance.
        route_path: URL where the WebSocket is exposed (default ``/api/status``).
        forwarded_patterns: Override the default topic patterns if desired.

    Returns:
        The configured :class:`OperationsHubWSHandler` — callers should invoke
        :meth:`~OperationsHubWSHandler.start` once the bus is running, and
        :meth:`~OperationsHubWSHandler.stop` during shutdown.
    """
    handler = OperationsHubWSHandler(event_bus, auth_manager=auth_manager, forwarded_patterns=forwarded_patterns)
    app.router.add_get(route_path, handler.websocket_handler)
    logger.info("Operations Hub WebSocket route registered at %s", route_path)
    return handler
