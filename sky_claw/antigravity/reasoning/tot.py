"""
Tree of Thoughts (ToT) Reasoning Module - Facade

This module provides a unified API for Tree of Thoughts reasoning capabilities.
The implementation has been modularized into separate components:

- types.py: Core type definitions, protocols, and data structures
- strategies.py: Search strategy implementations (BFS, DFS, MCTS, etc.)
- engine.py: Main engine, cycle detection, pruning policies

For direct access to specific components, import from the submodules directly.

References:
- Yao et al. (2023) "Tree of Thoughts: Deliberate Problem Solving with Large Language Models"
- Long (2023) "Large Language Models Are Zero-Shot Reasoners"
"""

# Types and Protocols
# Engine and Components
from .engine import (
    CycleDetector,
    DefaultThoughtEvaluator,
    DefaultThoughtGenerator,
    PruningPolicy,
    TreeOfThoughtEngine,
    create_tot_engine,
)

# Search Strategies
from .strategies import (
    BaseSearchStrategy,
    BeamSearchStrategy,
    BestFirstSearchStrategy,
    BFSSearchStrategy,
    DFSSearchStrategy,
    MCTSSearchStrategy,
    create_search_strategy,
)
from .types import (
    EvaluationResult,
    PruningMethod,
    SearchStrategyProtocol,
    SearchStrategyType,
    SolutionChecker,
    ThoughtEvaluator,
    ThoughtGenerator,
    ThoughtNode,
    ToTConfig,
)

__all__ = [
    "BFSSearchStrategy",
    # Estrategias de búsqueda
    "BaseSearchStrategy",
    "BeamSearchStrategy",
    "BestFirstSearchStrategy",
    # Componentes auxiliares
    "CycleDetector",
    "DFSSearchStrategy",
    "DefaultThoughtEvaluator",
    # Implementaciones por defecto
    "DefaultThoughtGenerator",
    "EvaluationResult",
    "MCTSSearchStrategy",
    "PruningMethod",
    "PruningPolicy",
    "SearchStrategyProtocol",
    "SearchStrategyType",
    "SolutionChecker",
    "ThoughtEvaluator",
    # Protocolos
    "ThoughtGenerator",
    # Estructuras de datos
    "ThoughtNode",
    # Configuración
    "ToTConfig",
    # Motor principal
    "TreeOfThoughtEngine",
    "create_search_strategy",
    # Factory
    "create_tot_engine",
]
