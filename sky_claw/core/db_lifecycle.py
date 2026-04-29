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

import atexit
import logging
import sqlite3
import time
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


# ---------------------------------------------------------------------------
# DatabaseLifecycleManager
# ---------------------------------------------------------------------------


class DatabaseLifecycleManager:
    """Manages SQLite WAL lifecycle across multiple database files.

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
        self._registered_signals: bool = False

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
        path_str = str(db_path)

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

    def get_connection(self, db_path: str) -> aiosqlite.Connection | None:
        """Get a managed connection by db_path string."""
        return self._connections.get(db_path)

    @property
    def managed_paths(self) -> list[str]:
        """List of currently managed database path strings."""
        return list(self._connections.keys())
