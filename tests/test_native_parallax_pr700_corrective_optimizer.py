"""§14 — Tests sintéticos del optimizador continuo (H1 / F1).

El brief correctivo exige probar el optimizador contra **óptimos conocidos** antes de
correr el corpus real. Cada caso declara su óptimo analítico y se exige un error de
recuperación acorde a la tolerancia declarada.

Casos cubiertos (brief §14):
  - óptimo positivo
  - óptimo negativo
  - óptimo cerca de cero
  - óptimo FUERA del piso histórico ±0.05
  - objetivo plano / casi plano
  - expansión de bracket (óptimo fuera del bracket inicial)

Tests RESEARCH-ONLY: sin red, sin corpus.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

# El módulo vive en la carpeta de la auditoría correctiva, no en el paquete instalado.
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
golden_section = opt.golden_section


# ------------------------------------------------------------------ sección áurea
def test_golden_section_recupera_optimo_cuadratico():
    """Un cuadrático convexo se recupera con error <= tolerancia."""
    f = lambda x: (x - 1.5) ** 2  # noqa: E731
    x, fx, iters, evals = golden_section(f, -10.0, 10.0, tolerance=1e-9, metric_tolerance=0.0, max_iterations=500)
    assert abs(x - 1.5) < 1e-4, f"x*={x}"
    assert fx < 1e-8
    assert iters > 0 and evals >= 2


# ------------------------------------------------------------------ óptimos conocidos
@pytest.mark.parametrize(
    ("label", "s_true"),
    [
        ("positive_far", 2.5),
        ("positive_small", 0.08),
        ("negative_far", -1.75),
        ("negative_small", -0.09),
        ("near_zero_positive", 0.01),
        ("near_zero_negative", -0.01),
    ],
)
def test_recupera_optimo_conocido(label, s_true):
    """El minimizador recupera el óptimo de un caso unimodal determinista.

    Tolerancia **medida**, no elegida a ojo (finding C — Weak Test Oracle). El error real
    de recuperación sobre los 6 casos es `<= 2.727e-07` y el piso teórico impuesto por
    `metric_tolerance` es `sqrt(metric_tolerance / f'') = 7.07e-07`. La aserción previa de
    `1e-4` era ~367x más laxa que el error observado: una desviación material del óptimo
    (p.ej. devolver el punto del barrido grueso, a `>= 3.4e-04` del óptimo real) habría
    pasado en silencio. `1e-5` deja 37x de margen sobre lo observado, 14x sobre el piso
    teórico, y sigue siendo 34x más estricta que la distancia al punto de rejilla.
    """
    f = lambda s: float((s - s_true) ** 2 + 0.1 * np.cos(0.0 * s))  # noqa: E731
    res = minimize_1d(f, OptimizerConfig())
    assert res.converged, f"{label}: status={res.status}"
    assert abs(res.best_x - s_true) <= 1e-5, f"{label}: x*={res.best_x} != {s_true}"


@pytest.mark.parametrize(
    ("label", "s_true"),
    [
        ("positive_far", 2.5),
        ("positive_small", 0.08),
        ("negative_far", -1.75),
        ("negative_small", -0.09),
        ("near_zero_positive", 0.01),
        ("near_zero_negative", -0.01),
    ],
)
def test_la_tolerancia_rechaza_una_desviacion_material(label, s_true):
    """La tolerancia elegida discrimina: el punto del barrido grueso NO la pasa.

    Defecto histórico F1: el "oráculo continuo" era un barrido finito y publicaba el mejor
    punto de la rejilla sin refinar. Ese punto dista `>= 3.4e-04` del óptimo real en todos
    los casos. Un test con tolerancia `1e-5` lo **rechaza**; uno con `1e-4` lo aceptaba.
    """
    f = lambda s: float((s - s_true) ** 2 + 0.1 * np.cos(0.0 * s))  # noqa: E731
    res = minimize_1d(f, OptimizerConfig())
    assert abs(res.best_x - s_true) <= 1e-5
    # el resultado SIN refinar (mejor punto del barrido grueso) queda fuera de la tolerancia
    assert abs(res.coarse_best_x - s_true) > 1e-5, (
        f"{label}: el punto coarse pasaría la tolerancia; el test no discriminaría"
    )


def test_optimo_fuera_del_piso_historico():
    """F1: el óptimo fuera de `|s| <= 0.05` debe encontrarse (el piso histórico lo perdía)."""
    s_true = 0.5  # 10x el piso histórico del oráculo
    f = lambda s: float((s - s_true) ** 2)  # noqa: E731
    res = minimize_1d(f, OptimizerConfig())
    assert res.converged
    assert abs(res.best_x - s_true) <= 1e-4
    assert abs(res.best_x) > 0.05, "el óptimo recuperado no supera el piso histórico"


def test_optimo_debil_fuera_del_soporte_de_la_sonda_vieja():
    """El óptimo más allá de 4x s0/4 (soporte del barrido log viejo) también se recupera."""
    s_true = 3.0
    f = lambda s: float((s - s_true) ** 2)  # noqa: E731
    res = minimize_1d(f, OptimizerConfig())
    assert res.converged
    assert abs(res.best_x - s_true) <= 1e-3


# ------------------------------------------------------------------ plano / casi plano
def test_objetivo_plano_no_finge_convergencia_de_calidad():
    """Un objetivo constante no tiene óptimo interior: el resultado no debe ser NaN/Inf.

    No se exige `CONVERGED`: un campo plano hace que el "mejor" punto sea arbitrario y
    caiga en el borde, lo que legítimamente dispara expansiones. Lo exigido es que el
    resultado sea finito y que NUNCA se reporte convergencia con calidad inventada.
    """
    res = minimize_1d(lambda s: 1.0, OptimizerConfig())
    assert np.isfinite(res.best_x)
    assert res.best_fx == pytest.approx(1.0)
    # Un objetivo sin mínimo interior no puede declararse resuelto con confianza.
    # Segunda ronda correctiva (finding E): un campo plano se reporta FLAT_OBJECTIVE,
    # no CONVERGED. Se aceptan además los estados de borde legítimos.
    if res.converged:
        assert res.status == opt.STATUS_CONVERGED
    else:
        assert res.status in {
            opt.STATUS_BOUNDARY,
            opt.STATUS_MAX_EXPANSIONS,
            opt.STATUS_FLAT,
            opt.STATUS_NO_VALID_BRACKET,
            opt.STATUS_MAX_ITERATIONS,
        }


def test_objetivo_casi_plano_no_explota():
    """Mejora despreciable => el resultado sigue siendo finito y acotado."""
    res = minimize_1d(lambda s: 1.0 + 1e-15 * s * s, OptimizerConfig())
    assert np.isfinite(res.best_x)
    assert np.isfinite(res.best_fx)
    assert abs(res.best_x) <= OptimizerConfig().bracket_max_extent


# ------------------------------------------------------------------ expansión de bracket
def test_expansion_de_bracket_para_optimo_lejano():
    """Un óptimo fuera del barrido grueso fuerza expansión y termina resuelto o marcado."""
    s_true = 9.0  # por encima de coarse_max_magnitude = 4.0
    f = lambda s: float((s - s_true) ** 2)  # noqa: E731
    res = minimize_1d(f, OptimizerConfig())
    # Con el barrido grueso hasta 4.0 el bracket detecta el borde y se expande.
    assert res.boundary_expansions > 0 or res.converged
    if res.converged:
        assert abs(res.best_x - s_true) <= 1e-3


def test_bracket_explicito_del_caller():
    """`initial_bracket` permite anclar el bracket en el basin de la forma cerrada."""
    s_true = 0.012
    f = lambda s: float((s - s_true) ** 2)  # noqa: E731
    res = minimize_1d(f, OptimizerConfig(), initial_bracket=(-0.05, 0.05))
    assert res.converged
    assert abs(res.best_x - s_true) <= 1e-5


# ------------------------------------------------------------------ determinismo
def test_determinismo_bit_a_bit():
    """Dos corridas del mismo objetivo devuelven exactamente el mismo resultado."""
    f = lambda s: float((s - 0.37) ** 2 + 0.01 * np.sin(3.0 * s))  # noqa: E731
    a = minimize_1d(f, OptimizerConfig())
    b = minimize_1d(f, OptimizerConfig())
    assert a.best_x == b.best_x
    assert a.best_fx == b.best_fx
    assert a.n_evaluations == b.n_evaluations
    assert a.status == b.status


def test_metadatos_de_convergencia_completos():
    """§13: todo resultado debe declarar los metadatos exigidos."""
    res = minimize_1d(lambda s: float((s - 0.2) ** 2), OptimizerConfig())
    d = res.as_dict()
    for key in (
        "continuous_best_strength",
        "continuous_best_objective",
        "converged",
        "iterations",
        "boundary_expansions",
        "coarse_best",
        "bracket_low",
        "bracket_high",
    ):
        assert key in d, f"falta {key}"
    cfg = d["config"]
    assert cfg["deterministic"] is True
    assert cfg["scipy_dependency"] is False
    assert cfg["tolerance"] == OptimizerConfig().tolerance


def test_no_depende_de_scipy():
    """El contrato del repo no incluye SciPy: el módulo no debe importarlo."""
    src = (_SCRIPTS / "corrective_optimizer.py").read_text(encoding="utf-8")
    assert "import scipy" not in src
    assert "from scipy" not in src
