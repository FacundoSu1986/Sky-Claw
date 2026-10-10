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

import json
import re
from pathlib import Path

_AUDIT = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "validation"
    / "native-parallax-h1-h5-falsification-impact-audit-20261007"
)
_CORRECTIVE = _AUDIT / "corrective-20261008"
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


# ---------------------------------------------------------------------------
# Ronda 4 (2026-10-09) — el conteo autoritativo de H1 tiene que ser UNO solo.
#
# La ronda 2 hizo un replace ciego `28/31, no 27/31` -> `27/31, no 27/31 del texto
# original`: dejó una tautología y una afirmación falsa (el texto original ya decía 27).
# Estos tests congelan que la evidencia cruda, la adjudicación y la prosa publicada
# digan lo mismo, y que ninguna afirmación VIGENTE publique `28/31`.
# ---------------------------------------------------------------------------


def _conteo_recomputado_desde_la_evidencia() -> int:
    doc = json.loads((_CORRECTIVE / "evidence" / "h1-corrected-evidence.json").read_text(encoding="utf-8"))
    return sum(1 for r in doc["rows"] if abs(float(r["continuous_best_strength"])) < 0.05)


def test_i_el_conteo_h1_autoritativo_es_27_y_coincide_con_la_evidencia():
    """Recomputa el conteo desde la evidencia cruda y lo cruza con la adjudicación."""
    evidencia = _conteo_recomputado_desde_la_evidencia()
    assert evidencia == 27, f"el conteo recomputado cambió a {evidencia}; revisar docs y adjudicación"
    doc = json.loads((_CORRECTIVE / "evidence" / "h1-corrected-evidence.json").read_text(encoding="utf-8"))
    assert doc["counts_16"]["n_continuous_abs_strength_lt_0_05"] == evidencia
    adjudicacion = json.loads((_CORRECTIVE / "corrective-adjudication.json").read_text(encoding="utf-8"))
    assert adjudicacion["H1"]["H1_ABS_STRENGTH_LT_0_05"] == evidencia


def test_i_la_adjudicacion_final_del_readme_correctivo_publica_27():
    """El bloque de adjudicación del README correctivo no puede volver a decir 28."""
    texto = (_CORRECTIVE / "README.md").read_text(encoding="utf-8")
    valores = re.findall(r"^H1_ABS_STRENGTH_LT_0_05\s*=\s*(\d+)", texto, flags=re.MULTILINE)
    assert valores == ["27"], f"el bloque de adjudicación publica {valores}"


def test_i_ninguna_afirmacion_vigente_publica_28_31():
    """`28/31` fue un artefacto de la ronda 1 (defecto D).

    Puede citarse como historia — en texto corrido o en un code span — pero no como
    afirmación vigente (negrita), que es como aparecía en `README.md` y
    `decision-impact.md`.
    """
    ofensivas: list[tuple[str, int]] = []
    for md in sorted(_AUDIT.rglob("*.md")):
        for n, linea in enumerate(md.read_text(encoding="utf-8").split("\n"), 1):
            if "**28/31**" in linea:
                ofensivas.append((str(md.relative_to(_AUDIT)), n))
    assert not ofensivas, f"afirmaciones vigentes con 28/31: {ofensivas}"
