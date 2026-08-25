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
import contextlib
import ctypes
import dataclasses
import os
import pathlib
import sys
from typing import Any

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


def _capturar_raw_security_descriptor_bytes(p: pathlib.Path) -> bytes:
    """Helper de test para capturar los bytes crudos del security descriptor (OWNER | GROUP | DACL)."""
    import ctypes.wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    sec_info = 1 | 2 | 4  # OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    needed = ctypes.wintypes.DWORD(0)
    advapi32.GetFileSecurityW(str(p), sec_info, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        raise OSError(f"No se pudo determinar tamaño de descriptor para '{p}': error {ctypes.get_last_error()}")
    buf = (ctypes.c_ubyte * needed.value)()
    if not advapi32.GetFileSecurityW(str(p), sec_info, buf, needed.value, ctypes.byref(needed)):
        raise OSError(f"GetFileSecurityW falló para '{p}': error {ctypes.get_last_error()}")
    return bytes(buf)


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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-111-222-333-1001",
            mutation_privileges_present=frozenset({"SeRestorePrivilege"}),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-111-222-333-1001",
            mutation_privileges_present=frozenset({"SeTakeOwnershipPrivilege"}),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-111-222-333-1001",
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Skyrim.exe",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, right_mutador}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-111-222-333-1001",
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Data",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, right_mutador}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-111-222-333-1001",
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.DELETE_CHILD}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-111-222-333-1001",
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.DELETE}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.ADD_FILE}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.ADD_FILE}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid=user_sid,
                    granted_rights=frozenset(
                        {GoldenProtectionRight.READ_DATA, GoldenProtectionRight.CHANGE_PERMISSIONS}
                    ),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid=user_sid,
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=True,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Data",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Data/Skyrim.esm",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",  # Administrators
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="SkyrimSE.exe",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.EXECUTE}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-18",  # LocalSystem
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-18",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",  # Administrators
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-111-222-333-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Data/Meshes/file.nif",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.WRITE_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Data/Textures/secret.dds",
                    node_kind="file",
                    owner_sid=None,  # No observable
                    granted_rights=None,
                    owner_rights_ace_present=None,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
        if res.state is not GoldenProtectionState.UNSUPPORTED:
            assert res.evidence.nodes is not None

    def test_gp_t08_unsupported_evidence_nodes_may_be_none(self) -> None:
        """GP-T08 capability gate: en estado UNSUPPORTED, evidence.nodes puede ser None."""
        ev = GoldenProtectionEvidence(
            platform="linux",  # Plataforma no soportada
            drive_type=None,
            filesystem=None,
            filesystem_persistent_acls=None,
            nodes=None,  # Ningún nodo inspeccionado
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNSUPPORTED
        assert res.success is True
        assert res.evidence.nodes is None

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
        """GP-T50: inspect no muta metadata, mtime, tamaño ni bytes crudos de Security Descriptors en ningún nodo ni parent."""
        sub = tmp_path / "golden_meta"
        sub.mkdir()
        f1 = sub / "f1.txt"
        f1.write_text("contenido 1", encoding="utf-8")
        d1 = sub / "subcarpeta"
        d1.mkdir()
        f2 = d1 / "f2.txt"
        f2.write_text("contenido 2", encoding="utf-8")

        all_paths = [tmp_path, sub, f1, d1, f2]
        stats_antes = {p: p.stat() for p in all_paths}

        # Capturar bytes crudos de Security Descriptor (OWNER | GROUP | DACL) antes de la inspección
        raw_sd_antes = {p: _capturar_raw_security_descriptor_bytes(p) for p in all_paths}

        # Ejecutar inspección
        res = inspect_golden_protection(sub)
        assert isinstance(res, GoldenProtectionResult)

        # Verificar stats después
        for p in all_paths:
            st_despues = p.stat()
            assert st_despues.st_mtime_ns == stats_antes[p].st_mtime_ns
            assert st_despues.st_size == stats_antes[p].st_size
            assert st_despues.st_mode == stats_antes[p].st_mode

        # Verificar bytes crudos de Security Descriptor después (byte por byte exacto)
        raw_sd_despues = {p: _capturar_raw_security_descriptor_bytes(p) for p in all_paths}
        for p in all_paths:
            assert raw_sd_despues[p] == raw_sd_antes[p], (
                f"Security descriptor crudo mutó byte-a-byte en '{p}' tras inspect_golden_protection"
            )

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_gp_t50_oraculo_detecta_mutacion_real(self, tmp_path: pathlib.Path) -> None:
        """GP-T50: demuestra que el oráculo de SD crudo es sensible y detecta una mutación deliberada de DACL."""
        sub = tmp_path / "golden_mut_oracle"
        sub.mkdir()
        f1 = sub / "file.txt"
        f1.write_text("data", encoding="utf-8")

        raw_sd_antes = _capturar_raw_security_descriptor_bytes(f1)

        # Mutador deliberado sólo en test
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn_convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        fn_convert_sddl.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.wintypes.ULONG),
        ]
        fn_convert_sddl.restype = ctypes.wintypes.BOOL
        fn_set_file_sec = advapi32.SetFileSecurityW
        fn_set_file_sec.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.c_void_p]
        fn_set_file_sec.restype = ctypes.wintypes.BOOL
        fn_local_free = kernel32.LocalFree

        p_sd = ctypes.c_void_p()
        fn_convert_sddl("D:P(A;;GA;;;WD)", 1, ctypes.byref(p_sd), None)
        try:
            ok = fn_set_file_sec(str(f1), 4 | 0x80000000, p_sd)
            assert ok, "Fallo al aplicar ACL mutada de prueba"
            raw_sd_mutated = _capturar_raw_security_descriptor_bytes(f1)
            # El oráculo DEBE detectar que los bytes crudos difieren
            assert raw_sd_mutated != raw_sd_antes, "El oráculo de SD crudo no detectó la mutación de DACL"
        finally:
            fn_local_free(p_sd)
            # Restaurar control total para cleanup limpio
            p_sd_restore = ctypes.c_void_p()
            fn_convert_sddl("D:P(A;OICI;GA;;;WD)", 1, ctypes.byref(p_sd_restore), None)
            fn_set_file_sec(str(f1), 4 | 0x80000000, p_sd_restore)
            fn_local_free(p_sd_restore)


# ============================================================================
# 4. Anti-Mutación y Allowlist AST (GP-T20, GP-T21, GP-T22, GP-T49)
# ============================================================================


class TestAntiMutacionYAllowlist:
    """Anclas de seguridad anti-mutación y AST."""

    _ALLOWED_WIN32_APIS = {
        "GetDriveTypeW",
        "CreateFileW",
        "GetVolumeInformationByHandleW",
        "CloseHandle",
        "LocalFree",
        "GetCurrentProcess",
        "GetCurrentThread",
        "OpenThreadToken",
        "OpenProcessToken",
        "DuplicateTokenEx",
        "GetTokenInformation",
        "LookupPrivilegeValueW",
        "ConvertSidToStringSidW",
        "GetNamedSecurityInfoW",
        "GetSecurityDescriptorControl",
        "GetSecurityDescriptorDacl",
        "GetAclInformation",
        "GetAce",
        "AccessCheck",
    }

    def test_gp_t20_positive_allowlist_ast_de_win32_apis(self) -> None:
        """GP-T20 / GP-T49: positive allowlist estricta: sólo APIs explícitamente permitidas son referenciadas."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        arbol = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))

        observed_apis: set[str] = set()
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Attribute)
                and isinstance(nodo.value, ast.Name)
                and nodo.value.id in ("_advapi32", "_kernel32")
            ):
                observed_apis.add(nodo.attr)

        extra_apis = observed_apis - self._ALLOWED_WIN32_APIS
        assert not extra_apis, f"APIs Win32 fuera de la positive allowlist detectadas: {extra_apis}"

    def test_gp_t20_allowlist_ast_de_win32_apis(self) -> None:
        """GP-T20 / GP-T49: protection.py no importa ni llama a APIs Win32 fuera de la allowlist."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        contenido = mod_path.read_text(encoding="utf-8")

        for mutador in _FORBIDDEN_MUTATORS:
            assert mutador not in contenido, f"API Win32 mutadora prohibida encontrada en protection.py: {mutador}"

    def test_gp_t20_create_file_arguments_ast_anchored(self) -> None:
        """GP-T20: CreateFileW se invoca estrictamente con desired_access=0, OPEN_EXISTING=3, FILE_FLAG_BACKUP_SEMANTICS."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        arbol = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))

        create_file_calls = []
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                func = nodo.func
                if isinstance(func, ast.Attribute) and func.attr == "CreateFileW":
                    create_file_calls.append(nodo)

        assert len(create_file_calls) == 1, "Debe haber exactamente 1 invocación de CreateFileW"
        call = create_file_calls[0]
        # Arg 1: str(path)
        # Arg 2: dwDesiredAccess (0)
        # Arg 3: dwShareMode (FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        # Arg 4: lpSecurityAttributes (None)
        # Arg 5: dwCreationDisposition (3 = OPEN_EXISTING)
        # Arg 6: dwFlagsAndAttributes (0x02000000 = FILE_FLAG_BACKUP_SEMANTICS)
        # Arg 7: hTemplateFile (None)
        desired_access_arg = call.args[1]
        assert isinstance(desired_access_arg, ast.Constant) and desired_access_arg.value == 0, (
            "CreateFileW dwDesiredAccess debe ser exactamente 0 (read-only volume query)"
        )

        creation_disp_arg = call.args[4]
        assert isinstance(creation_disp_arg, ast.Constant) and creation_disp_arg.value == 3, (
            "CreateFileW dwCreationDisposition debe ser exactamente 3 (OPEN_EXISTING)"
        )

        flags_arg = call.args[5]
        assert isinstance(flags_arg, ast.Constant) and flags_arg.value == 0x02000000, (
            "CreateFileW dwFlagsAndAttributes debe ser exactamente 0x02000000 (FILE_FLAG_BACKUP_SEMANTICS)"
        )

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
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Data/sub.esp",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.WRITE_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Data/bloqueado.esm",
                    node_kind="file",
                    owner_sid=None,
                    granted_rights=None,
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-1-2-3-1001",
            mutation_privileges_present=frozenset({"SeRestorePrivilege"}),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-1-2-3-1001",
            mutation_privileges_present=frozenset({"SeTakeOwnershipPrivilege"}),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid=user_sid,
                    granted_rights=frozenset(
                        {GoldenProtectionRight.READ_DATA, GoldenProtectionRight.CHANGE_PERMISSIONS}
                    ),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-1-2-3-1001",
            mutation_privileges_present=frozenset(),
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.WRITE_DATA, GoldenProtectionRight.ADD_FILE}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.ADD_FILE}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="Data/Scripts/test.pex",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.WRITE_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.HARDENED
        assert res.assurance_scope == "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"
        assert res.scan_scope == "FULL_SUBTREE_AND_PARENT"

    def test_m_gp1_27_current_user_sid_none_unknown(self) -> None:
        """M-GP1-27: current_user_sid ausente debe retornar UNKNOWN (fail-closed), nunca WRITE_PROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid=None,
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "token" in res.message.lower() or "sid" in res.message.lower()

    def test_m_gp1_28_empty_nodes_unknown(self) -> None:
        """M-GP1-28: nodes=() debe retornar UNKNOWN, nunca WRITE_PROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN

    def test_m_gp1_29_missing_parent_observation_unknown(self) -> None:
        """M-GP1-29: parent_observation=None debe retornar UNKNOWN (scope protegido incompleto)."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=None,
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "parent" in res.message.lower()

    def test_m_gp1_30_root_hereda_parent_write_dac_unprotected(self) -> None:
        """M-GP1-30: root hereda DACL (dacl_inheritance_protected=False) y parent tiene WRITE_DAC -> UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    dacl_inheritance_protected=False,  # Hereda del parent,
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.CHANGE_PERMISSIONS}),
                dacl_inheritance_protected=True,
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_m_gp1_31_root_hereda_parent_write_owner_unprotected(self) -> None:
        """M-GP1-31: root hereda DACL y parent tiene WRITE_OWNER -> UNPROTECTED."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    dacl_inheritance_protected=False,
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.CHANGE_OWNER}),
                dacl_inheritance_protected=True,
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_m_gp1_32_root_protegido_parent_write_dac_no_unprotected(self) -> None:
        """M-GP1-32: root protegido contra herencia (dacl_inheritance_protected=True) no se muta por parent WRITE_DAC."""
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
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    dacl_inheritance_protected=True,  # Protegido explícito,
                    owner_rights_ace_present=False,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.CHANGE_PERMISSIONS}),
                dacl_inheritance_protected=True,
                owner_rights_ace_present=False,
            ),
        )
        res = classify_protection(ev)
        assert res.state is not GoldenProtectionState.UNPROTECTED
        assert res.state is GoldenProtectionState.HARDENED

    def test_m_gp1_33_volume_info_failure_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-33: fallo en GetVolumeInformationByHandleW debe producir UNKNOWN, no UNSUPPORTED."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        monkeypatch.setattr(protection._kernel32, "GetVolumeInformationByHandleW", lambda *args: False)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert res.state is not GoldenProtectionState.UNSUPPORTED
        assert "GetVolumeInformationByHandleW" in res.message

    def test_m_gp1_34_create_file_failure_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-34: fallo en CreateFileW debe producir UNKNOWN, no UNSUPPORTED."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        monkeypatch.setattr(protection._kernel32, "CreateFileW", lambda *args: 0)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert res.state is not GoldenProtectionState.UNSUPPORTED
        assert "CreateFileW" in res.message

    def test_m_gp1_35_open_thread_token_error_non_1008_no_process_fallback(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-35: OpenThreadToken con error != 1008 no debe caer al token de proceso -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        def _mock_ott(th: Any, access: int, self_imp: bool, out_h: Any) -> bool:
            ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED
            return False

        process_token_called = False

        def _mock_opt(proc: Any, access: int, out_h: Any) -> bool:
            nonlocal process_token_called
            process_token_called = True
            return False

        monkeypatch.setattr(protection._advapi32, "OpenThreadToken", _mock_ott)
        monkeypatch.setattr(protection._advapi32, "OpenProcessToken", _mock_opt)

        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert process_token_called is False

    def test_m_gp1_36_token_user_failure_unknown(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """M-GP1-36: fallo en TokenUser -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        orig = protection._advapi32.GetTokenInformation

        def _mock_gti(h: Any, info_cls: int, buf: Any, buf_sz: int, ret_sz: Any) -> bool:
            if info_cls == 1:
                return False
            return orig(h, info_cls, buf, buf_sz, ret_sz)

        monkeypatch.setattr(protection._advapi32, "GetTokenInformation", _mock_gti)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "TokenUser" in res.message

    def test_m_gp1_37_token_groups_failure_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-37: fallo en TokenGroups -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        orig = protection._advapi32.GetTokenInformation

        def _mock_gti(h: Any, info_cls: int, buf: Any, buf_sz: int, ret_sz: Any) -> bool:
            if info_cls == 2:
                return False
            return orig(h, info_cls, buf, buf_sz, ret_sz)

        monkeypatch.setattr(protection._advapi32, "GetTokenInformation", _mock_gti)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "TokenGroups" in res.message

    def test_m_gp1_38_token_elevation_failure_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-38: fallo en TokenElevation -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        orig = protection._advapi32.GetTokenInformation

        def _mock_gti(h: Any, info_cls: int, buf: Any, buf_sz: int, ret_sz: Any) -> bool:
            if info_cls == 20:
                return False
            return orig(h, info_cls, buf, buf_sz, ret_sz)

        monkeypatch.setattr(protection._advapi32, "GetTokenInformation", _mock_gti)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "TokenElevation" in res.message

    def test_m_gp1_39_token_privileges_failure_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-39: fallo en TokenPrivileges -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        orig = protection._advapi32.GetTokenInformation

        def _mock_gti(h: Any, info_cls: int, buf: Any, buf_sz: int, ret_sz: Any) -> bool:
            if info_cls == 3:
                return False
            return orig(h, info_cls, buf, buf_sz, ret_sz)

        monkeypatch.setattr(protection._advapi32, "GetTokenInformation", _mock_gti)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "TokenPrivileges" in res.message

    def test_m_gp1_40_lookup_privilege_value_failure_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-40: fallo en LookupPrivilegeValueW -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        def _mock_lpv(sys_name: Any, priv_name: str, luid_ptr: Any) -> bool:
            return priv_name != "SeTakeOwnershipPrivilege"

        monkeypatch.setattr(protection._advapi32, "LookupPrivilegeValueW", _mock_lpv)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "LookupPrivilegeValueW" in res.message

    def test_m_gp1_41_get_security_descriptor_control_failure_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-41: fallo en GetSecurityDescriptorControl -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        monkeypatch.setattr(protection._advapi32, "GetSecurityDescriptorControl", lambda *args: False)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "GetSecurityDescriptorControl" in res.message

    def test_m_gp1_42_get_ace_middle_index_failure_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-42: fallo en GetAce en un índice intermedio -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        orig_get_ace = protection._advapi32.GetAce

        def _mock_get_ace(dacl: Any, idx: int, out_ace: Any) -> bool:
            if idx == 1:
                return False
            return orig_get_ace(dacl, idx, out_ace)

        monkeypatch.setattr(protection._advapi32, "GetAce", _mock_get_ace)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN

    def test_m_gp1_43_mutator_prohibited_api_in_ast(self) -> None:
        """M-GP1-43: si un mutador se introduce en protection.py, el AST allowlist lo detecta."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        arbol = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))
        prohibited = {"WriteFile", "SetFileSecurityW", "SetNamedSecurityInfoW", "DeleteFileW", "AdjustTokenPrivileges"}
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name):
                assert nodo.attr not in prohibited

    def test_m_gp1_44_create_file_desired_access_non_zero(self) -> None:
        """M-GP1-44: dwDesiredAccess != 0 en CreateFileW debe fallar el ancla AST."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        arbol = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "CreateFileW":
                assert isinstance(nodo.args[1], ast.Constant) and nodo.args[1].value == 0

    def test_m_gp1_45_create_file_creation_disposition_not_open_existing(self) -> None:
        """M-GP1-45: dwCreationDisposition != 3 (OPEN_EXISTING) debe fallar el ancla AST."""
        mod_path = pathlib.Path(rv_pkg.__file__).parent / "protection.py"
        arbol = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "CreateFileW":
                assert isinstance(nodo.args[4], ast.Constant) and nodo.args[4].value == 3

    def test_m_gp1_46_real_reparse_tag_pre_scan_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-46: reparse tag != 0 en PRE scan (p. ej. OneDrive Cloud 0x9000001A) -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        real_st = tmp_path.stat()

        class _MockStatWithReparse:
            st_mode = real_st.st_mode
            st_reparse_tag = 0x9000001A

            def __getattr__(self, name: str) -> Any:
                return getattr(real_st, name)

        def _mock_link(p: pathlib.Path) -> tuple[str | None, Any]:
            if p == tmp_path:
                return None, _MockStatWithReparse()
            return None, real_st

        monkeypatch.setattr(protection, "link_kind_and_identity_or_raise", _mock_link)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "0x9000001A" in res.message or "reanálisis" in res.message or "reparse" in res.message.lower()

    def test_m_gp1_47_real_reparse_tag_parent_scan_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-47: reparse tag != 0 en Parent scan -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        sub = tmp_path / "golden_sub"
        sub.mkdir()
        parent_st = tmp_path.stat()

        class _MockParentStat:
            st_mode = parent_st.st_mode
            st_reparse_tag = 0x9000001A

            def __getattr__(self, name: str) -> Any:
                return getattr(parent_st, name)

        def _mock_link(p: pathlib.Path) -> tuple[str | None, Any]:
            if p == tmp_path:
                return None, _MockParentStat()
            return None, p.stat()

        monkeypatch.setattr(protection, "link_kind_and_identity_or_raise", _mock_link)
        res = inspect_golden_protection(sub)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "0x9000001A" in res.message or "Parent" in res.message

    def test_m_gp1_48_real_reparse_tag_post_scan_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-GP1-48: reparse tag != 0 apareciendo durante POST scan genera drift estructural -> UNKNOWN."""
        if sys.platform != "win32":
            pytest.skip("Requiere Windows")
        from sky_claw.local.runtime_vault import protection

        call_count = 0
        real_st = tmp_path.stat()

        class _MockDriftStat:
            st_mode = real_st.st_mode
            st_reparse_tag = 0x9000001A

            def __getattr__(self, name: str) -> Any:
                return getattr(real_st, name)

        def _mock_link(p: pathlib.Path) -> tuple[str | None, Any]:
            nonlocal call_count
            call_count += 1
            if call_count > 2 and p == tmp_path:
                return None, _MockDriftStat()
            return None, p.stat()

        monkeypatch.setattr(protection, "link_kind_and_identity_or_raise", _mock_link)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN

    def test_m_gp1_49_root_identity_not_first_in_nodes_order_inherits_parent_unprotected(self) -> None:
        """M-GP1-49: root '.' no es el primer elemento de nodes y hereda DACL -> detecta UNPROTECTED vía parent."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path="!Extra",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=False,  # Root hereda
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.CHANGE_PERMISSIONS}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNPROTECTED

    def test_m_gp1_50_nodes_missing_root_observation_unknown(self) -> None:
        """M-GP1-50: lista de nodes sin observación de root (solo descendientes) -> UNKNOWN."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path="Data/sub.esp",
                    node_kind="file",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "root" in res.message.lower()

    def test_m_gp1_51_nodes_ambiguous_multiple_roots_unknown(self) -> None:
        """M-GP1-51: múltiples observaciones de root ambiguas ('.' y '') -> UNKNOWN."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
                NodeProtectionObservation(
                    relative_path="",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "múltiples" in res.message.lower() or "root" in res.message.lower()

    def test_m_gp1_52_node_owner_rights_ace_present_omitted_unknown(self) -> None:
        """M-GP1-52: owner_rights_ace_present omitido (None) en un nodo -> UNKNOWN."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=None,  # Omitido
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "incompleta" in res.message.lower()

    def test_m_gp1_53_node_dacl_inheritance_protected_omitted_unknown(self) -> None:
        """M-GP1-53: dacl_inheritance_protected omitido (None) en un nodo -> UNKNOWN."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=None,  # Omitido
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=True,
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "incompleta" in res.message.lower()

    def test_m_gp1_54_parent_security_evidence_omitted_unknown(self) -> None:
        """M-GP1-54: dacl_inheritance_protected omitido (None) en parent_observation -> UNKNOWN."""
        ev = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            pre_post_structural_match=True,
            current_user_sid="S-1-5-21-1-2-3-1001",
            nodes=(
                NodeProtectionObservation(
                    relative_path=".",
                    node_kind="dir",
                    owner_sid="S-1-5-32-544",
                    granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                    owner_rights_ace_present=False,
                    dacl_inheritance_protected=True,
                ),
            ),
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-32-544",
                granted_rights=frozenset({GoldenProtectionRight.READ_DATA}),
                owner_rights_ace_present=False,
                dacl_inheritance_protected=None,  # Omitido en parent
            ),
        )
        res = classify_protection(ev)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "incompleta" in res.message.lower()


# ============================================================================
# 7. Fail-Closed Detallado de Backend Win32 (Token, DACL, Lstat, Reparse)
# ============================================================================


class TestTokenInspectionFailClosed:
    """Demuestra que cualquier fallo en la cadena de inspección de token produce UNKNOWN."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_token_user_falla_retorna_unknown(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fallo en TokenUser -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        orig = protection._advapi32.GetTokenInformation

        def _mock_gti(h: Any, info_cls: int, buf: Any, buf_sz: int, ret_sz: Any) -> bool:
            if info_cls == 1:  # TokenUser
                return False
            return orig(h, info_cls, buf, buf_sz, ret_sz)

        monkeypatch.setattr(protection._advapi32, "GetTokenInformation", _mock_gti)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "TokenUser" in res.message

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_token_groups_falla_retorna_unknown(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fallo en TokenGroups -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        orig = protection._advapi32.GetTokenInformation

        def _mock_gti(h: Any, info_cls: int, buf: Any, buf_sz: int, ret_sz: Any) -> bool:
            if info_cls == 2:  # TokenGroups
                return False
            return orig(h, info_cls, buf, buf_sz, ret_sz)

        monkeypatch.setattr(protection._advapi32, "GetTokenInformation", _mock_gti)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "TokenGroups" in res.message

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_token_elevation_falla_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallo en TokenElevation -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        orig = protection._advapi32.GetTokenInformation

        def _mock_gti(h: Any, info_cls: int, buf: Any, buf_sz: int, ret_sz: Any) -> bool:
            if info_cls == 20:  # TokenElevation
                return False
            return orig(h, info_cls, buf, buf_sz, ret_sz)

        monkeypatch.setattr(protection._advapi32, "GetTokenInformation", _mock_gti)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "TokenElevation" in res.message

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_token_privileges_falla_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallo en TokenPrivileges -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        orig = protection._advapi32.GetTokenInformation

        def _mock_gti(h: Any, info_cls: int, buf: Any, buf_sz: int, ret_sz: Any) -> bool:
            if info_cls == 3:  # TokenPrivileges
                return False
            return orig(h, info_cls, buf, buf_sz, ret_sz)

        monkeypatch.setattr(protection._advapi32, "GetTokenInformation", _mock_gti)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "TokenPrivileges" in res.message

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_lookup_privilege_value_falla_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallo en LookupPrivilegeValueW -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        def _mock_lpv(sys_name: Any, priv_name: str, luid_ptr: Any) -> bool:
            return priv_name != "SeRestorePrivilege"

        monkeypatch.setattr(protection._advapi32, "LookupPrivilegeValueW", _mock_lpv)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "LookupPrivilegeValueW" in res.message

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_open_thread_token_error_distinto_1008_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenThreadToken fallando con ERROR_ACCESS_DENIED (5) no cae al token de proceso -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        def _mock_ott(th: Any, access: int, self_imp: bool, out_h: Any) -> bool:
            ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED
            return False

        monkeypatch.setattr(protection._advapi32, "OpenThreadToken", _mock_ott)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "token" in res.message.lower()


class TestDaclCallsFailClosed:
    """Demuestra que fallos en llamadas individuales de DACL producen UNKNOWN."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_get_security_descriptor_control_falla_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallo en GetSecurityDescriptorControl -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        monkeypatch.setattr(protection._advapi32, "GetSecurityDescriptorControl", lambda *args: False)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "GetSecurityDescriptorControl" in res.message

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_get_security_descriptor_dacl_falla_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallo en GetSecurityDescriptorDacl -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        monkeypatch.setattr(protection._advapi32, "GetSecurityDescriptorDacl", lambda *args: False)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "GetSecurityDescriptorDacl" in res.message

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_get_acl_information_falla_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallo en GetAclInformation -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        monkeypatch.setattr(protection._advapi32, "GetAclInformation", lambda *args: False)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "GetAclInformation" in res.message

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_get_ace_primer_indice_falla_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallo en GetAce(0) -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        def _mock_get_ace(dacl: Any, idx: int, out_ace: Any) -> bool:
            return idx != 0

        monkeypatch.setattr(protection._advapi32, "GetAce", _mock_get_ace)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "GetAce" in res.message


class TestLstatAndReparseFailClosed:
    """Demuestra que fallos de lstat o reparse tags en PRE, POST o Parent producen UNKNOWN."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_pre_lstat_oserror_retorna_unknown(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSError durante PRE-scan lstat -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        def _mock_lstat(p: pathlib.Path) -> tuple[str | None, os.stat_result | None]:
            if p == tmp_path:
                raise OSError("Fallo simulado de disco")
            return None, None

        monkeypatch.setattr(protection, "link_kind_and_identity_or_raise", _mock_lstat)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "lstat" in res.message.lower()

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_parent_lstat_oserror_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError durante Parent lstat -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        sub = tmp_path / "sub_dir"
        sub.mkdir()

        def _mock_lstat(p: pathlib.Path) -> tuple[str | None, os.stat_result | None]:
            if p == tmp_path:
                raise OSError("Fallo simulado en parent")
            return None, p.stat()

        monkeypatch.setattr(protection, "link_kind_and_identity_or_raise", _mock_lstat)
        res = inspect_golden_protection(sub)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "parent" in res.message.lower() or "lstat" in res.message.lower()

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_reparse_tag_inesperado_pre_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reparse point inesperado (tag 0x9000001A) detectado en PRE -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        real_st = tmp_path.stat()

        class _MockReparseStat:
            st_mode = real_st.st_mode
            st_reparse_tag = 0x9000001A

            def __getattr__(self, name: str) -> Any:
                return getattr(real_st, name)

        def _mock_link(p: pathlib.Path) -> tuple[str | None, Any]:
            if p == tmp_path:
                return None, _MockReparseStat()
            return None, real_st

        monkeypatch.setattr(protection, "link_kind_and_identity_or_raise", _mock_link)
        res = inspect_golden_protection(tmp_path)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "0x9000001A" in res.message or "reanálisis" in res.message or "reparse" in res.message.lower()

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_reparse_tag_inesperado_parent_retorna_unknown(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reparse point inesperado (tag 0x9000001A) detectado en Parent -> UNKNOWN."""
        from sky_claw.local.runtime_vault import protection

        sub = tmp_path / "sub_dir"
        sub.mkdir()
        parent_st = tmp_path.stat()

        class _MockParentReparseStat:
            st_mode = parent_st.st_mode
            st_reparse_tag = 0x9000001A

            def __getattr__(self, name: str) -> Any:
                return getattr(parent_st, name)

        def _mock_link(p: pathlib.Path) -> tuple[str | None, Any]:
            if p == tmp_path:
                return None, _MockParentReparseStat()
            return None, p.stat()

        monkeypatch.setattr(protection, "link_kind_and_identity_or_raise", _mock_link)
        res = inspect_golden_protection(sub)
        assert res.state is GoldenProtectionState.UNKNOWN
        assert "0x9000001A" in res.message or "Parent" in res.message


class TestNativeWindowsAclOracles:
    """Oráculos nativos en Windows con DACLs controladas determinísticamente en tmp_path.

    Escenarios requeridos:
    A. DACL explícita con permisos de escritura -> exact UNPROTECTED
    B. Parent explícito con DELETE_CHILD -> exact UNPROTECTED
    C. NULL DACL (acceso total incondicional) -> exact UNPROTECTED
    D. Token / Owner con WRITE_DAC efectivo -> exact UNPROTECTED
    E. DACL restrictiva protegida de herencia -> evidencia exacta; WRITE_PROTECTED/HARDENED
    """

    @staticmethod
    def _set_sddl_dacl(path: pathlib.Path, sddl: str) -> None:
        """Aplica una DACL especificada por SDDL a un objeto del sistema de archivos."""
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn_convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        fn_convert_sddl.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.wintypes.ULONG),
        ]
        fn_convert_sddl.restype = ctypes.wintypes.BOOL
        fn_set_file_sec = advapi32.SetFileSecurityW
        fn_set_file_sec.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.c_void_p]
        fn_set_file_sec.restype = ctypes.wintypes.BOOL
        fn_local_free = kernel32.LocalFree

        p_sd = ctypes.c_void_p()
        if not fn_convert_sddl(sddl, 1, ctypes.byref(p_sd), None):
            raise OSError(
                f"ConvertStringSecurityDescriptorToSecurityDescriptorW falló para '{sddl}': error {ctypes.get_last_error()}"
            )
        try:
            # DACL_SECURITY_INFORMATION (4) | PROTECTED_DACL_SECURITY_INFORMATION (0x80000000)
            if not fn_set_file_sec(str(path), 4 | 0x80000000, p_sd):
                raise OSError(f"SetFileSecurityW falló para '{path}': error {ctypes.get_last_error()}")
        finally:
            fn_local_free(p_sd)

    @staticmethod
    def _set_null_dacl(path: pathlib.Path) -> None:
        """Aplica una NULL DACL explícita (acceso incondicional sin restricciones)."""
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        fn_init_sd = advapi32.InitializeSecurityDescriptor
        fn_init_sd.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
        fn_init_sd.restype = ctypes.wintypes.BOOL
        fn_set_dacl = advapi32.SetSecurityDescriptorDacl
        fn_set_dacl.argtypes = [
            ctypes.wintypes.LPVOID,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.LPVOID,
            ctypes.wintypes.BOOL,
        ]
        fn_set_dacl.restype = ctypes.wintypes.BOOL
        fn_set_file_sec = advapi32.SetFileSecurityW
        fn_set_file_sec.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.c_void_p]
        fn_set_file_sec.restype = ctypes.wintypes.BOOL

        sd_buf = (ctypes.c_ubyte * 64)()
        p_sd = ctypes.cast(sd_buf, ctypes.c_void_p)
        fn_init_sd(p_sd, 1)  # SECURITY_DESCRIPTOR_REVISION = 1
        # NULL DACL: bDaclPresent=True, pDacl=None, bDaclDefaulted=False
        fn_set_dacl(p_sd, True, None, False)
        if not fn_set_file_sec(str(path), 4 | 0x80000000, p_sd):
            raise OSError(f"SetFileSecurityW falló al setear NULL DACL en '{path}': error {ctypes.get_last_error()}")

    @staticmethod
    def _restore_full_control(paths: list[pathlib.Path]) -> None:
        """Restaura control total a todos los paths para permitir la limpieza del fixture temporal."""
        for p in paths:
            with contextlib.suppress(Exception):
                TestNativeWindowsAclOracles._set_sddl_dacl(p, "D:P(A;OICI;GA;;;WD)")

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_oracle_controlled_explicit_writable_dacl_unprotected(self, tmp_path: pathlib.Path) -> None:
        """Escenario A: DACL explícita con permisos mutadores (GA) -> exact UNPROTECTED."""
        sub = tmp_path / "oracle_writable_a"
        sub.mkdir()
        f = sub / "test.txt"
        f.write_text("data", encoding="utf-8")
        paths = [sub, f]

        try:
            for p in paths:
                self._set_sddl_dacl(p, "D:P(A;OICI;GA;;;WD)")

            res = inspect_golden_protection(sub)
            assert res.state is GoldenProtectionState.UNPROTECTED
            assert res.success is True
            assert res.evidence.nodes is not None
            # Validar que efectivamente hay derechos de escritura concedidos
            any_writable = any(
                GoldenProtectionRight.WRITE_DATA in (n.granted_rights or set())
                or GoldenProtectionRight.ADD_FILE in (n.granted_rights or set())
                for n in res.evidence.nodes
            )
            assert any_writable is True
        finally:
            self._restore_full_control(paths)

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_oracle_controlled_explicit_parent_delete_child_unprotected(self, tmp_path: pathlib.Path) -> None:
        """Escenario B: parent con permiso explícito DELETE_CHILD -> exact UNPROTECTED."""
        parent_dir = tmp_path / "parent_b"
        parent_dir.mkdir()
        sub = parent_dir / "golden_b"
        sub.mkdir()
        f = sub / "test.txt"
        f.write_text("data", encoding="utf-8")
        paths = [parent_dir, sub, f]

        try:
            # Parent tiene Read + FILE_DELETE_CHILD (0x40): 0x1200A9 | 0x40 = 0x1200E9
            self._set_sddl_dacl(parent_dir, "D:P(A;OICI;0x1200E9;;;WD)")
            # Sub y archivo tienen DACL de solo lectura protegida: 0x1200A9
            self._set_sddl_dacl(sub, "D:P(A;OICI;0x1200A9;;;WD)")
            self._set_sddl_dacl(f, "D:P(A;;0x1200A9;;;WD)")

            res = inspect_golden_protection(sub)
            assert res.state is GoldenProtectionState.UNPROTECTED
            assert res.success is True
            assert res.evidence.parent_observation is not None
            assert GoldenProtectionRight.DELETE_CHILD in (res.evidence.parent_observation.granted_rights or set())
        finally:
            self._restore_full_control(paths)

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_oracle_controlled_null_dacl_unprotected(self, tmp_path: pathlib.Path) -> None:
        """Escenario C: NULL DACL explícita (acceso incondicional para todos) -> exact UNPROTECTED."""
        sub = tmp_path / "oracle_null_c"
        sub.mkdir()
        f = sub / "test.txt"
        f.write_text("data", encoding="utf-8")
        paths = [sub, f]

        try:
            for p in paths:
                self._set_null_dacl(p)

            res = inspect_golden_protection(sub)
            assert res.state is GoldenProtectionState.UNPROTECTED
            assert res.success is True
            assert res.evidence.nodes is not None
            # En NULL DACL se conceden todos los derechos
            any_writable = any(
                GoldenProtectionRight.WRITE_DATA in (n.granted_rights or set()) for n in res.evidence.nodes
            )
            assert any_writable is True
        finally:
            self._restore_full_control(paths)

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_oracle_controlled_owner_token_write_dac_unprotected(self, tmp_path: pathlib.Path) -> None:
        """Escenario D: objeto con WRITE_DAC (0x40000) concedido al usuario -> exact UNPROTECTED."""
        parent_dir = tmp_path / "parent_d"
        parent_dir.mkdir()
        sub = parent_dir / "golden_d"
        sub.mkdir()
        f = sub / "test.txt"
        f.write_text("data", encoding="utf-8")
        paths = [parent_dir, sub, f]

        try:
            # Parent solo lectura
            self._set_sddl_dacl(parent_dir, "D:P(A;OICI;0x1200A9;;;WD)")
            # Sub tiene Read + WRITE_DAC (0x40000): 0x1200A9 | 0x40000 = 0x1600A9
            self._set_sddl_dacl(sub, "D:P(A;OICI;0x1600A9;;;WD)")
            self._set_sddl_dacl(f, "D:P(A;;0x1200A9;;;WD)")

            res = inspect_golden_protection(sub)
            assert res.state is GoldenProtectionState.UNPROTECTED
            assert res.success is True
            # Debe registrar CHANGE_PERMISSIONS (WRITE_DAC)
            root_node = next((n for n in (res.evidence.nodes or ()) if n.relative_path == "."), None)
            assert root_node is not None
            assert GoldenProtectionRight.CHANGE_PERMISSIONS in (root_node.granted_rights or set())
        finally:
            self._restore_full_control(paths)

    @pytest.mark.skipif(sys.platform != "win32", reason="Requiere Windows")
    def test_oracle_controlled_restrictive_dacl_protected_inheritance(self, tmp_path: pathlib.Path) -> None:
        """Escenario E: DACL restrictiva protegida de herencia con OWNER_RIGHTS read-only -> WRITE_PROTECTED/HARDENED."""
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn_convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        fn_convert_sddl.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.wintypes.ULONG),
        ]
        fn_convert_sddl.restype = ctypes.wintypes.BOOL
        fn_set_kernel_sec = advapi32.SetKernelObjectSecurity
        fn_set_kernel_sec.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.c_void_p]
        fn_set_kernel_sec.restype = ctypes.wintypes.BOOL
        fn_create_file = kernel32.CreateFileW
        fn_create_file.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.HANDLE,
        ]
        fn_create_file.restype = ctypes.wintypes.HANDLE
        fn_close_handle = kernel32.CloseHandle
        fn_close_handle.argtypes = [ctypes.wintypes.HANDLE]
        fn_local_free = kernel32.LocalFree

        parent_dir = tmp_path / "parent_e"
        parent_dir.mkdir()
        sub = parent_dir / "golden_e"
        sub.mkdir()
        f = sub / "test.txt"
        f.write_text("data", encoding="utf-8")
        paths = [parent_dir, sub, f]

        # Abrir handles con WRITE_DAC (0x40000) ANTES de restringir los derechos del token/owner
        handles = {}
        for p in paths:
            h = fn_create_file(str(p), 0x40000, 7, None, 3, 0x02000000, None)
            assert h != -1 and h != 0, f"No se pudo abrir handle WRITE_DAC para '{p}'"
            handles[p] = h

        try:
            # Aplicar DACL restrictiva protegida con OWNER_RIGHTS read-only (0x1200A9)
            sddl = "D:P(A;OICI;0x1200A9;;;WD)(A;OICI;0x1200A9;;;OW)"
            p_sd = ctypes.c_void_p()
            if not fn_convert_sddl(sddl, 1, ctypes.byref(p_sd), None):
                raise OSError(f"ConvertStringSecurityDescriptorToSecurityDescriptorW falló: {ctypes.get_last_error()}")
            for p in paths:
                ok = fn_set_kernel_sec(handles[p], 4 | 0x80000000, p_sd)
                assert ok, f"SetKernelObjectSecurity falló para '{p}'"
            fn_local_free(p_sd)

            res = inspect_golden_protection(sub)
            assert res.success is True
            assert res.state in {GoldenProtectionState.WRITE_PROTECTED, GoldenProtectionState.HARDENED}

            # Demostración explícita de prerrequisitos de HARDENED:
            # Si el token no está elevado y no tiene privilegios de mutación, el estado es HARDENED
            if res.evidence.current_token_elevated is False and not res.evidence.mutation_privileges_present:
                assert res.state is GoldenProtectionState.HARDENED
            else:
                assert res.state is GoldenProtectionState.WRITE_PROTECTED

            # Verificar que ningún nodo en evidence tiene derechos de escritura
            for n in res.evidence.nodes or ():
                rights = n.granted_rights or set()
                assert GoldenProtectionRight.WRITE_DATA not in rights
                assert GoldenProtectionRight.ADD_FILE not in rights
                assert GoldenProtectionRight.APPEND_DATA not in rights
                assert GoldenProtectionRight.DELETE not in rights
                assert GoldenProtectionRight.CHANGE_PERMISSIONS not in rights
                assert GoldenProtectionRight.CHANGE_OWNER not in rights
        finally:
            # Restaurar control total a través de los handles abiertos con WRITE_DAC
            p_sd_full = ctypes.c_void_p()
            fn_convert_sddl("D:P(A;OICI;GA;;;WD)", 1, ctypes.byref(p_sd_full), None)
            for p in paths:
                fn_set_kernel_sec(handles[p], 4 | 0x80000000, p_sd_full)
                fn_close_handle(handles[p])
            fn_local_free(p_sd_full)
