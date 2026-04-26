# sky_claw/db/rollback_manager.py

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .journal import OperationJournal, OperationStatus, OperationType

if TYPE_CHECKING:
    from .snapshot_manager import CleanupResult, FileSnapshotManager, SnapshotInfo, SnapshotStats

logger = logging.getLogger(__name__)


class RollbackError(Exception):
    """Error durante operación de rollback."""

    pass


@dataclass(frozen=True)
class RollbackResult:
    """Resultado inmutable de una operación de rollback.

    ``frozen=True`` garantiza que el resultado no pueda ser mutado después
    de la construcción, previniendo bugs por asignación accidental de campos
    (ej. ``result.success = False`` que enmascararía el resultado real del rollback).
    """

    success: bool
    transaction_id: int | None = None
    entries_restored: int = 0
    files_deleted: int = 0
    errors: tuple[str, ...] = ()
    dry_run: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class RollbackManager:
    """Gestiona operaciones de rollback para archivos."""

    def __init__(
        self,
        journal: OperationJournal,
        snapshot_manager: FileSnapshotManager,
    ) -> None:
        self._journal = journal
        self._snapshots = snapshot_manager

    async def __aenter__(self) -> RollbackManager:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        pass

    async def undo_last_operation(self, agent_id: str) -> RollbackResult:
        """
        Revierte la última operación de un agente.

        Flujo:
        1. Consultar Journal para obtener última operación 'COMPLETED' o 'FAILED'
        2. Delegar al FileSnapshotManager para restaurar archivo original
        3. Actualizar estado de la entrada en el Journal a 'ROLLED_BACK'
        """
        # Obtener última operación
        entry = await self._journal.get_last_operation(agent_id, [OperationStatus.COMPLETED, OperationStatus.FAILED])

        if entry is None:
            return RollbackResult(
                success=False,
                transaction_id=None,
                errors=("No completed or failed operation found for agent",),
            )

        # Restaurar archivo desde snapshot
        if entry.snapshot_path:
            try:
                await self._snapshots.restore_snapshot(
                    pathlib.Path(entry.snapshot_path), pathlib.Path(entry.target_path)
                )
            except OSError as e:
                logger.critical("Rollback failed for %s: %s", agent_id, str(e), exc_info=True)
                raise RollbackError(str(e)) from e

        # Actualizar estado en journal
        await self._journal.mark_rolled_back(entry.id)

        return RollbackResult(
            success=True,
            transaction_id=entry.id,
            entries_restored=1 if entry.snapshot_path else 0,
            files_deleted=1 if entry.operation_type in [OperationType.FILE_CREATE, OperationType.MOD_INSTALL] else 0,
            errors=(),
        )

    # ------------------------------------------------------------------
    # Public proxy API — avoid direct access to private _journal/_snapshots
    # ------------------------------------------------------------------

    async def begin_transaction(
        self,
        description: str,
        mod_id: int | None = None,
        agent_id: str = "system",
    ) -> int:
        """Begin a new journal transaction; return the transaction ID."""
        return await self._journal.begin_transaction(description=description, mod_id=mod_id, agent_id=agent_id)

    async def commit_transaction(self, transaction_id: int) -> None:
        """Mark a journal transaction as committed."""
        await self._journal.commit_transaction(transaction_id)

    async def mark_transaction_rolled_back(self, transaction_id: int) -> None:
        """Mark a journal transaction as rolled back without undoing file operations.

        Use this when a transaction was started but no file operations were
        recorded (e.g. update cycle aborted early due to network error or HITL
        denial).  Unlike ``undo_last_operation``, this method never touches the
        filesystem — it only updates the journal row.
        """
        await self._journal.mark_transaction_rolled_back(transaction_id)

    async def begin_operation(
        self,
        agent_id: str,
        operation_type: OperationType,
        target_path: str,
        transaction_id: int | None = None,
        snapshot_path: str | None = None,
        checksum: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Record the start of a file operation; return the entry ID."""
        return await self._journal.begin_operation(
            agent_id=agent_id,
            operation_type=operation_type,
            target_path=target_path,
            transaction_id=transaction_id,
            snapshot_path=snapshot_path,
            checksum=checksum,
            metadata=metadata,
        )

    async def complete_operation(self, entry_id: int) -> None:
        """Mark a journal entry as completed."""
        await self._journal.complete_operation(entry_id)

    async def fail_operation(self, entry_id: int, error: str = "") -> None:
        """Mark a journal entry as failed."""
        await self._journal.fail_operation(entry_id, error=error)

    async def create_snapshot(self, file_path: pathlib.Path) -> SnapshotInfo:
        """Capture a point-in-time snapshot of *file_path*."""
        return await self._snapshots.create_snapshot(file_path)

    async def get_snapshot_stats(self) -> SnapshotStats:
        """Return current size/count statistics for the snapshot store."""
        return await self._snapshots.get_stats()

    async def cleanup_old_snapshots(self, days_old: int = 30, dry_run: bool = False) -> CleanupResult:
        """Remove snapshots older than *days_old* days."""
        return await self._snapshots.cleanup_old_snapshots(days_old=days_old, dry_run=dry_run)
