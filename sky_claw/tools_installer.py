"""Auto-installer for external tools (LOOT, SSEEdit, DynDOLOD, Pandora, BodySlide).

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
    """Extract a zip archive with zip-slip protection.

    Validates both the relative path (no '..' or absolute paths) and the
    resolved destination path (must remain inside *dest* after resolution).

    Note: ZIP entries with symlink metadata are not extracted as symlinks by Python's
    zipfile module on Windows (the primary target platform), mitigating symlink-escape attacks.
    """
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not _is_safe_path(info.filename):
                raise PathViolation(f"Zip-slip detected: {info.filename!r}")
            # Secondary check: resolved path must stay inside dest
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest_resolved):
                raise PathViolation(
                    f"Zip-slip (resolved path escapes sandbox): {info.filename!r}"
                )
            zf.extract(info, dest)


def _extract_7z