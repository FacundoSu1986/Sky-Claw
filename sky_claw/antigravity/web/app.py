"""Sky-Claw aiohttp service: chat API + Operations Hub WebSocket bridge.

After the GUI refactor (purge of legacy dual-state), this module no
longer serves the setup wizard or the legacy SPA — those flows are
handled exclusively by the NiceGUI Forge interface.  What remains:

* ``POST /api/chat`` — text chat against the LLM router.
* WebSocket routes mounted by :func:`register_operations_hub_routes`
  when an event bus is provided.

The module supports lazy initialisation: ``router`` may be ``None``
during boot and is populated once the GUI wizard completes the user's
configuration.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from sky_claw.antigravity.web.operations_hub_ws import (
    OperationsHubWSHandler,
    register_operations_hub_routes,
)
from sky_claw.logging_config import correlation_id_var

if TYPE_CHECKING:
    from sky_claw.antigravity.agent.router import LLMRouter
    from sky_claw.antigravity.core.event_bus import CoreEventBus
    from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager

logger = logging.getLogger(__name__)


def _get_static_dir() -> pathlib.Path:
    """Resolve the static assets directory, handling PyInstaller bundles."""
    if getattr(sys, "frozen", False):
        base = pathlib.Path(sys._MEIPASS)  # type: ignore[attr-defined]
        return base / "sky_claw" / "web" / "static"
    return pathlib.Path(__file__).parent / "static"


def _get_exe_dir() -> pathlib.Path:
    """Directory where the .exe lives (or CWD for normal Python)."""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).parent
    return pathlib.Path.cwd()


_STATIC_DIR = _get_static_dir()
_CONFIG_PATH = _get_exe_dir() / "sky_claw_config.json"


class WebApp:
    """Lightweight chat + Operations Hub aiohttp service.

    Args:
        router: The LLM router to delegate chat messages to.  May be
            ``None`` until the NiceGUI wizard completes initialisation.
        session: An ``aiohttp.ClientSession`` for outbound calls.
        config_path: Reserved for callers that need a config path during
            construction (kept for backward compatibility with tests).
        auth_manager: When provided, ``/api/chat`` requires a valid
            Bearer token issued by the manager.
        event_bus: When provided, mounts the Operations Hub WebSocket
            routes against this bus.
    """

    def __init__(
        self,
        router: LLMRouter | None,
        session: aiohttp.ClientSession,
        config_path: pathlib.Path | None = None,
        auth_manager: AuthTokenManager | None = None,
        event_bus: CoreEventBus | None = None,
        ws_route_path: str = "/api/status",
    ) -> None:
        self._router = router
        self._session = session
        self._chat_id = "web-session"
        self._config_path = config_path or _CONFIG_PATH
        self._auth_manager = auth_manager
        self._event_bus = event_bus
        # URL where the Operations Hub WebSocket is mounted. Default keeps the
        # historical /api/status contract (standalone usage + tests); the GUI
        # bootloader overrides it to /ws/ui so the AgentCommunicationClient
        # handshake hits a real route instead of 404.
        self._ws_route_path = ws_route_path
        self.ops_hub_handler: OperationsHubWSHandler | None = None

    @web.middleware
    async def _correlation_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Set a unique correlation ID for each web request."""
        corr_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
        try:
            uuid.UUID(corr_id)
        except ValueError:
            corr_id = str(uuid.uuid4())
        token = correlation_id_var.set(corr_id)
        try:
            return await handler(request)
        finally:
            correlation_id_var.reset(token)

    @web.middleware
    async def _chat_auth_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Require a Bearer token on ``/api/chat`` — fail-closed when no auth_manager."""
        if request.path == "/api/chat":
            if self._auth_manager is None:
                if os.environ.get("SKY_CLAW_DEV_NO_AUTH") == "1":
                    logger.warning("SKY_CLAW_DEV_NO_AUTH active — /api/chat auth bypassed (dev mode only)")
                    return await handler(request)
                logger.error("/api/chat blocked: auth_manager not configured (fail-closed)")
                return web.Response(status=401, text="Unauthorized")
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return web.Response(status=401, text="Unauthorized")
            token = auth_header[len("Bearer ") :]
            if not self._auth_manager.validate(token):
                logger.warning("Invalid auth token on /api/chat from %s", request.remote)
                return web.Response(status=401, text="Unauthorized")
        return await handler(request)

    def create_app(self) -> web.Application:
        """Build and return the aiohttp Application."""
        app = web.Application(
            middlewares=[
                self._correlation_middleware,
                self._chat_auth_middleware,
            ]
        )
        app.router.add_post("/api/chat", self._handle_chat)
        if _STATIC_DIR.exists():
            app.router.add_static("/static", _STATIC_DIR, name="static")

        if self._event_bus is not None:
            self.ops_hub_handler = register_operations_hub_routes(
                app, self._event_bus, auth_manager=self._auth_manager, route_path=self._ws_route_path
            )

        if self._auth_manager is not None and self.ops_hub_handler is not None:
            self._auth_manager.register_rotation_callback(self.ops_hub_handler.close_all_clients)

        return app

    async def _handle_chat(self, request: web.Request) -> web.Response:
        """Process a chat message and return the assistant's response.

        Returns 503 when the router is not yet initialised (the GUI
        wizard has not been completed).
        """
        if self._router is None:
            return web.json_response(
                {"error": "Sky-Claw no está configurado todavía. Completá el setup wizard primero."},
                status=503,
            )

        try:
            data: dict[str, Any] = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        message = data.get("message", "").strip()
        if not message:
            return web.json_response({"error": "Empty message"}, status=400)

        try:
            response = await self._router.chat(message, self._session, chat_id=self._chat_id)
            return web.json_response({"response": response})
        except Exception as exc:
            logger.exception("Chat error: %s", exc)
            return web.json_response(
                {"error": "Error del Agente. Revisa tu API Key en la config inicial y consulta los logs del servidor."},
                status=500,
            )
