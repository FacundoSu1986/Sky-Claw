"""Paquete de herramientas modulares para Sky-Claw agent.

FACADE: Este archivo actúa como el punto de entrada oficial (Facade)
que expone AsyncToolRegistry y todos los esquemas y handlers.

Extraído de tools.py como parte de la refactorización M-13.
TASK-012: validación Pydantic strict centralizada en ``execute`` y
``input_schema`` derivado de ``model_json_schema`` (single source of truth).
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

import pydantic

from sky_claw.config import SystemPaths
from sky_claw.loot.cli import LOOTConfig, LOOTRunner

from .db_tools import install_mod, search_mod
from .descriptor import ToolDescriptor
from .external_tools import setup_tools
from .nexus_tools import download_mod
from .schemas import (
    AnalyzeConflictsParams,
    DownloadModParams,
    InstallFromArchiveParams,
    InstallModParams,
    ModNameParams,
    PreviewInstallerParams,
    ProfileParams,
    ResolveFomodParams,
    SearchModParams,
    SetupToolsParams,
    ToggleModParams,
    XEditAnalysisParams,
)
from .system_tools import (
    analyze_esp_conflicts,
    check_load_order,
    close_game,
    detect_conflicts,
    generate_bashed_patch,
    install_mod_from_archive,
    launch_game,
    preview_mod_installer,
    resolve_fomod,
    run_bodyslide_batch,
    run_bodyslide_batch_direct,
    run_loot_sort,
    run_pandora,
    run_pandora_behavior,
    run_xedit_script,
    toggle_mod,
    uninstall_mod,
)

logger = logging.getLogger(__name__)


def _clean_schema(model_cls: type[pydantic.BaseModel]) -> dict[str, Any]:
    """Generate a JSON Schema from a Pydantic model and clean it for the Anthropic Tool API.

    Pydantic's ``model_json_schema()`` produces additional metadata keys
    (``title``, ``$defs``, etc.) that are either redundant for the LLM
    (the tool already has a ``name`` field) or, in some cases, rejected
    with HTTP 400 by stricter providers. This helper:

    * removes the root-level ``title`` (the tool name supplies that),
    * removes per-property ``title`` keys (cosmetic, can mislead the LLM),
    * preserves ``$defs``/``$ref`` when present (Anthropic accepts them).
    """
    schema = model_cls.model_json_schema()
    schema.pop("title", None)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop_schema in properties.values():
            if isinstance(prop_schema, dict):
                prop_schema.pop("title", None)
    return schema


class AsyncToolRegistry:
    """Registry and executor for async agent tools.

    Fábrica de herramientas que delega a los handlers modulares.
    Mantiene compatibilidad total con la implementación original.
    """

    def __init__(
        self,
        registry: Any,
        mo2: Any,
        sync_engine: Any,
        loot_exe: pathlib.Path | None = None,
        xedit_runner: Any | None = None,
        loot_runner: Any | None = None,
        hitl: Any | None = None,
        downloader: Any | None = None,
        fomod_installer: Any | None = None,
        tools_installer: Any | None = None,
        install_dir: pathlib.Path | None = None,
        animation_hub: Any | None = None,
        local_cfg: Any | None = None,
        config_path: pathlib.Path | None = None,
        wrye_bash_runner: Any | None = None,
        pandora_runner: Any | None = None,
        bodyslide_runner: Any | None = None,
    ) -> None:
        self._registry = registry
        self._mo2 = mo2
        self._sync_engine = sync_engine
        self._loot_exe = loot_exe or pathlib.Path("loot.exe")
        self._xedit_runner = xedit_runner
        self._loot_runner = loot_runner
        self._hitl = hitl
        self._downloader = downloader
        self._fomod_installer = fomod_installer
        self._tools_installer = tools_installer
        self._install_dir = install_dir or SystemPaths.modding_root()
        self._animation_hub = animation_hub
        self._local_cfg = local_cfg
        self._config_path = config_path
        self._wrye_bash_runner = wrye_bash_runner
        self._pandora_runner = pandora_runner
        self._bodyslide_runner = bodyslide_runner
        self._tools: dict[str, ToolDescriptor] = {}
        self._register_builtins()

    @property
    def tools(self) -> dict[str, ToolDescriptor]:
        return dict(self._tools)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": td.name,
                "description": td.description,
                "input_schema": td.input_schema,
            }
            for td in self._tools.values()
        ]

    def hermes_system_prompt_block(self) -> str:
        """Serialize registered tools as a <tools>…</tools> XML block for Hermes-style prompting."""
        schemas = [
            {
                "name": td.name,
                "description": td.description,
                "parameters": td.input_schema,
            }
            for td in self._tools.values()
        ]
        return f"<tools>\n{json.dumps(schemas, indent=2, ensure_ascii=False)}\n</tools>"

    async def _download_mod(self, nexus_id: int, file_id: int | None = None) -> str:
        """Download a mod with HITL approval (P0-2: re-fetches fresh URL on execute)."""
        return await download_mod(self._downloader, self._hitl, self._sync_engine, nexus_id, file_id)

    async def _run_loot_sort(self, profile: str) -> str:
        """Run LOOT sort, auto-initializing LOOTRunner from loot_exe if needed."""
        runner = self._loot_runner
        if runner is None and self._loot_exe is not None:
            try:
                config = LOOTConfig(loot_exe=self._loot_exe, game_path=self._mo2.root)
                runner = LOOTRunner(config)
            except Exception as exc:
                return json.dumps({"error": str(exc)})
        if runner is None:
            return json.dumps({"error": "LOOT runner is not configured"})
        try:
            result = await runner.sort()
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(
            {
                "profile": profile,
                "success": result.success,
                "return_code": result.return_code,
                "sorted_plugins": result.sorted_plugins,
                "warnings": result.warnings,
                "errors": result.errors,
            }
        )

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a registered tool.

        TASK-012: When the descriptor declares ``params_model`` the
        arguments dict (which originates from an LLM and is therefore
        untrusted) is validated through Pydantic strict mode BEFORE
        the handler is invoked. Any ``pydantic.ValidationError`` raised
        here propagates to ``LLMRouter.chat`` which formats it as
        constructive feedback for the model to retry.
        """
        td = self._tools.get(name)
        if td is None:
            raise KeyError(f"Unknown tool: {name!r}")
        if td.params_model is not None:
            validated = td.params_model(**arguments)
            return await td.fn(**validated.model_dump())
        return await td.fn(**arguments)

    def _register_builtins(self) -> None:
        # DB
        self._tools["search_mod"] = ToolDescriptor(
            name="search_mod",
            description="Search the local mod registry by name.",
            input_schema=_clean_schema(SearchModParams),
            fn=lambda mod_name: search_mod(self._registry, mod_name),
            params_model=SearchModParams,
        )
        self._tools["install_mod"] = ToolDescriptor(
            name="install_mod",
            description="Register a mod in the database via the SyncEngine.",
            input_schema=_clean_schema(InstallModParams),
            fn=lambda nexus_id, version: install_mod(self._registry, nexus_id, version),
            params_model=InstallModParams,
        )
        # Nexus
        self._tools["download_mod"] = ToolDescriptor(
            name="download_mod",
            description="Download a file from Nexus Mods with HITL approval.",
            input_schema=_clean_schema(DownloadModParams),
            fn=lambda nexus_id, file_id=None: download_mod(
                self._downloader, self._hitl, self._sync_engine, nexus_id, file_id
            ),
            params_model=DownloadModParams,
        )
        # System
        self._tools["check_load_order"] = ToolDescriptor(
            name="check_load_order",
            description="Read the MO2 modlist for a profile.",
            input_schema=_clean_schema(ProfileParams),
            fn=lambda profile: check_load_order(self._mo2, profile),
            params_model=ProfileParams,
        )
        self._tools["detect_conflicts"] = ToolDescriptor(
            name="detect_conflicts",
            description="Compare masters of active ESP plugins.",
            input_schema=_clean_schema(ProfileParams),
            fn=lambda profile: detect_conflicts(self._registry, self._mo2, profile),
            params_model=ProfileParams,
        )
        self._tools["run_loot_sort"] = ToolDescriptor(
            name="run_loot_sort",
            description="Invoke LOOT to sort load order.",
            input_schema=_clean_schema(ProfileParams),
            fn=lambda profile: run_loot_sort(self._mo2, self._loot_runner, self._loot_exe, profile),
            params_model=ProfileParams,
        )
        self._tools["run_xedit_script"] = ToolDescriptor(
            name="run_xedit_script",
            description="Run an xEdit script in headless mode.",
            input_schema=_clean_schema(XEditAnalysisParams),
            fn=lambda script_name, plugins: run_xedit_script(self._xedit_runner, script_name, plugins),
            params_model=XEditAnalysisParams,
        )
        self._tools["preview_mod_installer"] = ToolDescriptor(
            name="preview_mod_installer",
            description="Preview FOMOD options for a mod archive.",
            input_schema=_clean_schema(PreviewInstallerParams),
            fn=lambda archive_path: preview_mod_installer(self._fomod_installer, archive_path),
            params_model=PreviewInstallerParams,
        )
        self._tools["install_mod_from_archive"] = ToolDescriptor(
            name="install_mod_from_archive",
            description="Install a mod from archive into MO2.",
            input_schema=_clean_schema(InstallFromArchiveParams),
            fn=lambda archive_path, selections=None: install_mod_from_archive(
                self._mo2, self._fomod_installer, self._hitl, archive_path, selections
            ),
            params_model=InstallFromArchiveParams,
        )
        self._tools["resolve_fomod"] = ToolDescriptor(
            name="resolve_fomod",
            description="Resolve FOMOD installation options.",
            input_schema=_clean_schema(ResolveFomodParams),
            fn=lambda archive_path, selections=None: resolve_fomod(self._fomod_installer, archive_path, selections),
            params_model=ResolveFomodParams,
        )
        self._tools["analyze_esp_conflicts"] = ToolDescriptor(
            name="analyze_esp_conflicts",
            description="Analyze record-level ESP conflicts.",
            input_schema=_clean_schema(AnalyzeConflictsParams),
            fn=lambda profile, plugins=None: analyze_esp_conflicts(self._mo2, self._xedit_runner, profile, plugins),
            params_model=AnalyzeConflictsParams,
        )
        # Tools without parameters: no params_model, schema kept inline.
        self._tools["run_pandora"] = ToolDescriptor(
            name="run_pandora",
            description="Execute Pandora Behavior Engine.",
            input_schema={"type": "object", "properties": {}},
            fn=lambda: run_pandora(self._animation_hub),
        )
        self._tools["run_bodyslide"] = ToolDescriptor(
            name="run_bodyslide",
            description="Execute BodySlide in batch mode.",
            input_schema={"type": "object", "properties": {}},
            fn=lambda: run_bodyslide_batch(self._animation_hub),
        )
        # FASE 6: Direct runner tools (bypass animation_hub)
        self._tools["generate_bashed_patch"] = ToolDescriptor(
            name="generate_bashed_patch",
            description=(
                "Generate the Bashed Patch using Wrye Bash. "
                "Runs validate_load_order_limit (M-04) first and aborts if the 254-plugin limit is exceeded."
            ),
            input_schema={"type": "object", "properties": {}},
            fn=lambda: generate_bashed_patch(self._wrye_bash_runner),
        )
        self._tools["run_pandora_behavior"] = ToolDescriptor(
            name="run_pandora_behavior",
            description="Execute Pandora Behavior Engine (animation patcher) in auto mode for Skyrim SE.",
            input_schema={"type": "object", "properties": {}},
            fn=lambda: run_pandora_behavior(self._pandora_runner),
        )
        self._tools["run_bodyslide_batch"] = ToolDescriptor(
            name="run_bodyslide_batch",
            description="Execute BodySlide in batch mode for a preset group.",
            input_schema={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": "BodySlide preset group name.",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output directory for generated meshes.",
                    },
                },
            },
            fn=lambda group="CBBE", output_path="meshes": run_bodyslide_batch_direct(
                self._bodyslide_runner, group, output_path
            ),
        )
        self._tools["uninstall_mod"] = ToolDescriptor(
            name="uninstall_mod",
            description="Uninstall a mod completely.",
            input_schema=_clean_schema(ModNameParams),
            fn=lambda mod_name, profile="Default": uninstall_mod(self._mo2, mod_name, profile),
            params_model=ModNameParams,
        )
        self._tools["toggle_mod"] = ToolDescriptor(
            name="toggle_mod",
            description="Enable or disable an installed mod.",
            input_schema=_clean_schema(ToggleModParams),
            fn=lambda mod_name, enable, profile="Default": toggle_mod(self._mo2, mod_name, enable, profile),
            params_model=ToggleModParams,
        )
        self._tools["launch_game"] = ToolDescriptor(
            name="launch_game",
            description="Launch Skyrim via MO2.",
            input_schema=_clean_schema(ProfileParams),
            fn=lambda profile="Default": launch_game(self._mo2, profile),
            params_model=ProfileParams,
        )
        self._tools["close_game"] = ToolDescriptor(
            name="close_game",
            description="Forcefully close the game.",
            input_schema={"type": "object", "properties": {}},
            fn=lambda: close_game(self._mo2),
        )
        # External Tools
        self._tools["setup_tools"] = ToolDescriptor(
            name="setup_tools",
            description="Download and install external modding tools.",
            input_schema=_clean_schema(SetupToolsParams),
            fn=lambda tools=None: setup_tools(
                self._tools_installer,
                self._install_dir,
                self._local_cfg,
                self._config_path,
                self._animation_hub,
                self._downloader,
                [self._loot_exe],
                tools,
            ),
            params_model=SetupToolsParams,
        )


__all__ = [
    "AnalyzeConflictsParams",
    "AsyncToolRegistry",
    "DownloadModParams",
    "InstallFromArchiveParams",
    "InstallModParams",
    "LOOTConfig",
    "LOOTRunner",
    "ModNameParams",
    "PreviewInstallerParams",
    "ProfileParams",
    "ResolveFomodParams",
    # re-exports
    "SearchModParams",
    "SetupToolsParams",
    "ToggleModParams",
    "ToolDescriptor",
    "XEditAnalysisParams",
    "analyze_esp_conflicts",
    "check_load_order",
    "close_game",
    "detect_conflicts",
    "download_mod",
    "generate_bashed_patch",
    "install_mod",
    "install_mod_from_archive",
    "launch_game",
    "preview_mod_installer",
    "resolve_fomod",
    "run_bodyslide_batch",
    "run_bodyslide_batch_direct",
    "run_loot_sort",
    "run_pandora",
    "run_pandora_behavior",
    "run_xedit_script",
    "search_mod",
    "setup_tools",
    "toggle_mod",
    "uninstall_mod",
]
