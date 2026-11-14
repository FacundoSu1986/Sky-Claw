"""Telegram rechaza respuestas del LLM con HTML ajeno (#918): ``<thought>…`` llegaba como «error interno».

El modo HTML de Telegram devuelve 400 «can't parse entities» ante una etiqueta que no soporta. Dos
capas, con UN solo dueño cada una:

1. ``ocultar_razonamiento`` (``agent/reasoning_filter.py``) saca los bloques ``<thought>``/``<think>``
   en la salida de ``LLMRouter.chat``, para TODAS las superficies; sus tests viven en
   ``test_reasoning_filter.py`` y ``test_router_oculta_razonamiento.py``. Telegram ya no filtra por su cuenta.
2. ``_send_chunk`` (punto ÚNICO de ``sendMessage``) reintenta UNA vez en texto plano si Telegram
   responde 400 por entidades (red de seguridad para cualquier otro HTML ajeno); cualquier otro 400
   sigue siendo fallo duro. Se ancla acá.
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


async def test_process_bg_entrega_tal_cual_lo_que_devuelve_el_router() -> None:
    """El razonamiento lo oculta el router (un solo dueño): Telegram no vuelve a filtrar el texto.

    Si un ``<thought>`` llegara hasta acá por otro camino, el que lo cubre es el reintento en texto
    plano del sender, no un segundo filtro con reglas propias que divergen del del router.
    """
    from sky_claw.app.comms.telegram import TelegramWebhook

    router, sender = MagicMock(), MagicMock()
    router.chat = AsyncMock(return_value="<thought>razono</thought>Hola, soy Sky-Claw")
    sender.send = AsyncMock()
    handler = TelegramWebhook(router=router, sender=sender, session=MagicMock(spec=aiohttp.ClientSession))

    await handler._process_bg(5, "/start", 1)

    sender.send.assert_awaited_once_with(5, "<thought>razono</thought>Hola, soy Sky-Claw")


async def test_el_reintento_en_texto_plano_descuenta_el_cooldown_ya_consumido() -> None:
    """Codex P2 sobre #922: el reintento no puede reiniciar el presupuesto de espera de ``send()``."""
    sender, _gateway = _sender()
    presupuestos: list[float | None] = []
    acumulado: list[float] = []

    async def _enviar_falso(endpoint: str, chat_id: int, make_kwargs: Any, **kw: Any) -> Any:
        presupuestos.append(kw["presupuesto_cooldown"])
        if len(presupuestos) == 1:
            kw["on_cooldown_sleep"](30.0)  # el intento HTML durmió 30 s de flood-control
            raise TelegramEntitiesError("400 entidades")
        kw["on_cooldown_sleep"](10.0)
        return "ok"

    sender._enviar = _enviar_falso  # type: ignore[method-assign]

    resultado = await sender._send_chunk(1, "x", presupuesto_cooldown=90.0, on_cooldown_sleep=acumulado.append)

    assert resultado == "ok"
    assert presupuestos == [90.0, 60.0]  # 90 - 30 ya consumidos
    assert acumulado == [30.0, 10.0]  # el acumulador de send() ve las dos esperas
