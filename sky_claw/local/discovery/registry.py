"""Canonical declarative registry for external tools.

This module is the single source of truth for external tools detected,
managed, or integrated by Sky-Claw. It provides immutable tool specifications
and projections used by the scanner, GUI ritual controllers, and bootloader.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class ExternalToolSpec:
    """Declarative specification for an external tool.

    Contains only metadata needed to scan for, dispatch, or install the tool,
    without holding live scanner state, subprocess handles, or runtime results.
    """

    key: str
    exe_names: tuple[str, ...]
    friendly_action: str
    download_url: str
    is_critical: bool = False
    dispatcher_tool_name: str | None = None
    installer_method: str | None = None
    install_env_var: str | None = None
    snapshot_env_var: str | None = None
    config_field: str | None = None

    @property
    def friendly_description(self) -> str:
        """User-facing description alias for MissingTool representation."""
        return self.friendly_action


_RAW_SPECS: tuple[ExternalToolSpec, ...] = (
    ExternalToolSpec(
        key="skse",
        exe_names=("skse64_loader.exe", "skse_loader.exe"),
        friendly_action="Extensor del motor (SKSE) - Requiere instalación manual o auto-instalador",
        download_url="https://skse.silverlock.org/",
        is_critical=True,
        installer_method="ensure_skse",
    ),
    ExternalToolSpec(
        key="loot",
        exe_names=("LOOT.exe", "loot.exe"),
        friendly_action="Ordenar mods automáticamente",
        download_url="https://github.com/loot/loot/releases",
        is_critical=True,
        dispatcher_tool_name="execute_loot_sorting",
        installer_method="ensure_loot",
        install_env_var="LOOT_EXE",
        snapshot_env_var="LOOT_EXE",
        config_field="loot_exe",
    ),
    ExternalToolSpec(
        key="xedit",
        exe_names=("SSEEdit.exe", "TES5Edit.exe", "xEdit.exe"),
        friendly_action="Limpiar archivos problemáticos",
        download_url="https://github.com/TES5Edit/TES5Edit/releases",
        is_critical=False,
        dispatcher_tool_name="quick_auto_clean",
        installer_method="ensure_xedit",
        install_env_var="XEDIT_PATH",
        snapshot_env_var="XEDIT_PATH",
        config_field="xedit_exe",
    ),
    ExternalToolSpec(
        key="pandora",
        exe_names=("Pandora Behaviour Engine+.exe", "Pandora Engine.exe", "Pandora.exe"),
        friendly_action="Generar animaciones",
        download_url="https://github.com/Monitor221hz/Pandora-Behaviour-Engine-Plus/releases",
        is_critical=False,
        dispatcher_tool_name="generate_animations",
        installer_method="ensure_pandora",
        install_env_var="PANDORA_EXE",
        snapshot_env_var="PANDORA_EXE",
        config_field="pandora_exe",
    ),
    ExternalToolSpec(
        key="wrye_bash",
        exe_names=("Wrye Bash.exe", "Wrye Bash Launcher.exe"),
        friendly_action="Crear parche de compatibilidad",
        download_url="https://www.nexusmods.com/skyrimspecialedition/mods/6837",
        is_critical=False,
        dispatcher_tool_name="generate_bashed_patch",
        snapshot_env_var="WRYE_BASH_PATH",
    ),
    ExternalToolSpec(
        key="dyndolod",
        exe_names=("DynDOLOD64.exe", "DynDOLOD.exe", "DynDOLODx64.exe"),
        friendly_action="Optimizar gráficos (LOD)",
        download_url="https://www.nexusmods.com/skyrimspecialedition/mods/32382",
        is_critical=False,
        dispatcher_tool_name="generate_lods",
        snapshot_env_var="DYNDLOD_EXE",
    ),
    ExternalToolSpec(
        key="community_shaders",
        exe_names=("community_shaders.exe",),
        friendly_action="Renderizado moderno (Community Shaders) - Instalable desde el dashboard",
        download_url="https://www.nexusmods.com/skyrimspecialedition/mods/86492",
        is_critical=False,
        installer_method="ensure_community_shaders",
    ),
)


def _validate_and_freeze_registry(
    specs: tuple[ExternalToolSpec, ...],
) -> MappingProxyType[str, ExternalToolSpec]:
    mapping: dict[str, ExternalToolSpec] = {}
    for spec in specs:
        if spec.key in mapping:
            raise ValueError(f"Clave duplicada en EXTERNAL_TOOL_REGISTRY: {spec.key!r}")
        mapping[spec.key] = spec
    return MappingProxyType(mapping)


EXTERNAL_TOOL_REGISTRY: Final[Mapping[str, ExternalToolSpec]] = _validate_and_freeze_registry(_RAW_SPECS)


def get_tool_spec(key: str) -> ExternalToolSpec | None:
    """Retrieve an external tool specification by key, or ``None``."""
    return EXTERNAL_TOOL_REGISTRY.get(key)


def iter_external_tool_specs() -> tuple[ExternalToolSpec, ...]:
    """Iterate all declared tool specifications in canonical declaration order."""
    return tuple(EXTERNAL_TOOL_REGISTRY.values())


def build_tool_defs() -> tuple[tuple[str, tuple[str, ...], str, str, bool], ...]:
    """Projection matching the legacy ``tool_defs`` structure from ``scanner.py``.

    Yields tuples of ``(key, exe_names, friendly_action, download_url, is_critical)``.
    """
    return tuple(
        (spec.key, spec.exe_names, spec.friendly_action, spec.download_url, spec.is_critical)
        for spec in EXTERNAL_TOOL_REGISTRY.values()
    )


def build_ritual_tool_map() -> dict[str, str]:
    """Projection mapping tool keys to their dispatcher strategy names.

    Derives ``RITUAL_TOOL_MAP`` for ``ritual_runner.py``.
    """
    return {
        spec.key: spec.dispatcher_tool_name
        for spec in EXTERNAL_TOOL_REGISTRY.values()
        if spec.dispatcher_tool_name is not None
    }


def build_ritual_installer_map() -> dict[str, str]:
    """Projection mapping tool keys to their ``ToolsInstaller`` method names.

    Derives ``RITUAL_INSTALLER_MAP`` for ``ritual_runner.py``.
    """
    return {
        spec.key: spec.installer_method for spec in EXTERNAL_TOOL_REGISTRY.values() if spec.installer_method is not None
    }


def build_ritual_install_env() -> dict[str, str]:
    """Projection mapping tool keys to their resolver env vars set upon install.

    Derives ``RITUAL_INSTALL_ENV`` for ``ritual_runner.py``.
    """
    return {
        spec.key: spec.install_env_var for spec in EXTERNAL_TOOL_REGISTRY.values() if spec.install_env_var is not None
    }


def build_snapshot_tool_env() -> dict[str, str]:
    """Projection mapping tool keys to the env vars read by ``PathResolutionService``.

    Derives ``_SNAPSHOT_TOOL_ENV`` for ``_bootloader.py``.
    """
    return {
        spec.key: spec.snapshot_env_var for spec in EXTERNAL_TOOL_REGISTRY.values() if spec.snapshot_env_var is not None
    }


def build_tool_path_cfg_keys() -> dict[str, str]:
    """Projection mapping tool keys to their configured path field in ``config.toml``.

    Derives ``tool_path_cfg_keys`` for ``_build_environment_scanner()`` in ``_bootloader.py``.
    """
    return {spec.key: spec.config_field for spec in EXTERNAL_TOOL_REGISTRY.values() if spec.config_field is not None}


__all__ = [
    "EXTERNAL_TOOL_REGISTRY",
    "ExternalToolSpec",
    "build_ritual_install_env",
    "build_ritual_installer_map",
    "build_ritual_tool_map",
    "build_snapshot_tool_env",
    "build_tool_defs",
    "build_tool_path_cfg_keys",
    "get_tool_spec",
    "iter_external_tool_specs",
]
