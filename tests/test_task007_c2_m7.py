"""
TASK-007 Tests: C-2 Ghost Node Verification + M-7 Fallback Bug Fix.

C-2 (Ghost Node GENERATING_LODS):
- Verifies generating_lods_node exists and returns correct state
- Verifies route_from_dispatching routes to GENERATING_LODS when tool_name == "generate_lods"
- Verifies route_from_generating_lods routes to COMPLETED/ERROR/ROLLING_BACK correctly
- Verifies node is registered in _build_graph() (when LangGraph available)

M-7 (Fallback Bug):
- Verifies _execute_fallback returns ERROR when last_error is set after dispatch
- Verifies _execute_fallback returns COMPLETED on successful dispatch
- Verifies _execute_fallback returns COMPLETED from ANALYZING when no tool_name
- Verifies _execute_fallback stays IDLE for unrecognized event types
- Verifies _execute_fallback stays IDLE when no pending_event
"""

import os
import sys

import pytest

# Insert project root at start of path to avoid conflicts
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from sky_claw.antigravity.orchestrator.state_graph import (  # noqa: E402
    LANGGRAPH_AVAILABLE,
    StateGraphEdges,
    StateGraphNodes,
    SupervisorState,
    SupervisorStateGraph,
    WorkflowEventType,
)

# ---------------------------------------------------------------------------
# C-2: Ghost Node GENERATING_LODS — Verification Tests
# ---------------------------------------------------------------------------


class TestC2GeneratingLodsNode:
    """C-2 FIX: Verify generating_lods_node is not a ghost — exists and is wired."""

    def test_generating_lods_node_returns_correct_state(self):
        """generating_lods_node must return GENERATING_LODS as current_state."""
        state = {"workflow_id": "test_wf"}
        result = StateGraphNodes.generating_lods_node(state)

        assert result["current_state"] == SupervisorState.GENERATING_LODS.value
        assert result["previous_state"] == SupervisorState.DISPATCHING.value

    def test_generating_lods_node_is_static_method(self):
        """generating_lods_node must be callable without instance."""
        # If this were a ghost (undefined), AttributeError would be raised
        assert callable(StateGraphNodes.generating_lods_node)

    def test_generating_lods_node_preserves_workflow_id(self):
        """generating_lods_node should not clobber workflow_id."""
        state = {"workflow_id": "wf_20260425_unique"}
        result = StateGraphNodes.generating_lods_node(state)
        # Node returns only the keys it modifies; workflow_id is untouched
        assert "workflow_id" not in result or result.get("workflow_id") is None


class TestC2RouteFromDispatchingToGeneratingLods:
    """C-2 FIX: Verify DISPATCHING → GENERATING_LODS routing."""

    def test_dispatching_routes_to_generating_lods_when_generate_lods(self):
        """When tool_name is 'generate_lods' and tool_result is success, route to GENERATING_LODS."""
        state = {
            "tool_name": "generate_lods",
            "tool_result": {"status": "success"},
        }
        result = StateGraphEdges.route_from_dispatching(state)
        assert result == SupervisorState.GENERATING_LODS.value

    def test_dispatching_routes_to_generating_lods_when_aborted(self):
        """When tool_name is 'generate_lods' and tool_result is aborted, still route to GENERATING_LODS."""
        state = {
            "tool_name": "generate_lods",
            "tool_result": {"status": "aborted"},
        }
        result = StateGraphEdges.route_from_dispatching(state)
        assert result == SupervisorState.GENERATING_LODS.value

    def test_dispatching_routes_to_completed_for_other_tools(self):
        """Non-generate_lods tools with success should route to COMPLETED, not GENERATING_LODS."""
        state = {
            "tool_name": "query_mod_metadata",
            "tool_result": {"status": "success"},
        }
        result = StateGraphEdges.route_from_dispatching(state)
        assert result == SupervisorState.COMPLETED.value

    def test_dispatching_routes_to_error_on_last_error(self):
        """When last_error is set, dispatching should route to ERROR."""
        state = {
            "tool_name": "generate_lods",
            "last_error": "Tool execution failed",
        }
        result = StateGraphEdges.route_from_dispatching(state)
        assert result == SupervisorState.ERROR.value

    def test_dispatching_routes_to_hitl_wait_on_loop_detected(self):
        """When loop_detected is set, dispatching should route to HITL_WAIT."""
        state = {
            "tool_name": "generate_lods",
            "tool_result": {"status": "success"},
            "loop_detected": True,
        }
        result = StateGraphEdges.route_from_dispatching(state)
        assert result == SupervisorState.HITL_WAIT.value


class TestC2RouteFromGeneratingLods:
    """C-2 FIX: Verify GENERATING_LODS → COMPLETED/ERROR/ROLLING_BACK routing."""

    def test_generating_lods_to_completed_on_success(self):
        """Successful LOD generation should route to COMPLETED."""
        state = {
            "tool_result": {"status": "success"},
        }
        result = StateGraphEdges.route_from_generating_lods(state)
        assert result == SupervisorState.COMPLETED.value

    def test_generating_lods_to_completed_on_aborted(self):
        """Aborted LOD generation should route to COMPLETED."""
        state = {
            "tool_result": {"status": "aborted"},
        }
        result = StateGraphEdges.route_from_generating_lods(state)
        assert result == SupervisorState.COMPLETED.value

    def test_generating_lods_to_completed_on_failed_status_no_error(self):
        """Failed tool_result without last_error falls through to default COMPLETED.

        The routing logic checks last_error first, then tool_result status.
        A 'failed' status without last_error is not treated as an error condition.
        """
        state = {
            "tool_result": {"status": "failed"},
            "last_error": None,
        }
        result = StateGraphEdges.route_from_generating_lods(state)
        assert result == SupervisorState.COMPLETED.value

    def test_generating_lods_to_rolling_back_on_last_error_no_rollback(self):
        """When last_error is set but rollback not yet triggered, route to ROLLING_BACK."""
        state = {
            "tool_result": {"status": "success"},
            "last_error": "Something went wrong",
        }
        result = StateGraphEdges.route_from_generating_lods(state)
        assert result == SupervisorState.ROLLING_BACK.value

    def test_generating_lods_to_error_on_last_error_with_rollback(self):
        """When last_error is set and rollback already triggered, route to ERROR."""
        state = {
            "tool_result": {"status": "failed"},
            "last_error": "DynDOLOD crashed",
            "rollback_triggered": True,
        }
        result = StateGraphEdges.route_from_generating_lods(state)
        assert result == SupervisorState.ERROR.value


class TestC2NodeRegistration:
    """C-2 FIX: Verify GENERATING_LODS node is registered in the graph."""

    @pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not available")
    def test_generating_lods_node_registered_in_graph(self):
        """The compiled graph must contain the 'generating_lods' node."""
        sg = SupervisorStateGraph()
        assert sg.compiled_graph is not None
        # Access the compiled graph's node names
        # LangGraph compiled graphs expose nodes via .get_graph().nodes
        graph_data = sg.compiled_graph.get_graph()
        node_names = {node.name for node in graph_data.nodes.values()}
        assert SupervisorState.GENERATING_LODS.value in node_names

    def test_generating_lods_enum_exists(self):
        """SupervisorState must include GENERATING_LODS."""
        assert hasattr(SupervisorState, "GENERATING_LODS")
        assert SupervisorState.GENERATING_LODS.value == "generating_lods"


# ---------------------------------------------------------------------------
# M-7: Fallback Bug — Fix Verification Tests
# ---------------------------------------------------------------------------


class TestM7FallbackBug:
    """M-7 FIX: _execute_fallback must not blindly overwrite to COMPLETED."""

    @pytest.mark.asyncio
    async def test_fallback_dispatch_with_error_returns_error(self):
        """When last_error is set after dispatch, fallback must return ERROR, not COMPLETED.

        This is the core M-7 bug: previously, the fallback always set COMPLETED
        regardless of whether the tool failed.
        """
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        state["pending_event"] = WorkflowEventType.USER_COMMAND.value
        state["tool_name"] = "generate_lods"
        state["last_error"] = "DynDOLOD execution failed catastrophically"

        result = await sg._execute_fallback(state)

        assert result["current_state"] == SupervisorState.ERROR.value, (
            f"Expected ERROR but got {result['current_state']!r} — M-7 bug not fixed"
        )
        assert result["previous_state"] == SupervisorState.DISPATCHING.value

    @pytest.mark.asyncio
    async def test_fallback_dispatch_success_returns_completed(self):
        """When dispatch succeeds (no last_error), fallback must return COMPLETED."""
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        state["pending_event"] = WorkflowEventType.USER_COMMAND.value
        state["tool_name"] = "generate_lods"
        state["tool_result"] = {"status": "success"}

        result = await sg._execute_fallback(state)

        assert result["current_state"] == SupervisorState.COMPLETED.value
        assert result["previous_state"] == SupervisorState.DISPATCHING.value

    @pytest.mark.asyncio
    async def test_fallback_analysis_only_returns_completed(self):
        """When event is recognized but no tool_name, fallback should complete from ANALYZING."""
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        state["pending_event"] = WorkflowEventType.MODLIST_CHANGED.value
        # No tool_name — analysis only

        result = await sg._execute_fallback(state)

        assert result["current_state"] == SupervisorState.COMPLETED.value
        assert result["previous_state"] == SupervisorState.ANALYZING.value

    @pytest.mark.asyncio
    async def test_fallback_unrecognized_event_stays_idle(self):
        """Unrecognized event types must NOT falsely claim COMPLETED.

        Previously, any pending_event would result in COMPLETED, even if
        the event type was not handled.
        """
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        state["pending_event"] = "unknown_event_type"

        result = await sg._execute_fallback(state)

        assert result["current_state"] == SupervisorState.IDLE.value, (
            f"Expected IDLE for unrecognized event but got {result['current_state']!r}"
        )

    @pytest.mark.asyncio
    async def test_fallback_no_event_stays_idle(self):
        """When no pending_event, fallback should stay IDLE."""
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        # No pending_event set

        result = await sg._execute_fallback(state)

        assert result["current_state"] == SupervisorState.IDLE.value
        assert result["previous_state"] == SupervisorState.INIT.value

    @pytest.mark.asyncio
    async def test_fallback_shutdown_event_stays_idle(self):
        """Shutdown events in fallback should stay IDLE (not falsely COMPLETED)."""
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        state["pending_event"] = WorkflowEventType.SHUTDOWN.value

        result = await sg._execute_fallback(state)

        assert result["current_state"] == SupervisorState.IDLE.value, (
            "Shutdown event should not falsely claim COMPLETED in fallback"
        )

    @pytest.mark.asyncio
    async def test_fallback_timeout_event_stays_idle(self):
        """Timeout events in fallback should stay IDLE (not falsely COMPLETED)."""
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        state["pending_event"] = WorkflowEventType.TIMEOUT.value

        result = await sg._execute_fallback(state)

        assert result["current_state"] == SupervisorState.IDLE.value

    @pytest.mark.asyncio
    async def test_fallback_error_occurred_event_stays_idle(self):
        """ERROR_OCCURRED events in fallback should stay IDLE (not falsely COMPLETED)."""
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        state["pending_event"] = WorkflowEventType.ERROR_OCCURRED.value

        result = await sg._execute_fallback(state)

        assert result["current_state"] == SupervisorState.IDLE.value

    @pytest.mark.asyncio
    async def test_fallback_execute_integration_with_error(self):
        """Integration: execute() → _execute_fallback() must propagate ERROR state."""
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        state["pending_event"] = WorkflowEventType.USER_COMMAND.value
        state["tool_name"] = "xedit_patch"
        state["last_error"] = "xEdit process crashed"

        result = await sg.execute(state)

        assert result["current_state"] == SupervisorState.ERROR.value, (
            f"execute() must propagate ERROR from fallback, got {result['current_state']!r}"
        )


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
