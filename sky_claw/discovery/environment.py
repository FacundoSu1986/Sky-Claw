"""Environment snapshot — structured representation of the user's modding setup.

All dataclasses are frozen and serializable for easy logging, caching,
and transmission to the GUI or Telegram reporter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SkyrimEdition(str, Enum):
    """Supported Skyrim editions."""

    SE = "Special Edition"
    AE = "Anniversary Edition"
    LE = "Legendary Edition"
    UNKNOWN = "Unknown"


class HealthStatus(str, Enum):
    """Overall health of the modding environment."""

    READY = "ready"  # All critical tools found
    NEEDS_SETUP = "needs_setup"  # Some tools missing but game found
    CRITICAL = "critical"  # Game not found or fatal misconfiguration


@dataclass(frozen=True, slots=True)
class SkyrimInfo:
    """Detected Skyrim installation."""

    path: Path
    exe_name: str  # SkyrimSE.exe or Skyrim.exe
    edition: SkyrimEdition = SkyrimEdition.UNKNOWN
    version: str = ""  # e.g. "1.6.1170"
    store: str = "steam"  # steam | gog | epic | unknown


@dataclass(frozen=True, slots=True)
class MO2Info:
    """Detected Mod Organizer 2 installation."""

    path: Path
    profiles: list[str] = field(default_factory=list)
    active_profile: str = "Default"


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """A single detected external tool."""

    name: str  # Human-readable name (e.g. "LOOT")
    exe_path: Path  # Full path to the executable
    version: str = ""  # Version if detectable
    friendly_action: str = ""  # What pressing the button does (Spanish)


@dataclass(frozen=True, slots=True)
class MissingTool:
    """A tool that should exist but wasn't found."""

    name: str
    technical_name: str  # e.g. "LOOT", "SSEEdit"
    friendly_description: str  # Spanish, user-facing
    download_url: str  # Official download page
    is_critical: bool = False  # True = blocks "Preparar Juego"


@dataclass(slots=True)
class EnvironmentSnapshot:
    """Complete snapshot of the user's modding environment.

    Produced by :class:`EnvironmentScanner` during application startup.
    Consumed by the GUI to decide which buttons to show and which
    warnings to display.
    """

    skyrim: SkyrimInfo | None = None
    mo2: MO2Info | None = None
    tools: dict[str, ToolInfo] = field(default_factory=dict)
    missing: list[MissingTool] = field(default_factory=list)
    health_status: HealthStatus = HealthStatus.CRITICAL
    health_messages: list[str] = field(default_factory=list)

    # ── Convenience queries ────────────────────────────────────────────

    def has_tool(self, name: str) -> bool:
        """Check if a tool was detected (case-insensitive key)."""
        return name.lower() in self.tools

    def get_tool(self, name: str) -> ToolInfo | None:
        """Get tool info or None."""
        return self.tools.get(name.lower())

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON / Telegram reporting."""
        return {
            "skyrim": {
                "path": str(self.skyrim.path) if self.skyrim else None,
                "edition": self.skyrim.edition.value if self.skyrim else None,
                "version": self.skyrim.version if self.skyrim else None,
            },
            "mo2": str(self.mo2.path) if self.mo2 else None,
            "tools": {k: str(v.exe_path) for k, v in self.tools.items()},
            "missing": [m.name for m in self.missing],
            "health": self.health_status.value,
            "messages": self.health_messages,
        }
