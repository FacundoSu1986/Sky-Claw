from __future__ import annotations

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

# T5: decisiones HITL aceptadas. request_hitl retorna exactamente estos valores;
# cualquier otra cosa en un frame hitl_response se ignora como malformada.
_VALID_HITL_DECISIONS = frozenset({"approved", "denied"})


class InterfaceAgent:
    def __init__(self, gateway_url: str = "ws://127.0.0.1:18789", *, token_dir: str | None = None):
        self.gateway_url = assert_safe_ws_url(gateway_url)
        self._token_dir = token_dir
        self.ws_connection = None
        self._pending_hitl = {}
        self._command_callbacks = []
        # PR-F (RUF006): mantener referencias fuertes a tasks de dispatch
        # de EJECUTAR signal. Sin esto el GC puede recolectar las tasks
        # pendientes silenciosamente — mismo patron del bug T1-01.
        self._pending_dispatch: set[asyncio.Task[None]] = set()

    async def connect(self):
        """Bucle de reconexión infinita. Garantiza supervivencia del demonio."""
        backoff = 2.0
        disconnected_logged = False
        while True:
            try:
                self.ws_connection = await authenticated_connect(self.gateway_url, token_dir=self._token_dir)
                logger.info(f"Conectado al Gateway Node.js en {self.gateway_url}")
                backoff = 2.0  # Reset backoff tras conexión exitosa
                disconnected_logged = False  # re-arm WARNING for the next drop
                await self._listen_to_gateway()
            except (
                ConnectionClosed,
                ConnectionClosedError,
                ConnectionRefusedError,
                OSError,
            ) as e:
                # Log on state change: WARNING on the first drop, DEBUG on the
                # subsequent retries while still down. Without this, a Gateway
                # that is simply not deployed floods the log with a WARNING
                # every ≤30s forever. Re-armed to WARNING after a reconnect.
                level = logging.DEBUG if disconnected_logged else logging.WARNING
                logger.log(
                    level,
                    "RCA: Enlace con Gateway perdido (%s: %s). Reconectando silenciosamente en %ss...",
                    type(e).__name__,
                    e,
                    backoff,
                )
                disconnected_logged = True
                self.ws_connection = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)  # Backoff exponencial truncado a 30s

    async def _listen_to_gateway(self):
        async for message in self.ws_connection:
            # M-5: un frame malformado (no-JSON, o sin request_id/decision) NO
            # debe tumbar el loop. json.loads / accesos por clave lanzaban
            # JSONDecodeError/KeyError que connect() no captura → propagaban al
            # TaskGroup del supervisor y crasheaban el proceso, deteniendo todos
            # los demonios. Se aísla por-mensaje (igual que ws_daemon/frontend_bridge).
            try:
                data = json.loads(message)
                if data.get("type") == "hitl_response":
                    req_id = data.get("request_id")
                    decision = data.get("decision")
                    # T5 (review PR #257): sólo resolver el HITL pendiente con una
                    # decisión VÁLIDA. Un frame con request_id pero sin decision (o
                    # con un valor inesperado) despertaba la espera con None,
                    # terminándola prematuramente (prompts duplicados / falso deny).
                    # Un frame así se ignora y la espera sigue hasta una decisión real.
                    if req_id is not None and req_id in self._pending_hitl and decision in _VALID_HITL_DECISIONS:
                        self._pending_hitl[req_id]["decision"] = decision
                        self._pending_hitl[req_id]["event"].set()
                    elif req_id in self._pending_hitl:
                        logger.warning("hitl_response con decision inválida %r (req=%s); ignorado", decision, req_id)
                elif data.get("type") == "EJECUTAR":
                    logger.info("Señal 'EJECUTAR' recibida desde el Gateway.")
                    for callback in self._command_callbacks:
                        task = asyncio.create_task(callback(data), name="interface-ejecutar")
                        self._pending_dispatch.add(task)
                        task.add_done_callback(self._on_dispatch_done)
            except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as exc:
                logger.warning("Frame malformado del Gateway ignorado: %s", exc)
                continue

    def _on_dispatch_done(self, task: asyncio.Task[None]) -> None:
        """PR-F (RUF006): libera la referencia + loggea excepciones del callback."""
        self._pending_dispatch.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Comando EJECUTAR callback fallido: %s", exc, exc_info=(type(exc), exc, exc.__traceback__))

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
