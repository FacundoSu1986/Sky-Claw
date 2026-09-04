"""Tests del core no destructivo de RV-GP2 candidate planning.

Este slice no observa ni muta ACLs reales: prueba únicamente contratos,
precondiciones fail-closed, ABI PRE y sellado canónico. La captura Win32,
autorización privilegiada y SetSecurityInfo pertenecen a slices posteriores.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import pathlib

import pytest

from sky_claw.local.runtime_vault.golden_protection_plan import (
    DuplicateFileIdError,
    GoldenProtectionNodeKind,
    GoldenProtectionSealChecks,
    NodeSecurityBackup,
    PlanCreationError,
    SecurityBackupIntegrityError,
    prepare_golden_protection_plan,
    seal_golden_protection_plan,
)
from sky_claw.local.runtime_vault.models import (
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    RuntimeIdentity,
    RuntimeVerificationResult,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.protection import (
    GoldenProtectionEvidence,
    GoldenProtectionResult,
    GoldenProtectionRight,
    GoldenProtectionState,
    NodeProtectionObservation,
    classify_protection,
)

ROOT = pathlib.Path("G:/Modding/Skyrim_Stock_1.6.1170")
VOLUME_SERIAL = 0x1234ABCD
ROOT_FILE_ID = 100
CURRENT_USER_SID = "S-1-5-21-2000"
OTHER_OWNER_SID = "S-1-5-21-1000"
READ_ONLY = frozenset({GoldenProtectionRight.READ_DATA})


def _golden_verified() -> GoldenMasterVerificationResult:
    tree = TreeDigest(digest="a" * 64, files=1, bytes=1024)
    runtime = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
    return GoldenMasterVerificationResult(
        state=VerificationState.VERIFIED,
        tree_result=TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree,
            observed=tree,
        ),
        runtime_result=RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime,
            observed=runtime,
        ),
        descriptor=GoldenMasterDescriptor(
            location=ROOT,
            runtime_identity=runtime,
            tree_digest=tree,
        ),
    )


def _observation(
    relative_path: str,
    node_kind: str,
    rights: frozenset[GoldenProtectionRight],
    *,
    owner_sid: str = OTHER_OWNER_SID,
    owner_rights_ace_present: bool = True,
) -> NodeProtectionObservation:
    return NodeProtectionObservation(
        relative_path=relative_path,
        node_kind=node_kind,
        owner_sid=owner_sid,
        granted_rights=rights,
        owner_rights_ace_present=owner_rights_ace_present,
        dacl_inheritance_protected=True,
    )


def _gp1_result(
    *,
    platform: str = "windows",
    elevated: bool = False,
    root_rights: frozenset[GoldenProtectionRight] = READ_ONLY,
    file_rights: frozenset[GoldenProtectionRight] = frozenset(
        {GoldenProtectionRight.READ_DATA, GoldenProtectionRight.WRITE_DATA}
    ),
    parent_rights: frozenset[GoldenProtectionRight] = READ_ONLY,
    nodes: tuple[NodeProtectionObservation, ...] | None = None,
) -> GoldenProtectionResult:
    observed_nodes = nodes or (
        _observation(".", "dir", root_rights),
        _observation("Data/Skyrim.esm", "file", file_rights),
    )
    evidence = GoldenProtectionEvidence(
        platform=platform,
        drive_type=3,
        filesystem="NTFS",
        filesystem_persistent_acls=True,
        current_user_sid=CURRENT_USER_SID,
        current_group_sids=frozenset(),
        current_token_elevated=elevated,
        mutation_privileges_present=frozenset(),
        diagnostic_privileges_present=frozenset(),
        nodes=observed_nodes,
        parent_observation=_observation("..", "dir", parent_rights),
        pre_post_structural_match=True,
        observation_error=None,
    )
    return classify_protection(evidence)


def _gp1_write_protected() -> GoldenProtectionResult:
    nodes = (
        _observation(
            ".",
            "dir",
            READ_ONLY,
            owner_sid=CURRENT_USER_SID,
            owner_rights_ace_present=False,
        ),
        _observation("Data/Skyrim.esm", "file", READ_ONLY),
    )
    result = _gp1_result(nodes=nodes, file_rights=READ_ONLY)
    assert result.state is GoldenProtectionState.WRITE_PROTECTED
    return result


def _backup(
    relative_path: str,
    node_kind: GoldenProtectionNodeKind,
    file_id: int,
    *,
    sd_bytes: bytes | None = None,
    volume_serial: int = VOLUME_SERIAL,
) -> NodeSecurityBackup:
    raw = sd_bytes if sd_bytes is not None else f"sd:{relative_path}".encode()
    return NodeSecurityBackup(
        relative_path=relative_path,
        node_kind=node_kind,
        volume_serial_number=volume_serial,
        file_id=file_id,
        pre_sd_bytes_b64=base64.b64encode(raw).decode("ascii"),
        pre_sd_length=len(raw),
        pre_sd_sha256=hashlib.sha256(raw).hexdigest(),
        owner_sid=OTHER_OWNER_SID,
        group_sid="S-1-5-32-545",
        dacl_control_flags=0,
        pre_dacl_protected_flag=False,
        sddl_diagnostic="O:S-1-5-21-1000G:S-1-5-32-545D:(A;;GR;;;AU)",
    )


def _nodes() -> tuple[NodeSecurityBackup, ...]:
    # Deliberadamente desordenado: prepare debe producir manifest determinista.
    return (
        _backup("Data/Skyrim.esm", GoldenProtectionNodeKind.FILE, 101),
        _backup(".", GoldenProtectionNodeKind.DIR, ROOT_FILE_ID),
    )


def _checks(**overrides: bool) -> GoldenProtectionSealChecks:
    values = {
        "reparse_points_absent": True,
        "external_hardlinks_absent": True,
        "post_inventory_structural_match": True,
    }
    values.update(overrides)
    return GoldenProtectionSealChecks(**values)


def _prepare(
    *,
    golden: GoldenMasterVerificationResult | None = None,
    protection: GoldenProtectionResult | None = None,
    nodes: tuple[NodeSecurityBackup, ...] | None = None,
    checks: GoldenProtectionSealChecks | None = None,
    operation_id: str = "gp2-op-001",
    canonical_root: pathlib.Path = ROOT,
):
    return prepare_golden_protection_plan(
        golden_verification=golden if golden is not None else _golden_verified(),
        protection_result=protection if protection is not None else _gp1_result(),
        operation_id=operation_id,
        canonical_root=canonical_root,
        volume_serial_number=VOLUME_SERIAL,
        root_file_id=ROOT_FILE_ID,
        nodes=nodes if nodes is not None else _nodes(),
        seal_checks=checks if checks is not None else _checks(),
    )


class TestNodeSecurityBackup:
    def test_gp2_a01_backup_valido_liga_bytes_length_hash_y_flag(self) -> None:
        backup = _backup(".", GoldenProtectionNodeKind.DIR, ROOT_FILE_ID, sd_bytes=b"raw-self-relative-sd")

        assert backup.decoded_sd_bytes() == b"raw-self-relative-sd"
        assert backup.pre_sd_length == len(b"raw-self-relative-sd")
        assert backup.node_kind is GoldenProtectionNodeKind.DIR

    def test_gp2_a02_hash_adversario_falla_cerrado(self) -> None:
        raw = b"raw-self-relative-sd"
        with pytest.raises(SecurityBackupIntegrityError, match="sha256"):
            NodeSecurityBackup(
                relative_path=".",
                node_kind=GoldenProtectionNodeKind.DIR,
                volume_serial_number=VOLUME_SERIAL,
                file_id=ROOT_FILE_ID,
                pre_sd_bytes_b64=base64.b64encode(raw).decode("ascii"),
                pre_sd_length=len(raw),
                pre_sd_sha256="0" * 64,
                owner_sid=OTHER_OWNER_SID,
                group_sid="S-1-5-32-545",
                dacl_control_flags=0,
                pre_dacl_protected_flag=False,
            )

    def test_gp2_a03_flag_protected_debe_coincidir_con_control_word(self) -> None:
        raw = b"sd"
        with pytest.raises(SecurityBackupIntegrityError, match="SE_DACL_PROTECTED"):
            NodeSecurityBackup(
                relative_path=".",
                node_kind=GoldenProtectionNodeKind.DIR,
                volume_serial_number=VOLUME_SERIAL,
                file_id=ROOT_FILE_ID,
                pre_sd_bytes_b64=base64.b64encode(raw).decode("ascii"),
                pre_sd_length=len(raw),
                pre_sd_sha256=hashlib.sha256(raw).hexdigest(),
                owner_sid=OTHER_OWNER_SID,
                group_sid="S-1-5-32-545",
                dacl_control_flags=0x1000,
                pre_dacl_protected_flag=False,
            )

    @pytest.mark.parametrize("relative_path", ["../escape", "Data\\evil", "/absolute", "Data//x", "C:/escape"])
    def test_gp2_a04_relpath_no_puede_escapar_del_golden(self, relative_path: str) -> None:
        with pytest.raises(SecurityBackupIntegrityError):
            _backup(relative_path, GoldenProtectionNodeKind.FILE, 101)


class TestGoldenProtectionPlan:
    def test_gp2_a05_plan_valido_requiere_rv2_gp1_y_tres_seal_gates(self) -> None:
        plan = _prepare()

        assert plan.canonical_root == r"G:\Modding\Skyrim_Stock_1.6.1170"
        assert plan.volume_serial_number == VOLUME_SERIAL
        assert plan.root_file_id == ROOT_FILE_ID
        assert plan.node_count == 2
        assert [node.relative_path for node in plan.nodes] == [".", "Data/Skyrim.esm"]
        assert plan.initial_protection_state is GoldenProtectionState.UNPROTECTED

    def test_gp2_a06_rv2_no_verified_no_autoriza_plan(self) -> None:
        unverified = GoldenMasterVerificationResult(state=VerificationState.UNKNOWN, message="sin evidencia")

        with pytest.raises(PlanCreationError, match="RV-2 VERIFIED"):
            _prepare(golden=unverified)

    def test_gp2_a07_hardened_no_planifica_mutacion(self) -> None:
        hardened = _gp1_result(file_rights=READ_ONLY)
        assert hardened.state is GoldenProtectionState.HARDENED

        with pytest.raises(PlanCreationError, match="UNPROTECTED o WRITE_PROTECTED"):
            _prepare(protection=hardened)

    def test_gp2_a08_unsupported_no_planifica_mutacion(self) -> None:
        unsupported = _gp1_result(platform="linux")
        assert unsupported.state is GoldenProtectionState.UNSUPPORTED

        with pytest.raises(PlanCreationError, match="UNPROTECTED o WRITE_PROTECTED"):
            _prepare(protection=unsupported)

    def test_gp2_a09_write_protected_si_es_canonico_si_admite_plan(self) -> None:
        plan = _prepare(protection=_gp1_write_protected())

        assert plan.initial_protection_state is GoldenProtectionState.WRITE_PROTECTED

    def test_gp2_a10_resultado_gp1_forjado_no_puede_saltar_clasificador(self) -> None:
        forged = GoldenProtectionResult(
            state=GoldenProtectionState.UNPROTECTED,
            evidence=GoldenProtectionEvidence(platform="windows"),
        )

        with pytest.raises(PlanCreationError, match="clasificación canónica GP1"):
            _prepare(protection=forged)

    def test_gp2_a11_token_elevado_no_es_oraculo_gp1_admisible(self) -> None:
        elevated = _gp1_result(elevated=True)
        assert elevated.state is GoldenProtectionState.UNPROTECTED

        with pytest.raises(PlanCreationError, match="token efectivo no elevado"):
            _prepare(protection=elevated)

    @pytest.mark.parametrize(
        "unsafe_right",
        [
            GoldenProtectionRight.DELETE_CHILD,
            GoldenProtectionRight.CHANGE_PERMISSIONS,
            GoldenProtectionRight.CHANGE_OWNER,
        ],
    )
    def test_gp2_a12_parent_unsafe_refuse_to_apply(self, unsafe_right: GoldenProtectionRight) -> None:
        protection = _gp1_result(parent_rights=frozenset({unsafe_right}))
        assert protection.state is GoldenProtectionState.UNPROTECTED

        with pytest.raises(PlanCreationError, match="PARENT_UNSAFE"):
            _prepare(protection=protection)

    @pytest.mark.parametrize(
        "create_right",
        [GoldenProtectionRight.ADD_FILE, GoldenProtectionRight.ADD_SUBDIRECTORY],
    )
    def test_gp2_a13_rename_chain_root_delete_mas_parent_create_es_unsafe(
        self,
        create_right: GoldenProtectionRight,
    ) -> None:
        protection = _gp1_result(
            root_rights=frozenset({GoldenProtectionRight.DELETE}),
            parent_rights=frozenset({create_right}),
        )
        assert protection.state is GoldenProtectionState.UNPROTECTED

        with pytest.raises(PlanCreationError, match="rename chain"):
            _prepare(protection=protection)

    def test_gp2_a14_nodeset_debe_ser_literalmente_igual_al_observado_por_gp1(self) -> None:
        observed = (
            _observation(
                ".",
                "dir",
                frozenset({GoldenProtectionRight.ADD_FILE}),
            ),
        )
        protection = _gp1_result(nodes=observed)
        assert protection.state is GoldenProtectionState.UNPROTECTED

        with pytest.raises(PlanCreationError, match="MANIFEST_NODE_SET"):
            _prepare(protection=protection)

    def test_gp2_a15_file_id_duplicado_falla_antes_de_mutacion(self) -> None:
        duplicate = (
            _backup(".", GoldenProtectionNodeKind.DIR, ROOT_FILE_ID),
            _backup("Data/Skyrim.esm", GoldenProtectionNodeKind.FILE, ROOT_FILE_ID),
        )
        with pytest.raises(DuplicateFileIdError):
            _prepare(nodes=duplicate)

    @pytest.mark.parametrize(
        "gate",
        ["reparse_points_absent", "external_hardlinks_absent", "post_inventory_structural_match"],
    )
    def test_gp2_a16_cada_seal_gate_es_obligatorio(self, gate: str) -> None:
        with pytest.raises(PlanCreationError, match="sellado exige"):
            _prepare(checks=_checks(**{gate: False}))

    def test_gp2_a17_root_rv2_y_root_del_plan_deben_coincidir(self) -> None:
        with pytest.raises(PlanCreationError, match="canonical_root no coincide"):
            _prepare(canonical_root=pathlib.Path("G:/Otro/Golden"))

    @pytest.mark.parametrize("operation_id", ["../escape", "a/b", "a\\b", " "])
    def test_gp2_a18_operation_id_no_puede_escapar_staging(self, operation_id: str) -> None:
        with pytest.raises(PlanCreationError):
            _prepare(operation_id=operation_id)

    def test_gp2_a19_operation_id_se_canonicaliza_antes_de_sellarlo(self) -> None:
        plan = _prepare(operation_id="  gp2-op-001  ")

        assert plan.operation_id == "gp2-op-001"


class TestGoldenProtectionSeal:
    def test_gp2_a20_manifest_es_json_canonico_determinista_y_digest_ligado(self) -> None:
        first = seal_golden_protection_plan(_prepare(nodes=_nodes()))
        second = seal_golden_protection_plan(_prepare(nodes=tuple(reversed(_nodes()))))

        assert first.candidate_manifest_bytes == second.candidate_manifest_bytes
        assert first.staging_digest == second.staging_digest
        assert hashlib.sha256(first.candidate_manifest_bytes).hexdigest() == first.staging_digest
        payload = json.loads(first.candidate_manifest_bytes)
        assert payload["operation_id"] == "gp2-op-001"
        assert payload["node_count"] == 2
        assert payload["state"] == "prepared"
        assert payload["nodes"][0]["relative_path"] == "."
        assert b": " not in first.candidate_manifest_bytes
        assert b", " not in first.candidate_manifest_bytes

    def test_gp2_a21_candidate_manifest_contiene_abi_pre_completo(self) -> None:
        sealed = seal_golden_protection_plan(_prepare())
        payload = json.loads(sealed.candidate_manifest_bytes)
        node = payload["nodes"][0]

        assert {
            "relative_path",
            "node_kind",
            "VolumeSerialNumber",
            "FileId",
            "pre_sd_bytes_b64",
            "pre_sd_sha256",
            "pre_sd_length",
            "pre_dacl_protected_flag",
            "owner_sid",
            "group_sid",
            "dacl_control_flags",
            "sddl_diagnostic",
        } == set(node)


def test_gp2_a22_slice_no_contiene_primitivas_mutadoras_ni_elevacion() -> None:
    source_path = pathlib.Path("sky_claw/local/runtime_vault/golden_protection_plan.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    referenced_symbols = {child.id for child in ast.walk(tree) if isinstance(child, ast.Name)} | {
        child.attr for child in ast.walk(tree) if isinstance(child, ast.Attribute)
    }

    assert "ctypes" not in imported_roots
    assert "subprocess" not in imported_roots
    assert "SetSecurityInfo" not in referenced_symbols
    assert "ShellExecuteW" not in referenced_symbols
    assert "ShellExecuteExW" not in referenced_symbols
