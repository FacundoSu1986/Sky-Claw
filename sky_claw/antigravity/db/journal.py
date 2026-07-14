"""
Sistema de Journaling para operaciones de archivos.
Implementación asíncrona usando aiosqlite.

Este módulo proporciona un registro durable de todas las operaciones
de archivos realizadas, permitiendo rollback completo de transacciones.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pathlib
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
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


class OperationType(StrEnum):
    """Tipos de operaciones journalizables."""

    MOD_INSTALL = "mod_install"
    MOD_UNINSTALL = "mod_uninstall"
    MOD_UPDATE = "mod_update"
    PLUGIN_CLEAN = "plugin_clean"
    FILE_CREATE = "file_create"
    FILE_MODIFY = "file_modify"
    FILE_DELETE = "file_delete"
    FILE_RENAME = "file_rename"


class OperationStatus(StrEnum):
    """Estados de una operación en el journal."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class TransactionStatus(StrEnum):
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

    def __init__(
        self,
        db_path: pathlib.Path | None = None,
        *,
        lifecycle=None,  # DatabaseLifecycleManager | None — evita import circular en runtime
    ) -> None:
        """
        Inicializa el journal.

        Args:
            db_path: Path al archivo de base de datos SQLite.
                     Si es None, usa un path por defecto.
            lifecycle: DatabaseLifecycleManager opcional (M-01 DI). Si es None,
                       se crea uno interno (backwards-compat).
        """
        raw_path = str(db_path or ".skyclaw_journal.db")
        from sky_claw.antigravity.core.validators.path import PathTraversalValidator

        validator = PathTraversalValidator(allow_absolute=True)
        result = validator.validate(raw_path)
        if not result.is_valid:
            raise ValueError(f"Path traversal detected in journal path '{raw_path}': {result.error_message}")

        self._db_path = pathlib.Path(raw_path)
        self._lifecycle = lifecycle  # DatabaseLifecycleManager | None
        self._owns_conn: bool = False  # True when we opened the connection directly (no lifecycle)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._current_transaction: int | None = None

    async def open(self) -> None:
        """Abre la conexión a la base de datos y crea el schema si es necesario.

        M-01: If a DatabaseLifecycleManager was injected, the connection is
        requested from it (WAL recovery + hardened pragmas already applied).
        Otherwise falls back to a direct ``aiosqlite.connect`` with manual
        pragmas (pre-M-01 behaviour), which avoids spawning a lifecycle that
        creates non-daemon aiosqlite worker threads — those block process exit
        on CPython 3.11 when callers omit ``close()`` (CI 20-minute timeout).
        """
        if self._db is not None:
            return

        try:
            if self._lifecycle is not None:
                # ----------------------------------------------------------
                # M-01 DI path — lifecycle owns and manages the connection
                # ----------------------------------------------------------
                self._owns_conn = False
                self._db = await self._lifecycle.get_connection(self._db_path)
            else:
                # ----------------------------------------------------------
                # Backwards-compat path — direct aiosqlite.connect (pre-M-01)
                # ----------------------------------------------------------
                # Does NOT create a DatabaseLifecycleManager internally.
                # Lifecycle managers spawn non-daemon aiosqlite worker threads;
                # on CPython 3.11 those threads block process exit when callers
                # omit close(), causing observed CI 20-minute timeouts.
                self._owns_conn = True
                self._db = await aiosqlite.connect(self._db_path)
                await self._db.execute("PRAGMA journal_mode=WAL")
                await self._db.execute("PRAGMA foreign_keys=ON")
                await self._db.execute("PRAGMA busy_timeout=5000")
                await self._db.execute("PRAGMA synchronous=NORMAL")

            # Schema is the same regardless of path
            await self._db.executescript(self._SCHEMA_SQL)
            logger.info("Journal database opened", extra={"db_path": str(self._db_path)})
        except sqlite3.Error as e:
            if self._db is not None:
                if self._owns_conn:
                    with contextlib.suppress(Exception):
                        await self._db.close()
                elif self._lifecycle is not None:
                    with contextlib.suppress(Exception):
                        self._lifecycle.evict_connection(self._db_path)
            self._db = None
            self._owns_conn = False
            raise JournalConnectionError(f"Failed to open journal database: {e}") from e

        # Mantenimiento best-effort: transacciones PENDING huérfanas de sesiones
        # anteriores (crash / excepción sin rollback) se barren al arrancar.
        try:
            await self.sweep_stale_pending()
        except (JournalError, sqlite3.Error):
            logger.warning(
                "Journal: startup sweep of stale PENDING transactions failed",
                exc_info=True,
            )

    async def close(self) -> None:
        """Cierra la conexión a la base de datos."""
        if self._db:
            if self._owns_conn:
                # Backwards-compat: we opened the connection directly, we close it.
                with contextlib.suppress(Exception):
                    await self._db.close()
                self._owns_conn = False
            # Si lifecycle es externo, no cerramos la conexión aquí;
            # el propietario (LifecycleContext) la cierra en shutdown_all().
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
                    raise JournalTransactionError("Failed to get transaction ID after insert")

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
                raise JournalTransactionError(f"Failed to begin transaction: {e}") from e

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

                logger.info("Transaction committed", extra={"transaction_id": transaction_id})

            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to commit transaction: {e}", transaction_id=transaction_id
                ) from e

    async def rollback_transaction(self, transaction_id: int) -> None:
        """
        Marca una transacción como rolled_back.

        Args:
            transaction_id: ID de la transacción a revertir.

        Raises:
            JournalTransactionError: Si la transacción no existe o ya fue procesada.
        """
        db = await self._ensure_connected()

        async with self._lock:
            try:
                cursor = await db.execute(
                    """
                    UPDATE transactions
                    SET status = ?, rolled_back_at = datetime('now')
                    WHERE transaction_id = ? AND status = ?
                    """,
                    (
                        TransactionStatus.ROLLED_BACK.value,
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

                logger.info("Transaction rolled back", extra={"transaction_id": transaction_id})

            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to roll back transaction: {e}", transaction_id=transaction_id
                ) from e

    @contextlib.asynccontextmanager
    async def transaction(
        self,
        description: str,
        mod_id: int | None = None,
        agent_id: str = "system",
    ) -> AsyncIterator[int]:
        """Context manager transaccional: commit en salida limpia, rollback si no.

        Garantiza que ninguna fila quede PENDING para siempre cuando una
        excepción escapa entre begin y commit (hardening jun-2026).
        """
        transaction_id = await self.begin_transaction(description, mod_id=mod_id, agent_id=agent_id)
        try:
            yield transaction_id
        except BaseException:
            try:
                await self.rollback_transaction(transaction_id)
            except JournalError:
                # Nunca enmascarar la excepción original del cuerpo.
                logger.error(
                    "Rollback of transaction %d failed during exception handling",
                    transaction_id,
                    exc_info=True,
                )
            raise
        else:
            await self.commit_transaction(transaction_id)

    async def sweep_stale_pending(self, max_age_hours: float = 24.0) -> int:
        """Marca ROLLED_BACK las transacciones PENDING más viejas que el umbral.

        Una fila PENDING que sobrevive de una sesión anterior es una transacción
        huérfana (crash o excepción sin rollback): nunca va a confirmarse.

        Args:
            max_age_hours: Antigüedad mínima (horas) para considerar huérfana.

        Returns:
            Cantidad de transacciones barridas.
        """
        db = await self._ensure_connected()

        async with self._lock:
            try:
                cursor = await db.execute(
                    """
                    UPDATE transactions
                    SET status = ?, rolled_back_at = datetime('now')
                    WHERE status = ? AND created_at < datetime('now', ?)
                    """,
                    (
                        TransactionStatus.ROLLED_BACK.value,
                        TransactionStatus.PENDING.value,
                        f"-{max_age_hours} hours",
                    ),
                )
                await db.commit()
            except sqlite3.Error as e:
                raise JournalTransactionError(f"Failed to sweep stale transactions: {e}") from e

        count = cursor.rowcount
        if count > 0:
            logger.warning(
                "Journal: %d stale PENDING transaction(s) swept to ROLLED_BACK (older than %.1fh)",
                count,
                max_age_hours,
            )
        return count

    async def get_transaction(self, transaction_id: int) -> Transaction | None:
        """
        Obtiene una transacción por su ID.

        Args:
            transaction_id: ID de la transacción.

        Returns:
            La transacción o None si no existe.
        """
        db = await self._ensure_connected()

        async with (
            self._lock,
            db.execute(
                """
                SELECT transaction_id, mod_id, description, status,
                       created_at, committed_at, rolled_back_at
                FROM transactions
                WHERE transaction_id = ?
                """,
                (transaction_id,),
            ) as cursor,
        ):
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

        async with self._lock, db.execute(query, params) as cursor:
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
                        committed_at=datetime.fromisoformat(row[5]) if row[5] else None,
                        rolled_back_at=datetime.fromisoformat(row[6]) if row[6] else None,
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
            raise JournalTransactionError("No active transaction. Call begin_transaction first.")

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
                raise JournalTransactionError(f"Failed to log operation: {e}", transaction_id=tx_id) from e

    async def complete_operation(self, entry_id: int) -> None:
        """
        Marcar una operación como completada.

        Args:
            entry_id: ID de la entrada a actualizar.
        """
        await self._update_status(entry_id, OperationStatus.COMPLETED)
        logger.debug("Operation completed", extra={"entry_id": entry_id})

    async def persist_action_manifest(
        self,
        manifest: Any,
        *,
        agent_id: str,
        transaction_id: int,
    ) -> int:
        """Persiste un ActionManifest (T-26, ADR 0002) como metadata de una operación.

        La "caja negra de vuelo" exige que el manifiesto de un Ritual mutante
        quede auditable: se guarda su ``model_dump(mode="json")`` en la columna
        ``metadata`` (reusando ``begin_operation``, no una tabla nueva) y se
        recupera con :meth:`get_operations_by_transaction` →
        ``ActionManifest.model_validate(entry.metadata)``.

        Args:
            manifest: Un ``ActionManifest`` (pydantic). Se toma como ``Any`` para
                no acoplar la capa DB al modelo del orquestador.
            agent_id: Agente que emite el manifiesto (el servicio del Ritual).
            transaction_id: Transacción activa del Ritual.

        Returns:
            El id de la entrada del journal donde quedó persistido.
        """
        primary_path = manifest.files_touched[0] if manifest.files_touched else manifest.ritual_id
        op_id = await self.begin_operation(
            agent_id=agent_id,
            operation_type=OperationType.FILE_MODIFY,
            target_path=str(primary_path),
            transaction_id=transaction_id,
            metadata=manifest.model_dump(mode="json"),
        )
        await self.complete_operation(op_id)
        logger.info(
            "ActionManifest persisted",
            extra={"entry_id": op_id, "ritual_id": manifest.ritual_id, "tool": manifest.tool},
        )
        return op_id

    async def persist_flight_report(
        self,
        report: Any,
        *,
        agent_id: str,
        transaction_id: int,
    ) -> int:
        """Persiste un FlightReport (T-28, ADR 0002) como metadata de una operación.

        El informe final de vuelo es la caja negra leída después del vuelo: se
        guarda su ``model_dump(mode="json")`` en la columna ``metadata``
        (reusando ``begin_operation``, igual que el manifiesto de T-26). Su
        clave ``kind="flight_report"`` viaja dentro del metadata y lo distingue
        del op del manifiesto en la misma transacción; el path de lectura es
        ``compose_flight_report_from_journal`` (orchestrator/preview).

        Args:
            report: Un ``FlightReport`` (pydantic). Se toma como ``Any`` para
                no acoplar la capa DB al modelo del orquestador.
            agent_id: Agente que emite el informe (el servicio del Ritual).
            transaction_id: Transacción del Ritual informado (puede estar ya
                committed: el informe se emite post-vuelo).

        Returns:
            El id de la entrada del journal donde quedó persistido.
        """
        # Espejo de persist_action_manifest: el primer files_touched como
        # target_path lo hace ubicable por get_operations_by_path / idx_journal_path
        # y consistente con el op del manifiesto; el informe degradado (sin
        # archivos) cae al ritual_id o a un marcador por transacción.
        primary_path = (
            report.files_touched[0]
            if report.files_touched
            else (report.ritual_id or f"flight-report-tx-{transaction_id}")
        )
        op_id = await self.begin_operation(
            agent_id=agent_id,
            operation_type=OperationType.FILE_MODIFY,
            target_path=str(primary_path),
            transaction_id=transaction_id,
            metadata=report.model_dump(mode="json"),
        )
        await self.complete_operation(op_id)
        logger.info(
            "FlightReport persisted",
            extra={"entry_id": op_id, "ritual_id": report.ritual_id, "tool": report.tool},
        )
        return op_id

    async def fail_operation(self, entry_id: int, error: str = "", metadata: dict[str, Any] | None = None) -> None:
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

    async def log_operation(
        self,
        agent_id: str,
        operation_type: str,
        file_path: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Compatibility method for older clients. Maps a standalone operation to a full transaction."""
        tx_id = await self.begin_transaction(
            description=f"Legacy operation: {operation_type}",
            agent_id=agent_id,
        )

        try:
            try:
                op_enum = OperationType(operation_type)
            except ValueError:
                op_enum = OperationType.FILE_MODIFY

            op_id = await self.begin_operation(
                agent_id=agent_id,
                operation_type=op_enum,
                target_path=file_path,
                transaction_id=tx_id,
                metadata=details,
            )
            await self.complete_operation(op_id)
            await self.commit_transaction(tx_id)
        except Exception:
            await self.mark_transaction_rolled_back(tx_id)
            raise

        return op_id

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

        logger.info("Operation rolled back", extra={"entry_id": entry_id, "details": details})

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

    async def get_operations_by_transaction(self, transaction_id: int) -> list[JournalEntry]:
        """
        Obtener todas las operaciones de una transacción.

        Args:
            transaction_id: ID de la transacción.

        Returns:
            Lista de entradas del journal.
        """
        db = await self._ensure_connected()

        async with (
            self._lock,
            db.execute(
                """
                SELECT id, timestamp, agent_id, operation_type, target_path,
                       status, snapshot_path, checksum, metadata
                FROM journal_entries
                WHERE transaction_id = ?
                ORDER BY timestamp ASC
                """,
                (transaction_id,),
            ) as cursor,
        ):
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

    async def get_operation_by_id(self, entry_id: int) -> JournalEntry | None:
        """
        Obtener una operación puntual por su ``entry_id``.

        A diferencia de :meth:`get_last_operation` (que resuelve por agente y es
        vulnerable a revertir una operación no relacionada), esto permite al
        RollbackManager deshacer exactamente la operación que falló.

        Args:
            entry_id: ID de la entrada del journal.

        Returns:
            La entrada encontrada o None.
        """
        db = await self._ensure_connected()

        async with (
            self._lock,
            db.execute(
                """
                SELECT id, timestamp, agent_id, operation_type, target_path,
                       status, snapshot_path, checksum, metadata
                FROM journal_entries
                WHERE id = ?
                """,
                (entry_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()

            if row is None:
                return None

            return self._row_to_entry(row)

    async def get_operations_by_agent(self, agent_id: str, limit: int = 100) -> list[JournalEntry]:
        """
        Obtener todas las operaciones de un agente.

        Args:
            agent_id: ID del agente.
            limit: Máximo número de entradas.

        Returns:
            Lista de entradas del journal.
        """
        db = await self._ensure_connected()

        async with (
            self._lock,
            db.execute(
                """
                SELECT id, timestamp, agent_id, operation_type, target_path,
                       status, snapshot_path, checksum, metadata
                FROM journal_entries
                WHERE agent_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ) as cursor,
        ):
            rows = await cursor.fetchall()

            entries: list[JournalEntry] = []
            for row in rows:
                entries.append(self._row_to_entry(row))

            return entries

    async def get_operations_by_path(self, target_path: str, limit: int = 50) -> list[JournalEntry]:
        """
        Obtener operaciones que afectaron un path específico.

        Args:
            target_path: Path del archivo.
            limit: Máximo número de entradas.

        Returns:
            Lista de entradas del journal.
        """
        db = await self._ensure_connected()

        async with (
            self._lock,
            db.execute(
                """
                SELECT id, timestamp, agent_id, operation_type, target_path,
                       status, snapshot_path, checksum, metadata
                FROM journal_entries
                WHERE target_path = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (target_path, limit),
            ) as cursor,
        ):
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
    def _row_to_entry(row: tuple[Any, ...]) -> JournalEntry:
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

    async def __aenter__(self) -> OperationJournal:
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


class NoOpJournal(OperationJournal):
    """Journal de operaciones que no realiza ninguna operación de escritura en DB.

    Útil para ejecuciones en sandbox donde no se quiere persistir el historial
    antes de la aprobación del operador.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def begin_transaction(self, description: str, mod_id: int | None = None, agent_id: str = "system") -> int:
        return 999999

    async def commit_transaction(self, transaction_id: int) -> None:
        pass

    async def rollback_transaction(self, transaction_id: int) -> None:
        pass

    async def begin_operation(
        self, transaction_id: int, operation_type: Any, target_path: str, agent_id: str, metadata: dict[str, Any] | None = None
    ) -> int:
        return 999999

    async def complete_operation(self, entry_id: int) -> None:
        pass

    async def fail_operation(self, entry_id: int, error: str = "", metadata: dict[str, Any] | None = None) -> None:
        pass

    async def log_operation(
        self,
        transaction_id: int,
        operation_type: Any,
        target_path: str,
        agent_id: str,
        status: Any = "started",
        snapshot_path: str | None = None,
        checksum: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        return 999999

    async def persist_action_manifest(self, transaction_id: int, action_manifest: Any) -> None:
        pass

    async def persist_flight_report(self, transaction_id: int, flight_report: Any) -> None:
        pass

    async def mark_rolled_back(self, entry_id: int, details: str | None = None) -> None:
        pass

    async def mark_transaction_rolled_back(self, transaction_id: int) -> None:
        pass


class StagingJournal(OperationJournal):
    """Journal que difiere el commit de la transacción en el journal real

    hasta que se confirma la promoción del sandbox.
    """

    def __init__(self, real_journal: OperationJournal) -> None:
        self._real_journal = real_journal
        self._staged_tx_id: int | None = None
        self._staged_commit = False

    async def open(self) -> None:
        # El real_journal ya debería estar abierto.
        pass

    async def close(self) -> None:
        pass

    async def begin_transaction(self, description: str, mod_id: int | None = None, agent_id: str = "system") -> int:
        tx_id = await self._real_journal.begin_transaction(
            description=description,
            mod_id=mod_id,
            agent_id=agent_id,
        )
        self._staged_tx_id = tx_id
        self._staged_commit = False
        return tx_id

    async def commit_transaction(self, transaction_id: int) -> None:
        if transaction_id == self._staged_tx_id:
            self._staged_commit = True
            logger.info("StagingJournal: commit diferido para la transacción %d", transaction_id)
        else:
            await self._real_journal.commit_transaction(transaction_id)

    async def rollback_transaction(self, transaction_id: int) -> None:
        await self._real_journal.rollback_transaction(transaction_id)

    async def mark_transaction_rolled_back(self, transaction_id: int) -> None:
        if transaction_id == self._staged_tx_id:
            self._staged_commit = False
        await self._real_journal.mark_transaction_rolled_back(transaction_id)

    async def begin_operation(self, *args: Any, **kwargs: Any) -> int:
        return await self._real_journal.begin_operation(*args, **kwargs)

    async def complete_operation(self, *args: Any, **kwargs: Any) -> None:
        await self._real_journal.complete_operation(*args, **kwargs)

    async def fail_operation(self, *args: Any, **kwargs: Any) -> None:
        await self._real_journal.fail_operation(*args, **kwargs)

    async def log_operation(self, *args: Any, **kwargs: Any) -> int:
        return await self._real_journal.log_operation(*args, **kwargs)

    async def persist_action_manifest(self, *args: Any, **kwargs: Any) -> None:
        await self._real_journal.persist_action_manifest(*args, **kwargs)

    async def persist_flight_report(self, *args: Any, **kwargs: Any) -> None:
        await self._real_journal.persist_flight_report(*args, **kwargs)

    async def mark_rolled_back(self, *args: Any, **kwargs: Any) -> None:
        await self._real_journal.mark_rolled_back(*args, **kwargs)

    async def commit_staged(self) -> None:
        """Confirma la transacción diferida en el journal real."""
        if self._staged_tx_id is not None and self._staged_commit:
            logger.info("StagingJournal: confirmando transacción diferida %d", self._staged_tx_id)
            await self._real_journal.commit_transaction(self._staged_tx_id)
            self._staged_tx_id = None
            self._staged_commit = False

    async def rollback_staged(self) -> None:
        """Revierte la transacción diferida en el journal real."""
        if self._staged_tx_id is not None:
            logger.info("StagingJournal: revirtiendo transacción diferida %d", self._staged_tx_id)
            await self._real_journal.mark_transaction_rolled_back(self._staged_tx_id)
            self._staged_tx_id = None
            self._staged_commit = False

