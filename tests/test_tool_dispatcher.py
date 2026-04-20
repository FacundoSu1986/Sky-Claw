"""Unit tests for OrchestrationToolDispatcher (skeleton — no real strategies wired)."""

from __future__ import annotations

from typing import Any

import pytest

from sky_claw.orchestrator.tool_dispatcher import (
    DuplicateToolError,
    OrchestrationToolDispatcher,
)
from sky_claw.orchestrator.tool_strategies.base import NextCall, ToolStrategy

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeStrategy:
    def __init__(self, name: str, result: dict[str, Any] | None = None, raises: Exception | None = None):
        self.name = name
        self._result = result if result is not None else {"status": "ok", "from": name}
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload_dict)
        if self._raises is not None:
            raise self._raises
        return self._result


def _recording_middleware(label: str, log: list[str], short_circuit: bool = False):
    """Build a middleware that records call order into `log`."""

    async def mw(strategy, payload_dict, next_call: NextCall):
        log.append(f"{label}:before")
        if short_circuit:
            log.append(f"{label}:short-circuit")
            return {"status": "short-circuited", "by": label}
        result = await next_call()
        log.append(f"{label}:after")
        return result

    return mw


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_adds_strategy():
    d = OrchestrationToolDispatcher()
    d.register(_FakeStrategy("alpha"))
    assert d.registered_tools() == ["alpha"]


def test_register_duplicate_raises():
    d = OrchestrationToolDispatcher()
    d.register(_FakeStrategy("alpha"))
    with pytest.raises(DuplicateToolError) as exc:
        d.register(_FakeStrategy("alpha"))
    assert "alpha" in str(exc.value)


def test_register_multiple_distinct_strategies():
    d = OrchestrationToolDispatcher()
    d.register(_FakeStrategy("alpha"))
    d.register(_FakeStrategy("beta"))
    d.register(_FakeStrategy("gamma"))
    assert set(d.registered_tools()) == {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def test_dispatch_routes_to_strategy():
    d = OrchestrationToolDispatcher()
    strat = _FakeStrategy("alpha", result={"status": "ok", "value": 42})
    d.register(strat)

    result = await d.dispatch("alpha", {"foo": "bar"})

    assert result == {"status": "ok", "value": 42}
    assert strat.calls == [{"foo": "bar"}]


async def test_dispatch_unknown_tool_returns_legacy_dict():
    """Preserves the legacy unknown-tool contract with reason ``ToolNotFound``."""
    d = OrchestrationToolDispatcher()
    d.register(_FakeStrategy("alpha"))

    result = await d.dispatch("nonexistent", {})

    assert result == {"status": "error", "reason": "ToolNotFound"}


async def test_dispatch_propagates_strategy_exception_when_no_middleware():
    """Without ErrorWrappingMiddleware, exceptions bubble up — by design."""
    d = OrchestrationToolDispatcher()
    d.register(_FakeStrategy("alpha", raises=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await d.dispatch("alpha", {})


# ---------------------------------------------------------------------------
# Middleware chain
# ---------------------------------------------------------------------------


async def test_middleware_chain_outer_first_order():
    """middleware[0] is OUTER (called first, returns last). LIFO around strategy."""
    d = OrchestrationToolDispatcher()
    log: list[str] = []
    d.register(
        _FakeStrategy("alpha"),
        middleware=[
            _recording_middleware("outer", log),
            _recording_middleware("middle", log),
            _recording_middleware("inner", log),
        ],
    )

    await d.dispatch("alpha", {})

    assert log == [
        "outer:before",
        "middle:before",
        "inner:before",
        "inner:after",
        "middle:after",
        "outer:after",
    ]


async def test_middleware_can_short_circuit():
    d = OrchestrationToolDispatcher()
    log: list[str] = []
    strat = _FakeStrategy("alpha")
    d.register(
        strat,
        middleware=[
            _recording_middleware("gate", log, short_circuit=True),
            _recording_middleware("inner", log),
        ],
    )

    result = await d.dispatch("alpha", {})

    assert result == {"status": "short-circuited", "by": "gate"}
    assert log == ["gate:before", "gate:short-circuit"]
    assert strat.calls == []  # strategy NEVER ran


async def test_middleware_receives_strategy_and_payload():
    d = OrchestrationToolDispatcher()
    captured: dict[str, Any] = {}

    async def capturing_mw(strategy, payload_dict, next_call):
        captured["strategy_name"] = strategy.name
        captured["payload"] = payload_dict
        return await next_call()

    d.register(_FakeStrategy("alpha"), middleware=[capturing_mw])

    await d.dispatch("alpha", {"k": "v"})

    assert captured == {"strategy_name": "alpha", "payload": {"k": "v"}}


async def test_no_middleware_calls_strategy_directly():
    d = OrchestrationToolDispatcher()
    strat = _FakeStrategy("alpha", result={"direct": True})
    d.register(strat)

    result = await d.dispatch("alpha", {"x": 1})

    assert result == {"direct": True}
    assert strat.calls == [{"x": 1}]


def test_strategy_satisfies_protocol():
    """Structural typing check — _FakeStrategy must satisfy ToolStrategy."""
    strat = _FakeStrategy("alpha")
    assert isinstance(strat, ToolStrategy)
