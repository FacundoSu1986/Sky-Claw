"""PR-3f (ADR 0019 §2.1): orquestador de la Fase 1 — de un perfil V2 al mapa ``ID → shim``.

Une 3a (decisión CET), 3b (shim), 3d (build) y 3e (fuentes Git). Los dos pasos con red/procesos
(``compilar_patcher_local`` y ``obtener_fuentes_git``) se reemplazan por falsos que registran con qué
se los llamó y devuelven apphosts sintéticos; la decisión CET y la materialización del shim corren de
verdad sobre esos binarios.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from sky_claw.local.tools import synthesis_fase1 as f1
from sky_claw.local.tools import synthesis_shim as ss
from sky_claw.local.tools.synthesis_compilacion import PatcherCompilado
from sky_claw.local.tools.synthesis_fuentes_git import FuentesGit
from tests._pe_sintetico import patcher_sintetico, pe_sintetico

_GIT = "Synthesis.Bethesda.Execution.Settings.GithubPatcherSettings, Synthesis.Bethesda.Execution"
_SLN = "Synthesis.Bethesda.Execution.Settings.SolutionPatcherSettings, Synthesis.Bethesda.Execution"
_CLI = "Synthesis.Bethesda.Execution.Settings.CliPatcherSettings, Synthesis.Bethesda.Execution"
_SHA = "0123456789abcdef0123456789abcdef01234567"


class _Fakes:
    """Reemplaza build y descarga; cada patcher compilado es un apphost sintético SIN CET."""

    def __init__(self, tmp_path: pathlib.Path, *, cet_del_patcher: int = 0) -> None:
        self.tmp = tmp_path
        self.compilaciones: list[tuple[pathlib.Path, str]] = []
        self.descargas: list[tuple[str, str]] = []
        self.cet = cet_del_patcher

    async def compilar(self, raiz: pathlib.Path, proyecto: str, **kwargs: Any) -> PatcherCompilado:
        self.compilaciones.append((raiz, proyecto))
        nombre = pathlib.PurePosixPath(proyecto).stem
        salida = self.tmp / "builds" / f"{len(self.compilaciones)}-{nombre}"
        cet = 0 if nombre == "SkyClawSynthesisShim" else self.cet
        exe = patcher_sintetico(salida, nombre=nombre, pe=pe_sintetico(cet=cet, apps=(f"{nombre}.dll".encode(),)))
        return PatcherCompilado(exe=exe, salida_sha256="x" * 64, cache_hit=False)

    async def bajar(self, url: str, sha: str, **kwargs: Any) -> FuentesGit:
        self.descargas.append((url, sha))
        arbol = self.tmp / "git" / "arbol"
        (arbol / "Proy" / "InternalData").mkdir(parents=True, exist_ok=True)
        (arbol / "Proy" / "Proy.csproj").write_text("<Project/>", encoding="utf-8")
        return FuentesGit(raiz=arbol, arbol_sha256="y" * 64, cache_hit=False)


def _perfil(tmp_path: pathlib.Path, patchers: list[dict[str, Any]]) -> pathlib.Path:
    perfil = {"ID": "p", "Nickname": "Default", "Groups": [{"Name": "Synthesis", "On": True, "Patchers": patchers}]}
    ruta = tmp_path / "PipelineSettings.json"
    ruta.write_text(json.dumps({"Version": 2, "Profiles": [perfil]}), encoding="utf-8")
    return ruta


def _solucion(tmp_path: pathlib.Path, *, data: bool, internal: bool) -> pathlib.Path:
    raiz = tmp_path / "usuario" / "Sol"
    (raiz / "Proy").mkdir(parents=True)
    (raiz / "Proy" / "Proy.csproj").write_text("<Project/>", encoding="utf-8")
    if data:
        (raiz / "Proy" / "Data").mkdir()
    if internal:
        (raiz / "Proy" / "InternalData").mkdir()
    sln = raiz / "Sol.sln"
    sln.write_text("", encoding="utf-8")
    return sln


def _dotnet(tmp_path: pathlib.Path) -> pathlib.Path:
    exe = tmp_path / "dotnet" / "dotnet.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(pe_sintetico())
    return exe


async def _preparar(
    tmp_path: pathlib.Path, settings: pathlib.Path, fakes: _Fakes, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> f1.ResultadoFase1:
    monkeypatch.setattr(f1, "compilar_patcher_local", fakes.compilar)
    monkeypatch.setattr(f1, "obtener_fuentes_git", fakes.bajar)
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    return await f1.preparar_fase1(
        settings,
        "Default",
        kw.pop("patcher_ids", None),
        cache_raiz=tmp_path / "cache",
        directorio_shims=tmp_path / "corrida" / "shims",
        dotnet=_dotnet(tmp_path),
        git=git,
        hitl=object(),  # type: ignore[arg-type]
        timeout_s=60,
        max_output_bytes=1_000_000,
    )


async def test_los_tres_tipos_terminan_en_un_shim_cada_uno(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_exe = patcher_sintetico(tmp_path / "cli", nombre="CliP", pe=pe_sintetico(cet=0, apps=(b"CliP.dll",)))
    sln = _solucion(tmp_path, data=True, internal=True)
    settings = _perfil(
        tmp_path,
        [
            {
                "$type": _GIT,
                "On": True,
                "ID": "g",
                "RemoteRepoPath": "https://github.com/a/R",
                "SelectedProjectSubpath": "Proy/Proy.csproj",
                "PatcherVersioning": "Commit",
                "TargetCommit": _SHA,
            },
            {
                "$type": _SLN,
                "On": True,
                "ID": "s",
                "Nickname": "Sol",
                "SolutionPath": str(sln),
                "ProjectSubpath": "Proy/Proy.csproj",
            },
            {"$type": _CLI, "On": True, "ID": "c", "Nickname": "Cli", "PathToExecutable": str(cli_exe)},
        ],
    )
    fakes = _Fakes(tmp_path)
    resultado = await _preparar(tmp_path, settings, fakes, monkeypatch)

    assert set(resultado.ejecutables) == {"g", "s", "c"}
    assert fakes.descargas == [("https://github.com/a/R", _SHA)]
    # Primero cada patcher (un rechazo corta antes de compilar nada más) y al final, UNA vez, el shim
    # desde las fuentes del paquete.
    assert [p for _, p in fakes.compilaciones] == [
        "Proy/Proy.csproj",
        "Proy/Proy.csproj",
        "SkyClawSynthesisShim.csproj",
    ]
    assert fakes.compilaciones[1][0] == sln.parent
    assert fakes.compilaciones[2][0] == ss.directorio_fuentes_shim()
    for shim in resultado.ejecutables.values():
        assert shim.name == ss.NOMBRE_EJECUTABLE_SHIM and shim.parent.parent == tmp_path / "corrida" / "shims"
    cfg = {i: json.loads((resultado.ejecutables[i].parent / "shim.json").read_text(encoding="utf-8")) for i in "gsc"}
    # Carpetas de datos como upstream: junto al .csproj y solo si existen; CLI no lleva ninguna.
    assert (cfg["s"]["internalDataFolder"], cfg["s"]["defaultDataFolderPath"]) == (
        str(sln.parent / "Proy" / "InternalData"),
        str(sln.parent / "Proy" / "Data"),
    )
    assert cfg["g"]["internalDataFolder"] == str(tmp_path / "git" / "arbol" / "Proy" / "InternalData")
    assert cfg["g"]["defaultDataFolderPath"] is None
    assert (cfg["c"]["internalDataFolder"], cfg["c"]["defaultDataFolderPath"]) == (None, None)
    assert cfg["c"]["patcher"] == str(cli_exe)


async def test_patcher_con_cet_sale_via_dotnet(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sln = _solucion(tmp_path, data=False, internal=False)
    settings = _perfil(
        tmp_path,
        [{"$type": _SLN, "On": True, "ID": "s", "SolutionPath": str(sln), "ProjectSubpath": "Proy/Proy.csproj"}],
    )
    resultado = await _preparar(tmp_path, settings, _Fakes(tmp_path, cet_del_patcher=1), monkeypatch)
    cfg = json.loads((resultado.ejecutables["s"].parent / "shim.json").read_text(encoding="utf-8"))
    assert cfg["modo"] == "via_dotnet" and cfg["dotnet"] == str(tmp_path / "dotnet" / "dotnet.exe")


@pytest.mark.parametrize("versionado", ["Commit", "commit", 2])
async def test_git_por_commit_se_acepta(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, versionado: object
) -> None:
    settings = _perfil(
        tmp_path,
        [
            {
                "$type": _GIT,
                "On": True,
                "ID": "g",
                "RemoteRepoPath": "https://github.com/a/R",
                "SelectedProjectSubpath": "Proy/Proy.csproj",
                "PatcherVersioning": versionado,
                "TargetCommit": _SHA,
            }
        ],
    )
    resultado = await _preparar(tmp_path, settings, _Fakes(tmp_path), monkeypatch)
    assert set(resultado.ejecutables) == {"g"}


@pytest.mark.parametrize(
    "campos",
    [
        {"PatcherVersioning": "Tag", "TargetTag": "v1"},
        {"PatcherVersioning": "Branch"},
        {},  # default upstream: Branch
        {"PatcherVersioning": "Commit", "TargetCommit": "0123456"},
        {"PatcherVersioning": "Commit"},
        {"PatcherVersioning": 0, "TargetCommit": _SHA},
    ],
    ids=["tag", "branch", "default", "sha_corto", "sin_commit", "enum_tag"],
)
async def test_git_sin_commit_fijado_falla_sin_red(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, campos: dict[str, object]
) -> None:
    settings = _perfil(
        tmp_path,
        [
            {
                "$type": _GIT,
                "On": True,
                "ID": "g",
                "RemoteRepoPath": "https://github.com/a/R",
                "SelectedProjectSubpath": "Proy/Proy.csproj",
                **campos,
            }
        ],
    )
    fakes = _Fakes(tmp_path)
    # PR-3k: cada caso falla por su motivo real según upstream (commit ausente/corto, o un tag/rama
    # dinámico sin nombre o sin pin del preset); ninguno abre red.
    with pytest.raises(f1.Fase1Error, match="commit|TargetTag"):
        await _preparar(tmp_path, settings, fakes, monkeypatch)
    assert fakes.descargas == []


async def test_cli_con_ruta_relativa_falla(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _perfil(tmp_path, [{"$type": _CLI, "On": True, "ID": "c", "PathToExecutable": "relativo/p.exe"}])
    with pytest.raises(f1.Fase1Error, match="'c'"):
        await _preparar(tmp_path, settings, _Fakes(tmp_path), monkeypatch)


async def test_patcher_rechazado_por_cet_nombra_al_patcher(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    single = patcher_sintetico(
        tmp_path / "sf", nombre="SF", pe=pe_sintetico(cet=1, offsets_bundle=(99,), apps=(b"SF.dll",))
    )
    settings = _perfil(tmp_path, [{"$type": _CLI, "On": True, "ID": "c", "PathToExecutable": str(single)}])
    with pytest.raises(f1.Fase1Error, match="'c'.*single-file"):
        await _preparar(tmp_path, settings, _Fakes(tmp_path), monkeypatch)


async def test_respeta_la_seleccion(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli_exe = patcher_sintetico(tmp_path / "cli", nombre="CliP", pe=pe_sintetico(cet=0, apps=(b"CliP.dll",)))
    settings = _perfil(
        tmp_path,
        [
            {"$type": _CLI, "On": True, "ID": "c", "Nickname": "Cli", "PathToExecutable": str(cli_exe)},
            {
                "$type": _GIT,
                "On": True,
                "ID": "g",
                "RemoteRepoPath": "https://github.com/a/R",
                "SelectedProjectSubpath": "Proy/Proy.csproj",
                "PatcherVersioning": "Tag",
            },
        ],
    )
    fakes = _Fakes(tmp_path)
    resultado = await _preparar(tmp_path, settings, fakes, monkeypatch, patcher_ids=["Cli"])
    assert set(resultado.ejecutables) == {"c"} and fakes.descargas == []


async def test_directorio_de_shims_existente_falla(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "corrida" / "shims").mkdir(parents=True)
    cli_exe = patcher_sintetico(tmp_path / "cli", nombre="CliP", pe=pe_sintetico(cet=0, apps=(b"CliP.dll",)))
    settings = _perfil(tmp_path, [{"$type": _CLI, "On": True, "ID": "c", "PathToExecutable": str(cli_exe)}])
    with pytest.raises(f1.Fase1Error, match="existe"):
        await _preparar(tmp_path, settings, _Fakes(tmp_path), monkeypatch)


def test_cada_tipo_de_patcher_tiene_su_preparacion() -> None:
    # Ancla enumerante: un tipo nuevo admitido por el lector sin rama en el orquestador rompe acá.
    from sky_claw.local.tools.patcher_pipeline import TIPO_POR_PATCHER_TYPE

    assert set(f1.PREPARADORES) == set(TIPO_POR_PATCHER_TYPE.values())


# --- Cableado: SynthesisConfig.cli_executables → traductor (standalone) y transporte en brokered ------

import dataclasses  # noqa: E402

from sky_claw.local.mo2.brokered_synthesis import BrokeredSynthesisRunner  # noqa: E402
from sky_claw.local.tools import synthesis_runner as sr  # noqa: E402

#: Campos de SynthesisConfig, congelados por igualdad. Un campo nuevo rompe esto hasta decidir si viaja
#: al worker brokered (`BrokeredSynthesisRunner._build_job_payload`): si no viaja, se pierde en silencio
#: bajo USVFS, que es justo donde importa.
_CAMPOS_DE_SYNTHESIS_CONFIG = [
    "game_path",
    "mo2_path",
    "output_path",
    "synthesis_exe",
    "timeout_seconds",
    "profile",
    "pipeline_settings_path",
    "data_folder_path",
    "load_order_file_path",
    "mo2_profile",
    "extra_data_folder",
    "working_dir",
    "temp_dir",
    "dotnet_root",
    "persistence_mode",
    "persistence_path",
    "target_runtime",
    "block_building_within_mo2",
    "shortcircuit",
    "max_output_bytes",
    "require_patcher_receipts",
    # PR-3f/PR-3g: viaja al worker como payload.cli_executables y el worker lo valida.
    "cli_executables",
]


def test_campos_de_synthesis_config_congelados() -> None:
    assert [c.name for c in dataclasses.fields(sr.SynthesisConfig)] == _CAMPOS_DE_SYNTHESIS_CONFIG


def _config(tmp_path: pathlib.Path, **kw: Any) -> sr.SynthesisConfig:
    for nombre in ("game/Data", "mo2", "out", "bin"):
        (tmp_path / nombre).mkdir(parents=True, exist_ok=True)
    exe = tmp_path / "bin" / "Synthesis.Bethesda.CLI.exe"
    exe.write_bytes(b"x")
    settings = _perfil(tmp_path, [{"$type": _CLI, "On": True, "ID": "c", "PathToExecutable": str(exe)}])
    plugins = tmp_path / "mo2" / "plugins.txt"
    plugins.write_text("*Skyrim.esm" + chr(10), encoding="utf-8")
    kw.setdefault("load_order_file_path", plugins)
    return sr.SynthesisConfig(
        game_path=tmp_path / "game",
        mo2_path=tmp_path / "mo2",
        output_path=tmp_path / "out",
        synthesis_exe=exe,
        pipeline_settings_path=settings,
        **kw,
    )


async def test_el_runner_le_pasa_el_mapa_al_traductor(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mapa = {"c": tmp_path / "shims" / "00-c" / "SkyClawSynthesisShim.exe"}
    capturado: dict[str, Any] = {}

    def _espia(*args: Any, **kwargs: Any) -> Any:
        capturado.update(kwargs)
        raise sr.PatcherPipelineError("corte del espía")

    monkeypatch.setattr(sr.PatcherPipeline, "prepare_synthesis_cli_settings", _espia)
    monkeypatch.setattr(sr.SynthesisRunner, "settings_fingerprint", lambda self: "fp")
    runner = sr.SynthesisRunner(_config(tmp_path, cli_executables=mapa))
    with pytest.raises(sr.SynthesisExecutionError, match="corte del espía"):
        await runner.run_pipeline(["c"])
    assert capturado["cli_executables"] == mapa


def test_el_runner_brokered_transporta_el_mapa_en_vez_de_perderlo(tmp_path: pathlib.Path) -> None:
    # PR-3g: el mapa viaja en el payload (la validación del worker vive en test_brokered_synthesis.py).
    shim = tmp_path / "shims" / "00-c" / "SkyClawSynthesisShim.exe"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"MZ")
    cfg = _config(
        tmp_path,
        cli_executables={"c": shim},
        block_building_within_mo2=True,
        require_patcher_receipts=True,
    )
    proxy = BrokeredSynthesisRunner(
        config=cfg,
        broker=object(),
        instance_id="i",
        data_root=tmp_path / "mo2",
        mods_dir=tmp_path / "mo2" / "mods",  # type: ignore[arg-type]
    )
    payload = proxy._build_job_payload(
        patcher_ids=["c"], expected_plugins=["Skyrim.esm"], settings_fingerprint="a", load_order_fingerprint="b"
    )
    assert payload["cli_executables"] == {"c": str(shim)}


# --- PR-3h: el disco pesado de la Fase 1 no corre en el event loop del servicio ---------------------

import ast  # noqa: E402

#: Funciones síncronas de disco pesado (hash/copia de árboles, verificación de caché, publicación,
#: limpieza, lectura del perfil). Dentro de un ``async def`` de la Fase 1 solo se admiten como
#: argumento de ``asyncio.to_thread``; una llamada directa traba el loop del servicio (y la GUI).
_BLOQUEANTES_DE_FASE1 = frozenset(
    {
        "hash_arbol",
        "copytree",
        "materializar_shim",
        "seleccionar_patchers_v2",
        "_verificar_salida",
        "_verificar_entrada",
        "_limpiar_trabajo",
        "_identidad_toolchain",
        "_carpetas_de_datos",
        "_sellar_y_publicar",
        "_guardar_y_publicar",
        "exigir_arbol_copiable_sin_reparse",
        "construir_config_shim",
    }
)
_MODULOS_DE_FASE1 = (
    "sky_claw/local/tools/synthesis_compilacion.py",
    "sky_claw/local/tools/synthesis_fuentes_git.py",
    "sky_claw/local/tools/synthesis_fase1.py",
)


def _llamadas_bloqueantes_en_async(ruta: pathlib.Path) -> list[str]:
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    hallazgos: list[str] = []
    for funcion in ast.walk(arbol):
        if not isinstance(funcion, ast.AsyncFunctionDef):
            continue
        pendientes: list[ast.AST] = list(funcion.body)
        while pendientes:
            nodo = pendientes.pop()
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # un helper anidado se lanza con to_thread; su cuerpo no corre en el loop
            if isinstance(nodo, ast.Call):
                destino = nodo.func
                nombre = destino.attr if isinstance(destino, ast.Attribute) else getattr(destino, "id", "")
                if nombre in _BLOQUEANTES_DE_FASE1:
                    hallazgos.append(f"{ruta.name}:{nodo.lineno} {funcion.name} -> {nombre}")
            pendientes.extend(ast.iter_child_nodes(nodo))
    return hallazgos


def test_la_fase1_no_hace_disco_pesado_en_el_event_loop() -> None:
    raiz = pathlib.Path(__file__).resolve().parents[1]
    hallazgos = [h for modulo in _MODULOS_DE_FASE1 for h in _llamadas_bloqueantes_en_async(raiz / modulo)]
    assert hallazgos == []


def test_el_ancla_de_disco_pesado_detecta_una_llamada_directa(tmp_path: pathlib.Path) -> None:
    falso = tmp_path / "falso.py"
    falso.write_text(
        "import asyncio\n"
        "async def mala(p):\n    return hash_arbol(p)\n"
        "async def buena(p):\n    return await asyncio.to_thread(hash_arbol, p)\n",
        encoding="utf-8",
    )
    assert _llamadas_bloqueantes_en_async(falso) == ["falso.py:3 mala -> hash_arbol"]
