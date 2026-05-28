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
           (SIEMPRE, incluso si el restore falló — ver T2-02)

        T2-02 — IDEMPOTENCIA: si ``restore_snapshot`` falla con OSError, NO
        re-lanzamos como antes. En su lugar, marcamos la entry como
        ROLLED_BACK y devolvemos ``success=False`` con los errores en
        ``RollbackResult.errors``.  Esto previene que un retry del caller
        re-fetch la misma entry (aún en COMPLETED) y vuelva a re-intentar el
        restore sobre un archivo posiblemente ya parcialmente restaurado —
        causando corrupción progresiva.
        """
        # Obtener última operación
        entry = await self._journal.get_last_operation(agent_id, [OperationStatus.COMPLETED, OperationStatus.FAILED])

        if entry is None:
            return RollbackResult(
                success=False,
                transaction_id=None,
                errors=("No completed or failed operation found for agent",),
            )

        partial_errors: list[str] = []
        restored = 0

        # Restaurar archivo desde snapshot (best-effort, sin re-lanzar)
        if entry.snapshot_path:
            try:
                await self._snapshots.restore_snapshot(
                    pathlib.Path(entry.snapshot_path), pathlib.Path(entry.target_path)
                )
                restored = 1
            except OSError as e:
                logger.critical(
                    "Rollback restore failed for %s (entry=%s): %s",
                    agent_id,
                    entry.id,
                    str(e),
                    exc_info=True,
                )
                partial_errors.append(f"snapshot_restore: {e}")

        # T2-02: marcar ROLLED_BACK SIEMPRE para garantizar idempotencia.
        # Sin esto, un retry del caller volvería a procesar la misma entry.
        try:
            await self._journal.mark_rolled_back(entry.id)
        except Exception as mark_exc:
            # Si no se puede marcar, futuros rollbacks van a re-intentar.
            # Es CRÍTICO porque cada retry puede empeorar el estado del archivo.
            logger.critical(
                "CRÍTICO: no se pudo marcar entry %s como rolled_back; "
                "rollbacks futuros lo re-intentarán: %s",
                entry.id,
                mark_exc,
                exc_info=True,
            )
            partial_errors.append(f"mark_rolled_back: {mark_exc}")

        return RollbackResult(
            success=not partial_errors,
            transaction_id=entry.id,
            entries_restored=restored,
            files_deleted=1 if entry.operation_type in [OperationType.FILE_CREATE, OperationType.MOD_INSTALL] else 0,
            errors=tuple(partial_errors),
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
