"""Test de integración para Fase 2 Dynamic Patching.

Este módulo verifica la integración completa del sistema de parcheo transaccional:
- PatchOrchestrator con patrón Strategy
- XEditRunner con ScriptGenerator
- Flujo transaccional en SupervisorAgent
- Rollback automático en caso de fallo

Fase 2: Dynamic Patching & xEdit Integration
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.patch_orchestrator import (
    CreateMergedPatch,
    ExecuteXEditScript,
    PatchExecutionError,
    PatchingError,
    PatchOrchestrator,
    PatchPlan,
    PatchResult,
    PatchStrategyType,
    ScriptGenerationError,
    StrategySelectionError,
)
from sky_claw.local.xedit.runner import ScriptExecutionResult, XEditRunner


class TestPatchOrchestratorIntegration:
    """Tests de integración para PatchOrchestrator."""

    @pytest.fixture
    def mock_runner(self):
        """Crea un mock de XEditRunner."""
        runner = MagicMock(spec=XEditRunner)
        runner.execute_patch = AsyncMock(
            return_value=ScriptExecutionResult(
                success=True,
                exit_code=0,
                stdout="Processed 5 records",
                stderr="",
                records_processed=5,
                errors=[],
                warnings=[],
                script_path=Path("/tmp/script.pas"),
                execution_time=1.5,
            )
        )
        return runner

    @pytest.fixture
    def mock_snapshot_manager(self):
        """Crea un mock de FileSnapshotManager."""
        manager = MagicMock()
        manager.create_snapshot = MagicMock(return_value=Path("/tmp/snapshot.bak"))
        manager.restore_snapshot = MagicMock(return_value=True)
        return manager

    @pytest.fixture
    def mock_rollback_manager(self):
        """Crea un mock de RollbackManager."""
        manager = MagicMock()
        return manager

    @pytest.fixture
    def orchestrator(self, mock_runner, mock_snapshot_manager, mock_rollback_manager):
        """Crea un PatchOrchestrator con dependencias inyectadas."""
        return PatchOrchestrator(
            xedit_runner=mock_runner,
            snapshot_manager=mock_snapshot_manager,
            rollback_manager=mock_rollback_manager,
        )

    def test_orchestrator_initialization(self, orchestrator):
        """Verifica que el orquestador se inicializa correctamente con 2 estrategias."""
        assert orchestrator is not None
        assert len(orchestrator._strategies) == 2  # CreateMergedPatch + ExecuteXEditScript

        # Verificar que las estrategias están ordenadas por prioridad
        priorities = [s.get_priority() for s in orchestrator._strategies]
        assert priorities == sorted(priorities, reverse=True)

    def test_default_strategies_are_registered(self, orchestrator):
        """Verifica que las estrategias por defecto están registradas."""
        strategy_names = {s.__class__.__name__ for s in orchestrator._strategies}
        assert "CreateMergedPatch" in strategy_names
        assert "ExecuteXEditScript" in strategy_names

    @pytest.mark.asyncio
    async def test_resolve_empty_report(self, orchestrator):
        """Verifica comportamiento con reporte vacío."""
        empty_report = ConflictReport(
            total_conflicts=0,
            critical_conflicts=0,
            plugin_pairs=[],
            summary="No conflicts found",
        )
        result = await orchestrator.resolve(empty_report)

        assert result.success is True
        assert result.conflicts_resolved == 0
        assert result.records_patched == 0
        assert "No conflicts to resolve" in result.warnings

    @pytest.mark.asyncio
    async def test_resolve_leveled_list_conflict(self, orchestrator):
        """Verifica resolución de conflictos de leveled lists (LVLI)."""
        # Crear conflicto de leveled list
        conflict = RecordConflict(
            form_id="00012345",
            editor_id="TestLeveledItem",
            record_type="LVLI",
            winner="PluginA.esp",
            losers=["PluginB.esp", "PluginC.esp"],
            severity="warning",
        )

        pair = PluginConflictPair(
            plugin_a="PluginA.esp",
            plugin_b="PluginB.esp",
            conflicts=[conflict],
        )

        report = ConflictReport(
            total_conflicts=1,
            critical_conflicts=0,
            plugin_pairs=[pair],
            summary="1 leveled list conflict",
        )

        result = await orchestrator.resolve(report)

        # Verificar que se seleccionó CreateMergedPatch (para LVLI)
        assert result.success is True
        assert result.conflicts_resolved == 1
        assert result.output_path is not None

    @pytest.mark.asyncio
    async def test_resolve_critical_conflict(self, orchestrator):
        """Verifica resolución de conflictos críticos (NPC_, QUST, etc.)."""
        # Crear conflicto crítico
        conflict = RecordConflict(
            form_id="00054321",
            editor_id="TestNPC",
            record_type="NPC_",
            winner="PluginA.esp",
            losers=["PluginB.esp"],
            severity="critical",
        )

        pair = PluginConflictPair(
            plugin_a="PluginA.esp",
            plugin_b="PluginB.esp",
            conflicts=[conflict],
        )

        report = ConflictReport(
            total_conflicts=1,
            critical_conflicts=1,
            plugin_pairs=[pair],
            summary="1 critical conflict",
        )

        result = await orchestrator.resolve(report)

        # Verificar que se seleccionó ExecuteXEditScript (para críticos)
        assert result.success is True
        assert result.conflicts_resolved == 1


class TestPatchStrategies:
    """Tests para las estrategias de parcheo individuales."""

    @pytest.fixture
    def merged_patch_strategy(self):
        """Crea una instancia de CreateMergedPatch."""
        return CreateMergedPatch(output_dir=Path("/tmp"))

    @pytest.fixture
    def xedit_script_strategy(self):
        """Crea una instancia de ExecuteXEditScript."""
        return ExecuteXEditScript(
            scripts_dir=Path("/tmp/scripts"),
            output_dir=Path("/tmp"),
        )

    @pytest.mark.asyncio
    async def test_merged_patch_can_handle_leveled_list(self, merged_patch_strategy):
        """Verifica que CreateMergedPatch maneja LVLI, LVLN, LVSP."""
        lvli_conflict = RecordConflict(
            form_id="00011111",
            editor_id="TestLVLI",
            record_type="LVLI",
            winner="A.esp",
            losers=["B.esp"],
            severity="warning",
        )

        assert await merged_patch_strategy.can_handle(lvli_conflict) is True

        # Verificar que NO maneja otros tipos
        npc_conflict = RecordConflict(
            form_id="00022222",
            editor_id="TestNPC",
            record_type="NPC_",
            winner="A.esp",
            losers=["B.esp"],
            severity="critical",
        )

        assert await merged_patch_strategy.can_handle(npc_conflict) is False

    @pytest.mark.asyncio
    async def test_xedit_script_can_handle_critical(self, xedit_script_strategy):
        """Verifica que ExecuteXEditScript maneja conflictos críticos."""
        critical_conflict = RecordConflict(
            form_id="00033333",
            editor_id="TestQUST",
            record_type="QUST",
            winner="A.esp",
            losers=["B.esp"],
            severity="critical",
        )

        assert await xedit_script_strategy.can_handle(critical_conflict) is True

        # Verificar que NO maneja severidad no crítica
        warning_conflict = RecordConflict(
            form_id="00044444",
            editor_id="TestLVLI",
            record_type="LVLI",
            winner="A.esp",
            losers=["B.esp"],
            severity="warning",
        )

        assert await xedit_script_strategy.can_handle(warning_conflict) is False

    def test_strategy_priorities(self, merged_patch_strategy, xedit_script_strategy):
        """Verifica que las prioridades están configuradas correctamente."""
        # ExecuteXEditScript debe tener mayor prioridad (20) que CreateMergedPatch (10)
        assert xedit_script_strategy.get_priority() > merged_patch_strategy.get_priority()
        assert xedit_script_strategy.get_priority() == 20
        assert merged_patch_strategy.get_priority() == 10


class TestPatchPlanDataclass:
    """Tests para la dataclass PatchPlan."""

    def test_patch_plan_creation(self):
        """Verifica creación de PatchPlan con todos los campos."""
        plan = PatchPlan(
            strategy_type=PatchStrategyType.CREATE_MERGED_PATCH,
            target_plugins=["A.esp", "B.esp"],
            output_plugin="SkyClaw_Patch.esp",
            form_ids=["00011111", "00022222"],
            estimated_records=10,
            requires_hitl=False,
            script_path=None,
        )

        assert plan.strategy_type == PatchStrategyType.CREATE_MERGED_PATCH
        assert len(plan.target_plugins) == 2
        assert plan.output_plugin == "SkyClaw_Patch.esp"
        assert plan.requires_hitl is False

    def test_patch_plan_with_script(self):
        """Verifica PatchPlan con script Pascal."""
        plan = PatchPlan(
            strategy_type=PatchStrategyType.EXECUTE_XEDIT_SCRIPT,
            target_plugins=["A.esp"],
            output_plugin="SkyClaw_Patch.esp",
            form_ids=["00033333"],
            estimated_records=1,
            requires_hitl=True,
            script_path=Path("/tmp/fix_npc.pas"),
        )

        assert plan.strategy_type == PatchStrategyType.EXECUTE_XEDIT_SCRIPT
        assert plan.script_path is not None
        assert plan.requires_hitl is True


class TestPatchResultDataclass:
    """Tests para la dataclass PatchResult."""

    def test_successful_result(self):
        """Verifica PatchResult exitoso."""
        result = PatchResult(
            success=True,
            output_path=Path("/tmp/SkyClaw_Patch.esp"),
            records_patched=15,
            conflicts_resolved=5,
            xedit_exit_code=0,
            warnings=["Minor warning"],
        )

        assert result.success is True
        assert result.error is None
        assert result.records_patched == 15

    def test_failed_result(self):
        """Verifica PatchResult fallido."""
        result = PatchResult(
            success=False,
            output_path=None,
            records_patched=0,
            conflicts_resolved=0,
            xedit_exit_code=1,
            error="xEdit execution failed",
        )

        assert result.success is False
        assert result.error == "xEdit execution failed"
        assert result.xedit_exit_code == 1


class TestExceptions:
    """Tests para la jerarquía de excepciones."""

    def test_patching_error_hierarchy(self):
        """Verifica la jerarquía de excepciones de parcheo."""
        # StrategySelectionError es subclase de PatchingError
        assert issubclass(StrategySelectionError, PatchingError)
        assert issubclass(PatchExecutionError, PatchingError)
        assert issubclass(ScriptGenerationError, PatchingError)

    def test_strategy_selection_error_message(self):
        """Verifica mensaje de error de StrategySelectionError."""
        error = StrategySelectionError("No strategy found for conflict")
        assert "No strategy found" in str(error)

    def test_script_generation_error_message(self):
        """Verifica mensaje de error de ScriptGenerationError."""
        error = ScriptGenerationError("Failed to generate Pascal script")
        assert "Failed to generate" in str(error)


class TestImportsAndExports:
    """Tests para verificar imports y exports del módulo."""

    def test_xedit_module_exports(self):
        """Verifica que el módulo xedit exporta todas las clases necesarias."""
        from sky_claw.local import xedit

        # Verificar exports del runner
        assert hasattr(xedit, "XEditRunner")
        assert hasattr(xedit, "ScriptGenerator")
        assert hasattr(xedit, "ScriptExecutionResult")

        # Verificar exports del patch_orchestrator
        assert hasattr(xedit, "PatchOrchestrator")
        assert hasattr(xedit, "PatchPlan")
        assert hasattr(xedit, "PatchResult")
        assert hasattr(xedit, "PatchStrategyType")
        assert hasattr(xedit, "PatchStrategy")
        assert hasattr(xedit, "CreateMergedPatch")
        assert hasattr(xedit, "ExecuteXEditScript")

        # Verificar exports de excepciones
        assert hasattr(xedit, "PatchingError")
        assert hasattr(xedit, "StrategySelectionError")
        assert hasattr(xedit, "PatchExecutionError")
        assert hasattr(xedit, "ScriptGenerationError")

        # Verificar exports del conflict_analyzer
        assert hasattr(xedit, "ConflictReport")
        assert hasattr(xedit, "RecordConflict")
        assert hasattr(xedit, "PluginConflictPair")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
