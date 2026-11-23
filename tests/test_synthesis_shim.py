"""PR-3b (ADR 0019 §2.1/§2.4): shim de producción de los patchers de Synthesis.

Cada patcher de la Fase 2 se lanza a través de una copia del shim C#
(``sky_claw/local/tools/synthesis_shim_fuentes``) con un ``shim.json`` al lado. Acá se fija el
lado Python del contrato: el hash de árbol, la configuración cerrada (que reusa la decisión CET
de PR-3a) y la materialización de una copia por patcher. El lado C# se ancla por texto (claves y
csproj) y se ejecuta de verdad en el rig (``.NET 10 SDK``), no en CI.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pytest

from sky_claw.local.tools import synthesis_shim as ss
from sky_claw.local.tools.synthesis_prebuild import ModoLanzamiento
from tests._pe_sintetico import patcher_sintetico, pe_sintetico
from tests._symlink_guard import crear_junction, junction_guard

_FUENTES = pathlib.Path(ss.__file__).with_name("synthesis_shim_fuentes")


def _hash_esperado(archivos: dict[str, bytes]) -> str:
    """Algoritmo documentado, escrito aparte del módulo para no verificarlo contra sí mismo."""
    acumulado = hashlib.sha256()
    for rel in sorted(archivos):
        acumulado.update(f"{rel}\0{hashlib.sha256(archivos[rel]).hexdigest()}\n".encode())
    return acumulado.hexdigest()


def _dotnet(tmp_path: pathlib.Path) -> pathlib.Path:
    dotnet = tmp_path / "dotnet" / "dotnet.exe"
    dotnet.parent.mkdir(parents=True, exist_ok=True)
    dotnet.write_bytes(b"muxer")
    return dotnet


def _patcher(tmp_path: pathlib.Path, *, cet: int) -> pathlib.Path:
    return patcher_sintetico(tmp_path / "cache" / "MiPatcher", pe=pe_sintetico(cet=cet, apps=(b"MiPatcher.dll",)))


# --- hash de árbol ----------------------------------------------------------------------


def test_hash_de_arbol_sigue_el_algoritmo_documentado(tmp_path: pathlib.Path) -> None:
    archivos = {"a.dll": b"uno", "B.exe": b"dos", "sub/c.json": b"tres", "sub/z/d.bin": b"\0\1"}
    for rel, datos in archivos.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(datos)
    assert ss.hash_arbol(tmp_path) == _hash_esperado(archivos)


def test_hash_de_arbol_cambia_con_cualquier_archivo(tmp_path: pathlib.Path) -> None:
    exe = _patcher(tmp_path, cet=0)
    antes = ss.hash_arbol(exe.parent)
    (exe.parent / "Extra.dll").write_bytes(b"inyectada")
    assert ss.hash_arbol(exe.parent) != antes


@junction_guard
def test_hash_de_arbol_rechaza_un_junction(tmp_path: pathlib.Path) -> None:
    raiz = tmp_path / "patcher"
    raiz.mkdir()
    (raiz / "a.dll").write_bytes(b"x")
    destino = tmp_path / "afuera"
    destino.mkdir()
    assert crear_junction(raiz / "enlace", destino) is None
    with pytest.raises(ss.ShimConfigError):
        ss.hash_arbol(raiz)


def test_hash_de_arbol_rechaza_nombres_fuera_del_bmp(tmp_path: pathlib.Path) -> None:
    # C# ordena por UTF-16 y Python por code point: solo difieren fuera del BMP.
    (tmp_path / "\U0001f600.dll").write_bytes(b"x")
    with pytest.raises(ss.ShimConfigError):
        ss.hash_arbol(tmp_path)


def test_hash_de_arbol_rechaza_carpetas_fuera_del_bmp(tmp_path: pathlib.Path) -> None:
    # El shim C# revisa TODAS las entradas, no solo archivos: una carpeta vacía con emoji pasaría
    # en Python (no aporta líneas al hash) y fallaría en la Fase 2 con código 6.
    (tmp_path / "a.dll").write_bytes(b"x")
    (tmp_path / "\U0001f600").mkdir()
    with pytest.raises(ss.ShimConfigError):
        ss.hash_arbol(tmp_path)


def test_hash_de_arbol_convierte_errores_de_lectura(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.dll").write_bytes(b"x")
    abrir = pathlib.Path.open

    def _abrir_falla(self: pathlib.Path, *args: object, **kwargs: object) -> object:
        if self.name == "a.dll":
            raise PermissionError("bloqueado por el antivirus")
        return abrir(self, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(pathlib.Path, "open", _abrir_falla)
    with pytest.raises(ss.ShimConfigError, match="a.dll"):
        ss.hash_arbol(tmp_path)


def test_hash_de_arbol_de_carpeta_inexistente(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ss.ShimConfigError):
        ss.hash_arbol(tmp_path / "no")


# --- configuración ----------------------------------------------------------------------

# Modo de PR-3a -> modo del shim. Igualdad sobre el enum completo: un modo nuevo sin mapeo rompe.
_MODO_SHIM = {
    ModoLanzamiento.DIRECTO: "directo",
    ModoLanzamiento.VIA_DOTNET: "via_dotnet",
    ModoLanzamiento.RECHAZADO: None,  # no hay shim para un patcher rechazado
}


def test_cada_modo_de_lanzamiento_tiene_su_traduccion() -> None:
    assert set(_MODO_SHIM) == set(ModoLanzamiento)
    assert ss.MODO_SHIM_POR_MODO == _MODO_SHIM


def test_config_directa(tmp_path: pathlib.Path) -> None:
    exe = _patcher(tmp_path, cet=0)
    cfg = ss.construir_config_shim(exe, dotnet=_dotnet(tmp_path))
    assert cfg == {
        "schema": 1,
        "modo": "directo",
        "patcher": str(exe),
        "dll": None,
        "dotnet": None,  # directo no usa el muxer aunque se lo ofrezcan
        "arbolSha256": ss.hash_arbol(exe.parent),
        "internalDataFolder": None,
        "defaultDataFolderPath": None,
    }


def test_config_via_dotnet_para_patcher_con_cet(tmp_path: pathlib.Path) -> None:
    exe = _patcher(tmp_path, cet=1)
    dotnet = _dotnet(tmp_path)
    cfg = ss.construir_config_shim(exe, dotnet=dotnet)
    assert cfg["modo"] == "via_dotnet"
    assert cfg["dll"] == str(exe.with_suffix(".dll"))
    assert cfg["dotnet"] == str(dotnet)


def test_config_con_carpetas_de_datos(tmp_path: pathlib.Path) -> None:
    exe = _patcher(tmp_path, cet=0)
    internal, default = tmp_path / "InternalData", tmp_path / "Default"
    internal.mkdir()
    default.mkdir()
    cfg = ss.construir_config_shim(exe, internal_data_folder=internal, default_data_folder_path=default)
    assert (cfg["internalDataFolder"], cfg["defaultDataFolderPath"]) == (str(internal), str(default))


def test_las_claves_son_exactamente_las_del_contrato(tmp_path: pathlib.Path) -> None:
    cfg = ss.construir_config_shim(_patcher(tmp_path, cet=0))
    assert set(cfg) == ss.CLAVES_CONFIG_SHIM


def test_patcher_rechazado_no_genera_shim(tmp_path: pathlib.Path) -> None:
    exe = patcher_sintetico(tmp_path / "p", pe=pe_sintetico(cet=1, offsets_bundle=(168762,), apps=(b"MiPatcher.dll",)))
    with pytest.raises(ss.ShimConfigError, match="single-file"):
        ss.construir_config_shim(exe, dotnet=_dotnet(tmp_path))


def test_via_dotnet_sin_dotnet_falla(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ss.ShimConfigError, match="dotnet"):
        ss.construir_config_shim(_patcher(tmp_path, cet=1))


@pytest.mark.parametrize(
    "ruta",
    [
        "relativo/dotnet.exe",
        "C:/Program Files (x86)/dotnet/dotnet.exe",
        "C:/Program Files/dotnet/otro.exe",
    ],
    ids=["relativo", "x86", "no_es_dotnet_exe"],
)
def test_via_dotnet_con_dotnet_invalido_falla(tmp_path: pathlib.Path, ruta: str) -> None:
    with pytest.raises(ss.ShimConfigError):
        ss.construir_config_shim(_patcher(tmp_path, cet=1), dotnet=pathlib.Path(ruta))


def test_via_dotnet_con_dotnet_inexistente_falla(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ss.ShimConfigError):
        ss.construir_config_shim(_patcher(tmp_path, cet=1), dotnet=tmp_path / "no" / "dotnet.exe")


def test_carpetas_de_datos_van_juntas(tmp_path: pathlib.Path) -> None:
    exe = _patcher(tmp_path, cet=0)
    (tmp_path / "InternalData").mkdir()
    with pytest.raises(ss.ShimConfigError):
        ss.construir_config_shim(exe, internal_data_folder=tmp_path / "InternalData")


def test_carpeta_de_datos_inexistente_falla(tmp_path: pathlib.Path) -> None:
    exe = _patcher(tmp_path, cet=0)
    (tmp_path / "InternalData").mkdir()
    with pytest.raises(ss.ShimConfigError):
        ss.construir_config_shim(
            exe, internal_data_folder=tmp_path / "InternalData", default_data_folder_path=tmp_path / "no"
        )


def test_patcher_con_ruta_relativa_falla() -> None:
    with pytest.raises(ss.ShimConfigError):
        ss.construir_config_shim(pathlib.Path("MiPatcher/MiPatcher.exe"))


# --- materialización: una copia del shim por patcher -------------------------------------


def _binarios_shim(tmp_path: pathlib.Path, *, cet: int = 0) -> pathlib.Path:
    binarios = tmp_path / "shim_bin"
    patcher_sintetico(
        binarios, nombre="SkyClawSynthesisShim", pe=pe_sintetico(cet=cet, apps=(b"SkyClawSynthesisShim.dll",))
    )
    return binarios


def test_materializar_copia_el_shim_y_escribe_la_config(tmp_path: pathlib.Path) -> None:
    binarios = _binarios_shim(tmp_path)
    cfg = ss.construir_config_shim(_patcher(tmp_path, cet=0))
    destino = tmp_path / "shims" / "MiPatcher"
    exe_shim = ss.materializar_shim(binarios, destino, cfg)
    assert exe_shim == destino / ss.NOMBRE_EJECUTABLE_SHIM
    assert sorted(p.name for p in destino.iterdir()) == sorted([*(p.name for p in binarios.iterdir()), "shim.json"])
    assert json.loads((destino / "shim.json").read_text(encoding="utf-8")) == cfg


def test_materializar_no_pisa_un_destino_existente(tmp_path: pathlib.Path) -> None:
    binarios = _binarios_shim(tmp_path)
    cfg = ss.construir_config_shim(_patcher(tmp_path, cet=0))
    destino = tmp_path / "shims" / "MiPatcher"
    destino.mkdir(parents=True)
    with pytest.raises(ss.ShimConfigError, match="existe"):
        ss.materializar_shim(binarios, destino, cfg)


def test_materializar_rechaza_un_shim_compilado_con_cet(tmp_path: pathlib.Path) -> None:
    # El shim mismo corre bajo USVFS: si quedó con CET caería igual que cualquier patcher.
    binarios = _binarios_shim(tmp_path, cet=1)
    cfg = ss.construir_config_shim(_patcher(tmp_path, cet=0))
    with pytest.raises(ss.ShimConfigError, match="CETCompat"):
        ss.materializar_shim(binarios, tmp_path / "shims" / "MiPatcher", cfg)
    assert not (tmp_path / "shims" / "MiPatcher").exists()


def test_materializar_rechaza_config_fuera_de_contrato(tmp_path: pathlib.Path) -> None:
    binarios = _binarios_shim(tmp_path)
    cfg = dict(ss.construir_config_shim(_patcher(tmp_path, cet=0)))
    cfg["extra"] = "x"
    with pytest.raises(ss.ShimConfigError):
        ss.materializar_shim(binarios, tmp_path / "shims" / "MiPatcher", cfg)


@junction_guard
def test_materializar_rechaza_binarios_con_junction(tmp_path: pathlib.Path) -> None:
    binarios = _binarios_shim(tmp_path)
    (tmp_path / "afuera").mkdir()
    assert crear_junction(binarios / "enlace", tmp_path / "afuera") is None
    cfg = ss.construir_config_shim(_patcher(tmp_path, cet=0))
    with pytest.raises(ss.ShimConfigError):
        ss.materializar_shim(binarios, tmp_path / "shims" / "MiPatcher", cfg)


# --- anclas del lado C# -------------------------------------------------------------------


def test_claves_del_shim_csharp_coinciden_con_las_de_python() -> None:
    fuente = (_FUENTES / "Program.cs").read_text(encoding="utf-8")
    bloque = re.search(r"ClavesPermitidas\s*=\s*\{(?P<cuerpo>[^}]*)\}", fuente)
    assert bloque is not None
    assert set(re.findall(r'"([^"]+)"', bloque["cuerpo"])) == ss.CLAVES_CONFIG_SHIM


def test_esquema_del_shim_csharp_coincide() -> None:
    fuente = (_FUENTES / "Program.cs").read_text(encoding="utf-8")
    assert f"EsquemaSoportado = {ss.ESQUEMA_CONFIG_SHIM};" in fuente


def test_csproj_del_shim_apaga_cet_y_no_usa_paquetes() -> None:
    csproj = (_FUENTES / "SkyClawSynthesisShim.csproj").read_text(encoding="utf-8")
    assert "<CETCompat>false</CETCompat>" in csproj
    assert "<SelfContained>false</SelfContained>" in csproj
    assert "<PackageReference" not in csproj  # restore sin red
    assert pathlib.Path(ss.NOMBRE_EJECUTABLE_SHIM).stem == "SkyClawSynthesisShim"


def test_mensajes_del_shim_son_ascii() -> None:
    # La consola de Windows no está en UTF-8: un acento en stderr llega como "?" o mojibake al
    # log del CLI (visto en el rig). Los comentarios pueden llevar acentos; los literales no.
    fuente = (_FUENTES / "Program.cs").read_text(encoding="utf-8")
    sin_comentarios = re.sub(r"//[^\n]*", "", fuente)
    literales = re.findall(r'"[^"\n]*"', sin_comentarios)
    assert literales
    assert [lit for lit in literales if not lit.isascii()] == []


def test_directorio_de_fuentes() -> None:
    assert ss.directorio_fuentes_shim() == _FUENTES
    assert sorted(p.name for p in _FUENTES.iterdir()) == ["Program.cs", "SkyClawSynthesisShim.csproj"]
