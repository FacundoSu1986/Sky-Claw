"""Unit tests for ErrorWrappingMiddleware and DictResultGuardMiddleware."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sky_claw.antigravity.orchestrator.tool_dispatcher import OrchestrationToolDispatcher
from sky_claw.antigravity.orchestrator.tool_strategies.middleware import (
    DictResultGuardMiddleware,
    ErrorWrappingMiddleware,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Strategy:
    def __init__(self, name: str, returns: Any = None, raises: BaseException | None = None):
        self.name = name
        self._returns = returns
        self._raises = raises

    async def execute(self, payload_dict: dict[str, Any]) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._returns


# ---------------------------------------------------------------------------
# ErrorWrappingMiddleware
# ---------------------------------------------------------------------------


async def test_error_wrapping_passes_through_on_success():
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", returns={"status": "ok"}),
        middleware=[ErrorWrappingMiddleware("AlphaFailed")],
    )

    result = await d.dispatch("alpha", {})

    assert result == {"status": "ok"}


async def test_error_wrapping_catches_runtime_error():
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", raises=RuntimeError("kaboom")),
        middleware=[ErrorWrappingMiddleware("AlphaFailed")],
    )

    result = await d.dispatch("alpha", {})

    assert result["status"] == "error"
    assert result["reason"] == "AlphaFailed"
    assert "kaboom" in result["details"]


async def test_error_wrapping_catches_value_error():
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", raises=ValueError("bad input")),
        middleware=[ErrorWrappingMiddleware("AlphaFailed")],
    )

    result = await d.dispatch("alpha", {})

    assert result == {"status": "error", "reason": "AlphaFailed", "details": "bad input"}


async def test_error_wrapping_does_not_catch_keyboard_interrupt():
    """BaseException subclasses must propagate (shutdown discipline)."""
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", raises=KeyboardInterrupt()),
        middleware=[ErrorWrappingMiddleware("AlphaFailed")],
    )

    with pytest.raises(KeyboardInterrupt):
        await d.dispatch("alpha", {})


async def test_error_wrapping_does_not_catch_cancelled_error():
    """asyncio.CancelledError must propagate so task cancellation works."""
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", raises=asyncio.CancelledError()),
        middleware=[ErrorWrappingMiddleware("AlphaFailed")],
    )

    with pytest.raises(asyncio.CancelledError):
        await d.dispatch("alpha", {})


async def test_error_wrapping_does_not_catch_system_exit():
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", raises=SystemExit(1)),
        middleware=[ErrorWrappingMiddleware("AlphaFailed")],
    )

    with pytest.raises(SystemExit):
        await d.dispatch("alpha", {})


# ---------------------------------------------------------------------------
# DictResultGuardMiddleware
# ---------------------------------------------------------------------------


async def test_dict_guard_passes_dict_through():
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", returns={"valid": True}),
        middleware=[DictResultGuardMiddleware("AlphaInvalid")],
    )

    result = await d.dispatch("alpha", {})

    assert result == {"valid": True}


async def test_dict_guard_rejects_list():
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", returns=[1, 2, 3]),
        middleware=[DictResultGuardMiddleware("AlphaInvalid")],
    )

    result = await d.dispatch("alpha", {})

    assert result == {"status": "error", "reason": "AlphaInvalid"}


async def test_dict_guard_rejects_none():
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", returns=None),
        middleware=[DictResultGuardMiddleware("AlphaInvalid")],
    )

    result = await d.dispatch("alpha", {})

    assert result == {"status": "error", "reason": "AlphaInvalid"}


async def test_dict_guard_rejects_string():
    d = OrchestrationToolDispatcher()
    d.register(
        _Strategy("alpha", returns="oops"),
        middleware=[DictResultGuardMiddleware("AlphaInvalid")],
    )

    result = await d.dispatch("alpha", {})

    assert result == {"status": "error", "reason": "AlphaInvalid"}


# ---------------------------------------------------------------------------
# Composition (the synthesis/xedit pattern)
# ---------------------------------------------------------------------------


async def test_compose_error_wrapping_outside_dict_guard_inside():
    """ErrorWrap [outer] catches exceptions; DictGuard [inner] guards type.
    On strategy exception -> ErrorWrap returns {status:error,reason:Failed,details:...}.
    On strategy non-dict -> DictGuard returns {status:error,reason:Invalid}, ErrorWrap passes through.
    On strategy dict -> both pass through.
    """
    d = OrchestrationToolDispatcher()

    # Case A: exception
    d.register(
        _Strategy("a_exc", raises=RuntimeError("boom")),
        middleware=[
            ErrorWrappingMiddleware("Failed"),
            DictResultGuardMiddleware("Invalid"),
        ],
    )
    result_a = await d.dispatch("a_exc", {})
    assert result_a["status"] == "error"
    assert result_a["reason"] == "Failed"
    assert "boom" in result_a["details"]

    # Case B: non-dict result
    d.register(
        _Strategy("b_nondict", returns="not a dict"),
        middleware=[
            ErrorWrappingMiddleware("Failed"),
            DictResultGuardMiddleware("Invalid"),
        ],
    )
    result_b = await d.dispatch("b_nondict", {})
    assert result_b == {"status": "error", "reason": "Invalid"}

    # Case C: happy path
    d.register(
        _Strategy("c_ok", returns={"happy": True}),
        middleware=[
            ErrorWrappingMiddleware("Failed"),
            DictResultGuardMiddleware("Invalid"),
        ],
    )
    result_c = await d.dispatch("c_ok", {})
    assert result_c == {"happy": True}
