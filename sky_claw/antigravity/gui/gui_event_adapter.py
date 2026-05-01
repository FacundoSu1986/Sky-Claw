"""EventBus — sistema de eventos Observer para Sky-Claw GUI."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


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
        self._logger = logging.getLogger("SkyClaw.EventBus")

    def subscribe(self, event_type: EventType, callback: Callable) -> None:
        with self._subscribers_lock:
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: EventType, callback: Callable) -> None:
        with self._subscribers_lock:
            if callback in self._subscribers[event_type]:
                self._subscribers[event_type].remove(callback)

    def publish(self, event: SkyClawEvent) -> None:
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
        if not self._running:
            self._running = True
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
            self._processor = threading.Thread(target=self._process_events, daemon=True, name="EventBus-processor")
            self._processor.start()

    def stop(self) -> None:
        self._running = False
        if hasattr(self, "_processor") and self._processor is not None:
            self._processor.join(timeout=2.0)
            self._processor = None


# Singleton global — importar desde aquí en todo el proyecto
event_bus = EventBus()
