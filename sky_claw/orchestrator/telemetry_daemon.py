"""Demonio de telemetría de sistema extraído del SupervisorAgent.

Responsabilidad única: emitir métricas de CPU y RAM a 1 Hz hacia el
CoreEventBus de la aplicación, sin conocer al Supervisor ni a la UI.

Parte de la refactorización ARC-01 (Fat Object → SRP).
Sprint 1: Migrado de callback directo a CoreEventBus.
Fase 6: Añadidas emisiones GUI-facing ``ops.telemetry`` y ``ops.system_log``
         cuando el uso de RAM supera el umbral configurable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Final

import psutil

from sky_claw.core.event_bus import (
    OPS_SYSTEM_LOG_TOPIC,
    OPS_TELEMETRY_TOPIC,
    CoreEventBus,
    Event,
)

logger = logging.getLogger("SkyClaw.Telemetry")

#: Tópico legacy conservado por compatibilidad con suscriptores existentes.
TELEMETRY_TOPIC: Final[str] = "system.telemetry.metrics"

#: Porcentaje de RAM del sistema a partir del cual se emite ops.system_log.
DEFAULT_RAM_WARN_THRESHOLD_PCT: Final[float] = 80.0

#: Segundos mínimos entre dos advertencias consecutivas de RAM.
DEFAULT_RAM_WARN_COOLDOWN_S: Final[float] = 60.0


@dataclass(frozen=True, slots=True)
class TelemetryMetrics:
    """Payload tipado para métricas de sistema."""

    cpu: float
    ram_mb: float
    ram_percent: float


class TelemetryDaemon:
    """Worker asincrónico de telemetría de sistema a 1 Hz.

    Recolecta métricas de CPU y RAM del proceso actual via psutil
    y las publica al CoreEventBus en dos tópicos:

    * ``system.telemetry.metrics`` — tópico legacy (backward-compatible).
    * ``ops.telemetry`` — tópico GUI-facing consumido por el Operations Hub.

    Adicionalmente, cuando el uso global de RAM del sistema supera
    ``ram_warn_threshold_pct``, emite un evento ``ops.system_log`` de nivel
    ``warning`` (limitado a uno por ventana de ``ram_warn_cooldown_s``).

    Args:
        event_bus:             Instancia del CoreEventBus donde publicar.
        interval:              Segundos entre emisiones.  Default 1.0 (1 Hz).
        ram_warn_threshold_pct: Porcentaje de RAM que activa el aviso.
        ram_warn_cooldown_s:   Cooldown en segundos entre avisos consecutivos.
    """

    def __init__(
        self,
        event_bus: CoreEventBus,
        *,
        interval: float = 1.0,
        ram_warn_threshold_pct: float = DEFAULT_RAM_WARN_THRESHOLD_PCT,
        ram_warn_cooldown_s: float = DEFAULT_RAM_WARN_COOLDOWN_S,
    ) -> None:
        self._event_bus = event_bus
        self._interval = interval
        self._ram_warn_threshold_pct = ram_warn_threshold_pct
        self._ram_warn_cooldown_s = ram_warn_cooldown_s
        self._task: asyncio.Task[None] | None = None
        self._start_time: float = 0.0
        self._last_ram_warn_at: float = 0.0

    async def start(self) -> None:
        """Inicia el loop de telemetría como tarea de fondo."""
        if self._task is not None:
            logger.warning("TelemetryDaemon ya está corriendo, ignorando start() duplicado")
            return
        self._start_time = time.monotonic()
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

    # ────────────────────────────────────────── Internals ───────────────────

    async def _telemetry_loop(self) -> None:
        """Loop estricto de 1 Hz — emite métricas psutil al CoreEventBus."""
        proc = psutil.Process()
        # Primer call de cpu_percent devuelve 0.0 (requiere baseline)
        proc.cpu_percent(interval=None)

        while True:
            try:
                await self._emit_tick(proc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Error en worker de telemetría: %s", exc)
            await asyncio.sleep(self._interval)

    async def _emit_tick(self, proc: psutil.Process) -> None:
        """Recolecta y publica un ciclo de métricas."""
        cpu_usage = proc.cpu_percent(interval=None)
        mem = proc.memory_info()
        vmem = psutil.virtual_memory()

        metrics = TelemetryMetrics(
            cpu=round(float(cpu_usage), 1),
            ram_mb=round(float(mem.rss) / (1024.0 * 1024.0), 1),
            ram_percent=round(float(vmem.percent), 1),
        )
        uptime_s = round(time.monotonic() - self._start_time, 1)

        # ── Legacy tópico (backward-compatible) ─────────────────────────────
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

        # ── Nuevo tópico GUI-facing ─────────────────────────────────────────
        await self._event_bus.publish(
            Event(
                topic=OPS_TELEMETRY_TOPIC,
                payload={
                    "cpu": metrics.cpu,
                    "ram_mb": metrics.ram_mb,
                    "ram_percent": metrics.ram_percent,
                    "uptime_s": uptime_s,
                },
                source="telemetry-daemon",
            )
        )

        # ── Advertencia de RAM alta → ops.system_log ─────────────────────────
        now = time.monotonic()
        if (
            metrics.ram_percent >= self._ram_warn_threshold_pct
            and now - self._last_ram_warn_at >= self._ram_warn_cooldown_s
        ):
            self._last_ram_warn_at = now
            msg = (
                f"RAM del sistema al {metrics.ram_percent:.1f}% "
                f"(umbral: {self._ram_warn_threshold_pct:.0f}%)"
            )
            logger.warning(msg)
            await self._event_bus.publish(
                Event(
                    topic=OPS_SYSTEM_LOG_TOPIC,
                    payload={
                        "level": "warning",
                        "message": msg,
                        "source": "telemetry-daemon",
                        "ts": time.time(),
                    },
                    source="telemetry-daemon",
                )
            )
