"""LLM provider abstraction – multi-backend support.

Supports Anthropic Claude, DeepSeek, and Ollama as interchangeable
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
import os
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

    def __init__(self, api_key: str) -> None:
        # Eliminamos la dependencia del entorno. Guardamos la llave inyectada.
        self._api_key = api_key

    @retry(
        wait=wait_exponential(multiplier=1.5, min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception(_should_retry),
    )
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
        model = model or self.DEFAULT_MODEL
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


def _convert_messages_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic message format to OpenAI chat format."""
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue
        if isinstance(content, list) and content and isinstance(content[0], dict):
            first_type = content[0].get("type", "")
            if first_type == "tool_result":
                for block in content:
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": block.get("content", ""),
                        }
                    )
                continue
            text_parts = []
            tool_calls = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id", uuid.uuid4().hex),
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        }
                    )
            msg_dict: dict[str, Any] = {
                "role": role,
                "content": "\n".join(text_parts) if text_parts else ("..." if not tool_calls else None),
            }
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
            result.append(msg_dict)
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

    def __init__(self, api_key: str) -> None:
        # Eliminamos la dependencia del entorno. Guardamos la llave inyectada.
        self._api_key = api_key

    @retry(
        wait=wait_exponential(multiplier=1.5, min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception(_should_retry),
    )
    async def chat(self, messages, tools, session, gateway, *, system_prompt="", model=""):
        model = model or self.DEFAULT_MODEL
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
                logger.error(
                    "DeepSeek error %d: %s. Body: %s",
                    resp.status,
                    text,
                    json.dumps(body)[:500],
                )
            resp.raise_for_status()
            data = await resp.json()
        return _parse_openai_response(data)


class OllamaProvider(LLMProvider):
    DEFAULT_MODEL = "llama3.1"

    def __init__(self, base_url="http://localhost:11434"):
        self._base_url = base_url.rstrip("/")

    @retry(
        wait=wait_exponential(multiplier=1.5, min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception(_should_retry),
    )
    async def chat(self, messages, tools, session, gateway, *, system_prompt="", model=""):
        model = model or self.DEFAULT_MODEL
        oai_messages = _convert_messages_to_openai(messages)
        if system_prompt:
            oai_messages.insert(0, {"role": "system", "content": system_prompt})
        body = {"model": model, "messages": oai_messages}
        oai_tools = _convert_tools_to_openai(tools)
        if oai_tools:
            body["tools"] = oai_tools
        url = f"{self._base_url}/v1/chat/completions"
        async with await gateway.request("POST", url, session, json=body) as resp:
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
            key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            if not key:
                raise ProviderConfigError("ANTHROPIC_API_KEY is required.")
            logger.info("Using Anthropic provider")
            return AnthropicProvider(key)
        if name == "deepseek":
            key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
            if not key:
                raise ProviderConfigError("DEEPSEEK_API_KEY is required.")
            logger.info("Using DeepSeek provider")
            return DeepSeekProvider(key)
        if name == "ollama":
            logger.info("Using Ollama provider (local)")
            return OllamaProvider()
        raise ProviderConfigError(f"Unknown provider: {name}")

    # Auto-detect from environment
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        logger.info("Auto-detected Anthropic provider")
        return AnthropicProvider(anthropic_key)

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        logger.info("Auto-detected DeepSeek provider")
        return DeepSeekProvider(deepseek_key)

    logger.info("No API keys found — falling back to Ollama")
    return OllamaProvider()
