"""Reactive store: single source of truth for the NiceGUI Forge interface.

Replaces the previous ``_ReactiveVar`` wrapper (which never wired to
NiceGUI's update cycle and produced stale UI state).  The store keeps a
plain dict of values keyed by string and a per-key list of subscribers.
Each ``set`` invocation runs all callbacks registered for that key,
which is how ``@ui.refreshable`` views and ``ui.label`` bindings stay in
sync without polling.
"""

from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from threading import RLock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from nicegui import ui

logger = logging.getLogger(__name__)


class ReactiveStore:
    """Thread-safe key/value store with per-key subscribers."""

    __slots__ = ("_data", "_lock", "_subscribers")

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})
        self._subscribers: dict[str, list[Callable[[], None]]] = defaultdict(list)
        self._lock = RLock()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._data and self._data[key] == value:
                return
            self._data[key] = value
            callbacks = list(self._subscribers.get(key, ()))
        for cb in callbacks:
            try:
                cb()
            except Exception:
                logger.exception("ReactiveStore subscriber for %r failed", key)

    def subscribe(self, key: str, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers[key].append(callback)

        def _unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._subscribers[key].remove(callback)

        return _unsubscribe

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


_store: ReactiveStore | None = None


def get_store() -> ReactiveStore:
    """Return the process-wide ReactiveStore singleton."""
    global _store
    if _store is None:
        _store = ReactiveStore()
    return _store


def reset_store_for_tests() -> None:
    """Drop the singleton so tests start with a fresh store."""
    global _store
    _store = None


def bind_label(
    label: ui.label,
    key: str,
    store: ReactiveStore | None = None,
    fmt: Callable[[Any], str] = str,
) -> Callable[[], None]:
    """Subscribe a NiceGUI label to a store key.

    The label's text is set immediately and again on every store update.
    Returns the unsubscribe callable.
    """
    s = store or get_store()

    def _apply() -> None:
        label.text = fmt(s.get(key))

    _apply()
    return s.subscribe(key, _apply)
