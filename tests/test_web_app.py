"""Tests for sky_claw.antigravity.web.app.WebApp — security-focused.

Covers:
- /api/chat 500 must NOT leak exception details
- /api/chat auth_manager: missing Bearer → 401, invalid token → 401, valid → 200
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager
from sky_claw.antigravity.web.app import WebApp

if TYPE_CHECKING:
    import pathlib

    from aiohttp.test_utils import TestClient


def _make_web_app(
    router=None,
    session=None,
    config_path: pathlib.Path | None = None,
    auth_manager=None,
) -> WebApp:
    """Return a WebApp with sensible defaults for tests."""
    if session is None:
        session = MagicMock()
    return WebApp(
        router=router,
        session=session,
        config_path=config_path,
        auth_manager=auth_manager,
    )


def _make_mock_router(response: str = "ok") -> MagicMock:
    router = MagicMock()
    router.chat = AsyncMock(return_value=response)
    return router


@pytest.fixture
def mock_router() -> MagicMock:
    return _make_mock_router()


@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock()


async def _client(web_app: WebApp, aiohttp_client) -> TestClient:
    app = web_app.create_app()
    return await aiohttp_client(app)


# ===========================================================================
# /api/chat — 500 must NOT expose exception message
# ===========================================================================


class TestChat500:
    """Confirm that a router exception is NOT forwarded verbatim to the client."""

    @pytest.fixture(autouse=True)
    def _dev_auth_bypass(self, monkeypatch):
        """These tests focus on error handling, not auth — bypass auth via dev flag."""
        monkeypatch.setenv("SKY_CLAW_DEV_NO_AUTH", "1")

    @pytest.mark.asyncio
    async def test_500_does_not_leak_exception_detail(self, aiohttp_client, mock_session):
        secret_detail = "db password=hunter2 at host internal.corp"
        router = MagicMock()
        router.chat = AsyncMock(side_effect=RuntimeError(secret_detail))

        web_app = _make_web_app(router=router, session=mock_session)
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "hello"})

        assert resp.status == 500
        body = await resp.json()
        assert "error" in body

        error_text = body["error"]
        assert secret_detail not in error_text, f"Exception detail leaked in /api/chat response: {error_text!r}"

    @pytest.mark.asyncio
    async def test_500_returns_generic_message(self, aiohttp_client, mock_session):
        router = MagicMock()
        router.chat = AsyncMock(side_effect=Exception("boom"))

        web_app = _make_web_app(router=router, session=mock_session)
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "ping"})

        body = await resp.json()
        assert isinstance(body.get("error"), str)
        assert len(body["error"]) > 0

    @pytest.mark.asyncio
    async def test_500_body_is_json(self, aiohttp_client, mock_session):
        """Even on error the response must be valid JSON."""
        router = MagicMock()
        router.chat = AsyncMock(side_effect=Exception("crash"))

        web_app = _make_web_app(router=router, session=mock_session)
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "test"})
        assert resp.content_type == "application/json"


# ===========================================================================
# /api/chat — Bearer token authentication
# ===========================================================================


class TestChatBearerAuth:
    """When auth_manager is configured, /api/chat is token-gated."""

    def _make_auth_manager(self, valid: bool = True) -> MagicMock:
        mgr = MagicMock(spec=AuthTokenManager)
        mgr.validate = MagicMock(return_value=valid)
        return mgr

    @pytest.mark.asyncio
    async def test_missing_auth_header_returns_401(self, aiohttp_client, mock_session):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(
            router=_make_mock_router(),
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "hello"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_non_bearer_scheme_returns_401(self, aiohttp_client, mock_session):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(
            router=_make_mock_router(),
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        resp = await client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, aiohttp_client, mock_session):
        auth_mgr = self._make_auth_manager(valid=False)
        web_app = _make_web_app(
            router=_make_mock_router(),
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        resp = await client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_valid_token_returns_200(self, aiohttp_client, mock_session):
        auth_mgr = self._make_auth_manager(valid=True)
        router = _make_mock_router("authenticated response")
        web_app = _make_web_app(
            router=router,
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        resp = await client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer valid-token-abc"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body.get("response") == "authenticated response"

    @pytest.mark.asyncio
    async def test_no_auth_manager_rejects_without_dev_flag(self, aiohttp_client, mock_session, monkeypatch):
        """Without auth_manager and no dev flag, /api/chat must return 401 (fail-closed).

        P0.6: inverted from the old fail-open behaviour where None manager allowed all traffic.
        """
        monkeypatch.delenv("SKY_CLAW_DEV_NO_AUTH", raising=False)
        router = _make_mock_router("should not be reached")
        web_app = _make_web_app(router=router, session=mock_session, auth_manager=None)
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "hello"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_validate_called_with_token_value(self, aiohttp_client, mock_session):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(
            router=_make_mock_router(),
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        token_value = "my-secret-token-xyz"
        await client.post(
            "/api/chat",
            json={"message": "test"},
            headers={"Authorization": f"Bearer {token_value}"},
        )

        auth_mgr.validate.assert_called_once_with(token_value)

    @pytest.mark.asyncio
    async def test_middleware_401_without_bearer(self, mock_session):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(session=mock_session, auth_manager=auth_mgr)

        request = MagicMock(spec=web.Request)
        request.path = "/api/chat"
        request.remote = "127.0.0.1"
        request.headers = {"Authorization": ""}

        response = await web_app._chat_auth_middleware(
            request, handler=AsyncMock(return_value=web.Response(status=200))
        )
        assert response.status == 401

    @pytest.mark.asyncio
    async def test_middleware_passes_valid_token(self, mock_session):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(session=mock_session, auth_manager=auth_mgr)

        handler = AsyncMock(return_value=web.Response(status=200))
        request = MagicMock(spec=web.Request)
        request.path = "/api/chat"
        request.remote = "127.0.0.1"
        request.headers = {"Authorization": "Bearer goodtoken"}

        response = await web_app._chat_auth_middleware(request, handler)

        handler.assert_called_once()
        assert response.status == 200
