"""Núcleo puro del plan candidato de protección Golden (RV-GP2).

Este módulo implementa únicamente la parte no destructiva del ADR 0010:
contratos inmutables, validación fail-closed de evidencia ya observada y
sellado determinista del ``candidate_manifest``. No abre handles Win32, no
solicita elevación y no modifica ACLs.

La captura nativa de Security Descriptors, ``NumberOfLinks``/reparse checks,
Target DACL, ``GoldenMutationLock``, promoción privilegiada a
``authorized_plan.json`` y mutación con ``SetSecurityInfo`` pertenecen a slices
posteriores. Este módulo exige que los checks previos correspondientes lleguen
como evidencia explícita y nunca los infiere.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import ntpath
import os
import re
from dataclasses import dataclass
from enum import StrEnum

from sky_claw.local.runtime_vault.models import (
    GoldenMasterVerificationResult,
    RuntimeVaultError,
    TreeDigest,
)
from sky_claw.local.runtime_vault.protection import (
    GoldenProtectionResult,
    GoldenProtectionRight,
    GoldenProtectionState,
    classify_protection,
)

_SE_DACL_PROTECTED = 0x1000
_SID_RE = re.compile(r"^S-\d+(?:-\d+)+$")
_ALLOWED_INITIAL_STATES = frozenset(
    {
        GoldenProtectionState.UNPROTECTED,
        GoldenProtectionState.WRITE_PROTECTED,
    }
)
_PARENT_UNSAFE_RIGHTS = frozenset(
    {
        GoldenProtectionRight.DELETE_CHILD,
        GoldenProtectionRight.CHANGE_PERMISSIONS,
        GoldenProtectionRight.CHANGE_OWNER,
    }
)
_PARENT_CREATE_RIGHTS = frozenset(
    {
        GoldenProtectionRight.ADD_FILE,
        GoldenProtectionRight.ADD_SUBDIRECTORY,
    }
)


class GoldenProtectionPlanError(RuntimeVaultError):
    """Base para rechazos durante la preparación/sellado del plan GP2."""


class PlanCreationError(GoldenProtectionPlanError):
    """La evidencia no permite producir un plan candidato autorizable."""


class DuplicateFileIdError(PlanCreationError):
    """Dos nodos del Golden comparten la misma identidad física."""


class SecurityBackupIntegrityError(PlanCreationError):
    """El respaldo PRE de un Security Descriptor no satisface su binding."""


class GoldenProtectionPlanState(StrEnum):
    """Estados no autoritativos que puede escribir el coordinador no elevado."""

    PREPARING = "preparing"
    PREPARED = "prepared"
    AWAITING_ELEVATION = "awaiting_elevation"
    CANCELLED = "cancelled"
    ELEVATION_REJECTED = "elevation_rejected"


class GoldenProtectionNodeKind(StrEnum):
    """Tipos de nodo admitidos por el ABI GP2."""

    DIR = "dir"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class NodeSecurityBackup:
    """ABI PRE autoritativo por nodo definido por ADR 0010 §9.1.

    ``pre_sd_bytes_b64`` es la fuente de restauración. ``sddl_diagnostic`` es
    sólo diagnóstico y nunca participa en la verificación criptográfica.
    """

    relative_path: str
    node_kind: GoldenProtectionNodeKind
    volume_serial_number: int
    file_id: int
    pre_sd_bytes_b64: str
    pre_sd_length: int
    pre_sd_sha256: str
    owner_sid: str
    group_sid: str
    dacl_control_flags: int
    pre_dacl_protected_flag: bool
    sddl_diagnostic: str = ""

    def __post_init__(self) -> None:
        relative_path = _validate_canonical_relpath(self.relative_path)
        try:
            node_kind = GoldenProtectionNodeKind(self.node_kind)
        except (TypeError, ValueError) as exc:
            raise SecurityBackupIntegrityError("node_kind debe ser 'dir' o 'file'") from exc

        _validate_uint(self.volume_serial_number, "volume_serial_number")
        _validate_uint(self.file_id, "file_id")
        if not isinstance(self.pre_sd_length, int) or isinstance(self.pre_sd_length, bool) or self.pre_sd_length <= 0:
            raise SecurityBackupIntegrityError("pre_sd_length debe ser un entero positivo")
        if not isinstance(self.dacl_control_flags, int) or isinstance(self.dacl_control_flags, bool):
            raise SecurityBackupIntegrityError("dacl_control_flags debe ser un entero")
        if not 0 <= self.dacl_control_flags <= 0xFFFF:
            raise SecurityBackupIntegrityError("dacl_control_flags debe caber en el control word de 16 bits")
        if not isinstance(self.pre_dacl_protected_flag, bool):
            raise SecurityBackupIntegrityError("pre_dacl_protected_flag debe ser bool")
        expected_protected = bool(self.dacl_control_flags & _SE_DACL_PROTECTED)
        if self.pre_dacl_protected_flag is not expected_protected:
            raise SecurityBackupIntegrityError(
                "pre_dacl_protected_flag no coincide con SE_DACL_PROTECTED en dacl_control_flags"
            )
        _validate_sid(self.owner_sid, "owner_sid")
        _validate_sid(self.group_sid, "group_sid")
        if not isinstance(self.sddl_diagnostic, str):
            raise SecurityBackupIntegrityError("sddl_diagnostic debe ser string")

        sd_bytes = _decode_sd_bytes(self.pre_sd_bytes_b64)
        if len(sd_bytes) != self.pre_sd_length:
            raise SecurityBackupIntegrityError(
                "pre_sd_length no coincide con los bytes decodificados de pre_sd_bytes_b64"
            )
        digest = _validate_sha256(self.pre_sd_sha256, "pre_sd_sha256")
        observed_digest = hashlib.sha256(sd_bytes).hexdigest()
        if observed_digest != digest:
            raise SecurityBackupIntegrityError("sha256(base64_decode(pre_sd_bytes_b64)) no coincide con pre_sd_sha256")

        object.__setattr__(self, "relative_path", relative_path)
        object.__setattr__(self, "node_kind", node_kind)
        object.__setattr__(self, "pre_sd_sha256", digest)

    def decoded_sd_bytes(self) -> bytes:
        """Devuelve los bytes PRE cuya integridad ya fue validada."""
        return _decode_sd_bytes(self.pre_sd_bytes_b64)


@dataclass(frozen=True, slots=True)
class GoldenProtectionSealChecks:
    """Gates observacionales que el slice nativo deberá demostrar antes del sellado.

    No poseen defaults deliberadamente: un caller no puede olvidar declarar
    alguno de los tres gates del sellado pre-mutación.
    """

    reparse_points_absent: bool
    external_hardlinks_absent: bool
    post_inventory_structural_match: bool

    @property
    def all_passed(self) -> bool:
        return (
            self.reparse_points_absent is True
            and self.external_hardlinks_absent is True
            and self.post_inventory_structural_match is True
        )


@dataclass(frozen=True, slots=True)
class GoldenProtectionPlan:
    """Plan candidato PREPARED, aún no autorizado por el helper elevado."""

    operation_id: str
    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    tree_digest: TreeDigest
    policy_version: str
    initial_protection_state: GoldenProtectionState
    nodes: tuple[NodeSecurityBackup, ...]
    state: GoldenProtectionPlanState = GoldenProtectionPlanState.PREPARED

    def __post_init__(self) -> None:
        if self.state is not GoldenProtectionPlanState.PREPARED:
            raise PlanCreationError("GoldenProtectionPlan sólo representa un candidate manifest PREPARED")
        operation_id = _validate_operation_id(self.operation_id)
        canonical_root = _normalize_windows_root(self.canonical_root)
        _validate_uint(self.volume_serial_number, "volume_serial_number")
        _validate_uint(self.root_file_id, "root_file_id")
        if not isinstance(self.tree_digest, TreeDigest):
            raise PlanCreationError("tree_digest debe ser TreeDigest")
        policy_version = _validate_nonempty_text(self.policy_version, "policy_version")
        if self.initial_protection_state not in _ALLOWED_INITIAL_STATES:
            raise PlanCreationError("el plan mutador sólo admite estado inicial UNPROTECTED o WRITE_PROTECTED")
        if not isinstance(self.nodes, tuple):
            raise PlanCreationError("nodes debe ser una tupla inmutable")
        _validate_node_set(
            self.nodes,
            volume_serial_number=self.volume_serial_number,
            root_file_id=self.root_file_id,
        )

        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "canonical_root", canonical_root)
        object.__setattr__(self, "policy_version", policy_version)

    @property
    def node_count(self) -> int:
        return len(self.nodes)


@dataclass(frozen=True, slots=True)
class SealedGoldenProtectionPlan:
    """Candidate manifest serializado canónicamente y ligado a su SHA-256."""

    plan: GoldenProtectionPlan
    candidate_manifest_bytes: bytes
    staging_digest: str

    def __post_init__(self) -> None:
        if not self.candidate_manifest_bytes:
            raise PlanCreationError("candidate_manifest_bytes no puede ser vacío")
        digest = _validate_sha256(self.staging_digest, "staging_digest")
        if hashlib.sha256(self.candidate_manifest_bytes).hexdigest() != digest:
            raise PlanCreationError("staging_digest no coincide con candidate_manifest_bytes")
        object.__setattr__(self, "staging_digest", digest)


def prepare_golden_protection_plan(
    *,
    golden_verification: GoldenMasterVerificationResult,
    protection_result: GoldenProtectionResult,
    operation_id: str,
    canonical_root: str | os.PathLike[str],
    volume_serial_number: int,
    root_file_id: int,
    nodes: tuple[NodeSecurityBackup, ...],
    seal_checks: GoldenProtectionSealChecks,
    policy_version: str = "gp2-v1",
) -> GoldenProtectionPlan:
    """Construye un candidate plan sólo cuando toda evidencia previa es concluyente.

    La función no observa el filesystem. El slice nativo futuro será responsable
    de producir ``nodes`` y ``seal_checks`` mediante handles Win32 y de volver a
    revalidarlos en el helper antes de promover el plan a autoritativo.
    """
    if not isinstance(golden_verification, GoldenMasterVerificationResult):
        raise PlanCreationError("golden_verification debe ser GoldenMasterVerificationResult")
    if not golden_verification.success or golden_verification.descriptor is None:
        raise PlanCreationError("GP2 requiere un Golden Master RV-2 VERIFIED con descriptor")
    descriptor = golden_verification.descriptor
    if descriptor.role != "reference_only":
        raise PlanCreationError("GP2 sólo acepta GoldenMasterDescriptor role='reference_only'")

    normalized_root = _normalize_windows_root(canonical_root)
    descriptor_root = _normalize_windows_root(descriptor.location)
    if ntpath.normcase(normalized_root) != ntpath.normcase(descriptor_root):
        raise PlanCreationError("canonical_root no coincide con la ubicación del Golden RV-2 verificado")

    _validate_protection_preconditions(protection_result)
    if not isinstance(seal_checks, GoldenProtectionSealChecks) or not seal_checks.all_passed:
        raise PlanCreationError(
            "el sellado exige reparse_points_absent, external_hardlinks_absent y "
            "post_inventory_structural_match en True"
        )

    _validate_nodes_against_gp1(protection_result, nodes)
    ordered_nodes = tuple(sorted(nodes, key=lambda node: (node.relative_path, node.node_kind.value)))

    return GoldenProtectionPlan(
        operation_id=operation_id,
        canonical_root=normalized_root,
        volume_serial_number=volume_serial_number,
        root_file_id=root_file_id,
        tree_digest=descriptor.tree_digest,
        policy_version=policy_version,
        initial_protection_state=protection_result.state,
        nodes=ordered_nodes,
    )


def seal_golden_protection_plan(plan: GoldenProtectionPlan) -> SealedGoldenProtectionPlan:
    """Serializa el candidate manifest en JSON canónico y calcula ``staging_digest``."""
    if not isinstance(plan, GoldenProtectionPlan):
        raise PlanCreationError("plan debe ser GoldenProtectionPlan")
    payload = _plan_to_manifest_dict(plan)
    manifest_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return SealedGoldenProtectionPlan(
        plan=plan,
        candidate_manifest_bytes=manifest_bytes,
        staging_digest=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def _validate_protection_preconditions(result: GoldenProtectionResult) -> None:
    if not isinstance(result, GoldenProtectionResult):
        raise PlanCreationError("protection_result debe ser GoldenProtectionResult")
    canonical_result = classify_protection(result.evidence)
    if canonical_result != result:
        raise PlanCreationError("protection_result no coincide con la clasificación canónica GP1")
    if result.state not in _ALLOWED_INITIAL_STATES:
        raise PlanCreationError(
            f"GP2 sólo prepara mutación desde UNPROTECTED o WRITE_PROTECTED; estado observado: {result.state.value}"
        )
    if result.assurance_scope != "CURRENT_EFFECTIVE_UNELEVATED_TOKEN":
        raise PlanCreationError("assurance_scope GP1 no coincide con el contrato GP2")
    if result.scan_scope != "FULL_SUBTREE_AND_PARENT":
        raise PlanCreationError("scan_scope GP1 debe cubrir FULL_SUBTREE_AND_PARENT")
    evidence = result.evidence
    if evidence.current_token_elevated is not False:
        raise PlanCreationError("la planificación GP2 exige evidencia del token efectivo no elevado")
    if evidence.pre_post_structural_match is not True:
        raise PlanCreationError("la evidencia GP1 no demuestra estabilidad estructural PRE/POST")
    parent = evidence.parent_observation
    if parent is None or parent.granted_rights is None:
        raise PlanCreationError("la política del parent no puede evaluarse de forma concluyente")
    unsafe = parent.granted_rights & _PARENT_UNSAFE_RIGHTS
    if unsafe:
        derechos = ", ".join(sorted(right.value for right in unsafe))
        raise PlanCreationError(f"PARENT_UNSAFE -> REFUSE_TO_APPLY ({derechos})")

    observed_nodes = evidence.nodes
    if observed_nodes is None or not observed_nodes:
        raise PlanCreationError("GP1 no entregó un NodeSet completo para evaluar el root")
    roots = [node for node in observed_nodes if node.relative_path in ("", ".")]
    if len(roots) != 1 or roots[0].granted_rights is None:
        raise PlanCreationError("la política del root no puede evaluarse de forma concluyente")
    root = roots[0]
    root_rights = root.granted_rights
    assert root_rights is not None
    parent_create = parent.granted_rights & _PARENT_CREATE_RIGHTS
    if GoldenProtectionRight.DELETE in root_rights and parent_create:
        derechos = ", ".join(sorted(right.value for right in parent_create))
        raise PlanCreationError(f"PARENT_UNSAFE -> REFUSE_TO_APPLY (rename chain: root DELETE + parent {derechos})")


def _validate_nodes_against_gp1(
    result: GoldenProtectionResult,
    nodes: tuple[NodeSecurityBackup, ...],
) -> None:
    observed_nodes = result.evidence.nodes
    if observed_nodes is None or not observed_nodes:
        raise PlanCreationError("GP1 no entregó un NodeSet completo")
    if not nodes:
        raise PlanCreationError("el candidate plan no puede tener NodeSet vacío")

    expected: set[tuple[str, str]] = set()
    for observation in observed_nodes:
        rel_path = "." if observation.relative_path == "" else _validate_canonical_relpath(observation.relative_path)
        try:
            kind = GoldenProtectionNodeKind(observation.node_kind)
        except (TypeError, ValueError) as exc:
            raise PlanCreationError("GP1 contiene node_kind fuera del ABI GP2") from exc
        key = (rel_path, kind.value)
        if key in expected:
            raise PlanCreationError("GP1 contiene nodos duplicados en su NodeSet")
        expected.add(key)

    actual = {(node.relative_path, node.node_kind.value) for node in nodes}
    if actual != expected:
        raise PlanCreationError("MANIFEST_NODE_SET != GP1_NODE_SET")


def _validate_node_set(
    nodes: tuple[NodeSecurityBackup, ...],
    *,
    volume_serial_number: int,
    root_file_id: int,
) -> None:
    if not nodes:
        raise PlanCreationError("nodes no puede estar vacío")

    paths: set[str] = set()
    physical_ids: set[tuple[int, int]] = set()
    root_nodes: list[NodeSecurityBackup] = []
    for node in nodes:
        if not isinstance(node, NodeSecurityBackup):
            raise PlanCreationError("todos los nodos deben ser NodeSecurityBackup")
        if node.relative_path in paths:
            raise PlanCreationError(f"relative_path duplicado en candidate plan: {node.relative_path}")
        paths.add(node.relative_path)
        if node.volume_serial_number != volume_serial_number:
            raise PlanCreationError("todos los nodos deben pertenecer al volumen del root")
        physical_id = (node.volume_serial_number, node.file_id)
        if physical_id in physical_ids:
            raise DuplicateFileIdError("DuplicateFileIdError: dos nodos comparten (VolumeSerialNumber, FileId)")
        physical_ids.add(physical_id)
        if node.relative_path == ".":
            root_nodes.append(node)

    if len(root_nodes) != 1:
        raise PlanCreationError("el candidate plan debe contener exactamente un root relativo '.'")
    root = root_nodes[0]
    if root.node_kind is not GoldenProtectionNodeKind.DIR:
        raise PlanCreationError("el root del Golden debe ser node_kind='dir'")
    if root.file_id != root_file_id:
        raise PlanCreationError("root_file_id no coincide con el NodeSecurityBackup del root")


def _plan_to_manifest_dict(plan: GoldenProtectionPlan) -> dict[str, object]:
    return {
        "TreeDigest": {
            "bytes": plan.tree_digest.bytes,
            "digest": plan.tree_digest.digest,
            "files": plan.tree_digest.files,
        },
        "VolumeSerialNumber": plan.volume_serial_number,
        "canonical_root": plan.canonical_root,
        "initial_protection_state": plan.initial_protection_state.value,
        "node_count": plan.node_count,
        "nodes": [
            {
                "FileId": node.file_id,
                "VolumeSerialNumber": node.volume_serial_number,
                "dacl_control_flags": node.dacl_control_flags,
                "group_sid": node.group_sid,
                "node_kind": node.node_kind.value,
                "owner_sid": node.owner_sid,
                "pre_dacl_protected_flag": node.pre_dacl_protected_flag,
                "pre_sd_bytes_b64": node.pre_sd_bytes_b64,
                "pre_sd_length": node.pre_sd_length,
                "pre_sd_sha256": node.pre_sd_sha256,
                "relative_path": node.relative_path,
                "sddl_diagnostic": node.sddl_diagnostic,
            }
            for node in plan.nodes
        ],
        "operation_id": plan.operation_id,
        "policy_version": plan.policy_version,
        "root_file_id": plan.root_file_id,
        "state": plan.state.value,
    }


def _decode_sd_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise SecurityBackupIntegrityError("pre_sd_bytes_b64 debe ser Base64 no vacío")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecurityBackupIntegrityError("pre_sd_bytes_b64 no es Base64 válido") from exc
    if not decoded:
        raise SecurityBackupIntegrityError("pre_sd_bytes_b64 no puede decodificar a bytes vacíos")
    return decoded


def _validate_sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise SecurityBackupIntegrityError(f"{field_name} debe ser string")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise SecurityBackupIntegrityError(f"{field_name} debe ser SHA-256 hexadecimal de 64 caracteres")
    return normalized


def _validate_sid(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SID_RE.fullmatch(value) is None:
        raise SecurityBackupIntegrityError(f"{field_name} no tiene formato SID canónico S-...")


def _validate_uint(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PlanCreationError(f"{field_name} debe ser un entero >= 0")


def _validate_canonical_relpath(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise SecurityBackupIntegrityError("relative_path no puede ser vacío")
    if value == ".":
        return value
    if "\x00" in value or "\\" in value or ":" in value or value.startswith("/"):
        raise SecurityBackupIntegrityError("relative_path debe ser POSIX relativo canónico dentro del Golden")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SecurityBackupIntegrityError("relative_path contiene segmentos no canónicos o traversal")
    return value


def _validate_operation_id(value: str) -> str:
    normalized = _validate_nonempty_text(value, "operation_id")
    if normalized in {".", ".."} or any(ch in normalized for ch in ("/", "\\", "\x00")):
        raise PlanCreationError("operation_id no puede escapar su directorio de staging")
    return normalized


def _validate_nonempty_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise PlanCreationError(f"{field_name} debe ser string")
    normalized = value.strip()
    if not normalized:
        raise PlanCreationError(f"{field_name} no puede ser vacío ni whitespace-only")
    return normalized


def _normalize_windows_root(value: str | os.PathLike[str]) -> str:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise PlanCreationError("canonical_root debe ser str o PathLike") from exc
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise PlanCreationError("canonical_root debe ser una ruta Windows local no vacía")
    raw = raw.strip()
    if raw.startswith(("\\\\", "//")):
        raise PlanCreationError("canonical_root UNC/remoto no está soportado por GP2 v1")
    normalized = ntpath.normpath(raw)
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith(("\\", "/")):
        raise PlanCreationError("canonical_root debe ser una ruta Windows absoluta con volumen local")
    return normalized


__all__ = [
    "DuplicateFileIdError",
    "GoldenProtectionNodeKind",
    "GoldenProtectionPlan",
    "GoldenProtectionPlanError",
    "GoldenProtectionPlanState",
    "GoldenProtectionSealChecks",
    "NodeSecurityBackup",
    "PlanCreationError",
    "SealedGoldenProtectionPlan",
    "SecurityBackupIntegrityError",
    "prepare_golden_protection_plan",
    "seal_golden_protection_plan",
]
