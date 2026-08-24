"""
Plugin Worker para POC-002: Ejecuta exclusivamente inspección y validación de cabecera.
Resuelve P0-4 (validación no contradictoria y comparación con tolerancia flotante).
"""
import struct
from pathlib import Path
from typing import List

from exceptions import MalformedRecordError, PathContainmentError
from protocol import (
    AssertionResult,
    ClosedOperation,
    EvidenceLevel,
    OperationReceipt,
    ProtocolStatus
)
from synthetic_tes4 import SUPPORTED_HEDR_VERSIONS, StrictPluginParser
from workspace import JobWorkspace, compute_sha256


class PluginWorker:
    def __init__(self, workspace: JobWorkspace):
        self.workspace = workspace

    def execute(self, op_id: str, op: ClosedOperation, payload: dict, target_plugin: str) -> OperationReceipt:
        try:
            candidate_path = self.workspace.get_candidate_path(target_plugin)
        except PathContainmentError as e:
            return OperationReceipt(
                operation_id=op_id,
                operation=op,
                backend="plugin-worker",
                status=ProtocolStatus.FAILED,
                evidence_level=EvidenceLevel.E0_PLAN_VALID,
                input_sha256=None,
                output_sha256=None,
                error_message=f"Path containment error en target_plugin: {e}"
            )
        except Exception as e:
            return OperationReceipt(
                operation_id=op_id,
                operation=op,
                backend="plugin-worker",
                status=ProtocolStatus.FAILED,
                evidence_level=EvidenceLevel.E0_PLAN_VALID,
                input_sha256=None,
                output_sha256=None,
                error_message=f"Workspace error inesperado: {e}"
            )

        if not candidate_path.exists():
            return OperationReceipt(
                operation_id=op_id,
                operation=op,
                backend="plugin-worker",
                status=ProtocolStatus.FAILED,
                evidence_level=EvidenceLevel.E0_PLAN_VALID,
                input_sha256=None,
                output_sha256=None,
                error_message=f"Candidate plugin {target_plugin} no existe en candidates/."
            )

        input_sha = compute_sha256(candidate_path)

        if op != ClosedOperation.INSPECT_HEADER:
            return OperationReceipt(
                operation_id=op_id,
                operation=op,
                backend="plugin-worker",
                status=ProtocolStatus.UNSUPPORTED_CAPABILITY,
                evidence_level=EvidenceLevel.E0_PLAN_VALID,
                input_sha256=input_sha,
                output_sha256=input_sha,
                error_message=f"Operación '{op.value}' no soportada por PluginWorker en POC-002."
            )

        return self._handle_inspect_header(op_id, candidate_path, input_sha)

    def _handle_inspect_header(self, op_id: str, candidate_path: Path, input_sha: str) -> OperationReceipt:
        try:
            data = candidate_path.read_bytes()
            records = StrictPluginParser.parse_records(data)

            if not records or records[0].sig != "TES4":
                raise MalformedRecordError("El archivo no comienza con un record TES4 válido.")

            tes4 = records[0]
            assertions: List[AssertionResult] = []

            assertions.append(AssertionResult(
                check_type="record_signature_is_tes4",
                expected="TES4",
                actual=tes4.sig,
                passed=(tes4.sig == "TES4"),
                details="Firma de cabecera validada."
            ))

            assertions.append(AssertionResult(
                check_type="form_version_is_44",
                expected=44,
                actual=tes4.form_version,
                passed=(tes4.form_version == 44),
                details=f"FormVersion: {tes4.form_version}"
            ))

            hedr_found = False
            author = "UNKNOWN"
            desc = "UNKNOWN"

            for sub in tes4.subrecords:
                if sub.sig == "HEDR":
                    hedr_found = True
                    if len(sub.data) != 12:
                        raise MalformedRecordError(f"HEDR size inválido: {len(sub.data)} (esperado 12).")
                    version_flt, num_recs, next_fid = struct.unpack("<fII", sub.data)

                    is_supported_version = any(abs(version_flt - v) < 1e-3 for v in SUPPORTED_HEDR_VERSIONS)
                    assertions.append(AssertionResult(
                        check_type="hedr_version_supported",
                        expected=f"One of {list(SUPPORTED_HEDR_VERSIONS)}",
                        actual=round(version_flt, 2),
                        passed=is_supported_version,
                        details=f"HEDR.version={version_flt:.4f}, num_records={num_recs}, next_form_id=0x{next_fid:08X}"
                    ))
                elif sub.sig == "CNAM":
                    author = sub.data.rstrip(b"\x00").decode("utf-8", errors="replace")
                elif sub.sig == "SNAM":
                    desc = sub.data.rstrip(b"\x00").decode("utf-8", errors="replace")

            assertions.append(AssertionResult(
                check_type="hedr_subrecord_present",
                expected=True,
                actual=hedr_found,
                passed=hedr_found,
                details=f"Author: '{author}', Description: '{desc}'"
            ))

            all_passed = all(a.passed for a in assertions)

            return OperationReceipt(
                operation_id=op_id,
                operation=ClosedOperation.INSPECT_HEADER,
                backend="plugin-worker",
                status=ProtocolStatus.SUCCESS if all_passed else ProtocolStatus.FAILED,
                evidence_level=EvidenceLevel.E2_REOPENED_ASSERTIONS_PASS if all_passed else EvidenceLevel.E1_WORKER_COMPLETED,
                input_sha256=input_sha,
                output_sha256=input_sha,
                assertions=tuple(assertions)
            )

        except Exception as e:
            return OperationReceipt(
                operation_id=op_id,
                operation=ClosedOperation.INSPECT_HEADER,
                backend="plugin-worker",
                status=ProtocolStatus.FAILED,
                evidence_level=EvidenceLevel.E0_PLAN_VALID,
                input_sha256=input_sha,
                output_sha256=None,
                error_message=f"Fallo al inspeccionar header: {e}"
            )
