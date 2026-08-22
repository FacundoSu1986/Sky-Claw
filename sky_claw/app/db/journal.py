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
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import aiosqlite

from sky_claw.app.core.db_lifecycle import (
    DatabaseConnectionQuarantinedError,
    DatabaseLifecycleShuttingDownError,
)
from sky_claw.app.db.handoffs import (
    _INSERT_HANDOFF_SQL,
    _SELECT_HANDOFF_ACTIVO_SQL,
    HANDOFFS_SCHEMA_SQL,
    DeploymentHandoff,
    HandoffState,
    clave_canonica_de_path,
    fila_de_registro,
    handoff_desde_fila,
)

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


# Fallos SECUNDARIOS que un cleanup del journal absorbe (loguea) sin reemplazar
# la excepción PRIMARIA que lo disparó.
#
# Enunciado como propiedad del mecanismo, no como lista a recordar: el cleanup
# absorbe **todo rechazo de admisión del lifecycle**, y hoy
# ``lifecycle.transaction().__aenter__`` rechaza por exactamente dos motivos —
# shutdown (``_admit``) y cuarentena (``_resolve_connection``). Un tercer motivo
# que se agregue allá entra ACÁ, en un solo lugar, y por eso cubre de una vez a
# los dos sitios de cleanup del journal en vez de arreglar uno y dejar al gemelo.
# Esa desalineación es justo lo que produjo este bug: #475 enumeró el único
# rechazo que existía y #483 agregó el segundo sin tocar la enumeración.
#
# ``sqlite3.Error`` está por simetría OBSERVABLE entre los dos sitios:
# ``rollback_transaction`` ya envuelve los errores SQLite en
# ``JournalTransactionError``, pero ``mark_transaction_rolled_back`` no, así que
# sin él ``log_operation`` seguiría enmascarando la primaria donde
# ``transaction()`` no lo hace. El journal ya clasifica ese par como cleanup
# best-effort en el sweep de arranque (``except (JournalError, sqlite3.Error)``).
#
# Lo que deliberadamente NO está: ``BaseException``. Una cancelación NUEVA
# durante el cleanup debe seguir propagando con su identidad intacta.
_FALLOS_DE_CLEANUP_ABSORBIBLES: tuple[type[BaseException], ...] = (
    JournalError,
    sqlite3.Error,
    DatabaseLifecycleShuttingDownError,
    DatabaseConnectionQuarantinedError,
)


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

    _SCHEMA_SQL = (
        """
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
        + HANDOFFS_SCHEMA_SQL
    )

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
        from sky_claw.app.core.validators.path import PathTraversalValidator

        validator = PathTraversalValidator(allow_absolute=True)
        result = validator.validate(raw_path)
        if not result.is_valid:
            raise ValueError(f"Path traversal detected in journal path '{raw_path}': {result.error_message}")

        self._db_path = pathlib.Path(raw_path)
        self._lifecycle = lifecycle  # DatabaseLifecycleManager | None
        self._owns_conn: bool = False  # True when we opened the connection directly (no lifecycle)
        self._db: aiosqlite.Connection | None = None
        # Lock local, provisional: si hay lifecycle, ``open()`` lo reemplaza por
        # el lock compartido por path de ese manager. En el camino standalone
        # (``lifecycle=None``, conexión propia) este es el definitivo, y debe
        # seguir siendo independiente por instancia.
        self._lock = asyncio.Lock()
        self._current_transaction: int | None = None
        # F-002b (PR #493): IDs EXACTOS de TX PENDING que ``sweep_stale_pending``
        # convirtió a ROLLED_BACK en ESTE open. Se resetea al iniciar cada
        # open/sweep. Es la única ventana que le da al reconciler de arranque
        # acceso a evidencia ROLLED_BACK — la que acaba de convertir ÉL — sin
        # reintroducir historia arbitraria.
        self._swept_pending_transaction_ids_this_open: set[int] = set()

    @property
    def swept_pending_transaction_ids_this_open(self) -> frozenset[int]:
        """IDs exactos de PENDING barridas → ROLLED_BACK en el open actual.

        Read-only: la mutación es exclusiva de :meth:`sweep_stale_pending` (y el
        reset de ``open()``), que es el único actor que convierte esos estados.
        """
        return frozenset(self._swept_pending_transaction_ids_this_open)

    async def open(self) -> None:
        """Abre la conexión a la base de datos y crea el schema si es necesario.

        M-01: If a DatabaseLifecycleManager was injected, the connection is
        requested from it (WAL recovery + hardened pragmas already applied).
        Otherwise falls back to a direct ``aiosqlite.connect`` with manual
        pragmas (pre-M-01 behaviour), which avoids spawning a lifecycle that
        creates non-daemon aiosqlite worker threads — those block process exit
        on CPython 3.11 when callers omit ``close()`` (CI 20-minute timeout).

        **Invariante del lock de escritura.** Con lifecycle, el journal adopta
        acá el lock compartido por path de ese manager: *el lock acompaña a la
        conexión, no a la instancia*. Es la misma propiedad que ya sostiene
        ``AsyncModRegistry`` y la razón por la que ``get_write_lock`` cachea bajo
        la clave de path resuelto. Sin lifecycle la conexión es propia y el lock
        local se conserva, que es lo correcto: no hay nada que compartir.

        La inicialización del schema **corre adquiriendo** ese lock, no solo
        teniéndolo asignado: ``executescript`` commitea implícitamente lo que
        haya pendiente en la conexión compartida. El lock se suelta antes del
        sweep de arranque, que lo toma por su cuenta.
        """
        if self._db is not None:
            return

        try:
            if self._lifecycle is not None:
                # ----------------------------------------------------------
                # M-01 DI path — lifecycle owns and manages the connection
                # ----------------------------------------------------------
                self._owns_conn = False
                # El lock viaja con la conexión, no con la instancia. Las
                # conexiones del lifecycle son singleton por path resuelto, así
                # que dos `OperationJournal` sobre la misma DB comparten
                # conexión — y con un `asyncio.Lock` propio cada uno, sus
                # transacciones se intercalan sobre esa conexión compartida:
                # uno abre, el otro commitea lo que el primero llevaba a medias.
                # `get_write_lock` devuelve el lock cacheado bajo la MISMA clave
                # de path resuelto que la conexión, que es la propiedad que hace
                # falta; su docstring ya lo dice para `AsyncModRegistry`
                # ("a per-wrapper lock would leave the transaction-interleaving
                # race open") y acá aplica igual.
                #
                # Un solo rebind alcanza: los `async with self._lock` del módulo
                # leen el atributo en el momento de entrar, y todos resuelven la
                # conexión con `_ensure_connected()` —que llama a `open()`—
                # antes de tomarlo. Va acá, sin `await` de por medio, para que
                # ningún SQL posterior (empezando por el `executescript` del
                # schema y el sweep de arranque) corra bajo el lock equivocado.
                # Que el lock esté ASIGNADO no alcanza: el schema de abajo
                # además corre bajo el boundary del path.
                self._lock = self._lifecycle.get_write_lock(self._db_path)
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

            # Schema is the same regardless of path — pero el boundary no.
            #
            # `executescript` emite un COMMIT implícito de cualquier transacción
            # pendiente (documentado en `sqlite3`), así que un `open()` tardío
            # sobre la conexión compartida commiteaba el trabajo a medias de otro
            # journal que SÍ estaba sosteniendo el lock — y su `rollback()`
            # posterior ya no lo podía deshacer. PR-1b2b: el schema corre dentro
            # de `operation()`, que sostiene el boundary del path durante TODO el
            # DDL y además resuelve la conexión vigente.
            #
            # El boundary es solo el `executescript`, **no** todo `open()`:
            # `sweep_stale_pending()` obtiene su PROPIO `transaction()` más abajo
            # y `asyncio.Lock` no es reentrante, así que sostener el boundary
            # hasta el final de `open()` sería un deadlock garantizado en el
            # arranque.
            #
            # En el camino standalone la conexión es propia de esta instancia: no
            # hay un tercero que pueda commitear encima, así que no hace falta
            # boundary. Se usa `nullcontext` en vez de duplicar el
            # `executescript` en las dos ramas — dos call sites del mismo SQL son
            # exactamente el hermano desalineado que este repo colecciona.
            if self._lifecycle is not None:
                boundary: Any = self._lifecycle.operation(self._db_path)
            else:
                boundary = contextlib.nullcontext(self._db)
            async with boundary as db:
                if self._lifecycle is not None:
                    self._db = db
                await db.executescript(self._SCHEMA_SQL)
            logger.info("Journal database opened", extra={"db_path": str(self._db_path)})
        except sqlite3.Error as e:
            if self._db is not None and self._owns_conn:
                with contextlib.suppress(Exception):
                    await self._db.close()
            # Con lifecycle externo, el manager conserva ownership y
            # reintentara el cierre durante shutdown_all().
            self._db = None
            self._owns_conn = False
            raise JournalConnectionError(f"Failed to open journal database: {e}") from e

        # Mantenimiento best-effort: transacciones PENDING huérfanas de sesiones
        # anteriores (crash / excepción sin rollback) se barren al arrancar.
        # F-002b: el conjunto de IDs barridos pertenece a ESTE open — se resetea
        # acá y el sweep de abajo lo repuebla con los IDs que realmente convierte.
        self._swept_pending_transaction_ids_this_open = set()
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

        if self._lifecycle is not None:
            # PR-1b2b: el INSERT commitea en el boundary lifecycle. El estado
            # Python (_current_transaction) se actualiza SOLO después de que el
            # commit durable terminó: el código posterior al `async with` no
            # corre si transaction() falla o fue cancelado.
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    cursor = await conn.execute(
                        """
                        INSERT INTO transactions (mod_id, description, status)
                        VALUES (?, ?, ?)
                        """,
                        (mod_id, description, TransactionStatus.PENDING.value),
                    )
                    transaction_id = cursor.lastrowid

                    if transaction_id is None:
                        raise JournalTransactionError("Failed to get transaction ID after insert")

            except sqlite3.Error as e:
                raise JournalTransactionError(f"Failed to begin transaction: {e}") from e

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

        if self._lifecycle is not None:
            # El chequeo de rowcount va DENTRO del body: si no hay fila PENDING
            # que actualizar, la excepción dispara el rollback del boundary.
            # El estado Python se limpia SOLO después del commit durable.
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    cursor = await conn.execute(
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

                    if cursor.rowcount == 0:
                        raise JournalTransactionError(
                            f"Transaction {transaction_id} not found or not pending",
                            transaction_id=transaction_id,
                        )

            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to commit transaction: {e}", transaction_id=transaction_id
                ) from e

            if self._current_transaction == transaction_id:
                self._current_transaction = None

            logger.info("Transaction committed", extra={"transaction_id": transaction_id})

            return

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

        if self._lifecycle is not None:
            # PR-1b2b: el journal "rollback" es una MUTACIÓN normal (UPDATE de
            # status + commit); no confundir con el rollback SQLite que el
            # boundary hace ante errores. Estado Python post-commit.
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    cursor = await conn.execute(
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

                    if cursor.rowcount == 0:
                        raise JournalTransactionError(
                            f"Transaction {transaction_id} not found or not pending",
                            transaction_id=transaction_id,
                        )

            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to roll back transaction: {e}", transaction_id=transaction_id
                ) from e

            if self._current_transaction == transaction_id:
                self._current_transaction = None

            logger.info("Transaction rolled back", extra={"transaction_id": transaction_id})

            return

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
            except _FALLOS_DE_CLEANUP_ABSORBIBLES:
                # Nunca enmascarar la excepción original del cuerpo. Con el
                # boundary, un shutdown en curso —o un path en cuarentena—
                # rechaza la nueva admisión del rollback (fail-closed): la fila
                # PENDING puede sobrevivir y la barrerá sweep_stale_pending() en
                # el próximo arranque. Lo único inviolable acá es que la
                # excepción original salga por arriba (C2). La tupla no incluye
                # BaseException: una cancelación NUEVA durante el rollback debe
                # seguir propagando.
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

        F-002b: además registra en ``swept_pending_transaction_ids_this_open``
        los IDs EXACTOS de las filas realmente actualizadas pending →
        rolled_back en este barrido (``RETURNING``), para que el reconciler de
        arranque pueda tratar esa conversión recién hecha como evidencia del
        open actual sin reintoxicar con historia ROLLED_BACK arbitraria. El
        conjunto se resetea al iniciar cada sweep.

        Args:
            max_age_hours: Antigüedad mínima (horas) para considerar huérfana.

        Returns:
            Cantidad de transacciones barridas.
        """
        self._swept_pending_transaction_ids_this_open = set()
        db = await self._ensure_connected()

        # Un solo literal SQL para las dos ramas (defecto #1 del repo: dos
        # hermanos del mismo UPDATE terminan desalineados). RETURNING devuelve
        # los IDs realmente actualizados, en el mismo boundary que la mutación.
        sql = """
            UPDATE transactions
            SET status = ?, rolled_back_at = datetime('now')
            WHERE status = ? AND created_at < datetime('now', ?)
            RETURNING transaction_id
        """
        params = (
            TransactionStatus.ROLLED_BACK.value,
            TransactionStatus.PENDING.value,
            f"-{max_age_hours} hours",
        )

        if self._lifecycle is not None:
            # PR-1b2b: un solo boundary lifecycle para la mutación, sin tomar
            # self._lock (es el MISMO lock de path; asyncio.Lock no es reentrante).
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    cursor = await conn.execute(sql, params)
                    filas = [fila async for fila in cursor]
            except sqlite3.Error as e:
                raise JournalTransactionError(f"Failed to sweep stale transactions: {e}") from e
        else:
            async with self._lock:
                try:
                    cursor = await db.execute(sql, params)
                    filas = [fila async for fila in cursor]
                    await db.commit()
                except sqlite3.Error as e:
                    raise JournalTransactionError(f"Failed to sweep stale transactions: {e}") from e

        ids_barridos = {int(fila[0]) for fila in filas}
        self._swept_pending_transaction_ids_this_open.update(ids_barridos)
        count = len(ids_barridos)
        if count > 0:
            logger.warning(
                "Journal: %d stale PENDING transaction(s) swept to ROLLED_BACK (older than %.1fh)",
                count,
                max_age_hours,
            )
        return count

    # =========================================================================
    # HANDOFF DE DEPLOYMENT (D2, PR #493)
    # =========================================================================
    #
    # La invariante que esta sección protege (principio 5) es que el cierre
    # atómico "TX + handoff" pasa SIEMPRE por esta clase: nadie actualiza
    # ``transactions`` en crudo desde otro módulo, y ``_current_transaction``
    # (estado Python) sólo se limpia DESPUÉS del commit durable — igual que
    # ``commit_transaction``. Los tests lo anclan (T-D21).
    #
    # Estructura conforme al ancla PR-1b2b: los escritores públicos toman el
    # boundary DIRECTAMENTE (lifecycle.transaction o self._lock, ambas ramas
    # congeladas en test_pr_1b2b_boundary) y delegan los pasos SQL en helpers
    # que ASUMEN ese boundary (`_HELPERS_QUE_ASUMEN_BOUNDARY`), para no
    # duplicar las secuencias multi-paso en cuatro copias divergibles.

    @staticmethod
    async def _escribir_handoff_de_deployment(
        conn: aiosqlite.Connection,
        *,
        transaction_id: int,
        descripcion: str | None,
        registro: DeploymentHandoff,
        viejo: DeploymentHandoff | None,
    ) -> int:
        """Pasos SQL del cierre RUN 1/supersede; ASUME el boundary del caller."""
        cursor = await conn.execute(
            """
            UPDATE transactions
            SET description = COALESCE(?, description),
                status = ?, committed_at = datetime('now')
            WHERE transaction_id = ? AND status = ?
            """,
            (
                descripcion,
                TransactionStatus.COMMITTED.value,
                transaction_id,
                TransactionStatus.PENDING.value,
            ),
        )
        if cursor.rowcount == 0:
            raise JournalTransactionError(
                f"Transaction {transaction_id} not found or not pending",
                transaction_id=transaction_id,
            )
        if viejo is not None:
            cursor_v = await conn.execute(
                """
                UPDATE deployment_handoffs
                SET state = ?, superseded_at = datetime('now'), updated_at = datetime('now')
                WHERE handoff_id = ? AND state IN ('awaiting_deployment','indeterminate','superseding')
                """,
                (HandoffState.SUPERSEDED.value, viejo.handoff_id),
            )
            if cursor_v.rowcount == 0:
                raise JournalTransactionError(
                    f"Handoff {viejo.handoff_id} ya no está activo: no se puede superseder",
                    transaction_id=transaction_id,
                )
        cursor_i = await conn.execute(_INSERT_HANDOFF_SQL, fila_de_registro(registro))
        nuevo_id = cursor_i.lastrowid
        if nuevo_id is None:
            raise JournalTransactionError("Failed to get handoff ID after insert", transaction_id=transaction_id)
        if viejo is not None:
            await conn.execute(
                "UPDATE deployment_handoffs SET superseded_by = ? WHERE handoff_id = ?",
                (nuevo_id, viejo.handoff_id),
            )
        return nuevo_id

    @staticmethod
    async def _escribir_resume_completado(
        conn: aiosqlite.Connection,
        *,
        transaction_id: int,
        handoff_id: int,
    ) -> None:
        """Pasos SQL del resume exitoso; ASUME el boundary del caller."""
        cursor = await conn.execute(
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
        if cursor.rowcount == 0:
            raise JournalTransactionError(
                f"Transaction {transaction_id} not found or not pending",
                transaction_id=transaction_id,
            )
        cursor_h = await conn.execute(
            """
            UPDATE deployment_handoffs
            SET state = ?, completed_at = datetime('now'), updated_at = datetime('now')
            WHERE handoff_id = ? AND state = ?
            """,
            (HandoffState.COMPLETED.value, handoff_id, HandoffState.AWAITING_DEPLOYMENT.value),
        )
        if cursor_h.rowcount == 0:
            raise JournalTransactionError(
                f"Handoff {handoff_id} not found or not awaiting_deployment",
                transaction_id=transaction_id,
            )

    @staticmethod
    async def _escribir_transicion_de_handoff(
        conn: aiosqlite.Connection,
        *,
        sql: str,
        valores: tuple[object, ...],
    ) -> int:
        """Ejecuta la transición guardada; ASUME el boundary del caller."""
        cursor = await conn.execute(sql, valores)
        return cursor.rowcount

    @staticmethod
    async def _escribir_handoff_indeterminado(
        conn: aiosqlite.Connection,
        *,
        registro: DeploymentHandoff,
    ) -> int | None:
        """INSERT del INDETERMINATE; ASUME el boundary del caller. Levanta
        ``sqlite3.IntegrityError`` si ya hay owner activo del artifact."""
        cursor = await conn.execute(_INSERT_HANDOFF_SQL, fila_de_registro(registro))
        return int(cursor.lastrowid or 0) or None

    async def consultar_handoff_activo(self, artifact_path: str) -> DeploymentHandoff | None:
        """El handoff ACTIVO del artifact físico canónico, o ``None``.

        El índice único parcial garantiza a lo sumo una fila activa por
        ``artifact_path``; ``LIMIT 1`` es defensa en profundidad, no la garantía.
        """
        db = await self._ensure_connected()
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(_SELECT_HANDOFF_ACTIVO_SQL, (artifact_path,)) as cursor,
            ):
                fila = await cursor.fetchone()
        else:
            async with self._lock, db.execute(_SELECT_HANDOFF_ACTIVO_SQL, (artifact_path,)) as cursor:
                fila = await cursor.fetchone()
        return handoff_desde_fila(fila) if fila is not None else None

    async def crear_handoff_de_deployment(
        self,
        transaction_id: int,
        *,
        descripcion: str | None,
        registro: DeploymentHandoff,
        viejo: DeploymentHandoff | None = None,
    ) -> int:
        """Cierra la TX y crea el handoff en UN boundary SQLite (RUN 1 / supersede).

        Orden interno (principio 9 para el supersede):

        1. ``transactions`` → COMMITTED (con descripción estrechada si se pasó);
        2. si ``viejo``, ``deployment_handoffs`` → SUPERSEDED (sin ``superseded_by``);
        3. INSERT del handoff nuevo;
        4. si ``viejo``, ``viejo.superseded_by = nuevo``.

        Cualquier guard que falle revierte el boundary entero (con lifecycle) o
        deja la conexión sin commit (standalone): nunca queda una TX
        COMMITTED sin su handoff ni una historia partida. El estado Python
        ``_current_transaction`` se limpia sólo después del commit durable.
        """
        db = await self._ensure_connected()
        if self._lifecycle is not None:
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    nuevo_id = await self._escribir_handoff_de_deployment(
                        conn,
                        transaction_id=transaction_id,
                        descripcion=descripcion,
                        registro=registro,
                        viejo=viejo,
                    )
            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to commit transaction with deployment handoff: {e}",
                    transaction_id=transaction_id,
                ) from e
        else:
            async with self._lock:
                try:
                    nuevo_id = await self._escribir_handoff_de_deployment(
                        db,
                        transaction_id=transaction_id,
                        descripcion=descripcion,
                        registro=registro,
                        viejo=viejo,
                    )
                    await db.commit()
                except sqlite3.Error as e:
                    raise JournalTransactionError(
                        f"Failed to commit transaction with deployment handoff: {e}",
                        transaction_id=transaction_id,
                    ) from e

        if self._current_transaction == transaction_id:
            self._current_transaction = None
        logger.info(
            "Transaction committed with deployment handoff",
            extra={"transaction_id": transaction_id, "handoff_id": nuevo_id},
        )
        return nuevo_id

    async def completar_handoff_de_resume(self, transaction_id: int, handoff_id: int) -> None:
        """Cierra un resume exitoso en UN boundary: TX2 → COMMITTED + handoff → COMPLETED.

        PRECONDICIÓN del caller (principio 10): el filesystem ya está sellado
        (todos los DirectoryRollback del éxito confirmados) ANTES de llamar acá.
        Nunca queda una TX committeada sin su handoff completado, ni al revés.
        """
        db = await self._ensure_connected()
        if self._lifecycle is not None:
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    await self._escribir_resume_completado(conn, transaction_id=transaction_id, handoff_id=handoff_id)
            except sqlite3.Error as e:
                raise JournalTransactionError(
                    f"Failed to complete deployment handoff: {e}", transaction_id=transaction_id
                ) from e
        else:
            async with self._lock:
                try:
                    await self._escribir_resume_completado(db, transaction_id=transaction_id, handoff_id=handoff_id)
                    await db.commit()
                except sqlite3.Error as e:
                    raise JournalTransactionError(
                        f"Failed to complete deployment handoff: {e}", transaction_id=transaction_id
                    ) from e

        if self._current_transaction == transaction_id:
            self._current_transaction = None
        logger.info(
            "Deployment handoff completed",
            extra={"transaction_id": transaction_id, "handoff_id": handoff_id},
        )

    async def transicionar_handoff(
        self,
        handoff_id: int,
        *,
        desde: HandoffState,
        hacia: HandoffState,
        **campos: object,
    ) -> bool:
        """Transición guardada ``desde → hacia`` con columnas extra opcionales.

        El ``WHERE state = desde`` es la guarda: si el estado real difiere
        (concurrencia, reconciler previo), devuelve ``False`` sin mutar. Los
        ``campos`` son nombres de columna INTERNOS de este módulo — nunca input
        externo — y se parametrizan por posición.
        """
        columnas = ["state = ?", "updated_at = datetime('now')"]
        valores: list[object] = [hacia.value]
        for nombre, valor in campos.items():
            columnas.append(f"{nombre} = ?")
            valores.append(valor)
        valores.append(handoff_id)
        valores.append(desde.value)
        sql = f"UPDATE deployment_handoffs SET {', '.join(columnas)} WHERE handoff_id = ? AND state = ?"
        db = await self._ensure_connected()
        if self._lifecycle is not None:
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    tocadas = await self._escribir_transicion_de_handoff(conn, sql=sql, valores=tuple(valores))
            except sqlite3.Error as e:
                raise JournalTransactionError(f"Failed to transition handoff: {e}") from e
        else:
            async with self._lock:
                try:
                    tocadas = await self._escribir_transicion_de_handoff(db, sql=sql, valores=tuple(valores))
                    await db.commit()
                except sqlite3.Error as e:
                    raise JournalTransactionError(f"Failed to transition handoff: {e}") from e
        return tocadas == 1

    async def registrar_handoff_indeterminado(self, *, registro: DeploymentHandoff) -> int | None:
        """INSERT de un INDETERMINATE conservador (reconciler de arranque).

        Nunca compite: si ya existe un owner activo del artifact físico, el
        índice único parcial rechaza el INSERT y se devuelve ``None`` en vez de
        pisar a nadie. El registro NO puede traer identidad esperada (CHECK).
        """
        db = await self._ensure_connected()
        if self._lifecycle is not None:
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    return await self._escribir_handoff_indeterminado(conn, registro=registro)
            except sqlite3.IntegrityError:
                logger.warning(
                    "Handoff INDETERMINATE para '%s' no insertado: ya hay un owner activo",
                    registro.artifact_path,
                )
                return None
            except sqlite3.Error as e:
                raise JournalTransactionError(f"Failed to register indeterminate handoff: {e}") from e
        async with self._lock:
            try:
                nuevo_id = await self._escribir_handoff_indeterminado(db, registro=registro)
                await db.commit()
                return nuevo_id
            except sqlite3.IntegrityError:
                logger.warning(
                    "Handoff INDETERMINATE para '%s' no insertado: ya hay un owner activo",
                    registro.artifact_path,
                )
                return None
            except sqlite3.Error as e:
                raise JournalTransactionError(f"Failed to register indeterminate handoff: {e}") from e

    async def transacciones_que_nombran(self, objetivo: str, ids_extra: Iterable[int] = ()) -> list[int]:
        """IDs de transacciones cuya ActionManifest nombra ``objetivo``, acotadas
        a EVIDENCIA VIGENTE (F-002b).

        Devuelve las TX PENDING que nombran el artifact más, si el caller los
        pasa, los ``ids_extra`` — los IDs EXACTOS que ``sweep_stale_pending``
        barrió en ESTE open (pendientes huérfanas de la sesión anterior,
        convertidas a ROLLED_BACK justo ahora). Cualquier OTRO ROLLED_BACK —
        historia vieja, incluso con un COMMITTED posterior— NO es evidencia por
        sí mismo: reintroducirlo intoxicaba artifacts legítimos actuales con
        un INDETERMINATE falso.

        Evidencia de reconciliación (F-002): el manifiesto es PRE-mutación, así
        que esta consulta prueba intención, no provenance — el caller sólo puede
        fabricar INDETERMINATE con ella, nunca identidad autorizada.

        R-006: la comparación de paths se hace sobre la forma canónica
        link-safe (:func:`~sky_claw.app.db.handoffs.clave_canonica_de_path`) de
        AMBOS lados — el entry histórico del manifest y el objetivo — para que
        dos representaciones del mismo path no puedan producir un hit en el
        lookup del handoff y un miss acá (separador de cola, caso Windows,
        variantes sintácticas resolubles). El manifiesto no se reescribe: los
        strings originales quedan intactos.

        El filtro de rutas se hace en Python sobre el JSON de ``metadata``: un
        ``LIKE`` sobre texto JSON sería ciego a los escapes de backslash de las
        rutas Windows.
        """
        db = await self._ensure_connected()
        sql = """
            SELECT je.transaction_id, t.status, je.metadata
            FROM journal_entries je
            JOIN transactions t ON t.transaction_id = je.transaction_id
            WHERE je.metadata IS NOT NULL AND t.status IN ('pending', 'rolled_back')
        """
        if self._lifecycle is not None:
            async with self._lifecycle.operation(self._db_path) as conn, conn.execute(sql) as cursor:
                filas = [fila async for fila in cursor]
        else:
            async with self._lock, db.execute(sql) as cursor:
                filas = [fila async for fila in cursor]

        extra = set(ids_extra)
        coincidencias: set[int] = set()
        objetivo_canonico = clave_canonica_de_path(objetivo)
        prefijo = objetivo_canonico + ("\\" if "\\" in objetivo_canonico else "/")
        for tx_id, status, metadata in filas:
            if status != TransactionStatus.PENDING.value and int(tx_id) not in extra:
                continue
            try:
                files_touched = json.loads(metadata).get("files_touched") or []
            except (TypeError, json.JSONDecodeError):
                continue
            for ruta in files_touched:
                canonica = clave_canonica_de_path(str(ruta))
                if canonica == objetivo_canonico or canonica.startswith(prefijo):
                    coincidencias.add(int(tx_id))
                    break
        return sorted(coincidencias)

    async def get_transaction(self, transaction_id: int) -> Transaction | None:
        """
        Obtiene una transacción por su ID.

        Args:
            transaction_id: ID de la transacción.

        Returns:
            La transacción o None si no existe.
        """
        db = await self._ensure_connected()

        sql = """
            SELECT transaction_id, mod_id, description, status,
                   created_at, committed_at, rolled_back_at
            FROM transactions
            WHERE transaction_id = ?
            """
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, (transaction_id,)) as cursor,
            ):
                row = await cursor.fetchone()
        else:
            async with self._lock, db.execute(sql, (transaction_id,)) as cursor:
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

        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(query, params) as cursor,
            ):
                rows = await cursor.fetchall()
        else:
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

        if self._lifecycle is not None:
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    cursor = await conn.execute(
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
                    entry_id = cursor.lastrowid

                    if entry_id is None:
                        raise JournalTransactionError("Failed to get entry ID after insert")

            except sqlite3.Error as e:
                raise JournalTransactionError(f"Failed to log operation: {e}", transaction_id=tx_id) from e

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

    @staticmethod
    def _metadata_previa_fusionable(entry_id: int, crudo: Any) -> dict[str, Any]:
        """La metadata ya presente en la fila, o ``{}`` si no se puede fusionar.

        Es deliberadamente TOTAL: no propaga. Una fila cuya metadata no se puede
        leer no puede impedir que se registre un fallo nuevo — el error que
        :meth:`fail_operation` está intentando persistir tiene prioridad sobre el
        defecto de la metadata vieja. Los tres motivos de descarte:

        - ``json_invalido``: la columna no parsea (fila truncada, texto plano, o
          un tipo que ``json.loads`` no acepta).
        - ``raiz_no_objeto``: parsea, pero la raíz es ``[]`` / ``"texto"`` / un
          número / ``null``. No hay claves que fusionar.
        - ``no_reserializable``: es un objeto, pero no sobrevive a
          ``allow_nan=False``. ``json.loads`` **acepta** los literales
          ``NaN``/``Infinity`` que ``json.dumps`` estricto no emite, así que una
          fila legacy puede llegar acá con un ``float('nan')`` adentro y hacer
          fallar la re-serialización de la fusión.
        - ``limite_de_recursion``: la estructura es demasiado profunda para el
          parser o el serializador. ``RecursionError`` hereda de ``RuntimeError``,
          **no** de ``ValueError``/``TypeError``, así que sin nombrarlo explícito
          se escapaba del clasificador y hundía el reporte del fallo — justo lo
          que este método promete que no pasa. Se nombra en vez de ensanchar el
          ``except``: un ``BaseException`` acá se tragaría la cancelación.

        El WARNING no loguea el contenido descartado: puede traer rutas, nombres
        de perfil de MO2 o nombres de mod. Sale qué fila y por qué.

        La degradación cubre **solo** esta metadata previa. La del argumento se
        mantiene estricta y propaga, igual que en :meth:`begin_operation`: ahí no
        hay dato preexistente que rescatar, y tragarse un argumento inválido
        ocultaría un bug del caller.
        """
        if not crudo:
            return {}

        motivo = "json_invalido"
        try:
            valor = json.loads(crudo)
        except RecursionError:
            motivo = "limite_de_recursion"
        except (ValueError, TypeError):
            pass
        else:
            if not isinstance(valor, dict):
                motivo = "raiz_no_objeto"
            else:
                try:
                    json.dumps(valor, allow_nan=False)
                except RecursionError:
                    motivo = "limite_de_recursion"
                except ValueError:
                    motivo = "no_reserializable"
                else:
                    return valor

        logger.warning(
            "Metadata previa ilegible descartada al registrar el fallo de la operación",
            extra={
                "event": "journal_metadata_previa_descartada",
                "entry_id": entry_id,
                "motivo": motivo,
            },
        )
        return {}

    async def _retirar_conexion_propia(self, db: aiosqlite.Connection) -> None:
        """Saca de circulación la conexión PROPIA que quedó con la transacción abierta.

        Se invoca **solo** cuando el ``rollback`` del camino standalone falló: ahí
        la conexión conserva el estado parcial y cualquier escritor posterior lo
        commitea junto con su propio trabajo. Reproducido: un
        ``complete_operation`` de una operación **no relacionada** dejaba durable
        el ``FAILED`` sin evidencia de la primera.

        Cerrar la conexión aborta la transacción, y soltar la referencia hace que
        el próximo :meth:`_ensure_connected` reabra una limpia. Es best-effort y
        nunca eclipsa el fallo original: el llamador ya está propagando la
        excepción primaria, que es la que el consumidor necesita ver.

        **Lo que no puede saltearse es soltar la referencia**, porque es el paso
        que impide la reutilización. El cierre es un ``await`` y puede no
        completar nunca si hay una cancelación pendiente; si queda a medias el fd
        sobrevive hasta el GC, que es peor que un cierre limpio pero
        incomparablemente mejor que dejar que el siguiente escritor commitee el
        estado parcial.

        Eso está protegido DOS veces, y son redundantes a propósito: la
        referencia se suelta antes del ``await``, y el cierre va bajo
        ``suppress(BaseException)`` para que tampoco importe si se invierte el
        orden. Verificado por mutación: quitar una sola de las dos no rompe nada
        —cada una alcanza—, y quitar ambas tumba
        ``test_cancelacion_durante_el_cierre_igual_suelta_la_referencia``.

        El orden es, además, el inverso al de ``_invalidate_under_boundary`` del
        lifecycle —que cierra y después muta el registro— y las dos elecciones son
        correctas para su dueño: ahí la conexión es COMPARTIDA, retener la entrada
        es lo que permite que ``shutdown_all()`` reintente el cierre, y retirarla
        antes dejaría un fd vivo sin dueño. Acá la conexión es propia y no hay
        nadie que vaya a reintentar.

        **Solo standalone, y a propósito.** Con lifecycle, ``transaction()`` ya
        cierra la conexión y deja el path fail-closed vía
        ``_invalidate_under_boundary`` (cerrar primero, mutar el registro después,
        conservando ownership si el cierre no se confirma); duplicarlo acá
        competiría con esa política. Tampoco se usa ``evict_connection``: está
        congelada sin callers de producción y acá no hay lifecycle al que
        pedírsela — la conexión es nuestra.
        """
        # Guard de identidad: sostenemos el lock, pero no cuesta nada y evita
        # retirar la conexión de otro dueño si esto se llamara desde otro sitio.
        if self._db is db:
            self._db = None
            self._owns_conn = False

        # Best-effort, y DESPUÉS de soltar la referencia. `BaseException` para que
        # una cancelación pendiente no impida el WARNING de abajo; no se absorbe
        # nada que importe: el llamador ya está propagando su excepción y, si la
        # cancelación es para esta tarea, el `raise` del llamador la entrega.
        with contextlib.suppress(BaseException):
            await db.close()

        logger.warning(
            "Conexión propia del journal retirada tras un rollback fallido; se reabrirá en el próximo uso",
            extra={
                "event": "journal_conexion_propia_retirada",
                "db_path": str(self._db_path),
            },
        )

    async def _escribir_fallo(
        self,
        conn: aiosqlite.Connection,
        entry_id: int,
        evidencia: dict[str, Any],
    ) -> None:
        """Emite el estado FAILED y su evidencia. ASUME el boundary sostenido.

        No abre transacción ni commitea: eso es del llamador, y es lo que hace
        que ambos ``UPDATE`` caigan en la MISMA unidad. Un solo cuerpo para las
        dos ramas de :meth:`fail_operation` — dos copias del mismo SQL son
        exactamente el hermano desalineado que este repo colecciona.

        El ``UPDATE`` del estado va acá y **no** delegado en
        :meth:`_update_status`: ese método abre su propio boundary, y
        ``asyncio.Lock`` no es reentrante — anidarlo cuelga la operación para
        siempre en vez de propagar.
        """
        await conn.execute(
            "UPDATE journal_entries SET status = ? WHERE id = ?",
            (OperationStatus.FAILED.value, entry_id),
        )

        if not evidencia:
            return

        # La previa se relee DENTRO del boundary, no antes: leerla afuera
        # reintroduce read → await → stale, y otro escritor podría haber
        # cambiado la columna en el medio.
        async with conn.execute(
            "SELECT metadata FROM journal_entries WHERE id = ?",
            (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            # Entrada inexistente: el UPDATE de arriba tampoco afectó filas.
            return

        previa = self._metadata_previa_fusionable(entry_id, row[0])
        await conn.execute(
            "UPDATE journal_entries SET metadata = ? WHERE id = ?",
            (json.dumps({**previa, **evidencia}, allow_nan=False), entry_id),
        )

    async def fail_operation(self, entry_id: int, error: str = "", metadata: dict[str, Any] | None = None) -> None:
        """
        Marcar una operación como fallida, conservando su evidencia.

        La evidencia final de la fila es::

            metadata_previa + metadata (argumento) + {"error": error} si error != ""

        **Precedencia.** El argumento ``metadata`` pisa las claves homónimas de
        la metadata previa: es información más nueva sobre la misma operación. La
        clave ``error`` es **canónica del parámetro ``error``** y se aplica al
        final, así que un ``metadata={"error": ...}`` no puede sustituirla en
        silencio. Un ``error`` vacío **no** crea la clave: ausencia de mensaje y
        mensaje vacío no son lo mismo.

        La fusión se resuelve en Python y **no** con ``json_patch`` a propósito:
        RFC 7396 borra las claves cuyo valor es ``null``, que sería la misma
        pérdida de evidencia que este método debe evitar.

        **Atomicidad.** Estado y evidencia son UNA transición: un solo boundary,
        un solo commit. Antes eran dos —``_update_status`` commiteaba y recién
        después se tocaba la metadata—, y quedaba una fila ``FAILED`` con la
        evidencia ausente de forma durable si algo caía en el medio.

        **La cancelación, el rollback y la cuarentena NO se manejan acá.** Con
        lifecycle, ``transaction()`` ya completa el rollback aunque cancelen al
        llamador, conserva la identidad de la cancelación, preserva la excepción
        primaria y pone el path en cuarentena si el cierre no se confirma.
        Reimplementar eso en este método competiría con esa política; el camino
        standalone —conexión propia, sin lifecycle que coordine— sí necesita su
        rollback explícito.

        Args:
            entry_id: ID de la entrada a actualizar.
            error: Mensaje de error. Vacío = no se registra la clave ``error``.
            metadata: Metadatos adicionales del error. Se fusionan con la
                metadata previa de la fila.

        Raises:
            sqlite3.Error: Si falla la escritura o el commit. La transacción se
                deshace antes de propagar; no queda estado parcial.
            ValueError: Si ``metadata`` contiene ``NaN``/``Infinity``, y
                ``TypeError`` si contiene un valor que ``json.dumps`` no sabe
                serializar. Mismo criterio estricto que ``begin_operation`` para
                el dato que aporta el caller.
        """
        # La evidencia se compone ANTES de tocar la DB: es cálculo puro y no
        # tiene por qué ocurrir con el boundary tomado.
        evidencia: dict[str, Any] = dict(metadata) if metadata else {}
        if error:
            evidencia["error"] = error

        await self._ensure_connected()

        if self._lifecycle is not None:
            async with self._lifecycle.transaction(self._db_path) as conn:
                await self._escribir_fallo(conn, entry_id, evidencia)
        else:
            await self._escribir_fallo_standalone(entry_id, evidencia)

        logger.error("Operation failed", extra={"entry_id": entry_id, "error": error})

    async def _escribir_fallo_standalone(self, entry_id: int, evidencia: dict[str, Any]) -> None:
        """Rama sin lifecycle: lock local, un commit, y rollback explícito.

        **La conexión se relee DENTRO del lock, y el reintento es load-bearing.**
        Resolverla afuera y usarla adentro reintroduce resolve → await → stale —
        la misma regla que enuncia ``DatabaseLifecycleManager.operation()``—, y
        acá no es teórica: desde que un rollback fallido RETIRA la conexión, un
        ``fail_operation`` concurrente que la hubiera resuelto antes del lock
        entra con una conexión ya cerrada, propaga "no active connection" y deja
        SU operación en ``STARTED`` sin evidencia. Reproducido con dos
        ``fail_operation`` concurrentes.

        Pero ``_ensure_connected()`` **no** puede llamarse con el lock tomado:
        deriva en ``open()``, que termina en ``sweep_stale_pending()``, que pide
        el mismo lock — y ``asyncio.Lock`` no es reentrante. Es el deadlock que el
        docstring de ``open()`` ya advierte. Así que el ciclo es: reabrir AFUERA,
        verificar ADENTRO, y reintentar si la retiraron en el medio. Dos intentos
        alcanzan porque sólo se vuelve a iterar cuando la conexión fue retirada, y
        reabrir la instala; el tope evita convertir un fallo repetido en un bucle.
        """
        for _ in range(2):
            await self._ensure_connected()
            async with self._lock:
                # Lectura fresca bajo el lock: `self._db`, no el valor que
                # devolvió `_ensure_connected()` antes de esperarlo.
                db = self._db
                if db is None:
                    # Retirada entre el reintento y el lock: reabrir y repetir.
                    continue
                try:
                    await self._escribir_fallo(db, entry_id, evidencia)
                    await db.commit()
                except BaseException as fallo_primario:
                    # Soltar el lock con la transacción abierta dejaría que el
                    # siguiente escritor commitee el estado parcial que este
                    # boundary descarta. Hay TRES desenlaces y cada uno tiene su
                    # respuesta; el invariante común es que la conexión no queda
                    # instalada si el rollback no se pudo confirmar.
                    rollback_confirmado = False
                    try:
                        await db.rollback()
                        rollback_confirmado = True
                    except Exception:
                        # Invariante de `app/db/AGENTS.md`: "un rollback fallido
                        # conserva evidencia y no se reporta como éxito". Se
                        # loguea porque el `raise` de abajo propaga la excepción
                        # PRIMARIA —la que el caller necesita— y ésta quedaría
                        # invisible.
                        logger.exception(
                            "El rollback de fail_operation falló; la transacción puede haber quedado sucia",
                            extra={"entry_id": entry_id},
                        )
                    except BaseException as fallo_de_cleanup:
                        # Cancelación NUEVA durante el rollback (o cualquier otra
                        # BaseException). NO se absorbe —conserva su identidad y se
                        # re-lanza— pero tampoco puede saltear la limpieza: sin
                        # esto, la conexión quedaba instalada con la transacción
                        # abierta y el siguiente escritor commiteaba el FAILED
                        # parcial. Reproducido con `complete_operation` de una
                        # operación no relacionada.
                        #
                        # La primaria viaja como `__cause__` y no solo como
                        # `__context__`: es el mismo criterio que
                        # `_rollback_after_failure` del lifecycle, que también
                        # hace `raise final_cancellation from diagnostic_error`.
                        await self._retirar_conexion_propia(db)
                        raise fallo_de_cleanup from fallo_primario
                    if not rollback_confirmado:
                        # Loguear y soltar el lock NO alcanza: la transacción
                        # sigue abierta sobre la conexión y el siguiente escritor
                        # la commitea junto con su trabajo. Se retira.
                        await self._retirar_conexion_propia(db)
                    raise
                return

        raise JournalConnectionError(
            f"La conexión del journal fue retirada dos veces consecutivas al "
            f"registrar el fallo de la operación {entry_id}"
        )

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
            try:
                await self.mark_transaction_rolled_back(tx_id)
            except _FALLOS_DE_CLEANUP_ABSORBIBLES:
                # Mismo contrato que transaction(): el cleanup es SECUNDARIO y no
                # puede reemplazar la excepción primaria de la operación. Acá el
                # guard faltaba entero, así que cualquier fallo del marcado —no
                # sólo el de cuarentena— se llevaba puesta la primaria.
                logger.error(
                    "Marking transaction %d as rolled back failed during exception handling",
                    tx_id,
                    exc_info=True,
                )
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

        if self._lifecycle is not None:
            # PR-1b2b: UNA sola transaction() conteniendo ambos UPDATE.
            async with self._lifecycle.transaction(self._db_path) as conn:
                await conn.execute(
                    """
                    UPDATE journal_entries
                    SET status = ?, rolled_back = 1
                    WHERE id = ?
                    """,
                    (OperationStatus.ROLLED_BACK.value, entry_id),
                )

                if details:
                    await conn.execute(
                        """
                        UPDATE journal_entries
                        SET metadata = json_set(COALESCE(metadata, '{}'), '$.rollback_details', ?)
                        WHERE id = ?
                        """,
                        (details, entry_id),
                    )

            logger.info("Operation rolled back", extra={"entry_id": entry_id, "details": details})

            return

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

        if self._lifecycle is not None:
            async with self._lifecycle.transaction(self._db_path) as conn:
                await conn.execute(
                    "UPDATE journal_entries SET status = ? WHERE id = ?",
                    (status.value, entry_id),
                )
            return

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

        sql = """
            SELECT id, timestamp, agent_id, operation_type, target_path,
                   status, snapshot_path, checksum, metadata
            FROM journal_entries
            WHERE transaction_id = ?
            ORDER BY timestamp ASC
            """
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, (transaction_id,)) as cursor,
            ):
                rows = await cursor.fetchall()
        else:
            async with self._lock, db.execute(sql, (transaction_id,)) as cursor:
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

        query = (
            "SELECT id, timestamp, agent_id, operation_type, target_path, "
            "status, snapshot_path, checksum, metadata "
            "FROM journal_entries "
            f"WHERE agent_id = ? AND status IN ({placeholders}) "  # nosec
            "ORDER BY timestamp DESC "
            "LIMIT 1"
        )
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(query, (agent_id, *status_values)) as cursor,
            ):
                row = await cursor.fetchone()
        else:
            async with self._lock, db.execute(query, (agent_id, *status_values)) as cursor:
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

        sql = """
            SELECT id, timestamp, agent_id, operation_type, target_path,
                   status, snapshot_path, checksum, metadata
            FROM journal_entries
            WHERE id = ?
            """
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, (entry_id,)) as cursor,
            ):
                row = await cursor.fetchone()
        else:
            async with self._lock, db.execute(sql, (entry_id,)) as cursor:
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

        sql = """
            SELECT id, timestamp, agent_id, operation_type, target_path,
                   status, snapshot_path, checksum, metadata
            FROM journal_entries
            WHERE agent_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, (agent_id, limit)) as cursor,
            ):
                rows = await cursor.fetchall()
        else:
            async with self._lock, db.execute(sql, (agent_id, limit)) as cursor:
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

        sql = """
            SELECT id, timestamp, agent_id, operation_type, target_path,
                   status, snapshot_path, checksum, metadata
            FROM journal_entries
            WHERE target_path = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, (target_path, limit)) as cursor,
            ):
                rows = await cursor.fetchall()
        else:
            async with self._lock, db.execute(sql, (target_path, limit)) as cursor:
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

        if self._lifecycle is not None:
            async with self._lifecycle.transaction(self._db_path) as conn:
                await conn.execute(
                    """
                    UPDATE transactions
                    SET status = ?, rolled_back_at = datetime('now')
                    WHERE transaction_id = ?
                    """,
                    (TransactionStatus.ROLLED_BACK.value, transaction_id),
                )

            logger.info(
                "Transaction marked as rolled back",
                extra={"transaction_id": transaction_id},
            )

            return

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
        self,
        transaction_id: int,
        operation_type: Any,
        target_path: str,
        agent_id: str,
        metadata: dict[str, Any] | None = None,
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

    # D2 (PR #493): sin DB no hay handoff durable — no-op honestos para el
    # sandbox (nunca consultar `_ensure_connected` acá: abriría una conexión
    # real bajo la instancia no-op).

    async def consultar_handoff_activo(self, artifact_path: str) -> None:
        return None

    async def crear_handoff_de_deployment(
        self,
        transaction_id: int,
        *,
        descripcion: str | None,
        registro: DeploymentHandoff,
        viejo: DeploymentHandoff | None = None,
    ) -> int:
        return 999999

    async def completar_handoff_de_resume(self, transaction_id: int, handoff_id: int) -> None:
        pass

    async def transicionar_handoff(
        self, handoff_id: int, *, desde: HandoffState, hacia: HandoffState, **campos: object
    ) -> bool:
        return True

    async def registrar_handoff_indeterminado(self, *, registro: DeploymentHandoff) -> None:
        return None

    async def transacciones_que_nombran(self, objetivo: str, ids_extra: Iterable[int] = ()) -> list[int]:
        return []


class StagingJournal(OperationJournal):
    """Journal que difiere el commit de la transacción en el journal real

    hasta que se confirma la promoción del sandbox.
    """

    def __init__(self, real_journal: OperationJournal) -> None:
        self._real_journal = real_journal
        self._staged_tx_id: int | None = None
        self._staged_commit = False

    @property
    def staged_transaction_id(self) -> int | None:
        """La TX real diferida, o ``None`` si aún no se abrió o ya se resolvió.

        El caller la captura ANTES de ``commit_staged``/``rollback_staged`` (que
        la resetean a ``None``) para poder cerrar la caja negra post-vuelo (T-28)
        contra el journal real una vez que la TX tiene su estado final.
        """
        return self._staged_tx_id

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

    # D2 (PR #493): el handoff durable vive en el journal REAL — el sandbox no
    # crea identidad durable hasta la promoción. Las lecturas se delegan sin
    # diferir: un resume no ocurre dentro del sandbox.

    async def consultar_handoff_activo(self, *args: Any, **kwargs: Any) -> DeploymentHandoff | None:
        return await self._real_journal.consultar_handoff_activo(*args, **kwargs)

    async def crear_handoff_de_deployment(self, *args: Any, **kwargs: Any) -> int:
        return await self._real_journal.crear_handoff_de_deployment(*args, **kwargs)

    async def completar_handoff_de_resume(self, *args: Any, **kwargs: Any) -> None:
        await self._real_journal.completar_handoff_de_resume(*args, **kwargs)

    async def transicionar_handoff(self, *args: Any, **kwargs: Any) -> bool:
        return await self._real_journal.transicionar_handoff(*args, **kwargs)

    async def registrar_handoff_indeterminado(self, *args: Any, **kwargs: Any) -> int | None:
        return await self._real_journal.registrar_handoff_indeterminado(*args, **kwargs)

    async def transacciones_que_nombran(self, *args: Any, **kwargs: Any) -> list[int]:
        return await self._real_journal.transacciones_que_nombran(*args, **kwargs)

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
