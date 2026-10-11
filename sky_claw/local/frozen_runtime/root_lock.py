"""Primitiva de exclusión interproceso e intraproceso por FrozenRuntimeRoot (P4-S1).

ADR 0012 §34.3 es la autoridad normativa:
- LOCK_RESOURCE = FrozenRuntimeRoot (exclusión única por root, no por destino).
- LOCK_IDENTITY = canonical root (normcase abspath) + session_id (UUID por adquisición).
- LOCK_SCOPE = exclusión de mutaciones sobre autoridad del runtime.
- LOCK_EXCLUSION_SET = { PROMOTION, ROLLBACK, activation/rebinding,
  startup reconciliation, publish Generation, candidate build que escriba state/ }.
- LOCK_BUSY = fail-closed con timeout acotado; nunca espera infinita.
- LOCK_OWNER = { session_id, pid, process_create_time, acquired_at }.
- LOCK_LIVENESS = pid + process_create_time (nunca sólo PID ni os.kill(pid, 0)).
- LOCK_CRASH = el archivo de lock NO se borra al morir el proceso; persiste como
  evidencia forense de ownership para recuperación/clasificación.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import math
import os
import pathlib
import sys
import threading
import time
import uuid
from enum import StrEnum
from typing import Any

import psutil

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeLockAdmissionError,
    FrozenRuntimeLockBusyError,
    FrozenRuntimeLockIndeterminateError,
    FrozenRuntimeLockIndeterminateOwnershipError,
    FrozenRuntimeLockMetadataError,
    FrozenRuntimeLockOrphanedError,
    FrozenRuntimeLockOwnershipError,
)
from sky_claw.local.frozen_runtime.storage import admitir_storage_root

LOCK_FILE_NAME = "frozen-runtime.lock"
LOCK_SCHEMA_VERSION = 1

# Campos mínimos normativos de LOCK_OWNER (§34.3) más fase del ciclo de vida
LOCK_OWNER_FIELDS = frozenset(
    {
        "schema_version",
        "canonical_root",
        "session_id",
        "pid",
        "process_create_time",
        "acquired_at",
        "phase",
    }
)
LOCK_METADATA_ALLOWED_FIELDS = frozenset(LOCK_OWNER_FIELDS | {"released_at"})
LOCK_METADATA_FIELDS = LOCK_OWNER_FIELDS

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_ACTIVE_HANDLES: dict[str, FrozenRuntimeRootLockHandle] = {}
_THREAD_LOCKS_GUARD = threading.Lock()

# Offset virtual en Windows para exclusión interproceso sin bloquear lecturas de metadata.
_LOCK_BYTE_OFFSET = 1_000_000


if sys.platform == "win32":
    import msvcrt

    def _lock_fd(fd: int) -> bool:
        try:
            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _try_lock_probe_fd(fd: int) -> bool | None:
        try:
            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            # En Windows msvcrt.locking eleva EACCES (13) o EDEADLK (36) si la región ya está bloqueada
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            return None

    def _unlock_fd(fd: int) -> None:
        with contextlib.suppress(OSError):
            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_fd(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _try_lock_probe_fd(fd: int) -> bool | None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            # En POSIX flock eleva EWOULDBLOCK / EAGAIN o EACCES si el archivo ya está bloqueado
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            return None

    def _unlock_fd(fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def _after_fork_in_child() -> None:
    """Hook ejecutado en el proceso hijo tras os.fork() (F-19).

    Cierra los descriptores de archivo heredados sin llamar a flock(LOCK_UN)
    para no desbloquear el kernel lock del padre, pero garantizando que el hijo
    no retenga el flock si el padre muere posteriormente.
    Limpia los registries intraproceso para evitar estado zombie/desincronizado.
    """
    global _THREAD_LOCKS_GUARD, _THREAD_LOCKS, _ACTIVE_HANDLES
    for handle in list(_ACTIVE_HANDLES.values()):
        file_obj = getattr(handle, "_file_obj", None)
        if file_obj is not None and not getattr(file_obj, "closed", True):
            with contextlib.suppress(OSError):
                file_obj.close()
    _ACTIVE_HANDLES = {}
    _THREAD_LOCKS = {}
    _THREAD_LOCKS_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


class OSLockProbeResult(StrEnum):
    """Resultado tri-state del sondeo de exclusión del SO (§34.3)."""

    HELD = "HELD"
    FREE = "FREE"
    INDETERMINATE = "INDETERMINATE"


def _probe_os_lock_state(lock_path: pathlib.Path) -> OSLockProbeResult:
    """Comprueba causalmente si el mutex del SO está tomado sobre lock_path.

    Devuelve:
    - HELD: si el descriptor de prueba demostró exclusión activa (error específico
            EACCES/EWOULDBLOCK/EAGAIN al intentar bloquear).
    - FREE: si el descriptor de prueba pudo adquirir y liberar exitosamente el mutex del SO.
    - INDETERMINATE: si ocurrió un fallo de I/O, error de permisos en open() o error
                     inesperado que impide certificar el estado (fail-closed: UNKNOWN != OWNED).
    """
    try:
        probe_file = open(lock_path, "r+b")  # noqa: SIM115
    except OSError:
        # Fallo al abrir el archivo (permisos, I/O transitorio, sharing violation ajena)
        return OSLockProbeResult.INDETERMINATE

    try:
        locked_ok = _try_lock_probe_fd(probe_file.fileno())
        if locked_ok is None:
            # Error inesperado durante la operación de lock del SO
            return OSLockProbeResult.INDETERMINATE
        elif locked_ok:
            # Se pudo adquirir: el lock del SO no estaba retenido por nadie
            _unlock_fd(probe_file.fileno())
            return OSLockProbeResult.FREE
        else:
            # Falló específicamente porque la región/archivo ya está tomada exclusivamente
            return OSLockProbeResult.HELD
    finally:
        with contextlib.suppress(OSError):
            probe_file.close()


def _probe_os_lock_held(lock_path: pathlib.Path) -> bool:
    """Helper de conveniencia booleano (True sólo si HELD confirmado)."""
    return _probe_os_lock_state(lock_path) == OSLockProbeResult.HELD


class OwnerLiveness(StrEnum):
    """Estado de vitalidad del dueño registrado en la metadata."""

    ALIVE = "ALIVE"
    DEAD = "DEAD"
    INDETERMINATE = "INDETERMINATE"


class LockPhase(StrEnum):
    """Fase del ciclo de vida del lock persistida en disco (crash-safe)."""

    HELD = "HELD"
    RELEASED = "RELEASED"


class LockDisposition(StrEnum):
    """Clasificación del estado observado del lock de un root."""

    FREE = "FREE"
    ACQUIRING = "ACQUIRING"
    HELD_BY_LIVE_OWNER = "HELD_BY_LIVE_OWNER"
    ORPHANED = "ORPHANED"
    INDETERMINATE = "INDETERMINATE"
    CORRUPT_METADATA = "CORRUPT_METADATA"


@dataclasses.dataclass(frozen=True, slots=True)
class RootLockMetadata:
    """Metadata de ownership persistida en el lockfile (§34.3 + crash forensics)."""

    schema_version: int
    canonical_root: str
    session_id: str
    pid: int
    process_create_time: float
    acquired_at: float
    phase: LockPhase
    released_at: float | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class LockInspectionResult:
    """Resultado de la inspección no mutante de un root lock."""

    canonical_root: str
    lock_path: pathlib.Path
    disposition: LockDisposition
    is_os_locked: bool
    metadata: RootLockMetadata | None
    liveness: OwnerLiveness | None


def _get_thread_lock(canonical_root: str) -> threading.Lock:
    """Devuelve o crea el threading.Lock asociado al root canónico."""
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(canonical_root)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[canonical_root] = lock
        return lock


def _check_process_liveness(pid: int, expected_create_time: float) -> OwnerLiveness:
    """Verifica si el proceso dueño sigue vivo comparando PID y process_create_time."""
    if pid <= 0:
        return OwnerLiveness.DEAD
    try:
        proc = psutil.Process(pid)
        actual_create_time = proc.create_time()
        if not proc.is_running():
            return OwnerLiveness.DEAD
        if proc.status() == psutil.STATUS_ZOMBIE:
            return OwnerLiveness.DEAD
    except psutil.NoSuchProcess:
        return OwnerLiveness.DEAD
    except (psutil.AccessDenied, psutil.Error, OSError, ValueError):
        return OwnerLiveness.INDETERMINATE

    if abs(actual_create_time - expected_create_time) > 1e-4:
        # PID reutilizado por otro proceso del SO: el dueño original murió
        return OwnerLiveness.DEAD
    return OwnerLiveness.ALIVE


def canonical_root_key(root: pathlib.Path | str) -> tuple[str, pathlib.Path]:
    """Obtiene la clave canónica normalizada y la ruta esperada del lockfile."""
    p = pathlib.Path(root)
    p_abs = pathlib.Path(os.path.abspath(os.fspath(p)))
    canonical_str = os.path.normcase(os.path.abspath(os.fspath(p_abs)))
    canonical_root_path = pathlib.Path(canonical_str)
    lock_path = canonical_root_path / "state" / LOCK_FILE_NAME
    return canonical_str, lock_path


def _validate_root_and_state_admission_readonly(canonical_root_path: pathlib.Path) -> pathlib.Path:
    """Valida que el root y state/ cumplan los invariantes de seguridad de storage de forma no mutante."""
    res = admitir_storage_root(canonical_root_path)
    if res.state != res.state.ADMITTED:
        raise FrozenRuntimeLockAdmissionError(f"Root de storage rechazado: {res.message}")

    state_dir = canonical_root_path / "state"
    try:
        kind = link_kind_or_raise(state_dir)
    except OSError as exc:
        raise FrozenRuntimeLockAdmissionError(f"No se pudo inspeccionar state/: {exc}") from exc
    if kind is not None:
        raise FrozenRuntimeLockAdmissionError(
            f"state/ es un enlace/reparse ({kind}): namespace redirigido (fail-closed)"
        )

    if state_dir.exists() and not state_dir.is_dir():
        raise FrozenRuntimeLockAdmissionError(f"state/ no es un directorio: '{state_dir}'")

    lock_path = state_dir / LOCK_FILE_NAME
    try:
        lock_kind = link_kind_or_raise(lock_path)
    except OSError as exc:
        raise FrozenRuntimeLockAdmissionError(f"No se pudo inspeccionar lockfile: {exc}") from exc
    if lock_kind is not None:
        raise FrozenRuntimeLockAdmissionError(f"El lockfile es un enlace/reparse ({lock_kind}): fail-closed")

    return lock_path


def _validate_root_and_state_admission(canonical_root_path: pathlib.Path) -> pathlib.Path:
    """Valida que el root y state/ cumplan los invariantes de seguridad de storage."""
    res = admitir_storage_root(canonical_root_path)
    if res.state != res.state.ADMITTED:
        raise FrozenRuntimeLockAdmissionError(f"Root de storage rechazado: {res.message}")

    state_dir = canonical_root_path / "state"
    try:
        kind = link_kind_or_raise(state_dir)
    except OSError as exc:
        raise FrozenRuntimeLockAdmissionError(f"No se pudo inspeccionar state/: {exc}") from exc
    if kind is not None:
        raise FrozenRuntimeLockAdmissionError(
            f"state/ es un enlace/reparse ({kind}): namespace redirigido (fail-closed)"
        )

    if state_dir.exists() and not state_dir.is_dir():
        raise FrozenRuntimeLockAdmissionError(f"state/ no es un directorio: '{state_dir}'")
    if not state_dir.exists():
        state_dir.mkdir(parents=True, exist_ok=True)

    lock_path = state_dir / LOCK_FILE_NAME
    try:
        lock_kind = link_kind_or_raise(lock_path)
    except OSError as exc:
        raise FrozenRuntimeLockAdmissionError(f"No se pudo inspeccionar lockfile: {exc}") from exc
    if lock_kind is not None:
        raise FrozenRuntimeLockAdmissionError(f"El lockfile es un enlace/reparse ({lock_kind}): fail-closed")

    return lock_path


def _read_metadata_bytes(
    raw: bytes,
    expected_canonical_root: str | None = None,
) -> RootLockMetadata:
    """Parsea estrictamente la metadata del lock (esquema normativo cerrado con crash forensics)."""
    if not raw:
        raise FrozenRuntimeLockMetadataError("Metadata vacía")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FrozenRuntimeLockMetadataError(f"Metadata no es JSON UTF-8 válido: {exc}") from exc

    if not isinstance(payload, dict):
        raise FrozenRuntimeLockMetadataError("Metadata no es un objeto JSON")

    keys = set(payload.keys())
    if not LOCK_OWNER_FIELDS.issubset(keys) or not keys.issubset(LOCK_METADATA_ALLOWED_FIELDS):
        raise FrozenRuntimeLockMetadataError(
            f"Campos de metadata inválidos: {sorted(keys)}. Requeridos: {sorted(LOCK_OWNER_FIELDS)}"
        )

    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != LOCK_SCHEMA_VERSION:
        raise FrozenRuntimeLockMetadataError(f"schema_version inválido: {schema_version}")

    canonical_root = payload["canonical_root"]
    if isinstance(canonical_root, bool) or not isinstance(canonical_root, str) or not canonical_root:
        raise FrozenRuntimeLockMetadataError("canonical_root debe ser un string no vacío")

    if expected_canonical_root is not None and canonical_root != expected_canonical_root:
        raise FrozenRuntimeLockMetadataError(
            f"canonical_root no coincide con el root esperado ({canonical_root} != {expected_canonical_root})"
        )

    session_str = payload["session_id"]
    if isinstance(session_str, bool) or not isinstance(session_str, str):
        raise FrozenRuntimeLockMetadataError("session_id debe ser un string")
    try:
        parsed_uuid = uuid.UUID(session_str)
        if str(parsed_uuid) != session_str:
            raise FrozenRuntimeLockMetadataError("session_id debe ser un UUID canónico en minúsculas")
    except ValueError as exc:
        raise FrozenRuntimeLockMetadataError(f"session_id no es un UUID válido: {exc}") from exc

    pid = payload["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise FrozenRuntimeLockMetadataError(f"pid inválido: {pid}")

    create_time = payload["process_create_time"]
    if (
        isinstance(create_time, bool)
        or not isinstance(create_time, (int, float))
        or not math.isfinite(create_time)
        or create_time <= 0
    ):
        raise FrozenRuntimeLockMetadataError(f"process_create_time inválido: {create_time}")

    acquired_at = payload["acquired_at"]
    if (
        isinstance(acquired_at, bool)
        or not isinstance(acquired_at, (int, float))
        or not math.isfinite(acquired_at)
        or acquired_at <= 0
    ):
        raise FrozenRuntimeLockMetadataError(f"acquired_at inválido: {acquired_at}")

    raw_phase = payload["phase"]
    if raw_phase not in (LockPhase.HELD.value, LockPhase.RELEASED.value):
        raise FrozenRuntimeLockMetadataError(f"phase inválido: {raw_phase}")
    phase = LockPhase(raw_phase)

    if phase == LockPhase.HELD:
        if "released_at" in payload:
            raise FrozenRuntimeLockMetadataError("released_at no debe estar presente cuando phase es HELD")
        released_at_float: float | None = None
    else:
        # phase == LockPhase.RELEASED
        if "released_at" not in payload:
            raise FrozenRuntimeLockMetadataError("released_at es obligatorio cuando phase es RELEASED")
        released_at = payload["released_at"]
        if (
            isinstance(released_at, bool)
            or not isinstance(released_at, (int, float))
            or not math.isfinite(released_at)
            or released_at <= 0
        ):
            raise FrozenRuntimeLockMetadataError(f"released_at inválido: {released_at}")
        released_at_float = float(released_at)
        if released_at_float < float(acquired_at):
            raise FrozenRuntimeLockMetadataError(
                f"released_at ({released_at_float}) no puede ser anterior a acquired_at ({acquired_at})"
            )

    return RootLockMetadata(
        schema_version=int(schema_version),
        canonical_root=canonical_root,
        session_id=session_str,
        pid=int(pid),
        process_create_time=float(create_time),
        acquired_at=float(acquired_at),
        phase=phase,
        released_at=released_at_float,
    )


def _write_metadata_crash_safe(file_obj: Any, payload: dict[str, Any]) -> None:
    """Escribe metadata de forma crash-safe sin perder evidencia forense (F-18/F-20).

    Escribe primero el nuevo payload rellenado con espacios ASCII hasta el tamaño
    previo del archivo (garantizando JSON sintácticamente válido en todo momento).
    Tras el fsync de la nueva representación, trunca al tamaño exacto si era menor.
    """
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    file_obj.seek(0, os.SEEK_END)
    old_size = file_obj.tell()

    durable_payload = serialized + (b" " * (old_size - len(serialized))) if old_size > len(serialized) else serialized

    file_obj.seek(0)
    file_obj.write(durable_payload)
    file_obj.flush()
    os.fsync(file_obj.fileno())

    if len(durable_payload) > len(serialized):
        file_obj.truncate(len(serialized))
        file_obj.flush()
        os.fsync(file_obj.fileno())


def _is_current_process_owner(handle: FrozenRuntimeRootLockHandle) -> bool:
    """Verifica si el proceso actual es el dueño del handle (PID + process_create_time)."""
    current_pid = os.getpid()
    if current_pid != handle.pid:
        return False
    try:
        current_create_time = psutil.Process(current_pid).create_time()
        return bool(abs(float(current_create_time) - float(handle.process_create_time)) <= 1e-4)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error, OSError, ValueError):
        return False


def _is_same_process_stale_record(
    metadata: RootLockMetadata,
    canonical_root: str,
    current_pid: int,
    current_process_create_time: float,
) -> bool:
    """Decide si un registro HELD en disco es residuo del PROPIO proceso (F-22).

    Un ``release()`` que falla a mitad de camino (validación de ownership o persistencia)
    deja en disco el registro ``phase=HELD`` con la identidad del proceso dueño, pero
    libera TODOS los recursos locales: handle activo, mutex del SO, descriptor y thread
    lock (F-16). Sin esta comprobación el proceso se bloquearía a sí mismo hasta
    reiniciarse, porque la liveness de su propio PID es ``ALIVE`` por definición.

    Esta función NO es una autoridad global: sólo es una prueba concluyente dentro de
    ``acquire()``, donde el llamador YA posee el thread lock per-root y el mutex del SO.
    Todo handle legítimo local conserva su thread lock mientras vive, así que "poseo el
    thread lock + no hay handle activo para este root" demuestra que ningún dueño local
    puede seguir siéndolo. ``inspect_frozen_runtime_root_lock()`` no tiene esa prueba y
    por eso clasifica el mismo estado como ``INDETERMINATE``, nunca como ``FREE``.

    Un dueño VIVO de OTRO proceso nunca satisface la identidad de proceso y sigue siendo
    BUSY: esto no relaja el fail-closed interproceso. La reutilización de PID tampoco
    alcanza, porque ``process_create_time`` debe coincidir además del ``pid``.
    """
    if metadata.phase != LockPhase.HELD:
        return False
    if metadata.canonical_root != canonical_root:
        return False
    if metadata.pid != current_pid:
        return False
    if abs(metadata.process_create_time - current_process_create_time) > 1e-4:
        return False
    with _THREAD_LOCKS_GUARD:
        return _ACTIVE_HANDLES.get(canonical_root) is None


def _validate_handle_ownership(handle: FrozenRuntimeRootLockHandle) -> RootLockMetadata:
    """Valida identidad completa de ownership del handle (memoria, SO/proceso y disco)."""
    # F-07: Guard de frontera de proceso (anti-fork / anti-PID mismatch)
    current_pid = os.getpid()
    if current_pid != handle.pid:
        raise FrozenRuntimeLockOwnershipError(
            f"Frontera de proceso violada: el handle pertenece al PID {handle.pid}, "
            f"pero el proceso actual es PID {current_pid}."
        )

    try:
        current_create_time = psutil.Process(current_pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error, OSError) as exc:
        raise FrozenRuntimeLockOwnershipError(
            f"No se pudo determinar el process_create_time del proceso actual: {exc}"
        ) from exc

    if abs(current_create_time - handle.process_create_time) > 1e-4:
        raise FrozenRuntimeLockOwnershipError(
            f"process_create_time del proceso actual no coincide con el handle "
            f"({current_create_time} != {handle.process_create_time})."
        )

    if handle._released:
        raise FrozenRuntimeLockOwnershipError("El handle del lock ya fue liberado.")

    if handle._file_obj is None or handle._file_obj.closed:
        raise FrozenRuntimeLockOwnershipError("El descriptor de archivo del lock fue cerrado.")

    with _THREAD_LOCKS_GUARD:
        active = _ACTIVE_HANDLES.get(handle.canonical_root)
        if active is not handle:
            raise FrozenRuntimeLockOwnershipError("Este handle no es el dueño activo registrado en memoria.")

    # F-09: Validar metadata completa en disco contra identidad del handle
    if not handle.lock_path.exists():
        raise FrozenRuntimeLockOwnershipError("El archivo de lock no existe en disco.")

    try:
        raw_disk = handle.lock_path.read_bytes()
        meta = _read_metadata_bytes(raw_disk, expected_canonical_root=handle.canonical_root)
    except (FrozenRuntimeLockMetadataError, OSError) as exc:
        raise FrozenRuntimeLockOwnershipError(f"La metadata del lock en disco no es válida: {exc}") from exc

    if meta.session_id != handle.session_id:
        raise FrozenRuntimeLockOwnershipError(
            f"La metadata en disco pertenece a otra sesión ({meta.session_id} != {handle.session_id})"
        )
    if meta.pid != handle.pid:
        raise FrozenRuntimeLockOwnershipError(f"pid en disco no coincide con el handle ({meta.pid} != {handle.pid})")
    if meta.canonical_root != handle.canonical_root:
        raise FrozenRuntimeLockOwnershipError(
            f"canonical_root en disco no coincide con el handle ({meta.canonical_root} != {handle.canonical_root})"
        )

    if abs(meta.process_create_time - handle.process_create_time) > 1e-4:
        raise FrozenRuntimeLockOwnershipError(
            f"process_create_time en disco no coincide ({meta.process_create_time} != {handle.process_create_time})"
        )
    if abs(meta.acquired_at - handle.acquired_at) > 1e-4:
        raise FrozenRuntimeLockOwnershipError(
            f"acquired_at en disco no coincide ({meta.acquired_at} != {handle.acquired_at})"
        )
    if meta.phase != LockPhase.HELD:
        raise FrozenRuntimeLockOwnershipError(f"phase en disco no es HELD ({meta.phase})")

    # F-13 / F-14: Comprobar causalmente que el mutex del SO sigue efectivamente retenido
    probe_state = _probe_os_lock_state(handle.lock_path)
    if probe_state == OSLockProbeResult.FREE:
        raise FrozenRuntimeLockOwnershipError(
            f"El mutex del sistema operativo para {handle.root} no está retenido "
            "(exclusión del SO perdida: el lock está libre)."
        )
    if probe_state == OSLockProbeResult.INDETERMINATE:
        raise FrozenRuntimeLockIndeterminateOwnershipError(
            f"No se pudo determinar el estado del mutex del sistema operativo para {handle.root} "
            "(fail-closed: UNKNOWN != OWNED)."
        )

    return meta


class FrozenRuntimeRootLockHandle:
    """Handle de un lock adquirido por este proceso para un FrozenRuntimeRoot."""

    def __init__(
        self,
        *,
        root: pathlib.Path,
        canonical_root: str,
        lock_path: pathlib.Path,
        session_id: str,
        pid: int,
        process_create_time: float,
        acquired_at: float,
        phase: LockPhase = LockPhase.HELD,
        file_obj: Any = None,
        thread_lock: threading.Lock | None = None,
    ) -> None:
        self.root = root
        self.canonical_root = canonical_root
        self.lock_path = lock_path
        self.session_id = session_id
        self.pid = pid
        self.process_create_time = process_create_time
        self.acquired_at = acquired_at
        self.phase = phase
        self._file_obj = file_obj
        self._thread_lock = thread_lock
        self._released = False

    def assert_owned(self) -> None:
        """Verifica que este handle sigue siendo dueño legítimo del lock."""
        _validate_handle_ownership(self)

    def release(self) -> None:
        """Libera la exclusión del lock de forma crash-safe y poison-safe (F-16)."""
        if not _is_current_process_owner(self):
            # En un proceso hijo (fork) o ajeno, no tocar descriptores ni locks del dueño legítimo (F-16/F-07)
            _validate_handle_ownership(self)
            return

        if self._released:
            return

        try:
            # Validar ownership estricto antes de mutar disco (F-07, F-09, RL-18, F-03)
            _validate_handle_ownership(self)

            # F-08 / F-10: Persistir phase=RELEASED con released_at >= acquired_at (commit point)
            now = time.time()
            released_at = max(now, self.acquired_at)
            payload = {
                "schema_version": LOCK_SCHEMA_VERSION,
                "canonical_root": self.canonical_root,
                "session_id": self.session_id,
                "pid": self.pid,
                "process_create_time": self.process_create_time,
                "acquired_at": self.acquired_at,
                "phase": LockPhase.RELEASED.value,
                "released_at": released_at,
            }
            _write_metadata_crash_safe(self._file_obj, payload)
            self.phase = LockPhase.RELEASED
        finally:
            # F-16: Limpiar recursos locales del dueño incluso si la validación o fsync fallan
            self._released = True
            with _THREAD_LOCKS_GUARD:
                if _ACTIVE_HANDLES.get(self.canonical_root) is self:
                    _ACTIVE_HANDLES.pop(self.canonical_root, None)

            if self._file_obj is not None and not self._file_obj.closed:
                try:
                    _unlock_fd(self._file_obj.fileno())
                finally:
                    with contextlib.suppress(OSError):
                        self._file_obj.close()

            if self._thread_lock is not None:
                with contextlib.suppress(RuntimeError):
                    self._thread_lock.release()

    def __enter__(self) -> FrozenRuntimeRootLockHandle:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


def inspect_frozen_runtime_root_lock(root: pathlib.Path | str) -> LockInspectionResult:
    """Inspecciona el estado de exclusión y metadata del lock de un root sin mutarlo.

    Asimetría deliberada con ``acquire_frozen_runtime_root_lock`` (F-22): un registro
    ``phase=HELD`` con la identidad del proceso actual y el mutex del SO libre se clasifica
    ``INDETERMINATE``, nunca ``FREE`` ni "stale reclamable". El inspector no posee el thread
    lock per-root, así que NO puede probar que no exista un handle vivo local que siga siendo
    dueño; declarar ``FREE`` sin esa prueba relajaría el fail-closed de un observador
    read-only. ``acquire()``, que sí posee el thread lock y el mutex, puede demostrarlo y
    reclama el residuo.
    """
    canonical_str, lock_path = canonical_root_key(root)
    root_path = pathlib.Path(canonical_str)
    _validate_root_and_state_admission_readonly(root_path)

    if not lock_path.exists():
        return LockInspectionResult(
            canonical_root=canonical_str,
            lock_path=lock_path,
            disposition=LockDisposition.FREE,
            is_os_locked=False,
            metadata=None,
            liveness=None,
        )

    # Probar si el OS lock está tomado SIEMPRE que el archivo exista (Finding 1, F-13, F-14)
    probe_state = _probe_os_lock_state(lock_path)
    is_os_locked = probe_state == OSLockProbeResult.HELD

    st_size = lock_path.stat().st_size
    if st_size == 0:
        if probe_state == OSLockProbeResult.INDETERMINATE:
            return LockInspectionResult(
                canonical_root=canonical_str,
                lock_path=lock_path,
                disposition=LockDisposition.INDETERMINATE,
                is_os_locked=False,
                metadata=None,
                liveness=None,
            )
        if is_os_locked:
            return LockInspectionResult(
                canonical_root=canonical_str,
                lock_path=lock_path,
                disposition=LockDisposition.ACQUIRING,
                is_os_locked=True,
                metadata=None,
                liveness=None,
            )
        return LockInspectionResult(
            canonical_root=canonical_str,
            lock_path=lock_path,
            disposition=LockDisposition.FREE,
            is_os_locked=False,
            metadata=None,
            liveness=None,
        )

    # El archivo existe y tiene tamaño > 0
    try:
        raw_bytes = lock_path.read_bytes()
        metadata = _read_metadata_bytes(raw_bytes, expected_canonical_root=canonical_str)
    except FrozenRuntimeLockMetadataError:
        return LockInspectionResult(
            canonical_root=canonical_str,
            lock_path=lock_path,
            disposition=LockDisposition.CORRUPT_METADATA,
            is_os_locked=is_os_locked,
            metadata=None,
            liveness=None,
        )

    liveness = _check_process_liveness(metadata.pid, metadata.process_create_time)

    # F-14: Si la sonda de mutex del SO fue indeterminada, clasificar INDETERMINATE (fail-closed)
    if probe_state == OSLockProbeResult.INDETERMINATE:
        disposition = LockDisposition.INDETERMINATE
    elif metadata.phase == LockPhase.RELEASED:
        disposition = LockDisposition.FREE if not is_os_locked else LockDisposition.ACQUIRING
    elif is_os_locked:
        if liveness == OwnerLiveness.ALIVE:
            disposition = LockDisposition.HELD_BY_LIVE_OWNER
        elif liveness == OwnerLiveness.INDETERMINATE:
            disposition = LockDisposition.INDETERMINATE
        else:
            disposition = LockDisposition.ORPHANED
    else:
        # OS lock liberado (proceso muerto sin release / crash)
        if liveness == OwnerLiveness.DEAD:
            disposition = LockDisposition.ORPHANED
        elif liveness == OwnerLiveness.INDETERMINATE:
            disposition = LockDisposition.INDETERMINATE
        else:
            # F-17: PID vivo pero sin OS mutex: estado inconsistente/indeterminado (fail-closed)
            disposition = LockDisposition.INDETERMINATE

    return LockInspectionResult(
        canonical_root=canonical_str,
        lock_path=lock_path,
        disposition=disposition,
        is_os_locked=is_os_locked,
        metadata=metadata,
        liveness=liveness,
    )


def acquire_frozen_runtime_root_lock(
    root: pathlib.Path | str,
    *,
    timeout: float = 0.0,
    reclaim_orphaned: bool = False,
) -> FrozenRuntimeRootLockHandle:
    """Adquiere el lock exclusivo interproceso e intraproceso para FrozenRuntimeRoot."""
    if timeout < 0:
        raise ValueError("timeout debe ser >= 0")

    canonical_str, lock_path = canonical_root_key(root)
    root_path = pathlib.Path(canonical_str)
    _validate_root_and_state_admission(root_path)

    thread_lock = _get_thread_lock(canonical_str)
    start_time = time.monotonic()
    deadline = start_time + timeout

    # 1. Lock a nivel de threads dentro del mismo proceso
    acquired_thread = thread_lock.acquire(blocking=(timeout > 0), timeout=timeout if timeout > 0 else -1)
    if not acquired_thread:
        raise FrozenRuntimeLockBusyError(f"El root '{root}' está bloqueado por otro hilo en este proceso.")

    f = None
    try:
        # Identidad del proceso actual: se necesita ANTES de clasificar el registro
        # preexistente (F-22: residuo HELD del propio proceso tras un release fallido).
        own_pid = os.getpid()
        own_create_time = psutil.Process(own_pid).create_time()

        # Asegurar descriptor abierto atómicamente para el lockfile exclusivo (x+b o r+b).
        # F-23: la carrera x+b -> FileExistsError -> r+b -> FileNotFoundError es transitoria
        # y debe reintentarse, pero ACOTADA por el MISMO deadline monotónico que el resto de
        # la adquisición. Sin esa cota el bucle podía superar cualquier timeout pactado
        # (incluido timeout=0) y esperar indefinidamente, violando LOCK_BUSY.
        while f is None:
            try:
                f = open(lock_path, "x+b")  # noqa: SIM115
            except FileExistsError:
                try:
                    f = open(lock_path, "r+b")  # noqa: SIM115
                except FileNotFoundError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # timeout=0: un único intento inmediato; si la carrera persiste, falla acotado.
                        raise FrozenRuntimeLockBusyError(
                            f"El root '{root}' está ocupado: la carrera de apertura del lockfile agotó el timeout."
                        ) from None
                    # Backoff acotado que nunca duerme más allá del deadline pactado.
                    time.sleep(min(0.05, remaining))

        # 2. Lock a nivel de SO con retry acotado si timeout > 0
        while True:
            if _lock_fd(f.fileno()):
                break
            now = time.monotonic()
            if now >= deadline:
                raise FrozenRuntimeLockBusyError(f"El root '{root}' está ocupado por otro proceso.")
            time.sleep(min(0.05, max(0.005, deadline - now)))

        # 3. OS lock adquirido: examinar metadata preexistente en el archivo
        f.seek(0, os.SEEK_END)
        tamano = f.tell()
        if tamano > 0:
            f.seek(0)
            raw_existente = f.read()
            try:
                meta_existente = _read_metadata_bytes(raw_existente, expected_canonical_root=canonical_str)
            except FrozenRuntimeLockMetadataError as exc:
                _unlock_fd(f.fileno())
                f.close()
                raise FrozenRuntimeLockMetadataError(f"Metadata corrupta en el lockfile preexistente: {exc}") from exc

            # Si el lock previo fue liberado limpiamente, está disponible para adquisición inmediata
            if meta_existente.phase == LockPhase.RELEASED:
                pass
            else:
                # meta_existente.phase == LockPhase.HELD
                # Verificar liveness del dueño previo
                liveness = _check_process_liveness(meta_existente.pid, meta_existente.process_create_time)
                # F-22: un registro HELD del PROPIO proceso sin handle activo local es el residuo
                # de un release() fallido y se reclama SIN exigir reclaim_orphaned: ya poseemos el
                # thread lock per-root y el mutex del SO, así que ningún dueño local puede seguir
                # siéndolo. Un dueño de OTRO proceso —o este mismo con handle activo (Finding 3b)—
                # sigue siendo BUSY, incluso con reclaim_orphaned=True.
                residuo_local = _is_same_process_stale_record(meta_existente, canonical_str, own_pid, own_create_time)
                if liveness == OwnerLiveness.ALIVE and not residuo_local:
                    _unlock_fd(f.fileno())
                    f.close()
                    raise FrozenRuntimeLockBusyError(
                        f"El root '{root}' registra un lock activo no liberado por un proceso vivo "
                        f"(PID {meta_existente.pid})."
                    )

                if liveness == OwnerLiveness.INDETERMINATE:
                    _unlock_fd(f.fileno())
                    f.close()
                    raise FrozenRuntimeLockIndeterminateError(
                        "No se pudo determinar si el dueño del lock previo sigue vivo (fail-closed)."
                    )

                if liveness == OwnerLiveness.DEAD and not reclaim_orphaned:
                    _unlock_fd(f.fileno())
                    f.close()
                    raise FrozenRuntimeLockOrphanedError(
                        f"El root '{root}' contiene un lock huérfano de un proceso muerto (pid {meta_existente.pid}). "
                        "Requiere reconciliación/reclaim explícito."
                    )

        # 4. Acuñar nueva sesión de lock
        session_id = str(uuid.uuid4())
        acquired_at = time.time()

        payload = {
            "schema_version": LOCK_SCHEMA_VERSION,
            "canonical_root": canonical_str,
            "session_id": session_id,
            "pid": own_pid,
            "process_create_time": own_create_time,
            "acquired_at": acquired_at,
            "phase": LockPhase.HELD.value,
        }
        _write_metadata_crash_safe(f, payload)

        handle = FrozenRuntimeRootLockHandle(
            root=root_path,
            canonical_root=canonical_str,
            lock_path=lock_path,
            session_id=session_id,
            pid=own_pid,
            process_create_time=own_create_time,
            acquired_at=acquired_at,
            phase=LockPhase.HELD,
            file_obj=f,
            thread_lock=thread_lock,
        )

        with _THREAD_LOCKS_GUARD:
            _ACTIVE_HANDLES[canonical_str] = handle

        return handle

    except BaseException:
        # Finding 5: Cleanup estructural ante BaseException (KeyboardInterrupt, SystemExit, etc.)
        if f is not None and not f.closed:
            _unlock_fd(f.fileno())
            with contextlib.suppress(OSError):
                f.close()
        thread_lock.release()
        raise
