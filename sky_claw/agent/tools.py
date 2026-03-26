"""Async tool registry and tool implementations for the Sky-Claw agent.

Each tool validates its parameters with Pydantic v2 before touching
the database or filesystem and returns a JSON-encoded ``str`` for
compatibility with Anthropic's ``tool_use`` API.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Callable, Awaitable

import aiohttp
import pydantic

from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.fomod.installer import FomodInstaller
from sky_claw.loot.cli import LOOTConfig, LOOTRunner, LOOTNotFoundError
from sky_claw.mo2.vfs import MO2Controller
from sky_claw.orchestrator.sync_engine import SyncEngine
from sky_claw.scraper.nexus_downloader import NexusDownloader
from sky_claw.security.hitl import Decision, HITLGuard
from sky_claw.tools_installer import ToolsInstaller, ToolInstallError
from sky_claw.xedit.conflict_analyzer import ConflictAnalyzer
from sky_claw.xedit.runner import XEditRunner, XEditValidationError, XEditNotFoundError
from sky_claw.agent.animation_hub import AnimationHub
from sky_claw.local_config import LocalConfig, save as save_local_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic v2 parameter models
# ---------------------------------------------------------------------------


class SearchModParams(pydantic.BaseModel):
    """Parameters for the ``search_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")


class ProfileParams(pydantic.BaseModel):
    """Parameters for tools that operate on an MO2 profile."""

    model_config = pydantic.ConfigDict(strict=True)

    profile: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")


class InstallModParams(pydantic.BaseModel):
    """Parameters for the ``install_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    nexus_id: int = pydantic.Field(gt=0)
    version: str = pydantic.Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.\-]+$")


class XEditAnalysisParams(pydantic.BaseModel):
    """Parameters for the ``run_xedit_analysis`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    script_name: str = pydantic.Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_\-]+\.pas$")
    plugins: list[str] = pydantic.Field(min_length=1)


class DownloadModParams(pydantic.BaseModel):
    """Parameters for the ``download_mod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    nexus_id: int = pydantic.Field(gt=0)
    file_id: int | None = pydantic.Field(None, gt=0)


class PreviewInstallerParams(pydantic.BaseModel):
    """Parameters for the ``preview_mod_installer`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    archive_path: str = pydantic.Field(min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9_\\/\-.:]+$")


class InstallFromArchiveParams(pydantic.BaseModel):
    """Parameters for the ``install_mod_from_archive`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    archive_path: str = pydantic.Field(min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9_\\/\-.:]+$")
    selections: dict[str, list[str]] = pydantic.Field(default_factory=dict)


class ResolveFomodParams(pydantic.BaseModel):
    """Parameters for the ``resolve_fomod`` tool."""

    model_config = pydantic.ConfigDict(strict=True)

    archive_path: str = pydantic.Field(min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9_\\/\-.:]+$")
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

    profile: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")
    plugins: list[str] | None = pydantic.Field(
        default=None,
        description="Specific plugins to analyze. If omitted, uses all enabled plugins from the profile.",
    )


class ModNameParams(pydantic.BaseModel):
    """Parameters for tools specifying a mod name."""
    model_config = pydantic.ConfigDict(strict=True)
    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")
    profile: str = pydantic.Field(default="Default", pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")


class ToggleModParams(pydantic.BaseModel):
    """Parameters for toggling a mod."""
    model_config = pydantic.ConfigDict(strict=True)
    mod_name: str = pydantic.Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_. \-'%()\[\]]+$")
    enable: bool
    profile: str = pydantic.Field(default="Default", pattern=r"^[a-zA-Z0-9_. \-]+$")


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------


class ToolDescriptor:
    """Metadata for a single registered tool.

    Attributes:
        name: Unique tool name.
        description: Human-readable description for the LLM.
        input_schema: JSON Schema dict describing tool parameters.
        fn: The async callable that implements the tool.
    """

    __slots__ = ("name", "description", "input_schema", "fn")

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        fn: Callable[..., Awaitable[str]],
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.fn = fn


# ---------------------------------------------------------------------------
# AsyncToolRegistry
# ---------------------------------------------------------------------------


class AsyncToolRegistry:
    """Registry and executor for async agent tools.

    Args:
        registry: Async mod database layer.
        mo2: MO2 portable-instance controller.
        sync_engine: Orchestrator for mod synchronisation tasks.
        loot_exe: Path to the LOOT CLI executable.
        xedit_runner: Optional xEdit runner for plugin analysis.
        loot_runner: Optional LOOT runner for load-order sorting.
        hitl: Optional :class:`HITLGuard` for operator approval flows.
        downloader: Optional :class:`NexusDownloader` for premium downloads.
    """

    def __init__(
        self,
        registry: AsyncModRegistry,
        mo2: MO2Controller,
        sync_engine: SyncEngine,
        loot_exe: pathlib.Path | None = None,
        xedit_runner: XEditRunner | None = None,
        loot_runner: LOOTRunner | None = None,
        hitl: HITLGuard | None = None,
        downloader: NexusDownloader | None = None,
        fomod_installer: FomodInstaller | None = None,
        tools_installer: ToolsInstaller | None = None,
        install_dir: pathlib.Path | None = None,
        animation_hub: AnimationHub | None = None,
        local_cfg: LocalConfig | None = None,
        config_path: pathlib.Path | None = None,
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
        self._install_dir = install_dir or pathlib.Path("C:/Modding")
        self._animation_hub = animation_hub
        self._local_cfg = local_cfg
        self._config_path = config_path
        self._tools: dict[str, ToolDescriptor] = {}
        self._register_builtins()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def tools(self) -> dict[str, ToolDescriptor]:
        """Return a read-only view of registered tools."""
        return dict(self._tools)

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool definitions formatted for the Anthropic API."""
        return [
            {
                "name": td.name,
                "description": td.description,
                "input_schema": td.input_schema,
            }
            for td in self._tools.values()
        ]

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool by *name* with the given *arguments*.

        Args:
            name: Registered tool name.
            arguments: Raw arguments dict (validated internally).

        Returns:
            JSON-encoded string result.

        Raises:
            KeyError: If the tool is not registered.
        """
        td = self._tools.get(name)
        if td is None:
            raise KeyError(f"Unknown tool: {name!r}")
        return await td.fn(**arguments)

    # ------------------------------------------------------------------
    # Built-in tool registration
    # ------------------------------------------------------------------

    def _register_builtins(self) -> None:
        """Register the five built-in tools."""
        self._tools["search_mod"] = ToolDescriptor(
            name="search_mod",
            description="Search the local mod registry by name. Returns JSON with id, name, version, and VFS status.",
            input_schema={
                "type": "object",
                "properties": {
                    "mod_name": {
                        "type": "string",
                        "description": "Mod name (or partial name) to search for.",
                    },
                },
                "required": ["mod_name"],
            },
            fn=self._search_mod,
        )
        self._tools["check_load_order"] = ToolDescriptor(
            name="check_load_order",
            description="Read the MO2 modlist for a profile and return the ordered list with enabled/disabled status.",
            input_schema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "MO2 profile name.",
                    },
                },
                "required": ["profile"],
            },
            fn=self._check_load_order,
        )
        self._tools["detect_conflicts"] = ToolDescriptor(
            name="detect_conflicts",
            description="Compare masters of active ESP plugins in a profile and detect missing-master conflicts.",
            input_schema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "MO2 profile name.",
                    },
                },
                "required": ["profile"],
            },
            fn=self._detect_conflicts,
        )
        self._tools["run_loot_sort"] = ToolDescriptor(
            name="run_loot_sort",
            description="Invoke the LOOT CLI to sort the load order for a profile. Returns the LOOT sorting log.",
            input_schema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "MO2 profile name.",
                    },
                },
                "required": ["profile"],
            },
            fn=self._run_loot_sort,
        )
        self._tools["install_mod"] = ToolDescriptor(
            name="install_mod",
            description="Register a mod in the database via the SyncEngine. Does NOT download binaries.",
            input_schema={
                "type": "object",
                "properties": {
                    "nexus_id": {
                        "type": "integer",
                        "description": "Nexus Mods numeric ID.",
                    },
                    "version": {
                        "type": "string",
                        "description": "Mod version string.",
                    },
                },
                "required": ["nexus_id", "version"],
            },
            fn=self._install_mod,
        )
        self._tools["run_xedit_script"] = ToolDescriptor(
            name="run_xedit_script",
            description="Run an xEdit (SSEEdit) Pascal script in headless mode to analyze plugin conflicts at the record level.",
            input_schema={
                "type": "object",
                "properties": {
                    "script_name": {
                        "type": "string",
                        "description": "Name of the .pas script to execute (e.g. 'list_conflicts.pas').",
                    },
                    "plugins": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of plugin filenames to load (e.g. ['Skyrim.esm', 'Requiem.esp']).",
                    },
                },
                "required": ["script_name", "plugins"],
            },
            fn=self._run_xedit_script,
        )
        self._tools["download_mod"] = ToolDescriptor(
            name="download_mod",
            description=(
                "Download a specific file from Nexus Mods using premium API credentials. "
                "Queries file size and metadata first, then requires mandatory operator "
                "approval via HITL before any bytes are transferred. "
                "On approval the download is enqueued in the SyncEngine and the file "
                "is saved to the MO2 staging directory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "nexus_id": {
                        "type": "integer",
                        "description": "Nexus Mods numeric mod ID (e.g. 12345).",
                    },
                    "file_id": {
                        "type": "integer",
                        "description": "Optional. Nexus numeric file ID for a specific file version. If omitted, the latest primary file is downloaded automatically.",
                    },
                },
                "required": ["nexus_id"],
            },
            fn=self._download_mod,
        )
        self._tools["preview_mod_installer"] = ToolDescriptor(
            name="preview_mod_installer",
            description=(
                "Preview the FOMOD installer options for a downloaded mod archive. "
                "Returns the installation steps and available options so you can "
                "decide which selections to make before installing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "archive_path": {
                        "type": "string",
                        "description": "Path to the downloaded mod archive (.zip, .7z, .rar).",
                    },
                },
                "required": ["archive_path"],
            },
            fn=self._preview_mod_installer,
        )
        self._tools["install_mod_from_archive"] = ToolDescriptor(
            name="install_mod_from_archive",
            description=(
                "Install a mod from a downloaded archive into MO2. "
                "If the mod has a FOMOD installer, provide selections for each step. "
                "Use preview_mod_installer first to see available options."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "archive_path": {
                        "type": "string",
                        "description": "Path to the downloaded mod archive.",
                    },
                    "selections": {
                        "type": "object",
                        "description": (
                            "FOMOD selections as {step_name: [plugin_names]}. "
                            "Empty dict for simple mods without FOMOD."
                        ),
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "required": ["archive_path"],
            },
            fn=self._install_mod_from_archive,
        )
        self._tools["resolve_fomod"] = ToolDescriptor(
            name="resolve_fomod",
            description=(
                "Resolve FOMOD installation options without actually installing. "
                "Returns the files that would be installed and any pending decisions. "
                "Use this to validate your selections before calling install_mod_from_archive."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "archive_path": {
                        "type": "string",
                        "description": "Path to the downloaded mod archive.",
                    },
                    "selections": {
                        "type": "object",
                        "description": (
                            "FOMOD selections as {step_name: [plugin_names]}. "
                            "Empty dict for simple mods without FOMOD."
                        ),
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "required": ["archive_path"],
            },
            fn=self._resolve_fomod,
        )
        self._tools["setup_tools"] = ToolDescriptor(
            name="setup_tools",
            description=(
                "Download and install external modding tools (LOOT, SSEEdit, Pandora, BodySlide) "
                "from their official GitHub releases. Requires operator approval "
                "before downloading any binary."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tools": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["loot", "xedit", "pandora", "bodyslide"]},
                    },
                },
            },
            fn=self._setup_tools,
        )
        self._tools["analyze_esp_conflicts"] = ToolDescriptor(
            name="analyze_esp_conflicts",
            description="Analyze record-level conflicts between ESP plugins using xEdit.",
            input_schema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "MO2 profile name.",
                    },
                    "plugins": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["profile"],
            },
            fn=self._analyze_esp_conflicts,
        )
        self._tools["run_pandora"] = ToolDescriptor(
            name="run_pandora",
            description="Execute Pandora Behavior Engine to update animation behavior graphs.",
            input_schema={"type": "object", "properties": {}},
            fn=self._run_pandora,
        )
        self._tools["run_bodyslide"] = ToolDescriptor(
            name="run_bodyslide",
            description="Execute BodySlide in batch mode to generate meshes for outfits.",
            input_schema={"type": "object", "properties": {}},
            fn=self._run_bodyslide_batch,
        )
        self._tools["uninstall_mod"] = ToolDescriptor(
            name="uninstall_mod",
            description="Uninstall a mod completely by deleting its files from MO2 and removing it from the modlist.",
            input_schema={
                "type": "object",
                "properties": {
                    "mod_name": {"type": "string"},
                    "profile": {"type": "string", "default": "Default"},
                },
                "required": ["mod_name"],
            },
            fn=self._uninstall_mod,
        )
        self._tools["toggle_mod"] = ToolDescriptor(
            name="toggle_mod",
            description="Enable or disable an installed mod in a specific MO2 profile load order.",
            input_schema={
                "type": "object",
                "properties": {
                    "mod_name": {"type": "string"},
                    "enable": {"type": "boolean"},
                    "profile": {"type": "string", "default": "Default"},
                },
                "required": ["mod_name", "enable"],
            },
            fn=self._toggle_mod,
        )
        self._tools["launch_game"] = ToolDescriptor(
            name="launch_game",
            description="Launch Skyrim Special Edition via MO2 using the SKSE executable.",
            input_schema={
                "type": "object",
                "properties": {
                    "profile": {"type": "string", "default": "Default"},
                },
            },
            fn=self._launch_game,
        )
        self._tools["close_game"] = ToolDescriptor(
            name="close_game",
            description="Forcefully close Skyrim SE and MO2 to terminate the game.",
            input_schema={"type": "object", "properties": {}},
            fn=self._close_game,
        )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _search_mod(self, mod_name: str) -> str:
        """Search the local mod registry by name.

        Args:
            mod_name: Mod name (or partial name) to search for.

        Returns:
            JSON string with matching mod records.
        """
        params = SearchModParams(mod_name=mod_name)
        results = await self._registry.search_mods(params.mod_name)
        return json.dumps({"matches": results})

    async def _check_load_order(self, profile: str) -> str:
        """Read the MO2 modlist for a profile.

        Args:
            profile: MO2 profile name.

        Returns:
            JSON string with the ordered mod list.
        """
        params = ProfileParams(profile=profile)
        entries: list[dict[str, Any]] = []
        idx = 0
        async for mod_name, enabled in self._mo2.read_modlist(params.profile):
            entries.append(
                {"index": idx, "name": mod_name, "enabled": enabled}
            )
            idx += 1
        return json.dumps({"profile": params.profile, "load_order": entries})

    async def _detect_conflicts(self, profile: str) -> str:
        """Detect missing-master conflicts among active ESPs.

        Args:
            profile: MO2 profile name.

        Returns:
            JSON string with detected conflicts.
        """
        params = ProfileParams(profile=profile)

        # Collect enabled mod names from modlist.
        enabled_mods: list[str] = []
        async for mod_name, enabled in self._mo2.read_modlist(params.profile):
            if enabled:
                enabled_mods.append(mod_name)

        # Find missing masters in a single query.
        conflicts = await self._registry.find_missing_masters_for_mods(enabled_mods)

        return json.dumps({"profile": params.profile, "conflicts": conflicts})

    async def _run_loot_sort(self, profile: str) -> str:
        """Invoke the LOOT CLI to sort the load order.

        Uses LOOTRunner with the correct game path (real Skyrim SE
        directory, not MO2 root).

        Args:
            profile: MO2 profile name.

        Returns:
            JSON string with the LOOT sorting result.
        """
        params = ProfileParams(profile=profile)

        if self._loot_runner is None and self._loot_exe is not None:
            try:
                config = LOOTConfig(
                    loot_exe=self._loot_exe,
                    game_path=self._mo2.root,
                )
                self._loot_runner = LOOTRunner(config)
            except LOOTNotFoundError as exc:
                return json.dumps({"error": str(exc)})

        if self._loot_runner is None:
            return json.dumps(
                {"error": "LOOT runner is not configured"}
            )

        try:
            result = await self._loot_runner.sort()
        except LOOTNotFoundError as exc:
            return json.dumps({"error": str(exc)})
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)})

        return json.dumps(
            {
                "profile": params.profile,
                "success": result.success,
                "return_code": result.return_code,
                "sorted_plugins": result.sorted_plugins,
                "warnings": result.warnings,
                "errors": result.errors,
            }
        )

    async def _install_mod(self, nexus_id: int, version: str) -> str:
        """Register a mod in the database. Does NOT download binaries.

        Args:
            nexus_id: Nexus Mods numeric ID.
            version: Mod version string.

        Returns:
            JSON string confirming registration.
        """
        params = InstallModParams(nexus_id=nexus_id, version=version)
        mod_id = await self._registry.upsert_mod(
            nexus_id=params.nexus_id,
            name=f"nexus-{params.nexus_id}",
            version=params.version,
        )
        await self._registry.log_tasks_batch(
            [(mod_id, "install_mod", "registered", f"v{params.version}")]
        )
        return json.dumps(
            {
                "mod_id": mod_id,
                "nexus_id": params.nexus_id,
                "version": params.version,
                "status": "registered",
            }
        )

    async def _run_xedit_script(
        self, script_name: str, plugins: list[str]
    ) -> str:
        """Run an xEdit script in headless mode.

        Args:
            script_name: Pascal script filename.
            plugins: Plugin filenames to load.

        Returns:
            JSON string with xEdit analysis results.
        """
        params = XEditAnalysisParams(script_name=script_name, plugins=plugins)

        if self._xedit_runner is None:
            return json.dumps({"error": "xEdit runner is not configured"})

        try:
            result = await self._xedit_runner.run_script(
                params.script_name, params.plugins
            )
        except XEditNotFoundError as exc:
            return json.dumps({"error": str(exc)})
        except XEditValidationError as exc:
            return json.dumps({"error": str(exc)})
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)})

        return json.dumps(
            {
                "success": result.success,
                "return_code": result.return_code,
                "processed_plugins": result.processed_plugins,
                "conflicts": [
                    {
                        "plugin": c.plugin,
                        "record": c.record,
                        "detail": c.detail,
                    }
                    for c in result.conflicts
                ],
                "errors": result.errors,
            }
        )

    async def _download_mod(self, nexus_id: int, file_id: int | None = None) -> str:
        """Download a Nexus Mods file with mandatory HITL confirmation.

        Flow:
        1. Validate parameters with Pydantic.
        2. Return an error immediately when downloader or HITL are not configured.
        3. Query file metadata (name, size, MD5) via the Nexus API.
        4. Dispatch a :class:`HITLGuard` approval request with all relevant details.
        5. If denied or timed-out, abort without touching the filesystem.
        6. If approved, enqueue the download coroutine in :attr:`SyncEngine` and
           return a confirmation payload.

        Args:
            nexus_id: Nexus Mods numeric mod ID.
            file_id: Optional. Nexus Mods numeric file ID.

        Returns:
            JSON string with status and metadata, or an error description.
        """
        params = DownloadModParams(nexus_id=nexus_id, file_id=file_id)

        if self._downloader is None:
            return json.dumps({"error": "Nexus downloader is not configured"})
        if self._hitl is None:
            return json.dumps({"error": "HITL guard is not configured"})

        # ------------------------------------------------------------------
        # Step 1 – Query file metadata before asking the operator.
        # ------------------------------------------------------------------
        async with aiohttp.ClientSession() as session:
            try:
                file_info = await self._downloader.get_file_info(
                    params.nexus_id, params.file_id, session
                )
            except Exception as exc:
                logger.error(
                    "Failed to fetch metadata for mod=%d file=%d: %s",
                    params.nexus_id,
                    params.file_id,
                    exc,
                )
                return json.dumps(
                    {
                        "error": f"Could not retrieve file metadata: {exc}",
                        "nexus_id": params.nexus_id,
                        "file_id": params.file_id,
                    }
                )

            # ------------------------------------------------------------------
            # Step 2 – Mandatory HITL confirmation.
            # ------------------------------------------------------------------
            size_mb = file_info.size_bytes / (1024 * 1024) if file_info.size_bytes else 0
            detail = (
                f"File: {file_info.file_name}  |  "
                f"Size: {size_mb:.1f} MB  |  "
                f"MD5: {file_info.md5 or 'n/a'}  |  "
                f"URL: {file_info.download_url}"
            )
            request_id = f"download-{params.nexus_id}-{params.file_id}"
            decision = await self._hitl.request_approval(
                request_id=request_id,
                reason=(
                    f"Operator approval required to download "
                    f"mod {params.nexus_id} / file {params.file_id} "
                    f"({file_info.file_name}, {size_mb:.1f} MB)"
                ),
                url=file_info.download_url,
                detail=detail,
            )

            if decision is not Decision.APPROVED:
                logger.warning(
                    "Download denied by operator: mod=%d file=%d decision=%s",
                    params.nexus_id,
                    params.file_id,
                    decision.value,
                )
                return json.dumps(
                    {
                        "status": "denied",
                        "decision": decision.value,
                        "nexus_id": params.nexus_id,
                        "file_id": params.file_id,
                        "file_name": file_info.file_name,
                    }
                )

            # ------------------------------------------------------------------
            # Step 3 – Enqueue the download in SyncEngine.
            # ------------------------------------------------------------------
            downloader = self._downloader  # capture for closure
            _nexus_id = params.nexus_id
            _file_id = params.file_id

            async def _do_download() -> None:
                async with aiohttp.ClientSession() as dl_session:
                    fresh_info = await downloader.get_file_info(
                        _nexus_id, _file_id, dl_session
                    )
                    await downloader.download(fresh_info, dl_session)

            self._sync_engine.enqueue_download(
                _do_download(),
                context=f"nexus_id={params.nexus_id} file_id={params.file_id}"
            )
            logger.info(
                "Download enqueued: mod=%d file=%d name=%s",
                params.nexus_id,
                params.file_id,
                file_info.file_name,
            )

        return json.dumps(
            {
                "status": "enqueued",
                "nexus_id": params.nexus_id,
                "file_id": params.file_id,
                "file_name": file_info.file_name,
                "size_bytes": file_info.size_bytes,
                "staging_dir": str(self._downloader.staging_dir),
            }
        )

    async def _preview_mod_installer(self, archive_path: str) -> str:
        """Preview FOMOD options for a mod archive.

        Args:
            archive_path: Path to the mod archive.

        Returns:
            JSON string with FOMOD steps and options.
        """
        params = PreviewInstallerParams(archive_path=archive_path)

        if self._fomod_installer is None:
            return json.dumps({"error": "FOMOD installer is not configured"})

        try:
            preview = await self._fomod_installer.preview(
                pathlib.Path(params.archive_path)
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

        return json.dumps({
            "mod_name": preview.mod_name,
            "has_fomod": preview.has_fomod,
            "steps": preview.steps,
        })

    async def _install_mod_from_archive(
        self,
        archive_path: str,
        selections: dict[str, list[str]] | None = None,
    ) -> str:
        """Install a mod from archive into MO2.

        Args:
            archive_path: Path to the mod archive.
            selections: FOMOD selections.

        Returns:
            JSON string with installation result.
        """
        params = InstallFromArchiveParams(
            archive_path=archive_path,
            selections=selections or {},
        )

        if self._fomod_installer is None:
            return json.dumps({"error": "FOMOD installer is not configured"})

        mo2_mods_dir = self._mo2.root / "mods"

        try:
            result = await self._fomod_installer.install(
                archive_path=pathlib.Path(params.archive_path),
                mo2_mods_dir=mo2_mods_dir,
                selections=params.selections,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

        if result.installed:
            try:
                await self._mo2.add_mod_to_modlist(result.mod_name)
            except Exception as exc:
                result.errors.append(f"Failed to update modlist: {exc}")

        return json.dumps({
            "mod_name": result.mod_name,
            "installed": result.installed,
            "files_copied": result.files_copied,
            "pending_decisions": result.pending_decisions,
            "errors": result.errors,
        })

    async def _resolve_fomod(
        self,
        archive_path: str,
        selections: dict[str, list[str]] | None = None,
    ) -> str:
        """Resolve FOMOD options for a mod archive and return would-be installed files.

        Args:
            archive_path: Path to the mod archive.
            selections: FOMOD selections.

        Returns:
            JSON string with resolved files and pending decisions.
        """
        params = ResolveFomodParams(
            archive_path=archive_path,
            selections=selections or {},
        )

        if self._fomod_installer is None:
            return json.dumps({"error": "FOMOD installer is not configured"})

        from sky_claw.fomod.parser import parse_fomod_string, FomodParseError
        from sky_claw.fomod.resolver import FomodResolver

        archive = pathlib.Path(params.archive_path)
        # Use FomodInstaller's internal extraction capability
        if not hasattr(self._fomod_installer, "_extract_fomod_xml"):
            return json.dumps({"error": "FomodInstaller is missing _extract_fomod_xml capability."})

        # Ignoring type since it's an internal method being accessed
        fomod_xml = self._fomod_installer._extract_fomod_xml(archive) # type: ignore
        if fomod_xml is None:
            return json.dumps({"error": "No FOMOD configuration found in archive."})

        try:
            config = parse_fomod_string(fomod_xml)
        except FomodParseError as exc:
            return json.dumps({"error": f"FOMOD Parse Error: {exc}"})

        resolver = FomodResolver(config)
        result = resolver.resolve(params.selections)

        files = [str(f.source) for f in result.files]
        return json.dumps({
            "files_to_install": files,
            "pending_decisions": result.pending_decisions,
        })

    async def _setup_tools(
        self,
        tools: list[str] | None = None,
    ) -> str:
        """Download and install tools (loot, xedit, pandora, bodyslide).

        Args:
            tools: List of tools to install. Defaults to all.
        """
        params = SetupToolsParams(tools=tools or ["loot", "xedit", "pandora", "bodyslide"])

        if self._tools_installer is None:
            return json.dumps({"error": "Tools installer is not configured"})

        results: dict[str, Any] = {}

        async with aiohttp.ClientSession() as session:
            for tool_name in params.tools:
                tool_name_lower = tool_name.lower()
                try:
                    if tool_name_lower == "loot":
                        result = await self._tools_installer.ensure_loot(
                            self._install_dir, session
                        )
                        if not result.already_existed:
                            self._loot_exe = result.exe_path
                            if self._local_cfg:
                                self._local_cfg.loot_exe = str(result.exe_path)
                        results["loot"] = {
                            "status": "already_installed" if result.already_existed else "installed",
                            "exe_path": str(result.exe_path),
                            "version": result.version,
                        }
                    elif tool_name_lower == "xedit":
                        result = await self._tools_installer.ensure_xedit(
                            self._install_dir, session
                        )
                        if not result.already_existed and self._local_cfg:
                            self._local_cfg.xedit_exe = str(result.exe_path)
                        results["xedit"] = {
                            "status": "already_installed" if result.already_existed else "installed",
                            "exe_path": str(result.exe_path),
                            "version": result.version,
                        }
                    elif tool_name_lower == "pandora":
                        result = await self._tools_installer.ensure_pandora(
                            self._install_dir, session
                        )
                        if self._animation_hub:
                            self._animation_hub.pandora_exe = result.exe_path
                        if self._local_cfg:
                            self._local_cfg.pandora_exe = str(result.exe_path)
                        results["pandora"] = {
                            "status": "already_installed" if result.already_existed else "installed",
                            "exe_path": str(result.exe_path),
                            "version": result.version,
                        }
                    elif tool_name_lower == "bodyslide":
                        result = await self._tools_installer.ensure_bodyslide(
                            self._install_dir, session, self._downloader
                        )
                        if self._animation_hub:
                            self._animation_hub.bodyslide_exe = result.exe_path
                        if self._local_cfg:
                            self._local_cfg.bodyslide_exe = str(result.exe_path)
                        results["bodyslide"] = {
                            "status": "already_installed" if result.already_existed else "installed",
                            "exe_path": str(result.exe_path),
                            "version": result.version,
                        }
                    else:
                        results[tool_name] = {
                            "error": (
                                f"Unknown tool: {tool_name!r}. "
                                "Supported: loot, xedit, pandora, bodyslide"
                            )
                        }
                except ToolInstallError as exc:
                    results[tool_name_lower] = {"error": str(exc)}
                except Exception as exc:
                    logger.error("Failed to install %s: %s", tool_name, exc)
                    results[tool_name_lower] = {"error": str(exc)}

        # Persist configuration if tools were installed
        if self._local_cfg and self._config_path:
            save_local_config(self._local_cfg, self._config_path)

        return json.dumps(results)

    async def _analyze_esp_conflicts(
        self,
        profile: str,
        plugins: list[str] | None = None,
    ) -> str:
        """Analyze record-level conflicts between ESP plugins.

        Args:
            profile: MO2 profile name.
            plugins: Optional specific plugins. Defaults to all enabled.

        Returns:
            JSON string with conflict report and resolution suggestions.
        """
        params = AnalyzeConflictsParams(profile=profile, plugins=plugins)

        if self._xedit_runner is None:
            return json.dumps({
                "error": "xEdit runner is not configured. "
                         "Use the setup_tools tool to install SSEEdit first.",
            })

        # Resolve plugin list: use provided or collect from modlist.
        target_plugins = params.plugins
        if target_plugins is None:
            target_plugins = []
            async for mod_name, enabled in self._mo2.read_modlist(params.profile):
                if enabled and mod_name.endswith((".esp", ".esm", ".esl")):
                    target_plugins.append(mod_name)

        if not target_plugins:
            return json.dumps({
                "error": f"No plugins found for profile {params.profile!r}.",
            })

        analyzer = ConflictAnalyzer()
        try:
            report = await analyzer.analyze(target_plugins, self._xedit_runner)
        except XEditNotFoundError as exc:
            return json.dumps({
                "error": str(exc),
                "suggestion": "Use the setup_tools tool to install SSEEdit.",
            })
        except XEditValidationError as exc:
            return json.dumps({"error": str(exc)})
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)})

        suggestions = analyzer.suggest_resolution(report)
        result = report.to_dict()
        result["suggestions"] = suggestions
        return json.dumps(result)

    async def _run_pandora(self) -> str:
        """Run Pandora Behavior Engine."""
        if self._animation_hub is None:
            return json.dumps({"error": "AnimationHub is not configured."})
        result_msg = await self._animation_hub.run_pandora()
        return json.dumps({"status": result_msg})

    async def _run_bodyslide_batch(self) -> str:
        """Run Bodyslide in batch mode."""
        if self._animation_hub is None:
            return json.dumps({"error": "AnimationHub is not configured."})
        result_msg = await self._animation_hub.run_bodyslide_batch()
        return json.dumps({"status": result_msg})

    async def _uninstall_mod(self, mod_name: str, profile: str = "Default") -> str:
        """Uninstall a mod completely by deleting its files from MO2 and removing it from the modlist.

        Args:
            mod_name: The mod directory name.
            profile: MO2 profile name.

        Returns:
            JSON string confirming uninstallation.
        """
        params = ModNameParams(mod_name=mod_name, profile=profile)
        
        try:
            await self._mo2.remove_mod_from_modlist(params.mod_name, params.profile)
            await self._mo2.delete_mod_files(params.mod_name)
            
            return json.dumps({
                "mod_name": params.mod_name,
                "status": "uninstalled",
                "profile": params.profile
            })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def _toggle_mod(self, mod_name: str, enable: bool, profile: str = "Default") -> str:
        """Enable or disable an installed mod in a specific MO2 profile load order.

        Args:
            mod_name: The mod directory name.
            enable: True to enable, False to disable.
            profile: MO2 profile name.
            
        Returns:
            JSON string confirming the new state.
        """
        params = ToggleModParams(mod_name=mod_name, enable=enable, profile=profile)
        
        try:
            await self._mo2.toggle_mod_in_modlist(params.mod_name, params.profile, params.enable)
            
            state_str = "enabled" if params.enable else "disabled"
            return json.dumps({
                "mod_name": params.mod_name,
                "status": state_str,
                "profile": params.profile
            })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def _launch_game(self, profile: str = "Default") -> str:
        """Launch Skyrim Special Edition via MO2 using the SKSE executable.

        Args:
            profile: MO2 profile name.
            
        Returns:
            JSON string with launch status.
        """
        params = ProfileParams(profile=profile)
        
        try:
            result = await self._mo2.launch_game(params.profile)
            return json.dumps(result)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def _close_game(self) -> str:
        """Forcefully close Skyrim SE and MO2 to terminate the game.

        Returns:
            JSON string with close status.
        """
        try:
            result = await self._mo2.close_game()
            return json.dumps(result)
        except Exception as exc:
            return json.dumps({"error": str(exc)})
