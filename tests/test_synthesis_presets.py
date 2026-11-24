"""PR-3k (ADR 0019): versionado Git como upstream y ``SynthesisPreset`` (tags/ramas dinámicos → commit).

``run_capture`` se reemplaza por un falso de ``git ls-remote``: se fija la compuerta HITL única, el
aislamiento (perfil privado, sin config global), los argumentos exactos y el parseo (tag anotado →
``^{}``). El ``ls-remote`` real contra GitHub se verifica en el rig.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from sky_claw.app.security.hitl import Decision
from sky_claw.local.tools import synthesis_fase1 as f1
from sky_claw.local.tools import synthesis_fuentes_git as sg
from sky_claw.local.tools import synthesis_presets as sp
from tests.test_synthesis_fase1 import _GIT, _SHA, _Fakes, _perfil, _preparar

_SHA_B = "b" * 40
_SHA_C = "c" * 40


def _git_patcher(**campos: Any) -> dict[str, Any]:
    return {
        "$type": _GIT,
        "On": True,
        "ID": "g",
        "RemoteRepoPath": "https://github.com/Autor/Repo",
        "SelectedProjectSubpath": "Proy/Proy.csproj",
        **campos,
    }


# --- Versionado efectivo: copia de GitPatcherVersioning.Factory (0.36.6) -------------------------


@pytest.mark.parametrize(
    ("campos", "esperado"),
    [
        ({"PatcherVersioning": "Commit", "TargetCommit": _SHA}, ("commit", _SHA)),
        ({"PatcherVersioning": "Tag", "LatestTag": False, "TargetCommit": _SHA, "TargetTag": "v1"}, ("commit", _SHA)),
        ({"PatcherVersioning": "Tag", "TargetTag": "v1"}, ("tag", "v1")),  # LatestTag: default true
        ({"PatcherVersioning": "Branch", "TargetCommit": _SHA, "TargetBranch": "main"}, ("commit", _SHA)),
        ({"PatcherVersioning": "Branch", "AutoUpdateToBranchTip": True, "TargetBranch": "main"}, ("branch", "main")),
        ({"TargetCommit": _SHA}, ("commit", _SHA)),  # default: Branch sin AutoUpdateToBranchTip
        ({"PatcherVersioning": 0, "LatestTag": True, "TargetTag": "v2"}, ("tag", "v2")),
        ({"PatcherVersioning": 1, "AutoUpdateToBranchTip": True, "TargetBranch": "dev"}, ("branch", "dev")),
        ({"PatcherVersioning": 2, "TargetCommit": _SHA}, ("commit", _SHA)),
    ],
    ids=["commit", "tag_fijo", "tag_latest", "branch_fija", "branch_auto", "default", "enum0", "enum1", "enum2"],
)
def test_versionado_efectivo_copia_a_upstream(campos: dict[str, Any], esperado: tuple[str, str]) -> None:
    assert f1.versionado_efectivo(campos) == esperado


@pytest.mark.parametrize(
    "campos",
    [{"PatcherVersioning": "Otro"}, {"PatcherVersioning": 3}, {"PatcherVersioning": "Tag", "LatestTag": "si"}],
)
def test_versionado_desconocido_falla(campos: dict[str, Any]) -> None:
    with pytest.raises(f1.Fase1Error):
        f1.versionado_efectivo(campos)


async def test_branch_por_defecto_corre_su_target_commit_sin_preset(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrección de fidelidad: PR-3f rechazaba lo que upstream ejecuta fijado (la mayoría de los perfiles)."""
    settings = _perfil(tmp_path, [_git_patcher(PatcherVersioning="Branch", TargetBranch="main", TargetCommit=_SHA)])
    fakes = _Fakes(tmp_path)
    resultado = await _preparar(tmp_path, settings, fakes, monkeypatch)
    assert set(resultado.ejecutables) == {"g"}
    assert fakes.descargas == [("https://github.com/Autor/Repo", _SHA)]


async def test_tag_latest_sin_pin_falla_sin_red_y_con_pin_baja_ese_commit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _perfil(tmp_path, [_git_patcher(PatcherVersioning="Tag", TargetTag="v1.2")])
    fakes = _Fakes(tmp_path)
    with pytest.raises(f1.Fase1Error, match="SynthesisPreset"):
        await _preparar(tmp_path, settings, fakes, monkeypatch)
    assert fakes.descargas == []

    pin = {sg.ref_remoto("https://github.com/Autor/Repo", "tag", "v1.2"): _SHA_B}
    monkeypatch.setattr(f1, "obtener_fuentes_git", fakes.bajar)
    monkeypatch.setattr(f1, "compilar_patcher_local", fakes.compilar)
    git = tmp_path / "git.exe"
    resultado = await f1.preparar_fase1(
        settings,
        "Default",
        None,
        cache_raiz=tmp_path / "cache",
        directorio_shims=tmp_path / "corrida2" / "shims",
        dotnet=tmp_path / "dotnet" / "dotnet.exe",
        git=git,
        hitl=object(),  # type: ignore[arg-type]
        timeout_s=60,
        max_output_bytes=1_000_000,
        pins=pin,
    )
    assert set(resultado.ejecutables) == {"g"}
    assert fakes.descargas == [("https://github.com/Autor/Repo", _SHA_B)]


def test_el_pin_se_busca_por_repositorio_sin_importar_mayusculas_ni_dot_git() -> None:
    a = sg.ref_remoto("https://github.com/Autor/Repo.git", "branch", "main")
    b = sg.ref_remoto("https://github.com/autor/repo", "branch", "main")
    assert a == b


@pytest.mark.parametrize("nombre", ["", "-x", "a..b", "a b", "x.lock", "a//b", "fin/", "a@{1}", "x\\y"])
def test_nombres_de_ref_inadmisibles(nombre: str) -> None:
    with pytest.raises(sg.FuentesGitError):
        sg.ref_remoto("https://github.com/a/r", "tag", nombre)


# --- ls-remote: parseo, compuerta y aislamiento ------------------------------------------------


def test_un_tag_anotado_resuelve_al_commit_y_uno_liviano_a_si_mismo() -> None:
    tag = sg.ref_remoto("https://github.com/a/r", "tag", "v1")
    anotado = f"{_SHA_B}\trefs/tags/v1\n{_SHA_C}\trefs/tags/v1^{{}}\n".encode()
    assert sg._sha_de_ls_remote(anotado, tag) == _SHA_C
    assert sg._sha_de_ls_remote(f"{_SHA_B}\trefs/tags/v1\n".encode(), tag) == _SHA_B
    with pytest.raises(sg.FuentesGitError, match="no tiene"):
        sg._sha_de_ls_remote(f"{_SHA_B}\trefs/tags/v10\n".encode(), tag)


class _Hitl:
    def __init__(self, decision: Decision = Decision.APPROVED) -> None:
        self.decision = decision
        self.pedidos: list[dict[str, Any]] = []

    async def request_approval(self, **kw: Any) -> Decision:
        self.pedidos.append(kw)
        return self.decision


class _LsRemote:
    def __init__(self, respuestas: dict[str, str]) -> None:
        self.respuestas = respuestas
        self.llamadas: list[dict[str, Any]] = []

    async def __call__(self, args: list[str], **kw: Any) -> tuple[bytes, bytes, int]:
        self.llamadas.append({"args": args, **kw})
        url = args[args.index("--exit-code") + 1]
        refs = args[args.index("--exit-code") + 2 :]
        lineas = [f"{self.respuestas[f'{url} {r}']}\t{r}" for r in refs if f"{url} {r}" in self.respuestas]
        return ("\n".join(lineas) + "\n").encode(), b"", 0 if lineas else 2


def _git_exe(tmp_path: pathlib.Path) -> pathlib.Path:
    git = tmp_path / "git.exe"
    git.write_bytes(b"git")
    return git


async def test_resolver_pide_una_sola_aprobacion_y_corre_aislado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "perfil_real"))
    falso = _LsRemote(
        {
            "https://github.com/a/r refs/tags/v1": _SHA_B,
            "https://github.com/a/r refs/heads/main": _SHA_C,
        }
    )
    monkeypatch.setattr(sg, "run_capture", falso)
    hitl = _Hitl()
    refs = [
        sg.ref_remoto("https://github.com/a/r", "tag", "v1"),
        sg.ref_remoto("https://github.com/a/r", "branch", "main"),
    ]
    resueltos = await sg.resolver_refs_remotos(
        refs,
        trabajo_raiz=tmp_path / "trabajo",
        git=_git_exe(tmp_path),
        hitl=hitl,
        timeout_s=30,
        max_output_bytes=10_000,
    )
    assert resueltos == {refs[0]: _SHA_B, refs[1]: _SHA_C}
    assert len(hitl.pedidos) == 1
    detalle = hitl.pedidos[0]["detail"].splitlines()
    assert "a/r: tag v1" in detalle and "a/r: branch main" in detalle and "Hosts: github.com" in detalle
    for llamada in falso.llamadas:
        assert llamada["args"][1:15] == sg.CONFIG_GIT_SEGURA
        assert llamada["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert llamada["env"]["USERPROFILE"] != str(tmp_path / "perfil_real")
        assert llamada["require_job"] is True
    assert [ll["args"][-2:] for ll in falso.llamadas] == [
        ["https://github.com/a/r", "refs/heads/main"],
        ["refs/tags/v1", "refs/tags/v1^{}"],
    ]
    assert list((tmp_path / "trabajo").iterdir()) == []  # la carpeta de trabajo se limpia


async def test_sin_aprobacion_no_se_consulta_la_red(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    falso = _LsRemote({})
    monkeypatch.setattr(sg, "run_capture", falso)
    with pytest.raises(sg.FuentesGitError, match="no aprobó"):
        await sg.resolver_refs_remotos(
            [sg.ref_remoto("https://github.com/a/r", "tag", "v1")],
            trabajo_raiz=tmp_path,
            git=_git_exe(tmp_path),
            hitl=_Hitl(Decision.DENIED),
            timeout_s=30,
            max_output_bytes=10_000,
        )
    assert falso.llamadas == []


async def test_sin_refs_no_hay_pedido_ni_red(tmp_path: pathlib.Path) -> None:
    hitl = _Hitl()
    assert (
        await sg.resolver_refs_remotos(
            [], trabajo_raiz=tmp_path, git=tmp_path / "no.exe", hitl=hitl, timeout_s=1, max_output_bytes=1
        )
        == {}
    )
    assert hitl.pedidos == []


# --- SynthesisPreset: fijar, guardar y cargar ---------------------------------------------------


async def test_fijar_preset_junta_solo_los_dinamicos_de_la_seleccion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _perfil(
        tmp_path,
        [
            _git_patcher(ID="t", Nickname="T", PatcherVersioning="Tag", TargetTag="v1"),
            _git_patcher(
                ID="b", Nickname="B", PatcherVersioning="Branch", AutoUpdateToBranchTip=True, TargetBranch="main"
            ),
            _git_patcher(ID="c", Nickname="C", PatcherVersioning="Commit", TargetCommit=_SHA),
            _git_patcher(ID="x", Nickname="X", PatcherVersioning="Tag", TargetTag="v9"),
        ],
    )
    falso = _LsRemote(
        {
            "https://github.com/autor/repo refs/tags/v1": _SHA_B,
            "https://github.com/autor/repo refs/heads/main": _SHA_C,
        }
    )
    monkeypatch.setattr(sg, "run_capture", falso)
    preset = await sp.fijar_preset(
        settings,
        "Default",
        ["T", "B", "C"],
        git=_git_exe(tmp_path),
        hitl=_Hitl(),
        trabajo_raiz=tmp_path,
        timeout_s=30,
        max_output_bytes=10_000,
    )
    assert preset.perfil == "Default"
    assert {(r.tipo, r.nombre): sha for r, sha in preset.pins.items()} == {
        ("tag", "v1"): _SHA_B,
        ("branch", "main"): _SHA_C,
    }

    destino = tmp_path / "presets" / "Default.json"
    sp.guardar_preset(destino, preset)
    assert sp.cargar_preset(destino) == preset
    assert [p.name for p in destino.parent.iterdir()] == ["Default.json"]  # sin temporales


def test_las_claves_del_preset_estan_congeladas() -> None:
    assert frozenset({"esquema", "perfil", "pins"}) == sp.CLAVES_DE_PRESET
    assert frozenset({"repositorio", "tipo", "nombre", "commit"}) == sp.CLAVES_DE_PIN


@pytest.mark.parametrize(
    "contenido",
    [
        {"esquema": 1, "perfil": "D", "pins": [], "extra": 1},
        {"esquema": 2, "perfil": "D", "pins": []},
        {"esquema": 1, "perfil": "", "pins": []},
        {"esquema": 1, "perfil": "D", "pins": [{"repositorio": "a/r", "tipo": "tag", "nombre": "v1", "commit": "abc"}]},
        {"esquema": 1, "perfil": "D", "pins": [{"repositorio": "a/r", "tipo": "rama", "nombre": "v1", "commit": _SHA}]},
        {"esquema": 1, "perfil": "D", "pins": [{"repositorio": "A/R", "tipo": "tag", "nombre": "v1", "commit": _SHA}]},
        {
            "esquema": 1,
            "perfil": "D",
            "pins": [{"repositorio": "a/r", "tipo": "tag", "nombre": "v1", "commit": _SHA, "x": "y"}],
        },
        {
            "esquema": 1,
            "perfil": "D",
            "pins": [
                {"repositorio": "a/r", "tipo": "tag", "nombre": "v1", "commit": _SHA},
                {"repositorio": "a/r", "tipo": "tag", "nombre": "v1", "commit": _SHA_B},
            ],
        },
    ],
    ids=["clave_extra", "esquema", "perfil_vacio", "sha_corto", "tipo", "repo_no_canonico", "clave_pin", "duplicado"],
)
def test_cargar_un_preset_invalido_falla_cerrado(tmp_path: pathlib.Path, contenido: dict[str, Any]) -> None:
    ruta = tmp_path / "p.json"
    ruta.write_text(json.dumps(contenido), encoding="utf-8")
    with pytest.raises(sp.PresetError):
        sp.cargar_preset(ruta)


async def test_el_preparador_le_pasa_los_pins_a_la_fase1(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capturado: dict[str, Any] = {}

    async def _falso(*_a: Any, **kw: Any) -> f1.ResultadoFase1:
        capturado.update(kw)
        exe = kw["directorio_shims"] / "00-g" / "SkyClawSynthesisShim.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        return f1.ResultadoFase1(ejecutables={"g": exe})

    monkeypatch.setattr(f1, "preparar_fase1", _falso)
    (tmp_path / "raiz").mkdir()
    pins = {sg.ref_remoto("https://github.com/a/r", "tag", "v1"): _SHA_B}
    prep = f1.PreparadorFase1(
        dotnet=tmp_path / "d.exe",
        git=tmp_path / "g.exe",
        cache_raiz=tmp_path / "c",
        hitl=object(),  # type: ignore[arg-type]
        raiz_shims=tmp_path / "raiz",
        pins=pins,
    )
    await prep.preparar(tmp_path / "s.json", "Default", ["g"])
    assert capturado["pins"] == pins
