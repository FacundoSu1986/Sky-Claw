"""Micro-slice adversarial del optimizador H1 (PR #700) — convergencia multicuenca.

Hallazgos A y B de la ronda de validación multicuenca. Este archivo se escribió
**antes** del fix (RED → GREEN).

A) `CONVERGENCIA_MULTICUENCA`
   `minimize_1d` acumulaba `valid_bracket` y `stop_met_any` con un OR sobre TODAS las
   cuencas, y decidía el status con esos acumuladores globales. El resultado publicado,
   en cambio, es el de UNA cuenca. Consecuencia: un resultado podía declararse
   `CONVERGED` usando evidencia de bracketing/parada perteneciente a **otra** cuenca
   (brief §4 y §6). La evidencia debe quedar atada a la cuenca que produjo el resultado
   publicado.

B) `EXPANSION_DE_BORDES`
   La detección de borde y su expansión estaban dentro de `if bfx <= best_fx + 1e-15:`,
   es decir, restringidas a la cuenca que en ese momento era la mejor. Una cuenca que
   toca su borde pero todavía no es la mejor quedaba **sin expandir**, y el mínimo mejor
   situado fuera de su bracket se perdía (brief §5). La expansión debe separarse del
   orden de selección.

Los objetivos sintéticos son deterministas, continuos y matemáticamente verificables:
cada uno declara su mínimo real, medido por una referencia independiente.

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


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"corrective_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


opt = _load("corrective_optimizer")
OptimizerConfig = opt.OptimizerConfig
minimize_1d = opt.minimize_1d


def _coarse_best_x(f, cfg: OptimizerConfig) -> float:
    """Punto del barrido grueso de menor objetivo (lo que usa `_basins_from_coarse`)."""
    cands = opt._coarse_candidates(cfg)
    return min(((x, f(x)) for x in cands), key=lambda t: t[1])[0]


# ------------------------------------------------------------------ objetivos de la matriz
def cuenca_borde_ganadora(s: float) -> float:
    """Caso B/D/E: la cuenca ganadora sale del bracket de BORDE, que es inválido.

    - `s >= 0`: rama decreciente con el mejor punto del barrido en el extremo `+4`
      (f(4) = 0.18) y un mínimo real MÁS allá del dominio barrido, en `s = 6` (f = 0.10).
    - `s < 0`: cuenca interior en `-2` con bracket válido (f = 0.35).

    El barrido grueso ve la cuenca interior (0.35) y el extremo (0.18), así que el mejor
    coarse cae en el borde. `_basins_from_coarse` agrega el bracket simétrico de borde
    `(0, 4, 8)`, cuyo punto medio NO es un mínimo interior (f(0) = f(8) = 0.82 > f(4)).
    Ese bracket es INVÁLIDO, pero su refinamiento (s = 6, f = 0.10) es el resultado
    publicado: gana sobre la cuenca interior (0.35).

    Resultado correcto: NO CONVERGED — el ganador no tiene bracket válido propio.
    """
    if s >= 0.0:
        return 0.10 + 0.02 * (s - 6.0) ** 2
    return 0.35 + 0.05 * (s + 2.0) ** 2


def cuenca_borde_ganadora_espejo(s: float) -> float:
    """Inversión del orden espacial del caso B: el borde ganador queda a la izquierda."""
    return cuenca_borde_ganadora(-s)


def optimo_interior_estricto(s: float) -> float:
    """Caso A (control): un único mínimo interior bien bracketed."""
    return (s - 0.5) ** 2


def dos_cuencas_pozo_angosto(s: float) -> float:
    """Caso B con el borde ganador a la derecha y una cuenca interior angosta a la izquierda."""
    if s <= 0.0:
        return 0.12 + 3000.0 * (s + 2.0) ** 2
    if s <= 4.0:
        return 0.40 - 0.0575 * s  # f(4) = 0.17, mejor punto del barrido (borde)
    if s <= 12.0:
        return 0.17 - 0.01 * (s - 4.0)  # f(12) = 0.09 <-- mínimo real, fuera del barrido
    return 0.09 + 0.01 * (s - 12.0)


# ============================================================ Caso A — control
def test_caso_a_ganador_con_evidencia_propia_converge():
    """Control: cuando el ganador SÍ tiene bracket válido y parada cumplida, converge."""
    res = minimize_1d(optimo_interior_estricto, OptimizerConfig())
    assert res.converged is True
    assert res.status == opt.STATUS_CONVERGED
    assert res.valid_minimum_bracket is True
    assert res.stop_criterion_met is True
    assert abs(res.best_x - 0.5) < 1e-4


# ============================================================ Caso B/D — bracket ajeno
@pytest.mark.parametrize(
    ("nombre", "objective"),
    [
        ("borde derecho", cuenca_borde_ganadora),
        ("borde izquierdo (orden invertido)", cuenca_borde_ganadora_espejo),
    ],
)
def test_caso_b_ganador_sin_bracket_valido_no_converge(nombre, objective):
    """El ganador sale del bracket de borde (inválido): no puede declararse CONVERGED.

    Antes del fix el status era `CONVERGED` con `valid_minimum_bracket=True`: la validez
    la aportaba la cuenca interior `-2`, no el ganador. Ver brief §4 (matriz, caso B).
    """
    res = minimize_1d(objective, OptimizerConfig())
    assert res.converged is False, (
        f"{nombre}: CONVERGED usando evidencia de otra cuenca (best_x={res.best_x!r}, "
        f"status={res.status!r}, valid_minimum_bracket={res.valid_minimum_bracket!r})"
    )
    assert res.status != opt.STATUS_CONVERGED
    # La evidencia publicada debe describir al ganador, no a una cuenca ajena.
    assert res.valid_minimum_bracket is False


def test_caso_b_el_ganador_es_la_cuenca_del_borde():
    """Contraprueba de la construcción: el ganador proviene del bracket de borde."""
    res = minimize_1d(cuenca_borde_ganadora, OptimizerConfig())
    # El mínimo real de la rama positiva está en s = 6 con f = 0.10 (más allá del barrido).
    assert res.best_fx == pytest.approx(0.10, abs=1e-6)
    assert res.best_x == pytest.approx(6.0, abs=1e-3)
    # Y el punto medio del bracket que lo produjo NO es un mínimo interior:
    assert (
        opt.is_valid_minimum_bracket(cuenca_borde_ganadora, 0.0, 4.0, 8.0, tolerance=OptimizerConfig().metric_tolerance)
        is False
    )


def test_caso_b_invariante_d_no_se_rompe():
    """El fix no puede romper el invariante D: el publicado nunca es peor que el coarse."""
    res = minimize_1d(cuenca_borde_ganadora, OptimizerConfig())
    assert res.best_fx <= res.coarse_best_fx + 1e-12


# ============================================================ Caso C — parada ajena
def test_caso_c_ganador_sin_criterio_de_parada_no_converge():
    """El ganador agota `max_iterations`; otra cuenca cumple la parada ⇒ NO CONVERGED.

    El bracket del caller (ancho `8e-9` < `tolerance`) cumple la parada **sin iterar**
    (`0 < max_iterations` y `(b-a) <= tolerance`). El ganador, en cambio, es la cuenca
    del barrido grueso, que con `max_iterations=5` no alcanza la tolerancia. Antes del
    fix el OR global tomaba la parada del caller y declaraba `CONVERGED`.
    """
    cfg = OptimizerConfig(max_iterations=5, tolerance=1e-8)
    f = lambda s: (s - 0.02) ** 2  # noqa: E731
    cb_x = _coarse_best_x(f, cfg)
    res = minimize_1d(f, cfg, initial_bracket=(cb_x - 4e-9, cb_x + 4e-9))
    assert res.converged is False, (
        f"CONVERGED con la parada de otra cuenca (best_x={res.best_x!r}, status={res.status!r})"
    )
    assert res.status != opt.STATUS_CONVERGED


def test_caso_c_sin_caller_bracket_ya_era_no_converged():
    """Control: sin el bracket del caller, el mismo caso ya era `MAX_ITERATIONS`."""
    cfg = OptimizerConfig(max_iterations=5, tolerance=1e-8)
    res = minimize_1d(lambda s: (s - 0.02) ** 2, cfg)  # noqa: E731
    assert res.converged is False
    assert res.status == opt.STATUS_MAX_ITERATIONS


# ============================================================ Caso D — ni bracket ni parada
def test_caso_d_ganador_sin_bracket_ni_parada_no_converge():
    """El ganador no aporta ni bracket válido ni parada; ambas vienen de otras cuencas."""
    cfg = OptimizerConfig(max_iterations=5, tolerance=1e-8)
    cb_x = _coarse_best_x(cuenca_borde_ganadora, cfg)
    res = minimize_1d(cuenca_borde_ganadora, cfg, initial_bracket=(cb_x - 4e-9, cb_x + 4e-9))
    assert res.converged is False, f"status={res.status!r} con evidencia enteramente ajena"
    assert res.status != opt.STATUS_CONVERGED


# ============================================================ Caso E — mejor coarse sin refinamiento
def test_caso_e_mejor_coarse_sin_refinamiento_ganador_no_converge():
    """Si ninguna cuenca refina mejor que el mejor punto coarse, el resultado no converge.

    Con `max_iterations=0` la sección áurea no itera: devuelve un punto interior del
    bracket, nunca mejor que el mejor coarse. El resultado publicado es entonces el
    **punto coarse**, que no proviene de ningún refinamiento: aunque herede el bracket de
    la cuenca que lo CONTIENE, su criterio de parada propio no se cumple, así que no puede
    declararse `CONVERGED` (brief §4, caso E).
    """
    cfg = OptimizerConfig(max_iterations=0)
    res = minimize_1d(optimo_interior_estricto, cfg)
    assert res.best_x == res.coarse_best_x, "el ganador debería ser el punto coarse"
    assert res.stop_criterion_met is False, "ningún refinamiento cumplió la parada"
    assert res.converged is False
    assert res.status != opt.STATUS_CONVERGED


def test_caso_e_evidencia_solo_del_bracket_que_contiene_al_ganador():
    """La evidencia se toma del bracket que CONTIENE al ganador, no de uno cualquiera.

    Contraprueba del caso E: el mismo punto coarse hereda el bracket de la cuenca que lo
    contiene (que SÍ es válido), pero sin parada propia queda en `MAX_ITERATIONS` — nunca
    `CONVERGED`.
    """
    cfg = OptimizerConfig(max_iterations=0)
    res = minimize_1d(optimo_interior_estricto, cfg)
    assert res.valid_minimum_bracket is True  # el bracket que lo contiene SÍ es válido
    assert res.status == opt.STATUS_MAX_ITERATIONS


# ============================================================ Finding B — expansión de bordes
def test_finding_b_expansion_no_ganadora_no_oculta_minimo_mejor():
    """Una cuenca no-ganadora que toca su borde debe expandirse igual.

    El mejor punto del barrido está en el borde `+4` (f = 0.17). La cuenca interior
    (s = -2) refina a 0.12 y se convierte en el mejor actual; el bracket de borde
    refina a 0.13 (toca el extremo `8`). Como 0.13 > 0.12, el gate
    `bfx <= best_fx + 1e-15` lo excluía de la expansión y el mínimo real
    (s = 12, f = 0.09) quedaba oculto.

    Antes del fix: `best_fx = 0.12`, `boundary_expansions = 0`.
    """
    res = minimize_1d(dos_cuencas_pozo_angosto, OptimizerConfig())
    assert res.best_fx == pytest.approx(0.09, abs=1e-6), (
        f"se publicó {res.best_fx!r}: la expansión del borde quedó bloqueada por el orden"
    )
    assert res.best_x == pytest.approx(12.0, abs=1e-2)


def test_finding_b_referencia_independiente_confirma_el_minimo():
    """Referencia independiente: el mínimo real sobre `[-4, 20]` está en `s = 12`."""
    xs = np.linspace(-4.0, 20.0, 240001)
    fxs = np.array([dos_cuencas_pozo_angosto(float(x)) for x in xs])
    i = int(np.argmin(fxs))
    assert fxs[i] == pytest.approx(0.09, abs=1e-9)
    assert xs[i] == pytest.approx(12.0, abs=1e-3)
    res = minimize_1d(dos_cuencas_pozo_angosto, OptimizerConfig())
    assert res.best_fx <= fxs[i] + 1e-6, "el optimizador no alcanzó el mínimo de la referencia"


def test_finding_b_terminacion_y_limites_preservados():
    """La expansión de una cuenca no-ganadora no puede violar los límites declarados."""
    res = minimize_1d(dos_cuencas_pozo_angosto, OptimizerConfig())
    cfg = OptimizerConfig()
    assert res.boundary_expansions <= cfg.max_bracket_expansions
    assert abs(res.best_x) <= cfg.bracket_max_extent
    assert math.isfinite(res.best_x) and math.isfinite(res.best_fx)


# ============================================================ no-regresión (no sobre-corregir)
@pytest.mark.parametrize(
    ("nombre", "objective", "x_esperado", "tol"),
    [
        ("parabola", lambda s: (s - 0.5) ** 2, 0.5, 1e-4),
        ("kink", lambda s: 10.0 + 5.0 * abs(s - 3.9), 3.9, 1e-3),
        ("pozo angosto", lambda s: 1.0 + 50.0 * (s - 0.02) ** 2, 0.02, 1e-5),
        ("optimo lejano", lambda s: (s - 2.5) ** 2, 2.5, 1e-4),
    ],
)
def test_no_regresion_minimos_interiores_siguen_convergiendo(nombre, objective, x_esperado, tol):
    """El endurecimiento del contrato no puede tumbar mínimos interiores legítimos."""
    res = minimize_1d(objective, OptimizerConfig())
    assert res.converged is True, f"{nombre}: status={res.status!r}"
    assert res.valid_minimum_bracket is True
    assert res.stop_criterion_met is True
    assert abs(res.best_x - x_esperado) <= tol


def test_determinismo_de_los_casos_nuevos():
    """Los casos nuevos siguen siendo deterministas bit a bit."""
    for f in (cuenca_borde_ganadora, dos_cuencas_pozo_angosto):
        a = minimize_1d(f, OptimizerConfig())
        b = minimize_1d(f, OptimizerConfig())
        assert (a.best_x, a.best_fx, a.status, a.n_evaluations) == (
            b.best_x,
            b.best_fx,
            b.status,
            b.n_evaluations,
        )
