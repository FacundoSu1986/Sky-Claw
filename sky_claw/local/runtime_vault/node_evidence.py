"""Evidencia nativa de nodos para Runtime Vault (GP2-S1).

SECURITY RULE:
No filesystem path may be resolved/followed before its own
reparse-point status has been inspected fail-closed.

MEMORY OWNERSHIP RULE:
Only allocations owned by the caller according to the Win32 API
contract are LocalFree'd. OWNER/GROUP/DACL pointers returned inside
the GetSecurityInfo Security Descriptor are borrowed pointers and
must never be freed independently.

Este módulo implementa la primitive de solo lectura de evidencia nativa Win32
para candidate planning en el proceso no elevado:
- Apertura atada a handle con FILE_FLAG_BACKUP_SEMANTICS y FILE_FLAG_OPEN_REPARSE_POINT.
- Identidad física real por handle (VolumeSerialNumber, FileId128).
- Inspección de NumberOfLinks, DeletePending, FileAttributes, ReparseTag.
- Captura de Security Descriptors binarios self-relative mediante GetSecurityInfo.
- Detección de hardlinks en fases: Fase 2 (interno -> DuplicateFileIdError) y
  Fase 3 (externo -> NativeHardlinkError).
- Detección estricta de reparse points / junctions / symlinks fail-closed.
- Ordenamiento determinista bottom-up con '.' garantizado como último elemento.
- Prohibición absoluta de primitivas mutadoras (garantizada por AST guard).
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import pathlib
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from sky_claw.app.security.links import link_kind_and_identity_or_raise
from sky_claw.local.runtime_vault.golden_protection_plan import (
    DuplicateFileIdError,
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class NativeEvidenceError(RuntimeVaultError):
    """Base de las excepciones de dominio para la evidencia nativa de nodos."""


class NativeEvidenceUnsupportedError(NativeEvidenceError):
    """Plataforma no soportada o volumen/filesystem incompatible (no NTFS / no persistent ACLs)."""


class NativeHardlinkError(NativeEvidenceError):
    """Violación de la política de hardlinks (p. ej. archivos regulares con NumberOfLinks != 1)."""


class NativeReparsePointError(NativeEvidenceError):
    """Detección de enlaces o reparse points dentro del alcance evaluado (fail-closed)."""


# ============================================================================
# DTO de Evidencia Nativa
# ============================================================================


@dataclass(frozen=True, slots=True)
class NativeNodeEvidence:
    """Evidencia candidate no autoritativa recolectada por nodo en el proceso no elevado."""

    backup: NodeSecurityBackup
    number_of_links: int
    reparse_tag: int
    file_attributes: int
    delete_pending: bool


# ============================================================================
# Transformación de Identidad de Archivo (ABI Sky-Claw)
# ============================================================================


def file_id_128_to_int(identifier: bytes | Sequence[int]) -> int:
    """Convierte el arreglo Identifier[16] de FILE_ID_128 a un entero uint128 determinista.

    Usa orden little-endian explícito para garantizar reproducibilidad sin depender
    del endianness nativo de la CPU ni de casts implícitos.
    """
    raw_bytes = bytes(identifier)
    if len(raw_bytes) != 16:
        raise ValueError(f"FILE_ID_128 exige exactamente 16 bytes, se recibieron {len(raw_bytes)}")
    return int.from_bytes(raw_bytes, byteorder="little", signed=False)


def _bottom_up_evidence_sort_key(evidence: NativeNodeEvidence) -> tuple[int, str, str]:
    """Cálculo determinista de clave para ordenamiento bottom-up.

    '.' posee profundidad 0 y por tanto -profundidad es 0, garantizando que quede último
    frente a cualquier descendiente (-profundidad negativa).
    """
    rel = evidence.backup.relative_path
    depth = 0 if rel == "." else rel.count("/") + 1
    return (-depth, rel, evidence.backup.node_kind.value)


# ============================================================================
# Win32 Native Definitions (Solo Windows)
# ============================================================================

_DRIVE_FIXED = 3
_FILE_PERSISTENT_ACLS = 0x00000008
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_SE_DACL_PROTECTED = 0x1000
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_GROUP_SECURITY_INFORMATION = 0x00000002
_DACL_SECURITY_INFORMATION = 0x00000004

_FILE_READ_ATTRIBUTES = 0x0080
_READ_CONTROL = 0x00020000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

# Clases para GetFileInformationByHandleEx
_FILE_INFO_BY_HANDLE_CLASS_STANDARD = 1
_FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG = 9
_FILE_INFO_BY_HANDLE_CLASS_ID = 18

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

    _kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetDriveTypeW.restype = wintypes.UINT

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

    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL

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

    _advapi32.GetSecurityDescriptorLength.argtypes = [wintypes.LPVOID]
    _advapi32.GetSecurityDescriptorLength.restype = wintypes.DWORD

    _advapi32.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL

    _advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL


# ============================================================================
# Helpers Internos Win32
# ============================================================================


def _open_node_handle(path: pathlib.Path) -> int:
    """Abre un handle Win32 de solo lectura para observar el nodo sin seguir reparse points."""
    h = _kernel32.CreateFileW(
        str(path),
        _FILE_READ_ATTRIBUTES | _READ_CONTROL,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if h == wintypes.HANDLE(-1).value or h == 0:
        err = ctypes.get_last_error()
        raise NativeEvidenceError(f"CreateFileW falló al abrir '{path}': código {err}")
    return int(h)


def _probe_open_handle(
    handle: int,
    relative_path: str,
    expected_kind: GoldenProtectionNodeKind | None = None,
) -> NativeNodeEvidence:
    """Extrae evidencia y Security Descriptor autoritativo directamente desde un HANDLE abierto.

    MEMORY OWNERSHIP RULE:
    - Se libera pSecurityDescriptor devuelto por GetSecurityInfo con LocalFree.
    - Se liberan las cadenas devueltas por ConvertSidToStringSidW con LocalFree.
    - NUNCA se invoca LocalFree sobre ppsidOwner, ppsidGroup ni pDacl (son punteros
      prestados contenidos dentro de pSecurityDescriptor).
    """
    # 1. FileIdInfo: VolumeSerialNumber + FileId128
    info_id = _FileIdInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_INFO_BY_HANDLE_CLASS_ID,
        ctypes.byref(info_id),
        ctypes.sizeof(info_id),
    ):
        err = ctypes.get_last_error()
        raise NativeEvidenceError(f"GetFileInformationByHandleEx(FileIdInfo) falló en '{relative_path}': código {err}")

    volume_serial = int(info_id.VolumeSerialNumber)
    file_id = file_id_128_to_int(info_id.FileId.Identifier)
    if file_id == 0:
        raise NativeEvidenceError(f"Identidad inválida en '{relative_path}': FileId=0")

    # 2. FileStandardInfo: NumberOfLinks, DeletePending, Directory
    info_std = _FileStandardInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_INFO_BY_HANDLE_CLASS_STANDARD,
        ctypes.byref(info_std),
        ctypes.sizeof(info_std),
    ):
        err = ctypes.get_last_error()
        raise NativeEvidenceError(
            f"GetFileInformationByHandleEx(FileStandardInfo) falló en '{relative_path}': código {err}"
        )

    number_of_links = int(info_std.NumberOfLinks)
    delete_pending = bool(info_std.DeletePending)
    is_directory = bool(info_std.Directory)

    if delete_pending:
        raise NativeEvidenceError(f"El nodo '{relative_path}' tiene delete_pending=True (marcado para borrado)")

    # Gate real de expected_kind contra drift de tipo entre enumeración y apertura
    if expected_kind is not None:
        expected_is_dir = expected_kind is GoldenProtectionNodeKind.DIR
        if is_directory != expected_is_dir:
            obs = "dir" if is_directory else "file"
            raise NativeEvidenceError(
                f"Drift de tipo detectado en '{relative_path}': esperado={expected_kind.value}, observado={obs}"
            )

    node_kind = GoldenProtectionNodeKind.DIR if is_directory else GoldenProtectionNodeKind.FILE

    # 3. FileAttributeTagInfo: FileAttributes, ReparseTag
    info_tag = _FileAttributeTagInfo()
    if not _kernel32.GetFileInformationByHandleEx(
        handle,
        _FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG,
        ctypes.byref(info_tag),
        ctypes.sizeof(info_tag),
    ):
        err = ctypes.get_last_error()
        raise NativeEvidenceError(
            f"GetFileInformationByHandleEx(FileAttributeTagInfo) falló en '{relative_path}': código {err}"
        )

    file_attributes = int(info_tag.FileAttributes)
    reparse_tag = int(info_tag.ReparseTag)

    # Detección estricta de reparse points por handle (fail-closed)
    if bool(file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT) or reparse_tag != 0:
        raise NativeReparsePointError(
            f"Reparse point detectado por handle en '{relative_path}': "
            f"attributes=0x{file_attributes:08X}, tag=0x{reparse_tag:08X}"
        )

    # 4. Security Descriptor: GetSecurityInfo por HANDLE
    owner_p = wintypes.LPVOID()
    group_p = wintypes.LPVOID()
    dacl_p = wintypes.LPVOID()
    sd_p = wintypes.LPVOID()
    owner_str_p = wintypes.LPWSTR()
    group_str_p = wintypes.LPWSTR()

    try:
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
        if ret != 0:
            raise NativeEvidenceError(f"GetSecurityInfo falló en '{relative_path}': código {ret}")

        sd_len = _advapi32.GetSecurityDescriptorLength(sd_p)
        if sd_len <= 0:
            raise NativeEvidenceError(f"GetSecurityDescriptorLength devolvió {sd_len} en '{relative_path}'")

        sd_bytes = ctypes.string_at(sd_p, sd_len)

        control = wintypes.WORD()
        rev = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(sd_p, ctypes.byref(control), ctypes.byref(rev)):
            err = ctypes.get_last_error()
            raise NativeEvidenceError(f"GetSecurityDescriptorControl falló en '{relative_path}': código {err}")

        dacl_control_flags = int(control.value)
        pre_dacl_protected_flag = bool(dacl_control_flags & _SE_DACL_PROTECTED)

        if not _advapi32.ConvertSidToStringSidW(owner_p, ctypes.byref(owner_str_p)):
            err = ctypes.get_last_error()
            raise NativeEvidenceError(f"ConvertSidToStringSidW(owner) falló en '{relative_path}': código {err}")
        owner_sid = owner_str_p.value
        if not owner_sid:
            raise NativeEvidenceError(f"owner_sid vacío en '{relative_path}'")

        if not _advapi32.ConvertSidToStringSidW(group_p, ctypes.byref(group_str_p)):
            err = ctypes.get_last_error()
            raise NativeEvidenceError(f"ConvertSidToStringSidW(group) falló en '{relative_path}': código {err}")
        group_sid = group_str_p.value
        if not group_sid:
            raise NativeEvidenceError(f"group_sid vacío en '{relative_path}'")

        backup = NodeSecurityBackup(
            relative_path=relative_path,
            node_kind=node_kind,
            volume_serial_number=volume_serial,
            file_id=file_id,
            pre_sd_bytes_b64=base64.b64encode(sd_bytes).decode("ascii"),
            pre_sd_length=sd_len,
            pre_sd_sha256=hashlib.sha256(sd_bytes).hexdigest(),
            owner_sid=owner_sid,
            group_sid=group_sid,
            dacl_control_flags=dacl_control_flags,
            pre_dacl_protected_flag=pre_dacl_protected_flag,
            sddl_diagnostic="",
        )

        return NativeNodeEvidence(
            backup=backup,
            number_of_links=number_of_links,
            reparse_tag=reparse_tag,
            file_attributes=file_attributes,
            delete_pending=delete_pending,
        )
    finally:
        # Liberación estricta de asignaciones propias según contrato Win32
        if owner_str_p:
            _kernel32.LocalFree(owner_str_p)
        if group_str_p:
            _kernel32.LocalFree(group_str_p)
        if sd_p:
            _kernel32.LocalFree(sd_p)


# ============================================================================
# API Pública de Sonda de Evidencia
# ============================================================================


def probe_node_evidence(
    root: pathlib.Path | str | os.PathLike[str],
) -> tuple[NativeNodeEvidence, ...]:
    """Sondea la evidencia física y Security Descriptors de todos los nodos de un Golden en NTFS.

    Garantías:
    - Fail-closed: falla ante cualquier inconsistencia o enlace no permitido sin devolver parciales.
    - Read-only: ningún archivo o metadato es modificado.
    - Bound a handle: la identidad y SD se extraen de handles nativos abiertos con OPEN_REPARSE_POINT.
    - Ordenamiento: determinista bottom-up con '.' como último elemento absoluto.
    """
    if sys.platform != "win32":
        raise NativeEvidenceUnsupportedError("Plataforma no soportada: probe_node_evidence requiere Windows")

    # SECURITY RULE: normalización léxica estricta sin Path.resolve() para no seguir junctions/symlinks
    try:
        raw_path = os.fspath(root)
    except TypeError as exc:
        raise NativeEvidenceError(f"root debe ser str o PathLike: {exc}") from exc

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise NativeEvidenceError("root no puede ser vacío")

    # Normalización léxica pura
    abs_path_str = os.path.abspath(raw_path.strip())
    root_path = pathlib.Path(abs_path_str)

    # 1. Inspección inicial de la entrada raíz
    tipo_raiz, identidad_raiz = link_kind_and_identity_or_raise(root_path)
    if identidad_raiz is None:
        raise NativeEvidenceError(f"La ruta raíz no existe: '{root_path}'")
    if tipo_raiz is not None:
        raise NativeReparsePointError(f"La ruta raíz es un enlace ({tipo_raiz}): '{root_path}'")
    if not stat.S_ISDIR(identidad_raiz.st_mode):
        raise NativeEvidenceError(f"La ruta raíz no es un directorio: '{root_path}'")

    # 2. Verificación de volumen compatible
    anchor = root_path.anchor
    drive_type = _kernel32.GetDriveTypeW(anchor)
    if drive_type != _DRIVE_FIXED:
        raise NativeEvidenceUnsupportedError(
            f"Volumen no compatible para '{root_path}': drive_type={drive_type} (se requiere volumen fijo local)"
        )

    # 3. Apertura del handle de la raíz y verificación del filesystem
    root_handle = _open_node_handle(root_path)
    try:
        fs_buf = ctypes.create_unicode_buffer(260)
        flags_val = wintypes.DWORD()
        if not _kernel32.GetVolumeInformationByHandleW(
            root_handle,
            None,
            0,
            None,
            None,
            ctypes.byref(flags_val),
            fs_buf,
            260,
        ):
            err = ctypes.get_last_error()
            raise NativeEvidenceError(f"GetVolumeInformationByHandleW falló en '{root_path}': código {err}")

        fs_name = fs_buf.value
        persistent_acls = bool(flags_val.value & _FILE_PERSISTENT_ACLS)
        if fs_name != "NTFS" or not persistent_acls:
            raise NativeEvidenceUnsupportedError(
                f"Filesystem no soportado para '{root_path}': {fs_name} "
                "(se requiere NTFS con soporte de ACLs persistentes)"
            )

        root_evidence = _probe_open_handle(root_handle, ".", expected_kind=GoldenProtectionNodeKind.DIR)
    finally:
        _kernel32.CloseHandle(root_handle)

    # 4. Recorrido estricto fail-closed (Walker seguro)
    # FASE 1: Captura de evidencia de todo el árbol en memoria
    collected_nodes: list[NativeNodeEvidence] = [root_evidence]
    stack: list[pathlib.Path] = [root_path]
    seen_relpaths: set[str] = {"."}

    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as entries:
                sorted_entries = sorted(entries, key=lambda e: e.name)
                for entry in sorted_entries:
                    child_path = pathlib.Path(entry.path)
                    rel_path = child_path.relative_to(root_path).as_posix()
                    if rel_path in seen_relpaths:
                        raise NativeEvidenceError(f"Ruta relativa duplicada en el árbol: {rel_path}")
                    seen_relpaths.add(rel_path)

                    # Inspección lstat previa
                    tipo, c_st = link_kind_and_identity_or_raise(child_path)
                    if c_st is None:
                        raise NativeEvidenceError(f"Entrada desapareció durante recorrido: '{rel_path}'")
                    if tipo is not None:
                        raise NativeReparsePointError(f"Enlace ({tipo}) detectado en '{rel_path}'")

                    is_dir = stat.S_ISDIR(c_st.st_mode)
                    expected_k = GoldenProtectionNodeKind.DIR if is_dir else GoldenProtectionNodeKind.FILE

                    # Apertura del handle nativo
                    h = _open_node_handle(child_path)
                    try:
                        ev = _probe_open_handle(h, rel_path, expected_kind=expected_k)
                    finally:
                        _kernel32.CloseHandle(h)

                    # Verificar pertenencia al mismo volumen del root
                    if ev.backup.volume_serial_number != root_evidence.backup.volume_serial_number:
                        raise NativeEvidenceError(
                            f"Discrepancia de volumen en '{rel_path}': "
                            f"{ev.backup.volume_serial_number} != {root_evidence.backup.volume_serial_number}"
                        )

                    collected_nodes.append(ev)

                    # Solo si el handle confirmó que es directorio real, descendemos
                    if ev.backup.node_kind is GoldenProtectionNodeKind.DIR:
                        stack.append(child_path)
        except OSError as exc:
            raise NativeEvidenceError(f"Error al recorrer '{current_dir}': {exc}") from exc

    # FASE 2: Detección de hardlinks internos (FileIds duplicados dentro del NodeSet)
    seen_ids: dict[tuple[int, int], str] = {}
    for node in collected_nodes:
        phys_id = (node.backup.volume_serial_number, node.backup.file_id)
        if phys_id in seen_ids:
            raise DuplicateFileIdError(
                f"DuplicateFileIdError: dos nodos comparten (VolumeSerialNumber, FileId): "
                f"'{seen_ids[phys_id]}' y '{node.backup.relative_path}'"
            )
        seen_ids[phys_id] = node.backup.relative_path

    # FASE 3: Detección de hardlinks externos en archivos regulares únicos
    for node in collected_nodes:
        if node.backup.node_kind is GoldenProtectionNodeKind.FILE:
            if node.number_of_links != 1:
                raise NativeHardlinkError(
                    f"Archivo regular '{node.backup.relative_path}' tiene "
                    f"NumberOfLinks={node.number_of_links} != 1 (hardlink externo detectado)"
                )
        elif node.backup.node_kind is GoldenProtectionNodeKind.DIR and node.number_of_links < 1:
            raise NativeHardlinkError(
                f"Directorio '{node.backup.relative_path}' tiene NumberOfLinks={node.number_of_links} < 1 inválido"
            )

    # FASE 4: Ordenamiento determinista bottom-up con '.' último
    ordered = tuple(sorted(collected_nodes, key=_bottom_up_evidence_sort_key))
    if ordered[-1].backup.relative_path != ".":
        raise NativeEvidenceError("Error interno de ordenamiento: '.' debe ser el último elemento del resultado")

    return ordered


__all__ = [
    "NativeEvidenceError",
    "NativeEvidenceUnsupportedError",
    "NativeHardlinkError",
    "NativeNodeEvidence",
    "NativeReparsePointError",
    "file_id_128_to_int",
    "probe_node_evidence",
]
