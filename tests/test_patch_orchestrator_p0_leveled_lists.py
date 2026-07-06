"""Tests de contención del P0 de leveled lists (T-01 de TECHNICAL_REVIEW_TASKS.md).

El script de merge de leveled lists (estático y template generado) copia la
PRIMERA versión de cada FormID que itera y descarta los overrides posteriores,
por lo que el "merged patch" puede revertir los cambios de la modlist
(TECHNICAL_REVIEW.md §4.1). Hasta que T-04 implemente la semántica correcta:

1. ``CreateMergedPatch`` no debe estar en las estrategias por defecto.
2. Un reporte de solo leveled lists debe fallar explícitamente recomendando
   el Bashed Patch de Wrye Bash (la herramienta correcta ya integrada).
3. La generación de scripts debe rechazar planes ``CREATE_MERGED_PATCH``
   aunque alguien registre la estrategia manualmente (defensa en profundidad).
"""

from unittest.mock import MagicMock

import pytest

from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.patch_orchestrator import (
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

    async def test_reporte_solo_lvli_falla_recomendando_bashed_patch(
        self, orquestador_por_defecto: PatchOrchestrator
    ) -> None:
        """Un reporte de solo leveled lists falla explícito, sin plan silencioso.

        El error debe ser accionable: apuntar al Bashed Patch (Wrye Bash),
        no un genérico "no strategy found".
        """
        resultado = await orquestador_por_defecto.resolve(_reporte_solo_leveled_lists())

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
