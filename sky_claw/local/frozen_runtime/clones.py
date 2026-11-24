"""Registro y autoridad durable de RuntimeCloneRecord (P5-B).

ADR 0012 §19c, §34.10, §36.7 son las fuentes normativas:
- Registro bajo ``state/clones/<clone-id>.json`` (schema v1).
- Linaje explícito con ``source_generation_id`` (verificado contra state/generations/).
- Máquina de estados: CREATED -> PROVISIONING -> PROVISIONED | INVALID.
- Exclusión física de la Managed Source y de versions/ (SFR-18, SFR-19).
- Exclusión interproceso e intraproceso mediante ``participant_lock`` del FrozenRuntimeRoot.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeError,
    InvalidCloneIdError,
    RuntimeCloneAdmissionError,
    RuntimeCloneCollisionError,
    RuntimeCloneCorruptError,
    RuntimeCloneLifecycleError,
    RuntimeCloneNotFoundError,
    RuntimeCloneSchemaError,
)
from sky_claw.local.frozen_runtime.generation_id import validar_generation_id
from sky_claw.local.frozen_runtime.independence import (
    descripcion_de_enlace,
    verify_clone_physical_integrity,
)
from sky_claw.local.frozen_runtime.state import write_json_atomic
from sky_claw.local.frozen_runtime.storage import (
    _RESERVADOS_WINDOWS,
    _contenida,
    _forma_reservada_windows,
    candidates_dir,
    clone_record_path,
    generation_dir,
    state_dir,
    versions_dir,
)
from sky_claw.local.frozen_runtime.storage_models import (
    GenerationVerificationResult,
    GenerationVerificationState,
    IndependenceState,
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
)
from sky_claw.local.runtime_vault.runtime_observation import (
    RuntimeObservationError,
    observe_runtime_identity_from_root,
)

CLONE_RECORD_SCHEMA_VERSION = 1
CANONICAL_ADMITTED_ROLE = "runtime_clone"

__all__ = [
    "CANONICAL_ADMITTED_ROLE",
    "CLONE_ID_PATTERN",
    "CLONE_RECORD_SCHEMA_VERSION",
    "MicEvaluationResult",
    "MicEvaluationStatus",
    "RuntimeCloneLifecycle",
    "RuntimeCloneRecord",
    "VALID_LIFECYCLE_TRANSITIONS",
    "actualizar_lifecycle_clone",
    "admitir_clone_root_path",
    "cargar_runtime_clone_record",
    "evaluar_etiqueta_mic_archivo",
    "evaluar_herencia_mic_clone_parent",
    "leer_runtime_clone_record",
    "registrar_runtime_clone_record",
    "validar_clone_id",
    "verificar_runtime_clone",
]

# ── Inspección MIC (Mandatory Integrity Control) Win32 de solo lectura ────

SECURITY_MANDATORY_UNTRUSTED_RID = 0x0000
SECURITY_MANDATORY_LOW_RID = 0x1000
SECURITY_MANDATORY_MEDIUM_RID = 0x2000
SECURITY_MANDATORY_HIGH_RID = 0x3000
SECURITY_MANDATORY_SYSTEM_RID = 0x4000

_SE_FILE_OBJECT = 1
_LABEL_SECURITY_INFORMATION = 0x00000010
_ACL_SIZE_INFORMATION = 2
_SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
_SYSTEM_MANDATORY_LABEL_NO_WRITE_UP = 0x00000001
_SYSTEM_MANDATORY_LABEL_NO_READ_UP = 0x00000002
_SYSTEM_MANDATORY_LABEL_NO_EXECUTE_UP = 0x00000004
_SYSTEM_MANDATORY_LABEL_VALID_MASK = (
    _SYSTEM_MANDATORY_LABEL_NO_WRITE_UP | _SYSTEM_MANDATORY_LABEL_NO_READ_UP | _SYSTEM_MANDATORY_LABEL_NO_EXECUTE_UP
)
_SYSTEM_MANDATORY_LABEL_RESTRICTIVE_MASK = _SYSTEM_MANDATORY_LABEL_NO_READ_UP | _SYSTEM_MANDATORY_LABEL_NO_EXECUTE_UP
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_NO_PROPAGATE_INHERIT_ACE = 0x04
_INHERIT_ONLY_ACE = 0x08
_INHERITED_ACE = 0x10
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_MANDATORY_LABEL_AUTHORITY_BYTES = (0, 0, 0, 0, 0, 16)  # S-1-16-*
_ACL_HEADER_SIZE = 8
_ACE_HEADER_SIZE = 4
_MANDATORY_LABEL_PREFIX_SIZE = 8  # sizeof(ACE_HEADER)=4 + sizeof(ACCESS_MASK)=4
_SID_HEADER_SIZE = 8  # Revision(1) + SubAuthorityCount(1) + IdentifierAuthority(6)
_SID_REVISION_1 = 1
_MIN_MANDATORY_LABEL_ACE_SIZE = 20  # sizeof(ACE_HEADER)=4 + Mask=4 + SID(1 subauth: 8 + 4)=12
_WIN32_LONG_PATH_THRESHOLD = 248
_WIN32_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class _SidIdentifierAuthority(ctypes.Structure):
    _fields_ = [("Value", ctypes.c_ubyte * 6)]


class _AclSizeInfo(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


class _AceHeader(ctypes.Structure):
    """Cabecera mínima de 4 bytes común a toda ACE Win32 (ACE_HEADER)."""

    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    ]


class _MandatoryLabelAceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
        ("Mask", ctypes.c_uint32),
    ]


class _EstadoLecturaMic(StrEnum):
    OK = "OK"
    MISSING = "MISSING"
    INDETERMINATE = "INDETERMINATE"
    NO_WINDOWS = "NO_WINDOWS"


@dataclass(frozen=True)
class _MicAceObservada:
    rid: int
    ace_flags: int
    mask: int


class MicEvaluationStatus(StrEnum):
    """Clasificación fail-closed de etiqueta MIC para ejecutables y raíces de Clone."""

    VERIFIED_COMPATIBLE = "VERIFIED_COMPATIBLE"
    UNLABELED_DEFAULT_MEDIUM = "UNLABELED_DEFAULT_MEDIUM"
    INCOMPATIBLE_LOW = "INCOMPATIBLE_LOW"
    INCOMPATIBLE_POLICY = "INCOMPATIBLE_POLICY"
    INDETERMINATE = "INDETERMINATE"
    MISSING = "MISSING"
    NOT_APPLICABLE_NON_WINDOWS = "NOT_APPLICABLE_NON_WINDOWS"


@dataclass(frozen=True)
class MicEvaluationResult:
    """Resultado tipado de inspección MIC sin mutar descriptores de seguridad."""

    status: MicEvaluationStatus
    effective_rid: int | None
    validated_on_windows: bool
    detail: str

    @property
    def is_compatible(self) -> bool:
        return self.status in (
            MicEvaluationStatus.VERIFIED_COMPATIBLE,
            MicEvaluationStatus.UNLABELED_DEFAULT_MEDIUM,
            MicEvaluationStatus.NOT_APPLICABLE_NON_WINDOWS,
        )


_advapi32_mic: Any | None = None
_kernel32_mic: Any | None = None


def _asegurar_bindings_mic_win32() -> None:
    """Configura una sola vez las firmas ctypes de lectura MIC en Windows."""
    global _advapi32_mic, _kernel32_mic
    if sys.platform != "win32" or (_advapi32_mic is not None and _kernel32_mic is not None):
        return

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    krn = ctypes.WinDLL("kernel32", use_last_error=True)

    adv.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    adv.GetNamedSecurityInfoW.restype = ctypes.c_uint32

    adv.GetSecurityDescriptorSacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int32),
    ]
    adv.GetSecurityDescriptorSacl.restype = ctypes.c_int32

    adv.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    adv.GetAclInformation.restype = ctypes.c_int32

    adv.GetAce.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    adv.GetAce.restype = ctypes.c_int32

    adv.IsValidSid.argtypes = [ctypes.c_void_p]
    adv.IsValidSid.restype = ctypes.c_int32

    adv.GetLengthSid.argtypes = [ctypes.c_void_p]
    adv.GetLengthSid.restype = ctypes.c_uint32

    adv.GetSidIdentifierAuthority.argtypes = [ctypes.c_void_p]
    adv.GetSidIdentifierAuthority.restype = ctypes.POINTER(_SidIdentifierAuthority)

    adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)

    adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_uint32)

    krn.LocalFree.argtypes = [ctypes.c_void_p]
    krn.LocalFree.restype = ctypes.c_void_p

    _advapi32_mic = adv
    _kernel32_mic = krn


def _longitud_utf16(texto: str) -> int:
    """Devuelve el número de unidades de código UTF-16 (límite real de MAX_PATH en Win32)."""
    return len(texto.encode("utf-16-le")) // 2


def _ruta_para_win32_security_info(ruta: pathlib.Path) -> str:
    """Normaliza rutas largas (>= 248 unidades UTF-16) con prefijo extendido ``\\\\?\\`` para Win32.

    En hosts Windows con ``LongPathsEnabled=0``, ``GetNamedSecurityInfoW`` sin
    prefijo extendido puede devolver ``ERROR_PATH_NOT_FOUND`` sobre rutas válidas
    que superan ``MAX_PATH`` (260). Las rutas cortas se conservan sin alterar.
    """
    cruda = os.fspath(ruta)
    if cruda.startswith("\\\\?\\"):
        return cruda
    absoluta = os.path.normpath(os.path.abspath(cruda))
    if absoluta.startswith("\\\\?\\") or _longitud_utf16(absoluta) < _WIN32_LONG_PATH_THRESHOLD:
        return cruda
    if absoluta.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absoluta[2:]
    return "\\\\?\\" + absoluta


def _formatear_mascara_mic(mask: int) -> str:
    bits: list[str] = []
    if mask & _SYSTEM_MANDATORY_LABEL_NO_WRITE_UP:
        bits.append("NO_WRITE_UP")
    if mask & _SYSTEM_MANDATORY_LABEL_NO_READ_UP:
        bits.append("NO_READ_UP")
    if mask & _SYSTEM_MANDATORY_LABEL_NO_EXECUTE_UP:
        bits.append("NO_EXECUTE_UP")
    desconocidos = mask & ~_SYSTEM_MANDATORY_LABEL_VALID_MASK
    if desconocidos:
        bits.append(f"UNKNOWN(0x{desconocidos:x})")
    return "|".join(bits) if bits else "NONE"


def _nombre_nivel_mic(rid: int) -> str:
    nombres = {
        0x0000: "Untrusted Mandatory Level (S-1-16-0)",
        0x1000: "Low Mandatory Level (S-1-16-4096)",
        0x2000: "Medium Mandatory Level (S-1-16-8192)",
        0x2100: "Medium Plus Mandatory Level (S-1-16-8448)",
        0x3000: "High Mandatory Level (S-1-16-12288)",
        0x4000: "System Mandatory Level (S-1-16-16384)",
        0x5000: "Protected Process Mandatory Level (S-1-16-20480)",
    }
    return nombres.get(rid, f"Mandatory Level S-1-16-{rid}")


def _formatear_flags_ace(flags: int) -> str:
    etiquetas: list[str] = []
    if flags & _OBJECT_INHERIT_ACE:
        etiquetas.append("OI")
    if flags & _CONTAINER_INHERIT_ACE:
        etiquetas.append("CI")
    if flags & _NO_PROPAGATE_INHERIT_ACE:
        etiquetas.append("NP")
    if flags & _INHERIT_ONLY_ACE:
        etiquetas.append("IO")
    if flags & _INHERITED_ACE:
        etiquetas.append("ID")
    return "|".join(etiquetas) if etiquetas else "NONE"


def _leer_aces_etiqueta_mic(
    ruta: pathlib.Path,
) -> tuple[_EstadoLecturaMic, tuple[_MicAceObservada, ...], str]:
    """Lee de forma estrictamente read-only las ACEs SYSTEM_MANDATORY_LABEL_ACE en Windows."""
    if sys.platform != "win32":
        return (
            _EstadoLecturaMic.NO_WINDOWS,
            (),
            "inspección MIC Win32 no aplicable fuera de Windows",
        )

    _asegurar_bindings_mic_win32()
    adv = _advapi32_mic
    krn = _kernel32_mic
    if adv is None or krn is None:
        return (
            _EstadoLecturaMic.INDETERMINATE,
            (),
            "no se pudieron inicializar los enlaces Win32 de lectura MIC",
        )

    ruta_win32 = _ruta_para_win32_security_info(ruta)
    sacl_ptr = ctypes.c_void_p()
    sd_ptr = ctypes.c_void_p()
    rc = int(
        adv.GetNamedSecurityInfoW(
            ruta_win32,
            _SE_FILE_OBJECT,
            _LABEL_SECURITY_INFORMATION,
            None,
            None,
            None,
            ctypes.byref(sacl_ptr),
            ctypes.byref(sd_ptr),
        )
    )
    if rc in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        if sd_ptr.value:
            krn.LocalFree(sd_ptr)
        # Contradicción fail-closed: si el objeto sí existe en NTFS (p. ej. límite MAX_PATH
        # con LongPathsEnabled=0 o error transitorio de resolución), jamás reportar MISSING.
        try:
            os.lstat(ruta_win32)
        except FileNotFoundError:
            return (_EstadoLecturaMic.MISSING, (), f"ruta inexistente durante lectura MIC: '{ruta}'")
        except OSError as exc:
            return (
                _EstadoLecturaMic.INDETERMINATE,
                (),
                f"os.lstat falló durante comprobación de existencia MIC en '{ruta}': {exc}",
            )
        return (
            _EstadoLecturaMic.INDETERMINATE,
            (),
            f"GetNamedSecurityInfoW devolvió código Win32 {rc} sobre una ruta existente: '{ruta}'",
        )
    if rc != 0:
        if sd_ptr.value:
            krn.LocalFree(sd_ptr)
        return (
            _EstadoLecturaMic.INDETERMINATE,
            (),
            f"GetNamedSecurityInfoW(LABEL_SECURITY_INFORMATION) falló con código Win32 {rc} en '{ruta}'",
        )
    if not sd_ptr.value:
        return (
            _EstadoLecturaMic.INDETERMINATE,
            (),
            f"GetNamedSecurityInfoW devolvió éxito con SecurityDescriptor nulo en '{ruta}'",
        )

    try:
        sacl_present = ctypes.c_int32(0)
        sacl_defaulted = ctypes.c_int32(0)
        sacl_out = ctypes.c_void_p()
        if not adv.GetSecurityDescriptorSacl(
            sd_ptr,
            ctypes.byref(sacl_present),
            ctypes.byref(sacl_out),
            ctypes.byref(sacl_defaulted),
        ):
            err = ctypes.get_last_error()
            return (
                _EstadoLecturaMic.INDETERMINATE,
                (),
                f"GetSecurityDescriptorSacl falló con error Win32 {err} en '{ruta}'",
            )

        if not sacl_present.value or not sacl_out.value:
            return (_EstadoLecturaMic.OK, (), "")

        acl_info = _AclSizeInfo()
        if not adv.GetAclInformation(
            sacl_out,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            _ACL_SIZE_INFORMATION,
        ):
            err = ctypes.get_last_error()
            return (
                _EstadoLecturaMic.INDETERMINATE,
                (),
                f"GetAclInformation falló con error Win32 {err} en '{ruta}'",
            )

        acl_bytes_in_use = int(acl_info.AclBytesInUse)
        if acl_bytes_in_use < _ACL_HEADER_SIZE:
            return (
                _EstadoLecturaMic.INDETERMINATE,
                (),
                f"ACL SACL truncada (AclBytesInUse={acl_bytes_in_use} < {_ACL_HEADER_SIZE}) en '{ruta}'",
            )
        acl_base = int(sacl_out.value)
        acl_end = acl_base + acl_bytes_in_use

        encontradas: list[_MicAceObservada] = []
        for idx in range(int(acl_info.AceCount)):
            ace_ptr = ctypes.c_void_p()
            if not adv.GetAce(sacl_out, idx, ctypes.byref(ace_ptr)) or not ace_ptr.value:
                err = ctypes.get_last_error()
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"GetAce({idx}) falló con error Win32 {err} en '{ruta}'",
                )
            ace_addr = int(ace_ptr.value)
            # 1) Validar límites del buffer ACL antes de leer la cabecera mínima de 4 bytes (ACE_HEADER).
            if ace_addr < acl_base + _ACL_HEADER_SIZE or ace_addr + _ACE_HEADER_SIZE > acl_end:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"puntero ACE({idx}) fuera de los límites de la ACL en '{ruta}'",
                )
            base_hdr = ctypes.cast(ace_ptr, ctypes.POINTER(_AceHeader)).contents
            ace_type = int(base_hdr.AceType)
            ace_flags = int(base_hdr.AceFlags)
            ace_size = int(base_hdr.AceSize)
            if ace_size < _ACE_HEADER_SIZE or ace_addr + ace_size > acl_end:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"ACE({idx}) con AceSize={ace_size} fuera de los límites de la ACL en '{ruta}'",
                )
            if ace_type != _SYSTEM_MANDATORY_LABEL_ACE_TYPE:
                continue

            # 2) Una SYSTEM_MANDATORY_LABEL_ACE con SID S-1-16-<RID> (1 subautoridad) exige >= 20 bytes.
            if ace_size < _MIN_MANDATORY_LABEL_ACE_SIZE:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"ACE MIC truncada (AceSize={ace_size} < {_MIN_MANDATORY_LABEL_ACE_SIZE}) en '{ruta}'",
                )

            # 3) Ya acotados >= 20 bytes dentro de la ACL, leer Mask (offset 4..7) y cabecera SID (offset 8..15).
            ml_hdr = ctypes.cast(ace_ptr, ctypes.POINTER(_MandatoryLabelAceHeader)).contents
            sid_addr = ace_addr + _MANDATORY_LABEL_PREFIX_SIZE
            available_sid_bytes = ace_size - _MANDATORY_LABEL_PREFIX_SIZE
            raw_sid_hdr = (ctypes.c_ubyte * _SID_HEADER_SIZE).from_address(sid_addr)
            raw_sid_revision = int(raw_sid_hdr[0])
            raw_subauth_count = int(raw_sid_hdr[1])
            expected_sid_len = _SID_HEADER_SIZE + 4 * raw_subauth_count
            if raw_sid_revision != _SID_REVISION_1:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"revisión SID inválida ({raw_sid_revision}) en ACE MIC de '{ruta}'",
                )
            if expected_sid_len > available_sid_bytes:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"SID de ACE MIC excede AceSize (requiere {expected_sid_len} B, disponible {available_sid_bytes} B) "
                    f"en '{ruta}'",
                )
            if raw_subauth_count != 1:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"SubAuthorityCount inválido ({raw_subauth_count}) en SID de integridad de '{ruta}'",
                )

            # 4) Validación Win32 completa sobre el puntero SID ya acotado en [sid_addr, ace_addr + ace_size).
            sid_ptr = ctypes.c_void_p(sid_addr)
            if not adv.IsValidSid(sid_ptr):
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"SID inválido en ACE MIC de '{ruta}'",
                )
            sid_len = int(adv.GetLengthSid(sid_ptr))
            if sid_len != expected_sid_len or _MANDATORY_LABEL_PREFIX_SIZE + sid_len > ace_size:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"longitud de SID inconsistente (GetLengthSid={sid_len}, AceSize={ace_size}) en '{ruta}'",
                )
            auth_ptr = adv.GetSidIdentifierAuthority(sid_ptr)
            if not auth_ptr or tuple(auth_ptr.contents.Value) != _MANDATORY_LABEL_AUTHORITY_BYTES:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"autoridad SID inesperada en ACE MIC de '{ruta}' (se esperaba S-1-16)",
                )
            sub_cnt_ptr = adv.GetSidSubAuthorityCount(sid_ptr)
            if not sub_cnt_ptr or int(sub_cnt_ptr.contents.value) != 1:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"SubAuthorityCount inválido en SID de integridad de '{ruta}'",
                )
            rid_ptr = adv.GetSidSubAuthority(sid_ptr, 0)
            if not rid_ptr:
                return (
                    _EstadoLecturaMic.INDETERMINATE,
                    (),
                    f"no se pudo leer el RID de integridad en '{ruta}'",
                )
            encontradas.append(
                _MicAceObservada(
                    rid=int(rid_ptr.contents.value),
                    ace_flags=ace_flags,
                    mask=int(ml_hdr.Mask),
                )
            )
        return (_EstadoLecturaMic.OK, tuple(encontradas), "")
    finally:
        if sd_ptr.value:
            krn.LocalFree(sd_ptr)


def _formatear_ace_o_vacio(flags: int) -> str:
    return f" [flags={_formatear_flags_ace(flags)}]" if flags else ""


def _evaluar_conjunto_aces_mic(
    aces: Sequence[_MicAceObservada],
    *,
    objetivo: pathlib.Path,
    es_directorio_padre: bool,
) -> MicEvaluationResult:
    """Evalúa de forma fail-closed un conjunto no vacío de ACEs MIC efectivas o heredables.

    Prioridad:
    1. Bits de máscara desconocidos (fuera de NW|NR|NX) -> INDETERMINATE.
    2. Múltiples ACEs con (rid, mask) divergentes (p. ej. Medium + Low o High+NX + Medium+NW)
       -> INDETERMINATE: la evidencia es ambigua (Windows solo aplica la primera ACE efectiva)
       y nunca debe provocar PASS ni una degradación terminal prematura de PROVISIONED a INVALID.
    3. Política única con RID < Medium (Low/Untrusted) -> INCOMPATIBLE_LOW.
    4. Política única con RID > Medium y NO_READ_UP o NO_EXECUTE_UP -> INCOMPATIBLE_POLICY
       (impide a un proceso MO2 Medium leer o ejecutar el binario).
    5. Política única compatible (Medium con cualquier NW/NR/NX o >Medium con solo NW/0)
       -> VERIFIED_COMPATIBLE.
    """
    for ace in aces:
        if ace.mask & ~_SYSTEM_MANDATORY_LABEL_VALID_MASK:
            return MicEvaluationResult(
                status=MicEvaluationStatus.INDETERMINATE,
                effective_rid=ace.rid,
                validated_on_windows=True,
                detail=(
                    f"máscara MIC desconocida ({_formatear_mascara_mic(ace.mask)}) en '{objetivo}': "
                    "evidencia no determinista (fail-closed)"
                ),
            )

    politicas_distintas = {(a.rid, a.mask) for a in aces}
    if len(politicas_distintas) > 1:
        resumen = ", ".join(
            f"{_nombre_nivel_mic(rid)}[{_formatear_mascara_mic(mask)}]" for rid, mask in sorted(politicas_distintas)
        )
        return MicEvaluationResult(
            status=MicEvaluationStatus.INDETERMINATE,
            effective_rid=None,
            validated_on_windows=True,
            detail=(
                f"SACL ambigua en '{objetivo}': coexisten múltiples ACEs MIC divergentes ({resumen}); "
                "se clasifica INDETERMINATE (fail-closed sin degradación terminal)"
            ),
        )

    primera = aces[0]
    etiqueta_txt = _nombre_nivel_mic(primera.rid)
    if primera.rid < SECURITY_MANDATORY_MEDIUM_RID:
        if es_directorio_padre:
            flags_txt = _formatear_flags_ace(primera.ace_flags)
            return MicEvaluationResult(
                status=MicEvaluationStatus.INCOMPATIBLE_LOW,
                effective_rid=primera.rid,
                validated_on_windows=True,
                detail=(
                    f"aprovisionamiento rechazado antes de copiar: el directorio destino '{objetivo}' posee "
                    f"etiqueta MIC heredable {etiqueta_txt} (flags={flags_txt}); los ejecutables del futuro "
                    "Clone heredarían integridad Low/inferior a Medium e impedirían inicializar USVFS y SKSE "
                    "al ejecutar MO2 sin privilegios de administrador. Elija otro destino seguro para "
                    "clone_parent fuera de un árbol con etiqueta Low (no se modifica el destino ni sus ACLs)"
                ),
            )
        flags_txt = _formatear_ace_o_vacio(primera.ace_flags)
        return MicEvaluationResult(
            status=MicEvaluationStatus.INCOMPATIBLE_LOW,
            effective_rid=primera.rid,
            validated_on_windows=True,
            detail=(
                f"ejecutable '{objetivo}' posee etiqueta MIC incompatible {etiqueta_txt}{flags_txt}: "
                "bajo ejecución normal sin elevar desde MO2 el proceso se degradaría por debajo de Medium "
                "y USVFS/SKSE fallarían al abrir memoria compartida"
            ),
        )

    if primera.rid > SECURITY_MANDATORY_MEDIUM_RID and (primera.mask & _SYSTEM_MANDATORY_LABEL_RESTRICTIVE_MASK):
        mascara_txt = _formatear_mascara_mic(primera.mask)
        if es_directorio_padre:
            flags_txt = _formatear_flags_ace(primera.ace_flags)
            return MicEvaluationResult(
                status=MicEvaluationStatus.INCOMPATIBLE_POLICY,
                effective_rid=primera.rid,
                validated_on_windows=True,
                detail=(
                    f"aprovisionamiento rechazado antes de copiar: el directorio destino '{objetivo}' posee "
                    f"etiqueta MIC heredable restrictiva {etiqueta_txt} (mask={mascara_txt}, flags={flags_txt}); "
                    "los ejecutables del futuro Clone heredarían NO_READ_UP/NO_EXECUTE_UP por encima de Medium "
                    "e impedirían abrir o ejecutar el juego desde MO2 sin elevar. Elija otro destino seguro "
                    "para clone_parent (no se modifica el destino ni sus ACLs)"
                ),
            )
        flags_txt = _formatear_ace_o_vacio(primera.ace_flags)
        return MicEvaluationResult(
            status=MicEvaluationStatus.INCOMPATIBLE_POLICY,
            effective_rid=primera.rid,
            validated_on_windows=True,
            detail=(
                f"ejecutable '{objetivo}' posee política MIC restrictiva incompatible {etiqueta_txt} "
                f"(mask={mascara_txt}){flags_txt}: NO_READ_UP/NO_EXECUTE_UP sobre un nivel superior a Medium "
                "impide a MO2 no elevado leer o ejecutar el binario"
            ),
        )

    if es_directorio_padre:
        flags_txt = _formatear_flags_ace(primera.ace_flags)
        return MicEvaluationResult(
            status=MicEvaluationStatus.VERIFIED_COMPATIBLE,
            effective_rid=primera.rid,
            validated_on_windows=True,
            detail=f"herencia MIC compatible en '{objetivo}': {etiqueta_txt} (flags={flags_txt})",
        )
    flags_txt = _formatear_ace_o_vacio(primera.ace_flags)
    return MicEvaluationResult(
        status=MicEvaluationStatus.VERIFIED_COMPATIBLE,
        effective_rid=primera.rid,
        validated_on_windows=True,
        detail=f"etiqueta MIC compatible en '{objetivo}': {etiqueta_txt}{flags_txt}",
    )


def evaluar_etiqueta_mic_archivo(ruta: pathlib.Path) -> MicEvaluationResult:
    """Evalúa de solo lectura si un ejecutable tiene nivel y política MIC compatibles con MO2 Medium."""
    objetivo = pathlib.Path(ruta)
    estado, aces, detalle = _leer_aces_etiqueta_mic(objetivo)
    if estado is _EstadoLecturaMic.NO_WINDOWS:
        return MicEvaluationResult(
            status=MicEvaluationStatus.NOT_APPLICABLE_NON_WINDOWS,
            effective_rid=None,
            validated_on_windows=False,
            detail=detalle,
        )
    if estado is _EstadoLecturaMic.MISSING:
        return MicEvaluationResult(
            status=MicEvaluationStatus.MISSING,
            effective_rid=None,
            validated_on_windows=True,
            detail=detalle,
        )
    if estado is _EstadoLecturaMic.INDETERMINATE:
        return MicEvaluationResult(
            status=MicEvaluationStatus.INDETERMINATE,
            effective_rid=None,
            validated_on_windows=True,
            detail=detalle,
        )

    efectivas = [a for a in aces if not (a.ace_flags & _INHERIT_ONLY_ACE)]
    if not efectivas:
        return MicEvaluationResult(
            status=MicEvaluationStatus.UNLABELED_DEFAULT_MEDIUM,
            effective_rid=SECURITY_MANDATORY_MEDIUM_RID,
            validated_on_windows=True,
            detail=f"sin etiqueta MIC explícita en '{objetivo}'; Windows aplica Medium por defecto",
        )

    return _evaluar_conjunto_aces_mic(efectivas, objetivo=objetivo, es_directorio_padre=False)


def evaluar_herencia_mic_clone_parent(clone_parent: pathlib.Path) -> MicEvaluationResult:
    """Evalúa antes de copiar si ``clone_parent`` propagaría etiquetas MIC incompatibles al nuevo Clone."""
    padre = pathlib.Path(clone_parent)
    estado, aces, detalle = _leer_aces_etiqueta_mic(padre)
    if estado is _EstadoLecturaMic.NO_WINDOWS:
        return MicEvaluationResult(
            status=MicEvaluationStatus.NOT_APPLICABLE_NON_WINDOWS,
            effective_rid=None,
            validated_on_windows=False,
            detail=detalle,
        )
    if estado is _EstadoLecturaMic.MISSING:
        return MicEvaluationResult(
            status=MicEvaluationStatus.MISSING,
            effective_rid=None,
            validated_on_windows=True,
            detail=detalle,
        )
    if estado is _EstadoLecturaMic.INDETERMINATE:
        return MicEvaluationResult(
            status=MicEvaluationStatus.INDETERMINATE,
            effective_rid=None,
            validated_on_windows=True,
            detail=detalle,
        )

    # En NTFS (MS-DTYP §2.5.3.4), una ACE en clone_parent alcanza archivos dentro de
    # <clone_parent>/<clone_id>/<exe> si y solo si posee OBJECT_INHERIT_ACE sin NO_PROPAGATE_INHERIT_ACE.
    heredables_a_archivos_nietos = [
        a for a in aces if (a.ace_flags & _OBJECT_INHERIT_ACE) and not (a.ace_flags & _NO_PROPAGATE_INHERIT_ACE)
    ]
    if not heredables_a_archivos_nietos:
        return MicEvaluationResult(
            status=MicEvaluationStatus.UNLABELED_DEFAULT_MEDIUM,
            effective_rid=SECURITY_MANDATORY_MEDIUM_RID,
            validated_on_windows=True,
            detail=f"'{padre}' no propaga etiquetas MIC incompatibles hacia ejecutables hijos",
        )

    return _evaluar_conjunto_aces_mic(heredables_a_archivos_nietos, objetivo=padre, es_directorio_padre=True)


#: Charset seguro para clone_id: alfanuméricos, guiones, puntos y guiones bajos simples.
CLONE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[a-z0-9._-]*[a-z0-9]+)?$")

#: Transiciones válidas en la máquina de estados de RuntimeCloneLifecycle (§36.7).
VALID_LIFECYCLE_TRANSITIONS: dict[RuntimeCloneLifecycle, frozenset[RuntimeCloneLifecycle]] = {
    RuntimeCloneLifecycle.CREATED: frozenset(
        {
            RuntimeCloneLifecycle.PROVISIONING,
            RuntimeCloneLifecycle.INVALID,
        }
    ),
    RuntimeCloneLifecycle.PROVISIONING: frozenset(
        {
            RuntimeCloneLifecycle.PROVISIONED,
            RuntimeCloneLifecycle.INVALID,
        }
    ),
    RuntimeCloneLifecycle.PROVISIONED: frozenset(
        {
            RuntimeCloneLifecycle.INVALID,
        }
    ),
    RuntimeCloneLifecycle.INVALID: frozenset(),  # terminal
}


def _token_reservado(clone_id: str) -> str | None:
    token = re.split(r"[._-]", clone_id, maxsplit=1)[0]
    return token if token in _RESERVADOS_WINDOWS else None


def validar_clone_id(clone_id: str) -> str:
    """Valida y normaliza un clone_id (casefold); fail-closed ante cualquier ambigüedad."""
    if not isinstance(clone_id, str) or not clone_id:
        raise InvalidCloneIdError("clone_id no puede ser vacío")
    if clone_id != clone_id.strip():
        raise InvalidCloneIdError(f"clone_id con espacios en los bordes: '{clone_id}'")
    if len(clone_id) > 128:
        raise InvalidCloneIdError(f"clone_id excede longitud máxima (128): '{clone_id}'")
    if ".." in clone_id or "/" in clone_id or "\\" in clone_id or ":" in clone_id:
        raise InvalidCloneIdError(f"clone_id contiene caracteres de ruta o traversal: '{clone_id}'")

    normalized = clone_id.casefold()
    if not CLONE_ID_PATTERN.fullmatch(normalized):
        raise InvalidCloneIdError(
            f"clone_id con formato inválido: '{clone_id}' (esperado charset [a-z0-9._-], sin traversal ni separadores)"
        )
    reservado = _token_reservado(normalized)
    if reservado is not None:
        raise InvalidCloneIdError(f"clone_id con nombre reservado de Windows: '{reservado}'")
    return normalized


def validar_runtime_setup_id(setup_id: str) -> str:
    """Valida un identificador de RuntimeSetup."""
    if not isinstance(setup_id, str) or not setup_id.strip():
        raise RuntimeCloneSchemaError("runtime_setup_id no puede ser vacío")
    if ".." in setup_id or "/" in setup_id or "\\" in setup_id:
        raise RuntimeCloneSchemaError(f"runtime_setup_id con traversal o separadores: '{setup_id}'")
    return setup_id.strip()


def _canonical_long_path_windows(p: pathlib.Path) -> pathlib.Path:
    """Obtiene el nombre largo canónico en Windows expandiendo alias 8.3."""
    if sys.platform != "win32":
        return p
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        chars = ctypes.windll.kernel32.GetLongPathNameW(str(p), buf, 32768)
        if 0 < chars < 32768:
            return pathlib.Path(buf.value)
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    try:
        return pathlib.Path(os.path.realpath(p))
    except (OSError, RuntimeError):
        return p


def admitir_clone_root_path(
    root_path: pathlib.Path | str,
    *,
    storage_root: pathlib.Path | None = None,
    managed_source_root: pathlib.Path | None = None,
) -> pathlib.Path:
    """Admisión fail-closed de la ruta operativa de un Runtime Clone.

    Reglas:
    - Ruta absoluta y normalizada, sin componentes relativos ('..' o '.').
    - Sin alias de nombre corto 8.3 de Windows ni discrepancias con el nombre largo canónico.
    - Sin symlinks, junctions ni reparse points en la ruta ni sus ancestros.
    - Fuera de steamapps/common (Managed Source) y de cualquier directorio steamapps.
    - Fuera de versions/ del storage (SFR-19: una Generation nunca es runtime ejecutado).
    - Fuera de candidates/ y state/ del storage.
    """
    raw_path_str = os.fspath(root_path)
    partes_crudo = [p for p in re.split(r"[\\/]", raw_path_str) if p]
    if ".." in partes_crudo or "." in partes_crudo:
        raise RuntimeCloneAdmissionError(
            f"la ruta del clone contiene componentes relativos ('..' o '.'): '{root_path}'"
        )
    for parte in partes_crudo:
        if re.search(r"~\d", parte):
            raise RuntimeCloneAdmissionError(
                f"la ruta del clone contiene un alias de nombre corto 8.3 de Windows no permitido: '{parte}'"
            )

    ruta = pathlib.Path(root_path)
    if not ruta.is_absolute():
        raise RuntimeCloneAdmissionError(f"la ruta del clone debe ser absoluta: '{root_path}'")

    norm_str = os.path.normpath(os.path.abspath(raw_path_str))
    norm_ruta = pathlib.Path(norm_str)

    # Inspección de enlaces / reparse points sobre la ruta declarada, la canónica y todos sus ancestros
    motivo = descripcion_de_enlace(ruta)
    if motivo is not None:
        raise RuntimeCloneAdmissionError(f"root de clone redirigido por reparse point/enlace: {motivo}")
    for ancestro in (norm_ruta, *norm_ruta.parents):
        if not ancestro.exists():
            continue
        motivo_ancestro = descripcion_de_enlace(ancestro)
        if motivo_ancestro is not None:
            raise RuntimeCloneAdmissionError(
                f"root de clone redirigido por reparse point/enlace en '{ancestro}': {motivo_ancestro}"
            )

    forma = _forma_reservada_windows(norm_ruta.name)
    if forma is not None:
        raise RuntimeCloneAdmissionError(f"nombre de ruta de clone reservado en Windows: {forma}")

    # Validación y expansión de nombres largos canónicos en Windows
    try:
        resolved_ruta = norm_ruta.resolve()
    except (OSError, RuntimeError):
        resolved_ruta = norm_ruta

    long_ruta = _canonical_long_path_windows(norm_ruta)
    if norm_ruta.exists() and sys.platform == "win32":
        if os.path.normcase(str(norm_ruta)).rstrip("\\/") != os.path.normcase(str(long_ruta)).rstrip("\\/"):
            raise RuntimeCloneAdmissionError(
                f"la ruta del clone contiene un alias 8.3 de Windows no canónico: '{norm_ruta}' != '{long_ruta}'"
            )

    rutas_a_evaluar = {norm_ruta, long_ruta, resolved_ruta}

    # Exclusión de steamapps y steamapps/common sobre la ruta canónica y todos sus ancestros
    for r in rutas_a_evaluar:
        for componente in (r, *r.parents):
            if componente.name.casefold() == "common" and componente.parent.name.casefold() == "steamapps":
                raise RuntimeCloneAdmissionError(
                    f"el root del clone está dentro de un área administrada por Steam: '{componente}'"
                )
            if componente.name.casefold() == "steamapps":
                raise RuntimeCloneAdmissionError(
                    f"el root del clone está dentro de un área administrada por Steam: '{componente}'"
                )

    # Exclusión respecto del storage root
    if storage_root is not None:
        s_root = pathlib.Path(os.path.normpath(os.path.abspath(os.fspath(storage_root))))
        for target in rutas_a_evaluar:
            if _contenida(target, versions_dir(s_root)):
                raise RuntimeCloneAdmissionError(
                    "el root del clone está dentro de versions/ (violación SFR-19: Generation nunca es runtime ejecutado)"
                )
            if _contenida(target, candidates_dir(s_root)):
                raise RuntimeCloneAdmissionError("el root del clone está dentro de candidates/")
            if _contenida(target, state_dir(s_root)):
                raise RuntimeCloneAdmissionError("el root del clone está dentro de state/")
            if _contenida(target, s_root):
                raise RuntimeCloneAdmissionError(f"el root del clone está dentro de (o es) el storage root: '{target}'")
        if _contenida(s_root, norm_ruta):
            raise RuntimeCloneAdmissionError(
                f"el storage root está dentro del root del clone (ancestro no permitido): '{norm_ruta}'"
            )

    # Exclusión respecto de la Managed Source
    if managed_source_root is not None:
        ms_root = pathlib.Path(os.path.normpath(os.path.abspath(os.fspath(managed_source_root))))
        for target in rutas_a_evaluar:
            if _contenida(target, ms_root):
                raise RuntimeCloneAdmissionError(
                    f"el root del clone está dentro de (o es) la Managed Source: '{target}'"
                )
        if _contenida(ms_root, norm_ruta):
            raise RuntimeCloneAdmissionError(f"la Managed Source está dentro del root del clone: '{ms_root}'")

    return norm_ruta


def _payload_de_clone_record(record: RuntimeCloneRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "clone_id": record.clone_id,
        "source_generation_id": record.source_generation_id,
        "lifecycle": record.lifecycle.value,
        "intended_runtime_setup_id": record.intended_runtime_setup_id,
        "verified_runtime_setup_id": record.verified_runtime_setup_id,
        "root_path": record.root_path,
        "admitted_role": record.admitted_role,
        "created_at_ns": record.created_at_ns,
    }


def _clone_record_de_payload(data: dict[str, Any], *, source_label: str) -> RuntimeCloneRecord:
    if not isinstance(data, dict):
        raise RuntimeCloneCorruptError(f"{source_label}: el registro debe ser un objeto JSON")

    # Validación de schema_version
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise RuntimeCloneSchemaError(f"{source_label}: schema_version debe ser int")
    if schema != CLONE_RECORD_SCHEMA_VERSION:
        raise RuntimeCloneSchemaError(
            f"{source_label}: schema desconocido {schema}; soportado: {CLONE_RECORD_SCHEMA_VERSION} (fail-closed)"
        )

    # Campos requeridos
    requeridos = (
        "schema_version",
        "clone_id",
        "source_generation_id",
        "lifecycle",
        "intended_runtime_setup_id",
        "verified_runtime_setup_id",
        "root_path",
        "admitted_role",
        "created_at_ns",
    )
    for campo in requeridos:
        if campo not in data:
            raise RuntimeCloneSchemaError(f"{source_label}: falta el campo obligatorio '{campo}'")

    raw_clone_id = data["clone_id"]
    if not isinstance(raw_clone_id, str):
        raise RuntimeCloneSchemaError(f"{source_label}: clone_id debe ser string")
    clone_id = validar_clone_id(raw_clone_id)

    raw_gen_id = data["source_generation_id"]
    if not isinstance(raw_gen_id, str):
        raise RuntimeCloneSchemaError(f"{source_label}: source_generation_id debe ser string")
    source_gen_id = validar_generation_id(raw_gen_id)

    raw_lifecycle = data["lifecycle"]
    if not isinstance(raw_lifecycle, str) or raw_lifecycle not in RuntimeCloneLifecycle.__members__:
        raise RuntimeCloneSchemaError(
            f"{source_label}: lifecycle desconocido '{raw_lifecycle}'; "
            f"soportados: {list(RuntimeCloneLifecycle.__members__.keys())}"
        )
    lifecycle = RuntimeCloneLifecycle(raw_lifecycle)

    raw_intended = data["intended_runtime_setup_id"]
    intended_setup = validar_runtime_setup_id(raw_intended)

    raw_verified = data["verified_runtime_setup_id"]
    if raw_verified is not None:
        if not isinstance(raw_verified, str):
            raise RuntimeCloneSchemaError(f"{source_label}: verified_runtime_setup_id debe ser string o null")
        verified_setup = validar_runtime_setup_id(raw_verified)
    else:
        verified_setup = None

    # Invariante de setup vs lifecycle (§19c):
    # PROVISIONED exige verified_runtime_setup_id no null y coincidente con intended
    if lifecycle is RuntimeCloneLifecycle.PROVISIONED:
        if verified_setup is None:
            raise RuntimeCloneSchemaError(
                f"{source_label}: lifecycle PROVISIONED exige verified_runtime_setup_id no null"
            )
        if verified_setup != intended_setup:
            raise RuntimeCloneSchemaError(
                f"{source_label}: setup mismatch (intended={intended_setup!r} != verified={verified_setup!r})"
            )
    elif lifecycle in (RuntimeCloneLifecycle.CREATED, RuntimeCloneLifecycle.PROVISIONING):
        if verified_setup is not None:
            raise RuntimeCloneSchemaError(
                f"{source_label}: lifecycle {lifecycle.value} no admite verified_runtime_setup_id"
            )

    root_path_raw = data["root_path"]
    if not isinstance(root_path_raw, str) or not root_path_raw.strip():
        raise RuntimeCloneSchemaError(f"{source_label}: root_path debe ser string no vacío")

    admitted_role = data["admitted_role"]
    if admitted_role != CANONICAL_ADMITTED_ROLE:
        raise RuntimeCloneAdmissionError(
            f"{source_label}: admitted_role inválido '{admitted_role}' "
            f"(exigido: '{CANONICAL_ADMITTED_ROLE}'; violación SFR-19)"
        )

    created_at_ns = data["created_at_ns"]
    if not isinstance(created_at_ns, int) or isinstance(created_at_ns, bool) or created_at_ns < 0:
        raise RuntimeCloneSchemaError(f"{source_label}: created_at_ns debe ser int >= 0")

    return RuntimeCloneRecord(
        schema_version=schema,
        clone_id=clone_id,
        source_generation_id=source_gen_id,
        lifecycle=lifecycle,
        intended_runtime_setup_id=intended_setup,
        verified_runtime_setup_id=verified_setup,
        root_path=root_path_raw,
        admitted_role=admitted_role,
        created_at_ns=created_at_ns,
    )


def registrar_runtime_clone_record(
    root: pathlib.Path,
    record: RuntimeCloneRecord,
    *,
    timeout: float = 0.0,
    managed_source_root: pathlib.Path | None = None,
) -> RuntimeCloneRecord:
    """Frontera pública: adquiere el root lock y delega en ``*_bajo_lock``."""
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    with participant_lock(root, timeout=timeout) as handle:
        handle.assert_owned()
        return _registrar_runtime_clone_record_bajo_lock(
            root,
            record,
            managed_source_root=managed_source_root,
        )


def _registrar_runtime_clone_record_bajo_lock(
    root: pathlib.Path,
    record: RuntimeCloneRecord,
    *,
    managed_source_root: pathlib.Path | None = None,
) -> RuntimeCloneRecord:
    """Registra un RuntimeCloneRecord; el caller DEBE poseer el root lock.

    Idempotente si el registro ya existe con identidad idéntica.
    Falla con RuntimeCloneCollisionError si ya existe con datos distintos (no-clobber).
    No re-adquiere ``participant_lock`` (no reentrante por diseño); lo usa el
    provisionamiento P7-A1 dentro de su sección crítica.
    """
    if (
        not isinstance(record.schema_version, int)
        or isinstance(record.schema_version, bool)
        or record.schema_version != CLONE_RECORD_SCHEMA_VERSION
    ):
        raise RuntimeCloneSchemaError(
            f"schema_version inválido {record.schema_version}; soportado: {CLONE_RECORD_SCHEMA_VERSION}"
        )

    cid = validar_clone_id(record.clone_id)
    gid = validar_generation_id(record.source_generation_id)

    if not isinstance(record.lifecycle, RuntimeCloneLifecycle):
        raise RuntimeCloneSchemaError(f"lifecycle inválido: {record.lifecycle!r}")

    intended = validar_runtime_setup_id(record.intended_runtime_setup_id)
    if record.verified_runtime_setup_id is not None:
        if not isinstance(record.verified_runtime_setup_id, str):
            raise RuntimeCloneSchemaError("verified_runtime_setup_id debe ser string o None")
        verified = validar_runtime_setup_id(record.verified_runtime_setup_id)
    else:
        verified = None

    if not isinstance(record.created_at_ns, int) or isinstance(record.created_at_ns, bool) or record.created_at_ns < 0:
        raise RuntimeCloneSchemaError("created_at_ns debe ser int >= 0")

    if record.admitted_role != CANONICAL_ADMITTED_ROLE:
        raise RuntimeCloneAdmissionError(f"admitted_role inválido: '{record.admitted_role}'")

    # Invariante de setup vs lifecycle
    if record.lifecycle is RuntimeCloneLifecycle.PROVISIONED:
        if verified is None:
            raise RuntimeCloneSchemaError("PROVISIONED exige verified_runtime_setup_id no null")
        if verified != intended:
            raise RuntimeCloneSchemaError(f"setup mismatch: intended '{intended}' != verified '{verified}'")
    elif record.lifecycle in (RuntimeCloneLifecycle.CREATED, RuntimeCloneLifecycle.PROVISIONING):
        if verified is not None:
            raise RuntimeCloneSchemaError(f"lifecycle {record.lifecycle.value} no admite verified_runtime_setup_id")

    # Validación de admisión de la ruta del clone
    if not isinstance(record.root_path, str) or not record.root_path.strip():
        raise RuntimeCloneSchemaError("root_path debe ser string no vacío")
    admitted_root = admitir_clone_root_path(
        record.root_path, storage_root=root, managed_source_root=managed_source_root
    )

    # Validar existencia de la Generation de origen en el storage
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeStorageError
    from sky_claw.local.frozen_runtime.generations import leer_generation_metadata

    try:
        gen_meta = leer_generation_metadata(root, gid)
    except (FrozenRuntimeError, OSError) as exc:
        raise FrozenRuntimeStorageError(
            f"metadata ausente para la generation '{gid}' declarada en clone '{cid}': {exc}"
        ) from exc
    if gen_meta.generation_id != gid:
        raise FrozenRuntimeStorageError(f"generation_id en metadata '{gen_meta.generation_id}' no coincide con '{gid}'")

    canonical_record = RuntimeCloneRecord(
        schema_version=record.schema_version,
        clone_id=cid,
        source_generation_id=gid,
        lifecycle=record.lifecycle,
        intended_runtime_setup_id=intended,
        verified_runtime_setup_id=verified,
        root_path=os.fspath(admitted_root),
        admitted_role=record.admitted_role,
        created_at_ns=record.created_at_ns,
    )

    # Validar que la serialización round-trip sea consistente
    payload = _payload_de_clone_record(canonical_record)
    _clone_record_de_payload(payload, source_label=f"registro de clone '{cid}'")

    destino = clone_record_path(root, cid)

    from sky_claw.local.frozen_runtime.state import reservar_ruta_json_exclusiva

    try:
        existente = _leer_record_de_ruta(destino)
    except RuntimeCloneNotFoundError:
        try:
            reservar_ruta_json_exclusiva(destino)
        except FileExistsError as exc:
            raise RuntimeCloneCollisionError(
                f"clone_id '{cid}' ya existe (colisión en reserva exclusiva): {exc}"
            ) from exc
        write_json_atomic(destino, payload)
        return canonical_record

    if existente == canonical_record:
        return existente
    raise RuntimeCloneCollisionError(f"clone_id '{cid}' ya existe con identidad distinta (no-clobber fail-closed)")


def actualizar_lifecycle_clone(
    root: pathlib.Path,
    clone_id: str,
    nuevo_lifecycle: RuntimeCloneLifecycle,
    *,
    verified_runtime_setup_id: str | None = None,
    timeout: float = 0.0,
) -> RuntimeCloneRecord:
    """Frontera pública: adquiere el root lock y delega en ``*_bajo_lock``."""
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    with participant_lock(root, timeout=timeout) as handle:
        handle.assert_owned()
        return _actualizar_lifecycle_clone_bajo_lock(
            root,
            clone_id,
            nuevo_lifecycle,
            verified_runtime_setup_id=verified_runtime_setup_id,
        )


def _actualizar_lifecycle_clone_bajo_lock(
    root: pathlib.Path,
    clone_id: str,
    nuevo_lifecycle: RuntimeCloneLifecycle,
    *,
    verified_runtime_setup_id: str | None = None,
) -> RuntimeCloneRecord:
    """Actualiza la máquina de estados; el caller DEBE poseer el root lock.

    No re-adquiere ``participant_lock`` (no reentrante por diseño); lo usa el
    provisionamiento P7-A1 dentro de su sección crítica.
    """
    if not isinstance(nuevo_lifecycle, RuntimeCloneLifecycle):
        raise RuntimeCloneSchemaError(f"lifecycle inválido: {nuevo_lifecycle!r}")

    cid = validar_clone_id(clone_id)
    destino = clone_record_path(root, cid)

    if verified_runtime_setup_id is not None:
        if not isinstance(verified_runtime_setup_id, str):
            raise RuntimeCloneSchemaError("verified_runtime_setup_id debe ser string o None")
        v_setup_cand = validar_runtime_setup_id(verified_runtime_setup_id)
    else:
        v_setup_cand = None

    existente = leer_runtime_clone_record(root, cid)
    if existente.lifecycle == nuevo_lifecycle:
        return existente

    permitidas = VALID_LIFECYCLE_TRANSITIONS.get(existente.lifecycle, frozenset())
    if nuevo_lifecycle not in permitidas:
        raise RuntimeCloneLifecycleError(
            f"transición inválida de {existente.lifecycle.value} a {nuevo_lifecycle.value} "
            f"(permitidas: {[p.value for p in permitidas]})" + (" (estado terminal)" if not permitidas else "")
        )

    if nuevo_lifecycle in (RuntimeCloneLifecycle.CREATED, RuntimeCloneLifecycle.PROVISIONING):
        if v_setup_cand is not None:
            raise RuntimeCloneSchemaError(f"lifecycle {nuevo_lifecycle.value} no admite verified_runtime_setup_id")
        v_setup = None
    elif nuevo_lifecycle is RuntimeCloneLifecycle.PROVISIONED:
        v_setup = v_setup_cand or existente.verified_runtime_setup_id
        if not v_setup:
            raise RuntimeCloneLifecycleError("PROVISIONED exige verified_runtime_setup_id no vacío")
        if v_setup != existente.intended_runtime_setup_id:
            raise RuntimeCloneLifecycleError(
                f"setup mismatch: intended '{existente.intended_runtime_setup_id}' != verified '{v_setup}'"
            )
    else:
        # nuevo_lifecycle is RuntimeCloneLifecycle.INVALID
        # Preservación forense (§19c): si el clone ya estaba PROVISIONED con un setup verificado,
        # no se admite sustituirlo ni alterarlo; se preserva de forma inmutable.
        if existente.verified_runtime_setup_id is not None:
            if v_setup_cand is not None and v_setup_cand != existente.verified_runtime_setup_id:
                raise RuntimeCloneLifecycleError(
                    f"transición a INVALID no admite sustituir verified_runtime_setup_id previo "
                    f"'{existente.verified_runtime_setup_id}' por '{v_setup_cand}' (preservación forense fail-closed)"
                )
            v_setup = existente.verified_runtime_setup_id
        else:
            if v_setup_cand is not None:
                raise RuntimeCloneLifecycleError(
                    f"transición a INVALID de clone no PROVISIONED ('{existente.lifecycle.value}') "
                    f"no admite verified_runtime_setup_id (no existe verificación previa; fail-closed)"
                )
            v_setup = None

    actualizado = RuntimeCloneRecord(
        schema_version=existente.schema_version,
        clone_id=existente.clone_id,
        source_generation_id=existente.source_generation_id,
        lifecycle=nuevo_lifecycle,
        intended_runtime_setup_id=existente.intended_runtime_setup_id,
        verified_runtime_setup_id=v_setup,
        root_path=existente.root_path,
        admitted_role=existente.admitted_role,
        created_at_ns=existente.created_at_ns,
    )

    payload = _payload_de_clone_record(actualizado)
    _clone_record_de_payload(payload, source_label=f"actualización lifecycle clone '{cid}'")
    write_json_atomic(destino, payload)
    return actualizado


def _leer_record_de_ruta(ruta: pathlib.Path) -> RuntimeCloneRecord:
    """Lee y parsea un registro desde su ruta, validando redirección fail-closed."""
    try:
        kind = link_kind_or_raise(ruta)
    except OSError as exc:
        raise RuntimeCloneCorruptError(f"{ruta}: no se pudo inspeccionar el archivo de registro: {exc}") from exc
    if kind is not None:
        raise RuntimeCloneCorruptError(f"{ruta}: el archivo de registro es un enlace/reparse ({kind}): fail-closed")

    for ancestro in (ruta.parent, *ruta.parent.parents):
        if not ancestro.exists():
            continue
        motivo_ancestro = descripcion_de_enlace(ancestro)
        if motivo_ancestro is not None:
            raise RuntimeCloneCorruptError(
                f"{ruta}: namespace de clones redirigido por reparse point/enlace en ancestro '{ancestro}': {motivo_ancestro}"
            )

    if not ruta.exists():
        raise RuntimeCloneNotFoundError(f"registro de clone no encontrado: '{ruta}'")

    try:
        raw = ruta.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeCloneCorruptError(f"{ruta}: contenido no UTF-8: {exc}") from exc
    except OSError as exc:
        raise RuntimeCloneCorruptError(f"{ruta}: no se pudo leer el archivo: {exc}") from exc

    if not raw.strip():
        raise RuntimeCloneCorruptError(f"{ruta}: archivo de registro vacío o corrupto (0 bytes)")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeCloneCorruptError(f"{ruta}: JSON malformado o truncado: {exc}") from exc

    record = _clone_record_de_payload(data, source_label=str(ruta))
    canonical_id = ruta.stem
    if record.clone_id != canonical_id:
        raise RuntimeCloneCorruptError(
            f"{ruta}: clone_id en payload '{record.clone_id}' no coincide con el namespace canónico '{canonical_id}': fail-closed"
        )
    return record


def leer_runtime_clone_record(root: pathlib.Path, clone_id: str) -> RuntimeCloneRecord:
    """Lectura estricta y tipada de un RuntimeCloneRecord (fail-closed ante ausencia o corrupción)."""
    cid = validar_clone_id(clone_id)
    destino = clone_record_path(root, cid)
    return _leer_record_de_ruta(destino)


def cargar_runtime_clone_record(root: pathlib.Path, clone_id: str) -> RuntimeCloneRecord | None:
    """Carga de un RuntimeCloneRecord; devuelve None si no existe, lanza ante corrupción."""
    cid = validar_clone_id(clone_id)
    destino = clone_record_path(root, cid)
    try:
        return _leer_record_de_ruta(destino)
    except RuntimeCloneNotFoundError:
        return None


def verificar_runtime_clone(
    root: pathlib.Path,
    clone_id: str,
    *,
    managed_source_root: pathlib.Path | None = None,
) -> GenerationVerificationResult:
    """Verifica físicamente el Runtime Clone contra su Generation registrada."""
    try:
        record = leer_runtime_clone_record(root, clone_id)
    except OSError as exc:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"no se pudo leer el registro del clone '{clone_id}' (transitorio): {exc}",
        )
    except FrozenRuntimeError as exc:
        # _leer_record_de_ruta envuelve OSError de lectura como corrupto; el
        # cause distingue un fallo transitorio de un registro realmente inválido.
        if isinstance(exc.__cause__, OSError):
            return GenerationVerificationResult(
                state=GenerationVerificationState.INDETERMINATE,
                message=f"no se pudo leer el registro del clone '{clone_id}' (transitorio): {exc}",
            )
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"no se pudo cargar el registro del clone '{clone_id}': {exc}",
        )

    # 0. El clone debe estar en estado PROVISIONED (ADR 0012 §29.3, SFR-17)
    if record.lifecycle is not RuntimeCloneLifecycle.PROVISIONED:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"el lifecycle del clone '{clone_id}' no es PROVISIONED ({record.lifecycle.value}): fail-closed",
        )

    # 1. Verificar existencia, vinculación e integridad de la Generation de origen (ADR 0012 §29.3, SFR-17)
    from sky_claw.local.frozen_runtime.generations import verificar_generation

    gen_verif = verificar_generation(root, record.source_generation_id)
    if gen_verif.state is GenerationVerificationState.INDETERMINATE:
        if gen_verif.recorded is None:
            from sky_claw.local.frozen_runtime.storage import generations_state_dir

            metadata_path = generations_state_dir(root) / f"{record.source_generation_id}.json"
            if not metadata_path.exists():
                # Metadata AUSENTE: linaje huérfano confirmado => INVALID.
                return GenerationVerificationResult(
                    state=GenerationVerificationState.INVALID,
                    message=(
                        f"Generation de origen no existe o no es VALID "
                        f"({record.source_generation_id}, {gen_verif.state.value}): {gen_verif.message}"
                    ),
                )
            # Metadata PRESENTE pero ilegible/corrupta: fallo transitorio/indeterminado,
            # no se degrada el Clone a INVALID (terminal).
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=(
                f"Generation de origen no verificable de forma concluyente "
                f"({record.source_generation_id}, {gen_verif.state.value}): {gen_verif.message}"
            ),
            recorded=gen_verif.recorded,
        )
    if gen_verif.state is not GenerationVerificationState.VALID:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=(
                f"Generation de origen no existe o no es VALID "
                f"({record.source_generation_id}, {gen_verif.state.value}): {gen_verif.message}"
            ),
            recorded=gen_verif.recorded,
        )

    gen_meta = gen_verif.recorded
    if gen_meta is None or gen_meta.generation_id != record.source_generation_id:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=(
                f"Generation de origen con generation_id discordante "
                f"('{getattr(gen_meta, 'generation_id', None)}' != '{record.source_generation_id}')"
            ),
            recorded=gen_meta,
        )

    # 2. Admisión de la ruta física
    try:
        clone_root = admitir_clone_root_path(
            record.root_path, storage_root=root, managed_source_root=managed_source_root
        )
    except RuntimeCloneAdmissionError as exc:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"admisión física del clone rechazada: {exc}",
            recorded=gen_meta,
        )

    if not clone_root.is_dir():
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"la ruta del clone no existe o no es un directorio: '{clone_root}'",
            recorded=gen_meta,
        )

    # 3. Integridad física y aislamiento del Clone (SFR-18, ADR 0012 §35.4/§35.11)
    gen_dir = generation_dir(root, record.source_generation_id)
    integridad = verify_clone_physical_integrity(
        clone_root,
        generation_root=gen_dir if gen_dir.is_dir() else None,
        managed_source_root=managed_source_root,
    )
    if integridad.state is IndependenceState.VIOLATED:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"integridad física del clone violada: {integridad.message}",
            recorded=gen_meta,
        )
    if integridad.state is IndependenceState.INDETERMINATE:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"integridad física del clone no demostrable: {integridad.message}",
            recorded=gen_meta,
        )

    # 4. Observación de identidad fresca de runtime
    try:
        obs = observe_runtime_identity_from_root(clone_root, expected_game_key="skyrimse")
    except RuntimeObservationError as exc:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"falló la observación de identidad de runtime en el clone: {exc}",
            recorded=gen_meta,
        )

    if obs.runtime_identity != gen_meta.runtime_identity:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=(
                f"identidad de runtime observada '{obs.runtime_identity.game_version}' "
                f"difiere de la registrada en Generation '{gen_meta.runtime_identity.game_version}'"
            ),
            recorded=gen_meta,
            observed_identity=obs.runtime_identity,
        )

    # 4. Verificación de archivos críticos
    for crit in gen_meta.critical_files:
        c_path = clone_root / crit.rel_path
        if not c_path.exists():
            return GenerationVerificationResult(
                state=GenerationVerificationState.INVALID,
                message=f"archivo crítico ausente en clone: '{crit.rel_path}'",
                recorded=gen_meta,
                observed_identity=obs.runtime_identity,
            )
        try:
            st = c_path.stat()
            if st.st_size != crit.size:
                return GenerationVerificationResult(
                    state=GenerationVerificationState.INVALID,
                    message=f"tamaño de archivo crítico '{crit.rel_path}' difiere de la Generation",
                    recorded=gen_meta,
                    observed_identity=obs.runtime_identity,
                )
            hasher = hashlib.sha256()
            with c_path.open("rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
            digest_calculado = hasher.hexdigest().lower()
            if digest_calculado != crit.digest.lower():
                return GenerationVerificationResult(
                    state=GenerationVerificationState.INVALID,
                    message=(
                        f"digest de archivo crítico '{crit.rel_path}' difiere de la Generation: "
                        f"{digest_calculado} != {crit.digest}"
                    ),
                    recorded=gen_meta,
                    observed_identity=obs.runtime_identity,
                )
        except OSError as exc:
            return GenerationVerificationResult(
                state=GenerationVerificationState.INDETERMINATE,
                message=f"no se pudo inspeccionar archivo crítico '{crit.rel_path}': {exc}",
                recorded=gen_meta,
                observed_identity=obs.runtime_identity,
            )

    # 5. Verificación MIC de solo lectura sobre los ejecutables de arranque del Clone
    ejecutables_rel: list[tuple[str, bool]] = []
    vistos_exe: set[str] = set()
    for crit in gen_meta.critical_files:
        if crit.rel_path.casefold().endswith(".exe"):
            clave_exe = crit.rel_path.casefold()
            if clave_exe not in vistos_exe:
                vistos_exe.add(clave_exe)
                ejecutables_rel.append((crit.rel_path, True))
    if "skyrimse.exe" not in vistos_exe:
        vistos_exe.add("skyrimse.exe")
        ejecutables_rel.append(("SkyrimSE.exe", True))
    if "skse64_loader.exe" not in vistos_exe:
        ruta_loader = clone_root / "skse64_loader.exe"
        try:
            st_loader = os.lstat(ruta_loader)
        except FileNotFoundError:
            st_loader = None
        except OSError as exc:
            return GenerationVerificationResult(
                state=GenerationVerificationState.INDETERMINATE,
                message=f"no se pudo comprobar presencia de 'skse64_loader.exe' para inspección MIC: {exc}",
                recorded=gen_meta,
                observed_identity=obs.runtime_identity,
            )
        if st_loader is not None:
            attrs_loader = getattr(st_loader, "st_file_attributes", 0)
            if (attrs_loader & _WIN32_FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(st_loader.st_mode):
                return GenerationVerificationResult(
                    state=GenerationVerificationState.INVALID,
                    message="la entrada 'skse64_loader.exe' es un enlace/reparse point (fail-closed)",
                    recorded=gen_meta,
                    observed_identity=obs.runtime_identity,
                )
            if not stat.S_ISREG(st_loader.st_mode):
                return GenerationVerificationResult(
                    state=GenerationVerificationState.INVALID,
                    message="la entrada 'skse64_loader.exe' en el Clone no es un archivo regular (fail-closed)",
                    recorded=gen_meta,
                    observed_identity=obs.runtime_identity,
                )
            ejecutables_rel.append(("skse64_loader.exe", False))

    mic_verificado_en_windows = bool(ejecutables_rel)
    for rel_exe, es_critico in ejecutables_rel:
        res_mic = evaluar_etiqueta_mic_archivo(clone_root / rel_exe)
        if not res_mic.validated_on_windows:
            mic_verificado_en_windows = False
        if res_mic.status in (MicEvaluationStatus.INCOMPATIBLE_LOW, MicEvaluationStatus.INCOMPATIBLE_POLICY):
            return GenerationVerificationResult(
                state=GenerationVerificationState.INVALID,
                message=f"etiqueta MIC incompatible en Clone: {res_mic.detail}",
                recorded=gen_meta,
                observed_identity=obs.runtime_identity,
            )
        if res_mic.status is MicEvaluationStatus.INDETERMINATE:
            return GenerationVerificationResult(
                state=GenerationVerificationState.INDETERMINATE,
                message=f"inspección MIC indeterminada en Clone: {res_mic.detail}",
                recorded=gen_meta,
                observed_identity=obs.runtime_identity,
            )
        if res_mic.status is MicEvaluationStatus.MISSING:
            estado_missing = (
                GenerationVerificationState.INVALID if es_critico else GenerationVerificationState.INDETERMINATE
            )
            return GenerationVerificationResult(
                state=estado_missing,
                message=f"ejecutable '{rel_exe}' ausente durante inspección MIC del Clone",
                recorded=gen_meta,
                observed_identity=obs.runtime_identity,
            )

    mensaje_ok = (
        "Runtime Clone verificado exitosamente (MIC Win32 verificado)"
        if mic_verificado_en_windows
        else "Runtime Clone verificado sin comprobación MIC Win32 (entorno no Windows)"
    )
    return GenerationVerificationResult(
        state=GenerationVerificationState.VALID,
        message=mensaje_ok,
        recorded=gen_meta,
        observed_identity=obs.runtime_identity,
        mic_verified_on_windows=mic_verificado_en_windows,
    )
