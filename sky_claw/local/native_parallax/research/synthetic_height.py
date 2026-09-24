"""Superficies sintéticas deterministas (ground truth perfecto) para NP-M0.

Convención de muestreo (documentada en np-m0-results.md):

- Grid ``(n_rows, n_cols)`` (antes ``H×W``); el índice de fila ``i`` es ``y`` (hacia abajo) y el de columna ``j`` es
  ``x`` (hacia la derecha), coherente con el orden de filas de una textura.
- ``x_j = j / n_cols``, ``y_i = i / n_rows`` — dominio ``[0, 1)²``, interpretado como toro ``T²``.
- Las superficies ``S*`` son **matemáticamente periódicas** en el toro (fracciones,
  fórmulas cerradas de soporte compacto toroidal o suma de sinusoides de frecuencia
  entera). Los controles ``N*`` violan la periodicidad a propósito.
- ``dtype=float64``, amplitud O(1) salvo casos marcados (S13 ~0.01, S14 empinada).
- Toda aleatoriedad usa ``np.random.default_rng(seed)`` con seed fija en el código.

Clean-room: fórmulas propias; sin datasets externos ni herramientas de terceros.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

# Seeds fijas del corpus (reproducibilidad §29 del brief NP-M0).
SEED_BUMPS = 7
SEED_NOISE_SURFACE = 20260921


def _mesh(n_rows: int, n_cols: int) -> tuple[np.ndarray, np.ndarray]:
    """Devuelve ``(xx, yy)`` de shape ``(n_rows, n_cols)``: xx por columnas, yy por filas."""
    y = np.arange(n_rows, dtype=np.float64) / n_rows
    x = np.arange(n_cols, dtype=np.float64) / n_cols
    return np.meshgrid(x, y, indexing="xy")


def _wrap_dist(u: np.ndarray, c: float) -> np.ndarray:
    """Distancia toroidal a ``c`` en [0, 0.5]: mínima distancia considerando el wrap."""
    d = np.abs(u - c) % 1.0
    return np.minimum(d, 1.0 - d)


def _frac(u: np.ndarray) -> np.ndarray:
    return np.asarray(u - np.floor(u), dtype=np.float64)


def s01_flat(n_rows: int, n_cols: int) -> np.ndarray:
    """S01 — plano exacto: H(x,y)=0. Invariante 'flat → flat'."""
    return np.zeros((n_rows, n_cols), dtype=np.float64)


def s02_sine_x(n_rows: int, n_cols: int) -> np.ndarray:
    """S02 — seno único en xx (4 ciclos): caso analítico de referencia."""
    xx, _ = _mesh(n_rows, n_cols)
    return 0.8 * np.sin(2.0 * np.pi * 4.0 * xx)


def s03_sine_y(n_rows: int, n_cols: int) -> np.ndarray:
    """S03 — seno único en yy (4 ciclos): ejercita el eje de filas."""
    _, yy = _mesh(n_rows, n_cols)
    return 0.8 * np.sin(2.0 * np.pi * 4.0 * yy)


def s04_separable(n_rows: int, n_cols: int) -> np.ndarray:
    """S04 — suma separable 3X + 5Y."""
    xx, yy = _mesh(n_rows, n_cols)
    return 0.6 * np.sin(2.0 * np.pi * 3.0 * xx) + 0.4 * np.sin(2.0 * np.pi * 5.0 * yy)


def s05_diagonal(n_rows: int, n_cols: int) -> np.ndarray:
    """S05 — sinusoidal diagonal (3,2): energía fuera de los ejes."""
    xx, yy = _mesh(n_rows, n_cols)
    return np.sin(2.0 * np.pi * (3.0 * xx + 2.0 * yy))


def s06_multifreq(n_rows: int, n_cols: int) -> np.ndarray:
    """S06 — multi-frecuencia periódica (modo mezclado espectral moderado)."""
    xx, yy = _mesh(n_rows, n_cols)
    return (
        0.45 * np.sin(2.0 * np.pi * xx + 0.3)
        + 0.30 * np.sin(2.0 * np.pi * 3.0 * yy + 1.1)
        + 0.15 * np.sin(2.0 * np.pi * (4.0 * xx + 2.0 * yy) + 2.0)
        + 0.10 * np.sin(2.0 * np.pi * (7.0 * xx + 5.0 * yy) + 0.7)
    )


def s07_bumps(n_rows: int, n_cols: int) -> np.ndarray:
    """S07 — bultos suaves periódicos (12 gaussianas con distancia toroidal)."""
    xx, yy = _mesh(n_rows, n_cols)
    rng = np.random.default_rng(SEED_BUMPS)
    h = np.zeros((n_rows, n_cols), dtype=np.float64)
    for _ in range(12):
        cx, cy = float(rng.uniform(0.0, 1.0)), float(rng.uniform(0.0, 1.0))
        sigma = float(rng.uniform(0.05, 0.12))
        amp = float(rng.uniform(0.3, 1.0))
        dx = _wrap_dist(xx, cx)
        dy = _wrap_dist(yy, cy)
        h += amp * np.exp(-(dx * dx + dy * dy) / (2.0 * sigma * sigma))
    return h


def s08_ridges(n_rows: int, n_cols: int) -> np.ndarray:
    """S08 — crestas/surcos triangulares en xx (6 periodos; creases → no band-limited)."""
    xx, _ = _mesh(n_rows, n_cols)
    s = _frac(6.0 * xx)
    return 1.0 - 2.0 * np.abs(2.0 * s - 1.0)


def s09_bricks(n_rows: int, n_cols: int) -> np.ndarray:
    """S09 — relieve tipo ladrillo/mortero: 3x3 ladrillos con junta desfasada por fila.

    Coronas suaves por ladrillo, juntas horizontales en yy=k/3 y verticales desfasadas
    0.5 ladrillo por fila impar. La fase usa ``floor(3Y) % 2`` con 3 filas por dominio
    para que el patrón sea periódico en el borde del toro.
    """
    xx, yy = _mesh(n_rows, n_cols)
    row = np.floor(3.0 * yy)
    row_phase = 0.5 * (row % 2.0)
    u = _frac((xx + row_phase) * 3.0)
    dx_b = np.minimum(u, 1.0 - u) / 3.0
    dy_b = np.abs(_frac(3.0 * yy + 0.5) - 0.5) / 3.0
    sigma = 0.09
    return np.asarray(np.exp(-(dx_b * dx_b + dy_b * dy_b) / (2.0 * sigma * sigma)), dtype=np.float64)


def _g_periodica(u: np.ndarray, c1: float, s1: float, c2: float, s2: float) -> np.ndarray:
    """Perfil 1D periódico asimétrico: dos gaussianas toroidales de distinto peso."""
    return np.exp(-(_wrap_dist(u, c1) ** 2) / (2.0 * s1 * s1)) - 0.6 * np.exp(
        -(_wrap_dist(u, c2) ** 2) / (2.0 * s2 * s2)
    )


def s10_asymmetric(n_rows: int, n_cols: int) -> np.ndarray:
    """S10 — relieve repetido asimétrico (4x4 celdas): sensible a inversión de signo."""
    xx, yy = _mesh(n_rows, n_cols)
    gx = _g_periodica(_frac(4.0 * xx), 0.35, 0.10, 0.80, 0.15)
    gy = _g_periodica(_frac(4.0 * yy), 0.20, 0.12, 0.70, 0.18)
    return 0.8 * gx + 0.4 * gy


def s11_highfreq(n_rows: int, n_cols: int) -> np.ndarray:
    """S11 — alta frecuencia válida periódica (32X + 21Y; muy por debajo de Nyquist)."""
    xx, yy = _mesh(n_rows, n_cols)
    return 0.5 * np.sin(2.0 * np.pi * 32.0 * xx) + 0.3 * np.sin(2.0 * np.pi * 21.0 * yy)


def s12_mixed_scale(n_rows: int, n_cols: int) -> np.ndarray:
    """S12 — mezcla LF+HF: LF (1 ciclo), media (8 diagonal) y HF (24Y)."""
    xx, yy = _mesh(n_rows, n_cols)
    return (
        0.7 * np.sin(2.0 * np.pi * 1.0 * xx + 0.5)
        + 0.2 * np.sin(2.0 * np.pi * (8.0 * xx + 8.0 * yy))
        + 0.1 * np.sin(2.0 * np.pi * 24.0 * yy)
    )


def s13_near_flat(n_rows: int, n_cols: int) -> np.ndarray:
    """S13 — casi plano: misma forma que S06 con amplitud 0.01 (rango dinámico mínimo)."""
    return 0.01 * s06_multifreq(n_rows, n_cols)


def s14_steep(n_rows: int, n_cols: int) -> np.ndarray:
    """S14 — deliberadamente empinada: |∇h| ~ 94 → nz_min ~ 1e-2."""
    xx, yy = _mesh(n_rows, n_cols)
    return 2.5 * np.sin(2.0 * np.pi * 6.0 * xx) + 2.0 * np.cos(2.0 * np.pi * 5.0 * yy)


def s15_periodic_noise(n_rows: int, n_cols: int) -> np.ndarray:
    """S15 — ruido periódico determinista: 96 sinusoides de frecuencia entera, seed fija."""
    xx, yy = _mesh(n_rows, n_cols)
    rng = np.random.default_rng(SEED_NOISE_SURFACE)
    h = np.zeros((n_rows, n_cols), dtype=np.float64)
    for _ in range(96):
        kx = int(rng.integers(-24, 25))
        ky = int(rng.integers(-24, 25))
        if kx == 0 and ky == 0:
            continue
        amp = float(rng.uniform(0.02, 0.15)) / np.sqrt(kx * kx + ky * ky + 1.0)
        phase = float(rng.uniform(0.0, 2.0 * np.pi))
        h += amp * np.sin(2.0 * np.pi * (kx * xx + ky * yy) + phase)
    return h


def n01_ramp(n_rows: int, n_cols: int) -> np.ndarray:
    """N01 — control negativo: rampa lineal xx-0.5 (no representable en el toro)."""
    xx, _ = _mesh(n_rows, n_cols)
    return xx - 0.5


def n02_bump_boundary(n_rows: int, n_cols: int) -> np.ndarray:
    """N02 — control negativo: gaussiana SIN wrap cerca del borde (salta en el wrap)."""
    xx, yy = _mesh(n_rows, n_cols)
    sigma = 0.15
    return np.exp(-((xx - 0.15) ** 2 + (yy - 0.5) ** 2) / (2.0 * sigma * sigma))


PERIODIC_CASES: dict[str, Callable[[int, int], np.ndarray]] = {
    "S01_flat": s01_flat,
    "S02_sine_x": s02_sine_x,
    "S03_sine_y": s03_sine_y,
    "S04_separable": s04_separable,
    "S05_diagonal": s05_diagonal,
    "S06_multifreq": s06_multifreq,
    "S07_bumps": s07_bumps,
    "S08_ridges": s08_ridges,
    "S09_bricks": s09_bricks,
    "S10_asymmetric": s10_asymmetric,
    "S11_highfreq": s11_highfreq,
    "S12_mixed_scale": s12_mixed_scale,
    "S13_near_flat": s13_near_flat,
    "S14_steep": s14_steep,
    "S15_periodic_noise": s15_periodic_noise,
}

NEGATIVE_CONTROLS: dict[str, Callable[[int, int], np.ndarray]] = {
    "N01_ramp": n01_ramp,
    "N02_bump_boundary": n02_bump_boundary,
}


def boundary_wrap_ratio(h: np.ndarray) -> float:
    """Métrica de consistencia de frontera del ground truth (§11 del brief).

    Para cada eje: ``mean(|paso de wrap|) / mean(|paso interior vecino|)``. Para una
    superficie muestreada de un campo suave del toro ambos pasos son "un paso de
    muestra" → razón ~1 (aceptamos [0.2, 5] por contenido de creases tipo S08/S09).
    Una superficie no periódica produce razón ≫5 (rampa) o ≫1 localizada (N02).
    Devuelve el máximo de ambos ejes.
    """
    ratios = []
    for axis in (0, 1):
        wrap = np.abs(np.take(h, 0, axis=axis) - np.take(h, -1, axis=axis))
        inner = np.abs(np.take(h, 1, axis=axis) - np.take(h, 0, axis=axis))
        if float(inner.mean()) < 1e-12:
            ratios.append(1.0)  # campo constante en este eje: nada que violar → periódico
            continue
        ratios.append(float(wrap.mean()) / float(inner.mean()))
    return max(ratios)
