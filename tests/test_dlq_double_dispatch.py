"""
H-05: Tests de atomic claim para DLQManager.

El bug: dos instancias DLQManager que comparten la misma DB pueden hacer SELECT
de la misma fila 'pending' y, dado que el UPDATE a 'in_progress' no tiene la
guarda 'AND status='pending'', ambas ejecutan el handler → double dispatch.

El fix (atomic claim): el UPDATE incluye 'AND status='pending'' y verifica
cur.rowcount == 0 para retornar temprano sin llamar al handler.

Estos tests FALLAN con la implementación actual y PASAN tras el fix H-05.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from sky_claw.antigravity.core.dlq_manager import DLQManager
from sky_claw.antigravity.core.event_bus import Event
from tests.polling_utils import poll_until

_TOPIC = "probe.double"


def _make_dlq(db_path: Path, handler_resolver: dict, *, clock_value: int = 5_000_000) -> DLQManager:
    """Factory con reloj fijo a un valor grande para que las filas con next_retry_at=0 sean siempre elegibles."""
    return DLQManager(
        db_path=db_path,
        handler_resolver=handler_resolver.get,
        max_attempts=5,
        poll_interval_s=0,
        clock=lambda: clock_value,
    )


async def _insert_pending_row(db_path: Path, handler_name: str) -> None:
    """Inserta una fila 'pending' directamente en la DB con next_retry_at=0 (siempre elegible)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO dead_letter_events
                (topic, payload_json, source, event_ts_ms, handler_name,
                 error_type, error_message, attempts, next_retry_at,
                 status, enqueued_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?, ?, 0, 0, 'pending', 0, 0)
            """,
            (_TOPIC, "{}", "test", handler_name, "RuntimeError", "initial_failure"),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Test 1 — double dispatch en acceso concurrente (CORE del bug H-05)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_process_row_calls_handler_exactly_once(
    tmp_path: Path,
) -> None:
    """H-05 RED: dos managers llaman al handler 2 veces sobre la misma fila (broken).
    H-05 GREEN: atomic claim garantiza exactamente 1 ejecución (fixed).
    """
    calls: list[Event] = []

    async def handler(event: Event) -> None:
        calls.append(event)

    handler_name = DLQManager._handler_name(handler)
    db_path = tmp_path / "shared.db"
    resolver = {handler_name: handler}

    # Bootstrap schema + insertar fila con next_retry_at=0 (siempre elegible)
    dlq1 = _make_dlq(db_path, resolver)
    await dlq1._ensure_schema()
    await _insert_pending_row(db_path, handler_name)

    # Obtener la fila via dlq1
    rows = await dlq1._fetch_due_batch(limit=10)
    assert len(rows) == 1, f"Pre-condición: debe haber 1 fila pending. Obtenidas: {len(rows)}"
    row = rows[0]

    # Segundo manager que comparte la misma DB (simula multi-worker / race condition)
    dlq2 = _make_dlq(db_path, resolver)
    await dlq2._ensure_schema()

    # Race: ambos managers procesan la MISMA fila concurrentemente.
    # RED: ambos hacen UPDATE sin guarda 'AND status=pending' → ambos llaman al handler → len(calls) == 2
    # GREEN: atomic claim → sólo el primero tiene rowcount=1 → len(calls) == 1
    await asyncio.gather(
        dlq1._process_row(row),
        dlq2._process_row(row),
    )

    assert len(calls) == 1, (
        f"H-05: el handler debe ejecutarse exactamente 1 vez. "
        f"Se ejecutó {len(calls)} veces → double dispatch detectado."
    )


# ---------------------------------------------------------------------------
# Test 2 — startup recovery: filas in_progress se recuperan al arrancar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_progress_rows_recovered_on_startup(tmp_path: Path) -> None:
    """H-05: una fila 'in_progress' de un crash previo se recupera al arrancar el worker.

    Tras el fix, la recovery se mueve de _fetch_due_batch() (cada poll) a
    _recover_stale_rows(), invocado una vez al inicio de _retry_loop() (por ciclo
    start/stop). Este test verifica que la propiedad de recovery se preserva y que
    re-corre correctamente en ciclos stop()/start() de la misma instancia.
    """
    db_path = tmp_path / "recovery.db"

    # Bootstrap schema con un manager temporal
    dlq_setup = _make_dlq(db_path, {})
    await dlq_setup._ensure_schema()

    # Simular crash: insertar fila directamente como 'in_progress' con updated_at=0
    # (muy antigua → debe ser recuperada)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO dead_letter_events
                (topic, payload_json, source, event_ts_ms, handler_name,
                 error_type, error_message, attempts, next_retry_at,
                 status, enqueued_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?, ?, 0, 0, 'in_progress', 0, 0)
            """,
            (_TOPIC, "{}", "test", "test_handler", "RuntimeError", "crash_simulated"),
        )
        await db.commit()

    # Pre-condición: fila está in_progress
    async with aiosqlite.connect(db_path) as db, db.execute("SELECT status FROM dead_letter_events") as cur:
        row_before = await cur.fetchone()
    assert row_before is not None and row_before[0] == "in_progress", (
        "Pre-condición fallida: la fila debe estar in_progress"
    )

    # Nuevo arranque del DLQManager con clock_value=5_000_000
    # La recovery corre en _retry_loop() startup: updated_at(0) + 60000 < 5_000_000 → True
    dlq_new = _make_dlq(db_path, {}, clock_value=5_000_000)
    await dlq_new.start()

    async def _row_is_pending() -> bool:
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute("SELECT status FROM dead_letter_events") as cur,
        ):
            row = await cur.fetchone()
        return row is not None and row[0] == "pending"

    try:
        await poll_until(
            _row_is_pending,
            timeout=30.0,
            msg="H-05: la fila in_progress debe recuperarse a 'pending' al arrancar el worker",
        )
    finally:
        await dlq_new.stop()


@pytest.mark.asyncio
async def test_ensure_schema_does_not_recover_in_progress_rows(tmp_path: Path) -> None:
    """_ensure_schema() no debe mutar filas in_progress en flujos no-worker."""
    db_path = tmp_path / "schema_only.db"

    dlq_setup = _make_dlq(db_path, {})
    await dlq_setup._ensure_schema()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO dead_letter_events
                (topic, payload_json, source, event_ts_ms, handler_name,
                 error_type, error_message, attempts, next_retry_at,
                 status, enqueued_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?, ?, 0, 0, 'in_progress', 0, 0)
            """,
            (_TOPIC, "{}", "test", "test_handler", "RuntimeError", "crash_simulated"),
        )
        await db.commit()

    dlq_reader = _make_dlq(db_path, {}, clock_value=5_000_000)
    await dlq_reader._ensure_schema()

    async with aiosqlite.connect(db_path) as db, db.execute("SELECT status FROM dead_letter_events") as cur:
        row = await cur.fetchone()

    assert row is not None and row[0] == "in_progress"
