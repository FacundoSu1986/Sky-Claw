"""Environment snapshot — structured representation of the user's modding setup.

All dataclasses are frozen and serializable for easy logging, caching,
and transmission to the GUI or Telegram reporter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class SkyrimEdition(StrEnum):
    """Supported Skyrim editions."""

    SE = "Special Edition"
    AE = "Anniversary Edition"
    LE = "Legendary Edition"
    UNKNOWN = "Unknown"


class HealthStatus(StrEnum):
    """Overall health of the modding environment."""

    READY = "ready"  # All critical tools found
    NEEDS_SETUP = "needs_setup"  # Some tools missing but game found
    CRITICAL = "critical"  # Game not found or fatal misconfiguration


class ToolReadiness(StrEnum):
    """Estado semántico de preparación y disponibilidad para herramientas externas."""

    FOUND = "found"
    MISSING = "missing"
    INVALID_PATH = "invalid_path"
    WRONG_EXECUTABLE = "wrong_executable"
    STALE_CONFIGURED_PATH = "stale_configured_path"
    MOVED_INSTALLATION = "moved_installation"
    VERSION_UNKNOWN = "version_unknown"
    VERSION_UNSUPPORTED = "version_unsupported"


def classify_tool_readiness(
    *,
    configured_path: Path | None = None,
    discovered_path: Path | None = None,
    expected_names: tuple[str, ...] = (),
    configured_path_exists: bool | None = None,
    configured_path_is_file: bool | None = None,
    version_supported: bool | None = None,
    version_unknown: bool = False,
) -> ToolReadiness:
    """Clasificación pura de readiness sin I/O nuevo.

    Evalúa la evidencia disponible (ruta configurada vs descubierta, existencia,
    nombres esperados y soporte de versión) y devuelve el estado semántico
    correspondiente según la máquina de estados de ToolReadiness.
    """
    if version_supported is False:
        return ToolReadiness.VERSION_UNSUPPORTED
    if version_unknown:
        return ToolReadiness.VERSION_UNKNOWN

    if configured_path is not None:
        name = configured_path.name
        expected_folded = {expected.casefold() for expected in expected_names}
        is_expected = not expected_names or name.casefold() in expected_folded

        exists = configured_path_exists if configured_path_exists is not None else configured_path.exists()
        if not exists:
            if discovered_path is not None:
                return ToolReadiness.MOVED_INSTALLATION
            return ToolReadiness.STALE_CONFIGURED_PATH

        is_file = configured_path_is_file if configured_path_is_file is not None else configured_path.is_file()
        if is_file:
            if not is_expected:
                return ToolReadiness.WRONG_EXECUTABLE
            return ToolReadiness.FOUND
        return ToolReadiness.INVALID_PATH

    if discovered_path is not None:
        return ToolReadiness.FOUND

    return ToolReadiness.MISSING


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
    data_root: Path | None = None
    mods_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """A single detected external tool."""

    name: str  # Human-readable name (e.g. "LOOT")
    exe_path: Path  # Full path to the executable
    version: str = ""  # Version if detectable
    friendly_action: str = ""  # What pressing the button does (Spanish)
    readiness: ToolReadiness = ToolReadiness.FOUND


@dataclass(frozen=True, slots=True)
class MissingTool:
    """A tool that should exist but wasn't found."""

    name: str
    technical_name: str  # e.g. "LOOT", "SSEEdit"
    friendly_description: str  # Spanish, user-facing
    download_url: str  # Official download page
    is_critical: bool = False  # True = blocks "Preparar Juego"
    readiness: ToolReadiness = ToolReadiness.MISSING


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

    def skse_present_but_unverified(self) -> bool:
        """SKSE detectado en disco pero sin compatibilidad verificable.

        La señal reusa la MISMA evidencia que ya registró el scanner
        (``tools["skse"]``, poblado por ``find_skse_installation``, y ``skyrim.version``):
        con la versión exacta del ejecutable ilegible, ``find_skse_installation`` degrada
        a "loader + algún DLL de runtime" — presencia — en vez de probar el build —
        compatibilidad. No es una segunda lógica de detección: es la lectura del
        estado que el scanner ya dejó en el snapshot, y la consumen el mensaje de
        health del scanner y el estado de la tarjeta SKSE en el Forge dashboard.
        """
        return self.has_tool("skse") and self.skyrim is not None and self.skyrim.version == ""

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
