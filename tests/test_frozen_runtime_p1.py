"""Tests de P1 — Frozen Runtime: Managed Source discovery / identidad / estabilización.

Convención del repo: AAA en español, anclas enumerativas, sin sleeps largos
(ventana cero + hook de sleep inyectado). Los tests d*/i*/s* mapean al plan
del ADR 0012 §24.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import time

import pytest

from sky_claw.local.frozen_runtime import (
    DiscoveryState,
    FrozenRuntimeError,
    FrozenRuntimeObservationError,
    ManagedSource,
    ManagedSourceProvider,
    StabilityState,
    assess_managed_source_stability,
    discover_managed_source,
    observe_source_snapshot,
    obtain_stable_source_snapshot,
)
from sky_claw.local.frozen_runtime import observation as observation_module
from sky_claw.local.frozen_runtime.provider_signals import manifest_path_for
from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation, UnreadableRuntimeVersionError

MANIFEST_IDLE = (
    '"AppState"\n'
    "{\n"
    '\t"appid"\t\t"489830"\n'
    '\t"name"\t\t"Skyrim Special Edition"\n'
    '\t"StateFlags"\t\t"4"\n'
    '\t"buildid"\t\t"1234567"\n'
    '\t"BytesToDownload"\t\t"0"\n'
    '\t"BytesDownloaded"\t\t"0"\n'
    '\t"UpdateResult"\t\t"0"\n'
    '\t"installdir"\t\t"Skyrim Special Edition"\n'
    "}\n"
)

MANIFEST_UPDATE_ACTIVE = MANIFEST_IDLE.replace('"StateFlags"\t\t"4"', '"StateFlags"\t\t"6"')
MANIFEST_MALFORMED = '"AppState" {\n"StateFlags" "4" this is garbage'


def _escribir_juego(common: pathlib.Path, *, contenido_exe: bytes = b"fake-skyrimse") -> pathlib.Path:
    exe = common / "SkyrimSE.exe"
    exe.write_bytes(contenido_exe)
    data = common / "Data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "Skyrim.esm").write_bytes(b"esm-payload")
    return exe


def _construir_library(
    library: pathlib.Path, *, appid: str = "489830", manifest_text: str = MANIFEST_IDLE
) -> tuple[pathlib.Path, pathlib.Path]:
    steamapps = library / "steamapps"
    common = steamapps / "common" / "Skyrim Special Edition"
    common.mkdir(parents=True)
    _escribir_juego(common)
    (steamapps / f"appmanifest_{appid}.acf").write_text(manifest_text, encoding="utf-8")
    return steamapps, common


def _source(steamapps: pathlib.Path, common: pathlib.Path) -> ManagedSource:
    return ManagedSource(
        provider=ManagedSourceProvider.STEAM,
        game_key="skyrimse",
        appid="489830",
        root=common,
        library_steamapps=steamapps,
        library_root=steamapps.parent,
    )


def _parchear_identidad(
    monkeypatch: pytest.MonkeyPatch, versiones: list[str] | None = None
) -> list[tuple[pathlib.Path, str]]:
    """Parchea la observación de identidad PE; registra cada llamada (root, game_key)."""
    versiones = versiones or ["1.6.1170.0"]
    llamadas: list[tuple[pathlib.Path, str]] = []

    def identidad_falsa(root: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        indice = min(len(llamadas), len(versiones) - 1)
        llamadas.append((root, expected_game_key))
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version=versiones[indice],
            observed_exe_path=str(root / "SkyrimSE.exe"),
            observed_at_ns=time.time_ns(),
        )

    monkeypatch.setattr(observation_module, "observe_runtime_identity_from_root", identidad_falsa)
    return llamadas


# ── Discovery ────────────────────────────────────────────────────────────


class TestDiscovery:
    def test_d01_library_default_del_propio_steam(self, tmp_path: pathlib.Path) -> None:
        steam = tmp_path / "steam"
        steamapps, common = _construir_library(steam)
        resultado = discover_managed_source(steam_roots=(str(steam),))
        assert resultado.state is DiscoveryState.FOUND
        assert resultado.source is not None
        assert resultado.source.root == common
        assert resultado.source.library_steamapps == steamapps

    def test_d02_library_secundaria_via_vdf_moderno(self, tmp_path: pathlib.Path) -> None:
        steam = tmp_path / "steam"
        lib2 = tmp_path / "lib2"
        steam.mkdir(parents=True)
        (steam / "steamapps").mkdir()
        vdf = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"' + str(lib2).replace("\\", "\\\\") + '"\n\t}\n}\n'
        (steam / "steamapps" / "libraryfolders.vdf").write_text(vdf, encoding="utf-8")
        steamapps, common = _construir_library(lib2)
        resultado = discover_managed_source(steam_roots=(str(steam),))
        assert resultado.state is DiscoveryState.FOUND
        assert resultado.source is not None
        assert resultado.source.root == common
        assert resultado.source.library_steamapps == steamapps

    def test_d03_sin_skyrim(self, tmp_path: pathlib.Path) -> None:
        steam = tmp_path / "steam"
        (steam / "steamapps").mkdir(parents=True)
        resultado = discover_managed_source(steam_roots=(str(steam),))
        assert resultado.state is DiscoveryState.NOT_FOUND

    def test_d04_libraryfolders_malformado_fail_closed(self, tmp_path: pathlib.Path) -> None:
        steam = tmp_path / "steam"
        (steam / "steamapps").mkdir(parents=True)
        (steam / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders" { "0" { "path" ', encoding="utf-8")
        resultado = discover_managed_source(steam_roots=(str(steam),))
        assert resultado.state is DiscoveryState.NOT_FOUND
        assert "libraryfolders.vdf" in resultado.message

    def test_d05_appmanifest_malformado_no_rompe_discovery(self, tmp_path: pathlib.Path) -> None:
        steam = tmp_path / "steam"
        _construir_library(steam, manifest_text=MANIFEST_MALFORMED)
        resultado = discover_managed_source(steam_roots=(str(steam),))
        # la existencia del manifest es la evidencia de proveedor; su contenido es advisory
        assert resultado.state is DiscoveryState.FOUND

    def test_d06_dos_libraries_ambiguo(self, tmp_path: pathlib.Path) -> None:
        steam = tmp_path / "steam"
        lib2 = tmp_path / "lib2"
        steam.mkdir(parents=True)
        (steam / "steamapps").mkdir()
        vdf = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"' + str(lib2).replace("\\", "\\\\") + '"\n\t}\n}\n'
        (steam / "steamapps" / "libraryfolders.vdf").write_text(vdf, encoding="utf-8")
        _construir_library(steam)
        _construir_library(lib2)
        resultado = discover_managed_source(steam_roots=(str(steam),))
        assert resultado.state is DiscoveryState.AMBIGUOUS
        assert len(resultado.candidates) == 2

    def test_d07_ruta_externa_sin_evidencia_no_se_etiqueta_steam(self, tmp_path: pathlib.Path) -> None:
        copia = tmp_path / "copia"
        copia.mkdir()
        _escribir_juego(copia)
        resultado = discover_managed_source(explicit_root=copia)
        assert resultado.state is DiscoveryState.INVALID
        assert resultado.source is None
        assert "steamapps" in resultado.message

    def test_d08_ruta_explicita_sin_ejecutable(self, tmp_path: pathlib.Path) -> None:
        root_vacio = tmp_path / "lib2" / "steamapps" / "common" / "Skyrim Special Edition"
        root_vacio.mkdir(parents=True)
        (tmp_path / "lib2" / "steamapps" / "appmanifest_489830.acf").write_text(MANIFEST_IDLE, encoding="utf-8")
        resultado = discover_managed_source(explicit_root=root_vacio)
        assert resultado.state is DiscoveryState.INVALID
        assert "SkyrimSE.exe" in resultado.message

    def test_d09_ejecutable_equivocado_no_es_candidato(self, tmp_path: pathlib.Path) -> None:
        steam = tmp_path / "steam"
        steamapps = steam / "steamapps"
        common = steamapps / "common" / "Skyrim Special Edition"
        common.mkdir(parents=True)
        (common / "Skyrim.exe").write_bytes(b"oldrim")
        (steamapps / "appmanifest_489830.acf").write_text(MANIFEST_IDLE, encoding="utf-8")
        resultado = discover_managed_source(steam_roots=(str(steam),))
        assert resultado.state is DiscoveryState.NOT_FOUND

    def test_d10_symlink_en_root_rechazado(self, tmp_path: pathlib.Path) -> None:
        # una fuente redirigida no produce evidencia falsa (fail-closed)
        real = tmp_path / "lib"
        _construir_library(real)
        enlace = tmp_path / "enlace"
        try:
            enlace.symlink_to(real / "steamapps" / "common" / "Skyrim Special Edition", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks no disponibles en este entorno")
        resultado = discover_managed_source(explicit_root=enlace)
        assert resultado.state is DiscoveryState.INVALID
        assert "enlace" in resultado.message

    @pytest.mark.skipif(os.name != "nt", reason="junction es Windows-only")
    def test_d11_junction_en_root_rechazado(self, tmp_path: pathlib.Path) -> None:
        import subprocess

        real = tmp_path / "lib"
        _construir_library(real)
        enlace = tmp_path / "enlace"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(enlace), str(real / "steamapps" / "common" / "Skyrim Special Edition")],
            check=True,
            capture_output=True,
        )
        resultado = discover_managed_source(explicit_root=enlace)
        assert resultado.state is DiscoveryState.INVALID
        assert "enlace" in resultado.message


# ── Identidad / observación ──────────────────────────────────────────────


class TestIdentidad:
    def test_i01_identidad_valida_y_evidencia(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        snapshot = observe_source_snapshot(source)
        assert snapshot.runtime_identity.game_key == "skyrimse"
        assert snapshot.runtime_identity.game_version == "1.6.1170.0"
        assert snapshot.tree_digest.files >= 2
        assert snapshot.provider_metadata.buildid == "1234567"
        assert snapshot.provider_metadata.manifest_readable

    def test_i02_game_key_no_soportado_falla_en_el_modelo(self, tmp_path: pathlib.Path) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        with pytest.raises(FrozenRuntimeError):
            ManagedSource(
                provider=ManagedSourceProvider.STEAM,
                game_key="skyrim",
                appid="489830",
                root=common,
                library_steamapps=steamapps,
                library_root=steamapps.parent,
            )

    def test_i03_ejecutable_ilegible_falla_cerrado(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)

        def identidad_ilegible(root: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
            raise UnreadableRuntimeVersionError("sin recurso de versión")

        monkeypatch.setattr(observation_module, "observe_runtime_identity_from_root", identidad_ilegible)
        with pytest.raises(FrozenRuntimeObservationError):
            observe_source_snapshot(source)

    def test_i04_evidencia_critica_skyrimse_capturada(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        payload = b"contenido-critico"
        (common / "SkyrimSE.exe").write_bytes(payload)
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        snapshot = observe_source_snapshot(source)
        esperado = hashlib.sha256(payload).hexdigest()
        criticos = [c for c in snapshot.critical_files if c.rel_path.casefold() == "skyrimse.exe"]
        assert len(criticos) == 1
        assert criticos[0].digest == esperado
        assert criticos[0].size == len(payload)

    def test_i05_observacion_siempre_fresca(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        llamadas = _parchear_identidad(monkeypatch)
        observe_source_snapshot(source)
        (common / "Data" / "Skyrim.esm").write_bytes(b"mutado")
        observe_source_snapshot(source)
        assert len(llamadas) == 2


# ── Estabilización ───────────────────────────────────────────────────────


def _no_op(_segundos: float) -> None:
    return None


class TestEstabilizacion:
    def _assess(self, source: ManagedSource, sleep) -> object:
        return assess_managed_source_stability(source, quiet_window_seconds=0.0, sleep=sleep)

    def test_s01_pre_post_identicos_stable(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        resultado = self._assess(source, _no_op)
        assert resultado.state is StabilityState.STABLE
        assert resultado.pre_tree_digest == resultado.post_tree_digest

    def test_s02_archivo_modificado_unstable(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)

        def hook(_s: float) -> None:
            (common / "Data" / "Skyrim.esm").write_bytes(b"mutado-en-ventana")

        resultado = self._assess(source, hook)
        assert resultado.state is StabilityState.UNSTABLE

    def test_s03_archivo_agregado_unstable(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)

        def hook(_s: float) -> None:
            (common / "Data" / "nuevo.esp").write_bytes(b"nuevo")

        resultado = self._assess(source, hook)
        assert resultado.state is StabilityState.UNSTABLE

    def test_s04_archivo_eliminado_unstable(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)

        def hook(_s: float) -> None:
            (common / "Data" / "Skyrim.esm").unlink()

        resultado = self._assess(source, hook)
        assert resultado.state is StabilityState.UNSTABLE

    def test_s05_identidad_runtime_cambia_unstable(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch, versiones=["1.6.1170.0", "1.7.1000.0"])
        resultado = self._assess(source, _no_op)
        assert resultado.state is StabilityState.UNSTABLE
        assert "identidad de runtime" in resultado.message

    def test_s06_senal_provider_activa_precheck(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib", manifest_text=MANIFEST_UPDATE_ACTIVE)
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        resultado = self._assess(source, _no_op)
        assert resultado.state is StabilityState.UNSTABLE

    def test_s07_manifest_malformado_indeterminate(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib", manifest_text=MANIFEST_MALFORMED)
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        resultado = self._assess(source, _no_op)
        assert resultado.state is StabilityState.INDETERMINATE

    def test_s08_inventario_imposible_indeterminate(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)

        def hook(_s: float) -> None:
            for hijo in common.iterdir():
                if hijo.is_file():
                    hijo.unlink()
            (common / "Data" / "Skyrim.esm").unlink()

        resultado = self._assess(source, hook)
        assert resultado.state is StabilityState.INDETERMINATE

    def test_s09_buildid_cambia_unstable(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)

        def hook(_s: float) -> None:
            manifest_path_for(source).write_text(MANIFEST_IDLE.replace("1234567", "9999999"), encoding="utf-8")

        resultado = self._assess(source, hook)
        assert resultado.state is StabilityState.UNSTABLE
        assert "buildid" in resultado.message

    def test_s10_metadata_igual_arbol_cambiado_unstable(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)

        def hook(_s: float) -> None:
            (common / "Data" / "Skyrim.esm").write_bytes(b"cambiado")

        resultado = self._assess(source, hook)
        assert resultado.state is StabilityState.UNSTABLE
        assert resultado.pre_provider is not None and resultado.post_provider is not None
        assert resultado.pre_provider.buildid == resultado.post_provider.buildid == "1234567"

    def test_s11_arbol_igual_provider_activo_postcheck(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)

        def hook(_s: float) -> None:
            manifest_path_for(source).write_text(MANIFEST_UPDATE_ACTIVE, encoding="utf-8")

        resultado = self._assess(source, hook)
        assert resultado.state is StabilityState.UNSTABLE

    def test_sadv_mutacion_retorna_a_misma_version_superficial(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PRE = A; mutar; restaurar misma versión/buildid superficiales; POST = B ⇒ UNSTABLE
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)  # misma ProductVersion en ambas observaciones

        def hook(_s: float) -> None:
            (common / "SkyrimSE.exe").write_bytes(b"otro-contenido-misma-long")  # misma longitud, otro digest

        resultado = self._assess(source, hook)
        assert resultado.state is StabilityState.UNSTABLE
        assert "árbol cambió" in resultado.message

    def test_toctou_snapshot_pertenece_a_la_misma_ventana(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        observacion = obtain_stable_source_snapshot(source, quiet_window_seconds=0.0, sleep=_no_op)
        assert observacion.success
        assert observacion.snapshot is not None
        assert observacion.stability.post_tree_digest is not None
        assert observacion.snapshot.tree_digest == observacion.stability.post_tree_digest

    def test_toctou_unstable_sin_snapshot(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)

        def hook(_s: float) -> None:
            (common / "Data" / "Skyrim.esm").write_bytes(b"mutado")

        observacion = obtain_stable_source_snapshot(source, quiet_window_seconds=0.0, sleep=hook)
        assert not observacion.success
        assert observacion.snapshot is None
        assert observacion.stability.state is StabilityState.UNSTABLE

    def test_ventana_negativa_rechazada(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        with pytest.raises(FrozenRuntimeError):
            assess_managed_source_stability(source, quiet_window_seconds=-1.0)


# ── SFR-15 / autoridad ───────────────────────────────────────────────────


class TestAutoridadDeEvidencia:
    def test_mismo_buildid_distinto_arbol_distinta_evidencia(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        antes = observe_source_snapshot(source)
        (common / "Data" / "Skyrim.esm").write_bytes(b"cambiado")
        despues = observe_source_snapshot(source)
        assert antes.provider_metadata.buildid == despues.provider_metadata.buildid
        assert antes.tree_digest.digest != despues.tree_digest.digest

    def test_sin_archivo_critico_no_hay_evidencia(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steamapps, common = _construir_library(tmp_path / "lib")
        (common / "SkyrimSE.exe").unlink()
        source = _source(steamapps, common)
        _parchear_identidad(monkeypatch)
        with pytest.raises(FrozenRuntimeObservationError):
            observe_source_snapshot(source)

    def test_evidencia_exige_campos_no_vacios(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.frozen_runtime.models import ProviderMetadataObservation, SourceSnapshotEvidence
        from sky_claw.local.runtime_vault.models import FileIdentity, RuntimeIdentity, TreeDigest

        with pytest.raises(FrozenRuntimeError):
            SourceSnapshotEvidence(
                provider=ManagedSourceProvider.STEAM,
                game_key="skyrimse",
                runtime_identity=RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
                tree_digest=TreeDigest(digest="x", files=1, bytes=1),
                files=(FileIdentity(rel_path="SkyrimSE.exe", size=1, digest="x"),),
                critical_files=(),
                provider_metadata=ProviderMetadataObservation(provider=ManagedSourceProvider.STEAM, appid="489830"),
                observed_at_ns=1,
            )
