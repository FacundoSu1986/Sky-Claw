"""LOOT masterlist downloader with local cache.

Downloads the LOOT masterlist YAML from GitHub and caches it
locally with a configurable TTL (default: 24 hours).
All network traffic goes through :class:`NetworkGateway`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

    from sky_claw.antigravity.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

MASTERLIST_URL = "https://raw.githubusercontent.com/loot/skyrimse/master/masterlist.yaml"
DEFAULT_CACHE_DIR = pathlib.Path("sky_claw_data")
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 hours


class MasterlistDownloader:
    """Downloads and caches the LOOT masterlist YAML.

    Args:
        gateway: Network gateway for egress control.
        cache_dir: Directory for the cached file.
        ttl: Cache time-to-live in seconds.
    """

    def __init__(
        self,
        gateway: NetworkGateway,
        cache_dir: pathlib.Path = DEFAULT_CACHE_DIR,
        ttl: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._gateway = gateway
        self._cache_path = cache_dir / "masterlist.yaml"
        self._ttl = ttl
        self._lock = asyncio.Lock()

    @property
    def cache_path(self) -> pathlib.Path:
        return self._cache_path

    async def get(self, session: aiohttp.ClientSession) -> pathlib.Path:
        """Return path to the cached masterlist, downloading if stale.

        Uses double-check locking to prevent concurrent downloads
        from wasting bandwidth or corrupting the cache file.

        Args:
            session: aiohttp session for HTTP requests.

        Returns:
            Path to the local masterlist.yaml file.

        Raises:
            RuntimeError: If download fails.
        """
        # First check — fast path without lock.
        if self._is_cache_valid():
            logger.debug("Using cached masterlist at %s", self._cache_path)
            return self._cache_path

        # Acquire lock and re-check (another coroutine may have downloaded).
        async with self._lock:
            if self._is_cache_valid():
                logger.debug("Using cached masterlist at %s (after lock)", self._cache_path)
                return self._cache_path

            await self._download_atomic(session)
            return self._cache_path

    def _is_cache_valid(self) -> bool:
        """Check if the cached file exists and is within TTL."""
        if not self._cache_path.exists():
            return False
        age = time.time() - self._cache_path.stat().st_mtime
        return age < self._ttl

    async def _download_atomic(self, session: aiohttp.ClientSession) -> None:
        """Download the masterlist from GitHub via NetworkGateway.

        Uses atomic write (write to .tmp then os.replace) to avoid
        cache corruption if the process crashes mid-I/O.
        """
        logger.info("Downloading LOOT masterlist from %s", MASTERLIST_URL)

        resp = await self._gateway.request("GET", MASTERLIST_URL, session)
        async with resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Failed to download masterlist: HTTP {resp.status}: {body}")
            content = await resp.read()

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Golden Master: Atomic write — non-blocking via thread pool.
        tmp_path = self._cache_path.with_suffix(".tmp")

        def _write_and_replace() -> None:
            tmp_path.write_bytes(content)
            os.replace(tmp_path, self._cache_path)

        await asyncio.to_thread(_write_and_replace)

        logger.info(
            "Masterlist cached at %s (%d bytes)",
            self._cache_path,
            len(content),
        )
