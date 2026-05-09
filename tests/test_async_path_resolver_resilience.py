"""
C-2: Tests de resiliencia para AsyncPathResolver — Future leak bajo excepción inesperada
y cancelación del owner.

Estos tests FALLAN (timeout o hang) con la implementación actual porque:
- Una excepción no capturada en _resolve_blocking (ej. MemoryError) no limpia _inflight
  ni resuelve el Future → los waiters cuelgan indefinidamente.
- Cancelar el task owner deja el Future sin resolver → mismo efecto.

Tras el fix C-2 (except BaseException + cleanup atómico en _inflight), ambos pasan.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import threading
from unittest.mock import patch

import pytest

from sky_claw.antigravity.core.async_path_resolver import (
    AsyncPathResolver,
)

_PATH = "/probe/c2/path"
_KEY = (_PATH, True)


@pytest.fixture
def resolver() -> AsyncPathResolver:
    """AsyncPathResolver con logger silencioso, sin estado previo."""
    null_logger = logging.getLogger("tests.c2.async_path_resolver")
    null_logger.addHandler(logging.NullHandler())
    return AsyncPathResolver(logger=null_logger)


# ---------------------------------------------------------------------------
# Test 1 — excepción inesperada (MemoryError)
# ---------------------------------------------------------------------------


async def test_unexpected_exception_propagates_to_waiters_and_clears_inflight(
    resolver: AsyncPathResolver,
) -> None:
    """C-2 RED: MemoryError en _resolve_blocking deja waiters colgados (broken).
    C-2 GREEN: todos reciben MemoryError y _inflight queda vacío (fixed).
    """

    def _raises_oom(raw_path: str, strict: bool) -> pathlib.Path:  # noqa: ARG001
        raise MemoryError("C-2 OOM simulado")

    with patch.object(AsyncPathResolver, "_resolve_blocking", staticmethod(_raises_oom)):
        owner = asyncio.create_task(resolver.resolve_safe(_PATH), name="c2-owner")
        waiter1 = asyncio.create_task(resolver.resolve_safe(_PATH), name="c2-waiter-1")
        waiter2 = asyncio.create_task(resolver.resolve_safe(_PATH), name="c2-waiter-2")

        # RED: con código roto waiters cuelgan → TimeoutError (test falla).
        # GREEN: los 3 completan en <1 s con MemoryError.
        done, pending = await asyncio.wait_for(
            asyncio.wait({owner, waiter1, waiter2}),
            timeout=5.0,
        )

    assert not pending, f"Tasks colgadas (no debería ocurrir): {pending}"

    for task in done:
        exc = task.exception() if not task.cancelled() else None
        assert isinstance(exc, MemoryError), (
            f"Task '{task.get_name()}': esperado MemoryError, obtenido "
            f"{'Cancelled' if task.cancelled() else type(exc).__name__}"
        )

    assert resolver._inflight == {}, f"_inflight debe estar vacío tras excepción inesperada: {list(resolver._inflight)}"
    assert _KEY not in resolver._cache, "_cache no debe registrar un path cuya resolución falló"


# ---------------------------------------------------------------------------
# Test 2 — cancelación del owner
# ---------------------------------------------------------------------------


async def test_owner_cancellation_does_not_hang_waiters(
    resolver: AsyncPathResolver,
) -> None:
    """C-2 RED: cancelar el owner deja waiters colgados indefinidamente (broken).
    C-2 GREEN: waiters terminan (cancelled o con excepción) y _inflight queda vacío (fixed).
    """
    thread_started = threading.Event()
    thread_release = threading.Event()

    def _blocking_resolver(raw_path: str, strict: bool) -> pathlib.Path:  # noqa: ARG001
        thread_started.set()
        thread_release.wait(timeout=15.0)
        return pathlib.Path(raw_path)

    with patch.object(AsyncPathResolver, "_resolve_blocking", staticmethod(_blocking_resolver)):
        # El owner arranca primero para registrarse en _inflight
        owner = asyncio.create_task(resolver.resolve_safe(_PATH), name="c2-owner")
        await asyncio.sleep(0)  # Ceder el loop para que el owner arranque el thread

        waiter1 = asyncio.create_task(resolver.resolve_safe(_PATH), name="c2-waiter-1")
        waiter2 = asyncio.create_task(resolver.resolve_safe(_PATH), name="c2-waiter-2")

        # Esperar confirmación de que el thread está bloqueado
        await asyncio.to_thread(lambda: thread_started.wait(timeout=5.0))

        # Cancelar el owner mientras está bloqueado en asyncio.to_thread
        owner.cancel()

        try:
            # RED: waiters cuelgan → TimeoutError (test falla).
            # GREEN: los 3 completan en <5 s (owner cancelled, waiters reciben CancelledError).
            done, pending = await asyncio.wait_for(
                asyncio.wait({owner, waiter1, waiter2}),
                timeout=5.0,
            )
        finally:
            thread_release.set()  # Siempre liberar el thread para evitar fuga

    assert not pending, f"Tasks colgadas tras cancelación del owner: {[t.get_name() for t in pending]}"

    # Todos los tasks deben haber terminado (cancelled o con excepción)
    for task in done:
        assert task.done(), f"Task '{task.get_name()}' no terminó"

    assert resolver._inflight == {}, f"_inflight debe estar vacío tras cancelación: {list(resolver._inflight)}"
    assert _KEY not in resolver._cache, "_cache no debe contener un path cuya resolución fue cancelada"
