"""Regresiones del ciclo de vida y apagado de ``CoreEventBus``."""

from __future__ import annotations

import asyncio
import contextlib
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


class _DLQStartParcial:
    """DLQ falsa que puede fallar después de adquirir su recurso."""

    def __init__(self) -> None:
        self.recurso_adquirido = False
        self.fallar_siguiente_start = True
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.recurso_adquirido = True
        if self.fallar_siguiente_start:
            self.fallar_siguiente_start = False
            raise RuntimeError("fallo tras adquirir recurso DLQ")

    async def stop(self) -> None:
        self.stop_calls += 1
        self.recurso_adquirido = False

    async def enqueue(
        self,
        event: Event,
        callback: Callable[[Event], Awaitable[None]],
        exc: BaseException,
    ) -> None:
        del event, callback, exc


async def _cancelar_tareas(*tasks: asyncio.Task[object] | None) -> None:
    """Cancela y observa tareas auxiliares que hayan sobrevivido a un fallo."""
    activas = [task for task in tasks if task is not None and not task.done()]
    for task in activas:
        task.cancel()
    if activas:
        await asyncio.gather(*activas, return_exceptions=True)


def _drenar_cola_de_prueba(bus: CoreEventBus) -> None:
    """Retira residuos para que un RED no contamine el event loop de pytest."""
    while not bus._queue.empty():
        bus._queue.get_nowait()
        bus._queue.task_done()


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
async def test_stop_espera_publisher_pausado_despues_de_admision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop no adelanta el sentinel a un publisher admitido antes de su put."""
    bus = CoreEventBus()
    put_iniciado = asyncio.Event()
    liberar_put = asyncio.Event()
    publisher: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    put_original = bus._queue.put

    async def put_controlado(item: Event | None) -> None:
        if isinstance(item, Event) and item.payload.get("pausado"):
            put_iniciado.set()
            await liberar_put.wait()
        await put_original(item)

    monkeypatch.setattr(bus._queue, "put", put_controlado)
    await bus.start()
    try:
        publisher = asyncio.create_task(
            bus.publish(Event(topic="test", payload={"pausado": True})),
        )
        await asyncio.wait_for(put_iniciado.wait(), timeout=1)

        stop_task = asyncio.create_task(bus.stop())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not stop_task.done()

        liberar_put.set()
        await asyncio.wait_for(
            asyncio.gather(publisher, stop_task),
            timeout=2,
        )

        assert bus._queue.empty()
        assert bus._queue._unfinished_tasks == 0
        assert bus._dispatch_task is None
    finally:
        liberar_put.set()
        await _cancelar_tareas(publisher, stop_task)
        if bus._dispatch_task is not None:
            await bus.stop()


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
async def test_stress_cinco_reinicios_entregan_cincuenta_eventos() -> None:
    """Cinco ciclos reutilizan el bus sin residuos ni eventos omitidos."""
    bus = CoreEventBus()
    recibidos: list[int] = []
    ciclo_completo = asyncio.Event()
    esperados = 0

    async def handler(event: Event) -> None:
        recibidos.append(event.payload["seq"])
        if len(recibidos) == esperados:
            ciclo_completo.set()

    bus.subscribe("stress", handler)
    try:
        for ciclo in range(5):
            esperados = (ciclo + 1) * 10
            ciclo_completo = asyncio.Event()
            await bus.start()
            for secuencia in range(ciclo * 10, esperados):
                await bus.publish(Event(topic="stress", payload={"seq": secuencia}))

            await asyncio.wait_for(bus._queue.join(), timeout=2)
            await asyncio.wait_for(ciclo_completo.wait(), timeout=2)
            await bus.stop()

        assert recibidos == list(range(50))
        assert bus._queue.empty()
        assert bus._queue._unfinished_tasks == 0
        assert bus._dispatch_task is None
        assert not bus._pending_tasks
        assert not bus._dlq_tasks
        assert not bus._publisher_tasks
        assert bus._admitted_publishers == 0
        assert bus._state.name == "STOPPED"
        assert not bus._running
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
    assert dlq.stop.await_count == 2


@pytest.mark.asyncio
async def test_start_parcial_fallido_libera_dlq_y_permite_reintento() -> None:
    """Todo intento de DLQ.start se compensa si falla tras adquirir recursos."""
    dlq = _DLQStartParcial()
    bus = CoreEventBus(dlq=dlq)

    with pytest.raises(RuntimeError, match="fallo tras adquirir recurso DLQ"):
        await bus.start()

    assert dlq.start_calls == 1
    assert dlq.stop_calls == 1
    assert not dlq.recurso_adquirido
    assert bus._dispatch_task is None
    assert not bus._running
    with pytest.raises(RuntimeError, match="bus is not running"):
        await bus.publish(Event(topic="test", payload={}))

    try:
        await bus.start()
        assert dlq.recurso_adquirido
        await bus.stop()
    finally:
        if bus._dispatch_task is not None:
            await bus.stop()

    assert dlq.start_calls == 2
    assert dlq.stop_calls == 2
    assert not dlq.recurso_adquirido


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


@pytest.mark.asyncio
async def test_stop_completa_cleanup_si_dispatcher_ya_fallo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El error del dispatcher se propaga solo después de todo el cleanup."""
    dlq = _crear_dlq_mock()
    bus = CoreEventBus(dlq=dlq)
    handler_iniciado = asyncio.Event()
    handler_finalizado = asyncio.Event()
    liberar_handler = asyncio.Event()
    dispatcher_en_segundo_get = asyncio.Event()
    liberar_fallo = asyncio.Event()
    llamadas_get = 0
    get_original = bus._queue.get

    async def get_controlado() -> Event | None:
        nonlocal llamadas_get
        llamadas_get += 1
        if llamadas_get == 2:
            dispatcher_en_segundo_get.set()
            await liberar_fallo.wait()
            raise RuntimeError("dispatcher roto")
        return await get_original()

    async def handler_bloqueado(event: Event) -> None:  # noqa: ARG001
        handler_iniciado.set()
        try:
            await liberar_handler.wait()
        finally:
            handler_finalizado.set()

    monkeypatch.setattr(bus._queue, "get", get_controlado)
    bus.subscribe("test", handler_bloqueado)
    await bus.start()
    try:
        await bus.publish(Event(topic="test", payload={}))
        await asyncio.wait_for(handler_iniciado.wait(), timeout=1)
        await asyncio.wait_for(dispatcher_en_segundo_get.wait(), timeout=1)
        liberar_fallo.set()
        assert bus._dispatch_task is not None
        await asyncio.gather(bus._dispatch_task, return_exceptions=True)

        with pytest.raises(RuntimeError, match="dispatcher roto"):
            await bus.stop()

        assert handler_finalizado.is_set()
        assert dlq.stop.await_count == 1
        assert bus._queue.empty()
        assert bus._queue._unfinished_tasks == 0
        assert bus._dispatch_task is None
        assert not bus._pending_tasks
        assert not bus._dlq_tasks
        assert not bus._publisher_tasks
    finally:
        liberar_fallo.set()
        liberar_handler.set()
        for task in list(bus._pending_tasks):
            task.cancel()
        if bus._pending_tasks:
            await asyncio.gather(*bus._pending_tasks, return_exceptions=True)
        _drenar_cola_de_prueba(bus)


@pytest.mark.asyncio
async def test_stop_aborta_publisher_bloqueado_si_dispatcher_muere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La muerte anormal del dispatcher no deja publishers admitidos colgados."""
    bus = CoreEventBus(max_queue_size=1)
    dispatcher_en_get = asyncio.Event()
    liberar_fallo = asyncio.Event()
    segundo_put_iniciado = asyncio.Event()
    put_original = bus._queue.put
    publisher: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None

    async def get_fallido() -> Event | None:
        dispatcher_en_get.set()
        await liberar_fallo.wait()
        raise RuntimeError("dispatcher sin drenaje")

    async def put_controlado(item: Event | None) -> None:
        if isinstance(item, Event) and item.payload.get("seq") == 2:
            segundo_put_iniciado.set()
        await put_original(item)

    monkeypatch.setattr(bus._queue, "get", get_fallido)
    monkeypatch.setattr(bus._queue, "put", put_controlado)
    await bus.start()
    try:
        await asyncio.wait_for(dispatcher_en_get.wait(), timeout=1)
        await bus.publish(Event(topic="test", payload={"seq": 1}))
        publisher = asyncio.create_task(
            bus.publish(Event(topic="test", payload={"seq": 2})),
        )
        await asyncio.wait_for(segundo_put_iniciado.wait(), timeout=1)
        assert not publisher.done()

        liberar_fallo.set()
        assert bus._dispatch_task is not None
        await asyncio.gather(bus._dispatch_task, return_exceptions=True)
        stop_task = asyncio.create_task(bus.stop())

        done, _ = await asyncio.wait({publisher}, timeout=0.2)
        assert publisher in done
        with pytest.raises(RuntimeError, match="dispatcher"):
            await publisher
        with pytest.raises(RuntimeError, match="dispatcher sin drenaje"):
            await stop_task

        assert bus._queue.empty()
        assert bus._queue._unfinished_tasks == 0
        assert bus._dispatch_task is None
        assert not bus._pending_tasks
        assert not bus._dlq_tasks
        assert not bus._publisher_tasks
        assert bus.events_lost == 1
    finally:
        liberar_fallo.set()
        await _cancelar_tareas(publisher)
        _drenar_cola_de_prueba(bus)
        await _cancelar_tareas(stop_task)
        _drenar_cola_de_prueba(bus)


@pytest.mark.asyncio
async def test_publish_falla_rapido_si_dispatcher_ya_murio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un dispatcher terminado impide admitir otro put aunque la cola esté llena."""
    bus = CoreEventBus(max_queue_size=1)
    dispatcher_en_get = asyncio.Event()
    liberar_fallo = asyncio.Event()

    async def get_fallido() -> Event | None:
        dispatcher_en_get.set()
        await liberar_fallo.wait()
        raise RuntimeError("dispatcher muerto al publicar")

    monkeypatch.setattr(bus._queue, "get", get_fallido)
    await bus.start()
    try:
        await asyncio.wait_for(dispatcher_en_get.wait(), timeout=1)
        await bus.publish(Event(topic="test", payload={"seq": 1}))
        liberar_fallo.set()
        assert bus._dispatch_task is not None
        await asyncio.gather(bus._dispatch_task, return_exceptions=True)

        with pytest.raises(RuntimeError, match="dispatcher") as exc_info:
            await asyncio.wait_for(
                bus.publish(Event(topic="test", payload={"seq": 2})),
                timeout=0.2,
            )
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "dispatcher muerto al publicar" in str(exc_info.value.__cause__)

        assert bus._admitted_publishers == 0
        assert not bus._publisher_tasks
        assert bus._publishers_drained.is_set()
        assert bus._queue.qsize() == 1
    finally:
        liberar_fallo.set()
        with contextlib.suppress(RuntimeError, asyncio.CancelledError):
            await bus.stop()
        _drenar_cola_de_prueba(bus)


@pytest.mark.asyncio
async def test_enqueue_autocancelado_no_cancela_fallo_normal_del_handler() -> None:
    """CancelledError interno de DLQ es pérdida durable, no cancelación del handler."""
    dlq = _crear_dlq_mock()
    dlq.enqueue.side_effect = asyncio.CancelledError("cancelación interna DLQ")
    bus = CoreEventBus(dlq=dlq)

    async def handler_fallido(event: Event) -> None:  # noqa: ARG001
        raise RuntimeError("fallo normal")

    await bus._safe_execute(handler_fallido, Event(topic="test", payload={}))
    await asyncio.sleep(0)

    assert bus.events_lost == 1
    assert dlq.enqueue.await_count == 1
    assert not bus._dlq_tasks


@pytest.mark.asyncio
async def test_enqueue_autocancelado_preserva_cancelacion_original_del_handler() -> None:
    """La cancelación interna de DLQ no sustituye la cancelación del handler."""
    dlq = _crear_dlq_mock()
    dlq.enqueue.side_effect = asyncio.CancelledError("cancelación interna DLQ")
    bus = CoreEventBus(dlq=dlq)
    cancelacion_original = asyncio.CancelledError("cancelación original handler")

    async def handler_cancelado(event: Event) -> None:  # noqa: ARG001
        raise cancelacion_original

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await bus._safe_execute(handler_cancelado, Event(topic="test", payload={}))
    await asyncio.sleep(0)

    assert exc_info.value is cancelacion_original
    assert bus.events_lost == 1
    assert dlq.enqueue.await_count == 1
    assert not bus._dlq_tasks


@pytest.mark.asyncio
async def test_publish_prioriza_dispatcher_si_put_y_fallo_completan_juntos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La terminación simultánea nunca confirma un evento sin consumidor."""
    for intento in range(20):
        bus = CoreEventBus()
        liberar_ambos = asyncio.Event()
        dispatcher_listo = asyncio.Event()
        put_listo = asyncio.Event()
        put_original = bus._queue.put
        publisher: asyncio.Task[None] | None = None

        async def get_fallido(
            dispatcher_actual: asyncio.Event = dispatcher_listo,
            liberar_actual: asyncio.Event = liberar_ambos,
            intento_actual: int = intento,
        ) -> Event | None:
            dispatcher_actual.set()
            await liberar_actual.wait()
            raise RuntimeError(f"dispatcher simultáneo {intento_actual}")

        async def put_sincronizado(
            item: Event | None,
            put_listo_actual: asyncio.Event = put_listo,
            liberar_actual: asyncio.Event = liberar_ambos,
            put_actual: Callable[[Event | None], Awaitable[None]] = put_original,
        ) -> None:
            if item is not None:
                put_listo_actual.set()
                await liberar_actual.wait()
            await put_actual(item)

        monkeypatch.setattr(bus._queue, "get", get_fallido)
        monkeypatch.setattr(bus._queue, "put", put_sincronizado)
        await bus.start()
        try:
            await asyncio.wait_for(dispatcher_listo.wait(), timeout=1)
            publisher = asyncio.create_task(
                bus.publish(Event(topic="test", payload={"intento": intento})),
            )
            await asyncio.wait_for(put_listo.wait(), timeout=1)
            liberar_ambos.set()

            with pytest.raises(RuntimeError, match="dispatcher") as exc_info:
                await asyncio.wait_for(publisher, timeout=1)
            assert isinstance(exc_info.value.__cause__, RuntimeError)

            assert bus._queue.empty()
            assert bus._queue._unfinished_tasks == 0
            assert bus.events_lost == 1
            assert bus._admitted_publishers == 0
            assert not bus._publisher_tasks
            assert bus._publishers_drained.is_set()

            with pytest.raises(RuntimeError, match="dispatcher simultáneo"):
                await bus.stop()
            assert bus.events_lost == 1
            assert bus._queue.empty()
            assert bus._queue._unfinished_tasks == 0
        finally:
            liberar_ambos.set()
            await _cancelar_tareas(publisher)
            with contextlib.suppress(RuntimeError, asyncio.CancelledError):
                await bus.stop()
            _drenar_cola_de_prueba(bus)
