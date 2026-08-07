"""Persistencia de config compartida por las dos superficies (GUI y agente LLM).

Por qué existe: el autoinstalador de Community Shaders persiste los nombres de
los mods para que el orquestador los active en `modlist.txt` (contrato NGIO), y
cada superficie lo hacía a su manera:

* la GUI abría el path con `local_config.load` (JSON) — pero
  `AppContext.config_path` es el TOML canónico (`~/.sky_claw/config.toml`), así
  que el parseo fallaba, `load` devolvía defaults con TODO en `None`, y el
  `save` posterior reescribía el archivo entero: la config del usuario
  (mo2_root, skyrim_path, provider, modelos, chat id) desaparecía sin error;
* el agente escribía en `Config._data` y llamaba `local_config.save`, que hace
  `asdict()` y sólo acepta dataclases: con el `Config` real que inyecta
  `AppContext` levantaba `TypeError` y el campo nunca llegaba al disco.

Los dos formatos reescriben el archivo completo sin backup ni escritura
atómica, así que "leer mal" y "escribir" es destrucción de datos silenciosa.

Estos tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): el round-trip se ejerce sobre **las dos clases reales** de config
que existen en el árbol, y un ancla por AST congela quién puede importar los
`load`/`save` crudos del formato JSON legacy.
"""

from __future__ import annotations

import ast
import json
import pathlib
import tomllib

import pytest

from sky_claw.config import Config
from sky_claw.local.local_config import (
    LocalConfig,
    abrir_config_para_persistir,
    escribir_campo,
    guardar_config,
    persistir_campo,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

_TOML_DE_USUARIO = """\
llm_provider = "anthropic"
anthropic_model = "claude-opus-5"
mo2_root = "D:/Modding/MO2"
skyrim_path = "D:/Steam/steamapps/common/Skyrim Special Edition"
install_dir = "D:/Modding/Tools"
loot_exe = "D:/Modding/Tools/LOOT/LOOT.exe"
"""


# ── Round-trip por formato ──────────────────────────────────────────────────────
def test_persistir_campo_sobre_toml_conserva_la_config_del_usuario(tmp_path: pathlib.Path) -> None:
    """El path canónico es TOML: persistir un campo NO puede volverlo JSON de defaults."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_TOML_DE_USUARIO, encoding="utf-8")

    persistir_campo(config_path, "community_shaders_mods", ["Address Library", "Community Shaders"])

    datos = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert datos["community_shaders_mods"] == ["Address Library", "Community Shaders"]
    # Lo que había antes sigue ahí: éste es el punto del test, no el campo nuevo.
    assert datos["mo2_root"] == "D:/Modding/MO2"
    assert datos["skyrim_path"] == "D:/Steam/steamapps/common/Skyrim Special Edition"
    assert datos["install_dir"] == "D:/Modding/Tools"
    assert datos["loot_exe"] == "D:/Modding/Tools/LOOT/LOOT.exe"
    assert datos["llm_provider"] == "anthropic"
    assert datos["anthropic_model"] == "claude-opus-5"


def test_persistir_campo_sobre_json_legacy_conserva_la_config(tmp_path: pathlib.Path) -> None:
    """El JSON legacy (`sky_claw_config.json`) sigue soportado por su extensión."""
    config_path = tmp_path / "sky_claw_config.json"
    config_path.write_text(
        json.dumps({"mo2_root": "D:/Modding/MO2", "skyrim_path": "D:/Skyrim"}),
        encoding="utf-8",
    )

    persistir_campo(config_path, "ngio_mods", ["NGIO-NG"])

    datos = json.loads(config_path.read_text(encoding="utf-8"))
    assert datos["ngio_mods"] == ["NGIO-NG"]
    assert datos["mo2_root"] == "D:/Modding/MO2"
    assert datos["skyrim_path"] == "D:/Skyrim"


def test_persistir_campo_crea_el_archivo_si_no_existe(tmp_path: pathlib.Path) -> None:
    """Primer arranque: sin archivo previo no hay nada que perder, se crea."""
    config_path = tmp_path / "config.toml"

    persistir_campo(config_path, "community_shaders_mods", ["Community Shaders"])

    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["community_shaders_mods"] == ["Community Shaders"]


# ── Fail-closed: no se escribe sobre lo que no se pudo leer ─────────────────────
@pytest.mark.parametrize(
    ("nombre", "contenido"),
    [
        ("config.toml", "esto no es TOML = = ["),
        ("sky_claw_config.json", "{esto no es JSON"),
    ],
)
def test_abrir_config_para_persistir_falla_cerrado_si_no_parsea(
    tmp_path: pathlib.Path, nombre: str, contenido: str
) -> None:
    """Un archivo existente que no parsea aborta ANTES de devolver un objeto.

    Sin esto, el objeto con defaults viaja hasta el `save` y reescribe el
    archivo entero: exactamente la destrucción que este módulo evita. Que el
    archivo quede intacto es la mitad importante de la aserción.
    """
    config_path = tmp_path / nombre
    config_path.write_text(contenido, encoding="utf-8")

    with pytest.raises((tomllib.TOMLDecodeError, json.JSONDecodeError)):
        abrir_config_para_persistir(config_path)

    assert config_path.read_text(encoding="utf-8") == contenido


def test_persistir_campo_no_toca_el_archivo_ilegible(tmp_path: pathlib.Path) -> None:
    """El ciclo completo hereda el fail-closed del lector."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[[[roto", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        persistir_campo(config_path, "community_shaders_mods", ["X"])

    assert config_path.read_text(encoding="utf-8") == "[[[roto"


# ── Ancla: las DOS clases reales de config, no una muestra ──────────────────────
def test_escribir_y_guardar_soportan_las_dos_clases_reales(tmp_path: pathlib.Path) -> None:
    """`Config` (TOML, `_data`) y `LocalConfig` (JSON, dataclass) son las dos
    formas que circulan por el árbol: `AppContext` inyecta la primera en el
    registry del agente, y la segunda es el formato legacy que todavía migra
    `AppContext._migrate_legacy_json`. Un tercer tipo de config rompe acá antes
    de romper en producción.
    """
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(_TOML_DE_USUARIO, encoding="utf-8")
    json_path = tmp_path / "sky_claw_config.json"
    json_path.write_text("{}", encoding="utf-8")

    configs = {
        toml_path: Config(toml_path),
        json_path: LocalConfig(),
    }
    for path, config in configs.items():
        escribir_campo(config, "community_shaders_mods", ["A", "B"])
        guardar_config(config, path)

    assert tomllib.loads(toml_path.read_text(encoding="utf-8"))["community_shaders_mods"] == ["A", "B"]
    assert json.loads(json_path.read_text(encoding="utf-8"))["community_shaders_mods"] == ["A", "B"]


def test_guardar_config_rechaza_un_objeto_que_no_sabe_persistirse() -> None:
    """Nunca degradar a un `save` que no corresponde al formato: abortar."""

    class _ConfigAjena:
        pass

    with pytest.raises(TypeError, match="no sabe persistirse"):
        guardar_config(_ConfigAjena(), pathlib.Path("config.toml"))


# ── Ancla por AST: quién puede tocar el `save`/`load` crudos del JSON legacy ────
#: `save` escribe JSON pase lo que pase y `load` devuelve defaults en silencio
#: cuando el archivo no es JSON: juntos son la receta exacta que borró la config.
#: Fuera de `local_config.py` sólo se permiten los usos listados acá.
IMPORTADORES_PERMITIDOS = {
    # Migración legacy: el archivo *sí* es JSON en ese camino (`sky_claw_config.json`).
    "sky_claw/app_context.py": {"load"},
}

_MODULO = "sky_claw.local.local_config"
_CRUDOS = {"load", "save"}


def _importadores_crudos() -> dict[str, set[str]]:
    encontrados: dict[str, set[str]] = {}
    for archivo in PAQUETE.rglob("*.py"):
        if archivo.name == "local_config.py":
            continue  # el dueño del formato
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        nombres = {
            alias.name
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.ImportFrom) and nodo.module == _MODULO
            for alias in nodo.names
            if alias.name in _CRUDOS
        }
        if nombres:
            encontrados[archivo.relative_to(RAIZ).as_posix()] = nombres
    return encontrados


def test_ancla_de_importadores_del_save_json_crudo() -> None:
    """Igualdad literal, no `issubset`: un importador nuevo rompe hasta que se
    decida si va por `persistir_campo`/`guardar_config` o es otra exención.
    """
    assert _importadores_crudos() == IMPORTADORES_PERMITIDOS
