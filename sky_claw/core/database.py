import aiosqlite
import json
import logging
from typing import Optional, List, Dict

logger = logging.getLogger("SkyClaw.Database")


class DatabaseAgent:
    """
    Gestor central de base de datos SQLite para Sky-Claw.

    Modo WAL habilitado con pragmas de concurrencia optimizados.
    Contiene esquemas para: scraper, agent_memory, mods, conflicts, activity_log.
    """

    def __init__(self, db_path: str = "sky_claw_state.db"):
        self.db_path = db_path

    async def init_db(self):
        """Inicializa esquemas con modo WAL y pragmas de concurrencia."""
        async with aiosqlite.connect(self.db_path) as db:
            # ── WAL & Concurrency Hardening ──
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=5000")

            # ── Core tables (Scraper / Agent Memory) ──
            await db.execute("""
                CREATE TABLE IF NOT EXISTS scraper_state (
                    domain TEXT PRIMARY KEY,
                    cookies TEXT,
                    failures INTEGER DEFAULT 0,
                    locked_until REAL DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS agent_memory (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                )
            """)

            # ── GUI tables (Mods / Conflicts / Activity Log) ──
            await db.execute("""
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
            await db.execute("""
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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    message TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.commit()
            logger.info(
                "Base de datos SQLite inicializada en modo WAL "
                "(scraper_state, agent_memory, mods, conflicts, activity_log)."
            )

    # ─────────────────────────────────────────────────────────────────────
    # Scraper / Circuit Breaker
    # ─────────────────────────────────────────────────────────────────────

    async def get_circuit_breaker_state(self, domain: str) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM scraper_state WHERE domain = ?", (domain,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else {"failures": 0, "locked_until": 0}

    async def update_circuit_breaker(self, domain: str, failures: int, locked_until: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO scraper_state (domain, failures, locked_until) 
                VALUES (?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET 
                failures=excluded.failures, locked_until=excluded.locked_until
            """, (domain, failures, locked_until))
            await db.commit()

    # ─────────────────────────────────────────────────────────────────────
    # Agent Memory (Key-Value)
    # ─────────────────────────────────────────────────────────────────────

    async def get_memory(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value FROM agent_memory WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def set_memory(self, key: str, value: str, updated_at: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO agent_memory (key, value, updated_at) 
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET 
                value=excluded.value, updated_at=excluded.updated_at
            """, (key, value, updated_at))
            await db.commit()

    # ─────────────────────────────────────────────────────────────────────
    # Mods Repository (consumed by NiceGUI ReactiveState)
    # ─────────────────────────────────────────────────────────────────────

    async def get_mods(self, status: Optional[str] = None) -> List[Dict]:
        """Obtiene lista de mods con filtro opcional por status."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if status:
                async with db.execute(
                    "SELECT * FROM mods WHERE status = ? ORDER BY name", (status,)
                ) as cursor:
                    return [dict(row) for row in await cursor.fetchall()]
            else:
                async with db.execute("SELECT * FROM mods ORDER BY name") as cursor:
                    return [dict(row) for row in await cursor.fetchall()]

    async def add_mod(
        self, name: str, version: str = None, size_mb: float = 0, source: str = None
    ) -> int:
        """Añade o actualiza un mod y devuelve su ID."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO mods (name, version, size_mb, source, status) "
                "VALUES (?, ?, ?, ?, 'active')",
                (name, version, size_mb, source),
            )
            await db.commit()
            async with db.execute("SELECT last_insert_rowid()") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def get_conflicts(self, resolved: Optional[bool] = None) -> List[Dict]:
        """Obtiene conflictos con filtro opcional."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if resolved is not None:
                async with db.execute(
                    "SELECT * FROM conflicts WHERE resolved = ? ORDER BY detected_at DESC",
                    (resolved,),
                ) as cursor:
                    return [dict(row) for row in await cursor.fetchall()]
            else:
                async with db.execute(
                    "SELECT * FROM conflicts ORDER BY detected_at DESC"
                ) as cursor:
                    return [dict(row) for row in await cursor.fetchall()]

    async def log_activity(
        self, event_type: str, message: str, details: Optional[Dict] = None
    ) -> None:
        """Registra actividad en el log."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO activity_log (event_type, message, details) VALUES (?, ?, ?)",
                (event_type, message, json.dumps(details) if details else None),
            )
            await db.commit()
