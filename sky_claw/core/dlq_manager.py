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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from sky_claw.core.event_bus import Event, Subscriber

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
    updated_at    INTEGER NOT NULL
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
        base_backoff_s: Segundos base para el backoff (delay = base ** attempts).
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

    async def start(self) -> None:
        """Inicia el worker de reintento como asyncio.Task de fondo."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Detiene el worker de reintento de forma grácil."""
        raise NotImplementedError

    async def enqueue(self, event: Event, handler: Subscriber, exc: BaseException) -> None:
        """Persiste un evento fallido en la DLQ.

        Args:
            event: El evento que causó el fallo.
            handler: El callback que lanzó la excepción.
            exc: La excepción capturada.
        """
        raise NotImplementedError

    async def list_pending(self) -> list[DLQRow]:
        """Retorna todas las filas con status='pending'."""
        raise NotImplementedError

    async def list_dead(self) -> list[DLQRow]:
        """Retorna todas las filas con status='dead'."""
        raise NotImplementedError

    async def _ensure_schema(self) -> None:
        """Crea el directorio y la tabla si no existen."""
        raise NotImplementedError

    async def _retry_loop(self) -> None:
        """Loop de reintento que corre como tarea de fondo."""
        raise NotImplementedError

    async def _process_row(self, row: DLQRow) -> None:
        """Procesa una sola fila: resuelve handler, reintenta, actualiza estado."""
        raise NotImplementedError

    async def _mark_dead(self, row: DLQRow, reason: str) -> None:
        """Marca una fila como 'dead' con el motivo dado."""
        raise NotImplementedError

    async def _fetch_due_batch(self, limit: int) -> list[DLQRow]:
        """Retorna filas pendientes cuyo next_retry_at ya venció."""
        raise NotImplementedError

    def _next_retry_at(self, attempts: int) -> int:
        """Calcula el epoch ms para el próximo intento usando backoff exponencial."""
        raise NotImplementedError

    @staticmethod
    def _handler_name(cb: Subscriber) -> str:
        """Genera un identificador estable para un callable."""
        raise NotImplementedError

    async def _connect(self) -> aiosqlite.Connection:
        """Abre conexión con pragmas de producción."""
        raise NotImplementedError
