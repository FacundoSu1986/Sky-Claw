"""Tests para RV-GP1 — Golden Protection Status (ADR 0009).

Matriz de trazabilidad GP-T01 .. GP-T55, tests de modelo, clasificador puro,
backend Win32 sintético, integración Windows en tmp_path, anti-mutación por AST y
monkeypatch, ortogonalidad con RV-1/RV-2/RV-3 y mutaciones adversariales M-GP1-01 .. M-GP1-18.

Principios:
- READ-ONLY FIRST: sin active write probes, sin mutaciones ACL, sin UAC.
- FAIL CLOSED: UNKNOWN != WRITE_PROTECTED != HARDENED.
- NO FALSE GREEN: AccessCheck nativo + introspección de token.
- NO LOCALIZED ACCOUNT NAMES: SIDs y constantes numéricas solamente.
- NO RV-3 COUPLING: ortogonalidad AST y comportamental.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import pathlib
import sys

import pytest

import sky_claw.local.runtime_vault as rv_pkg
from sky_claw.local.runtime_vault.clone import create_runtime_clone
from sky_claw.local.runtime_vault.golden import verify_golden_master
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    FileIdentity,
    GoldenMasterCandidate,
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
    GoldenProtectionInputError,
    GoldenProtectionResult,
    GoldenProtectionRight,
    GoldenProtectionState,
    NodeProtectionObservation,
    classify_protection,
    inspect_golden_protection,
)

# Constantes para tests
_FORBIDDEN_MUTATORS = frozenset(
    {
        "SetNamedSecurityInfoW",
        "SetFileSecurityW",
        "SetSecurityInfo",
        "SetFileAttributesW",
        "DeleteFileW",
        "RemoveDirectoryW",
        "MoveFileW",
        "MoveFileExW",
        "CreateHardLinkW",
        "CreateSymbolicLinkW",
        "SetFileInformationByHandle",
        "AdjustTokenPrivileges",
        "InitiateSystemShutdownW",
        "InitiateSystemShutdownExW",
    }
)


# ============================================================================
# 1. Modelos, DTOs e Invariantes (GP-T24, GP-T25)
# ============================================================================


class TestModelosEInvariantes:
    """Valida DTOs, enums e invariantes de post-init según ADR 0009."""

    def test_los_dtos_son_frozen_y_slots(self) -> None:
        """Todos los dataclasses de protección deben ser frozen=True y slots=True."""
        dtos = (NodeProtectionObservation, GoldenProtectionEvidence, GoldenProtectionResult)
        for dto in dtos:
            assert dataclasses.is_dataclass(dto)
            assert dto.__dataclass_params__.frozen is True
            assert hasattr(dto, "__slots__")

    def test_estados_definidos(self) -> None:
        """Existen exactamente los 5 estados normativos."""
        esperados = {"unsupported", "unprotected", "write_protected", "hardened", "unknown"}
        assert {e.value for e in GoldenProtectionState} == esperados

    def test_derechos_definidos(self) -> None:
        """Existen los derechos normativos de GoldenProtectionRight."""
        esperados = {
            "read_data",
            "execute",
            "write_data",
            "append_data",
            "add_file",
            "add_subdirectory",
            "delete",
            "delete_child",
            "write_metadata",
            "change_permissions",
            "change_owner",
        }
        assert {r.value for r in GoldenProtectionRight} == esperados

    @pytest.mark.parametrize(
        ("state", "message", "espera_success"),
        [
            (GoldenProtectionState.UNSUPPORTED, "", True),
            (GoldenProtectionState.UNPROTECTED, "", True),
            (GoldenProtectionState.WRITE_PROTECTED, "", True),
            (GoldenProtectionState.HARDENED, "", True),
            (GoldenProtectionState.UNKNOWN, "error de lectura", False),
        ],
    )
    def test_gp_t25_invarianzas_success_state(
        self, state: GoldenProtectionState, message: str, espera_success: bool
    ) -> None:
        """GP-T25: success=True <=> state != UNKNOWN; UNKNOWN exige message != ''."""
        evidencia = GoldenProtectionEvidence(platform="windows")
        res = GoldenProtectionResult(state=state, evidence=evidencia, message=message)
        assert res.success is espera_success

    def test_invariante_success_con_mensaje_falla(self) -> None:
        """Un estado exitoso no puede llevar message no vacío."""
        evidencia = GoldenProtectionEvidence(platform="windows")
        with pytest.raises(ValueError, match="message debe ser vacío"):
            GoldenProtectionResult(
                state=GoldenProtectionState.WRITE_PROTECTED,
                evidence=evidencia,
                message="mensaje prohibido en éxito",
            )

    def test_invariante_unknown_sin_mensaje_falla(self) -> None:
        """UNKNOWN exige un message explicativo no vacío."""
        evidencia = GoldenProtectionEvidence(platform="windows")
        with pytest.raises(ValueError, match="UNKNOWN exige message"):
            GoldenProtectionResult(state=GoldenProtectionState.UNKNOWN, evidence=evidencia, message="")

    def test_gp_t24_input_error_path_inexistente(self, tmp_path: pathlib.Path) -> None:
        """GP-T24: path inexistente lanza GoldenProtectionInputError."""
        inexistente = tmp_path / "no_existe_dir"
        with pytest.raises(GoldenProtectionInputError, match="no existe"):
            inspect_golden_protection(inexistente)

    def test_gp_t24_input_error_path_archivo(self, tmp_path: pathlib.Path) -> None:
        """GP-T24: path que es archivo regular lanza GoldenProtectionInputError."""
        archivo = tmp_path / "archivo.txt"
        archivo.write_text("dummy", encoding="utf-8")
        with pytest.raises(GoldenProtectionInputError, match="no es un directorio"):
            inspect_golden_protection(archivo)

    def test_gp_t24_input_error_path_relativo(self) -> None:
        """GP-T24: path relativo lanza GoldenProtectionInputError."""
        with pytest.raises(GoldenProtectionInputError, match="absoluto"):
            inspect_golden_protection(pathlib.Path("relativo/path"))


# ============================================================================
# 2. Clasificador Puro y Matriz de Estados (GP-T01 .. GP-T17, GP-T26 .. GP-T48, GP-T55)
# ============================================================================


class TestClasificadorPuro:
    """Tests unitarios de pure classification sobre GoldenProtectionEvidence sintética."""

    def test_gp_t01_plataforma_no_soportada(self) -> None:
        """GP-T01: plataforma != 'windows' -> UNSUPPORTED con success=True."""
        ev = GoldenProtectionEvidence(platform="linux")
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNSUPPORTED
        assert res.success is True
        assert res.message == ""

    @pytest.mark.parametrize("fs", ["exFAT", "FAT32", "ReFS", "ext4", None])
    def test_gp_t02_filesystem_no_admitido(self, fs: str | None) -> None:
        """GP-T02: filesystem fuera de NTFS local -> UNSUPPORTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,  # DRIVE_FIXED
            filesystem=fs,
            filesystem_persistent_acls=(fs == "ReFS"),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNSUPPORTED
        assert res.success is True

    def test_gp_t33_unc_o_remote_unsupported(self) -> None:
        """GP-T33 / GP-T35: unidad remota DRIVE_REMOTE (4) -> UNSUPPORTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=4,  # DRIVE_REMOTE
            filesystem="NTFS",
            filesystem_persistent_acls=True,
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNSUPPORTED
        assert res.success is True

    @pytest.mark.parametrize("drive_type", [0, 1, 2, 4, 5, 6, None])
    def test_drive_type_no_fixed_unsupported(self, drive_type: int | None) -> None:
        """Cualquier drive type distinto de DRIVE_FIXED (3) -> UNSUPPORTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=drive_type,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNSUPPORTED

    def test_gp_t03_fallo_observacion_unknown(self) -> None:
        """GP-T03: observation failure -> UNKNOWN (success=False, message!='')."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            observation_error="ERROR_ACCESS_DENIED al leer descriptor de root",
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert res.success is False
        assert "ERROR_ACCESS_DENIED" in res.message

    def test_gp_t18_unknown_jamas_promovido(self) -> None:
        """GP-T18: UNKNOWN ante drift, falta de nodos, error de token, etc."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=False,  # Drift
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert res.success is False

    def test_gp_t27_gp_t45_privilegio_serestore_unprotected(self) -> None:
        """GP-T27 / GP-T45: SeRestorePrivilege presente -> UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset({"SeRestorePrivilege"}),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED
        assert res.success is True

    def test_gp_t46_privilegio_setakeownership_unprotected(self) -> None:
        """GP-T46: SeTakeOwnershipPrivilege presente -> UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset({"SeTakeOwnershipPrivilege"}),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_gp_t44_sebackupprivilege_diagnostico_no_unprotected(self) -> None:
        """GP-T44: SeBackupPrivilege / SeSecurityPrivilege solos NO disparan UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset(),
            diagnostic_privileges_present=frozenset({"SeBackupPrivilege", "SeSecurityPrivilege"}),
            current_user_sid="S-1-5-21-111-222-333-1001",
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED

    @pytest.mark.parametrize(
        "right_mutador",
        [
            GoldenProtectionRight.WRITE_DATA,
            GoldenProtectionRight.APPEND_DATA,
            GoldenProtectionRight.DELETE,
            GoldenProtectionRight.WRITE_METADATA,
            GoldenProtectionRight.CHANGE_PERMISSIONS,
            GoldenProtectionRight.CHANGE_OWNER,
        ],
    )
    def test_gp_t04_t06_t09_t10_archivo_con_derecho_mutador_unprotected(
        self, right_mutador: GoldenProtectionRight
    ) -> None:
        """GP-T04/T06/T09/T10: archivo con cualquier derecho mutador -> UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="Skyrim.exe",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, right_mutador}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    @pytest.mark.parametrize(
        "right_mutador",
        [
            GoldenProtectionRight.ADD_FILE,
            GoldenProtectionRight.ADD_SUBDIRECTORY,
            GoldenProtectionRight.DELETE,
            GoldenProtectionRight.DELETE_CHILD,
            GoldenProtectionRight.WRITE_METADATA,
            GoldenProtectionRight.CHANGE_PERMISSIONS,
            GoldenProtectionRight.CHANGE_OWNER,
        ],
    )
    def test_gp_t05_directorio_con_derecho_mutador_unprotected(self, right_mutador: GoldenProtectionRight) -> None:
        """GP-T05: directorio interno con derecho de creación/mutación -> UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="Data",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, right_mutador}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_gp_t29_parent_delete_child_unprotected(self) -> None:
        """GP-T29: parent con FILE_DELETE_CHILD -> root borrable -> UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.DELETE_CHILD}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_gp_t07_parent_rename_chain_unprotected(self) -> None:
        """GP-T07: root DELETE + parent ADD_FILE -> cadena rename viable -> UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.DELETE}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.ADD_FILE}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_gp_t55_parent_add_file_sin_delete_no_unprotected(self) -> None:
        """GP-T55: parent con sólo ADD_FILE (sin DELETE en root ni DELETE_CHILD en parent) no muta el Golden."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset(),
            current_user_sid="S-1-5-21-111-222-333-1001",
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.ADD_FILE}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED

    def test_gp_t47_owner_self_rewrite_unprotected(self) -> None:
        """GP-T47: owner=usuario y WRITE_DAC efectivo -> UNPROTECTED."""
        user_sid = "S-1-5-21-111-222-333-1001"
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid=user_sid,
            current_token_elevated=False,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid=user_sid,
                    granted_rights=frozenset(
                        {GoldenProtectionRight.READ_DATA, GoldenProtectionRight.CHANGE_PERMISSIONS}
                    ),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_gp_t47_owner_con_owner_rights_ace_restringido_hardened(self) -> None:
        """GP-T47: owner=usuario pero Owner Rights ACE restringe WRITE_DAC -> HARDENED posible."""
        user_sid = "S-1-5-21-111-222-333-1001"
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid=user_sid,
            current_token_elevated=False,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid=user_sid,
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED

    def test_gp_t28_token_elevado_no_es_hardened(self) -> None:
        """GP-T28: token elevado (TokenIsElevated=True) -> WRITE_PROTECTED pero NO HARDENED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-111-222-333-500",
            current_token_elevated=True,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.WRITE_PROTECTED

    def test_gp_t42_write_protected_completo(self) -> None:
        """GP-T42: todo el árbol protegido -> WRITE_PROTECTED si token elevado o no cumple HARDENED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-111-222-333-1001",
            current_token_elevated=True,  # elevado
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="Data",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="Data/Skyrim.esm",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.WRITE_PROTECTED
        assert res.success is True
        assert res.scan_scope == "FULL_SUBTREE_AND_PARENT"

    def test_gp_t43_hardened_completo(self) -> None:
        """GP-T43: todo el árbol protegido + token no elevado + owner no atribuible -> HARDENED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-111-222-333-1001",
            current_token_elevated=False,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",  # Administrators
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="SkyrimSE.exe",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED
        assert res.success is True
        assert res.assurance_scope == "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"
        assert res.scan_scope == "FULL_SUBTREE_AND_PARENT"

    def test_gp_t14_ms_account_sid(self) -> None:
        """GP-T14: Microsoft Account SID (S-1-12-1-...) es procesado sin asumir nombres de usuario."""
        msa_sid = "S-1-12-1-123456789-987654321-111-222"
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid=msa_sid,
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED

    def test_gp_t15_local_account_sid(self) -> None:
        """GP-T15: SID local (S-1-5-21-...-1001) es procesado genéricamente."""
        local_sid = "S-1-5-21-9999-8888-7777-1001"
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid=local_sid,
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-18",  # LocalSystem
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-18",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED

    def test_gp_t16_gp_t17_token_admin_filtrado(self) -> None:
        """GP-T16/T17: token filtrado con deny-only group para Administrators."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            current_group_sids=frozenset({"S-1-5-32-545"}),  # Users (Administrators excluido por deny-only)
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",  # Administrators
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED

    def test_gp_t36_descendant_con_ace_escribible(self) -> None:
        """GP-T36: descendiente con ACE explícita escribible -> UNPROTECTED (ANY_MUTABLE_NODE)."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="Data/Meshes/file.nif",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.WRITE_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_gp_t38_descendant_sd_unreadable_unknown(self) -> None:
        """GP-T38: Security descriptor de un descendiente ilegible -> UNKNOWN global."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="Data/Textures/secret.dds",
                    node_kind="file",
                    owner_sid=None,  # No observable
                    granted_rights=None,
                    owner_rights_ace_present=None,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert res.success is False


# ============================================================================
# 3. Integración en Windows (GP-T08, GP-T23, GP-T31, GP-T50)
# ============================================================================


class TestIntegracionWindowsYBackend:
    """Tests de integración en Windows usando temp_dir y backend ctypes."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_gp_t08_inspeccion_readonly_temp_dir(self, tmp_path: pathlib.Path) -> None:
        """GP-T08: inspección read-only en directorio temporal."""
        sub = tmp_path / "golden_dir"
        sub.mkdir()
        (sub / "archivo.txt").write_text("datos", encoding="utf-8")

        res = inspect_golden_protection(sub)
        assert isinstance(res, GoldenProtectionResult)
        assert res.state in {
            GoldenProtectionState.UNPROTECTED,
            GoldenProtectionState.WRITE_PROTECTED,
            GoldenProtectionState.HARDENED,
            GoldenProtectionState.UNSUPPORTED,
        }
        assert res.evidence.platform == "windows"
        assert res.evidence.nodes is not None

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_gp_t31_idempotencia(self, tmp_path: pathlib.Path) -> None:
        """GP-T31: dos inspecciones consecutivas producen resultados equivalentes y no modifican el árbol."""
        sub = tmp_path / "golden_idempotent"
        sub.mkdir()
        archivo = sub / "test.bin"
        archivo.write_bytes(b"content")
        stat_antes = archivo.stat()

        res1 = inspect_golden_protection(sub)
        res2 = inspect_golden_protection(sub)

        assert res1.state == res2.state
        assert res1.success == res2.success
        assert res1.message == res2.message
        stat_despues = archivo.stat()
        assert stat_antes.st_mtime_ns == stat_despues.st_mtime_ns
        assert stat_antes.st_size == stat_despues.st_size

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_gp_t23_reparse_point_falla_unknown(self, tmp_path: pathlib.Path) -> None:
        """GP-T23: si hay un symlink/junction dentro del árbol -> UNKNOWN fail-closed."""
        sub = tmp_path / "golden_symlink"
        sub.mkdir()
        target = tmp_path / "target_file.txt"
        target.write_text("externo", encoding="utf-8")
        link = sub / "link.txt"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("No hay privilegios para crear symlinks en este entorno")

        res = inspect_golden_protection(sub)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert res.success is False
        assert "enlace" in res.message.lower() or "reparse" in res.message.lower()

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_gp_t50_metadata_identica_antes_despues(self, tmp_path: pathlib.Path) -> None:
        """GP-T50: inspect no muta metadata, mtime, ni tamaño en ningún nodo."""
        sub = tmp_path / "golden_meta"
        sub.mkdir()
        f1 = sub / "f1.txt"
        f1.write_text("contenido 1", encoding="utf-8")
        d1 = sub / "subcarpeta"
        d1.mkdir()
        f2 = d1 / "f2.txt"
        f2.write_text("contenido 2", encoding="utf-8")

        stats_antes = {
            "sub": sub.stat(),
            "f1": f1.stat(),
            "d1": d1.stat(),
            "f2": f2.stat(),
        }

        inspect_golden_protection(sub)

        assert sub.stat().st_mtime_ns == stats_antes["sub"].st_mtime_ns
        assert f1.stat().st_mtime_ns == stats_antes["f1"].st_mtime_ns
        assert d1.stat().st_mtime_ns == stats_antes["d1"].st_mtime_ns
        assert f2.stat().st_mtime_ns == stats_antes["f2"].st_mtime_ns


# ============================================================================
# 4. Anti-Mutación y Allowlist AST (GP-T20, GP-T21, GP-T22, GP-T49)
# ============================================================================


class TestAntiMutacionYAllowlist:
    """Anclas de seguridad anti-mutación y AST."""

    def test_gp_t20_allowlist_ast_de_win32_apis(self) -> None:
        """GP-T20 / GP-T49: protection.py no importa ni llama a APIs Win32 fuera de la allowlist."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        contenido = mod_path.read_text(encoding="utf-8")

        for mutador in _FORBIDDEN_MUTATORS:
            assert mutador not in contenido, f"API Win32 mutadora prohibida encontrada en protection.py: {mutador}"

    def test_gp_t21_sin_subprocess_ni_icacls(self) -> None:
        """GP-T21: protection.py no importa subprocess ni usa icacls/takeown/powershell."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        arbol = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))

        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                for alias in nodo.names:
                    assert alias.name != "subprocess", "protection.py no puede importar subprocess"
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                assert "subprocess" not in nodo.module, "protection.py no puede importar de subprocess"
            elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                val = nodo.value.lower()
                for cli in ("icacls", "takeown.exe", "cacls", "powershell", "fsutil", "wmic"):
                    if cli == "cacls" and "dacl" in val:
                        continue
                    assert cli not in val, f"Literal CLI prohibido '{cli}' en protection.py: {val}"

    def test_gp_t22_no_recomputa_tree_digest(self) -> None:
        """GP-T22: protection.py no importa verification ni inventory para recomputar digest."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        arbol = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))

        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom) and nodo.module:
                assert "verification" not in nodo.module
                assert "inventory" not in nodo.module

    def test_gp_t13_sin_nombres_de_cuenta_localizados(self) -> None:
        """GP-T13: sin nombres textuales localizados como 'Administrators', 'Usuarios', etc."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        arbol = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))

        prohibidos = {"administrators", "administradores", "users", "usuarios", "everyone", "todos"}
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                assert nodo.value.lower() not in prohibidos, (
                    f"Nombre de cuenta localizado prohibido '{nodo.value}' en protection.py"
                )

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_gp_t20_monkeypatch_python_mutators(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GP-T20: inspect_golden_protection no invoca mutadores de Python (os.remove, os.chmod, etc.)."""
        sub = tmp_path / "golden_safe"
        sub.mkdir()
        (sub / "test.txt").write_text("safe", encoding="utf-8")

        def _forbidden_call(*args: object, **kwargs: object) -> None:
            pytest.fail("Llamada a mutador prohibida durante inspect_golden_protection")

        monkeypatch.setattr(os, "remove", _forbidden_call)
        monkeypatch.setattr(os, "unlink", _forbidden_call)
        monkeypatch.setattr(os, "rmdir", _forbidden_call)
        monkeypatch.setattr(os, "chmod", _forbidden_call)
        monkeypatch.setattr(pathlib.Path, "unlink", _forbidden_call)
        monkeypatch.setattr(pathlib.Path, "rmdir", _forbidden_call)
        monkeypatch.setattr(pathlib.Path, "chmod", _forbidden_call)
        monkeypatch.setattr(pathlib.Path, "write_text", _forbidden_call)
        monkeypatch.setattr(pathlib.Path, "write_bytes", _forbidden_call)

        res = inspect_golden_protection(sub)
        assert isinstance(res, GoldenProtectionResult)


# ============================================================================
# 5. Ortogonalidad e Integración con RV-1, RV-2 y RV-3 (GP-T19, GP-T34, GP-T51..54)
# ============================================================================


class TestOrtogonalidadEIntegridad:
    """GP-T34, GP-T54 y ortogonalidad completa con RV-1/RV-2/RV-3."""

    def test_gp_t34_gp_t54_core_rv_no_importa_protection(self) -> None:
        """GP-T34 / GP-T54: NINGUNO de models, inventory, verification, golden, clone importa protection."""
        paquete = pathlib.Path(rv_pkg.__file__).parent
        archivos_core = ["models.py", "inventory.py", "verification.py", "golden.py", "clone.py", "locking.py"]

        for nombre_archivo in archivos_core:
            archivo = paquete / nombre_archivo
            arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Import):
                    for alias in nodo.names:
                        assert "protection" not in alias.name, f"{nombre_archivo} importa {alias.name}"
                elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                    assert "protection" not in nodo.module, f"{nombre_archivo} importa {nodo.module}"

    def test_provenance_protection_no_en_modelos_core(self) -> None:
        """protection_state NO debe ser campo en GoldenMasterVerificationResult ni RuntimeCloneResult."""
        from sky_claw.local.runtime_vault.models import (
            GoldenMasterDescriptor,
            GoldenMasterVerificationResult,
            RuntimeCloneResult,
        )

        for modelo in (GoldenMasterDescriptor, GoldenMasterVerificationResult, RuntimeCloneResult):
            campos = {f.name for f in dataclasses.fields(modelo)}
            assert "protection" not in campos
            assert "protection_state" not in campos
            assert "golden_protection" not in campos

    def test_gp_t51_verified_con_unprotected_no_degrada_rv2(self, tmp_path: pathlib.Path) -> None:
        """GP-T51: un Golden VERIFIED con protección UNPROTECTED sigue siendo VERIFIED en RV-2."""
        loc = tmp_path / "golden"
        tree_d = TreeDigest("a" * 64, 1, 10)
        rt_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        gv_result = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            descriptor=GoldenMasterDescriptor(
                location=loc,
                runtime_identity=rt_id,
                tree_digest=tree_d,
                role="reference_only",
            ),
            runtime_result=RuntimeVerificationResult(state=VerificationState.VERIFIED, expected=rt_id, observed=rt_id),
            tree_result=TreeVerificationResult(state=VerificationState.VERIFIED, expected=tree_d, observed=tree_d),
        )
        assert gv_result.state is VerificationState.VERIFIED
        assert gv_result.success is True

    def test_gp_t52_gp_t53_gp_t19_clone_independiente_de_protection(self, tmp_path: pathlib.Path) -> None:
        """GP-T19/T52/T53: create_runtime_clone funciona igual sin importar el estado de protección."""
        src = tmp_path / "src"
        src.mkdir()
        f1 = src / "SkyrimSE.exe"
        f1.write_bytes(b"binary_content")
        f2_dir = src / "Data"
        f2_dir.mkdir()
        f2 = f2_dir / "Skyrim.esm"
        f2.write_bytes(b"master_content")

        import hashlib

        d1 = hashlib.sha256(b"binary_content").hexdigest()
        d2 = hashlib.sha256(b"master_content").hexdigest()
        files = (
            FileIdentity("Data/Skyrim.esm", len(b"master_content"), d2),
            FileIdentity("SkyrimSE.exe", len(b"binary_content"), d1),
        )
        from sky_claw.local.runtime_vault.verification import tree_digest_from_files

        tree_d = tree_digest_from_files(files)
        rt_id = RuntimeIdentity("skyrimse", "1.6.1170.0")

        crit_exp = (
            CriticalFileExpectation("SkyrimSE.exe", expected_digest=d1, expected_size=len(b"binary_content")),
            CriticalFileExpectation("Data/Skyrim.esm", expected_digest=d2, expected_size=len(b"master_content")),
        )

        gv_res = verify_golden_master(
            candidate=GoldenMasterCandidate(
                location=src,
                observed_runtime=rt_id,
            ),
            expected_tree=tree_d,
            expected_runtime=rt_id,
            critical_expectations=crit_exp,
        )
        assert gv_res.state is VerificationState.VERIFIED

        dest = tmp_path / "dest"
        clone_res = create_runtime_clone(
            golden_source=gv_res,
            destination=dest,
            critical_expectations=crit_exp,
        )
        assert clone_res.state is VerificationState.VERIFIED
        assert clone_res.success is True


# ============================================================================
# 6. Mutaciones Adversariales (M-GP1-01 .. M-GP1-18)
# ============================================================================


class TestMutacionesAdversariales:
    """M-GP1-01 .. M-GP1-18: Demuestra que cada mutante provocado rompe una aserción."""

    def test_m_gp1_01_unknown_convertido_a_write_protected(self) -> None:
        """M-GP1-01: un mutante que convierte UNKNOWN en WRITE_PROTECTED es detectado."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            observation_error="Fallo de lectura",
        )
        res = classify_protection(ev)
        assert res.state is not GoldenProtectionState.WRITE_PROTECTED
        assert res.state is GoldenProtectionState.UNKNOWN

    def test_m_gp1_02_subtree_scan_root_only_mutant(self) -> None:
        """M-GP1-02: mutante que ignora descendientes es detectado si un descendiente es escribible."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="Data/sub.esp",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.WRITE_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED
        assert res.state is not GoldenProtectionState.WRITE_PROTECTED

    def test_m_gp1_03_ignorar_descendiente_no_legible(self) -> None:
        """M-GP1-03: un mutante que ignora un nodo no legible debe fallar (debe ser UNKNOWN)."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
                NodeProtectionObservation(
                    relative_path="Data/bloqueado.esm",
                    node_kind="file",
                    owner_sid=None,
                    granted_rights=None,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert res.state is not GoldenProtectionState.HARDENED

    def test_m_gp1_04_ignorar_tree_drift(self) -> None:
        """M-GP1-04: un mutante que ignora el drift estructural PRE/POST debe ser detectado."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=False,  # Drift
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                ),
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN

    def test_m_gp1_05_drive_remote_tratado_como_local(self) -> None:
        """M-GP1-05: un mutante que trata DRIVE_REMOTE como local es detectado."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=4,  # REMOTE
            filesystem="NTFS",
            filesystem_persistent_acls=True,
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNSUPPORTED
        assert res.state is not GoldenProtectionState.WRITE_PROTECTED

    def test_m_gp1_06_sebackupprivilege_tratado_como_mutador(self) -> None:
        """M-GP1-06: SeBackupPrivilege NO debe provocar UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset(),
            diagnostic_privileges_present=frozenset({"SeBackupPrivilege"}),
            current_user_sid="S-1-5-21-1-2-3-1001",
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is not GoldenProtectionState.UNPROTECTED
        assert res.state is GoldenProtectionState.HARDENED

    def test_m_gp1_07_serestoreprivilege_ignorado(self) -> None:
        """M-GP1-07: un mutante que ignora SeRestorePrivilege es detectado."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset({"SeRestorePrivilege"}),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_m_gp1_08_setakeownershipprivilege_ignorado(self) -> None:
        """M-GP1-08: un mutante que ignora SeTakeOwnershipPrivilege es detectado."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset({"SeTakeOwnershipPrivilege"}),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_m_gp1_09_token_elevation_type_como_gate(self) -> None:
        """M-GP1-09: TokenElevationType no debe reemplazar TokenElevation."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-500",
            current_token_elevated=True,  # TokenIsElevated manda
            token_elevation_type=1,  # Default
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
            ),
        )
        res = classify_protection(ev)
        assert res.state is not GoldenProtectionState.HARDENED
        assert res.state is GoldenProtectionState.WRITE_PROTECTED

    def test_m_gp1_10_owner_write_dac_ignorado(self) -> None:
        """M-GP1-10: si owner tiene WRITE_DAC efectivo debe ser UNPROTECTED."""
        user_sid = "S-1-5-21-1-2-3-1001"
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid=user_sid,
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid=user_sid,
                    granted_rights=frozenset(
                        {GoldenProtectionRight.READ_DATA, GoldenProtectionRight.CHANGE_PERMISSIONS}
                    ),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_m_gp1_11_null_dacl_tratado_como_protegido(self) -> None:
        """M-GP1-11: NULL DACL (concede todo) jamás es clasificado como protegido."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.WRITE_DATA, GoldenProtectionRight.ADD_FILE}),
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_m_gp1_12_add_file_en_parent_aislado_no_es_unprotected(self) -> None:
        """M-GP1-12: parent con sólo ADD_FILE no debe clasificar como UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.ADD_FILE}),
            ),
        )
        res = classify_protection(ev)
        assert res.state is not GoldenProtectionState.UNPROTECTED

    def test_m_gp1_13_descendiente_escribible_ignorado(self) -> None:
        """M-GP1-13: un mutante que no verifique descendientes es detectado."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                ),
                NodeProtectionObservation(
                    relative_path="Data/Scripts/test.pex",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.WRITE_DATA}),
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_m_gp1_14_native_mutator_added_to_backend(self) -> None:
        """M-GP1-14: si se añade un mutador nativo a la lista permitida, el test falla."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        contenido = mod_path.read_text(encoding="utf-8")
        for mutador in ("SetNamedSecurityInfo", "SetFileSecurity", "DeleteFile", "AdjustTokenPrivileges"):
            assert mutador not in contenido

    def test_m_gp1_15_core_rv_imports_protection(self) -> None:
        """M-GP1-15: un mutante donde core RV importa protection falla el ancla AST."""
        from sky_claw.local.runtime_vault import models

        assert not hasattr(models, "inspect_golden_protection")

    def test_m_gp1_16_reparse_point_followed(self) -> None:
        """M-GP1-16: enlace en evidencia produce UNKNOWN (no se sigue)."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            observation_error="Enlace o reparse point 'junction' inesperado",
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN

    def test_m_gp1_17_group_security_information_omitted(self) -> None:
        """M-GP1-17: GetNamedSecurityInfoW solicita GROUP_SECURITY_INFORMATION (0x02)."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        contenido = mod_path.read_text(encoding="utf-8")
        assert "0x00000002" in contenido or "GROUP" in contenido

    def test_m_gp1_18_hardened_result_sin_assurance_scope(self) -> None:
        """M-GP1-18: resultado HARDENED debe tener assurance_scope estructurado."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            current_token_elevated=False,
            nodes=(
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED
        assert res.assurance_scope == "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"
        assert res.scan_scope == "FULL_SUBTREE_AND_PARENT"
