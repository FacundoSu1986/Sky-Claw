"""Unit tests for OrchestrationToolDispatcher (skeleton — no real strategies wired)."""

from __future__ import annotations

import pathlib
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sky_claw.antigravity.orchestrator.tool_dispatcher import (
    DuplicateToolError,
    OrchestrationToolDispatcher,
)
from sky_claw.antigravity.orchestrator.tool_strategies.base import NextCall, ToolStrategy

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


# ---------------------------------------------------------------------------
# _build_chain_preview_service — LOOT path resolution
# ---------------------------------------------------------------------------


class _FakeSupervisorForPreview:
    """Minimal supervisor double for _build_chain_preview_service tests.

    Provides only the attributes accessed by _build_chain_preview_service;
    everything else is a MagicMock so the function can build its
    collaborators without real binaries.
    """

    def __init__(self, *, loot_exe: pathlib.Path | None = None) -> None:
        self._path_resolver = MagicMock()
        self._path_resolver.get_skyrim_path.return_value = pathlib.Path("/skyrim")
        self._path_resolver.get_xedit_path.return_value = pathlib.Path("/xedit.exe")
        self._path_resolver.get_loot_exe.return_value = loot_exe
        self._path_validator = MagicMock()
        self._lock_manager = MagicMock()
        self.snapshot_manager = MagicMock()
        self.journal = MagicMock()
        self._event_bus = MagicMock()


# All classes lazily imported inside _build_chain_preview_service that we need
# to stub out so the function can run without real tool binaries on disk.
_CHAIN_PREVIEW_PATCHES = [
    "sky_claw.local.loot.cli.LOOTConfig",
    "sky_claw.local.loot.cli.LOOTRunner",
    "sky_claw.local.xedit.runner.XEditRunner",
    "sky_claw.local.xedit.conflict_analyzer.ConflictAnalyzer",
    "sky_claw.antigravity.orchestrator.preview.chain_preview_service.ChainPreviewService",
]


@pytest.mark.parametrize(
    "loot_exe_configured,expected_loot_exe",
    [
        (pathlib.Path(r"C:\LOOT\loot.exe"), pathlib.Path(r"C:\LOOT\loot.exe")),
        (None, pathlib.Path("loot.exe")),
    ],
    ids=["configured-path-is-used", "none-falls-back-to-loot.exe"],
)
def test_build_chain_preview_loot_exe_resolution(
    loot_exe_configured: pathlib.Path | None,
    expected_loot_exe: pathlib.Path,
) -> None:
    """_build_chain_preview_service consults get_loot_exe() and uses the
    resolved path; falls back to Path("loot.exe") when the resolver returns
    None (LOOT_EXE env var not set), preserving backward-compat behaviour.
    """
    from sky_claw.antigravity.orchestrator.tool_dispatcher import _build_chain_preview_service

    with ExitStack() as stack:
        mocks = [stack.enter_context(patch(p)) for p in _CHAIN_PREVIEW_PATCHES]
        mock_loot_config = mocks[0]  # sky_claw.local.loot.cli.LOOTConfig

        _build_chain_preview_service(
            _FakeSupervisorForPreview(loot_exe=loot_exe_configured),  # type: ignore[arg-type]
        )

        mock_loot_config.assert_called_once()
        assert mock_loot_config.call_args.kwargs["loot_exe"] == expected_loot_exe
