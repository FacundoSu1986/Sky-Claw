"""Auto-installer for external tools (LOOT, SSEEdit).

Downloads official releases from GitHub when the tools are not found
locally.  Every download requires mandatory HITL operator approval and
passes through :class:`NetworkGateway` for egress control.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from sky_claw.config import (
    SystemPaths,
)
from sky_claw.security.hitl import Decision, HITLGuard
from sky_claw.security.path_validator import PathValidator, PathViolation

if TYPE_CHECKING:
    from sky_claw.scraper.nexus_downloader import NexusDownloader
    from sky_claw.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

# GitHub API endpoints for official releases.
_LOOT_RELEASES_URL = "https://api.github.com/repos/loot/loot/releases/latest"
_XEDIT_RELEASES_URL = "https://api.github.com/repos/TES5Edit/TES5Edit/releases/latest"
_PANDORA_RELEASES_URL = "https://api.github.com/repos/Monitor221hz/Pandora-Behaviour-Engine-Plus/releases/latest"

# Common Windows paths where LOOT / SSEEdit may already be installed.
LOOT_COMMON_PATHS: tuple[pathlib.Path, ...] = (
    SystemPaths.modding_root() / "LOOT",
    SystemPaths.get_base_drive() / "LOOT",
    SystemPaths.get_base_drive() / "Program Files/LOOT",
    SystemPaths.get_base_drive() / "Program Files (x86)/LOOT",
)

XEDIT_COMMON_PATHS: tuple[pathlib.Path, ...] = (
    SystemPaths.modding_root() / "SSEEdit",
    SystemPaths.get_base_drive() / "SSEEdit",
    SystemPaths.get_base_drive() / "Program Files/SSEEdit",
    SystemPaths.get_base_drive() / "Program Files (x86)/SSEEdit",
)

# Chunk size for streaming downloads (1 MB).
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """Metadata for a single GitHub release asset."""

    name: str
    size: int
    download_url: str


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Result of an auto-install operation."""

    tool_name: str
    exe_path: pathlib.Path
    version: str
    already_existed: bool


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ToolInstallError(Exception):
    """Raised when a tool installation fails."""


# ---------------------------------------------------------------------------
# Zip-slip protection
# ---------------------------------------------------------------------------


def _is_safe_path(member_path: str) -> bool:
    """Reject paths with traversal components."""
    try:
        from sky_claw.core.validators import validate_path_strict

        # Rechazar explícitamente rutas absolutas o con letra de unidad
        if (
            pathlib.PureWindowsPath(member_path).is_absolute()
            or pathlib.PurePosixPath(member_path).is_absolute()
        ):
            return False

        validate_path_strict(member_path)
        return True
    except Exception:
        return False


def _extract_zip_safe(archive: pathlib.Path, dest: pathlib.Path) -> None:
    """Extract a zip archive with zip-slip protection."""
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not _is_safe_path(info.filename):
                raise PathViolation(f"Zip-slip detected: {info.filename!r}")
            zf.extract(info, dest)


def _extract_7z_safe(archive: pathlib.Path, dest: pathlib.Path) -> None:
    """Extract a 7z archive with zip-slip protection."""
    try:
        import py7zr
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "py7zr is required for .7z extraction — pip install py7zr"
        ) from exc

    with py7zr.SevenZipFile(archive, "r") as szf:
        for name in szf.getnames():
            if not _is_safe_path(name):
                raise PathViolation(f"Zip-slip detected in 7z: {name!r}")
        szf.extractall(dest)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_exe_in_dir(directory: pathlib.Path, exe_name: str) -> pathlib.Path | None:
    """Recursively search *directory* for *exe_name* and return its path."""
    if not directory.is_dir():
        return None
    for path in directory.rglob(exe_name):
        if path.is_file():
            return path
    return None


def scan_common_paths(
    common_paths: tuple[pathlib.Path, ...], exe_name: str
) -> pathlib.Path | None:
    """Check common installation directories for an executable."""
    for base in common_paths:
        found = find_exe_in_dir(base, exe_name)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# ToolsInstaller
# ---------------------------------------------------------------------------


class ToolsInstaller:
    """Downloads and extracts LOOT and SSEEdit from official GitHub releases.

    Every download:
    - Requires HITL operator approval.
    - Goes through NetworkGateway egress control.
    - Uses PathValidator for all file operations.
    - Validates file size post-download.
    - Applies zip-slip protection during extraction.

    Parameters
    ----------
    hitl:
        Human-in-the-loop guard for mandatory approval.
    gateway:
        Network gateway for egress authorization.
    path_validator:
        Sandbox validator for file I/O.
    """

    def __init__(
        self,
        hitl: HITLGuard,
        gateway: NetworkGateway,
        path_validator: PathValidator,
    ) -> None:
        self._hitl = hitl
        self._gateway = gateway
        self._validator = path_validator

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure_loot(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
    ) -> InstallResult:
        """Ensure LOOT is available, downloading if necessary.

        Args:
            install_dir: Directory to install LOOT into.
            session: Active HTTP session.

        Returns:
            :class:`InstallResult` with the path to ``loot.exe``.
        """
        self._validator.validate(install_dir)
        exe = find_exe_in_dir(install_dir, "loot.exe")
        if exe is not None:
            logger.info("LOOT already installed at %s", exe)
            return InstallResult(
                tool_name="LOOT",
                exe_path=exe,
                version="existing",
                already_existed=True,
            )

        asset, version = await self._find_github_asset(
            session,
            _LOOT_RELEASES_URL,
            keyword="win64",
        )

        decision = await self._hitl.request_approval(
            request_id=f"install-loot-{version}",
            reason=f"Install LOOT {version}?",
            url=asset.download_url,
            detail=(
                f"Asset: {asset.name}\nSize: {asset.size / (1024 * 1024):.1f} MB\nSource: GitHub loot/loot releases"
            ),
        )
        if decision is not Decision.APPROVED:
            raise ToolInstallError(
                f"LOOT installation denied by operator (decision={decision.value})"
            )

        archive = await self._download_asset(session, asset, install_dir)
        self._extract(archive, install_dir)
        archive.unlink(missing_ok=True)

        exe = find_exe_in_dir(install_dir, "loot.exe")
        if exe is None:
            raise ToolInstallError(
                "LOOT extraction succeeded but loot.exe not found in output"
            )

        logger.info("LOOT %s installed at %s", version, exe)
        return InstallResult(
            tool_name="LOOT",
            exe_path=exe,
            version=version,
            already_existed=False,
        )

    async def ensure_xedit(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
    ) -> InstallResult:
        """Ensure SSEEdit is available, downloading if necessary.

        Args:
            install_dir: Directory to install SSEEdit into.
            session: Active HTTP session.

        Returns:
            :class:`InstallResult` with the path to ``SSEEdit.exe``.
        """
        self._validator.validate(install_dir)
        exe = find_exe_in_dir(install_dir, "SSEEdit.exe")
        if exe is not None:
            logger.info("SSEEdit already installed at %s", exe)
            return InstallResult(
                tool_name="SSEEdit",
                exe_path=exe,
                version="existing",
                already_existed=True,
            )

        asset, version = await self._find_github_asset(
            session,
            _XEDIT_RELEASES_URL,
            keyword="SSEEdit",
        )

        decision = await self._hitl.request_approval(
            request_id=f"install-xedit-{version}",
            reason=f"Install SSEEdit {version}?",
            url=asset.download_url,
            detail=(
                f"Asset: {asset.name}\n"
                f"Size: {asset.size / (1024 * 1024):.1f} MB\n"
                f"Source: GitHub TES5Edit/TES5Edit releases"
            ),
        )
        if decision is not Decision.APPROVED:
            raise ToolInstallError(
                f"SSEEdit installation denied by operator (decision={decision.value})"
            )

        archive = await self._download_asset(session, asset, install_dir)
        self._extract(archive, install_dir)
        archive.unlink(missing_ok=True)

        exe = find_exe_in_dir(install_dir, "SSEEdit.exe")
        if exe is None:
            raise ToolInstallError(
                "SSEEdit extraction succeeded but SSEEdit.exe not found in output"
            )

        logger.info("SSEEdit %s installed at %s", version, exe)
        return InstallResult(
            tool_name="SSEEdit",
            exe_path=exe,
            version=version,
            already_existed=False,
        )

    async def ensure_pandora(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
    ) -> InstallResult:
        """Ensure Pandora Behavior Engine is available, downloading if necessary.

        Args:
            install_dir: Directory to install Pandora into.
            session: Active HTTP session.

        Returns:
            :class:`InstallResult` with the path to ``Pandora.exe``.
        """
        self._validator.validate(install_dir)
        # Check if already installed
        exe = find_exe_in_dir(install_dir, "Pandora.exe")
        if exe is not None:
            logger.info("Pandora already installed at %s", exe)
            return InstallResult(
                tool_name="Pandora",
                exe_path=exe,
                version="existing",
                already_existed=True,
            )

        asset, version = await self._find_github_asset(
            session,
            _PANDORA_RELEASES_URL,
            keyword="Pandora_Behaviour_Engine",
        )

        decision = await self._hitl.request_approval(
            request_id=f"install-pandora-{version}",
            reason=f"Install Pandora Behavior Engine {version}?",
            url=asset.download_url,
            detail=(
                f"Asset: {asset.name}\n"
                f"Size: {asset.size / (1024 * 1024):.1f} MB\n"
                f"Source: GitHub Monitor221hz/Pandora-Behaviour-Engine-Plus"
            ),
        )
        if decision is not Decision.APPROVED:
            raise ToolInstallError(
                f"Pandora installation denied by operator (decision={decision.value})"
            )

        archive = await self._download_asset(session, asset, install_dir)
        self._extract(archive, install_dir)
        archive.unlink(missing_ok=True)

        exe = find_exe_in_dir(install_dir, "Pandora.exe")
        if exe is None:
            raise ToolInstallError(
                "Pandora extraction succeeded but Pandora.exe not found in output"
            )

        logger.info("Pandora %s installed at %s", version, exe)
        return InstallResult(
            tool_name="Pandora",
            exe_path=exe,
            version=version,
            already_existed=False,
        )

    async def ensure_bodyslide(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        downloader: NexusDownloader | None = None,
    ) -> InstallResult:
        """Ensure BodySlide is available, downloading from Nexus if necessary.

        Args:
            install_dir: Directory to install BodySlide into.
            session: Active HTTP session.
            downloader: Optional Nexus downloader.

        Returns:
            :class:`InstallResult` with the path to ``BodySlide.exe``.
        """
        self._validator.validate(install_dir)
        # Check if already installed
        exe = find_exe_in_dir(install_dir, "BodySlide.exe")
        if exe is not None:
            logger.info("BodySlide already installed at %s", exe)
            return InstallResult(
                tool_name="BodySlide",
                exe_path=exe,
                version="existing",
                already_existed=True,
            )

        if downloader is None:
            raise ToolInstallError(
                "BodySlide requires a NexusDownloader for installation (not on GitHub)."
            )

        # BodySlide is Mod ID 201 on SSE.
        nexus_id = 201
        logger.info("Fetching BodySlide metadata from Nexus (ID %d)...", nexus_id)
        try:
            file_info = await downloader.get_file_info(nexus_id, None, session)
        except Exception as exc:
            raise ToolInstallError(
                f"Failed to fetch BodySlide info from Nexus: {exc}"
            ) from exc

        decision = await self._hitl.request_approval(
            request_id=f"install-bodyslide-{file_info.file_id}",
            reason=f"Install BodySlide and Outfit Studio (Nexus ID {nexus_id})?",
            url=f"https://www.nexusmods.com/skyrimspecialedition/mods/{nexus_id}",
            detail=(
                f"File: {file_info.file_name}\nSize: {file_info.size_bytes / (1024 * 1024):.1f} MB\nSource: Nexus Mods"
            ),
        )
        if decision is not Decision.APPROVED:
            raise ToolInstallError(
                f"BodySlide installation denied by operator (decision={decision.value})"
            )

        # Download to staging dir first (as per NexusDownloader logic)
        archive_path = await downloader.download(file_info, session)

        # Extract into the specialized tools directory
        self._extract(archive_path, install_dir)

        # Cleanup archive in staging
        archive_path.unlink(missing_ok=True)

        exe = find_exe_in_dir(install_dir, "BodySlide.exe")
        if exe is None:
            raise ToolInstallError(
                "BodySlide extraction succeeded but BodySlide.exe not found in output"
            )

        logger.info("BodySlide installed at %s", exe)
        return InstallResult(
            tool_name="BodySlide",
            exe_path=exe,
            version="Nexus",
            already_existed=False,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _find_github_asset(
        self,
        session: aiohttp.ClientSession,
        releases_url: str,
        keyword: str,
    ) -> tuple[ReleaseAsset, str]:
        """Fetch the latest GitHub release and find a matching asset.

        Args:
            session: HTTP session.
            releases_url: GitHub API URL for latest release.
            keyword: Substring the asset name must contain.

        Returns:
            Tuple of (:class:`ReleaseAsset`, version tag).
        """
        await self._gateway.authorize("GET", releases_url)

        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"Accept": "application/vnd.github+json"}

        async with session.get(releases_url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                raise ToolInstallError(
                    f"GitHub API returned {resp.status} for {releases_url}"
                )
            data: dict[str, Any] = await resp.json()

        version: str = data.get("tag_name", "unknown")
        assets: list[dict[str, Any]] = data.get("assets", [])

        # Find the first asset whose name contains the keyword and is
        # a .zip or .7z archive.
        for a in assets:
            name: str = a.get("name", "")
            if keyword.lower() in name.lower() and (
                name.endswith(".zip") or name.endswith(".7z")
            ):
                return (
                    ReleaseAsset(
                        name=name,
                        size=int(a.get("size", 0)),
                        download_url=str(a["browser_download_url"]),
                    ),
                    version,
                )

        available = [a.get("name", "?") for a in assets]
        raise ToolInstallError(
            f"No asset matching '{keyword}' (.zip/.7z) in release {version}. Available: {available}"
        )

    async def _download_asset(
        self,
        session: aiohttp.ClientSession,
        asset: ReleaseAsset,
        dest_dir: pathlib.Path,
    ) -> pathlib.Path:
        """Download a GitHub release asset to *dest_dir*.

        Validates file size after download.  Logs progress every 10 MB.
        """
        await self._gateway.authorize("GET", asset.download_url)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / asset.name
        self._validator.validate(dest)

        timeout = aiohttp.ClientTimeout(total=600, sock_read=60)
        downloaded = 0
        hasher = hashlib.sha256()

        logger.info(
            "Downloading %s (%.1f MB) ...",
            asset.name,
            asset.size / (1024 * 1024),
        )

        try:
            async with session.get(asset.download_url, timeout=timeout) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                        fh.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if downloaded % (10 * _DOWNLOAD_CHUNK_SIZE) == 0:
                            logger.info(
                                "  ... %d / %d bytes (%.0f%%)",
                                downloaded,
                                asset.size,
                                (downloaded / asset.size * 100) if asset.size else 0,
                            )
        except Exception as exc:
            logger.error("Download failed for %s: %s", asset.name, exc)
            if dest.exists():
                dest.unlink()
            raise

        # Size validation.
        if asset.size > 0 and downloaded != asset.size:
            dest.unlink(missing_ok=True)
            raise ToolInstallError(
                f"Size mismatch for {asset.name}: expected {asset.size}, got {downloaded}"
            )

        logger.info(
            "Downloaded %s (%d bytes, sha256=%s)",
            asset.name,
            downloaded,
            hasher.hexdigest()[:16],
        )
        return dest

    def _extract(self, archive: pathlib.Path, dest: pathlib.Path) -> None:
        """Extract archive into *dest* with zip-slip protection."""
        suffix = archive.suffix.lower()
        if suffix == ".zip":
            _extract_zip_safe(archive, dest)
        elif suffix == ".7z":
            _extract_7z_safe(archive, dest)
        else:
            raise ToolInstallError(f"Unsupported archive format: {suffix}")
        logger.info("Extracted %s → %s", archive.name, dest)
