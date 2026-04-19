"""Tests for the AgenticLoopGuardrail cognitive circuit breaker."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from sky_claw.core.models import CircuitBreakerTrippedError
from sky_claw.security.loop_guardrail import AgenticLoopGuardrail


class TestAgenticLoopGuardrail:
    def test_under_threshold_no_trip(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        guardrail.register_and_check("patch_plugin", {"plugin": "foo.esp"})
        guardrail.register_and_check("patch_plugin", {"plugin": "foo.esp"})
        assert len(guardrail.snapshot()) == 2

    def test_trips_at_max_repeats(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        args = {"plugin": "foo.esp", "priority": 10}
        guardrail.register_and_check("patch_plugin", args)
        guardrail.register_and_check("patch_plugin", args)
        with pytest.raises(CircuitBreakerTrippedError) as exc_info:
            guardrail.register_and_check("patch_plugin", args)
        assert exc_info.value.tool_name == "patch_plugin"
        assert exc_info.value.occurrences == 3
        assert "patch_plugin" in str(exc_info.value)

    def test_history_clears_after_trip(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        args = {"plugin": "foo.esp"}
        guardrail.register_and_check("patch_plugin", args)
        guardrail.register_and_check("patch_plugin", args)
        with pytest.raises(CircuitBreakerTrippedError):
            guardrail.register_and_check("patch_plugin", args)
        assert guardrail.snapshot() == ()
        guardrail.register_and_check("patch_plugin", args)
        assert len(guardrail.snapshot()) == 1

    def test_different_args_do_not_accumulate(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        guardrail.register_and_check("patch_plugin", {"plugin": "a.esp"})
        guardrail.register_and_check("patch_plugin", {"plugin": "b.esp"})
        guardrail.register_and_check("patch_plugin", {"plugin": "c.esp"})
        assert len(guardrail.snapshot()) == 3

    def test_different_tools_do_not_accumulate(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        args = {"target": "foo"}
        guardrail.register_and_check("tool_a", args)
        guardrail.register_and_check("tool_b", args)
        guardrail.register_and_check("tool_c", args)

    def test_arg_order_insensitive(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        guardrail.register_and_check("scan", {"a": 1, "b": 2})
        guardrail.register_and_check("scan", {"b": 2, "a": 1})
        with pytest.raises(CircuitBreakerTrippedError):
            guardrail.register_and_check("scan", {"a": 1, "b": 2})

    def test_non_json_serializable_args(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        args = {"path": Path("/tmp/foo"), "when": datetime(2026, 1, 1)}
        guardrail.register_and_check("inspect", args)
        guardrail.register_and_check("inspect", args)
        with pytest.raises(CircuitBreakerTrippedError):
            guardrail.register_and_check("inspect", args)

    def test_sliding_window_evicts_old(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=3)
        target = {"plugin": "target.esp"}
        other = {"plugin": "other.esp"}
        guardrail.register_and_check("patch", target)
        guardrail.register_and_check("patch", other)
        guardrail.register_and_check("patch", other)
        guardrail.register_and_check("patch", target)
        assert len(guardrail.snapshot()) == 3

    def test_reset_method(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        guardrail.register_and_check("a", {"x": 1})
        guardrail.register_and_check("a", {"x": 1})
        guardrail.reset()
        assert guardrail.snapshot() == ()

    def test_snapshot_returns_tuple(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        guardrail.register_and_check("a", {"x": 1})
        snap = guardrail.snapshot()
        assert isinstance(snap, tuple)
        assert len(snap) == 1

    def test_empty_args(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        guardrail.register_and_check("ping", {})
        guardrail.register_and_check("ping", {})
        with pytest.raises(CircuitBreakerTrippedError):
            guardrail.register_and_check("ping", {})

    def test_max_repeats_two(self) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=2, window_size=5)
        guardrail.register_and_check("a", {"x": 1})
        with pytest.raises(CircuitBreakerTrippedError):
            guardrail.register_and_check("a", {"x": 1})

    def test_logs_critical_on_trip(self, caplog: pytest.LogCaptureFixture) -> None:
        guardrail = AgenticLoopGuardrail(max_repeats=2, window_size=5)
        guardrail.register_and_check("a", {"x": 1})
        with (
            caplog.at_level("CRITICAL", logger="SkyClaw.AgenticLoopGuardrail"),
            pytest.raises(CircuitBreakerTrippedError),
        ):
            guardrail.register_and_check("a", {"x": 1})
        assert any("Loop Detectado" in record.message for record in caplog.records)

    def test_non_consecutive_pattern_does_not_trip(self) -> None:
        """FIX 4: Verify that non-consecutive repeats do NOT trigger the breaker.

        Pattern: A (registered), B (registered), A (registered), A (registered)
        Expected: No CircuitBreakerTrippedError raised because A is not in the
        last 3 slots consecutively. The last 3 elements are [B, A, A], which
        are not all identical.
        """
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)

        # Register A (hash 1)
        guardrail.register_and_check("tool_a", {"arg": "value"})
        assert len(guardrail.snapshot()) == 1

        # Register B (hash 2) — breaks the sequence
        guardrail.register_and_check("tool_b", {"arg": "other"})
        assert len(guardrail.snapshot()) == 2

        # Register A again (hash 1)
        guardrail.register_and_check("tool_a", {"arg": "value"})
        assert len(guardrail.snapshot()) == 3

        # Register A third time (still not consecutive from position 0)
        guardrail.register_and_check("tool_a", {"arg": "value"})
        assert len(guardrail.snapshot()) == 4

        # At this point, history is [hash(A), hash(B), hash(A), hash(A)]
        # The last 3 elements are [hash(B), hash(A), hash(A)] — NOT all identical
        # So the breaker should NOT trip
        # Verify guardrail is still active (not cleared)
        assert len(guardrail.snapshot()) == 4

    def test_consecutive_pattern_trips_correctly(self) -> None:
        """FIX 4: Verify that consecutive repeats DO trigger the breaker correctly.

        Pattern: A, A, A (three consecutive identical actions)
        Expected: CircuitBreakerTrippedError raised on the 3rd attempt.
        """
        guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)

        guardrail.register_and_check("tool_a", {"arg": "value"})
        guardrail.register_and_check("tool_a", {"arg": "value"})

        with pytest.raises(CircuitBreakerTrippedError) as exc_info:
            guardrail.register_and_check("tool_a", {"arg": "value"})

        assert exc_info.value.tool_name == "tool_a"
        assert exc_info.value.occurrences == 3
        # Verify history was cleared after trip
        assert guardrail.snapshot() == ()
