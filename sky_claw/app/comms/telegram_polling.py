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
        self._owns_session = False
        self._interval = interval
        self._authorized_chat_id = authorized_chat_id
        # review #257: loguear el fail-closed una sola vez por instancia (evita
        # spam de ERROR si llegan muchos updates con el operador sin configurar).
        self._fail_closed_logged = False
        self._last_update_id = 0
        self._running = False
        self._url = TELEGRAM_API_GET_UPDATES.format(token=token)
        self._dlq: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._polling_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Inicia el ciclo de polling después de validar sus precondiciones."""
        if self._running:
            return
        if self._gateway is None:
            raise RuntimeError("TelegramPolling requires a NetworkGateway for outbound traffic")

        self._running = True
        self._polling_task = asyncio.create_task(self._run_loop())
        self._polling_task.add_done_callback(self._on_polling_task_done)
        logger.info("Telegram long polling started")

    def _on_polling_task_done(self, task: asyncio.Task[None]) -> None:
        """Limpia tareas terminadas sin ocultar fallos inesperados."""
        if task.cancelled():
            if self._polling_task is task:
                self._polling_task = None
            return

        exception = task.exception()
        if exception is None:
            if self._polling_task is task:
                self._polling_task = None
            return

        # Se conserva la referencia para que stop() pueda propagar el fallo si
        # la tarea se recolecta posteriormente.
        logger.error(
            "La tarea de polling terminó con un error inesperado: %s",
            exception,
            exc_info=(type(exception), exception, exception.__traceback__),
        )

    async def stop(self) -> None:
        """Detiene el polling y espera la limpieza de su tarea.

        El método es idempotente. Consume únicamente la cancelación iniciada
        sobre la tarea de polling; una cancelación de la tarea que invoca
        ``stop`` se conserva y se propaga después de recolectar la tarea hija.
        """
        self._running = False
        task = self._polling_task
        if task is None:
            logger.info("Telegram long polling stopped")
            return

        current_task = asyncio.current_task()
        if task is current_task:
            # La tarea no puede esperarse a sí misma. El ciclo actual ejecutará
            # su cleanup sin que stop() vuelva a esperarlo.
            self._polling_task = None
            logger.info("Telegram long polling stopped from polling task")
            return

        initial_cancellation_count = current_task.cancelling() if current_task is not None else 0
        if not task.done():
            task.cancel()

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            caller_was_cancelled = current_task is not None and current_task.cancelling() > initial_cancellation_count
            if caller_was_cancelled:
                await self._drain_task(task)
                raise
            if not task.cancelled():
                raise
        finally:
            if self._polling_task is task and task.done():
                self._polling_task = None

        logger.info("Telegram long polling stopped")

    async def _drain_task(self, task: asyncio.Task[None]) -> None:
        """Recolecta una tarea cancelada aunque se cancele quien invoca ``stop``."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                logger.exception("Fallo inesperado al recolectar la tarea de polling")

    async def _run_loop(self) -> None:
        """Ejecuta el ciclo interno de polling."""
        own_session = self._session is None
        try:
            if self._gateway is None:
                raise RuntimeError("TelegramPolling requires a NetworkGateway for outbound traffic")

            if own_session:
                # S1-FIX: enruta la sesión por GatewayTCPConnector para fijar DNS y bloquear SSRF.
                from sky_claw.app.security.network_gateway import GatewayTCPConnector

                self._session = aiohttp.ClientSession(
                    connector=GatewayTCPConnector(self._gateway, limit=10),
                )
                self._owns_session = True

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
            if own_session:
                session = self._session
                self._session = None
                self._owns_session = False
                if session is not None and not session.closed:
                    await session.close()

    async def _poll_once(self) -> None:
        """Realiza una petición getUpdates."""
        if self._session is None:
            return

        params = {
            "offset": self._last_update_id + 1,
            "timeout": 30,
        }
        async with await self._gateway.request("GET", self._url, self._session, params=params) as resp:
            if resp.status != 200:
                logger.warning("Telegram getUpdates returned %d", resp.status)
                return

            data = await resp.json()
            if not data.get("ok"):
                logger.warning("Telegram getUpdates failed: %s", data.get("description"))
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

                if update_id is not None:
                    self._last_update_id = update_id

    async def _process_raw_update(self, update: dict[str, Any]) -> None:
        """Process a single raw update dict."""

        # CWE-284: Drop-Early Middleware.
        # C-2: fail-closed. Sin operador configurado NO se procesa ningún update:
        # de lo contrario cualquier usuario que descubra el bot podría disparar
        # tools mutantes (uninstall_mod, run_loot_sort, download_mod, ...).
        if self._authorized_chat_id is None:
            # review #257: la clave real es `telegram_chat_id` en la config (o el
            # flag CLI `--operator-chat-id`), no `telegram.operator_chat_id`. Y se
            # loguea una sola vez por instancia para no spammear ERROR por update.
            if not self._fail_closed_logged:
                logger.error(
                    "authorized_chat_id no configurado (fail-closed): se descartan TODOS los updates de "
                    "Telegram. Configure `telegram_chat_id` en la config (o el flag --operator-chat-id) "
                    "para habilitar el procesamiento."
                )
                self._fail_closed_logged = True
            return

        message = (
            update.get("message") or update.get("edited_message") or update.get("callback_query", {}).get("message")
        )
        if message:
            chat_id = message.get("chat", {}).get("id")
            if chat_id is not None and str(chat_id) != str(self._authorized_chat_id):
                logger.warning(
                    "CWE-284: Unauthorized access attempt from chat_id=%s. Dropping update.",
                    chat_id,
                )
                return

        if not hasattr(self._handler, "process_update"):
            raise TypeError("Handler must implement process_update(data: dict)")
        await self._handler.process_update(update)
