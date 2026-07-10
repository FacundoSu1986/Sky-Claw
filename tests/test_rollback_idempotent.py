"""QA-13 — undo_last_operation idempotente ante restore failure (T2-02).

Verifica que cuando ``restore_snapshot`` falla:
  (a) ``RollbackResult.success == False`` y ``errors`` contiene el mensaje.
  (b) La entry queda marcada como ROLLED_BACK en el journal (no en COMPLETED).
  (c) Un segundo ``undo_last_operation`` para el mismo agente devuelve
      "No completed or failed operation found" — porque ya fue rolled-back.

Sin este fix, el caller retry vería la entry aún en COMPLETED y reintentaría
el restore sobre el archivo ya parcialmente restaurado, causando corrupción
progresiva.

**P1 review fix (PR #140)**: el ``FileSnapshotManager`` real envuelve fallos
de I/O en ``JournalSnapshotError`` (snapshot missing, checksum mismatch,
``OSError`` capturados internamente). El test original sólo usaba ``OSError``
directo, lo que NO replicaba el comportamiento de producción. Ahora el fixture
default usa ``JournalSnapshotError`` y se mantiene un test paramétrico que
también cubre ``OSError`` para defense-in-depth.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from sky_claw.antigravity.db.journal import (
    JournalSnapshotError,
    OperationJournal,
    OperationStatus,
    OperationType,
)
from sky_claw.antigravity.db.rollback_manager import RollbackManager


@pytest.fixture
async def journal(tmp_path):
    """Journal sobre SQLite en tmp_path."""
    db_path = tmp_path / "rollback_idempotent.db"
    lifecycle = DatabaseLifecycleManager(
        db_paths=[],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    j = OperationJournal(db_path, lifecycle=lifecycle)
    await j.open()
    yield j
    await j.close()
    await lifecycle.shutdown_all()


@pytest.fixture
def failing_snapshot_manager():
    """SnapshotManager mock cuyo restore_snapshot falla con JournalSnapshotError.

    P1 fix: esto refleja el comportamiento real del production
    ``FileSnapshotManager.restore_snapshot``, que envuelve fallos de I/O
    (OSError de shutil, missing snapshot, checksum mismatch) en
    ``JournalSnapshotError``. El test original usaba OSError directo y por
    eso no detectaba que ``except OSError`` del rollback_manager NO capturaba
    nada en producción.
    """
    mgr = MagicMock()
    mgr.restore_snapshot = AsyncMock(side_effect=JournalSnapshotError("Snapshot file does not exist: /fake/snap.bin"))
    return mgr


@pytest.mark.asyncio
async def test_restore_failure_marks_rolled_back_and_returns_failure(journal, failing_snapshot_manager, tmp_path):
    """Restore falla → mark_rolled_back se ejecuta igualmente → result.success=False."""
    rm = RollbackManager(journal, failing_snapshot_manager)

    # Setup: una operación COMPLETED en el journal con un snapshot path.
    tx_id = await journal.begin_transaction(description="test", mod_id=None, agent_id="qa-13")
    entry_id = await journal.begin_operation(
        agent_id="qa-13",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(tmp_path / "target.esp"),
        transaction_id=tx_id,
        snapshot_path=str(tmp_path / "snapshot.bin"),
    )
    await journal.complete_operation(entry_id)

    # Ejecutar rollback — el restore va a fallar.
    result = await rm.undo_last_operation("qa-13")

    assert result.success is False
    assert len(result.errors) == 1
    assert "Snapshot file does not exist" in result.errors[0]
    assert result.transaction_id == entry_id
    # restore intentado una vez.
    failing_snapshot_manager.restore_snapshot.assert_awaited_once()

    # CRÍTICO: la entry debe estar marcada como ROLLED_BACK aunque restore falló.
    entry = await journal.get_last_operation(
        "qa-13",
        [OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.ROLLED_BACK],
    )
    assert entry is not None
    assert entry.status == OperationStatus.ROLLED_BACK


@pytest.mark.asyncio
async def test_second_undo_returns_no_operation_found(journal, failing_snapshot_manager, tmp_path):
    """Tras un rollback fallido, un retry no debe re-procesar la misma entry."""
    rm = RollbackManager(journal, failing_snapshot_manager)

    tx_id = await journal.begin_transaction(description="test", mod_id=None, agent_id="qa-13b")
    entry_id = await journal.begin_operation(
        agent_id="qa-13b",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(tmp_path / "target.esp"),
        transaction_id=tx_id,
        snapshot_path=str(tmp_path / "snapshot.bin"),
    )
    await journal.complete_operation(entry_id)

    # Primer rollback (falla en restore, pero marca rolled_back).
    first = await rm.undo_last_operation("qa-13b")
    assert first.success is False

    # Segundo rollback — NO debe re-procesar la misma entry.
    second = await rm.undo_last_operation("qa-13b")
    assert second.success is False
    assert second.transaction_id is None
    assert second.errors == ("No completed or failed operation found for agent",)

    # restore_snapshot fue llamado solo UNA vez, no dos.
    assert failing_snapshot_manager.restore_snapshot.await_count == 1


@pytest.mark.asyncio
async def test_happy_path_still_works(journal, tmp_path):
    """Si restore funciona, success=True como antes."""
    successful_mgr = MagicMock()
    successful_mgr.restore_snapshot = AsyncMock(return_value=True)
    rm = RollbackManager(journal, successful_mgr)

    tx_id = await journal.begin_transaction(description="test", mod_id=None, agent_id="qa-13c")
    entry_id = await journal.begin_operation(
        agent_id="qa-13c",
        operation_type=OperationType.MOD_INSTALL,
        target_path=str(tmp_path / "target.esp"),
        transaction_id=tx_id,
        snapshot_path=str(tmp_path / "snapshot.bin"),
    )
    await journal.complete_operation(entry_id)

    result = await rm.undo_last_operation("qa-13c")
    assert result.success is True
    assert result.entries_restored == 1
    assert result.errors == ()

    entry = await journal.get_last_operation(
        "qa-13c",
        [OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.ROLLED_BACK],
    )
    assert entry is not None
    assert entry.status == OperationStatus.ROLLED_BACK


@pytest.mark.parametrize(
    "exception",
    [
        # Production-realistic: lo que el FileSnapshotManager real lanza.
        JournalSnapshotError("Snapshot file does not exist: /fake/snap.bin"),
        JournalSnapshotError("Checksum verification failed for /target.esp."),
        # Defense-in-depth: si una implementación futura de SnapshotManager
        # NO envuelve OSError, el rollback_manager debe seguir manejándolo.
        OSError(28, "No space left on device"),
        PermissionError("Read-only file system"),
    ],
    ids=["snapshot_missing", "checksum_mismatch", "disk_full", "read_only_fs"],
)
@pytest.mark.asyncio
async def test_restore_failure_handled_for_multiple_exception_types(journal, tmp_path, exception):
    """Cualquier excepcion de restore_snapshot conocida marca rolled_back + retorna success=False.

    P1 review fix: cubre tanto JournalSnapshotError (lo que produccion lanza)
    como OSError directo (defense-in-depth).
    """
    mgr = MagicMock()
    mgr.restore_snapshot = AsyncMock(side_effect=exception)
    rm = RollbackManager(journal, mgr)

    agent_id = f"qa-13-{type(exception).__name__}"
    tx_id = await journal.begin_transaction(description="test", mod_id=None, agent_id=agent_id)
    entry_id = await journal.begin_operation(
        agent_id=agent_id,
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(tmp_path / "target.esp"),
        transaction_id=tx_id,
        snapshot_path=str(tmp_path / "snapshot.bin"),
    )
    await journal.complete_operation(entry_id)

    result = await rm.undo_last_operation(agent_id)
    assert result.success is False
    assert len(result.errors) == 1
    assert "snapshot_restore" in result.errors[0]

    # Entry marcada ROLLED_BACK SIEMPRE — la propiedad clave de idempotencia.
    entry = await journal.get_last_operation(
        agent_id,
        [OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.ROLLED_BACK],
    )
    assert entry is not None
    assert entry.status == OperationStatus.ROLLED_BACK


@pytest.mark.asyncio
async def test_no_snapshot_path_marks_rolled_back_without_restore(journal, tmp_path):
    """Si la entry no tiene snapshot_path, no se llama restore pero igual se marca."""
    no_op_mgr = MagicMock()
    no_op_mgr.restore_snapshot = AsyncMock()
    rm = RollbackManager(journal, no_op_mgr)

    tx_id = await journal.begin_transaction(description="test", mod_id=None, agent_id="qa-13d")
    entry_id = await journal.begin_operation(
        agent_id="qa-13d",
        operation_type=OperationType.FILE_CREATE,
        target_path=str(tmp_path / "new.esp"),
        transaction_id=tx_id,
        snapshot_path=None,
    )
    await journal.complete_operation(entry_id)

    result = await rm.undo_last_operation("qa-13d")
    assert result.success is True
    assert result.entries_restored == 0
    no_op_mgr.restore_snapshot.assert_not_called()

    entry = await journal.get_last_operation(
        "qa-13d",
        [OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.ROLLED_BACK],
    )
    assert entry is not None
    assert entry.status == OperationStatus.ROLLED_BACK


@pytest.mark.asyncio
async def test_undo_operation_reverts_specific_entry_not_last(journal, tmp_path):
    """H-1: undo_operation(entry_id) revierte la operación indicada, no la última del agente.

    Escenario del hallazgo: dos operaciones del MISMO agent_id (A luego B), ambas
    COMPLETED. undo_last_operation(agent_id) revertiría B (la más reciente) aunque
    quien falló fuera A. undo_operation(entry_id_A) debe revertir exactamente A.
    """
    restored_paths: list[str] = []
    mgr = MagicMock()

    async def _record_restore(snapshot, target):
        restored_paths.append(str(target))
        return True

    mgr.restore_snapshot = AsyncMock(side_effect=_record_restore)
    rm = RollbackManager(journal, mgr)

    tx_id = await journal.begin_transaction(description="dual", mod_id=None, agent_id="dual-agent")
    entry_a = await journal.begin_operation(
        agent_id="dual-agent",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(tmp_path / "A.esp"),
        transaction_id=tx_id,
        snapshot_path=str(tmp_path / "A.bin"),
    )
    await journal.complete_operation(entry_a)
    entry_b = await journal.begin_operation(
        agent_id="dual-agent",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(tmp_path / "B.esp"),
        transaction_id=tx_id,
        snapshot_path=str(tmp_path / "B.bin"),
    )
    await journal.complete_operation(entry_b)

    # Revertir explícitamente A (la operación "vieja"), no la última (B).
    result = await rm.undo_operation(entry_a)

    assert result.success is True
    assert result.transaction_id == entry_a
    # Se restauró SÓLO el target de A.
    assert restored_paths == [str(tmp_path / "A.esp")]

    # A quedó ROLLED_BACK; B sigue COMPLETED (intacta).
    entry_a_after = await journal.get_operation_by_id(entry_a)
    entry_b_after = await journal.get_operation_by_id(entry_b)
    assert entry_a_after.status == OperationStatus.ROLLED_BACK
    assert entry_b_after.status == OperationStatus.COMPLETED


@pytest.mark.asyncio
async def test_undo_operation_missing_entry_returns_failure(journal):
    """undo_operation con un entry_id inexistente devuelve success=False sin tocar disco."""
    mgr = MagicMock()
    mgr.restore_snapshot = AsyncMock()
    rm = RollbackManager(journal, mgr)

    result = await rm.undo_operation(999999)

    assert result.success is False
    assert result.transaction_id == 999999
    mgr.restore_snapshot.assert_not_awaited()
