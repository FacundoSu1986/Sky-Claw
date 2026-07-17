"""System tools for Sky-Claw agent.

Handlers for MO2 VFS, load order, conflict detection, and game control.
Extracted from tools.py as part of M-13 refactoring.

TASK-011 Tech Debt Cleanup: Removed redundant Pydantic instantiation from
all handlers.  Validation is now centralized in AsyncToolRegistry.execute()
via the tool's ``params_model``.  Handlers receive pre-validated arguments.
"""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
from typing import Any

from sky_claw.antigravity.security.hitl import Decision
from sky_claw.antigravity.security.sanitize import sanitize_for_prompt

logger = logging.getLogger(__name__)

#: Lock resource id para BodySlide (genera meshes en el overwrite/output). Serializa el
#: tool del agente contra otros mutadores, igual que LOAD_ORDER / BEHAVIOR_GRAPHS.
BODYSLIDE_MESHES_RESOURCE_ID = "bodyslide-meshes"


class _BashedPatchFailedError(Exception):
    """Interno: transporta el WryeBashResult fallido fuera del lock para que
    ``__aexit__`` restaure el snapshot del Bashed Patch previo."""

    def __init__(self, result: Any) -> None:
        super().__init__(f"Wrye Bash exit {result.return_code}")
        self.result = result


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


async def run_loot_sort(
    mo2: Any,
    loot_runner: Any,
    loot_exe: pathlib.Path | None,
    profile: str,
    *,
    lock_manager: Any | None = None,
    snapshot_manager: Any | None = None,
    path_validator: Any | None = None,
    journal: Any | None = None,
) -> str:
    """Invoke the LOOT CLI to sort the load order.

    Args are pre-validated by AsyncToolRegistry.execute() via ProfileParams.

    Audit #190: LOOT ``--sort`` mutates the shared load order. When the
    distributed lock is wired (production via ``app_context``), delegate to
    :class:`LootSortingService` so this live agent path serializes on the same
    ``load-order`` lock as the GUI orchestrator / dry-run preview — the
    cross-process lock only protects if every mutator participates. Without a
    lock manager (legacy callers / tests) the sort runs directly.

    PR #171 follow-up (same Codex P1 vector as pandora/bodyslide): ``loot_exe``
    is config-controlled (local_cfg / CLI args), so the lazily built runner
    gets ``path_validator`` — ``LOOTRunner.sort()`` validates the executable
    against the sandbox before any subprocess launch, exactly like the
    dry-run preview path already does (tool_dispatcher wires the same
    validator). A rejection surfaces through the existing error-JSON contract.

    T-26 (ADR 0002, follow-up de #243): cuando ``journal`` está cableado (via
    ``AsyncToolRegistry`` desde ``app_context``), se enhebra al
    ``LootSortingService`` para que este path del agente también emita+persista
    el ``ActionManifest`` ("caja negra de vuelo") antes de mutar — cerrando el
    hueco donde la emisión era un no-op fuera del path de la GUI/supervisor
    (review Codex #243 P1). Con ``journal=None`` el comportamiento no cambia.
    """
    if loot_runner is None and loot_exe is not None:
        try:
            from sky_claw.local.loot.cli import LOOTConfig, LOOTRunner

            config = LOOTConfig(loot_exe=loot_exe, game_path=mo2.root)
            loot_runner = LOOTRunner(config, path_validator=path_validator)
        except Exception as exc:
            return json.dumps({"error": str(exc)})
    if loot_runner is None:
        return json.dumps({"error": "LOOT runner is not configured"})

    if lock_manager is not None and snapshot_manager is not None:
        from sky_claw.local.tools.loot_service import LootSortingService

        service = LootSortingService(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            loot_runner=loot_runner,
            # Sin path_resolver en este path: loot_exe + mo2.root alimentan el
            # preflight perezoso — sin esto el guard era un no-op en el camino
            # del agente (review Codex PR #240 P1).
            loot_exe=loot_exe,
            mo2_root=getattr(mo2, "root", None),
            # T-26 (follow-up de #243): la caja negra de vuelo también en el
            # path del agente. None = comportamiento previo intacto.
            journal=journal,
        )
        # update_masterlist=False preserves the agent tool's prior no-network
        # behavior (ProfileParams has no masterlist flag).
        # LootSortingService converts lock contention / LOOTNotFound / timeout to
        # dicts; catch anything else (e.g. OSError on an unexecutable binary) so
        # the tool keeps its "always return JSON" contract (the lock is still
        # released by SnapshotTransactionLock.__aexit__ before the exception).
        try:
            res = await service.sort_load_order(update_masterlist=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        out: dict[str, Any] = {
            "profile": profile,
            "success": res.get("success", False),
            "return_code": res.get("return_code", -1),
            "sorted_plugins": res.get("sorted_plugins", []),
            "warnings": res.get("warnings", []),
            "errors": res.get("errors", []),
        }
        if not out["success"] and res.get("logs"):
            out["error"] = res["logs"]
        return json.dumps(out)

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


async def generate_bashed_patch(
    wrye_bash_runner: Any,
    *,
    lock_manager: Any | None = None,
    snapshot_manager: Any | None = None,
) -> str:
    """Generate 'Bashed Patch, 0.esp' using WryeBashRunner.

    NOTE: Plugin limit validation (M-04) is handled at the Supervisor level
    (execute_wrye_bash_pipeline). Here we only execute the runner.
    This handler is used when AsyncToolRegistry invokes the tool directly.

    §2.1 auditoría (mismo vector P1 que ``run_loot_sort``/``run_pandora``/
    ``run_bodyslide_batch``): Wrye Bash reescribe el Bashed Patch leyendo el
    load order completo, así que cuando el lock distribuido está cableado
    (producción vía ``app_context``) este path del agente serializa en el
    MISMO lock ``load-order`` que LOOT y el Ritual de la GUI — el lock
    cross-process solo protege si TODOS los mutadores participan. El ``.esp``
    previo se snapshotea para rollback si la generación muere a mitad de
    escritura. Sin lock manager (callers legacy / tests) corre directo,
    preservando el comportamiento anterior.
    """
    if wrye_bash_runner is None:
        return json.dumps({"error": "WryeBashRunner is not configured. Set WRYE_BASH_PATH."})

    if lock_manager is not None and snapshot_manager is not None:
        from sky_claw.antigravity.db.locks import LockAcquisitionError, SnapshotTransactionLock
        from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID
        from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME

        # Snapshot del .esp previo si existe (regeneración). getattr defensivo:
        # el runner llega tipado Any desde el registry. bashed_patch queda None
        # si no se pudo resolver game_path — guarda explícita para el cleanup
        # de más abajo (no asumir que "target_files vacío" implica el path resuelto).
        target_files: list[Any] = []
        bashed_patch: pathlib.Path | None = None
        config = getattr(wrye_bash_runner, "config", None)
        game_path = getattr(config, "game_path", None)
        if game_path is not None:
            bashed_patch = game_path / "Data" / BASHED_PATCH_NAME
            if bashed_patch.is_file():
                target_files = [bashed_patch]

        try:
            async with SnapshotTransactionLock(
                lock_manager=lock_manager,
                snapshot_manager=snapshot_manager,
                resource_id=LOAD_ORDER_RESOURCE_ID,
                agent_id="wrye-bash-tool",
                target_files=target_files,
                metadata={"source": "generate_bashed_patch"},
            ):
                result = await wrye_bash_runner.generate_bashed_patch()
                if not result.success:
                    # Lanzar DENTRO del lock: __aexit__ restaura el Bashed
                    # Patch previo en vez de dejar el .esp a medio escribir.
                    raise _BashedPatchFailedError(result)
        except LockAcquisitionError as exc:
            return json.dumps({"error": f"Could not acquire '{LOAD_ORDER_RESOURCE_ID}' lock: {exc}"})
        except _BashedPatchFailedError as exc:
            result = exc.result
            if not target_files and bashed_patch is not None:
                # Primera generación: sin .esp previo que snapshotear, así que
                # __aexit__ no restauró nada — el .esp corrupto/truncado que el
                # runner dejó a medio escribir quedaría persistente en Data/
                # (review Codex #316, mismo gap que el pipeline del supervisor).
                with contextlib.suppress(OSError):
                    bashed_patch.unlink(missing_ok=True)
        except Exception as exc:
            # Incluye LockLeaseLostError (heartbeat perdió el lease durante la
            # generación): __aexit__ no revierte ante lease loss, así que el
            # .esp queda en estado incierto — se reporta como error, no éxito.
            return json.dumps({"error": str(exc)})
    else:
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


async def run_pandora(
    pandora_runner: Any,
    *,
    lock_manager: Any | None = None,
    snapshot_manager: Any | None = None,
) -> str:
    """Execute Pandora Behavior Engine in auto mode (Skyrim SE) via PandoraRunner.

    Consolidation (obs #187): replaces the legacy AnimationHub-backed handler.
    The runner uses the unified ``_process`` helpers (timeout + kill-tree).

    Codex #213 (same P1 vector as ``run_loot_sort``/Audit #190): Pandora rewrites
    the shared behavior graphs, so when the distributed lock is wired (production
    via ``app_context``) this live agent path delegates to
    :class:`PandoraPipelineService` and serializes on the same ``behavior-graphs``
    lock as the GUI Ritual — the cross-process lock only protects if every mutator
    participates. Without a lock manager (legacy callers / tests) Pandora runs
    directly, preserving prior behavior.
    """
    if pandora_runner is None:
        return json.dumps(
            {"error": ("PandoraRunner is not configured. Set pandora_exe in config or install it via setup_tools.")}
        )

    if lock_manager is not None and snapshot_manager is not None:
        from sky_claw.local.tools.pandora_service import PandoraPipelineService

        service = PandoraPipelineService(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            pandora_runner=pandora_runner,
        )
        # The service serializes on the behavior-graphs lock and converts lock
        # contention / execution failures to a dict; map it to the tool's JSON
        # contract. A non-success carries the detail under ``logs``.
        try:
            res = await service.generate_animations()
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        out: dict[str, Any] = {
            "success": res.get("success", False),
            "return_code": res.get("return_code", -1),
            "stdout": sanitize_for_prompt(str(res.get("stdout", ""))) if res.get("stdout") else "",
            "stderr": sanitize_for_prompt(str(res.get("stderr", ""))) if res.get("stderr") else "",
            "duration_seconds": res.get("duration_seconds", 0.0),
        }
        if not out["success"] and res.get("logs"):
            out["error"] = res["logs"]
        return json.dumps(out)

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


async def run_bodyslide_batch(
    bodyslide_runner: Any,
    group: str = "CBBE",
    output_path: str = "meshes",
    *,
    lock_manager: Any | None = None,
    snapshot_manager: Any | None = None,
) -> str:
    """Execute BodySlide in batch mode via BodySlideRunner.

    Args are pre-validated by AsyncToolRegistry.execute() via BodySlideBatchParams.

    Consolidation (obs #187): replaces the legacy AnimationHub-backed handler
    that hardcoded the "CBBE Body Physics" preset; ``group`` is configurable.

    Codex #213 (same P1 vector as ``run_loot_sort``/``run_pandora``): BodySlide
    batch-builds meshes into the overwrite/output, so when the distributed lock is
    wired (production via ``app_context``) this agent path runs under the shared
    ``bodyslide-meshes`` lock — the cross-process lock only protects if every mutator
    participates. ``target_files=[]`` (serialize only): the output path is
    environment-dependent, so a blind snapshot would be a false safety net. Without a
    lock manager (legacy callers / tests) BodySlide runs directly, preserving prior behavior.
    """
    if bodyslide_runner is None:
        return json.dumps(
            {"error": ("BodySlideRunner is not configured. Set bodyslide_exe in config or install it via setup_tools.")}
        )

    if lock_manager is not None and snapshot_manager is not None:
        from sky_claw.antigravity.db.locks import LockAcquisitionError, SnapshotTransactionLock

        try:
            async with SnapshotTransactionLock(
                lock_manager=lock_manager,
                snapshot_manager=snapshot_manager,
                resource_id=BODYSLIDE_MESHES_RESOURCE_ID,
                agent_id="bodyslide-tool",
                target_files=[],
                metadata={"source": "bodyslide_batch", "group": group},
            ):
                result = await bodyslide_runner.run_batch(group, output_path)
        except LockAcquisitionError as exc:
            return json.dumps({"error": f"Could not acquire '{BODYSLIDE_MESHES_RESOURCE_ID}' lock: {exc}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})
    else:
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
