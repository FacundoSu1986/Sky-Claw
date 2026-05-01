"""Tests for TASK-005 (M-3): HITL_WAIT infinite loop elimination.

Validates that:
1. ``route_from_hitl_wait`` returns HITL_WAIT when response is None and timeout not expired
2. ``route_from_hitl_wait`` routes to ERROR after timeout expiration
3. ``route_from_hitl_wait`` routes to ERROR on inconsistent state (missing hitl_started_at)
4. ``route_from_hitl_wait`` routes correctly when response is received
5. ``hitl_wait_node`` preserves ``hitl_started_at`` across wait cycles
"""

from __future__ import annotations

import time
from unittest.mock import patch

from sky_claw.antigravity.orchestrator.state_graph import (
    StateGraphEdges,
    StateGraphNodes,
    SupervisorState,
)
from sky_claw.config import HITL_TIMEOUT_SECONDS


def _make_state(
    hitl_response: str | None = None,
    hitl_started_at: float | None = None,
    last_error: str | None = None,
) -> dict:
    """Create a minimal state dict for testing route_from_hitl_wait."""
    return {
        "workflow_id": "test_wf",
        "current_state": SupervisorState.HITL_WAIT.value,
        "previous_state": SupervisorState.DISPATCHING.value,
        "profile_name": "Default",
        "modlist_path": None,
        "last_mtime": 0.0,
        "pending_event": None,
        "event_data": {},
        "tool_name": None,
        "tool_payload": {},
        "tool_result": None,
        "hitl_request": {"action_type": "test"},
        "hitl_response": hitl_response,
        "transition_history": [],
        "last_error": last_error,
        "error_count": 0,
        "rollback_triggered": False,
        "rollback_result": None,
        "rollback_transaction_id": None,
        "loop_detected": False,
        "loop_context": None,
        "hitl_started_at": hitl_started_at,
    }


class TestHitlWaitReturnsWaitWhenNotExpired:
    """Condición B: Espera legítima sin expirar → HITL_WAIT."""

    def test_hitl_wait_returns_wait_when_response_none_and_timeout_not_expired(self) -> None:
        """Simula hitl_started_at reciente, hitl_response=None → HITL_WAIT."""
        now = time.monotonic()
        state = _make_state(hitl_response=None, hitl_started_at=now)

        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=now + 1.0):
            result = StateGraphEdges.route_from_hitl_wait(state)

        assert result == SupervisorState.HITL_WAIT.value

    def test_hitl_wait_returns_wait_at_half_timeout(self) -> None:
        """At exactly half the timeout, should still return HITL_WAIT."""
        started = time.monotonic()
        state = _make_state(hitl_response=None, hitl_started_at=started)

        half_time = started + (HITL_TIMEOUT_SECONDS / 2)
        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=half_time):
            result = StateGraphEdges.route_from_hitl_wait(state)

        assert result == SupervisorState.HITL_WAIT.value


class TestHitlTimeoutRoutesToError:
    """Condición A: Timeout alcanzado → ERROR."""

    def test_hitl_timeout_routes_to_error_after_expiration(self) -> None:
        """Simula hitl_started_at antiguo que excede HITL_TIMEOUT_SECONDS → ERROR."""
        started = 1000.0  # Arbitrary old timestamp
        state = _make_state(hitl_response=None, hitl_started_at=started)

        # Simulate time well past the timeout
        expired_time = started + HITL_TIMEOUT_SECONDS + 1.0
        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=expired_time):
            result = StateGraphEdges.route_from_hitl_wait(state)

        assert result == SupervisorState.ERROR.value
        assert "Timeout" in state["last_error"]
        assert str(HITL_TIMEOUT_SECONDS) in state["last_error"]

    def test_hitl_timeout_routes_to_error_exactly_at_boundary(self) -> None:
        """At exactly HITL_TIMEOUT_SECONDS elapsed → ERROR."""
        started = 5000.0
        state = _make_state(hitl_response=None, hitl_started_at=started)

        exact_boundary = started + HITL_TIMEOUT_SECONDS
        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=exact_boundary):
            result = StateGraphEdges.route_from_hitl_wait(state)

        assert result == SupervisorState.ERROR.value


class TestHitlInconsistentStateRoutesToError:
    """Condición C: Estado inconsistente (sin timestamp) → ERROR."""

    def test_hitl_inconsistent_state_routes_to_error(self) -> None:
        """hitl_response=None con hitl_started_at=None → ERROR."""
        state = _make_state(hitl_response=None, hitl_started_at=None)

        result = StateGraphEdges.route_from_hitl_wait(state)

        assert result == SupervisorState.ERROR.value
        assert "inconsistente" in state["last_error"].lower()
        assert "hitl_started_at" in state["last_error"]


class TestHitlResponseReceivedRoutesCorrectly:
    """Condición D: Respuesta recibida → transición según respuesta."""

    def test_approved_routes_to_dispatching(self) -> None:
        """hitl_response='approved' → DISPATCHING."""
        state = _make_state(hitl_response="approved", hitl_started_at=time.monotonic())
        result = StateGraphEdges.route_from_hitl_wait(state)
        assert result == SupervisorState.DISPATCHING.value

    def test_denied_routes_to_completed(self) -> None:
        """hitl_response='denied' → COMPLETED."""
        state = _make_state(hitl_response="denied", hitl_started_at=time.monotonic())
        result = StateGraphEdges.route_from_hitl_wait(state)
        assert result == SupervisorState.COMPLETED.value

    def test_timeout_response_routes_to_error(self) -> None:
        """hitl_response='timeout' → ERROR."""
        state = _make_state(hitl_response="timeout", hitl_started_at=time.monotonic())
        result = StateGraphEdges.route_from_hitl_wait(state)
        assert result == SupervisorState.ERROR.value

    def test_response_received_no_interference_from_timeout(self) -> None:
        """Even with expired timeout, a received response takes priority."""
        started = 1000.0
        expired_time = started + HITL_TIMEOUT_SECONDS + 100.0
        state = _make_state(hitl_response="approved", hitl_started_at=started)

        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=expired_time):
            result = StateGraphEdges.route_from_hitl_wait(state)

        # Response takes priority over timeout
        assert result == SupervisorState.DISPATCHING.value


class TestHitlStartedAtPreservedAcrossWaitCycles:
    """Verifica que hitl_started_at no se sobrescriba en iteraciones subsecuentes."""

    def test_hitl_started_at_preserved_across_wait_cycles(self) -> None:
        """hitl_wait_node no sobrescribe hitl_started_at si ya tiene valor."""
        original_ts = 12345.0
        state = _make_state(hitl_response=None, hitl_started_at=original_ts)

        result = StateGraphNodes.hitl_wait_node(state)

        # The node should NOT include hitl_started_at in updates (it's already set)
        assert "hitl_started_at" not in result
        assert result["current_state"] == SupervisorState.HITL_WAIT.value

    def test_hitl_started_at_injected_on_first_entry(self) -> None:
        """hitl_wait_node inyecta hitl_started_at cuando es None."""
        state = _make_state(hitl_response=None, hitl_started_at=None)

        fake_now = 99999.0
        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=fake_now):
            result = StateGraphNodes.hitl_wait_node(state)

        assert result["hitl_started_at"] == fake_now

    def test_full_cycle_first_entry_then_wait_then_timeout(self) -> None:
        """Full lifecycle: first entry → wait → timeout."""
        # Step 1: First entry — node injects timestamp
        state = _make_state(hitl_response=None, hitl_started_at=None)
        fake_start = 10000.0

        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=fake_start):
            node_result = StateGraphNodes.hitl_wait_node(state)
        state["hitl_started_at"] = node_result["hitl_started_at"]
        assert state["hitl_started_at"] == fake_start

        # Step 2: Not yet expired — router returns HITL_WAIT
        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=fake_start + 10.0):
            route = StateGraphEdges.route_from_hitl_wait(state)
        assert route == SupervisorState.HITL_WAIT.value

        # Step 3: Second entry — node preserves timestamp
        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=fake_start + 20.0):
            node_result2 = StateGraphNodes.hitl_wait_node(state)
        assert "hitl_started_at" not in node_result2  # Not overwritten

        # Step 4: Timeout expired — router returns ERROR
        expired = fake_start + HITL_TIMEOUT_SECONDS + 1.0
        with patch("sky_claw.antigravity.orchestrator.state_graph.time.monotonic", return_value=expired):
            route = StateGraphEdges.route_from_hitl_wait(state)
        assert route == SupervisorState.ERROR.value
        assert "Timeout" in state["last_error"]
