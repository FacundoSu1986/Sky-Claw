"""Sincronización de firmas críticas Python ↔ Pascal (T-08 de TECHNICAL_REVIEW_TASKS.md).

``list_all_conflicts.pas`` clasifica severidad dentro de xEdit con sets de
firmas duplicados a mano desde ``ConflictAnalyzer`` — y ya habían driftado:
usaba la firma de scripts de Oblivion (inexistente en Skyrim SE) y omitía
INFO/SCEN. El Pascal no puede importar Python, así que la "fuente única" se
ancla al revés: este test parsea el script y falla ante cualquier divergencia
con ``DEFAULT_CRITICAL_TYPES``/``DEFAULT_WARNING_TYPES``.
"""

import pathlib
import re

import sky_claw.local.xedit
from sky_claw.local.xedit.conflict_analyzer import (
    DEFAULT_CRITICAL_TYPES,
    DEFAULT_WARNING_TYPES,
)

RUTA_SCRIPT = pathlib.Path(sky_claw.local.xedit.__file__).parent / "scripts" / "list_all_conflicts.pas"

_FIRMA_RE = re.compile(r"sig = '([A-Z_0-9]{4})'")


def _firmas_de_funcion(nombre: str) -> frozenset[str]:
    """Extrae las firmas comparadas dentro de la función Pascal *nombre*."""
    script = RUTA_SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"function {nombre}.*?end;", script, flags=re.DOTALL)
    assert match is not None, f"No se encontró la función {nombre} en {RUTA_SCRIPT.name}"
    return frozenset(_FIRMA_RE.findall(match.group(0)))


def test_firmas_criticas_sincronizadas_con_el_analyzer() -> None:
    assert _firmas_de_funcion("IsCriticalType") == DEFAULT_CRITICAL_TYPES


def test_firmas_warning_sincronizadas_con_el_analyzer() -> None:
    assert _firmas_de_funcion("IsWarningType") == DEFAULT_WARNING_TYPES
