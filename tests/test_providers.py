"""Tests for LLM provider abstraction layer."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.agent.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    OllamaProvider,
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
        assert "api.deepseek.com" in call_args[0][1]
        body = call_args[1]["json"]
        assert body["model"] == "deepseek-chat"
        assert body["messages"][0]["role"] == "system"
        assert "tools" in body
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-key"

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

    def test_explicit_unknown_raises(self) -> None:
        with pytest.raises(ProviderConfigError, match="Unknown provider"):
            create_provider(provider_name="gpt5")

    def test_anthropic_without_key_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True), pytest.raises(ProviderConfigError, match="ANTHROPIC_API_KEY"):
            create_provider(provider_name="anthropic")

    def test_auto_detect_anthropic(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-auto"}, clear=True):
            provider = create_provider()
            assert isinstance(provider, AnthropicProvider)

    def test_auto_detect_deepseek(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "ds-auto"}, clear=True):
            provider = create_provider()
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
