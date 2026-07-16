"""T-28 en el promotion flow: la strategy de Synthesis sandboxeado cierra la caja
negra emitiendo el FlightReport tras resolver el staged journal.

El manifiesto (T-26) se persiste durante ``execute_pipeline`` con las rutas del
clon; recién tras ``commit_staged``/``rollback_staged`` la TX real tiene su
estado final. La strategy compone el informe desde el journal REAL para esa TX y
traduce las rutas del clon a las reales **solo al promover** (mismo criterio que
``rewrite_clone_paths`` del flow). Follow-up del PR #309.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.orchestrator.preview.action_manifest import build_action_manifest
from sky_claw.antigravity.orchestrator.tool_strategies.execute_synthesis import (
    ExecuteSynthesisPipelineStrategy,
)

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
async def real_journal(tmp_path: pathlib.Path):  # noqa: ANN201
    from sky_claw.antigravity.db.journal import OperationJournal

    j = OperationJournal(tmp_path / "flight_journal.db")
    await j.open()
    yield j  # type: ignore[misc]
    await j.close()


def _fake_clone(tmp_path: pathlib.Path) -> SimpleNamespace:
    """Clon fake con las 4 rutas que consume ``rewrite_clone_paths``."""
    return SimpleNamespace(
        overwrite_copy=tmp_path / "clone" / "overwrite",
        overwrite_source=tmp_path / "MO2" / "overwrite",
        profile_copy=tmp_path / "clone" / "profile",
        profile_source=tmp_path / "MO2" / "profiles" / "Default",
        root=tmp_path / "clone",
    )


class _FlowFake:
    """SandboxPromotionFlow fake: corre el ritual contra el clon y anota promoted."""

    def __init__(self, clone: SimpleNamespace, *, promoted: bool) -> None:
        self._clone = clone
        self._promoted = promoted

    async def run(self, *, ritual_name: str, ritual: Any) -> dict[str, Any]:
        result = dict(await ritual(self._clone))
        result["sandbox"] = {
            "promoted": self._promoted,
            "decision": "approved" if self._promoted else "denied",
        }
        return result


def _manifest_writing_factory(clone: SimpleNamespace):  # noqa: ANN202
    """service_factory fake: su execute_pipeline abre TX y persiste el manifiesto
    (vía el staging journal) apuntando al ESP del CLON, como el servicio real."""

    def _factory(output_path: pathlib.Path, staging_journal: Any) -> MagicMock:
        service = MagicMock()

        async def _execute_pipeline(**_kwargs: Any) -> dict[str, Any]:
            tx_id = await staging_journal.begin_transaction(
                description="synthesis_pipeline", agent_id="synthesis-service"
            )
            manifest = build_action_manifest(
                ritual_id=f"synthesis-pipeline-{tx_id}",
                tool="Synthesis",
                tool_version=None,
                target_files=[str(output_path / "Synthesis.esp")],
                snapshots=[],
                summary="run",
            )
            await staging_journal.persist_action_manifest(manifest, agent_id="synthesis-service", transaction_id=tx_id)
            # execute_pipeline real commitea la TX en éxito; en el staging journal
            # eso solo marca el commit diferido (lo confirma commit_staged luego).
            await staging_journal.commit_transaction(tx_id)
            return {"success": True, "message": "", "output_esp": str(output_path / "Synthesis.esp")}

        service.execute_pipeline = AsyncMock(side_effect=_execute_pipeline)
        return service

    return _factory


async def _flight_reports_ultima_tx(journal: Any) -> list[Any]:
    from sky_claw.antigravity.orchestrator.preview.flight_report import FlightReport

    (ultima,) = await journal.list_recent_transactions(limit=1)
    ops = await journal.get_operations_by_transaction(ultima.transaction_id)
    return [
        FlightReport.model_validate(e.metadata) for e in ops if e.metadata and e.metadata.get("kind") == "flight_report"
    ]


def _strategy(real_journal: Any, clone: SimpleNamespace, *, promoted: bool) -> ExecuteSynthesisPipelineStrategy:
    return ExecuteSynthesisPipelineStrategy(
        flow_provider=lambda: _FlowFake(clone, promoted=promoted),
        service_factory=_manifest_writing_factory(clone),
        real_journal_provider=lambda: real_journal,
    )


@pytest.mark.asyncio
async def test_promovido_emite_informe_committed_con_rutas_reales(real_journal: Any, tmp_path: pathlib.Path) -> None:
    clone = _fake_clone(tmp_path)
    strategy = _strategy(real_journal, clone, promoted=True)

    result = await strategy.execute({"patcher_ids": ["a"]})

    assert result["sandbox"]["promoted"] is True
    informes = await _flight_reports_ultima_tx(real_journal)
    assert len(informes) == 1
    informe = informes[0]
    assert informe.transaction_status == "committed"
    # La ruta del clon quedó traducida a la real del overwrite.
    assert str(clone.overwrite_source / "Synthesis.esp") in informe.files_touched
    assert str(clone.overwrite_copy / "Synthesis.esp") not in informe.files_touched


@pytest.mark.asyncio
async def test_descartado_emite_informe_rolled_back_sin_traducir(real_journal: Any, tmp_path: pathlib.Path) -> None:
    clone = _fake_clone(tmp_path)
    strategy = _strategy(real_journal, clone, promoted=False)

    result = await strategy.execute({"patcher_ids": ["a"]})

    assert result["sandbox"]["promoted"] is False
    informes = await _flight_reports_ultima_tx(real_journal)
    assert len(informes) == 1
    informe = informes[0]
    assert informe.transaction_status == "rolled_back"
    # Descarte: no se aplicó nada al real, así que el informe conserva la ruta del clon.
    assert str(clone.overwrite_copy / "Synthesis.esp") in informe.files_touched


@pytest.mark.asyncio
async def test_sin_transaccion_no_emite_informe(real_journal: Any, tmp_path: pathlib.Path) -> None:
    """Si el ritual no abre TX (p.ej. fail-closed antes de mutar), no hay caja negra
    que cerrar: no se emite ningún informe."""
    clone = _fake_clone(tmp_path)

    def _factory(output_path: pathlib.Path, staging_journal: Any) -> MagicMock:
        service = MagicMock()
        service.execute_pipeline = AsyncMock(return_value={"success": False, "message": "sin TX"})
        return service

    strategy = ExecuteSynthesisPipelineStrategy(
        flow_provider=lambda: _FlowFake(clone, promoted=False),
        service_factory=_factory,
        real_journal_provider=lambda: real_journal,
    )

    await strategy.execute({"patcher_ids": ["a"]})

    txs = await real_journal.list_recent_transactions(limit=1)
    assert txs == []  # ninguna TX abierta → ningún informe


@pytest.mark.asyncio
async def test_rollback_fallido_no_emite_informe_de_descarte(real_journal: Any, tmp_path: pathlib.Path) -> None:
    """SandboxRollbackFailed = promote falló Y su rollback también: el overwrite
    real puede estar inconsistente y el clon queda como backup manual. NO se emite
    un FlightReport de descarte limpio que mienta "nada llegó al real" (review #310)."""
    clone = _fake_clone(tmp_path)

    class _FlowRollbackFailed:
        async def run(self, *, ritual_name: str, ritual: Any) -> dict[str, Any]:
            result = dict(await ritual(clone))
            result["success"] = False
            result["reason"] = "SandboxRollbackFailed"
            result["sandbox"] = {"promoted": False, "decision": "rollback_failed"}
            return result

    strategy = ExecuteSynthesisPipelineStrategy(
        flow_provider=lambda: _FlowRollbackFailed(),
        service_factory=_manifest_writing_factory(clone),
        real_journal_provider=lambda: real_journal,
    )

    await strategy.execute({"patcher_ids": ["a"]})

    # La TX existe (el ritual la abrió) pero el informe de descarte se omite.
    informes = await _flight_reports_ultima_tx(real_journal)
    assert informes == []
