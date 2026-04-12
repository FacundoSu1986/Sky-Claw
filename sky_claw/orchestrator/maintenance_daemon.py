"""Demonio de mantenimiento pasivo extraído del SupervisorAgent.

Responsabilidad única: ejecutar tareas de limpieza de bajo nivel en background
(pruning de snapshots antiguos, control de tamaño de backups).

Parte de la refactorización ARC-01 (Fat Object → SRP).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.db.snapshot_manager import FileSnapshotManager

logger = logging.getLogger("SkyClaw.MaintenanceDaemon")


def get_max_backup_size_mb() -> int:
    """Obtiene el tamaño máximo de backups desde variable de entorno o default.

    Compartida entre MaintenanceDaemon (pruning) y SupervisorAgent (init de rollback).
    """
    try:
        return int(os.environ.get("ROLLBACK_MAX_SIZE", "1024"))  # Default 1GB
    except ValueError:
        return 1024


class MaintenanceDaemon:
    """Worker asincrónico de mantenimiento pasivo.

    Ejecuta tareas periódicas de limpieza del filesystem y base de datos:
    - Pruning de snapshots antiguos cuando el directorio de backups excede el límite.

    Recibe sus dependencias por inyección (no instancia nada internamente).
    """

    def __init__(self, snapshot_manager: FileSnapshotManager) -> None:
        self._snapshot_manager = snapshot_manager
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Inicia el loop de mantenimiento pasivo como tarea de fondo."""
        if self._task is not None:
            logger.warning(
                "MaintenanceDaemon ya está corriendo, ignorando start() duplicado"
            )
            return
        self._task = asyncio.create_task(
            self._pruning_loop(), name="maintenance-pruning"
        )
        logger.info("MaintenanceDaemon iniciado")

    async def stop(self) -> None:
        """Detiene el loop de mantenimiento de forma grácil."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("MaintenanceDaemon detenido")

    async def _pruning_loop(self) -> None:
        """FASE 1.5: Worker de limpieza pasiva de snapshots antiguos.

        Verifica el tamaño del directorio de backups y elimina los registros
        de journal y snapshots más antiguos si excede el límite.
        """
        while True:
            try:
                # Ejecutar cada hora
                await asyncio.sleep(3600)

                stats = await self._snapshot_manager.get_stats()
                max_size_bytes = get_max_backup_size_mb() * 1024 * 1024

                if stats.total_size_bytes > max_size_bytes:
                    logger.warning(
                        "Límite de tamaño de backups excedido: %d MB > %d MB. Iniciando pruning...",
                        stats.total_size_bytes // (1024 * 1024),
                        max_size_bytes // (1024 * 1024),
                    )

                    # Limpiar snapshots antiguos (más de 30 días)
                    result = await self._snapshot_manager.cleanup_old_snapshots(
                        days_old=30
                    )

                    logger.info(
                        "Pruning completado: %d snapshots eliminados, %d MB liberados",
                        result.deleted_count,
                        result.freed_bytes // (1024 * 1024),
                    )

                    if result.errors:
                        for error in result.errors:
                            logger.error("Error durante pruning: %s", error)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Error en worker de pruning pasivo: %s", e)
