import asyncio
import json
import logging

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from sky_claw.antigravity.comms._transport import (
    assert_safe_ws_url,
    authenticated_connect,
)
from sky_claw.antigravity.core.models import HitlApprovalRequest

logger = logging.getLogger("SkyClaw.Interface")


class InterfaceAgent:
    def __init__(self, gateway_url: str = "ws://127.0.0.1:18789", *, token_dir: str | None = None):
        self.gateway_url = assert_safe_ws_url(gateway_url)
        self._token_dir = token_dir
        self.ws_connection = None
        self._pending_hitl = {}
        self._command_callbacks = []

    async def connect(self):
        """Bucle de reconexión infinita. Garantiza supervivencia del demonio."""
        backoff = 2.0
        while True:
            try:
                self.ws_connection = await authenticated_connect(self.gateway_url, token_dir=self._token_dir)
                logger.info(f"Conectado al Gateway Node.js en {self.gateway_url}")
                backoff = 2.0  # Reset backoff tras conexión exitosa
                await self._listen_to_gateway()
            except (
                ConnectionClosed,
                ConnectionClosedError,
                ConnectionRefusedError,
                OSError,
            ) as e:
                logger.warning(
                    f"RCA: Enlace con Gateway perdido ({type(e).__name__}: {e!s}). Reconectando silenciosamente en {backoff}s..."
                )
                self.ws_connection = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)  # Backoff exponencial truncado a 30s

    async def _listen_to_gateway(self):
        async for message in self.ws_connection:
            data = json.loads(message)
            if data.get("type") == "hitl_response":
                req_id = data["request_id"]
                if req_id in self._pending_hitl:
                    self._pending_hitl[req_id]["decision"] = data["decision"]
                    self._pending_hitl[req_id]["event"].set()
            elif data.get("type") == "EJECUTAR":
                logger.info("Señal 'EJECUTAR' recibida desde el Gateway.")
                for callback in self._command_callbacks:
                    asyncio.create_task(callback(data))

    async def request_hitl(self, req: HitlApprovalRequest) -> str:
        # Si no hay conexión, aborta por seguridad en lugar de colgar el agente
        if not self.ws_connection:
            logger.error("RCA: Intento de HITL sin conexión a Gateway. Abortando acción destructiva.")
            return "denied"

        import uuid

        req_id = str(uuid.uuid4())
        event = asyncio.Event()
        self._pending_hitl[req_id] = {"event": event, "decision": None}

        payload = {
            "type": "hitl_request",
            "request_id": req_id,
            "data": req.model_dump(),
        }
        await self.ws_connection.send(json.dumps(payload))
        logger.info(f"HITL emitido. Bloqueando rutina (ReqID: {req_id})")

        try:
            await asyncio.wait_for(event.wait(), timeout=300.0)
            return self._pending_hitl[req_id]["decision"]
        except TimeoutError:
            logger.warning(f"HITL Timeout ({req_id}). Asumiendo DENIED.")
            return "denied"
        finally:
            self._pending_hitl.pop(req_id, None)

    def register_command_callback(self, callback):
        """Registra un callback asincrónico para mensajes de ejecución."""
        self._command_callbacks.append(callback)

    async def send_event(self, event_type: str, payload: dict) -> None:
        """Emite un evento tipado al Gateway con el contrato JSON estandarizado.

        Contrato: {"type": <str>, "payload": <dict>, "timestamp": <epoch_ms>}
        """
        if not self.ws_connection:
            return
        try:
            import time as _t

            msg = {
                "type": event_type,
                "payload": payload,
                "timestamp": int(_t.time() * 1000),
            }
            await self.ws_connection.send(json.dumps(msg))
        except Exception as e:
            logger.error(f"Fallo al enviar evento '{event_type}': {e}")

    async def send_telemetry(self, telemetry_data: dict) -> None:
        """Compat shim: reenvía a send_event('telemetry', ...)."""
        await self.send_event("telemetry", telemetry_data)
