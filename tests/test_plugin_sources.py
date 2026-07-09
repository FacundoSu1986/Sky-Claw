"""Tests del resolver de fuentes de plugins para el preflight (T-30w).

`resolve_plugin_sources` traduce el entorno (Data del juego, mods de MO2, el
archivo de load order) en lo que los sensores de masters/límites necesitan:
los directorios donde viven los plugins y la lista de plugins habilitados.
Pieza pura y testeable con un fixture MO2 en tmp — el cableado real vive en
`LootSortingService._ensure_preflight`.
"""

from __future__ import annotations

import pathlib

from sky_claw.local.mo2.plugin_sources import PluginSources, resolve_plugin_sources


def _mo2(tmp_path: pathlib.Path) -> pathlib.Path:
    """Instancia MO2 sintética: dos mods con plugins + Data del juego."""
    mods = tmp_path / "MO2" / "mods"
    (mods / "ModA").mkdir(parents=True)
    (mods / "ModA" / "A.esp").write_bytes(b"TES4")
    (mods / "ModB").mkdir(parents=True)
    (mods / "ModB" / "B.esp").write_bytes(b"TES4")
    data = tmp_path / "Skyrim" / "Data"
    data.mkdir(parents=True)
    (data / "Skyrim.esm").write_bytes(b"TES4")
    return tmp_path


# ---------------------------------------------------------------------------
# plugin_dirs
# ---------------------------------------------------------------------------


def test_plugin_dirs_incluye_mods_y_data(tmp_path: pathlib.Path) -> None:
    root = _mo2(tmp_path)
    sources = resolve_plugin_sources(
        game_data_dir=root / "Skyrim" / "Data",
        mo2_mods_dir=root / "MO2" / "mods",
        load_order_file=None,
    )

    nombres = {d.name for d in sources.plugin_dirs}
    assert "ModA" in nombres
    assert "ModB" in nombres
    assert "Data" in nombres


def test_plugin_dirs_sin_fuentes_es_vacio(tmp_path: pathlib.Path) -> None:
    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, load_order_file=None)

    assert sources == PluginSources(plugin_dirs=(), enabled_plugins=())


def test_mods_dir_inexistente_no_explota(tmp_path: pathlib.Path) -> None:
    sources = resolve_plugin_sources(
        game_data_dir=None,
        mo2_mods_dir=tmp_path / "no-existe",
        load_order_file=None,
    )

    assert sources.plugin_dirs == ()


# ---------------------------------------------------------------------------
# enabled_plugins
# ---------------------------------------------------------------------------


def test_plugins_txt_solo_los_activos(tmp_path: pathlib.Path) -> None:
    """Formato moderno: solo las líneas con `*` están activas."""
    lo = tmp_path / "plugins.txt"
    lo.write_text("*A.esp\nB.esp\n*C.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, load_order_file=lo)

    assert sources.enabled_plugins == ("A.esp", "C.esp")


def test_plugins_txt_sin_asteriscos_cae_a_todos(tmp_path: pathlib.Path) -> None:
    """Formato viejo (listar == activar): si ninguna línea trae `*`, todas cuentan."""
    lo = tmp_path / "plugins.txt"
    lo.write_text("A.esp\nB.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, load_order_file=lo)

    assert sources.enabled_plugins == ("A.esp", "B.esp")


def test_loadorder_txt_todas_las_lineas(tmp_path: pathlib.Path) -> None:
    lo = tmp_path / "loadorder.txt"
    lo.write_text("Skyrim.esm\nA.esp\nB.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, load_order_file=lo)

    assert sources.enabled_plugins == ("Skyrim.esm", "A.esp", "B.esp")


def test_ignora_comentarios_y_vacias(tmp_path: pathlib.Path) -> None:
    lo = tmp_path / "plugins.txt"
    lo.write_text("# comentario\n\n*A.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, load_order_file=lo)

    assert sources.enabled_plugins == ("A.esp",)


def test_bom_en_plugins_txt_se_maneja(tmp_path: pathlib.Path) -> None:
    """MO2 escribe plugins.txt con BOM UTF-8; no debe contaminar el primer nombre."""
    lo = tmp_path / "plugins.txt"
    lo.write_bytes(b"\xef\xbb\xbf*A.esp\r\n*B.esp\r\n")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, load_order_file=lo)

    assert sources.enabled_plugins == ("A.esp", "B.esp")
