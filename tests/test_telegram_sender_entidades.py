"""``TelegramSender``: un 400 «can't parse entities» no pierde el mensaje (#918).

El sender manda HTML por defecto. Cuando el texto trae una etiqueta que Telegram no
admite (``<thought>`` del modelo, o cualquier ``<Error: ...>`` de un log), la API
responde 400 y, antes, ``send`` lanzaba: el usuario recibía «error interno» en vez de
la respuesta. Ahora el sender reintenta UNA vez como texto plano — conserva texto,
``reply_markup`` y chat — y solo si el 400 es por entidades; cualquier otro 400 sigue
siendo un fallo duro, y un segundo fallo se propaga (no se fabrica un éxito).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.comms import telegram_sender as sender_mod
from sky_claw.app.comms.telegram_sender import TelegramMessage, TelegramSender, TelegramSendError

_TOKEN = "123:ABC"
_DESCRIPCION_ENTIDADES = 'Bad Request: can\'t parse entities: Unsupported start tag "thought" at byte offset 0'


def _respuesta(status: int, cuerpo: dict[str, Any]) -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.headers = {}
    texto = json.dumps(cuerpo)
    resp.text = AsyncMock(return_value=texto)
    resp.json = AsyncMock(return_value=cuerpo)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _ok(message_id: int = 7) -> AsyncMock:
    return _respuesta(200, {"ok": True, "result": {"message_id": message_id}})


def _400(descripcion: str = _DESCRIPCION_ENTIDADES) -> AsyncMock:
    return _respuesta(400, {"ok": False, "error_code": 400, "description": descripcion})


def _sender(*respuestas: AsyncMock) -> tuple[TelegramSender, MagicMock]:
    gateway = MagicMock()
    gateway.request = AsyncMock(side_effect=list(respuestas))
    sender = TelegramSender(bot_token=_TOKEN, gateway=gateway, session=MagicMock(spec=aiohttp.ClientSession))
    return sender, gateway


def _payloads(gateway: MagicMock) -> list[dict[str, Any]]:
    return [llamada.kwargs["json"] for llamada in gateway.request.await_args_list]


class TestReintentoSinFormatoAnteEntidadesInvalidas:
    async def test_un_400_por_entidades_se_reintenta_como_texto_plano_y_se_entrega(self) -> None:
        sender, gateway = _sender(_400(), _ok(message_id=42))

        entregado = await sender.send(10, "<thought>pienso</thought>\nHola")

        assert entregado == TelegramMessage(chat_id=10, message_id=42)
        primero, segundo = _payloads(gateway)
        assert primero["parse_mode"] == "HTML"
        assert "parse_mode" not in segundo
        assert segundo["text"] == primero["text"] == "<thought>pienso</thought>\nHola"
        assert segundo["chat_id"] == 10

    async def test_el_reintento_conserva_el_reply_markup(self) -> None:
        teclado = {"inline_keyboard": [[{"text": "Aprobar", "callback_data": "hitl:1"}]]}
        sender, gateway = _sender(_400(), _ok())

        await sender.send(10, "<Error: x>", reply_markup=teclado)

        _, segundo = _payloads(gateway)
        assert segundo["reply_markup"] == teclado

    async def test_tambien_cae_a_texto_plano_con_un_parse_mode_explicito(self) -> None:
        sender, gateway = _sender(_400(), _ok())

        await sender.send(10, "texto *roto", parse_mode="MarkdownV2")

        primero, segundo = _payloads(gateway)
        assert primero["parse_mode"] == "MarkdownV2"
        assert "parse_mode" not in segundo

    async def test_si_el_reintento_tambien_falla_el_error_se_propaga_sin_mas_intentos(self) -> None:
        sender, gateway = _sender(_400(), _400("Bad Request: message text is empty"))

        with pytest.raises(TelegramSendError, match="message text is empty"):
            await sender.send(10, "<thought>x</thought>")

        assert gateway.request.await_count == 2

    async def test_un_400_que_no_es_de_entidades_no_se_reintenta(self) -> None:
        sender, gateway = _sender(_400("Bad Request: chat not found"), _ok())

        with pytest.raises(TelegramSendError, match="chat not found"):
            await sender.send(10, "hola")

        assert gateway.request.await_count == 1

    async def test_un_envio_valido_no_toca_el_camino_de_reintento(self) -> None:
        sender, gateway = _sender(_ok(message_id=5))

        entregado = await sender.send(10, "<b>hola</b>")

        assert entregado.message_id == 5
        assert gateway.request.await_count == 1

    async def test_el_reintento_se_registra_como_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        sender, _ = _sender(_400(), _ok())

        with caplog.at_level(logging.WARNING, logger="sky_claw.app.comms.telegram_sender"):
            await sender.send(10, "<thought>x</thought>")

        assert any("texto plano" in r.getMessage() for r in caplog.records)

    async def test_un_mensaje_largo_reintenta_solo_el_fragmento_rechazado(self) -> None:
        # El primer fragmento es válido; el segundo trae la etiqueta inválida.
        largo = ("a" * 4000) + "\n" + "<thought>b</thought>" + ("c" * 200)
        sender, gateway = _sender(_ok(1), _400(), _ok(3))

        entregado = await sender.send(10, largo)

        assert entregado.message_id == 3
        payloads = _payloads(gateway)
        assert len(payloads) == 3
        assert "parse_mode" in payloads[0]
        assert "parse_mode" in payloads[1]
        assert "parse_mode" not in payloads[2]
        assert payloads[2]["text"] == payloads[1]["text"]


class TestElReintentoComparteElPresupuestoDeCooldown:
    async def test_el_reintento_descuenta_lo_que_el_primer_intento_ya_durmio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El tope de espera de ``send`` es del mensaje, no de cada intento: el plano no lo reinicia."""
        base = time.monotonic()
        reloj = [0.0]
        dormidos: list[float] = []

        async def _dormir(segundos: float) -> None:
            dormidos.append(segundos)
            reloj[0] += segundos

        monkeypatch.setattr(sender_mod.time, "monotonic", lambda: base + reloj[0])
        sender, _ = _sender(_400(), _ok())
        sender._cooldown.publicar(3)
        espiados: list[float | None] = []
        enviar_real = sender._enviar

        async def _enviar_espia(*args: Any, **kwargs: Any) -> Any:
            espiados.append(kwargs["presupuesto_cooldown"])
            return await enviar_real(*args, **kwargs)

        durmio: list[float] = []
        with patch.object(sender_mod.asyncio, "sleep", _dormir), patch.object(sender, "_enviar", _enviar_espia):
            await sender._send_chunk(
                10, "<thought>x</thought>", presupuesto_cooldown=5.0, on_cooldown_sleep=durmio.append
            )

        assert sum(durmio) == pytest.approx(3.0, abs=1e-2)
        assert espiados[0] == 5.0
        assert espiados[1] == pytest.approx(2.0, abs=1e-2)
