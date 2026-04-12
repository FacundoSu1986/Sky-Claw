"""External tools installer for Sky-Claw agent.

Handler for downloading and installing modding tools (LOOT, xEdit, Pandora, BodySlide).
Extracted from tools.py as part of M-13 refactoring.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

import aiohttp

from .schemas import SetupToolsParams

logger = logging.getLogger(__name__)


async def setup_tools(
    tools_installer: Any,
    install_dir: pathlib.Path,
    local_cfg: Any | None,
    config_path: pathlib.Path | None,
    animation_hub: Any | None,
    downloader: Any | None,
    loot_exe_ref: list | None = None,
    tools: list[str] | None = None,
) -> str:
    """Download and install tools (loot, xedit, pandora, bodyslide).

    Args:
        tools_installer: ToolsInstaller instance.
        install_dir: Installation directory for tools.
        local_cfg: LocalConfig instance (may be None).
        config_path: Path to local config file.
        animation_hub: AnimationHub instance (may be None).
        downloader: NexusDownloader instance (may be None).
        loot_exe_ref: Optional list to store loot_exe path reference.
        tools: List of tools to install. Defaults to all.

    Returns:
        JSON string with installation results.
    """
    from sky_claw.tools_installer import ToolInstallError
    from sky_claw.local_config import save as save_local_config

    params = SetupToolsParams(tools=tools or ["loot", "xedit", "pandora", "bodyslide"])

    if tools_installer is None:
        return json.dumps({"error": "Tools installer is not configured"})

    results: dict[str, Any] = {}

    async with aiohttp.ClientSession() as session:
        for tool_name in params.tools:
            tool_name_lower = tool_name.lower()
            try:
                if tool_name_lower == "loot":
                    result = await tools_installer.ensure_loot(install_dir, session)
                    if not result.already_existed and loot_exe_ref is not None:
                        loot_exe_ref[0] = result.exe_path
                    if local_cfg:
                        local_cfg.loot_exe = str(result.exe_path)
                    results["loot"] = {
                        "status": "already_installed"
                        if result.already_existed
                        else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                    }
                elif tool_name_lower == "xedit":
                    result = await tools_installer.ensure_xedit(install_dir, session)
                    if not result.already_existed and local_cfg:
                        local_cfg.xedit_exe = str(result.exe_path)
                    results["xedit"] = {
                        "status": "already_installed"
                        if result.already_existed
                        else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                    }
                elif tool_name_lower == "pandora":
                    result = await tools_installer.ensure_pandora(install_dir, session)
                    if animation_hub:
                        animation_hub.pandora_exe = result.exe_path
                    if local_cfg:
                        local_cfg.pandora_exe = str(result.exe_path)
                    results["pandora"] = {
                        "status": "already_installed"
                        if result.already_existed
                        else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                    }
                elif tool_name_lower == "bodyslide":
                    result = await tools_installer.ensure_bodyslide(
                        install_dir, session, downloader
                    )
                    if animation_hub:
                        animation_hub.bodyslide_exe = result.exe_path
                    if local_cfg:
                        local_cfg.bodyslide_exe = str(result.exe_path)
                    results["bodyslide"] = {
                        "status": "already_installed"
                        if result.already_existed
                        else "installed",
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
    if local_cfg and config_path:
        save_local_config(local_cfg, config_path)

    return json.dumps(results)
