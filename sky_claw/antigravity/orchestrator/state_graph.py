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
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from sky_claw.antigravity.core.models import CircuitBreakerTrippedError
from sky_claw.antigravity.security.loop_guardrail import AgenticLoopGuardrail
from sky_claw.config import HITL_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

# Conditional imports for graceful degradation
try:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.constants import START
    from langgraph.graph import END, StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph: Any = object  # type: ignore[no-redef]
    END: str = "__END__"  # type: ignore[no-redef]
    START: str = "__START__"  # type: ignore[no-redef]
    MemorySaver: Any = None  # type: ignore[no-redef]

try:
    from pydantic import BaseModel, Field, field_validator  # noqa: F401

    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    BaseModel: Any = object  # type: ignore[no-redef]

    def field_stub(*args: Any, **kwargs: Any) -> Any:
        return None

    Field: Any = field_stub  # type: ignore[no-redef]


logger = logging.getLogger("SkyClaw.StateGraph")

# TASK-006 (M-4): Maximum entries retained in transition_history.
# Prevents unbounded memory growth in long-running workflows.
MAX_TRANSITION_HISTORY: int = 50


def capped_transition_history(
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """LangGraph reducer for ``transition_history`` that caps growth.

    Merges *old* and *new* lists, then truncates to the most recent
    ``MAX_TRANSITION_HISTORY`` entries.  Used via ``Annotated`` in
    ``StateGraphState`` so that every node update automatically applies
    the cap without manual trimming.

    Args:
        old: Existing transition history from previous state.
        new: Newly appended transition entries from the current node.

    Returns:
        Merged list capped at ``MAX_TRANSITION_HISTORY`` entries.
    """
    merged = old + new
    if len(merged) <= MAX_TRANSITION_HISTORY:
        return merged
    return merged[-MAX_TRANSITION_HISTORY:]


# =============================================================================
# State Definitions
# =============================================================================


class SupervisorState(StrEnum):
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


class WorkflowEventType(StrEnum):
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

    _VALID_TRANSITIONS: dict[SupervisorState, set[SupervisorState]] = {
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
            SupervisorState.HITL_WAIT,
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
    def valid_transitions(cls) -> dict[SupervisorState, set[SupervisorState]]:
        """Retorna el diccionario de transiciones válidas."""
        return cls._VALID_TRANSITIONS.copy()

    @classmethod
    def is_valid_transition(cls, from_state: SupervisorState, to_state: SupervisorState) -> bool:
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
    def validate_transition(cls, from_state: SupervisorState, to_state: SupervisorState) -> None:
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
            raise ValueError(f"Transición de estado inválida: {from_state.value} -> {to_state.value}")


if PYDANTIC_AVAILABLE:

    class WorkflowState(BaseModel):
        """Estado del workflow de LangGraph para Sky-Claw."""

        # Identificación
        workflow_id: str = Field(default_factory=lambda: f"wf_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}")

        # Estado actual
        current_state: SupervisorState = Field(default=SupervisorState.INIT)
        previous_state: SupervisorState | None = None

        # Datos del contexto
        profile_name: str = Field(default="Default")
        modlist_path: str | None = None
        last_mtime: float = 0.0

        # Evento actual
        pending_event: WorkflowEventType | None = None
        event_data: dict[str, Any] = Field(default_factory=dict)

        # Resultados de herramientas
        tool_name: str | None = None
        tool_payload: dict[str, Any] = Field(default_factory=dict)
        tool_result: dict[str, Any] | None = None

        # HITL
        hitl_request: dict[str, Any] | None = None
        hitl_response: str | None = None  # "approved", "denied", "timeout"

        # Historial de transiciones
        transition_history: list[dict[str, Any]] = Field(default_factory=list)

        # Errores
        last_error: str | None = None
        error_count: int = 0

        # FASE 1.5: Campos de rollback para resiliencia
        rollback_triggered: bool = Field(default=False)
        rollback_result: dict[str, Any] | None = None
        rollback_transaction_id: int | None = None

        # Metadata
        created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
        updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

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
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
            # TASK-006 (M-4): Trim history to prevent unbounded growth
            if len(self.transition_history) > MAX_TRANSITION_HISTORY:
                self.transition_history = self.transition_history[-MAX_TRANSITION_HISTORY:]
            self.updated_at = datetime.now(UTC)

else:
    # Fallback sin Pydantic
    class WorkflowState:  # type: ignore[no-redef]
        """Estado del workflow sin Pydantic."""

        def __init__(self) -> None:
            self.workflow_id = f"wf_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
            self.current_state = SupervisorState.INIT
            self.previous_state = None
            self.profile_name = "Default"
            self.modlist_path = None
            self.last_mtime = 0.0
            self.pending_event = None
            self.event_data: dict[str, Any] = {}
            self.tool_name = None
            self.tool_payload: dict[str, Any] = {}
            self.tool_result = None
            self.hitl_request = None
            self.hitl_response = None
            self.transition_history: list[dict[str, Any]] = []
            self.last_error = None
            self.error_count = 0
            # FASE 1.5: Campos de rollback
            self.rollback_triggered = False
            self.rollback_result = None
            self.rollback_transaction_id = None
            self.created_at = datetime.now(UTC)
            self.updated_at = datetime.now(UTC)

        def trim_history(self) -> None:
            """TASK-006 (M-4): Trim transition_history to MAX_TRANSITION_HISTORY."""
            if len(self.transition_history) > MAX_TRANSITION_HISTORY:
                self.transition_history = self.transition_history[-MAX_TRANSITION_HISTORY:]


# TypedDict para LangGraph (requerido para anotaciones de estado)
class StateGraphState(TypedDict):
    """Estado tipado para LangGraph StateGraph."""

    workflow_id: str
    current_state: str
    previous_state: str | None
    profile_name: str
    modlist_path: str | None
    last_mtime: float
    pending_event: str | None
    event_data: dict[str, Any]
    tool_name: str | None
    tool_payload: dict[str, Any]
    tool_result: dict[str, Any] | None
    hitl_request: dict[str, Any] | None
    hitl_response: str | None
    transition_history: Annotated[list[dict[str, Any]], capped_transition_history]
    last_error: str | None
    error_count: int
    # FASE 1.5: Campos de rollback para resiliencia
    rollback_triggered: bool
    rollback_result: dict[str, Any] | None
    rollback_transaction_id: int | None
    # HITL timeout tracking — monotonic timestamp when HITL_WAIT was first entered
    hitl_started_at: float | None
    # Cortacircuitos cognitivo: AgenticLoopGuardrail sets these on trip.
    loop_detected: bool
    loop_context: dict[str, Any] | None


# =============================================================================
# Node Functions
# =============================================================================


class StateGraphNodes:
    """Nodos del grafo de estados para Sky-Claw."""

    @staticmethod
    def init_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de inicialización del workflow."""
        logger.info(f"[StateGraph] Inicializando workflow: {state.get('workflow_id')}")
        return {
            "current_state": SupervisorState.IDLE.value,
            "previous_state": SupervisorState.INIT.value,
        }

    @staticmethod
    def idle_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de espera - esperando eventos."""
        logger.debug("[StateGraph] En estado IDLE, esperando eventos...")
        return {"current_state": SupervisorState.IDLE.value}

    @staticmethod
    def watching_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de monitoreo - detectando cambios."""
        logger.debug("[StateGraph] Monitoreando cambios en modlist...")
        return {"current_state": SupervisorState.WATCHING.value}

    @staticmethod
    def analyzing_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de análisis - procesando cambios detectados."""
        state.get("event_data", {})
        logger.info(f"[StateGraph] Analizando evento: {state.get('pending_event')}")
        return {
            "current_state": SupervisorState.ANALYZING.value,
            "previous_state": SupervisorState.WATCHING.value,
        }

    @staticmethod
    def dispatching_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de despacho - ejecutando herramientas."""
        tool_name = state.get("tool_name")
        logger.info(f"[StateGraph] Despachando herramienta: {tool_name}")
        return {
            "current_state": SupervisorState.DISPATCHING.value,
            "previous_state": SupervisorState.ANALYZING.value,
        }

    @staticmethod
    def hitl_wait_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de espera HITL - esperando aprobación humana.

        Inyecta ``hitl_started_at`` con ``time.monotonic()`` en la primera
        entrada al ciclo de espera.  Las iteraciones subsecuentes preservan
        el timestamp original para que el timeout se mida desde el primer
        ingreso, no desde la última re-entrada.
        """
        hitl_request: dict[str, Any] = state.get("hitl_request") or {}
        logger.info("[StateGraph] Esperando aprobación HITL: %s", hitl_request.get("action_type"))

        updates: dict[str, Any] = {
            "current_state": SupervisorState.HITL_WAIT.value,
            "previous_state": SupervisorState.DISPATCHING.value,
        }

        # Preservar timestamp original; solo inyectar si es None (primera vez)
        if state.get("hitl_started_at") is None:
            updates["hitl_started_at"] = time.monotonic()

        return updates

    @staticmethod
    def completed_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de completado - operación finalizada."""
        logger.info("[StateGraph] Operación completada exitosamente")
        return {
            "current_state": SupervisorState.COMPLETED.value,
            "previous_state": SupervisorState.DISPATCHING.value,
            "pending_event": None,
        }

    @staticmethod
    def error_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de error - manejo de errores."""
        error_count = state.get("error_count", 0) + 1
        logger.error(f"[StateGraph] Error en workflow (intento {error_count}): {state.get('last_error')}")
        return {
            "current_state": SupervisorState.ERROR.value,
            "error_count": error_count,
        }

    # FASE 1.5: Nodos de rollback para resiliencia
    @staticmethod
    def rolling_back_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de rollback - ejecuta reversión de operaciones fallidas."""
        logger.info(f"[StateGraph] Iniciando rollback para transaction: {state.get('rollback_transaction_id')}")
        return {
            "current_state": SupervisorState.ROLLING_BACK.value,
            "previous_state": SupervisorState.ERROR.value,
            "rollback_triggered": True,
        }

    @staticmethod
    def error_fatal_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de error fatal - estado terminal sin recuperación posible."""
        logger.critical(f"[StateGraph] Error fatal detectado. Rollback falló: {state.get('last_error')}")
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
    def patching_node(state: StateGraphState) -> dict[str, Any]:
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
        # Este nodo solo actualiza el estado; la lógica vive ahora en
        # XEditPipelineService.execute_patch (despachado desde supervisor.dispatch_tool
        # con tool_name="resolve_conflict_with_patch")
        return {
            "current_state": SupervisorState.PATCHING.value,
            "previous_state": SupervisorState.ANALYZING.value,
            "event_data": event_data,
        }

    # FASE 4: Nodo de generación de LODs
    @staticmethod
    def generating_lods_node(state: StateGraphState) -> dict[str, Any]:
        """Nodo de generación de LODs - ejecuta pipeline DynDOLOD.

        Este nodo representa el estado donde se genera LODs para el paisaje.
        La lógica real de generación vive en DynDOLODPipelineService,
        despachada desde supervisor.dispatch_tool con tool_name="generate_lods".

        Args:
            state: Estado actual del workflow.

        Returns:
            Dict con el nuevo estado GENERATING_LODS.
        """
        logger.info("[StateGraph] Iniciando generación de LODs")
        return {
            "current_state": SupervisorState.GENERATING_LODS.value,
            "previous_state": SupervisorState.DISPATCHING.value,
        }


# =============================================================================
# Conditional Edges
# =============================================================================


class StateGraphEdges:
    """Aristas condicionales del grafo de estados."""

    @staticmethod
    def _validate_and_route(from_state: SupervisorState, to_state: SupervisorState) -> str:
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
            return StateGraphEdges._validate_and_route(SupervisorState.IDLE, SupervisorState.WATCHING)
        elif event == WorkflowEventType.USER_COMMAND.value:
            return StateGraphEdges._validate_and_route(SupervisorState.IDLE, SupervisorState.ANALYZING)
        elif event == WorkflowEventType.TOOL_REQUEST.value:
            return StateGraphEdges._validate_and_route(SupervisorState.IDLE, SupervisorState.DISPATCHING)
        elif event == WorkflowEventType.SHUTDOWN.value:
            return END
        elif event == WorkflowEventType.ERROR_OCCURRED.value:
            return StateGraphEdges._validate_and_route(SupervisorState.IDLE, SupervisorState.ERROR)

        return SupervisorState.IDLE.value

    @staticmethod
    def route_from_watching(state: StateGraphState) -> str:
        """Determina la transición desde WATCHING."""
        event = state.get("pending_event")

        if event == WorkflowEventType.MODLIST_CHANGED.value:
            return StateGraphEdges._validate_and_route(SupervisorState.WATCHING, SupervisorState.ANALYZING)
        elif event == WorkflowEventType.ERROR_OCCURRED.value:
            return StateGraphEdges._validate_and_route(SupervisorState.WATCHING, SupervisorState.ERROR)

        return StateGraphEdges._validate_and_route(SupervisorState.WATCHING, SupervisorState.IDLE)

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
            return StateGraphEdges._validate_and_route(SupervisorState.ANALYZING, SupervisorState.ERROR)
        elif tool_name:
            # FASE 2: Si requiere parcheo, ir a PATCHING
            if requires_patch:
                return StateGraphEdges._validate_and_route(SupervisorState.ANALYZING, SupervisorState.PATCHING)
            if requires_hitl:
                return StateGraphEdges._validate_and_route(SupervisorState.ANALYZING, SupervisorState.HITL_WAIT)
            return StateGraphEdges._validate_and_route(SupervisorState.ANALYZING, SupervisorState.DISPATCHING)

        return StateGraphEdges._validate_and_route(SupervisorState.ANALYZING, SupervisorState.COMPLETED)

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
                return StateGraphEdges._validate_and_route(SupervisorState.PATCHING, SupervisorState.ROLLING_BACK)
            return StateGraphEdges._validate_and_route(SupervisorState.PATCHING, SupervisorState.ERROR)
        elif tool_result and (tool_result.get("status") == "success" or tool_result.get("status") == "aborted"):
            return StateGraphEdges._validate_and_route(SupervisorState.PATCHING, SupervisorState.COMPLETED)

        # Default: completar
        return StateGraphEdges._validate_and_route(SupervisorState.PATCHING, SupervisorState.COMPLETED)

    # FASE 4: Routing desde estado GENERATING_LODS
    @staticmethod
    def route_from_generating_lods(state: StateGraphState) -> str:
        """FASE 4: Determina la transición desde GENERATING_LODS.

        Transiciones:
        - COMPLETED: LOD generation succeeded
        - ERROR: LOD generation failed
        - ROLLING_BACK: If rollback needed on failure
        """
        tool_result = state.get("tool_result", {})
        rollback_triggered = state.get("rollback_triggered", False)

        if state.get("last_error"):
            if not rollback_triggered:
                return StateGraphEdges._validate_and_route(
                    SupervisorState.GENERATING_LODS, SupervisorState.ROLLING_BACK
                )
            return StateGraphEdges._validate_and_route(SupervisorState.GENERATING_LODS, SupervisorState.ERROR)
        elif tool_result and tool_result.get("status") == "success":
            return StateGraphEdges._validate_and_route(SupervisorState.GENERATING_LODS, SupervisorState.COMPLETED)

        return StateGraphEdges._validate_and_route(SupervisorState.GENERATING_LODS, SupervisorState.COMPLETED)

    @staticmethod
    def route_from_hitl_wait(state: StateGraphState) -> str:
        """Determina la transición desde HITL_WAIT con timeout de seguridad.

        Lógica de decisión evaluada en orden estricto (matches implementation):

        D. Respuesta recibida (``hitl_response`` no es None) → transición según respuesta:
           - ``"approved"``  → DISPATCHING
           - ``"denied"``    → COMPLETED  (graceful no-op)
           - ``"timeout"``   → ERROR
           - desconocido     → COMPLETED  (fail-safe)
        C. Estado inconsistente (``hitl_started_at`` ausente) → ERROR
        A. Timeout alcanzado (elapsed ≥ HITL_TIMEOUT_SECONDS) → ERROR
        B. Espera legítima (sin expirar) → HITL_WAIT (continuar polling)
        """
        hitl_response = state.get("hitl_response")

        # Condición D — Respuesta recibida del humano
        if hitl_response is not None:
            if hitl_response == "approved":
                return StateGraphEdges._validate_and_route(SupervisorState.HITL_WAIT, SupervisorState.DISPATCHING)
            elif hitl_response == "denied":
                return StateGraphEdges._validate_and_route(SupervisorState.HITL_WAIT, SupervisorState.COMPLETED)
            elif hitl_response == "timeout":
                return StateGraphEdges._validate_and_route(SupervisorState.HITL_WAIT, SupervisorState.ERROR)
            # Respuesta desconocida — tratar como denegada
            logger.warning("[StateGraph] HITL response desconocido: %s", hitl_response)
            return StateGraphEdges._validate_and_route(SupervisorState.HITL_WAIT, SupervisorState.COMPLETED)

        # hitl_response is None — evaluar timeout
        hitl_started_at = state.get("hitl_started_at")

        # Condición C — Estado inconsistente: sin timestamp de inicio
        if hitl_started_at is None:
            logger.error("[StateGraph] Estado HITL inconsistente: hitl_started_at ausente")
            state["last_error"] = "Estado HITL inconsistente: hitl_started_at ausente"
            return StateGraphEdges._validate_and_route(SupervisorState.HITL_WAIT, SupervisorState.ERROR)

        # Evaluar elapsed time contra HITL_TIMEOUT_SECONDS
        elapsed = time.monotonic() - hitl_started_at

        # Condición A — Timeout alcanzado
        if elapsed >= HITL_TIMEOUT_SECONDS:
            logger.warning(
                "[StateGraph] HITL timeout alcanzado: %.1fs >= %ds",
                elapsed,
                HITL_TIMEOUT_SECONDS,
            )
            state["last_error"] = f"Timeout esperando aprobación humana después de {HITL_TIMEOUT_SECONDS}s"
            return StateGraphEdges._validate_and_route(SupervisorState.HITL_WAIT, SupervisorState.ERROR)

        # Condición B — Espera legítima, sin expirar
        return SupervisorState.HITL_WAIT.value

    @staticmethod
    def route_from_dispatching(state: StateGraphState) -> str:
        """Determina la transición desde DISPATCHING.

        FASE 4: Incluye soporte para transición a GENERATING_LODS cuando
        la herramienta despachada es generate_lods.
        """
        tool_result = state.get("tool_result", {})
        tool_name = state.get("tool_name", "")

        if state.get("loop_detected"):
            return StateGraphEdges._validate_and_route(SupervisorState.DISPATCHING, SupervisorState.HITL_WAIT)
        if state.get("last_error"):
            return StateGraphEdges._validate_and_route(SupervisorState.DISPATCHING, SupervisorState.ERROR)
        elif tool_result and (tool_result.get("status") == "success" or tool_result.get("status") == "aborted"):
            # FASE 4: Si la herramienta es generate_lods, ir a GENERATING_LODS
            if tool_name == "generate_lods":
                return StateGraphEdges._validate_and_route(SupervisorState.DISPATCHING, SupervisorState.GENERATING_LODS)
            return StateGraphEdges._validate_and_route(SupervisorState.DISPATCHING, SupervisorState.COMPLETED)

        return StateGraphEdges._validate_and_route(SupervisorState.DISPATCHING, SupervisorState.COMPLETED)

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
            logger.info("[StateGraph] Transición a ROLLING_BACK para revertir operación fallida")
            return StateGraphEdges._validate_and_route(SupervisorState.ERROR, SupervisorState.ROLLING_BACK)

        if error_count >= max_retries:
            return END  # Demasiados errores, terminar

        return StateGraphEdges._validate_and_route(SupervisorState.ERROR, SupervisorState.IDLE)

    # FASE 1.5: Routing desde estado ROLLING_BACK
    @staticmethod
    def route_from_rolling_back(state: StateGraphState) -> str:
        """Determina la transición desde ROLLING_BACK.

        Transiciones:
        - IDLE: si el rollback fue exitoso
        - ERROR_FATAL: si el rollback falló
        """
        rollback_result: dict[str, Any] = state.get("rollback_result") or {}

        if rollback_result.get("success", False):
            logger.info("[StateGraph] Rollback exitoso, transicionando a IDLE")
            return StateGraphEdges._validate_and_route(SupervisorState.ROLLING_BACK, SupervisorState.IDLE)
        else:
            logger.critical("[StateGraph] Rollback falló, transicionando a ERROR_FATAL")
            return StateGraphEdges._validate_and_route(SupervisorState.ROLLING_BACK, SupervisorState.ERROR_FATAL)


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
        self.graph: Any = None
        self.checkpointer: Any = None
        self.compiled_graph: Any = None
        self._state: WorkflowState | None = None
        self._callbacks: dict[str, Callable[..., Any]] = {}
        # Cortacircuitos cognitivo: per-graph singleton (el grafo es serial por perfil).
        self.loop_guardrail = AgenticLoopGuardrail(max_repeats=3, window_size=5)
        # TASK-006 (M-4): TTL tracking for checkpointer thread cleanup.
        # Maps thread_id → monotonic timestamp of last access.
        self._thread_timestamps: dict[str, float] = {}

        if LANGGRAPH_AVAILABLE:
            self._build_graph()
        else:
            logger.warning("[StateGraph] LangGraph no disponible. Usando implementación fallback.")

    def _make_callback_aware_node(self, state_value: str, node_fn: Any) -> Any:
        """M-1 FIX: Envuelve un nodo para invocar callbacks registrados antes de ejecutarlo.

        Los callbacks registrados vía register_callback() se almacenan en self._callbacks
        pero los nodos originales (funciones estáticas) nunca los invocan. Este wrapper
        resuelve esa desconexión: invoca el callback async antes de ejecutar la lógica
        del nodo, permitiendo que el cortacircuitos cognitivo y el flujo HITL funcionen.

        Args:
            state_value: Nombre del estado (ej. "dispatching").
            node_fn: Función de nodo original (StateGraphNodes.xxx_node).

        Returns:
            Función async que invoca callback + nodo original.
        """
        graph_ref = self  # closure capture

        async def wrapped_node(state: StateGraphState) -> dict[str, Any]:
            callback_key = f"{state_value}_callback"
            callback = graph_ref._callbacks.get(callback_key)
            if callback is not None:
                await callback(state)
            return node_fn(state)

        return wrapped_node

    def _build_graph(self) -> None:
        """Construye el grafo de estados con nodos y aristas."""
        if not LANGGRAPH_AVAILABLE:
            return

        # Crear el grafo con el estado tipado
        builder = StateGraph(StateGraphState)

        # Agregar nodos — los tres estados con callbacks usan wrapper M-1 FIX
        builder.add_node(SupervisorState.INIT.value, StateGraphNodes.init_node)
        builder.add_node(SupervisorState.IDLE.value, StateGraphNodes.idle_node)
        builder.add_node(SupervisorState.WATCHING.value, StateGraphNodes.watching_node)
        builder.add_node(
            SupervisorState.ANALYZING.value,
            self._make_callback_aware_node(SupervisorState.ANALYZING.value, StateGraphNodes.analyzing_node),
        )
        builder.add_node(
            SupervisorState.DISPATCHING.value,
            self._make_callback_aware_node(SupervisorState.DISPATCHING.value, StateGraphNodes.dispatching_node),
        )
        builder.add_node(
            SupervisorState.HITL_WAIT.value,
            self._make_callback_aware_node(SupervisorState.HITL_WAIT.value, StateGraphNodes.hitl_wait_node),
        )
        builder.add_node(SupervisorState.COMPLETED.value, StateGraphNodes.completed_node)
        builder.add_node(SupervisorState.ERROR.value, StateGraphNodes.error_node)
        # FASE 1.5: Nodos de rollback
        builder.add_node(SupervisorState.ROLLING_BACK.value, StateGraphNodes.rolling_back_node)
        builder.add_node(SupervisorState.ERROR_FATAL.value, StateGraphNodes.error_fatal_node)
        # FASE 2: Nodo de parcheo transaccional
        builder.add_node(SupervisorState.PATCHING.value, StateGraphNodes.patching_node)
        # FASE 4: Nodo de generación de LODs
        builder.add_node(SupervisorState.GENERATING_LODS.value, StateGraphNodes.generating_lods_node)

        # Definir punto de entrada
        builder.set_entry_point(SupervisorState.INIT.value)

        # Agregar aristas condicionales
        builder.add_conditional_edges(SupervisorState.INIT.value, lambda s: SupervisorState.IDLE.value)

        builder.add_conditional_edges(SupervisorState.IDLE.value, StateGraphEdges.route_from_idle)

        builder.add_conditional_edges(SupervisorState.WATCHING.value, StateGraphEdges.route_from_watching)

        builder.add_conditional_edges(SupervisorState.ANALYZING.value, StateGraphEdges.route_from_analyzing)

        builder.add_conditional_edges(SupervisorState.DISPATCHING.value, StateGraphEdges.route_from_dispatching)

        builder.add_conditional_edges(SupervisorState.HITL_WAIT.value, StateGraphEdges.route_from_hitl_wait)

        builder.add_conditional_edges(SupervisorState.ERROR.value, StateGraphEdges.route_from_error)

        # FASE 1.5: Aristas condicionales para rollback
        builder.add_conditional_edges(SupervisorState.ROLLING_BACK.value, StateGraphEdges.route_from_rolling_back)

        # FASE 2: Aristas condicionales para parcheo
        builder.add_conditional_edges(SupervisorState.PATCHING.value, StateGraphEdges.route_from_patching)

        # FASE 4: Aristas condicionales para generación de LODs
        builder.add_conditional_edges(SupervisorState.GENERATING_LODS.value, StateGraphEdges.route_from_generating_lods)

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

    def register_callback(self, state: SupervisorState, callback: Callable[..., Any]) -> None:
        """Registra un callback para un estado específico."""
        key = f"{state.value}_callback"
        self._callbacks[key] = callback
        logger.debug(f"[StateGraph] Callback registrado para estado: {state.value}")

    def cleanup_old_threads(self, max_age_seconds: int) -> int:
        """TASK-006 (M-4): Purge stale thread state from the MemorySaver checkpointer.

        Removes checkpoint data for threads whose last access timestamp
        exceeds *max_age_seconds*.  Also prunes the corresponding entries
        from ``_thread_timestamps``.

        The standard ``MemorySaver`` does not expose age-based cleanup, so
        this method implements a simple TTL layer at the wrapper level by
        tracking thread access times via ``time.monotonic()``.

        Args:
            max_age_seconds: Maximum age in seconds.  Threads not accessed
                within this window are purged.

        Returns:
            Number of threads removed from the checkpointer.
        """
        if not self.checkpointer or max_age_seconds <= 0:
            return 0

        now = time.monotonic()
        # Identify stale thread IDs from our TTL tracking
        stale_thread_ids: list[str] = [
            tid for tid, ts in self._thread_timestamps.items() if (now - ts) > max_age_seconds
        ]

        if not stale_thread_ids:
            return 0

        # Attempt to remove from MemorySaver's internal storage.
        # MemorySaver stores checkpoints in a dict-like `.storage` attribute
        # (or `.checkpoints` depending on the LangGraph version).
        removed = 0
        for tid in stale_thread_ids:
            # Try known internal attribute names for MemorySaver
            storage = getattr(self.checkpointer, "storage", None) or getattr(self.checkpointer, "checkpoints", None)
            if isinstance(storage, dict) and tid in storage:
                del storage[tid]
                removed += 1
            # Clean up our TTL tracking regardless
            del self._thread_timestamps[tid]

        if removed > 0:
            logger.info(
                "[StateGraph] Cleaned up %d stale thread(s) (max_age=%ds)",
                removed,
                max_age_seconds,
            )

        return removed

    def get_initial_state(self) -> StateGraphState:
        """Retorna el estado inicial del workflow."""
        return {
            "workflow_id": f"wf_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}",
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
            # Cortacircuitos cognitivo
            "loop_detected": False,
            "loop_context": None,
            "hitl_started_at": None,
        }

    async def execute(self, initial_state: StateGraphState | None = None) -> StateGraphState:
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
        thread_id: str = state["workflow_id"]
        config = {"configurable": {"thread_id": thread_id}}

        # TASK-006 (M-4): Track thread access time for TTL-based cleanup
        self._thread_timestamps[thread_id] = time.monotonic()

        try:
            # Ejecutar el grafo
            result = await self.compiled_graph.ainvoke(state, config)
            return result  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"[StateGraph] Error ejecutando workflow: {e}")
            state["last_error"] = str(e)
            state["current_state"] = SupervisorState.ERROR.value
            return state

    async def _execute_fallback(self, initial_state: StateGraphState | None = None) -> StateGraphState:
        """Ejecución fallback sin LangGraph.

        M-7 FIX: The final state now reflects the actual outcome:
        - COMPLETED only when dispatching succeeds (no ``last_error``).
        - ERROR when ``last_error`` is set (tool failure, bad state, etc.).
        - IDLE for unrecognized events (no false COMPLETED).
        """
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

                    # M-7 FIX: reflect actual dispatch outcome
                    if state.get("last_error"):
                        state["current_state"] = SupervisorState.ERROR.value
                        state["previous_state"] = SupervisorState.DISPATCHING.value
                    else:
                        state["current_state"] = SupervisorState.COMPLETED.value
                        state["previous_state"] = SupervisorState.DISPATCHING.value
                else:
                    # No tool to dispatch — analysis completed successfully
                    state["current_state"] = SupervisorState.COMPLETED.value
                    state["previous_state"] = SupervisorState.ANALYZING.value
            else:
                # Unrecognized event type — stay IDLE, don't falsely claim COMPLETED
                logger.warning(
                    "[StateGraph] Fallback: unrecognized event '%s', staying IDLE",
                    event,
                )

        return state

    async def submit_event(
        self,
        event_type: WorkflowEventType,
        event_data: dict[str, Any] | None = None,
        thread_id: str | None = None,
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

    def visualize(self, output_path: str | None = None) -> str | None:
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

            return mermaid_png  # type: ignore[no-any-return]
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

    def get_state_summary(self, state: StateGraphState) -> dict[str, Any]:
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
        self.state_graph.register_callback(SupervisorState.ANALYZING, self._on_analyzing)
        self.state_graph.register_callback(SupervisorState.DISPATCHING, self._on_dispatching)
        self.state_graph.register_callback(SupervisorState.HITL_WAIT, self._on_hitl_wait)

        logger.info("[StateGraph] Supervisor conectado exitosamente")

    async def _on_analyzing(self, state: StateGraphState) -> None:
        """Callback para estado ANALYZING."""
        if self._supervisor:
            await self._supervisor._trigger_proactive_analysis()

    async def _on_dispatching(self, state: StateGraphState) -> None:
        """Callback para estado DISPATCHING.

        Invoca el AgenticLoopGuardrail antes de dispatch. Si detecta un bucle,
        marca ``loop_detected`` y ``hitl_request`` para que ``route_from_dispatching``
        transite a HITL_WAIT; no invoca la tool.
        """
        if not self._supervisor:
            return
        tool_name = state.get("tool_name")
        payload = state.get("tool_payload") or {}

        # FIX 2: Validate tool_name before guardrail invocation
        if not tool_name or not isinstance(tool_name, str):
            state["last_error"] = "tool_name is None or invalid"
            state["tool_result"] = {
                "status": "error",
                "error": "Tool dispatch aborted: tool_name is missing or invalid",
            }
            return

        try:
            self.state_graph.loop_guardrail.register_and_check(tool_name, payload)
        except CircuitBreakerTrippedError as exc:
            state["loop_detected"] = True
            state["loop_context"] = {
                "tool_name": exc.tool_name,
                "occurrences": exc.occurrences,
            }
            # FIX 1: Use valid action_type and move context into context_data
            state["hitl_request"] = {
                "action_type": "circuit_breaker_halt",
                "reason": str(exc),
                "context_data": {
                    "trip_reason": "Loop detected",
                    "tool_name": exc.tool_name,
                    "occurrences": exc.occurrences,
                },
            }
            return
        result = await self._supervisor.dispatch_tool(tool_name, payload)
        state["tool_result"] = result

    async def _on_hitl_wait(self, state: StateGraphState) -> None:
        """Callback para estado HITL_WAIT."""
        if self._supervisor:
            hitl_request = state.get("hitl_request") or {}
            response = await self._supervisor.interface.request_hitl(
                self._supervisor._create_hitl_request(hitl_request)
            )
            state["hitl_response"] = response
            if response == "approved" and state.get("loop_detected"):
                self.state_graph.loop_guardrail.reset()
                state["loop_detected"] = False
                state["loop_context"] = None

    def translate_modlist_event(self, mtime: float, path: str) -> dict[str, Any]:
        """Traduce un evento de cambio de modlist al formato del grafo."""
        return {
            "event_type": WorkflowEventType.MODLIST_CHANGED,
            "event_data": {"mtime": mtime, "path": path, "requires_hitl": False},
        }

    def translate_user_command(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        """Traduce un comando de usuario al formato del grafo."""
        return {
            "event_type": WorkflowEventType.USER_COMMAND,
            "event_data": {"command": command, "params": params},
        }


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Availability flags
    "LANGGRAPH_AVAILABLE",
    # TASK-006 (M-4): Public constants
    "MAX_TRANSITION_HISTORY",
    # Reducer functions
    "capped_transition_history",
    # Integration helpers
    "StateGraphEdges",
    "StateGraphIntegration",
    # Graph components
    "StateGraphNodes",
    "StateGraphState",
    # Enums
    "SupervisorState",
    "SupervisorStateGraph",
    "WorkflowEventType",
    # State classes
    "WorkflowState",
    # Factory
    "create_supervisor_state_graph",
]
