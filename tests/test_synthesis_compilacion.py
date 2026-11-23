"""PR-3d (ADR 0019 §2.1): build de patchers locales en la Fase 1, con caché y compuerta HITL.

``run_capture`` se reemplaza por un falso que registra cada llamada y, en el build, escribe un
apphost sintético en la carpeta ``-o``: así se fija el ORDEN (HITL → restore → build → verificación
CET), el entorno (la lista blanca única) y la caché sin un SDK .NET. La compilación real con
``dotnet`` se verifica en el rig.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from sky_claw.app.security.hitl import Decision
from sky_claw.local.tools import synthesis_compilacion as sc
from sky_claw.local.tools.synthesis_prebuild import (
    argumentos_dotnet_build_patcher,
    argumentos_dotnet_restore_patcher,
)
from tests._pe_sintetico import RUNTIMECONFIG_FD, pe_sintetico


class _Hitl:
    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        self.pedidos: list[dict[str, Any]] = []

    async def request_approval(self, **kwargs: Any) -> Decision:
        self.pedidos.append(kwargs)
        return self.decision


class _Dotnet:
    """Falso de ``run_capture``: registra argv/env/cwd y simula restore y build."""

    def __init__(self, *, cet_en_salida: int = 0, rc_restore: int = 0, rc_build: int = 0) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.cet_en_salida = cet_en_salida
        self.rc = {"restore": rc_restore, "build": rc_build}

    async def __call__(self, args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        self.llamadas.append({"args": args, **kwargs})
        verbo = args[1]
        if verbo == "build" and self.rc["build"] == 0:
            salida = pathlib.Path(args[args.index("-o") + 1])
            salida.mkdir(parents=True, exist_ok=True)
            nombre = pathlib.Path(args[2]).stem
            (salida / f"{nombre}.exe").write_bytes(
                pe_sintetico(cet=self.cet_en_salida, apps=(f"{nombre}.dll".encode(),))
            )
            (salida / f"{nombre}.dll").write_bytes(b"il")
            (salida / f"{nombre}.runtimeconfig.json").write_text(json.dumps(RUNTIMECONFIG_FD), encoding="utf-8")
        return b"", f"{verbo} fallo simulado".encode() if self.rc[verbo] else b"", self.rc[verbo]


def _fuentes(tmp_path: pathlib.Path) -> pathlib.Path:
    raiz = tmp_path / "usuario" / "MiSolucion"
    (raiz / "MiPatcher").mkdir(parents=True)
    (raiz / "MiPatcher" / "MiPatcher.csproj").write_text("<Project Sdk='Microsoft.NET.Sdk'/>", encoding="utf-8")
    (raiz / "MiPatcher" / "Program.cs").write_text("// patcher", encoding="utf-8")
    (raiz / "Comun").mkdir()
    (raiz / "Comun" / "Comun.cs").write_text("// referenciado por ProjectReference", encoding="utf-8")
    return raiz


def _dotnet_exe(tmp_path: pathlib.Path) -> pathlib.Path:
    exe = tmp_path / "dotnet" / "dotnet.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(pe_sintetico())
    return exe


async def _compilar(
    tmp_path: pathlib.Path, hitl: object, dotnet: _Dotnet, monkeypatch: pytest.MonkeyPatch
) -> sc.PatcherCompilado:
    monkeypatch.setattr(sc, "run_capture", dotnet)
    return await sc.compilar_patcher_local(
        _fuentes(tmp_path) if not (tmp_path / "usuario").exists() else tmp_path / "usuario" / "MiSolucion",
        "MiPatcher/MiPatcher.csproj",
        cache_raiz=tmp_path / "cache",
        dotnet=_dotnet_exe(tmp_path) if not (tmp_path / "dotnet").exists() else tmp_path / "dotnet" / "dotnet.exe",
        hitl=hitl,  # type: ignore[arg-type]
        timeout_s=60,
        max_output_bytes=1_000_000,
    )


async def test_cache_miss_pide_hitl_y_compila_en_orden(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hitl, dotnet = _Hitl(Decision.APPROVED), _Dotnet()
    resultado = await _compilar(tmp_path, hitl, dotnet, monkeypatch)

    assert resultado.cache_hit is False
    assert len(hitl.pedidos) == 1
    # Igualdad de la línea completa (no una subcadena): el pedido nombra exactamente el host.
    lineas_host = [linea for linea in hitl.pedidos[0]["detail"].splitlines() if linea.startswith("Hosts:")]
    assert lineas_host == ["Hosts: api.nuget.org"]
    # El build corre en una carpeta de trabajo de la caché y después se publica con os.replace.
    proyecto = pathlib.Path(dotnet.llamadas[0]["args"][2])
    trabajo = proyecto.parents[2]
    assert trabajo.parent == tmp_path / "cache" and trabajo.name.startswith(".trabajo-")
    assert proyecto.relative_to(trabajo).as_posix() == "fuentes/MiPatcher/MiPatcher.csproj"
    assert [ll["args"][1:] for ll in dotnet.llamadas] == [
        argumentos_dotnet_restore_patcher(proyecto),
        argumentos_dotnet_build_patcher(proyecto, salida=trabajo / "salida"),
    ]
    assert not trabajo.exists()  # publicado
    assert resultado.exe.parents[1].parent == tmp_path / "cache"
    assert all(ll["require_job"] is True and ll["max_output_bytes"] == 1_000_000 for ll in dotnet.llamadas)
    assert resultado.exe.name == "MiPatcher.exe"
    assert resultado.exe.is_file()


async def test_compila_sobre_una_copia_y_no_toca_las_fuentes_del_usuario(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raiz = _fuentes(tmp_path)
    antes = sorted(p.relative_to(raiz).as_posix() for p in raiz.rglob("*"))
    resultado = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    assert sorted(p.relative_to(raiz).as_posix() for p in raiz.rglob("*")) == antes
    # La copia incluye los proyectos hermanos (ProjectReference ../Comun).
    assert (resultado.exe.parents[1] / "fuentes" / "Comun" / "Comun.cs").is_file()


async def test_el_entorno_es_la_lista_blanca_unica(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKYCLAW_TOKEN_SECRETO", "no-debe-pasar")
    monkeypatch.setenv("PROGRAMFILES", "C:\\Program Files")
    dotnet = _Dotnet()
    await _compilar(tmp_path, _Hitl(Decision.APPROVED), dotnet, monkeypatch)
    for llamada in dotnet.llamadas:
        claves = {k.upper() for k in llamada["env"]}
        assert "SKYCLAW_TOKEN_SECRETO" not in claves
        assert "PROGRAMFILES" in claves  # NuGet la necesita (rig PR-3b)
        assert llamada["env"]["DOTNET_ROOT"] == str(tmp_path / "dotnet")
        assert "NUGET_PACKAGES" in llamada["env"]


@pytest.mark.parametrize("decision", [Decision.DENIED, Decision.TIMEOUT])
async def test_sin_aprobacion_no_se_abre_red(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, decision: Decision
) -> None:
    dotnet = _Dotnet()
    with pytest.raises(sc.CompilacionPatcherError, match="egress"):
        await _compilar(tmp_path, _Hitl(decision), dotnet, monkeypatch)
    assert dotnet.llamadas == []


async def test_sin_hitl_no_se_abre_red(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotnet = _Dotnet()
    with pytest.raises(sc.CompilacionPatcherError, match="egress"):
        await _compilar(tmp_path, None, dotnet, monkeypatch)
    assert dotnet.llamadas == []


async def test_cache_hit_no_pide_hitl_ni_lanza_dotnet(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primero = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    hitl, dotnet = _Hitl(Decision.DENIED), _Dotnet()
    segundo = await _compilar(tmp_path, hitl, dotnet, monkeypatch)
    assert segundo.cache_hit is True
    assert segundo.exe == primero.exe
    assert hitl.pedidos == [] and dotnet.llamadas == []


async def test_cambiar_las_fuentes_invalida_la_cache(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primero = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    (tmp_path / "usuario" / "MiSolucion" / "MiPatcher" / "Program.cs").write_text("// v2", encoding="utf-8")
    segundo = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    assert segundo.cache_hit is False
    assert segundo.exe != primero.exe


async def test_salida_en_cache_alterada_falla_cerrado(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primero = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    primero.exe.with_suffix(".dll").write_bytes(b"il alterado")
    dotnet = _Dotnet()
    with pytest.raises(sc.CompilacionPatcherError, match="caché"):
        await _compilar(tmp_path, _Hitl(Decision.APPROVED), dotnet, monkeypatch)
    assert dotnet.llamadas == []  # no recompila encima: lo alterado se investiga


async def test_apphost_compilado_con_cet_falla(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(sc.CompilacionPatcherError, match="CETCompat"):
        await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(cet_en_salida=1), monkeypatch)
    assert not list((tmp_path / "cache").glob("*/marca.json"))  # nada publicado


@pytest.mark.parametrize(("rc_restore", "rc_build", "verbo"), [(1, 0, "restore"), (0, 1, "build")])
async def test_fallo_de_dotnet_falla_con_su_salida(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, rc_restore: int, rc_build: int, verbo: str
) -> None:
    with pytest.raises(sc.CompilacionPatcherError, match=f"{verbo} fallo simulado"):
        await _compilar(
            tmp_path, _Hitl(Decision.APPROVED), _Dotnet(rc_restore=rc_restore, rc_build=rc_build), monkeypatch
        )
    assert not list((tmp_path / "cache").glob("*/marca.json"))


@pytest.mark.parametrize(
    "proyecto", ["../Afuera/X.csproj", "C:/abs/X.csproj", "MiPatcher/NoExiste.csproj", "MiPatcher/Program.cs"]
)
async def test_proyecto_invalido_falla(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, proyecto: str) -> None:
    monkeypatch.setattr(sc, "run_capture", _Dotnet())
    with pytest.raises(sc.CompilacionPatcherError):
        await sc.compilar_patcher_local(
            _fuentes(tmp_path),
            proyecto,
            cache_raiz=tmp_path / "cache",
            dotnet=_dotnet_exe(tmp_path),
            hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )


async def test_dotnet_x86_se_rechaza(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotnet = tmp_path / "x86" / "dotnet.exe"
    dotnet.parent.mkdir()
    dotnet.write_bytes(pe_sintetico(maquina=0x14C))
    monkeypatch.setattr(sc, "run_capture", _Dotnet())
    with pytest.raises(sc.CompilacionPatcherError, match="x64"):
        await sc.compilar_patcher_local(
            _fuentes(tmp_path),
            "MiPatcher/MiPatcher.csproj",
            cache_raiz=tmp_path / "cache",
            dotnet=dotnet,
            hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )
