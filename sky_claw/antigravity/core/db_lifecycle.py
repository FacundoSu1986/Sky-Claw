"""DatabaseLifecycleManager — SQLite WAL lifecycle management for Sky-Claw.

Manages the full lifecycle of SQLite database connections with WAL mode:
- Initialization with orphaned WAL recovery
- Periodic checkpointing (PASSIVE) to prevent unbounded WAL growth
- Graceful shutdown with TRUNCATE checkpoint to eliminate WAL files
- Health monitoring of WAL file sizes
- Signal handlers for SIGINT/SIGTERM to ensure checkpoint on process exit

Design invariants:
- Every init must check for orphaned WAL files and recover them.
- Every shutdown must execute PRAGMA wal_checkpoint(TRUNCATE) before close.
- Signal handlers have a hard 10-second timeout to prevent zombie processes.
- All pragmas are applied once per connection, not per transaction.
- No circular imports with database.py — this module is standalone.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import sqlite3
import time
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("SkyClaw.DatabaseLifecycle")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CHECKPOINT_INTERVAL_SECONDS = 300  # 5 minutes
WAL_WARNING_THRESHOLD_BYTES = 10_485_760  # 10 MB
WAL_CRITICAL_THRESHOLD_BYTES = 52_428_800  # 50 MB
SHUTDOWN_CHECKPOINT_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DatabaseLifecycleConfig(BaseModel):
    """Immutable configuration for DatabaseLifecycleManager."""

    model_config = ConfigDict(strict=True, frozen=True)

    wal_checkpoint_interval_seconds: int = DEFAULT_CHECKPOINT_INTERVAL_SECONDS
    wal_warning_threshold_bytes: int = WAL_WARNING_THRESHOLD_BYTES
    wal_critical_threshold_bytes: int = WAL_CRITICAL_THRESHOLD_BYTES
    enable_auto_checkpoint: bool = True
    enable_signal_handlers: bool = True
    busy_timeout_ms: int = 5000
    synchronous_mode: str = "NORMAL"


class WALHealth(BaseModel):
    """Health status of a single database's WAL file."""

    model_config = ConfigDict(strict=True, frozen=True)

    db_path: str
    wal_exists: bool
    wal_size_bytes: int
    status: str  # "healthy" | "warning" | "critical" | "unknown"
    message: str = ""


class _DatabaseOperationCancelled(asyncio.CancelledError):
    """Distingue una operación DB cancelada de la cancelación del llamador."""

    def __init__(self, cause: asyncio.CancelledError) -> None:
        super().__init__(*cause.args)
        self.cause = cause


class _DatabaseOperationFailedAfterCancellation(asyncio.CancelledError):
    """Conserva cancelación externa y fallo posterior de la operación DB."""

    def __init__(
        self,
        cancellation: asyncio.CancelledError,
        operation_error: BaseException,
    ) -> None:
        super().__init__(*cancellation.args)
        self.cancellation = cancellation
        self.operation_error = operation_error


# ---------------------------------------------------------------------------
# DatabaseLifecycleManager
# ---------------------------------------------------------------------------


class DatabaseLifecycleManager:
    """Manages SQLite WAL lifecycle across multiple database files.

    CONTRATO M-01: Punto de entrada centralizado para conexiones aiosqlite en
    los módulos cubiertos por M-01. Todos ellos deben pedir su conexión vía
    ``await lifecycle.get_connection(path)`` en lugar de ejecutar
    ``aiosqlite.connect(path)`` directamente. Esto garantiza:

    - WAL recovery automática para archivos huérfanos.
    - Pragmas hardenizadas consistentes según la ``DatabaseLifecycleConfig``
      activa (defaults: synchronous=NORMAL, busy_timeout=5000,
      foreign_keys=ON, temp_store=MEMORY).
    - Shutdown coordinado vía ``shutdown_all()`` que ejecuta TRUNCATE
      checkpoint y elimina los ``-wal``/``-shm``.

    Módulos cubiertos por M-01/M-01.1: ``core/database.py``,
    ``db/async_registry.py``, ``db/journal.py``, ``security/governance.py``,
    ``db/locks.py``, ``agent/router.py``, ``core/dlq_manager.py`` (los tres
    últimos migrados en M-01.1 con DI opcional ``lifecycle=``; sin él
    conservan su fallback directo pre-M-01 para tests/standalone).

    Exenciones deliberadas (triage M-01.1 — NO migrar sin nueva razón):
    - ``security/credential_vault.py``: pool propio acotado
      (BoundedSemaphore + timeout) sobre un archivo con ACL owner-only;
      es un boundary de seguridad con su propio modelo de concurrencia.
    - ``agent/context_manager.py``: lecturas read-only por-operación
      contra el registry (WAL: los lectores no bloquean escritores).
    - ``db/registry.py``: legacy sin usos en producción.

    Usage::

        lifecycle = DatabaseLifecycleManager(
            db_paths=[Path("sky_claw_state.db")],
        )
        await lifecycle.init_all()
        # ... application runs ...
        await lifecycle.shutdown_all()

    For signal-based graceful shutdown::

        lifecycle.register_graceful_shutdown()
    """

    def __init__(
        self,
        db_paths: list[Path] | None = None,
        config: DatabaseLifecycleConfig | None = None,
    ) -> None:
        self._config = config or DatabaseLifecycleConfig()
        self._db_paths: list[Path] = list(db_paths) if db_paths else []
        self._connections: dict[str, aiosqlite.Connection] = {}
        # Per-path write lock shared by every wrapper reusing the same managed
        # connection (see get_write_lock) — closes the shared-connection write race.
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._registered_signals: bool = False
        # Lock para garantizar singleton por path ante llamadas concurrentes
        self._init_lock = asyncio.Lock()

    def add_db_path(self, path: Path) -> None:
        """Register an additional database path for lifecycle management."""
        resolved = path.resolve()
        if resolved not in [p.resolve() for p in self._db_paths]:
            self._db_paths.append(path)

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def init_all(self) -> None:
        """Initialize all registered databases with WAL recovery + pragmas.

        For each database:
        1. Check for orphaned WAL/SHM files → recover if found.
        2. Open connection with WAL mode and hardened pragmas.
        3. Verify pragmas are correctly applied.
        """
        for db_path in self._db_paths:
            await self._init_single(db_path)

    async def _init_single(self, db_path: Path) -> None:
        """Initialize a single database with recovery and pragmas."""
        # Usar la misma clave canonizada que get_connection para consistencia
        path_str = str(db_path.resolve())

        # Step 1: Check for orphaned WAL files (crash recovery)
        wal_path = Path(path_str + "-wal")
        shm_path = Path(path_str + "-shm")

        if wal_path.exists() or shm_path.exists():
            logger.warning(
                "Recovering from orphaned WAL file at %s (wal=%s, shm=%s)",
                path_str,
                wal_path.exists(),
                shm_path.exists(),
            )
            await self._recover_orphaned_wal(db_path, wal_path, shm_path)

        # Step 2: Open connection and apply pragmas
        conn = await aiosqlite.connect(path_str)
        conn.row_factory = aiosqlite.Row

        # Apply all pragmas
        await self._apply_pragmas(conn, path_str)

        # Store connection
        self._connections[path_str] = conn
        logger.info("DatabaseLifecycle: initialized %s with WAL mode", path_str)

    async def _recover_orphaned_wal(
        self,
        db_path: Path,
        wal_path: Path,
        shm_path: Path,
    ) -> None:
        """Recover data from orphaned WAL files before normal init.

        Opens a temporary connection, forces a TRUNCATE checkpoint,
        then closes. If recovery fails, renames the WAL for forensics.
        """
        path_str = str(db_path)
        try:
            # Open temporary connection for recovery
            conn = await aiosqlite.connect(path_str)
            await conn.execute("PRAGMA journal_mode=WAL")
            # Force checkpoint to flush WAL contents into main DB
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await conn.close()

            # Verify WAL files were cleaned up
            if wal_path.exists() or shm_path.exists():
                logger.warning(
                    "WAL files still present after recovery checkpoint for %s",
                    path_str,
                )

            logger.info(
                "DatabaseLifecycle: successfully recovered orphaned WAL for %s",
                path_str,
            )

        except Exception as e:
            logger.error(
                "DatabaseLifecycle: WAL recovery FAILED for %s: %s",
                path_str,
                e,
            )
            # Rename corrupted WAL for forensics
            timestamp = int(time.time())
            if wal_path.exists():
                corrupted_name = f"{path_str}-wal.corrupted.{timestamp}"
                try:
                    import shutil

                    shutil.move(str(wal_path), corrupted_name)
                    logger.error(
                        "Renamed corrupted WAL to %s for forensics",
                        corrupted_name,
                    )
                except OSError:
                    logger.error("Could not rename corrupted WAL file")

    async def _apply_pragmas(self, conn: aiosqlite.Connection, path_str: str) -> None:
        """Apply hardened SQLite pragmas to a connection."""
        cfg = self._config

        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(f"PRAGMA synchronous={cfg.synchronous_mode}")
        await conn.execute(f"PRAGMA busy_timeout={cfg.busy_timeout_ms}")
        await conn.execute("PRAGMA temp_store=MEMORY")

        # Verify critical pragmas
        async with conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
            mode = row[0] if row else ""
            if mode != "wal":
                logger.error(
                    "PRAGMA journal_mode returned '%s' instead of 'wal' for %s",
                    mode,
                    path_str,
                )

        async with conn.execute("PRAGMA foreign_keys") as cursor:
            row = await cursor.fetchone()
            fk = row[0] if row else 0
            if fk != 1:
                logger.error(
                    "PRAGMA foreign_keys returned %s instead of 1 for %s",
                    fk,
                    path_str,
                )

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    async def checkpoint_all(self, mode: str = "PASSIVE") -> dict[str, dict[str, Any]]:
        """Execute WAL checkpoint on all managed connections.

        Args:
            mode: Checkpoint mode — "PASSIVE" (non-blocking) for periodic
                maintenance, "TRUNCATE" for shutdown.

        Returns:
            Dict mapping db_path → checkpoint result info.
        """
        results: dict[str, dict[str, Any]] = {}
        for path_str, conn in list(self._connections.items()):
            try:
                # Log WAL size before checkpoint
                wal_size_before = self._get_wal_size(path_str)

                async with conn.execute(f"PRAGMA wal_checkpoint({mode})") as cursor:
                    row = await cursor.fetchone()
                    result = {
                        "busy": row[0] if row else -1,
                        "log_frames": row[1] if row else -1,
                        "checkpointed_frames": row[2] if row else -1,
                        "wal_size_before": wal_size_before,
                        "wal_size_after": self._get_wal_size(path_str),
                        "mode": mode,
                    }
                    results[path_str] = result

                    logger.info(
                        "WAL checkpoint (%s) for %s: %d frames checkpointed, WAL size %d→%d bytes",
                        mode,
                        path_str,
                        result["checkpointed_frames"],
                        wal_size_before,
                        result["wal_size_after"],
                    )

                    # Exceptional TRUNCATE if WAL exceeds critical threshold
                    if mode == "PASSIVE" and wal_size_before > self._config.wal_critical_threshold_bytes:
                        logger.warning(
                            "WAL size %d bytes exceeds critical threshold %d — executing emergency TRUNCATE for %s",
                            wal_size_before,
                            self._config.wal_critical_threshold_bytes,
                            path_str,
                        )
                        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            except Exception as e:
                logger.error("WAL checkpoint failed for %s: %s", path_str, e)
                results[path_str] = {"error": str(e), "mode": mode}

        return results

    def _get_wal_size(self, db_path: str) -> int:
        """Get the size of the WAL file for a database, or 0 if not found."""
        wal_path = Path(db_path + "-wal")
        try:
            return wal_path.stat().st_size if wal_path.exists() else 0
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown_all(self) -> None:
        """Gracefully shutdown all database connections.

        1. Execute TRUNCATE checkpoint on all connections.
        2. Close all connections.
        3. Verify WAL/SHM files were eliminated.
        """
        if not self._connections:
            return

        logger.info(
            "DatabaseLifecycle: shutting down %d connections...",
            len(self._connections),
        )

        # Step 1: TRUNCATE checkpoint
        try:
            await self.checkpoint_all(mode="TRUNCATE")
        except Exception as e:
            logger.critical("DatabaseLifecycle: checkpoint during shutdown FAILED: %s", e)

        # Step 2: Close all connections
        for path_str, conn in list(self._connections.items()):
            try:
                await conn.close()
                logger.info("DatabaseLifecycle: closed %s", path_str)
            except Exception as e:
                logger.error("Error closing %s: %s", path_str, e)

        self._connections.clear()

        # Step 3: Verify WAL/SHM elimination
        for db_path in self._db_paths:
            path_str = str(db_path)
            wal_path = Path(path_str + "-wal")
            shm_path = Path(path_str + "-shm")
            if wal_path.exists() or shm_path.exists():
                logger.warning(
                    "Post-shutdown: WAL/SHM files still present for %s "
                    "(wal=%s, shm=%s). This may indicate incomplete checkpoint.",
                    path_str,
                    wal_path.exists(),
                    shm_path.exists(),
                )

        logger.info("DatabaseLifecycle: shutdown complete")

    # ------------------------------------------------------------------
    # Health Check
    # ------------------------------------------------------------------

    async def health_check(self) -> dict[str, WALHealth]:
        """Check WAL health for all managed databases.

        Returns:
            Dict mapping db_path → WALHealth status.
        """
        results: dict[str, WALHealth] = {}
        for db_path in self._db_paths:
            path_str = str(db_path)
            wal_size = self._get_wal_size(path_str)
            wal_exists = wal_size > 0

            if wal_size > self._config.wal_critical_threshold_bytes:
                status = "critical"
                message = f"WAL size {wal_size} bytes exceeds critical threshold"
            elif wal_size > self._config.wal_warning_threshold_bytes:
                status = "warning"
                message = f"WAL size {wal_size} bytes exceeds warning threshold"
            else:
                status = "healthy"
                message = "WAL size within normal range"

            results[path_str] = WALHealth(
                db_path=path_str,
                wal_exists=wal_exists,
                wal_size_bytes=wal_size,
                status=status,
                message=message,
            )

        return results

    # ------------------------------------------------------------------
    # Atexit Handler
    # ------------------------------------------------------------------

    def register_atexit_handler(self) -> None:
        """Register atexit handler for synchronous WAL checkpoint on process exit.

        NOTE: Signal handling (SIGINT/SIGTERM) is NOT done here. The application
        entrypoint (AppContext or __main__.py) should use ``loop.add_signal_handler()``
        to set a ``shutdown_event`` that allows the async ``AsyncExitStack`` to
        unwind naturally. The WAL checkpoint will then execute via
        ``shutdown_all()`` as part of the async teardown.

        This atexit handler is a safety net for normal process exit paths
        where the async loop may have already stopped.
        """
        if self._registered_signals:
            return

        atexit.register(self._sync_shutdown)
        self._registered_signals = True
        logger.info("DatabaseLifecycle: registered atexit handler for WAL checkpoint")

    def _sync_shutdown(self) -> None:
        """Synchronous shutdown for atexit handler.

        Uses synchronous sqlite3 (not aiosqlite) since the async loop
        may have already stopped. Hard timeout of 10 seconds.
        """
        import time as _time

        start = _time.monotonic()
        for db_path in self._db_paths:
            path_str = str(db_path)
            try:
                conn = sqlite3.connect(path_str, timeout=2)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
                elapsed = _time.monotonic() - start
                logger.info(
                    "Sync shutdown checkpoint for %s completed in %.2fs",
                    path_str,
                    elapsed,
                )
            except Exception as e:
                elapsed = _time.monotonic() - start
                if elapsed > SHUTDOWN_CHECKPOINT_TIMEOUT_SECONDS:
                    logger.critical(
                        "Sync shutdown TIMEOUT (%.1fs) for %s — aborting checkpoint. Last error: %s",
                        elapsed,
                        path_str,
                        e,
                    )
                    break
                logger.error("Sync shutdown checkpoint failed for %s: %s", path_str, e)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    async def get_connection(self, db_path: Path | str) -> aiosqlite.Connection:
        """Return the managed connection for db_path, initializing on demand.

        CONTRATO M-01: Ningún módulo cubierto debe llamar
        ``aiosqlite.connect(...)`` directamente. Pedir la conexión a este
        método garantiza WAL recovery, pragmas hardenizadas según la
        ``DatabaseLifecycleConfig`` activa (defaults: synchronous=NORMAL,
        busy_timeout=5000, foreign_keys=ON, temp_store=MEMORY) y shutdown
        coordinado vía ``shutdown_all()``.

        Args:
            db_path: Ruta a la DB (Path o str). Se normaliza a str para la
                clave del dict ``_connections``.

        Returns:
            Conexión aiosqlite ya inicializada con pragmas. La misma instancia
            ante llamadas repetidas con el mismo path (singleton por path).
        """
        path_obj = db_path if isinstance(db_path, Path) else Path(db_path)
        # Canonizar la key con .resolve() para que rutas equivalentes
        # (p.ej. "db.db" vs "./db.db") mapeen al mismo singleton.
        path_str = str(path_obj.resolve())

        # Fast-path sin lock (double-checked locking)
        if path_str in self._connections:
            return self._connections[path_str]

        # Lock para evitar race condition: dos coroutines pueden pasar el
        # check anterior simultáneamente y ambas intentarían init.
        async with self._init_lock:
            # Re-check dentro del lock (segunda mitad del double-checked locking)
            if path_str in self._connections:
                return self._connections[path_str]

            # Lazy registration: si el path no estaba en _db_paths, agregarlo
            # para que los métodos que iteran (health_check, shutdown verification)
            # lo vean.
            if not any(p.resolve() == path_obj.resolve() for p in self._db_paths):
                self._db_paths.append(path_obj)

            await self._init_single(path_obj)
            return self._connections[path_str]

    @staticmethod
    async def _await_db_operation(awaitable: Awaitable[None]) -> None:
        """Completa una operación SQLite aunque el llamador sea cancelado."""
        task = asyncio.ensure_future(awaitable)
        cancelación: asyncio.CancelledError | None = None
        current_task = asyncio.current_task()

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancelación_externa = not task.cancelled() or (
                    current_task is not None and current_task.cancelling() > 0
                )
                if cancelación_externa and cancelación is None:
                    cancelación = error
            except BaseException:
                if task.done():
                    break
                raise

        try:
            task.result()
        except asyncio.CancelledError as error:
            if cancelación is not None:
                raise _DatabaseOperationFailedAfterCancellation(
                    cancelación,
                    error,
                ) from error
            raise _DatabaseOperationCancelled(error) from error
        except BaseException as error:
            if cancelación is not None:
                raise _DatabaseOperationFailedAfterCancellation(
                    cancelación,
                    error,
                ) from error
            raise

        if cancelación is not None:
            raise cancelación

    async def _rollback_after_failed_commit(
        self,
        conn: aiosqlite.Connection,
        commit_failure: _DatabaseOperationFailedAfterCancellation,
    ) -> None:
        """Observa rollback y después restaura la cancelación original."""
        rollback_error: BaseException | None = None
        try:
            await self._await_db_operation(conn.rollback())
        except _DatabaseOperationFailedAfterCancellation as error:
            rollback_error = error.operation_error
        except _DatabaseOperationCancelled as error:
            rollback_error = error.cause
        except asyncio.CancelledError:
            # Una cancelación externa posterior no reemplaza a la original.
            pass
        except BaseException as error:
            rollback_error = error

        if rollback_error is not None:
            try:
                raise rollback_error from commit_failure.operation_error
            except BaseException as chained_rollback_error:
                raise commit_failure.cancellation from chained_rollback_error

        raise commit_failure.cancellation from commit_failure.operation_error

    @asynccontextmanager
    async def transaction(
        self,
        db_path: Path | str,
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Serializa una transacción completa sobre la conexión compartida."""
        conn = await self.get_connection(db_path)
        async with self.get_write_lock(db_path):
            try:
                yield conn
            except BaseException:
                await self._await_db_operation(conn.rollback())
                raise

            try:
                await self._await_db_operation(conn.commit())
            except _DatabaseOperationFailedAfterCancellation as error:
                await self._rollback_after_failed_commit(conn, error)
            except _DatabaseOperationCancelled as error:
                await self._await_db_operation(conn.rollback())
                raise error.cause from error
            except asyncio.CancelledError:
                # La cancelación se propaga después de completar el commit.
                raise
            except BaseException:
                await self._await_db_operation(conn.rollback())
                raise

    def get_write_lock(self, db_path: Path | str) -> asyncio.Lock:
        """Return the write lock bound to the connection for *db_path*.

        Connections are singletons per resolved path (see get_connection), so
        every ``AsyncModRegistry`` wrapper reusing the same connection must
        serialize writes through the SAME lock; a per-wrapper lock would leave
        the transaction-interleaving race open. Created lazily and cached under
        the same resolved-path key as the connection. No ``await`` between lookup
        and insert keeps this atomic on the single-threaded event loop.
        """
        path_str = str((db_path if isinstance(db_path, Path) else Path(db_path)).resolve())
        lock = self._write_locks.get(path_str)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[path_str] = lock
        return lock

    def evict_connection(self, db_path: Path | str) -> None:
        """Remove a connection from the managed pool without closing it.

        Intended for recovery paths where the caller has already closed the
        connection (e.g. after renaming a corrupt DB file for forensics).
        After eviction, the next ``get_connection()`` call for the same path
        will open a fresh connection.

        The path remains registered in ``_db_paths`` so health_check and
        shutdown verification still track it.

        Args:
            db_path: Path to the database whose connection should be evicted.
        """
        path_obj = db_path if isinstance(db_path, Path) else Path(db_path)
        path_str = str(path_obj.resolve())
        self._connections.pop(path_str, None)

    @property
    def managed_paths(self) -> list[str]:
        """List of currently managed database path strings."""
        return list(self._connections.keys())
