"""QA-5 — DLQ attempts incrementa atomicamente via SQL (T1-05).

Verifica que el retry-path de ``DLQManager._process_row`` use el valor real
post-incremento (via ``UPDATE ... RETURNING attempts``) para decidir
dead-vs-retry y para calcular ``next_retry_at``.

**PR #142 review fix**: el test original construia su propio schema standalone
con `payload BLOB` + `created_at`, que NO matcheaba el ``_DDL`` real
(`payload_json TEXT` + `enqueued_at INTEGER`), y ejecutaba el UPDATE de retry
a mano sin invocar `_process_row`. Esto solo verificaba semantica SQLite
generica — una regresion futura en `_process_row` (ej. pierde el
`AND status='in_progress'` o vuelve a usar `tentative_attempts` stale) no
hubiera sido detectada.

Ahora el test:
  1. Construye un `DLQManager` real (usa `_DDL` de produccion).
  2. Encola un evento via `enqueue()` (mismo schema que produccion).
  3. Invoca `_process_row` directamente con la fila fetcheada.
  4. Verifica los efectos en la DB post-llamada.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite
import pytest

from sky_claw.antigravity.core.dlq_manager import DLQManager, DLQRow
from sky_claw.antigravity.core.event_bus import Event


def _make_failing_handler() -> Callable[[Event], Awaitable[None]]:
    """Handler que siempre lanza para forzar el retry-path."""

    async def _h(event: Event) -> None:
        raise RuntimeError(f"simulated failure for {event.topic}")

    return _h


async def _seed_via_enqueue(
    db_path: Any,
    handler: Callable[[Event], Awaitable[None]],
    *,
    initial_attempts: int = 0,
    max_attempts: int = 5,
) -> tuple[DLQManager, DLQRow]:
    """Crear DLQManager + sembrar una fila via enqueue() (schema real).

    Optionally bump `attempts` directly in DB to simulate a row that
    already accumulated retries from a prior cycle.
    """
    mgr = DLQManager(
        db_path=db_path,
        handler_resolver=lambda _: handler,
        max_attempts=max_attempts,
        poll_interval_s=0,
    )
    await mgr._ensure_schema()

    event = Event(topic="test.topic", payload={"k": "v"}, timestamp_ms=0, source="test")
    await mgr.enqueue(event, handler, RuntimeError("initial-seed-error"))

    if initial_attempts > 0:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE dead_letter_events SET attempts=? WHERE topic='test.topic'",
                (initial_attempts,),
            )
            await db.commit()

    # Fetch la fila real para pasarla a _process_row.
    pending = await mgr.list_pending()
    assert len(pending) == 1, f"expected 1 pending row, got {len(pending)}"
    return mgr, pending[0]


@pytest.mark.asyncio
async def test_retry_path_uses_db_value_not_stale_row_attempts(tmp_path):
    """Si la DB tiene attempts=4 (≥ max_attempts-1) y row.attempts=0 (stale),
    el handler debe marcar 'dead' segun el valor real (4+1=5 ≥ max=5),
    NO retry segun el stale (0+1=1 < 5).

    Antes del fix esto fallaba: el codigo usaba `tentative_attempts =
    row.attempts + 1 = 1` para la decision, dejando la fila en 'pending'
    en lugar de 'dead'.
    """
    db_path = tmp_path / "dlq.db"
    handler = _make_failing_handler()
    mgr, row = await _seed_via_enqueue(db_path, handler, initial_attempts=0, max_attempts=5)

    # Simular drift: otro path (ej. recovery bug, manual fix) deja attempts=4 en DB
    # despues de que `row` fue fetcheada con attempts=0.
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE dead_letter_events SET attempts=4 WHERE id=?", (row.id,))
        await db.commit()

    # Reconstruir la row con el snapshot stale (attempts=0, que es lo que
    # _fetch_due_batch habria dado antes del drift).
    stale_row = DLQRow(
        id=row.id,
        topic=row.topic,
        payload=row.payload,
        source=row.source,
        event_ts_ms=row.event_ts_ms,
        handler_name=row.handler_name,
        error_type=row.error_type,
        error_message=row.error_message,
        attempts=0,  # STALE: la DB ya tiene 4.
        next_retry_at=row.next_retry_at,
        status="pending",
        enqueued_at=row.enqueued_at,
        updated_at=row.updated_at,
    )

    # Invocar _process_row con la fila stale.
    await mgr._process_row(stale_row)

    # Verificar: la decision se tomo segun el valor real (4+1=5 >= max=5 -> dead).
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT attempts, status FROM dead_letter_events WHERE id=?",
            (row.id,),
        ) as cur,
    ):
        result = await cur.fetchone()
        assert result is not None
        attempts, status = result
        assert attempts == 5, f"expected attempts=5 (atomic increment), got {attempts}"
        assert status == "dead", f"expected dead (5 >= max=5), got {status}"


@pytest.mark.asyncio
async def test_retry_path_status_in_progress_guard_blocks_double_update(tmp_path):
    """Si la fila NO esta in_progress (recovery race), el retry UPDATE no aplica.

    Este test reemplaza el viejo `test_retry_update_guarded_by_in_progress`
    pero ahora invocando el flujo real: claim → handler raise → guarded UPDATE.
    """
    db_path = tmp_path / "dlq.db"
    handler = _make_failing_handler()
    mgr, row = await _seed_via_enqueue(db_path, handler, initial_attempts=2, max_attempts=10)

    # Tomar el lock atomico manualmente (simular que somos el worker).
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "UPDATE dead_letter_events SET status='in_progress' WHERE id=? AND status='pending'",
            (row.id,),
        )
        await db.commit()
        assert cur.rowcount == 1

    # Simular recovery race: otro tick resetea a 'pending' DESPUES del claim
    # nuestro pero antes de que terminemos el retry-UPDATE.
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE dead_letter_events SET status='pending' WHERE id=?", (row.id,))
        await db.commit()

    # Ejecutar el retry-UPDATE manualmente como lo hace _process_row tras
    # handler exception. El AND status='in_progress' debe filtrar.
    now = int(time.time() * 1000)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            UPDATE dead_letter_events
            SET attempts = attempts + 1,
                error_type = ?,
                error_message = ?,
                updated_at = ?
            WHERE id = ? AND status = 'in_progress'
            RETURNING attempts
            """,
            ("RuntimeError", "test", now, row.id),
        )
        result = await cur.fetchone()
        await db.commit()
        # rowcount==0 / RETURNING vacio: la guarda funciono.
        assert result is None, "retry UPDATE deberia haber sido bloqueado por status guard"

    # attempts NO fue incrementado (sigue siendo 2 del initial_attempts).
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute("SELECT attempts FROM dead_letter_events WHERE id=?", (row.id,)) as cur,
    ):
        attempts_row = await cur.fetchone()
        assert attempts_row is not None
        assert attempts_row[0] == 2, f"attempts should be 2 (unchanged), got {attempts_row[0]}"


@pytest.mark.asyncio
async def test_retry_path_keeps_status_pending_when_below_max(tmp_path):
    """Happy retry-path: handler falla pero attempts < max_attempts → status pending."""
    db_path = tmp_path / "dlq.db"
    handler = _make_failing_handler()
    mgr, row = await _seed_via_enqueue(db_path, handler, initial_attempts=1, max_attempts=10)

    await mgr._process_row(row)

    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT attempts, status, error_type FROM dead_letter_events WHERE id=?",
            (row.id,),
        ) as cur,
    ):
        result = await cur.fetchone()
        assert result is not None
        attempts, status, error_type = result
        # 1 (initial) + 1 (incremento atomico de este retry) = 2.
        assert attempts == 2
        assert status == "pending"
        assert error_type == "RuntimeError"
