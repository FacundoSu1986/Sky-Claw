"""run_phase.py - Harness del REAL RIG PR-2 (fases: precheck | A | B).

Boundary real: DynDOLODPipelineService.execute() con workspace resuelto por
resolver_workspace, coordinacion durable de etapa 9, journal y locks reales.
Seam de rig del runner: data_dir/ini_dir/plugins_file/temp_dir del rig T5-v2
(el servicio productivo no emite -m:/-p:; documentado en 01-environment.txt).
Sin mocks. No modifica codigo.

Uso: python run_phase.py <precheck|A|B> <session-dir>
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import sys
import traceback

REPO = pathlib.Path(r"C:\Worktrees\Sky-Claw-pr2")
SESSION = pathlib.Path(sys.argv[2]).resolve()
PHASE = sys.argv[1]
sys.path.insert(0, str(REPO))

GAME = pathlib.Path(r"G:\Modding\Skyrim_Runtime_1.6.1170")
MO2_INSTALL = pathlib.Path(r"C:\Modding\ModOrganizer2")
MO2_INSTANCE = pathlib.Path(r"G:\Modding\MO2\SkyrimSE")
MO2_MODS = pathlib.Path(r"G:\Modding\MO2\SkyrimSE\mods")
PROFILE = "SkyClaw-PR2-Rig"
WORK_ROOT = pathlib.Path(r"E:\Sky-Claw T5 Rig\PR2 Work")
BINDING_ESPERADO = "d9caaf53-2124-4165-8b7e-72e32237cf94"
DYNDLOD_EXE = pathlib.Path(r"C:\Modding\DynDOLOD RigTest\DynDOLODx64.exe")
TEXGEN_EXE = pathlib.Path(r"C:\Modding\DynDOLOD RigTest\TexGenx64.exe")
RIG_DATA = pathlib.Path(r"C:\Modding\DynDOLOD RigTest\_rig_test\data")
RIG_INI = pathlib.Path(r"C:\Modding\DynDOLOD RigTest\_rig_test\ini")
RIG_PLUGINS = pathlib.Path(r"C:\Modding\DynDOLOD RigTest\_rig_test\plugins.txt")
RIG_TEMP = pathlib.Path(r"E:\Sky-Claw T5 Rig\temp")

os.environ["SKYRIM_PATH"] = str(GAME)
os.environ["MO2_PATH"] = str(MO2_INSTALL)
os.environ["MO2_MODS_PATH"] = str(MO2_MODS)
os.environ["DYNDLOD_EXE"] = str(DYNDLOD_EXE)
os.environ["TEXGEN_EXE"] = str(TEXGEN_EXE)

from sky_claw.app.core.event_bus import CoreEventBus  # noqa: E402
from sky_claw.app.core.path_resolver import PathResolutionService  # noqa: E402
from sky_claw.app.db.journal import OperationJournal  # noqa: E402
from sky_claw.app.db.locks import DistributedLockManager  # noqa: E402
from sky_claw.app.db.snapshot_manager import FileSnapshotManager  # noqa: E402
from sky_claw.app.security.path_validator import PathValidator  # noqa: E402
from sky_claw.local.tools import dyndolod_workspace as ws  # noqa: E402
from sky_claw.local.tools.dyndolod_runner import (  # noqa: E402
    DynDOLODConfig,
    DynDOLODRunner,
    HerramientaDynDOLOD,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService  # noqa: E402


def log(msg: str) -> None:
    print(f"[{PHASE}] {msg}", flush=True)


def tree_lines(root: pathlib.Path, max_entries: int = 400) -> list[str]:
    lines: list[str] = []
    if not root.exists():
        return [f"root={root} AUSENTE"]
    files = 0
    total = 0
    dirs = 0
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            dirs += 1
        elif p.is_file():
            files += 1
            total += p.stat().st_size
    lines.append(f"root={root} archivos={files} dirs={dirs} bytes={total}")
    n = 0
    for p in sorted(root.rglob("*")):
        if n >= max_entries:
            lines.append(f"... (recorte a {max_entries} entradas)")
            break
        rel = p.relative_to(root)
        lines.append(f"  {rel} | {'DIR' if p.is_dir() else p.stat().st_size}")
        n += 1
    return lines


def escribir(nombre: str, lineas: list[str]) -> None:
    (SESSION / nombre).write_text("\n".join(lineas) + "\n", encoding="utf-8")


def escribir_json(nombre: str, data: object) -> None:
    (SESSION / nombre).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def estado_mo2_mods() -> list[str]:
    lineas = [f"mods_dir={MO2_MODS}"]
    for sub in ("TexGen Output", "DynDOLOD Output"):
        p = MO2_MODS / sub
        if p.exists():
            files = sum(1 for f in p.rglob("*") if f.is_file())
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            lineas.append(f"  {sub} existe=True archivos={files} bytes={total}")
        else:
            lineas.append(f"  {sub} existe=False")
    residuos = [f.name for f in MO2_MODS.iterdir() if ".rollback-" in f.name]
    lineas.append(f"residuos rollback en mods: {residuos}")
    return lineas


def escribir_packaging(sub: str, destino: str, sesion_archivo: str) -> None:
    p = MO2_MODS / sub
    lineas = [f"package={sub}", f"destination={p}"]
    if not p.exists():
        lineas.append("existe=False")
    else:
        files = [f for f in p.rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in files)
        rels = sorted(str(f.relative_to(p)) for f in files)
        lineas.append(f"existe=True archivos={len(files)} bytes={total}")
        lineas.append("muestra:")
        lineas.extend("  " + r for r in rels[:40])
        lineas.append(f"plugins: {[r for r in rels if r.lower().endswith(('.esp', '.esm', '.esl'))]}")
        lineas.append(f"raices data-relative: {sorted({r.split(chr(92))[0] for r in rels})}")
    escribir(sesion_archivo, lineas)


def construir_resolver() -> PathResolutionService:
    return PathResolutionService(
        PathValidator([GAME, MO2_INSTALL, MO2_INSTANCE, DYNDLOD_EXE.parent, TEXGEN_EXE.parent, pathlib.Path.home()]),
        profile_name=PROFILE,
        mo2_install_dir=MO2_INSTALL,
    )


async def preparar(svc_kwargs: dict) -> tuple[DynDOLODPipelineService, object]:
    resolver = construir_resolver()
    coordinacion = ws.construir_coordinacion_de_etapa9()
    registro = ws.registro_de_roots_activos()
    recursos = ws.ResourceBinding.desde_paths(
        game_path=GAME, mo2_instance_data_root=MO2_INSTANCE, mo2_mods_path=MO2_MODS
    )
    prohibidas = ws.RaicesProhibidas.desde_entorno(
        game=GAME,
        mo2_install=MO2_INSTALL,
        mo2_instance_data_root=MO2_INSTANCE,
        mo2_mods_path=MO2_MODS,
        dyndolod_exe=DYNDLOD_EXE,
        texgen_exe=TEXGEN_EXE,
    )
    workspace = await ws.resolver_workspace(
        preferencia=str(WORK_ROOT),
        recursos=recursos,
        prohibidas=prohibidas,
        registro=registro,
        coordinacion=coordinacion,
    )
    if workspace is None:
        raise SystemExit("ABORT: workspace NO CONFIGURADO")
    if workspace.binding.binding_id != BINDING_ESPERADO:
        raise SystemExit(f"ABORT: binding_id {workspace.binding.binding_id} != {BINDING_ESPERADO}")
    if workspace.root != WORK_ROOT:
        raise SystemExit(f"ABORT: root {workspace.root} != {WORK_ROOT}")
    log(f"workspace OK: root={workspace.root} estado={workspace.estado.name} binding={workspace.binding.binding_id}")

    lock_manager = DistributedLockManager(db_path=SESSION / "locks.db")
    await lock_manager.initialize()
    snapshot_manager = FileSnapshotManager(snapshot_dir=SESSION / "snapshots-locks")
    journal = OperationJournal(db_path=SESSION / "journal.db")
    await journal.open()
    bus = CoreEventBus()
    await bus.start()
    servicio = DynDOLODPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        path_resolver=resolver,
        event_bus=bus,
        mo2_profile=PROFILE,
        stage9_coordination=coordinacion,
        workspace=workspace,
        **svc_kwargs,
    )
    return servicio, {
        "coordinacion": coordinacion,
        "journal": journal,
        "bus": bus,
        "workspace": workspace,
        "runner": None,
        "lock_manager": lock_manager,
    }


def inyectar_runner(servicio: DynDOLODPipelineService, workspace) -> DynDOLODRunner:
    cfg = DynDOLODConfig(
        game_path=GAME,
        mo2_path=MO2_INSTALL,
        mo2_mods_path=MO2_MODS,
        dyndolod_exe=DYNDLOD_EXE,
        texgen_exe=TEXGEN_EXE,
        external_work_root=workspace.root,
        fence_ownership=servicio._fence_del_workspace,
        data_dir=RIG_DATA,
        ini_dir=RIG_INI,
        plugins_file=RIG_PLUGINS,
        temp_dir=RIG_TEMP,
        timeout_seconds=2700,
    )
    runner = DynDOLODRunner(cfg)
    servicio._runner = runner
    if not servicio._layout_del_runner_coincide(runner):
        raise SystemExit("ABORT: layout del runner no coincide con workspace")
    return runner


async def limpiar(ctx: dict) -> None:
    workspace = ctx["workspace"]
    try:
        if workspace is not None and workspace.ownership is not None:
            await workspace.ownership.liberar()
    finally:
        with contextlib.suppress(Exception):
            await ctx["bus"].stop()
        with contextlib.suppress(Exception):
            await ctx["lock_manager"].close()
        with contextlib.suppress(Exception):
            await ctx["journal"].close()
        await ctx["coordinacion"].close()


async def fase_precheck() -> int:
    servicio, ctx = await preparar({})
    try:
        preflight = servicio._ensure_preflight()
        if preflight is None:
            escribir("04-preflight.txt", ["preflight=None (game/MO2 no resolubles)"])
            return 2
        reporte = await preflight.run()
        lineas = [
            f"status={reporte.status.value}",
            f"blocks_mutations={reporte.blocks_mutations}",
            "",
        ]
        for c in reporte.checks:
            lineas.append(f"- {c.name}: {c.status.value} :: {c.summary}")
            for d in c.details:
                lineas.append(f"    {d}")
        lineas.append("")
        lineas.append(json.dumps(reporte.to_dict(), ensure_ascii=False, indent=2, default=str))
        escribir("04-preflight.txt", lineas)
        escribir_json("04-preflight.json", reporte.to_dict())
        binding = (WORK_ROOT / ws.ARCHIVO_DE_BINDING).read_text(encoding="utf-8")
        escribir_json("03-binding.json", {"binding_path": str(WORK_ROOT / ws.ARCHIVO_DE_BINDING), "contenido": json.loads(binding)})
        log(f"precheck status={reporte.status.value} blocks={reporte.blocks_mutations}")
        return 0
    finally:
        await limpiar(ctx)


async def fase_run(run_texgen: bool) -> int:
    servicio, ctx = await preparar({})
    try:
        runner = inyectar_runner(servicio, ctx["workspace"])
        herramienta = HerramientaDynDOLOD.TEXGEN if run_texgen else HerramientaDynDOLOD.DYNDOLOD
        argv = runner._build_xedit_args(None, herramienta=herramienta)
        linea = runner._linea_de_comando([str(TEXGEN_EXE if run_texgen else DYNDLOD_EXE), *argv])
        exe = TEXGEN_EXE if run_texgen else DYNDLOD_EXE
        prefijo = "texgen" if run_texgen else "dyndolod"
        escribir(
            f"{prefijo}-command.txt",
            [
                f"tool={'TexGen' if run_texgen else 'DynDOLOD'}",
                f"exe={exe}",
                f"argv={json.dumps(argv, ensure_ascii=False)}",
                f"command_line={linea}",
                f"cwd={pathlib.Path.cwd()}",
                f"data_dir={RIG_DATA}",
                f"ini_dir={RIG_INI}",
                f"plugins_file={RIG_PLUGINS}",
                f"temp_dir={RIG_TEMP}",
                f"external_work_root={ctx['workspace'].root}",
                "timeout_seconds=2700",
            ],
        )
        layout = runner._config.output_layout
        raiz = layout.texgen_root if run_texgen else layout.dyndolod_root
        escribir(f"{prefijo}-tree-before.txt", tree_lines(raiz))
        escribir("mo2-mods-before.txt" if run_texgen else "mo2-mods-phaseB-before.txt", estado_mo2_mods())

        inicio = asyncio.get_event_loop().time()
        try:
            resultado = await servicio.execute(preset="Medium", run_texgen=run_texgen, create_snapshot=True)
        except BaseException as exc:  # noqa: BLE001 - harness de evidencia
            duracion = asyncio.get_event_loop().time() - inicio
            escribir_json(
                f"{prefijo}-exception.json",
                {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(), "duration": duracion},
            )
            log(f"EXCEPCION en execute: {type(exc).__name__}: {exc}")
            return 3
        duracion = asyncio.get_event_loop().time() - inicio

        escribir_json(f"{prefijo}-result.json", resultado)
        resumen = [
            f"duration_seconds={duracion:.1f}",
            f"success={resultado.get('success')}",
            f"needs_deployment={resultado.get('needs_deployment')}",
            f"rolled_back={resultado.get('rolled_back')}",
            f"message={resultado.get('message')}",
            f"errors={resultado.get('errors')}",
            f"texgen_mod_path={resultado.get('texgen_mod_path')}",
            f"dyndolod_mod_path={resultado.get('dyndolod_mod_path')}",
            f"status={resultado.get('status')}",
            f"reason={resultado.get('reason')}",
            f"assisted={resultado.get('assisted')}",
            "preflight=" + json.dumps(resultado.get("preflight"), ensure_ascii=False, default=str),
        ]
        escribir(f"{prefijo}-result.txt", resumen)

        escribir(f"{prefijo}-tree-after.txt", tree_lines(raiz))
        escribir("mo2-mods-after.txt" if not run_texgen else "mo2-mods-phaseA-after.txt", estado_mo2_mods())
        escribir_packaging("TexGen Output" if run_texgen else "DynDOLOD Output", str(MO2_MODS), f"packaging-{'texgen' if run_texgen else 'dyndolod'}.txt")

        if run_texgen:
            from sky_claw.app.db.handoffs import clave_de_artifact

            entrada = await ctx["journal"].consultar_handoff_activo(clave_de_artifact(MO2_MODS / "TexGen Output"))
            escribir_json(
                "handoff-texgen.json",
                {
                    "handoff": None if entrada is None else {
                        "handoff_id": entrada.handoff_id,
                        "state": entrada.state.value,
                        "expected_profile": entrada.expected_profile,
                        "expected_digest": entrada.expected_digest,
                        "expected_files": entrada.expected_files,
                        "expected_bytes": entrada.expected_bytes,
                    },
                    "texgen_result": resultado.get("texgen_result"),
                    "needs_deployment": resultado.get("needs_deployment"),
                },
            )
            log(f"handoff: {entrada.state.value if entrada else None}")
        log(f"phase {'A' if run_texgen else 'B'} result: success={resultado.get('success')} needs_deployment={resultado.get('needs_deployment')}")
        return 0
    finally:
        await limpiar(ctx)


async def main() -> int:
    if PHASE == "precheck":
        return await fase_precheck()
    if PHASE == "A":
        return await fase_run(run_texgen=True)
    if PHASE == "B":
        return await fase_run(run_texgen=False)
    raise SystemExit(f"fase desconocida: {PHASE}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
