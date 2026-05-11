"""
╔══════════════════════════════════════════════════════════════════╗
║  Agent Communication — WebSocket Bridge (NiceGUI ↔ Daemon)    ║
║  Sky-Claw v2.0 (2026)                                         ║
╚══════════════════════════════════════════════════════════════════╝

Async WebSocket client that runs inside NiceGUI's event loop.
Connects to the Background Daemon's /ws/ui endpoint, injects the
auth token, and translates incoming daemon messages into EventBus
events so ReactiveState can update Vue reactively.

ARCHITECTURE:
  NiceGUI (this client)  ──ws──▷  Background Daemon (ws_daemon.py)
       ▲                                    │
       └── EventBus.publish() ◁── JSON msg ─┘
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

try:
    import websockets
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
    )
except ImportError:
    websockets = None  # Graceful degradation if not installed

import contextlib

from sky_claw.antigravity.comms._transport import assert_safe_ws_url, authenticated_connect

logger = logging.getLogger("SkyClaw.AgentComm")


class AgentCommunicationClient:
    """
    Lightweight async WS client living in NiceGUI's asyncio loop.

    Responsibilities:
      • Connect to the daemon with X-Auth-Token header
      • Auto-reconnect with exponential backoff
      • Dispatch incoming messages via a callback (→ EventBus)
      • Expose send_command() for fire-and-forget UI → Daemon commands
    """

    _DISPATCH_QUEUE_MAX = 16
    _DISPATCH_WORKERS = 4
    _AUTH_REJECTION_CLOSE_CODES = frozenset({4001})

    def __init__(
        self,
        daemon_url: str = "ws://localhost:8765/ws/ui",
        on_message: Callable[[dict[str, Any]], None] | None = None,
        on_connection_change: Callable[[bool], None] | None = None,
        token_dir: str | None = None,
    ):
        self._daemon_url = assert_safe_ws_url(daemon_url)
        self._on_message = on_message
        self._on_connection_change = on_connection_change
        self._token_dir = token_dir
        self._ws = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._consecutive_auth_failures = 0
        self._auth_lockout_until: float = 0.0
        self._dispatch_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._DISPATCH_QUEUE_MAX)
        self._dispatch_workers: list[asyncio.Task[None]] = []

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Schedule the connection loop as a background task."""
        if websockets is None:
            logger.error("websockets library not installed — agent communication disabled. Run: pip install websockets")
            return
        self._running = True
        self._start_dispatch_workers()
        self._task = asyncio.create_task(self._connection_loop())
        logger.info("AgentCommunicationClient started -> %s", self._daemon_url)

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._drain_dispatch_queue()
        await self._stop_dispatch_workers()
        logger.info("AgentCommunicationClient stopped.")

    # ── Outbound (UI → Daemon) ────────────────────────────────────────

    async def send_command(self, command: str, payload: dict | None = None) -> bool:
        """
        Fire-and-forget: send a command to the daemon.

        Returns True if sent successfully, False otherwise.
        """
        if not self._ws:
            logger.warning("Cannot send — not connected to daemon.")
            return False

        msg = {
            "id": str(uuid.uuid4()),
            "type": "command",
            "command": command,
            "payload": payload or {},
            "timestamp": time.time(),
            "source": "nicegui_ui",
        }

        try:
            await self._ws.send(json.dumps(msg))
            logger.debug("Sent command '%s' to daemon.", command)
            return True
        except (ConnectionClosed, ConnectionClosedError, OSError, RuntimeError) as exc:
            logger.error("Failed to send command: %s", exc)
            return False

    async def send_chat_message(self, text: str) -> bool:
        """Convenience wrapper for chat messages."""
        return await self.send_command("chat", {"text": text})

    # ── Connection Loop ───────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        """Infinite reconnection loop with exponential backoff."""
        backoff = 2.0

        while self._running:
            try:
                # ── Rate-limiting: lockout after 5 consecutive auth failures ──
                if self._consecutive_auth_failures >= 5 and time.time() < self._auth_lockout_until:
                    remaining = int(self._auth_lockout_until - time.time())
                    logger.warning("Auth lockout active — %ds remaining", remaining)
                    await asyncio.sleep(min(remaining, 30))
                    continue

                async with authenticated_connect(
                    self._daemon_url,
                    token_dir=self._token_dir,
                    open_timeout=10,
                ) as ws:
                    self._ws = ws
                    backoff = 2.0  # Reset on success
                    self._consecutive_auth_failures = 0
                    self._auth_lockout_until = 0.0
                    logger.info("✅ Connected to Background Daemon (UI channel).")

                    if self._on_connection_change:
                        self._on_connection_change(True)

                    await self._listen(ws)

            except (
                ConnectionClosed,
                ConnectionClosedError,
                ConnectionRefusedError,
                OSError,
            ) as e:
                if not self._running:
                    break
                logger.warning(
                    "Daemon connection lost (%s). Reconnecting in %.1fs...",
                    type(e).__name__,
                    backoff,
                )

                # H-04: Only count genuine auth rejections (daemon close code
                # 4001) towards the lockout counter.  Network
                # errors (ConnectionRefusedError, OSError, generic closures)
                # must NOT trigger lockout — a daemon restart is routine.
                if self._is_auth_rejection(e):
                    self._consecutive_auth_failures += 1
                    if self._consecutive_auth_failures >= 5:
                        self._auth_lockout_until = time.time() + 300.0
                        logger.warning("AUTH LOCKOUT: 5 consecutive auth rejections. Pausing 5 min.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Unexpected error in agent comm: %s", e)
            finally:
                self._ws = None
                if self._on_connection_change:
                    self._on_connection_change(False)

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

    async def _listen(self, ws) -> None:
        """Process incoming messages from the daemon.

        M-02: The ``on_message`` callback is dispatched asynchronously.
        If the callback is a coroutine function, it is awaited directly.
        If it is a synchronous callable, it is wrapped in a task to avoid
        blocking the event loop.
        """
        async for raw in ws:
            try:
                data = json.loads(raw)
                msg_type = data.get("type", "unknown")
                logger.debug("Received '%s' from daemon.", msg_type)

                if self._on_message:
                    if inspect.iscoroutinefunction(self._on_message):
                        await self._on_message(data)
                    else:
                        await self._enqueue_sync_callback(data)

            except json.JSONDecodeError:
                logger.error("Received malformed JSON from daemon.")
            except asyncio.QueueFull:
                logger.warning(
                    "Dropping daemon message because callback queue is full (%d).",
                    self._dispatch_queue.maxsize,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Error processing daemon message: %s", e)

    def _is_auth_rejection(self, exc: BaseException) -> bool:
        """Return True when the daemon explicitly rejected WS authentication."""
        return getattr(exc, "code", None) in self._AUTH_REJECTION_CLOSE_CODES

    def _start_dispatch_workers(self) -> None:
        """Start bounded workers for synchronous callback dispatch."""
        self._dispatch_workers = [worker for worker in self._dispatch_workers if not worker.done()]
        if self._dispatch_workers:
            return
        for idx in range(self._DISPATCH_WORKERS):
            self._dispatch_workers.append(
                asyncio.create_task(self._dispatch_worker(), name=f"agent-comm-dispatch-{idx}")
            )

    async def _enqueue_sync_callback(self, data: dict[str, Any]) -> None:
        """Queue sync callback work without blocking the websocket reader."""
        self._dispatch_queue.put_nowait(data)

    async def _dispatch_worker(self) -> None:
        """Run synchronous callbacks through the loop's bounded worker queue."""
        while True:
            data = await self._dispatch_queue.get()
            try:
                if self._on_message is not None:
                    await asyncio.to_thread(self._on_message, data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Synchronous on_message callback failed: %s", exc)
            finally:
                self._dispatch_queue.task_done()

    async def _drain_dispatch_queue(self) -> None:
        """Wait briefly for queued callbacks to finish before shutdown."""
        try:
            await asyncio.wait_for(self._dispatch_queue.join(), timeout=5.0)
        except TimeoutError:
            logger.warning(
                "Timed out draining callback queue with %d pending items.",
                self._dispatch_queue.qsize(),
            )

    async def _stop_dispatch_workers(self) -> None:
        """Cancel all callback workers and wait for cancellation."""
        workers = [worker for worker in self._dispatch_workers if not worker.done()]
        for worker in workers:
            worker.cancel()
        for worker in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._ws is not None
