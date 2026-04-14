"""Distributed lock manager with TTL-based leases for transactional VFS safety.

Provides :class:`DistributedLockManager` backed by SQLite ``resource_locks``
table, and :class:`SnapshotTransactionLock` — an async context manager that
coordinates lock acquisition, snapshotting via :class:`FileSnapshotManager`,
and automatic rollback on failure.

Sprint 2 (Fase 1): Prevents race conditions and VFS corruption when multiple
agents modify MO2 resources in parallel.  Deadlocks from agent crashes are
mitigated by time-based leases (TTL).

**Design decisions (LÓGICA, ARQUITECTURA, PREVENCIÓN):**

LÓGICA:
    - Atomic INSERT/UPDATE via ``INSERT ... ON CONFLICT DO UPDATE WHERE expires_at < ?``
      ensures only one agent can hold a lock at a time, even under concurrent access.
    - Expired leases are automatically reclaimed — no manual cleanup needed.

ARQUITECTURA:
    - Module lives in ``sky_claw.db.locks`` to keep lock infrastructure separate
      from the mod registry (``async_registry.py``) and journaling (``journal.py``).
    - ``SnapshotTransactionLock`` composes ``DistributedLockManager`` +
      ``FileSnapshotManager`` via constructor injection (DI) — no globals.

PREVENCIÓN:
    - 10-minute default TTL is tuned for xEdit/DynDOLOD long-running ops.
    - All DB writes use ``journal_mode=WAL`` for crash safety.
    - If the database fails, the filesystem is never partially mutated:
      snapshot is created AFTER lock acquisition, rollback happens BEFORE
      lock release.
    - ``asyncio.CancelledError`` is not caught by ``except Exception``,
      ensuring the lock is always released in ``__aexit__`` via ``finally``.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from sky_claw.db.snapshot_manager import FileSnapshotManager, SnapshotInfo

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTIONS
# =============================================================================


class LockError(Exception):
    """Base exception for distributed lock operations."""


class LockAcquisitionError(LockError):
    """Raised when a lock cannot be acquired after all retry attempts."""

    def __init__(
        self,
        resource_id: str,
        agent_id: str,
        message: str = "",
    ) -> None:
        self.resource_id = resource_id
        self.agent_id = agent_id
        super().__init__(
            message
            or f"Failed to acquire lock on '{resource_id}' for agent '{agent_id}'"
        )


class LockReleaseError(LockError):
    """Raised when a lock release fails (non-fatal, logged as warning)."""


# =============================================================================
# CONSTANTS
# =============================================================================

#: Default TTL in seconds — 10 minutes, tuned for long-running xEdit sessions.
DEFAULT_LOCK_TTL_SECONDS: float = 600.0

#: Maximum number of acquisition retry attempts.
DEFAULT_MAX_RETRIES: int = 5

#: Base delay for exponential backoff (seconds).
DEFAULT_BACKOFF_BASE: float = 0.1

#: Maximum delay between retries (seconds).
DEFAULT_BACKOFF_MAX: float = 5.0

_LOCKS_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS resource_locks (
    resource_id   TEXT    PRIMARY KEY,
    agent_id      TEXT    NOT NULL,
    acquired_at   REAL    NOT NULL,
    expires_at    REAL    NOT NULL
);
"""

_ACQUIRE_SQL = """\
INSERT INTO resource_locks (resource_id, agent_id, acquired_at, expires_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(resource_id) DO UPDATE SET
    agent_id    = excluded.agent_id,
    acquired_at = excluded.acquired_at,
    expires_at  = excluded.expires_at
WHERE resource_locks.expires_at < excluded.acquired_at
"""

_RELEASE_SQL = """\
DELETE FROM resource_locks
WHERE resource_id = ? AND agent_id = ?
"""

_RELEASE_ANY_SQL = """\
DELETE FROM resource_locks
WHERE resource_id = ?
"""


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class LockInfo:
    """Metadata about a held lock."""

    resource_id: str
    agent_id: str
    acquired_at: float
    expires_at: float

    @property
    def remaining_ttl(self) -> float:
        """Seconds remaining before this lease expires."""
        return max(0.0, self.expires_at - time.time())

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


# =============================================================================
# DISTRIBUTED LOCK MANAGER
# =============================================================================


class DistributedLockManager:
    """SQLite-backed distributed lock manager with TTL leases.

    Uses ``aiosqlite`` for async-safe operations and ``journal_mode=WAL``
    for crash resilience.  Locks are acquired atomically using
    ``INSERT ... ON CONFLICT DO UPDATE WHERE expires_at < ?`` so that
    expired leases are transparently reclaimed.

    Parameters
    ----------
    db_path:
        Path to the SQLite lock database file.
    default_ttl:
        Default lock TTL in seconds.
    max_retries:
        Maximum number of acquisition attempts before raising.
    backoff_base:
        Base delay for exponential backoff.
    backoff_max:
        Maximum delay between retries.
    """

    def __init__(
        self,
        db_path: pathlib.Path | str,
        *,
        default_ttl: float = DEFAULT_LOCK_TTL_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
    ) -> None:
        self._db_path = str(db_path)
        self._default_ttl = default_ttl
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open DB, set WAL mode, create the ``resource_locks`` table."""
        if self._conn is not None:
            return

        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_LOCKS_SCHEMA_SQL)
        logger.info(
            "DistributedLockManager initialized",
            extra={"db_path": self._db_path, "default_ttl": self._default_ttl},
        )

    async def close(self) -> None:
        """Close the DB connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("DistributedLockManager closed")

    def _ensure_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise LockError("LockManager not initialized — call initialize() first")
        return self._conn

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def acquire_lock(
        self,
        resource_id: str,
        agent_id: str,
        ttl: float | None = None,
    ) -> LockInfo:
        """Attempt to acquire a lock, retrying with exponential backoff.

        Parameters
        ----------
        resource_id:
            Unique identifier for the resource to lock (e.g. file path).
        agent_id:
            Unique identifier for the requesting agent.
        ttl:
            Lock TTL in seconds (overrides default).

        Returns
        -------
        LockInfo
            Metadata about the acquired lock.

        Raises
        ------
        LockAcquisitionError
            If the lock cannot be acquired after ``max_retries`` attempts.
        """
        conn = self._ensure_conn()
        ttl_seconds = ttl if ttl is not None else self._default_ttl

        for attempt in range(self._max_retries):
            now = time.time()
            expires_at = now + ttl_seconds

            try:
                async with conn.execute(
                    _ACQUIRE_SQL,
                    (resource_id, agent_id, now, expires_at),
                ) as cursor:
                    rowcount = cursor.rowcount

                await conn.commit()

                # SQLite INSERT ... ON CONFLICT: rowcount == 1 if we inserted or
                # updated (i.e. expired lock was reclaimed).  rowcount == 0 if
                # the conflict condition (expires_at < ?) was NOT met (lock held
                # by someone else and not yet expired).
                if rowcount > 0:
                    lock_info = LockInfo(
                        resource_id=resource_id,
                        agent_id=agent_id,
                        acquired_at=now,
                        expires_at=expires_at,
                    )
                    logger.info(
                        "Lock acquired",
                        extra={
                            "resource_id": resource_id,
                            "agent_id": agent_id,
                            "ttl": ttl_seconds,
                            "attempt": attempt + 1,
                        },
                    )
                    return lock_info

            except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                logger.warning(
                    "Lock acquisition DB error (attempt %d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    exc,
                )

            # Exponential backoff with jitter-free cap
            if attempt < self._max_retries - 1:
                delay = min(
                    self._backoff_base * (2**attempt),
                    self._backoff_max,
                )
                logger.debug(
                    "Lock busy, retrying in %.2fs (attempt %d/%d)",
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(delay)

        raise LockAcquisitionError(resource_id, agent_id)

    async def release_lock(
        self,
        resource_id: str,
        agent_id: str,
    ) -> bool:
        """Release a lock held by the given agent.

        Parameters
        ----------
        resource_id:
            Resource to unlock.
        agent_id:
            Agent that holds the lock.

        Returns
        -------
        bool
            ``True`` if the lock was found and deleted.
        """
        conn = self._ensure_conn()
        try:
            async with conn.execute(
                _RELEASE_SQL,
                (resource_id, agent_id),
            ) as cursor:
                deleted = cursor.rowcount > 0

            await conn.commit()

            if deleted:
                logger.info(
                    "Lock released",
                    extra={"resource_id": resource_id, "agent_id": agent_id},
                )
            else:
                logger.warning(
                    "Lock release: no matching lock found",
                    extra={"resource_id": resource_id, "agent_id": agent_id},
                )

            return deleted

        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
            logger.error(
                "Lock release failed: %s",
                exc,
                extra={"resource_id": resource_id, "agent_id": agent_id},
            )
            raise LockReleaseError(
                f"Failed to release lock '{resource_id}': {exc}"
            ) from exc

    async def force_release(self, resource_id: str) -> bool:
        """Force-release a lock regardless of agent ownership.

        Use only for emergency recovery (e.g. orphan locks after crash).
        """
        conn = self._ensure_conn()
        try:
            async with conn.execute(
                _RELEASE_ANY_SQL,
                (resource_id,),
            ) as cursor:
                deleted = cursor.rowcount > 0
            await conn.commit()
            if deleted:
                logger.warning(
                    "Lock force-released",
                    extra={"resource_id": resource_id},
                )
            return deleted
        except sqlite3.OperationalError as exc:
            raise LockReleaseError(
                f"Failed to force-release lock '{resource_id}': {exc}"
            ) from exc

    async def get_lock_info(self, resource_id: str) -> LockInfo | None:
        """Query current lock state for a resource (may be expired)."""
        conn = self._ensure_conn()
        async with conn.execute(
            "SELECT resource_id, agent_id, acquired_at, expires_at FROM resource_locks WHERE resource_id = ?",
            (resource_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return LockInfo(
                resource_id=row[0],
                agent_id=row[1],
                acquired_at=row[2],
                expires_at=row[3],
            )

    async def cleanup_expired(self) -> int:
        """Delete all locks whose TTL has expired.  Returns count removed."""
        conn = self._ensure_conn()
        now = time.time()
        async with conn.execute(
            "DELETE FROM resource_locks WHERE expires_at < ?",
            (now,),
        ) as cursor:
            count = cursor.rowcount
        await conn.commit()
        if count > 0:
            logger.info("Cleaned up %d expired lock(s)", count)
        return count


# =============================================================================
# SNAPSHOT TRANSACTION LOCK (Context Manager)
# =============================================================================


class SnapshotTransactionLock:
    """Async context manager that coordinates locking + snapshotting.

    Usage::

        async with SnapshotTransactionLock(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            resource_id="Skyrim.esm",
            agent_id="synthesis-pipeline",
            target_files=[Path("mods/Skyrim.esm")],
        ) as ctx:
            # Safe zone — lock held, snapshots created
            ctx.snapshots  # list of SnapshotInfo for rollback reference
            ... do work on the files ...

        # On normal exit: lock released.
        # On exception: files rolled back, then lock released.

    Parameters
    ----------
    lock_manager:
        Instance of :class:`DistributedLockManager`.
    snapshot_manager:
        Instance of :class:`FileSnapshotManager`.
    resource_id:
        Name/path of the resource to lock.
    agent_id:
        Agent requesting the lock.
    target_files:
        List of file paths to snapshot on entry.
    ttl:
        Lock TTL override in seconds.
    metadata:
        Extra metadata for the snapshot entries.
    """

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        resource_id: str,
        agent_id: str,
        target_files: list[pathlib.Path] | None = None,
        ttl: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._resource_id = resource_id
        self._agent_id = agent_id
        self._target_files = target_files or []
        self._ttl = ttl
        self._metadata = metadata

        # Populated during __aenter__
        self.lock_info: LockInfo | None = None
        self.snapshots: list[SnapshotInfo] = []

    async def __aenter__(self) -> SnapshotTransactionLock:
        """Acquire the distributed lock, then create snapshots."""
        # Step 1: Acquire lock
        self.lock_info = await self._lock_manager.acquire_lock(
            resource_id=self._resource_id,
            agent_id=self._agent_id,
            ttl=self._ttl,
        )

        # Step 2: Create snapshots of target files
        try:
            for file_path in self._target_files:
                if file_path.exists() and file_path.is_file():
                    snap = await self._snapshot_manager.create_snapshot(
                        file_path,
                        metadata=self._metadata,
                    )
                    self.snapshots.append(snap)
                    logger.debug(
                        "Snapshot created under transaction lock",
                        extra={
                            "resource_id": self._resource_id,
                            "file": str(file_path),
                            "snapshot_id": snap.snapshot_id,
                        },
                    )
        except Exception:
            # Snapshot creation failed — release the lock before propagating
            await self._safe_release()
            raise

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """On exception: rollback all snapshots.  Always release the lock."""
        try:
            if exc_type is not None:
                # Rollback: restore all snapshots in reverse order
                logger.warning(
                    "Exception detected — rolling back %d snapshot(s)",
                    len(self.snapshots),
                    extra={
                        "resource_id": self._resource_id,
                        "agent_id": self._agent_id,
                        "exception": str(exc_val),
                    },
                )
                for snap in reversed(self.snapshots):
                    try:
                        await self._snapshot_manager.restore_snapshot(
                            snap.snapshot_path,
                            pathlib.Path(snap.original_path),
                            verify_checksum=False,
                        )
                        logger.info(
                            "Rolled back file to snapshot",
                            extra={
                                "original_path": snap.original_path,
                                "snapshot_id": snap.snapshot_id,
                            },
                        )
                    except Exception as rollback_exc:
                        logger.critical(
                            "ROLLBACK FAILED for %s: %s — manual recovery required",
                            snap.original_path,
                            rollback_exc,
                            exc_info=True,
                        )
        finally:
            # Always release the lock, even if rollback fails
            await self._safe_release()

    async def _safe_release(self) -> None:
        """Release lock, swallowing errors to avoid masking the original exception."""
        try:
            await self._lock_manager.release_lock(
                resource_id=self._resource_id,
                agent_id=self._agent_id,
            )
        except Exception as release_exc:
            logger.error(
                "Failed to release lock (will expire by TTL): %s",
                release_exc,
                extra={
                    "resource_id": self._resource_id,
                    "agent_id": self._agent_id,
                },
            )
