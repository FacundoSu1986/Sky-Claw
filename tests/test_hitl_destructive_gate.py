"""Tests for HitlGateMiddleware — fail-closed redesign backed by HITLGuard.

Validates:
- Fail-closed default: without a guard, destructive tools are DENIED
  (``HITLGateUnavailable``) and never executed.
- ``allow_unattended=True`` is the only bypass (explicit, CRITICAL-logged).
- Destructive tools require approval through a real ``HITLGuard``:
  approve → executes; deny → blocked; guard timeout → blocked (fail-secure).
- Requests carry ``category="tool_execution"`` so the AppContext notify
  closure never auto-approves them.
- Non-destructive tools always pass through.
- Concurrent invocations of the same tool resolve independently.
- The dispatcher wires the shared gate on exactly the destructive tools.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.orchestrator.tool_strategies.middleware import (
    DESTRUCTIVE_TOOL_PATTERNS,
    HitlGateMiddleware,
)
from sky_claw.antigravity.security.hitl import HITLGuard

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _FakeStrategy:
    """Minimal strategy stub for testing."""

    def __init__(self, name: str) -> None:
        self.name = name


def _make_next() -> tuple[Any, list[bool]]:
    """Inner-chain stub that records whether it was executed."""
    calls: list[bool] = []

    async def _next() -> dict[str, Any]:
        calls.append(True)
        return {"status": "ok", "executed": True}

    return _next, calls


def _make_guard(timeout: int = 5) -> tuple[HITLGuard, list[Any], asyncio.Event]:
    """HITLGuard whose notify_fn captures each HITLRequest as it registers."""
    captured: list[Any] = []
    registered = asyncio.Event()

    async def _notify(req: Any) -> None:
        captured.append(req)
        registered.set()

    return HITLGuard(notify_fn=_notify, timeout=timeout), captured, registered


# ---------------------------------------------------------------------------
# Fail-closed default (no guard)
# ---------------------------------------------------------------------------


class TestFailClosedDefault:
    @pytest.mark.asyncio
    async def test_destructive_tool_denied_without_guard(self) -> None:
        """Default gate (no HITLGuard) must DENY destructive tools, not proceed."""
        gate = HitlGateMiddleware()
        next_call, calls = _make_next()

        result = await gate(_FakeStrategy("execute_loot_sorting"), {}, next_call)

        assert result["status"] == "error"
        assert result["reason"] == "HITLGateUnavailable"
        assert calls == [], "next_call must never run without approval"

    @pytest.mark.asyncio
    async def test_non_destructive_tool_bypasses_gate(self) -> None:
        gate = HitlGateMiddleware()
        next_call, calls = _make_next()

        result = await gate(_FakeStrategy("query_mod_metadata"), {}, next_call)

        assert result["status"] == "ok"
        assert calls == [True]

    @pytest.mark.asyncio
    async def test_allow_unattended_bypasses_with_critical_log(self, caplog) -> None:
        gate = HitlGateMiddleware(allow_unattended=True)
        next_call, calls = _make_next()

        with caplog.at_level(logging.CRITICAL):
            result = await gate(_FakeStrategy("generate_lods"), {}, next_call)

        assert result["status"] == "ok"
        assert calls == [True]
        assert any(record.levelno == logging.CRITICAL for record in caplog.records)

    @pytest.mark.asyncio
    async def test_custom_destructive_tools(self) -> None:
        custom = frozenset({"custom_destructive_tool"})
        gate = HitlGateMiddleware(destructive_tools=custom)

        # Standard destructive tool bypasses when a custom set is given.
        next_a, calls_a = _make_next()
        result = await gate(_FakeStrategy("execute_loot_sorting"), {}, next_a)
        assert result["status"] == "ok"
        assert calls_a == [True]

        # The custom tool is gated → fail-closed without guard.
        next_b, calls_b = _make_next()
        result2 = await gate(_FakeStrategy("custom_destructive_tool"), {}, next_b)
        assert result2["status"] == "error"
        assert result2["reason"] == "HITLGateUnavailable"
        assert calls_b == []


# ---------------------------------------------------------------------------
# Guard-backed approval flow
# ---------------------------------------------------------------------------


class TestGuardBackedGate:
    @pytest.mark.asyncio
    async def test_destructive_tool_approved_executes(self) -> None:
        guard, captured, registered = _make_guard()
        gate = HitlGateMiddleware(hitl_guard=guard)
        next_call, calls = _make_next()

        async def _approve() -> None:
            await asyncio.wait_for(registered.wait(), timeout=2.0)
            await guard.respond(captured[0].request_id, approved=True)

        approve_task = asyncio.create_task(_approve())
        result = await gate(_FakeStrategy("generate_lods"), {"output": "x"}, next_call)
        await approve_task

        assert result["status"] == "ok"
        assert calls == [True]

    @pytest.mark.asyncio
    async def test_request_carries_tool_execution_category(self) -> None:
        """The notify closure must be able to distinguish tool approvals."""
        guard, captured, registered = _make_guard()
        gate = HitlGateMiddleware(hitl_guard=guard)
        next_call, _calls = _make_next()

        async def _approve() -> None:
            await asyncio.wait_for(registered.wait(), timeout=2.0)
            await guard.respond(captured[0].request_id, approved=True)

        approve_task = asyncio.create_task(_approve())
        await gate(_FakeStrategy("generate_bashed_patch"), {}, next_call)
        await approve_task

        req = captured[0]
        assert req.category == "tool_execution"
        assert "generate_bashed_patch" in req.reason

    @pytest.mark.asyncio
    async def test_destructive_tool_denied_blocks(self) -> None:
        guard, captured, registered = _make_guard()
        gate = HitlGateMiddleware(hitl_guard=guard)
        next_call, calls = _make_next()

        async def _deny() -> None:
            await asyncio.wait_for(registered.wait(), timeout=2.0)
            await guard.respond(captured[0].request_id, approved=False)

        deny_task = asyncio.create_task(_deny())
        result = await gate(_FakeStrategy("generate_bashed_patch"), {}, next_call)
        await deny_task

        assert result["status"] == "error"
        assert result["reason"] == "HITLApprovalDenied"
        assert calls == []

    @pytest.mark.asyncio
    async def test_guard_timeout_blocks(self) -> None:
        """Guard timeout auto-denies (fail-secure) → tool never runs."""
        guard = HITLGuard(notify_fn=AsyncMock(), timeout=0)
        gate = HitlGateMiddleware(hitl_guard=guard)
        next_call, calls = _make_next()

        result = await gate(_FakeStrategy("resolve_conflict_with_patch"), {}, next_call)

        assert result["status"] == "error"
        assert result["reason"] == "HITLApprovalDenied"
        assert calls == []


# ---------------------------------------------------------------------------
# Concurrency — FASE 1.5.4 hardening preserved under the HITLGuard backend
# ---------------------------------------------------------------------------


class TestHitlConcurrentRequests:
    @pytest.mark.asyncio
    async def test_concurrent_same_tool_different_payloads_independent_decisions(self) -> None:
        """Dos invocaciones concurrentes de la misma tool con payloads distintos
        deben tener requests pendientes independientes; resolver una NO debe
        afectar a la otra."""
        captured: list[Any] = []

        async def _notify(req: Any) -> None:
            captured.append(req)

        guard = HITLGuard(notify_fn=_notify, timeout=5)
        gate = HitlGateMiddleware(hitl_guard=guard)
        strategy = _FakeStrategy("generate_lods")
        next_a, _calls_a = _make_next()
        next_b, _calls_b = _make_next()

        task_a = asyncio.create_task(gate(strategy, {"target": "A"}, next_a))
        task_b = asyncio.create_task(gate(strategy, {"target": "B"}, next_b))

        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(captured) >= 2:
                break

        assert len(captured) == 2, "Ambas invocaciones deben haber notificado"
        ids = [req.request_id for req in captured]
        assert ids[0] != ids[1], "Cada invocación debe tener un request_id único"

        # Aprobar SOLO la primera; la segunda debe seguir pending
        await guard.respond(ids[0], approved=True)
        result_a = await asyncio.wait_for(task_a, timeout=2.0)
        assert result_a["status"] == "ok"
        assert not task_b.done(), "task_b NO debe completarse al resolver task_a"

        # Denegar la segunda
        await guard.respond(ids[1], approved=False)
        result_b = await asyncio.wait_for(task_b, timeout=2.0)
        assert result_b["status"] == "error"
        assert result_b["reason"] == "HITLApprovalDenied"


# ---------------------------------------------------------------------------
# Dispatcher wiring — the shared gate covers exactly the destructive tools
# ---------------------------------------------------------------------------


class TestDispatcherGateWiring:
    @staticmethod
    def _make_supervisor() -> Any:
        from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent

        sup = SupervisorAgent.__new__(SupervisorAgent)
        sup.scraper = MagicMock()
        sup.tools = MagicMock()
        sup.interface = MagicMock()
        sup._loot_service = MagicMock()
        sup._synthesis_service = MagicMock()
        sup._xedit_service = MagicMock()
        sup._dyndolod_service = MagicMock()
        sup.profile_name = "TestProfile"
        return sup

    def test_gate_wraps_exactly_the_destructive_tools(self) -> None:
        from sky_claw.antigravity.orchestrator.tool_dispatcher import (
            build_orchestration_dispatcher,
        )

        sup = self._make_supervisor()
        gate = HitlGateMiddleware(allow_unattended=True)
        dispatcher = build_orchestration_dispatcher(sup, hitl_gate=gate)

        gated = {name for name, chain in dispatcher._middleware.items() if any(mw is gate for mw in chain)}
        assert gated == set(DESTRUCTIVE_TOOL_PATTERNS)


# ---------------------------------------------------------------------------
# Destructive tool patterns (pinned)
# ---------------------------------------------------------------------------


class TestDestructiveToolPatterns:
    def test_known_destructive_tools(self) -> None:
        expected = {
            "execute_loot_sorting",
            "generate_bashed_patch",
            "generate_lods",
            "resolve_conflict_with_patch",
        }
        assert expected == DESTRUCTIVE_TOOL_PATTERNS

    def test_query_mod_metadata_not_destructive(self) -> None:
        assert "query_mod_metadata" not in DESTRUCTIVE_TOOL_PATTERNS

    def test_validate_plugin_limit_not_destructive(self) -> None:
        assert "validate_plugin_limit" not in DESTRUCTIVE_TOOL_PATTERNS
