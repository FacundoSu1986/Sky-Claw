"""External tools installer for Sky-Claw agent.

Handler for downloading and installing modding tools (LOOT, xEdit, Pandora, BodySlide).
Extracted from tools.py as part of M-13 refactoring.

TASK-011 Tech Debt Cleanup: Removed redundant Pydantic instantiation.
Validation is now centralized in AsyncToolRegistry.execute().
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from sky_claw.antigravity.security.network_gateway import GatewayTCPConnector, NetworkGateway

if TYPE_CHECKING:
    import pathlib

logger = logging.getLogger(__name__)


async def setup_tools(
    tools_installer: Any,
    install_dir: pathlib.Path,
    local_cfg: Any | None,
    config_path: pathlib.Path | None,
    downloader: Any | None,
    tools: list[str] | None = None,
    *,
    gateway: NetworkGateway | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Download and install tools (loot, xedit, pandora, bodyslide).

    Args are pre-validated by AsyncToolRegistry.execute() via SetupToolsParams.

    Consolidation (obs #187): the ``animation_hub`` parameter was removed.
    Installed tool paths are persisted in ``local_cfg``, which the
    AsyncToolRegistry resolvers read at call time — updating an attribute
    on a hub instance was a no-op (the hub read from a frozen config). The
    ``loot_exe_ref`` list parameter was removed for the same reason: callers
    passed a fresh list per invocation, so the post-install mutation never
    propagated; LOOT now follows the same call-time ``local_cfg`` resolution.

    Args:
        tools_installer: ToolsInstaller instance.
        install_dir: Installation directory for tools.
        local_cfg: LocalConfig instance (may be None).
        config_path: Path to local config file.
        downloader: NexusDownloader instance (may be None).
        tools: List of tools to install. Defaults to all.

    Returns:
        JSON string with installation results.
    """
    from sky_claw.local.local_config import save as save_local_config
    from sky_claw.local.tools_installer import ToolInstallError

    if tools_installer is None:
        return json.dumps({"error": "Tools installer is not configured"})

    results: dict[str, Any] = {}

    # TASK-013 P1: Zero-Trust egress policy — a missing NetworkGateway means
    # the integration layer is misconfigured. Abort immediately rather than
    # degrade to an unprotected session that bypasses SSRF/allow-list defences.
    # NOTE: This check is unconditional — even an injected `session` is rejected
    # when gateway=None, preventing a false-success path where the installer
    # runs with an unvetted session that bypasses SSRF/allow-list enforcement.
    if gateway is None:
        logger.error("setup_tools called without NetworkGateway — aborting (Zero-Trust policy)")
        return json.dumps(
            {"error": ("NetworkGateway is required for all egress. Configure the gateway before calling this tool.")}
        )

    own_session = False
    if session is None:
        session = aiohttp.ClientSession(
            connector=GatewayTCPConnector(gateway, limit=10),
        )
        own_session = True

    try:
        for tool_name in tools or ["loot", "xedit", "pandora", "bodyslide"]:
            tool_name_lower = tool_name.lower()
            try:
                if tool_name_lower == "loot":
                    result = await tools_installer.ensure_loot(install_dir, session)
                    if local_cfg:
                        local_cfg.loot_exe = str(result.exe_path)
                    results["loot"] = {
                        "status": "already_installed" if result.already_existed else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                    }
                elif tool_name_lower == "xedit":
                    result = await tools_installer.ensure_xedit(install_dir, session)
                    if not result.already_existed and local_cfg:
                        local_cfg.xedit_exe = str(result.exe_path)
                    results["xedit"] = {
                        "status": "already_installed" if result.already_existed else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                    }
                elif tool_name_lower == "pandora":
                    result = await tools_installer.ensure_pandora(install_dir, session)
                    if local_cfg:
                        local_cfg.pandora_exe = str(result.exe_path)
                    results["pandora"] = {
                        "status": "already_installed" if result.already_existed else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                    }
                elif tool_name_lower == "bodyslide":
                    result = await tools_installer.ensure_bodyslide(install_dir, session, downloader)
                    if local_cfg:
                        local_cfg.bodyslide_exe = str(result.exe_path)
                    results["bodyslide"] = {
                        "status": "already_installed" if result.already_existed else "installed",
                        "exe_path": str(result.exe_path),
                        "version": result.version,
                    }
                else:
                    results[tool_name] = {
                        "error": (f"Unknown tool: {tool_name!r}. Supported: loot, xedit, pandora, bodyslide")
                    }
            except ToolInstallError as exc:
                results[tool_name_lower] = {"error": str(exc)}
            except Exception as exc:
                logger.error("Failed to install %s: %s", tool_name, exc)
                results[tool_name_lower] = {"error": str(exc)}

    finally:
        if own_session and session and not session.closed:
            await session.close()

    # Persist configuration if tools were installed
    # RND-02: Delegar I/O síncrono a thread pool para no bloquear el event loop
    if local_cfg and config_path:
        await asyncio.to_thread(save_local_config, local_cfg, config_path)

    return json.dumps(results)
