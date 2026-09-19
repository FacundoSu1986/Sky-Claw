"""rig_cli_prereqs.py — Rig REAL mínimo de prerrequisitos CLI (#593).

No es código productivo. Boundary que ejercita:

    DynDOLODPipelineService._ensure_runner()   (camino productivo, sin inyectar
                                                `DynDOLODConfig` a mano)
    → DynDOLODConfig(ini_dir, plugins_file)    (wiring nuevo)
    → DynDOLODRunner._build_xedit_args()       (builder compartido)
    → DynDOLODRunner._execute_process()        (spawn real, CREATE_NO_WINDOW)

El workspace se resuelve con la maquinaria real (`resolver_workspace`,
ownership vivo) porque el fence P2.2 corre antes del spawn; el rig NO muta el
root (no pulsa Start), sólo recibe el argv. Lanza TexGen Alpha-209, espera el
eco del binario en su log y deja que el timeout acotado mate el árbol.

Uso: python rig_cli_prereqs.py <repo> <session-dir> [timeout-s]
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import pathlib
import sys

REPO = pathlib.Path(sys.argv[1]).resolve()
SESSION = pathlib.Path(sys.argv[2]).resolve()
TIMEOUT_S = int(sys.argv[3]) if len(sys.argv) > 3 else 75
HERRAMIENTA = (sys.argv[4] if len(sys.argv) > 4 else "texgen").casefold()
es_dyndolod = HERRAMIENTA == "dyndolod"

GAME = pathlib.Path(r"G:\Modding\Skyrim_Runtime_1.6.1170")
MO2_INSTALL = pathlib.Path(r"C:\Modding\ModOrganizer2")
MO2_INSTANCE = pathlib.Path(r"G:\Modding\MO2\SkyrimSE")
MO2_MODS = MO2_INSTANCE / "mods"
PROFILE = "SkyClaw-PR2-Rig"
TOOL_DIR = pathlib.Path(r"C:\Modding\DynDOLOD RigTest")
DYNDLOD_EXE = TOOL_DIR / "DynDOLODx64.exe"
TEXGEN_EXE = TOOL_DIR / "TexGenx64.exe"
INI_DIR = TOOL_DIR / "_rig_test" / "ini"
WORK_ROOT = SESSION / "PR593 Work"
PREFIJO = "dyndolod" if es_dyndolod else "texgen"
EXE = DYNDLOD_EXE if es_dyndolod else TEXGEN_EXE
LOG = TOOL_DIR / "Logs" / ("DynDOLOD_SSE_log.txt" if es_dyndolod else "TexGen_SSE_log.txt")
NOMBRE_PROCESO = "dyndolodx64.exe" if es_dyndolod else "texgenx64.exe"

os.environ["SKYRIM_PATH"] = str(GAME)
os.environ["MO2_PATH"] = str(MO2_INSTALL)
os.environ["MO2_MODS_PATH"] = str(MO2_MODS)
os.environ["DYNDLOD_EXE"] = str(DYNDLOD_EXE)
os.environ["TEXGEN_EXE"] = str(TEXGEN_EXE)
os.environ["DYNDLOD_INI_DIR"] = str(INI_DIR)

sys.path.insert(0, str(REPO))

from sky_claw.app.core.event_bus import CoreEventBus  # noqa: E402
from sky_claw.app.core.path_resolver import PathResolutionService  # noqa: E402
from sky_claw.app.db.journal import OperationJournal  # noqa: E402
from sky_claw.app.db.locks import DistributedLockManager  # noqa: E402
from sky_claw.app.db.snapshot_manager import FileSnapshotManager  # noqa: E402
from sky_claw.app.security.path_validator import PathValidator  # noqa: E402
from sky_claw.local.tools import dyndolod_workspace as ws  # noqa: E402
from sky_claw.local.tools.dyndolod_runner import (  # noqa: E402
    DynDOLODTimeoutError,
    HerramientaDynDOLOD,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService  # noqa: E402

# Nota de seam: `_ensure_runner` usa `ReadinessMode.DISABLED_FOR_TEST` cuando el
# servicio no recibió una `CapacidadDeReadinessUIA` (modo doble/rig). Este rig
# no ejerce el gate de UI Automation — el objetivo es la RECEPCIÓN del argv.

HERRAMIENTA_BUILDER = HerramientaDynDOLOD.DYNDOLOD if es_dyndolod else HerramientaDynDOLOD.TEXGEN


def log(msg: str) -> None:
    print(f"[rig593] {msg}", flush=True)


def escribir(nombre: str, lineas: list[str]) -> None:
    (SESSION / nombre).write_text("\n".join(lineas) + "\n", encoding="utf-8")


def firma_de_log() -> tuple[int, int, str]:
    """``(líneas, bytes, sha256)`` del log del binario ANTES de lanzar."""
    if not LOG.is_file():
        return (0, -1, "")
    data = LOG.read_bytes()
    return (len(data.decode("utf-8", errors="replace").splitlines()), len(data), hashlib.sha256(data).hexdigest())


def lineas_desde(indice: int) -> list[str]:
    if not LOG.is_file():
        return []
    lineas = LOG.read_bytes().decode("utf-8", errors="replace").splitlines()
    return lineas[max(0, indice) :]


def eco(lineas: list[str], clave: str) -> str | None:
    for linea in reversed(lineas):
        if clave in linea:
            return linea
    return None


#: ``WM_CLOSE`` — cerrar el asistente como lo haría el operador (NO Start).
#: El binario vuelca su log al cerrarse; un hard-kill lo pierde (medido: la
#: primera corrida de este rig no dejó NINGUNA línea nueva en el log).
_WM_CLOSE = 0x0010
_ESPERA_ANTES_DE_CERRAR_S = 25


def _ventanas_del_pid(pid: int) -> list[int]:
    """Ventanas top-level del proceso (sin UI Automation: sólo user32)."""
    user32 = ctypes.windll.user32
    encontradas: list[int] = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)  # noqa: N806

    def _callback(hwnd, _lparam):  # noqa: ANN001
        propietario = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(propietario))
        if propietario.value == pid and user32.IsWindowVisible(hwnd):
            encontradas.append(int(hwnd))
        return True

    user32.EnumWindows(CB(_callback), 0)
    return encontradas


def cerrar_ventana_del_pid(pid: int) -> int:
    """Manda ``WM_CLOSE`` a las ventanas visibles del PID. Devuelve cuántas."""
    user32 = ctypes.windll.user32
    ventanas = _ventanas_del_pid(pid)
    for hwnd in ventanas:
        user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
    return len(ventanas)


async def main() -> int:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    n_lineas_antes, bytes_antes, sha_antes = firma_de_log()

    resolver = PathResolutionService(
        path_validator=PathValidator([GAME, MO2_INSTALL, MO2_INSTANCE, MO2_MODS, TOOL_DIR, SESSION]),
        profile_name=PROFILE,
        mo2_install_dir=MO2_INSTALL,
    )
    coordinacion = ws.construir_coordinacion_de_etapa9()
    registro = ws.registro_de_roots_activos()
    recursos = ws.ResourceBinding.desde_paths(
        game_path=GAME,
        mo2_instance_data_root=MO2_INSTANCE,
        mo2_mods_path=MO2_MODS,
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
        escribir("00-abort.txt", ["ABORT: workspace NO CONFIGURADO"])
        return 2
    log(f"workspace OK: root={workspace.root} estado={workspace.estado.name} ownership={workspace.ownership is not None}")
    escribir(
        "00-workspace.txt",
        [
            f"root={workspace.root}",
            f"estado={workspace.estado.name}",
            f"binding_id={workspace.binding.binding_id}",
            f"ownership={workspace.ownership is not None}",
        ],
    )

    lock_manager = DistributedLockManager(db_path=SESSION / "locks.db")
    await lock_manager.initialize()
    journal = OperationJournal(db_path=SESSION / "journal.db")
    await journal.open()
    bus = CoreEventBus()
    await bus.start()

    servicio = DynDOLODPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=FileSnapshotManager(snapshot_dir=SESSION / "snapshots"),
        journal=journal,
        path_resolver=resolver,
        event_bus=bus,
        mo2_profile=PROFILE,
        stage9_coordination=coordinacion,
        workspace=workspace,
    )

    try:
        runner = servicio._ensure_runner()  # CAMINO PRODUCTIVO
        config = runner._config
        plugins_esperado = MO2_INSTANCE / "profiles" / PROFILE / "plugins.txt"
        assert config.plugins_file == plugins_esperado, f"plugins_file={config.plugins_file!r} != {plugins_esperado}"
        assert config.ini_dir == INI_DIR.resolve(), f"ini_dir={config.ini_dir!r} != {INI_DIR}"
        assert config.game_mode == "sse", f"game_mode={config.game_mode!r}"
        log(f"config productiva OK: plugins={config.plugins_file} ini={config.ini_dir}")

        argv = runner._build_xedit_args(None, herramienta=HERRAMIENTA_BUILDER)
        linea = runner._linea_de_comando([str(EXE), *argv])
        escribir(
            f"01-argv-productivo-{PREFIJO}.txt",
            [
                f"tool={HERRAMIENTA}",
                f"exe={EXE}",
                f"argv={json.dumps(argv, ensure_ascii=False)}",
                f"command_line={linea}",
                f"plugins_file={config.plugins_file}",
                f"ini_dir={config.ini_dir}",
                f"data_dir={config.data_dir}",
                f"external_work_root={config.external_work_root}",
                f"game_mode={config.game_mode}",
                f"profile={resolver.get_active_profile()}",
            ],
        )
        log(f"argv: {json.dumps(argv, ensure_ascii=False)}")

        # Lanzamiento REAL acotado por timeout. El runner mata el árbol al vencer.
        resultado: dict[str, object] = {
            "timeout_s": TIMEOUT_S,
            "log_bytes_antes": bytes_antes,
            "log_sha256_antes": sha_antes,
            "log_lineas_antes": n_lineas_antes,
        }

        async def _cerrar_ventana_al_terminar() -> dict[str, object]:
            """Cierra el asistente con ``WM_CLOSE`` a los N s (sin UIA, sin Start)."""
            await asyncio.sleep(_ESPERA_ANTES_DE_CERRAR_S)
            import psutil

            pid: int | None = None
            for proceso in psutil.process_iter(["pid", "name"]):
                if (proceso.info.get("name") or "").casefold() == NOMBRE_PROCESO:
                    pid = int(proceso.info["pid"])
            if pid is None:
                return {"cerrado": False, "motivo": "proceso no encontrado"}
            ventanas = cerrar_ventana_del_pid(pid)
            return {"cerrado": ventanas > 0, "pid": pid, "ventanas": ventanas}

        watcher = asyncio.create_task(_cerrar_ventana_al_terminar())
        try:
            stdout, stderr, rc, duracion = await runner._execute_process(
                EXE,
                argv,
                "DynDOLOD" if es_dyndolod else "TexGen",
                timeout=TIMEOUT_S,
                cwd=SESSION,
            )
            resultado.update({"rc": rc, "duracion_s": round(duracion, 1), "stdout": stdout[-2000:], "stderr": stderr[-2000:]})
            resultado["cierre"] = "el proceso terminó solo" if not watcher.done() else watcher.result()
        except DynDOLODTimeoutError as exc:
            resultado.update({"rc": "TIMEOUT (árbol terminado)", "detalle": str(exc)[:500]})
        except Exception as exc:  # noqa: BLE001 - harness de evidencia
            resultado.update({"rc": f"EXCEPCION {type(exc).__name__}", "detalle": str(exc)[:500]})
        finally:
            if not watcher.done():
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
        log(f"spawn: {resultado.get('rc')} / cierre={resultado.get('cierre')}")

        # El log del binario: SÓLO las líneas que agregó ESTA corrida.
        nuevas = lineas_desde(n_lineas_antes)
        claves = (
            "starting session",
            "Using Skyrim Special Edition Data Path",
            "Using ini:",
            "Using plugin list:",
            "Loading active plugin list",
            "Using Output Path:",
        )
        eco_nuevo: dict[str, str | None] = {clave: eco(nuevas, clave) for clave in claves}
        resultado["eco"] = eco_nuevo
        resultado["lineas_nuevas"] = len(nuevas)
        escribir(f"02-eco-{PREFIJO}.txt", [f"{clave} :: {valor}" for clave, valor in eco_nuevo.items()])
        escribir(f"03-log-nuevo-{PREFIJO}.txt", nuevas[:80])
        escribir(f"04-resultado-{PREFIJO}.json", [json.dumps(resultado, ensure_ascii=False, indent=2, default=str)])
        log("eco: " + json.dumps(eco_nuevo, ensure_ascii=False))
        return 0
    finally:
        try:
            if workspace.ownership is not None:
                await workspace.ownership.liberar()
        finally:
            try:
                await bus.stop()
            finally:
                try:
                    await lock_manager.close()
                finally:
                    await journal.close()
                    await coordinacion.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
