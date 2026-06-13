# tests/test_journal.py

import pathlib

import pytest

from sky_claw.antigravity.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.antigravity.db.journal import (
    OperationJournal,
    OperationStatus,
    OperationType,
    TransactionStatus,
)
from sky_claw.antigravity.db.rollback_manager import RollbackManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager


@pytest.fixture
async def journal(tmp_path):
    """Fixture que provides a fresh journal instance (M-01: lifecycle injected)."""
    db_path = tmp_path / "test_journal.db"
    lifecycle = DatabaseLifecycleManager(
        db_paths=[],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    journal = OperationJournal(db_path, lifecycle=lifecycle)
    await journal.open()
    yield journal
    await journal.close()
    await lifecycle.shutdown_all()


@pytest.fixture
async def snapshot_manager(tmp_path):
    """Fixture that provides a snapshot manager instance."""
    snapshot_dir = tmp_path / "snapshots"
    manager = FileSnapshotManager(snapshot_dir)
    yield manager


class TestOperationJournal:
    """Tests for OperationJournal."""

    @pytest.mark.asyncio
    async def test_open_close(self, journal):
        """Test opening and closing journal."""
        assert journal._db is not None

    @pytest.mark.asyncio
    async def test_begin_operation(self, journal):
        """Test beginning an operation."""
        tx_id = await journal.begin_transaction(description="test tx", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="test_agent",
            operation_type=OperationType.MOD_INSTALL,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
        )
        assert entry_id > 0

    @pytest.mark.asyncio
    async def test_complete_operation(self, journal):
        """Test completing an operation."""
        tx_id = await journal.begin_transaction(description="test tx", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="test_agent",
            operation_type=OperationType.MOD_INSTALL,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
        )
        await journal.complete_operation(entry_id)

        entry = await journal.get_last_operation("test_agent")
        assert entry is not None
        assert entry.status == OperationStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fail_operation(self, journal):
        """Test failing an operation."""
        tx_id = await journal.begin_transaction(description="test tx", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="test_agent",
            operation_type=OperationType.MOD_INSTALL,
            target_path="/test/path/mod.esp",
            transaction_id=tx_id,
        )
        await journal.fail_operation(entry_id, "Test error")

        entry = await journal.get_last_operation("test_agent")
        assert entry is not None
        assert entry.status == OperationStatus.FAILED


class TestFileSnapshotManager:
    """Tests for FileSnapshotManager."""

    @pytest.mark.asyncio
    async def test_create_snapshot(self, snapshot_manager, tmp_path):
        """Test creating a snapshot."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        snapshot_info = await snapshot_manager.create_snapshot(test_file)
        assert snapshot_info is not None
        assert snapshot_info.original_path == str(test_file)
        assert pathlib.Path(snapshot_info.snapshot_path).exists()
        assert pathlib.Path(snapshot_info.snapshot_path).read_text() == "test content"

    @pytest.mark.asyncio
    async def test_restore_snapshot(self, snapshot_manager, tmp_path):
        """Test restoring a snapshot."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original content")

        snapshot_info = await snapshot_manager.create_snapshot(test_file)

        # Modify original file
        test_file.write_text("modified content")

        # Restore
        result = await snapshot_manager.restore_snapshot(snapshot_info.snapshot_path, test_file)
        assert result
        assert test_file.read_text() == "original content"


class TestRollbackManager:
    """Tests for RollbackManager."""

    @pytest.fixture
    async def rollback_manager(self, journal, snapshot_manager):
        """Fixture that provides a rollback manager instance."""
        manager = RollbackManager(journal, snapshot_manager)
        yield manager

    @pytest.mark.asyncio
    async def test_undo_last_operation(self, rollback_manager, journal, tmp_path):
        """Test undoing last operation."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original content")

        # Create snapshot
        snapshot_info = await rollback_manager._snapshots.create_snapshot(test_file)

        # Begin and complete operation
        tx_id = await journal.begin_transaction(description="test tx", mod_id=None)
        entry_id = await journal.begin_operation(
            agent_id="test_agent",
            operation_type=OperationType.FILE_MODIFY,
            target_path=str(test_file),
            transaction_id=tx_id,
            snapshot_path=snapshot_info.snapshot_path,
        )
        await journal.complete_operation(entry_id)

        # Modify file
        test_file.write_text("modified content")

        # Rollback
        result = await rollback_manager.undo_last_operation("test_agent")
        assert result.success
        assert test_file.read_text() == "original content"


class TestTransactionContextManager:
    """transaction() — commit/rollback garantizado (hardening jun-2026).

    Sin context manager, una excepción entre begin_transaction() y
    commit_transaction() deja la fila PENDING para siempre.
    """

    @pytest.mark.asyncio
    async def test_transaction_commits_on_clean_exit(self, journal):
        async with journal.transaction("ctx tx ok") as tx_id:
            assert tx_id > 0

        tx = await journal.get_transaction(tx_id)
        assert tx is not None
        assert tx.status is TransactionStatus.COMMITTED

    @pytest.mark.asyncio
    async def test_transaction_rolls_back_on_exception(self, journal):
        with pytest.raises(RuntimeError, match="boom"):
            async with journal.transaction("ctx tx fail") as tx_id:
                raise RuntimeError("boom")

        tx = await journal.get_transaction(tx_id)
        assert tx is not None
        assert tx.status is TransactionStatus.ROLLED_BACK
        assert tx.rolled_back_at is not None

    @pytest.mark.asyncio
    async def test_rollback_transaction_marks_pending_tx(self, journal):
        tx_id = await journal.begin_transaction("manual rollback")

        await journal.rollback_transaction(tx_id)

        tx = await journal.get_transaction(tx_id)
        assert tx is not None
        assert tx.status is TransactionStatus.ROLLED_BACK
        assert tx.rolled_back_at is not None

    @pytest.mark.asyncio
    async def test_sweep_stale_pending_rolls_back_old_transactions(self, journal):
        """El sweeper marca ROLLED_BACK solo las PENDING más viejas que el umbral."""
        tx_old = await journal.begin_transaction("stale orphan")
        await journal._db.execute(
            "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
            (tx_old,),
        )
        await journal._db.commit()
        tx_new = await journal.begin_transaction("fresh")

        swept = await journal.sweep_stale_pending(max_age_hours=24.0)

        assert swept == 1
        assert (await journal.get_transaction(tx_old)).status is TransactionStatus.ROLLED_BACK
        assert (await journal.get_transaction(tx_new)).status is TransactionStatus.PENDING

    @pytest.mark.asyncio
    async def test_open_sweeps_stale_pending(self, tmp_path):
        """Las PENDING huérfanas de una sesión anterior se barren al abrir."""
        db_path = tmp_path / "sweep_on_open.db"
        lifecycle = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        try:
            j1 = OperationJournal(db_path, lifecycle=lifecycle)
            await j1.open()
            tx_id = await j1.begin_transaction("orphan from previous session")
            await j1._db.execute(
                "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
                (tx_id,),
            )
            await j1._db.commit()
            await j1.close()

            j2 = OperationJournal(db_path, lifecycle=lifecycle)
            await j2.open()
            tx = await j2.get_transaction(tx_id)
            assert tx is not None
            assert tx.status is TransactionStatus.ROLLED_BACK
            await j2.close()
        finally:
            await lifecycle.shutdown_all()
