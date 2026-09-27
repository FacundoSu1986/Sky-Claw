"""Plan autoritativo COMPLETO y durable de GP2 — modelo puro (GP2-S4A).

Este módulo cierra la brecha que ``authorization_context.py`` declara explícita
(``NOT_DURABLE_AUTHORIZED_PLAN`` / ``NOT_MUTATION_AUTHORITY``): acá vive el
MODELO del plan autoritativo de ADR 0010 §12.2 paso 7 —bindings + tabla íntegra
de nodos con el ABI PRE de §9.1—, su serialización canónica versionada y su
digest verificable.

Qué ES este módulo y qué NO ES:

- ES puro: no abre handles, no escribe archivos, no habla con Win32. Puede
  importarse y probarse en cualquier plataforma.
- NO es autoridad por existir en memoria. Un ``AuthorizedPlan`` construido acá
  es un MODELO; la autoridad la otorga recién la promoción durable
  (``authorized_plan_store.promote_durable_authorized_plan``), que escribe,
  flushea, re-lee y revalida antes de acuñar ``DurableAuthorizedPlan``.
- NO acepta autoridad desde ``UNTRUSTED_STAGING``. El candidate manifest se
  re-lee en bytes, se liga por ``staging_digest`` contra la evidencia
  privilegiada y se reconstruye como modelo tipado; la identidad física y el
  TreeDigest se cross-bindean contra el payload PPSC y contra el TGR.

Orden normativo de gates (ADR 0010 §10/§11/§12.2 pasos 2-5, §13, §19, §20):

```text
1. sha256(candidate_manifest_bytes) == context.staging_digest      -> si no: REFUSE_TO_PLAN
2. esquema cerrado + validación estructural del manifest            -> si no: REFUSE_TO_PLAN
3. cross-bind plan <-> payload PPSC (7 campos normativos)           -> si no: REFUSE_TO_PLAN
4. cross-bind plan <-> entrada TGR (root, vol, file_id, tree, policy)-> si no: REFUSE_TO_PLAN
5. node_count == PPSC.node_count == len(nodes)                      -> si no: REFUSE_TO_PLAN
6. integridad PRE por nodo (b64/longitud/sha256/flag protegido)     -> si no: REFUSE_TO_PLAN
7. identidad de nodo (duplicados, root único, confinamiento)        -> si no: REFUSE_TO_PLAN
```

Todas las excepciones de este módulo descienden de ``PlanAuthorizationError``
(§24): cualquier fallo ocurre en la fase de autorización del helper, antes de
escribir un solo byte de ``authorized_plan.json``. La CLASIFICACIÓN de
recuperación (``INDETERMINATE`` vs ``NOT_DURABLE``) la determina el store por
evidencia observable en disco, nunca por el tipo de excepción.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from sky_claw.local.runtime_vault.authorization_context import PrivilegedAuthorizationContext
from sky_claw.local.runtime_vault.golden_protection_plan import (
    MANIFEST_NODE_KEYS,
    TREE_DIGEST_KEYS,
    GoldenProtectionPlanError,
    NodeSecurityBackup,
    SecurityBackupIntegrityError,
    deserialize_candidate_manifest,
    node_security_backup_to_dict,
)
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.operator_token import OperatorTokenEvidence
from sky_claw.local.runtime_vault.privileged_boundary import PlanAuthorizationError
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenRegistry,
    TrustedRegistryError,
    TrustedRegistrySchemaError,
    _normalize_windows_root,
    verify_trusted_golden_binding,
)

# ============================================================================
# Constantes normativas
# ============================================================================

#: Versión del schema del plan autoritativo. Un cambio de schema exige una
#: versión nueva: la deserialización es fail-closed ante cualquier otra.
AUTHORIZED_PLAN_SCHEMA_VERSION = "1.0"

#: Nombre normativo del archivo autoritativo dentro de ``operations/<op_id>/``.
AUTHORIZED_PLAN_FILE_NAME = "authorized_plan.json"

#: ``object_name`` de DACL del namespace protegido (§11.3, fila
#: ``operations/<op_id>/``): escritura sólo para el helper elevado.
AUTHORIZED_PLAN_OBJECT_NAME = AUTHORIZED_PLAN_FILE_NAME

#: Claves del objeto raíz del plan autoritativo (esquema CERRADO).
AUTHORIZED_PLAN_ROOT_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "operation_id",
        "canonical_root",
        "volume_serial_number",
        "root_file_id",
        "tree_digest",
        "node_count",
        "policy_version",
        "staging_digest",
        "operator_identity",
        "nodes",
        "plan_digest",
    }
)

#: Claves exactas de la evidencia de identidad del operador.
OPERATOR_IDENTITY_KEYS: frozenset[str] = frozenset({"operator_sid", "token_type", "acquired_via"})

#: El ABI PRE por nodo del plan autoritativo ES el del candidate manifest: misma
#: tupla de claves, para que el respaldo no pueda divergir entre el candidato y
#: la copia durable (§9.2.8: ``authorized_plan.json`` es la única fuente de
#: recuperación).
AUTHORIZED_PLAN_NODE_KEYS: frozenset[str] = MANIFEST_NODE_KEYS

_MAX_UINT64 = (1 << 64) - 1
_MAX_UINT128 = (1 << 128) - 1
_HEX_LOWER = frozenset("0123456789abcdef")

#: Afirmación normativa explícita: la promoción durable NO recalcula el TreeDigest
#: observando el filesystem actual. La autoridad del TreeDigest la fija P3
#: (TGR) y el plan la consume; un refresh pertenece a P3 (§11.4/§20).
AUTHORIZED_PLAN_TREE_DIGEST_SOURCE = "PPSC_PAYLOAD_CROSS_BOUND_TO_TGR_ENTRY"


# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class AuthorizedPlanError(PlanAuthorizationError):
    """Base de excepciones del plan autoritativo (fase de autorización del helper)."""


class AuthorizedPlanSchemaError(AuthorizedPlanError):
    """Violación del esquema cerrado o de la estructura del plan: fail-closed.

    Cubre tanto la lectura de un ``authorized_plan.json`` corrupto/adulterado
    (recuperación: nunca se rellena lo que falta desde el filesystem ni desde
    staging) como la reconstrucción de un manifest inválido.
    """


class AuthorizedPlanDigestError(AuthorizedPlanSchemaError):
    """El ``plan_digest`` almacenado no coincide con el contenido canónico."""


class AuthorizedPlanNotFoundError(AuthorizedPlanError):
    """No existe ``authorized_plan.json`` durable para la operación."""


class AuthorizedPlanAlreadyExistsError(AuthorizedPlanError):
    """La política create-once prohíbe sustituir un plan autoritativo existente."""


class StagingDigestMismatchError(AuthorizedPlanError):
    """``sha256(candidate_manifest_bytes) != PrivilegedAuthorizationContext.staging_digest``."""


class PpscPlanBindingError(AuthorizedPlanError):
    """El plan no coincide exactamente con el payload PPSC confirmado."""


class TrustedRegistryPlanBindingError(AuthorizedPlanError):
    """El plan no coincide exactamente con la entrada del TGR (o no hay entrada)."""


class NodeCountMismatchError(AuthorizedPlanError):
    """``len(nodes)`` no coincide con ``node_count`` del plan/PPSC."""


# ============================================================================
# Modelo inmutable del plan autoritativo
# ============================================================================


@dataclass(frozen=True, slots=True)
class AuthorizedPlan:
    """Plan autoritativo COMPLETO: bindings + tabla íntegra de nodos (§12.2 paso 7a).

    ``plan_digest`` es un campo DERIVADO: se recalcula siempre desde el contenido
    canónico y el valor recibido del caller se ignora. La verificación del digest
    almacenado en disco ocurre en :func:`deserialize_authorized_plan`, comparando
    los bytes del archivo contra la serialización canónica de este modelo.
    """

    schema_version: str
    operation_id: str
    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    tree_digest: TreeDigest
    node_count: int
    policy_version: str
    staging_digest: str
    operator_identity: OperatorTokenEvidence
    nodes: tuple[NodeSecurityBackup, ...]
    plan_digest: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        if self.schema_version != AUTHORIZED_PLAN_SCHEMA_VERSION:
            raise AuthorizedPlanSchemaError(
                f"schema_version debe ser '{AUTHORIZED_PLAN_SCHEMA_VERSION}'; observado '{self.schema_version}'"
            )
        operation_id = _validate_canonical_operation_id(self.operation_id)
        canonical_root = _normalize_canonical_root(self.canonical_root)
        volume_serial = _validate_uint(self.volume_serial_number, "volume_serial_number", _MAX_UINT64)
        root_file_id = _validate_uint(self.root_file_id, "root_file_id", _MAX_UINT128)
        tree_digest = _validate_tree_digest(self.tree_digest)
        policy_version = _validate_nonempty_text(self.policy_version, "policy_version")
        staging_digest = _validate_sha256_hex(self.staging_digest, "staging_digest")
        if not isinstance(self.operator_identity, OperatorTokenEvidence):
            raise AuthorizedPlanSchemaError("operator_identity debe ser OperatorTokenEvidence")
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise AuthorizedPlanSchemaError("nodes debe ser una tupla no vacía de NodeSecurityBackup")
        for node in self.nodes:
            if not isinstance(node, NodeSecurityBackup):
                raise AuthorizedPlanSchemaError("todos los nodos deben ser NodeSecurityBackup")

        _validate_node_set(self.nodes, volume_serial_number=volume_serial, root_file_id=root_file_id)
        if self.node_count != len(self.nodes):
            raise NodeCountMismatchError(
                f"node_count ({self.node_count}) no coincide con len(nodes) ({len(self.nodes)})"
            )

        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "canonical_root", canonical_root)
        object.__setattr__(self, "volume_serial_number", volume_serial)
        object.__setattr__(self, "root_file_id", root_file_id)
        object.__setattr__(self, "tree_digest", tree_digest)
        object.__setattr__(self, "policy_version", policy_version)
        object.__setattr__(self, "staging_digest", staging_digest)
        # Orden determinista de la tabla de nodos: mismo modelo -> mismos bytes
        # canónicos -> mismo digest, independientemente del orden de construcción.
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=_authorized_node_sort_key)))
        object.__setattr__(self, "plan_digest", compute_authorized_plan_digest(self))

    def node_for(self, relative_path: str) -> NodeSecurityBackup | None:
        """Devuelve el registro PRE exacto de un nodo, o ``None`` si no está en el plan."""
        for node in self.nodes:
            if node.relative_path == relative_path:
                return node
        return None


# ============================================================================
# Serialización canónica y digest
# ============================================================================


def authorized_plan_content_dict(plan: AuthorizedPlan) -> dict[str, Any]:
    """Contenido canónico del plan SIN ``plan_digest`` (base del digest)."""
    if not isinstance(plan, AuthorizedPlan):
        raise AuthorizedPlanSchemaError("plan debe ser AuthorizedPlan")
    return {
        "canonical_root": plan.canonical_root,
        "node_count": plan.node_count,
        "nodes": [node_security_backup_to_dict(node) for node in plan.nodes],
        "operation_id": plan.operation_id,
        "operator_identity": {
            "acquired_via": plan.operator_identity.acquired_via,
            "operator_sid": plan.operator_identity.operator_sid,
            "token_type": plan.operator_identity.token_type,
        },
        "policy_version": plan.policy_version,
        "root_file_id": plan.root_file_id,
        "schema_version": plan.schema_version,
        "staging_digest": plan.staging_digest,
        "tree_digest": {
            "bytes": plan.tree_digest.bytes,
            "digest": plan.tree_digest.digest,
            "files": plan.tree_digest.files,
        },
        "volume_serial_number": plan.volume_serial_number,
    }


def serialize_authorized_plan_content(plan: AuthorizedPlan) -> bytes:
    """Bytes canónicos del contenido (UTF-8, sin BOM, claves ordenadas, compacto)."""
    return _canonical_json(authorized_plan_content_dict(plan))


def compute_authorized_plan_digest(plan: AuthorizedPlan) -> str:
    """``authorized_plan_digest`` = SHA-256 de los bytes canónicos del contenido.

    Es una autoridad DISTINTA de ``staging_digest``: aquella liga los bytes del
    candidato no confiable, ésta liga la copia autoritativa y durable que el
    journal binding-a criptográficamente.
    """
    return hashlib.sha256(serialize_authorized_plan_content(plan)).hexdigest()


def serialize_authorized_plan(plan: AuthorizedPlan) -> bytes:
    """Documento completo que se persiste: contenido canónico + ``plan_digest``."""
    payload = authorized_plan_content_dict(plan)
    payload["plan_digest"] = compute_authorized_plan_digest(plan)
    return _canonical_json(payload)


def deserialize_authorized_plan(raw: bytes) -> AuthorizedPlan:
    """Carga un plan autoritativo desde bytes, fail-closed y sin rellenar huecos.

    Exige, en orden: JSON UTF-8 sin claves duplicadas, esquema cerrado,
    reconstrucción del modelo tipado, ``plan_digest`` coincidente con el
    contenido y bytes IDENTICOS a la serialización canónica (un archivo
    reescrito con formato no canónico es rechazado, no "normalizado").
    """
    if not isinstance(raw, bytes) or not raw:
        raise AuthorizedPlanSchemaError("authorized_plan.json no puede estar vacío")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorizedPlanSchemaError(f"authorized_plan.json no es JSON UTF-8 válido: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuthorizedPlanSchemaError("authorized_plan.json debe ser un objeto JSON")
    observed = set(payload.keys())
    if observed != AUTHORIZED_PLAN_ROOT_KEYS:
        missing = sorted(AUTHORIZED_PLAN_ROOT_KEYS - observed)
        extra = sorted(observed - AUTHORIZED_PLAN_ROOT_KEYS)
        raise AuthorizedPlanSchemaError(
            f"esquema de authorized_plan.json no cerrado: claves ausentes={missing}, desconocidas={extra}"
        )

    stored_digest = _validate_sha256_hex(payload["plan_digest"], "plan_digest")

    tree_raw = payload["tree_digest"]
    if not isinstance(tree_raw, dict) or set(tree_raw.keys()) != TREE_DIGEST_KEYS:
        raise AuthorizedPlanSchemaError("tree_digest debe tener exactamente {bytes, digest, files}")
    tree_digest = TreeDigest(
        digest=tree_raw["digest"],
        files=tree_raw["files"],
        bytes=tree_raw["bytes"],
    )

    identity_raw = payload["operator_identity"]
    if not isinstance(identity_raw, dict) or set(identity_raw.keys()) != OPERATOR_IDENTITY_KEYS:
        raise AuthorizedPlanSchemaError(
            "operator_identity debe tener exactamente {operator_sid, token_type, acquired_via}"
        )
    operator_identity = OperatorTokenEvidence(
        operator_sid=identity_raw["operator_sid"],
        token_type=identity_raw["token_type"],
        acquired_via=identity_raw["acquired_via"],
    )

    nodes_raw = payload["nodes"]
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise AuthorizedPlanSchemaError("nodes debe ser una lista no vacía")
    try:
        nodes = tuple(
            NodeSecurityBackup(
                relative_path=item["relative_path"],
                node_kind=item["node_kind"],
                volume_serial_number=item["VolumeSerialNumber"],
                file_id=item["FileId"],
                pre_sd_bytes_b64=item["pre_sd_bytes_b64"],
                pre_sd_length=item["pre_sd_length"],
                pre_sd_sha256=item["pre_sd_sha256"],
                owner_sid=item["owner_sid"],
                group_sid=item["group_sid"],
                dacl_control_flags=item["dacl_control_flags"],
                pre_dacl_protected_flag=item["pre_dacl_protected_flag"],
                sddl_diagnostic=item["sddl_diagnostic"],
            )
            for item in nodes_raw
        )
    except (TypeError, KeyError) as exc:
        raise AuthorizedPlanSchemaError(f"registro de nodo malformado en authorized_plan.json: {exc}") from exc

    node_count = payload["node_count"]
    if isinstance(node_count, bool) or not isinstance(node_count, int):
        raise AuthorizedPlanSchemaError("node_count debe ser un entero")

    try:
        plan = AuthorizedPlan(
            schema_version=payload["schema_version"],
            operation_id=payload["operation_id"],
            canonical_root=payload["canonical_root"],
            volume_serial_number=payload["volume_serial_number"],
            root_file_id=payload["root_file_id"],
            tree_digest=tree_digest,
            node_count=node_count,
            policy_version=payload["policy_version"],
            staging_digest=payload["staging_digest"],
            operator_identity=operator_identity,
            nodes=nodes,
        )
    except SecurityBackupIntegrityError as exc:
        raise AuthorizedPlanSchemaError(f"nodo PRE inválido en authorized_plan.json: {exc}") from exc

    if plan.plan_digest != stored_digest:
        raise AuthorizedPlanDigestError(
            "plan_digest almacenado no coincide con el contenido canónico: "
            f"esperado={plan.plan_digest}, almacenado={stored_digest}"
        )
    if serialize_authorized_plan(plan) != raw:
        raise AuthorizedPlanSchemaError(
            "los bytes de authorized_plan.json no son la serialización canónica del plan reconstruido"
        )
    return plan


# ============================================================================
# Promoción: gates privilegiados sobre evidencia ya establecida
# ============================================================================


def build_authorized_plan(
    *,
    context: PrivilegedAuthorizationContext,
    candidate_manifest_bytes: bytes,
    trusted_registry: TrustedGoldenRegistry,
) -> AuthorizedPlan:
    """Aplica todos los gates de autorización y devuelve el MODELO del plan.

    NO escribe nada en disco. El resultado es un modelo en memoria: sólo la
    promoción durable (``authorized_plan_store``) puede convertirlo en autoridad.

    Gates, en el orden normativo:

    1. ``staging_digest``: los bytes del candidato se re-leen y se ligan contra
       la evidencia privilegiada. Nunca se confía en un objeto Python derivado
       previamente del staging.
    2. Esquema cerrado y validación estructural del manifest (incluida la
       integridad PRE por nodo y la identidad de nodo).
    3. Cross-bind exacto contra el payload PPSC confirmado (los siete campos
       normativos): el root autoritativo NO sale de ``candidate["canonical_root"]``.
    4. Cross-bind exacto contra la entrada del TGR (root, ``VolumeSerialNumber``,
       ``root_file_id``, ``tree_digest`` completo y ``policy_version``).
    5. ``len(nodes) == plan.node_count == PPSC.node_count``.
    """
    if not isinstance(context, PrivilegedAuthorizationContext):
        raise AuthorizedPlanSchemaError("context debe ser PrivilegedAuthorizationContext")
    if not isinstance(trusted_registry, TrustedGoldenRegistry):
        raise AuthorizedPlanSchemaError("trusted_registry debe ser TrustedGoldenRegistry")
    if not isinstance(candidate_manifest_bytes, bytes) or not candidate_manifest_bytes:
        raise StagingDigestMismatchError("candidate_manifest_bytes debe ser bytes no vacío")

    # Gate 1 — staging digest (ADR 0010 §12.2 paso 2).
    observed_digest = hashlib.sha256(candidate_manifest_bytes).hexdigest()
    if observed_digest != context.staging_digest:
        raise StagingDigestMismatchError(
            "sha256(candidate_manifest_bytes) no coincide con staging_digest: "
            f"observado={observed_digest}, esperado={context.staging_digest}"
        )

    # Gate 2 — esquema cerrado + validación estructural (PSC: §12.2 paso 5).
    try:
        candidate = deserialize_candidate_manifest(candidate_manifest_bytes)
    except GoldenProtectionPlanError as exc:
        raise AuthorizedPlanSchemaError(f"candidate manifest inválido: {exc}") from exc

    payload = context.ppsc_confirmation.payload

    # Gate 3 — cross-bind contra el payload PPSC (ADR 0010 §11.0/§12.2 paso 3-4).
    _require_equal(candidate.operation_id, context.operation_id, "operation_id", PpscPlanBindingError)
    if ntpath.normcase(candidate.canonical_root) != ntpath.normcase(payload.canonical_root):
        raise PpscPlanBindingError(
            f"canonical_root del manifest ({candidate.canonical_root}) no coincide con el payload PPSC "
            f"({payload.canonical_root})"
        )
    _require_equal(
        candidate.volume_serial_number,
        payload.volume_serial_number,
        "VolumeSerialNumber",
        PpscPlanBindingError,
    )
    _require_equal(candidate.root_file_id, payload.root_file_id, "root_file_id", PpscPlanBindingError)
    if (
        candidate.tree_digest.digest.lower() != payload.tree_digest.digest.lower()
        or candidate.tree_digest.files != payload.tree_digest.files
        or candidate.tree_digest.bytes != payload.tree_digest.bytes
    ):
        raise PpscPlanBindingError(
            f"TreeDigest del manifest ({candidate.tree_digest}) no coincide con el payload PPSC ({payload.tree_digest})"
        )
    _require_equal(candidate.policy_version, payload.policy_version, "policy_version", PpscPlanBindingError)

    # Gate 4 — cross-bind contra el TGR (ADR 0010 §11.0/§12.2 paso 3, §20).
    # El TGR no se actualiza acá: P3 es la única operación de registro/refresco.
    try:
        entry = verify_trusted_golden_binding(
            trusted_registry,
            payload.canonical_root,
            payload.volume_serial_number,
            payload.root_file_id,
            payload.tree_digest,
        )
    except TrustedRegistryError as exc:
        raise TrustedRegistryPlanBindingError(f"binding TGR fallido: {exc}") from exc
    if entry.policy_version != payload.policy_version:
        raise TrustedRegistryPlanBindingError(
            f"policy_version del manifest ({payload.policy_version}) no coincide con la entrada TGR "
            f"({entry.policy_version})"
        )

    # Gate 5 — node_count (ADR 0010 §19).
    if candidate.node_count != payload.node_count:
        raise NodeCountMismatchError(
            f"node_count del manifest ({candidate.node_count}) no coincide con el payload PPSC ({payload.node_count})"
        )

    return AuthorizedPlan(
        schema_version=AUTHORIZED_PLAN_SCHEMA_VERSION,
        operation_id=context.operation_id,
        canonical_root=payload.canonical_root,
        volume_serial_number=payload.volume_serial_number,
        root_file_id=payload.root_file_id,
        tree_digest=payload.tree_digest,
        node_count=payload.node_count,
        policy_version=payload.policy_version,
        staging_digest=context.staging_digest,
        operator_identity=context.operator_identity,
        nodes=candidate.nodes,
    )


# ============================================================================
# Validadores internos (se reusan los existentes; no se duplican)
# ============================================================================


def _authorized_node_sort_key(node: NodeSecurityBackup) -> tuple[int, str, str]:
    depth = 0 if node.relative_path == "." else node.relative_path.count("/") + 1
    return (-depth, node.relative_path, node.node_kind.value)


def _validate_node_set(
    nodes: tuple[NodeSecurityBackup, ...],
    *,
    volume_serial_number: int,
    root_file_id: int,
) -> None:
    """Identidad de nodo: duplicados, confinamiento y root único (§18/§19).

    Reutiliza el validador del planner (``golden_protection_plan._validate_node_set``)
    en lugar de reescribirlo: es el mismo invariante sobre el mismo ABI.
    """
    from sky_claw.local.runtime_vault.golden_protection_plan import _validate_node_set as _validate

    _validate(nodes, volume_serial_number=volume_serial_number, root_file_id=root_file_id)


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise AuthorizedPlanSchemaError(f"clave JSON duplicada en authorized_plan.json: '{key}'")
        seen[key] = value
    return seen


def _validate_canonical_operation_id(value: object) -> str:
    if not isinstance(value, str):
        raise AuthorizedPlanSchemaError("operation_id debe ser string")
    normalized = value.strip()
    try:
        parsed = uuid.UUID(normalized)
    except ValueError as exc:
        raise AuthorizedPlanSchemaError(f"operation_id '{value}' no es un UUID válido") from exc
    if normalized != str(parsed):
        raise AuthorizedPlanSchemaError("operation_id debe ser UUID canónico con guiones en minúsculas")
    return normalized


def _normalize_canonical_root(value: object) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise AuthorizedPlanSchemaError("canonical_root debe ser un path tipo str/PathLike")
    try:
        return _normalize_windows_root(value)
    except TrustedRegistrySchemaError as exc:
        raise AuthorizedPlanSchemaError(f"canonical_root inválido: {exc}") from exc


def _validate_tree_digest(value: object) -> TreeDigest:
    if not isinstance(value, TreeDigest):
        raise AuthorizedPlanSchemaError("tree_digest debe ser TreeDigest")
    digest = _validate_sha256_hex(value.digest, "tree_digest.digest")
    for name, field_value in (("files", value.files), ("bytes", value.bytes)):
        if isinstance(field_value, bool) or not isinstance(field_value, int) or field_value < 0:
            raise AuthorizedPlanSchemaError(f"tree_digest.{name} debe ser un entero no negativo")
    return TreeDigest(digest=digest, files=value.files, bytes=value.bytes)


def _validate_uint(value: object, field_name: str, max_value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuthorizedPlanSchemaError(f"{field_name} debe ser un entero")
    if not 0 <= value <= max_value:
        raise AuthorizedPlanSchemaError(f"{field_name} debe estar en el rango [0, {max_value}]")
    return value


def _validate_sha256_hex(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AuthorizedPlanSchemaError(f"{field_name} debe ser string")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in _HEX_LOWER for ch in normalized):
        raise AuthorizedPlanSchemaError(f"{field_name} debe ser SHA-256 hexadecimal de 64 caracteres")
    return normalized


def _validate_nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorizedPlanSchemaError(f"{field_name} debe ser un string no vacío")
    return value.strip()


def _require_equal(observed: object, expected: object, field_name: str, error_cls: type[AuthorizedPlanError]) -> None:
    if observed != expected:
        raise error_cls(f"{field_name} no coincide: observado={observed!r}, esperado={expected!r}")


__all__ = [
    "AUTHORIZED_PLAN_NODE_KEYS",
    "AUTHORIZED_PLAN_OBJECT_NAME",
    "AUTHORIZED_PLAN_ROOT_KEYS",
    "AUTHORIZED_PLAN_SCHEMA_VERSION",
    "AUTHORIZED_PLAN_FILE_NAME",
    "AUTHORIZED_PLAN_TREE_DIGEST_SOURCE",
    "OPERATOR_IDENTITY_KEYS",
    "AuthorizedPlan",
    "AuthorizedPlanAlreadyExistsError",
    "AuthorizedPlanDigestError",
    "AuthorizedPlanError",
    "AuthorizedPlanNotFoundError",
    "AuthorizedPlanSchemaError",
    "NodeCountMismatchError",
    "PpscPlanBindingError",
    "StagingDigestMismatchError",
    "TrustedRegistryPlanBindingError",
    "authorized_plan_content_dict",
    "build_authorized_plan",
    "compute_authorized_plan_digest",
    "deserialize_authorized_plan",
    "serialize_authorized_plan",
    "serialize_authorized_plan_content",
]
