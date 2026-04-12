"""Tests for the Sky-Claw web UI server."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.web.app import WebApp


@pytest.fixture
def mock_router() -> MagicMock:
    router = MagicMock()
    router.chat = AsyncMock(return_value="Test response")
    return router


@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock()


@pytest.fixture
async def client(mock_router, mock_session, aiohttp_client):
    web_app = WebApp(router=mock_router, session=mock_session)
    app = web_app.create_app()
    return await aiohttp_client(app)


class TestWebIndex:
    @pytest.mark.asyncio
    async def test_index_returns_html(self, client) -> None:
        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "Sky-Claw" in text
        assert "<html" in text


class TestWebChatEndpoint:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self, client, mock_router) -> None:
        resp = await client.post(
            "/api/chat",
            json={"message": "hello"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["response"] == "Test response"
        mock_router.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_empty_message_returns_400(self, client) -> None:
        resp = await client.post(
            "/api/chat",
            json={"message": ""},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_chat_missing_message_returns_400(self, client) -> None:
        resp = await client.post(
            "/api/chat",
            json={},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_chat_invalid_json_returns_400(self, client) -> None:
        resp = await client.post(
            "/api/chat",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_chat_router_error_returns_500(self, client, mock_router) -> None:
        mock_router.chat = AsyncMock(side_effect=RuntimeError("LLM failed"))
        resp = await client.post(
            "/api/chat",
            json={"message": "test"},
        )
        assert resp.status == 500
        data = await resp.json()
        assert "error" in data
        # Exception details must NOT be leaked to the client (security requirement)
        assert "LLM failed" not in data["error"]
