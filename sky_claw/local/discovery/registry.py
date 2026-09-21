"""Registro canónico y declarativo para herramientas externas.

Este módulo es la única fuente de verdad para las herramientas externas detectadas,
gestionadas o integradas por Sky-Claw. Proporciona especificaciones inmutables y
proyecciones consumidas por el scanner, los controladores de rituales en la GUI y el bootloader.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class VersionProbeKind(StrEnum):
    """Mecanismo declarativo de inspección de versión para una herramienta externa."""

    NONE = "none"
    LOOT_CLI = "loot_cli"
    PE_PRODUCT_VERSION = "pe_product_version"


@dataclass(frozen=True, slots=True)
class ExternalToolSpec:
    """Especificación declarativa para una herramienta externa.

    Contiene únicamente los metadatos necesarios para escanear, despachar o instalar
    la herramienta, sin retener estado vivo del scanner, manejadores de subprocesos
    ni resultados de ejecución.
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
    version_probe_kind: VersionProbeKind = VersionProbeKind.NONE

    @property
    def friendly_description(self) -> str:
        """Alias descriptivo de cara al usuario para la representación en MissingTool."""
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
        version_probe_kind=VersionProbeKind.LOOT_CLI,
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
        version_probe_kind=VersionProbeKind.PE_PRODUCT_VERSION,
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
    """Obtiene la especificación de una herramienta externa por su clave, o ``None``."""
    return EXTERNAL_TOOL_REGISTRY.get(key)


def iter_external_tool_specs() -> tuple[ExternalToolSpec, ...]:
    """Itera todas las especificaciones de herramientas declaradas en orden canónico."""
    return tuple(EXTERNAL_TOOL_REGISTRY.values())


def build_tool_defs() -> tuple[tuple[str, tuple[str, ...], str, str, bool], ...]:
    """Proyección compatible con la estructura histórica ``tool_defs`` de ``scanner.py``.

    Produce tuplas de ``(key, exe_names, friendly_action, download_url, is_critical)``.
    """
    return tuple(
        (spec.key, spec.exe_names, spec.friendly_action, spec.download_url, spec.is_critical)
        for spec in EXTERNAL_TOOL_REGISTRY.values()
    )


def build_ritual_tool_map() -> dict[str, str]:
    """Proyección que asocia claves de herramientas con sus nombres de estrategia dispatcher.

    Deriva ``RITUAL_TOOL_MAP`` para ``ritual_runner.py``.
    """
    return {
        spec.key: spec.dispatcher_tool_name
        for spec in EXTERNAL_TOOL_REGISTRY.values()
        if spec.dispatcher_tool_name is not None
    }


def build_ritual_installer_map() -> dict[str, str]:
    """Proyección que asocia claves de herramientas con métodos de instalación en ``ToolsInstaller``.

    Deriva ``RITUAL_INSTALLER_MAP`` para ``ritual_runner.py``.
    """
    return {
        spec.key: spec.installer_method for spec in EXTERNAL_TOOL_REGISTRY.values() if spec.installer_method is not None
    }


def build_ritual_install_env() -> dict[str, str]:
    """Proyección que asocia claves de herramientas con variables de entorno tras instalación.

    Deriva ``RITUAL_INSTALL_ENV`` para ``ritual_runner.py``.
    """
    return {
        spec.key: spec.install_env_var for spec in EXTERNAL_TOOL_REGISTRY.values() if spec.install_env_var is not None
    }


def build_snapshot_tool_env() -> dict[str, str]:
    """Proyección que asocia claves de herramientas con variables de entorno leídas por ``PathResolutionService``.

    Deriva ``_SNAPSHOT_TOOL_ENV`` para ``_bootloader.py``.
    """
    return {
        spec.key: spec.snapshot_env_var for spec in EXTERNAL_TOOL_REGISTRY.values() if spec.snapshot_env_var is not None
    }


def build_tool_path_cfg_keys() -> dict[str, str]:
    """Proyección que asocia claves de herramientas con su campo de ruta en ``config.toml``.

    Deriva ``tool_path_cfg_keys`` para ``_build_environment_scanner()`` en ``_bootloader.py``.
    """
    return {spec.key: spec.config_field for spec in EXTERNAL_TOOL_REGISTRY.values() if spec.config_field is not None}


__all__ = [
    "EXTERNAL_TOOL_REGISTRY",
    "ExternalToolSpec",
    "VersionProbeKind",
    "build_ritual_install_env",
    "build_ritual_installer_map",
    "build_ritual_tool_map",
    "build_snapshot_tool_env",
    "build_tool_defs",
    "build_tool_path_cfg_keys",
    "get_tool_spec",
    "iter_external_tool_specs",
]
