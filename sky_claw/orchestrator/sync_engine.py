"""Sync Engine – Producer-Consumer orchestrator for mod synchronisation.

Reads ``modlist.txt`` via :class:`MO2Controller` (producer), fans out
mod-metadata fetches to a pool of async workers (consumers) through an
:class:`asyncio.Queue`, and persists results in micro-batches via
:class:`AsyncModRegistry`.

Fault-tolerance
~~~~~~~~~~~~~~~
* **Partial network failures** are handled with exponential-backoff
  retries (``tenacity``).
* **Corrupt / unparseable mods** are logged and skipped – they never
  block the rest of the batch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Coroutine, Sequence

import aiohttp
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.scraper.masterlist import (
    CircuitOpenError,
    MasterlistClient,
    MasterlistFetchError,
)
from sky_claw.mo2.vfs import MO2Controller

logger = logging.getLogger(__name__)

# Sentinel used to signal workers to shut down.
_POISON = None


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Tunables for the sync engine."""

    worker_count: int = 4
    batch_size: int = 20
    max_retries: int = 5
    api_semaphore_limit: int = 4
    queue_maxsize: int = 200


@dataclass
class SyncResult:
    """Aggregated outcome of a sync run."""

    processed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class SyncEngine:
    """Producer-Consumer orchestrator for mod synchronisation.

    Parameters
    ----------
    mo2:
        Controller for the MO2 portable instance (reads ``modlist.txt``).
    masterlist:
        Async client for Nexus Mods API metadata.
    registry:
        Async database layer for micro-batched persistence.
    config:
        Engine tunables (worker count, batch size, retry policy …).
    """

    def __init__(
        self,
        mo2: MO2Controller,
        masterlist: MasterlistClient,
        registry: AsyncModRegistry,
        config: SyncConfig | None = None,
    ) -> None:
        self._mo2 = mo2
        self._masterlist = masterlist
        self._registry = registry
        self._cfg = config or SyncConfig()
        # Tracks fire-and-forget download tasks so they are not GC'd early.
        self._download_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    async def run(
        self,
        session: aiohttp.ClientSession,
        profile: str = "Default",
    ) -> SyncResult:
        """Execute a full sync cycle.

        1. The **producer** reads ``modlist.txt`` and pushes mod names
           into the queue in batches.
        2. **Workers** (consumers) dequeue mod names, fetch metadata
           from Nexus (with retry + semaphore throttling), and collect
           DB rows.
        3. Collected rows are flushed to the database in micro-batches.
        """
        queue: asyncio.Queue[list[tuple[str, bool]] | None] = asyncio.Queue(
            maxsize=self._cfg.queue_maxsize,
        )
        semaphore = asyncio.Semaphore(self._cfg.api_semaphore_limit)
        result = SyncResult()

        producer = asyncio.create_task(
            self._produce(queue, profile),
            name="sync-producer",
        )
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
            # Send poison pills so every worker terminates.
            for _ in workers:
                await queue.put(_POISON)
        await asyncio.gather(*workers)

        logger.info(
            "Sync complete: processed=%d  failed=%d  skipped=%d",
            result.processed,
            result.failed,
            result.skipped,
        )
        return result

    def enqueue_download(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Schedule an arbitrary download coroutine as an :class:`asyncio.Task`.

        The task is stored internally to prevent it from being garbage-collected
        before completion.  Callers should check the returned task for
        exceptions when convenient.

        Args:
            coro: A coroutine that performs a single file download.

        Returns:
            The created :class:`asyncio.Task`.
        """
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        self._download_tasks.add(task)
        
        def _on_done(t: asyncio.Task[Any]) -> None:
            self._download_tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error("Download task %s failed with exception: %s", t.get_name(), exc, exc_info=exc)

        task.add_done_callback(_on_done)
        logger.info("Enqueued download task %s", task.get_name())
        return task

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------

    async def _produce(
        self,
        queue: asyncio.Queue[list[tuple[str, bool]] | None],
        profile: str,
    ) -> None:
        """Read ``modlist.txt`` and push batches into *queue*."""
        batch: list[tuple[str, bool]] = []
        async for mod_name, enabled in self._mo2.read_modlist(profile):
            batch.append((mod_name, enabled))
            if len(batch) >= self._cfg.batch_size:
                await queue.put(batch)
                batch = []
        if batch:
            await queue.put(batch)

    # ------------------------------------------------------------------
    # Consumer (worker)
    # ------------------------------------------------------------------

    async def _consume(
        self,
        queue: asyncio.Queue[list[tuple[str, bool]] | None],
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        result: SyncResult,
    ) -> None:
        """Dequeue batches, fetch metadata, and persist to DB."""
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
                logger.exception("Unexpected error processing batch: %s", exc)
                result.failed += len(batch)
            finally:
                queue.task_done()

    async def _process_batch(
        self,
        batch: list[tuple[str, bool]],
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        result: SyncResult,
    ) -> None:
        """Fetch metadata for each mod in *batch* and flush to DB."""
        mod_rows: list[tuple[int, str, str, str, str, str]] = []
        log_rows: list[tuple[int | None, str, str, str]] = []

        for mod_name, enabled in batch:
            try:
                info = await self._fetch_with_retry(mod_name, session, semaphore)
            except (MasterlistFetchError, CircuitOpenError, RetryError, aiohttp.ClientError) as exc:
                logger.warning("Skipping mod %r: %s", mod_name, exc)
                result.failed += 1
                result.errors.append(f"{mod_name}: {exc}")
                log_rows.append((None, "sync", "error", f"{mod_name}: {exc}"))
                continue

            if info is None:
                result.skipped += 1
                continue

            if "mod_id" not in info:
                logger.warning("Skipping mod %r: missing mod_id in response", mod_name)
                result.skipped += 1
                continue

            nexus_id = int(info["mod_id"])

            mod_rows.append((
                nexus_id,
                str(info.get("name", mod_name)),
                str(info.get("version", "")),
                str(info.get("author", "")),
                str(info.get("category_id", "")),
                str(info.get("download_url", "")),
            ))
            log_rows.append((None, "sync", "ok", mod_name))
            result.processed += 1

        # Micro-batch flush
        await self._registry.upsert_mods_batch(mod_rows)
        await self._registry.log_tasks_batch(log_rows)

    # ------------------------------------------------------------------
    # Retry-wrapped fetch
    # ------------------------------------------------------------------

    async def _fetch_with_retry(
        self,
        mod_name: str,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any] | None:
        """Fetch mod info with exponential-backoff retries.

        The semaphore limits concurrent Nexus API calls to avoid
        rate-limiting.
        """

        @retry(
            retry=retry_if_exception_type(
                (aiohttp.ClientError, MasterlistFetchError),
            ),
            stop=stop_after_attempt(self._cfg.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            reraise=True,
        )
        async def _inner() -> dict[str, Any] | None:
            async with semaphore:
                nexus_id = _extract_nexus_id(mod_name)
                if nexus_id is None:
                    return None
                return await self._masterlist.fetch_mod_info(nexus_id, session)

        return await _inner()


def _extract_nexus_id(mod_name: str) -> int | None:
    """Best-effort extraction of a Nexus Mods numeric ID from *mod_name*.

    MO2 mod folder names often follow patterns like
    ``ModName-1234-v1-0`` where 1234 is the Nexus ID.  Returns ``None``
    when no plausible ID can be extracted.
    """
    parts = mod_name.split("-")
    for part in parts:
        stripped = part.strip()
        if stripped.isdigit() and len(stripped) >= 2:
            return int(stripped)
    return None
