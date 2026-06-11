"""Strong-ref task tracking for GUI fire-and-forget callbacks (obs #211 / PR-4).

NiceGUI button/switch handlers are synchronous lambdas, so async work must be
scheduled with ``asyncio.create_task``. Bare ``create_task`` in a lambda keeps
no strong reference — the event loop only holds the task weakly between steps,
so it can be garbage-collected mid-flight (the risk ``ws_daemon.py`` documents)
— and an exception surfaces only at task destruction, far from the click.

``create_tracked_task`` keeps the task alive in a module-level set and logs any
failure immediately through the structured logging pipeline (correlation ids +
secret redaction). RUF006 cannot flag bare ``create_task`` inside lambdas, so
``tests/test_gui_task_tracking.py`` carries a regression guard for the GUI
files migrated to this helper.

This module deliberately has no NiceGUI import so it stays unit-testable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

logger = logging.getLogger(__name__)

#: Strong references to in-flight GUI tasks (discarded on completion).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def create_tracked_task(coro: Coroutine[Any, Any, Any], *, name: str = "") -> asyncio.Task[Any]:
    """Schedule *coro* with a strong reference and immediate failure logging.

    Args:
        coro: The coroutine to run (e.g. a controller callback).
        name: Task name surfaced in logs (``gui-<what>`` by convention).

    Returns:
        The created task, so callers may still await or cancel it.
    """
    task = asyncio.create_task(coro, name=name or "gui-task")
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return  # normal shutdown path, not an error
    exc = task.exception()
    if exc is not None:
        # Repo convention for done callbacks (router.py / comms/interface.py):
        # explicit (type, exc, tb) tuple so the full traceback always renders.
        logger.error(
            "GUI background task %r failed: %s",
            task.get_name(),
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
