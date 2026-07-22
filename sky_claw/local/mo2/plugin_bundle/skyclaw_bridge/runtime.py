"""Núcleo stdlib y testeable del plugin, sin imports de mobase/PyQt."""

from __future__ import annotations

import contextlib
import ctypes
import pathlib
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

PROTOCOL_VERSION = 1
_LAUNCH_FIELDS = frozenset({"protocol_version", "type", "job_id", "profile", "manifest_path", "overwrite_mod"})


class BridgeCommandError(RuntimeError):
    """Comando del daemon no permitido por el bridge mínimo."""


class OrganizerApi(Protocol):
    def startApplication(self, *args: object) -> int: ...  # noqa: N802 - API MO2

    def waitForApplication(  # noqa: N802 - API MO2
        self, handle: int, refresh: bool
    ) -> tuple[bool, int]: ...


class JobObject(Protocol):
    def assign(self, process_handle: int) -> None: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


def _safe_component(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise BridgeCommandError(f"{field} debe ser un string no vacío")
    if value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise BridgeCommandError(f"{field} no es un componente seguro")
    if any(ord(char) < 0x20 for char in value):
        raise BridgeCommandError(f"{field} contiene caracteres de control")
    return value


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    job_id: str
    profile: str
    manifest_path: pathlib.Path
    overwrite_mod: str | None


def validate_launch_request(
    message: Mapping[str, object],
    *,
    jobs_root: pathlib.Path,
) -> LaunchRequest:
    """Acepta solo ``launch_worker`` y confina el manifiesto al jobs_root."""
    unexpected = set(message) - _LAUNCH_FIELDS
    if unexpected:
        raise BridgeCommandError(f"campos no permitidos en launch_worker: {sorted(unexpected)}")
    if message.get("protocol_version") != PROTOCOL_VERSION or message.get("type") != "launch_worker":
        raise BridgeCommandError("comando launch_worker incompatible")
    job_id = _safe_component(message.get("job_id"), field="job_id")
    profile = _safe_component(message.get("profile"), field="profile")
    raw_manifest = message.get("manifest_path")
    if not isinstance(raw_manifest, str) or not raw_manifest:
        raise BridgeCommandError("manifest_path debe ser una ruta absoluta")
    manifest = pathlib.Path(raw_manifest)
    if not manifest.is_absolute():
        raise BridgeCommandError("manifest_path debe ser una ruta absoluta")
    root = jobs_root.resolve()
    resolved_manifest = manifest.resolve()
    try:
        resolved_manifest.relative_to(root)
    except ValueError as exc:
        raise BridgeCommandError("manifest_path queda fuera del jobs_root") from exc
    if manifest.is_symlink() or not resolved_manifest.is_file():
        raise BridgeCommandError("manifest_path debe ser un archivo regular, no symlink")

    raw_overwrite = message.get("overwrite_mod")
    overwrite: str | None = None
    if raw_overwrite is not None:
        overwrite = _safe_component(raw_overwrite, field="overwrite_mod")
    return LaunchRequest(
        job_id=job_id,
        profile=profile,
        manifest_path=resolved_manifest,
        overwrite_mod=overwrite,
    )


def build_worker_arguments(
    *,
    fixed_prefix: Sequence[str],
    request: LaunchRequest,
    descriptor_path: pathlib.Path,
) -> list[str]:
    """Construye args desde configuración fija + identificadores validados."""
    if not fixed_prefix or any(not isinstance(arg, str) or not arg for arg in fixed_prefix):
        raise BridgeCommandError("fixed_prefix debe contener argumentos no vacíos")
    descriptor = descriptor_path.resolve()
    if not descriptor.is_absolute():
        raise BridgeCommandError("descriptor_path debe ser absoluto")
    return [
        *fixed_prefix,
        "--manifest",
        str(request.manifest_path),
        "--descriptor",
        str(descriptor),
        "--job-id",
        request.job_id,
    ]


def flush_pending_events(
    pending: queue.Queue[dict[str, object]],
    *,
    timeout: float,
    still_running: Callable[[], bool],
    poll_interval: float = 0.02,
) -> bool:
    """Espera acotado a que el consumidor marque ``task_done`` sobre todo lo encolado.

    ``unfinished_tasks`` cubre también el evento ya sacado de la cola pero aún
    no escrito al socket — ``empty()`` mentiría en esa ventana. El corte por
    ``still_running`` evita esperar el timeout completo cuando el hilo
    consumidor ya murió y nada va a drenar la cola.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pending.unfinished_tasks:
            return True
        if not still_running():
            return False
        time.sleep(poll_interval)
    return not pending.unfinished_tasks


@dataclass(slots=True)
class _RunningJob:
    request: LaunchRequest
    process_handle: int
    job_object: JobObject
    monitor: threading.Thread | None = None


class BridgeLaunchController:
    """Lanza en el hilo Qt y espera el HANDLE fuera del hilo de UI."""

    def __init__(
        self,
        *,
        organizer: OrganizerApi,
        worker_executable: pathlib.Path,
        worker_prefix: Sequence[str],
        descriptor_path: pathlib.Path,
        jobs_root: pathlib.Path,
        send_event: Callable[[dict[str, object]], None],
        job_factory: Callable[[], JobObject] | None = None,
    ) -> None:
        worker = worker_executable.resolve()
        if not worker.is_file() or worker.is_symlink():
            raise BridgeCommandError("worker_executable fijo no existe o es symlink")
        self._organizer = organizer
        self._worker = worker
        self._prefix = tuple(worker_prefix)
        self._descriptor = descriptor_path.resolve()
        self._jobs_root = jobs_root.resolve()
        self._send_event = send_event
        self._job_factory = job_factory or Win32JobObject
        self._jobs: dict[str, _RunningJob] = {}
        self._monitors: list[threading.Thread] = []
        self._lock = threading.Lock()

    def launch(self, message: Mapping[str, object]) -> None:
        request = validate_launch_request(message, jobs_root=self._jobs_root)
        with self._lock:
            if self._jobs:
                raise BridgeCommandError("la instancia MO2 ya tiene un worker activo")
        arguments = build_worker_arguments(
            fixed_prefix=self._prefix,
            request=request,
            descriptor_path=self._descriptor,
        )
        overwrite = "" if request.overwrite_mod is None else request.overwrite_mod
        job_object = self._job_factory()
        handle = 0
        try:
            handle = self._organizer.startApplication(
                str(self._worker),
                arguments,
                str(self._worker.parent),
                request.profile,
                overwrite,
                False,
            )
            if type(handle) is not int or handle in (0, -1):
                raise BridgeCommandError("MO2 no devolvió un HANDLE válido para el worker")
            job_object.assign(handle)
        except Exception:
            if type(handle) is int and handle not in (0, -1):
                with contextlib.suppress(Exception):
                    job_object.terminate()
                with contextlib.suppress(Exception):
                    self._organizer.waitForApplication(handle, False)
            job_object.close()
            raise
        running = _RunningJob(request=request, process_handle=handle, job_object=job_object)
        with self._lock:
            if self._jobs:
                job_object.terminate()
                with contextlib.suppress(Exception):
                    self._organizer.waitForApplication(handle, False)
                job_object.close()
                raise BridgeCommandError("otro worker ganó la carrera de lanzamiento")
            self._jobs[request.job_id] = running
        self._send_event(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "launch_ack",
                "job_id": request.job_id,
            }
        )
        monitor = threading.Thread(
            target=self._monitor,
            args=(running,),
            name=f"skyclaw-vfs-{request.job_id}",
            daemon=True,
        )
        running.monitor = monitor
        try:
            monitor.start()
        except Exception:
            with self._lock:
                self._jobs.pop(request.job_id, None)
            with contextlib.suppress(Exception):
                job_object.terminate()
            with contextlib.suppress(Exception):
                self._organizer.waitForApplication(handle, False)
            job_object.close()
            raise
        with self._lock:
            self._monitors.append(monitor)

    def _monitor(self, running: _RunningJob) -> None:
        wait_ok = False
        exit_code = -1
        wait_error: str | None = None
        try:
            wait_ok, exit_code = self._organizer.waitForApplication(running.process_handle, False)
        except Exception as exc:
            wait_error = f"waitForApplication falló: {exc}"
        finally:
            running.job_object.close()
            with self._lock:
                self._jobs.pop(running.request.job_id, None)
        event: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "type": "event",
            "event": "worker_exit",
            "job_id": running.request.job_id,
            "wait_ok": wait_ok,
            "exit_code": exit_code,
        }
        if wait_error is not None:
            event["message"] = wait_error
        self._send_event(event)

    def cancel(self, message: Mapping[str, object]) -> None:
        allowed = {"protocol_version", "type", "job_id"}
        if set(message) - allowed:
            raise BridgeCommandError("cancel contiene campos no permitidos")
        if message.get("protocol_version") != PROTOCOL_VERSION or message.get("type") != "cancel":
            raise BridgeCommandError("comando cancel incompatible")
        job_id = _safe_component(message.get("job_id"), field="job_id")
        with self._lock:
            running = self._jobs.get(job_id)
        if running is None:
            raise BridgeCommandError("cancel apunta a un job desconocido")
        running.job_object.terminate()

    def stop(self) -> None:
        with self._lock:
            running = tuple(self._jobs.values())
        for job in running:
            job.job_object.terminate()

    def wait_for_monitors(self, *, timeout: float) -> None:
        # Join sobre los threads reales, no sobre ``_jobs``: el monitor
        # des-registra el job ANTES de emitir worker_exit, así que mirar el
        # dict corre la carrera y puede devolver con el evento aún sin encolar.
        with self._lock:
            monitors = tuple(self._monitors)
        for monitor in monitors:
            monitor.join(timeout=timeout)
        with self._lock:
            self._monitors = [monitor for monitor in self._monitors if monitor.is_alive()]


if sys.platform == "win32":
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
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

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class Win32JobObject:
    """Job Object kill-on-close que contiene al worker y todos sus descendientes."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise BridgeCommandError("Win32 Job Objects solo están disponibles en Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateProcess.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise BridgeCommandError(f"CreateJobObjectW falló: {ctypes.get_last_error()}")
        self._handle = handle
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            self.close()
            raise BridgeCommandError(f"SetInformationJobObject falló: {error}")

    def assign(self, process_handle: int) -> None:
        if self._handle is None:
            raise BridgeCommandError("Job Object ya cerrado")
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            error = ctypes.get_last_error()
            self._kernel32.TerminateProcess(process_handle, 1)
            raise BridgeCommandError(f"AssignProcessToJobObject falló: {error}")

    def terminate(self) -> None:
        if self._handle is not None and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise BridgeCommandError(f"TerminateJobObject falló: {ctypes.get_last_error()}")

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle is not None:
            self._kernel32.CloseHandle(handle)
            self._handle = None
