import asyncio
import json
import logging
import websockets
import uuid
import time
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

# Set-up standard 2026 logging
logger = logging.getLogger("SkyClaw.TelegramDaemon")

class TelegramDaemon:
    """
    TELEGRAM WS DAEMON (STANDARD 2026)
    
    Asynchronous client for the Telegram Gateway.
    Orchestrates command injection into the LLM Router.
    """
    def __init__(self, router, session, gateway_url="ws://localhost:8080"):
        self.router = router
        self.session = session
        self.gateway_url = gateway_url
        self.ws = None
        self._is_running = False

    async def start(self):
        """Infinite reconnection loop with exponential backoff."""
        self._is_running = True
        backoff = 2.0
        logger.info(f"🚀 Iniciando TelegramDaemon (Cliente WS) -> {self.gateway_url}")
        
        while self._is_running:
            try:
                # Use a custom connection timeout to prevent hanging
                async with websockets.connect(self.gateway_url, open_timeout=10) as ws:
                    self.ws = ws
                    logger.info("✅ Enlace establecido con Telegram Gateway (Stateless Perimeter Layer).")
                    backoff = 2.0  # Reset backoff upon successful connection
                    await self._listen_loop()
            except (ConnectionClosed, ConnectionClosedError, ConnectionRefusedError, OSError) as e:
                if not self._is_running:
                    break
                logger.warning(f"⚠️ Enlace perdido con Gateway ({type(e).__name__}). Reconectando en {backoff:.1f}s...")
                self.ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)
            except Exception as e:
                logger.error(f"❌ Fallo fatal en TelegramDaemon: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        """Graceful shutdown of the daemon."""
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
                    "timestamp": time.time()
                }
                await self.ws.send(json.dumps(ack))
                
                # Command injection for asynchronous processing
                if data.get("type") == "command":
                    asyncio.create_task(self._inject_to_router(data))
                
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
        # We use a static key to maintain conversation history in SQLite for this channel
        chat_id = f"tg-{data.get('metadata', {}).get('user_id', 'standard')}"
        
        try:
            logger.info(f"📥 Procesando comando [Telegram]: '{text[:60]}...'")
            
            # 100% async non-blocking injection
            response = await self.router.chat(text, self.session, chat_id=chat_id)
            
            # Construct standard 2026 response payload
            if self.ws and self.ws.open:
                res_payload = {
                    "id": str(uuid.uuid4()),
                    "type": "response",
                    "action": "reply",
                    "payload": {
                        "text": response
                    },
                    "metadata": {
                        "reply_to": msg_id,
                        "channel": "telegram",
                        "processed_at": time.time()
                    }
                }
                await self.ws.send(json.dumps(res_payload))
                logger.info(f"📤 Respuesta enviada al Gateway (ID Relacionado: {msg_id})")
                
        except Exception as e:
            logger.exception(f"❌ Error en Bridge Agent (Injection Layer): {e}")
            if self.ws and self.ws.open:
                err_payload = {
                    "type": "error",
                    "payload": {
                        "text": f"SISTEMA: Error en procesamiento del comando: {str(e)}"
                    }
                }
                await self.ws.send(json.dumps(err_payload))
