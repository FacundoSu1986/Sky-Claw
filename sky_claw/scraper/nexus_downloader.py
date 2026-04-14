import asyncio
import hashlib
import logging
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiofiles
import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from sky_claw.config import (
    NEXUS_DOWNLOAD_CHUNK_SIZE,
    NEXUS_DOWNLOAD_TIMEOUT_SECONDS,
)
from sky_claw.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

_NEXUS_API_BASE = "https://api.nexusmods.com/v1"

ProgressCallback = Callable[["DownloadProgress"], Awaitable[None]] | None


class DownloadError(Exception):
    pass


class HashValidationError(DownloadError):
    """Error de validación de hash (MD5 o SHA256)."""

    pass


# Alias para compatibilidad hacia atrás
MD5ValidationError = HashValidationError


class PremiumRequiredError(DownloadError):
    pass


@dataclass(frozen=True, slots=True)
class FileInfo:
    """Metadata for a Nexus file, fetched before the actual download.

    Attributes:
        nexus_id: Nexus Mods numeric mod ID.
        file_id: Nexus Mods numeric file ID.
        file_name: Original filename on Nexus.
        size_bytes: Expected file size in bytes (0 when unknown).
        md5: MD5 hex-digest provided by the Nexus API (empty string when absent).
        sha256: SHA256 hex-digest for local integrity verification (64 chars hex, empty when absent).
        download_url: CDN URL for the actual file bytes.
    """

    nexus_id: int
    file_id: int
    file_name: str
    size_bytes: int
    md5: str
    sha256: str = ""  # NUEVO: Para integridad local (64 chars hex)
    download_url: str = ""


def validate_sha256_format(hash_str: str) -> bool:
    """Valida formato de hash SHA256 (64 chars hex).

    Args:
        hash_str: String a validar como hash SHA256.

    Returns:
        True si el string es un hash SHA256 válido (64 caracteres hexadecimales).
    """
    if len(hash_str) != 64:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in hash_str)


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


def _should_retry_nexus(exc: BaseException) -> bool:
    """Determina si la excepción justifica un reintento (Network drop, 429, Hash mismatch)."""
    if isinstance(
        exc,
        (
            aiohttp.ClientConnectionError,
            asyncio.TimeoutError,
            OSError,
            MD5ValidationError,
        ),
    ):
        return True
    if isinstance(exc, aiohttp.ClientResponseError):
        # Reintentar explícitamente en Rate Limits (429) y errores del servidor Nexus (5xx)
        return exc.status == 429 or exc.status >= 500
    if isinstance(exc, DownloadError):
        msg = str(exc).lower()
        return "429" in msg or "502" in msg or "503" in msg or "timeout" in msg
    return False


def _log_retry(retry_state) -> None:
    logger.warning(
        "Falla en Nexus (Intento %d/5). Reintentando en %.1fs... Motivo: %s",
        retry_state.attempt_number,
        retry_state.next_action.sleep,
        repr(retry_state.outcome.exception()),
    )


class NexusDownloader:
    def __init__(
        self,
        api_key: str,
        gateway: NetworkGateway,
        staging_dir: pathlib.Path,
        chunk_size: int = NEXUS_DOWNLOAD_CHUNK_SIZE,
        timeout: int = NEXUS_DOWNLOAD_TIMEOUT_SECONDS,
        game_domain: str = "skyrimspecialedition",
        file_info_retry_wait=None,
        download_retry_wait=None,
    ) -> None:
        import random

        self._api_key = api_key
        self._gateway = gateway
        self._staging_dir = staging_dir
        self._chunk_size = chunk_size
        self._timeout = timeout
        self._game_domain = game_domain
        self._semaphore = asyncio.Semaphore(5)
        self._random = random
        # Retry wait strategies; override in tests to avoid real sleeps
        self._file_info_retry_wait = (
            file_info_retry_wait
            if file_info_retry_wait is not None
            else wait_exponential(multiplier=2, min=2, max=60)
        )
        self._download_retry_wait = (
            download_retry_wait
            if download_retry_wait is not None
            else wait_exponential(multiplier=4, min=5, max=120)
        )

    @property
    def staging_dir(self) -> pathlib.Path:
        return self._staging_dir

    @property
    def timeout(self) -> int:
        return self._timeout

    # Aplicamos Exponential Backoff. Empieza en 2s, sube hasta 60s, max 5 intentos.
    async def get_file_info(
        self,
        nexus_id: int,
        file_id: int | None,
        session: aiohttp.ClientSession,
    ) -> FileInfo:
        result: FileInfo
        async for attempt in AsyncRetrying(
            wait=self._file_info_retry_wait,
            stop=stop_after_attempt(5),
            retry=retry_if_exception(_should_retry_nexus),
            before_sleep=_log_retry,
            reraise=True,
        ):
            with attempt:
                async with self._semaphore:
                    # Añadir jitter para evitar Thundering Herd
                    await asyncio.sleep(self._random.uniform(0.1, 0.5))

                    headers = {"apikey": self._api_key, "Accept": "application/json"}
                    meta_timeout = aiohttp.ClientTimeout(total=30)

                    if file_id is None:
                        files_url = f"{_NEXUS_API_BASE}/games/{self._game_domain}/mods/{nexus_id}/files.json"
                        await self._gateway.authorize("GET", files_url)
                        async with session.get(
                            files_url, headers=headers, timeout=meta_timeout
                        ) as resp:
                            if resp.status in (401, 403):
                                raise DownloadError("Invalid or missing Nexus API key")
                            if resp.status == 404:
                                raise DownloadError(f"Mod {nexus_id} not found")
                            resp.raise_for_status()  # Dispara ClientResponseError en 429 para activar el reintento
                            files_data = await resp.json()

                        files_list = files_data.get("files", [])
                        if not files_list:
                            raise DownloadError(f"No files found for mod {nexus_id}")

                        primary_files = [f for f in files_list if f.get("is_primary")]
                        target_file = (
                            primary_files[0] if primary_files else files_list[-1]
                        )
                        file_id = target_file["file_id"]
                        if isinstance(target_file.get("file_id"), list):
                            file_id = target_file["file_id"][1]

                meta_url = f"{_NEXUS_API_BASE}/games/{self._game_domain}/mods/{nexus_id}/files/{file_id}.json"
                await self._gateway.authorize("GET", meta_url)

                async with session.get(
                    meta_url, headers=headers, timeout=meta_timeout
                ) as resp:
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
                    data.get("size_in_bytes") or data.get("size", 0) * 1024
                )
                md5: str = str(data.get("md5") or "")
                file_name: str = str(
                    data.get("file_name", f"mod_{nexus_id}_file_{file_id}")
                )

                try:
                    download_url = await self._get_download_url(
                        nexus_id, file_id, session
                    )
                except PremiumRequiredError:
                    download_url = "FREE_FALLBACK"

                result = FileInfo(
                    nexus_id=nexus_id,
                    file_id=file_id,
                    file_name=file_name,
                    size_bytes=size_bytes,
                    md5=md5,
                    download_url=download_url,
                )
        return result

    # Backoff más agresivo para la descarga en sí.
    async def download(
        self,
        file_info: FileInfo,
        session: aiohttp.ClientSession,
        progress_cb: ProgressCallback = None,
    ) -> pathlib.Path:
        # Handle FREE_FALLBACK before the retry loop – no retry needed here
        async with self._semaphore:
            self._staging_dir.mkdir(parents=True, exist_ok=True)
            dest = self._staging_dir / file_info.file_name
            if file_info.download_url == "FREE_FALLBACK":
                return await self._handle_manual_fallback(file_info, dest, progress_cb)

        dest_result: pathlib.Path
        async for attempt in AsyncRetrying(
            wait=self._download_retry_wait,
            stop=stop_after_attempt(5),
            retry=retry_if_exception(_should_retry_nexus),
            before_sleep=_log_retry,
            reraise=True,
        ):
            with attempt:
                async with self._semaphore:
                    # Añadir jitter para evitar Thundering Herd
                    await asyncio.sleep(self._random.uniform(0.1, 0.5))

                    self._staging_dir.mkdir(parents=True, exist_ok=True)
                    dest = self._staging_dir / file_info.file_name

                await self._gateway.authorize("GET", file_info.download_url)

                progress = DownloadProgress(
                    file_name=file_info.file_name,
                    total_bytes=file_info.size_bytes,
                )
                timeout = aiohttp.ClientTimeout(total=self._timeout, sock_read=60)
                headers = {"apikey": self._api_key}
                # Inicializar ambos hashes para cálculo dual
                md5_hash = hashlib.md5(usedforsecurity=False)  # nosec B324 - used for file integrity checksum, not security
                sha256_hash = hashlib.sha256()

                try:
                    async with session.get(
                        file_info.download_url, headers=headers, timeout=timeout
                    ) as resp:
                        resp.raise_for_status()

                        if progress.total_bytes == 0:
                            content_length = resp.headers.get("Content-Length")
                            if content_length:
                                progress.total_bytes = int(content_length)

                        async with aiofiles.open(dest, "wb") as fh:
                            async for chunk in resp.content.iter_chunked(
                                self._chunk_size
                            ):
                                await fh.write(chunk)
                                # Actualizar ambos hashes por chunk
                                md5_hash.update(chunk)
                                sha256_hash.update(chunk)
                                progress.downloaded_bytes += len(chunk)
                                if progress_cb is not None:
                                    await progress_cb(progress)
                                # Yield the event loop para evitar bloquear el thread principal
                                await asyncio.sleep(0)

                except aiohttp.ClientError as exc:
                    await _cleanup(dest)
                    raise DownloadError(
                        f"Download failed for {file_info.file_name!r}: {exc}"
                    ) from exc
                except (TimeoutError, OSError) as exc:
                    await _cleanup(dest)
                    raise DownloadError(
                        f"I/O or timeout error for {file_info.file_name!r}: {exc}"
                    ) from exc

                # POST-DOWNLOAD: Validación de Hash estricta (MD5 contra Nexus API).
                # Si esto falla, lanza HashValidationError, lo atrapa Tenacity, limpia el archivo corrupto y reintenta.
                if file_info.md5:
                    actual_md5 = md5_hash.hexdigest()
                    if actual_md5.lower() != file_info.md5.lower():
                        await _cleanup(dest)
                        raise HashValidationError(
                            f"MD5 mismatch for {file_info.file_name!r}: expected={file_info.md5!r} got={actual_md5!r}"
                        )
                    logger.info(
                        "MD5 validado OK para %s (%s)", file_info.file_name, actual_md5
                    )
                else:
                    logger.warning(
                        "Sin hash MD5 provisto por Nexus para %s. Omitiendo validación MD5.",
                        file_info.file_name,
                    )

                # Calcular y registrar SHA256 para integridad local
                actual_sha256 = sha256_hash.hexdigest()
                logger.info(
                    "SHA256 calculado para %s: %s", file_info.file_name, actual_sha256
                )

                # Validar SHA256 si fue proporcionado
                if file_info.sha256:
                    if not validate_sha256_format(file_info.sha256):
                        logger.warning(
                            "Formato SHA256 inválido proporcionado para %s (se esperaban 64 chars hex)",
                            file_info.file_name,
                        )
                    elif actual_sha256.lower() != file_info.sha256.lower():
                        await _cleanup(dest)
                        raise HashValidationError(
                            f"SHA256 mismatch for {file_info.file_name!r}: "
                            f"expected={file_info.sha256!r} got={actual_sha256!r}"
                        )
                    else:
                        logger.info("SHA256 validado OK para %s", file_info.file_name)

                dest_result = dest
        return dest_result

    async def _handle_manual_fallback(
        self,
        file_info: FileInfo,
        dest: pathlib.Path,
        progress_cb: ProgressCallback,
    ) -> pathlib.Path:
        """Fallback for non-premium accounts: open browser and watch staging_dir."""
        import shutil
        import webbrowser

        # Generate official download tab URL
        url = (
            f"https://www.nexusmods.com/{self._game_domain}"
            f"/mods/{file_info.nexus_id}?tab=files&file_id={file_info.file_id}"
        )

        logger.info("Premium required. Opening browser for manual download: %s", url)
        try:
            webbrowser.open(url)
        except Exception as exc:
            logger.warning(
                "Could not open browser: %s. Please visit %s manually.", exc, url
            )

        # Assuming the user drops it directly into MO2 downloads folder if they use MO2,
        # but here we'll watch the mo2 `downloads` folder as well.
        # Staging dir is `mods`, its parent is the mo2 root.
        mo2_downloads_dir = self._staging_dir.parent / "downloads"
        mo2_downloads_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Waiting for %r to appear in %s ...", file_info.file_name, mo2_downloads_dir
        )

        # Poll the downloads folder
        target_in_downloads = mo2_downloads_dir / file_info.file_name

        timeout_seconds = 3600  # Wait up to 1 hour
        poll_interval = 2.0
        elapsed = 0.0

        while elapsed < timeout_seconds:
            # Allow user to just put it directly in staging dir
            if dest.exists():
                dest_size = dest.stat().st_size
                if dest_size > 0:
                    if file_info.size_bytes > 0 and dest_size != file_info.size_bytes:
                        pass  # Still downloading
                    else:
                        try:
                            # On Windows, browsers lock the file for writing while downloading.
                            # If we can open it in append mode, the browser has released the lock.
                            with open(dest, "a"):
                                pass
                            logger.info(
                                "Manual download detected directly in staging dir: %s",
                                dest,
                            )
                            return dest
                        except (PermissionError, OSError):
                            pass  # Locked/Downloading

            # Poll the MO2 downloads directory
            if target_in_downloads.exists():
                target_size = target_in_downloads.stat().st_size
                if target_size > 0:
                    # Heuristic 1: If size known, it must match.
                    is_size_met = (file_info.size_bytes == 0) or (
                        target_size == file_info.size_bytes
                    )

                    if is_size_met:
                        try:
                            # Heuristic 2: Verify the OS-level file lock is released.
                            with open(target_in_downloads, "a"):
                                pass

                            # Extra validation for unknown size: wait 1s to ensure size stability
                            if file_info.size_bytes == 0:
                                await asyncio.sleep(1)
                                target_size_new = target_in_downloads.stat().st_size
                                if target_size_new != target_size:
                                    raise PermissionError(
                                        "File size changed, still downloading"
                                    )

                            await asyncio.to_thread(
                                shutil.copy2, target_in_downloads, dest
                            )
                            logger.info("Manual download detected and copied: %s", dest)
                            return dest
                        except (PermissionError, OSError):
                            pass

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise DownloadError(
            f"Manual download for {file_info.file_name} timed out after 1 hour."
        )

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
        url = f"{_NEXUS_API_BASE}/games/{self._game_domain}/mods/{nexus_id}/files/{file_id}/download_link.json"
        await self._gateway.authorize("GET", url)

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
            raise DownloadError(f"No CDN download links available for file {file_id}")

        # Prefer the first link (Nexus orders by preference).
        uri: str = links[0]["URI"]
        return uri


async def _cleanup(path: pathlib.Path) -> None:
    """Silently remove *path* if it exists (partial-download cleanup)."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("Could not remove partial download at %s", path)
