"""Unit tests para DLQManager.
Todos los tests usan tmp_path para aislar la DB — nunca escriben en ~/.sky_claw/.
El reloj se inyecta para controlar backoff sin asyncio.sleep reales.
"""
from __future__ import annotations
import asyncio
from pathlib import Path
import aiosqlite
import pytest
from sky_claw.core.dlq_manager import DLQManager
from sky_claw.core.event_bus import Event, Subscriber
from tests.polling_utils import poll_until

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_TOPIC = "notif.critical"
_PAYLOAD = {"msg": "test"}
_SOURCE = "test-source"

def _make_event() -> Event:
    return Event(topic=_TOPIC, payload=_PAYLOAD, source=_SOURCE)

def _make_dlq(
    tmp_path: Path,
    resolver: dict[str, Subscriber] | None = None,
    *,
    clock_values: list[int] | None = None,
    max_attempts: int = 5,
) -> DLQManager:
    """Factory de conveniencia para tests."""
    reg: dict[str, Subscriber] = resolver or {}
    if clock_values is not None:
        it = iter(clock_values)
        clock = lambda: next(it)  # noqa: E731
    else:
        clock = lambda: 1_000_000  # noqa: E731  epoch ms fijo
    return DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver=reg.get,
        max_attempts=max_attempts,
        clock=clock,
    )

async def _always_fail(event: Event) -> None:
    raise RuntimeError("boom")

# ---------------------------------------------------------------------------
# Test #1 — schema bootstrap
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_schema_bootstraps_on_first_use(tmp_path: Path) -> None:
    """enqueue() crea directorios + tabla + índice si no existen."""
    db_file = tmp_path / "sub" / "deep" / "dlq.db"
    dlq = DLQManager(
        db_path=db_file,
        handler_resolver={}.get,
    )
    exc = RuntimeError("x")
    await dlq.enqueue(_make_event(), _always_fail, exc)
    assert db_file.exists(), "El archivo de DB debe haberse creado"
    async with aiosqlite.connect(db_file) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dead_letter_events'") as cur:
            row = await cur.fetchone()
        assert row is not None, "La tabla dead_letter_events debe existir"
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_dlq_status_retry'"
        ) as cur:
            idx = await cur.fetchone()
        assert idx is not None, "El índice idx_dlq_status_retry debe existir"

# ---------------------------------------------------------------------------
# Test #2 — enqueue/list
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_enqueue_persists_event_data(tmp_path: Path) -> None:
    """Los campos del evento y el error se guardan correctamente."""
    dlq = _make_dlq(tmp_path)
    event = _make_event()
    await dlq.enqueue(event, _always_fail, ValueError("invalid value"))
    rows = await dlq.list_pending()
    assert len(rows) == 1
    r = rows[0]
    assert r.topic == _TOPIC
    assert r.payload == _PAYLOAD
    assert r.source == _SOURCE
    assert r.handler_name.endswith("_always_fail")
    assert r.error_type == "ValueError"
    assert r.error_message == "invalid value"
    assert r.status == "pending"
    assert r.attempts == 0

# ---------------------------------------------------------------------------
# Test #3 — list_dead
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_dead_only_returns_dead_rows(tmp_path: Path) -> None:
    """list_dead() filtra por status='dead'."""
    db_file = tmp_path / "dlq.db"
    dlq = DLQManager(db_path=db_file, handler_resolver={}.get)
    await dlq.enqueue(_make_event(), _always_fail, RuntimeError("x"))
    # Forzar una fila a dead manualmente vía SQL
    async with aiosqlite.connect(db_file) as db:
        await db.execute("UPDATE dead_letter_events SET status='dead'")
        await db.commit()
    dead = await dlq.list_dead()
    pending = await dlq.list_pending()
    assert len(dead) == 1
    assert len(pending) == 0

# ---------------------------------------------------------------------------
# Test #4 — retry loop (integration)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retry_loop_eventually_marks_dead(tmp_path: Path) -> None:
    """El worker reintenta hasta agotar max_attempts y marca como dead."""
    # Reloj que avanza 1 hora en cada llamada para saltar el backoff
    tick = [0]
    def advancing_clock() -> int:
        t = tick[0]
        tick[0] += 3600 * 1000
        return t
    dlq = DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver={CoreEventBus._handler_name(_always_fail): _always_fail}.get,
        max_attempts=5,
        poll_interval_s=0,
        clock=advancing_clock,
    )
    await dlq.enqueue(_make_event(), _always_fail, RuntimeError("initial"))
    await dlq.start()
    # Polling determinístico: espera hasta que la fila pase a 'dead' tras 5 intentos.
    async def has_dead_row() -> bool:
        return len(await dlq.list_dead()) == 1
    await poll_until(has_dead_row, timeout=30.0)
    await dlq.stop()
    dead = await dlq.list_dead()
    pending = await dlq.list_pending()
    assert len(dead) == 1, "Debe haber exactamente 1 fila dead"
    assert len(pending) == 0, "No deben quedar filas pending"
    assert dead[0].attempts == 5
    assert dead[0].status == "dead"

# ---------------------------------------------------------------------------
# Test #5 — backoff exponencial
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_backoff_schedule_is_exponential(tmp_path: Path) -> None:
    """Los deltas de next_retry_at deben ser 2000, 4000, 8000, 16000 ms."""
    async def always_fail_handler(event: Event) -> None:
        raise RuntimeError("boom")
    # Enqueue con clock fijo para verificar primer next_retry_at
    fixed_dlq = DLQManager(
        db_path=tmp_path / "fixed.db",
        handler_resolver={}.get,
        clock=lambda: 0,
    )
    await fixed_dlq.enqueue(_make_event(), always_fail_handler, RuntimeError("x"))
    rows = await fixed_dlq.list_pending()
    assert rows[0].next_retry_at == 2000, f"Primer next_retry_at debe ser 2000 ms, got {rows[0].next_retry_at}"
    # Verificar fórmula para cada attempt
    expected_deltas = [2000, 4000, 8000, 16000]
    for attempt, expected in enumerate(expected_deltas, start=1):
        result = DLQManager._compute_next_retry_at(0, attempt, base_backoff_s=2)
        assert result == expected, f"attempt={attempt}: esperado {expected}, got {result}"

# ---------------------------------------------------------------------------
# Test #6 — handler no registrado → transient (reintenta luego)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_handler_is_transient(tmp_path: Path) -> None:
    """Si handler_resolver retorna None, la fila vuelve a pending (transient, no dead)."""
    async def some_handler(event: Event) -> None:
        pass
    # Clock que no avanza mucho — para que next_retry_at quede en el futuro
    fixed_now = [1_000_000]
    def fixed_clock() -> int:
        return fixed_now[0]
    # Resolver vacío — handler no encontrado
    dlq = DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver={}.get,
        poll_interval_s=0,
        clock=fixed_clock,
    )
    await dlq.enqueue(_make_event(), some_handler, RuntimeError("x"))
    await dlq.start()
    await asyncio.sleep(0.1)
    await dlq.stop()
    # Debe quedar pending (transient, no dead), y next_retry_at > updated_at
    pending = await dlq.list_pending()
    dead = await dlq.list_dead()
    assert len(pending) == 1  # Transient: será reintentado
    assert len(dead) == 0
    assert pending[0].next_retry_at > pending[0].updated_at  # Scheduled para reintento

# ---------------------------------------------------------------------------
# Test #7 — batch limit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_batch_limit_respected(tmp_path: Path) -> None:
    """Con 100 filas pending vencidas, _fetch_due_batch(limit=50) retorna exactamente 50."""
    dlq = DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver={}.get,
        clock=lambda: 0,
    )
    async def dummy_handler(e: Event) -> None:
        raise RuntimeError("x")
    for _ in range(100):
        await dlq.enqueue(_make_event(), dummy_handler, RuntimeError("x"))
    # Con clock=lambda: 0, next_retry_at=2000 → aún no vencido si now=0
    # Necesitamos next_retry_at <= now. Usamos now grande:
    dlq._clock = lambda: 999_999_999
    batch = await dlq._fetch_due_batch(limit=50)
    assert len(batch) == 50

# ---------------------------------------------------------------------------
# Test #8 — stop grácil
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stop_cancels_worker_gracefully(tmp_path: Path) -> None:
    """stop() completa sin raise aunque el worker esté en asyncio.sleep."""
    dlq = DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver={}.get,
        poll_interval_s=60,  # sleep largo para forzar cancelación en mitad
    )
    await dlq.start()
    assert dlq._retry_task is not None
    assert not dlq._retry_task.done()
    # stop() no debe lanzar ni quedarse colgado
    await asyncio.wait_for(dlq.stop(), timeout=2.0)
    assert dlq._retry_task is None or dlq._retry_task.done()
