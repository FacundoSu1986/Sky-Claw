"""Event streamers — LangGraph node events + FASE 1.5.4 granular tool events.

Two streamer classes:

1. ``LangGraphEventStreamer`` — wraps SupervisorStateGraph.execute() and
   emits ``agent_state`` events for significant graph nodes.
2. ``ToolEventStreamer`` — emits granular tool lifecycle events:
   ``tool_started``, ``tool_progress``, ``tool_completed``,
   ``tool_failed``, ``tool_requires_approval``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.comms.interface import InterfaceAgent
    from sky_claw.orchestrator.state_graph import (
        StateGraphState,
        SupervisorStateGraph,
    )

logger = logging.getLogger("SkyClaw.WSEventStreamer")

# Solo nodos significativos (filtra ruido de INIT/IDLE/WATCHING)
SIGNIFICANT_NODES = {"analyzing", "dispatching", "hitl_wait", "completed", "error"}

# Mensajes amigables por nodo
NODE_MESSAGES = {
    "analyzing": "Analizando conflictos de carga…",
    "dispatching": "Ejecutando herramienta…",
    "hitl_wait": "Esperando aprobación humana…",
    "completed": "Operación completada.",
    "error": "Error en el workflow.",
}

# FASE 1.5.4: Tool event types emitted to the frontend
TOOL_EVENT_STARTED = "tool_started"
TOOL_EVENT_PROGRESS = "tool_progress"
TOOL_EVENT_COMPLETED = "tool_completed"
TOOL_EVENT_FAILED = "tool_failed"
TOOL_EVENT_REQUIRES_APPROVAL = "tool_requires_approval"


class LangGraphEventStreamer:
    """Envuelve SupervisorStateGraph.execute() para emitir 'agent_state' por nodo."""

    def __init__(self, graph: SupervisorStateGraph, interface: InterfaceAgent):
        self.graph = graph
        self.interface = interface

    async def stream_execute(self, initial_state: StateGraphState | None = None) -> StateGraphState:
        """Ejecuta el grafo mientras transmite eventos 'agent_state' por nodo significativo."""
        state = initial_state or self.graph.get_initial_state()
        config = {"configurable": {"thread_id": state["workflow_id"]}}
        last_state: StateGraphState = state

        try:
            async for update in self.graph.compiled_graph.astream(state, config):
                # astream yields {node_name: state_delta}
                for node_name, delta in update.items():
                    if node_name.lower() in SIGNIFICANT_NODES:
                        await self.interface.send_event(
                            "agent_state",
                            {
                                "node": node_name,
                                "event": "update",
                                "message": NODE_MESSAGES.get(node_name.lower(), f"Nodo: {node_name}"),
                            },
                        )
                    if delta:
                        last_state = {**last_state, **delta}
        except Exception as e:
            logger.exception("Error en stream_execute: %s", e)
            await self.interface.send_event(
                "agent_state",
                {
                    "node": "error",
                    "event": "error",
                    "message": f"Fallo en workflow: {e}",
                },
            )
            last_state["last_error"] = str(e)
        return last_state


# ---------------------------------------------------------------------------
# FASE 1.5.4: Tool Event Streamer
# ---------------------------------------------------------------------------


class ToolEventStreamer:
    """Emits granular tool lifecycle events via InterfaceAgent.

    Each method sends a single event frame to the frontend with a structured
    payload so the UI can render spinners, progress bars, and approval dialogs
    instead of assuming optimistic success.

    Args:
        interface: The InterfaceAgent used to send events to the frontend.
    """

    def __init__(self, interface: InterfaceAgent) -> None:
        self._interface = interface

    async def emit_started(
        self,
        tool_name: str,
        task_id: str,
        payload_keys: list[str] | None = None,
    ) -> None:
        """Emit ``tool_started`` event."""
        await self._safe_emit(TOOL_EVENT_STARTED, {
            "tool": tool_name,
            "task_id": task_id,
            "payload_keys": payload_keys or [],
        })

    async def emit_progress(
        self,
        tool_name: str,
        task_id: str,
        *,
        progress: float | None = None,
        message: str = "",
    ) -> None:
        """Emit ``tool_progress`` event."""
        await self._safe_emit(TOOL_EVENT_PROGRESS, {
            "tool": tool_name,
            "task_id": task_id,
            "progress": progress,
            "message": message,
        })

    async def emit_completed(
        self,
        tool_name: str,
        task_id: str,
        *,
        status: str = "success",
    ) -> None:
        """Emit ``tool_completed`` event."""
        await self._safe_emit(TOOL_EVENT_COMPLETED, {
            "tool": tool_name,
            "task_id": task_id,
            "status": status,
        })

    async def emit_failed(
        self,
        tool_name: str,
        task_id: str,
        *,
        error: str = "",
        error_type: str = "",
    ) -> None:
        """Emit ``tool_failed`` event."""
        await self._safe_emit(TOOL_EVENT_FAILED, {
            "tool": tool_name,
            "task_id": task_id,
            "error": error,
            "error_type": error_type,
        })

    async def emit_requires_approval(
        self,
        tool_name: str,
        task_id: str,
        *,
        reason: str = "",
        timeout: float = 120.0,
    ) -> None:
        """Emit ``tool_requires_approval`` event for HITL flow."""
        await self._safe_emit(TOOL_EVENT_REQUIRES_APPROVAL, {
            "tool": tool_name,
            "task_id": task_id,
            "reason": reason,
            "timeout": timeout,
        })

    async def _safe_emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit event, swallowing errors to avoid disrupting tool execution."""
        try:
            await self._interface.send_event(event_type, payload)
        except Exception:
            logger.debug(
                "ToolEventStreamer: failed to emit %s",
                event_type,
                exc_info=True,
            )
