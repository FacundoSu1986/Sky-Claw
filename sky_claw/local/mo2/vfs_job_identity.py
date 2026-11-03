"""Identidad del Job Object que contiene al worker: el worker EFECTIVO se verifica a sí mismo.

Problema (#856, informe §4.2/§6). En un ejecutable PyInstaller *onefile* el proceso que MO2 lanza y que
el bridge asigna al Job es el *bootloader*; Python corre en un HIJO. Si ese hijo nace antes del
``AssignProcessToJobObject`` queda FUERA del Job, y el fence de supervisión se cumple igual (el
``launch_ack`` llega) mientras la herramienta corre sin contención.

Mecanismo (P2). El broker elige, por launch, un nombre de Job inadivinable (``Local\\SkyClawVfs-<128 bits>``);
el bridge crea el Job con ese nombre (rechazando uno preexistente) y se lo pasa al worker; ANTES de hablar con
el broker, el worker efectivo:

1. abre el Job por nombre con ``JOB_OBJECT_QUERY`` (mínimo privilegio: no puede terminarlo ni asignar);
2. pregunta ``IsProcessInJob(<este proceso>, <ESE handle>)`` —nunca ``NULL``: con ``NULL`` el SO responde
   ``TRUE`` para pertenencia a CUALQUIER Job, también uno ajeno (#856 §4.1)—;
3. exige ``KILL_ON_JOB_CLOSE`` y ninguna forma de *breakaway* en los límites de ESE Job;
4. recorre sus descendientes vivos y exige que TODOS estén en el Job;
5. cierra el handle: ``KILL_ON_JOB_CLOSE`` solo dispara cuando se cierra el ÚLTIMO handle, así que un
   worker que conservara el suyo impediría que el cierre del Job del bridge matara el árbol.

Cualquier falla es un ``JobIdentityError`` (fail-closed), también la de construir la API de Windows y la de la
limpieza (``terminar_descendientes_vivos``). Si la verificación y el cierre del handle fallan a la vez, viaja la causa
de la verificación y el fallo de cierre solo se registra. Quien lo
captura debe llamar a ``terminar_descendientes_vivos`` antes de salir: un worker que no puede demostrar su
contención INTENTA no dejar procesos propios corriendo. La terminación es un intento, no una garantía: si algún
descendiente no muere tras los intentos, ``terminar_descendientes_vivos`` falla y ese proceso puede quedar vivo.

Qué NO prueba. La autenticidad del worker frente al broker sigue siendo el HMAC del descriptor; este módulo
prueba CONTENCIÓN, no autoría. Un descendiente huérfano cuyo padre ya murió no es alcanzable por la relación
padre→hijo y queda fuera de este barrido (límite documentado en la auditoría).

Se importa muy temprano en el arranque del worker: solo stdlib, ``ctypes`` y ``psutil``; nada de ``sky_claw.*``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import psutil

logger = logging.getLogger(__name__)

#: Formato CERRADO del nombre. ``fullmatch`` (no ``$``: aceptaría un ``\n`` final) y espacio de nombres
#: ``Local\`` (por sesión): el SO nunca recibe un string arbitrario. El bridge del plugin lleva una copia
#: literal de este patrón (no puede importar ``sky_claw``); un test ancla la igualdad.
PATRON_DEL_NOMBRE_DE_JOB = r"Local\\SkyClawVfs-[0-9a-f]{32}"
_PATRON = re.compile(PATRON_DEL_NOMBRE_DE_JOB)
_PREFIJO = "Local\\SkyClawVfs-"

_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_LIMITE_KILL_ON_JOB_CLOSE = 0x00002000
_LIMITE_BREAKAWAY_OK = 0x00000800
_LIMITE_SILENT_BREAKAWAY_OK = 0x00001000
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_ERROR_INVALID_PARAMETER = 87
_ESPERA_DE_MUERTE_MS = 3000

#: ``ctypes`` expone ``WinDLL``/``get_last_error`` solo en Windows; vía ``Any`` el módulo se tipa igual en ambas plataformas.
_ctypes_win: Any = ctypes


class JobIdentityError(RuntimeError):
    """El worker no pudo demostrar que vive dentro del Job que el bridge creó para este launch."""


@dataclass(frozen=True, slots=True)
class PruebaDeJob:
    """Resultado de una verificación EXITOSA. No se construye sin pasar por ``verificar_identidad_del_job``."""

    nombre: str
    pid: int
    #: Procesos que pasaron alguna vez por el Job al momento de verificar (diagnóstico: censo exacto del arranque).
    procesos_totales: int
    procesos_activos: int


class ApiDeJobs(Protocol):
    """Las primitivas Win32 que usa la verificación; una implementación falsa permite probar las fallas."""

    def abrir_job(self, nombre: str) -> int: ...

    def proceso_actual_en_job(self, job: int) -> bool: ...

    def banderas_de_limite(self, job: int) -> int: ...

    def contabilidad(self, job: int) -> tuple[int, int]: ...

    def descendientes_vivos(self) -> Sequence[int]: ...

    def proceso_en_job(self, pid: int, job: int) -> bool | None: ...

    def terminar_proceso(self, pid: int) -> None: ...

    def cerrar(self, handle: int) -> None: ...


def generar_nombre_de_job() -> str:
    """Nombre nuevo e inadivinable (128 bits) para UN launch; lo elige el broker."""
    return f"{_PREFIJO}{secrets.token_hex(16)}"


def validar_nombre_de_job(valor: object) -> str:
    """Devuelve ``valor`` si tiene EXACTAMENTE el formato de ``generar_nombre_de_job``; si no, ``JobIdentityError``."""
    if not isinstance(valor, str) or _PATRON.fullmatch(valor) is None:
        raise JobIdentityError("el nombre del Job no tiene el formato esperado")
    return valor


def _abrir_motor(api: ApiDeJobs | None) -> ApiDeJobs:
    """La API inyectada, o la de Windows construida DENTRO de la frontera fail-closed.

    Construir ``_ApiWin32`` (``ctypes.WinDLL`` y los ``argtypes`` de kernel32) puede fallar con ``OSError`` o
    ``AttributeError``. ``worker_main`` solo captura ``JobIdentityError``: un error crudo se saldría de ahí sin el
    código 72, sin barrer a los descendientes y sin cerrar el logging. Es el ÚNICO lugar que la construye (ancla).
    """
    if api is not None:
        return api
    try:
        return _api_por_defecto()
    except JobIdentityError:
        raise
    except Exception as exc:  # frontera de verificación: toda falla se vuelve un error de identidad
        raise JobIdentityError(f"no se pudo preparar la API de Jobs de Windows: {exc}") from exc


def verificar_identidad_del_job(nombre: object, *, api: ApiDeJobs | None = None) -> PruebaDeJob:
    """Demuestra que ESTE proceso y todos sus descendientes vivos están en el Job ``nombre``, o falla cerrado."""
    nombre_valido = validar_nombre_de_job(nombre)
    motor = _abrir_motor(api)
    try:
        handle = motor.abrir_job(nombre_valido)
    except JobIdentityError:
        raise
    except Exception as exc:  # frontera de verificación: toda falla se vuelve un error de identidad
        raise JobIdentityError(f"no se pudo abrir el Job: {exc}") from exc
    # Con una verificación exitosa, un handle que no se cierra invalida la prueba (``_cerrar`` lanza). Con un fallo
    # previo, el error que viaja es la CAUSA: el del cierre no la reemplaza (``_cerrar_sin_enmascarar`` lo registra).
    cerrar: Callable[[ApiDeJobs, int], None] = _cerrar
    try:
        return _comprobar(motor, handle, nombre_valido)
    except JobIdentityError:
        cerrar = _cerrar_sin_enmascarar
        raise
    except Exception as exc:  # fail-closed: ni una excepción inesperada puede salir como éxito
        cerrar = _cerrar_sin_enmascarar
        raise JobIdentityError(f"falló la verificación del Job: {exc}") from exc
    finally:
        cerrar(motor, handle)


def _comprobar(motor: ApiDeJobs, handle: int, nombre: str) -> PruebaDeJob:
    if not motor.proceso_actual_en_job(handle):
        raise JobIdentityError("el proceso del worker no pertenece al Job creado para este launch")
    banderas = motor.banderas_de_limite(handle)
    if not banderas & _LIMITE_KILL_ON_JOB_CLOSE:
        raise JobIdentityError("el Job no tiene KILL_ON_JOB_CLOSE: cerrarlo no terminaría al worker")
    if banderas & (_LIMITE_BREAKAWAY_OK | _LIMITE_SILENT_BREAKAWAY_OK):
        raise JobIdentityError("el Job permite breakaway: un descendiente podría salirse de la contención")
    totales, activos = motor.contabilidad(handle)
    fuera: list[int] = []
    for pid in motor.descendientes_vivos():
        pertenece = motor.proceso_en_job(pid, handle)
        if pertenece is False:  # ``None`` = murió entre la enumeración y la consulta: ya no es un problema
            fuera.append(pid)
    if fuera:
        raise JobIdentityError(f"hay descendientes vivos fuera del Job: {sorted(fuera)}")
    return PruebaDeJob(nombre=nombre, pid=os.getpid(), procesos_totales=totales, procesos_activos=activos)


def _cerrar(motor: ApiDeJobs, handle: int) -> None:
    try:
        motor.cerrar(handle)
    except JobIdentityError:
        raise
    except Exception as exc:  # un handle que no se cerró invalida la propiedad que se acaba de verificar
        raise JobIdentityError(f"no se pudo cerrar el handle del Job: {exc}") from exc


def _cerrar_sin_enmascarar(motor: ApiDeJobs, handle: int) -> None:
    """Cierra el handle cuando YA hay un fallo de verificación: si el cierre también falla, solo se registra.

    Reemplazar la causa real («no pertenece al Job…») por «no se pudo cerrar» deja el log sin el diagnóstico útil.
    El resultado sigue siendo fail-closed: viaja el error de la verificación.
    """
    try:
        _cerrar(motor, handle)
    except JobIdentityError as exc:
        logger.error("además del fallo de verificación, no se pudo cerrar el handle del Job: %s", exc)


def terminar_descendientes_vivos(
    *,
    api: ApiDeJobs | None = None,
    intentos: int = 5,
    pausa_s: float = 0.1,
) -> tuple[int, ...]:
    """Intenta terminar TODOS los descendientes vivos de este proceso y devuelve sus pids.

    Es lo que hace el worker cuando no puede probar su contención: antes de salir intenta no dejar procesos propios
    corriendo. Si alguno sigue vivo tras ``intentos`` rondas, falla: no se afirma «sin residuos» sin haberlo
    comprobado (y ese proceso puede quedar vivo). Un proceso que ya murió al intentar terminarlo no es un error.

    Cumple el mismo contrato que la verificación: toda falla de la API (``psutil.Error`` al enumerar, un error de
    ``ctypes`` al terminar) es un ``JobIdentityError``. ``worker_main`` solo captura ese tipo: un error crudo se saldría
    de ahí sin el código 72 y sin cerrar el logging.
    """
    motor = _abrir_motor(api)
    terminados: list[int] = []
    try:
        for _ in range(intentos):
            vivos = list(motor.descendientes_vivos())
            if not vivos:
                return tuple(terminados)
            for pid in vivos:
                motor.terminar_proceso(pid)
                if pid not in terminados:
                    terminados.append(pid)
            time.sleep(pausa_s)
        restantes = sorted(motor.descendientes_vivos())
    except JobIdentityError:
        raise
    except Exception as exc:  # frontera de limpieza: toda falla se vuelve un error de identidad
        raise JobIdentityError(f"falló la limpieza de los descendientes: {exc}") from exc
    if restantes:
        raise JobIdentityError(f"no se pudo terminar a los descendientes: {restantes}")
    return tuple(terminados)


# ---------------------------------------------------------------------------
# Implementación real (Windows)
# ---------------------------------------------------------------------------


def _api_por_defecto() -> ApiDeJobs:
    if sys.platform != "win32":
        raise JobIdentityError(
            "la verificación de identidad del Job requiere Windows: fuera de Windows no hay prueba posible"
        )
    return _ApiWin32()


class _ApiWin32:
    """``kernel32`` real. Cada error de Win32 es un ``JobIdentityError`` con su ``GetLastError``."""

    def __init__(self) -> None:
        from ctypes import wintypes

        k32 = _ctypes_win.WinDLL("kernel32", use_last_error=True)
        k32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k32.OpenJobObjectW.restype = wintypes.HANDLE
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        k32.IsProcessInJob.restype = wintypes.BOOL
        k32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryInformationJobObject.restype = wintypes.BOOL
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        self._k32 = k32
        self._wintypes = wintypes

    def _fallo(self, llamada: str) -> JobIdentityError:
        codigo = _ctypes_win.get_last_error()
        return JobIdentityError(f"{llamada} falló (GetLastError={codigo})")

    def abrir_job(self, nombre: str) -> int:
        # Mínimo privilegio: solo consulta. Con este handle no se puede terminar el Job, cambiar sus límites
        # ni asignarle procesos.
        handle = self._k32.OpenJobObjectW(_JOB_OBJECT_QUERY, False, nombre)
        if not handle:
            raise self._fallo("OpenJobObjectW")
        return int(handle)

    def proceso_actual_en_job(self, job: int) -> bool:
        if not job:
            raise JobIdentityError("handle de Job nulo: IsProcessInJob(NULL) no prueba identidad")
        resultado = self._wintypes.BOOL(0)
        if not self._k32.IsProcessInJob(self._k32.GetCurrentProcess(), job, ctypes.byref(resultado)):
            raise self._fallo("IsProcessInJob")
        return bool(resultado.value)

    def banderas_de_limite(self, job: int) -> int:
        info = _estructura_de_limites()()
        devueltos = self._wintypes.DWORD(0)
        if not self._k32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(devueltos),
        ):
            raise self._fallo("QueryInformationJobObject(ExtendedLimitInformation)")
        return int(info.BasicLimitInformation.LimitFlags)

    def contabilidad(self, job: int) -> tuple[int, int]:
        info = _estructura_de_contabilidad()()
        devueltos = self._wintypes.DWORD(0)
        if not self._k32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(devueltos),
        ):
            raise self._fallo("QueryInformationJobObject(BasicAccountingInformation)")
        return int(info.TotalProcesses), int(info.ActiveProcesses)

    def descendientes_vivos(self) -> Sequence[int]:
        return tuple(hijo.pid for hijo in psutil.Process().children(recursive=True))

    def proceso_en_job(self, pid: int, job: int) -> bool | None:
        handle = self._k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            if _ctypes_win.get_last_error() == _ERROR_INVALID_PARAMETER:
                return None  # el proceso ya no existe
            # No se puede abrir (p. ej. ACCESS_DENIED): no se puede probar que esté en el Job → no se asume que sí.
            raise self._fallo(f"OpenProcess({pid})")
        try:
            resultado = self._wintypes.BOOL(0)
            if not self._k32.IsProcessInJob(handle, job, ctypes.byref(resultado)):
                raise self._fallo(f"IsProcessInJob({pid})")
            return bool(resultado.value)
        finally:
            self._k32.CloseHandle(handle)

    def terminar_proceso(self, pid: int) -> None:
        handle = self._k32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
        )
        if not handle:
            if _ctypes_win.get_last_error() == _ERROR_INVALID_PARAMETER:
                return  # ya murió
            raise self._fallo(f"OpenProcess({pid}, terminar)")
        try:
            self._k32.TerminateProcess(handle, 1)
            if self._k32.WaitForSingleObject(handle, _ESPERA_DE_MUERTE_MS) != _WAIT_OBJECT_0:
                raise JobIdentityError(f"el proceso {pid} no terminó tras TerminateProcess")
        finally:
            self._k32.CloseHandle(handle)

    def cerrar(self, handle: int) -> None:
        if not self._k32.CloseHandle(handle):
            raise self._fallo("CloseHandle")


def _estructura_de_limites() -> Any:
    from ctypes import wintypes

    class _Contadores(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

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
            ("IoInfo", _Contadores),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return _Extendidos


def _estructura_de_contabilidad() -> Any:
    from ctypes import wintypes

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

    return _Contabilidad
