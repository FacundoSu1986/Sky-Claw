"""Patch Orchestrator - Strategy Pattern for Conflict Resolution.

This module implements the Phase 2 of Sky-Claw: Dynamic Patching & xEdit Integration.
It provides a flexible strategy-based architecture for resolving ESP record conflicts
using different approaches based on conflict type and severity.

Architecture:
    - PatchStrategy (ABC): Abstract interface for conflict resolution strategies
    - CreateMergedPatch: Strategy for leveled list merging (LVLI, LVLN, LVSP)
    - ExecuteXEditScript: Strategy for critical conflicts via Pascal scripts
    - PatchOrchestrator: Main coordinator that selects and executes strategies

Usage:
    orchestrator = PatchOrchestrator(
        xedit_runner=runner,
        snapshot_manager=snapshots,
        rollback_manager=rollback,
    )
    result = await orchestrator.resolve(conflict_report)
"""

from __future__ import annotations

import logging
import pathlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.db.rollback_manager import RollbackManager
    from sky_claw.db.snapshot_manager import FileSnapshotManager
    from sky_claw.xedit.conflict_analyzer import ConflictReport, RecordConflict
    from sky_claw.xedit.runner import XEditRunner

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTION HIERARCHY
# =============================================================================


class PatchingError(Exception):
    """Base exception for patching operations."""

    pass


class StrategySelectionError(PatchingError):
    """No suitable strategy found for conflict."""

    pass


class PatchExecutionError(PatchingError):
    """Error during patch execution."""

    pass


class ScriptGenerationError(PatchingError):
    """Error generating xEdit script."""

    pass


# =============================================================================
# ENUMS AND DATACLASSES
# =============================================================================


class PatchStrategyType(Enum):
    """Available patching strategies."""

    CREATE_MERGED_PATCH = "create_merged_patch"  # Para combinación general de records
    EXECUTE_XEDIT_SCRIPT = (
        "execute_xedit_script"  # Para correcciones específicas de FormID
    )
    FORWARD_DECLARATION = "forward_declaration"  # Para forward de records


@dataclass(frozen=True, slots=True)
class PatchPlan:
    """Plan de parcheo generado por una estrategia.

    Attributes:
        strategy_type: Tipo de estrategia seleccionada.
        target_plugins: Lista de plugins involucrados en el parcheo.
        output_plugin: Nombre del plugin de salida (ej: "SkyClaw_Patch.esp").
        form_ids: Lista de FormIDs afectados por el parcheo.
        estimated_records: Número estimado de records a procesar.
        requires_hitl: Si requiere intervención humana (Human-in-the-Loop).
        script_path: Path al script Pascal si la estrategia lo requiere.
    """

    strategy_type: PatchStrategyType
    target_plugins: list[str]
    output_plugin: str
    form_ids: list[str]  # FormIDs afectados
    estimated_records: int
    requires_hitl: bool
    script_path: pathlib.Path | None = None  # Si requiere script Pascal


@dataclass(frozen=True)
class PatchResult:
    """Resultado de una operación de parcheo (inmutable).

    Esta clase es inmutable para garantizar la integridad del resultado
    después de una operación de parcheo. Use el método de clase `create()`
    para construir instancias con warnings mutables que se convierten en tuple.

    Attributes:
        success: Si la operación fue exitosa.
        output_path: Path al plugin generado (si aplica).
        records_patched: Número de records modificados.
        conflicts_resolved: Número de conflictos resueltos.
        xedit_exit_code: Código de salida de xEdit (0 = éxito).
        warnings: Tupla de advertencias generadas (inmutable).
        error: Mensaje de error si falló la operación.
    """

    success: bool
    output_path: pathlib.Path | None
    records_patched: int
    conflicts_resolved: int
    xedit_exit_code: int
    warnings: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None

    @classmethod
    def create(
        cls,
        success: bool,
        output_path: pathlib.Path | None,
        records_patched: int,
        conflicts_resolved: int,
        xedit_exit_code: int,
        warnings: list[str] | None = None,
        error: str | None = None,
    ) -> "PatchResult":
        """Factory method para crear PatchResult con lista de warnings mutable.

        Args:
            success: Si la operación fue exitosa.
            output_path: Path al plugin generado.
            records_patched: Número de records modificados.
            conflicts_resolved: Número de conflictos resueltos.
            xedit_exit_code: Código de salida de xEdit.
            warnings: Lista de advertencias (se convierte a tuple).
            error: Mensaje de error si aplica.

        Returns:
            PatchResult inmutable.
        """
        return cls(
            success=success,
            output_path=output_path,
            records_patched=records_patched,
            conflicts_resolved=conflicts_resolved,
            xedit_exit_code=xedit_exit_code,
            warnings=tuple(warnings) if warnings else tuple(),
            error=error,
        )


# =============================================================================
# PATCH STRATEGY INTERFACE (ABC)
# =============================================================================


class PatchStrategy(ABC):
    """Estrategia abstracta para resolución de conflictos.

    Cada estrategia implementa su lógica para determinar si puede manejar
    un conflicto específico y cómo generar un plan de parcheo adecuado.

    Methods:
        can_handle: Determina si la estrategia puede manejar el conflicto.
        create_plan: Crea un plan de parcheo detallado.
        get_priority: Retorna la prioridad de la estrategia (mayor = más prioritaria).
    """

    @abstractmethod
    async def can_handle(self, conflict: "RecordConflict") -> bool:
        """Determina si esta estrategia puede manejar el conflicto.

        Args:
            conflict: El conflicto de record a evaluar.

        Returns:
            True si la estrategia puede manejar el conflicto.
        """
        ...

    @abstractmethod
    async def create_plan(self, conflicts: list["RecordConflict"]) -> PatchPlan:
        """Crea un plan de parcheo detallado.

        Args:
            conflicts: Lista de conflictos a resolver.

        Returns:
            PatchPlan con los detalles de la operación de parcheo.

        Raises:
            ScriptGenerationError: Si no se puede generar el plan.
        """
        ...

    @abstractmethod
    def get_priority(self) -> int:
        """Retorna prioridad de la estrategia (mayor = más prioritaria).

        Returns:
            Entero representando la prioridad.
        """
        ...


# =============================================================================
# CONCRETE STRATEGIES
# =============================================================================


class CreateMergedPatch(PatchStrategy):
    """Estrategia para combinar leveled lists (LVLI, LVLN, LVSP).

    Esta estrategia es adecuada para conflictos de leveled lists donde
    múltiples mods modifican las mismas listas. Genera un patch que
    combina todas las entradas de forma que ningún mod sobrescriba a otro.

    Priority: 10 (baja - usada como fallback para leveled lists)
    """

    #: Record types que esta estrategia puede manejar
    HANDLED_TYPES: frozenset[str] = frozenset({"LVLI", "LVLN", "LVSP"})

    def __init__(self, output_dir: pathlib.Path | None = None) -> None:
        """Inicializa la estrategia.

        Args:
            output_dir: Directorio donde se generará el patch.
        """
        self._output_dir = output_dir or pathlib.Path(".")

    async def can_handle(self, conflict: "RecordConflict") -> bool:
        """Verifica si el conflicto es de tipo leveled list.

        Args:
            conflict: Conflicto a evaluar.

        Returns:
            True si record_type está en HANDLED_TYPES.
        """
        can_handle = conflict.record_type.upper() in self.HANDLED_TYPES
        logger.debug(
            "CreateMergedPatch.can_handle: record_type=%s, result=%s",
            conflict.record_type,
            can_handle,
        )
        return can_handle

    async def create_plan(self, conflicts: list["RecordConflict"]) -> PatchPlan:
        """Crea un plan para generar un merged patch de leveled lists.

        Args:
            conflicts: Lista de conflictos de leveled lists.

        Returns:
            PatchPlan con los detalles del merged patch.

        Raises:
            ScriptGenerationError: Si no hay conflictos válidos.
        """
        if not conflicts:
            raise ScriptGenerationError("Cannot create plan: no conflicts provided")

        # Filtrar solo conflictos de leveled lists
        valid_conflicts = [
            c for c in conflicts if c.record_type.upper() in self.HANDLED_TYPES
        ]

        if not valid_conflicts:
            raise ScriptGenerationError(
                "No leveled list conflicts found in provided list"
            )

        # Recopilar plugins y FormIDs únicos
        target_plugins: set[str] = set()
        form_ids: list[str] = []

        for conflict in valid_conflicts:
            target_plugins.add(conflict.winner)
            target_plugins.update(conflict.losers)
            form_ids.append(conflict.form_id)

        # Determinar si requiere HITL (múltiples winners diferentes)
        unique_winners = {c.winner for c in valid_conflicts}
        requires_hitl = len(unique_winners) > 3  # Más de 3 mods diferentes

        # Generar nombre del patch
        output_plugin = "SkyClaw_MergedPatch.esp"

        logger.info(
            "CreateMergedPatch plan created: %d conflicts, %d plugins, HITL=%s",
            len(valid_conflicts),
            len(target_plugins),
            requires_hitl,
        )

        return PatchPlan(
            strategy_type=PatchStrategyType.CREATE_MERGED_PATCH,
            target_plugins=sorted(target_plugins),
            output_plugin=output_plugin,
            form_ids=form_ids,
            estimated_records=len(valid_conflicts),
            requires_hitl=requires_hitl,
            script_path=None,  # Merged patch no requiere script Pascal
        )

    def get_priority(self) -> int:
        """Retorna prioridad de la estrategia.

        Returns:
            10 (baja prioridad - fallback para leveled lists).
        """
        return 10


class ExecuteXEditScript(PatchStrategy):
    """Estrategia para conflictos críticos vía scripts Pascal de xEdit.

    Esta estrategia maneja conflictos de alto riesgo (NPC_, QUST, SCPT, PERK, etc.)
    que requieren correcciones específicas mediante scripts Pascal ejecutados
    en xEdit headless.

    Priority: 20 (alta - usada para conflictos críticos)
    """

    #: Record types considerados críticos
    CRITICAL_TYPES: frozenset[str] = frozenset(
        {
            "NPC_",
            "QUST",
            "SCPT",
            "PERK",
            "SPEL",
            "MGEF",
            "FACT",
            "DIAL",
            "PACK",
        }
    )

    def __init__(
        self,
        scripts_dir: pathlib.Path | None = None,
        output_dir: pathlib.Path | None = None,
    ) -> None:
        """Inicializa la estrategia.

        Args:
            scripts_dir: Directorio donde están los scripts Pascal.
            output_dir: Directorio donde se generará el patch.
        """
        self._scripts_dir = scripts_dir or pathlib.Path(".")
        self._output_dir = output_dir or pathlib.Path(".")

    async def can_handle(self, conflict: "RecordConflict") -> bool:
        """Verifica si el conflicto es de severidad crítica.

        Args:
            conflict: Conflicto a evaluar.

        Returns:
            True si severity == "critical".
        """
        is_critical = conflict.severity == "critical"
        logger.debug(
            "ExecuteXEditScript.can_handle: record_type=%s, severity=%s, result=%s",
            conflict.record_type,
            conflict.severity,
            is_critical,
        )
        return is_critical

    async def create_plan(self, conflicts: list["RecordConflict"]) -> PatchPlan:
        """Crea un plan para ejecutar script xEdit de corrección.

        Args:
            conflicts: Lista de conflictos críticos.

        Returns:
            PatchPlan con los detalles del script a ejecutar.

        Raises:
            ScriptGenerationError: Si no hay conflictos críticos.
        """
        if not conflicts:
            raise ScriptGenerationError("Cannot create plan: no conflicts provided")

        # Filtrar solo conflictos críticos
        critical_conflicts = [c for c in conflicts if c.severity == "critical"]

        if not critical_conflicts:
            raise ScriptGenerationError("No critical conflicts found in provided list")

        # Recopilar plugins y FormIDs únicos
        target_plugins: set[str] = set()
        form_ids: list[str] = []

        for conflict in critical_conflicts:
            target_plugins.add(conflict.winner)
            target_plugins.update(conflict.losers)
            form_ids.append(conflict.form_id)

        # Todos los conflictos críticos requieren HITL para revisión
        requires_hitl = True

        # Determinar el script a usar basado en los tipos de records
        script_name = self._select_script_for_conflicts(critical_conflicts)
        script_path = self._scripts_dir / script_name

        # Generar nombre del patch
        output_plugin = "SkyClaw_CriticalPatch.esp"

        logger.info(
            "ExecuteXEditScript plan created: %d critical conflicts, "
            "%d plugins, script=%s, HITL=%s",
            len(critical_conflicts),
            len(target_plugins),
            script_name,
            requires_hitl,
        )

        return PatchPlan(
            strategy_type=PatchStrategyType.EXECUTE_XEDIT_SCRIPT,
            target_plugins=sorted(target_plugins),
            output_plugin=output_plugin,
            form_ids=form_ids,
            estimated_records=len(critical_conflicts),
            requires_hitl=requires_hitl,
            script_path=script_path,
        )

    def get_priority(self) -> int:
        """Retorna prioridad de la estrategia.

        Returns:
            20 (alta prioridad - conflictos críticos).
        """
        return 20

    def _select_script_for_conflicts(self, conflicts: list["RecordConflict"]) -> str:
        """Selecciona el script Pascal apropiado para los conflictos.

        Args:
            conflicts: Lista de conflictos críticos.

        Returns:
            Nombre del script Pascal a ejecutar.
        """
        # Analizar tipos de records presentes
        record_types = {c.record_type.upper() for c in conflicts}

        # NPC conflicts -> script específico
        if "NPC_" in record_types:
            return "fix_npc_conflicts.pas"

        # Quest conflicts -> script específico
        if "QUST" in record_types:
            return "fix_quest_conflicts.pas"

        # Script conflicts -> script específico
        if "SCPT" in record_types:
            return "fix_script_conflicts.pas"

        # Default: script genérico de conflictos
        return "fix_critical_conflicts.pas"


# =============================================================================
# PATCH ORCHESTRATOR
# =============================================================================


class PatchOrchestrator:
    """Orquestador principal de operaciones de parcheo.

    Coordina la selección y ejecución de estrategias de parcheo basándose
    en el tipo y severidad de los conflictos detectados. Implementa un
    protocolo transaccional con soporte para rollback.

    Attributes:
        xedit_runner: Runner para ejecutar xEdit en modo headless.
        snapshot_manager: Gestor de snapshots para backup.
        rollback_manager: Gestor de rollback para deshacer cambios.
        strategies: Lista de estrategias disponibles, ordenadas por prioridad.

    Usage:
        orchestrator = PatchOrchestrator(
            xedit_runner=runner,
            snapshot_manager=snapshots,
            rollback_manager=rollback,
        )
        result = await orchestrator.resolve(conflict_report)
    """

    def __init__(
        self,
        xedit_runner: "XEditRunner",
        snapshot_manager: "FileSnapshotManager",
        rollback_manager: "RollbackManager",
        strategies: list[PatchStrategy] | None = None,
    ) -> None:
        """Inicializa el orquestador de parcheo.

        Args:
            xedit_runner: Runner para ejecutar xEdit.
            snapshot_manager: Gestor de snapshots.
            rollback_manager: Gestor de rollback.
            strategies: Lista de estrategias personalizadas (opcional).
        """
        self._xedit_runner = xedit_runner
        self._snapshot_manager = snapshot_manager
        self._rollback_manager = rollback_manager
        self._strategies = strategies or self._default_strategies()

        # Ordenar estrategias por prioridad (mayor primero)
        self._strategies.sort(key=lambda s: s.get_priority(), reverse=True)

        logger.info(
            "PatchOrchestrator initialized with %d strategies: %s",
            len(self._strategies),
            [s.__class__.__name__ for s in self._strategies],
        )

    async def resolve(self, report: "ConflictReport") -> PatchResult:
        """Resuelve conflictos usando la estrategia óptima.

        Protocolo Transaccional:
        1. Seleccionar estrategia basada en gravedad/tipo
        2. Crear plan de parcheo
        3. Retornar plan (la ejecución se hace en supervisor.py)

        Args:
            report: Reporte de conflictos a resolver.

        Returns:
            PatchResult con el resultado de la operación.
        """
        logger.info(
            "Starting conflict resolution: %d total conflicts, %d critical",
            report.total_conflicts,
            report.critical_conflicts,
        )

        if report.total_conflicts == 0:
            return PatchResult(
                success=True,
                output_path=None,
                records_patched=0,
                conflicts_resolved=0,
                xedit_exit_code=0,
                warnings=["No conflicts to resolve"],
            )

        # Recolectar todos los conflictos del reporte
        all_conflicts: list["RecordConflict"] = []
        for pair in report.plugin_pairs:
            all_conflicts.extend(pair.conflicts)

        try:
            # Seleccionar la mejor estrategia para el conjunto de conflictos
            strategy = await self._select_best_strategy(all_conflicts)

            logger.info(
                "Selected strategy: %s (priority=%d)",
                strategy.__class__.__name__,
                strategy.get_priority(),
            )

            # Crear plan de parcheo
            plan = await strategy.create_plan(all_conflicts)

            logger.info(
                "Patch plan created: type=%s, targets=%d, records=%d, HITL=%s",
                plan.strategy_type.value,
                len(plan.target_plugins),
                plan.estimated_records,
                plan.requires_hitl,
            )

            # Retornar resultado exitoso con el plan
            # (La ejecución real se hace en supervisor.py)
            return PatchResult(
                success=True,
                output_path=pathlib.Path(plan.output_plugin),
                records_patched=plan.estimated_records,
                conflicts_resolved=len(all_conflicts),
                xedit_exit_code=0,
                warnings=(
                    ["Requires Human-in-the-Loop review"] if plan.requires_hitl else []
                ),
            )

        except StrategySelectionError as e:
            logger.error("Strategy selection failed: %s", e)
            return PatchResult(
                success=False,
                output_path=None,
                records_patched=0,
                conflicts_resolved=0,
                xedit_exit_code=-1,
                error=f"Strategy selection error: {e}",
            )
        except ScriptGenerationError as e:
            logger.error("Script generation failed: %s", e)
            return PatchResult(
                success=False,
                output_path=None,
                records_patched=0,
                conflicts_resolved=0,
                xedit_exit_code=-1,
                error=f"Script generation error: {e}",
            )
        except PatchingError as e:
            logger.error("Patching error: %s", e)
            return PatchResult(
                success=False,
                output_path=None,
                records_patched=0,
                conflicts_resolved=0,
                xedit_exit_code=-1,
                error=f"Patching error: {e}",
            )

    def select_strategy(self, conflict: "RecordConflict") -> PatchStrategy:
        """Selecciona la estrategia óptima para un conflicto.

        Itera sobre las estrategias disponibles (ordenadas por prioridad)
        y retorna la primera que pueda manejar el conflicto.

        Args:
            conflict: Conflicto a resolver.

        Returns:
            La estrategia seleccionada.

        Raises:
            StrategySelectionError: Si ninguna estrategia puede manejar el conflicto.
        """
        import asyncio

        for strategy in self._strategies:
            # can_handle es async, necesitamos ejecutarlo
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Si ya estamos en un loop async, crear tarea
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(
                            asyncio.run, strategy.can_handle(conflict)
                        )
                        can_handle = future.result()
                else:
                    can_handle = loop.run_until_complete(strategy.can_handle(conflict))

                if can_handle:
                    logger.debug(
                        "Strategy %s selected for conflict %s",
                        strategy.__class__.__name__,
                        conflict.form_id,
                    )
                    return strategy
            except Exception as e:
                logger.warning(
                    "Error checking strategy %s: %s",
                    strategy.__class__.__name__,
                    e,
                )
                continue

        raise StrategySelectionError(
            f"No strategy found for conflict: form_id={conflict.form_id}, "
            f"record_type={conflict.record_type}, severity={conflict.severity}"
        )

    async def _select_best_strategy(
        self, conflicts: list["RecordConflict"]
    ) -> PatchStrategy:
        """Selecciona la mejor estrategia para un conjunto de conflictos.

        Evalúa todos los conflictos y selecciona la estrategia con mayor
        prioridad que pueda manejar la mayoría de los conflictos.

        Args:
            conflicts: Lista de conflictos a evaluar.

        Returns:
            La estrategia seleccionada.

        Raises:
            StrategySelectionError: Si no hay conflictos o ninguna estrategia aplica.
        """
        if not conflicts:
            raise StrategySelectionError("No conflicts provided")

        # Contar cuántos conflictos puede manejar cada estrategia
        strategy_scores: dict[str, tuple[PatchStrategy, int]] = {}

        for strategy in self._strategies:
            score = 0
            for conflict in conflicts:
                if await strategy.can_handle(conflict):
                    score += 1
            strategy_scores[strategy.__class__.__name__] = (strategy, score)

        # Log de scores
        for name, (_, score) in strategy_scores.items():
            logger.debug("Strategy %s score: %d/%d", name, score, len(conflicts))

        # Seleccionar la estrategia con mayor score (ya ordenadas por prioridad)
        best_strategy: PatchStrategy | None = None
        best_score = 0

        for strategy in self._strategies:
            score = strategy_scores[strategy.__class__.__name__][1]
            if score > best_score:
                best_score = score
                best_strategy = strategy

        if best_strategy is None or best_score == 0:
            raise StrategySelectionError(
                f"No strategy can handle any of the {len(conflicts)} conflicts"
            )

        return best_strategy

    def _default_strategies(self) -> list[PatchStrategy]:
        """Retorna la lista de estrategias por defecto.

        Returns:
            Lista con ExecuteXEditScript y CreateMergedPatch.
        """
        return [ExecuteXEditScript(), CreateMergedPatch()]

    @property
    def strategies(self) -> list[PatchStrategy]:
        """Retorna la lista de estrategias disponibles.

        Returns:
            Lista de estrategias ordenadas por prioridad.
        """
        return self._strategies.copy()

    def add_strategy(self, strategy: PatchStrategy) -> None:
        """Agrega una nueva estrategia al orquestador.

        La estrategia se inserta en la posición correcta según su prioridad.

        Args:
            strategy: Estrategia a agregar.
        """
        self._strategies.append(strategy)
        self._strategies.sort(key=lambda s: s.get_priority(), reverse=True)
        logger.info(
            "Added strategy %s with priority %d",
            strategy.__class__.__name__,
            strategy.get_priority(),
        )

    def remove_strategy(self, strategy_type: type[PatchStrategy]) -> bool:
        """Remueve una estrategia del orquestador.

        Args:
            strategy_type: Tipo de estrategia a remover.

        Returns:
            True si se removió, False si no existía.
        """
        for i, strategy in enumerate(self._strategies):
            if isinstance(strategy, strategy_type):
                self._strategies.pop(i)
                logger.info(
                    "Removed strategy %s",
                    strategy_type.__name__,
                )
                return True
        return False
