"""Journal transaccional autoritativo de GP2 — modelo puro (GP2-S4A, ADR 0010 §19).

El journal vive en ``AUTHORIZED_OPERATIONS`` (``operations/<operation_id>/``) y
es, desde que empieza la fase S4, la FUENTE DE ESTADO de la transacción:
``UNTRUSTED_STAGING`` no puede comitear, revertir, recuperar ni overridear lo que
el journal afirma (§29).

Este módulo es PURO (sin I/O, sin Win32): define el esquema cerrado y versionado
de los registros, la FSM autoritativa con su tabla cerrada de transiciones, la
máquina de estados por nodo (WAL) y la reproducción (replay) validada. La
durabilidad —``WriteFile`` + ``FlushFileBuffers`` verificado como GATE— vive en
:mod:`protection_journal_store`.

Estados: NO se inventan. Se extraen literalmente del diagrama y las notas de
ADR 0010 §19.2 (incluidas las transiciones a ``INDETERMINATE``), y la tabla
completa se congela por igualdad literal en los tests: una transición no
declarada rompe el test.

WAL por nodo: la propiedad esencial de S4 (§33/§34) es que ``MUTATING(K)`` debe
existir DURABLEMENTE antes de que ninguna implementación futura toque K. Acá se
modela la máquina de estados que lo permite y PROHIBE las regresiones
(``MUTATED -> MUTATING``, ``COMMITTED -> APPLYING``, ``ROLLED_BACK -> APPLYING``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sky_claw.local.runtime_vault.models import RuntimeVaultError

# ============================================================================
# Constantes normativas
# ============================================================================

#: Versión del schema del journal. Un cambio de schema exige versión nueva.
PROTECTION_JOURNAL_SCHEMA_VERSION = "1.0"

#: Nombre normativo del archivo de journal dentro de ``operations/<op_id>/``.
PROTECTION_JOURNAL_FILE_NAME = "protection_journal.json"

#: ``object_name`` de DACL del namespace protegido (§11.3).
PROTECTION_JOURNAL_OBJECT_NAME = PROTECTION_JOURNAL_FILE_NAME

_MAX_UINT64 = (1 << 64) - 1
_MAX_UINT128 = (1 << 128) - 1
_HEX_LOWER = frozenset("0123456789abcdef")

#: Estado transaccional autoritativo INICIAL del journal de S4: el helper elevado
#: adquiere el ``GoldenMutationLock`` y escribe el journal en ``APPLYING`` con
#: CERO nodos en ``MUTATING`` (§19.2 "Dos fases del FSM"; fila C3 de §20). La
#: mera promoción del plan NO escribe ``MUTATING(K)`` (§54).
INITIAL_TRANSACTION_STATE = "applying"


# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class ProtectionJournalError(RuntimeVaultError):
    """Base de excepciones del journal transaccional."""


class ProtectionJournalSchemaError(ProtectionJournalError):
    """Violación del esquema cerrado, del binding o de la monotonía: fail-closed."""


class ProtectionJournalIndeterminateError(ProtectionJournalError):
    """Evidencia durable ambigua (escritura cortada): clasificación INDETERMINATE.

    Corresponde a la fila C4b de ADR 0010 §20: ``write()`` ejecutado pero
    ``FlushFileBuffers`` no completado. Nunca se interpreta como "la operación
    nunca comenzó" ni como éxito.
    """


class ProtectionJournalTransitionError(ProtectionJournalSchemaError):
    """Transición de FSM o de nodo no declarada en las tablas cerradas."""


class PrematureCommitError(ProtectionJournalError):
    """Intento de declarar ``COMMITTED`` sin los gates de S4-B/C (§32).

    ``COMMITTED`` exige GP1 ``HARDENED``, RV-2 ``VERIFIED``, igualdad literal del
    NodeSet y ``ARCHIVING_BACKUP`` flushado. Ninguno de esos gates existe en S4-A,
    de modo que la API productiva de este slice lo rechaza siempre.
    """


# ============================================================================
# FSM autoritativa (ADR 0010 §19.2)
# ============================================================================


class ProtectionTransactionState(StrEnum):
    """Estados del FSM autoritativo de GP2, exactamente como los declara §19.2."""

    PREPARING = "preparing"
    PREPARED = "prepared"
    AWAITING_ELEVATION = "awaiting_elevation"
    APPLYING = "applying"
    VERIFYING_GP1 = "verifying_gp1"
    VERIFYING_RV2 = "verifying_rv2"
    VERIFYING_NODE_SET = "verifying_node_set"
    ARCHIVING_BACKUP = "archiving_backup"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    ELEVATION_REJECTED = "elevation_rejected"
    REFUSE_TO_APPLY = "refuse_to_apply"
    REFUSE_TO_PLAN = "refuse_to_plan"
    ROLLBACK_REQUIRED = "rollback_required"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    INDETERMINATE = "indeterminate"


#: Tabla CERRADA de transiciones del FSM (§19.2). Congelada por igualdad literal
#: en los tests: una transición no declarada aquí es inválida por construcción.
TRANSACTION_TRANSITIONS: dict[ProtectionTransactionState, frozenset[ProtectionTransactionState]] = {
    ProtectionTransactionState.PREPARING: frozenset(
        {
            ProtectionTransactionState.PREPARED,
            ProtectionTransactionState.REFUSE_TO_APPLY,
            ProtectionTransactionState.CANCELLED,
        }
    ),
    ProtectionTransactionState.PREPARED: frozenset(
        {
            ProtectionTransactionState.AWAITING_ELEVATION,
            ProtectionTransactionState.REFUSE_TO_APPLY,
            ProtectionTransactionState.CANCELLED,
        }
    ),
    ProtectionTransactionState.AWAITING_ELEVATION: frozenset(
        {
            ProtectionTransactionState.APPLYING,
            ProtectionTransactionState.REFUSE_TO_PLAN,
            ProtectionTransactionState.ELEVATION_REJECTED,
            ProtectionTransactionState.CANCELLED,
        }
    ),
    ProtectionTransactionState.APPLYING: frozenset(
        {
            ProtectionTransactionState.VERIFYING_GP1,
            ProtectionTransactionState.ROLLBACK_REQUIRED,
            ProtectionTransactionState.INDETERMINATE,
        }
    ),
    ProtectionTransactionState.VERIFYING_GP1: frozenset(
        {
            ProtectionTransactionState.VERIFYING_RV2,
            ProtectionTransactionState.ROLLBACK_REQUIRED,
            ProtectionTransactionState.INDETERMINATE,
        }
    ),
    ProtectionTransactionState.VERIFYING_RV2: frozenset(
        {
            ProtectionTransactionState.VERIFYING_NODE_SET,
            ProtectionTransactionState.ROLLBACK_REQUIRED,
            ProtectionTransactionState.INDETERMINATE,
        }
    ),
    ProtectionTransactionState.VERIFYING_NODE_SET: frozenset(
        {
            ProtectionTransactionState.ARCHIVING_BACKUP,
            ProtectionTransactionState.ROLLBACK_REQUIRED,
            ProtectionTransactionState.INDETERMINATE,
        }
    ),
    ProtectionTransactionState.ARCHIVING_BACKUP: frozenset(
        {
            ProtectionTransactionState.COMMITTED,
            ProtectionTransactionState.ROLLBACK_REQUIRED,
        }
    ),
    ProtectionTransactionState.ROLLBACK_REQUIRED: frozenset(
        {
            ProtectionTransactionState.ROLLING_BACK,
            ProtectionTransactionState.INDETERMINATE,
        }
    ),
    ProtectionTransactionState.ROLLING_BACK: frozenset(
        {
            ProtectionTransactionState.ROLLED_BACK,
            ProtectionTransactionState.ROLLBACK_FAILED,
            ProtectionTransactionState.INDETERMINATE,
        }
    ),
    ProtectionTransactionState.COMMITTED: frozenset(),
    ProtectionTransactionState.CANCELLED: frozenset(),
    ProtectionTransactionState.ELEVATION_REJECTED: frozenset(),
    ProtectionTransactionState.REFUSE_TO_APPLY: frozenset(),
    ProtectionTransactionState.REFUSE_TO_PLAN: frozenset(),
    ProtectionTransactionState.ROLLED_BACK: frozenset(),
    ProtectionTransactionState.ROLLBACK_FAILED: frozenset(),
    ProtectionTransactionState.INDETERMINATE: frozenset(),
}

#: Estados terminales: sin transiciones salientes.
TERMINAL_TRANSACTION_STATES: frozenset[ProtectionTransactionState] = frozenset(
    state for state, targets in TRANSACTION_TRANSITIONS.items() if not targets
)

#: Destinos que la API productiva de S4-A puede escribir. El camino de éxito
#: (``VERIFYING_*``/``ARCHIVING_BACKUP``/``COMMITTED``) pertenece a S4-B/C y se
#: rechaza acá: declararlo sin ejecutar sus gates sería afirmar una post-
#: verificación que no ocurrió (§32).
S4A_PERMITTED_TRANSITION_TARGETS: frozenset[ProtectionTransactionState] = frozenset(
    {
        ProtectionTransactionState.ROLLBACK_REQUIRED,
        ProtectionTransactionState.ROLLING_BACK,
        ProtectionTransactionState.ROLLED_BACK,
        ProtectionTransactionState.ROLLBACK_FAILED,
        ProtectionTransactionState.INDETERMINATE,
    }
)


# ============================================================================
# WAL por nodo
# ============================================================================


class NodeWalState(StrEnum):
    """Estados del WAL por nodo. ``MUTATING`` es intención durable, no mutación."""

    MUTATING = "mutating"
    MUTATED = "mutated"


#: Tabla CERRADA de transiciones por nodo. Sin registro -> ``MUTATING`` ->
#: ``MUTATED``. Cualquier otra arista es una regresión inválida (§39): no existe
#: ``MUTATED -> MUTATING`` (re-intentar un nodo ya mutado) ni ``MUTATING`` repetido.
NODE_WAL_TRANSITIONS: dict[NodeWalState | None, frozenset[NodeWalState]] = {
    None: frozenset({NodeWalState.MUTATING}),
    NodeWalState.MUTATING: frozenset({NodeWalState.MUTATED}),
    NodeWalState.MUTATED: frozenset(),
}


class JournalRecordKind(StrEnum):
    """Tipos de registro del journal (esquema cerrado)."""

    HEADER = "journal_header"
    TRANSACTION = "transaction_state"
    NODE = "node_state"


#: Claves exactas de cada tipo de registro.
HEADER_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "kind",
        "schema_version",
        "sequence",
        "operation_id",
        "authorized_plan_digest",
        "canonical_root",
        "volume_serial_number",
        "root_file_id",
        "created_at",
    }
)
TRANSACTION_RECORD_KEYS: frozenset[str] = frozenset({"kind", "sequence", "state"})
NODE_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "kind",
        "sequence",
        "relative_path",
        "volume_serial_number",
        "file_id",
        "pre_sd_sha256",
        "state",
    }
)


# ============================================================================
# Registros inmutables
# ============================================================================


@dataclass(frozen=True, slots=True)
class JournalHeaderRecord:
    """Primer registro del journal: liga la operación al plan durable ya existente."""

    schema_version: str
    sequence: int
    operation_id: str
    authorized_plan_digest: str
    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    created_at: str

    def __post_init__(self) -> None:
        # Validación por construcción: un registro inválido no puede existir en
        # memoria, de modo que ningún camino de código puede sembrar el journal
        # con evidencia malformada (la monotonía se valida en el replay).
        if self.schema_version != PROTECTION_JOURNAL_SCHEMA_VERSION:
            raise ProtectionJournalSchemaError(
                f"schema_version debe ser '{PROTECTION_JOURNAL_SCHEMA_VERSION}'; observado '{self.schema_version}'"
            )
        _validate_sequence(self.sequence)
        object.__setattr__(self, "operation_id", _validate_operation_id(self.operation_id))
        object.__setattr__(
            self, "authorized_plan_digest", _validate_sha256_hex(self.authorized_plan_digest, "authorized_plan_digest")
        )
        object.__setattr__(self, "canonical_root", _validate_nonempty_text(self.canonical_root, "canonical_root"))
        object.__setattr__(
            self, "volume_serial_number", _validate_uint(self.volume_serial_number, "volume_serial_number", _MAX_UINT64)
        )
        object.__setattr__(self, "root_file_id", _validate_uint(self.root_file_id, "root_file_id", _MAX_UINT128))
        object.__setattr__(self, "created_at", _validate_nonempty_text(self.created_at, "created_at"))

    @property
    def kind(self) -> JournalRecordKind:
        return JournalRecordKind.HEADER


@dataclass(frozen=True, slots=True)
class JournalTransactionRecord:
    """Transición del estado transaccional autoritativo."""

    sequence: int
    state: ProtectionTransactionState

    def __post_init__(self) -> None:
        _validate_sequence(self.sequence)
        if not isinstance(self.state, ProtectionTransactionState):
            raise ProtectionJournalSchemaError("state debe ser un miembro de ProtectionTransactionState")

    @property
    def kind(self) -> JournalRecordKind:
        return JournalRecordKind.TRANSACTION


@dataclass(frozen=True, slots=True)
class JournalNodeRecord:
    """Transición del WAL de UN nodo, ligada a su identidad física exacta (§38)."""

    sequence: int
    relative_path: str
    volume_serial_number: int
    file_id: int
    pre_sd_sha256: str
    state: NodeWalState

    def __post_init__(self) -> None:
        _validate_sequence(self.sequence)
        object.__setattr__(self, "relative_path", _validate_relative_path(self.relative_path))
        object.__setattr__(
            self, "volume_serial_number", _validate_uint(self.volume_serial_number, "volume_serial_number", _MAX_UINT64)
        )
        object.__setattr__(self, "file_id", _validate_uint(self.file_id, "file_id", _MAX_UINT128))
        object.__setattr__(self, "pre_sd_sha256", _validate_sha256_hex(self.pre_sd_sha256, "pre_sd_sha256"))
        if not isinstance(self.state, NodeWalState):
            raise ProtectionJournalSchemaError("state debe ser un miembro de NodeWalState")

    @property
    def kind(self) -> JournalRecordKind:
        return JournalRecordKind.NODE


JournalRecord = JournalHeaderRecord | JournalTransactionRecord | JournalNodeRecord


@dataclass(frozen=True, slots=True)
class JournalPhysicalRoot:
    """Identidad física del Golden ligada por el journal."""

    canonical_root: str
    volume_serial_number: int
    root_file_id: int


@dataclass(frozen=True, slots=True)
class ProtectionJournal:
    """Estado derivado del journal tras reproducir todos sus registros durables."""

    operation_id: str
    authorized_plan_digest: str
    physical_root: JournalPhysicalRoot
    transaction_state: ProtectionTransactionState
    node_records: tuple[JournalNodeRecord, ...]
    created_at: str
    sequence: int
    schema_version: str = PROTECTION_JOURNAL_SCHEMA_VERSION

    def node_record(self, relative_path: str) -> JournalNodeRecord | None:
        """ÚLTIMO registro durable del nodo: el estado vigente, no el primero."""
        for record in reversed(self.node_records):
            if record.relative_path == relative_path:
                return record
        return None

    def node_state(self, relative_path: str) -> NodeWalState | None:
        record = self.node_record(relative_path)
        return None if record is None else record.state

    def nodes_in_state(self, state: NodeWalState) -> tuple[JournalNodeRecord, ...]:
        """Nodos cuyo estado VIGENTE es ``state`` (un nodo aparece una sola vez)."""
        latest: dict[str, JournalNodeRecord] = {}
        for record in self.node_records:
            latest[record.relative_path] = record
        return tuple(record for record in latest.values() if record.state is state)

    def nodes_with_durable_mutation_intent(self) -> tuple[JournalNodeRecord, ...]:
        """Nodos con ``MUTATING`` durable: la mutación PUEDE haber ocurrido (§62).

        Un ``MUTATING(K)`` durable NO permite afirmar ``mutation never happened``:
        el permiso se emitió, el proceso pudo haber muerto antes o después de
        tocar K. Esa ambigüedad es exactamente lo que S4-C necesita conservar.
        """
        return self.nodes_in_state(NodeWalState.MUTATING)

    def nodes_with_durable_mutation_completed(self) -> tuple[JournalNodeRecord, ...]:
        return self.nodes_in_state(NodeWalState.MUTATED)


# ============================================================================
# Serialización canónica de registros (una línea canónica por registro)
# ============================================================================


def serialize_journal_record(record: JournalRecord) -> bytes:
    """Serializa UN registro como línea canónica UTF-8 terminada en ``\\n``.

    El ``\\n`` final es parte del contrato de durabilidad: un archivo que no
    termina en ``\\n`` tiene una escritura cortada y su último registro NO se
    considera durable (fila C4b de §20).
    """
    return _canonical_json(_journal_record_dict(record)) + b"\n"


def _journal_record_dict(record: JournalRecord) -> dict[str, Any]:
    if isinstance(record, JournalHeaderRecord):
        return {
            "kind": JournalRecordKind.HEADER.value,
            "schema_version": record.schema_version,
            "sequence": record.sequence,
            "operation_id": record.operation_id,
            "authorized_plan_digest": record.authorized_plan_digest,
            "canonical_root": record.canonical_root,
            "volume_serial_number": record.volume_serial_number,
            "root_file_id": record.root_file_id,
            "created_at": record.created_at,
        }
    if isinstance(record, JournalTransactionRecord):
        return {
            "kind": JournalRecordKind.TRANSACTION.value,
            "sequence": record.sequence,
            "state": record.state.value,
        }
    if isinstance(record, JournalNodeRecord):
        return {
            "kind": JournalRecordKind.NODE.value,
            "sequence": record.sequence,
            "relative_path": record.relative_path,
            "volume_serial_number": record.volume_serial_number,
            "file_id": record.file_id,
            "pre_sd_sha256": record.pre_sd_sha256,
            "state": record.state.value,
        }
    raise ProtectionJournalSchemaError(f"tipo de registro desconocido: {type(record).__name__}")


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ProtectionJournalSchemaError(f"clave JSON duplicada en el journal: '{key}'")
        seen[key] = value
    return seen


# ============================================================================
# Parse y clasificación
# ============================================================================


class JournalLoadDisposition(StrEnum):
    """Disposición de la evidencia durable leída del journal."""

    VALID = "valid"
    TORN_TAIL = "torn_tail"
    SCHEMA_INVALID = "schema_invalid"


@dataclass(frozen=True, slots=True)
class JournalLoadResult:
    """Resultado de leer bytes del journal. Nunca fabrica éxito desde evidencia rota."""

    disposition: JournalLoadDisposition
    journal: ProtectionJournal | None
    detail: str = ""

    @property
    def is_valid(self) -> bool:
        return self.disposition is JournalLoadDisposition.VALID and self.journal is not None


def parse_journal_bytes(raw: bytes) -> JournalLoadResult:
    """Parsea los bytes del journal y los clasifica sin lanzar por contenido roto.

    - ``VALID``: todos los registros son durables, cerrados y monotónicos.
    - ``TORN_TAIL``: hay una escritura cortada (archivo que no termina en ``\\n``
      o línea final incompleta) -> ``INDETERMINATE`` (C4b).
    - ``SCHEMA_INVALID``: clave desconocida, enum inválido, secuencia rota,
      transición no declarada o binding inválido -> fail-closed.
    """
    if not isinstance(raw, bytes):
        return JournalLoadResult(JournalLoadDisposition.SCHEMA_INVALID, None, "los bytes del journal deben ser bytes")
    if raw == b"":
        return JournalLoadResult(
            JournalLoadDisposition.TORN_TAIL,
            None,
            "el journal existe pero no tiene registros durables (creación cortada)",
        )

    if not raw.endswith(b"\n"):
        return JournalLoadResult(
            JournalLoadDisposition.TORN_TAIL,
            None,
            "el journal no termina en newline: última escritura cortada (FlushFileBuffers no confirmado)",
        )

    records: list[JournalRecord] = []
    for line in raw.split(b"\n")[:-1]:
        try:
            record = _parse_journal_line(line)
        except ProtectionJournalError as exc:
            return JournalLoadResult(JournalLoadDisposition.SCHEMA_INVALID, None, str(exc))
        records.append(record)

    try:
        journal = replay_journal_records(records)
    except ProtectionJournalError as exc:
        return JournalLoadResult(JournalLoadDisposition.SCHEMA_INVALID, None, str(exc))
    return JournalLoadResult(JournalLoadDisposition.VALID, journal)


def deserialize_journal_bytes(raw: bytes) -> ProtectionJournal:
    """Igual que :func:`parse_journal_bytes` pero lanza tipado ante evidencia rota."""
    result = parse_journal_bytes(raw)
    if result.journal is None or result.disposition is not JournalLoadDisposition.VALID:
        if result.disposition is JournalLoadDisposition.TORN_TAIL:
            raise ProtectionJournalIndeterminateError(result.detail)
        raise ProtectionJournalSchemaError(result.detail)
    return result.journal


def _parse_journal_line(line: bytes) -> JournalRecord:
    if not line:
        raise ProtectionJournalSchemaError("línea vacía en el journal")
    try:
        payload = json.loads(line.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectionJournalSchemaError(f"registro del journal no es JSON UTF-8 válido: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtectionJournalSchemaError("cada registro del journal debe ser un objeto JSON")
    kind = payload.get("kind")
    if not isinstance(kind, str):
        raise ProtectionJournalSchemaError("kind ausente o no string en un registro del journal")
    try:
        record_kind = JournalRecordKind(kind)
    except ValueError as exc:
        raise ProtectionJournalSchemaError(f"kind de registro desconocido: '{kind}'") from exc

    sequence = payload.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ProtectionJournalSchemaError("sequence debe ser un entero >= 1")

    if record_kind is JournalRecordKind.HEADER:
        if set(payload.keys()) != HEADER_RECORD_KEYS:
            raise ProtectionJournalSchemaError(
                f"esquema de header no cerrado: observado={sorted(payload.keys())}, esperado={sorted(HEADER_RECORD_KEYS)}"
            )
        if payload["schema_version"] != PROTECTION_JOURNAL_SCHEMA_VERSION:
            raise ProtectionJournalSchemaError(
                f"schema_version debe ser '{PROTECTION_JOURNAL_SCHEMA_VERSION}'; observado '{payload['schema_version']}'"
            )
        return JournalHeaderRecord(
            schema_version=payload["schema_version"],
            sequence=sequence,
            operation_id=_validate_operation_id(payload["operation_id"]),
            authorized_plan_digest=_validate_sha256_hex(payload["authorized_plan_digest"], "authorized_plan_digest"),
            canonical_root=_validate_nonempty_text(payload["canonical_root"], "canonical_root"),
            volume_serial_number=_validate_uint(payload["volume_serial_number"], "volume_serial_number", _MAX_UINT64),
            root_file_id=_validate_uint(payload["root_file_id"], "root_file_id", _MAX_UINT128),
            created_at=_validate_nonempty_text(payload["created_at"], "created_at"),
        )

    if record_kind is JournalRecordKind.TRANSACTION:
        if set(payload.keys()) != TRANSACTION_RECORD_KEYS:
            raise ProtectionJournalSchemaError(
                f"esquema de transaction_state no cerrado: observado={sorted(payload.keys())}, "
                f"esperado={sorted(TRANSACTION_RECORD_KEYS)}"
            )
        try:
            state = ProtectionTransactionState(payload["state"])
        except ValueError as exc:
            raise ProtectionJournalSchemaError(f"estado transaccional desconocido: '{payload['state']}'") from exc
        return JournalTransactionRecord(sequence=sequence, state=state)

    if set(payload.keys()) != NODE_RECORD_KEYS:
        raise ProtectionJournalSchemaError(
            f"esquema de node_state no cerrado: observado={sorted(payload.keys())}, esperado={sorted(NODE_RECORD_KEYS)}"
        )
    try:
        node_state = NodeWalState(payload["state"])
    except ValueError as exc:
        raise ProtectionJournalSchemaError(f"estado de nodo desconocido: '{payload['state']}'") from exc
    return JournalNodeRecord(
        sequence=sequence,
        relative_path=_validate_relative_path(payload["relative_path"]),
        volume_serial_number=_validate_uint(payload["volume_serial_number"], "volume_serial_number", _MAX_UINT64),
        file_id=_validate_uint(payload["file_id"], "file_id", _MAX_UINT128),
        pre_sd_sha256=_validate_sha256_hex(payload["pre_sd_sha256"], "pre_sd_sha256"),
        state=node_state,
    )


# ============================================================================
# Replay y validación de transiciones
# ============================================================================


def replay_journal_records(records: Sequence[JournalRecord]) -> ProtectionJournal:
    """Reproduce los registros y devuelve el estado derivado, validando monotonía.

    Exige: primer registro ``journal_header`` con ``sequence == 1``, secuencia
    estrictamente creciente de a 1, transiciones declaradas en las tablas cerradas
    y sin regresiones por nodo.
    """
    if not records:
        raise ProtectionJournalSchemaError("el journal no tiene registros")
    header = records[0]
    if not isinstance(header, JournalHeaderRecord):
        raise ProtectionJournalSchemaError("el primer registro del journal debe ser journal_header")
    if header.sequence != 1:
        raise ProtectionJournalSchemaError(f"el header del journal debe tener sequence=1; observado={header.sequence}")

    transaction_state = ProtectionTransactionState(INITIAL_TRANSACTION_STATE)
    transaction_declared = False
    node_records: list[JournalNodeRecord] = []
    node_states: dict[str, NodeWalState] = {}
    expected_sequence = 2

    for record in records[1:]:
        if record.sequence != expected_sequence:
            raise ProtectionJournalSchemaError(
                f"secuencia del journal rota: esperado={expected_sequence}, observado={record.sequence}"
            )
        expected_sequence += 1
        if isinstance(record, JournalTransactionRecord):
            if not transaction_declared and record.state is ProtectionTransactionState(INITIAL_TRANSACTION_STATE):
                # El primer registro transaccional DECLARA el estado inicial que el
                # helper elevado fija al crear el journal (§19.2, "Dos fases del
                # FSM"). No es una auto-transición: es el asentamiento del estado
                # desde el que el resto de la tabla cerrada sí se aplica.
                transaction_declared = True
                continue
            assert_transaction_transition(transaction_state, record.state)
            transaction_state = record.state
            transaction_declared = True
        elif isinstance(record, JournalNodeRecord):
            current = node_states.get(record.relative_path)
            assert_node_transition(current, record.state, record.relative_path)
            node_states[record.relative_path] = record.state
            node_records.append(record)
        else:  # pragma: no cover - el header sólo puede ser el primero
            raise ProtectionJournalSchemaError("journal_header duplicado en el journal")

    return ProtectionJournal(
        operation_id=header.operation_id,
        authorized_plan_digest=header.authorized_plan_digest,
        physical_root=JournalPhysicalRoot(
            canonical_root=header.canonical_root,
            volume_serial_number=header.volume_serial_number,
            root_file_id=header.root_file_id,
        ),
        transaction_state=transaction_state,
        node_records=tuple(node_records),
        created_at=header.created_at,
        sequence=expected_sequence - 1,
        schema_version=header.schema_version,
    )


def assert_transaction_transition(
    current: ProtectionTransactionState,
    target: ProtectionTransactionState,
) -> None:
    """Valida una transición del FSM contra la tabla cerrada de §19.2."""
    if not isinstance(current, ProtectionTransactionState) or not isinstance(target, ProtectionTransactionState):
        raise ProtectionJournalTransitionError("las transiciones exigen miembros de ProtectionTransactionState")
    allowed = TRANSACTION_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ProtectionJournalTransitionError(f"transición de FSM no declarada: {current.value} -> {target.value}")


def assert_node_transition(
    current: NodeWalState | None,
    target: NodeWalState,
    relative_path: str,
) -> None:
    """Valida una transición por nodo contra la tabla cerrada (sin regresiones)."""
    if not isinstance(target, NodeWalState):
        raise ProtectionJournalTransitionError("target debe ser un miembro de NodeWalState")
    allowed = NODE_WAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        observed = "sin registro" if current is None else current.value
        raise ProtectionJournalTransitionError(
            f"transición de nodo no declarada para '{relative_path}': {observed} -> {target.value}"
        )


def build_journal_header_record(
    *,
    operation_id: str,
    authorized_plan_digest: str,
    canonical_root: str,
    volume_serial_number: int,
    root_file_id: int,
    created_at: str,
) -> JournalHeaderRecord:
    """Construye el header canónico (sequence=1) del journal de una operación."""
    return JournalHeaderRecord(
        schema_version=PROTECTION_JOURNAL_SCHEMA_VERSION,
        sequence=1,
        operation_id=operation_id,
        authorized_plan_digest=authorized_plan_digest,
        canonical_root=canonical_root,
        volume_serial_number=volume_serial_number,
        root_file_id=root_file_id,
        created_at=created_at,
    )


def compute_journal_record_digest(record: JournalRecord) -> str:
    """SHA-256 de la línea canónica de un registro (evidencia de auditoría)."""
    return hashlib.sha256(serialize_journal_record(record)).hexdigest()


# ============================================================================
# Validadores internos
# ============================================================================


def _validate_sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProtectionJournalSchemaError("sequence debe ser un entero >= 1")
    return value


def _validate_operation_id(value: object) -> str:
    if not isinstance(value, str):
        raise ProtectionJournalSchemaError("operation_id debe ser string")
    normalized = value.strip()
    try:
        parsed = uuid.UUID(normalized)
    except ValueError as exc:
        raise ProtectionJournalSchemaError(f"operation_id '{value}' no es un UUID válido") from exc
    if normalized != str(parsed):
        raise ProtectionJournalSchemaError("operation_id debe ser UUID canónico con guiones en minúsculas")
    return normalized


def _validate_sha256_hex(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProtectionJournalSchemaError(f"{field_name} debe ser string")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in _HEX_LOWER for ch in normalized):
        raise ProtectionJournalSchemaError(f"{field_name} debe ser SHA-256 hexadecimal de 64 caracteres")
    return normalized


def _validate_uint(value: object, field_name: str, max_value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtectionJournalSchemaError(f"{field_name} debe ser un entero")
    if not 0 <= value <= max_value:
        raise ProtectionJournalSchemaError(f"{field_name} debe estar en el rango [0, {max_value}]")
    return value


def _validate_nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtectionJournalSchemaError(f"{field_name} debe ser un string no vacío")
    return value.strip()


def _validate_relative_path(value: object) -> str:
    """Misma canonicalización anti-traversal que el ABI PRE del plan (§18)."""
    from sky_claw.local.runtime_vault.golden_protection_plan import _validate_canonical_relpath

    try:
        return _validate_canonical_relpath(value)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - se traduce al tipo de error del journal
        raise ProtectionJournalSchemaError(f"relative_path inválido: {exc}") from exc


__all__ = [
    "HEADER_RECORD_KEYS",
    "INITIAL_TRANSACTION_STATE",
    "JournalHeaderRecord",
    "JournalLoadDisposition",
    "JournalLoadResult",
    "JournalNodeRecord",
    "JournalPhysicalRoot",
    "JournalRecord",
    "JournalRecordKind",
    "JournalTransactionRecord",
    "NODE_RECORD_KEYS",
    "NODE_WAL_TRANSITIONS",
    "NodeWalState",
    "PrematureCommitError",
    "ProtectionJournal",
    "ProtectionJournalError",
    "ProtectionJournalIndeterminateError",
    "ProtectionJournalSchemaError",
    "ProtectionJournalTransitionError",
    "ProtectionTransactionState",
    "PROTECTION_JOURNAL_FILE_NAME",
    "PROTECTION_JOURNAL_OBJECT_NAME",
    "PROTECTION_JOURNAL_SCHEMA_VERSION",
    "S4A_PERMITTED_TRANSITION_TARGETS",
    "TERMINAL_TRANSACTION_STATES",
    "TRANSACTION_RECORD_KEYS",
    "TRANSACTION_TRANSITIONS",
    "assert_node_transition",
    "assert_transaction_transition",
    "build_journal_header_record",
    "compute_journal_record_digest",
    "deserialize_journal_bytes",
    "parse_journal_bytes",
    "replay_journal_records",
    "serialize_journal_record",
]
