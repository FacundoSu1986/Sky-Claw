"""Tests for FASE 1.5.4 — IdempotencyMiddleware + ProgressMiddleware.

Validates:
- IdempotencyMiddleware rejects duplicate concurrent executions
- IdempotencyMiddleware releases lock on completion/failure
- ProgressMiddleware publishes lifecycle events to CoreEventBus
- ProgressMiddleware is a no-op pass-through when event_bus is None
- Middleware chain preserves the dispatch() contract (returns dict)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.core.event_bus import CoreEventBus, Event
from sky_claw.orchestrator.tool_state_machine import ToolStateMachine
from sky_claw.orchestrator.tool_strategies.middleware import (
    IdempotencyMiddleware,
    ProgressMiddleware,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_strategy(name: str = "test_tool") -> Any:
    """Create a mock ToolStrategy with a .name attribute."""
    strategy = MagicMock()
    strategy.name = name
    return strategy


async def _make_next_call(result: dict[str, Any] | None = None) -> Any:
    """Create a next_call thunk that returns the given result."""
    ret = result if result is not None else {"status": "success"}

    async def next_call() -> dict[str, Any]:
        return ret

    return next_call


# ------------------------------------------------------------------
# IdempotencyMiddleware
# ------------------------------------------------------------------


class TestIdempotencyMiddleware:
    """IdempotencyMiddleware rejects duplicate concurrent executions."""

    @pytest.mark.asyncio
    async def test_allows_first_execution(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success"})

        result = await mw(strategy, {"status": "active"}, next_call)

        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_rejects_duplicate_execution(self) -> None:
        """Simulates concurrent execution: first task is RUNNING when second arrives."""
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        payload = {"status": "active"}

        # Block the first execution so it stays RUNNING
        block_event = asyncio.Event()

        async def blocked_call() -> dict[str, Any]:
            await block_event.wait()
            return {"status": "success"}

        # Start first execution in background
        task1 = asyncio.create_task(mw(strategy, payload, blocked_call))
        # Give it time to acquire the lock and transition to RUNNING
        await asyncio.sleep(0.05)

        # Second execution with same payload should be rejected
        next_call_2 = await _make_next_call({"status": "success"})
        result_2 = await mw(strategy, payload, next_call_2)
        assert result_2["status"] == "error"
        assert result_2["reason"] == "DuplicateExecution"

        # Unblock and await first task
        block_event.set()
        result_1 = await task1
        assert result_1 == {"status": "success"}

    @pytest.mark.asyncio
    async def test_allows_different_payload(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")

        next_call_1 = await _make_next_call()
        result_1 = await mw(strategy, {"status": "active"}, next_call_1)
        assert result_1["status"] == "success"

        next_call_2 = await _make_next_call()
        result_2 = await mw(strategy, {"status": "inactive"}, next_call_2)
        assert result_2["status"] == "success"

    @pytest.mark.asyncio
    async def test_releases_lock_on_completion(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        payload = {"x": 1}

        # First execution completes
        next_call_1 = await _make_next_call()
        await mw(strategy, payload, next_call_1)

        # Lock should be released — same payload can run again
        next_call_2 = await _make_next_call()
        result = await mw(strategy, payload, next_call_2)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_releases_lock_on_failure(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")
        payload = {"x": 1}

        async def failing_call() -> dict[str, Any]:
            raise RuntimeError("Tool crashed")

        with pytest.raises(RuntimeError, match="Tool crashed"):
            await mw(strategy, payload, failing_call)

        # Lock should be released after failure
        next_call_2 = await _make_next_call()
        result = await mw(strategy, payload, next_call_2)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_state_machine_tracks_task_lifecycle(self) -> None:
        sm = ToolStateMachine()
        mw = IdempotencyMiddleware(state_machine=sm)
        strategy = _make_strategy("list_mods")

        next_call = await _make_next_call({"status": "success", "mods": [1, 2]})
        await mw(strategy, {}, next_call)

        # There should be a completed task in the state machine
        assert sm.active_task_count == 0


# ------------------------------------------------------------------
# ProgressMiddleware
# ------------------------------------------------------------------


class TestProgressMiddleware:
    """ProgressMiddleware publishes lifecycle events to CoreEventBus."""

    @pytest.mark.asyncio
    async def test_noop_when_no_bus(self) -> None:
        """When event_bus is None, middleware is a transparent pass-through."""
        mw = ProgressMiddleware(event_bus=None)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success"})

        result = await mw(strategy, {}, next_call)
        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_emits_started_and_completed(self) -> None:
        bus = CoreEventBus()
        await bus.start()

        collected_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            collected_events.append(event)

        bus.subscribe("ops.tool.*", subscriber)

        mw = ProgressMiddleware(event_bus=bus)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success"})

        result = await mw(strategy, {}, next_call)
        assert result == {"status": "success"}

        # Give the bus time to dispatch
        await asyncio.sleep(0.1)

        topics = [e.topic for e in collected_events]
        assert "ops.tool.started" in topics
        assert "ops.tool.completed" in topics

        await bus.stop()

    @pytest.mark.asyncio
    async def test_emits_failed_on_exception(self) -> None:
        bus = CoreEventBus()
        await bus.start()

        collected_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            collected_events.append(event)

        bus.subscribe("ops.tool.*", subscriber)

        mw = ProgressMiddleware(event_bus=bus)
        strategy = _make_strategy("failing_tool")

        async def failing_call() -> dict[str, Any]:
            raise RuntimeError("Boom")

        with pytest.raises(RuntimeError):
            await mw(strategy, {}, failing_call)

        await asyncio.sleep(0.1)

        topics = [e.topic for e in collected_events]
        assert "ops.tool.started" in topics
        assert "ops.tool.failed" in topics

        # Verify failed event has error details
        failed_event = next(e for e in collected_events if e.topic == "ops.tool.failed")
        assert failed_event.payload["tool"] == "failing_tool"
        assert "Boom" in failed_event.payload["error"]
        assert failed_event.payload["error_type"] == "RuntimeError"

        await bus.stop()

    @pytest.mark.asyncio
    async def test_completed_payload_includes_status(self) -> None:
        bus = CoreEventBus()
        await bus.start()

        collected_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            collected_events.append(event)

        bus.subscribe("ops.tool.completed", subscriber)

        mw = ProgressMiddleware(event_bus=bus)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success", "count": 5})

        await mw(strategy, {}, next_call)
        await asyncio.sleep(0.1)

        assert len(collected_events) == 1
        assert collected_events[0].payload["status"] == "success"
        assert collected_events[0].payload["tool"] == "list_mods"

        await bus.stop()

    @pytest.mark.asyncio
    async def test_bus_failure_does_not_disrupt_execution(self) -> None:
        """If the bus fails to publish, the tool still executes."""
        mock_bus = MagicMock()
        mock_bus.publish = AsyncMock(side_effect=ConnectionError("Bus down"))

        mw = ProgressMiddleware(event_bus=mock_bus)
        strategy = _make_strategy("list_mods")
        next_call = await _make_next_call({"status": "success"})

        # Should NOT raise despite bus failure
        result = await mw(strategy, {}, next_call)
        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_started_payload_includes_tool_name(self) -> None:
        bus = CoreEventBus()
        await bus.start()

        collected_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            collected_events.append(event)

        bus.subscribe("ops.tool.started", subscriber)

        mw = ProgressMiddleware(event_bus=bus)
        strategy = _make_strategy("generate_lods")
        next_call = await _make_next_call()

        await mw(strategy, {"profile": "Default"}, next_call)
        await asyncio.sleep(0.1)

        assert len(collected_events) == 1
        assert collected_events[0].payload["tool"] == "generate_lods"
        assert "profile" in collected_events[0].payload["payload_keys"]

        await bus.stop()
