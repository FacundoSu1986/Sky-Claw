"""LangGraph event streamer — emite mensajes 'agent_state' por nodo significativo."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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


class LangGraphEventStreamer:
    """Envuelve SupervisorStateGraph.execute() para emitir 'agent_state' por nodo."""

    def __init__(self, graph: SupervisorStateGraph, interface: InterfaceAgent):
        self.graph = graph
        self.interface = interface

    async def stream_execute(
        self, initial_state: StateGraphState | None = None
    ) -> StateGraphState:
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
                                "message": NODE_MESSAGES.get(
                                    node_name.lower(), f"Nodo: {node_name}"
                                ),
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
