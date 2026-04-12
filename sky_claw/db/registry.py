"""SQLite mod registry – ``mod_registry.db``.

Provides traceability of mod versions, VFS status, and dependency
correlation.
"""

from __future__ import annotations

import pathlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiosqlite

from sky_claw.config import DB_PATH

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS mods (
    mod_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nexus_id        INTEGER UNIQUE NOT NULL,
    name            TEXT    NOT NULL,
    version         TEXT    NOT NULL DEFAULT '',
    author          TEXT    NOT NULL DEFAULT '',
    category        TEXT    NOT NULL DEFAULT '',
    download_url    TEXT    NOT NULL DEFAULT '',
    installed       INTEGER NOT NULL DEFAULT 0,   -- boolean flag
    enabled_in_vfs  INTEGER NOT NULL DEFAULT 0,   -- boolean flag
    install_path    TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mods_name ON mods (name);

CREATE TABLE IF NOT EXISTS dependencies (
    dep_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id          INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
    depends_on_nexus_id INTEGER NOT NULL,
    dep_name        TEXT    NOT NULL DEFAULT '',
    resolved        INTEGER NOT NULL DEFAULT 0,   -- boolean
    UNIQUE(mod_id, depends_on_nexus_id)
);

CREATE TABLE IF NOT EXISTS task_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id          INTEGER REFERENCES mods(mod_id) ON DELETE SET NULL,
    action          TEXT    NOT NULL,               -- e.g. 'download', 'install', 'enable'
    status          TEXT    NOT NULL DEFAULT 'pending',
    detail          TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


class ModRegistry:
    """Thin wrapper around the SQLite ``mod_registry.db`` database.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  Created if it does not exist.
    """

    def __init__(self, db_path: pathlib.Path | str | None = None) -> None:
        self._db_path = str(db_path or DB_PATH)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open (or create) the database and ensure the schema exists."""
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        try:
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            async with self._conn.execute("PRAGMA quick_check") as cur:
                row = await cur.fetchone()
                if row is None or str(row[0]).lower() != "ok":
                    raise RuntimeError(
                        f"SQLite integrity check failed for {self._db_path}"
                    )
            await self._conn.executescript(_SCHEMA_SQL)
        except Exception:
            await self._conn.close()
            self._conn = None
            raise

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Cursor, None]:
        """Context manager for an atomic transaction."""
        assert self._conn is not None, "Database is not open"
        async with self._conn.cursor() as cur:
            try:
                yield cur
                await self._conn.commit()
            except Exception as exc:
                await self._conn.rollback()
                logger.error("Transaction failed, rolling back: %s", exc)
                raise

    # ------------------------------------------------------------------
    # Mod CRUD
    # ------------------------------------------------------------------

    async def upsert_mod(
        self,
        nexus_id: int,
        name: str,
        version: str = "",
        author: str = "",
        category: str = "",
        download_url: str = "",
    ) -> int:
        """Insert or update a mod record.  Returns the ``mod_id``."""
        async with self.transaction() as cur:
            await cur.execute(
                """
                INSERT INTO mods (nexus_id, name, version, author, category, download_url)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(nexus_id) DO UPDATE SET
                    name         = excluded.name,
                    version      = excluded.version,
                    author       = excluded.author,
                    category     = excluded.category,
                    download_url = excluded.download_url,
                    updated_at   = datetime('now')
                """,
                (nexus_id, name, version, author, category, download_url),
            )
            await cur.execute("SELECT mod_id FROM mods WHERE nexus_id = ?", (nexus_id,))
            row = await cur.fetchone()
            assert row is not None
            return int(row["mod_id"])

    async def get_mod(self, nexus_id: int) -> aiosqlite.Row | None:
        """Return the mod row for *nexus_id*, or ``None``."""
        assert self._conn is not None, "Database is not open"
        async with self._conn.execute(
            "SELECT * FROM mods WHERE nexus_id = ?", (nexus_id,)
        ) as cur:
            return await cur.fetchone()

    async def set_vfs_status(
        self, nexus_id: int, *, installed: bool, enabled: bool
    ) -> None:
        """Update VFS flags for a mod."""
        async with self.transaction() as cur:
            await cur.execute(
                """
                UPDATE mods
                   SET installed = ?, enabled_in_vfs = ?, updated_at = datetime('now')
                 WHERE nexus_id = ?
                """,
                (int(installed), int(enabled), nexus_id),
            )

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    async def add_dependency(
        self,
        mod_id: int,
        depends_on_nexus_id: int,
        dep_name: str = "",
    ) -> None:
        """Record that *mod_id* depends on *depends_on_nexus_id*."""
        async with self.transaction() as cur:
            await cur.execute(
                """
                INSERT OR IGNORE INTO dependencies
                    (mod_id, depends_on_nexus_id, dep_name)
                VALUES (?, ?, ?)
                """,
                (mod_id, depends_on_nexus_id, dep_name),
            )

    async def get_dependencies(self, mod_id: int) -> list[aiosqlite.Row]:
        """Return all dependency rows for *mod_id*."""
        assert self._conn is not None, "Database is not open"
        async with self._conn.execute(
            "SELECT * FROM dependencies WHERE mod_id = ?", (mod_id,)
        ) as cur:
            return await cur.fetchall()

    # ------------------------------------------------------------------
    # Task log
    # ------------------------------------------------------------------

    async def log_task(
        self,
        action: str,
        mod_id: int | None = None,
        status: str = "pending",
        detail: str = "",
    ) -> int:
        """Append an entry to the task log.  Returns the ``log_id``."""
        async with self.transaction() as cur:
            await cur.execute(
                """
                INSERT INTO task_log (mod_id, action, status, detail)
                VALUES (?, ?, ?, ?)
                """,
                (mod_id, action, status, detail),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid
