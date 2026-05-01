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

import asyncio
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

from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager

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

    def __init__(
        self,
        daemon_url: str = "ws://localhost:8765/ws/ui",
        on_message: Callable[[dict[str, Any]], None] | None = None,
        on_connection_change: Callable[[bool], None] | None = None,
        token_dir: str | None = None,
    ):
        self._daemon_url = daemon_url
        self._on_message = on_message
        self._on_connection_change = on_connection_change
        self._token_dir = token_dir
        self._ws = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._consecutive_auth_failures = 0
        self._auth_lockout_until: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Schedule the connection loop as a background task."""
        if websockets is None:
            logger.error("websockets library not installed — agent communication disabled. Run: pip install websockets")
            return
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())
        logger.info(f"AgentCommunicationClient started → {self._daemon_url}")

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
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
            logger.debug(f"📤 Sent command '{command}' to daemon.")
            return True
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
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
                    logger.warning(f"Auth lockout active — {remaining}s remaining")
                    await asyncio.sleep(min(remaining, 30))
                    continue

                # Read token for authentication
                token = AuthTokenManager.read_token_file(self._token_dir)
                extra_headers = {}
                if token:
                    extra_headers["X-Auth-Token"] = token

                async with websockets.connect(
                    self._daemon_url,
                    open_timeout=10,
                    extra_headers=extra_headers,
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
                logger.warning(f"⚠️ Daemon connection lost ({type(e).__name__}). Reconnecting in {backoff:.1f}s...")
                self._consecutive_auth_failures += 1
                if self._consecutive_auth_failures >= 5:
                    self._auth_lockout_until = time.time() + 300.0
                    logger.warning("AUTH LOCKOUT: 5 consecutive failures. Pausing 5 min.")
            except Exception as e:
                logger.error(f"❌ Unexpected error in agent comm: {e}")
            finally:
                self._ws = None
                if self._on_connection_change:
                    self._on_connection_change(False)

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

    async def _listen(self, ws) -> None:
        """Process incoming messages from the daemon."""
        async for raw in ws:
            try:
                data = json.loads(raw)
                msg_type = data.get("type", "unknown")
                logger.debug(f"📥 Received '{msg_type}' from daemon.")

                if self._on_message:
                    self._on_message(data)

            except json.JSONDecodeError:
                logger.error("Received malformed JSON from daemon.")
            except Exception as e:
                logger.exception(f"Error processing daemon message: {e}")

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._ws is not None
