"""
types.py - Definiciones de tipos base para el motor Tree-of-Thought.

Este módulo contiene todos los tipos, protocolos y estructuras de datos
fundamentales utilizados por el motor ToT, desacoplados del monolito tot.py
como parte del refactoring M-15 (Patrón Facade).

Contenido:
- TypeVars genéricos (T, S)
- Enums (SearchStrategyType, EvaluationResult, PruningMethod)
- Configuración Pydantic (ToTConfig)
- Estructura de datos principal (ThoughtNode)
- Protocolos de dependencia (ThoughtGenerator, ThoughtEvaluator, etc.)
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Callable

    from sky_claw.reasoning.engine import TreeOfThoughtEngine


# ============================================================================
# Tipos Genéricos
# ============================================================================

T = TypeVar("T")  # Tipo de estado del pensamiento
S = TypeVar("S")  # Tipo de solución


# ============================================================================
# Enums y Configuración
# ============================================================================


class SearchStrategyType(StrEnum):
    """Tipos de estrategias de búsqueda para explorar el árbol de pensamientos."""

    BFS = "bfs"  # Breadth-First Search - Explora nivel por nivel
    DFS = "dfs"  # Depth-First Search - Explora en profundidad
    BEST_FIRST = "best_first"  # Best-First Search - Prioriza nodos con mayor score
    BEAM = "beam"  # Beam Search - Mantiene solo los k mejores nodos por nivel
    MCTS = "mcts"  # Monte Carlo Tree Search - Exploración con simulaciones


class EvaluationResult(StrEnum):
    """Resultados posibles de la evaluación de un pensamiento."""

    PROMISING = "promising"  # Vale la pena explorar
    DEAD_END = "dead_end"  # Camino sin salida, podar
    SOLUTION = "solution"  # Solución encontrada
    UNCERTAIN = "uncertain"  # Necesita más exploración


class PruningMethod(StrEnum):
    """Métodos de poda dinámica."""

    FIXED_THRESHOLD = "fixed_threshold"  # Umbral fijo (legacy)
    RELATIVE_TOP_K = "relative_top_k"  # Mantener los K mejores
    RELATIVE_PERCENTILE = "relative_percentile"  # Mantener percentil X


class ToTConfig(BaseModel):
    """Configuración del motor Tree-of-Thought."""

    model_config = {"extra": "forbid", "strict": True}

    # Parámetros de búsqueda
    max_depth: int = Field(
        default=5, ge=1, le=10, description="Profundidad máxima del árbol"
    )
    max_thoughts_per_step: int = Field(
        default=3, ge=1, le=5, description="Pensamientos a generar por paso"
    )
    beam_width: int = Field(
        default=3, ge=1, le=5, description="Ancho del beam para Beam Search"
    )

    # Parámetros de evaluación y poda dinámica
    pruning_method: PruningMethod = Field(
        default=PruningMethod.RELATIVE_TOP_K, description="Método de poda dinámica"
    )
    pruning_threshold: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Umbral fijo para poda (solo si pruning_method=FIXED_THRESHOLD)",
    )
    pruning_top_k: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Número de nodos a mantener (solo si pruning_method=RELATIVE_TOP_K)",
    )
    pruning_percentile: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="Percentil mínimo para mantener (solo si pruning_method=RELATIVE_PERCENTILE)",
    )
    solution_threshold: float = Field(
        default=0.9, ge=0.0, le=1.0, description="Umbral para considerar solución"
    )

    # Parámetros de backtracking
    enable_backtracking: bool = Field(default=True, description="Habilitar retroceso")
    max_backtracks: int = Field(
        default=3, ge=0, le=10, description="Máximo de retrocesos permitidos"
    )

    # Parámetros de timeout
    timeout_seconds: int = Field(
        default=60, ge=10, le=300, description="Timeout total en segundos"
    )
    thought_timeout_seconds: int = Field(
        default=5, ge=1, le=30, description="Timeout por pensamiento"
    )

    # Estrategia de búsqueda
    search_strategy: SearchStrategyType = Field(
        default=SearchStrategyType.BEST_FIRST, description="Estrategia de búsqueda"
    )

    # Parámetros MCTS
    mcts_exploration_constant: float = Field(
        default=1.414, ge=0.0, le=5.0, description="Constante C para UCB1 en MCTS"
    )
    mcts_max_rollout_depth: int = Field(
        default=3, ge=1, le=10, description="Profundidad máxima de rollout en MCTS"
    )
    mcts_iterations: int = Field(
        default=100, ge=10, le=1000, description="Número de iteraciones MCTS"
    )

    # Detección de ciclos
    enable_cycle_detection: bool = Field(
        default=True, description="Habilitar detección de estados repetidos"
    )

    # Paralelismo
    enable_parallel_expansion: bool = Field(
        default=True, description="Habilitar expansión paralela de nodos"
    )
    max_parallel_expansions: int = Field(
        default=5, ge=1, le=10, description="Máximo de expansiones paralelas"
    )


# ============================================================================
# Estructura de Datos: ThoughtNode
# ============================================================================


@dataclass
class ThoughtNode(Generic[T]):
    """
    Nodo del árbol de pensamientos.

    Almacena el estado actual del pensamiento, su valor heurístico,
    y las referencias a nodos padre e hijos para navegación del árbol.

    Attributes:
        id: Identificador único del nodo
        state: Estado actual del pensamiento (tipo genérico T)
        thought: Descripción textual del pensamiento
        depth: Profundidad en el árbol (0 = raíz)
        score: Valor heurístico entre 0.0 y 1.0
        parent: Referencia al nodo padre (None para raíz)
        children: Lista de nodos hijos
        evaluation: Resultado de la evaluación del pensamiento
        created_at: Timestamp de creación (timezone-aware UTC)
        metadata: Metadatos adicionales
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: T = None
    thought: str = ""
    depth: int = 0
    score: float = 0.0
    parent: ThoughtNode[T] | None = None
    children: list[ThoughtNode[T]] = field(default_factory=list)
    evaluation: EvaluationResult = EvaluationResult.UNCERTAIN
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_child(self, child: ThoughtNode[T]) -> None:
        """Agrega un nodo hijo y establece la referencia padre."""
        child.parent = self
        child.depth = self.depth + 1
        self.children.append(child)

    def get_path_to_root(self) -> list[ThoughtNode[T]]:
        """Retorna el camino desde este nodo hasta la raíz."""
        path = [self]
        current = self.parent
        while current is not None:
            path.append(current)
            current = current.parent
        return list(reversed(path))

    def get_all_descendants(self) -> list[ThoughtNode[T]]:
        """Retorna todos los descendientes de este nodo."""
        descendants = []
        for child in self.children:
            descendants.append(child)
            descendants.extend(child.get_all_descendants())
        return descendants

    def is_leaf(self) -> bool:
        """Verifica si este nodo es una hoja (sin hijos)."""
        return len(self.children) == 0

    def get_state_hash(self, hash_func: Callable[[T], str] | None = None) -> str:
        """
        Genera un hash único para el estado de este nodo.

        Args:
            hash_func: Función opcional para generar hash del estado.
                      Si es None, usa representación string por defecto.

        Returns:
            Hash SHA-256 del estado
        """
        state_str = hash_func(self.state) if hash_func else str(self.state)
        return hashlib.sha256(state_str.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Convierte el nodo a diccionario para serialización."""
        return {
            "id": self.id,
            "thought": self.thought,
            "depth": self.depth,
            "score": self.score,
            "evaluation": self.evaluation.value,
            "children_count": len(self.children),
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }


# ============================================================================
# Protocolos para Inyección de Dependencias
# ============================================================================


@runtime_checkable
class ThoughtGenerator(Protocol[T]):
    """Protocolo para generadores de pensamientos candidatos."""

    async def generate(
        self, current_state: T, n: int = 3, context: dict[str, Any] | None = None
    ) -> list[tuple[T, str]]:
        """
        Genera n pensamientos candidatos a partir del estado actual.

        Args:
            current_state: Estado actual del problema
            n: Número de pensamientos a generar
            context: Contexto adicional (historial, restricciones, etc.)

        Returns:
            Lista de tuplas (nuevo_estado, descripción_pensamiento)
        """
        ...


@runtime_checkable
class ThoughtEvaluator(Protocol[T]):
    """Protocolo para evaluadores de pensamientos."""

    async def evaluate(
        self,
        thought: T,
        thought_description: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[float, EvaluationResult]:
        """
        Evalúa un pensamiento y retorna su score y resultado.

        Args:
            thought: Estado del pensamiento a evaluar
            thought_description: Descripción textual del pensamiento
            context: Contexto adicional para la evaluación

        Returns:
            Tupla (score entre 0-1, resultado de evaluación)
        """
        ...


@runtime_checkable
class SolutionChecker(Protocol[T, S]):
    """Protocolo para verificar si un estado es solución."""

    def check(self, state: T) -> S | None:
        """
        Verifica si el estado dado es una solución válida.

        Args:
            state: Estado a verificar

        Returns:
            La solución si es válida, None en caso contrario
        """
        ...


@runtime_checkable
class SearchStrategyProtocol(Protocol[T, S]):
    """Protocolo para estrategias de búsqueda."""

    async def search(
        self, root: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]
    ) -> S | None:
        """
        Ejecuta la búsqueda desde el nodo raíz.

        Args:
            root: Nodo raíz del árbol
            engine: Referencia al motor ToT para expandir nodos

        Returns:
            Solución encontrada o None
        """
        ...


__all__ = [
    "EvaluationResult",
    "PruningMethod",
    "S",
    "SearchStrategyProtocol",
    # Enums
    "SearchStrategyType",
    "SolutionChecker",
    # TypeVars
    "T",
    "ThoughtEvaluator",
    # Protocols
    "ThoughtGenerator",
    # Data structures
    "ThoughtNode",
    # Config
    "ToTConfig",
]
