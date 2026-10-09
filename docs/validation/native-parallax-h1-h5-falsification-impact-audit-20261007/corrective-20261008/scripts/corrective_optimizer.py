"""F1 — Minimizador escalar 1-D determinista para el oráculo de strength de H1.

El oráculo histórico (`OracleOnly.normal_height_residual_oracle`) y la sonda
"continua" de la auditoría #700 recorren **arrays finitos** de candidatos. Ni la
rejilla histórica ni `s0*exp2(linspace(-2,2,161)) ∪ linspace(-0.05,0.05,201)`
son minimización continua: no tienen bracketing, ni criterio de convergencia, ni
detección de borde. `polyhaven_gray_rocks` cae en el borde `0.05` con cero mejora,
lo que prueba que el óptimo puede estar fuera del rango buscado.

Este módulo implementa la estrategia de §13 del brief correctivo:

    1. barrido grueso suficientemente amplio (log-uniforme sobre magnitudes)
    2. detección de TODAS las cuencas (mínimos locales) del barrido grueso
    3. refinamiento continuo por búsqueda de sección áurea (determinista)
    4. detección de golpe de borde
    5. expansión del bracket cuando el borde se golpea
    6. falla explícita si no hay bracket finito / no converge

## Alcance de la búsqueda (segunda ronda correctiva — finding A)

El optimizador **no** prueba optimalidad global y no debe presentarse como tal.
Lo que sí garantiza:

    - barrido grueso sobre un dominio declarado y acotado (`coarse_max_magnitude`)
    - refinamiento local de cada cuenca detectada por ese barrido
    - el resultado publicado es el **mejor observado/refinado**, nunca uno peor

Si una cuenca es más angosta que el espaciado del barrido grueso, este método
puede no verla. Por eso el resultado emite `global_optimum_proven = False` y
`search_scope = "COARSE_GLOBAL_DOMAIN_LOCAL_REFINEMENT"`.

## Invariante de corrección (segunda ronda correctiva — findings D y E)

    D)  final_objective <= best_observed_objective  (+ tolerancia numérica)
        El refinamiento JAMÁS puede reemplazar un resultado coarse mejor por uno peor.
        Un bracket aportado por el caller sólo se usa si **contiene** al candidato que
        se pretende refinar; si no, se rechaza.

    E)  STATUS_CONVERGED exige, simultáneamente:
            VALID_MINIMUM_BRACKET        = sí
            REFINEMENT_STOP_CRITERION_MET = sí (no agotamiento de max_iterations)
            RESULT_FINITE                 = sí
        "No toqué el borde" NO es prueba de convergencia.

    `VALID_MINIMUM_BRACKET` **no** significa "el punto medio cae dentro del intervalo":
    exige evidencia de un mínimo interior (ver `is_valid_minimum_bracket`). Con la
    condición débil, un objetivo plano o monótono se declaraba con bracket válido.

Contrato de determinismo (brief §28): sin aleatoriedad, sin SciPy; tolerancia,
iteraciones máximas y regla de expansión son parámetros congelados y se emiten en
los metadatos de convergencia de cada asset.

Uso:
    from corrective_optimizer import minimize_1d, OptimizerConfig

    cfg = OptimizerConfig()
    res = minimize_1d(objective, cfg)
    res.status  # "CONVERGED" | "BOUNDARY_UNRESOLVED" | "MAX_EXPANSIONS" | ...
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["OptimizerConfig", "OptimizeResult", "minimize_1d", "golden_section", "is_valid_minimum_bracket"]

# Constante de la sección áurea: 1/phi = (sqrt(5) - 1) / 2
_INV_PHI = (math.sqrt(5.0) - 1.0) / 2.0

STATUS_CONVERGED = "CONVERGED"
STATUS_BOUNDARY = "BOUNDARY_UNRESOLVED"
STATUS_MAX_EXPANSIONS = "MAX_EXPANSIONS"
STATUS_NO_BRACKET = "NO_BRACKET"
# --- status nuevos de la segunda ronda correctiva (finding E)
# `FLAT_OBJECTIVE` describe la FORMA del objetivo (sin variación sobre el barrido grueso).
# NO se emite por una evaluación no finita: eso es un problema de evaluabilidad y cae por
# `result_finite` (NO_BRACKET). Ver el bloque de detección dentro de `minimize_1d`.
STATUS_FLAT = "FLAT_OBJECTIVE"
STATUS_NO_VALID_BRACKET = "NO_VALID_BRACKET"
STATUS_MAX_ITERATIONS = "MAX_ITERATIONS"

SEARCH_SCOPE = "COARSE_GLOBAL_DOMAIN_LOCAL_REFINEMENT"


@dataclass(frozen=True)
class OptimizerConfig:
    """Parámetros congelados del optimizador (determinista por construcción).

    El dominio **no** está fijado a `[-5, 5]` ni a `[-0.05, 0.05]`: `coarse_max_magnitude`
    es el alcance del barrido grueso y el bracket se expande hasta `bracket_max_extent`.
    """

    # --- barrido grueso: log-uniforme en magnitud (cubre décadas) + cero
    coarse_max_magnitude: float = 4.0  # |s| hasta 4.0 (supera el histórico 32x)
    coarse_decades_below: float = 6.0  # baja hasta 4e-6 (cubre |s*| ~ 1e-3)
    coarse_points: int = 161  # puntos por signo, log-espaciados

    # --- refinamiento continuo
    tolerance: float = 1e-8  # tolerancia sobre |x_hi - x_lo|
    metric_tolerance: float = 1e-12  # tolerancia sobre mejora de la métrica
    max_iterations: int = 200  # iteraciones de sección áurea por bracket
    boundary_eps: float = 1e-9  # fracción del extenso para declarar "toca borde"

    # --- expansión de bracket
    max_bracket_expansions: int = 12
    bracket_expansion_factor: float = 3.0
    bracket_max_extent: float = 1e4  # techo absoluto de expansión (anti-divergencia)

    # --- cuencas (segunda ronda correctiva)
    max_refined_basins: int = 8  # cuántas cuencas del barrido grueso se refinan
    flat_tolerance: float = 1e-12  # rango del barrido por debajo del cual es "plano"

    def as_dict(self) -> dict[str, Any]:
        return {
            "coarse_max_magnitude": self.coarse_max_magnitude,
            "coarse_decades_below": self.coarse_decades_below,
            "coarse_points": self.coarse_points,
            "tolerance": self.tolerance,
            "metric_tolerance": self.metric_tolerance,
            "max_iterations": self.max_iterations,
            "boundary_eps": self.boundary_eps,
            "max_bracket_expansions": self.max_bracket_expansions,
            "bracket_expansion_factor": self.bracket_expansion_factor,
            "bracket_max_extent": self.bracket_max_extent,
            "max_refined_basins": self.max_refined_basins,
            "flat_tolerance": self.flat_tolerance,
            "algorithm": "coarse_log_sweep + multi_basin_bracketing + golden_section",
            "deterministic": True,
            "scipy_dependency": False,
        }


@dataclass
class OptimizeResult:
    """Resultado con metadatos de convergencia completos (brief §13)."""

    best_x: float
    best_fx: float
    status: str
    converged: bool
    iterations: int
    n_evaluations: int = 0
    boundary_expansions: int = 0
    coarse_best_x: float = float("nan")
    coarse_best_fx: float = float("nan")
    bracket_low: float = float("nan")
    bracket_high: float = float("nan")
    boundary_hit: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    # --- evidencia de convergencia (finding E)
    valid_minimum_bracket: bool = False
    stop_criterion_met: bool = False
    result_finite: bool = True
    n_basins_refined: int = 0
    n_basins_detected: int = 0
    # --- provenance del bracket del caller (finding D)
    caller_bracket_accepted: bool = False
    # --- alcance honesto de la búsqueda (finding A)
    search_scope: str = SEARCH_SCOPE
    global_optimum_proven: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "continuous_best_strength": self.best_x,
            "continuous_best_objective": self.best_fx,
            "status": self.status,
            "converged": self.converged,
            "iterations": self.iterations,
            "n_evaluations": self.n_evaluations,
            "boundary_expansions": self.boundary_expansions,
            "coarse_best": self.coarse_best_x,
            "coarse_best_objective": self.coarse_best_fx,
            "bracket_low": self.bracket_low,
            "bracket_high": self.bracket_high,
            "boundary_hit": self.boundary_hit,
            "valid_minimum_bracket": self.valid_minimum_bracket,
            "stop_criterion_met": self.stop_criterion_met,
            "result_finite": self.result_finite,
            "n_basins_detected": self.n_basins_detected,
            "n_basins_refined": self.n_basins_refined,
            "caller_bracket_accepted": self.caller_bracket_accepted,
            "search_scope": self.search_scope,
            "global_optimum_proven": self.global_optimum_proven,
            "config": self.config,
        }


def _coarse_candidates(cfg: OptimizerConfig) -> list[float]:
    """Barrido grueso log-uniforme en magnitud, simétrico, más el cero exacto.

    Determinista: sólo depende de `cfg`. Cubre desde `max/10**decades` hasta `max`
    en `coarse_points` pasos logarítmicos, para ambos signos, más `0.0`.
    """
    lo = cfg.coarse_max_magnitude / (10.0**cfg.coarse_decades_below)
    hi = cfg.coarse_max_magnitude
    if cfg.coarse_points < 2:
        raise ValueError("coarse_points debe ser >= 2")
    logs = [
        math.log10(lo) + (math.log10(hi) - math.log10(lo)) * i / (cfg.coarse_points - 1)
        for i in range(cfg.coarse_points)
    ]
    mags = [10.0**v for v in logs]
    cands: list[float] = [0.0]
    for m in mags:
        cands.append(m)
        cands.append(-m)
    return cands


def _golden_section_detailed(
    f: Callable[[float], float],
    lo: float,
    hi: float,
    *,
    tolerance: float,
    metric_tolerance: float,
    max_iterations: int,
) -> tuple[float, float, int, int, bool]:
    """Sección áurea con bandera de "criterio de parada cumplido".

    Devuelve `(x*, f(x*), iteraciones, evaluaciones, stop_criterion_met)`.
    `stop_criterion_met` es False si el bucle terminó por agotar `max_iterations`
    en vez de por satisfacer `tolerance` o `metric_tolerance`. Sin esa bandera,
    "no toqué el borde" se confundía con convergencia (finding E).
    """
    if not (hi > lo):
        raise ValueError(f"golden_section: hi ({hi}) debe ser > lo ({lo})")

    inv_phi = _INV_PHI
    a, b = float(lo), float(hi)
    c = b - inv_phi * (b - a)
    d = a + inv_phi * (b - a)
    fc, fd = f(c), f(d)
    iters = 0
    evals = 2
    best_val = min(fc, fd)
    stop_met = False
    while iters < max_iterations and (b - a) > tolerance:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - inv_phi * (b - a)
            fc = f(c)
            evals += 1
        else:
            a, c, fc = c, d, fd
            d = a + inv_phi * (b - a)
            fd = f(d)
            evals += 1
        iters += 1
        # Parada por mejora despreciable SOLO sobre el valor realmente mejorado (el nuevo
        # punto evaluado), no sobre un `min` que puede repetir el valor reutilizado.
        newly = fc if fc <= fd else fd
        if newly < best_val:
            if (best_val - newly) < metric_tolerance:
                best_val = newly
                stop_met = True
                break
            best_val = newly
    if iters < max_iterations and (b - a) <= tolerance:
        stop_met = True
    if fc <= fd:
        return c, fc, iters, evals, stop_met
    return d, fd, iters, evals, stop_met


def golden_section(
    f: Callable[[float], float],
    lo: float,
    hi: float,
    *,
    tolerance: float,
    metric_tolerance: float,
    max_iterations: int,
) -> tuple[float, float, int, int]:
    """Minimización de sección áurea sobre `[lo, hi]` (unimodal en el bracket).

    Determinista y sin dependencias. Devuelve `(x*, f(x*), iteraciones, evaluaciones)`.
    Criterio de parada: intervalo < `tolerance` **o** mejora de métrica < `metric_tolerance`.

    Se conserva la firma de 4 elementos por compatibilidad; la versión con la bandera
    de criterio de parada es `_golden_section_detailed`.
    """
    x, fx, iters, evals, _ = _golden_section_detailed(
        f,
        lo,
        hi,
        tolerance=tolerance,
        metric_tolerance=metric_tolerance,
        max_iterations=max_iterations,
    )
    return x, fx, iters, evals


def is_valid_minimum_bracket(
    f: Callable[[float], float],
    lo: float,
    mid: float,
    hi: float,
    *,
    tolerance: float,
) -> bool:
    """¿El bracket `(lo, mid, hi)` aporta evidencia de un MÍNIMO INTERIOR?

    Contrato (bracketing estándar de tres puntos):

        lo < mid < hi
        AND f(mid) < f(lo) - tolerance
        AND f(mid) < f(hi) - tolerance

    Para `f` continua esto **implica** un mínimo local estrictamente dentro de
    `(lo, hi)`: el mínimo de `f` sobre `[lo, hi]` no puede estar en `lo` ni en `hi`
    (ambos son peores que `mid`), así que cae en el interior y allí es un mínimo local.

    La condición `lo < mid < hi` **sola no alcanza**: un objetivo plano o monótono la
    cumple y no tiene ningún mínimo interior. Tercera ronda correctiva: el código previo
    usaba sólo esa condición y por eso un objetivo plano o monótono salía con
    `VALID_MINIMUM_BRACKET = YES`.

    `tolerance` es la tolerancia de métrica del propio optimizador
    (`OptimizerConfig.metric_tolerance`): una mejora menor que ella no cuenta como
    evidencia. Fail-closed: si algún valor no es finito, devuelve `False`.
    """
    if not (lo < mid < hi):
        return False
    f_lo, f_mid, f_hi = f(lo), f(mid), f(hi)
    if not (math.isfinite(f_lo) and math.isfinite(f_mid) and math.isfinite(f_hi)):
        return False
    return f_mid < f_lo - tolerance and f_mid < f_hi - tolerance


def _boundary_eps(cfg: OptimizerConfig, span: float) -> float:
    """Umbral para declarar que el punto refinado tocó el borde del bracket.

    Incluye `cfg.tolerance` porque la sección áurea detiene cuando el intervalo baja
    de esa tolerancia, de modo que el punto devuelto puede quedar hasta `tolerance`
    alejado del extremo real. Sin este término, un óptimo pegado al borde se
    reportaba como interior (el defecto que hacía pasar por `CONVERGED` al caso
    `polyhaven_gray_rocks`, finding E).
    """
    return max(cfg.boundary_eps * abs(span), cfg.boundary_eps, cfg.tolerance)


def _local_minima_indices(fx: list[float]) -> list[int]:
    """Índices de mínimos locales de una secuencia (con meseta tratada como un punto)."""
    n = len(fx)
    if n < 3:
        return []
    out: list[int] = []
    i = 0
    while i < n:
        # agrupa mesetas contiguas de valor igual
        j = i
        while j + 1 < n and fx[j + 1] == fx[i]:
            j += 1
        if i > 0 and j < n - 1 and fx[i] < fx[i - 1] and fx[j] < fx[j + 1]:
            out.append((i + j) // 2)
        i = j + 1
    return out


def _basins_from_coarse(
    coarse: list[tuple[float, float]], max_basins: int, span: float
) -> list[tuple[float, float, float]]:
    """Brackets `(x_lo, x_mid, x_hi)` de las mejores cuencas del barrido grueso.

    Se detectan los mínimos locales de la secuencia ordenada por `x` y se devuelven
    los `max_basins` de menor objetivo, ordenados por objetivo ascendente (determinista).

    Si el mejor punto del barrido cae en el **extremo del dominio** (no hay mínimo local
    interior que lo contenga), se agrega un bracket simétrico de semiancho `span`
    centrado en él: es el que permite que la expansión de borde encuentre un óptimo
    que vive fuera del dominio barrido. Sin este caso, un óptimo lejano se perdía.
    """
    ordered = sorted(coarse, key=lambda t: t[0])
    xs = [t[0] for t in ordered]
    fx = [t[1] for t in ordered]
    n = len(xs)
    if n == 0:
        return []

    idx = _local_minima_indices(fx)
    idx_sorted = sorted(idx, key=lambda k: (fx[k], xs[k]))
    picked = idx_sorted[:max_basins]

    brackets: list[tuple[float, float, float]] = []
    for k in picked:
        lo_i = max(0, k - 1)
        hi_i = min(n - 1, k + 1)
        if hi_i > lo_i:
            brackets.append((xs[lo_i], xs[k], xs[hi_i]))

    # --- caso borde: el óptimo del barrido no está contenido en ningún bracket interior
    best_k = min(range(n), key=lambda k: (fx[k], xs[k]))
    contained = any(lo <= xs[best_k] <= hi for lo, _m, hi in brackets)
    if not contained:
        cx = xs[best_k]
        brackets.append((cx - span, cx, cx + span))

    return brackets


def minimize_1d(
    objective: Callable[[float], float],
    cfg: OptimizerConfig | None = None,
    *,
    initial_bracket: tuple[float, float] | None = None,
) -> OptimizeResult:
    """Minimiza `objective` en 1-D con barrido grueso + bracketing + sección áurea.

    `objective` debe ser determinista. No se asume unimodalidad global: se detectan
    las cuencas del barrido grueso, se refina cada una y se publica el **mejor
    resultado observado**, nunca uno peor que el mejor ya evaluado.

    `initial_bracket` es una **sugerencia** del caller: sólo se usa si contiene al
    candidato coarse que se pretende refinar. Un bracket incompatible se rechaza en
    vez de reemplazar un resultado mejor por uno peor (finding D).
    """
    cfg = cfg or OptimizerConfig()
    n_evals = 0

    def f(x: float) -> float:
        nonlocal n_evals
        n_evals += 1
        return float(objective(float(x)))

    # ---- 1) barrido grueso
    cands = _coarse_candidates(cfg)
    coarse: list[tuple[float, float]] = []
    for x in cands:
        coarse.append((x, f(x)))
    cb_x, cb_fx = min(coarse, key=lambda t: t[1])

    # ---- ancla del invariante D: mejor valor ya OBSERVADO
    best_x, best_fx = cb_x, cb_fx

    # ---- detección de objetivo plano: sin variación no hay mínimo que declarar
    #
    # `flat` describe la FORMA del objetivo (no varía), NO la calidad de la evaluación.
    # La versión previa mezclaba ambas cosas en una sola condición: si CUALQUIER punto del
    # barrido devolvía NaN/inf, `spread` quedaba en `inf` y el objetivo entero se clasificaba
    # como plano (`STATUS_FLAT`, `converged=False`), ocultando que el problema era de
    # evaluabilidad y no de forma. Hallazgo del Oracle sobre el HEAD 3ba2609e.
    #
    # Ahora: sin ningún valor finito no hay nada que minimizar (fail-closed: cae por
    # `result_finite` más abajo), y con valores finitos `flat` depende sólo del rango
    # observado. El corpus actual no tiene valores no finitos, así que el cambio es
    # inobservable en la evidencia publicada.
    vals = [v for _, v in coarse]
    finite_vals = [v for v in vals if math.isfinite(v)]
    spread = (max(finite_vals) - min(finite_vals)) if finite_vals else 0.0
    flat = bool(finite_vals) and spread <= cfg.flat_tolerance

    # ---- 2) cuencas candidatas del barrido grueso
    basins = _basins_from_coarse(coarse, cfg.max_refined_basins, cfg.coarse_max_magnitude)
    n_basins_detected = len(basins)

    # Sugerencia del caller: SÓLO válida si contiene al mejor coarse (finding D).
    caller_used = False
    if initial_bracket is not None:
        lo_c, hi_c = sorted((float(initial_bracket[0]), float(initial_bracket[1])))
        if hi_c > lo_c and lo_c <= cb_x <= hi_c:
            # Refina el bracket del caller, pero conservando el punto medio real.
            basins = [(lo_c, cb_x, hi_c), *basins]
            caller_used = True
    if not basins:
        span = cfg.coarse_max_magnitude
        basins = [(cb_x - span, cb_x, cb_x + span)]
        n_basins_detected = 0

    bracket_low, bracket_high = float("nan"), float("nan")
    boundary_hit = False
    expansions_total = 0
    iters_total = 0
    stop_met_any = False
    valid_bracket = False
    refined_any = False

    # ---- 3) refinamiento de cada cuenca, conservando el mejor resultado observado
    for b_lo, b_mid, b_hi in basins:
        lo, hi = float(b_lo), float(b_hi)
        if not (hi > lo):
            continue
        # Un bracket es válido sólo si APORTA evidencia de un mínimo interior. No alcanza
        # con que el punto medio sea interior al intervalo: un objetivo plano o monótono
        # también lo cumple y no tiene ningún mínimo interior que buscar.
        # Ver `is_valid_minimum_bracket` para el contrato y su justificación.
        if is_valid_minimum_bracket(f, lo, float(b_mid), hi, tolerance=cfg.metric_tolerance):
            valid_bracket = True
        bx, bfx, it, _, stop_met = _golden_section_detailed(
            f,
            lo,
            hi,
            tolerance=cfg.tolerance,
            metric_tolerance=cfg.metric_tolerance,
            max_iterations=cfg.max_iterations,
        )
        stop_met_any = stop_met_any or stop_met
        iters_total += it
        refined_any = True
        # INVARIANTE D: sólo se adopta el refinado si MEJORA lo ya observado.
        if bfx < best_fx:
            best_x, best_fx = bx, bfx
        if math.isfinite(bx):
            if math.isnan(bracket_low) or lo < bracket_low:
                bracket_low = lo
            if math.isnan(bracket_high) or hi > bracket_high:
                bracket_high = hi
        # ---- 4/5) borde + expansión, sólo sobre la cuenca que produjo el mejor valor
        if bfx <= best_fx + 1e-15:
            span = hi - lo
            eps = _boundary_eps(cfg, span)
            if abs(bx - lo) <= eps or abs(bx - hi) <= eps:
                boundary_hit = True
                expansions = 0
                cur_lo, cur_hi = lo, hi
                while boundary_hit and expansions < cfg.max_bracket_expansions:
                    span = cur_hi - cur_lo
                    n_lo = cur_lo - cfg.bracket_expansion_factor * span
                    n_hi = cur_hi + cfg.bracket_expansion_factor * span
                    if n_hi - n_lo > cfg.bracket_max_extent:
                        n_lo = -cfg.bracket_max_extent
                        n_hi = cfg.bracket_max_extent
                    if not (n_hi > n_lo):
                        break
                    ex, efx, eit, _, ex_stop = _golden_section_detailed(
                        f,
                        n_lo,
                        n_hi,
                        tolerance=cfg.tolerance,
                        metric_tolerance=cfg.metric_tolerance,
                        max_iterations=cfg.max_iterations,
                    )
                    stop_met_any = stop_met_any or ex_stop
                    iters_total += eit
                    if efx < best_fx:
                        best_x, best_fx = ex, efx
                    if math.isfinite(ex):
                        bracket_low = min(bracket_low, n_lo)
                        bracket_high = max(bracket_high, n_hi)
                    cur_lo, cur_hi = n_lo, n_hi
                    span = cur_hi - cur_lo
                    eps = _boundary_eps(cfg, span)
                    boundary_hit = bool(abs(ex - cur_lo) <= eps or abs(ex - cur_hi) <= eps)
                    expansions += 1
                expansions_total = max(expansions_total, expansions)

    result_finite = math.isfinite(best_x) and math.isfinite(best_fx)

    # ---- 6) status con evidencia real de convergencia (finding E)
    if flat:
        status, converged = STATUS_FLAT, False
    elif not result_finite:
        status, converged = STATUS_NO_BRACKET, False
    elif boundary_hit and expansions_total >= cfg.max_bracket_expansions:
        status, converged = STATUS_MAX_EXPANSIONS, False
    elif boundary_hit:
        status, converged = STATUS_BOUNDARY, False
    elif not valid_bracket:
        status, converged = STATUS_NO_VALID_BRACKET, False
    elif not stop_met_any:
        status, converged = STATUS_MAX_ITERATIONS, False
    else:
        status, converged = STATUS_CONVERGED, True

    if math.isnan(bracket_low):
        bracket_low = float("nan")
    if math.isnan(bracket_high):
        bracket_high = float("nan")

    return OptimizeResult(
        best_x=best_x,
        best_fx=best_fx,
        status=status,
        converged=converged,
        iterations=iters_total,
        n_evaluations=n_evals,
        boundary_expansions=expansions_total,
        coarse_best_x=cb_x,
        coarse_best_fx=cb_fx,
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        boundary_hit=boundary_hit,
        config=cfg.as_dict(),
        valid_minimum_bracket=valid_bracket,
        stop_criterion_met=stop_met_any,
        result_finite=result_finite,
        n_basins_detected=n_basins_detected,
        n_basins_refined=len(basins) if refined_any else 0,
        caller_bracket_accepted=caller_used,
        search_scope=SEARCH_SCOPE,
        global_optimum_proven=False,
    )
