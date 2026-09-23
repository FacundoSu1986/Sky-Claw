"""Inspección física y revalidación handle-bound de la raíz (physical_root).

Contrato normativo ADR 0010 §11.0 / §11.4:
- La igualdad textual del path NO es autoridad suficiente.
- La identidad física se compone de:
  (canonical_root, VolumeSerialNumber, root_file_id)
- La apertura de la raíz se ejecuta handle-first sin invocar ``Path.resolve()``
  previo (evita seguir junctions/symlinks de forma no controlada).
- Se utiliza ``FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT``:
  si ``ReparseTag != 0``, la raíz es un reparse point -> fail-closed incondicional
  con :class:`PhysicalRootReparseError`.
- La ruta canónica final se extrae directamente desde el handle abierto vía
  ``GetFinalPathNameByHandleW``, garantizando inmunidad a carreras TOCTOU.
- Dos operaciones explícitamente diferenciadas:
  * ``derive_physical_root``: apertura segura que deriva la tupla para emitirla
    como evidencia observada (en OBSERVE / TOFU).
  * ``verify_physical_root``: apertura segura fresca que exige coincidencia
    exacta contra una expectativa ya autorizada (en VERIFY / RV-2).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import pathlib
import sys
from collections.abc import Iterator
from dataclasses import dataclass

from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.node_evidence import file_id_128_to_int
from sky_claw.local.runtime_vault.privileged_boundary import PrivilegedBoundaryUnsupportedError

# ============================================================================
# Excepciones Tipadas
# ============================================================================


class PhysicalRootError(RuntimeVaultError):
    """Base de errores de inspección física de la raíz."""


class PhysicalRootReparseError(PhysicalRootError):
    """La raíz o un ancestro presenta un reparse tag != 0 (junction, symlink, etc.)."""


class PhysicalRootMismatchError(PhysicalRootError):
    """La raíz física viva no coincide con la expectativa autorizada."""


class PhysicalRootIdentityUnavailableError(PhysicalRootError):
    """La identidad física de 128-bit (FileIdInfo) no está disponible o falló en el volumen."""


# ============================================================================
# Modelo Inmutable de Identidad Física
# ============================================================================


@dataclass(frozen=True, slots=True)
class PhysicalRootIdentity:
    """Identidad física inmutable de un directorio raíz en el sistema de archivos."""

    canonical_root: str
    volume_serial_number: int
    root_file_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_root, str) or not self.canonical_root.strip():
            raise PhysicalRootError("canonical_root debe ser un string no vacío")
        if not isinstance(self.volume_serial_number, int) or self.volume_serial_number <= 0:
            raise PhysicalRootError("volume_serial_number debe ser un entero positivo")
        if not isinstance(self.root_file_id, int) or self.root_file_id <= 0:
            raise PhysicalRootError("root_file_id debe ser un entero positivo")


# ============================================================================
# Constantes y ABI Win32
# ============================================================================

_FILE_READ_ATTRIBUTES = 0x0080
_READ_CONTROL = 0x00020000
_OPEN_EXISTING = 3
_FILE_SHARE_READ = 1
_FILE_SHARE_WRITE = 2
_FILE_SHARE_DELETE = 4
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FILE_LIST_DIRECTORY = 0x0001
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

_FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG = 9
_FILE_INFO_BY_HANDLE_CLASS_STANDARD = 1
_FILE_INFO_BY_HANDLE_CLASS_ID = 18

_VOLUME_NAME_DOS = 0x0
_FILE_NAME_NORMALIZED = 0x0

if sys.platform == "win32":
    from ctypes import wintypes

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    class _FileStandardInfo(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", ctypes.c_bool),
            ("Directory", ctypes.c_bool),
        ]

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL

    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    _kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetFileAttributesW.restype = wintypes.DWORD


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise PrivilegedBoundaryUnsupportedError("La inspección física de la raíz sólo está soportada en Windows")


def _normalize_dos_prefix(path_str: str) -> str:
    """Normaliza prefijos de namespace extendido de Windows (\\\\?\\)."""
    if path_str.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_str[8:]
    if path_str.startswith("\\\\?\\"):
        return path_str[4:]
    return path_str


def _inspect_physical_root_handle(handle: int, lexical_path: str) -> PhysicalRootIdentity:
    """Extrae y revalida la identidad física directamente desde el handle Win32 abierto."""
    # 1. Comprobar ReparseTag == 0
    tag_info = _FileAttributeTagInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle),
        _FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG,
        ctypes.byref(tag_info),
        ctypes.sizeof(tag_info),
    ):
        err = ctypes.get_last_error()
        raise PhysicalRootError(
            f"GetFileInformationByHandleEx(FileAttributeTagInfo) falló en '{lexical_path}': código Win32 {err}"
        )
    if tag_info.ReparseTag != 0:
        raise PhysicalRootReparseError(
            f"La raíz '{lexical_path}' presenta ReparseTag != 0 ({hex(tag_info.ReparseTag)}): "
            "los junctions, symlinks o mount points quedan prohibidos como raíz del Vault"
        )

    # 2. Comprobar que es un directorio
    std_info = _FileStandardInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle),
        _FILE_INFO_BY_HANDLE_CLASS_STANDARD,
        ctypes.byref(std_info),
        ctypes.sizeof(std_info),
    ):
        err = ctypes.get_last_error()
        raise PhysicalRootError(
            f"GetFileInformationByHandleEx(FileStandardInfo) falló en '{lexical_path}': código Win32 {err}"
        )
    if not bool(std_info.Directory):
        raise PhysicalRootError(f"La ruta '{lexical_path}' no es un directorio (es un archivo regular u otro tipo)")

    # 3. Extraer VolumeSerialNumber y FileId (128-bit estricto, sin degradación a 64-bit)
    id_info = _FileIdInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle),
        _FILE_INFO_BY_HANDLE_CLASS_ID,
        ctypes.byref(id_info),
        ctypes.sizeof(id_info),
    ):
        err = ctypes.get_last_error()
        raise PhysicalRootIdentityUnavailableError(
            f"GetFileInformationByHandleEx(FileIdInfo) falló o no está soportado en '{lexical_path}': código Win32 {err}. "
            "Se requiere soporte nativo de FileIdInfo de 128-bit; fallback a 64-bit prohibido (fail-closed)."
        )

    vol_serial = int(id_info.VolumeSerialNumber)
    file_id = file_id_128_to_int(id_info.FileId.Identifier)
    if file_id == 0 or vol_serial == 0:
        raise PhysicalRootIdentityUnavailableError(
            f"Identidad física no disponible o inválida en '{lexical_path}': "
            f"VolumeSerialNumber={vol_serial}, FileId={file_id}. Fail-closed incondicional."
        )

    # 4. Obtener canonical_root directamente del handle
    capacity = 32768
    buf = ctypes.create_unicode_buffer(capacity)
    chars = _kernel32.GetFinalPathNameByHandleW(
        ctypes.c_void_p(handle),
        buf,
        capacity,
        _VOLUME_NAME_DOS | _FILE_NAME_NORMALIZED,
    )
    if chars == 0 or chars >= capacity:
        err = ctypes.get_last_error()
        raise PhysicalRootError(f"GetFinalPathNameByHandleW falló en '{lexical_path}': código Win32 {err}")

    canonical_raw = str(buf.value)
    canonical = _normalize_dos_prefix(canonical_raw)

    return PhysicalRootIdentity(
        canonical_root=canonical,
        volume_serial_number=vol_serial,
        root_file_id=file_id,
    )


def _validate_no_ancestor_reparse(lexical_path: str) -> None:
    """Valida fail-closed que ningún ancestro del camino léxico sea un reparse point / junction / symlink.

    Normativa ADR 0010 §11.0 / §11.4:
    Previene que un atacante configure un junction/symlink en un directorio intermedio (p. ej. C:\\Games\\Skyrim
    donde C:\\Games es un junction hacia otra ubicación) para escapar del root esperado.
    """
    _ensure_windows()
    p = pathlib.Path(lexical_path)
    for ancestor in p.parents:
        ancestor_str = str(ancestor)
        attrs = _kernel32.GetFileAttributesW(ancestor_str)
        if attrs == _INVALID_FILE_ATTRIBUTES:
            err = ctypes.get_last_error()
            raise PhysicalRootError(f"GetFileAttributesW falló en el ancestro '{ancestor_str}': código Win32 {err}")
        if ancestor == ancestor.parent:
            # Raíz del volumen (ej. 'C:\\'): verificar si está montado como reparse point de volumen
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise PhysicalRootReparseError(
                    f"El volumen raíz '{ancestor_str}' presenta atributo de reparse point (0x{attrs:x}): prohibido (fail-closed)"
                )
        else:
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise PhysicalRootReparseError(
                    f"El ancestro '{ancestor_str}' presenta atributo de reparse point / junction (0x{attrs:x}): "
                    "los junctions o symlinks en rutas intermedias quedan prohibidos (fail-closed)"
                )


def _open_physical_root(root: pathlib.Path | str) -> tuple[int, str]:
    """Abre de forma segura el directorio raíz sin seguir reparse points ni invocar Path.resolve()."""
    _ensure_windows()
    # Normalización léxica pura (sin I/O ni resolución de symlinks)
    lexical_path = os.path.abspath(os.fspath(root))

    handle = _kernel32.CreateFileW(
        lexical_path,
        _FILE_READ_ATTRIBUTES | _READ_CONTROL,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == wintypes.HANDLE(-1).value or handle == 0:
        err = ctypes.get_last_error()
        raise PhysicalRootError(
            f"CreateFileW(OPEN_REPARSE_POINT) falló en '{lexical_path}': código Win32 {err} "
            "(directorio inexistente o sin permisos de lectura de atributos)"
        )
    return int(handle), lexical_path


def _open_bound_physical_root(root: pathlib.Path | str) -> tuple[int, str]:
    """Abre de forma segura el directorio raíz SIN conceder FILE_SHARE_DELETE y validando ancestros.

    Al omitir FILE_SHARE_DELETE y solicitar FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | READ_CONTROL,
    el kernel Win32 prohíbe incondicionalmente cualquier intento concurrente de renombrar
    o eliminar el directorio raíz durante la medición (anti-TOCTOU, error Win32 32 ERROR_SHARING_VIOLATION).
    """
    _ensure_windows()
    lexical_path = os.path.abspath(os.fspath(root))
    _validate_no_ancestor_reparse(lexical_path)

    handle = _kernel32.CreateFileW(
        lexical_path,
        _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _READ_CONTROL,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,  # ¡NO _FILE_SHARE_DELETE!
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == wintypes.HANDLE(-1).value or handle == 0:
        err = ctypes.get_last_error()
        raise PhysicalRootError(
            f"CreateFileW(OPEN_BOUND_ROOT) falló en '{lexical_path}': código Win32 {err} "
            "(directorio inexistente o sin permisos de listado/atributos)"
        )
    return int(handle), lexical_path


def derive_physical_root(root: pathlib.Path | str) -> PhysicalRootIdentity:
    """Abre la raíz de forma segura y deriva su identidad física (para OBSERVE / TOFU).

    No valida contra ningún ID previo recibido del request/staging: deriva la
    tupla (canonical_root, VolumeSerialNumber, root_file_id) directamente del
    kernel para ser emitida como evidencia observada.
    """
    handle, lexical_path = _open_physical_root(root)
    try:
        return _inspect_physical_root_handle(handle, lexical_path)
    finally:
        _kernel32.CloseHandle(ctypes.c_void_p(handle))


def _verify_identity_match(derived: PhysicalRootIdentity, expected: PhysicalRootIdentity) -> None:
    # 1. Comparación de canonical_root (case-insensitive en Windows)
    if derived.canonical_root.upper() != expected.canonical_root.upper():
        raise PhysicalRootMismatchError(
            f"canonical_root mismatch: esperado '{expected.canonical_root}', observado '{derived.canonical_root}'"
        )

    # 2. VolumeSerialNumber
    if derived.volume_serial_number != expected.volume_serial_number:
        raise PhysicalRootMismatchError(
            f"VolumeSerialNumber mismatch para '{derived.canonical_root}': "
            f"esperado={expected.volume_serial_number}, observado={derived.volume_serial_number}"
        )

    # 3. root_file_id
    if derived.root_file_id != expected.root_file_id:
        raise PhysicalRootMismatchError(
            f"root_file_id mismatch para '{derived.canonical_root}': "
            f"esperado={expected.root_file_id}, observado={derived.root_file_id}"
        )


def verify_physical_root(
    root: pathlib.Path | str,
    expected: PhysicalRootIdentity,
) -> PhysicalRootIdentity:
    """Abre la raíz de forma segura fresca y exige coincidencia exacta con la expectativa autorizada (para VERIFY).

    Cualquier discrepancia en canonical_root, VolumeSerialNumber o root_file_id
    produce :class:`PhysicalRootMismatchError` fail-closed.
    """
    if not isinstance(expected, PhysicalRootIdentity):
        raise PhysicalRootMismatchError(f"expected debe ser PhysicalRootIdentity; observado {type(expected).__name__}")

    derived = derive_physical_root(root)
    _verify_identity_match(derived, expected)
    return derived


@contextlib.contextmanager
def bound_physical_root(
    root: pathlib.Path | str,
    expected: PhysicalRootIdentity | None = None,
) -> Iterator[PhysicalRootIdentity]:
    """Abre y sostiene un handle sobre la raíz durante la medición, protegiendo contra TOCTOU.

    1. Valida que ningún ancestro léxico sea un reparse point / junction.
    2. Abre handle-first con OPEN_REPARSE_POINT SIN FILE_SHARE_DELETE (inhibe renombre/borrado en el SO).
    3. Valida la identidad física inicial (y contra expected si se proveyó).
    4. Sostiene el handle abierto durante el bloque para inhibir renombres/borrados de directorio.
    5. Al salir, ejecuta un sandwich recheck:
       - Re-inspecciona el handle sostenido.
       - Abre un handle fresco sobre la ruta y verifica que la identidad física sea idéntica.
    """
    handle, lexical_path = _open_bound_physical_root(root)
    try:
        initial_id = _inspect_physical_root_handle(handle, lexical_path)
        if expected is not None:
            _verify_identity_match(initial_id, expected)

        yield initial_id

        # Sandwich recheck:
        held_id = _inspect_physical_root_handle(handle, lexical_path)
        if held_id != initial_id:
            raise PhysicalRootMismatchError(
                f"TOCTOU detectado: la raíz sostenida mutó durante la medición (inicial={initial_id}, final={held_id})"
            )
        fresh_id = derive_physical_root(root)
        if fresh_id != initial_id:
            raise PhysicalRootMismatchError(
                f"TOCTOU detectado: la raíz viva fue sustituida durante la medición (inicial={initial_id}, fresco={fresh_id})"
            )
    finally:
        _kernel32.CloseHandle(ctypes.c_void_p(handle))
