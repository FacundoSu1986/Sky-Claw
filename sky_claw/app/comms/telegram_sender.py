"""Telegram Sender — rate-limited message delivery.

Sends responses back to Telegram chats with per-chat rate limiting
(max 20 messages/minute) and automatic splitting of long messages.
Also attaches files (:meth:`TelegramSender.send_document`) and honours the Bot
API flood-control (HTTP 429 + ``retry_after``) with a bounded retry.
All outbound traffic goes through :class:`NetworkGateway`.

La subida de documentos sigue una política acotada: un máximo operativo derivado
(:data:`MAX_DOCUMENT_BYTES`) consistente con el deadline del gateway, y una cota
total de duración (:data:`MAX_UPLOAD_TOTAL_SECONDS`) que ``send_document`` IMPONE
— no sólo documenta — sobre la operación completa (rate limit local + cooldowns
compartidos + reintentos por 429 + hops de redirección).

Flood-control COORDINADO (#706)
-------------------------------

Un 429 no duerme sólo a la corrutina que lo recibió: publica un deadline en
:class:`CooldownCompartido` —compartido por TODAS las corrutinas del mismo
:class:`TelegramSender`— que se consulta **antes de cada intento HTTP**. Una
ráfaga concurrente, entonces, no vuelve a golpear la API dentro de la ventana de
flood-control que Telegram ya anunció (antes, una ``send()`` hermana pasaba el
rate limit local de inmediato y agotaba reintentos ajenos). El alcance del aviso es
el del BOT (un 429 no es atribuible a un chat o al bot: ver
:class:`CooldownCompartido`), y las tres vías HTTP del módulo —``sendMessage``/
``sendDocument``, ``editMessageText`` y ``answerCallbackQuery``— participan de él.

El contrato del consumidor (T3 — notificaciones) cuando los reintentos acotados se
agotan está en :class:`TelegramRateLimitError`: descarte explícito de lo no
crítico, alerta por otra vía para lo terminal, y nunca un bucle de reintento
ilimitado ni un éxito fabricado.
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

#: Presupuesto máximo acumulado de espera por cooldown en mensajes de texto
#: (cubre reintentos consecutivos al límite máximo sin colgar indefinidamente la tarea).
_MAX_COOLDOWN_TOTAL_TEXTO_SEGUNDOS = 90.0

#: Presupuesto máximo de espera en acknowledgements de callback (botones inline).
#: Corto por contrato: el cliente Telegram vence la interacción visual en segundos.
_PRESUPUESTO_CALLBACK_SEGUNDOS = 10.0

#: Espera cuando Telegram responde 429 sin ``retry_after`` legible.
_RETRY_AFTER_POR_DEFECTO = 1.0

#: Espera MÁXIMA que el cooldown compartido puede imponer antes de CADA intento:
#: el aviso vigente (≤ :data:`_RETRY_AFTER_MAX_SEGUNDOS`) más UNA extensión nueva que
#: haya llegado mientras se dormía (≤ el tope).
_MAX_ESPERA_COMPARTIDA_POR_INTENTO = 2.0 * _RETRY_AFTER_MAX_SEGUNDOS

#: Cota TOTAL de una subida — que :meth:`TelegramSender.send_document` IMPONE, no
#: sólo documenta, con un ``asyncio.timeout`` alrededor de TODA la operación:
#:   espera del rate limit local + Σ por intento (deadline del hop + espera
#:   compartida máxima) + hops de redirección.
#: Los hops de redirección quedan SUBORDINADOS a esta cota: cada hop tiene su propio
#: deadline, pero el acumulado no puede excederla. Desde #706 la espera COMPARTIDA
#: del flood-control precede a cada intento (no sólo al primero), así que entra en la
#: cota: omitirla dejaría vencimientos propios cortando subidas que sólo esperaban un
#: 429 anunciado.
MAX_UPLOAD_TOTAL_SECONDS = RATE_LIMIT_WINDOW_SECONDS + (1 + _MAX_REINTENTOS_429) * (
    UPLOAD_DEADLINE_SECONDS + _MAX_ESPERA_COMPARTIDA_POR_INTENTO
)

_NOMBRE_DE_ARCHIVO_MAX = 100
_NOMBRE_DE_ARCHIVO_INVALIDO = re.compile(r"[^A-Za-z0-9._ -]")


class CooldownCompartido:
    """Autoridad COMPARTIDA del cooldown de flood-control (HTTP 429).

    Una instancia por :class:`TelegramSender`: todas las corrutinas que envían por
    el mismo bot comparten el objeto, así que un 429 que observa una NO sólo la
    duerme a ella — publica un deadline que las demás consultan antes de CADA
    intento HTTP.

    Cuatro decisiones, y por qué:

    - **Reloj monotónico.** El deadline se mide con ``time.monotonic``: un ajuste
      del reloj de pared (NTP, cambio de huso) no puede acortar ni alargar la espera.
    - **Un aviso no retrocede.** ``publicar`` extiende con ``max``, nunca reemplaza,
      así que dos corrutinas que reciben 429 con ``retry_after`` distinto convergen en
      el MÁS LEJANO. La carrera leer-y-reemplazar no existe acá porque la mutación es
      síncrona: no hay ``await`` entre leer y escribir y el event loop es mono-hilo,
      así que el deadline que ve un consumidor nunca es un valor a medio actualizar.
    - **Alcance del BOT, no del chat (criterio 3 de #706).** Un 429 no dice si el
      límite excedido fue el del chat o el global del bot, y el rate limit local por
      chat (20/min) ya acota el caso por-chat ANTES de tocar la red. Se publica
      entonces un único deadline del bot y lo honran TODOS los chats: esperar de más
      cuesta latencia acotada en un bot que atiende a un operador, mientras que golpear
      la API dentro de una ventana anunciada cuesta avisos perdidos.
    - **Preservación íntegra de retry_after y fail-fast presupuestado.** La autoridad
      retiene el deadline completo devuelto por el servidor (acotado a
      :data:`_SANIDAD_MAX_COOLDOWN_SEGUNDOS`), y los llamadores con presupuesto finito
      fallan rápido determinísticamente sin realizar solicitudes prematuras a la red.
    - **Sin locks.** No hay lock que pueda quedar sostenido durante una espera o una
      petición de red (criterio 8 de #706): la coordinación es un deadline y la espera
      es un ``asyncio.sleep`` cancelable.
    """

    __slots__ = ("_deadline", "_publicacion", "_publicaciones")

    def __init__(self) -> None:
        #: ``(deadline, publicación)`` vigente del alcance del bot.
        self._deadline: tuple[float, int] = (0.0, 0)
        #: Contador monotónico global de publicaciones: distingue "el deadline se
        #: movió" de "el reloj no avanzó lo suficiente" sin depender del reloj.
        self._publicaciones = 0

    def publicar(self, espera: float) -> float:
        """Registra un aviso de flood-control y devuelve el deadline vigente.

        ``espera`` es el ``retry_after`` informado por Telegram. La autoridad retiene
        la restricción íntegra del servidor (acotada a la política de sanidad
        :data:`_SANIDAD_MAX_COOLDOWN_SEGUNDOS`), mientras que los llamadores con
        presupuesto finito fallan rápido sin golpear la red.
        """
        espera_acotada = min(max(espera, 0.0), _SANIDAD_MAX_COOLDOWN_SEGUNDOS) if espera > 0 else 0.0
        deadline = max(time.monotonic() + espera_acotada, self._deadline[0])
        self._publicaciones += 1
        self._deadline = (deadline, self._publicaciones)
        return deadline

    def restante(self) -> float:
        """Segundos que restan del aviso vigente (0.0 si no hay ninguno)."""
        return max(0.0, self._deadline[0] - time.monotonic())

    async def esperar(self, presupuesto: float | None = _MAX_COOLDOWN_TOTAL_TEXTO_SEGUNDOS) -> None:
        """Espera —cancelable— al deadline compartido vigente, si hay alguno.

        Reevalúa el deadline en bucle tras cada suspensión para honrar extensiones
        concurrentes. Si la espera requerida excede ``presupuesto``, lanza
        :class:`TelegramRateLimitError` para fallar rápido determinísticamente sin
        bloquear indefinidamente la tarea ni saturar el event loop.
        """
        total_dormido = 0.0
        while True:
            ahora = time.monotonic()
            restante = self._deadline[0] - ahora
            if restante <= 0.0:
                break
            if presupuesto is not None and (total_dormido + restante) > presupuesto:
                raise TelegramRateLimitError(
                    f"El cooldown compartido ({restante:.1f}s) excede el presupuesto disponible ({presupuesto:.1f}s)",
                    retry_after=restante,
                    motivo="COOLDOWN_EXCEEDS_OPERATION_BUDGET",
                    presupuesto_agotado=True,
                )
            antes = time.monotonic()
            await asyncio.sleep(restante)
            total_dormido += max(0.0, time.monotonic() - antes)


class TelegramSendError(Exception):
    """Raised when a Telegram sendMessage call fails."""


class TelegramRateLimitError(TelegramSendError):
    """Flood-control de Telegram (HTTP 429) que no se pudo absorber con reintentos.

    Subclase de :class:`TelegramSendError`: los callers existentes siguen
    atrapándola. ``retry_after`` (segundos) permite a un consumidor best-effort,
    como el notificador de progreso, decidir si descarta la actualización en vez
    de insistir.

    **Contrato del consumidor (T3 — notificaciones concurrentes, #706).** Cuando el
    error llega, los reintentos acotados ya se agotaron: no quedan credenciales
    para insistir dentro de esta llamada, y un 429 **nunca** es un mensaje
    entregado. Entonces:

    - una actualización **no crítica** (progreso, ``/lod_status``, seguimiento) se
      **descarta explícitamente**: se registra el fallo y no se reprograma, porque su
      validez caduca antes de la ventana de flood-control;
    - un evento **terminal** (fallo o completitud de un ritual) no puede depender del
      canal limitado para llegar al operador: se reporta por una vía que no compita
      por el mismo chat (estado del pipeline en la GUI/CLI) además de intentar
      **una** reprogramación acotada —sólo si el caller tiene su propia cola—;
    - el cooldown compartido ya quedó publicado con el aviso acotado, así que el
      siguiente envío —de cualquier corrutina del mismo sender— no vuelve a golpear
      la API antes de que venza;
    - **prohibido** el bucle de reintento ilimitado y tratar este error como éxito.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float,
        mensajes_entregados: tuple[TelegramMessage, ...] = (),
        motivo: str | None = None,
        presupuesto_agotado: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.mensajes_entregados = mensajes_entregados
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
        #: Cooldown de flood-control COMPARTIDO por todas las corrutinas de este
        #: sender (#706): el 429 de una frena a las demás antes de cada intento.
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
        mensajes_entregados: list[TelegramMessage] = []
        tiempo_inicio = time.monotonic()
        presupuesto_total = _MAX_COOLDOWN_TOTAL_TEXTO_SEGUNDOS
        for i, chunk in enumerate(chunks):
            await self._wait_for_rate_limit(chat_id)
            markup = reply_markup if i == len(chunks) - 1 else None
            tiempo_transcurrido = time.monotonic() - tiempo_inicio
            presupuesto_remanente = max(0.0, presupuesto_total - tiempo_transcurrido)
            try:
                last_message = await self._send_chunk(
                    chat_id,
                    chunk,
                    markup,
                    parse_mode,
                    presupuesto_cooldown=presupuesto_remanente,
                )
                mensajes_entregados.append(last_message)
            except TelegramRateLimitError as exc:
                if mensajes_entregados:
                    exc.mensajes_entregados = tuple(mensajes_entregados)
                    logger.warning(
                        "Telegram send: fallo parcial tras entregar %d/%d fragmentos en chat %d: %s",
                        len(mensajes_entregados),
                        len(chunks),
                        chat_id,
                        exc,
                    )
                raise

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

        Política de duración: TODA la operación (espera del rate limit local, esperas
        compartidas del flood-control, reintentos por 429 y hops de redirección) está
        envuelta en un
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
                presupuesto_remanente = max(0.0, MAX_UPLOAD_TOTAL_SECONDS - tiempo_transcurrido)
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
            tiempo_transcurrido = time.monotonic() - tiempo_inicio
            presupuesto_remanente = max(0.0, MAX_UPLOAD_TOTAL_SECONDS - tiempo_transcurrido)
            if exc.presupuesto_agotado or exc.retry_after > presupuesto_remanente:
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
        (:class:`CooldownCompartido`): la espera que pidió un 429 no duerme sólo a la
        corrutina que lo recibió, frena a todas las que comparten el sender, así que
        una ráfaga no vuelve a golpear la API dentro de la ventana anunciada. Un 429 se reintenta hasta :data:`_MAX_REINTENTOS_429` veces, con la
        espera acotada por :data:`_RETRY_AFTER_MAX_SEGUNDOS`. Cualquier otro
        ``status != 200`` es fallo duro.
        """
        url = self._url + endpoint
        retry_after = _RETRY_AFTER_POR_DEFECTO
        cuerpo_429 = ""
        tiempo_inicio = time.monotonic()
        for intento in range(_MAX_REINTENTOS_429 + 1):
            # La consulta va DENTRO del bucle: después de un 429 el próximo intento
            # también espera, y si otra corrutina publicó un aviso mientras tanto,
            # este intento lo honra.
            tiempo_restante = (
                max(0.0, presupuesto_cooldown - (time.monotonic() - tiempo_inicio))
                if presupuesto_cooldown is not None
                else None
            )
            await self._cooldown.esperar(presupuesto=tiempo_restante)
            resp = await self._gateway.request("POST", url, self._session, **make_kwargs())
            async with resp:
                if resp.status != 429:
                    return await self._interpretar_respuesta(resp, endpoint, chat_id)
                cuerpo_429 = await resp.text()
                retry_after = _retry_after_de(resp.headers, cuerpo_429)

            # El aviso se publica SIEMPRE, también cuando este caller ya no va a
            # reintentar: una tarea que se rinde no debe dejar a sus hermanas
            # golpeando la misma ventana de flood-control.
            self._cooldown.publicar(retry_after)

            if intento == _MAX_REINTENTOS_429 or retry_after > _RETRY_AFTER_MAX_SEGUNDOS:
                break
            logger.warning(
                "Telegram %s: flood-control (429), reintento %d/%d (espera compartida %.1fs)",
                endpoint,
                intento + 1,
                _MAX_REINTENTOS_429,
                self._cooldown.restante(),
            )

        tiempo_transcurrido = time.monotonic() - tiempo_inicio
        tiempo_restante = (
            max(0.0, presupuesto_cooldown - tiempo_transcurrido) if presupuesto_cooldown is not None else None
        )
        presupuesto_agotado = tiempo_restante is not None and retry_after > tiempo_restante
        raise TelegramRateLimitError(
            f"Telegram API returned 429: {cuerpo_429} (retry_after={retry_after:g}s)",
            retry_after=retry_after,
            presupuesto_agotado=presupuesto_agotado,
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
        *,
        presupuesto: float | None = _PRESUPUESTO_CALLBACK_SEGUNDOS,
    ) -> None:
        """Responde a un ``callback_query`` (botón inline) vía el NetworkGateway.

        Cierra el estado de carga del botón en el cliente de Telegram. Como todo el
        egress, pasa por ``gateway.request`` (allow-list/SSRF/timeout/re-auth de
        redirects) y libera la respuesta con ``async with`` para no fugar la conexión.

        Es una vía de control: participa del cooldown compartido del bot (#706)
        —espera el aviso vigente antes de intentar y publica el suyo si recibe un
        429— pero no reintenta, porque eso excede el contrato de un cierre de botón.
        """
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True

        url = self._url + "answerCallbackQuery"
        await self._cooldown.esperar(presupuesto=presupuesto)
        resp = await self._gateway.request(
            "POST",
            url,
            self._session,
            json=payload,
        )
        async with resp:
            if resp.status == 429:
                # La ventana se publica aunque esta llamada no reintente: si no,
                # cualquier corrutina hermana seguiría golpeando la API dentro de la
                # ventana que Telegram acaba de anunciar.
                self._cooldown.publicar(_retry_after_de(resp.headers, await resp.text()))
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

        Como ``answerCallbackQuery``, es una vía de control: no reintenta, pero SÍ
        consulta el cooldown compartido del bot (#706) antes de intentar y publica el
        suyo ante un 429, para que una edición perdida no deje botones viejos en el
        prompt del operador ni deje a las demás corrutinas golpeando la ventana.
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
        await self._cooldown.esperar()
        resp = await self._gateway.request(
            "POST",
            url,
            self._session,
            json=payload,
        )
        async with resp:
            if resp.status == 429:
                self._cooldown.publicar(_retry_after_de(resp.headers, await resp.text()))
                logger.warning("Telegram editMessageText: flood-control (429); cooldown del bot publicado")
            elif resp.status != 200:
                body = await resp.text()
                logger.error("Failed to edit message: %s", body)
