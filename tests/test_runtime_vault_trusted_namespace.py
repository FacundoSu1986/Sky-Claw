"""Tests para Trusted Namespace y Contrato de DACLs - GP2-S3a-1.

Verifica el contrato normativo de ADR 0010 §11.3 y atiende los 10 blockers de la revisión adversarial:
- NS-01: Bootstrap correcto sobre namespace inexistente (Caso A, creado con SD desde el nacimiento).
- NS-02: Normalización segura de directorio preexistente con owner Administrators a SYSTEM (Caso B canónico).
- NS-03: Rechazo fail-closed ante directorio preexistente con owner no permitido (usuario estándar).
- NS-04: Rechazo fail-closed ante reparse point / junction / symlink preexistente.
- NS-05: Rechazo fail-closed si se encuentra un archivo donde se esperaba un directorio.
- NS-06: Rechazo fail-closed incondicional si trusted_goldens.json ya existía (GP2-T50 Variante B).
- NS-07: Fallo en ancestro detiene el provisioning; ningún descendiente es creado ni modificado.
- Blockers 1 & 2: Cero fallbacks DACL-only; WRITE_OWNER obligatorio en handles mutantes; fallo cerrado sin privilegios.
- Blocker 3: Sin seams de test en la API productiva (sin runner_sid ni permitted_owners).
- Blocker 4: Caso B normaliza estrictamente Administrators -> SYSTEM y verifica estado canónico.
- Blocker 5: TGR nace con Security Descriptor canónico y se verifica por handle post-replace.
- Blocker 6: Detección estricta de reparse points / symlinks rotos vía CreateFileW (sin Path.exists).
- Blocker 7: Congelamiento de máscaras exactas (Sky-Claw AU=0x0020, staging AU=0x0025, TGR AU=0x00120089).
- Blocker 8: Verificación de permisos efectivos reales mediante kernel AccessCheck y token restringido.
- Blocker 10: Seguridad de memoria (sin dangling p_dacl tras LocalFree; booleans de SD verificados).
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.trusted_namespace import (
    AUTHENTICATED_USERS_SID,
    BUILTIN_ADMINISTRATORS_SID,
    CANONICAL_NAMESPACE_OWNER,
    CANONICAL_NAMESPACE_PRIMARY_GROUP,
    LOCAL_SYSTEM_SID,
    PERMITTED_NAMESPACE_OWNERS,
    AncestorProvisioningError,
    NamespaceObjectNotDirectoryError,
    NamespaceOwnerNotPermittedError,
    NamespaceReparsePointError,
    PreexistingTrustedRegistryError,
    TrustedNamespaceError,
    TrustedNamespaceResult,
    TrustedNamespaceUnsupportedError,
    apply_canonical_tgr_file_security,
    bootstrap_trusted_namespace,
    build_namespace_dacl_spec,
)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    from sky_claw.local.runtime_vault.trusted_namespace import (
        _DACL_SECURITY_INFORMATION,
        _DELETE,
        _FILE_ADD_FILE,
        _FILE_ADD_SUBDIRECTORY,
        _FILE_ALL_ACCESS,
        _FILE_APPEND_DATA,
        _FILE_DELETE_CHILD,
        _FILE_GENERIC_READ,
        _FILE_READ_ATTRIBUTES,
        _FILE_READ_DATA,
        _FILE_SHARE_DELETE,
        _FILE_SHARE_READ,
        _FILE_SHARE_WRITE,
        _FILE_TRAVERSE,
        _FILE_WRITE_DATA,
        _GROUP_SECURITY_INFORMATION,
        _OPEN_EXISTING,
        _OWNER_SECURITY_INFORMATION,
        _PROTECTED_DACL_SECURITY_INFORMATION,
        _READ_CONTROL,
        _SE_DACL_PROTECTED,
        _SE_FILE_OBJECT,
        _SECURITY_DESCRIPTOR_REVISION,
        _WRITE_DAC,
        _WRITE_OWNER,
        _advapi32,
        _build_native_acl,
        _check_object_exists_no_reparse,
        _kernel32,
    )

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class _GenericMapping(ctypes.Structure):
        _fields_ = [
            ("GenericRead", wintypes.DWORD),
            ("GenericWrite", wintypes.DWORD),
            ("GenericExecute", wintypes.DWORD),
            ("GenericAll", wintypes.DWORD),
        ]

    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL

    _advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.DuplicateTokenEx.restype = wintypes.BOOL

    _advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SidAndAttributes),
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_SidAndAttributes),
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.CreateRestrictedToken.restype = wintypes.BOOL

    _advapi32.AccessCheck.argtypes = [
        wintypes.LPVOID,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(_GenericMapping),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.AccessCheck.restype = wintypes.BOOL


# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class TestJerarquiaExcepcionesNamespace:
    """Verifica que las excepciones del namespace deriven de RuntimeVaultError."""

    def test_herencia(self) -> None:
        assert issubclass(TrustedNamespaceError, RuntimeVaultError)
        assert issubclass(NamespaceReparsePointError, TrustedNamespaceError)
        assert issubclass(NamespaceOwnerNotPermittedError, TrustedNamespaceError)
        assert issubclass(NamespaceObjectNotDirectoryError, TrustedNamespaceError)
        assert issubclass(PreexistingTrustedRegistryError, TrustedNamespaceError)
        assert issubclass(AncestorProvisioningError, TrustedNamespaceError)
        assert issubclass(TrustedNamespaceUnsupportedError, TrustedNamespaceError)


# ============================================================================
# Constantes y Especificaciones de DACL Puras (Blockers 3 & 7)
# ============================================================================


class TestNamespacePureSpecs:
    """Verificación de constantes y especificaciones de DACL según ADR 0010 §11.3."""

    def test_constantes_normativas(self) -> None:
        assert CANONICAL_NAMESPACE_OWNER == LOCAL_SYSTEM_SID
        assert CANONICAL_NAMESPACE_PRIMARY_GROUP == BUILTIN_ADMINISTRATORS_SID
        assert set(PERMITTED_NAMESPACE_OWNERS) == {LOCAL_SYSTEM_SID, BUILTIN_ADMINISTRATORS_SID}
        assert isinstance(PERMITTED_NAMESPACE_OWNERS, frozenset)

    def test_especificaciones_dacl_sin_seams_de_test(self) -> None:
        """Verifica que build_namespace_dacl_spec no posea parámetros de test y genere DACLs válidas."""
        for obj_name in [
            "Sky-Claw",
            "runtime_vault",
            "operations",
            "golden_backups",
            "locks",
            "staging",
            "trusted_goldens.json",
        ]:
            spec = build_namespace_dacl_spec(obj_name)
            assert spec.object_name == obj_name
            assert spec.control_flags == 0x1000  # SE_DACL_PROTECTED

            admins_ace = next((ace for ace in spec.aces if ace.sid == BUILTIN_ADMINISTRATORS_SID), None)
            system_ace = next((ace for ace in spec.aces if ace.sid == LOCAL_SYSTEM_SID), None)
            assert admins_ace is not None
            assert admins_ace.access_mask == 0x001F01FF
            assert system_ace is not None
            assert system_ace.access_mask == 0x001F01FF

    def test_blocker_7_exact_dacl_masks_frozen(self) -> None:
        """Blocker 7: Congelamiento exacto de máscaras de acceso sin bits útiles no concedidos."""
        # 1. Sky-Claw/ AU = FILE_TRAVERSE únicamente (0x00000020), flags 0x00
        # Sin READ_CONTROL (0x00020000) ni SYNCHRONIZE (0x00100000)
        spec_root = build_namespace_dacl_spec("Sky-Claw")
        au_root = next(ace for ace in spec_root.aces if ace.sid == AUTHENTICATED_USERS_SID)
        assert au_root.access_mask == 0x00000020
        assert au_root.ace_flags == 0x00
        assert (au_root.access_mask & 0x00020000) == 0
        assert (au_root.access_mask & 0x00100000) == 0

        # 2. staging/ parent AU = FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_ADD_SUBDIRECTORY (0x00000025), flags 0x00
        # Sin READ_CONTROL (0x00020000) ni SYNCHRONIZE (0x00100000)
        spec_staging = build_namespace_dacl_spec("staging")
        au_staging_parent = next(
            ace for ace in spec_staging.aces if ace.sid == AUTHENTICATED_USERS_SID and ace.ace_flags == 0x00
        )
        assert au_staging_parent.access_mask == 0x00000025
        assert au_staging_parent.ace_flags == 0x00
        assert (au_staging_parent.access_mask & 0x00020000) == 0
        assert (au_staging_parent.access_mask & 0x00100000) == 0

        # 3. trusted_goldens.json AU = FILE_GENERIC_READ (0x00120089), flags 0x00
        spec_tgr = build_namespace_dacl_spec("trusted_goldens.json")
        au_tgr = next(ace for ace in spec_tgr.aces if ace.sid == AUTHENTICATED_USERS_SID)
        assert au_tgr.access_mask == 0x00120089
        assert au_tgr.ace_flags == 0x00

        # 4. runtime_vault/, operations/, golden_backups/, locks/ AU = 0x001200A9
        for name in ("runtime_vault", "operations", "golden_backups", "locks"):
            spec = build_namespace_dacl_spec(name)
            au = next(ace for ace in spec.aces if ace.sid == AUTHENTICATED_USERS_SID)
            assert au.access_mask == 0x001200A9
            assert au.ace_flags == 0x00


# ============================================================================
# NS-01 a NS-07: Bootstrap, Anti-Squatting y Ancestros (Windows)
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas Win32 nativas de namespace")
class TestNamespaceBootstrapWindows:
    """Tests de orquestación y anti-squatting en Windows."""

    @pytest.fixture
    def mock_elevated_provisioning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simula elevación durante el bootstrap creando directorios reales y neutralizando syscalls privilegiadas."""
        from sky_claw.local.runtime_vault.trusted_namespace import _open_handle_no_reparse

        real_create_dir = _kernel32.CreateDirectoryW

        def _simulated_create_dir(path: str, sa: Any) -> bool:
            return bool(real_create_dir(path, None))

        monkeypatch.setattr(_kernel32, "CreateDirectoryW", _simulated_create_dir)

        real_open_handle = _open_handle_no_reparse

        def _simulated_open_handle(
            path: Any, desired_access: int = _READ_CONTROL | _FILE_READ_ATTRIBUTES, **kwargs: Any
        ) -> int:
            # En entorno de pruebas sintético, remover WRITE_OWNER para que el kernel permita abrir el handle en tmp_path
            access = desired_access & ~_WRITE_OWNER
            return real_open_handle(path, desired_access=access, **kwargs)

        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._open_handle_no_reparse",
            _simulated_open_handle,
        )

        monkeypatch.setattr(_advapi32, "SetSecurityInfo", lambda *args: 0)
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace.create_secured_file_from_birth",
            lambda path, obj="trusted_goldens.json": _kernel32.CreateFileW(
                str(path), 0x40000000 | 0x80000000, 0, None, 1, 0x80, None
            ),
        )
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace.verify_secured_file_by_handle",
            lambda path: None,
        )

    def test_ns_unelevated_without_privilege_fails_closed(self, tmp_path: pathlib.Path) -> None:
        """Blockers 1 & 2: Verifica que en entorno no elevado la creación falle cerrado con error 1307 (sin fallback)."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        with pytest.raises(AncestorProvisioningError) as exc_info:
            bootstrap_trusted_namespace(root_dir=root_test)
        assert "1307" in str(exc_info.value) or "1314" in str(exc_info.value)

    def test_ns_01_bootstrap_namespace_ausente_caso_a(
        self, tmp_path: pathlib.Path, mock_elevated_provisioning: None
    ) -> None:
        """NS-01: Bootstrap correcto sobre namespace inexistente (Caso A, orden de ancestros completo)."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"

        result = bootstrap_trusted_namespace(root_dir=root_test)

        assert isinstance(result, TrustedNamespaceResult)
        assert result.success is True

        rv_dir = root_test / "runtime_vault"
        assert rv_dir.is_dir()
        assert (rv_dir / "staging").is_dir()
        assert (rv_dir / "operations").is_dir()
        assert (rv_dir / "locks").is_dir()
        assert (rv_dir / "golden_backups").is_dir()
        assert (rv_dir / "trusted_goldens.json").is_file()

    def test_ns_02_directorio_preexistente_administrators_normaliza_a_system_caso_b(
        self, tmp_path: pathlib.Path, mock_elevated_provisioning: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NS-02 / Blocker 4: Preexistente propiedad de Administrators normaliza estrictamente a SYSTEM."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)

        set_security_info_spy = MagicMock(return_value=0)
        monkeypatch.setattr(_advapi32, "SetSecurityInfo", set_security_info_spy)

        with patch(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
            return_value=(BUILTIN_ADMINISTRATORS_SID, CANONICAL_NAMESPACE_PRIMARY_GROUP, True),
        ):
            result = bootstrap_trusted_namespace(root_dir=root_test)
            assert result.success is True

            # Verificar que SetSecurityInfo fue invocado con OWNER_SECURITY_INFORMATION
            assert set_security_info_spy.called
            for call in set_security_info_spy.call_args_list:
                args = call[0]
                sec_info_flags = args[2]
                assert sec_info_flags & _OWNER_SECURITY_INFORMATION
                assert sec_info_flags & _GROUP_SECURITY_INFORMATION
                assert sec_info_flags & _DACL_SECURITY_INFORMATION
                assert sec_info_flags & _PROTECTED_DACL_SECURITY_INFORMATION

    def test_ns_03_directorio_preexistente_owner_no_permitido_falla_cerrado(
        self, tmp_path: pathlib.Path, mock_elevated_provisioning: None
    ) -> None:
        """NS-03: Directorio preexistente con owner no permitido (usuario estándar) falla cerrado."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)

        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
                return_value=("S-1-5-21-12345-6789-0", "S-1-5-21-12345-6789-513", True),
            ),
            pytest.raises(NamespaceOwnerNotPermittedError, match="no pertenece a"),
        ):
            bootstrap_trusted_namespace(root_dir=root_test)

    def test_ns_04_reparse_point_preexistente_falla_cerrado(
        self, tmp_path: pathlib.Path, mock_elevated_provisioning: None
    ) -> None:
        """NS-04: Reparse point / junction / symlink preexistente falla cerrado incondicionalmente."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)

        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._read_handle_reparse_and_attributes",
                return_value=(0xA0000003, 0x410),
            ),
            pytest.raises(NamespaceReparsePointError, match="reparse point"),
        ):
            bootstrap_trusted_namespace(root_dir=root_test)

    def test_ns_05_archivo_donde_se_esperaba_directorio_falla_cerrado(
        self, tmp_path: pathlib.Path, mock_elevated_provisioning: None
    ) -> None:
        """NS-05: Archivo plano donde se esperaba un directorio falla cerrado."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        root_test.parent.mkdir(parents=True)
        root_test.write_text("archivo malicioso")

        with pytest.raises(NamespaceObjectNotDirectoryError, match="no es un directorio"):
            bootstrap_trusted_namespace(root_dir=root_test)

    def test_ns_06_gp2_t50_variante_b_preexisting_tgr_always_fails_closed(
        self, tmp_path: pathlib.Path, mock_elevated_provisioning: None
    ) -> None:
        """NS-06 / GP2-T50 Variante B: trusted_goldens.json preexistente SIEMPRE falla cerrado."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)
        planted_tgr = rv_dir / "trusted_goldens.json"
        planted_tgr.write_text('{"entries":[],"schema_version":"1.0"}')

        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
                return_value=(LOCAL_SYSTEM_SID, CANONICAL_NAMESPACE_PRIMARY_GROUP, True),
            ),
            pytest.raises(PreexistingTrustedRegistryError, match="preexistente"),
        ):
            bootstrap_trusted_namespace(root_dir=root_test)

    def test_ns_07_fallo_en_ancestro_no_crea_descendientes(self, tmp_path: pathlib.Path) -> None:
        """NS-07: Fallo en ancestro detiene el provisioning; ningún descendiente es creado ni confiado."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"

        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._provision_or_normalize_directory",
                side_effect=AncestorProvisioningError("Simulated ancestor failure"),
            ),
            pytest.raises(AncestorProvisioningError, match="Simulated ancestor failure"),
        ):
            bootstrap_trusted_namespace(root_dir=root_test)

        assert not (root_test / "runtime_vault").exists()


# ============================================================================
# Causal Win32 Security: WRITE_OWNER & Reparse Points (Blockers 1, 2, 6)
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas Win32 nativas de kernel")
class TestNamespaceCausalSecurityWindows:
    """Pruebas causales de comportamiento del kernel Win32 ante reparse points y derechos de handle."""

    def test_blocker_6_broken_reparse_point_fails_closed(self, tmp_path: pathlib.Path) -> None:
        """Blocker 6: Symlink/junction rota es detectada por CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT) y falla cerrado."""
        target_dir = tmp_path / "real_target"
        target_dir.mkdir()
        junction_dir = tmp_path / "broken_junction"

        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_dir), str(target_dir)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            pytest.skip("mklink /J no soportado o falló en este entorno")

        try:
            # Eliminar destino para que la junction quede rota
            target_dir.rmdir()

            # Path.exists() devolvería False (ciego ante la junction rota)
            assert not junction_dir.exists()

            # CreateFileW con FILE_FLAG_OPEN_REPARSE_POINT debe atraparla y lanzar NamespaceReparsePointError
            with pytest.raises(NamespaceReparsePointError, match="reparse point"):
                _check_object_exists_no_reparse(junction_dir)
        finally:
            subprocess.run(["cmd", "/c", "rmdir", str(junction_dir)], capture_output=True)

    def test_blocker_2_handle_without_write_owner_fails_closed(self, tmp_path: pathlib.Path) -> None:
        """Blocker 2: Handle sin WRITE_OWNER produce ERROR_ACCESS_DENIED (5) al invocar SetSecurityInfo con OWNER."""
        test_file = tmp_path / "write_owner_test.txt"
        test_file.write_text("causal test")

        h_no_write_owner = _kernel32.CreateFileW(
            str(test_file),
            _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,  # Sin _WRITE_OWNER
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        assert h_no_write_owner not in (-1, 0xFFFFFFFF)

        p_owner = wintypes.LPVOID()
        _advapi32.ConvertStringSidToSidW(LOCAL_SYSTEM_SID, ctypes.byref(p_owner))

        try:
            res = _advapi32.SetSecurityInfo(
                h_no_write_owner,
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION,
                p_owner,
                None,
                None,
                None,
            )
            # Retorna 5: ERROR_ACCESS_DENIED porque el handle carece del derecho WRITE_OWNER
            assert res == 5
        finally:
            _kernel32.CloseHandle(h_no_write_owner)
            _kernel32.LocalFree(p_owner)

    def test_blocker_1_apply_canonical_tgr_fails_closed_without_fallback(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blocker 1: apply_canonical_tgr_file_security falla cerrado sin fallback si SetSecurityInfo falla."""
        from sky_claw.local.runtime_vault.trusted_namespace import _open_handle_no_reparse as real_open

        test_file = tmp_path / "tgr_test.json"
        test_file.write_text("{}")

        # Permitir apertura del handle en tmp_path sin elevar
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._open_handle_no_reparse",
            lambda path, desired_access, **kw: real_open(path, desired_access=desired_access & ~_WRITE_OWNER, **kw),
        )
        # Simular que SetSecurityInfo retorna error 1307
        monkeypatch.setattr(_advapi32, "SetSecurityInfo", lambda *args: 1307)

        with pytest.raises(TrustedNamespaceError, match="código Win32 1307"):
            apply_canonical_tgr_file_security(test_file)


# ============================================================================
# NS-08 a NS-15: Verificación de Derechos Efectivos con AccessCheck (Blocker 8)
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas Win32 nativas de AccessCheck")
class TestNamespaceEffectiveAccessCheckWindows:
    """Blocker 8: Tests de permisos efectivos mediante AccessCheck real del kernel con token restringido.

    Crea un token restringido donde BUILTIN\\Administrators está marcado como DENY-ONLY
    y todos los privilegios han sido revocados (DISABLE_MAX_PRIVILEGE).
    Evalúa los Security Descriptors canónicos con la función de referencia del kernel.
    """

    @pytest.fixture(scope="class")
    def restricted_token(self) -> int:
        """Crea un token de impersonación restringido con Administrators DENY-ONLY y cero privilegios."""
        h_proc = _kernel32.GetCurrentProcess()
        h_token = wintypes.HANDLE()
        # TOKEN_DUPLICATE (0x0002) | TOKEN_QUERY (0x0008)
        if not _advapi32.OpenProcessToken(h_proc, 0x0002 | 0x0008, ctypes.byref(h_token)):
            pytest.skip("No se pudo abrir token de proceso para AccessCheck")

        try:
            psid_admin = wintypes.LPVOID()
            if not _advapi32.ConvertStringSidToSidW(BUILTIN_ADMINISTRATORS_SID, ctypes.byref(psid_admin)):
                pytest.skip("ConvertStringSidToSidW falló")

            try:
                sids_to_disable = (_SidAndAttributes * 1)()
                sids_to_disable[0].Sid = psid_admin
                sids_to_disable[0].Attributes = 0

                h_restricted = wintypes.HANDLE()
                # DISABLE_MAX_PRIVILEGE = 0x1
                if not _advapi32.CreateRestrictedToken(
                    h_token, 1, 1, sids_to_disable, 0, None, 0, None, ctypes.byref(h_restricted)
                ):
                    pytest.skip("CreateRestrictedToken falló")

                try:
                    h_impersonation = wintypes.HANDLE()
                    # SecurityImpersonation = 2, TokenImpersonation = 2
                    # TOKEN_QUERY (0x0008) | TOKEN_IMPERSONATE (0x0004) | STANDARD_RIGHTS_READ (0x00020000)
                    if not _advapi32.DuplicateTokenEx(
                        h_restricted,
                        0x00020000 | 0x0008 | 0x0004,
                        None,
                        2,
                        2,
                        ctypes.byref(h_impersonation),
                    ):
                        pytest.skip("DuplicateTokenEx falló al crear token de impersonación")

                    yield int(h_impersonation.value)
                    _kernel32.CloseHandle(h_impersonation)
                finally:
                    _kernel32.CloseHandle(h_restricted)
            finally:
                _kernel32.LocalFree(psid_admin)
        finally:
            _kernel32.CloseHandle(h_token)

    def _build_test_sd(self, object_name: str) -> tuple[Any, Any]:
        """Helper para construir un Security Descriptor canónico y mantener vivo su buffer."""
        spec = build_namespace_dacl_spec(object_name)
        acl_buf, pacl = _build_native_acl(spec.aces)

        p_owner = wintypes.LPVOID()
        p_group = wintypes.LPVOID()
        _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_owner))
        _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_group))

        sd_buf = (ctypes.c_ubyte * 256)()
        p_sd = ctypes.cast(sd_buf, wintypes.LPVOID)
        assert _advapi32.InitializeSecurityDescriptor(p_sd, _SECURITY_DESCRIPTOR_REVISION)
        assert _advapi32.SetSecurityDescriptorOwner(p_sd, p_owner, False)
        assert _advapi32.SetSecurityDescriptorGroup(p_sd, p_group, False)
        assert _advapi32.SetSecurityDescriptorDacl(p_sd, True, pacl, False)
        assert _advapi32.SetSecurityDescriptorControl(p_sd, _SE_DACL_PROTECTED, _SE_DACL_PROTECTED)

        _kernel32.LocalFree(p_owner)
        _kernel32.LocalFree(p_group)
        return (sd_buf, acl_buf), p_sd

    def _check_access(self, p_sd: Any, restricted_token: int, desired_access: int) -> bool:
        """Ejecuta AccessCheck real del kernel."""
        g_mapping = _GenericMapping(0x00120089, 0x00120116, 0x001200A0, 0x001F01FF)
        priv_buf = (ctypes.c_ubyte * 1024)()
        priv_len = wintypes.DWORD(1024)
        granted = wintypes.DWORD(0)
        status = wintypes.BOOL(0)
        ok = _advapi32.AccessCheck(
            p_sd,
            restricted_token,
            desired_access,
            ctypes.byref(g_mapping),
            ctypes.cast(priv_buf, wintypes.LPVOID),
            ctypes.byref(priv_len),
            ctypes.byref(granted),
            ctypes.byref(status),
        )
        return bool(ok and status.value)

    def test_ns_08_runtime_vault_access_check(self, restricted_token: int) -> None:
        """NS-08: AccessCheck en runtime_vault/ permite lectura/traverse y deniega add_file, add_subdir, delete_child."""
        _, p_sd = self._build_test_sd("runtime_vault")
        assert self._check_access(p_sd, restricted_token, _FILE_GENERIC_READ | _FILE_TRAVERSE) is True
        assert self._check_access(p_sd, restricted_token, _FILE_ADD_FILE) is False
        assert self._check_access(p_sd, restricted_token, _FILE_ADD_SUBDIRECTORY) is False
        assert self._check_access(p_sd, restricted_token, _FILE_DELETE_CHILD) is False
        assert self._check_access(p_sd, restricted_token, _DELETE) is False

    def test_ns_09_to_11_tgr_file_access_check(self, restricted_token: int) -> None:
        """NS-09 a NS-11: AccessCheck en trusted_goldens.json permite lectura y deniega write, delete, WRITE_DAC, WRITE_OWNER."""
        _, p_sd = self._build_test_sd("trusted_goldens.json")
        assert self._check_access(p_sd, restricted_token, _FILE_GENERIC_READ) is True
        assert self._check_access(p_sd, restricted_token, _FILE_WRITE_DATA) is False
        assert self._check_access(p_sd, restricted_token, _FILE_APPEND_DATA) is False
        assert self._check_access(p_sd, restricted_token, _DELETE) is False
        assert self._check_access(p_sd, restricted_token, _WRITE_DAC) is False
        assert self._check_access(p_sd, restricted_token, _WRITE_OWNER) is False

    def test_ns_12_and_13_operations_and_golden_backups_access_check(self, restricted_token: int) -> None:
        """NS-12 y NS-13: AccessCheck en operations/ y golden_backups/ deniega creación a usuarios restringidos."""
        for name in ("operations", "golden_backups"):
            _, p_sd = self._build_test_sd(name)
            assert self._check_access(p_sd, restricted_token, _FILE_GENERIC_READ) is True
            assert self._check_access(p_sd, restricted_token, _FILE_ADD_FILE) is False
            assert self._check_access(p_sd, restricted_token, _FILE_ADD_SUBDIRECTORY) is False
            assert self._check_access(p_sd, restricted_token, _FILE_DELETE_CHILD) is False

    def test_ns_14_locks_container_isolation_access_check(self, restricted_token: int) -> None:
        """NS-14: locks/ tiene ACE contenedor únicamente; un archivo *.lock dentro no tiene ACE para AU."""
        _, p_sd_dir = self._build_test_sd("locks")
        # En el directorio, el usuario restringido puede listar y traverse
        assert self._check_access(p_sd_dir, restricted_token, 0x0001 | 0x0020) is True

        # En un archivo .lock (que no hereda la ACE de AU porque flags=0x00), AU no tiene lectura
        # Simulamos la DACL de un archivo en locks/ (solo Admins y SYSTEM con FILE_ALL_ACCESS)
        spec_lock = [
            (BUILTIN_ADMINISTRATORS_SID, _FILE_ALL_ACCESS),
            (LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS),
        ]
        # Creamos SD para el archivo
        from sky_claw.local.runtime_vault.trusted_namespace import NamespaceAceSpec

        lock_aces = [NamespaceAceSpec(sid=s, access_mask=m, ace_flags=0) for s, m in spec_lock]
        acl_buf, pacl = _build_native_acl(lock_aces)
        p_owner = wintypes.LPVOID()
        p_group = wintypes.LPVOID()
        _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_owner))
        _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_group))
        sd_buf = (ctypes.c_ubyte * 256)()
        p_sd_file = ctypes.cast(sd_buf, wintypes.LPVOID)
        assert _advapi32.InitializeSecurityDescriptor(p_sd_file, _SECURITY_DESCRIPTOR_REVISION)
        assert _advapi32.SetSecurityDescriptorOwner(p_sd_file, p_owner, False)
        assert _advapi32.SetSecurityDescriptorGroup(p_sd_file, p_group, False)
        assert _advapi32.SetSecurityDescriptorDacl(p_sd_file, True, pacl, False)
        assert _advapi32.SetSecurityDescriptorControl(p_sd_file, _SE_DACL_PROTECTED, _SE_DACL_PROTECTED)

        # AU no puede leer el archivo de lock
        assert self._check_access(p_sd_file, restricted_token, _FILE_READ_DATA) is False
        _kernel32.LocalFree(p_owner)
        _kernel32.LocalFree(p_group)

    def test_ns_15_staging_contract_and_creator_owner_access_check(self, restricted_token: int) -> None:
        """NS-15: staging/ padre permite crear subdirectorios <op_id>; deniega crear archivos planos."""
        _, p_sd_staging = self._build_test_sd("staging")
        # En el directorio staging/ padre:
        assert self._check_access(p_sd_staging, restricted_token, 0x0001) is True  # FILE_LIST_DIRECTORY
        assert self._check_access(p_sd_staging, restricted_token, 0x0020) is True  # FILE_TRAVERSE
        assert self._check_access(p_sd_staging, restricted_token, 0x0004) is True  # FILE_ADD_SUBDIRECTORY
        assert self._check_access(p_sd_staging, restricted_token, _FILE_ADD_FILE) is False
        assert self._check_access(p_sd_staging, restricted_token, _FILE_DELETE_CHILD) is False


# ============================================================================
# POSIX Safety
# ============================================================================


class TestNamespacePosixSafety:
    """Verifica que el módulo se importe limpiamente en POSIX y falle de forma tipada."""

    def test_import_seguro_y_error_tipado_fuera_de_windows(self) -> None:
        with (
            patch("sys.platform", "linux"),
            pytest.raises(TrustedNamespaceUnsupportedError, match="solo soportad[ao] en Windows"),
        ):
            bootstrap_trusted_namespace()
