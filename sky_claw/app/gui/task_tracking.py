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

#: Espera máxima del drenado de apagado, alineada con
#: ``AppContext._startup_shutdown_timeout_s``.
_DRAIN_TIMEOUT_S = 5.0


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


async def drain_tracked_tasks(timeout_s: float = _DRAIN_TIMEOUT_S) -> None:
    """Espera a las tasks en vuelo al apagar y cancela las que no terminan.

    Las tasks de ``create_tracked_task`` viven en un set propio: no son las de
    ``nicegui.background_tasks`` ni las de ``AppContext._background_tasks``, así
    que hasta acá **nadie las drenaba**. El caso real: un "Análisis profundo
    (xEdit)" —que tarda minutos— sigue corriendo cuando el ``ExitWatchdog``
    apaga la app; al terminar retomaba contra un ``DatabaseAgent`` ya cerrado y
    su ``persist_record_conflicts`` moría con ``DatabaseAgent not initialized``,
    tragado por el ``except Exception`` del handler. Cero filas, en silencio.

    El apagado tampoco puede quedar rehén de una task colgada, así que el
    drenado es acotado: se espera ``timeout_s`` y se cancela lo que sobre,
    nombrando en el log qué se canceló (nunca un truncado silencioso).

    Args:
        timeout_s: Espera máxima antes de cancelar. El default sigue la
            convención de ``AppContext._startup_shutdown_timeout_s``.
    """
    pendientes = {task for task in _BACKGROUND_TASKS if not task.done()}
    if not pendientes:
        return

    _, sobrantes = await asyncio.wait(pendientes, timeout=timeout_s)
    if not sobrantes:
        return

    logger.warning(
        "Apagado: %d task(s) de la GUI no terminaron en %.1fs y se cancelan: %s",
        len(sobrantes),
        timeout_s,
        ", ".join(sorted(task.get_name() for task in sobrantes)),
    )
    for task in sobrantes:
        task.cancel()
    # ``return_exceptions`` para que un fallo de una no impida cancelar el resto;
    # ``_on_task_done`` ya lo logueó con su traceback.
    await asyncio.gather(*sobrantes, return_exceptions=True)


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
