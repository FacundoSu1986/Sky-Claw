"""Tests for HitlGateMiddleware — FASE 1.5.1.

Validates:
- Destructive tools require approval
- Non-destructive tools bypass gate
- Approval timeout → auto-deny
- Human denial → blocked execution
- Human approval → execution proceeds
- Fail-open when no notify_fn configured
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sky_claw.orchestrator.tool_strategies.middleware import (
    DESTRUCTIVE_TOOL_PATTERNS,
    HitlGateMiddleware,
)

# ---------------------------------------------------------------------------
# Stubs
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
# Tests
# ---------------------------------------------------------------------------


class TestHitlGate:
    @pytest.mark.asyncio
    async def test_non_destructive_tool_bypasses_gate(self) -> None:
        gate = HitlGateMiddleware(timeout=5.0)
        strategy = _FakeStrategy("query_mod_metadata")
        result = await gate(strategy, {}, _noop_next)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_destructive_tool_fail_open_without_notify(self) -> None:
        """Without notify_fn, destructive tools proceed with a warning."""
        gate = HitlGateMiddleware(timeout=5.0)
        strategy = _FakeStrategy("execute_loot_sorting")
        result = await gate(strategy, {}, _noop_next)
        assert result["status"] == "ok"
        assert result["executed"] is True

    @pytest.mark.asyncio
    async def test_destructive_tool_approved(self) -> None:
        notify_fn = AsyncMock()
        gate = HitlGateMiddleware(notify_fn=notify_fn, timeout=5.0)
        strategy = _FakeStrategy("generate_lods")

        # Simulate human approval in background — capturar el request_id del
        # payload de notify_fn (FASE 1.5.4: clave única por request, no por tool).
        async def _approve():
            await asyncio.sleep(0.05)
            request_id = notify_fn.call_args.args[0]["request_id"]
            gate.resolve(request_id, approved=True)

        asyncio.create_task(_approve())
        result = await gate(strategy, {}, _noop_next)

        assert notify_fn.called
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_destructive_tool_denied(self) -> None:
        notify_fn = AsyncMock()
        gate = HitlGateMiddleware(notify_fn=notify_fn, timeout=5.0)
        strategy = _FakeStrategy("generate_bashed_patch")

        # Simulate human denial in background
        async def _deny():
            await asyncio.sleep(0.05)
            request_id = notify_fn.call_args.args[0]["request_id"]
            gate.resolve(request_id, approved=False)

        asyncio.create_task(_deny())
        result = await gate(strategy, {}, _noop_next)

        assert result["status"] == "error"
        assert result["reason"] == "HITLApprovalDenied"

    @pytest.mark.asyncio
    async def test_destructive_tool_timeout(self) -> None:
        notify_fn = AsyncMock()
        gate = HitlGateMiddleware(notify_fn=notify_fn, timeout=0.1)
        strategy = _FakeStrategy("resolve_conflict_patch")

        result = await gate(strategy, {}, _noop_next)

        assert result["status"] == "error"
        assert result["reason"] == "HITLApprovalTimeout"

    @pytest.mark.asyncio
    async def test_custom_destructive_tools(self) -> None:
        custom = frozenset({"custom_destructive_tool"})
        gate = HitlGateMiddleware(destructive_tools=custom, timeout=0.1)

        # Standard destructive tool should bypass with custom set
        strategy = _FakeStrategy("execute_loot_sorting")
        result = await gate(strategy, {}, _noop_next)
        assert result["status"] == "ok"

        # Custom destructive tool should be gated
        strategy2 = _FakeStrategy("custom_destructive_tool")
        result2 = await gate(strategy2, {}, _noop_next)
        assert result2["status"] == "ok"  # fail-open without notify_fn


class TestHitlConcurrentRequests:
    """FASE 1.5.4 hardening: previene colisión cuando dos invocaciones
    concurrentes de la MISMA tool destructiva con payloads distintos llegan
    al gate simultáneamente."""

    @pytest.mark.asyncio
    async def test_concurrent_same_tool_different_payloads_independent_decisions(self) -> None:
        """Dos invocaciones concurrentes de la misma tool con payloads distintos
        deben tener entries pendientes independientes; resolver una NO debe
        afectar a la otra."""
        captured_ids: list[str] = []

        async def capturing_notify(payload: dict[str, Any]) -> None:
            captured_ids.append(payload["request_id"])

        gate = HitlGateMiddleware(notify_fn=capturing_notify, timeout=5.0)
        strategy = _FakeStrategy("generate_lods")

        # Lanzar dos invocaciones concurrentes de la misma tool
        task_a = asyncio.create_task(gate(strategy, {"target": "A"}, _noop_next))
        task_b = asyncio.create_task(gate(strategy, {"target": "B"}, _noop_next))

        # Esperar a que ambas notifiquen
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(captured_ids) >= 2:
                break

        assert len(captured_ids) == 2, "Ambas invocaciones deben haber notificado"
        assert captured_ids[0] != captured_ids[1], "Cada invocación debe tener un request_id único"

        # Aprobar SOLO la primera; la segunda debe seguir pending
        gate.resolve(captured_ids[0], approved=True)
        result_a = await asyncio.wait_for(task_a, timeout=1.0)
        assert result_a["status"] == "ok"
        assert not task_b.done(), "task_b NO debe completarse al resolver task_a"

        # Denegar la segunda
        gate.resolve(captured_ids[1], approved=False)
        result_b = await asyncio.wait_for(task_b, timeout=1.0)
        assert result_b["status"] == "error"
        assert result_b["reason"] == "HITLApprovalDenied"


class TestDestructiveToolPatterns:
    def test_known_destructive_tools(self) -> None:
        expected = {
            "execute_loot_sorting",
            "generate_bashed_patch",
            "generate_lods",
            "resolve_conflict_patch",
        }
        assert expected == DESTRUCTIVE_TOOL_PATTERNS

    def test_query_mod_metadata_not_destructive(self) -> None:
        assert "query_mod_metadata" not in DESTRUCTIVE_TOOL_PATTERNS

    def test_validate_plugin_limit_not_destructive(self) -> None:
        assert "validate_plugin_limit" not in DESTRUCTIVE_TOOL_PATTERNS
