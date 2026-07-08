"""Contratos de error en local/xedit tras BLE001 (T-11 de TECHNICAL_REVIEW_TASKS.md).

Segunda carpeta migrada a BLE001 (T-10 fue ``local/tools``). Los tres blind
excepts reales tenían tratamientos distintos:

* ``verify_masters``: acotado a errores del runner (``XEditError``/``OSError``)
  — un bug inesperado ahora PROPAGA en vez de volverse un string de error.
* Parser de líneas CONFLICT: acotado a errores de parseo.
* ``select_strategy``: aislamiento deliberado de estrategias custom rotas
  (log error + traceback, sigue con la próxima).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer, parse_conflict_lines
from sky_claw.local.xedit.patch_orchestrator import (
    DelegateToBashedPatch,
    PatchOrchestrator,
    PatchStrategy,
)
from sky_claw.local.xedit.runner import XEditTimeoutError


class TestVerifyMasters:
    async def test_error_del_runner_se_reporta_como_mensaje(self) -> None:
        runner = MagicMock()
        runner.run_script = AsyncMock(side_effect=XEditTimeoutError("timeout tras 120s"))

        errores = await ConflictAnalyzer().verify_masters(["Skyrim.esm"], runner)

        assert errores == ["timeout tras 120s"]

    async def test_bug_inesperado_propaga(self) -> None:
        """Un error que NO viene del runner es un bug: no debe degradarse a
        string silenciosamente (contrato post-BLE001)."""
        runner = MagicMock()
        runner.run_script = AsyncMock(side_effect=RuntimeError("bug interno"))

        with pytest.raises(RuntimeError, match="bug interno"):
            await ConflictAnalyzer().verify_masters(["Skyrim.esm"], runner)


class TestParseConflictLines:
    def test_linea_malformada_se_saltea_sin_romper(self) -> None:
        stdout = "\n".join(
            [
                "CONFLICT|00012345|LItemSword|LVLI|B.esp|A.esp",
                "CONFLICT|rota",  # malformada: se saltea
                "CONFLICT|00054321|BanditNPC|NPC_|B.esp|A.esp",
            ]
        )

        conflictos = parse_conflict_lines(stdout)

        assert [c.form_id for c in conflictos] == ["00012345", "00054321"]


class TestSelectStrategy:
    async def test_estrategia_custom_rota_se_saltea(self) -> None:
        """Aislamiento del plugin-boundary: una can_handle que explota no
        tumba la selección — se loguea y se prueba la siguiente."""

        class EstrategiaRota(PatchStrategy):
            async def can_handle(self, conflict: object) -> bool:
                raise ValueError("estrategia rota")

            async def create_plan(self, conflicts: list) -> object:  # pragma: no cover
                raise NotImplementedError

            def get_priority(self) -> int:
                return 99

        orquestador = PatchOrchestrator(
            xedit_runner=MagicMock(),
            snapshot_manager=MagicMock(),
            rollback_manager=MagicMock(),
            strategies=[EstrategiaRota(), DelegateToBashedPatch()],
        )
        conflicto = MagicMock()
        conflicto.record_type = "LVLI"

        seleccionada = await orquestador.select_strategy(conflicto)

        assert isinstance(seleccionada, DelegateToBashedPatch)

    async def test_cancelacion_propaga_no_se_traga(self) -> None:
        """CancelledError debe propagar (no tratarse como estrategia rota) —
        convención del repo, review Copilot PR #242."""
        import asyncio

        class EstrategiaCancelada(PatchStrategy):
            async def can_handle(self, conflict: object) -> bool:
                raise asyncio.CancelledError

            async def create_plan(self, conflicts: list) -> object:  # pragma: no cover
                raise NotImplementedError

            def get_priority(self) -> int:
                return 99

        orquestador = PatchOrchestrator(
            xedit_runner=MagicMock(),
            snapshot_manager=MagicMock(),
            rollback_manager=MagicMock(),
            strategies=[EstrategiaCancelada(), DelegateToBashedPatch()],
        )
        conflicto = MagicMock()
        conflicto.record_type = "LVLI"

        with pytest.raises(asyncio.CancelledError):
            await orquestador.select_strategy(conflicto)
