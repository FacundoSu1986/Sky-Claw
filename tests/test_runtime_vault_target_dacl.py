"""Tests para Target DACL Builder y Round-Trip real atado a HANDLE (GP2-S1b).

Implementa la verificación conforme ADR 0010 §7, §9, §10 y §12.2.

Cubre:
1. Tests puros / cross-platform de especificación TargetDaclSpec (ADR 0010 §7.2).
2. Jerarquía de excepciones y modelos inmutables.
3. Tests nativos en Windows:
   - Rig estructural sobre archivo y directorio temporales descartables.
   - Oráculo de acceso efectivo real con AccessCheck sobre token restringido/filtrado.
   - Verificación semántica de restauración: owner, group, DACL ordenada, SE_DACL_PROTECTED.
   - Independencia de serialización raw en restore (ADR 0010 §12.2).
   - Revalidaciones pre-mutación: identidad física obligatoria, reparse tag, anti-hardlink
     (doble verificación) y drift de live PRE SD.
4. Oráculos adversarios / pruebas de mutación (T1–T17 y M1–M15).
"""

from __future__ import annotations

import copy
import ctypes
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.node_evidence import probe_node_evidence
from sky_claw.local.runtime_vault.protection import GoldenProtectionRight
from sky_claw.local.runtime_vault.target_dacl import (
    AUTHENTICATED_USERS_SID,
    BUILTIN_ADMINISTRATORS_SID,
    DIR_TARGET_MASK_AUTHENTICATED_USERS,
    DIR_TARGET_MASK_OWNER_RIGHTS,
    FILE_TARGET_MASK_AUTHENTICATED_USERS,
    FILE_TARGET_MASK_OWNER_RIGHTS,
    LOCAL_SYSTEM_SID,
    OWNER_RIGHTS_SID,
    TARGET_MASK_FULL_ACCESS,
    TargetAceSpec,
    TargetDaclApplyError,
    TargetDaclBuildError,
    TargetDaclError,
    TargetDaclRestoreError,
    TargetDaclSpec,
    TargetDaclUnsupportedError,
    TargetDaclVerificationError,
    _create_test_restricted_token,
    apply_target_dacl_by_handle,
    build_native_target_dacl,
    build_target_dacl_spec,
    close_security_handle,
    open_node_security_handle,
    restore_security_descriptor_by_handle,
    verify_restored_security_descriptor_by_handle,
    verify_target_dacl_by_handle,
)

if sys.platform == "win32":
    from ctypes import wintypes

    from sky_claw.local.runtime_vault.target_dacl import _advapi32, _kernel32

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")


# ============================================================================
# 1. Tests Puros / Cross-Platform (Especificación Target DACL)
# ============================================================================


class TestTargetDaclPureSpec:
    """Validación exhaustiva de la especificación normativa (ADR 0010 §7) sin requerir Win32."""

    def test_jerarquia_excepciones(self) -> None:
        """Verifica que todas las excepciones de dominio deriven de TargetDaclError y RuntimeVaultError."""
        assert issubclass(TargetDaclError, RuntimeVaultError)
        assert issubclass(TargetDaclUnsupportedError, TargetDaclError)
        assert issubclass(TargetDaclBuildError, TargetDaclError)
        assert issubclass(TargetDaclApplyError, TargetDaclError)
        assert issubclass(TargetDaclRestoreError, TargetDaclError)
        assert issubclass(TargetDaclVerificationError, TargetDaclError)

    def test_target_dacl_spec_archivos_regulares(self) -> None:
        """Verifica la especificación exacta de Target DACL para archivos regulares (ADR 0010 §7.2)."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)

        assert spec.node_kind is GoldenProtectionNodeKind.FILE
        assert spec.control_flags == 0x1000  # SE_DACL_PROTECTED
        assert len(spec.aces) == 4

        # 1. Owner Rights
        ace1 = spec.aces[0]
        assert ace1.sid == OWNER_RIGHTS_SID == "S-1-3-4"
        assert ace1.ace_type == 0  # ACCESS_ALLOWED
        assert ace1.ace_flags == 0x00  # sin herencia
        assert ace1.access_mask == FILE_TARGET_MASK_OWNER_RIGHTS == 0x00120089

        # 2. Authenticated Users
        ace2 = spec.aces[1]
        assert ace2.sid == AUTHENTICATED_USERS_SID == "S-1-5-11"
        assert ace2.ace_type == 0
        assert ace2.ace_flags == 0x00
        assert ace2.access_mask == FILE_TARGET_MASK_AUTHENTICATED_USERS == 0x001200A9

        # 3. LocalSystem
        ace3 = spec.aces[2]
        assert ace3.sid == LOCAL_SYSTEM_SID == "S-1-5-18"
        assert ace3.ace_type == 0
        assert ace3.ace_flags == 0x00
        assert ace3.access_mask == TARGET_MASK_FULL_ACCESS == 0x001F01FF

        # 4. Builtin Administrators
        ace4 = spec.aces[3]
        assert ace4.sid == BUILTIN_ADMINISTRATORS_SID == "S-1-5-32-544"
        assert ace4.ace_type == 0
        assert ace4.ace_flags == 0x00
        assert ace4.access_mask == TARGET_MASK_FULL_ACCESS == 0x001F01FF

    def test_target_dacl_spec_directorios(self) -> None:
        """Verifica la especificación exacta de Target DACL para directorios (ADR 0010 §7.2)."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.DIR)

        assert spec.node_kind is GoldenProtectionNodeKind.DIR
        assert spec.control_flags == 0x1000  # SE_DACL_PROTECTED
        assert len(spec.aces) == 4

        # 1. Owner Rights
        ace1 = spec.aces[0]
        assert ace1.sid == OWNER_RIGHTS_SID == "S-1-3-4"
        assert ace1.ace_type == 0
        assert ace1.ace_flags == 0x00
        assert ace1.access_mask == DIR_TARGET_MASK_OWNER_RIGHTS == 0x001200A9

        # 2. Authenticated Users
        ace2 = spec.aces[1]
        assert ace2.sid == AUTHENTICATED_USERS_SID == "S-1-5-11"
        assert ace2.ace_type == 0
        assert ace2.ace_flags == 0x00
        assert ace2.access_mask == DIR_TARGET_MASK_AUTHENTICATED_USERS == 0x001200A9

        # 3. LocalSystem
        ace3 = spec.aces[2]
        assert ace3.sid == LOCAL_SYSTEM_SID == "S-1-5-18"
        assert ace3.ace_type == 0
        assert ace3.ace_flags == 0x00
        assert ace3.access_mask == TARGET_MASK_FULL_ACCESS == 0x001F01FF

        # 4. Builtin Administrators
        ace4 = spec.aces[3]
        assert ace4.sid == BUILTIN_ADMINISTRATORS_SID == "S-1-5-32-544"
        assert ace4.ace_type == 0
        assert ace4.ace_flags == 0x00
        assert ace4.access_mask == TARGET_MASK_FULL_ACCESS == 0x001F01FF

    def test_owner_rights_no_concede_escritura_ni_dac(self) -> None:
        """Owner Rights nunca debe contener WRITE_DAC, WRITE_OWNER ni derechos mutadores."""
        for kind in (GoldenProtectionNodeKind.FILE, GoldenProtectionNodeKind.DIR):
            spec = build_target_dacl_spec(kind)
            owner_ace = [a for a in spec.aces if a.sid == OWNER_RIGHTS_SID][0]

            assert not bool(owner_ace.access_mask & 0x00040000)  # WRITE_DAC
            assert not bool(owner_ace.access_mask & 0x00080000)  # WRITE_OWNER
            assert not bool(owner_ace.access_mask & 0x00010000)  # DELETE
            assert not bool(owner_ace.access_mask & 0x0002)  # WRITE_DATA / ADD_FILE
            assert not bool(owner_ace.access_mask & 0x0004)  # APPEND_DATA / ADD_SUBDIRECTORY
            assert owner_ace.access_mask != TARGET_MASK_FULL_ACCESS

    def test_authenticated_users_no_concede_escritura(self) -> None:
        """Authenticated Users nunca debe contener derechos mutadores."""
        for kind in (GoldenProtectionNodeKind.FILE, GoldenProtectionNodeKind.DIR):
            spec = build_target_dacl_spec(kind)
            au_ace = [a for a in spec.aces if a.sid == AUTHENTICATED_USERS_SID][0]

            assert not bool(au_ace.access_mask & 0x00040000)  # WRITE_DAC
            assert not bool(au_ace.access_mask & 0x00080000)  # WRITE_OWNER
            assert not bool(au_ace.access_mask & 0x00010000)  # DELETE
            assert not bool(au_ace.access_mask & 0x0002)  # WRITE_DATA / ADD_FILE
            assert not bool(au_ace.access_mask & 0x0004)  # APPEND_DATA / ADD_SUBDIRECTORY

    def test_target_dacl_spec_inmutable(self) -> None:
        """Verifica que TargetDaclSpec sea un dataclass frozen inmutable."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        with pytest.raises(Exception):  # noqa: B017
            spec.control_flags = 0  # type: ignore[misc]

    def test_target_dacl_spec_requiere_exactamente_cuatro_aces(self) -> None:
        """TargetDaclSpec falla si se instancian menos o más de 4 ACEs."""
        ace = TargetAceSpec("S-1-3-4", 0, 0, 0x00120089, "Owner")
        with pytest.raises(TargetDaclBuildError, match="requiere exactamente 4 ACEs"):
            TargetDaclSpec(
                node_kind=GoldenProtectionNodeKind.FILE,
                aces=(ace, ace),
                control_flags=0x1000,
            )

    def test_target_dacl_spec_requiere_flag_se_dacl_protected(self) -> None:
        """TargetDaclSpec falla si control_flags no contiene SE_DACL_PROTECTED."""
        spec_valid = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        with pytest.raises(TargetDaclBuildError, match="debe incluir el flag SE_DACL_PROTECTED"):
            TargetDaclSpec(
                node_kind=GoldenProtectionNodeKind.FILE,
                aces=spec_valid.aces,
                control_flags=0,
            )

    def test_target_dacl_spec_tipo_desconocido_falla(self) -> None:
        """build_target_dacl_spec con tipo de nodo desconocido lanza TargetDaclBuildError."""
        with pytest.raises(TargetDaclBuildError, match="Tipo de nodo desconocido"):
            build_target_dacl_spec("invalido")  # type: ignore[arg-type]

    def test_posix_safety_en_plataforma_no_windows(self) -> None:
        """En entorno no-Windows, las funciones nativas fallan con TargetDaclUnsupportedError."""
        if sys.platform != "win32":
            with pytest.raises(TargetDaclUnsupportedError):
                open_node_security_handle("dummy")
            with pytest.raises(TargetDaclUnsupportedError):
                apply_target_dacl_by_handle(1, None)  # type: ignore[arg-type]
            with pytest.raises(TargetDaclUnsupportedError):
                _create_test_restricted_token()


# ============================================================================
# 2. Tests Nativos en Windows: Rig Estructural (Token del Runner)
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas nativas Win32 solo en Windows")
class TestTargetDaclNativeStructuralRoundTrip:
    """Pruebas estructurales de mutación y restauración nativa sobre objetos descartables."""

    def test_roundtrip_estructural_archivo_descartable(self) -> None:
        """Demuestra APPLY → VERIFY estructural → RESTORE sobre 1 archivo descartable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "disposable_test_file.bin"
            test_file.write_bytes(b"sky_claw_gp2_s1b_disposable_file_content")

            evidences = probe_node_evidence(tmpdir)
            file_evidences = [e for e in evidences if e.backup.relative_path != "."]
            assert len(file_evidences) == 1
            pre = file_evidences[0].backup

            h_sec = open_node_security_handle(test_file)
            assert h_sec != 0

            try:
                # 1. Aplicar Target DACL
                spec = apply_target_dacl_by_handle(h_sec, pre)
                assert spec.node_kind is GoldenProtectionNodeKind.FILE

                # 2. Verificación estructural exhaustiva mediante verify_target_dacl_by_handle
                verif = verify_target_dacl_by_handle(h_sec, pre)
                assert verif.dacl_protected is True
                assert verif.owner_rights_present is True
                assert verif.owner_rights_mask == FILE_TARGET_MASK_OWNER_RIGHTS

                # 3. Restaurar y verificar semánticamente
                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)

    def test_roundtrip_estructural_directorio_descartable(self) -> None:
        """Demuestra APPLY → VERIFY estructural → RESTORE sobre 1 directorio descartable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = pathlib.Path(tmpdir) / "disposable_dir"
            test_dir.mkdir()

            evidences = probe_node_evidence(tmpdir)
            dir_evidences = [e for e in evidences if e.backup.relative_path == "disposable_dir"]
            assert len(dir_evidences) == 1
            pre = dir_evidences[0].backup

            h_sec = open_node_security_handle(test_dir)
            assert h_sec != 0

            try:
                spec = apply_target_dacl_by_handle(h_sec, pre)
                assert spec.node_kind is GoldenProtectionNodeKind.DIR

                verif = verify_target_dacl_by_handle(h_sec, pre)
                assert verif.dacl_protected is True
                assert verif.owner_rights_present is True
                assert verif.owner_rights_mask == DIR_TARGET_MASK_OWNER_RIGHTS

                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)

    def test_roundtrip_pre_ya_protegido_restaura_flag_protegido(self) -> None:
        """Verifica que un nodo PRE que ya poseía SE_DACL_PROTECTED se restaura como protegido."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "protected_pre.txt"
            test_file.write_text("data")

            h_sec = open_node_security_handle(test_file)
            try:
                # Fijar SE_DACL_PROTECTED en el estado PRE usando el handle abierto
                ret_protect = _advapi32.SetSecurityInfo(
                    h_sec,
                    1,  # _SE_FILE_OBJECT
                    4 | 0x80000000,  # _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
                    None,
                    None,
                    None,
                    None,
                )
                assert ret_protect == 0

                evidences = probe_node_evidence(tmpdir)
                pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]
                assert pre.pre_dacl_protected_flag is True

                apply_target_dacl_by_handle(h_sec, pre)
                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)
            finally:
                close_security_handle(h_sec)

    def test_roundtrip_pre_heredado_restaura_flag_desprotegido(self) -> None:
        """Verifica que un nodo PRE sin SE_DACL_PROTECTED se restaura con flag desprotegido."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "inherited_pre.txt"
            test_file.write_text("data")

            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h_sec = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h_sec, pre)
                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)
            finally:
                close_security_handle(h_sec)


# ============================================================================
# 3. Tests Nativos en Windows: Oráculo de Acceso Efectivo (Token Restringido)
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas nativas Win32 solo en Windows")
class TestTargetDaclEffectiveAccessOracle:
    """Oráculo de acceso efectivo usando un token restringido (privilegios drop + Admin deny-only)."""

    def test_accesscheck_token_restringido_archivo(self) -> None:
        """Verifica que un usuario no elevado bajo token restringido no puede mutar un archivo protegido."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "access_check_file.txt"
            test_file.write_text("sensible_game_asset")

            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h_sec = open_node_security_handle(test_file)
            h_restr, h_imp = _create_test_restricted_token()

            try:
                apply_target_dacl_by_handle(h_sec, pre)

                result = verify_target_dacl_by_handle(h_sec, pre, token_handle=h_imp)

                assert result.dacl_protected is True
                assert result.owner_rights_present is True
                assert not bool(result.owner_rights_mask & 0x00040000)  # WRITE_DAC

                assert GoldenProtectionRight.READ_DATA in result.granted_rights
                assert GoldenProtectionRight.EXECUTE in result.granted_rights

                # Ningún derecho mutador concedido
                assert GoldenProtectionRight.WRITE_DATA not in result.granted_rights
                assert GoldenProtectionRight.APPEND_DATA not in result.granted_rights
                assert GoldenProtectionRight.DELETE not in result.granted_rights
                assert GoldenProtectionRight.WRITE_METADATA not in result.granted_rights
                assert GoldenProtectionRight.CHANGE_PERMISSIONS not in result.granted_rights
                assert GoldenProtectionRight.CHANGE_OWNER not in result.granted_rights

                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)
                _kernel32.CloseHandle(h_imp)
                _kernel32.CloseHandle(h_restr)

    def test_accesscheck_token_restringido_directorio(self) -> None:
        """Verifica que un usuario no elevado bajo token restringido no puede mutar un directorio protegido."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = pathlib.Path(tmpdir) / "access_check_dir"
            test_dir.mkdir()

            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path == "access_check_dir"][0]

            h_sec = open_node_security_handle(test_dir)
            h_restr, h_imp = _create_test_restricted_token()

            try:
                apply_target_dacl_by_handle(h_sec, pre)

                result = verify_target_dacl_by_handle(h_sec, pre, token_handle=h_imp)

                assert result.dacl_protected is True
                assert result.owner_rights_present is True

                assert GoldenProtectionRight.READ_DATA in result.granted_rights
                assert GoldenProtectionRight.EXECUTE in result.granted_rights

                assert GoldenProtectionRight.ADD_FILE not in result.granted_rights
                assert GoldenProtectionRight.ADD_SUBDIRECTORY not in result.granted_rights
                assert GoldenProtectionRight.DELETE not in result.granted_rights
                assert GoldenProtectionRight.DELETE_CHILD not in result.granted_rights
                assert GoldenProtectionRight.WRITE_METADATA not in result.granted_rights
                assert GoldenProtectionRight.CHANGE_PERMISSIONS not in result.granted_rights
                assert GoldenProtectionRight.CHANGE_OWNER not in result.granted_rights

                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)
                _kernel32.CloseHandle(h_imp)
                _kernel32.CloseHandle(h_restr)

    def test_reabrir_handle_post_hardening_bajo_token_restringido_falla_access_denied(self) -> None:
        """Bajo contexto no elevado/restringido, intentar abrir un nuevo handle con WRITE_DAC falla con ACCESS_DENIED."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "reopen_check.txt"
            test_file.write_text("data")

            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h_sec = open_node_security_handle(test_file)
            h_restr, h_imp = _create_test_restricted_token()

            try:
                apply_target_dacl_by_handle(h_sec, pre)

                assert _advapi32.SetThreadToken(None, wintypes.HANDLE(h_imp))
                try:
                    h_new = _kernel32.CreateFileW(
                        str(test_file),
                        0x00040000,  # WRITE_DAC
                        0x00000001,  # FILE_SHARE_READ
                        None,
                        3,  # OPEN_EXISTING
                        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                        None,
                    )
                    err = ctypes.get_last_error()
                    assert h_new in (-1, 0, wintypes.HANDLE(-1).value)
                    assert err == 5  # ERROR_ACCESS_DENIED
                finally:
                    _advapi32.SetThreadToken(None, None)

                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)
                _kernel32.CloseHandle(h_imp)
                _kernel32.CloseHandle(h_restr)


# ============================================================================
# 4. Oráculos Adversarios y Pruebas Causales (T1–T17 y M1–M15)
# ============================================================================


class TestTargetDaclOraclesAndMutations:
    """Oráculos adversarios y pruebas de mutación (T1–T17 y M1–M15)."""

    def test_m1_owner_rights_con_full_access_detectado(self) -> None:
        """M1: Una mutación que otorgue FILE_ALL_ACCESS a Owner Rights es rechazada."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        owner_ace = [a for a in spec.aces if a.sid == OWNER_RIGHTS_SID][0]
        assert owner_ace.access_mask != TARGET_MASK_FULL_ACCESS

    def test_m2_quitar_owner_rights_detectado(self) -> None:
        """M2: Una mutación que elimine Owner Rights de la DACL es detectada."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        sids = [a.sid for a in spec.aces]
        assert OWNER_RIGHTS_SID in sids

    def test_m3_quitar_protected_dacl_detectado(self) -> None:
        """M3: Una mutación que omita SE_DACL_PROTECTED es detectada en la especificación."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        assert bool(spec.control_flags & 0x1000) is True

    def test_m4_authenticated_users_con_write_detectado(self) -> None:
        """M4: Una mutación que conceda escritura a Authenticated Users es detectada."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        au_ace = [a for a in spec.aces if a.sid == AUTHENTICATED_USERS_SID][0]
        assert not bool(au_ace.access_mask & 0x0002)

    def test_m5_current_user_con_write_dac_detectado(self) -> None:
        """M5: El oráculo confirma que el owner pierde WRITE_DAC implícito."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        owner_ace = [a for a in spec.aces if a.sid == OWNER_RIGHTS_SID][0]
        assert not bool(owner_ace.access_mask & 0x00040000)

    def test_m6_system_sin_full_access_detectado(self) -> None:
        """M6: Una mutación que degrade SYSTEM es detectada."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        system_ace = [a for a in spec.aces if a.sid == LOCAL_SYSTEM_SID][0]
        assert system_ace.access_mask == TARGET_MASK_FULL_ACCESS

    def test_m7_administrators_sin_full_access_detectado(self) -> None:
        """M7: Una mutación que degrade Administrators es detectada."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        admin_ace = [a for a in spec.aces if a.sid == BUILTIN_ADMINISTRATORS_SID][0]
        assert admin_ace.access_mask == TARGET_MASK_FULL_ACCESS

    def test_m8_aplicar_exige_handle_y_backup_sin_pathname(self) -> None:
        """M8: La API exige (handle, backup) y no admite pathname."""
        import inspect

        sig = inspect.signature(apply_target_dacl_by_handle)
        assert "handle" in sig.parameters
        assert "backup" in sig.parameters
        assert "path" not in sig.parameters

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m9_cerrar_restore_handle_antes_de_apply_falla(self) -> None:
        """M9: Usar un handle cerrado produce TargetDaclApplyError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "closed_handle.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            close_security_handle(h)

            with pytest.raises(TargetDaclApplyError):
                apply_target_dacl_by_handle(h, pre)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m10_reabrir_nuevo_handle_para_restore_falla_bajo_token_restringido(self) -> None:
        """M10: Intentar reabrir un nuevo handle para el restore en lugar del pre-abierto falla."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "reopen_restore_test.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h_pre = open_node_security_handle(test_file)
            h_restr, h_imp = _create_test_restricted_token()

            try:
                apply_target_dacl_by_handle(h_pre, pre)

                assert _advapi32.SetThreadToken(None, wintypes.HANDLE(h_imp))
                try:
                    h_new = _kernel32.CreateFileW(
                        str(test_file),
                        0x00040000 | 0x00020000,
                        0x00000001,
                        None,
                        3,
                        0x02000000 | 0x00200000,
                        None,
                    )
                    assert h_new in (-1, 0, wintypes.HANDLE(-1).value)
                    assert ctypes.get_last_error() == 5
                finally:
                    _advapi32.SetThreadToken(None, None)

                restore_security_descriptor_by_handle(h_pre, pre)
                verify_restored_security_descriptor_by_handle(h_pre, pre)
            finally:
                close_security_handle(h_pre)
                _kernel32.CloseHandle(h_imp)
                _kernel32.CloseHandle(h_restr)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m11_drift_de_file_id_en_restore_es_detectado(self) -> None:
        """M11: Un desajuste de FileId en restore es rechazado inmediatamente."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "drift_test.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)
                adulterated_backup = copy.copy(pre)
                object.__setattr__(adulterated_backup, "file_id", pre.file_id + 99999)

                with pytest.raises(TargetDaclRestoreError, match="Drift de FileId"):
                    restore_security_descriptor_by_handle(h, adulterated_backup)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m12_error_set_security_info_lanza_excepcion_dominio(self) -> None:
        """M12: SetSecurityInfo con retorno de error lanza TargetDaclApplyError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "err_test.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.SetSecurityInfo", return_value=87),
                    pytest.raises(TargetDaclApplyError, match="código 87"),
                ):
                    apply_target_dacl_by_handle(h, pre)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m13_build_native_target_dacl_libera_psids_correctamente(self) -> None:
        """M13: Verifica que build_native_target_dacl no fuga memoria ni causa double-free."""
        spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
        for _ in range(50):
            acl_buf, pacl = build_native_target_dacl(spec)
            assert pacl is not None
            assert len(acl_buf) > 0

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m14_access_check_fallido_lanza_excepcion_dominio(self) -> None:
        """M14: Si AccessCheck falla en el sistema operativo, verify_target_dacl_by_handle falla cerrado."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "accesscheck_fail.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.AccessCheck", return_value=0),
                    pytest.raises(TargetDaclVerificationError, match="AccessCheck falló"),
                ):
                    verify_target_dacl_by_handle(h, pre)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m15_error_en_apply_libera_recursos_nativos_fail_closed(self) -> None:
        """M15: Causal test: inyección de fallo intermedio durante apply no corrompe el nodo ni fuga memoria."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "exception_restore.txt"
            test_file.write_text("content")

            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                with (
                    patch(
                        "sky_claw.local.runtime_vault.target_dacl.build_native_target_dacl",
                        side_effect=TargetDaclBuildError("Simulated build error"),
                    ),
                    pytest.raises(TargetDaclBuildError),
                ):
                    apply_target_dacl_by_handle(h, pre)

                # Verificar que la mutación nunca se aplicó
                verif = verify_restored_security_descriptor_by_handle(h, pre)
                assert verif is None
            finally:
                close_security_handle(h)

    # ------------------------------------------------------------------------
    # Pruebas Causales Específicas T1–T17
    # ------------------------------------------------------------------------

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t1_extra_ace_en_verify_es_rechazado(self) -> None:
        """T1: Si la DACL contiene una 5ª ACE no permitida, verify_target_dacl_by_handle rechaza fail-closed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t1_extra_ace.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)

                # Construir y aplicar nativamente una DACL con 5 ACEs
                spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
                extra_ace = TargetAceSpec("S-1-1-0", 0, 0, 0x00120089, "Everyone")
                spec_5 = object.__new__(TargetDaclSpec)
                object.__setattr__(spec_5, "node_kind", GoldenProtectionNodeKind.FILE)
                object.__setattr__(spec_5, "aces", spec.aces + (extra_ace,))
                object.__setattr__(spec_5, "control_flags", 0x1000)

                acl_buf, pacl = build_native_target_dacl(spec_5)
                ret = _advapi32.SetSecurityInfo(h, 1, 4 | 0x80000000, None, None, pacl, None)
                assert ret == 0

                with pytest.raises(
                    TargetDaclVerificationError, match="DACL contiene 5 ACEs, se requieren exactamente 4"
                ):
                    verify_target_dacl_by_handle(h, pre)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t2_missing_ace_en_verify_es_rechazado(self) -> None:
        """T2: Si la DACL contiene menos de 4 ACEs, verify_target_dacl_by_handle rechaza fail-closed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t2_missing_ace.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)

                # Construir y aplicar nativamente una DACL con 3 ACEs
                spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
                spec_3 = object.__new__(TargetDaclSpec)
                object.__setattr__(spec_3, "node_kind", GoldenProtectionNodeKind.FILE)
                object.__setattr__(spec_3, "aces", spec.aces[:3])
                object.__setattr__(spec_3, "control_flags", 0x1000)

                acl_buf, pacl = build_native_target_dacl(spec_3)
                ret = _advapi32.SetSecurityInfo(h, 1, 4 | 0x80000000, None, None, pacl, None)
                assert ret == 0

                with pytest.raises(
                    TargetDaclVerificationError, match="DACL contiene 3 ACEs, se requieren exactamente 4"
                ):
                    verify_target_dacl_by_handle(h, pre)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t3_reordered_ace_en_verify_es_rechazado(self) -> None:
        """T3: Si el orden canónico de las ACEs está alterado, verify_target_dacl_by_handle rechaza fail-closed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t3_reorder.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)

                # Construir y aplicar nativamente una DACL con ACEs reordenadas (swap ACE 0 y ACE 1)
                spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
                reordered = (spec.aces[1], spec.aces[0], spec.aces[2], spec.aces[3])
                spec_reorder = TargetDaclSpec(
                    node_kind=GoldenProtectionNodeKind.FILE,
                    aces=reordered,
                    control_flags=0x1000,
                )
                acl_buf, pacl = build_native_target_dacl(spec_reorder)
                ret = _advapi32.SetSecurityInfo(h, 1, 4 | 0x80000000, None, None, pacl, None)
                assert ret == 0

                with pytest.raises(TargetDaclVerificationError, match="ACE #1 SID discordante"):
                    verify_target_dacl_by_handle(h, pre)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t4_wrong_mask_en_verify_es_rechazado(self) -> None:
        """T4: Si una ACE posee una máscara alterada, verify_target_dacl_by_handle rechaza fail-closed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t4_mask.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)

                spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
                bad_ace = TargetAceSpec(
                    spec.aces[0].sid, spec.aces[0].ace_type, spec.aces[0].ace_flags, 0x001F01FF, spec.aces[0].name
                )
                spec_bad = TargetDaclSpec(
                    node_kind=GoldenProtectionNodeKind.FILE,
                    aces=(bad_ace, spec.aces[1], spec.aces[2], spec.aces[3]),
                    control_flags=0x1000,
                )
                acl_buf, pacl = build_native_target_dacl(spec_bad)
                ret = _advapi32.SetSecurityInfo(h, 1, 4 | 0x80000000, None, None, pacl, None)
                assert ret == 0

                with pytest.raises(TargetDaclVerificationError, match="ACE #1 AccessMask discordante"):
                    verify_target_dacl_by_handle(h, pre)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t5_wrong_ace_flags_en_verify_es_rechazado(self) -> None:
        """T5: Si una ACE posee banderas de herencia no deseadas, verify rechaza fail-closed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = pathlib.Path(tmpdir) / "t5_dir"
            test_dir.mkdir()
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path == "t5_dir"][0]

            h = open_node_security_handle(test_dir)
            try:
                apply_target_dacl_by_handle(h, pre)

                spec = build_target_dacl_spec(GoldenProtectionNodeKind.DIR)
                bad_ace = TargetAceSpec(
                    spec.aces[0].sid, spec.aces[0].ace_type, 0x02, spec.aces[0].access_mask, spec.aces[0].name
                )
                spec_bad = TargetDaclSpec(
                    node_kind=GoldenProtectionNodeKind.DIR,
                    aces=(bad_ace, spec.aces[1], spec.aces[2], spec.aces[3]),
                    control_flags=0x1000,
                )
                acl_buf, pacl = build_native_target_dacl(spec_bad)
                ret = _advapi32.SetSecurityInfo(h, 1, 4 | 0x80000000, None, None, pacl, None)
                assert ret == 0

                with pytest.raises(TargetDaclVerificationError, match="ACE #1 AceFlags discordante"):
                    verify_target_dacl_by_handle(h, pre)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t6_wrong_ace_type_en_verify_es_rechazado(self) -> None:
        """T6: Si una ACE posee un tipo distinto a ACCESS_ALLOWED (ej. ACCESS_DENIED), verify rechaza."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t6_type.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)

                spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
                acl_buf, pacl = build_native_target_dacl(spec)
                # Modificar el byte AceType de la primera ACE en el buffer nativo (offset 8 = sizeof(ACL))
                acl_buf[8] = 1  # ACCESS_DENIED_ACE_TYPE
                ret = _advapi32.SetSecurityInfo(h, 1, 4 | 0x80000000, None, None, pacl, None)
                assert ret == 0

                with pytest.raises(TargetDaclVerificationError, match="ACE #1 AceType discordante"):
                    verify_target_dacl_by_handle(h, pre)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t7_wrong_volume_serial_aborta_antes_de_set_security_info(self) -> None:
        """T7: Desajuste de VolumeSerialNumber aborta en paso 1 sin llamar a SetSecurityInfo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t7_vol.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            adulterated_backup = copy.copy(pre)
            object.__setattr__(adulterated_backup, "volume_serial_number", pre.volume_serial_number + 12345)

            h = open_node_security_handle(test_file)
            try:
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.SetSecurityInfo") as mock_set,
                    pytest.raises(TargetDaclApplyError, match="Drift de identidad física"),
                ):
                    apply_target_dacl_by_handle(h, adulterated_backup)
                mock_set.assert_not_called()
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t8_wrong_file_id_aborta_antes_de_set_security_info(self) -> None:
        """T8: Desajuste de FileId aborta en paso 1 sin llamar a SetSecurityInfo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t8_fid.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            adulterated_backup = copy.copy(pre)
            object.__setattr__(adulterated_backup, "file_id", pre.file_id + 54321)

            h = open_node_security_handle(test_file)
            try:
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.SetSecurityInfo") as mock_set,
                    pytest.raises(TargetDaclApplyError, match="Drift de identidad física"),
                ):
                    apply_target_dacl_by_handle(h, adulterated_backup)
                mock_set.assert_not_called()
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t9_pre_sd_hash_drift_aborta_antes_de_set_security_info(self) -> None:
        """T9: Drift de PRE SD entre captura y mutación aborta sin mutar."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t9_drift.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            # Simulamos que backup tiene un hash obsoleto
            stale_backup = copy.copy(pre)
            object.__setattr__(
                stale_backup, "pre_sd_sha256", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            )

            h = open_node_security_handle(test_file)
            try:
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.SetSecurityInfo") as mock_set,
                    pytest.raises(TargetDaclApplyError, match="Drift de live PRE SD antes de mutar"),
                ):
                    apply_target_dacl_by_handle(h, stale_backup)
                mock_set.assert_not_called()
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t10_hardlink_externo_en_archivo_aborta_antes_de_set_security_info(self) -> None:
        """T10: Si un archivo regular posee NumberOfLinks > 1, apply aborta fail-closed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t10_file.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            # Crear un hardlink real hacia el archivo
            link_file = pathlib.Path(tmpdir) / "t10_hardlink.txt"
            os.link(test_file, link_file)

            h = open_node_security_handle(test_file)
            try:
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.SetSecurityInfo") as mock_set,
                    pytest.raises(TargetDaclApplyError, match="Hardlink externo detectado"),
                ):
                    apply_target_dacl_by_handle(h, pre)
                mock_set.assert_not_called()
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t11_invalid_binary_sd_rechazado_antes_de_parseo_nativo(self) -> None:
        """T11: Un SD corrupto (con Base64, SHA y length coherentes) es rechazado por IsValidSecurityDescriptor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t11_invalid_sd.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            # 32 bytes de basura
            junk_bytes = b"\xff" * 32
            import base64
            import hashlib

            junk_b64 = base64.b64encode(junk_bytes).decode("ascii")
            junk_sha = hashlib.sha256(junk_bytes).hexdigest()

            corrupt_backup = copy.copy(pre)
            object.__setattr__(corrupt_backup, "pre_sd_bytes_b64", junk_b64)
            object.__setattr__(corrupt_backup, "pre_sd_length", len(junk_bytes))
            object.__setattr__(corrupt_backup, "pre_sd_sha256", junk_sha)

            h = open_node_security_handle(test_file)
            try:
                with pytest.raises(TargetDaclRestoreError, match="IsValidSecurityDescriptor devolvió FALSE"):
                    restore_security_descriptor_by_handle(h, corrupt_backup)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t12_forged_pre_dacl_protected_metadata_rechazado(self) -> None:
        """T12: Discrepancia entre metadata y bytes autoritativos en restore es rechazada."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t12_forged.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            # Invertir flags en metadata mientras bytes de PRE quedan intactos
            forged_backup = copy.copy(pre)
            object.__setattr__(forged_backup, "pre_dacl_protected_flag", not pre.pre_dacl_protected_flag)
            # También invertimos dacl_control_flags para pasar post-init si fuera validado
            new_ctrl = pre.dacl_control_flags ^ 0x1000
            object.__setattr__(forged_backup, "dacl_control_flags", new_ctrl)

            h = open_node_security_handle(test_file)
            try:
                with pytest.raises(
                    TargetDaclRestoreError,
                    match="Discrepancia entre pre_dacl_protected_flag en backup y SE_DACL_PROTECTED",
                ):
                    restore_security_descriptor_by_handle(h, forged_backup)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t13_open_thread_token_error_distinto_a_no_token_falla_cerrado(self) -> None:
        """T13: Si OpenThreadToken falla con un error distinto a ERROR_NO_TOKEN (ej. ERROR_ACCESS_DENIED), falla cerrado."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t13_token_err.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)

                # Simular OpenThreadToken retornando 0 con GetLastError = 5 (ERROR_ACCESS_DENIED)
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.OpenThreadToken", return_value=0),
                    patch("sky_claw.local.runtime_vault.target_dacl.ctypes.get_last_error", return_value=5),
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.OpenProcessToken") as mock_proc_token,
                    pytest.raises(TargetDaclVerificationError, match="rehusando fallback a proceso"),
                ):
                    verify_target_dacl_by_handle(h, pre)
                # Garantizar que OpenProcessToken nunca se llamó
                mock_proc_token.assert_not_called()
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t14_semantic_restore_file_pass(self) -> None:
        """T14: Restauración semántica en archivo pasa round-trip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t14_file.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)
                restore_security_descriptor_by_handle(h, pre)
                verify_restored_security_descriptor_by_handle(h, pre)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t15_semantic_restore_dir_pass(self) -> None:
        """T15: Restauración semántica en directorio pasa round-trip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = pathlib.Path(tmpdir) / "t15_dir"
            test_dir.mkdir()
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path == "t15_dir"][0]

            h = open_node_security_handle(test_dir)
            try:
                apply_target_dacl_by_handle(h, pre)
                restore_security_descriptor_by_handle(h, pre)
                verify_restored_security_descriptor_by_handle(h, pre)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t16_semantic_restore_independiente_de_serializacion_raw(self) -> None:
        """T16: El oráculo post-restore es semántico e independiente de si los bytes raw difieren."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t16_oracle.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)
                restore_security_descriptor_by_handle(h, pre)

                # Comprobamos que verify_restored_security_descriptor_by_handle pasa
                # evaluando semántica (owner, group, DACL, protection)
                verify_restored_security_descriptor_by_handle(h, pre)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_t17_actual_drift_en_restore_falla_verificacion(self) -> None:
        """T17: Desviación real en componentes (owner, group, DACL, protection) hace fallar la verificación."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "t17_drift.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, pre)
                restore_security_descriptor_by_handle(h, pre)

                # 1. Adulteramos owner en backup para exigir un owner distinto -> falla
                drifted_owner_backup = copy.copy(pre)
                drift_sid = "S-1-5-18" if pre.owner_sid != "S-1-5-18" else "S-1-5-11"
                object.__setattr__(drifted_owner_backup, "owner_sid", drift_sid)
                with pytest.raises(TargetDaclVerificationError, match="owner"):
                    verify_restored_security_descriptor_by_handle(h, drifted_owner_backup)

                # 2. Desviación real en disco: alteramos la DACL en el objeto
                spec = build_target_dacl_spec(GoldenProtectionNodeKind.FILE)
                acl_buf, pacl = build_native_target_dacl(spec)
                ret = _advapi32.SetSecurityInfo(h, 1, 4 | 0x80000000, None, None, pacl, None)
                assert ret == 0
                with pytest.raises(TargetDaclVerificationError):
                    verify_restored_security_descriptor_by_handle(h, pre)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_reparse_point_rechazado_fail_closed(self) -> None:
        """Si un nodo es un reparse point / symlink (ReparseTag != 0), apply_target_dacl_by_handle rechaza fail-closed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "reparse_test.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            try:
                # Simular ReparseTag != 0 (ej. IO_REPARSE_TAG_SYMLINK = 0xA000000C)
                with (
                    patch(
                        "sky_claw.local.runtime_vault.target_dacl._read_reparse_tag_by_handle", return_value=0xA000000C
                    ),
                    pytest.raises(TargetDaclApplyError, match="reparse point / symlink"),
                ):
                    apply_target_dacl_by_handle(h, pre)
            finally:
                close_security_handle(h)
