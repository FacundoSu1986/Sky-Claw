"""Rollback del envío de chat — seam compartido de la capa de vista.

Histórico: este módulo era la sección preview de chat del viejo home pre-Forge
(``create_chat_preview``). El shell Forge lleva su propio chat pero reutiliza el
circuito de envío con rollback, que es el que queda acá y lo único vivo:

- :func:`_try_send_with_rollback`: envío optimista (clear-before-send) con
  restauración + notificación ante fallas sync Y async.
- :func:`_do_rollback`: mejor esfuerzo de restaurar/notificar sin tragar por
  completo las fallas de los callbacks.

VIEW PURO - Sin lógica de negocio, solo presentación.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _do_rollback(
    text: str,
    exc: BaseException,
    restore_fn: Callable[[str], Any],
    notify_fn: Callable[[str], Any],
) -> None:
    """Best-effort rollback: try restore + notify, swallow either's failure.

    A broken ``restore_fn`` must not block the user-facing notification, and
    a broken ``notify_fn`` must not crash the caller — at least one of the
    two will fire so the user gets some signal.
    """
    logger.warning("chat send failed (rollback engaged): %s", exc)
    try:
        restore_fn(text)
    except Exception as restore_exc:  # noqa: BLE001
        logger.error("chat restore_fn raised (input text may be lost): %s", restore_exc)
    try:
        notify_fn(f"⚠️ No se pudo enviar el mensaje: {exc}")
    except Exception as notify_exc:  # noqa: BLE001
        logger.error("chat notify_fn raised (user got no feedback): %s", notify_exc)


def _try_send_with_rollback(
    msg: str,
    on_send: Callable[[str], Any],
    restore_fn: Callable[[str], Any],
    notify_fn: Callable[[str], Any],
    *,
    original_text: str | None = None,
) -> None:
    """Optimistic send: the caller has already cleared the input.

    Rollback triggers for BOTH sync exceptions raised by ``on_send`` AND
    async failures when ``on_send`` returns an awaitable (e.g. the wiring
    ``lambda msg: asyncio.create_task(controller.handle_send_message(msg))``).
    For the async path we attach a ``done_callback`` that inspects the
    future's exception.

    Cancellation is intentionally re-raised on the sync path (explicit
    ``except asyncio.CancelledError: raise``) and ignored on the async
    path so a cancelled task doesn't trigger user-facing notifications.

    ``original_text`` defaults to ``msg`` for legacy callers but should be
    passed by UI handlers that ``.strip()`` the input — restoring the
    stripped version would silently drop leading/trailing whitespace the
    user typed (Copilot review on PR #144).
    """
    text_for_restore = original_text if original_text is not None else msg
    try:
        result = on_send(msg)
    except asyncio.CancelledError:
        raise  # never swallow cancellation
    except Exception as exc:  # noqa: BLE001 — surfaced via notify_fn for UX
        _do_rollback(text_for_restore, exc, restore_fn, notify_fn)
        return

    # Async path: on_send returned an awaitable (Task / Future / coroutine).
    # The real GUI wiring uses ``lambda msg: asyncio.create_task(...)`` so
    # the rollback contract here is what makes daemon/network failures
    # actually trigger the restore-and-notify cycle.
    if asyncio.iscoroutine(result):
        result = asyncio.ensure_future(result)
    if asyncio.isfuture(result):

        def _on_done(task: asyncio.Future[Any]) -> None:
            if task.cancelled():
                return  # cancellation is not a user-facing failure
            exc = task.exception()
            if exc is not None:
                _do_rollback(text_for_restore, exc, restore_fn, notify_fn)

        result.add_done_callback(_on_done)
