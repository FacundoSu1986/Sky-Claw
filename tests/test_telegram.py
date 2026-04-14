"""Tests for sky_claw.comms.telegram and sky_claw.comms.telegram_sender."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from sky_claw.comms.telegram import _DEDUP_MAX_SIZE, TelegramWebhook
from sky_claw.comms.telegram_sender import (
    MAX_MESSAGE_LENGTH,
    TelegramSender,
    TelegramSendError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_update(update_id: int, chat_id: int = 123, text: str = "hello") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id},
        },
    }


def _make_webhook(
    router_response: str = "I found 3 mods",
) -> tuple[TelegramWebhook, AsyncMock, AsyncMock]:
    """Create a TelegramWebhook with mocked router and sender."""
    mock_router = MagicMock()
    mock_router.chat = AsyncMock(return_value=router_response)

    mock_sender = MagicMock()
    mock_sender.send = AsyncMock()

    mock_session = MagicMock(spec=aiohttp.ClientSession)

    webhook = TelegramWebhook(
        router=mock_router,
        sender=mock_sender,
        session=mock_session,
    )
    return webhook, mock_router, mock_sender


# ------------------------------------------------------------------
# TelegramWebhook tests
# ------------------------------------------------------------------


class TestTelegramWebhook:
    """Tests for the webhook handler."""

    @pytest.fixture()
    async def webhook_app(
        self,
    ) -> AsyncGenerator[
        tuple[web.Application, TelegramWebhook, AsyncMock, AsyncMock], None
    ]:
        webhook, mock_router, mock_sender = _make_webhook()
        app = web.Application()
        app.router.add_post("/webhook", webhook.handle_update)

        yield app, webhook, mock_router, mock_sender

        tasks = list(webhook._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_valid_update_returns_200(self, aiohttp_client, webhook_app) -> None:
        app, _webhook, _, _ = webhook_app
        client = await aiohttp_client(app)
        resp = await client.post("/webhook", json=_make_update(1))
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_dispatches_to_router(self, aiohttp_client, webhook_app) -> None:
        app, webhook, mock_router, mock_sender = webhook_app
        client = await aiohttp_client(app)
        await client.post("/webhook", json=_make_update(1, text="search Requiem"))

        # Wait for background task to complete.
        await asyncio.sleep(0.1)

        mock_router.chat.assert_awaited_once_with(
            "search Requiem", webhook._session, chat_id="123"
        )
        mock_sender.send.assert_awaited_once_with(123, "I found 3 mods")

    @pytest.mark.asyncio
    async def test_deduplication(self, aiohttp_client, webhook_app) -> None:
        app, _webhook, mock_router, _ = webhook_app
        client = await aiohttp_client(app)

        await client.post("/webhook", json=_make_update(42))
        await client.post("/webhook", json=_make_update(42))
        await asyncio.sleep(0.1)

        # Router should only be called once despite two identical updates.
        assert mock_router.chat.await_count == 1

    @pytest.mark.asyncio
    async def test_invalid_json_returns_200(self, aiohttp_client, webhook_app) -> None:
        app, _, _, _ = webhook_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/webhook",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_missing_message_text_returns_200(
        self, aiohttp_client, webhook_app
    ) -> None:
        app, _webhook, mock_router, _ = webhook_app
        client = await aiohttp_client(app)

        update = {"update_id": 99, "message": {"chat": {"id": 1}}}
        resp = await client.post("/webhook", json=update)
        assert resp.status == 200
        await asyncio.sleep(0.1)
        mock_router.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_update_id_returns_200(
        self, aiohttp_client, webhook_app
    ) -> None:
        app, _webhook, mock_router, _ = webhook_app
        client = await aiohttp_client(app)

        update = {"message": {"text": "hi", "chat": {"id": 1}}}
        resp = await client.post("/webhook", json=update)
        assert resp.status == 200
        await asyncio.sleep(0.1)
        mock_router.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dedup_evicts_oldest(self) -> None:
        webhook, _, _ = _make_webhook()
        # Fill dedup set beyond max.
        for i in range(_DEDUP_MAX_SIZE + 50):
            webhook._seen_updates[i] = None
            while len(webhook._seen_updates) > _DEDUP_MAX_SIZE:
                webhook._seen_updates.popitem(last=False)

        assert len(webhook._seen_updates) == _DEDUP_MAX_SIZE
        # Oldest entries should be evicted.
        assert 0 not in webhook._seen_updates
        assert _DEDUP_MAX_SIZE + 49 in webhook._seen_updates

    @pytest.mark.asyncio
    async def test_router_error_sends_error_message(
        self, aiohttp_client, webhook_app
    ) -> None:
        app, _webhook, mock_router, mock_sender = webhook_app
        mock_router.chat = AsyncMock(side_effect=RuntimeError("API down"))
        client = await aiohttp_client(app)

        await client.post("/webhook", json=_make_update(10))
        await asyncio.sleep(0.1)

        # Should attempt to send error message to user.
        mock_sender.send.assert_awaited_once_with(
            123,
            "\u26a0\ufe0f El agente ha sufrido un error interno en la orquestaci\u00f3n. Reiniciando subsistema...",
        )


# ------------------------------------------------------------------
# TelegramSender tests
# ------------------------------------------------------------------


class TestTelegramSender:
    """Tests for the Telegram message sender."""

    def test_split_message_short(self) -> None:
        chunks = TelegramSender._split_message("short message")
        assert chunks == ["short message"]

    def test_split_message_long(self) -> None:
        text = "a" * (MAX_MESSAGE_LENGTH + 100)
        chunks = TelegramSender._split_message(text)
        assert len(chunks) == 2
        assert len(chunks[0]) == MAX_MESSAGE_LENGTH
        assert len(chunks[1]) == 100

    def test_split_message_on_newlines(self) -> None:
        line = "x" * 2000
        text = f"{line}\n{line}\n{line}"
        chunks = TelegramSender._split_message(text)
        # Should split on newline boundaries.
        for chunk in chunks:
            assert len(chunk) <= MAX_MESSAGE_LENGTH

    def test_split_empty_message(self) -> None:
        chunks = TelegramSender._split_message("")
        assert chunks == [""]

    @pytest.mark.asyncio
    async def test_send_calls_gateway(self) -> None:
        mock_gateway = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_gateway.request = AsyncMock(return_value=mock_response)

        mock_session = MagicMock(spec=aiohttp.ClientSession)

        sender = TelegramSender(
            bot_token="123:ABC",
            gateway=mock_gateway,
            session=mock_session,
        )

        await sender.send(456, "hello")

        mock_gateway.request.assert_awaited_once()
        call_args = mock_gateway.request.call_args
        assert call_args[0][0] == "POST"
        assert "123:ABC" in call_args[0][1]
        assert call_args[1]["json"]["chat_id"] == 456
        assert call_args[1]["json"]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_send_raises_on_api_error(self) -> None:
        mock_gateway = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 400
        mock_response.text = AsyncMock(return_value="Bad Request")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_gateway.request = AsyncMock(return_value=mock_response)

        mock_session = MagicMock(spec=aiohttp.ClientSession)

        sender = TelegramSender(
            bot_token="123:ABC",
            gateway=mock_gateway,
            session=mock_session,
        )

        with pytest.raises(TelegramSendError, match="400"):
            await sender.send(456, "hello")

    @pytest.mark.asyncio
    async def test_rate_limit_tracking(self) -> None:
        mock_gateway = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_gateway.request = AsyncMock(return_value=mock_response)

        mock_session = MagicMock(spec=aiohttp.ClientSession)

        sender = TelegramSender(
            bot_token="123:ABC",
            gateway=mock_gateway,
            session=mock_session,
            rate_limit=5,
        )

        # Send 5 messages — should all go through without waiting.
        for i in range(5):
            await sender.send(789, f"msg {i}")

        assert mock_gateway.request.await_count == 5
        assert len(sender._send_times[789]) == 5
