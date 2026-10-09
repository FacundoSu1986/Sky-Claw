"""Segunda ronda correctiva #700 — invariantes documentales (findings G y H).

G) Las barras verticales sin escapar dentro de celdas Markdown rompen la tabla.
   `|s*|` y `|Δ downstream aligned rmse|` se interpretan como separadores de celda.
   Este test enumera TODAS las tablas de la auditoría y falla si alguna fila tiene
   un número de celdas distinto al de su encabezado.

H) La igualdad de `rmse_self_median` entre brazos NO es, por sí sola, prueba de
   aislamiento causal. El texto debe citar la comparación estructural o limitar la
   afirmación. Este test fija que la afirmación fuerte no reaparezca.

Tests RESEARCH-ONLY: sin red; sólo leen documentos del PR.
"""

from __future__ import annotations

import re
from pathlib import Path

_AUDIT = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "validation"
    / "native-parallax-h1-h5-falsification-impact-audit-20261007"
)
_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _n_cells(row: str) -> int:
    return len(row.strip().strip("|").split("|"))


def _inconsistent_tables(path: Path) -> list[tuple[int, int, int]]:
    """Devuelve `(linea, celdas_esperadas, celdas_obtenidas)` por cada fila rota."""
    lines = path.read_text(encoding="utf-8").split("\n")
    bad: list[tuple[int, int, int]] = []
    i = 0
    while i < len(lines):
        row = lines[i]
        if row.strip().startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1]):
            ncol = _n_cells(row)
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                n = _n_cells(lines[j])
                if n != ncol:
                    bad.append((j + 1, ncol, n))
                j += 1
            i = j
        else:
            i += 1
    return bad


def test_g_todas_las_tablas_markdown_son_consistentes():
    """Enumera la familia completa: cualquier .md de la auditoría con tabla rota falla."""
    rotas: dict[str, list[tuple[int, int, int]]] = {}
    for md in sorted(_AUDIT.rglob("*.md")):
        bad = _inconsistent_tables(md)
        if bad:
            rotas[str(md.relative_to(_AUDIT))] = bad
    assert not rotas, f"tablas Markdown con celdas inconsistentes: {rotas}"


def test_g_sin_barras_sin_escapar_en_celdas_de_tabla():
    """Ninguna celda de tabla puede contener un `|...|` sin escapar."""
    ofensivas: list[tuple[str, int, str]] = []
    patron = re.compile(r"\|[^|`\n]{1,40}\|")
    for md in sorted(_AUDIT.rglob("*.md")):
        lines = md.read_text(encoding="utf-8").split("\n")
        i = 0
        while i < len(lines):
            if lines[i].strip().startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1]):
                j = i
                while j < len(lines) and lines[j].strip().startswith("|"):
                    for cell in lines[j].strip().strip("|").split("|"):
                        if patron.search(cell):
                            ofensivas.append((str(md.relative_to(_AUDIT)), j + 1, cell.strip()))
                    j += 1
                i = j
            else:
                i += 1
    assert not ofensivas, f"celdas con barras sin escapar: {ofensivas[:5]}"


def test_h_no_se_afirma_aislamiento_causal_por_la_igualdad_self():
    """La afirmación fuerte 'prueba que nada más cambió' no debe reaparecer."""
    texto = (_AUDIT / "decision-impact.md").read_text(encoding="utf-8")
    prohibido = "lo que prueba que nada más cambió"
    assert prohibido not in texto, "la igualdad de SELF no prueba aislamiento causal"


def test_h_cita_la_comparacion_estructural():
    """La afirmación debe apoyarse en la comparación de brazos, no en la coincidencia."""
    texto = (_AUDIT / "decision-impact.md").read_text(encoding="utf-8")
    assert "m4_row" in texto or "phase_c2_h4_corpus_corrective" in texto, (
        "debe citarse la comparación estructural de los brazos"
    )
    assert "por construcción" in texto or "por construccion" in texto


def test_h_declara_el_alcance_de_la_afirmacion():
    """Debe quedar explícito que el aislamiento es del contrafactual, no del sistema."""
    texto = (_AUDIT / "decision-impact.md").read_text(encoding="utf-8")
    assert "no es una prueba de aislamiento causal del sistema completo" in texto
