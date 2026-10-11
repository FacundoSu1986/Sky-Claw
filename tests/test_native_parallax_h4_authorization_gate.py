"""Invariantes documentales del cierre de autorización H4 `resize_normal`.

Congela la **clase** de defecto, no un caso: el documento de cierre de autorización no
puede publicar una autorización que los contratos no sostienen. Un cierre que declarara
`H4_CODE_IMPLEMENTATION_AUTHORIZED = YES` (o `READY_TO_IMPLEMENT`) mientras la condición 9
de §17 sigue `PENDING` rompe el ancla.

Tres familias, todas enumeradas (no muestreadas):

1. **Tablas Markdown** — se verifica con las funciones del checker del repo
   (`test_native_parallax_pr700_corrective_docs_invariants`) **importadas**, no con una
   regex propia. La lección es explícita: el instrumento de medición debe ser el
   autoritativo.
2. **Tokens de veredicto** — los valores declarados en §8 son exactamente los esperados,
   y ninguna asignación contradictoria aparece en el documento.
3. **Prohibición de autorización prematura** — no puede coexistir una aprobación con un
   bloqueante abierto.

Tests RESEARCH-ONLY: sin red; sólo leen el documento del PR.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# --- instrumento autoritativo (importado, NO reimplementado) -------------------
from tests.test_native_parallax_pr700_corrective_docs_invariants import (
    _SEPARATOR,
    _inconsistent_tables,
)

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "docs" / "design" / "research" / "native-parallax" / "h4-resize-normal-authorization-gate.md"

# --- familia de tokens de veredicto (§8 del documento) -------------------------
_VERDICTOS_ESPERADOS = {
    "H4_R1_NUMERIC_CONTRACT": "PASS",
    "H4_R5_PROVENANCE_CONTRACT": "PASS",
    "H4_R6_CONSUMER_COVERAGE": "PASS",
    "H4_D06": "CONTRACT_CLOSED",
    "H4_D07": "CONTRACT_CLOSED",
    "H4_TECH_LEAD_APPROVAL": "PENDING",
    "H4_CODE_IMPLEMENTATION_AUTHORIZED": "NO",
    "M6_IMPLEMENTATION_BLOCKED": "YES",
    "REAL_CORPUS_RERUN": "NO",
}

# Predicados DERIVADOS del bloque de cierre (§13). Repiten la misma condición que §8
# en otra superficie del documento: si §8 dice PENDING y §13 dice APPROVED, el
# documento se contradice. Se ancla cada superficie, no una muestra.
#
# `TECH_LEAD_SCOPE_APPROVAL` también aparece **citado** en §7 (el dictamen de #711, con
# comillas invertidas) como `=NOT_RECORDED`. Eso es una cita histórica, no una
# declaración de estado. `_declaraciones` filtra las citas entre backticks para que el
# ancla mida la superficie que declara, no la que cita.
_VERDICTOS_DERIVADOS = {
    "TECH_LEAD_SCOPE_APPROVAL": "PENDING",
}

# Tokens cuya presencia activa exige ausencia de bloqueantes abiertos.
_TOKENS_DE_AUTORIZACION = (
    "H4_CODE_IMPLEMENTATION_AUTHORIZED",
    "READY_TO_IMPLEMENT",
)

_ASSIGNMENT = re.compile(
    r"(?P<key>H4_R1_NUMERIC_CONTRACT|H4_R5_PROVENANCE_CONTRACT|H4_R6_CONSUMER_COVERAGE"
    r"|H4_D06|H4_D07|H4_TECH_LEAD_APPROVAL|H4_CODE_IMPLEMENTATION_AUTHORIZED"
    r"|M6_IMPLEMENTATION_BLOCKED|REAL_CORPUS_RERUN|READY_TO_IMPLEMENT"
    r"|PRODUCTION_CHANGED|SCIENTIFIC_ARTIFACTS_CHANGED|PR675_CHANGED"
    r"|THRESHOLDS_TUNED|CORPUS_ACCESSED|TECH_LEAD_SCOPE_APPROVAL)"
    r"\s*[=:]\s*[\"'`]?(?P<value>[A-Z][A-Z0-9_]*)"
)

# Una cita entre comillas invertidas (p.ej. `` `TECH_LEAD_SCOPE_APPROVAL=NOT_RECORDED` ``)
# referencia el dictamen de #711: no declara el estado vigente. Se neutraliza antes de
# buscar declaraciones, para que el ancla mida la superficie que declara y no la que cita.
_CITA_BACKTICK = re.compile(r"`[^`\n]*`")


def _declaraciones() -> list[re.Match[str]]:
    texto = _CITA_BACKTICK.sub(lambda m: " " * len(m.group(0)), _texto())
    return list(_ASSIGNMENT.finditer(texto))


def _valores(token: str) -> set[str]:
    return {m.group("value") for m in _declaraciones() if m.group("key") == token}


def _texto() -> str:
    return _DOC.read_text(encoding="utf-8")


def _lineas() -> list[str]:
    return _texto().split("\n")


def test_el_documento_de_cierre_existe():
    assert _DOC.is_file(), f"falta el documento de cierre: {_DOC}"


def test_todas_las_tablas_markdown_son_consistentes():
    """Usa el checker del repo (importado) sobre el documento de cierre."""
    bad = _inconsistent_tables(_DOC)
    assert bad == [], f"filas con conteo de celdas inconsistente: {bad}"


def test_no_hay_barras_escapadas_en_celdas():
    """El checker del repo prohíbe `\\|` dentro de una celda. Misma regla acá."""
    ofensivas: list[tuple[int, str]] = []
    lines = _lineas()
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1]):
            for j in range(i, len(lines)):
                if not lines[j].strip().startswith("|"):
                    break
                if r"\|" in lines[j]:
                    ofensivas.append((j + 1, lines[j].strip()))
    assert ofensivas == [], f"celdas con barra escapada: {ofensivas}"


@pytest.mark.parametrize(("token", "esperado"), sorted({**_VERDICTOS_ESPERADOS, **_VERDICTOS_DERIVADOS}.items()))
def test_los_veredictos_son_los_declarados(token: str, esperado: str):
    """Cada token de §8 tiene exactamente el valor declarado, en algún bloque del doc."""
    valores = _valores(token)
    assert valores, f"el token {token} no aparece en el documento"
    assert valores == {esperado}, f"{token}: esperado {{{esperado}}}, encontrado {sorted(valores)}"


def test_no_declara_autorizacion_con_el_contrato_abierto():
    """Falla cerrado: si la autorización es YES, no puede haber bloqueantes abiertos.

    El estado válido hoy es `H4_CODE_IMPLEMENTATION_AUTHORIZED = NO` con la condición 9
    `PENDING`. Declarar `YES` mientras `H4_TECH_LEAD_APPROVAL = PENDING` es una
    contradicción que este test rechaza.
    """
    auth = _valores("H4_CODE_IMPLEMENTATION_AUTHORIZED")
    aprobacion = _valores("H4_TECH_LEAD_APPROVAL")
    # El bloque de cierre (§13) repite la condición en otra superficie: ambas deben decir PENDING.
    derivada = _valores("TECH_LEAD_SCOPE_APPROVAL")
    assert aprobacion == {"PENDING"}, f"H4_TECH_LEAD_APPROVAL inesperado: {sorted(aprobacion)}"
    assert derivada == {"PENDING"}, (
        f"TECH_LEAD_SCOPE_APPROVAL (§13) contradice a §8 con el contrato abierto: {sorted(derivada)}"
    )
    assert auth == {"NO"}, (
        "H4_CODE_IMPLEMENTATION_AUTHORIZED debe ser NO mientras "
        f"H4_TECH_LEAD_APPROVAL = PENDING; encontrado {sorted(auth)}"
    )


def test_prohibe_declarar_ready_to_implement():
    """`READY_TO_IMPLEMENT` sólo puede aparecer en su forma prohibida (NO / negada)."""
    afirmaciones = sorted(_valores("READY_TO_IMPLEMENT"))
    assert afirmaciones, "el documento debe declarar explícitamente que READY_TO_IMPLEMENT es NO"
    assert all(v == "NO" for v in afirmaciones), f"READY_TO_IMPLEMENT afirmado con el contrato abierto: {afirmaciones}"


def test_el_cierre_declara_que_no_toco_produccion():
    """El write-set documental no puede haber alterado producción ni artefactos."""
    for clave, valor in (
        ("PRODUCTION_CHANGED", "NO"),
        ("SCIENTIFIC_ARTIFACTS_CHANGED", "NO"),
        ("PR675_CHANGED", "NO"),
        ("THRESHOLDS_TUNED", "NO"),
        ("CORPUS_ACCESSED", "NO"),
    ):
        encontrados = _valores(clave)
        assert encontrados == {valor}, f"{clave}: esperado {{{valor}}}, encontrado {sorted(encontrados)}"


def test_la_condicion_9_sigue_pendiente_y_se_cita():
    """El bloqueante debe ser explícito: la condición 9 de §17, no una vaga 'aprobación'."""
    texto = _texto()
    assert "condición 9" in texto or "condicion 9" in texto
    assert "TECH_LEAD_SCOPE_APPROVAL" in texto
    assert "PENDING" in texto
