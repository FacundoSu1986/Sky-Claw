"""Tests del motor de Runtime Clone independiente (RV-3) para Runtime Vault.

TDD obligatorio con casos R3-T01 a R3-T28 y verificaciones adversariales.
Verifica:
- Autoridad de fuente: solo GoldenMasterVerificationResult VERIFIED o GoldenMasterDescriptor autorizan clon.
- Fail-closed: destino preexistente (DestinationExistsError), paths anidados, origen corrupto o inexistente.
- Pre-copy y post-copy Golden Master re-verification.
- Copia física real (shutil.copy2) con staging temporal hermano en el mismo volumen.
- Prohibición de hardlinks (os.link), symlinks, junctions y reparse points.
- Independencia física absoluta (dest.st_dev, dest.st_ino) != (src.st_dev, src.st_ino).
- Verificación integral de staging y destination (TreeDigest + CriticalFiles single-pass).
- Atomic publish via os.replace.
- Rollback completo y limpio de staging ante cualquier falla sin tocar el destino.
- Manejo de archivos de solo lectura en Windows.
- Aislamiento AST contra módulos candidate-only.
- Modelos inmutables (frozen + slots).
"""

from __future__ import annotations

import ast
import hashlib
import os
import pathlib
import shutil
import stat
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.clone import (
    create_runtime_clone,
    verify_physical_independence,
)
from sky_claw.local.runtime_vault.golden import verify_golden_master
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    DestinationExistsError,
    FileIdentity,
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    InventoryError,
    InventoryLinkError,
    RuntimeCloneDescriptor,
    RuntimeCloneError,
    RuntimeCloneResult,
    RuntimeIdentity,
    RuntimeVaultError,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files
from tests._symlink_guard import symlink_guard


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def mock_golden_environment(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]]:
    """Crea una estructura sintética completa y un Golden Master verificado."""
    golden_root = tmp_path / "Golden_Master_Stock"
    golden_root.mkdir(parents=True)

    # Archivo ejecutable crítico
    exe_bytes = b"MZ\x90\x00synthetic_skyrim_game_binary_content"
    (golden_root / "SkyrimSE.exe").write_bytes(exe_bytes)

    # Launcher crítico
    launcher_bytes = b"MZ\x90\x00synthetic_launcher_binary_content"
    (golden_root / "SkyrimSELauncher.exe").write_bytes(launcher_bytes)

    # DLL crítico
    dll_bytes = b"\x7fELF_or_PE_dll_bink_video_codec_content"
    (golden_root / "bink2w64.dll").write_bytes(dll_bytes)

    # Archivo de datos en subdirectorio
    data_dir = golden_root / "Data"
    data_dir.mkdir()
    (data_dir / "Skyrim.esm").write_bytes(b"TES4_MASTER_PLUGIN_DATA_BLOCK")
    (data_dir / "Update.esm").write_bytes(b"TES4_UPDATE_PLUGIN_DATA_BLOCK")

    # Subdirectorio con caracteres unicode y espacios
    extra_dir = golden_root / "Extra Tools"
    extra_dir.mkdir()
    (extra_dir / "config_🔧.ini").write_text("sLanguage=ENGLISH\n", encoding="utf-8")

    # Calcular TreeDigest
    files = inventory_tree(golden_root)
    tree_digest = tree_digest_from_files(files)
    runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")

    critical_expectations = [
        CriticalFileExpectation(
            rel_path="SkyrimSE.exe",
            expected_digest=_sha256(exe_bytes),
            expected_size=len(exe_bytes),
        ),
        CriticalFileExpectation(
            rel_path="SkyrimSELauncher.exe",
            expected_digest=_sha256(launcher_bytes),
            expected_size=len(launcher_bytes),
        ),
        CriticalFileExpectation(
            rel_path="bink2w64.dll",
            expected_digest=_sha256(dll_bytes),
            expected_size=len(dll_bytes),
        ),
    ]

    golden_res = verify_golden_master(
        candidate=golden_root,
        expected_tree=tree_digest,
        expected_runtime=runtime_id,
        observed_runtime=runtime_id,
        critical_expectations=critical_expectations,
    )

    assert golden_res.state is VerificationState.VERIFIED
    assert golden_res.descriptor is not None

    return golden_root, golden_res, critical_expectations


# ==============================================================================
# SUITE REQUERIDA CANÓNICA R3-T01 A R3-T28
# ==============================================================================


class TestRuntimeCloneSuiteCanonico:
    """Suite canónica de pruebas para RV-3: R3-T01 a R3-T28."""

    def test_r3_t01_source_authority_con_golden_verificado_autoriza_clon(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T01: Source authority verification con GoldenMasterVerificationResult y GoldenMasterDescriptor."""
        golden_root, golden_res, critical_expectations = mock_golden_environment
        dest_path = tmp_path / "Skyrim_Runtime_Clone"

        # Caso 1: Pasando GoldenMasterVerificationResult verificado
        res = create_runtime_clone(
            golden_source=golden_res,
            destination=dest_path,
            critical_expectations=critical_expectations,
        )

        assert res.state is VerificationState.VERIFIED
        assert res.success is True
        assert res.descriptor is not None
        assert res.descriptor.role == "runtime_clone"
        assert res.descriptor.location == dest_path.resolve()
        assert res.physical_independence_verified is True

        # Caso 2: Pasando GoldenMasterDescriptor directamente a otro destino
        dest_path2 = tmp_path / "Skyrim_Runtime_Clone_2"
        assert golden_res.descriptor is not None
        res2 = create_runtime_clone(
            golden_source=golden_res.descriptor,
            destination=dest_path2,
            critical_expectations=critical_expectations,
        )
        assert res2.state is VerificationState.VERIFIED
        assert res2.success is True
        assert res2.descriptor is not None
        assert res2.descriptor.location == dest_path2.resolve()

    def test_r3_t02_rechazo_de_golden_no_verificado_o_rol_invalido(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T02: Rejection of unverified or non-reference golden candidate/descriptor."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest"

        # 1. GoldenMasterVerificationResult con estado FAILED
        res_failed = GoldenMasterVerificationResult(
            state=VerificationState.FAILED,
            message="Fallo simulado en golden",
            tree_result=None,
            runtime_result=None,
            critical_results=(),
            descriptor=None,
        )
        with pytest.raises(RuntimeCloneError, match="(?i)no verificad|failed|autoridad"):
            create_runtime_clone(res_failed, dest, critical_expectations=critical)

        # 2. GoldenMasterVerificationResult con estado UNKNOWN
        res_unknown = GoldenMasterVerificationResult(
            state=VerificationState.UNKNOWN,
            message="Estado desconocido",
            tree_result=None,
            runtime_result=None,
            critical_results=(),
            descriptor=None,
        )
        with pytest.raises(RuntimeCloneError, match="(?i)no verificad|unknown|autoridad"):
            create_runtime_clone(res_unknown, dest, critical_expectations=critical)

        # 3. Path crudo o tipo no autorizado
        with pytest.raises((RuntimeCloneError, TypeError)):
            create_runtime_clone(
                golden_root,  # type: ignore[arg-type]
                dest,
                critical_expectations=critical,
            )

    def test_r3_t03_inmutabilidad_y_post_init_runtime_clone_descriptor_y_result(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """R3-T03: Immutability & post_init validation of RuntimeCloneDescriptor and RuntimeCloneResult."""
        runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        digest = TreeDigest(digest="a" * 64, files=10, bytes=1000)
        loc = tmp_path / "runtime"

        # Descriptor solo admite role='runtime_clone'
        with pytest.raises(ValueError, match="role"):
            RuntimeCloneDescriptor(
                location=loc,
                runtime_identity=runtime_id,
                tree_digest=digest,
                role="invalid_role",
            )

        desc = RuntimeCloneDescriptor(
            location=loc,
            runtime_identity=runtime_id,
            tree_digest=digest,
            role="runtime_clone",
            source_golden=tmp_path / "golden",
        )

        # Frozen: no permite asignación
        with pytest.raises((AttributeError, TypeError)):
            desc.role = "other"  # type: ignore[misc]

        # RuntimeCloneResult VERIFIED requiere tree_result VERIFIED, physical_independence True y descriptor no nulo
        tree_res_verified = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=digest,
            observed=digest,
        )

        with pytest.raises(ValueError, match="descriptor"):
            RuntimeCloneResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res_verified,
                physical_independence_verified=True,
                descriptor=None,
            )

        with pytest.raises(ValueError, match="(?i)independencia"):
            RuntimeCloneResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res_verified,
                physical_independence_verified=False,
                descriptor=desc,
            )

        # Estado FAILED no puede tener descriptor
        with pytest.raises(ValueError, match="descriptor"):
            RuntimeCloneResult(
                state=VerificationState.FAILED,
                descriptor=desc,
            )

    def test_r3_t04_fail_closed_origen_inexistente_o_no_directorio(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """R3-T04: Fail-closed on missing source directory or non-directory."""
        runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        digest = TreeDigest(digest="b" * 64, files=1, bytes=10)

        # Origen que no existe
        non_existent_source = tmp_path / "does_not_exist"
        desc_missing = GoldenMasterDescriptor(
            location=non_existent_source,
            runtime_identity=runtime_id,
            tree_digest=digest,
            role="reference_only",
        )

        dest = tmp_path / "runtime_dest"
        with pytest.raises((RuntimeCloneError, InventoryError)):
            create_runtime_clone(desc_missing, dest)

        # Origen que es un archivo regular en vez de directorio
        file_source = tmp_path / "file_source.txt"
        file_source.write_text("not a dir")
        desc_file = GoldenMasterDescriptor(
            location=file_source,
            runtime_identity=runtime_id,
            tree_digest=digest,
            role="reference_only",
        )
        with pytest.raises((RuntimeCloneError, InventoryError)):
            create_runtime_clone(desc_file, dest)

    def test_r3_t05_fail_closed_origen_igual_a_destino_o_anidados(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T05: Fail-closed when source == destination or destination inside source / source inside destination."""
        golden_root, golden_res, critical = mock_golden_environment

        # 1. Origen idéntico a destino
        with pytest.raises(RuntimeCloneError, match="(?i)idéntic|anidad|mismo"):
            create_runtime_clone(golden_res, golden_root, critical_expectations=critical)

        # 2. Destino dentro del origen
        nested_dest = golden_root / "Subdir" / "Runtime"
        with pytest.raises(RuntimeCloneError, match="(?i)anidad|subpath|dentro"):
            create_runtime_clone(golden_res, nested_dest, critical_expectations=critical)

        # 3. Origen dentro del destino
        parent_dest = golden_root.parent
        with pytest.raises(RuntimeCloneError, match="(?i)anidad|subpath|dentro"):
            create_runtime_clone(golden_res, parent_dest, critical_expectations=critical)

    def test_r3_t06_fail_closed_destino_preexistente_destination_exists_error(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T06: Fail-closed when destination already exists (DestinationExistsError, no overwrite, no delete)."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Preexisting_Destination"
        dest.mkdir()
        marker_file = dest / "preexisting_user_file.txt"
        marker_file.write_text("DATOS IMPORTANTES DEL USUARIO")

        with pytest.raises(DestinationExistsError, match="(?i)ya existe"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # Confirmar que no se borró ni modificó el destino
        assert dest.exists()
        assert marker_file.exists()
        assert marker_file.read_text() == "DATOS IMPORTANTES DEL USUARIO"

    def test_r3_t07_pre_copy_golden_revalidation_detecta_tampering_y_aborta_sin_copiar(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T07: Pre-copy Golden revalidation (detects pre-copy tampering/corruption and aborts before copy)."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Tamper"

        # Tampering: mutamos un archivo en el Golden después de haber obtenido el resultado de verificación
        (golden_root / "SkyrimSE.exe").write_bytes(b"CORRUPTED_BINARY_BYTES")

        with pytest.raises(RuntimeCloneError, match="(?i)pre-copy|tamper|corrupt|revalidation"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # Confirmar que dest no existe y que no quedaron directorios staging
        assert not dest.exists()
        staging_dirs = list(tmp_path.glob(".*skyclaw-staging*"))
        assert len(staging_dirs) == 0

    def test_r3_t08_creacion_directorio_staging_hermano_en_mismo_volumen(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T08: Staging sibling directory creation on same volume with unique identifier."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Staging_Test"

        staging_paths_observed: list[pathlib.Path] = []
        orig_mkdir = pathlib.Path.mkdir

        def tracking_mkdir(self: pathlib.Path, *args: object, **kwargs: object) -> None:
            if "skyclaw-staging" in self.name:
                staging_paths_observed.append(self)
            orig_mkdir(self, *args, **kwargs)

        with patch.object(pathlib.Path, "mkdir", side_effect=tracking_mkdir, autospec=True):
            res = create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert res.success is True
        assert len(staging_paths_observed) >= 1
        staging_dir = staging_paths_observed[0]
        # El staging debe ser hermano de dest (mismo parent / mismo volumen)
        assert staging_dir.parent == dest.parent
        assert "skyclaw-staging" in staging_dir.name
        # Tras publish exitoso, staging ya no existe y dest sí
        assert not staging_dir.exists()
        assert dest.exists()

    def test_r3_t09_copia_fisica_shutil_copy2_preserva_arbol_y_bytes(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T09: Physical copy (shutil.copy2) preserving file tree structure and bytes."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Physical_Copy"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True

        # Verificar cada archivo copiado
        src_files = inventory_tree(golden_root)
        dest_files = inventory_tree(dest)

        assert len(src_files) == len(dest_files)
        src_map = {f.rel_path: f for f in src_files}
        dest_map = {f.rel_path: f for f in dest_files}

        for rel_path, src_f in src_map.items():
            assert rel_path in dest_map
            dest_f = dest_map[rel_path]
            assert dest_f.size == src_f.size
            assert dest_f.digest == src_f.digest

            # Contenido en disco idéntico byte a byte
            assert (dest / rel_path).read_bytes() == (golden_root / rel_path).read_bytes()

    @symlink_guard
    def test_r3_t10_rechazo_de_symlinks_junctions_reparse_points(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T10: Reparse points / symlinks / junctions rejection in source, staging, and destination."""
        golden_root, golden_res, critical = mock_golden_environment

        # Crear un symlink en el Golden
        target = golden_root / "SkyrimSE.exe"
        link_path = golden_root / "Data" / "SkyrimSE_link.exe"

        try:
            os.symlink(target, link_path)
        except (OSError, NotImplementedError):
            pytest.skip("Sistema de archivos sin privilegios para crear symlinks")

        dest = tmp_path / "Runtime_Dest_Symlink"
        with pytest.raises((RuntimeCloneError, InventoryLinkError)):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

    def test_r3_t11_verificacion_independencia_fisica_st_dev_st_ino(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T11: Inode / device physical independence verification (dest.st_dev, dest.st_ino) != (src.st_dev, src.st_ino)."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Independence"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True

        files = inventory_tree(golden_root)
        indep = verify_physical_independence(golden_root, dest, files)
        assert indep is True

        # Auditoría directa archivo por archivo
        for f in files:
            src_stat = (golden_root / f.rel_path).stat()
            dest_stat = (dest / f.rel_path).stat()
            # En mismo volumen NTFS/POSIX, el inode o file_id DEBE ser distinto
            if src_stat.st_dev == dest_stat.st_dev:
                assert src_stat.st_ino != dest_stat.st_ino, f"Hardlink detectado en {f.rel_path}"

    def test_r3_t12_staging_tree_digest_calculo_y_match_con_golden_esperado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T12: Staging TreeDigest calculation & match against expected Golden TreeDigest."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Tree_Digest"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True
        assert res.tree_result is not None
        assert res.tree_result.state is VerificationState.VERIFIED
        assert golden_res.descriptor is not None
        assert res.tree_result.observed == golden_res.descriptor.tree_digest

    def test_r3_t13_staging_archivos_criticos_verificacion_sha256_single_pass(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T13: Staging critical files verification against expected SHA256 hashes using sealed FileIdentity."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Critical_Files"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True
        assert len(res.critical_results) == 3
        for c in res.critical_results:
            assert c.state is VerificationState.VERIFIED
            assert c.success is True
            assert c.observed_digest is not None
            assert c.observed_digest.lower() == c.expected_digest.lower()

    def test_r3_t14_atomic_publish_solo_tras_verificacion_staging_exitosa(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T14: Atomic publish via os.replace / rename from staging to destination only after staging verification passes."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Atomic_Publish"

        publish_called = False
        orig_replace = os.replace

        def tracking_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            nonlocal publish_called
            publish_called = True
            orig_replace(src, dst)

        with patch("os.replace", side_effect=tracking_replace):
            res = create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert res.success is True
        assert publish_called is True
        assert dest.exists()

    def test_r3_t15_post_publish_destination_verification(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T15: Post-publish destination verification (re-computes TreeDigest, checks reparse points, critical files, independence)."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Post_Publish"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True

        # Re-verificar que el destination final cumple todos los contratos
        dest_files = inventory_tree(dest)
        dest_digest = tree_digest_from_files(dest_files)
        assert golden_res.descriptor is not None
        assert dest_digest == golden_res.descriptor.tree_digest

    def test_r3_t16_post_copy_golden_master_re_verification(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T16: Post-copy Golden Master re-verification (ensures source Golden was not mutated or degraded)."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Post_Golden_Recheck"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True

        # Re-verificar que el Golden Master original sigue 100% intacto
        golden_files_after = inventory_tree(golden_root)
        golden_digest_after = tree_digest_from_files(golden_files_after)
        assert golden_res.descriptor is not None
        assert golden_digest_after == golden_res.descriptor.tree_digest

    def test_r3_t17_rollback_limpia_staging_ante_fallo_de_copia(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T17: Rollback cleans up staging directory on copy failure without touching destination."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Copy_Fail"

        # Simular fallo durante shutil.copy2
        def failing_copy2(*args: object, **kwargs: object) -> None:
            raise OSError("Fallo simulado de disco durante copia física")

        with (
            patch("shutil.copy2", side_effect=failing_copy2),
            pytest.raises(RuntimeCloneError, match="(?i)disco|copia|staging"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # Destino no debe existir y no deben quedar staging residuales
        assert not dest.exists()
        stagings = list(tmp_path.glob(".*skyclaw-staging*"))
        assert len(stagings) == 0

    def test_r3_t18_rollback_limpia_staging_ante_fallo_de_verificacion(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T18: Rollback cleans up staging directory on verification failure without touching destination."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Verify_Fail"

        # Enganchar en inventory_tree para corromper el staging antes de que termine su verificación
        orig_inventory = inventory_tree

        def sabotaging_inventory(root: pathlib.Path) -> tuple[FileIdentity, ...]:
            if "skyclaw-staging" in root.name:
                # Corrompemos un archivo en staging
                corrupt_target = root / "SkyrimSE.exe"
                if corrupt_target.exists():
                    corrupt_target.write_bytes(b"SABOTAGED_STAGING_BYTES")
            return orig_inventory(root)

        with (
            patch("sky_claw.local.runtime_vault.clone.inventory_tree", side_effect=sabotaging_inventory),
            pytest.raises(RuntimeCloneError, match="(?i)verificación|digest|staging"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # Destino no debe existir y staging debe estar limpio
        assert not dest.exists()
        stagings = list(tmp_path.glob(".*skyclaw-staging*"))
        assert len(stagings) == 0

    def test_r3_t19_jerarquia_de_excepciones_runtime_clone_error(self) -> None:
        """R3-T19: Exception hierarchy (RuntimeCloneError, DestinationExistsError subclassing RuntimeVaultError)."""
        assert issubclass(RuntimeCloneError, RuntimeVaultError)
        assert issubclass(DestinationExistsError, RuntimeCloneError)
        assert issubclass(DestinationExistsError, RuntimeVaultError)

    def test_r3_t20_ast_isolation_check_sin_imports_candidate_only(self) -> None:
        """R3-T20: AST isolation check (no candidate-only / unmerged module imports)."""
        import sky_claw.local.runtime_vault as pkg

        pkg_dir = pathlib.Path(pkg.__file__).parent
        py_files = list(pkg_dir.glob("*.py"))
        assert len(py_files) >= 5

        forbidden_names = {
            "artifact_digest",
            "texgen_visibility",
            "handoffs",
            "ritual_runner",
        }

        for py_path in py_files:
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for forbidden in forbidden_names:
                            assert forbidden not in alias.name, f"Import prohibido '{alias.name}' en {py_path.name}"
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    for forbidden in forbidden_names:
                        assert forbidden not in mod, f"ImportFrom prohibido '{mod}' en {py_path.name}"

    def test_r3_t21_rechazo_de_hardlinks_falla_independencia_fisica(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T21: Rejection of hardlinks (os.link) - fails physical independence."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Hardlink_Test"

        # Simular una implementación tramposa que use os.link en vez de shutil.copy2
        def hardlink_copy(src: str, dst: str, *args: object, **kwargs: object) -> None:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)

        with (
            patch("shutil.copy2", side_effect=hardlink_copy),
            pytest.raises(RuntimeCloneError, match="(?i)independencia|hardlink"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # Destino no debe crearse
        assert not dest.exists()

    def test_r3_t22_manejo_atributos_read_only_en_archivos_origen_y_rollback(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T22: Handling of read-only attributes on source files during staging copy & rollback."""
        golden_root, golden_res, critical = mock_golden_environment

        # Marcar un archivo de origen como read-only
        ro_file = golden_root / "Data" / "Skyrim.esm"
        orig_mode = ro_file.stat().st_mode
        try:
            ro_file.chmod(stat.S_IREAD)

            # Caso A: Copia exitosa preservando lectura
            dest = tmp_path / "Runtime_Dest_ReadOnly"
            # Re-verificar golden con el nuevo modo para actualizar hash/res si correspondiera
            files = inventory_tree(golden_root)
            digest = tree_digest_from_files(files)
            golden_desc = GoldenMasterDescriptor(
                location=golden_root,
                runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
                tree_digest=digest,
            )

            res = create_runtime_clone(golden_desc, dest, critical_expectations=critical)
            assert res.success is True
            assert dest.exists()

            # Caso B: Rollback con archivos read-only en staging
            dest_fail = tmp_path / "Runtime_Dest_ReadOnly_Fail"

            def failing_step(*args: object, **kwargs: object) -> None:
                raise OSError("Fallo para forzar rollback con archivos read-only")

            with (
                patch("sky_claw.local.runtime_vault.clone.verify_physical_independence", side_effect=failing_step),
                pytest.raises(RuntimeCloneError),
            ):
                create_runtime_clone(golden_desc, dest_fail, critical_expectations=critical)

            # Staging debió eliminarse limpiamente sin PermissionError
            assert not dest_fail.exists()
            assert len(list(tmp_path.glob(".*skyclaw-staging*"))) == 0

        finally:
            ro_file.chmod(orig_mode)

    def test_r3_t23_deteccion_mutacion_concurrente_durante_copia_y_rollback(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T23: Concurrent modification during copy detection and rollback."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Concurrent_Mut"

        # Simular mutación en el Golden mientras se realiza la copia
        orig_copy2 = shutil.copy2

        def mutating_copy2(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object) -> str:
            res = orig_copy2(src, dst, **kwargs)
            # Modificar archivo en origen en medio de la copia
            if "SkyrimSE.exe" in str(src):
                (golden_root / "Data" / "Update.esm").write_bytes(b"CONCURRENT_MUTATION_DATA")
            return res

        with patch("shutil.copy2", side_effect=mutating_copy2), pytest.raises(RuntimeCloneError):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()
        assert len(list(tmp_path.glob(".*skyclaw-staging*"))) == 0

    def test_r3_t24_rechazo_de_origen_vacio_inventory_error(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """R3-T24: Empty source rejection (InventoryError)."""
        empty_dir = tmp_path / "Empty_Golden"
        empty_dir.mkdir()

        digest = TreeDigest(digest="c" * 64, files=0, bytes=0)
        desc = GoldenMasterDescriptor(
            location=empty_dir,
            runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            tree_digest=digest,
        )

        dest = tmp_path / "Runtime_Dest_Empty"
        with pytest.raises((RuntimeCloneError, InventoryError)):
            create_runtime_clone(desc, dest)

    def test_r3_t25_normalizacion_de_paths_y_seguridad_anti_traversal(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T25: Path normalization and anti-traversal safety."""
        golden_root, golden_res, critical = mock_golden_environment

        # Destino con segmentos relativos / redundantes
        redundant_dest = tmp_path / "sub" / ".." / "Runtime_Dest_Normalized"
        res = create_runtime_clone(golden_res, redundant_dest, critical_expectations=critical)
        assert res.success is True
        assert res.descriptor is not None
        assert res.descriptor.location == redundant_dest.resolve()
        assert redundant_dest.resolve().exists()

    def test_r3_t26_single_pass_sealed_file_identity_reuse(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T26: Single-pass sealed FileIdentity reuse across tree digest and critical files."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Single_Pass"

        inventory_calls: list[pathlib.Path] = []
        orig_inventory = inventory_tree

        def counting_inventory(root: pathlib.Path) -> tuple[FileIdentity, ...]:
            inventory_calls.append(root)
            return orig_inventory(root)

        with patch("sky_claw.local.runtime_vault.clone.inventory_tree", side_effect=counting_inventory):
            res = create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert res.success is True
        # Debe haber exactamente 4 llamadas a inventory_tree:
        # 1. Pre-copy golden revalidation
        # 2. Staging verification
        # 3. Post-publish destination verification
        # 4. Post-copy golden re-verification
        # Cero llamadas adicionales de re-escaneo para archivos críticos.
        assert len(inventory_calls) == 4

    def test_r3_t27_semantica_verification_state_unknown_no_es_verified(self) -> None:
        """R3-T27: VerificationState semantics (UNKNOWN != VERIFIED)."""
        assert VerificationState.UNKNOWN != VerificationState.VERIFIED
        assert VerificationState.UNKNOWN.value != VerificationState.VERIFIED.value

        res_unknown = RuntimeCloneResult(
            state=VerificationState.UNKNOWN,
            message="No verificado",
        )
        assert res_unknown.success is False
        assert res_unknown.descriptor is None

    def test_r3_t28_integracion_end_to_end_create_runtime_clone(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T28: Full end-to-end integration test of create_runtime_clone on mock tree."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Skyrim_Playable_Runtime"

        res = create_runtime_clone(
            golden_source=golden_res,
            destination=dest,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.VERIFIED
        assert res.success is True
        assert res.message == ""
        assert res.physical_independence_verified is True

        # Validar descriptor
        assert res.descriptor is not None
        assert res.descriptor.role == "runtime_clone"
        assert res.descriptor.location == dest.resolve()
        assert golden_res.descriptor is not None
        assert res.descriptor.tree_digest == golden_res.descriptor.tree_digest
        assert res.descriptor.runtime_identity == golden_res.descriptor.runtime_identity
        assert res.descriptor.source_golden == golden_root.resolve()

        # Validar archivos en destino
        assert (dest / "SkyrimSE.exe").exists()
        assert (dest / "SkyrimSELauncher.exe").exists()
        assert (dest / "bink2w64.dll").exists()
        assert (dest / "Data" / "Skyrim.esm").exists()
        assert (dest / "Extra Tools" / "config_🔧.ini").exists()

        # Validar limpieza de staging
        stagings = list(tmp_path.glob(".*skyclaw-staging*"))
        assert len(stagings) == 0


# ==============================================================================
# MUTATION GATES: M-R3-01 A M-R3-10
# ==============================================================================


class TestMutationScenariosRV3:
    """Batería de mutaciones M-R3-01 a M-R3-10 para validar sensibilidad de la suite."""

    def test_m_r3_01_mutante_bypass_pre_copy_golden_revalidation_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-01: Bypass Golden pre-copy revalidation -> caught by R3-T07."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M01"

        # Corromper Golden antes de la llamada
        (golden_root / "SkyrimSE.exe").write_bytes(b"MUTATED_BEFORE_COPY")

        with pytest.raises(RuntimeCloneError, match="(?i)pre-copy|tamper|revalidation"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()

    def test_m_r3_02_mutante_sobrescribir_destino_preexistente_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-02: Overwrite existing destination -> caught by R3-T06."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M02"
        dest.mkdir()
        canary = dest / "important_data.txt"
        canary.write_text("NO_SOBREESCRIBIR")

        with pytest.raises(DestinationExistsError, match="(?i)ya existe"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert canary.exists()
        assert canary.read_text() == "NO_SOBREESCRIBIR"

    def test_m_r3_03_mutante_usar_hardlinks_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-03: Use hardlinks (os.link) -> caught by R3-T11 / R3-T21."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M03"

        def link_instead_of_copy(src: str, dst: str, *args: object, **kwargs: object) -> None:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)

        with (
            patch("shutil.copy2", side_effect=link_instead_of_copy),
            pytest.raises(RuntimeCloneError, match="(?i)independencia|hardlink"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()

    @symlink_guard
    def test_m_r3_04_mutante_permitir_symlinks_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-04: Allow symlinks/junctions -> caught by R3-T10."""
        golden_root, golden_res, critical = mock_golden_environment
        target = golden_root / "SkyrimSE.exe"
        link_path = golden_root / "Data" / "SkyrimSE_link.exe"

        try:
            os.symlink(target, link_path)
        except (OSError, NotImplementedError):
            pytest.skip("Sistema de archivos sin privilegios para symlinks")

        dest = tmp_path / "Runtime_Dest_M04"
        with pytest.raises((RuntimeCloneError, InventoryLinkError)):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

    def test_m_r3_05_mutante_omitir_physical_independence_check_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-05: Skip physical independence check -> caught by R3-T11."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M05"

        # Simular que verify_physical_independence retorna False
        with (
            patch("sky_claw.local.runtime_vault.clone.verify_physical_independence", return_value=False),
            pytest.raises(RuntimeCloneError, match="(?i)independencia"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()

    def test_m_r3_06_mutante_omitir_staging_tree_digest_verification_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-06: Skip staging tree digest verification -> caught by R3-T12."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M06"

        orig_copy2 = shutil.copy2

        def corrupting_copy2(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object) -> str:
            res = orig_copy2(src, dst, **kwargs)
            if "Skyrim.esm" in str(src):
                pathlib.Path(dst).write_bytes(b"CORRUPTED_BYTES_IN_STAGING")
            return res

        with (
            patch("shutil.copy2", side_effect=corrupting_copy2),
            pytest.raises(RuntimeCloneError, match="(?i)digest|staging|verificación"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()

    def test_m_r3_07_mutante_omitir_critical_files_verification_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-07: Skip critical files verification -> caught by R3-T13."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M07"

        # Expectativa de archivo crítico con digest incorrecto
        bad_critical = [
            CriticalFileExpectation(
                rel_path="SkyrimSE.exe",
                expected_digest="0" * 64,
            )
        ]

        with pytest.raises(RuntimeCloneError, match="(?i)crítico|pre-copy|verificación"):
            create_runtime_clone(golden_res, dest, critical_expectations=bad_critical)

        assert not dest.exists()

    def test_m_r3_08_mutante_publicar_antes_de_verificar_staging_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-08: Publish before staging verification -> caught by R3-T14 / R3-T18."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M08"

        # Si staging está corrupto, NO debe publicarse (dest no debe existir)
        orig_copy2 = shutil.copy2

        def corrupt_staging(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object) -> str:
            res = orig_copy2(src, dst, **kwargs)
            if "bink2w64.dll" in str(src):
                pathlib.Path(dst).write_bytes(b"CORRUPTED_DLL")
            return res

        with patch("shutil.copy2", side_effect=corrupt_staging), pytest.raises(RuntimeCloneError):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()

    def test_m_r3_09_mutante_omitir_post_copy_golden_reverification_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-09: Skip post-copy Golden re-verification -> caught by R3-T16."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M09"

        orig_replace = os.replace

        def mutate_golden_on_publish(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            (golden_root / "SkyrimSE.exe").write_bytes(b"MUTATED_DURING_PUBLISH")
            orig_replace(src, dst)

        with (
            patch("os.replace", side_effect=mutate_golden_on_publish),
            pytest.raises(RuntimeCloneError, match="(?i)golden master|modificado|degradado"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()

    def test_m_r3_10_mutante_omitir_staging_cleanup_en_fallo_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-10: Omit staging cleanup on failure -> caught by R3-T17 / R3-T18."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M10"

        # Simular fallo durante la verificación de staging
        with (
            patch(
                "sky_claw.local.runtime_vault.clone.verify_physical_independence",
                side_effect=RuntimeError("Fallo forzado"),
            ),
            pytest.raises(RuntimeCloneError),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # Staging DEBE haber sido eliminado
        stagings = list(tmp_path.glob(".*skyclaw-staging*"))
        assert len(stagings) == 0
        assert not dest.exists()
