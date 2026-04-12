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
from typing import Any, Callable, Dict, List


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
    data: Dict[str, Any]
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
        self._subscribers: Dict[EventType, List[Callable]] = defaultdict(list)
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
                with self._subscribers_lock:
                    callbacks = list(self._subscribers.get(event.type, []))
                loop = self._loop  # atomic capture — prevents TOCTOU race
                if loop is None or not loop.is_running():
                    self._logger.warning(
                        "No hay event loop activo, descartando evento: %s (%d callbacks omitidos)",
                        event.type.value,
                        len(callbacks),
                    )
                    continue
                for callback in callbacks:
                    try:
                        loop.call_soon_threadsafe(callback, event)
                    except RuntimeError as exc:
                        # Loop closed between is_running() check and call_soon_threadsafe()
                        self._logger.warning(
                            "Event loop cerrado al despachar evento %s: %s",
                            event.type.value,
                            exc,
                        )
                    except Exception as exc:
                        self._logger.error("Error en callback: %s", exc)
            except queue.Empty:
                continue

    def start(self) -> None:
        if not self._running:
            self._running = True
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
            self._processor = threading.Thread(target=self._process_events, daemon=True)
            self._processor.start()

    def stop(self) -> None:
        self._running = False


# Singleton global — importar desde aquí en todo el proyecto
event_bus = EventBus()
