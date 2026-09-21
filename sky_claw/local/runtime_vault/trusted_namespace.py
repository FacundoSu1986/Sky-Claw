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
- Política canónica de Owner: LOCAL SYSTEM (S-1-5-18) para Caso A y Caso B.
- Contrato exacto de staging: aislamiento multi-usuario y teardown propio mediante CREATOR OWNER.
- Contrato de locks: ACE de contenedor sin herencia a archivos (UNPRIVILEGED_LOCK_FILE_READ = FORBIDDEN).
- Seguridad de memoria: auditoría estricta de LocalFree y CloseHandle; sin punteros colgantes.
- Sin seams de test en API productiva: cero fallbacks DACL-only.
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

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000

_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_NO_PROPAGATE_INHERIT_ACE = 0x04
_INHERIT_ONLY_ACE = 0x08

_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004

_CREATE_NEW = 1
_CREATE_ALWAYS = 2
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_TRUNCATE_EXISTING = 5

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

    _kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetFileAttributesW.restype = wintypes.DWORD

    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL

    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL

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
# Constructor Puro de Especificación de DACL de Namespace (Sin Seams de Test)
# ============================================================================


def build_namespace_dacl_spec(object_name: str) -> NamespaceDaclSpec:
    """Construye la especificación canónica inmutable de DACL para un objeto según ADR 0010 §11.3.

    Reglas de máscaras exactas:
    - Sky-Claw/: AU = FILE_TRAVERSE únicamente (0x00000020).
    - runtime_vault/, operations/, golden_backups/: AU = 0x001200A9 (FILE_GENERIC_READ | FILE_TRAVERSE).
    - locks/: AU = 0x001200A9 (directorio únicamente, flags 0x00, sin herencia a archivos *.lock).
    - staging/ (padre): AU = FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_ADD_SUBDIRECTORY (0x00000025).
      CREATOR OWNER heredable: 0x001701BF (OI|CI|IO).
      AU heredable a operaciones ajenas: FILE_GENERIC_READ (0x00120089, OI|CI|IO).
    - trusted_goldens.json: AU = FILE_GENERIC_READ (0x00120089).
    """
    aces: list[NamespaceAceSpec] = []

    if object_name == "Sky-Claw":
        # Sky-Claw/ (padre bajo %ProgramData%):
        # Admins y SYSTEM: FILE_ALL_ACCESS (0x001F01FF)
        # Authenticated Users: FILE_TRAVERSE únicamente (0x00000020), flags 0x00
        aces = [
            NamespaceAceSpec(BUILTIN_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0x00, "Administrators"),
            NamespaceAceSpec(LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS, 0x00, "LocalSystem"),
            NamespaceAceSpec(
                AUTHENTICATED_USERS_SID,
                _FILE_TRAVERSE,  # 0x00000020 exacto
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
        # Authenticated Users en staging/: FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_ADD_SUBDIRECTORY (0x00000025), flags 0x00
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
                _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_ADD_SUBDIRECTORY,  # 0x00000025 exacto
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
    """Cierra un handle Win32 de forma segura evitando excepciones."""
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


def _build_native_acl_with_psids(aces: Sequence[NamespaceAceSpec]) -> tuple[Any, Any, list[Any]]:
    """Construye un PACL nativo en memoria a partir de NamespaceAceSpec retornando los PSIDs creados."""
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

        return acl_buf, pacl, psids
    except Exception:
        for psid in psids:
            _safe_local_free(psid)
        raise


def _build_native_acl(aces: Sequence[NamespaceAceSpec]) -> tuple[Any, Any]:
    """Construye un PACL nativo en memoria a partir de NamespaceAceSpec. Libera PSIDs en finally."""
    acl_buf, pacl, psids = _build_native_acl_with_psids(aces)
    for psid in psids:
        _safe_local_free(psid)
    return acl_buf, pacl


class CanonicalSecurityDescriptorContext:
    """Contenedor seguro de memoria para un SECURITY_DESCRIPTOR nativo y sus buffers asociados."""

    def __init__(self, sd_buf: Any, pacl_buf: Any, psids: list[Any], p_sd: Any) -> None:
        self.sd_buf = sd_buf
        self.pacl_buf = pacl_buf
        self.psids = psids
        self.p_sd = p_sd

    def close(self) -> None:
        if sys.platform == "win32":
            for psid in self.psids:
                _safe_local_free(psid)
            self.psids.clear()

    def __enter__(self) -> CanonicalSecurityDescriptorContext:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def _build_canonical_security_descriptor(object_name: str) -> CanonicalSecurityDescriptorContext:
    """Construye un SECURITY_DESCRIPTOR canónico completo en memoria.

    Verifica estrictamente los retornos booleanos de:
    - InitializeSecurityDescriptor
    - SetSecurityDescriptorOwner (LOCAL SYSTEM S-1-5-18)
    - SetSecurityDescriptorGroup (BUILTIN\\Administrators S-1-5-32-544)
    - SetSecurityDescriptorDacl (DACL canónica protegida)
    - SetSecurityDescriptorControl (SE_DACL_PROTECTED)
    Cualquier valor False lanza TrustedNamespaceError (Fail-Closed).
    """
    _ensure_windows()
    psids: list[Any] = []
    try:
        # Owner SID
        p_canon_owner = wintypes.LPVOID()
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_canon_owner)):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_OWNER: {err}")
        psids.append(p_canon_owner)

        # Group SID
        p_canon_group = wintypes.LPVOID()
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_canon_group)):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_PRIMARY_GROUP: {err}")
        psids.append(p_canon_group)

        # Build DACL
        spec = build_namespace_dacl_spec(object_name)
        pacl_buf, pacl, ace_psids = _build_native_acl_with_psids(spec.aces)
        psids.extend(ace_psids)

        # SD Buffer (absolute security descriptor)
        sd_buf = (ctypes.c_ubyte * 256)()
        p_sd = ctypes.cast(sd_buf, wintypes.LPVOID)

        if not _advapi32.InitializeSecurityDescriptor(p_sd, _SECURITY_DESCRIPTOR_REVISION):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"InitializeSecurityDescriptor falló: {err}")

        if not _advapi32.SetSecurityDescriptorOwner(p_sd, p_canon_owner, False):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"SetSecurityDescriptorOwner falló: {err}")

        if not _advapi32.SetSecurityDescriptorGroup(p_sd, p_canon_group, False):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"SetSecurityDescriptorGroup falló: {err}")

        if not _advapi32.SetSecurityDescriptorDacl(p_sd, True, pacl, False):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"SetSecurityDescriptorDacl falló: {err}")

        if not _advapi32.SetSecurityDescriptorControl(p_sd, _SE_DACL_PROTECTED, _SE_DACL_PROTECTED):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"SetSecurityDescriptorControl falló: {err}")

        return CanonicalSecurityDescriptorContext(sd_buf, pacl_buf, psids, p_sd)
    except Exception:
        for psid in psids:
            _safe_local_free(psid)
        raise


def _open_handle_no_reparse(
    path: pathlib.Path | str,
    desired_access: int = _READ_CONTROL | _FILE_READ_ATTRIBUTES,
    creation_disposition: int = _OPEN_EXISTING,
    share_mode: int = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
    flags_and_attributes: int = _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    security_attributes: Any = None,
) -> int:
    """Abre un handle Win32 garantizando que no se sigan reparse points ni junctions."""
    _ensure_windows()
    target_str = str(path)
    p_sa = ctypes.byref(security_attributes) if security_attributes is not None else None
    h = _kernel32.CreateFileW(
        target_str,
        desired_access,
        share_mode,
        p_sa,
        creation_disposition,
        flags_and_attributes,
        None,
    )
    return int(h)


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


def _read_live_owner_group_dacl(handle: int) -> tuple[str, str, bool]:
    """Lee (owner_sid, group_sid, is_protected) desde un HANDLE abierto.

    Garantiza que no se retorne ningún puntero a memoria liberada por LocalFree.
    """
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
        _safe_local_free(owner_str_p)

        # Group SID string
        group_str_p = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(p_group, ctypes.byref(group_str_p)):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"ConvertSidToStringSidW falló para group: código {err}")
        group_sid = group_str_p.value or ""
        _safe_local_free(group_str_p)

        # Control flags (SE_DACL_PROTECTED)
        sd_ptr = ctypes.cast(p_sd, ctypes.POINTER(wintypes.WORD))
        control_flags = int(sd_ptr[1])
        is_protected = bool(control_flags & _SE_DACL_PROTECTED)

        return owner_sid, group_sid, is_protected
    finally:
        _safe_local_free(p_sd)


def _check_object_exists_no_reparse(path: pathlib.Path | str) -> bool:
    """Comprueba si un objeto existe sin seguir reparse points / junctions / symlinks.

    Reglas ADR 0010 §11.3:
    - Abre el objeto con FILE_FLAG_OPEN_REPARSE_POINT.
    - Si existe y posee ReparseTag != 0 o FILE_ATTRIBUTE_REPARSE_POINT -> NamespaceReparsePointError (Fail-Closed).
    - Si existe como objeto normal sin reparse -> True.
    - Si no existe en absoluto (ERROR_FILE_NOT_FOUND o ERROR_PATH_NOT_FOUND) -> False.
    - Ante cualquier otro error de acceso -> TrustedNamespaceError (Fail-Closed).
    """
    _ensure_windows()
    target_str = str(path)
    h = _open_handle_no_reparse(
        target_str,
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES,
        creation_disposition=_OPEN_EXISTING,
    )
    if not _is_invalid_handle(h):
        try:
            reparse_tag, file_attrs = _read_handle_reparse_and_attributes(h)
            if reparse_tag != 0 or (file_attrs & _FILE_ATTRIBUTE_REPARSE_POINT):
                raise NamespaceReparsePointError(
                    f"Se detectó un reparse point / symlink preexistente en '{target_str}' (tag=0x{reparse_tag:08X})"
                )
            return True
        finally:
            _safe_close_handle(h)

    last_err = ctypes.get_last_error()
    if last_err in (2, 3):  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
        attrs = _kernel32.GetFileAttributesW(target_str)
        if attrs != _INVALID_FILE_ATTRIBUTES:
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise NamespaceReparsePointError(
                    f"Se detectó un reparse point / symlink roto en '{target_str}': atributos 0x{attrs:08X}"
                )
            return True
        attr_err = ctypes.get_last_error()
        if attr_err in (2, 3):
            return False

    raise TrustedNamespaceError(f"No se pudo verificar la existencia segura de '{target_str}': código Win32 {last_err}")


def create_secured_file_from_birth(
    path: pathlib.Path | str,
    object_name: str = "trusted_goldens.json",
) -> int:
    """Crea un archivo nuevo garantizando que nazca con su SECURITY_DESCRIPTOR canónico.

    ADR 0010 §11.3 Caso A:
    - lpSecurityDescriptor asigna atómicamente:
      - Owner: LOCAL SYSTEM (S-1-5-18)
      - Primary Group: BUILTIN\\Administrators (S-1-5-32-544)
      - DACL: DACL canónica protegida de trusted_goldens.json
      - Control: SE_DACL_PROTECTED
    - Creación con CREATE_NEW (falla si el archivo ya existe).
    - dwDesiredAccess incluye WRITE_DAC | WRITE_OWNER | GENERIC_WRITE | GENERIC_READ | READ_CONTROL.
    - dwFlagsAndAttributes incluye FILE_FLAG_OPEN_REPARSE_POINT para evitar seguir reparse points.
    - Retorna el HANDLE Win32 abierto para escribir los datos iniciales y sincronizar.
    - Si falla la creación -> Fail-Closed incondicional (sin fallbacks).
    """
    _ensure_windows()
    target_str = str(path)

    if _check_object_exists_no_reparse(target_str):
        raise TrustedNamespaceError(f"El archivo '{target_str}' ya existe; no se puede crear desde su nacimiento")

    with _build_canonical_security_descriptor(object_name) as sd_ctx:
        sa = _SecurityAttributes()
        sa.nLength = ctypes.sizeof(sa)
        sa.lpSecurityDescriptor = sd_ctx.p_sd
        sa.bInheritHandle = False

        desired_access = (
            _GENERIC_READ | _GENERIC_WRITE | _READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _FILE_READ_ATTRIBUTES
        )
        h = _kernel32.CreateFileW(
            target_str,
            desired_access,
            0,  # Acceso exclusivo durante la creación y escritura inicial
            ctypes.byref(sa),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if _is_invalid_handle(h):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(
                f"CreateFileW falló al crear '{target_str}' con Security Descriptor canónico: código {err}"
            )
        return int(h)


def verify_secured_file_by_handle(path: pathlib.Path | str) -> None:
    """Verifica mediante handle sin seguir reparse points que un archivo cumpla el contrato canónico.

    Verificaciones atadas a handle:
    1. No es un directorio.
    2. No es un reparse point (ReparseTag == 0 y sin FILE_ATTRIBUTE_REPARSE_POINT).
    3. Owner es LOCAL SYSTEM (S-1-5-18).
    4. Group es BUILTIN\\Administrators (S-1-5-32-544).
    5. DACL es protegida (SE_DACL_PROTECTED).
    Cualquier discrepancia lanza TrustedNamespaceError (Fail-Closed).
    """
    _ensure_windows()
    target_str = str(path)
    h = _open_handle_no_reparse(
        target_str,
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES,
        creation_disposition=_OPEN_EXISTING,
    )
    if _is_invalid_handle(h):
        err = ctypes.get_last_error()
        raise TrustedNamespaceError(f"CreateFileW falló al reabrir '{target_str}' para verificación: código {err}")

    try:
        if _read_handle_is_directory(h):
            raise NamespaceObjectNotDirectoryError(
                f"El objeto verificado '{target_str}' es un directorio, no un archivo"
            )

        reparse_tag, file_attrs = _read_handle_reparse_and_attributes(h)
        if reparse_tag != 0 or (file_attrs & _FILE_ATTRIBUTE_REPARSE_POINT):
            raise NamespaceReparsePointError(
                f"El archivo verificado '{target_str}' posee un reparse point (tag=0x{reparse_tag:08X})"
            )

        owner_sid, group_sid, is_protected = _read_live_owner_group_dacl(h)
        if owner_sid != CANONICAL_NAMESPACE_OWNER:
            raise TrustedNamespaceError(
                f"Owner no canónico en '{target_str}': esperado={CANONICAL_NAMESPACE_OWNER}, observado={owner_sid}"
            )
        if group_sid != CANONICAL_NAMESPACE_PRIMARY_GROUP:
            raise TrustedNamespaceError(
                f"Group no canónico en '{target_str}': esperado={CANONICAL_NAMESPACE_PRIMARY_GROUP}, observado={group_sid}"
            )
        if not is_protected:
            raise TrustedNamespaceError(f"DACL en '{target_str}' no tiene flag SE_DACL_PROTECTED activo")
    finally:
        _safe_close_handle(h)


def inspect_namespace_object(path: pathlib.Path | str) -> NamespaceObjectInfo:
    """Inspecciona atado a handle el tipo, reparse, owner y DACL de un objeto del namespace."""
    _ensure_windows()
    target = pathlib.Path(path)
    if not target.exists():
        raise TrustedNamespaceError(f"El objeto no existe: '{target}'")

    h = _open_handle_no_reparse(
        str(target),
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES,
        creation_disposition=_OPEN_EXISTING,
    )
    if _is_invalid_handle(h):
        err = ctypes.get_last_error()
        raise TrustedNamespaceError(f"CreateFileW falló al abrir '{target}': código {err}")

    try:
        is_dir = _read_handle_is_directory(h)
        reparse_tag, _ = _read_handle_reparse_and_attributes(h)
        owner_sid, group_sid, is_protected = _read_live_owner_group_dacl(h)
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


def apply_canonical_tgr_file_security(file_path: pathlib.Path | str) -> None:
    """Aplica la DACL y propietarios canónicos al archivo trusted_goldens.json.

    Exige derechos WRITE_OWNER y WRITE_DAC.
    Si falla la asignación de OWNER + GROUP + DACL + PROTECTED_DACL -> FAIL CLOSED (sin fallbacks DACL-only).
    """
    _ensure_windows()
    target_str = str(file_path)
    h = _open_handle_no_reparse(
        target_str,
        desired_access=_READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _FILE_READ_ATTRIBUTES,
        creation_disposition=_OPEN_EXISTING,
    )
    if _is_invalid_handle(h):
        err = ctypes.get_last_error()
        raise TrustedNamespaceError(f"CreateFileW falló al abrir '{target_str}' para seguridad: código {err}")

    try:
        p_canon_owner = wintypes.LPVOID()
        p_canon_group = wintypes.LPVOID()
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_canon_owner)):
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_OWNER")
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_canon_group)):
            _safe_local_free(p_canon_owner)
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_PRIMARY_GROUP")

        try:
            spec = build_namespace_dacl_spec("trusted_goldens.json")
            _, pacl, ace_psids = _build_native_acl_with_psids(spec.aces)
            try:
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
                if res != 0:
                    raise TrustedNamespaceError(
                        f"SetSecurityInfo falló al aplicar seguridad canónica sobre '{target_str}': código Win32 {res}"
                    )
            finally:
                for psid in ace_psids:
                    _safe_local_free(psid)
        finally:
            _safe_local_free(p_canon_owner)
            _safe_local_free(p_canon_group)
    finally:
        _safe_close_handle(h)


def _provision_or_normalize_directory(
    dir_path: pathlib.Path,
    object_name: str,
    is_root: bool = False,
) -> None:
    """Aprovisiona o normaliza de forma segura un directorio del namespace (ADR 0010 §11.3 Caso A / Caso B)."""
    _ensure_windows()
    dir_str = str(dir_path)

    # Asegurar que el padre existe
    dir_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Comprobar si ya existe abriendo sin seguir reparse
    h = _open_handle_no_reparse(
        dir_str,
        desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES,
        creation_disposition=_OPEN_EXISTING,
    )
    last_err = ctypes.get_last_error()
    exists = not _is_invalid_handle(h)

    if not exists:
        if last_err not in (2, 3):  # 2: ERROR_FILE_NOT_FOUND, 3: ERROR_PATH_NOT_FOUND
            raise NamespaceOwnerNotPermittedError(
                f"No se pudo acceder de forma segura al objeto preexistente '{dir_str}': código Win32 {last_err}"
            )

        # Caso A: La ruta no existe -> crear desde su nacimiento con SECURITY_ATTRIBUTES canónico
        with _build_canonical_security_descriptor(object_name) as sd_ctx:
            sa = _SecurityAttributes()
            sa.nLength = ctypes.sizeof(sa)
            sa.lpSecurityDescriptor = sd_ctx.p_sd
            sa.bInheritHandle = False

            if not _kernel32.CreateDirectoryW(dir_str, ctypes.byref(sa)):
                err = ctypes.get_last_error()
                raise TrustedNamespaceError(f"CreateDirectoryW falló al crear '{dir_str}': código {err}")

        # Reabrir handle para garantizar PROTECTED_DACL_SECURITY_INFORMATION vía SetSecurityInfo
        h_new = _open_handle_no_reparse(
            dir_str,
            desired_access=_READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _FILE_READ_ATTRIBUTES,
            creation_disposition=_OPEN_EXISTING,
        )
        if _is_invalid_handle(h_new):
            err = ctypes.get_last_error()
            raise TrustedNamespaceError(f"CreateFileW falló al reabrir '{dir_str}' tras creación: código {err}")
        try:
            p_canon_owner = wintypes.LPVOID()
            p_canon_group = wintypes.LPVOID()
            if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_canon_owner)):
                raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_OWNER")
            if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_canon_group)):
                _safe_local_free(p_canon_owner)
                raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_PRIMARY_GROUP")
            try:
                spec = build_namespace_dacl_spec(object_name)
                _, pacl, ace_psids = _build_native_acl_with_psids(spec.aces)
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
                    if res_set != 0:
                        raise TrustedNamespaceError(
                            f"SetSecurityInfo falló tras creación en '{dir_str}': código Win32 {res_set}"
                        )
                finally:
                    for psid in ace_psids:
                        _safe_local_free(psid)
            finally:
                _safe_local_free(p_canon_owner)
                _safe_local_free(p_canon_group)
        finally:
            _safe_close_handle(h_new)

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
        live_owner, live_group, is_protected = _read_live_owner_group_dacl(h)

        # (5) Verificar que el owner pertenece al conjunto permitido (SYSTEM/Administrators)
        if live_owner not in PERMITTED_NAMESPACE_OWNERS:
            raise NamespaceOwnerNotPermittedError(
                f"Propietario no permitido en '{dir_str}': owner={live_owner} no pertenece a {PERMITTED_NAMESPACE_OWNERS}"
            )

        # (6) Normalizar de forma privilegiada: OWNER + GROUP + DACL al estado canónico
        # Normaliza owner estrictamente a CANONICAL_NAMESPACE_OWNER (SYSTEM) y group a CANONICAL_NAMESPACE_PRIMARY_GROUP (Administrators)
        p_canon_owner = wintypes.LPVOID()
        p_canon_group = wintypes.LPVOID()
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_canon_owner)):
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_OWNER")
        if not _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_canon_group)):
            _safe_local_free(p_canon_owner)
            raise TrustedNamespaceError("ConvertStringSidToSidW falló para CANONICAL_NAMESPACE_PRIMARY_GROUP")

        try:
            spec = build_namespace_dacl_spec(object_name)
            _, pacl, ace_psids = _build_native_acl_with_psids(spec.aces)
            try:
                # Abrir handle con WRITE_DAC y WRITE_OWNER para SetSecurityInfo
                h_mutate = _open_handle_no_reparse(
                    dir_str,
                    desired_access=_READ_CONTROL | _WRITE_DAC | _WRITE_OWNER | _FILE_READ_ATTRIBUTES,
                    creation_disposition=_OPEN_EXISTING,
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
                    if res != 0:
                        raise TrustedNamespaceError(
                            f"SetSecurityInfo falló al normalizar '{dir_str}': código Win32 {res}"
                        )
                finally:
                    _safe_close_handle(h_mutate)
            finally:
                for psid in ace_psids:
                    _safe_local_free(psid)
        finally:
            _safe_local_free(p_canon_owner)
            _safe_local_free(p_canon_group)
    finally:
        _safe_close_handle(h)


# ============================================================================
# Orquestador del Bootstrap de Namespace (Sin Seams de Test)
# ============================================================================


def bootstrap_trusted_namespace(
    root_dir: pathlib.Path | str | None = None,
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
        root_path = pathlib.Path(root_dir)

    # 1. Ancestro 1: Sky-Claw/
    try:
        _provision_or_normalize_directory(root_path, "Sky-Claw", is_root=True)
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
        _provision_or_normalize_directory(rv_dir, "runtime_vault")
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
        _provision_or_normalize_directory(subdir_path, obj_name)

    # 4. Archivo trust root: trusted_goldens.json
    tgr_file = rv_dir / "trusted_goldens.json"
    if _check_object_exists_no_reparse(tgr_file):
        # Excepción dura: Preexistente -> SIEMPRE FAIL CLOSED (Caso B.7)
        raise PreexistingTrustedRegistryError(
            f"trusted_goldens.json preexistente en '{tgr_file}'. El bootstrap inicial rechaza registries no creados por él."
        )

    # Caso A: Creación inicial vacía canónica desde su nacimiento
    empty_reg = TrustedGoldenRegistry(entries=(), schema_version="1.0")
    write_trusted_registry_atomically(empty_reg, tgr_file)

    return TrustedNamespaceResult(
        success=True,
        message="",
        root_path=root_path,
    )


# Import tardío de TrustedGoldenRegistry y write_trusted_registry_atomically
from sky_claw.local.runtime_vault.trusted_registry import (  # noqa: E402
    TrustedGoldenRegistry,
    write_trusted_registry_atomically,
)

__all__ = [
    "AUTHENTICATED_USERS_SID",
    "AncestorProvisioningError",
    "BUILTIN_ADMINISTRATORS_SID",
    "CANONICAL_NAMESPACE_OWNER",
    "CANONICAL_NAMESPACE_PRIMARY_GROUP",
    "CREATOR_OWNER_SID",
    "CanonicalSecurityDescriptorContext",
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
    "create_secured_file_from_birth",
    "inspect_namespace_object",
    "verify_secured_file_by_handle",
]
