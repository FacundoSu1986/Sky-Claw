"""Tests for LLM provider abstraction layer."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import aiohttp
import pytest

from sky_claw.antigravity.agent.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderConfigError,
    _convert_messages_to_openai,
    _convert_tools_to_openai,
    _parse_openai_response,
    _should_retry,
    create_provider,
)

# ------------------------------------------------------------------
# Tool/message conversion helpers
# ------------------------------------------------------------------


class TestConvertToolsToOpenAI:
    def test_converts_tool_schema(self) -> None:
        tools = [
            {
                "name": "search_mod",
                "description": "Search mods",
                "input_schema": {
                    "type": "object",
                    "properties": {"mod_name": {"type": "string"}},
                    "required": ["mod_name"],
                },
            }
        ]
        result = _convert_tools_to_openai(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search_mod"
        assert result[0]["function"]["parameters"]["type"] == "object"

    def test_empty_tools(self) -> None:
        assert _convert_tools_to_openai([]) == []


class TestConvertMessagesToOpenAI:
    def test_text_message(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        result = _convert_messages_to_openai(msgs)
        assert result == [{"role": "user", "content": "hello"}]

    def test_tool_result_blocks(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": '{"matches": []}',
                    }
                ],
            }
        ]
        result = _convert_messages_to_openai(msgs)
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "t1"

    def test_assistant_with_tool_use(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Searching..."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "search_mod",
                        "input": {"mod_name": "Requiem"},
                    },
                ],
            }
        ]
        result = _convert_messages_to_openai(msgs)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Searching..."
        assert len(result[0]["tool_calls"]) == 1
        assert result[0]["tool_calls"][0]["function"]["name"] == "search_mod"


class TestParseOpenAIResponse:
    def test_text_only(self) -> None:
        data = {
            "choices": [
                {
                    "message": {"content": "Hello!"},
                    "finish_reason": "stop",
                }
            ]
        }
        result = _parse_openai_response(data)
        assert result["stop_reason"] == "end_turn"
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Hello!"

    def test_tool_calls(self) -> None:
        data = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "search_mod",
                                    "arguments": '{"mod_name": "Requiem"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        result = _parse_openai_response(data)
        assert result["stop_reason"] == "tool_use"
        assert result["content"][0]["type"] == "tool_use"
        assert result["content"][0]["name"] == "search_mod"
        assert result["content"][0]["input"] == {"mod_name": "Requiem"}

    def test_empty_choices(self) -> None:
        result = _parse_openai_response({"choices": []})
        assert result["stop_reason"] == "end_turn"
        assert result["content"] == []

    def test_malformed_arguments(self) -> None:
        data = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "test",
                                    "arguments": "not json",
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        result = _parse_openai_response(data)
        assert result["content"][0]["input"] == {}


# ------------------------------------------------------------------
# DeepSeek provider
# ------------------------------------------------------------------


class TestDeepSeekProvider:
    @pytest.mark.asyncio
    async def test_chat_sends_correct_request(self) -> None:
        provider = DeepSeekProvider(api_key="test-key")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {"content": "Hola!"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        session = MagicMock(spec=aiohttp.ClientSession)

        messages = [{"role": "user", "content": "Hola"}]
        tools = [
            {
                "name": "search_mod",
                "description": "Search",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]

        result = await provider.chat(
            messages,
            tools,
            session,
            gateway=mock_gateway,
            system_prompt="You are helpful",
        )

        assert result["stop_reason"] == "end_turn"
        assert result["content"][0]["text"] == "Hola!"

        # Verify the request was made correctly
        call_args = mock_gateway.request.call_args
        request_url = call_args[0][1]
        assert urlparse(request_url).hostname == "api.deepseek.com"
        body = call_args[1]["json"]
        assert body["model"] == "deepseek-chat"
        assert body["messages"][0]["role"] == "system"
        assert "tools" in body
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-key"

    @pytest.mark.asyncio
    async def test_error_no_loguea_body_de_usuario(self, caplog) -> None:
        """M-6: en 4xx/5xx el log NO debe contener el contenido de los mensajes."""
        import logging

        provider = DeepSeekProvider(api_key="key")

        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.text = AsyncMock(return_value="Unauthorized")
        mock_response.raise_for_status = MagicMock(side_effect=RuntimeError("401"))
        mock_response.json = AsyncMock(return_value={})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)
        session = MagicMock(spec=aiohttp.ClientSession)

        secreto = "informacion-sensible-del-usuario-42"
        messages = [{"role": "user", "content": secreto}]

        with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
            await provider.chat(messages, [], session, gateway=mock_gateway)

        logs = "\n".join(r.getMessage() for r in caplog.records)
        assert "DeepSeek error 401" in logs
        # El contenido del mensaje del usuario NO debe filtrarse al log.
        assert secreto not in logs

    @pytest.mark.asyncio
    async def test_chat_with_tool_response(self) -> None:
        provider = DeepSeekProvider(api_key="key")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_mod",
                                        "arguments": '{"mod_name": "USSEP"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        session = MagicMock(spec=aiohttp.ClientSession)

        result = await provider.chat(
            [{"role": "user", "content": "busca USSEP"}],
            [],
            session,
            gateway=mock_gateway,
        )

        assert result["stop_reason"] == "tool_use"
        assert result["content"][0]["name"] == "search_mod"
        assert result["content"][0]["input"]["mod_name"] == "USSEP"


# ------------------------------------------------------------------
# OpenAI provider (OpenAI-compatible, mirrors DeepSeek)
# ------------------------------------------------------------------


class TestOpenAIProvider:
    @pytest.mark.asyncio
    async def test_chat_sends_correct_request(self) -> None:
        provider = OpenAIProvider(api_key="test-key")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {"content": "Hi!"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        session = MagicMock(spec=aiohttp.ClientSession)

        result = await provider.chat(
            [{"role": "user", "content": "Hi"}],
            [
                {
                    "name": "search_mod",
                    "description": "Search",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            session,
            gateway=mock_gateway,
            system_prompt="You are helpful",
        )

        assert result["stop_reason"] == "end_turn"
        assert result["content"][0]["text"] == "Hi!"

        call_args = mock_gateway.request.call_args
        request_url = call_args[0][1]
        assert urlparse(request_url).hostname == "api.openai.com"
        body = call_args[1]["json"]
        assert body["model"] == "gpt-5"  # default, overridable via model=
        assert body["messages"][0]["role"] == "system"
        assert "tools" in body
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-key"

    @pytest.mark.asyncio
    async def test_chat_respects_model_override(self) -> None:
        provider = OpenAIProvider(api_key="k")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        await provider.chat(
            [{"role": "user", "content": "x"}],
            [],
            MagicMock(spec=aiohttp.ClientSession),
            gateway=mock_gateway,
            model="gpt-4o",
        )

        assert mock_gateway.request.call_args[1]["json"]["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_uses_configured_instance_model(self) -> None:
        """A model passed at construction (from config) is used when chat() omits one."""
        provider = OpenAIProvider(api_key="k", model="gpt-4o")
        assert provider.model == "gpt-4o"

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        await provider.chat(
            [{"role": "user", "content": "x"}],
            [],
            MagicMock(spec=aiohttp.ClientSession),
            gateway=mock_gateway,
        )

        assert mock_gateway.request.call_args[1]["json"]["model"] == "gpt-4o"

    def test_defaults_to_gpt5_when_no_model_configured(self) -> None:
        assert OpenAIProvider(api_key="k").model == "gpt-5"


# ------------------------------------------------------------------
# Ollama provider
# ------------------------------------------------------------------


class TestOllamaProvider:
    @pytest.mark.asyncio
    async def test_chat_uses_local_url(self) -> None:
        provider = OllamaProvider(base_url="http://localhost:11434")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {"content": "OK"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        session = MagicMock(spec=aiohttp.ClientSession)

        result = await provider.chat([{"role": "user", "content": "test"}], [], session, gateway=mock_gateway)

        assert result["stop_reason"] == "end_turn"
        call_url = mock_gateway.request.call_args[0][1]
        assert "localhost:11434" in call_url
        assert "/v1/chat/completions" in call_url

    @pytest.mark.asyncio
    async def test_no_auth_header(self) -> None:
        provider = OllamaProvider()

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        session = MagicMock(spec=aiohttp.ClientSession)

        await provider.chat([{"role": "user", "content": "x"}], [], session, gateway=mock_gateway)

        # Ollama doesn't send auth headers
        call_kwargs = mock_gateway.request.call_args[1]
        assert "headers" not in call_kwargs


# ------------------------------------------------------------------
# Anthropic provider
# ------------------------------------------------------------------


class TestAnthropicProvider:
    @pytest.mark.asyncio
    async def test_chat_sends_anthropic_format(self) -> None:
        provider = AnthropicProvider(api_key="sk-test")

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done"}],
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        session = MagicMock(spec=aiohttp.ClientSession)

        result = await provider.chat(
            [{"role": "user", "content": "hi"}],
            [],
            session,
            gateway=mock_gateway,
            system_prompt="test",
        )

        assert result["stop_reason"] == "end_turn"
        assert result["content"][0]["text"] == "Done"

        headers = mock_gateway.request.call_args[1]["headers"]
        assert headers["x-api-key"] == "sk-test"
        assert "anthropic-version" in headers


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


class TestCreateProvider:
    def test_explicit_anthropic(self) -> None:
        provider = create_provider(provider_name="anthropic", api_key="sk-1")
        assert isinstance(provider, AnthropicProvider)

    def test_explicit_deepseek(self) -> None:
        provider = create_provider(provider_name="deepseek", api_key="ds-1")
        assert isinstance(provider, DeepSeekProvider)

    def test_explicit_ollama(self) -> None:
        provider = create_provider(provider_name="ollama")
        assert isinstance(provider, OllamaProvider)

    def test_explicit_openai(self) -> None:
        provider = create_provider(provider_name="openai", api_key="oai-1")
        assert isinstance(provider, OpenAIProvider)

    def test_openai_without_key_raises(self) -> None:
        with pytest.raises(ProviderConfigError, match="OPENAI_API_KEY"):
            create_provider(provider_name="openai")

    def test_openai_honors_configured_model(self) -> None:
        provider = create_provider(provider_name="openai", api_key="oai-1", model="gpt-4o")
        assert isinstance(provider, OpenAIProvider)
        assert provider.model == "gpt-4o"

    def test_explicit_unknown_raises(self) -> None:
        with pytest.raises(ProviderConfigError, match="Unknown provider"):
            create_provider(provider_name="gpt5")

    def test_anthropic_without_key_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True), pytest.raises(ProviderConfigError, match="ANTHROPIC_API_KEY"):
            create_provider(provider_name="anthropic")

    def test_explicit_api_key_anthropic(self) -> None:
        provider = create_provider(api_key="sk-auto")
        assert isinstance(provider, AnthropicProvider)

    def test_explicit_provider_and_key_deepseek(self) -> None:
        provider = create_provider(provider_name="deepseek", api_key="ds-auto")
        assert isinstance(provider, DeepSeekProvider)

    def test_fallback_to_ollama(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env = os.environ.copy()
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("DEEPSEEK_API_KEY", None)
            with patch.dict(os.environ, env, clear=True):
                provider = create_provider()
                assert isinstance(provider, OllamaProvider)


# ------------------------------------------------------------------
# Uniform model honoring (config.llm_model) across all providers
# ------------------------------------------------------------------


class TestModelHonoring:
    """Every provider must store the configured model and fall back to its
    DEFAULT_MODEL when none is given, so config.llm_model is honored uniformly.
    """

    def test_each_provider_stores_configured_model(self) -> None:
        assert AnthropicProvider("k", model="claude-x").model == "claude-x"
        assert DeepSeekProvider("k", model="ds-x").model == "ds-x"
        assert OllamaProvider(model="llama-x").model == "llama-x"
        assert OpenAIProvider("k", model="gpt-x").model == "gpt-x"

    def test_each_provider_defaults_when_no_model(self) -> None:
        assert AnthropicProvider("k").model == AnthropicProvider.DEFAULT_MODEL
        assert DeepSeekProvider("k").model == DeepSeekProvider.DEFAULT_MODEL
        assert OllamaProvider().model == OllamaProvider.DEFAULT_MODEL
        assert OpenAIProvider("k").model == OpenAIProvider.DEFAULT_MODEL

    def test_create_provider_threads_model_to_each(self) -> None:
        assert create_provider(provider_name="anthropic", api_key="k", model="claude-y").model == "claude-y"
        assert create_provider(provider_name="deepseek", api_key="k", model="ds-y").model == "ds-y"
        assert create_provider(provider_name="ollama", model="llama-y").model == "llama-y"
        assert create_provider(provider_name="openai", api_key="k", model="gpt-y").model == "gpt-y"

    def test_implicit_fallback_paths_thread_model(self) -> None:
        # No provider_name: explicit api_key → Anthropic fallback must honor model.
        assert create_provider(api_key="k", model="claude-z").model == "claude-z"
        # No provider_name, no api_key → Ollama fallback must honor model too.
        assert create_provider(model="llama-z").model == "llama-z"

    @pytest.mark.asyncio
    async def test_anthropic_uses_instance_model_in_request(self) -> None:
        provider = AnthropicProvider("k", model="claude-custom")
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(return_value={"stop_reason": "end_turn", "content": []})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_gateway = MagicMock()
        mock_gateway.request = AsyncMock(return_value=mock_response)

        await provider.chat(
            [{"role": "user", "content": "x"}],
            [],
            MagicMock(spec=aiohttp.ClientSession),
            gateway=mock_gateway,
        )

        assert mock_gateway.request.call_args[1]["json"]["model"] == "claude-custom"


# ------------------------------------------------------------------
# _should_retry – fix verification (audit finding #1: syntax guard)
# ------------------------------------------------------------------


class TestShouldRetry:
    """Verify _should_retry is syntactically correct and behaves as expected.

    This test class guards against the extra-parenthesis SyntaxError that
    was identified in the audit (providers.py line ~47).
    """

    def test_retries_on_connection_error(self) -> None:
        exc = aiohttp.ClientConnectionError()
        assert _should_retry(exc) is True

    def test_retries_on_timeout(self) -> None:
        exc = TimeoutError()
        assert _should_retry(exc) is True

    def test_retries_on_429(self) -> None:
        from unittest.mock import Mock

        exc = aiohttp.ClientResponseError(request_info=Mock(), history=(), status=429)
        assert _should_retry(exc) is True

    def test_retries_on_503(self) -> None:
        from unittest.mock import Mock

        exc = aiohttp.ClientResponseError(request_info=Mock(), history=(), status=503)
        assert _should_retry(exc) is True

    def test_no_retry_on_400(self) -> None:
        from unittest.mock import Mock

        exc = aiohttp.ClientResponseError(request_info=Mock(), history=(), status=400)
        assert _should_retry(exc) is False

    def test_no_retry_on_generic_exception(self) -> None:
        assert _should_retry(ValueError("unrelated")) is False
