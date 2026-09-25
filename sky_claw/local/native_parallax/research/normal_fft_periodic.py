"""Solver periódico de Poisson por FFT — ``normal_fft_periodic_v1`` (referencia NP-M0).

Derivación (documentada, no copiada):

Sea ``h`` un campo periódico en el toro muestreado en grid ``(H, W)`` y sean
``p = ∂h/∂x``, ``q = ∂h/∂y`` con ``x = j/W ∈ [0,1)`` e ``y = i/H`` (coordenadas de
textura, NO índices de muestra). Con la convención ``numpy.fft`` (``fft2`` sin
normalizar, ``ifft2`` con 1/N) las frecuencias correctas para derivar respecto de la
coordenada de textura son

    wx = 2π · (fftfreq(W) · W) = 2π·k   [rad por UNIDAD de x, eje de columnas]
    wy = 2π · (fftfreq(H) · H) = 2π·l   [rad por UNIDAD de y, eje de filas]

ADVERTENCIA de unidades (bug real cazado por NP-M0): ``2π·fftfreq`` a secas son
radianes POR MUESTRA y derivarían respecto del índice (p escalado por 1/W,
resolution-dependiente). La convención elegida hace a (p, q) independientes de la
resolución del grid — propiedad requerida cuando los gradientes vengan de normal maps
reales en futuras etapas.

valen las propiedades espectrales

    F{∂h/∂x} = i·wx·ĥ ,   F{∂h/∂y} = i·wy·ĥ ,   F{Δh} = -(wx² + wy²)·ĥ .

El integrador resuelve la ecuación de Poisson ``Δh = ∂p/∂x + ∂q/∂y``:

    -(wx² + wy²)·ĥ = i·wx·p̂ + i·wy·q̂
    ⟹  ĥ = ( -i·wx·p̂ - i·wy·q̂ ) / (wx² + wy²)        [modos ≠ (0,0)]

que es exactamente la proyección L2 del campo (p, q) sobre campos integrables en la
base Fourier periódica (Frankot–Chellappa 1988; survey de Quéau/Durou/Aujol 2017 §3.3).

Decisiones de convención (verificadas por tests, mutaciones M1–M4):

- **2π**: las frecuencias son angulares (rad/muestra) vía ``2π·fftfreq``. Omitir el 2π
  escala la solución ~2π en amplitud; el alineamiento afín de evaluación LO ABSORBERÍA
  (§35 del brief), por eso el oracle también mide ``gradient_rmse``/ángulo normal.
- **DC**: ``ĥ[0,0] = 0`` — la media no es observable desde gradientes (el solver devuelve
  media ~0 por construcción). No se cuenta como error (§15).
- **Nyquist**: en grados pares la derivada espectral es ambigua en el bin de Nyquist
  (autoparejado bajo k→-k; multiplicarlo por i·wx rompe la simetría Hermitiana y genera
  componente imaginaria). Convención adoptada: derivada nula en bins de Nyquist
  (extensión par). Superficies con energía en Nyquist pierden ese modo (se mide).
"""

from __future__ import annotations

import numpy as np


def freq_axes(n_rows: int, n_cols: int) -> tuple[np.ndarray, np.ndarray]:
    """Ejes de frecuencia angular listos para broadcast: ``(wy (H,1), wx (1, W))``.

    ``wx = 2π·fftfreq(W)`` con la derivada anulada en el bin de Nyquist (ver docstring
    del módulo). Única fuente de verdad de la convención de frecuencias del spike.
    """
    fx = np.fft.fftfreq(n_cols)
    fy = np.fft.fftfreq(n_rows)
    wx = 2.0 * np.pi * fx * n_cols
    wy = 2.0 * np.pi * fy * n_rows
    # Derivada nula en Nyquist (bins autoparejados): convención documentada arriba.
    # OJO: fftfreq representa Nyquist como -0.5 (no +0.5) → hay que mirar |f|.
    wx[np.abs(np.abs(fx) - 0.5) < 1e-12] = 0.0
    wy[np.abs(np.abs(fy) - 0.5) < 1e-12] = 0.0
    return wy.reshape(n_rows, 1), wx.reshape(1, n_cols)


def integrate_periodic(p: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    """Reconstruye ``h`` de media cero a partir del campo de gradientes (p, q).

    Devuelve ``(h, info)`` con ``h`` float64 de shape ``(n_rows, n_cols)`` y ``info`` con
    ``max_imag_residual`` (residuo imaginario tras IFFT; debe ser ~eps de máquina) y
    ``mean`` (debe ser ~0). No altera los datos de entrada. Complejidad O(N log N).
    """
    if p.shape != q.shape:
        raise ValueError(f"p y q deben tener el mismo shape: {p.shape} vs {q.shape}")
    n_rows, n_cols = p.shape
    wy, wx = freq_axes(n_rows, n_cols)
    p_hat = np.fft.fft2(p)
    q_hat = np.fft.fft2(q)
    denom = wx * wx + wy * wy
    # denom se anula en CUATRO bins: DC (0,0) y los tres Nyquist autoparejados
    # (0,W/2),(H/2,0),(H/2,W/2), porque freq_axes anula la derivada allí. En todos
    # ellos el numerador también es 0 (wx=wy=0) → 0/0 = NaN que contaminaría el
    # espectro completo. Se fijan explícitamente a modo nulo:
    nulo = denom == 0.0
    denom[nulo] = 1.0
    h_hat = (-1j * wx * p_hat - 1j * wy * q_hat) / denom
    h_hat[nulo] = 0.0
    h_cplx = np.fft.ifft2(h_hat)
    info = {
        "max_imag_residual": float(np.max(np.abs(h_cplx.imag))),
        "mean": float(h_cplx.real.mean()),
    }
    return np.ascontiguousarray(h_cplx.real), info
