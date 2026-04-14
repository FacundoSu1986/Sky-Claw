"""LOOT masterlist downloader with local cache.

Downloads the LOOT masterlist YAML from GitHub and caches it
locally with a configurable TTL (default: 24 hours).
All network traffic goes through :class:`NetworkGateway`.
"""

from __future__ import annotations

import logging
import pathlib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

    from sky_claw.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

MASTERLIST_URL = (
    "https://raw.githubusercontent.com/loot/skyrimse/master/masterlist.yaml"
)
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

    @property
    def cache_path(self) -> pathlib.Path:
        return self._cache_path

    async def get(self, session: aiohttp.ClientSession) -> pathlib.Path:
        """Return path to the cached masterlist, downloading if stale.

        Args:
            session: aiohttp session for HTTP requests.

        Returns:
            Path to the local masterlist.yaml file.

        Raises:
            RuntimeError: If download fails.
        """
        if self._is_cache_valid():
            logger.debug("Using cached masterlist at %s", self._cache_path)
            return self._cache_path

        await self._download(session)
        return self._cache_path

    def _is_cache_valid(self) -> bool:
        """Check if the cached file exists and is within TTL."""
        if not self._cache_path.exists():
            return False
        age = time.time() - self._cache_path.stat().st_mtime
        return age < self._ttl

    async def _download(self, session: aiohttp.ClientSession) -> None:
        """Download the masterlist from GitHub via NetworkGateway."""
        logger.info("Downloading LOOT masterlist from %s", MASTERLIST_URL)

        resp = await self._gateway.request("GET", MASTERLIST_URL, session)
        async with resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"Failed to download masterlist: HTTP {resp.status}: {body}"
                )
            content = await resp.read()

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_bytes(content)
        logger.info(
            "Masterlist cached at %s (%d bytes)",
            self._cache_path,
            len(content),
        )
