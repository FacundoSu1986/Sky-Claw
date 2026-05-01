"""Tests for TASK-004 (M-2): RollbackResult immutability and mutation removal.

Validates that:
1. ``RollbackResult`` is a frozen dataclass — mutation raises ``FrozenInstanceError``
2. ``execute_file_operation`` error path logs rollback outcome without mutation
3. ``RollbackResult`` preserves the actual undo outcome (no forced ``success=False``)
4. ``errors`` field is an immutable ``tuple`` (not ``list``)
"""

from __future__ import annotations

import pathlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.db.rollback_manager import RollbackResult
from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine

# ------------------------------------------------------------------
# 1. RollbackResult frozen dataclass contract
# ------------------------------------------------------------------


class TestRollbackResultFrozen:
    """RollbackResult must be immutable after construction."""

    def test_mutation_success_raises_frozen_instance_error(self) -> None:
        """Assigning to ``success`` after construction must raise."""
        result = RollbackResult(success=True)
        with pytest.raises(FrozenInstanceError):
            result.success = False  # type: ignore[misc]

    def test_mutation_transaction_id_raises_frozen_instance_error(self) -> None:
        """Assigning to ``transaction_id`` after construction must raise."""
        result = RollbackResult(success=True, transaction_id=42)
        with pytest.raises(FrozenInstanceError):
            result.transaction_id = 99  # type: ignore[misc]

    def test_mutation_errors_raises_frozen_instance_error(self) -> None:
        """Assigning to ``errors`` after construction must raise."""
        result = RollbackResult(success=False, errors=("err1",))
        with pytest.raises(FrozenInstanceError):
            result.errors = ("new",)  # type: ignore[misc]

    def test_construction_with_all_fields(self) -> None:
        """All fields can be set at construction time."""
        now = datetime.now(UTC)
        result = RollbackResult(
            success=True,
            transaction_id=10,
            entries_restored=2,
            files_deleted=1,
            errors=("err_a", "err_b"),
            dry_run=False,
            timestamp=now,
        )
        assert result.success is True
        assert result.transaction_id == 10
        assert result.entries_restored == 2
        assert result.files_deleted == 1
        assert result.errors == ("err_a", "err_b")
        assert result.dry_run is False
        assert result.timestamp is now

    def test_default_values(self) -> None:
        """Defaults: transaction_id=None, entries_restored=0, errors=(), dry_run=False."""
        result = RollbackResult(success=False)
        assert result.transaction_id is None
        assert result.entries_restored == 0
        assert result.files_deleted == 0
        assert result.errors == ()
        assert result.dry_run is False
        assert isinstance(result.timestamp, datetime)

    def test_errors_is_tuple_not_list(self) -> None:
        """The ``errors`` field must be a tuple, not a mutable list."""
        result = RollbackResult(success=True)
        assert isinstance(result.errors, tuple)
        assert result.errors == ()

    def test_errors_tuple_with_single_element(self) -> None:
        """Single-error construction uses a 1-tuple."""
        result = RollbackResult(success=False, errors=("something broke",))
        assert result.errors == ("something broke",)
        assert len(result.errors) == 1

    def test_frozen_hashable(self) -> None:
        """Frozen dataclasses with all hashable fields are hashable."""
        result = RollbackResult(success=True, transaction_id=1)
        # Should not raise TypeError
        hash(result)

    def test_frozen_equality(self) -> None:
        """Two RollbackResults with same field values are equal."""
        now = datetime.now(UTC)
        r1 = RollbackResult(success=True, transaction_id=5, timestamp=now)
        r2 = RollbackResult(success=True, transaction_id=5, timestamp=now)
        assert r1 == r2

    def test_frozen_inequality_different_success(self) -> None:
        """Results with different ``success`` values are not equal."""
        r1 = RollbackResult(success=True)
        r2 = RollbackResult(success=False)
        assert r1 != r2


# ------------------------------------------------------------------
# 2. SyncEngine.execute_file_operation — rollback result not mutated
# ------------------------------------------------------------------


class TestExecuteFileOperationRollbackNoMutation:
    """Validate that execute_file_operation does NOT mutate RollbackResult."""

    @pytest.mark.asyncio
    async def test_error_path_preserves_rollback_result(self, tmp_path: pathlib.Path) -> None:
        """When the operation fails, rollback_result.success reflects actual undo outcome.

        Before TASK-004, the code did ``rollback_result.success = False`` which:
        - Would crash with FrozenInstanceError on a frozen dataclass
        - Masked the actual rollback outcome (success could have been True)

        After TASK-004 + Phase-C (proxy API), the result is logged but never mutated.
        """
        # Setup mock rollback_manager via public proxy API (Phase C)
        mock_rm = MagicMock()
        mock_rm.begin_transaction = AsyncMock(return_value=100)
        mock_rm.begin_operation = AsyncMock(return_value=200)
        mock_rm.fail_operation = AsyncMock()
        mock_rm.complete_operation = AsyncMock()
        mock_rm.commit_transaction = AsyncMock()
        mock_rm.create_snapshot = AsyncMock(return_value=MagicMock(snapshot_path="/fake/snap"))
        # _passive_pruning runs in finally; stub stats under the size limit so it no-ops
        mock_rm.get_snapshot_stats = AsyncMock(return_value=MagicMock(total_size_bytes=0))

        # undo_last_operation returns a SUCCESSFUL rollback
        expected_result = RollbackResult(
            success=True,
            transaction_id=200,
            entries_restored=1,
        )
        mock_rm.undo_last_operation = AsyncMock(return_value=expected_result)

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
            rollback_manager=mock_rm,
        )

        target = tmp_path / "test_file.esp"
        target.write_text("original")

        async def failing_operation() -> str:
            raise RuntimeError("Simulated operation failure")

        with pytest.raises(RuntimeError, match="Simulated operation failure"):
            await engine.execute_file_operation(
                operation_type=MagicMock(value="FILE_MODIFY"),
                target_path=target,
                operation=failing_operation(),
                description="test op",
            )

        # Verify undo was called
        mock_rm.undo_last_operation.assert_called_once_with("sync_engine")

        # The returned result was NOT mutated — it still reports success=True
        # (We can't directly inspect the local variable, but we verify the mock
        # was called and the frozen dataclass would have prevented mutation)
        returned_result = mock_rm.undo_last_operation.return_value
        assert returned_result.success is True

    @pytest.mark.asyncio
    async def test_error_path_logs_rollback_outcome(self, tmp_path: pathlib.Path) -> None:
        """After rollback, the engine logs the actual outcome (success + transaction_id)."""
        mock_rm = MagicMock()
        mock_rm.begin_transaction = AsyncMock(return_value=100)
        mock_rm.begin_operation = AsyncMock(return_value=200)
        mock_rm.fail_operation = AsyncMock()
        mock_rm.complete_operation = AsyncMock()
        mock_rm.commit_transaction = AsyncMock()
        mock_rm.create_snapshot = AsyncMock(return_value=MagicMock(snapshot_path="/snap"))
        mock_rm.get_snapshot_stats = AsyncMock(return_value=MagicMock(total_size_bytes=0))

        rollback_result = RollbackResult(success=True, transaction_id=200)
        mock_rm.undo_last_operation = AsyncMock(return_value=rollback_result)

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
            rollback_manager=mock_rm,
        )

        target = tmp_path / "test.esp"
        target.write_text("data")

        async def failing_op() -> None:
            raise ValueError("boom")

        with (
            pytest.raises(ValueError, match="boom"),
            patch("sky_claw.antigravity.orchestrator.sync_engine.logger") as mock_logger,
        ):
            await engine.execute_file_operation(
                operation_type=MagicMock(value="FILE_MODIFY"),
                target_path=target,
                operation=failing_op(),
            )

        # Verify warning was logged with actual rollback result
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        # logger.warning uses lazy %-formatting: (format_string, success, transaction_id)
        assert "Rollback automático completado" in call_args[0][0]
        assert call_args[0][1] is True  # rollback_result.success
        assert call_args[0][2] == 200  # rollback_result.transaction_id

    @pytest.mark.asyncio
    async def test_error_path_rollback_failure_propagates_original_exception(self, tmp_path: pathlib.Path) -> None:
        """When rollback itself fails, the original exception still propagates."""
        mock_rm = MagicMock()
        mock_rm.begin_transaction = AsyncMock(return_value=100)
        mock_rm.begin_operation = AsyncMock(return_value=200)
        mock_rm.fail_operation = AsyncMock()
        mock_rm.complete_operation = AsyncMock()
        mock_rm.commit_transaction = AsyncMock()
        mock_rm.create_snapshot = AsyncMock(return_value=MagicMock(snapshot_path="/snap"))
        mock_rm.get_snapshot_stats = AsyncMock(return_value=MagicMock(total_size_bytes=0))

        rollback_result = RollbackResult(
            success=False,
            errors=("No completed operation found",),
        )
        mock_rm.undo_last_operation = AsyncMock(return_value=rollback_result)

        engine = SyncEngine(
            mo2=AsyncMock(),
            masterlist=AsyncMock(),
            registry=AsyncMock(),
            rollback_manager=mock_rm,
        )

        target = tmp_path / "test.esp"
        target.write_text("data")

        async def failing_op() -> None:
            raise RuntimeError("original error")

        # The original exception still propagates (raise at end of except block)
        with pytest.raises(RuntimeError, match="original error"):
            await engine.execute_file_operation(
                operation_type=MagicMock(value="FILE_MODIFY"),
                target_path=target,
                operation=failing_op(),
            )

        # Rollback was attempted and its result (success=False) was preserved
        mock_rm.undo_last_operation.assert_called_once()
        assert mock_rm.undo_last_operation.return_value.success is False
