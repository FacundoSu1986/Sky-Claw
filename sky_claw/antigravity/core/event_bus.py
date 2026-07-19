"""CoreEventBus — bus de eventos asíncrono agnóstico para la Titan Edition.

Infraestructura pub/sub instanciable (no singleton) diseñada para uso
global entre agentes. El dispatch usa fire-and-forget (``create_task``)
para que un consumidor lento jamás bloquee al bus ni a otros suscriptores.

Los eventos fallidos se persisten en la Dead Letter Queue (DLQManager) para
reintento con backoff exponencial. El bus sin DLQ conserva el comportamiento
original (backward compatible: ``dlq=None`` por defecto).

Parte del Sprint 1: Strangler Fig — desacoplamiento de ``supervisor.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.antigravity.core.dlq_manager import DLQManager

logger = logging.getLogger(__name__)

Subscriber = Callable[["Event"], Awaitable[None]]


class BackpressureDroppedError(RuntimeError):
    """Excepción que representa un evento descartado por backpressure del bus.

    Se usa como causa en la DLQ cuando ``_MAX_PENDING_TASKS`` está lleno y
    el evento no puede despacharse; la DLQ permite su reintento posterior.
    Si no hay DLQ configurada el comportamiento original (drop silencioso) se mantiene.
    """


# Alias para backward-compat con código que importe el nombre anterior.
BackpressureDropped = BackpressureDroppedError


class _LifecycleState(Enum):
    """Estados serializados del ciclo de vida del bus."""

    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()


@dataclass(frozen=True, slots=True)
class Event:
    """Envolvente inmutable de evento para transporte por el bus.

    Args:
        topic: Ruta dot-separated del evento (ej. ``system.telemetry.metrics``).
        payload: Diccionario con los datos del evento.
        timestamp_ms: Epoch en milisegundos (autogenerado).
        source: Identificador del emisor.
    """

    topic: str
    payload: dict
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    source: str = "system"


class CoreEventBus:
    """Bus de eventos asíncrono con pattern-matching y dispatch concurrente.

    Instanciable para permitir testing aislado. Los patrones de suscripción
    usan ``fnmatch`` (``*`` matchea cualquier cadena, incluso con puntos).

    Los eventos fallidos se encolan en la DLQ inyectada (si hay una).
    Usar ``create_bus_with_dlq()`` para obtener una instancia pre-cableada.

    Args:
        max_queue_size: Tamaño máximo de la cola interna (backpressure).
        dlq: Dead Letter Queue opcional. Si es None, comportamiento fire-and-forget original.
        require_dlq: P1.2 — cuando True, ``__init__`` exige que ``dlq`` no
            sea None y aborta con ``ValueError``.  La factory de producción
            ``create_bus_with_dlq`` lo activa por defecto: en producción un
            bus sin DLQ pierde eventos bajo backpressure sin dejar
            traza más allá de un WARNING, lo cual oculta un bug de config.
            Tests y dev shells siguen pasando ``require_dlq=False`` (default).
    """

    # Maximum number of subscriber coroutines that may run concurrently.
    # Exceeding this limit causes the offending dispatch to be dropped with a
    # WARNING instead of spawning an unbounded number of tasks (backpressure).
    _MAX_PENDING_TASKS: int = 50
    # DLQ enqueue tasks are tiny (one INSERT each); a cap 4x the dispatch cap
    # absorbs bursts where every dropped dispatch needs a DLQ reroute without
    # losing events (auditoría jun-2026: con 50/50 una ráfaga perdía eventos
    # con solo un log CRITICAL).
    _MAX_DLQ_TASKS: int = 200

    def __init__(
        self,
        *,
        max_queue_size: int = 1024,
        dlq: DLQManager | None = None,
        require_dlq: bool = False,
    ) -> None:
        if require_dlq and dlq is None:
            raise ValueError(
                "CoreEventBus: require_dlq=True needs a DLQManager instance — "
                "production deployments must wire up DLQ via create_bus_with_dlq() "
                "or pass dlq=... explicitly to avoid silent event loss under "
                "backpressure."
            )
        self._subscriptions: list[tuple[str, Subscriber]] = []
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(
            maxsize=max_queue_size,
        )
        self._dispatch_task: asyncio.Task[None] | None = None
        self._pending_tasks: set[asyncio.Task] = set()
        # DLQ fire-and-forget tasks (backpressure enqueues) are tracked separately
        # so stop() can await them without counting against _MAX_PENDING_TASKS.
        self._dlq_tasks: set[asyncio.Task] = set()
        self._running: bool = False
        self._state = _LifecycleState.STOPPED
        self._state_lock = asyncio.Lock()
        self._dlq = dlq
        self._require_dlq = require_dlq
        self._handler_index: dict[str, Subscriber] = {}
        # Contadores observables de degradación (auditoría jun-2026).
        self._backpressure_drops: int = 0
        self._events_lost: int = 0

    @property
    def backpressure_drops(self) -> int:
        """Dispatches descartados por el cap de tasks pendientes (reruteados a DLQ o perdidos)."""
        return self._backpressure_drops

    @property
    def events_lost(self) -> int:
        """Eventos perdidos definitivamente: sin DLQ, ruta DLQ saturada o enqueue fallido."""
        return self._events_lost

    async def start(self) -> None:
        """Inicia el loop de dispatch y el worker DLQ (si hay DLQ) como tareas de fondo."""
        async with self._state_lock:
            if self._state is _LifecycleState.RUNNING:
                logger.warning("CoreEventBus ya está corriendo, ignorando start() duplicado")
                return

            self._state = _LifecycleState.STARTING
            dlq_iniciada = False
            dispatch_task: asyncio.Task[None] | None = None
            try:
                if self._dlq is not None:
                    await self._dlq.start()
                    dlq_iniciada = True
                dispatch_task = asyncio.create_task(
                    self._dispatch_loop(),
                    name="core-event-bus-dispatch",
                )
                self._dispatch_task = dispatch_task
                self._state = _LifecycleState.RUNNING
                self._running = True
            except BaseException:
                cleanup = asyncio.create_task(
                    self._rollback_failed_start(dispatch_task, dlq_iniciada),
                    name="core-event-bus-start-rollback",
                )
                try:
                    await self._observe_task(cleanup)
                except BaseException:
                    logger.critical(
                        "Falló el rollback de CoreEventBus.start()",
                        exc_info=True,
                    )
                raise

            logger.info("CoreEventBus iniciado (queue_max=%d)", self._queue.maxsize)

    async def stop(self) -> None:
        """Detiene el loop de dispatch y el worker DLQ de forma grácil."""
        async with self._state_lock:
            if self._state is _LifecycleState.STOPPED:
                return

            self._running = False
            self._state = _LifecycleState.STOPPING
            cleanup = asyncio.create_task(
                self._stop_started_bus(),
                name="core-event-bus-stop",
            )
            await self._observe_task(cleanup)

    def subscribe(self, pattern: str, callback: Subscriber) -> None:
        """Registra un suscriptor para un patrón de tópico.

        Args:
            pattern: Patrón fnmatch (ej. ``system.telemetry.*``).
            callback: Coroutine ``async def(event: Event) -> None``.
        """
        self._subscriptions.append((pattern, callback))
        self._handler_index[self._handler_name(callback)] = callback

    def unsubscribe(self, pattern: str, callback: Subscriber) -> None:
        """Elimina un suscriptor previamente registrado."""
        with contextlib.suppress(ValueError):
            self._subscriptions.remove((pattern, callback))
        self._handler_index.pop(self._handler_name(callback), None)

    async def publish(self, event: Event) -> None:
        """Publica un evento en la cola para dispatch asíncrono."""
        if self._state is not _LifecycleState.RUNNING or not self._running:
            raise RuntimeError("bus is not running")
        await self._queue.put(event)

    async def _dispatch_loop(self) -> None:
        """Extrae eventos de la cola y los enruta sin bloquear el hilo principal."""
        while True:
            event = await self._queue.get()
            try:
                if event is None:
                    return

                for pattern, callback in self._subscriptions:
                    if fnmatch.fnmatch(event.topic, pattern):
                        if len(self._pending_tasks) >= self._MAX_PENDING_TASKS:
                            cb_name = getattr(callback, "__name__", repr(callback))
                            self._backpressure_drops += 1
                            logger.warning(
                                "Event bus backpressure: %d pending tasks — dropping dispatch for '%s' handler '%s'",
                                len(self._pending_tasks),
                                event.topic,
                                cb_name,
                            )
                            exc = BackpressureDroppedError(
                                f"Backpressure: {self._MAX_PENDING_TASKS} pending tasks alcanzado — "
                                f"handler '{self._handler_name(callback)}' topic '{event.topic}'"
                            )
                            self._schedule_failure(
                                event,
                                callback,
                                exc,
                                task_name=f"dlq-backpressure-{event.topic}",
                            )
                            continue
                        task = asyncio.create_task(self._safe_execute(callback, event))
                        self._pending_tasks.add(task)
                        task.add_done_callback(self._pending_tasks.discard)
            finally:
                self._queue.task_done()

    def _schedule_failure(
        self,
        event: Event,
        callback: Subscriber,
        exc: BaseException,
        *,
        task_name: str,
    ) -> asyncio.Task[None] | None:
        """Agenda de forma acotada la persistencia DLQ de un fallo."""
        if self._dlq is None:
            self._events_lost += 1
            logger.error(
                "Evento '%s' handler '%s' perdido: no hay DLQ configurada",
                event.topic,
                self._handler_name(callback),
            )
            return None
        if len(self._dlq_tasks) >= self._MAX_DLQ_TASKS:
            self._events_lost += 1
            logger.critical(
                "DLQ backpressure: %d pending enqueue tasks — evento '%s' handler '%s' perdido",
                len(self._dlq_tasks),
                event.topic,
                self._handler_name(callback),
            )
            return None
        task = asyncio.create_task(
            self._enqueue_failure(event, callback, exc),
            name=task_name,
        )
        self._dlq_tasks.add(task)
        task.add_done_callback(self._dlq_tasks.discard)
        return task

    async def _enqueue_failure(
        self,
        event: Event,
        callback: Subscriber,
        exc: BaseException,
    ) -> None:
        """Persiste un fallo sin propagar errores best-effort de la DLQ."""
        dlq = self._dlq
        if dlq is None:
            self._events_lost += 1
            logger.critical(
                "DLQ desconectada antes de persistir evento '%s' handler '%s' — evento perdido",
                event.topic,
                self._handler_name(callback),
            )
            return
        try:
            await dlq.enqueue(event, callback, exc)
        except Exception:
            self._events_lost += 1
            logger.critical(
                "DLQ enqueue falló para evento '%s' handler '%s' — evento perdido",
                event.topic,
                self._handler_name(callback),
                exc_info=True,
            )

    async def _safe_execute(self, callback: Subscriber, event: Event) -> None:
        """Ejecuta un consumidor aislando sus fallos del bus. Fallos van a DLQ si está activa."""
        try:
            await callback(event)
        except asyncio.CancelledError as exc:
            task = self._schedule_failure(
                event,
                callback,
                exc,
                task_name=f"dlq-cancelled-{event.topic}",
            )
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            raise exc
        except Exception as exc:
            cb_name = getattr(callback, "__name__", repr(callback))
            logger.error(
                "Fallo en consumidor %s procesando evento '%s': %s",
                cb_name,
                event.topic,
                exc,
                exc_info=True,
            )
            task = self._schedule_failure(
                event,
                callback,
                exc,
                task_name=f"dlq-failure-{event.topic}",
            )
            if task is not None:
                await asyncio.shield(task)

    async def _rollback_failed_start(
        self,
        dispatch_task: asyncio.Task[None] | None,
        dlq_iniciada: bool,
    ) -> None:
        """Revierte recursos parciales creados por ``start``."""
        try:
            if dispatch_task is not None:
                dispatch_task.cancel()
                await asyncio.gather(dispatch_task, return_exceptions=True)
            if dlq_iniciada and self._dlq is not None:
                await self._dlq.stop()
        finally:
            self._dispatch_task = None
            self._running = False
            self._state = _LifecycleState.STOPPED

    async def _stop_started_bus(self) -> None:
        """Completa el drenaje iniciado por ``stop`` aunque su caller sea cancelado."""
        try:
            dispatch_task = self._dispatch_task
            if dispatch_task is not None:
                await self._queue.put(None)
                await dispatch_task

            for task in list(self._pending_tasks):
                task.cancel()
            if self._pending_tasks:
                await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()

            if self._dlq_tasks:
                await asyncio.gather(*self._dlq_tasks, return_exceptions=True)
            self._dlq_tasks.clear()
            if self._dlq is not None:
                await self._dlq.stop()
        finally:
            self._dispatch_task = None
            self._pending_tasks.clear()
            self._dlq_tasks.clear()
            self._running = False
            self._state = _LifecycleState.STOPPED
        logger.info("CoreEventBus detenido")

    @staticmethod
    async def _observe_task(task: asyncio.Task[None]) -> None:
        """Observa una finalización pese a cancelaciones externas repetidas."""
        cancellation: asyncio.CancelledError | None = None
        current_task = asyncio.current_task()

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                external = not task.cancelled() or (current_task is not None and current_task.cancelling() > 0)
                if external and cancellation is None:
                    cancellation = exc
            except BaseException:
                if task.done():
                    break
                raise

        try:
            task.result()
        except BaseException as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise

        if cancellation is not None:
            raise cancellation

    @staticmethod
    def _handler_name(cb: Subscriber) -> str:
        """Genera un identificador estable para un callable."""
        mod = getattr(cb, "__module__", "unknown")
        qn = getattr(cb, "__qualname__", None)
        return f"{mod}.{qn}" if qn else repr(cb)


def create_bus_with_dlq(db_path: Path | None = None, *, lifecycle=None) -> CoreEventBus:
    """Factory que conecta un CoreEventBus con un DLQManager pre-cableado.

    P1.2 — pasa ``require_dlq=True`` al constructor para que cualquier llamada
    accidental con ``dlq=None`` (por ejemplo, un override en tests que
    invalida la DLQ) explote en construcción en lugar de degradarse al modo
    "drop silencioso" en producción.

    Args:
        db_path: Ruta al archivo SQLite. Default: ``~/.sky_claw/dlq/dlq.db``.
        lifecycle: ``DatabaseLifecycleManager`` opcional (M-01.1 DI) que el
            DLQManager usa para obtener su conexión compartida. ``None``
            conserva el fallback de conexión-por-operación pre-M-01.

    Returns:
        CoreEventBus con DLQManager inyectado, listo para ``start()``.
    """
    from sky_claw.antigravity.core.dlq_manager import DLQManager

    resolved_path = db_path or Path.home() / ".sky_claw" / "dlq" / "dlq.db"
    # Construir el bus primero sin DLQ para tener acceso a _handler_index;
    # luego instanciamos la DLQ apuntando a ese índice y reconstruimos con
    # require_dlq=True.  El _handler_index del bus de descarte se descarta.
    scratch_bus = CoreEventBus()
    dlq = DLQManager(
        db_path=resolved_path,
        handler_resolver=scratch_bus._handler_index.get,
        lifecycle=lifecycle,
    )
    bus = CoreEventBus(dlq=dlq, require_dlq=True)
    # Re-direccionar el handler_resolver al índice del bus definitivo.
    dlq._handler_resolver = bus._handler_index.get  # type: ignore[attr-defined]
    return bus
