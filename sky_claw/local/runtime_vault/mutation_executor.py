"""GP2-S4B: apply ACL real atado a handle, con permiso WAL y rollback exacto.

Este módulo es el ORQUESTADOR. No construye Target DACLs, no implementa otro WAL
ni otro lock: compone las primitivas ya auditadas de GP1/S4-A.

    - ``target_dacl``      -> SetSecurityInfo real, verificación POST y restore PRE
    - ``quiescence``       -> probe transitorio con dwShareMode = 0 (§4.1)
    - ``protection_journal_store`` -> MUTATING(K)/MUTATED(K) durables + WalMutationPermit
    - ``golden_mutation_lock`` / ``authorization_context`` -> autoridad viva

Autoridad exigida para mutar (nada de esto es opcional):

    DurableAuthorizedPlan          (el plan autoritativo, acuñado tras revalidación)
    + DurableProtectionJournal     (ligado por digest al plan)
    + PrivilegedBoundarySession abierta
    + GoldenMutationLock vivo, con operation_id e identidad física del plan
    + NodeMutationBinding del nodo K
    + WalMutationPermit no consumido

Orden inquebrantable por nodo K (ADR 0010 §12.2, fase de mutación):

    1  resolver K desde DurableAuthorizedPlan (nunca staging, nunca el filesystem)
    2  probe de quiescencia (handle transitorio, dwShareMode = 0)
    3  cerrar el probe
    4  abrir el handle de mutación (READ_CONTROL|WRITE_DAC|WRITE_OWNER|FILE_READ_ATTRIBUTES)
    5  revalidar VolumeSerialNumber / FileId
    6  revalidar ReparseTag == 0
    7  comprobar NumberOfLinks
    8  leer el PRE SD vivo sobre ESE handle
    9  comparar PRE vivo contra el PRE autorizado por el plan
    10 journal MUTATING(K)
    11 FlushFileBuffers del journal PASS
    12 obtener WalMutationPermit
    13 segunda comprobación de NumberOfLinks
    14 validar/bindear el permit
    15 consumir el permit
    16 SetSecurityInfo sobre EL MISMO handle
    17 leer el POST sobre EL MISMO handle
    18 verificar la Target DACL
    19 journal MUTATED(K)
    20 FlushFileBuffers del journal PASS
    21 cerrar el handle

Apply BOTTOM-UP (§16.1: descendientes más profundos primero, root último).
Rollback TOP-DOWN (§17.1: root primero, descendientes después), restaurando
exclusivamente el PRE autorizado por ``DurableAuthorizedPlan``.

S4-B NO produce ``COMMITTED``. El éxito significa "todos los nodos aplicados y
todos los ``MUTATED`` durables", no "transacción comprometida": GP1 final, RV-2
final, NodeSet final y ``ARCHIVING_BACKUP`` son S4-D. Tampoco implementa crash
recovery (S4-C): si el proceso muere, el journal queda suficiente para que S4-C
distinga ``MUTATING`` de ``MUTATED``.

Plataforma: la producción es Windows-only. Sin puerto inyectado en otra
plataforma se lanza :class:`MutationExecutorUnsupportedError`; los tests puros
usan puertos falsos deterministas.
"""

from __future__ import annotations

import contextlib
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from sky_claw.local.runtime_vault.authorization_context import PrivilegedBoundarySession
from sky_claw.local.runtime_vault.authorized_plan import AuthorizedPlan
from sky_claw.local.runtime_vault.authorized_plan_store import DurableAuthorizedPlan
from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
from sky_claw.local.runtime_vault.protection_journal_store import (
    DurableProtectionJournal,
    WalMutationPermit,
)

if TYPE_CHECKING:  # pragma: no cover - sólo anotaciones; el ABI vive en target_dacl
    from sky_claw.local.runtime_vault.target_dacl import TargetDaclVerificationResult

# ============================================================================
# Jerarquía de excepciones
# ============================================================================


class MutationExecutorError(RuntimeVaultError):
    """Base de las excepciones del orquestador de apply/rollback (S4-B)."""


class MutationExecutorUnsupportedError(MutationExecutorError):
    """Plataforma sin las garantías Win32 que el apply real requiere."""


class MutationAuthorityError(MutationExecutorError):
    """La autoridad exigida para mutar no está presente o no es coherente."""


class MutationPermitError(MutationAuthorityError):
    """El WalMutationPermit no bindea contra el plan/nodo, o ya fue consumido."""


class MutationApplyError(MutationExecutorError):
    """Fallo en la fase de apply de un nodo (nunca se mutó el siguiente)."""


class MutationRollbackError(MutationExecutorError):
    """Fallo en la fase de rollback: preserva el error de apply como causa."""

    def __init__(self, message: str, *, report: ApplyReport | None = None) -> None:
        super().__init__(message)
        self.report = report


# ============================================================================
# DTOs puros
# ============================================================================


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    """Identidad física observada sobre un handle vivo (§12.2 paso 2)."""

    volume_serial_number: int
    file_id: int
    reparse_tag: int
    number_of_links: int


@dataclass(frozen=True, slots=True)
class NodeMutationOutcome:
    """Desenlace de UN nodo. ``applied`` implica SetSecurityInfo ejecutado."""

    relative_path: str
    node_kind: GoldenProtectionNodeKind
    applied: bool = False
    mutated: bool = False
    rolled_back: bool = False

    @property
    def acl_write_reached(self) -> bool:
        """``SetSecurityInfo`` corrió: el nodo PUEDE tener la ACL cambiada.

        No se afirma que la escritura haya sido confirmada: sólo que la primitiva
        fue invocada, que es exactamente la ambigüedad que el rollback resuelve.
        """
        return self.applied


@dataclass(frozen=True, slots=True)
class ApplyReport:
    """Resultado completo de un apply. Nunca esconde la causa inicial (§35)."""

    operation_id: str
    outcomes: tuple[NodeMutationOutcome, ...] = ()
    transaction_state: ProtectionTransactionState = ProtectionTransactionState.APPLYING
    setsecurityinfo_calls: int = 0
    apply_error: MutationExecutorError | None = None
    rollback_error: MutationExecutorError | None = None

    @property
    def ok(self) -> bool:
        return self.apply_error is None and self.rollback_error is None

    @property
    def applied_nodes(self) -> tuple[str, ...]:
        return tuple(o.relative_path for o in self.outcomes if o.applied)

    @property
    def nodes_with_confirmed_wal(self) -> tuple[str, ...]:
        """Nodos cuyo ``MUTATED(K)`` quedó durable (apply + POST + flush)."""
        return tuple(o.relative_path for o in self.outcomes if o.mutated)

    @property
    def rolled_back_nodes(self) -> tuple[str, ...]:
        return tuple(o.relative_path for o in self.outcomes if o.rolled_back)

    def raise_if_failed(self) -> ApplyReport:
        """Fail-closed para callers que prefieran excepción a inspección."""
        if self.rollback_error is not None:
            raise MutationRollbackError(str(self.rollback_error), report=self) from self.apply_error
        if self.apply_error is not None:
            raise self.apply_error
        return self


# ============================================================================
# Puerto de mutación ACL atada a handle
# ============================================================================


class NodeMutationPort(Protocol):
    """Contrato mínimo de mutación por handle que el orquestador necesita.

    La implementación productiva (:class:`HandleBoundTargetDaclPort`) delega en
    ``target_dacl``, que es la ÚNICA capa auditada que invoca ``SetSecurityInfo``.
    Los tests inyectan puertos falsos deterministas para probar la causalidad
    (orden, gates, consume-once, rollback) sin tocar un Golden real.
    """

    def open(self, path: pathlib.Path, node_kind: GoldenProtectionNodeKind) -> int:
        """Abre el handle de mutación (NO el probe)."""
        ...

    def close(self, handle: int) -> None: ...

    def read_identity(self, handle: int) -> NodeIdentity:
        """(VolumeSerialNumber, FileId, ReparseTag, NumberOfLinks) sobre el handle."""
        ...

    def read_live_pre_sd_sha256(self, handle: int) -> str: ...

    def apply_target_dacl(self, handle: int, node: NodeSecurityBackup) -> None:
        """SetSecurityInfo REAL sobre el handle (§7)."""
        ...

    def verify_target_dacl(self, handle: int, node: NodeSecurityBackup) -> object:
        """Verifica la Target DACL POST sobre el handle (§12.2 paso 6).

        El adaptador productivo devuelve el ``TargetDaclVerificationResult``
        estructurado (evidencia auditable del POST); los puertos de prueba pueden
        devolver ``None``. El orquestador ignora el valor: lo normativo es que la
        verificación EXIJA, y ante cualquier divergencia lance.
        """
        ...

    def restore_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        """SetSecurityInfo de restauración sobre el handle (§12.2 rollback)."""
        ...

    def verify_restored_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None: ...

    @property
    def setsecurityinfo_calls(self) -> int:
        """Contador de llamadas REALES a SetSecurityInfo (oráculo de los tests)."""
        ...


class HandleBoundTargetDaclPort:
    """Adaptador productivo: handle-bound, delegando en las primitivas auditadas."""

    def __init__(self) -> None:
        self._setsecurityinfo_calls = 0

    def open(self, path: pathlib.Path, node_kind: GoldenProtectionNodeKind) -> int:
        from sky_claw.local.runtime_vault import target_dacl

        return target_dacl.open_node_security_handle(path)

    def close(self, handle: int) -> None:
        from sky_claw.local.runtime_vault import target_dacl

        target_dacl.close_security_handle(handle)

    def read_identity(self, handle: int) -> NodeIdentity:
        from sky_claw.local.runtime_vault import target_dacl

        vol_serial, file_id = target_dacl._read_file_id_info_by_handle(handle)
        return NodeIdentity(
            volume_serial_number=vol_serial,
            file_id=file_id,
            reparse_tag=target_dacl._read_reparse_tag_by_handle(handle),
            number_of_links=target_dacl._read_number_of_links_by_handle(handle),
        )

    def read_live_pre_sd_sha256(self, handle: int) -> str:
        from sky_claw.local.runtime_vault import target_dacl

        return target_dacl.read_live_pre_sd_sha256_by_handle(handle)

    def apply_target_dacl(self, handle: int, node: NodeSecurityBackup) -> None:
        from sky_claw.local.runtime_vault import target_dacl

        self._setsecurityinfo_calls += 1
        target_dacl.apply_target_dacl_by_handle(handle, node)

    def verify_target_dacl(self, handle: int, node: NodeSecurityBackup) -> TargetDaclVerificationResult:
        from sky_claw.local.runtime_vault import target_dacl

        return target_dacl.verify_target_dacl_by_handle(handle, node)

    def restore_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        from sky_claw.local.runtime_vault import target_dacl

        self._setsecurityinfo_calls += 1
        target_dacl.restore_security_descriptor_by_handle(handle, node)

    def verify_restored_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        from sky_claw.local.runtime_vault import target_dacl

        target_dacl.verify_restored_security_descriptor_by_handle(handle, node)

    @property
    def setsecurityinfo_calls(self) -> int:
        return self._setsecurityinfo_calls


# ============================================================================
# Orden determinista (§16 apply bottom-up, §17 rollback top-down)
# ============================================================================


def node_depth(relative_path: str) -> int:
    """Profundidad del nodo: la raíz ``.`` es 0, un hijo directo 1, ``a/b/c`` es 3.

    La cuenta es de SEGMENTOS, no de separadores: ``Data`` y ``Data/Scripts`` no
    comparten profundidad, y sin esa distinción el apply bottom-up no podría
    garantizar que un descendiente se mute antes que su ascendiente (§16.1).
    """
    if relative_path == ".":
        return 0
    return relative_path.count("/") + 1


#: Estados que NUNCA puede reportar un apply cuyo rollback falló. Afirmar
#: ``APPLYING``/``ROLLBACK_REQUIRED``/``ROLLING_BACK``/``ROLLED_BACK`` tras un
#: fallo de restauración invitaría a S4-C a reintentar el apply sobre un Golden
#: potencialmente mutado; §20 sólo admite ``ROLLBACK_FAILED`` o ``INDETERMINATE``.
_ESTADOS_PROHIBIDOS_TRAS_FALLO_DE_ROLLBACK = frozenset(
    {
        ProtectionTransactionState.APPLYING,
        ProtectionTransactionState.ROLLBACK_REQUIRED,
        ProtectionTransactionState.ROLLING_BACK,
        ProtectionTransactionState.ROLLED_BACK,
    }
)


def _nodes_of(plan: AuthorizedPlan | DurableAuthorizedPlan) -> tuple[NodeSecurityBackup, ...]:
    """Nodos autorizados, tanto desde el plan puro como desde el plan durable."""
    if isinstance(plan, DurableAuthorizedPlan):
        return tuple(plan.plan.nodes)
    return tuple(plan.nodes)


def apply_order(plan: AuthorizedPlan | DurableAuthorizedPlan) -> tuple[NodeSecurityBackup, ...]:
    """BOTTOM-UP: descendientes más profundos primero, root último (§16.1).

    Determinista y derivado de ``relative_path``/profundidad: NO depende del orden
    del JSON ni de la construcción del modelo.
    """
    return tuple(sorted(_nodes_of(plan), key=lambda n: (-node_depth(n.relative_path), n.relative_path)))


def rollback_order(
    plan: AuthorizedPlan | DurableAuthorizedPlan,
    mutated_relative_paths: Sequence[str],
) -> tuple[NodeSecurityBackup, ...]:
    """TOP-DOWN: root primero, descendientes después (§17.1).

    Sólo incluye los nodos que realmente se mutaron (``SetSecurityInfo`` corrió):
    un nodo cuya intención quedó en ``MUTATING`` sin mutación física no necesita
    restauración, pero SÍ conserva su ambigüedad para S4-C.
    """
    mutados = set(mutated_relative_paths)
    candidatos = [n for n in _nodes_of(plan) if n.relative_path in mutados]
    return tuple(sorted(candidatos, key=lambda n: (node_depth(n.relative_path), n.relative_path)))


# ============================================================================
# Derivación de rutas (autoridad del plan, nunca del caller)
# ============================================================================


def derive_node_path(plan: DurableAuthorizedPlan, relative_path: str) -> pathlib.Path:
    """``canonical_root`` + ``relative_path`` autorizado. Nunca un path del caller."""
    root = pathlib.Path(plan.canonical_root)
    if relative_path == ".":
        return root
    return root.joinpath(*relative_path.split("/"))


# ============================================================================
# Gates de autoridad (§7, §9)
# ============================================================================


def _require_authority(
    *,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    session: PrivilegedBoundarySession,
) -> None:
    """Exige autoridad suficiente ANTES de cualquier mutación.

    Un ``DurableAuthorizedPlan`` aislado NO es permiso de mutación. Un
    ``DurableProtectionJournal`` aislado tampoco.
    """
    if not isinstance(plan, DurableAuthorizedPlan):
        raise MutationAuthorityError("apply exige un DurableAuthorizedPlan acuñado desde disco protegido")
    if not isinstance(journal, DurableProtectionJournal):
        raise MutationAuthorityError("apply exige un DurableProtectionJournal vivo y ligado al plan")
    if not isinstance(session, PrivilegedBoundarySession):
        raise MutationAuthorityError("apply exige una PrivilegedBoundarySession viva")
    if session.closed:
        raise MutationAuthorityError("La sesión de frontera está cerrada: no se puede mutar sin el lock vivo")

    lock = session.lock  # lanza AuthorizationSessionError si la sesión se cerró
    if lock.closed:
        raise MutationAuthorityError("El GoldenMutationLock ya fue liberado: REFUSE_TO_APPLY")

    identity = lock.identity
    if identity.operation_id != plan.operation_id or journal.operation_id != plan.operation_id:
        raise MutationAuthorityError(
            "operation_id divergente entre sesión, plan y journal: "
            f"lock={identity.operation_id}, plan={plan.operation_id}, journal={journal.operation_id}"
        )
    if identity.volume_serial_number != plan.volume_serial_number or identity.root_file_id != plan.root_file_id:
        raise MutationAuthorityError(
            "La identidad física del GoldenMutationLock no corresponde al Golden del plan: REFUSE_TO_APPLY"
        )
    if journal.authorized_plan_digest != plan.digest:
        raise MutationAuthorityError(
            "El journal no liga contra el digest del plan autoritativo: "
            f"journal={journal.authorized_plan_digest}, plan={plan.digest}"
        )


def _require_applying_state(journal: DurableProtectionJournal) -> None:
    """Apply sólo mientras el FSM esté en ``APPLYING`` (§10)."""
    estado = journal.transaction_state
    if estado is not ProtectionTransactionState.APPLYING:
        raise MutationAuthorityError(
            f"El FSM transaccional está en '{estado.value}': apply sólo se admite desde 'applying'"
        )


def _require_identity(node: NodeSecurityBackup, observed: NodeIdentity, *, phase: str) -> None:
    """Revalidación de identidad física: mismatch => CERO SetSecurityInfo (§15)."""
    if observed.volume_serial_number != node.volume_serial_number:
        raise MutationApplyError(
            f"Drift de VolumeSerialNumber en {phase} de '{node.relative_path}': "
            f"esperado={node.volume_serial_number}, observado={observed.volume_serial_number}"
        )
    if observed.file_id != node.file_id:
        raise MutationApplyError(
            f"Drift de FileId en {phase} de '{node.relative_path}': "
            f"esperado={node.file_id}, observado={observed.file_id}"
        )
    if observed.reparse_tag != 0:
        raise MutationApplyError(
            f"'{node.relative_path}' es un reparse point (ReparseTag=0x{observed.reparse_tag:08X}): "
            "mutación rehusada fail-closed"
        )


def _require_single_link(node: NodeSecurityBackup, observed: NodeIdentity, *, phase: str) -> None:
    """Anti-hardlink: un segundo enlace externo bypassearía el confinamiento del root."""
    if node.node_kind is GoldenProtectionNodeKind.FILE and observed.number_of_links != 1:
        raise MutationApplyError(
            f"Hardlink externo detectado en {phase} de '{node.relative_path}': "
            f"NumberOfLinks={observed.number_of_links} != 1"
        )


def _require_live_pre_sd(node: NodeSecurityBackup, observed_sha256: str) -> None:
    """El PRE vivo debe coincidir con el PRE autorizado por el plan (§16)."""
    if observed_sha256 != node.pre_sd_sha256:
        raise MutationApplyError(
            f"Drift de PRE SD en '{node.relative_path}': autorizado={node.pre_sd_sha256}, vivo={observed_sha256}"
        )


def _require_permit(permit: WalMutationPermit, plan: DurableAuthorizedPlan, node: NodeSecurityBackup) -> None:
    """Binding fuerte del permiso (§18). Cualquier divergencia => 0 mutaciones."""
    if not isinstance(permit, WalMutationPermit):
        raise MutationPermitError("apply exige un WalMutationPermit acuñado por el journal")
    if permit.is_consumed:
        raise MutationPermitError(
            f"El permiso para '{node.relative_path}' ya fue consumido: un permiso autoriza UNA mutación"
        )
    if permit.operation_id != plan.operation_id:
        raise MutationPermitError(f"Permiso de otra operación: permiso={permit.operation_id}, plan={plan.operation_id}")
    if permit.authorized_plan_digest != plan.digest:
        raise MutationPermitError(f"Permiso de otro plan: permiso={permit.authorized_plan_digest}, plan={plan.digest}")
    binding = permit.node_binding
    if (
        binding.relative_path != node.relative_path
        or binding.volume_serial_number != node.volume_serial_number
        or binding.file_id != node.file_id
        or binding.pre_sd_sha256 != node.pre_sd_sha256
    ):
        raise MutationPermitError(
            f"Permiso de otro nodo: permiso={binding.relative_path}, exigido={node.relative_path}"
        )


# ============================================================================
# Aplicación por nodo
# ============================================================================


def _resolve_port(port: NodeMutationPort | None) -> NodeMutationPort:
    """Sin puerto inyectado, la durabilidad la aporta el backend Win32 real."""
    if port is not None:
        return port
    if sys.platform != "win32":
        raise MutationExecutorUnsupportedError(
            "El apply ACL real requiere las garantías Win32 del namespace (SetSecurityInfo por handle)"
        )
    return HandleBoundTargetDaclPort()


@dataclass(slots=True)
class _NodeResult:
    outcome: NodeMutationOutcome
    error: MutationExecutorError | None = None


def _apply_node(
    *,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    port: NodeMutationPort,
    node: NodeSecurityBackup,
    probe: Any,
) -> _NodeResult:
    """Ejecuta la secuencia normativa completa de UN nodo (§12.2).

    ``probe`` es el callable de quiescencia inyectable (``probe_node_quiescence``
    en producción). Devuelve el desenlace; NUNCA propaga: el caller decide entre
    ``REFUSE_TO_APPLY`` (0 nodos mutados) y ``ROLLBACK_REQUIRED`` (>0).
    """
    relative_path = node.relative_path
    outcome = NodeMutationOutcome(relative_path=relative_path, node_kind=node.node_kind)
    path = derive_node_path(plan, relative_path)
    handle = 0

    try:
        # 2-3. Probe de quiescencia: handle TRANSITORIO distinto del de mutación.
        probe(path, node.node_kind)

        # 4. Handle de mutación.
        handle = port.open(path, node.node_kind)
    except MutationExecutorError as exc:
        return _NodeResult(outcome, exc)
    except Exception as exc:  # noqa: BLE001 — boundary deliberada: se clasifica y se bifurca por nodos mutados
        return _NodeResult(outcome, MutationApplyError(f"Fallo de apertura/probe en '{relative_path}': {exc}"))

    try:
        # 5-7. Identidad física + reparse + hardlinks.
        observed = port.read_identity(handle)
        _require_identity(node, observed, phase="pre-WAL")
        _require_single_link(node, observed, phase="pre-WAL")

        # 8-9. PRE SD vivo contra el PRE autorizado por el plan.
        _require_live_pre_sd(node, port.read_live_pre_sd_sha256(handle))

        # 10-12. WAL: MUTATING(K) + flush => permiso.
        permit = journal.record_node_mutation_intent(journal.node_binding(relative_path))

        # 13. Segunda comprobación anti-hardlink tras el WAL (ventana TOCTOU).
        _require_single_link(node, port.read_identity(handle), phase="post-WAL")

        # 14-15. Binding fuerte + consume-once.
        _require_permit(permit, plan, node)
        permit.mark_consumed()

        # 16. SetSecurityInfo REAL sobre EL MISMO handle.
        port.apply_target_dacl(handle, node)
        outcome = NodeMutationOutcome(relative_path=relative_path, node_kind=node.node_kind, applied=True)

        # 17-18. POST leído y verificado sobre EL MISMO handle.
        port.verify_target_dacl(handle, node)

        # 19-20. MUTATED(K) durable.
        journal.record_node_mutation_completed(journal.node_binding(relative_path))
        outcome = NodeMutationOutcome(relative_path=relative_path, node_kind=node.node_kind, applied=True, mutated=True)
        return _NodeResult(outcome)
    except MutationExecutorError as exc:
        return _NodeResult(outcome, exc)
    except Exception as exc:  # noqa: BLE001 — boundary deliberada: el desenlace se clasifica, no se adivina
        return _NodeResult(outcome, MutationApplyError(f"Fallo de apply en '{relative_path}': {exc}"))
    finally:
        # 21. Cerrar el handle exactamente una vez. Si la apertura falló no hay
        # nada que cerrar: ``handle`` quedó en 0 y el puerto no registró handles.
        if handle:
            with contextlib.suppress(Exception):  # el cierre no debe enmascarar el desenlace del nodo
                port.close(handle)


# ============================================================================
# Rollback in-process (§17, §12.2 rollback)
# ============================================================================


def _restore_node(
    *,
    plan: DurableAuthorizedPlan,
    port: NodeMutationPort,
    node: NodeSecurityBackup,
) -> None:
    """Restaura el PRE autorizado de UN nodo y verifica la restauración."""
    path = derive_node_path(plan, node.relative_path)
    handle = 0
    try:
        handle = port.open(path, node.node_kind)
        observed = port.read_identity(handle)
        if observed.volume_serial_number != node.volume_serial_number or observed.file_id != node.file_id:
            raise MutationRollbackError(
                f"Identidad física cambiada en '{node.relative_path}' durante rollback: "
                "no se restaura otro objeto por path"
            )
        if observed.reparse_tag != 0:
            raise MutationRollbackError(
                f"'{node.relative_path}' se convirtió en reparse point durante rollback: restauración rehusada"
            )
        port.restore_pre_sd(handle, node)
        port.verify_restored_pre_sd(handle, node)
    finally:
        if handle:
            with contextlib.suppress(Exception):  # el cierre no debe enmascarar el fallo de restauración
                port.close(handle)


def _intentar_transicion(
    journal: DurableProtectionJournal,
    destino: ProtectionTransactionState,
) -> ProtectionTransactionState:
    """Transiciona el FSM si el journal aún puede anexar; si no, devuelve su estado.

    Un journal envenenado por un fallo de durabilidad previo ya no puede anexar:
    lanzar desde acá propagaría una excepción de store por encima del desenlace
    real del apply. La restauración física sigue siendo obligatoria y el FSM se
    queda en el estado honesto que el journal reporta (``INDETERMINATE``), que es
    exactamente lo que S4-C necesita ver.
    """
    # El FSM es best-effort; la restauración física NO lo es.
    with contextlib.suppress(Exception):
        journal.transition_to(destino)
    return journal.transaction_state


def _rollback(
    *,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    port: NodeMutationPort,
    outcomes: Sequence[NodeMutationOutcome],
) -> tuple[tuple[NodeMutationOutcome, ...], MutationExecutorError | None]:
    """Ejecuta el rollback TOP-DOWN. Devuelve outcomes actualizados y error (o None).

    El error de apply NO se toca acá: quien llama lo preserva tal cual (§35), de
    modo que si el rollback también falla se conserven las DOS causas.
    """
    mutados = [o for o in outcomes if o.applied]
    orden = rollback_order(plan, [o.relative_path for o in mutados])

    _intentar_transicion(journal, ProtectionTransactionState.ROLLING_BACK)

    restaurados: dict[str, bool] = {}
    for node in orden:
        try:
            _restore_node(plan=plan, port=port, node=node)
        except MutationExecutorError as exc:
            return _marcar(outcomes, restaurados), exc
        except Exception as exc:  # noqa: BLE001 — boundary deliberada
            return (
                _marcar(outcomes, restaurados),
                MutationRollbackError(f"Fallo restaurando '{node.relative_path}': {exc}"),
            )
        restaurados[node.relative_path] = True

    _intentar_transicion(journal, ProtectionTransactionState.ROLLED_BACK)
    return _marcar(outcomes, restaurados), None


def _marcar(
    outcomes: Sequence[NodeMutationOutcome],
    restaurados: dict[str, bool],
) -> tuple[NodeMutationOutcome, ...]:
    return tuple(
        NodeMutationOutcome(
            relative_path=o.relative_path,
            node_kind=o.node_kind,
            applied=o.applied,
            mutated=o.mutated,
            rolled_back=restaurados.get(o.relative_path, False),
        )
        for o in outcomes
    )


# ============================================================================
# API productiva
# ============================================================================


def apply_authorized_plan(
    *,
    plan: DurableAuthorizedPlan,
    journal: DurableProtectionJournal,
    session: PrivilegedBoundarySession,
    port: NodeMutationPort | None = None,
    probe: Any = None,
) -> ApplyReport:
    """Aplica la Target DACL autorizada a todos los nodos del plan, bottom-up.

    Ante cualquier fallo con >0 nodos mutados dispara el rollback exacto TOP-DOWN
    desde ``DurableAuthorizedPlan``. Con 0 nodos mutados el desenlace es
    ``REFUSE_TO_APPLY`` (rollback no-op: ``ROLLED_BACK`` con lista vacía).

    Nunca produce ``COMMITTED``. El reporte preserva ``apply_error`` y
    ``rollback_error`` por separado (§35): si el rollback también falla, se lanza
    :class:`MutationRollbackError` con el error de apply como ``__cause__``.
    """
    _require_authority(plan=plan, journal=journal, session=session)
    _require_applying_state(journal)
    active_port = _resolve_port(port)
    active_probe = probe if probe is not None else _default_probe

    outcomes: list[NodeMutationOutcome] = []
    apply_error: MutationExecutorError | None = None

    for node in apply_order(plan):
        resultado = _apply_node(plan=plan, journal=journal, port=active_port, node=node, probe=active_probe)
        outcomes.append(resultado.outcome)
        if resultado.error is not None:
            apply_error = resultado.error
            break

    if apply_error is None:
        return ApplyReport(
            operation_id=plan.operation_id,
            outcomes=tuple(outcomes),
            transaction_state=journal.transaction_state,
            setsecurityinfo_calls=active_port.setsecurityinfo_calls,
        )

    # >0 nodos mutados => ROLLBACK_REQUIRED. Con 0 nodos mutados el rollback es
    # un no-op declarado (ruta C3 de §20), que es exactamente REFUSE_TO_APPLY.
    _intentar_transicion(journal, ProtectionTransactionState.ROLLBACK_REQUIRED)

    finales, rollback_error = _rollback(
        plan=plan,
        journal=journal,
        port=active_port,
        outcomes=outcomes,
    )
    estado = journal.transaction_state
    if rollback_error is not None:
        if estado is ProtectionTransactionState.ROLLING_BACK:
            estado = _intentar_transicion(journal, ProtectionTransactionState.ROLLBACK_FAILED)
        if estado in _ESTADOS_PROHIBIDOS_TRAS_FALLO_DE_ROLLBACK:
            # El journal no pudo anexar (envenenado por un fallo de durabilidad
            # previo) y por tanto sigue reportando un estado de apply en curso.
            # El reporte NO puede afirmar un estado que invite a reintentar el
            # apply: §20 exige ROLLBACK_FAILED o INDETERMINATE.
            estado = ProtectionTransactionState.ROLLBACK_FAILED

    reporte = ApplyReport(
        operation_id=plan.operation_id,
        outcomes=finales,
        transaction_state=estado,
        setsecurityinfo_calls=active_port.setsecurityinfo_calls,
        apply_error=apply_error,
        rollback_error=rollback_error,
    )
    if rollback_error is not None:
        raise MutationRollbackError(str(rollback_error), report=reporte) from apply_error
    return reporte


def _default_probe(path: pathlib.Path, node_kind: GoldenProtectionNodeKind) -> None:
    from sky_claw.local.runtime_vault.quiescence import probe_node_quiescence

    probe_node_quiescence(path, node_kind)


__all__ = [
    "ApplyReport",
    "HandleBoundTargetDaclPort",
    "MutationApplyError",
    "MutationAuthorityError",
    "MutationExecutorError",
    "MutationExecutorUnsupportedError",
    "MutationPermitError",
    "MutationRollbackError",
    "NodeIdentity",
    "NodeMutationOutcome",
    "NodeMutationPort",
    "apply_authorized_plan",
    "apply_order",
    "derive_node_path",
    "node_depth",
    "rollback_order",
]
