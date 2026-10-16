"""Excepciones tipadas del subsistema Frozen Runtime (P1: Managed Source).

P1 es READ-ONLY sobre la Managed Source y fail-closed: toda condición
inobservable o ambigua se materializa como excepción tipada o estado
explícito, nunca como un valor por defecto silencioso.
"""

from __future__ import annotations


class FrozenRuntimeError(Exception):
    """Base de errores de Frozen Runtime."""


class MalformedVdfError(FrozenRuntimeError):
    """Entrada VDF/ACF sintácticamente inválida o con codificación ilegible."""


class DuplicateKeyVdfError(MalformedVdfError):
    """Clave duplicada en el mismo scope VDF/ACF (política explícita: fail-closed)."""


class ProviderEvidenceError(FrozenRuntimeError):
    """Sin evidencia del proveedor para etiquetar la ruta como Managed Source.

    Una copia externa con ``SkyrimSE.exe`` NO es una Managed Source de Steam:
    la identidad de proveedor requiere evidencia del proveedor (library de
    Steam + ``appmanifest_<appid>.acf``).
    """


class AmbiguousManagedSourceError(FrozenRuntimeError):
    """Múltiples candidatos válidos y distintos de Managed Source (sin desempate)."""


class FrozenRuntimeObservationError(FrozenRuntimeError):
    """No se pudo observar (identidad/inventario/metadata) la Managed Source."""


# ============================================================================
# Storage (P2)
# ============================================================================


class FrozenRuntimeStorageError(FrozenRuntimeError):
    """Base de errores de storage de Frozen Runtime (P2)."""


class StateCorruptError(FrozenRuntimeStorageError):
    """Estado persistente malformado, truncado o ilegible (fail-closed).

    Distingue ``state absent`` (arranque limpio) de ``state present but
    corrupt`` (jamás se cae silenciosamente a "sin active generation").
    """


class StateSchemaError(FrozenRuntimeStorageError):
    """Estado con schema desconocido o campos inválidos (fail-closed)."""


class InvalidGenerationIdError(FrozenRuntimeStorageError):
    """generation-id con caracteres, formato, traversal o forma reservada inválida."""


class GenerationCollisionError(FrozenRuntimeStorageError):
    """generation-id existente cuya identidad completa difiere (fail-closed).

    La idempotencia exige que full tree digest + runtime identity + critical
    evidence coincidan; si no, nunca se sobreescribe en silencio.
    """


# ============================================================================
# Candidates (P3)
# ============================================================================


class InvalidCandidateIdError(FrozenRuntimeStorageError):
    """candidate-id con formato, traversal o forma reservada invalido."""


class CandidateError(FrozenRuntimeError):
    """Base de fallos del ciclo de vida de un Candidate (P3).

    Se materializan como excepcion o como estado INVALID persistido con su
    motivo: nunca como un ``False`` ambiguo que el caller pueda interpretar
    como "copia ok".
    """


class CandidateCopyError(CandidateError):
    """La copia fisica independiente fallo (no se produjo un Candidate usable)."""


class CandidateVerificationError(CandidateError):
    """Un Candidate no supera la verificacion fresca contra PRE/POST."""


class CandidateIdCollisionError(CandidateError):
    """El ``candidate_id`` ya esta reservado por un Candidate existente.

    Se lanza desde la RESERVA del id, que ocurre ANTES de cualquier escritura de
    metadata, para que una colision de ids no pueda pisar un Candidate ya
    construido. Es un `CandidateError` y no un error de verificacion: el
    candidato que existia sigue siendo valido.
    """


class CandidateCorruptMetadataError(FrozenRuntimeStorageError):
    """Metadata de Candidate ausente en el payload, corrupta o de schema desconocido.

    Fail-closed en el sentido fuerte: nunca se interpreta como "sin Candidate"
    ni como "Candidate valido". Un Candidate sin metadata legible es UNKNOWN y
    jamas READY.
    """


# ============================================================================
# Root Lock (P4)
# ============================================================================


class FrozenRuntimeLockError(FrozenRuntimeError):
    """Base de errores del lock interproceso por root de Frozen Runtime (P4)."""


class FrozenRuntimeLockBusyError(FrozenRuntimeLockError):
    """El lock está actualmente ocupado por otro proceso o hilo (o timeout expirado)."""


class FrozenRuntimeLockOwnershipError(FrozenRuntimeLockError):
    """Fallo en la prueba de propiedad del lock (assert_owned, liberación ajena o metadata alterada)."""


class FrozenRuntimeLockMetadataError(FrozenRuntimeLockError):
    """Metadata del lock ilegible, corrupta o que no cumple el esquema normativo cerrado."""


class FrozenRuntimeLockIndeterminateError(FrozenRuntimeLockError):
    """No se puede determinar la vitalidad (liveness) del dueño de forma certera (fail-closed)."""


class FrozenRuntimeLockIndeterminateOwnershipError(
    FrozenRuntimeLockOwnershipError, FrozenRuntimeLockIndeterminateError
):
    """El estado del mutex del SO no se pudo determinar con certeza durante assert_owned (fail-closed: UNKNOWN != OWNED)."""


class FrozenRuntimeLockOrphanedError(FrozenRuntimeLockError):
    """El lock quedó huérfano por crash de un dueño anterior: requiere reconciliación/reclaim."""


class FrozenRuntimeLockAdmissionError(FrozenRuntimeLockError, FrozenRuntimeStorageError):
    """El root o el directorio state/ fue rechazado (redirigido, enlace simbólico o reparse).

    Hereda ADEMAS de ``FrozenRuntimeStorageError`` (P4-S2). La admisión del
    root/`state/` es la misma condición de namespace que P2 ya tipaba con la
    familia de storage: cuando el lock la re-evalúa al crear `state/` (o al
    sellar la metadata), el rechazo debe seguir siendo capturable por los
    callers que protegen el namespace con ``except FrozenRuntimeStorageError``.
    Sin este cruce, cablear el lock a los participantes cambiaría el TIPO visible
    del rechazo (sibling en vez de storage) y rompería contratos ya congelados
    (p.ej. ``test_sr09``). La semántica no cambia: sigue siendo fail-closed y la
    escritura sigue sin ocurrir.
    """


# ============================================================================
# Journal durable de transición (P4-S3)
# ============================================================================


class JournalError(FrozenRuntimeError):
    """Base de errores del journal durable de transición (ADR 0012 §34.4)."""


class JournalCorruptError(JournalError):
    """``transition.json`` (o un registro/reserva) es ilegible: JSON malformado,
    truncado, vacío, no UTF-8 o no-objeto.

    Fail-closed en el sentido fuerte: la corrupción NUNCA se degrada a
    ``ABSENT``/``NONE`` (§36.4 N2). Ausencia de archivo y corrupción son
    estados distintos, con clasificación distinta.
    """


class JournalSchemaError(JournalError):
    """Esquema desconocido, campo obligatorio ausente o tipo de dato inválido.

    Incluye ``schema_version`` futura (no se reinterpreta en silencio) y las
    validaciones de forma (UUID v4 canónico, digest SHA-256 de 64 hex, enteros
    monótonos, timestamps ``>= 0``).
    """


class JournalIdentityError(JournalError):
    """Identidad incoherente: registro incompatible con su nombre de archivo,
    ``source_activation_digest`` que no liga el contenido, testigo
    ``last_finalized_transition_id`` que difiere del store derivado, cadena
    ``finalized_from_transition_id`` rota, ``finalization_seq`` duplicado o
    hueco interior, reserva sin registro. Siempre fail-closed.
    """


class JournalCasError(JournalError):
    """Violación de CAS (§36.6): ``transition_id`` o ``journal_revision`` no
    coinciden con el documento vigente, existe un PENDING activo, el
    ``transition_id`` ya fue visto, o la reserva de ``finalization_seq`` ya
    pertenece a otra transición. El CAS fallido DESCARTa, nunca adapta.
    """


class JournalEvidenciaPostError(JournalError):
    """La evidencia POST es inexistente, inválida, no está en PASS o es
    inconsistente con la transición vigente (§35.6: ``FINALIZED`` no puede
    existir sin evidencia durable que ligue transición, source, target, estado
    deseado y observación de Effective).
    """


class JournalObservacionEffectiveError(JournalError):
    """El oráculo de Effective Runtime (P5) NO PUDO observar el Effective.

    Contrato del callback ``observador_effective`` (§34.5): los fallos
    **esperados** de observación —el runtime no responde, el dato no está
    disponible, la observación no se puede completar— se señalan con ESTE error,
    y ``_invocar_oraculo_effective`` los traduce a un veredicto
    ``FAIL_CLOSED``/``EFFECTIVE_INDETERMINADO``.

    Cualquier OTRA excepción se considera un fallo INESPERADO (un bug de
    programación en el callback, o un tipo de error que el contrato no previó):
    no se traduce a veredicto, se propaga hasta la frontera única de
    ``reconciliar_journal``, que la REGISTRA con traceback y también falla
    cerrado —con un motivo distinto— para que el fallo no quede enmascarado
    detrás de "no se pudo observar".
    """


class JournalDependenciaNoDisponibleError(JournalError):
    """La operación requiere una autoridad durable que P4-S3 no implementa
    (consumo de aprobación §36.5/§37.3, oráculo de Effective Runtime P5, POST
    verifier). Fallar cerrado es el resultado correcto: no se sustituye por un
    booleano, un mock productivo ni una aprobación simulada.
    """


class JournalStoreNoInicializadoError(JournalError):
    """``state/transitions/`` o ``state/finalization-seqs/`` no existen: el
    store del journal no fue inicializado (``initialize_frozen_runtime_storage``).
    No se crea el directorio en el camino de escritura: la inicialización del
    layout es del storage, bajo su propia admisión.
    """
