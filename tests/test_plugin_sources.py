"""Tests del resolver de fuentes de plugins y load order (T-30w, #585).

``resolve_plugin_sources`` traduce el entorno (Data del juego, mods de MO2, los
archivos de load order) en el **snapshot** que los sensores de masters, orden y
límites necesitan. El snapshot separa cuatro conceptos que antes colapsaban en
un único tuple ambiguo:

* ``explicit_enabled_plugins`` — lo que MO2 activa (``*`` en ``plugins.txt``).
* ``implicit_official_masters`` — oficiales **instalados** que el motor carga
  siempre aunque ``plugins.txt`` no los marque.
* ``ordered_plugins`` — el orden del perfil (``loadorder.txt``); listar ahí no
  es habilitar.
* ``effective_enabled_plugins`` — unión de los dos primeros, deduplicada
  case-insensitive y ordenada de forma determinista.

Pieza pura y testeable con un fixture MO2 en tmp; el cableado real vive en
``preflight_sensors``.
"""

from __future__ import annotations

import pathlib

from sky_claw.local.mo2.plugin_sources import (
    OFFICIAL_MASTERS,
    PluginSources,
    resolve_plugin_sources,
)


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


def _data_con_oficiales(tmp_path: pathlib.Path, nombres: tuple[str, ...]) -> pathlib.Path:
    data = tmp_path / "Skyrim" / "Data"
    data.mkdir(parents=True)
    for nombre in nombres:
        (data / nombre).write_bytes(b"TES4")
    return data


# ---------------------------------------------------------------------------
# plugin_dirs
# ---------------------------------------------------------------------------


def test_plugin_dirs_incluye_mods_y_data(tmp_path: pathlib.Path) -> None:
    root = _mo2(tmp_path)
    sources = resolve_plugin_sources(
        game_data_dir=root / "Skyrim" / "Data",
        mo2_mods_dir=root / "MO2" / "mods",
    )

    nombres = {d.name for d in sources.plugin_dirs}
    assert "ModA" in nombres
    assert "ModB" in nombres
    assert "Data" in nombres


def test_plugin_dirs_sin_fuentes_es_vacio(tmp_path: pathlib.Path) -> None:
    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None)

    assert sources == PluginSources(plugin_dirs=(), explicit_enabled_plugins=(), activation_source_status="absent")


def test_mods_dir_inexistente_no_explota(tmp_path: pathlib.Path) -> None:
    sources = resolve_plugin_sources(
        game_data_dir=None,
        mo2_mods_dir=tmp_path / "no-existe",
    )

    assert sources.plugin_dirs == ()


def test_overwrite_incluido_con_maxima_precedencia(tmp_path: pathlib.Path) -> None:
    """El overwrite de MO2 (plugins generados) se incluye y va primero (gana el
    first-match de los checkers) — review Codex #252."""
    mods = tmp_path / "MO2" / "mods"
    (mods / "ModA").mkdir(parents=True)
    overwrite = tmp_path / "MO2" / "overwrite"
    overwrite.mkdir(parents=True)
    data = tmp_path / "Skyrim" / "Data"
    data.mkdir(parents=True)

    sources = resolve_plugin_sources(
        game_data_dir=data,
        mo2_mods_dir=mods,
        mo2_overwrite_dir=overwrite,
    )

    assert sources.plugin_dirs[0] == overwrite  # máxima precedencia
    assert data in sources.plugin_dirs  # Data última pero presente


# ---------------------------------------------------------------------------
# explicit_enabled_plugins — activación de MO2
# ---------------------------------------------------------------------------


def test_plugins_txt_solo_los_activos(tmp_path: pathlib.Path) -> None:
    """Formato moderno: solo las líneas con `*` están activas."""
    lo = tmp_path / "plugins.txt"
    lo.write_text("*A.esp\nB.esp\n*C.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, plugins_file=lo)

    assert sources.explicit_enabled_plugins == ("A.esp", "C.esp")


def test_plugins_txt_sin_asteriscos_cae_a_todos(tmp_path: pathlib.Path) -> None:
    """Formato viejo (listar == activar): si ninguna línea trae `*`, todas cuentan."""
    lo = tmp_path / "plugins.txt"
    lo.write_text("A.esp\nB.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, plugins_file=lo)

    assert sources.explicit_enabled_plugins == ("A.esp", "B.esp")


def test_loadorder_solo_como_archivo_cae_a_listar_igual_activar(tmp_path: pathlib.Path) -> None:
    """Fallback histórico: sin ``plugins.txt``, ``loadorder.txt`` lista habilitados."""
    lo = tmp_path / "loadorder.txt"
    lo.write_text("Skyrim.esm\nA.esp\nB.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, order_file=lo)

    assert sources.explicit_enabled_plugins == ("Skyrim.esm", "A.esp", "B.esp")


def test_ignora_comentarios_y_vacias(tmp_path: pathlib.Path) -> None:
    lo = tmp_path / "plugins.txt"
    lo.write_text("# comentario\n\n*A.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, plugins_file=lo)

    assert sources.explicit_enabled_plugins == ("A.esp",)


def test_bom_en_plugins_txt_se_maneja(tmp_path: pathlib.Path) -> None:
    """MO2 escribe plugins.txt con BOM UTF-8; no debe contaminar el primer nombre."""
    lo = tmp_path / "plugins.txt"
    lo.write_bytes(b"\xef\xbb\xbf*A.esp\r\n*B.esp\r\n")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, plugins_file=lo)

    assert sources.explicit_enabled_plugins == ("A.esp", "B.esp")


def test_byte_invalido_no_deja_enabled_vacio(tmp_path: pathlib.Path) -> None:
    """best-effort real: un byte no-UTF8 en un comentario no debe tirar la
    decodificación y borrar el load order entero (review Copilot #252)."""
    lo = tmp_path / "plugins.txt"
    lo.write_bytes(b"# comentario \xff roto\r\n*A.esp\r\n")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, plugins_file=lo)

    assert sources.explicit_enabled_plugins == ("A.esp",)


# ---------------------------------------------------------------------------
# implicit_official_masters — disponibilidad física, no decreto
# ---------------------------------------------------------------------------


def test_oficiales_instalados_entran_en_el_snapshot(tmp_path: pathlib.Path) -> None:
    data = _data_con_oficiales(tmp_path, OFFICIAL_MASTERS)

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None)

    assert sources.implicit_official_masters == OFFICIAL_MASTERS


def test_oficiales_ausentes_no_se_inventan(tmp_path: pathlib.Path) -> None:
    """Solo los físicamente disponibles cuentan: la ausencia no se enmascara."""
    data = _data_con_oficiales(tmp_path, ("Skyrim.esm", "Update.esm"))

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None)

    assert sources.implicit_official_masters == ("Skyrim.esm", "Update.esm")


def test_sin_rutas_no_hay_oficiales(tmp_path: pathlib.Path) -> None:
    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None)

    assert sources.implicit_official_masters == ()


def test_creation_club_no_es_implicito_por_prefijo(tmp_path: pathlib.Path) -> None:
    """``cc*`` es contenido oficial, pero su activación no se deriva del prefijo:
    sin una fuente explícita NO entra al conjunto implícito (política #585)."""
    data = _data_con_oficiales(tmp_path, ("Skyrim.esm", "ccBGSSSE001-Fish.esm", "ccQDRSSE001-SurvivalMode.esl"))

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None)

    assert sources.implicit_official_masters == ("Skyrim.esm",)


def test_creation_club_activado_explicitamente_si_es_efectivo(tmp_path: pathlib.Path) -> None:
    """La fuente explícita manda: un `*` en plugins.txt sí lo habilita."""
    data = _data_con_oficiales(tmp_path, ("Skyrim.esm", "ccBGSSSE001-Fish.esm"))
    lo = tmp_path / "plugins.txt"
    lo.write_text("*ccBGSSSE001-Fish.esm\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None, plugins_file=lo)

    assert "ccBGSSSE001-Fish.esm" in sources.effective_enabled_plugins
    assert sources.implicit_official_masters == ("Skyrim.esm",)


def test_matching_case_insensitive_como_windows(tmp_path: pathlib.Path) -> None:
    """``SKYRIM.ESM`` en disco es el mismo master que ``Skyrim.esm``."""
    data = _data_con_oficiales(tmp_path, ("SKYRIM.ESM",))

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None)

    assert sources.implicit_official_masters == ("Skyrim.esm",)


# ---------------------------------------------------------------------------
# ordered_plugins — orden no es habilitación
# ---------------------------------------------------------------------------


def test_loadorder_preserva_el_orden_del_perfil(tmp_path: pathlib.Path) -> None:
    orden = tmp_path / "loadorder.txt"
    orden.write_text("Skyrim.esm\nA.esp\nB.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, order_file=orden)

    assert sources.ordered_plugins == ("Skyrim.esm", "A.esp", "B.esp")


def test_listar_en_loadorder_no_habilita(tmp_path: pathlib.Path) -> None:
    """``B.esp`` está en loadorder.txt (orden) pero no en plugins.txt (activación)."""
    plugins = tmp_path / "plugins.txt"
    plugins.write_text("*A.esp\n", encoding="utf-8")
    orden = tmp_path / "loadorder.txt"
    orden.write_text("Skyrim.esm\nA.esp\nB.esp\n", encoding="utf-8")
    data = _data_con_oficiales(tmp_path, ("Skyrim.esm",))

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None, plugins_file=plugins, order_file=orden)

    assert sources.ordered_plugins == ("Skyrim.esm", "A.esp", "B.esp")
    assert sources.explicit_enabled_plugins == ("A.esp",)
    assert sources.effective_enabled_plugins == ("Skyrim.esm", "A.esp")


def test_sin_loadorder_el_orden_sale_de_plugins_txt(tmp_path: pathlib.Path) -> None:
    plugins = tmp_path / "plugins.txt"
    plugins.write_text("*B.esp\n*A.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, plugins_file=plugins)

    assert sources.ordered_plugins == ("B.esp", "A.esp")
    assert sources.effective_enabled_plugins == ("B.esp", "A.esp")


# ---------------------------------------------------------------------------
# effective_enabled_plugins — el universo que ven los sensores
# ---------------------------------------------------------------------------


def test_efectivo_une_explicitos_y_oficiales(tmp_path: pathlib.Path) -> None:
    """Sin loadorder.txt los oficiales van al frente (el motor los carga primero)."""
    data = _data_con_oficiales(tmp_path, ("Skyrim.esm", "Update.esm"))
    plugins = tmp_path / "plugins.txt"
    plugins.write_text("*Mod.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None, plugins_file=plugins)

    assert sources.effective_enabled_plugins == ("Skyrim.esm", "Update.esm", "Mod.esp")


def test_efectivo_deduplica_case_insensitive(tmp_path: pathlib.Path) -> None:
    """El oficial explícito y el implícito son la misma identidad, no dos slots."""
    data = _data_con_oficiales(tmp_path, ("Skyrim.esm",))
    plugins = tmp_path / "plugins.txt"
    plugins.write_text("*skyrim.esm\n*Mod.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None, plugins_file=plugins)

    claves = [n.casefold() for n in sources.effective_enabled_plugins]
    assert claves == ["skyrim.esm", "mod.esp"]


def test_efectivo_excluye_deshabilitados_reales(tmp_path: pathlib.Path) -> None:
    data = _data_con_oficiales(tmp_path, ("Skyrim.esm",))
    plugins = tmp_path / "plugins.txt"
    plugins.write_text("*Habilitado.esp\nDeshabilitado.esp\n", encoding="utf-8")
    orden = tmp_path / "loadorder.txt"
    orden.write_text("Skyrim.esm\nHabilitado.esp\nDeshabilitado.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None, plugins_file=plugins, order_file=orden)

    assert sources.effective_enabled_plugins == ("Skyrim.esm", "Habilitado.esp")


# ---------------------------------------------------------------------------
# activation_source_status — no colapsar "no existe", "leído" e "ilegible"
# ---------------------------------------------------------------------------


def test_estado_ok_con_plugins_legible(tmp_path: pathlib.Path) -> None:
    plugins = tmp_path / "plugins.txt"
    plugins.write_text("*A.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, plugins_file=plugins)

    assert sources.activation_source_status == "ok"
    assert sources.explicit_enabled_plugins == ("A.esp",)


def test_estado_absent_sin_plugins_pero_con_loadorder(tmp_path: pathlib.Path) -> None:
    """Sin ``plugins.txt`` el fallback histórico no es un estado de error."""
    orden = tmp_path / "loadorder.txt"
    orden.write_text("A.esp\n", encoding="utf-8")

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, order_file=orden)

    assert sources.activation_source_status == "absent"
    assert sources.explicit_enabled_plugins == ("A.esp",)


def test_estado_absent_sin_archivos(tmp_path: pathlib.Path) -> None:
    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None)

    assert sources.activation_source_status == "absent"
    assert sources.explicit_enabled_plugins == ()


def test_estado_unreadable_no_se_confunde_con_vacio(tmp_path: pathlib.Path, monkeypatch) -> None:
    """``plugins.txt`` ilegible ≠ ``plugins.txt`` vacío: el estado lo distingue y
    los oficiales instalados no lo enmascaran."""
    data = _data_con_oficiales(tmp_path, ("Skyrim.esm",))
    plugins = tmp_path / "plugins.txt"
    plugins.write_text("*A.esp\n", encoding="utf-8")
    original = pathlib.Path.read_text

    def _falla(self: pathlib.Path, *args, **kwargs):
        if self == plugins:
            raise OSError("permiso denegado")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", _falla)

    sources = resolve_plugin_sources(game_data_dir=data, mo2_mods_dir=None, plugins_file=plugins)

    assert sources.activation_source_status == "unreadable"
    assert sources.explicit_enabled_plugins == ()
    # El universal efectivo puede tener oficiales; es el estado el que impide
    # tratarlo como configuración válida.
    assert sources.effective_enabled_plugins == ("Skyrim.esm",)


def test_estado_unreadable_sin_plugins_con_loadorder_ilegible(tmp_path: pathlib.Path, monkeypatch) -> None:
    """También el fallback: un ``loadorder.txt`` ilegible deja la activación
    desconocida en vez de aparentar "no hay nada activo"."""
    orden = tmp_path / "loadorder.txt"
    orden.write_text("A.esp\n", encoding="utf-8")
    original = pathlib.Path.read_text

    def _falla(self: pathlib.Path, *args, **kwargs):
        if self == orden:
            raise OSError("permiso denegado")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", _falla)

    sources = resolve_plugin_sources(game_data_dir=None, mo2_mods_dir=None, order_file=orden)

    assert sources.activation_source_status == "unreadable"
    assert sources.explicit_enabled_plugins == ()
