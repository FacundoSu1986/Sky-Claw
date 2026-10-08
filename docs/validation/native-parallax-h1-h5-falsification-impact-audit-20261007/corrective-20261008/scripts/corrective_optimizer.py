"""F1 — Minimizador escalar 1-D determinista para el oráculo de strength de H1.

El oráculo histórico (`OracleOnly.normal_height_residual_oracle`) y la sonda
"continua" de la auditoría #700 recorren **arrays finitos** de candidatos. Ni la
rejilla histórica ni `s0*exp2(linspace(-2,2,161)) ∪ linspace(-0.05,0.05,201)`
son minimización continua: no tienen bracketing, ni criterio de convergencia, ni
detección de borde. `polyhaven_gray_rocks` cae en el borde `0.05` con cero mejora,
lo que prueba que el óptimo puede estar fuera del rango buscado.

Este módulo implementa la estrategia de §13 del brief correctivo:

    1. barrido grueso suficientemente amplio (log-uniforme sobre magnitudes)
    2. bracket del mejor basin (trío x_lo < x_mid < x_hi con f(x_mid) < ambos)
    3. refinamiento continuo por búsqueda de sección áurea (determinista)
    4. detección de golpe de borde
    5. expansión del bracket cuando el borde se golpea
    6. falla explícita si no hay bracket finito / no converge

Contrato de determinismo (brief §28): sin aleatoriedad, sin SciPy; tolerancia,
iteraciones máximas y regla de expansión son parámetros congelados y se emiten en
los metadatos de convergencia de cada asset.

Uso:
    from corrective_optimizer import minimize_1d, OptimizerConfig

    cfg = OptimizerConfig()
    res = minimize_1d(objective, cfg)
    res.status  # "CONVERGED" | "BOUNDARY_UNRESOLVED" | "MAX_EXPANSIONS"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["OptimizerConfig", "OptimizeResult", "minimize_1d", "golden_section"]

# Constante de la sección áurea: 1/phi = (sqrt(5) - 1) / 2
_INV_PHI = (math.sqrt(5.0) - 1.0) / 2.0

STATUS_CONVERGED = "CONVERGED"
STATUS_BOUNDARY = "BOUNDARY_UNRESOLVED"
STATUS_MAX_EXPANSIONS = "MAX_EXPANSIONS"
STATUS_NO_BRACKET = "NO_BRACKET"


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
            "algorithm": "coarse_log_sweep + golden_section",
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
                break
            best_val = newly
    if fc <= fd:
        return c, fc, iters, evals
    return d, fd, iters, evals


def _bracket_from_coarse(
    coarse: list[tuple[float, float]],
) -> tuple[float, float, float] | None:
    """Trío de bracketing a partir del barrido grueso ordenado por x.

    Devuelve `(x_lo, x_mid, x_hi)` con `f(x_mid)` menor que ambos vecinos, o `None`
    si el mínimo del barrido está en un extremo del dominio (sin bracket interior).
    """
    ordered = sorted(coarse, key=lambda t: t[0])
    xs = [t[0] for t in ordered]
    fx = [t[1] for t in ordered]
    n = len(ordered)
    if n < 3:
        return None
    i = min(range(n), key=lambda k: fx[k])
    if i == 0 or i == n - 1:
        return None  # borde del barrido grueso: requiere expansión
    return xs[i - 1], xs[i], xs[i + 1]


def minimize_1d(
    objective: Callable[[float], float],
    cfg: OptimizerConfig | None = None,
    *,
    initial_bracket: tuple[float, float] | None = None,
) -> OptimizeResult:
    """Minimiza `objective` en 1-D con barrido grueso + bracketing + sección áurea.

    `objective` debe ser determinista. No se asume unimodalidad global: el bracket
    se ancla en el mejor punto del barrido grueso y se expande si toca el borde.
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

    # ---- 2) bracket inicial: el del barrido grueso, o el provisto por el caller
    expansions = 0
    bracket = _bracket_from_coarse(coarse)
    if initial_bracket is not None:
        bracket = (float(initial_bracket[0]), float(cb_x), float(initial_bracket[1]))
    if bracket is None:
        # el óptimo del barrido cayó en el extremo -> bracket simétrico a expandir
        span = cfg.coarse_max_magnitude
        bracket = (cb_x - span, cb_x, cb_x + span)

    bracket_low, _, bracket_high = bracket
    boundary_hit = False
    best_x, best_fx = cb_x, cb_fx
    iters_total = 0

    # ---- 3) refinamiento con expansión en caso de borde
    while True:
        lo = bracket[0]
        hi = bracket[2]
        bx, bfx, it, _ = golden_section(
            f,
            lo,
            hi,
            tolerance=cfg.tolerance,
            metric_tolerance=cfg.metric_tolerance,
            max_iterations=cfg.max_iterations,
        )
        iters_total += it
        best_x, best_fx = bx, bfx

        # ---- 4) detección de golpe de borde
        span = hi - lo
        eps = max(cfg.boundary_eps * abs(span), cfg.boundary_eps)
        boundary_hit = bool(abs(bx - lo) <= eps or abs(bx - hi) <= eps)

        # ---- 5) expansión
        if not boundary_hit:
            break
        if expansions >= cfg.max_bracket_expansions:
            break
        new_lo = lo - cfg.bracket_expansion_factor * span
        new_hi = hi + cfg.bracket_expansion_factor * span
        if new_hi - new_lo > cfg.bracket_max_extent:
            new_lo = -cfg.bracket_max_extent
            new_hi = cfg.bracket_max_extent
        bracket = (new_lo, bx, new_hi)
        bracket_low, bracket_high = new_lo, new_hi
        expansions += 1
        if expansions >= cfg.max_bracket_expansions:
            break

    if boundary_hit:
        status = (
            STATUS_MAX_EXPANSIONS if expansions >= cfg.max_bracket_expansions else STATUS_BOUNDARY
        )
        converged = False
    else:
        status = STATUS_CONVERGED
        converged = True

    return OptimizeResult(
        best_x=best_x,
        best_fx=best_fx,
        status=status,
        converged=converged,
        iterations=iters_total,
        n_evaluations=n_evals,
        boundary_expansions=expansions,
        coarse_best_x=cb_x,
        coarse_best_fx=cb_fx,
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        boundary_hit=boundary_hit,
        config=cfg.as_dict(),
    )
