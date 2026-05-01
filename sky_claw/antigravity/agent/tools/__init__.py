"""Paquete de herramientas modulares para Sky-Claw agent.

FACADE: Este archivo actúa como el punto de entrada oficial (Facade)
que expone AsyncToolRegistry y todos los esquemas y handlers.

Extraído de tools.py como parte de la refactorización M-13.

TASK-011 Single Source of Truth:
- ``execute()`` validates raw LLM arguments against the tool's
  ``params_model`` (Pydantic BaseModel) before dispatching.
- ``input_schema`` is auto-generated from ``params_model.model_json_schema()``
  via ``_clean_schema()`` — no more hardcoded JSON schemas.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

from sky_claw.config import SystemPaths
from sky_claw.local.loot.cli import LOOTConfig, LOOTRunner

from .db_tools import install_mod, search_mod
from .descriptor import ToolDescriptor
from .external_tools import setup_tools
from .nexus_tools import download_mod
from .schemas import (
    AnalyzeConflictsParams,
    BodySlideBatchParams,
    DownloadModParams,
    InstallFromArchiveParams,
    InstallModParams,
    LaunchGameParams,
    ModNameParams,
    PreviewInstallerParams,
    ProfileParams,
    ResolveFomodParams,
    SearchModParams,
    SetupToolsParams,
    ToggleModParams,
    UninstallModParams,
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


class AsyncToolRegistry:
    """Registry and executor for async agent tools.

    Fábrica de herramientas que delega a los handlers modulares.
    Mantiene compatibilidad total con la implementación original.

    TASK-011: ``execute()`` now validates raw LLM arguments against
    the registered ``params_model`` (Pydantic BaseModel with strict=True)
    before dispatching to the handler.  This is the Single Source of Truth
    for parameter validation — handlers no longer need defensive Pydantic
    instantiation.
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
        # TASK-013 P1: Zero-Trust egress — gateway is threaded to every tool
        # that performs outbound HTTP so NetworkGateway.authorize() is always enforced.
        gateway: Any | None = None,
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
        self._gateway = gateway
        self._tools: dict[str, ToolDescriptor] = {}
        self._register_builtins()

    def _resolve_gateway(self) -> Any:
        """Resolve the active NetworkGateway using a best-effort fallback chain.

        Priority:
        1. ``self._gateway`` — explicitly injected at construction time.
        2. ``self._downloader._gateway`` — falls back to the downloader's gateway
           for callers (e.g. older AppContext versions) that wire the gateway to the
           downloader but forget to pass it here.
        3. ``self._tools_installer._gateway`` — same pattern for the tools installer.
        4. ``None`` — no gateway available; Zero-Trust handlers will abort.
        """
        if self._gateway is not None:
            return self._gateway
        if (
            self._downloader is not None
            and hasattr(self._downloader, "_gateway")
            and self._downloader._gateway is not None
        ):
            return self._downloader._gateway
        if (
            self._tools_installer is not None
            and hasattr(self._tools_installer, "_gateway")
            and self._tools_installer._gateway is not None
        ):
            return self._tools_installer._gateway
        return None

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
        return await download_mod(
            self._downloader,
            self._hitl,
            self._sync_engine,
            nexus_id,
            file_id,
            gateway=self._resolve_gateway(),
        )

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
        """Execute a registered tool with centralized Pydantic validation.

        TASK-011 Single Source of Truth: Before dispatching to the handler,
        the raw ``arguments`` dict from the LLM is validated against the
        tool's ``params_model`` using ``model_validate(arguments, strict=True)``.
        On success, validated fields are passed as keyword arguments via
        ``model_dump()``.  On failure, ``pydantic.ValidationError`` propagates
        to the caller (LLMRouter) for self-healing feedback.

        Args:
            name: Registered tool name.
            arguments: Raw parameter dict from the LLM.

        Returns:
            JSON-encoded string result from the tool handler.

        Raises:
            KeyError: If ``name`` is not a registered tool.
            pydantic.ValidationError: If ``arguments`` fail validation.
        """
        td = self._tools.get(name)
        if td is None:
            raise KeyError(f"Unknown tool: {name!r}")

        # Single Source of Truth: centralized validation gate
        if td.params_model is not None:
            validated = td.params_model.model_validate(arguments, strict=True)
            return await td.fn(**validated.model_dump())

        # Fallback for tools without a params_model (no parameters)
        return await td.fn()

    def _register_builtins(self) -> None:
        # DB
        self._tools["search_mod"] = ToolDescriptor(
            name="search_mod",
            description="Search the local mod registry by name.",
            params_model=SearchModParams,
            fn=lambda mod_name: search_mod(self._registry, mod_name),
        )
        self._tools["install_mod"] = ToolDescriptor(
            name="install_mod",
            description="Register a mod in the database via the SyncEngine.",
            params_model=InstallModParams,
            fn=lambda nexus_id, version: install_mod(self._registry, nexus_id, version),
        )
        # Nexus
        self._tools["download_mod"] = ToolDescriptor(
            name="download_mod",
            description="Download a file from Nexus Mods with HITL approval.",
            params_model=DownloadModParams,
            fn=lambda nexus_id, file_id=None: download_mod(
                self._downloader,
                self._hitl,
                self._sync_engine,
                nexus_id,
                file_id,
                gateway=self._resolve_gateway(),
            ),
        )
        # System
        self._tools["check_load_order"] = ToolDescriptor(
            name="check_load_order",
            description="Read the MO2 modlist for a profile.",
            params_model=ProfileParams,
            fn=lambda profile: check_load_order(self._mo2, profile),
        )
        self._tools["detect_conflicts"] = ToolDescriptor(
            name="detect_conflicts",
            description="Compare masters of active ESP plugins.",
            params_model=ProfileParams,
            fn=lambda profile: detect_conflicts(self._registry, self._mo2, profile),
        )
        self._tools["run_loot_sort"] = ToolDescriptor(
            name="run_loot_sort",
            description="Invoke LOOT to sort load order.",
            params_model=ProfileParams,
            fn=lambda profile: run_loot_sort(self._mo2, self._loot_runner, self._loot_exe, profile),
        )
        self._tools["run_xedit_script"] = ToolDescriptor(
            name="run_xedit_script",
            description="Run an xEdit script in headless mode.",
            params_model=XEditAnalysisParams,
            fn=lambda script_name, plugins: run_xedit_script(self._xedit_runner, script_name, plugins),
        )
        self._tools["preview_mod_installer"] = ToolDescriptor(
            name="preview_mod_installer",
            description="Preview FOMOD options for a mod archive.",
            params_model=PreviewInstallerParams,
            fn=lambda archive_path: preview_mod_installer(self._fomod_installer, archive_path),
        )
        self._tools["install_mod_from_archive"] = ToolDescriptor(
            name="install_mod_from_archive",
            description="Install a mod from archive into MO2.",
            params_model=InstallFromArchiveParams,
            fn=lambda archive_path, selections=None: install_mod_from_archive(
                self._mo2, self._fomod_installer, self._hitl, archive_path, selections
            ),
        )
        self._tools["resolve_fomod"] = ToolDescriptor(
            name="resolve_fomod",
            description="Resolve FOMOD installation options.",
            params_model=ResolveFomodParams,
            fn=lambda archive_path, selections=None: resolve_fomod(self._fomod_installer, archive_path, selections),
        )
        self._tools["analyze_esp_conflicts"] = ToolDescriptor(
            name="analyze_esp_conflicts",
            description="Analyze record-level ESP conflicts.",
            params_model=AnalyzeConflictsParams,
            fn=lambda profile, plugins=None: analyze_esp_conflicts(self._mo2, self._xedit_runner, profile, plugins),
        )
        self._tools["run_pandora"] = ToolDescriptor(
            name="run_pandora",
            description="Execute Pandora Behavior Engine.",
            params_model=None,
            fn=lambda: run_pandora(self._animation_hub),
        )
        self._tools["run_bodyslide"] = ToolDescriptor(
            name="run_bodyslide",
            description="Execute BodySlide in batch mode.",
            params_model=None,
            fn=lambda: run_bodyslide_batch(self._animation_hub),
        )
        # FASE 6: Direct runner tools (bypass animation_hub)
        self._tools["generate_bashed_patch"] = ToolDescriptor(
            name="generate_bashed_patch",
            description=(
                "Generate the Bashed Patch using Wrye Bash. "
                "Runs validate_load_order_limit (M-04) first and aborts if the 254-plugin limit is exceeded."
            ),
            params_model=None,
            fn=lambda: generate_bashed_patch(self._wrye_bash_runner),
        )
        self._tools["run_pandora_behavior"] = ToolDescriptor(
            name="run_pandora_behavior",
            description="Execute Pandora Behavior Engine (animation patcher) in auto mode for Skyrim SE.",
            params_model=None,
            fn=lambda: run_pandora_behavior(self._pandora_runner),
        )
        self._tools["run_bodyslide_batch"] = ToolDescriptor(
            name="run_bodyslide_batch",
            description="Execute BodySlide in batch mode for a preset group.",
            params_model=BodySlideBatchParams,
            fn=lambda group="CBBE", output_path="meshes": run_bodyslide_batch_direct(
                self._bodyslide_runner, group, output_path
            ),
        )
        self._tools["uninstall_mod"] = ToolDescriptor(
            name="uninstall_mod",
            description="Uninstall a mod completely.",
            params_model=UninstallModParams,
            fn=lambda mod_name, profile="Default": uninstall_mod(self._mo2, mod_name, profile),
        )
        self._tools["toggle_mod"] = ToolDescriptor(
            name="toggle_mod",
            description="Enable or disable an installed mod.",
            params_model=ToggleModParams,
            fn=lambda mod_name, enable, profile="Default": toggle_mod(self._mo2, mod_name, enable, profile),
        )
        self._tools["launch_game"] = ToolDescriptor(
            name="launch_game",
            description="Launch Skyrim via MO2.",
            params_model=LaunchGameParams,
            fn=lambda profile="Default": launch_game(self._mo2, profile),
        )
        self._tools["close_game"] = ToolDescriptor(
            name="close_game",
            description="Forcefully close the game.",
            params_model=None,
            fn=lambda: close_game(self._mo2),
        )
        # External Tools
        self._tools["setup_tools"] = ToolDescriptor(
            name="setup_tools",
            description="Download and install external modding tools.",
            params_model=SetupToolsParams,
            fn=lambda tools=None: setup_tools(
                self._tools_installer,
                self._install_dir,
                self._local_cfg,
                self._config_path,
                self._animation_hub,
                self._downloader,
                [self._loot_exe],
                tools,
                gateway=self._resolve_gateway(),
            ),
        )


__all__ = [
    "AnalyzeConflictsParams",
    "AsyncToolRegistry",
    "BodySlideBatchParams",
    "DownloadModParams",
    "InstallFromArchiveParams",
    "InstallModParams",
    "LaunchGameParams",
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
    "UninstallModParams",
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
