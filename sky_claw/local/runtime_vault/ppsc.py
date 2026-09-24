"""Privileged Plan-Specific Confirmation (PPSC) — foundation de datos (GP2-S3b-1, componente F).

Congela el contrato de datos de ADR 0010 §11.0(2)/§12.2 paso 4: el diálogo
privilegiado del helper debe exhibir EXACTAMENTE siete valores normativos y
obtener consentimiento explícito del operador antes de que exista cualquier
binding autorizado:

```text
operation_id, canonical_root, VolumeSerialNumber, root_file_id,
TreeDigest, node_count, policy_version
```

Reglas no negociables:

- La confirmación produce SÓLO ``CONFIRMED`` o ``REJECTED``; no existen defaults.
- NUNCA ``timeout = confirmed``, NUNCA ``dialog close = confirmed``, NUNCA
  ``UAC accepted = confirmed`` (UAC != autorización, ADR 0010 §11.0/§13.1).
- Un resultado no confirmado produce ``PlanAuthorizationError``
  (``REFUSE_TO_PLAN``, terminal no mutador, antes del GoldenMutationLock,
  ADR 0010 §12.2/§19.2).

Este slice NO diseña ni inventa la UI productiva (no existe capa de frontend
segura todavía): entrega el modelo inmutable, el puerto
``PrivilegedPlanConfirmationProvider`` y los tests del contrato. No se incluye
ningún provider por defecto, y en particular ninguno que confirme.
"""

from __future__ import annotations

import string
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sky_claw.local.runtime_vault.models import RuntimeVaultError, TreeDigest
from sky_claw.local.runtime_vault.privileged_boundary import PlanAuthorizationError
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedRegistrySchemaError,
    _normalize_windows_root,
)

_UTF8_LOWER_HEX = frozenset(string.hexdigits.lower())
_MAX_UINT64 = (1 << 64) - 1
_MAX_UINT128 = (1 << 128) - 1


class PrivilegedPlanConfirmationError(RuntimeVaultError):
    """Base de excepciones del contrato PPSC."""


class PrivilegedPlanConfirmationModelError(PrivilegedPlanConfirmationError):
    """El payload o el recibo PPSC violan el contrato de datos: fail-closed."""


#: Los SIETE campos normativos del diálogo PPSC, en el orden canónico del ADR.
#: Ancla de test: los fields del dataclass deben coincidir literalmente.
PPSC_NORMATIVE_FIELDS: tuple[str, ...] = (
    "operation_id",
    "canonical_root",
    "volume_serial_number",
    "root_file_id",
    "tree_digest",
    "node_count",
    "policy_version",
)


def _validate_canonical_operation_id(value: str) -> str:
    if not isinstance(value, str):
        raise PrivilegedPlanConfirmationModelError("operation_id debe ser string")
    normalized = value.strip()
    try:
        parsed = uuid.UUID(normalized)
    except ValueError as exc:
        raise PrivilegedPlanConfirmationModelError(f"operation_id '{value}' no es un UUID válido") from exc
    if normalized != str(parsed):
        raise PrivilegedPlanConfirmationModelError(
            f"operation_id '{value}' debe ser UUID canónico con guiones en minúsculas"
        )
    return normalized


def _validate_uint(value: int, field_name: str, *, max_value: int, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PrivilegedPlanConfirmationModelError(f"{field_name} debe ser un entero")
    lower = 0 if allow_zero else 1
    if not lower <= value <= max_value:
        raise PrivilegedPlanConfirmationModelError(f"{field_name} debe estar en el rango [{lower}, {max_value}]")
    return value


@dataclass(frozen=True, slots=True)
class PrivilegedPlanConfirmation:
    """Payload inmutable exhibido al operador en la sesión elevada (PPSC).

    Contiene EXACTAMENTE los siete campos normativos de ADR 0010 §11.0(2):
    ninguno menos (fail-closed por ausencia) y ninguno más (fail-closed por
    estructura de dataclass: no acepta campos extra de construcción).
    """

    operation_id: str
    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    tree_digest: TreeDigest
    node_count: int
    policy_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _validate_canonical_operation_id(self.operation_id))
        try:
            normalized_root = _normalize_windows_root(self.canonical_root)
        except TrustedRegistrySchemaError as exc:
            raise PrivilegedPlanConfirmationModelError(f"canonical_root inválido: {exc}") from exc
        object.__setattr__(self, "canonical_root", normalized_root)
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
        if not isinstance(self.tree_digest, TreeDigest):
            raise PrivilegedPlanConfirmationModelError("tree_digest debe ser TreeDigest")
        digest = self.tree_digest.digest.strip().lower()
        if len(digest) != 64 or any(ch not in _UTF8_LOWER_HEX for ch in digest):
            raise PrivilegedPlanConfirmationModelError("tree_digest.digest debe ser SHA-256 hex de 64 caracteres")
        object.__setattr__(
            self,
            "tree_digest",
            TreeDigest(digest=digest, files=self.tree_digest.files, bytes=self.tree_digest.bytes),
        )
        object.__setattr__(
            self,
            "node_count",
            _validate_uint(self.node_count, "node_count", max_value=_MAX_UINT64, allow_zero=False),
        )
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise PrivilegedPlanConfirmationModelError("policy_version debe ser un string no vacío")
        object.__setattr__(self, "policy_version", self.policy_version.strip())


class PrivilegedPlanConfirmationResult(StrEnum):
    """Únicos desenlaces admisibles de la confirmación: sin defaults, sin timeout-as-confirm."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class PrivilegedPlanConfirmationProvider(Protocol):
    """Puerto de la confirmación privilegiada.

    La implementación productiva (diálogo en sesión elevada) pertenece a la capa
    de frontend de un slice posterior; este slice NO la provee. Todo provider
    debe devolver EXACTAMENTE un ``PrivilegedPlanConfirmationResult``: cualquier
    otro valor (``None``, bool, string) se trata como NO confirmado.
    """

    def request_confirmation(
        self,
        payload: PrivilegedPlanConfirmation,
    ) -> PrivilegedPlanConfirmationResult: ...


@dataclass(frozen=True, slots=True)
class PrivilegedPlanConfirmationReceipt:
    """Recibo inmutable de una PPSC CONFIRMADA.

    Por construcción, un recibo con ``result != CONFIRMED`` no puede existir
    (``__post_init__`` lanza ``PlanAuthorizationError``): en el sistema tipado
    del repositorio, "no confirmado" nunca se representa como recibo.
    """

    payload: PrivilegedPlanConfirmation
    result: PrivilegedPlanConfirmationResult

    def __post_init__(self) -> None:
        if not isinstance(self.payload, PrivilegedPlanConfirmation):
            raise PrivilegedPlanConfirmationModelError("payload debe ser PrivilegedPlanConfirmation")
        if not isinstance(self.result, PrivilegedPlanConfirmationResult):
            raise PlanAuthorizationError(
                "result debe ser PrivilegedPlanConfirmationResult: cualquier otro valor es NO confirmado"
            )
        if self.result is not PrivilegedPlanConfirmationResult.CONFIRMED:
            raise PlanAuthorizationError(
                "La PPSC no fue confirmada por el operador: REFUSE_TO_PLAN (sin lock, sin authorized_plan)"
            )


def require_ppsc_outcome(
    provider: PrivilegedPlanConfirmationProvider,
    payload: PrivilegedPlanConfirmation,
) -> PrivilegedPlanConfirmationReceipt:
    """Ejecuta la PPSC y exige CONFIRMED o lanza ``PlanAuthorizationError``.

    Los valores exóticos del provider (``None``, ``True``, ``"confirmed"``...)
    no son aceptados: sólo el miembro de enum EXACTO ``CONFIRMED`` produce
    recibo. Un UAC concedido jamás puede sustituir este paso (mutante M-A1).
    """
    if not isinstance(payload, PrivilegedPlanConfirmation):
        raise PrivilegedPlanConfirmationModelError("payload debe ser PrivilegedPlanConfirmation")
    result = provider.request_confirmation(payload)
    if not isinstance(result, PrivilegedPlanConfirmationResult):
        raise PlanAuthorizationError(
            f"El provider PPSC devolvió un valor no contractual ({type(result).__name__}): NO confirmado -> REFUSE_TO_PLAN"
        )
    return PrivilegedPlanConfirmationReceipt(payload=payload, result=result)


__all__ = [
    "PPSC_NORMATIVE_FIELDS",
    "PrivilegedPlanConfirmation",
    "PrivilegedPlanConfirmationError",
    "PrivilegedPlanConfirmationModelError",
    "PrivilegedPlanConfirmationProvider",
    "PrivilegedPlanConfirmationReceipt",
    "PrivilegedPlanConfirmationResult",
    "require_ppsc_outcome",
]
