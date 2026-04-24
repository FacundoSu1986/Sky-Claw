"""Sky-Claw Web UI — local aiohttp server with chat interface.

Serves a single-page chat UI and exposes a REST endpoint for
sending messages to the LLM router.  Supports running inside a
PyInstaller bundle (``sys._MEIPASS``) as well as normal Python.

The WebApp supports **lazy initialization**: the ``router`` argument
can be ``None`` on first run.  When the user completes the setup
wizard, the ``on_setup_complete`` callback is invoked which triggers
``AppContext.start_full()`` and updates the router reference.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from sky_claw.auto_detect import AutoDetector
from sky_claw.local_config import load as load_local_config
from sky_claw.local_config import save as save_local_config
from sky_claw.logging_config import correlation_id_var
from sky_claw.web.operations_hub_ws import (
    OperationsHubWSHandler,
    register_operations_hub_routes,
)

if TYPE_CHECKING:
    from sky_claw.agent.router import LLMRouter
    from sky_claw.core.event_bus import CoreEventBus
    from sky_claw.security.auth_token_manager import AuthTokenManager

logger = logging.getLogger(__name__)


def _get_static_dir() -> pathlib.Path:
    """Resolve the static assets directory, handling PyInstaller bundles."""
    if getattr(sys, "frozen", False):
        # Running inside a PyInstaller bundle.
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

# Type alias for the setup-complete callback.
OnSetupComplete = Callable[["WebApp"], Awaitable[None]]


class WebApp:
    """Lightweight web frontend for Sky-Claw.

    Args:
        router: The LLM router to delegate chat messages to.
                 Can be ``None`` on first run (lazy init).
        session: An ``aiohttp.ClientSession`` for outbound calls.
        config_path: Path to local_config.json for the setup wizard.
        tools_installer: Optional ToolsInstaller for auto-downloading tools.
        on_setup_complete: Async callback invoked after the user saves the
            setup wizard.  The callback receives ``self`` (the WebApp) so
            it can update ``_router`` and ``_tools_installer`` after
            ``AppContext.start_full()`` completes.
    """

    def __init__(
        self,
        router: LLMRouter | None,
        session: aiohttp.ClientSession,
        config_path: pathlib.Path | None = None,
        tools_installer: Any = None,
        on_setup_complete: OnSetupComplete | None = None,
        auth_manager: AuthTokenManager | None = None,
        event_bus: CoreEventBus | None = None,
    ) -> None:
        self._router = router
        self._session = session
        self._chat_id = "web-session"
        self._config_path = config_path or _CONFIG_PATH
        self._tools_installer = tools_installer
        self._on_setup_complete = on_setup_complete
        self._auth_manager = auth_manager
        self._event_bus = event_bus
        # Populated by create_app() when an event_bus is supplied.  Callers
        # should invoke ``await webapp.ops_hub_handler.start()`` once their
        # CoreEventBus is running, and ``stop()`` during shutdown.
        self.ops_hub_handler: OperationsHubWSHandler | None = None

    @web.middleware
    async def _correlation_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Middleware that sets a unique correlation ID for each web request."""
        corr_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
        # Reject suspicious non-UUID correlation IDs from clients
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
    async def _setup_auth_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Protect /api/setup and /api/* mutation endpoints.

        Strategy: setup endpoints are restricted to requests originating
        from loopback (127.0.0.1 / ::1).  This is correct for a local
        desktop tool — the setup wizard is only ever used from the same
        machine, and binding to localhost prevents remote exploitation.
        For the /api/chat endpoint, a Bearer token is also required when
        an AuthTokenManager is configured.
        """
        path = request.path

        if path in ("/api/setup",) or path.startswith("/api/setup"):
            peer = request.remote or ""
            if peer not in ("127.0.0.1", "::1", "localhost"):
                logger.warning("Setup endpoint blocked for non-local peer: %s", peer)
                return web.Response(status=403, text="Forbidden: setup only accessible from localhost")

        if path == "/api/chat" and self._auth_manager is not None:
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
                self._setup_auth_middleware,
            ]
        )
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/setup.html", self._handle_setup_page)
        app.router.add_get("/api/setup", self._handle_get_setup)
        app.router.add_post("/api/setup", self._handle_post_setup)
        app.router.add_get("/api/auto-detect", self._handle_auto_detect)
        app.router.add_post("/api/install-tools", self._handle_install_tools)
        app.router.add_post("/api/chat", self._handle_chat)
        app.router.add_static("/static", _STATIC_DIR, name="static")

        # Operations Hub WebSocket bridge — wired only when a CoreEventBus is
        # supplied.  The handler is started/stopped by the enclosing AppContext
        # so its lifecycle matches the bus it forwards from.
        if self._event_bus is not None:
            self.ops_hub_handler = register_operations_hub_routes(app, self._event_bus, auth_manager=self._auth_manager)

        return app

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the main HTML page, or redirect to setup if first run."""
        cfg = load_local_config(self._config_path)
        if cfg.first_run:
            setup_page = _STATIC_DIR / "setup.html"
            if setup_page.exists():
                raise web.HTTPFound("/setup.html")
        index_path = _STATIC_DIR / "index.html"
        return web.FileResponse(index_path)

    async def _handle_setup_page(self, request: web.Request) -> web.Response:
        """Serve the setup wizard page."""
        setup_path = _STATIC_DIR / "setup.html"
        if not setup_path.exists():
            return web.Response(text="Setup page not found", status=404)
        return web.FileResponse(setup_path)

    async def _handle_get_setup(self, request: web.Request) -> web.Response:
        """Return current config (without sensitive keys)."""
        cfg = load_local_config(self._config_path)
        return web.json_response(
            {
                "mo2_root": cfg.mo2_root or "",
                "install_dir": cfg.install_dir or "",
                "loot_exe": cfg.loot_exe or "",
                "xedit_exe": cfg.xedit_exe or "",
                "pandora_exe": cfg.pandora_exe or "",
                "bodyslide_exe": cfg.bodyslide_exe or "",
                "first_run": cfg.first_run,
                # API keys are never returned for security.
            }
        )

    async def _handle_post_setup(self, request: web.Request) -> web.Response:
        """Save setup config from the wizard, then trigger full init."""
        try:
            data: dict[str, Any] = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        cfg = load_local_config(self._config_path)
        # Update fields from the wizard.
        if data.get("mo2_root"):
            cfg.mo2_root = str(data["mo2_root"])
        if data.get("install_dir"):
            cfg.install_dir = str(data["install_dir"])
        if data.get("loot_exe"):
            cfg.loot_exe = str(data["loot_exe"])
        if data.get("xedit_exe"):
            cfg.xedit_exe = str(data["xedit_exe"])
        if data.get("pandora_exe"):
            cfg.pandora_exe = str(data["pandora_exe"])
        if data.get("bodyslide_exe"):
            cfg.bodyslide_exe = str(data["bodyslide_exe"])

        if data.get("api_key"):
            cfg.set_api_key(data["api_key"])
        if data.get("nexus_api_key"):
            cfg.set_nexus_api_key(data["nexus_api_key"])
        if data.get("telegram_bot_token"):
            cfg.set_telegram_bot_token(data["telegram_bot_token"])
        if data.get("telegram_chat_id"):
            cfg.telegram_chat_id = str(data["telegram_chat_id"])

        if data.get("skyrim_path"):
            cfg.skyrim_path = str(data["skyrim_path"])

        cfg.first_run = False
        save_local_config(cfg, self._config_path)

        # Trigger full initialization if callback is provided.
        if self._on_setup_complete is not None:
            try:
                await self._on_setup_complete(self)
                logger.info("on_setup_complete callback executed successfully")
            except Exception as exc:
                logger.exception("on_setup_complete callback failed: %s", exc)
                return web.json_response(
                    {"error": "Configuration saved but initialization failed. Please restart the application."},
                    status=500,
                )

        return web.json_response({"status": "ok"})

    async def _handle_auto_detect(self, request: web.Request) -> web.Response:
        """Run AutoDetector and return discovered paths."""
        try:
            result = await AutoDetector.detect_all()
            return web.json_response(result)
        except Exception as exc:
            logger.error("Auto-detect error: %s", exc)
            return web.json_response({"error": "Auto-detection failed. Check server logs."}, status=500)

    async def _handle_install_tools(self, request: web.Request) -> web.Response:
        """Install missing LOOT / SSEEdit via ToolsInstaller."""
        if self._tools_installer is None:
            return web.json_response({"error": "ToolsInstaller not available"}, status=503)

        try:
            data: dict[str, Any] = await request.json()
        except json.JSONDecodeError:
            data = {}

        install_dir = pathlib.Path(data.get("install_dir", "") or str(self._config_path.parent / "tools"))
        results: dict[str, str] = {}

        if data.get("install_loot", True):
            try:
                loot_result = await self._tools_installer.ensure_loot(session=self._session, install_dir=install_dir)
                results["loot"] = str(loot_result.exe_path)
                # Persist to config.
                cfg = load_local_config(self._config_path)
                cfg.loot_exe = str(loot_result.exe_path)
                save_local_config(cfg, self._config_path)
            except Exception as exc:
                logger.error("LOOT install error: %s", exc)
                results["loot_error"] = "LOOT installation failed. Check server logs."

        if data.get("install_xedit", True):
            try:
                xedit_result = await self._tools_installer.ensure_xedit(session=self._session, install_dir=install_dir)
                results["xedit"] = str(xedit_result.exe_path)
                cfg = load_local_config(self._config_path)
                cfg.xedit_exe = str(xedit_result.exe_path)
                save_local_config(cfg, self._config_path)
            except Exception as exc:
                logger.error("xEdit install error: %s", exc)
                results["xedit_error"] = "xEdit installation failed. Check server logs."

        return web.json_response(results)

    async def _handle_chat(self, request: web.Request) -> web.Response:
        """Process a chat message and return the assistant's response.

        Returns 503 if the router is not yet initialized (first run,
        setup wizard not completed).
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
