"""P1 R-07 — SupervisorStateGraph.execute must auto-purge stale thread state.

``cleanup_old_threads`` already exists on ``SupervisorStateGraph`` but is
NEVER invoked by ``execute()`` — every workflow run writes a new entry to
``_thread_timestamps`` and the dict grows monotonically until process exit.

Fix: maintain a small ``_execution_count`` counter, and every
``_cleanup_interval`` executions invoke ``cleanup_old_threads`` with a
generous TTL (default 3600s). Test patches the interval to a low value so
the contract is reachable in a unit test.

Contracts:
- ``cleanup_old_threads`` is invoked exactly once after the interval is hit.
- The counter resets after each cleanup so the next sweep is N more
  executions away.
- Disabling the interval (=0) keeps the legacy behavior — no auto cleanup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.orchestrator.state_graph import SupervisorStateGraph


def _bare_integration(cleanup_interval: int) -> SupervisorStateGraph:
    """Build a minimal SupervisorStateGraph bypassing __init__ to test cleanup behavior."""
    sg = SupervisorStateGraph.__new__(SupervisorStateGraph)
    sg.checkpointer = MagicMock()
    sg.compiled_graph = MagicMock()
    sg.compiled_graph.ainvoke = AsyncMock(side_effect=lambda state, _config: state)
    sg._state = None
    sg._callbacks = {}
    sg._thread_timestamps = {}
    sg._execution_count = 0
    sg._cleanup_interval = cleanup_interval  # type: ignore[attr-defined]
    sg._cleanup_max_age_seconds = 3600  # type: ignore[attr-defined]
    # Patch loop_guardrail off — not needed for these tests.
    sg.loop_guardrail = MagicMock()
    return sg


def _state(workflow_id: str) -> dict:  # type: ignore[type-arg]
    return {
        "workflow_id": workflow_id,
        "current_state": "started",
        "iteration": 0,
        "last_error": None,
        "context": {},
        "tool_results": [],
        "tool_calls": [],
        "tool_state_machine": None,
        "tools_metadata": None,
        "loop_context": None,
        "hitl_started_at": None,
    }


class TestExecuteAutoCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_invoked_after_interval_executions(self) -> None:
        """After N=3 executions (interval=3), cleanup_old_threads must run exactly once."""
        sg = _bare_integration(cleanup_interval=3)
        sg.cleanup_old_threads = MagicMock(return_value=0)  # type: ignore[method-assign]

        for i in range(3):
            await sg.execute(_state(f"wf-{i}"))

        sg.cleanup_old_threads.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_not_invoked_before_interval(self) -> None:
        """N-1 executions must not trigger cleanup."""
        sg = _bare_integration(cleanup_interval=5)
        sg.cleanup_old_threads = MagicMock(return_value=0)  # type: ignore[method-assign]

        for i in range(4):
            await sg.execute(_state(f"wf-{i}"))

        sg.cleanup_old_threads.assert_not_called()

    @pytest.mark.asyncio
    async def test_counter_resets_so_cleanup_fires_periodically(self) -> None:
        """After the first sweep, the counter must reset so another sweep fires N executions later."""
        sg = _bare_integration(cleanup_interval=2)
        sg.cleanup_old_threads = MagicMock(return_value=0)  # type: ignore[method-assign]

        for i in range(4):
            await sg.execute(_state(f"wf-{i}"))

        assert sg.cleanup_old_threads.call_count == 2, (
            f"Expected 2 cleanup sweeps for 4 executions at interval=2; got {sg.cleanup_old_threads.call_count}"
        )

    @pytest.mark.asyncio
    async def test_zero_interval_disables_auto_cleanup(self) -> None:
        """Setting interval=0 keeps the legacy behavior (no automatic sweep)."""
        sg = _bare_integration(cleanup_interval=0)
        sg.cleanup_old_threads = MagicMock(return_value=0)  # type: ignore[method-assign]

        for i in range(50):
            await sg.execute(_state(f"wf-{i}"))

        sg.cleanup_old_threads.assert_not_called()
