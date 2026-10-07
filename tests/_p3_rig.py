"""Rig sintetico COMPARTIDO de la suite Frozen Runtime P3.

Vive en su propio modulo para que los archivos de test no dependan uno del otro
por import de fixtures (pytest resuelve las fixtures del modulo donde fueron
definidas, y arrastrarlas por import arrastra sus dependencias).
"""

from __future__ import annotations

import pathlib
import time

import pytest

from sky_claw.local.frozen_runtime.candidates import crear_candidate
from sky_claw.local.frozen_runtime.models import ManagedSource, ManagedSourceProvider
from sky_claw.local.frozen_runtime.storage import initialize_frozen_runtime_storage

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


def sin_op(_segundos: float) -> None:
    """Dormir no es necesario: los tests son deterministas, no temporizados."""
    return


def escribir_managed_source(steamapps: pathlib.Path) -> pathlib.Path:
    """Managed Source de Steam falsa con un directorio VACIO (identidad P3)."""
    common = steamapps / "common" / "Skyrim Special Edition"
    (common / "Data" / "Meshes" / "Characters").mkdir(parents=True)
    (common / "Data" / "Textures").mkdir(parents=True)
    (common / "Data" / "EmptyFolder").mkdir(parents=True)
    (common / "SkyrimSE.exe").write_bytes(b"fake-skyrimse-payload")
    (common / "Data" / "Meshes" / "Characters" / "a.nif").write_bytes(b"nif-payload")
    (common / "Data" / "Skyrim.esm").write_bytes(b"esm-payload")
    (steamapps / "appmanifest_489830.acf").write_text(MANIFEST_IDLE, encoding="utf-8")
    return common


def construir_source(steamapps: pathlib.Path) -> ManagedSource:
    common = steamapps / "common" / "Skyrim Special Edition"
    return ManagedSource(
        provider=ManagedSourceProvider.STEAM,
        game_key="skyrimse",
        appid="489830",
        root=common,
        library_steamapps=steamapps,
        library_root=steamapps.parent,
    )


@pytest.fixture
def parche_identidad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identidad PE falsa y estable (el exe sintetico no es un PE real).

    Se parchea en LOS DOS modulos que importan la primitive: P1 la usa a traves
    de ``observation`` y P3 a traves de ``membership`` (que la importa por
    nombre). Parchear solo uno dejaria al Candidate observando un PE sintetico
    ilegible.
    """
    from sky_claw.local.frozen_runtime import membership as membership_module
    from sky_claw.local.frozen_runtime import observation as observation_module
    from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation

    def identidad(root: pathlib.Path, *, expected_game_key: str = "skyrimse") -> FreshRuntimeObservation:
        return FreshRuntimeObservation(
            game_key=expected_game_key,
            game_version="1.6.1170.0",
            observed_exe_path=str(pathlib.Path(root) / "SkyrimSE.exe"),
            observed_at_ns=time.time_ns(),
        )

    monkeypatch.setattr(observation_module, "observe_runtime_identity_from_root", identidad)
    monkeypatch.setattr(membership_module, "observe_runtime_identity_from_root", identidad)


@pytest.fixture
def rig(tmp_path: pathlib.Path, parche_identidad: None) -> tuple[ManagedSource, pathlib.Path]:
    steamapps = tmp_path / "library" / "steamapps"
    steamapps.mkdir(parents=True)
    escribir_managed_source(steamapps)
    root = tmp_path / "frozen-runtime"
    assert initialize_frozen_runtime_storage(root).success
    return construir_source(steamapps), root


def crear(source: ManagedSource, root: pathlib.Path, cid: str | None = None):
    """Atajo de `crear_candidate` con ventana de estabilizacion en cero."""
    fabrica = (lambda: cid) if cid else None
    kwargs = {"id_factory": fabrica} if fabrica else {}
    return crear_candidate(source, root, quiet_window_seconds=0.0, sleep=sin_op, **kwargs)
