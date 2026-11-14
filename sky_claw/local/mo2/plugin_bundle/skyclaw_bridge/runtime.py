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
from datetime import UTC, datetime
from typing import Protocol

PROTOCOL_VERSION = 1
#: Plazo para confirmar que el Job quedó VACÍO tras terminarlo, y cada cuánto se consulta. Los procesos terminados
#: mueren en milisegundos; el plazo es una cota de fallo cerrado, no una espera esperada.
_JOB_EMPTY_TIMEOUT_S = 10.0
_JOB_EMPTY_POLL_S = 0.02
_LAUNCH_FIELDS = frozenset(
    {"protocol_version", "type", "job_id", "profile", "manifest_path", "overwrite_mod", "job_object"}
)
#: Formato CERRADO del nombre del Job que el broker pide por launch. El plugin no puede importar ``sky_claw``, así que
#: lleva su copia; un test ancla que sea IGUAL a ``vfs_job_identity.PATRON_DEL_NOMBRE_DE_JOB``. ``fullmatch`` (no ``$``).
JOB_OBJECT_NAME_PATTERN = r"Local\\SkyClawVfs-[0-9a-f]{32}"
_JOB_OBJECT_NAME = re.compile(JOB_OBJECT_NAME_PATTERN)


def bridge_hello(*, instance_id: str, session_id: str, bundle_digest: str) -> dict[str, object]:
    """El ÚNICO ``hello`` del bridge: declara la identidad del bundle que está EJECUTANDO (H-5).

    ``PROTOCOL_VERSION`` no cambió con los fixes del bridge, así que no distingue un plugin viejo de uno nuevo; el digest
    del bundle sí. El broker lo compara con el de su propio producto y rechaza un bridge desactualizado.
    """

    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "hello",
        "role": "bridge",
        "instance_id": instance_id,
        "session_id": session_id,
        "bundle_digest": bundle_digest,
    }


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


def bridge_error_event_for_command(message: Mapping[str, object], exc: Exception) -> dict[str, object]:
    """``bridge_error`` de un comando fallido, con el ``command`` con el que el broker lo clasifica.

    El broker pone la instancia en cuarentena solo ante ``cancel``/``terminate``; para un ``launch_worker`` fallido
    asume «nada corrió» y libera todo. Un launch que falla porque quedó un descendiente vivo fuera del Job
    (``BridgeTerminationError``) NO es un rechazo ordinario: se reporta como ``terminate``, la misma señal que emite el
    monitor cuando el Job no se vacía, para que el broker conserve la terminalidad como desconocida.
    """
    command: object = message.get("type")
    if command == "launch_worker" and isinstance(exc, BridgeTerminationError):
        command = "terminate"
    return bridge_error_event(command=command, job_id=message.get("job_id"), exc=exc)


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

    def adopt_descendants(self, process_handle: int) -> int:
        """Asigna al Job a los descendientes VIVOS del proceso lanzado que no estén en él; devuelve cuántos adoptó.

        Lanza ``BridgeCommandError`` si alguno no se pudo contener (auditoría independiente, H-01).
        """
        ...

    def terminate(self) -> None: ...

    def active_processes(self) -> int:
        """Procesos activos hoy en el Job (``ActiveProcesses``): lo que decide si el worker terminó DE VERDAD."""
        ...

    def close(self) -> None: ...


class AdoptionApi(Protocol):
    """Lo que la adopción necesita del sistema operativo; un doble permite probar la lógica sin Windows."""

    def processes(self) -> Sequence[tuple[int, int]]:
        """``(pid, ppid)`` de los procesos vivos."""
        ...

    def process_id(self, handle: int) -> int: ...

    def open_process(self, pid: int) -> int | None:
        """Handle del proceso, ``None`` si ya no existe; ``BridgeCommandError`` si existe y no se pudo abrir."""
        ...

    def creation_time(self, handle: int) -> int: ...

    def creation_time_by_pid(self, pid: int) -> int | None:
        """Creación de ``pid`` leída del sistema SIN abrir el proceso; ``None`` si ya no figura.

        Es la única forma de saber cuándo nació un proceso que no se puede abrir (``csrss``, ``wininit``, ``winlogon``...):
        ``creation_time`` necesita un handle. Mismas unidades y reloj que ``creation_time``. ``BridgeCommandError`` si la
        lectura falla.
        """
        ...

    def has_exited(self, handle: int) -> bool: ...

    def in_job(self, handle: int) -> bool:
        """Pertenencia contra el handle del Job PROPIO. Nunca contra NULL: ``IsProcessInJob(p, NULL)`` responde por
        cualquier Job y no prueba nada."""
        ...

    def assign(self, handle: int) -> None: ...

    def terminate(self, handle: int) -> None: ...

    def close(self, handle: int) -> None: ...


#: Pasadas máximas de la adopción. Un descendiente adoptado ya hereda el Job en lo que cree después, así que el
#: punto fijo llega rápido; agotarlas es una señal (algo crea procesos más rápido de lo que se adoptan), no un caso normal.
_ADOPTION_MAX_PASSES = 8


def _salio_confirmado(api: AdoptionApi, handle: int) -> bool:
    """``True`` solo si se CONFIRMA que el proceso salió; si no se puede consultar, se lo trata como vivo."""
    try:
        return api.has_exited(handle)
    except BridgeCommandError:
        return False


def _nacio_antes_que_su_padre(api: AdoptionApi, pid: int, creacion_del_padre: int) -> bool:
    """``True`` solo con evidencia POSITIVA de que ``pid`` es más viejo que su padre, y por lo tanto no es su hijo.

    Un proceso nace después que su padre (el padre existía para crearlo, también cuando el padre se falsea), así que uno
    MÁS VIEJO que el padre retenido solo comparte con él el PPID numérico: Windows no reasigna el PPID cuando el padre
    muere y el sistema puede entregarle ese PID a otro proceso más tarde. Se lee la creación SIN abrir el proceso porque
    justamente estos procesos suelen ser de sistema y no se abren; el mismo criterio aplica ``psutil.Process.children``
    (con la misma lectura de reserva) en el lado del worker y del broker.

    Si la lectura falla, o el proceso ya no figura, NO hay evidencia: ``False``, y el candidato sigue siendo un posible
    descendiente vivo fuera del Job (fail-closed). Una creación IGUAL a la del padre tampoco descarta: un bootloader crea
    a su hijo a los pocos milisegundos.
    """
    try:
        creacion = api.creation_time_by_pid(pid)
    except BridgeCommandError:
        return False
    return creacion is not None and creacion < creacion_del_padre


def adopt_descendants(api: AdoptionApi, root_handle: int, *, max_passes: int = _ADOPTION_MAX_PASSES) -> int:
    """Contiene a TODO el árbol del proceso lanzado (auditoría independiente, H-01).

    En un ejecutable PyInstaller onefile el handle que devuelve ``startApplication`` es el del bootloader; el
    intérprete es su hijo y puede haber nacido ANTES de la asignación al Job, que no adopta a quien ya existía.
    Se recorre el árbol hasta un punto fijo y se asigna al Job a cada descendiente vivo que no esté en él.

    Un proceso es hijo de otro solo si su PPID es el de uno ya RETENIDO y nació después que él. Los handles de los
    procesos ya vistos quedan ABIERTOS hasta el final: mientras uno esté abierto su PID no puede reutilizarse, así que
    un PPID numéricamente igual al de un proceso viejo no se confunde con un hijo. Eso también permite seguir a un
    nieto cuyo padre ya murió.

    «Nació después» se lee del handle del candidato; si no se pudo abrir (un proceso de sistema con el PPID reciclado:
    ``csrss``, ``wininit`` o ``winlogon`` conservan el de un ``smss`` de sesión que ya salió) se lee del sistema sin abrirlo
    (``creation_time_by_pid``). Solo la evidencia POSITIVA de que es más viejo que su padre lo descarta; sin ella un
    candidato que no se pudo identificar sigue contando como descendiente vivo fuera del Job (ver ``_nacio_antes_que_su_padre``).

    El error es por PID (como la limpieza de H-03): el fallo de uno no corta la ronda. Un descendiente vivo que no se
    pudo adoptar se intenta terminar (no puede quedar suelto fuera del Job) y al final se lanza ``BridgeCommandError``
    que los nombra a todos, cada uno con su PPID: el bridge no emite ``launch_ack``. Devuelve cuántos adoptó.

    Dos clases de fallo, que el broker trata distinto: si todo lo que no se pudo adoptar se TERMINÓ, es un rechazo
    ordinario (``BridgeCommandError``: nada quedó vivo). Si queda un descendiente vivo FUERA del Job (no se pudo abrir ni
    terminar) o no se llegó a un punto fijo, la terminalidad es desconocida y el error es ``BridgeTerminationError``:
    ``TerminateJobObject`` y ``KILL_ON_JOB_CLOSE`` no alcanzan a lo que no está en el Job.
    """
    root_pid = api.process_id(root_handle)
    retenidos: dict[int, int] = {root_pid: api.creation_time(root_handle)}  # pid -> creación
    abiertos: list[int] = []  # handles que abrió ESTA función (el de la raíz es del bridge: no se cierra)
    ajenos: set[int] = set()
    fallos: dict[int, str] = {}
    # pid fallido -> su PPID: sin él, al leer el error un PID reciclado no se distingue de un hijo.
    padres: dict[int, int] = {}
    vivos_fuera: set[int] = set()  # fallaron y NO se pudieron terminar: siguen vivos fuera del Job
    adoptados = 0
    try:
        for _ in range(max_passes):
            hubo_nuevos = False
            for pid, ppid in api.processes():
                if pid in retenidos or pid in ajenos or pid in fallos or ppid not in retenidos:
                    continue
                handle: int | None = None
                try:
                    handle = api.open_process(pid)
                    if handle is None:
                        continue  # murió entre la enumeración y la apertura
                    abiertos.append(handle)
                    creacion = api.creation_time(handle)
                    if creacion < retenidos[ppid]:
                        ajenos.add(pid)  # PPID coincidente de un proceso viejo: no es hijo
                        abiertos.remove(handle)
                        api.close(handle)
                        continue
                    retenidos[pid] = creacion
                    hubo_nuevos = True
                    if api.has_exited(handle) or api.in_job(handle):
                        continue
                    try:
                        api.assign(handle)
                    except BridgeCommandError:
                        if api.has_exited(handle):
                            continue  # asignar a un proceso que ya salió falla con ACCESS_DENIED: no hay qué contener
                        raise
                    adoptados += 1
                except BridgeCommandError as exc:
                    # Todavía no se lo identificó como descendiente (la excepción saltó antes de retenerlo: no se pudo
                    # abrir, o no se pudo leer su creación por handle) y tampoco como ajeno. Que no se pueda leer por
                    # handle no lo vuelve un descendiente: si el sistema da su creación y es ANTERIOR a la de su padre,
                    # su PPID es un PID reciclado. Un ajeno ya identificado NO pasa por acá: que falle cerrar su handle
                    # sigue rechazando el launch (ver más abajo).
                    if (
                        pid not in retenidos
                        and pid not in ajenos
                        and _nacio_antes_que_su_padre(api, pid, retenidos[ppid])
                    ):
                        ajenos.add(pid)
                        continue
                    detalle = str(exc)
                    # Sigue vivo salvo que terminarlo funcione: sin handle (no se pudo abrir) o con ``terminate`` fallido
                    # queda un proceso fuera del Job que nada de lo que hace ``launch`` alcanza. Un PID ya identificado
                    # como AJENO (PPID coincidente de un proceso viejo) no es un descendiente: que falle cerrar su handle
                    # puede rechazar el launch, pero no es un sobreviviente que ponga la instancia en cuarentena.
                    sigue_vivo = pid not in ajenos
                    if handle is not None and pid in retenidos:
                        try:
                            api.terminate(handle)
                            sigue_vivo = False
                        except BridgeCommandError as term_exc:
                            detalle += f"; no se pudo terminarlo: {term_exc}"
                            # ``TerminateProcess`` sobre un proceso que ya salió también falla: si murió entre el
                            # chequeo de ``has_exited`` y el ``terminate``, no queda nada vivo.
                            sigue_vivo = not _salio_confirmado(api, handle)
                    fallos[pid] = detalle
                    padres[pid] = ppid
                    if sigue_vivo:
                        vivos_fuera.add(pid)
            if not hubo_nuevos:
                break
        else:
            raise BridgeTerminationError(
                f"la adopción de descendientes no convergió en {max_passes} pasadas: algo sigue creando procesos"
            )
    finally:
        for handle_abierto in abiertos:
            with contextlib.suppress(BridgeCommandError):
                api.close(handle_abierto)
    if fallos:
        detalle_total = "; ".join(f"pid {pid} (PPID {padres[pid]}): {motivo}" for pid, motivo in sorted(fallos.items()))
        if vivos_fuera:
            raise BridgeTerminationError(
                "no se pudo contener a los descendientes del worker "
                f"(quedan vivos fuera del Job: {sorted(vivos_fuera)}): {detalle_total}"
            )
        raise BridgeCommandError(f"no se pudo contener a los descendientes del worker: {detalle_total}")
    return adoptados


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
        job_empty_timeout_s: float = _JOB_EMPTY_TIMEOUT_S,
        job_empty_poll_s: float = _JOB_EMPTY_POLL_S,
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
        self._job_empty_timeout_s = job_empty_timeout_s
        self._job_empty_poll_s = job_empty_poll_s
        self._jobs: dict[str, _RunningJob] = {}
        self._lock = threading.Lock()

    def launch(self, message: Mapping[str, object]) -> None:
        t0 = time.perf_counter()
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
        t_job = time.perf_counter()
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
            t_start = time.perf_counter()
            if type(handle) is not int or handle in (0, -1):
                raise BridgeCommandError("MO2 no devolvió un HANDLE válido para el worker")
            job_object.assign(handle)
            t_assign = time.perf_counter()
            # El handle es el del proceso que lanzó MO2 (en onefile, el bootloader): su hijo puede haber nacido antes
            # de la asignación y quedar fuera. Se contiene a todo el árbol ANTES de confirmar nada al broker.
            adopted = job_object.adopt_descendants(handle)
            t_adopt = time.perf_counter()
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
        t_ack = time.perf_counter()
        if self._log_info is not None:
            # La telemetría nunca altera el flujo del protocolo: si ``log_info`` falla, el monitor igual arranca.
            with contextlib.suppress(Exception):
                ts_utc = datetime.now(UTC).isoformat()
                self._log_info(
                    f"Sky-Claw VFS Bridge launch timings ts={ts_utc} job_id={request.job_id} "
                    f"job_object={request.job_object} "
                    f"create_job_ms={(t_job - t0) * 1000.0:.3f} startApplication_ms={(t_start - t_job) * 1000.0:.3f} "
                    f"assign_ms={(t_assign - t_start) * 1000.0:.3f} adopt_ms={(t_adopt - t_assign) * 1000.0:.3f} "
                    f"adopted={adopted} total_to_ack_ms={(t_ack - t0) * 1000.0:.3f}"
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
        t_wait0 = time.perf_counter()
        try:
            wait_ok, exit_code = self._process_waiter(running.process_handle)
        except Exception as exc:
            wait_error = f"espera Win32 del worker falló: {exc}"
        finally:
            t_exit = time.perf_counter()
            # Que el proceso lanzado salga NO es que el worker terminó: el broker toma ``worker_exit`` como
            # terminalidad confirmada (y con él libera la cuarentena). Solo se emite con el Job VACÍO confirmado.
            survivors = self._terminate_and_confirm_empty(running.job_object)
            with self._lock:
                self._jobs.pop(running.request.job_id, None)
        if self._log_info is not None:
            # El evento que sigue (``worker_exit`` o el error de terminación) es lo que el broker espera para decidir:
            # el log no puede impedir ninguno de los dos.
            with contextlib.suppress(Exception):
                ts_utc = datetime.now(UTC).isoformat()
                self._log_info(
                    f"Sky-Claw VFS Bridge worker_exit confirmed ts={ts_utc} job_id={running.request.job_id} "
                    f"wait_ok={wait_ok} exit_code={exit_code} wait_ms={(t_exit - t_wait0) * 1000.0:.3f} "
                    f"job_empty={survivors is None}"
                )
        if survivors is not None:
            self._send_event(
                bridge_error_event(
                    command="terminate",
                    job_id=running.request.job_id,
                    exc=BridgeTerminationError(survivors),
                    kind="termination_failed",
                )
            )
            return
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

    def _terminate_and_confirm_empty(self, job_object: JobObject) -> str | None:
        """Confirma que el Job quedó VACÍO (terminándolo si hace falta) y lo cierra. ``None`` = vacío; texto = por qué no.

        Si el Job ya está vacío no se termina nada (ciclo de vida de siempre: salida natural → cerrar). Si conserva
        procesos —o no se puede consultar— se intenta terminarlo y se vuelve a confirmar. Lo que decide es lo que sigue
        vivo: que ``TerminateJobObject`` falle no importa si el Job ya está vacío. El handle se cierra siempre
        (``KILL_ON_JOB_CLOSE`` es la última red).
        """
        problems: list[str] = []
        active: int | None
        try:
            active = job_object.active_processes()
        except Exception as exc:  # sin poder consultar no se puede afirmar que no queda nada
            active = None
            problems.append(f"no se pudo consultar el Job: {exc}")
        if active != 0:
            try:
                job_object.terminate()
            except Exception as exc:  # se intenta confirmar igual
                problems.append(f"terminate: {exc}")
            try:
                active = self._wait_for_empty_job(job_object)
            except Exception as exc:
                active = None
                problems.append(f"no se pudo consultar el Job tras terminarlo: {exc}")
        try:
            job_object.close()
        except Exception as exc:
            problems.append(f"close: {exc}")
        if active == 0:
            return None
        return f"el Job no quedó vacío (activos={active}): " + "; ".join(problems or ["sin más detalle"])

    def _wait_for_empty_job(self, job_object: JobObject) -> int:
        deadline = time.monotonic() + self._job_empty_timeout_s
        while True:
            active = job_object.active_processes()
            if active == 0 or time.monotonic() >= deadline:
                return active
            time.sleep(self._job_empty_poll_s)

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

    class _BasicAccountingInformation(ctypes.Structure):
        """``JOBOBJECT_BASIC_ACCOUNTING_INFORMATION`` (misma disposición que usa el worker en ``vfs_job_identity``)."""

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

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class _SystemProcessInformation(ctypes.Structure):
        """Prefijo de ``SYSTEM_PROCESS_INFORMATION`` (``NtQuerySystemInformation``, clase 5) hasta el PPID.

        ctypes alinea cada campo como el compilador de Windows, así que estas mismas definiciones valen en 32 y 64 bits
        (``UNICODE_STRING`` mide 8 o 16 bytes según el puntero). Un test del kernel real compara ``CreateTime`` contra
        ``GetProcessTimes``: si la disposición estuviera mal, no coincidirían.
        """

        _fields_ = [
            ("NextEntryOffset", wintypes.ULONG),
            ("NumberOfThreads", wintypes.ULONG),
            ("WorkingSetPrivateSize", ctypes.c_longlong),
            ("HardFaultCount", wintypes.ULONG),
            ("NumberOfThreadsHighWatermark", wintypes.ULONG),
            ("CycleTime", ctypes.c_ulonglong),
            ("CreateTime", ctypes.c_longlong),
            ("UserTime", ctypes.c_longlong),
            ("KernelTime", ctypes.c_longlong),
            ("ImageNameLength", wintypes.USHORT),
            ("ImageNameMaximumLength", wintypes.USHORT),
            ("ImageNameBuffer", ctypes.c_void_p),
            ("BasePriority", wintypes.LONG),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ]

    class _Win32AdoptionApi:
        """``kernel32`` real para :func:`adopt_descendants`. Cada fallo es un ``BridgeCommandError`` con su ``GetLastError``.

        El plugin corre dentro de MO2 y no tiene ``psutil``: el árbol de procesos se lee con ToolHelp32.
        """

        _PROCESS_TERMINATE = 0x0001
        _PROCESS_SET_QUOTA = 0x0100  # AssignProcessToJobObject lo exige sobre el proceso
        _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        _SYNCHRONIZE = 0x00100000
        _TH32CS_SNAPPROCESS = 0x00000002
        _ERROR_INVALID_PARAMETER = 87
        _ERROR_NO_MORE_FILES = 18
        _WAIT_OBJECT_0 = 0
        _WAIT_TIMEOUT = 0x102
        _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        _SYSTEM_PROCESS_INFORMATION = 5  # clase de NtQuerySystemInformation
        _STATUS_BUFFER_CORTO = frozenset({0xC0000004, 0xC0000023})  # INFO_LENGTH_MISMATCH, BUFFER_TOO_SMALL
        _SYSTEM_PROCESS_LIST_INITIAL_BYTES = 1 << 20
        _SYSTEM_PROCESS_LIST_SLACK_BYTES = 1 << 16
        _SYSTEM_PROCESS_LIST_ATTEMPTS = 5

        def __init__(self, kernel32: ctypes.WinDLL, job_handle: int) -> None:
            k32 = kernel32
            k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
            k32.Process32FirstW.restype = wintypes.BOOL
            k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
            k32.Process32NextW.restype = wintypes.BOOL
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.GetProcessId.argtypes = [wintypes.HANDLE]
            k32.GetProcessId.restype = wintypes.DWORD
            k32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            k32.GetProcessTimes.restype = wintypes.BOOL
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
            k32.IsProcessInJob.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.TerminateProcess.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            self._k32 = k32
            self._job = job_handle

        @staticmethod
        def _error(call: str) -> BridgeCommandError:
            return BridgeCommandError(f"{call} falló (GetLastError={ctypes.get_last_error()})")

        def processes(self) -> list[tuple[int, int]]:
            snapshot = self._k32.CreateToolhelp32Snapshot(self._TH32CS_SNAPPROCESS, 0)
            if not snapshot or snapshot == self._INVALID_HANDLE_VALUE:
                raise self._error("CreateToolhelp32Snapshot")
            try:
                entry = _ProcessEntry32W()
                entry.dwSize = ctypes.sizeof(entry)
                found: list[tuple[int, int]] = []
                ok = self._k32.Process32FirstW(snapshot, ctypes.byref(entry))
                while ok:
                    found.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                    ok = self._k32.Process32NextW(snapshot, ctypes.byref(entry))
                if ctypes.get_last_error() != self._ERROR_NO_MORE_FILES:
                    raise self._error("Process32NextW")
                return found
            finally:
                self._k32.CloseHandle(snapshot)

        def process_id(self, handle: int) -> int:
            pid = int(self._k32.GetProcessId(handle))
            if pid == 0:
                raise self._error("GetProcessId")
            return pid

        def open_process(self, pid: int) -> int | None:
            rights = (
                self._PROCESS_TERMINATE
                | self._PROCESS_SET_QUOTA
                | self._PROCESS_QUERY_LIMITED_INFORMATION
                | self._SYNCHRONIZE
            )
            handle = self._k32.OpenProcess(rights, False, pid)
            if not handle:
                if ctypes.get_last_error() == self._ERROR_INVALID_PARAMETER:
                    return None  # el proceso ya no existe
                raise self._error(f"OpenProcess({pid})")
            return int(handle)

        def creation_time(self, handle: int) -> int:
            creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not self._k32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ):
                raise self._error("GetProcessTimes")
            return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)

        def creation_time_by_pid(self, pid: int) -> int | None:
            """Creación (``FILETIME``, el mismo reloj que ``creation_time``) leída de la lista de procesos del sistema.

            ``NtQuerySystemInformation(SystemProcessInformation)`` no abre ningún proceso, así que contesta también por los
            que ``OpenProcess`` rechaza (``csrss``, ``wininit``...). Es la lectura de reserva que usa ``psutil`` cuando
            ``GetProcessTimes`` no tiene acceso.
            """
            try:
                ntdll = ctypes.WinDLL("ntdll")
            except OSError as exc:
                raise BridgeCommandError(f"no se pudo cargar ntdll: {exc}") from exc
            consulta = ntdll.NtQuerySystemInformation
            consulta.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
            consulta.restype = wintypes.LONG  # NTSTATUS
            tamano = self._SYSTEM_PROCESS_LIST_INITIAL_BYTES
            for _ in range(self._SYSTEM_PROCESS_LIST_ATTEMPTS):
                buffer = ctypes.create_string_buffer(tamano)
                necesarios = wintypes.ULONG(0)
                estado = int(consulta(self._SYSTEM_PROCESS_INFORMATION, buffer, tamano, ctypes.byref(necesarios)))
                if estado >= 0:
                    return self._creacion_en_la_lista(buffer, tamano, pid)
                if (estado & 0xFFFFFFFF) not in self._STATUS_BUFFER_CORTO:
                    raise BridgeCommandError(f"NtQuerySystemInformation falló (NTSTATUS=0x{estado & 0xFFFFFFFF:08X})")
                # La lista creció entre la medición y la lectura (nacen procesos): se reintenta con margen.
                tamano = max(int(necesarios.value), tamano) + self._SYSTEM_PROCESS_LIST_SLACK_BYTES
            raise BridgeCommandError("NtQuerySystemInformation: la lista de procesos siguió creciendo en cada intento")

        @staticmethod
        def _creacion_en_la_lista(buffer: ctypes.Array[ctypes.c_char], tamano: int, pid: int) -> int | None:
            entrada = ctypes.sizeof(_SystemProcessInformation)
            desplazamiento = 0
            while True:
                if desplazamiento + entrada > tamano:
                    raise BridgeCommandError("NtQuerySystemInformation devolvió una lista de procesos truncada")
                proceso = _SystemProcessInformation.from_buffer(buffer, desplazamiento)
                if int(proceso.UniqueProcessId or 0) == pid:
                    return int(proceso.CreateTime)
                siguiente = int(proceso.NextEntryOffset)
                if siguiente == 0:
                    return None  # recorrida completa: el proceso ya no figura
                if siguiente < entrada:
                    raise BridgeCommandError("NtQuerySystemInformation devolvió una entrada de proceso inválida")
                desplazamiento += siguiente

        def has_exited(self, handle: int) -> bool:
            result = self._k32.WaitForSingleObject(handle, 0)
            if result == self._WAIT_OBJECT_0:
                return True
            if result == self._WAIT_TIMEOUT:
                return False
            raise self._error("WaitForSingleObject")

        def in_job(self, handle: int) -> bool:
            result = wintypes.BOOL(0)
            # Contra el handle del Job PROPIO: con NULL respondería por CUALQUIER Job (un test lo prohíbe en producción).
            if not self._k32.IsProcessInJob(handle, self._job, ctypes.byref(result)):
                raise self._error("IsProcessInJob")
            return bool(result.value)

        def assign(self, handle: int) -> None:
            if not self._k32.AssignProcessToJobObject(self._job, handle):
                raise self._error("AssignProcessToJobObject")

        def terminate(self, handle: int) -> None:
            if not self._k32.TerminateProcess(handle, 1):
                raise self._error("TerminateProcess")

        def close(self, handle: int) -> None:
            if not self._k32.CloseHandle(handle):
                raise self._error("CloseHandle")


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
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
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
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
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

    def adopt_descendants(self, process_handle: int) -> int:
        if sys.platform != "win32":
            raise BridgeCommandError("Win32 Job Objects solo están disponibles en Windows")
        if self._handle is None:
            raise BridgeCommandError("Job Object ya cerrado")
        return adopt_descendants(_Win32AdoptionApi(self._kernel32, self._handle), process_handle)

    def terminate(self) -> None:
        if self._handle is not None and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise BridgeTerminationError(f"TerminateJobObject falló: {ctypes.get_last_error()}")

    def active_processes(self) -> int:
        if self._handle is None:
            raise BridgeCommandError("Job Object ya cerrado")
        info = _BasicAccountingInformation()
        returned = wintypes.DWORD(0)
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        ):
            raise BridgeCommandError(
                f"QueryInformationJobObject(BasicAccountingInformation) falló: {ctypes.get_last_error()}"
            )
        return int(info.ActiveProcesses)

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
