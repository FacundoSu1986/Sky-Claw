"""Matriz FILE_SHARE_NONE con procesos externos reales, códigos Win32 medidos.

El tenedor usa el módulo real de Sky-Claw.  Cada agresor corre en un PROCESO
SEPARADO (lo que hace válida la medición) y reporta el código Win32 exacto.

Uso: python matrix_win32.py <run_dir>
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import pathlib
import subprocess
import sys
import time

from sky_claw.local.frozen_runtime import mo2_ini_exclusion as gate

from harness_validacion import HarnessError, exigir_bloqueo, exigir_marca, exigir_resultado

#: Se completa en :func:`configurar`; así el módulo se puede importar desde los
#: tests sin depender de ``sys.argv``.
RUN: pathlib.Path = pathlib.Path()
CONTENIDO = b"[General]\ngameName=Skyrim Special Edition\ngamePath=G:/Lab_Compat/Runtime\n"

AGRESORES: dict[str, str] = {
    "escribir": """
import sys
ruta = sys.argv[1]
try:
    with open(ruta, "r+b") as f:
        f.write(b"PWNED")
    print("EXITO")
except OSError as e:
    print(f"BLOQUEADO:{getattr(e, 'winerror', None) or e.errno}")
""",
    "truncar": """
import sys
ruta = sys.argv[1]
try:
    with open(ruta, "r+b") as f:
        f.truncate(0)
    print("EXITO")
except OSError as e:
    print(f"BLOQUEADO:{getattr(e, 'winerror', None) or e.errno}")
""",
    "borrar": """
import os, sys
try:
    os.unlink(sys.argv[1])
    print("EXITO")
except OSError as e:
    print(f"BLOQUEADO:{getattr(e, 'winerror', None) or e.errno}")
""",
    "mover_encima": """
import os, pathlib, sys
destino, intruso = sys.argv[1], sys.argv[2]
pathlib.Path(intruso).write_bytes(b"OTRO")
try:
    os.replace(intruso, destino)
    print("EXITO")
except OSError as e:
    print(f"BLOQUEADO:{getattr(e, 'winerror', None) or e.errno}")
""",
    "replacefilew": """
import ctypes, pathlib, sys
from ctypes import wintypes
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
kernel32.ReplaceFileW.restype = wintypes.BOOL
destino, reemplazo = sys.argv[1], sys.argv[2]
pathlib.Path(reemplazo).write_bytes(b"NUEVO")
ok = kernel32.ReplaceFileW(destino, reemplazo, None, 0, None, None)
if ok:
    print("EXITO")
else:
    print(f"BLOQUEADO:{ctypes.get_last_error()}")
""",
    "leer_crt": """
import sys
try:
    with open(sys.argv[1], "rb") as f:
        f.read()
    print("EXITO")
except OSError as e:
    print(f"BLOQUEADO:{getattr(e, 'winerror', None) or e.errno}")
""",
    "escribir_win32": """
import ctypes, sys
from ctypes import wintypes
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.WriteFile.restype = wintypes.BOOL
GENERIC_WRITE = 0x40000000
h = kernel32.CreateFileW(sys.argv[1], GENERIC_WRITE, 7, None, 3, 0x80, None)
INVALIDO = (1 << 64) - 1
if not h or int(h) == INVALIDO:
    print(f"BLOQUEADO:{ctypes.get_last_error()}")
else:
    escritos = wintypes.DWORD(0)
    ok = kernel32.WriteFile(ctypes.c_void_p(h), b"PWNED", 5, ctypes.byref(escritos), None)
    kernel32.CloseHandle(ctypes.c_void_p(h))
    print("EXITO" if ok else f"BLOQUEADO:{ctypes.get_last_error()}")
""",
    "leer_win32": """
import ctypes, sys
from ctypes import wintypes
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
GENERIC_READ = 0x80000000
h = kernel32.CreateFileW(sys.argv[1], GENERIC_READ, 7, None, 3, 0x80, None)
INVALIDO = (1 << 64) - 1
if not h or int(h) == INVALIDO:
    print(f"BLOQUEADO:{ctypes.get_last_error()}")
else:
    kernel32.CloseHandle(ctypes.c_void_p(h))
    print("EXITO")
""",
    "abrir_solo_delete": """
import ctypes, sys
from ctypes import wintypes
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
h = kernel32.CreateFileW(sys.argv[1], 0x00010000, 7, None, 3, 0x80, None)
INVALIDO = (1 << 64) - 1
if not h or int(h) == INVALIDO:
    print(f"BLOQUEADO:{ctypes.get_last_error()}")
else:
    kernel32.CloseHandle(ctypes.c_void_p(h))
    print("EXITO")
""",
}


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def agredir(nombre: str, ini: pathlib.Path, extra: list[str] | None = None) -> str:
    script = RUN / f"agresor_{nombre}.py"
    script.write_text(AGRESORES[nombre], encoding="utf-8")
    args = [sys.executable, str(script), str(ini)] + list(extra or [])
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    salida = (proc.stdout or "").strip()
    if proc.returncode != 0 or not salida:
        raise HarnessError(
            f"agresor {nombre}: sin resultado válido (rc={proc.returncode}) "
            f"stdout={salida!r} stderr={(proc.stderr or '').strip()[:200]!r}"
        )
    return salida


def nuevo_ini(nombre: str) -> pathlib.Path:
    carpeta = RUN / nombre
    carpeta.mkdir(parents=True, exist_ok=True)
    ini = carpeta / "modorganizer.ini"
    ini.write_bytes(CONTENIDO)
    return ini


def medir() -> dict[str, object]:
    RUN.mkdir(parents=True, exist_ok=True)
    resultados: dict[str, object] = {}

    # --- matriz: cada agresor contra un INI distinto, con el lock retenido ---
    for nombre in ("escribir", "truncar", "borrar", "mover_encima", "replacefilew", "leer_crt", "escribir_win32", "leer_win32", "abrir_solo_delete"):
        ini = nuevo_ini(nombre)
        previo = sha(ini)
        exclusion = gate.adquirir_exclusion_ini(ini)
        try:
            assert exclusion.autoriza, exclusion.diagnostico.motivo
            if nombre == "mover_encima":
                salida = agredir(nombre, ini, [str(ini.parent / "intruso.ini")])
            elif nombre == "replacefilew":
                salida = agredir(nombre, ini, [str(ini.parent / "reemplazo.ini")])
            else:
                salida = agredir(nombre, ini)
        finally:
            exclusion.cerrar()
        intacto = sha(ini) == previo if ini.exists() else False
        exigir_bloqueo(salida, f"matriz/{nombre}")
        exigir_resultado(f"matriz/{nombre}", intacto, "el INI cambió bajo la exclusión")
        resultados[nombre] = {
            "resultado": salida,
            "archivo_intacto": intacto,
            "archivo_existe": ini.exists(),
        }

    # --- control: liberado el handle, el mismo agresor escribe ---
    ini = nuevo_ini("control_liberacion")
    resultados["control_tras_cerrar"] = agredir("escribir", ini)
    exigir_marca(resultados["control_tras_cerrar"], "control_tras_cerrar", "EXITO")

    # --- alias hardlink: la exclusión es sobre el OBJETO ---
    ini = nuevo_ini("alias")
    alias = ini.parent / "alias.ini"
    previo = sha(ini)
    exclusion = gate.adquirir_exclusion_ini(ini)
    try:
        import os

        os.link(ini, alias)
        salida_alias = agredir("escribir", alias)
        nlinks = exclusion.identidad.nlinks if exclusion.identidad else None
    finally:
        exclusion.cerrar()
    resultados["alias_hardlink"] = {
        "resultado_por_alias": salida_alias,
        "nlinks_antes_del_alias": 1,
        "nlinks_reportados_por_la_adquisicion": nlinks,
        "objeto_intacto": sha(ini) == previo,
    }
    exigir_bloqueo(salida_alias, "matriz/alias")
    exigir_resultado("matriz/alias", sha(ini) == previo, "el objeto cambió por el alias")

    # --- muerte abrupta del tenedor ---
    ini = nuevo_ini("muerte")
    codigo = (
        "import os\n"
        "from sky_claw.local.frozen_runtime import mo2_ini_exclusion as g\n"
        f"e = g.adquirir_exclusion_ini({str(ini)!r})\n"
        "assert e.autoriza\n"
        "os._exit(9)\n"
    )
    proc = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, timeout=60, check=False)
    time.sleep(0.2)
    reacquire = gate.adquirir_exclusion_ini(ini, intentos=10, espera_s=0.01)
    try:
        resultados["muerte_abrupta"] = {
            "rc_del_tenedor": proc.returncode,
            "readquisicion": reacquire.estado.value,
            "autoriza": reacquire.autoriza,
        }
    finally:
        reacquire.cerrar()

    # El tenedor debía morir con 9 y, al morir, el kernel libera el handle: si la
    # readquisición no autoriza, el escenario no mide lo que declara.
    exigir_resultado("muerte_abrupta/rc", proc.returncode == 9, proc.returncode)
    exigir_resultado("muerte_abrupta/readquisicion", reacquire.autoriza, reacquire.diagnostico.motivo)

    # --- contención transitoria: reintento acotado ---
    ini = nuevo_ini("reintento")
    a = gate.adquirir_exclusion_ini(ini)
    inicio = time.monotonic()
    b = gate.adquirir_exclusion_ini(ini, intentos=3, espera_s=0.005)
    transcurrido = time.monotonic() - inicio
    try:
        resultados["contencion_con_reintento"] = {
            "segunda_adquisicion": b.estado.value,
            "autoriza": b.autoriza,
            "motivo": b.diagnostico.motivo,
            "segundos": round(transcurrido, 4),
        }
        exigir_resultado(
            "contencion_con_reintento",
            b.estado.value == "EXCLUSION_NOT_ESTABLISHED" and not b.autoriza,
            b.estado.value,
        )
    finally:
        b.cerrar()
        a.cerrar()

    resultados["windows_build"] = sys.getwindowsversion().build
    resultados["python"] = sys.version.split()[0]
    return resultados


def configurar(argv: list[str]) -> None:
    """Fija ``RUN`` desde argv. Fuera de ``main`` el módulo es importable."""
    global RUN
    if len(argv) < 2:
        raise SystemExit("uso: matrix_win32.py <run_dir>")
    RUN = pathlib.Path(argv[1]).resolve()


def main() -> int:
    configurar(sys.argv)
    resultados = medir()
    (RUN / "matrix.json").write_text(json.dumps(resultados, indent=2, ensure_ascii=True), encoding="utf-8")
    # Sólo se publica ok si ``medir`` llegó al final: cualquier escenario fuera de
    # contrato levanta y el proceso termina sin este JSON.
    print(json.dumps({"ok": True, "run": str(RUN)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


