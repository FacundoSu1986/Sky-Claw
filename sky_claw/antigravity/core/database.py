from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from sky_claw.antigravity.core.db_lifecycle import DatabaseLifecycleManager

logger = logging.getLogger("SkyClaw.Database")


class DatabaseAgent:
    """Gestor central de base de datos SQLite para Sky-Claw.

    FASE 1.5.2: Delegates WAL lifecycle management to
    DatabaseLifecycleManager while preserving the identical public API.

    Contiene esquemas para: scraper, agent_memory, mods, conflicts, activity_log.
    """

    def __init__(self, db_path: str = "sky_claw_state.db") -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lifecycle: DatabaseLifecycleManager | None = None

    async def init_db(self) -> None:
        """Inicializa esquemas con modo WAL y pragmas de concurrencia.

        FASE 1.5.2: Uses DatabaseLifecycleManager for WAL recovery and
        hardened pragmas. Maintains identical behavior to the legacy init.
        """
        db_path = Path(self.db_path)

        # FASE 1.5.2: Use lifecycle manager for WAL recovery + pragmas
        self._lifecycle = DatabaseLifecycleManager(db_paths=[db_path])
        await self._lifecycle.init_all()

        # Get the managed connection (M-01: get_connection is async + lazy).
        # get_connection siempre retorna Connection o lanza excepción; no retorna None.
        self._conn = await self._lifecycle.get_connection(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        async with self._write_transaction() as conn:
            # ── Core tables (Scraper / Agent Memory) ──
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS scraper_state (
                    domain TEXT PRIMARY KEY,
                    cookies TEXT,
                    failures INTEGER DEFAULT 0,
                    locked_until REAL DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_memory (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                )
            """)

            # ── GUI tables (Mods / Conflicts / Activity Log) ──
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mods (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    version TEXT,
                    size_mb REAL DEFAULT 0,
                    status TEXT DEFAULT 'inactive',
                    source TEXT,
                    installed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mod_id_1 INTEGER,
                    mod_id_2 INTEGER,
                    conflict_type TEXT,
                    resolved BOOLEAN DEFAULT 0,
                    resolution TEXT,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (mod_id_1) REFERENCES mods(id),
                    FOREIGN KEY (mod_id_2) REFERENCES mods(id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    message TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        logger.info(
            "Base de datos SQLite inicializada en modo WAL "
            "(scraper_state, agent_memory, mods, conflicts, activity_log)."
        )

    async def close(self) -> None:
        """Cierra la conexión persistente con checkpointing de WAL.

        FASE 1.5.2: Delegates to DatabaseLifecycleManager.shutdown_all()
        which executes PRAGMA wal_checkpoint(TRUNCATE) before closing.
        """
        if self._lifecycle is not None:
            await self._lifecycle.shutdown_all()
            self._lifecycle = None
            self._conn = None
        else:
            # Fallback for legacy codepath without lifecycle manager
            conn = self._conn
            if conn:
                self._conn = None
                await conn.close()

    async def _get_conn(self) -> aiosqlite.Connection:
        """Devuelve la conexión persistente; lanza error si no fue inicializada."""
        if self._conn is None:
            raise RuntimeError("DatabaseAgent not initialized. Await init_db() first.")
        return self._conn

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Delega una escritura completa al lifecycle transaccional."""
        if self._lifecycle is None:
            raise RuntimeError("DatabaseAgent not initialized. Await init_db() first.")
        async with self._lifecycle.transaction(self.db_path) as conn:
            yield conn

    # ─────────────────────────────────────────────────────────────────────
    # Scraper / Circuit Breaker
    # ─────────────────────────────────────────────────────────────────────

    async def get_circuit_breaker_state(self, domain: str) -> dict:
        conn = await self._get_conn()
        async with conn.execute("SELECT * FROM scraper_state WHERE domain = ?", (domain,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {"failures": 0, "locked_until": 0}

    async def update_circuit_breaker(self, domain: str, failures: int, locked_until: float) -> None:
        try:
            async with self._write_transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO scraper_state (domain, failures, locked_until)
                    VALUES (?, ?, ?)
                    ON CONFLICT(domain) DO UPDATE SET
                    failures=excluded.failures, locked_until=excluded.locked_until
                """,
                    (domain, failures, locked_until),
                )
        except sqlite3.Error:
            raise

    # ─────────────────────────────────────────────────────────────────────
    # Agent Memory (Key-Value)
    # ─────────────────────────────────────────────────────────────────────

    async def get_memory(self, key: str) -> str | None:
        conn = await self._get_conn()
        async with conn.execute("SELECT value FROM agent_memory WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_memory(self, key: str, value: str, updated_at: float) -> None:
        try:
            async with self._write_transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO agent_memory (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                    (key, value, updated_at),
                )
        except sqlite3.Error:
            raise

    # ─────────────────────────────────────────────────────────────────────
    # Mods Repository (consumed by NiceGUI ReactiveState)
    # ─────────────────────────────────────────────────────────────────────

    async def get_mods(self, status: str | None = None) -> list[dict]:
        """Obtiene lista de mods con filtro opcional por status."""
        conn = await self._get_conn()
        if status:
            async with conn.execute("SELECT * FROM mods WHERE status = ? ORDER BY name", (status,)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        else:
            async with conn.execute("SELECT * FROM mods ORDER BY name") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def add_mod(
        self,
        name: str,
        version: str | None = None,
        size_mb: float = 0,
        source: str | None = None,
    ) -> int:
        """Añade o actualiza un mod y devuelve su ID.

        UPSERT (``ON CONFLICT(name) DO UPDATE``) en vez de ``INSERT OR
        REPLACE``: REPLACE borra+reinserta la fila con un id nuevo, lo que con
        ``foreign_keys=ON`` rompe las FKs de ``conflicts`` y bloquearía
        actualizar un mod con conflicto registrado (review Codex en #220).
        El id se lee por nombre (determinista, sin carrera de
        ``last_insert_rowid()`` en la conexión compartida).
        """
        try:
            async with self._write_transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO mods (name, version, size_mb, source, status)
                    VALUES (?, ?, ?, ?, 'active')
                    ON CONFLICT(name) DO UPDATE SET
                        version = excluded.version,
                        size_mb = excluded.size_mb,
                        source = excluded.source,
                        status = 'active',
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (name, version, size_mb, source),
                )
                async with conn.execute("SELECT id FROM mods WHERE name = ?", (name,)) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else 0
        except sqlite3.Error:
            raise

    async def get_conflicts(self, resolved: bool | None = None) -> list[dict]:
        """Obtiene conflictos con filtro opcional."""
        conn = await self._get_conn()
        if resolved is not None:
            async with conn.execute(
                "SELECT * FROM conflicts WHERE resolved = ? ORDER BY detected_at DESC",
                (resolved,),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        else:
            async with conn.execute("SELECT * FROM conflicts ORDER BY detected_at DESC") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def add_conflict(self, mod_id_1: int, mod_id_2: int, conflict_type: str | None = None) -> int:
        """Registra un conflicto entre dos mods y devuelve su ID.

        El id sale de ``cursor.lastrowid`` del propio INSERT (atómico al
        statement): un ``SELECT last_insert_rowid()`` posterior podría devolver
        el id de otra corrutina que insertó entre awaits en la conexión
        compartida (review de Copilot en #220).
        """
        try:
            async with self._write_transaction() as conn:
                cursor = await conn.execute(
                    "INSERT INTO conflicts (mod_id_1, mod_id_2, conflict_type) VALUES (?, ?, ?)",
                    (mod_id_1, mod_id_2, conflict_type),
                )
                row_id = cursor.lastrowid or 0
        except sqlite3.Error:
            raise
        return row_id

    async def resolve_conflict(self, conflict_id: int, resolution: str | None = None) -> None:
        """Marca un conflicto como resuelto (opcionalmente con una nota)."""
        try:
            async with self._write_transaction() as conn:
                await conn.execute(
                    "UPDATE conflicts SET resolved = 1, resolution = ? WHERE id = ?",
                    (resolution, conflict_id),
                )
        except sqlite3.Error:
            raise

    async def log_activity(self, event_type: str, message: str, details: dict | None = None) -> None:
        """Registra actividad en el log."""
        try:
            async with self._write_transaction() as conn:
                await conn.execute(
                    "INSERT INTO activity_log (event_type, message, details) VALUES (?, ?, ?)",
                    (event_type, message, json.dumps(details) if details else None),
                )
        except sqlite3.Error:
            raise
