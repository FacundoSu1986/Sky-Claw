"""Modelos Pydantic para validación de parámetros de herramientas.

Este módulo contiene todos los esquemas de validación para las herramientas del agente.
Extraído de tools.py como parte de la refactorización M-13.

TASK-011 Single Source of Truth:
- All tool parameter models use ConfigDict(strict=True).
- ``_clean_schema()`` sanitizes Pydantic JSON schemas for LLM APIs.
- ``model_json_schema()`` is the single source for ``input_schema``.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pydantic
from pydantic import field_validator

from sky_claw.config import SystemPaths

# HOTFIX: Sandbox directories for path validation
ALLOWED_SANDBOX_DIRS = [
    SystemPaths.modding_root().resolve(),
    pathlib.Path.home() / "Modding",
]

# ---------------------------------------------------------------------------
# Schema sanitization for LLM tool-use APIs
# ---------------------------------------------------------------------------

# Keys that Anthropic/OpenAI tool-use APIs reject or ignore.
_REJECTED_KEYS = frozenset({"title", "$defs"})


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a Pydantic ``model_json_schema()`` for LLM tool-use APIs.

    Removes metadata fields that Anthropic and OpenAI reject or ignore,
    such as ``title`` and ``$defs``.  Recursively cleans nested dicts
    and lists so the final schema is clean at every depth.

    Args:
        schema: Raw output of ``SomeModel.model_json_schema()``.

    Returns:
        A cleaned schema safe for ``tools[].input_schema``.
    """

    def _clean(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items() if k not in _REJECTED_KEYS}
        if isinstance(obj, list):
            return [_clean(item) for item in obj]
        return obj

    return _clean(schema)


# ---------------------------------------------------------------------------
# Path validation helper
# ---------------------------------------------------------------------------


def _validate_sandbox_path(v: str) -> str:
    """Validate that path is within allowed sandbox directories.

    SECURITY: Prevents path traversal attacks by ensuring the resolved
    path starts with an allowed base directory.
    """
    try:
        resolved = pathlib.Path(v).resolve()
    except Exception as exc:
        raise ValueError(f"Invalid path format: {exc}") from exc

    for allowed_dir in ALLOWED_SANDBOX_DIRS:
        try:
            resolved.relative_to(allowed_dir)
            return str(resolved)  # Return canonical path
        except ValueError:
            continue

    raise ValueError(f"Path traversal blocked: '{v}' is outside allowed sandbox directories")


# ---------------------------------------------------------------------------
# Pydantic parameter models (all strict=True)
# ---------------------------------------------------------------------------


class SearchModParams(pydantic.BaseModel):
    """Parameters for the ``search_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")


class ProfileParams(pydantic.BaseModel):
    """Parameters for tools that operate on an MO2 profile."""

    model_config = pydantic.ConfigDict(strict=True)

    # SECURITY: Tightened pattern — removed '%()\[\] to prevent argument injection
    # into LOOT CLI (loot.exe --game-path ...).  Spaces and dots are still valid
    # because MO2 profile names frequently contain them.
    profile: str = pydantic.Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.\- ]+$")


class LaunchGameParams(pydantic.BaseModel):
    """Parameters for the ``launch_game`` tool.

    Separate from ProfileParams because ``profile`` has a default value.
    """

    model_config = pydantic.ConfigDict(strict=True)

    profile: str = pydantic.Field(default="Default", min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.\- ]+$")


class InstallModParams(pydantic.BaseModel):
    """Parameters for the ``install_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    nexus_id: int = pydantic.Field(gt=0)
    version: str = pydantic.Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.\-]+$")


class XEditAnalysisParams(pydantic.BaseModel):
    """Parameters for the ``run_xedit_analysis`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    script_name: str = pydantic.Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_\-]+\.pas$")
    # SECURITY: max_length=50 prevents DoS via oversized plugin lists that
    # would generate a command line exceeding Windows MAX_PATH limits for SSEEdit.exe.
    plugins: list[str] = pydantic.Field(min_length=1, max_length=50)


class DownloadModParams(pydantic.BaseModel):
    """Parameters for the ``download_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    nexus_id: int = pydantic.Field(gt=0)
    file_id: int | None = pydantic.Field(None, gt=0)


class SearchNexusParams(pydantic.BaseModel):
    """Parameters for the ``search_nexus`` tool (read-only Nexus discovery)."""

    model_config = pydantic.ConfigDict(strict=True)

    # Free-text query OR a Nexus mod URL/ID. Relaxed charset so the LLM can
    # pass real mod names and full URLs; this value is never used as a shell
    # arg or a path — it is URL-encoded into a Brave query and sanitized.
    query: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")
    min_downloads: int | None = pydantic.Field(default=None, ge=0)
    limit: int = pydantic.Field(default=5, ge=1, le=10)


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


# T2-03 — pattern restrictivo para profiles elegidos por el LLM.
# Caracteres permitidos: alfanumericos (A-Z, a-z, 0-9), underscore (_),
# punto (.), guion (-) y espacio. Antes permitiamos `'%()[]` lo que abria
# chains de injection contextual (directorios con esos caracteres rompen
# parsers de loadorder.txt o argumentos de subprocess de LOOT/xEdit). La UI
# humana puede usar otro path para profiles con caracteres especiales (esos
# no son LLM-controlled).
_SAFE_NAME_PATTERN = r"^[a-zA-Z0-9_.\- ]+$"

# PR #141 review fix: para mod_name de mods EXISTENTES (toggle/uninstall),
# el LLM tiene que poder hablar de nombres reales de Nexus que contienen
# apostrofos y parentesis: "powerofthree's Tweaks", "Mod Name (SE)", etc.
# El pattern original `_SAFE_NAME_PATTERN` rechazaba esos mods validos.
# Para esos casos usamos el pattern menos restrictivo (mismo que el path
# component validator `assert_safe_component` usa) — rechaza solo
# separadores de path, control chars y caracteres null. La proteccion real
# contra shell injection esta en los handlers (subprocess sin shell=True,
# args como lista). Aqui solo evitamos cosas que rompan path resolution.
_EXISTING_MOD_NAME_PATTERN = r"^[^/\\:*?\"<>|\x00-\x1f]+$"


class AnalyzeConflictsParams(pydantic.BaseModel):
    """Parameters for the ``analyze_esp_conflicts`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    profile: str = pydantic.Field(min_length=1, max_length=128, pattern=_SAFE_NAME_PATTERN)
    plugins: list[str] | None = pydantic.Field(
        default=None,
        description="Specific plugins to analyze. If omitted, uses all enabled plugins from the profile.",
    )


class ModNameParams(pydantic.BaseModel):
    """Parameters for tools specifying an EXISTING mod name (read-only ops).

    Uses the relaxed ``_EXISTING_MOD_NAME_PATTERN`` because real Nexus mods
    have apostrophes / parens that ``_SAFE_NAME_PATTERN`` would reject.
    """

    model_config = pydantic.ConfigDict(strict=True)

    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=_EXISTING_MOD_NAME_PATTERN)
    profile: str = pydantic.Field(default="Default", pattern=_SAFE_NAME_PATTERN)


class ToggleModParams(pydantic.BaseModel):
    """Parameters for toggling a mod.

    PR #141 review fix: relaxed `mod_name` pattern to allow real Nexus mod
    names like ``powerofthree's Tweaks`` and ``Mod (SE)``. Profile sigue
    siendo `_SAFE_NAME_PATTERN` (esos vienen de la UI humana en MO2).
    """

    model_config = pydantic.ConfigDict(strict=True)

    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=_EXISTING_MOD_NAME_PATTERN)
    enable: bool
    profile: str = pydantic.Field(default="Default", pattern=_SAFE_NAME_PATTERN)


class BodySlideBatchParams(pydantic.BaseModel):
    """Parameters for the ``run_bodyslide`` tool (M-03 BodySlideRunner)."""

    model_config = pydantic.ConfigDict(strict=True)

    group: str = pydantic.Field(
        default="CBBE",
        min_length=1,
        max_length=128,
        description="BodySlide preset group name.",
    )
    output_path: str = pydantic.Field(
        default="meshes",
        min_length=1,
        max_length=256,
        description="Output directory for generated meshes, relative to the game directory.",
    )

    @field_validator("output_path")
    @classmethod
    def _output_path_stays_relative(cls, v: str) -> str:
        """Reject absolute / drive-anchored / traversal output paths.

        PR #171 review (Codex P1): ``output_path`` is forwarded to
        ``BodySlide.exe -o`` with the game directory as cwd. An absolute
        path, a drive-relative path (``C:evil``), a UNC share, or ``..``
        segments would direct generated meshes outside the sandbox.
        """
        candidate = pathlib.PureWindowsPath(v)
        if candidate.is_absolute() or candidate.drive or v.startswith(("/", "\\")):
            raise ValueError("output_path must be a relative path (no absolute paths, drive letters, or UNC shares)")
        if ".." in candidate.parts:
            raise ValueError("output_path must not contain '..' traversal segments")
        return v


class UninstallModParams(pydantic.BaseModel):
    """Parameters for the ``uninstall_mod`` tool.

    Separate from ModNameParams to decouple tool-specific evolution.
    """

    model_config = pydantic.ConfigDict(strict=True)

    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")
    profile: str = pydantic.Field(default="Default", pattern=r"^[a-zA-Z0-9_.\- ]+$")


__all__ = [
    "AnalyzeConflictsParams",
    "BodySlideBatchParams",
    "DownloadModParams",
    "InstallFromArchiveParams",
    "InstallModParams",
    "LaunchGameParams",
    "ModNameParams",
    "PreviewInstallerParams",
    "ProfileParams",
    "ResolveFomodParams",
    "SearchModParams",
    "SearchNexusParams",
    "SetupToolsParams",
    "ToggleModParams",
    "UninstallModParams",
    "XEditAnalysisParams",
    "_clean_schema",
]
