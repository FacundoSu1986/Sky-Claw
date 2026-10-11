"""Telegram Sender — rate-limited message delivery.

Sends responses back to Telegram chats with per-chat rate limiting
(max 20 messages/minute) and automatic splitting of long messages.
Also attaches files (:meth:`TelegramSender.send_document`) and honours the Bot
API flood-control (HTTP 429 + ``retry_after``) with a bounded retry.
All outbound traffic goes through :class:`NetworkGateway`.

La subida de documentos sigue una política acotada: un máximo operativo derivado
(:data:`MAX_DOCUMENT_BYTES`) consistente con el deadline del gateway, y una cota
total de duración (:data:`MAX_UPLOAD_TOTAL_SECONDS`) que ``send_document`` IMPONE
— no sólo documenta — sobre la operación completa (rate limit local + reintentos
por 429 + hops de redirección).
"""

from __future__ import annotations

import asyncio
import collections
import io
import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from sky_claw.app.security.network_gateway import DEFAULT_REQUEST_TIMEOUT

if TYPE_CHECKING:
    from sky_claw.app.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)


def _deadline_de_subida(total: float | None) -> float:
    """``total`` del gateway como deadline positivo y finito de la política de subida.

    Falla RUIDOSAMENTE si el gateway quedara sin deadline. Un ``or 0.0`` silencioso
    derivaría un presupuesto negativo y un :data:`MAX_DOCUMENT_BYTES` absurdo: el
    rechazo se leería como "documento demasiado grande" cuando el defecto real es
    "la política no se pudo derivar".
    """
    if total is None or not math.isfinite(total) or total <= 0:
        raise RuntimeError(
            f"telegram_sender: DEFAULT_REQUEST_TIMEOUT.total={total!r} no es un deadline finito y positivo; "
            "la política de subida no puede derivarse"
        )
    return float(total)


def _margen_de_conexion(timeout: aiohttp.ClientTimeout) -> float:
    """Segundos del deadline reservados al establecimiento de la conexión.

    aiohttp usa ``total`` cuando ``connect`` es ``None``, así que ese es el margen
    REAL que hay que reservar. Reservar de menos (p. ej. un 5.0 fijo mientras
    ``connect`` permite 10 s) publica un máximo que no entra en el deadline cuando
    el handshake consume su presupuesto completo.
    """
    margen = timeout.connect if timeout.connect is not None else timeout.total
    if margen is None or not math.isfinite(margen) or margen <= 0:
        raise RuntimeError(f"telegram_sender: DEFAULT_REQUEST_TIMEOUT.connect={margen!r} no es un margen válido")
    return float(margen)


def _presupuesto_de_transferencia(deadline: float, margen: float) -> float:
    """Presupuesto de transferencia de bytes, o fallo RUIDOSO si no queda ninguno.

    Un presupuesto ≤ 0 derivaría ``MAX_DOCUMENT_BYTES = 0``: la política rechazaría
    TODO documento y el síntoma ("documento demasiado grande") no señalaría la causa.
    """
    presupuesto = deadline - margen
    if presupuesto <= 0:
        raise RuntimeError(
            f"telegram_sender: el margen de conexión ({margen!r}s) consume el deadline ({deadline!r}s); "
            "no queda presupuesto de transferencia"
        )
    return presupuesto


TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/"
MAX_MESSAGE_LENGTH = 4096
MAX_MESSAGES_PER_MINUTE = 20

#: Ventana del rate limit local por chat. :meth:`TelegramSender._wait_for_rate_limit`
#: puede dormir casi esta ventana completa, así que forma parte de la cota TOTAL de
#: una subida (no de cada petición).
RATE_LIMIT_WINDOW_SECONDS = 60.0

#: Límite de ``caption`` de la Bot API (1024 caracteres tras el parseo).
MAX_CAPTION_LENGTH = 1024

#: Tope de ``send_document`` según la Bot API (50 MB para bots). Es el límite del
#: PROTOCOLO, no el de Sky-Claw: el multipart agrega bytes de framing y, sobre todo,
#: el :class:`NetworkGateway` corta la subida en su deadline. Ver
#: :data:`MAX_DOCUMENT_BYTES`.
TELEGRAM_BOT_API_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

#: Deadline por hop que el :class:`NetworkGateway` aplica cuando el caller no fija
#: uno. La política de subida lo toma como TECHO: un documento que no entre en este
#: presupuesto se corta con ``NetworkGatewayTimeoutError``.
UPLOAD_DEADLINE_SECONDS = _deadline_de_subida(DEFAULT_REQUEST_TIMEOUT.total)

#: Margen dentro del deadline reservado al establecimiento de la conexión — el
#: ``connect`` REAL del gateway, no un valor fijo. Lo que queda es presupuesto de
#: transferencia de bytes.
UPLOAD_SETUP_MARGIN_SECONDS = _margen_de_conexion(DEFAULT_REQUEST_TIMEOUT)

#: Presupuesto efectivo de transferencia de bytes de una subida.
UPLOAD_TRANSFER_BUDGET_SECONDS = _presupuesto_de_transferencia(UPLOAD_DEADLINE_SECONDS, UPLOAD_SETUP_MARGIN_SECONDS)

#: Throughput mínimo que la política ASUME para un enlace de salida (~2 Mbit/s).
#: Es un SUPUESTO de la estimación, no una propiedad del enlace: por debajo de esto
#: la subida NO está garantizada y el gateway la corta con
#: ``NetworkGatewayTimeoutError``. El máximo publicado es, por lo tanto, una
#: ESTIMACIÓN bajo este supuesto y el de latencia de
#: :data:`UPLOAD_RESIDUAL_SLACK_SECONDS`.
MIN_UPLOAD_THROUGHPUT_BYTES_PER_SECOND = 256 * 1024

#: Unidad a la que se trunca (HACIA ABAJO) el máximo publicado.
_CUANTIZACION_MAX_DOCUMENT_BYTES = 1024 * 1024

#: Máximo operativo de ``send_document``. DERIVADO del presupuesto de subida y del
#: throughput mínimo asumido — no un número mágico — para que el límite publicado
#: sea consistente con el deadline del gateway (defecto de #705: 45 MiB exigían
#: ≈1 MiB/s sostenido y el contrato era internamente inconsistente). Se trunca a un
#: múltiplo entero de MiB hacia abajo para no publicar un borde que consuma el
#: presupuesto hasta el último segundo. Queda muy por debajo del tope de la Bot API.
#: El caller adjunta la COLA del log, no el archivo.
#:
#: Es una ESTIMACIÓN bajo los supuestos de throughput y latencia de esta política,
#: NO una garantía de éxito en cualquier red: dice "este tamaño entra en el deadline
#: si el enlace sostiene el throughput asumido y la latencia cabe en la holgura
#: residual", no "cualquier red lo sube".
MAX_DOCUMENT_BYTES = (
    int(UPLOAD_TRANSFER_BUDGET_SECONDS * MIN_UPLOAD_THROUGHPUT_BYTES_PER_SECOND) // _CUANTIZACION_MAX_DOCUMENT_BYTES
) * _CUANTIZACION_MAX_DOCUMENT_BYTES

#: Holgura residual dentro del deadline que la política NO asigna a transferencia.
#: Es el margen implícito para la latencia de ida y vuelta, el framing del multipart
#: y el tiempo de respuesta del endpoint: el máximo publicado se transmite al
#: throughput mínimo asumido y lo que sobra hasta el deadline es esta holgura. Debe
#: ser ESTRICTAMENTE positiva; si se agotara, ``MAX_DOCUMENT_BYTES`` dejaría de ser
#: una estimación conservadora y pasaría a ser una promesa que la red puede romper.
UPLOAD_RESIDUAL_SLACK_SECONDS = (
    UPLOAD_DEADLINE_SECONDS - UPLOAD_SETUP_MARGIN_SECONDS - MAX_DOCUMENT_BYTES / MIN_UPLOAD_THROUGHPUT_BYTES_PER_SECOND
)

#: Reintentos tras un 429 (además del intento inicial). Acotado a propósito: un
#: aviso de fallo que no entra tras ``1 + N`` intentos se reporta, no se encola
#: indefinidamente detrás del flood-control.
_MAX_REINTENTOS_429 = 3

#: ``retry_after`` por encima de este tope NO se espera: bloquearía la tarea del
#: caller (un HITL, un aviso de pipeline) por un plazo que ya no es "un instante".
_RETRY_AFTER_MAX_SEGUNDOS = 30.0

#: Tope de sanidad para la autoridad compartida (24 horas): evita desbordes con
#: valores absurdos pero preserva restricciones reales del servidor como retry_after=600s.
_SANIDAD_MAX_COOLDOWN_SEGUNDOS = 86400.0

#: Espera cuando Telegram responde 429 sin ``retry_after`` legible.
_RETRY_AFTER_POR_DEFECTO = 1.0

#: Presupuesto máximo acumulado de espera por cooldown en mensajes de texto
#: (cubre reintentos consecutivos al límite máximo sin colgar indefinidamente la tarea).
_MAX_COOLDOWN_TOTAL_TEXTO_SEGUNDOS = 90.0

#: Cota TOTAL de una subida — que :meth:`TelegramSender.send_document` IMPONE, no
#: sólo documenta, con un ``asyncio.timeout`` alrededor de TODA la operación:
#:   espera del rate limit local + (1 + reintentos) × deadline por intento
#:   + Σ esperas de ``retry_after``.
#: Los hops de redirección quedan SUBORDINADOS a esta cota: cada hop tiene su propio
#: deadline, pero el acumulado no puede excederla.
MAX_UPLOAD_TOTAL_SECONDS = (
    RATE_LIMIT_WINDOW_SECONDS
    + (1 + _MAX_REINTENTOS_429) * UPLOAD_DEADLINE_SECONDS
    + _MAX_REINTENTOS_429 * _RETRY_AFTER_MAX_SEGUNDOS
)

_NOMBRE_DE_ARCHIVO_MAX = 100
_NOMBRE_DE_ARCHIVO_INVALIDO = re.compile(r"[^A-Za-z0-9._ -]")


class TelegramSendError(Exception):
    """Raised when a Telegram sendMessage call fails."""


class TelegramRateLimitError(TelegramSendError):
    """Flood-control de Telegram (HTTP 429) que no se pudo absorber con reintentos.

    Subclase de :class:`TelegramSendError`: los callers existentes siguen
    atrapándola. ``retry_after`` (segundos) permite a un consumidor,
    como el notificador de progreso (T3), decidir qué hacer:

    Contrato del consumidor T3 ante reintentos agotados:
    - **Avisos críticos** (fallo terminal del ritual, estado final, solicitud HITL):
      NUNCA asumir entrega exitosa. Preservar este error tipado y notificar la falla
      de entrega a través de un canal observable alternativo (e.g. estado de corrida en
      GUI/CLI o archivo de auditoría) con reintento acotado si el productor tiene cola propia.
    - **Actualizaciones no críticas** (progreso continuo, /lod_status):
      Descartar explícitamente la actualización obsoleta; no reprogramar ni encolar
      indefinidamente. Su ventana de validez expira antes de que venza el cooldown.
    - El cooldown compartido ya queda publicado en el sender, por lo que envíos
      posteriores no golpearán la API de Telegram durante la ventana anunciada.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float,
        motivo: str | None = None,
        presupuesto_agotado: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.motivo = motivo
        self.presupuesto_agotado = presupuesto_agotado


class TelegramUploadTimeoutError(TelegramSendError):
    """La subida excedió la cota TOTAL de la política (:data:`MAX_UPLOAD_TOTAL_SECONDS`).

    Subclase de :class:`TelegramSendError`: los callers existentes la siguen
    atrapando. Distingue "se agotó el acumulado de la operación" (rate limit local
    + reintentos + hops) de "el gateway cortó un hop"
    (:class:`~sky_claw.app.security.network_gateway.NetworkGatewayTimeoutError`):
    son fallos distintos y el caller puede querer tratarlos distinto.
    """


def _como_segundos(valor: object) -> float | None:
    """``valor`` como segundos finitos y no negativos, o ``None``."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        return None
    try:
        segundos = float(valor)
    except ValueError:
        return None
    if not math.isfinite(segundos) or segundos < 0:
        return None
    return segundos


def _retry_after_de(headers: Mapping[str, str] | None, cuerpo: str) -> float:
    """Segundos de espera pedidos por Telegram ante un 429.

    Orden: ``parameters.retry_after`` del cuerpo JSON (es el contrato de la Bot
    API), luego el header ``Retry-After``, luego un default. Un valor ilegible
    (no numérico, negativo, ``nan``/``inf``) cuenta como ausente: nunca se duerme
    un plazo que no se pudo validar.
    """
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        datos = None
    parametros = datos.get("parameters") if isinstance(datos, dict) else None
    candidato = _como_segundos(parametros.get("retry_after")) if isinstance(parametros, dict) else None
    if candidato is None and headers is not None:
        candidato = _como_segundos(headers.get("Retry-After"))
    return candidato if candidato is not None else _RETRY_AFTER_POR_DEFECTO


def _nombre_de_archivo_seguro(nombre: str) -> str:
    """Nombre de archivo apto para el multipart: sin rutas, sin control, acotado.

    El nombre viaja en el header ``Content-Disposition`` y el destinatario lo ve:
    se descartan los componentes de ruta (``/`` y ``\\``), se reemplaza todo lo que
    no sea ``[A-Za-z0-9._ -]`` y se conserva la COLA del nombre cuando excede el
    máximo, porque ahí vive la extensión.
    """
    base = re.split(r"[\\/]", nombre.strip())[-1]
    base = _NOMBRE_DE_ARCHIVO_INVALIDO.sub("_", base)[-_NOMBRE_DE_ARCHIVO_MAX:]
    return base if base.strip(". ") else "log.txt"


class CooldownCompartido:
    """Autoridad de flood-control (HTTP 429) compartida en memoria para un bot.

    Coordina corrutinas concurrentes dentro de la misma instancia de :class:`TelegramSender`:
    - Las fechas de vencimiento (deadlines) se calculan a partir de :func:`time.monotonic`.
    - Ámbito por chat: ``chat_id`` numérico aísla la restricción si se indica ámbito 'chat'.
    - Ámbito bot/global: política conservadora por defecto ante 429 ambiguos (broadcast/rate de bot).
    - Extensión monotónica: publicar un nuevo deadline nunca acorta uno existente mayor.
    - Preservación de deadline: la autoridad retiene el plazo completo devuelto por Telegram
      (hasta una cota de sanidad :data:`_SANIDAD_MAX_COOLDOWN_SEGUNDOS`), mientras que los llamadores
      usan presupuestos finitos para fallar rápido sin golpear la API.
    - Purgado amortizado: se limpian los chats vencidos periódicamente sin hilos de fondo.
    - Espera cancelable y presupuestada: reevalúa ``deadline_para()`` tras cada suspensión
      para no depender de contadores de versión.
    """

    def __init__(self) -> None:
        self._deadlines: dict[int | None, float] = {}
        self._operaciones: int = 0

    def _purgar_vencidos(self, ahora: float) -> None:
        vencidos = [cid for cid, d in self._deadlines.items() if d <= ahora]
        for cid in vencidos:
            del self._deadlines[cid]

    def publicar(
        self,
        chat_id: int | None = None,
        espera_segundos: float = _RETRY_AFTER_POR_DEFECTO,
        *,
        ambito: str = "global",
    ) -> float:
        """Publica o extiende el deadline de forma monotónica.

        Ante respuestas 429 estándar de Telegram donde no se puede demostrar que el límite
        es exclusivamente local a un chat, se aplica por defecto ``ambito='global'``
        afectando a todos los envíos del bot. Un ámbito ``'chat'`` explícito aísla
        la restricción al ``chat_id`` indicado.
        """
        ahora = time.monotonic()
        self._purgar_vencidos(ahora)
        espera_acotada = min(max(espera_segundos, 0.0), _SANIDAD_MAX_COOLDOWN_SEGUNDOS)
        nuevo_deadline = ahora + espera_acotada
        if chat_id is not None:
            self._deadlines[chat_id] = max(self._deadlines.get(chat_id, 0.0), nuevo_deadline)
        if ambito == "global" or chat_id is None:
            self._deadlines[None] = max(self._deadlines.get(None, 0.0), nuevo_deadline)
        return max(self._deadlines.get(chat_id, 0.0), self._deadlines.get(None, 0.0))

    def deadline_para(self, chat_id: int | None) -> float:
        """Deadline efectivo para ``chat_id`` considerando el límite por chat y el global."""
        d_chat = self._deadlines.get(chat_id, 0.0) if chat_id is not None else 0.0
        d_global = self._deadlines.get(None, 0.0)
        return max(d_chat, d_global)

    def restante(self, chat_id: int | None = None) -> float:
        """Segundos restantes de cooldown para ``chat_id``."""
        ahora = time.monotonic()
        return max(self.deadline_para(chat_id) - ahora, 0.0)

    async def esperar(
        self,
        chat_id: int | None = None,
        presupuesto: float | None = _MAX_COOLDOWN_TOTAL_TEXTO_SEGUNDOS,
    ) -> None:
        """Espera al deadline compartido vigente para ``chat_id``.

        Si la espera requerida excede ``presupuesto``, lanza :class:`TelegramRateLimitError`.
        El bucle reevalúa ``deadline_para(chat_id)`` tras cada despertar, garantizando que
        extensiones concurrentes se respeten sin falsas salidas.
        """
        total_dormido = 0.0
        while True:
            ahora = time.monotonic()
            d = self.deadline_para(chat_id)
            restante = d - ahora
            if restante <= 0.0:
                break
            if presupuesto is not None and (total_dormido + restante) > presupuesto:
                raise TelegramRateLimitError(
                    f"El cooldown compartido ({restante:.1f}s) excede el presupuesto disponible ({presupuesto:.1f}s)",
                    retry_after=restante,
                    motivo="COOLDOWN_EXCEEDS_OPERATION_BUDGET",
                    presupuesto_agotado=True,
                )
            dormir_secs = round(restante, 4)
            await asyncio.sleep(dormir_secs)
            total_dormido += restante


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    """Identidad mínima de un mensaje creado por ``sendMessage``."""

    chat_id: int
    message_id: int


class TelegramSender:
    """Rate-limited Telegram message sender.

    Args:
        bot_token: Telegram Bot API token.
        gateway: Network gateway for egress control.
        session: Shared aiohttp session.
        rate_limit: Max messages per minute per chat (default: 20).
    """

    def __init__(
        self,
        bot_token: str,
        gateway: NetworkGateway,
        session: aiohttp.ClientSession,
        rate_limit: int = MAX_MESSAGES_PER_MINUTE,
    ) -> None:
        self._token = bot_token
        self._gateway = gateway
        self._session = session
        self._rate_limit = rate_limit
        self._send_times: dict[int, collections.deque[float]] = {}
        self._cooldown = CooldownCompartido()
        self._url = TELEGRAM_API_BASE.format(token=bot_token)

    async def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> TelegramMessage:
        """Send a message to a Telegram chat.

        Automatically splits messages exceeding 4096 characters
        and respects per-chat rate limits.

        Args:
            chat_id: Telegram chat identifier.
            text: Message text to send.
            reply_markup: Optional inline keyboard or other markup.
            parse_mode: Optional Telegram parse mode (e.g. "MarkdownV2").

        Returns:
            The identity of the last message created.  HITL prompts are kept
            below the Telegram size limit, so this is the prompt's identity.

        Raises:
            TelegramSendError: On API or malformed-response failure.
        """
        chunks = self._split_message(text)
        last_message: TelegramMessage | None = None
        tiempo_inicio = time.monotonic()
        presupuesto_total = _MAX_COOLDOWN_TOTAL_TEXTO_SEGUNDOS
        for i, chunk in enumerate(chunks):
            await self._wait_for_rate_limit(chat_id)
            # Only add reply_markup to the last chunk if it's split
            markup = reply_markup if i == len(chunks) - 1 else None
            tiempo_transcurrido = time.monotonic() - tiempo_inicio
            presupuesto_remanente = max(presupuesto_total - tiempo_transcurrido, 0.0)
            last_message = await self._send_chunk(
                chat_id,
                chunk,
                markup,
                parse_mode,
                presupuesto_cooldown=presupuesto_remanente,
            )

        # ``_split_message`` siempre devuelve al menos un chunk, pero conservar
        # este guard explícito evita que un cambio futuro fabrique un mapping HITL
        # sin identidad de Telegram.
        if last_message is None:
            raise TelegramSendError("Telegram send produced no message identity")
        return last_message

    async def _send_chunk(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        *,
        presupuesto_cooldown: float | None = _MAX_COOLDOWN_TOTAL_TEXTO_SEGUNDOS,
    ) -> TelegramMessage:
        """Send a single message chunk via the Telegram API."""
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode or "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        return await self._enviar(
            "sendMessage",
            chat_id,
            lambda: {"json": payload},
            presupuesto_cooldown=presupuesto_cooldown,
        )

    async def send_document(
        self,
        chat_id: int,
        data: bytes,
        filename: str,
        *,
        caption: str | None = None,
    ) -> TelegramMessage:
        """Adjunta ``data`` como documento (``sendDocument``, multipart).

        Pensado para el log de una herramienta externa tras un fallo: el caller
        adjunta la COLA del archivo, no el archivo entero. El ``caption`` va como
        texto plano (sin ``parse_mode``): un log de DynDOLOD trae líneas
        ``<Error: ...>`` y el modo HTML de Telegram las rechazaría. El egreso pasa
        por :class:`NetworkGateway` igual que el resto del transporte.

        Política de tamaño y deadline: ``data`` no puede superar
        :data:`MAX_DOCUMENT_BYTES` (el máximo OPERATIVO de Sky-Claw, muy por debajo
        del tope de la Bot API). Ese máximo es una ESTIMACIÓN DERIVADA de
        :data:`UPLOAD_TRANSFER_BUDGET_SECONDS` y
        :data:`MIN_UPLOAD_THROUGHPUT_BYTES_PER_SECOND` bajo DOS supuestos: el
        throughput mínimo del enlace y la latencia de respuesta. Un documento
        admitido entra en el deadline del gateway si el enlace sostiene ese
        throughput y la latencia cabe en la holgura residual
        (:data:`UPLOAD_RESIDUAL_SLACK_SECONDS`). NO es una garantía de éxito en
        cualquier red: un enlace más lento, o con más latencia de la asumida, hace
        que el gateway corte la subida con ``NetworkGatewayTimeoutError`` — un fallo
        observable, nunca un éxito fabricado.

        Política de duración: TODA la operación (espera del rate limit local,
        reintentos por 429 y hops de redirección) está envuelta en un
        ``asyncio.timeout`` de :data:`MAX_UPLOAD_TOTAL_SECONDS`, así que la cota
        acumulada se IMPONE y no sólo se documenta. Una cancelación externa NO se
        convierte en timeout.

        El documento se envía como ``BytesIO`` (leído por trozos): un body grande
        no bloquea el event loop y la cancelación externa se procesa durante la
        subida. El costo es una copia transitoria del buffer (≤ 2× el documento).

        Raises:
            ValueError: ``data`` vacío o por encima de :data:`MAX_DOCUMENT_BYTES`
                (error del caller, se detecta antes de tocar la red).
            TelegramUploadTimeoutError: se agotó la cota total de la operación.
            TelegramRateLimitError: flood-control que los reintentos no absorbieron.
            TelegramSendError: cualquier otra falla de la API.
        """
        if not data:
            raise ValueError("send_document: el documento está vacío (Telegram rechaza archivos sin contenido)")
        if len(data) > MAX_DOCUMENT_BYTES:
            # El rechazo NO es silencioso: nombra el máximo, su unidad y la RAZÓN
            # (política DERIVADA del deadline). El máximo es más chico que el valor
            # fijo anterior, así que un caller que adjuntaba el log entero recibe
            # una acción concreta (mandar la COLA) en vez de un "demasiado grande"
            # sin explicación.
            raise ValueError(
                f"send_document: el documento excede el máximo operativo de {MAX_DOCUMENT_BYTES} bytes "
                f"({MAX_DOCUMENT_BYTES // _CUANTIZACION_MAX_DOCUMENT_BYTES} MiB), derivado del deadline de subida "
                f"({UPLOAD_DEADLINE_SECONDS:g}s) y del throughput mínimo asumido "
                f"({MIN_UPLOAD_THROUGHPUT_BYTES_PER_SECOND // 1024} KiB/s). Adjuntar la COLA del log."
            )

        nombre = _nombre_de_archivo_seguro(filename)
        texto = caption[:MAX_CAPTION_LENGTH] if caption else None

        def _formulario() -> dict[str, Any]:
            # ``FormData`` es de UN solo uso: tras un 429 el reintento arma uno nuevo.
            # El documento viaja como ``BytesIO``, NO como ``bytes`` crudos: aiohttp
            # marca un body >1 MB en bytes con un ``ResourceWarning`` ("might lock the
            # event loop"), que la política de tests del repo eleva a error, y es la
            # forma que aiohttp recomienda para cuerpos grandes. ``BytesIO`` se lee
            # por trozos (mejor para la cancelación) y es reconstruible por intento.
            #
            # Ciclo de vida: ``BytesIO`` no posee ningún recurso del SO (no hay fd
            # que cerrar); su único recurso es el buffer, que es una COPIA de
            # ``data``. ``data`` sigue vivo en el frame del caller durante toda la
            # llamada, así que el pico de memoria es 2× el documento se cierre o no
            # explícitamente, y en CPython el refcount lo libera al soltar el dict
            # del formulario. Un ``finally`` no bajaría el pico: sólo haría
            # determinista la liberación de memoria ya no referenciada.
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            if texto:
                form.add_field("caption", texto)
            form.add_field("document", io.BytesIO(data), filename=nombre, content_type="text/plain")
            return {"data": form}

        # Cota TOTAL de la operación, no sólo de cada intento: ``asyncio.timeout``
        # envuelve la espera del rate limit local, los reintentos por 429 y los hops
        # de redirección del gateway (cada hop tiene su deadline, pero el acumulado
        # queda acotado acá).
        tiempo_inicio = time.monotonic()
        cota = asyncio.timeout(MAX_UPLOAD_TOTAL_SECONDS)
        try:
            async with cota:
                await self._wait_for_rate_limit(chat_id)
                tiempo_transcurrido = time.monotonic() - tiempo_inicio
                presupuesto_remanente = max(MAX_UPLOAD_TOTAL_SECONDS - tiempo_transcurrido, 0.0)
                return await self._enviar(
                    "sendDocument",
                    chat_id,
                    _formulario,
                    presupuesto_cooldown=presupuesto_remanente,
                )
        except TimeoutError as exc:
            # Sólo el vencimiento PROPIO es la cota. ``asyncio.timeout`` re-lanza
            # encadenando (``raise ... from``) el ``CancelledError`` de SU propia
            # cancelación, así que el ``TimeoutError`` que produce él es el único
            # con un ``CancelledError`` como ``__cause__``. Un ``TimeoutError``
            # AJENO — incluso uno lanzado durante el desenrollado, cuando la cota
            # YA expiró — no lo lleva y se propaga intacto: ``expired()`` por sí
            # solo no distingue ese caso y reetiquetaría la causa real.
            if not cota.expired() or not isinstance(exc.__cause__, asyncio.CancelledError):
                raise
            raise TelegramUploadTimeoutError(
                f"send_document: la subida excedió la cota total de {MAX_UPLOAD_TOTAL_SECONDS:g}s "
                f"({_MAX_REINTENTOS_429} reintentos incluidos)"
            ) from exc
        except TelegramRateLimitError as exc:
            if exc.presupuesto_agotado or exc.retry_after > MAX_UPLOAD_TOTAL_SECONDS:
                raise TelegramUploadTimeoutError(
                    f"send_document: la subida excedió la cota total de {MAX_UPLOAD_TOTAL_SECONDS:g}s "
                    f"debido a flood-control ({exc.retry_after:g}s)"
                ) from exc
            raise

    async def _enviar(
        self,
        endpoint: str,
        chat_id: int,
        make_kwargs: Callable[[], dict[str, Any]],
        *,
        presupuesto_cooldown: float | None = _MAX_COOLDOWN_TOTAL_TEXTO_SEGUNDOS,
    ) -> TelegramMessage:
        """POST a ``endpoint`` con la política de flood-control compartida.

        UNA sola implementación para ``sendMessage`` y ``sendDocument``: la
        alternativa —repetir la lectura de la respuesta y el manejo del 429 en
        cada método— deja que un endurecimiento futuro aterrice en uno solo.
        ``make_kwargs`` se invoca en CADA intento porque un cuerpo multipart se
        consume al enviarse.

        Antes de CADA intento se consulta el cooldown COMPARTIDO
        (:class:`CooldownCompartido`): un 429 no duerme sólo a la corrutina que lo
        recibió, frena a todas las corrutinas del mismo chat antes de su próximo
        intento. Un 429 se reintenta hasta :data:`_MAX_REINTENTOS_429` veces con
        la espera acotada por :data:`_RETRY_AFTER_MAX_SEGUNDOS` y por el presupuesto
        de la operación. Cualquier otro ``status != 200`` es fallo duro.
        """
        url = self._url + endpoint
        retry_after = _RETRY_AFTER_POR_DEFECTO
        cuerpo_429 = ""
        tiempo_inicio = time.monotonic()
        for intento in range(_MAX_REINTENTOS_429 + 1):
            tiempo_restante = (
                max(presupuesto_cooldown - (time.monotonic() - tiempo_inicio), 0.0)
                if presupuesto_cooldown is not None
                else None
            )
            await self._cooldown.esperar(chat_id, presupuesto=tiempo_restante)
            resp = await self._gateway.request("POST", url, self._session, **make_kwargs())
            async with resp:
                if resp.status != 429:
                    return await self._interpretar_respuesta(resp, endpoint, chat_id)
                cuerpo_429 = await resp.text()
                retry_after = _retry_after_de(resp.headers, cuerpo_429)

            self._cooldown.publicar(chat_id, retry_after, ambito="global")

            if intento == _MAX_REINTENTOS_429 or retry_after > _RETRY_AFTER_MAX_SEGUNDOS:
                break
            logger.warning(
                "Telegram %s: flood-control (429), reintento %d/%d (espera compartida %.1fs)",
                endpoint,
                intento + 1,
                _MAX_REINTENTOS_429,
                self._cooldown.restante(chat_id),
            )

        raise TelegramRateLimitError(
            f"Telegram API returned 429: {cuerpo_429} (retry_after={retry_after:g}s)",
            retry_after=retry_after,
        )

    async def _interpretar_respuesta(
        self,
        resp: aiohttp.ClientResponse,
        endpoint: str,
        chat_id: int,
    ) -> TelegramMessage:
        """Valida una respuesta no limitada y registra el envío en el rate limit local."""
        if resp.status != 200:
            body = await resp.text()
            raise TelegramSendError(f"Telegram API returned {resp.status}: {body}")

        try:
            body = await resp.json()
        except Exception as exc:
            raise TelegramSendError(f"Telegram {endpoint} returned invalid JSON") from exc

        result = body.get("result") if isinstance(body, dict) and body.get("ok") else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            description = body.get("description") if isinstance(body, dict) else None
            detail = f": {description}" if description else ""
            raise TelegramSendError(f"Telegram {endpoint} returned no message_id{detail}")

        self._record_send(chat_id)
        return TelegramMessage(chat_id=chat_id, message_id=message_id)

    async def _wait_for_rate_limit(self, chat_id: int) -> None:
        """Block until we are within the per-chat rate limit."""
        if chat_id not in self._send_times:
            self._send_times[chat_id] = collections.deque()

        times = self._send_times[chat_id]
        now = time.monotonic()

        # Prune entries older than the rate-limit window.
        while times and now - times[0] > RATE_LIMIT_WINDOW_SECONDS:
            times.popleft()

        if len(times) >= self._rate_limit:
            wait_seconds = RATE_LIMIT_WINDOW_SECONDS - (now - times[0])
            if wait_seconds > 0:
                logger.debug("Rate limit for chat_id=%d, waiting %.1fs", chat_id, wait_seconds)
                await asyncio.sleep(wait_seconds)

    def _record_send(self, chat_id: int) -> None:
        """Record a send timestamp for rate limiting."""
        if chat_id not in self._send_times:
            self._send_times[chat_id] = collections.deque()
        self._send_times[chat_id].append(time.monotonic())

    @staticmethod
    def _split_message(text: str) -> list[str]:
        """Split text into chunks of at most MAX_MESSAGE_LENGTH chars.

        Tries to split on newline boundaries when possible.
        """
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]

        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= MAX_MESSAGE_LENGTH:
                chunks.append(remaining)
                break

            # Try to split at last newline within limit.
            split_at = remaining.rfind("\n", 0, MAX_MESSAGE_LENGTH)
            if split_at == -1:
                split_at = MAX_MESSAGE_LENGTH

            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")

        return chunks

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        """Responde a un ``callback_query`` (botón inline) vía el NetworkGateway.

        Cierra el estado de carga del botón en el cliente de Telegram. Como todo el
        egress, pasa por ``gateway.request`` (allow-list/SSRF/timeout/re-auth de
        redirects) y libera la respuesta con ``async with`` para no fugar la conexión.

        Participa del cooldown compartido: consulta el cooldown a nivel bot (sin chat_id)
        antes del intento y, si recibe un 429, publica la ventana a nivel bot para que
        ningún chat golpee la API durante la misma.
        """
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True

        url = self._url + "answerCallbackQuery"
        await self._cooldown.esperar(None)
        resp = await self._gateway.request(
            "POST",
            url,
            self._session,
            json=payload,
        )
        async with resp:
            if resp.status == 429:
                self._cooldown.publicar(None, _retry_after_de(resp.headers, await resp.text()))
                logger.warning("Telegram answerCallbackQuery: flood-control (429); cooldown del bot publicado")
            elif resp.status != 200:
                body = await resp.text()
                logger.error("Failed to answer callback query: %s", body)

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Edit an existing Telegram message.

        Consulta el cooldown compartido del chat antes del intento y, si recibe un 429,
        publica la ventana para que las demás corrutinas que envían a este chat esperen.
        """
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            # Telegram necesita un teclado inline vacío para retirar los botones;
            # omitir la clave deja el markup anterior intacto.
            "reply_markup": reply_markup if reply_markup is not None else {"inline_keyboard": []},
        }
        url = self._url + "editMessageText"
        await self._cooldown.esperar(chat_id)
        resp = await self._gateway.request(
            "POST",
            url,
            self._session,
            json=payload,
        )
        async with resp:
            if resp.status == 429:
                self._cooldown.publicar(chat_id, _retry_after_de(resp.headers, await resp.text()), ambito="global")
                logger.warning("Telegram editMessageText: flood-control (429); cooldown del bot publicado")
            elif resp.status != 200:
                body = await resp.text()
                logger.error("Failed to edit message: %s", body)
