"""Async tool registry and tool implementations for the Sky-Claw agent.

FACADE: Este archivo re-exporta todos los componentes del paquete tools/
para mantener compatibilidad con importaciones existentes.

Cada tool valida sus parámetros con Pydantic v2 antes de tocar
la base de datos o filesystem y retorna un JSON-encoded ``str`` para
compatibilidad con el API ``tool_use`` de Anthropic.

Refactorización M-13: Separation of Concerns.
"""

from __future__ import annotations

# Re-exportar todo desde el paquete modular
from sky_claw.agent.tools import (
    # Clase principal
    AsyncToolRegistry,
    # Descriptor
    ToolDescriptor,
    # Schemas
    SearchModParams,
    ProfileParams,
    InstallModParams,
    XEditAnalysisParams,
    DownloadModParams,
    PreviewInstallerParams,
    InstallFromArchiveParams,
    ResolveFomodParams,
    SetupToolsParams,
    AnalyzeConflictsParams,
    ModNameParams,
    ToggleModParams,
    # Handlers - DB
    search_mod,
    install_mod,
    # Handlers - Nexus
    download_mod,
    # Handlers - System
    check_load_order,
    detect_conflicts,
    run_loot_sort,
    run_xedit_script,
    preview_mod_installer,
    install_mod_from_archive,
    resolve_fomod,
    analyze_esp_conflicts,
    run_pandora,
    run_bodyslide_batch,
    uninstall_mod,
    toggle_mod,
    launch_game,
    close_game,
    # Handlers - External
    setup_tools,
)

__all__ = [
    # Clase principal
    "AsyncToolRegistry",
    # Descriptor
    "ToolDescriptor",
    # Schemas (re-exports)
    "SearchModParams",
    "ProfileParams",
    "InstallModParams",
    "XEditAnalysisParams",
    "DownloadModParams",
    "PreviewInstallerParams",
    "InstallFromArchiveParams",
    "ResolveFomodParams",
    "SetupToolsParams",
    "AnalyzeConflictsParams",
    "ModNameParams",
    "ToggleModParams",
    # Handlers - DB
    "search_mod",
    "install_mod",
    # Handlers - Nexus
    "download_mod",
    # Handlers - System
    "check_load_order",
    "detect_conflicts",
    "run_loot_sort",
    "run_xedit_script",
    "preview_mod_installer",
    "install_mod_from_archive",
    "resolve_fomod",
    "analyze_esp_conflicts",
    "run_pandora",
    "run_bodyslide_batch",
    "uninstall_mod",
    "toggle_mod",
    "launch_game",
    "close_game",
    # Handlers - External
    "setup_tools",
]
