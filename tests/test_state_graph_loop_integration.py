"""Integration tests: AgenticLoopGuardrail + StateGraph dispatching."""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from sky_claw.orchestrator.state_graph import (  # noqa: E402
    StateGraphEdges,
    StateGraphIntegration,
    SupervisorState,
    SupervisorStateGraph,
)


def _base_state(tool_name: str = "patch_plugin", payload: dict | None = None) -> dict:
    return {
        "workflow_id": "wf_test",
        "current_state": SupervisorState.DISPATCHING.value,
        "previous_state": SupervisorState.ANALYZING.value,
        "profile_name": "Default",
        "modlist_path": None,
        "last_mtime": 0.0,
        "pending_event": None,
        "event_data": {},
        "tool_name": tool_name,
        "tool_payload": payload if payload is not None else {"plugin": "foo.esp"},
        "tool_result": None,
        "hitl_request": None,
        "hitl_response": None,
        "transition_history": [],
        "last_error": None,
        "error_count": 0,
        "rollback_triggered": False,
        "rollback_result": None,
        "rollback_transaction_id": None,
        "loop_detected": False,
        "loop_context": None,
    }


def _supervisor_mock(dispatch_result: dict | None = None) -> MagicMock:
    sup = MagicMock()
    sup.dispatch_tool = AsyncMock(return_value=dispatch_result or {"status": "success"})
    return sup


class TestDispatchingLoopDetection:
    async def test_first_two_dispatches_invoke_tool(self) -> None:
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state()
        await integration._on_dispatching(state)
        await integration._on_dispatching(state)

        assert sup.dispatch_tool.await_count == 2
        assert state["loop_detected"] is False
        assert state["tool_result"] == {"status": "success"}

    async def test_third_identical_dispatch_trips_guardrail(self) -> None:
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state()
        await integration._on_dispatching(state)
        await integration._on_dispatching(state)
        await integration._on_dispatching(state)

        assert sup.dispatch_tool.await_count == 2  # 3ra NO invoca la tool
        assert state["loop_detected"] is True
        assert state["hitl_request"]["action_type"] == "circuit_breaker_halt"
        assert state["hitl_request"]["context_data"]["tool_name"] == "patch_plugin"
        assert state["hitl_request"]["context_data"]["occurrences"] == 3
        assert state["loop_context"]["tool_name"] == "patch_plugin"

    async def test_different_args_do_not_trip(self) -> None:
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        for plugin in ("a.esp", "b.esp", "c.esp"):
            state = _base_state(payload={"plugin": plugin})
            await integration._on_dispatching(state)
            assert state["loop_detected"] is False

        assert sup.dispatch_tool.await_count == 3

    async def test_route_from_dispatching_goes_to_hitl_on_loop(self) -> None:
        state = _base_state()
        state["loop_detected"] = True
        assert StateGraphEdges.route_from_dispatching(state) == SupervisorState.HITL_WAIT.value

    async def test_route_from_dispatching_normal_path_when_no_loop(self) -> None:
        state = _base_state()
        state["tool_result"] = {"status": "success"}
        assert StateGraphEdges.route_from_dispatching(state) == SupervisorState.COMPLETED.value

    async def test_hitl_approved_resets_guardrail(self) -> None:
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = MagicMock()
        sup.dispatch_tool = AsyncMock(return_value={"status": "success"})
        sup.interface = MagicMock()
        sup.interface.request_hitl = AsyncMock(return_value="approved")
        sup._create_hitl_request = MagicMock(side_effect=lambda req: req)
        integration.connect_supervisor(sup)

        state = _base_state()
        await integration._on_dispatching(state)
        await integration._on_dispatching(state)
        await integration._on_dispatching(state)
        assert state["loop_detected"] is True
        assert graph.loop_guardrail.snapshot() == ()

        graph.loop_guardrail.register_and_check("patch_plugin", {"plugin": "foo.esp"})
        assert len(graph.loop_guardrail.snapshot()) == 1

        await integration._on_hitl_wait(state)

        assert state["loop_detected"] is False
        assert state["loop_context"] is None
        assert graph.loop_guardrail.snapshot() == ()

    async def test_hitl_denied_does_not_reset_guardrail(self) -> None:
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = MagicMock()
        sup.interface = MagicMock()
        sup.interface.request_hitl = AsyncMock(return_value="denied")
        sup._create_hitl_request = MagicMock(side_effect=lambda req: req)
        integration.connect_supervisor(sup)

        state = _base_state()
        state["loop_detected"] = True
        state["hitl_request"] = {
            "action_type": "circuit_breaker_halt",
            "reason": "Loop detected",
            "context_data": {"tool_name": "t", "occurrences": 1},
        }
        graph.loop_guardrail.register_and_check("t", {"x": 1})
        assert len(graph.loop_guardrail.snapshot()) == 1

        await integration._on_hitl_wait(state)

        assert state["hitl_response"] == "denied"
        assert state["loop_detected"] is True  # unchanged
        assert len(graph.loop_guardrail.snapshot()) == 1

    async def test_dispatch_without_supervisor_is_noop(self) -> None:
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        state = _base_state()
        await integration._on_dispatching(state)
        assert state["loop_detected"] is False
        assert graph.loop_guardrail.snapshot() == ()

    async def test_dispatch_tool_not_called_on_trip(self) -> None:
        """Verifica que dispatch_tool no se ejecuta en la llamada que trippea."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        for _ in range(2):
            s = _base_state()
            await integration._on_dispatching(s)

        tripping_state = _base_state()
        await integration._on_dispatching(tripping_state)

        assert sup.dispatch_tool.await_count == 2
        assert tripping_state["tool_result"] is None
        assert tripping_state["loop_detected"] is True


class TestValidator:
    def test_dispatching_to_hitl_wait_allowed(self) -> None:
        from sky_claw.orchestrator.state_graph import StateGraphValidator

        assert StateGraphValidator.is_valid_transition(SupervisorState.DISPATCHING, SupervisorState.HITL_WAIT)
