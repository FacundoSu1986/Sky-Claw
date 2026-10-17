"""Presets de la etapa 9 desatendida (#661 P5/P8): huella, snapshot durable y restore.

Casos medidos en P0-B: la herramienta reescribe su preset al iniciar (TexGen
sólo cambió ``OutputPath``), y el operador restauró byte-exacto por SHA. Acá ese
restore es del producto, durable y verificado.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib

import pytest

from sky_claw.local.tools.dyndolod_presets import (
    TAMANO_MAXIMO_DE_PRESET,
    GuardaDePresets,
    PresetError,
    huella_de_configuracion,
    leer_estado_de_preset,
    valor_de_clave,
)

PRESET_DYNDOLOD = (
    b"[DynDOLOD]\r\n"
    b"OutputPath=E:\\Sky-Claw T5 Rig\\PR2 Work\\DynDOLOD\\DynDOLOD\r\n"
    b"Preset=Medium\r\n"
    b"DoTreeLOD=1\r\n"
    b"Tamriel=1\r\n"
    b"DLC2SolstheimWorld=1\r\n"
    b"\r\n"
    b"[Skyrim LODGen]\r\n"
    b"Preset=NoEsLaDeCalidad\r\n"
)


def _sha(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


def test_la_huella_ignora_solo_el_output_path():
    otro_output = PRESET_DYNDOLOD.replace(b"PR2 Work", b"Otro Lugar")
    assert huella_de_configuracion(PRESET_DYNDOLOD) == huella_de_configuracion(otro_output)
    # Cambiar un worldspace SÍ cambia la huella.
    sin_solstheim = PRESET_DYNDOLOD.replace(b"DLC2SolstheimWorld=1", b"DLC2SolstheimWorld=0")
    assert huella_de_configuracion(PRESET_DYNDOLOD) != huella_de_configuracion(sin_solstheim)
    # La clave se reconoce sin mayúsculas ni espacios.
    variante = PRESET_DYNDOLOD.replace(b"OutputPath=", b"  outputpath =")
    assert huella_de_configuracion(variante) == huella_de_configuracion(PRESET_DYNDOLOD)


def test_el_preset_de_calidad_se_lee_de_su_seccion():
    assert valor_de_clave(PRESET_DYNDOLOD, seccion="DynDOLOD", clave="Preset") == "Medium"
    assert valor_de_clave(PRESET_DYNDOLOD, seccion="dyndolod", clave="preset") == "Medium"
    assert valor_de_clave(PRESET_DYNDOLOD, seccion="TexGen", clave="Preset") is None


def test_una_clave_duplicada_en_la_seccion_es_ambigua():
    duplicado = PRESET_DYNDOLOD.replace(b"Preset=Medium\r\n", b"Preset=Medium\r\nPreset=High\r\n")
    assert valor_de_clave(duplicado, seccion="DynDOLOD", clave="Preset") is None


def test_leer_estado_presente_y_ausente(tmp_path):
    ruta = tmp_path / "DynDOLOD_SSE_Default.ini"
    assert leer_estado_de_preset(ruta, seccion="DynDOLOD", clave_de_calidad="Preset").presente is False
    ruta.write_bytes(PRESET_DYNDOLOD)
    estado = leer_estado_de_preset(ruta, seccion="DynDOLOD", clave_de_calidad="Preset")
    assert estado.presente
    assert estado.sha256 == _sha(PRESET_DYNDOLOD)
    assert estado.huella == huella_de_configuracion(PRESET_DYNDOLOD)
    assert estado.preset_de_calidad == "Medium"
    assert estado.tamano == len(PRESET_DYNDOLOD)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="sin symlinks")
def test_un_preset_que_es_enlace_se_rechaza(tmp_path):
    real = tmp_path / "real.ini"
    real.write_bytes(PRESET_DYNDOLOD)
    enlace = tmp_path / "DynDOLOD_SSE_Default.ini"
    try:
        enlace.symlink_to(real)
    except OSError:
        pytest.skip("el entorno no permite crear symlinks")
    with pytest.raises(PresetError):
        leer_estado_de_preset(enlace, seccion="DynDOLOD", clave_de_calidad="Preset")
    with pytest.raises(PresetError):
        GuardaDePresets(tmp_path / "estado").proteger(enlace)


def test_un_preset_gigante_se_rechaza(tmp_path):
    ruta = tmp_path / "enorme.ini"
    ruta.write_bytes(b"x" * (TAMANO_MAXIMO_DE_PRESET + 1))
    with pytest.raises(PresetError):
        leer_estado_de_preset(ruta, seccion="DynDOLOD", clave_de_calidad=None)


def test_ci_19_restore_exacto_tras_la_reescritura_de_la_herramienta(tmp_path):
    ruta = tmp_path / "Presets" / "DynDOLOD_SSE_TexGen.ini"
    ruta.parent.mkdir()
    ruta.write_bytes(PRESET_DYNDOLOD)
    guarda = GuardaDePresets(tmp_path / "estado")
    registro = guarda.proteger(ruta)
    # La herramienta reescribe su OutputPath al iniciar (medido en P0-B).
    ruta.write_bytes(PRESET_DYNDOLOD.replace(b"PR2 Work", b"SkyClawWork"))
    guarda.restaurar(registro)
    assert ruta.read_bytes() == PRESET_DYNDOLOD
    assert guarda.pendientes() == []


def test_ci_19_absent_antes_queda_absent_despues(tmp_path):
    ruta = tmp_path / "Presets" / "DynDOLOD_SSE_TexGen.ini"
    ruta.parent.mkdir()
    guarda = GuardaDePresets(tmp_path / "estado")
    registro = guarda.proteger(ruta)
    assert registro.presente is False
    ruta.write_bytes(b"[TexGen]\r\nOutputPath=X\r\n")
    guarda.restaurar(registro)
    assert not ruta.exists()
    assert guarda.pendientes() == []


def test_el_snapshot_es_durable_y_se_reconcilia_tras_una_muerte(tmp_path):
    ruta = tmp_path / "DynDOLOD_SSE_Default.ini"
    ruta.write_bytes(PRESET_DYNDOLOD)
    GuardaDePresets(tmp_path / "estado").proteger(ruta)
    ruta.write_bytes(b"pisado por la corrida que murio")
    # Otro proceso (el próximo arranque) reconstruye la guarda desde disco.
    nueva = GuardaDePresets(tmp_path / "estado")
    assert [registro.ruta for registro in nueva.pendientes()] == [ruta]
    assert nueva.reconciliar() == [ruta]
    assert ruta.read_bytes() == PRESET_DYNDOLOD
    assert nueva.pendientes() == []


def test_no_se_protege_dos_veces_sin_reconciliar(tmp_path):
    ruta = tmp_path / "DynDOLOD_SSE_Default.ini"
    ruta.write_bytes(PRESET_DYNDOLOD)
    guarda = GuardaDePresets(tmp_path / "estado")
    guarda.proteger(ruta)
    with pytest.raises(PresetError):
        guarda.proteger(ruta)


def test_un_snapshot_corrupto_no_se_aplica(tmp_path):
    ruta = tmp_path / "DynDOLOD_SSE_Default.ini"
    ruta.write_bytes(PRESET_DYNDOLOD)
    guarda = GuardaDePresets(tmp_path / "estado")
    registro = guarda.proteger(ruta)
    (registro.directorio / "contenido.bin").write_bytes(b"alterado")
    ruta.write_bytes(b"estado de la herramienta")
    with pytest.raises(PresetError):
        guarda.restaurar(registro)
    # Fail-closed: el preset no se pisa con bytes que no prueban su SHA, y el
    # registro queda para diagnóstico.
    assert ruta.read_bytes() == b"estado de la herramienta"
    assert registro.directorio.exists()


@pytest.mark.parametrize(
    "registro",
    [
        {"version": 2, "ruta": "x", "presente": False, "sha256": None, "tamano": None},
        {"version": 1, "ruta": "x", "presente": True, "sha256": None, "tamano": None},
        {"version": 1, "ruta": "x", "presente": False, "sha256": "ab", "tamano": None},
        {"version": 1, "ruta": "x", "presente": False, "sha256": None, "tamano": None, "extra": 1},
    ],
    ids=["version", "present_sin_sha", "absent_con_sha", "campo_extra"],
)
def test_un_registro_fuera_de_schema_es_error(tmp_path, registro):
    directorio = tmp_path / "estado" / "abc"
    directorio.mkdir(parents=True)
    (directorio / "registro.json").write_text(json.dumps(registro), encoding="utf-8")
    with pytest.raises(PresetError):
        GuardaDePresets(tmp_path / "estado").pendientes()


def test_restaurar_no_reescribe_si_ya_esta_igual(tmp_path):
    ruta = tmp_path / "DynDOLOD_SSE_Default.ini"
    ruta.write_bytes(PRESET_DYNDOLOD)
    guarda = GuardaDePresets(tmp_path / "estado")
    registro = guarda.proteger(ruta)
    antes = ruta.stat().st_mtime_ns
    guarda.restaurar(registro)
    assert ruta.stat().st_mtime_ns == antes


def test_sin_directorio_de_estado_no_hay_pendientes(tmp_path):
    assert GuardaDePresets(tmp_path / "no-existe").pendientes() == []


def test_un_preset_que_es_directorio_se_rechaza(tmp_path):
    ruta: pathlib.Path = tmp_path / "DynDOLOD_SSE_Default.ini"
    ruta.mkdir()
    with pytest.raises(PresetError):
        leer_estado_de_preset(ruta, seccion="DynDOLOD", clave_de_calidad=None)
