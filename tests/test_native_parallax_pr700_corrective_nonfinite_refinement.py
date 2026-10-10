"""Novena ronda correctiva #700 — adjudicación de las cinco observaciones del informe
automático sobre el HEAD `dbec8d9c`, con foco en el refinamiento ante valores no finitos.

El informe automático (Qodo) dejó cinco observaciones. Cuatro son **no bloqueantes** (una
ya cubierta por tests previos: C2 fail-closed ante truthy no booleano); la quinta — NaN/Inf
**dentro** del refinamiento por sección áurea — se adjudica acá con una **prueba focal**, sin
reabrir las corridas científicas (brief del Tech Lead).

Resultado de la prueba focal: **no se reproduce ningún defecto material**. El refinamiento es
fail-closed frente a valores no finitos **por construcción**, y estos tests **fijan** esa
propiedad enumerando escenarios (no muestreando uno):

  (i)   `CONVERGED` exige `result_finite`: un resultado no finito jamás se publica como
        convergente;
  (ii)  el invariante D se sostiene (`final <= best_observed`) aunque haya no finitos;
  (iii) `is_valid_minimum_bracket` rechaza cualquier bracket con un punto no finito;
  (iv)  un ancla no finita en `f(0.0)` —el `0.0` es el PRIMER candidato del barrido— nunca
        produce `CONVERGED`;
  (v)   un no finito **aislado** no reemplaza un mejor valor finito ya observado (el
        invariante D lo rechaza porque `NaN < x` es falso).

Las propiedades (i)–(iii) y (v) son de CONTRATO. La (iv) documenta el comportamiento
determinista del ancla: `min(coarse, key=...)` con una clave `NaN` conserva el primer
elemento (toda comparación con `NaN` es falsa), así que `f(0.0)` no finito deja el resultado
no finito y cae por `result_finite`. Es un **fail-closed pesimista**, no una respuesta
incorrecta.

Los tests de la observación 1 fijan que la parada por métrica de la sección áurea sólo puede
dispararse sobre el punto **recién evaluado** (no sobre un valor reutilizado), y los de la
observación 4 fijan que el bracket del caller es **simétrico alrededor de cero**, no centrado
en la semilla `s0`.

Tests RESEARCH-ONLY: sin red, sin corpus.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "validation"
    / "native-parallax-h1-h5-falsification-impact-audit-20261007"
    / "corrective-20261008"
    / "scripts"
)

sys.path.insert(0, str(_SCRIPTS))


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"corrective_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


opt = _load("corrective_optimizer")
pcc = _load("phase_c_corrective")

NAN = float("nan")
INF = float("inf")


def _mismo(a: float, b: float) -> bool:
    """Igualdad que trata `NaN == NaN` como verdadera (para comparar determinismo)."""
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return bool(a == b)


# ---------------------------------------------------------------------------
# Observación 3 (focal) — el objetivo devuelve un no finito en una región DISTINTA
# en cada escenario. Enumera la familia; no muestrea un caso.
# ---------------------------------------------------------------------------
_ESCENARIOS = [
    ("todos_nan", lambda s: NAN),
    ("todos_pos_inf", lambda s: INF),
    ("todos_neg_inf", lambda s: -INF),
    ("nan_en_ancla_cero", lambda s: NAN if s == 0.0 else s * s),
    ("nan_en_vecindad_optimo", lambda s: NAN if abs(s) < 1e-3 else s * s),
    ("nan_en_optimo_exacto", lambda s: NAN if abs(s - 0.5) < 1e-15 else (s - 0.5) ** 2),
    ("nan_en_positivos", lambda s: NAN if s > 0 else s * s),
    ("pos_inf_en_vecindad_optimo", lambda s: INF if abs(s) < 0.1 else s * s),
    ("neg_inf_en_optimo", lambda s: -INF if abs(s - 0.5) < 1e-12 else (s - 0.5) ** 2),
    ("nan_en_un_punto_del_barrido", lambda s: NAN if abs(abs(s) - 4.0) < 1e-12 else s * s),
    ("nan_en_vecindad_de_un_minimo_lejano", lambda s: NAN if abs(s - 0.02) < 1e-3 else (s - 0.02) ** 2),
    ("control_limpio", lambda s: s * s),
]
_IDS = [nombre for nombre, _ in _ESCENARIOS]


@pytest.mark.parametrize(("nombre", "objective"), _ESCENARIOS, ids=_IDS)
def test_converged_exige_resultado_finito(nombre, objective):
    """(i) Un resultado no finito jamás se publica como `CONVERGED`."""
    res = opt.minimize_1d(objective, opt.OptimizerConfig())
    finito = math.isfinite(res.best_x) and math.isfinite(res.best_fx)
    if res.converged:
        assert finito, f"{nombre}: CONVERGED con resultado no finito"
    if not finito:
        assert res.converged is False, f"{nombre}: no finito publicado como convergente"
        # Contrato de F8: un resultado NO EVALUABLE cae por `result_finite` (`NO_BRACKET`),
        # no se confunde con un problema de forma (`FLAT_OBJECTIVE`) ni de bracketing.
        assert res.status == opt.STATUS_NO_BRACKET, f"{nombre}: {res.status!r}"


@pytest.mark.parametrize(("nombre", "objective"), _ESCENARIOS, ids=_IDS)
def test_invariante_d_se_sostiene_con_no_finitos(nombre, objective):
    """(ii) `final_objective <= best_observed_objective` (invariante D) con no finitos."""
    res = opt.minimize_1d(objective, opt.OptimizerConfig())
    if math.isfinite(res.coarse_best_fx) and math.isfinite(res.best_fx):
        assert res.best_fx <= res.coarse_best_fx, nombre


@pytest.mark.parametrize(("nombre", "objective"), _ESCENARIOS, ids=_IDS)
def test_determinismo_bajo_no_finitos(nombre, objective):
    """La presencia de no finitos no introduce no determinismo."""
    cfg = opt.OptimizerConfig()
    a = opt.minimize_1d(objective, cfg)
    b = opt.minimize_1d(objective, cfg)
    assert (a.status, a.converged, a.n_evaluations) == (b.status, b.converged, b.n_evaluations), nombre
    assert _mismo(a.best_x, b.best_x) and _mismo(a.best_fx, b.best_fx), nombre


@pytest.mark.parametrize("valor", [NAN, INF, -INF], ids=["nan", "pos_inf", "neg_inf"])
def test_ancla_no_finita_en_cero_nunca_converge(valor):
    """(iv) `f(0.0)` no finito ⇒ fail-closed, nunca `CONVERGED`.

    El `0.0` es el primer candidato del barrido grueso. Con `NaN` la clave de `min` conserva
    ese primer elemento (toda comparación con `NaN` es falsa) y el resultado queda no finito;
    con `+inf`/`-inf` el ordenamiento sigue funcionando, pero el ancla tampoco es un mínimo
    interior. Los tres caminos terminan en `converged = False` (status `NO_BRACKET` para
    `NaN`/`-inf`; `NO_VALID_BRACKET` para `+inf`). El contrato que se fija es el fail-closed,
    no el status concreto.
    """
    res = opt.minimize_1d(lambda s: valor if s == 0.0 else s * s, opt.OptimizerConfig())
    assert res.converged is False
    assert res.status != opt.STATUS_CONVERGED


def test_control_objetivo_limpio_sigue_convergiendo():
    """Sin no finitos el optimizador converge: la guarda no sobre-corrige."""
    res = opt.minimize_1d(lambda s: s * s, opt.OptimizerConfig())
    assert res.status == opt.STATUS_CONVERGED
    assert res.converged is True
    assert res.result_finite is True


@pytest.mark.parametrize(
    ("nombre", "objective"),
    [
        ("nan_en_vecindad_de_un_minimo_lejano", lambda s: NAN if abs(s - 0.02) < 1e-3 else (s - 0.02) ** 2),
        ("nan_en_optimo_exacto", lambda s: NAN if abs(s - 0.5) < 1e-15 else (s - 0.5) ** 2),
        ("nan_en_un_punto_del_barrido", lambda s: NAN if abs(abs(s) - 4.0) < 1e-12 else s * s),
        ("neg_inf_en_optimo", lambda s: -INF if abs(s - 0.5) < 1e-12 else (s - 0.5) ** 2),
    ],
    ids=["nan_minimo_lejano", "nan_optimo_exacto", "nan_punto_barrido", "neg_inf_optimo"],
)
def test_un_punto_no_finito_aislado_no_envenena_el_resultado(nombre, objective):
    """Un no finito AISLADO no puede reemplazar un mejor valor finito ya observado.

    El refinamiento puede DEVOLVER un valor no finito (si su punto final cae en la región no
    evaluable). El invariante D lo rechaza: `bfx < best_fx` es falso cuando `bfx` es `NaN`,
    así que el resultado publicado sigue siendo el mejor **finito**. Si la adopción usara
    `not (bfx > best_fx)` en vez de `bfx < best_fx`, el `NaN` se adoptaría y el resultado
    publicado pasaría a no ser finito.
    """
    res = opt.minimize_1d(objective, opt.OptimizerConfig())
    assert math.isfinite(res.best_x), nombre
    assert math.isfinite(res.best_fx), nombre
    assert res.result_finite is True, nombre


# ---------------------------------------------------------------------------
# Observación 3 — `is_valid_minimum_bracket` es fail-closed por punto.
# ---------------------------------------------------------------------------
_NO_FINITOS = [(NAN, "nan"), (INF, "pos_inf"), (-INF, "neg_inf")]


@pytest.mark.parametrize(("no_finito", "id_nf"), _NO_FINITOS, ids=[i for _, i in _NO_FINITOS])
@pytest.mark.parametrize("posicion", ["lo", "mid", "hi"])
def test_is_valid_minimum_bracket_rechaza_puntos_no_finitos(no_finito, id_nf, posicion):
    """(iii) Un bracket con CUALQUIER punto no finito no aporta evidencia de mínimo."""
    objetivo = {"lo": 0.0, "mid": 1.0, "hi": 2.0}

    def f(s: float) -> float:
        if s == objetivo[posicion]:
            return no_finito
        return (s - 1.0) ** 2

    assert opt.is_valid_minimum_bracket(f, 0.0, 1.0, 2.0, tolerance=1e-12) is False


def test_is_valid_minimum_bracket_control_finito_es_true():
    """Contraprueba: el MISMO bracket con valores finitos SÍ es válido."""
    assert opt.is_valid_minimum_bracket(lambda s: (s - 1.0) ** 2, 0.0, 1.0, 2.0, tolerance=1e-12) is True


# ---------------------------------------------------------------------------
# Observación 3 — el refinamiento de un objetivo finito devuelve un resultado finito.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("lo", "hi", "xmin"),
    [(-1.0, 2.0, 0.3), (0.0, 1.0, 0.5), (-5.0, 5.0, -2.0)],
)
def test_refinamiento_con_objetivo_finito_devuelve_finito(lo, hi, xmin):
    cfg = opt.OptimizerConfig()
    _x, fx, _iters, _evals, _stop = opt._golden_section_detailed(
        lambda s: (s - xmin) ** 2,
        lo,
        hi,
        tolerance=cfg.tolerance,
        metric_tolerance=cfg.metric_tolerance,
        max_iterations=cfg.max_iterations,
    )
    assert math.isfinite(fx)
    assert fx <= (lo - xmin) ** 2
    assert fx <= (hi - xmin) ** 2


# ---------------------------------------------------------------------------
# Observación 1 — la parada de la sección áurea usa el punto RECIÉN evaluado.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("metric_tol", [1e-2, 1e-3], ids=["mt_1e-2", "mt_1e-3"])
def test_la_parada_por_metrica_dispara_sobre_el_punto_nuevo(metric_tol):
    """Si la parada usara un valor REUTILIZADO, `nuevo` no sería un mínimo estricto nuevo.

    Se registra la secuencia de valores evaluados. La iteración `i` evalúa exactamente UN
    punto nuevo, en la posición `i + 1` de la secuencia (las dos primeras evaluaciones son
    los extremos iniciales). La parada por métrica sólo puede dispararse si ese punto nuevo
    es un mínimo estricto de todo lo evaluado antes.
    """
    cfg = opt.OptimizerConfig()
    seq: list[float] = []

    def f(s: float) -> float:
        v = (s - 0.3) ** 2
        seq.append(v)
        return v

    _x, _fx, iters, evals, stop = opt._golden_section_detailed(
        f, 0.0, 1.0, tolerance=cfg.tolerance, metric_tolerance=metric_tol, max_iterations=200
    )
    assert stop is True
    assert evals == 2 + iters
    assert len(seq) == evals
    nuevo = seq[iters + 1]
    previo = min(seq[: iters + 1])
    assert nuevo < previo, "la parada debe provenir de una mejora del punto NUEVO, no de uno reutilizado"


def test_contabilidad_de_evaluaciones_en_la_parada_por_intervalo():
    """Con `metric_tolerance = 0` la parada sólo puede venir del intervalo; el conteo cierra."""
    calls = {"n": 0}

    def f(s: float) -> float:
        calls["n"] += 1
        return (s - 0.3) ** 2

    _x, _fx, iters, evals, stop = opt._golden_section_detailed(
        f, 0.0, 1.0, tolerance=1e-6, metric_tolerance=0.0, max_iterations=200
    )
    assert stop is True
    assert evals == 2 + iters == calls["n"]


# ---------------------------------------------------------------------------
# Observación 4 — el bracket del caller es simétrico alrededor de CERO, no de `s0`.
# ---------------------------------------------------------------------------
def _resultado_dummy() -> opt.OptimizeResult:
    return opt.OptimizeResult(best_x=0.0, best_fx=0.0, status=opt.STATUS_CONVERGED, converged=True, iterations=0)


def _capturar_bracket(monkeypatch, s0: float) -> dict:
    capturado: dict = {}

    def fake_minimize(objective, cfg, *, initial_bracket=None):
        capturado["bracket"] = initial_bracket
        return _resultado_dummy()

    monkeypatch.setattr(pcc, "closed_form_strength", lambda normal, hc: s0)
    monkeypatch.setattr(pcc, "minimize_1d", fake_minimize)
    pcc.continuous_oracle_real(np.zeros((2, 2)), np.zeros((2, 2)), opt.OptimizerConfig())
    return capturado


@pytest.mark.parametrize(
    ("s0", "esperado"),
    [(0.37, (-0.74, 0.74)), (-0.37, (-0.74, 0.74)), (1.0, (-2.0, 2.0))],
    ids=["s0_pos", "s0_neg", "s0_uno"],
)
def test_bracket_inicial_del_caller_es_simetrico_alrededor_de_cero(monkeypatch, s0, esperado):
    """El bracket es `[-2·abs(s0), 2·abs(s0)]`: simétrico alrededor de CERO, no de `s0`."""
    capturado = _capturar_bracket(monkeypatch, s0)
    lo, hi = capturado["bracket"]
    assert (lo, hi) == esperado
    assert lo + hi == 0.0, "el bracket es simétrico alrededor de CERO"
    assert abs((lo + hi) / 2.0 - s0) > 0.0 or s0 == 0.0, "el punto medio es 0, no s0"


def test_sin_semilla_no_hay_bracket_inicial(monkeypatch):
    """Con `s0 == 0` no se pasa bracket: el optimizador usa su barrido grueso."""
    capturado = _capturar_bracket(monkeypatch, 0.0)
    assert capturado["bracket"] is None
