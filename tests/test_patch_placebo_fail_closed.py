"""Tests anti-placebo del pipeline de parcheo xEdit (Etapa 0 + Fase 1).

Historia: los 3 scripts .pas de conflictos críticos referenciados por
ExecuteXEditScript nunca existieron, y el pipeline degradaba EN SILENCIO al
template genérico — cuyo cuerpo de Process es un placeholder sin lógica — y
reportaba éxito con un .esp vacío. Estos tests anclan el cierre fail-closed
de toda esa cadena:

1. ``can_handle`` exige que el script exista (el routing no reclama lo que
   no puede ejecutar).
2. ``create_plan`` rechaza planes cuyo script no está en disco.
3. El generador rechaza EXECUTE_XEDIT_SCRIPT sin script real (no más template).
4. El service usa el plan REAL del orquestador (script_path/form_ids llegan
   al runner, no una reconstrucción que los pierde).
5. La rama AI_ASSISTED: sin LLM falla closed con mensaje accionable; con LLM
   produce recomendaciones advisory sin tocar disco.
"""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.xedit_service import XEditPipelineService
from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.patch_orchestrator import (
    ExecuteXEditScript,
    PatchPlan,
    PatchResult,
    PatchStrategyType,
    ScriptGenerationError,
)
from sky_claw.local.xedit.runner import ScriptExecutionResult, ScriptGenerator, XEditScriptError
from sky_claw.local.xedit.script_staging import BUNDLED_SCRIPTS_DIR

if TYPE_CHECKING:
    pass


# =============================================================================
# Fixtures (mismo patrón que test_xedit_service.py)
# =============================================================================


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_path / "test_locks.db",
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr  # type: ignore[misc]
    await mgr.close()


@pytest.fixture
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    d = tmp_path / "snapshots"
    d.mkdir()
    return FileSnapshotManager(snapshot_dir=d)


@pytest.fixture
def mock_journal() -> AsyncMock:
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=1)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    return journal


@pytest.fixture
def mock_path_resolver(tmp_path: pathlib.Path) -> MagicMock:
    resolver = MagicMock()
    xedit_exe = tmp_path / "xEdit.exe"
    xedit_exe.touch()
    game_path = tmp_path / "Skyrim"
    game_path.mkdir()
    resolver.get_xedit_path = MagicMock(return_value=xedit_exe)
    resolver.get_skyrim_path = MagicMock(return_value=game_path)
    return resolver


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def target_plugin(tmp_path: pathlib.Path) -> pathlib.Path:
    plugin = tmp_path / "TestMod.esp"
    plugin.write_bytes(b"TES4")
    return plugin


def _critico(record_type: str = "NPC_") -> RecordConflict:
    return RecordConflict(
        form_id="00012EB7",
        editor_id="BanditThief",
        record_type=record_type,
        winner="A.esp",
        losers=["B.esp"],
        severity="critical",
    )


def _reporte_critico(record_type: str = "NPC_") -> ConflictReport:
    return ConflictReport(
        total_conflicts=1,
        critical_conflicts=1,
        plugin_pairs=[PluginConflictPair(plugin_a="A.esp", plugin_b="B.esp", conflicts=[_critico(record_type)])],
    )


def _service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    llm=None,
) -> XEditPipelineService:
    return XEditPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_resolver=mock_path_resolver,
        event_bus=mock_event_bus,
        llm=llm,
    )


# =============================================================================
# 1-2. ExecuteXEditScript: existencia del script como parte del contrato
# =============================================================================


async def test_can_handle_rechaza_critico_sin_script(tmp_path: pathlib.Path) -> None:
    strategy = ExecuteXEditScript(scripts_dir=tmp_path)  # dir vacío

    assert await strategy.can_handle(_critico()) is False


async def test_can_handle_acepta_critico_con_script_presente(tmp_path: pathlib.Path) -> None:
    (tmp_path / "fix_npc_conflicts.pas").write_text("// stub", encoding="utf-8")
    strategy = ExecuteXEditScript(scripts_dir=tmp_path)

    assert await strategy.can_handle(_critico()) is True


def test_scripts_dir_default_es_el_bundle() -> None:
    """El default anterior era Path('.') — cwd, donde ningún script vive."""
    assert ExecuteXEditScript()._scripts_dir == BUNDLED_SCRIPTS_DIR


async def test_create_plan_falla_closed_si_el_script_del_set_no_existe(tmp_path: pathlib.Path) -> None:
    """Defensa en profundidad: un set mixto puede elegir OTRO script que el
    validado por conflicto individual en can_handle."""
    # Solo existe el de NPC; el set mixto NPC_+QUST selecciona fix_npc_conflicts,
    # pero un set solo-QUST selecciona fix_quest_conflicts (inexistente).
    (tmp_path / "fix_npc_conflicts.pas").write_text("// stub", encoding="utf-8")
    strategy = ExecuteXEditScript(scripts_dir=tmp_path)

    with pytest.raises(ScriptGenerationError, match="fix_quest_conflicts.pas"):
        await strategy.create_plan([_critico("QUST")])


# =============================================================================
# 3. El generador ya no produce el template placebo
# =============================================================================


def test_generate_script_from_plan_rechaza_execute_xedit_script() -> None:
    plan = PatchPlan(
        strategy_type=PatchStrategyType.EXECUTE_XEDIT_SCRIPT,
        target_plugins=["A.esp"],
        output_plugin="SkyClaw_CriticalPatch.esp",
        form_ids=["00012EB7"],
        estimated_records=1,
        requires_hitl=True,
        script_path=None,
    )

    with pytest.raises(XEditScriptError, match="placebo"):
        ScriptGenerator.generate_script_from_plan(plan)


# =============================================================================
# 4. El service enruta el plan REAL del orquestador al runner
# =============================================================================


async def test_execute_patch_pasa_el_plan_del_orquestador_al_runner(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    target_plugin: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """La reconstrucción manual anterior descartaba script_path y form_ids."""
    script = tmp_path / "fix_npc_conflicts.pas"
    script.write_text("// stub", encoding="utf-8")
    plan = PatchPlan(
        strategy_type=PatchStrategyType.EXECUTE_XEDIT_SCRIPT,
        target_plugins=["A.esp", "B.esp"],
        output_plugin="SkyClaw_CriticalPatch.esp",
        form_ids=["00012EB7"],
        estimated_records=1,
        requires_hitl=True,
        script_path=script,
    )
    resultado_plan = PatchResult(
        success=True,
        output_path=pathlib.Path(plan.output_plugin),
        records_patched=1,
        conflicts_resolved=1,
        xedit_exit_code=0,
        strategy_type=PatchStrategyType.EXECUTE_XEDIT_SCRIPT,
        plan=plan,
    )
    mock_orchestrator = AsyncMock()
    mock_orchestrator.resolve = AsyncMock(return_value=resultado_plan)
    mock_runner = AsyncMock()
    mock_runner.execute_patch = AsyncMock(
        return_value=ScriptExecutionResult(
            success=True,
            exit_code=0,
            stdout="",
            stderr="",
            records_processed=1,
        )
    )

    service = _service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)
    service._xedit_runner = mock_runner

    with patch.object(service, "_ensure_patch_orchestrator", return_value=mock_orchestrator):
        out = await service.execute_patch(_reporte_critico(), target_plugin)

    assert out["success"] is True
    mock_runner.execute_patch.assert_awaited_once()
    plan_recibido = mock_runner.execute_patch.call_args.args[0]
    assert plan_recibido is plan  # el MISMO plan, con script_path y form_ids
    assert "plan" not in out  # detalle interno: no viaja en el contrato dict


# =============================================================================
# 5. Rama AI_ASSISTED del service
# =============================================================================


async def test_critico_sin_script_y_sin_llm_falla_closed_end_to_end(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    target_plugin: pathlib.Path,
) -> None:
    """Orquestador REAL (bundle sin fix_*.pas) + service sin LLM: el viejo
    éxito placebo ahora es un error accionable, sin .esp y con rollback."""
    service = _service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus)

    out = await service.execute_patch(_reporte_critico(), target_plugin)

    assert out["success"] is False
    assert "sin LLM configurado" in out["error"]
    assert out["output_path"] is None
    mock_journal.commit_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)


async def test_critico_con_llm_produce_recomendaciones_advisory(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    target_plugin: pathlib.Path,
) -> None:
    """Con LLM configurado, la rama AI_ASSISTED devuelve éxito advisory:
    recomendaciones en warnings, output_path=None, nada mutado."""

    async def llm(system: str, user: str) -> str:
        return json.dumps(
            {
                "form_id": "00012EB7",
                "record_type": "NPC_",
                "severity": "needs_review",
                "summary": "Forwardear las factions de B.esp al patch.",
                "subrecords": [],
                "confidence": 0.7,
            }
        )

    bytes_originales = target_plugin.read_bytes()
    service = _service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus, llm=llm)

    out = await service.execute_patch(_reporte_critico(), target_plugin)

    assert out["success"] is True
    assert out["output_path"] is None  # advisory: no se generó .esp
    assert out["strategy_type"] == "ai_assisted"
    assert any("AI Advice [NPC_ 00012eb7]" in w for w in out["warnings"])
    assert any("Forwardear las factions" in w for w in out["warnings"])
    assert target_plugin.read_bytes() == bytes_originales  # nada mutado
    mock_journal.commit_transaction.assert_awaited_once_with(1)


async def test_advisor_degrada_a_manual_only_si_el_llm_revienta(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_path_resolver: MagicMock,
    mock_event_bus: AsyncMock,
    target_plugin: pathlib.Path,
) -> None:
    """Un LLM que lanza no tumba el pipeline: recomendación manual_only."""

    async def llm_roto(system: str, user: str) -> str:
        raise RuntimeError("proveedor caído")

    service = _service(lock_manager, snapshot_manager, mock_journal, mock_path_resolver, mock_event_bus, llm=llm_roto)

    out = await service.execute_patch(_reporte_critico(), target_plugin)

    assert out["success"] is True  # el advisor produjo (una degradada)
    assert any("manual_only" in w for w in out["warnings"])
