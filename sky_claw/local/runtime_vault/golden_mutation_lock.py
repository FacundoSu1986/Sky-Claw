"""Runtime Vault — GoldenMutationLock exclusivo por Golden (GP2-S3b, componente G).

ADR 0010 §19.1 es la fuente normativa. El lock es un archivo
``%ProgramData%\\Sky-Claw\\runtime_vault\\locks\\skyclaw_golden_lock_<vol>_<fid>.lock``
abierto con ``CreateFileW`` bajo el contrato inequívoco::

    dwDesiredAccess       = GENERIC_READ | GENERIC_WRITE
    dwShareMode           = 0    (EXCLUSIVO: sin FILE_SHARE_*)
    dwCreationDisposition = OPEN_ALWAYS
    dwFlagsAndAttributes  = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT

Contratos anclados:

- El path del lock se DERIVA internamente (mutante M-L3: el caller nunca pasa
  el path; sólo ``volume_serial_number`` + ``root_file_id``; la raíz ProgramData
  se resuelve vía SHGetKnownFolderPath de S3a, nunca por env var).
- ``ERROR_SHARING_VIOLATION`` (32) → ``GoldenLockBusyError`` tipado. UN solo
  intento: no hay bucles de retry infinitos (M-L2) ni política de reintentos
  inventada en este slice.
- Post-open: ``GetFileInformationByHandleEx(FileAttributeTagInfo).ReparseTag``
  debe ser 0 (§19.1.6): cualquier reparse/junction aborta fail-closed.
- Metadata canónica con exactamente 7 claves (§19.1.5): ``lock_key``,
  ``owner_pid``, ``owner_process_creation_time``, ``session_id``,
  ``operation_id``, ``phase``, ``created_at`` (epoch seconds int).
- Defensa PID-reuse (§19.1.5): metadata con ``phase != RELEASED`` y dueño vivo
  (``OpenProcess(owner_pid)`` + ``GetProcessTimes`` creation-time coincidente)
  → BUSY, prohibido robar. Dueño muerto o PID reutilizado → lock HUÉRFANO
  (``GoldenLockOrphanedError``): la ruta segura es Crash Recovery §20, fuera de
  este slice. Legibilidad del dueño indeterminada → BUSY (nunca se roba en
  ambigüedad). ``phase == RELEASED`` → residual: se sobreescribe sin borrado.
- ``GoldenMutationLockHandle``: ``release()`` escribe ``phase=RELEASED``,
  flushea y cierra EXACTAMENTE una vez (el archivo NO se borra, §19.1.5); uso
  posterior a la liberación lanza ``GoldenLockOwnershipError``.
"""

from __future__ import annotations

import ctypes
import json
import pathlib
import sys
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sky_claw.local.runtime_vault.privileged_boundary import (
    PlanAuthorizationError,
    PrivilegedBoundaryUnsupportedError,
)

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class GoldenLockError(PlanAuthorizationError):
    """Base de errores del GoldenMutationLock (fail-closed; REFUSE_TO_PLAN en planning)."""


class GoldenLockBusyError(GoldenLockError):
    """El lock está poseído por otro proceso vivo: refuse tipado, sin retry."""

    def __init__(self, *args: Any, win32_error: int | None = None) -> None:
        super().__init__(*args)
        self.win32_error = win32_error


class GoldenLockOrphanedError(GoldenLockError):
    """Lock huérfano por crash (dueño muerto o PID reutilizado): exige Crash Recovery §20."""


class GoldenLockMetadataError(GoldenLockError):
    """La metadata del lock es ilegible o no cumple el esquema cerrado: fail-closed."""


class GoldenLockIoError(GoldenLockError):
    """Fallo de E/S nativa sobre el lock file (read/write/flush): fail-closed."""

    def __init__(self, *args: Any, win32_error: int | None = None) -> None:
        super().__init__(*args)
        self.win32_error = win32_error


class GoldenLockOwnershipError(GoldenLockError):
    """Uso del lock después de su liberación o acuñación fuera del path de adquisición."""


class GoldenLockAcquisitionCleanupError(GoldenLockIoError):
    """La adquisición falló tras haber escrito metadata fresca (carried cleanup explícito).

    ``cleanup_succeeded`` es True sólo si el cleanup a metadata RELEASED +
    flush tuvo éxito (estado residual liberado, reusable). False declara un
    estado AMBIGUO fail-closed: jamás se afirma que el lock quedó liberado.
    """

    def __init__(self, *args: Any, win32_error: int | None = None, cleanup_succeeded: bool = False) -> None:
        super().__init__(*args, win32_error=win32_error)
        self.cleanup_succeeded = cleanup_succeeded


# ============================================================================
# Constantes Normativas (contrato CreateFileW, ancladas por tests)
# ============================================================================

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000

#: dwDesiredAccess contractual: GENERIC_READ | GENERIC_WRITE (§19.1.1).
GOLDEN_LOCK_DESIRED_ACCESS = GENERIC_READ | GENERIC_WRITE

#: dwShareMode contractual: 0 estrictamente (NUNCA FILE_SHARE_READ/WRITE/DELETE).
GOLDEN_LOCK_SHARE_MODE = 0

#: dwCreationDisposition contractual: OPEN_ALWAYS (Archivo residual reutilizable).
GOLDEN_LOCK_CREATION_DISPOSITION = 4

_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

#: dwFlagsAndAttributes contractual (§19.1.1).
GOLDEN_LOCK_FLAGS = _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT

GOLDEN_LOCK_NAME_PREFIX = "skyclaw_golden_lock_"
GOLDEN_LOCK_FILE_SUFFIX = ".lock"

_MAX_METADATA_BYTES = 65536  # Cota defensiva: metadata canónica mide < 1 KiB.

_MAX_UINT32 = (1 << 32) - 1
_MAX_UINT64 = (1 << 64) - 1
_MAX_UINT128 = (1 << 128) - 1
_MAX_INT64 = (1 << 63) - 1

_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

if sys.platform == "win32":
    from ctypes import wintypes as _wt

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", _wt.DWORD),
            ("ReparseTag", _wt.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        _wt.LPCWSTR,
        _wt.DWORD,
        _wt.DWORD,
        _wt.LPVOID,
        _wt.DWORD,
        _wt.DWORD,
        _wt.HANDLE,
    ]
    _kernel32.CreateFileW.restype = _wt.HANDLE
    _kernel32.CloseHandle.argtypes = [_wt.HANDLE]
    _kernel32.CloseHandle.restype = _wt.BOOL
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        _wt.HANDLE,
        ctypes.c_int,
        _wt.LPVOID,
        _wt.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = _wt.BOOL
    _kernel32.GetFileSizeEx.argtypes = [_wt.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    _kernel32.GetFileSizeEx.restype = _wt.BOOL
    _kernel32.ReadFile.argtypes = [
        _wt.HANDLE,
        _wt.LPVOID,
        _wt.DWORD,
        ctypes.POINTER(_wt.DWORD),
        _wt.LPVOID,
    ]
    _kernel32.ReadFile.restype = _wt.BOOL
    _kernel32.WriteFile.argtypes = [
        _wt.HANDLE,
        _wt.LPCVOID,
        _wt.DWORD,
        ctypes.POINTER(_wt.DWORD),
        _wt.LPVOID,
    ]
    _kernel32.WriteFile.restype = _wt.BOOL
    _kernel32.SetEndOfFile.argtypes = [_wt.HANDLE]
    _kernel32.SetEndOfFile.restype = _wt.BOOL
    _kernel32.SetFilePointerEx.argtypes = [
        _wt.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        _wt.DWORD,
    ]
    _kernel32.SetFilePointerEx.restype = _wt.BOOL
    _kernel32.FlushFileBuffers.argtypes = [_wt.HANDLE]
    _kernel32.FlushFileBuffers.restype = _wt.BOOL
    _kernel32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
    _kernel32.OpenProcess.restype = _wt.HANDLE
    _kernel32.GetProcessTimes.argtypes = [
        _wt.HANDLE,
        ctypes.POINTER(_wt.FILETIME),
        ctypes.POINTER(_wt.FILETIME),
        ctypes.POINTER(_wt.FILETIME),
        ctypes.POINTER(_wt.FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = _wt.BOOL
    _kernel32.GetCurrentProcessId.argtypes = []
    _kernel32.GetCurrentProcessId.restype = _wt.DWORD
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    _kernel32.ProcessIdToSessionId.argtypes = [_wt.DWORD, ctypes.POINTER(_wt.DWORD)]
    _kernel32.ProcessIdToSessionId.restype = _wt.BOOL


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise PrivilegedBoundaryUnsupportedError(
            "El GoldenMutationLock nativo (CreateFileW share=0) solo existe en Windows"
        )


def _is_invalid_handle(handle: object) -> bool:
    if handle is None or handle == 0:
        return True
    return handle in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


def _filetime_to_uint64(file_time: Any) -> int:
    return (int(file_time.dwHighDateTime) << 32) | int(file_time.dwLowDateTime)


# ============================================================================
# Validadores del Modelo
# ============================================================================


def _validate_uint(value: int, field_name: str, *, max_value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GoldenLockModelError(f"{field_name} debe ser un entero")
    if not 0 <= value <= max_value:
        raise GoldenLockModelError(f"{field_name} debe estar en el rango [0, {max_value}]")
    return value


def _validate_canonical_operation_id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise GoldenLockModelError("operation_id debe ser UUID textual canónico")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise GoldenLockModelError("operation_id no es un UUID válido") from exc
    canonical = str(parsed)
    if canonical != value:
        raise GoldenLockModelError("operation_id debe ser el UUID canónico lowercase con guiones")
    return canonical


class GoldenLockModelError(GoldenLockError):
    """El modelo del lock (identidad, metadata en memoria, argumentos) es inválido."""


# ============================================================================
# Derivación Interna del Path (M-L3: el caller NUNCA pasa el path)
# ============================================================================


def derive_golden_lock_key(volume_serial_number: int, root_file_id: int) -> str:
    """Clave identitaria del lock: ``skyclaw_golden_lock_<vol_hex>_<fid_hex>`` (§19.1.2)."""
    serial = _validate_uint(volume_serial_number, "volume_serial_number", max_value=_MAX_UINT64)
    file_id = _validate_uint(root_file_id, "root_file_id", max_value=_MAX_UINT128)
    return f"{GOLDEN_LOCK_NAME_PREFIX}{serial:x}_{file_id:x}"


def derive_golden_lock_path(
    volume_serial_number: int,
    root_file_id: int,
    *,
    programdata_resolver: Any = None,
) -> pathlib.PurePath:
    """Deriva el path canónico del lock desde la identidad física del Golden.

    El caller NO puede pasar el path del lock (mutante M-L3): sólo aporta la
    identidad física ``(volume_serial_number, root_file_id)``. La raíz del
    namespace se resuelve internamente (por defecto
    ``SHGetKnownFolderPath(FOLDERID_ProgramData)`` vía S3a); ``programdata_resolver``
    es un seam de verificación que aporta la RAÍZ ProgramData, nunca el lock path.
    """
    base: pathlib.PurePath
    if programdata_resolver is None:
        _ensure_windows()
        from sky_claw.local.runtime_vault.trusted_namespace import _resolve_programdata_known_folder

        base = pathlib.Path(str(_resolve_programdata_known_folder()))
    else:
        raw = programdata_resolver()
        if isinstance(raw, str):
            if not raw.strip() or "\x00" in raw:
                raise PlanAuthorizationError("El resolver de ProgramData devolvió una raíz vacía o con NUL")
            base = pathlib.PureWindowsPath(raw)
        elif isinstance(raw, pathlib.PurePath):
            base = raw
        else:
            raise PlanAuthorizationError(
                f"El resolver de ProgramData devolvió un tipo no contratado: {type(raw).__name__}"
            )
    if not base.is_absolute():
        raise PlanAuthorizationError(f"La raíz ProgramData resuelta no es absoluta: {base!r}")
    locks_dir = base / "Sky-Claw" / "runtime_vault" / "locks"
    return locks_dir / f"{derive_golden_lock_key(volume_serial_number, root_file_id)}{GOLDEN_LOCK_FILE_SUFFIX}"


# ============================================================================
# Fases y Metadata (esquema cerrado de 7 claves, §19.1.5)
# ============================================================================


class GoldenLockPhase(StrEnum):
    """Fases que ESTE slice escribe en la metadata del lock.

    Las fases de mutación (APPLYING, ...) pertenecen a S4 y NO se declaran
    aquí: inventarlas ahora abriría la ilusión de un FSM autoritativo inexistente.
    """

    AUTHORIZATION_BOUNDARY = "AUTHORIZATION_BOUNDARY"
    RELEASED = "RELEASED"


_LOCK_METADATA_KEYS = (
    "lock_key",
    "owner_pid",
    "owner_process_creation_time",
    "session_id",
    "operation_id",
    "phase",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class GoldenLockMetadata:
    """Metadata del dueño del lock (campos exactos de ADR 0010 §19.1.5)."""

    lock_key: str
    owner_pid: int
    owner_process_creation_time: int
    session_id: int
    operation_id: str
    phase: str
    created_at: int  # epoch seconds UTC

    def __post_init__(self) -> None:
        key = self.lock_key
        if not isinstance(key, str) or not key.startswith(GOLDEN_LOCK_NAME_PREFIX):
            raise GoldenLockModelError("lock_key debe comenzar con el prefijo normativo")
        object.__setattr__(self, "owner_pid", _validate_uint(self.owner_pid, "owner_pid", max_value=_MAX_UINT32))
        if self.owner_pid == 0:
            raise GoldenLockModelError("owner_pid debe ser > 0 (el pid 0 no es un dueño válido)")
        object.__setattr__(
            self,
            "owner_process_creation_time",
            _validate_uint(self.owner_process_creation_time, "owner_process_creation_time", max_value=_MAX_UINT64),
        )
        object.__setattr__(self, "session_id", _validate_uint(self.session_id, "session_id", max_value=_MAX_UINT32))
        object.__setattr__(self, "operation_id", _validate_canonical_operation_id(self.operation_id))
        if self.phase not in (GoldenLockPhase.AUTHORIZATION_BOUNDARY.value, GoldenLockPhase.RELEASED.value):
            raise GoldenLockModelError(f"phase desconocida para este slice: {self.phase!r}")
        object.__setattr__(self, "created_at", _validate_uint(self.created_at, "created_at", max_value=_MAX_INT64))
        if self.created_at == 0:
            raise GoldenLockModelError("created_at debe ser > 0 (epoch seconds)")


def serialize_golden_lock_metadata(metadata: GoldenLockMetadata) -> bytes:
    """Serialización canónica (claves ordenadas, separadores compactos, UTF-8)."""
    if not isinstance(metadata, GoldenLockMetadata):
        raise GoldenLockModelError("metadata debe ser GoldenLockMetadata")
    payload = {
        "created_at": metadata.created_at,
        "lock_key": metadata.lock_key,
        "operation_id": metadata.operation_id,
        "owner_pid": metadata.owner_pid,
        "owner_process_creation_time": metadata.owner_process_creation_time,
        "phase": metadata.phase,
        "session_id": metadata.session_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def deserialize_golden_lock_metadata(raw: bytes) -> GoldenLockMetadata:
    """Parse estricto: tamaño acotado, objeto JSON, exactamente las 7 claves normativas."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise GoldenLockMetadataError("metadata vacía o no son bytes")
    if len(raw) > _MAX_METADATA_BYTES:
        raise GoldenLockMetadataError(f"metadata excede la cota defensiva ({len(raw)} bytes > {_MAX_METADATA_BYTES})")
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoldenLockMetadataError("metadata del lock no es JSON UTF-8 válido") from exc
    if not isinstance(payload, dict):
        raise GoldenLockMetadataError("metadata del lock no es un objeto JSON")
    if set(payload) != set(_LOCK_METADATA_KEYS):
        raise GoldenLockMetadataError(
            "La metadata del lock debe contener exactamente las 7 claves normativas (esquema cerrado)"
        )
    try:
        return GoldenLockMetadata(
            lock_key=payload["lock_key"],
            owner_pid=payload["owner_pid"],
            owner_process_creation_time=payload["owner_process_creation_time"],
            session_id=payload["session_id"],
            operation_id=payload["operation_id"],
            phase=payload["phase"],
            created_at=payload["created_at"],
        )
    except (TypeError, KeyError, GoldenLockModelError) as exc:
        raise GoldenLockMetadataError(f"metadata del lock fuera de esquema: {exc}") from exc


# ============================================================================
# Identidad del Lock (evidencia inmutable, sin HANDLE crudo)
# ============================================================================


@dataclass(frozen=True, slots=True)
class GoldenLockIdentity:
    """Evidencia inmutable de un GoldenMutationLock adquirido por ESTE proceso."""

    lock_key: str
    volume_serial_number: int
    root_file_id: int
    owner_pid: int
    owner_process_creation_time: int
    session_id: int
    operation_id: str
    phase: str
    created_at: int

    def __post_init__(self) -> None:
        metadata = GoldenLockMetadata(
            lock_key=self.lock_key,
            owner_pid=self.owner_pid,
            owner_process_creation_time=self.owner_process_creation_time,
            session_id=self.session_id,
            operation_id=self.operation_id,
            phase=self.phase,
            created_at=self.created_at,
        )
        object.__setattr__(self, "lock_key", metadata.lock_key)
        object.__setattr__(self, "owner_pid", metadata.owner_pid)
        object.__setattr__(self, "owner_process_creation_time", metadata.owner_process_creation_time)
        object.__setattr__(self, "session_id", metadata.session_id)
        object.__setattr__(self, "operation_id", metadata.operation_id)
        object.__setattr__(self, "phase", metadata.phase)
        object.__setattr__(self, "created_at", metadata.created_at)
        object.__setattr__(
            self,
            "volume_serial_number",
            _validate_uint(self.volume_serial_number, "volume_serial_number", max_value=_MAX_UINT64),
        )
        object.__setattr__(
            self,
            "root_file_id",
            _validate_uint(self.root_file_id, "root_file_id", max_value=_MAX_UINT128),
        )
        expected_key = derive_golden_lock_key(self.volume_serial_number, self.root_file_id)
        if metadata.lock_key != expected_key:
            raise GoldenLockModelError("lock_key no coincide con (volume_serial_number, root_file_id)")


# ============================================================================
# Kernel del Lock (seam) — nativo Win32 y fake POSIX-testable
# ============================================================================


class GoldenLockKernel(Protocol):
    """Primitivas de kernel para el ciclo de vida del lock.

    Errores tipados, nunca éxito fabricado: este seam es el ÚNICO punto Win32 y
    permite probar causalmente la máquina de estados con una implementación
    nativa real en Windows (y un fake exhaustivo en POSIX) sin mocks de la lógica.
    """

    def open_lock_file(self, path: pathlib.PurePath) -> int: ...
    def get_reparse_tag(self, handle: int) -> int: ...
    def read_lock_bytes(self, handle: int) -> bytes: ...
    def write_lock_bytes(self, handle: int, payload: bytes) -> None: ...
    def flush_lock(self, handle: int) -> None: ...
    def is_owner_alive(self, owner_pid: int, owner_creation_time: int) -> bool | None: ...
    def current_process_identity(self) -> tuple[int, int, int]: ...
    def current_epoch_seconds(self) -> int: ...
    def close_handle(self, handle: int) -> None: ...


class _Win32GoldenLockKernel:
    """Kernel nativo: CreateFileW share=0 + reparse check + E/S acotada + PID-reuse."""

    def open_lock_file(self, path: pathlib.PurePath) -> int:
        handle = _kernel32.CreateFileW(
            str(path),
            GOLDEN_LOCK_DESIRED_ACCESS,
            GOLDEN_LOCK_SHARE_MODE,
            None,
            GOLDEN_LOCK_CREATION_DISPOSITION,
            GOLDEN_LOCK_FLAGS,
            None,
        )
        if _is_invalid_handle(handle):
            err = ctypes.get_last_error()
            if err == _ERROR_SHARING_VIOLATION:
                raise GoldenLockBusyError(
                    f"Otro proceso posee el GoldenMutationLock '{path}' (dwShareMode=0): ERROR_SHARING_VIOLATION",
                    win32_error=err,
                )
            raise GoldenLockIoError(f"CreateFileW falló al abrir el lock '{path}': código {err}", win32_error=err)
        return int(handle)

    def get_reparse_tag(self, handle: int) -> int:
        info = _FileAttributeTagInfo()
        if not _kernel32.GetFileInformationByHandleEx(
            handle,
            9,  # FileAttributeTagInfo
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"GetFileInformationByHandleEx falló sobre el lock: código {err}", win32_error=err)
        return int(info.ReparseTag)

    def _seek_lock_begin(self, handle: int) -> None:
        # Cada operación es independiente de la posición heredada del handle:
        # el puntero SIEMPRE se fija en 0 antes de ReadFile/WriteFile (post-review P1:
        # re-apertura de un lock RELEASED jamás concatena <JSON viejo><JSON nuevo>).
        if not _kernel32.SetFilePointerEx(handle, 0, None, 0):  # 0 = FILE_BEGIN
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"SetFilePointerEx(FILE_BEGIN) falló sobre el lock: código {err}", win32_error=err)

    def read_lock_bytes(self, handle: int) -> bytes:
        size = ctypes.c_longlong(0)
        if not _kernel32.GetFileSizeEx(handle, ctypes.byref(size)):
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"GetFileSizeEx falló sobre el lock: código {err}", win32_error=err)
        if size.value == 0:
            return b""
        if size.value > _MAX_METADATA_BYTES:
            raise GoldenLockIoError(
                f"El lock file excede la cota de metadata ({size.value} bytes > {_MAX_METADATA_BYTES})",
                win32_error=None,
            )
        self._seek_lock_begin(handle)  # ANTES de ReadFile: posición 0 garantizada
        buffer = (ctypes.c_ubyte * size.value)()
        read = _wt.DWORD(0)
        if not _kernel32.ReadFile(handle, buffer, size.value, ctypes.byref(read), None):
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"ReadFile falló sobre el lock: código {err}", win32_error=err)
        if read.value != size.value:
            raise GoldenLockIoError("ReadFile devolvió un conteo parcial: todo o nada violado")
        return bytes(buffer)

    def write_lock_bytes(self, handle: int, payload: bytes) -> None:
        if not payload or len(payload) > _MAX_METADATA_BYTES:
            raise GoldenLockIoError("payload de metadata fuera de la cota normativa")
        self._seek_lock_begin(handle)  # ANTES de WriteFile: posición 0 garantizada
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        written = _wt.DWORD(0)
        if not _kernel32.WriteFile(handle, buffer, len(payload), ctypes.byref(written), None):
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"WriteFile falló sobre el lock: código {err}", win32_error=err)
        if written.value != len(payload):
            raise GoldenLockIoError("WriteFile escribió un conteo parcial: todo o nada violado")
        if not _kernel32.SetEndOfFile(handle):  # truncate write: el contenido viejo nunca queda colgando
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"SetEndOfFile falló sobre el lock: código {err}", win32_error=err)

    def flush_lock(self, handle: int) -> None:
        if not _kernel32.FlushFileBuffers(handle):
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"FlushFileBuffers falló sobre el lock: código {err}", win32_error=err)

    def is_owner_alive(self, owner_pid: int, owner_creation_time: int) -> bool | None:
        """True: proceso vivo con creation-time coincidente; False: muerto/PID reusado; None: indeterminable."""
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, owner_pid)
        if _is_invalid_handle(handle):
            err = ctypes.get_last_error()
            if err == _ERROR_INVALID_PARAMETER:
                return False  # PID inexistente: dueño muerto
            return None  # existe pero no verificable (p. ej. ERROR_ACCESS_DENIED): ambigüedad fail-closed
        try:
            creation = _wt.FILETIME()
            dummy_exit = _wt.FILETIME()
            dummy_kernel = _wt.FILETIME()
            dummy_user = _wt.FILETIME()
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(dummy_exit),
                ctypes.byref(dummy_kernel),
                ctypes.byref(dummy_user),
            ):
                return None
            return _filetime_to_uint64(creation) == owner_creation_time
        finally:
            _kernel32.CloseHandle(handle)

    def current_process_identity(self) -> tuple[int, int, int]:
        """(pid, creation_time QWORD, session_id) del proceso actual: el owner del lock."""
        pid = int(_kernel32.GetCurrentProcessId())
        current = _kernel32.GetCurrentProcess()  # pseudo-handle: jamás se cierra
        creation = _wt.FILETIME()
        dummy_exit = _wt.FILETIME()
        dummy_kernel = _wt.FILETIME()
        dummy_user = _wt.FILETIME()
        if not _kernel32.GetProcessTimes(
            current,
            ctypes.byref(creation),
            ctypes.byref(dummy_exit),
            ctypes.byref(dummy_kernel),
            ctypes.byref(dummy_user),
        ):
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"GetProcessTimes(self) falló: código {err}", win32_error=err)
        session_id = _wt.DWORD(0)
        if not _kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
            err = ctypes.get_last_error()
            raise GoldenLockIoError(f"ProcessIdToSessionId falló: código {err}", win32_error=err)
        return pid, _filetime_to_uint64(creation), int(session_id.value)

    def current_epoch_seconds(self) -> int:
        return int(time.time())

    def close_handle(self, handle: int) -> None:
        if not _is_invalid_handle(handle):
            _kernel32.CloseHandle(handle)


# ============================================================================
# Clasificación del Lock Preexistente (§19.1.5)
# ============================================================================


class PreexistingLockDisposition(StrEnum):
    """Clasificación del estado previo del lock: reusable sólo si limpio o RELEASED."""

    NO_PREEXISTING = "no_preexisting"
    RESIDUAL_RELEASED = "residual_released"
    BUSY_OWNER_ALIVE = "busy_owner_alive"
    BUSY_OWNER_UNREADABLE = "busy_owner_unreadable"
    ORPHANED_LOCK = "orphaned_lock"


def classify_preexisting_lock(
    metadata: GoldenLockMetadata | None,
    *,
    owner_alive: bool | None,
) -> PreexistingLockDisposition:
    """Clasifica la metadata preexistente (pura, sin efectos).

    - Sin metadata: el lock estaba libre (``NO_PREEXISTING``).
    - ``phase == RELEASED``: residual de una operación terminada normalmente
      (§19.1.5); el nuevo dueño la sobreescribe (``RESIDUAL_RELEASED``).
    - ``phase != RELEASED``: el dueño sigue vivo con pid+creation-time
      coincidentes -> BUSY (prohibido robar). Dueño muerto o PID reutilizado ->
      ORPHANED (crash recovery §20, fuera de este slice). Legibilidad
      indeterminada del dueño -> BUSY por fail-closed (nunca se roba en
      ambigüedad).
    """
    if metadata is None:
        return PreexistingLockDisposition.NO_PREEXISTING
    if metadata.phase == GoldenLockPhase.RELEASED.value:
        return PreexistingLockDisposition.RESIDUAL_RELEASED
    if owner_alive is True:
        return PreexistingLockDisposition.BUSY_OWNER_ALIVE
    if owner_alive is None:
        return PreexistingLockDisposition.BUSY_OWNER_UNREADABLE
    return PreexistingLockDisposition.ORPHANED_LOCK


# ============================================================================
# Handle del Lock (ownership exactamente una vez)
# ============================================================================

_MINT_PROOF: Any = object()


class GoldenMutationLockHandle:
    """Ownership del handle exclusivo del lock (abierto durante toda la operación).

    - Sólo se acuña desde :func:`acquire_golden_mutation_lock` (proof privada).
    - ``release()`` escribe ``phase=RELEASED``, flushea y cierra EXACTAMENTE una
      vez (el archivo NO se borra, §19.1.5). Incluso si el flush falla, el
      handle se cierra exactamente una vez y el error se propaga tipado.
    - Todo uso posterior al cierre lanza ``GoldenLockOwnershipError``.
    """

    __slots__ = ("_closed", "_handle", "_identity", "_kernel")

    def __init__(
        self,
        handle: int,
        identity: GoldenLockIdentity,
        kernel: GoldenLockKernel,
        *,
        _proof: Any = None,
    ) -> None:
        if _proof is not _MINT_PROOF:
            raise GoldenLockOwnershipError(
                "GoldenMutationLockHandle sólo puede crearse desde acquire_golden_mutation_lock"
            )
        if _is_invalid_handle(handle):
            raise GoldenLockOwnershipError("No se puede tomar ownership de un handle de lock inválido")
        self._handle = int(handle)
        self._identity = identity
        self._kernel = kernel
        self._closed = False

    @property
    def identity(self) -> GoldenLockIdentity:
        return self._identity

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def raw_handle(self) -> int:
        """Handle prestado (borrowing) para llamadas Win32 internas: el consumidor NO lo cierra."""
        if self._closed:
            raise GoldenLockOwnershipError("Uso del GoldenMutationLock después de su liberación")
        return self._handle

    def release(self) -> bool:
        """Libera el lock: metadata RELEASED + flush + close exactamente una vez.

        True si esta llamada liberó; False si ya estaba liberado. Un fallo de
        escritura/flush NO impide el cierre del handle (exactamente una vez) y
        produce ``GoldenLockIoError`` — un proceso vivo nunca pierde su lock por
        este fallo (degrada a la ruta huérfano C10 de §20 si el proceso muere).
        """
        if self._closed:
            return False
        self._closed = True
        release_metadata = GoldenLockMetadata(
            lock_key=self._identity.lock_key,
            owner_pid=self._identity.owner_pid,
            owner_process_creation_time=self._identity.owner_process_creation_time,
            session_id=self._identity.session_id,
            operation_id=self._identity.operation_id,
            phase=GoldenLockPhase.RELEASED.value,
            created_at=self._identity.created_at,
        )
        try:
            self._kernel.write_lock_bytes(self._handle, serialize_golden_lock_metadata(release_metadata))
            self._kernel.flush_lock(self._handle)
        finally:
            self._kernel.close_handle(self._handle)
        return True

    def __enter__(self) -> GoldenMutationLockHandle:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


# ============================================================================
# Adquisición
# ============================================================================


def _acquire_lock_core(
    lock_path: pathlib.PurePath,
    lock_key: str,
    volume_serial_number: int,
    root_file_id: int,
    operation_id: str,
    kernel: GoldenLockKernel,
) -> GoldenMutationLockHandle:
    """Núcleo de adquisición (abierto, validado, clasificado, metadata escrita y flusheada)."""
    handle = kernel.open_lock_file(lock_path)
    success = False
    try:
        reparse_tag = kernel.get_reparse_tag(handle)
        if reparse_tag != 0:
            raise GoldenLockError(
                f"El lock file fue sustituido por un reparse point (tag=0x{reparse_tag:08X}): fail-closed"
            )

        raw = kernel.read_lock_bytes(handle)
        metadata: GoldenLockMetadata | None = None
        if raw:
            metadata = deserialize_golden_lock_metadata(raw)
            if metadata.lock_key != lock_key:
                raise GoldenLockMetadataError(
                    f"El lock file contiene lock_key '{metadata.lock_key}' distinto de '{lock_key}': "
                    "evidencia de sustitución cruzada entre Goldens"
                )

        owner_alive: bool | None = None
        if metadata is not None and metadata.phase != GoldenLockPhase.RELEASED.value:
            owner_alive = kernel.is_owner_alive(metadata.owner_pid, metadata.owner_process_creation_time)

        disposition = classify_preexisting_lock(metadata, owner_alive=owner_alive)
        if disposition is PreexistingLockDisposition.BUSY_OWNER_ALIVE:
            raise GoldenLockBusyError(
                "El GoldenMutationLock pertenece a un proceso vivo con pid+creation-time coincidentes: "
                "prohibido robar el lock (ADR 0010 §19.1.5)"
            )
        if disposition is PreexistingLockDisposition.BUSY_OWNER_UNREADABLE:
            raise GoldenLockBusyError(
                "No se pudo demostrar muerte del dueño del lock (ambigüedad): fail-closed, nunca se roba"
            )
        if disposition is PreexistingLockDisposition.ORPHANED_LOCK:
            raise GoldenLockOrphanedError(
                "Lock huérfano por crash (dueño muerto o PID reutilizado): requiere el procedimiento de "
                "crash recovery de §20 (fuera de este slice)"
            )

        owner_pid, creation_time, session_id = kernel.current_process_identity()
        created_at = kernel.current_epoch_seconds()
        identity = GoldenLockIdentity(
            lock_key=lock_key,
            volume_serial_number=volume_serial_number,
            root_file_id=root_file_id,
            owner_pid=owner_pid,
            owner_process_creation_time=creation_time,
            session_id=session_id,
            operation_id=operation_id,
            phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            created_at=created_at,
        )
        fresh_metadata = GoldenLockMetadata(
            lock_key=identity.lock_key,
            owner_pid=identity.owner_pid,
            owner_process_creation_time=identity.owner_process_creation_time,
            session_id=identity.session_id,
            operation_id=identity.operation_id,
            phase=identity.phase,
            created_at=identity.created_at,
        )
        try:
            kernel.write_lock_bytes(handle, serialize_golden_lock_metadata(fresh_metadata))
            kernel.flush_lock(handle)
        except GoldenLockIoError as io_exc:
            # Post-escritura: quedaría metadata NO-RELEASED perteneciente a ESTE
            # proceso sin kernel handle (self-DoS). El handle exclusivo TODAVÍA
            # está abierto (lo cierra el finally): cleanup explícito a RELEASED
            # + flush. Si el cleanup también falla, el estado queda AMBIGUO y se
            # reporta tipado fail-closed; NUNCA se convierte en success y el
            # lock file JAMÁS se borra (§19.1).
            cleanup_ok = False
            try:
                residual_metadata = GoldenLockMetadata(
                    lock_key=identity.lock_key,
                    owner_pid=identity.owner_pid,
                    owner_process_creation_time=identity.owner_process_creation_time,
                    session_id=identity.session_id,
                    operation_id=identity.operation_id,
                    phase=GoldenLockPhase.RELEASED.value,
                    created_at=identity.created_at,
                )
                kernel.write_lock_bytes(handle, serialize_golden_lock_metadata(residual_metadata))
                kernel.flush_lock(handle)
                cleanup_ok = True
            except GoldenLockIoError:
                cleanup_ok = False
            raise GoldenLockAcquisitionCleanupError(
                f"Adquisición del lock falló tras escribir metadata fresca (cleanup a RELEASED "
                f"{'OK: lock residual liberado' if cleanup_ok else 'FALLÓ: estado ambiguo, fail-closed'})",
                win32_error=io_exc.win32_error,
                cleanup_succeeded=cleanup_ok,
            ) from io_exc

        lock_handle = GoldenMutationLockHandle(handle, identity, kernel, _proof=_MINT_PROOF)
        success = True
        return lock_handle
    finally:
        if not success:
            kernel.close_handle(handle)


def acquire_golden_mutation_lock(
    volume_serial_number: int,
    root_file_id: int,
    operation_id: str | uuid.UUID,
    *,
    kernel: GoldenLockKernel | None = None,
    programdata_resolver: Any = None,
) -> GoldenMutationLockHandle:
    """Adquiere el GoldenMutationLock exclusivo de un Golden (un solo intento).

    El path se deriva internamente (ProgramData + identidad física); el caller
    NO puede pasar el path del lock (M-L3). ``kernel`` y ``programdata_resolver``
    son seams de verificación: sin ambos, la vía productiva resuelve ProgramData
    vía SHGetKnownFolderPath (S3a) y usa el kernel nativo Win32.
    """
    serial = _validate_uint(volume_serial_number, "volume_serial_number", max_value=_MAX_UINT64)
    file_id = _validate_uint(root_file_id, "root_file_id", max_value=_MAX_UINT128)
    op_id = _validate_canonical_operation_id(str(operation_id))
    active_kernel: GoldenLockKernel
    if kernel is None:
        _ensure_windows()
        active_kernel = _Win32GoldenLockKernel()
    else:
        active_kernel = kernel
    lock_path = derive_golden_lock_path(serial, file_id, programdata_resolver=programdata_resolver)
    key = derive_golden_lock_key(serial, file_id)
    return _acquire_lock_core(lock_path, key, serial, file_id, op_id, active_kernel)


def _acquire_golden_mutation_lock_at(
    locks_dir: pathlib.PurePath,
    volume_serial_number: int,
    root_file_id: int,
    operation_id: str | uuid.UUID,
    *,
    kernel: GoldenLockKernel | None = None,
) -> GoldenMutationLockHandle:
    """Seam de verificación (convención ``_at`` del repo): adquiere bajo un locks dir explícito.

    La rama pública productiva es :func:`acquire_golden_mutation_lock` (path
    derivado, sin parámetros de path). Este seam sólo existe para tests causales
    con objetos descartables (y para la suite POSIX con kernel fake).
    """
    serial = _validate_uint(volume_serial_number, "volume_serial_number", max_value=_MAX_UINT64)
    file_id = _validate_uint(root_file_id, "root_file_id", max_value=_MAX_UINT128)
    op_id = _validate_canonical_operation_id(str(operation_id))
    if not isinstance(locks_dir, pathlib.PurePath) or not locks_dir.is_absolute():
        raise GoldenLockModelError("locks_dir del seam debe ser un path absoluto")
    key = derive_golden_lock_key(serial, file_id)
    lock_path = locks_dir / f"{key}{GOLDEN_LOCK_FILE_SUFFIX}"
    active_kernel: GoldenLockKernel
    if kernel is None:
        _ensure_windows()
        active_kernel = _Win32GoldenLockKernel()
    else:
        active_kernel = kernel
    return _acquire_lock_core(lock_path, key, serial, file_id, op_id, active_kernel)


__all__ = [
    "GOLDEN_LOCK_CREATION_DISPOSITION",
    "GOLDEN_LOCK_DESIRED_ACCESS",
    "GOLDEN_LOCK_FILE_SUFFIX",
    "GOLDEN_LOCK_FLAGS",
    "GOLDEN_LOCK_NAME_PREFIX",
    "GOLDEN_LOCK_SHARE_MODE",
    "GoldenLockAcquisitionCleanupError",
    "GoldenLockBusyError",
    "GoldenLockError",
    "GoldenLockIdentity",
    "GoldenLockIoError",
    "GoldenLockKernel",
    "GoldenLockMetadata",
    "GoldenLockMetadataError",
    "GoldenLockModelError",
    "GoldenLockOrphanedError",
    "GoldenLockOwnershipError",
    "GoldenLockPhase",
    "GoldenMutationLockHandle",
    "PreexistingLockDisposition",
    "acquire_golden_mutation_lock",
    "classify_preexisting_lock",
    "derive_golden_lock_key",
    "derive_golden_lock_path",
    "deserialize_golden_lock_metadata",
    "serialize_golden_lock_metadata",
]
