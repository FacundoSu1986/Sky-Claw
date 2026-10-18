"""Autoridad durable de aprobación de Frozen Runtime (P4-S4; ADR 0012).

Autoridad normativa (en orden de dependencia):

- **§34.6 / §35.2 / §38.5** — ``ApprovalScope`` de **14 campos** canónicos, sin
  alias, con ``approval_id``/``approval_seq`` preasignados ANTES del digest y
  ``approval_scope_digest`` = SHA-256 del JSON canónico de los otros 13.
- **§36.5 / §37.3** — ``state/approval.json`` como **caché reconciliable** con un
  único ``approval_revision`` bajo CAS para TODA escritura (emisión, consumo,
  revocación), y separación estricta emisor/consumidor.
- **§37.2 / §38.2** — protocolo **burn-first**: validar todo → escribir la
  evidencia terminal inmutable → recién entonces refrescar la caché. La
  validación precede al tombstone: un request stale o malformado NO terminaliza
  una aprobación.
- **§37.4** — FSM ``ISSUED | CONSUMED | REVOKED``, ``MAX_ISSUED_APPROVALS_PER_ROOT
  = 1`` y ``TIME_BASED_EXPIRATION_REQUIRED = NO`` (la invalidación es por cambio
  de ``source_activation`` o de evidencia target; una aprobación abandonada se
  REVOCA, no expira).
- **§38.2** — ``state/approvals/<seq>-<id>.json``: un tombstone inmutable por
  aprobación terminal, no-clobber, con detección R1–R4.
- **§40.2 / §40.3** — ``state/issuances/<seq>.json``: el ``CREATE`` del witness
  **es** la emisión. Es la autoridad que sobrevive a la restauración del ledger
  (sin él, el ABA ``R → R+1 → R`` del estado ``ISSUED`` era indetectable) y la que
  hace cumplir ``MAX_ISSUED = 1`` por colisión de nombre, sin lock adicional.
  Una sola semántica de colisión: ``FAIL_CLOSED``, sin re-propuesta y sin saltos
  de secuencia.
- **§45.10** — la protección anti-replay/restore vive en la **no-clobber** de los
  dos stores inmutables + el root lock + la validación previa; el CAS del ledger
  protege la **concurrencia de la caché**, no la durabilidad. ``N6`` (retroceso
  SOLO de la revisión con evidencia coherente) se repara hacia adelante; el
  retroceso o la incoherencia de la autoridad durable es ``FAIL_CLOSED``.
- **§37.3** — ``root_id`` es el root al que pertenece la evidencia, y se VERIFICA
  al leer: un ``state/`` trasplantado desde otro root es coherente consigo mismo
  —ledger, witnesses y tombstones concuerdan entre sí— y pasaría cualquier
  validación de forma. Sin el cross-check, la aprobación que el dueño dio para el
  root A autorizaría una transición en el root B. Es la misma comprobación que el
  journal hace sobre sus reservas de ``finalization_seq``, y la misma consecuencia
  declarada: mover el root invalida su evidencia y exige reconciliación del dueño.

**Superficie de este slice (P4_S4_SCOPE = DURABLE_APPROVAL_AUTHORITY).**

- NO activa nada: no hay bind, no hay promoción, no hay rollback, no se toca
  Effective Runtime ni el journal. ``PROMOTION``/``ROLLBACK`` efectivos siguen
  deshabilitados.
- La **emisión** productiva (``emitir_aprobacion``) falla cerrado con
  ``ApprovalHitolNoDisponibleError``: ``APPROVAL_AUTHORITY`` es la superficie
  HITL del producto y el producto no la tiene (§35.2). Ningún lock, booleano,
  mock ni mensaje del agente LLM se interpreta como consentimiento humano. El
  núcleo durable se ejercita con **fixtures explícitas**
  (``ConsentimientoAprobacion``) a través de ``_emitir_aprobacion_bajo_lock``,
  igual que el journal de P4-S3.
- El **consumo** y la **revocación** SÍ son entradas productivas: son operaciones
  de la maquinaria de transición, no autorización, y fallan cerrado contra la
  evidencia durable. Sin una emisión previa (hoy imposible en producción) no hay
  nada que consumir.

Persistencia: los dos stores inmutables se crean con ``open(path, "x")``
exclusivo + ``fsync`` (el modo que el propio §38.2 nombra como no-clobber); el
ledger se escribe con ``state.write_json_atomic``. Este módulo NO usa
``os.replace`` sobre un store inmutable, NO usa ``os.open`` y NO crea
directorios: el layout es del storage, bajo su admisión.

``POWER_LOSS_GUARANTEE = NOT_CLAIMED`` y ``DIRECTORY_FSYNC = NOT_CLAIMED`` en
Windows (no hay forma portable de fsync de un directorio): ``os.replace`` y el
``fsync`` del archivo son lo máximo que se afirma, igual que el resto del
subsistema. ``CROSS_FILE_ATOMICITY = NOT_CLAIMED`` (§37.2): el protocolo es
burn-first con matriz de crash, no una transacción distribuida.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import re
import time
import uuid
from collections.abc import Callable
from enum import StrEnum

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime import state as state_module
from sky_claw.local.frozen_runtime.errors import (
    ApprovalCarreraNormalError,
    ApprovalCasError,
    ApprovalConsentimientoError,
    ApprovalCorruptError,
    ApprovalHitolNoDisponibleError,
    ApprovalIdentityError,
    ApprovalLedgerRetrocedidoError,
    ApprovalMaxIssuedError,
    ApprovalMintingDefectError,
    ApprovalRootAjenoError,
    ApprovalSchemaError,
    ApprovalStoreNoInicializadoError,
    InvalidGenerationIdError,
    JournalCorruptError,
)
from sky_claw.local.frozen_runtime.generation_id import validar_generation_id
from sky_claw.local.frozen_runtime.independence import descripcion_de_enlace, exigir_namespace_escribible
from sky_claw.local.frozen_runtime.journal import (
    AprobacionConsumida,
    SourceActivation,
    digest_canonico,
    leer_json_de_control,
)
from sky_claw.local.frozen_runtime.root_lock import FrozenRuntimeRootLockHandle, participant_lock
from sky_claw.local.frozen_runtime.storage import (
    admitir_directorio_storage,
    approval_ledger_path,
    approvals_state_dir,
    issuances_state_dir,
)

APPROVAL_LEDGER_SCHEMA_VERSION = 1
ISSUANCE_WITNESS_SCHEMA_VERSION = 1
TERMINAL_TOMBSTONE_SCHEMA_VERSION = 1

#: Censo normativo del ``ApprovalScope`` (§38.5). Es una constante, no un
#: comentario: el test de censo la compara contra ``dataclasses.fields``.
APPROVAL_SCOPE_FIELDS = 14

#: §37.4: una aprobación ``ISSUED`` bloquea la emisión de la siguiente.
MAX_ISSUED_APPROVALS_PER_ROOT = 1

#: §37.4: no hay TTL. Se declara para que el contrato sea legible y auditable.
TIME_BASED_EXPIRATION_REQUIRED = False

#: §38.2: el store de tombstones es la única frontera anti-rollback.
APPROVAL_TOMBSTONE_GC_PERMITIDO = False

#: §37.2: dos archivos no forman una transacción.
CROSS_FILE_ATOMICITY = False

_HEX_DIGEST = 64

#: §38.2 — filename safety del tombstone. ``approval_seq`` decimal SIN signo y
#: SIN ceros a la izquierda; UUID v4 canónico en minúsculas.
_PATRON_TOMBSTONE = re.compile(
    r"^(?P<seq>[1-9][0-9]*)-(?P<ident>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json$"
)

#: §40.2 — un witness por ``approval_seq``.
_PATRON_WITNESS = re.compile(r"^(?P<seq>[1-9][0-9]*)\.json$")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumeraciones normativas
# ---------------------------------------------------------------------------


class ApprovalOperation(StrEnum):
    """Operación del ``ApprovalScope`` (campo 1; §38.5)."""

    PROMOTION = "PROMOTION"
    ROLLBACK = "ROLLBACK"


class ApprovalLifecycle(StrEnum):
    """FSM de la aprobación (§37.4). No hay un cuarto estado.

    ``PROPOSAL_IS_NOT_A_LIFECYCLE_STATE = YES``: una propuesta es un valor en
    memoria de la superficie emisora; lo único durable es el resultado del CAS.
    """

    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


class TerminalState(StrEnum):
    """Estados TERMINALES de un tombstone (§38.2)."""

    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


class ApprovalLedgerDisposition(StrEnum):
    """Clasificación de lectura del ledger. ``ABSENT`` no es ``CORRUPT``."""

    ABSENT = "ABSENT"
    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"
    CORRUPT = "CORRUPT"


class VeredictoAutoridad(StrEnum):
    """Veredicto de la reconciliación de la autoridad durable (§40.2/§40.4/§45.10)."""

    #: Evidencia durable y caché coherentes; no hay nada que reparar.
    CONSISTENT = "CONSISTENT"
    #: Sin ledger y sin evidencia durable: el estado inicial legítimo.
    GENESIS = "GENESIS"
    #: Retrocede SÓLO la revisión, con witnesses y tombstones coherentes (N6):
    #: se reconstruye la caché hacia adelante, sin pedir aprobación nueva.
    CACHE_REVISION_ROLLBACK_ONLY = "CACHE_REVISION_ROLLBACK_ONLY"
    #: La evidencia durable está por delante del ledger (S1/R2) y es contigua:
    #: el ledger quedó ATRÁS y se completa hacia adelante de forma determinista.
    LEDGER_RETROCEDIDO = "LEDGER_RETROCEDIDO"
    #: El ledger afirma una emisión que ningún witness sustenta (S2).
    LEDGER_ISSUED_WITHOUT_WITNESS = "LEDGER_ISSUED_WITHOUT_WITNESS"
    #: El ledger declara un estado terminal sin tombstone (write-ahead violado).
    LEDGER_TERMINAL_WITHOUT_TOMBSTONE = "LEDGER_TERMINAL_WITHOUT_TOMBSTONE"
    #: Cadena de witnesses con hueco interior, o estado derivado que contradice
    #: el ledger. Nunca se resuelve a favor de la caché.
    INCONSISTENT_AUTHORITY = "INCONSISTENT_AUTHORITY"
    #: ``approval_id`` reutilizado por otro registro durable (S3/§40.3).
    APPROVAL_ID_MINTING_DEFECT = "APPROVAL_ID_MINTING_DEFECT"
    #: Evidencia ilegible o incoherente: el veredicto es siempre fail-closed.
    FAIL_CLOSED = "FAIL_CLOSED"


# ---------------------------------------------------------------------------
# Modelos tipados
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalScope:
    """Los 14 campos canónicos del ``ApprovalScope`` (§38.5).

    El orden de declaración **no** es semántico: el digest se computa sobre
    ``canonical_json``, que ordena claves. ``candidate_id = None`` **es**
    ``NOT_APPLICABLE`` (§38.5 campo 10): se usa ``None`` y no la cadena
    ``"NOT_APPLICABLE"`` para que un candidato llamado así no pueda confundirse
    con la ausencia de candidato, y el digest compromete el ``null`` explícito.
    """

    #: 1 — ``PROMOTION`` | ``ROLLBACK``.
    operation: ApprovalOperation
    #: 2 — contexto que se REEMPLAZA (§35.2; incluye ``effective_runtime_evidence``).
    source_activation: SourceActivation
    #: 3 — SHA-256(canonical_json(source_activation)).
    source_activation_digest: str
    #: 4 — target (sin prefijo ``target_``: ese prefijo es del registro, §38.5).
    generation_id: str
    #: 5 — target.
    clone_id: str
    #: 6 — digest de CONTENIDO del Clone, no su ruta.
    clone_evidence: str
    #: 7 — target.
    runtime_setup_id: str
    #: 8 — digest de CONTENIDO del setup aplicado.
    runtime_setup_evidence: str
    #: 9 — evidencia de compatibilidad, content-bound (§34.7).
    compatibility_evidence_id: str
    #: 10 — REQUIRED en PROMOTION · ``NOT_APPLICABLE`` (``None``) en ROLLBACK.
    candidate_id: str | None
    #: 11 — UUID v4 del token, preasignado ANTES del digest (§39.3 A2).
    approval_id: str
    #: 12 — entero monótono por root, preasignado ANTES del digest.
    approval_seq: int
    #: 13 — SHA-256 del scope sin este campo.
    approval_scope_digest: str
    #: 14 — referencia opaca a la superficie emisora + id de solicitud.
    approval_provenance: str


@dataclasses.dataclass(frozen=True, slots=True)
class PropuestaAprobacion:
    """Propuesta en MEMORIA: no persiste nada y no tiene autoridad (§39.3 A2).

    ``ledger_revision_at_proposal`` es la ``R`` de A1 y la que el ``CAS ISSUE``
    presenta como esperada. Releerla al emitir sería ABA (D0-R6.1): el CAS tiene
    que comparar contra la revisión que el propietario vio, no contra la actual.
    """

    scope: ApprovalScope
    ledger_revision_at_proposal: int
    proposed_at_ns: int


@dataclasses.dataclass(frozen=True, slots=True)
class ConsentimientoAprobacion:
    """Consentimiento HUMANO sobre un digest concreto (§39.3 B2).

    En este slice es una **fixture explícita**: no existe superficie HITL, así
    que la única vía productiva de emisión (``emitir_aprobacion``) falla cerrado.
    El objeto liga exactamente lo que el propietario aprobó —el digest y la
    procedencia de la presentación— para que ``WHAT_HUMAN_APPROVED ==
    WHAT_WAS_ISSUED`` sea verificable y no una intención.
    """

    approval_scope_digest: str
    approval_provenance: str
    surface: str


@dataclasses.dataclass(frozen=True, slots=True)
class IssuanceWitness:
    """Evidencia inmutable de emisión (``state/issuances/<seq>.json``; §40.2).

    ``ledger_revision_at_issue`` permite reparar un ledger atrasado de forma
    determinista: el witness guarda la terna completa y la revisión sobre la que
    se emitió.
    """

    schema_version: int
    root_id: str
    approval_seq: int
    approval_id: str
    approval_scope_digest: str
    approval_provenance: str
    issued_at_ns: int
    ledger_revision_at_issue: int


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalTombstone:
    """Evidencia terminal inmutable (``state/approvals/<seq>-<id>.json``; §38.2)."""

    schema_version: int
    approval_id: str
    approval_seq: int
    approval_scope_digest: str
    terminal_state: TerminalState
    consumed_by_transition_id: str | None
    revoked_reason: str | None
    terminal_at_ns: int


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalLedger:
    """Caché reconciliable (``state/approval.json``; §37.3).

    ``approval_revision`` es un token de concurrencia, **no** autoridad: su
    retroceso, por sí solo, no rompe ninguna garantía porque la caché se
    reconstruye desde la evidencia durable (§37.6; §45.10).

    ``last_issued_approval_seq == 0`` y ``state is None`` representan el génesis
    (ninguna aprobación emitida todavía).
    """

    schema_version: int
    root_id: str
    approval_revision: int
    last_issued_approval_seq: int
    last_issued_approval_id: str | None
    last_issued_scope_digest: str | None
    last_issued_provenance: str | None
    last_consumed_approval_seq: int
    consumed_approval_id: str | None
    consumed_scope_digest: str | None
    consumed_by_transition_id: str | None
    state: ApprovalLifecycle | None
    issued_at_ns: int | None
    consumed_at_ns: int | None
    revoked_at_ns: int | None
    approval_terminal_store: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalLedgerLoadResult:
    """Resultado de lectura del ledger: ausente NO es corrupto."""

    disposition: ApprovalLedgerDisposition
    ledger: ApprovalLedger | None
    path: pathlib.Path
    message: str


@dataclasses.dataclass(frozen=True, slots=True)
class AutoridadAprobacion:
    """Vista derivada de los DOS stores inmutables (§37.6).

    ``max_issued_seq`` es ``H`` y ``max_terminal_seq`` es ``T`` de §40.2.
    ``last_consumed`` es el tombstone de MAYOR ``approval_seq`` **cuyo
    ``terminal_state == CONSUMED``** — un ``REVOKED`` de seq mayor nunca participa
    en ``last_consumed_*`` (R11.13; §45.10.2).
    """

    witnesses: tuple[IssuanceWitness, ...]
    tombstones: tuple[ApprovalTombstone, ...]
    max_issued_seq: int
    max_terminal_seq: int
    last_consumed: ApprovalTombstone | None
    lifecycle: ApprovalLifecycle | None
    revision_minima: int


@dataclasses.dataclass(frozen=True, slots=True)
class ResultadoConsumo:
    """Resultado de un ``CONSUME``: evidencia terminal + estado de la caché."""

    approval_id: str
    approval_seq: int
    scope_digest: str
    consumed_by_transition_id: str
    terminal_path: pathlib.Path
    cache_reconciled: bool

    def a_evidencia_journal(self) -> AprobacionConsumida:
        """Puente a P4-S3: la evidencia que el ``CREATE`` del journal verifica."""
        return AprobacionConsumida(
            approval_id=self.approval_id,
            approval_seq=self.approval_seq,
            scope_digest=self.scope_digest,
            consumed_by_transition_id=self.consumed_by_transition_id,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ReconciliacionAprobacion:
    """Veredicto de la reconciliación de arranque (§40.2/§45.10)."""

    veredicto: VeredictoAutoridad
    motivo: str
    ledger: ApprovalLedger | None
    autoridad: AutoridadAprobacion | None
    cache_reconstruida: bool


# ---------------------------------------------------------------------------
# Validación de forma (paridad con el journal; ver test de paridad)
# ---------------------------------------------------------------------------


def _exigir_texto(valor: object, *, campo: str, etiqueta: str) -> str:
    """String no vacío y sin whitespace de borde. Se VALIDA, no se normaliza."""
    if isinstance(valor, bool) or not isinstance(valor, str) or not valor:
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' debe ser un string no vacío")
    if valor != valor.strip():
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' no puede tener espacios en los bordes: {valor!r}")
    return valor


def _exigir_entero(valor: object, *, campo: str, etiqueta: str, minimo: int) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < minimo:
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' debe ser un entero >= {minimo}")
    return valor


def _exigir_uuid_v4(valor: object, *, campo: str, etiqueta: str) -> str:
    if isinstance(valor, bool) or not isinstance(valor, str) or not valor:
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' debe ser un string")
    try:
        parseado = uuid.UUID(valor)
    except ValueError as exc:
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' no es un UUID válido: {exc}") from exc
    if parseado.version != 4 or str(parseado) != valor:
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' debe ser un UUID v4 canónico en minúsculas")
    return valor


def _exigir_digest(valor: object, *, campo: str, etiqueta: str) -> str:
    if isinstance(valor, bool) or not isinstance(valor, str):
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' debe ser un string")
    if len(valor) != _HEX_DIGEST or any(caracter not in "0123456789abcdef" for caracter in valor):
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' debe ser un digest SHA-256 hexadecimal de 64 caracteres")
    return valor


def _exigir_generation_id(valor: object, *, campo: str, etiqueta: str) -> str:
    """Generation-id CANÓNICO según el validador del dominio (misma puerta que el journal)."""
    if isinstance(valor, bool) or not isinstance(valor, str) or not valor:
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' debe ser un string no vacío")
    try:
        canonico = validar_generation_id(valor)
    except InvalidGenerationIdError as exc:
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' no es un generation-id válido: {exc}") from exc
    if canonico != valor:
        raise ApprovalSchemaError(f"{etiqueta}: '{campo}' debe ser un generation-id CANÓNICO: {valor!r}")
    return valor


def _exigir_campos_exactos(
    payload: dict[str, object],
    *,
    requeridos: frozenset[str],
    etiqueta: str,
) -> None:
    """Esquema CERRADO: falta un campo obligatorio o sobra uno desconocido ⇒ error."""
    claves = set(payload)
    if not requeridos.issubset(claves):
        raise ApprovalSchemaError(f"{etiqueta}: faltan campos obligatorios {sorted(requeridos - claves)}")
    sobran = claves - requeridos
    if sobran:
        raise ApprovalSchemaError(f"{etiqueta}: campos desconocidos {sorted(sobran)} (esquema cerrado)")


# ---------------------------------------------------------------------------
# ApprovalScope: digest y validación
# ---------------------------------------------------------------------------


def payload_scope_sin_digest(scope: ApprovalScope) -> dict[str, object]:
    """Los 13 campos que el ``approval_scope_digest`` compromete (§38.5).

    Se enumeran a mano y no con ``dataclasses.asdict`` a propósito: así el test de
    censo puede afirmar que son exactamente 13 y que cubren los 14 campos menos
    ``approval_scope_digest``. Un ``asdict`` incluiría el propio digest y el
    cálculo sería circular.
    """
    return {
        "operation": scope.operation.value,
        "source_activation": dataclasses.asdict(scope.source_activation),
        "source_activation_digest": scope.source_activation_digest,
        "generation_id": scope.generation_id,
        "clone_id": scope.clone_id,
        "clone_evidence": scope.clone_evidence,
        "runtime_setup_id": scope.runtime_setup_id,
        "runtime_setup_evidence": scope.runtime_setup_evidence,
        "compatibility_evidence_id": scope.compatibility_evidence_id,
        "candidate_id": scope.candidate_id,
        "approval_id": scope.approval_id,
        "approval_seq": scope.approval_seq,
        "approval_provenance": scope.approval_provenance,
    }


def calcular_approval_scope_digest(scope: ApprovalScope) -> str:
    """SHA-256 del scope sin el propio digest (§38.5)."""
    return digest_canonico(payload_scope_sin_digest(scope))


def _validar_source_activation(fuente: object, *, etiqueta: str) -> SourceActivation:
    if not isinstance(fuente, SourceActivation):
        raise ApprovalSchemaError(f"{etiqueta}: 'source_activation' debe ser SourceActivation")
    for campo, generacion in (
        ("generation_id", True),
        ("clone_id", False),
        ("runtime_setup_id", False),
        ("effective_runtime_evidence", False),
    ):
        valor = getattr(fuente, campo)
        if valor is None:
            # El source puede ser NONE (arranque limpio: §36.8). Un `None` es un
            # valor legítimo y el digest lo compromete; sólo se valida la forma
            # cuando hay valor.
            continue
        if generacion:
            _exigir_generation_id(valor, campo=campo, etiqueta=etiqueta)
        else:
            _exigir_texto(valor, campo=campo, etiqueta=etiqueta)
    return fuente


def validar_scope(scope: ApprovalScope) -> ApprovalScope:
    """Valida los 14 campos y que el digest recompute (§38.5).

    Un ``ApprovalScope`` adulterado —cualquiera de los 13 campos cambiado sin
    recomputar el digest— se rechaza acá, antes de tocar el disco. El consumo
    además lo compara contra ``last_issued_scope_digest``, que es la segunda
    fuente independiente: este chequeo solo sería tautológico si el presentador
    pudiera fijar ambos lados, y no puede (§37.3).
    """
    if not isinstance(scope, ApprovalScope):
        raise ApprovalSchemaError("scope: se esperaba un ApprovalScope")
    if not isinstance(scope.operation, ApprovalOperation):
        raise ApprovalSchemaError("scope: 'operation' debe ser ApprovalOperation")
    fuente = _validar_source_activation(scope.source_activation, etiqueta="scope")
    digest_fuente = digest_canonico(fuente)
    _exigir_digest(scope.source_activation_digest, campo="source_activation_digest", etiqueta="scope")
    if scope.source_activation_digest != digest_fuente:
        raise ApprovalIdentityError(
            "scope: 'source_activation_digest' no liga el contenido de source_activation (§35.2)"
        )
    _exigir_generation_id(scope.generation_id, campo="generation_id", etiqueta="scope")
    _exigir_texto(scope.clone_id, campo="clone_id", etiqueta="scope")
    _exigir_digest(scope.clone_evidence, campo="clone_evidence", etiqueta="scope")
    _exigir_texto(scope.runtime_setup_id, campo="runtime_setup_id", etiqueta="scope")
    _exigir_digest(scope.runtime_setup_evidence, campo="runtime_setup_evidence", etiqueta="scope")
    _exigir_digest(scope.compatibility_evidence_id, campo="compatibility_evidence_id", etiqueta="scope")
    # Campo 10: obligatorio en PROMOTION, NOT_APPLICABLE en ROLLBACK (§38.5).
    if scope.operation is ApprovalOperation.PROMOTION:
        _exigir_texto(scope.candidate_id, campo="candidate_id", etiqueta="scope (PROMOTION exige candidate_id)")
    elif scope.candidate_id is not None:
        raise ApprovalSchemaError(
            "scope: ROLLBACK exige candidate_id = NOT_APPLICABLE (None); "
            f"se recibió {scope.candidate_id!r} (§38.5 campo 10)"
        )
    _exigir_uuid_v4(scope.approval_id, campo="approval_id", etiqueta="scope")
    _exigir_entero(scope.approval_seq, campo="approval_seq", etiqueta="scope", minimo=1)
    _exigir_texto(scope.approval_provenance, campo="approval_provenance", etiqueta="scope")
    _exigir_digest(scope.approval_scope_digest, campo="approval_scope_digest", etiqueta="scope")
    if scope.approval_scope_digest != calcular_approval_scope_digest(scope):
        raise ApprovalIdentityError(
            "scope: 'approval_scope_digest' no recomputa sobre los otros 13 campos "
            "(el scope fue adulterado o el digest no se calculó con la forma canónica)"
        )
    return scope


def construir_scope(
    *,
    operation: ApprovalOperation,
    source_activation: SourceActivation,
    generation_id: str,
    clone_id: str,
    clone_evidence: str,
    runtime_setup_id: str,
    runtime_setup_evidence: str,
    compatibility_evidence_id: str,
    candidate_id: str | None,
    approval_id: str,
    approval_seq: int,
    approval_provenance: str,
) -> ApprovalScope:
    """Construye y sella el scope (digest incluido) — §39.3 A3/A4.

    ``approval_id`` y ``approval_seq`` llegan YA preasignados: el digest los
    compromete, así que no pueden nacer después.
    """
    borrador = ApprovalScope(
        operation=operation,
        source_activation=source_activation,
        source_activation_digest=digest_canonico(source_activation),
        generation_id=generation_id,
        clone_id=clone_id,
        clone_evidence=clone_evidence,
        runtime_setup_id=runtime_setup_id,
        runtime_setup_evidence=runtime_setup_evidence,
        compatibility_evidence_id=compatibility_evidence_id,
        candidate_id=candidate_id,
        approval_id=approval_id,
        approval_seq=approval_seq,
        approval_scope_digest="0" * _HEX_DIGEST,
        approval_provenance=approval_provenance,
    )
    # Se construye el scope definitivo explícitamente en vez de con
    # ``dataclasses.replace``: el censo de mutadores de P4-S2 clasifica
    # ``X.replace(...)`` por nombre de atributo, y ``dataclasses.replace`` no
    # escribe nada. Una excepción en el censo por un falso positivo es peor que
    # una construcción explícita: el censo se queda sin agujeros.
    scope = ApprovalScope(
        operation=operation,
        source_activation=source_activation,
        source_activation_digest=borrador.source_activation_digest,
        generation_id=generation_id,
        clone_id=clone_id,
        clone_evidence=clone_evidence,
        runtime_setup_id=runtime_setup_id,
        runtime_setup_evidence=runtime_setup_evidence,
        compatibility_evidence_id=compatibility_evidence_id,
        candidate_id=candidate_id,
        approval_id=approval_id,
        approval_seq=approval_seq,
        approval_scope_digest=calcular_approval_scope_digest(borrador),
        approval_provenance=approval_provenance,
    )
    return validar_scope(scope)


# ---------------------------------------------------------------------------
# Serialización / parseo
# ---------------------------------------------------------------------------


def _serializar_ledger(ledger: ApprovalLedger) -> dict[str, object]:
    return {
        "schema_version": ledger.schema_version,
        "root_id": ledger.root_id,
        "approval_revision": ledger.approval_revision,
        "last_issued_approval_seq": ledger.last_issued_approval_seq,
        "last_issued_approval_id": ledger.last_issued_approval_id,
        "last_issued_scope_digest": ledger.last_issued_scope_digest,
        "last_issued_provenance": ledger.last_issued_provenance,
        "last_consumed_approval_seq": ledger.last_consumed_approval_seq,
        "consumed_approval_id": ledger.consumed_approval_id,
        "consumed_scope_digest": ledger.consumed_scope_digest,
        "consumed_by_transition_id": ledger.consumed_by_transition_id,
        "state": ledger.state.value if ledger.state is not None else None,
        "issued_at_ns": ledger.issued_at_ns,
        "consumed_at_ns": ledger.consumed_at_ns,
        "revoked_at_ns": ledger.revoked_at_ns,
        "approval_terminal_store": ledger.approval_terminal_store,
    }


_CAMPOS_LEDGER = frozenset(
    {
        "schema_version",
        "root_id",
        "approval_revision",
        "last_issued_approval_seq",
        "last_issued_approval_id",
        "last_issued_scope_digest",
        "last_issued_provenance",
        "last_consumed_approval_seq",
        "consumed_approval_id",
        "consumed_scope_digest",
        "consumed_by_transition_id",
        "state",
        "issued_at_ns",
        "consumed_at_ns",
        "revoked_at_ns",
        "approval_terminal_store",
    }
)

_CAMPOS_WITNESS = frozenset(
    {
        "schema_version",
        "root_id",
        "approval_seq",
        "approval_id",
        "approval_scope_digest",
        "approval_provenance",
        "issued_at_ns",
        "ledger_revision_at_issue",
    }
)

_CAMPOS_TOMBSTONE = frozenset(
    {
        "schema_version",
        "approval_id",
        "approval_seq",
        "approval_scope_digest",
        "terminal_state",
        "consumed_by_transition_id",
        "revoked_reason",
        "terminal_at_ns",
    }
)


def _opcional(valor: object, *, campo: str, etiqueta: str, validador: str) -> str | None:
    if valor is None:
        return None
    if validador == "uuid":
        return _exigir_uuid_v4(valor, campo=campo, etiqueta=etiqueta)
    if validador == "digest":
        return _exigir_digest(valor, campo=campo, etiqueta=etiqueta)
    return _exigir_texto(valor, campo=campo, etiqueta=etiqueta)


def _exigir_root_local(root_id: str, *, esperado: str | None, ruta: pathlib.Path) -> None:
    """El artefacto declara a qué root pertenece; si no es éste, no es evidencia local.

    ``root_id`` es "el FrozenRuntimeRoot al que pertenece el ledger" (§37.3) y tanto
    el lock como la propiedad de la secuencia son root-keyed. Sin este cross-check,
    un ``state/`` copiado o restaurado desde OTRO root —con ledger, witnesses y
    tombstones, todo internamente coherente— pasaría la auditoría entera y la
    aprobación emitida para el root A autorizaría una transición en el root B.

    Es exactamente la comprobación que el journal hace sobre sus reservas de
    ``finalization_seq`` (``root_id_esperado``): mismo mecanismo, misma razón. Se
    verifica al LEER, no al escribir: un campo que sólo se escribe es decoración.
    """
    if esperado is not None and root_id != esperado:
        raise ApprovalRootAjenoError(
            f"{ruta}: 'root_id' no corresponde al root auditado "
            f"(declara {root_id!r}, el root canónico es {esperado!r}): "
            "la evidencia de OTRO root NO es evidencia local (§37.3)"
        )


def _parsear_ledger(
    payload: dict[str, object],
    ruta: pathlib.Path,
    *,
    root_id_esperado: str | None = None,
) -> ApprovalLedger:
    etiqueta = str(ruta)
    _exigir_campos_exactos(payload, requeridos=_CAMPOS_LEDGER, etiqueta=etiqueta)
    version = payload["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != APPROVAL_LEDGER_SCHEMA_VERSION:
        raise ApprovalSchemaError(
            f"{etiqueta}: schema_version desconocido {version!r}; soportado: "
            f"{APPROVAL_LEDGER_SCHEMA_VERSION} (fail-closed, no se reinterpreta)"
        )
    estado_raw = payload["state"]
    if estado_raw is None:
        estado: ApprovalLifecycle | None = None
    elif isinstance(estado_raw, str) and estado_raw in {e.value for e in ApprovalLifecycle}:
        estado = ApprovalLifecycle(estado_raw)
    else:
        raise ApprovalSchemaError(f"{etiqueta}: 'state' debe ser null, ISSUED, CONSUMED o REVOKED: {estado_raw!r}")
    root_id = _exigir_texto(payload["root_id"], campo="root_id", etiqueta=etiqueta)
    _exigir_root_local(root_id, esperado=root_id_esperado, ruta=ruta)
    ledger = ApprovalLedger(
        schema_version=version,
        root_id=root_id,
        approval_revision=_exigir_entero(
            payload["approval_revision"], campo="approval_revision", etiqueta=etiqueta, minimo=0
        ),
        last_issued_approval_seq=_exigir_entero(
            payload["last_issued_approval_seq"], campo="last_issued_approval_seq", etiqueta=etiqueta, minimo=0
        ),
        last_issued_approval_id=_opcional(
            payload["last_issued_approval_id"],
            campo="last_issued_approval_id",
            etiqueta=etiqueta,
            validador="uuid",
        ),
        last_issued_scope_digest=_opcional(
            payload["last_issued_scope_digest"],
            campo="last_issued_scope_digest",
            etiqueta=etiqueta,
            validador="digest",
        ),
        last_issued_provenance=_opcional(
            payload["last_issued_provenance"], campo="last_issued_provenance", etiqueta=etiqueta, validador="texto"
        ),
        last_consumed_approval_seq=_exigir_entero(
            payload["last_consumed_approval_seq"], campo="last_consumed_approval_seq", etiqueta=etiqueta, minimo=0
        ),
        consumed_approval_id=_opcional(
            payload["consumed_approval_id"], campo="consumed_approval_id", etiqueta=etiqueta, validador="uuid"
        ),
        consumed_scope_digest=_opcional(
            payload["consumed_scope_digest"], campo="consumed_scope_digest", etiqueta=etiqueta, validador="digest"
        ),
        consumed_by_transition_id=_opcional(
            payload["consumed_by_transition_id"],
            campo="consumed_by_transition_id",
            etiqueta=etiqueta,
            validador="uuid",
        ),
        state=estado,
        issued_at_ns=_entero_opcional(payload["issued_at_ns"], campo="issued_at_ns", etiqueta=etiqueta),
        consumed_at_ns=_entero_opcional(payload["consumed_at_ns"], campo="consumed_at_ns", etiqueta=etiqueta),
        revoked_at_ns=_entero_opcional(payload["revoked_at_ns"], campo="revoked_at_ns", etiqueta=etiqueta),
        approval_terminal_store=_opcional(
            payload["approval_terminal_store"],
            campo="approval_terminal_store",
            etiqueta=etiqueta,
            validador="texto",
        ),
    )
    _validar_coherencia_ledger(ledger, etiqueta=etiqueta)
    return ledger


def _entero_opcional(valor: object, *, campo: str, etiqueta: str) -> int | None:
    if valor is None:
        return None
    return _exigir_entero(valor, campo=campo, etiqueta=etiqueta, minimo=0)


def _validar_coherencia_ledger(ledger: ApprovalLedger, *, etiqueta: str) -> None:
    """Coherencia INTERNA del ledger (esquema cerrado por estado).

    Un ``state=ISSUED`` sin identidad emitida, o un ``state=null`` con una
    emisión registrada, no es un ledger degradado: es un ledger corrupto, y se
    trata como tal en vez de «arreglarlo» en silencio.
    """
    if ledger.state is None:
        if ledger.last_issued_approval_seq != 0 or ledger.last_issued_approval_id is not None:
            raise ApprovalSchemaError(f"{etiqueta}: state=null con una emisión registrada (incoherente)")
        return
    if ledger.last_issued_approval_seq < 1 or ledger.last_issued_approval_id is None:
        raise ApprovalSchemaError(f"{etiqueta}: state={ledger.state.value} sin identidad emitida (incoherente)")
    if ledger.last_issued_scope_digest is None or ledger.last_issued_provenance is None:
        raise ApprovalSchemaError(
            f"{etiqueta}: state={ledger.state.value} sin scope digest / provenance de la emisión (incoherente)"
        )
    if ledger.state is ApprovalLifecycle.CONSUMED:
        if ledger.last_consumed_approval_seq != ledger.last_issued_approval_seq:
            raise ApprovalSchemaError(
                f"{etiqueta}: state=CONSUMED con last_consumed_approval_seq distinto de la emisión (incoherente)"
            )
        if ledger.consumed_approval_id != ledger.last_issued_approval_id:
            raise ApprovalSchemaError(f"{etiqueta}: state=CONSUMED con consumed_approval_id ajeno (incoherente)")
        if ledger.consumed_by_transition_id is None:
            raise ApprovalSchemaError(f"{etiqueta}: state=CONSUMED sin consumed_by_transition_id (incoherente)")


def _serializar_witness(witness: IssuanceWitness) -> dict[str, object]:
    return {
        "schema_version": witness.schema_version,
        "root_id": witness.root_id,
        "approval_seq": witness.approval_seq,
        "approval_id": witness.approval_id,
        "approval_scope_digest": witness.approval_scope_digest,
        "approval_provenance": witness.approval_provenance,
        "issued_at_ns": witness.issued_at_ns,
        "ledger_revision_at_issue": witness.ledger_revision_at_issue,
    }


def _parsear_witness(
    payload: dict[str, object],
    ruta: pathlib.Path,
    *,
    seq_esperada: int,
    root_id_esperado: str | None = None,
) -> IssuanceWitness:
    etiqueta = str(ruta)
    _exigir_campos_exactos(payload, requeridos=_CAMPOS_WITNESS, etiqueta=etiqueta)
    version = payload["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != ISSUANCE_WITNESS_SCHEMA_VERSION:
        raise ApprovalSchemaError(f"{etiqueta}: schema_version desconocido {version!r} (fail-closed)")
    seq = _exigir_entero(payload["approval_seq"], campo="approval_seq", etiqueta=etiqueta, minimo=1)
    if seq != seq_esperada:
        raise ApprovalIdentityError(
            f"{etiqueta}: 'approval_seq' del cuerpo ({seq}) no coincide con el del nombre ({seq_esperada})"
        )
    root_id = _exigir_texto(payload["root_id"], campo="root_id", etiqueta=etiqueta)
    _exigir_root_local(root_id, esperado=root_id_esperado, ruta=ruta)
    return IssuanceWitness(
        schema_version=version,
        root_id=root_id,
        approval_seq=seq,
        approval_id=_exigir_uuid_v4(payload["approval_id"], campo="approval_id", etiqueta=etiqueta),
        approval_scope_digest=_exigir_digest(
            payload["approval_scope_digest"], campo="approval_scope_digest", etiqueta=etiqueta
        ),
        approval_provenance=_exigir_texto(
            payload["approval_provenance"], campo="approval_provenance", etiqueta=etiqueta
        ),
        issued_at_ns=_exigir_entero(payload["issued_at_ns"], campo="issued_at_ns", etiqueta=etiqueta, minimo=0),
        ledger_revision_at_issue=_exigir_entero(
            payload["ledger_revision_at_issue"], campo="ledger_revision_at_issue", etiqueta=etiqueta, minimo=0
        ),
    )


def _serializar_tombstone(tombstone: ApprovalTombstone) -> dict[str, object]:
    return {
        "schema_version": tombstone.schema_version,
        "approval_id": tombstone.approval_id,
        "approval_seq": tombstone.approval_seq,
        "approval_scope_digest": tombstone.approval_scope_digest,
        "terminal_state": tombstone.terminal_state.value,
        "consumed_by_transition_id": tombstone.consumed_by_transition_id,
        "revoked_reason": tombstone.revoked_reason,
        "terminal_at_ns": tombstone.terminal_at_ns,
    }


def _parsear_tombstone(
    payload: dict[str, object], ruta: pathlib.Path, *, seq_esperada: int, id_esperado: str
) -> ApprovalTombstone:
    etiqueta = str(ruta)
    _exigir_campos_exactos(payload, requeridos=_CAMPOS_TOMBSTONE, etiqueta=etiqueta)
    version = payload["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != TERMINAL_TOMBSTONE_SCHEMA_VERSION:
        raise ApprovalSchemaError(f"{etiqueta}: schema_version desconocido {version!r} (fail-closed)")
    estado_raw = payload["terminal_state"]
    if not isinstance(estado_raw, str) or estado_raw not in {e.value for e in TerminalState}:
        raise ApprovalSchemaError(f"{etiqueta}: 'terminal_state' debe ser CONSUMED o REVOKED: {estado_raw!r}")
    estado = TerminalState(estado_raw)
    seq = _exigir_entero(payload["approval_seq"], campo="approval_seq", etiqueta=etiqueta, minimo=1)
    ident = _exigir_uuid_v4(payload["approval_id"], campo="approval_id", etiqueta=etiqueta)
    if seq != seq_esperada or ident != id_esperado:
        raise ApprovalIdentityError(
            f"{etiqueta}: el contenido ({seq}-{ident}) no coincide con el nombre del archivo "
            f"({seq_esperada}-{id_esperado})"
        )
    transicion = payload["consumed_by_transition_id"]
    motivo = payload["revoked_reason"]
    transicion_validada: str | None
    motivo_validado: str | None
    if estado is TerminalState.CONSUMED:
        if transicion is None:
            raise ApprovalSchemaError(f"{etiqueta}: terminal_state=CONSUMED exige consumed_by_transition_id")
        if motivo is not None:
            raise ApprovalSchemaError(f"{etiqueta}: terminal_state=CONSUMED no admite revoked_reason")
        transicion_validada = _exigir_uuid_v4(transicion, campo="consumed_by_transition_id", etiqueta=etiqueta)
        motivo_validado = None
    else:
        if transicion is not None:
            raise ApprovalSchemaError(f"{etiqueta}: terminal_state=REVOKED no admite consumed_by_transition_id")
        if motivo is None:
            raise ApprovalSchemaError(f"{etiqueta}: terminal_state=REVOKED exige revoked_reason")
        transicion_validada = None
        motivo_validado = _exigir_texto(motivo, campo="revoked_reason", etiqueta=etiqueta)
    return ApprovalTombstone(
        schema_version=version,
        approval_id=ident,
        approval_seq=seq,
        approval_scope_digest=_exigir_digest(
            payload["approval_scope_digest"], campo="approval_scope_digest", etiqueta=etiqueta
        ),
        terminal_state=estado,
        consumed_by_transition_id=transicion_validada,
        revoked_reason=motivo_validado,
        terminal_at_ns=_exigir_entero(payload["terminal_at_ns"], campo="terminal_at_ns", etiqueta=etiqueta, minimo=0),
    )


# ---------------------------------------------------------------------------
# Primitivas de disco
# ---------------------------------------------------------------------------


def _leer_json(ruta: pathlib.Path) -> dict[str, object]:
    """Lector estricto compartido; traduce el error a la familia de aprobación."""
    try:
        return leer_json_de_control(ruta)
    except JournalCorruptError as exc:
        raise ApprovalCorruptError(str(exc)) from exc


def _admitir_dir(directorio: pathlib.Path) -> bool:
    """Admite un directorio del store ANTES de preguntar si existe.

    ``exists()`` sigue enlaces: un junction colgado —o un ancestro redirigido— se
    ve «ausente» y el store se degradaría a «vacío legítimo». La admisión va
    PRIMERO y falla cerrado (misma doctrina que el journal, §34.5 N2).
    """
    admision = admitir_directorio_storage(directorio)
    if not admision.success:
        raise ApprovalIdentityError(f"{directorio}: {admision.message}")
    return directorio.exists()


def _exigir_dirs_store(root: pathlib.Path) -> None:
    """Los dos stores inmutables deben existir (los crea el init del storage)."""
    for directorio in (issuances_state_dir(root), approvals_state_dir(root)):
        if not _admitir_dir(directorio):
            raise ApprovalStoreNoInicializadoError(
                f"{directorio} no existe: ejecutá initialize_frozen_runtime_storage antes de usar la "
                "autoridad de aprobación"
            )


def _crear_json_inmutable(ruta: pathlib.Path, payload: dict[str, object]) -> None:
    """``CREATE`` no-clobber de un store inmutable (§38.2/§40.2).

    ``open(path, "x")`` es el modo que el propio ADR nombra: reserva exclusiva,
    sin ``os.replace`` y sin ``os.open``. El contenido se escribe completo sobre
    el descriptor recién creado, con ``flush`` + ``fsync`` antes de cerrar.

    Un archivo PARCIAL (crash a mitad de la escritura) queda como evidencia
    ilegible y el lector lo clasifica ``FAIL_CLOSED`` — nunca «ausente» (§38.2).
    Es una diferencia DELIBERADA con el journal, que completa un placeholder
    vacío: para una aprobación, «¿esta aprobación es terminal?» no admite una
    respuesta reconstruida por el camino de escritura.
    """
    exigir_namespace_escribible(ruta.parent)
    texto = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    with open(ruta, "x", encoding="utf-8") as fh:
        fh.write(texto)
        fh.flush()
        os.fsync(fh.fileno())


def _nombre_witness(seq: int) -> str:
    return f"{seq}.json"


def _nombre_tombstone(seq: int, approval_id: str) -> str:
    return f"{seq}-{approval_id}.json"


def _root_id(root: pathlib.Path) -> str:
    """Identidad canónica del root (la misma clave que usa el root lock)."""
    from sky_claw.local.frozen_runtime.root_lock import canonical_root_key

    return canonical_root_key(root)[0]


# ---------------------------------------------------------------------------
# Lectura del ledger (caché)
# ---------------------------------------------------------------------------


def _ledger_genesis(root: pathlib.Path) -> ApprovalLedger:
    return ApprovalLedger(
        schema_version=APPROVAL_LEDGER_SCHEMA_VERSION,
        root_id=_root_id(root),
        approval_revision=0,
        last_issued_approval_seq=0,
        last_issued_approval_id=None,
        last_issued_scope_digest=None,
        last_issued_provenance=None,
        last_consumed_approval_seq=0,
        consumed_approval_id=None,
        consumed_scope_digest=None,
        consumed_by_transition_id=None,
        state=None,
        issued_at_ns=None,
        consumed_at_ns=None,
        revoked_at_ns=None,
        approval_terminal_store=None,
    )


def cargar_ledger_aprobacion(root: pathlib.Path) -> ApprovalLedgerLoadResult:
    """Carga tipada del ledger: ausente (génesis) vs corrupto (fail-closed).

    ``LEDGER_AUSENTE`` no se interpreta como «sin consumo previo» cuando hay
    evidencia durable: eso lo decide la reconciliación, no este lector (§36.5).
    """
    ruta = approval_ledger_path(pathlib.Path(root))
    root_id_esperado = _root_id(pathlib.Path(root))
    try:
        kind = link_kind_or_raise(ruta)
    except OSError as exc:
        raise ApprovalCorruptError(f"{ruta}: no se pudo inspeccionar el ledger: {exc}") from exc
    if kind is not None:
        raise ApprovalCorruptError(f"{ruta}: el ledger es un enlace/reparse ({kind}): fail-closed")
    motivo_padre = descripcion_de_enlace(ruta.parent)
    if motivo_padre is not None:
        raise ApprovalCorruptError(f"{ruta}: namespace del ledger redirigido: {motivo_padre}")
    if not ruta.exists():
        return ApprovalLedgerLoadResult(
            disposition=ApprovalLedgerDisposition.ABSENT,
            ledger=None,
            path=ruta,
            message="ledger ausente: sin emisiones registradas en la caché",
        )
    try:
        # La LECTURA va dentro del try, no sólo el parseo: un ledger vacío o con
        # bytes no-JSON es el caso más común de corrupción, y si escapara como
        # excepción la clasificación ``CORRUPT`` sería inalcanzable (los guards
        # ``disposition is CORRUPT`` de los llamadores quedarían muertos y cada
        # caller reventaría con un error de I/O en vez de diagnosticar). El
        # namespace ya se validó arriba, así que lo que queda por acá es
        # contenido: se clasifica, nunca se degrada a ABSENT (§36.5).
        payload = _leer_json(ruta)
        ledger = _parsear_ledger(payload, ruta, root_id_esperado=root_id_esperado)
    except ApprovalRootAjenoError:
        # NO se clasifica como CORRUPT: el archivo es legible y coherente, lo que
        # no corresponde es el root. Se levanta para que el operador vea «evidencia
        # de otro root» y no «archivo ilegible» (que invita a borrarlo).
        raise
    except (ApprovalCorruptError, ApprovalSchemaError, ApprovalIdentityError) as exc:
        # Contenido ilegible o incoherente: se CLASIFICA como corrupto en vez de
        # degradarse a ABSENT (§36.5: ``LEDGER_CORRUPTO ⇒ FAIL_CLOSED``, nunca
        # «sin consumo previo»). Un namespace redirigido sí levanta, porque no es
        # «contenido corrupto» sino una escritura que escaparía del root.
        return ApprovalLedgerLoadResult(
            disposition=ApprovalLedgerDisposition.CORRUPT,
            ledger=None,
            path=ruta,
            message=f"ledger ilegible: {exc}",
        )
    if ledger.state is ApprovalLifecycle.ISSUED:
        disposicion = ApprovalLedgerDisposition.ISSUED
    elif ledger.state is ApprovalLifecycle.CONSUMED:
        disposicion = ApprovalLedgerDisposition.CONSUMED
    elif ledger.state is ApprovalLifecycle.REVOKED:
        disposicion = ApprovalLedgerDisposition.REVOKED
    else:
        disposicion = ApprovalLedgerDisposition.ABSENT
    return ApprovalLedgerLoadResult(disposition=disposicion, ledger=ledger, path=ruta, message="ledger cargado")


# ---------------------------------------------------------------------------
# Lectura de los stores inmutables
# ---------------------------------------------------------------------------


def _listar_archivos(directorio: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Enumera el store rechazando cualquier entrada que no sea un archivo regular.

    Un subdirectorio, un enlace o un nombre que no matchea el contrato es
    ``FAIL_CLOSED``: nunca se ignora en silencio (§38.2 R4).
    """
    try:
        entradas = sorted(directorio.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ApprovalCorruptError(f"{directorio}: no se pudo enumerar el store: {exc}") from exc
    for entrada in entradas:
        try:
            kind = link_kind_or_raise(entrada)
        except OSError as exc:
            raise ApprovalCorruptError(f"{entrada}: no se pudo inspeccionar: {exc}") from exc
        if kind is not None:
            raise ApprovalCorruptError(f"{entrada}: entrada del store es un enlace/reparse ({kind}): fail-closed")
        if not entrada.is_file():
            raise ApprovalCorruptError(f"{entrada}: entrada del store no es un archivo regular: fail-closed")
    return tuple(entradas)


def _cargar_witnesses(root: pathlib.Path) -> tuple[IssuanceWitness, ...]:
    """Lee ``state/issuances/`` y valida nombre, contenido y unicidad (§40.2)."""
    directorio = issuances_state_dir(root)
    root_id_esperado = _root_id(root)
    if not _admitir_dir(directorio):
        return ()
    witnesses: list[IssuanceWitness] = []
    vistos_seq: set[int] = set()
    vistos_id: set[str] = set()
    for ruta in _listar_archivos(directorio):
        coincidencia = _PATRON_WITNESS.match(ruta.name)
        if coincidencia is None:
            raise ApprovalCorruptError(
                f"{ruta}: nombre fuera del contrato '<approval_seq>.json' con seq decimal sin ceros a la "
                "izquierda: fail-closed (§40.2)"
            )
        seq = int(coincidencia.group("seq"))
        if seq in vistos_seq:
            raise ApprovalMintingDefectError(f"{directorio}: approval_seq {seq} duplicado en el store de emisiones")
        witness = _parsear_witness(_leer_json(ruta), ruta, seq_esperada=seq, root_id_esperado=root_id_esperado)
        if witness.approval_id in vistos_id:
            raise ApprovalMintingDefectError(
                f"{directorio}: approval_id {witness.approval_id} reutilizado por dos witnesses (S3)"
            )
        vistos_seq.add(seq)
        vistos_id.add(witness.approval_id)
        witnesses.append(witness)
    witnesses.sort(key=lambda item: item.approval_seq)
    _exigir_cadena_contigua(witnesses, directorio)
    return tuple(witnesses)


def _exigir_cadena_contigua(witnesses: list[IssuanceWitness], directorio: pathlib.Path) -> None:
    """La cadena de emisiones es contigua desde 1 (§40.2).

    Un hueco INTERIOR es ``INCONSISTENT_AUTHORITY``: la secuencia no salta, así
    que un hueco significa que se borró evidencia, no que se omitió un número.
    """
    for indice, witness in enumerate(witnesses, start=1):
        if witness.approval_seq != indice:
            raise ApprovalIdentityError(
                f"{directorio}: cadena de witnesses con hueco interior en seq {indice} "
                f"(se encontró {witness.approval_seq}): INCONSISTENT_AUTHORITY"
            )


def _cargar_tombstones(root: pathlib.Path) -> tuple[ApprovalTombstone, ...]:
    """Lee ``state/approvals/`` y valida nombre, contenido, unicidad y R3 (§38.2)."""
    directorio = approvals_state_dir(root)
    if not _admitir_dir(directorio):
        return ()
    tombstones: list[ApprovalTombstone] = []
    vistos_seq: set[int] = set()
    vistos_id: set[str] = set()
    for ruta in _listar_archivos(directorio):
        coincidencia = _PATRON_TOMBSTONE.match(ruta.name)
        if coincidencia is None:
            raise ApprovalCorruptError(f"{ruta}: nombre fuera del contrato '<seq>-<uuid v4>.json': fail-closed (§38.2)")
        seq = int(coincidencia.group("seq"))
        ident = coincidencia.group("ident")
        if seq in vistos_seq:
            raise ApprovalIdentityError(f"{directorio}: approval_seq {seq} duplicado en el store terminal (R3)")
        if ident in vistos_id:
            raise ApprovalIdentityError(f"{directorio}: approval_id {ident} duplicado en el store terminal (R3)")
        tombstone = _parsear_tombstone(_leer_json(ruta), ruta, seq_esperada=seq, id_esperado=ident)
        vistos_seq.add(seq)
        vistos_id.add(ident)
        tombstones.append(tombstone)
    tombstones.sort(key=lambda item: item.approval_seq)
    return tuple(tombstones)


def derivar_autoridad_aprobacion(root: pathlib.Path) -> AutoridadAprobacion:
    """Deriva ``H``, ``T``, ``last_consumed_*`` y el lifecycle de los stores (§37.6).

    Es LECTURA: no muta nada y no adquiere el root lock. Cualquier evidencia
    ilegible, duplicada, fuera de contrato o sin witness que la sustente levanta
    un error tipado — nunca se degrada a «store vacío».
    """
    ruta = pathlib.Path(root)
    witnesses = _cargar_witnesses(ruta)
    tombstones = _cargar_tombstones(ruta)
    por_clave = {(witness.approval_seq, witness.approval_id): witness for witness in witnesses}
    for tombstone in tombstones:
        witness = por_clave.get((tombstone.approval_seq, tombstone.approval_id))
        if witness is None:
            raise ApprovalIdentityError(
                f"{ruta}: tombstone {tombstone.approval_seq}-{tombstone.approval_id} sin witness de emisión: "
                "evidencia terminal sin autoridad de emisión (INCONSISTENT_AUTHORITY)"
            )
        if witness.approval_scope_digest != tombstone.approval_scope_digest:
            raise ApprovalIdentityError(
                f"{ruta}: el digest del tombstone {tombstone.approval_seq} no coincide con el del witness: "
                "evidencia terminal contradictoria"
            )
    max_issued = witnesses[-1].approval_seq if witnesses else 0
    max_terminal = tombstones[-1].approval_seq if tombstones else 0
    # R11.13/§45.10.2: `last_consumed_*` filtra por estado. El tombstone de MAYOR
    # seq puede ser REVOKED y entonces NO fue consumido: `seq 20 = CONSUMED`,
    # `seq 21 = REVOKED` ⇒ last_consumed sigue siendo 20.
    consumidos = [item for item in tombstones if item.terminal_state is TerminalState.CONSUMED]
    last_consumed = consumidos[-1] if consumidos else None
    lifecycle: ApprovalLifecycle | None = None
    if witnesses:
        ultimo = witnesses[-1]
        terminal = next(
            (
                item
                for item in tombstones
                if item.approval_seq == ultimo.approval_seq and item.approval_id == ultimo.approval_id
            ),
            None,
        )
        lifecycle = (
            ApprovalLifecycle(terminal.terminal_state.value) if terminal is not None else ApprovalLifecycle.ISSUED
        )
    revision_minima = (witnesses[-1].ledger_revision_at_issue + 1) if witnesses else 0
    return AutoridadAprobacion(
        witnesses=witnesses,
        tombstones=tombstones,
        max_issued_seq=max_issued,
        max_terminal_seq=max_terminal,
        last_consumed=last_consumed,
        lifecycle=lifecycle,
        revision_minima=revision_minima,
    )


# ---------------------------------------------------------------------------
# Clasificación de coherencia (R1–R4 / S1–S4 / N6)
# ---------------------------------------------------------------------------


def _tombstone_de(autoridad: AutoridadAprobacion, seq: int, ident: str | None) -> ApprovalTombstone | None:
    if ident is None:
        return None
    return next(
        (item for item in autoridad.tombstones if item.approval_seq == seq and item.approval_id == ident),
        None,
    )


def clasificar_autoridad(
    ledger_result: ApprovalLedgerLoadResult,
    autoridad: AutoridadAprobacion,
) -> tuple[VeredictoAutoridad, str]:
    """Clasifica la coherencia caché ↔ evidencia durable (§40.4, §45.10).

    El orden de los chequeos es normativo: R1 (resurrección directa) se evalúa
    antes que S1 (witness adelantado) porque describen situaciones distintas.

    R1 **no** manda ``FAIL_CLOSED`` incondicional: el tombstone es la autoridad, la
    evidencia durable es coherente y lo único que pasó es que la caché quedó atrás,
    así que el veredicto es ``LEDGER_RETROCEDIDO`` — reparable hacia adelante
    (§45.10.1, ``CACHE_REVISION_ROLLBACK_ONLY`` vs. autoridad incoherente). Lo que
    R1 prohíbe es lo único que importa: el token NO se consume y NUNCA se resucita.
    """
    ledger = ledger_result.ledger
    tiene_evidencia = bool(autoridad.witnesses or autoridad.tombstones)
    if ledger_result.disposition is ApprovalLedgerDisposition.CORRUPT:
        return VeredictoAutoridad.FAIL_CLOSED, "ledger corrupto: nunca se lee como «sin consumo previo»"
    if ledger is None:
        if not tiene_evidencia:
            return VeredictoAutoridad.GENESIS, "sin ledger y sin evidencia durable: estado inicial"
        return (
            VeredictoAutoridad.LEDGER_RETROCEDIDO,
            "ledger ausente con evidencia durable presente: la caché se reconstruye desde los stores",
        )
    # R1 — resurrección directa: el ledger presenta como ISSUED algo ya terminal.
    if ledger.state is ApprovalLifecycle.ISSUED:
        terminal = _tombstone_de(autoridad, ledger.last_issued_approval_seq, ledger.last_issued_approval_id)
        if terminal is not None:
            return (
                VeredictoAutoridad.LEDGER_RETROCEDIDO,
                f"R1: el ledger presenta como ISSUED la aprobación {ledger.last_issued_approval_seq}, que ya es "
                f"{terminal.terminal_state.value}: el token NO se consume (REQUIRE_OWNER)",
            )
    # Write-ahead violado: estado terminal sin evidencia terminal.
    if ledger.state in (ApprovalLifecycle.CONSUMED, ApprovalLifecycle.REVOKED):
        terminal = _tombstone_de(autoridad, ledger.last_issued_approval_seq, ledger.last_issued_approval_id)
        if terminal is None:
            return (
                VeredictoAutoridad.LEDGER_TERMINAL_WITHOUT_TOMBSTONE,
                f"el ledger declara {ledger.state.value} sin tombstone para "
                f"{ledger.last_issued_approval_seq}: write-ahead violado o store retrocedido",
            )
        if ApprovalLifecycle(terminal.terminal_state.value) is not ledger.state:
            return (
                VeredictoAutoridad.INCONSISTENT_AUTHORITY,
                f"el ledger declara {ledger.state.value} y el tombstone declara {terminal.terminal_state.value}",
            )
    # S2 — el ledger afirma una emisión que ningún witness sustenta.
    if autoridad.max_issued_seq < ledger.last_issued_approval_seq:
        return (
            VeredictoAutoridad.LEDGER_ISSUED_WITHOUT_WITNESS,
            f"el ledger afirma la emisión {ledger.last_issued_approval_seq} y el store de emisiones llega a "
            f"{autoridad.max_issued_seq}: LEDGER_ISSUED_WITHOUT_WITNESS",
        )
    max_evidencia = max(autoridad.max_issued_seq, autoridad.max_terminal_seq)
    # §40.4: la fila «ledger adelantado a la evidencia durable» se resuelve como
    # `LEDGER_ISSUED_WITHOUT_WITNESS` (S2), que ya se evaluó arriba: si
    # `max(H,T) < last_issued` entonces `max(H) < last_issued`, y S2 dispara
    # primero. No hay un veredicto separado que no sea código muerto.
    if max_evidencia > ledger.last_issued_approval_seq:
        return (
            VeredictoAutoridad.LEDGER_RETROCEDIDO,
            f"S1: max(H,T)={max_evidencia} > last_issued={ledger.last_issued_approval_seq}: el ledger quedó atrás "
            "de la evidencia durable y se completa hacia adelante",
        )
    if autoridad.lifecycle is not None and ledger.state is not None and autoridad.lifecycle is not ledger.state:
        return (
            VeredictoAutoridad.INCONSISTENT_AUTHORITY,
            f"el lifecycle derivado es {autoridad.lifecycle.value} y el ledger declara {ledger.state.value}",
        )
    # N6 — retrocede SÓLO la revisión, con evidencia coherente (§45.10.1).
    if ledger.approval_revision < autoridad.revision_minima:
        return (
            VeredictoAutoridad.CACHE_REVISION_ROLLBACK_ONLY,
            f"approval_revision={ledger.approval_revision} < mínima derivada {autoridad.revision_minima}: "
            "retroceso de la CACHÉ con evidencia coherente ⇒ se reconstruye, sin pedir aprobación nueva",
        )
    return VeredictoAutoridad.CONSISTENT, "caché y evidencia durable coherentes"


#: Veredictos que se reparan escribiendo la caché HACIA ADELANTE desde los stores.
_VEREDICTOS_REPARABLES = frozenset(
    {
        VeredictoAutoridad.CACHE_REVISION_ROLLBACK_ONLY,
        VeredictoAutoridad.LEDGER_RETROCEDIDO,
    }
)


def _reconstruir_ledger(
    root: pathlib.Path,
    autoridad: AutoridadAprobacion,
    *,
    revision_actual: int,
) -> ApprovalLedger:
    """Reconstruye la caché EXACTAMENTE desde la evidencia durable (§37.6).

    ``last_issued_*`` sale del witness de MAYOR ``approval_seq`` —sin restarle los
    tombstones—: restarlos haría RETROCEDER el contador de emisión al consumir o
    revocar la última aprobación, y la siguiente propuesta reutilizaría la misma
    ``seq`` para colisionar contra su propio witness inmutable (LEDGER_RETROCEDIDO
    permanente). ``last_consumed_*`` sale del tombstone de mayor seq **CONSUMED**.
    """
    ultimo = autoridad.witnesses[-1] if autoridad.witnesses else None
    terminal = None
    if ultimo is not None:
        terminal = _tombstone_de(autoridad, ultimo.approval_seq, ultimo.approval_id)
    estado = ApprovalLifecycle(terminal.terminal_state.value) if terminal is not None else None
    if ultimo is not None and terminal is None:
        estado = ApprovalLifecycle.ISSUED
    consumido = autoridad.last_consumed
    revision = max(revision_actual, autoridad.revision_minima) + 1
    return ApprovalLedger(
        schema_version=APPROVAL_LEDGER_SCHEMA_VERSION,
        root_id=_root_id(root),
        approval_revision=revision,
        last_issued_approval_seq=ultimo.approval_seq if ultimo is not None else 0,
        last_issued_approval_id=ultimo.approval_id if ultimo is not None else None,
        last_issued_scope_digest=ultimo.approval_scope_digest if ultimo is not None else None,
        last_issued_provenance=ultimo.approval_provenance if ultimo is not None else None,
        last_consumed_approval_seq=consumido.approval_seq if consumido is not None else 0,
        consumed_approval_id=consumido.approval_id if consumido is not None else None,
        consumed_scope_digest=consumido.approval_scope_digest if consumido is not None else None,
        consumed_by_transition_id=consumido.consumed_by_transition_id if consumido is not None else None,
        state=estado,
        issued_at_ns=ultimo.issued_at_ns if ultimo is not None else None,
        consumed_at_ns=consumido.terminal_at_ns if consumido is not None else None,
        revoked_at_ns=terminal.terminal_at_ns if terminal is not None and estado is ApprovalLifecycle.REVOKED else None,
        approval_terminal_store=(
            f"state/approvals/{_nombre_tombstone(terminal.approval_seq, terminal.approval_id)}"
            if terminal is not None
            else None
        ),
    )


def _escribir_ledger(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    ledger: ApprovalLedger,
) -> ApprovalLedger:
    """Escribe la caché SIN CAS (sólo para la reconstrucción hacia adelante)."""
    handle.assert_owned()
    ruta = approval_ledger_path(root)
    # Round-trip por el parser ANTES de escribir, con el root esperado: así un
    # ledger que declare otro root no se publica ni se reconstruye.
    _parsear_ledger(_serializar_ledger(ledger), ruta, root_id_esperado=_root_id(root))
    state_module.write_json_atomic(ruta, _serializar_ledger(ledger))
    return ledger


def _escribir_ledger_cas(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    *,
    esperado: int,
    nuevo: ApprovalLedger,
) -> ApprovalLedger:
    """CAS sobre ``approval_revision``: TODO escritor presenta la revisión esperada.

    El root lock serializa a los participantes; el CAS es la barrera contra un
    escritor que NO participa del lock (lost update / escritura stale). Falla
    cerrado: nunca «sigue igual».
    """
    handle.assert_owned()
    ruta = approval_ledger_path(root)
    actual = cargar_ledger_aprobacion(root)
    if actual.disposition is ApprovalLedgerDisposition.CORRUPT:
        raise ApprovalCorruptError(f"{ruta}: el ledger está corrupto: ninguna escritura lo pisa (FAIL_CLOSED, §36.5)")
    revision_actual = actual.ledger.approval_revision if actual.ledger is not None else 0
    if revision_actual != esperado:
        raise ApprovalCasError(
            f"{ruta}: CAS fallido — se esperaba approval_revision={esperado} y hay {revision_actual}"
        )
    definitivo = dataclasses.replace(nuevo, approval_revision=esperado + 1)
    _parsear_ledger(_serializar_ledger(definitivo), ruta, root_id_esperado=_root_id(root))
    state_module.write_json_atomic(ruta, _serializar_ledger(definitivo))
    return definitivo


def _reconciliar_hacia_adelante(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
) -> ApprovalLedger | None:
    """Repara la caché desde la evidencia durable DESPUÉS de una escritura fallida.

    Se usa cuando el tombstone o el witness ya se persistieron y el refresco de la
    caché falló: la emisión/terminalización **YA OCURRIÓ** —la evidencia inmutable
    es la autoridad— así que NO se descarta y NO se borra nada. Devuelve el ledger
    reconstruido, o ``None`` si la autoridad durable no es coherente (el caller
    debe fallar cerrado).
    """
    handle.assert_owned()
    autoridad = derivar_autoridad_aprobacion(root)
    actual = cargar_ledger_aprobacion(root)
    veredicto, _motivo = clasificar_autoridad(actual, autoridad)
    if veredicto is VeredictoAutoridad.CONSISTENT:
        return actual.ledger
    if veredicto not in _VEREDICTOS_REPARABLES:
        return None
    revision = actual.ledger.approval_revision if actual.ledger is not None else 0
    reconstruido = _reconstruir_ledger(root, autoridad, revision_actual=revision)
    return _escribir_ledger(handle, root, reconstruido)


# ---------------------------------------------------------------------------
# FASE A — propuesta (persiste NADA)
# ---------------------------------------------------------------------------


def proponer_aprobacion(
    root: pathlib.Path,
    *,
    operation: ApprovalOperation,
    source_activation: SourceActivation,
    generation_id: str,
    clone_id: str,
    clone_evidence: str,
    runtime_setup_id: str,
    runtime_setup_evidence: str,
    compatibility_evidence_id: str,
    candidate_id: str | None,
    approval_provenance: str,
    timeout: float = 0.0,
    approval_id_factory: Callable[[], object] = uuid.uuid4,
    clock_ns: Callable[[], int] = time.time_ns,
) -> PropuestaAprobacion:
    """FASE A del protocolo (§39.3): preasignar → digest, SIN persistir nada.

    Bajo el root lock: lee la caché y los stores, verifica la autoridad durable
    (A5) y acuña ``approval_seq``/``approval_id`` **en memoria**. Los dos valores
    existen antes del digest porque el digest los compromete; no tienen ninguna
    autoridad hasta el ``ISSUE``. El lock se libera al salir: no se sostiene
    durante la interacción humana (§39.4).
    """
    ruta = pathlib.Path(root)
    with participant_lock(ruta, timeout=timeout) as handle:
        handle.assert_owned()
        _exigir_dirs_store(ruta)
        actual = cargar_ledger_aprobacion(ruta)
        if actual.disposition is ApprovalLedgerDisposition.CORRUPT:
            raise ApprovalCorruptError(f"{actual.path}: ledger corrupto: no se propone (fail-closed)")
        autoridad = derivar_autoridad_aprobacion(ruta)
        veredicto, motivo = clasificar_autoridad(actual, autoridad)
        if veredicto is not VeredictoAutoridad.CONSISTENT and veredicto is not VeredictoAutoridad.GENESIS:
            raise ApprovalLedgerRetrocedidoError(
                f"no se propone con la autoridad durable en {veredicto.value}: {motivo} "
                "(§39.3 A5: FAIL_CLOSED · RECOVERY antes de presentar nada)"
            )
        ledger = actual.ledger if actual.ledger is not None else _ledger_genesis(ruta)
        if ledger.state is ApprovalLifecycle.ISSUED:
            raise ApprovalMaxIssuedError(
                f"ya existe una aprobación ISSUED (seq {ledger.last_issued_approval_seq}): "
                "hay que consumirla o revocarla antes de emitir otra "
                f"(MAX_ISSUED_APPROVALS_PER_ROOT = {MAX_ISSUED_APPROVALS_PER_ROOT}; §37.4)"
            )
        propuesta_seq = ledger.last_issued_approval_seq + 1
        max_evidencia = max(autoridad.max_issued_seq, autoridad.max_terminal_seq)
        if max_evidencia >= propuesta_seq:
            # §40.3: `proposed_seq = last_issued + 1`, así que un registro durable
            # en ESA seq ES la detección disparándose. No se «busca otra secuencia».
            raise ApprovalLedgerRetrocedidoError(
                f"la seq {propuesta_seq} ya tiene evidencia durable (max(H,T)={max_evidencia}) y el ledger "
                f"registra last_issued={ledger.last_issued_approval_seq}: LEDGER_RETROCEDIDO (§40.3)"
            )
        propuesta_id = str(approval_id_factory())
        _exigir_uuid_v4(propuesta_id, campo="approval_id", etiqueta="propuesta")
        if any(witness.approval_id == propuesta_id for witness in autoridad.witnesses) or any(
            tombstone.approval_id == propuesta_id for tombstone in autoridad.tombstones
        ):
            raise ApprovalMintingDefectError(
                f"el approval_id {propuesta_id} ya lo usa un registro durable: APPROVAL_ID_MINTING_DEFECT (§40.3)"
            )
        scope = construir_scope(
            operation=operation,
            source_activation=source_activation,
            generation_id=generation_id,
            clone_id=clone_id,
            clone_evidence=clone_evidence,
            runtime_setup_id=runtime_setup_id,
            runtime_setup_evidence=runtime_setup_evidence,
            compatibility_evidence_id=compatibility_evidence_id,
            candidate_id=candidate_id,
            approval_id=propuesta_id,
            approval_seq=propuesta_seq,
            approval_provenance=approval_provenance,
        )
        return PropuestaAprobacion(
            scope=scope,
            ledger_revision_at_proposal=ledger.approval_revision,
            proposed_at_ns=int(clock_ns()),
        )


# ---------------------------------------------------------------------------
# FASE C — emisión (burn: witness primero, caché después)
# ---------------------------------------------------------------------------


def emitir_aprobacion(
    root: pathlib.Path, propuesta: PropuestaAprobacion, consentimiento: ConsentimientoAprobacion
) -> None:
    """BLOQUEADA en P4-S4: no hay superficie HITL que autorice la emisión.

    ``APPROVAL_AUTHORITY = la superficie HITL del producto`` (§35.2/§36.5) y el
    producto **no la tiene**: la solicitud HITL es efímera y
    ``OWNER_IDENTITY_BINDING = DEFERRED``. Emitir desde código convertiría la
    aprobación en una auto-autorización, que es exactamente lo que
    ``SELF_ISSUED_APPROVAL_IS_AUTHORIZATION = NO`` prohíbe.

    El núcleo durable está implementado y probado en
    ``_emitir_aprobacion_bajo_lock`` con fixtures explícitas; esta frontera
    productiva permanece cerrada hasta que exista la superficie real.
    """
    raise ApprovalHitolNoDisponibleError(
        "P4-S4 no habilita la emisión productiva de aprobaciones: el producto no tiene superficie HITL que "
        "emita el consentimiento (§35.2/§36.5). Un lock, un booleano, un mock o un mensaje del agente LLM no "
        "son consentimiento humano. La autoridad durable está implementada y probada con fixtures explícitas; "
        "la entrada productiva sigue cerrada."
    )


def _validar_consentimiento(propuesta: PropuestaAprobacion, consentimiento: ConsentimientoAprobacion) -> None:
    """``WHAT_HUMAN_APPROVED == WHAT_WAS_ISSUED`` (§39.3 C2).

    Se comparan el digest y la procedencia EXACTOS que el propietario aprobó. Si
    no coinciden, no se emite: adaptar el scope para que «encaje» es el defecto
    que este chequeo impide.
    """
    if not isinstance(consentimiento, ConsentimientoAprobacion):
        raise ApprovalConsentimientoError("se esperaba un ConsentimientoAprobacion")
    digest_aprobado = _exigir_digest(
        consentimiento.approval_scope_digest, campo="approval_scope_digest", etiqueta="consentimiento"
    )
    if digest_aprobado != propuesta.scope.approval_scope_digest:
        raise ApprovalConsentimientoError(
            "el consentimiento no liga el scope propuesto: WHAT_HUMAN_APPROVED != WHAT_WAS_ISSUED "
            f"({digest_aprobado} != {propuesta.scope.approval_scope_digest})"
        )
    procedencia = _exigir_texto(
        consentimiento.approval_provenance, campo="approval_provenance", etiqueta="consentimiento"
    )
    if procedencia != propuesta.scope.approval_provenance:
        raise ApprovalConsentimientoError(
            "la procedencia aprobada difiere de la del scope propuesto: no se publica un scope distinto del "
            "aprobado (§39.6)"
        )
    _exigir_texto(consentimiento.surface, campo="surface", etiqueta="consentimiento")


def _emitir_aprobacion_bajo_lock(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    propuesta: PropuestaAprobacion,
    consentimiento: ConsentimientoAprobacion,
) -> ApprovalLedger:
    """FASE C del ``ISSUE`` (§39.3): el ``CREATE`` del witness ES la emisión.

    Orden: verificar consentimiento → revalidar la autoridad durable bajo el lock
    → ``CREATE`` del witness (no-clobber) → CAS de la caché. Un fallo del CAS
    DESPUÉS del witness NO se descarta: la emisión ya ocurrió y se reconcilia
    hacia adelante.
    """
    handle.assert_owned()
    _exigir_dirs_store(root)
    scope = validar_scope(propuesta.scope)
    _validar_consentimiento(propuesta, consentimiento)
    actual = cargar_ledger_aprobacion(root)
    if actual.disposition is ApprovalLedgerDisposition.CORRUPT:
        raise ApprovalCorruptError(f"{actual.path}: ledger corrupto: no se emite (fail-closed)")
    autoridad = derivar_autoridad_aprobacion(root)
    veredicto, motivo = clasificar_autoridad(actual, autoridad)
    if veredicto is not VeredictoAutoridad.CONSISTENT and veredicto is not VeredictoAutoridad.GENESIS:
        raise ApprovalLedgerRetrocedidoError(
            f"la autoridad durable está en {veredicto.value} ({motivo}): la propuesta se DESCARTA"
        )
    ledger = actual.ledger if actual.ledger is not None else _ledger_genesis(root)
    # La revisión esperada es la de la PROPUESTA (A1), no una recién leída: releerla
    # sería ABA y dejaría pasar una propuesta emitida sobre un ledger restaurado.
    if ledger.approval_revision != propuesta.ledger_revision_at_proposal:
        raise ApprovalCasError(
            f"approval_revision={ledger.approval_revision} != la de la propuesta "
            f"({propuesta.ledger_revision_at_proposal}): la propuesta se DESCARTA y se re-presenta"
        )
    if ledger.state is ApprovalLifecycle.ISSUED:
        raise ApprovalMaxIssuedError(
            f"ya existe una aprobación ISSUED (seq {ledger.last_issued_approval_seq}): MAX_ISSUED = "
            f"{MAX_ISSUED_APPROVALS_PER_ROOT} (§37.4)"
        )
    if ledger.last_issued_approval_seq != scope.approval_seq - 1:
        raise ApprovalCasError(
            f"last_issued_approval_seq={ledger.last_issued_approval_seq} != proposed_seq-1 "
            f"({scope.approval_seq - 1}): la propuesta se DESCARTA"
        )
    if autoridad.max_terminal_seq > scope.approval_seq - 1:
        raise ApprovalLedgerRetrocedidoError(
            f"hay un tombstone en seq {autoridad.max_terminal_seq} >= proposed_seq {scope.approval_seq}: R2"
        )
    witness = IssuanceWitness(
        schema_version=ISSUANCE_WITNESS_SCHEMA_VERSION,
        root_id=_root_id(root),
        approval_seq=scope.approval_seq,
        approval_id=scope.approval_id,
        approval_scope_digest=scope.approval_scope_digest,
        approval_provenance=scope.approval_provenance,
        issued_at_ns=propuesta.proposed_at_ns,
        ledger_revision_at_issue=propuesta.ledger_revision_at_proposal,
    )
    _crear_witness(handle, root, witness, ledger=ledger)
    nuevo = dataclasses.replace(
        ledger,
        last_issued_approval_seq=scope.approval_seq,
        last_issued_approval_id=scope.approval_id,
        last_issued_scope_digest=scope.approval_scope_digest,
        last_issued_provenance=scope.approval_provenance,
        state=ApprovalLifecycle.ISSUED,
        issued_at_ns=propuesta.proposed_at_ns,
        consumed_at_ns=None,
        revoked_at_ns=None,
        approval_terminal_store=None,
    )
    try:
        return _escribir_ledger_cas(handle, root, esperado=propuesta.ledger_revision_at_proposal, nuevo=nuevo)
    except ApprovalCasError:
        # Post-witness: la emisión YA ocurrió (el witness es la autoridad). No se
        # descarta, no se reasigna la seq y no se publica otro scope: se reconcilia.
        reconciliado = _reconciliar_hacia_adelante(handle, root)
        if reconciliado is None:
            raise ApprovalLedgerRetrocedidoError(
                "el CAS falló tras crear el witness y la autoridad durable no permite reconciliar hacia "
                "adelante: FAIL_CLOSED · RECOVERY (§39.3 C4)"
            ) from None
        logger.warning(
            "emisión %s reconciliada hacia adelante tras fallo del CAS: el witness es la autoridad",
            scope.approval_seq,
        )
        return reconciliado


def _crear_witness(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    witness: IssuanceWitness,
    *,
    ledger: ApprovalLedger,
) -> IssuanceWitness:
    """``CREATE`` no-clobber del witness; clasifica la colisión (§40.2/§40.3)."""
    handle.assert_owned()
    ruta = issuances_state_dir(root) / _nombre_witness(witness.approval_seq)
    try:
        _crear_json_inmutable(ruta, _serializar_witness(witness))
    except FileExistsError:
        existente = _parsear_witness(
            _leer_json(ruta), ruta, seq_esperada=witness.approval_seq, root_id_esperado=_root_id(root)
        )
        if existente == witness:
            # Reintento idempotente del MISMO contenido: el CREATE se considera cumplido.
            return existente
        if ledger.last_issued_approval_seq >= witness.approval_seq:
            raise ApprovalCarreraNormalError(
                f"otra emisión legítima ganó la seq {witness.approval_seq} y el ledger ya la conoce: "
                "CARRERA_NORMAL ⇒ descartar y re-presentar (§40.3)"
            ) from None
        raise ApprovalLedgerRetrocedidoError(
            f"la seq {witness.approval_seq} ya tiene witness y el ledger NO la conoce: LEDGER_RETROCEDIDO (§40.2 C2)"
        ) from None
    return witness


# ---------------------------------------------------------------------------
# CONSUME / REVOKE (burn-first: validar → tombstone → caché)
# ---------------------------------------------------------------------------


def _exigir_evidencia_terminal_previa(
    autoridad: AutoridadAprobacion,
    *,
    approval_seq: int,
    approval_id: str,
    terminal_state: TerminalState,
    transition_id: str | None,
) -> ApprovalTombstone | None:
    """Reintento idempotente vs conflicto (§38.2: mismo contenido ⇒ cumplido)."""
    existente = _tombstone_de(autoridad, approval_seq, approval_id)
    if existente is None:
        return None
    if existente.terminal_state is not terminal_state:
        raise ApprovalIdentityError(
            f"ya existe un tombstone {existente.terminal_state.value} para {approval_seq}-{approval_id}: "
            "no hay transición CONSUMED→REVOKED ni al revés (§38.2)"
        )
    if terminal_state is TerminalState.CONSUMED and existente.consumed_by_transition_id != transition_id:
        raise ApprovalIdentityError(
            f"la aprobación {approval_seq} ya fue consumida por la transición "
            f"{existente.consumed_by_transition_id}, no por {transition_id}: la aprobación es single-use (§35.2)"
        )
    return existente


def _crear_tombstone(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    tombstone: ApprovalTombstone,
) -> pathlib.Path:
    """``[1]`` — write-ahead: la evidencia terminal ANTES de la caché (§38.2)."""
    handle.assert_owned()
    ruta = approvals_state_dir(root) / _nombre_tombstone(tombstone.approval_seq, tombstone.approval_id)
    try:
        _crear_json_inmutable(ruta, _serializar_tombstone(tombstone))
    except FileExistsError:
        existente = _parsear_tombstone(
            _leer_json(ruta),
            ruta,
            seq_esperada=tombstone.approval_seq,
            id_esperado=tombstone.approval_id,
        )
        if existente != tombstone:
            raise ApprovalIdentityError(
                f"{ruta}: el tombstone ya existe con otro contenido: no se sobrescribe (§38.2)"
            ) from None
    return ruta


def consumir_aprobacion(
    root: pathlib.Path,
    *,
    approval_id: str,
    approval_seq: int,
    approval_scope_digest: str,
    transition_id: str,
    timeout: float = 0.0,
    clock_ns: Callable[[], int] = time.time_ns,
) -> ResultadoConsumo:
    """``CONSUME`` — quema la aprobación ANTES de crear la transición (§37.2).

    Protocolo: **[V] validar completamente → [1] tombstone CONSUMED → [2] CAS de la
    caché**. La validación precede al tombstone: un request stale o malformado no
    puede terminalizar una aprobación válida ni dejar el root fail-closed para
    siempre (§37.2/R11.2).

    Es una entrada PRODUCTIVA de la maquinaria de transición, no una
    autorización: exige una aprobación ``ISSUED`` que sólo la superficie HITL
    podría emitir. Sin emisión previa falla cerrado.
    """
    ruta = pathlib.Path(root)
    _exigir_uuid_v4(approval_id, campo="approval_id", etiqueta="consumo")
    _exigir_entero(approval_seq, campo="approval_seq", etiqueta="consumo", minimo=1)
    _exigir_digest(approval_scope_digest, campo="approval_scope_digest", etiqueta="consumo")
    _exigir_uuid_v4(transition_id, campo="transition_id", etiqueta="consumo")
    with participant_lock(ruta, timeout=timeout) as handle:
        handle.assert_owned()
        _exigir_dirs_store(ruta)
        actual = cargar_ledger_aprobacion(ruta)
        if actual.disposition is ApprovalLedgerDisposition.CORRUPT:
            raise ApprovalCorruptError(f"{actual.path}: ledger corrupto: no se consume (fail-closed)")
        autoridad = derivar_autoridad_aprobacion(ruta)
        veredicto, motivo = clasificar_autoridad(actual, autoridad)
        # GENESIS se deja pasar a propósito: «sin ledger y sin evidencia durable»
        # no es un retroceso, es que nunca se emitió nada. Reportarlo como
        # LEDGER_RETROCEDIDO fabricaría una alarma de rollback y mandaría al
        # operador a buscar una pérdida de datos que no existe (el mismo criterio
        # que ``proponer_aprobacion``/``_emitir_aprobacion_bajo_lock``, §39.3 A5).
        # El diagnóstico honesto lo da el branch de abajo: no hay nada que
        # consumir. Cualquier otro veredicto no-CONSISTENT sí es FAIL_CLOSED.
        if veredicto is not VeredictoAutoridad.CONSISTENT and veredicto is not VeredictoAutoridad.GENESIS:
            raise ApprovalLedgerRetrocedidoError(
                f"no se consume con la autoridad durable en {veredicto.value}: {motivo}"
            )
        ledger = actual.ledger
        if ledger is None or ledger.state is None:
            raise ApprovalIdentityError("no hay ninguna aprobación emitida para consumir (ledger ausente)")
        # [V] validación completa, campo por campo, contra DOS fuentes
        # independientes: el scope PRESENTADO y el scope REGISTRADO AL EMITIR.
        # Corre ANTES del branch de estado: el reintento idempotente tiene que
        # verificar la identidad igual que el consumo nuevo, o un reintento con
        # otro transition_id se colaría como «ya consumido».
        if approval_seq != ledger.last_issued_approval_seq:
            raise ApprovalIdentityError(
                f"approval_seq {approval_seq} != last_issued_approval_seq {ledger.last_issued_approval_seq}"
            )
        if approval_id != ledger.last_issued_approval_id:
            raise ApprovalIdentityError(
                f"approval_id {approval_id} != last_issued_approval_id {ledger.last_issued_approval_id}"
            )
        if approval_scope_digest != ledger.last_issued_scope_digest:
            raise ApprovalIdentityError(
                "el approval_scope_digest presentado no coincide con el registrado al emitir: el scope fue "
                "adulterado (§37.3 anti-tautología)"
            )
        witness = next(
            (item for item in autoridad.witnesses if item.approval_seq == approval_seq),
            None,
        )
        if witness is None:
            raise ApprovalIdentityError(f"no hay witness de emisión para la seq {approval_seq}")
        if (
            witness.approval_id != approval_id
            or witness.approval_scope_digest != approval_scope_digest
            or witness.approval_provenance != ledger.last_issued_provenance
        ):
            raise ApprovalIdentityError(
                f"el witness de la seq {approval_seq} no coincide con la aprobación presentada: FAIL_CLOSED"
            )
        if ledger.state is ApprovalLifecycle.CONSUMED:
            # REINTENTO IDEMPOTENTE: el consumo YA ocurrió y la respuesta se
            # perdió. Sólo es idempotente para la MISMA transición; otra
            # transición con la misma aprobación es un replay (§35.2 single-use).
            previo = _exigir_evidencia_terminal_previa(
                autoridad,
                approval_seq=approval_seq,
                approval_id=approval_id,
                terminal_state=TerminalState.CONSUMED,
                transition_id=transition_id,
            )
            if previo is None or previo.approval_scope_digest != approval_scope_digest:
                raise ApprovalIdentityError(
                    f"el ledger declara CONSUMED la aprobación {approval_seq} sin evidencia terminal "
                    "coherente: FAIL_CLOSED (write-ahead violado)"
                )
            return ResultadoConsumo(
                approval_id=approval_id,
                approval_seq=approval_seq,
                scope_digest=approval_scope_digest,
                consumed_by_transition_id=transition_id,
                terminal_path=approvals_state_dir(ruta) / _nombre_tombstone(approval_seq, approval_id),
                cache_reconciled=False,
            )
        if ledger.state is not ApprovalLifecycle.ISSUED:
            raise ApprovalIdentityError(
                f"el estado de la aprobación es {ledger.state.value}, no ISSUED: una aprobación terminal no se "
                "consume ni se reutiliza (§37.4)"
            )
        previo = _exigir_evidencia_terminal_previa(
            autoridad,
            approval_seq=approval_seq,
            approval_id=approval_id,
            terminal_state=TerminalState.CONSUMED,
            transition_id=transition_id,
        )
        if previo is not None:
            # Reintento idempotente: el tombstone ya existe con el MISMO contenido.
            reconciliado = _reconciliar_hacia_adelante(handle, ruta)
            return ResultadoConsumo(
                approval_id=approval_id,
                approval_seq=approval_seq,
                scope_digest=approval_scope_digest,
                consumed_by_transition_id=transition_id,
                terminal_path=approvals_state_dir(ruta) / _nombre_tombstone(approval_seq, approval_id),
                cache_reconciled=reconciliado is not None and reconciliado.state is ApprovalLifecycle.CONSUMED,
            )
        tombstone = ApprovalTombstone(
            schema_version=TERMINAL_TOMBSTONE_SCHEMA_VERSION,
            approval_id=approval_id,
            approval_seq=approval_seq,
            approval_scope_digest=approval_scope_digest,
            terminal_state=TerminalState.CONSUMED,
            consumed_by_transition_id=transition_id,
            revoked_reason=None,
            terminal_at_ns=int(clock_ns()),
        )
        terminal_path = _crear_tombstone(handle, ruta, tombstone)
        nuevo = dataclasses.replace(
            ledger,
            state=ApprovalLifecycle.CONSUMED,
            last_consumed_approval_seq=approval_seq,
            consumed_approval_id=approval_id,
            consumed_scope_digest=approval_scope_digest,
            consumed_by_transition_id=transition_id,
            consumed_at_ns=tombstone.terminal_at_ns,
            approval_terminal_store=f"state/approvals/{terminal_path.name}",
        )
        try:
            _escribir_ledger_cas(handle, ruta, esperado=ledger.approval_revision, nuevo=nuevo)
            cache_reconciled = False
        except ApprovalCasError:
            # [2] falló tras [1]: el tombstone MANDA. Se reconcilia la caché hacia
            # adelante y NUNCA se borra la evidencia terminal.
            cache_reconciled = _reconciliar_hacia_adelante(handle, ruta) is not None
            if not cache_reconciled:
                raise ApprovalLedgerRetrocedidoError(
                    "el consumo se persistió (tombstone) y la caché no pudo reconciliarse: la aprobación ES "
                    "terminal; FAIL_CLOSED · RECOVERY (§38.2)"
                ) from None
            logger.warning(
                "consumo de %s reconciliado hacia adelante tras fallo del CAS: el tombstone es la autoridad",
                approval_seq,
            )
        return ResultadoConsumo(
            approval_id=approval_id,
            approval_seq=approval_seq,
            scope_digest=approval_scope_digest,
            consumed_by_transition_id=transition_id,
            terminal_path=terminal_path,
            cache_reconciled=cache_reconciled,
        )


def revocar_aprobacion(
    root: pathlib.Path,
    *,
    approval_id: str,
    approval_seq: int,
    approval_scope_digest: str,
    motivo: str,
    timeout: float = 0.0,
    clock_ns: Callable[[], int] = time.time_ns,
) -> pathlib.Path:
    """``REVOKE`` — cancela una aprobación ``ISSUED`` antes de su consumo (§37.4).

    Mismo protocolo que ``CONSUME``: validar → tombstone REVOKED → CAS. Una
    aprobación revocada no se consume ni se reutiliza, y deja evidencia terminal
    inmutable: sin tombstone la revocación no era demostrable tras un rollback del
    ledger (D0-R4.1).
    """
    ruta = pathlib.Path(root)
    _exigir_uuid_v4(approval_id, campo="approval_id", etiqueta="revocación")
    _exigir_entero(approval_seq, campo="approval_seq", etiqueta="revocación", minimo=1)
    _exigir_digest(approval_scope_digest, campo="approval_scope_digest", etiqueta="revocación")
    _exigir_texto(motivo, campo="motivo", etiqueta="revocación")
    with participant_lock(ruta, timeout=timeout) as handle:
        handle.assert_owned()
        _exigir_dirs_store(ruta)
        actual = cargar_ledger_aprobacion(ruta)
        if actual.disposition is ApprovalLedgerDisposition.CORRUPT:
            raise ApprovalCorruptError(f"{actual.path}: ledger corrupto: no se revoca (fail-closed)")
        autoridad = derivar_autoridad_aprobacion(ruta)
        veredicto, detalle = clasificar_autoridad(actual, autoridad)
        # GENESIS no es un retroceso: es un root donde nunca se emitió nada. Se
        # deja pasar para que el branch de abajo diga «el estado es ausente»
        # (§37.4) en vez de inventar un LEDGER_RETROCEDIDO que apunte a una
        # pérdida de datos inexistente. Mismo criterio que la emisión.
        if veredicto is not VeredictoAutoridad.CONSISTENT and veredicto is not VeredictoAutoridad.GENESIS:
            raise ApprovalLedgerRetrocedidoError(
                f"no se revoca con la autoridad durable en {veredicto.value}: {detalle}"
            )
        ledger = actual.ledger
        if ledger is None or ledger.state is not ApprovalLifecycle.ISSUED:
            estado = "ausente" if ledger is None or ledger.state is None else ledger.state.value
            raise ApprovalIdentityError(
                f"sólo se revoca una aprobación ISSUED; el estado es {estado} (CANCEL TRAS CREATE no revoca: "
                "la transición gobierna, §37.4)"
            )
        if approval_seq != ledger.last_issued_approval_seq or approval_id != ledger.last_issued_approval_id:
            raise ApprovalIdentityError("la aprobación a revocar no es la emitida vigente")
        if approval_scope_digest != ledger.last_issued_scope_digest:
            raise ApprovalIdentityError("el approval_scope_digest presentado no coincide con el emitido")
        witness = next((item for item in autoridad.witnesses if item.approval_seq == approval_seq), None)
        if (
            witness is None
            or witness.approval_id != approval_id
            or witness.approval_scope_digest != approval_scope_digest
        ):
            raise ApprovalIdentityError(
                f"el witness de la seq {approval_seq} no coincide con la aprobación a revocar: FAIL_CLOSED"
            )
        previo = _exigir_evidencia_terminal_previa(
            autoridad,
            approval_seq=approval_seq,
            approval_id=approval_id,
            terminal_state=TerminalState.REVOKED,
            transition_id=None,
        )
        if previo is not None:
            _reconciliar_hacia_adelante(handle, ruta)
            return approvals_state_dir(ruta) / _nombre_tombstone(approval_seq, approval_id)
        tombstone = ApprovalTombstone(
            schema_version=TERMINAL_TOMBSTONE_SCHEMA_VERSION,
            approval_id=approval_id,
            approval_seq=approval_seq,
            approval_scope_digest=approval_scope_digest,
            terminal_state=TerminalState.REVOKED,
            consumed_by_transition_id=None,
            revoked_reason=motivo,
            terminal_at_ns=int(clock_ns()),
        )
        terminal_path = _crear_tombstone(handle, ruta, tombstone)
        nuevo = dataclasses.replace(
            ledger,
            state=ApprovalLifecycle.REVOKED,
            revoked_at_ns=tombstone.terminal_at_ns,
            approval_terminal_store=f"state/approvals/{terminal_path.name}",
        )
        try:
            _escribir_ledger_cas(handle, ruta, esperado=ledger.approval_revision, nuevo=nuevo)
        except ApprovalCasError:
            if _reconciliar_hacia_adelante(handle, ruta) is None:
                raise ApprovalLedgerRetrocedidoError(
                    "la revocación se persistió (tombstone) y la caché no pudo reconciliarse: la aprobación ES "
                    "terminal; FAIL_CLOSED · RECOVERY (§38.2)"
                ) from None
            logger.warning(
                "revocación de %s reconciliada hacia adelante tras fallo del CAS: el tombstone es la autoridad",
                approval_seq,
            )
        return terminal_path


# ---------------------------------------------------------------------------
# Verificación (read-only) y reconciliación
# ---------------------------------------------------------------------------


def verificar_consumo_aprobacion(
    root: pathlib.Path,
    *,
    approval_id: str,
    approval_seq: int,
    approval_scope_digest: str,
    transition_id: str,
) -> AprobacionConsumida:
    """Cross-check de LECTURA que el ``CREATE`` del journal exige (§36.6).

    No consume y no muta: verifica que exista evidencia terminal ``CONSUMED``
    inmutable para esa aprobación, que el digest coincida y que la transición
    consumidora sea exactamente la que va a crear el PENDING. Es el puente
    P4-S3 ↔ P4-S4: la autoridad durable existe, la transición todavía no se
    habilita.
    """
    ruta = pathlib.Path(root)
    autoridad = derivar_autoridad_aprobacion(ruta)
    tombstone = _tombstone_de(autoridad, approval_seq, approval_id)
    if tombstone is None:
        raise ApprovalIdentityError(
            f"no hay evidencia terminal para la aprobación {approval_seq}-{approval_id}: "
            "PENDING_WITHOUT_APPROVAL_PROOF = PROHIBIDO (§36.6)"
        )
    if tombstone.terminal_state is not TerminalState.CONSUMED:
        raise ApprovalIdentityError(
            f"la aprobación {approval_seq} está {tombstone.terminal_state.value}, no CONSUMED: no autoriza nada"
        )
    if tombstone.approval_scope_digest != approval_scope_digest:
        raise ApprovalIdentityError("el scope consumido difiere del approval_scope_digest de la propuesta")
    if tombstone.consumed_by_transition_id != transition_id:
        raise ApprovalIdentityError(
            f"la aprobación fue consumida por la transición {tombstone.consumed_by_transition_id}, "
            f"no por {transition_id}: no puede crear una segunda transición (§35.2)"
        )
    return AprobacionConsumida(
        approval_id=tombstone.approval_id,
        approval_seq=tombstone.approval_seq,
        scope_digest=tombstone.approval_scope_digest,
        consumed_by_transition_id=transition_id,
    )


def reconciliar_aprobacion(root: pathlib.Path, *, timeout: float = 0.0) -> ReconciliacionAprobacion:
    """Reconciliación de arranque de la autoridad durable (§40.2/§45.10).

    Bajo el root lock: clasifica la coherencia caché ↔ evidencia durable y, si el
    veredicto es REPARABLE, reconstruye la caché **hacia adelante** desde los
    stores inmutables. Nunca borra evidencia, nunca resuelve a favor del ledger y
    nunca «resucita» un token.
    """
    ruta = pathlib.Path(root)
    with participant_lock(ruta, timeout=timeout) as handle:
        handle.assert_owned()
        _exigir_dirs_store(ruta)
        actual = cargar_ledger_aprobacion(ruta)
        autoridad = derivar_autoridad_aprobacion(ruta)
        veredicto, motivo = clasificar_autoridad(actual, autoridad)
        if veredicto not in _VEREDICTOS_REPARABLES:
            return ReconciliacionAprobacion(
                veredicto=veredicto,
                motivo=motivo,
                ledger=actual.ledger,
                autoridad=autoridad,
                cache_reconstruida=False,
            )
        revision = actual.ledger.approval_revision if actual.ledger is not None else 0
        reconstruido = _reconstruir_ledger(ruta, autoridad, revision_actual=revision)
        _escribir_ledger(handle, ruta, reconstruido)
        return ReconciliacionAprobacion(
            veredicto=veredicto,
            motivo=motivo,
            ledger=reconstruido,
            autoridad=autoridad,
            cache_reconstruida=True,
        )
