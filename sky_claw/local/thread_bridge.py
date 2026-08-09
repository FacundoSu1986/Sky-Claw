"""Puente seguro entre tareas async y mutaciones bloqueantes en hilos."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any


async def esperar_hilo_ininterrumpible(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Espera a que *fn* termine antes de propagar una cancelación pendiente.

    Un hilo ya lanzado no se puede cancelar. Propagar ``CancelledError`` antes
    de que termine permitiría que el caller libere locks o limpie staging
    mientras el hilo todavía muta esos mismos recursos.
    """
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, partial(fn, *args, **kwargs))
    cancelacion_pendiente = False
    while True:
        try:
            resultado = await asyncio.shield(fut)
            if cancelacion_pendiente:
                raise asyncio.CancelledError
            return resultado
        except asyncio.CancelledError:
            cancelacion_pendiente = True
            if fut.done():
                raise
