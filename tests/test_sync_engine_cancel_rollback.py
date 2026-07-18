"""F3 (auditoría 2026-07-18) — execute_file_operation: rollback blindado ante cancelación.

El bloque ``except asyncio.CancelledError`` ejecuta ``fail_operation`` +
``undo_operation`` para conservar el estado observable del archivo. Sin
``asyncio.shield``, una SEGUNDA cancelación (shutdown que cancela y un gather
del caller que re-cancela, o un ``wait_for`` externo) interrumpía el undo a
mitad: journal en FAILED sin restauración → archivo real a medias. Además, el
``finally`` encolaba el pruning pasivo (más awaits de DB/FS) en plena ruta de
unwind de la cancelación.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.db.journal import OperationType
from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine


class _RollbackManagerObservador:
    """RM fake: undo bloqueante y observable para simular la ventana de cleanup."""

    def __init__(self) -> None:
        self.fail_llamado = False
        self.undo_iniciado = asyncio.Event()
        self.liberar_undo = asyncio.Event()
        self.undo_completado = False
        self.stats_consultado = False

    async def begin_transaction(self, **kwargs: Any) -> int:
        return 100

    async def begin_operation(self, **kwargs: Any) -> int:
        return 200

    async def create_snapshot(self, path: pathlib.Path) -> Any:
        return MagicMock(snapshot_path="/fake/snapshot.bin")

    async def complete_operation(self, entry_id: int) -> None:
        return None

    async def commit_transaction(self, transaction_id: int) -> None:
        return None

    async def mark_transaction_rolled_back(self, transaction_id: int) -> None:
        return None

    async def fail_operation(self, entry_id: int, error: str = "") -> None:
        self.fail_llamado = True

    async def undo_operation(self, entry_id: int) -> Any:
        self.undo_iniciado.set()
        await self.liberar_undo.wait()
        self.undo_completado = True
        return MagicMock(success=True, transaction_id=100)

    async def get_snapshot_stats(self) -> Any:
        self.stats_consultado = True
        return MagicMock(total_size_bytes=0)


def _engine(rm: _RollbackManagerObservador) -> SyncEngine:
    return SyncEngine(
        mo2=AsyncMock(),
        masterlist=AsyncMock(),
        registry=AsyncMock(),
        rollback_manager=rm,  # type: ignore[arg-type]
    )


def _lanzar_operacion_bloqueada(engine: SyncEngine, tmp_path: pathlib.Path) -> tuple[asyncio.Task[Any], asyncio.Event]:
    """Lanza execute_file_operation con una operación que nunca completa."""
    arranco = asyncio.Event()

    async def operacion() -> None:
        arranco.set()
        await asyncio.Event().wait()  # bloqueada hasta la cancelación

    task = asyncio.create_task(
        engine.execute_file_operation(
            operation_type=OperationType.FILE_MODIFY,
            target_path=tmp_path / "mod.esp",
            operation=operacion(),
            description="operación cancelable",
        )
    )
    return task, arranco


async def _esperar(condicion: Any, timeout: float = 2.0) -> None:
    """Drena el loop hasta que ``condicion()`` sea verdadera (cleanup shieldeado)."""
    async with asyncio.timeout(timeout):
        while not condicion():
            await asyncio.sleep(0)


class TestRollbackAnteCancelacion:
    async def test_cancelacion_marca_fallida_y_revierte(self, tmp_path: pathlib.Path) -> None:
        """Contrato base de la rama cancelada: FAILED + undo, y la señal propaga."""
        rm = _RollbackManagerObservador()
        rm.liberar_undo.set()  # undo instantáneo
        task, arranco = _lanzar_operacion_bloqueada(_engine(rm), tmp_path)
        await asyncio.wait_for(arranco.wait(), timeout=2.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert rm.fail_llamado is True
        assert rm.undo_completado is True

    async def test_segunda_cancelacion_no_interrumpe_el_undo(self, tmp_path: pathlib.Path) -> None:
        """F3: el par fail+undo corre bajo shield — un segundo cancel en pleno
        undo no puede partirlo a la mitad (journal FAILED sin restauración)."""
        rm = _RollbackManagerObservador()
        task, arranco = _lanzar_operacion_bloqueada(_engine(rm), tmp_path)
        await asyncio.wait_for(arranco.wait(), timeout=2.0)

        task.cancel()  # 1ª cancelación → entra al rollback
        await asyncio.wait_for(rm.undo_iniciado.wait(), timeout=2.0)
        task.cancel()  # 2ª cancelación, con el undo en vuelo
        for _ in range(5):
            await asyncio.sleep(0)

        rm.liberar_undo.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        # El cleanup shieldeado debe completar el undo aunque el caller ya se fue.
        await _esperar(lambda: rm.undo_completado)
        assert rm.fail_llamado is True

    async def test_pruning_se_saltea_en_la_ruta_de_cancelacion(self, tmp_path: pathlib.Path) -> None:
        """F3: el finally no encola pruning (DB/FS) durante el unwind de una
        cancelación — corre en la próxima operación no cancelada."""
        rm = _RollbackManagerObservador()
        rm.liberar_undo.set()
        task, arranco = _lanzar_operacion_bloqueada(_engine(rm), tmp_path)
        await asyncio.wait_for(arranco.wait(), timeout=2.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert rm.stats_consultado is False

    async def test_pruning_sigue_corriendo_en_el_camino_normal(self, tmp_path: pathlib.Path) -> None:
        """Regresión: el skip es SOLO para cancelación; éxito y fallo común
        siguen ejecutando el pruning pasivo del finally."""
        rm = _RollbackManagerObservador()
        engine = _engine(rm)

        async def operacion_ok() -> str:
            return "ok"

        resultado = await engine.execute_file_operation(
            operation_type=OperationType.FILE_MODIFY,
            target_path=tmp_path / "mod.esp",
            operation=operacion_ok(),
            description="operación exitosa",
        )

        assert resultado == "ok"
        assert rm.stats_consultado is True
