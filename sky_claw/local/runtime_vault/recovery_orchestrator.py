"""GP2-S4C — recovery de operaciones de protección interrumpidas por crash.

Después de un crash, LA MEMORIA NO EXISTE: ``ApplyReport``, objetos Python,
estado cacheado y ``INDETERMINATE`` en memoria no son autoridad. La
recuperación sólo confía en evidencia durable y observable:

    RECOVERY_AUTHORITY =
        authorized_plan.json (protegido, revalidado)        §9.2.8 / §27
      + protection_journal.json (durable, clasificado)      §19 / §20
      + metadata del GoldenMutationLock (dueno vivo/muerto) §19.1.5
      + estado físico fresco del Golden (por handle)        §12.2

``UNTRUSTED_STAGING`` NUNCA es fuente de recuperación
(``RECOVERY_SOURCE = AUTHORIZED_OPERATIONS ONLY``); el PRE restaurado sale
exclusivamente de ``authorized_plan.json``.

Invariante central (§20 C4): ``APPLYING`` tras un reinicio NO significa
«seguir aplicando». ``MUTATING(K)`` significa que K PUEDE estar mutado: la
recuperación reconcilia (rollback idempotente) o rehúsa como
``INDETERMINATE``; jamás reanuda el apply.

Idempotencia de rollback (§20 C8, respuesta normativa del slice): Process C no
necesita saber qué nodos restauró Process B antes de morir. El WAL por nodo
(``None -> MUTATING -> MUTATED``) está congelado y NO se extiende con
``RESTORING``/``RESTORED``. En su lugar, por cada nodo candidato en orden
TOP-DOWN: se abre el handle, se revalida la identidad física
(``VolumeSerialNumber``/``FileId``, reparse, hardlink) y se ejecuta una sonda
SEMÁNTICA contra el PRE autorizado (``verify_restored_pre_sd``, el mismo
contrato productivo de #644 — nunca igualdad raw de bytes). Si el nodo ya está
en PRE (nunca mutado o restaurado por un proceso muerto), se salta sin
escribir; si no, se restaura el PRE autorizado y se verifica. Cualquier crash
posterior deja evidencia durable suficiente para que un proceso nuevo repita
el procedimiento desde cero.

Frontera S4-C/S4-D: ``APPLYING`` con TODOS los nodos ``MUTATED`` durables se
clasifica como ``POST_VERIFICATION_REQUIRED`` (una disposición de recuperación,
no un estado del FSM): el apply físico quedó completo y la post-verificación
(GP1/RV-2/NodeSet) y el COMMITTED pertenecen a S4-D. Este módulo JAMÁS escribe
``VERIFYING_*``, ``COMMITTED`` ni registros de nodo nuevos en el WAL.

El lock se re-adquiere SIEMPRE antes de tocar ACLs (§9): un lock huérfano
(dueño muerto o PID reutilizado, demostrado por pid+creation-time) sólo puede
ser tomado por el mismo ``operation_id``; un dueño vivo o ilegible produce
``LOCK_BUSY`` sin robo. El lock queda RETENIDO (no se escribe
``phase=RELEASED``) en ``INDETERMINATE`` — para inspección — y en
``POST_VERIFICATION_REQUIRED``: §47 exige no liberarlo antes de la transición
durable terminal, que en el handoff pertenece a S4-D. Retenerlo también
serializa el Golden: mientras la operación no tenga transición terminal, una
operación nueva cae en la ruta de huérfano -> recovery en vez de pisar el
estado a medias; la continuación de S4-D re-toma el MISMO ``operation_id``.

Este módulo COMPONE: no construye Target DACLs, no implementa otro WAL, no
duplica el lock y no invoca ``SetSecurityInfo`` directamente — muta a través
del puerto (``NodeRecoveryPort``), cuya implementación productiva es
``HandleBoundTargetDaclPort`` (``target_dacl``, la única capa auditada).
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sky_claw.local.runtime_vault.authorized_plan_store import (
    DurableAuthorizedPlan,
    DurableWriteOutcome,
    classify_durable_authorized_plan,
    load_durable_authorized_plan,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockBusyError,
    GoldenLockError,
    GoldenLockKernel,
    GoldenLockOrphanedOperationMismatchError,
    GoldenMutationLockHandle,
    PreexistingLockDisposition,
    RecoveryLockAcquisition,
    acquire_golden_mutation_lock_for_recovery,
)
from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.mutation_executor import (
    NodeIdentity,
    derive_node_path,
    rollback_order,
)
from sky_claw.local.runtime_vault.protection_journal import (
    TERMINAL_TRANSACTION_STATES,
    NodeWalState,
    ProtectionJournal,
    ProtectionTransactionState,
)
from sky_claw.local.runtime_vault.protection_journal_store import (
    DurableProtectionJournal,
    JournalDurabilityKernel,
    ProtectionJournalClassification,
    classify_protection_journal,
    open_protection_journal,
)

logger = logging.getLogger(__name__)

# ============================================================================
# Jerarquía de excepciones
# ============================================================================


class RecoveryOrchestratorError(RuntimeVaultError):
    """Base de las excepciones del orquestador de recuperación (S4-C)."""


class RecoveryUnsupportedError(RecoveryOrchestratorError):
    """Plataforma sin las garantías Win32 que el recovery real necesita."""


# ============================================================================
# DTOs puros (§7 del encargo: RecoveryDisposition != ProtectionTransactionState)
# ============================================================================


class RecoveryDisposition(StrEnum):
    """Decisión de recuperación. NO es un estado del FSM autoritativo.

    El FSM sigue viviendo en el journal (``ProtectionTransactionState``); esta
    enumeración describe QUÉ HACER con la evidencia observada, para no agregar
    estados al FSM por conveniencia.
    """

    NO_TRANSACTION = "no_transaction"
    ROLLED_BACK = "rolled_back"
    POST_VERIFICATION_REQUIRED = "post_verification_required"
    INDETERMINATE = "indeterminate"
    LOCK_BUSY = "lock_busy"
    TERMINAL = "terminal"


class RecoveryLockOutcome(StrEnum):
    """Desenlace del ciclo del GoldenMutationLock durante el recovery."""

    NOT_ATTEMPTED = "not_attempted"
    ACQUIRED_RELEASED = "acquired_released"
    ACQUIRED_RETAINED = "acquired_retained"
    BUSY = "busy"
    REFUSED = "refused"


class RecoveryIdentitySource(StrEnum):
    """Fuente durable de la identidad física del Golden (nunca el caller)."""

    AUTHORIZED_PLAN = "authorized_plan"
    PROTECTION_JOURNAL = "protection_journal"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class NodeWalSummary:
    """Resumen del WAL de UN nodo (último registro durable; sin bytes de SD)."""

    relative_path: str
    wal_state: str


@dataclass(frozen=True, slots=True)
class RecoveryForensicReport:
    """Resultado tipado y seguro de una recuperación (§32 del encargo).

    No contiene bytes de Security Descriptor ni rutas absolutas: sólo
    clasificaciones, conteos, identidades relativas del plan y disposición.
    """

    operation_id: str
    plan_classification: DurableWriteOutcome
    journal_classification: ProtectionJournalClassification
    observed_transaction_state: ProtectionTransactionState | None
    node_wal_summary: tuple[NodeWalSummary, ...]
    physical_identity_source: RecoveryIdentitySource
    lock_outcome: RecoveryLockOutcome
    stale_lock_takeover: bool
    disposition: RecoveryDisposition
    nodes_restored: tuple[str, ...]
    nodes_skipped: tuple[str, ...]
    nodes_pending: tuple[str, ...]
    physical_restoration_completed: bool
    operator_intervention_required: bool
    indeterminate_reason: str
    detail: str
    setsecurityinfo_calls: int


# ============================================================================
# Puerto de recuperación (composición, nunca primitivas crudas)
# ============================================================================


class NodeRecoveryPort(Protocol):
    """Subconjunto del contrato de mutación que el recovery necesita.

    Sólo lectura de identidad + restauración semántica por handle. El recovery
    NO aplica Target DACLs ni compara hashes raw: la fidelidad de la
    restauración es la verificación SEMÁNTICA de #644
    (``verify_restored_security_descriptor_by_handle``).
    """

    def open(self, path: pathlib.Path, node_kind: GoldenProtectionNodeKind) -> int: ...

    def close(self, handle: int) -> None: ...

    def read_identity(self, handle: int) -> NodeIdentity: ...

    def restore_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        """``SetSecurityInfo`` de restauración REAL sobre el handle (§12.2)."""
        ...

    def verify_restored_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        """Verificación SEMÁNTICA post-restauración sobre EL MISMO handle."""
        ...

    @property
    def setsecurityinfo_calls(self) -> int: ...


def _resolver_puerto(port: NodeRecoveryPort | None) -> NodeRecoveryPort:
    """Sin puerto inyectado, la restauración real la aporta ``target_dacl``."""
    if port is not None:
        return port
    if sys.platform != "win32":
        raise RecoveryUnsupportedError(
            "El recovery real requiere las garantías Win32 (SetSecurityInfo por handle sobre el namespace)"
        )
    from sky_claw.local.runtime_vault.mutation_executor import HandleBoundTargetDaclPort

    return HandleBoundTargetDaclPort()


# ============================================================================
# Resultado interno del dispatch
# ============================================================================


@dataclass(frozen=True, slots=True)
class _Resultado:
    disposition: RecoveryDisposition
    retener_lock: bool
    operator: bool
    nodes_restored: tuple[str, ...] = ()
    nodes_skipped: tuple[str, ...] = ()
    nodes_pending: tuple[str, ...] = ()
    physical_completed: bool = False
    reason: str = ""
    detail: str = ""
    setsecurityinfo_calls: int = 0


def _reporte(
    operation_id: str,
    plan_classification: DurableWriteOutcome,
    journal_classification: ProtectionJournalClassification,
    *,
    state: ProtectionTransactionState | None,
    resumen: tuple[NodeWalSummary, ...],
    fuente: RecoveryIdentitySource,
    lock_outcome: RecoveryLockOutcome,
    stale_takeover: bool,
    resultado: _Resultado,
) -> RecoveryForensicReport:
    return RecoveryForensicReport(
        operation_id=operation_id,
        plan_classification=plan_classification,
        journal_classification=journal_classification,
        observed_transaction_state=state,
        node_wal_summary=resumen,
        physical_identity_source=fuente,
        lock_outcome=lock_outcome,
        stale_lock_takeover=stale_takeover,
        disposition=resultado.disposition,
        nodes_restored=resultado.nodes_restored,
        nodes_skipped=resultado.nodes_skipped,
        nodes_pending=resultado.nodes_pending,
        physical_restoration_completed=resultado.physical_completed,
        operator_intervention_required=resultado.operator,
        indeterminate_reason=resultado.reason,
        detail=resultado.detail,
        setsecurityinfo_calls=resultado.setsecurityinfo_calls,
    )


# ============================================================================
# Gates de identidad física (HANDLE > PATH)
# ============================================================================


def _motivo_identidad(node: NodeSecurityBackup, observed: NodeIdentity) -> str | None:
    """Motivo de detención si la identidad viva no corresponde; ``None`` si ok."""
    if observed.volume_serial_number != node.volume_serial_number:
        return (
            f"drift de VolumeSerialNumber en '{node.relative_path}': "
            f"esperado={node.volume_serial_number}, observado={observed.volume_serial_number}"
        )
    if observed.file_id != node.file_id:
        return f"drift de FileId en '{node.relative_path}': esperado={node.file_id}, observado={observed.file_id}"
    if observed.reparse_tag != 0:
        return f"'{node.relative_path}' es un reparse point (tag=0x{observed.reparse_tag:08X})"
    if node.node_kind is GoldenProtectionNodeKind.FILE and observed.number_of_links != 1:
        return f"hardlink externo en '{node.relative_path}': NumberOfLinks={observed.number_of_links} != 1"
    return None


@dataclass(frozen=True, slots=True)
class _EvaluacionNodo:
    estado: str  # "en_pre" | "necesita_restore" | "detener"
    motivo: str = ""


def _evaluar_nodo(
    port: NodeRecoveryPort,
    path: pathlib.Path,
    node: NodeSecurityBackup,
    *,
    es_candidato: bool,
) -> _EvaluacionNodo:
    """Evaluación READ-ONLY de un nodo: identidad + sonda semántica contra PRE."""
    handle = 0
    try:
        handle = port.open(path, node.node_kind)
    except Exception as exc:  # noqa: BLE001 — boundary deliberada: se clasifica, no se propaga
        return _EvaluacionNodo("detener", f"no se pudo abrir '{node.relative_path}': {exc}")
    try:
        try:
            observed = port.read_identity(handle)
            motivo = _motivo_identidad(node, observed)
            if motivo is not None:
                return _EvaluacionNodo("detener", motivo)
            try:
                port.verify_restored_pre_sd(handle, node)
                return _EvaluacionNodo("en_pre")
            except Exception as exc:  # noqa: BLE001 — sonda fallida: drift o nodo mutado
                if es_candidato:
                    return _EvaluacionNodo("necesita_restore")
                return _EvaluacionNodo(
                    "detener",
                    f"drift contra PRE en nodo sin registro durable '{node.relative_path}': {exc}",
                )
        except Exception as exc:  # noqa: BLE001
            return _EvaluacionNodo("detener", f"evaluación falló en '{node.relative_path}': {exc}")
    finally:
        with contextlib.suppress(Exception):  # el cierre no debe enmascarar el desenlace
            port.close(handle)


def _restaurar_nodo(port: NodeRecoveryPort, path: pathlib.Path, node: NodeSecurityBackup) -> str | None:
    """Restaura el PRE autorizado y lo verifica SEMÁNTICAMENTE. Motivo o ``None``."""
    handle = 0
    try:
        handle = port.open(path, node.node_kind)
    except Exception as exc:  # noqa: BLE001
        return f"no se pudo abrir '{node.relative_path}' para restaurar: {exc}"
    try:
        observed = port.read_identity(handle)
        motivo = _motivo_identidad(node, observed)
        if motivo is not None:
            return motivo
        port.restore_pre_sd(handle, node)
        port.verify_restored_pre_sd(handle, node)
        return None
    except Exception as exc:  # noqa: BLE001 — boundary deliberada
        return f"restauración de '{node.relative_path}' falló: {exc}"
    finally:
        with contextlib.suppress(Exception):
            port.close(handle)


def _transicionar(journal: DurableProtectionJournal, target: ProtectionTransactionState) -> str | None:
    """Transición durable; devuelve motivo del fallo o ``None``.

    NUNCA se suprime un fallo de transición: un flush no confirmado deja la
    evidencia durable en estado desconocido (§20 C4b) y el desenlace honesto
    es ``INDETERMINATE`` con el lock retenido, no un estado fabricado.
    """
    try:
        journal.transition_to(target)
    except Exception as exc:  # noqa: BLE001 — boundary deliberada: se clasifica el fallo
        return f"transición durable a '{target.value}' no confirmada: {exc}"
    return None


def _append_indeterminate_best_effort(journal: DurableProtectionJournal) -> None:
    """Asienta ``INDETERMINATE`` si el journal aún puede anexar (best-effort).

    Si el journal está envenenado por un fallo de durabilidad previo, el estado
    durable existente ya es la evidencia honesta: no se fabrica nada.
    """
    with contextlib.suppress(Exception):
        journal.transition_to(ProtectionTransactionState.INDETERMINATE)


# ============================================================================
# Dispatch del FSM autoritativo (§20 C3, C4, C5, C6/C7, C8, terminales)
# ============================================================================


def _relpaths_en_estado(journal: ProtectionJournal, estado: NodeWalState) -> tuple[str, ...]:
    return tuple(registro.relative_path for registro in journal.nodes_in_state(estado))


def _resumen_wal(
    journal: ProtectionJournal,
    plan: DurableAuthorizedPlan,
) -> tuple[NodeWalSummary, ...]:
    """Último estado durable por nodo, en el orden de la tabla del plan."""
    ultimos: dict[str, str] = {}
    for registro in journal.node_records:
        ultimos[registro.relative_path] = registro.state.value
    orden = [n.relative_path for n in plan.plan.nodes]
    resumen = [NodeWalSummary(relative_path=rel, wal_state=ultimos[rel]) for rel in orden if rel in ultimos]
    extras = [rel for rel in ultimos if rel not in set(orden)]
    resumen.extend(NodeWalSummary(relative_path=rel, wal_state=ultimos[rel]) for rel in extras)
    return tuple(resumen)


def _nodo_wal_desalineado(journal: ProtectionJournal, plan: DurableAuthorizedPlan) -> str | None:
    """Cruce per-nodo WAL<->plan: identidad física y PRE autorizado.

    Un registro de nodo cuyo binding no corresponde a la tabla del plan
    autoritativo convierte cualquier clasificación de éxito en evidencia
    contradictoria.
    """
    for registro in journal.node_records:
        nodo = plan.node_for(registro.relative_path)
        if nodo is None:
            return f"el journal nombra '{registro.relative_path}', ausente del plan autoritativo"
        if (
            registro.volume_serial_number != nodo.volume_serial_number
            or registro.file_id != nodo.file_id
            or registro.pre_sd_sha256 != nodo.pre_sd_sha256
        ):
            return f"binding del WAL de '{registro.relative_path}' no corresponde al plan autoritativo"
    return None


def _flujo_rollback(
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    port: NodeRecoveryPort,
    candidatos: frozenset[str],
) -> _Resultado:
    """Rollback recuperable e idempotente (§17, §20 C4/C8).

    READ-ONLY primero: se escanean TODOS los nodos del plan revalidando
    identidad y comparando semánticamente contra el PRE autorizado, antes de
    ejecutar el primer ``SetSecurityInfo``. Cualquier evidencia contradictoria
    detiene el flujo sin escribir.
    """
    if journal.transaction_state is ProtectionTransactionState.APPLYING:
        motivo = _transicionar(journal, ProtectionTransactionState.ROLLBACK_REQUIRED)
        if motivo is not None:
            return _Resultado(
                disposition=RecoveryDisposition.INDETERMINATE,
                retener_lock=True,
                operator=True,
                reason=motivo,
                detail="rollback rehusado: la transición durable a rollback_required no se confirmó",
                setsecurityinfo_calls=port.setsecurityinfo_calls,
            )
    if journal.transaction_state is ProtectionTransactionState.ROLLBACK_REQUIRED:
        motivo = _transicionar(journal, ProtectionTransactionState.ROLLING_BACK)
        if motivo is not None:
            return _Resultado(
                disposition=RecoveryDisposition.INDETERMINATE,
                retener_lock=True,
                operator=True,
                reason=motivo,
                detail="rollback rehusado: la transición durable a rolling_back no se confirmó",
                setsecurityinfo_calls=port.setsecurityinfo_calls,
            )

    # --- Fase 1: escaneo READ-ONLY completo (sin un solo SetSecurityInfo) ---
    dentro_de_pre: list[str] = []
    necesita_restore: list[str] = []
    for node in plan.plan.nodes:
        path = derive_node_path(plan, node.relative_path)
        evaluacion = _evaluar_nodo(port, path, node, es_candidato=node.relative_path in candidatos)
        if evaluacion.estado == "en_pre":
            dentro_de_pre.append(node.relative_path)
        elif evaluacion.estado == "necesita_restore":
            necesita_restore.append(node.relative_path)
        else:
            _append_indeterminate_best_effort(journal)
            return _Resultado(
                disposition=RecoveryDisposition.INDETERMINATE,
                retener_lock=True,
                operator=True,
                nodes_skipped=tuple(dentro_de_pre),
                nodes_pending=(node.relative_path, *necesita_restore),
                reason=evaluacion.motivo,
                detail="evidencia contradictoria o ilegible durante el escaneo: cero escrituras sobre el Golden",
                setsecurityinfo_calls=port.setsecurityinfo_calls,
            )

    # --- Fase 2: restauración TOP-DOWN de lo que difiere del PRE autorizado ---
    restaurados: list[str] = []
    pendientes = [n.relative_path for n in rollback_order(plan, necesita_restore)]
    for node in rollback_order(plan, necesita_restore):
        path = derive_node_path(plan, node.relative_path)
        motivo = _restaurar_nodo(port, path, node)
        if motivo is not None:
            _append_indeterminate_best_effort(journal)
            restaurados_set = set(restaurados)
            pendientes = [rel for rel in pendientes if rel not in restaurados_set]
            return _Resultado(
                disposition=RecoveryDisposition.INDETERMINATE,
                retener_lock=True,
                operator=True,
                nodes_restored=tuple(restaurados),
                nodes_skipped=tuple(dentro_de_pre),
                nodes_pending=tuple(pendientes),
                physical_completed=False,
                reason=motivo,
                detail="rollback detenido: la restauración física no pudo completarse",
                setsecurityinfo_calls=port.setsecurityinfo_calls,
            )
        restaurados.append(node.relative_path)

    # --- Fase 3: transición terminal durable ---
    motivo = _transicionar(journal, ProtectionTransactionState.ROLLED_BACK)
    if motivo is not None:
        return _Resultado(
            disposition=RecoveryDisposition.INDETERMINATE,
            retener_lock=True,
            operator=True,
            nodes_restored=tuple(restaurados),
            nodes_skipped=tuple(dentro_de_pre),
            physical_completed=True,
            reason=motivo,
            detail=(
                "restauración física completada y verificada, pero el asiento durable final no se confirmó: "
                "el desenlace transaccional es desconocido (§48)"
            ),
            setsecurityinfo_calls=port.setsecurityinfo_calls,
        )
    return _Resultado(
        disposition=RecoveryDisposition.ROLLED_BACK,
        retener_lock=False,
        operator=False,
        nodes_restored=tuple(restaurados),
        nodes_skipped=tuple(dentro_de_pre),
        physical_completed=True,
        setsecurityinfo_calls=port.setsecurityinfo_calls,
    )


def _dispatch_fsm(
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    port: NodeRecoveryPort | None,
) -> _Resultado:
    """Decide por el estado AUTORITATIVO del journal (nunca por memoria previa).

    El puerto se resuelve SÓLO si el flujo de rollback lo necesita: los caminos
    de handoff (C5/C6/C7) y terminales no tocan nodos y no exigen plataforma.
    """
    estado = journal.transaction_state

    desalineado = _nodo_wal_desalineado(journal.journal, plan)
    if desalineado is not None:
        _append_indeterminate_best_effort(journal)
        return _Resultado(
            disposition=RecoveryDisposition.INDETERMINATE,
            retener_lock=True,
            operator=True,
            reason=desalineado,
            detail="el WAL del journal no corresponde al plan autoritativo",
        )

    if estado is ProtectionTransactionState.APPLYING:
        mutating = _relpaths_en_estado(journal.journal, NodeWalState.MUTATING)
        mutated = _relpaths_en_estado(journal.journal, NodeWalState.MUTATED)
        if not mutating and not mutated:
            # C3: APPLYING con cero nodos => rollback vacío por transiciones
            # declaradas (ROLLBACK_REQUIRED -> ROLLING_BACK -> ROLLED_BACK).
            return _flujo_rollback(plan, journal, _resolver_puerto(port), candidatos=frozenset())
        relpaths_plan = frozenset(n.relative_path for n in plan.plan.nodes)
        if not mutating and frozenset(mutated) == relpaths_plan:
            # C5: apply físico completo => handoff a la post-verificación de
            # S4-D. S4-C NO escribe VERIFYING_GP1 ni COMMITTED. El lock se
            # RETIENE (§47: no se libera antes de la transición durable
            # terminal, que pertenece a S4-D): mientras X no termine, una
            # operación nueva sobre el mismo Golden queda serializada por la
            # ruta de huérfano -> recovery, y la continuación de S4-D re-toma
            # el lock con el MISMO operation_id.
            return _Resultado(
                disposition=RecoveryDisposition.POST_VERIFICATION_REQUIRED,
                retener_lock=True,
                operator=False,
                detail=(
                    "todos los nodos del plan tienen MUTATED durable: el apply físico quedó completo; "
                    "GP1/RV-2/NodeSet/archivado y COMMITTED pertenecen a S4-D; lock retenido hasta esa "
                    "transición terminal"
                ),
            )
        return _flujo_rollback(
            plan, journal, _resolver_puerto(port), candidatos=frozenset(mutating) | frozenset(mutated)
        )

    if estado in (ProtectionTransactionState.ROLLBACK_REQUIRED, ProtectionTransactionState.ROLLING_BACK):
        mutating = _relpaths_en_estado(journal.journal, NodeWalState.MUTATING)
        mutated = _relpaths_en_estado(journal.journal, NodeWalState.MUTATED)
        return _flujo_rollback(
            plan, journal, _resolver_puerto(port), candidatos=frozenset(mutating) | frozenset(mutated)
        )

    if estado in (
        ProtectionTransactionState.VERIFYING_GP1,
        ProtectionTransactionState.VERIFYING_RV2,
        ProtectionTransactionState.VERIFYING_NODE_SET,
        ProtectionTransactionState.ARCHIVING_BACKUP,
    ):
        # C6/C7: las verificaciones y el archivado son de S4-D. S4-C clasifica
        # y entrega; no ejecuta verificaciones ficticias ni comitea. El lock se
        # retiene por la misma razón que en C5: X sigue sin transición terminal.
        return _Resultado(
            disposition=RecoveryDisposition.POST_VERIFICATION_REQUIRED,
            retener_lock=True,
            operator=False,
            detail=(
                f"estado durable '{estado.value}': la post-verificación/archivado pertenecen a S4-D; "
                "lock retenido hasta la transición terminal"
            ),
        )

    if estado in TERMINAL_TRANSACTION_STATES:
        inspeccion = estado in (
            ProtectionTransactionState.INDETERMINATE,
            ProtectionTransactionState.ROLLBACK_FAILED,
        )
        return _Resultado(
            disposition=RecoveryDisposition.TERMINAL,
            retener_lock=inspeccion,
            operator=inspeccion,
            reason="" if not inspeccion else f"estado terminal '{estado.value}' requiere inspección del operador",
            detail=f"estado terminal durable '{estado.value}': sin acción automática (idempotente)",
        )

    # PREPARING/PREPARED/AWAITING_ELEVATION: imposibles bajo el replay del
    # journal autoritativo (el estado inicial es 'applying' y no hay aristas de
    # retorno). Evidencia contradictoria: fail-closed.
    _append_indeterminate_best_effort(journal)
    return _Resultado(
        disposition=RecoveryDisposition.INDETERMINATE,
        retener_lock=True,
        operator=True,
        reason=f"estado transaccional imposible en el journal autoritativo: '{estado.value}'",
        detail="evidencia contradictoria: no se actúa automáticamente",
    )


# ============================================================================
# Bajo lock: reload fresco + dispatch
# ============================================================================


def _dispatch_bajo_lock(
    operation_id: str,
    *,
    durable_plan: DurableAuthorizedPlan | None,
    journal_previo: ProtectionJournalClassification,
    journal_kernel: JournalDurabilityKernel | None,
    programdata_resolver: Callable[[], object] | None,
    port: NodeRecoveryPort | None,
) -> _Resultado:
    """Pasada decisoria, ya con exclusividad: re-lee toda la evidencia fresca."""
    journal_ahora = classify_protection_journal(
        operation_id, programdata_resolver=programdata_resolver, kernel=journal_kernel
    )

    if durable_plan is None:
        # C4c: la identidad física se resolvió desde el journal, pero el plan
        # autoritativo no es demostrablemente recuperable: NO hay rollback
        # automático (sin PRE autoritativo), NO hay staging fallback.
        detalle = journal_ahora.detail or "plan autoritativo ausente/corrupto"
        return _Resultado(
            disposition=RecoveryDisposition.INDETERMINATE,
            retener_lock=True,
            operator=True,
            reason=(f"authorized_plan.json no es utilizable como fuente del PRE: {detalle}"),
            detail=(
                "sin PRE autoritativo no hay rollback automático: intervención del operador requerida "
                "(C4c); el lock queda retenido para inspección"
            ),
        )

    # Re-load del plan bajo exclusividad: si desapareció o mutó entre la
    # clasificación y el lock, la evidencia es contradictoria.
    try:
        plan_fresco = load_durable_authorized_plan(operation_id, programdata_resolver=programdata_resolver)
    except Exception as exc:  # noqa: BLE001 — boundary deliberada
        return _Resultado(
            disposition=RecoveryDisposition.INDETERMINATE,
            retener_lock=True,
            operator=True,
            reason=f"el plan autoritativo dejó de ser cargable bajo lock: {exc}",
            detail="evidencia contradictoria entre clasificación y exclusividad",
        )

    # El plan con el que se DERIVÓ la identidad física del lock debe ser el
    # mismo que el que decide el recovery: una sustitución entre clasificación
    # y exclusividad dejaría al lock ligado a un Golden y al dispatch actuando
    # sobre otro. La ligadura journal<->plan cubre el caso del journal VALID,
    # pero esta comparación cierra también la ventana del journal ausente.
    if (
        plan_fresco.digest != durable_plan.digest
        or plan_fresco.physical_identity != durable_plan.physical_identity
        or plan_fresco.canonical_root != durable_plan.canonical_root
    ):
        return _Resultado(
            disposition=RecoveryDisposition.INDETERMINATE,
            retener_lock=True,
            operator=True,
            reason="el plan autoritativo cambió entre la clasificación y la exclusividad",
            detail="sustitución de evidencia dura: fail-closed con lock retenido para inspección",
        )

    if journal_ahora.classification is ProtectionJournalClassification.ABSENT:
        if journal_previo in (
            ProtectionJournalClassification.VALID,
            ProtectionJournalClassification.INDETERMINATE,
        ):
            return _Resultado(
                disposition=RecoveryDisposition.INDETERMINATE,
                retener_lock=True,
                operator=True,
                reason="el journal durable desapareció entre la clasificación y la exclusividad",
                detail="evidencia transaccional destruida de forma anómala: fail-closed",
            )
        return _Resultado(
            disposition=RecoveryDisposition.NO_TRANSACTION,
            retener_lock=False,
            operator=False,
            detail="plan durable sin journal: la operación nunca entró a la fase lock-gated (cero mutaciones posibles)",
        )

    if journal_ahora.classification is ProtectionJournalClassification.INDETERMINATE:
        # C4b: escritura cortada / evidencia ambigua. No se interpreta como
        # "operación no ocurrió" ni como éxito.
        return _Resultado(
            disposition=RecoveryDisposition.INDETERMINATE,
            retener_lock=True,
            operator=True,
            reason=journal_ahora.detail,
            detail=(
                "journal con evidencia ambigua (torn tail o esquema inválido): cero escrituras sobre el "
                "Golden; el lock queda retenido para inspección (§20 C4b)"
            ),
        )

    # Journal VALID: abrir y ligar contra el plan autoritativo.
    try:
        journal = open_protection_journal(
            operation_id,
            plan_fresco,
            programdata_resolver=programdata_resolver,
            kernel=journal_kernel,
        )
    except Exception as exc:  # noqa: BLE001 — boundary deliberada
        return _Resultado(
            disposition=RecoveryDisposition.INDETERMINATE,
            retener_lock=True,
            operator=True,
            reason=f"el journal no pudo abrirse/ligarse contra el plan autoritativo: {exc}",
            detail="evidencia transaccional inconsistente con el plan: fail-closed",
        )
    try:
        return _dispatch_fsm(plan_fresco, journal, port)
    finally:
        journal.close()


# ============================================================================
# API productiva
# ============================================================================


def recover_interrupted_protection(
    *,
    operation_id: str,
    programdata_resolver: Callable[[], object] | None = None,
    journal_kernel: JournalDurabilityKernel | None = None,
    lock_kernel: GoldenLockKernel | None = None,
    port: NodeRecoveryPort | None = None,
) -> RecoveryForensicReport:
    """Recupera una operación de protección interrumpida usando SOLO autoridad durable.

    Entrada: ``operation_id`` a secas. Ni root, ni paths, ni bytes de SD, ni
    ``--file``/``--journal-path`` salen del caller: la raíz canónica, los paths
    de los nodos, el PRE y la identidad física se derivan de
    ``authorized_plan.json`` / el journal protegido.

    Secuencia (§9 del encargo):

        1. clasificar evidencia durable (plan + journal), sin tocar nada;
        2. re-adquirir exclusividad: ``GoldenMutationLock`` con takeover de
           huérfano sólo para el MISMO operation_id (dueño vivo => LOCK_BUSY);
        3. bajo lock, re-leer y ligar la evidencia fresca;
        4. decidir por el FSM autoritativo: rollback idempotente, handoff
           S4-D o INDETERMINATE (lock retenido, operador alertado).
    """
    plan_classification = classify_durable_authorized_plan(operation_id, programdata_resolver=programdata_resolver)
    journal_result = classify_protection_journal(
        operation_id, programdata_resolver=programdata_resolver, kernel=journal_kernel
    )
    journal_previo = journal_result.classification

    durable_plan: DurableAuthorizedPlan | None = None
    identity: tuple[int, int] | None = None
    fuente = RecoveryIdentitySource.NONE

    if plan_classification is DurableWriteOutcome.DURABLE:
        try:
            durable_plan = load_durable_authorized_plan(operation_id, programdata_resolver=programdata_resolver)
            identity = (durable_plan.volume_serial_number, durable_plan.root_file_id)
            fuente = RecoveryIdentitySource.AUTHORIZED_PLAN
        except Exception:  # noqa: BLE001 — carrera entre clasificar y cargar
            plan_classification = DurableWriteOutcome.INDETERMINATE
            durable_plan = None

    journal_valido: ProtectionJournal | None = journal_result.journal if journal_result.is_valid else None
    if identity is None and journal_valido is not None:
        identity = (
            journal_valido.physical_root.volume_serial_number,
            journal_valido.physical_root.root_file_id,
        )
        fuente = RecoveryIdentitySource.PROTECTION_JOURNAL

    resumen: tuple[NodeWalSummary, ...] = ()
    if journal_valido is not None:
        resumen = (
            _resumen_wal(journal_valido, durable_plan)
            if durable_plan is not None
            else tuple(
                NodeWalSummary(relative_path=registro.relative_path, wal_state=registro.state.value)
                for registro in journal_valido.node_records
            )
        )

    estado_observado = journal_valido.transaction_state if journal_valido is not None else None

    def _base(**overrides: Any) -> dict[str, Any]:
        campos: dict[str, Any] = {
            "operation_id": operation_id,
            "plan_classification": plan_classification,
            "journal_classification": journal_previo,
            "state": estado_observado,
            "resumen": resumen,
            "fuente": fuente,
        }
        campos.update(overrides)
        return campos

    # --- Fase sin identidad: no hay ni cómo adquirir el lock ---
    if identity is None:
        if plan_classification is DurableWriteOutcome.NOT_DURABLE and (
            journal_previo is ProtectionJournalClassification.ABSENT
        ):
            return _reporte(
                **_base(),
                lock_outcome=RecoveryLockOutcome.NOT_ATTEMPTED,
                stale_takeover=False,
                resultado=_Resultado(
                    disposition=RecoveryDisposition.NO_TRANSACTION,
                    retener_lock=False,
                    operator=False,
                    detail="sin plan ni journal: no existe evidencia durable de transacción alguna",
                ),
            )
        return _reporte(
            **_base(),
            lock_outcome=RecoveryLockOutcome.NOT_ATTEMPTED,
            stale_takeover=False,
            resultado=_Resultado(
                disposition=RecoveryDisposition.INDETERMINATE,
                retener_lock=False,
                operator=True,
                reason=(
                    "evidencia durable insuficiente para resolver la identidad física del Golden "
                    f"(plan={plan_classification.value}, journal={journal_previo.value})"
                ),
                detail="no se pudo siquiera derivar el lock: intervención del operador requerida",
            ),
        )

    # --- Fase con identidad: exclusividad ANTES de tocar ACLs ---
    serial, file_id = identity
    stale_takeover = False
    try:
        adquisicion: RecoveryLockAcquisition = acquire_golden_mutation_lock_for_recovery(
            serial,
            file_id,
            operation_id,
            kernel=lock_kernel,
            programdata_resolver=programdata_resolver,
        )
    except GoldenLockBusyError as exc:
        return _reporte(
            **_base(),
            lock_outcome=RecoveryLockOutcome.BUSY,
            stale_takeover=False,
            resultado=_Resultado(
                disposition=RecoveryDisposition.LOCK_BUSY,
                retener_lock=False,
                operator=False,
                detail=f"otro participante vivo posee el GoldenMutationLock: {exc}",
            ),
        )
    except GoldenLockOrphanedOperationMismatchError as exc:
        return _reporte(
            **_base(),
            lock_outcome=RecoveryLockOutcome.REFUSED,
            stale_takeover=False,
            resultado=_Resultado(
                disposition=RecoveryDisposition.INDETERMINATE,
                retener_lock=False,
                operator=True,
                reason=str(exc),
                detail="el lock huérfano pertenece a otra operación: primero debe recuperarse esa operación",
            ),
        )
    except GoldenLockError as exc:
        return _reporte(
            **_base(),
            lock_outcome=RecoveryLockOutcome.REFUSED,
            stale_takeover=False,
            resultado=_Resultado(
                disposition=RecoveryDisposition.INDETERMINATE,
                retener_lock=False,
                operator=True,
                reason=f"el lock no pudo adquirirse con evidencia válida: {exc}",
                detail="metadata de lock ilegible/contradictoria: fail-closed sin robo",
            ),
        )

    stale_takeover = adquisicion.preexisting_disposition is PreexistingLockDisposition.ORPHANED_LOCK
    handle: GoldenMutationLockHandle = adquisicion.handle

    resultado = _Resultado(
        disposition=RecoveryDisposition.INDETERMINATE,
        retener_lock=True,
        operator=True,
        reason="excepción inesperada durante la recuperación",
        detail="fail-closed",
    )
    try:
        resultado = _dispatch_bajo_lock(
            operation_id,
            durable_plan=durable_plan,
            journal_previo=journal_previo,
            journal_kernel=journal_kernel,
            programdata_resolver=programdata_resolver,
            port=port,
        )
    except Exception as exc:  # noqa: BLE001 — boundary superior: jamás se pierde el lock por una excepción no tipada
        resultado = _Resultado(
            disposition=RecoveryDisposition.INDETERMINATE,
            retener_lock=True,
            operator=True,
            reason=f"excepción inesperada durante la recuperación: {exc!r}",
            detail="fail-closed: el lock queda retenido para inspección",
        )
    finally:
        lock_outcome = RecoveryLockOutcome.ACQUIRED_RELEASED
        try:
            if resultado.retener_lock:
                handle.retain_for_inspection()
                lock_outcome = RecoveryLockOutcome.ACQUIRED_RETAINED
            else:
                handle.release()
        except GoldenLockError as exc:
            # El lock ya se cerró (release cierra en finally aunque el flush
            # falle): la metadata pudo quedar no-RELEASED y el próximo
            # adquirente la clasificará huérfano -> recovery idempotente.
            lock_outcome = RecoveryLockOutcome.ACQUIRED_RETAINED
            logger.warning(
                "recovery: liberación del GoldenMutationLock no confirmada (operation_id=%s): %s",
                operation_id,
                exc,
            )

    reporte = _reporte(
        **_base(journal_classification=journal_previo),
        lock_outcome=lock_outcome,
        stale_takeover=stale_takeover,
        resultado=resultado,
    )
    if reporte.disposition is RecoveryDisposition.INDETERMINATE:
        logger.warning(
            "recovery INDETERMINATE (operation_id=%s): %s",
            operation_id,
            reporte.indeterminate_reason or reporte.detail,
        )
    return reporte


__all__ = [
    "NodeRecoveryPort",
    "NodeWalSummary",
    "RecoveryDisposition",
    "RecoveryForensicReport",
    "RecoveryIdentitySource",
    "RecoveryLockOutcome",
    "RecoveryOrchestratorError",
    "RecoveryUnsupportedError",
    "recover_interrupted_protection",
]
