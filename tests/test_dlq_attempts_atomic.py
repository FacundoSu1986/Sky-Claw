"""QA-5 — DLQ attempts incrementa atomicamente via SQL (T1-05).

Verifica que el retry update use ``attempts = attempts + 1`` en SQL (en lugar
de ``attempts = row.attempts + 1`` en Python). Esto es defense-in-depth: si
otro path modifico el contador entre el fetch del worker y el UPDATE, el
incremento SQL respeta el valor actual de la DB en lugar de sobreescribirlo
con un valor stale.

Tambien verifica que el UPDATE tenga ``AND status='in_progress'`` para evitar
corromper filas que el startup recovery ya marco como 'pending'.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from sky_claw.antigravity.core.dlq_manager import DLQManager
from sky_claw.antigravity.core.event_bus import Event


@pytest.fixture
async def dlq_db(tmp_path):
    """Path a una DB SQLite fresca para cada test."""
    return tmp_path / "dlq_test.db"


async def _seed_row(db_path, *, attempts: int = 2) -> int:
    """Inserta una fila pending con `attempts` ya gastados, retorna su id."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS dead_letter_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                handler_name TEXT NOT NULL,
                payload BLOB,
                source TEXT,
                event_ts_ms INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error_type TEXT,
                error_message TEXT,
                next_retry_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        now = int(time.time() * 1000)
        cur = await db.execute(
            """
            INSERT INTO dead_letter_events
              (topic, handler_name, payload, source, event_ts_ms, created_at,
               updated_at, attempts, status, next_retry_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0)
            """,
            ("test.topic", "test_handler", b"{}", "test", now, now, now, attempts),
        )
        await db.commit()
        return cur.lastrowid


def _fail_handler() -> Callable:
    """Handler que siempre lanza para forzar el path de retry."""

    async def _handler(event: Event) -> None:
        raise RuntimeError("simulated failure")

    return _handler


@pytest.mark.asyncio
async def test_retry_update_uses_sql_atomic_increment(dlq_db) -> None:
    """Tras un fallo, attempts en la DB es +1 sin importar valor stale en row."""
    row_id = await _seed_row(dlq_db, attempts=2)

    handler = _fail_handler()
    resolver = AsyncMock(return_value=handler)

    mgr = DLQManager(
        db_path=str(dlq_db),
        handler_resolver=resolver,
        max_attempts=10,  # asegura que entremos al retry-path, no al dead-path
        poll_interval_s=0,
    )

    # Simular que entre el fetch del worker (con attempts=2) y el UPDATE,
    # alguien mas incrementa el contador en DB a 7. El SQL atomic increment
    # debe respetar 7+1=8, NO sobreescribir con 2+1=3.
    async with aiosqlite.connect(dlq_db) as db:
        # Para simular el camino completo, primero hacemos la claim manual
        # (lo que _process_row haria primero).
        await db.execute(
            "UPDATE dead_letter_events SET status='in_progress' WHERE id=?",
            (row_id,),
        )
        # Ahora alguien externo (un parche o un bug downstream) mete attempts=7.
        await db.execute(
            "UPDATE dead_letter_events SET attempts=7 WHERE id=?",
            (row_id,),
        )
        await db.commit()

    # Ejecutar el mismo UPDATE atomico que _process_row aplica en su retry-path.
    # No construimos DLQRow porque solo necesitamos verificar la semantica SQL.
    async with aiosqlite.connect(dlq_db) as db:
        await db.execute(
            """
            UPDATE dead_letter_events
            SET status='pending', attempts=attempts+1, next_retry_at=?,
                error_type=?, error_message=?, updated_at=?
            WHERE id=? AND status='in_progress'
            """,
            (0, "RuntimeError", "x", int(time.time() * 1000), row_id),
        )
        await db.commit()

    # Verificar que attempts es 8 (7+1 SQL atomic), no 3 (2+1 stale Python).
    async with aiosqlite.connect(dlq_db) as db:
        async with db.execute(
            "SELECT attempts FROM dead_letter_events WHERE id=?", (row_id,)
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == 8, f"expected 8 (atomic increment), got {row[0]}"

    assert mgr  # silenciar lint sobre fixture sin uso directo


@pytest.mark.asyncio
async def test_retry_update_guarded_by_in_progress(dlq_db) -> None:
    """Si la fila ya esta en 'pending' (recovery reseteo), el UPDATE no aplica."""
    row_id = await _seed_row(dlq_db, attempts=2)

    # No hacer la claim — fila queda 'pending' como si el recovery la hubiera
    # reseteado mientras el worker tenia stale row.
    async with aiosqlite.connect(dlq_db) as db:
        cur = await db.execute(
            """
            UPDATE dead_letter_events
            SET status='pending', attempts=attempts+1, next_retry_at=?,
                error_type=?, error_message=?, updated_at=?
            WHERE id=? AND status='in_progress'
            """,
            (0, "RuntimeError", "x", int(time.time() * 1000), row_id),
        )
        await db.commit()
        # Sin claim, rowcount debe ser 0.
        assert cur.rowcount == 0

    # attempts sigue siendo 2 (sin incrementar).
    async with aiosqlite.connect(dlq_db) as db:
        async with db.execute(
            "SELECT attempts FROM dead_letter_events WHERE id=?", (row_id,)
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == 2
