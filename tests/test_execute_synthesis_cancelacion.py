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

    def __init__(self, clone: SimpleNamespace, *, promovido: bool, ritual_corre: bool = True) -> None:
        self._clone = clone
        self._promovido = promovido
        self._ritual_corre = ritual_corre

    async def run(self, *, ritual_name: str, ritual: Any) -> dict[str, Any]:
        if self._ritual_corre:
            await ritual(self._clone)
        raise asyncio.CancelledError()

    async def desenlace_promocion(self) -> bool:
        return self._promovido


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
        strategy = _strategy(real_journal, _FlowCanceladoEnPromote(_fake_clone(tmp_path), promovido=True))

        with pytest.raises(asyncio.CancelledError):
            await strategy.execute({})

        real_journal.commit_transaction.assert_awaited_once_with(7)
        real_journal.mark_transaction_rolled_back.assert_not_awaited()

    async def test_promote_no_aplicado_revierte_la_tx_diferida_y_propaga(self, tmp_path: pathlib.Path) -> None:
        """Cancelación sin cambios en el árbol real → la TX diferida se marca
        rolled_back (no queda PENDING para siempre)."""
        real_journal = _real_journal_mock(tx_id=7)
        strategy = _strategy(real_journal, _FlowCanceladoEnPromote(_fake_clone(tmp_path), promovido=False))

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
            _FlowCanceladoEnPromote(_fake_clone(tmp_path), promovido=False, ritual_corre=False),
        )

        with pytest.raises(asyncio.CancelledError):
            await strategy.execute({})

        real_journal.commit_transaction.assert_not_awaited()
        real_journal.mark_transaction_rolled_back.assert_not_awaited()
        real_journal.begin_transaction.assert_not_awaited()
