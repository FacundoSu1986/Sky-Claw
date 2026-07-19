"""Regresiones del ciclo de vida y apagado de ``CoreEventBus``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event


def _crear_dlq_mock() -> MagicMock:
    """Crea una DLQ con todos sus bordes asíncronos controlables."""
    dlq = MagicMock()
    dlq.start = AsyncMock()
    dlq.stop = AsyncMock()
    dlq.enqueue = AsyncMock()
    return dlq


async def _cancelar_tareas(*tasks: asyncio.Task[object] | None) -> None:
    """Cancela y observa tareas auxiliares que hayan sobrevivido a un fallo."""
    activas = [task for task in tasks if task is not None and not task.done()]
    for task in activas:
        task.cancel()
    if activas:
        await asyncio.gather(*activas, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_drena_publisher_admitido_con_cola_llena(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un publisher admitido antes de stop conserva FIFO y no queda bloqueado."""
    bus = CoreEventBus(max_queue_size=1)
    extraido = asyncio.Event()
    liberar_dispatcher = asyncio.Event()
    publisher: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    get_original = bus._queue.get

    async def get_controlado() -> Event | None:
        item = await get_original()
        if isinstance(item, Event) and item.payload["seq"] == 1:
            extraido.set()
            await liberar_dispatcher.wait()
        return item

    monkeypatch.setattr(bus._queue, "get", get_controlado)
    await bus.start()
    try:
        await bus.publish(Event(topic="test", payload={"seq": 1}))
        await asyncio.wait_for(extraido.wait(), timeout=1)
        await bus.publish(Event(topic="test", payload={"seq": 2}))

        publisher = asyncio.create_task(
            bus.publish(Event(topic="test", payload={"seq": 3})),
        )
        await asyncio.sleep(0)
        assert not publisher.done()

        stop_task = asyncio.create_task(bus.stop())
        await asyncio.sleep(0)
        liberar_dispatcher.set()
        await asyncio.wait_for(
            asyncio.gather(publisher, stop_task),
            timeout=2,
        )

        assert bus._queue.empty()
        assert bus._queue._unfinished_tasks == 0
        assert bus._dispatch_task is None
    finally:
        liberar_dispatcher.set()
        await _cancelar_tareas(publisher, stop_task)
        if bus._dispatch_task is not None:
            bus._dispatch_task.cancel()
            await asyncio.gather(bus._dispatch_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_procesa_eventos_sin_residuos() -> None:
    """Dos ciclos completos entregan ambos eventos y dejan la cola limpia."""
    bus = CoreEventBus()
    recibidos: list[int] = []

    async def handler(event: Event) -> None:
        recibidos.append(event.payload["seq"])

    bus.subscribe("test", handler)
    try:
        await bus.start()
        await bus.publish(Event(topic="test", payload={"seq": 1}))
        await bus._queue.join()
        await asyncio.sleep(0)
        await bus.stop()

        await bus.start()
        await bus.publish(Event(topic="test", payload={"seq": 2}))
        await bus._queue.join()
        await asyncio.sleep(0)
        await bus.stop()

        assert recibidos == [1, 2]
        assert bus._queue.empty()
        assert bus._queue._unfinished_tasks == 0
        assert bus._dispatch_task is None
    finally:
        if bus._dispatch_task is not None:
            await bus.stop()


@pytest.mark.asyncio
async def test_start_fallido_restaura_estado_y_permite_reintento() -> None:
    """Un fallo de DLQ.start no publica un bus parcial ni impide reintentar."""
    dlq = _crear_dlq_mock()
    dlq.start.side_effect = [RuntimeError("fallo al iniciar DLQ"), None]
    bus = CoreEventBus(dlq=dlq)

    with pytest.raises(RuntimeError, match="fallo al iniciar DLQ"):
        await bus.start()

    assert bus._dispatch_task is None
    assert not bus._running
    with pytest.raises(RuntimeError, match="bus is not running"):
        await bus.publish(Event(topic="test", payload={}))

    try:
        await bus.start()
        await bus.stop()
    finally:
        if bus._dispatch_task is not None:
            await bus.stop()

    assert dlq.start.await_count == 2
    assert dlq.stop.await_count == 1


@pytest.mark.asyncio
async def test_start_y_stop_concurrentes_terminan_detenidos() -> None:
    """stop iniciado durante STARTING se serializa y gana al terminar start."""
    dlq = _crear_dlq_mock()
    inicio_dlq = asyncio.Event()
    liberar_inicio = asyncio.Event()
    start_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None

    async def start_bloqueado() -> None:
        inicio_dlq.set()
        await liberar_inicio.wait()

    dlq.start.side_effect = start_bloqueado
    bus = CoreEventBus(dlq=dlq)
    try:
        start_task = asyncio.create_task(bus.start())
        await asyncio.wait_for(inicio_dlq.wait(), timeout=1)
        stop_task = asyncio.create_task(bus.stop())
        await asyncio.sleep(0)
        liberar_inicio.set()
        await asyncio.wait_for(
            asyncio.gather(start_task, stop_task),
            timeout=2,
        )

        assert bus._dispatch_task is None
        assert not bus._running
        with pytest.raises(RuntimeError, match="bus is not running"):
            await bus.publish(Event(topic="test", payload={}))
        assert dlq.stop.await_count == 1
    finally:
        liberar_inicio.set()
        await _cancelar_tareas(start_task, stop_task)
        if bus._dispatch_task is not None:
            await bus.stop()


@pytest.mark.asyncio
async def test_stop_persiste_cancelacion_de_handler_antes_de_detener_dlq() -> None:
    """La cancelación de un handler queda durable antes de DLQ.stop."""
    dlq = _crear_dlq_mock()
    handler_iniciado = asyncio.Event()
    enqueue_iniciado = asyncio.Event()
    liberar_enqueue = asyncio.Event()
    orden: list[str] = []
    excepciones: list[BaseException] = []
    stop_task: asyncio.Task[None] | None = None

    async def handler_bloqueado(event: Event) -> None:  # noqa: ARG001
        handler_iniciado.set()
        await asyncio.Event().wait()

    async def enqueue_bloqueado(
        event: Event,
        callback: Callable[[Event], Awaitable[None]],
        exc: BaseException,
    ) -> None:
        del event, callback
        excepciones.append(exc)
        orden.append("enqueue-inicio")
        enqueue_iniciado.set()
        await liberar_enqueue.wait()
        orden.append("enqueue-fin")

    async def detener_dlq() -> None:
        orden.append("dlq-stop")

    dlq.enqueue.side_effect = enqueue_bloqueado
    dlq.stop.side_effect = detener_dlq
    bus = CoreEventBus(dlq=dlq)
    bus.subscribe("test", handler_bloqueado)
    await bus.start()
    try:
        await bus.publish(Event(topic="test", payload={}))
        await asyncio.wait_for(handler_iniciado.wait(), timeout=1)

        stop_task = asyncio.create_task(bus.stop())
        await asyncio.wait_for(enqueue_iniciado.wait(), timeout=1)
        assert not stop_task.done()
        assert orden == ["enqueue-inicio"]

        liberar_enqueue.set()
        await asyncio.wait_for(stop_task, timeout=2)

        assert orden == ["enqueue-inicio", "enqueue-fin", "dlq-stop"]
        assert len(excepciones) == 1
        assert isinstance(excepciones[0], asyncio.CancelledError)
    finally:
        liberar_enqueue.set()
        await _cancelar_tareas(stop_task)
        if bus._dispatch_task is not None:
            await bus.stop()
