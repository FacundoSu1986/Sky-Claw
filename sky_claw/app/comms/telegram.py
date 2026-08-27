"""Telegram Webhook — receives updates, dispatches to LLMRouter.

Handles incoming Telegram updates via an aiohttp webhook endpoint.
Processing is fire-and-forget: the handler responds 200 immediately
and processes the message in a background task.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from sky_claw.app.security.network_gateway import EgressViolationError, NetworkGatewayTimeoutError
from sky_claw.logging_config import correlation_id_var

logger = logging.getLogger(__name__)

_DEDUP_MAX_SIZE = 1000
_APPROVE_PREFIX = "/approve "
_DENY_PREFIX = "/deny "


@dataclass(frozen=True, slots=True)
class TelegramHITLMessage:
    """Referencia mínima al mensaje Telegram de un prompt HITL."""

    chat_id: int
    message_id: int


class TelegramHITLMessageRegistry:
    """Asocia requests HITL pendientes con mensajes Telegram, sólo en memoria.

    El lock protege cada operación del mapping y ``max_entries`` evita que un
    fallo de cleanup convierta el registry en almacenamiento ilimitado. La
    identidad de request por intento sigue siendo responsabilidad del productor.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._entries: collections.OrderedDict[str, TelegramHITLMessage] = collections.OrderedDict()
        self._lock = asyncio.Lock()
        self._max_entries = max_entries

    async def register(self, request_id: str, chat_id: int, message_id: int) -> None:
        """Registra la identidad exacta del prompt ya entregado."""
        if not request_id:
            raise ValueError("request_id must not be empty")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            raise TypeError("chat_id must be an integer")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise TypeError("message_id must be an integer")

        async with self._lock:
            self._entries[request_id] = TelegramHITLMessage(chat_id=chat_id, message_id=message_id)
            self._entries.move_to_end(request_id)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    async def get(self, request_id: str) -> TelegramHITLMessage | None:
        """Obtiene una referencia sin modificar el registry."""
        async with self._lock:
            return self._entries.get(request_id)

    async def pop(self, request_id: str) -> TelegramHITLMessage | None:
        """Elimina y devuelve una referencia terminalizada."""
        async with self._lock:
            return self._entries.pop(request_id, None)

    async def clear(self) -> None:
        """Limpia explícitamente referencias retenidas durante shutdown."""
        async with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        """Devuelve el tamaño observado del registry para diagnóstico/tests."""
        return len(self._entries)


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
) -> None:
    """Register a delivered message before allowing caller cancellation through.

    The task is owned locally and drained on cancellation. This shields only the
    in-memory registration after ``sendMessage`` succeeded; network delivery is
    never shielded.
    """
    registration = asyncio.create_task(
        registry.register(request_id, message.chat_id, message.message_id),
        name=f"hitl-register:{request_id}",
    )
    try:
        await asyncio.shield(registration)
    except asyncio.CancelledError:
        # Drenar la tarea owned antes de repropagar la cancelación evita tasks
        # huérfanas y garantiza que el mapping exista para on_cancel.
        with contextlib.suppress(asyncio.CancelledError):
            await registration
        raise


async def terminalize_hitl_message(
    registry: TelegramHITLMessageRegistry,
    sender: TelegramSender | None,
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
    if sender is None:
        return True

    try:
        await sender.edit_message(
            reference.chat_id,
            reference.message_id,
            terminal_hitl_text(decision),
            reply_markup=None,
        )
    except Exception:
        logger.exception("No se pudo editar el mensaje HITL terminal para %s", request_id)
    return True


def _parse_hitl_command(text: str) -> tuple[bool, str] | None:
    """Parse an operator HITL command from *text*.

    Returns ``(approved, request_id)`` when the text is a valid
    ``/approve <id>`` or ``/deny <id>`` command, otherwise ``None``.

    An empty or whitespace-only request_id is treated as invalid.
    """
    stripped = text.strip()
    if stripped.startswith(_APPROVE_PREFIX):
        req_id = stripped[len(_APPROVE_PREFIX) :].strip()
        return (True, req_id) if req_id else None
    if stripped.startswith(_DENY_PREFIX):
        req_id = stripped[len(_DENY_PREFIX) :].strip()
        return (False, req_id) if req_id else None
    return None


import html  # noqa: E402

if TYPE_CHECKING:
    from sky_claw.app.agent.router import LLMRouter
    from sky_claw.app.comms.telegram_sender import TelegramMessage, TelegramSender
    from sky_claw.app.orchestrator.sync_engine import UpdatePayload
    from sky_claw.app.security.hitl import HITLGuard


def escape_html(text: str) -> str:
    if not text:
        return ""
    return html.escape(str(text))


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
        """Terminaliza el prompt asociado sin alterar la decisión autoritativa.

        El mapping se consume antes del I/O de edición. Por eso un fallo de
        Telegram no deja una referencia retenida ni puede cambiar el resultado
        que el ``HITLGuard`` ya comprometió.
        """
        if self._hitl_registry is None:
            return False
        return await terminalize_hitl_message(self._hitl_registry, self._sender, request_id, decision)

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

        # Formato: "hitl:approve:<request_id>" o "hitl:deny:<request_id>".
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "hitl":
            logger.warning("Invalid HITL callback format received")
            await self._answer_callback_query_safely(query, text="Invalid callback")
            return

        action, request_id = parts[1], parts[2]
        if action not in {"approve", "deny"} or not request_id.strip():
            # Una acción desconocida o un ID vacío nunca deben convertirse
            # implícitamente en denegación ni en otro identificador.
            logger.warning("Unknown or empty HITL callback action received: %r", data)
            await self._answer_callback_query_safely(query, text="Invalid HITL action")
            return

        approved = action == "approve"

        if self._hitl is None:
            logger.error("Se recibió un callback HITL válido sin HITLGuard inicializado")
            await self._answer_callback_query_safely(query, text="HITL unavailable")
            return

        found = await self._hitl.respond(request_id, approved)
        verb = "Approved" if approved else "Denied"

        # Confirma el callback para quitar el estado de carga. Un fallo de
        # entrega se observa, pero no cambia el resultado HITL comprometido.
        await self._answer_callback_query_safely(query)

        if found:
            if self._hitl_registry is not None:
                # En producción, el hook del guard normalmente ya consumió el
                # mapping. Este fallback cubre guards compatibles sin hook.
                await self.terminalize_hitl(request_id, self._hitl_decision(approved))
            else:
                # Compatibilidad para callers antiguos que sólo disponen de la
                # identidad embebida en el callback inline.
                text = f"Request '{request_id}' {verb.lower()} by operator."
                try:
                    await self._sender.edit_message(chat_id, message_id, text, reply_markup=None)
                except Exception:
                    logger.exception("No se pudo editar el mensaje HITL terminal para %s", request_id)
        else:
            await self._sender.send(
                chat_id,
                f"Error: Request '{request_id}' not found or already processed.",
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
