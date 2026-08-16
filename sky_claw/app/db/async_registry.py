"""Async SQLite mod registry – ``mod_registry.db``.

Wraps the database layer with :mod:`aiosqlite` for non-blocking access
and provides micro-batched writes via :meth:`executemany` to minimize
lock contention and I/O overhead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import sqlite3
import time
from typing import TYPE_CHECKING

import aiosqlite
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from sky_claw.app.core.db_lifecycle import (
    DatabaseConnectionQuarantinedError,
    DatabaseConnectionReplacedError,
)
from sky_claw.config import DB_PATH

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Raised when a database operation fails and has been rolled back."""


_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS mods (
    mod_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nexus_id        INTEGER UNIQUE NOT NULL,
    name            TEXT    NOT NULL,
    version         TEXT    NOT NULL DEFAULT '',
    author          TEXT    NOT NULL DEFAULT '',
    category        TEXT    NOT NULL DEFAULT '',
    download_url    TEXT    NOT NULL DEFAULT '',
    installed       INTEGER NOT NULL DEFAULT 0,
    enabled_in_vfs  INTEGER NOT NULL DEFAULT 0,
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
    resolved        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(mod_id, depends_on_nexus_id)
);

CREATE TABLE IF NOT EXISTS task_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id          INTEGER REFERENCES mods(mod_id) ON DELETE SET NULL,
    action          TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    detail          TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# DB-002: Unified upsert — single and batch now update the same columns
_UPSERT_MOD_SQL = """\
INSERT INTO mods (nexus_id, name, version, author, category, download_url, installed, enabled_in_vfs)
VALUES (?, ?, ?, ?, ?, ?, 0, 0)
ON CONFLICT(nexus_id) DO UPDATE SET
    name           = excluded.name,
    version        = excluded.version,
    author         = excluded.author,
    category       = excluded.category,
    download_url   = excluded.download_url,
    updated_at     = datetime('now')
RETURNING mod_id
"""

_UPSERT_MOD_SQL_BATCH = """\
INSERT INTO mods (nexus_id, name, version, author, category, download_url, installed, enabled_in_vfs)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(nexus_id) DO UPDATE SET
    name         = excluded.name,
    version      = excluded.version,
    author       = excluded.author,
    category     = excluded.category,
    download_url = excluded.download_url,
    installed    = excluded.installed,
    enabled_in_vfs = excluded.enabled_in_vfs,
    updated_at   = datetime('now')
"""

_INSERT_DEP_SQL = """\
INSERT OR IGNORE INTO dependencies (mod_id, depends_on_nexus_id, dep_name)
VALUES (?, ?, ?)
"""

_LOG_TASK_SQL = """\
INSERT INTO task_log (mod_id, action, status, detail)
VALUES (?, ?, ?, ?)
"""


class _DatabaseCorruptionError(RuntimeError):
    """DB-004: Specific exception for database integrity failures.

    Prevents unrelated ``RuntimeError`` instances from triggering the
    corrupt-database recovery path (rename + recreate).
    """


class AsyncModRegistry:
    """Async wrapper around the SQLite ``mod_registry.db`` database.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  Created if it does not exist.
    """

    def __init__(
        self,
        db_path: pathlib.Path | str | None = None,
        *,
        lifecycle=None,  # DatabaseLifecycleManager | None — evita import en runtime circular
    ) -> None:
        raw_path = str(db_path or DB_PATH)
        from sky_claw.app.core.validators.path import PathTraversalValidator

        validator = PathTraversalValidator(allow_absolute=True)
        result = validator.validate(raw_path)
        if not result.is_valid:
            raise ValueError(f"Path traversal detected in database path '{raw_path}': {result.error_message}")

        self._db_path = raw_path
        self._lifecycle = lifecycle  # DatabaseLifecycleManager | None
        self._owns_conn: bool = False  # True when we opened the connection directly (no lifecycle)
        self._conn: aiosqlite.Connection | None = None
        # P1: all coroutines share ONE aiosqlite connection, so execute+commit
        # (and executemany+rollback) must be serialized — otherwise one writer's
        # commit/rollback lands mid-transaction of another, committing partial
        # state or discarding uncommitted rows (silent data loss).
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open (or create) the database and ensure the schema exists.

        M-01: If a DatabaseLifecycleManager was injected, the connection is
        requested from it (WAL recovery + hardened pragmas already applied).
        Otherwise falls back to a direct ``aiosqlite.connect`` with manual
        pragmas (pre-M-01 behaviour), which avoids spawning a lifecycle that
        creates non-daemon aiosqlite worker threads — those block process exit
        on CPython 3.11 when callers omit ``close()`` (CI 20-minute timeout).

        Runs a quick integrity check on open.  If the database is corrupt,
        it automatically renames the corrupt file as a backup and creates a
        fresh database to prevent agent fatal loops.
        """
        if self._conn is not None:
            return

        if self._lifecycle is not None:
            # ----------------------------------------------------------
            # M-01 DI path — lifecycle owns and manages the connection
            # ----------------------------------------------------------
            self._owns_conn = False
            try:
                # Bind the per-connection write lock (shared across wrappers that
                # reuse this managed connection), not the per-instance default —
                # otherwise concurrent wrappers on the same connection would not
                # serialize and the transaction-interleaving race would remain.
                self._write_lock = self._lifecycle.get_write_lock(self._db_path)

                # PR-1b2b: el SQL lifecycle-backed corre dentro del boundary del
                # path. El quick_check es lectura: operation() sostiene el lock
                # compartido mientras corre, y lo libera antes de cualquier
                # recovery (close/evict/rename corren fuera del boundary).
                async with self._lifecycle.operation(self._db_path) as conn:
                    self._conn = conn
                    self._conn.row_factory = aiosqlite.Row
                    async with conn.execute("PRAGMA quick_check") as cur:
                        row = await cur.fetchone()
                        if row is None or str(row[0]).lower() != "ok":
                            # DB-004: Use specific exception to avoid catching unrelated RuntimeErrors
                            raise _DatabaseCorruptionError(f"SQLite integrity check failed for {self._db_path}")

            except _DatabaseCorruptionError as corruption_error:
                # PR-2: la invalidación de la conexión corrupta ya NO se hace a
                # mano (close + evict_connection). Ese par dejaba dos agujeros:
                # el cierre no confirmado perdía el ownership, y entre el evict y
                # el rename un operation() hermano podía abrir una conexión
                # "fresh" sobre el archivo que estaba por moverse — el rename se
                # llevaba el inodo y la recovery terminaba escribiendo el schema
                # dentro del backup corrupto. La ventana del lifecycle cubre las
                # dos cosas: mantiene el path fail-closed mientras dura, sin
                # sostener L_path durante el rename/backoff.
                corrupt_conn = self._conn
                if corrupt_conn is None:
                    raise
                self._conn = None

                db_file = pathlib.Path(self._db_path)
                try:
                    async with self._lifecycle.recover_connection(
                        self._db_path,
                        corrupt_conn,
                    ) as reabrir:
                        if db_file.exists():
                            backup_path = db_file.with_name(
                                f"{db_file.stem}.corrupt.{int(time.time())}{db_file.suffix}"
                            )

                            @retry(
                                retry=retry_if_exception_type(OSError),
                                stop=stop_after_attempt(5),
                                wait=wait_exponential(multiplier=1, min=1, max=10),
                                reraise=True,
                            )
                            async def _do_backup_lifecycle():
                                await asyncio.to_thread(db_file.rename, backup_path)

                            try:
                                await _do_backup_lifecycle()
                                logger.warning("Corrupt database moved to %s. Rebuilding...", backup_path)
                            except OSError as e:
                                logger.error("Failed to backup corrupt database: %s", e)
                                raise

                        # Conexión fresh Y schema DENTRO de la ventana: la
                        # cuarentena se levanta al salir, así que el path nunca
                        # se presenta como usable a medio reconstruir.
                        async with reabrir() as conn:
                            self._conn = conn
                            conn.row_factory = aiosqlite.Row
                            await conn.executescript(_SCHEMA_SQL)
                except DatabaseConnectionReplacedError as error:
                    self._conn = None
                    logger.error(
                        "Corrupt database connection was replaced before invalidation; replacement ownership retained"
                    )
                    raise corruption_error from error
                except DatabaseConnectionQuarantinedError as error:
                    self._conn = None
                    logger.error(
                        "Failed to close corrupt database; lifecycle ownership retained: %s",
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                    raise corruption_error from (error.__cause__ or error)

            except Exception as exc:
                # El lifecycle sigue siendo propietario de cualquier conexion
                # obtenida antes del fallo y la cerrara durante shutdown_all().
                self._conn = None
                logger.error("Failed to open async registry: %s", exc)
                raise

        else:
            # ----------------------------------------------------------
            # Backwards-compat path — direct aiosqlite.connect (pre-M-01)
            # ----------------------------------------------------------
            # Does NOT create a DatabaseLifecycleManager internally.
            # Lifecycle managers spawn non-daemon aiosqlite worker threads;
            # on CPython 3.11 those threads block process exit when callers
            # omit close(), causing observed CI 20-minute timeouts.
            self._owns_conn = True
            try:
                self._conn = await aiosqlite.connect(self._db_path)
                self._conn.row_factory = aiosqlite.Row
                await self._conn.execute("PRAGMA journal_mode=WAL")
                await self._conn.execute("PRAGMA foreign_keys=ON")
                await self._conn.execute("PRAGMA busy_timeout=5000")
                await self._conn.execute("PRAGMA synchronous=NORMAL")

                async with self._conn.execute("PRAGMA quick_check") as cur:
                    row = await cur.fetchone()
                    if row is None or str(row[0]).lower() != "ok":
                        raise _DatabaseCorruptionError(f"SQLite integrity check failed for {self._db_path}")

            except _DatabaseCorruptionError:
                with contextlib.suppress(Exception):
                    await self._conn.close()
                self._conn = None

                db_file = pathlib.Path(self._db_path)
                if db_file.exists():
                    backup_path = db_file.with_name(f"{db_file.stem}.corrupt.{int(time.time())}{db_file.suffix}")

                    @retry(
                        retry=retry_if_exception_type(OSError),
                        stop=stop_after_attempt(5),
                        wait=wait_exponential(multiplier=1, min=1, max=10),
                        reraise=True,
                    )
                    async def _do_backup_direct():
                        await asyncio.to_thread(db_file.rename, backup_path)

                    try:
                        await _do_backup_direct()
                        logger.warning("Corrupt database moved to %s. Rebuilding...", backup_path)
                    except OSError as e:
                        logger.error("Failed to backup corrupt database: %s", e)
                        raise

                self._conn = await aiosqlite.connect(self._db_path)
                self._conn.row_factory = aiosqlite.Row
                await self._conn.execute("PRAGMA journal_mode=WAL")
                await self._conn.execute("PRAGMA foreign_keys=ON")
                await self._conn.execute("PRAGMA busy_timeout=5000")
                await self._conn.execute("PRAGMA synchronous=NORMAL")

            except Exception as exc:
                if self._conn is not None:
                    with contextlib.suppress(Exception):
                        await self._conn.close()
                self._conn = None
                logger.error("Failed to open async registry: %s", exc)
                raise

        # El schema es DDL: executescript emite un COMMIT implícito de cualquier
        # transacción pendiente en la conexión compartida, así que en el camino
        # lifecycle debe correr bajo el boundary del path (nunca con SQL ajeno
        # en vuelo). En standalone la conexión es propia y no hace falta.
        if self._lifecycle is not None:
            async with self._lifecycle.operation(self._db_path) as conn:
                self._conn = conn
                await conn.executescript(_SCHEMA_SQL)
        else:
            await self._conn.executescript(_SCHEMA_SQL)

    async def close(self) -> None:
        if self._conn is not None:
            if self._owns_conn:
                # Backwards-compat: we opened the connection directly, we close it.
                with contextlib.suppress(Exception):
                    await self._conn.close()
                self._owns_conn = False
            # Si el lifecycle es externo, no cerramos la conexión aquí;
            # el propietario (LifecycleContext) la cierra en shutdown_all().
            self._conn = None

    # ------------------------------------------------------------------
    # Single-row helpers
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
        if self._conn is None:
            raise RuntimeError("Database is not open")
        params = (nexus_id, name, version, author, category, download_url)
        if self._lifecycle is not None:
            # PR-1b2b: transaction() es el único dueño de commit/rollback sobre
            # la conexión compartida; el retorno sale después del commit.
            async with (
                self._lifecycle.transaction(self._db_path) as conn,
                conn.execute(_UPSERT_MOD_SQL, params) as cur,
            ):
                row = await cur.fetchone()
                if row is None:
                    raise RuntimeError(f"Failed to retrieve mod_id for nexus_id={nexus_id}")
                return int(row[0])
        async with (
            self._write_lock,
            self._conn.execute(_UPSERT_MOD_SQL, params) as cur,
        ):
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError(f"Failed to retrieve mod_id for nexus_id={nexus_id}")
            await self._conn.commit()
            return int(row[0])

    async def set_vfs_status(self, nexus_id: int, *, installed: bool, enabled: bool) -> None:
        """Update the VFS installation and activation status for a mod."""
        if self._conn is None:
            raise RuntimeError("Database is not open")
        params = (int(installed), int(enabled), nexus_id)
        sql = "UPDATE mods SET installed = ?, enabled_in_vfs = ?, updated_at = datetime('now') WHERE nexus_id = ?"
        if self._lifecycle is not None:
            async with self._lifecycle.transaction(self._db_path) as conn:
                await conn.execute(sql, params)
            return
        async with self._write_lock, self._conn.execute(sql, params):
            await self._conn.commit()

    async def get_mod(self, nexus_id: int) -> aiosqlite.Row | None:
        """Return the mod row for *nexus_id*, or ``None``."""
        if self._conn is None:
            raise RuntimeError("Database is not open")
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute("SELECT * FROM mods WHERE nexus_id = ?", (nexus_id,)) as cur,
            ):
                return await cur.fetchone()
        async with self._conn.execute("SELECT * FROM mods WHERE nexus_id = ?", (nexus_id,)) as cur:
            return await cur.fetchone()

    async def is_empty(self) -> bool:
        """Return True if the mods table is empty."""
        if self._conn is None:
            raise RuntimeError("Database is not open")
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute("SELECT COUNT(*) FROM mods") as cur,
            ):
                row = await cur.fetchone()
                return int(row[0]) == 0 if row else True
        async with self._conn.execute("SELECT COUNT(*) FROM mods") as cur:
            row = await cur.fetchone()
            return int(row[0]) == 0 if row else True

    # ------------------------------------------------------------------
    # Public query helpers
    # ------------------------------------------------------------------

    async def search_mods(self, pattern: str) -> list[dict]:
        """Search mods by name with LIKE, escaping wildcards in *pattern*.

        Args:
            pattern: User-supplied search term (``%`` and ``_`` are escaped).

        Returns:
            List of dicts with mod metadata.
        """
        if self._conn is None:
            raise RuntimeError("Database is not open")
        escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = (
            "SELECT mod_id, nexus_id, name, version, installed, enabled_in_vfs FROM mods WHERE name LIKE ? ESCAPE '\\'"
        )
        params = (f"%{escaped}%",)
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, params) as cur,
            ):
                rows = await cur.fetchall()
        else:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [
            {
                "mod_id": row[0],
                "nexus_id": row[1],
                "name": row[2],
                "version": row[3],
                "installed": bool(row[4]),
                "enabled_in_vfs": bool(row[5]),
            }
            for row in rows
        ]

    async def get_all_nexus_ids(self) -> set[int]:
        """Return all registered nexus_id values."""
        if self._conn is None:
            raise RuntimeError("Database is not open")
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute("SELECT nexus_id FROM mods") as cur,
            ):
                return {int(row[0]) for row in await cur.fetchall()}
        async with self._conn.execute("SELECT nexus_id FROM mods") as cur:
            return {int(row[0]) for row in await cur.fetchall()}

    async def get_dependencies(self, mod_id: int) -> list[tuple[int, str]]:
        """Return ``(depends_on_nexus_id, dep_name)`` for *mod_id*."""
        if self._conn is None:
            raise RuntimeError("Database is not open")
        sql = "SELECT depends_on_nexus_id, dep_name FROM dependencies WHERE mod_id = ?"
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, (mod_id,)) as cur,
            ):
                return [(int(row[0]), str(row[1])) for row in await cur.fetchall()]
        async with self._conn.execute(sql, (mod_id,)) as cur:
            return [(int(row[0]), str(row[1])) for row in await cur.fetchall()]

    async def find_missing_masters_for_mods(
        self,
        mod_names: list[str],
    ) -> list[dict]:
        """Find dependencies whose nexus_id is not in the mods table.

        Uses a single LEFT JOIN query instead of loading all IDs in memory.

        Args:
            mod_names: Exact mod names to check.

        Returns:
            List of dicts with mod_name, missing nexus_id, and dep_name.
        """
        if self._conn is None:
            raise RuntimeError("Database is not open")
        if not mod_names:
            return []
        placeholders = ",".join("?" for _ in mod_names)
        sql = (
            "SELECT src.name, d.depends_on_nexus_id, d.dep_name "
            "FROM dependencies d "
            "JOIN mods src ON d.mod_id = src.mod_id "
            "LEFT JOIN mods m ON d.depends_on_nexus_id = m.nexus_id "
            "WHERE m.nexus_id IS NULL AND src.name IN (" + placeholders + ")"  # nosec
        )
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, tuple(mod_names)) as cur,
            ):
                rows = await cur.fetchall()
        else:
            async with self._conn.execute(sql, tuple(mod_names)) as cur:
                rows = await cur.fetchall()
        return [
            {
                "mod": str(row[0]),
                "missing_master_nexus_id": int(row[1]),
                "missing_master_name": str(row[2]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Micro-batched writes
    # ------------------------------------------------------------------

    async def upsert_mods_batch(
        self,
        rows: Sequence[tuple[int, str, str, str, str, str, bool, bool]],
    ) -> None:
        """Batch-upsert mod rows using ``executemany``.

        Each element of *rows* is
        ``(nexus_id, name, version, author, category, download_url)``.
        """
        if not rows:
            return
        if self._conn is None:
            raise RuntimeError("Database is not open")
        if self._lifecycle is not None:
            # El try envuelve el boundary COMPLETO: el commit corre en
            # __aexit__ de transaction() y debe quedar bajo la misma política
            # de error (traducción a DatabaseError) que el executemany.
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    await conn.executemany(_UPSERT_MOD_SQL_BATCH, rows)
            except sqlite3.Error as exc:
                logger.error("Batch upsert failed, rolled back: %s", exc)
                raise DatabaseError(f"upsert_mods_batch failed: {exc}") from exc
        else:
            async with self._write_lock:
                try:
                    await self._conn.executemany(_UPSERT_MOD_SQL_BATCH, rows)
                    await self._conn.commit()
                except sqlite3.Error as exc:
                    await self._conn.rollback()
                    logger.error("Batch upsert failed, rolled back: %s", exc)
                    raise DatabaseError(f"upsert_mods_batch failed: {exc}") from exc
        logger.debug("Batch-upserted %d mod rows", len(rows))

    async def insert_deps_batch(
        self,
        rows: Sequence[tuple[int, int, str]],
    ) -> None:
        """Batch-insert dependency rows using ``executemany``.

        Each element of *rows* is ``(mod_id, depends_on_nexus_id, dep_name)``.
        """
        if not rows:
            return
        if self._conn is None:
            raise RuntimeError("Database is not open")
        if self._lifecycle is not None:
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    await conn.executemany(_INSERT_DEP_SQL, rows)
            except sqlite3.Error as exc:
                logger.error("Batch insert deps failed, rolled back: %s", exc)
                raise DatabaseError(f"insert_deps_batch failed: {exc}") from exc
        else:
            async with self._write_lock:
                try:
                    await self._conn.executemany(_INSERT_DEP_SQL, rows)
                    await self._conn.commit()
                except sqlite3.Error as exc:
                    await self._conn.rollback()
                    logger.error("Batch insert deps failed, rolled back: %s", exc)
                    raise DatabaseError(f"insert_deps_batch failed: {exc}") from exc
        logger.debug("Batch-inserted %d dependency rows", len(rows))

    async def log_tasks_batch(
        self,
        rows: Sequence[tuple[int | None, str, str, str]],
    ) -> None:
        """Batch-insert task log rows using ``executemany``.

        Each element of *rows* is ``(mod_id, action, status, detail)``.
        """
        if not rows:
            return
        if self._conn is None:
            raise RuntimeError("Database is not open")
        if self._lifecycle is not None:
            try:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    await conn.executemany(_LOG_TASK_SQL, rows)
            except sqlite3.Error as exc:
                logger.error("Batch log tasks failed, rolled back: %s", exc)
                raise DatabaseError(f"log_tasks_batch failed: {exc}") from exc
        else:
            async with self._write_lock:
                try:
                    await self._conn.executemany(_LOG_TASK_SQL, rows)
                    await self._conn.commit()
                except sqlite3.Error as exc:
                    await self._conn.rollback()
                    logger.error("Batch log tasks failed, rolled back: %s", exc)
                    raise DatabaseError(f"log_tasks_batch failed: {exc}") from exc
        logger.debug("Batch-logged %d task rows", len(rows))

    async def get_task_log(self, limit: int = 50) -> list[dict[str, object]]:
        """Lee el registro de actividad (``task_log``), lo más reciente primero.

        Hasta ahora la tabla era write-only (la escriben sync_engine, el tool
        install_mod y las descargas fallidas); esta lectura alimenta el
        "Registro de la Puerta" de la sección Descargas del Forge. El nombre
        del mod se resuelve con LEFT JOIN (``mod_name`` es ``None`` para
        eventos sin mod asociado, p. ej. syncs).
        """
        if self._conn is None:
            raise RuntimeError("Database is not open")
        # En SQLite un LIMIT negativo significa "sin límite": un -1 accidental
        # volcaría el historial completo. Clampear a >= 0 (0 → lista vacía).
        limit = max(int(limit), 0)
        sql = """
            SELECT t.action, t.status, t.detail, t.created_at, m.name AS mod_name
            FROM task_log t
            LEFT JOIN mods m ON m.mod_id = t.mod_id
            ORDER BY t.log_id DESC
            LIMIT ?
            """
        if self._lifecycle is not None:
            async with (
                self._lifecycle.operation(self._db_path) as conn,
                conn.execute(sql, (limit,)) as cur,
            ):
                rows = await cur.fetchall()
                columns = [d[0] for d in cur.description]
        else:
            async with self._conn.execute(sql, (limit,)) as cur:
                rows = await cur.fetchall()
                columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]
