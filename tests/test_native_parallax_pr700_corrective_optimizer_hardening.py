"""Segunda ronda correctiva #700 — endurecimiento del optimizador H1 (findings D y E).

Estos tests se escribieron **antes** del fix (rojo → verde) y fijan dos propiedades
que la primera ronda no garantizaba:

D) `REFINEMENT_CAN_NEVER_LOSE_A_BETTER_COARSE_RESULT`
   El resultado publicado nunca puede ser peor que el mejor punto ya evaluado en el
   barrido grueso. La primera ronda construía el bracket como
   `(initial_bracket[0], coarse_best_x, initial_bracket[1])`, pero `golden_section`
   sólo usa los extremos como intervalo: si `coarse_best_x` quedaba fuera del bracket
   del caller, el refinamiento buscaba en una región que excluía el mejor punto y
   luego **sobrescribía** el resultado coarse con uno peor.

E) `STATUS_CONVERGED` exige evidencia de convergencia
   No alcanza con "no toqué el borde". Hace falta bracket válido con mínimo interior,
   criterio de parada efectivamente cumplido (no agotamiento de `max_iterations`) y
   resultado finito.

Regresión obligatoria del caso real: `polyhaven_gray_rocks`.

Tests RESEARCH-ONLY: sin red, sin corpus.
"""

from __future__ import annotations

import importlib.util
import math
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

# Valores reales del asset `polyhaven_gray_rocks` registrados en
# `evidence/h1-corrected-evidence.json` de la primera ronda correctiva.
GRAY_ROCKS_COARSE_X = 0.08954884554273354
GRAY_ROCKS_COARSE_FX = 17.024981969359246
GRAY_ROCKS_CALLER_BRACKET = (-0.023735650993217856, 0.023735650993217856)
GRAY_ROCKS_OLD_PUBLISHED_FX = 23.695281265126724


# ------------------------------------------------------------------ D: invariante global
@pytest.mark.parametrize(
    "objective, bracket",
    [
        # el mínimo real está MUY por fuera del bracket del caller
        (lambda s: 17.025 + 800.0 * (s - GRAY_ROCKS_COARSE_X) ** 2, GRAY_ROCKS_CALLER_BRACKET),
        # bracket del caller desplazado al otro lado del mínimo
        (lambda s: 5.0 + 100.0 * (s - 0.5) ** 2, (-2.0, -1.0)),
        # bracket del caller que contiene el mínimo pero es enorme
        (lambda s: 1.0 + (s - 0.02) ** 2, (-100.0, 100.0)),
    ],
)
def test_invariante_final_nunca_peor_que_mejor_coarse(objective, bracket):
    """El publicado JAMÁS puede ser peor que el mejor valor ya observado."""
    res = minimize_1d(objective, OptimizerConfig(), initial_bracket=bracket)
    assert res.best_fx <= res.coarse_best_fx + 1e-12, (
        f"refinamiento perdió un resultado coarse mejor: publicado={res.best_fx!r} > coarse={res.coarse_best_fx!r}"
    )


def test_mejor_coarse_fuera_del_bracket_del_caller():
    """Regresión del defecto: bracket incompatible no puede descartar el coarse best."""
    objective = lambda s: 17.025 + 800.0 * (s - GRAY_ROCKS_COARSE_X) ** 2  # noqa: E731
    res = minimize_1d(objective, OptimizerConfig(), initial_bracket=GRAY_ROCKS_CALLER_BRACKET)
    # El coarse best está fuera del bracket: se publica un resultado al menos tan bueno
    # como el coarse (17.025 en el mínimo analítico), no el refinado peor de 23.695.
    assert res.best_fx <= res.coarse_best_fx + 1e-12
    assert res.best_fx < 18.0, f"se publicó el refinado peor: {res.best_fx!r}"
    assert res.best_fx < GRAY_ROCKS_OLD_PUBLISHED_FX


def test_regresion_polyhaven_gray_rocks():
    """El caso real reportado: 17.025 coarse vs 23.695 publicado en la 1ra ronda."""
    objective = lambda s: 17.025 + 800.0 * (s - GRAY_ROCKS_COARSE_X) ** 2  # noqa: E731
    res = minimize_1d(objective, OptimizerConfig(), initial_bracket=GRAY_ROCKS_CALLER_BRACKET)
    # El invariante es la propiedad esencial: nunca publicar peor que lo ya observado.
    assert res.best_fx <= res.coarse_best_fx + 1e-12, (
        "polyhaven_gray_rocks: el resultado publicado quedó peor que el coarse best"
    )
    # Y el resultado debe ser el mínimo analítico (~17.025), no el 23.695 de la 1ra ronda.
    assert res.best_fx < 17.1, f"no se recuperó el mínimo analítico: {res.best_fx!r}"
    assert abs(res.best_x - GRAY_ROCKS_COARSE_X) < 1e-3


def test_refinamiento_mejor_que_coarse_si_mejora():
    """Contraprueba: si el refinamiento SÍ mejora, se publica la mejora."""
    objective = lambda s: (s - 0.3) ** 2  # noqa: E731
    res = minimize_1d(objective, OptimizerConfig(), initial_bracket=(-1.0, 1.0))
    assert res.best_fx <= res.coarse_best_fx + 1e-12
    assert abs(res.best_x - 0.3) < 1e-3


# ------------------------------------------------------------------ D: múltiples cuencas
def test_multiples_cuencas_locales_publica_la_mejor():
    """Con dos cuencas, se publica la de menor objetivo (no una arbitraria)."""

    def two_wells(s: float) -> float:
        shallow = 2.0 + 50.0 * (s - 0.02) ** 2
        deep = 0.5 + 50.0 * (s - 0.4) ** 2
        return min(shallow, deep)

    res = minimize_1d(two_wells, OptimizerConfig())
    assert res.best_fx <= res.coarse_best_fx + 1e-12
    assert res.best_fx < 1.0, f"se publicó la cuenca superficial: {res.best_fx!r}"


# ------------------------------------------------------------------ E: contrato de status
def test_plano_no_reporta_converged():
    """Objetivo estrictamente plano: no hay mínimo interior que declarar."""
    res = minimize_1d(lambda s: 1.0, OptimizerConfig())
    assert math.isfinite(res.best_x)
    assert not res.converged, "un objetivo plano no puede declararse CONVERGED"
    assert res.status != opt.STATUS_CONVERGED


def test_casi_plano_no_reporta_converged():
    """Ruido despreciable: el mínimo es arbitrario, no una convergencia real."""
    res = minimize_1d(lambda s: 2.0 + 1e-14 * math.sin(s * 1000.0), OptimizerConfig())
    assert not res.converged
    assert res.status != opt.STATUS_CONVERGED


def test_max_iterations_agotado_no_reporta_converged():
    """Si el criterio de parada NO se cumplió, no se puede declarar CONVERGED."""
    cfg = OptimizerConfig(max_iterations=1, tolerance=1e-30)
    res = minimize_1d(lambda s: (s - 0.5) ** 2 + 1.0, cfg)
    assert not res.converged, "agotar max_iterations no es converger"
    assert res.status != opt.STATUS_CONVERGED


def test_objetivo_monotono_no_reporta_converged():
    """Objetivo estrictamente monótono en el dominio: el mínimo está en el borde,
    no en un interior, así que no hay convergencia interior que declarar."""

    def monotone(s: float) -> float:
        return 10.0 + 5.0 * s

    res = minimize_1d(monotone, OptimizerConfig())
    assert not res.converged, "un objetivo monótono no tiene mínimo interior"
    assert res.status != opt.STATUS_CONVERGED


def test_kink_interior_si_converge():
    """Contraprueba: un mínimo interior (aunque sea un kink) SÍ debe converger."""
    res = minimize_1d(lambda s: 10.0 + 5.0 * abs(s - 3.9), OptimizerConfig())
    assert res.converged
    assert res.status == opt.STATUS_CONVERGED
    assert abs(res.best_x - 3.9) < 1e-3


def test_status_declarado_es_uno_de_los_conocidos():
    """El status emitido pertenece al conjunto declarado."""
    validos = {
        opt.STATUS_CONVERGED,
        opt.STATUS_BOUNDARY,
        opt.STATUS_MAX_EXPANSIONS,
        opt.STATUS_NO_BRACKET,
        getattr(opt, "STATUS_FLAT", "FLAT_OBJECTIVE"),
        getattr(opt, "STATUS_NO_VALID_BRACKET", "NO_VALID_BRACKET"),
        getattr(opt, "STATUS_MAX_ITERATIONS", "MAX_ITERATIONS"),
    }
    for obj in (
        lambda s: (s - 0.5) ** 2,
        lambda s: 1.0,
        lambda s: 10.0 + 5.0 * abs(s - 3.9),
    ):
        res = minimize_1d(obj, OptimizerConfig())
        assert res.status in validos, f"status inesperado: {res.status!r}"


# ------------------------------------------------------------------ E: bracket inválido
def test_bracket_invalido_es_rechazado_o_ignorado():
    """Un bracket no creciente no puede usarse como intervalo de refinamiento."""
    objective = lambda s: (s - 0.5) ** 2  # noqa: E731
    res = minimize_1d(objective, OptimizerConfig(), initial_bracket=(1.0, -1.0))
    assert res.best_fx <= res.coarse_best_fx + 1e-12


def test_resultado_siempre_finito():
    """Ningún camino puede publicar NaN/inf."""
    for obj in (
        lambda s: (s - 0.5) ** 2,
        lambda s: 1.0,
        lambda s: 10.0 + 5.0 * abs(s - 3.9),
        lambda s: 2.0 + 1e-14 * math.sin(s * 1000.0),
    ):
        res = minimize_1d(obj, OptimizerConfig())
        assert math.isfinite(res.best_x)
        assert math.isfinite(res.best_fx)


# ------------------------------------------------------------------ metadatos de evidencia
def test_metadatos_declarar_cobertura_no_global():
    """El resultado debe declarar honestamente que NO prueba optimalidad global."""
    res = minimize_1d(lambda s: (s - 0.5) ** 2, OptimizerConfig())
    d = res.as_dict()
    assert d.get("global_optimum_proven") is False, "el optimizador no puede afirmar optimalidad global"
    assert d.get("search_scope") in {"COARSE_GLOBAL_DOMAIN_LOCAL_REFINEMENT", None} or isinstance(
        d.get("search_scope"), str
    )
