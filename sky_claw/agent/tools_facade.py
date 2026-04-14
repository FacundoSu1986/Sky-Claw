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
    AnalyzeConflictsParams,
    # Clase principal
    AsyncToolRegistry,
    DownloadModParams,
    InstallFromArchiveParams,
    InstallModParams,
    ModNameParams,
    PreviewInstallerParams,
    ProfileParams,
    ResolveFomodParams,
    # Schemas
    SearchModParams,
    SetupToolsParams,
    ToggleModParams,
    # Descriptor
    ToolDescriptor,
    XEditAnalysisParams,
    analyze_esp_conflicts,
    # Handlers - System
    check_load_order,
    close_game,
    detect_conflicts,
    # Handlers - Nexus
    download_mod,
    install_mod,
    install_mod_from_archive,
    launch_game,
    preview_mod_installer,
    resolve_fomod,
    run_bodyslide_batch,
    run_loot_sort,
    run_pandora,
    run_xedit_script,
    # Handlers - DB
    search_mod,
    # Handlers - External
    setup_tools,
    toggle_mod,
    uninstall_mod,
)

__all__ = [
    "AnalyzeConflictsParams",
    # Clase principal
    "AsyncToolRegistry",
    "DownloadModParams",
    "InstallFromArchiveParams",
    "InstallModParams",
    "ModNameParams",
    "PreviewInstallerParams",
    "ProfileParams",
    "ResolveFomodParams",
    # Schemas (re-exports)
    "SearchModParams",
    "SetupToolsParams",
    "ToggleModParams",
    # Descriptor
    "ToolDescriptor",
    "XEditAnalysisParams",
    "analyze_esp_conflicts",
    # Handlers - System
    "check_load_order",
    "close_game",
    "detect_conflicts",
    # Handlers - Nexus
    "download_mod",
    "install_mod",
    "install_mod_from_archive",
    "launch_game",
    "preview_mod_installer",
    "resolve_fomod",
    "run_bodyslide_batch",
    "run_loot_sort",
    "run_pandora",
    "run_xedit_script",
    # Handlers - DB
    "search_mod",
    # Handlers - External
    "setup_tools",
    "toggle_mod",
    "uninstall_mod",
]
