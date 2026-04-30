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
from typing import TYPE_CHECKING, Any

import aiosqlite
import pydantic

from sky_claw.agent.context_manager import ContextManager
from sky_claw.agent.hermes_parser import extract_tool_calls, has_tool_calls
from sky_claw.agent.lcel_chains import (
    ChainBuilder,
    PromptComposer,
    ToolExecutor,
)
from sky_claw.agent.providers import LLMProvider, create_provider
from sky_claw.agent.semantic_router import SemanticRouter
from sky_claw.agent.token_budget import TokenBudgetManager
from sky_claw.agent.token_circuit_breaker import TokenCircuitBreaker
from sky_claw.core.errors import AgentOrchestrationError, SecurityViolationError
from sky_claw.core.schemas import RouteClassification
from sky_claw.security.sanitize import sanitize_for_prompt

if TYPE_CHECKING:
    import aiohttp

    from sky_claw.agent.tools_facade import AsyncToolRegistry
    from sky_claw.security.agent_guardrail import AgentGuardrail
    from sky_claw.security.credential_vault import CredentialVault

logger = logging.getLogger(__name__)

# FASE 1.5.3: Legacy constants kept for backward compatibility with tests.
# Actual budget management is delegated to TokenBudgetManager.
MAX_CONTEXT_MESSAGES = 20
MAX_TOOL_ROUNDS = 10
MAX_HERMES_RETRIES = 3

# FASE 1.5.3: Default tool round timeout
DEFAULT_TOOL_ROUND_TIMEOUT = 120.0

_HERMES_TOOL_INSTRUCTIONS = (
    "\n\nYou have access to the following tools. To call a tool, respond with a "
    '<tool_call> block containing a JSON object with "name" and "arguments" keys. '
    "Wait for the tool result before continuing. If no tool is needed, reply normally.\n\n"
)


def _format_validation_feedback(tool_name: str, exc: pydantic.ValidationError) -> dict[str, Any]:
    """Convert a Pydantic ValidationError into a structured feedback dict for the LLM.

    TASK-012: When the model emits a ``tool_use`` block with arguments that
    fail strict-mode validation, we surface the per-field errors so the
    model can self-correct on the next turn instead of giving up.
    """
    return {
        "error": "Invalid arguments for tool — please retry with corrected types/values.",
        "tool": tool_name,
        "validation_errors": [
            {
                "field": ".".join(str(p) for p in err.get("loc", ())) or "<root>",
                "issue": err.get("msg", ""),
                "input": err.get("input"),
            }
            for err in exc.errors()
        ],
        "instruction": (
            "Re-emit the tool_use block respecting the input_schema field types, "
            "constraints (min/max length, regex pattern, gt/lt) and required fields."
        ),
    }


# BUG-002 FIX: Función de validación de API keys
def _is_valid_api_key(key: str | None) -> bool:
    """Valida que una API key sea válida y no un placeholder.

    BUG-002 FIX: Previene el uso de API keys placeholder o inválidas.

    Args:
        key: API key a validar

    Returns:
        True si la key parece válida, False si es placeholder o inválida
    """
    if not key or not isinstance(key, str):
        return False
    stripped = key.strip()
    if not stripped or len(stripped) < 8:
        return False
    # Placeholders comunes que deben ser rechazados
    placeholders = {
        "your_api_key_here",
        "insert_your_key",
        "xxx",
        "sk-xxx",
        "sk-...",
        "your-api-key",
        "change_me",
        "api_key_here",
        "paste_your_key",
        "sk-test",
        "sk-placeholder",
        "sk-your-key-here",
    }
    if len(set(stripped)) <= 2:
        return False
    return stripped.lower() not in placeholders


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
        mo2_profile: str = "",
        vault: CredentialVault | None = None,
        gateway: Any | None = None,
        guardrail: AgentGuardrail | None = None,
        hermes_mode: bool = False,
    ) -> None:
        if provider is None and not vault:
            # BUG-002 FIX: Validar API key antes de instanciar provider
            if not _is_valid_api_key(api_key):
                raise ValueError(
                    "Se requiere provider, vault, o api_key válido para inicializar LLMRouter. "
                    "Complete la configuración inicial. "
                    "La API key proporcionada está vacía, es muy corta, o es un placeholder."
                )
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

        # Standard 2026 Orchestration Layers
        self._semantic_router = SemanticRouter()
        self._context_manager = ContextManager(registry_db, mo2_profile)
        self._gateway = gateway
        self._guardrail = guardrail
        self._hermes_mode = hermes_mode
        if hermes_mode and tool_registry is None:
            raise ValueError("hermes_mode=True requires a tool_registry")

        # LangChain LCEL Integration
        self._lcel_prompt_composer = PromptComposer(
            system_prompt=system_prompt or "Eres un asistente de modding de Skyrim SE/AE.",
            tool_registry=tool_registry,
        )
        # Tool executor por defecto para cadenas LCEL
        self._lcel_tool_executor = ToolExecutor(
            tool_name="lcel_default",
            tool_description="Ejecutor de herramientas LCEL por defecto",
        )
        self._lcel_chain_builder = ChainBuilder(tool_executor=self._lcel_tool_executor)

        # FASE 1.5.3: Token budget management and circuit breaker
        self._token_budget = TokenBudgetManager()
        self._circuit_breaker = TokenCircuitBreaker()

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

        # 1. Semantic Routing con RouteClassification (LCEL Integration)
        routing_data = {"payload": {"text": user_message}, "metadata": metadata or {}}
        routed = self._semantic_router.route(routing_data)

        # Convertir a RouteClassification schema para validación
        route_classification = RouteClassification(
            intent=routed.get("intent", "CHAT_GENERAL"),
            confidence=routed.get("confidence", 0.7),
            target_agent=routed.get("target_agent"),
            tool_name=routed.get("tool_name"),
            parameters=routed.get("parameters", {}),
            requires_context=routed.get("intent") in ["CONSULTA_MODDING", "RAG_CONSULTA"],
            metadata=metadata or {},
        )

        logger.info(
            f"🎯 RouteClassification: intent={route_classification.intent}, confidence={route_classification.confidence}"
        )

        # Branch: Contextual Modding Query (RAG) usando LCEL
        injected_context = ""
        if route_classification.intent == "CONSULTA_MODDING":
            if progress_callback:
                asyncio.create_task(progress_callback("searching_registry", 20))
            injected_context = await self._context_manager.build_prompt_context(user_message)
            # TASK-013 P0: sanitize RAG context to prevent prompt-injection via
            # poisoned mod metadata scraped from Nexus Mods.
            injected_context = sanitize_for_prompt(injected_context)

            # Usar LCEL para componer prompt RAG
            if route_classification.requires_context:
                rag_prompt = self._lcel_prompt_composer.compose_rag_prompt(
                    query=user_message,
                    context=injected_context,
                    sources=["mod_registry", "conflict_db"],
                )
                logger.debug(f"📝 LCEL RAG prompt compuesto: {len(rag_prompt)} mensajes")

        # Input gate — guardrail (Titan v7.0) or legacy sanitize_for_prompt
        if self._guardrail:
            try:
                user_message = await self._guardrail.before_model_callback(user_message)
            except SecurityViolationError as exc:
                logger.warning("Guardrail blocked input: %s", exc)
                return (
                    "\u26a0\ufe0f Tu mensaje fue bloqueado por una pol\u00edtica de seguridad. Por favor reformulalo."
                )
            except AgentOrchestrationError as exc:
                logger.error("Orchestration error on input: %s", exc)
                return "\u26a0\ufe0f Error de orquestaci\u00f3n en la entrada."
        else:
            user_message = sanitize_for_prompt(user_message)

        await self._save_message(chat_id, "user", user_message)
        messages = await self._load_context(chat_id)

        tool_schemas = self._tools.tool_schemas() if self._tools else []
        consecutive_errors = 0
        # TASK-012: separate counters so a stream of malformed XML from the
        # model does not exhaust the budget reserved for legitimate execution
        # retries (and vice versa).
        hermes_parse_error_count = 0
        hermes_exec_error_count = 0

        for _round in range(MAX_TOOL_ROUNDS):
            try:
                # ── FASE 1.5.3: Pre-call budget check ──────────────────────
                # Handles context growth from accumulated tool results.
                budget_verdict = self._token_budget.check_budget(messages)
                if budget_verdict.action == "reject":
                    logger.warning(
                        "TokenBudget: context rejected at %.1f%% utilization (%d/%d tokens)",
                        budget_verdict.utilization_pct,
                        budget_verdict.current_tokens,
                        budget_verdict.max_tokens,
                    )
                    return (
                        "\u26a0\ufe0f El contexto de la conversaci\u00f3n excede el presupuesto de tokens. "
                        "Inicia una nueva conversaci\u00f3n."
                    )
                if budget_verdict.action == "truncate":
                    messages = self._token_budget.truncate_older_messages(messages)
                    logger.info("TokenBudget: truncated context to fit budget")
                elif budget_verdict.action == "summarize":
                    messages = self._token_budget.summarize_older_messages(messages)
                    logger.info("TokenBudget: summarized older context")

                # Re-estimate after potential summarization/truncation
                # Construir effective_system ANTES del circuit-breaker para incluir su
                # costo en pre_call_tokens. Evita bypass del límite cuando system_prompt
                # o injected_context (RAG) son grandes y messages solos no exceden el umbral.
                effective_system = f"{self._system_prompt}\n\n{injected_context}"
                system_tokens = self._token_budget.estimate_tokens(effective_system)
                pre_call_tokens = self._token_budget._estimate_messages_tokens(messages) + system_tokens

                # ── FASE 1.5.3: Circuit breaker pre-check ──────────────────
                if not self._circuit_breaker.check_request(pre_call_tokens):
                    logger.warning(
                        "TokenCircuitBreaker: request rejected (state=%s, est=%d tokens incl. system)",
                        self._circuit_breaker.state,
                        pre_call_tokens,
                    )
                    return (
                        "\u26a0\ufe0f Circuit breaker activado \u2014 consumo de tokens excesivo. Espera y reintenta."
                    )
                effective_tools = tool_schemas
                if self._hermes_mode and self._tools:
                    effective_system = (
                        effective_system + _HERMES_TOOL_INSTRUCTIONS + self._tools.hermes_system_prompt_block()
                    )
                    effective_tools = []

                chat_kwargs = {
                    "messages": messages,
                    "tools": effective_tools,
                    "session": session,
                    "gateway": self._gateway,
                    "system_prompt": effective_system,
                }
                if self._model:
                    chat_kwargs["model"] = self._model

                # Lock transaccional SRE para Hot-Swapping seguro sin caída de Event Loop
                # RND-01: Timeout de 120s para evitar que un provider colgado bloquee el lock indefinidamente
                async with self._provider_lock:
                    if not self._provider:
                        raise RuntimeError(
                            "SISTEMA: LLM Provider nulo. Iniciar Hot-Swap o configurar API Key primaria."
                        )
                    response_data = await asyncio.wait_for(
                        self._provider.chat(**chat_kwargs),
                        timeout=120.0,
                    )

                if response_data is None or not isinstance(response_data, dict):
                    return "Error: El proveedor de IA no devolvió datos."

                stop_reason = response_data.get("stop_reason", "end_turn")
                content_blocks: list[dict[str, Any]] = response_data.get("content", [])

                # ── FASE 1.5.3: Record token usage after LLM response ──────
                response_tokens = self._token_budget.estimate_tokens(json.dumps(content_blocks, default=str))
                self._token_budget.record_usage(pre_call_tokens + response_tokens)
                self._circuit_breaker.record_response(pre_call_tokens + response_tokens)

                await self._save_message(chat_id, "assistant", json.dumps(content_blocks))
                messages.append({"role": "assistant", "content": content_blocks})

                # ── Hermes mode: detect <tool_call> tags in plain text ──────────────
                if self._hermes_mode:
                    full_text = "\n".join(
                        block.get("text", "") for block in content_blocks if block.get("type") == "text"
                    )
                    if has_tool_calls(full_text):
                        try:
                            calls = extract_tool_calls(full_text)
                        except ValueError as exc:
                            hermes_parse_error_count += 1
                            if hermes_parse_error_count >= MAX_HERMES_RETRIES:
                                return "Error: max self-healing retries exceeded (parse error)."
                            # role="user" — Hermes mode has no native tool_call_id contract
                            messages.append(
                                {
                                    "role": "user",
                                    "content": sanitize_for_prompt(f"[Tool Error] Error parsing tool call: {exc}"),
                                }
                            )
                            continue
                        # Successful parse resets the parse-error budget so the
                        # next malformed block gets a fresh allowance.
                        hermes_parse_error_count = 0
                        for call in calls:
                            tool_name_h = call["name"]
                            tool_args_h = call["arguments"]
                            try:
                                result_str_h = await asyncio.wait_for(
                                    self._tools.execute(tool_name_h, tool_args_h),
                                    timeout=DEFAULT_TOOL_ROUND_TIMEOUT,
                                )
                                hermes_exec_error_count = 0
                            except (asyncio.CancelledError, KeyboardInterrupt):
                                raise
                            except pydantic.ValidationError as ve:
                                hermes_exec_error_count += 1
                                if hermes_exec_error_count >= MAX_HERMES_RETRIES:
                                    return "Error: max self-healing retries exceeded (execution error)."
                                feedback = _format_validation_feedback(tool_name_h, ve)
                                error_content = sanitize_for_prompt(
                                    f"[Tool Error] {json.dumps(feedback, ensure_ascii=False, default=str)}"
                                )
                                await self._save_message(chat_id, "user", error_content)
                                messages.append({"role": "user", "content": error_content})
                                continue
                            except (KeyError, ValueError, TypeError, RuntimeError, OSError) as exc:
                                hermes_exec_error_count += 1
                                if hermes_exec_error_count >= MAX_HERMES_RETRIES:
                                    return "Error: max self-healing retries exceeded (execution error)."
                                error_content = sanitize_for_prompt(
                                    f"[Tool Error] Error executing {tool_name_h}: {exc}"
                                )
                                await self._save_message(chat_id, "user", error_content)
                                messages.append({"role": "user", "content": error_content})
                                continue  # give every call its own error response; don't drop the rest
                            if len(result_str_h) > 4000:
                                result_str_h = result_str_h[:4000] + "\n\n[... truncated ...]"
                            tool_content = sanitize_for_prompt(f"[Tool Result] {result_str_h}")
                            await self._save_message(chat_id, "user", tool_content)
                            messages.append({"role": "user", "content": tool_content})
                        messages = messages[-self._max_context :]
                        continue
                    else:
                        hermes_parse_error_count = 0
                        hermes_exec_error_count = 0
                        # Output gate — apply same guardrail as non-Hermes path
                        if self._guardrail:
                            try:
                                await self._guardrail.after_model_callback(full_text)
                            except SecurityViolationError as exc:
                                logger.warning("Guardrail blocked Hermes output: %s", exc)
                                return "\u26a0\ufe0f La respuesta fue bloqueada por una pol\u00edtica de seguridad."
                            except AgentOrchestrationError as exc:
                                logger.error("Schema violation in Hermes output: %s", exc)
                                return "\u26a0\ufe0f La respuesta del modelo no cumple el esquema esperado."
                        return full_text
                # ── end Hermes branch ────────────────────────────────────────────────

                if stop_reason != "tool_use":
                    text_parts = [block.get("text", "") for block in content_blocks if block.get("type") == "text"]
                    final_text = "\n".join(text_parts)

                    # Output gate — guardrail (Titan v7.0)
                    if self._guardrail:
                        try:
                            await self._guardrail.after_model_callback(final_text)
                        except SecurityViolationError as exc:
                            logger.warning("Guardrail blocked output: %s", exc)
                            return "\u26a0\ufe0f La respuesta fue bloqueada por una pol\u00edtica de seguridad."
                        except AgentOrchestrationError as exc:
                            logger.error("Schema violation in output: %s", exc)
                            return "\u26a0\ufe0f La respuesta del modelo no cumple el esquema esperado."

                    return final_text

                # Execute requested tools con integración LCEL.
                tool_results: list[dict[str, Any]] = []
                for block in content_blocks:
                    if block.get("type") != "tool_use":
                        continue
                    tool_id: str = block["id"]
                    tool_name: str = block["name"]
                    tool_input: dict[str, Any] = block.get("input", {})

                    try:
                        # Usar LCEL para componer prompt de herramienta si está disponible
                        if route_classification.intent == "EJECUCION_HERRAMIENTA":
                            self._lcel_prompt_composer.compose_tool_prompt(
                                tool_name=tool_name,
                                tool_input=tool_input,
                                tool_description=route_classification.parameters.get(
                                    "description", f"Ejecutar {tool_name}"
                                ),
                            )
                            logger.debug(f"🔧 LCEL tool prompt compuesto para {tool_name}")

                        # Ejecutar herramienta con compatibilidad AsyncToolRegistry
                        result_str = await asyncio.wait_for(
                            self._tools.execute(tool_name, tool_input),
                            timeout=DEFAULT_TOOL_ROUND_TIMEOUT,
                        )
                        consecutive_errors = 0
                        if progress_callback:
                            asyncio.create_task(progress_callback(f"executed_{tool_name}", 100))
                    except pydantic.ValidationError as ve:
                        # TASK-012: hallucinated arguments — return structured
                        # feedback so the model can self-correct on the next round.
                        # MUST come before the ValueError branch since
                        # ``pydantic.ValidationError`` subclasses ``ValueError``.
                        consecutive_errors += 1
                        backoff = min(2**consecutive_errors, 16)
                        logger.warning(
                            "Tool argument validation failed for %s (%d errors, attempt %d). Backing off %ds...",
                            tool_name,
                            len(ve.errors()),
                            consecutive_errors,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        feedback = _format_validation_feedback(tool_name, ve)
                        result_str = json.dumps(feedback, ensure_ascii=False, default=str)
                    except (
                        KeyError,
                        ValueError,
                        TypeError,
                        RuntimeError,
                        OSError,
                    ) as exc:
                        # Broadened exception handling for system/runtime errors (xEdit/LOOT zombies)
                        consecutive_errors += 1
                        backoff = min(2**consecutive_errors, 16)
                        logger.warning(
                            "Tool execution error (%s): %s (attempt %d). Backing off %ds...",
                            type(exc).__name__,
                            exc,
                            consecutive_errors,
                            backoff,
                        )
                        await asyncio.sleep(backoff)

                        feedback = {
                            "error": "Critical tool execution failure.",
                            "exception_type": type(exc).__name__,
                            "details": sanitize_for_prompt(str(exc)),
                            "instruction": "Verify that requirements are met and that external processes (LOOT/xEdit) are not blocked.",
                        }
                        result_str = json.dumps(feedback)

                    if len(result_str) > 4000:
                        result_str = result_str[:4000] + "\n\n[... truncated ...]"

                    # TASK-013 P0: sanitize tool output before injecting back into
                    # the LLM context — prevents indirect prompt injection via
                    # adversarial mod metadata, file paths, or HTTP responses.
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": sanitize_for_prompt(result_str),
                        }
                    )

                await self._save_message(chat_id, "user", json.dumps(tool_results))
                messages.append({"role": "user", "content": tool_results})
                messages = messages[-self._max_context :]
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except (MemoryError, SystemError, RecursionError, AttributeError, ImportError, SyntaxError) as fatal:
                logger.critical("Fatal router error, cannot continue: %s", fatal, exc_info=True)
                raise
            except Exception as outer_exc:
                logger.exception("System-level router failure: %s", outer_exc)
                return "Error Critico: El ciclo de herramientas fallo por una excepcion interna. Consulta los logs del servidor."
        else:
            raise RuntimeError(f"Agent exceeded {MAX_TOOL_ROUNDS} tool rounds")

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------

    async def _save_message(self, chat_id: str, role: str, content: str) -> None:
        """Persist a message to the history database immediately."""
        if self._conn is None:
            raise RuntimeError("Router database is not open")
        await self._conn.execute(
            "INSERT INTO chat_history (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, time.time()),
        )
        await self._conn.commit()

    async def _load_context(self, chat_id: str) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Router database is not open")
        async with self._conn.execute(
            "SELECT role, content FROM chat_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
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
