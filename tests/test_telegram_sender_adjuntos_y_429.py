"""``TelegramSender``: adjuntar un documento y honrar el flood-control (HTTP 429).

Dos huecos que la auditoría de la etapa 9 midió sobre el transporte de Telegram:

1. **No había forma de adjuntar un archivo.** Ante un fallo de TexGen/DynDOLOD lo
   único que viajaba al operador eran las líneas terminales del log dentro del
   texto; el log completo (``DynDOLOD_SSE_log.txt``) no podía enviarse.
2. **Un 429 era un fallo duro.** ``_send_chunk`` trataba cualquier ``status != 200``
   como ``TelegramSendError``: durante una corrida larga, un aviso de fallo que
   pegaba contra el flood-control se perdía justo cuando más importaba. Telegram
   informa ``parameters.retry_after`` en el cuerpo (y a veces ``Retry-After``).

El multipart se verifica con un round-trip REAL contra un servidor aiohttp local
(loopback): mirar los campos privados de ``FormData`` no probaría que el cuerpo
que sale por el cable es el que Telegram parsea. El 429 se simula con respuestas
falsas y un ``sleep`` espiado — el tiempo no es la autoridad del test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sky_claw.app.comms import telegram_sender as sender_mod
from sky_claw.app.comms.telegram_sender import (
    TelegramMessage,
    TelegramRateLimitError,
    TelegramSender,
    TelegramSendError,
)

_TOKEN = "123:ABC"


def _respuesta(status: int, cuerpo: dict[str, Any] | str, headers: dict[str, str] | None = None) -> AsyncMock:
    """Respuesta falsa de ``gateway.request`` (soporta ``async with``)."""
    resp = AsyncMock()
    resp.status = status
    resp.headers = headers or {}
    texto = cuerpo if isinstance(cuerpo, str) else json.dumps(cuerpo)
    resp.text = AsyncMock(return_value=texto)
    resp.json = AsyncMock(return_value=json.loads(texto) if not isinstance(cuerpo, str) else {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _ok(message_id: int = 7) -> AsyncMock:
    return _respuesta(200, {"ok": True, "result": {"message_id": message_id}})


def _429(retry_after: int | None = 2, *, header: str | None = None) -> AsyncMock:
    cuerpo: dict[str, Any] = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 2"}
    if retry_after is not None:
        cuerpo["parameters"] = {"retry_after": retry_after}
    return _respuesta(429, cuerpo, {"Retry-After": header} if header is not None else None)


def _sender(*respuestas: AsyncMock) -> tuple[TelegramSender, MagicMock]:
    gateway = MagicMock()
    gateway.request = AsyncMock(side_effect=list(respuestas))
    return TelegramSender(bot_token=_TOKEN, gateway=gateway, session=MagicMock(spec=aiohttp.ClientSession)), gateway


@pytest.fixture
def dormidos() -> Any:
    """Espía ``asyncio.sleep`` del sender: el backoff se observa, no se espera."""
    esperas: list[float] = []

    async def _dormir(segundos: float) -> None:
        esperas.append(segundos)

    with patch.object(sender_mod.asyncio, "sleep", _dormir):
        yield esperas


# ---------------------------------------------------------------------------
# Flood-control: HTTP 429
# ---------------------------------------------------------------------------


class TestFloodControl429:
    async def test_un_429_se_reintenta_tras_esperar_el_retry_after_de_telegram(self, dormidos: list[float]) -> None:
        sender, gateway = _sender(_429(retry_after=3), _ok(11))

        resultado = await sender.send(456, "hola")

        assert resultado == TelegramMessage(chat_id=456, message_id=11)
        assert gateway.request.await_count == 2
        assert dormidos == [3.0]

    async def test_el_retry_after_cae_al_header_cuando_el_cuerpo_no_lo_trae(self, dormidos: list[float]) -> None:
        sender, _gateway = _sender(_429(retry_after=None, header="4"), _ok())

        await sender.send(456, "hola")

        assert dormidos == [4.0]

    async def test_sin_ninguna_pista_se_espera_un_segundo_por_defecto(self, dormidos: list[float]) -> None:
        sender, _gateway = _sender(_429(retry_after=None), _ok())

        await sender.send(456, "hola")

        assert dormidos == [1.0]

    async def test_un_429_persistente_agota_los_reintentos_y_lanza_el_error_tipado(self, dormidos: list[float]) -> None:
        # 1 intento + _MAX_REINTENTOS_429 reintentos, todos limitados.
        intentos = sender_mod._MAX_REINTENTOS_429 + 1
        sender, gateway = _sender(*[_429(retry_after=1) for _ in range(intentos)])

        with pytest.raises(TelegramRateLimitError, match="429") as exc_info:
            await sender.send(456, "hola")

        assert exc_info.value.retry_after == 1.0
        assert isinstance(exc_info.value, TelegramSendError), "debe seguir siendo un TelegramSendError"
        assert gateway.request.await_count == intentos
        assert len(dormidos) == intentos - 1, "no se duerme tras el último intento"

    async def test_un_retry_after_mayor_al_tope_no_se_espera_y_falla_de_inmediato(self, dormidos: list[float]) -> None:
        sender, gateway = _sender(_429(retry_after=int(sender_mod._RETRY_AFTER_MAX_SEGUNDOS) + 1))

        with pytest.raises(TelegramRateLimitError):
            await sender.send(456, "hola")

        assert gateway.request.await_count == 1
        assert dormidos == [], "un retry_after absurdo no puede bloquear la tarea del caller"

    @pytest.mark.parametrize("basura", ["abc", "-5", "nan", "inf", ""])
    async def test_un_retry_after_invalido_se_trata_como_ausente(self, basura: str, dormidos: list[float]) -> None:
        sender, _gateway = _sender(_429(retry_after=None, header=basura), _ok())

        await sender.send(456, "hola")

        assert dormidos == [1.0]

    async def test_otros_errores_http_siguen_siendo_fallo_duro_sin_reintento(self, dormidos: list[float]) -> None:
        sender, gateway = _sender(_respuesta(400, "Bad Request"))

        with pytest.raises(TelegramSendError, match="400"):
            await sender.send(456, "hola")

        assert gateway.request.await_count == 1
        assert dormidos == []

    async def test_la_cancelacion_durante_el_backoff_se_propaga(self) -> None:
        dentro_del_sleep = asyncio.Event()

        async def _dormir_para_siempre(_segundos: float) -> None:
            dentro_del_sleep.set()
            await asyncio.Event().wait()

        sender, _gateway = _sender(_429(retry_after=5), _ok())

        with patch.object(sender_mod.asyncio, "sleep", _dormir_para_siempre):
            tarea = asyncio.create_task(sender.send(456, "hola"))
            await asyncio.wait_for(dentro_del_sleep.wait(), timeout=5)
            tarea.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tarea

    async def test_el_429_no_cuenta_como_envio_para_el_rate_limit_local(self, dormidos: list[float]) -> None:
        sender, _gateway = _sender(_429(retry_after=1), _ok())

        await sender.send(456, "hola")

        assert len(sender._send_times[456]) == 1, "sólo el envío que Telegram aceptó ocupa un slot"


# ---------------------------------------------------------------------------
# send_document: multipart real contra un servidor local
# ---------------------------------------------------------------------------


class _ServidorDeTelegram:
    """Servidor aiohttp local que parsea el multipart como lo hace Telegram."""

    def __init__(self) -> None:
        self.recibidos: list[dict[str, Any]] = []
        self.ruta: str | None = None
        app = web.Application()
        app.router.add_post("/bot{token}/sendDocument", self._send_document)
        self.server = TestServer(app)

    async def _send_document(self, request: web.Request) -> web.Response:
        self.ruta = request.path
        campos = await request.post()
        documento = campos["document"]
        assert isinstance(documento, web.FileField)
        self.recibidos.append(
            {
                "chat_id": campos["chat_id"],
                "caption": campos.get("caption"),
                "parse_mode": campos.get("parse_mode"),
                "filename": documento.filename,
                "content_type": documento.content_type,
                "contenido": documento.file.read(),
            }
        )
        return web.json_response({"ok": True, "result": {"message_id": 99}})


@pytest.fixture
async def servidor() -> Any:
    telegram = _ServidorDeTelegram()
    await telegram.server.start_server()
    try:
        yield telegram
    finally:
        await telegram.server.close()


async def _sender_contra(servidor: _ServidorDeTelegram) -> tuple[TelegramSender, aiohttp.ClientSession]:
    """Un sender cuyo gateway reescribe la URL de Telegram hacia el servidor local."""
    sesion = aiohttp.ClientSession()

    async def _request(method: str, url: str, session: aiohttp.ClientSession, **kwargs: Any) -> Any:
        assert url.startswith("https://api.telegram.org/bot"), "el egreso debe apuntar al host de Telegram"
        local = str(servidor.server.make_url(url.removeprefix("https://api.telegram.org")))
        return await session.request(method, local, **kwargs)

    gateway = MagicMock()
    gateway.request = _request
    return TelegramSender(bot_token=_TOKEN, gateway=gateway, session=sesion), sesion


class TestSendDocument:
    async def test_envia_el_archivo_como_multipart_con_el_nombre_y_el_contenido_exactos(
        self, servidor: _ServidorDeTelegram
    ) -> None:
        sender, sesion = await _sender_contra(servidor)
        contenido = b"[00:47] TexGen completed successfully\n<Error: Deleted reference X>\n"
        try:
            resultado = await sender.send_document(456, contenido, "TexGen_SSE_log.txt", caption="TexGen falló")
        finally:
            await sesion.close()

        assert resultado == TelegramMessage(chat_id=456, message_id=99)
        assert servidor.ruta == f"/bot{_TOKEN}/sendDocument"
        [recibido] = servidor.recibidos
        assert recibido["chat_id"] == "456"
        assert recibido["filename"] == "TexGen_SSE_log.txt"
        assert recibido["contenido"] == contenido, "los bytes del log deben llegar intactos (incluidos los '<' del log)"
        assert recibido["caption"] == "TexGen falló"
        assert recibido["parse_mode"] is None, "el caption va como texto plano: el log trae '<Error: ...>'"

    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("../../etc/passwd", "passwd"),
            ("C:\\Logs\\DynDOLOD_SSE_log.txt", "DynDOLOD_SSE_log.txt"),
            ("log con espacios.txt", "log con espacios.txt"),
            ("bad\x00\nname<>.txt", "bad__name__.txt"),
            ("", "log.txt"),
            ("   ", "log.txt"),
            ("x" * 300 + ".txt", ("x" * 300 + ".txt")[-100:]),
        ],
    )
    def test_el_nombre_del_archivo_se_sanea(self, entrada: str, esperado: str) -> None:
        assert sender_mod._nombre_de_archivo_seguro(entrada) == esperado

    async def test_el_caption_se_trunca_al_limite_de_telegram(self, servidor: _ServidorDeTelegram) -> None:
        sender, sesion = await _sender_contra(servidor)
        try:
            await sender.send_document(456, b"x", "a.txt", caption="c" * 5000)
        finally:
            await sesion.close()

        assert len(servidor.recibidos[0]["caption"]) == sender_mod.MAX_CAPTION_LENGTH

    async def test_rechaza_un_documento_vacio_o_demasiado_grande_sin_tocar_la_red(self) -> None:
        sender, gateway = _sender()

        with pytest.raises(ValueError, match="vacío"):
            await sender.send_document(456, b"", "a.txt")
        with pytest.raises(ValueError, match="excede"):
            await sender.send_document(456, b"x" * (sender_mod.MAX_DOCUMENT_BYTES + 1), "a.txt")

        gateway.request.assert_not_called()

    async def test_un_429_al_adjuntar_reconstruye_el_formulario_y_reintenta(self, dormidos: list[float]) -> None:
        """``FormData`` es de un solo uso: el reintento tiene que armar uno NUEVO."""
        sender, gateway = _sender(_429(retry_after=2), _ok(55))

        resultado = await sender.send_document(456, b"contenido", "a.txt")

        assert resultado == TelegramMessage(chat_id=456, message_id=55)
        assert dormidos == [2.0]
        formularios = [llamada.kwargs["data"] for llamada in gateway.request.await_args_list]
        assert len(formularios) == 2
        assert formularios[0] is not formularios[1], "reusar el FormData consumido enviaría un cuerpo vacío"
        assert all(isinstance(f, aiohttp.FormData) for f in formularios)

    async def test_una_respuesta_sin_message_id_es_un_error_explicito(self) -> None:
        sender, _gateway = _sender(_respuesta(200, {"ok": False, "description": "Bad Request: file is too big"}))

        with pytest.raises(TelegramSendError, match="file is too big"):
            await sender.send_document(456, b"x", "a.txt")

    async def test_el_adjunto_respeta_el_rate_limit_por_chat(self, dormidos: list[float]) -> None:
        sender, _gateway = _sender(_ok(), _ok())
        sender._rate_limit = 1

        await sender.send_document(456, b"x", "a.txt")
        await sender.send_document(456, b"y", "b.txt")

        assert len(dormidos) == 1, "el segundo adjunto debe esperar el slot del rate limit local"
