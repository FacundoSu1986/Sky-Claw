"""QA-12 — execute_file_operation: rollback de transacción en early failure (T2-01).

Verifica que cuando ``create_snapshot()`` o ``begin_operation()`` lanzan antes
de asignar ``entry_id``, la transacción se marca ROLLED_BACK y el snapshot
huérfano se limpia. Sin este fix, la tx queda en PENDING para siempre y los
snapshots se acumulan en disco sin referencia en el journal.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.db.journal import OperationType
from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine


def _make_engine_with_mock_rm() -> tuple[SyncEngine, MagicMock]:
    mock_rm = MagicMock()
    mock_rm.begin_transaction = AsyncMock(return_value=100)
    mock_rm.create_snapshot = AsyncMock(return_value=MagicMock(snapshot_path="/fake/snapshot.bin"))
    mock_rm.begin_operation = AsyncMock(return_value=200)
    mock_rm.complete_operation = AsyncMock()
    mock_rm.fail_operation = AsyncMock()
    mock_rm.commit_transaction = AsyncMock()
    mock_rm.mark_transaction_rolled_back = AsyncMock()
    mock_rm.undo_last_operation = AsyncMock()
    mock_rm.get_snapshot_stats = AsyncMock(return_value=MagicMock(total_size_bytes=0))

    engine = SyncEngine(
        mo2=AsyncMock(),
        masterlist=AsyncMock(),
        registry=AsyncMock(),
        rollback_manager=mock_rm,
    )
    return engine, mock_rm


@pytest.mark.asyncio
async def test_create_snapshot_failure_marks_transaction_rolled_back(
    tmp_path: pathlib.Path,
) -> None:
    """Si ``create_snapshot`` lanza, la tx ya creada debe quedar ROLLED_BACK."""
    engine, mock_rm = _make_engine_with_mock_rm()
    mock_rm.create_snapshot.side_effect = PermissionError("snapshot dir read-only")

    target = tmp_path / "mod.esp"
    target.write_text("content")

    async def noop_operation() -> None:
        # Esta corutina se cancelará sin ejecutarse — la excepción ocurre antes.
        return None

    op = noop_operation()
    try:
        with pytest.raises(PermissionError, match="snapshot dir read-only"):
            await engine.execute_file_operation(
                operation_type=OperationType.FILE_MODIFY,
                target_path=target,
                operation=op,
                description="early-fail snapshot",
            )
    finally:
        op.close()

    # La transacción se creó pero el snapshot falló → debe haberse marcado rolled_back.
    mock_rm.mark_transaction_rolled_back.assert_awaited_once_with(100)
    # No se debe haber llamado a undo_last_operation (no hay entry_id que deshacer).
    mock_rm.undo_last_operation.assert_not_called()
    # No se debe haber llamado a complete/commit.
    mock_rm.complete_operation.assert_not_called()
    mock_rm.commit_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_begin_operation_failure_marks_tx_rolled_back_and_cleans_snapshot(
    tmp_path: pathlib.Path,
) -> None:
    """Si ``begin_operation`` lanza tras un snapshot exitoso, el snapshot
    huérfano debe ser borrado del disco y la tx debe quedar ROLLED_BACK."""
    engine, mock_rm = _make_engine_with_mock_rm()

    # Crear un snapshot real en disco para verificar la limpieza.
    snap_file = tmp_path / "snapshot.bin"
    snap_file.write_bytes(b"snapshot-data")
    mock_rm.create_snapshot.return_value = MagicMock(snapshot_path=str(snap_file))

    mock_rm.begin_operation.side_effect = RuntimeError("journal disk full")

    target = tmp_path / "mod.esp"
    target.write_text("content")

    async def noop_operation() -> None:
        return None

    op = noop_operation()
    try:
        with pytest.raises(RuntimeError, match="journal disk full"):
            await engine.execute_file_operation(
                operation_type=OperationType.FILE_MODIFY,
                target_path=target,
                operation=op,
                description="early-fail begin_operation",
            )
    finally:
        op.close()

    # Tx marcada rolled_back.
    mock_rm.mark_transaction_rolled_back.assert_awaited_once_with(100)
    # Snapshot huérfano eliminado del disco.
    assert not snap_file.exists(), "Snapshot huérfano debió ser limpiado"
    # Sin entry_id → no undo_last_operation.
    mock_rm.undo_last_operation.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_still_works(tmp_path: pathlib.Path) -> None:
    """Caso feliz: complete_operation + commit_transaction se llaman normalmente."""
    engine, mock_rm = _make_engine_with_mock_rm()
    target = tmp_path / "mod.esp"
    target.write_text("content")

    async def ok_operation() -> str:
        return "done"

    result = await engine.execute_file_operation(
        operation_type=OperationType.FILE_MODIFY,
        target_path=target,
        operation=ok_operation(),
        description="happy",
    )

    assert result == "done"
    mock_rm.complete_operation.assert_awaited_once_with(200)
    mock_rm.commit_transaction.assert_awaited_once_with(100)
    # No early-failure cleanup en el happy path.
    mock_rm.mark_transaction_rolled_back.assert_not_called()


@pytest.mark.asyncio
async def test_mark_transaction_rolled_back_failure_does_not_mask_original(
    tmp_path: pathlib.Path,
) -> None:
    """Si ``mark_transaction_rolled_back`` también lanza, debemos seguir
    propagando la excepción original (contextlib.suppress en el cleanup)."""
    engine, mock_rm = _make_engine_with_mock_rm()
    mock_rm.create_snapshot.side_effect = PermissionError("original error")
    mock_rm.mark_transaction_rolled_back.side_effect = RuntimeError("db down")

    target = tmp_path / "mod.esp"
    target.write_text("content")

    async def noop_operation() -> None:
        return None

    op = noop_operation()
    try:
        # La excepción que sale debe ser la original (PermissionError), no la del cleanup.
        with pytest.raises(PermissionError, match="original error"):
            await engine.execute_file_operation(
                operation_type=OperationType.FILE_MODIFY,
                target_path=target,
                operation=op,
            )
    finally:
        op.close()

    mock_rm.mark_transaction_rolled_back.assert_awaited_once_with(100)
