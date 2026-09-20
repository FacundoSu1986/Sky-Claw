"""Constructor de Target DACL y primitivas de aplicación y restauración atadas a HANDLE (GP2-S1b).

Implementa el contrato normativo de ADR 0010 §7, §9, §10 y §12.2 (docs/adr/0010-runtime-vault-golden-protection-apply.md).

Principios normativos:
- HANDLE > PATHNAME: toda mutación y restauración opera exclusivamente sobre un HANDLE
  abierto previamente, impidiendo ataques de sustitución/TOCTOU sobre el namespace.
- ALLOWLIST ESTRICTA: exactamente 4 ACEs canónicas (Owner Rights S-1-3-4, Authenticated
  Users S-1-5-11, LocalSystem S-1-5-18, Builtin Administrators S-1-5-32-544).
- SIN ACES DENY: seguridad basada en exclusión explícita sin herencia (flags 0x00).
- OWNER RIGHTS ACE: suprime la concesión implícita de WRITE_DAC al propietario del objeto.
- SE_DACL_PROTECTED: aislamiento contra propagación de herencia de ancestros.
- PRESERVE PRE & SEMANTIC RESTORE: restauración autoritativa desde NodeSecurityBackup
  preservando el estado protegido/desprotegido original según los bytes autoritativos
  y verificación post-restauración semántica exhaustiva (owner, group, DACL ordenada,
  SE_DACL_PROTECTED), sin exigir igualdad raw de bytes reserializados por el SO.
- REVALIDACIONES PRE-MUTACIÓN: enlace físico (VolumeSerialNumber, FileId), rechazo
  de reparse points (ReparseTag != 0), comprobación anti-hardlink (NumberOfLinks == 1
  para archivos, ejecutada dos veces para cerrar la ventana TOCTOU) y coincidencia
  del SHA-256 del Security Descriptor vivo con el backup.
- MEMORY OWNERSHIP: auditoría estricta de asignaciones Win32 (LocalFree para PSIDs y SDs,
  cierre garantizado de handles, sin liberar punteros prestados de Security Descriptors).
- POSIX SAFETY: import seguro en entornos no-Windows con error explícito en entrypoints nativos.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import pathlib
import sys
from dataclasses import dataclass
from typing import Any

from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.protection import GoldenProtectionRight

# ============================================================================
# Jerarquía de Excepciones de Dominio
# ============================================================================


class TargetDaclError(RuntimeVaultError):
    """Base de las excepciones de dominio para Target DACL."""


class TargetDaclUnsupportedError(TargetDaclError):
    """Plataforma no Windows o subsistema incompatible."""


class TargetDaclBuildError(TargetDaclError):
    """Fallo en la construcción o validación de la Target DACL."""


class TargetDaclApplyError(TargetDaclError):
    """Fallo al aplicar la Target DACL sobre el handle del nodo."""


class TargetDaclRestoreError(TargetDaclError):
    """Fallo al restaurar el Security Descriptor PRE sobre el handle."""


class TargetDaclVerificationError(TargetDaclError):
    """Fallo de verificación de seguridad tras aplicar o restaurar la Target DACL."""


# ============================================================================
# Constantes Normativas (ADR 0010 §7)
# ============================================================================

OWNER_RIGHTS_SID = "S-1-3-4"
AUTHENTICATED_USERS_SID = "S-1-5-11"
LOCAL_SYSTEM_SID = "S-1-5-18"
BUILTIN_ADMINISTRATORS_SID = "S-1-5-32-544"

# Win32 Access Masks Nominales (ADR 0010 §7.2)
# File:
# 1. Owner Rights: FILE_GENERIC_READ
#    (FILE_READ_DATA | FILE_READ_ATTRIBUTES | FILE_READ_EA | READ_CONTROL | SYNCHRONIZE)
FILE_TARGET_MASK_OWNER_RIGHTS = 0x00120089

# 2. Authenticated Users: FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
#    (agrega FILE_EXECUTE para binarios/DLLs del juego)
FILE_TARGET_MASK_AUTHENTICATED_USERS = 0x001200A9

# Directory:
# 1. Owner Rights: FILE_GENERIC_READ | FILE_TRAVERSE
#    (FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | FILE_READ_EA | FILE_TRAVERSE | READ_CONTROL | SYNCHRONIZE)
DIR_TARGET_MASK_OWNER_RIGHTS = 0x001200A9

# 2. Authenticated Users: FILE_GENERIC_READ | FILE_TRAVERSE
DIR_TARGET_MASK_AUTHENTICATED_USERS = 0x001200A9

# LocalSystem y Administrators: FILE_ALL_ACCESS
TARGET_MASK_FULL_ACCESS = 0x001F01FF

# Win32 Generic Mapping Constants
# FILE_GENERIC_READ = STANDARD_RIGHTS_READ | FILE_READ_DATA | FILE_READ_ATTRIBUTES | FILE_READ_EA | SYNCHRONIZE
FILE_GENERIC_READ = 0x00120089
# FILE_GENERIC_WRITE = STANDARD_RIGHTS_WRITE | FILE_WRITE_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | FILE_APPEND_DATA | SYNCHRONIZE
FILE_GENERIC_WRITE = 0x00120116
# FILE_GENERIC_EXECUTE = STANDARD_RIGHTS_EXECUTE | FILE_READ_ATTRIBUTES | FILE_EXECUTE | SYNCHRONIZE
FILE_GENERIC_EXECUTE = 0x001200A0

# Directory Generic Mappings
DIR_GENERIC_READ = 0x00120089
DIR_GENERIC_WRITE = 0x00120116
DIR_GENERIC_EXECUTE = 0x001200A0

# Win32 Constants
_ACCESS_ALLOWED_ACE_TYPE = 0x00
_NO_INHERITANCE_ACE_FLAGS = 0x00
_SE_DACL_PROTECTED = 0x1000

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_GROUP_SECURITY_INFORMATION = 0x00000002
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000

# Access rights para handle de seguridad (ADR 0010 §2.3 / §4.1)
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_FILE_READ_ATTRIBUTES = 0x0080

_SECURITY_HANDLE_DESIRED_ACCESS = _READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _FILE_READ_ATTRIBUTES
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_ACL_REVISION = 2

# Clases para GetFileInformationByHandleEx
_FILE_INFO_BY_HANDLE_CLASS_STANDARD = 1
_FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG = 9
_FILE_INFO_BY_HANDLE_CLASS_ID = 18

# Token constants
_DISABLE_MAX_PRIVILEGE = 0x1
_TOKEN_QUERY = 0x0008
_TOKEN_DUPLICATE = 0x0002
_TOKEN_IMPERSONATE = 0x0004
_SECURITY_IMPERSONATION = 2
_TOKEN_IMPERSONATION = 2

# Win32 Error Codes
_ERROR_SUCCESS = 0
_ERROR_ACCESS_DENIED = 5
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_NO_TOKEN = 1008


# ============================================================================
# DTOs Puros (Cross-Platform)
# ============================================================================


@dataclass(frozen=True, slots=True)
class TargetAceSpec:
    """Especificación canónica inmutable de una entrada de control de acceso (ACE)."""

    sid: str
    ace_type: int
    ace_flags: int
    access_mask: int
    name: str


@dataclass(frozen=True, slots=True)
class TargetDaclSpec:
    """Especificación de Target DACL para un tipo de nodo (ADR 0010 §7.1 / §7.2)."""

    node_kind: GoldenProtectionNodeKind
    aces: tuple[TargetAceSpec, ...]
    control_flags: int

    def __post_init__(self) -> None:
        if len(self.aces) != 4:
            raise TargetDaclBuildError(f"TargetDaclSpec requiere exactamente 4 ACEs, recibidas {len(self.aces)}")
        if not (self.control_flags & _SE_DACL_PROTECTED):
            raise TargetDaclBuildError("TargetDaclSpec debe incluir el flag SE_DACL_PROTECTED (0x1000)")


@dataclass(frozen=True, slots=True)
class TargetDaclVerificationResult:
    """Resultado estructurado de la verificación de Target DACL sobre un handle."""

    node_kind: GoldenProtectionNodeKind
    dacl_protected: bool
    owner_rights_present: bool
    owner_rights_mask: int
    granted_rights: frozenset[GoldenProtectionRight]
    effective_access_mask: int


# ============================================================================
# Builder Puro de Especificación Target DACL
# ============================================================================


def build_target_dacl_spec(node_kind: GoldenProtectionNodeKind) -> TargetDaclSpec:
    """Construye la especificación canónica inmutable de Target DACL para un tipo de nodo.

    Reglas ADR 0010 §7:
    - Orden canónico estricto de las 4 ACEs:
      1. Owner Rights (S-1-3-4) -> Concesión restringida al owner (sin WRITE_DAC).
      2. Authenticated Users (S-1-5-11) -> Concesión de lectura / ejecución.
      3. LocalSystem (S-1-5-18) -> FILE_ALL_ACCESS.
      4. Builtin Administrators (S-1-5-32-544) -> FILE_ALL_ACCESS.
    - SE_DACL_PROTECTED activado en control_flags.
    """
    if node_kind is GoldenProtectionNodeKind.FILE:
        aces = (
            TargetAceSpec(
                sid=OWNER_RIGHTS_SID,
                ace_type=_ACCESS_ALLOWED_ACE_TYPE,
                ace_flags=_NO_INHERITANCE_ACE_FLAGS,
                access_mask=FILE_TARGET_MASK_OWNER_RIGHTS,
                name="Owner Rights",
            ),
            TargetAceSpec(
                sid=AUTHENTICATED_USERS_SID,
                ace_type=_ACCESS_ALLOWED_ACE_TYPE,
                ace_flags=_NO_INHERITANCE_ACE_FLAGS,
                access_mask=FILE_TARGET_MASK_AUTHENTICATED_USERS,
                name="Authenticated Users",
            ),
            TargetAceSpec(
                sid=LOCAL_SYSTEM_SID,
                ace_type=_ACCESS_ALLOWED_ACE_TYPE,
                ace_flags=_NO_INHERITANCE_ACE_FLAGS,
                access_mask=TARGET_MASK_FULL_ACCESS,
                name="LocalSystem",
            ),
            TargetAceSpec(
                sid=BUILTIN_ADMINISTRATORS_SID,
                ace_type=_ACCESS_ALLOWED_ACE_TYPE,
                ace_flags=_NO_INHERITANCE_ACE_FLAGS,
                access_mask=TARGET_MASK_FULL_ACCESS,
                name="Builtin Administrators",
            ),
        )
    elif node_kind is GoldenProtectionNodeKind.DIR:
        aces = (
            TargetAceSpec(
                sid=OWNER_RIGHTS_SID,
                ace_type=_ACCESS_ALLOWED_ACE_TYPE,
                ace_flags=_NO_INHERITANCE_ACE_FLAGS,
                access_mask=DIR_TARGET_MASK_OWNER_RIGHTS,
                name="Owner Rights",
            ),
            TargetAceSpec(
                sid=AUTHENTICATED_USERS_SID,
                ace_type=_ACCESS_ALLOWED_ACE_TYPE,
                ace_flags=_NO_INHERITANCE_ACE_FLAGS,
                access_mask=DIR_TARGET_MASK_AUTHENTICATED_USERS,
                name="Authenticated Users",
            ),
            TargetAceSpec(
                sid=LOCAL_SYSTEM_SID,
                ace_type=_ACCESS_ALLOWED_ACE_TYPE,
                ace_flags=_NO_INHERITANCE_ACE_FLAGS,
                access_mask=TARGET_MASK_FULL_ACCESS,
                name="LocalSystem",
            ),
            TargetAceSpec(
                sid=BUILTIN_ADMINISTRATORS_SID,
                ace_type=_ACCESS_ALLOWED_ACE_TYPE,
                ace_flags=_NO_INHERITANCE_ACE_FLAGS,
                access_mask=TARGET_MASK_FULL_ACCESS,
                name="Builtin Administrators",
            ),
        )
    else:
        raise TargetDaclBuildError(f"Tipo de nodo desconocido: {node_kind}")

    return TargetDaclSpec(
        node_kind=node_kind,
        aces=aces,
        control_flags=_SE_DACL_PROTECTED,
    )


# ============================================================================
# Declaraciones Win32 Ctypes (Windows Only)
# ============================================================================

if sys.platform == "win32":
    from ctypes import wintypes

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        ]

    class _FileStandardInfo(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", ctypes.c_bool),
            ("Directory", ctypes.c_bool),
        ]

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    class _AclHeader(ctypes.Structure):
        _fields_ = [
            ("AclRevision", wintypes.BYTE),
            ("Sbz1", wintypes.BYTE),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class _GenericMapping(ctypes.Structure):
        _fields_ = [
            ("GenericRead", wintypes.DWORD),
            ("GenericWrite", wintypes.DWORD),
            ("GenericExecute", wintypes.DWORD),
            ("GenericAll", wintypes.DWORD),
        ]

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [
            ("Sid", wintypes.LPVOID),
            ("Attributes", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

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

    _kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    _kernel32.LocalFree.restype = wintypes.LPVOID

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentThread.restype = wintypes.HANDLE

    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL

    _advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL

    _advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    _advapi32.GetLengthSid.argtypes = [wintypes.LPVOID]
    _advapi32.GetLengthSid.restype = wintypes.DWORD

    _advapi32.InitializeAcl.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _advapi32.InitializeAcl.restype = wintypes.BOOL

    _advapi32.AddAccessAllowedAceEx.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    _advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL

    _advapi32.GetAclInformation.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    _advapi32.GetAclInformation.restype = wintypes.BOOL

    _advapi32.GetAce.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _advapi32.GetAce.restype = wintypes.BOOL

    _advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _advapi32.GetSecurityInfo.restype = wintypes.DWORD

    _advapi32.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _advapi32.SetSecurityInfo.restype = wintypes.DWORD

    _advapi32.GetSecurityDescriptorLength.argtypes = [wintypes.LPVOID]
    _advapi32.GetSecurityDescriptorLength.restype = wintypes.DWORD

    _advapi32.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL

    _advapi32.GetSecurityDescriptorOwner.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL

    _advapi32.GetSecurityDescriptorGroup.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorGroup.restype = wintypes.BOOL

    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL

    _advapi32.IsValidSecurityDescriptor.argtypes = [wintypes.LPVOID]
    _advapi32.IsValidSecurityDescriptor.restype = wintypes.BOOL

    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL

    _advapi32.OpenThreadToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.BOOL,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenThreadToken.restype = wintypes.BOOL

    _advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.DuplicateTokenEx.restype = wintypes.BOOL

    _advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SidAndAttributes),
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.CreateRestrictedToken.restype = wintypes.BOOL

    _advapi32.SetThreadToken.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.HANDLE,
    ]
    _advapi32.SetThreadToken.restype = wintypes.BOOL

    _advapi32.AccessCheck.argtypes = [
        wintypes.LPVOID,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(_GenericMapping),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.AccessCheck.restype = wintypes.BOOL


# ============================================================================
# Helpers Internos Win32
# ============================================================================


def _ensure_windows() -> None:
    """Verifica que el entorno de ejecución sea Windows."""
    if sys.platform != "win32":
        raise TargetDaclUnsupportedError("Operación nativa de Target DACL solo soportada en Windows")


def _read_file_id_info_by_handle(handle: int) -> tuple[int, int]:
    """Obtiene (VolumeSerialNumber, FileId) desde un HANDLE abierto mediante GetFileInformationByHandleEx."""
    _ensure_windows()
    info_id = _FileIdInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_INFO_BY_HANDLE_CLASS_ID,
        ctypes.byref(info_id),
        ctypes.sizeof(info_id),
    ):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"GetFileInformationByHandleEx(FileIdInfo) falló: código {err}")

    vol_serial = int(info_id.VolumeSerialNumber)
    raw_fid_bytes = bytes(info_id.FileId.Identifier)
    file_id = int.from_bytes(raw_fid_bytes, byteorder="little", signed=False)
    return vol_serial, file_id


def _read_number_of_links_by_handle(handle: int) -> int:
    """Obtiene NumberOfLinks desde un HANDLE abierto mediante GetFileInformationByHandleEx."""
    _ensure_windows()
    std_info = _FileStandardInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_INFO_BY_HANDLE_CLASS_STANDARD,
        ctypes.byref(std_info),
        ctypes.sizeof(std_info),
    ):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"GetFileInformationByHandleEx(FileStandardInfo) falló: código {err}")
    return int(std_info.NumberOfLinks)


def _read_reparse_tag_by_handle(handle: int) -> int:
    """Obtiene ReparseTag desde un HANDLE abierto mediante GetFileInformationByHandleEx."""
    _ensure_windows()
    tag_info = _FileAttributeTagInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG,
        ctypes.byref(tag_info),
        ctypes.sizeof(tag_info),
    ):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"GetFileInformationByHandleEx(FileAttributeTagInfo) falló: código {err}")
    return int(tag_info.ReparseTag)


def _extract_sd_components(
    sd_ptr: Any,
) -> tuple[str, str, bool, list[tuple[str, int, int, int]], bool]:
    """Extrae componentes semánticos autoritativos desde un puntero a SECURITY_DESCRIPTOR válido.

    Devuelve:
    (owner_sid, group_sid, dacl_present, ordered_aces, is_dacl_protected)
    donde ordered_aces es una lista de tuplas (sid_str, ace_type, ace_flags, access_mask).
    """
    _ensure_windows()
    if not _advapi32.IsValidSecurityDescriptor(sd_ptr):
        raise TargetDaclError("Puntero no contiene un SECURITY_DESCRIPTOR válido")

    control = wintypes.WORD()
    rev = wintypes.DWORD()
    if not _advapi32.GetSecurityDescriptorControl(sd_ptr, ctypes.byref(control), ctypes.byref(rev)):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"GetSecurityDescriptorControl falló: código {err}")
    is_protected = bool(control.value & _SE_DACL_PROTECTED)

    owner_p = wintypes.LPVOID()
    owner_def = wintypes.BOOL()
    if (
        not _advapi32.GetSecurityDescriptorOwner(sd_ptr, ctypes.byref(owner_p), ctypes.byref(owner_def))
        or not owner_p.value
    ):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"GetSecurityDescriptorOwner falló o no hay owner: código {err}")
    owner_sid_str_p = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(owner_p, ctypes.byref(owner_sid_str_p)):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"ConvertSidToStringSidW falló para owner: código {err}")
    owner_sid = owner_sid_str_p.value or ""
    _kernel32.LocalFree(owner_sid_str_p)

    group_p = wintypes.LPVOID()
    group_def = wintypes.BOOL()
    if (
        not _advapi32.GetSecurityDescriptorGroup(sd_ptr, ctypes.byref(group_p), ctypes.byref(group_def))
        or not group_p.value
    ):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"GetSecurityDescriptorGroup falló o no hay group: código {err}")
    group_sid_str_p = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(group_p, ctypes.byref(group_sid_str_p)):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"ConvertSidToStringSidW falló para group: código {err}")
    group_sid = group_sid_str_p.value or ""
    _kernel32.LocalFree(group_sid_str_p)

    dacl_present = wintypes.BOOL()
    dacl_defaulted = wintypes.BOOL()
    dacl_p = wintypes.LPVOID()
    if not _advapi32.GetSecurityDescriptorDacl(
        sd_ptr, ctypes.byref(dacl_present), ctypes.byref(dacl_p), ctypes.byref(dacl_defaulted)
    ):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"GetSecurityDescriptorDacl falló: código {err}")

    aces: list[tuple[str, int, int, int]] = []
    has_dacl = bool(dacl_present.value and dacl_p.value)
    if has_dacl:
        acl_info = _AclSizeInformation()
        if not _advapi32.GetAclInformation(dacl_p, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2):
            err = ctypes.get_last_error()
            raise TargetDaclError(f"GetAclInformation falló: código {err}")

        for i in range(acl_info.AceCount):
            ace_ptr = wintypes.LPVOID()
            if not _advapi32.GetAce(dacl_p, i, ctypes.byref(ace_ptr)) or not ace_ptr.value:
                err = ctypes.get_last_error()
                raise TargetDaclError(f"GetAce({i}) falló: código {err}")

            header = ctypes.cast(ace_ptr, ctypes.POINTER(_AceHeader)).contents
            raw_ace = ctypes.cast(ace_ptr, ctypes.c_void_p).value
            if not raw_ace:
                continue
            mask_val = ctypes.cast(raw_ace + 4, ctypes.POINTER(wintypes.DWORD)).contents.value
            sid_ptr = wintypes.LPVOID(raw_ace + 8)

            sid_str_p = wintypes.LPWSTR()
            if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str_p)):
                err = ctypes.get_last_error()
                raise TargetDaclError(f"ConvertSidToStringSidW falló para ACE #{i}: código {err}")
            ace_sid = sid_str_p.value or ""
            _kernel32.LocalFree(sid_str_p)

            aces.append((ace_sid, int(header.AceType), int(header.AceFlags), int(mask_val)))

    return owner_sid, group_sid, has_dacl, aces, is_protected


# ============================================================================
# Primitiva: Constructor Nativo PACL Win32
# ============================================================================


def build_native_target_dacl(spec: TargetDaclSpec) -> tuple[Any, Any]:
    """Construye un buffer de ACL Win32 nativo en memoria a partir de TargetDaclSpec.

    MEMORY OWNERSHIP:
    - Cada PSID obtenido mediante ConvertStringSidToSidW es liberado de forma estricta
      vía LocalFree en un bloque finally tras ser añadido al buffer PACL.
    - AddAccessAllowedAceEx copia los bytes del SID en el buffer del PACL, por lo
      que no retiene punteros a las estructuras temporales.
    - El buffer del PACL es gestionado como arreglo ctypes en memoria de Python y
      su puntero permanece válido mientras el buffer de retorno sea retenido.
    """
    _ensure_windows()

    psids: list[Any] = []
    total_sid_bytes = 0

    try:
        for ace in spec.aces:
            psid = wintypes.LPVOID()
            if not _advapi32.ConvertStringSidToSidW(ace.sid, ctypes.byref(psid)):
                err = ctypes.get_last_error()
                raise TargetDaclBuildError(f"ConvertStringSidToSidW falló para '{ace.sid}': código {err}")
            psids.append(psid)
            total_sid_bytes += _advapi32.GetLengthSid(psid)

        acl_size = ctypes.sizeof(_AclHeader) + len(spec.aces) * 8 + total_sid_bytes + 64
        acl_buffer = (ctypes.c_ubyte * acl_size)()
        pacl = ctypes.cast(acl_buffer, wintypes.LPVOID)

        if not _advapi32.InitializeAcl(pacl, acl_size, _ACL_REVISION):
            err = ctypes.get_last_error()
            raise TargetDaclBuildError(f"InitializeAcl falló: código {err}")

        for i, ace in enumerate(spec.aces):
            if not _advapi32.AddAccessAllowedAceEx(
                pacl,
                _ACL_REVISION,
                ace.ace_flags,
                ace.access_mask,
                psids[i],
            ):
                err = ctypes.get_last_error()
                raise TargetDaclBuildError(
                    f"AddAccessAllowedAceEx falló para ACE '{ace.name}' ({ace.sid}): código {err}"
                )

        return acl_buffer, pacl
    finally:
        for psid in psids:
            if ctypes.cast(psid, ctypes.c_void_p).value:
                _kernel32.LocalFree(psid)


# ============================================================================
# Primitiva: Apertura de Handle de Seguridad (PRE-Hardening)
# ============================================================================


def open_node_security_handle(path: pathlib.Path | str | os.PathLike[str]) -> int:
    """Abre un HANDLE Win32 con derechos de mutación y restauración ANTES del hardening.

    SECURITY RULE (ADR 0010 §2.3 / §4.1 / §12.2):
    El handle debe poseer READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES
    y abrirse antes de que la Target DACL neutralice los derechos implícitos del owner.
    Usa FILE_FLAG_BACKUP_SEMANTICS y FILE_FLAG_OPEN_REPARSE_POINT con FILE_SHARE_READ.
    """
    _ensure_windows()

    h = _kernel32.CreateFileW(
        str(path),
        _SECURITY_HANDLE_DESIRED_ACCESS,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if h == wintypes.HANDLE(-1).value or h == 0:
        err = ctypes.get_last_error()
        raise TargetDaclApplyError(f"CreateFileW falló al abrir '{path}' para seguridad: código {err}")
    return int(h)


def close_security_handle(handle: int) -> None:
    """Cierra de forma segura un HANDLE Win32."""
    _ensure_windows()
    if handle and handle != wintypes.HANDLE(-1).value:
        _kernel32.CloseHandle(handle)


# ============================================================================
# Primitiva: Aplicación de Target DACL Atada a HANDLE
# ============================================================================


def apply_target_dacl_by_handle(
    handle: int,
    backup: NodeSecurityBackup,
) -> TargetDaclSpec:
    """Aplica la Target DACL sobre el HANDLE Win32 abierto con SE_DACL_PROTECTED.

    SECURITY RULES (ADR 0010 §12.2 pasos 2 a 5):
    1. Revalida la identidad física (VolumeSerialNumber, FileId).
    2. Rechaza reparse points (ReparseTag != 0) fail-closed.
    3. Revalida NumberOfLinks == 1 para archivos regulares (paso 1).
    4. Lee y revalida el live PRE Security Descriptor contra backup.pre_sd_sha256.
    5. Construye la Target DACL canónica para backup.node_kind.
    6. Revalida NumberOfLinks == 1 OTRA VEZ inmediatamente antes de SetSecurityInfo
       para cerrar la ventana TOCTOU entre la lectura PRE y la mutación.
    7. Aplica SetSecurityInfo con DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
    """
    _ensure_windows()

    try:
        # 1. Identidad física
        vol_serial, file_id = _read_file_id_info_by_handle(handle)
        if vol_serial != backup.volume_serial_number or file_id != backup.file_id:
            raise TargetDaclApplyError(
                f"Drift de identidad física: esperado=({backup.volume_serial_number}, {backup.file_id}), "
                f"observado=({vol_serial}, {file_id})"
            )

        # 2. Rechazar reparse points
        reparse_tag = _read_reparse_tag_by_handle(handle)
        if reparse_tag != 0:
            raise TargetDaclApplyError(
                f"El nodo es un reparse point / symlink (ReparseTag=0x{reparse_tag:08X}); mutación rehusada fail-closed"
            )

        # 3. Comprobación anti-hardlink inicial (solo archivos)
        if backup.node_kind is GoldenProtectionNodeKind.FILE:
            links_initial = _read_number_of_links_by_handle(handle)
            if links_initial != 1:
                raise TargetDaclApplyError(
                    f"Hardlink externo detectado en verificación inicial: NumberOfLinks={links_initial} != 1"
                )

        # 4. Leer y revalidar live PRE SD contra el backup
        live_sd_p = wintypes.LPVOID()
        ret_get = _advapi32.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _GROUP_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(live_sd_p),
        )
        if ret_get != _ERROR_SUCCESS:
            raise TargetDaclApplyError(f"GetSecurityInfo falló al leer live PRE SD: código {ret_get}")

        try:
            live_len = _advapi32.GetSecurityDescriptorLength(live_sd_p)
            if live_len <= 0:
                raise TargetDaclApplyError("GetSecurityDescriptorLength devolvió longitud inválida para live PRE SD")
            live_bytes = ctypes.string_at(live_sd_p, live_len)
            live_sha = hashlib.sha256(live_bytes).hexdigest()
            if live_sha != backup.pre_sd_sha256:
                raise TargetDaclApplyError(
                    f"Drift de live PRE SD antes de mutar: hash esperado={backup.pre_sd_sha256}, observado={live_sha}"
                )
        finally:
            if ctypes.cast(live_sd_p, ctypes.c_void_p).value:
                _kernel32.LocalFree(live_sd_p)

        # 5. Construir Target DACL
        spec = build_target_dacl_spec(backup.node_kind)
        acl_buffer, pacl = build_native_target_dacl(spec)

        # 6. Revalidación anti-hardlink INMEDIATA antes de mutar (cierra ventana TOCTOU)
        if backup.node_kind is GoldenProtectionNodeKind.FILE:
            links_final = _read_number_of_links_by_handle(handle)
            if links_final != 1:
                raise TargetDaclApplyError(
                    f"Hardlink externo detectado en revalidación pre-mutación: NumberOfLinks={links_final} != 1"
                )

        # 7. Aplicar Target DACL
        ret = _advapi32.SetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            pacl,
            None,
        )
        if ret != _ERROR_SUCCESS:
            raise TargetDaclApplyError(f"SetSecurityInfo falló al aplicar Target DACL: código {ret}")

        return spec
    except (TargetDaclApplyError, TargetDaclBuildError):
        raise
    except TargetDaclError as exc:
        raise TargetDaclApplyError(str(exc)) from exc


# ============================================================================
# Primitiva: Verificación Causal de Target DACL Atada a HANDLE
# ============================================================================


def verify_target_dacl_by_handle(
    handle: int,
    backup: NodeSecurityBackup,
    *,
    token_handle: Any = None,
) -> TargetDaclVerificationResult:
    """Verifica causalmente sobre el HANDLE la Target DACL completa aplicada (ADR 0010 §12.2 paso 6).

    Comprueba:
    1. SE_DACL_PROTECTED activado en los bits de control del descriptor.
    2. Owner SID y Group SID coinciden con los esperados en backup.
    3. DACL contiene exactamente las 4 ACEs requeridas en orden canónico estricto
       (SID, AceType, AceFlags y AccessMask exactos para cada entrada).
    4. Evaluación real de AccessCheck sobre el descriptor contra el token provisto
       o el token efectivo (con fallback a process token ÚNICAMENTE con ERROR_NO_TOKEN).
    """
    _ensure_windows()

    sd_p = wintypes.LPVOID()
    owner_p = wintypes.LPVOID()
    group_p = wintypes.LPVOID()
    dacl_p = wintypes.LPVOID()

    ret = _advapi32.GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _GROUP_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner_p),
        ctypes.byref(group_p),
        ctypes.byref(dacl_p),
        None,
        ctypes.byref(sd_p),
    )
    if ret != _ERROR_SUCCESS:
        raise TargetDaclVerificationError(f"GetSecurityInfo falló en verificación: código {ret}")

    token_to_close: Any = None

    try:
        # 1. Comprobar SE_DACL_PROTECTED
        control = wintypes.WORD()
        rev = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(sd_p, ctypes.byref(control), ctypes.byref(rev)):
            err = ctypes.get_last_error()
            raise TargetDaclVerificationError(f"GetSecurityDescriptorControl falló: código {err}")

        dacl_protected = bool(control.value & _SE_DACL_PROTECTED)
        if not dacl_protected:
            raise TargetDaclVerificationError("Target DACL no posee el flag SE_DACL_PROTECTED")

        # 2. Validar Owner y Group invariantes
        owner_sid_str_p = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(owner_p, ctypes.byref(owner_sid_str_p)):
            err = ctypes.get_last_error()
            raise TargetDaclVerificationError(f"ConvertSidToStringSidW falló para post owner: código {err}")
        post_owner = owner_sid_str_p.value or ""
        _kernel32.LocalFree(owner_sid_str_p)

        if post_owner != backup.owner_sid:
            raise TargetDaclVerificationError(
                f"Owner post-apply ({post_owner}) no coincide con esperado ({backup.owner_sid})"
            )

        group_sid_str_p = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(group_p, ctypes.byref(group_sid_str_p)):
            err = ctypes.get_last_error()
            raise TargetDaclVerificationError(f"ConvertSidToStringSidW falló para post group: código {err}")
        post_group = group_sid_str_p.value or ""
        _kernel32.LocalFree(group_sid_str_p)

        if post_group != backup.group_sid:
            raise TargetDaclVerificationError(
                f"Group post-apply ({post_group}) no coincide con esperado ({backup.group_sid})"
            )

        # 3. Validar presencia de DACL y conteo exacto de 4 ACEs
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl_out = wintypes.LPVOID()
        if not _advapi32.GetSecurityDescriptorDacl(
            sd_p,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl_out),
            ctypes.byref(dacl_defaulted),
        ):
            err = ctypes.get_last_error()
            raise TargetDaclVerificationError(f"GetSecurityDescriptorDacl falló: código {err}")

        if not dacl_present.value or not dacl_out.value:
            raise TargetDaclVerificationError("Security Descriptor no contiene DACL presente")

        acl_info = _AclSizeInformation()
        if not _advapi32.GetAclInformation(dacl_out, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2):
            err = ctypes.get_last_error()
            raise TargetDaclVerificationError(f"GetAclInformation falló: código {err}")

        expected_spec = build_target_dacl_spec(backup.node_kind)
        if acl_info.AceCount != len(expected_spec.aces):
            raise TargetDaclVerificationError(
                f"DACL contiene {acl_info.AceCount} ACEs, se requieren exactamente {len(expected_spec.aces)}"
            )

        owner_rights_found = False
        owner_rights_mask = 0

        # Validar cada una de las 4 ACEs en orden canónico exacto
        for i, expected_ace in enumerate(expected_spec.aces):
            ace_ptr = wintypes.LPVOID()
            if not _advapi32.GetAce(dacl_out, i, ctypes.byref(ace_ptr)) or not ace_ptr.value:
                err = ctypes.get_last_error()
                raise TargetDaclVerificationError(f"GetAce({i}) falló: código {err}")

            header = ctypes.cast(ace_ptr, ctypes.POINTER(_AceHeader)).contents
            raw_ace = ctypes.cast(ace_ptr, ctypes.c_void_p).value
            if not raw_ace:
                raise TargetDaclVerificationError(f"Puntero crudo nulo en ACE #{i + 1}")

            mask_val = ctypes.cast(raw_ace + 4, ctypes.POINTER(wintypes.DWORD)).contents.value
            sid_ptr = wintypes.LPVOID(raw_ace + 8)

            sid_str_p = wintypes.LPWSTR()
            if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str_p)):
                err = ctypes.get_last_error()
                raise TargetDaclVerificationError(f"ConvertSidToStringSidW falló en ACE #{i + 1}: código {err}")
            sid_str = sid_str_p.value or ""
            _kernel32.LocalFree(sid_str_p)

            # Comparación canónica exacta
            if sid_str != expected_ace.sid:
                raise TargetDaclVerificationError(
                    f"ACE #{i + 1} SID discordante: esperado={expected_ace.sid} ({expected_ace.name}), observado={sid_str}"
                )
            if header.AceType != expected_ace.ace_type:
                raise TargetDaclVerificationError(
                    f"ACE #{i + 1} AceType discordante: esperado={expected_ace.ace_type}, observado={header.AceType}"
                )
            if header.AceFlags != expected_ace.ace_flags:
                raise TargetDaclVerificationError(
                    f"ACE #{i + 1} AceFlags discordante: esperado=0x{expected_ace.ace_flags:02X}, observado=0x{header.AceFlags:02X}"
                )
            if mask_val != expected_ace.access_mask:
                raise TargetDaclVerificationError(
                    f"ACE #{i + 1} AccessMask discordante: esperado=0x{expected_ace.access_mask:08X}, observado=0x{mask_val:08X}"
                )

            if sid_str == OWNER_RIGHTS_SID:
                owner_rights_found = True
                owner_rights_mask = mask_val

        if not owner_rights_found:
            raise TargetDaclVerificationError("ACE para Owner Rights (S-1-3-4) no encontrada en la DACL")

        # Comprobar que Owner Rights no concede derechos peligrosos
        if bool(owner_rights_mask & _WRITE_DAC):
            raise TargetDaclVerificationError("ACE Owner Rights concede WRITE_DAC inadmisible")
        if bool(owner_rights_mask & _WRITE_OWNER):
            raise TargetDaclVerificationError("ACE Owner Rights concede WRITE_OWNER inadmisible")
        if owner_rights_mask == TARGET_MASK_FULL_ACCESS:
            raise TargetDaclVerificationError("ACE Owner Rights posee FILE_ALL_ACCESS inadmisible")

        # 4. AccessCheck nativo
        token_eval = token_handle
        if token_eval is None:
            token_raw = wintypes.HANDLE()
            token_query_dup = _TOKEN_QUERY | _TOKEN_DUPLICATE
            res = _advapi32.OpenThreadToken(
                _kernel32.GetCurrentThread(), token_query_dup, True, ctypes.byref(token_raw)
            )
            if not res:
                err = ctypes.get_last_error()
                # Fail-closed estricto: fallback a proceso ÚNICAMENTE si no hay token en el thread (ERROR_NO_TOKEN 1008)
                if err == _ERROR_NO_TOKEN:
                    res_proc = _advapi32.OpenProcessToken(
                        _kernel32.GetCurrentProcess(), token_query_dup, ctypes.byref(token_raw)
                    )
                    if not res_proc:
                        p_err = ctypes.get_last_error()
                        raise TargetDaclVerificationError(f"OpenProcessToken falló tras ERROR_NO_TOKEN: código {p_err}")
                else:
                    raise TargetDaclVerificationError(
                        f"OpenThreadToken falló con código {err} != ERROR_NO_TOKEN ({_ERROR_NO_TOKEN}); "
                        "rehusando fallback a proceso"
                    )

            token_imp = wintypes.HANDLE()
            res_dup = _advapi32.DuplicateTokenEx(
                token_raw,
                _TOKEN_QUERY,
                None,
                _SECURITY_IMPERSONATION,
                _TOKEN_IMPERSONATION,
                ctypes.byref(token_imp),
            )
            _kernel32.CloseHandle(token_raw)
            if not res_dup:
                err = ctypes.get_last_error()
                raise TargetDaclVerificationError(f"DuplicateTokenEx falló: código {err}")
            token_eval = token_imp
            token_to_close = token_imp

        mapping = _GenericMapping()
        if backup.node_kind is GoldenProtectionNodeKind.FILE:
            mapping.GenericRead = FILE_GENERIC_READ
            mapping.GenericWrite = FILE_GENERIC_WRITE
            mapping.GenericExecute = FILE_GENERIC_EXECUTE
            mapping.GenericAll = TARGET_MASK_FULL_ACCESS
        else:
            mapping.GenericRead = DIR_GENERIC_READ
            mapping.GenericWrite = DIR_GENERIC_WRITE
            mapping.GenericExecute = DIR_GENERIC_EXECUTE
            mapping.GenericAll = TARGET_MASK_FULL_ACCESS

        # Asignación dinámica de PRIVILEGE_SET para AccessCheck (reintentos acotados ante buffer insuficiente)
        priv_set_len = wintypes.DWORD(256)
        priv_set_buf = ctypes.create_string_buffer(priv_set_len.value)
        granted_mask = wintypes.DWORD()
        access_status = wintypes.BOOL()
        desired_access = 0x02000000  # MAXIMUM_ALLOWED

        chk_res = _advapi32.AccessCheck(
            sd_p,
            token_eval,
            desired_access,
            ctypes.byref(mapping),
            priv_set_buf,
            ctypes.byref(priv_set_len),
            ctypes.byref(granted_mask),
            ctypes.byref(access_status),
        )
        if not chk_res:
            err = ctypes.get_last_error()
            if err == _ERROR_INSUFFICIENT_BUFFER:
                priv_set_buf = ctypes.create_string_buffer(priv_set_len.value)
                chk_res = _advapi32.AccessCheck(
                    sd_p,
                    token_eval,
                    desired_access,
                    ctypes.byref(mapping),
                    priv_set_buf,
                    ctypes.byref(priv_set_len),
                    ctypes.byref(granted_mask),
                    ctypes.byref(access_status),
                )
                if not chk_res:
                    err2 = ctypes.get_last_error()
                    raise TargetDaclVerificationError(f"AccessCheck retry falló: código {err2}")
            else:
                raise TargetDaclVerificationError(f"AccessCheck falló con código Win32 {err}")

        effective_mask = granted_mask.value if access_status.value != 0 else 0

        rights: set[GoldenProtectionRight] = set()
        if backup.node_kind is GoldenProtectionNodeKind.FILE:
            if effective_mask & 0x0001:
                rights.add(GoldenProtectionRight.READ_DATA)
            if effective_mask & 0x0020:
                rights.add(GoldenProtectionRight.EXECUTE)
            if effective_mask & 0x0002:
                rights.add(GoldenProtectionRight.WRITE_DATA)
            if effective_mask & 0x0004:
                rights.add(GoldenProtectionRight.APPEND_DATA)
        else:
            if effective_mask & 0x0001:
                rights.add(GoldenProtectionRight.READ_DATA)
            if effective_mask & 0x0020:
                rights.add(GoldenProtectionRight.EXECUTE)
            if effective_mask & 0x0002:
                rights.add(GoldenProtectionRight.ADD_FILE)
            if effective_mask & 0x0004:
                rights.add(GoldenProtectionRight.ADD_SUBDIRECTORY)
            if effective_mask & 0x0040:
                rights.add(GoldenProtectionRight.DELETE_CHILD)

        if effective_mask & 0x00010000:
            rights.add(GoldenProtectionRight.DELETE)
        if effective_mask & (0x0100 | 0x0010):
            rights.add(GoldenProtectionRight.WRITE_METADATA)
        if effective_mask & 0x00040000:
            rights.add(GoldenProtectionRight.CHANGE_PERMISSIONS)
        if effective_mask & 0x00080000:
            rights.add(GoldenProtectionRight.CHANGE_OWNER)

        return TargetDaclVerificationResult(
            node_kind=backup.node_kind,
            dacl_protected=dacl_protected,
            owner_rights_present=owner_rights_found,
            owner_rights_mask=owner_rights_mask,
            granted_rights=frozenset(rights),
            effective_access_mask=effective_mask,
        )

    finally:
        if token_to_close:
            _kernel32.CloseHandle(token_to_close)
        if ctypes.cast(sd_p, ctypes.c_void_p).value:
            _kernel32.LocalFree(sd_p)


# ============================================================================
# Primitiva: Helper de Test para Token Restringido / Filtrado
# ============================================================================


def _create_test_restricted_token() -> tuple[int, int]:
    """Crea un token restringido Win32 para oráculo de pruebas de acceso no elevado.

    TEST ORACLE RULE:
    En entornos CI (ej. GitHub Actions windows-latest), el runner ejecuta con UAC
    deshabilitado como Administrator completo. Para evaluar si la Target DACL
    neutraliza efectivamente permisos no elevados, este helper:
    1. Abre el token del proceso actual.
    2. Usa CreateRestrictedToken con DISABLE_MAX_PRIVILEGE (deshabilita todos los privilegios).
    3. Pasa BUILTIN\\Administrators (S-1-5-32-544) en SidsToDisable, marcándolo como DENY-ONLY.
    4. Duplica a un token de impersonación listo para AccessCheck o SetThreadToken.
    Devuelve (restricted_token_handle, impersonation_token_handle). Ambos deben cerrarse con CloseHandle.
    """
    _ensure_windows()

    token_raw = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(),
        _TOKEN_QUERY | _TOKEN_DUPLICATE,
        ctypes.byref(token_raw),
    ):
        err = ctypes.get_last_error()
        raise TargetDaclError(f"OpenProcessToken falló: código {err}")

    admin_sid = wintypes.LPVOID()
    if not _advapi32.ConvertStringSidToSidW(BUILTIN_ADMINISTRATORS_SID, ctypes.byref(admin_sid)):
        err = ctypes.get_last_error()
        _kernel32.CloseHandle(token_raw)
        raise TargetDaclError(f"ConvertStringSidToSidW falló para {BUILTIN_ADMINISTRATORS_SID}: código {err}")

    try:
        sids_to_disable = (_SidAndAttributes * 1)()
        sids_to_disable[0].Sid = admin_sid
        sids_to_disable[0].Attributes = 0

        restricted_token = wintypes.HANDLE()
        if not _advapi32.CreateRestrictedToken(
            token_raw,
            _DISABLE_MAX_PRIVILEGE,
            1,
            sids_to_disable,
            0,
            None,
            0,
            None,
            ctypes.byref(restricted_token),
        ):
            err = ctypes.get_last_error()
            raise TargetDaclError(f"CreateRestrictedToken falló: código {err}")

        token_imp = wintypes.HANDLE()
        desired = _TOKEN_QUERY | _TOKEN_IMPERSONATE | 0x0001
        if not _advapi32.DuplicateTokenEx(
            restricted_token,
            desired,
            None,
            _SECURITY_IMPERSONATION,
            _TOKEN_IMPERSONATION,
            ctypes.byref(token_imp),
        ):
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(restricted_token)
            raise TargetDaclError(f"DuplicateTokenEx falló: código {err}")

        restr_val = restricted_token.value
        imp_val = token_imp.value
        if restr_val is None or imp_val is None or restr_val in (0, -1) or imp_val in (0, -1):
            _kernel32.CloseHandle(restricted_token)
            _kernel32.CloseHandle(token_imp)
            raise TargetDaclError("Handle de token restringido inválido tras creación")

        return int(restr_val), int(imp_val)
    finally:
        _kernel32.LocalFree(admin_sid)
        _kernel32.CloseHandle(token_raw)


# ============================================================================
# Primitiva: Restauración de Security Descriptor PRE Atada a HANDLE
# ============================================================================


def restore_security_descriptor_by_handle(
    handle: int,
    backup: NodeSecurityBackup,
) -> None:
    """Restaura fielmente el Security Descriptor PRE sobre el HANDLE usando NodeSecurityBackup.

    SECURITY RULES (ADR 0010 §12.2 pasos 7 y 8):
    1. Revalida la identidad física del objeto mediante GetFileInformationByHandleEx.
    2. Revalida el enlace criptográfico sha256(decode(pre_sd_bytes_b64)) == pre_sd_sha256.
    3. Valida la estructura binaria del SD (longitud >= 20, IsValidSecurityDescriptor == TRUE).
    4. Lee SE_DACL_PROTECTED directamente desde los bytes binarios autoritativos y
       comprueba coherencia estricta con la metadata del backup.
    5. Extrae OWNER, GROUP y DACL desde los bytes binarios autoritativos.
    6. Aplica SetSecurityInfo respetando el flag de protección derivado de los bytes:
       - Si PRE era protegido -> PROTECTED_DACL_SECURITY_INFORMATION.
       - Si PRE no era protegido -> UNPROTECTED_DACL_SECURITY_INFORMATION.
    7. Nunca incluye SACL.
    """
    _ensure_windows()

    try:
        # 1. Revalidar identidad física
        vol_serial, file_id = _read_file_id_info_by_handle(handle)
        if vol_serial != backup.volume_serial_number:
            raise TargetDaclRestoreError(
                f"Drift de volumen en restore: esperado={backup.volume_serial_number}, observado={vol_serial}"
            )
        if file_id != backup.file_id:
            raise TargetDaclRestoreError(f"Drift de FileId en restore: esperado={backup.file_id}, observado={file_id}")

        # 2. Revalidar enlace criptográfico de bytes PRE
        try:
            raw_sd_bytes = base64.b64decode(backup.pre_sd_bytes_b64)
        except Exception as exc:
            raise TargetDaclRestoreError(f"Error al decodificar pre_sd_bytes_b64: {exc}") from exc

        calc_sha = hashlib.sha256(raw_sd_bytes).hexdigest()
        if calc_sha != backup.pre_sd_sha256:
            raise TargetDaclRestoreError(
                f"Fallo de integridad criptográfica en restore: hash esperado={backup.pre_sd_sha256}, calculado={calc_sha}"
            )
        if len(raw_sd_bytes) != backup.pre_sd_length:
            raise TargetDaclRestoreError(
                f"Fallo de longitud en restore: esperada={backup.pre_sd_length}, observada={len(raw_sd_bytes)}"
            )

        # 3. Validación estructural antes de cualquier parseo nativo
        if len(raw_sd_bytes) < 20:
            raise TargetDaclRestoreError(
                f"Buffer PRE menor a la longitud mínima estructural de SECURITY_DESCRIPTOR (20 bytes): {len(raw_sd_bytes)}"
            )

        sd_buf = ctypes.create_string_buffer(raw_sd_bytes, len(raw_sd_bytes))
        sd_ptr = ctypes.cast(sd_buf, wintypes.LPVOID)

        if not _advapi32.IsValidSecurityDescriptor(sd_ptr):
            raise TargetDaclRestoreError("IsValidSecurityDescriptor devolvió FALSE para buffer PRE")

        sd_len = _advapi32.GetSecurityDescriptorLength(sd_ptr)
        if sd_len != backup.pre_sd_length:
            raise TargetDaclRestoreError(
                f"GetSecurityDescriptorLength ({sd_len}) no coincide con longitud esperada ({backup.pre_sd_length})"
            )

        # 4. Derivar protection flag desde los bytes autoritativos y cruzar con metadata
        control = wintypes.WORD()
        rev = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(sd_ptr, ctypes.byref(control), ctypes.byref(rev)):
            err = ctypes.get_last_error()
            raise TargetDaclRestoreError(f"GetSecurityDescriptorControl falló en buffer PRE: código {err}")

        pre_dacl_protected_from_bytes = bool(control.value & _SE_DACL_PROTECTED)
        if pre_dacl_protected_from_bytes != backup.pre_dacl_protected_flag:
            raise TargetDaclRestoreError(
                "Discrepancia entre pre_dacl_protected_flag en backup y SE_DACL_PROTECTED en bytes de descriptor"
            )
        if bool(backup.dacl_control_flags & _SE_DACL_PROTECTED) != pre_dacl_protected_from_bytes:
            raise TargetDaclRestoreError(
                "Discrepancia entre dacl_control_flags en backup y SE_DACL_PROTECTED en bytes de descriptor"
            )

        # 5. Extraer punteros a componentes desde el buffer self-relative
        p_owner = wintypes.LPVOID()
        p_group = wintypes.LPVOID()
        p_dacl = wintypes.LPVOID()
        def_val = wintypes.BOOL()
        present_val = wintypes.BOOL()

        if not _advapi32.GetSecurityDescriptorOwner(sd_ptr, ctypes.byref(p_owner), ctypes.byref(def_val)):
            err = ctypes.get_last_error()
            raise TargetDaclRestoreError(f"GetSecurityDescriptorOwner falló en buffer PRE: código {err}")

        if not _advapi32.GetSecurityDescriptorGroup(sd_ptr, ctypes.byref(p_group), ctypes.byref(def_val)):
            err = ctypes.get_last_error()
            raise TargetDaclRestoreError(f"GetSecurityDescriptorGroup falló en buffer PRE: código {err}")

        if not _advapi32.GetSecurityDescriptorDacl(
            sd_ptr, ctypes.byref(present_val), ctypes.byref(p_dacl), ctypes.byref(def_val)
        ):
            err = ctypes.get_last_error()
            raise TargetDaclRestoreError(f"GetSecurityDescriptorDacl falló en buffer PRE: código {err}")

        # 6. Configurar flags de información de seguridad
        sec_info = _OWNER_SECURITY_INFORMATION | _GROUP_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
        if pre_dacl_protected_from_bytes:
            sec_info |= _PROTECTED_DACL_SECURITY_INFORMATION
        else:
            sec_info |= _UNPROTECTED_DACL_SECURITY_INFORMATION

        # 7. Aplicar SetSecurityInfo para restauración
        ret = _advapi32.SetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            sec_info,
            p_owner,
            p_group,
            p_dacl if present_val.value else None,
            None,
        )
        if ret != _ERROR_SUCCESS:
            raise TargetDaclRestoreError(f"SetSecurityInfo falló al restaurar descriptor PRE: código {ret}")
    except TargetDaclRestoreError:
        raise
    except TargetDaclError as exc:
        raise TargetDaclRestoreError(str(exc)) from exc


# ============================================================================
# Primitiva: Verificación Semántica de Restauración Atada a HANDLE
# ============================================================================


def verify_restored_security_descriptor_by_handle(
    handle: int,
    backup: NodeSecurityBackup,
) -> None:
    """Verifica semánticamente que el Security Descriptor restaurado coincide con PRE.

    Garantía normativa (ADR 0010 §12.2 línea 771):
    - owner POST == owner PRE
    - group POST == group PRE
    - dacl POST == dacl PRE (comparación semántica exhaustiva de ACEs y orden canónico)
    - SE_DACL_PROTECTED POST == SE_DACL_PROTECTED PRE
    - SACL no capturado, no mutado, no comparado.
    - NO exige POST_RESTORE_SD_BYTES == PRE_SD_BYTES (Windows puede reserializar con layout/padding equivalente).
    """
    _ensure_windows()

    try:
        # 1. Extraer componentes semánticos autoritativos del PRE
        try:
            raw_sd_bytes = base64.b64decode(backup.pre_sd_bytes_b64)
        except Exception as exc:
            raise TargetDaclVerificationError(f"Error al decodificar pre_sd_bytes_b64: {exc}") from exc

        if hashlib.sha256(raw_sd_bytes).hexdigest() != backup.pre_sd_sha256:
            raise TargetDaclVerificationError("Fallo de integridad criptográfica en buffer PRE durante verificación")

        if len(raw_sd_bytes) < 20:
            raise TargetDaclVerificationError("Buffer PRE menor al tamaño mínimo de SECURITY_DESCRIPTOR")

        sd_pre_buf = ctypes.create_string_buffer(raw_sd_bytes, len(raw_sd_bytes))
        sd_pre_ptr = ctypes.cast(sd_pre_buf, wintypes.LPVOID)

        try:
            pre_owner, pre_group, pre_has_dacl, pre_aces, pre_protected = _extract_sd_components(sd_pre_ptr)
        except Exception as exc:
            raise TargetDaclVerificationError(f"Fallo al extraer componentes semánticos PRE: {exc}") from exc

        # Validar coherencia entre los bytes autoritativos PRE y la metadata del backup
        if pre_protected != backup.pre_dacl_protected_flag:
            raise TargetDaclVerificationError(
                f"Inconsistencia en protección DACL PRE: bytes={pre_protected} != metadata={backup.pre_dacl_protected_flag}"
            )
        if pre_owner != backup.owner_sid:
            raise TargetDaclVerificationError(
                f"Inconsistencia en owner PRE: bytes={pre_owner} != metadata={backup.owner_sid}"
            )
        if pre_group != backup.group_sid:
            raise TargetDaclVerificationError(
                f"Inconsistencia en group PRE: bytes={pre_group} != metadata={backup.group_sid}"
            )

        # 2. Leer descriptor vivo post-restauración desde el HANDLE
        sd_post_p = wintypes.LPVOID()
        owner_post_p = wintypes.LPVOID()
        group_post_p = wintypes.LPVOID()
        dacl_post_p = wintypes.LPVOID()

        ret = _advapi32.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _GROUP_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner_post_p),
            ctypes.byref(group_post_p),
            ctypes.byref(dacl_post_p),
            None,
            ctypes.byref(sd_post_p),
        )
        if ret != _ERROR_SUCCESS:
            raise TargetDaclVerificationError(f"GetSecurityInfo falló al verificar restore: código {ret}")

        try:
            post_owner, post_group, post_has_dacl, post_aces, post_protected = _extract_sd_components(sd_post_p)

            # 3. Comparación semántica exhaustiva
            if post_owner != pre_owner:
                raise TargetDaclVerificationError(
                    f"Owner post-restore ({post_owner}) no coincide con PRE ({pre_owner})"
                )

            if post_group != pre_group:
                raise TargetDaclVerificationError(
                    f"Group post-restore ({post_group}) no coincide con PRE ({pre_group})"
                )

            if post_protected != pre_protected:
                raise TargetDaclVerificationError(
                    f"Estado protegido no coincide tras restore: post={post_protected}, pre={pre_protected}"
                )

            if post_has_dacl != pre_has_dacl:
                raise TargetDaclVerificationError(
                    f"Presencia de DACL post-restore ({post_has_dacl}) no coincide con PRE ({pre_has_dacl})"
                )

            if len(post_aces) != len(pre_aces):
                raise TargetDaclVerificationError(
                    f"Cantidad de ACEs post-restore ({len(post_aces)}) no coincide con PRE ({len(pre_aces)})"
                )

            for i, (p_ace, post_ace) in enumerate(zip(pre_aces, post_aces, strict=True)):
                if post_ace != p_ace:
                    raise TargetDaclVerificationError(
                        f"ACE #{i + 1} post-restore discordante con PRE: post={post_ace}, pre={p_ace}"
                    )

        finally:
            if ctypes.cast(sd_post_p, ctypes.c_void_p).value:
                _kernel32.LocalFree(sd_post_p)
    except TargetDaclVerificationError:
        raise
    except TargetDaclError as exc:
        raise TargetDaclVerificationError(str(exc)) from exc
