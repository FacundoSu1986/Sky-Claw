"""Tests de contención del P0 de leveled lists (T-01/T-04, ADR 0001).

Historia: el merge propio copiaba la primera versión de cada FormID (revertía
overrides — el P0 original). El hotfix T-02 lo convirtió en forward del
ganador, pero sigue sin implementar un merge real de entradas (Relev/Delev),
así que queda deshabilitado permanentemente y las leveled lists se delegan al
Bashed Patch de Wrye Bash (ADR 0001, implementado en T-04). Estos tests anclan:

1. ``CreateMergedPatch`` no está en las estrategias por defecto.
2. Sin la estrategia de delegación registrada, un reporte de solo leveled
   lists falla explícito recomendando el Bashed Patch (mensaje accionable).
3. La generación de scripts rechaza planes ``CREATE_MERGED_PATCH`` aunque
   alguien registre la estrategia manualmente (defensa en profundidad).
"""

from unittest.mock import MagicMock

import pytest

from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.patch_orchestrator import (
    ExecuteXEditScript,
    PatchOrchestrator,
    PatchPlan,
    PatchStrategyType,
)
from sky_claw.local.xedit.runner import ScriptGenerator, XEditScriptError


def _reporte_solo_leveled_lists() -> ConflictReport:
    """Reporte con un único conflicto LVLI (no crítico)."""
    conflicto = RecordConflict(
        form_id="00012345",
        editor_id="LItemBanditSword",
        record_type="LVLI",
        winner="OverhaulB.esp",
        losers=["OverhaulA.esp"],
        severity="warning",
    )
    par = PluginConflictPair(
        plugin_a="OverhaulA.esp",
        plugin_b="OverhaulB.esp",
        conflicts=[conflicto],
    )
    return ConflictReport(
        total_conflicts=1,
        critical_conflicts=0,
        plugin_pairs=[par],
        summary="1 conflicto de leveled list",
    )


@pytest.fixture
def orquestador_por_defecto() -> PatchOrchestrator:
    """PatchOrchestrator con las estrategias por defecto."""
    return PatchOrchestrator(
        xedit_runner=MagicMock(),
        snapshot_manager=MagicMock(),
        rollback_manager=MagicMock(),
    )


class TestContencionP0LeveledLists:
    """T-01: la estrategia buggy no debe ser alcanzable por ningún Ritual."""

    def test_estrategias_por_defecto_excluyen_create_merged_patch(
        self, orquestador_por_defecto: PatchOrchestrator
    ) -> None:
        """CreateMergedPatch no está registrada por defecto (P0 §4.1)."""
        nombres = {s.__class__.__name__ for s in orquestador_por_defecto.strategies}
        assert "CreateMergedPatch" not in nombres
        assert "ExecuteXEditScript" in nombres

    async def test_sin_delegacion_lvli_falla_recomendando_bashed_patch(self) -> None:
        """Sin DelegateToBashedPatch registrada, el error sigue siendo accionable.

        Cubre listas de estrategias custom: debe apuntar al Bashed Patch
        (Wrye Bash), no un genérico "no strategy found".
        """
        orquestador = PatchOrchestrator(
            xedit_runner=MagicMock(),
            snapshot_manager=MagicMock(),
            rollback_manager=MagicMock(),
            strategies=[ExecuteXEditScript()],
        )

        resultado = await orquestador.resolve(_reporte_solo_leveled_lists())

        assert resultado.success is False
        assert resultado.error is not None
        assert "Bashed Patch" in resultado.error

    def test_generate_script_from_plan_rechaza_create_merged_patch(self) -> None:
        """Defensa en profundidad: la generación de scripts bloquea el plan buggy."""
        plan = PatchPlan(
            strategy_type=PatchStrategyType.CREATE_MERGED_PATCH,
            target_plugins=["OverhaulA.esp", "OverhaulB.esp"],
            output_plugin="SkyClaw_MergedPatch.esp",
            form_ids=["00012345"],
            estimated_records=1,
            requires_hitl=False,
        )

        with pytest.raises(XEditScriptError, match="deshabilitad"):
            ScriptGenerator.generate_script_from_plan(plan)
