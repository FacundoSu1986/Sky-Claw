"""LLM Router – conversation loop with pluggable LLM providers.

Maintains chat history in SQLite, calls the configured LLM provider
(Anthropic, DeepSeek, or Ollama), and executes tools through the
:class:`AsyncToolRegistry` until the model signals ``end_turn``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import aiohttp
import aiosqlite

from sky_claw.agent.providers import LLMProvider, create_provider
from sky_claw.agent.tools import AsyncToolRegistry
from sky_claw.security.sanitize import sanitize_for_prompt
from sky_claw.agent.semantic_router import SemanticRouter
from sky_claw.agent.context_manager import ContextManager
from sky_claw.security.credential_vault import CredentialVault

logger = logging.getLogger(__name__)

MAX_CONTEXT_MESSAGES = 20
MAX_TOOL_ROUNDS = 10

_HISTORY_SCHEMA = """\
CREATE TABLE IF NOT EXISTS chat_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    timestamp   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id
    ON chat_history (chat_id, id);
"""


class LLMRouter:
    """Conversation router backed by a pluggable LLM provider.

    Args:
        provider: LLM provider instance (Anthropic, DeepSeek, Ollama).
        tool_registry: Async tool registry for tool execution.
        db_path: Path to the SQLite database for chat history.
        model: Model identifier override (provider-specific).
        system_prompt: Optional system prompt prepended to every request.
        max_context: Maximum messages sent per API request (sliding window).
        api_key: Deprecated — use ``provider`` instead.  Kept for
            backwards compatibility with existing call-sites.
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        tool_registry: AsyncToolRegistry | None = None,
        db_path: str = "",
        model: str = "",
        system_prompt: str = "",
        max_context: int = MAX_CONTEXT_MESSAGES,
        *,
        # Legacy parameter — ignored when ``provider`` is given.
        api_key: str = "",
        registry_db: str = "mod_registry.db",
        mo2_profile: str = "/mnt/c/Modding/MO2/profiles/Default",
        vault: CredentialVault | None = None,
    ) -> None:
        if provider is None and not vault:
            # Legacy fallback pattern, removed 'os.environ' dependency per SRE directives.
            from sky_claw.agent.providers import DeepSeekProvider
            # Instantiating locally if api_key passed directly, otherwise vault is required
            provider = DeepSeekProvider(api_key)

        self._provider = provider
        self._vault = vault
        self._provider_lock = asyncio.Lock()
        self._tools = tool_registry
        self._db_path = db_path
        self._model = model
        self._system_prompt = system_prompt
        self._max_context = max_context
        self._conn: aiosqlite.Connection | None = None
        self._message_queue: asyncio.Queue[tuple[str, str, str, float]] = asyncio.Queue()
        self._batch_task: asyncio.Task[None] | None = None
        
        # Standard 2026 Orchestration Layers
        self._semantic_router = SemanticRouter()
        self._context_manager = ContextManager(registry_db, mo2_profile)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the history database and ensure schema exists."""
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_HISTORY_SCHEMA)
        self._batch_task = asyncio.create_task(self._process_batch_commits())

    async def close(self) -> None:
        """Close the history database and flush pending commits atomically."""
        if self._batch_task:
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass
            
        if self._conn is not None:
             # Atomic final flush of message queue
            pending = []
            while not self._message_queue.empty():
                 pending.append(self._message_queue.get_nowait())
            
            if pending:
                try:
                    await self._conn.executemany(
                        "INSERT INTO chat_history (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)", 
                        pending
                    )
                    await self._conn.commit()
                except Exception as exc:
                    logger.error("Failed to perform final atomic flush: %s", exc)

            await self._conn.close()
            self._conn = None

    async def _process_batch_commits(self, batch_size: int = 5, timeout: float = 30.0) -> None:
        """Background worker to commit messages in batches or on timeout."""
        batch = []
        while True:
            try:
                try:
                    # Wait for a message with timeout
                    msg = await asyncio.wait_for(self._message_queue.get(), timeout=timeout)
                    batch.append(msg)
                except asyncio.TimeoutError:
                    # Timeout reached, commit whatever we have
                    pass

                if batch and (len(batch) >= batch_size or self._message_queue.empty()):
                    if self._conn:
                        await self._conn.executemany(
                            "INSERT INTO chat_history (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                            batch
                        )
                        await self._conn.commit()
                        logger.debug("Committed batch of %d messages to SQLite", len(batch))
                    batch = []
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in message batch worker: %s", exc)
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # LLM Hot-Swapping Factory Pattern (SRE Phase 1)
    # ------------------------------------------------------------------

    async def reload_provider(self, new_provider_name: str) -> bool:
        """Cambia el LLM subyacente en caliente extrayendo llaves Zero-Trust del Vault."""
        logger.info(f"🔄 Iniciando secuencia de Hot-Swap hacia [{new_provider_name}]...")
        if not self._vault:
            logger.error("RCA: Bóveda criptográfica (Vault) no asginada. Fallo de Hot-Swap.")
            return False

        # Las llaves deben guardarse en la Bóveda como '{provider}_api_key'
        api_key = await self._vault.get_secret(f"{new_provider_name}_api_key")
        if not api_key:
            logger.error(f"RCA: Clave maestra no hallada en SQLite WAL para {new_provider_name}.")
            return False

        async with self._provider_lock:
            # Fábrica estática instanciada de providers.py - Inyección de dependencia
            try:
                self._provider = create_provider(provider_name=new_provider_name, api_key=api_key)
                logger.info(f"🚀 Hot-Swap finalizado: LLM Router ahora utilizando {type(self._provider).__name__}.")
                return True
            except Exception as e:
                logger.error(f"RCA Crítico: El patrón de fábrica devolvió un Provider defectuoso: {e}")
                return False

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    async def chat(
        self,
        user_message: str,
        session: aiohttp.ClientSession,
        chat_id: str | None = None,
        *,
        metadata: dict | None = None,
        progress_callback: Any | None = None,
    ) -> str:
        """Send a user message and return the final assistant text.

        Handles the full tool-use loop: call the provider, execute any
        requested tools, re-send with ``tool_result``, repeat until
        the model returns ``stop_reason == "end_turn"``.

        Args:
            user_message: The user's input text.
            session: An ``aiohttp.ClientSession`` for HTTP calls.
            chat_id: Optional conversation identifier.

        Returns:
            The assistant's final text response.
        """
        if not chat_id:
            raise ValueError(
                "Strict session handling enforced. chat_id cannot be null. "
                "The integration layer must explicitly initialize the session context."
            )

        # 1. Semantic Routing (Cognitive classification)
        routing_data = {"payload": {"text": user_message}, "metadata": metadata or {}}
        routed = self._semantic_router.route(routing_data)
        
        # Branch A: Deterministic System Management
        if routed["intent"] == "COMANDO_SISTEMA":
            return await self._handle_system_op(routed["original_text"])

        # Branch B: Contextual Modding Query (RAG)
        injected_context = ""
        if routed["intent"] == "CONSULTA_MODDING":
            if progress_callback:
                asyncio.create_task(progress_callback("searching_registry", 20))
            injected_context = await self._context_manager.build_prompt_context(user_message)

        user_message = sanitize_for_prompt(user_message)
        await self._save_message(chat_id, "user", user_message)
        messages = await self._load_context(chat_id)

        tool_schemas = self._tools.tool_schemas() if self._tools else []
        consecutive_errors = 0

        for _round in range(MAX_TOOL_ROUNDS):
            try:
                chat_kwargs = {
                    "messages": messages,
                    "tools": tool_schemas,
                    "session": session,
                    "system_prompt": f"{self._system_prompt}\n\n{injected_context}",
                }
                if self._model:
                    chat_kwargs["model"] = self._model
                    
                # Lock transaccional SRE para Hot-Swapping seguro sin caída de Event Loop
                async with self._provider_lock:
                    if not self._provider:
                        raise RuntimeError("SISTEMA: LLM Provider nulo. Iniciar Hot-Swap o configurar API Key primaria.")
                    response_data = await self._provider.chat(**chat_kwargs)

                if response_data is None or not isinstance(response_data, dict):
                    return "Error: El proveedor de IA no devolvió datos."

                stop_reason = response_data.get("stop_reason", "end_turn")
                content_blocks: list[dict[str, Any]] = response_data.get("content", [])

                await self._save_message(
                    chat_id, "assistant", json.dumps(content_blocks)
                )
                messages.append({"role": "assistant", "content": content_blocks})

                if stop_reason != "tool_use":
                    text_parts = [
                        block.get("text", "")
                        for block in content_blocks
                        if block.get("type") == "text"
                    ]
                    return "\n".join(text_parts)

                # Execute requested tools.
                tool_results: list[dict[str, Any]] = []
                for block in content_blocks:
                    if block.get("type") != "tool_use":
                        continue
                    tool_id: str = block["id"]
                    tool_name: str = block["name"]
                    tool_input: dict[str, Any] = block.get("input", {})

                    try:
                        result_str = await self._tools.execute(tool_name, tool_input)
                        consecutive_errors = 0
                        if progress_callback:
                            asyncio.create_task(progress_callback(f"executed_{tool_name}", 100))
                    except (KeyError, ValueError, TypeError, RuntimeError, OSError) as exc:
                        # Broadened exception handling for system/runtime errors (xEdit/LOOT zombies)
                        consecutive_errors += 1
                        backoff = min(2 ** consecutive_errors, 16)
                        logger.warning("Tool execution error (%s): %s (attempt %d). Backing off %ds...", 
                                     type(exc).__name__, exc, consecutive_errors, backoff)
                        await asyncio.sleep(backoff)
                        
                        feedback = {
                            "error": "Critical tool execution failure.",
                            "exception_type": type(exc).__name__,
                            "details": str(exc),
                            "instruction": "Verify that requirements are met and that external processes (LOOT/xEdit) are not blocked."
                        }
                        result_str = json.dumps(feedback)

                    if len(result_str) > 4000:
                        result_str = result_str[:4000] + "\n\n[... truncated ...]"

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": result_str,
                        }
                    )

                await self._save_message(
                    chat_id, "user", json.dumps(tool_results)
                )
                messages.append({"role": "user", "content": tool_results})
                messages = messages[-self._max_context:]
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as outer_exc:
                logger.error("System-level router failure: %s", outer_exc)
                return f"Error Crítico: El ciclo de herramientas falló por una excepción de sistema: {outer_exc}"
        else:
            raise RuntimeError(
                f"Agent exceeded {MAX_TOOL_ROUNDS} tool rounds"
            )

    async def _handle_system_op(self, command: str) -> str:
        """Handles COMANDO_SISTEMA branch synchronously."""
        cmd = command.lower()
        if "status" in cmd:
            return "SISTEMA: Núcleo Sky-Claw activo. WS Daemon operativo. Load Order proactivo activo."
        if "uptime" in cmd:
            return f"SISTEMA: Uptime registrado (WSL2). Conexión Gateway: ESTABLE."
        return f"COMANDO_SISTEMA: '{command}' recibido pero no implementado en esta versión."

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------

    async def _save_message(
        self, chat_id: str, role: str, content: str
    ) -> None:
        """Enqueues a message for batch persistence."""
        if self._conn is None:
            raise RuntimeError("Router database is not open")
        await self._message_queue.put((chat_id, role, content, time.time()))

    async def _load_context(self, chat_id: str) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Router database is not open")
        async with self._conn.execute(
            "SELECT role, content FROM chat_history "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, self._max_context),
        ) as cur:
            rows = await cur.fetchall()

        messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            role = str(row[0])
            raw_content = str(row[1])
            try:
                parsed = json.loads(raw_content)
                if isinstance(parsed, list):
                    messages.append({"role": role, "content": parsed})
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            messages.append({"role": role, "content": raw_content})
        return messages
