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
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from sky_claw.core.dlq_manager import DLQManager

logger = logging.getLogger(__name__)

Subscriber = Callable[["Event"], Awaitable[None]]

# ---------------------------------------------------------------------------
# Tópicos GUI-facing (consumidos por el puente WebSocket → Operations Hub)
# ---------------------------------------------------------------------------
#
# Convención jerárquica: ``ops.<category>.<subcategory>``.  El puente WS y el
# cliente frontend matchean con patrones ``fnmatch`` de la forma
# ``ops.<category>.*``; los tópicos planos (ej. ``ops.telemetry``) son
# ignorados por esos patrones, de modo que cualquier emisor nuevo DEBE usar
# al menos un sub-segmento después del prefijo.

#: Tick de métricas del sistema — emitido por TelemetryDaemon a 1 Hz.
#: Sub-tópico único (no hay otras categorías de telemetría hoy).
OPS_TELEMETRY_TOPIC: Final[str] = "ops.telemetry.tick"

#: Prefijo para cambios de ciclo de vida de procesos/herramientas.
#: Los emisores construyen el tópico final como ``ops.process.<state>``
#: donde ``state ∈ {"started", "completed", "error"}``.  Usar el helper
#: :func:`ops_process_topic` para no hardcodear el prefijo.
OPS_PROCESS_TOPIC_PREFIX: Final[str] = "ops.process"

#: Prefijo para entradas de log estructurado destinadas al Orbe de Visión.
#: Los emisores construyen el tópico final como ``ops.log.<level>``
#: donde ``level ∈ {"info", "warning", "error", "critical"}``.  Usar el
#: helper :func:`ops_log_topic` para no hardcodear el prefijo.
OPS_LOG_TOPIC_PREFIX: Final[str] = "ops.log"


def ops_process_topic(state: str) -> str:
    """Construye el tópico CoreEventBus para una transición de proceso.

    Args:
        state: Estado del proceso (``started`` | ``completed`` | ``error``).

    Returns:
        Cadena con formato ``ops.process.<state>``.
    """
    return f"{OPS_PROCESS_TOPIC_PREFIX}.{state}"


def ops_log_topic(level: str) -> str:
    """Construye el tópico CoreEventBus para una entrada de log.

    Args:
        level: Nivel de severidad (``info`` | ``warning`` | ``error`` |
            ``critical``).

    Returns:
        Cadena con formato ``ops.log.<level>``.
    """
    return f"{OPS_LOG_TOPIC_PREFIX}.{level}"


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
    """

    def __init__(self, *, max_queue_size: int = 1024, dlq: DLQManager | None = None) -> None:
        self._subscriptions: list[tuple[str, Subscriber]] = []
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(
            maxsize=max_queue_size,
        )
        self._dispatch_task: asyncio.Task[None] | None = None
        self._running: bool = False
        self._dlq = dlq
        self._handler_index: dict[str, Subscriber] = {}

    async def start(self) -> None:
        """Inicia el loop de dispatch y el worker DLQ (si hay DLQ) como tareas de fondo."""
        if self._dispatch_task is not None:
            logger.warning("CoreEventBus ya está corriendo, ignorando start() duplicado")
            return
        if self._dlq is not None:
            await self._dlq.start()
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="core-event-bus-dispatch")
        logger.info("CoreEventBus iniciado (queue_max=%d)", self._queue.maxsize)

    async def stop(self) -> None:
        """Detiene el loop de dispatch y el worker DLQ de forma grácil."""
        if self._dispatch_task is None:
            return
        self._running = False
        await self._queue.put(None)  # Sentinel de apagado
        with contextlib.suppress(asyncio.CancelledError):
            await self._dispatch_task
        self._dispatch_task = None
        if self._dlq is not None:
            await self._dlq.stop()
        logger.info("CoreEventBus detenido")

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
        await self._queue.put(event)

    async def _dispatch_loop(self) -> None:
        """Extrae eventos de la cola y los enruta sin bloquear el hilo principal."""
        while self._running:
            event = await self._queue.get()

            if event is None:  # Sentinel de apagado
                self._queue.task_done()
                break

            for pattern, callback in self._subscriptions:
                if fnmatch.fnmatch(event.topic, pattern):
                    asyncio.create_task(self._safe_execute(callback, event))

            self._queue.task_done()

    async def _safe_execute(self, callback: Subscriber, event: Event) -> None:
        """Ejecuta un consumidor aislando sus fallos del bus. Fallos van a DLQ si está activa."""
        try:
            await callback(event)
        except Exception as exc:
            cb_name = getattr(callback, "__name__", repr(callback))
            logger.error(
                "Fallo en consumidor %s procesando evento '%s': %s",
                cb_name,
                event.topic,
                exc,
                exc_info=True,
            )
            if self._dlq is not None:
                try:
                    await self._dlq.enqueue(event, callback, exc)
                except Exception:
                    logger.critical(
                        "DLQ enqueue falló para evento '%s' handler '%s' — evento perdido",
                        event.topic,
                        cb_name,
                        exc_info=True,
                    )

    @staticmethod
    def _handler_name(cb: Subscriber) -> str:
        """Genera un identificador estable para un callable."""
        mod = getattr(cb, "__module__", "unknown")
        qn = getattr(cb, "__qualname__", None)
        return f"{mod}.{qn}" if qn else repr(cb)


def create_bus_with_dlq(db_path: Path | None = None) -> CoreEventBus:
    """Factory que conecta un CoreEventBus con un DLQManager pre-cableado.

    Args:
        db_path: Ruta al archivo SQLite. Default: ``~/.sky_claw/dlq/dlq.db``.

    Returns:
        CoreEventBus con DLQManager inyectado, listo para ``start()``.
    """
    from sky_claw.core.dlq_manager import DLQManager

    bus = CoreEventBus()
    resolved_path = db_path or Path.home() / ".sky_claw" / "dlq" / "dlq.db"
    dlq = DLQManager(
        db_path=resolved_path,
        handler_resolver=bus._handler_index.get,
    )
    bus._dlq = dlq
    return bus
