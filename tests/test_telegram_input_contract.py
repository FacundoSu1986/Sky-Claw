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


@pytest.mark.parametrize(
    "clave,valor",
    [
        ("chat", []),
        ("chat", "123"),
        ("from", []),
        ("from", "123"),
    ],
)
@pytest.mark.asyncio
async def test_telegram_rechaza_shapes_anidados_invalidos(clave, valor) -> None:
    # Preparación
    webhook, router, _sender = _webhook()
    update = _update(40, "hola")
    # La clave es la de cable de Telegram ("from"), no un kwarg del helper:
    # cada caso debe alcanzar process_update() con exactamente un shape
    # anidado malformado y el resto del mensaje válido.
    update["message"][clave] = valor

    # Actuar
    await webhook.process_update(update)

    # Verificación
    router.chat.assert_not_awaited()
    assert not webhook._tasks


# Matriz que enumera CADA campo validado por el boundary process_update():
# raíz JSON (test_webhook_rechaza_json_raiz_no_objeto), update_id,
# message, message.text, chat/from, chat.id/from.id, reply_to_message y
# los shapes internos de callback_query. Cada campo tiene su matriz de
# valores claramente inválidos y su aserto de no-enrutado / no-task.
@pytest.mark.parametrize("update_id", [None, "1", 1.0, True, [], {}])
@pytest.mark.asyncio
async def test_telegram_rechaza_update_id_invalido(update_id) -> None:
    # Preparación
    webhook, router, _sender = _webhook()
    update = _update(45, "hola")
    update["update_id"] = update_id

    # Actuar
    await webhook.process_update(update)

    # Verificación
    router.chat.assert_not_awaited()
    assert not webhook._tasks
    # El rechazo ocurre antes de la deduplicación: no consume un registro.
    assert not webhook._seen_updates


@pytest.mark.parametrize(
    "campo,objeto",
    [
        ("chat", {"id": "123"}),
        ("chat", {"id": 123.0}),
        ("chat", {"id": True}),
        ("chat", {"id": None}),
        ("chat", {}),
        ("from", {"id": "123"}),
        ("from", {"id": 123.0}),
        ("from", {"id": True}),
        ("from", {"id": None}),
        ("from", {}),
    ],
)
@pytest.mark.asyncio
async def test_telegram_rechaza_ids_internos_invalidos(campo, objeto) -> None:
    # Preparación
    webhook, router, _sender = _webhook()
    update = _update(46, "hola")
    update["message"][campo] = objeto

    # Actuar
    await webhook.process_update(update)

    # Verificación
    router.chat.assert_not_awaited()
    assert not webhook._tasks


@pytest.mark.parametrize("reply", ["texto", [], 123, True])
@pytest.mark.asyncio
async def test_telegram_rechaza_reply_to_message_no_objeto(reply) -> None:
    # Preparación
    webhook, router, _sender = _webhook()
    update = _update(47, "hola")
    update["message"]["reply_to_message"] = reply

    # Actuar
    await webhook.process_update(update)

    # Verificación
    router.chat.assert_not_awaited()
    assert not webhook._tasks


@pytest.mark.parametrize(
    "mutation",
    [
        {"from": None},
        {"from": "usuario"},
        {"from": []},
        {"from": 7},
        {"message": None},
        {"message": "mensaje"},
        {"message": {"chat": None}},
        {"message": {"chat": "chat"}},
        {"message": {"chat": []}},
        {"reply_to_message": "respuesta"},
        {"message": {"chat": {"id": 123}, "reply_to_message": 5}},
    ],
)
@pytest.mark.asyncio
async def test_telegram_rechaza_callback_query_con_shapes_internos_invalidos(mutation) -> None:
    # Preparación
    webhook, router, sender = _webhook()
    sender.answer_callback_query = AsyncMock()
    hitl = MagicMock()
    hitl.respond = AsyncMock(return_value=True)
    webhook._hitl = hitl
    callback = {
        "id": "callback-1",
        "from": {"id": 123},
        "message": {"chat": {"id": 123}, "message_id": 9},
        "data": "hitl:approve:token",
    }
    callback.update(mutation)

    # Actuar
    await webhook.process_update({"update_id": 48, "callback_query": callback})

    # Verificación: rechazo controlado, sin AttributeError y sin resolver HITL.
    hitl.respond.assert_not_awaited()
    router.chat.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Invalid callback")
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
