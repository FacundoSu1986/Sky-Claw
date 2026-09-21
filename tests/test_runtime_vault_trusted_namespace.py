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

import ast
import inspect
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
    STAGING_MULTI_USER_EFFECTIVE_ISOLATION,
    AncestorProvisioningError,
    CanonicalSecurityDescriptorContext,
    NamespaceAceSpec,
    NamespaceObjectNotDirectoryError,
    NamespaceOwnerNotPermittedError,
    NamespaceReparsePointError,
    PreexistingTrustedRegistryError,
    TrustedNamespaceError,
    TrustedNamespaceResult,
    TrustedNamespaceUnsupportedError,
    _bootstrap_trusted_namespace_at,
    _build_canonical_security_descriptor,
    _resolve_programdata_known_folder,
    apply_canonical_tgr_file_security,
    bootstrap_trusted_namespace,
    build_namespace_dacl_spec,
    inspect_namespace_object,
    verify_secured_file_by_handle,
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
        _build_native_acl_with_psids,
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

    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL


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

    def test_staging_multi_user_isolation_constant_declared(self) -> None:
        """P2: Constante explícita que declara el nivel de aislamiento verificado en staging."""
        assert STAGING_MULTI_USER_EFFECTIVE_ISOLATION == "PARTIAL"

    def test_bootstrap_trusted_namespace_signature_zero_arguments(self) -> None:
        """P1: La API pública bootstrap_trusted_namespace toma exactamente 0 argumentos."""
        sig = inspect.signature(bootstrap_trusted_namespace)
        assert len(sig.parameters) == 0, (
            f"bootstrap_trusted_namespace debe aceptar 0 argumentos, tiene {sig.parameters}"
        )

        # Verificación por AST
        import sky_claw.local.runtime_vault.trusted_namespace as ns_mod

        src_file = pathlib.Path(ns_mod.__file__)
        tree = ast.parse(src_file.read_text(encoding="utf-8"), filename=str(src_file))
        fn_nodes = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "bootstrap_trusted_namespace"
        ]
        assert len(fn_nodes) == 1
        fn = fn_nodes[0]
        assert len(fn.args.posonlyargs) == 0
        assert len(fn.args.args) == 0
        assert fn.args.vararg is None
        assert len(fn.args.kwonlyargs) == 0
        assert fn.args.kwarg is None

    @pytest.mark.skipif(sys.platform != "win32", reason="Resolver nativo solo en Windows")
    def test_resolve_programdata_known_folder_ignores_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P1: _resolve_programdata_known_folder NO usa os.environ['PROGRAMDATA'].

        Monkeypatch PROGRAMDATA a una ruta controlada por el atacante;
        _resolve_programdata_known_folder debe retornar la ruta autoritativa
        de SHGetKnownFolderPath, NO la del environment block.
        """
        attacker_path = "D:\\attacker-controlled"
        monkeypatch.setenv("PROGRAMDATA", attacker_path)

        result = _resolve_programdata_known_folder()
        assert str(result) != attacker_path, (
            f"_resolve_programdata_known_folder retornó la variable de entorno envenenada '{attacker_path}'"
        )
        # SHGetKnownFolderPath en cualquier Windows real retorna una ruta que contiene 'ProgramData'
        assert "ProgramData" in str(result), f"La ruta autoritativa no contiene 'ProgramData': '{result}'"

    def test_resolve_programdata_known_folder_posix_fails(self) -> None:
        """P1: _resolve_programdata_known_folder falla con TrustedNamespaceUnsupportedError en POSIX."""
        with patch("sky_claw.local.runtime_vault.trusted_namespace.sys") as mock_sys:
            mock_sys.platform = "linux"
            with pytest.raises(TrustedNamespaceUnsupportedError):
                _resolve_programdata_known_folder()

    @pytest.mark.skipif(sys.platform != "win32", reason="Resolver nativo solo en Windows")
    def test_bootstrap_trusted_namespace_uses_known_folder_not_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P1: bootstrap_trusted_namespace resuelve ProgramData por SHGetKnownFolderPath, no por os.environ."""
        calls: list[pathlib.Path] = []

        def spy_bootstrap(path: Any) -> TrustedNamespaceResult:
            calls.append(pathlib.Path(path))
            return TrustedNamespaceResult(success=True, root_path=pathlib.Path(path))

        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._bootstrap_trusted_namespace_at",
            spy_bootstrap,
        )
        attacker_path = "D:\\attacker-controlled"
        monkeypatch.setenv("PROGRAMDATA", attacker_path)

        res = bootstrap_trusted_namespace()
        assert res.success is True
        assert len(calls) == 1
        # No debe contener la ruta del atacante
        assert attacker_path not in str(calls[0]), f"bootstrap usó la ruta envenenada del env: '{calls[0]}'"
        # Debe terminar en Sky-Claw
        assert calls[0].name == "Sky-Claw"

    def test_runtime_vault_public_api_no_tgr_writer_with_pathname(self) -> None:
        """P1 / Anchor: La API pública de runtime_vault no exporta ningún TGR writer que reciba pathname."""
        import sky_claw.local.runtime_vault as rv_mod

        public_names = set(rv_mod.__all__)
        forbidden_writer_patterns = {
            "write_trusted_registry_atomically",
            "_write_trusted_registry_atomically_at",
            "write_trusted_goldens",
            "write_tgr",
        }
        exported_writers = public_names & forbidden_writer_patterns
        assert exported_writers == set(), (
            f"VIOLACIÓN: La API pública exporta TGR writers con pathname: {exported_writers}"
        )


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
        root_test.parent.mkdir(parents=True, exist_ok=True)
        with pytest.raises(AncestorProvisioningError) as exc_info:
            _bootstrap_trusted_namespace_at(root_test)
        assert "1307" in str(exc_info.value) or "1314" in str(exc_info.value)

    def test_ns_01_bootstrap_namespace_ausente_caso_a(
        self, tmp_path: pathlib.Path, mock_elevated_provisioning: None
    ) -> None:
        """NS-01: Bootstrap correcto sobre namespace inexistente (Caso A, orden de ancestros completo)."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        root_test.parent.mkdir(parents=True, exist_ok=True)

        result = _bootstrap_trusted_namespace_at(root_test)

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
            result = _bootstrap_trusted_namespace_at(root_test)
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
            _bootstrap_trusted_namespace_at(root_test)

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
            _bootstrap_trusted_namespace_at(root_test)

    def test_ns_05_archivo_donde_se_esperaba_directorio_falla_cerrado(
        self, tmp_path: pathlib.Path, mock_elevated_provisioning: None
    ) -> None:
        """NS-05: Archivo plano donde se esperaba un directorio falla cerrado."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        root_test.parent.mkdir(parents=True)
        root_test.write_text("archivo malicioso")

        with pytest.raises(NamespaceObjectNotDirectoryError, match="no es un directorio"):
            _bootstrap_trusted_namespace_at(root_test)

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
            _bootstrap_trusted_namespace_at(root_test)

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
            _bootstrap_trusted_namespace_at(root_test)

        assert not (root_test / "runtime_vault").exists()

    def test_provision_missing_parent_fails_closed(self, tmp_path: pathlib.Path) -> None:
        """P2: _provision_or_normalize_directory con padre inexistente falla cerrado sin crear ancestros.

        Verifica que NO se usa pathlib.mkdir(parents=True). Si el padre no fue
        previamente validado por el orden ancestors-first, el provisioning falla cerrado.
        """
        from sky_claw.local.runtime_vault.trusted_namespace import _provision_or_normalize_directory

        # Ruta con padre inexistente: el padre NO existe
        deep_child = tmp_path / "nonexistent_parent" / "child_dir"

        with pytest.raises(AncestorProvisioningError, match="ancestors-first"):
            _provision_or_normalize_directory(deep_child, "child_dir")

        # Verificar que NO se crearon ancestros implícitamente
        assert not (tmp_path / "nonexistent_parent").exists()
        assert not deep_child.exists()


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

    def test_inspect_namespace_object_broken_reparse_point_fails_closed(self, tmp_path: pathlib.Path) -> None:
        """P2: inspect_namespace_object sobre un reparse point roto falla cerrado con NamespaceReparsePointError (sin Path.exists)."""
        target_dir = tmp_path / "real_target_insp"
        target_dir.mkdir()
        junction_dir = tmp_path / "broken_junction_insp"

        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_dir), str(target_dir)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            pytest.skip("mklink /J no soportado o falló en este entorno")

        try:
            target_dir.rmdir()
            # Broken junction: inspect_namespace_object debe detectar reparse roto y fallar con NamespaceReparsePointError
            with pytest.raises(NamespaceReparsePointError, match="reparse point"):
                inspect_namespace_object(junction_dir)
        finally:
            subprocess.run(["cmd", "/c", "rmdir", str(junction_dir)], capture_output=True)

    def test_inspect_namespace_object_nonexistent_fails_closed(self, tmp_path: pathlib.Path) -> None:
        """P2: inspect_namespace_object sobre un objeto que no existe falla cerrado con TrustedNamespaceError."""
        nonexistent = tmp_path / "does_not_exist.txt"
        with pytest.raises(TrustedNamespaceError, match="no existe"):
            inspect_namespace_object(nonexistent)

    def test_verify_secured_file_by_handle_exact_dacl_mutants(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1: verify_secured_file_by_handle verifica estructuralmente la DACL y rechaza mutantes."""
        test_file = tmp_path / "tgr_verify_test.json"
        test_file.write_text("{}")

        # Configurar lectura de owner/group canónico y DACL protegida
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
            lambda h: (LOCAL_SYSTEM_SID, CANONICAL_NAMESPACE_PRIMARY_GROUP, True),
        )

        # 1. Caso Canónico: 3 ACEs exactas -> pasa
        canonical_aces = [
            (0x00, 0x00, 0x001F01FF, BUILTIN_ADMINISTRATORS_SID),
            (0x00, 0x00, 0x001F01FF, LOCAL_SYSTEM_SID),
            (0x00, 0x00, 0x00120089, AUTHENTICATED_USERS_SID),
        ]
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_dacl_aces",
            lambda h: canonical_aces,
        )
        verify_secured_file_by_handle(test_file)

        # 2. Mutant A: AU con FILE_ALL_ACCESS (0x001F01FF) -> falla
        mutant_a = [
            (0x00, 0x00, 0x001F01FF, BUILTIN_ADMINISTRATORS_SID),
            (0x00, 0x00, 0x001F01FF, LOCAL_SYSTEM_SID),
            (0x00, 0x00, 0x001F01FF, AUTHENTICATED_USERS_SID),
        ]
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_dacl_aces",
            lambda h: mutant_a,
        )
        with pytest.raises(TrustedNamespaceError, match="AccessMask no canónico"):
            verify_secured_file_by_handle(test_file)

        # 3. Mutant B: ACE extra para atacante -> falla conteo de ACEs
        mutant_b = [
            (0x00, 0x00, 0x001F01FF, BUILTIN_ADMINISTRATORS_SID),
            (0x00, 0x00, 0x001F01FF, LOCAL_SYSTEM_SID),
            (0x00, 0x00, 0x00120089, AUTHENTICATED_USERS_SID),
            (0x00, 0x00, 0x00120089, "S-1-5-21-9999-9999-9999-666"),
        ]
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_dacl_aces",
            lambda h: mutant_b,
        )
        with pytest.raises(TrustedNamespaceError, match="Conteo de ACEs en DACL.*no es canónico"):
            verify_secured_file_by_handle(test_file)

        # 4. Mutant C: DACL sin flag SE_DACL_PROTECTED -> falla
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
            lambda h: (LOCAL_SYSTEM_SID, CANONICAL_NAMESPACE_PRIMARY_GROUP, False),
        )
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_dacl_aces",
            lambda h: canonical_aces,
        )
        with pytest.raises(TrustedNamespaceError, match="SE_DACL_PROTECTED"):
            verify_secured_file_by_handle(test_file)

        # Restaurar flag de protección
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
            lambda h: (LOCAL_SYSTEM_SID, CANONICAL_NAMESPACE_PRIMARY_GROUP, True),
        )

        # 5. Mutant D: ACE con AceType != 0 (ej. ACCESS_DENIED = 0x01) -> falla
        mutant_d = [
            (0x00, 0x00, 0x001F01FF, BUILTIN_ADMINISTRATORS_SID),
            (0x00, 0x00, 0x001F01FF, LOCAL_SYSTEM_SID),
            (0x01, 0x00, 0x00120089, AUTHENTICATED_USERS_SID),
        ]
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_dacl_aces",
            lambda h: mutant_d,
        )
        with pytest.raises(TrustedNamespaceError, match="Tipo de ACE inválido"):
            verify_secured_file_by_handle(test_file)

        # 6. Mutant E: ACE con AceFlags no nulos en archivo (ej. 0x01) -> falla
        mutant_e = [
            (0x00, 0x00, 0x001F01FF, BUILTIN_ADMINISTRATORS_SID),
            (0x00, 0x00, 0x001F01FF, LOCAL_SYSTEM_SID),
            (0x00, 0x01, 0x00120089, AUTHENTICATED_USERS_SID),
        ]
        monkeypatch.setattr(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_dacl_aces",
            lambda h: mutant_e,
        )
        with pytest.raises(TrustedNamespaceError, match="AceFlags no nulos"):
            verify_secured_file_by_handle(test_file)


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

    def test_canonical_sd_context_lifetime_and_no_uaf(self, restricted_token: int) -> None:
        """P2: CanonicalSecurityDescriptorContext mantiene vivos SD, ACL y SIDs durante AccessCheck sin UAF."""
        with _build_canonical_security_descriptor("runtime_vault") as sd_ctx:
            assert sd_ctx.p_sd.value != 0
            assert len(sd_ctx.psids) >= 4  # Owner, Group y al menos 2 ACE SIDs
            # Durante el contexto, AccessCheck se ejecuta con memoria 100% válida
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_GENERIC_READ) is True

        # Al salir del contexto, los SIDs han sido liberados de forma segura
        assert len(sd_ctx.psids) == 0

    def test_ns_08_runtime_vault_access_check(self, restricted_token: int) -> None:
        """NS-08: AccessCheck en runtime_vault/ permite lectura/traverse y deniega add_file, add_subdir, delete_child."""
        with _build_canonical_security_descriptor("runtime_vault") as sd_ctx:
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_GENERIC_READ | _FILE_TRAVERSE) is True
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_ADD_FILE) is False
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_ADD_SUBDIRECTORY) is False
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_DELETE_CHILD) is False
            assert self._check_access(sd_ctx.p_sd, restricted_token, _DELETE) is False

    def test_ns_09_to_11_tgr_file_access_check(self, restricted_token: int) -> None:
        """NS-09 a NS-11: AccessCheck en trusted_goldens.json permite lectura y deniega write, delete, WRITE_DAC, WRITE_OWNER."""
        with _build_canonical_security_descriptor("trusted_goldens.json") as sd_ctx:
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_GENERIC_READ) is True
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_WRITE_DATA) is False
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_APPEND_DATA) is False
            assert self._check_access(sd_ctx.p_sd, restricted_token, _DELETE) is False
            assert self._check_access(sd_ctx.p_sd, restricted_token, _WRITE_DAC) is False
            assert self._check_access(sd_ctx.p_sd, restricted_token, _WRITE_OWNER) is False

    def test_ns_12_and_13_operations_and_golden_backups_access_check(self, restricted_token: int) -> None:
        """NS-12 y NS-13: AccessCheck en operations/ y golden_backups/ deniega creación a usuarios restringidos."""
        for name in ("operations", "golden_backups"):
            with _build_canonical_security_descriptor(name) as sd_ctx:
                assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_GENERIC_READ) is True
                assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_ADD_FILE) is False
                assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_ADD_SUBDIRECTORY) is False
                assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_DELETE_CHILD) is False

    def test_ns_14_locks_container_isolation_access_check(self, restricted_token: int) -> None:
        """NS-14: locks/ tiene ACE contenedor únicamente; un archivo *.lock dentro no tiene ACE para AU."""
        with _build_canonical_security_descriptor("locks") as sd_ctx_dir:
            # En el directorio, el usuario restringido puede listar y traverse
            assert self._check_access(sd_ctx_dir.p_sd, restricted_token, 0x0001 | 0x0020) is True

        # En un archivo .lock (que no hereda la ACE de AU porque flags=0x00), AU no tiene lectura
        # Creamos SD para el archivo mediante CanonicalSecurityDescriptorContext
        lock_aces = [
            NamespaceAceSpec(sid=BUILTIN_ADMINISTRATORS_SID, access_mask=_FILE_ALL_ACCESS, ace_flags=0),
            NamespaceAceSpec(sid=LOCAL_SYSTEM_SID, access_mask=_FILE_ALL_ACCESS, ace_flags=0),
        ]
        acl_buf, pacl, ace_psids = _build_native_acl_with_psids(lock_aces)
        psids: list[Any] = list(ace_psids)
        p_owner = wintypes.LPVOID()
        p_group = wintypes.LPVOID()
        _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_OWNER, ctypes.byref(p_owner))
        _advapi32.ConvertStringSidToSidW(CANONICAL_NAMESPACE_PRIMARY_GROUP, ctypes.byref(p_group))
        psids.extend([p_owner, p_group])

        sd_buf = (ctypes.c_ubyte * 256)()
        p_sd_file = ctypes.cast(sd_buf, wintypes.LPVOID)
        assert _advapi32.InitializeSecurityDescriptor(p_sd_file, _SECURITY_DESCRIPTOR_REVISION)
        assert _advapi32.SetSecurityDescriptorOwner(p_sd_file, p_owner, False)
        assert _advapi32.SetSecurityDescriptorGroup(p_sd_file, p_group, False)
        assert _advapi32.SetSecurityDescriptorDacl(p_sd_file, True, pacl, False)
        assert _advapi32.SetSecurityDescriptorControl(p_sd_file, _SE_DACL_PROTECTED, _SE_DACL_PROTECTED)

        with CanonicalSecurityDescriptorContext(sd_buf, acl_buf, psids, p_sd_file):
            # AU no puede leer el archivo de lock
            assert self._check_access(p_sd_file, restricted_token, _FILE_READ_DATA) is False

    def test_ns_15_staging_contract_and_creator_owner_access_check(self, restricted_token: int) -> None:
        """NS-15: staging/ padre permite crear subdirectorios <op_id>; deniega crear archivos planos."""
        with _build_canonical_security_descriptor("staging") as sd_ctx:
            # En el directorio staging/ padre:
            assert self._check_access(sd_ctx.p_sd, restricted_token, 0x0001) is True  # FILE_LIST_DIRECTORY
            assert self._check_access(sd_ctx.p_sd, restricted_token, 0x0020) is True  # FILE_TRAVERSE
            assert self._check_access(sd_ctx.p_sd, restricted_token, 0x0004) is True  # FILE_ADD_SUBDIRECTORY
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_ADD_FILE) is False
            assert self._check_access(sd_ctx.p_sd, restricted_token, _FILE_DELETE_CHILD) is False

    def test_ns_16_staging_multi_user_causal_oracle(self) -> None:
        """P2 / Staging Multi-User: Oráculo causal de AccessCheck.

        Verifica que:
        - Creator A (token creador con User A SID) tiene permisos completos para escribir y borrar su subdirectorio <op_id>.
        - Principal B (token restringido / Authenticated User no creador) tiene denegado el acceso de escritura y borrado en el subdirectorio de A.
        """
        h_proc = _kernel32.GetCurrentProcess()
        h_tok = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(h_proc, 0x0002 | 0x0008, ctypes.byref(h_tok)):
            pytest.skip("No se pudo abrir token de proceso para AccessCheck")

        try:
            buf_len = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(h_tok, 1, None, 0, ctypes.byref(buf_len))
            buf = (ctypes.c_ubyte * buf_len.value)()
            if not _advapi32.GetTokenInformation(
                h_tok, 1, ctypes.cast(buf, wintypes.LPVOID), buf_len.value, ctypes.byref(buf_len)
            ):
                pytest.skip("GetTokenInformation falló")
            user_info = ctypes.cast(buf, ctypes.POINTER(_SidAndAttributes)).contents
            user_str = wintypes.LPWSTR()
            if not _advapi32.ConvertSidToStringSidW(user_info.Sid, ctypes.byref(user_str)):
                pytest.skip("ConvertSidToStringSidW falló")
            creator_a_sid = user_str.value or ""
            _kernel32.LocalFree(user_str)

            # Token de impersonación para Creator A (restringido para remover Admin y verificar que el acceso es por la ACE de Creator)
            psid_admin = wintypes.LPVOID()
            _advapi32.ConvertStringSidToSidW(BUILTIN_ADMINISTRATORS_SID, ctypes.byref(psid_admin))
            try:
                sids_to_disable = (_SidAndAttributes * 1)()
                sids_to_disable[0].Sid = psid_admin
                sids_to_disable[0].Attributes = 0
                h_creator_restricted = wintypes.HANDLE()
                if not _advapi32.CreateRestrictedToken(
                    h_tok, 1, 1, sids_to_disable, 0, None, 0, None, ctypes.byref(h_creator_restricted)
                ):
                    pytest.skip("CreateRestrictedToken falló")

                try:
                    h_creator_imp = wintypes.HANDLE()
                    if not _advapi32.DuplicateTokenEx(
                        h_creator_restricted, 0x00020000 | 0x0008 | 0x0004, None, 2, 2, ctypes.byref(h_creator_imp)
                    ):
                        pytest.skip("DuplicateTokenEx falló")

                    try:
                        # 1. SD de la carpeta de Creator A:
                        aces_a = [
                            NamespaceAceSpec(BUILTIN_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0x00, "Admins"),
                            NamespaceAceSpec(LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS, 0x00, "SYSTEM"),
                            NamespaceAceSpec(creator_a_sid, 0x001301FF, 0x00, "Creator A"),
                            NamespaceAceSpec(AUTHENTICATED_USERS_SID, 0x00120089, 0x00, "AU"),
                        ]
                        acl_buf_a, pacl_a, psids_a = _build_native_acl_with_psids(aces_a)
                        p_owner_a = wintypes.LPVOID()
                        p_group_a = wintypes.LPVOID()
                        _advapi32.ConvertStringSidToSidW(creator_a_sid, ctypes.byref(p_owner_a))
                        _advapi32.ConvertStringSidToSidW(BUILTIN_ADMINISTRATORS_SID, ctypes.byref(p_group_a))
                        all_psids_a = list(psids_a) + [p_owner_a, p_group_a]
                        sd_buf_a = (ctypes.c_ubyte * 512)()
                        p_sd_a = ctypes.cast(sd_buf_a, wintypes.LPVOID)
                        assert _advapi32.InitializeSecurityDescriptor(p_sd_a, 1)
                        assert _advapi32.SetSecurityDescriptorOwner(p_sd_a, p_owner_a, False)
                        assert _advapi32.SetSecurityDescriptorGroup(p_sd_a, p_group_a, False)
                        assert _advapi32.SetSecurityDescriptorDacl(p_sd_a, True, pacl_a, False)
                        assert _advapi32.SetSecurityDescriptorControl(p_sd_a, 0x1000, 0x1000)

                        with CanonicalSecurityDescriptorContext(sd_buf_a, acl_buf_a, all_psids_a, p_sd_a):
                            # Creator A sobre su propia carpeta:
                            assert self._check_access(p_sd_a, int(h_creator_imp.value), _FILE_WRITE_DATA) is True
                            assert self._check_access(p_sd_a, int(h_creator_imp.value), _DELETE) is True
                            assert self._check_access(p_sd_a, int(h_creator_imp.value), _FILE_DELETE_CHILD) is True
                            assert self._check_access(p_sd_a, int(h_creator_imp.value), _FILE_GENERIC_READ) is True

                        # 2. SD de la carpeta de otro usuario (Other User):
                        other_user_sid = "S-1-5-21-9999-9999-9999-1001"
                        aces_other = [
                            NamespaceAceSpec(BUILTIN_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, 0x00, "Admins"),
                            NamespaceAceSpec(LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS, 0x00, "SYSTEM"),
                            NamespaceAceSpec(other_user_sid, 0x001301FF, 0x00, "Other Creator"),
                            NamespaceAceSpec(AUTHENTICATED_USERS_SID, 0x00120089, 0x00, "AU"),
                        ]
                        acl_buf_oth, pacl_oth, psids_oth = _build_native_acl_with_psids(aces_other)
                        p_owner_oth = wintypes.LPVOID()
                        p_group_oth = wintypes.LPVOID()
                        _advapi32.ConvertStringSidToSidW(other_user_sid, ctypes.byref(p_owner_oth))
                        _advapi32.ConvertStringSidToSidW(BUILTIN_ADMINISTRATORS_SID, ctypes.byref(p_group_oth))
                        all_psids_oth = list(psids_oth) + [p_owner_oth, p_group_oth]
                        sd_buf_oth = (ctypes.c_ubyte * 512)()
                        p_sd_oth = ctypes.cast(sd_buf_oth, wintypes.LPVOID)
                        assert _advapi32.InitializeSecurityDescriptor(p_sd_oth, 1)
                        assert _advapi32.SetSecurityDescriptorOwner(p_sd_oth, p_owner_oth, False)
                        assert _advapi32.SetSecurityDescriptorGroup(p_sd_oth, p_group_oth, False)
                        assert _advapi32.SetSecurityDescriptorDacl(p_sd_oth, True, pacl_oth, False)
                        assert _advapi32.SetSecurityDescriptorControl(p_sd_oth, 0x1000, 0x1000)

                        with CanonicalSecurityDescriptorContext(sd_buf_oth, acl_buf_oth, all_psids_oth, p_sd_oth):
                            # El usuario actuando como Principal B sobre la carpeta ajena:
                            assert self._check_access(p_sd_oth, int(h_creator_imp.value), _FILE_GENERIC_READ) is True
                            assert self._check_access(p_sd_oth, int(h_creator_imp.value), _FILE_WRITE_DATA) is False
                            assert self._check_access(p_sd_oth, int(h_creator_imp.value), _DELETE) is False
                            assert self._check_access(p_sd_oth, int(h_creator_imp.value), _FILE_DELETE_CHILD) is False

                    finally:
                        _kernel32.CloseHandle(h_creator_imp)
                finally:
                    _kernel32.CloseHandle(h_creator_restricted)
            finally:
                _kernel32.LocalFree(psid_admin)
        finally:
            _kernel32.CloseHandle(h_tok)


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
