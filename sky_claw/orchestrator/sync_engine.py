"""Sync Engine – Producer-Consumer orchestrator for mod synchronisation.

Reads ``modlist.txt`` via :class:`MO2Controller` (producer), fans out
mod-metadata fetches to a pool of async workers (consumers) through an
:class:`asyncio.Queue`, and persists results in micro-batches via
:class:`AsyncModRegistry`.

Includes a fully automated Update Cycle with controlled concurrency and
robust exception handling to prevent single-mod failures from crashing
entire batches.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Coroutine

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
from sky_claw.scraper.nexus_downloader import NexusDownloader
from sky_claw.security.hitl import HITLGuard, Decision

logger = logging.getLogger(__name__)

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


@dataclass
class UpdatePayload:
    """Payload generated after a full update cycle for Telegram reporting."""
    total_checked: int = 0
    updated_mods: list[dict[str, Any]] = field(default_factory=list)
    failed_mods: list[dict[str, Any]] = field(default_factory=list)
    up_to_date_mods: list[str] = field(default_factory=list)


class SyncEngine:
    """Orchestrator for mod synchronisation and automatic updates.

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
    """

    def __init__(
        self,
        mo2: MO2Controller,
        masterlist: MasterlistClient,
        registry: AsyncModRegistry,
        config: SyncConfig | None = None,
        downloader: NexusDownloader | None = None,
        hitl: HITLGuard | None = None,
    ) -> None:
        self._mo2 = mo2
        self._masterlist = masterlist
        self._registry = registry
        self._cfg = config or SyncConfig()
        self._downloader = downloader
        self._hitl = hitl
        self._download_tasks: set[asyncio.Task[Any]] = set()

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
        tasks = [
            self._check_and_update_mod(mod, session, semaphore)
            for mod in tracked_mods
        ]

        logger.info("Iniciando verificación de actualizaciones para %d mods...", payload.total_checked)
        
        # Ejecución paralela con contención de fallas
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for mod, result in zip(tracked_mods, results):
            if isinstance(result, Exception):
                logger.error("Error aislando tarea de actualización para %r: %s", mod["name"], result)
                payload.failed_mods.append({
                    "name": mod["name"],
                    "nexus_id": mod["nexus_id"],
                    "error": str(result)
                })
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
            len(payload.updated_mods), len(payload.up_to_date_mods), len(payload.failed_mods)
        )
        return payload

    async def _check_and_update_mod(
        self, 
        mod: dict[str, Any], 
        session: aiohttp.ClientSession, 
        semaphore: asyncio.Semaphore
    ) -> dict[str, Any]:
        """Worker aislado para consultar y actualizar un mod individual."""
        nexus_id = mod["nexus_id"]
        local_version = mod["version"]
        mod_name = mod["name"]

        # 1. Fetch metadata con Semáforo y Backoff
        info = await self._safe_fetch_info(nexus_id, session, semaphore)
        if not info:
            return {"status": "error", "name": mod_name, "nexus_id": nexus_id, "error": "No metadata returned"}
            
        nexus_version = str(info.get("version", ""))

        # 2. Comparación de versiones
        if not nexus_version or nexus_version == local_version:
            return {"status": "up_to_date", "name": mod_name}

        if self._downloader is None:
            return {"status": "error", "name": mod_name, "nexus_id": nexus_id, "error": "Downloader not configured"}

        logger.info("Actualización disponible para %s: %s -> %s", mod_name, local_version, nexus_version)

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
                    "error": "Descarga abortada por HITL"
                }

        download_path = await self._downloader.download(file_info, session)

        # 4. Actualización Atómica en Base de Datos
        await self._registry.upsert_mod(
            nexus_id=nexus_id,
            name=info.get("name", mod_name),
            version=nexus_version,
            author=str(info.get("author", "")),
            category=str(info.get("category_id", "")),
            download_url=file_info.download_url
        )
        
        await self._registry.log_tasks_batch([(
            None, "update_mod", "success", f"{mod_name}: {local_version} -> {nexus_version}"
        )])

        return {
            "status": "updated",
            "name": mod_name,
            "nexus_id": nexus_id,
            "old_version": local_version,
            "new_version": nexus_version,
            "file_path": str(download_path)
        }

    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, MasterlistFetchError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    async def _safe_fetch_info(self, nexus_id: int, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> dict[str, Any] | None:
        """Envuelve la consulta a Nexus API con un semáforo de concurrencia y backoff."""
        async with semaphore:
            return await self._masterlist.fetch_mod_info(nexus_id, session)

    # ------------------------------------------------------------------
    # Sync Local Load Order (Legacy Logic)
    # ------------------------------------------------------------------

    async def run(self, session: aiohttp.ClientSession, profile: str = "Default") -> SyncResult:
        queue: asyncio.Queue[list[tuple[str, bool]] | None] = asyncio.Queue(maxsize=self._cfg.queue_maxsize)
        semaphore = asyncio.Semaphore(self._cfg.api_semaphore_limit)
        result = SyncResult()

        producer = asyncio.create_task(self._produce(queue, profile), name="sync-producer")
        workers = [
            asyncio.create_task(self._consume(queue, session, semaphore, result), name=f"sync-worker-{i}")
            for i in range(self._cfg.worker_count)
        ]

        try:
            await producer
        finally:
            for _ in workers:
                await queue.put(_POISON)
        await asyncio.gather(*workers)

        logger.info("Sync complete: processed=%d failed=%d skipped=%d", result.processed, result.failed, result.skipped)
        return result

    def enqueue_download(self, coro: Coroutine[Any, Any, Any], context: str = "unknown") -> asyncio.Task[Any]:
        async def _download_wrapper() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Download task failed with exception: %s", exc, exc_info=exc)
                detail = f"{context} failed: {exc}"
                try:
                    await self._registry.log_tasks_batch([(None, "download_mod", "failed", detail)])
                except Exception as comp_exc:
                    logger.error("Failed to log compensation task: %s", comp_exc)
                    
        task: asyncio.Task[Any] = asyncio.create_task(_download_wrapper())
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)
        return task

    async def _produce(self, queue: asyncio.Queue[list[tuple[str, bool]] | None], profile: str) -> None:
        batch: list[tuple[str, bool]] = []
        async for mod_name, enabled in self._mo2.read_modlist(profile):
            batch.append((mod_name, enabled))
            if len(batch) >= self._cfg.batch_size:
                await queue.put(batch)
                batch = []
        if batch:
            await queue.put(batch)

    async def _consume(self, queue: asyncio.Queue[list[tuple[str, bool]] | None], session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, result: SyncResult) -> None:
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

    async def _process_batch(self, batch: list[tuple[str, bool]], session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, result: SyncResult) -> None:
        mod_rows: list[tuple[int, str, str, str, str, str]] = []
        log_rows: list[tuple[int | None, str, str, str]] = []

        for mod_name, enabled in batch:
            nexus_id = _extract_nexus_id(mod_name)
            if nexus_id is None:
                result.skipped += 1
                continue

            try:
                info = await self._safe_fetch_info(nexus_id, session, semaphore)
            except (MasterlistFetchError, CircuitOpenError, RetryError, aiohttp.ClientError) as exc:
                logger.warning("Skipping mod %r: %s", mod_name, exc)
                result.failed += 1
                result.errors.append(f"{mod_name}: {exc}")
                log_rows.append((None, "sync", "error", f"{mod_name}: {exc}"))
                continue

            if not info or "mod_id" not in info:
                result.skipped += 1
                continue

            mod_rows.append((
                int(info["mod_id"]),
                str(info.get("name", mod_name)),
                str(info.get("version", "")),
                str(info.get("author", "")),
                str(info.get("category_id", "")),
                str(info.get("download_url", "")),
                1,
                int(enabled),
            ))
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
            
    meta_path = os.path.join(r"C:\Modding\MO2\mods", mod_name, "meta.ini")
    if os.path.exists(meta_path):
        try:
            config = configparser.ConfigParser()
            config.read(meta_path, encoding='utf-8')
            if 'General' in config and 'modid' in config['General']:
                modid = config['General']['modid']
                if modid.isdigit() and modid != "0":
                    return int(modid)
        except Exception as exc:
            logger.debug("Failed to read meta.ini for %s: %s", mod_name, exc)
            
    return None
