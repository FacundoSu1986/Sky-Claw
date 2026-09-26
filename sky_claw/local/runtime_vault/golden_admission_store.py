"""Registro protegido de operación de Golden Admission (ADR 0010 §11.4).

El registro es la memoria durable de UNA operación ``REGISTER_OR_REFRESH_TRUSTED_GOLDEN``
y el único lugar donde se conserva la autoridad que la fila del TGR no guarda
(``expected_runtime`` y ``critical_expectations``). No es un WAL, no habilita
recovery/replay del TGR ni añade estados al FSM de GP2 apply: es evidencia de
auditoría, retenida mientras su entrada ``after`` siga siendo la vigente.

Qué conserva, exactamente lo que pide §11.4:

- el ``GoldenAdmissionReceipt`` inmutable tal como fue emitido por el helper;
- los ``critical_expectations`` normalizados (incluida la lista vacía) para poder
  recomputar su digest;
- el resultado y los bindings de cada observación / RV-2 ejecutado;
- la ``source reference`` validada, cuando aplique;
- las entradas TGR ``before`` (``ABSENT`` o el valor previo) y ``after``
  (todos los campos), persistidas ANTES del replace y revalidadas; si esa
  revalidación falla, no se escribe el TGR;
- el resultado final, anexado después del replace.

Persistencia: directorio ``operations/<operation_id>/`` creado con semántica
single-winner (``CREATE_NEW``) y archivo ``golden_admission_record.json`` que
nace con SECURITY_DESCRIPTOR canónico y se reescribe sólo vía la primitiva
atómica única ``trusted_namespace.write_secured_file_atomically_at``.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sky_claw.local.runtime_vault.critical_expectations import critical_expectations_digest
from sky_claw.local.runtime_vault.golden_admission import (
    GoldenAdmissionClaimAlreadyExistsError,
    GoldenAdmissionError,
    GoldenAdmissionOutcome,
    GoldenAdmissionReceipt,
    GoldenAdmissionRejectionReason,
    GoldenAdmissionSource,
    GoldenAdmissionState,
    GoldenAdmissionStoreError,
    validate_operation_id,
)
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    RuntimeIdentity,
    RuntimeVaultError,
    TreeDigest,
)
from sky_claw.local.runtime_vault.physical_root import PhysicalRootIdentity
from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenEntry,
    trusted_golden_entry_from_dict,
    trusted_golden_entry_to_dict,
)

#: Versión del schema del registro de operación.
ADMISSION_RECORD_SCHEMA_VERSION = "1.0"

#: Nombre de archivo del registro dentro de ``operations/<operation_id>/``.
GOLDEN_ADMISSION_RECORD_FILE = "golden_admission_record.json"

#: ``object_name`` de DACL usado al crear y verificar el archivo del registro.
GOLDEN_ADMISSION_RECORD_OBJECT = GOLDEN_ADMISSION_RECORD_FILE

#: Claves del objeto raíz del registro (esquema cerrado).
RECORD_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "operation_id",
        "receipt",
        "critical_expectations",
        "source_reference",
        "observations",
        "tgr_binding",
        "result",
    }
)

#: Campos exactos de un ``critical_expectations`` serializado.
CRITICAL_ENTRY_KEYS = frozenset({"rel_path", "expected_digest", "expected_size"})

#: Campos exactos de una observación serializada.
OBSERVATION_KEYS = frozenset(
    {
        "stage",
        "state",
        "physical_root",
        "observed_tree",
        "observed_runtime",
        "critical_expectations_digest",
        "message",
    }
)

#: Campos exactos del resultado serializado.
RESULT_KEYS = frozenset(
    {
        "outcome",
        "reason",
        "registered_at",
        "registered_by",
        "message",
    }
)

#: Campos exactos de la referencia de fuente serializada.
SOURCE_REFERENCE_KEYS = frozenset({"kind", "digest"})

#: Campos exactos del binding TGR serializado.
TGR_BINDING_KEYS = frozenset({"before", "after"})

#: Etapas de una observación registrada.
ADMISSION_PASSES = ("OBSERVE", "RV2")


__all__ = [
    "ADMISSION_PASSES",
    "ADMISSION_RECORD_SCHEMA_VERSION",
    "CRITICAL_ENTRY_KEYS",
    "GOLDEN_ADMISSION_RECORD_FILE",
    "GOLDEN_ADMISSION_RECORD_OBJECT",
    "OBSERVATION_KEYS",
    "RECORD_ROOT_KEYS",
    "RESULT_KEYS",
    "SOURCE_REFERENCE_KEYS",
    "TGR_BINDING_KEYS",
    "GoldenAdmissionObservationRecord",
    "GoldenAdmissionRecord",
    "GoldenAdmissionResultRecord",
    "GoldenAdmissionSourceReference",
    "GoldenAdmissionTgrBinding",
    "append_observation",
    "append_result",
    "bind_tgr_replacement",
    "create_admission_record",
    "derive_admission_record_dir",
    "ESTADOS_ADMITIDOS_POR_PASS",
    "derive_admission_record_path",
    "deserialize_admission_record",
    "load_admission_record",
    "revalidate_for_tgr_replace",
    "serialize_admission_record",
]


# ============================================================================
# Modelos del registro
# ============================================================================


def _validate_pass(stage: object) -> str:
    if not isinstance(stage, str) or stage not in ADMISSION_PASSES:
        raise GoldenAdmissionStoreError(f"stage '{stage}' no pertenece a {ADMISSION_PASSES}")
    return stage


def _validate_sha256_hex(value: object, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise GoldenAdmissionStoreError(f"{field_name} debe ser SHA-256 hex en minúsculas")
    return value


def _validate_state(value: object) -> str:
    if isinstance(value, GoldenAdmissionState):
        return str(value)
    if not isinstance(value, str):
        raise GoldenAdmissionStoreError(f"observation.state '{value}' no pertenece al conjunto cerrado")
    try:
        return str(GoldenAdmissionState(value))
    except ValueError as exc:
        raise GoldenAdmissionStoreError(f"observation.state '{value}' no pertenece al conjunto cerrado") from exc


#: Estados admisibles por pass: ``VERIFIED`` sólo puede nacer de un RV-2, nunca
#: de la primera observación (ADR 0010 §11.4: ``OBSERVED`` → ``VERIFIED``).
ESTADOS_ADMITIDOS_POR_PASS: dict[str, frozenset[str]] = {
    "OBSERVE": frozenset({str(GoldenAdmissionState.OBSERVED)}),
    "RV2": frozenset({str(GoldenAdmissionState.OBSERVED), str(GoldenAdmissionState.VERIFIED)}),
}


@dataclass(frozen=True, slots=True)
class GoldenAdmissionSourceReference:
    """Referencia validada de la evidencia/provenance bundle, cuando aplique (§11.4)."""

    kind: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise GoldenAdmissionStoreError("source_reference.kind debe ser un string no vacío")
        _validate_sha256_hex(self.digest, "source_reference.digest")


@dataclass(frozen=True, slots=True)
class GoldenAdmissionObservationRecord:
    """Bindings de UN pass de observación / RV-2 ejecutado en la operación (§11.4).

    ``critical_expectations_digest`` es el digest de la lista VIGENTE en ese
    pass, tal como se usó para el verdicto: se guarda explícito porque es la
    expectativa con la que se observó, no la del receipt.
    """

    stage: str
    state: str
    physical_root: PhysicalRootIdentity
    observed_tree: TreeDigest
    observed_runtime: FreshRuntimeObservation
    critical_expectations_digest: str
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", _validate_pass(self.stage))
        object.__setattr__(self, "state", _validate_state(self.state))
        if self.state not in ESTADOS_ADMITIDOS_POR_PASS[self.stage]:
            permitidos = ", ".join(sorted(ESTADOS_ADMITIDOS_POR_PASS[self.stage]))
            raise GoldenAdmissionStoreError(
                f"observation.state '{self.state}' no es admisible en el pass '{self.stage}' (permitidos: {permitidos})"
            )
        _validate_sha256_hex(self.critical_expectations_digest, "observation.critical_expectations_digest")
        if not isinstance(self.physical_root, PhysicalRootIdentity):
            raise GoldenAdmissionStoreError("observation.physical_root debe ser PhysicalRootIdentity")
        if not isinstance(self.observed_tree, TreeDigest):
            raise GoldenAdmissionStoreError("observation.observed_tree debe ser TreeDigest")
        if not isinstance(self.observed_runtime, FreshRuntimeObservation):
            raise GoldenAdmissionStoreError("observation.observed_runtime debe ser FreshRuntimeObservation")
        if not isinstance(self.message, str):
            raise GoldenAdmissionStoreError("observation.message debe ser string")


@dataclass(frozen=True, slots=True)
class GoldenAdmissionTgrBinding:
    """Entradas TGR ``before``/``after`` de la operación (§11.4).

    ``before is None`` significa ``ABSENT`` — no hay entrada previa para esa
    identidad física. ``after`` es siempre la entrada completa (todos los
    campos), persistida como ``intended after`` antes del replace.
    """

    before: TrustedGoldenEntry | None
    after: TrustedGoldenEntry

    def __post_init__(self) -> None:
        if self.before is not None and not isinstance(self.before, TrustedGoldenEntry):
            raise GoldenAdmissionStoreError("tgr_binding.before debe ser TrustedGoldenEntry o None (ABSENT)")
        if not isinstance(self.after, TrustedGoldenEntry):
            raise GoldenAdmissionStoreError("tgr_binding.after debe ser TrustedGoldenEntry")


@dataclass(frozen=True, slots=True)
class GoldenAdmissionResultRecord:
    """Resultado terminal anexado al registro después del intento de commit."""

    outcome: GoldenAdmissionOutcome
    reason: GoldenAdmissionRejectionReason | None = None
    registered_at: str | None = None
    registered_by: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, GoldenAdmissionOutcome):
            try:
                object.__setattr__(self, "outcome", GoldenAdmissionOutcome(self.outcome))
            except (ValueError, TypeError) as exc:
                raise GoldenAdmissionStoreError(f"result.outcome '{self.outcome}' inválido") from exc
        if self.reason is not None and not isinstance(self.reason, GoldenAdmissionRejectionReason):
            try:
                object.__setattr__(self, "reason", GoldenAdmissionRejectionReason(self.reason))
            except (ValueError, TypeError) as exc:
                raise GoldenAdmissionStoreError(f"result.reason '{self.reason}' inválida") from exc
        if self.outcome is GoldenAdmissionOutcome.REGISTERED:
            if self.reason is not None:
                raise GoldenAdmissionStoreError("REGISTERED no puede llevar reason de rechazo")
            if not isinstance(self.registered_at, str) or not self.registered_at:
                raise GoldenAdmissionStoreError("REGISTERED exige registered_at")
            if not isinstance(self.registered_by, str) or not self.registered_by:
                raise GoldenAdmissionStoreError("REGISTERED exige registered_by")
        elif self.registered_at is not None or self.registered_by is not None:
            raise GoldenAdmissionStoreError("Sólo REGISTERED lleva registered_at/registered_by")
        if self.outcome is GoldenAdmissionOutcome.REJECTED and self.reason is None:
            raise GoldenAdmissionStoreError("REJECTED exige reason tipado")
        if not isinstance(self.message, str):
            raise GoldenAdmissionStoreError("result.message debe ser string")


@dataclass(frozen=True, slots=True)
class GoldenAdmissionRecord:
    """Registro protegido de operación, en memoria (esquema cerrado, inmutable)."""

    operation_id: str
    receipt: GoldenAdmissionReceipt
    critical_expectations: tuple[CriticalFileExpectation, ...]
    source_reference: GoldenAdmissionSourceReference | None = None
    observations: tuple[GoldenAdmissionObservationRecord, ...] = ()
    tgr_binding: GoldenAdmissionTgrBinding | None = None
    result: GoldenAdmissionResultRecord | None = None
    schema_version: str = ADMISSION_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ADMISSION_RECORD_SCHEMA_VERSION:
            raise GoldenAdmissionStoreError(
                f"schema_version '{self.schema_version}' no soportado (esperado '{ADMISSION_RECORD_SCHEMA_VERSION}')"
            )
        object.__setattr__(self, "operation_id", validate_operation_id(self.operation_id))
        if not isinstance(self.receipt, GoldenAdmissionReceipt):
            raise GoldenAdmissionStoreError("record.receipt debe ser GoldenAdmissionReceipt")
        if self.receipt.operation_id != self.operation_id:
            raise GoldenAdmissionStoreError(
                f"El receipt pertenece a otra operación: {self.receipt.operation_id} != {self.operation_id}"
            )
        if not isinstance(self.critical_expectations, tuple):
            raise GoldenAdmissionStoreError("record.critical_expectations debe ser tuple")
        for item in self.critical_expectations:
            if not isinstance(item, CriticalFileExpectation):
                raise GoldenAdmissionStoreError("Cada critical_expectation debe ser CriticalFileExpectation")
        if not isinstance(self.observations, tuple):
            raise GoldenAdmissionStoreError("record.observations debe ser tuple")
        for obs in self.observations:
            if not isinstance(obs, GoldenAdmissionObservationRecord):
                raise GoldenAdmissionStoreError("Cada observación debe ser GoldenAdmissionObservationRecord")

        # §11.4: la source reference sólo puede existir en provenance; en TOFU
        # el receipt lleva source_provenance_digest = null y el registro tampoco
        # puede declarar una fuente (TOFU no se convierte en provenance por
        # rellenar un campo).
        if self.source_reference is not None:
            if not isinstance(self.source_reference, GoldenAdmissionSourceReference):
                raise GoldenAdmissionStoreError("record.source_reference inválida")
            if self.receipt.admission_source is not GoldenAdmissionSource.INDEPENDENT_PROVENANCE:
                raise GoldenAdmissionStoreError(
                    "source_reference sólo existe con admission_source=INDEPENDENT_PROVENANCE"
                )
            if self.receipt.source_provenance_digest != self.source_reference.digest:
                raise GoldenAdmissionStoreError(
                    "source_reference.digest no coincide con receipt.source_provenance_digest"
                )

        # Un resultado REGISTERED exige el binding que lo hace auditable.
        if (
            self.result is not None
            and self.result.outcome is GoldenAdmissionOutcome.REGISTERED
            and self.tgr_binding is None
        ):
            raise GoldenAdmissionStoreError("REGISTERED exige tgr_binding before/after")

    @property
    def critical_expectations_digest(self) -> str:
        """Digest de la lista serializada (siempre presente, también ``()``)."""
        return critical_expectations_digest(self.critical_expectations)

    @property
    def tgr_before_is_absent(self) -> bool:
        """True sólo si el binding ya fue registrado y no había entrada previa."""
        return self.tgr_binding is not None and self.tgr_binding.before is None


# ============================================================================
# Serialización canónica
# ============================================================================


def _tree_digest_to_dict(value: TreeDigest) -> dict[str, Any]:
    return {"bytes": value.bytes, "digest": value.digest, "files": value.files}


def _tree_digest_from_dict(raw: object, field_name: str) -> TreeDigest:
    if not isinstance(raw, dict):
        raise GoldenAdmissionStoreError(f"{field_name} debe ser un objeto")
    if set(raw.keys()) != {"bytes", "digest", "files"}:
        raise GoldenAdmissionStoreError(f"{field_name} tiene claves inesperadas: {set(raw.keys())}")
    return TreeDigest(digest=str(raw["digest"]), files=raw["files"], bytes=raw["bytes"])


def _runtime_identity_to_dict(value: RuntimeIdentity) -> dict[str, Any]:
    return {"game_key": value.game_key, "game_version": value.game_version}


def _runtime_identity_from_dict(raw: object, field_name: str) -> RuntimeIdentity:
    if not isinstance(raw, dict) or set(raw.keys()) != {"game_key", "game_version"}:
        raise GoldenAdmissionStoreError(f"{field_name} inválido")
    return RuntimeIdentity(game_key=str(raw["game_key"]), game_version=str(raw["game_version"]))


def _fresh_runtime_to_dict(value: FreshRuntimeObservation) -> dict[str, Any]:
    return {
        "game_key": value.game_key,
        "game_version": value.game_version,
        "observed_at_ns": value.observed_at_ns,
        "observed_exe_path": value.observed_exe_path,
    }


def _fresh_runtime_from_dict(raw: object, field_name: str) -> FreshRuntimeObservation:
    if not isinstance(raw, dict):
        raise GoldenAdmissionStoreError(f"{field_name} debe ser un objeto")
    if set(raw.keys()) != {"game_key", "game_version", "observed_at_ns", "observed_exe_path"}:
        raise GoldenAdmissionStoreError(f"{field_name} tiene claves inesperadas: {set(raw.keys())}")
    return FreshRuntimeObservation(
        game_key=str(raw["game_key"]),
        game_version=str(raw["game_version"]),
        observed_exe_path=str(raw["observed_exe_path"]),
        observed_at_ns=raw["observed_at_ns"],
    )


def _physical_root_to_dict(value: PhysicalRootIdentity) -> dict[str, Any]:
    return {
        "canonical_root": value.canonical_root,
        "root_file_id": value.root_file_id,
        "volume_serial_number": value.volume_serial_number,
    }


def _physical_root_from_dict(raw: object, field_name: str) -> PhysicalRootIdentity:
    if not isinstance(raw, dict):
        raise GoldenAdmissionStoreError(f"{field_name} debe ser un objeto")
    if set(raw.keys()) != {"canonical_root", "root_file_id", "volume_serial_number"}:
        raise GoldenAdmissionStoreError(f"{field_name} tiene claves inesperadas: {set(raw.keys())}")
    return PhysicalRootIdentity(
        canonical_root=str(raw["canonical_root"]),
        volume_serial_number=raw["volume_serial_number"],
        root_file_id=raw["root_file_id"],
    )


def _receipt_to_dict(receipt: GoldenAdmissionReceipt) -> dict[str, Any]:
    return {
        "admitted_at": receipt.admitted_at,
        "admission_source": str(receipt.admission_source),
        "canonical_root": receipt.canonical_root,
        "critical_expectations_digest": receipt.critical_expectations_digest,
        "expected_runtime": _runtime_identity_to_dict(receipt.expected_runtime),
        "nonce": receipt.nonce,
        "operation_id": receipt.operation_id,
        "operator_sid": receipt.operator_sid,
        "policy_version": receipt.policy_version,
        "root_file_id": receipt.root_file_id,
        "source_provenance_digest": receipt.source_provenance_digest,
        "tree_digest": _tree_digest_to_dict(receipt.tree_digest),
        "volume_serial_number": receipt.volume_serial_number,
    }


_RECEIPT_KEYS = frozenset(
    {
        "admitted_at",
        "admission_source",
        "canonical_root",
        "critical_expectations_digest",
        "expected_runtime",
        "nonce",
        "operation_id",
        "operator_sid",
        "policy_version",
        "root_file_id",
        "source_provenance_digest",
        "tree_digest",
        "volume_serial_number",
    }
)


def _receipt_from_dict(raw: object) -> GoldenAdmissionReceipt:
    if not isinstance(raw, dict) or set(raw.keys()) != _RECEIPT_KEYS:
        claves = set(raw.keys()) if isinstance(raw, dict) else type(raw).__name__
        raise GoldenAdmissionStoreError(f"receipt con claves inesperadas: {claves}")
    return GoldenAdmissionReceipt(
        operation_id=raw["operation_id"],
        canonical_root=raw["canonical_root"],
        volume_serial_number=raw["volume_serial_number"],
        root_file_id=raw["root_file_id"],
        tree_digest=_tree_digest_from_dict(raw["tree_digest"], "receipt.tree_digest"),
        expected_runtime=_runtime_identity_from_dict(raw["expected_runtime"], "receipt.expected_runtime"),
        policy_version=raw["policy_version"],
        admission_source=raw["admission_source"],
        operator_sid=raw["operator_sid"],
        admitted_at=raw["admitted_at"],
        critical_expectations_digest=raw["critical_expectations_digest"],
        source_provenance_digest=raw["source_provenance_digest"],
        nonce=raw["nonce"],
    )


def _critical_to_dict(item: CriticalFileExpectation) -> dict[str, Any]:
    return {
        "expected_digest": item.expected_digest,
        "expected_size": item.expected_size,
        "rel_path": item.rel_path,
    }


def _critical_from_dict(raw: object) -> CriticalFileExpectation:
    if not isinstance(raw, dict) or set(raw.keys()) != CRITICAL_ENTRY_KEYS:
        raise GoldenAdmissionStoreError("critical_expectation con claves inesperadas")
    return CriticalFileExpectation(
        rel_path=str(raw["rel_path"]),
        expected_digest=str(raw["expected_digest"]),
        expected_size=raw["expected_size"],
    )


def _observation_to_dict(obs: GoldenAdmissionObservationRecord) -> dict[str, Any]:
    return {
        "critical_expectations_digest": obs.critical_expectations_digest,
        "message": obs.message,
        "observed_runtime": _fresh_runtime_to_dict(obs.observed_runtime),
        "observed_tree": _tree_digest_to_dict(obs.observed_tree),
        "physical_root": _physical_root_to_dict(obs.physical_root),
        "stage": obs.stage,
        "state": obs.state,
    }


def _observation_from_dict(raw: object) -> GoldenAdmissionObservationRecord:
    if not isinstance(raw, dict) or set(raw.keys()) != OBSERVATION_KEYS:
        raise GoldenAdmissionStoreError("observation con claves inesperadas")
    return GoldenAdmissionObservationRecord(
        stage=raw["stage"],
        state=raw["state"],
        physical_root=_physical_root_from_dict(raw["physical_root"], "observation.physical_root"),
        observed_tree=_tree_digest_from_dict(raw["observed_tree"], "observation.observed_tree"),
        observed_runtime=_fresh_runtime_from_dict(raw["observed_runtime"], "observation.observed_runtime"),
        critical_expectations_digest=raw["critical_expectations_digest"],
        message=raw["message"],
    )


def _result_to_dict(result: GoldenAdmissionResultRecord) -> dict[str, Any]:
    return {
        "message": result.message,
        "outcome": str(result.outcome),
        "reason": None if result.reason is None else str(result.reason),
        "registered_at": result.registered_at,
        "registered_by": result.registered_by,
    }


def _result_from_dict(raw: object) -> GoldenAdmissionResultRecord:
    if not isinstance(raw, dict) or set(raw.keys()) != RESULT_KEYS:
        raise GoldenAdmissionStoreError("result con claves inesperadas")
    reason = raw["reason"]
    return GoldenAdmissionResultRecord(
        outcome=raw["outcome"],
        reason=None if reason is None else reason,
        registered_at=raw["registered_at"],
        registered_by=raw["registered_by"],
        message=raw["message"],
    )


def _source_reference_to_dict(ref: GoldenAdmissionSourceReference) -> dict[str, Any]:
    return {"digest": ref.digest, "kind": ref.kind}


def _source_reference_from_dict(raw: object) -> GoldenAdmissionSourceReference:
    if not isinstance(raw, dict) or set(raw.keys()) != SOURCE_REFERENCE_KEYS:
        raise GoldenAdmissionStoreError("source_reference con claves inesperadas")
    return GoldenAdmissionSourceReference(kind=str(raw["kind"]), digest=str(raw["digest"]))


def _binding_to_dict(binding: GoldenAdmissionTgrBinding) -> dict[str, Any]:
    return {
        "after": trusted_golden_entry_to_dict(binding.after),
        "before": None if binding.before is None else trusted_golden_entry_to_dict(binding.before),
    }


def _binding_from_dict(raw: object) -> GoldenAdmissionTgrBinding:
    if not isinstance(raw, dict) or set(raw.keys()) != TGR_BINDING_KEYS:
        raise GoldenAdmissionStoreError("tgr_binding con claves inesperadas")
    before = raw["before"]
    return GoldenAdmissionTgrBinding(
        before=None if before is None else trusted_golden_entry_from_dict(before),
        after=trusted_golden_entry_from_dict(raw["after"]),
    )


def serialize_admission_record(record: GoldenAdmissionRecord) -> bytes:
    """Serializa el registro a bytes canónicos UTF-8 deterministas (sin newline final)."""
    data: dict[str, Any] = {
        "critical_expectations": [_critical_to_dict(i) for i in record.critical_expectations],
        "operation_id": record.operation_id,
        "observations": [_observation_to_dict(o) for o in record.observations],
        "receipt": _receipt_to_dict(record.receipt),
        "result": None if record.result is None else _result_to_dict(record.result),
        "schema_version": record.schema_version,
        "source_reference": (
            None if record.source_reference is None else _source_reference_to_dict(record.source_reference)
        ),
        "tgr_binding": None if record.tgr_binding is None else _binding_to_dict(record.tgr_binding),
    }
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def deserialize_admission_record(raw: bytes | str) -> GoldenAdmissionRecord:
    """Deserializa y valida exhaustivamente el registro (Fail-Closed, esquema cerrado)."""
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GoldenAdmissionStoreError("JSON inválido: decodificación UTF-8 falló") from exc
    else:
        raise GoldenAdmissionStoreError("Los datos del registro deben ser bytes o str")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GoldenAdmissionStoreError(f"JSON inválido: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GoldenAdmissionStoreError("La raíz del registro debe ser un diccionario")

    claves = set(parsed.keys())
    if claves != RECORD_ROOT_KEYS:
        desconocidas = claves - RECORD_ROOT_KEYS
        faltantes = RECORD_ROOT_KEYS - claves
        if desconocidas:
            raise GoldenAdmissionStoreError(f"Clave desconocida en el registro: {desconocidas}")
        raise GoldenAdmissionStoreError(f"Clave obligatoria ausente en el registro: {faltantes}")

    observaciones = parsed["observations"]
    if not isinstance(observaciones, list):
        raise GoldenAdmissionStoreError("El campo 'observations' debe ser una lista")
    criticas = parsed["critical_expectations"]
    if not isinstance(criticas, list):
        raise GoldenAdmissionStoreError("El campo 'critical_expectations' debe ser una lista")

    source_reference = parsed["source_reference"]
    tgr_binding = parsed["tgr_binding"]
    result = parsed["result"]

    try:
        return GoldenAdmissionRecord(
            operation_id=parsed["operation_id"],
            receipt=_receipt_from_dict(parsed["receipt"]),
            critical_expectations=tuple(_critical_from_dict(i) for i in criticas),
            source_reference=None if source_reference is None else _source_reference_from_dict(source_reference),
            observations=tuple(_observation_from_dict(o) for o in observaciones),
            tgr_binding=None if tgr_binding is None else _binding_from_dict(tgr_binding),
            result=None if result is None else _result_from_dict(result),
            schema_version=parsed["schema_version"],
        )
    except GoldenAdmissionStoreError:
        raise
    except (GoldenAdmissionError, RuntimeVaultError, ValueError, TypeError, KeyError) as exc:
        raise GoldenAdmissionStoreError(f"Registro inválido: {exc}") from exc


# ============================================================================
# Persistencia en el namespace protegido (Win32)
# ============================================================================


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise GoldenAdmissionStoreError(
            "El registro protegido de Golden Admission sólo existe con las garantías Win32 del namespace"
        )


def derive_admission_record_dir(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """``%ProgramData%\\Sky-Claw\\runtime_vault\\operations\\<operation_id>``."""
    from sky_claw.local.runtime_vault.trusted_registry_lock import _resolve_runtime_vault_dir

    op = validate_operation_id(operation_id)
    return pathlib.Path(_resolve_runtime_vault_dir(programdata_resolver=programdata_resolver) / "operations" / op)


def derive_admission_record_path(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """Path canónico del archivo del registro de la operación."""
    return derive_admission_record_dir(operation_id, programdata_resolver=programdata_resolver) / (
        GOLDEN_ADMISSION_RECORD_FILE
    )


def _revalidar_registro(raw: bytes) -> None:
    """Callback de revalidación para la primitiva atómica: sólo valida, no devuelve."""
    deserialize_admission_record(raw)


def _persist(record: GoldenAdmissionRecord, *, programdata_resolver: Callable[[], object] | None = None) -> None:
    from sky_claw.local.runtime_vault.trusted_namespace import write_secured_file_atomically_at

    path = derive_admission_record_path(record.operation_id, programdata_resolver=programdata_resolver)
    payload = serialize_admission_record(record)
    try:
        write_secured_file_atomically_at(
            path,
            payload,
            GOLDEN_ADMISSION_RECORD_OBJECT,
            validate=_revalidar_registro,
            error_factory=GoldenAdmissionStoreError,
            parent_error_message=f"El directorio de operación '{path.parent}' no existe o no es confiable",
        )
    except GoldenAdmissionStoreError:
        raise
    except Exception as exc:
        raise GoldenAdmissionStoreError(f"No se pudo persistir el registro '{path}': {exc}") from exc


def load_admission_record(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> GoldenAdmissionRecord:
    """Lee y valida el registro desde disco (Fail-Closed)."""
    _ensure_windows()
    try:
        path = derive_admission_record_path(operation_id, programdata_resolver=programdata_resolver)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise GoldenAdmissionStoreError(f"No se pudo leer el registro de la operación '{path}': {exc}") from exc
        record = deserialize_admission_record(raw)
        if record.operation_id != validate_operation_id(operation_id):
            raise GoldenAdmissionStoreError("El registro en disco pertenece a otra operación")
        return record
    except GoldenAdmissionStoreError:
        raise
    except Exception as exc:
        raise GoldenAdmissionStoreError(
            f"No se pudo cargar el registro de la operación '{operation_id}': {exc}"
        ) from exc


def create_admission_record(
    *,
    receipt: GoldenAdmissionReceipt,
    critical_expectations: tuple[CriticalFileExpectation, ...],
    source_reference: GoldenAdmissionSourceReference | None = None,
    observations: tuple[GoldenAdmissionObservationRecord, ...] = (),
    programdata_resolver: Callable[[], object] | None = None,
) -> GoldenAdmissionRecord:
    """Crea ``operations/<operation_id>/`` (single-winner) y su registro inicial.

    La creación del directorio ES la claim one-use de la operación: un replay
    del mismo ``operation_id`` choca con ``CREATE_NEW`` y queda ``REJECTED``
    sin tocar el registro existente ni el TGR. Ese choque se tipa como
    ``GoldenAdmissionClaimAlreadyExistsError`` (subtipo de
    ``GoldenAdmissionStoreError``) porque el registro pertenece al ganador: el
    perdedor no debe leerlo ni escribir en él. ``observations`` permite sembrar
    el registro con la evidencia del pass ``OBSERVE`` que precede a la emisión
    del receipt en el flujo TOFU.
    """
    _ensure_windows()
    from sky_claw.local.runtime_vault.trusted_namespace import (
        NamespaceAlreadyExistsError,
        create_secure_directory_exclusive,
    )

    try:
        record = GoldenAdmissionRecord(
            operation_id=receipt.operation_id,
            receipt=receipt,
            critical_expectations=tuple(critical_expectations),
            source_reference=source_reference,
            observations=tuple(observations),
        )

        record_dir = derive_admission_record_dir(receipt.operation_id, programdata_resolver=programdata_resolver)
        try:
            create_secure_directory_exclusive(record_dir, "operations")
        except NamespaceAlreadyExistsError as exc:
            raise GoldenAdmissionClaimAlreadyExistsError(
                f"La operación '{receipt.operation_id}' ya tiene registro: el claim es one-use"
            ) from exc
        except GoldenAdmissionStoreError:
            raise
        except Exception as exc:
            raise GoldenAdmissionStoreError(f"No se pudo crear el directorio de operación: {exc}") from exc

        _persist(record, programdata_resolver=programdata_resolver)
        return record
    except GoldenAdmissionStoreError:
        raise
    except Exception as exc:
        raise GoldenAdmissionStoreError(f"No se pudo crear el registro de admisión: {exc}") from exc


def _mutar(
    operation_id: str,
    mutador: Any,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> GoldenAdmissionRecord:
    try:
        actual = load_admission_record(operation_id, programdata_resolver=programdata_resolver)
        nuevo = mutador(actual)
        if not isinstance(nuevo, GoldenAdmissionRecord):
            raise GoldenAdmissionStoreError("El mutador no devolvió un GoldenAdmissionRecord")
        if nuevo.operation_id != actual.operation_id:
            raise GoldenAdmissionStoreError("El mutador no puede cambiar el operation_id del registro")
        _persist(nuevo, programdata_resolver=programdata_resolver)
        return nuevo
    except GoldenAdmissionStoreError:
        raise
    except Exception as exc:
        raise GoldenAdmissionStoreError(f"Fallo al mutar el registro de la operación '{operation_id}': {exc}") from exc


def append_observation(
    operation_id: str,
    observation: GoldenAdmissionObservationRecord,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> GoldenAdmissionRecord:
    """Anexa los bindings de un pass ``OBSERVE``/``RV2`` al registro."""
    if not isinstance(observation, GoldenAdmissionObservationRecord):
        raise GoldenAdmissionStoreError("observation debe ser GoldenAdmissionObservationRecord")

    def mutador(actual: GoldenAdmissionRecord) -> GoldenAdmissionRecord:
        return GoldenAdmissionRecord(
            operation_id=actual.operation_id,
            receipt=actual.receipt,
            critical_expectations=actual.critical_expectations,
            source_reference=actual.source_reference,
            observations=(*actual.observations, observation),
            tgr_binding=actual.tgr_binding,
            result=actual.result,
            schema_version=actual.schema_version,
        )

    return _mutar(operation_id, mutador, programdata_resolver=programdata_resolver)


def bind_tgr_replacement(
    operation_id: str,
    *,
    before: TrustedGoldenEntry | None,
    after: TrustedGoldenEntry,
    programdata_resolver: Callable[[], object] | None = None,
) -> GoldenAdmissionRecord:
    """Persiste ``before`` (``None`` = ``ABSENT``) y ``intended after`` ANTES del replace TGR."""
    try:
        binding = GoldenAdmissionTgrBinding(before=before, after=after)
    except GoldenAdmissionStoreError:
        raise
    except Exception as exc:
        raise GoldenAdmissionStoreError(f"Binding TGR inválido: {exc}") from exc

    def mutador(actual: GoldenAdmissionRecord) -> GoldenAdmissionRecord:
        if actual.tgr_binding is not None:
            raise GoldenAdmissionStoreError("El binding TGR ya fue registrado: no se reescribe")
        if actual.result is not None:
            raise GoldenAdmissionStoreError("No se puede registrar un binding con resultado ya anexado")
        return GoldenAdmissionRecord(
            operation_id=actual.operation_id,
            receipt=actual.receipt,
            critical_expectations=actual.critical_expectations,
            source_reference=actual.source_reference,
            observations=actual.observations,
            tgr_binding=binding,
            result=None,
            schema_version=actual.schema_version,
        )

    return _mutar(operation_id, mutador, programdata_resolver=programdata_resolver)


def append_result(
    operation_id: str,
    result: GoldenAdmissionResultRecord,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> GoldenAdmissionRecord:
    """Anexa el desenlace terminal (``REGISTERED`` / ``REJECTED`` / desconocido)."""
    if not isinstance(result, GoldenAdmissionResultRecord):
        raise GoldenAdmissionStoreError("result debe ser GoldenAdmissionResultRecord")

    def mutador(actual: GoldenAdmissionRecord) -> GoldenAdmissionRecord:
        if actual.result is not None:
            raise GoldenAdmissionStoreError("El resultado ya fue anexado: es un único desenlace terminal")
        return GoldenAdmissionRecord(
            operation_id=actual.operation_id,
            receipt=actual.receipt,
            critical_expectations=actual.critical_expectations,
            source_reference=actual.source_reference,
            observations=actual.observations,
            tgr_binding=actual.tgr_binding,
            result=result,
            schema_version=actual.schema_version,
        )

    return _mutar(operation_id, mutador, programdata_resolver=programdata_resolver)


def revalidate_for_tgr_replace(
    operation_id: str,
    *,
    expected: GoldenAdmissionRecord,
    programdata_resolver: Callable[[], object] | None = None,
) -> GoldenAdmissionRecord:
    """Relee el registro de disco y lo exige idéntico a ``expected`` antes del replace TGR.

    §11.4: "el helper persiste y revalida este registro protegido con ``before``
    e ``intended after`` antes del replace TGR; si no puede hacerlo, no escribe
    el TGR". Cualquier divergencia — receipt, expectativas, binding o
    source reference — es ``GoldenAdmissionStoreError`` y corta el commit.
    """
    try:
        disco = load_admission_record(operation_id, programdata_resolver=programdata_resolver)
        if disco != expected:
            diferencias = _diferencias(expected, disco)
            raise GoldenAdmissionStoreError(
                f"El registro en disco divergió del esperado antes del replace TGR: {diferencias}"
            )
        if disco.tgr_binding is None:
            raise GoldenAdmissionStoreError("El registro no tiene before/intended after: no se puede escribir el TGR")
        if disco.critical_expectations_digest != expected.critical_expectations_digest:
            raise GoldenAdmissionStoreError("El digest de critical_expectations divergió antes del replace TGR")
        return disco
    except GoldenAdmissionStoreError:
        raise
    except Exception as exc:
        raise GoldenAdmissionStoreError(
            f"Fallo al revalidar el registro para el replace TGR de '{operation_id}': {exc}"
        ) from exc


def _diferencias(esperado: GoldenAdmissionRecord, observado: GoldenAdmissionRecord) -> str:
    campos = []
    for nombre in (
        "receipt",
        "critical_expectations",
        "source_reference",
        "observations",
        "tgr_binding",
        "result",
    ):
        if getattr(esperado, nombre) != getattr(observado, nombre):
            campos.append(nombre)
    return ",".join(campos) or "schema_version"
