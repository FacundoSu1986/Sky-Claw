"""PR-3c (ADR 0019 §2.1): traductor V2 → ``CliPatcherSettings`` para la Fase 2.

Bajo USVFS, Synthesis no debe compilar ni abrir red: cada patcher seleccionado (Git, Solution o
CLI) se convierte en un ``CliPatcherSettings`` cuyo ``PathToExecutable`` es su shim (PR-3b). El
traductor vive DENTRO de ``prepare_synthesis_cli_settings``, el único productor del settings de
la corrida: un segundo materializador sería el hermano que alguien olvida actualizar.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from sky_claw.local.tools import patcher_pipeline as pp
from sky_claw.local.tools.patcher_pipeline import PatcherPipeline, PatcherPipelineError

_GIT = "Synthesis.Bethesda.Execution.Settings.GithubPatcherSettings, Synthesis.Bethesda.Execution"
_SLN = "Synthesis.Bethesda.Execution.Settings.SolutionPatcherSettings, Synthesis.Bethesda.Execution"
_CLI = "Synthesis.Bethesda.Execution.Settings.CliPatcherSettings, Synthesis.Bethesda.Execution"


def _settings(tmp_path: pathlib.Path) -> pathlib.Path:
    perfil: dict[str, Any] = {
        "$type": "Synthesis.Bethesda.Execution.Settings.V2.SynthesisProfile, Synthesis.Bethesda.Execution",
        "ID": "perfil-1",
        "Nickname": "Default",
        "TargetRelease": "SkyrimSE",
        "Groups": [
            {
                "Name": "Synthesis",
                "On": True,
                "BlacklistedMods": [],
                "Patchers": [
                    {
                        "$type": _GIT,
                        "On": True,
                        "ID": "git-1",
                        # Sin Nickname: Synthesis imprime el nombre del repo.
                        "RemoteRepoPath": "https://github.com/autor/RepoDelPatcher",
                        "SelectedProjectSubpath": "Patcher/Patcher.csproj",
                        "PatcherVersioning": {"Versioning": "Tag"},
                    },
                    {
                        "$type": _SLN,
                        "On": True,
                        "ID": "sln-1",
                        "Nickname": "Solucion",
                        "SolutionPath": "C:/fuentes/Sol.sln",
                        "ProjectSubpath": "Proy/Proy.csproj",
                    },
                    {"$type": _GIT, "On": False, "ID": "apagado", "Nickname": "Apagado"},
                ],
            },
            {
                "Name": "Otro",
                "On": True,
                "Patchers": [
                    {"$type": _CLI, "On": True, "ID": "cli-1", "Nickname": "Cli", "PathToExecutable": "C:/viejo.exe"}
                ],
            },
        ],
    }
    ruta = tmp_path / "PipelineSettings.json"
    ruta.write_text(json.dumps({"Version": 2, "Profiles": [perfil]}), encoding="utf-8")
    return ruta


def _shims(tmp_path: pathlib.Path, *ids: str) -> dict[str, pathlib.Path]:
    return {i: tmp_path / "shims" / i / "SkyClawSynthesisShim.exe" for i in ids}


def _leer(ruta: pathlib.Path) -> dict[str, Any]:
    try:
        datos: dict[str, Any] = json.loads(ruta.read_text(encoding="utf-8"))
        return datos
    finally:
        ruta.unlink(missing_ok=True)


def test_cada_patcher_seleccionado_pasa_a_cli_apuntando_a_su_shim(tmp_path: pathlib.Path) -> None:
    shims = _shims(tmp_path, "git-1", "sln-1", "cli-1")
    temporal, perfil = PatcherPipeline.prepare_synthesis_cli_settings(
        _settings(tmp_path), "Default", cli_executables=shims
    )
    grupos = _leer(temporal)["Profiles"][0]["Groups"]
    assert [[p for p in g["Patchers"]] for g in grupos] == [
        [
            {"$type": _CLI, "On": True, "Nickname": "RepoDelPatcher", "PathToExecutable": str(shims["git-1"])},
            {"$type": _CLI, "On": True, "Nickname": "Solucion", "PathToExecutable": str(shims["sln-1"])},
        ],
        [{"$type": _CLI, "On": True, "Nickname": "Cli", "PathToExecutable": str(shims["cli-1"])}],
    ]
    # Los recibos de ConsoleReporter se buscan por este nombre: no puede cambiar al traducir.
    assert perfil.patcher_nicknames == ("RepoDelPatcher", "Solucion", "Cli")


def test_la_forma_del_cli_patcher_es_cerrada() -> None:
    # Cualquier campo de Git/Solution que sobreviva (RemoteRepoPath, SolutionPath...) haría que
    # Synthesis intente compilar o clonar bajo USVFS.
    assert frozenset({"$type", "On", "Nickname", "PathToExecutable"}) == pp.CLAVES_CLI_PATCHER_TRADUCIDO


def test_respeta_la_seleccion_de_patchers(tmp_path: pathlib.Path) -> None:
    shims = _shims(tmp_path, "sln-1")
    temporal, _ = PatcherPipeline.prepare_synthesis_cli_settings(
        _settings(tmp_path), "Default", ["Solucion"], cli_executables=shims
    )
    grupos = _leer(temporal)["Profiles"][0]["Groups"]
    assert [p["PathToExecutable"] for g in grupos for p in g["Patchers"]] == [str(shims["sln-1"])]


def test_las_claves_se_comparan_sin_mayusculas(tmp_path: pathlib.Path) -> None:
    shims = {"GIT-1": tmp_path / "a.exe", "Sln-1": tmp_path / "b.exe", "cli-1": tmp_path / "c.exe"}
    temporal, _ = PatcherPipeline.prepare_synthesis_cli_settings(_settings(tmp_path), "Default", cli_executables=shims)
    _leer(temporal)


@pytest.mark.parametrize(
    ("ids", "patron"),
    [
        (("git-1", "sln-1"), "sin ejecutable"),  # falta cli-1
        (("git-1", "sln-1", "cli-1", "apagado"), "no seleccionado"),  # sobra uno deshabilitado
        (("git-1", "sln-1", "cli-1", "inventado"), "no seleccionado"),
    ],
    ids=["falta", "sobra_deshabilitado", "sobra_desconocido"],
)
def test_el_mapa_debe_cubrir_exactamente_la_seleccion(
    tmp_path: pathlib.Path, ids: tuple[str, ...], patron: str
) -> None:
    with pytest.raises(PatcherPipelineError, match=patron):
        PatcherPipeline.prepare_synthesis_cli_settings(
            _settings(tmp_path), "Default", cli_executables=_shims(tmp_path, *ids)
        )


def test_claves_duplicadas_por_mayusculas_fallan(tmp_path: pathlib.Path) -> None:
    shims = {**_shims(tmp_path, "git-1", "sln-1", "cli-1"), "CLI-1": tmp_path / "otro.exe"}
    with pytest.raises(PatcherPipelineError, match="duplicad"):
        PatcherPipeline.prepare_synthesis_cli_settings(_settings(tmp_path), "Default", cli_executables=shims)


def test_ruta_relativa_falla(tmp_path: pathlib.Path) -> None:
    shims = {**_shims(tmp_path, "git-1", "sln-1"), "cli-1": pathlib.Path("shims/cli.exe")}
    with pytest.raises(PatcherPipelineError, match="absoluta"):
        PatcherPipeline.prepare_synthesis_cli_settings(_settings(tmp_path), "Default", cli_executables=shims)


def test_no_modifica_el_settings_del_usuario(tmp_path: pathlib.Path) -> None:
    settings = _settings(tmp_path)
    antes = settings.read_bytes()
    temporal, _ = PatcherPipeline.prepare_synthesis_cli_settings(
        settings, "Default", cli_executables=_shims(tmp_path, "git-1", "sln-1", "cli-1")
    )
    _leer(temporal)
    assert settings.read_bytes() == antes


def test_sin_mapa_el_comportamiento_previo_no_cambia(tmp_path: pathlib.Path) -> None:
    temporal, _ = PatcherPipeline.prepare_synthesis_cli_settings(_settings(tmp_path), "Default")
    patchers = [p for g in _leer(temporal)["Profiles"][0]["Groups"] for p in g["Patchers"]]
    assert [p["$type"] for p in patchers] == [_GIT, _SLN, _CLI]
    assert patchers[0]["RemoteRepoPath"] == "https://github.com/autor/RepoDelPatcher"


def test_el_resto_del_endurecimiento_se_mantiene(tmp_path: pathlib.Path) -> None:
    temporal, _ = PatcherPipeline.prepare_synthesis_cli_settings(
        _settings(tmp_path), "Default", cli_executables=_shims(tmp_path, "git-1", "sln-1", "cli-1")
    )
    perfil = _leer(temporal)["Profiles"][0]
    assert perfil["UpdateLoadOrderAfterRun"] is False
    assert perfil["SplitIfMaxMastersExceeded"] is True
