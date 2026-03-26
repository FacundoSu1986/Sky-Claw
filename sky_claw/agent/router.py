"""LLM Router – conversation loop with pluggable LLM providers.

Maintains chat history in SQLite, calls the configured LLM provider
(Anthropic, DeepSeek, or Ollama), and executes tools through the
:class:`AsyncToolRegistry` until the model signals ``end_turn``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
import aiosqlite

from sky_claw.agent.providers import LLMProvider
from sky_claw.agent.tools import AsyncToolRegistry
from sky_claw.security.sanitize import sanitize_for_prompt

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
    ) -> None:
        if provider is None:
            # Legacy path: build an AnthropicProvider from the raw key.
            from sky_claw.agent.providers import AnthropicProvider
            provider = AnthropicProvider(api_key)

        self._provider = provider
        self._tools = tool_registry
        self._db_path = db_path
        self._model = model
        self._system_prompt = system_prompt
        self._max_context = max_context
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the history database and ensure schema exists."""
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_HISTORY_SCHEMA)

    async def close(self) -> None:
        """Close the history database."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    async def chat(
        self,
        user_message: str,
        session: aiohttp.ClientSession,
        chat_id: str | None = None,
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

        user_message = sanitize_for_prompt(user_message)
        await self._save_message(chat_id, "user", user_message)
        messages = await self._load_context(chat_id)

        tool_schemas = self._tools.tool_schemas() if self._tools else []
        consecutive_errors = 0

        for _round in range(MAX_TOOL_ROUNDS):
            response_data = await self._provider.chat(
                messages=messages,
                tools=tool_schemas,
                session=session,
                system_prompt=self._system_prompt,
                model=self._model,
            )

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
                except (KeyError, ValueError, TypeError) as exc:
                    consecutive_errors += 1
                    backoff = min(2 ** consecutive_errors, 16)
                    logger.warning("Tool execution error (attempt %d). Backing off %ds...", consecutive_errors, backoff)
                    await asyncio.sleep(backoff)
                    
                    feedback = {
                        "error": "Tool execution failed due to invalid arguments or formatting.",
                        "exception_type": type(exc).__name__,
                        "details": str(exc),
                        "instruction": "Please carefully review the required tool schema and provide strictly matching parameters."
                    }
                    result_str = json.dumps(feedback)

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
        else:
            raise RuntimeError(
                f"Agent exceeded {MAX_TOOL_ROUNDS} tool rounds"
            )

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------

    async def _save_message(
        self, chat_id: str, role: str, content: str
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Router database is not open")
        await self._conn.execute(
            "INSERT INTO chat_history (chat_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, role, content, time.time()),
        )
        await self._conn.commit()

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
