"""Tests para la orquestación read-only de planificación GP2-S2.

Cubre exhaustivamente:
- Orquestación: S2-01 a S2-15
- Seal: P1 a P5
- FSM / Result: F1 a F5
- AST Anti-Mutation Guards
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest

from sky_claw.local.runtime_vault.golden_protection_plan import (
    DuplicateFileIdError,
    GoldenProtectionNodeKind,
    GoldenProtectionPlanState,
    NodeSecurityBackup,
    SealedGoldenProtectionPlan,
)
from sky_claw.local.runtime_vault.models import (
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    InventoryLinkError,
    RuntimeIdentity,
    RuntimeVerificationResult,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.node_evidence import NativeNodeEvidence
from sky_claw.local.runtime_vault.planning_orchestrator import (
    GP2PlanningDisposition,
    GP2Result,
    orchestrate_golden_protection_planning,
)
from sky_claw.local.runtime_vault.protection import (
    GoldenProtectionEvidence,
    GoldenProtectionResult,
    GoldenProtectionRight,
    GoldenProtectionState,
    NodeProtectionObservation,
    classify_protection,
)
from sky_claw.local.runtime_vault.quiescence import QuiescenceViolationError

# ============================================================================
# Fixtures y Helpers Sintéticos Deterministas
# ============================================================================


def _create_synthetic_sd_b64() -> tuple[str, int, str]:
    """Genera bytes SD simulados válidos con binding SHA-256."""
    import base64

    raw_bytes = b"SYNTHETIC_SECURITY_DESCRIPTOR_BYTES_FOR_TESTS_1234"
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    length = len(raw_bytes)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return b64, length, digest


def _create_synthetic_backup(
    relpath: str,
    node_kind: GoldenProtectionNodeKind,
    file_id: int,
    volume_serial: int = 0x12345678,
) -> NodeSecurityBackup:
    b64, length, digest = _create_synthetic_sd_b64()
    return NodeSecurityBackup(
        relative_path=relpath,
        node_kind=node_kind,
        volume_serial_number=volume_serial,
        file_id=file_id,
        pre_sd_bytes_b64=b64,
        pre_sd_length=length,
        pre_sd_sha256=digest,
        owner_sid="S-1-5-21-1000",
        group_sid="S-1-5-21-513",
        dacl_control_flags=0x1004,  # SE_DACL_PROTECTED | SE_DACL_PRESENT
        pre_dacl_protected_flag=True,
    )


def _create_synthetic_native_evidence(
    relpath: str,
    node_kind: GoldenProtectionNodeKind,
    file_id: int,
    number_of_links: int = 1,
    reparse_tag: int = 0,
    file_attributes: int = 0x20,  # FILE_ATTRIBUTE_ARCHIVE
    delete_pending: bool = False,
    volume_serial: int = 0x12345678,
) -> NativeNodeEvidence:
    backup = _create_synthetic_backup(relpath, node_kind, file_id, volume_serial=volume_serial)
    return NativeNodeEvidence(
        backup=backup,
        number_of_links=number_of_links,
        reparse_tag=reparse_tag,
        file_attributes=file_attributes,
        delete_pending=delete_pending,
    )


@pytest.fixture
def mock_clean_golden_tree(
    tmp_path: pathlib.Path,
) -> tuple[
    pathlib.Path,
    GoldenMasterVerificationResult,
    GoldenProtectionResult,
    tuple[NativeNodeEvidence, ...],
]:
    """Crea un árbol descartable con RV-2 VERIFIED, GP1 UNPROTECTED y evidencia nativa limpia."""
    root = tmp_path / "GoldenMaster"
    root.mkdir()
    f1 = root / "SkyrimSE.exe"
    f1.write_bytes(b"MOCK_EXE_CONTENT")
    sub = root / "Data"
    sub.mkdir()
    f2 = sub / "Skyrim.esm"
    f2.write_bytes(b"MOCK_ESM_CONTENT")

    runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
    tree_digest = TreeDigest(digest="a" * 64, files=2, bytes=f1.stat().st_size + f2.stat().st_size)
    descriptor = GoldenMasterDescriptor(
        location=root,
        runtime_identity=runtime_id,
        tree_digest=tree_digest,
        role="reference_only",
    )
    tree_result = TreeVerificationResult(
        state=VerificationState.VERIFIED,
        message="",
        expected=tree_digest,
        observed=tree_digest,
    )
    runtime_result = RuntimeVerificationResult(
        state=VerificationState.VERIFIED,
        message="",
        expected=runtime_id,
        observed=runtime_id,
    )
    golden_verif = GoldenMasterVerificationResult(
        state=VerificationState.VERIFIED,
        message="",
        tree_result=tree_result,
        runtime_result=runtime_result,
        critical_results=(),
        descriptor=descriptor,
    )

    read_only_rights = frozenset(
        {
            GoldenProtectionRight.READ_DATA,
            GoldenProtectionRight.EXECUTE,
        }
    )
    dir_rights = frozenset(
        {
            GoldenProtectionRight.READ_DATA,
            GoldenProtectionRight.EXECUTE,
            GoldenProtectionRight.ADD_FILE,
        }
    )
    file_rights = frozenset(
        {
            GoldenProtectionRight.READ_DATA,
            GoldenProtectionRight.EXECUTE,
            GoldenProtectionRight.WRITE_DATA,
        }
    )
    gp1_evidence = GoldenProtectionEvidence(
        platform="windows",
        drive_type=3,
        filesystem="NTFS",
        filesystem_persistent_acls=True,
        current_user_sid="S-1-5-21-1000",
        current_group_sids=frozenset({"S-1-5-21-513"}),
        current_token_elevated=False,
        nodes=(
            NodeProtectionObservation(
                ".",
                "dir",
                "S-1-5-21-1000",
                dir_rights,
                owner_rights_ace_present=False,
                dacl_inheritance_protected=False,
            ),
            NodeProtectionObservation(
                "SkyrimSE.exe",
                "file",
                "S-1-5-21-1000",
                file_rights,
                owner_rights_ace_present=False,
                dacl_inheritance_protected=False,
            ),
            NodeProtectionObservation(
                "Data",
                "dir",
                "S-1-5-21-1000",
                dir_rights,
                owner_rights_ace_present=False,
                dacl_inheritance_protected=False,
            ),
            NodeProtectionObservation(
                "Data/Skyrim.esm",
                "file",
                "S-1-5-21-1000",
                file_rights,
                owner_rights_ace_present=False,
                dacl_inheritance_protected=False,
            ),
        ),
        parent_observation=NodeProtectionObservation(
            "..",
            "dir",
            "S-1-5-21-1000",
            read_only_rights,
            owner_rights_ace_present=True,
            dacl_inheritance_protected=True,
        ),
        pre_post_structural_match=True,
    )
    gp1_result = classify_protection(gp1_evidence)

    native_ev = (
        _create_synthetic_native_evidence("SkyrimSE.exe", GoldenProtectionNodeKind.FILE, 101),
        _create_synthetic_native_evidence("Data/Skyrim.esm", GoldenProtectionNodeKind.FILE, 102),
        _create_synthetic_native_evidence("Data", GoldenProtectionNodeKind.DIR, 103, file_attributes=0x10),
        _create_synthetic_native_evidence(".", GoldenProtectionNodeKind.DIR, 100, file_attributes=0x10),
    )

    return root, golden_verif, gp1_result, native_ev


# ============================================================================
# S2-01 a S2-15: Orquestación Secuencial de Gates
# ============================================================================


def test_s2_01_rv2_no_verified_no_llama_gates_posteriores(mock_clean_golden_tree: Any) -> None:
    """S2-01: Si RV-2 no está VERIFIED, no se invocan GP1, evidencia nativa ni quiescence."""
    root, _, gp1_result, _ = mock_clean_golden_tree

    unverified_rv2 = GoldenMasterVerificationResult(
        state=VerificationState.FAILED,
        message="Hash mismatch en SkyrimSE.exe",
        tree_result=None,
        runtime_result=None,
        critical_results=(),
        descriptor=None,
    )

    gp1_called = False
    evidence_called = False
    quiescence_called = False

    def spy_gp1(*args: Any) -> GoldenProtectionResult:
        nonlocal gp1_called
        gp1_called = True
        return gp1_result

    def spy_evidence(*args: Any) -> Sequence[NativeNodeEvidence]:
        nonlocal evidence_called
        evidence_called = True
        return ()

    def spy_quiescence(*args: Any, **kwargs: Any) -> None:
        nonlocal quiescence_called
        quiescence_called = True

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-01",
        _verify_golden_fn=lambda r: unverified_rv2,
        _inspect_protection_fn=spy_gp1,
        _probe_evidence_fn=spy_evidence,
        _probe_quiescence_fn=spy_quiescence,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "no está VERIFIED" in result.message
    assert result.sealed_plan is None
    assert not gp1_called
    assert not evidence_called
    assert not quiescence_called


def test_s2_02_golden_descriptor_no_reference_only_refusa(mock_clean_golden_tree: Any) -> None:
    """S2-02: Descriptor sin role='reference_only' genera REFUSE_TO_APPLY."""
    root, golden_verif, _, _ = mock_clean_golden_tree

    assert golden_verif.descriptor is not None
    bad_descriptor = MagicMock()
    bad_descriptor.location = root
    bad_descriptor.tree_digest = golden_verif.tree_result.observed
    bad_descriptor.runtime_identity = golden_verif.runtime_result.observed
    bad_descriptor.role = "mutable_working_copy"

    rv2_res = GoldenMasterVerificationResult(
        state=VerificationState.VERIFIED,
        message="",
        tree_result=golden_verif.tree_result,
        runtime_result=golden_verif.runtime_result,
        critical_results=(),
        descriptor=bad_descriptor,
    )

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-000000000002",
        _verify_golden_fn=lambda r: rv2_res,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "reference_only" in result.message


def test_s2_03_gp1_unknown_fail_closed(mock_clean_golden_tree: Any) -> None:
    """S2-03: GP1 UNKNOWN falla cerrado con disposition=FAILED."""
    root, golden_verif, _, _ = mock_clean_golden_tree

    gp1_unknown = GoldenProtectionResult(
        state=GoldenProtectionState.UNKNOWN,
        evidence=GoldenProtectionEvidence(platform="win32"),
        message="Permisos ambiguos detectados",
    )

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-03",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_unknown,
    )

    assert result.disposition is GP2PlanningDisposition.FAILED
    assert not result.success
    assert "UNKNOWN" in result.message


def test_s2_04_gp1_unsupported_refusa(mock_clean_golden_tree: Any) -> None:
    """S2-04: GP1 UNSUPPORTED genera REFUSE_TO_APPLY."""
    root, golden_verif, _, _ = mock_clean_golden_tree

    gp1_unsupported = GoldenProtectionResult(
        state=GoldenProtectionState.UNSUPPORTED,
        evidence=GoldenProtectionEvidence(platform="linux"),
        message="",
    )

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-04",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_unsupported,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "UNSUPPORTED" in result.message


def test_s2_05_gp1_hardened_es_noop_tipado_sin_plan_mutador(mock_clean_golden_tree: Any) -> None:
    """S2-05: GP1 HARDENED devuelve ALREADY_HARDENED y no genera plan mutador."""
    root, golden_verif, _, _ = mock_clean_golden_tree

    gp1_hardened = GoldenProtectionResult(
        state=GoldenProtectionState.HARDENED,
        evidence=GoldenProtectionEvidence(platform="win32"),
        message="",
    )

    evidence_called = False

    def spy_evidence(*args: Any) -> Sequence[NativeNodeEvidence]:
        nonlocal evidence_called
        evidence_called = True
        return ()

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-05",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_hardened,
        _probe_evidence_fn=spy_evidence,
    )

    assert result.disposition is GP2PlanningDisposition.ALREADY_HARDENED
    assert result.success is True
    assert result.message == ""
    assert result.sealed_plan is None
    assert not evidence_called


def test_s2_06_s2_07_gp1_unprotected_y_write_protected_continuan(mock_clean_golden_tree: Any) -> None:
    """S2-06 y S2-07: UNPROTECTED y WRITE_PROTECTED continúan válidamente al plan."""
    root, golden_verif, gp1_unprotected, native_ev = mock_clean_golden_tree

    # S2-06: UNPROTECTED
    res_unprotected = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-000000000006",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_unprotected,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )
    assert res_unprotected.disposition is GP2PlanningDisposition.PREPARED
    assert res_unprotected.success is True

    # S2-07: WRITE_PROTECTED
    read_only_rights = frozenset(
        {
            GoldenProtectionRight.READ_DATA,
            GoldenProtectionRight.EXECUTE,
        }
    )
    ev_write_prot = GoldenProtectionEvidence(
        platform="windows",
        drive_type=3,
        filesystem="NTFS",
        filesystem_persistent_acls=True,
        current_user_sid="S-1-5-21-1000",
        current_group_sids=frozenset({"S-1-5-21-513"}),
        current_token_elevated=False,
        pre_post_structural_match=True,
        nodes=(
            NodeProtectionObservation(
                ".",
                "dir",
                "S-1-5-21-1000",
                read_only_rights,
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
            NodeProtectionObservation(
                "SkyrimSE.exe",
                "file",
                "S-1-5-21-1000",
                read_only_rights,
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
            NodeProtectionObservation(
                "Data",
                "dir",
                "S-1-5-21-1000",
                read_only_rights,
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
            NodeProtectionObservation(
                "Data/Skyrim.esm",
                "file",
                "S-1-5-21-1000",
                read_only_rights,
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        ),
        parent_observation=NodeProtectionObservation(
            "..",
            "dir",
            "S-1-5-21-1000",
            read_only_rights,
            owner_rights_ace_present=True,
            dacl_inheritance_protected=True,
        ),
    )
    gp1_write_prot = classify_protection(ev_write_prot)
    assert gp1_write_prot.state is GoldenProtectionState.WRITE_PROTECTED

    res_write_prot = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-000000000007",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_write_prot,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )
    assert res_write_prot.disposition is GP2PlanningDisposition.PREPARED
    assert res_write_prot.success is True


def test_s2_08_parent_unsafe_refusa(mock_clean_golden_tree: Any) -> None:
    """S2-08: Parent con DELETE_CHILD o WRITE_DAC genera REFUSE_TO_APPLY."""
    root, golden_verif, gp1_clean, _ = mock_clean_golden_tree

    unsafe_rights = frozenset({GoldenProtectionRight.DELETE_CHILD, GoldenProtectionRight.READ_DATA})
    unsafe_parent = NodeProtectionObservation(
        "..",
        "dir",
        "S-1-5-21-1000",
        unsafe_rights,
        owner_rights_ace_present=True,
    )
    evidence_unsafe = GoldenProtectionEvidence(
        platform="win32",
        nodes=gp1_clean.evidence.nodes,
        parent_observation=unsafe_parent,
        pre_post_structural_match=True,
    )
    gp1_unsafe = GoldenProtectionResult(
        state=GoldenProtectionState.UNPROTECTED,
        evidence=evidence_unsafe,
        message="",
    )

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-08",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_unsafe,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "PARENT_UNSAFE" in result.message


def test_s2_09_nodeset_gp1_distinto_de_native_evidence_refusa(mock_clean_golden_tree: Any) -> None:
    """S2-09: Discrepancia entre NodeSet GP1 y evidencia nativa genera REFUSE_TO_APPLY."""
    root, golden_verif, gp1_clean, _ = mock_clean_golden_tree

    # Evidencia nativa a la que le falta un archivo
    incomplete_native = (
        _create_synthetic_native_evidence("SkyrimSE.exe", GoldenProtectionNodeKind.FILE, 101),
        _create_synthetic_native_evidence(".", GoldenProtectionNodeKind.DIR, 100, file_attributes=0x10),
    )

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-09",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: incomplete_native,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "NodeSet mismatch" in result.message


def test_s2_10_root_identity_mismatch_refusa(mock_clean_golden_tree: Any) -> None:
    """S2-10: Ausencia o tipo incorrecto de nodo raíz '.' genera REFUSE_TO_APPLY."""
    root, golden_verif, gp1_clean, _ = mock_clean_golden_tree

    # Raíz marcada como FILE en vez de DIR
    bad_root_native = (_create_synthetic_native_evidence(".", GoldenProtectionNodeKind.FILE, 100),)

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-10",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: bad_root_native,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "nodo raíz '.' no es un directorio" in result.message or "NodeSet mismatch" in result.message


def test_s2_11_reparse_point_detectado_refusa(mock_clean_golden_tree: Any) -> None:
    """S2-11: Presencia de reparse point en la evidencia nativa genera REFUSE_TO_APPLY."""
    root, golden_verif, gp1_clean, _ = mock_clean_golden_tree

    def mock_reparse_evidence(*args: Any) -> Sequence[NativeNodeEvidence]:
        raise InventoryLinkError("Junction detectado en Data/Link")

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-11",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=mock_reparse_evidence,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "Junction detectado" in result.message


def test_s2_12_external_hardlink_detectado_refusa(mock_clean_golden_tree: Any) -> None:
    """S2-12: Presencia de hardlinks externos (NumberOfLinks > 1) genera REFUSE_TO_APPLY."""
    root, golden_verif, gp1_clean, _ = mock_clean_golden_tree

    def mock_hardlink_evidence(*args: Any) -> Sequence[NativeNodeEvidence]:
        raise DuplicateFileIdError("NumberOfLinks=2 != 1 (hardlink externo detectado)")

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-12",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=mock_hardlink_evidence,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "hardlink externo" in result.message


def test_s2_13_delete_pending_incompatible_refusa(mock_clean_golden_tree: Any) -> None:
    """S2-13: delete_pending=True en cualquier nodo genera REFUSE_TO_APPLY."""
    root, golden_verif, gp1_clean, _ = mock_clean_golden_tree

    del_pending_native = (
        _create_synthetic_native_evidence("SkyrimSE.exe", GoldenProtectionNodeKind.FILE, 101, delete_pending=True),
        _create_synthetic_native_evidence("Data/Skyrim.esm", GoldenProtectionNodeKind.FILE, 102),
        _create_synthetic_native_evidence("Data", GoldenProtectionNodeKind.DIR, 103, file_attributes=0x10),
        _create_synthetic_native_evidence(".", GoldenProtectionNodeKind.DIR, 100, file_attributes=0x10),
    )

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-13",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: del_pending_native,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "delete_pending=True" in result.message


def test_s2_14_quiescence_failure_no_prepara_plan(mock_clean_golden_tree: Any) -> None:
    """S2-14: Fallo de quiescencia genera REFUSE_TO_APPLY y nunca prepara ni sella plan."""
    root, golden_verif, gp1_clean, native_ev = mock_clean_golden_tree

    def mock_failing_quiescence(*args: Any, **kwargs: Any) -> None:
        raise QuiescenceViolationError("ERROR_SHARING_VIOLATION persistente")

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-s2-14",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=mock_failing_quiescence,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "Fallo en la prueba de quiescencia" in result.message
    assert result.sealed_plan is None


def test_s2_15_todos_los_gates_pasan_produce_prepared_y_sealed_plan(mock_clean_golden_tree: Any) -> None:
    """S2-15: Éxito a través de todos los gates produce PREPARED con SealedGoldenProtectionPlan válido."""
    root, golden_verif, gp1_clean, native_ev = mock_clean_golden_tree

    op_id = "00000000-0000-4000-8000-000000000015"
    result = orchestrate_golden_protection_planning(
        root,
        operation_id=op_id,
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )

    assert result.disposition is GP2PlanningDisposition.PREPARED
    assert result.success is True
    assert result.message == ""
    assert result.sealed_plan is not None
    assert isinstance(result.sealed_plan, SealedGoldenProtectionPlan)
    assert result.sealed_plan.plan.state is GoldenProtectionPlanState.PREPARED
    assert result.sealed_plan.plan.operation_id == op_id
    assert result.sealed_plan.plan.node_count == 4


# ============================================================================
# P1 a P5: Invariantes de Sellado y Determinismo
# ============================================================================


def test_p1_no_se_fabrican_seal_checks_en_true(mock_clean_golden_tree: Any) -> None:
    """P1: seal_checks no se crean hardcodeados en True sino evaluando evidencia real."""
    root, golden_verif, gp1_clean, _ = mock_clean_golden_tree

    # Si hay un reparse_tag en la evidencia que se escapó al filtro previo
    native_with_reparse = (
        _create_synthetic_native_evidence("SkyrimSE.exe", GoldenProtectionNodeKind.FILE, 101, reparse_tag=0xA000000C),
        _create_synthetic_native_evidence("Data/Skyrim.esm", GoldenProtectionNodeKind.FILE, 102),
        _create_synthetic_native_evidence("Data", GoldenProtectionNodeKind.DIR, 103, file_attributes=0x10),
        _create_synthetic_native_evidence(".", GoldenProtectionNodeKind.DIR, 100, file_attributes=0x10),
    )

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-000000000021",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_with_reparse,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert "reparse points detectados" in result.message


def test_p2_nodeset_ordenamiento_estable_bottom_up(mock_clean_golden_tree: Any) -> None:
    """P2: El plan sellado garantiza que los nodos están ordenados bottom-up con '.' al final."""
    root, golden_verif, gp1_clean, native_ev = mock_clean_golden_tree

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-000000000022",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )

    assert result.sealed_plan is not None
    plan_nodes = result.sealed_plan.plan.nodes
    # '.' debe ser estrictamente el último elemento
    assert plan_nodes[-1].relative_path == "."
    # Las hojas más profundas primero
    depths = [0 if n.relative_path == "." else n.relative_path.count("/") + 1 for n in plan_nodes]
    # Verificar que el orden de profundidad es no creciente
    assert depths == sorted(depths, reverse=True)


def test_p3_manifest_determinista_sin_espacios_redundantes(mock_clean_golden_tree: Any) -> None:
    """P3: candidate_manifest_bytes es determinista y no contiene espacios redundantes."""
    root, golden_verif, gp1_clean, native_ev = mock_clean_golden_tree

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-000000000033",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )

    assert result.sealed_plan is not None
    manifest_bytes = result.sealed_plan.candidate_manifest_bytes
    manifest_str = manifest_bytes.decode("utf-8")
    assert ": " not in manifest_str
    assert ", " not in manifest_str


def test_p4_mismo_input_produce_mismo_digest_y_bytes(mock_clean_golden_tree: Any) -> None:
    """P4: Mismos inputs incluyendo operation_id producen idénticos bytes y staging_digest."""
    root, golden_verif, gp1_clean, native_ev = mock_clean_golden_tree

    op_id = "00000000-0000-4000-8000-000000000044"

    res1 = orchestrate_golden_protection_planning(
        root,
        operation_id=op_id,
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )

    res2 = orchestrate_golden_protection_planning(
        root,
        operation_id=op_id,
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )

    assert res1.sealed_plan is not None and res2.sealed_plan is not None
    assert res1.sealed_plan.candidate_manifest_bytes == res2.sealed_plan.candidate_manifest_bytes
    assert res1.sealed_plan.staging_digest == res2.sealed_plan.staging_digest


def test_p5_drift_estructural_post_check_refusa(mock_clean_golden_tree: Any) -> None:
    """P5: Si se introduce un archivo adicional antes del sellado, el post-check lo detecta y refusa."""
    root, golden_verif, gp1_clean, native_ev = mock_clean_golden_tree

    # Simulamos drift creando un archivo nuevo no registrado en native_ev
    drift_file = root / "unregistered_intruder.dll"
    drift_file.write_bytes(b"MALICIOUS_DRIFT")

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-000000000055",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )

    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY
    assert not result.success
    assert "drift estructural detectado" in result.message


# ============================================================================
# F1 a F5: Invariantes de FSM y Modelo GP2Result
# ============================================================================


def test_f1_success_semantics_explicita() -> None:
    """F1: success=True sólo es admisible para PREPARED y ALREADY_HARDENED."""
    # PREPARED con success=False debe fallar validación
    with pytest.raises(ValueError, match="exige success=True"):
        GP2Result(disposition=GP2PlanningDisposition.PREPARED, success=False)

    # REFUSE_TO_APPLY con success=True debe fallar validación
    with pytest.raises(ValueError, match="exige success=False"):
        GP2Result(disposition=GP2PlanningDisposition.REFUSE_TO_APPLY, success=True, message="err")

    # Éxito exige mensaje vacío
    with pytest.raises(ValueError, match="exige message=''"):
        GP2Result(disposition=GP2PlanningDisposition.ALREADY_HARDENED, success=True, message="no vacio")


def test_f2_f3_prepared_exige_plan_y_refusal_no_puede_contenerlo(mock_clean_golden_tree: Any) -> None:
    """F2 y F3: PREPARED exige sealed_plan y los rechazos/no-ops prohíben sealed_plan."""
    root, golden_verif, gp1_clean, native_ev = mock_clean_golden_tree

    res_ok = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-0000000000f2",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )
    plan = res_ok.sealed_plan
    assert plan is not None

    # F2: PREPARED sin plan
    with pytest.raises(ValueError, match="Desenlace PREPARED exige sealed_plan"):
        GP2Result(disposition=GP2PlanningDisposition.PREPARED, success=True, sealed_plan=None)

    # F3: REFUSE_TO_APPLY con plan
    with pytest.raises(ValueError, match="no pueden contener un sealed_plan"):
        GP2Result(
            disposition=GP2PlanningDisposition.REFUSE_TO_APPLY,
            success=False,
            message="Rechazado",
            sealed_plan=plan,
        )


def test_f4_estados_privilegiados_posteriores_no_son_alcanzables_por_s2(mock_clean_golden_tree: Any) -> None:
    """F4: S2 jamás emite estados privilegiados posteriores como COMMITTED o MUTATING."""
    root, golden_verif, gp1_clean, native_ev = mock_clean_golden_tree

    result = orchestrate_golden_protection_planning(
        root,
        operation_id="00000000-0000-4000-8000-0000000000f4",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: native_ev,
        _probe_quiescence_fn=lambda *args, **kwargs: None,
    )

    forbidden_states = {
        "applying",
        "verifying_gp1",
        "verifying_rv2",
        "verifying_node_set",
        "archiving_backup",
        "committed",
        "rollback_required",
        "rolling_back",
        "rolled_back",
        "rollback_failed",
        "indeterminate",
    }
    assert result.disposition.value not in forbidden_states


def test_f5_no_confundir_gp1_success_con_gp2_success(mock_clean_golden_tree: Any) -> None:
    """F5: gp1_result.success=True (ej. UNPROTECTED) no otorga GP2Result.success=True si un gate posterior falla."""
    root, golden_verif, gp1_clean, _ = mock_clean_golden_tree

    assert gp1_clean.success is True  # UNPROTECTED tiene success=True en GP1

    # Forzamos fallo en evidencia nativa
    result = orchestrate_golden_protection_planning(
        root,
        operation_id="test-op-f5",
        _verify_golden_fn=lambda r: golden_verif,
        _inspect_protection_fn=lambda r: gp1_clean,
        _probe_evidence_fn=lambda r: (_ for _ in ()).throw(InventoryLinkError("Link")),
    )

    # A pesar de gp1_result.success == True, GP2Result debe ser False
    assert result.success is False
    assert result.disposition is GP2PlanningDisposition.REFUSE_TO_APPLY


# ============================================================================
# AST Anti-Mutation Guards
# ============================================================================


def test_ast_anti_mutation_guard_en_modulos_s2() -> None:
    """Verifica mediante AST que los módulos de S2 no importan ni invocan primitivas mutadoras."""
    import sky_claw.local.runtime_vault.planning_orchestrator as orch_mod
    import sky_claw.local.runtime_vault.quiescence as quiesc_mod

    s2_modules = [
        pathlib.Path(orch_mod.__file__),
        pathlib.Path(quiesc_mod.__file__),
    ]

    forbidden_identifiers = {
        "SetSecurityInfo",
        "apply_target_dacl_by_handle",
        "restore_security_descriptor_by_handle",
        "ShellExecuteExW",
        "runas",
    }

    for mod_path in s2_modules:
        tree = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_identifiers, (
                        f"Import prohibido '{alias.name}' en {mod_path.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name not in forbidden_identifiers, (
                        f"ImportFrom prohibido '{alias.name}' en {mod_path.name}"
                    )
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden_identifiers, f"Identificador prohibido '{node.id}' en {mod_path.name}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_identifiers, f"Atributo prohibido '{node.attr}' en {mod_path.name}"
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Asegurar que no se invoca "runas" como string literal
                assert node.value != "runas", f"Literal prohibido 'runas' en {mod_path.name}"
