"""
Suite de Pruebas Automatizadas AAA para POC-002.
Incluye cobertura de ModPlan vacío, Operation ID Security, Workspace Containment, Target Plugin, TES4 y Orquestación.
"""
import struct
import tempfile
import unittest
from pathlib import Path

from capability import CapabilityRegistry, CapabilityRouter
from exceptions import (
    MalformedRecordError,
    PathContainmentError,
    PolicyViolationError,
    UnsupportedCapabilityError,
    WorkspaceViolationError
)
from orchestrator import Orchestrator, PolicyEngine
from protocol import (
    ClosedOperation,
    EvidenceLevel,
    ModPlan,
    ModPlanOperation,
    OperationReceipt,
    ProtocolStatus,
    validate_operation_id
)
from synthetic_tes4 import (
    GOLDEN_AUTHOR,
    GOLDEN_DESC,
    GOLDEN_SYNTHETIC_TES4_BYTES,
    StrictPluginParser,
    build_synthetic_tes4_record
)
from workspace import JobWorkspace, compute_sha256, resolve_under


class TestOperationIdSecurity(unittest.TestCase):
    """P0-9 & P1-1: Pruebas de contención, formato y no-sobreescritura de operation_id y receipts."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.ws = JobWorkspace(self.root, "JOB-OP-ID-01")
        self.registry = CapabilityRegistry()
        self.router = CapabilityRouter(self.registry)
        self.orchestrator = Orchestrator(self.ws, self.registry, self.router)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_operation_id_allowed(self):
        valid_ids = ["OP-001", "OP_001", "quest.stage-01", "A1", "op123"]
        for op_id in valid_ids:
            self.assertEqual(validate_operation_id(op_id), op_id)

    def test_operation_id_parent_traversal_rejected(self):
        with self.assertRaises(PolicyViolationError):
            validate_operation_id("../../escaped")

    def test_operation_id_windows_traversal_rejected(self):
        with self.assertRaises(PolicyViolationError):
            validate_operation_id("..\\..\\escaped")

    def test_operation_id_separator_rejected(self):
        with self.assertRaises(PolicyViolationError):
            validate_operation_id("OP/001")
        with self.assertRaises(PolicyViolationError):
            validate_operation_id("OP\\001")

    def test_operation_id_windows_drive_rejected(self):
        with self.assertRaises(PolicyViolationError):
            validate_operation_id("C:evil")

    def test_operation_id_absolute_rejected(self):
        with self.assertRaises(PolicyViolationError):
            validate_operation_id("/evil")

    def test_receipt_path_remains_under_receipts_root(self):
        receipt = OperationReceipt(
            operation_id="OP-TEST-SAFE",
            operation=ClosedOperation.INSPECT_HEADER,
            backend="plugin-worker",
            status=ProtocolStatus.SUCCESS,
            evidence_level=EvidenceLevel.E2_REOPENED_ASSERTIONS_PASS,
            input_sha256="abc",
            output_sha256="abc"
        )
        persisted = self.orchestrator.persist_receipt(receipt)
        self.assertTrue(persisted.resolve().is_relative_to(self.ws.receipts_dir.resolve()))
        self.assertEqual(persisted.name, "OP-TEST-SAFE.json")

    def test_duplicate_operation_ids_rejected(self):
        plugin_name = "Synthetic.esp"
        self.ws.provision_original(plugin_name, build_synthetic_tes4_record().serialize())
        self.ws.prepare_candidate_from_original(plugin_name)

        plan = ModPlan(
            plan_id="PLAN-DUP",
            description="Duplicate IDs plan",
            operations=(
                ModPlanOperation.create("OP-DUP", ClosedOperation.INSPECT_HEADER, {}, plugin_name),
                ModPlanOperation.create("OP-DUP", ClosedOperation.INSPECT_HEADER, {}, plugin_name),
            )
        )
        summary = self.orchestrator.execute_plan(plan, plugin_name)
        self.assertEqual(summary.verdict, "FAIL")
        self.assertEqual(summary.receipts[0].status, ProtocolStatus.POLICY_VIOLATION)

    def test_existing_receipt_is_not_overwritten(self):
        receipt1 = OperationReceipt(
            operation_id="OP-UNIQUE-01",
            operation=ClosedOperation.INSPECT_HEADER,
            backend="plugin-worker",
            status=ProtocolStatus.SUCCESS,
            evidence_level=EvidenceLevel.E2_REOPENED_ASSERTIONS_PASS,
            input_sha256="111",
            output_sha256="111"
        )
        receipt2 = OperationReceipt(
            operation_id="OP-UNIQUE-01",
            operation=ClosedOperation.INSPECT_HEADER,
            backend="plugin-worker",
            status=ProtocolStatus.FAILED,
            evidence_level=EvidenceLevel.E0_PLAN_VALID,
            input_sha256="222",
            output_sha256="222"
        )
        self.orchestrator.persist_receipt(receipt1)
        with self.assertRaises(WorkspaceViolationError):
            self.orchestrator.persist_receipt(receipt2)


class TestWorkspaceSecurity(unittest.TestCase):
    """P0-1 & P0-5: Pruebas de contención del Workspace y preservación de Job ID."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_path_allowed(self):
        res = resolve_under(self.root, "plugin.esp")
        self.assertEqual(res, self.root / "plugin.esp")

    def test_parent_traversal_rejected(self):
        with self.assertRaises(PathContainmentError):
            resolve_under(self.root, "../escape.esp")

    def test_windows_parent_traversal_rejected(self):
        with self.assertRaises(PathContainmentError):
            resolve_under(self.root, "..\\escape.esp")

    def test_absolute_posix_path_rejected(self):
        with self.assertRaises(PathContainmentError):
            resolve_under(self.root, "/etc/passwd")

    def test_absolute_windows_drive_rejected(self):
        with self.assertRaises(PathContainmentError):
            resolve_under(self.root, "C:\\Windows\\System32\\cmd.exe")

    def test_windows_drive_relative_rejected(self):
        with self.assertRaises(PathContainmentError):
            resolve_under(self.root, "C:escape.esp")

    def test_unc_path_rejected(self):
        with self.assertRaises(PathContainmentError):
            resolve_under(self.root, "\\\\server\\share\\exploit.esp")

    def test_job_id_traversal_rejected(self):
        with self.assertRaises(PathContainmentError):
            JobWorkspace(self.root, "../evil_job")

    def test_workspace_preserves_validated_job_id(self):
        valid_job_id = "JOB-2026-001"
        ws = JobWorkspace(self.root, valid_job_id)
        self.assertEqual(ws.job_id, valid_job_id)


class TestTargetPluginSecurity(unittest.TestCase):
    """P0-6: Pruebas de contención en target_plugin sobre candidate paths."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.ws = JobWorkspace(self.root, "JOB-SEC-01")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_target_plugin_parent_traversal_rejected(self):
        with self.assertRaises(PathContainmentError):
            self.ws.get_candidate_path("../escape.esp")

    def test_target_plugin_windows_traversal_rejected(self):
        with self.assertRaises(PathContainmentError):
            self.ws.get_candidate_path("..\\escape.esp")

    def test_target_plugin_posix_absolute_rejected(self):
        with self.assertRaises(PathContainmentError):
            self.ws.get_candidate_path("/tmp/escape.esp")

    def test_target_plugin_windows_absolute_rejected(self):
        with self.assertRaises(PathContainmentError):
            self.ws.get_candidate_path("C:\\escape.esp")

    def test_target_plugin_unc_rejected(self):
        with self.assertRaises(PathContainmentError):
            self.ws.get_candidate_path("\\\\server\\share\\escape.esp")

    def test_candidate_path_cannot_escape_candidates_root(self):
        cand_path = self.ws.get_candidate_path("valid_candidate.esp")
        self.assertTrue(cand_path.resolve().is_relative_to(self.ws.candidates_dir.resolve()))


class TestTes4FormatAndGoldenFixture(unittest.TestCase):
    """P0-2 & P0-3: Pruebas de conformidad con Skyrim SE/AE y Golden Fixture independiente."""

    def test_hedr_version_bytes_match_documented_value(self):
        expected_hex = "9a99d93f"
        packed = struct.pack("<f", 1.70)
        unpacked_flt = struct.unpack("<f", bytes.fromhex(expected_hex))[0]
        self.assertEqual(packed.hex(), expected_hex)
        self.assertAlmostEqual(unpacked_flt, 1.70, places=4)

    def test_serializer_matches_golden_bytes(self):
        record = build_synthetic_tes4_record(
            author=GOLDEN_AUTHOR,
            description=GOLDEN_DESC,
            hedr_version=1.70,
            form_version=44
        )
        serialized = record.serialize()
        self.assertEqual(serialized, GOLDEN_SYNTHETIC_TES4_BYTES)

    def test_parser_parses_golden_bytes(self):
        raw_bytes = GOLDEN_SYNTHETIC_TES4_BYTES
        records = StrictPluginParser.parse_records(raw_bytes)
        self.assertEqual(len(records), 1)
        tes4 = records[0]
        self.assertEqual(tes4.sig, "TES4")
        self.assertEqual(tes4.form_version, 44)
        self.assertEqual(len(tes4.subrecords), 3)
        self.assertEqual(tes4.subrecords[0].sig, "HEDR")
        self.assertEqual(tes4.subrecords[1].sig, "CNAM")
        self.assertEqual(tes4.subrecords[2].sig, "SNAM")
        self.assertEqual(tes4.subrecords[1].data, b"Agent\x00")
        self.assertEqual(tes4.subrecords[2].data, b"Test\x00")

    def test_truncated_record_header_rejected(self):
        bad_data = b"TES4\x00\x00\x00"
        with self.assertRaises(MalformedRecordError):
            StrictPluginParser.parse_records(bad_data)

    def test_record_payload_overflow_rejected(self):
        bad_data = b"TES4" + struct.pack("<IIIIHH", 100, 0, 0, 0, 44, 0) + b"\x00" * 10
        with self.assertRaises(MalformedRecordError):
            StrictPluginParser.parse_records(bad_data)

    def test_truncated_subrecord_rejected(self):
        sub_body = b"HED"
        bad_data = b"TES4" + struct.pack("<IIIIHH", len(sub_body), 0, 0, 0, 44, 0) + sub_body
        with self.assertRaises(MalformedRecordError):
            StrictPluginParser.parse_records(bad_data)

    def test_subrecord_payload_overflow_rejected(self):
        bad_sub = b"HEDR" + struct.pack("<H", 20) + b"\x00\x00"
        bad_data = b"TES4" + struct.pack("<IIIIHH", len(bad_sub), 0, 0, 0, 44, 0) + bad_sub
        with self.assertRaises(MalformedRecordError):
            StrictPluginParser.parse_records(bad_data)

    def test_trailing_bytes_rejected(self):
        bad_data = GOLDEN_SYNTHETIC_TES4_BYTES + b"\xDE\xAD\xBE\xEF"
        with self.assertRaises(MalformedRecordError):
            StrictPluginParser.parse_records(bad_data)

    def test_invalid_signature_rejected(self):
        bad_data = b"\xFF\xFE\x00\x01" + struct.pack("<IIIIHH", 0, 0, 0, 0, 44, 0)
        with self.assertRaises(MalformedRecordError):
            StrictPluginParser.parse_records(bad_data)


class TestCapabilityTruth(unittest.TestCase):
    """P0 Capability Truth: Solo capabilities en estado SUPPORTED se resuelven."""

    def setUp(self):
        self.registry = CapabilityRegistry()
        self.router = CapabilityRouter(self.registry)

    def test_inspect_header_is_supported(self):
        backend = self.router.resolve_backend(ClosedOperation.INSPECT_HEADER)
        self.assertEqual(backend, "plugin-worker")

    def test_disabled_operations_rejected(self):
        with self.assertRaises(UnsupportedCapabilityError):
            self.router.resolve_backend(ClosedOperation.CREATE_MISC_ITEM)

        with self.assertRaises(UnsupportedCapabilityError):
            self.router.resolve_backend(ClosedOperation.COMPILE_PAPYRUS)


class TestOrchestrationIntegrity(unittest.TestCase):
    """P0 Fail-Closed Orchestration, Universal Receipts y Preservación de Invariantes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.ws = JobWorkspace(self.base_path, "JOB-ORCH-01")
        self.registry = CapabilityRegistry()
        self.router = CapabilityRouter(self.registry)
        self.orchestrator = Orchestrator(self.ws, self.registry, self.router)

        self.plugin_name = "Synthetic.esp"
        rec = build_synthetic_tes4_record()
        self.ws.provision_original(self.plugin_name, rec.serialize())
        self.ws.prepare_candidate_from_original(self.plugin_name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_successful_inspection_produces_pass_and_receipt(self):
        plan = ModPlan(
            plan_id="PLAN-OK",
            description="Valid Inspection Plan",
            operations=(
                ModPlanOperation.create(
                    operation_id="OP-001",
                    kind=ClosedOperation.INSPECT_HEADER,
                    payload={},
                    target_plugin=self.plugin_name
                ),
            )
        )
        summary = self.orchestrator.execute_plan(plan, self.plugin_name)
        self.assertEqual(summary.verdict, "PASS")
        self.assertEqual(summary.evidence_level, EvidenceLevel.E2_REOPENED_ASSERTIONS_PASS)
        self.assertEqual(summary.original_sha_before, summary.original_sha_after)
        self.assertEqual(len(summary.receipts), 1)
        self.assertEqual(summary.receipts[0].status, ProtocolStatus.SUCCESS)

    def test_first_failure_aborts_following_operations(self):
        plan = ModPlan(
            plan_id="PLAN-FAIL-SEQ",
            description="Plan with unsupported operation followed by inspect",
            operations=(
                ModPlanOperation.create(
                    operation_id="OP-UNSUPPORTED",
                    kind=ClosedOperation.CREATE_MISC_ITEM,
                    payload={},
                    target_plugin=self.plugin_name
                ),
                ModPlanOperation.create(
                    operation_id="OP-AFTER",
                    kind=ClosedOperation.INSPECT_HEADER,
                    payload={},
                    target_plugin=self.plugin_name
                ),
            )
        )
        summary = self.orchestrator.execute_plan(plan, self.plugin_name)
        self.assertEqual(summary.verdict, "FAIL")
        self.assertEqual(len(summary.receipts), 2)
        self.assertEqual(summary.receipts[0].status, ProtocolStatus.UNSUPPORTED_CAPABILITY)
        self.assertEqual(summary.receipts[1].status, ProtocolStatus.ABORTED)

    def test_original_hash_unchanged_after_success(self):
        orig_path = self.ws.get_original_path(self.plugin_name)
        sha_initial = compute_sha256(orig_path)
        plan = ModPlan(
            plan_id="PLAN-CHECK-HASH-OK",
            description="Inspection",
            operations=(
                ModPlanOperation.create("OP-H1", ClosedOperation.INSPECT_HEADER, {}, self.plugin_name),
            )
        )
        summary = self.orchestrator.execute_plan(plan, self.plugin_name)
        self.assertEqual(summary.original_sha_before, sha_initial)
        self.assertEqual(summary.original_sha_after, sha_initial)

    def test_original_hash_unchanged_after_failure(self):
        orig_path = self.ws.get_original_path(self.plugin_name)
        sha_initial = compute_sha256(orig_path)
        plan = ModPlan(
            plan_id="PLAN-CHECK-HASH-FAIL",
            description="Failure plan",
            operations=(
                ModPlanOperation.create("OP-FAIL", ClosedOperation.CREATE_MISC_ITEM, {}, self.plugin_name),
            )
        )
        summary = self.orchestrator.execute_plan(plan, self.plugin_name)
        self.assertEqual(summary.original_sha_before, sha_initial)
        self.assertEqual(summary.original_sha_after, sha_initial)

    def test_receipt_count_matches_plan_operations(self):
        plan = ModPlan(
            plan_id="PLAN-RECEIPTS-COUNT",
            description="Two operations plan",
            operations=(
                ModPlanOperation.create("OP-R1", ClosedOperation.INSPECT_HEADER, {}, self.plugin_name),
                ModPlanOperation.create("OP-R2", ClosedOperation.INSPECT_HEADER, {}, self.plugin_name),
            )
        )
        summary = self.orchestrator.execute_plan(plan, self.plugin_name)
        self.assertEqual(len(summary.receipts), len(plan.operations))
        receipt_files = list(self.ws.receipts_dir.glob("*.json"))
        self.assertEqual(len(receipt_files), 2)

    def test_empty_plan_rejected(self):
        empty_plan = ModPlan(plan_id="PLAN-EMPTY", description="Empty", operations=())
        policy = PolicyEngine(self.registry)
        with self.assertRaises(PolicyViolationError):
            policy.validate_plan(empty_plan)

    def test_empty_plan_never_returns_e2(self):
        empty_plan = ModPlan(plan_id="PLAN-EMPTY", description="Empty", operations=())
        summary = self.orchestrator.execute_plan(empty_plan, self.plugin_name)
        self.assertEqual(summary.verdict, "FAIL")
        self.assertEqual(summary.evidence_level, EvidenceLevel.E0_PLAN_VALID)

    def test_empty_plan_produces_policy_rejection_receipt(self):
        empty_plan = ModPlan(plan_id="PLAN-EMPTY", description="Empty", operations=())
        summary = self.orchestrator.execute_plan(empty_plan, self.plugin_name)
        self.assertEqual(len(summary.receipts), 1)
        self.assertEqual(summary.receipts[0].status, ProtocolStatus.POLICY_VIOLATION)
        self.assertEqual(summary.receipts[0].operation_id, "PLAN_POLICY_REJECTION")
        receipt_file = self.ws.receipts_dir / "PLAN_POLICY_REJECTION.json"
        self.assertTrue(receipt_file.exists())


if __name__ == "__main__":
    unittest.main()
