from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any


async def poll_until(
    condition: Callable[[], Any | Awaitable[Any]],
    *,
    timeout: float = 5.0,
    interval: float = 0.05,
    msg: str = "Condición no cumplida en el tiempo esperado",
) -> None:
    """Evalúa *condition* (callable síncrono o asíncrono) hasta que sea truthy
    o venza *timeout*.

    Lanza AssertionError con *msg* al expirar el tiempo de espera para que
    el fallo sea descriptivo.  Centraliza timeout/interval para que los
    callers no repitan este patrón en línea.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = condition()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"{msg} (timeout={timeout}s)")
