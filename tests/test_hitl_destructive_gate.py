"""Tests for HitlGateMiddleware — FASE 1.5.1.

Validates:
- Destructive tools require approval
- Non-destructive tools bypass gate
- Approval timeout → auto-deny
- Human denial → blocked execution
- Human approval → execution proceeds
- Fail-open when no notify_fn configured

NOTE: HitlGateMiddleware not yet implemented per middleware.py comments (YAGNI).
Will be added when a SECOND HITL use site appears. Tests are skipped but
preserved for future implementation.
"""

from __future__ import annotations

import asyncio  # noqa: F401 - used when HitlGateMiddleware is implemented
from typing import Any  # noqa: F401
from unittest.mock import AsyncMock  # noqa: F401 - used when HitlGateMiddleware is implemented

import pytest

# NOTE: HitlGateMiddleware not yet implemented per middleware.py comments (YAGNI).
# Will be added when a SECOND HITL use site appears.
# from sky_claw.orchestrator.tool_strategies.middleware import (
#     DESTRUCTIVE_TOOL_PATTERNS,
#     HitlGateMiddleware,
# )

# ---------------------------------------------------------------------------
# Stubs (for future use when HitlGateMiddleware is implemented)
# ---------------------------------------------------------------------------


class _FakeStrategy:
    """Minimal strategy stub for testing."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "tool": self.name}


async def _noop_next() -> dict[str, Any]:
    return {"status": "ok", "executed": True}


# ---------------------------------------------------------------------------
# Tests (all skipped - waiting for HitlGateMiddleware implementation)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="HitlGateMiddleware not yet implemented (YAGNI). See middleware.py.")
class TestHitlGate:
    """Test suite for HitlGateMiddleware (pending implementation)."""

    @pytest.mark.asyncio
    async def test_non_destructive_tool_bypasses_gate(self) -> None:
        """Non-destructive tools should bypass the HITL gate."""
        # from sky_claw.orchestrator.tool_strategies.middleware import HitlGateMiddleware
        # gate = HitlGateMiddleware(timeout=5.0)
        # strategy = _FakeStrategy("query_mod_metadata")
        # result = await gate(strategy, {}, _noop_next)
        # assert result["status"] == "ok"
        pass

    @pytest.mark.asyncio
    async def test_destructive_tool_fail_open_without_notify(self) -> None:
        """Without notify_fn, destructive tools proceed with a warning."""
        # from sky_claw.orchestrator.tool_strategies.middleware import HitlGateMiddleware
        # gate = HitlGateMiddleware(timeout=5.0)
        # strategy = _FakeStrategy("execute_loot_sorting")
        # result = await gate(strategy, {}, _noop_next)
        # assert result["status"] == "ok"
        # assert result["executed"] is True
        pass

    @pytest.mark.asyncio
    async def test_destructive_tool_approved(self) -> None:
        """Destructive tools should proceed when explicitly approved."""
        # from sky_claw.orchestrator.tool_strategies.middleware import HitlGateMiddleware
        # notify_fn = AsyncMock()
        # gate = HitlGateMiddleware(notify_fn=notify_fn, timeout=5.0)
        # strategy = _FakeStrategy("generate_lods")
        #
        # # Simulate human approval in background
        # async def _approve():
        #     await asyncio.sleep(0.05)
        #     gate.resolve("generate_lods", approved=True)
        #
        # asyncio.create_task(_approve())
        # result = await gate(strategy, {}, _noop_next)
        #
        # assert notify_fn.called
        # assert result["status"] == "ok"
        pass

    @pytest.mark.asyncio
    async def test_destructive_tool_denied(self) -> None:
        """Destructive tools should be blocked when explicitly denied."""
        # from sky_claw.orchestrator.tool_strategies.middleware import HitlGateMiddleware
        # notify_fn = AsyncMock()
        # gate = HitlGateMiddleware(notify_fn=notify_fn, timeout=5.0)
        # strategy = _FakeStrategy("generate_bashed_patch")
        #
        # # Simulate human denial in background
        # async def _deny():
        #     await asyncio.sleep(0.05)
        #     gate.resolve("generate_bashed_patch", approved=False)
        #
        # asyncio.create_task(_deny())
        # result = await gate(strategy, {}, _noop_next)
        #
        # assert result["status"] == "error"
        # assert result["reason"] == "HITLApprovalDenied"
        pass

    @pytest.mark.asyncio
    async def test_destructive_tool_timeout(self) -> None:
        """Destructive tools should timeout if approval not provided within window."""
        # from sky_claw.orchestrator.tool_strategies.middleware import HitlGateMiddleware
        # notify_fn = AsyncMock()
        # gate = HitlGateMiddleware(notify_fn=notify_fn, timeout=0.1)
        # strategy = _FakeStrategy("resolve_conflict_patch")
        #
        # result = await gate(strategy, {}, _noop_next)
        #
        # assert result["status"] == "error"
        # assert result["reason"] == "HITLApprovalTimeout"
        pass

    @pytest.mark.asyncio
    async def test_custom_destructive_tools(self) -> None:
        """Custom destructive tool patterns should be configurable."""
        # from sky_claw.orchestrator.tool_strategies.middleware import HitlGateMiddleware
        # custom = frozenset({"custom_destructive_tool"})
        # gate = HitlGateMiddleware(destructive_tools=custom, timeout=0.1)
        #
        # # Standard destructive tool should bypass with custom set
        # strategy = _FakeStrategy("execute_loot_sorting")
        # result = await gate(strategy, {}, _noop_next)
        # assert result["status"] == "ok"
        #
        # # Custom destructive tool should be gated
        # strategy2 = _FakeStrategy("custom_destructive_tool")
        # result2 = await gate(strategy2, {}, _noop_next)
        # assert result2["status"] == "ok"  # fail-open without notify_fn
        pass


@pytest.mark.skip(reason="HitlGateMiddleware not yet implemented (YAGNI). See middleware.py.")
class TestDestructiveToolPatterns:
    """Test suite for destructive tool pattern detection (pending HitlGateMiddleware)."""

    def test_known_destructive_tools(self) -> None:
        """DESTRUCTIVE_TOOL_PATTERNS should contain expected tools."""
        # from sky_claw.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS
        # expected = {
        #     "execute_loot_sorting",
        #     "generate_bashed_patch",
        #     "generate_lods",
        #     "resolve_conflict_patch",
        # }
        # assert expected == DESTRUCTIVE_TOOL_PATTERNS
        pass

    def test_query_mod_metadata_not_destructive(self) -> None:
        """query_mod_metadata should not be classified as destructive."""
        # from sky_claw.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS
        # assert "query_mod_metadata" not in DESTRUCTIVE_TOOL_PATTERNS
        pass

    def test_validate_plugin_limit_not_destructive(self) -> None:
        """validate_plugin_limit should not be classified as destructive."""
        # from sky_claw.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS
        # assert "validate_plugin_limit" not in DESTRUCTIVE_TOOL_PATTERNS
        pass
