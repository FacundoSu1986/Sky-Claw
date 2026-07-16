"""Delegación de leveled lists al Bashed Patch (T-04, ADR 0001).

Implementa la decisión del ADR: los conflictos LVLI/LVLN/LVSP no generan un
script xEdit propio — producen un plan ``DELEGATE_BASHED_PATCH`` que la capa
de servicio enruta hacia Wrye Bash (sin ejecutar xEdit). Además,
``PatchResult`` transporta el ``strategy_type`` del plan seleccionado: el
enrutado previo adivinaba mirando ``orchestrator._strategies[0]``, un bug
latente que ejecutaba la estrategia equivocada.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.xedit_service import XEditPipelineService
from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.patch_orchestrator import (
    LEVELED_LIST_TYPES,
    DelegateToBashedPatch,
    PatchOrchestrator,
    PatchResult,
    PatchStrategyType,
)

if TYPE_CHECKING:
    import pathlib


def _conflicto(record_type: str, severity: str) -> RecordConflict:
    return RecordConflict(
        form_id="00012345",
        editor_id="Registro",
        record_type=record_type,
        winner="OverhaulB.esp",
        losers=["OverhaulA.esp"],
        severity=severity,
    )


def _reporte(conflicto: RecordConflict, criticos: int = 0) -> ConflictReport:
    par = PluginConflictPair(plugin_a="OverhaulA.esp", plugin_b="OverhaulB.esp", conflicts=[conflicto])
    return ConflictReport(
        total_conflicts=1,
        critical_conflicts=criticos,
        plugin_pairs=[par],
        summary="reporte de prueba",
    )


@pytest.fixture
def orquestador() -> PatchOrchestrator:
    return PatchOrchestrator(
        xedit_runner=MagicMock(),
        snapshot_manager=MagicMock(),
        rollback_manager=MagicMock(),
    )


class TestOrquestador:
    """El orquestador produce planes delegados para leveled lists."""

    def test_estrategias_por_defecto_incluyen_delegacion(self, orquestador: PatchOrchestrator) -> None:
        nombres = {s.__class__.__name__ for s in orquestador.strategies}
        # AIAssistedPatch (Fase 1): catch-all advisory para críticos sin script.
        assert nombres == {"ExecuteXEditScript", "DelegateToBashedPatch", "AIAssistedPatch"}

    async def test_resolve_lvli_produce_plan_delegado(self, orquestador: PatchOrchestrator) -> None:
        resultado = await orquestador.resolve(_reporte(_conflicto("LVLI", "warning")))

        assert resultado.success is True
        assert resultado.strategy_type is PatchStrategyType.DELEGATE_BASHED_PATCH
        assert any("Bashed Patch" in w for w in resultado.warnings)

    async def test_conflictos_criticos_no_van_al_bashed_patch(self, orquestador: PatchOrchestrator) -> None:
        """El punto de esta ancla es que la delegación NO roba críticos (ADR 0001).

        Con el bundle actual (sin fix_npc_conflicts.pas) el crítico enruta al
        advisor de IA — can_handle de ExecuteXEditScript exige script real
        (fail-closed anti-placebo). Con el .pas en disco volvería a
        EXECUTE_XEDIT_SCRIPT (anclado en test_ai_assisted_strategy.py).
        """
        resultado = await orquestador.resolve(_reporte(_conflicto("NPC_", "critical"), criticos=1))

        assert resultado.success is True
        assert resultado.strategy_type is not PatchStrategyType.DELEGATE_BASHED_PATCH
        assert resultado.strategy_type is PatchStrategyType.AI_ASSISTED

    async def test_delegacion_maneja_los_tres_tipos(self) -> None:
        estrategia = DelegateToBashedPatch()
        for tipo in ("LVLI", "LVLN", "LVSP"):
            assert await estrategia.can_handle(_conflicto(tipo, "warning")) is True
        assert await estrategia.can_handle(_conflicto("NPC_", "critical")) is False


class TestLeveledListTypesEsCerrado:
    """Ancla de regresión: LEVELED_LIST_TYPES es EXACTAMENTE {LVLI, LVLN, LVSP}.

    Una auditoría propuso ampliarlo a LVLC/LVEF/REFR/NAVM. El veredicto (OODA+TOT)
    fue NO ampliar, y este test fija ese veredicto para que nadie lo aplique a
    ciegas más adelante:

    - LVLC (Leveled Creature) y LVEF no existen como firmas de Skyrim SE — son
      relictos de Oblivion/Fallout. El repo ya tiene precedente de removerlos
      (SCA-001/T-07: SCPT→SCEN).
    - REFR (refs colocadas) y NAVM (navmesh) son un dominio de conflicto
      distinto, clasificado como WARNING en conflict_analyzer.DEFAULT_WARNING_TYPES.
      El Bashed Patch NO los fusiona; rutearlos por LEVELED_LIST_TYPES los
      mandaría a DelegateToBashedPatch → delegación falsa (ver ADR 0001), una
      regresión de correctitud y potencialmente destructiva para navmeshes.
    """

    def test_set_es_exactamente_los_tres_tipos_de_skyrim(self) -> None:
        assert sorted(LEVELED_LIST_TYPES) == ["LVLI", "LVLN", "LVSP"]

    def test_tipos_no_leveled_estan_excluidos(self) -> None:
        for tipo in ("REFR", "NAVM", "LVLC", "LVEF"):
            assert tipo not in LEVELED_LIST_TYPES

    async def test_delegacion_no_maneja_refr_ni_navm(self) -> None:
        """El guard a nivel comportamiento: la delegación a Wrye Bash rechaza
        conflictos de refs colocadas y navmesh."""
        estrategia = DelegateToBashedPatch()
        for tipo in ("REFR", "NAVM"):
            assert await estrategia.can_handle(_conflicto(tipo, "warning")) is False


class TestServicio:
    """El servicio enruta por el strategy_type SELECCIONADO, no por strategies[0]."""

    @pytest.fixture
    async def lock_manager(self, tmp_path: pathlib.Path) -> DistributedLockManager:
        mgr = DistributedLockManager(
            tmp_path / "locks.db",
            default_ttl=5.0,
            max_retries=2,
            backoff_base=0.05,
            backoff_max=0.2,
        )
        await mgr.initialize()
        yield mgr  # type: ignore[misc]
        await mgr.close()

    @pytest.fixture
    def servicio(self, lock_manager: DistributedLockManager, tmp_path: pathlib.Path) -> XEditPipelineService:
        snapshots = tmp_path / "snapshots"
        snapshots.mkdir()
        resolver = MagicMock()
        return XEditPipelineService(
            lock_manager=lock_manager,
            snapshot_manager=FileSnapshotManager(snapshot_dir=snapshots),
            journal=AsyncMock(begin_transaction=AsyncMock(return_value=1)),
            path_resolver=resolver,
            event_bus=AsyncMock(),
        )

    def _resultado_orquestador(self, strategy_type: PatchStrategyType | None, tmp_path: pathlib.Path) -> PatchResult:
        return PatchResult(
            success=True,
            output_path=tmp_path / "Salida.esp",
            records_patched=1,
            conflicts_resolved=1,
            xedit_exit_code=0,
            warnings=(),
            strategy_type=strategy_type,
        )

    async def _ejecutar(
        self,
        servicio: XEditPipelineService,
        resultado: PatchResult,
        tmp_path: pathlib.Path,
    ) -> tuple[dict, AsyncMock]:
        runner = MagicMock()
        runner.execute_patch = AsyncMock()
        servicio._xedit_runner = runner

        orquestador = AsyncMock()
        orquestador.resolve = AsyncMock(return_value=resultado)

        target = tmp_path / "TestMod.esp"
        target.write_bytes(b"TES4")
        reporte = MagicMock(spec=ConflictReport)
        reporte.total_conflicts = 1
        reporte.critical_conflicts = 0
        reporte.plugin_pairs = []

        with patch.object(servicio, "_ensure_patch_orchestrator", return_value=orquestador):
            respuesta = await servicio.execute_patch(reporte, target)
        return respuesta, runner.execute_patch

    async def test_plan_delegado_no_ejecuta_xedit(self, servicio: XEditPipelineService, tmp_path: pathlib.Path) -> None:
        resultado = self._resultado_orquestador(PatchStrategyType.DELEGATE_BASHED_PATCH, tmp_path)

        respuesta, execute_patch = await self._ejecutar(servicio, resultado, tmp_path)

        execute_patch.assert_not_awaited()
        assert respuesta["success"] is True
        assert respuesta["strategy_type"] == "delegate_bashed_patch"

    async def test_sin_strategy_type_ejecuta_script_generico(
        self, servicio: XEditPipelineService, tmp_path: pathlib.Path
    ) -> None:
        """Compatibilidad: un PatchResult viejo (sin strategy_type) va al script genérico."""
        from sky_claw.local.xedit.runner import ScriptExecutionResult

        resultado = self._resultado_orquestador(None, tmp_path)

        runner_result = ScriptExecutionResult(
            success=True,
            exit_code=0,
            stdout="",
            stderr="",
            records_processed=1,
            errors=[],
            warnings=[],
            script_path=None,
            execution_time=0.1,
        )

        runner = MagicMock()
        runner.execute_patch = AsyncMock(return_value=runner_result)
        servicio._xedit_runner = runner

        orquestador = AsyncMock()
        orquestador.resolve = AsyncMock(return_value=resultado)

        target = tmp_path / "TestMod.esp"
        target.write_bytes(b"TES4")
        reporte = MagicMock(spec=ConflictReport)
        reporte.total_conflicts = 1
        reporte.critical_conflicts = 0
        reporte.plugin_pairs = []

        with patch.object(servicio, "_ensure_patch_orchestrator", return_value=orquestador):
            respuesta = await servicio.execute_patch(reporte, target)

        execute_patch = runner.execute_patch
        execute_patch.assert_awaited_once()
        plan = execute_patch.await_args.args[0]
        assert plan.strategy_type is PatchStrategyType.EXECUTE_XEDIT_SCRIPT
        assert respuesta["success"] is True
