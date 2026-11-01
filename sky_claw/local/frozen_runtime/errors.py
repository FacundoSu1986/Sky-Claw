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


class StateCasError(FrozenRuntimeStorageError):
    """CAS sobre ``state/active.json`` fallido: el estado cambió bajo los pies.

    No es un error de programación ni un fallo de I/O: es la barrera contra un
    lost update. Se levanta ANTES de escribir, así que el estado concurrente
    queda intacto y el mutador falla cerrado en vez de pisarlo.
    """


class InvalidGenerationIdError(FrozenRuntimeStorageError):
    """generation-id con caracteres, formato, traversal o forma reservada inválida."""


class GenerationCollisionError(FrozenRuntimeStorageError):
    """generation-id existente cuya identidad completa difiere (fail-closed).

    La idempotencia exige que full tree digest + runtime identity + critical
    evidence coincidan; si no, nunca se sobreescribe en silencio.
    """


# ============================================================================
# Runtime Clones (P5-B)
# ============================================================================


class RuntimeCloneError(FrozenRuntimeError):
    """Error base de la autoridad de Runtime Clone."""


class InvalidCloneIdError(RuntimeCloneError, FrozenRuntimeStorageError):
    """clone_id con caracteres, formato, traversal o forma reservada inválida."""


class RuntimeCloneCorruptError(RuntimeCloneError, StateCorruptError):
    """Registro de Runtime Clone corrupto, truncado o ilegible (fail-closed)."""


class RuntimeCloneSchemaError(RuntimeCloneError, StateSchemaError):
    """Registro de Runtime Clone con schema desconocido o campos requeridos ausentes."""


class RuntimeCloneCollisionError(RuntimeCloneError, FrozenRuntimeStorageError):
    """clone_id ya existe en storage con identidad distinta (no-clobber)."""


class RuntimeCloneLifecycleError(RuntimeCloneError):
    """Transición inválida en la máquina de estados de RuntimeCloneLifecycle."""


class RuntimeCloneAdmissionError(RuntimeCloneError, FrozenRuntimeStorageError):
    """Ruta o rol de Runtime Clone no admisible (reparse, Managed Source, rol inválido)."""


class RuntimeCloneNotFoundError(RuntimeCloneError, FrozenRuntimeStorageError):
    """El RuntimeCloneRecord solicitado no existe en el storage."""


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


# ============================================================================
# Autoridad durable de aprobación (P4-S4)
# ============================================================================


class ApprovalError(FrozenRuntimeError):
    """Base de errores de la autoridad durable de aprobación (ADR 0012 §36.5/§37)."""


class ApprovalCorruptError(ApprovalError):
    """El ledger, un witness de emisión o un tombstone terminal es ilegible.

    JSON malformado, truncado, vacío, no UTF-8, no-objeto, nombre de archivo que
    no matchea el contrato o contenido incoherente con su nombre. Fail-closed en
    el sentido fuerte: la corrupción NUNCA se degrada a ``ABSENT`` (§38.2 R4,
    §40.2 S4). Un tombstone parcial es ``FAIL_CLOSED``, no «ausente».
    """


class ApprovalSchemaError(ApprovalError):
    """Esquema desconocido, campo obligatorio ausente, tipo inválido o
    ``ApprovalScope`` que no satisface los 14 campos canónicos (§38.5)."""


class ApprovalIdentityError(ApprovalError):
    """Identidad incoherente: ``approval_scope_digest`` que no recomputa,
    ``source_activation_digest`` que no liga el contenido, witness o tombstone
    que no coincide con el ``(approval_seq, approval_id)`` de su nombre, o
    consumo cuyo ``approval_id``/``approval_seq``/digest no coincide con la
    emisión registrada. Siempre fail-closed.
    """


class ScopeBindingError(FrozenRuntimeError):
    """La transición NO liga el ``ApprovalScope`` autorizado (P4-S5).

    Vive fuera de las familias ``ApprovalError`` y ``JournalError`` a propósito:
    el binding es el contrato **entre** la autoridad de aprobación y el journal,
    y lo ejercen los dos. Colgarlo de una sola familia obligaría al otro módulo a
    importar el error del vecino para poder declararlo, que es el ciclo de imports
    que el subsistema evita.

    Un ``ScopeBindingError`` significa que el conjunto de artefactos que la
    transición declara —operación, source, target, candidate y evidencia de
    contenido— no es el que el propietario aprobó. Siempre fail-closed: no se
    consume la aprobación, no se escribe el PENDING y no se aplica nada.
    """


class ApprovalRootAjenoError(ApprovalIdentityError):
    """El ledger o un witness declara un ``root_id`` que NO es el del root auditado.

    ``root_id`` es "el FrozenRuntimeRoot al que pertenece el ledger" (§37.3) y el
    lock, la secuencia y la propiedad de la aprobación son root-keyed. Sin este
    cross-check, un ``state/`` copiado o restaurado desde OTRO root —con su
    ledger, sus witnesses y sus tombstones— pasaría la auditoría entera: la
    aprobación que el dueño dio para el root A autorizaría una transición en el
    root B. Es la misma comprobación que el journal hace sobre sus reservas de
    ``finalization_seq`` (``root_id_esperado``), y por la misma razón.

    No se clasifica como corrupción: el archivo está bien formado y su contenido
    es coherente consigo mismo; lo que no corresponde es el root. La distinción
    importa para el operador —«este archivo es ilegible» invita a borrarlo,
    «esta evidencia es de otro root» invita a buscar de dónde salió—.
    """


class ApprovalCasError(ApprovalError):
    """Violación de CAS sobre ``approval_revision`` (§37.3/§37.6).

    Toda escritura del ledger presenta la revisión esperada y la incrementa. Un
    CAS fallido DESCARTa la propuesta; nunca la adapta, nunca reasigna la
    secuencia y nunca publica un scope distinto del aprobado.
    """


class ApprovalLedgerRetrocedidoError(ApprovalError):
    """La evidencia durable está por delante del ledger (``max(H, T) > L``) o el
    ledger presenta como ``ISSUED`` una aprobación que ya es terminal (§38.2 R1,
    §40.2 S1). ``LEDGER_RETROCEDIDO`` ⇒ ``FAIL_CLOSED``; la única salida es la
    reconciliación explícita, nunca una re-propuesta ni un salto de secuencia.
    """


class ApprovalMintingDefectError(ApprovalError):
    """Colisión de ``approval_id`` (UUID v4 ya usado por otro registro durable) o
    cadena de witnesses con hueco interior / duplicados (§38.2 R3, §40.2 S3,
    §40.3). Un RNG que colisiona NO se reintenta a ciegas: ``FAIL_CLOSED``.
    """


class ApprovalMaxIssuedError(ApprovalError):
    """Ya existe una aprobación ``ISSUED`` para este root
    (``MAX_ISSUED_APPROVALS_PER_ROOT = 1``; §37.4). El dueño debe consumirla o
    revocarla antes de emitir otra: una aprobación abandonada BLOQUEA, no expira
    por reloj (``TIME_BASED_EXPIRATION_REQUIRED = NO``).
    """


class ApprovalCarreraNormalError(ApprovalError):
    """Otra emisión legítima ganó la misma ``approval_seq`` y el ledger YA la
    conoce (``max(H, T) == L_now``). No es un defecto: se descarta la propuesta y
    se re-presenta (§40.3). Es el único caso en que se permite re-proponer.
    """


class ApprovalStoreNoInicializadoError(ApprovalError):
    """``state/issuances/`` o ``state/approvals/`` no existen: el store de la
    autoridad no fue inicializado (``initialize_frozen_runtime_storage``). El
    camino de escritura no crea directorios: el layout es del storage, bajo su
    propia admisión.
    """


class ApprovalConsentimientoError(ApprovalError):
    """El consentimiento presentado no liga el scope que se va a publicar.

    ``WHAT_HUMAN_APPROVED == WHAT_WAS_ISSUED`` (§39.3 C2): si el digest o el
    ``approval_provenance`` aprobados no son EXACTAMENTE los del scope propuesto,
    el ``ISSUE`` no ocurre. Adaptar el scope para que coincida es el defecto que
    esta excepción impide.
    """


class ApprovalHitolNoDisponibleError(ApprovalError):
    """La EMISIÓN exige una superficie HITL real y el producto no la tiene.

    ``APPROVAL_AUTHORITY = la superficie HITL del producto`` (§35.2/§36.5) y
    ``SELF_ISSUED_APPROVAL_IS_AUTHORIZATION = NO``. Ninguna otra cosa —un lock,
    un booleano, un mensaje del agente LLM, un objeto construido en código— se
    interpreta como consentimiento humano. La entrada productiva de emisión falla
    cerrado con este error hasta que exista la superficie real; el núcleo durable
    se ejercita con fixtures explícitas en los tests.
    """


# ============================================================================
# Consentimiento HITL del propietario (P4-S6)
# ============================================================================


class ConsentimientoError(FrozenRuntimeError):
    """Base de errores del consentimiento verificado del propietario (ADR 0012 §46).

    Vive fuera de ``ApprovalError`` a propósito: la autoridad durable de aprobación
    y la superficie que obtiene el consentimiento son módulos distintos con
    contratos distintos, y colgar el error del consentimiento de la familia de la
    autoridad haría que ``approval.py`` tuviera que importar el error del módulo
    que lo consume (el ciclo de imports que el subsistema evita). El punto donde
    ambos contratos se tocan —la emisión— sí habla ``ApprovalConsentimientoError``.
    """


class IdentidadPropietarioNoVerificadaError(ConsentimientoError):
    """No hay prueba verificable de que quien responde sea el propietario.

    Un ``mecanismo`` que no está en ``SUPERFICIES_DE_CONSENTIMIENTO``, un
    ``from.id`` que no coincide con el operador configurado, un mensaje
    reenviado, un chat de grupo o un ``id`` con el tipo equivocado (texto,
    booleano) caen acá. ``SELF_ISSUED_APPROVAL_IS_AUTHORIZATION = NO``: la
    identidad no se toma de un campo que el llamador controla.
    """


class SuperficieSinIdentidadVerificadaError(ConsentimientoError):
    """La superficie no tiene mecanismo que demuestre identidad del propietario.

    ``GUI`` es presencia local, no identidad: no hay login ni credencial, así que
    aceptarla equivaldría a tratar un clic como prueba de quién lo dio. Queda
    registrada como blocker (``OWNER_IDENTITY_BINDING`` por superficie), no
    silenciada como si el botón fuera suficiente.
    """


class SolicitudConsentimientoDesconocidaError(ConsentimientoError):
    """No hay solicitud ABIERTA con ese ``solicitud_id``.

    Cubre tres casos que se tratan igual porque exigen la misma respuesta —no
    emitir nada—: un id inventado, una solicitud ya resuelta y una solicitud que
    vivía en otro proceso (el registro es en memoria; un reinicio la extingue,
    ``SESSION_CLOSE_INVALIDATES_PENDING = YES``).
    """


class ConsentimientoDuplicadoError(ConsentimientoError):
    """La solicitud ya fue respondida (aprobada, rechazada o cancelada).

    Una decisión terminal no se reabre: sin esto, un primer ``RECHAZAR`` podría
    convertirse en ``APROBAR`` con un segundo mensaje y la decisión registrada no
    sería la que el propietario tomó.
    """


class SesionConsentimientoNoCoincideError(ConsentimientoError):
    """Quien responde no es el propietario de ESA solicitud, en ESA sesión.

    La solicitud liga ``(superficie, sujeto, sesión)``. Comparar sólo el sujeto
    dejaría pasar el replay entre sesiones del mismo propietario —dos ventanas,
    dos conversaciones— y el consentimiento de una autorizaría la otra.
    """
