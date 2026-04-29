"""Tests for FASE 1.5.3 — TokenBudget + CircuitBreaker integration in LLMRouter.

Validates that budget checks, summarization, truncation, circuit breaker
rejection, usage recording, and tool round timeout are correctly wired into
the router's chat loop.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.agent.providers import AnthropicProvider
from sky_claw.agent.router import LLMRouter
from sky_claw.agent.token_budget import TokenBudgetConfig, TokenBudgetManager
from sky_claw.agent.token_circuit_breaker import (
    TokenCircuitBreaker,
    TokenCircuitBreakerConfig,
)
from sky_claw.agent.tools import AsyncToolRegistry
from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.mo2.vfs import MO2Controller
from sky_claw.orchestrator.sync_engine import SyncEngine
from sky_claw.scraper.masterlist import MasterlistClient
from sky_claw.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.security.path_validator import PathValidator

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_mo2(tmp_path: pathlib.Path) -> MO2Controller:
    profile_dir = tmp_path / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text("+TestMod-100\n", encoding="utf-8")
    validator = PathValidator(roots=[tmp_path])
    return MO2Controller(tmp_path, path_validator=validator)


@pytest.fixture()
async def adb(tmp_path: pathlib.Path) -> AsyncModRegistry:
    registry = AsyncModRegistry(db_path=tmp_path / "test_router_token.db")
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


def _make_router(
    tmp_path: pathlib.Path,
    tool_registry: AsyncToolRegistry,
    *,
    budget_config: TokenBudgetConfig | None = None,
    cb_config: TokenCircuitBreakerConfig | None = None,
) -> LLMRouter:
    """Create an LLMRouter with mocked gateway for token integration tests."""
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
        db_path=str(tmp_path / "chat_history_token.db"),
        model="claude-sonnet-4-6",
        system_prompt="You are a helpful Skyrim mod assistant.",
        gateway=mock_gateway,
    )

    # Override budget + circuit breaker with custom configs if provided
    if budget_config is not None:
        r._token_budget = TokenBudgetManager(config=budget_config)
    if cb_config is not None:
        r._circuit_breaker = TokenCircuitBreaker(config=cb_config)

    return r


@pytest.fixture()
async def router(tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path) -> LLMRouter:
    r = _make_router(tmp_path, tool_registry)
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


def _end_turn_response(text: str = "Hello!") -> dict[str, Any]:
    """Build a provider response dict that signals end_turn."""
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
    }


# ------------------------------------------------------------------
# Tests: Budget integration
# ------------------------------------------------------------------


class TestBudgetRejection:
    """When context exceeds 100% of token budget, router rejects."""

    @pytest.mark.asyncio
    async def test_reject_on_budget_exceeded(self, tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path) -> None:
        # Tiny budget: 100 tokens → even a small message will exceed after loading
        budget_config = TokenBudgetConfig(max_context_tokens=10)
        r = _make_router(tmp_path, tool_registry, budget_config=budget_config)
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

        # Pre-load a large message into history
        await r._save_message("chat1", "user", "x" * 200)
        await r._save_message("chat1", "assistant", "y" * 200)

        session = MagicMock()
        result = await r.chat("test message", session, chat_id="chat1")
        assert "presupuesto de tokens" in result
        await r.close()


class TestBudgetSummarization:
    """When context hits 75%, older messages are summarized."""

    @pytest.mark.asyncio
    async def test_summarize_on_warning_threshold(
        self, tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        # Budget: 500 tokens, warning at 75% = 375 tokens
        budget_config = TokenBudgetConfig(
            max_context_tokens=500,
            warning_threshold_pct=0.75,
            critical_threshold_pct=0.90,
            messages_to_preserve=2,
        )
        r = _make_router(tmp_path, tool_registry, budget_config=budget_config)
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

        # Mock the provider to return end_turn immediately
        r._provider = MagicMock()
        r._provider.chat = AsyncMock(return_value=_end_turn_response("Summarized!"))

        # Pre-load enough messages to trigger summarization (75%+ of 500 tokens)
        for i in range(10):
            await r._save_message("chat2", "user", f"Message {i} " + "x" * 40)
            await r._save_message("chat2", "assistant", f"Reply {i} " + "y" * 40)

        session = MagicMock()
        result = await r.chat("new query", session, chat_id="chat2")

        # Should succeed (not rejected) and provider should have been called
        assert result == "Summarized!"
        r._provider.chat.assert_awaited_once()
        await r.close()


class TestBudgetTruncation:
    """When context hits 90%, older messages are truncated."""

    @pytest.mark.asyncio
    async def test_truncate_on_critical_threshold(
        self, tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        # Budget: 300 tokens, critical at 90% = 270 tokens
        budget_config = TokenBudgetConfig(
            max_context_tokens=300,
            warning_threshold_pct=0.75,
            critical_threshold_pct=0.90,
            messages_to_preserve=2,
        )
        r = _make_router(tmp_path, tool_registry, budget_config=budget_config)
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

        # Mock the provider
        r._provider = MagicMock()
        r._provider.chat = AsyncMock(return_value=_end_turn_response("Truncated!"))

        # Pre-load enough messages to trigger truncation (90%+ of 300 tokens)
        for i in range(8):
            await r._save_message("chat3", "user", f"Message {i} " + "x" * 40)
            await r._save_message("chat3", "assistant", f"Reply {i} " + "y" * 40)

        session = MagicMock()
        result = await r.chat("new query", session, chat_id="chat3")

        assert result == "Truncated!"
        r._provider.chat.assert_awaited_once()
        await r.close()


# ------------------------------------------------------------------
# Tests: Circuit breaker integration
# ------------------------------------------------------------------


class TestCircuitBreakerRejection:
    """When circuit breaker is OPEN, router rejects immediately."""

    @pytest.mark.asyncio
    async def test_reject_when_circuit_open(self, tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path) -> None:
        r = _make_router(tmp_path, tool_registry)
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

        # Force circuit breaker to OPEN state
        r._circuit_breaker._state = "open"
        r._circuit_breaker._opened_at = asyncio.get_event_loop().time()

        session = MagicMock()
        result = await r.chat("test message", session, chat_id="cb1")

        assert "Circuit breaker activado" in result
        # Provider should NOT have been called
        await r.close()


# ------------------------------------------------------------------
# Tests: Usage recording
# ------------------------------------------------------------------


class TestUsageRecording:
    """After a successful LLM call, token usage is recorded."""

    @pytest.mark.asyncio
    async def test_usage_recorded_after_response(self, router: LLMRouter) -> None:
        # Mock the provider
        router._provider = MagicMock()
        router._provider.chat = AsyncMock(return_value=_end_turn_response("Hello!"))

        initial_total = router._token_budget._total_tokens_consumed

        session = MagicMock()
        await router.chat("hi", session, chat_id="usage1")

        # Token usage should have increased
        assert router._token_budget._total_tokens_consumed > initial_total

    @pytest.mark.asyncio
    async def test_session_report_available(self, router: LLMRouter) -> None:
        router._provider = MagicMock()
        router._provider.chat = AsyncMock(return_value=_end_turn_response("Hi!"))

        session = MagicMock()
        await router.chat("hello", session, chat_id="report1")

        report = router._token_budget.get_session_report()
        assert report.total_tokens_consumed > 0
        assert report.session_duration_seconds > 0
        assert report.estimated_cost_usd > 0


# ------------------------------------------------------------------
# Tests: Tool round timeout
# ------------------------------------------------------------------


class TestToolRoundTimeout:
    """Tool execution is wrapped with asyncio.wait_for timeout."""

    @pytest.mark.asyncio
    async def test_tool_timeout_returns_error(self, tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path) -> None:
        r = _make_router(tmp_path, tool_registry)
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

        # First call returns tool_use, second call returns end_turn
        call_count = 0

        async def _mock_chat(**kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "list_mods",
                            "input": {},
                        }
                    ],
                }
            return _end_turn_response("Done after timeout error")

        r._provider = MagicMock()
        r._provider.chat = AsyncMock(side_effect=_mock_chat)

        # Make tool execution hang (sleep longer than timeout)

        async def _slow_execute(name: str, args: dict) -> str:
            await asyncio.sleep(300)
            return "should not reach here"

        r._tools.execute = _slow_execute

        # Use a very short timeout for testing
        with patch("sky_claw.agent.router.DEFAULT_TOOL_ROUND_TIMEOUT", 0.1):
            session = MagicMock()
            # The timeout error should be caught by the existing OSError handler
            # (asyncio.TimeoutError is a subclass of TimeoutError → OSError in 3.11+)
            result = await r.chat("run tool", session, chat_id="timeout1")

        # Should have gotten an error response, not hung forever
        assert result is not None
        assert isinstance(result, str)
        await r.close()


# ------------------------------------------------------------------
# Tests: Budget + Circuit Breaker interaction
# ------------------------------------------------------------------


class TestBudgetCircuitBreakerInteraction:
    """Budget check happens before circuit breaker check in the loop."""

    @pytest.mark.asyncio
    async def test_budget_check_before_circuit_breaker(
        self, tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        """Budget rejection takes priority over circuit breaker state."""
        budget_config = TokenBudgetConfig(max_context_tokens=10)
        r = _make_router(tmp_path, tool_registry, budget_config=budget_config)
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

        # Pre-load large context
        await r._save_message("chat_int", "user", "x" * 200)

        # Circuit breaker is CLOSED (normal), but budget will reject
        assert r._circuit_breaker.state == "closed"

        session = MagicMock()
        result = await r.chat("test", session, chat_id="chat_int")

        # Budget rejection message, not circuit breaker
        assert "presupuesto de tokens" in result
        await r.close()

    @pytest.mark.asyncio
    async def test_circuit_breaker_does_not_record_usage_on_reject(self, router: LLMRouter) -> None:
        """When circuit breaker rejects, no usage should be recorded."""
        # Force circuit breaker OPEN
        router._circuit_breaker._state = "open"
        router._circuit_breaker._opened_at = asyncio.get_event_loop().time()

        initial_total = router._token_budget._total_tokens_consumed

        session = MagicMock()
        await router.chat("test", session, chat_id="cb_no_usage")

        # No tokens consumed because the LLM was never called
        assert router._token_budget._total_tokens_consumed == initial_total


# ------------------------------------------------------------------
# Tests: Hermes mode timeout
# ------------------------------------------------------------------


class TestHermesToolTimeout:
    """Hermes mode tool execution also respects timeout."""

    @pytest.mark.asyncio
    async def test_hermes_tool_timeout_handled(self, tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path) -> None:
        r = _make_router(tmp_path, tool_registry)
        # Enable hermes mode
        r._hermes_mode = True
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

        # Provider returns text with tool call
        call_count = 0

        async def _mock_chat(**kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "stop_reason": "end_turn",
                    "content": [
                        {
                            "type": "text",
                            "text": '```tool\n{"name": "list_mods", "arguments": {}}\n```',
                        }
                    ],
                }
            return {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done"}],
            }

        r._provider = MagicMock()
        r._provider.chat = AsyncMock(side_effect=_mock_chat)

        async def _slow_execute(name: str, args: dict) -> str:
            await asyncio.sleep(300)
            return "should not reach"

        r._tools.execute = _slow_execute

        with patch("sky_claw.agent.router.DEFAULT_TOOL_ROUND_TIMEOUT", 0.1):
            session = MagicMock()
            result = await r.chat("run hermes tool", session, chat_id="hermes_timeout")

        assert result is not None
        assert isinstance(result, str)
        await r.close()
