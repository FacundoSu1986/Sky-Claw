"""Telegram Webhook — receives updates, dispatches to LLMRouter.

Handles incoming Telegram updates via an aiohttp webhook endpoint.
Processing is fire-and-forget: the handler responds 200 immediately
and processes the message in a background task.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import uuid
from typing import Any

import aiohttp
from aiohttp import web

from sky_claw.agent.router import LLMRouter
from sky_claw.comms.telegram_sender import TelegramSender
from sky_claw.security.hitl import HITLGuard
from sky_claw.logging_config import correlation_id_var
from sky_claw.orchestrator.sync_engine import UpdatePayload

logger = logging.getLogger(__name__)

_DEDUP_MAX_SIZE = 1000
_APPROVE_PREFIX = "/approve "
_DENY_PREFIX = "/deny "


def _parse_hitl_command(text: str) -> tuple[bool, str] | None:
    """Parse an operator HITL command from *text*.

    Returns ``(approved, request_id)`` when the text is a valid
    ``/approve <id>`` or ``/deny <id>`` command, otherwise ``None``.

    An empty or whitespace-only request_id is treated as invalid.
    """
    stripped = text.strip()
    if stripped.startswith(_APPROVE_PREFIX):
        req_id = stripped[len(_APPROVE_PREFIX):].strip()
        return (True, req_id) if req_id else None
    if stripped.startswith(_DENY_PREFIX):
        req_id = stripped[len(_DENY_PREFIX):].strip()
        return (False, req_id) if req_id else None
    return None


def escape_mdv2(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"([_*\[\]\(\)\~`\>\#\+\-\=\|\{\}\.\!])", r"\\\1", str(text))


def _format_update_payload(payload: UpdatePayload) -> str:
    lines = [
        "📊 *Reporte de Actualización de Mods*",
        f"🔍 *Total verificados:* {payload.total_checked}",
        ""
    ]

    if payload.updated_mods:
        lines.append(f"✅ *Actualizados exitosamente:* \\({len(payload.updated_mods)}\\)")
        for mod in payload.updated_mods:
            name = escape_mdv2(mod["name"])
            old_v = escape_mdv2(mod.get("old_version", "?"))
            new_v = escape_mdv2(mod.get("new_version", "?"))
            lines.append(f"\\- {name}: {old_v} ➡️ {new_v}")
        lines.append("")

    if payload.failed_mods:
        lines.append(f"❌ *Fallidos:* \\({len(payload.failed_mods)}\\)")
        for mod in payload.failed_mods:
            name = escape_mdv2(mod["name"])
            err = escape_mdv2(mod.get("error", "Error desconocido")[:60])
            lines.append(f"\\- {name}: {err}...")
        lines.append("")

    lines.append(f"✨ *Al día:* {len(payload.up_to_date_mods)}")
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
    ) -> None:
        self._router = router
        self._sender = sender
        self._session = session
        self._hitl = hitl
        self._secret_token = secret_token
        self._seen_updates: collections.OrderedDict[int, None] = collections.OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle_update(self, request: web.Request) -> web.Response:
        """Handle an incoming Telegram update.

        Parses the JSON body, deduplicates by ``update_id``, and
        dispatches processing to a background task. Always returns
        200 OK within the Telegram timeout window.
        """
        if self._secret_token:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if not token or token != self._secret_token:
                logger.warning("Unauthorized webhook access attempt")
                return web.Response(status=401, text="Unauthorized")

        try:
            data: dict[str, Any] = await request.json()
            if "callback_query" in data:
                await self._handle_callback_query(data["callback_query"])
            else:
                await self.process_update(data)
        except (ValueError, TypeError):
            logger.warning("Invalid JSON in Telegram update")
        except Exception as exc:
            logger.exception("Unexpected error in Telegram webhook handler: %s", exc)

        return web.Response(status=200)

    async def process_update(self, data: dict[str, Any]) -> None:
        """Process a single Telegram update dict.

        This can be called from the webhook handler or a polling loop.
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

        # Extract chat_id and text from message.
        message = data.get("message", {})
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = chat.get("id")

        if not text or chat_id is None:
            return

        # Intercept HITL operator commands before routing to the LLM.
        if self._hitl is not None:
            parsed = _parse_hitl_command(text)
            if parsed is not None:
                approved, request_id = parsed
                task = asyncio.create_task(
                    self._handle_hitl_command(chat_id, approved, request_id, update_id)
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return

        # Intercept update command
        if text.strip() == "/update_mods":
            task = asyncio.create_task(self._handle_update_mods_command(chat_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        # Fire-and-forget background processing via LLM.
        task = asyncio.create_task(
            self._process_bg(chat_id, text, update_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_bg(
        self, chat_id: int, text: str, update_id: int
    ) -> None:
        """Process a message in the background.

        Calls the LLMRouter and sends the response back via Telegram.
        """
        try:
            if self._router is None:
                logger.warning("Telegram update received but router is not initialized yet")
                await self._sender.send(chat_id, "Sky-Claw is still starting up. Please wait a moment.")
                return

            response = await self._router.chat(
                text, self._session, chat_id=str(chat_id)
            )
            await self._sender.send(chat_id, response)
        except Exception as exc:
            logger.exception(
                "Error processing update_id=%d, chat_id=%d: %s", update_id, chat_id, exc
            )
            try:
                await self._sender.send(
                    chat_id, "An internal error occurred. Please try again."
                )
            except Exception as exc:
                logger.exception("Failed to send error message to chat_id=%d: %s", chat_id, exc)

    async def _handle_update_mods_command(self, chat_id: int) -> None:
        await self._sender.send(chat_id, "⏳ Iniciando ciclo de actualización de mods. Esto puede demorar varios minutos...")
        try:
            sync_engine = self._router._tools._sync_engine
            payload = await sync_engine.check_for_updates(self._session)
            report = _format_update_payload(payload)
            await self._sender.send(chat_id, report, parse_mode="MarkdownV2")
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("Falla en /update_mods: %s", exc)
            await self._sender.send(chat_id, "❌ Ocurrió un error crítico durante la actualización.")

    async def _handle_callback_query(self, query: dict[str, Any]) -> None:
        """Handle an operator clicking an inline button (Approve/Deny)."""
        data = query.get("data", "")
        chat = query.get("message", {}).get("chat", {})
        chat_id = chat.get("id")
        message_id = query.get("message", {}).get("message_id")
        
        if not data or not chat_id or not message_id:
            return

        # Format: "hitl:approve:<request_id>" or "hitl:deny:<request_id>"
        if not data.startswith("hitl:"):
            return

        parts = data.split(":")
        if len(parts) != 3:
            return

        action, request_id = parts[1], parts[2]
        approved = (action == "approve")

        if self._hitl is not None:
            found = self._hitl.respond(request_id, approved)
            verb = "Approved" if approved else "Denied"
            
            # Answer callback to remove loading state in Telegram
            callback_id = query.get("id")
            if callback_id:
                url = self._sender._url + "answerCallbackQuery"
                await self._session.post(url, json={"callback_query_id": callback_id})

            if found:
                text = f"Request '{request_id}' {verb.lower()} by operator."
                await self._sender.edit_message(chat_id, message_id, text, reply_markup=None)
            else:
                await self._sender.send(chat_id, f"Error: Request '{request_id}' not found or already processed.")

    async def _handle_hitl_command(
        self,
        chat_id: int,
        approved: bool,
        request_id: str,
        update_id: int,
    ) -> None:
        """Deliver an operator HITL decision and confirm via Telegram.

        Calls :meth:`HITLGuard.respond` and replies to the operator with
        the outcome.  If no pending request exists for *request_id*, a
        "not found" message is sent instead.
        """
        assert self._hitl is not None
        found = self._hitl.respond(request_id, approved)
        verb = "approved" if approved else "denied"
        try:
            if found:
                await self._sender.send(
                    chat_id, f"Request '{request_id}' {verb}."
                )
            else:
                await self._sender.send(
                    chat_id,
                    f"No pending HITL request found for ID '{request_id}'.",
                )
        except Exception as exc:
            logger.exception(
                "Failed to send HITL confirmation for update_id=%d: %s", update_id, exc
            )
