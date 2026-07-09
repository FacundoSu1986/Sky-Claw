"""Tests del parser TES4 canónico (T-17 seed): masters + flags reales.

Extrae el header de un plugin sin xEdit/LOOT. Lo nuevo respecto de
``read_masters`` es la lectura de los **flags reales** del record — master
(ESM, 0x1) y light (ESL/ESPFE, 0x200) — que es lo que distingue un ``.esp``
con flag ligero (cuenta contra el pool FE) de un ``.esp`` normal.
"""

from __future__ import annotations

import pathlib
import struct

import pytest

from sky_claw.local.validators.plugin_header import (
    PluginHeaderError,
    read_plugin_header,
)

_FLAG_MASTER = 0x00000001
_FLAG_LIGHT = 0x00000200


def _plugin(path: pathlib.Path, *, masters: list[str] | None = None, flags: int = 0) -> pathlib.Path:
    """Escribe un plugin sintético con esos masters y flags de record."""
    subrecords = b""
    hedr = struct.pack("<fiI", 1.7, 0, 0x800)
    subrecords += b"HEDR" + struct.pack("<H", len(hedr)) + hedr
    for master in masters or []:
        data = master.encode("cp1252") + b"\x00"
        subrecords += b"MAST" + struct.pack("<H", len(data)) + data
        subrecords += b"DATA" + struct.pack("<H", 8) + struct.pack("<Q", 0)
    # header: TES4 + dataSize + flags + formID + vc + formVersion + vc2
    header = b"TES4" + struct.pack("<IIIIHH", len(subrecords), flags, 0, 0, 44, 0)
    path.write_bytes(header + subrecords)
    return path


def test_lee_masters_y_flags_apagados(tmp_path: pathlib.Path) -> None:
    plugin = _plugin(tmp_path / "Mod.esp", masters=["Skyrim.esm"], flags=0)

    header = read_plugin_header(plugin)

    assert header.masters == ["Skyrim.esm"]
    assert header.is_master is False
    assert header.is_light is False


def test_flag_master(tmp_path: pathlib.Path) -> None:
    plugin = _plugin(tmp_path / "Base.esm", flags=_FLAG_MASTER)

    header = read_plugin_header(plugin)

    assert header.is_master is True
    assert header.is_light is False


def test_flag_light_en_un_esp_es_espfe(tmp_path: pathlib.Path) -> None:
    """Un .esp con el flag light (0x200) es ESPFE: cuenta como ligero pese a la extensión."""
    plugin = _plugin(tmp_path / "LightPatch.esp", flags=_FLAG_LIGHT)

    header = read_plugin_header(plugin)

    assert header.is_light is True
    assert header.is_master is False


def test_flag_master_y_light_combinados(tmp_path: pathlib.Path) -> None:
    plugin = _plugin(tmp_path / "LightMaster.esm", flags=_FLAG_MASTER | _FLAG_LIGHT)

    header = read_plugin_header(plugin)

    assert header.is_master is True
    assert header.is_light is True


def test_header_invalido_es_error_tipado(tmp_path: pathlib.Path) -> None:
    falso = tmp_path / "NoEsPlugin.esp"
    falso.write_bytes(b"MZ\x90\x00" + b"\x00" * 60)

    with pytest.raises(PluginHeaderError):
        read_plugin_header(falso)
