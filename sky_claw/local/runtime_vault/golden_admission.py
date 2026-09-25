"""Golden Admission — DTOs de autoridad y confirmación (ADR 0010 §11.4; GP2-P3).

Congela los modelos del workflow de admisión/refresco del Trusted Golden, que
es **independiente** del FSM GP2 apply: aquí no se agrega ``AUTHORIZED`` ni
ningún otro estado al FSM de apply, y ningún flujo usa el
``GoldenMasterVerificationResult`` staged como resultado autoritativo.

Modelos (§11.4 "Modelos de admission"):

```text
GoldenAdmissionObservation   # OBSERVED; no expected_tree y no autoridad
GoldenAdmissionExpectation   # ADMITTED; expected_tree + expected_runtime + source
GoldenAdmissionReceipt       # inmutable, emitido por helper, operation-bound
GoldenAdmissionState = {OBSERVED, ADMITTED, VERIFIED, REJECTED}
```

Invariantes estructurales que este módulo impone (no delega al caller):

- una observación **no puede** auto-admitirse ni auto-producir ``VERIFIED``
  (RVO-02): su ``state`` está congelado a ``OBSERVED``;
- una expectativa sólo puede ser ``ADMITTED`` (RVO-01/RVO-03): un digest de
  staging jamás se convierte en ``expected_tree`` porque el constructor exige
  los modelos tipados completos;
- el receipt es helper-issued: su ``nonce`` CSPRNG se genera dentro de
  :func:`issue_golden_admission_receipt` y nunca acepta valor del caller;
- ``source_provenance_digest`` es obligatorio en ``INDEPENDENT_PROVENANCE`` y
  obligatoriamente ``None`` en ``OPERATOR_TOFU``: TOFU no puede declararse
  provenance por rellenar ese campo (§11.4);
- ``policy_version`` sólo puede ser la policy ACTIVA del helper
  (:data:`ACTIVE_HELPER_POLICY_VERSION`): staging no puede elegirla ni
  sustituirla;
- la confirmación de Golden Admission es un DTO **distinto** de
  ``PrivilegedPlanConfirmation``: los siete campos normativos de GP2 apply no
  se distorsionan ni se reutilizan acá.

No se incluye ningún provider de confirmación por defecto, ni uno que
confirme: el servicio recibe el puerto y, sin él, la operación se rehúsa.
"""

from __future__ import annotations

import secrets
import string
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from sky_claw.local.runtime_vault.critical_expectations import critical_expectations_digest
from sky_claw.local.runtime_vault.models import (
    CriticalFileEvidence,
    CriticalFileExpectation,
    RuntimeIdentity,
    RuntimeVaultError,
    TreeDigest,
)
from sky_claw.local.runtime_vault.physical_root import PhysicalRootIdentity
from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedRegistrySchemaError,
    _normalize_windows_root,
    _validate_canonical_sid,
    _validate_iso8601_utc,
    _validate_sha256_hex,
)

_UTF8_LOWER_HEX = frozenset(string.hexdigits.lower())
_MAX_UINT64 = (1 << 64) - 1
_MAX_UINT128 = (1 << 128) - 1
_NONCE_HEX_LENGTH = 64  # secrets.token_hex(32)

#: Versión ACTIVA de la policy del helper. La elige SÓLO el helper; staging no
#: puede elegirla ni sustituirla (§11.4 "Bindings normativos del receipt").
ACTIVE_HELPER_POLICY_VERSION = "gp2-v1"

#: Advertencia literal que el diálogo de confirmación TOFU debe exhibir (§11.4).
OPERATOR_TOFU_WARNING = "OPERATOR_TOFU DOES NOT DETECT PRE-EXISTING COMPROMISE"

#: Campos normativos mínimos del ``GoldenAdmissionReceipt`` (§11.4).
GOLDEN_ADMISSION_RECEIPT_FIELDS: tuple[str, ...] = (
    "operation_id",
    "canonical_root",
    "volume_serial_number",
    "root_file_id",
    "tree_digest",
    "expected_runtime",
    "policy_version",
    "admission_source",
    "operator_sid",
    "admitted_at",
    "critical_expectations_digest",
    "source_provenance_digest",
    "nonce",
)

#: Campos exhibidos por el payload de confirmación de Golden Admission.
GOLDEN_ADMISSION_CONFIRMATION_FIELDS: tuple[str, ...] = (
    "operation_id",
    "canonical_root",
    "volume_serial_number",
    "root_file_id",
    "proposed_tree",
    "proposed_runtime",
    "policy_version",
    "previous_tgr_tree_digest",
    "admission_source",
    "critical_expectations",
    "stage",
    "critical_expectations_digest",
)

__all__ = [
    "ACTIVE_HELPER_POLICY_VERSION",
    "GOLDEN_ADMISSION_CONFIRMATION_FIELDS",
    "GOLDEN_ADMISSION_RECEIPT_FIELDS",
    "OPERATOR_TOFU_WARNING",
    "GoldenAdmissionCommitError",
    "GoldenAdmissionConcurrentChangeError",
    "GoldenAdmissionConfirmation",
    "GoldenAdmissionConfirmationError",
    "GoldenAdmissionConfirmationProvider",
    "GoldenAdmissionConfirmationReceipt",
    "GoldenAdmissionConfirmationResult",
    "GoldenAdmissionConfirmationStage",
    "GoldenAdmissionError",
    "GoldenAdmissionExpectation",
    "GoldenAdmissionModelError",
    "GoldenAdmissionObservation",
    "GoldenAdmissionOutcome",
    "GoldenAdmissionReceipt",
    "GoldenAdmissionRejectedError",
    "GoldenAdmissionRejectionReason",
    "GoldenAdmissionRequestError",
    "GoldenAdmissionSource",
    "GoldenAdmissionSourceError",
    "GoldenAdmissionSourceUnavailableError",
    "GoldenAdmissionState",
    "GoldenAdmissionStoreError",
    "issue_golden_admission_receipt",
    "require_golden_admission_outcome",
    "validate_operation_id",
]


# ============================================================================
# Excepciones
# ============================================================================


class GoldenAdmissionError(RuntimeVaultError):
    """Base de excepciones de Golden Admission."""


class GoldenAdmissionModelError(GoldenAdmissionError):
    """Un DTO de Golden Admission viola su contrato de datos: fail-closed."""


class GoldenAdmissionRequestError(GoldenAdmissionError):
    """La solicitud entrante es inválida (sin side effects)."""


class GoldenAdmissionSourceError(GoldenAdmissionRequestError):
    """El ``admission_source`` no pertenece al conjunto cerrado de fuentes."""


class GoldenAdmissionRejectedError(GoldenAdmissionError):
    """El operador no confirmó (o el provider devolvió un valor no contractual)."""


class GoldenAdmissionSourceUnavailableError(GoldenAdmissionError):
    """El source solicitado no tiene proveedor concreto disponible: fail-closed."""


class GoldenAdmissionStoreError(GoldenAdmissionError):
    """No se pudo persistir o revalidar el registro protegido de operación."""


class GoldenAdmissionConcurrentChangeError(GoldenAdmissionError):
    """El TGR vivo cambió entre la lectura ``before`` y el lock: cero TGR writes."""


class GoldenAdmissionCommitError(GoldenAdmissionError):
    """El replace del TGR falló o su resultado no puede determinarse."""


# ============================================================================
# Enumeraciones cerradas
# ============================================================================


class GoldenAdmissionSource(StrEnum):
    """Fuentes cerradas de expectativa (§11.4): conjunto exacto y cerrado.

    ``GOLDEN_ADMISSION_SOURCES = {INDEPENDENT_PROVENANCE, OPERATOR_TOFU}``
    (§11.0). Cualquier otro valor — incluido un source "mejorado" inventado
    por staging — se rechaza en la frontera de la solicitud.
    """

    INDEPENDENT_PROVENANCE = "INDEPENDENT_PROVENANCE"
    OPERATOR_TOFU = "OPERATOR_TOFU"


class GoldenAdmissionState(StrEnum):
    """Estados del workflow de admission (NO son estados del FSM GP2 apply)."""

    OBSERVED = "OBSERVED"
    ADMITTED = "ADMITTED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class GoldenAdmissionOutcome(StrEnum):
    """Desenlace terminal de la operación REGISTER_OR_REFRESH_TRUSTED_GOLDEN."""

    REGISTERED = "REGISTERED"
    REJECTED = "REJECTED"
    COMMIT_OUTCOME_UNKNOWN = "COMMIT_OUTCOME_UNKNOWN"


class GoldenAdmissionRejectionReason(StrEnum):
    """Causas tipadas de ``REJECTED`` (todas preservan el TGR sin modificar)."""

    REQUEST_INVALID = "REQUEST_INVALID"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_INVALID = "SOURCE_INVALID"
    TOKEN_UNAVAILABLE = "TOKEN_UNAVAILABLE"
    OBSERVE_FAILED = "OBSERVE_FAILED"
    RUNTIME_OBSERVATION_FAILED = "RUNTIME_OBSERVATION_FAILED"
    OPERATOR_REJECTED = "OPERATOR_REJECTED"
    RV2_MISMATCH = "RV2_MISMATCH"
    PHYSICAL_BINDING_MISMATCH = "PHYSICAL_BINDING_MISMATCH"
    POLICY_MISMATCH = "POLICY_MISMATCH"
    AUDIT_RECORD_FAILED = "AUDIT_RECORD_FAILED"
    TGR_CONCURRENT_CHANGE = "TGR_CONCURRENT_CHANGE"
    STORE_FAILED = "STORE_FAILED"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"


class GoldenAdmissionConfirmationStage(StrEnum):
    """Momento del flujo en que se pide la confirmación privilegiada."""

    TOFU_PRE_RERUN = "TOFU_PRE_RERUN"
    PROVENANCE_POST_VERIFIED = "PROVENANCE_POST_VERIFIED"


class GoldenAdmissionConfirmationResult(StrEnum):
    """Únicos desenlaces admisibles de la confirmación: sin defaults."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


# ============================================================================
# Validadores comunes
# ============================================================================


def validate_operation_id(value: object) -> str:
    """Valida un ``operation_id`` UUID canónico (minúsculas, con guiones)."""
    if not isinstance(value, str):
        raise GoldenAdmissionModelError("operation_id debe ser string")
    normalized = value.strip()
    try:
        parsed = uuid.UUID(normalized)
    except ValueError as exc:
        raise GoldenAdmissionModelError(f"operation_id '{value}' no es un UUID válido") from exc
    if normalized != str(parsed):
        raise GoldenAdmissionModelError(f"operation_id '{value}' debe ser UUID canónico con guiones en minúsculas")
    return normalized


def _validate_uint(value: object, field_name: str, *, max_value: int, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GoldenAdmissionModelError(f"{field_name} debe ser un entero")
    lower = 0 if allow_zero else 1
    if not lower <= value <= max_value:
        raise GoldenAdmissionModelError(f"{field_name} debe estar en el rango [{lower}, {max_value}]")
    return value


def _validate_tree_digest(value: object, field_name: str) -> TreeDigest:
    if not isinstance(value, TreeDigest):
        raise GoldenAdmissionModelError(f"{field_name} debe ser TreeDigest")
    digest = _validate_sha256_hex(value.digest)
    if isinstance(value.files, bool) or not isinstance(value.files, int) or value.files < 0:
        raise GoldenAdmissionModelError(f"{field_name}.files debe ser entero no negativo")
    if isinstance(value.bytes, bool) or not isinstance(value.bytes, int) or value.bytes < 0:
        raise GoldenAdmissionModelError(f"{field_name}.bytes debe ser entero no negativo")
    return TreeDigest(digest=digest, files=value.files, bytes=value.bytes)


def _validate_runtime(value: object, field_name: str) -> RuntimeIdentity:
    if not isinstance(value, RuntimeIdentity):
        raise GoldenAdmissionModelError(f"{field_name} debe ser RuntimeIdentity")
    if not isinstance(value.game_key, str) or not value.game_key.strip():
        raise GoldenAdmissionModelError(f"{field_name}.game_key no puede ser vacío")
    if not isinstance(value.game_version, str) or not value.game_version.strip():
        raise GoldenAdmissionModelError(f"{field_name}.game_version no puede ser vacío")
    return RuntimeIdentity(game_key=value.game_key.strip(), game_version=value.game_version.strip())


def _validate_nonce(value: object) -> str:
    if not isinstance(value, str):
        raise GoldenAdmissionModelError("nonce debe ser string")
    clean = value.strip().lower()
    if len(clean) != _NONCE_HEX_LENGTH or any(ch not in _UTF8_LOWER_HEX for ch in clean):
        raise GoldenAdmissionModelError(f"nonce debe ser {_NONCE_HEX_LENGTH} caracteres hex de un CSPRNG de 32 bytes")
    return clean


def _validate_critical_digest(value: object) -> str:
    if not isinstance(value, str):
        raise GoldenAdmissionModelError("critical_expectations_digest debe ser string")
    try:
        return _validate_sha256_hex(value)
    except TrustedRegistrySchemaError as exc:
        raise GoldenAdmissionModelError(f"critical_expectations_digest inválido: {exc}") from exc


def _validate_critical_tuple(value: object, field_name: str) -> tuple[CriticalFileExpectation, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GoldenAdmissionModelError(f"{field_name} debe ser una secuencia de CriticalFileExpectation")
    items = tuple(value)
    for item in items:
        if not isinstance(item, CriticalFileExpectation):
            raise GoldenAdmissionModelError(
                f"{field_name} sólo puede contener CriticalFileExpectation; obtenido {type(item).__name__}"
            )
    return items


def _validate_source(value: object, field_name: str = "admission_source") -> GoldenAdmissionSource:
    if isinstance(value, GoldenAdmissionSource):
        return value
    try:
        return GoldenAdmissionSource(value)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise GoldenAdmissionSourceError(
            f"{field_name} '{value}' no pertenece al conjunto cerrado {{INDEPENDENT_PROVENANCE, OPERATOR_TOFU}}"
        ) from exc


def _validate_optional_sha256(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GoldenAdmissionModelError(f"{field_name} debe ser string SHA-256 o null")
    try:
        return _validate_sha256_hex(value)
    except TrustedRegistrySchemaError as exc:
        raise GoldenAdmissionModelError(f"{field_name} inválido: {exc}") from exc


def _validate_policy_version(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoldenAdmissionModelError("policy_version debe ser un string no vacío")
    normalized = value.strip()
    if normalized != ACTIVE_HELPER_POLICY_VERSION:
        raise GoldenAdmissionModelError(
            f"policy_version '{normalized}' no es la policy ACTIVA del helper "
            f"('{ACTIVE_HELPER_POLICY_VERSION}'): staging no puede elegirla ni sustituirla"
        )
    return normalized


def _validate_canonical_root(value: object) -> str:
    try:
        return _normalize_windows_root(value)  # type: ignore[arg-type]
    except TrustedRegistrySchemaError as exc:
        raise GoldenAdmissionModelError(f"canonical_root inválido: {exc}") from exc


def _validate_sid(value: object) -> str:
    try:
        return _validate_canonical_sid(value)
    except TrustedRegistrySchemaError as exc:
        raise GoldenAdmissionModelError(f"operator_sid inválido: {exc}") from exc


def _validate_timestamp(value: object) -> str:
    try:
        return _validate_iso8601_utc(value)  # type: ignore[arg-type]
    except TrustedRegistrySchemaError as exc:
        raise GoldenAdmissionModelError(f"admitted_at inválido: {exc}") from exc


# ============================================================================
# GoldenAdmissionObservation — OBSERVED (sin autoridad)
# ============================================================================


@dataclass(frozen=True, slots=True)
class GoldenAdmissionObservation:
    """Resultado de un pase ``OBSERVE``: mide, no autoriza (§11.4).

    ``state`` está congelado a ``OBSERVED``: una observación no puede
    auto-admitirse ni auto-producir ``VERIFIED`` (RVO-02).
    """

    state: GoldenAdmissionState
    physical_root: PhysicalRootIdentity
    observed_tree: TreeDigest
    observed_runtime: FreshRuntimeObservation
    critical_evidences: tuple[CriticalFileEvidence, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        if self.state is not GoldenAdmissionState.OBSERVED:
            raise GoldenAdmissionModelError(
                f"GoldenAdmissionObservation sólo admite state OBSERVED; obtenido '{self.state}'"
            )
        if not isinstance(self.physical_root, PhysicalRootIdentity):
            raise GoldenAdmissionModelError("physical_root debe ser PhysicalRootIdentity")
        object.__setattr__(self, "observed_tree", _validate_tree_digest(self.observed_tree, "observed_tree"))
        if not isinstance(self.observed_runtime, FreshRuntimeObservation):
            raise GoldenAdmissionModelError("observed_runtime debe ser FreshRuntimeObservation")
        if not isinstance(self.critical_evidences, tuple):
            raise GoldenAdmissionModelError("critical_evidences debe ser tuple")
        for ev in self.critical_evidences:
            if not isinstance(ev, CriticalFileEvidence):
                raise GoldenAdmissionModelError("critical_evidences sólo admite CriticalFileEvidence")
        if not isinstance(self.message, str):
            raise GoldenAdmissionModelError("message debe ser string")


# ============================================================================
# GoldenAdmissionExpectation — ADMITTED
# ============================================================================


@dataclass(frozen=True, slots=True)
class GoldenAdmissionExpectation:
    """Expectativa ``ADMITTED``: expected_tree + expected_runtime + source.

    ``critical_expectations_digest`` se deriva SIEMPRE de la propia lista
    (nunca se suministra aparte): el digest de la lista queda ligado a la
    misma expectativa que TreeDigest/runtime (RVO-10).
    """

    state: GoldenAdmissionState
    source: GoldenAdmissionSource
    expected_tree: TreeDigest
    expected_runtime: RuntimeIdentity
    critical_expectations: tuple[CriticalFileExpectation, ...] = ()
    source_provenance_digest: str | None = None
    critical_expectations_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.state is not GoldenAdmissionState.ADMITTED:
            raise GoldenAdmissionModelError(
                f"GoldenAdmissionExpectation sólo admite state ADMITTED; obtenido '{self.state}'"
            )
        object.__setattr__(self, "source", _validate_source(self.source))
        object.__setattr__(self, "expected_tree", _validate_tree_digest(self.expected_tree, "expected_tree"))
        object.__setattr__(self, "expected_runtime", _validate_runtime(self.expected_runtime, "expected_runtime"))
        object.__setattr__(
            self,
            "critical_expectations",
            _validate_critical_tuple(self.critical_expectations, "critical_expectations"),
        )
        provenance = _validate_optional_sha256(self.source_provenance_digest, "source_provenance_digest")
        if self.source is GoldenAdmissionSource.INDEPENDENT_PROVENANCE:
            if provenance is None:
                raise GoldenAdmissionModelError("INDEPENDENT_PROVENANCE exige source_provenance_digest no nulo (§11.4)")
            object.__setattr__(self, "source_provenance_digest", provenance)
        else:
            if provenance is not None:
                raise GoldenAdmissionModelError(
                    "OPERATOR_TOFU exige source_provenance_digest = null: TOFU no puede declararse "
                    "provenance por rellenar ese campo (§11.4)"
                )
        object.__setattr__(
            self,
            "critical_expectations_digest",
            critical_expectations_digest(self.critical_expectations),
        )


# ============================================================================
# GoldenAdmissionReceipt — helper-issued, inmutable, operation-bound
# ============================================================================


@dataclass(frozen=True, slots=True)
class GoldenAdmissionReceipt:
    """Receipt inmutable de Golden Admission, creado sólo por el helper.

    Contrato cerrado (§11.4): sus 13 campos normativos, ninguno menos y ninguno
    más. Nunca se deserializa desde ``UNTRUSTED_STAGING``: sólo se acuña con
    :func:`issue_golden_admission_receipt`.
    """

    operation_id: str
    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    tree_digest: TreeDigest
    expected_runtime: RuntimeIdentity
    policy_version: str
    admission_source: GoldenAdmissionSource
    operator_sid: str
    admitted_at: str
    critical_expectations_digest: str
    source_provenance_digest: str | None
    nonce: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", validate_operation_id(self.operation_id))
        object.__setattr__(self, "canonical_root", _validate_canonical_root(self.canonical_root))
        object.__setattr__(
            self,
            "volume_serial_number",
            _validate_uint(self.volume_serial_number, "volume_serial_number", max_value=_MAX_UINT64, allow_zero=True),
        )
        object.__setattr__(
            self,
            "root_file_id",
            _validate_uint(self.root_file_id, "root_file_id", max_value=_MAX_UINT128, allow_zero=True),
        )
        object.__setattr__(self, "tree_digest", _validate_tree_digest(self.tree_digest, "tree_digest"))
        object.__setattr__(self, "expected_runtime", _validate_runtime(self.expected_runtime, "expected_runtime"))
        object.__setattr__(self, "policy_version", _validate_policy_version(self.policy_version))
        object.__setattr__(self, "admission_source", _validate_source(self.admission_source))
        object.__setattr__(self, "operator_sid", _validate_sid(self.operator_sid))
        object.__setattr__(self, "admitted_at", _validate_timestamp(self.admitted_at))
        object.__setattr__(
            self, "critical_expectations_digest", _validate_critical_digest(self.critical_expectations_digest)
        )

        provenance = _validate_optional_sha256(self.source_provenance_digest, "source_provenance_digest")
        if self.admission_source is GoldenAdmissionSource.INDEPENDENT_PROVENANCE:
            if provenance is None:
                raise GoldenAdmissionModelError("INDEPENDENT_PROVENANCE exige source_provenance_digest no nulo (§11.4)")
        else:
            if provenance is not None:
                raise GoldenAdmissionModelError("OPERATOR_TOFU exige source_provenance_digest = null (§11.4)")
        object.__setattr__(self, "source_provenance_digest", provenance)

        object.__setattr__(self, "nonce", _validate_nonce(self.nonce))


def issue_golden_admission_receipt(
    *,
    operation_id: str,
    canonical_root: str,
    volume_serial_number: int,
    root_file_id: int,
    tree_digest: TreeDigest,
    expected_runtime: RuntimeIdentity,
    admission_source: GoldenAdmissionSource,
    operator_sid: str,
    admitted_at: str,
    critical_expectations_digest: str,
    source_provenance_digest: str | None,
    policy_version: str = ACTIVE_HELPER_POLICY_VERSION,
) -> GoldenAdmissionReceipt:
    """Acuña un receipt helper-issued con ``nonce`` CSPRNG de una sola emisión.

    El caller NO puede suministrar el ``nonce``: se genera aquí con
    ``secrets.token_hex(32)`` para que el valor jamás venga de staging.
    """
    return GoldenAdmissionReceipt(
        operation_id=operation_id,
        canonical_root=canonical_root,
        volume_serial_number=volume_serial_number,
        root_file_id=root_file_id,
        tree_digest=tree_digest,
        expected_runtime=expected_runtime,
        policy_version=policy_version,
        admission_source=_validate_source(admission_source),
        operator_sid=operator_sid,
        admitted_at=admitted_at,
        critical_expectations_digest=critical_expectations_digest,
        source_provenance_digest=source_provenance_digest,
        nonce=secrets.token_hex(32),
    )


# ============================================================================
# Confirmación privilegiada de Golden Admission (DTO propio, no PPSC)
# ============================================================================


@dataclass(frozen=True, slots=True)
class GoldenAdmissionConfirmation:
    """Payload de la confirmación privilegiada de Golden Admission (§11.4).

    Es un DTO **distinto** de ``PrivilegedPlanConfirmation``: los siete campos
    normativos de GP2 apply no se distorsionan ni se reutilizan acá. En TOFU
    confirma exactamente la identidad física, el ``TreeDigest B`` observado,
    el runtime observado, la ``policy_version``, el digest anterior del TGR o
    ``ABSENT``, el source y el digest/valor de ``critical_expectations``
    (también cuando es ``()``).
    """

    operation_id: str
    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    proposed_tree: TreeDigest
    proposed_runtime: RuntimeIdentity
    policy_version: str
    previous_tgr_tree_digest: str | None
    admission_source: GoldenAdmissionSource
    critical_expectations: tuple[CriticalFileExpectation, ...]
    stage: GoldenAdmissionConfirmationStage
    critical_expectations_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", validate_operation_id(self.operation_id))
        object.__setattr__(self, "canonical_root", _validate_canonical_root(self.canonical_root))
        object.__setattr__(
            self,
            "volume_serial_number",
            _validate_uint(self.volume_serial_number, "volume_serial_number", max_value=_MAX_UINT64, allow_zero=True),
        )
        object.__setattr__(
            self,
            "root_file_id",
            _validate_uint(self.root_file_id, "root_file_id", max_value=_MAX_UINT128, allow_zero=True),
        )
        object.__setattr__(self, "proposed_tree", _validate_tree_digest(self.proposed_tree, "proposed_tree"))
        object.__setattr__(self, "proposed_runtime", _validate_runtime(self.proposed_runtime, "proposed_runtime"))
        object.__setattr__(self, "policy_version", _validate_policy_version(self.policy_version))
        object.__setattr__(
            self,
            "previous_tgr_tree_digest",
            _validate_optional_sha256(self.previous_tgr_tree_digest, "previous_tgr_tree_digest"),
        )
        object.__setattr__(self, "admission_source", _validate_source(self.admission_source))
        object.__setattr__(
            self, "critical_expectations", _validate_critical_tuple(self.critical_expectations, "critical_expectations")
        )
        if not isinstance(self.stage, GoldenAdmissionConfirmationStage):
            raise GoldenAdmissionModelError("stage debe ser GoldenAdmissionConfirmationStage")
        if self.stage is GoldenAdmissionConfirmationStage.TOFU_PRE_RERUN and (
            self.admission_source is not GoldenAdmissionSource.OPERATOR_TOFU
        ):
            raise GoldenAdmissionModelError("El stage TOFU_PRE_RERUN exige admission_source=OPERATOR_TOFU")
        if self.stage is GoldenAdmissionConfirmationStage.PROVENANCE_POST_VERIFIED and (
            self.admission_source is not GoldenAdmissionSource.INDEPENDENT_PROVENANCE
        ):
            raise GoldenAdmissionModelError(
                "El stage PROVENANCE_POST_VERIFIED exige admission_source=INDEPENDENT_PROVENANCE"
            )
        object.__setattr__(
            self,
            "critical_expectations_digest",
            critical_expectations_digest(self.critical_expectations),
        )

    @property
    def previous_tgr_display(self) -> str:
        """``ABSENT`` cuando no hay entrada previa en el TGR."""
        return "ABSENT" if self.previous_tgr_tree_digest is None else self.previous_tgr_tree_digest

    @property
    def tofu_warning(self) -> str:
        """Advertencia literal TOFU; cadena vacía cuando la fuente no es TOFU."""
        if self.admission_source is GoldenAdmissionSource.OPERATOR_TOFU:
            return OPERATOR_TOFU_WARNING
        return ""

    @property
    def expected_display(self) -> str:
        """Texto old/absent → new exhibido al operador."""
        return f"{self.previous_tgr_display} -> {self.proposed_tree.digest}"


class GoldenAdmissionConfirmationError(RuntimeVaultError):
    """Base de excepciones del puerto de confirmación de Golden Admission."""


class GoldenAdmissionConfirmationProvider(Protocol):
    """Puerto de la confirmación privilegiada de Golden Admission.

    No existe provider por defecto — y en particular ninguno que confirme.
    Todo provider debe devolver EXACTAMENTE un
    ``GoldenAdmissionConfirmationResult``: cualquier otro valor se trata como
    NO confirmado.
    """

    def request_confirmation(
        self,
        payload: GoldenAdmissionConfirmation,
    ) -> GoldenAdmissionConfirmationResult: ...


@dataclass(frozen=True, slots=True)
class GoldenAdmissionConfirmationReceipt:
    """Recibo inmutable de una confirmación CONFIRMADA."""

    payload: GoldenAdmissionConfirmation
    result: GoldenAdmissionConfirmationResult

    def __post_init__(self) -> None:
        if not isinstance(self.payload, GoldenAdmissionConfirmation):
            raise GoldenAdmissionConfirmationError("payload debe ser GoldenAdmissionConfirmation")
        if not isinstance(self.result, GoldenAdmissionConfirmationResult):
            raise GoldenAdmissionRejectedError(
                "result debe ser GoldenAdmissionConfirmationResult: cualquier otro valor es NO confirmado"
            )
        if self.result is not GoldenAdmissionConfirmationResult.CONFIRMED:
            raise GoldenAdmissionRejectedError(
                "La confirmación de Golden Admission no fue confirmada por el operador: REJECTED, cero TGR writes"
            )


def require_golden_admission_outcome(
    provider: GoldenAdmissionConfirmationProvider | None,
    payload: GoldenAdmissionConfirmation,
) -> GoldenAdmissionConfirmationReceipt:
    """Ejecuta la confirmación y exige CONFIRMED; sin provider es REJECTED.

    Los valores exóticos del provider (``None``, ``True``, ``"confirmed"``...)
    no son aceptados: sólo el miembro de enum EXACTO ``CONFIRMED`` produce
    recibo. Sin provider no hay diálogo que pueda confirmar — fail-closed.
    """
    if not isinstance(payload, GoldenAdmissionConfirmation):
        raise GoldenAdmissionConfirmationError("payload debe ser GoldenAdmissionConfirmation")
    if provider is None:
        raise GoldenAdmissionRejectedError(
            "Sin GoldenAdmissionConfirmationProvider no hay confirmación privilegiada: REJECTED"
        )
    result = provider.request_confirmation(payload)
    if not isinstance(result, GoldenAdmissionConfirmationResult):
        raise GoldenAdmissionRejectedError(
            f"El provider devolvió un valor no contractual ({type(result).__name__}): NO confirmado"
        )
    return GoldenAdmissionConfirmationReceipt(payload=payload, result=result)
