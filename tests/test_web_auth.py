"""P0.6 contract tests: Zero-Trust fail-closed for /api/chat and /api/status WS.

When auth_manager is None (not configured):
  - Production (no SKY_CLAW_DEV_NO_AUTH): requests are DENIED — fail-closed.
  - Development (SKY_CLAW_DEV_NO_AUTH=1): bypass allowed with a logged warning.

RED path on both HTTP tests until the _chat_auth_middleware guard is inverted.
RED path on the WS reject test until _validate_ws_auth guard is inverted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from sky_claw.antigravity.web.app import WebApp
from sky_claw.antigravity.web.operations_hub_ws import OperationsHubWSHandler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_web_app(router=None, session=None) -> WebApp:
    if session is None:
        session = MagicMock()
    return WebApp(router=router, session=session, auth_manager=None)


# ---------------------------------------------------------------------------
# P0.6 — /api/chat HTTP fail-closed
# ---------------------------------------------------------------------------


class TestZeroTrustHTTP:
    """POST /api/chat must deny when auth_manager is None (fail-closed)."""

    @pytest.mark.asyncio
    async def test_no_auth_manager_rejects_production(self, aiohttp_client, monkeypatch):
        """Without auth_manager and no dev flag, /api/chat must return 401.

        RED path: current middleware only checks auth when auth_manager is not None,
        so it currently returns 200 on unauthenticated requests when manager is None.
        """
        monkeypatch.delenv("SKY_CLAW_DEV_NO_AUTH", raising=False)
        web_app = _make_web_app()
        client = await aiohttp_client(web_app.create_app())

        resp = await client.post("/api/chat", json={"message": "hello"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_dev_flag_allows_bypass(self, aiohttp_client, monkeypatch):
        """SKY_CLAW_DEV_NO_AUTH=1 with no auth_manager must let /api/chat proceed."""
        monkeypatch.setenv("SKY_CLAW_DEV_NO_AUTH", "1")
        router = MagicMock()
        router.chat = AsyncMock(return_value="dev response")
        web_app = _make_web_app(router=router)
        client = await aiohttp_client(web_app.create_app())

        resp = await client.post("/api/chat", json={"message": "hello"})
        assert resp.status == 200
        body = await resp.json()
        assert body.get("response") == "dev response"


# ---------------------------------------------------------------------------
# P0.6 — WebSocket fail-closed
# ---------------------------------------------------------------------------


class TestZeroTrustWS:
    """_validate_ws_auth must deny when auth_manager is None (fail-closed)."""

    def test_no_auth_manager_rejects(self, monkeypatch):
        """Without auth_manager and no dev flag, _validate_ws_auth must return False.

        RED path: current code returns True when auth_manager is None.
        """
        monkeypatch.delenv("SKY_CLAW_DEV_NO_AUTH", raising=False)
        handler = OperationsHubWSHandler(MagicMock(), auth_manager=None)
        request = MagicMock(spec=web.Request)
        request.headers = {}
        assert handler._validate_ws_auth(request) is False

    def test_dev_flag_allows_bypass(self, monkeypatch):
        """SKY_CLAW_DEV_NO_AUTH=1 with no auth_manager must let WS auth pass."""
        monkeypatch.setenv("SKY_CLAW_DEV_NO_AUTH", "1")
        handler = OperationsHubWSHandler(MagicMock(), auth_manager=None)
        request = MagicMock(spec=web.Request)
        request.headers = {}
        assert handler._validate_ws_auth(request) is True
