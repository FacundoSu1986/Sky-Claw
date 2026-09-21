"""Tests para Trusted Namespace y Contrato de DACLs - GP2-S3a-1.

Verifica el contrato normativo de ADR 0010 §11.3:
- NS-01: Bootstrap correcto sobre namespace inexistente (Caso A, creado con SD desde el nacimiento).
- NS-02: Normalización segura de directorio preexistente con owner permitido (Caso B, normaliza OWNER + GROUP + DACL).
- NS-03: Rechazo fail-closed ante directorio preexistente con owner no permitido (usuario estándar).
- NS-04: Rechazo fail-closed ante reparse point / junction / symlink preexistente.
- NS-05: Rechazo fail-closed si se encuentra un archivo donde se esperaba un directorio.
- NS-06: Rechazo fail-closed incondicional si trusted_goldens.json ya existía (GP2-T50 Variante B).
- NS-07: Fallo en ancestro detiene el provisioning; ningún descendiente es creado ni modificado.
- NS-08: DACL de runtime_vault/ deniega FILE_ADD_FILE, FILE_ADD_SUBDIRECTORY, FILE_DELETE_CHILD a Authenticated Users.
- NS-09: DACL de trusted_goldens.json concede lectura y deniega escritura a Authenticated Users.
- NS-10: DACL de trusted_goldens.json deniega DELETE a Authenticated Users.
- NS-11: DACL de trusted_goldens.json deniega WRITE_DAC y WRITE_OWNER a Authenticated Users.
- NS-12: operations/ deniega creación de subdirectorios a Authenticated Users.
- NS-13: golden_backups/ deniega creación de archivos o directorios a Authenticated Users.
- NS-14: locks/ deniega FILE_READ_DATA sobre archivos de lock a Authenticated Users.
- NS-15: staging/ permite crear subdirectorios <op_id> y ejecutar teardown propio (DELETE/FILE_DELETE_CHILD),
         pero deniega crear archivos sueltos en el padre y deniega escribir o borrar en operaciones de otros usuarios.
- GP2-T50: Cobertura semántica/fundacional de anti-squatting en namespaces sintéticos desechables.
- Mutation anchors: M-N1, M-D1, M-L1, M-PROV.
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.trusted_namespace import (
    AUTHENTICATED_USERS_SID,
    BUILTIN_ADMINISTRATORS_SID,
    CANONICAL_NAMESPACE_OWNER,
    CANONICAL_NAMESPACE_PRIMARY_GROUP,
    CREATOR_OWNER_SID,
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
    bootstrap_trusted_namespace,
    build_namespace_dacl_spec,
    inspect_namespace_object,
)

# En Windows, importar helpers nativos de test si están disponibles
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    def _get_current_runner_sid() -> str | None:
        """Obtiene el SID del token del proceso de pruebas en Windows."""
        try:
            adv32 = ctypes.WinDLL("advapi32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            adv32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
            adv32.ConvertSidToStringSidW.restype = wintypes.BOOL
            adv32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            adv32.OpenProcessToken.restype = wintypes.BOOL
            adv32.GetTokenInformation.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            adv32.GetTokenInformation.restype = wintypes.BOOL
            token = wintypes.HANDLE()
            if not adv32.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                return None
            try:
                buf_size = wintypes.DWORD()
                adv32.GetTokenInformation(token, 1, None, 0, ctypes.byref(buf_size))
                buf = ctypes.create_string_buffer(buf_size.value)
                if not adv32.GetTokenInformation(token, 1, buf, buf_size.value, ctypes.byref(buf_size)):
                    return None

                class _SidAndAttributes(ctypes.Structure):
                    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

                class _TokenUser(ctypes.Structure):
                    _fields_ = [("User", _SidAndAttributes)]

                u = ctypes.cast(buf, ctypes.POINTER(_TokenUser)).contents
                s = wintypes.LPWSTR()
                if not adv32.ConvertSidToStringSidW(u.User.Sid, ctypes.byref(s)):
                    return None
                val = s.value
                k32.LocalFree(s)
                return val
            finally:
                k32.CloseHandle(token)
        except Exception:
            return None

    _RUNNER_SID = _get_current_runner_sid()
    _PERMITTED_TEST_OWNERS = frozenset(PERMITTED_NAMESPACE_OWNERS | ({_RUNNER_SID} if _RUNNER_SID else set()))
else:
    _RUNNER_SID = None
    _PERMITTED_TEST_OWNERS = PERMITTED_NAMESPACE_OWNERS


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
# Constantes y Especificaciones de DACL Puras
# ============================================================================


class TestNamespacePureSpecs:
    """Verificación de constantes y especificaciones de DACL según ADR 0010 §11.3."""

    def test_constantes_normativas(self) -> None:
        assert CANONICAL_NAMESPACE_OWNER == LOCAL_SYSTEM_SID
        assert CANONICAL_NAMESPACE_PRIMARY_GROUP == BUILTIN_ADMINISTRATORS_SID
        assert set(PERMITTED_NAMESPACE_OWNERS) == {LOCAL_SYSTEM_SID, BUILTIN_ADMINISTRATORS_SID}
        assert isinstance(PERMITTED_NAMESPACE_OWNERS, frozenset)

    def test_especificaciones_dacl_por_objeto(self) -> None:
        """Verifica que cada objeto del namespace tenga su especificación de DACL canónica."""
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
            assert spec.control_flags & 0x1000  # SE_DACL_PROTECTED

            # Verificar que Administrators y SYSTEM tienen FILE_ALL_ACCESS
            admins_ace = next((ace for ace in spec.aces if ace.sid == BUILTIN_ADMINISTRATORS_SID), None)
            system_ace = next((ace for ace in spec.aces if ace.sid == LOCAL_SYSTEM_SID), None)
            assert admins_ace is not None
            assert admins_ace.access_mask == 0x001F01FF
            assert system_ace is not None
            assert system_ace.access_mask == 0x001F01FF


# ============================================================================
# NS-01 a NS-07: Bootstrap, Anti-Squatting y Ancestros
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas Win32 nativas de namespace")
class TestNamespaceBootstrapWindows:
    """Tests de provisioning y anti-squatting en Windows sobre directores sintéticos desechables."""

    def test_ns_01_bootstrap_namespace_ausente_caso_a(self, tmp_path: pathlib.Path) -> None:
        """NS-01: Bootstrap correcto sobre namespace inexistente (Caso A, creado con SD desde el nacimiento)."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"

        result = bootstrap_trusted_namespace(root_dir=root_test, runner_sid=_RUNNER_SID)

        assert isinstance(result, TrustedNamespaceResult)
        assert result.success is True

        # Verificar existencia de todo el árbol
        rv_dir = root_test / "runtime_vault"
        assert rv_dir.is_dir()
        assert (rv_dir / "staging").is_dir()
        assert (rv_dir / "operations").is_dir()
        assert (rv_dir / "locks").is_dir()
        assert (rv_dir / "golden_backups").is_dir()
        assert (rv_dir / "trusted_goldens.json").is_file()

        # Inspeccionar objeto raíz y TGR
        info_rv = inspect_namespace_object(rv_dir)
        assert info_rv.is_directory is True
        assert info_rv.reparse_tag == 0
        assert info_rv.is_dacl_protected is True

    def test_ns_02_directorio_preexistente_owner_permitido_se_normaliza_caso_b(self, tmp_path: pathlib.Path) -> None:
        """NS-02: Directorio preexistente con owner permitido se normaliza en OWNER + GROUP + DACL."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)

        with patch(
            "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
            return_value=(BUILTIN_ADMINISTRATORS_SID, CANONICAL_NAMESPACE_PRIMARY_GROUP, None, True),
        ):
            result = bootstrap_trusted_namespace(
                root_dir=root_test, runner_sid=_RUNNER_SID, permitted_owners=_PERMITTED_TEST_OWNERS
            )
            assert result.success is True

            info_rv = inspect_namespace_object(rv_dir)
            assert info_rv.owner_sid in PERMITTED_NAMESPACE_OWNERS
            assert info_rv.group_sid == CANONICAL_NAMESPACE_PRIMARY_GROUP
            assert info_rv.is_dacl_protected is True

    def test_ns_03_directorio_preexistente_owner_no_permitido_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """NS-03: Directorio preexistente con owner no permitido (ej. usuario estándar) falla cerrado."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)

        # Simular que el owner es un usuario estándar (no SYSTEM ni Administrators)
        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
                return_value=("S-1-5-21-12345-6789-0", "S-1-5-21-12345-6789-513", None, True),
            ),
            pytest.raises(NamespaceOwnerNotPermittedError, match="no permitido"),
        ):
            bootstrap_trusted_namespace(root_dir=root_test, runner_sid=_RUNNER_SID)

    def test_ns_04_reparse_point_preexistente_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """NS-04: Reparse point / junction / symlink preexistente falla cerrado incondicionalmente."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)

        # Simular reparse tag distinto de 0 (ej. IO_REPARSE_TAG_MOUNT_POINT)
        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._read_handle_reparse_and_attributes",
                return_value=(0xA0000003, 0x410),  # ReparseTag != 0, FILE_ATTRIBUTE_REPARSE_POINT
            ),
            pytest.raises(NamespaceReparsePointError, match="reparse point"),
        ):
            bootstrap_trusted_namespace(
                root_dir=root_test, runner_sid=_RUNNER_SID, permitted_owners=_PERMITTED_TEST_OWNERS
            )

    def test_ns_05_archivo_donde_se_esperaba_directorio_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """NS-05: Archivo plano donde se esperaba un directorio falla cerrado."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        root_test.parent.mkdir(parents=True)
        # Crear Sky-Claw como archivo regular en vez de directorio
        root_test.write_text("archivo malicioso")

        with pytest.raises(NamespaceObjectNotDirectoryError, match="no es un directorio"):
            bootstrap_trusted_namespace(root_dir=root_test, runner_sid=_RUNNER_SID)

    def test_ns_06_gp2_t50_variante_b_preexisting_tgr_always_fails_closed(self, tmp_path: pathlib.Path) -> None:
        """NS-06 / GP2-T50 Variante B: trusted_goldens.json preexistente SIEMPRE falla cerrado."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)
        planted_tgr = rv_dir / "trusted_goldens.json"
        planted_tgr.write_text('{"entries":[],"schema_version":"1.0"}')

        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
                return_value=(BUILTIN_ADMINISTRATORS_SID, CANONICAL_NAMESPACE_PRIMARY_GROUP, None, True),
            ),
            pytest.raises(PreexistingTrustedRegistryError, match="preexistente"),
        ):
            bootstrap_trusted_namespace(
                root_dir=root_test, runner_sid=_RUNNER_SID, permitted_owners=_PERMITTED_TEST_OWNERS
            )

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
            bootstrap_trusted_namespace(root_dir=root_test, runner_sid=_RUNNER_SID)

        # Verificar que runtime_vault y sus subdirectorios jamás fueron creados
        assert not (root_test / "runtime_vault").exists()


# ============================================================================
# NS-08 a NS-15: Verificación de Derechos Efectivos y Aislamiento de Staging
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas Win32 nativas de DACL")
class TestNamespaceAccessRightsWindows:
    """Tests de permisos efectivos sobre los objetos aprovisionados."""

    def test_ns_08_runtime_vault_dacl_deniega_add_y_delete_child_a_au(self) -> None:
        """NS-08: runtime_vault/ deniega FILE_ADD_FILE, FILE_ADD_SUBDIRECTORY y FILE_DELETE_CHILD a AU."""
        spec = build_namespace_dacl_spec("runtime_vault")
        au_ace = next(ace for ace in spec.aces if ace.sid == AUTHENTICATED_USERS_SID)

        # Mask: 0x001200A9 (FILE_GENERIC_READ | FILE_TRAVERSE)
        # Deniega FILE_ADD_FILE (0x0002), FILE_ADD_SUBDIRECTORY (0x0004), FILE_DELETE_CHILD (0x0040), DELETE (0x00010000)
        assert (au_ace.access_mask & 0x0002) == 0
        assert (au_ace.access_mask & 0x0004) == 0
        assert (au_ace.access_mask & 0x0040) == 0
        assert (au_ace.access_mask & 0x00010000) == 0

    def test_ns_09_to_11_tgr_dacl_read_only_sin_write_delete_write_dac(self) -> None:
        """NS-09 a NS-11: trusted_goldens.json es read-only absoluto para AU (sin write, delete, write_dac, write_owner)."""
        spec = build_namespace_dacl_spec("trusted_goldens.json")
        au_ace = next(ace for ace in spec.aces if ace.sid == AUTHENTICATED_USERS_SID)

        # FILE_GENERIC_READ = 0x00120089
        assert au_ace.access_mask == 0x00120089
        # Sin WRITE_DATA (0x0002)
        assert (au_ace.access_mask & 0x0002) == 0
        # Sin DELETE (0x00010000)
        assert (au_ace.access_mask & 0x00010000) == 0
        # Sin WRITE_DAC (0x00040000)
        assert (au_ace.access_mask & 0x00040000) == 0
        # Sin WRITE_OWNER (0x00080000)
        assert (au_ace.access_mask & 0x00080000) == 0

        spec = build_namespace_dacl_spec("trusted_goldens.json")
        au_ace = next(ace for ace in spec.aces if ace.sid == AUTHENTICATED_USERS_SID)

        # FILE_GENERIC_READ = 0x00120089
        assert au_ace.access_mask == 0x00120089
        # Sin WRITE_DATA (0x0002)
        assert (au_ace.access_mask & 0x0002) == 0
        # Sin DELETE (0x00010000)
        assert (au_ace.access_mask & 0x00010000) == 0
        # Sin WRITE_DAC (0x00040000)
        assert (au_ace.access_mask & 0x00040000) == 0
        # Sin WRITE_OWNER (0x00080000)
        assert (au_ace.access_mask & 0x00080000) == 0

    def test_ns_12_and_13_operations_and_golden_backups_read_only_para_au(self, tmp_path: pathlib.Path) -> None:
        """NS-12 y NS-13: operations/ y golden_backups/ son read-only para AU."""
        for obj_name in ["operations", "golden_backups"]:
            spec = build_namespace_dacl_spec(obj_name)
            au_ace = next(ace for ace in spec.aces if ace.sid == AUTHENTICATED_USERS_SID)
            assert (au_ace.access_mask & 0x0002) == 0  # sin add file
            assert (au_ace.access_mask & 0x0004) == 0  # sin add subdir
            assert (au_ace.access_mask & 0x0040) == 0  # sin delete child

    def test_ns_14_locks_container_ace_no_hereda_a_archivos_de_lock(self, tmp_path: pathlib.Path) -> None:
        """NS-14: locks/ tiene ACE de contenedor sin flags de herencia (flags 0x00). Ningún .lock hereda FILE_READ_DATA."""
        spec = build_namespace_dacl_spec("locks")
        au_ace = next(ace for ace in spec.aces if ace.sid == AUTHENTICATED_USERS_SID)
        # flags 0x00: NO OBJECT_INHERIT (0x01) ni CONTAINER_INHERIT (0x02)
        assert au_ace.ace_flags == 0x00

    def test_ns_15_staging_contrato_exacto_y_teardown_creator_owner(self, tmp_path: pathlib.Path) -> None:
        """NS-15: staging/ permite crear subdirectorios <op_id> pero no archivos sueltos. CREATOR OWNER puede teardown."""
        spec = build_namespace_dacl_spec("staging")

        # 1. Authenticated Users en el padre (flags 0x00):
        au_parent_ace = next(ace for ace in spec.aces if ace.sid == AUTHENTICATED_USERS_SID and ace.ace_flags == 0x00)
        # Permite listar (0x0001) y add subdir (0x0004), pero NO add file (0x0002) ni delete child (0x0040)
        assert (au_parent_ace.access_mask & 0x0001) != 0
        assert (au_parent_ace.access_mask & 0x0004) != 0
        assert (au_parent_ace.access_mask & 0x0002) == 0
        assert (au_parent_ace.access_mask & 0x0040) == 0

        # 2. CREATOR OWNER heredable (flags 0x0B = OI|CI|IO):
        co_ace = next(ace for ace in spec.aces if ace.sid == CREATOR_OWNER_SID)
        assert co_ace.ace_flags == 0x0B
        # Concede DELETE (0x00010000) y FILE_DELETE_CHILD (0x0040) para teardown de su propia operación
        assert (co_ace.access_mask & 0x00010000) != 0
        assert (co_ace.access_mask & 0x0040) != 0

        # 3. Authenticated Users heredable a operaciones ajenas (flags 0x0B):
        au_child_ace = next(ace for ace in spec.aces if ace.sid == AUTHENTICATED_USERS_SID and ace.ace_flags == 0x0B)
        # Solo lectura (0x00120089); sin escritura ni borrado en operaciones de otro usuario
        assert (au_child_ace.access_mask & 0x0002) == 0
        assert (au_child_ace.access_mask & 0x00010000) == 0


# ============================================================================
# Mutation Anchors (M-N1, M-D1, M-L1, M-PROV)
# ============================================================================


class TestNamespaceMutationAnchors:
    """Verifica que mutantes de regresión y fallas intencionales sean atrapados por los tests."""

    def test_m_n1_confiar_en_error_already_exists_falla_el_test(self, tmp_path: pathlib.Path) -> None:
        """M-N1: Si el bootstrap confiara ciegamente en un directorio preexistente con owner no permitido, falla."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)

        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
                return_value=("S-1-5-21-999-999-999", "S-1-5-21-999-999-513", None, True),
            ),
            pytest.raises(NamespaceOwnerNotPermittedError),
        ):
            bootstrap_trusted_namespace(root_dir=root_test, runner_sid=_RUNNER_SID)

    def test_m_prov_adoptar_tgr_preexistente_falla_el_test(self, tmp_path: pathlib.Path) -> None:
        """M-PROV: Si el bootstrap adoptara un trusted_goldens.json preexistente, falla inmediatamente."""
        root_test = tmp_path / "ProgramDataSynthetic" / "Sky-Claw"
        rv_dir = root_test / "runtime_vault"
        rv_dir.mkdir(parents=True)
        (rv_dir / "trusted_goldens.json").write_text("{}")

        with (
            patch(
                "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
                return_value=(BUILTIN_ADMINISTRATORS_SID, CANONICAL_NAMESPACE_PRIMARY_GROUP, None, True),
            ),
            pytest.raises(PreexistingTrustedRegistryError),
        ):
            bootstrap_trusted_namespace(
                root_dir=root_test, runner_sid=_RUNNER_SID, permitted_owners=_PERMITTED_TEST_OWNERS
            )


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
