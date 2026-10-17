"""Consistencia documental del informe EXP-M6 — cierre REV 7.4.

Cierra el P2 confirmado por CodeRabbit en REV 9: la tabla consolidada de
estados (§7) y los conteos de §5.7/§6.3 seguían afirmando cifras del modelo
previo —superseded por REV 7.3— y del gate de REV 5/6.

Método: cada test extrae un BLOQUE por delimitadores de sección estables y
comprueba las cifras ACTIVAS dentro de ese bloque. No se usan búsquedas
globales: una cifra correcta en otra sección no satisface el ancla.

No tocan el modelo, el solver, la cuadratura ni los dorados: el runner y su
snapshot del Apéndice A deben quedar byte-idénticos al baseline.

Ejecución: ``pytest tests/test_native_parallax_m6_documentary_consistency.py
--noconftest -q`` (numpy-only).
"""

from __future__ import annotations

import ast
import importlib.util
import math
import pathlib
import re
import sys
from typing import Any

import numpy as np
import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
RUNNER_PATH = RAIZ / "scripts" / "run_lambda_study_m6.py"
GATE_PATH = RAIZ / "tests" / "test_native_parallax_m6_lambda_gate.py"
DOC_PATH = RAIZ / "docs" / "design" / "research" / "native-parallax" / "exp-m6-screen-lambda-derivation-proposal.md"

# Delimitadores de sección (estables: encabezados y viñetas del documento).
DELIM = {
    "s5_7": ("7. **Gate automatizado", "## 6. Estado del harness H4"),
    "s6_3": ("### 6.3 Gate del contrato matemático", "## 7. Estados"),
    "s7": ("## 7. Estados", "## 8. Adjudicaciones"),
    "s8_6": ("### 8.6 REV 7.3 adjudicación", "### 8.7"),
    "s8_7": ("### 8.7 REV 7.4", "## Apéndice A"),
    "b3": ("**B.3", "**B.4"),
    "b4": ("**B.4 Gate (CI)", "**B.5"),
}


def _runner() -> Any:
    spec = importlib.util.spec_from_file_location("run_lambda_study_m6", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _bloque(doc: str, clave: str) -> str:
    """Extrae el bloque entre dos delimitadores. Falla si el delimitador de
    apertura no existe o si aparece más de una vez (ambigüedad de ancla)."""
    inicio, fin = DELIM[clave]
    assert doc.count(inicio) == 1, f"delimitador de apertura {inicio!r} ausente o ambiguo en {clave}"
    cola = doc.split(inicio, maxsplit=1)[1]
    assert fin in cola, f"delimitador de cierre {fin!r} ausente en {clave}"
    return cola.split(fin, maxsplit=1)[0]


def _conteo_tests_gate() -> int:
    arbol = ast.parse(GATE_PATH.read_text(encoding="utf-8"))
    return sum(1 for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))


def _bias_modelo_previo(n: int, beta: float, lams: Any) -> np.ndarray:
    """Réplica del sesgo del modelo PREVIO (REV 7.2, commit 408914d2):
    cociente de esperanzas Σu/Σv sobre las órbitas ±k. Equivale al cálculo
    Σ_rfft w·B·eh / Σ_rfft w·eh del código anterior."""
    radios, mult = _runner()._orbit_classes(n)
    v = 2.0 * radios ** (-(beta + 2.0))
    d = radios**2
    salida = []
    for lam in np.atleast_1d(np.asarray(lams, dtype=float)):
        u = v * (lam / (d + lam)) ** 2
        salida.append(float(np.sum(mult * u) / np.sum(mult * v)))
    return np.array(salida)


def _lambda_star_previo(n: int, beta: float, sigma: float, filtrado: bool = True) -> float:
    """λ* del modelo previo: misma grilla y refinamiento que el runner, pero
    con el sesgo del cociente de esperanzas. El ruido se reutiliza del runner
    (`include_signal=False`), que es la ruta que REV 7.3 declaró intacta."""
    runner = _runner()
    grid = np.geomspace(1e-12, 1.0, 200)

    def curva(lams: Any) -> np.ndarray:
        ruido = runner.analytic_curve(n, beta, sigma, lams, filtrado, include_signal=False)
        return np.sqrt(_bias_modelo_previo(n, beta, lams) + ruido**2)

    e = curva(grid)
    i = int(np.argmin(e))
    lo, hi = float(grid[max(i - 1, 0)]), float(grid[min(i + 1, len(grid) - 1)])
    for _ in range(3):
        loc = np.geomspace(lo, hi, 80)
        el = curva(loc)
        j = int(np.argmin(el))
        lo, hi = float(loc[max(j - 1, 0)]), float(loc[min(j + 1, len(loc) - 1)])
    return float(loc[j])


# ---------------------------------------------------------------------------
# §7 — tabla consolidada de estados
# ---------------------------------------------------------------------------
def test_seccion7_tabla_estados_muestra_solo_rangos_activos_corregidos() -> None:
    """La tabla de estados es la vista consolidada: no puede presentar como
    activos los rangos de `c` del modelo previo (0.085/0.146)."""
    s7 = _bloque(_doc(), "s7")
    for activo in ("0.0914", "0.0918", "0.1574", "0.1578"):
        assert activo in s7, f"§7 debe mostrar el rango activo {activo}"
    for retirado in ("c_A≈0.085", "c_B≈0.146", "0.0846", "0.1457"):
        assert retirado not in s7, f"§7 conserva el valor superseded {retirado!r} como activo"


def test_seccion7_encabezado_declara_tabla_consolidada() -> None:
    """El encabezado decía `(REV 4)` sobre una tabla con filas de REV 6 y 7.3:
    induce a leer un estado histórico como consolidado."""
    doc = _doc()
    linea = next(ln for ln in doc.splitlines() if ln.startswith("## 7. Estados"))
    assert "consolidad" in linea.lower(), f"§7 debe declararse consolidada: {linea!r}"
    assert "REV 7.4" in linea, f"§7 debe declarar su última actualización: {linea!r}"


def test_conteo_tests_gate_documentado_igual_al_conteo_real() -> None:
    """Anti-drift: el número de tests del gate que el documento publica debe
    ser el real. Agregar un test al gate sin actualizar §5.7/§6.3/§7/B.4 rompe
    acá, en vez de dejar cifras obsoletas circulando.

    En §7 la comprobación es de UNIVERSALIDAD, no de existencia: una sola fila
    que publique un conteo viejo basta para fallar (una mención correcta en
    otra fila no la absuelve)."""
    real = _conteo_tests_gate()
    assert real == 31, f"el gate M6 cambió de tamaño ({real}); actualizar el documento y este pin"
    doc = _doc()
    for clave in ("s5_7", "s6_3", "b4"):
        assert f"{real} passed" in _bloque(doc, clave), f"{clave} no publica el conteo real {real} passed"
    # `N passed` como conteo de gate: se excluye la forma "M failed, N passed"
    # de la demostración negativa, que no es un conteo de tests del gate.
    conteos = {int(m) for m in re.findall(r"(?<!failed, )(\d+)\s+passed", _bloque(doc, "s7"))}
    assert conteos == {real}, f"§7 publica conteos de gate {sorted(conteos)}; el real es {real}"


# ---------------------------------------------------------------------------
# §5.7 / §6.3 — conteos del gate y del selftest
# ---------------------------------------------------------------------------
def test_seccion57_gate_selftest_declara_10_de_10_vigente() -> None:
    s57 = _bloque(_doc(), "s5_7")
    assert "selftest 10/10" in s57, "§5.7 debe declarar el selftest vigente (10/10)"
    assert "9/9" not in s57, "§5.7 conserva el selftest superseded 9/9"


def test_seccion63_gate_declara_estado_vigente_31_passed() -> None:
    """§6.3 declaraba `4 passed` como estado del commit. El estado vigente debe
    ser el real, y la demostración negativa (runner ausente) debe citar las
    cifras del gate actual, no las de REV 6, que sobreviven sólo etiquetadas."""
    s63 = _bloque(_doc(), "s6_3")
    vigente = re.search(r"Estado en este commit \(REV 7\.4\): \*\*(\d+) passed\*\*", s63)
    assert vigente, "§6.3 debe declarar el estado vigente del gate con su revisión"
    assert int(vigente.group(1)) == 31, f"§6.3 declara {vigente.group(1)} passed como vigente"
    assert "4 passed" not in s63, "§6.3 conserva el conteo superseded 4 passed"
    assert "9 failed, 5 passed, 17 skipped" in s63, "§6.3 debe citar la demostración negativa actual"
    lineas = s63.splitlines()
    for i, linea in enumerate(lineas):
        if "1 failed, 6 skipped" in linea:
            ventana = " ".join(lineas[max(0, i - 2) : i + 2])
            assert "REV 6" in ventana, f"cifra histórica de REV 6 sin etiqueta: {linea.strip()!r}"


# ---------------------------------------------------------------------------
# §8.6 / §8.7 — adjudicaciones
# ---------------------------------------------------------------------------
def test_seccion86_conserva_cr1_cr4_y_registra_cr5_cr6() -> None:
    doc = _doc()
    s86 = _bloque(doc, "s8_6")
    for cr in ("CR-1", "CR-2", "CR-3", "CR-4"):
        assert cr in s86, f"§8.6 debe conservar la adjudicación de {cr}"
    assert "REV_7_3_CR4_BIAS_PER_REALIZATION: CORRECTED" in s86, "§8.6 perdió el veredicto de CR-4"
    assert "sobreestimaba el sesgo ≈7.4%" in s86, "§8.6 perdió la magnitud del sesgo del modelo previo"
    s87 = _bloque(doc, "s8_7")
    for cr in ("CR-5", "CR-6"):
        assert cr in s87, f"§8.7 debe registrar la adjudicación de {cr}"
    assert "REV_7_4_CR5_DIRECCION_LAMBDA: CORRECTED" in s87, "§8.7 debe declarar el veredicto de CR-5"
    assert "REV_7_4_CR6_RATIO_MC_PREVIO: CORRECTED" in s87, "§8.7 debe declarar el veredicto de CR-6"


def test_cr5_lambda_previo_queda_por_debajo_del_exacto() -> None:
    """CR-5: la dirección del error estaba invertida. El óptimo del modelo
    previo está ~7.45% POR DEBAJO del exacto, no por encima."""
    if not RUNNER_PATH.exists():
        pytest.skip("runner M6 ausente")
    runner = _runner()
    lam_prev = _lambda_star_previo(128, 2.0, 0.1, True)
    lam_exacto = runner.lambda_star_continuous(128, 2.0, 0.1, True)[0]
    rel = lam_prev / lam_exacto - 1.0
    assert lam_prev < lam_exacto, f"el previo debe quedar por DEBAJO: {lam_prev:.6e} vs {lam_exacto:.6e}"
    assert -0.08 < rel < -0.07, f"desvío esperado ≈ −7.45%, medido {rel:.4%}"
    docstring = runner._bias_exact_batch.__doc__ or ""
    assert "λ* ~7% bajo" in docstring, "el docstring del runner debe declarar la dirección corregida"
    assert "λ* ~7% alto" not in docstring, "el docstring reintrodujo la dirección invertida"


def test_cr6_ratio_mc_previo_y_previo_exacto_distinguidos() -> None:
    """CR-6: el ratio es MC/previo ≈ 0.93 (el previo sobreestima el sesgo).
    `previo/exacto ≈ 1.074` es el valor de N=128; el recibo corre a N=64 (1.078).
    Ambas direcciones deben quedar protegidas."""
    if not RUNNER_PATH.exists():
        pytest.skip("runner M6 ausente")
    runner = _runner()
    p128 = float(_bias_modelo_previo(128, 2.0, [1e-3])[0]) / float(runner._bias_exact_batch(128, 2.0, [1e-3])[0])
    p64 = float(_bias_modelo_previo(64, 2.0, [1e-3])[0]) / float(runner._bias_exact_batch(64, 2.0, [1e-3])[0])
    assert math.isclose(p128, 1.074, rel_tol=2e-3), p128
    assert math.isclose(p64, 1.078, rel_tol=2e-3), p64
    assert math.isclose(1.0 / p64, 0.93, abs_tol=0.01), 1.0 / p64
    assert p128 < p64, "el desvío del previo decrece con N: 1.074 (N=128) < 1.078 (N=64)"
    doc = _doc()
    s86 = _bloque(doc, "s8_6")
    assert "MC/modelo ≈ 0.93" in s86, "§8.6 debe declarar el ratio MC/previo ≈ 0.93"
    assert "previo/exacto ≈ 1.074" in s86, "§8.6 debe declarar previo/exacto ≈ 1.074"
    s87 = _bloque(doc, "s8_7")
    assert "N=64" in s87 and "N=128" in s87, "§8.7 debe distinguir la atribución N=128 vs N=64"


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------
def test_cifras_c_previas_aparecen_solo_etiquetadas_superseded() -> None:
    """Las cifras de `c` del modelo previo pueden sobrevivir como historia,
    pero nunca sin la etiqueta que las declara superadas."""
    lineas = _doc().splitlines()
    for marca in ("0.0846", "0.1457"):
        indices = [i for i, ln in enumerate(lineas) if marca in ln]
        assert indices, f"se esperaba al menos una mención histórica de {marca}"
        for i in indices:
            ventana = " ".join(lineas[max(0, i - 3) : i + 4]).lower()
            assert "superseded" in ventana, f"{marca!r} en la línea {i + 1} sin etiqueta superseded"


def test_apendice_b3_etiqueta_escalado_con_rangos_activos() -> None:
    """El snapshot de B.3 es la reproducción publicada de §4.2: sus rangos de
    `c` deben ser los del modelo corregido."""
    b3 = _bloque(_doc(), "b3")
    assert "[0.0914, 0.0918]" in b3, "B.3 debe publicar c_A ∈ [0.0914, 0.0918]"
    assert "[0.1574, 0.1578]" in b3, "B.3 debe publicar c_B ∈ [0.1574, 0.1578]"
