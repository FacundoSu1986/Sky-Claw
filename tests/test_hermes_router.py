"""Tests for LLMRouter in hermes_mode=True."""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.agent.providers import AnthropicProvider
from sky_claw.agent.router import MAX_HERMES_RETRIES, LLMRouter
from sky_claw.agent.tools import AsyncToolRegistry
from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.mo2.vfs import MO2Controller
from sky_claw.orchestrator.sync_engine import SyncEngine
from sky_claw.scraper.masterlist import MasterlistClient
from sky_claw.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.security.path_validator import PathValidator


def _make_mo2(tmp_path: pathlib.Path) -> MO2Controller:
    profile_dir = tmp_path / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text("+TestMod-100\n", encoding="utf-8")
    validator = PathValidator(roots=[tmp_path])
    return MO2Controller(tmp_path, path_validator=validator)


@pytest.fixture()
async def adb(tmp_path: pathlib.Path) -> AsyncModRegistry:
    registry = AsyncModRegistry(db_path=tmp_path / "hermes_router_test.db")
    await registry.open()
    yield registry  # type: ignore[misc]
    await registry.close()


@pytest.fixture()
def tool_registry(adb: AsyncModRegistry, tmp_path: pathlib.Path) -> AsyncToolRegistry:
    mo2 = _make_mo2(tmp_path)
    gw = NetworkGateway(EgressPolicy(block_private_ips=False))
    masterlist = MasterlistClient(gateway=gw, api_key="fake")
    engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=adb)
    return AsyncToolRegistry(registry=adb, mo2=mo2, sync_engine=engine)


@pytest.fixture()
async def hermes_router(tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path) -> LLMRouter:
    mock_gateway = MagicMock(spec=NetworkGateway)
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.text = AsyncMock(return_value="")
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    mock_gateway.request = AsyncMock(return_value=mock_cm)

    r = LLMRouter(
        provider=AnthropicProvider("test-key"),
        tool_registry=tool_registry,
        db_path=str(tmp_path / "hermes_chat.db"),
        system_prompt="You are a Skyrim assistant.",
        gateway=mock_gateway,
        hermes_mode=True,
    )
    await r.open()
    r._semantic_router.route = MagicMock(
        return_value={
            "intent": "CHAT_GENERAL",
            "confidence": 0.7,
            "target_agent": None,
            "tool_name": None,
            "parameters": {},
            "original_text": "",
        }
    )
    yield r  # type: ignore[misc]
    await r.close()


# ------------------------------------------------------------------
# Init-time guard
# ------------------------------------------------------------------


class TestHermesInitGuard:
    def test_hermes_mode_without_registry_raises(self, tmp_path: pathlib.Path) -> None:
        """hermes_mode=True without a tool_registry must fail at construction time."""
        with pytest.raises(ValueError, match="tool_registry"):
            LLMRouter(
                provider=AnthropicProvider("test-key"),
                tool_registry=None,
                db_path=str(tmp_path / "x.db"),
                gateway=MagicMock(),
                hermes_mode=True,
            )


# ------------------------------------------------------------------
# System prompt injection
# ------------------------------------------------------------------


class TestHermesSystemPrompt:
    @pytest.mark.asyncio
    async def test_tools_block_injected_in_system_prompt(
        self, hermes_router: LLMRouter, tool_registry: AsyncToolRegistry
    ) -> None:
        """When hermes_mode=True, provider.chat receives <tools> in system_prompt."""
        captured: list[dict[str, Any]] = []

        async def fake_chat(**kwargs: Any) -> dict[str, Any]:
            captured.append(kwargs)
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}

        hermes_router._provider.chat = fake_chat  # type: ignore[method-assign]
        mock_session = MagicMock(spec=aiohttp.ClientSession)
        await hermes_router.chat("hello", mock_session, chat_id="h-1")

        assert captured, "provider.chat was never called"
        system_prompt = captured[0]["system_prompt"]
        assert "<tools>" in system_prompt
        assert "search_mod" in system_prompt
        assert captured[0]["tools"] == []


# ------------------------------------------------------------------
# Tool call detection and execution
# ------------------------------------------------------------------


class TestHermesToolExecution:
    @pytest.mark.asyncio
    async def test_tool_call_in_text_triggers_execution(self, hermes_router: LLMRouter) -> None:
        """LLM returns <tool_call> in text → router calls registry.execute."""
        responses = [
            {
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "text",
                        "text": '<tool_call>{"name": "search_mod", "arguments": {"mod_name": "SKSE"}}</tool_call>',
                    }
                ],
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Found 0 results for SKSE."}],
            },
        ]
        call_index = 0

        async def fake_chat(**kwargs: Any) -> dict[str, Any]:
            nonlocal call_index
            r = responses[call_index]
            call_index += 1
            return r

        hermes_router._provider.chat = fake_chat  # type: ignore[method-assign]
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        with patch.object(hermes_router._tools, "execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = '{"matches": []}'
            result = await hermes_router.chat("Search for SKSE", mock_session, chat_id="h-2")

        assert result == "Found 0 results for SKSE."
        mock_exec.assert_called_once_with("search_mod", {"mod_name": "SKSE"})


# ------------------------------------------------------------------
# Self-healing loop
# ------------------------------------------------------------------


class TestHermesSelfHealing:
    @pytest.mark.asyncio
    async def test_tool_error_injected_back_to_llm(self, hermes_router: LLMRouter) -> None:
        """On tool exception, error is injected as role=tool; LLM retries and succeeds."""
        responses = [
            {
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "text",
                        "text": '<tool_call>{"name": "run_loot_sort", "arguments": {"profile": "Broken"}}</tool_call>',
                    }
                ],
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Sorry, I could not sort. Please check the profile name."}],
            },
        ]
        call_index = 0

        async def fake_chat(**kwargs: Any) -> dict[str, Any]:
            nonlocal call_index
            r = responses[call_index]
            call_index += 1
            return r

        hermes_router._provider.chat = fake_chat  # type: ignore[method-assign]
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        with patch.object(hermes_router._tools, "execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = RuntimeError("LOOT runner not configured")
            result = await hermes_router.chat("Sort my load order", mock_session, chat_id="h-3")

        assert "Sorry" in result
        assert call_index == 2

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_returns_error_message(self, hermes_router: LLMRouter) -> None:
        """After MAX_HERMES_RETRIES consecutive failures, router returns an error string."""

        async def fake_chat(**kwargs: Any) -> dict[str, Any]:
            return {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '<tool_call>{"name": "close_game"}</tool_call>'}],
            }

        hermes_router._provider.chat = fake_chat  # type: ignore[method-assign]
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        with patch.object(hermes_router._tools, "execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = RuntimeError("always fails")
            result = await hermes_router.chat("Close game", mock_session, chat_id="h-4")

        assert "max" in result.lower() or "error" in result.lower()
        assert mock_exec.call_count == MAX_HERMES_RETRIES

    @pytest.mark.asyncio
    async def test_malformed_tool_call_json_injected_as_error(self, hermes_router: LLMRouter) -> None:
        """Malformed <tool_call> JSON is caught and fed back as a tool-role error."""
        responses = [
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "<tool_call>NOT JSON</tool_call>"}],
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Apologies, I made an error."}],
            },
        ]
        call_index = 0

        async def fake_chat(**kwargs: Any) -> dict[str, Any]:
            nonlocal call_index
            r = responses[call_index]
            call_index += 1
            return r

        hermes_router._provider.chat = fake_chat  # type: ignore[method-assign]
        mock_session = MagicMock(spec=aiohttp.ClientSession)
        result = await hermes_router.chat("Broken call", mock_session, chat_id="h-5")
        assert "Apologies" in result
        assert call_index == 2
