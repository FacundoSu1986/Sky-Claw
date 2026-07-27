"""Tests del sensor de visibilidad de mods (U-01 de la auditoría 03 de pipeline).

El mantenedor confirmó que Sky-Claw corre **standalone**, no lanzado desde MO2:
los tools se spawnean directo y **ninguno hereda la USVFS**. Con un MO2 en USVFS
estándar (el default), los tools leen el ``Data`` del juego base — sin un solo
mod — y el pipeline reporta verde desde el Stage 1.

Ningún sensor previo lo detecta. ``VfsHealthChecker`` (T-13) solo busca
symlinks/junctions, y ``MissingMastersChecker`` busca a través de TODOS los
``plugin_dirs`` (``Data`` + ``mods/*`` + ``overwrite``): encuentra el plugin
dentro de ``mods/SomeMod/`` y lo reporta presente, que es exactamente el punto
ciego. Este sensor hace la pregunta que faltaba: **¿los plugins del perfil son
visibles en la ruta que el tool va a leer?**

Criterio deliberadamente conservador (ver ``vfs_visibility``): solo el caso
inequívoco — CERO de N visibles — bloquea. Una materialización parcial pasa: un
falso negativo acotado es preferible a frenar un setup que funciona.
"""

from __future__ import annotations

import pathlib

from sky_claw.local.validators.preflight import PreflightStatus
from sky_claw.local.validators.vfs_visibility import (
    VfsVisibilityChecker,
    VisibilityScan,
    visibility_preflight_check,
)

#: Masters base que SIEMPRE viven en Data (no son mods): verlos no prueba VFS.
_VANILLA = ("Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm")


def _entorno(
    tmp_path: pathlib.Path,
    *,
    habilitados: tuple[str, ...],
    en_data: tuple[str, ...],
) -> VfsVisibilityChecker:
    """Arma un juego con *en_data* materializado y un perfil que activa *habilitados*."""
    data_dir = tmp_path / "Skyrim" / "Data"
    data_dir.mkdir(parents=True)
    for nombre in en_data:
        (data_dir / nombre).write_bytes(b"TES4")
    return VfsVisibilityChecker(game_data_dir=data_dir, enabled_plugins=habilitados)


def test_perfil_con_mods_pero_data_pelado_es_critico(tmp_path: pathlib.Path) -> None:
    """El bug de U-01: el perfil activa mods, pero el Data del juego solo tiene
    los masters base — el tool va a correr contra el juego vainilla."""
    checker = _entorno(
        tmp_path,
        habilitados=(*_VANILLA, "SomeMod.esp", "OtroMod.esp"),
        en_data=_VANILLA,
    )

    scan = checker.check()

    assert scan.mod_plugins == ("SomeMod.esp", "OtroMod.esp")
    assert scan.visible == ()
    check = visibility_preflight_check(scan)
    assert check.status is PreflightStatus.RED
    assert check.name == "vfs_visibility"


def test_mods_visibles_en_data_pasa(tmp_path: pathlib.Path) -> None:
    """Contracara: con los mods materializados en Data, el sensor no molesta."""
    checker = _entorno(
        tmp_path,
        habilitados=(*_VANILLA, "SomeMod.esp", "OtroMod.esp"),
        en_data=(*_VANILLA, "SomeMod.esp", "OtroMod.esp"),
    )

    scan = checker.check()

    assert scan.visible == ("SomeMod.esp", "OtroMod.esp")
    assert visibility_preflight_check(scan).status is PreflightStatus.GREEN


def test_perfil_solo_con_masters_base_no_opina(tmp_path: pathlib.Path) -> None:
    """Sin plugins de mods no hay nada que afirmar: el sensor no inventa un
    hallazgo (un perfil vainilla es legítimo)."""
    checker = _entorno(tmp_path, habilitados=_VANILLA, en_data=_VANILLA)

    scan = checker.check()

    assert scan.mod_plugins == ()
    assert visibility_preflight_check(scan).status is PreflightStatus.GREEN


def test_contenido_creation_club_no_cuenta_como_mod(tmp_path: pathlib.Path) -> None:
    """El contenido de Creation Club (``cc*``) es oficial y vive en Data igual
    que los masters base. Contarlo como mod haría que un run standalone lo viera
    visible y el sensor mintiera VERDE — justo el falso negativo a evitar."""
    checker = _entorno(
        tmp_path,
        habilitados=(*_VANILLA, "ccBGSSSE001-Fish.esm", "SomeMod.esp"),
        en_data=(*_VANILLA, "ccBGSSSE001-Fish.esm"),
    )

    scan = checker.check()

    assert scan.mod_plugins == ("SomeMod.esp",)  # el cc* no cuenta
    assert scan.visible == ()
    assert visibility_preflight_check(scan).status is PreflightStatus.RED


def test_visibilidad_parcial_no_bloquea(tmp_path: pathlib.Path) -> None:
    """Decisión de diseño anclada: 1 de 3 visibles NO bloquea. Solo el caso
    inequívoco (cero visibles) prueba que la VFS no se heredó; frenar una
    materialización parcial rompería setups que funcionan."""
    checker = _entorno(
        tmp_path,
        habilitados=(*_VANILLA, "Uno.esp", "Dos.esp", "Tres.esp"),
        en_data=(*_VANILLA, "Uno.esp"),
    )

    scan = checker.check()

    assert scan.visible == ("Uno.esp",)
    assert visibility_preflight_check(scan).status is PreflightStatus.GREEN


def test_sin_data_resoluble_no_miente_verde(tmp_path: pathlib.Path) -> None:
    """Sin ``Data`` del juego no hay señal: el sensor lo dice, no afirma que
    todo esté bien (lección #250 — 'no configurado' ≠ verde verificado)."""
    checker = VfsVisibilityChecker(game_data_dir=None, enabled_plugins=("SomeMod.esp",))

    scan = checker.check()

    assert scan.configured is False
    check = visibility_preflight_check(scan)
    assert check.status is PreflightStatus.GREEN
    assert "no configurado" in check.summary.lower()


def test_el_detalle_nombra_los_plugins_invisibles(tmp_path: pathlib.Path) -> None:
    """El hallazgo tiene que ser accionable: el operador necesita saber QUÉ no
    se ve, no solo que algo falta."""
    checker = _entorno(
        tmp_path,
        habilitados=(*_VANILLA, "SomeMod.esp"),
        en_data=_VANILLA,
    )

    check = visibility_preflight_check(checker.check())

    assert any("SomeMod.esp" in d for d in check.details)


def test_scan_es_serializable_como_los_otros_sensores() -> None:
    """``VisibilityScan`` respeta la forma de ``OverwriteScan``: dataclass frozen
    de tuplas, para que el reporte del preflight siga siendo estable."""
    scan = VisibilityScan(configured=True, mod_plugins=("A.esp",), visible=())

    assert scan.mod_plugins == ("A.esp",)
    assert scan.visible == ()
