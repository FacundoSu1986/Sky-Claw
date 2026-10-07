"""Adaptador Telegram del canal de operador (``CanalDeOperador``).

El notificador de DynDOLOD no conoce Telegram: habla con un puerto de dos
métodos. Este adaptador lo traduce a ``TelegramSender`` resolviendo sender y chat
EN CADA envío (``AppContext`` los publica al final del arranque y los limpia al
cerrar: un adaptador que los capturara al construirse podría hablarle a un sender
ya cerrado).

A diferencia del HITL —que es fail-CLOSED: sin canal, deniega—, una notificación
informativa sin canal es un no-op: no hay decisión que proteger y un aviso no
puede gatear una corrida.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.comms import telegram_operator_channel as mod
from sky_claw.app.comms.telegram_operator_channel import TelegramCanalDeOperador
from sky_claw.app.comms.telegram_sender import TelegramSendError


def _sender() -> MagicMock:
    sender = MagicMock()
    sender.send = AsyncMock()
    sender.send_document = AsyncMock()
    return sender


class TestTelegramCanalDeOperador:
    async def test_avisar_envia_html_al_chat_del_operador(self) -> None:
        sender = _sender()
        canal = TelegramCanalDeOperador(sender=lambda: sender, chat_id=lambda: 456)

        await canal.avisar("<b>hola</b>")

        sender.send.assert_awaited_once_with(456, "<b>hola</b>", parse_mode="HTML")

    async def test_adjuntar_envia_el_documento_con_su_descripcion_como_caption(self) -> None:
        sender = _sender()
        canal = TelegramCanalDeOperador(sender=lambda: sender, chat_id=lambda: 456)

        await canal.adjuntar(nombre="DynDOLOD_SSE_log.txt", contenido=b"[00:01] Fatal: boom\n", descripcion="Cola")

        sender.send_document.assert_awaited_once_with(
            456, b"[00:01] Fatal: boom\n", "DynDOLOD_SSE_log.txt", caption="Cola"
        )

    @pytest.mark.parametrize(
        ("sender", "chat"),
        [(None, 456), ("sender", None), (None, None)],
        ids=["sin_sender", "sin_chat", "sin_nada"],
    )
    async def test_sin_sender_o_sin_chat_es_un_noop_que_se_declara_una_sola_vez(
        self, sender: str | None, chat: int | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        real = _sender() if sender else None
        canal = TelegramCanalDeOperador(sender=lambda: real, chat_id=lambda: chat)

        with caplog.at_level(logging.INFO, logger=mod.__name__):
            await canal.avisar("uno")
            await canal.adjuntar(nombre="a_log.txt", contenido=b"x", descripcion="d")
            await canal.avisar("dos")

        if real is not None:
            real.send.assert_not_awaited()
            real.send_document.assert_not_awaited()
        avisos = [r for r in caplog.records if "sin canal de operador" in r.getMessage()]
        assert len(avisos) == 1, "no se repite la advertencia en cada notificación"

    async def test_resuelve_sender_y_chat_en_cada_envio(self) -> None:
        primero, segundo = _sender(), _sender()
        actual = {"sender": primero, "chat": 1}
        canal = TelegramCanalDeOperador(sender=lambda: actual["sender"], chat_id=lambda: actual["chat"])  # type: ignore[arg-type,return-value]

        await canal.avisar("a")
        actual.update(sender=segundo, chat=2)
        await canal.avisar("b")

        primero.send.assert_awaited_once_with(1, "a", parse_mode="HTML")
        segundo.send.assert_awaited_once_with(2, "b", parse_mode="HTML")

    async def test_un_error_de_transporte_se_propaga_para_que_el_notificador_lo_registre(self) -> None:
        sender = _sender()
        sender.send.side_effect = TelegramSendError("Telegram API returned 400: can't parse entities")
        canal = TelegramCanalDeOperador(sender=lambda: sender, chat_id=lambda: 456)

        with pytest.raises(TelegramSendError, match="400"):
            await canal.avisar("<mal>")

    async def test_un_error_de_transporte_al_adjuntar_tambien_se_propaga(self) -> None:
        """El hermano de ``avisar``: un 413/429 agotado en ``send_document`` no puede tragarse acá."""
        sender = _sender()
        sender.send_document.side_effect = TelegramSendError("Telegram API returned 413: Request Entity Too Large")
        canal = TelegramCanalDeOperador(sender=lambda: sender, chat_id=lambda: 456)

        with pytest.raises(TelegramSendError, match="413"):
            await canal.adjuntar(nombre="DynDOLOD_SSE_log.txt", contenido=b"x", descripcion="d")
