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
import sys
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.clone import (
    create_runtime_clone,
    verify_physical_independence,
)
from sky_claw.local.runtime_vault.golden import verify_golden_master
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.locking import destination_lock
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    DestinationExistsError,
    FileIdentity,
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    InventoryError,
    InventoryLinkError,
    PhysicalIndependenceResult,
    RuntimeCloneDescriptor,
    RuntimeCloneError,
    RuntimeCloneResult,
    RuntimeIdentity,
    RuntimeVaultError,
    RuntimeVerificationResult,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files
from tests._symlink_guard import crear_junction, symlink_guard


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


class TestPendingFindingsReproduction:
    """Pruebas de reproducción en ROJO de los findings pendientes (Thread 1 a 8)."""

    def test_red_junction_safe_cleanup(self, tmp_path: pathlib.Path) -> None:
        """Thread 1 / Codex P1: _cleanup_staging no debe descender a través de junctions a targets externos."""
        from sky_claw.local.runtime_vault.clone import _cleanup_staging

        external = tmp_path / "External_Target"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("IMPORTANT_EXTERNAL_DATA")

        staging = tmp_path / "Staging_With_Junction"
        staging.mkdir()
        normal_file = staging / "normal.txt"
        normal_file.write_text("NORMAL")

        junction_in_staging = staging / "Injected_Junction"
        if sys.platform == "win32":
            err = crear_junction(junction_in_staging, external)
            if err:
                pytest.skip(f"No se pudo crear junction: {err}")
        else:
            junction_in_staging.symlink_to(external, target_is_directory=True)

        _cleanup_staging(staging)

        assert sentinel.exists(), "El sentinel externo DEBE preservarse intacto tras el cleanup"
        assert sentinel.read_text() == "IMPORTANT_EXTERNAL_DATA"
        assert not staging.exists(), "El staging debe eliminarse completamente"

    def test_red_broken_destination_link(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """Thread 2 / Codex P2: Destino que es un enlace roto debe fallar inmediatamente con DestinationExistsError."""
        golden_root, golden_res, critical = mock_golden_environment
        broken_target = tmp_path / "nonexistent_target_123"
        broken_link = tmp_path / "Broken_Dest_Link"

        try:
            broken_link.symlink_to(broken_target)
        except (OSError, NotImplementedError):
            pytest.skip("No se pudo crear symlink roto")

        with (
            patch("shutil.copy2") as copy_spy,
            pytest.raises(DestinationExistsError, match="(?i)ya existe|enlace"),
        ):
            create_runtime_clone(golden_res, broken_link, critical_expectations=critical)

        assert copy_spy.call_count == 0

    def test_red_empty_directories_preserved(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Thread 3 / Codex P2: Clon debe preservar directorios vacíos del Golden Master."""
        src = tmp_path / "Golden_With_Empty"
        src.mkdir()
        (src / "Empty_Dir").mkdir()
        (src / "Nested" / "Empty_Subdir").mkdir(parents=True)
        (src / "Data").mkdir()
        (src / "Data" / "file.esm").write_bytes(b"DATA_FILE")
        (src / "SkyrimSE.exe").write_bytes(b"EXE")

        files = inventory_tree(src)
        tree_digest = tree_digest_from_files(files)
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")

        golden_res = verify_golden_master(
            candidate=src,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
        )
        assert golden_res.success is True

        dest = tmp_path / "Runtime_Empty_Preserved"
        res = create_runtime_clone(golden_res, dest)
        assert res.success is True

        assert (dest / "Empty_Dir").is_dir()
        assert (dest / "Nested" / "Empty_Subdir").is_dir()
        assert (dest / "Data" / "file.esm").exists()

    def test_red_atomic_noreplace_publish(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """Thread 4 / Codex P2: Publicación atómica no debe sobreescribir un destino creado concurrentemente."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Collision_Target"

        orig_inventory = inventory_tree

        def collision_before_publish(root: pathlib.Path) -> tuple[FileIdentity, ...]:
            res = orig_inventory(root)
            if "skyclaw-staging" in root.name and not dest.exists():
                dest.mkdir()
                (dest / "sentinel.txt").write_text("EXTERNAL_DATA")
            return res

        with (
            patch("sky_claw.local.runtime_vault.clone.inventory_tree", side_effect=collision_before_publish),
            pytest.raises((DestinationExistsError, RuntimeCloneError)),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert dest.exists()
        assert (dest / "sentinel.txt").exists()
        assert (dest / "sentinel.txt").read_text() == "EXTERNAL_DATA"

    def test_red_parent_mkdir_error_translation(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """Thread 5 / Codex P2: Fallo al crear padre del destino debe traducirse a RuntimeCloneError."""
        golden_root, golden_res, critical = mock_golden_environment
        file_parent = tmp_path / "File_As_Parent"
        file_parent.write_text("NOT_A_DIR")
        dest = file_parent / "Child_Dest"

        with pytest.raises(RuntimeCloneError, match="(?i)padre|directorio"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

    def test_red_operational_runtime_writability(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """Thread 6 / Codex P2: Runtime clone debe ser mutable/escribible aunque el Golden tenga archivos ReadOnly."""
        golden_root, golden_res, critical = mock_golden_environment
        ro_file = golden_root / "Data" / "Skyrim.esm"
        orig_mode = ro_file.stat().st_mode
        ro_file.chmod(stat.S_IREAD)

        dest = tmp_path / "Runtime_Writable_Test"
        try:
            res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
            assert res.success is True

            dest_file = dest / "Data" / "Skyrim.esm"
            assert (ro_file.stat().st_mode & stat.S_IWRITE) == 0, "Golden original debe seguir ReadOnly"
            assert (dest_file.stat().st_mode & stat.S_IWRITE) != 0, "Destination debe tener bit de escritura"

            # Destination se puede abrir para escritura
            with dest_file.open("r+b") as fh:
                fh.write(b"W")
        finally:
            ro_file.chmod(orig_mode)

    def test_red_relative_golden_descriptor_provenance(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """Thread 7 / CodeRabbit: Descriptor con ubicación relativa no debe fallar con ValueError tras publish."""
        golden_root, golden_res, critical = mock_golden_environment
        assert golden_res.descriptor is not None

        try:
            rel_location = pathlib.Path(os.path.relpath(golden_root, os.getcwd()))
        except ValueError:
            pytest.skip("No se puede construir una ruta relativa entre unidades distintas")

        relative_desc = GoldenMasterDescriptor(
            location=rel_location,
            runtime_identity=golden_res.descriptor.runtime_identity,
            tree_digest=golden_res.descriptor.tree_digest,
            role="reference_only",
        )
        relative_golden_res = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=golden_res.tree_result,
            runtime_result=golden_res.runtime_result,
            critical_results=golden_res.critical_results,
            descriptor=relative_desc,
        )

        dest = tmp_path / "Runtime_Relative_Provenance"
        res = create_runtime_clone(relative_golden_res, dest, critical_expectations=critical)
        assert res.success is True
        assert res.descriptor is not None
        assert res.descriptor.source_golden == rel_location

    def test_red_lock_preparation_leak_on_mkdir_failure(self, tmp_path: pathlib.Path) -> None:
        """Thread 8 / CodeRabbit: Fallo en mkdir de lock_dir no debe fugar el thread_lock."""
        dest = tmp_path / "Runtime_Lock_Leak_Test"

        with (
            patch("pathlib.Path.mkdir", side_effect=OSError("Fallo mkdir lock")),
            pytest.raises(RuntimeCloneError),
            destination_lock(dest),
        ):
            pass

        # Segunda llamada DEBE poder obtener el thread_lock sin quedar colgada
        with (
            patch("pathlib.Path.mkdir", side_effect=OSError("Fallo mkdir lock")),
            pytest.raises(RuntimeCloneError),
            destination_lock(dest, timeout=0.0),
        ):
            pass

    def test_b5_physical_independence_result_modelo_e_invariantes(self) -> None:
        """B5: Invariantes y validaciones de PhysicalIndependenceResult."""
        res_ok = PhysicalIndependenceResult(success=True, message="", files_checked=10)
        assert res_ok.success is True
        assert res_ok.message == ""
        assert res_ok.files_checked == 10
        assert res_ok.failed_rel_path is None

        # Éxito con mensaje no vacío es inválido
        with pytest.raises(ValueError, match="message=''"):
            PhysicalIndependenceResult(success=True, message="no debe haber mensaje")

        # Éxito con failed_rel_path es inválido
        with pytest.raises(ValueError, match="failed_rel_path"):
            PhysicalIndependenceResult(success=True, failed_rel_path="Data/Skyrim.esm")

        # Fallo sin mensaje es inválido
        with pytest.raises(ValueError, match="message explicativo"):
            PhysicalIndependenceResult(success=False, message="")

        res_fail = PhysicalIndependenceResult(
            success=False,
            message="Hardlink detectado",
            files_checked=5,
            failed_rel_path="SkyrimSE.exe",
        )
        assert res_fail.success is False
        assert res_fail.failed_rel_path == "SkyrimSE.exe"

    def test_p2_runtime_clone_result_binds_provenance_to_source_golden(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """P2: RuntimeCloneResult VERIFIED exige source_golden y coincidencia de ubicación."""
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        digest = TreeDigest(digest="d" * 64, files=5, bytes=500)
        loc = tmp_path / "runtime"
        golden_loc = tmp_path / "golden"

        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=digest,
            observed=digest,
        )

        clone_desc = RuntimeCloneDescriptor(
            location=loc,
            runtime_identity=runtime_id,
            tree_digest=digest,
            source_golden=golden_loc,
        )

        # 1. source_golden=None en VERIFIED es inválido
        with pytest.raises(ValueError, match="source_golden no nulo"):
            RuntimeCloneResult(
                state=VerificationState.VERIFIED,
                source_golden=None,
                tree_result=tree_res,
                physical_independence_verified=True,
                descriptor=clone_desc,
            )

        # 2. descriptor.source_golden discordante con source_golden.location es inválido
        other_golden_loc = tmp_path / "other_golden"
        other_golden_desc = GoldenMasterDescriptor(
            location=other_golden_loc,
            runtime_identity=runtime_id,
            tree_digest=digest,
        )
        with pytest.raises(ValueError, match="ubicación del Golden Master"):
            RuntimeCloneResult(
                state=VerificationState.VERIFIED,
                source_golden=other_golden_desc,
                tree_result=tree_res,
                physical_independence_verified=True,
                descriptor=clone_desc,
            )


class TestLockingAndConcurrency:
    """Pruebas L1 a L6 para la serialización por destino (B4)."""

    def test_l1_adquisiciones_concurrentes_mismo_destino_serializadas(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """L1: Dos adquisiciones concurrentes sobre el mismo destino no pueden solaparse."""
        dest = tmp_path / "Shared_Runtime_Dest"

        with (
            destination_lock(dest),
            pytest.raises(RuntimeCloneError, match="bloqueado"),
            destination_lock(dest, timeout=0.0),
        ):
            pass

    def test_l2_destinos_distintos_locks_independientes(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """L2: Destinos distintos operan con locks independientes y no se bloquean entre sí."""
        dest1 = tmp_path / "Runtime_Dest_1"
        dest2 = tmp_path / "Runtime_Dest_2"

        with destination_lock(dest1), destination_lock(dest2):
            assert True

    def test_l3_fallo_copia_libera_lock(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """L3: Fallo durante la copia física libera el lock para reintentos posteriores."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Fail_Lock"

        def failing_copy(*args: object, **kwargs: object) -> None:
            raise OSError("Fallo simulado de I/O")

        with patch("shutil.copy2", side_effect=failing_copy), pytest.raises(RuntimeCloneError):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # El lock debe estar completamente liberado
        with destination_lock(dest):
            assert True

    def test_l4_fallo_staging_verification_libera_lock(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """L4: Fallo durante la verificación de staging libera el lock."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Staging_Fail_Lock"

        with (
            patch(
                "sky_claw.local.runtime_vault.clone.verify_physical_independence",
                return_value=PhysicalIndependenceResult(success=False, message="Fallo simulado"),
            ),
            pytest.raises(RuntimeCloneError),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # El lock debe estar completamente liberado
        with destination_lock(dest):
            assert True

    def test_l5_publish_exitoso_libera_lock(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """L5: Publish exitoso libera el lock."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Success_Lock"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True

        # El lock debe estar disponible tras completar la clonación
        with destination_lock(dest):
            assert True

    def test_l6_ningun_otro_mutador_runtime_evita_lock(self) -> None:
        """L6: create_runtime_clone es el único mutador de Runtime Vault activo."""
        import sky_claw.local.runtime_vault as pkg

        pkg_dir = pathlib.Path(pkg.__file__).parent
        py_files = list(pkg_dir.glob("*.py"))
        mutating_functions: list[str] = []

        for py_path in py_files:
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and ("clone" in node.name or "mutate" in node.name):
                    mutating_functions.append(f"{py_path.stem}.{node.name}")

        assert mutating_functions == ["clone.create_runtime_clone"]


class TestRuntimeCloneSuiteCanonico:
    """Suite canónica de pruebas para RV-3: R3-T01 a R3-T28."""

    def test_r3_t01_source_authority_con_golden_verificado_autoriza_clon(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T01: Verificación de autoridad de fuente: solo GoldenMasterVerificationResult VERIFIED autoriza el clon."""
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

        # Caso 2: Pasando un descriptor bare directamente debe ser rechazado
        dest_path2 = tmp_path / "Skyrim_Runtime_Clone_2"
        assert golden_res.descriptor is not None
        with pytest.raises(RuntimeCloneError, match="(?i)GoldenMasterVerificationResult|autoridad|no autorizado"):
            create_runtime_clone(
                golden_source=golden_res.descriptor,  # type: ignore[arg-type]
                destination=dest_path2,
                critical_expectations=critical_expectations,
            )

    def test_r3_t02_rechazo_de_golden_no_verificado_o_rol_invalido(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T02: Rechazo de candidato o descriptor Golden no verificado o con rol inválido."""
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
        """R3-T03: Validación de inmutabilidad y post_init en RuntimeCloneDescriptor y RuntimeCloneResult."""
        runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        digest = TreeDigest(digest="a" * 64, files=10, bytes=1000)
        loc = tmp_path / "runtime"
        golden_loc = tmp_path / "golden"

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
            source_golden=golden_loc,
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

        golden_desc = GoldenMasterDescriptor(
            location=golden_loc,
            runtime_identity=runtime_id,
            tree_digest=digest,
        )

        with pytest.raises(ValueError, match="descriptor"):
            RuntimeCloneResult(
                state=VerificationState.VERIFIED,
                source_golden=golden_desc,
                tree_result=tree_res_verified,
                physical_independence_verified=True,
                descriptor=None,
            )

        with pytest.raises(ValueError, match="(?i)independencia"):
            RuntimeCloneResult(
                state=VerificationState.VERIFIED,
                source_golden=golden_desc,
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
        """R3-T04: Comportamiento fail-closed ante directorio de origen inexistente o que no sea directorio."""
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
        tree_res = TreeVerificationResult(state=VerificationState.VERIFIED, expected=digest, observed=digest)
        run_res = RuntimeVerificationResult(state=VerificationState.VERIFIED, expected=runtime_id, observed=runtime_id)
        res_missing = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=tree_res,
            runtime_result=run_res,
            descriptor=desc_missing,
        )

        dest = tmp_path / "runtime_dest"
        with pytest.raises((RuntimeCloneError, InventoryError)):
            create_runtime_clone(res_missing, dest)

        # Origen que es un archivo regular en vez de directorio
        file_source = tmp_path / "file_source.txt"
        file_source.write_text("not a dir")
        desc_file = GoldenMasterDescriptor(
            location=file_source,
            runtime_identity=runtime_id,
            tree_digest=digest,
            role="reference_only",
        )
        res_file = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=tree_res,
            runtime_result=run_res,
            descriptor=desc_file,
        )
        with pytest.raises((RuntimeCloneError, InventoryError)):
            create_runtime_clone(res_file, dest)

    def test_r3_t05_fail_closed_origen_igual_a_destino_o_anidados(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T05: Fail-closed cuando origen == destino o el destino está anidado dentro del origen o viceversa."""
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
        """R3-T06: Fail-closed ante destino preexistente (DestinationExistsError, sin sobreescritura ni borrado)."""
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
        """R3-T07: Revalidación pre-copia del Golden Master (detecta corrupción previa y aborta antes de copiar)."""
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
        """R3-T08: Creación de directorio staging hermano en el mismo volumen con identificador único."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Staging_Test"

        staging_paths_observed: list[pathlib.Path] = []
        orig_mkdir = pathlib.Path.mkdir

        def tracking_mkdir(
            self: pathlib.Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
        ) -> None:
            if "skyclaw-staging" in self.name:
                staging_paths_observed.append(self)
            orig_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

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
        """R3-T09: Copia física real (shutil.copy2) preservando la estructura del árbol y los bytes de cada archivo."""
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
        """R3-T10: Rechazo estricto de reparse points, symlinks y junctions en origen, staging y destino."""
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
        """R3-T11: Verificación de independencia física por inode y device (dest.st_dev, dest.st_ino) != (src.st_dev, src.st_ino)."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Independence"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True

        files = inventory_tree(golden_root)
        indep = verify_physical_independence(golden_root, dest, files)
        assert indep.success is True
        assert indep.message == ""
        assert indep.files_checked == len(files)

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
        """R3-T12: Cálculo de TreeDigest en staging y coincidencia exacta contra el TreeDigest esperado del Golden."""
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
        """R3-T13: Verificación de archivos críticos en staging contra hashes SHA256 esperados usando captura sellada única."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Critical_Files"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
        assert res.success is True
        assert len(res.critical_results) == 3
        for c in res.critical_results:
            assert c.state is VerificationState.VERIFIED
            assert c.success is True
            assert c.observed_digest is not None
            assert c.expected_digest is not None
            assert c.observed_digest.lower() == c.expected_digest.lower()

    def test_r3_t14_atomic_publish_solo_tras_verificacion_staging_exitosa(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T14: Publicación atómica vía os.replace desde staging hacia destino solo tras superar la verificación de staging."""
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
        """R3-T15: Verificación post-publish en destino (recalcula TreeDigest, verifica reparse points, críticos e independencia)."""
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
        """R3-T16: Re-verificación post-copia del Golden Master (asegura que el origen no fue mutado ni degradado)."""
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
        """R3-T17: Rollback elimina el directorio de staging ante fallos de copia física sin tocar el destino."""
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
        """R3-T18: Rollback elimina el directorio de staging ante fallos de verificación sin tocar el destino."""
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
        """R3-T19: Jerarquía de excepciones de dominio (RuntimeCloneError, DestinationExistsError heredan de RuntimeVaultError)."""
        assert issubclass(RuntimeCloneError, RuntimeVaultError)
        assert issubclass(DestinationExistsError, RuntimeCloneError)
        assert issubclass(DestinationExistsError, RuntimeVaultError)

    def test_r3_t20_ast_isolation_check_sin_imports_candidate_only(self) -> None:
        """R3-T20: Aislamiento AST: el paquete runtime_vault coincide exactamente con el set esperado y sin imports candidate-only."""
        import sky_claw.local.runtime_vault as pkg

        pkg_dir = pathlib.Path(pkg.__file__).parent
        py_files = list(pkg_dir.glob("*.py"))
        discovered_modules = {p.stem for p in py_files}

        # Ancla de igualdad exacta para evitar hermanos no cableados
        expected_modules = {
            "__init__",
            "clone",
            "golden",
            "inventory",
            "locking",
            "models",
            "protection",
            "verification",
        }
        assert discovered_modules == expected_modules

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
        """R3-T21: Rechazo estricto de hardlinks (os.link) provocando fallo en la verificación de independencia física."""
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
        """R3-T22: Manejo de atributos de solo lectura en archivos de origen durante copia en staging y rollback."""
        golden_root, golden_res, critical = mock_golden_environment

        # Marcar un archivo de origen como read-only
        ro_file = golden_root / "Data" / "Skyrim.esm"
        orig_mode = ro_file.stat().st_mode
        try:
            ro_file.chmod(stat.S_IREAD)

            # Caso A: Copia exitosa preservando lectura
            dest = tmp_path / "Runtime_Dest_ReadOnly"
            res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
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
                create_runtime_clone(golden_res, dest_fail, critical_expectations=critical)

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
        """R3-T23: Detección de mutación concurrente en el origen durante la copia y rollback inmediato."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_Concurrent_Mut"

        orig_copy2 = shutil.copy2

        def mutating_copy2(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object) -> object:
            res = orig_copy2(src, dst, **kwargs)  # type: ignore[call-overload]
            # Mutar un archivo de origen que aún NO fue copiado en el orden de iteración
            if str(src).endswith("Skyrim.esm"):
                (golden_root / "bink2w64.dll").write_bytes(b"CONCURRENT_MUTATION_DATA")
            return res

        with (
            patch("shutil.copy2", side_effect=mutating_copy2),
            pytest.raises(RuntimeCloneError, match="(?i)staging|digest|crítico|verificación"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()
        assert len(list(tmp_path.glob(".*skyclaw-staging*"))) == 0

    def test_r3_t24_rechazo_de_origen_vacio_inventory_error(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """R3-T24: Rechazo de origen vacío lanzando InventoryError / RuntimeCloneError."""
        empty_dir = tmp_path / "Empty_Golden"
        empty_dir.mkdir()

        digest = TreeDigest(digest="c" * 64, files=0, bytes=0)
        desc = GoldenMasterDescriptor(
            location=empty_dir,
            runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            tree_digest=digest,
        )
        tree_res = TreeVerificationResult(state=VerificationState.VERIFIED, expected=digest, observed=digest)
        run_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            observed=RuntimeIdentity("skyrimse", "1.6.1170.0"),
        )
        res_empty = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=tree_res,
            runtime_result=run_res,
            descriptor=desc,
        )

        dest = tmp_path / "Runtime_Dest_Empty"
        with pytest.raises((RuntimeCloneError, InventoryError)):
            create_runtime_clone(res_empty, dest)

    def test_r3_t25_normalizacion_de_paths_y_seguridad_anti_traversal(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """R3-T25: Normalización exhaustiva de rutas y seguridad anti-traversal."""
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
        """R3-T26: Reutilización de captura sellada FileIdentity única entre TreeDigest y archivos críticos."""
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
        """R3-T27: Semántica de VerificationState (UNKNOWN != VERIFIED)."""
        unknown_state: VerificationState = VerificationState.UNKNOWN
        verified_state: VerificationState = VerificationState.VERIFIED
        assert unknown_state != verified_state
        assert unknown_state.value != verified_state.value

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
        """R3-T28: Integración end-to-end: clonación completa en escenario nominal."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_E2E"

        res = create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert res.state is VerificationState.VERIFIED
        assert res.success is True
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
# MUTATION GATES: M-R3-01 A M-R3-17
# ==============================================================================


class TestMutationScenariosRV3:
    """Batería de mutaciones M-R3-01 a M-R3-17 para validar sensibilidad de la suite."""

    def test_m_r3_01_mutante_bypass_pre_copy_golden_revalidation_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-01: Bypass de revalidación pre-copia del Golden Master -> capturado por R3-T07."""
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
        """M-R3-02: Sobreescritura de destino preexistente -> capturado por R3-T06."""
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
        """M-R3-03: Uso de hardlinks (os.link) -> capturado por R3-T11 / R3-T21."""
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
        """M-R3-04: Introducción de symlink en Golden Master durante clonación -> capturado por R3-T08."""
        golden_root, golden_res, critical = mock_golden_environment
        target_exe = golden_root / "SkyrimSE.exe"
        real_target = tmp_path / "Real_Exe_Target.exe"
        real_target.write_bytes(b"REAL_EXE")
        target_exe.unlink()
        target_exe.symlink_to(real_target)

        dest = tmp_path / "Runtime_Dest_M04"
        with pytest.raises((RuntimeCloneError, InventoryLinkError), match="(?i)enlace|symlink|reparse"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

    def test_m_r3_05_mutante_permitir_junctions_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-05: Introducción de junction en Golden Master durante clonación -> capturado por R3-T09."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M05"

        junction_path = golden_root / "Injected_Junction"
        target_dir = tmp_path / "External_Target"
        target_dir.mkdir()

        if sys.platform == "win32":
            err = crear_junction(junction_path, target_dir)
            if err:
                pytest.skip(f"No se pudo crear junction en Windows: {err}")
        else:
            junction_path.symlink_to(target_dir, target_is_directory=True)

        with pytest.raises((RuntimeCloneError, InventoryLinkError), match="(?i)enlace|junction|reparse"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()

    def test_m_r3_06_mutante_omitir_tree_digest_verification_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-06: Omitir verificación de TreeDigest en staging -> capturado por R3-T12."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M06"

        orig_copy2 = shutil.copy2

        def corrupting_copy2(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object) -> object:
            res = orig_copy2(src, dst, **kwargs)  # type: ignore[call-overload]
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
        """M-R3-07: Omitir verificación de archivos críticos -> capturado por R3-T13."""
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
        """M-R3-08: Publicar antes de verificar staging -> capturado por R3-T14 / R3-T18."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M08"

        orig_copy2 = shutil.copy2

        def corrupt_staging(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object) -> object:
            res = orig_copy2(src, dst, **kwargs)  # type: ignore[call-overload]
            if "bink2w64.dll" in str(src):
                pathlib.Path(dst).write_bytes(b"CORRUPTED_DLL")
            return res

        with (
            patch("shutil.copy2", side_effect=corrupt_staging),
            patch("os.replace") as replace_spy,
            pytest.raises(RuntimeCloneError),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # Confirmar que os.replace JAMÁS fue llamado
        assert replace_spy.call_count == 0
        assert not dest.exists()

    def test_m_r3_09_mutante_omitir_post_copy_golden_reverification_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-09: Omitir re-verificación post-copia del Golden Master -> capturado por R3-T16."""
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

        # En fallo post-publish el destino publicado NO se borra
        assert dest.exists()

    def test_m_r3_10_mutante_permitir_hardlink_en_staging_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-10: Detección de hardlink en staging -> capturado por R3-T11 / R3-T18."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M10"

        with (
            patch(
                "sky_claw.local.runtime_vault.clone.verify_physical_independence",
                return_value=PhysicalIndependenceResult(
                    success=False,
                    message="Hardlink detectado en staging",
                    failed_rel_path="SkyrimSE.exe",
                ),
            ),
            patch("os.replace") as replace_spy,
            pytest.raises(RuntimeCloneError, match="(?i)independencia|hardlink"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert replace_spy.call_count == 0
        assert not dest.exists()

    def test_m_r3_11_mutante_permitir_hardlink_post_publish_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-11: Detección de hardlink post-publish -> capturado por R3-T21."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M11"

        with (
            patch(
                "sky_claw.local.runtime_vault.clone.verify_physical_independence",
                side_effect=[
                    PhysicalIndependenceResult(success=True),
                    PhysicalIndependenceResult(
                        success=False,
                        message="Hardlink post-publish detectado",
                        failed_rel_path="SkyrimSE.exe",
                    ),
                ],
            ),
            pytest.raises(RuntimeCloneError, match="(?i)independencia|hardlink"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

    def test_m_r3_12_mutante_permitir_escape_por_ancestro_con_junction_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-12: Destino apuntando a través de un ancestro junction hacia el Golden -> capturado por R3-T05."""
        golden_root, golden_res, critical = mock_golden_environment
        inner_dir = golden_root / "Inner_Dir"
        inner_dir.mkdir()

        alias_parent = tmp_path / "Alias_Parent_M12"
        if sys.platform == "win32":
            err = crear_junction(alias_parent, inner_dir)
            if err:
                pytest.skip(f"No se pudo crear junction en Windows: {err}")
        else:
            alias_parent.symlink_to(inner_dir, target_is_directory=True)

        dest = alias_parent / "Escape_Target"
        with pytest.raises(RuntimeCloneError, match="(?i)anidado|idéntico|origen"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

    def test_m_r3_13_mutante_restaurar_cleanup_dest_root_post_publish_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-13: Mutante que llama _cleanup_staging(dest_root) post-publish es rechazado."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M13"

        with (
            patch(
                "sky_claw.local.runtime_vault.clone.verify_physical_independence",
                side_effect=[
                    PhysicalIndependenceResult(success=True),
                    PhysicalIndependenceResult(success=False, message="fallo simulado"),
                ],
            ),
            pytest.raises(RuntimeCloneError),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        # Destino DEBE preservarse en disco
        assert dest.exists()

    def test_m_r3_14_mutante_deshabilitar_destination_lock_es_rechazado(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """M-R3-14: Mutante que deshabilita el lock de destino es detectado por concurrencia."""
        dest = tmp_path / "Runtime_Dest_M14"
        with (
            destination_lock(dest),
            pytest.raises(RuntimeCloneError, match="bloqueado"),
            destination_lock(dest, timeout=0.0),
        ):
            pass

    def test_m_r3_15_mutante_physical_independence_result_sin_evidencia_es_rechazado(self) -> None:
        """M-R3-15: Mutante que retorna bool o estructura vacía en fallo es rechazado por invariantes."""
        with pytest.raises(ValueError, match="message explicativo"):
            PhysicalIndependenceResult(success=False, message="")

    def test_m_r3_16_mutante_publicar_staging_corrupto_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-16: Publicar staging corrupto antes de verificarlo es rechazado (os.replace no invocado)."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M16"

        orig_copy2 = shutil.copy2

        def corrupt_bink(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object) -> object:
            res = orig_copy2(src, dst, **kwargs)  # type: ignore[call-overload]
            if "bink2w64.dll" in str(src):
                pathlib.Path(dst).write_bytes(b"BAD_BINK")
            return res

        with (
            patch("shutil.copy2", side_effect=corrupt_bink),
            patch("os.replace") as replace_spy,
            pytest.raises(RuntimeCloneError),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert replace_spy.call_count == 0
        assert not dest.exists()

    def test_m_r3_17_mutante_r3_t23_mutacion_tardia_detectada_en_staging(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-17: R3-T23 detecta mutación concurrente en staging antes de la publicación."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_Dest_M17"

        orig_copy2 = shutil.copy2

        def mutating_hook(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: object) -> object:
            res = orig_copy2(src, dst, **kwargs)  # type: ignore[call-overload]
            if str(src).endswith("Skyrim.esm"):
                (golden_root / "bink2w64.dll").write_bytes(b"TAMPERED_IN_FLIGHT")
            return res

        with (
            patch("shutil.copy2", side_effect=mutating_hook),
            pytest.raises(RuntimeCloneError, match="(?i)staging|digest|crítico|verificación"),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert not dest.exists()

    def test_m_r3_18_mutante_cleanup_atravesando_junctions_es_rechazado(self, tmp_path: pathlib.Path) -> None:
        """M-R3-18: Cleanup de staging no debe atravesar junctions ni borrar targets externos."""
        from sky_claw.local.runtime_vault.clone import _cleanup_staging

        external = tmp_path / "External_Target_M18"
        external.mkdir()
        sentinel = external / "important.bin"
        sentinel.write_bytes(b"EXT_DATA")

        staging = tmp_path / "Staging_M18"
        staging.mkdir()
        junction_link = staging / "Link_To_Ext"

        if sys.platform == "win32":
            err = crear_junction(junction_link, external)
            if err:
                pytest.skip(f"No se pudo crear junction: {err}")
        else:
            junction_link.symlink_to(external, target_is_directory=True)

        _cleanup_staging(staging)
        assert sentinel.exists()
        assert sentinel.read_bytes() == b"EXT_DATA"
        assert not staging.exists()

    def test_m_r3_19_mutante_path_exists_en_destino_con_link_roto_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-19: Usar Path.exists() en destino dejaría pasar enlaces rotos -> detectado."""
        golden_root, golden_res, critical = mock_golden_environment
        broken_dest = tmp_path / "Broken_Link_M19"
        try:
            broken_dest.symlink_to(tmp_path / "nonexistent_dir_m19")
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not supported")

        with pytest.raises(DestinationExistsError, match="(?i)ya existe|enlace"):
            create_runtime_clone(golden_res, broken_dest, critical_expectations=critical)

    def test_m_r3_20_mutante_ignorar_directorios_vacios_es_rechazado(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """M-R3-20: Mutante que ignora directorios vacíos es rechazado por verificación estructural."""
        src = tmp_path / "Golden_M20"
        src.mkdir()
        (src / "Empty_Subdir").mkdir()
        (src / "file.txt").write_bytes(b"CONTENT")

        files = inventory_tree(src)
        tree_digest = tree_digest_from_files(files)
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")

        golden_res = verify_golden_master(
            candidate=src,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
        )

        dest = tmp_path / "Runtime_M20"
        res = create_runtime_clone(golden_res, dest)
        assert res.success is True
        assert (dest / "Empty_Subdir").is_dir()

    def test_m_r3_21_mutante_omitir_guardia_no_clobber_en_publish_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-21: Mutante que omite guardia de colisión en publish es rechazado."""
        golden_root, golden_res, critical = mock_golden_environment
        dest = tmp_path / "Runtime_M21"

        orig_inv = inventory_tree

        def inject_dest_during_staging_verify(root: pathlib.Path) -> tuple[FileIdentity, ...]:
            res = orig_inv(root)
            if "skyclaw-staging" in root.name and not dest.exists():
                dest.mkdir()
                (dest / "canary.txt").write_text("CANARY")
            return res

        with (
            patch("sky_claw.local.runtime_vault.clone.inventory_tree", side_effect=inject_dest_during_staging_verify),
            pytest.raises((DestinationExistsError, RuntimeCloneError)),
        ):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

        assert dest.exists()
        assert (dest / "canary.txt").read_text() == "CANARY"

    def test_m_r3_22_mutante_dest_parent_mkdir_sin_traduccion_de_error_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-22: Fallo de dest_parent.mkdir no traducido a RuntimeCloneError es rechazado."""
        golden_root, golden_res, critical = mock_golden_environment
        file_parent = tmp_path / "FileParent_M22"
        file_parent.write_text("NOT_DIR")
        dest = file_parent / "Child"

        with pytest.raises(RuntimeCloneError, match="(?i)padre|directorio"):
            create_runtime_clone(golden_res, dest, critical_expectations=critical)

    def test_m_r3_23_mutante_no_normalizar_escritura_deja_clon_readonly_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-23: Clon debe ser escribible para el dueño aun si el Golden es ReadOnly."""
        golden_root, golden_res, critical = mock_golden_environment
        ro_file = golden_root / "Data" / "Skyrim.esm"
        orig_mode = ro_file.stat().st_mode
        ro_file.chmod(stat.S_IREAD)

        dest = tmp_path / "Runtime_M23"
        try:
            res = create_runtime_clone(golden_res, dest, critical_expectations=critical)
            assert res.success is True
            dest_file = dest / "Data" / "Skyrim.esm"
            assert (dest_file.stat().st_mode & stat.S_IWRITE) != 0
        finally:
            ro_file.chmod(orig_mode)

    def test_m_r3_24_mutante_source_golden_normalizada_rompe_descriptor_relativo_es_rechazado(
        self,
        tmp_path: pathlib.Path,
        mock_golden_environment: tuple[pathlib.Path, GoldenMasterVerificationResult, list[CriticalFileExpectation]],
    ) -> None:
        """M-R3-24: Usar source_golden normalizada en vez de golden_desc.location falla el modelo."""
        golden_root, golden_res, critical = mock_golden_environment
        assert golden_res.descriptor is not None

        try:
            rel_loc = pathlib.Path(os.path.relpath(golden_root, os.getcwd()))
        except ValueError:
            pytest.skip("No se puede construir una ruta relativa entre unidades distintas")
        rel_desc = GoldenMasterDescriptor(
            location=rel_loc,
            runtime_identity=golden_res.descriptor.runtime_identity,
            tree_digest=golden_res.descriptor.tree_digest,
            role="reference_only",
        )
        rel_golden_res = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=golden_res.tree_result,
            runtime_result=golden_res.runtime_result,
            critical_results=golden_res.critical_results,
            descriptor=rel_desc,
        )

        dest = tmp_path / "Runtime_M24"
        res = create_runtime_clone(rel_golden_res, dest, critical_expectations=critical)
        assert res.success is True
        assert res.descriptor is not None
        assert res.descriptor.source_golden == rel_loc

    def test_m_r3_25_mutante_lock_dir_mkdir_fuga_thread_lock_es_rechazado(self, tmp_path: pathlib.Path) -> None:
        """M-R3-25: lock_dir.mkdir fuera de try/finally fugaría thread_lock -> verificado."""
        dest = tmp_path / "Runtime_M25"

        with (
            patch("pathlib.Path.mkdir", side_effect=OSError("Fallo mkdir lock")),
            pytest.raises(RuntimeCloneError),
            destination_lock(dest),
        ):
            pass

        # Si thread_lock fugó, esta llamada fallaría con timeout/bloqueo de proceso
        with (
            patch("pathlib.Path.mkdir", side_effect=OSError("Fallo mkdir lock")),
            pytest.raises(RuntimeCloneError),
            destination_lock(dest, timeout=0.0),
        ):
            pass

    def test_m_r3_26_mutante_locking_sin_bucle_de_timeout_es_rechazado(self, tmp_path: pathlib.Path) -> None:
        """M-R3-26: destination_lock con timeout > 0 debe reintentar hasta el deadline."""
        dest = tmp_path / "Runtime_M26"

        attempts = 0

        if sys.platform == "win32":
            import msvcrt

            def fake_msvcrt_locking(fd: int, mode: int, nbytes: int) -> None:
                nonlocal attempts
                if mode == msvcrt.LK_UNLCK:
                    return
                attempts += 1
                if attempts == 1:
                    raise OSError("Bloqueado")

            with (
                patch("msvcrt.locking", side_effect=fake_msvcrt_locking),
                destination_lock(dest, timeout=0.2),
            ):
                pass
            assert attempts >= 2
        else:
            import fcntl

            def fake_flock(fd: int, operation: int) -> None:
                nonlocal attempts
                if operation == fcntl.LOCK_UN:
                    return
                assert operation & fcntl.LOCK_EX
                attempts += 1
                if attempts == 1:
                    raise OSError("Bloqueado")

            with (
                patch("fcntl.flock", side_effect=fake_flock),
                destination_lock(dest, timeout=0.2),
            ):
                pass
            assert attempts >= 2
