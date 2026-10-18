"""Journal durable de transición de Frozen Runtime (P4-S3; ADR 0012 §34.4).

Autoridad normativa (en orden de dependencia):

- **§34.4 (`P4_DURABLE_TRANSITION`)** — esquema del journal, ubicaciones
  (``state/transition.json``, ``state/transitions/<T>.json``) e invariantes
  (``PERSIST_BEFORE_BIND``, ``AT_MOST_ONE_PENDING_PER_ROOT``, ``CAS_ON_EVERY_MUTATION``,
  ``UNKNOWN_STATE = FAIL_CLOSED``, ``CORRUPT_JOURNAL = FAIL_CLOSED``).
- **§36.6** — CAS de CREATE/UPDATE/FINALIZE/START_NEXT y anti-ABA
  (``transition_id`` UUIDv4 + ``journal_revision`` monótona por root).
- **§37.5** — protocolo write-ahead de finalización (F1 registro inmutable,
  F2 rotación del journal) y el invariante ``RECORD_EXISTS ⇒ TRANSITION_FINALIZED``.
- **§38.3** — ``finalization_seq`` derivado del store validado y la reserva
  no-clobber keyed por seq (``state/finalization-seqs/<seq>.json``).
- **§34.5/§36.9** — matriz de recuperación: ``UNKNOWN`` nunca se convierte en
  éxito; ``PENDING`` no se revierte automáticamente.

**Superficie de este slice (P4_S3_SCOPE = DURABLE_JOURNAL_FOUNDATION).**

- NO muta Effective Runtime, NO promueve, NO hace rollback, NO declara
  promoción completa.
- La superficie pública es de LECTURA/VALIDACIÓN/CLASIFICACIÓN más las entradas
  de creación ``iniciar_transicion_*``, que fallan cerrado
  (``JournalDependenciaNoDisponibleError``): la autoridad durable de aprobación
  consumida (§36.5/§37.3) no existe todavía y este slice NO la simula.
- La maquinaria de escritura (creación de PENDING, UPDATE, START_NEXT,
  FINALIZE con F1a/F1b/F2) está implementada y se ejercita mediante las
  funciones internas ``*_bajo_lock`` (adquieren el root lock real) con
  evidencia EXPLÍCITA inyectada por los tests (fault injection). No hay
  producción que pueda alcanzarlas en P4-S3: sin PENDING no hay nada que
  finalizar, y sin autoridad de aprobación no hay PENDING.
- La evidencia POST (§35.6) es un REQUISITO DE FORMA que el journal sella: la
  verdad de la observación de Effective Runtime es del verificador POST (P5),
  autoridad diferida. El journal exige que la evidencia exista, esté en PASS y
  sea consistente con la transición vigente; nunca la inventa.

 Persistencia: se reutilizan las primitivas declaradas de ``state.py``
(``write_json_atomic`` = mkstemp mismo directorio + fsync + ``os.replace``;
``reservar_ruta_json_exclusiva`` = ``open(path, "x")`` no-clobber). Este módulo
NO abre archivos ni usa ``os.open``: no figura en el oráculo de escritura de
``tests/test_frozen_runtime_p1_readonly.py`` por delegación declarada, y su
disciplina de lock se ancla en ``tests/test_frozen_runtime_p4_s3_durable_journal.py``.

``POWER_LOSS_GUARANTEE = NOT_CLAIMED``: ``os.replace`` es un reemplazo atómico
del namespace; no se afirma durabilidad ante corte de energía (no hay WAL).
``CROSS_FILE_ATOMICITY = NOT_CLAIMED`` (§37.2): el protocolo es burn-first con
matriz de crash, no una transacción distribuida.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import pathlib
import stat
import time
import uuid
from collections.abc import Callable
from enum import StrEnum

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime import state as state_module
from sky_claw.local.frozen_runtime.errors import (
    InvalidGenerationIdError,
    JournalCasError,
    JournalCorruptError,
    JournalDependenciaNoDisponibleError,
    JournalEvidenciaPostError,
    JournalIdentityError,
    JournalObservacionEffectiveError,
    JournalSchemaError,
    JournalStoreNoInicializadoError,
    StateCorruptError,
    StateSchemaError,
)
from sky_claw.local.frozen_runtime.generation_id import validar_generation_id
from sky_claw.local.frozen_runtime.independence import descripcion_de_enlace
from sky_claw.local.frozen_runtime.root_lock import FrozenRuntimeRootLockHandle
from sky_claw.local.frozen_runtime.state import load_frozen_runtime_state
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
    admitir_directorio_storage,
    finalization_seqs_state_dir,
    transition_journal_path,
    transitions_state_dir,
)
from sky_claw.local.frozen_runtime.storage_models import (
    FrozenRuntimeState,
    FrozenRuntimeStateLoadResult,
)

JOURNAL_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
RESERVATION_SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)

#: Retención mínima contractual (§37.5): gobierna RETENCIÓN/GC/procedencia de
#: rollback, NO el arranque de la siguiente transición (R11.5).
RETENTION_MIN_K = 3

_HEX_DIGEST = 64


# ---------------------------------------------------------------------------
# Enumeraciones normativas
# ---------------------------------------------------------------------------


class JournalOperation(StrEnum):
    """Operación de la transición (§34.4)."""

    PROMOTION = "PROMOTION"
    ROLLBACK = "ROLLBACK"


class JournalState(StrEnum):
    """Estado persistido del journal vigente (§36.6).

    ``CORRUPT`` es clasificación de LECTURA, no una fase que se persista;
    ``FINALIZED`` es el estado del REGISTRO inmutable, no del journal (§38.4).
    """

    NONE = "NONE"
    PENDING_PROMOTION = "PENDING_PROMOTION"
    PENDING_ROLLBACK = "PENDING_ROLLBACK"


class JournalDisposition(StrEnum):
    """Clasificación de lectura del journal (incluye ABSENT y CORRUPT)."""

    ABSENT = "ABSENT"
    NONE = "NONE"
    PENDING_PROMOTION = "PENDING_PROMOTION"
    PENDING_ROLLBACK = "PENDING_ROLLBACK"
    CORRUPT = "CORRUPT"


class RecordState(StrEnum):
    """Estado del registro inmutable del historial (§37.5)."""

    FINALIZED = "FINALIZED"


class PostVerifyResultado(StrEnum):
    """Resultado del POST de coherencia que la evidencia declara (§35.6)."""

    PASS = "PASS"
    FAIL = "FAIL"


class VeredictoReconciliacion(StrEnum):
    """Veredicto de la reconciliación de arranque del journal (§34.5)."""

    COMPLETE = "COMPLETE"
    RECOVER = "RECOVER"
    REQUIRE_OWNER = "REQUIRE_OWNER"
    FAIL_CLOSED = "FAIL_CLOSED"
    UNKNOWN_EFFECTIVE = "UNKNOWN_EFFECTIVE"
    REGISTRO_FINALIZADO_CON_JOURNAL_PENDING = "REGISTRO_FINALIZADO_CON_JOURNAL_PENDING"


# ---------------------------------------------------------------------------
# Modelos tipados
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class SourceActivation:
    """Qué estaba activo al crear la transición (§34.4, §36.3)."""

    generation_id: str | None
    clone_id: str | None
    runtime_setup_id: str | None
    effective_runtime_evidence: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenceDigests:
    """Digests sellados de evidencia objetivo (§34.4/§34.6)."""

    clone_evidence: str
    runtime_setup_evidence: str
    compatibility_evidence: str


@dataclasses.dataclass(frozen=True, slots=True)
class PropuestaTransicion:
    """Propuesta completa de PENDING: la aprueba la autoridad de aprobación
    (ausente en P4-S3) y la consume el journal."""

    transition_id: str
    operation: JournalOperation
    source_activation: SourceActivation
    source_activation_digest: str
    target_generation_id: str
    target_clone_id: str
    target_runtime_setup_id: str
    approval_scope_digest: str
    evidence_digests: EvidenceDigests
    expected_effective_path: str
    created_at_ns: int


@dataclasses.dataclass(frozen=True, slots=True)
class AprobacionConsumida:
    """Evidencia de que la aprobación YA fue consumida por esta transición.

    El journal la VERIFICA (cross-checks de identidad y digest); no la produce.
    La autoridad que la emite de forma durable (``state/approval.json`` +
    witnesses + tombstones, §36.5/§37.3/§38.2) es dependencia diferida.
    """

    approval_id: str
    approval_seq: int
    scope_digest: str
    consumed_by_transition_id: str


@dataclasses.dataclass(frozen=True, slots=True)
class PostVerifyEvidence:
    """Evidencia durable del POST exitoso (§35.6).

    Liga, al mismo documento: transición, source, target, estado deseado,
    observación de Effective **y la revisión del journal sobre la que se corrió
    el POST** (``expected_journal_revision``). El journal sella su digest; la
    observación en sí es responsabilidad del verificador POST (P5, diferido).

    ``expected_journal_revision`` es el CAS anti-ABA de §36.6
    (``JOURNAL_FINALIZE_CAS = (transition_id, journal_revision)``): una evidencia
    calculada para la revisión R NO puede finalizar una transición que ya avanzó
    a R+1. Sin este campo, comparar sólo ``transition_id`` deja que el mismo
    ``transition_id`` cierre con evidencia obsoleta (§36.6/§36.9 C6).
    """

    transition_id: str
    resultado: PostVerifyResultado
    source_activation_digest: str
    target_generation_id: str
    target_clone_id: str
    target_runtime_setup_id: str
    desired_active_generation: str | None
    effective_observation_digest: str
    payload_digest: str
    verified_at_ns: int
    expected_journal_revision: int


@dataclasses.dataclass(frozen=True, slots=True)
class TransitionJournal:
    """Documento ``state/transition.json`` parseado (esquema cerrado por estado)."""

    schema_version: int
    state: JournalState
    journal_revision: int
    created_at_ns: int
    lock_session_id: str
    transition_id: str | None = None
    operation: JournalOperation | None = None
    source_activation: SourceActivation | None = None
    source_activation_digest: str | None = None
    target_generation_id: str | None = None
    target_clone_id: str | None = None
    target_runtime_setup_id: str | None = None
    approval_scope_digest: str | None = None
    consumed_approval_id: str | None = None
    consumed_approval_seq: int | None = None
    consumed_scope_digest: str | None = None
    evidence_digests: EvidenceDigests | None = None
    expected_effective_path: str | None = None
    last_finalized_transition_id: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class JournalLoadResult:
    """Resultado de lectura del journal: ausente NO es corrupto."""

    disposition: JournalDisposition
    journal: TransitionJournal | None
    path: object
    message: str


@dataclasses.dataclass(frozen=True, slots=True)
class FinalizedTransitionRecord:
    """Registro inmutable ``state/transitions/<T>.json`` (§37.5)."""

    schema_version: int
    record_state: RecordState
    transition_id: str
    finalization_seq: int
    finalized_from_transition_id: str | None
    operation: JournalOperation
    source_activation: SourceActivation
    source_activation_digest: str
    previous_activation_target: SourceActivation
    target_generation_id: str
    target_clone_id: str
    target_runtime_setup_id: str
    approval_scope_digest: str
    consumed_approval_id: str
    consumed_approval_seq: int
    post_verify_evidence_digest: str
    post_verified_at_ns: int
    finalized_at_ns: int


@dataclasses.dataclass(frozen=True, slots=True)
class SeqReservation:
    """Reserva no-clobber keyed por ``finalization_seq`` (§38.3)."""

    schema_version: int
    root_id: str
    finalization_seq: int
    transition_id: str


@dataclasses.dataclass(frozen=True, slots=True)
class HistorialFinalizado:
    """Vista derivada del store validado (§38.3)."""

    registros: tuple[FinalizedTransitionRecord, ...]
    next_seq: int
    latest: FinalizedTransitionRecord | None
    frontera_seq: int | None


@dataclasses.dataclass(frozen=True, slots=True)
class AuditoriaReservas:
    """Auditoría de las reservas contra el store derivado."""

    reservas: tuple[SeqReservation, ...]
    huerfanas: tuple[tuple[int, str], ...]
    inconsistentes: tuple[tuple[int, str], ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ReconciliacionJournal:
    """Veredicto de reconciliación: observa y clasifica, nunca muta."""

    veredicto: VeredictoReconciliacion
    punto_crash: str | None
    motivo: str
    requiere_dueno: bool
    registro_finalizado_presente: bool | None


# ---------------------------------------------------------------------------
# Validadores de forma (fail-closed, sin defaults silenciosos)
# ---------------------------------------------------------------------------

#: Registro plantilla: sólo para derivar el conjunto EXACTO de campos del
#: registro inmutable (nunca se persiste).
_REGISTRO_VACIO = FinalizedTransitionRecord(
    schema_version=RECORD_SCHEMA_VERSION,
    record_state=RecordState.FINALIZED,
    transition_id="00000000-0000-4000-8000-000000000000",
    finalization_seq=1,
    finalized_from_transition_id=None,
    operation=JournalOperation.PROMOTION,
    source_activation=SourceActivation(
        generation_id=None, clone_id=None, runtime_setup_id=None, effective_runtime_evidence=None
    ),
    source_activation_digest="0" * 64,
    previous_activation_target=SourceActivation(
        generation_id=None, clone_id=None, runtime_setup_id=None, effective_runtime_evidence=None
    ),
    target_generation_id="",
    target_clone_id="",
    target_runtime_setup_id="",
    approval_scope_digest="0" * 64,
    consumed_approval_id="00000000-0000-4000-8000-000000000000",
    consumed_approval_seq=1,
    post_verify_evidence_digest="0" * 64,
    post_verified_at_ns=1,
    finalized_at_ns=0,
)


def _digest_canonico(payload: object) -> str:
    """SHA-256 del JSON canónico (sort_keys + separadores compactos).

    Acepta dataclasses (se convierten a dict): el digest del
    ``source_activation`` debe ser idéntico sobre la forma dict (la que se
    persiste) y sobre la forma tipada (la que se valida).
    """
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    canonico = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def digest_canonico(payload: object) -> str:
    """Forma pública del digest canónico del subsistema (P4-S4).

    La autoridad durable de aprobación (``approval.py``) DEBE reusar esta
    función en vez de reimplementarla: ``source_activation_digest`` viaja en el
    ``ApprovalScope`` y el journal lo recomputa al crear el PENDING. Dos
    implementaciones del mismo digest es el modo de falla clásico de este repo
    —arreglar un hermano y dejar el otro— y acá produciría una aprobación que el
    journal rechaza por «no ligar el contenido».
    """
    return _digest_canonico(payload)


def _exigir_texto_valor(valor: object, *, campo: str, etiqueta: str) -> str:
    """String no vacío y SIN whitespace de borde (R3-01/R3-02).

    El chequeo previo era `not valor`, que rechaza `""` pero acepta `"   "`: una
    identidad o una ruta compuesta sólo por espacios pasaba la validación y se
    persistía como valor legítimo.

    Es **validación, no normalización**: se rechaza, no se recorta. Recortar en
    silencio cambiaría el valor persistido y, con él, el digest que lo liga
    (§36.3/§36.6): `"x "` y `"x"` no son la misma identidad.
    """
    if isinstance(valor, bool) or not isinstance(valor, str) or not valor:
        raise JournalSchemaError(f"{etiqueta}: '{campo}' debe ser un string no vacío")
    if valor != valor.strip():
        raise JournalSchemaError(f"{etiqueta}: '{campo}' debe ser un string sin espacios en los bordes: {valor!r}")
    return valor


def _exigir_generation_id_valor(valor: object, *, campo: str, etiqueta: str) -> str:
    """Generation-id CANÓNICO según el validador del dominio (P2 G03–G05).

    El journal NO inventa su propio criterio para un id que ya tiene validador
    normativo: delega en ``validar_generation_id``, que rechaza el whitespace de
    borde, el formato `<display-version>__<digest12>`, el traversal y los nombres
    reservados de Windows — y su docstring lo declara la puerta obligatoria de
    todo id que llegue a un path.

    Se exige el id **ya canónico** (minúsculas): se VALIDA, no se normaliza.
    Aceptar `"1.6.1170.0__A1B2C3D4E5F6"` y persistirlo tal cual dejaría una
    identidad que el dominio considera no canónica; transformarla en silencio
    cambiaría la identidad persistida y su digest.
    """
    if isinstance(valor, bool) or not isinstance(valor, str) or not valor:
        raise JournalSchemaError(f"{etiqueta}: '{campo}' debe ser un string no vacío")
    try:
        canonico = validar_generation_id(valor)
    except InvalidGenerationIdError as exc:
        raise JournalSchemaError(
            f"{etiqueta}: '{campo}' debe ser un generation-id canónico "
            f"('<display-version>__<digest12>', minúsculas): {exc}"
        ) from exc
    if canonico != valor:
        raise JournalSchemaError(
            f"{etiqueta}: '{campo}' debe ser un generation-id CANÓNICO (minúsculas, sin espacios): {valor!r}"
        )
    return valor


def _exigir_texto(payload: dict[str, object], campo: str, *, etiqueta: str) -> str:
    if campo not in payload:
        raise JournalSchemaError(f"{etiqueta}: falta el campo obligatorio '{campo}'")
    return _exigir_texto_valor(payload[campo], campo=campo, etiqueta=etiqueta)


def _exigir_texto_opcional(payload: dict[str, object], campo: str, *, etiqueta: str) -> str | None:
    if campo not in payload:
        return None
    valor = payload[campo]
    if valor is None:
        return None
    return _exigir_texto_valor(valor, campo=campo, etiqueta=etiqueta)


def _exigir_generation_id(payload: dict[str, object], campo: str, *, etiqueta: str) -> str:
    if campo not in payload:
        raise JournalSchemaError(f"{etiqueta}: falta el campo obligatorio '{campo}'")
    return _exigir_generation_id_valor(payload[campo], campo=campo, etiqueta=etiqueta)


def _exigir_generation_id_opcional(payload: dict[str, object], campo: str, *, etiqueta: str) -> str | None:
    if campo not in payload:
        return None
    valor = payload[campo]
    if valor is None:
        return None
    return _exigir_generation_id_valor(valor, campo=campo, etiqueta=etiqueta)


def _exigir_entero(payload: dict[str, object], campo: str, *, etiqueta: str, minimo: int) -> int:
    if campo not in payload:
        raise JournalSchemaError(f"{etiqueta}: falta el campo obligatorio '{campo}'")
    valor = payload[campo]
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < minimo:
        raise JournalSchemaError(f"{etiqueta}: '{campo}' debe ser un entero >= {minimo}")
    return valor


def _exigir_uuid_v4(valor: object, *, campo: str, etiqueta: str) -> str:
    if isinstance(valor, bool) or not isinstance(valor, str) or not valor:
        raise JournalSchemaError(f"{etiqueta}: '{campo}' debe ser un string")
    try:
        parseado = uuid.UUID(valor)
    except ValueError as exc:
        raise JournalSchemaError(f"{etiqueta}: '{campo}' no es un UUID válido: {exc}") from exc
    if parseado.version != 4 or str(parseado) != valor:
        raise JournalSchemaError(f"{etiqueta}: '{campo}' debe ser un UUID v4 canónico en minúsculas")
    return valor


def _exigir_digest(valor: object, *, campo: str, etiqueta: str) -> str:
    if isinstance(valor, bool) or not isinstance(valor, str):
        raise JournalSchemaError(f"{etiqueta}: '{campo}' debe ser un string")
    if len(valor) != _HEX_DIGEST or any(caracter not in "0123456789abcdef" for caracter in valor):
        raise JournalSchemaError(f"{etiqueta}: '{campo}' debe ser un digest SHA-256 hexadecimal de 64 caracteres")
    return valor


def _exigir_campos_exactos(
    payload: dict[str, object],
    *,
    requeridos: frozenset[str],
    opcionales: frozenset[str],
    etiqueta: str,
) -> None:
    claves = set(payload)
    if not requeridos.issubset(claves):
        raise JournalSchemaError(f"{etiqueta}: faltan campos obligatorios {sorted(requeridos - claves)}")
    if not claves.issubset(requeridos | opcionales):
        raise JournalSchemaError(f"{etiqueta}: campos no previstos {sorted(claves - (requeridos | opcionales))}")


def _validar_source_activation(payload: object, *, etiqueta: str) -> SourceActivation:
    """Source baseline COHERENTE o bootstrap vacío (§36.3).

    ``NEW_TRANSITION_REQUIRES_COHERENT_SOURCE_BASELINE = YES``: la terna
    completa —incluido ``effective_runtime_evidence``— es lo que convierte una
    afirmación *registrada* en una *observada*. Un source parcial (p. ej.
    ``{generation_id, clone_id, runtime_setup_id, effective_runtime_evidence:
    null}``) no es ni bootstrap ni observación: no demuestra contra qué runtime
    se está midiendo, y ``previous_activation_target`` se derivaría de una
    fuente que nunca estuvo demostrada (§36.3/§36.8). Fail-closed.
    """
    if not isinstance(payload, dict):
        raise JournalSchemaError(f"{etiqueta}: 'source_activation' debe ser un objeto JSON")
    _exigir_campos_exactos(
        payload,
        requeridos=frozenset({"generation_id", "clone_id", "runtime_setup_id", "effective_runtime_evidence"}),
        opcionales=frozenset(),
        etiqueta=etiqueta,
    )
    source = SourceActivation(
        generation_id=_exigir_generation_id_opcional(payload, "generation_id", etiqueta=etiqueta),
        clone_id=_exigir_texto_opcional(payload, "clone_id", etiqueta=etiqueta),
        runtime_setup_id=_exigir_texto_opcional(payload, "runtime_setup_id", etiqueta=etiqueta),
        effective_runtime_evidence=_exigir_texto_opcional(payload, "effective_runtime_evidence", etiqueta=etiqueta),
    )
    presentes = [
        nombre
        for nombre, valor in (
            ("generation_id", source.generation_id),
            ("clone_id", source.clone_id),
            ("runtime_setup_id", source.runtime_setup_id),
            ("effective_runtime_evidence", source.effective_runtime_evidence),
        )
        if valor is not None
    ]
    if presentes and len(presentes) != 4:
        faltantes = sorted(
            {
                "generation_id",
                "clone_id",
                "runtime_setup_id",
                "effective_runtime_evidence",
            }
            - set(presentes)
        )
        raise JournalSchemaError(
            f"{etiqueta}: 'source_activation' PARCIAL: presentes {sorted(presentes)}, faltantes {faltantes}. "
            "O bootstrap íntegramente vacío (los cuatro en null) o la terna completa con su "
            "effective_runtime_evidence (§36.3): una fuente a medias no es una observación."
        )
    return source


def _validar_evidence_digests(payload: object, *, etiqueta: str) -> EvidenceDigests:
    if not isinstance(payload, dict):
        raise JournalSchemaError(f"{etiqueta}: 'evidence_digests' debe ser un objeto JSON")
    _exigir_campos_exactos(
        payload,
        requeridos=frozenset({"clone_evidence", "runtime_setup_evidence", "compatibility_evidence"}),
        opcionales=frozenset(),
        etiqueta=etiqueta,
    )
    return EvidenceDigests(
        clone_evidence=_exigir_digest(payload["clone_evidence"], campo="clone_evidence", etiqueta=etiqueta),
        runtime_setup_evidence=_exigir_digest(
            payload["runtime_setup_evidence"], campo="runtime_setup_evidence", etiqueta=etiqueta
        ),
        compatibility_evidence=_exigir_digest(
            payload["compatibility_evidence"], campo="compatibility_evidence", etiqueta=etiqueta
        ),
    )


def _root_id_canonico(root: pathlib.Path) -> str:
    """Identidad canónica del root actual (la misma clave que usa el root lock)."""
    from sky_claw.local.frozen_runtime.root_lock import canonical_root_key

    return canonical_root_key(root)[0]


def _admitir_dir_store(directorio: pathlib.Path) -> bool:
    """Admite un directorio del store ANTES de preguntar si existe (§34.5 N2).

    ``exists()`` SIGUE enlaces: un symlink/junction colgado —o un ancestro
    redirigido— se ve "ausente", y un store redirigido se degradaría a "store
    vacío legítimo" (historial vacío ⇒ N0/N1 limpio). La admisión va PRIMERO y
    falla cerrado; sólo un directorio real o uno inexistente-de-verdad siguen.

    Devuelve ``True`` si el directorio existe (y es real), ``False`` si no
    existe. Un enlace/reparse/archivo/ilegible ⇒ ``JournalIdentityError``.
    """
    admision = admitir_directorio_storage(directorio)
    if not admision.success:
        raise JournalIdentityError(f"{directorio}: {admision.message}")
    return directorio.exists()


def _exigir_dirs_store(root: pathlib.Path) -> None:
    """El store del journal debe existir (lo crea el init del storage)."""
    ruta = pathlib.Path(root)
    for directorio in (transitions_state_dir(ruta), finalization_seqs_state_dir(ruta)):
        if not _admitir_dir_store(directorio):
            raise JournalStoreNoInicializadoError(
                f"{directorio} no existe: ejecutá initialize_frozen_runtime_storage antes de usar el journal"
            )


def _placeholder_vacio(ruta: pathlib.Path) -> bool:
    """True si *ruta* es un archivo REGULAR genuino de 0 bytes.

    ``reservar_ruta_json_exclusiva`` deja el placeholder a propósito; si la
    escritura del payload falla después, el archivo queda vacío. Bajo el root
    lock, un placeholder vacío de una ruta reservada por ESTA transición es
    completable por su dueño: no hay evidencia que perder.

    Un ENLACE nunca es placeholder, y tampoco lo es un DIRECTORIO: ``stat()``
    sigue enlaces y en NTFS un directorio reporta ``st_size == 0``, así que un
    junction llamado ``<T>.json`` o un directorio real llamado ``<T>.json`` se
    omitían como placeholder y nunca llegaban a ``_leer_json_estricto``, que sí
    los rechaza (§34.5 N2). Se inspecciona el enlace ANTES de medir, se mide con
    ``lstat`` (sin seguir enlaces) y sólo se acepta ``S_ISREG``.
    """
    try:
        if link_kind_or_raise(ruta) is not None:
            return False
        medida = ruta.lstat()
    except OSError:
        return False
    return stat.S_ISREG(medida.st_mode) and medida.st_size == 0


def _leer_json_estricto(ruta: pathlib.Path) -> dict[str, object]:
    """Lee un JSON de control rechazando enlaces, namespace redirigido y basura.

    Un enlace/reparse NUNCA es "ausente": se rechaza fail-closed (§8 del brief;
    misma doctrina que ``load_frozen_runtime_state``).
    """
    ruta = pathlib.Path(ruta)
    try:
        kind = link_kind_or_raise(ruta)
    except OSError as exc:
        raise JournalCorruptError(f"{ruta}: no se pudo inspeccionar: {exc}") from exc
    if kind is not None:
        raise JournalCorruptError(f"{ruta}: el archivo es un enlace/reparse ({kind}): fail-closed")
    motivo_padre = descripcion_de_enlace(ruta.parent)
    if motivo_padre is not None:
        raise JournalCorruptError(f"{ruta}: namespace redirigido: {motivo_padre}")
    try:
        raw = ruta.read_bytes()
    except OSError as exc:
        raise JournalCorruptError(f"{ruta}: no se pudo leer: {exc}") from exc
    if not raw.strip():
        raise JournalCorruptError(f"{ruta}: archivo vacío (0 bytes)")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalCorruptError(f"{ruta}: JSON UTF-8 inválido o truncado: {exc}") from exc
    if not isinstance(data, dict):
        raise JournalCorruptError(f"{ruta}: el documento debe ser un objeto JSON")
    return data


def leer_json_de_control(ruta: pathlib.Path) -> dict[str, object]:
    """Lector estricto público de un JSON de control (lo comparten journal y
    autoridad de aprobación).

    Un enlace/reparse o un namespace redirigido NUNCA es «ausente»: se rechaza
    fail-closed. La autoridad de aprobación lo reusa en vez de reimplementarlo
    —misma doctrina que ``digest_canonico``— y traduce el error a su propia
    familia para que el caller distinga el subsistema que falló.
    """
    return _leer_json_estricto(ruta)


# ---------------------------------------------------------------------------
# Parseo del journal vigente (esquema cerrado por estado)
# ---------------------------------------------------------------------------

_CAMPOS_JOURNAL_BASE = frozenset({"schema_version", "state", "journal_revision", "created_at_ns", "lock_session_id"})
_CAMPOS_JOURNAL_PENDIENTE = frozenset(
    {
        "operation",
        "transition_id",
        "source_activation",
        "source_activation_digest",
        "target_generation_id",
        "target_clone_id",
        "target_runtime_setup_id",
        "approval_scope_digest",
        "consumed_approval_id",
        "consumed_approval_seq",
        "consumed_scope_digest",
        "evidence_digests",
        "expected_effective_path",
    }
)


def _parsear_journal(payload: dict[str, object], ruta: object) -> TransitionJournal:
    etiqueta = str(ruta)
    if "schema_version" not in payload:
        raise JournalSchemaError(f"{etiqueta}: falta el campo obligatorio 'schema_version'")
    version = payload["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != JOURNAL_SCHEMA_VERSION:
        raise JournalSchemaError(
            f"{etiqueta}: schema_version desconocido {version!r}; soportado: {JOURNAL_SCHEMA_VERSION} (fail-closed)"
        )
    if "state" not in payload:
        raise JournalSchemaError(f"{etiqueta}: falta el campo obligatorio 'state'")
    estado_raw = payload["state"]
    if estado_raw not in {estado.value for estado in JournalState}:
        raise JournalSchemaError(f"{etiqueta}: estado desconocido {estado_raw!r}")

    revision = _exigir_entero(payload, "journal_revision", etiqueta=etiqueta, minimo=0)
    created_at_ns = _exigir_entero(payload, "created_at_ns", etiqueta=etiqueta, minimo=0)
    lock_session_id = _exigir_uuid_v4(
        _exigir_texto(payload, "lock_session_id", etiqueta=etiqueta),
        campo="lock_session_id",
        etiqueta=etiqueta,
    )

    if estado_raw == JournalState.NONE.value:
        _exigir_campos_exactos(
            payload,
            requeridos=_CAMPOS_JOURNAL_BASE,
            opcionales=frozenset({"last_finalized_transition_id"}),
            etiqueta=etiqueta,
        )
        testigo = payload.get("last_finalized_transition_id")
        if testigo is not None:
            testigo = _exigir_uuid_v4(testigo, campo="last_finalized_transition_id", etiqueta=etiqueta)
        return TransitionJournal(
            schema_version=version,
            state=JournalState.NONE,
            journal_revision=revision,
            created_at_ns=created_at_ns,
            lock_session_id=lock_session_id,
            last_finalized_transition_id=testigo,
        )

    # PENDING_*: esquema completo y cerrado
    _exigir_campos_exactos(
        payload,
        requeridos=_CAMPOS_JOURNAL_BASE | _CAMPOS_JOURNAL_PENDIENTE,
        opcionales=frozenset(),
        etiqueta=etiqueta,
    )
    operation_raw = payload["operation"]
    if operation_raw not in {operacion.value for operacion in JournalOperation}:
        raise JournalSchemaError(f"{etiqueta}: operation desconocida {operation_raw!r}")
    operation = JournalOperation(operation_raw)
    if estado_raw != f"PENDING_{operation.value}":
        raise JournalSchemaError(f"{etiqueta}: state {estado_raw!r} inconsistente con operation {operation_raw!r}")
    transition_id = _exigir_uuid_v4(
        _exigir_texto(payload, "transition_id", etiqueta=etiqueta), campo="transition_id", etiqueta=etiqueta
    )
    fuente = _validar_source_activation(payload["source_activation"], etiqueta=etiqueta)
    fuente_digest = _exigir_digest(
        payload["source_activation_digest"], campo="source_activation_digest", etiqueta=etiqueta
    )
    if fuente_digest != _digest_canonico(fuente):
        raise JournalIdentityError(f"{etiqueta}: source_activation_digest no liga el source_activation declarado")
    evidence = _validar_evidence_digests(payload["evidence_digests"], etiqueta=etiqueta)
    scope_digest = _exigir_digest(payload["approval_scope_digest"], campo="approval_scope_digest", etiqueta=etiqueta)
    consumed_scope = _exigir_digest(payload["consumed_scope_digest"], campo="consumed_scope_digest", etiqueta=etiqueta)
    if consumed_scope != scope_digest:
        raise JournalIdentityError(
            f"{etiqueta}: consumed_scope_digest difiere de approval_scope_digest (cross-check §37.6)"
        )
    return TransitionJournal(
        schema_version=version,
        state=JournalState(estado_raw),
        journal_revision=revision,
        created_at_ns=created_at_ns,
        lock_session_id=lock_session_id,
        transition_id=transition_id,
        operation=operation,
        source_activation=fuente,
        source_activation_digest=fuente_digest,
        target_generation_id=_exigir_generation_id(payload, "target_generation_id", etiqueta=etiqueta),
        target_clone_id=_exigir_texto(payload, "target_clone_id", etiqueta=etiqueta),
        target_runtime_setup_id=_exigir_texto(payload, "target_runtime_setup_id", etiqueta=etiqueta),
        approval_scope_digest=scope_digest,
        consumed_approval_id=_exigir_uuid_v4(
            _exigir_texto(payload, "consumed_approval_id", etiqueta=etiqueta),
            campo="consumed_approval_id",
            etiqueta=etiqueta,
        ),
        consumed_approval_seq=_exigir_entero(payload, "consumed_approval_seq", etiqueta=etiqueta, minimo=1),
        consumed_scope_digest=consumed_scope,
        evidence_digests=evidence,
        expected_effective_path=_exigir_texto(payload, "expected_effective_path", etiqueta=etiqueta),
        last_finalized_transition_id=None,
    )


def _serializar_journal(doc: TransitionJournal) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": doc.schema_version,
        "state": doc.state.value,
        "journal_revision": doc.journal_revision,
        "created_at_ns": doc.created_at_ns,
        "lock_session_id": doc.lock_session_id,
    }
    if doc.state == JournalState.NONE:
        payload["last_finalized_transition_id"] = doc.last_finalized_transition_id
        return payload
    assert doc.transition_id is not None and doc.operation is not None  # invariante de construcción
    assert doc.source_activation is not None and doc.evidence_digests is not None
    payload.update(
        {
            "operation": doc.operation.value,
            "transition_id": doc.transition_id,
            "source_activation": dataclasses.asdict(doc.source_activation),
            "source_activation_digest": doc.source_activation_digest,
            "target_generation_id": doc.target_generation_id,
            "target_clone_id": doc.target_clone_id,
            "target_runtime_setup_id": doc.target_runtime_setup_id,
            "approval_scope_digest": doc.approval_scope_digest,
            "consumed_approval_id": doc.consumed_approval_id,
            "consumed_approval_seq": doc.consumed_approval_seq,
            "consumed_scope_digest": doc.consumed_scope_digest,
            "evidence_digests": dataclasses.asdict(doc.evidence_digests),
            "expected_effective_path": doc.expected_effective_path,
        }
    )
    return payload


def _serializar_registro(record: FinalizedTransitionRecord) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "record_state": record.record_state.value,
        "transition_id": record.transition_id,
        "finalization_seq": record.finalization_seq,
        "finalized_from_transition_id": record.finalized_from_transition_id,
        "operation": record.operation.value,
        "source_activation": dataclasses.asdict(record.source_activation),
        "source_activation_digest": record.source_activation_digest,
        "previous_activation_target": dataclasses.asdict(record.previous_activation_target),
        "target_generation_id": record.target_generation_id,
        "target_clone_id": record.target_clone_id,
        "target_runtime_setup_id": record.target_runtime_setup_id,
        "approval_scope_digest": record.approval_scope_digest,
        "consumed_approval_id": record.consumed_approval_id,
        "consumed_approval_seq": record.consumed_approval_seq,
        "post_verify_evidence_digest": record.post_verify_evidence_digest,
        "post_verified_at_ns": record.post_verified_at_ns,
        "finalized_at_ns": record.finalized_at_ns,
    }


def _serializar_reserva(reserva: SeqReservation) -> dict[str, object]:
    return {
        "schema_version": reserva.schema_version,
        "root_id": reserva.root_id,
        "finalization_seq": reserva.finalization_seq,
        "transition_id": reserva.transition_id,
    }


# ---------------------------------------------------------------------------
# Lectura pública: journal, historial, reservas (sin mutación)
# ---------------------------------------------------------------------------


def cargar_journal_transicion(root: pathlib.Path) -> JournalLoadResult:
    """Lee y clasifica el journal vigente. Ausente ≠ corrupto (§36.4 N2).

    Un reparse point (enlace, junction, y también uno CÍCLICO o colgado, cuyo
    ``exists()`` es False) NUNCA se lee como ausente: se inspecciona el enlace
    con ``lstat`` ANTES de decidir, y ``_leer_json_estricto`` rechaza lo que no
    sea un archivo real.
    """
    ruta = transition_journal_path(root)
    try:
        kind = link_kind_or_raise(ruta)
    except OSError as exc:
        raise JournalCorruptError(f"{ruta}: no se pudo inspeccionar: {exc}") from exc
    if kind is None and not ruta.exists():
        # La HOJA no es un enlace y no existe, pero eso NO alcanza para afirmar
        # ausencia legítima: si `state/` (o un ancestro) es un symlink/junction
        # hacia un directorio externo vacío, la hoja "no existe" AHÍ y el
        # namespace redirigido se leería como primer arranque (§34.5 N2). El
        # hermano `load_frozen_runtime_state` ya inspecciona el parent antes de
        # decidir; acá se hace lo mismo, y `_leer_json_estricto` lo repetiría si
        # la hoja existiera.
        motivo_padre = descripcion_de_enlace(ruta.parent)
        if motivo_padre is not None:
            raise JournalCorruptError(f"{ruta}: namespace del journal redirigido: {motivo_padre}")
        return JournalLoadResult(
            disposition=JournalDisposition.ABSENT,
            journal=None,
            path=ruta,
            message="journal ausente: arranque limpio (sin transición)",
        )
    payload = _leer_json_estricto(ruta)
    doc = _parsear_journal(payload, ruta)
    return JournalLoadResult(
        disposition=JournalDisposition(doc.state.value),
        journal=doc,
        path=ruta,
        message="journal cargado",
    )


def _validar_registro(payload: dict[str, object], ruta: pathlib.Path) -> FinalizedTransitionRecord:
    etiqueta = str(ruta)
    if "schema_version" not in payload:
        raise JournalSchemaError(f"{etiqueta}: falta el campo obligatorio 'schema_version'")
    version = payload["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != RECORD_SCHEMA_VERSION:
        raise JournalSchemaError(f"{etiqueta}: schema_version desconocido {version!r}")
    if "record_state" not in payload:
        raise JournalSchemaError(f"{etiqueta}: falta el campo obligatorio 'record_state'")
    if payload["record_state"] != RecordState.FINALIZED.value:
        raise JournalSchemaError(f"{etiqueta}: record_state desconocido {payload['record_state']!r}")
    _exigir_campos_exactos(
        payload,
        requeridos=frozenset(_serializar_registro(_REGISTRO_VACIO)),
        opcionales=frozenset(),
        etiqueta=etiqueta,
    )
    transition_id = _exigir_uuid_v4(
        _exigir_texto(payload, "transition_id", etiqueta=etiqueta), campo="transition_id", etiqueta=etiqueta
    )
    if ruta.stem != transition_id:
        raise JournalIdentityError(f"{etiqueta}: registro incompatible con su nombre (declara {transition_id})")
    seq = _exigir_entero(payload, "finalization_seq", etiqueta=etiqueta, minimo=1)
    previa_raw = payload["finalized_from_transition_id"]
    previa = (
        None
        if previa_raw is None
        else _exigir_uuid_v4(previa_raw, campo="finalized_from_transition_id", etiqueta=etiqueta)
    )
    operation_raw = payload["operation"]
    if operation_raw not in {operacion.value for operacion in JournalOperation}:
        raise JournalSchemaError(f"{etiqueta}: operation desconocida {operation_raw!r}")
    fuente = _validar_source_activation(payload["source_activation"], etiqueta=etiqueta)
    fuente_digest = _exigir_digest(
        payload["source_activation_digest"], campo="source_activation_digest", etiqueta=etiqueta
    )
    if fuente_digest != _digest_canonico(fuente):
        raise JournalIdentityError(f"{etiqueta}: source_activation_digest no liga el contenido")
    previo_target = _validar_source_activation(payload["previous_activation_target"], etiqueta=etiqueta)
    if previo_target != fuente:
        raise JournalIdentityError(
            f"{etiqueta}: previous_activation_target debe ser el source_activation del registro (§36.8)"
        )
    return FinalizedTransitionRecord(
        schema_version=version,
        record_state=RecordState.FINALIZED,
        transition_id=transition_id,
        finalization_seq=seq,
        finalized_from_transition_id=previa,
        operation=JournalOperation(operation_raw),
        source_activation=fuente,
        source_activation_digest=fuente_digest,
        previous_activation_target=previo_target,
        target_generation_id=_exigir_generation_id(payload, "target_generation_id", etiqueta=etiqueta),
        target_clone_id=_exigir_texto(payload, "target_clone_id", etiqueta=etiqueta),
        target_runtime_setup_id=_exigir_texto(payload, "target_runtime_setup_id", etiqueta=etiqueta),
        approval_scope_digest=_exigir_digest(
            payload["approval_scope_digest"], campo="approval_scope_digest", etiqueta=etiqueta
        ),
        consumed_approval_id=_exigir_uuid_v4(
            _exigir_texto(payload, "consumed_approval_id", etiqueta=etiqueta),
            campo="consumed_approval_id",
            etiqueta=etiqueta,
        ),
        consumed_approval_seq=_exigir_entero(payload, "consumed_approval_seq", etiqueta=etiqueta, minimo=1),
        post_verify_evidence_digest=_exigir_digest(
            payload["post_verify_evidence_digest"], campo="post_verify_evidence_digest", etiqueta=etiqueta
        ),
        post_verified_at_ns=_exigir_entero(payload, "post_verified_at_ns", etiqueta=etiqueta, minimo=1),
        finalized_at_ns=_exigir_entero(payload, "finalized_at_ns", etiqueta=etiqueta, minimo=0),
    )


def _validar_reserva(
    payload: dict[str, object],
    ruta: pathlib.Path,
    *,
    root_id_esperado: str | None = None,
) -> SeqReservation:
    etiqueta = str(ruta)
    _exigir_campos_exactos(
        payload,
        requeridos=frozenset({"schema_version", "root_id", "finalization_seq", "transition_id"}),
        opcionales=frozenset(),
        etiqueta=etiqueta,
    )
    version = payload["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != RESERVATION_SCHEMA_VERSION:
        raise JournalSchemaError(f"{etiqueta}: schema_version desconocido {version!r}")
    seq = _exigir_entero(payload, "finalization_seq", etiqueta=etiqueta, minimo=1)
    if ruta.stem != str(seq):
        raise JournalIdentityError(f"{etiqueta}: reserva incompatible con su nombre (declara seq {seq})")
    if not isinstance(payload["root_id"], str) or not payload["root_id"]:
        raise JournalSchemaError(f"{etiqueta}: 'root_id' debe ser un string no vacío")
    # F9: la reserva es autoridad LOCAL de una seq de ESTE root (§38.3). Sin este
    # cross-check, reservas copiadas/restauradas de OTRO FrozenRuntimeRoot —y con
    # registros que las acompañen— pasarían la auditoría entera, pese a que tanto
    # el lock como la propiedad de la secuencia son root-keyed. La clave canónica
    # se compara normalizada (normcase + abspath), igual que el lock.
    if root_id_esperado is not None and payload["root_id"] != root_id_esperado:
        raise JournalIdentityError(
            f"{etiqueta}: 'root_id' no corresponde al root auditado "
            f"(declara {payload['root_id']!r}, el root canónico es {root_id_esperado!r}): "
            "una reserva de otro root NO es evidencia local"
        )
    return SeqReservation(
        schema_version=version,
        root_id=payload["root_id"],
        finalization_seq=seq,
        transition_id=_exigir_uuid_v4(
            _exigir_texto(payload, "transition_id", etiqueta=etiqueta),
            campo="transition_id",
            etiqueta=etiqueta,
        ),
    )


def derivar_historial_finalizados(root: pathlib.Path) -> HistorialFinalizado:
    """Deriva NEXT/LATEST/frontera del store validado (§38.3).

    Rechaza duplicados, huecos interiores, predecesores rotos y registros
    corruptos.     El truncamiento frontal es la frontera de retención (§38.6) y
    su predecesor puede estar fuera de la ventana (declarado, no verificable).

    Un placeholder vacío (0 bytes) en ``state/transitions/`` es una F1b
    interrumpida: la transición NO está finalizada y el archivo no es
    evidencia — se omite en lugar de leerse como corrupción. Sólo su dueño
    (bajo el root lock) lo completa (``_persistir_registro_locked``).
    """
    directorio = transitions_state_dir(root)
    if not _admitir_dir_store(directorio):
        snapshot = cargar_journal_transicion(root)
        if snapshot.disposition == JournalDisposition.ABSENT:
            return HistorialFinalizado(registros=(), next_seq=1, latest=None, frontera_seq=None)
        raise JournalStoreNoInicializadoError(
            f"{directorio} no existe pero el journal está presente: store no inicializado (fail-closed)"
        )
    registros: list[FinalizedTransitionRecord] = []
    for archivo in sorted(directorio.glob("*.json")):
        if _placeholder_vacio(archivo):
            continue  # F1b interrumpida: sin registro inmutable que derivar
        payload = _leer_json_estricto(archivo)
        registros.append(_validar_registro(payload, archivo))
    if not registros:
        return HistorialFinalizado(registros=(), next_seq=1, latest=None, frontera_seq=None)

    seqs = [registro.finalization_seq for registro in registros]
    ids = [registro.transition_id for registro in registros]
    if len(set(seqs)) != len(seqs):
        raise JournalIdentityError("finalization_seq duplicado en el store (anti-fork)")
    if len(set(ids)) != len(ids):
        raise JournalIdentityError("transition_id duplicado en el store")
    registros.sort(key=lambda registro: registro.finalization_seq)
    # OJO: el glob ordena por NOMBRE DE ARCHIVO (UUID), no por seq: s_min/s_max
    # se derivan del conjunto, nunca de la primera posición de la enumeración.
    s_min, s_max = min(seqs), max(seqs)
    if set(seqs) != set(range(s_min, s_max + 1)):
        raise JournalIdentityError(
            f"hueco interior en la cadena de finalization_seq: {sorted(seqs)} no es contiguo desde {s_min}"
        )
    for registro in registros:
        if registro.finalization_seq == s_min:
            continue  # frontera: su predecesor puede estar fuera de la ventana
        previo = next((r for r in registros if r.finalization_seq == registro.finalization_seq - 1), None)
        if previo is None or previo.transition_id != registro.finalized_from_transition_id:
            raise JournalIdentityError(
                f"predecesor roto: seq {registro.finalization_seq} declara "
                f"finalized_from_transition_id={registro.finalized_from_transition_id}"
            )
    return HistorialFinalizado(
        registros=tuple(registros),
        next_seq=s_max + 1,
        latest=registros[-1],
        frontera_seq=s_min,
    )


def auditar_reservas_finalizacion(
    root: pathlib.Path, historial: HistorialFinalizado | None = None
) -> AuditoriaReservas:
    """Audita las reservas contra el store derivado (§38.3).

    - registro sin reserva ⇒ autoridad incoherente (``JournalIdentityError``);
    - reserva sin registro ⇒ huérfana (reconciliable o ``REQUIRE_OWNER``);
    - reserva y registro con el mismo seq pero distinto ``transition_id`` ⇒
      inconsistente (fail-closed).

    Un placeholder vacío (0 bytes) en ``state/finalization-seqs/`` es una F1a
    interrumpida: la reserva NO quedó comprometida (no hay evidencia de qué
    transición la reclamó), así que se omite — no es huérfana ni corrupción.
    """
    if historial is None:
        historial = derivar_historial_finalizados(root)
    directorio = finalization_seqs_state_dir(root)
    if not _admitir_dir_store(directorio):
        if historial.registros:
            raise JournalIdentityError(f"{directorio} no existe pero hay registros finalizados: autoridad incoherente")
        return AuditoriaReservas(reservas=(), huerfanas=(), inconsistentes=())
    reservas: list[SeqReservation] = []
    root_id_esperado = _root_id_canonico(root)
    for archivo in sorted(directorio.glob("*.json")):
        if _placeholder_vacio(archivo):
            continue  # F1a interrumpida: la reserva nunca se comprometió
        payload = _leer_json_estricto(archivo)
        reservas.append(_validar_reserva(payload, archivo, root_id_esperado=root_id_esperado))
    seqs_store = {registro.finalization_seq: registro.transition_id for registro in historial.registros}
    inconsistentes: list[tuple[int, str]] = []
    for reserva in reservas:
        tid_store = seqs_store.get(reserva.finalization_seq)
        if tid_store is not None and tid_store != reserva.transition_id:
            inconsistentes.append((reserva.finalization_seq, reserva.transition_id))
    for seq, tid in seqs_store.items():
        if not any(reserva.finalization_seq == seq and reserva.transition_id == tid for reserva in reservas):
            inconsistentes.append((seq, tid))
    if inconsistentes:
        raise JournalIdentityError(f"reservas incoherentes con el store: {sorted(inconsistentes)}")
    huerfanas = tuple(
        (reserva.finalization_seq, reserva.transition_id)
        for reserva in reservas
        if reserva.finalization_seq not in seqs_store
    )
    return AuditoriaReservas(reservas=tuple(reservas), huerfanas=huerfanas, inconsistentes=())


# ---------------------------------------------------------------------------
# Validación de propuestas, aprobación y evidencia POST
# ---------------------------------------------------------------------------


def _exigir_sin_reservas_huerfanas(root: pathlib.Path, historial: HistorialFinalizado) -> None:
    """CR-R3-02 — un estado con reserva huérfana exige dueño, no un PENDING nuevo.

    ``auditar_reservas_finalizacion`` devuelve las huérfanas en lugar de
    levantarlas porque, cuando hay un ``PENDING`` vigente, una reserva huérfana
    puede ser la MISMA transición interrumpida entre F1a y F1b y entonces el
    veredicto es ``RECOVER`` (§38.3). Los mutadores que crean un ``PENDING``
    nuevo operan sobre otro estado: exigen journal ``ABSENT``/``NONE``, y ahí no
    existe transición pendiente que pueda reclamar la reserva. Por lo tanto toda
    huérfana es AJENA —incluso si su ``transition_id`` coincide con el que se va
    a proponer, porque sin ``PENDING`` tampoco hay ``RECOVER``— y el estado
    durable es el que la reconciliación ya clasifica ``REQUIRE_OWNER``
    (``NONE``) o ``FAIL_CLOSED`` (``ABSENT``).

    Sin este guard, CREATE/START_NEXT escriben un ``PENDING`` que después no
    puede finalizar: ``_reservar_seq_locked`` (F1a) encuentra la seq tomada por
    la otra transición y levanta ``JournalCasError``, y por
    ``AT_MOST_ONE_PENDING_PER_ROOT`` el ``PENDING`` colgado bloquea además todo
    ``CREATE`` posterior.

    Se levanta ``JournalIdentityError`` y no ``JournalCasError``: el estado no es
    un CAS desactualizado que se resuelva reintentando, es autoridad incoherente
    que requiere intervención del dueño — el mismo error que el docstring de
    ``JournalIdentityError`` ya enumera como "reserva sin registro", y el mismo
    que usa su hermana ``auditar_reservas_finalizacion`` para las
    inconsistentes. El CAS fallido descarta; esto además exige un dueño.
    """
    auditoria = auditar_reservas_finalizacion(root, historial)
    if auditoria.huerfanas:
        raise JournalIdentityError(
            f"reserva(s) huérfana(s) sin transición pendiente: {auditoria.huerfanas} "
            "(REQUIRE_OWNER): no se escribe un PENDING nuevo sobre un store cuya autoridad "
            "de secuencia no tiene dueño"
        )


def _validar_propuesta(propuesta: PropuestaTransicion) -> None:
    _exigir_uuid_v4(propuesta.transition_id, campo="transition_id", etiqueta="propuesta")
    if not isinstance(propuesta.operation, JournalOperation):
        raise JournalSchemaError("propuesta: operation debe ser JournalOperation")
    fuente = propuesta.source_activation
    if not isinstance(fuente, SourceActivation):
        raise JournalSchemaError("propuesta: source_activation debe ser SourceActivation")
    if propuesta.source_activation_digest != _digest_canonico(fuente):
        raise JournalIdentityError("propuesta: source_activation_digest no liga el contenido")
    # R3-01/R3-02: el camino CREATE valida las MISMAS identidades y la misma
    # ruta que el parser. Un camino hermano sin el chequeo deja entrar por acá
    # exactamente lo que el parser rechaza.
    _exigir_generation_id_valor(propuesta.target_generation_id, campo="target_generation_id", etiqueta="propuesta")
    for campo in ("target_clone_id", "target_runtime_setup_id", "expected_effective_path"):
        _exigir_texto_valor(getattr(propuesta, campo), campo=campo, etiqueta="propuesta")
    _exigir_digest(propuesta.approval_scope_digest, campo="approval_scope_digest", etiqueta="propuesta")
    if not isinstance(propuesta.evidence_digests, EvidenceDigests):
        raise JournalSchemaError("propuesta: evidence_digests debe ser EvidenceDigests")
    for campo in ("clone_evidence", "runtime_setup_evidence", "compatibility_evidence"):
        _exigir_digest(getattr(propuesta.evidence_digests, campo), campo=campo, etiqueta="propuesta")
    if (
        isinstance(propuesta.created_at_ns, bool)
        or not isinstance(propuesta.created_at_ns, int)
        or propuesta.created_at_ns < 0
    ):
        raise JournalSchemaError("propuesta: created_at_ns debe ser un entero >= 0")


def _validar_aprobacion(aprobacion: AprobacionConsumida) -> None:
    _exigir_uuid_v4(aprobacion.approval_id, campo="approval_id", etiqueta="aprobación")
    _exigir_uuid_v4(aprobacion.consumed_by_transition_id, campo="consumed_by_transition_id", etiqueta="aprobación")
    _exigir_digest(aprobacion.scope_digest, campo="scope_digest", etiqueta="aprobación")
    if (
        isinstance(aprobacion.approval_seq, bool)
        or not isinstance(aprobacion.approval_seq, int)
        or aprobacion.approval_seq < 1
    ):
        raise JournalSchemaError("aprobación: approval_seq debe ser un entero >= 1")


def _exigir_digest_post(valor: object, *, campo: str) -> str:
    """Digest de la evidencia POST: los errores de FORMA son del dominio POST."""
    try:
        return _exigir_digest(valor, campo=campo, etiqueta="evidencia POST")
    except JournalSchemaError as exc:
        raise JournalEvidenciaPostError(str(exc)) from exc


def _exigir_uuid_v4_post(valor: object, *, campo: str) -> str:
    try:
        return _exigir_uuid_v4(valor, campo=campo, etiqueta="evidencia POST")
    except JournalSchemaError as exc:
        raise JournalEvidenciaPostError(str(exc)) from exc


def _exigir_identidad_post(valor: object, *, campo: str, generacion: bool) -> str:
    """Identidad de la evidencia POST (R3-01): forma del dominio, error del POST.

    Los errores de FORMA son del dominio POST —igual que ``_exigir_digest_post``—,
    y esta validación corre ANTES de comparar la evidencia contra el journal, para
    que una identidad en blanco se rechace por forma y no "por no coincidir".
    """
    try:
        if generacion:
            return _exigir_generation_id_valor(valor, campo=campo, etiqueta="evidencia POST")
        return _exigir_texto_valor(valor, campo=campo, etiqueta="evidencia POST")
    except JournalSchemaError as exc:
        raise JournalEvidenciaPostError(str(exc)) from exc


def _validar_post_evidence(evidencia: PostVerifyEvidence) -> None:
    _exigir_uuid_v4_post(evidencia.transition_id, campo="transition_id")
    resultado = evidencia.resultado
    if isinstance(resultado, str):
        if resultado not in {estado.value for estado in PostVerifyResultado}:
            raise JournalEvidenciaPostError("evidencia POST: resultado debe ser PASS o FAIL")
    elif not isinstance(resultado, PostVerifyResultado):
        raise JournalEvidenciaPostError("evidencia POST: resultado debe ser PASS o FAIL")
    _exigir_digest_post(evidencia.source_activation_digest, campo="source_activation_digest")
    _exigir_digest_post(evidencia.effective_observation_digest, campo="effective_observation_digest")
    _exigir_digest_post(evidencia.payload_digest, campo="payload_digest")
    # R3-01: las identidades de la evidencia se validan con la MISMA forma que el
    # parser y la propuesta, y antes de cualquier comparación de coherencia.
    _exigir_identidad_post(evidencia.target_generation_id, campo="target_generation_id", generacion=True)
    for campo in ("target_clone_id", "target_runtime_setup_id"):
        _exigir_identidad_post(getattr(evidencia, campo), campo=campo, generacion=False)
    deseado = evidencia.desired_active_generation
    if deseado is not None:
        _exigir_identidad_post(deseado, campo="desired_active_generation", generacion=True)
    if (
        isinstance(evidencia.verified_at_ns, bool)
        or not isinstance(evidencia.verified_at_ns, int)
        or evidencia.verified_at_ns < 1
    ):
        raise JournalEvidenciaPostError("evidencia POST: verified_at_ns debe ser un entero >= 1")
    revision = evidencia.expected_journal_revision
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise JournalEvidenciaPostError(
            "evidencia POST: expected_journal_revision debe ser un entero >= 0 "
            "(la revisión del journal sobre la que se corrió el POST; §36.6)"
        )


# ---------------------------------------------------------------------------
# Escritura bajo el root lock (convención ``_locked``: el caller posee el lock)
# ---------------------------------------------------------------------------


def _validar_antes_de_escribir_journal(doc: TransitionJournal, ruta: pathlib.Path) -> None:
    """Round-trip por el parser estricto ANTES de escribir (igual que state.py)."""
    _parsear_journal(_serializar_journal(doc), ruta)


def _reservar_seq_locked(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    *,
    seq: int,
    transition_id: str,
) -> SeqReservation:
    """F1a — reserva no-clobber keyed por ``finalization_seq`` (§38.3).

    El nombre depende SÓLO de la seq: dos finalizadores que deriven el mismo
    NEXT colisionan en el MISMO path y sólo uno gana. El ``open(..., "x")`` de
    ``reservar_ruta_json_exclusiva`` es la garantía física.
    """
    handle.assert_owned()
    ruta = finalization_seqs_state_dir(root) / f"{seq}.json"
    root_id = _root_id_canonico(root)
    try:
        state_module.reservar_ruta_json_exclusiva(ruta)
    except FileExistsError:
        if not _placeholder_vacio(ruta):
            existente = _validar_reserva(_leer_json_estricto(ruta), ruta, root_id_esperado=root_id)
            if existente.finalization_seq == seq and existente.transition_id == transition_id:
                return existente  # reintento de la MISMA reserva: idempotente
            raise JournalCasError(
                f"la seq {seq} ya está reservada por la transición {existente.transition_id}"
            ) from None
        # Reserva interrumpida ANTES de escribir contenido: el placeholder vacío
        # es nuestro (el root lock prueba que somos el único escritor) y no
        # contiene evidencia que perder. Se completa.
    reserva = SeqReservation(
        schema_version=RESERVATION_SCHEMA_VERSION,
        root_id=root_id,
        finalization_seq=seq,
        transition_id=transition_id,
    )
    state_module.write_json_atomic(ruta, _serializar_reserva(reserva))
    return reserva


def _persistir_registro_locked(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    record: FinalizedTransitionRecord,
) -> FinalizedTransitionRecord:
    """F1b — registro inmutable ``state/transitions/<T>.json`` (§37.5).

    Si ya existe y coincide en identidad+seq+evidencia, se DEVUELVE el existente
    (crash entre F1b y F2): el registro nunca se reescribe.
    """
    handle.assert_owned()
    ruta = transitions_state_dir(root) / f"{record.transition_id}.json"
    try:
        state_module.reservar_ruta_json_exclusiva(ruta)
    except FileExistsError:
        if not _placeholder_vacio(ruta):
            existente = _validar_registro(_leer_json_estricto(ruta), ruta)
            if (
                existente.transition_id == record.transition_id
                and existente.finalization_seq == record.finalization_seq
                and existente.post_verify_evidence_digest == record.post_verify_evidence_digest
            ):
                return existente
            raise JournalIdentityError(
                f"{ruta}: el registro inmutable ya existe con otra identidad o evidencia: no se sobrescribe"
            ) from None
        # Placeholder vacío de una F1b interrumpida: se completa (mismo razonamiento
        # que la reserva huérfana de F1a).
    state_module.write_json_atomic(ruta, _serializar_registro(record))
    return record


def _rotar_journal_locked(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    *,
    registro: FinalizedTransitionRecord,
    revision_previa: int,
) -> TransitionJournal:
    """F2 — rotación del journal a ``NONE`` con testigo redundante (§37.5).

    ``last_finalized_transition_id`` es TESTIGO: la autoridad del último
    registro es el store derivado por ``max(finalization_seq)`` (§38.3).
    """
    handle.assert_owned()
    ruta = transition_journal_path(root)
    vigente = cargar_journal_transicion(root)
    if (
        vigente.disposition == JournalDisposition.NONE
        and vigente.journal is not None
        and vigente.journal.last_finalized_transition_id == registro.transition_id
    ):
        return vigente.journal  # ya estaba rotado (reintento)
    if vigente.disposition not in (JournalDisposition.PENDING_PROMOTION, JournalDisposition.PENDING_ROLLBACK):
        raise JournalCasError("no se puede rotar: el journal vigente no tiene un PENDING de esta transición")
    if vigente.journal is None or vigente.journal.transition_id != registro.transition_id:
        raise JournalCasError("el PENDING vigente no corresponde a la transición finalizada")
    if vigente.journal.journal_revision != revision_previa:
        raise JournalCasError("revision desactualizada al rotar el journal (CAS)")
    doc = TransitionJournal(
        schema_version=JOURNAL_SCHEMA_VERSION,
        state=JournalState.NONE,
        journal_revision=revision_previa + 1,
        created_at_ns=time.time_ns(),
        lock_session_id=handle.session_id,
        last_finalized_transition_id=registro.transition_id,
    )
    _validar_antes_de_escribir_journal(doc, ruta)
    state_module.write_json_atomic(ruta, _serializar_journal(doc))
    return doc


def _construir_doc_pendiente(
    *,
    propuesta: PropuestaTransicion,
    aprobacion: AprobacionConsumida,
    revision: int,
    lock_session_id: str,
) -> TransitionJournal:
    return TransitionJournal(
        schema_version=JOURNAL_SCHEMA_VERSION,
        state=JournalState(f"PENDING_{propuesta.operation.value}"),
        journal_revision=revision,
        created_at_ns=propuesta.created_at_ns,
        lock_session_id=lock_session_id,
        transition_id=propuesta.transition_id,
        operation=propuesta.operation,
        source_activation=propuesta.source_activation,
        source_activation_digest=propuesta.source_activation_digest,
        target_generation_id=propuesta.target_generation_id,
        target_clone_id=propuesta.target_clone_id,
        target_runtime_setup_id=propuesta.target_runtime_setup_id,
        approval_scope_digest=propuesta.approval_scope_digest,
        consumed_approval_id=aprobacion.approval_id,
        consumed_approval_seq=aprobacion.approval_seq,
        consumed_scope_digest=aprobacion.scope_digest,
        evidence_digests=propuesta.evidence_digests,
        expected_effective_path=propuesta.expected_effective_path,
        last_finalized_transition_id=None,
    )


def _exigir_precondiciones_comunes(
    propuesta: PropuestaTransicion,
    aprobacion: AprobacionConsumida,
) -> None:
    _validar_propuesta(propuesta)
    _validar_aprobacion(aprobacion)
    if aprobacion.consumed_by_transition_id != propuesta.transition_id:
        raise JournalIdentityError(
            "la aprobación fue consumida por otra transición (consumed_by_transition_id != transition_id)"
        )
    if aprobacion.scope_digest != propuesta.approval_scope_digest:
        raise JournalIdentityError(
            "el scope consumido difiere del approval_scope_digest de la propuesta (cross-check §37.6)"
        )


def _crear_pendiente_locked(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    propuesta: PropuestaTransicion,
    aprobacion: AprobacionConsumida,
) -> TransitionJournal:
    """CREATE — ``ABSENT|NONE → PENDING`` con CAS completo (§36.6).

    Precondiciones: journal ∈ {ABSENT, NONE} ∧ sin PENDING activo ∧
    ``transition_id`` no visto ∧ aprobación YA consumida por esta transición
    (verificada, no producida). La autoridad durable que EMITE la aprobación
    sigue siendo dependencia diferida.
    """
    handle.assert_owned()
    _exigir_dirs_store(root)
    _exigir_precondiciones_comunes(propuesta, aprobacion)
    ruta = transition_journal_path(root)
    snapshot = cargar_journal_transicion(root)
    if snapshot.disposition in (JournalDisposition.PENDING_PROMOTION, JournalDisposition.PENDING_ROLLBACK):
        raise JournalCasError("ya existe un PENDING activo para este root (AT_MOST_ONE_PENDING_PER_ROOT)")
    historial = derivar_historial_finalizados(root)
    # CR-R3-02 — hermana de F4: la autoridad de la secuencia se audita ANTES de
    # escribir el PENDING. Escribirlo sobre una reserva huérfana ajena produce
    # una transición que no puede finalizar (ver ``_exigir_sin_reservas_huerfanas``).
    _exigir_sin_reservas_huerfanas(root, historial)
    if any(registro.transition_id == propuesta.transition_id for registro in historial.registros):
        raise JournalCasError("el transition_id ya fue utilizado por una transición finalizada")
    if snapshot.disposition == JournalDisposition.NONE:
        journal = snapshot.journal
        assert journal is not None  # invariante de lectura
        if historial.registros:
            raise JournalCasError("hay historial finalizado: esta vía es START_NEXT, no CREATE")
        if journal.last_finalized_transition_id is not None:
            raise JournalCasError("journal NONE con testigo pero store vacío: estado incoherente")
        revision = journal.journal_revision + 1
    else:
        if historial.registros:
            raise JournalIdentityError("store con registros pero journal ausente (fail-closed)")
        revision = 0
    doc = _construir_doc_pendiente(
        propuesta=propuesta,
        aprobacion=aprobacion,
        revision=revision,
        lock_session_id=handle.session_id,
    )
    _validar_antes_de_escribir_journal(doc, ruta)
    state_module.write_json_atomic(ruta, _serializar_journal(doc))
    return doc


def _start_next_locked(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    propuesta: PropuestaTransicion,
    aprobacion: AprobacionConsumida,
) -> TransitionJournal:
    """START_NEXT — ``NONE → PENDING`` con LATEST derivado del store (§38.3)."""
    handle.assert_owned()
    _exigir_dirs_store(root)
    _exigir_precondiciones_comunes(propuesta, aprobacion)
    ruta = transition_journal_path(root)
    snapshot = cargar_journal_transicion(root)
    if snapshot.disposition != JournalDisposition.NONE or snapshot.journal is None:
        raise JournalCasError("START_NEXT exige journal en NONE")
    historial = derivar_historial_finalizados(root)
    if not historial.registros or historial.latest is None:
        raise JournalCasError("START_NEXT exige historial finalizado (use CREATE para el bootstrap)")
    # F4 — hermana de FINALIZE: la autoridad de la secuencia se audita antes de
    # arrancar la transición siguiente. Arrancar sobre un store cuyo registro
    # perdió su reserva propagaría la incoherencia a la cadena nueva.
    # CR-R3-02 — y una reserva HUÉRFANA (sin registro) también es autoridad sin
    # dueño: acá no hay PENDING que pueda reclamarla, así que el veredicto es
    # REQUIRE_OWNER y no se escribe el PENDING (el resultado de la auditoría ya
    # no se descarta).
    _exigir_sin_reservas_huerfanas(root, historial)
    latest = historial.latest
    if propuesta.transition_id == latest.transition_id:
        raise JournalCasError("START_NEXT no puede reutilizar el transition_id del predecesor")
    if any(registro.transition_id == propuesta.transition_id for registro in historial.registros):
        raise JournalCasError("el transition_id ya fue utilizado por una transición finalizada")
    if snapshot.journal.last_finalized_transition_id is None:
        raise JournalCasError("journal NONE sin testigo con store no vacío (fail-closed)")
    if snapshot.journal.last_finalized_transition_id != latest.transition_id:
        raise JournalIdentityError("el testigo difiere del LATEST_FINALIZED_RECORD derivado del store")
    doc = _construir_doc_pendiente(
        propuesta=propuesta,
        aprobacion=aprobacion,
        revision=snapshot.journal.journal_revision + 1,
        lock_session_id=handle.session_id,
    )
    _validar_antes_de_escribir_journal(doc, ruta)
    state_module.write_json_atomic(ruta, _serializar_journal(doc))
    return doc


#: Único campo actualizable de un PENDING (§36.6).
#:
#: ``evidence_digests`` (``clone_evidence``, ``runtime_setup_evidence``,
#: ``compatibility_evidence``) NO está acá a propósito: §38.5 lo declara parte
#: del ``ApprovalScope`` —campos 6, 8 y 9—, y §38.5 "Cobertura del digest"
#: establece que ``approval_scope_digest`` compromete los 13 campos restantes.
#: Permitir sustituirlos conservando el ``approval_scope_digest`` consumido
#: dejaba finalizar contenido NUEVO bajo la aprobación del contenido VIEJO
#: (F1). §34.7 es explícito: ``ANY_APPROVED_MATERIAL_CHANGE = NO BIND`` — si la
#: evidencia aprobada cambió, la transición NO avanza; no se "actualiza".
#:
#: ``expected_effective_path`` sí es actualizable: NO figura entre los 14 campos
#: del ``ApprovalScope`` (§38.5), es "la ruta que el bind va a producir"
#: (§34.4) y el scope liga CONTENIDO, no rutas (§34.6).
_CAMPOS_ACTUALIZABLES = frozenset({"expected_effective_path"})


def _actualizar_locked(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    *,
    expected_transition_id: str,
    expected_journal_revision: int,
    cambios: dict[str, object],
) -> TransitionJournal:
    """UPDATE — ``PENDING T → PENDING T`` con CAS sobre identidad y revisión.

    La identidad de la transición (operation, transition_id, source, target,
    aprobación, created_at) es INMUTABLE, y también lo es la evidencia
    aprobada (``evidence_digests``: campos 6/8/9 del ``ApprovalScope``, §38.5).
    Sólo ``expected_effective_path`` puede cambiar, y siempre con revisión + 1.
    """
    handle.assert_owned()
    ruta = transition_journal_path(root)
    snapshot = cargar_journal_transicion(root)
    if snapshot.disposition not in (JournalDisposition.PENDING_PROMOTION, JournalDisposition.PENDING_ROLLBACK):
        raise JournalCasError("no hay un PENDING que actualizar")
    journal = snapshot.journal
    assert journal is not None  # invariante de lectura
    if journal.transition_id != expected_transition_id or journal.journal_revision != expected_journal_revision:
        raise JournalCasError(
            "CAS desactualizado: (transition_id, journal_revision) no coinciden con el documento vigente"
        )
    no_permitidos = set(cambios) - _CAMPOS_ACTUALIZABLES
    if no_permitidos:
        raise JournalIdentityError(
            f"campos no actualizables (identidad inmutable de la transición y evidencia aprobada): "
            f"{sorted(no_permitidos)}"
        )
    expected_path = journal.expected_effective_path
    if "expected_effective_path" in cambios:
        # R3-02: MISMO validador que el parser y la propuesta (§34.4: "la ruta
        # que el bind va a producir"). `not valor` aceptaba `"   "` y una ruta
        # con espacio final, que se persistían como la ruta esperada.
        expected_path = _exigir_texto_valor(
            cambios["expected_effective_path"], campo="expected_effective_path", etiqueta=str(ruta)
        )
    doc = dataclasses.replace(
        journal,
        journal_revision=journal.journal_revision + 1,
        expected_effective_path=expected_path,
    )
    _validar_antes_de_escribir_journal(doc, ruta)
    state_module.write_json_atomic(ruta, _serializar_journal(doc))
    return doc


def _exigir_evidencia_coherente_con_pending(
    journal: TransitionJournal,
    evidencia_post: PostVerifyEvidence,
) -> None:
    """La evidencia POST debe ligar la transición vigente, su revisión y su Desired.

    §35.6: ``FINALIZED`` no puede existir sin evidencia durable que ligue, al
    MISMO: ``{ transition_id, source, target, desired state, effective
    observation }``. §36.6: ``JOURNAL_FINALIZE_CAS = (transition_id,
    journal_revision) ∧ POST evidence PASS``.

    Comparar sólo ``transition_id`` (lo que hacía este camino) aceptaba
    evidencia calculada para la revisión R sobre una transición que ya avanzó a
    R+1 (F2), y evidencia con ``desired_active_generation`` de otra Generation o
    ``None`` (F3): el POST declaraba explícitamente que el estado deseado NO era
    el target y la transición finalizaba igual.
    """
    assert journal.transition_id is not None  # invariante del esquema PENDING
    if journal.transition_id != evidencia_post.transition_id:
        raise JournalEvidenciaPostError("la evidencia POST pertenece a otra transición")
    if str(evidencia_post.resultado) != PostVerifyResultado.PASS.value:
        raise JournalEvidenciaPostError("POST no está en PASS (C5): NO FINALIZA")
    # F2 — CAS anti-ABA (§36.6/§36.9 C6): la evidencia declara la revisión que
    # observó. Una evidencia calculada para R NO finaliza R+1.
    if journal.journal_revision != evidencia_post.expected_journal_revision:
        raise JournalCasError(
            "CAS desactualizado al finalizar: la evidencia POST se calculó sobre la revisión "
            f"{evidencia_post.expected_journal_revision} y el journal vigente está en la revisión "
            f"{journal.journal_revision} (anti-ABA §36.6)"
        )
    for campo_evidencia, campo_journal in (
        ("source_activation_digest", "source_activation_digest"),
        ("target_generation_id", "target_generation_id"),
        ("target_clone_id", "target_clone_id"),
        ("target_runtime_setup_id", "target_runtime_setup_id"),
    ):
        if getattr(evidencia_post, campo_evidencia) != getattr(journal, campo_journal):
            raise JournalEvidenciaPostError(
                f"evidencia POST inconsistente con la transición vigente en '{campo_evidencia}'"
            )
    # F3 — el estado DESEADO también es parte de la identidad que la evidencia
    # liga (§35.6). Para PROMOTION y ROLLBACK el target es la Generation que
    # queda deseada: un Desired ``None`` o de otra Generation NO demuestra la
    # transición y no puede producir un registro FINALIZED.
    if evidencia_post.desired_active_generation != journal.target_generation_id:
        raise JournalEvidenciaPostError(
            "evidencia POST inconsistente con la transición vigente en 'desired_active_generation' "
            f"({evidencia_post.desired_active_generation!r} != {journal.target_generation_id!r}): "
            "§35.6 exige que la evidencia ligue el estado deseado"
        )


def _exigir_registro_coherente_con_pending(
    registro: FinalizedTransitionRecord,
    journal: TransitionJournal,
    historial: HistorialFinalizado,
) -> None:
    """El registro FINALIZED existente debe ser EXACTAMENTE el que el PENDING describe.

    §36.9 C6: al repetir el cierre hay que exigir "mismo ``transition_id`` ∧
    mismo source/target ∧ mismo Desired ∧ mismo Effective". Comparar sólo
    ``transition_id``, digest POST y timestamp aceptaba un registro de forma
    válida pero de OTRO estado (operación, source, target, aprobación o
    secuencia distintos) —mezcla de backups o corrupción verosímil— y después
    rotaba el journal, volviendo autoridad a un historial que contradice la
    transición pendiente (F7). El registro inmutable NO se reemplaza para
    "corregir" la incompatibilidad: se falla cerrado.
    """
    discrepancias: list[str] = []
    for nombre, valor_registro, valor_journal in (
        ("operation", registro.operation, journal.operation),
        ("source_activation", registro.source_activation, journal.source_activation),
        ("source_activation_digest", registro.source_activation_digest, journal.source_activation_digest),
        ("target_generation_id", registro.target_generation_id, journal.target_generation_id),
        ("target_clone_id", registro.target_clone_id, journal.target_clone_id),
        ("target_runtime_setup_id", registro.target_runtime_setup_id, journal.target_runtime_setup_id),
        ("approval_scope_digest", registro.approval_scope_digest, journal.approval_scope_digest),
        ("consumed_approval_id", registro.consumed_approval_id, journal.consumed_approval_id),
        ("consumed_approval_seq", registro.consumed_approval_seq, journal.consumed_approval_seq),
    ):
        if valor_registro != valor_journal:
            discrepancias.append(nombre)
    if historial.latest is None or historial.latest.transition_id != registro.transition_id:
        discrepancias.append("finalization_seq (el registro no es el tip de la cadena derivada)")
    if discrepancias:
        raise JournalIdentityError(
            f"el registro FINALIZED de {registro.transition_id} NO corresponde a la transición pendiente: "
            f"difieren {sorted(discrepancias)}. No se reescribe el registro inmutable para corregirlo."
        )


def _finalizar_locked(
    handle: FrozenRuntimeRootLockHandle,
    root: pathlib.Path,
    evidencia_post: PostVerifyEvidence,
) -> FinalizedTransitionRecord:
    """FINALIZE — F1a reserva → F1b registro → F2 rotación (§37.5).

    Exige, además del CAS, evidencia POST en PASS y consistente con la
    transición vigente (§35.6/§36.6). ``FINALIZED_WITHOUT_POST`` es imposible
    por construcción: sin evidencia válida no se llega a F1.
    """
    handle.assert_owned()
    _exigir_dirs_store(root)
    if not isinstance(evidencia_post, PostVerifyEvidence):
        raise JournalEvidenciaPostError("la evidencia POST debe ser un PostVerifyEvidence")
    _validar_post_evidence(evidencia_post)
    snapshot = cargar_journal_transicion(root)
    if snapshot.disposition == JournalDisposition.ABSENT:
        raise JournalCasError("no hay journal vigente: nada que finalizar con esta evidencia")
    journal = snapshot.journal
    assert journal is not None  # invariante de lectura
    # El registro inmutable es la autoridad de la finalización: si YA existe uno
    # para esta transición (crash entre F1b y F2, o reintento), no se reescribe
    # nunca — sólo se completa la rotación. NEXT se deriva SIEMPRE del store
    # validado, jamás del journal, para no bifurcar la cadena de seq.
    historial = derivar_historial_finalizados(root)
    # F4 — la autoridad de la secuencia se audita ANTES de ampliar el historial
    # (§38.3: "registro sin reserva ⇒ INCONSISTENT_AUTHORITY ⇒ FAIL_CLOSED").
    # Si un registro retenido perdió su reserva, la cadena YA no es autoridad y
    # FINALIZE no puede avanzar sobre ella dejando un store que sólo la
    # reconciliación detectaría.
    auditar_reservas_finalizacion(root, historial)
    registro_existente = next(
        (registro for registro in historial.registros if registro.transition_id == evidencia_post.transition_id),
        None,
    )
    if registro_existente is not None:
        if (
            registro_existente.post_verify_evidence_digest != evidencia_post.payload_digest
            or registro_existente.post_verified_at_ns != evidencia_post.verified_at_ns
        ):
            raise JournalEvidenciaPostError(
                "la evidencia POST no coincide con el registro inmutable: no se sobrescribe historial"
            )
        if str(evidencia_post.resultado) != PostVerifyResultado.PASS.value:
            raise JournalEvidenciaPostError("POST no está en PASS (C5): NO FINALIZA")
        if snapshot.disposition == JournalDisposition.NONE:
            if journal.last_finalized_transition_id != evidencia_post.transition_id:
                raise JournalCasError("el journal en NONE no corresponde a la transición de la evidencia")
            return registro_existente  # reintento idempotente: nada que mutar
        # C7: registro inmutable con journal PENDING — la única reconciliación
        # admitida es completar F2 con el registro que ya existe. Antes de rotar
        # hay que probar que el registro ES el que la transición pendiente
        # describe (F7) y que la evidencia sigue siendo la de ESTA revisión (F2).
        _exigir_registro_coherente_con_pending(registro_existente, journal, historial)
        _exigir_evidencia_coherente_con_pending(journal, evidencia_post)
        _rotar_journal_locked(handle, root, registro=registro_existente, revision_previa=journal.journal_revision)
        return registro_existente
    if snapshot.disposition == JournalDisposition.NONE:
        raise JournalCasError("el testigo apunta a una transición sin registro inmutable")
    _exigir_evidencia_coherente_con_pending(journal, evidencia_post)
    # Acá el journal vigente ES un PENDING (ya se descartaron ABSENT y NONE):
    # la esquema cerrada del parser garantiza estos campos. Se afirman para
    # no propagar ``str | None`` al registro inmutable.
    assert journal.operation is not None
    assert journal.transition_id is not None
    assert journal.source_activation is not None
    assert journal.source_activation_digest is not None
    assert journal.target_generation_id is not None
    assert journal.target_clone_id is not None
    assert journal.target_runtime_setup_id is not None
    assert journal.approval_scope_digest is not None
    assert journal.consumed_approval_id is not None
    assert journal.consumed_approval_seq is not None
    registro = FinalizedTransitionRecord(
        schema_version=RECORD_SCHEMA_VERSION,
        record_state=RecordState.FINALIZED,
        transition_id=journal.transition_id,
        finalization_seq=historial.next_seq,
        finalized_from_transition_id=historial.latest.transition_id if historial.latest else None,
        operation=journal.operation,
        source_activation=journal.source_activation,
        source_activation_digest=journal.source_activation_digest,
        previous_activation_target=journal.source_activation,
        target_generation_id=journal.target_generation_id,
        target_clone_id=journal.target_clone_id,
        target_runtime_setup_id=journal.target_runtime_setup_id,
        approval_scope_digest=journal.approval_scope_digest,
        consumed_approval_id=journal.consumed_approval_id,
        consumed_approval_seq=journal.consumed_approval_seq,
        post_verify_evidence_digest=evidencia_post.payload_digest,
        post_verified_at_ns=evidencia_post.verified_at_ns,
        finalized_at_ns=time.time_ns(),
    )
    _reservar_seq_locked(handle, root, seq=registro.finalization_seq, transition_id=registro.transition_id)
    registro = _persistir_registro_locked(handle, root, registro)
    _rotar_journal_locked(handle, root, registro=registro, revision_previa=journal.journal_revision)
    return registro


# ---------------------------------------------------------------------------
# Envoltorios que adquieren el root lock (frontera de adquisición)
# ---------------------------------------------------------------------------


def _crear_pendiente_bajo_lock(
    root: pathlib.Path,
    propuesta: PropuestaTransicion,
    aprobacion: AprobacionConsumida,
) -> TransitionJournal:
    """CREATE bajo el root lock real. Interno: P4-S3 no tiene autoridad de
    aprobación que produzca ``aprobacion`` en producción (ver
    ``iniciar_transicion_pendiente``)."""
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    ruta = pathlib.Path(root)
    with participant_lock(ruta, timeout=0.0) as handle:
        handle.assert_owned()
        return _crear_pendiente_locked(handle, ruta, propuesta, aprobacion)


def _start_next_bajo_lock(
    root: pathlib.Path,
    propuesta: PropuestaTransicion,
    aprobacion: AprobacionConsumida,
) -> TransitionJournal:
    """START_NEXT bajo el root lock real. Interno (misma dependencia que CREATE)."""
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    ruta = pathlib.Path(root)
    with participant_lock(ruta, timeout=0.0) as handle:
        handle.assert_owned()
        return _start_next_locked(handle, ruta, propuesta, aprobacion)


def _actualizar_bajo_lock(
    root: pathlib.Path,
    *,
    expected_transition_id: str,
    expected_journal_revision: int,
    cambios: dict[str, object],
) -> TransitionJournal:
    """UPDATE bajo el root lock real."""
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    ruta = pathlib.Path(root)
    with participant_lock(ruta, timeout=0.0) as handle:
        handle.assert_owned()
        return _actualizar_locked(
            handle,
            ruta,
            expected_transition_id=expected_transition_id,
            expected_journal_revision=expected_journal_revision,
            cambios=cambios,
        )


def _finalizar_transicion_bajo_lock(
    root: pathlib.Path,
    evidencia_post: PostVerifyEvidence,
) -> FinalizedTransitionRecord:
    """FINALIZE (F1a/F1b/F2) bajo el root lock real.

    Interno: la producción no puede alcanzarlo en P4-S3 porque no puede crear
    el PENDING que exige (dependencia de aprobación diferida) ni producir la
    evidencia POST (verificador de P5, diferido).
    """
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    ruta = pathlib.Path(root)
    with participant_lock(ruta, timeout=0.0) as handle:
        handle.assert_owned()
        return _finalizar_locked(handle, ruta, evidencia_post)


# ---------------------------------------------------------------------------
# Superficie pública bloqueada (fail-closed por dependencia ausente)
# ---------------------------------------------------------------------------


def iniciar_transicion_pendiente(root: pathlib.Path, propuesta: PropuestaTransicion) -> TransitionJournal:
    """BLOQUEADA en P4-S3.

    ``PENDING_WITHOUT_APPROVAL_PROOF = PROHIBIDO`` (§36.5/§37.3): la creación
    de un PENDING de producción exige una aprobación YA CONSUMED, verificada
    contra una autoridad durable que este slice no implementa
    (``state/approval.json`` + issuance witnesses + terminal store, §38.2).
    No se sustituye por un booleano ni por una aprobación simulada.
    """
    raise JournalDependenciaNoDisponibleError(
        "P4-S3 no implementa la autoridad durable de aprobación consumida "
        "(§36.5/§37.3/§38.2): la creación de un PENDING de producción permanece BLOQUEADA. "
        "No hay evidencia de consumo que verificar, así que no se persiste nada."
    )


def iniciar_transicion_rollback(root: pathlib.Path, propuesta: PropuestaTransicion) -> TransitionJournal:
    """BLOQUEADA en P4-S3 (misma dependencia que ``iniciar_transicion_pendiente``).

    El rollback además exige procedencia verificable del target inmediato
    anterior (§37.5/§38.6), que el store todavía no puede demostrar.
    """
    raise JournalDependenciaNoDisponibleError(
        "P4-S3 no implementa la autoridad durable de aprobación consumida ni la "
        "procedencia de rollback (§37.5/§38.6): la creación de un PENDING_ROLLBACK "
        "de producción permanece BLOQUEADA. No hay evidencia de consumo que verificar."
    )


# ---------------------------------------------------------------------------
# Reconciliación de arranque (observa y clasifica; NUNCA muta)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ObservacionEffective:
    """Observación TIPADA del Effective Runtime (oráculo de P5, §34.5).

    Vocabulario ÚNICO para N0 y PENDING. Antes el callback devolvía un
    ``str | None`` que N0 comparaba contra una **Generation** y PENDING contra
    una **ruta de Clone**: el mismo callback no podía ser las dos cosas, y la
    igualdad de Generation se usaba como prueba de identidad efectiva (F10).
    La observación declara sus dos facetas:

    - ``generation_id`` — la Generation que el runtime declara ejecutar.
    - ``clone_id`` — la identidad del Runtime Clone efectivo.
    - ``effective_path`` — la ruta que el bind produce (§34.4).

    ``None`` en un campo = esa faceta quedó INDETERMINADA. ``None`` como
    observación entera = el oráculo no pudo observar el runtime.
    """

    generation_id: str | None
    clone_id: str | None
    effective_path: str | None


def _invocar_oraculo_effective(
    observador_effective: Callable[[], ObservacionEffective | None],
    *,
    punto_crash: str,
) -> ObservacionEffective | ReconciliacionJournal:
    """Invoca el oráculo y NORMALIZA los fallos ESPERADOS a INDETERMINADO.

    Contrato del callback (§34.5): el oráculo es externo (P5). Sus fallos
    **esperados** de observación se señalan con ``JournalObservacionEffectiveError``;
    los de IO/filesystem y los de identidad también son esperables. Todos ellos
    se traducen a ``FAIL_CLOSED``/INDETERMINADO: ``UNKNOWN != SUCCESS``.

    Lo que NO se hace acá es tragarse lo inesperado. Un `except Exception` global
    convertía cualquier fallo —incluido un `AttributeError` por un bug de
    programación en el callback— en el mismo "no se pudo observar", sin dejar
    rastro. Ahora esos fallos **se propagan** hasta la frontera única de
    ``reconciliar_journal``, que los REGISTRA con traceback y también falla
    cerrado, con un motivo distinto (R3-03).
    """
    try:
        observado = observador_effective()
    except JournalObservacionEffectiveError as exc:
        return _fail_closed(punto_crash, f"EFFECTIVE_INDETERMINADO: {exc}")
    except (OSError, JournalIdentityError, InvalidGenerationIdError, StateCorruptError, StateSchemaError) as exc:
        # Esperables también: el oráculo lee el filesystem y la identidad del
        # runtime. Se traducen a INDETERMINADO con el tipo en el motivo, para que
        # un fallo de IO se distinga de un bug del callback.
        return _fail_closed(punto_crash, f"EFFECTIVE_INDETERMINADO({type(exc).__name__}): {exc}")
    if observado is None:
        return _fail_closed(punto_crash, "EFFECTIVE_INDETERMINADO: el oráculo no pudo determinar el runtime efectivo")
    if not isinstance(observado, ObservacionEffective):
        return _fail_closed(
            punto_crash,
            f"EFFECTIVE_NO_TIPADO: el oráculo devolvió {type(observado).__name__}; se exige "
            "ObservacionEffective (vocabulario único N0/PENDING)",
        )
    return observado


def _clone_activo_esperado(historial: HistorialFinalizado, generation_id: str) -> str | None:
    """Identidad del Runtime Clone que la autoridad durable registra para *generation_id*.

    P4-S3 no implementa el store de ``RuntimeCloneRecord`` (P4/P5 diferido), así
    que la única autoridad durable disponible sobre qué Clone quedó activo es el
    registro FINALIZED más reciente (§37.5): si su target es esa Generation, el
    Clone activo es su ``target_clone_id``. Sin ese registro —o si el tip
    pertenece a otra Generation— no hay demostración posible y se devuelve
    ``None``: el caller falla INDETERMINADO, nunca ``COMPLETE``.
    """
    ultimo = historial.latest
    if ultimo is None or ultimo.target_generation_id != generation_id:
        return None
    return ultimo.target_clone_id


def _veredicto_n0_n1(
    deseado: FrozenRuntimeState | None,
    historial: HistorialFinalizado,
    observador_effective: Callable[[], ObservacionEffective | None] | None,
) -> ReconciliacionJournal:
    """N0/N1 exigiendo identidad de Runtime Clone DEMOSTRADA (§34.5, §36.4).

    ``N0_COMPLETE_REQUIRES_PROVEN_IDENTITY = YES``. La igualdad de Generation
    entre Desired y Effective NO demuestra la coherencia del Clone efectivo
    (§35.4: la integridad del Clone es física). Además de la igualdad de
    Generation, N0 exige que la identidad del Clone observada coincida con la
    que la autoridad durable registra para ese Desired. Si P4-S3 no puede
    resolverla, el veredicto es INDETERMINADO — nunca ``COMPLETE``.
    """
    if observador_effective is None:
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.UNKNOWN_EFFECTIVE,
            punto_crash="N0/N1",
            motivo="ORACULO_EFFECTIVE_NO_DISPONIBLE (P5): no se puede afirmar N0 ni N1",
            requiere_dueno=False,
            registro_finalizado_presente=None,
        )
    observado = _invocar_oraculo_effective(observador_effective, punto_crash="N0/N1")
    if isinstance(observado, ReconciliacionJournal):
        return observado
    deseado_generacion = getattr(deseado, "desired_active_generation", None)
    if observado.generation_id is None:
        # El oráculo no determinó la Generation: INDETERMINADO. Se verifica
        # ANTES de la igualdad porque ``None == None`` sería verdadero y
        # convertiría dos indeterminados en COMPLETE (UNKNOWN != SUCCESS, §34.5).
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.FAIL_CLOSED,
            punto_crash="N1",
            motivo="EFFECTIVE_INDETERMINADO: el oráculo no determinó la Generation efectiva",
            requiere_dueno=True,
            registro_finalizado_presente=None,
        )
    if deseado_generacion != observado.generation_id:
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.FAIL_CLOSED,
            punto_crash="N1",
            motivo="INCONSISTENT_BASELINE: sin transición y Desired != Effective",
            requiere_dueno=True,
            registro_finalizado_presente=None,
        )
    clone_esperado = _clone_activo_esperado(historial, observado.generation_id)
    if clone_esperado is None:
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.UNKNOWN_EFFECTIVE,
            punto_crash="N0",
            motivo=(
                "IDENTIDAD_DE_CLONE_NO_DEMOSTRABLE: Desired == Effective en Generation, pero P4-S3 no "
                "puede resolver la identidad del Runtime Clone del Desired (no hay registro FINALIZED "
                "para esa Generation). UNKNOWN != SUCCESS (§34.5): no se declara COMPLETE."
            ),
            requiere_dueno=False,
            registro_finalizado_presente=None,
        )
    if observado.clone_id is None:
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.FAIL_CLOSED,
            punto_crash="N0",
            motivo="EFFECTIVE_INDETERMINADO: el oráculo no determinó la identidad del Runtime Clone",
            requiere_dueno=True,
            registro_finalizado_presente=None,
        )
    if observado.clone_id != clone_esperado:
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.FAIL_CLOSED,
            punto_crash="N0",
            motivo=(
                "INCONSISTENT_BASELINE: Desired == Effective en Generation, pero el Runtime Clone "
                f"observado ({observado.clone_id!r}) no es el registrado para ese Desired ({clone_esperado!r})"
            ),
            requiere_dueno=True,
            registro_finalizado_presente=None,
        )
    return ReconciliacionJournal(
        veredicto=VeredictoReconciliacion.COMPLETE,
        punto_crash="N0",
        motivo="Desired == Effective (Generation y Runtime Clone), sin transición: coherente",
        requiere_dueno=False,
        registro_finalizado_presente=None,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _EstadoDurable:
    """Snapshot causalmente coherente del estado durable (tomado bajo el lock)."""

    snapshot: JournalLoadResult
    historial: HistorialFinalizado
    auditoria: AuditoriaReservas
    deseado: FrozenRuntimeStateLoadResult
    huella: tuple[object, ...]


def _huella_estado_durable(
    snapshot: JournalLoadResult,
    historial: HistorialFinalizado,
    auditoria: AuditoriaReservas,
    deseado: FrozenRuntimeStateLoadResult,
) -> tuple[object, ...]:
    """Huella de TODAS las fuentes que la clasificación consulta.

    Sirve para revalidar, tras la observación externa, que la autoridad durable
    no derivó: si la huella cambió, el veredicto correspondería a un estado que
    ya no existe y NO puede devolverse como ``COMPLETE``.
    """
    journal = snapshot.journal
    estado_deseado = getattr(deseado, "state", None)
    return (
        snapshot.disposition.value,
        None if journal is None else journal.journal_revision,
        None if journal is None else journal.transition_id,
        None if journal is None else journal.last_finalized_transition_id,
        None if journal is None else journal.expected_effective_path,
        historial.next_seq,
        tuple(registro.transition_id for registro in historial.registros),
        tuple((reserva.finalization_seq, reserva.transition_id) for reserva in auditoria.reservas),
        getattr(deseado, "found", None),
        None if estado_deseado is None else estado_deseado.desired_active_generation,
    )


def _observar_estado_durable(root: pathlib.Path) -> _EstadoDurable:
    """Lectura COHERENTE de journal + historial + reservas + Desired.

    Debe correr BAJO el root lock: sin él, las cuatro fuentes se leen en
    instantes distintos y la clasificación mezcla generaciones de estado —una
    transición creada entre dos lecturas es invisible para el snapshot— (F8).
    Propaga los errores tipados del dominio; el caller los clasifica.
    """
    snapshot = cargar_journal_transicion(root)
    historial = derivar_historial_finalizados(root)
    auditoria = auditar_reservas_finalizacion(root, historial)
    deseado = load_frozen_runtime_state(active_state_path(root))
    return _EstadoDurable(
        snapshot=snapshot,
        historial=historial,
        auditoria=auditoria,
        deseado=deseado,
        huella=_huella_estado_durable(snapshot, historial, auditoria, deseado),
    )


def _fail_closed(punto_crash: str | None, motivo: str) -> ReconciliacionJournal:
    return ReconciliacionJournal(
        veredicto=VeredictoReconciliacion.FAIL_CLOSED,
        punto_crash=punto_crash,
        motivo=motivo,
        requiere_dueno=True,
        registro_finalizado_presente=None,
    )


def _clasificar_reconciliacion(
    estado: _EstadoDurable,
    observador_effective: Callable[[], ObservacionEffective | None] | None,
) -> ReconciliacionJournal:
    """Clasifica un snapshot durable ya tomado (§34.5/§36.9). Función PURA.

    El oráculo de P5 se invoca acá, es decir FUERA del root lock (§34.3/§39.4:
    no se sostiene el lock durante una llamada externa y potencialmente lenta).
    """
    snapshot = estado.snapshot
    historial = estado.historial
    auditoria = estado.auditoria
    deseado = estado.deseado

    if snapshot.disposition == JournalDisposition.ABSENT:
        if historial.registros or auditoria.huerfanas or auditoria.reservas:
            return ReconciliacionJournal(
                veredicto=VeredictoReconciliacion.FAIL_CLOSED,
                punto_crash="N2",
                motivo="HISTORIAL_O_RESERVAS_SIN_JOURNAL: evidencia huérfana (fail-closed)",
                requiere_dueno=True,
                registro_finalizado_presente=None,
            )
        return _veredicto_n0_n1(deseado.state, historial, observador_effective)

    journal = snapshot.journal
    assert journal is not None  # invariante de lectura

    if snapshot.disposition == JournalDisposition.NONE:
        testigo = journal.last_finalized_transition_id
        if historial.registros:
            if testigo is None:
                return ReconciliacionJournal(
                    veredicto=VeredictoReconciliacion.FAIL_CLOSED,
                    punto_crash="N2",
                    motivo="JOURNAL_NONE_SIN_TESTIGO_CON_STORE_NO_VACIO",
                    requiere_dueno=True,
                    registro_finalizado_presente=None,
                )
            if historial.latest is None or testigo != historial.latest.transition_id:
                return ReconciliacionJournal(
                    veredicto=VeredictoReconciliacion.FAIL_CLOSED,
                    punto_crash="N2",
                    motivo="TESTIGO_DIFIERE_DEL_STORE_DERIVADO",
                    requiere_dueno=True,
                    registro_finalizado_presente=None,
                )
        elif testigo is not None:
            return ReconciliacionJournal(
                veredicto=VeredictoReconciliacion.FAIL_CLOSED,
                punto_crash="N2",
                motivo="TESTIGO_SIN_REGISTROS_EN_EL_STORE",
                requiere_dueno=True,
                registro_finalizado_presente=None,
            )
        if auditoria.huerfanas:
            return ReconciliacionJournal(
                veredicto=VeredictoReconciliacion.REQUIRE_OWNER,
                punto_crash="C6",
                motivo=f"RESERVA_HUERFANA_SIN_TRANSICION_PENDIENTE: {auditoria.huerfanas}",
                requiere_dueno=True,
                registro_finalizado_presente=None,
            )
        return _veredicto_n0_n1(deseado.state, historial, observador_effective)

    # PENDING_*
    assert journal.transition_id is not None  # invariante de lectura
    registro_vigente = next(
        (registro for registro in historial.registros if registro.transition_id == journal.transition_id),
        None,
    )
    if registro_vigente is not None:
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.REGISTRO_FINALIZADO_CON_JOURNAL_PENDING,
            punto_crash="C7",
            motivo=(
                "REGISTRO_INMUTABLE_EXISTE_CON_JOURNAL_PENDING (crash entre F1b y F2): la transición "
                "ESTÁ finalizada; ambas evidencias se preservan y la única reconciliación admitida "
                "es completar la rotación (F2)"
            ),
            requiere_dueno=False,
            registro_finalizado_presente=True,
        )
    if auditoria.huerfanas:
        for seq, tid in auditoria.huerfanas:
            if tid == journal.transition_id:
                return ReconciliacionJournal(
                    veredicto=VeredictoReconciliacion.RECOVER,
                    punto_crash="C6",
                    motivo=f"RESERVA_HUERFANA_PARA_LA_TRANSICION_PENDIENTE (seq {seq})",
                    requiere_dueno=False,
                    registro_finalizado_presente=False,
                )
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.REQUIRE_OWNER,
            punto_crash="C6",
            motivo=f"RESERVA_HUERFANA_DE_OTRA_TRANSICION: {auditoria.huerfanas}",
            requiere_dueno=True,
            registro_finalizado_presente=False,
        )
    if observador_effective is None:
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.UNKNOWN_EFFECTIVE,
            punto_crash="C1..C6",
            motivo="PENDING_SIN_REGISTRO: requiere oráculo Effective (P5) y verificador POST",
            requiere_dueno=False,
            registro_finalizado_presente=False,
        )
    observado = _invocar_oraculo_effective(observador_effective, punto_crash="C2")
    if isinstance(observado, ReconciliacionJournal):
        return dataclasses.replace(observado, registro_finalizado_presente=False)
    if observado.effective_path is None:
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.FAIL_CLOSED,
            punto_crash="C2",
            motivo="EFFECTIVE_INDETERMINADO durante el bind: el oráculo no determinó la ruta del Clone",
            requiere_dueno=True,
            registro_finalizado_presente=False,
        )
    if observado.effective_path == journal.expected_effective_path:
        # §34.5: "PENDING_*, Effective coincide con expected_effective_path,
        # POST no corrido ⇒ re-correr POST". La identidad que hay que demostrar
        # es la del Runtime Clone (la ruta que el bind produce, §34.4); la
        # Generation a secas no la demuestra (§35.4: integridad física del Clone).
        return ReconciliacionJournal(
            veredicto=VeredictoReconciliacion.RECOVER,
            punto_crash="C6",
            motivo=(
                "EFFECTIVE_COINCIDE_CON_EXPECTED_PATH: re-correr POST idempotente y finalizar sólo con "
                "mismo transition_id ∧ mismo source/target ∧ misma evidencia en PASS"
            ),
            requiere_dueno=False,
            registro_finalizado_presente=False,
        )
    return ReconciliacionJournal(
        veredicto=VeredictoReconciliacion.REQUIRE_OWNER,
        punto_crash="C1",
        motivo=(
            "EFFECTIVE_NO_COINCIDE_CON_EXPECTED_PATH: la re-verificación de evidencia objetivo "
            "(P4_REVERIFY_AFTER_APPROVAL) es dependencia diferida"
        ),
        requiere_dueno=True,
        registro_finalizado_presente=False,
    )


def reconciliar_journal(
    root: pathlib.Path,
    *,
    observador_effective: Callable[[], ObservacionEffective | None] | None = None,
) -> ReconciliacionJournal:
    """Clasifica el estado del journal al arrancar (§34.5/§36.9).

    Reglas no negociables: corrupción jamás se lee como ``NONE``; ``UNKNOWN``
    jamás se convierte en éxito; un ``PENDING`` no se revierte automáticamente;
    si un registro FINALIZED existe con el journal en PENDING, se preservan
    AMBAS evidencias y la única reconciliación admitida es completar la
    rotación. Esta función NO muta evidencia: sólo lee y clasifica.

    **Coherencia causal (F8).** El estado durable (journal + historial +
    reservas + Desired) se lee en un único snapshot BAJO el root lock; el
    oráculo de P5 se invoca DESPUÉS, fuera del lock (§34.3/§39.4: no se sostiene
    el lock durante una llamada externa lenta); y antes de devolver se
    re-adquiere el lock y se compara la huella durable. Si cambió —una
    transición concurrente creada mientras se observaba el Effective— el
    veredicto es ``FAIL_CLOSED``: nunca ``COMPLETE`` sobre un estado anterior.

    ``observador_effective`` es el oráculo de P5: devuelve una
    ``ObservacionEffective`` (vocabulario ÚNICO para N0 y PENDING), o ``None`` si
    no pudo observar el runtime (indeterminado ⇒ ``FAIL_CLOSED``, nunca éxito).

    Nota de alcance: adquirir el root lock materializa ``state/`` vacío si el
    root no estaba inicializado. No se altera ninguna evidencia —journal,
    registros y reservas siguen ausentes— y la clasificación no cambia.
    """
    ruta = pathlib.Path(root)
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockError
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    # (1) Snapshot durable BAJO el root lock: una observación causalmente coherente.
    try:
        with participant_lock(ruta, timeout=0.0) as handle:
            handle.assert_owned()
            estado = _observar_estado_durable(ruta)
    except (StateCorruptError, StateSchemaError, InvalidGenerationIdError) as exc:
        # F11: `InvalidGenerationIdError` es hermana de `StateCorruptError` (ambas
        # `FrozenRuntimeStorageError`), NO subclase: sin incluirla, un `active.json`
        # con un generation-id sintácticamente inválido ESCAPABA de
        # `reconciliar_journal`, abortando el barrido de roots en vez de devolver
        # el resultado tipado. Se capturan sólo errores de dominio: no un `except
        # Exception` global.
        return _fail_closed(None, f"DESIRED_ILEGIBLE: {exc}")
    except (JournalCorruptError, JournalSchemaError, JournalIdentityError) as exc:
        return _fail_closed("N2", f"JOURNAL_CORRUPTO_O_ILEGIBLE: {exc}")
    except JournalStoreNoInicializadoError as exc:
        return _fail_closed("N2", f"STORE_INCONSISTENTE: {exc}")
    except FrozenRuntimeLockError as exc:
        return _fail_closed(None, f"ROOT_LOCK_NO_ADQUIRIDO: {exc}")

    # (2) Clasificación: el oráculo externo corre FUERA del lock.
    # FRONTERA ÚNICA de los errores INESPERADOS del oráculo (R3-03). El bloque (1)
    # enumera errores de dominio porque el estado durable SÍ es enumerable; el
    # oráculo es código EXTERNO y no lo es. `_invocar_oraculo_effective` traduce
    # sólo los fallos ESPERADOS; lo demás se propaga hasta acá, se REGISTRA con
    # traceback —para que un bug de programación del callback no quede enmascarado
    # como "no se pudo observar"— y también falla cerrado, con motivo propio.
    # §34.5 sigue intacto: ninguna excepción del oráculo puede producir éxito.
    try:
        veredicto = _clasificar_reconciliacion(estado, observador_effective)
    except Exception:  # noqa: BLE001 — frontera superior ÚNICA y deliberada: el oráculo es externo
        logger.exception("reconciliación del journal: excepción INESPERADA del oráculo Effective en %s", ruta)
        return _fail_closed(None, "ORACULO_FALLO_INESPERADO: excepción no tipada al observar el Effective")

    # (3) Revalidación bajo el lock: la autoridad durable no puede haber derivado.
    try:
        with participant_lock(ruta, timeout=0.0) as handle:
            handle.assert_owned()
            huella_actual = _observar_estado_durable(ruta).huella
    except (
        JournalCorruptError,
        JournalSchemaError,
        JournalIdentityError,
        JournalStoreNoInicializadoError,
        StateCorruptError,
        StateSchemaError,
        InvalidGenerationIdError,
        FrozenRuntimeLockError,
    ) as exc:
        return _fail_closed(None, f"REVALIDACION_IMPOSIBLE: {exc}")
    if huella_actual != estado.huella:
        return _fail_closed(
            None,
            "AUTORIDAD_CAMBIADA_DURANTE_LA_RECONCILIACION: el estado durable derivó mientras se "
            "observaba el Effective; la clasificación correspondería a un estado anterior (fail-closed)",
        )
    return veredicto
