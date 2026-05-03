"""Sky-Claw web UI server — aiohttp application.

Routes
------
GET  /                → HTML dashboard index page
GET  /api/setup       → loopback-only: read current LocalConfig as JSON
POST /api/setup       → loopback-only: persist LocalConfig fields from JSON body
GET  /api/auto-detect → loopback-only: run AutoDetector and return discovered paths
POST /api/chat        → optional Bearer-token auth; delegates to router.chat()

Security contract
-----------------
- Setup and auto-detect endpoints are restricted to loopback addresses only.
- Exception details are never forwarded to HTTP clients (prevents information leakage).
- When an ``auth_manager`` is provided, ``/api/chat`` requires a valid Bearer token.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from sky_claw.local.auto_detect import AutoDetector
from sky_claw.local.local_config import load as load_local_config
from sky_claw.local.local_config import save as save_local_config

if TYPE_CHECKING:
    import pathlib

    from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager

logger = logging.getLogger(__name__)

# Paths that require the request to originate from loopback.
_SETUP_PATHS: frozenset[str] = frozenset({"/api/setup", "/api/auto-detect"})

# Paths that require Bearer authentication when auth_manager is configured.
_AUTH_PATHS: frozenset[str] = frozenset({"/api/chat"})

# Accepted loopback addresses (IPv4, IPv6, IPv4-mapped IPv6).
_LOOPBACK_ADDRS: frozenset[str] = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

_INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sky-Claw Operations Hub</title>
</head>
<body>
  <h1>Sky-Claw</h1>
  <p>Operations Hub is running.</p>
</body>
</html>
"""

_GENERIC_ERROR = "An internal error occurred. Please try again later."


class WebApp:
    """aiohttp-based web UI server for Sky-Claw.

    Parameters
    ----------
    router:
        Object exposing ``async chat(message, session) -> str``.
    session:
        Session context forwarded to ``router.chat``.
    config_path:
        Optional :class:`pathlib.Path` to the local config file.
        When *None* the default path from ``local_config`` is used.
    auth_manager:
        Optional :class:`~sky_claw.antigravity.security.auth_token_manager.AuthTokenManager`.
        When provided, ``/api/chat`` requires a valid Bearer token.
    """

    def __init__(
        self,
        *,
        router: Any = None,
        session: Any = None,
        config_path: pathlib.Path | None = None,
        auth_manager: AuthTokenManager | None = None,
    ) -> None:
        self._router = router
        self._session = session
        self._config_path = config_path
        self._auth_manager = auth_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_app(self) -> web.Application:
        """Build and return the configured :class:`aiohttp.web.Application`."""
        # ``_setup_auth_middleware`` is an instance method, so we cannot apply
        # ``@web.middleware`` at class definition time.  Wrap it here with a
        # new-style middleware closure so aiohttp receives a coroutine function
        # that carries the ``_is_middleware = True`` marker.
        _mw_impl = self._setup_auth_middleware

        @web.middleware
        async def _auth_middleware(
            request: web.Request,
            handler: Any,
        ) -> web.Response:
            return await _mw_impl(request, handler)

        app = web.Application(middlewares=[_auth_middleware])
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/setup", self._handle_get_setup)
        app.router.add_post("/api/setup", self._handle_post_setup)
        app.router.add_get("/api/auto-detect", self._handle_auto_detect)
        app.router.add_post("/api/chat", self._handle_chat)
        return app

    # ------------------------------------------------------------------
    # Middleware (also called directly in tests for isolated unit checks)
    # ------------------------------------------------------------------

    async def _setup_auth_middleware(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.Response:
        """Enforce loopback restriction and optional Bearer authentication.

        Setup/auto-detect paths → 403 for non-loopback origins.
        Chat path + auth_manager configured → 401 if token missing or invalid.
        All other paths pass through unchanged.
        """
        path = request.path

        # ── Loopback-only guard for setup endpoints ────────────────────
        if path in _SETUP_PATHS and request.remote not in _LOOPBACK_ADDRS:
            return web.Response(
                status=403,
                content_type="application/json",
                body=json.dumps({"error": "Forbidden: this endpoint is only accessible from localhost"}).encode(),
            )

        # ── Bearer token guard for chat endpoint ──────────────────────
        if path in _AUTH_PATHS and self._auth_manager is not None:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return web.Response(
                    status=401,
                    content_type="application/json",
                    body=json.dumps({"error": "Unauthorized: Bearer token required"}).encode(),
                )
            token = auth_header[len("Bearer ") :]
            if not self._auth_manager.validate(token):
                return web.Response(
                    status=401,
                    content_type="application/json",
                    body=json.dumps({"error": "Unauthorized: invalid or expired token"}).encode(),
                )

        return await handler(request)

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, _request: web.Request) -> web.Response:
        return web.Response(
            status=200,
            content_type="text/html",
            text=_INDEX_HTML,
        )

    async def _handle_get_setup(self, _request: web.Request) -> web.Response:
        try:
            cfg = load_local_config(self._config_path) if self._config_path else load_local_config()
            data: dict[str, Any] = {
                "first_run": cfg.first_run,
                "mo2_root": str(cfg.mo2_root or ""),
                "install_dir": str(cfg.install_dir or ""),
                "loot_exe": str(cfg.loot_exe or ""),
                "xedit_exe": str(cfg.xedit_exe or ""),
                "pandora_exe": str(cfg.pandora_exe or ""),
                "bodyslide_exe": str(cfg.bodyslide_exe or ""),
            }
            return web.Response(
                status=200,
                content_type="application/json",
                body=json.dumps(data).encode(),
            )
        except Exception:
            logger.exception("Failed to read local config")
            return web.Response(
                status=500,
                content_type="application/json",
                body=json.dumps({"error": _GENERIC_ERROR}).encode(),
            )

    async def _handle_post_setup(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.Response(
                status=400,
                content_type="application/json",
                body=json.dumps({"error": "Invalid JSON body"}).encode(),
            )
        try:
            cfg = load_local_config(self._config_path) if self._config_path else load_local_config()
            for field in (
                "mo2_root",
                "install_dir",
                "loot_exe",
                "xedit_exe",
                "pandora_exe",
                "bodyslide_exe",
            ):
                if field in body:
                    setattr(cfg, field, body[field])
            cfg.first_run = False
            if self._config_path:
                save_local_config(cfg, self._config_path)
            else:
                save_local_config(cfg)
            return web.Response(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True}).encode(),
            )
        except Exception:
            logger.exception("Failed to save local config")
            return web.Response(
                status=500,
                content_type="application/json",
                body=json.dumps({"error": _GENERIC_ERROR}).encode(),
            )

    async def _handle_auto_detect(self, _request: web.Request) -> web.Response:
        try:
            result = await AutoDetector.detect_all()
            return web.Response(
                status=200,
                content_type="application/json",
                body=json.dumps(result).encode(),
            )
        except Exception:
            logger.exception("AutoDetector.detect_all failed")
            return web.Response(
                status=500,
                content_type="application/json",
                body=json.dumps({"error": _GENERIC_ERROR}).encode(),
            )

    async def _handle_chat(self, request: web.Request) -> web.Response:
        # Parse JSON body (400 on invalid JSON or missing/empty message)
        try:
            body = await request.json()
        except Exception:
            return web.Response(
                status=400,
                content_type="application/json",
                body=json.dumps({"error": "Invalid JSON body"}).encode(),
            )

        message: str = body.get("message", "")
        if not message:
            return web.Response(
                status=400,
                content_type="application/json",
                body=json.dumps({"error": "Field 'message' is required and must be non-empty"}).encode(),
            )

        # Delegate to router — mask any exception detail from the client
        try:
            response_text = await self._router.chat(message, self._session)
            return web.Response(
                status=200,
                content_type="application/json",
                body=json.dumps({"response": response_text}).encode(),
            )
        except Exception:
            logger.exception("router.chat raised an exception")
            return web.Response(
                status=500,
                content_type="application/json",
                body=json.dumps({"error": _GENERIC_ERROR}).encode(),
            )
