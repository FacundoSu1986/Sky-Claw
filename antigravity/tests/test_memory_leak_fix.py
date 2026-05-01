"""Tests for TASK-006 (M-4): Memory leak fix — transition_history cap + checkpointer cleanup.

Validates that:
1. ``capped_transition_history`` reducer caps merged lists at ``MAX_TRANSITION_HISTORY`` (50)
2. ``capped_transition_history`` returns the full list when under the cap
3. ``capped_transition_history`` handles empty inputs correctly
4. ``MAX_TRANSITION_HISTORY`` constant is set to 50
5. ``WorkflowState.add_transition`` trims history when it exceeds the cap (Pydantic path)
6. ``WorkflowState.trim_history`` trims correctly (non-Pydantic fallback path)
7. ``SupervisorStateGraph.cleanup_old_threads`` purges stale threads from checkpointer
8. ``cleanup_old_threads`` returns 0 when no threads are stale
9. ``cleanup_old_threads`` returns 0 when max_age_seconds <= 0
10. ``cleanup_old_threads`` handles missing checkpointer gracefully
11. ``_thread_timestamps`` is initialized in ``SupervisorStateGraph.__init__``
12. ``execute`` records thread timestamps for TTL tracking
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from sky_claw.antigravity.orchestrator.state_graph import (
    LANGGRAPH_AVAILABLE,
    MAX_TRANSITION_HISTORY,
    PYDANTIC_AVAILABLE,
    SupervisorState,
    SupervisorStateGraph,
    WorkflowState,  # Pydantic or fallback depending on env
    capped_transition_history,
)

# ---------------------------------------------------------------------------
# Problem 1: Custom reducer for transition_history
# ---------------------------------------------------------------------------


class TestCappedTransitionHistoryReducer:
    """Tests for the capped_transition_history LangGraph reducer."""

    def test_max_transition_history_is_50(self) -> None:
        """MAX_TRANSITION_HISTORY must be exactly 50 as per TASK-006 spec."""
        assert MAX_TRANSITION_HISTORY == 50

    def test_merge_under_cap_returns_full_list(self) -> None:
        """When merged list is under the cap, return it unchanged."""
        old = [{"from": "init", "to": "idle"}]
        new = [{"from": "idle", "to": "analyzing"}]
        result = capped_transition_history(old, new)
        assert result == old + new
        assert len(result) == 2

    def test_merge_at_cap_returns_full_list(self) -> None:
        """When merged list is exactly at the cap, return it unchanged."""
        old = [{"idx": i} for i in range(MAX_TRANSITION_HISTORY - 1)]
        new = [{"idx": MAX_TRANSITION_HISTORY - 1}]
        result = capped_transition_history(old, new)
        assert len(result) == MAX_TRANSITION_HISTORY

    def test_merge_over_cap_truncates_to_cap(self) -> None:
        """When merged list exceeds the cap, truncate to last MAX_TRANSITION_HISTORY entries."""
        old = [{"idx": i} for i in range(60)]
        new = [{"idx": i} for i in range(60, 70)]
        result = capped_transition_history(old, new)
        assert len(result) == MAX_TRANSITION_HISTORY
        # Should keep the LAST 50 entries (indices 20..69)
        assert result[0]["idx"] == 20
        assert result[-1]["idx"] == 69

    def test_empty_old_returns_new_capped(self) -> None:
        """When old is empty, return new (or its tail if over cap)."""
        new = [{"idx": i} for i in range(80)]
        result = capped_transition_history([], new)
        assert len(result) == MAX_TRANSITION_HISTORY
        assert result[0]["idx"] == 30

    def test_empty_new_returns_old(self) -> None:
        """When new is empty, return old unchanged."""
        old = [{"idx": i} for i in range(10)]
        result = capped_transition_history(old, [])
        assert result == old

    def test_both_empty_returns_empty(self) -> None:
        """When both lists are empty, return empty."""
        result = capped_transition_history([], [])
        assert result == []

    def test_keeps_most_recent_entries(self) -> None:
        """The reducer must keep the MOST RECENT (last) entries."""
        old = [{"step": f"old_{i}"} for i in range(40)]
        new = [{"step": f"new_{i}"} for i in range(20)]
        result = capped_transition_history(old, new)
        assert len(result) == MAX_TRANSITION_HISTORY
        # 40 + 20 = 60, cap=50 → keep last 50 → old[10..39] + new[0..19]
        assert result[0]["step"] == "old_10"
        # Last 20 should be from new
        assert result[-1]["step"] == "new_19"


class TestWorkflowStateTransitionCap:
    """Tests for WorkflowState.add_transition trimming (Pydantic path)."""

    def test_add_transition_under_cap(self) -> None:
        """Adding transitions under the cap should not trim."""
        ws = WorkflowState()
        for i in range(10):
            ws.add_transition(SupervisorState.IDLE, SupervisorState.ANALYZING, reason=f"step_{i}")
        assert len(ws.transition_history) == 10

    def test_add_transition_at_cap(self) -> None:
        """Adding transitions exactly at the cap should not trim."""
        ws = WorkflowState()
        for i in range(MAX_TRANSITION_HISTORY):
            ws.add_transition(SupervisorState.IDLE, SupervisorState.ANALYZING, reason=f"step_{i}")
        assert len(ws.transition_history) == MAX_TRANSITION_HISTORY

    def test_add_transition_over_cap_trims(self) -> None:
        """Adding transitions over the cap should trim to MAX_TRANSITION_HISTORY."""
        ws = WorkflowState()
        total_entries = MAX_TRANSITION_HISTORY + 30
        for i in range(total_entries):
            ws.add_transition(SupervisorState.IDLE, SupervisorState.ANALYZING, reason=f"step_{i}")
        assert len(ws.transition_history) == MAX_TRANSITION_HISTORY
        # Should keep the LAST 50 entries (reasons step_30 through step_79)
        assert ws.transition_history[0]["reason"] == f"step_{total_entries - MAX_TRANSITION_HISTORY}"

    def test_add_transition_preserves_latest_reasons(self) -> None:
        """After trimming, the most recent transitions should be preserved."""
        ws = WorkflowState()
        for i in range(100):
            ws.add_transition(SupervisorState.IDLE, SupervisorState.ANALYZING, reason=f"r{i}")
        # Last entry should be the most recent
        assert ws.transition_history[-1]["reason"] == "r99"
        # First entry should be r50 (the 51st entry, since we keep last 50)
        assert ws.transition_history[0]["reason"] == "r50"


@pytest.mark.skipif(PYDANTIC_AVAILABLE, reason="Non-Pydantic WorkflowState only when Pydantic is unavailable")
class TestNonPydanticWorkflowStateTrim:
    """Tests for the non-Pydantic WorkflowState.trim_history method."""

    def test_trim_history_under_cap(self) -> None:
        """trim_history should not modify lists under the cap."""
        ws = WorkflowState()
        ws.transition_history = [{"i": i} for i in range(10)]
        ws.trim_history()
        assert len(ws.transition_history) == 10

    def test_trim_history_over_cap(self) -> None:
        """trim_history should truncate to MAX_TRANSITION_HISTORY."""
        ws = WorkflowState()
        ws.transition_history = [{"i": i} for i in range(100)]
        ws.trim_history()
        assert len(ws.transition_history) == MAX_TRANSITION_HISTORY
        assert ws.transition_history[0]["i"] == 50

    def test_trim_history_at_cap(self) -> None:
        """trim_history should not modify lists exactly at the cap."""
        ws = WorkflowState()
        ws.transition_history = [{"i": i} for i in range(MAX_TRANSITION_HISTORY)]
        ws.trim_history()
        assert len(ws.transition_history) == MAX_TRANSITION_HISTORY


# ---------------------------------------------------------------------------
# Problem 2: Checkpointer cleanup with TTL
# ---------------------------------------------------------------------------


class TestCleanupOldThreads:
    """Tests for SupervisorStateGraph.cleanup_old_threads TTL-based cleanup."""

    def test_init_creates_thread_timestamps_dict(self) -> None:
        """__init__ must create _thread_timestamps as an empty dict."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        assert hasattr(graph, "_thread_timestamps")
        assert isinstance(graph._thread_timestamps, dict)
        assert len(graph._thread_timestamps) == 0

    def test_cleanup_returns_zero_when_no_threads(self) -> None:
        """When no threads have been tracked, cleanup should return 0."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        result = graph.cleanup_old_threads(max_age_seconds=3600)
        assert result == 0

    def test_cleanup_returns_zero_when_none_stale(self) -> None:
        """When all threads are recent, cleanup should return 0."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        # Simulate recent thread access
        graph._thread_timestamps["thread_1"] = time.monotonic()
        graph._thread_timestamps["thread_2"] = time.monotonic()
        # Mock checkpointer with storage dict
        graph.checkpointer = MagicMock()
        graph.checkpointer.storage = {"thread_1": "data1", "thread_2": "data2"}
        result = graph.cleanup_old_threads(max_age_seconds=3600)
        assert result == 0
        # Timestamps should still be present
        assert "thread_1" in graph._thread_timestamps
        assert "thread_2" in graph._thread_timestamps

    def test_cleanup_removes_stale_threads(self) -> None:
        """Stale threads should be purged from both checkpointer and timestamps."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        now = time.monotonic()
        # Thread 1 is old (2 hours ago)
        graph._thread_timestamps["stale_thread"] = now - 7200
        # Thread 2 is recent
        graph._thread_timestamps["fresh_thread"] = now
        # Mock checkpointer with storage dict
        graph.checkpointer = MagicMock()
        graph.checkpointer.storage = {
            "stale_thread": "old_data",
            "fresh_thread": "new_data",
        }
        result = graph.cleanup_old_threads(max_age_seconds=3600)
        assert result == 1
        # Stale thread should be removed from timestamps
        assert "stale_thread" not in graph._thread_timestamps
        # Fresh thread should remain
        assert "fresh_thread" in graph._thread_timestamps
        # Stale thread should be removed from storage
        assert "stale_thread" not in graph.checkpointer.storage
        assert "fresh_thread" in graph.checkpointer.storage

    def test_cleanup_removes_multiple_stale_threads(self) -> None:
        """Multiple stale threads should all be purged."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        now = time.monotonic()
        graph._thread_timestamps["old_1"] = now - 7200
        graph._thread_timestamps["old_2"] = now - 10800
        graph._thread_timestamps["fresh"] = now
        graph.checkpointer = MagicMock()
        graph.checkpointer.storage = {
            "old_1": "data1",
            "old_2": "data2",
            "fresh": "data3",
        }
        result = graph.cleanup_old_threads(max_age_seconds=3600)
        assert result == 2
        assert len(graph._thread_timestamps) == 1
        assert "fresh" in graph._thread_timestamps

    def test_cleanup_returns_zero_for_zero_max_age(self) -> None:
        """max_age_seconds=0 should return 0 (nothing to clean)."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        graph._thread_timestamps["thread_1"] = time.monotonic() - 9999
        result = graph.cleanup_old_threads(max_age_seconds=0)
        assert result == 0

    def test_cleanup_returns_zero_for_negative_max_age(self) -> None:
        """Negative max_age_seconds should return 0."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        graph._thread_timestamps["thread_1"] = time.monotonic() - 9999
        result = graph.cleanup_old_threads(max_age_seconds=-1)
        assert result == 0

    def test_cleanup_handles_no_checkpointer(self) -> None:
        """When checkpointer is None, should return 0 gracefully."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        graph.checkpointer = None
        graph._thread_timestamps["thread_1"] = time.monotonic() - 9999
        result = graph.cleanup_old_threads(max_age_seconds=3600)
        assert result == 0

    def test_cleanup_handles_checkpointer_without_storage(self) -> None:
        """When checkpointer has no storage/checkpoints attr, should still clean timestamps."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        now = time.monotonic()
        graph._thread_timestamps["old_thread"] = now - 7200
        # Mock checkpointer without storage or checkpoints
        graph.checkpointer = MagicMock(spec=[])  # No attributes
        result = graph.cleanup_old_threads(max_age_seconds=3600)
        # No threads removed from storage (no storage), but timestamps cleaned
        assert result == 0
        # Timestamp should still be cleaned up
        assert "old_thread" not in graph._thread_timestamps

    def test_cleanup_uses_checkpoints_attribute_fallback(self) -> None:
        """Should try 'checkpoints' attribute if 'storage' is not available."""
        graph = SupervisorStateGraph(profile_name="test_cleanup")
        now = time.monotonic()
        graph._thread_timestamps["old_thread"] = now - 7200
        # Mock checkpointer with 'checkpoints' instead of 'storage'
        graph.checkpointer = MagicMock(spec=["checkpoints"])
        graph.checkpointer.checkpoints = {"old_thread": "data"}
        # Make storage raise AttributeError to trigger fallback
        type(graph.checkpointer).storage = property(lambda self: (_ for _ in ()).throw(AttributeError))
        result = graph.cleanup_old_threads(max_age_seconds=3600)
        assert result == 1
        assert "old_thread" not in graph._thread_timestamps


class TestExecuteTracksTimestamps:
    """Tests that execute() records thread timestamps for TTL tracking."""

    def test_thread_timestamps_initialized_empty(self) -> None:
        """_thread_timestamps should start empty."""
        graph = SupervisorStateGraph(profile_name="test_ts")
        assert graph._thread_timestamps == {}

    @pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
    @pytest.mark.asyncio
    async def test_execute_records_timestamp(self) -> None:
        """execute() should record a monotonic timestamp for the thread_id."""
        graph = SupervisorStateGraph(profile_name="test_ts_exec")
        initial = graph.get_initial_state()
        thread_id = initial["workflow_id"]

        # Execute will likely end quickly (graph runs through nodes)
        # We just need to verify the timestamp was recorded
        import contextlib

        with contextlib.suppress(Exception):
            await graph.execute(initial)

        assert thread_id in graph._thread_timestamps
        assert isinstance(graph._thread_timestamps[thread_id], float)
        # Timestamp should be recent (within last 5 seconds)
        assert (time.monotonic() - graph._thread_timestamps[thread_id]) < 5.0
