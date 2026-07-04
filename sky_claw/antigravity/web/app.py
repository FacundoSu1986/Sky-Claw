"""Sky-Claw aiohttp service: chat API for the NiceGUI Forge interface.

After the GUI refactor (purge of legacy dual-state), this module no
longer serves the setup wizard or the legacy SPA — those flows are
handled exclusively by the NiceGUI Forge interface.  What remains:

* ``POST /api/chat`` — text chat against the LLM router.
* ``GET /ws/ui`` — GUI↔daemon chat WebSocket.

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

from sky_claw.logging_config import correlation_id_var

if TYPE_CHECKING:
    from sky_claw.antigravity.agent.router import LLMRouter
    from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager

logger = logging.getLogger(__name__)


def _get_exe_dir() -> pathlib.Path:
    """Directory where the .exe lives (or CWD for normal Python)."""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).parent
    return pathlib.Path.cwd()


_CONFIG_PATH = _get_exe_dir() / "sky_claw_config.json"


def _dev_no_auth_enabled() -> bool:
    """El bypass de auth de desarrollo (``SKY_CLAW_DEV_NO_AUTH``) SOLO aplica al
    ejecutar desde fuente.

    En un binario empaquetado (``sys.frozen``) se ignora incondicionalmente: un
    .exe distribuido nunca debe poder desactivar la autenticación vía variable
    de entorno, aunque el usuario —o un atacante con acceso al entorno— la fije.
    Sin este guard, ``SKY_CLAW_DEV_NO_AUTH=1`` abría /api/chat y /ws/ui en el
    release público (ZAi S2 [ALTO]).
    """
    if getattr(sys, "frozen", False):
        return False
    return os.environ.get("SKY_CLAW_DEV_NO_AUTH") == "1"


class WebApp:
    """Lightweight chat aiohttp service for the NiceGUI Forge interface.

    Args:
        router: The LLM router to delegate chat messages to.  May be
            ``None`` until the NiceGUI wizard completes initialisation.
        session: An ``aiohttp.ClientSession`` for outbound calls.
        config_path: Reserved for callers that need a config path during
            construction (kept for backward compatibility with tests).
        auth_manager: When provided, ``/api/chat`` requires a valid
            Bearer token issued by the manager.
    """

    def __init__(
        self,
        router: LLMRouter | None,
        session: aiohttp.ClientSession,
        config_path: pathlib.Path | None = None,
        auth_manager: AuthTokenManager | None = None,
    ) -> None:
        self._router = router
        self._session = session
        self._chat_id = "web-session"
        self._config_path = config_path or _CONFIG_PATH
        self._auth_manager = auth_manager

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
                if _dev_no_auth_enabled():
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
        app.router.add_get("/ws/ui", self._handle_ws_ui)

        return app

    def _validate_ws_auth(self, request: web.Request) -> bool:
        """X-Auth-Token check for /ws/ui (fail-closed when no auth_manager)."""
        if self._auth_manager is None:
            if _dev_no_auth_enabled():
                logger.warning("SKY_CLAW_DEV_NO_AUTH active — /ws/ui auth bypassed (dev mode only)")
                return True
            logger.error("/ws/ui auth rejected: auth_manager not configured (fail-closed)")
            return False
        token = request.headers.get("X-Auth-Token", "")
        if not token:
            return False
        return self._auth_manager.validate(token)

    async def _handle_ws_ui(self, request: web.Request) -> web.WebSocketResponse:
        """GUI↔daemon chat WebSocket at /ws/ui (Q&A: command/chat -> LLMRouter)."""
        if not self._validate_ws_auth(request):
            logger.warning("/ws/ui auth rejected (remote=%s)", request.remote)
            # The upgrade is still required: the GUI client only recognises an
            # auth rejection via the WS close code 4001 (mirrors the ops-hub
            # handler, which likewise upgrades then closes with a WS code).
            ws_reject = web.WebSocketResponse()
            await ws_reject.prepare(request)
            await ws_reject.close(code=4001, message=b"Authentication required")
            return ws_reject
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_ws_ui_message(ws, msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning("/ws/ui socket error: %s", ws.exception())
        return ws

    async def _handle_ws_ui_message(self, ws: web.WebSocketResponse, raw: str) -> None:
        """Handle one text frame: route command/chat to the LLM router."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            await ws.send_json({"type": "response", "payload": {"response": "⚠️ Invalid chat frame."}})
            return
        if not (isinstance(data, dict) and data.get("type") == "command" and data.get("command") == "chat"):
            return  # YAGNI: non-chat commands ignored gracefully (future agentic phase)
        text = str((data.get("payload") or {}).get("text", "")).strip()
        if not text:
            await ws.send_json({"type": "response", "payload": {"response": "⚠️ Empty message."}})
            return
        if self._router is None:
            await ws.send_json(
                {
                    "type": "response",
                    "payload": {"response": "⚠️ Sky-Claw no está configurado todavía. Completá el setup wizard."},
                }
            )
            return
        try:
            response = await self._router.chat(text, self._session, chat_id=self._chat_id)
        except Exception as exc:  # never crash the socket; don't leak internals to the client
            logger.exception("/ws/ui chat failed: %s", exc)
            await ws.send_json(
                {
                    "type": "response",
                    "payload": {
                        "response": "⚠️ Error del Agente. Revisa tu API Key en la config inicial y consulta los logs del servidor."
                    },
                }
            )
            return
        await ws.send_json({"type": "response", "payload": {"response": response}})

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
