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


def _read_gpu_percent() -> float | None:
    """Return NVIDIA GPU utilization (0-100) or ``None`` when unavailable.

    Honest reporting: ``pynvml`` is an *optional* dependency (no NVIDIA GPU,
    or simply not installed → most dev machines and CI). Rather than fabricate
    a number, every failure path returns ``None`` so the GUI can render "N/D".
    """
    try:
        import pynvml  # type: ignore[import-untyped]

        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            return float(util.gpu)
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        # ImportError (not installed), NVMLError (no GPU/driver), anything else.
        return None


@dataclass(frozen=True, slots=True)
class TelemetryMetrics:
    """Payload tipado para métricas de sistema.

    ``gpu`` es ``None`` cuando no hay GPU NVIDIA / pynvml disponible — la GUI
    lo muestra como "N/D" en lugar de inventar un porcentaje.
    """

    cpu: float
    ram_mb: float
    ram_percent: float
    gpu: float | None = None

    def as_payload(self) -> dict:
        """Serializa las métricas al dict publicado en el CoreEventBus."""
        return {
            "cpu": self.cpu,
            "ram_mb": self.ram_mb,
            "ram_percent": self.ram_percent,
            "gpu": self.gpu,
        }


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

    async def run(self) -> None:
        """Ejecuta el loop en primer plano para que el caller lo supervise (H-2)."""
        await self._telemetry_loop()

    async def stop(self) -> None:
        """Detiene el loop de telemetría de forma grácil."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("TelemetryDaemon detenido")

    def _sample(self, proc: psutil.Process) -> TelemetryMetrics:
        """Recolecta una muestra puntual de CPU/RAM/GPU.

        Aislado del loop para ser testeable sin event bus ni asyncio.

        ``cpu`` es CPU **del host** (``psutil.cpu_percent``), no del proceso: la
        GUI lo muestra bajo "Vitalidad del sistema", así que reportar solo el
        consumo de Sky-Claw mostraría un procesador ocioso mientras el equipo
        está saturado. Requiere haber cebado ``psutil.cpu_percent(interval=None)``
        para que la primera lectura no devuelva 0.0. ``ram_mb`` sí es del proceso
        (RSS) y ``ram_percent`` es del host (memoria virtual).
        """
        cpu_usage = psutil.cpu_percent(interval=None)
        mem = proc.memory_info()
        vmem = psutil.virtual_memory()
        return TelemetryMetrics(
            cpu=round(cpu_usage, 1),
            ram_mb=round(mem.rss / (1024 * 1024), 1),
            ram_percent=round(vmem.percent, 1),
            gpu=_read_gpu_percent(),
        )

    async def _telemetry_loop(self) -> None:
        """Loop estricto de 1 Hz — emite métricas psutil al CoreEventBus."""
        proc = psutil.Process()
        # Primer call de cpu_percent devuelve 0.0 (requiere baseline). Cebamos el
        # contador host-wide que usa _sample.
        psutil.cpu_percent(interval=None)
        while True:
            try:
                metrics = self._sample(proc)
                await self._event_bus.publish(
                    Event(
                        topic=TELEMETRY_TOPIC,
                        payload=metrics.as_payload(),
                        source="telemetry-daemon",
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Error en worker de telemetría: %s", e)
            await asyncio.sleep(self._interval)
