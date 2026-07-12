"""Tests del ``IniEditor`` (PR-1 del plan de grass cache, Stage 8 del SOP).

El editor de INIs es la pieza fundacional de la automatización de Pre-Cache
Grass: escribe ``GrassControl.ini`` (sintaxis plana de NGIO-NG, sin secciones),
``Skyrim.ini``/``SkyrimPrefs.ini`` (secciones clásicas ``[Grass]``) y
``SSEDisplayTweaks.ini``. La disciplina es la misma que ``profile_sandbox``:
**byte-fidelidad** — BOM UTF-8 y CRLF intactos, las líneas no tocadas quedan
byte-idénticas, y solo cambia la línea editada.

Anclas del contrato:
- ``get`` nunca muta el archivo.
- ``set`` reemplaza el valor preservando el spelling original de la clave y el
  espaciado alrededor del ``=``; crea la clave/sección si faltan.
- ``set`` idempotente (mismo valor) no reescribe ni genera backup.
- Antes de cada escritura real se crea ``<archivo>.bak`` con los bytes previos
  (escritura atómica tmp → replace, igual que ``_write_modlist_atomic``).
- Los valores son strings verbatim: ``20272.0000`` no se "normaliza".
- Líneas comentadas (``;`` / ``#``) jamás matchean como claves.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.mo2.ini_editor import IniEditor, IniEditResult

# Contenidos byte-exactos, como los escribe el juego/MO2 en Windows.
_SKYRIM_INI = (
    b"\xef\xbb\xbf[General]\r\n"
    b"sLanguage=ENGLISH\r\n"
    b"\r\n"
    b"[Grass]\r\n"
    b"bAllowCreateGrass=1\r\n"
    b"fGrassStartFadeDistance=7000.0000\r\n"
)

# GrassControl.ini de NGIO-NG: sintaxis plana Key = Value, sin secciones, LF.
_GRASS_CONTROL = (
    b"; NGIO-NG configuration\n"
    b"Use-grass-cache = False\n"
    b"Extend-grass-distance = True\n"
    b"#Only-load-from-cache = True\n"
    b"Only-load-from-cache = False\n"
)


@pytest.fixture
def skyrim_ini(tmp_path: pathlib.Path) -> pathlib.Path:
    ini = tmp_path / "Skyrim.ini"
    ini.write_bytes(_SKYRIM_INI)
    return ini


@pytest.fixture
def grass_control(tmp_path: pathlib.Path) -> pathlib.Path:
    ini = tmp_path / "GrassControl.ini"
    ini.write_bytes(_GRASS_CONTROL)
    return ini


@pytest.fixture
def editor() -> IniEditor:
    return IniEditor()


# ---------------------------------------------------------------------------
# get: lectura sin mutación
# ---------------------------------------------------------------------------


async def test_get_lee_valor_de_seccion_clasica(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    valor = await editor.get(skyrim_ini, "fGrassStartFadeDistance", section="Grass")
    assert valor == "7000.0000"


async def test_get_lee_valor_de_sintaxis_plana_ngio(editor: IniEditor, grass_control: pathlib.Path) -> None:
    valor = await editor.get(grass_control, "Use-grass-cache")
    assert valor == "False"


async def test_get_no_muta_el_archivo(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    await editor.get(skyrim_ini, "fGrassStartFadeDistance", section="Grass")
    assert skyrim_ini.read_bytes() == _SKYRIM_INI


async def test_get_clave_inexistente_devuelve_none(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    assert await editor.get(skyrim_ini, "fGrassMaxStartFadeDistance", section="Grass") is None


async def test_get_es_case_insensitive_en_clave_y_seccion(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    # Semántica INI de Windows: clave y sección matchean sin distinguir mayúsculas.
    valor = await editor.get(skyrim_ini, "FGRASSSTARTFADEDISTANCE", section="grass")
    assert valor == "7000.0000"


async def test_get_ignora_lineas_comentadas(editor: IniEditor, grass_control: pathlib.Path) -> None:
    # La línea "#Only-load-from-cache = True" NO debe matchear: gana la real.
    valor = await editor.get(grass_control, "Only-load-from-cache")
    assert valor == "False"


async def test_get_archivo_inexistente_lanza(editor: IniEditor, tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError):
        await editor.get(tmp_path / "no_existe.ini", "Clave")


# ---------------------------------------------------------------------------
# set: reemplazo byte-fiel
# ---------------------------------------------------------------------------


async def test_set_reemplaza_valor_preservando_bom_y_crlf(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    resultado = await editor.set(skyrim_ini, "fGrassStartFadeDistance", "20272.0000", section="Grass")

    assert isinstance(resultado, IniEditResult)
    assert resultado.changed is True
    assert resultado.previous_value == "7000.0000"
    # Solo cambia la línea editada; BOM, CRLF y el resto quedan byte-idénticos.
    esperado = _SKYRIM_INI.replace(
        b"fGrassStartFadeDistance=7000.0000\r\n",
        b"fGrassStartFadeDistance=20272.0000\r\n",
    )
    assert skyrim_ini.read_bytes() == esperado


async def test_set_preserva_espaciado_de_sintaxis_plana(editor: IniEditor, grass_control: pathlib.Path) -> None:
    # NGIO-NG usa "Key = Value": el espaciado alrededor del = se conserva.
    await editor.set(grass_control, "Use-grass-cache", "True")

    esperado = _GRASS_CONTROL.replace(
        b"Use-grass-cache = False\n",
        b"Use-grass-cache = True\n",
    )
    assert grass_control.read_bytes() == esperado


async def test_set_no_matchea_lineas_comentadas(editor: IniEditor, grass_control: pathlib.Path) -> None:
    await editor.set(grass_control, "Only-load-from-cache", "True")

    contenido = grass_control.read_bytes()
    # La línea comentada queda intacta; la real es la que cambia.
    assert b"#Only-load-from-cache = True\n" in contenido
    assert b"Only-load-from-cache = True\n" in contenido.replace(b"#Only-load-from-cache = True\n", b"")


async def test_set_valor_float_se_escribe_verbatim(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    # El API es de strings: "20272.0000" no se normaliza a "20272.0".
    await editor.set(skyrim_ini, "fGrassStartFadeDistance", "20272.0000", section="Grass")
    assert b"fGrassStartFadeDistance=20272.0000\r\n" in skyrim_ini.read_bytes()


async def test_set_idempotente_no_reescribe(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    resultado = await editor.set(skyrim_ini, "fGrassStartFadeDistance", "7000.0000", section="Grass")

    assert resultado.changed is False
    assert resultado.previous_value == "7000.0000"
    assert resultado.backup_path is None
    assert skyrim_ini.read_bytes() == _SKYRIM_INI
    assert not skyrim_ini.with_suffix(".ini.bak").exists()


async def test_set_preserva_spelling_original_de_la_clave(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    # Match case-insensitive, pero el spelling que queda es el del archivo.
    await editor.set(skyrim_ini, "fgrassstartfadedistance", "1.0", section="GRASS")
    assert b"fGrassStartFadeDistance=1.0\r\n" in skyrim_ini.read_bytes()


# ---------------------------------------------------------------------------
# set: creación de claves, secciones y archivos
# ---------------------------------------------------------------------------


async def test_set_agrega_clave_faltante_dentro_de_su_seccion(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    resultado = await editor.set(skyrim_ini, "fGrassMaxStartFadeDistance", "20272.0000", section="Grass")

    assert resultado.changed is True
    assert resultado.previous_value is None
    contenido = skyrim_ini.read_bytes()
    # La clave nueva cae DENTRO de [Grass] (después de sus claves, con CRLF),
    # no al final del archivo ni en [General].
    assert b"fGrassStartFadeDistance=7000.0000\r\nfGrassMaxStartFadeDistance=20272.0000\r\n" in contenido


async def test_set_crea_seccion_faltante_al_final(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    await editor.set(skyrim_ini, "bBorderRegionsEnabled", "0", section="Display")

    contenido = skyrim_ini.read_bytes()
    assert contenido.endswith(b"[Display]\r\nbBorderRegionsEnabled=0\r\n")
    # Lo previo quedó intacto.
    assert contenido.startswith(_SKYRIM_INI)


async def test_set_agrega_clave_plana_al_final(editor: IniEditor, grass_control: pathlib.Path) -> None:
    await editor.set(grass_control, "DynDOLOD-Grass-Mode", "1")

    contenido = grass_control.read_bytes()
    assert contenido.startswith(_GRASS_CONTROL)
    # Archivo LF: la línea nueva usa el EOL del archivo, no CRLF.
    assert contenido.endswith(b"DynDOLOD-Grass-Mode = 1\n")


async def test_set_crea_archivo_nuevo_sin_bom_con_crlf(editor: IniEditor, tmp_path: pathlib.Path) -> None:
    # El mod de config "SkyClaw - Grass Precache Config" genera GrassControl.ini
    # desde cero: sin BOM (NGIO no lo espera) y CRLF (convención Windows).
    nuevo = tmp_path / "GrassControl.ini"

    resultado = await editor.set(nuevo, "Use-grass-cache", "True")

    assert resultado.changed is True
    assert resultado.previous_value is None
    assert nuevo.read_bytes() == b"Use-grass-cache = True\r\n"


# ---------------------------------------------------------------------------
# Backup atómico
# ---------------------------------------------------------------------------


async def test_set_crea_backup_con_los_bytes_previos(editor: IniEditor, skyrim_ini: pathlib.Path) -> None:
    resultado = await editor.set(skyrim_ini, "fGrassStartFadeDistance", "20272.0000", section="Grass")

    assert resultado.backup_path is not None
    assert resultado.backup_path.read_bytes() == _SKYRIM_INI


async def test_backup_refleja_el_estado_previo_a_la_ultima_escritura(
    editor: IniEditor, grass_control: pathlib.Path
) -> None:
    await editor.set(grass_control, "Use-grass-cache", "True")
    intermedio = grass_control.read_bytes()
    await editor.set(grass_control, "Extend-grass-distance", "False")

    backup = grass_control.with_suffix(".ini.bak")
    assert backup.read_bytes() == intermedio
