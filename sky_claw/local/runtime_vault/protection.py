"""Inspección read-only del estado de protección filesystem del Golden Master (RV-GP1).

Implementa el contrato canónico definido en ADR 0009 (docs/adr/0009-runtime-vault-golden-protection-status.md).

Principios normativos:
- READ-ONLY FIRST: prohibida cualquier prueba mutadora (sin archivos probe, sin escrituras).
- FAIL CLOSED: evidencia ausente, incompleta o ambigua produce UNKNOWN.
- NO FALSE GREEN: AccessCheck nativo + evaluación del token efectivo actual.
- LOCALIDAD Y CAPABILITY: sólo volúmenes locales DRIVE_FIXED con filesystem NTFS y FILE_PERSISTENT_ACLS.
- ORTOGONALIDAD: RV-1/RV-2/RV-3 no dependen de este módulo.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sky_claw.app.security.links import (
    link_kind_and_identity_or_raise,
    reparse_tag_or_zero,
    same_file_identity,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError

if TYPE_CHECKING:
    pass


class GoldenProtectionInputError(RuntimeVaultError):
    """Excepción de dominio para errores de entrada de programador al invocar protección."""


class GoldenProtectionState(StrEnum):
    """Estados normativos de protección del Golden Master."""

    UNSUPPORTED = "unsupported"
    UNPROTECTED = "unprotected"
    WRITE_PROTECTED = "write_protected"
    HARDENED = "hardened"
    UNKNOWN = "unknown"


class GoldenProtectionRight(StrEnum):
    """Derechos efectivos relevantes resueltos por AccessCheck por nodo."""

    READ_DATA = "read_data"  # FILE_READ_DATA / FILE_LIST_DIRECTORY
    EXECUTE = "execute"  # FILE_EXECUTE / FILE_TRAVERSE
    WRITE_DATA = "write_data"  # FILE_WRITE_DATA sobre archivo
    APPEND_DATA = "append_data"  # FILE_APPEND_DATA sobre archivo
    ADD_FILE = "add_file"  # FILE_ADD_FILE sobre directorio
    ADD_SUBDIRECTORY = "add_subdirectory"  # FILE_ADD_SUBDIRECTORY sobre directorio
    DELETE = "delete"  # DELETE del nodo
    DELETE_CHILD = "delete_child"  # FILE_DELETE_CHILD del directorio
    WRITE_METADATA = "write_metadata"  # FILE_WRITE_ATTRIBUTES + FILE_WRITE_EA
    CHANGE_PERMISSIONS = "change_permissions"  # WRITE_DAC
    CHANGE_OWNER = "change_owner"  # WRITE_OWNER


@dataclass(frozen=True, slots=True)
class NodeProtectionObservation:
    """Evidencia observada de un nodo individual (archivo o directorio)."""

    relative_path: str
    node_kind: str  # "dir" | "file"
    owner_sid: str | None
    granted_rights: frozenset[GoldenProtectionRight] | None
    owner_rights_ace_present: bool | None = None
    dacl_inheritance_protected: bool | None = None


@dataclass(frozen=True, slots=True)
class _TokenInspection:
    """Resultado estructurado completo de la inspección del token nativo."""

    user_sid: str
    group_sids: frozenset[str]
    elevated: bool
    elevation_type: int | None
    mutation_privileges: frozenset[str]
    diagnostic_privileges: frozenset[str]


@dataclass(frozen=True, slots=True)
class GoldenProtectionEvidence:
    """Evidencia agregada recopilada durante la inspección del Golden Master."""

    platform: str
    drive_type: int | None = None
    filesystem: str | None = None
    filesystem_persistent_acls: bool | None = None
    current_user_sid: str | None = None
    current_group_sids: frozenset[str] = frozenset()
    current_token_elevated: bool | None = None
    token_elevation_type: int | None = None
    mutation_privileges_present: frozenset[str] = frozenset()
    diagnostic_privileges_present: frozenset[str] = frozenset()
    nodes: tuple[NodeProtectionObservation, ...] | None = None
    parent_observation: NodeProtectionObservation | None = None
    pre_post_structural_match: bool | None = None
    observation_error: str | None = None


@dataclass(frozen=True, slots=True)
class GoldenProtectionResult:
    """Resultado final de la inspección del estado de protección del Golden Master."""

    state: GoldenProtectionState
    evidence: GoldenProtectionEvidence
    message: str = ""
    scan_scope: str = "FULL_SUBTREE_AND_PARENT"
    assurance_scope: str = "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"

    def __post_init__(self) -> None:
        if self.state is GoldenProtectionState.UNKNOWN:
            if not self.message:
                raise ValueError("Estado UNKNOWN exige message explicativo no vacío")
        else:
            if self.message != "":
                raise ValueError("message debe ser vacío para estados definitivos de éxito")

    @property
    def success(self) -> bool:
        """True sólo para estados concluyentes (UNSUPPORTED, UNPROTECTED, WRITE_PROTECTED, HARDENED)."""
        return self.state is not GoldenProtectionState.UNKNOWN


# Constantes y mappings Win32 nativos
_DRIVE_FIXED = 3
_FILE_PERSISTENT_ACLS = 0x00000008
_ERROR_NO_TOKEN = 1008
_SE_DACL_PROTECTED = 0x1000

_MUTATION_ENABLING_PRIVILEGES = frozenset({"SeRestorePrivilege", "SeTakeOwnershipPrivilege"})
_DIAGNOSTIC_PRIVILEGES = frozenset({"SeBackupPrivilege", "SeSecurityPrivilege"})

_OWNER_RIGHTS_SID = "S-1-3-4"

_FILE_MUTATION_RIGHTS = frozenset(
    {
        GoldenProtectionRight.WRITE_DATA,
        GoldenProtectionRight.APPEND_DATA,
        GoldenProtectionRight.DELETE,
        GoldenProtectionRight.WRITE_METADATA,
        GoldenProtectionRight.CHANGE_PERMISSIONS,
        GoldenProtectionRight.CHANGE_OWNER,
    }
)

_DIR_MUTATION_RIGHTS = frozenset(
    {
        GoldenProtectionRight.ADD_FILE,
        GoldenProtectionRight.ADD_SUBDIRECTORY,
        GoldenProtectionRight.DELETE,
        GoldenProtectionRight.DELETE_CHILD,
        GoldenProtectionRight.WRITE_METADATA,
        GoldenProtectionRight.CHANGE_PERMISSIONS,
        GoldenProtectionRight.CHANGE_OWNER,
    }
)


def _find_root_observation(
    nodes: Sequence[NodeProtectionObservation],
) -> tuple[NodeProtectionObservation | None, str | None]:
    """Busca y valida exactamente una observación del root canónico ('.' o '')."""
    roots = [n for n in nodes if n.relative_path in (".", "")]
    if not roots:
        return None, "No se encontró observación del root en la evidencia"
    if len(roots) > 1:
        return None, f"Se encontraron múltiples observaciones de root ({len(roots)}) en la evidencia"
    root = roots[0]
    if root.node_kind != "dir":
        return None, f"El root observado debe ser de tipo directorio ('dir'), se obtuvo '{root.node_kind}'"
    return root, None


def classify_protection(evidence: GoldenProtectionEvidence) -> GoldenProtectionResult:
    """Clasificador determinista puro sobre la evidencia observada (sin I/O)."""
    # 1. Capability gates de plataforma y filesystem
    if evidence.platform != "windows":
        return GoldenProtectionResult(state=GoldenProtectionState.UNSUPPORTED, evidence=evidence, message="")

    if evidence.drive_type != _DRIVE_FIXED:
        return GoldenProtectionResult(state=GoldenProtectionState.UNSUPPORTED, evidence=evidence, message="")

    if evidence.filesystem != "NTFS" or not evidence.filesystem_persistent_acls:
        return GoldenProtectionResult(state=GoldenProtectionState.UNSUPPORTED, evidence=evidence, message="")

    # 2. Fallas de observación / drift / enlaces / cardinalidad de evidencia
    if evidence.observation_error is not None:
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNKNOWN,
            evidence=evidence,
            message=evidence.observation_error,
        )

    if evidence.pre_post_structural_match is not True or not evidence.nodes:
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNKNOWN,
            evidence=evidence,
            message="Observación no sellada o incompleta: drift, fallo estructural o lista de nodos vacía",
        )

    if evidence.parent_observation is None:
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNKNOWN,
            evidence=evidence,
            message="Evidencia incompleta: parent_observation es requerido para evaluar alcance protegido",
        )

    if evidence.current_user_sid is None:
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNKNOWN,
            evidence=evidence,
            message="No se pudo determinar el SID del usuario en el token efectivo",
        )

    # 3. Validar que todos los nodos tienen evidencia completa
    for nodo in evidence.nodes:
        if (
            nodo.granted_rights is None
            or nodo.owner_sid is None
            or nodo.owner_rights_ace_present is None
            or nodo.dacl_inheritance_protected is None
        ):
            return GoldenProtectionResult(
                state=GoldenProtectionState.UNKNOWN,
                evidence=evidence,
                message=f"Evidencia incompleta en nodo '{nodo.relative_path}'",
            )

    if (
        evidence.parent_observation.granted_rights is None
        or evidence.parent_observation.owner_sid is None
        or evidence.parent_observation.owner_rights_ace_present is None
        or evidence.parent_observation.dacl_inheritance_protected is None
    ):
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNKNOWN,
            evidence=evidence,
            message="Evidencia incompleta en parent del Golden",
        )

    # Identificar y validar observación exacta de Root
    root_obs, root_err = _find_root_observation(evidence.nodes)
    if root_obs is None or root_err:
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNKNOWN,
            evidence=evidence,
            message=root_err or "Observación de root ausente o ambigua",
        )

    # 4. Privilegios mutation-enabling en el token
    if evidence.mutation_privileges_present:
        return GoldenProtectionResult(state=GoldenProtectionState.UNPROTECTED, evidence=evidence, message="")

    # 5. Comprobar derechos mutadores por nodo del árbol
    for nodo in evidence.nodes:
        rights = nodo.granted_rights
        assert rights is not None
        if nodo.node_kind == "file":
            if any(r in rights for r in _FILE_MUTATION_RIGHTS):
                return GoldenProtectionResult(state=GoldenProtectionState.UNPROTECTED, evidence=evidence, message="")
        else:
            if any(r in rights for r in _DIR_MUTATION_RIGHTS):
                return GoldenProtectionResult(state=GoldenProtectionState.UNPROTECTED, evidence=evidence, message="")

        # Chequeo owner self-rewrite: si el usuario es owner y tiene CHANGE_PERMISSIONS (WRITE_DAC)
        if nodo.owner_sid is not None:
            es_owner = nodo.owner_sid == evidence.current_user_sid or nodo.owner_sid in evidence.current_group_sids
            if es_owner and GoldenProtectionRight.CHANGE_PERMISSIONS in rights and not nodo.owner_rights_ace_present:
                return GoldenProtectionResult(state=GoldenProtectionState.UNPROTECTED, evidence=evidence, message="")

    # 6. Comprobar superficie del Parent y escalación por herencia
    parent_obs = evidence.parent_observation
    parent_rights = parent_obs.granted_rights
    assert parent_rights is not None

    root_rights = root_obs.granted_rights or frozenset()

    # 6a. Delete del root: parent DELETE_CHILD o root DELETE
    if GoldenProtectionRight.DELETE_CHILD in parent_rights:
        return GoldenProtectionResult(state=GoldenProtectionState.UNPROTECTED, evidence=evidence, message="")

    # 6b. Rename del root: root deletable AND parent add_file/add_sub
    root_deletable = (GoldenProtectionRight.DELETE_CHILD in parent_rights) or (
        GoldenProtectionRight.DELETE in root_rights
    )
    if root_deletable and (
        GoldenProtectionRight.ADD_FILE in parent_rights or GoldenProtectionRight.ADD_SUBDIRECTORY in parent_rights
    ):
        return GoldenProtectionResult(state=GoldenProtectionState.UNPROTECTED, evidence=evidence, message="")

    # 6c. Escalación de permisos/propietario desde el parent.
    # CHANGE_PERMISSIONS (WRITE_DAC efectivo sobre el parent) es una capacidad DIRECTA: AccessCheck
    # ya lo acredita en granted_rights (una Owner Rights ACE que lo suprimiera lo habría quitado del
    # conjunto efectivo). El token puede reescribir la DACL del parent y auto-concederse
    # FILE_DELETE_CHILD, que borra hijos IGNORANDO su propia DACL. Esa vía existe aunque el root esté
    # protegido contra herencia (dacl_inheritance_protected=True) — FILE_DELETE_CHILD sobre el parent
    # no consulta la DACL del root — así que NO se condiciona a la protección de herencia del root
    # (ver M-GP1-30 heredado / M-GP1-32 protegido).
    if GoldenProtectionRight.CHANGE_PERMISSIONS in parent_rights:
        return GoldenProtectionResult(state=GoldenProtectionState.UNPROTECTED, evidence=evidence, message="")
    # CHANGE_OWNER (WRITE_OWNER) es INDIRECTO: solo permite TOMAR POSESIÓN del parent. El nuevo dueño
    # obtiene WRITE_DAC IMPLÍCITO —y con él la vía a FILE_DELETE_CHILD— únicamente si NO hay una Owner
    # Rights ACE (S-1-3-4) en el parent; esa ACE suprime el grant implícito del dueño (MS-DTYP). Con
    # la ACE presente, WRITE_OWNER por sí solo no prueba mutación, así que se condiciona a su ausencia
    # (ver M-GP1-31/55 sin la ACE -> UNPROTECTED; M-GP1-56 con la ACE -> no UNPROTECTED).
    if GoldenProtectionRight.CHANGE_OWNER in parent_rights and not parent_obs.owner_rights_ace_present:
        return GoldenProtectionResult(state=GoldenProtectionState.UNPROTECTED, evidence=evidence, message="")

    # 7. Si llegamos acá, el árbol es WRITE_PROTECTED. Evaluar HARDENED
    # HARDENED requiere:
    # a. Token no elevado (TokenElevation == False)
    # b. Ningún owner atribuible con vía de reescritura
    if evidence.current_token_elevated is True or evidence.current_token_elevated is None:
        return GoldenProtectionResult(state=GoldenProtectionState.WRITE_PROTECTED, evidence=evidence, message="")

    for nodo in evidence.nodes:
        if nodo.owner_sid is not None:
            es_owner = nodo.owner_sid == evidence.current_user_sid or nodo.owner_sid in evidence.current_group_sids
            if es_owner and not nodo.owner_rights_ace_present:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.WRITE_PROTECTED, evidence=evidence, message=""
                )

    return GoldenProtectionResult(
        state=GoldenProtectionState.HARDENED,
        evidence=evidence,
        message="",
        assurance_scope="CURRENT_EFFECTIVE_UNELEVATED_TOKEN",
        scan_scope="FULL_SUBTREE_AND_PARENT",
    )


# ============================================================================
# Backend Win32 ctypes (aislado para plataforma Windows)
# ============================================================================

if sys.platform == "win32":
    from ctypes import wintypes

    # Win32 Structs
    class _Luid(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class _LuidAndAttributes(ctypes.Structure):
        _fields_ = [("Luid", _Luid), ("Attributes", wintypes.DWORD)]

    class _TokenPrivileges(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", _LuidAndAttributes * 1)]

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAndAttributes)]

    class _TokenGroups(ctypes.Structure):
        _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", _SidAndAttributes * 1)]

    class _TokenElevation(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    class _GenericMapping(ctypes.Structure):
        _fields_ = [
            ("GenericRead", wintypes.DWORD),
            ("GenericWrite", wintypes.DWORD),
            ("GenericExecute", wintypes.DWORD),
            ("GenericAll", wintypes.DWORD),
        ]

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    class _AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("Header", _AceHeader),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    # Setup DLLs and signatures
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _advapi32.OpenThreadToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.BOOL,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenThreadToken.restype = wintypes.BOOL

    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL

    _advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.DuplicateTokenEx.restype = wintypes.BOOL

    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL

    _advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_Luid)]
    _advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL

    _advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD

    _advapi32.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL

    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL

    _advapi32.GetAclInformation.argtypes = [wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD]
    _advapi32.GetAclInformation.restype = wintypes.BOOL

    _advapi32.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    _advapi32.GetAce.restype = wintypes.BOOL

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

    _kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetDriveTypeW.restype = wintypes.UINT

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

    _kernel32.GetVolumeInformationByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _kernel32.GetVolumeInformationByHandleW.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    _kernel32.LocalFree.restype = wintypes.LPVOID

    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    _kernel32.GetCurrentThread.argtypes = []
    _kernel32.GetCurrentThread.restype = wintypes.HANDLE


def _sid_to_str(psid: Any) -> str | None:
    if not psid or sys.platform != "win32":
        return None
    str_ptr = wintypes.LPWSTR()
    if _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(str_ptr)):
        val = str_ptr.value
        _kernel32.LocalFree(str_ptr)
        return val
    return None


def _obtener_token_efectivo() -> tuple[Any, Any]:
    """Obtiene y duplica el token de impersonación efectivo del thread/proceso."""
    if sys.platform != "win32":
        return None, None

    token_raw = wintypes.HANDLE()
    token_query_dup = 0x0008 | 0x0002  # TOKEN_QUERY | TOKEN_DUPLICATE
    res = _advapi32.OpenThreadToken(_kernel32.GetCurrentThread(), token_query_dup, True, ctypes.byref(token_raw))
    if not res:
        err = ctypes.get_last_error()
        if err == _ERROR_NO_TOKEN:
            # 2. Fallback a token del proceso únicamente si el thread no tiene token de impersonación
            res = _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), token_query_dup, ctypes.byref(token_raw))
            if not res:
                return None, None
        else:
            # El thread tiene token pero falló la apertura -> fail-closed, no caer al proceso
            return None, None

    # Duplicar a SecurityImpersonation / TokenImpersonation
    token_imp = wintypes.HANDLE()
    security_impersonation = 2
    token_impersonation = 2
    res_dup = _advapi32.DuplicateTokenEx(
        token_raw,
        0x0008,  # TOKEN_QUERY
        None,
        security_impersonation,
        token_impersonation,
        ctypes.byref(token_imp),
    )
    if not res_dup:
        _kernel32.CloseHandle(token_raw)
        return None, None

    return token_raw, token_imp


def _inspeccionar_token(
    token_raw: Any,
) -> tuple[_TokenInspection | None, str | None]:
    """Inspecciona user, groups, elevación y privilegios del token con canal de error explícito."""
    if sys.platform != "win32" or not token_raw:
        return None, "Token inválido o plataforma no Windows"

    # User SID
    user_sid_str: str | None = None
    buf_size = wintypes.DWORD()
    _advapi32.GetTokenInformation(token_raw, 1, None, 0, ctypes.byref(buf_size))  # TokenUser = 1
    if buf_size.value <= 0:
        err = ctypes.get_last_error()
        return None, f"GetTokenInformation(TokenUser tamaño) falló con error {err}"
    buf_user = ctypes.create_string_buffer(buf_size.value)
    if not _advapi32.GetTokenInformation(token_raw, 1, buf_user, buf_size.value, ctypes.byref(buf_size)):
        err = ctypes.get_last_error()
        return None, f"GetTokenInformation(TokenUser) falló con error {err}"
    user_struct = ctypes.cast(buf_user, ctypes.POINTER(_TokenUser)).contents
    user_sid_str = _sid_to_str(user_struct.User.Sid)
    if not user_sid_str:
        return None, "No se pudo convertir el SID del usuario a cadena"

    # Groups SIDs (excluyendo deny-only)
    group_sids: set[str] = set()
    buf_size_grp = wintypes.DWORD()
    _advapi32.GetTokenInformation(token_raw, 2, None, 0, ctypes.byref(buf_size_grp))  # TokenGroups = 2
    if buf_size_grp.value <= 0:
        err = ctypes.get_last_error()
        return None, f"GetTokenInformation(TokenGroups tamaño) falló con error {err}"
    buf_groups = ctypes.create_string_buffer(buf_size_grp.value)
    if not _advapi32.GetTokenInformation(token_raw, 2, buf_groups, buf_size_grp.value, ctypes.byref(buf_size_grp)):
        err = ctypes.get_last_error()
        return None, f"GetTokenInformation(TokenGroups) falló con error {err}"
    tg = ctypes.cast(buf_groups, ctypes.POINTER(_TokenGroups)).contents
    count = tg.GroupCount
    groups_ptr = ctypes.cast(ctypes.byref(tg.Groups), ctypes.POINTER(_SidAndAttributes))
    for i in range(count):
        item = groups_ptr[i]
        is_deny_only = bool(item.Attributes & 0x00000010)  # SE_GROUP_USE_FOR_DENY_ONLY
        if not is_deny_only:
            sid_s = _sid_to_str(item.Sid)
            if sid_s:
                group_sids.add(sid_s)

    # Elevation
    elev_struct = _TokenElevation()
    ret_len = wintypes.DWORD()
    if not _advapi32.GetTokenInformation(
        token_raw, 20, ctypes.byref(elev_struct), ctypes.sizeof(elev_struct), ctypes.byref(ret_len)
    ):  # TokenElevation = 20
        err = ctypes.get_last_error()
        return None, f"GetTokenInformation(TokenElevation) falló con error {err}"
    is_elevated = bool(elev_struct.TokenIsElevated != 0)

    # ElevationType (diagnóstico)
    elev_type: int | None = None
    elev_type_val = wintypes.DWORD()
    if _advapi32.GetTokenInformation(
        token_raw, 18, ctypes.byref(elev_type_val), ctypes.sizeof(elev_type_val), ctypes.byref(ret_len)
    ):  # TokenElevationType = 18
        elev_type = elev_type_val.value

    # Privileges
    mutation_privs: set[str] = set()
    diag_privs: set[str] = set()

    # Mapear LUIDs de privilegios conocidos
    known_luids: dict[str, _Luid] = {}
    for priv_name in _MUTATION_ENABLING_PRIVILEGES | _DIAGNOSTIC_PRIVILEGES:
        luid = _Luid()
        if not _advapi32.LookupPrivilegeValueW(None, priv_name, ctypes.byref(luid)):
            err = ctypes.get_last_error()
            return None, f"LookupPrivilegeValueW({priv_name}) falló con error {err}"
        known_luids[priv_name] = luid

    buf_size_priv = wintypes.DWORD()
    _advapi32.GetTokenInformation(token_raw, 3, None, 0, ctypes.byref(buf_size_priv))  # TokenPrivileges = 3
    if buf_size_priv.value <= 0:
        err = ctypes.get_last_error()
        return None, f"GetTokenInformation(TokenPrivileges tamaño) falló con error {err}"
    buf_privs = ctypes.create_string_buffer(buf_size_priv.value)
    if not _advapi32.GetTokenInformation(token_raw, 3, buf_privs, buf_size_priv.value, ctypes.byref(buf_size_priv)):
        err = ctypes.get_last_error()
        return None, f"GetTokenInformation(TokenPrivileges) falló con error {err}"
    tp = ctypes.cast(buf_privs, ctypes.POINTER(_TokenPrivileges)).contents
    priv_count = tp.PrivilegeCount
    priv_ptr = ctypes.cast(ctypes.byref(tp.Privileges), ctypes.POINTER(_LuidAndAttributes))
    for i in range(priv_count):
        entry = priv_ptr[i]
        for name, luid in known_luids.items():
            if entry.Luid.LowPart == luid.LowPart and entry.Luid.HighPart == luid.HighPart:
                if name in _MUTATION_ENABLING_PRIVILEGES:
                    mutation_privs.add(name)
                elif name in _DIAGNOSTIC_PRIVILEGES:
                    diag_privs.add(name)

    return (
        _TokenInspection(
            user_sid=user_sid_str,
            group_sids=frozenset(group_sids),
            elevated=is_elevated,
            elevation_type=elev_type,
            mutation_privileges=frozenset(mutation_privs),
            diagnostic_privileges=frozenset(diag_privs),
        ),
        None,
    )


def _evaluar_acceso_nodo(
    path: pathlib.Path,
    node_kind: str,
    token_imp: Any,
) -> tuple[NodeProtectionObservation | None, str | None]:
    """Evalúa Security Descriptor y AccessCheck sobre un nodo específico."""
    if sys.platform != "win32":
        return None, "Plataforma no Windows"

    owner_ptr = wintypes.LPVOID()
    group_ptr = wintypes.LPVOID()
    dacl_ptr = wintypes.LPVOID()
    sd_ptr = wintypes.LPVOID()

    flags = 0x00000001 | 0x00000002 | 0x00000004  # OWNER | GROUP | DACL
    err = _advapi32.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        flags,
        ctypes.byref(owner_ptr),
        ctypes.byref(group_ptr),
        ctypes.byref(dacl_ptr),
        None,
        ctypes.byref(sd_ptr),
    )
    if err != 0:
        return None, f"GetNamedSecurityInfoW falló con error {err} en '{path}'"

    try:
        # Owner SID
        owner_sid = _sid_to_str(owner_ptr)

        # Control y protección contra herencia (SE_DACL_PROTECTED = 0x1000)
        sd_control = wintypes.WORD()
        sd_rev = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(sd_ptr, ctypes.byref(sd_control), ctypes.byref(sd_rev)):
            err = ctypes.get_last_error()
            return None, f"GetSecurityDescriptorControl falló con error {err} en '{path}'"

        dacl_inheritance_protected = bool(sd_control.value & _SE_DACL_PROTECTED)

        # DACL inspection
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl_out = wintypes.LPVOID()
        if not _advapi32.GetSecurityDescriptorDacl(
            sd_ptr,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl_out),
            ctypes.byref(dacl_defaulted),
        ):
            err = ctypes.get_last_error()
            return None, f"GetSecurityDescriptorDacl falló con error {err} en '{path}'"

        owner_rights_ace_present: bool | None = False
        if dacl_present.value != 0 and dacl_out.value:
            acl_info = _AclSizeInformation()
            if not _advapi32.GetAclInformation(
                dacl_out, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2
            ):  # AclSizeInformation = 2
                err = ctypes.get_last_error()
                return None, f"GetAclInformation falló con error {err} en '{path}'"

            for i in range(acl_info.AceCount):
                ace_ptr = wintypes.LPVOID()
                if not _advapi32.GetAce(dacl_out, i, ctypes.byref(ace_ptr)) or not ace_ptr.value:
                    err = ctypes.get_last_error()
                    return None, f"GetAce({i}) falló con error {err} en '{path}'"

                header = ctypes.cast(ace_ptr, ctypes.POINTER(_AceHeader)).contents
                if header.AceType in (0, 1):  # ACCESS_ALLOWED / ACCESS_DENIED
                    ace_raw = ctypes.cast(ace_ptr, ctypes.c_void_p).value
                    if ace_raw:
                        # SidStart está a offset 8 (Header 4 bytes + Mask 4 bytes)
                        ace_sid_ptr = ace_raw + 8
                        ace_sid_str = _sid_to_str(ace_sid_ptr)
                        if ace_sid_str == _OWNER_RIGHTS_SID:
                            owner_rights_ace_present = True

        # Generic Mapping
        # Standard Rights: Read/Write/Execute = 0x00020000 (READ_CONTROL)
        # SYNCHRONIZE = 0x00100000
        mapping = _GenericMapping()
        if node_kind == "file":
            mapping.GenericRead = 0x00120089  # FILE_GENERIC_READ
            mapping.GenericWrite = 0x00120116  # FILE_GENERIC_WRITE
            mapping.GenericExecute = 0x001200A0  # FILE_GENERIC_EXECUTE
            mapping.GenericAll = 0x001F01FF  # FILE_ALL_ACCESS
        else:
            mapping.GenericRead = 0x00120089  # LIST_DIRECTORY | READ_ATTRIBUTES | READ_EA | READ_CONTROL | SYNCHRONIZE
            mapping.GenericWrite = (
                0x00120116  # ADD_FILE | ADD_SUBDIR | WRITE_ATTRIBUTES | WRITE_EA | WRITE_DAC | SYNCHRONIZE
            )
            mapping.GenericExecute = 0x001200A0  # TRAVERSE | READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE
            mapping.GenericAll = 0x001F01FF

        priv_set_buf = ctypes.create_string_buffer(1024)
        priv_set_len = wintypes.DWORD(1024)
        granted_mask = wintypes.DWORD()
        access_status = wintypes.BOOL()

        desired_access = 0x02000000  # MAXIMUM_ALLOWED
        chk_res = _advapi32.AccessCheck(
            sd_ptr,
            token_imp,
            desired_access,
            ctypes.byref(mapping),
            priv_set_buf,
            ctypes.byref(priv_set_len),
            ctypes.byref(granted_mask),
            ctypes.byref(access_status),
        )
        if not chk_res:
            err = ctypes.get_last_error()
            return None, f"AccessCheck falló con código {err} en '{path}'"

        mask = granted_mask.value if access_status.value != 0 else 0

        # Mapear bits de máscara a derechos
        rights: set[GoldenProtectionRight] = set()
        if node_kind == "file":
            if mask & 0x0001:  # FILE_READ_DATA
                rights.add(GoldenProtectionRight.READ_DATA)
            if mask & 0x0020:  # FILE_EXECUTE
                rights.add(GoldenProtectionRight.EXECUTE)
            if mask & 0x0002:  # FILE_WRITE_DATA
                rights.add(GoldenProtectionRight.WRITE_DATA)
            if mask & 0x0004:  # FILE_APPEND_DATA
                rights.add(GoldenProtectionRight.APPEND_DATA)
        else:
            if mask & 0x0001:  # FILE_LIST_DIRECTORY
                rights.add(GoldenProtectionRight.READ_DATA)
            if mask & 0x0020:  # FILE_TRAVERSE
                rights.add(GoldenProtectionRight.EXECUTE)
            if mask & 0x0002:  # FILE_ADD_FILE
                rights.add(GoldenProtectionRight.ADD_FILE)
            if mask & 0x0004:  # FILE_ADD_SUBDIRECTORY
                rights.add(GoldenProtectionRight.ADD_SUBDIRECTORY)
            if mask & 0x0040:  # FILE_DELETE_CHILD
                rights.add(GoldenProtectionRight.DELETE_CHILD)

        if mask & 0x00010000:  # DELETE
            rights.add(GoldenProtectionRight.DELETE)
        if mask & (0x0100 | 0x0010):  # FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA
            rights.add(GoldenProtectionRight.WRITE_METADATA)
        if mask & 0x00040000:  # WRITE_DAC
            rights.add(GoldenProtectionRight.CHANGE_PERMISSIONS)
        if mask & 0x00080000:  # WRITE_OWNER
            rights.add(GoldenProtectionRight.CHANGE_OWNER)

        return (
            NodeProtectionObservation(
                relative_path="",
                node_kind=node_kind,
                owner_sid=owner_sid,
                granted_rights=frozenset(rights),
                owner_rights_ace_present=owner_rights_ace_present,
                dacl_inheritance_protected=dacl_inheritance_protected,
            ),
            None,
        )

    finally:
        _kernel32.LocalFree(sd_ptr)


def inspect_golden_protection(path: pathlib.Path) -> GoldenProtectionResult:
    """Inspecciona el estado de protección del Golden Master de forma estrictamente read-only."""
    # 1. Programmer input checks
    if not isinstance(path, pathlib.Path) or not path.is_absolute():
        raise GoldenProtectionInputError(f"La ruta debe ser un Path absoluto: '{path}'")

    if str(path).startswith("\\\\"):
        # UNC path es UNSUPPORTED
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNSUPPORTED,
            evidence=GoldenProtectionEvidence(platform=sys.platform, drive_type=4),
            message="",
        )

    if not path.exists():
        raise GoldenProtectionInputError(f"La ruta no existe: '{path}'")

    if not path.is_dir():
        raise GoldenProtectionInputError(f"La ruta no es un directorio: '{path}'")

    # 2. Gate de plataforma no-Windows
    if sys.platform != "win32":
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNSUPPORTED,
            evidence=GoldenProtectionEvidence(platform=sys.platform),
            message="",
        )

    # 3. Localidad y volumen en Windows
    anchor = path.anchor
    drive_type = int(_kernel32.GetDriveTypeW(anchor))
    if drive_type != _DRIVE_FIXED:
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNSUPPORTED,
            evidence=GoldenProtectionEvidence(platform="windows", drive_type=drive_type),
            message="",
        )

    # Abrir handle del volumen/directorio con flags backup read-only
    h_file = _kernel32.CreateFileW(
        str(path),
        0,  # 0 desired access
        0x01 | 0x02 | 0x04,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    if h_file == wintypes.HANDLE(-1).value or h_file == 0:
        err = ctypes.get_last_error()
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNKNOWN,
            evidence=GoldenProtectionEvidence(
                platform="windows",
                drive_type=drive_type,
                observation_error=f"CreateFileW falló al abrir volumen '{path}' con error {err}",
            ),
            message=f"CreateFileW falló al abrir volumen '{path}' con error {err}",
        )

    fs_name: str | None = None
    persistent_acls: bool | None = None
    try:
        fs_buf = ctypes.create_unicode_buffer(260)
        flags_val = wintypes.DWORD()
        if not _kernel32.GetVolumeInformationByHandleW(
            h_file,
            None,
            0,
            None,
            None,
            ctypes.byref(flags_val),
            fs_buf,
            260,
        ):
            err = ctypes.get_last_error()
            return GoldenProtectionResult(
                state=GoldenProtectionState.UNKNOWN,
                evidence=GoldenProtectionEvidence(
                    platform="windows",
                    drive_type=drive_type,
                    observation_error=f"GetVolumeInformationByHandleW falló con error {err}",
                ),
                message=f"GetVolumeInformationByHandleW falló con error {err}",
            )
        fs_name = fs_buf.value
        persistent_acls = bool(flags_val.value & _FILE_PERSISTENT_ACLS)
    finally:
        _kernel32.CloseHandle(h_file)

    if fs_name != "NTFS" or not persistent_acls:
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNSUPPORTED,
            evidence=GoldenProtectionEvidence(
                platform="windows",
                drive_type=drive_type,
                filesystem=fs_name,
                filesystem_persistent_acls=persistent_acls,
            ),
            message="",
        )

    # 4. Token acquisition
    token_raw, token_imp = _obtener_token_efectivo()
    if not token_raw or not token_imp:
        return GoldenProtectionResult(
            state=GoldenProtectionState.UNKNOWN,
            evidence=GoldenProtectionEvidence(
                platform="windows",
                drive_type=drive_type,
                filesystem=fs_name,
                filesystem_persistent_acls=persistent_acls,
                observation_error="No se pudo obtener el token de seguridad efectivo",
            ),
            message="No se pudo obtener el token de seguridad efectivo",
        )

    try:
        token_inspection, token_err = _inspeccionar_token(token_raw)
        if token_err or not token_inspection:
            return GoldenProtectionResult(
                state=GoldenProtectionState.UNKNOWN,
                evidence=GoldenProtectionEvidence(
                    platform="windows",
                    drive_type=drive_type,
                    filesystem=fs_name,
                    filesystem_persistent_acls=persistent_acls,
                    observation_error=token_err or "Fallo de inspección de token",
                ),
                message=token_err or "Fallo de inspección de token",
            )

        user_sid = token_inspection.user_sid
        group_sids = token_inspection.group_sids
        is_elevated = token_inspection.elevated
        elev_type = token_inspection.elevation_type
        mutation_privs = token_inspection.mutation_privileges
        diag_privs = token_inspection.diagnostic_privileges

        # 5. PRE-Scan: recorrido estructural y detección de reparse points
        pre_entries: dict[str, tuple[pathlib.Path, str, os.stat_result]] = {}
        pending = [path]
        while pending:
            curr = pending.pop()
            try:
                link_k, st = link_kind_and_identity_or_raise(curr)
            except OSError as exc:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=f"Error de lstat en '{curr}': {exc}",
                    ),
                    message=f"Error de lstat en '{curr}': {exc}",
                )
            if link_k is not None:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=f"Enlace o reparse point '{link_k}' inesperado en '{curr}'",
                    ),
                    message=f"Enlace o reparse point '{link_k}' inesperado en '{curr}'",
                )
            if st is None:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=f"No se pudo obtener stat de '{curr}'",
                    ),
                    message=f"No se pudo obtener stat de '{curr}'",
                )

            tag = reparse_tag_or_zero(st)
            if tag != 0:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=f"Punto de reanálisis no link (tag 0x{tag:08X}) en '{curr}'",
                    ),
                    message=f"Punto de reanálisis no link (tag 0x{tag:08X}) en '{curr}'",
                )

            rel = curr.relative_to(path).as_posix()
            if curr == path or stat.S_ISDIR(st.st_mode):
                pre_entries[rel] = (curr, "dir", st)
                try:
                    with os.scandir(curr) as entries:
                        for entry in entries:
                            pending.append(pathlib.Path(entry.path))
                except OSError as exc:
                    return GoldenProtectionResult(
                        state=GoldenProtectionState.UNKNOWN,
                        evidence=GoldenProtectionEvidence(
                            platform="windows",
                            drive_type=drive_type,
                            filesystem=fs_name,
                            filesystem_persistent_acls=persistent_acls,
                            observation_error=f"Error al enumerar '{curr}': {exc}",
                        ),
                        message=f"Error al enumerar '{curr}': {exc}",
                    )
            elif stat.S_ISREG(st.st_mode):
                pre_entries[rel] = (curr, "file", st)
            else:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=f"Tipo de archivo no regular en '{curr}'",
                    ),
                    message=f"Tipo de archivo no regular en '{curr}'",
                )

        # 6. EVIDENCE collection: evaluar Security Descriptor de cada nodo
        node_observations: list[NodeProtectionObservation] = []
        for rel, (node_path, kind, _) in sorted(pre_entries.items()):
            obs, err_msg = _evaluar_acceso_nodo(node_path, kind, token_imp)
            if err_msg or obs is None:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=err_msg or "Error en evaluación de acceso",
                    ),
                    message=err_msg or "Error en evaluación de acceso",
                )
            node_observations.append(
                NodeProtectionObservation(
                    relative_path=rel,
                    node_kind=kind,
                    owner_sid=obs.owner_sid,
                    granted_rights=obs.granted_rights,
                    owner_rights_ace_present=obs.owner_rights_ace_present,
                    dacl_inheritance_protected=obs.dacl_inheritance_protected,
                )
            )

        # Evaluar Parent inmediato del root
        parent_obs: NodeProtectionObservation | None = None
        parent_path = path.parent
        if parent_path != path and parent_path.exists():
            try:
                p_link, p_st = link_kind_and_identity_or_raise(parent_path)
            except OSError as exc:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=f"Error de lstat en parent '{parent_path}': {exc}",
                    ),
                    message=f"Error de lstat en parent '{parent_path}': {exc}",
                )
            if p_link is not None:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=f"Parent '{parent_path}' es un enlace '{p_link}'",
                    ),
                    message=f"Parent '{parent_path}' es un enlace '{p_link}'",
                )
            p_tag = reparse_tag_or_zero(p_st)
            if p_tag != 0:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=f"Parent '{parent_path}' es un reparse point (tag 0x{p_tag:08X})",
                    ),
                    message=f"Parent '{parent_path}' es un reparse point (tag 0x{p_tag:08X})",
                )

            p_obs, p_err = _evaluar_acceso_nodo(parent_path, "dir", token_imp)
            if p_err or p_obs is None:
                return GoldenProtectionResult(
                    state=GoldenProtectionState.UNKNOWN,
                    evidence=GoldenProtectionEvidence(
                        platform="windows",
                        drive_type=drive_type,
                        filesystem=fs_name,
                        filesystem_persistent_acls=persistent_acls,
                        observation_error=p_err or "Error al evaluar parent",
                    ),
                    message=p_err or "Error al evaluar parent",
                )
            parent_obs = NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid=p_obs.owner_sid,
                granted_rights=p_obs.granted_rights,
                owner_rights_ace_present=p_obs.owner_rights_ace_present,
                dacl_inheritance_protected=p_obs.dacl_inheritance_protected,
            )

        # 7. POST-Scan: re-verificación estructural para sellar observación
        post_entries: dict[str, tuple[pathlib.Path, str, os.stat_result]] = {}
        pending = [path]
        while pending:
            curr = pending.pop()
            try:
                link_k, st = link_kind_and_identity_or_raise(curr)
            except OSError:
                drift_match = False
                break
            if link_k is not None or st is None or reparse_tag_or_zero(st) != 0:
                drift_match = False
                break

            rel = curr.relative_to(path).as_posix()
            if curr == path or stat.S_ISDIR(st.st_mode):
                post_entries[rel] = (curr, "dir", st)
                try:
                    with os.scandir(curr) as entries:
                        for entry in entries:
                            pending.append(pathlib.Path(entry.path))
                except OSError:
                    drift_match = False
                    break
            elif stat.S_ISREG(st.st_mode):
                post_entries[rel] = (curr, "file", st)
            else:
                drift_match = False
                break
        else:
            drift_match = set(pre_entries.keys()) == set(post_entries.keys())
            if drift_match:
                for k in pre_entries:
                    pre_st = pre_entries[k][2]
                    post_st = post_entries[k][2]
                    if not same_file_identity(pre_st, post_st):
                        drift_match = False
                        break

        # 8. Construir evidencia agregada y clasificar
        evidence = GoldenProtectionEvidence(
            platform="windows",
            drive_type=drive_type,
            filesystem=fs_name,
            filesystem_persistent_acls=persistent_acls,
            current_user_sid=user_sid,
            current_group_sids=group_sids,
            current_token_elevated=is_elevated,
            token_elevation_type=elev_type,
            mutation_privileges_present=mutation_privs,
            diagnostic_privileges_present=diag_privs,
            nodes=tuple(node_observations),
            parent_observation=parent_obs,
            pre_post_structural_match=drift_match,
        )

        return classify_protection(evidence)

    finally:
        _kernel32.CloseHandle(token_raw)
        _kernel32.CloseHandle(token_imp)


__all__ = [
    "GoldenProtectionEvidence",
    "GoldenProtectionInputError",
    "GoldenProtectionResult",
    "GoldenProtectionRight",
    "GoldenProtectionState",
    "NodeProtectionObservation",
    "classify_protection",
    "inspect_golden_protection",
]
