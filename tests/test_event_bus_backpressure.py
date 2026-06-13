"""
H-04: Test de backpressure para CoreEventBus.

Verifica que los eventos descartados por backpressure (cuando _pending_tasks
alcanza _MAX_PENDING_TASKS) sean encolados en la DLQ en lugar de perderse
silenciosamente.

Este test FALLA con la implementación actual (el `continue` descarta el evento
sin notificar a la DLQ) y PASA tras el fix H-04.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.event_bus import BackpressureDroppedError, CoreEventBus, Event
from tests.polling_utils import poll_until


def _make_dlq_mock() -> MagicMock:
    """Factory: DLQManager mock con métodos async correctamente configurados."""
    dlq = MagicMock()
    dlq.start = AsyncMock()
    dlq.stop = AsyncMock()
    dlq.enqueue = AsyncMock()
    return dlq


# ---------------------------------------------------------------------------
# H-04 Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_routes_dropped_events_to_dlq() -> None:
    """H-04 RED: el evento descartado por backpressure se pierde silenciosamente (broken).
    H-04 GREEN: el evento va a DLQ con excepción BackpressureDropped (fixed).
    """
    dlq = _make_dlq_mock()
    bus = CoreEventBus(dlq=dlq)

    # Handler lento que mantiene _pending_tasks lleno
    hold = asyncio.Event()

    async def slow_handler(event: Event) -> None:  # noqa: ARG001
        await hold.wait()

    bus.subscribe("probe.*", slow_handler)

    # Bajar el cap a 1 para que sea fácil saturar con un test pequeño
    original_cap = CoreEventBus._MAX_PENDING_TASKS
    CoreEventBus._MAX_PENDING_TASKS = 1

    await bus.start()
    try:
        # Evento 1 → ocupa el único slot disponible en _pending_tasks
        event_1 = Event(topic="probe.fill", payload={"seq": 1})
        await bus.publish(event_1)
        await asyncio.sleep(0)  # Ceder el loop para que _dispatch_loop procese
        await asyncio.sleep(0)  # Asegurar que el task se añade a _pending_tasks

        # Evento 2 → _pending_tasks está lleno → debería ir a DLQ
        event_2 = Event(topic="probe.drop", payload={"seq": 2})
        await bus.publish(event_2)

        # Esperar a que _dispatch_loop procese event_2 y (tras el fix) llame a dlq.enqueue
        # Con código roto: dlq.enqueue NUNCA se llama → poll_until agota el timeout → falla
        await poll_until(
            lambda: dlq.enqueue.called,
            timeout=3.0,
            msg="DLQ.enqueue debe haber sido llamado para el evento descartado por backpressure",
        )
    finally:
        CoreEventBus._MAX_PENDING_TASKS = original_cap
        hold.set()  # Liberar el slow_handler para que bus.stop() finalice limpiamente
        await bus.stop()

    # Verificar que enqueue fue llamado con los argumentos correctos
    assert dlq.enqueue.call_count >= 1, "DLQ.enqueue debe haberse llamado al menos una vez"

    call_event, call_callback, call_exc = dlq.enqueue.call_args[0]
    assert call_event.topic == event_2.topic, (
        f"El evento encolado debe ser event_2 ('{event_2.topic}'), no '{call_event.topic}'"
    )
    assert isinstance(call_exc, BackpressureDroppedError), (
        f"La excepción debe ser BackpressureDroppedError, obtenido {type(call_exc).__name__}"
    )


@pytest.mark.asyncio
async def test_backpressure_dlq_enqueues_are_bounded() -> None:
    """La ruta DLQ por backpressure no debe crear tasks sin límite."""
    dlq = _make_dlq_mock()
    bus = CoreEventBus(dlq=dlq)
    hold_handler = asyncio.Event()
    enqueue_started = asyncio.Event()
    release_enqueue = asyncio.Event()

    async def slow_handler(event: Event) -> None:  # noqa: ARG001
        await hold_handler.wait()

    async def slow_enqueue(*args: object) -> None:
        enqueue_started.set()
        await release_enqueue.wait()

    dlq.enqueue.side_effect = slow_enqueue
    bus.subscribe("probe.*", slow_handler)

    original_pending_cap = CoreEventBus._MAX_PENDING_TASKS
    original_dlq_cap = CoreEventBus._MAX_DLQ_TASKS
    CoreEventBus._MAX_PENDING_TASKS = 1
    CoreEventBus._MAX_DLQ_TASKS = 1

    await bus.start()
    try:
        await bus.publish(Event(topic="probe.fill", payload={"seq": 1}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await bus.publish(Event(topic="probe.drop.1", payload={"seq": 2}))
        await poll_until(
            lambda: enqueue_started.is_set(),
            timeout=3.0,
            msg="DLQ.enqueue debe quedar en curso para saturar la ruta DLQ",
        )

        await bus.publish(Event(topic="probe.drop.2", payload={"seq": 3}))
        await bus._queue.join()

        assert dlq.enqueue.call_count == 1
        assert len(bus._dlq_tasks) <= CoreEventBus._MAX_DLQ_TASKS
    finally:
        CoreEventBus._MAX_PENDING_TASKS = original_pending_cap
        CoreEventBus._MAX_DLQ_TASKS = original_dlq_cap
        release_enqueue.set()
        hold_handler.set()
        await bus.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_backpressure_dlq_enqueue() -> None:
    """stop() debe drenar los enqueues DLQ aceptados antes de detener la DLQ."""
    dlq = _make_dlq_mock()
    bus = CoreEventBus(dlq=dlq)
    hold_handler = asyncio.Event()
    enqueue_started = asyncio.Event()
    release_enqueue = asyncio.Event()

    async def slow_handler(event: Event) -> None:  # noqa: ARG001
        await hold_handler.wait()

    async def slow_enqueue(*args: object) -> None:
        enqueue_started.set()
        await release_enqueue.wait()

    dlq.enqueue.side_effect = slow_enqueue
    bus.subscribe("probe.*", slow_handler)

    original_cap = CoreEventBus._MAX_PENDING_TASKS
    CoreEventBus._MAX_PENDING_TASKS = 1

    await bus.start()
    try:
        await bus.publish(Event(topic="probe.fill", payload={"seq": 1}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await bus.publish(Event(topic="probe.drop", payload={"seq": 2}))
        await poll_until(
            lambda: enqueue_started.is_set(),
            timeout=3.0,
            msg="DLQ.enqueue debe estar en curso antes de detener el bus",
        )

        stop_task = asyncio.create_task(bus.stop())
        await asyncio.sleep(0)
        assert not stop_task.done()

        release_enqueue.set()
        hold_handler.set()
        await stop_task
    finally:
        CoreEventBus._MAX_PENDING_TASKS = original_cap
        if bus._dispatch_task is not None:
            release_enqueue.set()
            hold_handler.set()
            await bus.stop()


# ---------------------------------------------------------------------------
# Hardening jun-2026: cap de DLQ elevado + contadores de drops observables
# ---------------------------------------------------------------------------


def test_max_dlq_tasks_cap_is_200() -> None:
    """El cap de enqueues DLQ pendientes sube de 50 a 200 (auditoría jun-2026)."""
    assert CoreEventBus._MAX_DLQ_TASKS == 200


@pytest.mark.asyncio
async def test_backpressure_drop_increments_counter() -> None:
    """Cada dispatch descartado por backpressure incrementa bus.backpressure_drops."""
    dlq = _make_dlq_mock()
    bus = CoreEventBus(dlq=dlq)
    hold = asyncio.Event()

    async def slow_handler(event: Event) -> None:  # noqa: ARG001
        await hold.wait()

    bus.subscribe("probe.*", slow_handler)

    original_cap = CoreEventBus._MAX_PENDING_TASKS
    CoreEventBus._MAX_PENDING_TASKS = 1

    await bus.start()
    try:
        await bus.publish(Event(topic="probe.fill", payload={"seq": 1}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert bus.backpressure_drops == 0

        await bus.publish(Event(topic="probe.drop", payload={"seq": 2}))
        await poll_until(
            lambda: bus.backpressure_drops >= 1,
            timeout=3.0,
            msg="backpressure_drops debe incrementarse al descartar un dispatch",
        )
    finally:
        CoreEventBus._MAX_PENDING_TASKS = original_cap
        hold.set()
        await bus.stop()


@pytest.mark.asyncio
async def test_dlq_overflow_increments_events_lost() -> None:
    """Un evento perdido por saturación de la ruta DLQ incrementa bus.events_lost."""
    dlq = _make_dlq_mock()
    bus = CoreEventBus(dlq=dlq)
    hold_handler = asyncio.Event()
    enqueue_started = asyncio.Event()
    release_enqueue = asyncio.Event()

    async def slow_handler(event: Event) -> None:  # noqa: ARG001
        await hold_handler.wait()

    async def slow_enqueue(*args: object) -> None:
        enqueue_started.set()
        await release_enqueue.wait()

    dlq.enqueue.side_effect = slow_enqueue
    bus.subscribe("probe.*", slow_handler)

    original_pending_cap = CoreEventBus._MAX_PENDING_TASKS
    original_dlq_cap = CoreEventBus._MAX_DLQ_TASKS
    CoreEventBus._MAX_PENDING_TASKS = 1
    CoreEventBus._MAX_DLQ_TASKS = 1

    await bus.start()
    try:
        await bus.publish(Event(topic="probe.fill", payload={"seq": 1}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await bus.publish(Event(topic="probe.drop.1", payload={"seq": 2}))
        await poll_until(
            lambda: enqueue_started.is_set(),
            timeout=3.0,
            msg="DLQ.enqueue debe quedar en curso para saturar la ruta DLQ",
        )

        assert bus.events_lost == 0

        await bus.publish(Event(topic="probe.drop.2", payload={"seq": 3}))
        await bus._queue.join()

        assert bus.events_lost >= 1
    finally:
        CoreEventBus._MAX_PENDING_TASKS = original_pending_cap
        CoreEventBus._MAX_DLQ_TASKS = original_dlq_cap
        release_enqueue.set()
        hold_handler.set()
        await bus.stop()
