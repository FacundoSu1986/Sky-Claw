"""Contrato estricto para las entradas de chat de Telegram."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web

from sky_claw.app.comms.telegram import TelegramWebhook


def _webhook() -> tuple[TelegramWebhook, MagicMock, MagicMock]:
    router = MagicMock()
    router.chat = AsyncMock(return_value="respuesta")
    sender = MagicMock()
    sender.send = AsyncMock()
    webhook = TelegramWebhook(
        router=router,
        sender=sender,
        session=MagicMock(spec=aiohttp.ClientSession),
        authorized_user_id=123,
    )
    return webhook, router, sender


def _update(
    update_id: int,
    text: object = "hola",
    *,
    chat: object | None = None,
    sender: object | None = None,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": 123} if chat is None else chat,
            "from": {"id": 123} if sender is None else sender,
        },
    }


async def _drain(webhook: TelegramWebhook) -> None:
    tasks = list(webhook._tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("raw_json", ["[]", "null", "123", '"texto"', "true"])
@pytest.mark.asyncio
async def test_webhook_rechaza_json_raiz_no_objeto(aiohttp_client, raw_json) -> None:
    # Preparación
    webhook, router, _sender = _webhook()
    app = web.Application()
    app.router.add_post("/webhook", webhook.handle_update)
    client = await aiohttp_client(app)

    # Actuar
    response = await client.post(
        "/webhook",
        data=raw_json,
        headers={"Content-Type": "application/json"},
    )

    # Verificación
    assert response.status == 200
    router.chat.assert_not_awaited()
    assert not webhook._tasks


@pytest.mark.parametrize("message", [[], 7, "texto", True])
@pytest.mark.asyncio
async def test_telegram_rechaza_message_no_objeto(message) -> None:
    # Preparación
    webhook, router, _sender = _webhook()

    # Actuar
    await webhook.process_update({"update_id": 10, "message": message})

    # Verificación
    router.chat.assert_not_awaited()
    assert not webhook._tasks


@pytest.mark.parametrize("text", [None, 123, [], {}, True])
@pytest.mark.asyncio
async def test_telegram_no_coerciona_texto_de_otro_tipo(text) -> None:
    # Preparación
    webhook, router, _sender = _webhook()

    # Actuar
    await webhook.process_update(_update(20, text))

    # Verificación
    router.chat.assert_not_awaited()
    assert not webhook._tasks


@pytest.mark.asyncio
async def test_telegram_rechaza_texto_de_solo_espacios() -> None:
    # Preparación
    webhook, router, _sender = _webhook()

    # Actuar
    await webhook.process_update(_update(30, "  \t  "))

    # Verificación
    router.chat.assert_not_awaited()
    assert not webhook._tasks


@pytest.mark.parametrize("field,value", [("chat", []), ("chat", "123"), ("from", []), ("from", "123")])
@pytest.mark.asyncio
async def test_telegram_rechaza_shapes_anidados_invalidos(field, value) -> None:
    # Preparación
    webhook, router, _sender = _webhook()
    kwargs = {field: value}

    # Actuar
    await webhook.process_update(_update(40, "hola", **kwargs))

    # Verificación
    router.chat.assert_not_awaited()
    assert not webhook._tasks


@pytest.mark.asyncio
async def test_telegram_sigue_disponible_despues_de_un_mensaje_invalido() -> None:
    # Preparación
    webhook, router, sender = _webhook()

    # Actuar
    await webhook.process_update(_update(50, 42))
    await webhook.process_update(_update(51, "  hola  "))
    await _drain(webhook)

    # Verificación
    router.chat.assert_awaited_once_with("hola", webhook._session, chat_id="123")
    sender.send.assert_awaited_once_with(123, "respuesta")


@pytest.mark.asyncio
async def test_telegram_no_normaliza_el_request_id_hitl_opaco() -> None:
    # Preparación
    webhook, router, _sender = _webhook()
    hitl = MagicMock()
    hitl.respond = AsyncMock(return_value=False)
    webhook._hitl = hitl

    # Actuar
    await webhook.process_update(_update(60, "  /approve req-1  "))
    await _drain(webhook)

    # Verificación
    hitl.respond.assert_awaited_once_with("req-1  ", True)
    router.chat.assert_not_awaited()
