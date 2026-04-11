# -*- coding: utf-8 -*-
"""
Tests for LangGraph StateGraph integration in Sky_Claw.

Tests verify:
- State transitions
- Event handling
- Fallback behavior without LangGraph
- Integration with SupervisorAgent
"""

import os
import sys
import asyncio
import pytest

# Insert project root at start of path to avoid conflicts
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from sky_claw.orchestrator.state_graph import (
    SupervisorState,
    WorkflowEventType,
    SupervisorStateGraph,
    StateGraphNodes,
    StateGraphEdges,
    StateGraphIntegration,
    create_supervisor_state_graph,
    LANGGRAPH_AVAILABLE,
)


class TestSupervisorState:
    """Tests for SupervisorState enum."""
    
    def test_state_values(self):
        """Verify all states have correct string values."""
        assert SupervisorState.INIT.value == "init"
        assert SupervisorState.IDLE.value == "idle"
        assert SupervisorState.WATCHING.value == "watching"
        assert SupervisorState.ANALYZING.value == "analyzing"
        assert SupervisorState.DISPATCHING.value == "dispatching"
        assert SupervisorState.HITL_WAIT.value == "hitl_wait"
        assert SupervisorState.COMPLETED.value == "completed"
        assert SupervisorState.ERROR.value == "error"
    
    def test_state_count(self):
        """Verify we have all expected states."""
        states = list(SupervisorState)
        assert len(states) == 12


class TestWorkflowEventType:
    """Tests for WorkflowEventType enum."""
    
    def test_event_types(self):
        """Verify all event types are defined."""
        assert WorkflowEventType.MODLIST_CHANGED.value == "modlist_changed"
        assert WorkflowEventType.USER_COMMAND.value == "user_command"
        assert WorkflowEventType.TOOL_REQUEST.value == "tool_request"
        assert WorkflowEventType.HITL_RESPONSE.value == "hitl_response"
        assert WorkflowEventType.ERROR_OCCURRED.value == "error_occurred"
        assert WorkflowEventType.TIMEOUT.value == "timeout"
        assert WorkflowEventType.SHUTDOWN.value == "shutdown"


class TestStateGraphNodes:
    """Tests for StateGraph node functions."""
    
    def test_init_node(self):
        """Test init_node returns correct state update."""
        state = {"workflow_id": "test_wf"}
        result = StateGraphNodes.init_node(state)
        
        assert result["current_state"] == SupervisorState.IDLE.value
        assert result["previous_state"] == SupervisorState.INIT.value
    
    def test_idle_node(self):
        """Test idle_node maintains IDLE state."""
        state = {"workflow_id": "test_wf"}
        result = StateGraphNodes.idle_node(state)
        
        assert result["current_state"] == SupervisorState.IDLE.value
    
    def test_analyzing_node(self):
        """Test analyzing_node processes event data."""
        state = {
            "workflow_id": "test_wf",
            "pending_event": WorkflowEventType.MODLIST_CHANGED.value,
            "event_data": {"mtime": 12345.0}
        }
        result = StateGraphNodes.analyzing_node(state)
        
        assert result["current_state"] == SupervisorState.ANALYZING.value
        assert result["previous_state"] == SupervisorState.WATCHING.value
    
    def test_dispatching_node(self):
        """Test dispatching_node logs tool name."""
        state = {
            "workflow_id": "test_wf",
            "tool_name": "query_mod_metadata"
        }
        result = StateGraphNodes.dispatching_node(state)
        
        assert result["current_state"] == SupervisorState.DISPATCHING.value
    
    def test_hitl_wait_node(self):
        """Test hitl_wait_node processes HITL request."""
        state = {
            "workflow_id": "test_wf",
            "hitl_request": {"action_type": "destructive_xedit"}
        }
        result = StateGraphNodes.hitl_wait_node(state)
        
        assert result["current_state"] == SupervisorState.HITL_WAIT.value
    
    def test_completed_node(self):
        """Test completed_node clears pending event."""
        state = {"workflow_id": "test_wf"}
        result = StateGraphNodes.completed_node(state)
        
        assert result["current_state"] == SupervisorState.COMPLETED.value
        assert result["pending_event"] is None
    
    def test_error_node_increments_count(self):
        """Test error_node increments error count."""
        state = {
            "workflow_id": "test_wf",
            "last_error": "Test error",
            "error_count": 0
        }
        result = StateGraphNodes.error_node(state)
        
        assert result["current_state"] == SupervisorState.ERROR.value
        assert result["error_count"] == 1


class TestStateGraphEdges:
    """Tests for conditional edge routing."""
    
    def test_route_from_idle_modlist_changed(self):
        """Test routing from IDLE on modlist change."""
        state = {"pending_event": WorkflowEventType.MODLIST_CHANGED.value}
        result = StateGraphEdges.route_from_idle(state)
        assert result == SupervisorState.WATCHING.value
    
    def test_route_from_idle_user_command(self):
        """Test routing from IDLE on user command."""
        state = {"pending_event": WorkflowEventType.USER_COMMAND.value}
        result = StateGraphEdges.route_from_idle(state)
        assert result == SupervisorState.ANALYZING.value
    
    def test_route_from_idle_tool_request(self):
        """Test routing from IDLE on tool request."""
        state = {"pending_event": WorkflowEventType.TOOL_REQUEST.value}
        result = StateGraphEdges.route_from_idle(state)
        assert result == SupervisorState.DISPATCHING.value
    
    def test_route_from_idle_shutdown(self):
        """Test routing from IDLE on shutdown."""
        state = {"pending_event": WorkflowEventType.SHUTDOWN.value}
        result = StateGraphEdges.route_from_idle(state)
        assert result == "__END__"
    
    def test_route_from_idle_no_event(self):
        """Test routing from IDLE with no event."""
        state = {"pending_event": None}
        result = StateGraphEdges.route_from_idle(state)
        assert result == SupervisorState.IDLE.value
    
    def test_route_from_hitl_wait_approved(self):
        """Test routing from HITL_WAIT when approved."""
        state = {"hitl_response": "approved"}
        result = StateGraphEdges.route_from_hitl_wait(state)
        assert result == SupervisorState.DISPATCHING.value
    
    def test_route_from_hitl_wait_denied(self):
        """Test routing from HITL_WAIT when denied."""
        state = {"hitl_response": "denied"}
        result = StateGraphEdges.route_from_hitl_wait(state)
        assert result == SupervisorState.COMPLETED.value
    
    def test_route_from_hitl_wait_timeout(self):
        """Test routing from HITL_WAIT on timeout."""
        state = {"hitl_response": "timeout"}
        result = StateGraphEdges.route_from_hitl_wait(state)
        assert result == SupervisorState.ERROR.value
    
    def test_route_from_error_max_retries(self):
        """Test routing from ERROR when max retries exceeded."""
        state = {"error_count": 3}
        result = StateGraphEdges.route_from_error(state)
        assert result == "__END__"
    
    def test_route_from_error_retry(self):
        """Test routing from ERROR when retries available."""
        state = {"error_count": 1}
        result = StateGraphEdges.route_from_error(state)
        assert result == SupervisorState.IDLE.value


class TestSupervisorStateGraph:
    """Tests for SupervisorStateGraph class."""
    
    def test_create_instance(self):
        """Test creating a SupervisorStateGraph instance."""
        sg = SupervisorStateGraph(profile_name="TestProfile")
        assert sg.profile_name == "TestProfile"
    
    def test_get_initial_state(self):
        """Test initial state has correct structure."""
        sg = SupervisorStateGraph()
        state = sg.get_initial_state()
        
        assert "workflow_id" in state
        assert state["current_state"] == SupervisorState.INIT.value
        assert state["profile_name"] == "Default"
        assert state["error_count"] == 0
    
    def test_get_mermaid_diagram(self):
        """Test Mermaid diagram generation."""
        sg = SupervisorStateGraph()
        diagram = sg.get_mermaid_diagram()
        
        assert "mermaid" in diagram
        assert "stateDiagram" in diagram
        assert "INIT" in diagram
        assert "IDLE" in diagram
        assert "DISPATCHING" in diagram
    
    def test_get_state_summary(self):
        """Test state summary generation."""
        sg = SupervisorStateGraph()
        state = {
            "workflow_id": "test_wf",
            "current_state": "analyzing",
            "previous_state": "idle",
            "pending_event": "modlist_changed",
            "tool_name": "query_mod_metadata",
            "error_count": 0,
            "last_error": None,
            "transition_history": [{"from": "idle", "to": "analyzing"}]
        }
        summary = sg.get_state_summary(state)
        
        assert summary["workflow_id"] == "test_wf"
        assert summary["current_state"] == "analyzing"
        assert summary["transitions"] == 1
    
    @pytest.mark.asyncio
    async def test_execute_fallback(self):
        """Test fallback execution without LangGraph."""
        sg = SupervisorStateGraph()
        initial_state = sg.get_initial_state()
        
        result = await sg.execute(initial_state)
        
        # Should complete even without LangGraph
        assert result is not None
        assert "current_state" in result
    
    @pytest.mark.asyncio
    async def test_submit_event(self):
        """Test submitting an event to the workflow."""
        sg = SupervisorStateGraph()
        
        result = await sg.submit_event(
            WorkflowEventType.USER_COMMAND,
            {"command": "test_command"}
        )
        
        assert result is not None
        assert result["pending_event"] == WorkflowEventType.USER_COMMAND.value


class TestStateGraphIntegration:
    """Tests for StateGraphIntegration helper class."""
    
    def test_create_integration(self):
        """Test creating integration helper."""
        sg = SupervisorStateGraph()
        integration = StateGraphIntegration(sg)
        assert integration.state_graph is sg
    
    def test_translate_modlist_event(self):
        """Test translating modlist change event."""
        sg = SupervisorStateGraph()
        integration = StateGraphIntegration(sg)
        
        result = integration.translate_modlist_event(
            mtime=12345.0,
            path="/test/modlist.txt"
        )
        
        assert result["event_type"] == WorkflowEventType.MODLIST_CHANGED
        assert result["event_data"]["mtime"] == 12345.0
        assert result["event_data"]["path"] == "/test/modlist.txt"
    
    def test_translate_user_command(self):
        """Test translating user command event."""
        sg = SupervisorStateGraph()
        integration = StateGraphIntegration(sg)
        
        result = integration.translate_user_command(
            command="query_mod",
            params={"mod_id": 123}
        )
        
        assert result["event_type"] == WorkflowEventType.USER_COMMAND
        assert result["event_data"]["command"] == "query_mod"
        assert result["event_data"]["params"]["mod_id"] == 123


class TestFactoryFunction:
    """Tests for factory function."""
    
    def test_create_supervisor_state_graph(self):
        """Test factory function creates correct instance."""
        sg = create_supervisor_state_graph(profile_name="CustomProfile")
        
        assert isinstance(sg, SupervisorStateGraph)
        assert sg.profile_name == "CustomProfile"


class TestGracefulDegradation:
    """Tests for graceful degradation when LangGraph unavailable."""
    
    def test_langgraph_flag_exists(self):
        """Test LANGGRAPH_AVAILABLE flag is defined."""
        assert isinstance(LANGGRAPH_AVAILABLE, bool)
    
    @pytest.mark.asyncio
    async def test_works_without_langgraph(self):
        """Test that basic functionality works without LangGraph."""
        sg = SupervisorStateGraph()
        
        # Should not raise even without LangGraph
        state = sg.get_initial_state()
        assert state is not None
        
        result = await sg.execute(state)
        assert result is not None
    
    def test_visualize_returns_none_without_langgraph(self):
        """Test visualize returns None when LangGraph unavailable."""
        if LANGGRAPH_AVAILABLE:
            pytest.skip("LangGraph is available, skipping degradation test")
        
        sg = SupervisorStateGraph()
        result = sg.visualize()
        assert result is None


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
