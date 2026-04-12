"""Demonio de telemetría de sistema extraído del SupervisorAgent.

Responsabilidad única: emitir métricas de CPU y RAM a 1 Hz hacia el
bus de eventos de la aplicación, sin conocer al Supervisor ni a la UI.

Parte de la refactorización ARC-01 (Fat Object → SRP).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import psutil

logger = logging.getLogger("SkyClaw.Telemetry")

# Type alias para el callback de emisión de eventos.
# Firma idéntica a InterfaceAgent.send_event(event_type, payload).
EventEmitter = Callable[[str, dict], Awaitable[None]]


class TelemetryDaemon:
    """Worker asincrónico de telemetría de sistema a 1 Hz.

    Recolecta métricas de CPU y RAM del proceso actual via psutil
    y las emite a través de un callback inyectado, desacoplando la
    recolección de métricas de la capa de transporte (WebSocket/IPC).

    Args:
        emit_event: Coroutine que recibe (event_type: str, payload: dict).
                    Típicamente ``interface.send_event``.
        interval: Segundos entre emisiones. Default 1.0 (1 Hz).
    """

    def __init__(self, emit_event: EventEmitter, *, interval: float = 1.0) -> None:
        self._emit_event = emit_event
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Inicia el loop de telemetría como tarea de fondo."""
        if self._task is not None:
            logger.warning(
                "TelemetryDaemon ya está corriendo, ignorando start() duplicado"
            )
            return
        self._task = asyncio.create_task(self._telemetry_loop(), name="telemetry-1hz")
        logger.info("TelemetryDaemon iniciado (interval=%.1fs)", self._interval)

    async def stop(self) -> None:
        """Detiene el loop de telemetría de forma grácil."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("TelemetryDaemon detenido")

    async def _telemetry_loop(self) -> None:
        """Loop estricto de 1 Hz — emite métricas psutil al Gateway."""
        proc = psutil.Process()
        # Primer call de cpu_percent devuelve 0.0 (requiere baseline)
        proc.cpu_percent(interval=None)
        while True:
            try:
                cpu_usage = proc.cpu_percent(interval=None)
                mem = proc.memory_info()
                vmem = psutil.virtual_memory()
                payload = {
                    "cpu": round(cpu_usage, 1),
                    "ram_mb": round(mem.rss / (1024 * 1024), 1),
                    "ram_percent": round(vmem.percent, 1),
                }
                await self._emit_event("telemetry", payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Error en worker de telemetría: %s", e)
            await asyncio.sleep(self._interval)
