"""Tests de P2 — generations: identity, metadata, discovery y drift (G*/DR*).

La Generation sintética se construye en staging, se inventaría, se deriva su
id por contenido y se copia a ``versions/<id>`` — el digest usa relpaths, así
que copiar no lo cambia.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

from sky_claw.local.frozen_runtime import (
    GenerationCollisionError,
    GenerationVerificationState,
    InvalidGenerationIdError,
    ManagedSourceProvider,
    ProviderMetadataObservation,
    SourceSnapshotEvidence,
    StateCorruptError,
    construir_generation_id,
    descubrir_generations,
    generacion_id_desde_evidencia,
    initialize_frozen_runtime_storage,
    leer_generation_metadata,
    registrar_generation_metadata,
    validar_generation_id,
    verificar_generation,
)
from sky_claw.local.frozen_runtime.state import write_json_atomic
from sky_claw.local.frozen_runtime.storage import generation_dir, generations_state_dir, versions_dir
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import RuntimeIdentity, TreeDigest
from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

VERSION_JUEGO = "1.6.1170.0"


def _parchear_identidad(monkeypatch: pytest.MonkeyPatch, version: str = VERSION_JUEGO) -> None:
    """La identidad PE real no existe en árboles sintéticos: se inyecta."""

    def identidad_falsa(root: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version=version,
            observed_exe_path=str(root / "SkyrimSE.exe"),
            observed_at_ns=1,
        )

    monkeypatch.setattr("sky_claw.local.frozen_runtime.generations.observe_runtime_identity_from_root", identidad_falsa)


def _construir_evidencia(arbol: pathlib.Path, *, version: str = VERSION_JUEGO) -> SourceSnapshotEvidence:
    files = inventory_tree(arbol)
    digest = tree_digest_from_files(files)
    criticos = tuple(f for f in files if f.rel_path.casefold() == "skyrimse.exe")
    assert criticos, "el árbol de prueba debe tener SkyrimSE.exe"
    return SourceSnapshotEvidence(
        provider=ManagedSourceProvider.STEAM,
        game_key="skyrimse",
        runtime_identity=RuntimeIdentity(game_key="skyrimse", game_version=version),
        tree_digest=digest,
        files=files,
        critical_files=criticos,
        provider_metadata=ProviderMetadataObservation(
            provider=ManagedSourceProvider.STEAM, appid="489830", buildid="1234567"
        ),
        observed_at_ns=1000,
    )


def _crear_arbol_generacion(staging: pathlib.Path) -> pathlib.Path:
    (staging / "Data").mkdir(parents=True)
    (staging / "SkyrimSE.exe").write_bytes(b"exe-de-generacion")
    (staging / "Data" / "Skyrim.esm").write_bytes(b"esm-payload")
    return staging


def _publicar_generacion(root: pathlib.Path, tmp_path: pathlib.Path, *, version: str = VERSION_JUEGO) -> str:
    """Crea el árbol en staging, deriva el id por contenido y lo publica en versions/."""
    staging = _crear_arbol_generacion(tmp_path / "staging")
    evidencia = _construir_evidencia(staging, version=version)
    generation_id = generacion_id_desde_evidencia(evidencia)
    destino = generation_dir(root, generation_id)
    shutil.copytree(staging, destino)
    registrar_generation_metadata(root, evidencia)
    return generation_id


# ── Generation-id ────────────────────────────────────────────────────────


class TestGenerationId:
    def test_g01_id_valido_version_mas_digest(self) -> None:
        digest = TreeDigest(digest="a1b2c3d4e5f6" + "0" * 52, files=2, bytes=10)
        gid = construir_generation_id("1.6.1170", digest)
        assert gid == "1.6.1170__a1b2c3d4e5f6"
        assert validar_generation_id(gid.upper()) == gid  # normaliza casefold

    def test_g02_misma_version_distinto_digest_distinto_id(self) -> None:
        a = construir_generation_id("1.6.1170", TreeDigest(digest="a" * 64, files=1, bytes=1))
        b = construir_generation_id("1.6.1170", TreeDigest(digest="b" * 64, files=1, bytes=1))
        assert a != b

    @pytest.mark.parametrize(
        "invalido",
        [
            "../evil__aaaaaaaaaaaa",
            "..\\evil__aaaaaaaaaaaa",
            "1.6.1170__../../etc",
            "/absoluto__aaaaaaaaaaaa",
            "C:drive__aaaaaaaaaaaa",
            "con__aaaaaaaaaaaa",
            "nul.1__aaaaaaaaaaaa",
            "1.6.1170. __aaaaaaaaaaaa",
            "1.6.1170__aaaaaaaaaaaa.",
            "",
        ],
    )
    def test_g03_g05_traversal_separadores_y_reservados_rechazados(self, invalido: str) -> None:
        with pytest.raises(InvalidGenerationIdError):
            validar_generation_id(invalido)

    def test_g06_mismo_id_misma_identidad_idempotente(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        staging = _crear_arbol_generacion(tmp_path / "staging")
        evidencia = _construir_evidencia(staging)
        primera = registrar_generation_metadata(root, evidencia)
        segunda = registrar_generation_metadata(root, evidencia)
        assert primera == segunda
        metadata_files = list(generations_state_dir(root).glob("*.json"))
        assert len(metadata_files) == 1

    def test_g07_mismo_id_identidad_distinta_colision(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        staging = _crear_arbol_generacion(tmp_path / "staging")
        evidencia = _construir_evidencia(staging, version="1.6.1170.0")
        registrar_generation_metadata(root, evidencia)
        # mismo display (1.6.1170) y mismo digest ⇒ mismo id, distinta RuntimeIdentity
        evidencia_distinta = _construir_evidencia(staging, version="1.6.1170.1")
        assert generacion_id_desde_evidencia(evidencia_distinta) == generacion_id_desde_evidencia(evidencia)
        with pytest.raises(GenerationCollisionError):
            registrar_generation_metadata(root, evidencia_distinta)

    def test_g08_digest_completo_retenido_en_metadata(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gid = _publicar_generacion(root, tmp_path)
        metadata = leer_generation_metadata(root, gid)
        assert len(metadata.tree_digest.digest) == 64
        assert gid.endswith(metadata.tree_digest.digest[:12])
        assert metadata.provider == "steam"
        assert metadata.provider_buildid == "1234567"  # auxiliar, no identidad


# ── Drift verification ───────────────────────────────────────────────────


class TestDrift:
    @pytest.fixture(autouse=True)
    def _identidad_sintetica(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _parchear_identidad(monkeypatch)

    def _publicada(self, tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gid = _publicar_generacion(root, tmp_path)
        return root, gid

    def test_dr01_unchanged_valid(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        resultado = verificar_generation(root, gid)
        assert resultado.state is GenerationVerificationState.VALID
        assert resultado.observed_digest == resultado.recorded.tree_digest  # type: ignore[union-attr]

    def test_dr02_archivo_modificado_drifted(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        (generation_dir(root, gid) / "Data" / "Skyrim.esm").write_bytes(b"mutado")
        assert verificar_generation(root, gid).state is GenerationVerificationState.DRIFTED

    def test_dr03_archivo_agregado_drifted(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        (generation_dir(root, gid) / "Data" / "extra.esp").write_bytes(b"extra")
        assert verificar_generation(root, gid).state is GenerationVerificationState.DRIFTED

    def test_dr04_archivo_eliminado_drifted(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        (generation_dir(root, gid) / "Data" / "Skyrim.esm").unlink()
        assert verificar_generation(root, gid).state is GenerationVerificationState.DRIFTED

    def test_dr05_ejecutable_cambiado_drifted(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        exe = generation_dir(root, gid) / "SkyrimSE.exe"
        original = exe.read_bytes()
        exe.write_bytes(b"X" * len(original))  # misma longitud, otro contenido
        assert verificar_generation(root, gid).state is GenerationVerificationState.DRIFTED

    def test_dr06_metadata_corrupta_indeterminate(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        (generations_state_dir(root) / f"{gid}.json").write_text("{ roto", encoding="utf-8")
        assert verificar_generation(root, gid).state is GenerationVerificationState.INDETERMINATE

    def test_dr07_identidad_distinta_drifted(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        metadata = leer_generation_metadata(root, gid)
        payload = {
            "schema_version": 1,
            "generation_id": gid,
            "display_version": metadata.display_version,
            "runtime_identity": {"game_key": "skyrimse", "game_version": "9.9.9.9"},
            "tree_digest": {
                "digest": metadata.tree_digest.digest,
                "files": metadata.tree_digest.files,
                "bytes": metadata.tree_digest.bytes,
            },
            "critical_files": [
                {"rel_path": c.rel_path, "size": c.size, "digest": c.digest} for c in metadata.critical_files
            ],
            "provider": metadata.provider,
            "provider_appid": metadata.provider_appid,
            "provider_buildid": metadata.provider_buildid,
            "created_at_ns": metadata.created_at_ns,
        }
        write_json_atomic(generations_state_dir(root) / f"{gid}.json", payload)
        assert verificar_generation(root, gid).state is GenerationVerificationState.DRIFTED

    def test_dr08_generation_ausente_invalid(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        resultado = verificar_generation(root, "1.6.1170__aaaaaaaaaaaa")
        assert resultado.state is GenerationVerificationState.INVALID

    def test_dr09_metadata_ausente_indeterminate(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        staging = _crear_arbol_generacion(tmp_path / "staging")
        evidencia = _construir_evidencia(staging)
        gid = generacion_id_desde_evidencia(evidencia)
        shutil.copytree(staging, generation_dir(root, gid))
        resultado = verificar_generation(root, gid)
        assert resultado.state is GenerationVerificationState.INDETERMINATE

    def test_dr10_metadata_corrupta_no_se_confunde_con_vacia(self, tmp_path: pathlib.Path) -> None:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gid = "1.6.1170__aaaaaaaaaaaa"
        (versions_dir(root) / gid).mkdir()
        (generations_state_dir(root) / f"{gid}.json").write_text("", encoding="utf-8")
        with pytest.raises(StateCorruptError):
            leer_generation_metadata(root, gid)

    def test_dr11_discovery_clasifica_sin_activar(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        (versions_dir(root) / "carpeta_ajena").mkdir()
        (versions_dir(root) / "archivo_suelto.txt").write_text("x", encoding="utf-8")
        inventario = descubrir_generations(root)
        por_nombre = {r.directory.name: r for r in inventario.records}
        assert por_nombre[gid].state is GenerationVerificationState.UNKNOWN  # conocida, verificación on-demand
        assert por_nombre[gid].metadata is not None
        assert por_nombre["carpeta_ajena"].state is GenerationVerificationState.UNKNOWN
        assert por_nombre["carpeta_ajena"].metadata is None
        assert por_nombre["archivo_suelto.txt"].state is GenerationVerificationState.INVALID


def _enlace_directorio(destino: pathlib.Path, enlace: pathlib.Path) -> None:
    """Enlace de directorio: junction en Windows, symlink en POSIX."""
    if os.name == "nt":
        import subprocess

        subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(destino)], check=True, capture_output=True)
    else:
        enlace.symlink_to(destino, target_is_directory=True)


def _hardlink(objetivo: pathlib.Path, enlace: pathlib.Path) -> None:
    try:
        os.link(objetivo, enlace)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks no disponibles en este filesystem: {exc}")


class TestIntegridadFisicaPostPublicacion:
    """SFR-18 on-demand en ``verificar_generation`` (P2-B2): mismo digest ≠ VALID."""

    @pytest.fixture(autouse=True)
    def _identidad_sintetica(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _parchear_identidad(monkeypatch)

    def _publicada(self, tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
        root = tmp_path / "frozen"
        initialize_frozen_runtime_storage(root)
        gid = _publicar_generacion(root, tmp_path)
        return root, gid

    def _digest_actual(self, generacion: pathlib.Path) -> str:
        return tree_digest_from_files(inventory_tree(generacion)).digest

    def test_pi11_hardlink_post_publicacion_mismo_digest_no_valid(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        gen = generation_dir(root, gid)
        objetivo = gen / "Data" / "Skyrim.esm"
        externo = tmp_path / "externo.esm"
        externo.write_bytes(objetivo.read_bytes())
        objetivo.unlink()
        _hardlink(externo, objetivo)  # mismo file object, mismos bytes
        assert self._digest_actual(gen) == leer_generation_metadata(root, gid).tree_digest.digest
        resultado = verificar_generation(root, gid)
        assert resultado.state is GenerationVerificationState.INVALID

    def test_pi12_skyrimse_hardlink_no_valid(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        gen = generation_dir(root, gid)
        objetivo = gen / "SkyrimSE.exe"
        externo = tmp_path / "externo.exe"
        externo.write_bytes(objetivo.read_bytes())
        objetivo.unlink()
        _hardlink(externo, objetivo)
        assert self._digest_actual(gen) == leer_generation_metadata(root, gid).tree_digest.digest
        assert verificar_generation(root, gid).state is GenerationVerificationState.INVALID

    def test_pi14_sin_managed_source_verificacion_valida(self, tmp_path: pathlib.Path) -> None:
        # No existe ninguna Managed Source en el escenario: la verificación de la
        # Generation no la necesita (rollback futuro sin Steam).
        root, gid = self._publicada(tmp_path)
        assert verificar_generation(root, gid).state is GenerationVerificationState.VALID

    def test_pi15_reparse_post_publicacion_no_valid(self, tmp_path: pathlib.Path) -> None:
        root, gid = self._publicada(tmp_path)
        gen = generation_dir(root, gid)
        externo = tmp_path / "externo"
        externo.mkdir()
        _enlace_directorio(externo, gen / "Data" / "enlace")
        resultado = verificar_generation(root, gid)
        assert resultado.state is GenerationVerificationState.INVALID
