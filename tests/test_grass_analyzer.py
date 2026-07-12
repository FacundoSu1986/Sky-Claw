"""Tests del ``GrassAnalyzer`` (PR-2 del plan grass cache, Stage 8 del SOP).

Capa de diagnóstico xEdit del pipeline de No Grass In Objects: dos scripts
Pascal read-only (``list_grass_worldspaces.pas`` y ``list_zero_bound_grass.pas``)
emiten líneas pipe-delimited que estos parsers consumen — mismo contrato que
``list_all_conflicts.pas`` → ``ConflictAnalyzer``.

Anclas del contrato:
- Los parsers son funciones puras; toleran el ruido real del log de xEdit
  (líneas ``[HH:MM] Processing: ...`` del loader) y un prefijo de timestamp
  opcional en las líneas propias; las líneas malformadas se saltean.
- El analyzer honra ``result.success`` (lección review Codex #226: xEdit con
  exit != 0 jamás se reporta como "sin hallazgos") y exige SUMMARY consistente
  (fail-closed: un ``OnlyPregenerateWorldSpaces`` incompleto saltearía
  worldspaces en silencio durante el precache).
- Cero hallazgos con SUMMARY consistente = éxito (reporte vacío, no excepción).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.xedit.conflict_analyzer import parse_summary_line
from sky_claw.local.xedit.grass_analyzer import (
    SCRIPT_WORLDSPACES,
    SCRIPT_ZERO_BOUNDS,
    WSGRASS_PREFIX,
    ZEROBOUND_PREFIX,
    GrassAnalyzer,
    parse_worldspace_lines,
    parse_zero_bound_lines,
)
from sky_claw.local.xedit.output_parser import XEditResult

# ---------------------------------------------------------------------------
# Fixtures de salida de xEdit (con el ruido real del loader)
# ---------------------------------------------------------------------------

SALIDA_UN_WORLDSPACE = """\
[00:01] Processing: Skyrim.esm
WSGRASS|0000003C|Tamriel|Skyrim.esm
SUMMARY|grass_worldspaces=1|land_scanned=4321|ltex_grass=17
"""

SALIDA_TRES_WORLDSPACES = """\
[00:00] Background Loader: starting
[00:01] Processing: Skyrim.esm
[00:01] Processing: Dragonborn.esm
[00:02] Background Loader: finished
WSGRASS|0000003C|Tamriel|Skyrim.esm
[00:05] cache hit for LTEX 00000C16
WSGRASS|0001A26F|DLC2SolstheimWorld|Dragonborn.esm
WSGRASS|00016BB4|FalkreathWorld|Skyrim.esm
SUMMARY|grass_worldspaces=3|land_scanned=9999|ltex_grass=42
"""

SALIDA_ZERO_BOUNDS = """\
[00:01] Processing: BrokenGrass.esp
ZEROBOUND|0101A001|BrokenTundraGrass|BrokenGrass.esp|BrokenGrass.esp|zeros
ZEROBOUND|0101A002|GrassSinBounds|Fix.esp|BrokenGrass.esp|missing
SUMMARY|total_gras=250|zero_bounds=2
"""

SALIDA_ZERO_BOUNDS_LIMPIA = """\
[00:01] Processing: Skyrim.esm
SUMMARY|total_gras=250|zero_bounds=0
"""


def _runner(stdout: str, return_code: int = 0, errors: list[str] | None = None, stderr: str = "") -> MagicMock:
    """Mock del XEditRunner con staging y run_script trackeados en orden.

    ``attach_mock`` (y no asignación directa) para que ambas llamadas queden
    registradas en ``runner.mock_calls`` y se pueda asertar el ORDEN.
    """
    runner = MagicMock()
    runner.attach_mock(AsyncMock(return_value=[]), "ensure_scripts_staged")
    runner.attach_mock(
        AsyncMock(
            return_value=XEditResult(
                return_code=return_code,
                raw_stdout=stdout,
                raw_stderr=stderr,
                errors=errors or [],
            )
        ),
        "run_script",
    )
    return runner


# ---------------------------------------------------------------------------
# Fase A — parsers puros
# ---------------------------------------------------------------------------


def test_parsea_un_worldspace() -> None:
    resultado = parse_worldspace_lines(SALIDA_UN_WORLDSPACE)

    assert len(resultado) == 1
    ws = resultado[0]
    assert ws.form_id == "0000003C"
    assert ws.editor_id == "Tamriel"
    assert ws.plugin == "Skyrim.esm"


def test_parsea_tres_worldspaces_con_ruido_de_log() -> None:
    resultado = parse_worldspace_lines(SALIDA_TRES_WORLDSPACES)

    assert [ws.editor_id for ws in resultado] == ["Tamriel", "DLC2SolstheimWorld", "FalkreathWorld"]


def test_parsea_cincuenta_worldspaces() -> None:
    lineas = "\n".join(f"WSGRASS|{i:08X}|World{i}|Mod{i}.esp" for i in range(50))
    salida = f"{lineas}\nSUMMARY|grass_worldspaces=50|land_scanned=1|ltex_grass=1\n"

    resultado = parse_worldspace_lines(salida)

    assert len(resultado) == 50
    # El orden del script se preserva (determinismo del Finalize ordenado).
    assert resultado[0].editor_id == "World0"
    assert resultado[49].editor_id == "World49"


def test_salida_vacia_devuelve_lista_vacia() -> None:
    assert parse_worldspace_lines("") == []
    assert parse_worldspace_lines("[00:01] Processing: Skyrim.esm\nsolo ruido\n") == []


def test_lineas_malformadas_se_saltean() -> None:
    salida = (
        "WSGRASS|0000003C|Tamriel\n"  # 3 campos: de menos
        "WSGRASS|0000003C|Tamriel|Skyrim.esm|extra\n"  # 5 campos: corrupción (review #259)
        "WSGRASS|XYZNOHEX|Tamriel|Skyrim.esm\n"  # FormID no-hex
        "WSGRASS|0001A26F|DLC2SolstheimWorld|Dragonborn.esm\n"  # válida
    )

    resultado = parse_worldspace_lines(salida)

    assert len(resultado) == 1
    assert resultado[0].form_id == "0001A26F"


def test_editor_id_con_espacios_y_unicode() -> None:
    salida = "WSGRASS|0001A26F|Sölstheim World|Mod Ñandú.esp\n"

    resultado = parse_worldspace_lines(salida)

    assert resultado[0].editor_id == "Sölstheim World"
    assert resultado[0].plugin == "Mod Ñandú.esp"


def test_editor_id_vacio_se_acepta() -> None:
    # Un WRLD sin EDID es raro pero legal: el parser no lo descarta.
    resultado = parse_worldspace_lines("WSGRASS|0000003C||Skyrim.esm\n")

    assert len(resultado) == 1
    assert resultado[0].editor_id == ""


def test_prefijo_de_timestamp_se_tolera() -> None:
    # Algunos builds de xEdit prefijan las AddMessage con [HH:MM] o [HH:MM:SS].
    salida = "[00:12] WSGRASS|0000003C|Tamriel|Skyrim.esm\n[0:12:59] WSGRASS|00016BB4|FalkreathWorld|Skyrim.esm\n"

    resultado = parse_worldspace_lines(salida)

    assert [ws.editor_id for ws in resultado] == ["Tamriel", "FalkreathWorld"]


def test_parsea_zero_bounds() -> None:
    resultado = parse_zero_bound_lines(SALIDA_ZERO_BOUNDS)

    assert len(resultado) == 2
    zeros, missing = resultado
    assert zeros.form_id == "0101A001"
    assert zeros.editor_id == "BrokenTundraGrass"
    assert zeros.winner_plugin == "BrokenGrass.esp"
    assert zeros.source_plugin == "BrokenGrass.esp"
    assert zeros.reason == "zeros"
    assert missing.reason == "missing"
    assert missing.winner_plugin == "Fix.esp"


def test_zero_bound_reason_invalido_se_saltea() -> None:
    salida = (
        "ZEROBOUND|0101A001|X|A.esp|A.esp|banana\n"  # reason inválido
        "ZEROBOUND|0101A002|Y|B.esp|B.esp|zeros\n"  # válida
    )

    resultado = parse_zero_bound_lines(salida)

    assert len(resultado) == 1
    assert resultado[0].form_id == "0101A002"


def test_summary_line_reutilizado_lee_claves_grass() -> None:
    # parse_summary_line (conflict_analyzer) es genérico: la fuente única
    # también sirve para las claves de grass.
    assert parse_summary_line(SALIDA_UN_WORLDSPACE) == {
        "grass_worldspaces": 1,
        "land_scanned": 4321,
        "ltex_grass": 17,
    }


# ---------------------------------------------------------------------------
# Fase B — GrassAnalyzer (runner mockeado)
# ---------------------------------------------------------------------------


async def test_list_grass_worldspaces_feliz() -> None:
    # Dos worldspaces comparten EditorID (patológico pero posible) y uno no
    # tiene EDID: editor_ids dedupea y excluye vacíos, preservando el orden.
    salida = (
        "WSGRASS|0000003C|Tamriel|Skyrim.esm\n"
        "WSGRASS|0001A26F|DLC2SolstheimWorld|Dragonborn.esm\n"
        "WSGRASS|0501B00F|Tamriel|Clon.esp\n"
        "WSGRASS|0501B010||SinEdid.esp\n"
        "SUMMARY|grass_worldspaces=4|land_scanned=100|ltex_grass=5\n"
    )
    runner = _runner(salida)

    reporte = await GrassAnalyzer().list_grass_worldspaces(["Skyrim.esm", "Dragonborn.esm"], runner)

    assert len(reporte.worldspaces) == 4
    assert reporte.summary["land_scanned"] == 100
    assert reporte.editor_ids == ["Tamriel", "DLC2SolstheimWorld"]
    runner.run_script.assert_awaited_once_with(
        SCRIPT_WORLDSPACES,
        ["Skyrim.esm", "Dragonborn.esm"],
        timeout=None,
    )


async def test_fallo_de_xedit_lanza_en_vez_de_lista_vacia() -> None:
    """Exit != 0 no debe parecer "sin worldspaces": el precache saltearía todo (lección #226)."""
    runner = _runner("", return_code=1, stderr="Fatal: could not load master")

    with pytest.raises(RuntimeError, match="xEdit falló"):
        await GrassAnalyzer().list_grass_worldspaces(["Skyrim.esm"], runner)


async def test_errores_parseados_tambien_lanzan() -> None:
    # success es False también con exit 0 si el parser detectó errores.
    runner = _runner(SALIDA_UN_WORLDSPACE, errors=["Error: script terminated"])

    with pytest.raises(RuntimeError, match="xEdit falló"):
        await GrassAnalyzer().list_grass_worldspaces(["Skyrim.esm"], runner)


async def test_summary_faltante_lanza_salida_truncada() -> None:
    # Sin SUMMARY el script no llegó a Finalize: exit 0 no garantiza scan completo.
    runner = _runner("WSGRASS|0000003C|Tamriel|Skyrim.esm\n")

    with pytest.raises(RuntimeError, match="truncada"):
        await GrassAnalyzer().list_grass_worldspaces(["Skyrim.esm"], runner)


async def test_conteo_inconsistente_con_summary_lanza() -> None:
    # 2 líneas parseadas vs 5 declaradas: stdout corrupto/entrelazado.
    salida = (
        "WSGRASS|0000003C|Tamriel|Skyrim.esm\n"
        "WSGRASS|0001A26F|DLC2SolstheimWorld|Dragonborn.esm\n"
        "SUMMARY|grass_worldspaces=5|land_scanned=1|ltex_grass=1\n"
    )
    runner = _runner(salida)

    with pytest.raises(RuntimeError, match="inconsistente"):
        await GrassAnalyzer().list_grass_worldspaces(["Skyrim.esm"], runner)


async def test_zero_bounds_cero_hallazgos_es_exito() -> None:
    runner = _runner(SALIDA_ZERO_BOUNDS_LIMPIA)

    reporte = await GrassAnalyzer().detect_zero_bound_grass(["Skyrim.esm"], runner)

    assert reporte.findings == []
    assert reporte.has_findings is False
    assert reporte.summary == {"total_gras": 250, "zero_bounds": 0}


async def test_zero_bounds_n_hallazgos() -> None:
    runner = _runner(SALIDA_ZERO_BOUNDS)

    reporte = await GrassAnalyzer().detect_zero_bound_grass(["BrokenGrass.esp"], runner)

    assert reporte.has_findings is True
    assert [f.reason for f in reporte.findings] == ["zeros", "missing"]
    runner.run_script.assert_awaited_once_with(SCRIPT_ZERO_BOUNDS, ["BrokenGrass.esp"], timeout=None)


async def test_analyzer_stagea_scripts_antes_de_correr() -> None:
    # El staging cierra el gap "el .pas no está en Edit Scripts" en el camino
    # caliente: debe ocurrir ANTES de lanzar xEdit.
    runner = _runner(SALIDA_UN_WORLDSPACE)

    await GrassAnalyzer().list_grass_worldspaces(["Skyrim.esm"], runner)

    nombres = [nombre for nombre, _args, _kwargs in runner.mock_calls]
    assert nombres.index("ensure_scripts_staged") < nombres.index("run_script")
    runner.ensure_scripts_staged.assert_awaited_once_with([SCRIPT_WORLDSPACES])


async def test_to_dict_es_json_serializable() -> None:
    reporte_ws = await GrassAnalyzer().list_grass_worldspaces(["Skyrim.esm"], _runner(SALIDA_UN_WORLDSPACE))
    reporte_zb = await GrassAnalyzer().detect_zero_bound_grass(["Skyrim.esm"], _runner(SALIDA_ZERO_BOUNDS))

    assert json.loads(json.dumps(reporte_ws.to_dict()))["worldspaces"][0]["editor_id"] == "Tamriel"
    assert json.loads(json.dumps(reporte_zb.to_dict()))["findings"][0]["reason"] == "zeros"


async def test_timeout_por_llamada_se_propaga_al_runner() -> None:
    # El scan de LAND sobre un load order real tarda más que los 120s default.
    runner = _runner(SALIDA_UN_WORLDSPACE)

    await GrassAnalyzer().list_grass_worldspaces(["Skyrim.esm"], runner, timeout=1800)

    runner.run_script.assert_awaited_once_with(SCRIPT_WORLDSPACES, ["Skyrim.esm"], timeout=1800)


def test_constantes_de_prefijo_y_script() -> None:
    # Ancladas también por tests/test_grass_scripts_sync.py contra los .pas.
    assert WSGRASS_PREFIX == "WSGRASS|"
    assert ZEROBOUND_PREFIX == "ZEROBOUND|"
    assert SCRIPT_WORLDSPACES == "list_grass_worldspaces.pas"
    assert SCRIPT_ZERO_BOUNDS == "list_zero_bound_grass.pas"
