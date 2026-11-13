"""Telegram rechaza respuestas del LLM con HTML ajeno (#918): ``<thought>…`` llegaba como «error interno».

El modo HTML de Telegram devuelve 400 «can't parse entities» ante una etiqueta que no soporta. Dos
capas, ambas ancladas acá:

1. ``quitar_razonamiento`` saca los bloques ``<thought>``/``<think>`` del texto del modelo.
2. ``_send_chunk`` (punto ÚNICO de ``sendMessage``) reintenta UNA vez en texto plano si Telegram
   responde 400 por entidades; cualquier otro 400 sigue siendo fallo duro.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.telegram_sender import (
    TelegramEntitiesError,
    TelegramSender,
    TelegramSendError,
    quitar_razonamiento,
)

_TOKEN = "123:ABC"
_DESCRIPCION_400 = 'Bad Request: can\'t parse entities: Unsupported start tag "thought" at byte offset 0'


def _respuesta(status: int, cuerpo: dict[str, Any]) -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.headers = {}
    resp.text = AsyncMock(return_value=json.dumps(cuerpo))
    resp.json = AsyncMock(return_value=cuerpo)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _ok() -> AsyncMock:
    return _respuesta(200, {"ok": True, "result": {"message_id": 7}})


def _400(descripcion: str = _DESCRIPCION_400) -> AsyncMock:
    return _respuesta(400, {"ok": False, "error_code": 400, "description": descripcion})


def _sender(*respuestas: AsyncMock) -> tuple[TelegramSender, MagicMock]:
    gateway = MagicMock()
    gateway.request = AsyncMock(side_effect=list(respuestas))
    sender = TelegramSender(bot_token=_TOKEN, gateway=gateway, session=MagicMock(spec=aiohttp.ClientSession))
    return sender, gateway


def _payloads(gateway: MagicMock) -> list[dict[str, Any]]:
    return [llamada.kwargs["json"] for llamada in gateway.request.await_args_list]


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("<thought>pienso</thought>Hola", "Hola"),
        ("<think>a\nb</think>\n\nHola", "Hola"),
        ("<THOUGHT>x</THOUGHT>Hola <b>mundo</b>", "Hola <b>mundo</b>"),
        ("Hola <thought>x</thought>mundo", "Hola mundo"),
        ("<thought>sin cierre\nHola", "sin cierre\nHola"),
        ("Hola sin etiquetas", "Hola sin etiquetas"),
    ],
)
def test_quitar_razonamiento(entrada: str, esperado: str) -> None:
    assert quitar_razonamiento(entrada) == esperado


def test_quitar_razonamiento_no_deja_el_mensaje_vacio() -> None:
    assert quitar_razonamiento("<thought>solo razonamiento</thought>") == "solo razonamiento"


async def test_400_por_entidades_reintenta_en_texto_plano_y_entrega() -> None:
    sender, gateway = _sender(_400(), _ok())

    mensaje = await sender.send(1, "<thought>x</thought>Hola")

    assert mensaje.message_id == 7
    primero, segundo = _payloads(gateway)
    assert primero["parse_mode"] == "HTML"
    assert "parse_mode" not in segundo and segundo["text"] == primero["text"]


async def test_400_por_entidades_con_reintento_fallido_propaga() -> None:
    sender, gateway = _sender(_400(), _400())

    with pytest.raises(TelegramEntitiesError):
        await sender.send(1, "<thought>x</thought>Hola")

    assert gateway.request.await_count == 2  # UN reintento, no un bucle


async def test_otro_400_no_se_reintenta() -> None:
    sender, gateway = _sender(_400("Bad Request: chat not found"))

    with pytest.raises(TelegramSendError) as info:
        await sender.send(1, "Hola")

    assert not isinstance(info.value, TelegramEntitiesError)
    assert gateway.request.await_count == 1


async def test_el_reintento_respeta_el_reply_markup() -> None:
    sender, gateway = _sender(_400(), _ok())
    markup = {"inline_keyboard": [[{"text": "Aprobar", "callback_data": "x"}]]}

    await sender.send(1, "<thought>x</thought>Hola", reply_markup=markup)

    assert all(p["reply_markup"] == markup for p in _payloads(gateway))


async def test_process_bg_entrega_la_respuesta_sin_el_bloque_de_razonamiento() -> None:
    """#918, extremo a extremo: el texto que llega al sender no trae ``<thought>`` ni «error interno»."""
    from sky_claw.app.comms.telegram import TelegramWebhook

    router, sender = MagicMock(), MagicMock()
    router.chat = AsyncMock(return_value="<thought>razono</thought>Hola, soy Sky-Claw")
    sender.send = AsyncMock()
    handler = TelegramWebhook(router=router, sender=sender, session=MagicMock(spec=aiohttp.ClientSession))

    await handler._process_bg(5, "/start", 1)

    sender.send.assert_awaited_once_with(5, "Hola, soy Sky-Claw")
