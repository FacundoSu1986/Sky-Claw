# -*- coding: utf-8 -*-
"""
LangGraph StateGraph Integration for Sky-Claw SupervisorAgent.

This module implements a stateful workflow graph using LangGraph fororchestrating agent operations with:
- Defined states for each phase
- Conditional transitions
- Checkpointing support
- Graph visualization

Gracefully degrades when LangGraph is not installed.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Callable, TypedDict
from datetime import datetime

# Conditional imports for graceful degradation
try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.constants import START

    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph = object
    END = "__END__"
    START = "__START__"
    MemorySaver = None

try:
    from pydantic import BaseModel, Field, field_validator  # noqa: F401

    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    BaseModel = object

    def Field(*args, **kwargs):
        return None


logger = logging.getLogger("SkyClaw.StateGraph")


# =============================================================================
# State Definitions
# =============================================================================


class SupervisorState(str, Enum):
    """Estados del SupervisorAgent en el workflow de LangGraph."""

    INIT = "init"
    IDLE = "idle"
    WATCHING = "watching"
    ANALYZING = "analyzing"
    DISPATCHING = "dispatching"
    HITL_WAIT = "hitl_wait"
    COMPLETED = "completed"
    ERROR = "error"
    # FASE 1.5: Estados de Rollback para resiliencia
    ROLLING_BACK = "rolling_back"
    ERROR_FATAL = "error_fatal"
    # FASE 2: Estado de Parcheo Transaccional
    PATCHING = "patching"
    # FASE 4: Estado de generación de LODs
    GENERATING_LODS = "generating_lods"


class WorkflowEventType(str, Enum):
    """Tipos de eventos que pueden disparar transiciones."""

    MODLIST_CHANGED = "modlist_changed"
    USER_COMMAND = "user_command"
    TOOL_REQUEST = "tool_request"
    HITL_RESPONSE = "hitl_response"
    ERROR_OCCURRED = "error_occurred"
    TIMEOUT = "timeout"
    SHUTDOWN = "shutdown"


# BUG-003 FIX: Clase validadora de transiciones de estado
class StateGraphValidator:
    """Validador de transiciones de estado.

    BUG-003 FIX: Implementa validación estricta de transiciones permitidas
    para prevenir estados inválidos en el workflow.
    """

    _VALID_TRANSITIONS: Dict[SupervisorState, set[SupervisorState]] = {
        SupervisorState.INIT: {SupervisorState.IDLE, SupervisorState.ERROR},
        SupervisorState.IDLE: {
            SupervisorState.WATCHING,
            SupervisorState.ANALYZING,
            SupervisorState.DISPATCHING,
            SupervisorState.ERROR,
        },
        SupervisorState.WATCHING: {
            SupervisorState.ANALYZING,
            SupervisorState.IDLE,
            SupervisorState.ERROR,
        },
        SupervisorState.ANALYZING: {
            SupervisorState.DISPATCHING,
            SupervisorState.HITL_WAIT,
            SupervisorState.COMPLETED,
            SupervisorState.IDLE,
            SupervisorState.ERROR,
            SupervisorState.PATCHING,
        },
        SupervisorState.DISPATCHING: {
            SupervisorState.COMPLETED,
            SupervisorState.IDLE,
            SupervisorState.ERROR,
            SupervisorState.GENERATING_LODS,
        },
        SupervisorState.HITL_WAIT: {
            SupervisorState.DISPATCHING,
            SupervisorState.COMPLETED,
            SupervisorState.ERROR,
        },
        SupervisorState.COMPLETED: {SupervisorState.IDLE},
        # FASE 1.5: Transiciones de rollback - ERROR puede transicionar a ROLLING_BACK
        SupervisorState.ERROR: {
            SupervisorState.IDLE,
            SupervisorState.INIT,
            SupervisorState.ROLLING_BACK,
        },
        # ROLLING_BACK puede terminar en IDLE (éxito) o ERROR_FATAL (fallo)
        SupervisorState.ROLLING_BACK: {
            SupervisorState.IDLE,
            SupervisorState.ERROR_FATAL,
        },
        # ERROR_FATAL es terminal - solo puede ir a END
        SupervisorState.ERROR_FATAL: set(),  # Estado terminal
        # FASE 2: Transiciones de parcheo transaccional
        SupervisorState.PATCHING: {
            SupervisorState.COMPLETED,
            SupervisorState.ERROR,
            SupervisorState.ROLLING_BACK,
        },
        # FASE 4: Transiciones de generación de LODs
        SupervisorState.GENERATING_LODS: {
            SupervisorState.COMPLETED,
            SupervisorState.ERROR,
            SupervisorState.ROLLING_BACK,
        },
    }

    @classmethod
    def valid_transitions(cls) -> Dict[SupervisorState, set[SupervisorState]]:
        """Retorna el diccionario de transiciones válidas."""
        return cls._VALID_TRANSITIONS.copy()

    @classmethod
    def is_valid_transition(
        cls, from_state: SupervisorState, to_state: SupervisorState
    ) -> bool:
        """Verifica si una transición es válida.

        Args:
            from_state: Estado origen
            to_state: Estado destino

        Returns:
            True si la transición está permitida
        """
        valid_targets = cls._VALID_TRANSITIONS.get(from_state, set())
        return to_state in valid_targets

    @classmethod
    def validate_transition(
        cls, from_state: SupervisorState, to_state: SupervisorState
    ) -> None:
        """Valida una transición y lanza excepción si es inválida.

        Args:
            from_state: Estado origen
            to_state: Estado destino

        Raises:
            ValueError: Si la transición no está permitida
        """
        if not cls.is_valid_transition(from_state, to_state):
            logger.error(
                "BUG-003: Transición inválida detectada: %s -> %s",
                from_state.value,
                to_state.value,
            )
            raise ValueError(
                f"Transición de estado inválida: {from_state.value} -> {to_state.value}"
            )


if PYDANTIC_AVAILABLE:

    class WorkflowState(BaseModel):
        """Estado del workflow de LangGraph para Sky-Claw."""

        # Identificación
        workflow_id: str = Field(
            default_factory=lambda: f"wf_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        )

        # Estado actual
        current_state: SupervisorState = Field(default=SupervisorState.INIT)
        previous_state: Optional[SupervisorState] = None

        # Datos del contexto
        profile_name: str = Field(default="Default")
        modlist_path: Optional[str] = None
        last_mtime: float = 0.0

        # Evento actual
        pending_event: Optional[WorkflowEventType] = None
        event_data: Dict[str, Any] = Field(default_factory=dict)

        # Resultados de herramientas
        tool_name: Optional[str] = None
        tool_payload: Dict[str, Any] = Field(default_factory=dict)
        tool_result: Optional[Dict[str, Any]] = None

        # HITL
        hitl_request: Optional[Dict[str, Any]] = None
        hitl_response: Optional[str] = None  # "approved", "denied", "timeout"

        # Historial de transiciones
        transition_history: List[Dict[str, Any]] = Field(default_factory=list)

        # Errores
        last_error: Optional[str] = None
        error_count: int = 0

        # FASE 1.5: Campos de rollback para resiliencia
        rollback_triggered: bool = Field(default=False)
        rollback_result: Optional[Dict[str, Any]] = None
        rollback_transaction_id: Optional[int] = None

        # Metadata
        created_at: datetime = Field(default_factory=datetime.utcnow)
        updated_at: datetime = Field(default_factory=datetime.utcnow)

        def add_transition(
            self,
            from_state: SupervisorState,
            to_state: SupervisorState,
            reason: str = "",
        ) -> None:
            """Registra una transición en el historial."""
            self.transition_history.append(
                {
                    "from": from_state.value,
                    "to": to_state.value,
                    "reason": reason,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )
            self.updated_at = datetime.utcnow()

else:
    # Fallback sin Pydantic
    class WorkflowState:
        """Estado del workflow sin Pydantic."""

        def __init__(self):
            self.workflow_id = f"wf_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            self.current_state = SupervisorState.INIT
            self.previous_state = None
            self.profile_name = "Default"
            self.modlist_path = None
            self.last_mtime = 0.0
            self.pending_event = None
            self.event_data = {}
            self.tool_name = None
            self.tool_payload = {}
            self.tool_result = None
            self.hitl_request = None
            self.hitl_response = None
            self.transition_history = []
            self.last_error = None
            self.error_count = 0
            # FASE 1.5: Campos de rollback
            self.rollback_triggered = False
            self.rollback_result = None
            self.rollback_transaction_id = None
            self.created_at = datetime.utcnow()
            self.updated_at = datetime.utcnow()


# TypedDict para LangGraph (requerido para anotaciones de estado)
class StateGraphState(TypedDict):
    """Estado tipado para LangGraph StateGraph."""

    workflow_id: str
    current_state: str
    previous_state: Optional[str]
    profile_name: str
    modlist_path: Optional[str]
    last_mtime: float
    pending_event: Optional[str]
    event_data: Dict[str, Any]
    tool_name: Optional[str]
    tool_payload: Dict[str, Any]
    tool_result: Optional[Dict[str, Any]]
    hitl_request: Optional[Dict[str, Any]]
    hitl_response: Optional[str]
    transition_history: List[Dict[str, Any]]
    last_error: Optional[str]
    error_count: int
    # FASE 1.5: Campos de rollback para resiliencia
    rollback_triggered: bool
    rollback_result: Optional[Dict[str, Any]]
    rollback_transaction_id: Optional[int]


# =============================================================================
# Node Functions
# =============================================================================


class StateGraphNodes:
    """Nodos del grafo de estados para Sky-Claw."""

    @staticmethod
    def init_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de inicialización del workflow."""
        logger.info(f"[StateGraph] Inicializando workflow: {state.get('workflow_id')}")
        return {
            "current_state": SupervisorState.IDLE.value,
            "previous_state": SupervisorState.INIT.value,
        }

    @staticmethod
    def idle_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de espera - esperando eventos."""
        logger.debug("[StateGraph] En estado IDLE, esperando eventos...")
        return {"current_state": SupervisorState.IDLE.value}

    @staticmethod
    def watching_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de monitoreo - detectando cambios."""
        logger.debug("[StateGraph] Monitoreando cambios en modlist...")
        return {"current_state": SupervisorState.WATCHING.value}

    @staticmethod
    def analyzing_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de análisis - procesando cambios detectados."""
        state.get("event_data", {})
        logger.info(f"[StateGraph] Analizando evento: {state.get('pending_event')}")
        return {
            "current_state": SupervisorState.ANALYZING.value,
            "previous_state": SupervisorState.WATCHING.value,
        }

    @staticmethod
    def dispatching_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de despacho - ejecutando herramientas."""
        tool_name = state.get("tool_name")
        logger.info(f"[StateGraph] Despachando herramienta: {tool_name}")
        return {
            "current_state": SupervisorState.DISPATCHING.value,
            "previous_state": SupervisorState.ANALYZING.value,
        }

    @staticmethod
    def hitl_wait_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de espera HITL - esperando aprobación humana."""
        hitl_request = state.get("hitl_request", {})
        logger.info(
            f"[StateGraph] Esperando aprobación HITL: {hitl_request.get('action_type')}"
        )
        return {
            "current_state": SupervisorState.HITL_WAIT.value,
            "previous_state": SupervisorState.DISPATCHING.value,
        }

    @staticmethod
    def completed_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de completado - operación finalizada."""
        logger.info("[StateGraph] Operación completada exitosamente")
        return {
            "current_state": SupervisorState.COMPLETED.value,
            "previous_state": SupervisorState.DISPATCHING.value,
            "pending_event": None,
        }

    @staticmethod
    def error_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de error - manejo de errores."""
        error_count = state.get("error_count", 0) + 1
        logger.error(
            f"[StateGraph] Error en workflow (intento {error_count}): {state.get('last_error')}"
        )
        return {
            "current_state": SupervisorState.ERROR.value,
            "error_count": error_count,
        }

    # FASE 1.5: Nodos de rollback para resiliencia
    @staticmethod
    def rolling_back_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de rollback - ejecuta reversión de operaciones fallidas."""
        logger.info(
            f"[StateGraph] Iniciando rollback para transaction: {state.get('rollback_transaction_id')}"
        )
        return {
            "current_state": SupervisorState.ROLLING_BACK.value,
            "previous_state": SupervisorState.ERROR.value,
            "rollback_triggered": True,
        }

    @staticmethod
    def error_fatal_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de error fatal - estado terminal sin recuperación posible."""
        logger.critical(
            f"[StateGraph] Error fatal detectado. Rollback falló: {state.get('last_error')}"
        )
        return {
            "current_state": SupervisorState.ERROR_FATAL.value,
            "previous_state": SupervisorState.ROLLING_BACK.value,
            "rollback_triggered": False,
            "rollback_result": {
                "success": False,
                "reason": "Rollback failed, fatal error",
            },
        }

    # FASE 2: Nodo de parcheo transaccional
    @staticmethod
    def patching_node(state: StateGraphState) -> Dict[str, Any]:
        """Nodo de parcheo transaccional - ejecuta resolución de conflictos.

        Este nodo representa el estado intermedio donde se ejecuta el protocolo
        transaccional de parcheo. El estado resultante dependerá de:
        - Éxito -> COMPLETED
        - Fallo con rollback exitoso -> IDLE (o ERROR para reintento)
        - Fallo crítico de rollback -> ERROR_FATAL

        Args:
            state: Estado actual del workflow.

        Returns:
            Dict con el nuevo estado y datos del resultado de parcheo.
        """
        logger.info("[StateGraph] Iniciando nodo de parcheo transaccional")
        event_data = state.get("event_data", {})

        # Extraer datos del conflicto
        conflict_report = event_data.get("conflict_report")
        event_data.get("target_plugin")

        if not conflict_report:
            logger.warning("[StateGraph] No hay conflict_report, regresando a IDLE")
            return {
                "current_state": SupervisorState.IDLE.value,
                "previous_state": SupervisorState.PATCHING.value,
            }

        # El resultado del parcheo se determinará por el callback externo
        # Este nodo solo actualiza el estado, la lógica está en supervisor.resolve_conflict_with_patch
        return {
            "current_state": SupervisorState.PATCHING.value,
            "previous_state": SupervisorState.ANALYZING.value,
            "event_data": event_data,
        }


# =============================================================================
# Conditional Edges
# =============================================================================


class StateGraphEdges:
    """Aristas condicionales del grafo de estados."""

    @staticmethod
    def _validate_and_route(
        from_state: SupervisorState, to_state: SupervisorState
    ) -> str:
        """BUG-003 FIX: Valida y retorna la transición, o falla ruidosamente si es inválida."""
        if StateGraphValidator.is_valid_transition(from_state, to_state):
            return to_state.value
        # Transición inválida — fallar ruidosamente en lugar de silenciar
        msg = f"BUG-003: Invalid state transition blocked: {from_state.value} -> {to_state.value}"
        logger.error(msg)
        raise ValueError(msg)

    @staticmethod
    def route_from_idle(state: StateGraphState) -> str:
        """Determina la transición desde IDLE basándose en el evento pendiente."""
        event = state.get("pending_event")

        if event == WorkflowEventType.MODLIST_CHANGED.value:
            return StateGraphEdges._validate_and_route(
                SupervisorState.IDLE, SupervisorState.WATCHING
            )
        elif event == WorkflowEventType.USER_COMMAND.value:
            return StateGraphEdges._validate_and_route(
                SupervisorState.IDLE, SupervisorState.ANALYZING
            )
        elif event == WorkflowEventType.TOOL_REQUEST.value:
            return StateGraphEdges._validate_and_route(
                SupervisorState.IDLE, SupervisorState.DISPATCHING
            )
        elif event == WorkflowEventType.SHUTDOWN.value:
            return END
        elif event == WorkflowEventType.ERROR_OCCURRED.value:
            return StateGraphEdges._validate_and_route(
                SupervisorState.IDLE, SupervisorState.ERROR
            )

        return SupervisorState.IDLE.value

    @staticmethod
    def route_from_watching(state: StateGraphState) -> str:
        """Determina la transición desde WATCHING."""
        event = state.get("pending_event")

        if event == WorkflowEventType.MODLIST_CHANGED.value:
            return StateGraphEdges._validate_and_route(
                SupervisorState.WATCHING, SupervisorState.ANALYZING
            )
        elif event == WorkflowEventType.ERROR_OCCURRED.value:
            return StateGraphEdges._validate_and_route(
                SupervisorState.WATCHING, SupervisorState.ERROR
            )

        return StateGraphEdges._validate_and_route(
            SupervisorState.WATCHING, SupervisorState.IDLE
        )

    @staticmethod
    def route_from_analyzing(state: StateGraphState) -> str:
        """Determina la transición desde ANALYZING.

        FASE 2: Incluye soporte para transición a PATCHING cuando hay conflictos
        que requieren parcheo transaccional.
        """
        tool_name = state.get("tool_name")
        event_data = state.get("event_data", {})
        requires_hitl = event_data.get("requires_hitl", False)
        requires_patch = event_data.get("requires_patch", False)

        if state.get("last_error"):
            return StateGraphEdges._validate_and_route(
                SupervisorState.ANALYZING, SupervisorState.ERROR
            )
        elif tool_name:
            # FASE 2: Si requiere parcheo, ir a PATCHING
            if requires_patch:
                return StateGraphEdges._validate_and_route(
                    SupervisorState.ANALYZING, SupervisorState.PATCHING
                )
            if requires_hitl:
                return StateGraphEdges._validate_and_route(
                    SupervisorState.ANALYZING, SupervisorState.HITL_WAIT
                )
            return StateGraphEdges._validate_and_route(
                SupervisorState.ANALYZING, SupervisorState.DISPATCHING
            )

        return StateGraphEdges._validate_and_route(
            SupervisorState.ANALYZING, SupervisorState.COMPLETED
        )

    @staticmethod
    def route_from_patching(state: StateGraphState) -> str:
        """FASE 2: Determina la transición desde PATCHING.

        Transiciones:
        - COMPLETED: Si el parcheo fue exitoso
        - ERROR: Si el parcheo falló (con rollback exitoso)
        - ROLLING_BACK: Si se necesita rollback manual
        """
        tool_result = state.get("tool_result", {})
        rollback_triggered = state.get("rollback_triggered", False)

        if state.get("last_error"):
            # Si hay error y no se ha hecho rollback automático
            if not rollback_triggered:
                return StateGraphEdges._validate_and_route(
                    SupervisorState.PATCHING, SupervisorState.ROLLING_BACK
                )
            return StateGraphEdges._validate_and_route(
                SupervisorState.PATCHING, SupervisorState.ERROR
            )
        elif tool_result.get("status") == "success":
            return StateGraphEdges._validate_and_route(
                SupervisorState.PATCHING, SupervisorState.COMPLETED
            )
        elif tool_result.get("status") == "aborted":
            return StateGraphEdges._validate_and_route(
                SupervisorState.PATCHING, SupervisorState.COMPLETED
            )

        # Default: completar
        return StateGraphEdges._validate_and_route(
            SupervisorState.PATCHING, SupervisorState.COMPLETED
        )

    @staticmethod
    def route_from_hitl_wait(state: StateGraphState) -> str:
        """Determina la transición desde HITL_WAIT basándose en la respuesta."""
        hitl_response = state.get("hitl_response")

        if hitl_response == "approved":
            return StateGraphEdges._validate_and_route(
                SupervisorState.HITL_WAIT, SupervisorState.DISPATCHING
            )
        elif hitl_response == "denied":
            return StateGraphEdges._validate_and_route(
                SupervisorState.HITL_WAIT, SupervisorState.COMPLETED
            )
        elif hitl_response == "timeout":
            return StateGraphEdges._validate_and_route(
                SupervisorState.HITL_WAIT, SupervisorState.ERROR
            )

        return SupervisorState.HITL_WAIT.value  # Seguir esperando

    @staticmethod
    def route_from_dispatching(state: StateGraphState) -> str:
        """Determina la transición desde DISPATCHING."""
        tool_result = state.get("tool_result", {})

        if state.get("last_error"):
            return StateGraphEdges._validate_and_route(
                SupervisorState.DISPATCHING, SupervisorState.ERROR
            )
        elif tool_result.get("status") == "success":
            return StateGraphEdges._validate_and_route(
                SupervisorState.DISPATCHING, SupervisorState.COMPLETED
            )
        elif tool_result.get("status") == "aborted":
            return StateGraphEdges._validate_and_route(
                SupervisorState.DISPATCHING, SupervisorState.COMPLETED
            )

        return StateGraphEdges._validate_and_route(
            SupervisorState.DISPATCHING, SupervisorState.COMPLETED
        )

    @staticmethod
    def route_from_error(state: StateGraphState) -> str:
        """Determina la transición desde ERROR.

        FASE 1.5: Soporte para transición a ROLLING_BACK cuando hay operaciones
        que pueden revertirse.
        """
        error_count = state.get("error_count", 0)
        max_retries = 3
        rollback_triggered = state.get("rollback_triggered", False)

        # Si hay una transacción de rollback disponible, intentar revertir
        if state.get("rollback_transaction_id") and not rollback_triggered:
            logger.info(
                "[StateGraph] Transición a ROLLING_BACK para revertir operación fallida"
            )
            return StateGraphEdges._validate_and_route(
                SupervisorState.ERROR, SupervisorState.ROLLING_BACK
            )

        if error_count >= max_retries:
            return END  # Demasiados errores, terminar

        return StateGraphEdges._validate_and_route(
            SupervisorState.ERROR, SupervisorState.IDLE
        )

    # FASE 1.5: Routing desde estado ROLLING_BACK
    @staticmethod
    def route_from_rolling_back(state: StateGraphState) -> str:
        """Determina la transición desde ROLLING_BACK.

        Transiciones:
        - IDLE: si el rollback fue exitoso
        - ERROR_FATAL: si el rollback falló
        """
        rollback_result = state.get("rollback_result", {})

        if rollback_result.get("success", False):
            logger.info("[StateGraph] Rollback exitoso, transicionando a IDLE")
            return StateGraphEdges._validate_and_route(
                SupervisorState.ROLLING_BACK, SupervisorState.IDLE
            )
        else:
            logger.critical("[StateGraph] Rollback falló, transicionando a ERROR_FATAL")
            return StateGraphEdges._validate_and_route(
                SupervisorState.ROLLING_BACK, SupervisorState.ERROR_FATAL
            )


# =============================================================================
# StateGraph Builder
# =============================================================================


class SupervisorStateGraph:
    """
    Constructor y ejecutor del StateGraph de LangGraph para SupervisorAgent.

    Proporciona:
    - Construcción declarativa del grafo de estados
    - Ejecución asíncrona con checkpointing
    - Visualización del grafo
    - Integración con SupervisorAgent existente

    Gracefully degrada cuando LangGraph no está disponible.
    """

    def __init__(self, profile_name: str = "Default"):
        self.profile_name = profile_name
        self.graph: Optional[StateGraph] = None
        self.checkpointer: Optional[MemorySaver] = None
        self.compiled_graph = None
        self._state: Optional[WorkflowState] = None
        self._callbacks: Dict[str, Callable] = {}

        if LANGGRAPH_AVAILABLE:
            self._build_graph()
        else:
            logger.warning(
                "[StateGraph] LangGraph no disponible. Usando implementación fallback."
            )

    def _build_graph(self) -> None:
        """Construye el grafo de estados con nodos y aristas."""
        if not LANGGRAPH_AVAILABLE:
            return

        # Crear el grafo con el estado tipado
        builder = StateGraph(StateGraphState)

        # Agregar nodos
        builder.add_node(SupervisorState.INIT.value, StateGraphNodes.init_node)
        builder.add_node(SupervisorState.IDLE.value, StateGraphNodes.idle_node)
        builder.add_node(SupervisorState.WATCHING.value, StateGraphNodes.watching_node)
        builder.add_node(
            SupervisorState.ANALYZING.value, StateGraphNodes.analyzing_node
        )
        builder.add_node(
            SupervisorState.DISPATCHING.value, StateGraphNodes.dispatching_node
        )
        builder.add_node(
            SupervisorState.HITL_WAIT.value, StateGraphNodes.hitl_wait_node
        )
        builder.add_node(
            SupervisorState.COMPLETED.value, StateGraphNodes.completed_node
        )
        builder.add_node(SupervisorState.ERROR.value, StateGraphNodes.error_node)
        # FASE 1.5: Nodos de rollback
        builder.add_node(
            SupervisorState.ROLLING_BACK.value, StateGraphNodes.rolling_back_node
        )
        builder.add_node(
            SupervisorState.ERROR_FATAL.value, StateGraphNodes.error_fatal_node
        )
        # FASE 2: Nodo de parcheo transaccional
        builder.add_node(SupervisorState.PATCHING.value, StateGraphNodes.patching_node)

        # Definir punto de entrada
        builder.set_entry_point(SupervisorState.INIT.value)

        # Agregar aristas condicionales
        builder.add_conditional_edges(
            SupervisorState.INIT.value, lambda s: SupervisorState.IDLE.value
        )

        builder.add_conditional_edges(
            SupervisorState.IDLE.value, StateGraphEdges.route_from_idle
        )

        builder.add_conditional_edges(
            SupervisorState.WATCHING.value, StateGraphEdges.route_from_watching
        )

        builder.add_conditional_edges(
            SupervisorState.ANALYZING.value, StateGraphEdges.route_from_analyzing
        )

        builder.add_conditional_edges(
            SupervisorState.DISPATCHING.value, StateGraphEdges.route_from_dispatching
        )

        builder.add_conditional_edges(
            SupervisorState.HITL_WAIT.value, StateGraphEdges.route_from_hitl_wait
        )

        builder.add_conditional_edges(
            SupervisorState.ERROR.value, StateGraphEdges.route_from_error
        )

        # FASE 1.5: Aristas condicionales para rollback
        builder.add_conditional_edges(
            SupervisorState.ROLLING_BACK.value, StateGraphEdges.route_from_rolling_back
        )

        # FASE 2: Aristas condicionales para parcheo
        builder.add_conditional_edges(
            SupervisorState.PATCHING.value, StateGraphEdges.route_from_patching
        )

        # Arista final desde COMPLETED
        builder.add_edge(SupervisorState.COMPLETED.value, SupervisorState.IDLE.value)

        # FASE 1.5: ERROR_FATAL es terminal, va a END
        builder.add_edge(SupervisorState.ERROR_FATAL.value, END)

        # Configurar checkpointer para persistencia
        self.checkpointer = MemorySaver()

        # Compilar el grafo
        self.compiled_graph = builder.compile(checkpointer=self.checkpointer)
        self.graph = builder

        logger.info("[StateGraph] Grafo de estados construido y compilado exitosamente")

    def register_callback(self, state: SupervisorState, callback: Callable) -> None:
        """Registra un callback para un estado específico."""
        key = f"{state.value}_callback"
        self._callbacks[key] = callback
        logger.debug(f"[StateGraph] Callback registrado para estado: {state.value}")

    def get_initial_state(self) -> StateGraphState:
        """Retorna el estado inicial del workflow."""
        return {
            "workflow_id": f"wf_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
            "current_state": SupervisorState.INIT.value,
            "previous_state": None,
            "profile_name": self.profile_name,
            "modlist_path": None,
            "last_mtime": 0.0,
            "pending_event": None,
            "event_data": {},
            "tool_name": None,
            "tool_payload": {},
            "tool_result": None,
            "hitl_request": None,
            "hitl_response": None,
            "transition_history": [],
            "last_error": None,
            "error_count": 0,
            # FASE 1.5: Campos de rollback
            "rollback_triggered": False,
            "rollback_result": None,
            "rollback_transaction_id": None,
        }

    async def execute(
        self, initial_state: Optional[StateGraphState] = None
    ) -> StateGraphState:
        """
        Ejecuta el workflow de forma asíncrona.

        Args:
            initial_state: Estado inicial opcional

        Returns:
            Estado final del workflow
        """
        if not LANGGRAPH_AVAILABLE:
            return await self._execute_fallback(initial_state)

        state = initial_state or self.get_initial_state()
        config = {"configurable": {"thread_id": state["workflow_id"]}}

        try:
            # Ejecutar el grafo
            result = await self.compiled_graph.ainvoke(state, config)
            return result
        except Exception as e:
            logger.error(f"[StateGraph] Error ejecutando workflow: {e}")
            state["last_error"] = str(e)
            state["current_state"] = SupervisorState.ERROR.value
            return state

    async def _execute_fallback(
        self, initial_state: Optional[StateGraphState] = None
    ) -> StateGraphState:
        """Ejecución fallback sin LangGraph."""
        state = initial_state or self.get_initial_state()

        # Simular transiciones básicas
        state["current_state"] = SupervisorState.IDLE.value
        state["previous_state"] = SupervisorState.INIT.value

        # Procesar evento si existe
        if state.get("pending_event"):
            event = state["pending_event"]

            if event in [
                WorkflowEventType.MODLIST_CHANGED.value,
                WorkflowEventType.USER_COMMAND.value,
            ]:
                state["current_state"] = SupervisorState.ANALYZING.value
                state["previous_state"] = SupervisorState.IDLE.value

                if state.get("tool_name"):
                    state["current_state"] = SupervisorState.DISPATCHING.value
                    state["previous_state"] = SupervisorState.ANALYZING.value

            state["current_state"] = SupervisorState.COMPLETED.value
            state["previous_state"] = SupervisorState.DISPATCHING.value

        return state

    async def submit_event(
        self,
        event_type: WorkflowEventType,
        event_data: Optional[Dict[str, Any]] = None,
        thread_id: Optional[str] = None,
    ) -> StateGraphState:
        """
        Envía un evento al workflow en ejecución.

        Args:
            event_type: Tipo de evento
            event_data: Datos asociados al evento
            thread_id: ID del thread para continuación

        Returns:
            Estado resultante
        """
        state = self.get_initial_state()
        state["pending_event"] = event_type.value
        state["event_data"] = event_data or {}

        if thread_id:
            state["workflow_id"] = thread_id

        return await self.execute(state)

    def visualize(self, output_path: Optional[str] = None) -> Optional[str]:
        """
        Genera una visualización del grafo en formato Mermaid.

        Args:
            output_path: Ruta opcional para guardar la imagen

        Returns:
            Diagrama Mermaid como string o None si no está disponible
        """
        if not LANGGRAPH_AVAILABLE:
            logger.warning("[StateGraph] Visualización no disponible sin LangGraph")
            return None

        try:
            # Obtener representación Mermaid
            mermaid_png = self.compiled_graph.get_graph().draw_mermaid()

            if output_path:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(mermaid_png)
                logger.info(f"[StateGraph] Diagrama guardado en: {output_path}")

            return mermaid_png
        except Exception as e:
            logger.error(f"[StateGraph] Error generando visualización: {e}")
            return None

    def get_mermaid_diagram(self) -> str:
        """Retorna el diagrama del grafo en formato Mermaid."""
        return """
```mermaid
stateDiagram-v2
    [*] --> INIT
    INIT --> IDLE
    IDLE --> WATCHING: modlist_changed
    IDLE --> ANALYZING: user_command
    IDLE --> DISPATCHING: tool_request
    IDLE --> [*]: shutdown
    IDLE --> ERROR: error
    WATCHING --> ANALYZING: change_detected
    WATCHING --> IDLE: no_change
    WATCHING --> ERROR: error
    ANALYZING --> DISPATCHING: tool_required
    ANALYZING --> HITL_WAIT: requires_approval
    ANALYZING --> COMPLETED: no_action_needed
    ANALYZING --> ERROR: error
    DISPATCHING --> COMPLETED: success
    DISPATCHING --> COMPLETED: aborted
    DISPATCHING --> ERROR: error
    HITL_WAIT --> DISPATCHING: approved
    HITL_WAIT --> COMPLETED: denied
    HITL_WAIT --> ERROR: timeout
    COMPLETED --> IDLE
    ERROR --> ROLLING_BACK: has_transaction
    ERROR --> IDLE: retry
    ERROR --> [*]: max_retries
    %% FASE 1.5: Estados de Rollback
    ROLLING_BACK --> IDLE: rollback_success
    ROLLING_BACK --> ERROR_FATAL: rollback_failed
    ERROR_FATAL --> [*]
```
"""

    def get_state_summary(self, state: StateGraphState) -> Dict[str, Any]:
        """Retorna un resumen legible del estado actual."""
        return {
            "workflow_id": state.get("workflow_id"),
            "current_state": state.get("current_state"),
            "previous_state": state.get("previous_state"),
            "pending_event": state.get("pending_event"),
            "tool_name": state.get("tool_name"),
            "error_count": state.get("error_count"),
            "last_error": state.get("last_error"),
            "transitions": len(state.get("transition_history", [])),
            # FASE 1.5: Campos de rollback
            "rollback_triggered": state.get("rollback_triggered", False),
            "rollback_transaction_id": state.get("rollback_transaction_id"),
        }


# =============================================================================
# Factory Function
# =============================================================================


def create_supervisor_state_graph(
    profile_name: str = "Default",
) -> SupervisorStateGraph:
    """
    Factory function para crear un StateGraph de Supervisor.

    Args:
        profile_name: Nombre del perfil de modding

    Returns:
        Instancia configurada de SupervisorStateGraph
    """
    return SupervisorStateGraph(profile_name=profile_name)


# =============================================================================
# Integration Helper
# =============================================================================


class StateGraphIntegration:
    """
    Helper para integrar StateGraph con SupervisorAgent existente.

    Proporciona métodos de conveniencia para:
    - Conectar callbacks del supervisor
    - Traducir eventos del supervisor al formato del grafo
    - Sincronizar estado entre supervisor y grafo
    """

    def __init__(self, state_graph: SupervisorStateGraph):
        self.state_graph = state_graph
        self._supervisor = None

    def connect_supervisor(self, supervisor: Any) -> None:
        """Conecta un SupervisorAgent al StateGraph."""
        self._supervisor = supervisor

        # Registrar callbacks para cada estado
        self.state_graph.register_callback(
            SupervisorState.ANALYZING, self._on_analyzing
        )
        self.state_graph.register_callback(
            SupervisorState.DISPATCHING, self._on_dispatching
        )
        self.state_graph.register_callback(
            SupervisorState.HITL_WAIT, self._on_hitl_wait
        )

        logger.info("[StateGraph] Supervisor conectado exitosamente")

    async def _on_analyzing(self, state: StateGraphState) -> None:
        """Callback para estado ANALYZING."""
        if self._supervisor:
            await self._supervisor._trigger_proactive_analysis()

    async def _on_dispatching(self, state: StateGraphState) -> None:
        """Callback para estado DISPATCHING."""
        if self._supervisor:
            tool_name = state.get("tool_name")
            payload = state.get("tool_payload", {})
            result = await self._supervisor.dispatch_tool(tool_name, payload)
            state["tool_result"] = result

    async def _on_hitl_wait(self, state: StateGraphState) -> None:
        """Callback para estado HITL_WAIT."""
        if self._supervisor:
            hitl_request = state.get("hitl_request", {})
            response = await self._supervisor.interface.request_hitl(
                self._supervisor._create_hitl_request(hitl_request)
            )
            state["hitl_response"] = response

    def translate_modlist_event(self, mtime: float, path: str) -> Dict[str, Any]:
        """Traduce un evento de cambio de modlist al formato del grafo."""
        return {
            "event_type": WorkflowEventType.MODLIST_CHANGED,
            "event_data": {"mtime": mtime, "path": path, "requires_hitl": False},
        }

    def translate_user_command(
        self, command: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Traduce un comando de usuario al formato del grafo."""
        return {
            "event_type": WorkflowEventType.USER_COMMAND,
            "event_data": {"command": command, "params": params},
        }


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Enums
    "SupervisorState",
    "WorkflowEventType",
    # State classes
    "WorkflowState",
    "StateGraphState",
    # Graph components
    "StateGraphNodes",
    "StateGraphEdges",
    "SupervisorStateGraph",
    "StateGraphIntegration",
    # Factory
    "create_supervisor_state_graph",
    # Availability flags
    "LANGGRAPH_AVAILABLE",
]
