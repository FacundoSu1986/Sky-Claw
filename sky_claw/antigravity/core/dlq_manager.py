"""DLQManager — Dead Letter Queue respaldada en SQLite para el CoreEventBus.

Persiste eventos fallidos y los reintenta con backoff exponencial (2 s → 32 s, 5 intentos).
El worker corre como asyncio.Task acoplado al ciclo de vida del bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from sky_claw.antigravity.core.event_bus import Event, Subscriber

if TYPE_CHECKING:
    pass

logger = logging.getLogger("SkyClaw.DLQ")

_DDL = """
CREATE TABLE IF NOT EXISTS dead_letter_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT    NOT NULL,
    payload_json  TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    event_ts_ms   INTEGER NOT NULL,
    handler_name  TEXT    NOT NULL,
    error_type    TEXT    NOT NULL,
    error_message TEXT    NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    enqueued_at   INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    CHECK(status IN ('pending', 'in_progress', 'dead'))
);
CREATE INDEX IF NOT EXISTS idx_dlq_status_retry
    ON dead_letter_events(status, next_retry_at);
"""


def _default_now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class DLQRow:
    """Representa una fila de la tabla dead_letter_events."""

    id: int
    topic: str
    payload: dict
    source: str
    event_ts_ms: int
    handler_name: str
    error_type: str
    error_message: str
    attempts: int
    next_retry_at: int
    status: str
    enqueued_at: int
    updated_at: int


class DLQManager:
    """Gestiona la Dead Letter Queue: persiste eventos fallidos y los reintenta.

    Args:
        db_path: Ruta al archivo SQLite de la DLQ.
        handler_resolver: Callable que dado un handler_name retorna el Subscriber o None.
        max_attempts: Número máximo de intentos antes de marcar como 'dead'.
        base_backoff_s: Base del backoff exponencial (delay = base^attempt segundos).
        poll_interval_s: Segundos entre polls cuando la DLQ está vacía.
        batch_size: Máximo de filas procesadas por tick.
        clock: Función que retorna epoch en ms (inyectable para tests).
    """

    def __init__(
        self,
        db_path: Path,
        handler_resolver: Callable[[str], Subscriber | None],
        *,
        max_attempts: int = 5,
        base_backoff_s: int = 2,
        poll_interval_s: float = 1.0,
        batch_size: int = 50,
        clock: Callable[[], int] = _default_now_ms,
    ) -> None:
        self._db_path = db_path
        self._handler_resolver = handler_resolver
        self._max_attempts = max_attempts
        self._base_backoff_s = base_backoff_s
        self._poll_interval_s = poll_interval_s
        self._batch_size = batch_size
        self._clock = clock
        self._retry_task: asyncio.Task[None] | None = None
        self._schema_ensured: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Inicia el worker de reintento como asyncio.Task de fondo."""
        if self._retry_task is not None:
            logger.warning("DLQManager ya está corriendo, ignorando start() duplicado")
            return
        self._retry_task = asyncio.create_task(self._retry_loop(), name="dlq-retry-worker")
        logger.info("DLQManager iniciado (db=%s)", self._db_path)

    async def stop(self) -> None:
        """Detiene el worker de reintento de forma grácil."""
        if self._retry_task is None:
            return
        self._retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._retry_task
        self._retry_task = None
        logger.info("DLQManager detenido")

    async def enqueue(self, event: Event, handler: Subscriber, exc: BaseException) -> None:
        """Persiste un evento fallido en la DLQ.

        Args:
            event: El evento que causó el fallo.
            handler: El callback que lanzó la excepción.
            exc: La excepción capturada.
        """
        await self._ensure_schema()
        now = self._clock()
        name = self._handler_name(handler)
        next_retry = self._compute_next_retry_at(now, 1, self._base_backoff_s)
        payload_json = json.dumps(event.payload, sort_keys=True, default=str)

        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO dead_letter_events
                    (topic, payload_json, source, event_ts_ms, handler_name,
                     error_type, error_message, attempts, next_retry_at,
                     status, enqueued_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'pending', ?, ?)
                """,
                (
                    event.topic,
                    payload_json,
                    event.source,
                    event.timestamp_ms,
                    name,
                    type(exc).__name__,
                    str(exc),
                    next_retry,
                    now,
                    now,
                ),
            )
            await db.commit()

        logger.debug(
            "DLQ: encolado evento '%s' desde handler '%s' (error: %s)",
            event.topic,
            name,
            type(exc).__name__,
        )

    async def list_pending(self) -> list[DLQRow]:
        """Retorna todas las filas con status='pending'."""
        await self._ensure_schema()
        return await self._select_by_status("pending")

    async def list_dead(self) -> list[DLQRow]:
        """Retorna todas las filas con status='dead'."""
        await self._ensure_schema()
        return await self._select_by_status("dead")

    # ------------------------------------------------------------------
    # Internal — schema / connection
    # ------------------------------------------------------------------

    async def _ensure_schema(self) -> None:
        """Crea el directorio y la tabla si no existen."""
        if self._schema_ensured:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.executescript(_DDL)
            await db.commit()
        self._schema_ensured = True

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Abre conexión con pragmas de producción."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            yield db

    # ------------------------------------------------------------------
    # Internal — retry loop
    # ------------------------------------------------------------------

    async def _retry_loop(self) -> None:
        """Loop de reintento que corre como tarea de fondo."""
        await self._ensure_schema()
        try:
            while True:
                try:
                    rows = await self._fetch_due_batch(limit=self._batch_size)
                    if not rows:
                        # Yield to event loop even when poll_interval_s==0 to avoid
                        # a busy-spin and allow aiosqlite callbacks to progress.
                        await asyncio.sleep(self._poll_interval_s)
                        continue
                    for row in rows:
                        await self._process_row(row)
                    # After processing a batch: if poll_interval_s==0 still yield so
                    # other coroutines (e.g. test assertions) get a chance to run.
                    if self._poll_interval_s == 0:
                        await asyncio.sleep(0)
                except Exception as exc:
                    logger.error(
                        "DLQ worker error (continuando): %s",
                        exc,
                        exc_info=True,
                    )
                    await asyncio.sleep(self._poll_interval_s)
        except asyncio.CancelledError:
            raise

    async def _process_row(self, row: DLQRow) -> None:
        """Procesa una sola fila: resuelve handler, reintenta, actualiza estado."""
        now = self._clock()
        async with self._connect() as db:
            await db.execute(
                "UPDATE dead_letter_events SET status='in_progress', updated_at=? WHERE id=?",
                (now, row.id),
            )
            await db.commit()

        handler = self._handler_resolver(row.handler_name)
        if handler is None:
            # Handler no registrado aún: tratar como transient, reintent en 60s
            now2 = self._clock()
            next_retry = now2 + 60_000  # Retry en 1 minuto
            async with self._connect() as db:
                await db.execute(
                    "UPDATE dead_letter_events SET status='pending', next_retry_at=?, updated_at=? WHERE id=?",
                    (next_retry, now2, row.id),
                )
                await db.commit()
            logger.debug(
                "DLQ: handler '%s' no registrado aún, reintentando en 60s",
                row.handler_name,
            )
            return

        event = Event(
            topic=row.topic,
            payload=row.payload,
            timestamp_ms=row.event_ts_ms,
            source=row.source,
        )
        try:
            await handler(event)
        except Exception as exc:
            new_attempts = row.attempts + 1
            if new_attempts >= self._max_attempts:
                await self._mark_dead(row, str(exc), attempts=new_attempts)
                logger.warning(
                    "DLQ: evento '%s' handler '%s' agotó %d intentos → dead",
                    row.topic,
                    row.handler_name,
                    self._max_attempts,
                )
            else:
                now2 = self._clock()
                next_retry = self._compute_next_retry_at(now2, new_attempts, self._base_backoff_s)
                async with self._connect() as db:
                    await db.execute(
                        """
                        UPDATE dead_letter_events
                        SET status='pending', attempts=?, next_retry_at=?,
                            error_type=?, error_message=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            new_attempts,
                            next_retry,
                            type(exc).__name__,
                            str(exc),
                            now2,
                            row.id,
                        ),
                    )
                    await db.commit()
        else:
            async with self._connect() as db:
                await db.execute("DELETE FROM dead_letter_events WHERE id=?", (row.id,))
                await db.commit()
            logger.info(
                "DLQ: evento '%s' handler '%s' reintentado con éxito (intentos=%d)",
                row.topic,
                row.handler_name,
                row.attempts + 1,
            )

    async def _mark_dead(self, row: DLQRow, reason: str, *, attempts: int | None = None) -> None:
        """Marca una fila como 'dead' con el motivo dado."""
        now = self._clock()
        final_attempts = attempts if attempts is not None else row.attempts
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE dead_letter_events
                SET status='dead', error_message=?, updated_at=?, attempts=?
                WHERE id=?
                """,
                (reason, now, final_attempts, row.id),
            )
            await db.commit()

    async def _fetch_due_batch(self, limit: int) -> list[DLQRow]:
        """Retorna filas pendientes cuyo next_retry_at ya venció.

        También recupera filas `in_progress` stancadas (crash/cancel) y las marca como `pending`.
        """
        now = self._clock()
        await self._ensure_schema()

        # Recuperar filas stancadas: in_progress + updated_at + 60s < now → pending
        async with self._connect() as db:
            await db.execute(
                "UPDATE dead_letter_events SET status='pending' WHERE status='in_progress' AND updated_at + 60000 < ?",
                (now,),
            )
            await db.commit()

        async with (
            self._connect() as db,
            db.execute(
                """
                SELECT id, topic, payload_json, source, event_ts_ms, handler_name,
                       error_type, error_message, attempts, next_retry_at,
                       status, enqueued_at, updated_at
                FROM dead_letter_events
                WHERE status = 'pending' AND next_retry_at <= ?
                ORDER BY next_retry_at ASC
                LIMIT ?
                """,
                (now, limit),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [self._row_from_sqlite(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    async def _select_by_status(self, status: str) -> list[DLQRow]:
        async with (
            self._connect() as db,
            db.execute(
                """
                SELECT id, topic, payload_json, source, event_ts_ms, handler_name,
                       error_type, error_message, attempts, next_retry_at,
                       status, enqueued_at, updated_at
                FROM dead_letter_events
                WHERE status = ?
                ORDER BY id ASC
                """,
                (status,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [self._row_from_sqlite(r) for r in rows]

    @staticmethod
    def _row_from_sqlite(row: aiosqlite.Row) -> DLQRow:
        return DLQRow(
            id=row["id"],
            topic=row["topic"],
            payload=json.loads(row["payload_json"]),
            source=row["source"],
            event_ts_ms=row["event_ts_ms"],
            handler_name=row["handler_name"],
            error_type=row["error_type"],
            error_message=row["error_message"],
            attempts=row["attempts"],
            next_retry_at=row["next_retry_at"],
            status=row["status"],
            enqueued_at=row["enqueued_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _compute_next_retry_at(now_ms: int, attempt: int, base_backoff_s: int = 2) -> int:
        """Calcula el epoch ms para el próximo intento: now + base^attempt * 1000 ms."""
        return now_ms + (base_backoff_s**attempt) * 1000

    @staticmethod
    def _handler_name(cb: Subscriber) -> str:
        """Genera un identificador estable para un callable."""
        mod = getattr(cb, "__module__", "unknown")
        qn = getattr(cb, "__qualname__", None)
        return f"{mod}.{qn}" if qn else repr(cb)
