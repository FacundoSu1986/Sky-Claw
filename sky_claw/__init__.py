"""Sky-Claw – Autonomous Skyrim mod management agent."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING, Any

try:
    # Canonical source: the installed package version (derived from the git tag
    # by hatch-vcs). Keeps __version__ in sync with the release without a manual
    # bump for dev/installed use.
    __version__ = _pkg_version("sky-claw")
except PackageNotFoundError:
    # Fallback when dist metadata is unavailable (e.g. the PyInstaller frozen
    # exe, which does not bundle .dist-info). This literal is the single
    # manual version point: sky_claw.spec also falls back to it when building
    # the exe's VERSIONINFO resource. Bump it to match the release.
    __version__ = "0.2.4"

if TYPE_CHECKING:
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


def __getattr__(name: str) -> Any:
    """Carga perezosa de re-exports para que workers livianos no arrastren ``config``/``keyring``."""
    if name in __all__:
        from sky_claw.local import assets as _assets  # noqa: PLC0415

        return getattr(_assets, name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    """Lista también los re-exports perezosos para que ``dir()``/``help()``/IDEs los vean sin resolverlos."""
    return sorted({*globals(), *__all__})
