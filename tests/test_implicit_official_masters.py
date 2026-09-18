"""RED de #585: los masters oficiales implícitos no son plugins "disabled".

Perfil MO2 real (el del rig de PR #580): ``plugins.txt`` solo marca con ``*``
el mod dependiente, y ``loadorder.txt`` contiene los masters oficiales
(``Skyrim.esm`` + DLCs) seguidos del mod. ``EscudoDwember.esp`` declara
``MAST Skyrim.esm`` y ``Skyrim.esm`` está instalado en ``Data``.

El modelo viejo colapsaba "habilitado explícitamente por MO2" con "carga en el
load order efectivo": al resolver solo ``plugins.txt``, ``Skyrim.esm`` quedaba
fuera del set y ``MissingMastersChecker`` lo reportaba ``[disabled]`` — un falso
RED sobre un master oficial que el motor carga siempre.

Estos tests ejercen el cableado COMPLETO (resolver del perfil → builders de
sensores → checker), que es el camino que produjo el bug real. Los controles
(A2/A3/A6) congelan que el fix no rebaje el fail-closed de los RED genuinos.
"""

from __future__ import annotations

import pathlib
import struct

from sky_claw.local.validators.preflight_sensors import (
    build_master_order_sensor,
    build_mo2_profile_sources_resolver,
    build_modlist_sensors,
)

#: Flags del record TES4 (mismos valores que `plugin_header`).
_FLAG_MASTER = 0x00000001

#: Masters oficiales mínimos de Skyrim SE/AE (la política vive en producción).
_OFICIALES = ("Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm")


def _tes4_plugin(
    path: pathlib.Path,
    masters: list[str],
    *,
    flags: int = 0,
) -> pathlib.Path:
    """Escribe un plugin sintético con header TES4 válido, masters y flags."""
    subrecords = b""
    hedr = struct.pack("<fiI", 1.7, 0, 0x800)
    subrecords += b"HEDR" + struct.pack("<H", len(hedr)) + hedr
    for master in masters:
        data = master.encode("cp1252") + b"\x00"
        subrecords += b"MAST" + struct.pack("<H", len(data)) + data
        subrecords += b"DATA" + struct.pack("<H", 8) + struct.pack("<Q", 0)
    header = b"TES4" + struct.pack("<IIIIHH", len(subrecords), flags, 0, 0, 44, 0)
    path.write_bytes(header + subrecords)
    return path


def _perfil_mo2(
    tmp_path: pathlib.Path,
    *,
    plugins_txt: str | None,
    loadorder_txt: str | None,
    mods: dict[str, dict[str, list[str]]],
    oficiales: tuple[str, ...] = _OFICIALES,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Arma un entorno MO2 sintético con perfil ``Default``.

    Args:
        plugins_txt: Contenido de ``plugins.txt`` (``None`` para omitirlo).
        loadorder_txt: Contenido de ``loadorder.txt`` (``None`` para omitirlo).
        mods: ``{carpeta_de_mod: {plugin: [masters]}}``.
        oficiales: Masters oficiales escritos en ``Data``.

    Returns:
        ``(game, mo2)`` — el juego (con ``Data``) y la instancia MO2.
    """
    game = tmp_path / "Skyrim"
    data = game / "Data"
    data.mkdir(parents=True)
    for nombre in oficiales:
        _tes4_plugin(data / nombre, [])

    mo2 = tmp_path / "MO2"
    mods_dir = mo2 / "mods"
    for carpeta, plugins in mods.items():
        mod_dir = mods_dir / carpeta
        mod_dir.mkdir(parents=True)
        for plugin, masters in plugins.items():
            _tes4_plugin(mod_dir / plugin, masters)

    profile_dir = mo2 / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    if plugins_txt is not None:
        (profile_dir / "plugins.txt").write_text(plugins_txt, encoding="utf-8")
    if loadorder_txt is not None:
        (profile_dir / "loadorder.txt").write_text(loadorder_txt, encoding="utf-8")
    return game, mo2


def _sensores(game: pathlib.Path, mo2: pathlib.Path):
    resolver = build_mo2_profile_sources_resolver(game=game, mo2=mo2, profile="Default")
    assert resolver is not None
    masters, limits = build_modlist_sensors(resolver)
    order = build_master_order_sensor(resolver)
    assert masters is not None and limits is not None and order is not None
    return masters, limits, order


# ---------------------------------------------------------------------------
# A1 — el falso RED real de #585, por el cableado completo
# ---------------------------------------------------------------------------


def test_a1_master_oficial_implicito_no_es_falso_disabled(tmp_path: pathlib.Path) -> None:
    """``*EscudoDwember.esp`` + ``MAST Skyrim.esm`` con Skyrim.esm instalado.

    ``plugins.txt`` no lo marca (MO2 no lista masters implícitos), pero
    ``loadorder.txt`` lo pone primero y el motor lo carga siempre: masters y
    orden deben quedar VERDES.
    """
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*EscudoDwember.esp\n",
        loadorder_txt="\n".join([*_OFICIALES, "EscudoDwember.esp"]) + "\n",
        mods={"EscudoDwember": {"EscudoDwember.esp": ["Skyrim.esm"]}},
    )
    masters, _limits, order = _sensores(game, mo2)

    assert masters() == []
    assert order() == []


# ---------------------------------------------------------------------------
# Controles: el fix no puede rebajar los RED genuinos
# ---------------------------------------------------------------------------


def test_a2_master_de_mod_instalado_y_deshabilitado_sigue_rojo(tmp_path: pathlib.Path) -> None:
    """``Requiem.esp`` existe pero no está habilitado ni es oficial."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*Parche.esp\n",
        loadorder_txt="\n".join([*_OFICIALES, "Requiem.esp", "Parche.esp"]) + "\n",
        mods={
            "Requiem": {"Requiem.esp": []},
            "Parche": {"Parche.esp": ["Skyrim.esm", "Requiem.esp"]},
        },
    )
    masters, _limits, _order = _sensores(game, mo2)

    issues = masters()
    assert [(i.plugin, i.master, i.kind) for i in issues] == [("Parche.esp", "Requiem.esp", "disabled")]


def test_a3_master_ausente_sigue_rojo(tmp_path: pathlib.Path) -> None:
    """``Fantasma.esm`` no existe en ningún directorio de plugins."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*Parche.esp\n",
        loadorder_txt="\n".join([*_OFICIALES, "Parche.esp"]) + "\n",
        mods={"Parche": {"Parche.esp": ["Fantasma.esm"]}},
    )
    masters, _limits, _order = _sensores(game, mo2)

    issues = masters()
    assert [(i.plugin, i.master, i.kind) for i in issues] == [("Parche.esp", "Fantasma.esm", "missing")]


def test_a6_inversion_de_masters_no_oficiales_sigue_rojo(tmp_path: pathlib.Path) -> None:
    """El enriquecimiento con oficiales no tape una inversión real entre mods."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*Parche.esm\n*Base.esm\n",
        loadorder_txt="\n".join([*_OFICIALES, "Parche.esm", "Base.esm"]) + "\n",
        mods={
            "Base": {"Base.esm": []},
            "Parche": {"Parche.esm": ["Base.esm"]},
        },
    )
    _masters, _limits, order = _sensores(game, mo2)

    issues = order()
    assert [(i.plugin, i.master, i.kind) for i in issues] == [("Parche.esm", "Base.esm", "master_after_dependent")]


def test_a6b_master_oficial_implicito_primero_no_rompe_el_orden(tmp_path: pathlib.Path) -> None:
    """Un dependiente de un oficial cuyo ``loadorder`` lo lista primero: verde."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*Dependiente.esm\n",
        loadorder_txt="\n".join([*_OFICIALES, "Dependiente.esm"]) + "\n",
        mods={"Dep": {"Dependiente.esm": ["Skyrim.esm"]}},
    )
    _masters, _limits, order = _sensores(game, mo2)

    assert order() == []


# ---------------------------------------------------------------------------
# A7 — límites: el conteo sale del MISMO snapshot efectivo
# ---------------------------------------------------------------------------


def test_a7_limits_cuentan_oficiales_implicitos_y_excluyen_deshabilitados(tmp_path: pathlib.Path) -> None:
    """Los 5 oficiales consumen slots full reales; un plugin presente pero sin
    ``*`` no se cuenta como activo."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*Activo.esp\n",
        loadorder_txt="\n".join([*_OFICIALES, "Deshabilitado.esp", "Activo.esp"]) + "\n",
        mods={
            "Activo": {"Activo.esp": []},
            "Deshabilitado": {"Deshabilitado.esp": []},
        },
    )
    _masters, limits, _order = _sensores(game, mo2)

    resultado = limits()
    assert resultado.full_count == len(_OFICIALES) + 1  # 5 oficiales + Activo.esp
    assert resultado.light_count == 0


# ---------------------------------------------------------------------------
# A8/A9 — identidad case-insensitive y sin duplicados en el wiring
# ---------------------------------------------------------------------------


def test_a8_master_declarado_con_otra_grafia_no_es_falso_disabled(tmp_path: pathlib.Path) -> None:
    """``MAST SKYRIM.ESM`` con ``Skyrim.esm`` instalado: misma identidad Windows."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*Mod.esp\n",
        loadorder_txt="Skyrim.esm\nMod.esp\n",
        mods={"Mod": {"Mod.esp": ["SKYRIM.ESM"]}},
        oficiales=("Skyrim.esm",),
    )
    masters, _limits, _order = _sensores(game, mo2)

    assert masters() == []


def test_a8b_oficial_en_disco_con_otra_grafia_entra_al_snapshot(tmp_path: pathlib.Path) -> None:
    """``SKYRIM.ESM`` en disco se reconoce como el oficial ``Skyrim.esm``."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*Mod.esp\n",
        loadorder_txt=None,
        mods={"Mod": {"Mod.esp": ["Skyrim.esm"]}},
        oficiales=("SKYRIM.ESM",),
    )
    masters, _limits, _order = _sensores(game, mo2)

    assert masters() == []


def test_a9_oficial_explicito_implicito_y_ordenado_no_duplica(tmp_path: pathlib.Path) -> None:
    """El mismo oficial en ``plugins.txt``, ``loadorder.txt`` y derivación
    implícita aparece UNA vez en el orden efectivo (sin inversiones espurias)."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*skyrim.esm\n*Mod.esm\n",
        loadorder_txt="Skyrim.esm\nMod.esm\n",
        mods={"Mod": {"Mod.esm": ["Skyrim.esm"]}},
    )
    resolver = build_mo2_profile_sources_resolver(game=game, mo2=mo2, profile="Default")
    assert resolver is not None
    sources = resolver()
    claves = [n.casefold() for n in sources.effective_enabled_plugins]
    assert claves.count("skyrim.esm") == 1
    assert len(claves) == len(set(claves))
    assert "mod.esm" in claves
    masters, _limits, order = _sensores(game, mo2)
    assert masters() == []
    assert order() == []


# ---------------------------------------------------------------------------
# A10 — fallback sin loadorder.txt: determinista y documentado
# ---------------------------------------------------------------------------


def test_a10_sin_loadorder_los_oficiales_van_primero(tmp_path: pathlib.Path) -> None:
    """Sin ``loadorder.txt`` el orden efectivo es oficiales (canónico) y después
    la activación; nunca un orden inventado con el oficial detrás de su
    dependiente."""
    game, mo2 = _perfil_mo2(
        tmp_path,
        plugins_txt="*Mod.esm\n",
        loadorder_txt=None,
        mods={"Mod": {"Mod.esm": ["Skyrim.esm"]}},
    )
    resolver = build_mo2_profile_sources_resolver(game=game, mo2=mo2, profile="Default")
    assert resolver is not None
    sources = resolver()

    assert sources.ordered_plugins == ("Mod.esm",)
    assert sources.effective_enabled_plugins[: len(_OFICIALES)] == _OFICIALES
    assert sources.effective_enabled_plugins[-1] == "Mod.esm"
    _masters, _limits, order = _sensores(game, mo2)
    assert order() == []
