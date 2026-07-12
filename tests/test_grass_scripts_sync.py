"""Sincronización Python ↔ Pascal de los scripts de grass (patrón T-08).

Los ``.pas`` no pueden importar Python, así que la fuente única se ancla al
revés (igual que ``test_conflict_signatures_sync.py``): estos tests parsean
los scripts bundleados y fallan ante cualquier divergencia con las constantes
de ``grass_analyzer.py`` — prefijos de línea y claves del SUMMARY que los
parsers esperan.

Además anclan que ambos scripts sean READ-ONLY: el diagnóstico de grass jamás
muta plugins (Stage 8 del SOP corre ANTES del precache; una escritura acá
invalidaría el load order estabilizado por LOOT).
"""

from __future__ import annotations

import pathlib

import sky_claw.local.xedit
from sky_claw.local.xedit.grass_analyzer import (
    SCRIPT_WORLDSPACES,
    SCRIPT_ZERO_BOUNDS,
    WSGRASS_PREFIX,
    ZEROBOUND_PREFIX,
)

_SCRIPTS_DIR = pathlib.Path(sky_claw.local.xedit.__file__).parent / "scripts"

#: Tokens del API de escritura de xEdit que un script read-only jamás usa.
_TOKENS_DE_ESCRITURA = (
    "SetElementEditValues",
    "SetElementNativeValues",
    "SetEditValue",
    "SetNativeValue",
    "ElementAssign",
    "wbCopyElementToRecord",
    "AddNewFile",
    "AddElement",
    "RemoveElement",
    "RemoveNode",
    "CleanMasters",
    "uses mteFunctions",
)


def _script(nombre: str) -> str:
    ruta = _SCRIPTS_DIR / nombre
    assert ruta.exists(), f"Script bundleado faltante: {ruta}"
    return ruta.read_text(encoding="utf-8")


def test_prefijos_y_summary_sincronizados_con_worldspaces() -> None:
    script = _script(SCRIPT_WORLDSPACES)

    assert f"'{WSGRASS_PREFIX}'" in script, "El script no emite líneas con el prefijo WSGRASS|"
    for clave in ("grass_worldspaces=", "land_scanned=", "ltex_grass="):
        assert clave in script, f"Falta la clave de SUMMARY '{clave}' en {SCRIPT_WORLDSPACES}"


def test_prefijos_y_summary_sincronizados_con_zero_bounds() -> None:
    script = _script(SCRIPT_ZERO_BOUNDS)

    assert f"'{ZEROBOUND_PREFIX}'" in script, "El script no emite líneas con el prefijo ZEROBOUND|"
    for clave in ("total_gras=", "zero_bounds="):
        assert clave in script, f"Falta la clave de SUMMARY '{clave}' en {SCRIPT_ZERO_BOUNDS}"
    # Las dos razones que el parser acepta (honestidad tipo SPIT: OBND ausente
    # no es lo mismo que OBND en cero).
    assert "'zeros'" in script
    assert "'missing'" in script


def test_scripts_de_grass_son_read_only() -> None:
    for nombre in (SCRIPT_WORLDSPACES, SCRIPT_ZERO_BOUNDS):
        script = _script(nombre)
        for token in _TOKENS_DE_ESCRITURA:
            assert token not in script, f"{nombre} contiene el token de escritura {token!r}"
