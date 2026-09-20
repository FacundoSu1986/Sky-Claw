"""rig_cli_prereqs.py — Rig REAL mínimo de prerrequisitos CLI (#593).

No es código productivo. Boundary que ejercita:

    DynDOLODPipelineService._ensure_runner()   (camino productivo, sin inyectar
                                                `DynDOLODConfig` a mano)
    → DynDOLODConfig(ini_dir, plugins_file)    (wiring nuevo)
    → DynDOLODRunner._build_xedit_args()       (builder compartido)
    → DynDOLODRunner._execute_process()        (spawn real, CREATE_NO_WINDOW)

El workspace se resuelve con la maquinaria real (`resolver_workspace`,
ownership vivo) porque el fence P2.2 corre antes del spawn; el rig NO muta el
root (no pulsa Start), sólo recibe el argv. Lanza TexGen/DynDOLOD Alpha-209,
espera el eco del binario y cierra únicamente el proceso descendiente de ESTE
rig con ``WM_CLOSE``; el timeout del runner queda como último fail-safe.

Uso: python rig_cli_prereqs.py <repo> <session-dir> [timeout-s] [texgen|dyndolod]
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


def _redactar_evidencia(texto: str) -> str:
    """Redacta el home local, también cuando está escapado dentro de JSON."""
    homes = {str(pathlib.Path.home())}
    userprofile = os.environ.get("USERPROFILE", "").strip()
    if userprofile:
        homes.add(userprofile)
    candidatos: set[str] = set()
    for home in homes:
        if not home:
            continue
        candidatos.add(home)
        candidatos.add(json.dumps(home, ensure_ascii=False)[1:-1])
    for candidato in sorted(candidatos, key=len, reverse=True):
        texto = texto.replace(candidato, "%USERPROFILE%")
    return texto


def escribir(nombre: str, lineas: list[str]) -> None:
    contenido = _redactar_evidencia("\n".join(lineas) + "\n")
    (SESSION / nombre).write_text(contenido, encoding="utf-8")


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
    """Manda ``WM_CLOSE`` a las ventanas visibles del PID. Devuelve cuántos envíos tuvieron éxito.

    ``PostMessageW`` devuelve 0 cuando el mensaje no se pudo encolar (p. ej. la
    ventana dejó de existir entre la enumeración y el envío): una ventana
    encontrada NO implica un cierre efectivamente enviado.
    """
    user32 = ctypes.windll.user32
    ventanas = _ventanas_del_pid(pid)
    enviadas = 0
    for hwnd in ventanas:
        if user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0):
            enviadas += 1
    return enviadas


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

        import psutil

        proceso_rig = psutil.Process(os.getpid())
        hijos_previos = {proceso.pid for proceso in proceso_rig.children(recursive=True)}
        exe_esperado = os.path.normcase(os.path.normpath(str(EXE.resolve())))

        async def _cerrar_ventana_al_terminar() -> dict[str, object]:
            """Cierra sólo el descendiente creado por ESTE rig (sin UIA, sin Start)."""
            await asyncio.sleep(_ESPERA_ANTES_DE_CERRAR_S)

            candidatos: list[int] = []
            for proceso in proceso_rig.children(recursive=True):
                if proceso.pid in hijos_previos:
                    continue
                try:
                    nombre = proceso.name().casefold()
                    exe_real = os.path.normcase(os.path.normpath(proceso.exe()))
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    continue
                if nombre == NOMBRE_PROCESO and exe_real == exe_esperado:
                    candidatos.append(proceso.pid)

            if len(candidatos) != 1:
                return {
                    "cerrado": False,
                    "motivo": f"se esperaba 1 descendiente {NOMBRE_PROCESO}, encontrados={candidatos}",
                    "candidatos": candidatos,
                }
            pid = candidatos[0]
            enviadas = cerrar_ventana_del_pid(pid)
            return {"cerrado": enviadas > 0, "pid": pid, "enviadas": enviadas}

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
            "Using Temp Path:",
            "Using Output Path:",
        )
        eco_nuevo: dict[str, str | None] = {clave: eco(nuevas, clave) for clave in claves}
        resultado["eco"] = eco_nuevo
        resultado["lineas_nuevas"] = len(nuevas)

        def _valor_de_switch(prefijo: str) -> str:
            coincidencias = [arg[len(prefijo) :] for arg in argv if arg.startswith(prefijo)]
            if len(coincidencias) != 1:
                raise RuntimeError(f"argv no contiene exactamente un {prefijo}: {argv!r}")
            return coincidencias[0]

        esperado_d = _valor_de_switch("-d:")
        esperado_m = _valor_de_switch("-m:")
        esperado_p = _valor_de_switch("-p:")
        esperado_t = _valor_de_switch("-t:")
        esperado_o = _valor_de_switch("-o:")
        ecos_esperados = {
            "Using Skyrim Special Edition Data Path": f"Using Skyrim Special Edition Data Path: {esperado_d}",
            "Using ini:": f"Using ini: {esperado_m}\\Skyrim.ini",
            "Using plugin list:": f"Using plugin list: {esperado_p}",
            "Loading active plugin list": f"Loading active plugin list: {esperado_p}",
            "Using Temp Path:": f"Using Temp Path: {esperado_t}",
            "Using Output Path:": f"Using Output Path: {esperado_o}",
        }
        errores: list[str] = []
        for clave, esperado in ecos_esperados.items():
            observado = eco_nuevo.get(clave)
            if observado != esperado:
                errores.append(f"{clave}: esperado={esperado!r} observado={observado!r}")

        sesion = eco_nuevo.get("starting session")
        prefijo_sesion = (
            "DynDOLOD 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE) ("
            if es_dyndolod
            else "TexGen 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE) ("
        )
        if not isinstance(sesion, str) or not sesion.startswith(prefijo_sesion) or " starting session " not in sesion:
            errores.append(f"starting session inesperada: {sesion!r}")

        if resultado.get("rc") != 0:
            errores.append(f"rc inesperado: {resultado.get('rc')!r}")
        cierre = resultado.get("cierre")
        if not isinstance(cierre, dict) or cierre.get("cerrado") is not True:
            errores.append(f"el rig no cerró su propio proceso con WM_CLOSE: {cierre!r}")

        resultado["validacion"] = {"ok": not errores, "errores": errores}
        escribir(f"02-eco-{PREFIJO}.txt", [f"{clave} :: {valor}" for clave, valor in eco_nuevo.items()])
        escribir(f"03-log-nuevo-{PREFIJO}.txt", nuevas[:80])
        escribir(f"04-resultado-{PREFIJO}.json", [json.dumps(resultado, ensure_ascii=False, indent=2, default=str)])
        log("eco: " + json.dumps(eco_nuevo, ensure_ascii=False))
        if errores:
            for error in errores:
                log(f"FAIL: {error}")
            return 1
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
