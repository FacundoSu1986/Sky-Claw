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
import math
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
    TelegramUploadTimeoutError,
)
from sky_claw.app.security import network_gateway as gateway_mod
from sky_claw.app.security.network_gateway import (
    EgressPolicy,
    NetworkGateway,
    NetworkGatewayTimeoutError,
)

_TOKEN = "123:ABC"

#: El ``TestServer`` de aiohttp capa el body en 1 MiB por defecto; la política de
#: #705 admite documentos más grandes, así que los servidores de prueba suben el cap.
_CLIENT_MAX_SIZE = 64 * 1024 * 1024


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
        app = web.Application(client_max_size=_CLIENT_MAX_SIZE)
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

    @pytest.mark.parametrize(
        ("crudo", "esperado"),
        [
            ("../../etc/passwd", "passwd"),
            ("C:\\Logs\\DynDOLOD_SSE_log.txt", "DynDOLOD_SSE_log.txt"),
        ],
    )
    async def test_el_saneo_esta_cableado_en_send_document(
        self, servidor: _ServidorDeTelegram, crudo: str, esperado: str
    ) -> None:
        """El nombre saneado es el que sale por el cable, no sólo el que devuelve la función.

        ``test_el_nombre_del_archivo_se_sanea`` cubre ``_nombre_de_archivo_seguro``
        aislada: si ``send_document`` deja de invocarla, ese test sigue verde y el
        nombre crudo del caller (una ruta con ``..``) viaja al ``Content-Disposition``.
        Este caso cierra el cableado end-to-end, contra el multipart real.
        """
        sender, sesion = await _sender_contra(servidor)
        try:
            await sender.send_document(456, b"x", crudo)
        finally:
            await sesion.close()

        assert servidor.recibidos[-1]["filename"] == esperado

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


# ---------------------------------------------------------------------------
# #705 — política de tamaño operativo
# ---------------------------------------------------------------------------


class TestPoliticaDeTamanoOperativo:
    """El máximo publicado debe ser TRANSMISIBLE dentro del deadline del gateway.

    Defecto de #705: ``MAX_DOCUMENT_BYTES = 45 MiB`` exige ≈1 MiB/s sostenido para
    entrar en los 45 s por defecto del :class:`NetworkGateway`. El contrato era
    internamente inconsistente. El máximo ahora se DERIVA de un presupuesto de
    subida y de un throughput mínimo asumido, y queda muy por debajo del tope de
    la Bot API (50 MB), que sigue siendo el límite del protocolo, no el nuestro.
    """

    def test_el_maximo_operativo_se_deriva_del_presupuesto_y_el_throughput(self) -> None:
        crudo = int(sender_mod.UPLOAD_TRANSFER_BUDGET_SECONDS * sender_mod.MIN_UPLOAD_THROUGHPUT_BYTES_PER_SECOND)
        cuantizado = (
            crudo // sender_mod._CUANTIZACION_MAX_DOCUMENT_BYTES
        ) * sender_mod._CUANTIZACION_MAX_DOCUMENT_BYTES
        assert cuantizado == sender_mod.MAX_DOCUMENT_BYTES, "el máximo debe ser un número DERIVADO, no mágico"

    def test_el_maximo_operativo_es_un_multiplo_exacto_de_la_cuantizacion(self) -> None:
        assert sender_mod.MAX_DOCUMENT_BYTES % sender_mod._CUANTIZACION_MAX_DOCUMENT_BYTES == 0

    def test_el_maximo_operativo_es_menor_que_el_de_la_bot_api(self) -> None:
        assert sender_mod.MAX_DOCUMENT_BYTES < sender_mod.TELEGRAM_BOT_API_MAX_DOCUMENT_BYTES

    def test_el_presupuesto_de_subida_entra_en_el_deadline_del_gateway(self) -> None:
        # Consistencia cross-módulo: si el gateway cambiara su deadline por defecto
        # sin revisar la política de subida, esta ancla rompe. El margen debe ser
        # ESTRICTAMENTE positivo (presupuesto < deadline).
        assert sender_mod.UPLOAD_TRANSFER_BUDGET_SECONDS < sender_mod.UPLOAD_DEADLINE_SECONDS
        assert gateway_mod.DEFAULT_REQUEST_TIMEOUT.total == sender_mod.UPLOAD_DEADLINE_SECONDS

    def test_el_margen_reservado_cubre_todo_el_connect_del_gateway(self) -> None:
        # El margen NO es un valor fijo elegido a mano: es el ``connect`` REAL del
        # gateway. Reservar menos (el 5.0 anterior) publicaba un máximo que no entra
        # en el deadline si el handshake consumía su presupuesto completo.
        assert gateway_mod.DEFAULT_REQUEST_TIMEOUT.connect == sender_mod.UPLOAD_SETUP_MARGIN_SECONDS

    def test_el_maximo_transmitido_mas_el_margen_entran_en_el_deadline(self) -> None:
        # La garantía que la política PUBLICA: un documento admitido, transmitido al
        # throughput mínimo asumido, cabe en el deadline aunque el handshake consuma
        # TODO su presupuesto.
        segundos_de_transferencia = sender_mod.MAX_DOCUMENT_BYTES / sender_mod.MIN_UPLOAD_THROUGHPUT_BYTES_PER_SECOND
        assert segundos_de_transferencia + sender_mod.UPLOAD_SETUP_MARGIN_SECONDS <= sender_mod.UPLOAD_DEADLINE_SECONDS

    def test_el_peor_caso_total_de_una_subida_esta_acotado(self) -> None:
        # La política debe acotar el ACUMULADO, no sólo cada petición: la espera del
        # rate limit local + intentos × deadline por intento + Σ esperas de
        # retry_after. Omitir la espera del rate limit dejaba fuera hasta 60 s.
        intentos = sender_mod._MAX_REINTENTOS_429 + 1
        esperado = (
            sender_mod.RATE_LIMIT_WINDOW_SECONDS
            + intentos * sender_mod.UPLOAD_DEADLINE_SECONDS
            + sender_mod._MAX_REINTENTOS_429 * sender_mod._RETRY_AFTER_MAX_SEGUNDOS
        )
        assert esperado == sender_mod.MAX_UPLOAD_TOTAL_SECONDS

    def test_la_cota_total_domina_la_espera_del_rate_limit_local(self) -> None:
        assert sender_mod.MAX_UPLOAD_TOTAL_SECONDS > sender_mod.RATE_LIMIT_WINDOW_SECONDS

    async def test_rechaza_un_byte_por_encima_del_maximo_sin_tocar_la_red(self) -> None:
        sender, gateway = _sender()

        with pytest.raises(ValueError, match="excede"):
            await sender.send_document(456, b"x" * (sender_mod.MAX_DOCUMENT_BYTES + 1), "a.txt")

        gateway.request.assert_not_called()

    async def test_rechaza_el_tamano_antiguo_de_45_mib_antes_de_la_red(self) -> None:
        # El límite previo (45 MiB) ya NO es aceptable: exigía ≈1 MiB/s para entrar
        # en el deadline. El rechazo ocurre antes de tocar la red (error del caller).
        sender, gateway = _sender()

        with pytest.raises(ValueError, match="excede"):
            await sender.send_document(456, b"x" * (45 * 1024 * 1024), "a.txt")

        gateway.request.assert_not_called()

    async def test_el_rechazo_por_tamano_explica_la_politica_y_la_accion(self) -> None:
        # El máximo es más chico que el valor fijo anterior: un caller que adjuntaba
        # el log ENTERO debe recibir la RAZÓN (política derivada del deadline) y la
        # ACCIÓN concreta (mandar la cola), no un "demasiado grande" sin explicación.
        sender, gateway = _sender()

        with pytest.raises(ValueError) as excinfo:
            await sender.send_document(456, b"x" * (sender_mod.MAX_DOCUMENT_BYTES + 1), "a.txt")

        mensaje = str(excinfo.value)
        assert str(sender_mod.MAX_DOCUMENT_BYTES) in mensaje
        assert "deadline de subida" in mensaje
        assert "COLA" in mensaje
        gateway.request.assert_not_called()

    async def test_acepta_exactamente_el_maximo_operativo(self, servidor: _ServidorDeTelegram) -> None:
        sender, sesion = await _sender_contra(servidor)
        contenido = b"x" * sender_mod.MAX_DOCUMENT_BYTES
        try:
            resultado = await sender.send_document(456, contenido, "log.txt")
        finally:
            await sesion.close()

        assert resultado == TelegramMessage(chat_id=456, message_id=99)
        assert servidor.recibidos[-1]["contenido"] == contenido, "el borde superior debe transmitirse entero"

    async def test_send_document_egresa_por_el_gateway(self) -> None:
        # No hay ruta alternativa: el adjunto pasa por gateway.request (allow-list,
        # método autorizado, timeout) como el resto del egress.
        sender, gateway = _sender(_ok())

        await sender.send_document(456, b"x", "a.txt")

        args, _kwargs = gateway.request.await_args
        assert args[0] == "POST"
        assert args[1].startswith("https://api.telegram.org/bot")
        assert args[1].endswith("/sendDocument")

    async def test_la_respuesta_se_libera_con_el_context_manager(self) -> None:
        # ``async with resp`` es lo que devuelve la conexión al pool. Si el bloque
        # deja de usarlo (p. ej. se cambia por un ``if``), la respuesta queda
        # retenida y el pool se agota bajo carga.
        resp = _ok()
        sender, _gateway = _sender(resp)

        await sender.send_document(456, b"x", "a.txt")

        resp.__aexit__.assert_awaited()


class TestDerivacionDeLaPolitica:
    """La derivación debe FALLAR RUIDOSAMENTE, no enmascarar una mala configuración.

    ``TestPoliticaDeTamanoOperativo`` ancla las constantes YA derivadas (se
    calcularon al importar). Estos tests ejercen la función de derivación con
    entradas DISTINTAS — incluida la mala configuración — de modo que el defecto
    (un ``or 0.0`` silencioso) se rechace sin depender de recargar el módulo.
    """

    @pytest.mark.parametrize("total", [None, 0, 0.0, -1.0, float("nan"), float("inf")])
    def test_un_deadline_ausente_o_no_finito_no_se_enmascara(self, total: float | None) -> None:
        with pytest.raises(RuntimeError, match="no es un deadline finito y positivo"):
            sender_mod._deadline_de_subida(total)

    def test_un_deadline_valido_se_conserva(self) -> None:
        assert sender_mod._deadline_de_subida(45.0) == 45.0

    def test_el_margen_es_el_connect_real_del_gateway(self) -> None:
        assert sender_mod._margen_de_conexion(aiohttp.ClientTimeout(total=45, connect=10)) == 10.0

    def test_el_margen_cae_al_total_cuando_connect_es_none(self) -> None:
        # aiohttp usa ``total`` si ``connect`` es ``None``: ese es el margen real.
        assert sender_mod._margen_de_conexion(aiohttp.ClientTimeout(total=45)) == 45.0

    @pytest.mark.parametrize("connect", [0, -1.0, float("nan")])
    def test_un_margen_invalido_no_se_enmascara(self, connect: float) -> None:
        with pytest.raises(RuntimeError, match="no es un margen válido"):
            sender_mod._margen_de_conexion(aiohttp.ClientTimeout(total=45, connect=connect))

    def test_un_presupuesto_no_positivo_no_se_enmascara(self) -> None:
        # Sin este guardia, ``connect == total`` derivaría MAX_DOCUMENT_BYTES = 0 y
        # TODA subida se rechazaría como "demasiado grande": el síntoma taparía la
        # causa real.
        with pytest.raises(RuntimeError, match="no queda presupuesto de transferencia"):
            sender_mod._presupuesto_de_transferencia(45.0, 45.0)

    def test_un_presupuesto_positivo_se_conserva(self) -> None:
        assert sender_mod._presupuesto_de_transferencia(45.0, 10.0) == 35.0


# ---------------------------------------------------------------------------
# #705 — deadline de subida sobre un enlace lento
# ---------------------------------------------------------------------------


class _ServidorConEnlaceLento:
    """Servidor local cuyo consumo del body simula un enlace de salida.

    ``bytes_por_segundo <= 0`` ⇒ enlace detenido: el handler nunca responde, así
    que la petición queda colgada hasta que el deadline del gateway la corta. Un
    valor positivo ⇒ el handler lee a esa tasa (la subida tarda ``size / rate``).
    """

    def __init__(self, bytes_por_segundo: float) -> None:
        self.rate = bytes_por_segundo
        self.leido = 0
        self.recibido = asyncio.Event()
        app = web.Application(client_max_size=_CLIENT_MAX_SIZE)
        app.router.add_post("/bot{token}/sendDocument", self._handler)
        self.server = TestServer(app)

    async def _handler(self, request: web.Request) -> web.Response:
        self.recibido.set()
        if self.rate <= 0:
            await asyncio.Event().wait()  # enlace que dejó de avanzar
        total = 0
        while True:
            trozo = await request.content.read(16 * 1024)
            if not trozo:
                break
            total += len(trozo)
            await asyncio.sleep(len(trozo) / self.rate)
        self.leido = total
        return web.json_response({"ok": True, "result": {"message_id": 42}})


@pytest.fixture
async def enlace() -> Any:
    """Fábrica de servidores de enlace lento; cierra todos al terminar el test."""
    servidores: list[_ServidorConEnlaceLento] = []

    async def _crear(bytes_por_segundo: float) -> _ServidorConEnlaceLento:
        servidor = _ServidorConEnlaceLento(bytes_por_segundo)
        await servidor.server.start_server()
        servidores.append(servidor)
        return servidor

    try:
        yield _crear
    finally:
        for servidor in servidores:
            await servidor.server.close()


def _gateway_loopback() -> NetworkGateway:
    """Gateway REAL con política que autoriza el servidor local (loopback)."""
    policy = EgressPolicy(
        allowed_hosts=frozenset(["127.0.0.1"]),
        allowed_methods={"127.0.0.1": frozenset(["POST"])},
        block_private_ips=False,
    )
    return NetworkGateway(policy)


def _sender_con_gateway_real(
    servidor: _ServidorConEnlaceLento, *, limite_del_pool: int = 100
) -> tuple[TelegramSender, aiohttp.ClientSession]:
    """Sender cuyo egreso pasa por el NetworkGateway REAL hacia el servidor local.

    ``limite_del_pool`` acota el pool de conexiones: con ``1``, una conexión
    retenida deja sin slot a la petición siguiente, así que el propio éxito de esa
    petición demuestra —por comportamiento PÚBLICO— que la anterior se liberó.
    """
    sesion = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=limite_del_pool))
    sender = TelegramSender(bot_token=_TOKEN, gateway=_gateway_loopback(), session=sesion)
    sender._url = str(servidor.server.make_url(f"/bot{_TOKEN}/"))
    return sender, sesion


class TestDeadlineDeSubida:
    """El deadline del gateway debe cortar una subida trabada, de forma observable.

    Se inyecta un deadline CORTO en el gateway en vez de esperar 45 s reales: la
    autoridad del corte es el deadline, no un ``sleep`` del test.
    """

    async def test_una_subida_que_deja_de_avanzar_se_corta_en_el_deadline(
        self, enlace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_mod, "DEFAULT_REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.25, connect=0.2))
        servidor = await enlace(0.0)
        sender, sesion = _sender_con_gateway_real(servidor)
        try:
            with pytest.raises(NetworkGatewayTimeoutError):
                await sender.send_document(456, b"x" * 4096, "log.txt")
        finally:
            await sesion.close()

    async def test_el_timeout_no_se_convierte_en_un_exito(self, enlace: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # Nunca debe devolverse un TelegramMessage si Telegram no confirmó nada.
        monkeypatch.setattr(gateway_mod, "DEFAULT_REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.25, connect=0.2))
        servidor = await enlace(0.0)
        sender, sesion = _sender_con_gateway_real(servidor)
        try:
            with pytest.raises(NetworkGatewayTimeoutError):
                await sender.send_document(456, b"x" * 4096, "log.txt")
        finally:
            await sesion.close()

    async def test_una_subida_lenta_pero_dentro_del_deadline_completa(
        self, enlace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_mod, "DEFAULT_REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=5.0, connect=1.0))
        # 128 KiB a 512 KiB/s ⇒ ~0.25 s de transferencia, holgadamente bajo el deadline.
        servidor = await enlace(512 * 1024)
        sender, sesion = _sender_con_gateway_real(servidor)
        contenido = b"x" * (128 * 1024)
        try:
            resultado = await sender.send_document(456, contenido, "log.txt")
        finally:
            await sesion.close()

        assert resultado == TelegramMessage(chat_id=456, message_id=42)
        # El servidor lee el body multipart COMPLETO (payload + framing), por eso
        # se compara por cota inferior: el payload tiene que llegar entero.
        assert servidor.leido >= len(contenido)

    async def test_la_cancelacion_durante_una_subida_se_propaga(self, enlace: Any) -> None:
        servidor = await enlace(0.0)
        sender, sesion = _sender_con_gateway_real(servidor)
        try:
            tarea = asyncio.create_task(sender.send_document(456, b"x" * 4096, "log.txt"))
            await asyncio.wait_for(servidor.recibido.wait(), timeout=5)
            tarea.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tarea
        finally:
            await sesion.close()

    async def test_el_timeout_devuelve_la_conexion_al_pool(self, enlace: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # Comportamiento PÚBLICO, sin leer atributos privados de aiohttp: con un
        # pool de UNA sola conexión, una conexión retenida dejaría sin slot a la
        # subida siguiente y ésta se agotaría esperando. Que la segunda subida
        # COMPLETE prueba que el fallo no dejó la sesión inservible.
        #
        # Alcance honesto: en la ruta de timeout la respuesta NUNCA se construye
        # (``gateway.request`` propaga antes de devolverla), así que lo que este
        # test ancla es "el estado del pool sobrevive al fallo" y que el fallo no
        # se convierte en un éxito fabricado — verificado con un mutante que sí lo
        # mata. NO pretende cubrir la liberación de una respuesta que no existe.
        monkeypatch.setattr(gateway_mod, "DEFAULT_REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.25, connect=0.2))
        detenido = await enlace(0.0)
        sender, sesion = _sender_con_gateway_real(detenido, limite_del_pool=1)
        try:
            with pytest.raises(NetworkGatewayTimeoutError):
                await sender.send_document(456, b"x" * 4096, "log.txt")

            # Deadline holgado para la segunda subida: el punto no es el tiempo,
            # sino que el slot del pool esté DISPONIBLE.
            monkeypatch.setattr(gateway_mod, "DEFAULT_REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=5.0, connect=1.0))
            vivo = await enlace(512 * 1024)
            sender._url = str(vivo.server.make_url(f"/bot{_TOKEN}/"))
            resultado = await sender.send_document(456, b"y" * 1024, "log.txt")
        finally:
            await sesion.close()

        assert resultado == TelegramMessage(chat_id=456, message_id=42)


# ---------------------------------------------------------------------------
# #705 — cota TOTAL de la subida (IMPONE, no sólo documenta)
# ---------------------------------------------------------------------------


class TestCotaTotalDeSubida:
    """``MAX_UPLOAD_TOTAL_SECONDS`` debe IMPONERSE sobre la operación completa.

    Defecto de la revisión: la constante se documentaba como cota acumulada pero
    nada la aplicaba — el gateway sólo acota CADA hop, así que la suma de rate
    limit local (≤ 60 s) + reintentos + hops de redirección no tenía techo. La
    autoridad de estos tests es la cota inyectada, no el tiempo real medido: el
    ``asyncio.timeout`` corta en cuanto vence, sin esperar el plazo nominal.
    """

    def test_la_cota_total_es_un_limite_finito_y_positivo(self) -> None:
        assert math.isfinite(sender_mod.MAX_UPLOAD_TOTAL_SECONDS)
        assert sender_mod.MAX_UPLOAD_TOTAL_SECONDS > 0

    def test_el_error_de_cota_lo_siguen_atrapando_los_callers_existentes(self) -> None:
        assert issubclass(TelegramUploadTimeoutError, TelegramSendError)

    async def test_la_espera_del_rate_limit_local_esta_dentro_de_la_cota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sender, _gateway = _sender(_ok(), _ok())
        sender._rate_limit = 1
        await sender.send_document(456, b"x", "a.txt")

        # El segundo adjunto quedaría ~60 s esperando el slot del rate limit local.
        monkeypatch.setattr(sender_mod, "MAX_UPLOAD_TOTAL_SECONDS", 0.05)

        with pytest.raises(TelegramUploadTimeoutError, match="cota total"):
            await sender.send_document(456, b"y", "b.txt")

    async def test_la_espera_del_backoff_429_esta_dentro_de_la_cota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sender, _gateway = _sender(_429(retry_after=30), _ok())
        monkeypatch.setattr(sender_mod, "MAX_UPLOAD_TOTAL_SECONDS", 0.05)

        # Sin la cota, este ``asyncio.sleep(30.0)`` del backoff bloquearía al caller.
        with pytest.raises(TelegramUploadTimeoutError, match="cota total"):
            await sender.send_document(456, b"x", "a.txt")

    async def test_la_cota_corta_la_subida_real_antes_que_el_deadline_del_hop(
        self, enlace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # La cota se IMPONE sobre la operación real (gateway real + servidor real):
        # con un enlace detenido y una cota más corta que el deadline del gateway,
        # el fallo reportado es el de la cota total, no el del hop.
        monkeypatch.setattr(sender_mod, "MAX_UPLOAD_TOTAL_SECONDS", 0.05)
        servidor = await enlace(0.0)
        sender, sesion = _sender_con_gateway_real(servidor)
        try:
            with pytest.raises(TelegramUploadTimeoutError, match="cota total"):
                await sender.send_document(456, b"x" * 4096, "log.txt")
        finally:
            await sesion.close()

    async def test_una_cancelacion_externa_no_se_reporta_como_vencimiento_de_la_cota(self, enlace: Any) -> None:
        # ``Timeout.expired()`` distingue NUESTRO vencimiento de una cancelación
        # ajena: si se re-etiquetara, el caller perdería la señal de cancelación.
        servidor = await enlace(0.0)
        sender, sesion = _sender_con_gateway_real(servidor)
        try:
            tarea = asyncio.create_task(sender.send_document(456, b"x" * 4096, "log.txt"))
            await asyncio.wait_for(servidor.recibido.wait(), timeout=5)
            tarea.cancel()
            with pytest.raises(asyncio.CancelledError) as excinfo:
                await tarea
            assert not isinstance(excinfo.value, TelegramUploadTimeoutError)
        finally:
            await sesion.close()

    async def test_un_timeout_ajeno_no_se_re_etiqueta_como_vencimiento_de_la_cota(self) -> None:
        # Sólo el vencimiento PROPIO es la cota: un ``TimeoutError`` de otra capa
        # debe propagarse intacto. Re-etiquetarlo taparía la causa real y haría que
        # el caller buscara el problema en la política de subida.
        gateway = MagicMock()
        gateway.request = AsyncMock(side_effect=TimeoutError("de otra capa"))
        sender = TelegramSender(bot_token=_TOKEN, gateway=gateway, session=MagicMock(spec=aiohttp.ClientSession))

        with pytest.raises(TimeoutError) as excinfo:
            await sender.send_document(456, b"x", "a.txt")

        assert not isinstance(excinfo.value, TelegramUploadTimeoutError)
        assert str(excinfo.value) == "de otra capa"
