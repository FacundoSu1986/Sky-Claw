"""Telegram Long Polling — active update retrieval.

Retrieves updates from Telegram via the getUpdates method. This is
suitable for local development or instances behind NAT that cannot
receive webhooks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import aiohttp


class UpdateHandler(Protocol):
    async def process_update(self, data: dict[str, Any]) -> None: ...


logger = logging.getLogger(__name__)

TELEGRAM_API_GET_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"


class TelegramPolling:
    """Long polling client for Telegram Bot API.

    Args:
        token: Telegram Bot API token.
        webhook_handler: Instance of :class:`TelegramWebhook` to process updates.
        session: Shared aiohttp session.
        interval: Polling interval in seconds (default: 1.0).
    """

    def __init__(
        self,
        token: str,
        webhook_handler: UpdateHandler,
        gateway: Any,  # NetworkGateway
        session: aiohttp.ClientSession | None = None,
        interval: float = 1.0,
        authorized_chat_id: str | int | None = None,
    ) -> None:
        self._token = token
        self._handler = webhook_handler
        self._gateway = gateway
        self._session = session
        self._interval = interval
        self._authorized_chat_id = authorized_chat_id
        self._last_update_id = 0
        self._running = False
        self._url = TELEGRAM_API_GET_UPDATES.format(token=token)
        self._dlq: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._session: aiohttp.ClientSession | None = None
        self._polling_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the polling loop."""
        if self._running:
            return
        self._running = True
        logger.info("Telegram long polling started")
        self._polling_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
        logger.info("Telegram long polling stopped")

    async def _run_loop(self) -> None:
        """Internal polling loop."""
        own_session = False
        if self._session is None:
            self._session = aiohttp.ClientSession()
            own_session = True
        try:
            while self._running:
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.exception("Error in Telegram polling loop: %s", exc)

                await asyncio.sleep(self._interval)
        finally:
            self._running = False
            if own_session and self._session and not self._session.closed:
                await self._session.close()
                self._session = None

    async def _poll_once(self) -> None:
        """Perform a single getUpdates request."""
        if not self._session:
            return

        params = {
            "offset": self._last_update_id + 1,
            "timeout": 30,
        }
        async with await self._gateway.request(
            "GET", self._url, self._session, params=params
        ) as resp:
            if resp.status != 200:
                logger.warning("Telegram getUpdates returned %d", resp.status)
                return

            data = await resp.json()
            if not data.get("ok"):
                logger.warning(
                    "Telegram getUpdates failed: %s", data.get("description")
                )
                return

            results = data.get("result", [])
            for update in results:
                update_id = update.get("update_id")

                try:
                    await self._process_raw_update(update)
                except Exception as exc:
                    logger.error(
                        "Malformed update or processing error for %s: %s. Routing to DLQ.",
                        update_id,
                        exc,
                    )
                    await self._dlq.put(update)

                if update_id:
                    self._last_update_id = update_id

    async def _process_raw_update(self, update: dict[str, Any]) -> None:
        """Process a single raw update dict."""

        # CWE-284: Drop-Early Middleware
        if self._authorized_chat_id is not None:
            message = (
                update.get("message")
                or update.get("edited_message")
                or update.get("callback_query", {}).get("message")
            )
            if message:
                chat_id = message.get("chat", {}).get("id")
                if chat_id is not None and str(chat_id) != str(
                    self._authorized_chat_id
                ):
                    logger.warning(
                        "CWE-284: Unauthorized access attempt from chat_id=%s. Dropping update.",
                        chat_id,
                    )
                    return

        if not hasattr(self._handler, "process_update"):
            raise TypeError("Handler must implement process_update(data: dict)")
        await self._handler.process_update(update)
