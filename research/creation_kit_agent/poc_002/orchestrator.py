"""
Orquestador de Pipeline con semántica Fail-Closed, Receipts Inmutables y No-Overwrite.
Corrige el P0 de ModPlan vacío exigiendo al menos una operación y protegiendo el cálculo de veredicto.
"""
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from capability import CapabilityRegistry, CapabilityRouter
from exceptions import (
    InvariantViolationError,
    PathContainmentError,
    PolicyViolationError,
    UnsupportedCapabilityError,
    WorkspaceViolationError
)
from protocol import (
    ClosedOperation,
    EvidenceLevel,
    ModPlan,
    ModPlanOperation,
    OperationReceipt,
    ProtocolStatus,
    validate_operation_id
)
from worker import PluginWorker
from workspace import JobWorkspace, compute_sha256, resolve_under

logger = logging.getLogger("SkyrimAgent.Orchestrator")


class PolicyEngine:
    """Validación exhaustiva de contrato antes de tocar el filesystem o invocar workers."""
    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def validate_plan(self, plan: ModPlan) -> None:
        if not plan.operations:
            raise PolicyViolationError("ModPlan debe contener al menos una operación.")

        op_ids = [op.operation_id for op in plan.operations]
        if len(op_ids) != len(set(op_ids)):
            raise PolicyViolationError("operation_id duplicado detectado en el ModPlan.")

        for op in plan.operations:
            validate_operation_id(op.operation_id)

            entry = self.registry.get(op.kind)
            if not entry:
                raise PolicyViolationError(f"Operación no permitida en registro: {op.kind}")

            if not op.target_plugin or not op.target_plugin.lower().endswith((".esp", ".esm", ".esl")):
                raise PolicyViolationError(f"Extensión de target_plugin inválida: {op.target_plugin}")

            for k, v in op.payload:
                if isinstance(v, str) and (".." in v or "/" in v or "\\" in v):
                    raise PolicyViolationError(f"Valor sospechoso en payload key '{k}': {v}")


@dataclass(frozen=True)
class ExecutionSummary:
    job_id: str
    plan_id: str
    verdict: str
    evidence_level: EvidenceLevel
    receipts: Tuple[OperationReceipt, ...]
    original_sha_before: str
    original_sha_after: str
    reasons: Tuple[str, ...]

    def to_dict(self) -> Dict:
        return {
            "job_id": self.job_id,
            "plan_id": self.plan_id,
            "verdict": self.verdict,
            "evidence_level": self.evidence_level.value,
            "original_sha_before": self.original_sha_before,
            "original_sha_after": self.original_sha_after,
            "reasons": list(self.reasons),
            "receipts": [r.to_dict() for r in self.receipts]
        }


class Orchestrator:
    def __init__(self, workspace: JobWorkspace, registry: CapabilityRegistry, router: CapabilityRouter):
        self.workspace = workspace
        self.registry = registry
        self.router = router
        self.policy = PolicyEngine(registry)
        self.worker = PluginWorker(workspace)

    def persist_receipt(self, receipt: OperationReceipt) -> Path:
        safe_id = validate_operation_id(receipt.operation_id)
        filename = f"{safe_id}.json"
        dest = resolve_under(self.workspace.receipts_dir, filename)

        if dest.exists():
            raise WorkspaceViolationError(f"El recibo ya existe y no puede ser sobrescrito: {filename}")

        dest.write_text(json.dumps(receipt.to_dict(), indent=2))
        return dest

    def execute_plan(self, plan: ModPlan, primary_plugin: str) -> ExecutionSummary:
        receipts: List[OperationReceipt] = []
        reasons: List[str] = []

        orig_path = self.workspace.get_original_path(primary_plugin)
        original_sha_before = compute_sha256(orig_path)

        try:
            self.policy.validate_plan(plan)
        except Exception as e:
            reasons.append(f"Policy Engine rechazó el plan: {e}")
            safe_rejection_id = "PLAN_POLICY_REJECTION"
            receipt = OperationReceipt(
                operation_id=safe_rejection_id,
                operation=ClosedOperation.INSPECT_HEADER,
                backend="policy-engine",
                status=ProtocolStatus.POLICY_VIOLATION,
                evidence_level=EvidenceLevel.E0_PLAN_VALID,
                input_sha256=original_sha_before,
                output_sha256=original_sha_before,
                error_message=str(e)
            )
            self.persist_receipt(receipt)
            receipts.append(receipt)

            original_sha_after = compute_sha256(orig_path)
            return ExecutionSummary(
                job_id=self.workspace.job_id,
                plan_id=plan.plan_id,
                verdict="FAIL",
                evidence_level=EvidenceLevel.E0_PLAN_VALID,
                receipts=tuple(receipts),
                original_sha_before=original_sha_before,
                original_sha_after=original_sha_after,
                reasons=tuple(reasons)
            )

        aborted = False

        for op in plan.operations:
            if aborted:
                receipt = OperationReceipt(
                    operation_id=op.operation_id,
                    operation=op.kind,
                    backend="orchestrator",
                    status=ProtocolStatus.ABORTED,
                    evidence_level=EvidenceLevel.E0_PLAN_VALID,
                    input_sha256=None,
                    output_sha256=None,
                    error_message="Operación abortada debido a fallo previo en el plan."
                )
                self.persist_receipt(receipt)
                receipts.append(receipt)
                continue

            try:
                backend = self.router.resolve_backend(op.kind)
                if backend != "plugin-worker":
                    raise UnsupportedCapabilityError(f"Backend '{backend}' no instanciado para POC-002.")

                receipt = self.worker.execute(
                    op_id=op.operation_id,
                    op=op.kind,
                    payload=op.get_payload_dict(),
                    target_plugin=op.target_plugin
                )
            except UnsupportedCapabilityError as e:
                receipt = OperationReceipt(
                    operation_id=op.operation_id,
                    operation=op.kind,
                    backend="capability-router",
                    status=ProtocolStatus.UNSUPPORTED_CAPABILITY,
                    evidence_level=EvidenceLevel.E0_PLAN_VALID,
                    input_sha256=None,
                    output_sha256=None,
                    error_message=str(e)
                )
            except Exception as e:
                receipt = OperationReceipt(
                    operation_id=op.operation_id,
                    operation=op.kind,
                    backend="orchestrator",
                    status=ProtocolStatus.FAILED,
                    evidence_level=EvidenceLevel.E0_PLAN_VALID,
                    input_sha256=None,
                    output_sha256=None,
                    error_message=f"Fallo en resolución/ejecución: {e}"
                )

            self.persist_receipt(receipt)
            receipts.append(receipt)

            if receipt.status != ProtocolStatus.SUCCESS:
                aborted = True
                reasons.append(f"Operación {op.operation_id} finalizó con estado {receipt.status.value}: {receipt.error_message}")

        original_sha_after = compute_sha256(orig_path)
        if original_sha_before != original_sha_after:
            raise InvariantViolationError(
                f"FALLO CRÍTICO DE INTEGRIDAD: SHA del original fue alterado ({original_sha_before} != {original_sha_after})"
            )

        has_receipts = bool(receipts)
        all_success = all(r.status == ProtocolStatus.SUCCESS for r in receipts)
        all_assertions_passed = (
            has_receipts and all(
                bool(r.assertions) and all(a.passed for a in r.assertions)
                for r in receipts
            )
        )
        immutability_preserved = (original_sha_before == original_sha_after)

        if has_receipts and all_success and all_assertions_passed and immutability_preserved and not reasons:
            verdict = "PASS"
            evidence_level = EvidenceLevel.E2_REOPENED_ASSERTIONS_PASS
        else:
            verdict = "FAIL"
            evidence_level = EvidenceLevel.E0_PLAN_VALID

        return ExecutionSummary(
            job_id=self.workspace.job_id,
            plan_id=plan.plan_id,
            verdict=verdict,
            evidence_level=evidence_level,
            receipts=tuple(receipts),
            original_sha_before=original_sha_before,
            original_sha_after=original_sha_after,
            reasons=tuple(reasons)
        )
