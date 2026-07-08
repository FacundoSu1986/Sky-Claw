"""Tests del sensor de masters faltantes (T-30·1, Oleada 7 / ADR 0002).

Primer sensor adicional del preflight: detecta plugins cuyos masters no están
instalados o no están habilitados en el load order — la causa clásica de CTD
al arrancar. Lee los subrecords MAST del header TES4 con parsing binario puro
(sin xEdit ni LOOT: el preflight corre antes de cualquier herramienta).

Las fixtures construyen plugins sintéticos con el layout real de Skyrim SE:
record TES4 (header de 24 bytes) + subrecords HEDR/MAST/DATA.
"""

from __future__ import annotations

import pathlib
import struct

import pytest

from sky_claw.local.validators.missing_masters import (
    MissingMastersChecker,
    masters_preflight_check,
    read_masters,
)
from sky_claw.local.validators.preflight import PreflightStatus


def _tes4_plugin(path: pathlib.Path, masters: list[str]) -> pathlib.Path:
    """Escribe un plugin sintético con header TES4 válido y esos masters."""
    subrecords = b""
    hedr = struct.pack("<fiI", 1.7, 0, 0x800)
    subrecords += b"HEDR" + struct.pack("<H", len(hedr)) + hedr
    for master in masters:
        data = master.encode("cp1252") + b"\x00"
        subrecords += b"MAST" + struct.pack("<H", len(data)) + data
        subrecords += b"DATA" + struct.pack("<H", 8) + struct.pack("<Q", 0)
    header = b"TES4" + struct.pack("<IIIIHH", len(subrecords), 0, 0, 0, 44, 0)
    path.write_bytes(header + subrecords)
    return path


def _tes4_raw(path: pathlib.Path, payload: bytes, *, declared_size: int | None = None) -> pathlib.Path:
    """Escribe un plugin sintético con payload TES4 arbitrario."""
    data_size = len(payload) if declared_size is None else declared_size
    header = b"TES4" + struct.pack("<IIIIHH", data_size, 0, 0, 0, 44, 0)
    path.write_bytes(header + payload)
    return path


# ---------------------------------------------------------------------------
# read_masters: parsing binario del header TES4
# ---------------------------------------------------------------------------


def test_read_masters_extrae_los_mast_en_orden(tmp_path: pathlib.Path) -> None:
    plugin = _tes4_plugin(tmp_path / "Mod.esp", ["Skyrim.esm", "Update.esm", "USSEP.esp"])

    assert read_masters(plugin) == ["Skyrim.esm", "Update.esm", "USSEP.esp"]


def test_read_masters_sin_masters_devuelve_vacio(tmp_path: pathlib.Path) -> None:
    plugin = _tes4_plugin(tmp_path / "Standalone.esp", [])

    assert read_masters(plugin) == []


def test_read_masters_archivo_truncado_no_explota(tmp_path: pathlib.Path) -> None:
    """Un header corrupto/truncado produce error tipado, no una excepción cruda."""
    from sky_claw.local.validators.missing_masters import PluginHeaderError

    roto = tmp_path / "Roto.esp"
    roto.write_bytes(b"TES4\xff\xff")  # ni siquiera alcanza para el header

    with pytest.raises(PluginHeaderError):
        read_masters(roto)


def test_read_masters_firma_invalida_es_error_tipado(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.validators.missing_masters import PluginHeaderError

    falso = tmp_path / "NoEsPlugin.esp"
    falso.write_bytes(b"MZ\x90\x00" + b"\x00" * 60)  # un .exe renombrado

    with pytest.raises(PluginHeaderError):
        read_masters(falso)


def test_read_masters_rechaza_header_tes4_demasiado_grande(tmp_path: pathlib.Path) -> None:
    """Un dataSize anómalo debe fallar antes de intentar leer un payload gigante."""
    from sky_claw.local.validators.missing_masters import PluginHeaderError

    gigante = _tes4_raw(tmp_path / "Gigante.esp", b"", declared_size=64 * 1024 * 1024)

    with pytest.raises(PluginHeaderError, match="demasiado grande"):
        read_masters(gigante)


def test_read_masters_rechaza_subrecord_truncado(tmp_path: pathlib.Path) -> None:
    """Un MAST cuyo size excede el payload TES4 es corrupción, no master parcial."""
    from sky_claw.local.validators.missing_masters import PluginHeaderError

    truncado = _tes4_raw(tmp_path / "Truncado.esp", b"MAST" + struct.pack("<H", 100) + b"Skyrim.esm")

    with pytest.raises(PluginHeaderError, match="subrecord"):
        read_masters(truncado)


def test_read_masters_rechaza_xxxx_truncado(tmp_path: pathlib.Path) -> None:
    """XXXX debe contener sus 4 bytes antes de extender el siguiente subrecord."""
    from sky_claw.local.validators.missing_masters import PluginHeaderError

    xxxx_truncado = _tes4_raw(tmp_path / "XXXXTruncado.esp", b"XXXX" + struct.pack("<H", 4) + b"\x20\x00")

    with pytest.raises(PluginHeaderError, match="XXXX"):
        read_masters(xxxx_truncado)


# ---------------------------------------------------------------------------
# MissingMastersChecker
# ---------------------------------------------------------------------------


def test_todo_presente_y_habilitado_sin_issues(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Skyrim.esm", [])
    _tes4_plugin(data / "Mod.esp", ["Skyrim.esm"])

    checker = MissingMastersChecker(plugin_dirs=[data])

    assert checker.check(["Skyrim.esm", "Mod.esp"]) == []


def test_master_inexistente_es_critical(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Mod.esp", ["NoInstalado.esm"])

    issues = MissingMastersChecker(plugin_dirs=[data]).check(["Mod.esp"])

    assert len(issues) == 1
    assert issues[0].kind == "missing"
    assert issues[0].severity == "critical"
    assert issues[0].plugin == "Mod.esp"
    assert issues[0].master == "NoInstalado.esm"
    assert "instal" in issues[0].remediation.lower()  # remediación accionable


def test_master_en_disco_pero_deshabilitado_es_critical(tmp_path: pathlib.Path) -> None:
    """El master existe pero no está en el load order habilitado: el motor
    igual crashea al cargar — pero la remediación es distinta (habilitarlo)."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Skyrim.esm", [])
    _tes4_plugin(data / "Requiem.esp", [])
    _tes4_plugin(data / "Parche.esp", ["Skyrim.esm", "Requiem.esp"])

    issues = MissingMastersChecker(plugin_dirs=[data]).check(["Skyrim.esm", "Parche.esp"])

    assert len(issues) == 1
    assert issues[0].kind == "disabled"
    assert issues[0].severity == "critical"
    assert issues[0].master == "Requiem.esp"
    assert "habilit" in issues[0].remediation.lower()


def test_matching_case_insensitive_como_windows(tmp_path: pathlib.Path) -> None:
    """En Windows los nombres no distinguen mayúsculas: 'skyrim.esm' en el
    header debe matchear 'Skyrim.esm' del load order sin falso positivo."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Skyrim.esm", [])
    _tes4_plugin(data / "Mod.esp", ["skyrim.esm"])

    issues = MissingMastersChecker(plugin_dirs=[data]).check(["SKYRIM.ESM", "Mod.esp"])

    assert issues == []


def test_plugin_del_load_order_que_no_existe_se_reporta(tmp_path: pathlib.Path) -> None:
    """Un plugins.txt stale que lista un plugin inexistente es su propio issue."""
    data = tmp_path / "Data"
    data.mkdir()

    issues = MissingMastersChecker(plugin_dirs=[data]).check(["Fantasma.esp"])

    assert len(issues) == 1
    assert issues[0].kind == "plugin_not_found"
    assert issues[0].severity == "warning"


def test_master_habilitado_pero_ausente_en_disco_es_critical(tmp_path: pathlib.Path) -> None:
    """Un master en plugins.txt pero borrado del disco no satisface al dependiente."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Dependiente.esp", ["Fantasma.esm"])

    issues = MissingMastersChecker(plugin_dirs=[data]).check(["Fantasma.esm", "Dependiente.esp"])

    assert any(i.plugin == "Fantasma.esm" and i.kind == "plugin_not_found" for i in issues)
    assert any(
        i.plugin == "Dependiente.esp"
        and i.master == "Fantasma.esm"
        and i.kind == "missing"
        and i.severity == "critical"
        for i in issues
    )
    assert masters_preflight_check(issues).status is PreflightStatus.RED


def test_header_corrupto_se_reporta_como_unreadable(tmp_path: pathlib.Path) -> None:
    """Un plugin ilegible no tumba el preflight: issue warning y se sigue."""
    data = tmp_path / "Data"
    data.mkdir()
    (data / "Corrupto.esp").write_bytes(b"garbage")

    issues = MissingMastersChecker(plugin_dirs=[data]).check(["Corrupto.esp"])

    assert len(issues) == 1
    assert issues[0].kind == "unreadable"
    assert issues[0].severity == "warning"


def test_busca_en_multiples_directorios(tmp_path: pathlib.Path) -> None:
    """MO2 reparte plugins entre mods: el checker recibe varios directorios."""
    data = tmp_path / "Data"
    mod_a = tmp_path / "mods" / "ModA"
    data.mkdir()
    mod_a.mkdir(parents=True)
    _tes4_plugin(data / "Skyrim.esm", [])
    _tes4_plugin(mod_a / "ModA.esp", ["Skyrim.esm"])

    issues = MissingMastersChecker(plugin_dirs=[data, mod_a]).check(["Skyrim.esm", "ModA.esp"])

    assert issues == []


def test_directorio_inaccesible_no_tumba_preflight(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un fallo de IO al listar directorios degrada a best-effort."""
    data = tmp_path / "Data"
    data.mkdir()
    original_iterdir = pathlib.Path.iterdir

    def fail_for_data(self: pathlib.Path):
        if self == data:
            raise OSError("permiso denegado")
        return original_iterdir(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", fail_for_data)
    caplog.set_level("DEBUG", logger="sky_claw.local.validators.missing_masters")

    issues = MissingMastersChecker(plugin_dirs=[data]).check(["Fantasma.esp"])

    assert len(issues) == 1
    assert issues[0].kind == "plugin_not_found"
    assert "permiso denegado" in caplog.text


# ---------------------------------------------------------------------------
# Puente al semáforo del preflight (composición sin tocar preflight.py)
# ---------------------------------------------------------------------------


def test_check_verde_sin_issues() -> None:
    check = masters_preflight_check([])

    assert check.name == "masters"
    assert check.status is PreflightStatus.GREEN


def test_check_rojo_con_master_faltante(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Mod.esp", ["NoInstalado.esm"])
    issues = MissingMastersChecker(plugin_dirs=[data]).check(["Mod.esp"])

    check = masters_preflight_check(issues)

    assert check.status is PreflightStatus.RED
    assert any("NoInstalado.esm" in d for d in check.details)


def test_check_amarillo_con_solo_warnings(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    issues = MissingMastersChecker(plugin_dirs=[data]).check(["Fantasma.esp"])

    check = masters_preflight_check(issues)

    assert check.status is PreflightStatus.YELLOW
