"""Tests unitarios y causales para FreshRuntimeObservation (R01..R04).

Contratos:
- R01: El caller no puede inyectar `observed_runtime` como evidencia; la observación
  mide el ejecutable en el disco.
- R02: Copiar `expected_runtime` como `observed_runtime` es imposible / detectado.
- R03: Modificar la versión observable del runtime entre OBSERVE y VERIFY hace que
  VERIFY vuelva a medir y falle (drift detection).
- R04: Si el runtime no puede medirse (ejecutable ausente o versión ilegible),
  falla cerrado con error tipado y nunca emite VERIFIED.
- Anti-ambigüedad: Si coexisten múltiples ejecutables de runtime (ej. SkyrimSE.exe y Skyrim.exe)
  o ejecutables contradictorios, lanza AmbiguousRuntimeError (sin priorización oportunista).
- Freshness: Cada llamada re-mide y emite un timestamp / observación frescos.
"""

from __future__ import annotations

import pathlib
import time
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.models import RuntimeIdentity
from sky_claw.local.runtime_vault.runtime_observation import (
    AmbiguousRuntimeError,
    ExecutableNotFoundError,
    RuntimeObservationError,
    UnreadableRuntimeVersionError,
    observe_runtime_identity_from_root,
)


class TestRuntimeObservationContract:
    def test_r01_caller_no_puede_inyectar_observed_runtime(self, tmp_path: pathlib.Path) -> None:
        """R01: La firma no acepta observed_runtime de entrada; mide el disco."""
        import inspect

        params = inspect.signature(observe_runtime_identity_from_root).parameters
        assert "observed_runtime" not in params
        assert "candidate_runtime" not in params
        assert "staged_runtime" not in params

    def test_r02_copiar_expected_runtime_es_imposible(self, tmp_path: pathlib.Path) -> None:
        """R02: expected_runtime no se usa como fallback ni se copia a la observación."""
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"dummy")

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            obs = observe_runtime_identity_from_root(tmp_path, expected_game_key="skyrimse")
            assert obs.game_version == "1.6.1170.0"
            assert obs.game_key == "skyrimse"
            assert obs.runtime_identity == RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")

    def test_r03_drift_de_version_detectado_en_remedicion(self, tmp_path: pathlib.Path) -> None:
        """R03: Si la versión en disco cambia entre pasadas, una nueva medición refleja el cambio."""
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"dummy")

        with patch(
            "sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version",
            side_effect=["1.6.640.0", "1.6.1170.0"],
        ):
            obs1 = observe_runtime_identity_from_root(tmp_path, expected_game_key="skyrimse")
            obs2 = observe_runtime_identity_from_root(tmp_path, expected_game_key="skyrimse")

            assert obs1.game_version == "1.6.640.0"
            assert obs2.game_version == "1.6.1170.0"
            assert obs1.game_version != obs2.game_version

    def test_r04_ejecutable_ausente_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """R04: Si no existe el ejecutable esperado en la raíz, falla con ExecutableNotFoundError."""
        with pytest.raises(ExecutableNotFoundError, match="No se encontró el ejecutable"):
            observe_runtime_identity_from_root(tmp_path, expected_game_key="skyrimse")

    def test_r04_version_ilegible_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """R04: Si read_skyrim_version devuelve cadena vacía, falla con UnreadableRuntimeVersionError."""
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"corrupted_or_no_pe_version")

        with (
            patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value=""),
            pytest.raises(UnreadableRuntimeVersionError, match="No se pudo leer la versión"),
        ):
            observe_runtime_identity_from_root(tmp_path, expected_game_key="skyrimse")

    def test_anti_ambiguedad_multiples_ejecutables_rechazados(self, tmp_path: pathlib.Path) -> None:
        """Si en root coexisten SkyrimSE.exe y Skyrim.exe, no prioriza oportunistamente: fail-closed."""
        (tmp_path / "SkyrimSE.exe").write_bytes(b"se")
        (tmp_path / "Skyrim.exe").write_bytes(b"le")

        with pytest.raises(AmbiguousRuntimeError, match="Coexisten múltiples ejecutables de runtime"):
            observe_runtime_identity_from_root(tmp_path, expected_game_key="skyrimse")

    def test_game_key_desconocido_rechazado(self, tmp_path: pathlib.Path) -> None:
        """game_key no soportado falla cerrado con RuntimeObservationError."""
        with pytest.raises(RuntimeObservationError, match="game_key no reconocido"):
            observe_runtime_identity_from_root(tmp_path, expected_game_key="fallout4")

    def test_freshness_timestamps_distintos(self, tmp_path: pathlib.Path) -> None:
        """Cada medición es fresca y registra timestamp reciente."""
        exe = tmp_path / "SkyrimSE.exe"
        exe.write_bytes(b"dummy")

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            t_before = time.time_ns()
            obs = observe_runtime_identity_from_root(tmp_path, expected_game_key="skyrimse")
            t_after = time.time_ns()

            assert t_before <= obs.observed_at_ns <= t_after
            assert obs.observed_exe_path.lower() == str(exe).lower()
