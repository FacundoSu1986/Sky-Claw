"""Unified external-process execution helpers (M-1).

Consolidates the subprocess pattern that was duplicated across the local tool
runners (BodySlide, Pandora, Wrye Bash, xEdit, Synthesis): spawn via
``asyncio.create_subprocess_exec``, capture stdout/stderr under a bounded
timeout, and guarantee that no orphaned OS process survives a timeout,
cancellation, or error.

Design notes
------------
- ``kill_and_reap`` suppresses ONLY ``TimeoutError`` during the reap; a shutdown
  ``CancelledError`` raised while reaping must propagate (the process is already
  killed), matching the canonical ``antigravity.core.windows_interop._kill_and_reap``.
- This module lives in the ``local`` layer so the local runners share one copy
  without importing it from ``antigravity`` (layering: ``local`` may depend on
  ``antigravity``, not the reverse — so the antigravity layer keeps its own
  equivalent).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger("SkyClaw.Process")

#: Windows ``CREATE_NO_WINDOW`` — suppress console popups for GUI tools.
_CREATE_NO_WINDOW = 0x08000000

#: Default grace period (seconds) to reap a killed process before giving up.
_REAP_TIMEOUT = 3.0

#: Timeout corto para ``taskkill`` — best-effort, no debe bloquear el cleanup.
_TASKKILL_TIMEOUT = 5.0


def _kill_tree_windows(pid: int) -> None:
    """Best-effort: mata el ÁRBOL de procesos en Windows vía ``taskkill /T``.

    ``proc.kill()`` solo termina el hijo directo, dejando huérfanos a los nietos
    (DynDOLOD lanza TexGen; xEdit puede lanzar procesos auxiliares). ``taskkill
    /F /T /PID`` termina el árbol completo a partir del PID raíz.

    Best-effort por diseño: cualquier fallo (proceso ya muerto, ``taskkill``
    ausente) se ignora — la garantía dura sigue siendo ``proc.kill()`` + reap.
    El ``timeout`` acotado evita que un ``taskkill`` colgado (AV, PID en estado
    raro) bloquee indefinidamente el flujo de cleanup, aun corriendo en thread.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=_TASKKILL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # subprocess.TimeoutExpired es subclase de SubprocessError → cubierto:
        # si taskkill se cuelga, se aborta y seguimos con proc.kill() + reap.
        logger.debug("taskkill best-effort falló/timeout para PID %s: %s", pid, exc)


async def kill_and_reap(
    proc: asyncio.subprocess.Process | None,
    timeout: float = _REAP_TIMEOUT,
) -> None:
    """Kill *proc* (y su árbol en Windows) y reap para no dejar procesos huérfanos.

    Safe with ``None`` (process never spawned) and tolerant of an already-exited
    process. Suppresses only the reap ``TimeoutError`` — a ``CancelledError``
    raised while reaping propagates (the process is already killed), so shutdown
    cancellation is never swallowed.

    En Windows mata el árbol completo (``taskkill /T``) ANTES del ``proc.kill()``,
    mientras la relación padre-hijo sigue intacta, para no orfanar nietos.
    """
    if proc is None:
        return
    if sys.platform == "win32" and isinstance(getattr(proc, "pid", None), int):
        await asyncio.to_thread(_kill_tree_windows, proc.pid)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=timeout)


async def run_capture(
    args: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
) -> tuple[bytes, bytes, int]:
    """Run *args* to completion, capturing stdout/stderr under *timeout*.

    On Windows, ``CREATE_NO_WINDOW`` is applied so GUI tools do not flash a
    console. The child is always killed + reaped on timeout, cancellation, or
    error — never orphaned.

    Parameters
    ----------
    args:
        Full argv vector; ``args[0]`` is the executable.
    timeout:
        Seconds before the run is aborted (the process is killed first).
    cwd:
        Working directory for the child, or ``None``.

    Returns
    -------
    tuple[bytes, bytes, int]
        ``(stdout, stderr, returncode)`` on success.

    Raises
    ------
    FileNotFoundError
        If the executable does not exist (caller maps to a domain error).
    TimeoutError
        If execution exceeds *timeout* (the process is killed first).
    OSError
        If ``communicate()`` returns without setting ``returncode`` — an
        indeterminate state that must never be reported as success.
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    if cwd is not None:
        kwargs["cwd"] = cwd

    proc: asyncio.subprocess.Process | None = None
    completed = False
    job: int | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        # U-02: meter el hijo en un Job Object kill-on-close ANTES de esperarlo.
        # Sin esto, la muerte DURA de Python (SIGKILL/OOM/corte de luz) orfana el
        # árbol completo — toda la garantía anti-huérfano vive en este `finally`,
        # que una muerte dura no ejecuta (F1). Con el job, el propio Windows mata
        # el árbol al cerrar los handles del proceso, incluso si Python nunca
        # llega a este `finally`. No-op fuera de Windows.
        job = assign_kill_on_close_job(proc.pid)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode is None:
            # No debería pasar nunca en asyncio real (communicate() espera a que
            # el proceso salga antes de retornar); si pasa, es un estado
            # indeterminado y NO se reporta como éxito (return_code=0 sería el
            # mismo falso verde por exit-code que F5 marca en otros rituales).
            # completed sigue False → el finally hace kill+reap por las dudas.
            raise OSError(f"{args[0]}: proceso terminó sin returncode tras communicate()")
        completed = True
        return stdout, stderr, proc.returncode
    finally:
        # Any non-normal exit — timeout, cancellation, or an I/O/pipe error after
        # spawn — must not leave the child running. The original exception (if
        # any) propagates unchanged; the caller maps it to a domain error.
        if not completed:
            await kill_and_reap(proc)
        # Cerrar el job en TODA salida (éxito incluido, espejo de U-07/DynDOLOD):
        # evita filtrar el handle en un proceso Python de larga vida y aniquila
        # cualquier nieto que sobreviva al padre (ya no alcanzable por PID).
        close_job(job)


async def spawn_detached(
    args: list[str],
    *,
    cwd: str | None = None,
) -> asyncio.subprocess.Process:
    """Lanza *args* como proceso interactivo *detached* — fire-and-forget.

    Contrapartida de :func:`run_capture` para las GUIs que el usuario opera y
    cierra a mano (p. ej. abrir xEdit posicionado en un conflicto para forwardeo
    manual — T-29). Las dos diferencias clave:

    - **Sin PIPE**: no captura ``stdout``/``stderr``. En una sesión larga los
      pipes se llenarían y bloquearían al proceso; además no hay salida que
      parsear.
    - **Sin kill/reap**: el proceso debe SOBREVIVIR a esta llamada (es el editor
      abierto), así que ni se trackea ni se mata al retornar.

    En Windows aplica ``CREATE_NO_WINDOW`` para no parpadear una consola (la GUI
    del editor aparece igual; solo se suprime la consola de la que colgaría).

    Parameters
    ----------
    args:
        Full argv vector; ``args[0]`` is the executable.
    cwd:
        Working directory for the child, or ``None``.

    Returns
    -------
    asyncio.subprocess.Process
        El proceso lanzado (el caller puede leer su ``pid``).

    Raises
    ------
    FileNotFoundError
        If the executable does not exist (caller maps to a domain error).
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    if cwd is not None:
        kwargs["cwd"] = cwd

    return await asyncio.create_subprocess_exec(*args, **kwargs)


def assign_kill_on_close_job(pid: int) -> int | None:
    """Windows: mete *pid* en un Job Object con ``KILL_ON_JOB_CLOSE`` y devuelve su handle.

    Cerrar ese handle (:func:`close_job`) mata el árbol completo — **incluidos
    nietos reparentados cuyo padre ya salió**, el caso que ``proc.kill()`` /
    ``taskkill /T`` NO cubren en la salida normal del proceso (U-07/U-02): una vez
    que el padre murió no hay forma de enumerar al nieto desde su PID. El job sí lo
    retiene por membresía, no por relación padre-hijo.

    Fuera de Windows, o ante cualquier fallo, devuelve ``None`` (no-op): la garantía
    dura sigue siendo :func:`kill_and_reap`. Best-effort a propósito — un fallo acá
    nunca debe romper el run.

    Espeja el ``Win32JobObject`` (validado en el smoke real del PR #350) del bundle
    del bridge MO2; no se puede importar ese porque el bundle debe ser autocontenido
    (se copia dentro de MO2) y su ``assign`` toma un HANDLE, no un pid.

    Caveat: hay una micro-ventana entre el spawn y esta asignación donde el hijo
    podría crear descendientes antes de entrar al job (lo ideal sería spawn
    suspendido, que asyncio no expone). Aceptable para el uso actual.
    """
    if sys.platform != "win32":
        return None
    # Defensivo: un pid inválido (None / no-int / ≤0) NUNCA debe tocar Win32.
    # ``OpenProcess`` con basura lanza ``ctypes.ArgumentError`` —que NO es
    # ``OSError``— y escaparía el best-effort, abortando el run (o colgándolo si el
    # caller quedó esperando un evento que ya no se setea). Además blinda los unit
    # tests que ejercitan ``_execute_process`` con procesos ``MagicMock``.
    if not isinstance(pid, int) or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    kill_on_close = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    extended_limit_class = 9  # JobObjectExtendedLimitInformation
    process_terminate = 0x0001
    process_set_quota = 0x0100

    class _BasicLimit(ctypes.Structure):
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

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job: int | None = None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        job = k32.CreateJobObjectW(None, None)
        if not job:
            logger.debug("assign_kill_on_close_job: CreateJobObjectW falló (err=%s)", ctypes.get_last_error())
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = kill_on_close
        if not k32.SetInformationJobObject(job, extended_limit_class, ctypes.byref(info), ctypes.sizeof(info)):
            logger.debug("assign_kill_on_close_job: SetInformationJobObject falló (err=%s)", ctypes.get_last_error())
            k32.CloseHandle(job)
            return None
        hproc = k32.OpenProcess(process_terminate | process_set_quota, False, pid)
        if not hproc:
            logger.debug("assign_kill_on_close_job: OpenProcess(PID %s) falló (err=%s)", pid, ctypes.get_last_error())
            k32.CloseHandle(job)
            return None
        try:
            if not k32.AssignProcessToJobObject(job, hproc):
                logger.debug(
                    "assign_kill_on_close_job: AssignProcessToJobObject falló (err=%s)", ctypes.get_last_error()
                )
                k32.CloseHandle(job)
                return None
        finally:
            k32.CloseHandle(hproc)
        return int(job)
    except (OSError, ctypes.ArgumentError, ValueError) as exc:
        # Best-effort: cualquier fallo —incluido el marshalling de argumentos, que
        # NO es OSError (ctypes.ArgumentError deriva de Exception)— deja el cleanup
        # en manos de kill_and_reap sin abortar el run. Si el job alcanzó a crearse,
        # lo cerramos para no filtrar el handle.
        logger.debug("assign_kill_on_close_job: no disponible para PID %s (no-op): %s", pid, exc)
        if job:
            close_job(int(job))
        return None


def close_job(job_handle: int | None) -> None:
    """Cierra el handle del Job Object → el SO mata el árbol (``KILL_ON_JOB_CLOSE``).

    No-op si *job_handle* es ``None`` (fuera de Windows o creación fallida). Cerrar
    tras una salida NORMAL en la que un nieto sobrevivió (heredó el pipe) es lo que
    aniquila a ese huérfano que ``kill_and_reap`` ya no alcanza (el padre murió).
    """
    if job_handle is None or sys.platform != "win32":
        return
    import ctypes

    with contextlib.suppress(OSError):
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(job_handle))
