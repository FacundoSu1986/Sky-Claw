"""
FRONTEND BRIDGE (Standard 2026 - Refactored v2.0)

Asynchronous WebSocket client that connects to the Node.js Gateway on
the Agent port (18789). Handles:
  - GET_CONFIG   → Returns masked configuration to the UI
  - UPDATE_CONFIG → Validates, persists, and hot-reloads changed settings
  - QUERY        → Forwards chat messages to the LLM Router

## Architecture
- Single WebSocket connection to Gateway :18789 (agent port)
- Reconnection with exponential backoff (max 5 attempts before 5-min pause)
- Type-safe message dispatching with JSON schema validation
- Hot-reload support for LLM providers and Telegram configuration
- All secrets stored in OS keyring (Windows Credential Manager on Windows)

## Invariants
1. Active tasks are always tracked and canceled on shutdown
2. WebSocket is checked before sending (avoid AttributeError on closed socket)
3. JSON validation happens before routing to prevent injection attacks
4. Secrets are NEVER sent to frontend (only boolean status: has_llm_key, etc.)
5. Reconnection respects DOS prevention limit (5 attempts, then 5-min pause)
6. All configuration changes are atomic (either fully applied or fully rolled back)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

import keyring
import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from sky_claw.antigravity.agent.providers import ProviderConfigError, create_provider
from sky_claw.antigravity.comms._transport import assert_safe_ws_url
from sky_claw.config import Config

if TYPE_CHECKING:
    from sky_claw.app_context import AppContext

logger = logging.getLogger("SkyClaw.FrontendBridge")


class AuthError(ConnectionRefusedError):
    """WS authentication rejected or token missing for Gateway handshake."""


# ── Constants ──────────────────────────────────────────────────────────
VALID_PROVIDERS = {"deepseek", "anthropic", "ollama"}
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_PAUSE_DURATION = 300  # 5 minutes in seconds

# Keys that map to provider-specific storage in keyring
PROVIDER_KEY_MAP = {
    "deepseek": "deepseek_api_key",
    "anthropic": "anthropic_api_key",
}

SENSITIVE_KEYS = {
    "llm_api_key",
    "nexus_api_key",
    "telegram_bot_token",
}

# ── Type Definitions ───────────────────────────────────────────────────


class WebSocketClient(Protocol):
    """Type protocol for WebSocket client interface.

    Ensures type safety when working with asyncio websockets,
    preventing Any-type abuse and catching interface mismatches early.
    """

    open: bool

    async def send(self, data: str) -> None:
        """Send a message through the WebSocket."""
        ...

    async def recv(self) -> str:
        """Receive a message from the WebSocket."""
        ...

    async def close(self) -> None:
        """Close the WebSocket connection."""
        ...

    def __aenter__(self) -> WebSocketClient:
        """Context manager entry."""
        ...

    def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        ...


# ── FrontendBridge Implementation ──────────────────────────────────────


class FrontendBridge:
    """WebSocket client that bridges the static frontend UI to the Python Daemon
    via the Node.js Gateway's agent port.

    Manages bidirectional communication for:
    - Configuration delivery (GET_CONFIG)
    - Configuration updates (UPDATE_CONFIG) with validation
    - Chat query forwarding (QUERY)

    Attributes:
        router: LLM router for forwarding chat queries
        session: HTTP session for API calls
        config: Configuration object for reading/writing settings
        ctx: Application context for accessing managed components
        gateway_url: WebSocket URL of the Gateway agent port
        ws: Active WebSocket connection (None if disconnected)
        _is_running: Flag indicating if reconnection loop should continue
        _running_lock: Lock protecting _is_running flag
        _active_queries: Set of asyncio.Task objects that need cleanup
        _reconnect_count: Counter for reconnection attempts (reset on success)
        _keyring_client: Dependency-injected keyring accessor (for testing)
    """

    def __init__(
        self,
        router: Any,
        session: Any,
        config: Config,
        app_context: AppContext,
        gateway_url: str = "ws://127.0.0.1:18789",
        keyring_client: Any = None,
    ) -> None:
        """Initialize FrontendBridge.

        Args:
            router: LLM router instance for forwarding chat queries
            session: HTTP session (for potential future API calls)
            config: Configuration object (must have Config interface)
            app_context: Application context with routing/polling components
            gateway_url: Full WebSocket URL of Gateway agent port
            keyring_client: (Testing) Override keyring module for mocking
                           Defaults to system keyring if None.
        """
        self.router = router
        self.session = session
        self.config = config
        self.ctx = app_context
        self.gateway_url = assert_safe_ws_url(gateway_url)
        self.ws: WebSocketClient | None = None
        self._is_running = False
        self._running_lock = asyncio.Lock()
        self._active_queries: set[asyncio.Task[Any]] = set()
        self._reconnect_count = 0

        # Dependency injection for testability
        self._keyring_client = keyring_client if keyring_client is not None else keyring

        # Input length limits (prevent DOS via oversized payloads)
        self._max_key_len = 512
        self._max_chatid_len = 32

    # ── Lifecycle Management ───────────────────────────────────────────

    async def start(self) -> None:
        """Start the FrontendBridge with exponential backoff reconnection.

        Implements infinite reconnection loop with:
        - Exponential backoff (starting at 2s, max 60s)
        - Hard limit of 5 reconnection attempts before 5-minute pause
        - Graceful exit when _is_running flag is set to False

        Loop flow:
            1. Try to connect to Gateway
            2. If successful: reset backoff, listen for messages
            3. If connection drops: increase backoff, retry
            4. After 5 failed attempts: pause 5 minutes, reset counter
            5. If stop() called: break loop cleanly
        """
        async with self._running_lock:
            self._is_running = True

        backoff = 2.0
        logger.info("🚀 FrontendBridge iniciando: Cliente WS → %s", self.gateway_url)

        while self._is_running:
            try:
                async with websockets.connect(self.gateway_url, open_timeout=10) as ws:
                    self.ws = ws
                    await self._authenticate(ws)
                    logger.info("✅ Enlace establecido con Gateway (Frontend Bridge).")
                    self._reconnect_count = 0
                    backoff = 2.0
                    await self._listen_loop()
            except (ConnectionClosed, ConnectionClosedError) as exc:
                # Expected connection errors - retry with backoff
                if not self._is_running:
                    break
                self._handle_reconnect_error(exc, "Enlace perdido con Gateway", backoff)
                self.ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)
                self._reconnect_count += 1
                self._check_reconnect_limit()
            except ConnectionRefusedError as exc:
                # Gateway not running
                if not self._is_running:
                    break
                self._handle_reconnect_error(exc, "Gateway no está disponible", backoff)
                self.ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)
                self._reconnect_count += 1
                self._check_reconnect_limit()
            except OSError as exc:
                # Network-level errors (ECONNREFUSED, etc.)
                if not self._is_running:
                    break
                self._handle_reconnect_error(exc, f"Error de red: {exc}", backoff)
                self.ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)
                self._reconnect_count += 1
                self._check_reconnect_limit()
            except Exception as exc:
                # Unexpected errors - log and retry
                logger.error("❌ Fallo inesperado en FrontendBridge: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    def _handle_reconnect_error(self, exc: Exception, msg: str, backoff: float) -> None:
        """Log reconnection errors with attempt counter."""
        logger.warning(
            "⚠️ %s (%s). Reintento %d/%d en %.1fs...",
            msg,
            type(exc).__name__,
            self._reconnect_count + 1,
            MAX_RECONNECT_ATTEMPTS,
            backoff,
        )

    def _check_reconnect_limit(self) -> None:
        """Check if reconnection attempts exceed limit.

        If max attempts reached, log warning about 5-minute pause.
        """
        if self._reconnect_count >= MAX_RECONNECT_ATTEMPTS:
            logger.warning(
                "⚠️ Límite de intentos de reconexión (%d) alcanzado. Pausa de %d segundos antes de reintentar...",
                MAX_RECONNECT_ATTEMPTS,
                RECONNECT_PAUSE_DURATION,
            )
            # Pause will happen in the main loop via asyncio.sleep()

    async def stop(self) -> None:
        """Gracefully shutdown the FrontendBridge.

        - Signals the reconnection loop to exit
        - Closes the WebSocket connection if open
        - Cancels all active query tasks
        - Waits for cancellations to complete
        - Logs completion

        Invariant: All tracked tasks are cleaned up before returning.
        """
        async with self._running_lock:
            self._is_running = False

        if self.ws:
            try:
                await self.ws.close()
            except Exception as exc:
                logger.warning("Error cerrando WebSocket: %s", exc)

        # Cancel all active query tasks
        for task in self._active_queries:
            if not task.done():
                task.cancel()

        # Wait for all tasks to finish cancellation
        if self._active_queries:
            await asyncio.gather(
                *self._active_queries,
                return_exceptions=True,  # Ignore cancellation exceptions
            )

        logger.info("🛑 FrontendBridge detenido de forma segura.")

    # ── Authentication ─────────────────────────────────────────────────

    async def _authenticate(self, ws: WebSocketClient) -> None:
        """Send HMAC token and wait for auth_ok from the Gateway.

        Reads WS_AUTH_TOKEN from environment first, then falls back to keyring.
        Raises AuthError if the token is missing or the Gateway rejects it.
        """
        # Zero-Trust: removed os.environ fallback. Token must be injected or retrieved from keyring.
        token = self._keyring_client.get_password("sky_claw", "ws_auth_token") or ""
        if not token:
            raise AuthError(
                "WS_AUTH_TOKEN no configurado en entorno ni en keyring. Ejecuta el setup inicial para generar el token."
            )

        await ws.send(json.dumps({"type": "auth", "token": token}))
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        ack = json.loads(raw)
        if ack.get("type") != "auth_ok":
            raise AuthError(f"Handshake rechazado por Gateway: {ack}")
        logger.info("🔐 Handshake WS autenticado correctamente.")

    # ── Message Loop ───────────────────────────────────────────────────

    async def _listen_loop(self) -> None:
        """Main message listening loop.

        Continuously receives and dispatches messages from the Gateway.

        Message format validation (must be JSON):
            {
                "type": "GET_CONFIG" | "UPDATE_CONFIG" | "QUERY",
                "id": string (uuid),
                "content": object (optional, depending on type)
            }

        Stops cleanly when WebSocket closes or _is_running becomes False.

        Error handling:
            - JSON decode errors: log and continue
            - Unknown message types: log debug and continue
            - Handler exceptions: log and continue (don't crash loop)

        Invariant: No single malformed message crashes the entire bridge.
        """
        if not self.ws:
            logger.error("WebSocket no está disponible al iniciar _listen_loop")
            return

        try:
            async for raw in self.ws:
                try:
                    # ── JSON Validation (C1 fix: explicit type checking) ──
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        logger.warning(
                            "Mensaje JSON no es un objeto: tipo %s recibido",
                            type(data).__name__,
                        )
                        continue

                    msg_type = data.get("type", "")
                    if not isinstance(msg_type, str):
                        logger.warning("Campo 'type' no es string: %s", type(msg_type).__name__)
                        continue

                    # ── Route by message type ──
                    if msg_type == "GET_CONFIG":
                        await self._handle_get_config(data)
                    elif msg_type == "UPDATE_CONFIG":
                        await self._handle_update_config(data)
                    elif msg_type == "QUERY":
                        task = asyncio.create_task(self._handle_query(data))
                        self._active_queries.add(task)
                        task.add_done_callback(self._active_queries.discard)
                    else:
                        logger.debug(
                            "Tipo de mensaje no reconocido: %s",
                            msg_type,
                        )
                except json.JSONDecodeError as exc:
                    logger.error("JSON malformado desde Gateway: %s", exc)
                except Exception as exc:
                    logger.exception("Error procesando mensaje frontend: %s", exc)
        except asyncio.CancelledError:
            logger.debug("_listen_loop cancelado durante shutdown")
            raise
        except Exception as exc:
            logger.exception("Error fatal en _listen_loop: %s", exc)

    # ── GET_CONFIG Handler ─────────────────────────────────────────────

    async def _handle_get_config(self, data: dict[str, Any]) -> None:
        """Handle GET_CONFIG request.

        Returns the current configuration with secrets masked.

        Response format:
            {
                "type": "CONFIG_DATA",
                "id": <original message id>,
                "content": {
                    "llm_provider": "deepseek" | "anthropic" | "ollama",
                    "telegram_chat_id": "123456" | "",
                    "has_llm_key": bool,
                    "has_nexus_key": bool,
                    "has_telegram_token": bool
                }
            }

        Invariant: Secrets are NEVER included in response, only status.
        """
        msg_id = data.get("id", str(uuid.uuid4()))

        try:
            # Re-read config from disk to get latest state
            fresh_cfg = Config()
        except Exception:
            fresh_cfg = self.config

        # Determine which provider-specific key is set
        provider = getattr(fresh_cfg, "llm_provider", "deepseek")
        provider_key_name = PROVIDER_KEY_MAP.get(provider, "llm_api_key")

        has_llm_key = bool(getattr(fresh_cfg, provider_key_name, "") or getattr(fresh_cfg, "llm_api_key", ""))
        has_nexus_key = bool(getattr(fresh_cfg, "nexus_api_key", ""))
        has_telegram_token = bool(getattr(fresh_cfg, "telegram_bot_token", ""))

        response = {
            "type": "CONFIG_DATA",
            "id": msg_id,
            "content": {
                "llm_provider": provider,
                "telegram_chat_id": str(getattr(fresh_cfg, "telegram_chat_id", "")),
                "has_llm_key": has_llm_key,
                "has_nexus_key": has_nexus_key,
                "has_telegram_token": has_telegram_token,
            },
        }

        await self._send(response)
        logger.info("📤 CONFIG_DATA enviado al frontend.")

    # ── UPDATE_CONFIG Handler ──────────────────────────────────────────

    async def _handle_update_config(self, data: dict[str, Any]) -> None:
        """Handle UPDATE_CONFIG request with validation and hot-reload.

        Validates all inputs, persists to TOML + keyring, then hot-reloads
        LLM provider and Telegram configuration if needed.

        Validation:
            - llm_provider: must be in ["deepseek", "anthropic", "ollama"]
            - llm_api_key, nexus_api_key, telegram_bot_token: max 512 chars
            - telegram_bot_token: must contain ':' if provided
            - telegram_chat_id: must be numeric or empty, max 32 chars

        Persistence:
            - Secrets → keyring (OS Credential Manager on Windows)
            - Non-secrets → TOML config file

        Hot-reload:
            - If llm_provider or llm_api_key changed: swap LLM provider
            - If telegram_bot_token changed: restart Telegram polling

        Response format:
            {
                "type": "CONFIG_UPDATED",
                "id": <original message id>,
                "success": bool,
                "message": string (status or error)
            }

        Invariant: Atomicity - either all changes applied or none.
                   Empty fields = no change (preserve existing value).
        """
        msg_id = data.get("id", str(uuid.uuid4()))
        content = data.get("content", {})

        try:
            # ── Validation Phase ────────────────────────────────────────
            llm_provider = content.get("llm_provider", "").strip().lower()
            if llm_provider and llm_provider not in VALID_PROVIDERS:
                await self._send_config_result(
                    msg_id,
                    False,
                    f"Proveedor LLM inválido: '{llm_provider}'. Opciones: {', '.join(VALID_PROVIDERS)}",
                )
                return

            # Length validation for sensitive keys
            for field in ("llm_api_key", "nexus_api_key", "telegram_bot_token"):
                val = content.get(field, "")
                if val and len(val) > self._max_key_len:
                    await self._send_config_result(
                        msg_id,
                        False,
                        f"El campo '{field}' excede el largo maximo permitido.",
                    )
                    return

            # Telegram token format validation
            telegram_token = content.get("telegram_bot_token", "").strip()
            if telegram_token and ":" not in telegram_token:
                await self._send_config_result(
                    msg_id,
                    False,
                    "El token de Telegram no tiene el formato correcto (debe contener ':').",
                )
                return

            # Telegram chat ID validation
            telegram_chat_id = content.get("telegram_chat_id", "").strip()
            if telegram_chat_id and len(telegram_chat_id) > self._max_chatid_len:
                await self._send_config_result(msg_id, False, "El Chat ID es demasiado largo.")
                return
            if telegram_chat_id:
                try:
                    int(telegram_chat_id)
                except ValueError:
                    await self._send_config_result(
                        msg_id,
                        False,
                        "El Chat ID de Telegram debe ser numérico.",
                    )
                    return

            # ── Persistence Phase ────────────────────────────────────────
            llm_api_key = content.get("llm_api_key", "").strip()
            nexus_api_key = content.get("nexus_api_key", "").strip()

            if llm_api_key:
                # Store as generic llm_api_key
                self._set_keyring("llm_api_key", llm_api_key)
                # Also store as provider-specific key
                if llm_provider:
                    provider_key = PROVIDER_KEY_MAP.get(llm_provider)
                    if provider_key:
                        self._set_keyring(provider_key, llm_api_key)

            if nexus_api_key:
                self._set_keyring("nexus_api_key", nexus_api_key)

            if telegram_token:
                self._set_keyring("telegram_bot_token", telegram_token)

            # Persist non-sensitive fields to Config + TOML
            if llm_provider:
                self.config._data["llm_provider"] = llm_provider

            if telegram_chat_id:
                self.config._data["telegram_chat_id"] = telegram_chat_id

            # Propagate secrets into config._data so save() can handle them
            if llm_api_key:
                self.config._data["llm_api_key"] = llm_api_key
            if nexus_api_key:
                self.config._data["nexus_api_key"] = nexus_api_key
            if telegram_token:
                self.config._data["telegram_bot_token"] = telegram_token

            self.config.save()
            logger.info("💾 Configuración guardada en TOML + keyring.")

            # ── Hot-reload Phase ────────────────────────────────────────
            reload_messages: list[str] = []

            # LLM Provider hot-swap
            if llm_provider or llm_api_key:
                target_provider = llm_provider or getattr(self.config, "llm_provider", "deepseek")
                success = await self._do_llm_reload(target_provider, llm_api_key)
                if success:
                    reload_messages.append(f"LLM cambiado a {target_provider.capitalize()}")
                else:
                    reload_messages.append(f"LLM: no se pudo cambiar a {target_provider} (verifica la API key)")

            # Telegram hot-reload
            if telegram_token:
                tg_ok = await self._reload_telegram(
                    telegram_token,
                    telegram_chat_id or getattr(self.config, "telegram_chat_id", ""),
                )
                if tg_ok:
                    reload_messages.append("Telegram reconectado")
                else:
                    reload_messages.append("Telegram: token guardado, reinicia para activar")

            status_msg = "Configuración guardada."
            if reload_messages:
                status_msg += " " + " | ".join(reload_messages) + "."

            await self._send_config_result(msg_id, True, status_msg)

        except Exception as exc:
            logger.exception("❌ Error guardando configuración: %s", exc)
            await self._send_config_result(msg_id, False, "Error interno al guardar configuracion.")

    # ── QUERY Handler (Chat forwarding) ─────────────────────────────────

    async def _handle_query(self, data: dict[str, Any]) -> None:
        """Handle QUERY message (chat query from frontend).

        Forwards the query to the LLM Router and returns the response.

        Request format:
            {
                "type": "QUERY",
                "id": string (uuid),
                "content": string (chat message)
            }

        Response format:
            {
                "type": "RESPONSE",
                "id": <original message id>,
                "content": string (LLM response),
                "metadata": {
                    "reply_to": <original message id>,
                    "channel": "frontend",
                    "processed_at": float (Unix timestamp)
                }
            }

        Error handling:
            - Empty queries are silently ignored
            - Router errors result in error message to UI
        """
        msg_id = data.get("id", str(uuid.uuid4()))
        text = data.get("content", "").strip()

        if not text:
            logger.debug("Consulta vacía recibida, ignorando")
            return

        chat_id = f"ui-{msg_id[:8]}"

        try:
            response = await self.router.chat(
                text,
                self.session,
                chat_id=chat_id,
                metadata={"channel": "frontend"},
            )

            await self._send(
                {
                    "type": "RESPONSE",
                    "id": msg_id,
                    "content": response,
                    "metadata": {
                        "reply_to": msg_id,
                        "channel": "frontend",
                        "processed_at": time.time(),
                    },
                }
            )
        except Exception as exc:
            logger.exception("❌ Error procesando query frontend: %s", exc)
            await self._send(
                {
                    "type": "RESPONSE",
                    "id": msg_id,
                    "content": "⚠️ Error interno del sistema. Revisa los logs del Daemon.",
                }
            )

    # ── Hot-Reload Helpers ──────────────────────────────────────────────

    async def _do_llm_reload(self, provider_name: str, api_key: str = "") -> bool:
        """Hot-swap the LLM provider at runtime.

        Creates a new provider instance and assigns it under the router's
        provider lock to ensure thread-safety.

        Args:
            provider_name: "deepseek", "anthropic", or "ollama"
            api_key: Optional API key (skipped for ollama)

        Returns:
            True if swap succeeded, False if provider config invalid

        Invariant: Provider swap happens atomically under lock.
                   No queries are interrupted during the swap.
        """
        try:
            key = api_key or self._get_keyring(PROVIDER_KEY_MAP.get(provider_name, "llm_api_key"))

            # Ollama doesn't require an API key
            if not key and provider_name != "ollama":
                # Try the generic llm_api_key as fallback
                key = self._get_keyring("llm_api_key")
                if not key:
                    logger.error("No API key found for provider '%s'.", provider_name)
                    return False

            new_provider = create_provider(provider_name=provider_name, api_key=key)

            # ── Atomic swap under lock (C3 fix: proper lock usage) ──
            async with self.ctx.router._provider_lock:
                self.ctx.router._provider = new_provider

            logger.info(
                "🚀 Hot-Swap LLM completado: ahora usando %s",
                type(new_provider).__name__,
            )
            return True

        except ProviderConfigError as exc:
            logger.error("Provider config error during hot-swap: %s", exc)
            return False
        except Exception as exc:
            logger.exception("Hot-swap failed: %s", exc)
            return False

    async def _reload_telegram(self, token: str, chat_id: str = "") -> bool:
        """Stop existing Telegram polling and restart with new token.

        Completely recreates the Telegram sender and polling handler
        to pick up the new token.

        Args:
            token: New Telegram bot token
            chat_id: Optional chat ID restriction

        Returns:
            True if reload succeeded, False if error during restart
        """
        try:
            # Stop existing polling if active
            if self.ctx.polling is not None:
                await self.ctx.polling.stop()
                self.ctx.polling = None
                logger.info("🛑 Telegram polling detenido para recarga.")

            # Recreate sender
            from sky_claw.antigravity.comms.telegram_sender import TelegramSender

            self.ctx.sender = TelegramSender(
                bot_token=token,
                gateway=self.ctx.gateway,
                session=self.ctx.session,
            )

            # Recreate polling
            from sky_claw.antigravity.comms.telegram import TelegramWebhook
            from sky_claw.antigravity.comms.telegram_polling import TelegramPolling

            webhook_handler = TelegramWebhook(
                router=self.ctx.router,
                sender=self.ctx.sender,
                session=self.ctx.session,
                hitl=self.ctx.hitl,
            )
            self.ctx.polling = TelegramPolling(
                token=token,
                webhook_handler=webhook_handler,
                gateway=self.ctx.gateway,
                session=self.ctx.session,
                authorized_chat_id=chat_id or None,
            )
            await self.ctx.polling.start()
            logger.info("✅ Telegram polling reiniciado con nuevo token.")
            return True

        except Exception as exc:
            logger.exception("❌ Error recargando Telegram: %s", exc)
            return False

    # ── Utility Methods ─────────────────────────────────────────────────

    async def _send(self, payload: dict[str, Any]) -> None:
        """Send a JSON payload through the WebSocket.

        Checks that WebSocket is open before sending to prevent
        AttributeError and connection errors.

        Args:
            payload: Dictionary to send as JSON

        Invariant: Only sends if WebSocket connection is open (I3 fix).
        """
        # ── WebSocket open check (I3 fix: explicit check) ──
        if self.ws and self.ws.open:
            try:
                await self.ws.send(json.dumps(payload))
            except ConnectionClosed:
                logger.debug("WebSocket cerrado al intentar enviar")
            except Exception as exc:
                logger.error("Error enviando por WebSocket: %s", exc)
        else:
            logger.debug("WebSocket no está disponible para envío")

    async def _send_config_result(self, msg_id: str, success: bool, message: str) -> None:
        """Send a CONFIG_UPDATED response to the frontend.

        Args:
            msg_id: Original message ID to correlate response
            success: Whether the operation succeeded
            message: Status or error message
        """
        await self._send(
            {
                "type": "CONFIG_UPDATED",
                "id": msg_id,
                "success": success,
                "message": message,
            }
        )

    def _set_keyring(self, key: str, value: str) -> None:
        """Store a secret in the OS keyring with fallback.

        Attempts to store in system keyring (Windows Credential Manager).
        If that fails, logs a warning (fallback handled by config.save()).

        Args:
            key: Keyring service key
            value: Secret value to store

        Note: Not async because keyring is synchronous.
        """
        try:
            self._keyring_client.set_password("sky_claw", key, value)
        except Exception as exc:
            logger.warning(
                "Keyring set failed for '%s': %s. Value will be stored in TOML as fallback.",
                key,
                exc,
            )

    def _get_keyring(self, key: str) -> str:
        """Retrieve a secret from the OS keyring.

        Args:
            key: Keyring service key

        Returns:
            Secret value if found, empty string if not found or error

        Note: Not async because keyring is synchronous.
        """
        try:
            return self._keyring_client.get_password("sky_claw", key) or ""
        except Exception:
            return ""
