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
import re
from typing import Any

import pydantic
from pydantic import field_validator

from sky_claw.config import SystemPaths

from .xedit_policy import XEditAgentScriptName

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


# `ProfileParams` y `LaunchGameParams` vivían acá y exponían el perfil MO2 como
# argumento del tool. Se eliminaron: el perfil queda FIJO durante la sesión (spec del
# PR #454) y el registry lo inyecta desde `AsyncToolRegistry(mo2_profile=…)`, así que
# devolvérselo al LLM como parámetro contradecía el contrato y era la vía por la que
# `check_load_order`/`detect_conflicts`/`run_loot_sort`/`launch_game` terminaban
# operando sobre otro perfil. Los tools que perdieron su único campo ya no declaran
# `params_model`. La validación del nombre (antes el `pattern` de estos modelos) vive
# ahora en `AsyncToolRegistry.__init__` vía `assert_safe_component`, que es el único
# punto por el que el perfil entra. Ancla: `tests/test_perfil_sesion_invariante.py`.


class InstallModParams(pydantic.BaseModel):
    """Parameters for the ``install_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    nexus_id: int = pydantic.Field(gt=0)
    version: str = pydantic.Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.\-]+$")


class XEditAnalysisParams(pydantic.BaseModel):
    """Parámetros del tool LLM ``run_xedit_script``."""

    model_config = pydantic.ConfigDict(strict=True)

    # El Literal es la misma fuente de verdad que consume el handler: el schema
    # anuncia exactamente las capacidades que la ejecución acepta.
    script_name: XEditAgentScriptName
    # SEGURIDAD: limita el tamaño del argv que terminará recibiendo SSEEdit.exe.
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
        default_factory=lambda: ["loot", "xedit", "pandora", "bodyslide", "ngio"],
        description=(
            "List of tools to install. Supported: 'loot', 'xedit', 'pandora', 'bodyslide', "
            "'ngio' (dependencias del precache de grass: NGIO-NG + Address Library + "
            "Grass Cache Helper NG en AE), 'community_shaders' (Community Shaders core + "
            "Address Library + SSE Engine Fixes; requiere Skyrim SE 1.5.97 o AE 1.6.1170+, "
            "SKSE instalado, sin ENB, y el preloader de Engine Fixes en la raíz del juego). "
            "'community_shaders' NO está en el default: sus prerrequisitos no los puede "
            "satisfacer Sky-Claw solo — se despacha por nombre explícito."
        ),
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

    plugins: list[str] | None = pydantic.Field(
        default=None,
        description="Specific plugins to analyze. If omitted, uses all enabled plugins from the profile.",
    )


# `ModNameParams` también vivía acá. Además de exponer `profile`, era código muerto:
# estaba exportado en `__all__` y en el facade, pero ningún `ToolDescriptor` lo
# registraba. Se eliminó junto con el resto de la familia.


class ToggleModParams(pydantic.BaseModel):
    """Parameters for toggling a mod.

    PR #141 review fix: relaxed `mod_name` pattern to allow real Nexus mod
    names like ``powerofthree's Tweaks`` and ``Mod (SE)``.
    """

    model_config = pydantic.ConfigDict(strict=True)

    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=_EXISTING_MOD_NAME_PATTERN)
    enable: bool


class BodySlideBatchParams(pydantic.BaseModel):
    """Parameters for the ``run_bodyslide`` tool (M-03 BodySlideRunner)."""

    model_config = pydantic.ConfigDict(strict=True)

    group: str = pydantic.Field(
        default="CBBE",
        min_length=1,
        max_length=128,
        pattern=_SAFE_NAME_PATTERN,
        description="BodySlide preset group name.",
    )
    output_path: str = pydantic.Field(
        default="meshes",
        min_length=1,
        max_length=256,
        description=(
            "Legacy hint for where generated meshes should conceptually land. U-04: no longer the "
            "literal write destination — Sky-Claw manages BodySlide's physical output as an isolated "
            "per-group directory (see output_targets.bodyslide_output_target) so a directory-level "
            "rollback can never touch the shared Data/meshes tree that other mods write to."
        ),
    )
    preset: str | None = pydantic.Field(
        default=None,
        description=(
            "BodySlide preset to build with (forwarded as -p). Omitted from the command when null, "
            "which keeps BodySlide's documented default ('last used preset'). Use 'Zeroed Sliders' "
            "when the meshes will be morphed at runtime by OBody NG / AutoBody, so a static "
            "deformation is not baked in on top of the dynamic one."
        ),
    )
    build_morphs: bool = pydantic.Field(
        default=True,
        description=(
            "Emit .tri morph files (forwarded as -tri). On by default: without it BodySlide builds "
            "meshes with no morph data, which OBody NG / RaceMenu need to deform bodies at runtime."
        ),
    )

    @field_validator("preset")
    @classmethod
    def _preset_es_un_nombre_seguro(cls, v: str | None) -> str | None:
        """Mismo vocabulario que ``group``, aplicado por validador y no por ``pattern``.

        Pydantic v2 rechaza aplicar una constraint de string sobre un tipo unión
        (``str | None``), así que el chequeo se hace acá en vez de en el ``Field``.
        No es sanitización de shell —``create_subprocess_exec`` no pasa por shell—
        sino el mismo criterio de nombre razonable que ya rige para ``group``.
        """
        if v is None:
            return None
        if not (1 <= len(v) <= 128) or not re.fullmatch(_SAFE_NAME_PATTERN, v):
            raise ValueError("preset must be a safe name (letters, digits, spaces, '_', '.', '-'), 1-128 chars")
        return v

    @field_validator("output_path")
    @classmethod
    def _output_path_stays_relative(cls, v: str) -> str:
        """Reject absolute / drive-anchored / traversal output paths.

        PR #171 review (Codex P1): ``output_path`` is forwarded to
        ``BodySlide.exe -o`` with the game directory as cwd. An absolute
        path, a drive-relative path (``C:evil``), a UNC share, or ``..``
        segments would direct generated meshes outside the sandbox.

        U-04: el valor ya no llega literal a ``-o`` (ver el ``description``
        del campo), pero la validación se conserva sin cambios — no hay
        motivo para relajar un chequeo de sandboxing existente sólo porque
        cambió el consumidor.
        """
        candidate = pathlib.PureWindowsPath(v)
        if candidate.is_absolute() or candidate.drive or v.startswith(("/", "\\")):
            raise ValueError("output_path must be a relative path (no absolute paths, drive letters, or UNC shares)")
        if ".." in candidate.parts:
            raise ValueError("output_path must not contain '..' traversal segments")
        return v

    @field_validator("group")
    @classmethod
    def _group_is_not_a_bare_dot_segment(cls, v: str) -> str:
        """Reject ``"."`` / ``".."`` as the full value.

        U-04: ``group`` pasa a ser el componente único de ruta bajo
        ``BodySlide_Output/<group>`` (``output_targets.bodyslide_output_target``).
        ``_SAFE_NAME_PATTERN`` ya bloquea separadores de ruta, así que ningún
        valor puede formar MÁS de un componente — pero sí permite ``.``
        (nombres reales como "Preset 1.0"), y un valor de solo puntos es
        exactamente el componente especial que el filesystem resuelve como
        "acá mismo"/"un nivel arriba". Mismo chequeo que ``output_path`` ya
        aplica para ``..`` (PR #171), acá para un campo que antes no tocaba
        el filesystem.
        """
        if v in {".", ".."}:
            raise ValueError("group must not be a bare '.' or '..' path segment")
        return v


class UninstallModParams(pydantic.BaseModel):
    """Parameters for the ``uninstall_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")


__all__ = [
    "AnalyzeConflictsParams",
    "BodySlideBatchParams",
    "DownloadModParams",
    "InstallFromArchiveParams",
    "InstallModParams",
    "PreviewInstallerParams",
    "ResolveFomodParams",
    "SearchModParams",
    "SearchNexusParams",
    "SetupToolsParams",
    "ToggleModParams",
    "UninstallModParams",
    "XEditAnalysisParams",
    "_clean_schema",
]
