"""Telegram Webhook — receives updates, dispatches to LLMRouter.

Handles incoming Telegram updates via an aiohttp webhook endpoint.
Processing is fire-and-forget: the handler responds 200 immediately
and processes the message in a background task.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from sky_claw.app.security.network_gateway import EgressViolationError, NetworkGatewayTimeoutError
from sky_claw.logging_config import correlation_id_var

logger = logging.getLogger(__name__)

_DEDUP_MAX_SIZE = 1000
_TELEGRAM_HITL_COMPENSATION_TIMEOUT_SECONDS = 2.0
_APPROVE_PREFIX = "/approve "
_DENY_PREFIX = "/deny "
_HITL_CALLBACK_NAMESPACE = "hitl"
_HITL_CALLBACK_ACTIONS = frozenset({"approve", "deny"})
_HITL_TOKEN_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_DEFAULT_HITL_TOKEN_BYTES = 16
_DEFAULT_HITL_TOKEN_ATTEMPTS = 8


class TelegramHITLRegistryError(RuntimeError):
    """Error base para fallos de inscripción en el registry HITL."""


class TelegramHITLRegistryFullError(TelegramHITLRegistryError):
    """El registry alcanzó su capacidad sin expulsar mappings activos."""


class TelegramHITLRegistryDuplicateError(TelegramHITLRegistryError):
    """El request_id ya tiene un mensaje pendiente registrado."""


class TelegramHITLTokenCollisionError(TelegramHITLRegistryError):
    """El generador agotó sus intentos sin producir un token libre."""


@dataclass(frozen=True, slots=True)
class TelegramHITLMessage:
    """Referencia mínima al mensaje Telegram y su transporte propietario."""

    chat_id: int
    message_id: int
    sender: TelegramSender
    token: str = ""


class TelegramHITLMessageRegistry:
    """Registry efímero del lifecycle Telegram y de sus tokens opacos.

    Las reservas cuentan contra ``max_entries`` desde antes de ``sendMessage``.
    El lock protege tanto la reserva como el mapping request/token y nunca se
    expulsan entradas activas. ``_token_to_request`` sólo contiene tokens que
    aún no fueron registrados; ``_registered_tokens`` conserva los tokens de
    mensajes accionables hasta su cleanup terminal. Los tokens retirados se
    recuerdan en una ventana bounded para impedir su reutilización inmediata.
    """

    def __init__(
        self,
        max_entries: int = 1024,
        *,
        token_factory: Callable[[], str] | None = None,
        max_token_attempts: int = _DEFAULT_HITL_TOKEN_ATTEMPTS,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_token_attempts <= 0:
            raise ValueError("max_token_attempts must be positive")
        self._entries: collections.OrderedDict[str, TelegramHITLMessage] = collections.OrderedDict()
        self._token_to_request: dict[str, str] = {}
        self._registered_tokens: dict[str, str] = {}
        self._request_to_token: dict[str, str] = {}
        self._retired_tokens: collections.deque[str] = collections.deque(maxlen=max_entries)
        self._retired_token_set: set[str] = set()
        self._lock = asyncio.Lock()
        self._max_entries = max_entries
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(_DEFAULT_HITL_TOKEN_BYTES))
        self._max_token_attempts = max_token_attempts

    async def reserve_token(self, request_id: str) -> str:
        """Reserva un token nuevo antes de construir botones o enviar el prompt."""
        if not request_id:
            raise ValueError("request_id must not be empty")

        async with self._lock:
            return self._reserve_token_locked(request_id)

    def _reserve_token_locked(self, request_id: str) -> str:
        if request_id in self._request_to_token:
            raise TelegramHITLRegistryDuplicateError(f"HITL request_id already registered: {request_id}")
        if len(self._request_to_token) >= self._max_entries:
            raise TelegramHITLRegistryFullError(f"HITL registry capacity exhausted ({self._max_entries})")

        for _attempt in range(self._max_token_attempts):
            try:
                token = self._token_factory()
            except Exception as exc:
                raise TelegramHITLTokenCollisionError("HITL token generator failed") from exc
            if not _is_valid_hitl_token(token):
                continue
            if token in self._token_to_request or token in self._registered_tokens or token in self._retired_token_set:
                continue
            self._request_to_token[request_id] = token
            self._token_to_request[token] = request_id
            return token
        raise TelegramHITLTokenCollisionError("Unable to reserve a unique HITL Telegram token")

    async def release_token(self, token: str) -> bool:
        """Libera sólo una reserva pre-envío, nunca un mensaje ya accionable."""
        async with self._lock:
            request_id = self._token_to_request.pop(token, None)
            if request_id is None:
                return False
            if self._request_to_token.get(request_id) == token:
                self._request_to_token.pop(request_id, None)
            self._retire_token_locked(token)
            return True

    def _retire_token_locked(self, token: str) -> None:
        if token in self._retired_token_set:
            return
        if len(self._retired_tokens) == self._retired_tokens.maxlen:
            oldest = self._retired_tokens.popleft()
            self._retired_token_set.discard(oldest)
        self._retired_tokens.append(token)
        self._retired_token_set.add(token)

    async def resolve_token(self, token: str) -> str | None:
        """Resuelve sólo tokens existentes, sin tratar nunca el token como request_id."""
        if not isinstance(token, str):
            return None
        async with self._lock:
            return self._token_to_request.get(token) or self._registered_tokens.get(token)

    async def token_for_request(self, request_id: str) -> str | None:
        """Obtiene el token de un intento activo para sincronización de lifecycle."""
        async with self._lock:
            return self._request_to_token.get(request_id)

    async def register(
        self,
        request_id: str,
        chat_id: int,
        message_id: int,
        sender: TelegramSender,
        *,
        token: str | None = None,
    ) -> None:
        """Registra el mensaje para un token reservado, o crea una reserva legacy de test."""
        if not request_id:
            raise ValueError("request_id must not be empty")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            raise TypeError("chat_id must be an integer")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise TypeError("message_id must be an integer")

        async with self._lock:
            if request_id in self._entries:
                raise TelegramHITLRegistryDuplicateError(f"HITL request_id already registered: {request_id}")
            reserved = self._request_to_token.get(request_id)
            if token is None:
                if reserved is not None:
                    raise TelegramHITLRegistryDuplicateError(f"HITL request_id already reserved: {request_id}")
                token = self._reserve_token_locked(request_id)
            elif not _is_valid_hitl_token(token):
                raise TelegramHITLRegistryError("HITL token is not valid")
            elif reserved != token or self._token_to_request.get(token) != request_id:
                raise TelegramHITLRegistryError("HITL token is not reserved for this request_id")

            self._token_to_request.pop(token, None)
            self._registered_tokens[token] = request_id
            self._entries[request_id] = TelegramHITLMessage(
                chat_id=chat_id,
                message_id=message_id,
                sender=sender,
                token=token,
            )

    async def get(self, request_id: str) -> TelegramHITLMessage | None:
        """Obtiene una referencia sin modificar el registry."""
        async with self._lock:
            return self._entries.get(request_id)

    async def pop(self, request_id: str) -> TelegramHITLMessage | None:
        """Elimina mensaje y token asociado, incluyendo una reserva pre-envío."""
        async with self._lock:
            reference = self._entries.pop(request_id, None)
            token = self._request_to_token.pop(request_id, None)
            if token is not None:
                self._token_to_request.pop(token, None)
                self._registered_tokens.pop(token, None)
                self._retire_token_locked(token)
            return reference

    async def clear(self) -> None:
        """Limpia explícitamente referencias y reservas durante shutdown."""
        async with self._lock:
            self._entries.clear()
            self._token_to_request.clear()
            self._registered_tokens.clear()
            self._request_to_token.clear()
            self._retired_tokens.clear()
            self._retired_token_set.clear()

    def __len__(self) -> int:
        """Devuelve reservas más mensajes activos para el límite bounded."""
        return len(self._request_to_token)


def _is_valid_hitl_token(token: object) -> bool:
    return isinstance(token, str) and bool(token) and all(character in _HITL_TOKEN_ALPHABET for character in token)


def build_hitl_callback_data(action: str, token: str) -> str:
    """Construye y valida el único formato de callback HITL de producción."""
    if not isinstance(action, str) or action not in _HITL_CALLBACK_ACTIONS:
        raise ValueError(f"Unknown HITL callback action: {action!r}")
    if not _is_valid_hitl_token(token):
        raise ValueError("HITL callback token must be non-empty ASCII URL-safe text")
    callback_data = f"{_HITL_CALLBACK_NAMESPACE}:{action}:{token}"
    size = len(callback_data.encode("utf-8"))
    if not 1 <= size <= 64:
        raise ValueError(f"HITL callback_data must be between 1 and 64 UTF-8 bytes, got {size}")
    return callback_data


def parse_hitl_callback_data(data: object) -> tuple[str, str] | None:
    """Parsea namespace, action y token sin interpretar ningún request_id."""
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if (
        len(parts) != 3
        or parts[0] != _HITL_CALLBACK_NAMESPACE
        or parts[1] not in _HITL_CALLBACK_ACTIONS
        or not _is_valid_hitl_token(parts[2])
    ):
        return None
    try:
        size = len(data.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if not 1 <= size <= 64:
        return None
    return parts[1], parts[2]


def terminal_hitl_text(decision: Any) -> str:
    """Renderiza un estado HITL terminal sin dejar una acción disponible."""
    return {
        "approved": "✅ Solicitud aprobada",
        "denied": "❌ Solicitud denegada",
        "timeout": "⌛ Solicitud expirada",
    }.get(getattr(decision, "value", ""), "⚠️ Solicitud no accionable")


async def register_hitl_message_cancellation_safe(
    registry: TelegramHITLMessageRegistry,
    request_id: str,
    message: TelegramMessage,
    sender: TelegramSender,
    *,
    token: str | None = None,
) -> None:
    """Registra un mensaje entregado antes de propagar cancelación al caller.

    La tarea tiene ownership local y se drena si hay cancelación. El shield sólo
    protege el registro en memoria posterior a un ``sendMessage`` exitoso; la
    entrega de red nunca queda shielded. Los fallos normales de registro se
    propagan al caller, que compensa el mensaje recién creado.
    """
    if token is None:
        registration_coro = registry.register(request_id, message.chat_id, message.message_id, sender)
    else:
        registration_coro = registry.register(
            request_id,
            message.chat_id,
            message.message_id,
            sender,
            token=token,
        )
    registration = asyncio.create_task(registration_coro, name=f"hitl-register:{request_id}")
    try:
        await asyncio.shield(registration)
    except asyncio.CancelledError:
        # Drenar la tarea owned antes de repropagar la cancelación evita tareas
        # huérfanas. Un error de registro también se consume aquí para no dejar
        # una excepción no observada; la cancelación del caller sigue ganando.
        try:
            await registration
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("HITL registration failed during cancellation: %s", exc)
            if token is not None:
                await registry.release_token(token)
            await invalidate_unregistered_hitl_message(sender, message)
        raise
    except Exception:
        if token is not None:
            await registry.release_token(token)
        raise


async def invalidate_unregistered_hitl_message(
    sender: TelegramSender,
    message: TelegramMessage,
) -> None:
    """Retira botones de un mensaje enviado cuyo registro no pudo completarse."""
    try:
        await asyncio.wait_for(
            sender.edit_message(
                message.chat_id,
                message.message_id,
                "⚠️ Solicitud no accionable",
                reply_markup=None,
            ),
            timeout=_TELEGRAM_HITL_COMPENSATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Timeout invalidando el mensaje HITL sin mapping (%s, %s)",
            message.chat_id,
            message.message_id,
        )
    except Exception:
        logger.exception(
            "No se pudo invalidar el mensaje HITL sin mapping (%s, %s)",
            message.chat_id,
            message.message_id,
        )


async def terminalize_hitl_message(
    registry: TelegramHITLMessageRegistry,
    request_id: str,
    decision: Any,
) -> bool:
    """Consume el mapping y refleja un estado terminal en Telegram.

    El mapping se elimina antes del I/O de presentación: una edición fallida no
    altera la decisión HITL ya comprometida ni retiene memoria indefinidamente.
    """
    reference = await registry.pop(request_id)
    if reference is None:
        return False

    try:
        await reference.sender.edit_message(
            reference.chat_id,
            reference.message_id,
            terminal_hitl_text(decision),
            reply_markup=None,
        )
    except Exception:
        logger.exception("No se pudo editar el mensaje HITL terminal para %s", request_id)
    return True


def _parse_hitl_command(text: str) -> tuple[bool, str] | None:
    """Parsea un comando HITL sin modificar el request_id restante.

    El parser consume exactamente ``/approve `` o ``/deny ``. El resto es una
    identidad opaca literal; ``strip`` sólo se usa para decidir si está vacío,
    nunca para reemplazar el valor retornado.
    """
    if not isinstance(text, str):
        return None
    command = text.lstrip()
    if command.startswith(_APPROVE_PREFIX):
        req_id = command[len(_APPROVE_PREFIX) :]
        return (True, req_id) if req_id.strip() else None
    if command.startswith(_DENY_PREFIX):
        req_id = command[len(_DENY_PREFIX) :]
        return (False, req_id) if req_id.strip() else None
    return None


import html  # noqa: E402

if TYPE_CHECKING:
    from sky_claw.app.agent.router import LLMRouter
    from sky_claw.app.comms.telegram_sender import TelegramMessage, TelegramSender
    from sky_claw.app.orchestrator.sync_engine import UpdatePayload
    from sky_claw.app.security.hitl import HITLGuard


_HITL_REQUEST_ID_VISIBLE_PREFIX_CHARS = 48
_HITL_REQUEST_ID_SHORT_MAX_CHARS = 128


def escape_html(text: str) -> str:
    if not text:
        return ""
    return html.escape(str(text))


def format_hitl_request_id_for_telegram(request_id: str) -> str:
    """Renderiza una vista acotada del ID sin cambiar su identidad interna.

    IDs cortos se muestran completos para conservar la ergonomía existente. Para
    IDs largos, Telegram recibe sólo un prefijo escapado y una huella SHA-256
    derivada del valor exacto; el ``request_id`` original nunca se trunca en el
    guard ni en el registry.
    """
    if not isinstance(request_id, str):
        raise TypeError("request_id must be a string")
    if len(request_id) <= _HITL_REQUEST_ID_SHORT_MAX_CHARS:
        return escape_html(request_id)
    prefix = escape_html(request_id[:_HITL_REQUEST_ID_VISIBLE_PREFIX_CHARS])
    fingerprint = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}… [sha256:{fingerprint}]"


def _format_update_payload(payload: UpdatePayload) -> str:
    lines = [
        "📊 <b>Reporte de Actualización de Mods</b>",
        f"🔍 <b>Total verificados:</b> {payload.total_checked}",
        "",
    ]

    if payload.updated_mods:
        lines.append(f"✅ <b>Actualizados exitosamente:</b> ({len(payload.updated_mods)})")
        for mod in payload.updated_mods:
            name = escape_html(mod["name"])
            old_v = escape_html(mod.get("old_version", "?"))
            new_v = escape_html(mod.get("new_version", "?"))
            lines.append(f"- <i>{name}</i>: <pre>{old_v} ➡️ {new_v}</pre>")
        lines.append("")

    if payload.failed_mods:
        lines.append(f"❌ <b>Fallidos:</b> ({len(payload.failed_mods)})")
        for mod in payload.failed_mods:
            name = escape_html(mod["name"])
            err = escape_html(mod.get("error", "Error desconocido")[:60])
            lines.append(f"- <i>{name}</i>: <pre>{err}...</pre>")
        lines.append("")

    lines.append(f"✨ <b>Al día:</b> {len(payload.up_to_date_mods)}")
    return "\n".join(lines)


class TelegramWebhook:
    """Aiohttp webhook handler for Telegram Bot API updates.

    Args:
        router: LLM conversation router.
        sender: Telegram message sender.
        session: Shared aiohttp session for outbound requests.
        hitl: Optional :class:`HITLGuard` instance.  When provided,
            ``/approve <id>`` and ``/deny <id>`` messages are intercepted
            and routed to :meth:`HITLGuard.respond` instead of the LLM.
    """

    def __init__(
        self,
        router: LLMRouter,
        sender: TelegramSender,
        session: aiohttp.ClientSession,
        hitl: HITLGuard | None = None,
        secret_token: str | None = None,
        authorized_user_id: int | None = None,
        hitl_registry: TelegramHITLMessageRegistry | None = None,
    ) -> None:
        self._router = router
        self._sender = sender
        self._session = session
        self._hitl = hitl
        self._hitl_registry = hitl_registry
        self._secret_token = secret_token
        self._seen_updates: collections.OrderedDict[int, None] = collections.OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        # Asignamos directamente desde la inyección de dependencias
        self._allowed_user_id = authorized_user_id

    def _validate_sender(self, message_or_callback: dict[str, Any]) -> bool:
        """Valida que el mensaje no sea reenviado y el usuario esté autorizado.

        Args:
            message_or_callback: Diccionario con datos del mensaje o callback_query.
                Para mensajes: debe contener 'from' y opcionalmente 'forward_date'/'forward_from'.
                Para callbacks: debe contener 'from' y 'message'.

        Returns:
            True si el remitente está autorizado y el mensaje no es reenviado.
        """
        if self._allowed_user_id is None:
            logger.warning("TELEGRAM_ALLOWED_USER_ID no configurado - rechazando comando HITL")
            return False

        # Obtener usuario del mensaje/callback
        from_user = message_or_callback.get("from", {})
        user_id = from_user.get("id")

        # Verificar que el usuario esté autorizado
        if user_id != self._allowed_user_id:
            return False

        # Verificar que no sea un mensaje reenviado (para callbacks, verificar el mensaje original)
        if message_or_callback.get("forward_date") is not None:
            return False
        if message_or_callback.get("forward_from") is not None:
            return False

        # Para callbacks, verificar también el mensaje asociado
        message = message_or_callback.get("message", {})
        if message:
            if message.get("forward_date") is not None:
                return False
            if message.get("forward_from") is not None:
                return False

        # Verificar reply_to_message — un reply a un mensaje reenviado también es sospechoso
        reply_msg = message_or_callback.get("reply_to_message") or message.get("reply_to_message")
        if reply_msg and (reply_msg.get("forward_date") is not None or reply_msg.get("forward_from") is not None):
            logger.warning("Blocked: reply to forwarded message from potentially unauthorized source")
            return False

        return True

    def _validate_private_operator(self, message_or_callback: dict[str, Any]) -> bool:
        """Acepta sólo al operador configurado en su chat privado.

        ``telegram_chat_id`` se reutiliza como identidad del operador para el
        modo privado de Telegram, donde ``chat.id == from.id``. Un ID negativo
        de grupo no representa a un usuario y no puede abrir una frontera de
        confianza para tools mutantes.
        """
        if not self._validate_sender(message_or_callback):
            return False

        message = message_or_callback.get("message") or message_or_callback
        chat_id = message.get("chat", {}).get("id")
        return chat_id is not None and str(chat_id) == str(self._allowed_user_id)

    async def handle_update(self, request: web.Request) -> web.Response:
        """Handle an incoming Telegram update.

        Parses the JSON body, deduplicates by ``update_id``, and
        dispatches processing to a background task. Always returns
        200 OK within the Telegram timeout window.
        """
        if self._secret_token:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            # H-5: comparación en tiempo constante. token != self._secret_token
            # cortocircuitaba en el primer byte distinto, filtrando el secreto por
            # timing. secrets.compare_digest es resistente a ese ataque.
            if not token or not secrets.compare_digest(token, self._secret_token):
                logger.warning("Unauthorized webhook access attempt")
                return web.Response(status=401, text="Unauthorized")

        try:
            data: dict[str, Any] = await request.json()
            await self.process_update(data)
        except (ValueError, TypeError):
            logger.warning("Invalid JSON in Telegram update")
        except Exception as exc:
            logger.exception("Unexpected error in Telegram webhook handler: %s", exc)

        return web.Response(status=200)

    async def process_update(self, data: dict[str, Any]) -> None:
        """Procesa un update de Telegram mediante el dispatcher autoritativo.

        Webhook y long polling usan esta misma ruta. La deduplicación y la
        autorización ocurren antes de enrutar mensajes o callbacks, de modo
        que un callback no pueda eludir el límite HITL.
        """
        correlation_id_var.set(str(uuid.uuid4()))
        update_id = data.get("update_id")
        if update_id is None:
            logger.warning("Telegram update missing update_id")
            return

        # Deduplication — Telegram re-sends if it doesn't get 200 fast enough.
        if update_id in self._seen_updates:
            logger.debug("Duplicate update_id=%d, skipping", update_id)
            return

        self._seen_updates[update_id] = None
        while len(self._seen_updates) > _DEDUP_MAX_SIZE:
            self._seen_updates.popitem(last=False)

        # Enruta callbacks por el mismo boundary que los mensajes.
        callback_query = data.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)
            return

        # Extrae chat_id y texto del mensaje.
        message = data.get("message", {})
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = chat.get("id")

        if not text or chat_id is None:
            return

        if not self._validate_private_operator(message):
            logger.warning("Telegram update rechazado: el operador debe usar su chat privado autorizado.")
            return

        # Intercepta comandos HITL antes de enrutar al LLM.
        if self._hitl is not None:
            parsed = _parse_hitl_command(text)
            if parsed is not None:
                approved, request_id = parsed
                task = asyncio.create_task(self._handle_hitl_command(chat_id, approved, request_id, update_id, message))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return

        # Intercepta el comando de actualización.
        if text.strip() == "/update_mods":
            task = asyncio.create_task(self._handle_update_mods_command(chat_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        # Procesamiento en segundo plano mediante el LLM.
        task = asyncio.create_task(self._process_bg(chat_id, text, update_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_bg(self, chat_id: int, text: str, update_id: int) -> None:
        """Process a message in the background.

        Calls the LLMRouter and sends the response back via Telegram.
        """
        try:
            if self._router is None:
                logger.warning("Telegram update received but router is not initialized yet")
                await self._sender.send(chat_id, "Sky-Claw is still starting up. Please wait a moment.")
                return

            try:
                response = await self._router.chat(text, self._session, chat_id=str(chat_id))
                await self._sender.send(chat_id, response)
            except Exception as e:
                logger.exception("Critical Router Failure: %s", e)
                await self._sender.send(
                    chat_id,
                    "⚠️ El agente ha sufrido un error interno en la orquestación. Reiniciando subsistema...",
                )
        except Exception as exc:
            logger.exception("Error processing update_id=%d, chat_id=%d: %s", update_id, chat_id, exc)
            try:
                await self._sender.send(chat_id, "An internal error occurred. Please try again.")
            except Exception as exc:
                logger.exception("Failed to send error message to chat_id=%d: %s", chat_id, exc)

    async def _handle_update_mods_command(self, chat_id: int) -> None:
        await self._sender.send(
            chat_id,
            "⏳ Iniciando ciclo de actualización de mods. Esto puede demorar varios minutos...",
        )
        try:
            sync_engine = self._router._tools._sync_engine
            payload = await sync_engine.check_for_updates(self._session)
            report = _format_update_payload(payload)
            await self._sender.send(chat_id, report, parse_mode="HTML")
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Falla en /update_mods: %s", exc)
            await self._sender.send(chat_id, "❌ Ocurrió un error crítico durante la actualización.")

    async def terminalize_hitl(self, request_id: str, decision: Any) -> bool:
        """Terminaliza el prompt usando el sender owner registrado."""
        if self._hitl_registry is None:
            return False
        return await terminalize_hitl_message(self._hitl_registry, request_id, decision)

    async def _answer_callback_query_safely(self, query: dict[str, Any], text: str | None = None) -> None:
        """Confirma un callback sin ocultar la decisión autoritativa de HITL."""
        callback_id = query.get("id")
        if not callback_id:
            return
        try:
            await self._sender.answer_callback_query(callback_id, text=text)
        except (aiohttp.ClientError, OSError, TimeoutError, EgressViolationError, NetworkGatewayTimeoutError):
            # Un fallo de transporte no revierte una decisión ya comprometida.
            logger.warning("No se pudo confirmar el callback de Telegram %s", callback_id, exc_info=True)

    async def _handle_callback_query(self, query: dict[str, Any]) -> None:
        """Procesa el clic de un operador en un botón inline (aprobar/denegar)."""
        # H-03: Validación anti-spoofing
        if not self._validate_private_operator(query):
            logger.warning("Intento de spoofing HITL detectado y bloqueado en callback_query.")
            await self._answer_callback_query_safely(query, text="Unauthorized")
            return

        data = query.get("data", "")
        chat = query.get("message", {}).get("chat", {})
        chat_id = chat.get("id")
        message_id = query.get("message", {}).get("message_id")

        if not data or chat_id is None or message_id is None:
            await self._answer_callback_query_safely(query, text="Invalid callback")
            return

        parsed = parse_hitl_callback_data(data)
        if parsed is None:
            logger.warning("Invalid HITL callback format received")
            await self._answer_callback_query_safely(query, text="Invalid callback")
            return

        action, token = parsed
        if self._hitl is None or self._hitl_registry is None:
            logger.error("Se recibió un callback HITL válido sin guard o registry inicializado")
            await self._answer_callback_query_safely(query, text="HITL unavailable")
            return

        # El token es sólo una identidad de transporte. Nunca cae como
        # request_id al guard si falta o quedó stale.
        request_id = await self._hitl_registry.resolve_token(token)
        if request_id is None:
            await self._answer_callback_query_safely(query, text="Unknown or expired HITL request")
            return

        approved = action == "approve"
        found = await self._hitl.respond(request_id, approved)

        # Confirma el callback para quitar el estado de carga. Un fallo de
        # entrega se observa, pero no cambia el resultado HITL comprometido.
        await self._answer_callback_query_safely(query)

        if found:
            # El hook del guard suele consumir el mapping. Este fallback cubre
            # guards compatibles sin hook y conserva el sender owner de C1.
            await self.terminalize_hitl(request_id, self._hitl_decision(approved))
        else:
            await self._sender.send(
                chat_id,
                "Error: HITL request already processed or unavailable.",
            )

    @staticmethod
    def _hitl_decision(approved: bool) -> Any:
        """Devuelve el enum sin importar el módulo en tiempo de importación."""
        from sky_claw.app.security.hitl import Decision

        return Decision.APPROVED if approved else Decision.DENIED

    async def _handle_hitl_command(
        self,
        chat_id: int,
        approved: bool,
        request_id: str,
        update_id: int,
        message: dict[str, Any] | None = None,
    ) -> None:
        """Deliver an operator HITL decision and confirm via Telegram.

        Calls :meth:`HITLGuard.respond` and replies to the operator with
        the outcome.  If no pending request exists for *request_id*, a
        "not found" message is sent instead.
        """
        # H-03: Validación anti-spoofing
        if message is not None and not self._validate_private_operator(message):
            logger.warning("Intento de spoofing HITL detectado y bloqueado.")
            return

        if self._hitl is None:
            logger.error("HITL command received without an initialized HITL guard")
            return

        found = await self._hitl.respond(request_id, approved)
        verb = "approved" if approved else "denied"
        try:
            if found:
                if self._hitl_registry is not None:
                    # Compatibilidad para guards sin hook terminal; con el
                    # wiring productivo el hook ya habrá consumido el mapping.
                    await self.terminalize_hitl(request_id, self._hitl_decision(approved))
                await self._sender.send(chat_id, f"Request '{request_id}' {verb}.")
            else:
                await self._sender.send(
                    chat_id,
                    f"No pending HITL request found for ID '{request_id}'.",
                )
        except Exception as exc:
            logger.exception("Failed to send HITL confirmation for update_id=%d: %s", update_id, exc)
