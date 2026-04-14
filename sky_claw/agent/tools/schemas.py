"""Modelos Pydantic para validación de parámetros de herramientas.

Este módulo contiene todos los esquemas de validación para las herramientas del agente.
Extraído de tools.py como parte de la refactorización M-13.
"""

from __future__ import annotations

import pathlib

import pydantic
from pydantic import field_validator

from sky_claw.config import SystemPaths

# HOTFIX: Sandbox directories for path validation
ALLOWED_SANDBOX_DIRS = [
    SystemPaths.modding_root().resolve(),
    pathlib.Path.home() / "Modding",
]


def _validate_sandbox_path(v: str) -> str:
    """Validate that path is within allowed sandbox directories.

    SECURITY: Prevents path traversal attacks by ensuring the resolved
    path starts with an allowed base directory.
    """
    try:
        resolved = pathlib.Path(v).resolve()
    except Exception as exc:
        raise ValueError(f"Invalid path format: {exc}")

    for allowed_dir in ALLOWED_SANDBOX_DIRS:
        try:
            resolved.relative_to(allowed_dir)
            return str(resolved)  # Return canonical path
        except ValueError:
            continue

    raise ValueError(
        f"Path traversal blocked: '{v}' is outside allowed sandbox directories"
    )


class SearchModParams(pydantic.BaseModel):
    """Parameters for the ``search_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    mod_name: str = pydantic.Field(
        min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$"
    )


class ProfileParams(pydantic.BaseModel):
    """Parameters for tools that operate on an MO2 profile."""

    model_config = pydantic.ConfigDict(strict=True)

    profile: str = pydantic.Field(
        min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$"
    )


class InstallModParams(pydantic.BaseModel):
    """Parameters for the ``install_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    nexus_id: int = pydantic.Field(gt=0)
    version: str = pydantic.Field(
        min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.\-]+$"
    )


class XEditAnalysisParams(pydantic.BaseModel):
    """Parameters for the ``run_xedit_analysis`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    script_name: str = pydantic.Field(
        min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_\-]+\.pas$"
    )
    plugins: list[str] = pydantic.Field(min_length=1)


class DownloadModParams(pydantic.BaseModel):
    """Parameters for the ``download_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    nexus_id: int = pydantic.Field(gt=0)
    file_id: int | None = pydantic.Field(None, gt=0)


class PreviewInstallerParams(pydantic.BaseModel):
    """Parameters for the ``preview_mod_installer`` tool.

    SECURITY: Uses sandbox path validation instead of weak regex.
    """

    model_config = pydantic.ConfigDict(strict=True)

    archive_path: str = pydantic.Field(min_length=1, max_length=512)

    @field_validator("archive_path")
    @classmethod
    def validate_archive_path(cls, v: str) -> str:
        return _validate_sandbox_path(v)


class InstallFromArchiveParams(pydantic.BaseModel):
    """Parameters for the ``install_mod_from_archive`` tool.

    SECURITY: Uses sandbox path validation instead of weak regex.
    """

    model_config = pydantic.ConfigDict(strict=True)

    archive_path: str = pydantic.Field(min_length=1, max_length=512)

    @field_validator("archive_path")
    @classmethod
    def validate_archive_path(cls, v: str) -> str:
        return _validate_sandbox_path(v)

    selections: dict[str, list[str]] = pydantic.Field(default_factory=dict)


class ResolveFomodParams(pydantic.BaseModel):
    """Parameters for the ``resolve_fomod`` tool.

    SECURITY: Uses sandbox path validation instead of weak regex.
    """

    model_config = pydantic.ConfigDict(strict=True)

    archive_path: str = pydantic.Field(min_length=1, max_length=512)

    @field_validator("archive_path")
    @classmethod
    def validate_archive_path(cls, v: str) -> str:
        return _validate_sandbox_path(v)

    selections: dict[str, list[str]] = pydantic.Field(default_factory=dict)


class SetupToolsParams(pydantic.BaseModel):
    """Parameters for the ``setup_tools`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    tools: list[str] = pydantic.Field(
        default_factory=lambda: ["loot", "xedit", "pandora", "bodyslide"],
        description="List of tools to install. Supported: 'loot', 'xedit', 'pandora', 'bodyslide'.",
    )


class AnalyzeConflictsParams(pydantic.BaseModel):
    """Parameters for the ``analyze_esp_conflicts`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    profile: str = pydantic.Field(
        min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$"
    )
    plugins: list[str] | None = pydantic.Field(
        default=None,
        description="Specific plugins to analyze. If omitted, uses all enabled plugins from the profile.",
    )


class ModNameParams(pydantic.BaseModel):
    """Parameters for tools specifying a mod name."""

    model_config = pydantic.ConfigDict(strict=True)
    mod_name: str = pydantic.Field(
        min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$"
    )
    profile: str = pydantic.Field(
        default="Default", pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$"
    )


class ToggleModParams(pydantic.BaseModel):
    """Parameters for toggling a mod."""

    model_config = pydantic.ConfigDict(strict=True)
    mod_name: str = pydantic.Field(
        min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$"
    )
    enable: bool
    profile: str = pydantic.Field(default="Default", pattern=r"^[a-zA-Z0-9_. \-]+$")


__all__ = [
    "AnalyzeConflictsParams",
    "DownloadModParams",
    "InstallFromArchiveParams",
    "InstallModParams",
    "ModNameParams",
    "PreviewInstallerParams",
    "ProfileParams",
    "ResolveFomodParams",
    "SearchModParams",
    "SetupToolsParams",
    "ToggleModParams",
    "XEditAnalysisParams",
]
