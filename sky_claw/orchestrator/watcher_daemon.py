"""Demonio de monitoreo de modlist extraído del SupervisorAgent.

Responsabilidad única: polling asincrónico del ``modlist.txt`` de MO2,
detectando modificaciones externas y disparando un callback de análisis.

Se usa polling en lugar de inotify porque este último falla a través
del protocolo 9P (WSL2 → Windows).

Parte de la refactorización ARC-01 (Fat Object → SRP).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.core.database import DatabaseAgent

logger = logging.getLogger("SkyClaw.Watcher")

# Type alias para el callback de cambio detectado.
OnChangeCallback = Callable[[], Awaitable[None]]


class WatcherDaemon:
    """Worker asincrónico de monitoreo de modlist.txt.

    Realiza polling del ``st_mtime`` del archivo de modlist de MO2
    y dispara un callback cuando detecta una modificación externa.

    Args:
        modlist_path: Ruta absoluta al ``modlist.txt`` a monitorear.
        profile_name: Nombre del perfil MO2 (para clave de memoria en DB).
        db: ``DatabaseAgent`` para persistir el último mtime conocido.
        on_change: Coroutine invocada cuando se detecta un cambio.
        interval: Segundos entre cada chequeo. Default 10.0.
    """

    def __init__(
        self,
        modlist_path: str,
        profile_name: str,
        db: DatabaseAgent,
        on_change: OnChangeCallback,
        *,
        interval: float = 10.0,
    ) -> None:
        self._modlist_path = modlist_path
        self._profile_name = profile_name
        self._db = db
        self._on_change = on_change
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Inicia el loop de monitoreo como tarea de fondo."""
        if self._task is not None:
            logger.warning(
                "WatcherDaemon ya está corriendo, ignorando start() duplicado"
            )
            return
        self._task = asyncio.create_task(self._watch_loop(), name="watcher-modlist")
        logger.info(
            "WatcherDaemon iniciado (path=%s, interval=%.1fs)",
            self._modlist_path,
            self._interval,
        )

    async def stop(self) -> None:
        """Detiene el loop de monitoreo de forma grácil."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("WatcherDaemon detenido")

    async def _watch_loop(self) -> None:
        """Polling asincrónico del modlist.txt.

        Evita inotify porque falla a través del protocolo 9P (WSL2 -> Windows).
        """
        mem_key = f"modlist_mtime_{self._profile_name}"

        while True:
            try:
                if os.path.exists(self._modlist_path):
                    current_mtime = os.stat(self._modlist_path).st_mtime
                    last_mtime_str = await self._db.get_memory(mem_key)
                    last_mtime = float(last_mtime_str) if last_mtime_str else 0.0

                    if current_mtime > last_mtime:
                        logger.info(
                            "Modificación detectada en MO2 desde fuera del agente. Iniciando análisis proactivo."
                        )
                        await self._db.set_memory(
                            mem_key, str(current_mtime), time.time()
                        )
                        await self._on_change()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("RCA: Fallo en el watcher asincrónico: %s", e)

            await asyncio.sleep(self._interval)
