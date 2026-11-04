"""Núcleo stdlib y testeable del plugin, sin imports de mobase/PyQt."""

from __future__ import annotations

import contextlib
import ctypes
import pathlib
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

PROTOCOL_VERSION = 1
_LAUNCH_FIELDS = frozenset(
    {"protocol_version", "type", "job_id", "profile", "manifest_path", "overwrite_mod", "job_object"}
)
#: Formato CERRADO del nombre del Job que el broker pide por launch. El plugin no puede importar ``sky_claw``, así que
#: lleva su copia; un test ancla que sea IGUAL a ``vfs_job_identity.PATRON_DEL_NOMBRE_DE_JOB``. ``fullmatch`` (no ``$``).
JOB_OBJECT_NAME_PATTERN = r"Local\\SkyClawVfs-[0-9a-f]{32}"
_JOB_OBJECT_NAME = re.compile(JOB_OBJECT_NAME_PATTERN)


def ascii_safe_message(message: object) -> str:
    """Evita que el logger ASCII del Python embebido de MO2 mate un thread."""

    return str(message).encode("ascii", errors="backslashreplace").decode("ascii")


class BridgeCommandError(RuntimeError):
    """Comando del daemon no permitido por el bridge mínimo."""


class BridgeJobUnknownError(BridgeCommandError):
    """El job solicitado no está activo o ya terminó en el bridge."""


class BridgeTerminationError(BridgeCommandError):
    """Falla al terminar el Job Object o proceso del worker."""


def bridge_error_event(
    *,
    command: object,
    job_id: object,
    exc: Exception,
    kind: str | None = None,
) -> dict[str, object]:
    """Construye un evento bridge_error con clasificación estructurada (kind)."""
    resolved_kind = kind
    if resolved_kind is None:
        if isinstance(exc, BridgeJobUnknownError):
            resolved_kind = "job_unknown"
        elif isinstance(exc, BridgeTerminationError):
            resolved_kind = "termination_failed"
        elif isinstance(exc, BridgeCommandError):
            resolved_kind = "rejected"
        else:
            resolved_kind = "rejected"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "event",
        "event": "bridge_error",
        "command": command,
        "job_id": job_id,
        "message": str(exc),
        "kind": resolved_kind,
    }


class BridgeEventOutbox(queue.Queue[dict[str, object]]):
    """Cola con espera acotada hasta confirmar el envío de sus eventos."""

    def wait_until_drained(self, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.all_tasks_done:
            while self.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.all_tasks_done.wait(remaining)
        return True


class OrganizerApi(Protocol):
    """API de MO2 que consume el bridge.

    Ojo con ``startApplication``: el segundo parámetro **no** es un argv
    semántico. MO2 lo une con espacios y lo pasa a ``CreateProcess`` sin citar,
    así que cada elemento tiene que llegar ya serializado (ver
    ``encode_mo2_arguments``).
    """

    def startApplication(self, *args: object) -> int: ...  # noqa: N802 - API MO2

    def waitForApplication(  # noqa: N802 - API MO2
        self, handle: int, refresh: bool
    ) -> tuple[bool, int]: ...


class JobObject(Protocol):
    def assign(self, process_handle: int) -> None: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class CommandQueue(Protocol):
    def put(self, item: dict[str, object]) -> None: ...


class BridgeCommandInbox:
    """Encola un comando y despierta al consumidor del hilo Qt."""

    def __init__(self, *, commands: CommandQueue, notify: Callable[[], None]) -> None:
        self._commands = commands
        self._notify = notify

    def deliver(self, message: Mapping[str, object], *, jobs_root: pathlib.Path) -> None:
        command = dict(message)
        command["_jobs_root"] = str(jobs_root.resolve())
        self._commands.put(command)
        self._notify()


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
    job_object: str


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
    job_object = message.get("job_object")
    if not isinstance(job_object, str) or _JOB_OBJECT_NAME.fullmatch(job_object) is None:
        # Sin un nombre de Job válido no hay identidad que verificar: no se lanza nada (y al SO no llega un string libre).
        raise BridgeCommandError("job_object ausente o con un formato no permitido")
    return LaunchRequest(
        job_id=job_id,
        profile=profile,
        manifest_path=resolved_manifest,
        overwrite_mod=overwrite,
        job_object=job_object,
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
        "--job-object",
        request.job_object,
        "--job-id",
        request.job_id,
    ]


def quote_windows_argument(argument: str) -> str:
    """Serializa un argumento con las reglas del CRT de Windows.

    Es la inversa de ``CommandLineToArgvW``, que es el parser que aplica el
    proceso hijo sobre su línea de comandos.
    """
    return subprocess.list2cmdline([argument])


def encode_mo2_arguments(arguments: Sequence[str]) -> list[str]:
    """Traduce argv semántico a los fragmentos que MO2 concatena con espacios.

    MO2 2.5.2 no cita nada: ``ProcessRunner::setFromFileOrExecutable`` hace
    ``setArguments(args.join(" "))`` y ``spawn::spawn`` compone
    ``"<bin>" + " " + arguments`` y se lo pasa a ``CreateProcess``. Cada
    elemento tiene que llegar ya serializado, o el parser CRT del worker parte
    el valor en los espacios (E3-R2-D1: ``A209 F1`` → dos tokens).

    La transformación vive sólo acá. ``build_worker_arguments`` sigue
    devolviendo conceptos, no transporte: serializar dos veces dejaría comillas
    literales dentro del valor.
    """
    return [quote_windows_argument(argument) for argument in arguments]


def wait_for_process_handle(process_handle: int) -> tuple[bool, int]:
    """Espera y cierra el HANDLE nativo sin entrar al runner Qt de MO2."""

    if sys.platform != "win32":
        raise BridgeCommandError("la espera de procesos del bridge requiere Windows")
    from ctypes import wintypes as win_types

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [win_types.HANDLE, win_types.DWORD]
    kernel32.WaitForSingleObject.restype = win_types.DWORD
    kernel32.GetExitCodeProcess.argtypes = [win_types.HANDLE, ctypes.POINTER(win_types.DWORD)]
    kernel32.GetExitCodeProcess.restype = win_types.BOOL
    kernel32.CloseHandle.argtypes = [win_types.HANDLE]
    kernel32.CloseHandle.restype = win_types.BOOL
    wait_object_0 = 0x00000000
    infinite = 0xFFFFFFFF
    try:
        wait_result = kernel32.WaitForSingleObject(process_handle, infinite)
        if wait_result != wait_object_0:
            raise BridgeCommandError(
                f"WaitForSingleObject falló: result={wait_result}, error={ctypes.get_last_error()}"
            )
        exit_code = win_types.DWORD()
        if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
            raise BridgeCommandError(f"GetExitCodeProcess falló: {ctypes.get_last_error()}")
        return True, int(exit_code.value)
    finally:
        if not kernel32.CloseHandle(process_handle):
            raise BridgeCommandError(f"CloseHandle falló: {ctypes.get_last_error()}")


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
        job_factory: Callable[[str], JobObject] | None = None,
        process_waiter: Callable[[int], tuple[bool, int]] | None = None,
        breakaway_checker: Callable[[], None] | None = None,
        log_info: Callable[[str], None] | None = None,
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
        self._process_waiter = process_waiter or wait_for_process_handle
        if breakaway_checker is not None:
            self._breakaway_checker = breakaway_checker
        elif job_factory is None and process_waiter is None and sys.platform == "win32":
            self._breakaway_checker = verify_mo2_breakaway_allowed
        else:
            self._breakaway_checker = lambda: None
        self._log_info = log_info
        self._jobs: dict[str, _RunningJob] = {}
        self._lock = threading.Lock()

    def launch(self, message: Mapping[str, object]) -> None:
        t0 = time.monotonic()
        request = validate_launch_request(message, jobs_root=self._jobs_root)
        with self._lock:
            if self._jobs:
                raise BridgeCommandError("la instancia MO2 ya tiene un worker activo")
        self._breakaway_checker()
        arguments = build_worker_arguments(
            fixed_prefix=self._prefix,
            request=request,
            descriptor_path=self._descriptor,
        )
        # Frontera de transporte: MO2 une con espacios y no cita (spawn.cpp).
        transport_arguments = encode_mo2_arguments(arguments)
        overwrite = "" if request.overwrite_mod is None else request.overwrite_mod
        # El Job nace ANTES que el proceso y con el nombre que pidió el broker: si ya existía, la creación falla y no
        # nace nada. El worker efectivo (que puede ser un hijo del proceso asignado) lo abre por ese nombre.
        job_object = self._job_factory(request.job_object)
        t_job = time.monotonic()
        handle = 0
        try:
            handle = self._organizer.startApplication(
                str(self._worker),
                transport_arguments,
                str(self._worker.parent),
                request.profile,
                overwrite,
                False,
            )
            t_start = time.monotonic()
            if type(handle) is not int or handle in (0, -1):
                raise BridgeCommandError("MO2 no devolvió un HANDLE válido para el worker")
            job_object.assign(handle)
            t_assign = time.monotonic()
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
                # El broker compara este nombre con el que pidió: sin él (o con otro) no autoriza al worker.
                "job_object": request.job_object,
            }
        )
        t_ack = time.monotonic()
        if self._log_info is not None:
            self._log_info(
                f"Sky-Claw VFS Bridge launch timings job_id={request.job_id} job_object={request.job_object} "
                f"create_job_ms={(t_job - t0) * 1000.0:.1f} startApplication_ms={(t_start - t_job) * 1000.0:.1f} "
                f"assign_ms={(t_assign - t_start) * 1000.0:.1f} total_to_ack_ms={(t_ack - t0) * 1000.0:.1f}"
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

    def _monitor(self, running: _RunningJob) -> None:
        wait_ok = False
        exit_code = -1
        wait_error: str | None = None
        try:
            wait_ok, exit_code = self._process_waiter(running.process_handle)
        except Exception as exc:
            wait_error = f"espera Win32 del worker falló: {exc}"
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
            raise BridgeJobUnknownError("cancel apunta a un job desconocido")
        running.job_object.terminate()

    def stop(self) -> None:
        with self._lock:
            running = tuple(self._jobs.values())
        for job in running:
            job.job_object.terminate()

    def wait_for_monitors(self, *, timeout: float) -> None:
        with self._lock:
            monitors = tuple(job.monitor for job in self._jobs.values() if job.monitor is not None)
        for monitor in monitors:
            monitor.join(timeout=timeout)


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

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
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
    """Job Object kill-on-close con NOMBRE que contiene al worker y todos sus descendientes.

    El nombre lo elige el broker (inadivinable, un launch = un nombre) y el worker efectivo lo abre para
    comprobar que vive dentro (``vfs_job_identity``). Por eso la creación es **fail-closed**:

    * si el nombre ya existía (squatting, reutilización) NO se adopta ese Job: se cierra el handle y se falla;
    * la DACL es de SOLO CONSULTA para el propietario y los administradores: ningún otro proceso puede
      terminar el Job, cambiarle los límites (quitar ``KILL_ON_JOB_CLOSE``) ni asignarle procesos ajenos para
      aparentar pertenencia. El creador conserva su handle con acceso total (se otorga al crear). Es defensa en
      profundidad, no una frontera de confianza frente a un proceso del mismo usuario con inyección de código.
    """

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _ERROR_ALREADY_EXISTS = 183
    _ERROR_ACCESS_DENIED = 5
    _SDDL_REVISION_1 = 1
    #: DACL protegida (sin herencia): solo ``JOB_OBJECT_QUERY`` (0x4) para el propietario y los administradores.
    _SDDL_SOLO_CONSULTA = "D:P(A;;0x4;;;OW)(A;;0x4;;;BA)"

    def __init__(self, name: str) -> None:
        if sys.platform != "win32":
            raise BridgeCommandError("Win32 Job Objects solo están disponibles en Windows")
        if _JOB_OBJECT_NAME.fullmatch(name) is None:
            raise BridgeCommandError("job_object ausente o con un formato no permitido")
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
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p
        self._handle: int | None = self._crear_con_nombre_y_dacl(name)
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

    def _crear_con_nombre_y_dacl(self, name: str) -> int:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        convertir = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convertir.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        convertir.restype = wintypes.BOOL
        descriptor = ctypes.c_void_p()
        if not convertir(self._SDDL_SOLO_CONSULTA, self._SDDL_REVISION_1, ctypes.byref(descriptor), None):
            # Sin la DACL restrictiva NO se crea con la por defecto: más permisiva = menos contención.
            raise BridgeCommandError(f"no se pudo construir la DACL del Job: {ctypes.get_last_error()}")
        try:
            atributos = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            ctypes.set_last_error(0)
            handle = self._kernel32.CreateJobObjectW(ctypes.byref(atributos), name)
            error = ctypes.get_last_error()
        finally:
            self._kernel32.LocalFree(descriptor)
        if not handle:
            if error == self._ERROR_ACCESS_DENIED:
                # Medido en Windows: si el nombre existe y OTRO Job (p. ej. uno nuestro con esta misma DACL de solo
                # consulta) lo tiene, CreateJobObjectW no puede abrirlo con acceso total y falla con ACCESS_DENIED en
                # lugar de ERROR_ALREADY_EXISTS. Sigue siendo un rechazo; solo se nombra con claridad.
                raise BridgeCommandError(
                    f"el Job {name} ya existe y no es accesible (ACCESS_DENIED): no se adopta (squatting o nombre reutilizado)"
                )
            raise BridgeCommandError(f"CreateJobObjectW falló: {error}")
        if error == self._ERROR_ALREADY_EXISTS:
            # CreateJobObjectW devuelve un handle al Job EXISTENTE: asignarle el worker sería meterlo en un
            # contenedor que no controlamos (quien lo creó puede quitarle KILL_ON_JOB_CLOSE o dejarlo vivir).
            self._kernel32.CloseHandle(handle)
            raise BridgeCommandError(f"el Job {name} ya existía: no se adopta (squatting o nombre reutilizado)")
        return int(handle)

    def assign(self, process_handle: int) -> None:
        if self._handle is None:
            raise BridgeCommandError("Job Object ya cerrado")
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            error = ctypes.get_last_error()
            self._kernel32.TerminateProcess(process_handle, 1)
            raise BridgeCommandError(f"AssignProcessToJobObject falló: {error}")

    def terminate(self) -> None:
        if self._handle is not None and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise BridgeTerminationError(f"TerminateJobObject falló: {ctypes.get_last_error()}")

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle is not None:
            self._kernel32.CloseHandle(handle)
            self._handle = None


_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_ERROR_ACCESS_DENIED = 5


def _query_current_process_job_limit_flags() -> int | None:
    """Devuelve ``LimitFlags`` del Job Object actual, o ``None`` si el proceso no pertenece a ningún Job.

    Según la documentación de Win32 para ``QueryInformationJobObject``, cuando ``hJob`` es ``NULL`` y el
    proceso llamador no está asociado a ningún Job Object, la llamada falla con ``ERROR_ACCESS_DENIED (5)``.
    """
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    info = _ExtendedLimitInformation()
    returned = wintypes.DWORD(0)
    ok = kernel32.QueryInformationJobObject(
        None,
        Win32JobObject._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(returned),
    )
    if not ok:
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:
            return None
        raise BridgeCommandError(f"QueryInformationJobObject falló en preflight de breakaway: {err}")
    return int(info.BasicLimitInformation.LimitFlags)


def verify_mo2_breakaway_allowed(*, query_flags: Callable[[], int | None] | None = None) -> None:
    """Falla cerrado antes de ``organizer.startApplication`` si MO2 no puede usar ``CREATE_BREAKAWAY_FROM_JOB``.

    En MO2 2.5.2 (``spawn.cpp``), ``startApplication`` llama a ``usvfsCreateProcessHooked`` con
    ``dwCreationFlags = CREATE_BREAKAWAY_FROM_JOB (0x01000000)``. Si ``ModOrganizer.exe`` corre dentro
    de un Job Object cuyo ``LimitFlags`` no incluye ``JOB_OBJECT_LIMIT_BREAKAWAY_OK (0x0800)`` ni
    ``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK (0x1000)``, ``CreateProcessW`` falla con ``ERROR_ACCESS_DENIED (5)``
    y MO2 abre un diálogo modal de error en el hilo Qt que bloquea ``startApplication`` e induce una
    cuarentena espuria en el broker aunque jamás existió ningún proceso worker.
    """
    flags = (query_flags or _query_current_process_job_limit_flags)()
    if flags is None:
        return
    if not (flags & (_JOB_OBJECT_LIMIT_BREAKAWAY_OK | _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)):
        raise BridgeCommandError(
            f"ModOrganizer.exe corre dentro de un Job Object sin permiso de breakaway (LimitFlags={flags:#x}); "
            "MO2/USVFS pasa CREATE_BREAKAWAY_FROM_JOB y requiere JOB_OBJECT_LIMIT_BREAKAWAY_OK"
        )
