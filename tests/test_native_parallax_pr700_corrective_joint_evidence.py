"""Octava ronda correctiva #700 — la evidencia de convergencia debe ser CONJUNTA.

Hallazgo del Tech Lead sobre el HEAD `44a74310` (residual del finding A, ronda 7).

El fix de la séptima ronda ató la evidencia al conjunto de brackets que **contienen** al
ganador, pero dentro de ese conjunto seguía calculando dos `any()` independientes:

    containing    = [c for c in considered if c[0] <= best_x <= c[2]]
    valid_bracket = any(is_valid_minimum_bracket(f, c[0], c[1], c[2], ...) for c in containing)
    stop_met_any  = any(c[3] for c in containing)

Dos brackets **distintos**, ambos conteniendo al ganador, podían aportar uno la validez y
el otro la parada: `VALID_BRACKET(A) + STOP_MET(B)` con `A != B` seguía produciendo
`CONVERGED`. La contención arregló la atribución **entre cuencas**; no la asociación
**entre brackets superpuestos** dentro del conjunto que contiene al ganador.

El escenario se materializa cuando el barrido grueso contiene el óptimo EXACTO — el cero
está siempre en `_coarse_candidates`, así que basta un objetivo minimizado en `0`:

  * ningún refinamiento mejora al punto coarse ⇒ el ganador **no se mueve** y queda
    dentro del bracket del caller;
  * el bracket del caller (semiancho `4e-9`) es demasiado angosto para demostrar un
    mínimo por encima de `metric_tolerance = 1e-12` — la variación de `f` allí es
    `~1.6e-17` — pero lo bastante angosto para satisfacer la parada **sin iterar**;
  * la cuenca del barrido SÍ demuestra el mínimo, pero con `max_iterations = 5` agota
    las iteraciones sin cumplir la parada.

Resultado defectuoso: `CONVERGED` con `valid_minimum_bracket = True` (de la cuenca) y
`stop_criterion_met = True` (del caller) — dos brackets distintos.

Nota: el caso C del suite multicuenca **no** cubría esto. Allí el ganador se mueve fuera
del bracket del caller y la contención lo excluye; acá el ganador es el punto coarse y
queda dentro de los tres brackets.

Tests RESEARCH-ONLY: sin red, sin corpus.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


# El bracket del caller del contraejemplo del Tech Lead.
_CALLER_LO, _CALLER_HI = -4e-9, 4e-9


def _cfg_degenerado() -> OptimizerConfig:
    """Config del contraejemplo: barrido ancho (contiene el 0 exacto) y pocas iteraciones."""
    return OptimizerConfig(coarse_max_magnitude=400.0, max_iterations=5)


# ------------------------------------------------------------------ el contraejemplo
def test_contraejemplo_brackets_superpuestos_no_converge():
    """El contraejemplo exacto del Tech Lead: `f(s) = s²`, caller `(-4e-9, 4e-9)`.

    Antes del fix: `status = CONVERGED` con la validez aportada por la cuenca del barrido
    y la parada por el bracket del caller — dos brackets distintos que contienen a `0.0`.
    """
    res = minimize_1d(lambda s: s * s, _cfg_degenerado(), initial_bracket=(_CALLER_LO, _CALLER_HI))
    assert res.converged is False, (
        f"CONVERGED sin evidencia conjunta: best_x={res.best_x!r}, status={res.status!r}, "
        f"valid_minimum_bracket={res.valid_minimum_bracket!r}, "
        f"stop_criterion_met={res.stop_criterion_met!r}"
    )
    assert res.status != opt.STATUS_CONVERGED
    assert res.status == opt.STATUS_MAX_ITERATIONS


@pytest.mark.parametrize(
    ("nombre", "objective"),
    [
        ("s²", lambda s: s * s),
        ("2·s²", lambda s: 2.0 * s * s),
        ("s²/4", lambda s: 0.25 * s * s),
    ],
)
def test_familia_de_objetivos_minimizados_en_cero_no_converge(nombre, objective):
    """Enumera la familia, no muestrea un caso: cualquier cuadrática con mínimo en `0`.

    El cero está siempre en `_coarse_candidates`, así que el ganador es el punto coarse
    en todos los miembros de la familia.
    """
    res = minimize_1d(objective, _cfg_degenerado(), initial_bracket=(_CALLER_LO, _CALLER_HI))
    assert res.converged is False, f"{nombre}: status={res.status!r}"
    assert res.status != opt.STATUS_CONVERGED


def test_el_ganador_es_el_punto_coarse_y_no_se_mueve():
    """Contraprueba de la construcción: el ganador es el punto coarse (el `0` exacto)."""
    res = minimize_1d(lambda s: s * s, _cfg_degenerado(), initial_bracket=(_CALLER_LO, _CALLER_HI))
    assert res.best_x == res.coarse_best_x == 0.0
    assert res.best_fx == res.coarse_best_fx == 0.0


def test_el_bracket_del_caller_no_es_evidencia_de_minimo_interior():
    """El bracket minúsculo del caller NO demuestra un mínimo interior.

    La variación de `f` en `[-4e-9, 4e-9]` es `(4e-9)² = 1.6e-17`, muy por debajo de
    `metric_tolerance = 1e-12`: el bracketing de tres puntos no se satisface.
    """
    cfg = _cfg_degenerado()
    assert (
        opt.is_valid_minimum_bracket(lambda s: s * s, _CALLER_LO, 0.0, _CALLER_HI, tolerance=cfg.metric_tolerance)
        is False
    )


def test_el_bracket_del_caller_si_satisface_la_parada_sin_iterar():
    """Y sin embargo SÍ satisface la parada: el ancho `8e-9` es menor que `tolerance`.

    Esto es lo que hacía peligrosa la combinación: `stop_criterion_met` no puede decidir
    el status por sí sola si proviene de un bracket que no aporta el mínimo.
    """
    cfg = _cfg_degenerado()
    _, _, iters, _, stop_met = opt._golden_section_detailed(
        lambda s: s * s,
        _CALLER_LO,
        _CALLER_HI,
        tolerance=cfg.tolerance,
        metric_tolerance=cfg.metric_tolerance,
        max_iterations=cfg.max_iterations,
    )
    assert stop_met is True
    assert iters == 0


def test_las_banderas_reportadas_son_de_existencia_no_de_asociacion():
    """`valid_minimum_bracket` y `stop_criterion_met` son afirmaciones de EXISTENCIA.

    En este caso ambas son `True` (hay un bracket válido y hay un bracket que paró), pero
    pertenecen a brackets distintos: por eso el status **no** puede ser `CONVERGED`. El
    status es el veredicto conjunto; las banderas describen la búsqueda.
    """
    res = minimize_1d(lambda s: s * s, _cfg_degenerado(), initial_bracket=(_CALLER_LO, _CALLER_HI))
    assert res.valid_minimum_bracket is True
    assert res.stop_criterion_met is True
    assert res.status == opt.STATUS_MAX_ITERATIONS


# ------------------------------------------------------------------ no sobre-corregir
def test_control_bracket_ancho_del_caller_si_converge():
    """Un bracket del caller ancho SÍ aporta ambas cosas: el status debe seguir siendo CONVERGED.

    Sin esta contraprueba, exigir la asociación conjunta podría tumbar mínimos legítimos
    que llegan por el bracket del caller.
    """
    res = minimize_1d(lambda s: s * s, OptimizerConfig(), initial_bracket=(-0.01, 0.01))
    assert res.converged is True, f"status={res.status!r}"
    assert res.status == opt.STATUS_CONVERGED
    assert res.valid_minimum_bracket is True
    assert res.stop_criterion_met is True


@pytest.mark.parametrize(
    ("nombre", "objective", "x_esperado"),
    [
        ("parabola", lambda s: (s - 0.5) ** 2, 0.5),
        ("pozo angosto", lambda s: 1.0 + 50.0 * (s - 0.02) ** 2, 0.02),
        ("optimo lejano", lambda s: (s - 2.5) ** 2, 2.5),
    ],
)
def test_no_regresion_minimos_interiores_siguen_convergiendo(nombre, objective, x_esperado):
    """El gate conjunto no puede tumbar mínimos interiores legítimos con la config por defecto."""
    res = minimize_1d(objective, OptimizerConfig())
    assert res.converged is True, f"{nombre}: status={res.status!r}"
    assert abs(res.best_x - x_esperado) < 1e-4


def test_determinismo_del_caso_nuevo():
    """El caso nuevo sigue siendo determinista bit a bit."""
    cfg = _cfg_degenerado()
    a = minimize_1d(lambda s: s * s, cfg, initial_bracket=(_CALLER_LO, _CALLER_HI))
    b = minimize_1d(lambda s: s * s, cfg, initial_bracket=(_CALLER_LO, _CALLER_HI))
    assert (a.best_x, a.best_fx, a.status, a.n_evaluations) == (
        b.best_x,
        b.best_fx,
        b.status,
        b.n_evaluations,
    )
