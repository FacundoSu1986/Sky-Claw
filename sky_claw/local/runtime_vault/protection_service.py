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
    DurableWriteOutcome,
    TrustedRegistryProvider,
    classify_durable_authorized_plan,
    derive_authorized_plan_dir,
    load_durable_authorized_plan,
    promote_durable_authorized_plan,
    publish_candidate_manifest,
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
    ProtectionJournalClassification,
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


#: Rutas PRE-JOURNAL: todavía no hay journal, y la evidencia durable mínima
#: para decidir está en el binding.
#:
#: Existe una ventana legítima en el protocolo:
#:
#:     operation_lock_binding durable
#:       -> GoldenMutationLock adquirido
#:       -> crash
#:       -> NO hay authorized_plan, NO hay journal
#:
#: Es REAL y S4-C la soporta (``RecoveryDisposition.PRE_PLAN_LOCK_RECOVERED``).
#: Antes de P3 el router de S4-E exigía journal válido antes de tiempo y
#: devolvía INDETERMINATE, lo que hacía ese contrato inalcanzable desde el
#: camino productivo.
#:
#: Se congela por igualdad literal porque la distinción que importa es
#: ABSENT vs. NO-INTERPRETABLE: un journal ausente con binding durable es un
#: pre-plan limpio; un journal INDETERMINATE es evidencia contradictoria y se
#: queda fail-closed. Confundir los dos convertiría un crash limpio en un
#: incidente que exige operador.
PRE_JOURNAL_ROUTES: Final[
    dict[
        tuple[DurableWriteOutcome, OperationLockBindingEvidence],
        RestartRoute,
    ]
] = {
    # plan ausente (la pre-condicion del router) × evidencia del binding:
    (DurableWriteOutcome.NOT_DURABLE, OperationLockBindingEvidence.DURABLE): RestartRoute.S4C_ROLLBACK,
    (DurableWriteOutcome.NOT_DURABLE, OperationLockBindingEvidence.ABSENT): RestartRoute.TERMINAL,
    (DurableWriteOutcome.NOT_DURABLE, OperationLockBindingEvidence.INDETERMINATE): (RestartRoute.OPERATOR_REQUIRED),
    # Un plan presente-pero-no-utilizable NO es un pre-plan: es evidencia
    # contradictoria (S4-C lo trata así en recovery_orchestrator.py:715). Se
    # enruta igual a S4-C porque es quien tiene el detalle fino, pero la fila
    # existe para que el anchor detects si alguien cambia esa intención.
    (DurableWriteOutcome.INDETERMINATE, OperationLockBindingEvidence.DURABLE): RestartRoute.OPERATOR_REQUIRED,
    (DurableWriteOutcome.INDETERMINATE, OperationLockBindingEvidence.ABSENT): RestartRoute.OPERATOR_REQUIRED,
    (DurableWriteOutcome.INDETERMINATE, OperationLockBindingEvidence.INDETERMINATE): (RestartRoute.OPERATOR_REQUIRED),
    # DURABLE + journal ausente: el plan afirma autoridad pero el journal no
    # está. No hay ventana de pre-plan que Normalizar; es evidencia
    # contradictoria y S4-C debe decidir bajo lock retenido.
    (DurableWriteOutcome.DURABLE, OperationLockBindingEvidence.DURABLE): RestartRoute.S4C_ROLLBACK,
    (DurableWriteOutcome.DURABLE, OperationLockBindingEvidence.ABSENT): RestartRoute.OPERATOR_REQUIRED,
    (DurableWriteOutcome.DURABLE, OperationLockBindingEvidence.INDETERMINATE): (RestartRoute.OPERATOR_REQUIRED),
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


def _cerrar_frontera(session: PrivilegedBoundarySession, *, retener_lock: bool) -> bool:
    """Cierra la sesión privileged y DEVUELVE si el lock quedó retenido.

    Antes esta función hacía el cierre a mano, llamando a los metodos de
    `operator_token` y `lock` por fuera y sin tocar `_closed`. El
    resultado era una sesion con los recursos fisicamente cerrados pero
    `closed == False`: `session.lock` seguia devolviendo el handle sin
    avisar, y un `session.close()` posterior intentaba cerrarlo otra vez.

    Ahora la sesion es la UNICA duena de su lifecycle y aca solo se elige la
    politica:

    * `close()` desenlace cerrado. Es correcto en el camino feliz aunque
      S4-D ya haya liberado el lock tras el COMMITTED durable, porque
      `release()` sobre un handle cerrado es un no-op idempotente.
    * `close_retaining_lock()` cualquier otro desenlace. Escribir RELEASED
      sobre un Golden que todavia puede terminar en ROLLBACK_REQUIRED abriria
      la ventana que S4-D declara prohibida.

    Devuelve `True` si el lock quedo RETENIDO para inspeccion (metadata sin
    RELEASED) y `False` si quedo LIBERADO.

    No se puede inferir despues: una vez cerrada, `session.lock` lanza
    `AuthorizationSessionError` —que es exactamente la propiedad que el
    anti-pattern rompia—, asi que el estado fisico tiene que salir de quien
    ejecuto el cierre. Preguntarselo a la sesion DESPUES de cerrarla seria
    volver a depender de un estado que ya no es observable.
    """
    if session.closed:
        return False
    if retener_lock:
        session.close_retaining_lock()
        return True
    session.close()
    return False


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


#: S4-C disposition -> disposicion de S4-E. Congelada por igualdad literal.
#:
#: `PRE_PLAN_LOCK_RECOVERED` es el caso que motiva la fila: S4-C normalizó el
#: lock huérfano de una operación que nunca alcanzó el publish del plan, así
#: que NO hubo MUTATING, NO hubo rollback y NO hubo nada que revertir. Es un
#: desenlace CERRADO y limpio: `NOT_APPLICABLE` (settled, no committed).
#: Proyectarlo como INDETERMINATE decía "no se puede demostrar qué pasó" de
#: algo que S4-C ya demostró y normalizó.
#:
#: El default es INDETERMINATE a propósito: un disposition nuevo de S4-C cae
#: en fail-closed hasta que alguien lo clasifique aquí.
_DISPOSICION_POR_DESENECHO_DE_S4C: Final[dict[RecoveryDisposition, ProtectionDisposition]] = {
    RecoveryDisposition.ROLLED_BACK: ProtectionDisposition.ROLLED_BACK,
    RecoveryDisposition.POST_VERIFICATION_REQUIRED: ProtectionDisposition.ROLLBACK_REQUIRED,
    RecoveryDisposition.PRE_PLAN_LOCK_RECOVERED: ProtectionDisposition.NOT_APPLICABLE,
    RecoveryDisposition.LOCK_BUSY: ProtectionDisposition.LOCK_BUSY,
    RecoveryDisposition.NO_TRANSACTION: ProtectionDisposition.NOT_APPLICABLE,
    RecoveryDisposition.INDETERMINATE: ProtectionDisposition.INDETERMINATE,
    # TERMINAL se decide por el estado durable; se incluye para que el default
    # del .get() no lo toque por accidente.
    RecoveryDisposition.TERMINAL: ProtectionDisposition.REFUSED,
}

#: Estado durable terminal -> disposición, cuando S4-C dice TERMINAL.
#:
#: Se congela por igualdad literal sobre los estados REALES: un terminal nuevo
#: en el enum del journal tiene que romper esta tabla (y con ella el anchor
#: de exhaustividad), no caer en un default que afirme un rollback.
_DISPOSICION_POR_ESTADO_TERMINAL: Final[dict[ProtectionTransactionState, ProtectionDisposition]] = {
    ProtectionTransactionState.COMMITTED: ProtectionDisposition.ALREADY_COMMITTED,
    ProtectionTransactionState.ROLLED_BACK: ProtectionDisposition.ROLLED_BACK,
    # Los cuatro siguientes NO implican restauración física: la operación se
    # cerró antes de mutar nada, o porque el operador no aprobó.
    ProtectionTransactionState.CANCELLED: ProtectionDisposition.NOT_APPLICABLE,
    ProtectionTransactionState.ELEVATION_REJECTED: ProtectionDisposition.REFUSED,
    ProtectionTransactionState.REFUSE_TO_PLAN: ProtectionDisposition.REFUSED,
    ProtectionTransactionState.REFUSE_TO_APPLY: ProtectionDisposition.REFUSED,
    # Los dos fail-closed NO son terminales limpios: exigen operador.
    ProtectionTransactionState.INDETERMINATE: ProtectionDisposition.INDETERMINATE,
    ProtectionTransactionState.ROLLBACK_FAILED: ProtectionDisposition.INDETERMINATE,
}


def _proyeccion_de_recovery(
    reporte: RecoveryForensicReport,
    *,
    ruta: RestartRoute | None = None,
) -> ProtectionOutcome:
    """Proyecta el ``RecoveryForensicReport`` de S4-C.

    GP2-S4E / P5 — cada terminal tiene su PROPIA disposición. Antes, todo
    terminal que no fuera COMMITTED caía en ``ROLLED_BACK``, que es una
    afirmación forense falsa: ``CANCELLED``, ``ELEVATION_REJECTED``,
    ``REFUSE_TO_PLAN`` y ``REFUSE_TO_APPLY`` significan que NO se ocurrió
    restauración física. Afirmar un rollback que no pasó es exactamente el
    tipo de mentira que este paquete no puede permitirse.
    """
    disposition = _DISPOSICION_POR_DESENECHO_DE_S4C.get(
        reporte.disposition,
        ProtectionDisposition.INDETERMINATE,
    )
    if reporte.disposition is RecoveryDisposition.TERMINAL:
        # TERMINAL sin estado durable observable NO es terminal limpio: es
        # evidencia incompleta. Fail-closed, no un default silencioso.
        estado = reporte.observed_transaction_state
        disposition = (
            _DISPOSICION_POR_ESTADO_TERMINAL.get(estado, ProtectionDisposition.INDETERMINATE)
            if estado is not None
            else ProtectionDisposition.INDETERMINATE
        )
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

    # GP2-S4E / P2: publicar el candidate manifest ANTES de abrir la frontera.
    #
    # Sin esto, `promote_durable_authorized_plan` (authorized_plan_store.py)
    # falla con `CandidateManifestUnavailableError`: relee
    # `staging/<op>/candidate_manifest.json` y nada lo escribía. El gap estaba
    # entre planning y S4-A, no dentro de ninguno de los dos.
    #
    # El orden importa: los bytes canónicos salen de planning con el digest ya
    # ligado; S4-A los RELEE y revalida. Publicar antes de la frontera
    # privileged evita abrirla para una promoción que va a fallar, y deja el
    # manifest en disco para que el digest de S4-A sea el que decide.
    #
    # Staging sigue siendo UNTRUSTED: esto no lo convierte en autoridad. El
    # recovery nunca lee de acá; los anchors AST de
    # `test_runtime_vault_s4e_wiring.py` lo prohíben por import y por llamada.
    try:
        publish_candidate_manifest(
            operation_id,
            planificacion.sealed_plan.candidate_manifest_bytes,
            programdata_resolver=frontend.programdata_resolver,
        )
    except Exception as exc:  # noqa: BLE001 — no se abre frontera para una promoción que va a fallar
        return _resultado(
            operation_id=operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.PLANNING,
            source_orchestrator="authorized_plan_store.publish_candidate_manifest",
            detail="no se pudo publicar el candidate manifest en staging; no se abrió frontera",
            fail_closed_reason=f"{type(exc).__name__}: {exc}",
            operator_intervention_required=True,
        )

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
        journal: DurableProtectionJournal | None = None
        try:
            journal = create_protection_journal(
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
                # El apply no llegó a completarse: S4-D NO tiene nada que
                # finalizar. Es un if/else y no un `return` porque el
                # resultado se devuelve DESPUÉS del cierre de frontera (P5-C),
                # y un `return` acá se saltaría tanto el cierre como la
                # reconciliación del estado forense del lock.
                desenlace = _proyeccion_de_apply(reporte_apply)
            else:
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
        finally:
            # El journal es un HANDLE de Win32 con share mode exclusivo: sin
            # cerrarlo, la re-apertura posterior del mismo journal en el mismo
            # proceso falla con sharing violation, y el RIG lo comprueba.
            # Se cierra DESPUÉS de que S4-B y S4-D terminaron de usarlo — cerrarlo
            # antes los dejaría escribiendo sobre un handle cerrado.
            if journal is not None and not journal.is_closed:
                journal.close()
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
            operator_intervention_required=True,
        )

    # GP2-S4E / P5-C — el lock se cierra ANTES de construir el resultado
    # definitivo.
    #
    # Antes el desenlace se construía dentro del try y el `finally` cerraba la
    # frontera después, con lo que `lock_retained` describía una INTENCIÓN y no
    # un estado: un rollback exitoso reportaba `lock_retained=True` mientras la
    # sesión ya había liberado el lock. `resource state == forensic report
    # state` es una propiedad, no una aspiración, así que la proyección se
    # corrige contra el estado físico una vez cerrado.
    #
    # Liberar-vs-retener se decide por el DESENLACE, nunca por un default:
    # soltar el lock sobre un Golden que puede terminar en ROLLBACK_REQUIRED
    # abre la ventana que S4-D declara prohibida. Si ni siquiera se pudo
    # calcular un desenlace, se retiene.
    desenlace = desenlace or _resultado(
        operation_id=operation_id,
        disposition=ProtectionDisposition.INDETERMINATE,
        stage=ProtectionStage.AUTHORIZATION,
        source_orchestrator="protection_service.protect_golden_root",
        detail="la frontera no llegó a producir desenlace",
        fail_closed_reason="sin desenlace calculado: se retiene el lock por defecto",
        operator_intervention_required=True,
    )
    if session is None:
        return desenlace
    lock_retained_fisico = _cerrar_frontera(session, retener_lock=not desenlace.settled)
    return _reconciliar_lock_reportado(desenlace, lock_retained_fisico=lock_retained_fisico)


def _reconciliar_lock_reportado(
    desenlace: ProtectionOutcome,
    *,
    lock_retained_fisico: bool,
) -> ProtectionOutcome:
    """Alinea ``lock_retained`` con el estado FÍSICO del lock ya cerrado.

    La proyección de cada suborquestador se escribe cuando ese suborquestador
    termina, antes de que la frontera decida el destino del lock. Para un
    desenlace cerrado eso es incorrecto: un ``ROLLED_BACK`` con
    ``lock_retained=True`` describe un lock retenido que la sesión acaba de
    liberar.

    El dato viene de quien EJECUTÓ el cierre, no de una segunda predicción, y
    no de inspeccionar la sesión ya cerrada (imposible: ``session.lock``
    lanza una vez que ``_closed`` es ``True``).

    Sólo baja el flag, nunca lo sube: un ``lock_retained=True`` que el cierre
    realmente retuvo es correcto, y bajarlo sería mentir en el otro sentido.
    """
    if lock_retained_fisico or not desenlace.lock_retained:
        return desenlace
    return replace(desenlace, lock_retained=False)


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

    # GP2-S4E / P3 — PRE-JOURNAL.
    #
    # Un journal ABSENT no es evidencia rota: hay una ventana legítima del
    # protocolo donde el binding es durable y el lock está tomado, pero el
    # plan y el journal todavía no existen. Confundir ABSENT con
    # INDETERMINATE hacía inalcanzable `PRE_PLAN_LOCK_RECOVERED` de S4-C.
    #
    # `classify_protection_journal` ya clasificó; acá sólo se decide a qué
    # suborquestador le toca. Staging NO participa: la autoridad de esta
    # decisión es el binding durable y el filesystem.
    if clasificacion.classification is ProtectionJournalClassification.ABSENT:
        return _resolver_pre_journal(operation_id, frontend)

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


def _resolver_pre_journal(operation_id: str, frontend: _Frontend) -> ProtectionOutcome:
    """Decide el camino cuando NO hay journal, leyendo el binding durable.

    Tres desenlaces, y sólo uno muta:

    * binding DURABLE + plan ausente → S4-C, que toma el lock huérfano de la
      MISMA ``operation_id`` y devuelve ``PRE_PLAN_LOCK_RECOVERED``. Es la
      ventana real del protocolo.
    * binding ABSENT + plan ausente → no hay transacción: cero escrituras.
    * cualquier otro caso (binding ilegible, plan presente-pero-no-utilizable)
      → fail-closed con operador requerido.
    """
    plan = classify_durable_authorized_plan(operation_id, programdata_resolver=frontend.programdata_resolver)
    binding = classify_operation_lock_binding(operation_id, programdata_resolver=frontend.programdata_resolver)
    ruta = PRE_JOURNAL_ROUTES[(plan, binding)]
    _registrar(
        f"pre-journal: plan={plan.value} binding={binding.value} -> ruta '{ruta.value}'",
        operation_id,
        stage=ProtectionStage.ROUTING,
        extra={"route": ruta.value, "plan": plan.value, "binding": binding.value},
    )

    match ruta:
        case RestartRoute.S4C_ROLLBACK:
            # S4-C normaliza el lock huérfano. Cero ACLs: en pre-plan no hay
            # apply que deshacer, y el report de S4-C lo afirma.
            return _reanudar_por_s4c(operation_id, frontend, ruta=ruta)
        case RestartRoute.TERMINAL:
            return _resultado(
                operation_id=operation_id,
                disposition=ProtectionDisposition.NOT_APPLICABLE,
                stage=ProtectionStage.ROUTING,
                source_orchestrator="protection_service.resume_golden_protection",
                route=ruta,
                detail="no hay evidencia durable de transaccion: ni plan, ni journal, ni binding",
                fail_closed_reason="",
            )
        case _:
            return _resultado(
                operation_id=operation_id,
                disposition=ProtectionDisposition.INDETERMINATE,
                stage=ProtectionStage.ROUTING,
                source_orchestrator="protection_service.resume_golden_protection",
                route=ruta,
                detail=(
                    f"evidencia durable contradictoria pre-journal "
                    f"(plan={plan.value}, binding={binding.value}): cero gates, cero escrituras"
                ),
                fail_closed_reason=(
                    f"plan={plan.value} + binding={binding.value}: no se puede decidir el camino sin inventar evidencia"
                ),
                lock_retained=True,
                operator_intervention_required=True,
            )


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
    try:
        return _reanudar_por_s4d(
            continuation.report.operation_id,
            frontend,
            ruta=ruta,
            continuation_lock=handle,
        )
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
        # Contrato CAUSAL, no inferencia. Un retorno normal de S4-D NO prueba
        # que consumió el handle: `_reanudar_por_s4d` puede volver temprano
        # (plan ilegible, journal ausente, digest incoherente, estado no
        # admisible, normalización COMMITTED) sin haber llegado jamás a
        # `_adquirir_lock_para_finalizacion`. Inferir ownership desde el retorno
        # producía un HANDLE LEAK silencioso.
        #
        # La evidencia es `handle.closed`: si S4-D consumió y liberó/retenió el
        # handle, el kernel handle está cerrado; si retornó antes de consumirlo,
        # sigue abierto y el owner sigue siendo S4-E.
        #
        # Se usa `retain_for_inspection()` y NUNCA `release()`: cerrar el
        # kernel handle sin escribir `RELEASED` deja la evidencia durable no
        # terminal y permite que el recovery futuro re-tome la MISMA
        # operación. Escribir `RELEASED` sobre un Golden que S4-D no verificó
        # abriría la ventana que todo este diseño existe para evitar.
        if not handle.closed:
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
    try:
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
    except Exception as exc:  # noqa: BLE001 — el servicio promete ProtectionOutcome; nada escapa
        #
        # Frontera de dominio de S4-D: construcción del puerto de verificación
        # (plataforma no soportada, WAL inválido, puerto mal formado), el
        # finalizador mismo y la validación del lock. Todo eso se traduce a
        # INDETERMINATE con la cadena causal, NUNCA a un desenlace que afirme
        # éxito.
        #
        # `lock_retained=True` es la opción honesta: si el error ocurrió con el
        # handle en manos de S4-D, ya lo retuvo él; si ocurrió antes, el
        # `finally` de `_proyectar_continuacion` (o el cierre de la frontera en
        # el camino fresh) lo retiene. En ninguno de los dos casos corresponde
        # afirmar que el lock quedó libre.
        logger.exception(
            "Fallo de dominio en S4-D; fail-closed con evidencia preservada",
            extra={"operation_id": operation_id, "stage": ProtectionStage.FINALIZATION.value},
        )
        return _resultado(
            operation_id=operation_id,
            disposition=ProtectionDisposition.INDETERMINATE,
            stage=ProtectionStage.FINALIZATION,
            source_orchestrator="finalization_orchestrator.finalize_protection_transaction",
            detail="S4-D no pudo completarse; la evidencia durable queda sin resolver",
            fail_closed_reason=f"{type(exc).__name__}: {exc}",
            route=ruta,
            lock_retained=True,
            operator_intervention_required=True,
        )
    finally:
        # El journal abierto es un HANDLE de Win32. Sin cerrarlo, la
        # re-apertura posterior del mismo journal en el mismo proceso falla con
        # sharing violation. Se cierra exactamente una vez, después de que S4-D
        # terminó de usarlo — nunca antes, porque S4-D lee y escribe en él.
        if not journal.is_closed:
            journal.close()

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


@dataclass(frozen=True, slots=True)
class StartupDiagnosis:
    """Diagnóstico READ-ONLY de una operación pendiente al arranque.

    No muta nada, no abre frontera y no ejecuta gates. Clasifica la evidencia
    durable y dice **qué haría falta** para cerrarla.

    Es la respuesta honesta a un helper no provisionado: el arranque normal
    SÍ puede leer y clasificar (no necesita `WRITE_DAC` para eso), pero NO
    puede deshacer un apply ni cerrar un `COMMITTED` sin la frontera
    privilegiada. Inventar esa frontera —lanzar `python.exe` elevado, un
    `runas` improvisado, o correr la mutation en un `asyncio.to_thread` que no
    cambia el token de seguridad— sería afirmar una garantía que no existe.
    """

    operation_id: str
    route: RestartRoute
    journal_state: ProtectionTransactionState | None
    binding_evidence: OperationLockBindingEvidence
    #: ``True`` cuando cerrarla requiere la frontera privilegiada.
    requiere_privilegios: bool
    #: Por qué, en prosa: esto es lo que el operador va a leer.
    motivo: str

    @property
    def bloquea_operador(self) -> bool:
        """Si el arranque debe dejar constancia visible de esta operación."""
        return self.requiere_privilegios


#: Rutas que NO necesitan la frontera privilegiada para cerrarse.
#:
#: `PRE_PLAN_LOCK_RECOVERED` es el ejemplo importante: S4-C toma el lock
#: huérfano y lo NORMALIZA sin tocar una sola ACL. El arranque puede resolverlo
#: sin privilegios — SIEMPRE que el lock esté tomado por un proceso muerto,
#: que es exactamente la condición de la ventana pre-plan.
#: Cualquier otra cosa muta el Golden o sus SDs.
_RUTAS_SIN_PRIVILEGIOS: Final[frozenset[RestartRoute]] = frozenset({RestartRoute.TERMINAL})


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

    def diagnosticar_arranque(
        self,
        *,
        programdata_resolver: Callable[[], object] | None = None,
    ) -> tuple[StartupDiagnosis, ...]:
        """Clasifica read-only las operaciones pendientes. NO muta nada.

        Es el único trabajo de arranque que hace falta sin frontera
        privilegiada: leer la evidencia durable y decidir qué falta. Se puede
        hacer en el proceso normal porque no escribe ni pide `WRITE_DAC`.

        Lo que NO hace —y es deliberado— es ejecutar el recovery. Con
        ``PACKAGED_HELPER_PROVISIONING_STATUS == "UNRESOLVED"`` no existe una
        forma honesta de levantar la frontera privilegiada desde acá, así que
        el arranque clasifica y reporta en vez de fingir que reconcilió.

        Importa distinguir esto de "no hacer nada": una operación que SÍ puede
        cerrarse sin privilegios (el pre-plan) queda marcada como tal, y el
        operador puede ejecutarla; una que NO puede queda marcada
        ``requiere_privilegios=True`` y el arranque lo publica.
        """
        pendientes = discover_pending_operations(programdata_resolver=programdata_resolver)
        diagnostico: list[StartupDiagnosis] = []
        for entrada in pendientes:
            ruta = entrada.route
            requiere = ruta not in _RUTAS_SIN_PRIVILEGIOS
            motivo = (
                "no requiere la frontera privilegiada: S4-C normaliza el lock sin tocar ACLs"
                if not requiere
                else (
                    "cierre mutante (rollback de SDs, gates o archivado): requiere la frontera "
                    f"privilegiada, y PACKAGED_HELPER_PROVISIONING_STATUS=UNRESOLVED "
                    f"(ruta '{ruta.value}')"
                )
            )
            diagnostico.append(
                StartupDiagnosis(
                    operation_id=entrada.operation_id,
                    route=ruta,
                    journal_state=entrada.journal_state,
                    binding_evidence=entrada.binding_evidence,
                    requiere_privilegios=requiere,
                    motivo=motivo,
                )
            )
        return tuple(diagnostico)


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
    "PRE_JOURNAL_ROUTES",
    "RESTART_ROUTES",
    "RestartRoute",
    "RuntimeVaultProtectionCoordinator",
    "StartupDiagnosis",
    "ROLLED_BACK_DISPOSITIONS",
    "SETTLED_DISPOSITIONS",
    "TERMINAL_DISPOSITIONS",
    "UNTRUSTED_STAGING_AS_RECOVERY_SOURCE",
    "discover_pending_operations",
    "protect_golden_root",
    "resume_golden_protection",
    "route_for_state",
]
