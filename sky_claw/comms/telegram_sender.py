"""Telegram Sender — rate-limited message delivery.

Sends responses back to Telegram chats with per-chat rate limiting
(max 20 messages/minute) and automatic splitting of long messages.
All outbound traffic goes through :class:`NetworkGateway`.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any

import aiohttp

from sky_claw.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/"
MAX_MESSAGE_LENGTH = 4096
MAX_MESSAGES_PER_MINUTE = 20


class TelegramSendError(Exception):
    """Raised when a Telegram sendMessage call fails."""


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
        self._url = TELEGRAM_API_BASE.format(token=bot_token)

    async def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        """Send a message to a Telegram chat.

        Automatically splits messages exceeding 4096 characters
        and respects per-chat rate limits.

        Args:
            chat_id: Telegram chat identifier.
            text: Message text to send.
            reply_markup: Optional inline keyboard or other markup.
            parse_mode: Optional Telegram parse mode (e.g. "MarkdownV2").

        Raises:
            TelegramSendError: On API failure after sending.
        """
        chunks = self._split_message(text)
        for i, chunk in enumerate(chunks):
            await self._wait_for_rate_limit(chat_id)
            # Only add reply_markup to the last chunk if it's split
            markup = reply_markup if i == len(chunks) - 1 else None
            await self._send_chunk(chat_id, chunk, markup, parse_mode)

    async def _send_chunk(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        """Send a single message chunk via the Telegram API."""
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode or "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        url = self._url + "sendMessage"
        resp = await self._gateway.request(
            "POST",
            url,
            self._session,
            json=payload,
        )
        async with resp:
            if resp.status != 200:
                body = await resp.text()
                raise TelegramSendError(f"Telegram API returned {resp.status}: {body}")
            self._record_send(chat_id)

    async def _wait_for_rate_limit(self, chat_id: int) -> None:
        """Block until we are within the per-chat rate limit."""
        if chat_id not in self._send_times:
            self._send_times[chat_id] = collections.deque()

        times = self._send_times[chat_id]
        now = time.monotonic()

        # Prune entries older than 60 seconds.
        while times and now - times[0] > 60.0:
            times.popleft()

        if len(times) >= self._rate_limit:
            wait_seconds = 60.0 - (now - times[0])
            if wait_seconds > 0:
                logger.debug(
                    "Rate limit for chat_id=%d, waiting %.1fs", chat_id, wait_seconds
                )
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

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Edit an existing Telegram message."""
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        url = self._url + "editMessageText"
        resp = await self._gateway.request(
            "POST",
            url,
            self._session,
            json=payload,
        )
        async with resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("Failed to edit message: %s", body)
