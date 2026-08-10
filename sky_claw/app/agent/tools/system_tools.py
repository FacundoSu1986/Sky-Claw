"""System tools for Sky-Claw agent.

Handlers for MO2 VFS, load order, conflict detection, and game control.
Extracted from tools.py as part of M-13 refactoring.

TASK-011 Tech Debt Cleanup: Removed redundant Pydantic instantiation from
all handlers.  Validation is now centralized in AsyncToolRegistry.execute()
via the tool's ``params_model``.  Handlers receive pre-validated arguments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any

from sky_claw.app.core.models import LootExecutionParams
from sky_claw.app.security.hitl import Decision
from sky_claw.app.security.sanitize import sanitize_for_prompt
from sky_claw.local.tools.output_targets import BODYSLIDE_MESHES_RESOURCE_ID

logger = logging.getLogger(__name__)


async def check_load_order(mo2: Any, *, profile: str) -> str:
    """Read the MO2 modlist for the session profile.

    ``profile`` es keyword-only y sin default, como toda la familia — ver
    :func:`uninstall_mod`. La tool ya no lo expone al LLM.
    """
    entries: list[dict[str, Any]] = []
    idx = 0
    async for mod_name, enabled in mo2.read_modlist(profile):
        entries.append({"index": idx, "name": mod_name, "enabled": enabled})
        idx += 1
    return json.dumps({"profile": profile, "load_order": entries})


async def detect_conflicts(registry: Any, mo2: Any, *, profile: str) -> str:
    """Detect missing-master conflicts among active ESPs.

    ``profile`` es keyword-only y sin default, como toda la familia — ver
    :func:`uninstall_mod`. La tool ya no lo expone al LLM.
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
    *,
    profile: str,
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
            res = await service.sort_load_order(LootExecutionParams(profile_name=profile, update_masterlist=False))
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

    # Rama sin lock (legacy/tests; producción siempre cablea lock+snapshot y va por
    # el servicio de arriba). Rebindear el runner al perfil pedido ANTES de ordenar:
    # un `BrokeredLootRunner` viene atado al perfil con el que se construyó, así que
    # sin esto se ordenaba ese y el payload informaba el pedido — el resultado decía
    # un perfil sobre el que no actuó (review CodeRabbit #460). `for_profile`
    # devuelve `self` cuando coinciden, así que el caso normal no paga nada.
    # La detección sale del alias público de `loot_service`, que es su fuente única:
    # duplicarla acá reabriría el desfasaje que ese helper existe para cerrar.
    from sky_claw.local.tools.loot_service import es_runner_por_perfil

    if es_runner_por_perfil(loot_runner):
        loot_runner = loot_runner.for_profile(profile)

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

    Contrato canónico de tools (AGENTS.md): ``success`` + ``message`` (vacío en
    éxito) más los campos estructurados del preview.
    """
    if fomod_installer is None:
        return json.dumps({"success": False, "message": "FOMOD installer is not configured."})
    try:
        preview = await fomod_installer.preview(pathlib.Path(archive_path))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # frontera deliberada: JSON para el LLM, nunca propagar crudo
        logger.exception("preview_mod_installer falló para %s", archive_path)
        return json.dumps({"success": False, "message": sanitize_for_prompt(str(exc), max_length=256)})
    return json.dumps(
        {
            "success": True,
            "message": "",
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
    *,
    profile: str,
    lock_manager: Any | None = None,
) -> str:
    """Install a mod from archive into MO2 with mandatory HITL approval.

    Args are pre-validated by AsyncToolRegistry.execute() via InstallFromArchiveParams.

    Contrato canónico de tools (AGENTS.md): ``success`` + ``message`` (vacío en
    éxito). El rechazo del operador conserva ``status="denied"`` como estado
    estructurado; el fallo parcial (archivos copiados pero modlist no
    actualizado) se reporta como fallo sin perder los campos estructurados.

    Con ``lock_manager`` cableado (producción vía ``app_context``), todo el
    flujo mutante (extracción+copia en ``mods/`` y ``modlist.txt``) corre bajo
    el lock cross-process de la familia T-31 — el lock solo protege si TODOS
    los mutadores participan. Sin lock (legacy/tests) el flujo corre directo,
    igual que ``run_loot_sort`` sin lock_manager.
    """
    if hitl is None:
        return json.dumps({"success": False, "message": "HITL guard is not configured. Installation blocked."})
    request_id = f"install-{pathlib.Path(archive_path).name}"
    # Decision ya está importado a nivel de módulo (HOTFIX: se eliminó el import dinámico).
    try:
        decision = await hitl.request_approval(
            request_id=request_id,
            reason=f"Confirmar instalación de mod: {pathlib.Path(archive_path).name}",
            detail=f"Selecciones FOMOD detectadas: {json.dumps(selections or {})}",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # frontera deliberada: JSON para el LLM, nunca propagar crudo
        logger.exception("Solicitud HITL falló para %s", archive_path)
        return json.dumps({"success": False, "message": sanitize_for_prompt(str(exc), max_length=256)})
    if decision is not Decision.APPROVED:
        return json.dumps(
            {
                "success": False,
                "status": "denied",
                "message": "Instalación rechazada por el operador.",
            }
        )
    if fomod_installer is None:
        return json.dumps({"success": False, "message": "FOMOD installer is not configured."})

    mo2_mods_dir = mo2.root / "mods"
    if lock_manager is not None:
        # API pública de la familia de instalación (T-31): el mismo recurso que
        # los autoinstaladores de tools sobre mods/.
        from sky_claw.local.tools_installer import bajo_lock_de_instalacion, install_lock_resource_id

        async with bajo_lock_de_instalacion(lock_manager, install_lock_resource_id(mo2_mods_dir)):
            return await _instalar_con_contrato(
                mo2,
                fomod_installer,
                archive_path,
                selections,
                mo2_mods_dir,
                profile,
            )
    return await _instalar_con_contrato(mo2, fomod_installer, archive_path, selections, mo2_mods_dir, profile)


async def _instalar_con_contrato(
    mo2: Any,
    fomod_installer: Any,
    archive_path: str,
    selections: dict[str, list[str]] | None,
    mo2_mods_dir: pathlib.Path,
    profile: str,
) -> str:
    """Ejecuta la instalación FOMOD y devuelve el JSON con contrato canónico.

    TODO error que cruza al LLM pasa por ``sanitize_for_prompt`` (un nombre de
    archivo o un mensaje de excepción del sistema puede contener texto no
    confiable).
    """
    try:
        result = await fomod_installer.install(
            archive_path=pathlib.Path(archive_path),
            mo2_mods_dir=mo2_mods_dir,
            selections=selections or {},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # frontera deliberada: JSON para el LLM, nunca propagar crudo
        logger.exception("Instalación falló para %s", archive_path)
        return json.dumps({"success": False, "message": sanitize_for_prompt(str(exc), max_length=256)})

    errores_saneados = [sanitize_for_prompt(str(e), max_length=256) for e in result.errors]

    if result.installed:
        try:
            await mo2.add_mod_to_modlist(result.mod_name, profile=profile)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Registro en modlist falló para %s", result.mod_name)
            # Fallo parcial: los archivos quedaron en mods/ pero el mod no se
            # activa en MO2 — se reporta como fallo conservando los campos
            # estructurados para diagnóstico.
            error_modlist = sanitize_for_prompt(f"modlist: {exc}", max_length=256)
            return json.dumps(
                {
                    "success": False,
                    "message": sanitize_for_prompt(
                        f"El mod se instaló pero no se registró en modlist.txt: {exc}",
                        max_length=256,
                    ),
                    "mod_name": result.mod_name,
                    "files_copied": result.files_copied,
                    "pending_decisions": result.pending_decisions,
                    "errors": [error_modlist],
                }
            )

    return json.dumps(
        {
            "success": result.installed,
            "message": (
                ""
                if result.installed
                else "; ".join(errores_saneados) or "La instalación no copió archivos (faltan decisiones FOMOD?)."
            ),
            "mod_name": result.mod_name,
            "installed": result.installed,
            "files_copied": result.files_copied,
            "pending_decisions": result.pending_decisions,
            "errors": errores_saneados,
        }
    )


async def resolve_fomod(
    fomod_installer: Any,
    archive_path: str,
    selections: dict[str, list[str]] | None = None,
) -> str:
    """Resolve FOMOD options for a mod archive and return would-be installed files.

    Args are pre-validated by AsyncToolRegistry.execute() via ResolveFomodParams.

    Contrato canónico de tools (AGENTS.md): ``success`` + ``message`` (vacío en
    éxito) más los campos estructurados de la resolución.
    """
    if fomod_installer is None:
        return json.dumps({"success": False, "message": "FOMOD installer is not configured."})
    from sky_claw.app.core.errors import FomodParserSecurityError
    from sky_claw.local.fomod.parser import FomodParseError, parse_fomod_string
    from sky_claw.local.fomod.resolver import FomodResolver

    archive = pathlib.Path(archive_path)
    if not hasattr(fomod_installer, "read_fomod_xml"):
        return json.dumps({"success": False, "message": "FomodInstaller is missing read_fomod_xml capability."})
    try:
        # RND-03: la lectura del archive (zip/7z/rar) es I/O síncrono — fuera
        # del event loop.
        fomod_xml = await asyncio.to_thread(fomod_installer.read_fomod_xml, archive)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # frontera deliberada: JSON para el LLM, nunca propagar crudo
        logger.exception("Lectura del FOMOD falló para %s", archive_path)
        return json.dumps({"success": False, "message": sanitize_for_prompt(str(exc), max_length=256)})
    if fomod_xml is None:
        return json.dumps({"success": False, "message": "El archive no contiene fomod/ModuleConfig.xml."})
    try:
        config = parse_fomod_string(fomod_xml)
    except (FomodParseError, FomodParserSecurityError) as exc:
        # FomodParserSecurityError (XXE/entidades prohibidas) NO es subclase de
        # FomodParseError: sin este catch el XML malicioso reventaría la tool.
        return json.dumps(
            {"success": False, "message": sanitize_for_prompt(f"FOMOD Parse Error: {exc}", max_length=256)}
        )
    resolver = FomodResolver(
        config,
        # El installer conoce el proveedor de estado de plugins (fail-closed si
        # no está cableado): las fileDependency se evalúan contra la instalación.
        file_state=getattr(fomod_installer, "file_state_provider", None),
    )
    # RND-03: la resolución puede escanear mods/ (proveedor de estado) — I/O
    # síncrono fuera del event loop.
    result = await asyncio.to_thread(resolver.resolve, selections or {})
    files = [str(f.source) for f in result.files]
    return json.dumps(
        {
            "success": True,
            "message": "",
            "files_to_install": files,
            "pending_decisions": result.pending_decisions,
        }
    )


async def analyze_esp_conflicts(
    mo2: Any,
    xedit_runner: Any,
    *,
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


async def uninstall_mod(mo2: Any, mod_name: str, *, profile: str) -> str:
    """Uninstall a mod completely by deleting its files from MO2.

    Args are pre-validated by AsyncToolRegistry.execute() via UninstallModParams.

    ``profile`` es keyword-only y SIN default a propósito: el default silencioso
    `"Default"` era lo que hacía que esta tool quitara la entrada del perfil
    equivocado mientras `delete_mod_files` borraba igual — dejando el modlist del
    perfil de sesión apuntando a un directorio inexistente. El perfil sale de
    ``AsyncToolRegistry(mo2_profile=…)``; un caller que no lo pase falla al invocar,
    no en producción.
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


async def toggle_mod(mo2: Any, mod_name: str, enable: bool, *, profile: str) -> str:
    """Enable or disable an installed mod in the session's MO2 profile load order.

    Args are pre-validated by AsyncToolRegistry.execute() via ToggleModParams.
    ``profile`` es keyword-only y sin default — ver :func:`uninstall_mod`.
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


async def launch_game(mo2: Any, *, profile: str) -> str:
    """Launch Skyrim Special Edition via MO2 using SKSE.

    ``profile`` es keyword-only y sin default — ver :func:`uninstall_mod`. La tool ya
    no declara ``params_model``: no le queda ningún argumento que el LLM elija.
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
    journal: Any | None = None,
) -> str:
    """Generate 'Bashed Patch, 0.esp' using WryeBashRunner.

    NOTE: Plugin limit validation (M-04) is handled at the Supervisor level
    (execute_wrye_bash_pipeline). Here we only execute the runner.
    This handler is used when AsyncToolRegistry invokes the tool directly.

    #315 (PR A) extrajo :class:`WryeBashPipelineService`, el mismo servicio que
    usa el Ritual de la GUI/dispatcher, con lock ANIDADO (``Bashed Patch, 0.esp``
    + ``load-order``): serializa contra otra corrida de Wrye Bash y contra un
    sort de LOOT concurrente — el lock cross-process solo protege si TODOS los
    mutadores participan. Este path del agente delega al mismo servicio en vez
    de mantener una segunda implementación del lock (§2.1 auditoría original:
    mismo vector P1 que ``run_loot_sort``/``run_pandora``). Sin lock manager
    (callers legacy / tests) corre directo, preservando el comportamiento
    anterior.

    T-26/T-28 (ADR 0002, "PR C" del #315): cuando ``journal`` está cableado (via
    ``app_context``) este path del agente TAMBIÉN emite la caja negra de vuelo —
    igual que ``run_loot_sort``/``run_pandora``. Con ``journal=None`` no se emite.
    """
    if wrye_bash_runner is None:
        return json.dumps({"error": "WryeBashRunner is not configured. Set WRYE_BASH_PATH."})

    if lock_manager is not None and snapshot_manager is not None:
        from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

        service = WryeBashPipelineService(
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            wrye_bash_runner=wrye_bash_runner,
            journal=journal,
        )
        # El servicio siempre devuelve un dict serializable (guard M-04, lock
        # contention, LockLeaseLostError, error de ejecución) — nunca propaga.
        # validate_limit=False: M-04 se valida a nivel Supervisor para este path.
        res = await service.execute_pipeline(profile="agent-tool", validate_limit=False)
        out: dict[str, Any] = {
            "success": res.get("success", False),
            "return_code": res.get("return_code", -1),
            "stdout": sanitize_for_prompt(str(res.get("stdout", ""))) if res.get("stdout") else "",
            "stderr": sanitize_for_prompt(str(res.get("stderr", ""))) if res.get("stderr") else "",
            "duration_seconds": res.get("duration_seconds", 0.0),
            # U-04: el hermano LLM del log del servicio. Un fallo revertido y uno que
            # dejó un Bashed Patch parcial piden acciones DISTINTAS del operador, y el
            # log no viaja al agente: sin este campo los dos se ven igual desde acá.
            "rolled_back": res.get("rolled_back", False),
        }
        if not out["success"] and res.get("message"):
            out["error"] = res["message"]
        return json.dumps(out)

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
    journal: Any | None = None,
) -> str:
    """Execute Pandora Behavior Engine in auto mode (Skyrim SE) via PandoraRunner.

    Consolidation (obs #187): replaces the legacy AnimationHub-backed handler.
    The runner uses the unified ``_process`` helpers (timeout + kill-tree).

    Codex #213 (same P1 vector as ``run_loot_sort``/Audit #190): Pandora rewrites
    the shared behavior graphs, so every mutator delegates to
    :class:`PandoraPipelineService` and participates in the same
    ``behavior-graphs`` lock and rollback as the GUI Ritual. Callers without both
    protection managers fail closed before the runner can mutate shared output.

    T-26/T-28 (ADR 0002, review Codex #318): cuando ``journal`` está cableado (via
    ``app_context``) este path del agente TAMBIÉN emite la caja negra de vuelo —
    igual que ``run_loot_sort``. Sin cablearlo, un run de Pandora por Telegram/LLM
    mutaría los behavior graphs sin ``ActionManifest``/``FlightReport`` y T-26/T-28
    no estarían cubiertos para todos los callers de producción. Con ``journal=None``
    (callers legacy / tests) el comportamiento no cambia.
    """
    if pandora_runner is None:
        detail = "PandoraRunner is not configured. Set pandora_exe in config or install it via setup_tools."
        return json.dumps({"success": False, "message": detail, "error": detail})

    missing_managers = [
        name
        for name, manager in (
            ("lock_manager", lock_manager),
            ("snapshot_manager", snapshot_manager),
        )
        if manager is None
    ]
    if missing_managers:
        detail = f"Pandora requiere protección; faltan: {', '.join(missing_managers)}"
        return json.dumps({"success": False, "message": detail, "error": detail})

    from sky_claw.local.tools.pandora_service import PandoraPipelineService

    service = PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        pandora_runner=pandora_runner,
        journal=journal,
    )
    # The service serializes on the behavior-graphs lock and converts lock
    # contention / execution failures to a dict; map it to the tool's JSON
    # contract. A non-success carries the detail under ``logs``.
    try:
        res = await service.generate_animations()
    except Exception as exc:
        detail = sanitize_for_prompt(str(exc))
        return json.dumps({"success": False, "message": detail, "error": detail})
    raw_message = str(res.get("message", ""))
    # ``warnings`` es texto crudo de ``Engine.log`` (nombres de mods/nodos
    # controlados por el autor del mod que generó el log) — mismo origen no
    # confiable que ``message``/``stdout``/``stderr``, saneado con el mismo
    # criterio. Sin esto un mod malicioso podría inyectar instrucciones para el
    # LLM vía una línea ``WARN`` (hallazgo qodo, PR #439: el campo se agregó en
    # el mismo PR que sí saneó ``message`` y quedó afuera).
    warnings_crudos = res.get("warnings")
    out: dict[str, Any] = {
        "success": res.get("success", False),
        "message": sanitize_for_prompt(raw_message) if raw_message else "",
        "return_code": res.get("return_code", -1),
        "stdout": sanitize_for_prompt(str(res.get("stdout", ""))) if res.get("stdout") else "",
        "stderr": sanitize_for_prompt(str(res.get("stderr", ""))) if res.get("stderr") else "",
        "duration_seconds": res.get("duration_seconds", 0.0),
        "warnings": (
            [sanitize_for_prompt(str(w)) for w in warnings_crudos] if isinstance(warnings_crudos, list) else []
        ),
    }
    if not out["success"] and res.get("logs"):
        out["error"] = sanitize_for_prompt(str(res["logs"]))
    return json.dumps(out)


class _BodySlideRunFallidoError(Exception):
    """Interno (U-04): un resultado con ``success=False`` se convierte en excepción
    DENTRO del lock para disparar el rollback real de la salida.

    Mismo patrón que ``wrye_bash_service._RunFallidoError`` / ``pandora_service.
    _RunFallidoError``: tanto ``SnapshotTransactionLock`` como
    ``DirectoryRollback`` sólo restauran en la rama de excepción de su
    ``__aexit__``; un resultado ``success=False`` (exit non-zero) sale "limpio"
    del ``async with`` si no se puentea, y el backup se descartaría con el
    parcial fallido pisando la salida previa.

    Carga el resultado completo para que el ``except`` arme el MISMO dict de
    salida que el camino no-excepcional — el contrato de la tool no cambia,
    sólo gana el rollback que le faltaba.
    """

    def __init__(self, result: Any) -> None:
        self.result = result
        super().__init__(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "BodySlide run failed")


def _bodyslide_dict_de_resultado(result: Any) -> dict[str, Any]:
    """Dict de salida compartido por el camino exitoso y el de ``_BodySlideRunFallidoError``.

    Contrato ``success``/``message`` (AGENTS.md, mismo patrón que
    ``pandora_service._dict_de_resultado``): ``message`` canónico vacío en
    éxito, detalle de fallo si no (CodeRabbit, PR #430). Saneado con
    ``sanitize_for_prompt`` como ``stdout``/``stderr`` acá mismo, y como ya hace
    ``run_pandora`` (mismo archivo) con su propio ``message`` — sin esto, el
    detalle de fallo (stderr/stdout de BodySlide) llegaría CRUDO al LLM por una
    llave mientras las otras dos del mismo dict van saneadas.
    """
    # ``message`` del runner primero: para un binario ``/SUBSYSTEM:Windows``
    # ``stdout``/``stderr`` son SIEMPRE vacíos, así que derivar el detalle sólo de
    # ellos producía ``success=False`` con ``message=""`` — un fallo sin causa
    # sobre el que nadie puede actuar. El runner puebla la causa real (veredicto
    # del post-check de artefacto + cola de ``Log_BS.txt``). Se exige ``str`` para
    # que un doble de test que no fije el atributo no filtre un repr de mock al LLM.
    mensaje_del_runner = getattr(result, "message", "")
    if not isinstance(mensaje_del_runner, str):
        mensaje_del_runner = ""
    detalle_fallo = mensaje_del_runner or result.stderr or result.stdout or ""
    return {
        "success": result.success,
        "message": "" if result.success else sanitize_for_prompt(detalle_fallo),
        "return_code": result.return_code,
        "stdout": sanitize_for_prompt(result.stdout) if result.stdout else "",
        "stderr": sanitize_for_prompt(result.stderr) if result.stderr else "",
        "duration_seconds": result.duration_seconds,
    }


async def run_bodyslide_batch(
    bodyslide_runner: Any,
    group: str = "CBBE",
    output_path: str = "meshes",
    preset: str | None = None,
    build_morphs: bool = True,
    *,
    lock_manager: Any | None = None,
    snapshot_manager: Any | None = None,
) -> str:
    """Execute BodySlide in batch mode via BodySlideRunner.

    Args are pre-validated by AsyncToolRegistry.execute() via BodySlideBatchParams.

    Consolidation (obs #187): replaces the legacy AnimationHub-backed handler
    that hardcoded the "CBBE Body Physics" preset; ``group`` is configurable.

    Codex #213 (same P1 vector as ``run_loot_sort``/``run_pandora``): BodySlide
    batch-builds meshes, so when the distributed lock is wired (production via
    ``app_context``) this agent path runs under the shared ``bodyslide-meshes``
    lock — the cross-process lock only protects if every mutator participates.

    U-04: el destino físico deja de ser el ``output_path`` que manda el LLM.
    Ese valor podía resolver bajo ``Data/meshes``, un árbol compartido por
    miles de archivos de otros mods — un move-aside de directorio completo ahí
    sería catastrófico (restauraría/descartaría mallas ajenas, no sólo las de
    BodySlide). En cambio se usa ``output_targets.bodyslide_output_target``:
    un subárbol propio POR GRUPO que Sky-Claw administra, la misma propiedad
    ("la herramienta regenera el target por completo") que habilita el
    move-aside de Pandora/DynDOLOD. ``output_path`` se conserva en la firma
    por compatibilidad de contrato pero ya no determina dónde escribe
    BodySlide. Sin lock manager o snapshot manager, la ejecución falla cerrada
    para no permitir una mutación sin protección.
    """
    if bodyslide_runner is None:
        return json.dumps(
            {"error": ("BodySlideRunner is not configured. Set bodyslide_exe in config or install it via setup_tools.")}
        )

    missing_managers = [
        name
        for name, manager in (
            ("lock_manager", lock_manager),
            ("snapshot_manager", snapshot_manager),
        )
        if manager is None
    ]
    if missing_managers:
        return json.dumps({"error": f"BodySlide requiere protección; faltan: {', '.join(missing_managers)}"})

    import contextlib

    from sky_claw.app.db.locks import LockAcquisitionError, SnapshotTransactionLock
    from sky_claw.local.tools._dir_rollback import DirectoryRollback
    from sky_claw.local.tools.output_targets import bodyslide_output_target

    game_path = getattr(getattr(bodyslide_runner, "config", None), "game_path", None)
    target = bodyslide_output_target(game=game_path, group=group)
    if target is None:
        detail = "No se pudo resolver el destino administrado de BodySlide (game_path no configurado)."
        return json.dumps({"success": False, "message": detail, "error": detail})

    try:
        async with contextlib.AsyncExitStack() as stack:
            tx = await stack.enter_async_context(
                SnapshotTransactionLock(
                    lock_manager=lock_manager,
                    snapshot_manager=snapshot_manager,
                    resource_id=BODYSLIDE_MESHES_RESOURCE_ID,
                    agent_id="bodyslide-tool",
                    target_files=[],
                    metadata={"source": "bodyslide_batch", "group": group},
                )
            )
            # U-04: move-aside DENTRO del lock (mismo orden que pandora_service) —
            # el AsyncExitStack cierra los context en orden LIFO, así que el
            # restore corre mientras todavía se tiene la exclusividad del lock.
            # El veto de lease (`not tx.lease_lost`) hace que el move-aside y el
            # lock que lo envuelve apliquen el MISMO criterio: si el heartbeat
            # perdió el lease durante la corrida, no se restaura pisando a un
            # dueño concurrente (review Codex #399, mismo patrón en Pandora).
            await stack.enter_async_context(DirectoryRollback(target, should_rollback=lambda: not tx.lease_lost))
            result = await bodyslide_runner.run_batch(
                group,
                str(target),
                preset=preset,
                build_morphs=build_morphs,
            )
            if not result.success:
                # U-04: el puente fallo→excepción. Sin esto, un exit non-zero sale
                # limpio del stack, DirectoryRollback DESCARTA el backup y el
                # árbol parcial queda en disco.
                raise _BodySlideRunFallidoError(result)
    except LockAcquisitionError as exc:
        # Saneado igual que el catch-all de abajo (CodeRabbit, PR #430): hoy
        # resource_id/agent_id son constantes, pero el mensaje de la excepción
        # no está bajo control de este módulo — mismo criterio defensivo que
        # ``run_pandora`` ya aplica a su propio catch-all.
        detail = sanitize_for_prompt(f"Could not acquire '{BODYSLIDE_MESHES_RESOURCE_ID}' lock: {exc}")
        return json.dumps({"success": False, "message": detail, "error": detail})
    except _BodySlideRunFallidoError as exc:
        return json.dumps(_bodyslide_dict_de_resultado(exc.result))
    except Exception as exc:
        # Mismo patrón que ``run_pandora`` (mismo archivo): una excepción
        # inesperada puede envolver salida cruda del runner.
        detail = sanitize_for_prompt(str(exc))
        return json.dumps({"success": False, "message": detail, "error": detail})

    return json.dumps(_bodyslide_dict_de_resultado(result))
