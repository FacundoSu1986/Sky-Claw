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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# FASE 1.5: Imports de componentes de rollback
from sky_claw.antigravity.scraper.masterlist import (
    CircuitOpenError,
    MasterlistClient,
    MasterlistFetchError,
)
from sky_claw.antigravity.security.hitl import Decision, HITLGuard
from sky_claw.config import SystemPaths

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from sky_claw.antigravity.db.async_registry import AsyncModRegistry
    from sky_claw.antigravity.db.journal import OperationType
    from sky_claw.antigravity.db.rollback_manager import RollbackManager
    from sky_claw.antigravity.scraper.nexus_downloader import NexusDownloader
    from sky_claw.local.mo2.vfs import MO2Controller

logger = logging.getLogger(__name__)

_POISON = None

# FASE 1.5: Constante para directorio de staging de backups
BACKUP_STAGING_DIR = pathlib.Path(".skyclaw_backups/")


class SyncConfig(BaseModel):
    """Tunables for the sync engine.

    Serialize for transport with ``.model_dump(mode="json")``.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    worker_count: int = 4
    batch_size: int = 20
    max_retries: int = 5
    api_semaphore_limit: int = 4
    queue_maxsize: int = 50
    queue_put_timeout: float = 120.0
    enable_rollback: bool = True
    rollback_max_size_mb: int = 1024
    max_pruning_age_days: int = 30


class SyncResult(BaseModel):
    """Aggregated outcome of a sync run.

    Serialize for transport with ``.model_dump(mode="json")``.
    """

    model_config = ConfigDict(strict=True)

    processed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    rollback_performed: bool = False
    rollback_success: bool = False
    rollback_transaction_id: int | None = None


class UpdatePayload(BaseModel):
    """Payload generated after a full update cycle for Telegram reporting.

    Serialize for transport with ``.model_dump(mode="json")``.
    """

    model_config = ConfigDict(strict=True)

    total_checked: int = 0
    updated_mods: list[dict[str, Any]] = Field(default_factory=list)
    failed_mods: list[dict[str, Any]] = Field(default_factory=list)
    up_to_date_mods: list[str] = Field(default_factory=list)
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
        fetch_retry_wait: Any = None,
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

        rm = self._rollback_manager
        entry_id: int | None = None

        try:
            # 1. Iniciar transacción
            transaction_id = await rm.begin_transaction(
                description=description or f"file_operation: {operation_type.value}",
                agent_id=self._agent_id,
            )

            # 2. Capturar snapshot si el archivo existe
            snapshot_info = None
            if target_path.exists() and target_path.is_file():
                snapshot_info = await rm.create_snapshot(target_path)

            # 3. Registrar inicio de operación
            entry_id = await rm.begin_operation(
                agent_id=self._agent_id,
                operation_type=operation_type,
                target_path=str(target_path),
                transaction_id=transaction_id,
                snapshot_path=snapshot_info.snapshot_path if snapshot_info else None,
            )

            # 4. Ejecutar la operación
            try:
                result = await operation

                # 5. Marcar como completada
                await rm.complete_operation(entry_id)
                await rm.commit_transaction(transaction_id)

                return result

            except Exception as exc:
                # 6. Error: marcar como fallida y ejecutar rollback
                logger.error(
                    "Operación falló, ejecutando rollback automático: %s",
                    str(exc),
                    exc_info=True,
                )
                await rm.fail_operation(entry_id, error=str(exc))

                # Ejecutar rollback — result already reflects actual outcome
                rollback_result = await rm.undo_last_operation(self._agent_id)
                logger.warning(
                    "Rollback automático completado: success=%s, transaction=%s",
                    rollback_result.success,
                    rollback_result.transaction_id,
                )

                raise

        finally:
            # Bound snapshot storage growth: prune on every file operation,
            # success or failure.  Logs and swallows its own errors so it never
            # masks the real outcome of the operation above.
            await self._passive_pruning()

    async def _passive_pruning(self) -> None:
        """FASE 1.5: Ejecuta pruning pasivo del directorio de backups.

        Verifica el tamaño del directorio y elimina registros antiguos si excede el límite.
        """
        if self._rollback_manager is None:
            return

        rm = self._rollback_manager
        try:
            stats = await rm.get_snapshot_stats()
            max_size = self._get_max_backup_size_bytes()

            if stats.total_size_bytes > max_size:
                logger.warning(
                    "Límite de backups excedido: %d MB > %d MB. Iniciando pruning...",
                    stats.total_size_bytes // (1024 * 1024),
                    max_size // (1024 * 1024),
                )
                result = await rm.cleanup_old_snapshots(days_old=self._cfg.max_pruning_age_days)

                logger.info(
                    "Pruning completado: %d snapshots eliminados, %d MB liberados",
                    result.deleted_count,
                    result.freed_bytes // (1024 * 1024),
                )
        except (OSError, PermissionError) as e:
            logger.error("Error durante passive pruning: %s", e)

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

        Uses ``asyncio.TaskGroup`` with a concurrency limiter Semaphore(15) to
        bound simultaneous check-and-update lifecycles.  Exceptions are captured
        per-task so a single mod failure never aborts the entire cycle.
        Returns a structured payload safe for Telegram notifications.
        """
        all_mods = await self._registry.search_mods("")
        tracked_mods = [m for m in all_mods if m.get("installed")]
        payload = UpdatePayload(total_checked=len(tracked_mods))
        if not tracked_mods:
            logger.info("No tracked mods found for updates.")
            return payload

        # Outer concurrency limiter — never more than 15 simultaneous lifecycles
        concurrency_limit = asyncio.Semaphore(15)
        api_semaphore = asyncio.Semaphore(self._cfg.api_semaphore_limit)

        logger.info(
            "Iniciando verificación de actualizaciones para %d mods...",
            payload.total_checked,
        )

        # Pre-allocate results list to preserve order with indexed assignment
        results: list[Any] = [None] * len(tracked_mods)

        async def _wrapped_worker(idx: int, mod: dict[str, Any]) -> None:
            """Acquires the concurrency semaphore then runs the full lifecycle.

            Only catches expected per-mod failures (network, circuit-open, retry
            exhaustion, bad data).  Unexpected exceptions (bugs, MemoryError,
            asyncio.CancelledError) propagate so TaskGroup can cancel peers and
            surface the real problem.
            """
            async with concurrency_limit:
                try:
                    results[idx] = await self._check_and_update_mod(mod, session, api_semaphore)
                except (
                    MasterlistFetchError,
                    CircuitOpenError,
                    RetryError,
                    aiohttp.ClientError,
                    ValueError,
                    OSError,
                ) as exc:
                    # Expected per-mod failures — isolate so other mods continue
                    results[idx] = exc

        async with asyncio.TaskGroup() as tg:
            for idx, mod in enumerate(tracked_mods):
                tg.create_task(_wrapped_worker(idx, mod))

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
            elif isinstance(result, dict):
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
        """Worker for checking and optionally updating a single mod.

        If a ``rollback_manager`` is configured, the update is wrapped in a
        journal transaction.  Expected network failures return an error dict
        (graceful degradation); unexpected exceptions re-raise after rollback
        so the TaskGroup can perform cooperative cancellation.
        """
        nexus_id = mod["nexus_id"]
        local_version = mod["version"]
        mod_name = mod["name"]
        rm = self._rollback_manager
        transaction_id: int | None = None

        try:
            if rm is not None:
                transaction_id = await rm.begin_transaction(
                    description=f"update_{mod_name}",
                    mod_id=mod.get("nexus_id"),
                    agent_id=self._agent_id,
                )

            # 1. Fetch metadata with Semaphore + exponential backoff
            info = await self._safe_fetch_info(nexus_id, session, semaphore)
            if not info:
                if rm is not None and transaction_id is not None:
                    await rm.commit_transaction(transaction_id)  # no-op: no file ops recorded
                return {
                    "status": "error",
                    "name": mod_name,
                    "nexus_id": nexus_id,
                    "error": "No metadata returned",
                }

            nexus_version = str(info.get("version", ""))
            # 2. Version comparison
            if not nexus_version or nexus_version == local_version:
                if rm is not None and transaction_id is not None:
                    await rm.commit_transaction(transaction_id)
                return {"status": "up_to_date", "name": mod_name}

            if self._downloader is None:
                if rm is not None and transaction_id is not None:
                    await rm.commit_transaction(transaction_id)  # no-op: no file ops recorded
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

            # 3. Robust download (backoff + MD5 validation inside NexusDownloader)
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
                    if rm is not None and transaction_id is not None:
                        await rm.mark_transaction_rolled_back(transaction_id)  # user denied
                    return {
                        "status": "error",
                        "name": mod_name,
                        "nexus_id": nexus_id,
                        "error": "Descarga abortada por HITL",
                    }

            download_path = await self._downloader.download(file_info, session)

            # 4. Atomic DB update
            await self._registry.upsert_mod(
                nexus_id=nexus_id,
                name=info.get("name", mod_name),
                version=nexus_version,
                author=str(info.get("author", "")),
                category=str(info.get("category_id", "")),
                download_url=file_info.download_url,
            )
            await self._registry.log_tasks_batch(
                [(None, "update_mod", "success", f"{mod_name}: {local_version} -> {nexus_version}")]
            )

            if rm is not None and transaction_id is not None:
                await rm.commit_transaction(transaction_id)

            result: dict[str, Any] = {
                "status": "updated",
                "name": mod_name,
                "nexus_id": nexus_id,
                "old_version": local_version,
                "new_version": nexus_version,
                "file_path": str(download_path),
            }
            if rm is not None:
                result["rollback_performed"] = True
                result["rollback_transaction_id"] = transaction_id
            return result

        except (MasterlistFetchError, CircuitOpenError, RetryError, aiohttp.ClientError) as exc:
            # Expected network failure — graceful degradation, no re-raise.
            # No file operations were recorded under this transaction, so
            # mark_transaction_rolled_back (journal-only) is correct here.
            # Calling undo_last_operation would incorrectly undo an *unrelated*
            # previous operation for this agent.
            if rm is not None and transaction_id is not None:
                logger.warning("Network error updating %s; marking transaction rolled back: %s", mod_name, exc)
                try:
                    await rm.mark_transaction_rolled_back(transaction_id)
                except Exception as rb_exc:
                    logger.critical("mark_transaction_rolled_back failed for %s: %s", mod_name, rb_exc)
            return {
                "status": "error",
                "name": mod_name,
                "nexus_id": nexus_id,
                "error": str(exc),
            }

        except Exception as exc:
            # Unexpected bug — mark transaction rolled back, then re-raise (fail-fast).
            # Same reasoning: no file ops recorded, so journal-only cleanup suffices.
            if rm is not None and transaction_id is not None:
                logger.error("Unexpected error updating %s; marking transaction rolled back: %s", mod_name, exc)
                try:
                    await rm.mark_transaction_rolled_back(transaction_id)
                except Exception as rb_exc:
                    logger.critical("mark_transaction_rolled_back also failed for %s: %s", mod_name, rb_exc)
            raise

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
        """Sync local load order via producer-consumer pipeline.

        Uses ``asyncio.TaskGroup`` so an unexpected worker crash cancels the
        producer and all peers cooperatively (fail-fast), while the narrowed
        ``except`` clause in ``_consume`` still absorbs expected network errors.
        POISON pills are guaranteed via ``_produce_then_poison``'s ``finally``
        block even if the producer itself fails mid-stream.
        """
        queue: asyncio.Queue[list[tuple[str, bool]] | None] = asyncio.Queue(maxsize=self._cfg.queue_maxsize)
        semaphore = asyncio.Semaphore(self._cfg.api_semaphore_limit)
        result = SyncResult()

        async def _produce_then_poison() -> None:
            """Produce batches then send POISON pills — even on producer failure."""
            try:
                await self._produce(queue, profile)
            finally:
                for _ in range(self._cfg.worker_count):
                    await queue.put(_POISON)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_produce_then_poison(), name="sync-producer")
            for i in range(self._cfg.worker_count):
                tg.create_task(
                    self._consume(queue, session, semaphore, result),
                    name=f"sync-worker-{i}",
                )

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
                            "timestamp": datetime.now(tz=UTC).isoformat(),
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
            except (MasterlistFetchError, CircuitOpenError, RetryError, aiohttp.ClientError) as exc:
                # H-02: Logging estructurado para debugging
                logger.error(
                    "batch_processing_failed",
                    extra={
                        "batch_size": len(batch),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "timestamp": datetime.now(tz=UTC).isoformat(),
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
        mod_rows: list[tuple[int, str, str, str, str, str, bool, bool]] = []
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
                    True,
                    bool(enabled),
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
        except (OSError, PermissionError, configparser.Error) as exc:
            logger.debug("Failed to read meta.ini for %s: %s", mod_name, exc)

    return None
