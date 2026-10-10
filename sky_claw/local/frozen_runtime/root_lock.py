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
import json
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
    FrozenRuntimeLockMetadataError,
    FrozenRuntimeLockOrphanedError,
    FrozenRuntimeLockOwnershipError,
)
from sky_claw.local.frozen_runtime.storage import admitir_storage_root

LOCK_FILE_NAME = "frozen-runtime.lock"
LOCK_SCHEMA_VERSION = 1
LOCK_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "canonical_root",
        "session_id",
        "pid",
        "process_create_time",
        "acquired_at",
    }
)

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

    def _unlock_fd(fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


class OwnerLiveness(StrEnum):
    """Estado de vitalidad del dueño registrado en la metadata."""

    ALIVE = "ALIVE"
    DEAD = "DEAD"
    INDETERMINATE = "INDETERMINATE"


class LockDisposition(StrEnum):
    """Clasificación del estado observado del lock de un root."""

    FREE = "FREE"
    HELD_BY_LIVE_OWNER = "HELD_BY_LIVE_OWNER"
    ORPHANED = "ORPHANED"
    INDETERMINATE = "INDETERMINATE"
    CORRUPT_METADATA = "CORRUPT_METADATA"


@dataclasses.dataclass(frozen=True, slots=True)
class RootLockMetadata:
    """Metadata de ownership persistida en el lockfile.

    Esquema cerrado normativo con exactamente 6 campos (§34.3).
    """

    schema_version: int
    canonical_root: str
    session_id: str
    pid: int
    process_create_time: float
    acquired_at: float


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
        if sys.platform == "win32" and proc.status() == psutil.STATUS_ZOMBIE:
            return OwnerLiveness.DEAD
    except psutil.NoSuchProcess:
        return OwnerLiveness.DEAD
    except (psutil.AccessDenied, psutil.Error, OSError, ValueError):
        return OwnerLiveness.INDETERMINATE

    if abs(actual_create_time - expected_create_time) > 0.05:
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
    if lock_path.exists():
        try:
            lock_kind = link_kind_or_raise(lock_path)
        except OSError as exc:
            raise FrozenRuntimeLockAdmissionError(f"No se pudo inspeccionar lockfile: {exc}") from exc
        if lock_kind is not None:
            raise FrozenRuntimeLockAdmissionError(f"El lockfile es un enlace/reparse ({lock_kind}): fail-closed")

    return lock_path


def _read_metadata_bytes(raw: bytes) -> RootLockMetadata:
    """Parsea estrictamente la metadata del lock (esquema normativo cerrado)."""
    if not raw:
        raise FrozenRuntimeLockMetadataError("Metadata vacía")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FrozenRuntimeLockMetadataError(f"Metadata no es JSON UTF-8 válido: {exc}") from exc

    if not isinstance(payload, dict):
        raise FrozenRuntimeLockMetadataError("Metadata no es un objeto JSON")

    if set(payload.keys()) != LOCK_METADATA_FIELDS:
        raise FrozenRuntimeLockMetadataError(
            f"Metadata debe contener exactamente los campos normativos {sorted(LOCK_METADATA_FIELDS)}"
        )

    if payload["schema_version"] != LOCK_SCHEMA_VERSION:
        raise FrozenRuntimeLockMetadataError(f"schema_version desconocido: {payload['schema_version']}")

    session_str = payload["session_id"]
    if not isinstance(session_str, str):
        raise FrozenRuntimeLockMetadataError("session_id debe ser un string")
    try:
        parsed_uuid = uuid.UUID(session_str)
        if str(parsed_uuid) != session_str:
            raise FrozenRuntimeLockMetadataError("session_id debe ser un UUID canónico en minúsculas")
    except ValueError as exc:
        raise FrozenRuntimeLockMetadataError(f"session_id no es un UUID válido: {exc}") from exc

    pid = payload["pid"]
    if not isinstance(pid, int) or pid <= 0:
        raise FrozenRuntimeLockMetadataError(f"pid inválido: {pid}")

    create_time = payload["process_create_time"]
    if not isinstance(create_time, (int, float)):
        raise FrozenRuntimeLockMetadataError(f"process_create_time inválido: {create_time}")

    acquired_at = payload["acquired_at"]
    if not isinstance(acquired_at, (int, float)):
        raise FrozenRuntimeLockMetadataError(f"acquired_at inválido: {acquired_at}")

    return RootLockMetadata(
        schema_version=int(payload["schema_version"]),
        canonical_root=str(payload["canonical_root"]),
        session_id=session_str,
        pid=int(pid),
        process_create_time=float(create_time),
        acquired_at=float(acquired_at),
    )


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
        self._file_obj = file_obj
        self._thread_lock = thread_lock
        self._released = False

    def assert_owned(self) -> None:
        """Verifica que este handle sigue siendo dueño legítimo del lock."""
        if self._released:
            raise FrozenRuntimeLockOwnershipError("El handle del lock ya fue liberado.")

        if self._file_obj is None or self._file_obj.closed:
            raise FrozenRuntimeLockOwnershipError("El descriptor de archivo del lock fue cerrado.")

        with _THREAD_LOCKS_GUARD:
            active = _ACTIVE_HANDLES.get(self.canonical_root)
            if active is not self:
                raise FrozenRuntimeLockOwnershipError("Este handle no es el dueño activo registrado en memoria.")

        # Verificar que la metadata en disco no haya sido alterada ni sustituida (RL-18)
        if not self.lock_path.exists():
            raise FrozenRuntimeLockOwnershipError("El archivo de lock no existe en disco.")

        try:
            raw = self.lock_path.read_bytes()
            meta = _read_metadata_bytes(raw)
        except (FrozenRuntimeLockMetadataError, OSError) as exc:
            raise FrozenRuntimeLockOwnershipError(f"La metadata del lock en disco no es válida: {exc}") from exc

        if meta.session_id != self.session_id:
            raise FrozenRuntimeLockOwnershipError(
                f"La metadata en disco pertenece a otra sesión ({meta.session_id} != {self.session_id})"
            )
        if meta.pid != self.pid or meta.canonical_root != self.canonical_root:
            raise FrozenRuntimeLockOwnershipError("La metadata en disco no coincide con este handle.")

    def release(self) -> None:
        """Libera la exclusión del lock de forma segura e idempotente."""
        if self._released:
            return

        with _THREAD_LOCKS_GUARD:
            active = _ACTIVE_HANDLES.get(self.canonical_root)
            if active is not None and active.session_id != self.session_id:
                raise FrozenRuntimeLockOwnershipError(
                    f"No se puede liberar un lock perteneciente a otra sesión ({self.session_id} != {active.session_id})."
                )
            if active is None and self._file_obj is None:
                raise FrozenRuntimeLockOwnershipError("Este handle no posee un lock activo para liberar.")
            if active is self:
                _ACTIVE_HANDLES.pop(self.canonical_root, None)

        self._released = True

        if self._file_obj is not None and not self._file_obj.closed:
            try:
                # Truncar a 0 bytes en liberación limpia para indicar estado libre
                self._file_obj.seek(0)
                self._file_obj.truncate(0)
                self._file_obj.flush()
                os.fsync(self._file_obj.fileno())
            except OSError:
                pass
            finally:
                _unlock_fd(self._file_obj.fileno())
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
    """Inspecciona el estado de exclusión y metadata del lock de un root sin mutarlo."""
    canonical_str, lock_path = canonical_root_key(root)
    if not lock_path.exists() or lock_path.stat().st_size == 0:
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
        metadata = _read_metadata_bytes(raw_bytes)
    except FrozenRuntimeLockMetadataError:
        return LockInspectionResult(
            canonical_root=canonical_str,
            lock_path=lock_path,
            disposition=LockDisposition.CORRUPT_METADATA,
            is_os_locked=True,
            metadata=None,
            liveness=None,
        )

    # Probar si el OS lock está tomado
    is_os_locked = False
    try:
        with open(lock_path, "r+b") as probe_file:
            if not _lock_fd(probe_file.fileno()):
                is_os_locked = True
            else:
                _unlock_fd(probe_file.fileno())
    except OSError:
        is_os_locked = True

    liveness = _check_process_liveness(metadata.pid, metadata.process_create_time)

    if is_os_locked:
        if liveness == OwnerLiveness.ALIVE:
            disposition = LockDisposition.HELD_BY_LIVE_OWNER
        elif liveness == OwnerLiveness.INDETERMINATE:
            disposition = LockDisposition.INDETERMINATE
        else:
            disposition = LockDisposition.ORPHANED
    else:
        if liveness == OwnerLiveness.DEAD:
            disposition = LockDisposition.ORPHANED
        elif liveness == OwnerLiveness.INDETERMINATE:
            disposition = LockDisposition.INDETERMINATE
        else:
            disposition = LockDisposition.ORPHANED

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
        # Asegurar existencia del lockfile exclusivo si no existe
        if not lock_path.exists():
            try:
                with open(lock_path, "xb"):
                    pass
            except FileExistsError:
                pass

        # El descriptor debe permanecer abierto bajo el ciclo de vida del handle
        f = open(lock_path, "r+b")  # noqa: SIM115

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
                meta_existente = _read_metadata_bytes(raw_existente)
            except FrozenRuntimeLockMetadataError as exc:
                _unlock_fd(f.fileno())
                f.close()
                raise FrozenRuntimeLockMetadataError(f"Metadata corrupta en el lockfile preexistente: {exc}") from exc

            # Verificar liveness del dueño preexistente
            liveness = _check_process_liveness(meta_existente.pid, meta_existente.process_create_time)
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
        own_pid = os.getpid()
        own_create_time = psutil.Process(own_pid).create_time()
        acquired_at = time.time()

        payload = {
            "schema_version": LOCK_SCHEMA_VERSION,
            "canonical_root": canonical_str,
            "session_id": session_id,
            "pid": own_pid,
            "process_create_time": own_create_time,
            "acquired_at": acquired_at,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

        f.seek(0)
        f.write(serialized)
        f.truncate(len(serialized))
        f.flush()
        os.fsync(f.fileno())

        handle = FrozenRuntimeRootLockHandle(
            root=root_path,
            canonical_root=canonical_str,
            lock_path=lock_path,
            session_id=session_id,
            pid=own_pid,
            process_create_time=own_create_time,
            acquired_at=acquired_at,
            file_obj=f,
            thread_lock=thread_lock,
        )

        with _THREAD_LOCKS_GUARD:
            _ACTIVE_HANDLES[canonical_str] = handle

        return handle

    except Exception:
        if f is not None and not f.closed:
            _unlock_fd(f.fileno())
            with contextlib.suppress(OSError):
                f.close()
        thread_lock.release()
        raise
