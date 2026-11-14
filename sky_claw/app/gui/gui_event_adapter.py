"""EventBus — sistema de eventos Observer para Sky-Claw GUI.

Propiedad y política (#575 §3; anclado en ``tests/test_gui_event_bus_ciclo_de_vida.py``):

- **Singleton deliberado.** ``event_bus`` es el default del composition root: solo
  ``setup_app`` lo arranca (``app.on_startup(event_bus.start)``) y lo detiene (apagado). El
  resto publica y se suscribe; los controladores lo reciben por inyección. Para tests,
  ``EventBus.aislado()`` entrega un bus independiente y restaura el singleton.
- **Nada se descarta por política.** No hay ``maxsize``: el hilo procesador reenvía cada
  evento al instante con ``call_soon_threadsafe``, así que la cola propia queda vacía y el
  backlog real vive en la cola interna del loop; acotar ``_event_queue`` no acotaría la
  memoria. Y hoy ningún productor publica telemetría de alta frecuencia (el ancla de los
  tests congela qué tipos publica producción). El día que aparezca uno, hay que decidir la
  política de overflow, dejando fuera los eventos de control/HITL.
- **Lo no despachado es ruidoso.** ``stop()`` avisa cuántos eventos quedaron sin que el hilo
  procesador los tomara (``pending``) y ``publish()`` tras ``stop()`` avisa; ambos se retienen
  hasta un ``start()`` posterior. **Límite:** lo que el procesador ya reenvió al loop con
  ``call_soon_threadsafe`` pertenece al loop y ``stop()`` NO lo cuenta (con el loop ocupado,
  ``pending`` es 0 aunque haya eventos esperando). No se pierde mientras el loop siga corriendo,
  pero el bus no da acuse de entrega. ``pending`` es aproximado con hilos activos
  (``Queue.qsize``) y exacto sin consumidor ni productores.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class EventType(Enum):
    MOD_ADDED = "mod_added"
    MOD_REMOVED = "mod_removed"
    MOD_UPDATED = "mod_updated"
    CONFLICT_DETECTED = "conflict_detected"
    CONFLICT_RESOLVED = "conflict_resolved"
    LLM_RESPONSE = "llm_response"
    DOWNLOAD_PROGRESS = "download_progress"
    AGENT_STATUS_CHANGE = "agent_status_change"
    CONFIG_CHANGED = "config_changed"
    EVENT_BROADCAST = "event_broadcast"
    # Parte 5: navegación y selección de mods (controllers → viewmodel/store)
    NAVIGATION_REQUESTED = "navigation_requested"
    MOD_SELECTED = "mod_selected"


@dataclass
class SkyClawEvent:
    type: EventType
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "system"


class EventBus:
    """Thread-safe singleton event bus (Observer pattern)."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._subscribers: dict[EventType, list[Callable]] = defaultdict(list)
        self._subscribers_lock = threading.Lock()
        self._event_queue: queue.Queue = queue.Queue()
        self._running = False
        # True desde cualquier stop() explícito (haya arrancado o no) hasta el próximo start().
        self._detenido = False
        # Bound at start() to the running loop; None means events are dropped.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logger = logging.getLogger("SkyClaw.EventBus")

    @property
    def pending(self) -> int:
        """Eventos publicados que el hilo procesador todavía no tomó (diagnóstico y tests).

        Aproximado con hilos activos (``Queue.qsize``); exacto sin consumidor ni productores.
        No incluye lo que el procesador ya reenvió al loop y este aún no ejecutó.
        """
        return self._event_queue.qsize()

    @classmethod
    @contextmanager
    def aislado(cls) -> Iterator[EventBus]:
        """Bus nuevo e independiente del singleton, que se restaura al salir (para tests).

        Es la receta soportada para aislar estado: evita que cada test toque
        ``EventBus._instance`` y ``_subscribers`` a mano. ``stop()`` corre al salir.

        Contrato: **solo para tests secuenciales**. Reemplaza el singleton mientras dura el
        bloque, así que no es seguro si otro hilo llama ``EventBus()`` en ese lapso.
        """
        with cls._lock:
            guardada = cls._instance
            cls._instance = None
        bus = cls()  # fuera del lock: ``__new__`` lo toma y no es reentrante
        try:
            yield bus
        finally:
            bus.stop()
            with cls._lock:
                cls._instance = guardada

    def subscribe(self, event_type: EventType, callback: Callable) -> None:
        with self._subscribers_lock:
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: EventType, callback: Callable) -> None:
        with self._subscribers_lock:
            if callback in self._subscribers[event_type]:
                self._subscribers[event_type].remove(callback)

    def publish(self, event: SkyClawEvent) -> None:
        if self._detenido:
            self._logger.warning(
                "Bus detenido: el evento %s queda retenido hasta el próximo start()",
                event.type.value,
            )
        self._event_queue.put(event)
        self._logger.info("Evento publicado: %s", event.type.value)

    def _process_events(self) -> None:
        while self._running:
            try:
                event = self._event_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            with self._subscribers_lock:
                callbacks = list(self._subscribers.get(event.type, []))

            loop = self._loop  # capture reference once per event
            if loop is None:
                self._logger.warning(
                    "No hay event loop activo, descartando evento: %s (%d callbacks omitidos)",
                    event.type.value,
                    len(callbacks),
                )
                continue

            for callback in callbacks:
                try:
                    loop.call_soon_threadsafe(callback, event)
                except RuntimeError:
                    # Loop was closed — clear our reference and stop dispatching
                    self._loop = None
                    self._logger.warning(
                        "Event loop cerrado al despachar evento %s, descartando callbacks pendientes",
                        event.type.value,
                    )
                    break
                except Exception as exc:
                    self._logger.error("Error en callback: %s", exc)

    def start(self) -> None:
        if self._running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop → every event would be dropped. Fail loud AND
            # leave _running False so a later start() (from app.on_startup, with
            # the loop live) can still bind successfully. start() must run inside
            # the event loop, never eagerly during synchronous setup.
            self._logger.error(
                "EventBus.start() called without a running event loop — not started. "
                "Start it from an async context (app.on_startup) or every event is dropped."
            )
            return
        self._loop = loop
        self._running = True
        self._detenido = False
        self._processor = threading.Thread(target=self._process_events, daemon=True, name="EventBus-processor")
        self._processor.start()

    def stop(self) -> None:
        self._running = False
        if hasattr(self, "_processor") and self._processor is not None:
            self._processor.join(timeout=2.0)
            self._processor = None
        self._detenido = True
        pendientes = self._event_queue.qsize()
        if pendientes:
            self._logger.warning(
                "EventBus detenido con %d evento(s) sin despachar; quedan retenidos hasta un start()",
                pendientes,
            )


# Singleton global — importar desde aquí en todo el proyecto
event_bus = EventBus()
