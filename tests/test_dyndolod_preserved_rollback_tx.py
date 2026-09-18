"""#592.1 — semántica de preservado_para_deployment en el cierre de TX."""

from __future__ import annotations

import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.db.journal import JournalTransactionError
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from tests.test_dyndolod_service import (
    _pipeline_con_texgen_current,
    _texgen_que_genera_current,
)

pytest_plugins = ("tests.test_dyndolod_service",)


def _protector(target: pathlib.Path, *, rollback_completed: bool) -> MagicMock:
    protector = MagicMock()
    protector.target = target
    protector.rollback_completed = rollback_completed
    return protector


@pytest.mark.asyncio
async def test_cerrar_tx_con_mutacion_preservada_no_marca_rolled_back(
    service: DynDOLODPipelineService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R1+R2 (#592.1): preservado vivo ⇒ TX no rolled back y log deliberado.

    El resto de protectores puede haber cerrado bien. Eso no autoriza afirmar
    rollback total: hay una mutación viva por diseño.
    """
    preservado = pathlib.Path("/mods/TexGen Output")
    dir_rollbacks = [
        _protector(preservado, rollback_completed=False),
        _protector(pathlib.Path("/work/TexGen"), rollback_completed=True),
    ]

    with caplog.at_level(logging.DEBUG, logger="SkyClaw.DynDOLODPipelineService"):
        rolled_back = await service._cerrar_tx_tras_rollback(
            42,
            dir_rollbacks,
            journal_committed=False,
            mutation_started=True,
            mutation_coverage_complete=True,
            contexto="error de dominio",
            preservado_para_deployment=preservado,
        )

    assert rolled_back is False
    service._journal.mark_transaction_rolled_back.assert_not_awaited()
    mensajes = [r.getMessage() for r in caplog.records]
    assert any("PRESERVADA a propósito" in msg and "PENDIENTE" in msg for msg in mensajes)
    assert not any("rollback INCOMPLETO" in msg for msg in mensajes)


@pytest.mark.asyncio
async def test_cerrar_tx_sin_preservado_con_rollback_incompleto_sigue_fail_closed(
    service: DynDOLODPipelineService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3 (#592.1): sin preservación, un rollback roto sigue siendo incompleto."""
    vivo = pathlib.Path("/mods/DynDOLOD Output")
    dir_rollbacks = [_protector(vivo, rollback_completed=False)]

    with caplog.at_level(logging.DEBUG, logger="SkyClaw.DynDOLODPipelineService"):
        rolled_back = await service._cerrar_tx_tras_rollback(
            42,
            dir_rollbacks,
            journal_committed=False,
            mutation_started=True,
            mutation_coverage_complete=True,
            contexto="error de dominio",
        )

    assert rolled_back is False
    service._journal.mark_transaction_rolled_back.assert_not_awaited()
    criticos = [r for r in caplog.records if "rollback INCOMPLETO" in r.getMessage()]
    assert criticos
    assert criticos[0].levelno == logging.CRITICAL
    assert not any("PRESERVADA a propósito" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cerrar_tx_sin_preservado_con_rollback_completo_marca_rolled_back(
    service: DynDOLODPipelineService,
) -> None:
    """R4 (#592.1): sin mutación viva, el cierre histórico se conserva."""
    cerrado = pathlib.Path("/mods/DynDOLOD Output")
    dir_rollbacks = [_protector(cerrado, rollback_completed=True)]

    rolled_back = await service._cerrar_tx_tras_rollback(
        42,
        dir_rollbacks,
        journal_committed=False,
        mutation_started=True,
        mutation_coverage_complete=True,
        contexto="error de dominio",
    )

    assert rolled_back is True
    service._journal.mark_transaction_rolled_back.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_certificacion_fallida_no_reporta_preservacion_como_rollback_incompleto(
    service: DynDOLODPipelineService,
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-02 / #592.1: preservar + certificar mal no cierra la TX como rolled back.

    El camino productivo descarta hoy el path de `_preservar_mod_de_texgen` y el
    cierre transaccional cuenta esa mutación viva como rollback roto. El artifact
    sigue en disco a propósito: la TX queda PENDIENTE y el log no puede pedir
    que se auditen backups como si el restore hubiera fallado.
    """
    _config, runner, staging, mod_texgen = _pipeline_con_texgen_current(tmp_path, service)
    service._journal.crear_handoff_de_deployment = AsyncMock(
        side_effect=JournalTransactionError("handoff no durable", transaction_id=42)
    )

    with (
        caplog.at_level(logging.WARNING, logger="SkyClaw.DynDOLODPipelineService"),
        patch.object(runner, "_execute_process", _texgen_que_genera_current(tmp_path, staging)),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        result = await service.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["success"] is False
    assert result["rolled_back"] is False
    assert "needs_deployment" not in result
    assert (mod_texgen / "textures" / "a.dds").read_bytes() == b"CURRENT!"
    service._journal.mark_transaction_rolled_back.assert_not_awaited()
    service._journal.commit_transaction.assert_not_awaited()
    mensajes = [r.getMessage() for r in caplog.records]
    assert any("PENDIENTE con una mutación PRESERVADA a propósito" in msg for msg in mensajes)
    assert not any("rollback INCOMPLETO" in msg for msg in mensajes)
