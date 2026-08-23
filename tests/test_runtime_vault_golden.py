"""Tests del módulo Golden Master (RV-2) para Runtime Vault.

TDD obligatorio con casos mínimos T1 a T16 y tests adversariales.
Verifica:
- Invariante Golden: solo VERIFIED si tree, runtime y critical son VERIFIED.
- Trust model: UNKNOWN != VERIFIED, prohibición de auto-trust tautológico.
- Contrato fail-closed: links, mutaciones concurrentes, árboles inexistentes.
- Seguridad de paths en archivos críticos (Windows/POSIX, anti-traversal).
- Separación de rol, ubicación física e identidad de contenido.
- Modelos inmutables (frozen + slots).
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
from collections.abc import Sequence

import pytest

from sky_claw.local.runtime_vault.golden import (
    verify_critical_files,
    verify_golden_master,
)
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    CriticalFileEvidence,
    CriticalFileExpectation,
    FileIdentity,
    GoldenMasterCandidate,
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    RuntimeIdentity,
    TreeDigest,
    VerificationState,
)
from sky_claw.local.runtime_vault.verification import (
    tree_digest_from_files,
)
from tests._symlink_guard import symlink_guard


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def mock_candidate(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]]:
    """Crea una estructura sintética válida para candidate Golden Master."""
    root = tmp_path / "candidate_root"
    root.mkdir(parents=True)

    # Archivo ejecutable crítico
    exe_bytes = b"MZ\x90\x00synthetic_skyrim_exe"
    (root / "SkyrimSE.exe").write_bytes(exe_bytes)

    # Launcher crítico
    launcher_bytes = b"MZ\x90\x00synthetic_launcher"
    (root / "SkyrimSELauncher.exe").write_bytes(launcher_bytes)

    # DLL crítico
    dll_bytes = b"\x7fELF_or_PE_dll_bytes"
    (root / "bink2w64.dll").write_bytes(dll_bytes)

    # Data mod/master
    data_dir = root / "Data"
    data_dir.mkdir()
    (data_dir / "Skyrim.esm").write_bytes(b"TES4_HEADER_DATA")

    # Subdirectorio con espacios y unicode
    extra_dir = root / "Extra Tools"
    extra_dir.mkdir()
    (extra_dir / "tool_🎮.txt").write_text("herramienta de prueba", encoding="utf-8")

    # Calcular TreeDigest independiente
    files = inventory_tree(root)
    tree_digest = tree_digest_from_files(files)

    runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")

    critical = [
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

    return root, tree_digest, runtime_id, critical


# ==============================================================================
# T1 - T16: SUITE REQUERIDA CANÓNICA
# ==============================================================================


class TestGoldenMasterSuiteCanonico:
    """Casos de prueba canónicos T1 a T16."""

    def test_t1_candidate_valido_con_expectativas_validas_es_verified(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.VERIFIED
        assert res.success is True
        assert res.message == ""
        assert res.descriptor is not None
        assert res.descriptor.location == root
        assert res.descriptor.runtime_identity == runtime_id
        assert res.descriptor.tree_digest == tree_digest
        assert res.descriptor.role == "reference_only"
        assert res.tree_result is not None and res.tree_result.state is VerificationState.VERIFIED
        assert res.runtime_result is not None and res.runtime_result.state is VerificationState.VERIFIED
        assert len(res.critical_results) == 3
        assert all(c.state is VerificationState.VERIFIED for c in res.critical_results)

    def test_t2_sin_expected_tree_identity_es_unknown_nunca_verified(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, _, runtime_id, critical = mock_candidate

        res = verify_golden_master(
            candidate=root,
            expected_tree=None,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.UNKNOWN
        assert res.success is False
        assert res.descriptor is None
        assert res.tree_result is not None
        assert res.tree_result.state is VerificationState.UNKNOWN

    def test_t3_anti_tautologia_requiere_evidencia_independiente(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        """La función verify_golden_master exige expectativas explícitas del caller."""
        root, _, runtime_id, _ = mock_candidate

        # Si no se provee expected_tree, verify_golden_master no autogenera ni auto-aprueba
        res = verify_golden_master(
            candidate=root,
            expected_tree=None,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
        )
        assert res.state is VerificationState.UNKNOWN
        assert res.descriptor is None

    def test_t4_tree_mismatch_es_failed(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        # Mutamos un archivo
        (root / "Data" / "Skyrim.esm").write_bytes(b"MUTATED_DATA")

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None
        assert res.tree_result is not None and res.tree_result.state is VerificationState.FAILED

    def test_t5_runtime_version_mismatch_es_failed(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        observed_wrong = RuntimeIdentity(game_key="skyrimse", game_version="1.5.97.0")

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=observed_wrong,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None
        assert res.runtime_result is not None and res.runtime_result.state is VerificationState.FAILED

    def test_t6_game_key_mismatch_es_failed(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        observed_wrong = RuntimeIdentity(game_key="skyrimle", game_version="1.6.1170.0")

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=observed_wrong,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None
        assert res.runtime_result is not None and res.runtime_result.state is VerificationState.FAILED

    def test_t7_runtime_identity_incompleta_es_unknown(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        observed_incomplete = RuntimeIdentity(game_key="skyrimse", game_version="")

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=observed_incomplete,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.UNKNOWN
        assert res.success is False
        assert res.descriptor is None
        assert res.runtime_result is not None and res.runtime_result.state is VerificationState.UNKNOWN

    def test_t8_missing_candidate_es_unknown(
        self,
        tmp_path: pathlib.Path,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        _, tree_digest, runtime_id, critical = mock_candidate
        non_existent = tmp_path / "non_existent_folder"

        res = verify_golden_master(
            candidate=non_existent,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.UNKNOWN
        assert res.success is False
        assert res.descriptor is None
        assert res.tree_result is not None and res.tree_result.state is VerificationState.UNKNOWN

    @symlink_guard
    def test_t9_root_symlink_o_junction_es_unknown_jamas_verified(
        self,
        tmp_path: pathlib.Path,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        link_root = tmp_path / "link_candidate_root"
        link_root.symlink_to(root, target_is_directory=True)

        res = verify_golden_master(
            candidate=link_root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.UNKNOWN
        assert res.success is False
        assert res.descriptor is None

    @symlink_guard
    def test_t10_nested_reparse_es_unknown(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        (root / "nested_link.txt").symlink_to(root / "SkyrimSE.exe")

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.UNKNOWN
        assert res.success is False
        assert res.descriptor is None

    def test_t11_tree_cambia_durante_verification_es_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate

        # Inyectamos hook de mutación concurrente de RV-1
        import sky_claw.local.runtime_vault.inventory as inv_module

        def mutar_archivo(ruta: pathlib.Path) -> None:
            if ruta.name == "SkyrimSE.exe":
                ruta.write_bytes(b"MUTATED_DURING_CAPTURE")

        monkeypatch.setattr(inv_module, "_HOOK_DURANTE_LA_LECTURA", mutar_archivo)

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.UNKNOWN
        assert res.success is False
        assert res.descriptor is None

    def test_t12_critical_executable_missing_es_failed(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, _, runtime_id, critical = mock_candidate
        (root / "SkyrimSE.exe").unlink()

        # Recalculamos tree_digest del árbol sin el exe para aislar la falla en el critical file
        tree_digest_sin_exe = tree_digest_from_files(inventory_tree(root))

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest_sin_exe,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None
        # Identificamos que el crítico ausente falló
        missing_crit = next(c for c in res.critical_results if c.rel_path == "SkyrimSE.exe")
        assert missing_crit.state is VerificationState.FAILED

    def test_t13_critical_executable_sha_mismatch_es_failed(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate

        # Modificamos una expectativa crítica con un hash erróneo
        wrong_critical = [
            CriticalFileExpectation(
                rel_path="SkyrimSE.exe",
                expected_digest="0" * 64,
                expected_size=100,
            )
        ]

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=wrong_critical,
        )

        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None
        assert res.critical_results[0].state is VerificationState.FAILED

    def test_t14_critical_relative_path_escape_rejected(self) -> None:
        """Cualquier intento de path traversal o absolute path debe ser rechazado."""
        invalid_paths = [
            "../escape.exe",
            "foo/../../bar.exe",
            "C:\\SkyrimSE.exe",
            "C:SkyrimSE.exe",
            "/SkyrimSE.exe",
            "\\SkyrimSE.exe",
            "\\\\server\\share\\SkyrimSE.exe",
            "//server/share/SkyrimSE.exe",
            "",
            "   ",
            "foo/\x00/bar.exe",
        ]
        for p in invalid_paths:
            with pytest.raises(ValueError):
                CriticalFileExpectation(rel_path=p, expected_digest="a" * 64)

    def test_t15_ubicacion_fisica_diferente_mismo_contenido_conserva_tree_identity(
        self,
        tmp_path: pathlib.Path,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        otro_root = tmp_path / "otro_candidate_root"
        shutil.copytree(root, otro_root)

        res = verify_golden_master(
            candidate=otro_root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )

        assert res.state is VerificationState.VERIFIED
        assert res.descriptor is not None
        assert res.descriptor.location == otro_root
        assert res.descriptor.tree_digest == tree_digest
        assert res.descriptor.role == "reference_only"

    def test_t16_golden_models_son_frozen_y_slots(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        from dataclasses import FrozenInstanceError

        root, tree_digest, runtime_id, critical = mock_candidate

        descriptor = GoldenMasterDescriptor(
            location=root,
            runtime_identity=runtime_id,
            tree_digest=tree_digest,
            role="reference_only",
        )
        # Test frozen
        with pytest.raises(FrozenInstanceError):
            descriptor.role = "mutated"  # type: ignore[misc]
        # Test slots
        assert isinstance(getattr(GoldenMasterDescriptor, "__slots__", None), tuple)
        assert isinstance(getattr(GoldenMasterVerificationResult, "__slots__", None), tuple)
        assert isinstance(getattr(CriticalFileExpectation, "__slots__", None), tuple)
        assert isinstance(getattr(CriticalFileEvidence, "__slots__", None), tuple)
        assert isinstance(getattr(GoldenMasterCandidate, "__slots__", None), tuple)


# ==============================================================================
# TESTS ADVERSARIALES
# ==============================================================================


class TestGoldenMasterAdversarial:
    """Suite adversarial para bordes de path, unicode, ceros, duplicados y dimensiones."""

    def test_tree_digest_incorrecto_mismo_files_bytes(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        fake_tree = TreeDigest(digest="f" * 64, files=tree_digest.files, bytes=tree_digest.bytes)

        res = verify_golden_master(
            candidate=root,
            expected_tree=fake_tree,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.FAILED

    def test_tree_digest_files_incorrecto_mismo_digest(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        fake_tree = TreeDigest(digest=tree_digest.digest, files=tree_digest.files + 10, bytes=tree_digest.bytes)

        res = verify_golden_master(
            candidate=root,
            expected_tree=fake_tree,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.FAILED

    def test_tree_digest_bytes_incorrecto_mismo_digest(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        fake_tree = TreeDigest(digest=tree_digest.digest, files=tree_digest.files, bytes=tree_digest.bytes + 999)

        res = verify_golden_master(
            candidate=root,
            expected_tree=fake_tree,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.FAILED

    def test_zero_byte_critical_file(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, _, runtime_id, _ = mock_candidate
        (root / "empty.txt").write_bytes(b"")

        files = inventory_tree(root)
        tree_digest = tree_digest_from_files(files)

        empty_sha = hashlib.sha256(b"").hexdigest()
        crit = [
            CriticalFileExpectation(
                rel_path="empty.txt",
                expected_digest=empty_sha,
                expected_size=0,
            )
        ]

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=crit,
        )
        assert res.state is VerificationState.VERIFIED

    def test_uppercase_lowercase_hex_en_critical_digest(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, _ = mock_candidate
        exe_sha_upper = _sha256(b"MZ\x90\x00synthetic_skyrim_exe").upper()

        crit = [
            CriticalFileExpectation(
                rel_path="SkyrimSE.exe",
                expected_digest=exe_sha_upper,
            )
        ]

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=crit,
        )
        assert res.state is VerificationState.VERIFIED

    def test_unicode_y_espacios_en_archivos_criticos(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, _ = mock_candidate
        tool_content = b"herramienta de prueba"
        crit = [
            CriticalFileExpectation(
                rel_path="Extra Tools/tool_🎮.txt",
                expected_digest=_sha256(tool_content),
                expected_size=len(tool_content),
            )
        ]

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=crit,
        )
        assert res.state is VerificationState.VERIFIED

    def test_lista_criticos_vacia_es_valida(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, _ = mock_candidate

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=(),
        )
        assert res.state is VerificationState.VERIFIED
        assert res.critical_results == ()

    def test_expectativas_criticas_duplicadas_rechazadas(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, _ = mock_candidate
        dup_crit = [
            CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest="a" * 64),
            CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest="b" * 64),
        ]

        with pytest.raises(ValueError, match="duplicadas"):
            verify_golden_master(
                candidate=root,
                expected_tree=tree_digest,
                expected_runtime=runtime_id,
                observed_runtime=runtime_id,
                critical_expectations=dup_crit,
            )

    def test_candidate_como_dto_golden_master_candidate(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        cand = GoldenMasterCandidate(location=root, observed_runtime=runtime_id)

        res = verify_golden_master(
            candidate=cand,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.descriptor is not None
        assert res.descriptor.location == root

    def test_single_pass_efficiency_no_double_inventory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        """Verifica que inventory_tree se llame exactamente una sola vez."""
        root, tree_digest, runtime_id, critical = mock_candidate

        import sky_claw.local.runtime_vault.golden as golden_module

        conteo = 0
        orig_inv = golden_module.inventory_tree

        def inv_tracker(r: pathlib.Path):
            nonlocal conteo
            conteo += 1
            return orig_inv(r)

        monkeypatch.setattr(golden_module, "inventory_tree", inv_tracker)

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.VERIFIED
        assert conteo == 1, f"inventory_tree debió ejecutarse 1 vez, se ejecutó {conteo}"

    def test_case_sensitivity_relpath_en_critical_expectation(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        """La búsqueda de archivo crítico en el inventario canónico exige match exacto de relpath."""
        root, tree_digest, runtime_id, _ = mock_candidate

        # En el disco el archivo se llama "SkyrimSE.exe"
        # Si la expectativa usa "skyrimse.exe" (minúsculas), no coincide exactamente con el relpath canónico
        crit = [
            CriticalFileExpectation(
                rel_path="skyrimse.exe",
                expected_digest=_sha256(b"MZ\x90\x00synthetic_skyrim_exe"),
            )
        ]
        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=crit,
        )
        assert res.state is VerificationState.FAILED
        assert res.critical_results[0].state is VerificationState.FAILED

    def test_candidate_root_desaparece_falla_cerrado(
        self,
        tmp_path: pathlib.Path,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        temp_dir = tmp_path / "ephemeral_root"
        shutil.copytree(root, temp_dir)

        # Eliminamos la carpeta antes de verificar
        shutil.rmtree(temp_dir)

        res = verify_golden_master(
            candidate=temp_dir,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.UNKNOWN
        assert res.success is False
        assert res.descriptor is None


# ==============================================================================
# MUTATION TESTS M1 - M5 (Sección 19)
# ==============================================================================


class TestMutationScenarios:
    """Anclas contra las 5 mutaciones semánticas prohibidas en RV-2."""

    def test_m1_expected_tree_faltante_nunca_produce_verified(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        """M1: Si falta expected_tree, el resultado DEBE ser UNKNOWN, nunca VERIFIED."""
        root, _, runtime_id, critical = mock_candidate
        res = verify_golden_master(
            candidate=root,
            expected_tree=None,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.UNKNOWN
        assert res.success is False
        assert res.descriptor is None

    def test_m2_omitir_tree_verification_del_gate_rompe_invariante(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        """M2: Un fallo en tree verification (state FAILED) DEBE propagarse al estado global."""
        root, tree_digest, runtime_id, critical = mock_candidate
        # Árbol modificado
        (root / "Data" / "Skyrim.esm").write_bytes(b"CORRUPTED")

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None

    def test_m3_omitir_runtime_identity_del_gate_rompe_invariante(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        """M3: Un fallo en runtime identity (state FAILED) DEBE hacer que el gate global sea FAILED."""
        root, tree_digest, runtime_id, critical = mock_candidate
        mismatched_runtime = RuntimeIdentity(game_key="skyrimse", game_version="9.9.999.0")

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=mismatched_runtime,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None

    def test_m4_ignorar_critical_sha_mismatch_rompe_invariante(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        """M4: Un archivo crítico con SHA erróneo DEBE hacer que el resultado sea FAILED."""
        root, tree_digest, runtime_id, _ = mock_candidate
        bad_critical = [
            CriticalFileExpectation(
                rel_path="SkyrimSE.exe",
                expected_digest="1" * 64,
            )
        ]
        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=bad_critical,
        )
        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None

    def test_m5_permitir_path_traversal_critical_rompe_seguridad(self) -> None:
        """M5: Cualquier intento de traversal (..) o absolute path DEBE ser rechazado inmediatamente."""
        traversal_cases = [
            "../secret.txt",
            "Data/../../Windows/System32/cmd.exe",
            "C:\\boot.ini",
            "/etc/passwd",
        ]
        for bad_path in traversal_cases:
            with pytest.raises(ValueError, match="El path crítico"):
                CriticalFileExpectation(rel_path=bad_path, expected_digest="a" * 64)


class TestP21StableAbsoluteLocation:
    """P2-1: La ubicación almacenada en el descriptor debe ser absoluta y estable ante cambios de CWD."""

    def test_a1_candidate_relativo_produce_descriptor_absoluto_estable(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import os

        dir_a = tmp_path / "dir_a"
        dir_a.mkdir()
        golden_dir = dir_a / "golden_relative"
        golden_dir.mkdir()
        (golden_dir / "SkyrimSE.exe").write_bytes(b"MZ_EXE")

        files = inventory_tree(golden_dir)
        tree_digest = tree_digest_from_files(files)
        runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")

        # CWD = dir_a, pasar candidato relativo "golden_relative"
        monkeypatch.chdir(dir_a)
        res = verify_golden_master(
            candidate=pathlib.Path("golden_relative"),
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.descriptor is not None
        assert res.descriptor.location.is_absolute()
        assert str(res.descriptor.location) == os.path.abspath("golden_relative")

        # Cambiar CWD = dir_b
        dir_b = tmp_path / "dir_b"
        dir_b.mkdir()
        monkeypatch.chdir(dir_b)

        # El descriptor debe seguir apuntando a dir_a/golden_relative
        assert str(res.descriptor.location) == os.path.abspath(str(golden_dir))
        assert (res.descriptor.location / "SkyrimSE.exe").is_file()

    def test_a2_candidate_absoluto_preserva_ubicacion_absoluta(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        res = verify_golden_master(
            candidate=root.resolve(),
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.descriptor is not None
        assert res.descriptor.location.is_absolute()
        assert res.descriptor.location == root.resolve()

    def test_a3_root_symlink_sigue_fail_closed_sin_ocultar_enlace(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        symlink_guard()
        target_dir = tmp_path / "real_golden"
        target_dir.mkdir()
        (target_dir / "SkyrimSE.exe").write_bytes(b"MZ_EXE")

        link_dir = tmp_path / "link_golden"
        try:
            link_dir.symlink_to(target_dir, target_is_directory=True)
        except OSError:
            pytest.skip("No se pueden crear symlinks en este entorno")

        files = inventory_tree(target_dir)
        tree_digest = tree_digest_from_files(files)
        runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")

        # CWD = tmp_path, candidate relativo apuntando al enlace
        monkeypatch.chdir(tmp_path)
        res = verify_golden_master(
            candidate=pathlib.Path("link_golden"),
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
        )
        # La canonicalización lexical NO debe seguir el symlink; inventory_tree debe fail-closed
        assert res.state is VerificationState.UNKNOWN
        assert "symlink" in res.message.lower() or "enlace" in res.message.lower()
        assert res.descriptor is None


class TestVerifyCriticalFilesDirecto:
    """Tests directos D1-D6 sobre la función pública verify_critical_files."""

    def test_d1_expectations_vacias_devuelve_tupla_vacia(self) -> None:
        files = [FileIdentity(rel_path="SkyrimSE.exe", size=10, digest="a" * 64)]
        res = verify_critical_files(files, ())
        assert res == ()

    def test_d2_duplicated_rel_path_levanta_value_error(self) -> None:
        files = [FileIdentity(rel_path="SkyrimSE.exe", size=10, digest="a" * 64)]
        expectations = [
            CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest="a" * 64),
            CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest="a" * 64),
        ]
        with pytest.raises(ValueError, match="duplicadas"):
            verify_critical_files(files, expectations)

    def test_d3_critical_missing_produce_failed(self) -> None:
        files = [FileIdentity(rel_path="Other.exe", size=10, digest="a" * 64)]
        expectations = [
            CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest="a" * 64, expected_size=10),
        ]
        res = verify_critical_files(files, expectations)
        assert res == (
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.FAILED,
                message="Archivo crítico ausente en el árbol: 'SkyrimSE.exe'",
                expected_digest="a" * 64,
                observed_digest=None,
                expected_size=10,
                observed_size=None,
            ),
        )

    def test_d4_digest_mismatch_produce_failed(self) -> None:
        files = [FileIdentity(rel_path="SkyrimSE.exe", size=10, digest="b" * 64)]
        expectations = [
            CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest="a" * 64, expected_size=10),
        ]
        res = verify_critical_files(files, expectations)
        assert res == (
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.FAILED,
                message=f"Archivo crítico 'SkyrimSE.exe' no coincide: sha256 ({'b' * 64} vs {'a' * 64})",
                expected_digest="a" * 64,
                observed_digest="b" * 64,
                expected_size=10,
                observed_size=10,
            ),
        )

    def test_d5_digest_correcto_pero_size_incorrecto_produce_failed(self) -> None:
        files = [FileIdentity(rel_path="SkyrimSE.exe", size=20, digest="a" * 64)]
        expectations = [
            CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest="a" * 64, expected_size=10),
        ]
        res = verify_critical_files(files, expectations)
        assert res == (
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.FAILED,
                message="Archivo crítico 'SkyrimSE.exe' no coincide: tamaño (20 vs 10 bytes)",
                expected_digest="a" * 64,
                observed_digest="a" * 64,
                expected_size=10,
                observed_size=20,
            ),
        )

    def test_d6_digest_y_size_correctos_produce_verified(self) -> None:
        files = [FileIdentity(rel_path="SkyrimSE.exe", size=10, digest="a" * 64)]
        expectations = [
            CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest="A" * 64, expected_size=10),
        ]
        res = verify_critical_files(files, expectations)
        assert res == (
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.VERIFIED,
                message="",
                expected_digest="a" * 64,
                observed_digest="a" * 64,
                expected_size=10,
                observed_size=10,
            ),
        )


class TestGeneratorsDefensiveMaterialization:
    """Valida que iterables de una sola pasada (generators) sean materializados defensivamente."""

    def test_g1_bad_critical_generator_produce_failed_y_no_falso_verde(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        real_digest = critical[0].expected_digest
        bad_exp = CriticalFileExpectation(
            rel_path="SkyrimSE.exe",
            expected_digest="0" * 64,
            expected_size=100,
        )
        generator_exp = (exp for exp in [bad_exp])

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=generator_exp,
        )
        assert res.state is VerificationState.FAILED
        assert res.success is False
        assert res.descriptor is None
        assert res.critical_results == (
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.FAILED,
                message=f"Archivo crítico 'SkyrimSE.exe' no coincide: sha256 ({real_digest} vs {'0' * 64}), tamaño (24 vs 100 bytes)",
                expected_digest="0" * 64,
                observed_digest=real_digest,
                expected_size=100,
                observed_size=24,
            ),
        )

    def test_g2_direct_verify_critical_files_con_generator(self) -> None:
        files = [FileIdentity(rel_path="SkyrimSE.exe", size=10, digest="a" * 64)]
        bad_exp = CriticalFileExpectation(
            rel_path="SkyrimSE.exe",
            expected_digest="0" * 64,
            expected_size=10,
        )
        generator_exp = (exp for exp in [bad_exp])

        res = verify_critical_files(files, generator_exp)
        assert res == (
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.FAILED,
                message=f"Archivo crítico 'SkyrimSE.exe' no coincide: sha256 ({'a' * 64} vs {'0' * 64})",
                expected_digest="0" * 64,
                observed_digest="a" * 64,
                expected_size=10,
                observed_size=10,
            ),
        )

    def test_g3_valid_critical_generator_produce_verified(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        critical_for_g3 = [
            critical[0],
            critical[1],
            CriticalFileExpectation(
                rel_path=critical[2].rel_path,
                expected_digest=critical[2].expected_digest,
                expected_size=None,
            ),
        ]
        generator_exp = (exp for exp in critical_for_g3)

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=generator_exp,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.success is True
        assert res.descriptor is not None

        expected_evidencias: list[CriticalFileEvidence] = []
        for exp in critical_for_g3:
            physical_path = root.joinpath(*pathlib.PurePosixPath(exp.rel_path).parts)
            data = physical_path.read_bytes()
            real_digest = _sha256(data)
            real_size = len(data)

            expected_evidencias.append(
                CriticalFileEvidence(
                    rel_path=exp.rel_path,
                    state=VerificationState.VERIFIED,
                    message="",
                    expected_digest=exp.expected_digest,
                    observed_digest=real_digest,
                    expected_size=exp.expected_size,
                    observed_size=real_size,
                )
            )

        assert res.critical_results == tuple(expected_evidencias)

        evidence_no_expected_size = res.critical_results[2]
        assert evidence_no_expected_size.expected_size is None
        assert evidence_no_expected_size.observed_size is not None
        assert evidence_no_expected_size.observed_size == len(
            root.joinpath(*pathlib.PurePosixPath(critical_for_g3[2].rel_path).parts).read_bytes()
        )

    def test_g4_duplicate_critical_generator_levanta_error(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, critical = mock_candidate
        generator_exp = (exp for exp in (critical[0], critical[0]))

        with pytest.raises(ValueError, match="duplicadas"):
            verify_golden_master(
                candidate=root,
                expected_tree=tree_digest,
                expected_runtime=runtime_id,
                observed_runtime=runtime_id,
                critical_expectations=generator_exp,
            )

    def test_g5_empty_critical_generator_produce_verified(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
    ) -> None:
        root, tree_digest, runtime_id, _ = mock_candidate
        empty_gen = iter(())

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=empty_gen,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.success is True
        assert res.descriptor is not None
        assert res.critical_results == ()


class TestSameCaptureOracle:
    """Ancla que verify_critical_files recibe exactamente el mismo objeto capturado en inventory_tree."""

    def test_verify_critical_files_recibe_mismo_objeto_capturado(
        self,
        mock_candidate: tuple[pathlib.Path, TreeDigest, RuntimeIdentity, list[CriticalFileExpectation]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import sky_claw.local.runtime_vault.golden as golden_module

        root, tree_digest, runtime_id, critical = mock_candidate
        original_inventory = golden_module.inventory_tree
        original_verify_critical = golden_module.verify_critical_files

        inventarios: list[Sequence[FileIdentity]] = []
        capturas_criticas: list[Sequence[FileIdentity]] = []

        def spy_inventory_tree(path: pathlib.Path) -> Sequence[FileIdentity]:
            files = original_inventory(path)
            inventarios.append(files)
            return files

        def spy_verify_critical(
            files: Sequence[FileIdentity],
            expectations: Sequence[CriticalFileExpectation],
        ) -> tuple[CriticalFileEvidence, ...]:
            capturas_criticas.append(files)
            return original_verify_critical(files, expectations)

        monkeypatch.setattr(golden_module, "inventory_tree", spy_inventory_tree)
        monkeypatch.setattr(golden_module, "verify_critical_files", spy_verify_critical)

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.VERIFIED
        assert len(inventarios) == 1
        assert len(capturas_criticas) == 1
        assert capturas_criticas[0] is inventarios[0]
