"""Orquestación read-only / pre-mutación de planificación Golden Protection (GP2-S2).

SECURITY RULE (READ-ONLY PRE-MUTATION ORCHESTRATION):
- READ_ONLY_ORCHESTRATION != AUTHORIZATION
- PREPARED != AUTHORIZED, PREPARED != MUTATING, PREPARED != HARDENED
- S2 no realiza ninguna mutación ACL real ni invoca primitivas mutadoras
  (apply_target_dacl_by_handle, restore_security_descriptor_by_handle, SetSecurityInfo,
   ShellExecuteExW, runas).
- Encadenamiento estricto de gates frescos:
  RV-2 (verify_golden_master)
  ↓
  GP1 (inspect_golden_protection)
  ↓
  native node evidence (probe_node_evidence)
  ↓
  quiescence probe (probe_tree_quiescence)
  ↓
  prepare_golden_protection_plan
  ↓
  seal_golden_protection_plan
  ↓
  resultado PREPARED / refusal tipado

Reutiliza GoldenProtectionPlanState de golden_protection_plan.py para el early FSM
y define GP2PlanningDisposition para desacoplar desenlaces de estados de ciclo de vida.
"""

from __future__ import annotations

import os
import pathlib
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sky_claw.app.security.links import link_kind_and_identity_or_raise
from sky_claw.local.runtime_vault.golden import verify_golden_master
from sky_claw.local.runtime_vault.golden_protection_plan import (
    DuplicateFileIdError,
    GoldenProtectionNodeKind,
    GoldenProtectionPlanState,
    GoldenProtectionSealChecks,
    PlanCreationError,
    SealedGoldenProtectionPlan,
    prepare_golden_protection_plan,
    seal_golden_protection_plan,
)
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    GoldenMasterVerificationResult,
    InventoryLinkError,
    RuntimeIdentity,
    TreeDigest,
    VerificationState,
)
from sky_claw.local.runtime_vault.node_evidence import (
    NativeEvidenceError,
    NativeNodeEvidence,
    probe_node_evidence,
)
from sky_claw.local.runtime_vault.protection import (
    GoldenProtectionResult,
    GoldenProtectionRight,
    GoldenProtectionState,
    inspect_golden_protection,
)
from sky_claw.local.runtime_vault.quiescence import (
    DEFAULT_BASE_BACKOFF_SECONDS,
    MAX_PROBE_RETRIES,
    QuiescenceError,
    QuiescenceViolationError,
    default_quiescence_jitter,
    probe_tree_quiescence,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


# ============================================================================
# Modelo de Desenlace de Planificación y Resultado GP2
# ============================================================================


class GP2PlanningDisposition(StrEnum):
    """Desenlaces posibles de la orquestación de planificación GP2-S2.

    Distingue claramente entre el estado del ciclo de vida FSM
    (GoldenProtectionPlanState.PREPARED) y los desenlaces no mutadores.
    """

    PREPARED = "prepared"
    ALREADY_HARDENED = "already_hardened"
    REFUSE_TO_APPLY = "refuse_to_apply"
    REFUSE_TO_PLAN = "refuse_to_plan"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GP2Result:
    """Resultado estructurado de la orquestación GP2."""

    disposition: GP2PlanningDisposition
    success: bool
    message: str = ""
    sealed_plan: SealedGoldenProtectionPlan | None = None
    golden_verification: GoldenMasterVerificationResult | None = None
    protection_result: GoldenProtectionResult | None = None

    def __post_init__(self) -> None:
        # F1 & F5: semántica explícita de success
        if self.disposition in (GP2PlanningDisposition.PREPARED, GP2PlanningDisposition.ALREADY_HARDENED):
            if not self.success:
                raise ValueError(f"Desenlace {self.disposition.value} exige success=True")
            if self.message != "":
                raise ValueError(f"Desenlace de éxito {self.disposition.value} exige message=''")
        else:
            if self.success:
                raise ValueError(f"Desenlace no exitoso {self.disposition.value} exige success=False")
            if not self.message:
                raise ValueError(f"Desenlace de rechazo/fallo {self.disposition.value} exige message no vacío")

        # F2: PREPARED exige estrictamente sealed_plan
        if self.disposition is GP2PlanningDisposition.PREPARED:
            if self.sealed_plan is None:
                raise ValueError("Desenlace PREPARED exige sealed_plan")
            if self.sealed_plan.plan.state is not GoldenProtectionPlanState.PREPARED:
                raise ValueError("El plan sellado en PREPARED debe tener estado PREPARED")

        # F3: Desenlaces no-PREPARED no pueden contener sealed_plan
        if self.disposition is not GP2PlanningDisposition.PREPARED and self.sealed_plan is not None:
            raise ValueError("Desenlaces de rechazo o no-op no pueden contener un sealed_plan")


# ============================================================================
# Helpers Internos de Verificación Estructural
# ============================================================================


def _check_post_inventory_structural_match(
    root_path: pathlib.Path,
    native_nodes: Sequence[NativeNodeEvidence],
) -> bool:
    """Re-verifica exhaustivamente que el árbol físico coincida con la evidencia capturada.

    Detecta drift entre la captura inicial de evidencia nativa y el momento previo
    al sellado (archivos eliminados, modificados en tipo o agregados externamente).
    """
    try:
        # 1. Verificar existencia y tipo de cada nodo registrado en la evidencia
        for ev in native_nodes:
            rel = ev.backup.relative_path
            p = root_path if rel in ("", ".") else root_path / rel
            try:
                tipo, c_st = link_kind_and_identity_or_raise(p)
            except OSError:
                return False
            if c_st is None or tipo is not None:
                return False

            is_dir = stat.S_ISDIR(c_st.st_mode)
            expected_dir = ev.backup.node_kind is GoldenProtectionNodeKind.DIR
            if is_dir != expected_dir:
                return False

        # 2. Verificar que no se hayan introducido rutas adicionales en el subárbol
        observed_relpaths: set[str] = set()
        stack: list[pathlib.Path] = [root_path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as sc:
                    for entry in sc:
                        entry_path = pathlib.Path(entry.path)
                        rel_str = entry_path.relative_to(root_path).as_posix()
                        observed_relpaths.add(rel_str)
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry_path)
            except OSError:
                return False

        expected_relpaths = {ev.backup.relative_path for ev in native_nodes if ev.backup.relative_path != "."}
        return observed_relpaths == expected_relpaths
    except OSError:
        return False


# ============================================================================
# Orquestador Principal GP2-S2
# ============================================================================


def orchestrate_golden_protection_planning(
    root: str | os.PathLike[str],
    *,
    operation_id: str,
    policy_version: str = "gp2-v1",
    expected_tree: TreeDigest | None = None,
    expected_runtime: RuntimeIdentity | None = None,
    observed_runtime: RuntimeIdentity | None = None,
    critical_expectations: Sequence[CriticalFileExpectation] = (),
    max_quiescence_attempts: int = MAX_PROBE_RETRIES,
    quiescence_base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
    # Hooks internos exclusivos para inyección en tests unitarios:
    _verify_golden_fn: Callable[..., GoldenMasterVerificationResult] | None = None,
    _inspect_protection_fn: Callable[..., GoldenProtectionResult] | None = None,
    _probe_evidence_fn: Callable[..., Sequence[NativeNodeEvidence]] | None = None,
    _probe_quiescence_fn: Callable[..., None] | None = None,
    _quiescence_jitter_fn: Callable[[float], float] | None = None,
    _quiescence_sleeper: Callable[[float], None] = time.sleep,
) -> GP2Result:
    """Ejecuta secuencialmente los gates read-only de planificación de protección Golden (GP2-S2).

    Garantiza que la evidencia de RV-2 y GP1 sea siempre fresca, ejecuta la captura nativa
    de descriptores e identidades, realiza el probe transitorio de quiescencia y sella el
    manifiesto candidato determinista sin mutar ninguna ACL ni elevar privilegios.
    """
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("operation_id es obligatorio y no puede ser vacío")

    str_root = os.path.abspath(os.fspath(root))
    root_path = pathlib.Path(str_root)

    # ------------------------------------------------------------------------
    # Gate 1: RV-2 Verify Golden Master
    # ------------------------------------------------------------------------
    if _verify_golden_fn is not None:
        golden_verif = _verify_golden_fn(root_path)
    else:
        golden_verif = verify_golden_master(
            root_path,
            expected_tree=expected_tree,
            expected_runtime=expected_runtime,
            observed_runtime=observed_runtime,
            critical_expectations=critical_expectations,
        )

    if (
        not golden_verif.success
        or golden_verif.descriptor is None
        or golden_verif.state is not VerificationState.VERIFIED
    ):
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"Golden Master RV-2 no está VERIFIED: {golden_verif.message}",
            golden_verification=golden_verif,
        )

    if golden_verif.descriptor.role != "reference_only":
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"Golden Master descriptor tiene rol '{golden_verif.descriptor.role}', se requiere 'reference_only'",
            golden_verification=golden_verif,
        )

    # ------------------------------------------------------------------------
    # Gate 2: GP1 Inspect Golden Protection
    # ------------------------------------------------------------------------
    inspect_fn = _inspect_protection_fn or inspect_golden_protection
    gp1_result = inspect_fn(root_path)

    if gp1_result.state is GoldenProtectionState.UNKNOWN:
        return GP2Result(
            disposition=GP2PlanningDisposition.FAILED,
            success=False,
            message=f"Inspección GP1 falló con estado UNKNOWN: {gp1_result.message}",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    if gp1_result.state is GoldenProtectionState.UNSUPPORTED:
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"Entorno no soportado en inspección GP1: {gp1_result.message or 'UNSUPPORTED'}",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    if gp1_result.state is GoldenProtectionState.HARDENED:
        return GP2Result(
            disposition=GP2PlanningDisposition.ALREADY_HARDENED,
            success=True,
            message="",
            sealed_plan=None,
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    if gp1_result.state not in (GoldenProtectionState.UNPROTECTED, GoldenProtectionState.WRITE_PROTECTED):
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"Estado GP1 inválido para planificación: {gp1_result.state.value}",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    # Parent Safety Gate
    parent = gp1_result.evidence.parent_observation
    if parent is not None and parent.granted_rights is not None:
        unsafe = parent.granted_rights & frozenset(
            {
                GoldenProtectionRight.DELETE_CHILD,
                GoldenProtectionRight.CHANGE_PERMISSIONS,
                GoldenProtectionRight.CHANGE_OWNER,
            }
        )
        if unsafe:
            derechos = ", ".join(sorted(r.value for r in unsafe))
            return GP2Result(
                disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
                success=False,
                message=f"PARENT_UNSAFE -> REFUSE_TO_APPLY: parent otorga derechos inseguros ({derechos})",
                golden_verification=golden_verif,
                protection_result=gp1_result,
            )

    # ------------------------------------------------------------------------
    # Gate 3: Native Node Evidence
    # ------------------------------------------------------------------------
    evidence_fn = _probe_evidence_fn or probe_node_evidence
    try:
        native_evidence = evidence_fn(root_path)
    except (InventoryLinkError, NativeEvidenceError, DuplicateFileIdError, OSError) as exc:
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"Error en evidencia nativa de nodos: {exc}",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    # Validar ausencia de delete_pending
    for ev in native_evidence:
        if ev.delete_pending:
            return GP2Result(
                disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
                success=False,
                message=f"Nodo '{ev.backup.relative_path}' tiene delete_pending=True",
                golden_verification=golden_verif,
                protection_result=gp1_result,
            )

    # Validar identidad del nodo raíz
    root_nodes = [ev for ev in native_evidence if ev.backup.relative_path == "."]
    if len(root_nodes) != 1:
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message="Evidencia nativa no contiene exactamente un nodo raíz '.'",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )
    root_ev = root_nodes[0]
    if root_ev.backup.node_kind is not GoldenProtectionNodeKind.DIR:
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message="El nodo raíz '.' no es un directorio",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    # Validar coincidencia de NodeSet entre GP1 y native evidence
    gp1_nodes = gp1_result.evidence.nodes or ()
    gp1_set = {("." if n.relative_path == "" else n.relative_path, n.node_kind) for n in gp1_nodes}
    native_set = {(ev.backup.relative_path, ev.backup.node_kind.value) for ev in native_evidence}
    if gp1_set != native_set:
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"NodeSet mismatch entre GP1 ({len(gp1_set)}) y native evidence ({len(native_set)})",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    # ------------------------------------------------------------------------
    # Gate 4: Quiescence Probe
    # ------------------------------------------------------------------------
    quiescence_fn = _probe_quiescence_fn or probe_tree_quiescence
    jitter = _quiescence_jitter_fn or default_quiescence_jitter
    try:
        quiescence_fn(
            root_path,
            native_evidence,
            max_attempts=max_quiescence_attempts,
            base_backoff_seconds=quiescence_base_backoff_seconds,
            jitter_fn=jitter,
            sleeper=_quiescence_sleeper,
        )
    except (QuiescenceViolationError, QuiescenceError, OSError) as exc:
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"Fallo en la prueba de quiescencia: {exc}",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    # ------------------------------------------------------------------------
    # Gate 5: Structural Pass & Seal Checks & Prepare Plan
    # ------------------------------------------------------------------------
    reparse_absent = all(
        ev.reparse_tag == 0 and not (ev.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT) for ev in native_evidence
    )
    hardlinks_absent = all(
        ev.number_of_links == 1 for ev in native_evidence if ev.backup.node_kind is GoldenProtectionNodeKind.FILE
    )
    structural_match = _check_post_inventory_structural_match(root_path, native_evidence)

    seal_checks = GoldenProtectionSealChecks(
        reparse_points_absent=reparse_absent,
        external_hardlinks_absent=hardlinks_absent,
        post_inventory_structural_match=structural_match,
    )

    if not seal_checks.all_passed:
        reasons: list[str] = []
        if not reparse_absent:
            reasons.append("reparse points detectados")
        if not hardlinks_absent:
            reasons.append("external hardlinks detectados")
        if not structural_match:
            reasons.append("drift estructural detectado en post-inventory check")
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"Seal checks fallaron: {', '.join(reasons)}",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    backups = tuple(ev.backup for ev in native_evidence)
    try:
        plan = prepare_golden_protection_plan(
            golden_verification=golden_verif,
            protection_result=gp1_result,
            operation_id=operation_id,
            canonical_root=root_path,
            volume_serial_number=root_ev.backup.volume_serial_number,
            root_file_id=root_ev.backup.file_id,
            nodes=backups,
            seal_checks=seal_checks,
            policy_version=policy_version,
        )
    except PlanCreationError as exc:
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"prepare_golden_protection_plan rechazó el plan: {exc}",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    # ------------------------------------------------------------------------
    # Gate 6: Seal Golden Protection Plan
    # ------------------------------------------------------------------------
    try:
        sealed_plan = seal_golden_protection_plan(plan)
    except PlanCreationError as exc:
        return GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message=f"seal_golden_protection_plan falló: {exc}",
            golden_verification=golden_verif,
            protection_result=gp1_result,
        )

    # ------------------------------------------------------------------------
    # Outcome: PREPARED
    # ------------------------------------------------------------------------
    return GP2Result(
        disposition=GP2PlanningDisposition.PREPARED,
        success=True,
        message="",
        sealed_plan=sealed_plan,
        golden_verification=golden_verif,
        protection_result=gp1_result,
    )


__all__ = [
    "GP2PlanningDisposition",
    "GP2Result",
    "orchestrate_golden_protection_planning",
]
