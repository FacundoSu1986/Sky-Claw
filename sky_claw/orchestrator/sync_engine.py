"""Sync Engine – Producer-Consumer orchestrator for mod synchronisation.

Reads ``modlist.txt`` via :class:`MO2Controller` (producer), fans out
mod-metadata fetches to a pool of async workers (consumers) through an
:class:`asyncio.Queue`, and persists results in micro-batches via
:class:`AsyncModRegistry`.

Includes a fully automated Update Cycle with controlled concurrency and
 robust exception handling to prevent single-mod failures from crashing
entire batches.

FASE 1.5: Integración with RollbackManager for atomic file operations.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
import pathlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from sky_claw.config import SystemPaths

# FASE 1.5: Imports de componentes de rollback
from sky_claw.scraper.masterlist import (
    CircuitOpenError,
    MasterlistClient,
    MasterlistFetchError,
)
from sky_claw.security.hitl import Decision, HITLGuard

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from sky_claw.db.async_registry import AsyncModRegistry
    from sky_claw.db.journal import OperationType
    from sky_claw.db.rollback_manager import RollbackManager
    from sky_claw.mo2.vfs import MO2Controller
    from sky_claw.scraper.nexus_downloader import NexusDownloader

logger = logging.getLogger(__name__)

_POISON = None

# FASE 1.5: Constante para directorio de staging de backups
BACKUP_STAGING_DIR = pathlib.Path(".skyclaw_backups/")


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Tunables for the sync engine."""

    worker_count: int = 4
    batch_size: int = 20
    max_retries: int = 5
    api_semaphore_limit: int = 4
    # PRF-01: Tightened from 200 to 50 — max 50*20=1000 items buffered.
    # Provides backpressure without excessive RAM usage for large modlists.
    queue_maxsize: int = 50
    # PRF-01: Timeout (seconds) for producer put — detects dead workers.
    queue_put_timeout: float = 120.0
    # FASE 1.5: Configuración de rollback
    enable_rollback: bool = True
    rollback_max_size_mb: int = 1024  # Default 1GB
    max_pruning_age_days: int = 30  # Días para pruning


@dataclass
class SyncResult:
    """Aggregated outcome of a sync run."""

    processed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    # FASE 1.5: Campos de rollback
    rollback_performed: bool = False
    rollback_success: bool = False
    rollback_transaction_id: int | None = None


@dataclass
class UpdatePayload:
    """Payload generated after a full update cycle for Telegram reporting."""

    total_checked: int = 0
    updated_mods: list[dict[str, Any]] = field(default_factory=list)
    failed_mods: list[dict[str, Any]] = field(default_factory=list)
    up_to_date_mods: list[str] = field(default_factory=list)
    # FASE 1.5: Campos de rollback
    rollback_performed: bool = False
    rollback_transaction_id: int | None = None


# BUG-001 FIX: SyncMetrics refactorizado como dataclass con asyncio.Lock
@dataclass
class SyncMetrics:
    """Métricas asíncronas del SyncEngine."""

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _error_count: int = field(default=0, init=False)
    _error_types: dict[str, int] = field(default_factory=lambda: defaultdict(int), init=False)

    async def get_error_count(self) -> int:
        """Retorna el contador de errores."""
        async with self._lock:
            return self._error_count

    async def get_error_types(self) -> dict[str, int]:
        """Retorna una copia del diccionario de tipos de errores."""
        async with self._lock:
            return dict(self._error_types)

    async def record_error(self, error: Exception) -> None:
        """Registra un error de forma asíncrona."""
        async with self._lock:
            self._error_count += 1
            self._error_types[type(error).__name__] += 1

    async def increment_error_type(self, error_type_name: str) -> None:
        """Incrementa un tipo de error específico de forma asíncrona."""
        async with self._lock:
            self._error_count += 1
            self._error_types[error_type_name] += 1


class SyncEngine:
    """Orchestrator for mod synchronisation and automatic updates.

    FASE 1.5: Integración con RollbackManager para operaciones at archivos at resiliencia.

    Parameters
    ----------
    mo2:
        Controller for the MO2 portable instance.
    masterlist:
        Async client for Nexus Mods API metadata.
    registry:
        Async database layer for micro-batched persistence.
    config:
        Engine tunables (worker count, batch size, retry policy).
    downloader:
        Robust Nexus downloader (Required for automatic updates).
    rollback_manager:
        FASE 1.5: Gestor de rollback para operaciones de archivos.
    """

    def __init__(
        self,
        mo2: MO2Controller,
        masterlist: MasterlistClient,
        registry: AsyncModRegistry,
        config: SyncConfig | None = None,
        downloader: NexusDownloader | None = None,
        hitl: HITLGuard | None = None,
        rollback_manager: RollbackManager | None = None,  # FASE 1.5
        fetch_retry_wait=None,
    ) -> None:
        self._mo2 = mo2
        self._masterlist = masterlist
        self._registry = registry
        self._cfg = config or SyncConfig()
        self._downloader = downloader
        self._hitl = hitl
        self._rollback_manager = rollback_manager  # FASE 1.5
        self._download_tasks: set[asyncio.Task[Any]] = set()
        # CONCURRENCY: Limit simultaneous downloads to 3 to avoid saturating
        # bandwidth, exhausting file descriptors, and triggering Nexus Mods
        # rate-limiting / IP bans.
        self._download_semaphore = asyncio.Semaphore(3)
        self._shutdown_event = asyncio.Event()
        # Retry wait strategy; override in tests to avoid real sleeps
        self._fetch_retry_wait = (
            fetch_retry_wait if fetch_retry_wait is not None else wait_exponential(multiplier=2, min=2, max=30)
        )
        # H-06: Inicializar métricas
        self.metrics = SyncMetrics()
        # FASE 1.5: ID de agente para journal
        self._agent_id = "sync_engine"

    # ------------------------------------------------------------------
    # Lifecycle & Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Gracefully shuts down the engine, cancelling pending tasks."""
        logger.info("SyncEngine shutting down...")
        self._shutdown_event.set()

        if self._download_tasks:
            logger.info("Cancelling %d background download tasks...", len(self._download_tasks))
            for task in self._download_tasks:
                task.cancel()

            await asyncio.gather(*self._download_tasks, return_exceptions=True)
            self._download_tasks.clear()

        logger.info("SyncEngine shutdown complete.")

    # ------------------------------------------------------------------
    # FASE 1.5: Unit of Work Pattern para File Operations
    # ------------------------------------------------------------------

    async def execute_file_operation(
        self,
        operation_type: OperationType,
        target_path: pathlib.Path,
        operation: Coroutine[Any, Any, Any],
        description: str = "",
    ) -> Any:
        """
        Ejecuta una operación de archivo con patrón Unit of Work.

        Flujo:
        1. Capturar snapshot del archivo original (si existe)
        2. Registrar operación en journal con estado STARTED
        3. Ejecutar la operación
        4. Si éxito: marcar como COMPLETED
        5. Si fallo: marcar como FAILED y ejecutar rollback automático

        Args:
            operation_type: Tipo de operación
            target_path: Path al archivo afectado
            operation: Corutina que ejecuta la operación
            description: Descripción de la operación

        Returns:
            Resultado de la operación
        """
        if self._rollback_manager is None:
            # Sin rollback manager, ejecutar sin transaccional
            return await operation

        transaction_id: int | None = None
        entry_id: int | None = None

        try:
            # 1. Iniciar transacción
            transaction_id = await self._rollback_manager._journal.begin_transaction(
                description=description or f"file_operation: {operation_type.value}",
                agent_id=self._agent_id,
            )

            # 2. Capturar snapshot si el archivo existe
            snapshot_info = None
            if target_path.exists() and target_path.is_file():
                snapshot_info = await self._rollback_manager._snapshots.create_snapshot(
                    target_path,
                )

            # 3. Registrar inicio de operación
            entry_id = await self._rollback_manager._journal.begin_operation(
                agent_id=self._agent_id,
                operation_type=operation_type,
                target_path=str(target_path),
                transaction_id=transaction_id,
                snapshot_path=str(snapshot_info.snapshot_path) if snapshot_info else None,
            )

            # 4. Ejecutar la operación
            try:
                result = await operation

                # 5. Marcar como completada
                await self._rollback_manager._journal.complete_operation(entry_id)
                await self._rollback_manager._journal.commit_transaction(transaction_id)

                return result

            except Exception as exc:
                # 6. Error: marcar como fallida y ejecutar rollback
                logger.error(
                    "Operación falló, ejecutando rollback automático: %s",
                    str(exc),
                    exc_info=True,
                )
                await self._rollback_manager._journal.fail_operation(entry_id, error=str(exc))

                # Ejecutar rollback
                rollback_result = await self._rollback_manager.undo_last_operation(self._agent_id)
                rollback_result.success = False

                raise

        finally:
            # FASE 1.5: Pruning pasivo post-ejecución
            await self._passive_pruning()

    async def _passive_pruning(self) -> None:
        """FASE 1.5: Ejecuta pruning pasivo del directorio de backups.

        Verifica el tamaño del directorio y elimina registros antiguos si excede el límite.
        """
        if self._rollback_manager is None:
            return

        try:
            stats = await self._rollback_manager._snapshots.get_stats()
            max_size = self._get_max_backup_size_bytes()

            if stats.total_size_bytes > max_size:
                logger.warning(
                    "Límite de backups excedido: %d MB > %d MB. Iniciando pruning...",
                    stats.total_size_bytes // (1024 * 1024),
                    max_size // (1024 * 1024),
                )
                # Limpiar snapshots antiguos
                result = await self._rollback_manager._snapshots.cleanup_old_snapshots(
                    days_old=self._cfg.max_pruning_age_days
                )

                logger.info(
                    "Pruning completado: %d snapshots eliminados, %d MB liberados",
                    result.deleted_count,
                    result.freed_bytes // (1024 * 1024),
                )
        except Exception as e:
            logger.error("Error durante passive pruning: %s", e)

    async def _bounded_gather(
        self,
        coroutines: list[Coroutine[Any, Any, Any]],
        *,
        max_concurrency: int,
    ) -> list[Any]:
        """Ejecuta corutinas en lotes limitando solo la cantidad de tasks vivas."""
        if not coroutines:
            return []

        results: list[Any] = []
        batch_size = max(max_concurrency * 2, 1)

        for start in range(0, len(coroutines), batch_size):
            batch = coroutines[start : start + batch_size]
            tasks = [asyncio.create_task(coro) for coro in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            results.extend(batch_results)

        return results

    def _get_max_backup_size_bytes(self) -> int:
        """Obtiene el tamaño máximo de backups desde configuración."""
        if self._cfg is None:
            return 1024 * 1024 * 1024  # 1GB default
        return self._cfg.rollback_max_size_mb * 1024 * 1024

    # ------------------------------------------------------------------
    # Automated Update Cycle
    # ------------------------------------------------------------------

    async def check_for_updates(self, session: aiohttp.ClientSession) -> UpdatePayload:
        """Automated update cycle for all tracked mods.

        Uses controlled concurrency via Semaphore to query Nexus API.
        Downloads updates using the robust NexusDownloader.
        Returns a structured payload safe for Telegram notifications.
        """
        all_mods = await self._registry.search_mods("")
        tracked_mods = [m for m in all_mods if m.get("installed")]
        payload = UpdatePayload(total_checked=len(tracked_mods))
        if not tracked_mods:
            logger.info("No tracked mods found for updates.")
            return payload

        semaphore = asyncio.Semaphore(self._cfg.api_semaphore_limit)

        # Generar las tareas asincrónicas
        tasks = [self._check_and_update_mod(mod, session, semaphore) for mod in tracked_mods]

        logger.info(
            "Iniciando verificación de actualizaciones para %d mods...",
            payload.total_checked,
        )

        # Ejecución paralela con contención de fallas y materialización acotada
        results = await self._bounded_gather(
            tasks,
            max_concurrency=self._cfg.api_semaphore_limit,
        )
        for mod, result in zip(tracked_mods, results, strict=False):
            if isinstance(result, Exception):
                logger.error(
                    "Error aislando tarea de actualización para %r: %s",
                    mod["name"],
                    result,
                )
                payload.failed_mods.append(
                    {
                        "name": mod["name"],
                        "nexus_id": mod["nexus_id"],
                        "error": str(result),
                    }
                )
            else:
                status = result.get("status")
                if status == "updated":
                    payload.updated_mods.append(result)
                elif status == "up_to_date":
                    payload.up_to_date_mods.append(result["name"])
                elif status == "error":
                    payload.failed_mods.append(result)

        logger.info(
            "Ciclo de actualización completado: %d actualizados, %d al día, %d fallidos.",
            len(payload.updated_mods),
            len(payload.up_to_date_mods),
            len(payload.failed_mods),
        )
        return payload

    async def _check_and_update_mod(
        self,
        mod: dict[str, Any],
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        """Worker aislado para consultar y actualizar un mod individual."""
        nexus_id = mod["nexus_id"]
        local_version = mod["version"]
        mod_name = mod["name"]

        # 1. Fetch metadata con Semáforo y Backoff
        info = await self._safe_fetch_info(nexus_id, session, semaphore)
        if not info:
            return {
                "status": "error",
                "name": mod_name,
                "nexus_id": nexus_id,
                "error": "No metadata returned",
            }

        nexus_version = str(info.get("version", ""))
        # 2. Comparación de versiones
        if not nexus_version or nexus_version == local_version:
            return {"status": "up_to_date", "name": mod_name}

        if self._downloader is None:
            return {
                "status": "error",
                "name": mod_name,
                "nexus_id": nexus_id,
                "error": "Downloader not configured",
            }

        logger.info(
            "Actualización disponible para %s: %s -> %s",
            mod_name,
            local_version,
            nexus_version,
        )

        # 3. Descarga Robusta (Aplica backoff interno y validación MD5 en NexusDownloader)
        file_info = await self._downloader.get_file_info(nexus_id, None, session)

        if self._hitl:
            desc = f"Update for {mod_name} ({local_version} -> {nexus_version})"
            decision = await self._hitl.request_approval(
                request_id=f"update_{nexus_id}",
                reason="Automatic Mod Update",
                url=file_info.download_url,
                detail=desc,
            )
            if decision != Decision.APPROVED:
                logger.warning("Descarga abortada por HITL para %s", mod_name)
                return {
                    "status": "error",
                    "name": mod_name,
                    "nexus_id": nexus_id,
                    "error": "Descarga abortada por HITL",
                }

        download_path = await self._downloader.download(file_info, session)

        # 4. Actualización Atómica en Base de Datos
        await self._registry.upsert_mod(
            nexus_id=nexus_id,
            name=info.get("name", mod_name),
            version=nexus_version,
            author=str(info.get("author", "")),
            category=str(info.get("category_id", "")),
            download_url=file_info.download_url,
        )

        await self._registry.log_tasks_batch(
            [
                (
                    None,
                    "update_mod",
                    "success",
                    f"{mod_name}: {local_version} -> {nexus_version}",
                )
            ]
        )

        return {
            "status": "updated",
            "name": mod_name,
            "nexus_id": nexus_id,
            "old_version": local_version,
            "new_version": nexus_version,
            "file_path": str(download_path),
        }

    async def _check_and_update_mod_with_rollback(
        self,
        mod: dict[str, Any],
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        """FASE 1.5: Worker con soporte de rollback transaccional."""
        nexus_id = mod["nexus_id"]
        local_version = mod["version"]
        mod_name = mod["name"]
        transaction_id: int | None = None

        # Usar Unit of Work pattern para operaciones de archivos
        try:
            async with self._rollback_manager:
                # Iniciar transacción
                transaction_id = await self._rollback_manager._journal.begin_transaction(
                    description=f"update_{mod_name}",
                    mod_id=mod.get("nexus_id"),
                    agent_id=self._agent_id,
                )

                # Ejecutar actualización con soporte de rollback
                result = await self._check_and_update_mod_internal(mod, session, semaphore, transaction_id)

                # Marcar transacción como committed
                await self._rollback_manager._journal.commit_transaction(transaction_id)

                new_version = result.get("new_version", "unknown")
                return {
                    "status": "updated",
                    "name": mod_name,
                    "nexus_id": nexus_id,
                    "old_version": local_version,
                    "new_version": new_version,
                    "file_path": str(result.get("file_path")),
                    "rollback_performed": True,
                    "rollback_transaction_id": transaction_id,
                }

        except Exception as exc:
            # Rollback en caso de error
            logger.error("Error during mod update, executing rollback: %s", exc)
            try:
                await self._rollback_manager.undo_last_operation(self._agent_id)
            except Exception as rollback_exc:
                logger.critical("Rollback also failed for mod %s: %s", mod_name, str(rollback_exc))
                return {
                    "status": "error",
                    "name": mod_name,
                    "nexus_id": nexus_id,
                    "error": f"Update failed, rollback error: {rollback_exc!s}",
                    "rollback_performed": True,
                    "rollback_success": False,
                }
            return {
                "status": "error",
                "name": mod_name,
                "nexus_id": nexus_id,
                "error": str(exc),
                "rollback_performed": True,
                "rollback_success": True,
            }

    async def _check_and_update_mod_internal(
        self,
        mod: dict[str, Any],
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        transaction_id: int,
    ) -> dict[str, Any]:
        """Implementación interna con soporte de rollback."""
        nexus_id = mod["nexus_id"]
        local_version = mod["version"]
        mod_name = mod["name"]

        # 1. Fetch metadata con Semáforo y Backoff
        info = await self._safe_fetch_info(nexus_id, session, semaphore)
        if not info:
            return {
                "status": "error",
                "name": mod_name,
                "nexus_id": nexus_id,
                "error": "No metadata returned",
            }

        nexus_version = str(info.get("version", ""))
        # 2. Comparación de versiones
        if not nexus_version or nexus_version == local_version:
            return {"status": "up_to_date", "name": mod_name}

        if self._downloader is None:
            return {
                "status": "error",
                "name": mod_name,
                "nexus_id": nexus_id,
                "error": "Downloader not configured",
            }

        logger.info(
            "Actualización disponible para %s: %s -> %s",
            mod_name,
            local_version,
            nexus_version,
        )
        # 3. Descarga Robusta (Aplica backoff interno y validación MD5 en NexusDownloader)
        file_info = await self._downloader.get_file_info(nexus_id, None, session)
        if self._hitl:
            desc = f"Update for {mod_name} ({local_version} -> {nexus_version})"
            decision = await self._hitl.request_approval(
                request_id=f"update_{nexus_id}",
                reason="Automatic Mod Update",
                url=file_info.download_url,
                detail=desc,
            )
            if decision != Decision.APPROVED:
                logger.warning("Descarga abortada por HITL para %s", mod_name)
                return {
                    "status": "error",
                    "name": mod_name,
                    "nexus_id": nexus_id,
                    "error": "Descarga abortada por HITL",
                }

        download_path = await self._downloader.download(file_info, session)

        # 4. Actualización Atómica en Base de Datos
        await self._registry.upsert_mod(
            nexus_id=nexus_id,
            name=info.get("name", mod_name),
            version=nexus_version,
            author=str(info.get("author", "")),
            category=str(info.get("category_id", "")),
            download_url=file_info.download_url,
        )

        await self._registry.log_tasks_batch(
            [
                (
                    None,
                    "update_mod",
                    "success",
                    f"{mod_name}: {local_version} -> {nexus_version}",
                )
            ]
        )

        return {
            "status": "updated",
            "name": mod_name,
            "nexus_id": nexus_id,
            "old_version": local_version,
            "new_version": nexus_version,
            "file_path": str(download_path),
        }

    # ------------------------------------------------------------------
    # Legacy Update Cycle (without rollback)
    # ------------------------------------------------------------------

    async def _safe_fetch_info(
        self,
        nexus_id: int,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any] | None:
        """Envuelve la consulta a Nexus API con un semáforo de concurrencia y backoff."""
        result: dict[str, Any]
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((aiohttp.ClientError, MasterlistFetchError)),
            stop=stop_after_attempt(5),
            wait=self._fetch_retry_wait,
            reraise=True,
        ):
            with attempt:
                async with semaphore:
                    result = await self._masterlist.fetch_mod_info(nexus_id, session)
        return result

    # ------------------------------------------------------------------
    # Sync Local Load Order (Legacy Logic)
    # ------------------------------------------------------------------

    async def run(self, session: aiohttp.ClientSession, profile: str = "Default") -> SyncResult:
        queue: asyncio.Queue[list[tuple[str, bool]] | None] = asyncio.Queue(maxsize=self._cfg.queue_maxsize)
        semaphore = asyncio.Semaphore(self._cfg.api_semaphore_limit)
        result = SyncResult()

        producer = asyncio.create_task(self._produce(queue, profile), name="sync-producer")
        workers = [
            asyncio.create_task(
                self._consume(queue, session, semaphore, result),
                name=f"sync-worker-{i}",
            )
            for i in range(self._cfg.worker_count)
        ]

        try:
            await producer
        finally:
            for _ in workers:
                await queue.put(_POISON)
        # H-01: return_exceptions=True para prevenir crashes del orquestador
        await asyncio.gather(*workers, return_exceptions=True)

        logger.info(
            "Sync complete: processed=%d failed=%d skipped=%d",
            result.processed,
            result.failed,
            result.skipped,
        )
        return result

    def enqueue_download(self, coro: Coroutine[Any, Any, Any], context: str = "unknown") -> asyncio.Task[Any]:
        async def _download_wrapper() -> None:
            async with self._download_semaphore:
                try:
                    await coro
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # H-02: Logging estructurado para debugging
                    logger.error(
                        "worker_download_failed",
                        extra={
                            "context": context,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "timestamp": datetime.utcnow().isoformat(),
                        },
                    )
                    detail = f"{context} failed: {exc}"
                    try:
                        # H-06: Actualizar estado de tarea a "failed" en SQLite
                        await self._registry.log_tasks_batch([(None, "download_mod", "failed", detail)])
                    except Exception as comp_exc:
                        logger.error("Failed to log compensation task: %s", comp_exc)
                    finally:
                        # BUG-001 FIX: Usar método thread-safe para registrar errores
                        await self.metrics.increment_error_type(type(exc).__name__)

        task: asyncio.Task[Any] = asyncio.create_task(_download_wrapper())
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)
        return task

    async def _produce(self, queue: asyncio.Queue[list[tuple[str, bool]] | None], profile: str) -> None:
        """PRF-01: Producer with backpressure via bounded queue.

        ``await queue.put(batch)`` suspends the producer coroutine when the
        queue is full (maxsize reached), allowing workers to drain items before
        more are enqueued.  A timeout detects deadlock if all workers have died.
        """
        batch: list[tuple[str, bool]] = []
        async for mod_name, enabled in self._mo2.read_modlist(profile):
            batch.append((mod_name, enabled))
            if len(batch) >= self._cfg.batch_size:
                await asyncio.wait_for(
                    queue.put(batch),
                    timeout=self._cfg.queue_put_timeout,
                )
                batch = []
        if batch:
            await asyncio.wait_for(
                queue.put(batch),
                timeout=self._cfg.queue_put_timeout,
            )

    async def _consume(
        self,
        queue: asyncio.Queue[list[tuple[str, bool]] | None],
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        result: SyncResult,
    ) -> None:
        while True:
            batch = await queue.get()
            if batch is _POISON:
                queue.task_done()
                return
            try:
                await self._process_batch(batch, session, semaphore, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # H-02: Logging estructurado para debugging
                logger.error(
                    "batch_processing_failed",
                    extra={
                        "batch_size": len(batch),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                )
                result.failed += len(batch)
                # BUG-001 FIX: Usar método loop-safe para registrar errores
                await self.metrics.increment_error_type(type(exc).__name__)
            finally:
                queue.task_done()

    async def _process_batch(
        self,
        batch: list[tuple[str, bool]],
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        result: SyncResult,
    ) -> None:
        mod_rows: list[tuple[int, str, str, str, str, str]] = []
        log_rows: list[tuple[int | None, str, str, str]] = []

        for mod_name, enabled in batch:
            nexus_id = _extract_nexus_id(mod_name)
            if nexus_id is None:
                result.skipped += 1
                continue

            try:
                info = await self._safe_fetch_info(nexus_id, session, semaphore)
            except (
                MasterlistFetchError,
                CircuitOpenError,
                RetryError,
                aiohttp.ClientError,
            ) as exc:
                logger.warning("Skipping mod %r: %s", mod_name, exc)
                result.failed += 1
                result.errors.append(f"{mod_name}: {exc}")
                log_rows.append((None, "sync", "error", f"{mod_name}: {exc}"))
                continue

            if not info or "mod_id" not in info:
                result.skipped += 1
                continue

            mod_rows.append(
                (
                    int(info["mod_id"]),
                    str(info.get("name", mod_name)),
                    str(info.get("version", "")),
                    str(info.get("author", "")),
                    str(info.get("category_id", "")),
                    str(info.get("download_url", "")),
                    1,
                    int(enabled),
                )
            )
            log_rows.append((None, "sync", "ok", mod_name))
            result.processed += 1

        await self._registry.upsert_mods_batch(mod_rows)
        await self._registry.log_tasks_batch(log_rows)


def _extract_nexus_id(mod_name: str) -> int | None:
    parts = mod_name.split("-")
    for part in parts:
        stripped = part.strip()
        if stripped.isdigit() and len(stripped) >= 2:
            return int(stripped)

    # Use SystemPaths to resolve the mods directory dynamically
    meta_path = SystemPaths.modding_root() / "MO2/mods" / mod_name / "meta.ini"

    if meta_path.exists():
        try:
            config = configparser.ConfigParser()
            config.read(str(meta_path), encoding="utf-8")
            if "General" in config and "modid" in config["General"]:
                modid = config["General"]["modid"]
                if modid.isdigit() and modid != "0":
                    return int(modid)
        except Exception as exc:
            logger.debug("Failed to read meta.ini for %s: %s", mod_name, exc)

    return None
