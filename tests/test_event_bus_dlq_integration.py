"""Integración CoreEventBus + DLQManager.

Verifica el contrato de durabilidad end-to-end:
- handler que falla → evento en DLQ
- retry exitoso → fila eliminada
- create_bus_with_dlq() lifecycle completo
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sky_claw.core.dlq_manager import DLQManager, DLQRow
from sky_claw.core.event_bus import CoreEventBus, Event, create_bus_with_dlq
from tests.polling_utils import poll_until


async def _poll_pending(dlq: DLQManager, *, min_count: int = 1, timeout: float = 3.0) -> list[DLQRow]:
    """Consulta dlq.list_pending() hasta que aparezcan al menos *min_count* filas.

    Retorna las filas una vez alcanzado el umbral.  Si *min_count* no aparece
    antes de *timeout*, propaga el ``AssertionError`` lanzado por
    ``poll_until``.
    """
    rows: list[DLQRow] = []

    async def _check() -> bool:
        nonlocal rows
        rows = await dlq.list_pending()
        return len(rows) >= min_count

    await poll_until(_check, timeout=timeout, msg=f"DLQ no alcanzó {min_count} filas pending")
    return rows


@pytest.mark.asyncio
async def test_failing_handler_goes_to_dlq(tmp_path: Path) -> None:
    """Handler que lanza excepción → evento persiste en DLQ con campos correctos."""
    bus = CoreEventBus()
    dlq = DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver=bus._handler_index.get,
    )
    bus._dlq = dlq
    await dlq._ensure_schema()  # pre-create schema to avoid SQLite locking race
    await bus.start()

    async def telegram_handler(event: Event) -> None:
        raise RuntimeError("telegram api 500")

    bus.subscribe("notif.*", telegram_handler)

    await bus.publish(Event(topic="notif.critical", payload={"msg": "hi"}))
    pending = await _poll_pending(dlq, min_count=1)
    assert len(pending) == 1
    row = pending[0]
    assert row.topic == "notif.critical"
    assert row.payload == {"msg": "hi"}
    assert row.handler_name.endswith("telegram_handler")
    assert row.error_type == "RuntimeError"
    assert "telegram api 500" in row.error_message
    assert row.attempts == 0
    assert row.status == "pending"

    await bus.stop()


@pytest.mark.asyncio
async def test_successful_handler_not_enqueued(tmp_path: Path) -> None:
    """Handler exitoso no genera fila en la DLQ."""
    bus = CoreEventBus()
    dlq = DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver=bus._handler_index.get,
    )
    bus._dlq = dlq
    await bus.start()

    async def ok_handler(event: Event) -> None:
        pass

    bus.subscribe("ok.*", ok_handler)
    await bus.publish(Event(topic="ok.done", payload={}))
    await asyncio.sleep(0.1)

    pending = await dlq.list_pending()
    assert len(pending) == 0

    await bus.stop()


@pytest.mark.asyncio
async def test_only_failed_handler_enqueued_not_successful_one(tmp_path: Path) -> None:
    """Con 2 suscriptores: sólo el que falla va a la DLQ — el exitoso no se duplica."""
    bus = CoreEventBus()
    dlq = DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver=bus._handler_index.get,
    )
    bus._dlq = dlq
    await dlq._ensure_schema()  # pre-create schema to avoid SQLite locking race
    await bus.start()

    good_calls: list[Event] = []

    async def good_handler(event: Event) -> None:
        good_calls.append(event)

    async def bad_handler(event: Event) -> None:
        raise ValueError("bad")

    bus.subscribe("multi.*", good_handler)
    bus.subscribe("multi.*", bad_handler)

    await bus.publish(Event(topic="multi.test", payload={"x": 1}))
    pending = await _poll_pending(dlq, min_count=1)

    # Good handler fue llamado exactamente una vez
    assert len(good_calls) == 1

    # DLQ tiene sólo la fila del bad_handler
    assert len(pending) == 1
    assert pending[0].handler_name.endswith("bad_handler")

    await bus.stop()


@pytest.mark.asyncio
async def test_dlq_retry_delivers_to_handler(tmp_path: Path) -> None:
    """Worker reintenta evento fallido y lo entrega exitosamente."""
    fail_once = [True]
    retried: list[Event] = []

    async def flaky_handler(event: Event) -> None:
        retried.append(event)
        if fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("first fail")

    handler_name = CoreEventBus._handler_name(flaky_handler)

    tick = [0]

    def fast_clock() -> int:
        t = tick[0]
        tick[0] += 50_000
        return t

    bus = CoreEventBus()
    dlq = DLQManager(
        db_path=tmp_path / "dlq.db",
        handler_resolver={handler_name: flaky_handler}.get,
        poll_interval_s=0,
        clock=fast_clock,
    )
    bus._dlq = dlq
    await bus.start()

    bus.subscribe("retry.*", flaky_handler)
    await bus.publish(Event(topic="retry.test", payload={"attempt": True}))
    await poll_until(
        lambda: len(retried) >= 2,
        timeout=5.0,
        msg="Handler no fue llamado 2 veces (initial dispatch + DLQ retry)",
    )
    await bus.stop()

    # Handler llamado 2 veces: dispatch inicial (falla) + retry (éxito)
    assert len(retried) == 2
    assert retried[0].topic == "retry.test"
    assert retried[1].topic == "retry.test"

    # DLQ está vacía — fila eliminada tras éxito
    pending = await dlq.list_pending()
    dead = await dlq.list_dead()
    assert len(pending) == 0
    assert len(dead) == 0


@pytest.mark.asyncio
async def test_create_bus_with_dlq_factory(tmp_path: Path) -> None:
    """create_bus_with_dlq() produce un bus funcional con DLQ integrada."""
    bus = create_bus_with_dlq(db_path=tmp_path / "factory.db")
    assert bus._dlq is not None

    await bus.start()

    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("fac.*", handler)
    await bus.publish(Event(topic="fac.test", payload={"ok": True}))
    await asyncio.sleep(0.1)

    assert len(received) == 1

    await bus.stop()


@pytest.mark.asyncio
async def test_bus_without_dlq_still_works(tmp_path: Path) -> None:
    """CoreEventBus sin DLQ (dlq=None) conserva comportamiento fire-and-forget original."""
    bus = CoreEventBus()  # sin DLQ
    assert bus._dlq is None

    received: list[Event] = []

    async def ok_handler(event: Event) -> None:
        received.append(event)

    async def bad_handler(event: Event) -> None:
        raise RuntimeError("silent failure")

    bus.subscribe("t", ok_handler)
    bus.subscribe("t", bad_handler)
    await bus.start()
    await bus.publish(Event(topic="t", payload={}))
    await asyncio.sleep(0.1)

    # ok_handler recibió el evento, bad_handler falló silenciosamente (no crasha el bus)
    assert len(received) == 1

    await bus.stop()
