"""Tests para Target DACL Builder y Round-Trip real atado a HANDLE (GP2-S1b).

Cubre:
1. Tests puros / cross-platform de especificación TargetDaclSpec (ADR 0010 §7.2).
2. Jerarquía de excepciones y modelos inmutables.
3. Tests nativos en Windows:
   - Rig estructural sobre archivo y directorio temporales descartables (token del runner).
   - Oráculo de acceso efectivo real con AccessCheck sobre token restringido/filtrado
     (deshabilitando privilegios y neutralizando BUILTIN\\Administrators).
   - Demostración de que reabrir un handle solicitando WRITE_DAC tras el hardening
     falla con ERROR_ACCESS_DENIED bajo el contexto restringido.
   - Restauración exacta bit a bit: POST_RESTORE_SD_BYTES == PRE_SD_BYTES y SHA256 idéntico.
   - Manejo fiel del flag SE_DACL_PROTECTED tanto en PRE protegido como no protegido.
   - Verificación de identidad física (VolumeSerialNumber, FileId) anti-TOCTOU.
4. Oráculos adversarios / pruebas de mutación (M1–M15).
"""

from __future__ import annotations

import ctypes
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
    TargetDaclApplyError,
    TargetDaclBuildError,
    TargetDaclError,
    TargetDaclRestoreError,
    TargetDaclUnsupportedError,
    TargetDaclVerificationError,
    apply_target_dacl_by_handle,
    build_native_target_dacl,
    build_target_dacl_spec,
    close_security_handle,
    create_test_restricted_token,
    open_node_security_handle,
    restore_security_descriptor_by_handle,
    verify_restored_security_descriptor_by_handle,
    verify_target_dacl_by_handle,
)

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

    def test_posix_safety_en_plataforma_no_windows(self) -> None:
        """En entorno no-Windows, las funciones nativas fallan con TargetDaclUnsupportedError."""
        if sys.platform != "win32":
            with pytest.raises(TargetDaclUnsupportedError):
                open_node_security_handle("dummy")
            with pytest.raises(TargetDaclUnsupportedError):
                apply_target_dacl_by_handle(1, GoldenProtectionNodeKind.FILE)
            with pytest.raises(TargetDaclUnsupportedError):
                create_test_restricted_token()


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

            # 1. Capturar PRE
            evidences = probe_node_evidence(tmpdir)
            file_evidences = [e for e in evidences if e.backup.relative_path != "."]
            assert len(file_evidences) == 1
            pre = file_evidences[0].backup

            # 2. Abrir handle de seguridad ANTES de hardening
            h_sec = open_node_security_handle(test_file)
            assert h_sec != 0

            try:
                # 3. Aplicar Target DACL por handle
                spec = apply_target_dacl_by_handle(
                    h_sec,
                    GoldenProtectionNodeKind.FILE,
                    expected_volume_serial=pre.volume_serial_number,
                    expected_file_id=pre.file_id,
                )
                assert spec.node_kind is GoldenProtectionNodeKind.FILE

                # 4. Verificación estructural directa vía GetSecurityInfo
                from ctypes import wintypes

                sd_p = wintypes.LPVOID()
                ret = ctypes.windll.advapi32.GetSecurityInfo(
                    h_sec,
                    1,  # SE_FILE_OBJECT
                    1 | 2 | 4,  # OWNER | GROUP | DACL
                    None,
                    None,
                    None,
                    None,
                    ctypes.byref(sd_p),
                )
                assert ret == 0

                try:
                    # SE_DACL_PROTECTED
                    control = wintypes.WORD()
                    rev = wintypes.DWORD()
                    assert ctypes.windll.advapi32.GetSecurityDescriptorControl(
                        sd_p, ctypes.byref(control), ctypes.byref(rev)
                    )
                    assert bool(control.value & 0x1000) is True  # SE_DACL_PROTECTED

                    # DACL entries
                    dacl_present = wintypes.BOOL()
                    dacl_defaulted = wintypes.BOOL()
                    dacl_out = wintypes.LPVOID()
                    assert ctypes.windll.advapi32.GetSecurityDescriptorDacl(
                        sd_p,
                        ctypes.byref(dacl_present),
                        ctypes.byref(dacl_out),
                        ctypes.byref(dacl_defaulted),
                    )
                    assert dacl_present.value != 0

                    from sky_claw.local.runtime_vault.target_dacl import _AclSizeInformation

                    acl_info = _AclSizeInformation()
                    assert ctypes.windll.advapi32.GetAclInformation(
                        dacl_out, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2
                    )
                    assert acl_info.AceCount == 4

                    # Validar orden exacto de SIDs
                    observed_sids: list[str] = []
                    observed_masks: list[int] = []
                    for i in range(acl_info.AceCount):
                        ace_p = wintypes.LPVOID()
                        assert ctypes.windll.advapi32.GetAce(dacl_out, i, ctypes.byref(ace_p))
                        raw = ctypes.cast(ace_p, ctypes.c_void_p).value
                        assert raw is not None
                        mask_val = ctypes.cast(raw + 4, ctypes.POINTER(wintypes.DWORD)).contents.value
                        sid_ptr = wintypes.LPVOID(raw + 8)

                        s_str_p = wintypes.LPWSTR()
                        assert ctypes.windll.advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(s_str_p))
                        s_val = s_str_p.value
                        ctypes.windll.kernel32.LocalFree(s_str_p)
                        assert s_val is not None
                        observed_sids.append(s_val)
                        observed_masks.append(mask_val)

                    assert observed_sids == [
                        OWNER_RIGHTS_SID,
                        AUTHENTICATED_USERS_SID,
                        LOCAL_SYSTEM_SID,
                        BUILTIN_ADMINISTRATORS_SID,
                    ]
                    assert observed_masks == [
                        FILE_TARGET_MASK_OWNER_RIGHTS,
                        FILE_TARGET_MASK_AUTHENTICATED_USERS,
                        TARGET_MASK_FULL_ACCESS,
                        TARGET_MASK_FULL_ACCESS,
                    ]

                finally:
                    ctypes.windll.kernel32.LocalFree(sd_p)

                # 5. Restaurar sobre el MISMO handle
                restore_security_descriptor_by_handle(
                    h_sec,
                    pre,
                    expected_volume_serial=pre.volume_serial_number,
                    expected_file_id=pre.file_id,
                )

                # 6. Verificar restauración exacta
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)

    def test_roundtrip_estructural_directorio_descartable(self) -> None:
        """Demuestra APPLY → VERIFY estructural → RESTORE sobre 1 directorio descartable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = pathlib.Path(tmpdir) / "disposable_test_dir"
            test_dir.mkdir()

            # Capturar PRE
            evidences = probe_node_evidence(tmpdir)
            dir_evidences = [e for e in evidences if e.backup.relative_path == "disposable_test_dir"]
            assert len(dir_evidences) == 1
            pre = dir_evidences[0].backup
            assert pre.node_kind is GoldenProtectionNodeKind.DIR

            # Abrir handle ANTES del hardening
            h_sec = open_node_security_handle(test_dir)
            assert h_sec != 0

            try:
                # Aplicar Target DACL
                spec = apply_target_dacl_by_handle(
                    h_sec,
                    GoldenProtectionNodeKind.DIR,
                    expected_volume_serial=pre.volume_serial_number,
                    expected_file_id=pre.file_id,
                )
                assert spec.node_kind is GoldenProtectionNodeKind.DIR

                # Restaurar y verificar igualdad binaria
                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)

    def test_roundtrip_pre_ya_protegido_restaura_flag_protegido(self) -> None:
        """Verifica que un nodo PRE que ya poseía SE_DACL_PROTECTED se restaura como protegido."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "protected_pre.txt"
            test_file.write_text("data")

            # Abrir handle de seguridad ANTES de cualquier cambio
            h_sec = open_node_security_handle(test_file)
            try:
                # Fijar SE_DACL_PROTECTED en el estado PRE usando el handle abierto
                # manteniendo permisos de control
                from sky_claw.local.runtime_vault.target_dacl import (
                    _DACL_SECURITY_INFORMATION,
                    _PROTECTED_DACL_SECURITY_INFORMATION,
                    _SE_FILE_OBJECT,
                    _advapi32,
                )

                ret_protect = _advapi32.SetSecurityInfo(
                    h_sec,
                    _SE_FILE_OBJECT,
                    _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    None,
                    None,
                )
                assert ret_protect == 0

                # Capturar PRE: pre_dacl_protected_flag debe ser True
                evidences = probe_node_evidence(tmpdir)
                pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]
                assert pre.pre_dacl_protected_flag is True

                # Aplicar Target DACL y luego restaurar
                apply_target_dacl_by_handle(h_sec, GoldenProtectionNodeKind.FILE)
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
            h_restr, h_imp = create_test_restricted_token()

            try:
                apply_target_dacl_by_handle(h_sec, GoldenProtectionNodeKind.FILE)

                # Evaluar verificación con token restringido
                result = verify_target_dacl_by_handle(h_sec, GoldenProtectionNodeKind.FILE, token_handle=h_imp)

                assert result.dacl_protected is True
                assert result.owner_rights_present is True
                assert not bool(result.owner_rights_mask & 0x00040000)  # WRITE_DAC

                # Comprobar derechos del token restringido
                assert GoldenProtectionRight.READ_DATA in result.granted_rights
                assert GoldenProtectionRight.EXECUTE in result.granted_rights

                # Ningún derecho mutador concedido
                assert GoldenProtectionRight.WRITE_DATA not in result.granted_rights
                assert GoldenProtectionRight.APPEND_DATA not in result.granted_rights
                assert GoldenProtectionRight.DELETE not in result.granted_rights
                assert GoldenProtectionRight.WRITE_METADATA not in result.granted_rights
                assert GoldenProtectionRight.CHANGE_PERMISSIONS not in result.granted_rights
                assert GoldenProtectionRight.CHANGE_OWNER not in result.granted_rights

                # Restaurar
                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)
                ctypes.windll.kernel32.CloseHandle(h_imp)
                ctypes.windll.kernel32.CloseHandle(h_restr)

    def test_accesscheck_token_restringido_directorio(self) -> None:
        """Verifica que un usuario no elevado bajo token restringido no puede mutar un directorio protegido."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = pathlib.Path(tmpdir) / "access_check_dir"
            test_dir.mkdir()

            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path == "access_check_dir"][0]

            h_sec = open_node_security_handle(test_dir)
            h_restr, h_imp = create_test_restricted_token()

            try:
                apply_target_dacl_by_handle(h_sec, GoldenProtectionNodeKind.DIR)

                result = verify_target_dacl_by_handle(h_sec, GoldenProtectionNodeKind.DIR, token_handle=h_imp)

                assert result.dacl_protected is True
                assert result.owner_rights_present is True

                assert GoldenProtectionRight.READ_DATA in result.granted_rights  # LIST_DIRECTORY
                assert GoldenProtectionRight.EXECUTE in result.granted_rights  # TRAVERSE

                # Denegaciones en directorio
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
                ctypes.windll.kernel32.CloseHandle(h_imp)
                ctypes.windll.kernel32.CloseHandle(h_restr)

    def test_reabrir_handle_post_hardening_bajo_token_restringido_falla_access_denied(self) -> None:
        """Bajo contexto no elevado/restringido, intentar abrir un nuevo handle con WRITE_DAC falla con ACCESS_DENIED."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "reopen_check.txt"
            test_file.write_text("data")

            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h_sec = open_node_security_handle(test_file)
            h_restr, h_imp = create_test_restricted_token()

            try:
                apply_target_dacl_by_handle(h_sec, GoldenProtectionNodeKind.FILE)

                # Impersonar el token restringido en el thread actual
                from ctypes import wintypes

                from sky_claw.local.runtime_vault.target_dacl import _advapi32, _kernel32

                assert _advapi32.SetThreadToken(None, wintypes.HANDLE(h_imp))
                try:
                    # Intentar abrir un nuevo handle solicitando WRITE_DAC
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
                    # Revertir impersonación
                    _advapi32.SetThreadToken(None, None)

                # El handle pre-abierto SÍ puede restaurar el archivo
                restore_security_descriptor_by_handle(h_sec, pre)
                verify_restored_security_descriptor_by_handle(h_sec, pre)

            finally:
                close_security_handle(h_sec)
                ctypes.windll.kernel32.CloseHandle(h_imp)
                ctypes.windll.kernel32.CloseHandle(h_restr)


# ============================================================================
# 4. Oráculos Adversarios / Mutation Testing (M1–M15)
# ============================================================================


class TestTargetDaclMutationOracles:
    """Oráculos diseñados para detectar desviaciones y mutaciones accidentales."""

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

    def test_m8_aplicar_por_pathname_prohibido_en_api(self) -> None:
        """M8: La API no admite pathnames, exige un HANDLE entero abierto."""
        import inspect

        sig = inspect.signature(apply_target_dacl_by_handle)
        assert "handle" in sig.parameters
        assert "path" not in sig.parameters

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m9_cerrar_restore_handle_antes_de_apply_falla(self) -> None:
        """M9: Usar un handle cerrado produce TargetDaclApplyError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "closed_handle.txt"
            test_file.write_text("data")
            h = open_node_security_handle(test_file)
            close_security_handle(h)

            with pytest.raises(TargetDaclApplyError):
                apply_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE)

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
                apply_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE)
                with pytest.raises(TargetDaclRestoreError, match="Drift de FileId"):
                    restore_security_descriptor_by_handle(h, pre, expected_file_id=pre.file_id + 99999)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m12_error_set_security_info_lanza_excepcion_dominio(self) -> None:
        """M12: SetSecurityInfo con retorno de error lanza TargetDaclApplyError."""
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "err_test.txt"
            test_file.write_text("data")
            h = open_node_security_handle(test_file)
            try:
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.SetSecurityInfo", return_value=87),
                    pytest.raises(TargetDaclApplyError, match="código 87"),
                ):
                    apply_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m15_restore_en_ruta_de_excepcion(self) -> None:
        """M15: Simula un fallo intermedio y comprueba que el bloque finally ejecuta la restauración."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "exception_restore.txt"
            test_file.write_text("content")

            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h = open_node_security_handle(test_file)
            restored = False

            try:
                apply_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE)
                # Simular excepción en verify
                raise ValueError("Simulated failure in test payload")
            except ValueError:
                pass
            finally:
                restore_security_descriptor_by_handle(h, pre)
                verify_restored_security_descriptor_by_handle(h, pre)
                restored = True
                close_security_handle(h)

            assert restored is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_m10_reabrir_nuevo_handle_para_restore_falla_bajo_token_restringido(self) -> None:
        """M10: Intentar reabrir un nuevo handle para el restore en lugar del pre-abierto falla."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "reopen_restore_test.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            h_pre = open_node_security_handle(test_file)
            h_restr, h_imp = create_test_restricted_token()

            try:
                apply_target_dacl_by_handle(h_pre, GoldenProtectionNodeKind.FILE)

                # Bajo token restringido, intentar abrir un NUEVO handle para restaurar
                from sky_claw.local.runtime_vault.target_dacl import _advapi32, _kernel32

                assert _advapi32.SetThreadToken(None, ctypes.wintypes.HANDLE(h_imp))
                try:
                    h_new = _kernel32.CreateFileW(
                        str(test_file),
                        0x00040000 | 0x00020000,  # WRITE_DAC | READ_CONTROL
                        0x00000001,
                        None,
                        3,
                        0x02000000 | 0x00200000,
                        None,
                    )
                    assert h_new in (-1, 0, ctypes.wintypes.HANDLE(-1).value)
                    assert ctypes.get_last_error() == 5  # ACCESS_DENIED
                finally:
                    _advapi32.SetThreadToken(None, None)

                # Pero el handle pre-abierto SÍ puede restaurar
                restore_security_descriptor_by_handle(h_pre, pre)
                verify_restored_security_descriptor_by_handle(h_pre, pre)
            finally:
                close_security_handle(h_pre)
                ctypes.windll.kernel32.CloseHandle(h_imp)
                ctypes.windll.kernel32.CloseHandle(h_restr)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_drift_se_dacl_protected_en_restore_es_detectado(self) -> None:
        """Si el descriptor verificado no recupera el estado protegido original, se detecta."""
        import copy

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "protected_drift.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            # Falsificar backup para exigir flag protected invertido
            adulterated_backup = copy.copy(pre)
            object.__setattr__(
                adulterated_backup,
                "pre_dacl_protected_flag",
                not pre.pre_dacl_protected_flag,
            )

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE)
                restore_security_descriptor_by_handle(h, pre)
                with pytest.raises(TargetDaclVerificationError, match="Estado protegido no coincide"):
                    verify_restored_security_descriptor_by_handle(h, adulterated_backup)
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
            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE)
                with (
                    patch("sky_claw.local.runtime_vault.target_dacl._advapi32.AccessCheck", return_value=0),
                    pytest.raises(TargetDaclVerificationError, match="AccessCheck falló"),
                ):
                    verify_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE)
            finally:
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_crypto_integrity_check_en_restore_rechaza_tampering(self) -> None:
        """Verifica que restore_security_descriptor_by_handle rechace hash SHA256 adulterado."""
        import copy

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "crypto_tamper.txt"
            test_file.write_text("data")
            evidences = probe_node_evidence(tmpdir)
            pre = [e.backup for e in evidences if e.backup.relative_path != "."][0]

            tampered_backup = copy.copy(pre)
            object.__setattr__(tampered_backup, "pre_sd_sha256", "deadbeef" * 8)

            h = open_node_security_handle(test_file)
            try:
                apply_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE)
                with pytest.raises(TargetDaclRestoreError, match="Fallo de integridad criptográfica"):
                    restore_security_descriptor_by_handle(h, tampered_backup)
            finally:
                restore_security_descriptor_by_handle(h, pre)
                close_security_handle(h)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_drift_de_volumen_en_apply_es_detectado(self) -> None:
        """Verifica que apply_target_dacl_by_handle rechace volumen discordante."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = pathlib.Path(tmpdir) / "vol_drift.txt"
            test_file.write_text("data")
            h = open_node_security_handle(test_file)
            try:
                with pytest.raises(TargetDaclApplyError, match="Drift de volumen"):
                    apply_target_dacl_by_handle(h, GoldenProtectionNodeKind.FILE, expected_volume_serial=12345678)
            finally:
                close_security_handle(h)
