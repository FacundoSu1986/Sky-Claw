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
    FUENTE_NUGET_APROBABLE,
    argumentos_dotnet_assembly_name,
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

    def __init__(
        self, *, cet_en_salida: int = 0, rc_restore: int = 0, rc_build: int = 0, assembly_name: str = "MiPatcher"
    ) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.cet_en_salida = cet_en_salida
        self.assembly_name = assembly_name
        self.rc = {"restore": rc_restore, "build": rc_build, "msbuild": 0}

    async def __call__(self, args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        self.llamadas.append({"args": args, **kwargs})
        verbo = args[1]
        if verbo == "msbuild":
            # MSBuild imprime el valor con un salto de linea de Windows al final.
            return (self.assembly_name + chr(13) + chr(10)).encode(), b"", 0
        if verbo == "build" and self.rc["build"] == 0:
            salida = pathlib.Path(args[args.index("-o") + 1])
            salida.mkdir(parents=True, exist_ok=True)
            # Un AssemblyName inválido no puede ser un nombre de archivo: el build "real" deja uno
            # normal y la que decide es la validación del motor, no el sistema de archivos.
            nombre = self.assembly_name if self.assembly_name.isidentifier() else "MiPatcher"
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
        argumentos_dotnet_restore_patcher(proyecto, configfile=trabajo / "nuget.config"),
        argumentos_dotnet_build_patcher(proyecto, salida=trabajo / "publicar" / "salida"),
        argumentos_dotnet_assembly_name(proyecto),
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
    # La copia (que se compila y después se descarta) incluía los proyectos hermanos: la clave de
    # caché exige que su hash coincida con el de la solución entera, ../Comun incluido.
    assert resultado.exe.is_file()


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


async def test_el_nuget_config_del_usuario_no_agrega_hosts(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hallazgo Codex P1: sin --configfile, restore usaría el NuGet.Config copiado con la solución y
    # podría contactar hosts que el operador no aprobó.
    raiz = _fuentes(tmp_path)
    (raiz / "NuGet.Config").write_text(
        "<configuration><packageSources><add key='otro' value='https://evil.example/v3/index.json'/>"
        "</packageSources></configuration>",
        encoding="utf-8",
    )
    capturado: dict[str, str] = {}
    dotnet = _Dotnet()

    async def _capturar(args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        if args[1] == "restore":
            capturado["config"] = pathlib.Path(args[args.index("--configfile") + 1]).read_text(encoding="utf-8")
        return await dotnet(args, **kwargs)

    monkeypatch.setattr(sc, "run_capture", _capturar)
    await sc.compilar_patcher_local(
        raiz,
        "MiPatcher/MiPatcher.csproj",
        cache_raiz=tmp_path / "cache",
        dotnet=_dotnet_exe(tmp_path),
        hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
        timeout_s=60,
        max_output_bytes=1_000_000,
    )
    restore = next(ll["args"] for ll in dotnet.llamadas if ll["args"][1] == "restore")
    assert pathlib.Path(restore[restore.index("--configfile") + 1]).name == "nuget.config"
    assert "evil.example" not in capturado["config"]
    assert "<clear />" in capturado["config"]
    assert FUENTE_NUGET_APROBABLE in capturado["config"]


async def test_fuentes_cambiadas_durante_el_hitl_fallan(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hallazgo Codex P2: lo que se compila tiene que ser lo que se hasheó para la clave de caché.
    raiz = _fuentes(tmp_path)

    class _HitlQueEdita(_Hitl):
        async def request_approval(self, **kwargs: Any) -> Decision:
            (raiz / "MiPatcher" / "Program.cs").write_text("// editado mientras se aprobaba", encoding="utf-8")
            return await super().request_approval(**kwargs)

    dotnet = _Dotnet()
    monkeypatch.setattr(sc, "run_capture", dotnet)
    with pytest.raises(sc.CompilacionPatcherError, match="cambiaron"):
        await sc.compilar_patcher_local(
            raiz,
            "MiPatcher/MiPatcher.csproj",
            cache_raiz=tmp_path / "cache",
            dotnet=_dotnet_exe(tmp_path),
            hitl=_HitlQueEdita(Decision.APPROVED),  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )
    assert dotnet.llamadas == []
    assert not list((tmp_path / "cache").glob("*/marca.json"))


async def test_assembly_name_distinto_del_csproj(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resultado = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(assembly_name="OtroNombre"), monkeypatch)
    assert resultado.exe.name == "OtroNombre.exe"
    segundo = await _compilar(tmp_path, _Hitl(Decision.DENIED), _Dotnet(assembly_name="OtroNombre"), monkeypatch)
    assert segundo.cache_hit is True and segundo.exe == resultado.exe


@pytest.mark.parametrize("nombre", ["", "sub/Nombre", "..\\Nombre", "Dos\nLineas"])
async def test_assembly_name_invalido_falla(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, nombre: str
) -> None:
    with pytest.raises(sc.CompilacionPatcherError, match="AssemblyName"):
        await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(assembly_name=nombre), monkeypatch)


async def test_cambiar_el_sdk_invalida_la_cache(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CodeRabbit: un SDK actualizado en el mismo dotnet root no puede devolver el build del anterior.
    (tmp_path / "dotnet" / "sdk" / "10.0.404").mkdir(parents=True)
    (tmp_path / "dotnet" / "dotnet.exe").write_bytes(pe_sintetico())
    primero = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    (tmp_path / "dotnet" / "sdk" / "10.0.500").mkdir()
    hitl = _Hitl(Decision.APPROVED)
    segundo = await _compilar(tmp_path, hitl, _Dotnet(), monkeypatch)
    assert segundo.cache_hit is False and len(hitl.pedidos) == 1
    assert segundo.exe != primero.exe


async def test_otro_dotnet_no_reusa_la_cache(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raiz = _fuentes(tmp_path)
    primero = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    otro = tmp_path / "otro_dotnet" / "dotnet.exe"
    otro.parent.mkdir()
    otro.write_bytes(pe_sintetico())
    segundo = await sc.compilar_patcher_local(
        raiz,
        "MiPatcher/MiPatcher.csproj",
        cache_raiz=tmp_path / "cache",
        dotnet=otro,
        hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
        timeout_s=60,
        max_output_bytes=1_000_000,
    )
    assert segundo.cache_hit is False and segundo.exe != primero.exe


async def test_error_del_sistema_al_lanzar_dotnet_es_error_de_compilacion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _falla(args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        raise OSError("no se pudo asignar el Job Object")

    monkeypatch.setattr(sc, "run_capture", _falla)
    with pytest.raises(sc.CompilacionPatcherError, match="Job Object"):
        await sc.compilar_patcher_local(
            _fuentes(tmp_path),
            "MiPatcher/MiPatcher.csproj",
            cache_raiz=tmp_path / "cache",
            dotnet=_dotnet_exe(tmp_path),
            hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )


async def test_publicacion_concurrente_devuelve_la_entrada_ganadora(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dos builds del mismo patcher: mientras A espera la aprobación, B compila y publica. A no debe
    # fallar al publicar: la entrada de B es válida y se revalida.
    raiz = _fuentes(tmp_path)
    dotnet_exe = _dotnet_exe(tmp_path)
    monkeypatch.setattr(sc, "run_capture", _Dotnet())

    async def _compilar_b() -> sc.PatcherCompilado:
        return await sc.compilar_patcher_local(
            raiz,
            "MiPatcher/MiPatcher.csproj",
            cache_raiz=tmp_path / "cache",
            dotnet=dotnet_exe,
            hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )

    resultado_b: list[sc.PatcherCompilado] = []

    class _HitlQueDejaPasarAB(_Hitl):
        async def request_approval(self, **kwargs: Any) -> Decision:
            resultado_b.append(await _compilar_b())
            return await super().request_approval(**kwargs)

    resultado_a = await sc.compilar_patcher_local(
        raiz,
        "MiPatcher/MiPatcher.csproj",
        cache_raiz=tmp_path / "cache",
        dotnet=dotnet_exe,
        hitl=_HitlQueDejaPasarAB(Decision.APPROVED),  # type: ignore[arg-type]
        timeout_s=60,
        max_output_bytes=1_000_000,
    )
    assert resultado_a.exe == resultado_b[0].exe
    assert resultado_a.cache_hit is True


async def test_cache_raiz_relativa_falla_sin_red(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermano del hallazgo de PR-3e: dotnet corre con otro cwd y una ruta relativa apuntaría a otro lado.
    dotnet, hitl = _Dotnet(), _Hitl(Decision.APPROVED)
    monkeypatch.setattr(sc, "run_capture", dotnet)
    with pytest.raises(sc.CompilacionPatcherError, match="absoluta"):
        await sc.compilar_patcher_local(
            _fuentes(tmp_path),
            "MiPatcher/MiPatcher.csproj",
            cache_raiz=pathlib.Path("cache_relativa"),
            dotnet=_dotnet_exe(tmp_path),
            hitl=hitl,  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
        )
    assert hitl.pedidos == [] and dotnet.llamadas == []


async def test_publica_solo_la_salida_y_la_marca(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resultado = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    assert sorted(p.name for p in resultado.exe.parents[1].iterdir()) == ["marca.json", "salida"]
    assert not list((tmp_path / "cache").glob(".trabajo-*"))


@pytest.mark.parametrize(("rc_restore", "rc_build"), [(1, 0), (0, 1)])
async def test_un_fallo_no_deja_carpetas_de_trabajo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, rc_restore: int, rc_build: int
) -> None:
    with pytest.raises(sc.CompilacionPatcherError):
        await _compilar(
            tmp_path, _Hitl(Decision.APPROVED), _Dotnet(rc_restore=rc_restore, rc_build=rc_build), monkeypatch
        )
    assert not list((tmp_path / "cache").glob(".trabajo-*"))


async def test_el_build_no_ve_el_perfil_real_del_usuario(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""PR-3i: el Directory.Build.targets de Synthesis 0.36.6 borra %USERPROFILE%\.nuget\packages\<id>.

    Restore, build y msbuild corren con un perfil privado dentro de la carpeta de trabajo: el perfil
    real nunca llega a un proceso de la Fase 1.
    """
    perfil_real = str(tmp_path / "Users" / "real")
    for clave in ("USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.setenv(clave, perfil_real)
    dotnet = _Dotnet()
    await _compilar(tmp_path, _Hitl(Decision.APPROVED), dotnet, monkeypatch)
    for llamada in dotnet.llamadas:
        env = llamada["env"]
        assert perfil_real not in env.values()
        trabajo = pathlib.Path(env["USERPROFILE"]).parent
        assert trabajo.parent == tmp_path / "cache" and trabajo.name.startswith(".trabajo-")
        assert env["HOME"] == env["DOTNET_CLI_HOME"] == env["USERPROFILE"]
        assert not {k.upper() for k in env} & {"HOMEDRIVE", "HOMEPATH"}


async def test_las_propiedades_llegan_al_build_y_cambian_la_clave_de_cache(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sc, "run_capture", _Dotnet())
    base = await _compilar(tmp_path, _Hitl(Decision.APPROVED), _Dotnet(), monkeypatch)
    dotnet = _Dotnet()
    monkeypatch.setattr(sc, "run_capture", dotnet)
    con_props = await sc.compilar_patcher_local(
        tmp_path / "usuario" / "MiSolucion",
        "MiPatcher/MiPatcher.csproj",
        cache_raiz=tmp_path / "cache",
        dotnet=tmp_path / "dotnet" / "dotnet.exe",
        hitl=_Hitl(Decision.APPROVED),  # type: ignore[arg-type]
        timeout_s=60,
        max_output_bytes=1_000_000,
        propiedades={"Version": "0.36.6"},
    )
    assert con_props.cache_hit is False and con_props.exe.parents[1] != base.exe.parents[1]
    build = next(ll["args"] for ll in dotnet.llamadas if ll["args"][1] == "build")
    assert "-p:Version=0.36.6" in build


async def test_una_propiedad_no_admitida_falla_sin_red(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hitl, dotnet = _Hitl(Decision.APPROVED), _Dotnet()
    monkeypatch.setattr(sc, "run_capture", dotnet)
    with pytest.raises(sc.CompilacionPatcherError, match="no admitida"):
        await sc.compilar_patcher_local(
            _fuentes(tmp_path),
            "MiPatcher/MiPatcher.csproj",
            cache_raiz=tmp_path / "cache",
            dotnet=_dotnet_exe(tmp_path),
            hitl=hitl,  # type: ignore[arg-type]
            timeout_s=60,
            max_output_bytes=1_000_000,
            propiedades={"CETCompat": "true"},
        )
    assert hitl.pedidos == [] and dotnet.llamadas == []
