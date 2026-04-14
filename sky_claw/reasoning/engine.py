"""
engine.py - Motor principal Tree-of-Thought (Patrón Facade).

Este módulo contiene el motor principal ToT y sus componentes auxiliares,
desacoplados del monolito tot.py como parte del refactoring M-15.

Contenido:
- CycleDetector: Detector de estados repetidos
- PruningPolicy: Política de poda dinámica
- TreeOfThoughtEngine: Motor principal con15+ métodos de orquestación
- create_tot_engine: Factory function
- DefaultThoughtGenerator: Implementación por defecto
- DefaultThoughtEvaluator: Implementación por defecto
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Generic

# Importar desde el módulo strategies
from .strategies import create_search_strategy

# Importar desde el módulo types
from .types import (
    EvaluationResult,
    PruningMethod,
    S,
    SolutionChecker,
    T,
    ThoughtEvaluator,
    ThoughtGenerator,
    ThoughtNode,
    ToTConfig,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# ============================================================================
# Detector de Ciclos
# ============================================================================


class CycleDetector:
    """Detector de estados repetidos para evitar ciclos en la búsqueda."""

    def __init__(self, state_hash_func: Callable[[Any], str] | None = None):
        """
        Inicializa el detector de ciclos.

        Args:
            state_hash_func: Función para generar hash único del estado.
                           Si es None, usa str() por defecto.
        """
        self._state_hash_func = state_hash_func
        self._visited_states: set[str] = set()

    def is_visited(self, node: ThoughtNode[T]) -> bool:
        """Verifica si el estado del nodo ya fue visitado."""
        state_hash = node.get_state_hash(self._state_hash_func)
        return state_hash in self._visited_states

    def mark_visited(self, node: ThoughtNode[T]) -> None:
        """Marca el estado del nodo como visitado."""
        state_hash = node.get_state_hash(self._state_hash_func)
        self._visited_states.add(state_hash)

    def reset(self) -> None:
        """Limpia el registro de estados visitados."""
        self._visited_states.clear()

    @property
    def visited_count(self) -> int:
        """Retorna el número de estados visitados."""
        return len(self._visited_states)


# ============================================================================
# Política de Poda Dinámica
# ============================================================================


class PruningPolicy:
    """Política de poda dinámica para filtrar nodos."""

    def __init__(self, config: ToTConfig):
        self._config = config

    def apply(self, nodes: list[ThoughtNode[T]]) -> list[ThoughtNode[T]]:
        """
        Aplica la política de poda a una lista de nodos.

        Args:
            nodes: Lista de nodos a filtrar

        Returns:
            Lista de nodos que pasan el filtro
        """
        if not nodes:
            return nodes

        # Filtrar nodos marcados como DEAD_END
        alive_nodes = [n for n in nodes if n.evaluation != EvaluationResult.DEAD_END]

        if self._config.pruning_method == PruningMethod.FIXED_THRESHOLD:
            return self._apply_fixed_threshold(alive_nodes)
        elif self._config.pruning_method == PruningMethod.RELATIVE_TOP_K:
            return self._apply_top_k(alive_nodes)
        elif self._config.pruning_method == PruningMethod.RELATIVE_PERCENTILE:
            return self._apply_percentile(alive_nodes)
        else:
            return alive_nodes

    def _apply_fixed_threshold(
        self, nodes: list[ThoughtNode[T]]
    ) -> list[ThoughtNode[T]]:
        """Aplica umbral fijo de poda."""
        result = []
        for node in nodes:
            if node.score < self._config.pruning_threshold:
                node.evaluation = EvaluationResult.DEAD_END
                logger.debug(
                    f"Pruned node {node.id} with score {node.score:.2f} < {self._config.pruning_threshold}"
                )
            else:
                result.append(node)
        return result

    def _apply_top_k(self, nodes: list[ThoughtNode[T]]) -> list[ThoughtNode[T]]:
        """Mantiene solo los K mejores nodos."""
        if len(nodes) <= self._config.pruning_top_k:
            return nodes

        sorted_nodes = sorted(nodes, key=lambda n: n.score, reverse=True)
        kept_nodes = sorted_nodes[: self._config.pruning_top_k]
        pruned_nodes = sorted_nodes[self._config.pruning_top_k :]

        for node in pruned_nodes:
            node.evaluation = EvaluationResult.DEAD_END
            logger.debug(
                f"Pruned node {node.id} with score {node.score:.2f} (not in top {self._config.pruning_top_k})"
            )

        return kept_nodes

    def _apply_percentile(self, nodes: list[ThoughtNode[T]]) -> list[ThoughtNode[T]]:
        """Mantiene nodos en el percentil X superior."""
        if len(nodes) <= 1:
            return nodes

        scores = [n.score for n in nodes]
        scores_sorted = sorted(scores)
        threshold_index = int(len(scores_sorted) * self._config.pruning_percentile)
        threshold = scores_sorted[threshold_index]

        result = []
        for node in nodes:
            if node.score < threshold:
                node.evaluation = EvaluationResult.DEAD_END
                logger.debug(
                    f"Pruned node {node.id} with score {node.score:.2f} < percentile threshold {threshold:.2f}"
                )
            else:
                result.append(node)

        return result


# ============================================================================
# Motor Tree-of-Thought (Facade)
# ============================================================================


class TreeOfThoughtEngine(Generic[T, S]):
    """
    Motor Tree-of-Thought para razonamiento complejo.

    Este motor implementa el algoritmo ToT que permite explorar
    múltiples caminos de razonamiento de forma sistemática,
    con capacidad de retroceso cuando un camino resulta improductivo.

    Example:
        ```python
        # Definir generador y evaluador
        async def generate_thoughts(state, n, context):
            # Usar LLM para generar pensamientos candidatos
            ...

        async def evaluate_thought(thought, desc, context):
            # Usar LLM para evaluar viabilidad
            ...

        # Crear motor
        engine = TreeOfThoughtEngine(
            thought_generator=generate_thoughts,
            thought_evaluator=evaluate_thoughts,
            solution_checker=lambda s: s if is_valid(s) else None,
            config=ToTConfig(max_depth=5)
        )

        # Ejecutar búsqueda
        result = await engine.solve(initial_state)
        ```
    """

    def __init__(
        self,
        thought_generator: ThoughtGenerator[T],
        thought_evaluator: ThoughtEvaluator[T],
        solution_checker: SolutionChecker[T, S],
        config: ToTConfig | None = None,
        llm_client: Any | None = None,
        state_hash_func: Callable[[T], str] | None = None,
    ):
        """
        Inicializa el motor ToT.

        Args:
            thought_generator: Generador de pensamientos candidatos
            thought_evaluator: Evaluador de viabilidad de pensamientos
            solution_checker: Verificador de soluciones
            config: Configuración del motor
            llm_client: Cliente LLM opcional para generación/evaluación
            state_hash_func: Función para generar hash único del estado
        """
        self._generator = thought_generator
        self._evaluator = thought_evaluator
        self._checker = solution_checker
        self._config = config or ToTConfig()
        self._llm_client = llm_client

        # Crear estrategia de búsqueda
        self._search_strategy = create_search_strategy(
            self._config.search_strategy, self._config
        )

        # Componentes auxiliares
        self._cycle_detector = CycleDetector(state_hash_func)
        self._pruning_policy = PruningPolicy(self._config)

        # Estado interno
        self._root: ThoughtNode[T] | None = None
        self._current_best: ThoughtNode[T] | None = None
        self._solutions: list[S] = []
        self._start_time: datetime | None = None

        logger.info(
            f"TreeOfThoughtEngine initialized with strategy={self._config.search_strategy.value}"
        )

    @property
    def generator(self) -> ThoughtGenerator[T]:
        """Retorna el generador de pensamientos."""
        return self._generator

    @property
    def evaluator(self) -> ThoughtEvaluator[T]:
        """Retorna el evaluador de pensamientos."""
        return self._evaluator

    @property
    def checker(self) -> SolutionChecker[T, S]:
        """Retorna el verificador de soluciones."""
        return self._checker

    @property
    def cycle_detector(self) -> CycleDetector:
        """Retorna el detector de ciclos."""
        return self._cycle_detector

    @property
    def pruning_policy(self) -> PruningPolicy:
        """Retorna la política de poda."""
        return self._pruning_policy

    @property
    def config(self) -> ToTConfig:
        """Retorna la configuración del motor."""
        return self._config

    def add_solution(self, solution: S) -> None:
        """Agrega una solución a la lista de soluciones encontradas."""
        self._solutions.append(solution)

    async def solve(
        self, initial_state: T, initial_thought: str = "Estado inicial del problema"
    ) -> S | None:
        """
        Ejecuta el algoritmo ToT para encontrar una solución.

        Args:
            initial_state: Estado inicial del problema
            initial_thought: Descripción del estado inicial

        Returns:
            La solución encontrada o None si no se encuentra
        """
        logger.info(
            f"Starting ToT search with strategy={self._config.search_strategy.value}"
        )
        self._start_time = datetime.now(UTC)

        # Resetear estado
        self._solutions = []
        self._cycle_detector.reset()

        # Crear nodo raíz
        self._root = ThoughtNode[T](
            state=initial_state, thought=initial_thought, depth=0, score=1.0
        )

        try:
            # Envolver búsqueda con timeout usando asyncio.wait_for
            solution = await asyncio.wait_for(
                self._search_strategy.search(self._root, self),
                timeout=self._config.timeout_seconds,
            )

            elapsed = (datetime.now(UTC) - self._start_time).total_seconds()
            logger.info(
                f"ToT search completed in {elapsed:.2f}s, solution_found={solution is not None}"
            )

            return solution

        except TimeoutError:
            logger.warning(
                f"ToT search timed out after {self._config.timeout_seconds}s, returning best solution found"
            )
            return self._get_best_solution()
        except Exception as e:
            logger.error(f"ToT search failed: {e}")
            raise

    async def expand_node(self, node: ThoughtNode[T]) -> list[ThoughtNode[T]]:
        """
        Expande un nodo generando y evaluando pensamientos candidatos.

        Args:
            node: Nodo a expandir

        Returns:
            Lista de nodos hijos creados
        """
        context = {
            "depth": node.depth,
            "path": [n.thought for n in node.get_path_to_root()],
        }

        # Generar pensamientos candidatos
        try:
            candidates = await asyncio.wait_for(
                self._generator.generate(
                    current_state=node.state,
                    n=self._config.max_thoughts_per_step,
                    context=context,
                ),
                timeout=self._config.thought_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(f"Thought generation timed out for node {node.id}")
            return []

        if self._config.enable_parallel_expansion and len(candidates) > 1:
            # Expansión paralela
            children = await self._expand_candidates_parallel(node, candidates, context)
        else:
            # Expansión secuencial
            children = await self._expand_candidates_sequential(
                node, candidates, context
            )

        logger.debug(f"Expanded node {node.id}: {len(children)} children")
        return children

    async def _expand_candidates_sequential(
        self,
        node: ThoughtNode[T],
        candidates: list[tuple[T, str]],
        context: dict[str, Any],
    ) -> list[ThoughtNode[T]]:
        """Expande candidatos de forma secuencial."""
        children = []

        for state, thought in candidates:
            child = await self._create_child_node(node, state, thought, context)
            if child:
                children.append(child)

        return children

    async def _expand_candidates_parallel(
        self,
        node: ThoughtNode[T],
        candidates: list[tuple[T, str]],
        context: dict[str, Any],
    ) -> list[ThoughtNode[T]]:
        """Expande candidatos en paralelo usando asyncio.gather."""
        # Limitar paralelismo
        batch_size = min(len(candidates), self._config.max_parallel_expansions)
        batches = [
            candidates[i : i + batch_size]
            for i in range(0, len(candidates), batch_size)
        ]

        all_children = []

        for batch in batches:
            tasks = [
                self._create_child_node(node, state, thought, context)
                for state, thought in batch
            ]

            children = await asyncio.gather(*tasks, return_exceptions=True)

            for child in children:
                if isinstance(child, Exception):
                    logger.error(
                        f"Critical error creating child node in parallel batch: {child}",
                        exc_info=True,
                    )
                elif child is not None:
                    all_children.append(child)

        return all_children

    async def _create_child_node(
        self, parent: ThoughtNode[T], state: T, thought: str, context: dict[str, Any]
    ) -> ThoughtNode[T] | None:
        """Crea un nodo hijo evaluando el candidato."""
        try:
            # Evaluar pensamiento
            score, evaluation = await self._evaluator.evaluate(
                thought=state, thought_description=thought, context=context
            )

            # Crear nodo hijo
            child = ThoughtNode[T](
                state=state,
                thought=thought,
                depth=parent.depth + 1,
                score=score,
                evaluation=evaluation,
            )

            parent.add_child(child)

            # Verificar si es solución
            if score >= self._config.solution_threshold:
                solution = self._checker.check(state)
                if solution:
                    self._solutions.append(solution)
                    child.evaluation = EvaluationResult.SOLUTION

            return child

        except Exception as e:
            logger.error(f"Error creating child node: {e}")
            return None

    def _get_best_solution(self) -> S | None:
        """Retorna la mejor solución encontrada o None."""
        if self._solutions:
            return self._solutions[0]
        return None

    def get_tree_stats(self) -> dict[str, Any]:
        """Retorna estadísticas del árbol de búsqueda."""
        if self._root is None:
            return {"error": "No tree built yet"}

        all_nodes = [self._root, *self._root.get_all_descendants()]

        return {
            "total_nodes": len(all_nodes),
            "max_depth_reached": max(n.depth for n in all_nodes),
            "solutions_found": len(self._solutions),
            "pruned_nodes": sum(
                1 for n in all_nodes if n.evaluation == EvaluationResult.DEAD_END
            ),
            "promising_nodes": sum(
                1 for n in all_nodes if n.evaluation == EvaluationResult.PROMISING
            ),
            "avg_score": sum(n.score for n in all_nodes) / len(all_nodes)
            if all_nodes
            else 0,
            "states_visited": self._cycle_detector.visited_count,
        }

    def export_tree(self) -> dict[str, Any]:
        """Exporta el árbol completo para visualización."""
        if self._root is None:
            return {}

        def node_to_dict(node: ThoughtNode[T]) -> dict[str, Any]:
            return {
                "id": node.id,
                "thought": node.thought[:100] + "..."
                if len(node.thought) > 100
                else node.thought,
                "depth": node.depth,
                "score": round(node.score, 3),
                "evaluation": node.evaluation.value,
                "children": [node_to_dict(c) for c in node.children],
            }

        return node_to_dict(self._root)


# ============================================================================
# Factory Function
# ============================================================================


def create_tot_engine(
    llm_generate_func: Callable,
    llm_evaluate_func: Callable,
    solution_check_func: Callable,
    config: ToTConfig | None = None,
    state_hash_func: Callable[[Any], str] | None = None,
) -> TreeOfThoughtEngine:
    """
    Factory function para crear un motor ToT con funciones simples.

    Args:
        llm_generate_func: Función async (state, n, context) -> List[Tuple[state, thought]]
        llm_evaluate_func: Función async (thought, desc, context) -> Tuple[float, EvaluationResult]
        solution_check_func: Función (state) -> Optional[solution]
        config: Configuración del motor
        state_hash_func: Función para generar hash único del estado

    Returns:
        Motor ToT configurado
    """

    class SimpleThoughtGenerator:
        async def generate(self, current_state, n, context):
            return await llm_generate_func(current_state, n, context)

    class SimpleThoughtEvaluator:
        async def evaluate(self, thought, thought_description, context):
            return await llm_evaluate_func(thought, thought_description, context)

    class SimpleSolutionChecker:
        def check(self, state):
            return solution_check_func(state)

    return TreeOfThoughtEngine(
        thought_generator=SimpleThoughtGenerator(),
        thought_evaluator=SimpleThoughtEvaluator(),
        solution_checker=SimpleSolutionChecker(),
        config=config,
        state_hash_func=state_hash_func,
    )


# ============================================================================
# Implementaciones por Defecto (Requieren LLM real)
# ============================================================================


class DefaultThoughtGenerator:
    """
    Generador de pensamientos por defecto.

    IMPORTANTE: Esta clase lanza NotImplementedError si no se proporciona
    un cliente LLM real, evitando comportamientos aleatorios que ocultan errores.
    """

    def __init__(self, llm_client: Any | None = None):
        self._llm = llm_client

    async def generate(
        self, current_state: Any, n: int = 3, context: dict[str, Any] | None = None
    ) -> list[tuple[Any, str]]:
        """
        Genera pensamientos candidatos usando el LLM.

        Raises:
            NotImplementedError: Si no se proporcionó un cliente LLM real
        """
        if self._llm is None:
            raise NotImplementedError(
                "DefaultThoughtGenerator requiere un cliente LLM real. "
                "Inyecte un llm_client en el constructor o proporcione "
                "una implementación personalizada de ThoughtGenerator."
            )

        # Modo LLM: usar el cliente para generar
        # La implementación específica depende del tipo de LLM
        # Este es un placeholder que debe ser sobrescrito
        raise NotImplementedError(
            "LLM-based generation requires implementation. "
            "Subclass DefaultThoughtGenerator and implement the generate method."
        )


class DefaultThoughtEvaluator:
    """
    Evaluador de pensamientos por defecto.

    IMPORTANTE: Esta clase lanza NotImplementedError si no se proporciona
    un cliente LLM real, evitando comportamientos aleatorios que ocultan errores.
    """

    def __init__(self, llm_client: Any | None = None):
        self._llm = llm_client

    async def evaluate(
        self,
        thought: Any,
        thought_description: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[float, EvaluationResult]:
        """
        Evalúa un pensamiento usando el LLM.

        Raises:
            NotImplementedError: Si no se proporcionó un cliente LLM real
        """
        if self._llm is None:
            raise NotImplementedError(
                "DefaultThoughtEvaluator requiere un cliente LLM real. "
                "Inyecte un llm_client en el constructor o proporcione "
                "una implementación personalizada de ThoughtEvaluator."
            )

        # Modo LLM: usar el cliente para evaluar
        raise NotImplementedError(
            "LLM-based evaluation requires implementation. "
            "Subclass DefaultThoughtEvaluator and implement the evaluate method."
        )


__all__ = [
    "CycleDetector",
    "DefaultThoughtEvaluator",
    "DefaultThoughtGenerator",
    "PruningPolicy",
    "TreeOfThoughtEngine",
    "create_tot_engine",
]
