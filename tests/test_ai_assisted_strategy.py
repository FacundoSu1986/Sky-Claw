"""Tests de la strategy AIAssistedPatch y su enrutado en el orquestador (Fase 1).

El contrato clave: el catch-all (priority=1) SOLO gana cuando ninguna strategy
con script real puede reclamar el conflicto — con el .pas presente, gana
ExecuteXEditScript (priority=20); sin él, el conflicto crítico cae al advisor
en vez de a "no strategy" o al viejo template placebo.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.local.xedit.ai_assisted_strategy import AIAssistedPatch
from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.patch_orchestrator import (
    ExecuteXEditScript,
    PatchOrchestrator,
    PatchStrategyType,
    ScriptGenerationError,
)


def _critico(record_type: str = "NPC_", form_id: str = "00012EB7") -> RecordConflict:
    return RecordConflict(
        form_id=form_id,
        editor_id="TestRecord",
        record_type=record_type,
        winner="A.esp",
        losers=["B.esp"],
        severity="critical",
    )


def _warning() -> RecordConflict:
    return RecordConflict(
        form_id="00099999",
        editor_id="TestWeap",
        record_type="WEAP",
        winner="A.esp",
        losers=["B.esp"],
        severity="warning",
    )


def _reporte(conflictos: list[RecordConflict]) -> ConflictReport:
    criticos = sum(1 for c in conflictos if c.severity == "critical")
    return ConflictReport(
        total_conflicts=len(conflictos),
        critical_conflicts=criticos,
        plugin_pairs=[PluginConflictPair(plugin_a="A.esp", plugin_b="B.esp", conflicts=conflictos)],
    )


def _orquestador(**kwargs) -> PatchOrchestrator:
    return PatchOrchestrator(
        xedit_runner=MagicMock(),
        snapshot_manager=MagicMock(),
        rollback_manager=MagicMock(),
        **kwargs,
    )


# =============================================================================
# La strategy en sí
# =============================================================================


async def test_can_handle_acepta_criticos_y_rechaza_warnings() -> None:
    strategy = AIAssistedPatch()

    assert await strategy.can_handle(_critico()) is True
    assert await strategy.can_handle(_warning()) is False


async def test_create_plan_es_advisory_puro() -> None:
    plan = await AIAssistedPatch().create_plan([_critico(), _warning(), _critico(form_id="0000AAAA")])

    assert plan.strategy_type is PatchStrategyType.AI_ASSISTED
    assert plan.output_plugin == ""  # no se genera .esp
    assert plan.script_path is None  # no hay script Pascal
    assert plan.requires_hitl is True  # el operador SIEMPRE decide
    assert plan.form_ids == ["00012EB7", "0000AAAA"]  # solo los críticos
    assert plan.estimated_records == 2


async def test_create_plan_sin_criticos_lanza() -> None:
    with pytest.raises(ScriptGenerationError):
        await AIAssistedPatch().create_plan([_warning()])


def test_priority_es_la_mas_baja() -> None:
    assert AIAssistedPatch().get_priority() == 1
    assert AIAssistedPatch().get_priority() < ExecuteXEditScript().get_priority()


# =============================================================================
# Registro y enrutado en el orquestador
# =============================================================================


def test_default_strategies_incluye_el_catch_all() -> None:
    nombres = [s.__class__.__name__ for s in _orquestador().strategies]

    assert "AIAssistedPatch" in nombres
    assert "ExecuteXEditScript" in nombres
    assert "DelegateToBashedPatch" in nombres
    # Orden por prioridad: el catch-all queda último.
    assert nombres[-1] == "AIAssistedPatch"


async def test_critico_sin_script_cae_al_advisor() -> None:
    """El caso Fase 1: no existe fix_npc_conflicts.pas en el bundle."""
    resultado = await _orquestador().resolve(_reporte([_critico()]))

    assert resultado.success is True
    assert resultado.strategy_type is PatchStrategyType.AI_ASSISTED
    assert resultado.output_path is None  # advisory: sin .esp
    assert resultado.plan is not None
    assert resultado.plan.requires_hitl is True


async def test_critico_con_script_real_gana_execute_xedit_script(tmp_path: pathlib.Path) -> None:
    """Con el .pas en disco, la strategy de scripts (priority=20) le gana al catch-all."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "fix_npc_conflicts.pas").write_text("// stub", encoding="utf-8")
    orquestador = _orquestador(
        strategies=[ExecuteXEditScript(scripts_dir=scripts), AIAssistedPatch()],
    )

    resultado = await orquestador.resolve(_reporte([_critico()]))

    assert resultado.success is True
    assert resultado.strategy_type is PatchStrategyType.EXECUTE_XEDIT_SCRIPT
    assert resultado.plan is not None
    assert resultado.plan.script_path == scripts / "fix_npc_conflicts.pas"


async def test_leveled_lists_siguen_yendo_al_bashed_patch() -> None:
    """El catch-all no roba los conflictos que ya tenían dueño (ADR 0001)."""
    lvli = RecordConflict(
        form_id="00011111",
        editor_id="TestLVLI",
        record_type="LVLI",
        winner="A.esp",
        losers=["B.esp"],
        severity="warning",
    )

    resultado = await _orquestador().resolve(_reporte([lvli]))

    assert resultado.strategy_type is PatchStrategyType.DELEGATE_BASHED_PATCH
