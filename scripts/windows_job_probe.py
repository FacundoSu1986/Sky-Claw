"""Laboratorio de contención con Job Objects (solo Windows) para el fence de supervisión.

Responde, con procesos INOCUOS y en un entorno descartable, tres preguntas que el
protocolo del broker MO2/USVFS da por sentadas:

1. ¿Qué queda dentro de un Job Object y qué no? Un proceso hijo creado ANTES de
   ``AssignProcessToJobObject`` no se adopta: sobrevive a ``TerminateJobObject``.
2. ¿``IsProcessInJob(proceso, NULL)`` prueba pertenencia al Job de NUESTRO bridge?
   No: responde ``TRUE`` para pertenencia a CUALQUIER Job. La identidad se prueba
   contra el handle del Job propio.
3. ¿El proceso que ejecuta Python en ``SkyClawApp.exe`` (PyInstaller *onefile*) es el
   mismo al que el bridge asigna al Job? En onefile el bootloader es el proceso que
   MO2 lanza y Python corre en un HIJO propio: si ese hijo naciera antes de la
   asignación quedaría fuera del Job.

NO ejecuta TexGen, DynDOLOD, Skyrim ni MO2. Los únicos procesos que lanza son
``python.exe`` durmiendo, el ejecutable bajo prueba con argumentos inocuos
(``--vfs-probe-child``: lee un archivo temporal y devuelve su hash) y un ejecutable
mínimo construido para el experimento. Una medición con N iteraciones NO prueba
ausencia de escapes: este módulo MIDE la geometría del problema (cuánto tarda el hijo
en nacer frente a cuánto tarda la asignación), no la demuestra imposible.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from typing import Any

import psutil

_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ALREADY_EXISTS = 183
_MAX_PIDS = 512

#: ``ctypes`` expone ``WinDLL``/``get_last_error`` solo en Windows; vía ``Any`` el módulo se tipa igual en ambas plataformas.
_ctypes_win: Any = ctypes


class JobObjectError(OSError):
    """Falla de una primitiva Win32 de Job Objects (incluye el ``GetLastError``)."""


def _exigir_windows() -> None:
    if sys.platform != "win32":
        raise JobObjectError("los Job Objects solo existen en Windows")


def _kernel32() -> Any:
    _exigir_windows()
    from ctypes import wintypes

    k32 = _ctypes_win.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryInformationJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k32.IsProcessInJob.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _fallo(llamada: str) -> JobObjectError:
    codigo = _ctypes_win.get_last_error()
    return JobObjectError(codigo, f"{llamada} falló (GetLastError={codigo})")


class _ContadoresIo(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def _estructura_limites() -> type[ctypes.Structure]:
    from ctypes import wintypes

    class _Basicos(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _Extendidos(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basicos),
            ("IoInfo", _ContadoresIo),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return _Extendidos


def _estructura_lista_de_pids() -> type[ctypes.Structure]:
    from ctypes import wintypes

    class _Lista(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * _MAX_PIDS),
        ]

    return _Lista


# ---------------------------------------------------------------------------
# Primitivas de Job Object y de proceso
# ---------------------------------------------------------------------------


def crear_job(*, nombre: str | None = None, kill_on_close: bool = True) -> int:
    """Crea un Job (opcionalmente con nombre) y devuelve su HANDLE.

    Con ``nombre`` falla si ya existía: abrir en silencio el Job de otro sería
    asignar procesos a un contenedor que no controlamos.
    """
    k32 = _kernel32()
    handle = k32.CreateJobObjectW(None, nombre)
    if not handle:
        raise _fallo("CreateJobObjectW")
    if nombre is not None and _ctypes_win.get_last_error() == _ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        raise JobObjectError(_ERROR_ALREADY_EXISTS, f"el Job '{nombre}' ya existía: no se adopta")
    if kill_on_close:
        limites: Any = _estructura_limites()()
        limites.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limites), ctypes.sizeof(limites)
        ):
            error = _fallo("SetInformationJobObject")
            k32.CloseHandle(handle)
            raise error
    return int(handle)


def abrir_job_por_nombre(nombre: str) -> int:
    """Abre (solo consulta) un Job existente por nombre; falla si no existe."""
    k32 = _kernel32()
    consulta = 0x0004  # JOB_OBJECT_QUERY
    handle = k32.OpenJobObjectW(consulta, False, nombre)
    if not handle:
        raise _fallo("OpenJobObjectW")
    return int(handle)


def abrir_proceso(pid: int) -> int:
    """HANDLE con los permisos mínimos para asignar, consultar y terminar."""
    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise _fallo(f"OpenProcess({pid})")
    return int(handle)


def cerrar_handle(handle: int) -> None:
    if handle:
        _kernel32().CloseHandle(handle)


def asignar_a_job(job: int, proceso: int) -> None:
    if not _kernel32().AssignProcessToJobObject(job, proceso):
        raise _fallo("AssignProcessToJobObject")


def terminar_job(job: int, *, codigo: int = 1) -> None:
    if not _kernel32().TerminateJobObject(job, codigo):
        raise _fallo("TerminateJobObject")


def pids_del_job(job: int | None) -> tuple[int, ...]:
    """PIDs asignados al Job. ``None`` consulta el Job del PROCESO ACTUAL."""
    from ctypes import wintypes

    k32 = _kernel32()
    lista: Any = _estructura_lista_de_pids()()
    devueltos = wintypes.DWORD(0)
    if not k32.QueryInformationJobObject(
        job, _JOB_OBJECT_BASIC_PROCESS_ID_LIST, ctypes.byref(lista), ctypes.sizeof(lista), ctypes.byref(devueltos)
    ):
        raise _fallo("QueryInformationJobObject(BasicProcessIdList)")
    cantidad = int(lista.NumberOfProcessIdsInList)
    return tuple(int(lista.ProcessIdList[i]) for i in range(cantidad))


def job_tiene_kill_on_close(job: int | None) -> bool:
    """¿El Job tiene ``KILL_ON_JOB_CLOSE``? ``None`` consulta el Job del proceso actual."""
    from ctypes import wintypes

    k32 = _kernel32()
    limites: Any = _estructura_limites()()
    devueltos = wintypes.DWORD(0)
    if not k32.QueryInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limites),
        ctypes.sizeof(limites),
        ctypes.byref(devueltos),
    ):
        raise _fallo("QueryInformationJobObject(ExtendedLimitInformation)")
    return bool(limites.BasicLimitInformation.LimitFlags & _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)


def proceso_en_job(proceso: int, job: int | None) -> bool:
    """``IsProcessInJob``. Con ``job=None`` responde por pertenencia a CUALQUIER Job."""
    from ctypes import wintypes

    resultado = wintypes.BOOL(0)
    if not _kernel32().IsProcessInJob(proceso, job, ctypes.byref(resultado)):
        raise _fallo("IsProcessInJob")
    return bool(resultado.value)


@contextlib.contextmanager
def job_temporal(*, nombre: str | None = None, kill_on_close: bool = True) -> Iterator[int]:
    job = crear_job(nombre=nombre, kill_on_close=kill_on_close)
    try:
        yield job
    finally:
        # Cerrar un Job kill-on-close termina todo lo que contenga: red de seguridad del laboratorio.
        cerrar_handle(job)


def lanzar_como_mo2(argv: Sequence[str]) -> subprocess.Popen[bytes]:
    """Lanza ``argv`` como ``organizer.startApplication``: el proceso nace CORRIENDO (sin suspender)."""
    return subprocess.Popen(  # noqa: S603 - argv fijo del laboratorio, procesos inocuos
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def matar_arbol(pid: int) -> None:
    """Limpieza del laboratorio: mata un proceso y sus descendientes si aún viven."""
    with contextlib.suppress(psutil.Error):
        raiz = psutil.Process(pid)
        for hijo in raiz.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                hijo.kill()
        raiz.kill()


# ---------------------------------------------------------------------------
# Medición sobre un ejecutable (bootloader onefile): geometría del problema
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ProcesoObservado:
    pid: int
    ppid: int
    exe: str
    creado_ms: float
    en_job_propio: bool
    creado_antes_de_asignar: bool


@dataclasses.dataclass(frozen=True, slots=True)
class MedicionDeAsignacion:
    retardo_de_asignacion_ms: float
    asignacion_completada_ms: float
    procesos: tuple[ProcesoObservado, ...]
    imagen_python_efectiva_pid: int | None
    imagen_python_efectiva_en_job: bool | None
    arbol_completo_en_job: bool
    ventana_de_observacion_ms: float

    def a_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def medir_asignacion(
    argv: Sequence[str],
    *,
    retardo_de_asignacion_s: float,
    ventana_s: float = 12.0,
    paso_s: float = 0.01,
) -> MedicionDeAsignacion:
    """Lanza ``argv`` como lo hace MO2, asigna al Job tras ``retardo`` y observa el árbol.

    La *imagen Python efectiva* es el DESCENDIENTE más profundo que comparte imagen con el
    proceso lanzado (en *onefile* el bootloader re-ejecuta el mismo ``.exe`` como hijo).
    Si el ejecutable NO es onefile, es el propio proceso lanzado.
    """
    ejecutable = str(pathlib.Path(argv[0]).resolve()).casefold()
    with job_temporal() as job:
        inicio_wall = time.time()
        inicio = time.perf_counter()
        proceso = lanzar_como_mo2(argv)
        try:
            if retardo_de_asignacion_s > 0:
                time.sleep(retardo_de_asignacion_s)
            handle_raiz = abrir_proceso(proceso.pid)
            try:
                asignar_a_job(job, handle_raiz)
            finally:
                cerrar_handle(handle_raiz)
            asignado_wall = time.time()
            asignado_ms = (time.perf_counter() - inicio) * 1000.0

            vistos: dict[int, ProcesoObservado] = {}
            limite = inicio + ventana_s
            while time.perf_counter() < limite:
                try:
                    raiz = psutil.Process(proceso.pid)
                    candidatos = [raiz, *raiz.children(recursive=True)]
                except psutil.Error:
                    break
                for candidato in candidatos:
                    if candidato.pid in vistos:
                        continue
                    try:
                        handle = abrir_proceso(candidato.pid)
                    except JobObjectError:
                        continue
                    try:
                        en_job = proceso_en_job(handle, job)
                        creado = candidato.create_time()
                        vistos[candidato.pid] = ProcesoObservado(
                            pid=candidato.pid,
                            ppid=candidato.ppid(),
                            exe=str(candidato.exe()),
                            creado_ms=(creado - inicio_wall) * 1000.0,
                            en_job_propio=en_job,
                            creado_antes_de_asignar=creado < asignado_wall,
                        )
                    except (psutil.Error, JobObjectError):
                        continue
                    finally:
                        cerrar_handle(handle)
                if proceso.poll() is not None and not any(hijo.is_running() for hijo in _hijos_vivos(proceso.pid)):
                    break
                time.sleep(paso_s)
        finally:
            matar_arbol(proceso.pid)

    ordenados = tuple(sorted(vistos.values(), key=lambda p: p.creado_ms))
    de_la_imagen = [p for p in ordenados if p.exe.casefold() == ejecutable]
    efectiva = de_la_imagen[-1] if de_la_imagen else None
    return MedicionDeAsignacion(
        retardo_de_asignacion_ms=retardo_de_asignacion_s * 1000.0,
        asignacion_completada_ms=asignado_ms,
        procesos=ordenados,
        imagen_python_efectiva_pid=efectiva.pid if efectiva else None,
        imagen_python_efectiva_en_job=efectiva.en_job_propio if efectiva else None,
        arbol_completo_en_job=bool(ordenados) and all(p.en_job_propio for p in ordenados),
        ventana_de_observacion_ms=ventana_s * 1000.0,
    )


def _hijos_vivos(pid: int) -> list[psutil.Process]:
    try:
        return list(psutil.Process(pid).children(recursive=True))
    except psutil.Error:
        return []


# ---------------------------------------------------------------------------
# CLI (lo usa el workflow del sandbox Windows)
# ---------------------------------------------------------------------------


def _argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="windows_job_probe", description=__doc__)
    parser.add_argument("--exe", type=pathlib.Path, required=True, help="ejecutable bajo prueba")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        help="argumento para el ejecutable (repetible); '{probe_file}' y '{probe_sha}' se expanden",
    )
    parser.add_argument("--retardos", default="0,0.05,0.2,0.5,1,2", help="retardos de asignación en segundos")
    parser.add_argument("--repeticiones", type=int, default=3)
    parser.add_argument("--ventana", type=float, default=12.0)
    parser.add_argument("--json", type=pathlib.Path, help="ruta del JSON de resultados")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _argumentos(argv)
    if sys.platform != "win32":
        print("windows_job_probe: solo Windows", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    retardos = [float(valor) for valor in str(args.retardos).split(",") if valor.strip()]
    with tempfile.TemporaryDirectory(prefix="skyclaw-job-probe-") as temporal:
        archivo = pathlib.Path(temporal) / "canary.bin"
        archivo.write_bytes(b"canary")
        sha = hashlib.sha256(b"canary").hexdigest()
        extra = [valor.replace("{probe_file}", str(archivo)).replace("{probe_sha}", sha) for valor in args.arg]
        resultados: list[dict[str, Any]] = []
        for retardo in retardos:
            for repeticion in range(args.repeticiones):
                medicion = medir_asignacion(
                    [str(args.exe), *extra], retardo_de_asignacion_s=retardo, ventana_s=args.ventana
                )
                fila = medicion.a_json()
                fila["repeticion"] = repeticion
                resultados.append(fila)
                print(  # noqa: T201 - CLI
                    json.dumps(
                        {
                            "retardo_ms": fila["retardo_de_asignacion_ms"],
                            "rep": repeticion,
                            "n_procesos": len(fila["procesos"]),
                            "python_efectivo_en_job": fila["imagen_python_efectiva_en_job"],
                            "arbol_completo_en_job": fila["arbol_completo_en_job"],
                        }
                    ),
                    flush=True,
                )
    if args.json:
        args.json.write_text(json.dumps(resultados, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
