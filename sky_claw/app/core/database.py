from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from sky_claw.app.core.db_lifecycle import DatabaseLifecycleManager

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
        # True SOLO tras publicar un lifecycle cuyo DDL ya terminó. Distingue
        # "inicializado y usable" de "lifecycle retenido para reintentar su
        # cierre" (``_lifecycle`` no nulo con ``_conn is None``); sin la
        # bandera, un ``init_db()`` repetido no podría discriminar ambos.
        self._initialized = False
        # Serializa las TRANSICIONES de estado (init_db/close): el contrato
        # de ownership vale solo si dos transiciones no se solapan — dos
        # init concurrentes crearían dos managers y el segundo pisaría al
        # primero sin cerrarlo, y un close a mitad de un init retornaría
        # "cerrado" antes de que el init publique su manager. Las
        # OPERACIONES (lecturas/escrituras) no pasan por este lock: tienen
        # su propio boundary en el lifecycle.
        self._ownership_lock = asyncio.Lock()

    async def _cerrar_lifecycle_propio(
        self,
        lifecycle: DatabaseLifecycleManager,
    ) -> tuple[BaseException | None, asyncio.CancelledError | None]:
        """Cierra un lifecycle propio observando el cierre hasta su final.

        Devuelve ``(error_del_cierre, cancelación_externa)`` sin lanzar:

        - ``error_del_cierre is None`` = cierre confirmado; la referencia al
          manager puede soltarse. Cualquier otro valor = cierre NO
          confirmado; el manager debe QUEDAR referenciado para reintentar
          su cierre, nunca huérfano.
        - ``cancelación_externa`` conserva la identidad de una cancelación
          del llamador recibida DURANTE el cierre. El shutdown corre como
          tarea blindada y se lo observa hasta que termina (mismo patrón
          que ``DatabaseLifecycleManager._await_db_operation``): un
          ``shield`` a secas devolvería el control al primer
          ``CancelledError`` con el shutdown aún corriendo en segundo
          plano, y el llamador propagaría el error equivocado creyendo
          que la limpieza ya terminó.
        """
        tarea_cierre = asyncio.ensure_future(lifecycle.shutdown_all())
        cancelacion: asyncio.CancelledError | None = None
        while not tarea_cierre.done():
            try:
                await asyncio.shield(tarea_cierre)
            except asyncio.CancelledError as error:
                if cancelacion is None:
                    cancelacion = error
            except BaseException:  # noqa: BLE001 - el resultado se lee de la tarea
                break
        try:
            tarea_cierre.result()
        except BaseException as error:  # noqa: BLE001 - se devuelve, no se traga
            return error, cancelacion
        return None, cancelacion

    async def _descartar_lifecycle_retenido(self) -> None:
        """Cierra el manager retenido de un intento anterior antes de crear otro.

        ASUME ``_ownership_lock`` sostenido por el llamador.

        Propiedad del mecanismo (#719): *ningún* ``init_db()`` crea un manager
        nuevo mientras el anterior siga referenciado sin cerrar — reemplazarlo
        sin más perdería el ownership del previo (sus conexiones aiosqlite
        mantienen worker threads vivos). Si el cierre del retenido falla, el
        error se propaga y la referencia se conserva para el próximo intento;
        si lo que llega es una cancelación externa, manda ella.
        """
        retenido = self._lifecycle
        if retenido is None:
            return
        error_cierre, cancelacion = await self._cerrar_lifecycle_propio(retenido)
        if error_cierre is not None:
            self._conn = None
            raise error_cierre
        if cancelacion is not None:
            # El cierre SÍ terminó: el ownership ya puede soltarse, y recién
            # ahora se propaga la cancelación con su identidad intacta.
            self._lifecycle = None
            self._conn = None
            raise cancelacion
        self._lifecycle = None
        self._conn = None

    async def init_db(self) -> None:
        """Inicializa esquemas con modo WAL y pragmas de concurrencia.

        FASE 1.5.2: Uses DatabaseLifecycleManager for WAL recovery and
        hardened pragmas. Maintains identical behavior to the legacy init.

        Contrato de ownership (#719):

        - **Idempotente tras el éxito**: una segunda llamada consecutiva es
          un no-op que conserva el mismo manager y la misma conexión.
        - **Publicación atómica**: ``_lifecycle``/``_conn`` solo se asignan
          cuando el DDL ya terminó; un fallo de ``init_all``,
          ``get_connection``, del DDL/commit o una cancelación no deja una
          conexión publicada como si estuviera inicializada.
        - **Limpieza propietaria**: ante cualquier fallo, el manager creado
          por esta llamada se cierra acá. Si ese cierre también falla, el
          manager queda retenido en ``_lifecycle`` (con ``_conn is None``)
          y el error ORIGINAL se propaga; el próximo ``init_db()`` cierra
          primero el retenido (ver ``_descartar_lifecycle_retenido``).
        - **Serializado con ``close()``**: toda la transición corre bajo
          ``_ownership_lock``; un ``close()`` concurrente espera a que el
          init termine (y cierra lo publicado) en vez de declarar el
          cierre completo con un init todavía en vuelo.
        """
        async with self._ownership_lock:
            await self._init_db_con_lock()

    async def _init_db_con_lock(self) -> None:
        """Cuerpo de ``init_db()``; ASUME ``_ownership_lock`` sostenido."""
        if self._initialized:
            return
        await self._descartar_lifecycle_retenido()

        db_path = Path(self.db_path)

        # FASE 1.5.2: Use lifecycle manager for WAL recovery + pragmas.
        # Todo en variables LOCALES hasta que el esquema esté completo:
        # publicar antes del DDL expondría una conexión "inicializada"
        # sobre un esquema a medio crear.
        lifecycle = DatabaseLifecycleManager(db_paths=[db_path])
        conn: aiosqlite.Connection | None = None
        try:
            await lifecycle.init_all()

            # Get the managed connection (M-01: get_connection is async + lazy).
            # get_connection siempre retorna Connection o lanza excepción; no retorna None.
            conn = await lifecycle.get_connection(self.db_path)
            conn.row_factory = aiosqlite.Row

            async with lifecycle.transaction(self.db_path) as conn:
                await conn.execute("BEGIN IMMEDIATE")
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
        except BaseException as error_original:
            # Limpieza propietaria: el manager creado por ESTA llamada se
            # cierra acá, también en cancelación (``BaseException``), y se lo
            # observa hasta que termina. El error de cierre solo decide
            # ownership (ver el contrato en el docstring); lo que se propaga
            # es el error original — o la cancelación externa recibida
            # durante la limpieza, que manda sobre ambos.
            error_cierre, cancelacion = await self._cerrar_lifecycle_propio(lifecycle)
            self._initialized = False
            if error_cierre is None:
                if self._lifecycle is lifecycle:
                    self._lifecycle = None
                self._conn = None
            else:
                # Cierre no confirmado: se conserva la referencia para que
                # el próximo init_db()/close() reintente el cierre; perderla
                # dejaría el manager huérfano con sus threads vivos.
                self._lifecycle = lifecycle
                self._conn = None
                if not isinstance(error_cierre, asyncio.CancelledError):
                    logger.critical(
                        "DatabaseAgent: no se pudo cerrar el lifecycle tras un fallo de "
                        "inicialización; se conserva el ownership para reintentar el cierre",
                        exc_info=(type(error_cierre), error_cierre, error_cierre.__traceback__),
                    )
            if cancelacion is not None and not isinstance(error_original, asyncio.CancelledError):
                raise cancelacion from error_original
            raise

        # Publicación: recién ahora, con el esquema completo, la conexión
        # pasa a ser "la inicializada" que consumen _get_conn/_read_operation.
        self._lifecycle = lifecycle
        self._conn = conn
        self._initialized = True
        logger.info(
            "Base de datos SQLite inicializada en modo WAL "
            "(scraper_state, agent_memory, mods, conflicts, activity_log)."
        )

    async def close(self) -> None:
        """Cierra la conexión persistente con checkpointing de WAL.

        FASE 1.5.2: Delegates to DatabaseLifecycleManager.shutdown_all()
        which executes PRAGMA wal_checkpoint(TRUNCATE) before closing.

        Seguro de repetir tras éxito, tras un éxito parcial y sin haber
        inicializado (#719). Si el shutdown falla, el error se propaga y el
        ownership se CONSERVA (``_lifecycle`` sigue referenciado) para que
        un cierre posterior lo reintente — el caller de la GUI
        (``close_db_agent``) depende de exactamente esto. Con lifecycle
        presente NO se cierra además ``_conn`` por cuenta propia: esa
        conexión es del lifecycle y cerrarla dos veces sería cerrar un
        recurso de otro propietario.

        Corre bajo el mismo ``_ownership_lock`` que ``init_db()``: si hay
        una inicialización en vuelo, el cierre espera a que publique y
        cierra ese manager, en vez de retornar "cerrado" con un init
        todavía corriendo por detrás.
        """
        async with self._ownership_lock:
            await self._close_con_lock()

    async def _close_con_lock(self) -> None:
        """Cuerpo de ``close()``; ASUME ``_ownership_lock`` sostenido."""
        self._initialized = False
        if self._lifecycle is not None:
            await self._lifecycle.shutdown_all()
            self._lifecycle = None
            self._conn = None
        else:
            # Fallback for legacy codepath without lifecycle manager.
            # La referencia se suelta SOLO tras un cierre confirmado: si
            # ``conn.close()`` falla, ``_conn`` queda para el reintento.
            conn = self._conn
            if conn is not None:
                await conn.close()
                self._conn = None

    async def _get_conn(self) -> aiosqlite.Connection:
        """Devuelve la conexión persistente; lanza error si no fue inicializada."""
        lifecycle = self._lifecycle
        if lifecycle is not None:
            conn = await lifecycle.get_connection(self.db_path)
            conn.row_factory = aiosqlite.Row
            self._conn = conn
            return conn
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

    @asynccontextmanager
    async def _read_operation(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Boundary de lectura: la conexión compartida permanece bajo el
        ``operation()`` del lifecycle durante TODO el SQL de la lectura.

        Antes, las lecturas resolvían la conexión con ``get_connection()`` —que
        suelta la admisión al retornar— y recién después emitían el SELECT: una
        ventana en la que ``shutdown_all()`` podía cerrar la conexión debajo de
        la query (``ValueError: no active connection``). El camino sin lifecycle
        conserva su semántica previa (``_get_conn`` legacy).
        """
        if self._lifecycle is not None:
            async with self._lifecycle.operation(self.db_path) as conn:
                conn.row_factory = aiosqlite.Row
                yield conn
        else:
            if self._conn is None:
                raise RuntimeError("DatabaseAgent not initialized. Await init_db() first.")
            yield self._conn

    # ─────────────────────────────────────────────────────────────────────
    # Scraper / Circuit Breaker
    # ─────────────────────────────────────────────────────────────────────

    async def get_circuit_breaker_state(self, domain: str) -> dict:
        async with (
            self._read_operation() as conn,
            conn.execute("SELECT * FROM scraper_state WHERE domain = ?", (domain,)) as cursor,
        ):
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
        async with (
            self._read_operation() as conn,
            conn.execute("SELECT value FROM agent_memory WHERE key = ?", (key,)) as cursor,
        ):
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
        async with self._read_operation() as conn:
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
        async with self._read_operation() as conn:
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
