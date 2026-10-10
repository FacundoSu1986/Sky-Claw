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

Tercera ronda (2026-10-09) — `VALID_MINIMUM_BRACKET` con evidencia real de mínimo
   El código de la ronda 2 marcaba

       valid_minimum_bracket = True   <=>   lo < b_mid < hi

   Eso sólo prueba que el punto medio es **interior al intervalo**, no que exista
   evidencia de un mínimo interior. Un objetivo plano o monótono recibía
   `VALID_MINIMUM_BRACKET = YES`. El contrato correcto es el bracketing estándar de
   tres puntos, con la tolerancia de métrica del propio optimizador:

       lo < mid < hi  AND  f(mid) < f(lo) - tol  AND  f(mid) < f(hi) - tol

   Para `f` continua eso SÍ implica un mínimo local estrictamente dentro de `(lo, hi)`.
   Los tests de la sección F se escribieron **antes** del fix (rojo → verde).

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


# ------------------------------------------------------------------ F: bracket con evidencia real
# Tercera ronda correctiva. `valid_minimum_bracket` significa "hay evidencia de un mínimo
# interior bracketed", no "el punto medio cae dentro del intervalo". Reproducción del
# defecto sobre el código previo al fix:
#
#   caso                                status            valid_minimum_bracket
#   plano f=1.0                         FLAT_OBJECTIVE    True   <-- no hay mínimo alguno
#   monotono f=10+5s                    MAX_EXPANSIONS    True   <-- mínimo en el borde
#   monotono f=10-5s                    MAX_EXPANSIONS    True   <-- mínimo en el borde
#   casi-plano 2+1e-14*sin(1000s)       FLAT_OBJECTIVE    True   <-- ruido despreciable
#
# La familia se enumera por forma del objetivo, no por un caso suelto: plano, monótono en
# ambos sentidos, casi-plano, mínimo interior estricto, kink y bracket del caller.


@pytest.mark.parametrize(
    "nombre, objective",
    [
        ("plano", lambda s: 1.0),
        ("monotono creciente", lambda s: 10.0 + 5.0 * s),
        ("monotono decreciente", lambda s: 10.0 - 5.0 * s),
        ("casi plano", lambda s: 2.0 + 1e-14 * math.sin(s * 1000.0)),
    ],
)
def test_sin_minimo_interior_no_declara_bracket_valido(nombre, objective):
    """Sin evidencia de mínimo interior, `valid_minimum_bracket` debe ser False."""
    res = minimize_1d(objective, OptimizerConfig())
    assert res.valid_minimum_bracket is False, (
        f"{nombre}: se declaró VALID_MINIMUM_BRACKET=YES sin evidencia de mínimo interior "
        f"(status={res.status!r}, boundary_hit={res.boundary_hit!r})"
    )
    assert not res.converged


@pytest.mark.parametrize(
    "nombre, objective",
    [
        ("parabola", lambda s: (s - 0.5) ** 2),
        ("kink", lambda s: 10.0 + 5.0 * abs(s - 3.9)),
        ("cuenca angosta", lambda s: 1.0 + 50.0 * (s - 0.02) ** 2),
    ],
)
def test_minimo_interior_estricto_si_declara_bracket_valido(nombre, objective):
    """Contraprueba: con mínimo interior real, el bracket sigue siendo válido y converge."""
    res = minimize_1d(objective, OptimizerConfig())
    assert res.valid_minimum_bracket is True, f"{nombre}: se perdió un bracket legítimo"
    assert res.converged is True, f"{nombre}: un mínimo interior debe converger"
    assert res.status == opt.STATUS_CONVERGED


def test_predicado_de_bracket_exige_mejora_estricta_en_el_punto_medio():
    """Contrato del predicado, caso por caso, sin depender del barrido."""
    tol = OptimizerConfig().metric_tolerance

    def ident(s: float) -> float:
        return s

    def valle(s: float) -> float:
        return (s - 1.0) ** 2

    def desplazado(s: float) -> float:
        return (s - 0.5) ** 2

    def no_finito(s: float) -> float:
        return math.nan

    # punto medio interior pero objetivo monótono -> NO hay mínimo interior
    assert opt.is_valid_minimum_bracket(ident, 0.0, 1.0, 2.0, tolerance=tol) is False
    # mínimo justo en el punto medio -> bracket válido
    assert opt.is_valid_minimum_bracket(valle, 0.0, 1.0, 2.0, tolerance=tol) is True
    # punto medio NO interior -> no es un bracket
    assert opt.is_valid_minimum_bracket(valle, 1.0, 1.0, 2.0, tolerance=tol) is False
    assert opt.is_valid_minimum_bracket(valle, 2.0, 1.0, 0.0, tolerance=tol) is False
    # f(mid) == f(lo): sin mejora estricta no hay evidencia
    assert opt.is_valid_minimum_bracket(desplazado, 0.0, 1.0, 2.0, tolerance=tol) is False
    # valores no finitos -> fail-closed
    assert opt.is_valid_minimum_bracket(no_finito, 0.0, 1.0, 2.0, tolerance=tol) is False


def test_candidato_en_el_borde_no_declara_bracket_valido():
    """Un óptimo en el borde del dominio NO es un mínimo interior bracketed.

    `f = 10 + 5s` es monótona: su mínimo sobre el dominio barrido está en el extremo, y el
    bracket simétrico que el optimizador arma alrededor del mejor punto coarse no aporta
    evidencia de un mínimo interior. La expansión de borde sigue funcionando; lo que cambia
    es que el bracket no se declara válido.
    """
    res = minimize_1d(lambda s: 10.0 + 5.0 * s, OptimizerConfig())
    assert res.boundary_hit is True or res.status in {opt.STATUS_MAX_EXPANSIONS, opt.STATUS_BOUNDARY}
    assert res.valid_minimum_bracket is False
    assert res.converged is False


def test_objetivo_plano_no_se_convierte_en_bracket_valido():
    """Brief §15: un objetivo plano NO puede volverse VALID_MINIMUM_BRACKET=YES."""
    res = minimize_1d(lambda s: 1.0, OptimizerConfig())
    assert res.status == opt.STATUS_FLAT
    assert res.valid_minimum_bracket is False
    assert res.converged is False


def test_resultado_finito_sigue_siendo_el_contrato_previo():
    """Los contratos anteriores de convergencia no se debilitan con el fix del bracket."""
    res = minimize_1d(lambda s: (s - 0.5) ** 2, OptimizerConfig())
    assert res.result_finite is True
    assert res.stop_criterion_met is True
    assert res.valid_minimum_bracket is True
    assert res.status == opt.STATUS_CONVERGED


# ------------------------------------------- Cuarta ronda: plano vs no-evaluable
# Finding del Regression & Test Oracle sobre el HEAD 3ba2609e:
# `flat` describía DOS cosas a la vez (forma del objetivo y calidad de la evaluación).
# Una sola evaluación no finita hacía `spread = inf` y clasificaba TODO el barrido como
# plano, forzando `STATUS_FLAT` y `converged=False` aunque el resto fuera informativo.
def test_un_valor_no_finito_no_clasifica_todo_como_plano():
    """Con un tramo no finito pero variación real, el objetivo NO es plano.

    Antes del fix este caso devolvía `STATUS_FLAT`: la región `s < -1` devuelve `inf`, el
    rango del barrido quedaba en `inf` y la clasificación se comía un objetivo que tiene un
    mínimo interior perfectamente refinable.
    """

    def objective(s: float) -> float:
        return float("inf") if s < -1.0 else (s - 0.5) ** 2

    res = minimize_1d(objective, OptimizerConfig())
    assert res.status != opt.STATUS_FLAT, f"clasificado como plano: {res.status}"
    assert res.converged is True
    assert res.valid_minimum_bracket is True
    assert res.best_x == pytest.approx(0.5, abs=1e-4)


def test_objetivo_realmente_plano_sigue_siendo_flat():
    """El fix no puede sobre-corregir: sin variación REAL el veredicto sigue siendo FLAT."""
    res = minimize_1d(lambda s: 1.0, OptimizerConfig())
    assert res.status == opt.STATUS_FLAT
    assert res.converged is False


def test_casi_plano_con_tramo_no_finito_sigue_siendo_flat():
    """Casi-plano (rango bajo `flat_tolerance`) con un tramo `inf` sigue siendo FLAT.

    La separación es entre «no varía» y «no se puede evaluar», no entre «varía» y «falla».
    """
    res = minimize_1d(lambda s: float("inf") if s < -1.0 else 1.0 + 1e-14 * s, OptimizerConfig())
    assert res.status == opt.STATUS_FLAT
    assert res.converged is False


def test_sin_ninguna_evaluacion_finita_es_fail_closed():
    """Si NO hay ningún valor finito no hay nada que minimizar: fail-closed, nunca CONVERGED."""
    res = minimize_1d(lambda s: float("inf"), OptimizerConfig())
    assert res.status != opt.STATUS_CONVERGED
    assert res.converged is False
    assert res.result_finite is False


def test_nan_en_el_barrido_no_clasifica_como_plano():
    """Un NaN tampoco puede hacer que TODO el objetivo se declare plano.

    El NaN está lejos del mínimo real (que existe y es refinable), así que el resultado
    correcto es converger al mínimo: lo que NO puede pasar es perder el objetivo entero
    por una evaluación no finita en otra región.
    """

    def objective(s: float) -> float:
        return float("nan") if s > 1.0 else (s - 0.5) ** 2

    res = minimize_1d(objective, OptimizerConfig())
    assert res.status != opt.STATUS_FLAT
    assert res.result_finite is True
    assert res.best_x == pytest.approx(0.5, abs=1e-4)
