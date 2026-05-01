"""Tests for sky_claw.antigravity.agent.router."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.antigravity.agent.providers import AnthropicProvider
from sky_claw.antigravity.agent.router import MAX_CONTEXT_MESSAGES, MAX_TOOL_ROUNDS, LLMRouter
from sky_claw.antigravity.agent.tools import AsyncToolRegistry
from sky_claw.antigravity.db.async_registry import AsyncModRegistry
from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine
from sky_claw.antigravity.scraper.masterlist import MasterlistClient
from sky_claw.antigravity.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.mo2.vfs import MO2Controller

if TYPE_CHECKING:
    import pathlib

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
    registry = AsyncModRegistry(db_path=tmp_path / "test_router.db")
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
async def router(tool_registry: AsyncToolRegistry, tmp_path: pathlib.Path) -> LLMRouter:
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
        db_path=str(tmp_path / "chat_history.db"),
        model="claude-sonnet-4-6",
        system_prompt="You are a helpful Skyrim mod assistant.",
        gateway=mock_gateway,
    )
    await r.open()
    # LLMRouter.chat() calls self._semantic_router.route() but SemanticRouter
    # only exposes classify(). Mock route() on the instance so the router can
    # call it without AttributeError.
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
# History schema
# ------------------------------------------------------------------


class TestHistorySchema:
    @pytest.mark.asyncio
    async def test_schema_created(self, router: LLMRouter) -> None:
        assert router._conn is not None
        async with router._conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            tables = {row[0] for row in await cur.fetchall()}
        assert "chat_history" in tables


# ------------------------------------------------------------------
# Message persistence
# ------------------------------------------------------------------


class TestMessagePersistence:
    @pytest.mark.asyncio
    async def test_save_and_load(self, router: LLMRouter) -> None:
        await router._save_message("chat-1", "user", "hello")
        await router._save_message("chat-1", "assistant", "hi there")
        messages = await router._load_context("chat-1")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_sliding_window(self, router: LLMRouter) -> None:
        for i in range(30):
            await router._save_message("chat-w", "user", f"msg-{i}")
        messages = await router._load_context("chat-w")
        assert len(messages) == MAX_CONTEXT_MESSAGES

    @pytest.mark.asyncio
    async def test_separate_chats_isolated(self, router: LLMRouter) -> None:
        await router._save_message("chat-a", "user", "msg-a")
        await router._save_message("chat-b", "user", "msg-b")
        a = await router._load_context("chat-a")
        b = await router._load_context("chat-b")
        assert len(a) == 1
        assert len(b) == 1
        assert a[0]["content"] == "msg-a"
        assert b[0]["content"] == "msg-b"


# ------------------------------------------------------------------
# Chat end-turn flow (mocked API)
# ------------------------------------------------------------------


class TestChatEndTurn:
    @pytest.mark.asyncio
    async def test_simple_end_turn(self, router: LLMRouter) -> None:
        api_response = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Hello!"}],
        }
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=api_response)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="")

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        router._gateway.request = AsyncMock(return_value=mock_cm)
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        result = await router.chat("Hi there", mock_session, chat_id="test-1")
        assert result == "Hello!"

    @pytest.mark.asyncio
    async def test_tool_use_then_end_turn(self, router: LLMRouter) -> None:
        # First API call returns tool_use
        tool_use_response = {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-123",
                    "name": "search_mod",
                    "input": {"mod_name": "SKSE"},
                },
            ],
        }
        # Second API call returns end_turn
        end_turn_response = {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "No mods found matching SKSE."},
            ],
        }

        call_count = 0

        async def fake_json() -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return tool_use_response
            return end_turn_response

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = fake_json
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="")

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        router._gateway.request = AsyncMock(return_value=mock_cm)
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        result = await router.chat("Search for SKSE", mock_session, chat_id="test-2")
        assert result == "No mods found matching SKSE."
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_tool_error_propagated(self, router: LLMRouter) -> None:
        # API requests a tool that doesn't exist
        tool_use_response = {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-err",
                    "name": "nonexistent_tool",
                    "input": {},
                },
            ],
        }
        end_turn_response = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Tool failed."}],
        }

        call_count = 0

        async def fake_json() -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return tool_use_response
            return end_turn_response

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = fake_json
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="")

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        router._gateway.request = AsyncMock(return_value=mock_cm)
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        result = await router.chat("Run nonexistent tool", mock_session, chat_id="test-err")
        assert result == "Tool failed."
        # The error should have been caught and sent as tool_result
        assert call_count == 2


# ------------------------------------------------------------------
# Context structured content roundtrip
# ------------------------------------------------------------------


class TestStructuredContent:
    @pytest.mark.asyncio
    async def test_tool_result_roundtrip(self, router: LLMRouter) -> None:
        blocks = [{"type": "tool_result", "tool_use_id": "abc", "content": "{}"}]
        await router._save_message("chat-s", "user", json.dumps(blocks))
        messages = await router._load_context("chat-s")
        assert len(messages) == 1
        assert isinstance(messages[0]["content"], list)
        assert messages[0]["content"][0]["type"] == "tool_result"


# ------------------------------------------------------------------
# MAX_TOOL_ROUNDS exceeded
# ------------------------------------------------------------------


class TestMaxToolRounds:
    @pytest.mark.asyncio
    async def test_exceeds_max_rounds_raises(self, router: LLMRouter) -> None:
        """Mock API always returns tool_use → router must raise after MAX_TOOL_ROUNDS."""
        tool_use_response = {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-loop",
                    "name": "search_mod",
                    "input": {"mod_name": "SKSE"},
                },
            ],
        }

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=tool_use_response)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="")

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        router._gateway.request = AsyncMock(return_value=mock_cm)
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        with (
            pytest.raises(RuntimeError, match=f"exceeded {MAX_TOOL_ROUNDS}"),
            patch.object(router._tools, "execute", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = '{"matches": []}'
            await router.chat("loop forever", mock_session, chat_id="test-loop")


# ------------------------------------------------------------------
# Retry on 429
# ------------------------------------------------------------------


class TestRetry429:
    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self, router: LLMRouter) -> None:
        """First call returns 429, second succeeds → chat returns normally."""
        call_count = 0

        def fake_gateway_ctx(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1

            mock_resp = AsyncMock()
            if call_count == 1:
                # Simulate 429
                mock_resp.status = 429
                mock_resp.raise_for_status = MagicMock(
                    side_effect=aiohttp.ClientResponseError(
                        request_info=MagicMock(),
                        history=(),
                        status=429,
                        message="Too Many Requests",
                    )
                )
            else:
                mock_resp.status = 200
                mock_resp.json = AsyncMock(
                    return_value={
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "OK after retry"}],
                    }
                )
                mock_resp.raise_for_status = MagicMock()
                mock_resp.text = AsyncMock(return_value="")

            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=mock_resp)
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

        router._gateway.request = AsyncMock(side_effect=fake_gateway_ctx)
        mock_session = MagicMock(spec=aiohttp.ClientSession)

        result = await router.chat("retry test", mock_session, chat_id="test-429")
        assert result == "OK after retry"
        assert call_count == 2
