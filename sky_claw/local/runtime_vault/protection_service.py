"""GP2-S4E — cableado productivo de la transacción de protección del Golden.

Este módulo **compone** las piezas ya auditadas; no reimplementa ninguna:

===========================  ==================================================
S4-A                        :mod:`authorization_context`
                             (``establish_privileged_authorization``)
S4-B                        :mod:`mutation_executor` (``apply_authorized_plan``)
S4-C                        :mod:`recovery_orchestrator`
                             (``recover_interrupted_protection``)
S4-D                        :mod:`finalization_orchestrator`
                             (``finalize_protection_transaction``)
===========================  ==================================================

Lo que este slice agrega es exactamente lo que faltaba y el census de
call-sites midió: **un único camino productivo** que encadena esas piezas y
**el router de restart** que enruta cada estado durable al suborquestador que
le corresponde. Hasta S4-E, ``recover_interrupted_protection`` documentaba el
handoff hacia S4-D en prosa y ningún módulo lo realizaba.

Invariantes que este módulo garantiza por construcción
-----------------------------------------------------

* ``HANDLE > pathname`` y ``durable evidence > process memory``: la única
  entrada del camino de recuperación es un ``operation_id``. No se aceptan
  rutas de plan, de journal, de backup, de lock, ni SD crudo.
* ``STAGING != AUTHORITY``: este módulo no importa :mod:`clone` ni ninguna
  primitiva de staging. Un anchor AST lo congela.
* ``OBSERVATION != AUTHORIZATION``: planning devuelve un plan SELLADO, no
  permiso; el permiso lo acuña ``promote_durable_authorized_plan``.
* ``UNKNOWN != VERIFIED`` y *fail closed on ambiguity*: todo desenlace que no
  sea terminal sale como :attr:`ProtectionDisposition.INDETERMINATE` con
  ``fail_closed_reason`` no vacío y el lock RETENIDO.
* **Continuidad del GoldenMutationLock**: la sesión de frontera que abre S4-A
  es la MISMA que se pasa a S4-B y a S4-D. No existe ventana
  ``release → reacquire`` porque el servicio nunca suelta el handle entre
  apply y finalización; S4-D reutiliza ``session.lock`` sin re-adquirir
  (``_adquirir_lock_para_finalizacion``, ``finalization_orchestrator.py:493``).
* **``COMMITTED`` se escribe únicamente en S4-D**. Este módulo no importa ni
  llama ``commit_finalized``.
* **Ningún estado efímero sobrevive al reinicio**: el FSM durable sigue
  siendo el ``ProtectionJournal``; ``RestartRoute`` sólo DECIDE, no registra.

Lo que este módulo NO hace (deliberadamente, y por qué)
---------------------------------------------------------

* No reimplementa apply, rollback ni finalización: los delega. Hay anchors
  AST que lo impiden.
* No expone ninguna tool al LLM ni al ``tool_dispatcher``. El cableado de
  tool del Runtime Vault es trabajo posterior con su propio security review.
* No crea un FSM paralelo ni ningún ``*_state.json``.
* No amplía privilegios: reutiliza el privileged boundary, el Operator
  Verifier Bridge y los contratos de token existentes. No solicita ninguno de
  los privilegios que el anchor de ``test_runtime_vault_operator_token.py``
  veta por nombre, y no acepta un token aportado por el caller como
  autoridad — el token lo decide la estrategia de elevación contra la
  identidad verificada del coordinador.
"""

from __future__ import annotations

import logging
import os
import pathlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from sky_claw.app.security.links import link_kind_and_identity_or_raise
from sky_claw.local.runtime_vault.authorization_context import (
    PrivilegedAuthorizationContext,
    PrivilegedBoundarySession,
    establish_privileged_authorization,
)
from sky_claw.local.runtime_vault.authorized_plan_store import (
    AuthorizedPlanDurableWriter,
    TrustedRegistryProvider,
    derive_authorized_plan_dir,
    load_durable_authorized_plan,
    promote_durable_authorized_plan,
)
from sky_claw.local.runtime_vault.coordinator_identity import (
    CoordinatorIdentityProbeProvider,
    CoordinatorProcessIdentity,
)
from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationDisposition,
    FinalizationForensicReport,
    FinalizationVerificationPort,
    finalize_protection_transaction,
)
from sky_claw.local.runtime_vault.finalization_verification import build_default_verification_port
from sky_claw.local.runtime_vault.golden_backup_archive import GoldenBackupDurableWriter
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockKernel,
    GoldenMutationLockHandle,
)
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    RuntimeIdentity,
    TreeDigest,
)
from sky_claw.local.runtime_vault.mutation_executor import (
    ApplyReport,
    NodeMutationPort,
    apply_authorized_plan,
)
from sky_claw.local.runtime_vault.operation_lock_binding import (
    OperationLockBindingEvidence,
    classify_operation_lock_binding,
)
from sky_claw.local.runtime_vault.operator_token import OtsElevationCase
from sky_claw.local.runtime_vault.planning_orchestrator import (
    GP2PlanningDisposition,
    GP2Result,
    orchestrate_golden_protection_planning,
)
from sky_claw.local.runtime_vault.ppsc import (
    PrivilegedPlanConfirmation,
    PrivilegedPlanConfirmationProvider,
)
from sky_claw.local.runtime_vault.privileged_boundary import PrivilegedHelperLaunchRequest
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
from sky_claw.local.runtime_vault.protection_journal_store import (
    DurableProtectionJournal,
    JournalDurabilityKernel,
    classify_protection_journal,
    create_protection_journal,
    open_protection_journal,
)
from sky_claw.local.runtime_vault.recovery_orchestrator import (
    NodeRecoveryPort,
    RecoveryContinuation,
    RecoveryDisposition,
    RecoveryForensicReport,
    RecoveryLockOutcome,
    recover_interrupted_protection_for_continuation,
)
from sky_claw.local.runtime_vault.trusted_registry import TrustedGoldenRegistry

logger = logging.getLogger(__name__)

#: ``UNTRUSTED_STAGING_AS_RECOVERY_SOURCE = NEVER`` (ADR 0010 §24). Congelado
#: como constante legible para que el anchor AST y el PR puedan citarla: este
#: módulo no tiene ninguna ruta de recovery que toque staging.
UNTRUSTED_STAGING_AS_RECOVERY_SOURCE: Final[str] = "NEVER"


class ProtectionDisposition(StrEnum):
    """Desenlace terminal (o terminal-fail-closed) de la transacción completa.

    Proyección de los enums de los suborquestadores, NO un enum paralelo del
    FSM: el FSM durable sigue siendo ``ProtectionTransactionState`` en el
    journal. Estos valores distinguen lo que el operador tiene que hacer.
    """

    COMMITTED = "committed"
    ALREADY_COMMITTED = "already_committed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_REQUIRED = "rollback_required"
    LOCK_BUSY = "lock_busy"
    INDETERMINATE = "indeterminate"
    REFUSED = "refused"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"


#: Desposiciones en las que el Golden está ENDURECIDO y con evidencia durable.
#: Es lo único que ``ProtectionOutcome.committed`` afirma.
TERMINAL_DISPOSITIONS: Final[frozenset[ProtectionDisposition]] = frozenset(
    {ProtectionDisposition.COMMITTED, ProtectionDisposition.ALREADY_COMMITTED}
)

#: Disposiciones en las que la operación se CERRÓ sin endurecer el Golden:
#: volvió a su estado previo, o nunca hubo nada que hacer.
#:
#: ``ROLLED_BACK`` no es un fail-closed: es un desenlace legítima y completo —
#: el contrato exige que devuelva el Golden a su estado previo y lo prueba. Por
#: eso va aparte de ``TERMINAL_DISPOSITIONS`` (``committed`` sigue siendo falso)
#: pero NO obliga a ``fail_closed_reason``: exigirle una razón sería afirmar que
#: un rollback correcto es un fallo, y haría que todo ``ROLLED_BACK`` real
#: terminara en excepción al construir el DTO.
ROLLED_BACK_DISPOSITIONS: Final[frozenset[ProtectionDisposition]] = frozenset(
    {ProtectionDisposition.ROLLED_BACK, ProtectionDisposition.NOT_APPLICABLE}
)

#: Desposiciones CERRADAS: no hay nada pendiente y el lock puede liberarse.
#: Todo lo demás obliga a explicar por qué no se pudo cerrar.
SETTLED_DISPOSITIONS: Final[frozenset[ProtectionDisposition]] = TERMINAL_DISPOSITIONS | ROLLED_BACK_DISPOSITIONS


class ProtectionStage(StrEnum):
    """Etapa del servicio que produjo el desenlace (observabilidad, §55)."""

    PLANNING = "planning"
    AUTHORIZATION = "authorization"
    PLAN_PROMOTION = "plan_promotion"
    JOURNAL = "journal"
    APPLY = "apply"
    RECOVERY = "recovery"
    FINALIZATION = "finalization"
    ROUTING = "routing"


class RestartRoute(StrEnum):
    """A qué suborquestador le pertenece un estado durable del journal.

    Es una tabla de DECISIÓN, no un estado: no se persiste, no se escribe, no
    aparece en el journal. Se deriva del enum vivo
    ``ProtectionTransactionState`` (18 miembros) y el anchor
    ``test_todo_estado_del_fsm_tiene_una_ruta`` falla si aparece un estado
    nuevo sin ruta.
    """

    #: S4-C decide: o hace rollback, o declara que el apply quedó completo.
    S4C_ROLLBACK = "s4c_rollback"
    #: S4-D: reanuda/cierra el tramo de post-verificación y archivado.
    S4D_FINALIZE = "s4d_finalize"
    #: Ya terminal y ya endurecido: S4-D normaliza el lock, sin re-aplicar.
    S4D_NORMALIZE = "s4d_normalize"
    #: Terminal sin endurecer: nada que hacer, sin tocar el Golden.
    TERMINAL = "terminal"
    #: Fail-closed que exige operador. NUNCA se resuelve automáticamente.
    OPERATOR_REQUIRED = "operator_required"


#: Tabla EXHAUSTIVA estado-durable -> ruta, derivada del contrato vigente.
#:
#: Notas de contrato, no decisiones de S4-E:
#:
#: * ``PREPARING`` / ``PREPARED`` / ``AWAITING_ELEVATION`` NO son alcanzables
#:   bajo el replay del journal autoritativo (su estado inicial es ``applying``
#:   y no hay aristas de retorno), pero se enrutan a S4-C, que es quien ya
#:   los clasifica como evidencia contradictoria fail-closed
#:   (``recovery_orchestrator.py:658``). S4-E no inventa un cuarto destino.
#: * ``APPLYING`` va a S4-C y NO a S4-D: sólo cuando el apply físico quedó
#:   completo (todos los nodos ``MUTATED`` durable) S4-C devuelve
#:   ``POST_VERIFICATION_REQUIRED``, y es S4-E quien encadena ahí S4-D. Ese
#:   encadenamiento es el handoff que S4-C documentaba en prosa y nadie
#:   ejecutaba.
#: * ``ROLLBACK_REQUIRED`` / ``ROLLING_BACK`` NUNCA van a S4-D: su
#:   ``_FASES_ADMISIBLES`` los excluye y los devuelve ``NOT_APPLICABLE``
#:   (``finalization_orchestrator.py:639``). El motor es S4-C.
#: * ``COMMITTED`` va a ``S4D_NORMALIZE``: S4-D relee el backup, verifica que
#:   sea de esta operación y normaliza el lock huérfano, sin re-ejecutar
#:   apply, gates ni archivado.
RESTART_ROUTES: Final[dict[ProtectionTransactionState, RestartRoute]] = {
    ProtectionTransactionState.PREPARING: RestartRoute.S4C_ROLLBACK,
    ProtectionTransactionState.PREPARED: RestartRoute.S4C_ROLLBACK,
    ProtectionTransactionState.AWAITING_ELEVATION: RestartRoute.S4C_ROLLBACK,
    ProtectionTransactionState.APPLYING: RestartRoute.S4C_ROLLBACK,
    ProtectionTransactionState.ROLLBACK_REQUIRED: RestartRoute.S4C_ROLLBACK,
    ProtectionTransactionState.ROLLING_BACK: RestartRoute.S4C_ROLLBACK,
    ProtectionTransactionState.VERIFYING_GP1: RestartRoute.S4D_FINALIZE,
    ProtectionTransactionState.VERIFYING_RV2: RestartRoute.S4D_FINALIZE,
    ProtectionTransactionState.VERIFYING_NODE_SET: RestartRoute.S4D_FINALIZE,
    ProtectionTransactionState.ARCHIVING_BACKUP: RestartRoute.S4D_FINALIZE,
    ProtectionTransactionState.COMMITTED: RestartRoute.S4D_NORMALIZE,
    ProtectionTransactionState.CANCELLED: RestartRoute.TERMINAL,
    ProtectionTransactionState.ELEVATION_REJECTED: RestartRoute.TERMINAL,
    ProtectionTransactionState.REFUSE_TO_APPLY: RestartRoute.TERMINAL,
    ProtectionTransactionState.REFUSE_TO_PLAN: RestartRoute.TERMINAL,
    ProtectionTransactionState.ROLLED_BACK: RestartRoute.TERMINAL,
    ProtectionTransactionState.ROLLBACK_FAILED: RestartRoute.OPERATOR_REQUIRED,
    ProtectionTransactionState.INDETERMINATE: RestartRoute.OPERATOR_REQUIRED,
}


def route_for_state(estado: ProtectionTransactionState) -> RestartRoute:
    """Ruta de restart para *estado*. ``KeyError`` si el FSM creció sin ruta.

    Deliberadamente NO hay valor por defecto: un estado nuevo del enum tiene
    que romper esta función (y con ella el anchor de exhaustividad) hasta que
    alguien decida a qué suborquestador pertenece.
    """
    return RESTART_ROUTES[estado]


@dataclass(frozen=True, slots=True)
class ProtectionOutcome:
    """Resultado forense de la transacción completa (§29, §30).

    Proyecta los suborquestadores sin duplicar sus enums: distingue
    COMMITTED de ALREADY_COMMITTED, ROLLED_BACK de ROLLBACK_REQUIRED, y
    separa LOCK_BUSY ("nadie hizo nada, reintentá") de INDETERMINATE ("no se
    puede demostrar qué pasó, que mire un operador").

    Invariante dura: si el desenlace NO es terminal, ``fail_closed_reason``
    es obligatorio. Es la contraposición de la invariante que S4-D ya impone
    sobre su propio reporte.
    """

    operation_id: str
    disposition: ProtectionDisposition
    stage: ProtectionStage
    source_orchestrator: str
    journal_state: ProtectionTransactionState | None = None
    route: RestartRoute | None = None
    plan_digest: str = ""
    archive_digest: str | None = None
    lock_outcome: str = ""
    lock_retained: bool = False
    rollback_executed: bool = False
    detail: str = ""
    fail_closed_reason: str = ""
    operator_intervention_required: bool = False

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("ProtectionOutcome exige operation_id no vacío")
        if not self.source_orchestrator:
            raise ValueError("ProtectionOutcome exige source_orchestrator no vacío: el resultado dice QUÉN habló")
        if self.disposition not in SETTLED_DISPOSITIONS and not self.fail_closed_reason:
            raise ValueError(
                f"Desenlace no cerrado '{self.disposition.value}' exige fail_closed_reason no vacío: "
                "un resultado que no cierra la operación tiene que decir por qué"
            )

    @property
    def committed(self) -> bool:
        """El Golden quedó endurecido con evidencia durable."""
        return self.disposition in TERMINAL_DISPOSITIONS

    @property
    def settled(self) -> bool:
        """La operación se CERRÓ: no queda desenlace pendiente.

        Distinto de :attr:`committed`: un ``ROLLED_BACK`` es ``settled`` sin
        ser ``committed``. Es la propiedad que decide si la frontera libera o
        retiene el ``GoldenMutationLock`` — soltar sobre un desenlace NO
        cerrado abre la ventana que S4-D declara prohibida.
        """
        return self.disposition in SETTLED_DISPOSITIONS


@dataclass(frozen=True, slots=True)
class DiscoveredOperation:
    """Operación encontrada por :func:`discover_pending_operations`.

    ``operation_id`` es una IDENTIDAD observada, nunca autoridad: quien la
    consume la vuelve a validar contra la evidencia durable.
    """

    operation_id: str
    binding_evidence: OperationLockBindingEvidence
    journal_state: ProtectionTransactionState | None
    route: RestartRoute


@dataclass(frozen=True, slots=True)
class ProtectionAuthorizationInputs:
    """Entradas de S4-A (autorización del operador y PPSC).

    Se agrupan en un bundle para que la firma del servicio quede legible, pero
    NO las simplifies ni las interpretes: son exactamente las que
    ``establish_privileged_authorization`` exige. Ninguna de ellas es
    autoridad de filesystem (no hay ruta de plan, journal, backup ni lock), y
    ninguna es un token aportado por el caller — el token lo decide la
    estrategia de elevación contra la identidad del coordinador verificada.
    """

    #: Solicitud de elevación que abre el privileged boundary.
    launch_request: PrivilegedHelperLaunchRequest
    #: Identidad del proceso coordinador (pid + creation time + imagen).
    expected_coordinator: CoordinatorProcessIdentity
    coordinator_probe_provider: CoordinatorIdentityProbeProvider
    elevation_case: OtsElevationCase
    #: Provider de PPSC. Sin implementación productiva por diseño (§33): el
    #: puerto se inyecta, el servicio no inventa UI de confirmación.
    ppsc_provider: PrivilegedPlanConfirmationProvider
    ppsc_payload: PrivilegedPlanConfirmation
    volume_serial_number: int
    root_file_id: int


@dataclass(frozen=True, slots=True)
class _Frontend:
    """Seams de plataforma inyectables (tests y RIG). Vacío = producción.

    ``authorization_establisher`` existe por una razón concreta y acotada: la
    frontera de S4-A requiere elevación real y no puede ejecutarse en un RIG
    automatizado. El RIG de S4-E lo sustituye por unactory que toma el
    ``GoldenMutationLock`` REAL y devuelve una sesión real — todo lo demás
    (plan durable, journal, apply, finalización, recovery, locks) corre con
    las primitivas productivas. El valor por defecto es SIEMPRE
    ``establish_privileged_authorization``, así que producción no tiene
    override posible desde esta ruta.
    """

    authorization_establisher: (
        Callable[
            [ProtectionAuthorizationInputs],
            tuple[PrivilegedAuthorizationContext, PrivilegedBoundarySession],
        ]
        | None
    ) = None
    mutation_port: NodeMutationPort | None = None
    recovery_port: NodeRecoveryPort | None = None
    verification_port: FinalizationVerificationPort | None = None
    archive_writer: GoldenBackupDurableWriter | None = None
    journal_kernel: JournalDurabilityKernel | None = None
    lock_kernel: GoldenLockKernel | None = None
    programdata_resolver: Callable[[], object] | None = None
    trusted_registry: TrustedGoldenRegistry | TrustedRegistryProvider | None = None
    plan_writer: AuthorizedPlanDurableWriter | None = None


#: Instancia compartida usada como default: el dataclass es inmutable, así que
#: un default por llamada no aportaría nada y ruff lo marca como B008.
_DEFAULT_FRONTEND: Final[_Frontend] = _Frontend()


# --------------------------------------------------------------------------
# Cierre de frontera: liberar vs. retener
# --------------------------------------------------------------------------


def _cerrar_frontera(session: PrivilegedBoundarySession, *, retener_lock: bool) -> None:
    """Cierra la sesión privileged decidiendo explícitamente el destino del lock.

    ``PrivilegedBoundarySession.close()`` LIERA el lock sin condición. Eso es
    correcto para el camino feliz —S4-D ya lo liberó tras el COMMITTED durable
    y el segundo ``release()`` es un no-op idempotente— pero es incorrecto para
    un desenlace no terminal: liberar ahí abriría una ventana en la que otra
    mutadora entra sobre un Golden que todavía puede terminar en
    ``ROLLBACK_REQUIRED`` (el mismo argumento que S4-D documenta en
    ``finalization_orchestrator.py:996``).

    Por eso el servicio elige: ``release()`` en terminal,
    ``retain_for_inspection()`` en cualquier otro caso, de modo que la
    siguiente adquisición clasifique ``ORPHANED`` y el recovery pueda retomar
    el MISMO ``operation_id``.

    Idempotente: si la sesión ya cerró, no hace nada.
    """
    if session.closed:
        return
    primer_error: BaseException | None = None
    try:
        lock = session.lock
        if not lock.closed:
            if retener_lock:
                lock.retain_for_inspection()
            else:
                lock.release()
    except BaseException as exc:  # noqa: BLE001 — boundary: se colecta y se relanza tras cerrar el token
        primer_error = exc
    try:
        session.operator_token.close()
    except BaseException as exc:  # noqa: BLE001 — el fallo del lock nunca impide cerrar el token
        if primer_error is None:
            primer_error = exc
    if primer_error is not None:
        raise primer_error


# --------------------------------------------------------------------------
# Proyecciones
# --------------------------------------------------------------------------


def _proyeccion_de_apply(reporte: ApplyReport) -> ProtectionOutcome:
    """Proyecta el ``ApplyReport`` de S4-B sin reinterpretar sus garantías.

    S4-B es todo-o-nada: ante el primer nodo con error entra a rollback y no
    aplica el resto. ``rollback_error`` presente significa que la restauración
    no se pudo demostrar → el estado durable es ``ROLLBACK_FAILED`` y el
    operador tiene que mirar.
    """
    if reporte.rollback_error is not None:
        return ProtectionOutcome(
            operation_id=reporte.operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.APPLY,
            source_orchestrator="mutation_executor.apply_authorized_plan",
            journal_state=reporte.transaction_state,
            lock_outcome="retained",
            lock_retained=True,
            rollback_executed=True,
            detail="S4-B no pudo demostrar la restauración física del PRE",
            fail_closed_reason=(
                f"rollback_error={reporte.rollback_error!r}: el estado durable del Golden no se pudo probar"
            ),
            operator_intervention_required=True,
        )
    if reporte.apply_error is not None:
        return ProtectionOutcome(
            operation_id=reporte.operation_id,
            disposition=ProtectionDisposition.ROLLED_BACK,
            stage=ProtectionStage.APPLY,
            source_orchestrator="mutation_executor.apply_authorized_plan",
            journal_state=reporte.transaction_state,
            lock_outcome="retained",
            lock_retained=True,
            rollback_executed=True,
            detail=f"apply interrumpido en {len(reporte.rolled_back_nodes)} nodos y restaurado a PRE",
            fail_closed_reason=f"apply_error={reporte.apply_error!r}: el Golden volvió a su estado previo",
        )
    return ProtectionOutcome(
        operation_id=reporte.operation_id,
        disposition=ProtectionDisposition.ROLLED_BACK,
        stage=ProtectionStage.APPLY,
        source_orchestrator="mutation_executor.apply_authorized_plan",
        journal_state=reporte.transaction_state,
        lock_outcome="retained",
        lock_retained=True,
        rollback_executed=False,
        detail="apply completo; el tramo de post-verificación pertenece a S4-D",
        fail_closed_reason="apply físico completo sin verificar: la autoridad es S4-D",
    )


def _proyeccion_de_finalizacion(
    reporte: FinalizationForensicReport,
    *,
    ruta: RestartRoute | None = None,
) -> ProtectionOutcome:
    """Proyecta el ``FinalizationForensicReport`` de S4-D.

    Copia ``disposition`` tal cual porque S4-D ya hizo el trabajo de distinguir
    ``COMMITTED`` de ``ALREADY_COMMITTED`` y de separar ``LOCK_BUSY`` (nadie
    hizo nada) de ``INDETERMINATE`` (no se puede demostrar). Re-clasificar acá
    sería una segunda taxonomía que puede discrepar de la suya.
    """
    mapa: Final[dict[FinalizationDisposition, ProtectionDisposition]] = {
        FinalizationDisposition.COMMITTED: ProtectionDisposition.COMMITTED,
        FinalizationDisposition.ALREADY_COMMITTED: ProtectionDisposition.ALREADY_COMMITTED,
        FinalizationDisposition.ROLLBACK_REQUIRED: ProtectionDisposition.ROLLBACK_REQUIRED,
        FinalizationDisposition.LOCK_BUSY: ProtectionDisposition.LOCK_BUSY,
        FinalizationDisposition.INDETERMINATE: ProtectionDisposition.INDETERMINATE,
        FinalizationDisposition.NOT_APPLICABLE: ProtectionDisposition.NOT_APPLICABLE,
    }
    disposition = mapa[reporte.disposition]
    lock = reporte.lock
    return ProtectionOutcome(
        operation_id=reporte.operation_id,
        disposition=disposition,
        stage=ProtectionStage.FINALIZATION,
        source_orchestrator="finalization_orchestrator.finalize_protection_transaction",
        journal_state=reporte.journal_state,
        route=ruta,
        plan_digest=reporte.authorized_plan_digest,
        archive_digest=reporte.archive_digest,
        lock_outcome=f"acquired={lock.acquired} released={lock.released} orphaned={lock.retained_as_orphan}",
        lock_retained=lock.retained_as_orphan or (lock.acquired and not lock.released),
        rollback_executed=reporte.rollback_executed,
        detail=f"fase alcanzada: {reporte.phase_reached.value}",
        fail_closed_reason=reporte.fail_closed_reason,
        operator_intervention_required=disposition
        in (ProtectionDisposition.INDETERMINATE, ProtectionDisposition.ROLLBACK_REQUIRED),
    )


def _proyeccion_de_recovery(
    reporte: RecoveryForensicReport,
    *,
    ruta: RestartRoute | None = None,
) -> ProtectionOutcome:
    """Proyecta el ``RecoveryForensicReport`` de S4-C."""
    if reporte.disposition is RecoveryDisposition.ROLLED_BACK:
        disposition = ProtectionDisposition.ROLLED_BACK
    elif reporte.disposition is RecoveryDisposition.POST_VERIFICATION_REQUIRED:
        disposition = ProtectionDisposition.ROLLBACK_REQUIRED
    elif reporte.disposition is RecoveryDisposition.LOCK_BUSY:
        disposition = ProtectionDisposition.LOCK_BUSY
    elif reporte.disposition is RecoveryDisposition.NO_TRANSACTION:
        disposition = ProtectionDisposition.NOT_APPLICABLE
    elif reporte.disposition is RecoveryDisposition.TERMINAL:
        # S4-C returns TERMINAL for already-terminal states. Whether they mean
        # "hardened" or "not hardened" is decided below from the durable state.
        if reporte.observed_transaction_state is ProtectionTransactionState.COMMITTED:
            disposition = ProtectionDisposition.ALREADY_COMMITTED
        else:
            disposition = ProtectionDisposition.ROLLED_BACK
    else:
        disposition = ProtectionDisposition.INDETERMINATE
    return ProtectionOutcome(
        operation_id=reporte.operation_id,
        disposition=disposition,
        stage=ProtectionStage.RECOVERY,
        source_orchestrator="recovery_orchestrator.recover_interrupted_protection",
        journal_state=reporte.observed_transaction_state,
        route=ruta,
        lock_outcome=reporte.lock_outcome.value,
        lock_retained=reporte.lock_outcome is RecoveryLockOutcome.ACQUIRED_RETAINED,
        rollback_executed=reporte.physical_restoration_completed,
        detail=reporte.detail,
        fail_closed_reason=reporte.indeterminate_reason or reporte.detail,
        operator_intervention_required=reporte.operator_intervention_required,
    )


# --------------------------------------------------------------------------
# Camino feliz
# --------------------------------------------------------------------------


def protect_golden_root(
    *,
    root: str | os.PathLike[str],
    operation_id: str,
    authorization: ProtectionAuthorizationInputs,
    expected_tree: TreeDigest | None = None,
    expected_runtime: RuntimeIdentity | None = None,
    observed_runtime: RuntimeIdentity | None = None,
    critical_expectations: Sequence[CriticalFileExpectation] = (),
    policy_version: str = "gp2-v1",
    frontend: _Frontend = _DEFAULT_FRONTEND,
) -> ProtectionOutcome:
    """Ejecuta la transacción de protección completa de punta a punta.

    Encadena, en el orden que fija el contrato vigente (ADR 0010 §12.2):

    ``planning`` → ``S4-A`` (PPSC + binding + GoldenMutationLock) →
    ``DurableAuthorizedPlan`` → ``ProtectionJournal`` → ``S4-B`` → ``S4-D``.

    Recibe INTENCIÓN DE DOMINIO (``root``, ``operation_id`` y las entradas de
    autorización) y no detalles de filesystem autoritativo: no hay forma de
    pasarle una ruta de plan, journal, backup o lock, ni un SD.

    El ``GoldenMutationLock`` que S4-A toma es el MISMO objeto que S4-B exige y
    que S4-D reutiliza: la exclusión mutadora es continua entre apply y
    finalización, sin ventana de release/reacquire (§21).

    Returns:
        :class:`ProtectionOutcome` tipado. Nunca ``None``; los errores de
        dominio de cada suborquestador se traducen a disposición.
    """
    _registrar("planning iniciado", operation_id, stage=ProtectionStage.PLANNING)

    planificacion: GP2Result = orchestrate_golden_protection_planning(
        root,
        operation_id=operation_id,
        policy_version=policy_version,
        expected_tree=expected_tree,
        expected_runtime=expected_runtime,
        observed_runtime=observed_runtime,
        critical_expectations=critical_expectations,
    )
    if not planificacion.success or planificacion.sealed_plan is None:
        return _rechazo_de_planning(planificacion, operation_id)

    session: PrivilegedBoundarySession | None = None
    try:
        if frontend.authorization_establisher is not None:
            contexto, session = frontend.authorization_establisher(authorization)
        else:
            contexto, session = establish_privileged_authorization(
                launch_request=authorization.launch_request,
                expected_coordinator=authorization.expected_coordinator,
                coordinator_probe_provider=authorization.coordinator_probe_provider,
                elevation_case=authorization.elevation_case,
                ppsc_provider=authorization.ppsc_provider,
                ppsc_payload=authorization.ppsc_payload,
                volume_serial_number=authorization.volume_serial_number,
                root_file_id=authorization.root_file_id,
            )
    except Exception as exc:  # noqa: BLE001 — S4-A reporta NEGATIVAMENTE, con tipo
        return _resultado(
            operation_id=operation_id,
            disposition=ProtectionDisposition.REFUSED,
            stage=ProtectionStage.AUTHORIZATION,
            source_orchestrator="authorization_context.establish_privileged_authorization",
            detail="S4-A no Crédito la frontera privilegiada",
            fail_closed_reason=f"{type(exc).__name__}: {exc}",
        )

    desenlace: ProtectionOutcome | None = None
    try:
        plan = promote_durable_authorized_plan(
            context=contexto,
            session=session,
            trusted_registry=frontend.trusted_registry,
            programdata_resolver=frontend.programdata_resolver,
            plan_writer=frontend.plan_writer,
        )
        journal: DurableProtectionJournal = create_protection_journal(
            plan,
            programdata_resolver=frontend.programdata_resolver,
            kernel=frontend.journal_kernel,
        )
        reporte_apply = apply_authorized_plan(
            plan=plan,
            journal=journal,
            session=session,
            port=frontend.mutation_port,
        )
        if reporte_apply.rollback_error is not None or reporte_apply.apply_error is not None:
            desenlace = _proyeccion_de_apply(reporte_apply)
            return desenlace

        puerto = frontend.verification_port
        if puerto is None:
            puerto = build_default_verification_port()
        reporte_final = finalize_protection_transaction(
            operation_id=operation_id,
            plan=plan,
            journal=journal,
            port=puerto,
            programdata_resolver=frontend.programdata_resolver,
            archive_writer=frontend.archive_writer,
            lock_kernel=frontend.lock_kernel,
            session=session,
        )
        desenlace = _proyeccion_de_finalizacion(reporte_final)
        return desenlace
    except Exception as exc:  # noqa: BLE001 — boundary: INDETERMINATE, nunca éxito
        logger.exception(
            "Fallo inesperado en la transacción de protección; fail-closed con evidencia preservada",
            extra={"operation_id": operation_id, "stage": ProtectionStage.APPLY.value},
        )
        desenlace = _resultado(
            operation_id=operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.APPLY,
            source_orchestrator="protection_service.protect_golden_root",
            detail="error de dominio o bug inesperado en el tramo mutador; el lock queda retenido",
            fail_closed_reason=f"{type(exc).__name__}: {exc}",
            lock_retained=True,
            operator_intervention_required=True,
        )
        return desenlace
    finally:
        # Liberar-vs-retener se decide por el DESENLACE, nunca por un default:
        # soltar el lock sobre un Golden que puede terminar en
        # ROLLBACK_REQUIRED abre la ventana que S4-D ya declara prohibida.
        # Si ni siquiera se pudo calcular el desenlace, se retiene.
        desenlace = desenlace or _resultado(
            operation_id=operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.AUTHORIZATION,
            source_orchestrator="protection_service.protect_golden_root",
            detail="la frontera no llegó a producir desenlace",
            fail_closed_reason="sin desenlace calculado: se retiene el lock por defecto",
            lock_retained=True,
            operator_intervention_required=True,
        )
        if session is not None:
            _cerrar_frontera(session, retener_lock=not desenlace.settled)


def _rechazo_de_planning(planificacion: GP2Result, operation_id: str) -> ProtectionOutcome:
    """Planning noifluoró un plan sellado: NO se abrió frontera ni se tomó lock."""
    if planificacion.disposition is GP2PlanningDisposition.ALREADY_HARDENED:
        disposition = ProtectionDisposition.ALREADY_COMMITTED
    elif planificacion.disposition is GP2PlanningDisposition.FAILED:
        disposition = ProtectionDisposition.INDETERMINATE
    else:
        disposition = ProtectionDisposition.REFUSED
    return _resultado(
        operation_id=operation_id,
        disposition=disposition,
        stage=ProtectionStage.PLANNING,
        source_orchestrator="planning_orchestrator.orchestrate_golden_protection_planning",
        detail=f"planificación: {planificacion.disposition.value}",
        fail_closed_reason="" if disposition is ProtectionDisposition.ALREADY_COMMITTED else planificacion.message,
    )


# --------------------------------------------------------------------------
# Router de restart
# --------------------------------------------------------------------------


def resume_golden_protection(
    *,
    operation_id: str,
    authorization: ProtectionAuthorizationInputs | None = None,
    frontend: _Frontend = _DEFAULT_FRONTEND,
) -> ProtectionOutcome:
    """Reanuda una operación interrumpida a partir de evidencia DURABLE.

    La única entrada es ``operation_id`` y las —eventuales— entradas de
    autorización para el tramo que aún no ocurrió. No acepta ruta de plan,
    journal, backup ni lock: esas se derivan del namespace por
    ``operation_id`` y se vuelven a validar con las primitivas de
    clasificación que usan S4-C y S4-D.

    La decisión sale de :func:`classify_protection_journal` (evidencia durable
    releída de disco), nunca de memoria del proceso anterior, ni de staging, ni
    de un archivo paralelo.
    """
    clasificacion = classify_protection_journal(
        operation_id,
        programdata_resolver=frontend.programdata_resolver,
        kernel=frontend.journal_kernel,
    )
    journal = clasificacion.journal
    if not clasificacion.is_valid or journal is None:
        return _resultado(
            operation_id=operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.ROUTING,
            source_orchestrator="protection_service.resume_golden_protection",
            detail="el journal durable no es interpretable: cero gates, cero escrituras",
            fail_closed_reason=f"journal durable no válido: {clasificacion.detail}",
            lock_retained=True,
            operator_intervention_required=True,
        )

    estado = journal.transaction_state
    ruta = route_for_state(estado)
    _registrar(
        f"resume: estado durable '{estado.value}' -> ruta '{ruta.value}'",
        operation_id,
        stage=ProtectionStage.ROUTING,
        extra={"route": ruta.value, "journal_state": estado.value},
    )

    match ruta:
        case RestartRoute.OPERATOR_REQUIRED:
            return _resultado(
                operation_id=operation_id,
                disposition=ProtectionDisposition.INDETERMINATE,
                stage=ProtectionStage.ROUTING,
                source_orchestrator="protection_service.resume_golden_protection",
                journal_state=estado,
                route=ruta,
                detail=f"estado durable '{estado.value}' exige intervención de operador",
                fail_closed_reason=(
                    f"estado durable '{estado.value}': S4-E no resuelve ni reintenta un desenlace fail-closed"
                ),
                lock_retained=True,
                operator_intervention_required=True,
            )
        case RestartRoute.TERMINAL:
            ya_endurecido = estado is ProtectionTransactionState.COMMITTED
            return _resultado(
                operation_id=operation_id,
                disposition=(
                    ProtectionDisposition.ALREADY_COMMITTED if ya_endurecido else ProtectionDisposition.ROLLED_BACK
                ),
                stage=ProtectionStage.ROUTING,
                source_orchestrator="protection_service.resume_golden_protection",
                journal_state=estado,
                route=ruta,
                detail=f"estado durable terminal '{estado.value}': cero gates, cero escrituras",
                fail_closed_reason="" if ya_endurecido else f"estado terminal sin endurecer: '{estado.value}'",
            )
        case RestartRoute.S4C_ROLLBACK:
            return _reanudar_por_s4c(operation_id, frontend, ruta=ruta)
        case RestartRoute.S4D_FINALIZE | RestartRoute.S4D_NORMALIZE:
            return _reanudar_por_s4d(operation_id, frontend, ruta=ruta)
        case _:  # pragma: no cover — la tabla es exhaustiva y el anchor lo congela
            raise AssertionError(f"Ruta de restart sin despacho: {ruta!r}")


def _reanudar_por_s4c(
    operation_id: str,
    frontend: _Frontend,
    *,
    ruta: RestartRoute | None = None,
) -> ProtectionOutcome:
    """S4-C decide; si declara el apply completo, S4-E encadena S4-D.

    Este es el handoff que ``recovery_orchestrator`` describía en prosa y que
    ningún módulo ejecutaba. La pieza notrivial es la CONTINUIDAD FÍSICA:

    S4-C toma el lock huérfano de la MISMA ``operation_id`` con
    ``acquire_golden_mutation_lock_for_recovery``. Si devolviera el handle con
    ``retain_for_inspection()``, la metadata quedaría sin ``RELEASED`` pero con
    ``owner_pid`` de ESTE proceso — y S4-D, sin sesión, re-intentaría tomarlo
    por el camino de recovery, que sólo acepta locks huérfanos: lo vería como
    ``BUSY_OWNER_ALIVE`` porque el dueño está vivo (es el propio S4-E), y
    devolvería LOCK_BUSY sin ejecutar un solo gate.

    Por eso se usa ``recover_interrupted_protection_for_continuation``, que
    **transfiere el handle vivo** en vez de cerrarlo, y S4-D lo recibe con
    ``continuation_lock=``. Nadie cierra el handle de Win32 y nadie lo reabre:
    la exclusión sobre el Golden es continua a través de la frontera.

    Y deliberadamente NO se fabrica una ``PrivilegedBoundarySession`` ficticia
    para satisfacer la firma de S4-D: el lock del Golden y el token del
    operador son recursos distintos.
    """
    try:
        reporte, continuation = recover_interrupted_protection_for_continuation(
            operation_id=operation_id,
            programdata_resolver=frontend.programdata_resolver,
            journal_kernel=frontend.journal_kernel,
            lock_kernel=frontend.lock_kernel,
            port=frontend.recovery_port,
        )
    except Exception as exc:  # noqa: BLE001 — S4-C no debería lanzar; si lo hace, no afirmamos nada
        return _resultado(
            operation_id=operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.RECOVERY,
            source_orchestrator="recovery_orchestrator.recover_interrupted_protection_for_continuation",
            detail="S4-C lanzó; la evidencia durable queda sin resolver",
            fail_closed_reason=f"{type(exc).__name__}: {exc}",
            lock_retained=True,
            operator_intervention_required=True,
        )

    if continuation is None:
        # S4-C cerró el handle según su política: no hay nada que continuar.
        # Se proyecta el reporte que YA volvió, sin re-ejecutar la recovery.
        return _proyeccion_de_recovery(reporte, ruta=ruta)

    return _proyectar_continuacion(continuation, frontend, ruta=ruta)


def _proyectar_continuacion(
    continuation: RecoveryContinuation,
    frontend: _Frontend,
    *,
    ruta: RestartRoute | None = None,
) -> ProtectionOutcome:
    """S4-E es owner TEMPORAL del handle transferido y se lo pasa a S4-D.

    Exception safety: si S4-D lanza ANTES de asumir el ownership —al cargar el
    plan, al abrir el journal o al construir el puerto— S4-E sigue siendo el
    owner y hace ``retain_for_inspection()``, NO ``release()``: soltarlo
    escribiría ``RELEASED`` sobre un Golden que S4-D todavía no verificó,
    abriendo la ventana que todo este diseño existe para cerrar.

    Si S4-D ya tomó ownership (o sea, entró al ``try``), no se vuelve a tocar
    el handle: ``release()`` y ``retain_for_inspection()`` son idempotentes y
    `closed` lo vuelve un no-op, pero el contrato queda explícito en el código.
    """
    handle = continuation.lock
    s4d_tomo_ownership = False
    try:
        desenlace = _reanudar_por_s4d(
            continuation.report.operation_id,
            frontend,
            ruta=ruta,
            continuation_lock=handle,
        )
        s4d_tomo_ownership = True
        return desenlace
    except Exception as exc:  # noqa: BLE001 — boundary: INDETERMINATE, nunca éxito
        logger.exception(
            "Fallo en el handoff S4-C -> S4-D; el handle transferido queda retenido",
            extra={
                "operation_id": continuation.report.operation_id,
                "route": (ruta or RestartRoute.S4D_FINALIZE).value,
            },
        )
        return _resultado(
            operation_id=continuation.report.operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.FINALIZATION,
            source_orchestrator="protection_service._proyectar_continuacion",
            detail="S4-D no llegó a asumir el lock transferido; S4-E lo retiene",
            fail_closed_reason=f"{type(exc).__name__}: {exc}",
            route=ruta,
            lock_retained=True,
            operator_intervention_required=True,
        )
    finally:
        if not s4d_tomo_ownership and not handle.closed:
            handle.retain_for_inspection()


def _reanudar_por_s4d(
    operation_id: str,
    frontend: _Frontend,
    *,
    ruta: RestartRoute | None = None,
    continuation_lock: GoldenMutationLockHandle | None = None,
) -> ProtectionOutcome:
    """S4-D reanuda (o normaliza) leyendo su autoridad del namespace."""
    try:
        plan = load_durable_authorized_plan(
            operation_id,
            programdata_resolver=frontend.programdata_resolver,
        )
        journal = open_protection_journal(
            operation_id,
            plan,
            programdata_resolver=frontend.programdata_resolver,
            kernel=frontend.journal_kernel,
        )
    except Exception as exc:  # noqa: BLE001 — plan/journal ilegibles ⇒ INDETERMINATE, cero escrituras
        return _resultado(
            operation_id=operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.FINALIZATION,
            source_orchestrator="finalization_orchestrator.finalize_protection_transaction",
            detail="no se pudo recuperar la autoridad durable de la operación",
            fail_closed_reason=f"{type(exc).__name__}: {exc}",
            lock_retained=True,
            operator_intervention_required=True,
        )

    puerto = frontend.verification_port
    if puerto is None:
        puerto = build_default_verification_port()
    reporte = finalize_protection_transaction(
        operation_id=operation_id,
        plan=plan,
        journal=journal,
        port=puerto,
        programdata_resolver=frontend.programdata_resolver,
        archive_writer=frontend.archive_writer,
        lock_kernel=frontend.lock_kernel,
        continuation_lock=continuation_lock,
    )
    return _proyeccion_de_finalizacion(reporte, ruta=ruta)


# --------------------------------------------------------------------------
# Discovery (mínimo, read-only, nunca autoridad)
# --------------------------------------------------------------------------


def discover_pending_operations(
    *,
    programdata_resolver: Callable[[], object] | None = None,
    journal_kernel: JournalDurabilityKernel | None = None,
) -> tuple[DiscoveredOperation, ...]:
    """Enumera operaciones con evidencia durable en el namespace del vault.

    Existe sólo para que el arranque del producto sepa QUÉ ``operation_id``
    feedearle a :func:`resume_golden_protection`. Es estrictamente READ-ONLY y
    su salida **nunca es autoridad**: cada entrada se revalida contra el
    journal durable antes de que algo la use, y una entrada inesperada se
    ignora en vez de convertirse en permiso.

    Validación aplicada a cada entrada, en este orden:

    1. **Scope**: el nombre tiene que volver a derivar exactamente al
       directorio del que se leyó (``derive_authorized_plan_dir``), lo que
       descarta rutas, traversal y nombres que no sean un ``operation_id``
       canónico en una sola operación.
    2. **Reparse**: la entrada no puede ser symlink ni junction
       (``link_kind_and_identity_or_raise``) — mismo criterio que el resto del
       paquete.
    3. **Evidencia durable**: se clasifica el binding y el journal con las
       MISMAS primitivas que usan S4-C y S4-D.

    No escanea el sistema de archivos entero: sólo ``operations/`` del vault,
    que es el namespace cerrado del Runtime Vault.
    """
    # Import local: replica exactamente el patrón de `derive_authorized_plan_dir`
    # (`authorized_plan_store.py:270`), que resuelve la raíz del vault con el
    # mismo resolver privado. Deuda: promoverlo a API pública del paquete.
    from sky_claw.local.runtime_vault.trusted_registry_lock import _resolve_runtime_vault_dir

    try:
        raiz = pathlib.Path(_resolve_runtime_vault_dir(programdata_resolver=programdata_resolver))
        operaciones = raiz / "operations"
        entradas = sorted(operaciones.iterdir(), key=lambda ruta: ruta.name)
    except OSError:
        logger.warning("No se pudo enumerar el namespace de operaciones; discovery vacío", exc_info=True)
        return ()

    descubiertas: list[DiscoveredOperation] = []
    for entrada in entradas:
        operation_id = entrada.name
        try:
            # (1) scope + nombre canónico, validados por la derivación canónica.
            if derive_authorized_plan_dir(operation_id, programdata_resolver=programdata_resolver) != entrada:
                logger.warning(
                    "Entrada del namespace de operaciones descartada: no deriva a sí misma",
                    extra={"operation_id": operation_id},
                )
                continue
            # (2) reparse: un enlace no es una operación.
            tipo, _ = link_kind_and_identity_or_raise(entrada)
        except Exception:  # noqa: BLE001 — un nombre inválido no es una operación
            continue
        if tipo is not None:
            logger.warning(
                "Entrada del namespace de operaciones descartada: es un enlace",
                extra={"operation_id": operation_id, "link_kind": tipo},
            )
            continue

        # (3) evidencia durable, con las primitivas de S4-C/S4-D.
        binding = classify_operation_lock_binding(operation_id, programdata_resolver=programdata_resolver)
        clasificacion = classify_protection_journal(
            operation_id,
            programdata_resolver=programdata_resolver,
            kernel=journal_kernel,
        )
        estado = clasificacion.journal.transaction_state if clasificacion.journal is not None else None
        descubiertas.append(
            DiscoveredOperation(
                operation_id=operation_id,
                binding_evidence=binding,
                journal_state=estado,
                route=route_for_state(estado) if estado is not None else RestartRoute.OPERATOR_REQUIRED,
            )
        )
    return tuple(descubiertas)


# --------------------------------------------------------------------------
# Adapter de arranque
# --------------------------------------------------------------------------


class RuntimeVaultProtectionCoordinator:
    """Handle del servicio para el lifecycle de arranque del producto.

    **No es una segunda capa de orquestación.** No tiene estado mutable, no
    decide rutas y no llama a suborquestadores por su cuenta: delega en las
    funciones de este módulo. Existe sólo porque el composition root
    (`AppContext`) publica y cierra objetos con ciclo de vida, y porque el
    barrido de arranque necesita ser invocable sin conocer las funciones
    sueltas.

    Deliberadamente **NO** expone ninguna tool al LLM ni al ``tool_dispatcher``:
    el cableado de tool del Runtime Vault es trabajo posterior y con su propio
    security review.
    """

    __slots__ = ("_reconciliar_al_arrancar",)

    def __init__(self, *, reconciliar_al_arrancar: bool = False) -> None:
        self._reconciliar_al_arrancar = reconciliar_al_arrancar

    @property
    def reconciliar_al_arrancar(self) -> bool:
        """Si el arranque debe barrer operaciones pendientes. Sólo lectura."""
        return self._reconciliar_al_arrancar

    async def reconciliar_arranque_pendiente(
        self,
        *,
        programdata_resolver: Callable[[], object] | None = None,
        frontend: _Frontend = _DEFAULT_FRONTEND,
    ) -> tuple[ProtectionOutcome, ...]:
        """Reanuda todas las operaciones Golden pendientes que la evidencia durable muestra.

        Read-only sobre el namespace y Best-effort por operación: una
        operación que falla NO impide reconciliar las demás, y su resultado
        queda en el ``tuple`` devuelto con su ``fail_closed_reason``.

        ``frontend`` existe para que el camino de arranque sea testeable con
        los mismos seams que el resto del paquete (es el mismo patrón
        ``Protocol`` que usa ``establish_privileged_authorization`` con su
        ``lock_acquirer``). Producción lo deja vacío y por lo tanto corre con
        las primitivas productivas: sin el bootstrap del namespace, un backup
        que no se puede publicar con SD canónico devuelve ``INDETERMINATE``,
        que es la respuesta correcta, no un bug.

        La autoridad es exclusivamente la evidencia durable — el journal de
        cada operación, clasificado desde disco. Una entrada inesperada en el
        namespace nunca se convierte en permiso: :func:`discover_pending_operations`
        la descarta, y :func:`resume_golden_protection` la vuelve a validar.
        """
        import asyncio  # noqa: PLC0415 — import diferido: el paquete no exige event loop al importarse

        pendientes = discover_pending_operations(programdata_resolver=programdata_resolver)
        if not pendientes:
            return ()

        # El resolver de ProgramData tiene que viajar al resume TAMBIÉN. Si sólo
        # lo pasáramos al discovery, el barrido enumeraría un namespace y el
        # resume leería otro: el más grave, `%ProgramData%` PRODUCTIVO. El
        # resolver nunca se toma del default de `resume_golden_protection`
        # cuando el caller ya loexpreso.
        efectivo = frontend
        if frontend.programdata_resolver is None and programdata_resolver is not None:
            efectivo = replace(frontend, programdata_resolver=programdata_resolver)

        resultados: list[ProtectionOutcome] = []
        for entrada in pendientes:
            try:
                resultados.append(
                    await asyncio.to_thread(
                        resume_golden_protection,
                        operation_id=entrada.operation_id,
                        frontend=efectivo,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — una operación rota no impide las demás
                logger.exception(
                    "Reconciliación de operación Golden pendiente falló; se continúa con las demás",
                    extra={"operation_id": entrada.operation_id},
                )
                resultados.append(
                    _resultado(
                        operation_id=entrada.operation_id,
                        disposition=ProtectionDisposition.INDETERMINATE,
                        stage=ProtectionStage.ROUTING,
                        source_orchestrator="protection_service.RuntimeVaultProtectionCoordinator",
                        detail="la reconciliación de esta operación falló",
                        fail_closed_reason=f"{type(exc).__name__}: {exc}",
                        lock_retained=True,
                        operator_intervention_required=True,
                    )
                )
        return tuple(resultados)


# --------------------------------------------------------------------------
# Utilidades internas
# --------------------------------------------------------------------------


def _resultado(
    *,
    operation_id: str,
    disposition: ProtectionDisposition,
    stage: ProtectionStage,
    source_orchestrator: str,
    detail: str = "",
    fail_closed_reason: str = "",
    journal_state: ProtectionTransactionState | None = None,
    route: RestartRoute | None = None,
    plan_digest: str = "",
    archive_digest: str | None = None,
    lock_outcome: str = "",
    lock_retained: bool = False,
    rollback_executed: bool = False,
    operator_intervention_required: bool = False,
) -> ProtectionOutcome:
    return ProtectionOutcome(
        operation_id=operation_id,
        disposition=disposition,
        stage=stage,
        source_orchestrator=source_orchestrator,
        journal_state=journal_state,
        route=route,
        plan_digest=plan_digest,
        archive_digest=archive_digest,
        lock_outcome=lock_outcome,
        lock_retained=lock_retained,
        rollback_executed=rollback_executed,
        detail=detail,
        fail_closed_reason=fail_closed_reason,
        operator_intervention_required=operator_intervention_required,
    )


def _registrar(mensaje: str, operation_id: str, *, stage: ProtectionStage, **extra: object) -> None:
    """Log estructurado de forense. Nunca SD crudo, tokens, handles ni bytes."""
    logger.info(
        mensaje,
        extra={"operation_id": operation_id, "stage": stage.value, **extra},
    )


__all__ = [
    "DiscoveredOperation",
    "ProtectionAuthorizationInputs",
    "ProtectionDisposition",
    "ProtectionOutcome",
    "ProtectionStage",
    "RESTART_ROUTES",
    "RestartRoute",
    "RuntimeVaultProtectionCoordinator",
    "ROLLED_BACK_DISPOSITIONS",
    "SETTLED_DISPOSITIONS",
    "TERMINAL_DISPOSITIONS",
    "UNTRUSTED_STAGING_AS_RECOVERY_SOURCE",
    "discover_pending_operations",
    "protect_golden_root",
    "resume_golden_protection",
    "route_for_state",
]
