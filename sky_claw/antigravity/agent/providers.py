"""LLM provider abstraction – multi-backend support.

Supports Anthropic Claude, DeepSeek, OpenAI, and Ollama as interchangeable
LLM backends.  Each provider normalises its API response into a
common internal format consumed by :class:`LLMRouter`.

Provider selection follows a fallback chain::

    ANTHROPIC_API_KEY set  →  AnthropicProvider
    DEEPSEEK_API_KEY set   →  DeepSeekProvider
    Ollama running locally →  OllamaProvider
    Nothing available      →  ConfigurationError
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any

import aiohttp
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Internal response format returned by all providers.
# {
#     "stop_reason": "end_turn" | "tool_use",
#     "content": [
#         {"type": "text", "text": "..."},
#         {"type": "tool_use", "id": "...", "name": "...", "input": {...}},
#     ],
# }


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError)):
        return True
    return isinstance(exc, aiohttp.ClientResponseError) and (exc.status == 429 or exc.status >= 500)


# P-1: política de retry conservadora para APIs de pago remotas (rate-limits,
# backoff largo justificado). Usada por Anthropic/DeepSeek/OpenAI.
_PAID_API_RETRY: dict[str, Any] = {
    "wait": wait_exponential(multiplier=1.5, min=2, max=60),
    "stop": stop_after_attempt(5),
    "retry": retry_if_exception(_should_retry),
}

# P-1: Ollama es local — sin rate-limiting, así que un hipo se reintenta
# rápido; si el servicio está caído, no tiene sentido malgastar los backoffs
# largos pensados para APIs remotas.
_LOCAL_RETRY: dict[str, Any] = {
    "wait": wait_exponential(multiplier=0.5, min=0.5, max=5),
    "stop": stop_after_attempt(3),
    "retry": retry_if_exception(_should_retry),
}


class LLMProvider(ABC):
    """Base class for LLM API providers."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        session: aiohttp.ClientSession,
        gateway: Any,  # NetworkGateway
        *,
        system_prompt: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        """Send a chat request and return a normalised response dict.

        Returns:
            Dict with ``stop_reason`` and ``content`` keys.
        """


# ------------------------------------------------------------------
# Anthropic Claude
# ------------------------------------------------------------------


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API provider."""

    API_URL = "https://api.anthropic.com/v1/messages"
    DEFAULT_MODEL = "claude-3-5-sonnet-20240620"

    def __init__(self, api_key: str, model: str = "") -> None:
        # Eliminamos la dependencia del entorno. Guardamos la llave inyectada.
        self._api_key = api_key
        #: Effective default model (config-driven; public for AppContext logging).
        self.model = model or self.DEFAULT_MODEL

    @retry(**_PAID_API_RETRY)
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        session: aiohttp.ClientSession,
        gateway: Any,
        *,
        system_prompt: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        model = model or self.model
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "messages": messages,
            "tools": tools,
        }
        if system_prompt:
            body["system"] = system_prompt

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with await gateway.request("POST", self.API_URL, session, json=body, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                logger.error("Anthropic error %d: %s", resp.status, text)
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json()

        return {
            "stop_reason": data.get("stop_reason", "end_turn"),
            "content": data.get("content", []),
        }


# ------------------------------------------------------------------
# OpenAI-compatible base (DeepSeek / Ollama)
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# OpenAI-compatible base (DeepSeek / Ollama) - ESTÁNDAR CLAUDE ENGINE
# ------------------------------------------------------------------


def _convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool schemas to OpenAI function-calling format."""
    result = []
    for tool in tools:
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )
    return result


def _flush_openai_role_message(
    result: list[dict[str, Any]],
    role: str,
    text_parts: list[str],
    tool_calls: list[dict[str, Any]],
    pending: bool,
) -> None:
    """Append the accumulated text/tool_use as a single role message, if any.

    ``pending`` distinguishes "nothing seen since the last flush" from "saw a
    block but it produced no text/tool_calls" (e.g. an unrecognised block
    type) — the latter still needs the ``"..."`` fallback message.
    """
    if not pending:
        return
    msg_dict: dict[str, Any] = {
        "role": role,
        "content": "\n".join(text_parts) if text_parts else ("..." if not tool_calls else None),
    }
    if tool_calls:
        msg_dict["tool_calls"] = list(tool_calls)
    result.append(msg_dict)


def _convert_messages_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic message format to OpenAI chat format.

    P-3: un mensaje puede mezclar bloques ``tool_result`` con bloques
    ``text``/``tool_use`` (p. ej. el modelo comenta mientras reporta un
    resultado de tool). Ramificar solo por ``content[0].get("type")`` con un
    ``continue`` temprano perdía el resto del mensaje según qué tipo de
    bloque viniera primero. Se clasifica cada bloque en una sola pasada,
    intercalando (flush) el texto/tool_use acumulado antes de cada
    tool_result para preservar el orden original — invertirlo cambiaría el
    contexto que ve el modelo (un tool_result no puede parecer anterior al
    texto que en realidad lo precedía).
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            pending = False
            for block in content:
                block_type = block.get("type")
                if block_type == "tool_result":
                    _flush_openai_role_message(result, role, text_parts, tool_calls, pending)
                    text_parts, tool_calls, pending = [], [], False
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": block.get("content", ""),
                        }
                    )
                    continue
                # Marca que hubo actividad desde el último flush, aunque el
                # bloque sea de un tipo desconocido (preserva el fallback
                # "..." incluso sin texto ni tool_calls reales).
                pending = True
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    # Acceso defensivo (L-2, mismo patrón que router.py): un
                    # tool_use sin 'name' puede persistir en el historial
                    # (router.py guarda content_blocks crudo antes de su propia
                    # sanitización) y llegar acá si el LLMRouter luego cambia a
                    # un provider OpenAI-compatible. Se omite el bloque en vez
                    # de romper la conversión entera con KeyError.
                    tool_name = block.get("name")
                    if tool_name:
                        tool_calls.append(
                            {
                                "id": block.get("id", uuid.uuid4().hex),
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            }
                        )
            _flush_openai_role_message(result, role, text_parts, tool_calls, pending)
            continue
        result.append({"role": role, "content": str(content)})
    return result


def _parse_openai_response(data: dict[str, Any]) -> dict[str, Any]:
    """Convert OpenAI response to internal format."""
    choices = data.get("choices", [])
    if not choices:
        return {"stop_reason": "end_turn", "content": []}
    choice = choices[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    content_blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id", uuid.uuid4().hex),
                    "name": fn.get("name", ""),
                    "input": args,
                }
            )
    stop_reason = "tool_use" if tool_calls or finish_reason == "tool_calls" else "end_turn"
    usage = data.get("usage", {})
    return {
        "stop_reason": stop_reason,
        "content": content_blocks,
        "usage_input_tokens": usage.get("prompt_tokens", 0),
        "usage_output_tokens": usage.get("completion_tokens", 0),
    }


class DeepSeekProvider(LLMProvider):
    """DeepSeek API provider (OpenAI-compatible)."""

    API_URL = "https://api.deepseek.com/v1/chat/completions"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(self, api_key: str, model: str = "") -> None:
        # Eliminamos la dependencia del entorno. Guardamos la llave inyectada.
        self._api_key = api_key
        #: Effective default model (config-driven; public for AppContext logging).
        self.model = model or self.DEFAULT_MODEL

    @retry(**_PAID_API_RETRY)
    async def chat(self, messages, tools, session, gateway, *, system_prompt="", model=""):
        model = model or self.model
        oai_messages = _convert_messages_to_openai(messages)
        if system_prompt:
            oai_messages.insert(0, {"role": "system", "content": system_prompt})
        body = {"model": model, "messages": oai_messages, "max_tokens": 4096}
        oai_tools = _convert_tools_to_openai(tools)
        if oai_tools:
            body["tools"] = oai_tools
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with await gateway.request("POST", self.API_URL, session, json=body, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                # M-6: loguear sólo metadata — NO el request body, que contiene
                # los mensajes del usuario (prompt/tool content, sensible y
                # verboso). Consistente con el sibling OpenAIProvider.
                logger.error(
                    "DeepSeek error %d: %s (model=%s, %d msgs, %d tools)",
                    resp.status,
                    text,
                    model,
                    len(oai_messages),
                    len(oai_tools),
                )
            resp.raise_for_status()
            data = await resp.json()
        return _parse_openai_response(data)


class OpenAIProvider(LLMProvider):
    """OpenAI API provider (the reference OpenAI-compatible Chat Completions API).

    Mirrors :class:`DeepSeekProvider` — same request/response shape via the
    shared ``_convert_*`` / ``_parse_openai_response`` helpers, Bearer auth,
    and routed through the ``NetworkGateway`` (``api.openai.com`` is already on
    the egress allowlist).

    ``DEFAULT_MODEL`` is ``gpt-5``. The effective model resolves as
    ``per-call model=`` → ``self.model`` (injected at construction from the
    provider-scoped ``config.openai_model``) → ``DEFAULT_MODEL``. If the model
    is unavailable on the caller's account the API returns a 4xx that is logged
    and raised — switch models then.
    """

    API_URL = "https://api.openai.com/v1/chat/completions"
    DEFAULT_MODEL = "gpt-5"

    def __init__(self, api_key: str, model: str = "") -> None:
        self._api_key = api_key
        #: Effective default model for this instance (config-driven, public so
        #: AppContext can surface it via ``getattr(provider, "model", ...)``).
        self.model = model or self.DEFAULT_MODEL

    @retry(**_PAID_API_RETRY)
    async def chat(self, messages, tools, session, gateway, *, system_prompt="", model=""):
        model = model or self.model
        oai_messages = _convert_messages_to_openai(messages)
        if system_prompt:
            oai_messages.insert(0, {"role": "system", "content": system_prompt})
        body = {"model": model, "messages": oai_messages, "max_tokens": 4096}
        oai_tools = _convert_tools_to_openai(tools)
        if oai_tools:
            body["tools"] = oai_tools
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with await gateway.request("POST", self.API_URL, session, json=body, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                # Log metadata only — NOT the request body (prompt/tool content
                # is not credential-grade but is sensitive and verbose; the
                # redaction filter targets secrets, not arbitrary prompts).
                logger.error(
                    "OpenAI error %d: %s (model=%s, %d msgs, %d tools)",
                    resp.status,
                    text,
                    model,
                    len(oai_messages),
                    len(oai_tools),
                )
            resp.raise_for_status()
            data = await resp.json()
        return _parse_openai_response(data)


class OllamaProvider(LLMProvider):
    DEFAULT_MODEL = "llama3.1"

    #: P-2: inferencia local puede tardar minutos en hardware modesto — más
    #: generoso que el default de 45s del NetworkGateway, pero acotado (no
    #: cuelga indefinidamente si Ollama está colgado).
    DEFAULT_TIMEOUT_SECONDS: float = 300.0

    def __init__(self, base_url="http://localhost:11434", model: str = "", timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self._base_url = base_url.rstrip("/")
        #: Effective default model (config-driven; public for AppContext logging).
        self.model = model or self.DEFAULT_MODEL
        #: Presupuesto de la request HTTP (público: LLMRouter lo lee vía
        #: getattr para que su wait_for externo no corte la inferencia local
        #: legítima antes de que este timeout tenga oportunidad de disparar).
        self.timeout = timeout

    @retry(**_LOCAL_RETRY)
    async def chat(self, messages, tools, session, gateway, *, system_prompt="", model=""):
        model = model or self.model
        oai_messages = _convert_messages_to_openai(messages)
        if system_prompt:
            oai_messages.insert(0, {"role": "system", "content": system_prompt})
        body = {"model": model, "messages": oai_messages}
        oai_tools = _convert_tools_to_openai(tools)
        if oai_tools:
            body["tools"] = oai_tools
        url = f"{self._base_url}/v1/chat/completions"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with await gateway.request("POST", url, session, json=body, timeout=timeout) as resp:
            if resp.status >= 400:
                text = await resp.text()
                logger.error("Ollama error %d: %s", resp.status, text)
            resp.raise_for_status()
            return _parse_openai_response(await resp.json())


class ProviderConfigError(RuntimeError):
    pass


def create_provider(*, provider_name=None, api_key=None, model=None):
    """Factory for LLM providers with auto-detection fallback chain.

    Explicit provider_name and api_key take precedence. Following security
    hardening (April 2026), this factory does NOT mutate os.environ.
    Final fallback is Ollama.
    """
    if provider_name:
        name = provider_name.lower()
        if name == "anthropic":
            if not api_key:
                raise ProviderConfigError("ANTHROPIC_API_KEY is required. Provide it via setup wizard or config.toml.")
            logger.info("Using Anthropic provider")
            return AnthropicProvider(api_key, model=model or "")
        if name == "deepseek":
            if not api_key:
                raise ProviderConfigError("DEEPSEEK_API_KEY is required. Provide it via setup wizard or config.toml.")
            logger.info("Using DeepSeek provider")
            return DeepSeekProvider(api_key, model=model or "")
        if name == "openai":
            if not api_key:
                raise ProviderConfigError("OPENAI_API_KEY is required. Provide it via setup wizard or config.toml.")
            logger.info("Using OpenAI provider")
            return OpenAIProvider(api_key, model=model or "")
        if name == "ollama":
            logger.info("Using Ollama provider (local)")
            return OllamaProvider(model=model or "")
        raise ProviderConfigError(f"Unknown provider: {name}")

    # Zero-Trust: explicit api_key takes precedence. No os.environ fallback.
    if api_key:
        logger.info("Explicit api_key provided — using Anthropic provider")
        return AnthropicProvider(api_key, model=model or "")

    # Zero-Trust: DeepSeek key must be injected explicitly via CredentialVault or caller.

    logger.info("No API keys found — falling back to Ollama")
    return OllamaProvider(model=model or "")
