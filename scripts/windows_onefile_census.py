"""Censo EXACTO del arranque de un ejecutable PyInstaller *onefile* dentro de un Job Object (solo Windows).

El laboratorio de ``windows_job_probe`` mide por MUESTREO cada 10 ms: un proceso más efímero que eso
(un ``cmd.exe /c ver`` del arranque del intérprete) puede no verse, y no contabiliza qué sobrevive a la
terminación del Job. Para decidir si la verificación de identidad del worker (P2) alcanza, hace falta
saber QUÉ nace antes de que corra el código de verificación y QUÉ queda fuera del Job. Este módulo lo
mide con tres instrumentos independientes que se cruzan:

* **Puerto de completion del Job (IOCP)**: el kernel avisa de CADA proceso que entra al Job
  (``JOB_OBJECT_MSG_NEW_PROCESS``) y de cada salida. No hay muestreo: es el registro exacto de la
  membresía, y ``TotalProcesses`` (contabilidad del Job) lo confirma.
* **Traza de arranque de procesos de WMI** (``Win32_ProcessStartTrace``, requiere administrador): ve
  los procesos creados FUERA del Job, que el IOCP no puede ver. Es la verdad de terreno del árbol. Si no
  está disponible, el resultado lo dice (``wmi.disponible = False``) y esa parte queda NO VERIFICADA.
* **Muestreo** del árbol con ``psutil`` (como ``windows_job_probe``): imagen, línea de comandos y
  ``IsProcessInJob`` contra el handle del Job PROPIO, nunca contra ``NULL``.

Después de observar, termina el Job (como el ``cancel`` del bridge) o cierra su handle
(``KILL_ON_JOB_CLOSE``) y registra qué procesos SIGUEN VIVOS: esa es la medida de «terminación de lo no
autorizado».

Una medición con N repeticiones NO prueba ausencia de escapes. Este módulo no concluye «seguro»: cuenta
procesos y supervivientes de una corrida concreta. La garantía se argumenta por construcción (un proceso
nace en el Job de su padre) y estas mediciones la contrastan con el sistema operativo real.

NO ejecuta TexGen, DynDOLOD, Skyrim ni MO2: solo ``python.exe``, el ejecutable bajo prueba con argumentos
inocuos y procesos que duermen.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import pathlib
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import psutil

from scripts import windows_job_probe as sonda

_ctypes_win: Any = ctypes

# JOBOBJECTINFOCLASS
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION = 7

# JOB_OBJECT_MSG_*
MENSAJES_DEL_JOB: Mapping[int, str] = {
    4: "ACTIVE_PROCESS_ZERO",
    6: "NEW_PROCESS",
    7: "EXIT_PROCESS",
    8: "ABNORMAL_EXIT_PROCESS",
}

_PROCESS_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 258
_INVALID_HANDLE_VALUE = -1

#: Cuánto se espera, tras terminar/cerrar el Job, a que mueran los procesos antes de contar supervivientes.
_GRACIA_TRAS_TERMINAR_S = 3.0
#: Cuánto se espera a que lleguen los últimos eventos asincrónicos de WMI antes de analizar.
_GRACIA_WMI_S = 1.5


# ---------------------------------------------------------------------------
# Parte PURA (se prueba en cualquier plataforma)
# ---------------------------------------------------------------------------


def _descendientes(raiz: int, aristas: Iterable[tuple[int, int]]) -> set[int]:
    """Clausura de ``raiz`` por la relación hijo→padre (``(pid, ppid)``), incluida la raíz."""
    hijos: dict[int, set[int]] = {}
    for pid, ppid in aristas:
        hijos.setdefault(ppid, set()).add(pid)
    visto = {raiz}
    pendientes = [raiz]
    while pendientes:
        for hijo in hijos.get(pendientes.pop(), ()):
            if hijo not in visto:
                visto.add(hijo)
                pendientes.append(hijo)
    return visto


def clasificar(corrida: Mapping[str, Any]) -> dict[str, Any]:
    """Cruza IOCP, WMI y muestreo de UNA corrida y devuelve la clasificación del árbol.

    * ``dentro``: pids que el kernel reportó como miembros del Job (``NEW_PROCESS``).
    * ``arbol``: raíz + descendientes conocidos por WMI o por el muestreo.
    * ``fuera``: del árbol y NO miembros. Si WMI no está disponible solo incluye lo que el muestreo vio:
      un proceso efímero creado fuera del Job es invisible y NO se puede afirmar que no existió.
    * ``efimeros_dentro``: miembros que el muestreo nunca vio (lo que el laboratorio anterior perdía).
    """
    raiz = int(corrida["raiz_pid"])
    dentro = {int(m["pid"]) for m in corrida.get("iocp", []) if m["mensaje"] == "NEW_PROCESS"}
    aristas: list[tuple[int, int]] = [(int(p["pid"]), int(p["ppid"])) for p in corrida.get("muestreo", [])]
    wmi = corrida.get("wmi", {})
    aristas += [(int(e["pid"]), int(e["ppid"])) for e in wmi.get("eventos", [])]
    arbol = _descendientes(raiz, aristas)
    vistos_por_muestreo = {int(p["pid"]) for p in corrida.get("muestreo", [])}
    # El muestreo también sabe, por IsProcessInJob, quién estaba dentro aunque el IOCP no lo haya reportado.
    dentro_por_muestreo = {int(p["pid"]) for p in corrida.get("muestreo", []) if p.get("en_job")}
    miembros = dentro | dentro_por_muestreo
    fuera = sorted(arbol - miembros)
    total = corrida.get("total_procesos_job")
    return {
        "arbol": sorted(arbol),
        "dentro": sorted(miembros),
        "fuera": fuera,
        "efimeros_dentro": sorted(dentro - vistos_por_muestreo),
        "total_job": total,
        "total_job_coincide_con_iocp": (total == len(dentro)) if isinstance(total, int) else None,
        "wmi_disponible": bool(wmi.get("disponible", False)),
        "supervivientes": [s["pid"] for s in corrida.get("supervivientes", [])],
        "raiz_ya_termino": bool(corrida.get("raiz_ya_termino", False)),
    }


def _nombre_de_imagen(ficha: Mapping[str, Any]) -> str:
    imagen = ficha.get("imagen") or ficha.get("nombre") or "?"
    return pathlib.PureWindowsPath(str(imagen)).name or "?"


def imagenes_por_pid(corrida: Mapping[str, Any]) -> dict[int, str]:
    """Mejor nombre de imagen conocido por pid (IOCP → muestreo → WMI)."""
    nombres: dict[int, str] = {}
    for origen in ("iocp", "muestreo"):
        for ficha in corrida.get(origen, []):
            if ficha.get("imagen"):
                nombres.setdefault(int(ficha["pid"]), _nombre_de_imagen(ficha))
    for evento in corrida.get("wmi", {}).get("eventos", []):
        nombres.setdefault(int(evento["pid"]), _nombre_de_imagen(evento))
    return nombres


def resumir(ruta: pathlib.Path) -> str:
    """Tabla compacta por retardo: qué nació, qué quedó fuera del Job y qué sobrevivió a la terminación."""
    corridas: list[dict[str, Any]] = json.loads(ruta.read_text(encoding="utf-8"))
    por_retardo: dict[float, list[dict[str, Any]]] = {}
    for corrida in corridas:
        por_retardo.setdefault(float(corrida["retardo_ms"]), []).append(corrida)
    lineas = [
        f"{ruta.name}: {len(corridas)} corridas",
        "retardo_ms | n | raiz_ya_termino | dentro(min-max) | fuera(min-max) | efimeros_dentro(max) | "
        "total_job==iocp | supervivientes(max) | wmi",
    ]
    for retardo in sorted(por_retardo):
        grupo = por_retardo[retardo]
        clasif = [clasificar(c) for c in grupo]
        concluyentes = [k for k in clasif if not k["raiz_ya_termino"]]
        dentro = [len(k["dentro"]) for k in concluyentes]
        fuera = [len(k["fuera"]) for k in concluyentes]
        coincide = [
            k["total_job_coincide_con_iocp"] for k in concluyentes if k["total_job_coincide_con_iocp"] is not None
        ]
        lineas.append(
            f"{retardo:>10.0f} | {len(grupo)} | {sum(k['raiz_ya_termino'] for k in clasif)} | "
            f"{_rango(dentro)} | {_rango(fuera)} | "
            f"{max((len(k['efimeros_dentro']) for k in concluyentes), default=0)} | "
            f"{sum(1 for v in coincide if v)}/{len(coincide)} | "
            f"{max((len(k['supervivientes']) for k in clasif), default=0)} | "
            f"{'si' if all(k['wmi_disponible'] for k in clasif) else 'NO'}"
        )
    imagenes: Counter[str] = Counter()
    for corrida in corridas:
        for pid, nombre in imagenes_por_pid(corrida).items():
            if pid in clasificar(corrida)["arbol"]:
                imagenes[nombre.casefold()] += 1
    lineas.append(
        "imagenes del arbol (apariciones sobre todas las corridas): " + json.dumps(dict(imagenes.most_common()))
    )
    motivos = sorted({str(c["wmi"]["motivo"]) for c in corridas if c.get("wmi", {}).get("motivo")})
    if motivos:
        lineas.append("wmi NO disponible, motivo: " + " | ".join(motivos))
    return "\n".join(lineas)


def _rango(valores: Sequence[int]) -> str:
    return f"{min(valores)}-{max(valores)}" if valores else "-"


# ---------------------------------------------------------------------------
# Parte WINDOWS
# ---------------------------------------------------------------------------

_k32_cacheado: Any = None


def _k32() -> Any:
    """``kernel32`` con las firmas del laboratorio base más las del puerto de completion."""
    global _k32_cacheado  # noqa: PLW0603 - cache de módulo para configurar las firmas una sola vez
    if _k32_cacheado is None:
        from ctypes import wintypes

        k32 = sonda._kernel32()
        k32.CreateIoCompletionPort.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD]
        k32.CreateIoCompletionPort.restype = wintypes.HANDLE
        k32.GetQueuedCompletionStatus.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        ]
        k32.GetQueuedCompletionStatus.restype = wintypes.BOOL
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        _k32_cacheado = k32
    return _k32_cacheado


def _estructuras() -> tuple[type[ctypes.Structure], type[ctypes.Structure]]:
    from ctypes import wintypes

    class _AsociarPuerto(ctypes.Structure):
        _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wintypes.HANDLE)]

    class _Contabilidad(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    return _AsociarPuerto, _Contabilidad


def _ficha_de_proceso(pid: int) -> dict[str, str | None]:
    """Imagen y línea de comandos de ``pid`` si todavía vive (mejor esfuerzo: puede haber muerto)."""
    imagen: str | None = None
    cmdline: str | None = None
    with contextlib.suppress(psutil.Error, OSError):
        proceso = psutil.Process(pid)
        imagen = str(proceso.exe())
        cmdline = " ".join(proceso.cmdline())
    return {"imagen": imagen, "cmdline": cmdline}


class _Puerto:
    """Puerto de completion asociado a un Job: registra EXACTAMENTE cada proceso que entra y sale."""

    def __init__(self, job: int, t0: float) -> None:
        asociar, _ = _estructuras()
        k32 = _k32()
        from ctypes import wintypes

        self._k32 = k32
        self._t0 = t0
        puerto = k32.CreateIoCompletionPort(wintypes.HANDLE(_INVALID_HANDLE_VALUE), None, 0, 1)
        if not puerto:
            raise sonda._fallo("CreateIoCompletionPort")
        self._puerto = int(puerto)
        info: Any = asociar()
        info.CompletionKey = 1
        info.CompletionPort = self._puerto
        if not k32.SetInformationJobObject(
            job, _JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = sonda._fallo("SetInformationJobObject(AssociateCompletionPort)")
            k32.CloseHandle(self._puerto)
            raise error
        self.mensajes: list[dict[str, Any]] = []
        self._parar = threading.Event()
        self._hilo = threading.Thread(target=self._leer, name="censo-iocp", daemon=True)
        self._hilo.start()

    def _leer(self) -> None:
        from ctypes import wintypes

        while True:
            mensaje = wintypes.DWORD(0)
            clave = ctypes.c_size_t(0)
            overlapped = ctypes.c_void_p(None)
            ok = self._k32.GetQueuedCompletionStatus(
                self._puerto, ctypes.byref(mensaje), ctypes.byref(clave), ctypes.byref(overlapped), 50
            )
            if not ok:
                if self._parar.is_set():
                    return
                continue  # WAIT_TIMEOUT u otro: se reintenta hasta que se pida parar
            nombre = MENSAJES_DEL_JOB.get(int(mensaje.value), f"MSG_{int(mensaje.value)}")
            pid = int(overlapped.value) if overlapped.value else 0
            ficha: dict[str, Any] = {"t_ms": (time.perf_counter() - self._t0) * 1000.0, "mensaje": nombre, "pid": pid}
            if nombre == "NEW_PROCESS" and pid:
                ficha.update(_ficha_de_proceso(pid))
            self.mensajes.append(ficha)

    def cerrar(self) -> None:
        self._parar.set()
        self._hilo.join(timeout=3)
        self._k32.CloseHandle(self._puerto)


class _TrazaWmi:
    """Verdad de terreno del árbol: ``Win32_ProcessStartTrace`` ve TAMBIÉN lo que nace fuera del Job.

    «Disponible» NO significa «se pudo suscribir»: significa que la traza ENTREGÓ el evento de un proceso
    canario creado a propósito. Una traza que se suscribe y no entrega nada (permisos, proveedor) daría una
    falsa sensación de «no nació nada fuera del Job»; por eso se autovalida y, si falla, dice por qué.
    """

    def __init__(self, t0: float) -> None:
        self._t0 = t0
        self.disponible = False
        self.motivo: str | None = None
        self.eventos: list[dict[str, Any]] = []
        self.errores: list[str] = []
        self._suscripta = threading.Event()
        self._parar = threading.Event()
        self._hilo = threading.Thread(target=self._correr, name="censo-wmi", daemon=True)
        self._hilo.start()
        self._suscripta.wait(timeout=20)
        if self.motivo is None:
            self._autoprobar()

    def _correr(self) -> None:
        try:
            import pythoncom  # type: ignore[import-not-found,unused-ignore]
            import wmi  # type: ignore[import-not-found,unused-ignore]

            pythoncom.CoInitialize()
            try:
                vigia = wmi.WMI().watch_for(raw_wql="SELECT * FROM Win32_ProcessStartTrace")
                self._suscripta.set()
                while not self._parar.is_set():
                    try:
                        evento = vigia(timeout_ms=100)
                    except wmi.x_wmi_timed_out:
                        continue
                    try:
                        registro = {
                            "t_ms": (time.perf_counter() - self._t0) * 1000.0,
                            "pid": int(evento.ProcessID),
                            "ppid": int(evento.ParentProcessID),
                            "nombre": str(evento.ProcessName),
                        }
                    except Exception as exc:  # noqa: BLE001 - instrumento opcional: se registra y se sigue
                        self.errores.append(f"{type(exc).__name__}: {exc}")
                        continue
                    self.eventos.append(registro)
            finally:
                pythoncom.CoUninitialize()
        except Exception as exc:  # noqa: BLE001 - instrumento opcional: cualquier falla se informa, no se oculta
            self.motivo = f"{type(exc).__name__}: {exc}"
        finally:
            self._suscripta.set()

    def _autoprobar(self) -> None:
        """Crea un proceso canario y espera su evento: solo entonces la traza cuenta como disponible."""
        import os
        import subprocess

        subprocess.run(["cmd", "/c", "exit", "0"], check=False, capture_output=True, timeout=30)  # noqa: S603, S607 - canario inocuo
        limite = time.perf_counter() + 8.0
        while time.perf_counter() < limite:
            if any(e["ppid"] == os.getpid() for e in self.eventos):
                self.disponible = True
                return
            time.sleep(0.05)
        self.motivo = (
            "la traza se suscribió pero NO entregó el evento del proceso canario "
            f"(eventos totales={len(self.eventos)}, errores={self.errores[:3]})"
        )

    def detener(self) -> None:
        self._parar.set()
        self._hilo.join(timeout=5)


def _total_y_activos(job: int) -> tuple[int | None, int | None]:
    from ctypes import wintypes

    _, contabilidad = _estructuras()
    info: Any = contabilidad()
    devueltos = wintypes.DWORD(0)
    if not _k32().QueryInformationJobObject(
        job, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(devueltos)
    ):
        return None, None
    return int(info.TotalProcesses), int(info.ActiveProcesses)


def _sigue_vivo(pid: int, creado_epoch: float | None) -> bool:
    """¿Sigue vivo ESE proceso (mismo pid y misma hora de creación)? Evita confundirlo con un pid reutilizado."""
    try:
        proceso = psutil.Process(pid)
        if creado_epoch is not None and abs(proceso.create_time() - creado_epoch) > 2.0:
            return False
        return bool(proceso.is_running()) and proceso.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def medir_censo(
    argv: Sequence[str],
    *,
    retardo_s: float,
    ventana_s: float = 12.0,
    modo_fin: str = "terminate",
    paso_s: float = 0.005,
    con_wmi: bool = True,
    terminar_tras_s: float | None = None,
) -> dict[str, Any]:
    """Lanza ``argv`` como MO2 (corriendo), asigna al Job tras ``retardo_s`` y levanta el censo exacto.

    ``modo_fin`` ``"terminate"`` imita el ``cancel`` del bridge (``TerminateJobObject``); ``"close"`` cierra
    el handle del Job (``KILL_ON_JOB_CLOSE``, lo que ocurre si el bridge muere).

    ``terminar_tras_s``: segundos DESPUÉS de la asignación en que se termina el Job aunque el árbol siga vivo.
    Sin él, el censo observa hasta que el árbol termina solo y los «supervivientes» salen siempre en cero (el
    escapado ya murió por su cuenta): no mediría la terminación, que es justo lo que se quiere medir.
    """
    sonda._exigir_windows()
    t0 = time.perf_counter()
    inicio_wall = time.time()
    traza = _TrazaWmi(t0) if con_wmi else None
    job = sonda.crear_job()
    puerto = _Puerto(job, t0)
    muestreo: dict[int, dict[str, Any]] = {}
    handles: dict[int, int] = {}
    proceso = None
    raiz_ya_termino = False
    asignado_ms = -1.0
    total = activos = None
    job_cerrado = False
    try:
        proceso = sonda.lanzar_como_mo2(argv)
        if retardo_s > 0:
            time.sleep(retardo_s)
        handle_raiz = sonda.abrir_proceso(proceso.pid)
        try:
            sonda.asignar_a_job(job, handle_raiz)
        except sonda.JobObjectError:
            if proceso.poll() is None:
                raise
            raiz_ya_termino = True
        finally:
            sonda.cerrar_handle(handle_raiz)
        asignado_ms = (time.perf_counter() - t0) * 1000.0
        limite = 0.0 if raiz_ya_termino else t0 + ventana_s
        if terminar_tras_s is not None:
            limite = min(limite, time.perf_counter() + terminar_tras_s)
        while time.perf_counter() < limite:
            try:
                raiz = psutil.Process(proceso.pid)
                candidatos = [raiz, *raiz.children(recursive=True)]
            except psutil.Error:
                break
            for candidato in candidatos:
                if candidato.pid in muestreo:
                    continue
                try:
                    # Se conserva el handle abierto: evita la reutilización del pid y permite esperar su muerte.
                    handle = _abrir_para_censo(candidato.pid)
                except sonda.JobObjectError:
                    continue
                try:
                    muestreo[candidato.pid] = {
                        "pid": candidato.pid,
                        "ppid": candidato.ppid(),
                        "imagen": str(candidato.exe()),
                        "cmdline": " ".join(candidato.cmdline()),
                        "creado_epoch": candidato.create_time(),
                        "creado_ms": (candidato.create_time() - inicio_wall) * 1000.0,
                        "en_job": sonda.proceso_en_job(handle, job),
                    }
                    handles[candidato.pid] = handle
                except (psutil.Error, sonda.JobObjectError):
                    sonda.cerrar_handle(handle)
            if proceso.poll() is not None and not any(h.is_running() for h in sonda._hijos_vivos(proceso.pid)):
                break
            time.sleep(paso_s)
        total, activos = _total_y_activos(job)
        if modo_fin == "close":
            sonda.cerrar_handle(job)
            job_cerrado = True
        else:
            with contextlib.suppress(sonda.JobObjectError):
                sonda.terminar_job(job)
        time.sleep(_GRACIA_TRAS_TERMINAR_S)
        if traza is not None:
            time.sleep(_GRACIA_WMI_S)
        supervivientes = _supervivientes(proceso.pid, muestreo, traza)
    finally:
        if proceso is not None:
            sonda.matar_arbol(proceso.pid)
            _matar_vivos(_pids_del_arbol(proceso.pid, muestreo, traza), muestreo)
            with contextlib.suppress(Exception):
                proceso.wait(timeout=10)
        for handle in handles.values():
            sonda.cerrar_handle(handle)
        if not job_cerrado:
            sonda.cerrar_handle(job)
        puerto.cerrar()
        if traza is not None:
            traza.detener()
    assert proceso is not None
    return {
        "retardo_ms": retardo_s * 1000.0,
        "asignado_ms": asignado_ms,
        "raiz_pid": proceso.pid,
        "raiz_ya_termino": raiz_ya_termino,
        "modo_fin": modo_fin,
        "iocp": puerto.mensajes,
        "total_procesos_job": total,
        "activos_job_antes_de_terminar": activos,
        "muestreo": sorted(muestreo.values(), key=lambda p: p["creado_ms"]),
        "wmi": {
            "disponible": bool(traza is not None and traza.disponible),
            "motivo": None if traza is None else traza.motivo,
            "eventos": [] if traza is None else traza.eventos,
        },
        "supervivientes": supervivientes,
    }


def _abrir_para_censo(pid: int) -> int:
    k32 = _k32()
    acceso = sonda._PROCESS_TERMINATE | sonda._PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_SYNCHRONIZE
    handle = k32.OpenProcess(acceso, False, pid)
    if not handle:
        raise sonda._fallo(f"OpenProcess({pid})")
    return int(handle)


def _pids_del_arbol(raiz_pid: int, muestreo: Mapping[int, Mapping[str, Any]], traza: _TrazaWmi | None) -> set[int]:
    aristas = [(int(p["pid"]), int(p["ppid"])) for p in muestreo.values()]
    if traza is not None:
        aristas += [(int(e["pid"]), int(e["ppid"])) for e in traza.eventos]
    return _descendientes(raiz_pid, aristas)


def _matar_vivos(pids: Iterable[int], muestreo: Mapping[int, Mapping[str, Any]]) -> None:
    """Higiene del laboratorio: un hijo que escapó del Job queda huérfano y ``matar_arbol(raiz)`` ya no lo ve."""
    for pid in pids:
        ficha = muestreo.get(pid)
        creado = float(ficha["creado_epoch"]) if ficha is not None else None
        if _sigue_vivo(pid, creado):
            with contextlib.suppress(psutil.Error):
                psutil.Process(pid).kill()


def _supervivientes(
    raiz_pid: int, muestreo: Mapping[int, Mapping[str, Any]], traza: _TrazaWmi | None
) -> list[dict[str, Any]]:
    """Procesos del árbol que SIGUEN VIVOS tras terminar/cerrar el Job (la medida de la terminación)."""
    arbol = _pids_del_arbol(raiz_pid, muestreo, traza)
    vivos: list[dict[str, Any]] = []
    for pid in sorted(arbol):
        ficha = muestreo.get(pid)
        creado = float(ficha["creado_epoch"]) if ficha is not None else None
        if _sigue_vivo(pid, creado):
            vivos.append(
                {
                    "pid": pid,
                    **_ficha_de_proceso(pid),
                    "dentro_del_job": None if ficha is None else bool(ficha["en_job"]),
                }
            )
    return vivos


# ---------------------------------------------------------------------------
# CLI (lo usa el workflow del sandbox Windows)
# ---------------------------------------------------------------------------


def _argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="windows_onefile_census", description=__doc__)
    parser.add_argument("--exe", type=pathlib.Path, help="ejecutable bajo prueba")
    parser.add_argument("--resumir", type=pathlib.Path, help="imprime la tabla de un JSON de resultados y termina")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        help="argumento del ejecutable; '{probe_file}' y '{probe_sha}' se expanden",
    )
    parser.add_argument("--retardos", default="0,0.5,1,2", help="retardos de asignación en segundos")
    parser.add_argument("--repeticiones", type=int, default=3)
    parser.add_argument("--ventana", type=float, default=12.0)
    parser.add_argument("--modo-fin", choices=("terminate", "close"), default="terminate")
    parser.add_argument(
        "--terminar-tras",
        type=float,
        default=None,
        help="segundos tras la asignación en que se termina el Job con el árbol VIVO (mide la terminación real)",
    )
    parser.add_argument(
        "--sin-wmi", action="store_true", help="no usa la traza de WMI (queda NO verificado lo externo)"
    )
    parser.add_argument("--json", type=pathlib.Path, help="ruta del JSON de resultados")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _argumentos(argv)
    if args.resumir is not None:
        print(resumir(args.resumir))  # noqa: T201 - CLI
        return 0
    if args.exe is None:
        print("windows_onefile_census: falta --exe", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    if sys.platform != "win32":
        print("windows_onefile_census: solo Windows", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    retardos = [float(valor) for valor in str(args.retardos).split(",") if valor.strip()]
    with tempfile.TemporaryDirectory(prefix="skyclaw-censo-") as temporal:
        archivo = pathlib.Path(temporal) / "canary.bin"
        archivo.write_bytes(b"canary")
        sha = hashlib.sha256(b"canary").hexdigest()
        extra = [valor.replace("{probe_file}", str(archivo)).replace("{probe_sha}", sha) for valor in args.arg]
        resultados: list[dict[str, Any]] = []
        for retardo in retardos:
            for repeticion in range(args.repeticiones):
                corrida = medir_censo(
                    [str(args.exe), *extra],
                    retardo_s=retardo,
                    ventana_s=args.ventana,
                    modo_fin=args.modo_fin,
                    con_wmi=not args.sin_wmi,
                    terminar_tras_s=args.terminar_tras,
                )
                corrida["repeticion"] = repeticion
                resultados.append(corrida)
                k = clasificar(corrida)
                print(  # noqa: T201 - CLI
                    json.dumps(
                        {
                            "retardo_ms": corrida["retardo_ms"],
                            "rep": repeticion,
                            "dentro": len(k["dentro"]),
                            "fuera": len(k["fuera"]),
                            "efimeros_dentro": len(k["efimeros_dentro"]),
                            "supervivientes": len(k["supervivientes"]),
                            "raiz_ya_termino": k["raiz_ya_termino"],
                            "wmi": k["wmi_disponible"],
                        }
                    ),
                    flush=True,
                )
    if args.json:
        args.json.write_text(json.dumps(resultados, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
