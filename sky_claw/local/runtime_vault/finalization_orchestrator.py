"""GP2-S4D: orquestador de finalización (post-verificación → COMMITTED).

Contrato normativo ADR 0010 §12.2 pasos 8-11, §19.2 (``VERIFYING_GP1`` …
``COMMITTED``), §20 (filas C1..C10) y §23.2.

Qué es este módulo
------------------
Es el ÚNICO lugar del paquete que puede llevar una operación de protección del
Golden hasta ``COMMITTED``. No muta ACLs, no construye Target DACLs, no escribe
el TGR, no lee staging y no implementa rollback: compone primitivas auditadas de
otros módulos y decide el ORDEN del protocolo. La diferencia con
``recovery_orchestrator`` (S4-C) es de dirección, no de poder: S4-C reconcilia
una operación interrumpida hacia atrás; S4-D la lleva hacia adelante.

La property que gobierna todo el módulo
---------------------------------------
``COMMITTED`` es una afirmación DURABLE, no un mensaje. Estructuralmente no
puede escribirse sin que exista un :class:`DurableGoldenBackupArchive` en mano
— es decir, sin un archivo create-once, flusheado, re-leído y verificado byte a
byte. Eso no es disciplina del caller: es la única firma que acepta
:meth:`DurableProtectionJournal.commit_finalized`, y esa firma exige el backup.
Un mutant que mueva ``COMMITTED`` antes del archive no compila contra esta API.

Lo que el backup ata, y CÓMO
-----------------------------
La afirmación de commit se apoya en estas tres cosas, y **ninguna** de ellas es
un campo del registro ``COMMITTED``:

1. el backup existe en una RUTA DERIVADA de la identidad del plan
   (``golden_backups/<vol>_<fid>/<policy_version>/<op_id>_manifest.json``), así
   que no hace falta que el journal lo nombre para poder Hallarlo;
2. su contenido lleva ``operation_id`` y ``authorized_plan_digest``, y
   ``commit_finalized`` exige que atan con el plan y con el header del journal;
3. es create-once, así que un backup ya publicado no puede haber cambiado bajo el
   commit.

El registro ``COMMITTED`` del journal **no** almacena el digest del archivo. Se
dice explícitamente porque una garantía inventada en un comentario es peor que
ninguna: un operador que leyera "el digest está en el journal" buscaría un campo
que no existe. El binding es recuperable desde el disco y por eso
``_normalizar_commit_ya_durable`` relee el backup antes de liberar el lock.

Orden obligatorio (ADR 0010 §12.2)
-----------------------------------
``VERIFYING_GP1`` → GP1 fresco → ``VERIFYING_RV2`` → RV-2 fresco →
``VERIFYING_NODE_SET`` → NodeSet fresco → quiescence rerun → RV-2 final fresco
→ ``ARCHIVING_BACKUP`` → backup durable y revalidado → ``COMMITTED`` durable →
recién entonces liberar el lock.

Dos detalles normativos que se pierden si se leen por encima:

* **El rerun de quiescence y el SEGUNDO RV-2 ocurren dentro de la arista
  ``VERIFYING_NODE_SET -> ARCHIVING_BACKUP``**, no antes (§19.2 lo dice
  explícitamente: la arista "incorpora el probe rerun + un segundo RV-2
  final"). Cerran la ventana en la que un escritor mutaría contenido entre la
  comprobación y el archivado.
* **Ninguna verificación se cachea.** GP1, RV-2 y NodeSet son OBSERVACIONES:
  tras un crash se vuelven a ejecutar porque el journal vuelve a decir
  ``VERIFYING_GP1``, no porque nadie guarde un flag "ya verificado". No hay
  flags de completitud en el journal, y no se agregan: el ADR no los exige y
  un flag sólo agregaría una segunda fuente de verdad que puede sobrevivir al
  crash que la invalidó.

Idempotencia y replay
---------------------
La única autoridad es el journal durable. ``finalize_protection_transaction``
deriva su punto de partida del estado que LEE de disco, así que llamarla dos
veces, o llamarla después de un crash, es la misma operación con el mismo
resultado. Si el journal ya dice ``COMMITTED``, el resultado es un replay
terminal: no se re-verifica, no se re-archiva, no se re-muta, y el lock huérfano
se normaliza.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from sky_claw.local.runtime_vault.authorized_plan import AuthorizedPlan
from sky_claw.local.runtime_vault.authorized_plan_store import DurableAuthorizedPlan
from sky_claw.local.runtime_vault.golden_backup_archive import (
    GoldenBackupError,
    archive_golden_backup,
    load_durable_golden_backup,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockBusyError,
    GoldenLockError,
    GoldenLockIdentity,
    GoldenLockIoError,
    GoldenLockKernel,
    GoldenLockOrphanedOperationMismatchError,
    GoldenMutationLockHandle,
    acquire_golden_mutation_lock_for_recovery,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.protection_journal import (
    NodeWalState,
    ProtectionTransactionState,
)
from sky_claw.local.runtime_vault.protection_journal_store import (
    DurableProtectionJournal,
    ProtectionJournalStoreError,
    classify_protection_journal,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Excepciones tipadas
# ============================================================================


class FinalizationError(RuntimeVaultError):
    """Base de errores de la finalización S4-D."""


class FinalizationUnsupportedError(FinalizationError):
    """Plataforma sin las garantías Win32 que S4-D necesita."""


class FinalizationPreconditionError(FinalizationError):
    """S4-D invocado sin sus precondiciones (nodos no MUTATED, fase inconsistente)."""


class FinalizationAuthorityError(FinalizationError):
    """La autoridad ofrecida no corresponde a la operación del Golden: fail-closed."""


class FinalizationLockBusyError(FinalizationError):
    """Otra mutadora vive sobre el Golden: cero gates, cero escrituras."""


class FinalizationIndeterminateError(FinalizationError):
    """La evidencia durable es ambigua: no se afirma ni commit ni rollback."""


# ============================================================================
# DTOs de resultado
# ============================================================================


class FinalizationDisposition(StrEnum):
    """Desenlace de S4-D. Sin ``bool`` ambiguo: cada valor afirma algo distinto.

    La distinción entre ``LOCK_BUSY`` y ``INDETERMINATE`` no es cosmética: la
    primera significa "nadie ha hecho nada, reintentá" y la segunda significa
    "no se puede demostrar qué pasó, que un operador mire". Un ``success=False``
    para las dos obligaría al caller a adivinar.
    """

    COMMITTED = "committed"
    ALREADY_COMMITTED = "already_committed"
    ROLLBACK_REQUIRED = "rollback_required"
    LOCK_BUSY = "lock_busy"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE = "not_applicable"


class FinalizationPhase(StrEnum):
    """Fase del protocolo en la que se decidió el desenlace (para el reporte forense)."""

    ENTRY = "entry"
    VERIFYING_GP1 = "verifying_gp1"
    VERIFYING_RV2 = "verifying_rv2"
    VERIFYING_NODE_SET = "verifying_node_set"
    ARCHIVING_BACKUP = "archiving_backup"
    COMMITTED = "committed"
    ROLLBACK = "rollback"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Veredicto de UN gate de post-verificación, con su evidencia observable.

    ``detail`` es texto de diagnóstico SIN bytes de Security Descriptor: el
    contrato de observabilidad prohíbe registrar SD crudo, y un SD contiene SIDs
    y ACEs que no belong en un log.
    """

    gate: str
    passed: bool
    detail: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.gate, str) or not self.gate.strip():
            raise FinalizationError("gate debe ser un string no vacío")
        if not isinstance(self.passed, bool):
            raise FinalizationError("passed debe ser bool")
        if not isinstance(self.detail, str):
            raise FinalizationError("detail debe ser string")
        if not isinstance(self.evidence_digest, str) or len(self.evidence_digest) != 64:
            raise FinalizationError("evidence_digest debe ser un sha256 hex de 64 caracteres")


@dataclass(frozen=True, slots=True)
class FinalizationLockOutcome:
    """Estado del GoldenMutationLock al terminar S4-D.

    ``released`` distingue el final feliz (metadata ``phase=RELEASED`` escrita)
    del final retenido (handle cerrado SIN liberar, dejando un lock huérfano
    ligado a esta misma ``operation_id``). La diferencia importa: liberado, el
    Golden queda disponible; retenido, la única mutadora que puede tomarlo es
    la MISMA operación.
    """

    acquired: bool
    released: bool
    retained_as_orphan: bool
    operation_id: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FinalizationForensicReport:
    """Reporte forense estructurado de un intento de S4-D.

    Es la salida que un operador lee después de un crash. Distingue lo que se
    OBSERVÓ de lo que se AFIRMÓ, y lleva la evidencia durable que sostiene la
    afirmación de commit (digest del plan y digest del backup) para que el
    commit sea auditable sin depender de este proceso, que puede no existir.

    ``rollback_executed`` es SIEMPRE ``False`` desde S4-D, y está explícito en el
    DTO para que el campo no dependa de que el lector recuerde el contrato. Un
    ``disposition=ROLLBACK_REQUIRED`` significa "el estado durable pide rollback
    y el motor de S4-C tiene que correr", NO "el rollback corrió". La distinción
    importa: un operador que leyera ``ROLLBACK_REQUIRED`` como "restaurado"
    cerraría un incidente sobre un Golden que sigue endurecido.
    """

    operation_id: str
    disposition: FinalizationDisposition
    phase_reached: FinalizationPhase
    verdicts: tuple[GateVerdict, ...]
    lock: FinalizationLockOutcome
    journal_state: ProtectionTransactionState
    archive_digest: str | None
    authorized_plan_digest: str
    fail_closed_reason: str = ""
    rollback_executed: bool = False

    @property
    def committed(self) -> bool:
        """True SÓLO para un ``COMMITTED`` durable o su replay terminal.

        Deliberadamente no es "el disposition no es un error": la única forma
        de que este backup diga "commit durable" es que el journal lo diga, y el
        journal es lo único que se escribe con flush verificado.
        """
        return self.disposition in (
            FinalizationDisposition.COMMITTED,
            FinalizationDisposition.ALREADY_COMMITTED,
        )

    def verdict_for(self, gate: str) -> GateVerdict | None:
        for verdict in self.verdicts:
            if verdict.gate == gate:
                return verdict
        return None

    def __post_init__(self) -> None:
        if self.committed and self.archive_digest is None:
            raise FinalizationError(
                "un disposition COMMITTED exige archive_digest: el commit durable sin backup es imposible"
            )
        if (
            not self.committed
            and self.disposition is not FinalizationDisposition.LOCK_BUSY
            and not self.fail_closed_reason
        ):
            raise FinalizationError("todo desenlace no-commit exige fail_closed_reason no vacío")


# ============================================================================
# Puertos de verificación
# ============================================================================


@runtime_checkable
class FinalizationVerificationPort(Protocol):
    """Observaciones FRESCAS de post-verificación. Sin flags, sin caché.

    Cada método es una OBSERVACIÓN, no una aserción: se ejecuta una vez por
    llamada y su resultado no se recuerda. Es lo que hace seguro repetir las
    verificaciones tras un crash — no hay estado que pueda sobrevivir al proceso
    que lo observó.

    ``nodos_autorizados`` se entrega al port para que pueda comparar contra la
    autoridad; el port NO devuelve una lista de "nodos que revisó": devuelve un
    veredicto sobre el CONJUNTO, porque un subconjunto verificado no es el
    conjunto.
    """

    def observar_gp1(self, *, raiz: str, esperado: str) -> GateVerdict: ...

    def observar_rv2(self, *, raiz: str, tree_digest_esperado: Any) -> GateVerdict: ...

    def observar_node_set(self, *, raiz: str, nodos_autorizados: tuple[Any, ...]) -> GateVerdict: ...

    def observar_quiescence(self, *, raiz: str, nodos_autorizados: tuple[Any, ...]) -> GateVerdict: ...


# ============================================================================
# Puertos de rollback (S4-C), inyectados para no duplicar el motor
# ============================================================================


#: Callback que S4-D invoca al dejar ``ROLLBACK_REQUIRED`` durable. NO es el
#: motor de rollback: es un aviso.
#:
#: El seam es una llamada y no un ``Protocol`` con un método, y eso es
#: deliberado. S4-D no implementa restauración física ni toca ACLs; lo único que
#: necesita del rollback es **no hacerlo**, y un puerto con un método tipo
#: ``ejecutar_rollback`` sugeriría que S4-D tiene un rollback propio que
#: delegar. ``_desenrollar_rollback`` deja el estado durable escrito y avisa;
#: ejecutar la restauración es de S4-C.
#:
#: Quien reciba este callback NO puede inferir de que fue invocado que el Golden
#: quedó restaurado: en el momento de la llamada no lo está. Para que eso no
#: dependa de leer el docstring, :class:`FinalizationForensicReport` expone
#: ``rollback_executed``, que S4-D pone SIEMPRE en ``False``.
RollbackNotifier = Callable[[str], None]


#: Aviso por defecto: registra que el rollback quedó pendiente y no muta nada.
#: El nombre viejo ("sin rollback") sugería ausencia de una acción; éste dice
#: lo que hace, que es NOTIFICAR.
def _solo_aviso_de_rollback(operation_id: str) -> None:
    logger.info(
        "S4D rollback requerido para operation_id=%s: lo ejecuta el motor de S4-C, no S4-D",
        operation_id,
    )


# ============================================================================
# Verificaciones de autoridad
# ============================================================================

#: Fases en las que S4-D puede ENTRAR. ``APPLYING`` con todos los nodos
#: MUTATED es la precondición de S4-C; las ``VERIFYING_*`` y ``ARCHIVING_BACKUP``
#: son reentradas por crash. Cualquier otra cosa NO es asunto de S4-D.
_FASES_ADMISIBLES: Final[frozenset[ProtectionTransactionState]] = frozenset(
    {
        ProtectionTransactionState.APPLYING,
        ProtectionTransactionState.VERIFYING_GP1,
        ProtectionTransactionState.VERIFYING_RV2,
        ProtectionTransactionState.VERIFYING_NODE_SET,
        ProtectionTransactionState.ARCHIVING_BACKUP,
    }
)

#: ORDEN NORMATIVO del tramo de finalización de §19.2, en el sentido en que el
#: FSM lo admite. La reanudación avanza por índice sobre esta tupla.
#:
#: Es la única fuente del orden en el código, y existe para que la monotonía sea
#: una propiedad ESTRUCTURAL: sólo se entra a ``_TRAMO_S4D[i + 1]`` cuando el
#: estado durable es exactamente ``_TRAMO_S4D[i]``. Con un
#: ``if estado is not FASE: entrar(FASE)`` por fase, una reanudación desde
#: ``VERIFYING_NODE_SET`` pedía ``VERIFYING_RV2`` — una arista que el FSM no
#: admite — y terminaba en INDETERMINATE sin ejecutar un solo gate. Ese
#: defecto era invisible en los tests porque todos arrancaban desde APPLYING.
_TRAMO_S4D: Final[tuple[ProtectionTransactionState, ...]] = (
    ProtectionTransactionState.VERIFYING_GP1,
    ProtectionTransactionState.VERIFYING_RV2,
    ProtectionTransactionState.VERIFYING_NODE_SET,
    ProtectionTransactionState.ARCHIVING_BACKUP,
)


def _exigir_operacion_coincidente(
    operation_id: str,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
) -> None:
    """El ``operation_id``, el plan y el journal tienen que ser la MISMA operación.

    Los tres, y no sólo los dos primeros: S4-D lee el estado durable de
    ``classify_protection_journal(operation_id)`` —que deriva la RUTA del
    ``operation_id``— pero ESCRIBE con los métodos del objeto ``journal``, que
    escribe en la ruta de SU propio ``operation_id``. Un journal de otra
    operación rompería exactamente esa correspondencia: se leería el estado
    de un journal y se escribiría en otro. Los dos son archivos protegidos por
    el namespace, así que un caller mixto no sería un atacante sino un bug, y un
    bug que escribe en el journal equivocado es la peor clase de bug.
    """
    if not isinstance(plan, DurableAuthorizedPlan):
        raise FinalizationAuthorityError("plan debe ser DurableAuthorizedPlan (evidencia re-leída, no un objeto)")
    if plan.operation_id != operation_id:
        raise FinalizationAuthorityError(
            f"operation_id '{operation_id}' no coincide con el del plan durable "
            f"'{plan.operation_id}': fail-closed sin gates y sin escrituras"
        )
    if journal.operation_id != operation_id:
        raise FinalizationAuthorityError(
            f"el journal corresponde a '{journal.operation_id}' pero se ofrecen '{operation_id}': "
            "S4-D no puede leer el estado durable de una ruta y escribir las transiciones en otra"
        )


def _exigir_wal_de_aplicacion_completo(journal: DurableProtectionJournal, plan: DurableAuthorizedPlan) -> None:
    """S4-D sólo arranca con TODOS los nodos del plan en ``MUTATED`` durable.

    Un nodo en ``MUTATING`` significa que un apply murió a mitad de camino: la
    operación no es verificable, y verificarla "parcialmente" produciría un
    COMMITTED sobre un Golden que nunca quedó completo. La transición a
    ``ROLLBACK_REQUIRED`` es la de S4-C, no la de S4-D.
    """
    nodos = plan.plan.nodes
    pendientes = [
        node.relative_path
        for node in nodos
        if journal.journal.node_state(node.relative_path) is not NodeWalState.MUTATED
    ]
    if pendientes:
        raise FinalizationPreconditionError(
            f"S4-D exige todos los nodos MUTATED durable; no lo están: {pendientes}. "
            "La recuperación de un apply incompleto es de S4-C, no de S4-D"
        )


def _exigir_lock_coherente(identity: GoldenLockIdentity, plan: DurableAuthorizedPlan) -> None:
    if identity.operation_id != plan.operation_id:
        raise FinalizationAuthorityError(
            f"el lock pertenece a '{identity.operation_id}' y la operación es "
            f"'{plan.operation_id}': S4-D nunca opera sobre el lock de otra operación"
        )
    if identity.volume_serial_number != plan.volume_serial_number or identity.root_file_id != plan.root_file_id:
        raise FinalizationAuthorityError("el lock no corresponde a la identidad física del plan: fail-closed sin gates")


# ============================================================================
# Orquestador
# ============================================================================


def _handle_de_recovery(
    plan: DurableAuthorizedPlan,
    programdata_resolver: Callable[[], object] | None,
    lock_kernel: GoldenLockKernel | None,
) -> GoldenMutationLockHandle:
    """Adquiere el lock huérfano de ESTA operación y devuelve su handle vivo."""
    acquisition = acquire_golden_mutation_lock_for_recovery(
        volume_serial_number=plan.volume_serial_number,
        root_file_id=plan.root_file_id,
        operation_id=plan.operation_id,
        programdata_resolver=programdata_resolver,
        kernel=lock_kernel,
    )
    return acquisition.handle


def _adquirir_lock_para_finalizacion(
    *,
    plan: DurableAuthorizedPlan,
    session: Any,
    programdata_resolver: Callable[[], object] | None,
    lock_kernel: GoldenLockKernel | None,
) -> GoldenMutationLockHandle:
    """Obtiene un ``GoldenMutationLock`` VIVO para ESTA ``operation_id``.

    Dos caminos, UNA sola semántica:

    * **Continuidad de sesión**: si el caller trae una sesión de frontera con el
      lock vivo (S4-D siguiendo inmediatamente a S4-C en el mismo proceso), se
      reusa ese MISMO handle. No se suelta ni se re-toma: una ventana de
      re-adquisición sería exactamente la ventana que S4-D existe para cerrar.
    * **Reanudación tras crash**: si no hay sesión, se re-adquiere por el camino
      de recovery, que sólo permite tomar un lock huérfano de la MISMA
      ``operation_id`` y jamás uno con dueño vivo.

    En ambos casos el resultado es un handle vivo ligado a la identidad del
    plan, y la exclusión sobre el Golden es continua desde el último
    ``MUTATED`` durable hasta ``COMMITTED`` o el estado terminal de fallo.
    """
    if session is not None:
        if getattr(session, "closed", True):
            raise FinalizationAuthorityError("la sesión de frontera está cerrada: no hay lock vivo")
        handle = session.lock  # lanza AuthorizationSessionError si se cerró
        if not isinstance(handle, GoldenMutationLockHandle) or handle.closed:
            # El ``isinstance`` NO es decorativo: la sesión es un puerto de
            # frontera, y algo que implemente ``.lock`` devolviendo otra cosa
            # (un mock laxo, un wrapper) haría que S4-D operara sobre el Golden
            # SIN exclusión real, que es la garantía que S4-D existe para dar.
            raise FinalizationAuthorityError(
                "la sesión de frontera no expone un GoldenMutationLockHandle vivo: S4-D no puede continuar"
            )
        _exigir_lock_coherente(handle.identity, plan)
        return handle

    return _handle_de_recovery(plan, programdata_resolver, lock_kernel)


def _adquirir_lock_para_normalizacion(
    *,
    plan: DurableAuthorizedPlan,
    programdata_resolver: Callable[[], object] | None,
    lock_kernel: GoldenLockKernel | None,
) -> GoldenMutationLockHandle:
    """Adquiere el lock huérfano de un ``COMMITTED`` durable para normalizarlo.

    Es el caso de §15/§20 P-Q: el journal dice COMMITTED pero el proceso murió
    antes de escribir ``phase=RELEASED``. El lock huérfano se toma SÓLO para la
    misma ``operation_id`` y sólo para liberarlo: no se re-ejecuta apply, no se
    re-ejecuta rollback, no se re-verifica nada.
    """
    return _handle_de_recovery(plan, programdata_resolver, lock_kernel)


def finalize_protection_transaction(
    *,
    operation_id: str,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    port: FinalizationVerificationPort,
    programdata_resolver: Callable[[], object] | None = None,
    archive_writer: Any = None,
    lock_kernel: GoldenLockKernel | None = None,
    session: Any = None,
    rollback_notifier: RollbackNotifier | None = None,
) -> FinalizationForensicReport:
    """Ejecuta S4-D de punta a punta y devuelve el reporte forense.

    La función es un único camino de código que arranca en el estado durable que
    LEE y termina en un estado durable que ESCRIBE. No hay atajos por "ya lo
    verifiqué antes": cada gate se re-observa.

    Args:
        operation_id: la operación a finalizar. Debe coincidir con el plan.
        plan: autoridad durable del plan (objeto re-leído de disco, nunca el
            candidato ni un manifest de staging).
        journal: journal durable abierto. Sus transiciones son la autoridad.
        port: verificaciones frescas de GP1 / RV-2 / NodeSet / quiescence.
        programdata_resolver: sólo para tests; en producción deriva de ProgramData.
        archive_writer: seam de publicación create-once del backup.
        lock_kernel: seam del lock (tests).
        session: sesión de frontera con lock VIVO, para continuidad de sesión.
        rollback_notifier: AVISO de que el rollback quedó PENDIENTE. S4-D no lo ejecuta.
        El motor de S4-C es el único que restaura ACLs; invocar este callback NO
        significa que el Golden esté restaurado (mira ``rollback_executed``).
    """
    _exigir_operacion_coincidente(operation_id, plan, journal)
    plan_real: AuthorizedPlan = plan.plan
    notificar_rollback: RollbackNotifier = (
        rollback_notifier if rollback_notifier is not None else _solo_aviso_de_rollback
    )

    # ------------------------------------------------------------------
    # 0. Estado durable REAL, releído del archivo protegido.
    #
    # Se clasifica con la MISMA primitiva que usa S4-C, para que un tail
    # cortado (fila C4b de §20) se clasifique INDETERMINATE en vez de
    # interpretarse como "todavía no había terminado". Acá no hay memoria:
    # sólo hay bytes.
    # ------------------------------------------------------------------
    clasificacion = classify_protection_journal(
        operation_id,
        programdata_resolver=programdata_resolver,
    )
    if not clasificacion.is_valid or clasificacion.journal is None:
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.INDETERMINATE,
            phase_reached=FinalizationPhase.ENTRY,
            verdicts=(),
            lock=FinalizationLockOutcome(
                acquired=False,
                released=False,
                retained_as_orphan=False,
                operation_id=operation_id,
                detail="el journal durable no es interpretable: cero gates, cero escrituras",
            ),
            journal_state=ProtectionTransactionState.INDETERMINATE,
            archive_digest=None,
            authorized_plan_digest=plan.digest,
            fail_closed_reason=f"journal durable no válido: {clasificacion.detail}",
        )
    journal_leido = clasificacion.journal

    # El journal y el plan tienen que LIGAR. Un plan con el mismo
    # `operation_id` pero otro contenido es un plan distinto: sin este chequeo,
    # S4-D verificaría el Golden contra un digest de árbol que nadie autorizó.
    if journal_leido.authorized_plan_digest != plan.digest:
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.INDETERMINATE,
            phase_reached=FinalizationPhase.ENTRY,
            verdicts=(),
            lock=FinalizationLockOutcome(
                acquired=False,
                released=False,
                retained_as_orphan=False,
                operation_id=operation_id,
                detail="el journal durable liga contra otro authorized_plan_digest",
            ),
            journal_state=journal_leido.transaction_state,
            archive_digest=None,
            authorized_plan_digest=plan.digest,
            fail_closed_reason=(
                f"el journal fue creado contra el plan '{journal_leido.authorized_plan_digest}' y se "
                f"ofrece '{plan.digest}': fail-closed antes de ejecutar ningún gate"
            ),
        )

    estado_durable = journal_leido.transaction_state

    # Un COMMITTED durable es un hecho consumado: re-verificar o re-archivar no
    # lo haría "más cierto", y re-mutar un Golden ya comprometido sería una
    # segunda transacción. Lo único que queda es normalizar el lock (§15).
    if estado_durable is ProtectionTransactionState.COMMITTED:
        return _normalizar_commit_ya_durable(
            operation_id=operation_id,
            plan=plan,
            journal=journal,
            programdata_resolver=programdata_resolver,
            lock_kernel=lock_kernel,
        )

    # ------------------------------------------------------------------
    # 1. Precondiciones y exclusión.
    # ------------------------------------------------------------------
    if estado_durable not in _FASES_ADMISIBLES:
        return _reporte_no_aplicable(operation_id, plan, estado_durable)

    _exigir_wal_de_aplicacion_completo(journal, plan)

    try:
        lock = _adquirir_lock_para_finalizacion(
            plan=plan,
            session=session,
            programdata_resolver=programdata_resolver,
            lock_kernel=lock_kernel,
        )
    except GoldenLockBusyError as exc:
        # Otra mutadora vive sobre el Golden. Cero gates, cero escrituras, cero
        # transiciones: la operación sigue exactamente donde estaba.
        logger.warning("S4D lock busy para operation_id=%s: no se ejecuta ningún gate", operation_id)
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.LOCK_BUSY,
            phase_reached=FinalizationPhase.ENTRY,
            verdicts=(),
            lock=FinalizationLockOutcome(
                acquired=False,
                released=False,
                retained_as_orphan=False,
                operation_id=operation_id,
                detail=str(exc),
            ),
            journal_state=estado_durable,
            archive_digest=None,
            authorized_plan_digest=plan.digest,
            fail_closed_reason=f"otra operación mutadora tiene el GoldenMutationLock: {exc}",
        )
    except GoldenLockOrphanedOperationMismatchError as exc:
        return _reporte_indeterminate(
            operation_id=operation_id,
            plan=plan,
            journal=journal,
            estado=estado_durable,
            phase=FinalizationPhase.ENTRY,
            verdicts=(),
            reason=f"el lock huérfano pertenece a otra operación: {exc}",
        )

    lock_tomado = True
    veredictos: list[GateVerdict] = []

    try:
        # ------------------------------------------------------------------
        # 2..5. La cadena de fases se recorre HACIA ADELANTE, sin retroceder.
        #
        # El FSM de §19.2 es lineal, así que la reanudación tiene que empezar en
        # la fase durable en la que murió el proceso anterior y recorrer lo que
        # falta. Lo que NO se hace es confiar en que esa fase "ya pasó": cada
        # gate en el que se entra se VUELVE a observar.
        #
        # La monotonía se hace ESTRUCTURAL y no por remembering: se declara el
        # orden normativo una vez y se avanza por índice. Un `if estado is not
        # X: entrar(X)` repetido por fase puede pedir una arista que el FSM no
        # admite —desde VERIFYING_NODE_SET se pedía VERIFYING_RV2— y el
        # resultado era INDETERMINATE con cero gates ejecutados. Con el índice
        # eso no es expresable: sólo se entra a la fase N+1 cuando el estado
        # durable es exactamente la N.
        # ------------------------------------------------------------------
        def _fallar(fase: ProtectionTransactionState, motivo: str) -> FinalizationForensicReport:
            return _desenrollar_rollback(
                operation_id=operation_id,
                plan=plan,
                journal=journal,
                lock=lock,
                veredictos=veredictos,
                estado=fase,
                reason=motivo,
                verdict_rollback=notificar_rollback,
            )

        def _observar(
            veredicto: GateVerdict, fase: ProtectionTransactionState, motivo: str
        ) -> FinalizationForensicReport | None:
            veredictos.append(veredicto)
            _log_gate(operation_id, veredicto)
            return None if veredicto.passed else _fallar(fase, f"{motivo}: {veredicto.detail}")

        # Órdenes absolutos: re-observar un gate que se acaba de observar es
        # correcto; saltarse uno por "que ya lo hizo otro proceso" es exactamente
        # el defecto que las verificaciones re-observadas evitan.
        # Índice de la fase durable: -1 significa "todavía antes del tramo", que
        # es exactamente lo que representa APPLYING (S4-C dejó el apply cerrado
        # y S4-D arranca el tramo de verificación).
        _indice_inicial = _TRAMO_S4D.index(estado_durable) if estado_durable in _TRAMO_S4D else -1
        if _indice_inicial < 0 and estado_durable is not ProtectionTransactionState.APPLYING:
            # `_FASES_ADMISIBLES` ya lo filtró; es una defensa explícita para que
            # agregar un estado admisible sin agregarlo a `_TRAMO_S4D` no se
            # convierta en un ValueError opaco en medio del protocolo.
            raise FinalizationError(
                f"el estado durable '{estado_durable.value}' no tiene posición en el tramo normativo "
                f"{[estado.value for estado in _TRAMO_S4D]}: fail-closed antes de ejecutar ningún gate"
            )

        for _fase in _TRAMO_S4D:
            _pos = _TRAMO_S4D.index(_fase)
            if _pos < _indice_inicial:
                continue  # ya superado: no se retrocede ni se reescribe
            if _pos > _indice_inicial:
                journal.enter_finalization_phase(_fase)
                estado_durable = _fase

            if _fase is ProtectionTransactionState.VERIFYING_GP1:
                fallo = _observar(
                    _evaluar("gp1", port.observar_gp1(raiz=plan_real.canonical_root, esperado="hardened")),
                    _fase,
                    "GP1 final no PASÓ",
                )
            elif _fase is ProtectionTransactionState.VERIFYING_RV2:
                fallo = _observar(
                    _evaluar(
                        "rv2",
                        port.observar_rv2(raiz=plan_real.canonical_root, tree_digest_esperado=plan_real.tree_digest),
                    ),
                    _fase,
                    "RV-2 final no PASÓ",
                )
            elif _fase is ProtectionTransactionState.VERIFYING_NODE_SET:
                # Los tres van DENTRO de la arista VERIFYING_NODE_SET ->
                # ARCHIVING_BACKUP, que es como §19.2 y §12.2 los describen.
                fallo = _observar(
                    _evaluar(
                        "node_set",
                        port.observar_node_set(raiz=plan_real.canonical_root, nodos_autorizados=plan_real.nodes),
                    ),
                    _fase,
                    "NodeSet final no PASÓ",
                )
                if fallo is None:
                    # Rerun de quiescence con la MISMA primitiva y la MISMA
                    # política de reintentos del ADR: no una variante informal.
                    fallo = _observar(
                        _evaluar(
                            "quiescence",
                            port.observar_quiescence(raiz=plan_real.canonical_root, nodos_autorizados=plan_real.nodes),
                        ),
                        _fase,
                        "quiescence final no PASÓ",
                    )
                if fallo is None:
                    # SEGUNDO RV-2, después del probe: cierra la ventana en la
                    # que un escritor habría podido cambiar contenido entre la
                    # verificación de contenido y el archivado.
                    fallo = _observar(
                        _evaluar(
                            "rv2",
                            port.observar_rv2(
                                raiz=plan_real.canonical_root, tree_digest_esperado=plan_real.tree_digest
                            ),
                        ),
                        _fase,
                        "RV-2 final (post-quiescence) no PASÓ",
                    )
            else:  # ARCHIVING_BACKUP
                # Re-observación del contenido justo antes de archivar.
                #
                # Si S4-D entra acá por REANUDACIÓN (el proceso anterior murió
                # durante el archivado), el FSM no permite volver a
                # VERIFYING_NODE_SET — y no debería: §20 C8 dice que desde
                # ARCHIVING_BACKUP el recovery continúa el archivado. Pero el
                # contenido pudo cambiar mientras el proceso estaba muerto, y un
                # COMMITTED sobre contenido alterado desde el crash sería una
                # afirmación falsa.
                #
                # La salida no es inventar una arista de retroceso: es
                # RE-OBSERVAR sin escribir transición.
                fallo = _observar(
                    _evaluar(
                        "rv2",
                        port.observar_rv2(raiz=plan_real.canonical_root, tree_digest_esperado=plan_real.tree_digest),
                    ),
                    _fase,
                    "RV-2 previo al archivado no PASÓ",
                )
            if fallo is not None:
                return fallo

        # --------------------------------------------------------------
        # 6. Backup durable create-once + flush + relectura + verificación.
        #
        # ``archive_golden_backup`` es create-once y, si el objeto ya existe,
        # sólo lo ACEPTA si sus bytes canónicos coinciden y todos los bindings
        # atan con el plan. Un backup de otra operación o corrupto es
        # INDETERMINATE: nunca se sobrescribe evidencia existente.
        # --------------------------------------------------------------
        try:
            escritura = archive_golden_backup(
                plan,
                programdata_resolver=programdata_resolver,
                writer=archive_writer,
            )
        except GoldenBackupError as exc:
            return _reporte_indeterminate(
                operation_id=operation_id,
                plan=plan,
                journal=journal,
                estado=estado_durable,
                phase=FinalizationPhase.ARCHIVING_BACKUP,
                verdicts=tuple(veredictos),
                reason=f"el backup durable no se pudo probar: {exc}",
            )
        veredictos.append(
            GateVerdict(
                gate="archive",
                passed=True,
                detail=(
                    "backup create-once durable y revalidado"
                    if escritura.republished
                    else "backup preexistente revalidado por equivalencia exacta de bytes"
                ),
                evidence_digest=escritura.durable.archive_digest,
            )
        )
        _log_gate(operation_id, veredictos[-1])

        # --------------------------------------------------------------
        # 8. COMMITTED durable. La ÚNICA llamada posible lleva el digest del
        #    backup, así que "COMMITTED sin backup" no es representable.
        # --------------------------------------------------------------
        try:
            journal.commit_finalized(
                archive=escritura.durable,
                plan=plan,
            )
        except ProtectionJournalStoreError as exc:
            # El flush del registro COMMITTED no se confirmó: la afirmación
            # de commit durable NO es válida. Se clasifica INDETERMINATE y
            # el lock se RETIENE (nunca se libera un Golden cuyo estado final
            # se desconoce).
            logger.error("S4D flush de COMMITTED no confirmado para operation_id=%s: %s", operation_id, exc)
            lock.retain_for_inspection()  # sin release: el lock queda huérfano de ESTA operación
            return FinalizationForensicReport(
                operation_id=operation_id,
                disposition=FinalizationDisposition.INDETERMINATE,
                phase_reached=FinalizationPhase.COMMITTED,
                verdicts=tuple(veredictos),
                lock=FinalizationLockOutcome(
                    acquired=True,
                    released=False,
                    retained_as_orphan=True,
                    operation_id=operation_id,
                    detail="flush de COMMITTED no confirmado: el lock se retiene para inspección del operador",
                ),
                journal_state=estado_durable,
                archive_digest=escritura.durable.archive_digest,
                authorized_plan_digest=plan.digest,
                fail_closed_reason=f"el registro COMMITTED no quedó durable: {exc}",
            )

        estado_durable = ProtectionTransactionState.COMMITTED

        # --------------------------------------------------------------
        # 9. Recién AHORA se libera el lock.
        #
        # El orden inverso —liberar y después comitear— abriría una ventana
        # en la que otra mutadora puede entrar sobre un Golden que todavía
        # puede terminar en ROLLBACK_REQUIRED. Está anclado por AST.
        #
        # Y si la escritura de ``phase=RELEASED`` falla (§20 Q), el COMMITTED
        # sigue siendo un hecho: lo que queda pendiente es normalizar el
        # lock, no deshacer el commit. Por eso el fallo NO cambia el
        # disposition a INDETERMINATE.
        # --------------------------------------------------------------
        normalizado = True
        detalle_lock = "liberado después del COMMITTED durable"
        try:
            lock.release()
        except (GoldenLockIoError, OSError) as exc:
            normalizado = False
            detalle_lock = (
                f"COMMITTED durable pero phase=RELEASED no se pudo escribir ({exc}); "
                "el lock queda huérfano de esta operación y se normaliza en el próximo recovery"
            )
            logger.warning("S4D no se pudo escribir phase=RELEASED para operation_id=%s: %s", operation_id, exc)
        lock_tomado = False
        logger.info(
            "S4D COMMITTED durable para operation_id=%s archive=%s", operation_id, escritura.durable.archive_digest
        )
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.COMMITTED,
            phase_reached=FinalizationPhase.COMMITTED,
            verdicts=tuple(veredictos),
            lock=FinalizationLockOutcome(
                acquired=True,
                released=normalizado,
                retained_as_orphan=not normalizado,
                operation_id=operation_id,
                detail=detalle_lock,
            ),
            journal_state=estado_durable,
            archive_digest=escritura.durable.archive_digest,
            authorized_plan_digest=plan.digest,
        )

    except (FinalizationError, ProtectionJournalStoreError, GoldenLockError) as exc:
        # Un fallo de durabilidad del journal en CUALQUIER fase (no sólo en
        # COMMITTED) deja el estado durable desconocido: el store marca el journal
        # INDETERMINATE en memoria y lo que se escribió a medias no se puede
        # atribuir. Clasificar INDETERMINATE y RETENER el lock es lo único
        # honesto; un COMMITTED aquí sería una afirmación sobre bytes que nadie
        # confirmó.
        logger.error("S4D fail-closed para operation_id=%s: %s", operation_id, exc)
        if lock_tomado:
            lock.retain_for_inspection()  # retenido: la evidencia no permite afirmar un final
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.INDETERMINATE,
            phase_reached=FinalizationPhase.ENTRY,
            verdicts=tuple(veredictos),
            lock=FinalizationLockOutcome(
                acquired=True,
                released=False,
                retained_as_orphan=lock_tomado,
                operation_id=operation_id,
                detail="fail-closed: evidencia no concluyente",
            ),
            journal_state=estado_durable,
            archive_digest=None,
            authorized_plan_digest=plan.digest,
            fail_closed_reason=str(exc),
        )
    finally:
        if lock_tomado and not lock.closed:
            # Belt-and-braces: si una excepción no cubierta dejó el handle vivo,
            # se RETIENE (nunca se libera) para que el Golden no quede
            # disponible mientras su estado final es desconocido.
            lock.retain_for_inspection()


# ============================================================================
# Ramas terminales
# ============================================================================


def _normalizar_commit_ya_durable(
    *,
    operation_id: str,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    programdata_resolver: Callable[[], object] | None,
    lock_kernel: GoldenLockKernel | None,
) -> FinalizationForensicReport:
    """§15/§20 P-Q: COMMITTED durable + lock huérfano → normalizar y salir.

    Lo que NO hace, y es lo importante: no re-ejecuta apply, no re-ejecuta
    rollback, no re-verifica GP1/RV-2/NodeSet y no re-archiva. El commit ya
    ocurrió; la única labor pendiente es cerrar el lock. Si el lock pertenece a
    OTRA operación, esto NO lo toca: se clasifica INDETERMINATE y el operador
    decide.

    Antes de liberar el lock relee el backup desde disco. No es opcional: un
    COMMITTED cuya evidencia de respaldo no se puede demostrar no es un
    COMMITTED auto-certificado, y liberar el lock sin comprobarlo convertiría
    un estado dudoso en un Golden disponible para todos.
    """
    try:
        backup = load_durable_golden_backup(plan.plan, programdata_resolver=programdata_resolver)
    except GoldenBackupError as exc:
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.INDETERMINATE,
            phase_reached=FinalizationPhase.TERMINAL,
            verdicts=(),
            lock=FinalizationLockOutcome(
                acquired=False,
                released=False,
                retained_as_orphan=False,
                operation_id=operation_id,
                detail="el lock NO se libera: el COMMITTED durable no tiene backup verificable",
            ),
            journal_state=ProtectionTransactionState.COMMITTED,
            archive_digest=None,
            authorized_plan_digest=plan.digest,
            fail_closed_reason=f"el journal dice COMMITTED pero su backup no se puede probar: {exc}",
        )

    try:
        lock = _adquirir_lock_para_normalizacion(
            plan=plan,
            programdata_resolver=programdata_resolver,
            lock_kernel=lock_kernel,
        )
    except GoldenLockOrphanedOperationMismatchError as exc:
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.INDETERMINATE,
            phase_reached=FinalizationPhase.TERMINAL,
            verdicts=(),
            lock=FinalizationLockOutcome(
                acquired=False,
                released=False,
                retained_as_orphan=False,
                operation_id=operation_id,
                detail="el lock huérfano pertenece a otra operación: no se toca",
            ),
            journal_state=ProtectionTransactionState.COMMITTED,
            archive_digest=backup.archive_digest,
            authorized_plan_digest=plan.digest,
            fail_closed_reason=f"COMMITTED durable pero el lock huérfano es de otra operación: {exc}",
        )
    except GoldenLockBusyError as exc:
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.LOCK_BUSY,
            phase_reached=FinalizationPhase.TERMINAL,
            verdicts=(),
            lock=FinalizationLockOutcome(
                acquired=False,
                released=False,
                retained_as_orphan=False,
                operation_id=operation_id,
                detail=str(exc),
            ),
            journal_state=ProtectionTransactionState.COMMITTED,
            archive_digest=backup.archive_digest,
            authorized_plan_digest=plan.digest,
            fail_closed_reason=f"COMMITTED durable pero el lock sigue tomado: {exc}",
        )

    # F14/§20 Q: el COMMITTED YA ES durable. Un fallo al escribir
    # `phase=RELEASED` NO puede reescribir ese hecho — el commit ocurrió y no se
    # deshace. Lo que degrada es la NORMALIZACIÓN del lock: el handle se cierra
    # de todos modos (el kernel lo libera al morir el proceso) pero la metadata
    # queda sin RELEASED, así que la próxima adquisición de esta MISMA
    # `operation_id` la clasifica como huérfano y vuelve a normalizar. Reportar
    # esto como INDETERMINATE sería mentir sobre el commit; reportarlo como
    # COMMITTED limpio sería mentir sobre el lock.
    normalizado = True
    detalle_lock = "lock huérfano de la MISMA operación normalizado a phase=RELEASED"
    try:
        lock.release()
    except (GoldenLockIoError, OSError) as exc:
        # Igual que el camino principal: `release()` propaga `OSError` cuando la
        # escritura de `phase=RELEASED` falla a nivel de sistema (disco lleno,
        # sharing violation, handle cerrado por otro proceso). Sin `OSError` en
        # la tupla, esa excepción escapaba al caller CON un COMMITTED durable ya
        # escrito: el llamador recibía una excepción por una normalización de
        # lock y podía interpretar que el commit se había deshecho.
        #
        # El commit NO se deshace. Lo pendiente es la normalización del lock, y
        # eso se refleja en `released=False` + `retained_as_orphan=True`.
        normalizado = False
        detalle_lock = (
            f"COMMITTED durable pero phase=RELEASED no se pudo escribir ({exc}); "
            "el lock queda huérfano de esta operación y su normalización se reintenta en el próximo recovery"
        )
        logger.warning("S4D no se pudo escribir phase=RELEASED para operation_id=%s: %s", operation_id, exc)
    finally:
        if not lock.closed:
            lock.retain_for_inspection()
    logger.info("S4D lock huérfano normalizado tras COMMITTED durable: operation_id=%s", operation_id)
    return FinalizationForensicReport(
        operation_id=operation_id,
        disposition=FinalizationDisposition.ALREADY_COMMITTED,
        phase_reached=FinalizationPhase.TERMINAL,
        verdicts=(),
        lock=FinalizationLockOutcome(
            acquired=True,
            released=normalizado,
            retained_as_orphan=not normalizado,
            operation_id=operation_id,
            detail=detalle_lock,
        ),
        journal_state=ProtectionTransactionState.COMMITTED,
        archive_digest=backup.archive_digest,
        authorized_plan_digest=plan.digest,
    )


def _desenrollar_rollback(
    *,
    operation_id: str,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    lock: GoldenMutationLockHandle,
    veredictos: list[GateVerdict],
    estado: ProtectionTransactionState,
    reason: str,
    verdict_rollback: Callable[[str], None],
) -> FinalizationForensicReport:
    """Un gate falló: deja ``ROLLBACK_REQUIRED`` durable y devuelve el control.

    Dos decisiones que el ADR obliga y que conviene no dar por hechas:

    1. **La transición a ROLLBACK_REQUIRED es durable ANTES de depender de
       ella.** Si el append falla, el reporte es INDETERMINATE y el lock se
       retiene: nunca se afirma un rollback que no quedó registrado.
    2. **El lock NO se libera.** ROLLBACK_REQUIRED no es un estado terminal:
       el Golden todavía tiene que ser restaurado, y hasta que lo sea ninguna
       otra mutadora debe poder entrar. Se deja huérfano y ligado a ESTA
       ``operation_id``, que es la única que puede reclamarlo. Liberar aquí
       abriría una ventana en la que otra operación mutaría un Golden a medio
       deshacer.
    """
    logger.warning("S4D rollback requerido para operation_id=%s: %s", operation_id, reason)
    try:
        journal.transition_to(ProtectionTransactionState.ROLLBACK_REQUIRED)
    except ProtectionJournalStoreError as exc:
        lock.retain_for_inspection()
        return FinalizationForensicReport(
            operation_id=operation_id,
            disposition=FinalizationDisposition.INDETERMINATE,
            phase_reached=FinalizationPhase.ROLLBACK,
            verdicts=tuple(veredictos),
            lock=FinalizationLockOutcome(
                acquired=True,
                released=False,
                retained_as_orphan=True,
                operation_id=operation_id,
                detail="la transición a ROLLBACK_REQUIRED no quedó durable: el lock se retiene",
            ),
            journal_state=estado,
            archive_digest=None,
            authorized_plan_digest=plan.digest,
            fail_closed_reason=f"no se pudo registrar ROLLBACK_REQUIRED: {exc}",
        )

    if not lock.closed:
        lock.retain_for_inspection()  # retenido como huérfano de esta operación, NO liberado
    verdict_rollback(operation_id)
    return FinalizationForensicReport(
        operation_id=operation_id,
        disposition=FinalizationDisposition.ROLLBACK_REQUIRED,
        phase_reached=FinalizationPhase.ROLLBACK,
        verdicts=tuple(veredictos),
        lock=FinalizationLockOutcome(
            acquired=True,
            released=False,
            retained_as_orphan=True,
            operation_id=operation_id,
            detail="lock retenido hasta que el motor de S4-C alcance un estado terminal",
        ),
        journal_state=ProtectionTransactionState.ROLLBACK_REQUIRED,
        archive_digest=None,
        authorized_plan_digest=plan.digest,
        fail_closed_reason=reason,
    )


def _reporte_no_aplicable(
    operation_id: str, plan: DurableAuthorizedPlan, estado: ProtectionTransactionState
) -> FinalizationForensicReport:
    """S4-D no es el dueño de esta fase: no toca nada y lo dice."""
    return FinalizationForensicReport(
        operation_id=operation_id,
        disposition=FinalizationDisposition.NOT_APPLICABLE,
        phase_reached=FinalizationPhase.ENTRY,
        verdicts=(),
        lock=FinalizationLockOutcome(
            acquired=False,
            released=False,
            retained_as_orphan=False,
            operation_id=operation_id,
            detail="S4-D no adquirido lock: la fase durable no le corresponde",
        ),
        journal_state=estado,
        archive_digest=None,
        authorized_plan_digest=plan.digest,
        fail_closed_reason=f"fase durable '{estado.value}' fuera del alcance de S4-D: cero gates, cero escrituras",
    )


def _reporte_indeterminate(
    *,
    operation_id: str,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    estado: ProtectionTransactionState,
    phase: FinalizationPhase,
    verdicts: Sequence[GateVerdict],
    reason: str,
) -> FinalizationForensicReport:
    """Evidencia ambigua: no se afirma commit NI rollback. Conserva evidencia."""
    logger.error("S4D INDETERMINATE para operation_id=%s: %s", operation_id, reason)
    return FinalizationForensicReport(
        operation_id=operation_id,
        disposition=FinalizationDisposition.INDETERMINATE,
        phase_reached=phase,
        verdicts=tuple(verdicts),
        lock=FinalizationLockOutcome(
            acquired=True,
            released=False,
            retained_as_orphan=True,
            operation_id=operation_id,
            detail="evidencia ambigua: el lock se retiene para inspección del operador",
        ),
        journal_state=estado,
        archive_digest=None,
        authorized_plan_digest=plan.digest,
        fail_closed_reason=reason,
    )


# ============================================================================
# Helpers de veredicto
# ============================================================================


def _evaluar(gate: str, observacion: Any) -> GateVerdict:
    """Normaliza la observación del puerto a un :class:`GateVerdict` inmutable.

    Acepta dos formas, y las dos conservan la identidad del gate:

    * un ``GateVerdict`` ya construido, que pasa tal cual (el gate tiene que
      coincidir con el que pide el orquestador: si no, es un puerto con un
      typo, y aceptarlo produciría un reporte donde ``verdict_for("rv2")``
      devuelve ``None`` sobre un reporte que sí contiene un veredicto de RV-2);
    * el par ``(passed, detail)`` de un puerto simple.

    Lo que NO acepta es un ``bool`` pelado: un veredicto sin detalle no es
    auditable, y sin auditabilidad un INDETERMINATE no se puede distinguir de un
    fallo real.

    El nombre del gate es un PARÁMETRO y no una etiqueta fija (``"observacion"``)
    porque el nombre es la única forma que tiene el reporte forense de localizar
    un veredicto concreto.
    """
    if isinstance(observacion, GateVerdict):
        if observacion.gate != gate:
            raise FinalizationError(
                f"el puerto devolvió un veredicto del gate '{observacion.gate}' donde se esperaba "
                f"'{gate}': el reporte forense quedaría inconsistente con los gates ejecutados"
            )
        return observacion
    if isinstance(observacion, tuple) and len(observacion) == 2:
        passed, detail = observacion
        if not isinstance(detail, str):
            raise FinalizationError(
                f"el detalle del gate '{gate}' debe ser string para ser auditable; recibido {type(detail).__name__}"
            )
        return GateVerdict(
            gate=gate,
            passed=bool(passed),
            detail=detail,
            evidence_digest=hashlib.sha256(f"{gate}:{detail}".encode()).hexdigest(),
        )
    raise FinalizationError(
        f"el puerto de verificación debe devolver GateVerdict o (passed, detail) para el gate "
        f"'{gate}'; recibido {type(observacion).__name__}"
    )
    raise FinalizationError(
        f"el puerto de verificación debe devolver GateVerdict o (passed, detail); recibido {type(observacion).__name__}"
    )


def _log_gate(operation_id: str, veredicto: GateVerdict) -> None:
    """Log estructurado con operation_id, gate y motivo. Nunca bytes de SD."""
    if veredicto.passed:
        logger.info("S4D gate operation_id=%s gate=%s PASS", operation_id, veredicto.gate)
    else:
        logger.warning(
            "S4D gate operation_id=%s gate=%s FAIL detail=%s",
            operation_id,
            veredicto.gate,
            veredicto.detail,
        )


__all__ = [
    "FinalizationAuthorityError",
    "FinalizationDisposition",
    "FinalizationError",
    "FinalizationForensicReport",
    "FinalizationIndeterminateError",
    "FinalizationLockBusyError",
    "FinalizationLockOutcome",
    "FinalizationPhase",
    "FinalizationPreconditionError",
    "RollbackNotifier",
    "FinalizationUnsupportedError",
    "FinalizationVerificationPort",
    "GateVerdict",
    "finalize_protection_transaction",
]
