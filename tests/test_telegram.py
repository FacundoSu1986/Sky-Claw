"""Tests for sky_claw.antigravity.comms.telegram and sky_claw.antigravity.comms.telegram_sender."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web

from sky_claw.antigravity.comms.telegram import _DEDUP_MAX_SIZE, TelegramWebhook
from sky_claw.antigravity.comms.telegram_sender import (
    MAX_MESSAGE_LENGTH,
    TelegramSender,
    TelegramSendError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_update(
    update_id: int,
    chat_id: int = 123,
    text: str = "hello",
    sender_id: int | None = None,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id},
            "from": {"id": chat_id if sender_id is None else sender_id},
        },
    }


def _make_webhook(
    router_response: str = "I found 3 mods",
    authorized_user_id: int | None = 123,
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
        authorized_user_id=authorized_user_id,
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
    ) -> AsyncGenerator[tuple[web.Application, TelegramWebhook, AsyncMock, AsyncMock], None]:
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

        mock_router.chat.assert_awaited_once_with("search Requiem", webhook._session, chat_id="123")
        mock_sender.send.assert_awaited_once_with(123, "I found 3 mods")

    @pytest.mark.asyncio
    async def test_group_message_never_reaches_router(self) -> None:
        """Un grupo no puede representar la identidad de un operador individual."""
        webhook, mock_router, _ = _make_webhook(authorized_user_id=123)

        await webhook.process_update(
            _make_update(
                500,
                chat_id=-100123,
                text="run_loot_sort",
                sender_id=777,
            )
        )

        try:
            mock_router.chat.assert_not_awaited()
            assert not webhook._tasks
        finally:
            tasks = list(webhook._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
    async def test_missing_message_text_returns_200(self, aiohttp_client, webhook_app) -> None:
        app, _webhook, mock_router, _ = webhook_app
        client = await aiohttp_client(app)

        update = {"update_id": 99, "message": {"chat": {"id": 1}}}
        resp = await client.post("/webhook", json=update)
        assert resp.status == 200
        await asyncio.sleep(0.1)
        mock_router.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_update_id_returns_200(self, aiohttp_client, webhook_app) -> None:
        app, _webhook, mock_router, _ = webhook_app
        client = await aiohttp_client(app)

        update = {"message": {"text": "hi", "chat": {"id": 1}}}
        resp = await client.post("/webhook", json=update)
        assert resp.status == 200
        await asyncio.sleep(0.1)
        mock_router.chat.assert_not_awaited()

    @pytest.fixture()
    async def secret_webhook_app(
        self,
    ) -> AsyncGenerator[tuple[web.Application, TelegramWebhook], None]:
        """Webhook con secret_token configurado para probar la validación (H-5)."""
        mock_router = MagicMock()
        mock_router.chat = AsyncMock(return_value="ok")
        mock_sender = MagicMock()
        mock_sender.send = AsyncMock()
        webhook = TelegramWebhook(
            router=mock_router,
            sender=mock_sender,
            session=MagicMock(spec=aiohttp.ClientSession),
            secret_token="s3cr3t-token-xyz",
        )
        app = web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        yield app, webhook
        tasks = list(webhook._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_secret_token_correcto_acepta(self, aiohttp_client, secret_webhook_app) -> None:
        """H-5: token correcto pasa la validación (200)."""
        app, _ = secret_webhook_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/webhook",
            json=_make_update(1),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t-token-xyz"},
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_secret_token_incorrecto_rechaza(self, aiohttp_client, secret_webhook_app) -> None:
        """H-5: token incorrecto se rechaza con 401."""
        app, _ = secret_webhook_app
        client = await aiohttp_client(app)
        resp = await client.post(
            "/webhook",
            json=_make_update(1),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-token"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_secret_token_ausente_rechaza(self, aiohttp_client, secret_webhook_app) -> None:
        """H-5: sin header de token se rechaza con 401."""
        app, _ = secret_webhook_app
        client = await aiohttp_client(app)
        resp = await client.post("/webhook", json=_make_update(1))
        assert resp.status == 401

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
    async def test_router_error_sends_error_message(self, aiohttp_client, webhook_app) -> None:
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

    @pytest.mark.asyncio
    async def test_answer_callback_query_goes_through_gateway(self) -> None:
        """F4: answerCallbackQuery pasa por gateway.request (allow-list/SSRF/timeout)
        y consume la respuesta con async with (sin fuga de conexión)."""
        mock_gateway = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_gateway.request = AsyncMock(return_value=mock_response)

        mock_session = MagicMock(spec=aiohttp.ClientSession)

        sender = TelegramSender(bot_token="123:ABC", gateway=mock_gateway, session=mock_session)
        await sender.answer_callback_query("cbid-1", text="Unauthorized")

        mock_gateway.request.assert_awaited_once()
        call_args = mock_gateway.request.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1].endswith("answerCallbackQuery")
        assert call_args[1]["json"]["callback_query_id"] == "cbid-1"
        assert call_args[1]["json"]["text"] == "Unauthorized"
        # La respuesta se abre y cierra (async with) → sin fuga.
        mock_response.__aenter__.assert_awaited_once()
        mock_response.__aexit__.assert_awaited_once()
        # No se toca la sesión cruda (bypass del gateway).
        mock_session.post.assert_not_called()


class TestCallbackQueryEgress:
    """F4: el callback_query no debe bypassear el gateway ni fugar conexiones."""

    @pytest.mark.asyncio
    async def test_unauthorized_callback_answers_via_sender_not_raw_session(self) -> None:
        webhook, _router, mock_sender = _make_webhook(authorized_user_id=123)
        mock_sender.answer_callback_query = AsyncMock()

        # callback_query de un usuario NO autorizado (spoofing) → rama "Unauthorized".
        callback = {
            "id": "cb-1",
            "from": {"id": 999},
            "message": {"chat": {"id": 999}, "message_id": 5},
            "data": "hitl:approve:req-1",
        }
        await webhook._handle_callback_query(callback)

        mock_sender.answer_callback_query.assert_awaited_once()
        _args, kwargs = mock_sender.answer_callback_query.call_args
        assert kwargs.get("text") == "Unauthorized"
        # No se usa la sesión aiohttp cruda (que salteaba el gateway y fugaba la respuesta).
        webhook._session.post.assert_not_called()


# ------------------------------------------------------------------
# TelegramPolling — control de acceso por chat_id (C-2)
# ------------------------------------------------------------------


class TestTelegramPollingFailClosed:
    """Verifica el fail-closed del polling cuando no hay chat_id autorizado (C-2)."""

    def _make_polling(self, authorized_chat_id):
        from sky_claw.antigravity.comms.telegram_polling import TelegramPolling

        handler = MagicMock()
        handler.process_update = AsyncMock()
        polling = TelegramPolling(
            token="123:ABC",
            webhook_handler=handler,
            gateway=MagicMock(),
            session=MagicMock(spec=aiohttp.ClientSession),
            authorized_chat_id=authorized_chat_id,
        )
        return polling, handler

    async def test_sin_chat_id_configurado_no_despacha(self) -> None:
        """C-2: con authorized_chat_id=None el update NO debe llegar al handler."""
        polling, handler = self._make_polling(None)
        update = {"update_id": 1, "message": {"text": "hola", "chat": {"id": 555}}}

        await polling._process_raw_update(update)

        handler.process_update.assert_not_awaited()

    async def test_fail_closed_loguea_una_sola_vez(self, caplog) -> None:
        """review #257: el ERROR de fail-closed se emite una vez por instancia, no por update."""
        import logging

        polling, handler = self._make_polling(None)
        update = {"update_id": 1, "message": {"text": "hola", "chat": {"id": 555}}}

        with caplog.at_level(logging.ERROR):
            for _ in range(5):
                await polling._process_raw_update(update)

        fail_closed_errors = [r for r in caplog.records if "fail-closed" in r.getMessage()]
        assert len(fail_closed_errors) == 1
        # Y el mensaje apunta a la clave real, no a la inexistente telegram.operator_chat_id.
        assert "telegram_chat_id" in fail_closed_errors[0].getMessage()
        assert "telegram.operator_chat_id" not in fail_closed_errors[0].getMessage()
        handler.process_update.assert_not_awaited()

    async def test_chat_id_autorizado_despacha(self) -> None:
        """El operador autorizado sí es despachado al handler."""
        polling, handler = self._make_polling(555)
        update = {"update_id": 1, "message": {"text": "hola", "chat": {"id": 555}}}

        await polling._process_raw_update(update)

        handler.process_update.assert_awaited_once_with(update)

    async def test_chat_id_no_autorizado_se_descarta(self) -> None:
        """Un chat distinto al autorizado se descarta (comportamiento previo intacto)."""
        polling, handler = self._make_polling(555)
        update = {"update_id": 1, "message": {"text": "hola", "chat": {"id": 999}}}

        await polling._process_raw_update(update)

        handler.process_update.assert_not_awaited()
