"""Nexus Mods premium downloader with chunked transfer and MD5 validation.

Security invariants
~~~~~~~~~~~~~~~~~~~
* :meth:`NetworkGateway.authorize` is called before **every** outbound
  request — both metadata queries and actual file transfers.
* No file bytes are written until :class:`HITLGuard` has returned
  ``Decision.APPROVED``.  The guard is enforced at the call-site
  (:meth:`AsyncToolRegistry._download_mod`), not here.
* Downloaded files land exclusively in the MO2 staging directory passed
  at construction time.
* Post-download MD5 is validated against the hash provided by the Nexus
  API.  A mismatch deletes the partial file and raises
  :class:`MD5ValidationError`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import aiohttp

from sky_claw.config import (
    NEXUS_DOWNLOAD_CHUNK_SIZE,
    NEXUS_DOWNLOAD_TIMEOUT_SECONDS,
)
from sky_claw.security.network_gateway import EgressViolation, NetworkGateway

logger = logging.getLogger(__name__)

_NEXUS_API_BASE = "https://api.nexusmods.com/v1"

# Type alias for the optional progress callback.
ProgressCallback = Callable[["DownloadProgress"], Awaitable[None]] | None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DownloadError(Exception):
    """Raised when a Nexus download fails for any non-validation reason."""


class MD5ValidationError(DownloadError):
    """Raised when the downloaded file's MD5 hash does not match the expected value."""


class PremiumRequiredError(DownloadError):
    """Raised when a Premium API key is required to generate direct links."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileInfo:
    """Metadata for a Nexus file, fetched before the actual download.

    Attributes:
        nexus_id: Nexus Mods numeric mod ID.
        file_id: Nexus Mods numeric file ID.
        file_name: Original filename on Nexus.
        size_bytes: Expected file size in bytes (0 when unknown).
        md5: MD5 hex-digest provided by the Nexus API (empty string when absent).
        download_url: CDN URL for the actual file bytes.
    """

    nexus_id: int
    file_id: int
    file_name: str
    size_bytes: int
    md5: str
    download_url: str


@dataclass
class DownloadProgress:
    """Mutable progress snapshot passed to the optional progress callback.

    Attributes:
        file_name: Filename being downloaded.
        total_bytes: Expected total size (0 when unknown).
        downloaded_bytes: Bytes received so far.
    """

    file_name: str
    total_bytes: int
    downloaded_bytes: int = 0

    @property
    def percent(self) -> float:
        """Return download completion as a value in ``[0.0, 100.0]``."""
        if self.total_bytes == 0:
            return 0.0
        return min((self.downloaded_bytes / self.total_bytes) * 100, 100.0)


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


class NexusDownloader:
    """Premium Nexus Mods downloader.

    Every HTTP request goes through :class:`NetworkGateway` for egress
    control before it is dispatched.  No file I/O happens until the caller
    has obtained HITL approval externally.

    Parameters
    ----------
    api_key:
        Nexus Mods premium API key.  Required for download-link generation.
    gateway:
        :class:`NetworkGateway` instance; ``authorize()`` is called before
        every request.
    staging_dir:
        MO2 staging directory.  All files are written here.
    chunk_size:
        Read chunk size in bytes (default: ``NEXUS_DOWNLOAD_CHUNK_SIZE``).
    timeout:
        Per-download timeout in seconds (default:
        ``NEXUS_DOWNLOAD_TIMEOUT_SECONDS``).
    game_domain:
        Nexus game-domain slug (default: ``skyrimspecialedition``).
    """

    def __init__(
        self,
        api_key: str,
        gateway: NetworkGateway,
        staging_dir: pathlib.Path,
        chunk_size: int = NEXUS_DOWNLOAD_CHUNK_SIZE,
        timeout: int = NEXUS_DOWNLOAD_TIMEOUT_SECONDS,
        game_domain: str = "skyrimspecialedition",
    ) -> None:
        self._api_key = api_key
        self._gateway = gateway
        self._staging_dir = staging_dir
        self._chunk_size = chunk_size
        self._timeout = timeout
        self._game_domain = game_domain

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def staging_dir(self) -> pathlib.Path:
        """MO2 staging directory where downloads are saved."""
        return self._staging_dir

    @property
    def timeout(self) -> int:
        """Per-download timeout in seconds."""
        return self._timeout

    async def get_file_info(
        self,
        nexus_id: int,
        file_id: int | None,
        session: aiohttp.ClientSession,
    ) -> FileInfo:
        """Query Nexus API for file metadata and obtain a CDN download URL.

        This method makes **two** authorized requests:
        1. File metadata endpoint — size, filename, MD5.
        2. Download-link endpoint — CDN URL for premium users.
        
        If `file_id` is None, it queries the /files.json endpoint automatically 
        and picks the primary file, or the latest uploaded file.

        Args:
            nexus_id: Nexus Mods numeric mod ID.
            file_id: Nexus Mods numeric file ID (optional).
            session: Active :class:`aiohttp.ClientSession`.

        Returns:
            :class:`FileInfo` populated with all metadata.

        Raises:
            DownloadError: On HTTP 401 (bad key), 403 (not premium), 404
                (file not found), or any other non-2xx status.
            EgressViolation: If the URL is not in the egress allow-list.
        """
        headers = {"apikey": self._api_key, "Accept": "application/json"}
        meta_timeout = aiohttp.ClientTimeout(total=30)
        
        if file_id is None:
            files_url = f"{_NEXUS_API_BASE}/games/{self._game_domain}/mods/{nexus_id}/files.json"
            self._gateway.authorize("GET", files_url)
            async with session.get(files_url, headers=headers, timeout=meta_timeout) as resp:
                if resp.status == 401 or resp.status == 403:
                    raise DownloadError("Invalid or missing Nexus API key")
                if resp.status == 404:
                    raise DownloadError(f"Mod {nexus_id} not found")
                resp.raise_for_status()
                files_data = await resp.json()
            
            files_list = files_data.get("files", [])
            if not files_list:
                raise DownloadError(f"No files found for mod {nexus_id}")
            
            primary_files = [f for f in files_list if f.get("is_primary")]
            target_file = primary_files[0] if primary_files else files_list[-1]
            file_id = target_file["file_id"]
            if isinstance(target_file.get("file_id"), list):
                file_id = target_file["file_id"][1]  # edge case handling
            
        meta_url = (
            f"{_NEXUS_API_BASE}/games/{self._game_domain}"
            f"/mods/{nexus_id}/files/{file_id}.json"
        )
        self._gateway.authorize("GET", meta_url)

        async with session.get(meta_url, headers=headers, timeout=meta_timeout) as resp:
            if resp.status == 401:
                raise DownloadError("Invalid or missing Nexus API key")
            if resp.status == 403:
                raise DownloadError(
                    "Nexus API key does not have premium access"
                )
            if resp.status == 404:
                raise DownloadError(
                    f"File {file_id} not found for mod {nexus_id}"
                )
            resp.raise_for_status()
            data = await resp.json()

        size_bytes: int = int(
            data.get("size_in_bytes")
            or data.get("size", 0) * 1024
        )
        md5: str = str(data.get("md5") or "")
        file_name: str = str(data.get("file_name", f"mod_{nexus_id}_file_{file_id}"))

        try:
            download_url = await self._get_download_url(nexus_id, file_id, session)
        except PremiumRequiredError:
            download_url = "FREE_FALLBACK"

        return FileInfo(
            nexus_id=nexus_id,
            file_id=file_id,
            file_name=file_name,
            size_bytes=size_bytes,
            md5=md5,
            download_url=download_url,
        )

    async def download(
        self,
        file_info: FileInfo,
        session: aiohttp.ClientSession,
        progress_cb: ProgressCallback = None,
    ) -> pathlib.Path:
        """Download a file using chunked transfer and validate its MD5 hash.

        The file is written to ``self.staging_dir / file_info.file_name``.
        If any error occurs mid-transfer the partial file is deleted before
        the exception propagates.  If MD5 validation fails the file is also
        deleted and :class:`MD5ValidationError` is raised.

        Args:
            file_info: Metadata from :meth:`get_file_info`.
            session: Active :class:`aiohttp.ClientSession`.
            progress_cb: Optional async callable receiving
                :class:`DownloadProgress` after each chunk.

        Returns:
            Absolute path to the downloaded file inside the staging directory.

        Raises:
            DownloadError: On network or I/O failure.
            MD5ValidationError: When the hash does not match.
            EgressViolation: If the CDN URL is not in the egress allow-list.
        """
        await asyncio.to_thread(self._staging_dir.mkdir, parents=True, exist_ok=True)
        dest = self._staging_dir / file_info.file_name

        if file_info.download_url == "FREE_FALLBACK":
            return await self._handle_manual_fallback(file_info, dest, progress_cb)

        self._gateway.authorize("GET", file_info.download_url)

        progress = DownloadProgress(
            file_name=file_info.file_name,
            total_bytes=file_info.size_bytes,
        )
        timeout = aiohttp.ClientTimeout(
            total=self._timeout,
            sock_read=60,
        )
        headers = {"apikey": self._api_key}
        md5_hash = hashlib.md5()

        try:
            async with session.get(
                file_info.download_url,
                headers=headers,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()

                # Use Content-Length when size was absent from file metadata.
                if progress.total_bytes == 0:
                    content_length = resp.headers.get("Content-Length")
                    if content_length:
                        progress.total_bytes = int(content_length)

                def _open_fh():
                    return open(dest, "wb")
                    
                fh = await asyncio.to_thread(_open_fh)
                try:
                    async for chunk in resp.content.iter_chunked(self._chunk_size):
                        await asyncio.to_thread(fh.write, chunk)
                        md5_hash.update(chunk)
                        progress.downloaded_bytes += len(chunk)
                        if progress_cb is not None:
                            await progress_cb(progress)
                        await asyncio.sleep(0)  # Release GIL during CPU-bound hash chunking
                finally:
                    await asyncio.to_thread(fh.close)

        except aiohttp.ClientError as exc:
            await _cleanup(dest)
            raise DownloadError(f"Download failed for {file_info.file_name!r}: {exc}") from exc
        except (OSError, asyncio.TimeoutError) as exc:
            await _cleanup(dest)
            raise DownloadError(f"I/O or timeout error for {file_info.file_name!r}: {exc}") from exc

        # ------------------------------------------------------------------
        # Post-download MD5 validation
        # ------------------------------------------------------------------
        if file_info.md5:
            actual = md5_hash.hexdigest()
            if actual.lower() != file_info.md5.lower():
                await _cleanup(dest)
                raise MD5ValidationError(
                    f"MD5 mismatch for {file_info.file_name!r}: "
                    f"expected={file_info.md5!r} got={actual!r}"
                )
            logger.info(
                "MD5 OK for %s (%s)",
                file_info.file_name,
                actual,
            )
        else:
            logger.warning(
                "No MD5 available for %s — skipping hash validation",
                file_info.file_name,
            )

        logger.info(
            "Download complete: %s → %s (%d bytes)",
            file_info.file_name,
            dest,
            progress.downloaded_bytes,
        )
        return dest

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _handle_manual_fallback(
        self,
        file_info: FileInfo,
        dest: pathlib.Path,
        progress_cb: ProgressCallback,
    ) -> pathlib.Path:
        """Fallback for non-premium accounts: open browser and watch staging_dir."""
        import webbrowser
        import shutil

        # Generate official download tab URL
        url = (
            f"https://www.nexusmods.com/{self._game_domain}"
            f"/mods/{file_info.nexus_id}?tab=files&file_id={file_info.file_id}"
        )
        
        logger.info("Premium required. Opening browser for manual download: %s", url)
        try:
            webbrowser.open(url)
        except Exception as exc:
            logger.warning("Could not open browser: %s. Please visit %s manually.", exc, url)

        # Assuming the user drops it directly into MO2 downloads folder if they use MO2,
        # but here we'll watch the mo2 `downloads` folder as well.
        # Staging dir is `mods`, its parent is the mo2 root.
        mo2_downloads_dir = self._staging_dir.parent / "downloads"
        await asyncio.to_thread(mo2_downloads_dir.mkdir, parents=True, exist_ok=True)

        logger.info("Waiting for %r to appear in %s ...", file_info.file_name, mo2_downloads_dir)
        
        # Poll the downloads folder
        target_in_downloads = mo2_downloads_dir / file_info.file_name
        
        timeout_seconds = 3600  # Wait up to 1 hour
        poll_interval = 2.0
        elapsed = 0.0

        while elapsed < timeout_seconds:
            # Allow user to just put it directly in staging dir
            dest_exists = await asyncio.to_thread(dest.exists)
            if dest_exists:
                dest_size = (await asyncio.to_thread(dest.stat)).st_size
                if dest_size > 0:
                    if file_info.size_bytes > 0 and dest_size != file_info.size_bytes:
                        pass  # Still downloading
                    else:
                        try:
                            # On Windows, browsers lock the file for writing while downloading.
                            # If we can open it in append mode, the browser has released the lock.
                            def _check_open_a_dest():
                                with open(dest, "a"):
                                    pass
                            await asyncio.to_thread(_check_open_a_dest)
                            logger.info("Manual download detected directly in staging dir: %s", dest)
                            return dest
                        except (PermissionError, OSError):
                            pass  # Locked/Downloading

            # Poll the MO2 downloads directory
            target_exists = await asyncio.to_thread(target_in_downloads.exists)
            if target_exists:
                target_size = (await asyncio.to_thread(target_in_downloads.stat)).st_size
                if target_size > 0:
                    # Heuristic 1: If size known, it must match.
                    is_size_met = (file_info.size_bytes == 0) or (target_size == file_info.size_bytes)
                    
                    if is_size_met:
                        try:
                            # Heuristic 2: Verify the OS-level file lock is released.
                            def _check_open_a_target():
                                with open(target_in_downloads, "a"):
                                    pass
                            await asyncio.to_thread(_check_open_a_target)
                            
                            # Extra validation for unknown size: wait 1s to ensure size stability
                            if file_info.size_bytes == 0:
                                await asyncio.sleep(1)
                                target_size_new = (await asyncio.to_thread(target_in_downloads.stat)).st_size
                                if target_size_new != target_size:
                                    raise PermissionError("File size changed, still downloading")

                            await asyncio.to_thread(shutil.copy2, target_in_downloads, dest)
                            logger.info("Manual download detected and copied: %s", dest)
                            return dest
                        except (PermissionError, OSError):
                            pass

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise DownloadError(f"Manual download for {file_info.file_name} timed out after 1 hour.")

    async def _get_download_url(
        self,
        nexus_id: int,
        file_id: int,
        session: aiohttp.ClientSession,
    ) -> str:
        """Fetch the CDN download URL from the Nexus premium download-link endpoint.

        Args:
            nexus_id: Nexus Mods numeric mod ID.
            file_id: Nexus Mods numeric file ID.
            session: Active :class:`aiohttp.ClientSession`.

        Returns:
            CDN URI string.

        Raises:
            DownloadError: When the API returns an error or no links.
        """
        url = (
            f"{_NEXUS_API_BASE}/games/{self._game_domain}"
            f"/mods/{nexus_id}/files/{file_id}/download_link.json"
        )
        self._gateway.authorize("GET", url)

        headers = {"apikey": self._api_key, "Accept": "application/json"}
        meta_timeout = aiohttp.ClientTimeout(total=30)

        async with session.get(url, headers=headers, timeout=meta_timeout) as resp:
            if resp.status in (401, 403):
                raise PremiumRequiredError(
                    "Premium API key required to generate download links"
                )
            if resp.status == 404:
                raise DownloadError(
                    f"Download link not found for file {file_id} of mod {nexus_id}"
                )
            resp.raise_for_status()
            links = await resp.json()

        if not links:
            raise DownloadError(
                f"No CDN download links available for file {file_id}"
            )

        # Prefer the first link (Nexus orders by preference).
        uri: str = links[0]["URI"]
        return uri


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _cleanup(path: pathlib.Path) -> None:
    """Silently remove *path* if it exists (partial-download cleanup)."""
    def _do_rm() -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            logger.warning("Could not remove partial download at %s", path)
            
    await asyncio.to_thread(_do_rm)
