"""System tools for Sky-Claw agent.

Handlers for MO2 VFS, load order, conflict detection, and game control.
Extracted from tools.py as part of M-13 refactoring.

TASK-011 Tech Debt Cleanup: Removed redundant Pydantic instantiation from
all handlers.  Validation is now centralized in AsyncToolRegistry.execute()
via the tool's ``params_model``.  Handlers receive pre-validated arguments.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

from sky_claw.antigravity.security.hitl import Decision
from sky_claw.antigravity.security.sanitize import sanitize_for_prompt

logger = logging.getLogger(__name__)


async def check_load_order(mo2: Any, profile: str) -> str:
    """Read the MO2 modlist for a profile.

    Args are pre-validated by AsyncToolRegistry.execute() via ProfileParams.
    """
    entries: list[dict[str, Any]] = []
    idx = 0
    async for mod_name, enabled in mo2.read_modlist(profile):
        entries.append({"index": idx, "name": mod_name, "enabled": enabled})
        idx += 1
    return json.dumps({"profile": profile, "load_order": entries})


async def detect_conflicts(registry: Any, mo2: Any, profile: str) -> str:
    """Detect missing-master conflicts among active ESPs.

    Args are pre-validated by AsyncToolRegistry.execute() via ProfileParams.
    """
    enabled_mods: list[str] = []
    async for mod_name, enabled in mo2.read_modlist(profile):
        if enabled:
            enabled_mods.append(mod_name)
    conflicts = await registry.find_missing_masters_for_mods(enabled_mods)
    return json.dumps({"profile": profile, "conflicts": conflicts})


async def run_loot_sort(mo2: Any, loot_runner: Any, loot_exe: pathlib.Path | None, profile: str) -> str:
    """Invoke the LOOT CLI to sort the load order.

    Args are pre-validated by AsyncToolRegistry.execute() via ProfileParams.
    """
    if loot_runner is None and loot_exe is not None:
        try:
            from sky_claw.local.loot.cli import LOOTConfig, LOOTRunner

            config = LOOTConfig(loot_exe=loot_exe, game_path=mo2.root)
            loot_runner = LOOTRunner(config)
        except Exception as exc:
            return json.dumps({"error": str(exc)})
    if loot_runner is None:
        return json.dumps({"error": "LOOT runner is not configured"})
    try:
        result = await loot_runner.sort()
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


async def run_xedit_script(xedit_runner: Any, script_name: str, plugins: list[str]) -> str:
    """Run an xEdit script in headless mode.

    Args are pre-validated by AsyncToolRegistry.execute() via XEditAnalysisParams.

    SECURITY: XEditRunner uses asyncio.create_subprocess_exec() with
    argument list (shell=False equivalent). Input validation is delegated to
    XEditRunner._validate_inputs() which enforces strict regex patterns.
    No shell quoting needed - raw strings are passed safely.
    """
    if xedit_runner is None:
        return json.dumps({"error": "xEdit runner is not configured"})
    try:
        result = await xedit_runner.run_script(script_name, plugins)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        {
            "success": result.success,
            "return_code": result.return_code,
            "processed_plugins": result.processed_plugins,
            "conflicts": [{"plugin": c.plugin, "record": c.record, "detail": c.detail} for c in result.conflicts],
            "errors": result.errors,
        }
    )


async def preview_mod_installer(fomod_installer: Any, archive_path: str) -> str:
    """Preview FOMOD options for a mod archive.

    Args are pre-validated by AsyncToolRegistry.execute() via PreviewInstallerParams.
    """
    if fomod_installer is None:
        return json.dumps({"error": "FOMOD installer is not configured"})
    try:
        preview = await fomod_installer.preview(pathlib.Path(archive_path))
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        {
            "mod_name": preview.mod_name,
            "has_fomod": preview.has_fomod,
            "steps": preview.steps,
        }
    )


async def install_mod_from_archive(
    mo2: Any,
    fomod_installer: Any,
    hitl: Any,
    archive_path: str,
    selections: dict[str, list[str]] | None = None,
) -> str:
    """Install a mod from archive into MO2 with mandatory HITL approval.

    Args are pre-validated by AsyncToolRegistry.execute() via InstallFromArchiveParams.
    """
    if hitl is None:
        return json.dumps({"error": "HITL guard is not configured. Installation blocked."})
    request_id = f"install-{pathlib.Path(archive_path).name}"
    # Decision already imported at module level (HOTFIX: removed dynamic import)
    decision = await hitl.request_approval(
        request_id=request_id,
        reason=f"Confirmar instalación de mod: {pathlib.Path(archive_path).name}",
        detail=f"Selecciones FOMOD detectadas: {json.dumps(selections or {})}",
    )
    if decision is not Decision.APPROVED:
        return json.dumps({"status": "denied", "reason": "User rejected the installation."})
    if fomod_installer is None:
        return json.dumps({"error": "FOMOD installer is not configured"})
    mo2_mods_dir = mo2.root / "mods"
    try:
        result = await fomod_installer.install(
            archive_path=pathlib.Path(archive_path),
            mo2_mods_dir=mo2_mods_dir,
            selections=selections or {},
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    if result.installed:
        try:
            await mo2.add_mod_to_modlist(result.mod_name)
        except Exception as exc:
            result.errors.append(f"Failed to update modlist: {exc}")
    return json.dumps(
        {
            "mod_name": result.mod_name,
            "installed": result.installed,
            "files_copied": result.files_copied,
            "pending_decisions": result.pending_decisions,
            "errors": result.errors,
        }
    )


async def resolve_fomod(
    fomod_installer: Any,
    archive_path: str,
    selections: dict[str, list[str]] | None = None,
) -> str:
    """Resolve FOMOD options for a mod archive and return would-be installed files.

    Args are pre-validated by AsyncToolRegistry.execute() via ResolveFomodParams.
    """
    if fomod_installer is None:
        return json.dumps({"error": "FOMOD installer is not configured"})
    from sky_claw.local.fomod.parser import FomodParseError, parse_fomod_string
    from sky_claw.local.fomod.resolver import FomodResolver

    archive = pathlib.Path(archive_path)
    if not hasattr(fomod_installer, "_extract_fomod_xml"):
        return json.dumps({"error": "FomodInstaller is missing _extract_fomod_xml capability."})
    fomod_xml = fomod_installer._extract_fomod_xml(archive)
    if fomod_xml is None:
        return json.dumps({"error": "No FOMOD configuration found in archive."})
    try:
        config = parse_fomod_string(fomod_xml)
    except FomodParseError as exc:
        return json.dumps({"error": f"FOMOD Parse Error: {exc}"})
    resolver = FomodResolver(config)
    result = resolver.resolve(selections or {})
    files = [str(f.source) for f in result.files]
    return json.dumps(
        {
            "files_to_install": files,
            "pending_decisions": result.pending_decisions,
        }
    )


async def analyze_esp_conflicts(
    mo2: Any,
    xedit_runner: Any,
    profile: str,
    plugins: list[str] | None = None,
) -> str:
    """Analyze record-level conflicts between ESP plugins.

    Args are pre-validated by AsyncToolRegistry.execute() via AnalyzeConflictsParams.
    """
    if xedit_runner is None:
        return json.dumps(
            {
                "error": "xEdit runner is not configured. Use the setup_tools tool to install SSEEdit first.",
            }
        )
    target_plugins = plugins
    if target_plugins is None:
        target_plugins = []
        async for mod_name, enabled in mo2.read_modlist(profile):
            if enabled and mod_name.endswith((".esp", ".esm", ".esl")):
                target_plugins.append(mod_name)
    if not target_plugins:
        return json.dumps({"error": f"No plugins found for profile {profile!r}."})
    from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer
    from sky_claw.local.xedit.runner import XEditNotFoundError, XEditValidationError

    analyzer = ConflictAnalyzer()
    try:
        report = await analyzer.analyze(target_plugins, xedit_runner)
    except XEditNotFoundError as exc:
        return json.dumps(
            {
                "error": str(exc),
                "suggestion": "Use the setup_tools tool to install SSEEdit.",
            }
        )
    except XEditValidationError as exc:
        return json.dumps({"error": str(exc)})
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})
    suggestions = analyzer.suggest_resolution(report)
    result = report.to_dict()
    result["suggestions"] = suggestions
    return json.dumps(result)


async def run_pandora(animation_hub: Any) -> str:
    """Run Pandora Behavior Engine."""
    if animation_hub is None:
        return json.dumps({"error": "AnimationHub is not configured."})
    result = await animation_hub.run_pandora()
    return json.dumps(result)


async def run_bodyslide_batch(animation_hub: Any) -> str:
    """Run Bodyslide in batch mode."""
    if animation_hub is None:
        return json.dumps({"error": "AnimationHub is not configured."})
    result = await animation_hub.run_bodyslide_batch()
    return json.dumps(result)


async def uninstall_mod(mo2: Any, mod_name: str, profile: str = "Default") -> str:
    """Uninstall a mod completely by deleting its files from MO2.

    Args are pre-validated by AsyncToolRegistry.execute() via UninstallModParams.
    """
    try:
        await mo2.remove_mod_from_modlist(mod_name, profile)
        await mo2.delete_mod_files(mod_name)
        return json.dumps(
            {
                "mod_name": mod_name,
                "status": "uninstalled",
                "profile": profile,
            }
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def toggle_mod(mo2: Any, mod_name: str, enable: bool, profile: str = "Default") -> str:
    """Enable or disable an installed mod in a specific MO2 profile load order.

    Args are pre-validated by AsyncToolRegistry.execute() via ToggleModParams.
    """
    try:
        await mo2.toggle_mod_in_modlist(mod_name, profile, enable)
        state_str = "enabled" if enable else "disabled"
        return json.dumps(
            {
                "mod_name": mod_name,
                "status": state_str,
                "profile": profile,
            }
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def launch_game(mo2: Any, profile: str = "Default") -> str:
    """Launch Skyrim Special Edition via MO2 using SKSE.

    Args are pre-validated by AsyncToolRegistry.execute() via LaunchGameParams.
    """
    try:
        result = await mo2.launch_game(profile)
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def close_game(mo2: Any) -> str:
    """Forcefully close Skyrim SE and MO2."""
    try:
        result = await mo2.close_game()
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# FASE 6: Direct runner handlers
# ---------------------------------------------------------------------------


async def generate_bashed_patch(wrye_bash_runner: Any) -> str:
    """Generate 'Bashed Patch, 0.esp' using WryeBashRunner.

    NOTE: Plugin limit validation (M-04) is handled at the Supervisor level
    (execute_wrye_bash_pipeline). Here we only execute the runner.
    This handler is used when AsyncToolRegistry invokes the tool directly.
    """
    if wrye_bash_runner is None:
        return json.dumps({"error": "WryeBashRunner is not configured. Set WRYE_BASH_PATH."})
    try:
        result = await wrye_bash_runner.generate_bashed_patch()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        {
            "success": result.success,
            "return_code": result.return_code,
            "stdout": sanitize_for_prompt(result.stdout) if result.stdout else "",
            "stderr": sanitize_for_prompt(result.stderr) if result.stderr else "",
            "duration_seconds": result.duration_seconds,
        }
    )


async def run_pandora_behavior(pandora_runner: Any) -> str:
    """Execute Pandora Behavior Engine in auto mode (Skyrim SE) via PandoraRunner."""
    if pandora_runner is None:
        return json.dumps({"error": "PandoraRunner is not configured. Set PANDORA_EXE."})
    try:
        result = await pandora_runner.run_pandora()
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        {
            "success": result.success,
            "return_code": result.return_code,
            "stdout": sanitize_for_prompt(result.stdout) if result.stdout else "",
            "stderr": sanitize_for_prompt(result.stderr) if result.stderr else "",
            "duration_seconds": result.duration_seconds,
        }
    )


async def run_bodyslide_batch_direct(
    bodyslide_runner: Any,
    group: str = "CBBE",
    output_path: str = "meshes",
) -> str:
    """Execute BodySlide in batch mode via BodySlideRunner.

    Args are pre-validated by AsyncToolRegistry.execute() via BodySlideBatchParams.
    """
    if bodyslide_runner is None:
        return json.dumps({"error": "BodySlideRunner is not configured. Set BODYSLIDE_EXE."})
    try:
        result = await bodyslide_runner.run_batch(group, output_path)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        {
            "success": result.success,
            "return_code": result.return_code,
            "stdout": sanitize_for_prompt(result.stdout) if result.stdout else "",
            "stderr": sanitize_for_prompt(result.stderr) if result.stderr else "",
            "duration_seconds": result.duration_seconds,
        }
    )
