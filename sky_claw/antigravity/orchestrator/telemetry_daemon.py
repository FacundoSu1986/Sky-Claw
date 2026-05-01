"""Demonio de telemetría de sistema extraído del SupervisorAgent.

Responsabilidad única: emitir métricas de CPU y RAM a 1 Hz hacia el
CoreEventBus de la aplicación, sin conocer al Supervisor ni a la UI.

Parte de la refactorización ARC-01 (Fat Object → SRP).
Sprint 1: Migrado de callback directo a CoreEventBus.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Final

import psutil

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event

logger = logging.getLogger("SkyClaw.Telemetry")

TELEMETRY_TOPIC: Final[str] = "system.telemetry.metrics"


@dataclass(frozen=True, slots=True)
class TelemetryMetrics:
    """Payload tipado para métricas de sistema."""

    cpu: float
    ram_mb: float
    ram_percent: float


class TelemetryDaemon:
    """Worker asincrónico de telemetría de sistema a 1 Hz.

    Recolecta métricas de CPU y RAM del proceso actual via psutil
    y las publica al CoreEventBus, desacoplando la recolección de
    métricas de la capa de transporte (WebSocket/IPC).

    Args:
        event_bus: Instancia del CoreEventBus donde publicar eventos.
        interval: Segundos entre emisiones. Default 1.0 (1 Hz).
    """

    def __init__(self, event_bus: CoreEventBus, *, interval: float = 1.0) -> None:
        self._event_bus = event_bus
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Inicia el loop de telemetría como tarea de fondo."""
        if self._task is not None:
            logger.warning("TelemetryDaemon ya está corriendo, ignorando start() duplicado")
            return
        self._task = asyncio.create_task(self._telemetry_loop(), name="telemetry-1hz")
        logger.info("TelemetryDaemon iniciado (interval=%.1fs)", self._interval)

    async def stop(self) -> None:
        """Detiene el loop de telemetría de forma grácil."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("TelemetryDaemon detenido")

    async def _telemetry_loop(self) -> None:
        """Loop estricto de 1 Hz — emite métricas psutil al CoreEventBus."""
        proc = psutil.Process()
        # Primer call de cpu_percent devuelve 0.0 (requiere baseline)
        proc.cpu_percent(interval=None)
        while True:
            try:
                cpu_usage = proc.cpu_percent(interval=None)
                mem = proc.memory_info()
                vmem = psutil.virtual_memory()
                metrics = TelemetryMetrics(
                    cpu=round(cpu_usage, 1),
                    ram_mb=round(mem.rss / (1024 * 1024), 1),
                    ram_percent=round(vmem.percent, 1),
                )
                await self._event_bus.publish(
                    Event(
                        topic=TELEMETRY_TOPIC,
                        payload={
                            "cpu": metrics.cpu,
                            "ram_mb": metrics.ram_mb,
                            "ram_percent": metrics.ram_percent,
                        },
                        source="telemetry-daemon",
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Error en worker de telemetría: %s", e)
            await asyncio.sleep(self._interval)
