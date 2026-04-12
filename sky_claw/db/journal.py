"""
Sistema de Journaling para operaciones de archivos.
Implementación asíncrona usando aiosqlite.

Este módulo proporciona un registro durable de todas las operaciones
de archivos realizadas, permitiendo rollback completo de transacciones.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPCIONES JERÁRQUICAS
# =============================================================================


class JournalError(Exception):
    """Excepción base para errores del journal."""

    def __init__(self, message: str, operation_id: int | None = None) -> None:
        super().__init__(message)
        self.operation_id = operation_id


class JournalConnectionError(JournalError):
    """Error de conexión a la base de datos del journal."""

    pass


class JournalTransactionError(JournalError):
    """Error en operaciones de transacción."""

    def __init__(
        self,
        message: str,
        transaction_id: int | None = None,
        operation_id: int | None = None,
    ) -> None:
        super().__init__(message, operation_id)
        self.transaction_id = transaction_id


class JournalSnapshotError(JournalError):
    """Error en operaciones de snapshot."""

    pass


class JournalRollbackError(JournalError):
    """Error durante operaciones de rollback."""

    def __init__(
        self,
        message: str,
        transaction_id: int | None = None,
        partial_success: bool = False,
    ) -> None:
        super().__init__(message)
        self.transaction_id = transaction_id
        self.partial_success = partial_success


# =============================================================================
# ENUMS
# =============================================================================


class OperationType(str, Enum):
    """Tipos de operaciones journalizables."""

    MOD_INSTALL = "mod_install"
    MOD_UNINSTALL = "mod_uninstall"
    MOD_UPDATE = "mod_update"
    PLUGIN_CLEAN = "plugin_clean"
    FILE_CREATE = "file_create"
    FILE_MODIFY = "file_modify"
    FILE_DELETE = "file_delete"
    FILE_RENAME = "file_rename"


class OperationStatus(str, Enum):
    """Estados de una operación en el journal."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class TransactionStatus(str, Enum):
    """Estados de una transacción."""

    PENDING = "pending"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """Entrada individual en el journal de operaciones."""

    id: int
    timestamp: datetime
    agent_id: str
    operation_type: OperationType
    target_path: str
    status: OperationStatus
    snapshot_path: str | None = None
    checksum: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Transaction:
    """Transacción agrupadora de operaciones."""

    transaction_id: int
    mod_id: int | None
    description: str
    status: TransactionStatus
    created_at: datetime
    committed_at: datetime | None = None
    rolled_back_at: datetime | None = None


@dataclass
class TransactionResult:
    """Resultado de una transacción de journal."""

    success: bool
    transaction_id: int | None
    entries_count: int = 0
    message: str = ""


@dataclass
class RollbackResult:
    """Resultado de una operación de rollback."""

    success: bool
    transaction_id: int
    files_restored: int = 0
    files_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# =============================================================================
# OPERATION JOURNAL
# =============================================================================


class OperationJournal:
    """
    Journal de operaciones de archivos para rollback.

    Implementación asíncrona usando aiosqlite para mejor rendimiento
    y manejo concurrente de operaciones.

    Usage:
        journal = OperationJournal(db_path)
        await journal.open()

        try:
            tx_id = await journal.begin_transaction("install_mod", mod_id=123)
            entry_id = await journal.log_operation(
                tx_id, OperationType.FILE_CREATE, "/path/to/file"
            )
            await journal.commit_transaction(tx_id)
        finally:
            await journal.close()
    """

    _SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        mod_id INTEGER,
        description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        committed_at TEXT,
        rolled_back_at TEXT
    );
    
    CREATE TABLE IF NOT EXISTS journal_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id INTEGER NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
        timestamp TEXT NOT NULL DEFAULT (datetime('now')),
        agent_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        target_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'started',
        snapshot_path TEXT,
        checksum TEXT,
        metadata TEXT,
        rolled_back INTEGER DEFAULT 0
    );
    
    CREATE INDEX IF NOT EXISTS idx_journal_transaction ON journal_entries(transaction_id);
    CREATE INDEX IF NOT EXISTS idx_journal_agent ON journal_entries(agent_id);
    CREATE INDEX IF NOT EXISTS idx_journal_status ON journal_entries(status);
    CREATE INDEX IF NOT EXISTS idx_journal_timestamp ON journal_entries(timestamp);
    CREATE INDEX IF NOT EXISTS idx_journal_path ON journal_entries(target_path);
    """

    def __init__(self, db_path: pathlib.Path | None = None) -> None:
        """
        Inicializa el journal.

        Args:
            db_path: Path al archivo de base de datos SQLite.
                     Si es None, usa un path por defecto.
        """
        raw_path = str(db_path or ".skyclaw_journal.db")
        from sky_claw.core.validators.path import PathTraversalValidator

        validator = PathTraversalValidator(allow_absolute=True)
        result = validator.validate(raw_path)
        if not result.is_valid:
            raise ValueError(
                f"Path traversal detected in journal path '{raw_path}': {result.error_message}"
            )

        self._db_path = pathlib.Path(raw_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._current_transaction: int | None = None

    async def open(self) -> None:
        """Abre la conexión a la base de datos y crea el schema si es necesario."""
        if self._db is not None:
            return

        try:
            self._db = await aiosqlite.connect(str(self._db_path))
            await self._db.executescript(self._SCHEMA_SQL)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")
            logger.info(
                "Journal database opened", extra={"db_path": str(self._db_path)}
            )
        except sqlite3.Error as e:
            raise JournalConnectionError(f"Failed to open journal database: {e}") from e

    async def close(self) -> None:
        """Cierra la conexión a la base de datos."""
        if self._db:
            await self._db.close()
            self._db = None
            self._current_transaction = None
            logger.info("Journal database closed")

    async def _ensure_connected(self) -> aiosqlite.Connection:
        """Asegura que la conexión está abierta."""
        if self._db is None:
            await self.open()
        if self._db is None:
            raise JournalConnectionError("Database connection not available")
        return self._db

    # =========================================================================
    # TRANSACTION MANAGEMENT
    # =========================================================================

    async def begin_transaction(
        self,
        description: str,
        mod_id: int | None = None,
        agent_id: str = "system",
    ) -> int:
        """
        Inicia una nueva transacción.

        Args:
            description: Descripción de la transacción.
            mod_id: ID del mod asociado (opcional).
            agent_id: ID del agente que inicia la transacción.

        Returns:
            ID de la transacción creada.

        Raises:
            JournalTransactionError: Si falla la creación.
        """
        db = await self._ensure_connected()

        async with self._lock:
            try:
                cursor = await db.execute(
                    """
                    INSERT INTO transactions (mod_id, description, status)
                    VALUES (?, ?, ?)
                    """,
                    (mod_id, description, TransactionStatus.PENDING.value),
                )
                await db.commit()
                transaction_id = cursor.lastrowid

                if transaction_id is None:
                    raise JournalTransactionError(
                        "Failed to get transaction ID after insert"
                    )

                self._current_transaction = transaction_id

                logger.info(
                    "Transaction started",
                    extra={
                        "transaction_id": transaction_id,
                        "description": description,
                        "mod_id": mod_id,
                        "agent_id": agent_id,
                    },
                )

                return transaction_id

            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to begin transaction: {e}"
                ) from e

    async def commit_transaction(self, transaction_id: int) -> None:
        """
        Marca una transacción como committed.

        Args:
            transaction_id: ID de la transacción a confirmar.

        Raises:
            JournalTransactionError: Si la transacción no existe o ya fue procesada.
        """
        db = await self._ensure_connected()

        async with self._lock:
            try:
                cursor = await db.execute(
                    """
                    UPDATE transactions 
                    SET status = ?, committed_at = datetime('now')
                    WHERE transaction_id = ? AND status = ?
                    """,
                    (
                        TransactionStatus.COMMITTED.value,
                        transaction_id,
                        TransactionStatus.PENDING.value,
                    ),
                )
                await db.commit()

                if cursor.rowcount == 0:
                    raise JournalTransactionError(
                        f"Transaction {transaction_id} not found or not pending",
                        transaction_id=transaction_id,
                    )

                if self._current_transaction == transaction_id:
                    self._current_transaction = None

                logger.info(
                    "Transaction committed", extra={"transaction_id": transaction_id}
                )

            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to commit transaction: {e}", transaction_id=transaction_id
                ) from e

    async def get_transaction(self, transaction_id: int) -> Transaction | None:
        """
        Obtiene una transacción por su ID.

        Args:
            transaction_id: ID de la transacción.

        Returns:
            La transacción o None si no existe.
        """
        db = await self._ensure_connected()

        async with self._lock:
            async with db.execute(
                """
                SELECT transaction_id, mod_id, description, status, 
                       created_at, committed_at, rolled_back_at
                FROM transactions 
                WHERE transaction_id = ?
                """,
                (transaction_id,),
            ) as cursor:
                row = await cursor.fetchone()

                if row is None:
                    return None

                return Transaction(
                    transaction_id=row[0],
                    mod_id=row[1],
                    description=row[2],
                    status=TransactionStatus(row[3]),
                    created_at=datetime.fromisoformat(row[4]),
                    committed_at=datetime.fromisoformat(row[5]) if row[5] else None,
                    rolled_back_at=datetime.fromisoformat(row[6]) if row[6] else None,
                )

    async def list_recent_transactions(
        self,
        limit: int = 50,
        status: TransactionStatus | None = None,
        mod_id: int | None = None,
    ) -> list[Transaction]:
        """
        Lista transacciones recientes con filtros opcionales.

        Args:
            limit: Máximo número de transacciones a retornar.
            status: Filtrar por estado (opcional).
            mod_id: Filtrar por mod_id (opcional).

        Returns:
            Lista de transacciones.
        """
        db = await self._ensure_connected()

        query = """
            SELECT transaction_id, mod_id, description, status, 
                   created_at, committed_at, rolled_back_at
            FROM transactions
        """
        params: list[Any] = []
        conditions: list[str] = []

        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)

        if mod_id is not None:
            conditions.append("mod_id = ?")
            params.append(mod_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with self._lock:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()

                transactions: list[Transaction] = []
                for row in rows:
                    transactions.append(
                        Transaction(
                            transaction_id=row[0],
                            mod_id=row[1],
                            description=row[2],
                            status=TransactionStatus(row[3]),
                            created_at=datetime.fromisoformat(row[4]),
                            committed_at=datetime.fromisoformat(row[5])
                            if row[5]
                            else None,
                            rolled_back_at=datetime.fromisoformat(row[6])
                            if row[6]
                            else None,
                        )
                    )

                return transactions

    # =========================================================================
    # OPERATION LOGGING
    # =========================================================================

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
        """
        Registrar el inicio de una operación.

        Args:
            agent_id: ID del agente que realiza la operación.
            operation_type: Tipo de operación.
            target_path: Path del archivo afectado.
            transaction_id: ID de transacción (usa la actual si es None).
            snapshot_path: Path al snapshot si existe.
            checksum: Checksum SHA256 del archivo original.
            metadata: Metadatos adicionales.

        Returns:
            ID de la entrada creada.

        Raises:
            JournalTransactionError: Si no hay transacción activa.
        """
        db = await self._ensure_connected()

        tx_id = transaction_id or self._current_transaction
        if tx_id is None:
            raise JournalTransactionError(
                "No active transaction. Call begin_transaction first."
            )

        async with self._lock:
            try:
                cursor = await db.execute(
                    """
                    INSERT INTO journal_entries 
                    (transaction_id, timestamp, agent_id, operation_type, 
                     target_path, status, snapshot_path, checksum, metadata)
                    VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx_id,
                        agent_id,
                        operation_type.value,
                        target_path,
                        OperationStatus.STARTED.value,
                        snapshot_path,
                        checksum,
                        json.dumps(metadata) if metadata else None,
                    ),
                )
                await db.commit()
                entry_id = cursor.lastrowid

                if entry_id is None:
                    raise JournalTransactionError("Failed to get entry ID after insert")

                logger.debug(
                    "Operation started",
                    extra={
                        "entry_id": entry_id,
                        "transaction_id": tx_id,
                        "operation_type": operation_type.value,
                        "target_path": target_path,
                        "agent_id": agent_id,
                    },
                )

                return entry_id

            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to log operation: {e}", transaction_id=tx_id
                ) from e

    async def complete_operation(self, entry_id: int) -> None:
        """
        Marcar una operación como completada.

        Args:
            entry_id: ID de la entrada a actualizar.
        """
        await self._update_status(entry_id, OperationStatus.COMPLETED)
        logger.debug("Operation completed", extra={"entry_id": entry_id})

    async def fail_operation(
        self, entry_id: int, error: str = "", metadata: dict[str, Any] | None = None
    ) -> None:
        """
        Marcar una operación como fallida.

        Args:
            entry_id: ID de la entrada a actualizar.
            error: Mensaje de error.
            metadata: Metadatos adicionales del error.
        """
        await self._update_status(entry_id, OperationStatus.FAILED)

        if metadata:
            db = await self._ensure_connected()
            async with self._lock:
                await db.execute(
                    """
                    UPDATE journal_entries 
                    SET metadata = json_set(COALESCE(metadata, '{}'), '$.error', ?)
                    WHERE id = ?
                    """,
                    (error, entry_id),
                )
                await db.commit()

        logger.error("Operation failed", extra={"entry_id": entry_id, "error": error})

    async def mark_rolled_back(self, entry_id: int, details: str | None = None) -> None:
        """
        Marcar una operación como revertida.

        Args:
            entry_id: ID de la entrada a actualizar.
            details: Detalles adicionales del rollback.
        """
        db = await self._ensure_connected()

        async with self._lock:
            await db.execute(
                """
                UPDATE journal_entries 
                SET status = ?, rolled_back = 1
                WHERE id = ?
                """,
                (OperationStatus.ROLLED_BACK.value, entry_id),
            )

            if details:
                await db.execute(
                    """
                    UPDATE journal_entries 
                    SET metadata = json_set(COALESCE(metadata, '{}'), '$.rollback_details', ?)
                    WHERE id = ?
                    """,
                    (details, entry_id),
                )

            await db.commit()

        logger.info(
            "Operation rolled back", extra={"entry_id": entry_id, "details": details}
        )

    async def _update_status(self, entry_id: int, status: OperationStatus) -> None:
        """Actualizar el estado de una operación."""
        db = await self._ensure_connected()

        async with self._lock:
            await db.execute(
                "UPDATE journal_entries SET status = ? WHERE id = ?",
                (status.value, entry_id),
            )
            await db.commit()

    # =========================================================================
    # QUERY OPERATIONS
    # =========================================================================

    async def get_operations_by_transaction(
        self, transaction_id: int
    ) -> list[JournalEntry]:
        """
        Obtener todas las operaciones de una transacción.

        Args:
            transaction_id: ID de la transacción.

        Returns:
            Lista de entradas del journal.
        """
        db = await self._ensure_connected()

        async with self._lock:
            async with db.execute(
                """
                SELECT id, timestamp, agent_id, operation_type, target_path, 
                       status, snapshot_path, checksum, metadata
                FROM journal_entries 
                WHERE transaction_id = ?
                ORDER BY timestamp ASC
                """,
                (transaction_id,),
            ) as cursor:
                rows = await cursor.fetchall()

                entries: list[JournalEntry] = []
                for row in rows:
                    entries.append(self._row_to_entry(row))

                return entries

    async def get_last_operation(
        self, agent_id: str, statuses: list[OperationStatus] | None = None
    ) -> JournalEntry | None:
        """
        Obtener la última operación de un agente.

        Args:
            agent_id: ID del agente.
            statuses: Lista de estados a buscar (por defecto: COMPLETED, FAILED)

        Returns:
            La última entrada encontrada o None.
        """
        if statuses is None:
            statuses = [OperationStatus.COMPLETED, OperationStatus.FAILED]

        db = await self._ensure_connected()

        status_values = [s.value for s in statuses]
        placeholders = ",".join("?" * len(status_values))

        async with self._lock:
            query = (
                "SELECT id, timestamp, agent_id, operation_type, target_path, "
                "status, snapshot_path, checksum, metadata "
                "FROM journal_entries "
                f"WHERE agent_id = ? AND status IN ({placeholders}) "  # nosec
                "ORDER BY timestamp DESC "
                "LIMIT 1"
            )
            async with db.execute(query, (agent_id, *status_values)) as cursor:
                row = await cursor.fetchone()

                if row is None:
                    return None

                return self._row_to_entry(row)

    async def get_operations_by_agent(
        self, agent_id: str, limit: int = 100
    ) -> list[JournalEntry]:
        """
        Obtener todas las operaciones de un agente.

        Args:
            agent_id: ID del agente.
            limit: Máximo número de entradas.

        Returns:
            Lista de entradas del journal.
        """
        db = await self._ensure_connected()

        async with self._lock:
            async with db.execute(
                """
                SELECT id, timestamp, agent_id, operation_type, target_path, 
                       status, snapshot_path, checksum, metadata
                FROM journal_entries 
                WHERE agent_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()

                entries: list[JournalEntry] = []
                for row in rows:
                    entries.append(self._row_to_entry(row))

                return entries

    async def get_operations_by_path(
        self, target_path: str, limit: int = 50
    ) -> list[JournalEntry]:
        """
        Obtener operaciones que afectaron un path específico.

        Args:
            target_path: Path del archivo.
            limit: Máximo número de entradas.

        Returns:
            Lista de entradas del journal.
        """
        db = await self._ensure_connected()

        async with self._lock:
            async with db.execute(
                """
                SELECT id, timestamp, agent_id, operation_type, target_path, 
                       status, snapshot_path, checksum, metadata
                FROM journal_entries 
                WHERE target_path = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (target_path, limit),
            ) as cursor:
                rows = await cursor.fetchall()

                entries: list[JournalEntry] = []
                for row in rows:
                    entries.append(self._row_to_entry(row))

                return entries

    # =========================================================================
    # MARK TRANSACTION ROLLED BACK
    # =========================================================================

    async def mark_transaction_rolled_back(self, transaction_id: int) -> None:
        """
        Marca una transacción completa como rolled back.

        Args:
            transaction_id: ID de la transacción.
        """
        db = await self._ensure_connected()

        async with self._lock:
            await db.execute(
                """
                UPDATE transactions 
                SET status = ?, rolled_back_at = datetime('now')
                WHERE transaction_id = ?
                """,
                (TransactionStatus.ROLLED_BACK.value, transaction_id),
            )
            await db.commit()

            logger.info(
                "Transaction marked as rolled back",
                extra={"transaction_id": transaction_id},
            )

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    @staticmethod
    def _row_to_entry(row: tuple) -> JournalEntry:
        """Convierte una fila de BD a JournalEntry."""
        return JournalEntry(
            id=row[0],
            timestamp=datetime.fromisoformat(row[1]),
            agent_id=row[2],
            operation_type=OperationType(row[3]),
            target_path=row[4],
            status=OperationStatus(row[5]),
            snapshot_path=row[6],
            checksum=row[7],
            metadata=json.loads(row[8]) if row[8] else None,
        )

    async def __aenter__(self) -> "OperationJournal":
        """Context manager entry."""
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Context manager exit."""
        await self.close()
