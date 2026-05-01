"""
strategies.py - Estrategias de búsqueda para el motor Tree-of-Thought.

Este módulo contiene todas las estrategias de búsqueda concretas utilizadas
por el motor ToT, desacopladas del monolito tot.py como parte del
refactoring M-15 (Patrón Facade).

Contenido:
- BaseSearchStrategy: Clase base abstracta para estrategias
- BFSSearchStrategy: Búsqueda en anchura (con deque.popleft O(1))
- DFSSearchStrategy: Búsqueda en profundidad con backtracking
- BestFirstSearchStrategy: Búsqueda best-first con cola de prioridad
- BeamSearchStrategy: Búsqueda beam (k mejores por nivel)
- MCTSSearchStrategy: Monte Carlo Tree Search con UCB1
- create_search_strategy: Factory function
"""

from __future__ import annotations

import heapq
import math
from abc import ABC, abstractmethod
from collections import deque
from typing import TYPE_CHECKING, Generic

# Importar desde el módulo types creado anteriormente
from .types import (
    EvaluationResult,
    S,
    SearchStrategyType,
    T,
    ThoughtNode,
    ToTConfig,
)

if TYPE_CHECKING:
    # Importación circular solo para type hints
    from .tot import TreeOfThoughtEngine

import logging

logger = logging.getLogger(__name__)


# ============================================================================
# Clase Base Abstracta
# ============================================================================


class BaseSearchStrategy(ABC, Generic[T, S]):
    """Clase base para estrategias de búsqueda."""

    def __init__(self, config: ToTConfig):
        self._config = config

    @abstractmethod
    async def search(self, root: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]) -> S | None:
        """Ejecuta la búsqueda desde el nodo raíz."""
        ...

    def _check_solution(self, node: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]) -> S | None:
        """Verifica si un nodo contiene una solución."""
        solution = engine.checker.check(node.state)
        if solution:
            engine.add_solution(solution)
            logger.info(f"Solution found at depth {node.depth}")
        return solution


# ============================================================================
# Estrategias Concretas
# ============================================================================


class BFSSearchStrategy(BaseSearchStrategy[T, S]):
    """Búsqueda en anchura (Breadth-First Search)."""

    async def search(self, root: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]) -> S | None:
        queue = deque([root])

        while queue:
            current = queue.popleft()  # O(1) - CRÍTICO: NO CAMBIAR

            # Verificar si es solución
            solution = self._check_solution(current, engine)
            if solution:
                return solution

            # Verificar profundidad máxima
            if current.depth >= self._config.max_depth:
                continue

            # Verificar ciclo
            if engine.cycle_detector.is_visited(current):
                continue
            engine.cycle_detector.mark_visited(current)

            # Expandir nodo
            children = await engine.expand_node(current)

            # Aplicar política de poda
            children = engine.pruning_policy.apply(children)

            # Agregar hijos a la cola (BFS: FIFO)
            queue.extend(children)

        return None


class DFSSearchStrategy(BaseSearchStrategy[T, S]):
    """Búsqueda en profundidad con backtracking explícito."""

    async def search(self, root: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]) -> S | None:
        # Pila con (nodo, índice_de_exploración_de_hijos)
        stack: list[tuple[ThoughtNode[T], int]] = [(root, 0)]
        backtrack_count = 0

        while stack:
            current, child_idx = stack[-1]  # Peek sin remover

            # Verificar si es solución
            solution = self._check_solution(current, engine)
            if solution:
                return solution

            # Verificar ciclo
            # PREVENCIÓN: Solo verificar ciclos si es la primera vez que evaluamos este nodo (child_idx == 0)
            if child_idx == 0:
                if engine.cycle_detector.is_visited(current):
                    stack.pop()
                    continue
                engine.cycle_detector.mark_visited(current)

            # Verificar profundidad máxima
            if current.depth >= self._config.max_depth:
                # Backtracking explícito
                if self._config.enable_backtracking and backtrack_count < self._config.max_backtracks:
                    backtrack_count += 1
                    logger.debug(f"Backtracking from depth {current.depth} (count: {backtrack_count})")
                    stack.pop()
                    continue
                else:
                    stack.pop()
                    continue

            # Expandir nodo si no tiene hijos o necesitamos más
            if not current.children and child_idx == 0:
                children = await engine.expand_node(current)
                children = engine.pruning_policy.apply(children)
                # Los hijos ya fueron agregados al nodo durante expand_node

            # Navegar al siguiente hijo
            if child_idx < len(current.children):
                # Actualizar índice del hijo actual en la pila
                stack[-1] = (current, child_idx + 1)
                # Agregar hijo a la pila
                stack.append((current.children[child_idx], 0))
            else:
                # No más hijos, backtracking
                stack.pop()

        return None


class BestFirstSearchStrategy(BaseSearchStrategy[T, S]):
    """Búsqueda best-first (prioriza nodos con mayor score)."""

    async def search(self, root: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]) -> S | None:
        # Cola de prioridad: (-score, id, node) - negativo para max-heap
        priority_queue: list[tuple[float, str, ThoughtNode[T]]] = [(-root.score, root.id, root)]
        visited_ids: set[str] = set()

        while priority_queue:
            _, _, current = heapq.heappop(priority_queue)

            # Evitar visitar el mismo nodo múltiples veces
            if current.id in visited_ids:
                continue
            visited_ids.add(current.id)

            # Verificar ciclo
            if engine.cycle_detector.is_visited(current):
                continue
            engine.cycle_detector.mark_visited(current)

            # Verificar solución
            solution = self._check_solution(current, engine)
            if solution:
                return solution

            # Verificar profundidad
            if current.depth >= self._config.max_depth:
                continue

            # Expandir nodo
            children = await engine.expand_node(current)
            children = engine.pruning_policy.apply(children)

            # Agregar hijos a la cola de prioridad
            for child in children:
                heapq.heappush(priority_queue, (-child.score, child.id, child))

        return None


class BeamSearchStrategy(BaseSearchStrategy[T, S]):
    """Búsqueda beam (mantiene solo los k mejores nodos por nivel)."""

    async def search(self, root: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]) -> S | None:
        current_level = [root]

        for _depth in range(self._config.max_depth):
            if not current_level:
                break

            next_level = []

            for node in current_level:
                # Verificar ciclo
                if engine.cycle_detector.is_visited(node):
                    continue
                engine.cycle_detector.mark_visited(node)

                # Verificar solución
                solution = self._check_solution(node, engine)
                if solution:
                    return solution

                # Expandir nodo
                children = await engine.expand_node(node)
                next_level.extend(children)

            # Aplicar poda y mantener solo los beam_width mejores
            next_level = engine.pruning_policy.apply(next_level)
            next_level.sort(key=lambda n: n.score, reverse=True)
            current_level = next_level[: self._config.beam_width]

        return None


class MCTSSearchStrategy(BaseSearchStrategy[T, S]):
    """Búsqueda Monte Carlo Tree Search con UCB1 correcto."""

    async def search(self, root: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]) -> S | None:
        # Inicializar metadatos MCTS en la raíz
        root.metadata["mcts_visits"] = 1
        root.metadata["mcts_total_score"] = root.score

        for _iteration in range(self._config.mcts_iterations):
            # Selection: Encontrar nodo prometedor usando UCB1
            node = self._select_promising_node(root)

            # Verificar solución en nodo seleccionado
            solution = self._check_solution(node, engine)
            if solution:
                return solution

            if node.depth >= self._config.max_depth:
                continue

            # Expansion: Expandir el nodo
            children = await engine.expand_node(node)

            if not children:
                continue

            # Simulation y Backpropagation para cada hijo
            for child in children:
                # Verificar ciclo antes de simular
                if engine.cycle_detector.is_visited(child):
                    continue
                engine.cycle_detector.mark_visited(child)

                # Realizar rollout
                result = await self._simulate(child, engine)

                # Backpropagation
                self._backpropagate(child, result)

                # Verificar solución
                solution = self._check_solution(child, engine)
                if solution:
                    return solution

        # Retornar el mejor hijo de la raíz
        if root.children:
            best_child = max(root.children, key=lambda c: c.metadata.get("mcts_visits", 0))
            return engine.checker.check(best_child.state)

        return None

    def _select_promising_node(self, root: ThoughtNode[T]) -> ThoughtNode[T]:
        """
        Selecciona el nodo más prometedor usando UCB1.

        Fórmula UCB1: exploitation + C * sqrt(ln(N_parent) / N_child)
        donde:
        - exploitation = score promedio del nodo
        - C = constante de exploración (configurable)
        - N_parent = suma de visitas de todos los hijos
        - N_child = visitas del nodo hijo
        """
        node = root
        exploration_c = self._config.mcts_exploration_constant

        while node.children:
            # LÓGICA: N_parent son las visitas reales del padre, no la suma de los hijos
            parent_visits = node.metadata.get("mcts_visits", 1)

            # Protección contra log(1) == 0 en etapa temprana de exploración
            if parent_visits <= 1:
                # Si ningún hijo ha sido visitado, seleccionar aleatoriamente
                # pero evitar error de lista vacía
                return node.children[0] if node.children else node

            best_score = float("-inf")
            best_child = None

            for child in node.children:
                visits = child.metadata.get("mcts_visits", 0)

                # Si no ha sido visitado, priorizar exploración
                if visits == 0:
                    best_child = child
                    break

                # Calcular UCB1
                exploitation = child.metadata.get("mcts_avg_score", child.score)
                exploration = exploration_c * math.sqrt(math.log(parent_visits) / visits)
                ucb_score = exploitation + exploration

                if ucb_score > best_score:
                    best_score = ucb_score
                    best_child = child

            if best_child is None:
                # Fallback: retornar nodo actual si no hay hijos válidos
                return node

            node = best_child

        return node

    async def _simulate(self, node: ThoughtNode[T], engine: TreeOfThoughtEngine[T, S]) -> float:
        """
        Realiza un rollout desde el nodo hasta la profundidad máxima
        o hasta encontrar una solución.

        Retorna el score acumulado del rollout.
        """
        current = node
        total_score = current.score
        depth = 0

        while depth < self._config.mcts_max_rollout_depth:
            # Verificar si es solución
            if engine.checker.check(current.state):
                return 1.0  # Score máximo para soluciones

            # Generar hijos temporales para el rollout
            try:
                context = {
                    "depth": current.depth,
                    "path": [n.thought for n in current.get_path_to_root()],
                    "rollout": True,
                }

                candidates = await engine.generator.generate(
                    current_state=current.state,
                    n=1,  # Solo un candidato por paso de rollout
                    context=context,
                )

                if not candidates:
                    break

                # Seleccionar el primer candidato (política rápida)
                new_state, new_thought = candidates[0]

                # Evaluar el candidato
                score, evaluation = await engine.evaluator.evaluate(
                    thought=new_state, thought_description=new_thought, context=context
                )

                # Crear nodo temporal para continuar el rollout
                temp_node = ThoughtNode(
                    state=new_state,
                    thought=new_thought,
                    depth=current.depth + 1,
                    score=score,
                )

                # Acumular score con factor de descuento
                discount = 0.9**depth
                total_score += score * discount

                # Si encontramos un dead end, terminar rollout
                if evaluation == EvaluationResult.DEAD_END:
                    break

                current = temp_node
                depth += 1

            except (TimeoutError, Exception) as e:
                logger.debug(f"Rollout error at depth {depth}: {e}")
                break

        return total_score / (depth + 1)  # Score promedio

    def _backpropagate(self, node: ThoughtNode[T], result: float) -> None:
        """Propaga el resultado de la simulación hacia la raíz."""
        current = node

        while current is not None:
            visits = current.metadata.get("mcts_visits", 0) + 1
            total_score = current.metadata.get("mcts_total_score", 0) + result

            current.metadata["mcts_visits"] = visits
            current.metadata["mcts_total_score"] = total_score
            current.metadata["mcts_avg_score"] = total_score / visits

            current = current.parent


# ============================================================================
# Factory de Estrategias
# ============================================================================


def create_search_strategy(strategy_type: SearchStrategyType, config: ToTConfig) -> BaseSearchStrategy[T, S]:
    """
    Factory para crear instancias de estrategias de búsqueda.

    Args:
        strategy_type: Tipo de estrategia a crear
        config: Configuración del motor ToT

    Returns:
        Instancia de la estrategia de búsqueda
    """
    strategies = {
        SearchStrategyType.BFS: BFSSearchStrategy,
        SearchStrategyType.DFS: DFSSearchStrategy,
        SearchStrategyType.BEST_FIRST: BestFirstSearchStrategy,
        SearchStrategyType.BEAM: BeamSearchStrategy,
        SearchStrategyType.MCTS: MCTSSearchStrategy,
    }

    strategy_class = strategies.get(strategy_type)
    if strategy_class is None:
        raise ValueError(f"Unknown search strategy: {strategy_type}")

    return strategy_class(config)


__all__ = [
    "BFSSearchStrategy",
    "BaseSearchStrategy",
    "BeamSearchStrategy",
    "BestFirstSearchStrategy",
    "DFSSearchStrategy",
    "MCTSSearchStrategy",
    "create_search_strategy",
]
