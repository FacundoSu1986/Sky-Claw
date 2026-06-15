"""Sky-Claw – Autonomous Skyrim mod management agent."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Canonical source: the installed package version (derived from the git tag
    # by hatch-vcs). Keeps __version__ in sync with the release without a manual
    # bump for dev/installed use.
    __version__ = _pkg_version("sky-claw")
except PackageNotFoundError:
    # Fallback when dist metadata is unavailable (e.g. the PyInstaller frozen
    # exe, which does not bundle .dist-info). Bump to match the release.
    __version__ = "0.2.0"

# FASE 5: Asset Conflict Detection Module
from sky_claw.local.assets import (
    AssetConflictDetector,
    AssetConflictReport,
    AssetInfo,
    AssetType,
)

__all__ = [
    "AssetConflictDetector",
    "AssetConflictReport",
    "AssetInfo",
    "AssetType",
]
