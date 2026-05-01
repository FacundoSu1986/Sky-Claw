"""TASK-003 (M-1) Validation: StateGraphIntegration wiring + callback-aware nodes.

Tests that the cognitive circuit breaker and HITL flow are activated
through the graph when StateGraphIntegration is connected.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import pytest  # noqa: E402

from sky_claw.antigravity.orchestrator.state_graph import (  # noqa: E402
    LANGGRAPH_AVAILABLE,
    StateGraphEdges,
    StateGraphIntegration,
    SupervisorState,
    SupervisorStateGraph,
)

pytestmark = pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")


def _base_state(
    current_state: str = SupervisorState.DISPATCHING.value,
    tool_name: str = "patch_plugin",
    payload: dict | None = None,
) -> dict:
    return {
        "workflow_id": "wf_test_wiring",
        "current_state": current_state,
        "previous_state": SupervisorState.ANALYZING.value,
        "profile_name": "Test",
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
        "hitl_started_at": None,
        "loop_detected": False,
        "loop_context": None,
    }


def _supervisor_mock(dispatch_result: dict | None = None) -> MagicMock:
    sup = MagicMock()
    sup.dispatch_tool = AsyncMock(return_value=dispatch_result or {"status": "success"})
    sup._trigger_proactive_analysis = AsyncMock()
    sup.interface = MagicMock()
    sup.interface.request_hitl = AsyncMock(return_value="approved")
    sup._create_hitl_request = MagicMock(return_value={"action_type": "test"})
    return sup


class TestCallbackAwareNodeWrapper:
    """Tests for _make_callback_aware_node wrapper mechanism."""

    async def test_wrapper_invokes_registered_callback(self) -> None:
        """Callback registrado se invoca antes de ejecutar el nodo."""
        graph = SupervisorStateGraph(profile_name="Test")
        callback_called = False

        async def my_callback(state: dict) -> None:
            nonlocal callback_called
            callback_called = True

        graph.register_callback(SupervisorState.DISPATCHING, my_callback)

        # Obtener el nodo wrapped del grafo compilado
        node_fn = graph._make_callback_aware_node(
            SupervisorState.DISPATCHING.value,
            lambda s: {"current_state": SupervisorState.DISPATCHING.value},
        )

        state = _base_state()
        result = await node_fn(state)

        assert callback_called is True
        assert result["current_state"] == SupervisorState.DISPATCHING.value

    async def test_wrapper_works_without_callback(self) -> None:
        """Sin callback registrado, el nodo se ejecuta normalmente."""
        graph = SupervisorStateGraph(profile_name="Test")

        node_fn = graph._make_callback_aware_node(
            SupervisorState.IDLE.value,
            lambda s: {"current_state": SupervisorState.IDLE.value},
        )

        state = _base_state(current_state=SupervisorState.IDLE.value)
        result = await node_fn(state)

        assert result["current_state"] == SupervisorState.IDLE.value

    async def test_callback_can_modify_state_in_place(self) -> None:
        """El callback puede modificar el estado antes de que el nodo retorne."""

        async def modify_callback(state: dict) -> None:
            state["tool_result"] = {"status": "modified_by_callback"}

        graph = SupervisorStateGraph(profile_name="Test")
        graph.register_callback(SupervisorState.DISPATCHING, modify_callback)

        node_fn = graph._make_callback_aware_node(
            SupervisorState.DISPATCHING.value,
            lambda s: {"current_state": SupervisorState.DISPATCHING.value},
        )

        state = _base_state()
        await node_fn(state)

        assert state["tool_result"] == {"status": "modified_by_callback"}


class TestStateGraphIntegrationWiring:
    """Tests for StateGraphIntegration.connect_supervisor wiring."""

    async def test_connect_supervisor_registers_three_callbacks(self) -> None:
        """connect_supervisor registra callbacks para ANALYZING, DISPATCHING, HITL_WAIT."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        assert "analyzing_callback" in graph._callbacks
        assert "dispatching_callback" in graph._callbacks
        assert "hitl_wait_callback" in graph._callbacks

    async def test_dispatching_callback_invokes_guardrail(self) -> None:
        """El callback de DISPATCHING invoca el guardrail antes de dispatch."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state()
        await graph._callbacks["dispatching_callback"](state)

        assert sup.dispatch_tool.await_count == 1
        assert state["tool_result"] == {"status": "success"}
        assert state["loop_detected"] is False

    async def test_dispatching_callback_trips_on_loop(self) -> None:
        """El callback de DISPATCHING detecta bucles y marca loop_detected."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state()
        # Invocar 3 veces la misma tool para disparar el guardrail
        await integration._on_dispatching(state)
        await integration._on_dispatching(state)
        await integration._on_dispatching(state)

        assert state["loop_detected"] is True
        assert state["hitl_request"] is not None
        assert state["hitl_request"]["action_type"] == "circuit_breaker_halt"

    async def test_hitl_callback_requests_approval(self) -> None:
        """El callback de HITL_WAIT solicita aprobación humana."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state(current_state=SupervisorState.HITL_WAIT.value)
        state["hitl_request"] = {"action_type": "circuit_breaker_halt", "reason": "Loop"}

        await graph._callbacks["hitl_wait_callback"](state)

        sup.interface.request_hitl.assert_awaited_once()
        assert state["hitl_response"] == "approved"

    async def test_hitl_callback_resets_guardrail_on_approve(self) -> None:
        """Cuando HITL aprueba, el guardrail se resetea y loop_detected se limpia."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state(current_state=SupervisorState.HITL_WAIT.value)
        state["hitl_request"] = {"action_type": "circuit_breaker_halt"}
        state["loop_detected"] = True

        await graph._callbacks["hitl_wait_callback"](state)

        assert state["loop_detected"] is False
        assert state["loop_context"] is None

    async def test_analyzing_callback_triggers_proactive_analysis(self) -> None:
        """El callback de ANALYZING invoca _trigger_proactive_analysis."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state(current_state=SupervisorState.ANALYZING.value)
        await graph._callbacks["analyzing_callback"](state)

        sup._trigger_proactive_analysis.assert_awaited_once()


class TestLoopDetectionRoutesToHITL:
    """Tests that loop detection through the graph routes to HITL_WAIT."""

    def test_route_from_dispatching_goes_to_hitl_on_loop(self) -> None:
        """route_from_dispatching transita a HITL_WAIT cuando loop_detected=True."""
        state = _base_state()
        state["loop_detected"] = True

        result = StateGraphEdges.route_from_dispatching(state)

        assert result == SupervisorState.HITL_WAIT.value

    def test_route_from_hitl_wait_stays_on_none_response(self) -> None:
        """route_from_hitl_wait permanece en HITL_WAIT si response es None y timeout no expirado.

        TASK-005 FIX: hitl_started_at must be set for a legitimate wait.
        Without it, the state is inconsistent and routes to ERROR.
        """
        import time

        state = _base_state(current_state=SupervisorState.HITL_WAIT.value)
        state["hitl_response"] = None
        state["hitl_started_at"] = time.monotonic()  # TASK-005: required for legitimate wait

        result = StateGraphEdges.route_from_hitl_wait(state)

        assert result == SupervisorState.HITL_WAIT.value

    def test_route_from_hitl_wait_goes_to_dispatching_on_approve(self) -> None:
        """route_from_hitl_wait transita a DISPATCHING si response es approved."""
        state = _base_state(current_state=SupervisorState.HITL_WAIT.value)
        state["hitl_response"] = "approved"

        result = StateGraphEdges.route_from_hitl_wait(state)

        assert result == SupervisorState.DISPATCHING.value

    def test_route_from_hitl_wait_goes_to_error_on_timeout(self) -> None:
        """route_from_hitl_wait transita a ERROR si response es timeout."""
        state = _base_state(current_state=SupervisorState.HITL_WAIT.value)
        state["hitl_response"] = "timeout"

        result = StateGraphEdges.route_from_hitl_wait(state)

        assert result == SupervisorState.ERROR.value


class TestDispatchingCallbackInvalidTool:
    """Edge cases for dispatching callback with invalid tool_name."""

    async def test_dispatching_callback_rejects_none_tool_name(self) -> None:
        """El callback rechaza tool_name=None sin invocar dispatch."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state()
        state["tool_name"] = None

        await integration._on_dispatching(state)

        sup.dispatch_tool.assert_not_awaited()
        assert state["last_error"] is not None

    async def test_dispatching_callback_rejects_non_string_tool_name(self) -> None:
        """El callback rechaza tool_name no-string sin invocar dispatch."""
        graph = SupervisorStateGraph(profile_name="Test")
        integration = StateGraphIntegration(graph)
        sup = _supervisor_mock()
        integration.connect_supervisor(sup)

        state = _base_state()
        state["tool_name"] = 12345

        await integration._on_dispatching(state)

        sup.dispatch_tool.assert_not_awaited()
        assert state["last_error"] is not None
