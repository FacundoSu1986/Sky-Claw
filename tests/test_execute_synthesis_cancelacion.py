"""Review Codex #320 (P1): resolución del journal diferido ante cancelación.

Si ``execute_synthesis_pipeline`` es cancelado durante un promote aprobado y el
promote shieldeado termina COMPLETANDO, los archivos reales quedan aplicados
pero la strategy nunca llegaba a ``commit_staged()``/``rollback_staged()``: la
TX diferida del :class:`StagingJournal` quedaba sin estado final en el journal
real. La strategy ahora captura la cancelación, consulta el desenlace terminal
del promote vía ``flow.desenlace_promocion()`` (bajo shield) y resuelve la TX
— commit si los cambios llegaron al árbol real, rollback si no — antes de
propagar la señal.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.orchestrator.tool_strategies.execute_synthesis import (
    ExecuteSynthesisPipelineStrategy,
)


def _fake_clone(tmp_path: pathlib.Path) -> SimpleNamespace:
    return SimpleNamespace(
        overwrite_copy=tmp_path / "clone" / "overwrite",
        overwrite_source=tmp_path / "MO2" / "overwrite",
        profile_copy=tmp_path / "clone" / "profile",
        profile_source=tmp_path / "MO2" / "profiles" / "Default",
        root=tmp_path / "clone",
    )


class _FlowCanceladoEnPromote:
    """Flow fake: el ritual corre (abre la TX staged) y la cancelación llega
    durante el promote; ``desenlace_promocion`` reporta el desenlace real."""

    def __init__(self, clone: SimpleNamespace, *, desenlace: str, ritual_corre: bool = True) -> None:
        self._clone = clone
        self._desenlace = desenlace
        self._ritual_corre = ritual_corre

    async def run(self, *, ritual_name: str, ritual: Any) -> dict[str, Any]:
        if self._ritual_corre:
            await ritual(self._clone)
        raise asyncio.CancelledError()

    async def desenlace_promocion(self) -> str:
        return self._desenlace


def _tx_staging_factory() -> Any:
    """service_factory fake: execute_pipeline abre la TX en el staging journal y
    la comitea (diferido), como el servicio real en éxito."""

    def _factory(output_path: pathlib.Path, staging_journal: Any) -> MagicMock:
        service = MagicMock()

        async def _execute_pipeline(**_kwargs: Any) -> dict[str, Any]:
            tx_id = await staging_journal.begin_transaction(
                description="synthesis_pipeline", agent_id="synthesis-service"
            )
            await staging_journal.commit_transaction(tx_id)
            return {"success": True, "message": ""}

        service.execute_pipeline = AsyncMock(side_effect=_execute_pipeline)
        return service

    return _factory


def _real_journal_mock(tx_id: int = 7) -> MagicMock:
    journal = MagicMock()
    journal.begin_transaction = AsyncMock(return_value=tx_id)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    return journal


def _strategy(real_journal: MagicMock, flow: _FlowCanceladoEnPromote) -> ExecuteSynthesisPipelineStrategy:
    return ExecuteSynthesisPipelineStrategy(
        flow_provider=lambda: flow,
        service_factory=_tx_staging_factory(),
        real_journal_provider=lambda: real_journal,
    )


class TestResolucionStagedTrasCancelacion:
    async def test_promote_completado_comitea_la_tx_diferida_y_propaga(self, tmp_path: pathlib.Path) -> None:
        """El caso del review: archivos reales aplicados pese al cancel → la TX
        diferida se confirma en el journal real, y la señal propaga igual."""
        real_journal = _real_journal_mock(tx_id=7)
        strategy = _strategy(real_journal, _FlowCanceladoEnPromote(_fake_clone(tmp_path), desenlace="aplicada"))

        with pytest.raises(asyncio.CancelledError):
            await strategy.execute({})

        real_journal.commit_transaction.assert_awaited_once_with(7)
        real_journal.mark_transaction_rolled_back.assert_not_awaited()

    async def test_promote_no_aplicado_revierte_la_tx_diferida_y_propaga(self, tmp_path: pathlib.Path) -> None:
        """Cancelación sin cambios en el árbol real → la TX diferida se marca
        rolled_back (no queda PENDING para siempre)."""
        real_journal = _real_journal_mock(tx_id=7)
        strategy = _strategy(real_journal, _FlowCanceladoEnPromote(_fake_clone(tmp_path), desenlace="no_aplicada"))

        with pytest.raises(asyncio.CancelledError):
            await strategy.execute({})

        real_journal.mark_transaction_rolled_back.assert_awaited_once_with(7)
        real_journal.commit_transaction.assert_not_awaited()

    async def test_cancelacion_sin_tx_abierta_solo_propaga(self, tmp_path: pathlib.Path) -> None:
        """Si el ritual nunca corrió (cancel temprano) no hay TX que resolver:
        el journal real no se toca."""
        real_journal = _real_journal_mock()
        strategy = _strategy(
            real_journal,
            _FlowCanceladoEnPromote(_fake_clone(tmp_path), desenlace="no_aplicada", ritual_corre=False),
        )

        with pytest.raises(asyncio.CancelledError):
            await strategy.execute({})

        real_journal.commit_transaction.assert_not_awaited()
        real_journal.mark_transaction_rolled_back.assert_not_awaited()
        real_journal.begin_transaction.assert_not_awaited()

    async def test_rollback_fallido_marca_rolled_back_pero_alerta_recuperacion_manual(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Review Codex #322 (P2): promote fallido CON rollback fallido no es un
        descarte limpio — la TX se marca rolled_back (criterio #310) pero queda
        el CRITICAL de restauración manual, no un rollback silencioso."""
        real_journal = _real_journal_mock(tx_id=7)
        strategy = _strategy(real_journal, _FlowCanceladoEnPromote(_fake_clone(tmp_path), desenlace="rollback_fallido"))

        with caplog.at_level("CRITICAL"), pytest.raises(asyncio.CancelledError):
            await strategy.execute({})

        real_journal.mark_transaction_rolled_back.assert_awaited_once_with(7)
        real_journal.commit_transaction.assert_not_awaited()
        assert any("restauración manual" in r.message for r in caplog.records if r.levelname == "CRITICAL")


class TestDrainDePendientes:
    async def test_drain_espera_las_resoluciones_en_vuelo(self, tmp_path: pathlib.Path) -> None:
        """Review Codex #322 (P2): el shutdown del supervisor drena las
        resoluciones ANTES de cerrar el journal — drain_pendientes bloquea
        hasta que la resolución en background termina."""
        strategy = _strategy(_real_journal_mock(), _FlowCanceladoEnPromote(_fake_clone(tmp_path), desenlace="aplicada"))
        liberar = asyncio.Event()
        terminado = False

        async def _resolucion_lenta() -> None:
            nonlocal terminado
            await liberar.wait()
            terminado = True

        pendiente = asyncio.ensure_future(_resolucion_lenta())
        strategy._resoluciones_pendientes.add(pendiente)
        pendiente.add_done_callback(strategy._resoluciones_pendientes.discard)

        drain_task = asyncio.create_task(strategy.drain_pendientes())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not drain_task.done()  # bloqueado esperando la resolución

        liberar.set()
        await asyncio.wait_for(drain_task, timeout=2.0)
        assert terminado is True

    async def test_dispatcher_drain_invoca_a_las_strategies_que_lo_exponen(self) -> None:
        """El supervisor llama dispatcher.drain() en su shutdown; toda strategy
        con drain_pendientes() debe ser esperada (duck-typed)."""
        from sky_claw.antigravity.orchestrator.tool_dispatcher import OrchestrationToolDispatcher

        drenados: list[str] = []

        class _StrategyConDrain:
            name = "con_drain"

            async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
                return {}

            async def drain_pendientes(self) -> None:
                drenados.append(self.name)

        class _StrategySinDrain:
            name = "sin_drain"

            async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
                return {}

        dispatcher = OrchestrationToolDispatcher()
        dispatcher.register(_StrategyConDrain())
        dispatcher.register(_StrategySinDrain())

        await dispatcher.drain()

        assert drenados == ["con_drain"]
