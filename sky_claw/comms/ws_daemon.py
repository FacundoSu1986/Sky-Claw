from __future__ import annotations

import asyncio
import json
import logging
import websockets
import uuid
import time
from websockets.exceptions import ConnectionClosed, ConnectionClosedError
import sys
from pathlib import Path
from typing import Set, Optional

# Zero Trust AST Import (Local Repo Resolution)
WORK_DIR = Path(__file__).resolve().parent.parent.parent
AST_SKILLS_PATH = WORK_DIR / ".agents" / "skills" / "skyclaw-purple-auditor" / "scripts"
if str(AST_SKILLS_PATH) not in sys.path:
    sys.path.append(str(AST_SKILLS_PATH))
import ast_guardian  # noqa: E402

from sky_claw.security.auth_token_manager import AuthTokenManager  # noqa: E402

# Set-up standard 2026 logging
logger = logging.getLogger("SkyClaw.TelegramDaemon")


class TelegramDaemon:
    """
    TELEGRAM WS DAEMON (STANDARD 2026)

    Asynchronous client for the Telegram Gateway.
    Orchestrates command injection into the LLM Router.
    """

    def __init__(
        self,
        router,
        session,
        gateway_url="ws://localhost:8080",
        ui_broadcast: Optional["UIBroadcastServer"] = None,
    ):
        self.router = router
        self.session = session
        self.gateway_url = gateway_url
        self.ws = None
        self._is_running = False
        self._running_lock = asyncio.Lock()
        self.ui_broadcast = ui_broadcast

        # Instanciando el guardián de seguridad (AST Purple Auditor)
        self.guardian = ast_guardian.ASTGuardian()

    async def start(self):
        """Infinite reconnection loop with exponential backoff."""
        async with self._running_lock:
            self._is_running = True
        backoff = 2.0
        logger.info(f"🚀 Iniciando TelegramDaemon (Cliente WS) -> {self.gateway_url}")

        while self._is_running:
            try:
                # Use a custom connection timeout to prevent hanging
                async with websockets.connect(self.gateway_url, open_timeout=10) as ws:
                    self.ws = ws
                    logger.info(
                        "✅ Enlace establecido con Telegram Gateway (Stateless Perimeter Layer)."
                    )
                    backoff = 2.0  # Reset backoff upon successful connection
                    await self._listen_loop()
            except (
                ConnectionClosed,
                ConnectionClosedError,
                ConnectionRefusedError,
                OSError,
            ) as e:
                if not self._is_running:
                    break
                logger.warning(
                    f"⚠️ Enlace perdido con Gateway ({type(e).__name__}). Reconectando en {backoff:.1f}s..."
                )
                self.ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)
            except Exception as e:
                logger.error(f"❌ Fallo fatal en TelegramDaemon: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        """Graceful shutdown of the daemon."""
        async with self._running_lock:
            self._is_running = False
        if self.ws:
            await self.ws.close()
            logger.info("🛑 TelegramDaemon detenido de forma segura.")

    async def _listen_loop(self):
        """Main listening loop for incoming Telegram messages."""
        async for message in self.ws:
            try:
                data = json.loads(message)
                msg_id = data.get("id")

                # Zero Trust ACK (Mandatory by standard 2026)
                ack = {
                    "id": msg_id,
                    "type": "ack",
                    "status": "received",
                    "timestamp": time.time(),
                }
                await self.ws.send(json.dumps(ack))

                # Command injection for asynchronous processing
                if data.get("type") == "command":
                    task = asyncio.create_task(self._inject_to_router(data))
                    task.add_done_callback(
                        lambda t: (
                            logger.error(
                                "Error no manejado en _inject_to_router: %s",
                                t.exception(),
                            )
                            if not t.cancelled() and t.exception() is not None
                            else None
                        )
                    )

            except json.JSONDecodeError:
                logger.error("🚫 Recibido JSON malformado desde el Gateway.")
            except Exception as e:
                logger.exception(f"⚠️ Error procesando flujo de WebSocket: {e}")

    async def _inject_to_router(self, data):
        """Dispatches the command to the LLM agent and relays response via WS."""
        payload = data.get("payload", {})
        text = payload.get("text")
        msg_id = data.get("id")

        if not text:
            logger.debug(f"Payload vacío en mensaje {msg_id}. Ignorando.")
            return

        # Standardized chat session for Telegram bridge
        chat_id = f"tg-{data.get('metadata', {}).get('user_id', 'standard')}"

        try:
            logger.debug(f"📥 Procesando comando [Telegram]: '{text[:60]}...'")

            # Misión 2: Auditoría Zero-Trust Async
            is_safe = await self.guardian.execute_audit("telegram_payload", text)
            if not is_safe:
                logger.warning(
                    "🚫 Auditoría AST falló. Comando descartado por políticas Zero-Trust."
                )
                if self.ws and getattr(self.ws, "open", False):
                    err_msg = json.dumps(
                        {
                            "id": str(uuid.uuid4()),
                            "type": "error",
                            "payload": {
                                "text": "🛡️ Sistema: Payload inyectado fue bloqueado preventivamente."
                            },
                        }
                    )
                    await self.ws.send(err_msg)
                return

            # 100% async non-blocking injection with telemetry
            async def _progress_callback(status: str, progress: int):
                if self.ws and self.ws.open:
                    telemetry = {
                        "id": str(uuid.uuid4()),
                        "type": "telemetry",
                        "status": status,
                        "progress": progress,
                        "metadata": {"reply_to": msg_id},
                    }
                    await self.ws.send(json.dumps(telemetry))

            response = await self.router.chat(
                text,
                self.session,
                chat_id=chat_id,
                metadata=data.get("metadata", {}),
                progress_callback=_progress_callback,
            )

            # Construct standard 2026 response payload
            if self.ws and self.ws.open:
                res_payload = {
                    "id": str(uuid.uuid4()),
                    "type": "response",
                    "action": "reply",
                    "payload": {"text": response},
                    "metadata": {
                        "reply_to": msg_id,
                        "channel": "telegram",
                        "processed_at": time.time(),
                    },
                }
                await self.ws.send(json.dumps(res_payload))
                logger.info(
                    f"📤 Respuesta enviada al Gateway (ID Relacionado: {msg_id})"
                )

            # ── NEW: Broadcast to NiceGUI UI clients ──
            if self.ui_broadcast:
                await self.ui_broadcast.broadcast(
                    {
                        "type": "agent_result",
                        "action": "chat_response",
                        "payload": {"text": response},
                        "metadata": {"channel": "telegram", "reply_to": msg_id},
                    }
                )

        except Exception as e:
            logger.exception(f"❌ Error en Bridge Agent (Injection Layer): {e}")
            if self.ws and self.ws.open:
                err_payload = {
                    "type": "error",
                    "payload": {
                        "text": f"SISTEMA: Error en procesamiento del comando: {str(e)}"
                    },
                }
                await self.ws.send(json.dumps(err_payload))


# ═══════════════════════════════════════════════════════════════════════════════
# UI BROADCAST SERVER — WebSocket endpoint for NiceGUI clients
# ═══════════════════════════════════════════════════════════════════════════════


class UIBroadcastServer:
    """
    Lightweight WS server that NiceGUI's AgentCommunicationClient connects to.

    Validates X-Auth-Token on upgrade, then pushes AGENT_RESULT / BROADCAST
    events to all connected UI clients.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self._clients: Set = set()
        self._server = None
        self._auth = AuthTokenManager()
        self._logger = logging.getLogger("SkyClaw.UIBroadcast")

    async def start(self) -> None:
        """Generate auth token and start the WS server."""
        self._auth.generate()
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
        )
        self._logger.info(
            f"🌐 UIBroadcastServer listening on ws://{self.host}:{self.port}/ws/ui"
        )

    async def stop(self) -> None:
        """Shutdown the server and revoke the token."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self._auth.revoke()
        self._logger.info("🛑 UIBroadcastServer stopped.")

    async def broadcast(self, message: dict) -> None:
        """Send a JSON message to all connected UI clients."""
        if not self._clients:
            return

        payload = json.dumps(message)
        disconnected = set()

        for ws in self._clients:
            try:
                await ws.send(payload)
            except (ConnectionClosed, ConnectionClosedError):
                disconnected.add(ws)
            except Exception as e:
                self._logger.error(f"Broadcast error: {e}")
                disconnected.add(ws)

        self._clients -= disconnected

    async def _handler(self, websocket, path: str = "") -> None:
        """Handle incoming UI client connections with token validation."""
        # ── Auth gate ──
        token = websocket.request_headers.get("X-Auth-Token", "")
        if not self._auth.validate(token):
            self._logger.warning(
                f"🚫 Rejected UI client — invalid token from {websocket.remote_address}"
            )
            await websocket.close(4001, "Unauthorized")
            return

        self._clients.add(websocket)
        self._logger.info(f"✅ UI client connected ({len(self._clients)} total)")

        try:
            # Listen for commands from the UI (chat messages, etc.)
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                    msg_type = data.get("type", "")

                    if msg_type == "command":
                        # Forward to router via an event or direct call
                        self._logger.debug(
                            f"📥 UI command received: {data.get('command')}"
                        )
                        # Emit ack
                        await websocket.send(
                            json.dumps(
                                {
                                    "id": data.get("id", str(uuid.uuid4())),
                                    "type": "ack",
                                    "status": "received",
                                    "timestamp": time.time(),
                                }
                            )
                        )

                except json.JSONDecodeError:
                    self._logger.error("Malformed JSON from UI client.")

        except (ConnectionClosed, ConnectionClosedError):
            pass
        finally:
            self._clients.discard(websocket)
            self._logger.info(
                f"UI client disconnected ({len(self._clients)} remaining)"
            )
