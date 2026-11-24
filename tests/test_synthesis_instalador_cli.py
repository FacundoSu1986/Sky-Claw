"""PR-3i (ADR 0019 §2.2): ``ToolsInstaller.ensure_synthesis_cli`` compila el CLI de Synthesis LOCAL.

Las fuentes (PR-3e) y el build (PR-3d) se reemplazan por falsos que registran con qué se los llamó:
así se fija el pin (repositorio + commit completo), las propiedades MSBuild, el lock de la carpeta y
la copia verificada sin red ni SDK .NET. El build real del CLI se verifica en el rig.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local import tools_installer as ti
from sky_claw.local.tools import synthesis_compilacion as sc
from sky_claw.local.tools import synthesis_fuentes_git as sg
from sky_claw.local.tools.synthesis_prebuild import (
    PROPIEDADES_MSBUILD_ADMITIDAS,
    argumentos_dotnet_build_patcher,
    es_ejecutable_de_consola,
    validar_propiedades_msbuild,
)
from sky_claw.local.tools.synthesis_runner import VARIABLES_DE_PERFIL, con_perfil_privado
from sky_claw.local.tools.synthesis_shim import hash_arbol
from tests._pe_sintetico import pe_sintetico


class _Lock:
    def __init__(self) -> None:
        self.acquire_lock = AsyncMock(side_effect=self._acquire)
        self.release_lock = AsyncMock(return_value=True)
        self.renew_lock = AsyncMock(return_value=True)

    async def _acquire(self, *args: object, **kwargs: object) -> MagicMock:
        lock = MagicMock()
        lock.resource_id, lock.agent_id = args[0], args[1]
        return lock


def _installer(lock: _Lock | None = None) -> ti.ToolsInstaller:
    return ti.ToolsInstaller(
        hitl=MagicMock(),
        gateway=MagicMock(),
        path_validator=MagicMock(),
        lock_manager=lock or _Lock(),  # type: ignore[arg-type]
    )


def _salida_de_build(raiz: pathlib.Path, *, subsistema: int = 3) -> sc.PatcherCompilado:
    salida = raiz / "builds" / "clave" / "salida"
    salida.mkdir(parents=True, exist_ok=True)
    exe = salida / ti.SYNTHESIS_CLI_EXE
    exe.write_bytes(pe_sintetico(subsistema=subsistema))
    (salida / "Synthesis.Bethesda.CLI.dll").write_bytes(b"il")
    (salida / "runtimes").mkdir(exist_ok=True)
    (salida / "runtimes" / "nativo.dll").write_bytes(b"n")
    return sc.PatcherCompilado(exe=exe, salida_sha256=hash_arbol(salida), cache_hit=False)


def _falsos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, *, subsistema: int = 3, error: Exception | None = None
) -> dict[str, Any]:
    llamadas: dict[str, Any] = {}

    async def _fuentes(url: str, sha: str, **kw: Any) -> sg.FuentesGit:
        llamadas["git"] = (url, sha, kw)
        if error is not None:
            raise error
        arbol = tmp_path / "cache" / "git" / "x" / "arbol"
        arbol.mkdir(parents=True, exist_ok=True)
        return sg.FuentesGit(raiz=arbol, arbol_sha256="a" * 64, cache_hit=False)

    async def _compilar(raiz: pathlib.Path, proyecto: str, **kw: Any) -> sc.PatcherCompilado:
        llamadas["build"] = (raiz, proyecto, kw)
        return _salida_de_build(tmp_path / "cache", subsistema=subsistema)

    monkeypatch.setattr(sg, "obtener_fuentes_git", _fuentes)
    monkeypatch.setattr(sc, "compilar_patcher_local", _compilar)
    return llamadas


async def _instalar(installer: ti.ToolsInstaller, tmp_path: pathlib.Path, destino: pathlib.Path) -> ti.InstallResult:
    return await installer.ensure_synthesis_cli(
        destino, dotnet=tmp_path / "dotnet.exe", git=tmp_path / "git.exe", cache_raiz=tmp_path / "cache"
    )


async def test_compila_el_tag_pineado_y_copia_la_salida_verificada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llamadas = _falsos(monkeypatch, tmp_path)
    lock = _Lock()
    installer = _installer(lock)
    destino = tmp_path / "tools" / "Synthesis"
    resultado = await _instalar(installer, tmp_path, destino)

    url, sha, kw_git = llamadas["git"]
    assert (url, sha) == ("https://github.com/Mutagen-Modding/Synthesis", "d8150fb29d544216081e37e2704e56e3b48460f7")
    assert kw_git["cache_raiz"] == tmp_path / "cache" / "git" and kw_git["hitl"] is installer._hitl
    raiz, proyecto, kw_build = llamadas["build"]
    assert raiz == tmp_path / "cache" / "git" / "x" / "arbol"
    assert proyecto == "Synthesis.Bethesda.CLI/Synthesis.Bethesda.CLI.csproj"
    assert kw_build["propiedades"] == {"DisableGitVersionTask": "true", "Version": "0.36.6"}
    assert kw_build["cache_raiz"] == tmp_path / "cache" / "builds" and kw_build["hitl"] is installer._hitl

    assert resultado.exe_path == destino / "Synthesis.Bethesda.CLI.exe"
    assert (resultado.version, resultado.already_existed) == ("0.36.6", False)
    assert (destino / "runtimes" / "nativo.dll").is_file()
    assert lock.acquire_lock.await_count == 1 and lock.release_lock.await_count == 1
    assert lock.acquire_lock.await_args.args[0] == ti._install_lock_resource_id(destino)


async def test_un_cli_de_consola_existente_se_usa_sin_red(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llamadas = _falsos(monkeypatch, tmp_path)
    destino = tmp_path / "Synthesis"
    destino.mkdir()
    (destino / ti.SYNTHESIS_CLI_EXE).write_bytes(pe_sintetico(subsistema=3))
    resultado = await _instalar(_installer(), tmp_path, destino)
    assert resultado.already_existed is True
    assert llamadas == {}


async def test_un_exe_de_ventana_con_el_nombre_del_cli_falla_cerrado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llamadas = _falsos(monkeypatch, tmp_path)
    destino = tmp_path / "Synthesis"
    destino.mkdir()
    (destino / ti.SYNTHESIS_CLI_EXE).write_bytes(pe_sintetico(subsistema=2))
    with pytest.raises(ti.ToolInstallError, match="SYNTHESIS_CLI_MISSING"):
        await _instalar(_installer(), tmp_path, destino)
    assert llamadas == {}


async def test_un_build_que_no_da_consola_falla(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _falsos(monkeypatch, tmp_path, subsistema=2)
    with pytest.raises(ti.ToolInstallError, match="SYNTHESIS_CLI_MISSING"):
        await _instalar(_installer(), tmp_path, tmp_path / "Synthesis")


async def test_no_mezcla_con_una_carpeta_con_restos(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _falsos(monkeypatch, tmp_path)
    destino = tmp_path / "Synthesis"
    destino.mkdir()
    (destino / "resto.dll").write_bytes(b"viejo")
    with pytest.raises(ti.ToolInstallError, match="vacialo"):
        await _instalar(_installer(), tmp_path, destino)
    assert sorted(p.name for p in destino.iterdir()) == ["resto.dll"]


async def test_un_fallo_de_fuentes_es_error_de_instalacion_y_suelta_el_lock(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _falsos(monkeypatch, tmp_path, error=sg.FuentesGitError("HITL denegado"))
    lock = _Lock()
    with pytest.raises(ti.ToolInstallError, match="HITL denegado"):
        await _instalar(_installer(lock), tmp_path, tmp_path / "Synthesis")
    assert lock.release_lock.await_count == 1


async def test_cache_relativa_falla_antes_del_lock(tmp_path: pathlib.Path) -> None:
    lock = _Lock()
    with pytest.raises(ti.ToolInstallError, match="absoluta"):
        await _installer(lock).ensure_synthesis_cli(
            tmp_path / "S", dotnet=tmp_path / "d.exe", git=tmp_path / "g.exe", cache_raiz=pathlib.Path("rel")
        )
    lock.acquire_lock.assert_not_awaited()


# --- Piezas compartidas con la Fase 1 -----------------------------------------------------------


def test_es_ejecutable_de_consola_lee_el_subsistema(tmp_path: pathlib.Path) -> None:
    for nombre, datos, esperado in [
        ("cui.exe", pe_sintetico(subsistema=3), True),
        ("gui.exe", pe_sintetico(subsistema=2), False),
        ("basura.exe", b"no es un PE", False),
    ]:
        (tmp_path / nombre).write_bytes(datos)
        assert es_ejecutable_de_consola(tmp_path / nombre) is esperado, nombre
    assert es_ejecutable_de_consola(tmp_path / "no-existe.exe") is False


def test_las_propiedades_msbuild_son_una_lista_cerrada() -> None:
    assert frozenset({"DisableGitVersionTask", "Version"}) == PROPIEDADES_MSBUILD_ADMITIDAS
    assert validar_propiedades_msbuild({"Version": "0.36.6", "DisableGitVersionTask": "true"}) == (
        ("DisableGitVersionTask", "true"),
        ("Version", "0.36.6"),
    )
    for malas in ({"CETCompat": "true"}, {"Version": "1;rm"}, {"Version": ""}, {"Version": "-p:X=1"}):
        with pytest.raises(ValueError):
            validar_propiedades_msbuild(malas)


def test_el_build_agrega_las_propiedades_sin_pisar_los_flags_fijos(tmp_path: pathlib.Path) -> None:
    args = argumentos_dotnet_build_patcher(
        tmp_path / "p.csproj", salida=tmp_path / "o", propiedades={"Version": "0.36.6", "DisableGitVersionTask": "true"}
    )
    assert args == [
        *argumentos_dotnet_build_patcher(tmp_path / "p.csproj")[:9],
        "-p:DisableGitVersionTask=true",
        "-p:Version=0.36.6",
        "-o",
        str(tmp_path / "o"),
    ]
    assert "-p:CETCompat=false" in args and "-p:UseSharedCompilation=false" in args


def test_con_perfil_privado_reemplaza_todo_el_perfil_sin_importar_mayusculas(tmp_path: pathlib.Path) -> None:
    env = {
        "UserProfile": "C:/Users/real",
        "HOME": "C:/Users/real",
        "HomeDrive": "C:",
        "HOMEPATH": "/Users/real",
        "PATH": "x",
    }
    con_perfil_privado(env, tmp_path / "home")
    assert env == {
        "PATH": "x",
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "DOTNET_CLI_HOME": str(tmp_path / "home"),
    }
    assert {"HOME", "USERPROFILE", "DOTNET_CLI_HOME"} <= VARIABLES_DE_PERFIL


async def test_cancelar_durante_la_copia_no_suelta_el_lock_con_el_hilo_vivo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receta T-31 del mutador nuevo: el lock de install_dir se suelta recién cuando la copia terminó."""
    import asyncio
    import threading

    _falsos(monkeypatch, tmp_path)
    arranco, soltar = threading.Event(), threading.Event()
    orden: list[str] = []
    copia_real = ti._copiar_cli_verificado

    def _copia_lenta(salida: pathlib.Path, destino: pathlib.Path, sha: str) -> pathlib.Path:
        orden.append("copia:inicio")
        arranco.set()
        soltar.wait(timeout=10)
        orden.append("copia:fin")
        return copia_real(salida, destino, sha)

    monkeypatch.setattr(ti, "_copiar_cli_verificado", _copia_lenta)
    lock = _Lock()
    tarea = asyncio.ensure_future(_instalar(_installer(lock), tmp_path, tmp_path / "Synthesis"))
    await asyncio.to_thread(arranco.wait, 10)
    tarea.cancel()
    await asyncio.sleep(0.05)
    assert lock.release_lock.await_count == 0, "no se suelta el lock con la copia escribiendo en install_dir"
    soltar.set()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert orden == ["copia:inicio", "copia:fin"]
    assert lock.release_lock.await_count == 1


async def test_una_copia_cortada_no_deja_un_cli_que_despues_pase_por_instalado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hallazgo de Codex en #1013: la copia se verifica en staging y se publica entera o nada."""
    _falsos(monkeypatch, tmp_path)
    destino = tmp_path / "tools" / "Synthesis"
    copytree_real = ti.shutil.copytree

    def _copia_que_se_corta(src: Any, dst: Any, **kw: Any) -> Any:
        pathlib.Path(dst).mkdir(parents=True)
        (pathlib.Path(dst) / ti.SYNTHESIS_CLI_EXE).write_bytes(pe_sintetico(subsistema=3))
        raise OSError(28, "No queda espacio en el disco")

    monkeypatch.setattr(ti.shutil, "copytree", _copia_que_se_corta)
    with pytest.raises(ti.ToolInstallError, match="No se pudo copiar"):
        await _instalar(_installer(), tmp_path, destino)
    assert not destino.exists()
    assert [p.name for p in destino.parent.iterdir()] == []  # ni destino ni staging

    # El reintento no ve un "ya instalado": vuelve a compilar y publica la copia entera.
    monkeypatch.setattr(ti.shutil, "copytree", copytree_real)
    resultado = await _instalar(_installer(), tmp_path, destino)
    assert resultado.already_existed is False
    assert (destino / "Synthesis.Bethesda.CLI.dll").is_file()


async def test_destino_vacio_preexistente_se_reemplaza_por_la_copia_verificada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _falsos(monkeypatch, tmp_path)
    destino = tmp_path / "Synthesis"
    destino.mkdir()
    resultado = await _instalar(_installer(), tmp_path, destino)
    assert resultado.exe_path == destino / ti.SYNTHESIS_CLI_EXE
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".Synthesis")) == []
