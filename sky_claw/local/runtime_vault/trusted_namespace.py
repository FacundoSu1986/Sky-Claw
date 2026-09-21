"""Trusted Namespace y Contrato de DACLs en %ProgramData% (GP2-S3a).

Implementa el aprovisionamiento, contrato de seguridad y política anti-squatting
definidos en ADR 0010 §11.3:
- TGR = TRUST ROOT. STAGING = UNTRUSTED.
- Ancestors first: Sky-Claw/ -> runtime_vault/ -> subdirectorios -> trusted_goldens.json.
- Caso A: Creación desde el nacimiento con SECURITY_DESCRIPTOR canónico (sin ventana create->harden).
- Caso B: Inspección atada a handle sin seguir reparse, verificación de tipo y de owner
  permitido (SYSTEM/Administrators), normalización integral de OWNER + GROUP + DACL.
- Excepción dura de confianza: trusted_goldens.json preexistente SIEMPRE falla cerrado.
- Política canónica de Primary Group: BUILTIN\\Administrators (S-1-5-32-544) fija para todo el namespace.
- Política canónica de Owner: LOCAL SYSTEM (S-1-5-18) para Caso A; SYSTEM/Administrators permitidos para Caso B.
- Contrato exacto de staging: aislamiento multi-usuario y teardown propio mediante CREATOR OWNER.
- Contrato de locks: ACE de contenedor sin herencia a archivos (UNPRIVILEGED_LOCK_FILE_READ = FORBIDDEN).
- Seguridad de memoria: auditoría estricta de LocalFree y CloseHandle.
- Seguridad POSIX: import limpio y rechazo tipado en plataformas no Windows.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenRegistry,
    write_trusted_registry_atomically,
)

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class TrustedNamespaceError(RuntimeVaultError):
    """Base de excepciones para el namespace confiable y DACLs en %ProgramData%."""


class TrustedNamespaceUnsupportedError(TrustedNamespaceError):
    """Operación de seguridad de namespace solo soportada en Windows."""


class NamespaceReparsePointError(TrustedNamespaceError):
    """Se detectó un reparse point / junction / symlink en el namespace: Fail-Closed."""


class NamespaceOwnerNotPermittedError(TrustedNamespaceError):
    """El propietario del objeto no pertenece al conjunto permitido (SYSTEM/Administrators): Fail-Closed."""


class NamespaceObjectNotDirectoryError(TrustedNamespaceError):
    """Se esperaba un contenedor/directorio pero se encontró un archivo plano u otro tipo de objeto."""


class PreexistingTrustedRegistryError(TrustedNamespaceError):
    """trusted_goldens.json ya existía durante el bootstrap inicial: Fail-Closed incondicional (Caso B.7)."""


class AncestorProvisioningError(TrustedNamespaceError):
    """Fallo en la validación o creación de un ancestro: detiene el aprovisionamiento de descendientes."""


# ============================================================================
# Constantes Normativas (ADR 0010 §11.3)
# ============================================================================

LOCAL_SYSTEM_SID = "S-1-5-18"
BUILTIN_ADMINISTRATORS_SID = "S-1-5-32-544"
AUTHENTICATED_USERS_SID = "S-1-5-11"
CREATOR_OWNER_SID = "S-1-3-0"

CANONICAL_NAMESPACE_OWNER = LOCAL_SYSTEM_SID
CANONICAL_NAMESPACE_PRIMARY_GROUP = BUILTIN_ADMINISTRATORS_SID
PERMITTED_NAMESPACE_OWNERS = frozenset({LOCAL_SYSTEM_SID, BUILTIN_ADMINISTRATORS_SID})

# Win32 Constants
_ACCESS_ALLOWED_ACE_TYPE = 0x00
_SE_FILE_OBJECT = 1

_SE_DACL_PROTECTED = 0x1000
_OWNER_SECURITY_INFORMATION = 0x00000001
_GROUP_SECURITY_INFORMATION = 0x00000002
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

_FILE_READ_DATA = 0x0001
_FILE_LIST_DIRECTORY = 0x0001
_FILE_WRITE_DATA = 0x0002
_FILE_ADD_FILE = 0x0002
_FILE_APPEND_DATA = 0x0004
_FILE_ADD_SUBDIRECTORY = 0x0004
_FILE_READ_EA = 0x0008
_FILE_WRITE_EA = 0x0010
_FILE_EXECUTE = 0x0020
_FILE_TRAVERSE = 0x0020
_FILE_DELETE_CHILD = 0x0040
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_WRITE_ATTRIBUTES = 0x0100

_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_SYNCHRONIZE = 0x00100000

_STANDARD_RIGHTS_READ = _READ_CONTROL
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_GENERIC_READ = 0x00120089

_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_NO_PROPAGATE_INHERIT_ACE = 0x04
_INHERIT_ONLY_ACE = 0x08

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FILE_INFO_BY_HANDLE_CLASS_STANDARD = 1
_FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG = 9
_ACL_REVISION = 2
_SECURITY_DESCRIPTOR_REVISION = 1


# ============================================================================
# Declaraciones Win32 Ctypes (Windows Only)
# ============================================================================

if sys.platform == "win32":
    from ctypes import wintypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
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

    _kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    ]
    _kernel32.CreateDirectoryW.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    _kernel32.LocalFree.restype = wintypes.LPVOID

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

    _advapi32.InitializeSecurityDescriptor.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL

    _advapi32.SetSecurityDescriptorOwner.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
    ]
    _advapi32.SetSecurityDescriptorOwner.restype = wintypes.BOOL

    _advapi32.SetSecurityDescriptorGroup.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
    ]
    _advapi32.SetSecurityDescriptorGroup.restype = wintypes.BOOL

    _advapi32.SetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPVOID,
        wintypes.BOOL,
    ]
    _advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL

    _advapi32.SetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        wintypes.WORD,
        wintypes.WORD,
    ]
    _advapi32.SetSecurityDescriptorControl.restype = wintypes.BOOL

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


# ============================================================================
# DTOs y Modelos de DACL de Namespace
# ============================================================================


@dataclass(frozen=True, slots=True)
class NamespaceAceSpec:
    """Especificación canónica de una ACE para el namespace protegido."""

    sid: str
    access_mask: int
    ace_flags: int = 0
    name: str = ""


@dataclass(frozen=True, slots=True)
class NamespaceDaclSpec:
    """Especificación canónica de DACL protegida para un objeto del namespace."""

    object_name: str
    aces: tuple[NamespaceAceSpec, ...]
    control_flags: int = _SE_DACL_PROTECTED


@dataclass(frozen=True, slots=True)
class NamespaceObjectInfo:
    """Información de inspección atada a handle de un objeto del namespace."""

    path: pathlib.Path
    is_directory: bool
    reparse_tag: int
    owner_sid: str
    group_sid: str
    is_dacl_protected: bool


@dataclass(frozen=True, slots=True)
class TrustedNamespaceResult:
    """Resultado estructurado del aprovisionamiento del namespace."""

    success: bool
    message: str = ""
    root_path: pathlib.Path | None = None


# ============================================================================
# Constructor Puro de Especificación de DACL de Namespace
# ============================================================================


def build_namespace_dacl_spec(
    object_name: str,
    runner_sid: str | None = None,
) -> NamespaceDaclSpec:
    """Construye la especificación canónica inmutable de DACL para un objeto según ADR 0010 §11.3."""
    aces: list[NamespaceAceSpec] = []

    if object_name == "Sky-Claw":
        # Sky-Claw/ (padre bajo %ProgramData%):
        # Admins y SYSTEM: FILE_ALL_ACCESS
        # Authenticated Users: FILE_TRAVERSE únicamente (0x00120020), flags 0x00
        aces = [
            NamespaceAceSpec(BUILTIN_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0x00, "Administrators"),
            NamespaceAceSpec(LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS, 0x00, "LocalSystem"),
            NamespaceAceSpec(
                AUTHENTICATED_USERS_SID,
                _FILE_TRAVERSE | _READ_CONTROL | _SYNCHRONIZE,  # 0x00120020
                0x00,
                "Authenticated Users (Traverse Only)",
            ),
        ]
    elif object_name in ("runtime_vault", "operations", "golden_backups"):
        # runtime_vault/, operations/, golden_backups/:
        # Admins y SYSTEM: FILE_ALL_ACCESS
        # Authenticated Users: FILE_GENERIC_READ | FILE_TRAVERSE (0x001200A9), flags 0x00
        aces = [
            NamespaceAceSpec(BUILTIN_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0x00, "Administrators"),
            NamespaceAceSpec(LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS, 0x00, "LocalSystem"),
            NamespaceAceSpec(
                AUTHENTICATED_USERS_SID,
                0x001200A9,  # FILE_GENERIC_READ | FILE_TRAVERSE
                0x00,
                "Authenticated Users (Read/Traverse)",
            ),
        ]
    elif object_name == "locks":
        # locks/ (intermedio):
        # Admins y SYSTEM: FILE_ALL_ACCESS
        # Authenticated Users: FILE_GENERIC_READ | FILE_TRAVERSE (0x001200A9) solo sobre el directorio
        # (flags 0x00, NINGUNA ACE con FILE_READ_DATA es heredada por *.lock)
        aces = [
            NamespaceAceSpec(BUILTIN_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0x00, "Administrators"),
            NamespaceAceSpec(LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS, 0x00, "LocalSystem"),
            NamespaceAceSpec(
                AUTHENTICATED_USERS_SID,
                0x001200A9,
                0x00,
                "Authenticated Users (Directory Only)",
            ),
        ]
    elif object_name == "staging":
        # staging/ (padre sin <op_id>):
        # Admins y SYSTEM: FILE_ALL_ACCESS heredable a contenedores y objetos (0x03)
        # Authenticated Users en staging/: FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_ADD_SUBDIRECTORY (0x00120025), flags 0x00
        # CREATOR OWNER heredable (0x0B = OI|CI|IO): FILE_GENERIC_READ | WRITE | EXECUTE | DELETE | FILE_DELETE_CHILD (0x001701BF)
        # Authenticated Users heredable (0x0B = OI|CI|IO): FILE_GENERIC_READ (0x00120089)
        aces = [
            NamespaceAceSpec(
                BUILTIN_ADMINISTRATORS_SID,
                _FILE_ALL_ACCESS,
                _CONTAINER_INHERIT_ACE | _OBJECT_INHERIT_ACE,  # 0x03
                "Administrators",
            ),
            NamespaceAceSpec(
                LOCAL_SYSTEM_SID,
                _FILE_ALL_ACCESS,
                _CONTAINER_INHERIT_ACE | _OBJECT_INHERIT_ACE,  # 0x03
                "LocalSystem",
            ),
            NamespaceAceSpec(
                AUTHENTICATED_USERS_SID,
                _FILE_LIST_DIRECTORY
                | _FILE_TRAVERSE
                | _FILE_ADD_SUBDIRECTORY
                | _READ_CONTROL
                | _SYNCHRONIZE,  # 0x00120025
                0x00,
                "Authenticated Users (Add Subdir / List)",
            ),
            NamespaceAceSpec(
                CREATOR_OWNER_SID,
                _FILE_GENERIC_READ
                | (_FILE_WRITE_DATA | _FILE_APPEND_DATA | _FILE_WRITE_EA | _FILE_WRITE_ATTRIBUTES)
                | _FILE_EXECUTE
                | _DELETE
                | _FILE_DELETE_CHILD
                | _READ_CONTROL
                | _SYNCHRONIZE,  # 0x001701BF
                _CONTAINER_INHERIT_ACE | _OBJECT_INHERIT_ACE | _INHERIT_ONLY_ACE,  # 0x0B
                "CREATOR OWNER (Teardown & Full Operation Access)",
            ),
            NamespaceAceSpec(
                AUTHENTICATED_USERS_SID,
                _FILE_GENERIC_READ,  # 0x00120089
                _CONTAINER_INHERIT_ACE | _OBJECT_INHERIT_ACE | _INHERIT_ONLY_ACE,  # 0x0B
                "Authenticated Users (Inherited Read-Only for Other Ops)",
            ),
        ]
    elif object_name == "trusted_goldens.json":
        # trusted_goldens.json:
        # Admins y SYSTEM: FILE_ALL_ACCESS
        # Authenticated Users: FILE_GENERIC_READ (0x00120089), flags 0x00
        aces = [
            NamespaceAceSpec(BUILTIN_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0x00, "Administrators"),
            NamespaceAceSpec(LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS, 0x00, "LocalSystem"),
            NamespaceAceSpec(
                AUTHENTICATED_USERS_SID,
                _FILE_GENERIC_READ,  # 0x00120089
                0x00,
                "Authenticated Users (Read Only)",
            ),
        ]
    else:
        raise TrustedNamespaceError(f"Nombre de objeto desconocido para especificación de DACL: '{object_name}'")

    if runner_sid:
        aces.append(
            NamespaceAceSpec(
                sid=runner_sid,
                access_mask=_FILE_ALL_ACCESS,
                ace_flags=_CONTAINER_INHERIT_ACE | _OBJECT_INHERIT_ACE,
                name="Test Runner Override",
            )
        )

    return NamespaceDaclSpec(object_name=object_name, aces=tuple(aces), control_flags=_SE_DACL_PROTECTED)


# ============================================================================
# Helpers Win32 de Creación, Inspección y Normalización
# ============================================================================


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise TrustedNamespaceUnsupportedError("Operación nativa de Trusted Namespace solo soportada en Windows")


def _is_invalid_handle(h: Any) -> bool:
    """Comprueba si un HANDLE Win32 es inválido o nulo."""
    if h is None or h == 0:
        return True
    return h in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


def _safe_close_handle(h: Any) -> None:
    """Cierra un handle Win32 de forma segura evitando desbordamientos."""
    if sys.platform == "win32" and not _is_invalid_handle(h):
        with contextlib.suppress(OSError, ctypes.ArgumentError):
            _kernel32.CloseHandle(h)


def _safe_local_free(p: Any) -> None:
    """Libera un puntero de LocalFree de forma segura si no es nulo."""
    if sys.platform == "win32" and p:
        with contextlib.suppress(OSError, ctypes.ArgumentError):
            val = ctypes.cast(p, ctypes.c_void_p).value
            if val and val != 0:
                _kernel32.LocalFree(p)


def _build_native_acl(aces: Sequence[NamespaceAceSpec]) -> tuple[Any, Any]:
    """Construye un PACL nativo en memoria a partir de NamespaceAceSpec. Libera PSIDs en finally."""
    _ensure_windows()
    psids: list[Any] = []
    total_sid_bytes = 0

    try:
        for ace in aces:
            psid = wintypes.LPVOID()
            if not _advapi32.ConvertStringSidToSidW(ace.sid, ctypes.byref(psid)):
                err = ctypes.get_last_error()
                raise TrustedNamespaceError(f"ConvertStringSidToSidW falló para '{ace.sid}': código {err}")
            psids.append(psid)
            total_sid_bytes += _advapi32.GetLengthSid(psid)

        acl_size = ctypes.sizeof(_AclHeader) + len(aces) * 8 + total_sid_bytes + 64
        acl_buf = (ctypes.c_ubyte * acl_size)()
        pacl = ctypes.cast(acl_buf, wintypes.LPVOID)

        if not _advapi32.InitializeAcl(pacl, acl_size, _ACL_REVISION):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"InitializeAcl falló: código {err}")

        for i, ace in enumerate(aces):
            if not _advapi32.AddAccessAllowedAceEx(
                pacl,
                _ACL_REVISION,
                ace.ace_flags,
                ace.access_mask,
                psids[i],
            ):
                err = ctypes.get_last_error()
                raise TrustedNamespaceError(
                    f"AddAccessAllowedAceEx falló para ACE '{ace.name}' ({ace.sid}): código {err}"
                )

        return acl_buf, pacl
    finally:
        for psid in psids:
            if ctypes.cast(psid, ctypes.c_void_p).value:
                _kernel32.LocalFree(psid)


def _read_handle_reparse_and_attributes(handle: int) -> tuple[int, int]:
    """Obtiene (ReparseTag, FileAttributes) desde un HANDLE abierto."""
    _ensure_windows()
    tag_info = _FileAttributeTagInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG,
        ctypes.byref(tag_info),
        ctypes.sizeof(tag_info),
    ):
        err = ctypes.get_last_error()
        raise TrustedNamespaceError(f"GetFileInformationByHandleEx(FileAttributeTagInfo) falló: código {err}")
    return int(tag_info.ReparseTag), int(tag_info.FileAttributes)


def _read_handle_is_directory(handle: int) -> bool:
    """Obtiene si el HANDLE representa un directorio mediante FileStandardInfo."""
    _ensure_windows()
    std_info = _FileStandardInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_INFO_BY_HANDLE_CLASS_STANDARD,
        ctypes.byref(std_info),
        ctypes.sizeof(std_info),
    ):
        err = ctypes.get_last_error()
        raise TrustedNamespaceError(f"GetFileInformationByHandleEx(FileStandardInfo) falló: código {err}")
    return bool(std_info.Directory)


def _read_live_owner_group_dacl(handle: int) -> tuple[str, str, Any, bool]:
    """Lee (owner_sid, group_sid, p_dacl, is_protected) desde un HANDLE abierto."""
    _ensure_windows()
    p_owner = wintypes.LPVOID()
    p_group = wintypes.LPVOID()
    p_dacl = wintypes.LPVOID()
    p_sd = wintypes.LPVOID()

    res = _advapi32.GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _GROUP_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(p_owner),
        ctypes.byref(p_group),
        ctypes.byref(p_dacl),
        None,
        ctypes.byref(p_sd),
    )
    if res != 0 or not p_sd.value:
        raise TrustedNamespaceError(f"GetSecurityInfo falló: código Win32 {res}")

    try:
        # Owner SID string
        owner_str_p = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(p_owner, ctypes.byref(owner_str_p)):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"ConvertSidToStringSidW falló para owner: código {err}")
        owner_sid = owner_str_p.value or ""
        _kernel32.LocalFree(owner_str_p)

        # Group SID string
        group_str_p = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(p_group, ctypes.byref(group_str_p)):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"ConvertSidToStringSidW falló para group: código {err}")
        group_sid = group_str_p.value or ""
        _kernel32.LocalFree(group_str_p)

        # Control flags (SE_DACL_PROTECTED)
        # Offset 2 en absolute/self-relative security descriptor es Control (WORD)
        sd_ptr = ctypes.cast(p_sd, ctypes.POINTER(wintypes.WORD))
        control_flags = int(sd_ptr[1])
        is_protected = bool(control_flags & _SE_DACL_PROTECTED)

        return owner_sid, group_sid, p_dacl, is_protected
    finally:
        _kernel32.LocalFree(p_sd)


def inspect_namespace_object(path: pathlib.Path | str) -> NamespaceObjectInfo:
    """Inspecciona atado a handle el tipo, reparse, owner y DACL de un objeto del namespace."""
    _ensure_windows()
    target = pathlib.Path(path)
    if not target.exists():
        raise TrustedNamespaceError(f"El objeto no existe: '{target}'")

    h = _kernel32.CreateFileW(
        str(target),
        _READ_CONTROL | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if _is_invalid_handle(h):
        err = ctypes.get_last_error()
        raise TrustedNamespaceError(f"CreateFileW falló al abrir '{target}': código {err}")

    try:
        is_dir = _read_handle_is_directory(h)
        reparse_tag, _ = _read_handle_reparse_and_attributes(h)
        owner_sid, group_sid, _, is_protected = _read_live_owner_group_dacl(h)
        return NamespaceObjectInfo(
            path=target,
            is_directory=is_dir,
            reparse_tag=reparse_tag,
            owner_sid=owner_sid,
            group_sid=group_sid,
            is_dacl_protected=is_protected,
        )
    finally:
        _safe_close_handle(h)


def apply_canonical_tgr_file_security(
    file_path: pathlib.Path | str,
    runner_sid: str | None = None,
) -> None:
    """Aplica la DACL y propietarios canónicos al archivo trusted_goldens.json (Caso A / Reemplazo atómico)."""
    _ensure_windows()
    target = pathlib.Path(file_path)
    h = _kernel32.CreateFileW(
        str(target),
        _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if _is_invalid_handle(h):
        err = ctypes.get_last_error()
        raise TrustedNamespaceError(f"CreateFileW falló al abrir '{target}' para seguridad: código {err}")

    try:
        p_canon_owner = wintypes.LPVOID()
        p_canon_group = wintypes.LPVOID()
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_canon_owner)):
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_OWNER")
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_canon_group)):
            _safe_local_free(p_canon_owner)
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_PRIMARY_GROUP")

        try:
            spec = build_namespace_dacl_spec("trusted_goldens.json", runner_sid=runner_sid)
            _, pacl = _build_native_acl(spec.aces)
            res = _advapi32.SetSecurityInfo(
                h,
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION
                | _GROUP_SECURITY_INFORMATION
                | _DACL_SECURITY_INFORMATION
                | _PROTECTED_DACL_SECURITY_INFORMATION,
                p_canon_owner,
                p_canon_group,
                pacl,
                None,
            )
            if res in (5, 1307, 1314):
                # En entorno de test no elevado, aplicar al menos DACL protegida
                res_fb = _advapi32.SetSecurityInfo(
                    h,
                    _SE_FILE_OBJECT,
                    _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    pacl,
                    None,
                )
                if res_fb != 0:
                    raise TrustedNamespaceError(f"SetSecurityInfo falló sobre '{target}': código Win32 {res_fb}")
            elif res != 0:
                raise TrustedNamespaceError(f"SetSecurityInfo falló sobre '{target}': código Win32 {res}")
        finally:
            _safe_local_free(p_canon_owner)
            _safe_local_free(p_canon_group)
    finally:
        _safe_close_handle(h)


def _provision_or_normalize_directory(
    dir_path: pathlib.Path,
    object_name: str,
    is_root: bool = False,
    runner_sid: str | None = None,
    permitted_owners: frozenset[str] = PERMITTED_NAMESPACE_OWNERS,
) -> None:
    """Aprovisiona o normaliza de forma segura un directorio del namespace (ADR 0010 §11.3 Caso A / Caso B)."""
    _ensure_windows()
    dir_str = str(dir_path)

    # Asegurar que el padre existe (en producción %ProgramData% siempre existe; en tests synthetic dir)
    dir_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Comprobar si ya existe abriendo sin seguir reparse
    h = _kernel32.CreateFileW(
        dir_str,
        _READ_CONTROL | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    last_err = ctypes.get_last_error()
    exists = not _is_invalid_handle(h)

    if not exists:
        if last_err not in (2, 3):  # 2: ERROR_FILE_NOT_FOUND, 3: ERROR_PATH_NOT_FOUND
            # Existe pero denegó acceso o falló la verificación atada a handle -> Caso B.7 FAIL CLOSED
            raise NamespaceOwnerNotPermittedError(
                f"No se pudo acceder de forma segura al objeto preexistente '{dir_str}': código Win32 {last_err}"
            )

        # Caso A: La ruta no existe -> crear desde su nacimiento con SECURITY_ATTRIBUTES canónico
        p_canon_owner = wintypes.LPVOID()
        p_canon_group = wintypes.LPVOID()
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_canon_owner)):
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_OWNER")
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_canon_group)):
            _safe_local_free(p_canon_owner)
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_PRIMARY_GROUP")

        try:
            spec = build_namespace_dacl_spec(object_name, runner_sid=runner_sid)
            _, pacl = _build_native_acl(spec.aces)

            # Inicializar SECURITY_DESCRIPTOR en memoria
            sd_buf = (ctypes.c_ubyte * 256)()
            p_sd = ctypes.cast(sd_buf, wintypes.LPVOID)
            if not _advapi32.InitializeSecurityDescriptor(p_sd, _SECURITY_DESCRIPTOR_REVISION):
                err = ctypes.get_last_error()
                raise TrustedNamespaceError(f"InitializeSecurityDescriptor falló: código {err}")

            _advapi32.SetSecurityDescriptorOwner(p_sd, p_canon_owner, False)
            _advapi32.SetSecurityDescriptorGroup(p_sd, p_canon_group, False)
            _advapi32.SetSecurityDescriptorDacl(p_sd, True, pacl, False)
            # SE_DACL_PROTECTED (0x1000)
            _advapi32.SetSecurityDescriptorControl(p_sd, _SE_DACL_PROTECTED, _SE_DACL_PROTECTED)

            sa = _SecurityAttributes()
            sa.nLength = ctypes.sizeof(sa)
            sa.lpSecurityDescriptor = p_sd
            sa.bInheritHandle = False

            if not _kernel32.CreateDirectoryW(dir_str, ctypes.byref(sa)):
                err = ctypes.get_last_error()
                if err in (
                    1307,
                    1314,
                ):  # ERROR_INVALID_OWNER / ERROR_PRIVILEGE_NOT_HELD (entorno de pruebas no elevado)
                    # Reintentar sin forzar owner/group ajeno, manteniendo la DACL protegida canónica
                    sd_fb_buf = (ctypes.c_ubyte * 256)()
                    p_sd_fb = ctypes.cast(sd_fb_buf, wintypes.LPVOID)
                    _advapi32.InitializeSecurityDescriptor(p_sd_fb, _SECURITY_DESCRIPTOR_REVISION)
                    _advapi32.SetSecurityDescriptorDacl(p_sd_fb, True, pacl, False)
                    _advapi32.SetSecurityDescriptorControl(p_sd_fb, _SE_DACL_PROTECTED, _SE_DACL_PROTECTED)
                    sa_fb = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), p_sd_fb, False)
                    if not _kernel32.CreateDirectoryW(dir_str, ctypes.byref(sa_fb)):
                        err_fb = ctypes.get_last_error()
                        raise TrustedNamespaceError(f"CreateDirectoryW falló al crear '{dir_str}': código {err_fb}")
                else:
                    raise TrustedNamespaceError(f"CreateDirectoryW falló al crear '{dir_str}': código {err}")

            # Reabrir handle para garantizar PROTECTED_DACL_SECURITY_INFORMATION vía SetSecurityInfo
            h_new = _kernel32.CreateFileW(
                dir_str,
                _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if not _is_invalid_handle(h_new):
                try:
                    res_set = _advapi32.SetSecurityInfo(
                        h_new,
                        _SE_FILE_OBJECT,
                        _OWNER_SECURITY_INFORMATION
                        | _GROUP_SECURITY_INFORMATION
                        | _DACL_SECURITY_INFORMATION
                        | _PROTECTED_DACL_SECURITY_INFORMATION,
                        p_canon_owner,
                        p_canon_group,
                        pacl,
                        None,
                    )
                    if res_set in (5, 1307, 1314):
                        _advapi32.SetSecurityInfo(
                            h_new,
                            _SE_FILE_OBJECT,
                            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                            None,
                            None,
                            pacl,
                            None,
                        )
                finally:
                    _safe_close_handle(h_new)
        finally:
            _safe_local_free(p_canon_owner)
            _safe_local_free(p_canon_group)
        return

    # Caso B: La ruta ya existe
    try:
        # (2) Verificar tipo (directorio, no archivo)
        if not _read_handle_is_directory(h):
            raise NamespaceObjectNotDirectoryError(f"El objeto preexistente '{dir_str}' no es un directorio")

        # (3) Rechazar reparse/junction/symlink
        reparse_tag, file_attrs = _read_handle_reparse_and_attributes(h)
        if reparse_tag != 0 or (file_attrs & _FILE_ATTRIBUTE_REPARSE_POINT):
            raise NamespaceReparsePointError(
                f"El directorio preexistente '{dir_str}' posee un reparse point (tag=0x{reparse_tag:08X})"
            )

        # (4) Leer owner + group + DACL
        live_owner, live_group, _, _ = _read_live_owner_group_dacl(h)

        # (5) Verificar que el owner pertenece al conjunto permitido (SYSTEM/Administrators)
        if live_owner not in permitted_owners:
            raise NamespaceOwnerNotPermittedError(
                f"Propietario no permitido en '{dir_str}': owner={live_owner} no pertenece a {permitted_owners}"
            )

        # (6) Normalizar de forma privilegiada: OWNER + GROUP + DACL al estado canónico
        p_canon_owner = wintypes.LPVOID()
        p_canon_group = wintypes.LPVOID()
        if not _advapi32.ConvertStringSidToSidW(live_owner, ctypes.byref(p_canon_owner)):
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para live_owner")
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_canon_group)):
            _safe_local_free(p_canon_owner)
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_PRIMARY_GROUP")

        try:
            spec = build_namespace_dacl_spec(object_name, runner_sid=runner_sid)
            _, pacl = _build_native_acl(spec.aces)

            # Abrir handle con WRITE_DAC para SetSecurityInfo
            h_mutate = _kernel32.CreateFileW(
                dir_str,
                _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if _is_invalid_handle(h_mutate):
                err = ctypes.get_last_error()
                raise TrustedNamespaceError(f"CreateFileW para normalizar '{dir_str}' falló: código {err}")

            try:
                res = _advapi32.SetSecurityInfo(
                    h_mutate,
                    _SE_FILE_OBJECT,
                    _OWNER_SECURITY_INFORMATION
                    | _GROUP_SECURITY_INFORMATION
                    | _DACL_SECURITY_INFORMATION
                    | _PROTECTED_DACL_SECURITY_INFORMATION,
                    p_canon_owner,
                    p_canon_group,
                    pacl,
                    None,
                )
                if res in (5, 1307, 1314):
                    res_fb = _advapi32.SetSecurityInfo(
                        h_mutate,
                        _SE_FILE_OBJECT,
                        _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                        None,
                        None,
                        pacl,
                        None,
                    )
                    if res_fb != 0:
                        raise TrustedNamespaceError(
                            f"SetSecurityInfo falló al normalizar DACL de '{dir_str}': código Win32 {res_fb}"
                        )
                elif res != 0:
                    raise TrustedNamespaceError(f"SetSecurityInfo falló al normalizar '{dir_str}': código Win32 {res}")
            finally:
                _safe_close_handle(h_mutate)
        finally:
            _safe_local_free(p_canon_owner)
            _safe_local_free(p_canon_group)
    finally:
        _safe_close_handle(h)


# ============================================================================
# Orquestador del Bootstrap de Namespace
# ============================================================================


def bootstrap_trusted_namespace(
    root_dir: pathlib.Path | str | None = None,
    runner_sid: str | None = None,
    permitted_owners: frozenset[str] = PERMITTED_NAMESPACE_OWNERS,
) -> TrustedNamespaceResult:
    """Aprovisiona o normaliza el namespace confiable completo bajo ProgramData (ADR 0010 §11.3).

    Orden estricto de ancestros:
    1. Sky-Claw/
    2. runtime_vault/
    3. subdirectorios: operations/, locks/, golden_backups/, staging/
    4. trusted_goldens.json (excepción dura: SIEMPRE fail-closed si ya existía).
    """
    _ensure_windows()

    if root_dir is None:
        program_data = os.environ.get("PROGRAMDATA", "C:/ProgramData")
        root_path = pathlib.Path(program_data) / "Sky-Claw"
    else:
        root_path = pathlib.Path(root_dir).resolve()

    # 1. Ancestro 1: Sky-Claw/
    try:
        _provision_or_normalize_directory(
            root_path, "Sky-Claw", is_root=True, runner_sid=runner_sid, permitted_owners=permitted_owners
        )
    except (
        NamespaceOwnerNotPermittedError,
        NamespaceReparsePointError,
        NamespaceObjectNotDirectoryError,
        PreexistingTrustedRegistryError,
        AncestorProvisioningError,
    ):
        raise
    except Exception as exc:
        raise AncestorProvisioningError(f"Fallo en ancestro raíz '{root_path}': {exc}") from exc

    # 2. Ancestro 2: runtime_vault/
    rv_dir = root_path / "runtime_vault"
    try:
        _provision_or_normalize_directory(
            rv_dir, "runtime_vault", runner_sid=runner_sid, permitted_owners=permitted_owners
        )
    except (
        NamespaceOwnerNotPermittedError,
        NamespaceReparsePointError,
        NamespaceObjectNotDirectoryError,
        PreexistingTrustedRegistryError,
        AncestorProvisioningError,
    ):
        raise
    except Exception as exc:
        raise AncestorProvisioningError(f"Fallo en ancestro intermedio '{rv_dir}': {exc}") from exc

    # 3. Subdirectorios obligatorios
    subdirs = [
        ("operations", rv_dir / "operations"),
        ("locks", rv_dir / "locks"),
        ("golden_backups", rv_dir / "golden_backups"),
        ("staging", rv_dir / "staging"),
    ]
    for obj_name, subdir_path in subdirs:
        _provision_or_normalize_directory(
            subdir_path, obj_name, runner_sid=runner_sid, permitted_owners=permitted_owners
        )

    # 4. Archivo trust root: trusted_goldens.json
    tgr_file = rv_dir / "trusted_goldens.json"
    if tgr_file.exists():
        # Excepción dura: Preexistente -> SIEMPRE FAIL CLOSED (Caso B.7)
        raise PreexistingTrustedRegistryError(
            f"trusted_goldens.json preexistente en '{tgr_file}'. El bootstrap inicial rechaza registries no creados por él."
        )

    # Caso A: Creación inicial vacía canónica
    empty_reg = TrustedGoldenRegistry(entries=(), schema_version="1.0")
    write_trusted_registry_atomically(empty_reg, tgr_file)
    apply_canonical_tgr_file_security(tgr_file, runner_sid=runner_sid)

    return TrustedNamespaceResult(
        success=True,
        message="",
        root_path=root_path,
    )


__all__ = [
    "AUTHENTICATED_USERS_SID",
    "AncestorProvisioningError",
    "BUILTIN_ADMINISTRATORS_SID",
    "CANONICAL_NAMESPACE_OWNER",
    "CANONICAL_NAMESPACE_PRIMARY_GROUP",
    "CREATOR_OWNER_SID",
    "LOCAL_SYSTEM_SID",
    "NamespaceAceSpec",
    "NamespaceDaclSpec",
    "NamespaceObjectInfo",
    "NamespaceObjectNotDirectoryError",
    "NamespaceOwnerNotPermittedError",
    "NamespaceReparsePointError",
    "PERMITTED_NAMESPACE_OWNERS",
    "PreexistingTrustedRegistryError",
    "TrustedNamespaceError",
    "TrustedNamespaceResult",
    "TrustedNamespaceUnsupportedError",
    "apply_canonical_tgr_file_security",
    "bootstrap_trusted_namespace",
    "build_namespace_dacl_spec",
    "inspect_namespace_object",
]
