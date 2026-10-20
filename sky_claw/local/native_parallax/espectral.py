"""Convención de frecuencias espectrales del generador nativo de parallax (NP-D0 y NP-I0).

UNA sola definición: el corpus sintético deriva con estas frecuencias (H → normal) y el integrador las usa para
invertir (normal → H). Un solver con otra convención daría gradientes escalados por la resolución: es un bug
real que ya apareció en NP-M0 (``2π · fftfreq`` a secas deriva respecto del índice de muestra y no de la
coordenada de textura). Por eso ``tests/test_native_parallax_producto_invariant.py`` congela por AST que este
sea el único módulo de producto que llama a ``fftfreq``.

Puro: no importa ``numpy``; recibe el módulo ya importado.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = ["ejes_de_frecuencia"]


def _exigir_entero(nombre: str, valor: object) -> int:
    if not isinstance(valor, int) or isinstance(valor, bool) or valor < 2:
        raise ValueError(f"{nombre} debe ser un entero >= 2, no {valor!r}")
    return valor


def ejes_de_frecuencia(
    np: ModuleType,
    n_fft_filas: int,
    n_fft_cols: int,
    muestras_por_unidad_filas: int | None = None,
    muestras_por_unidad_cols: int | None = None,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Frecuencias angulares para derivar respecto de la coordenada de textura: ``(wy (H,1), wx (1,W))``.

    ``wx = 2π · fftfreq(W) · W`` está en radianes por UNIDAD de x (la textura mide 1), no por muestra. Con las
    longitudes de FFT iguales a las de la textura (caso periódico) las muestras por unidad son las mismas; en la
    extensión por espejo de la variante Neumann la FFT es de ``2W`` pero siguen siendo ``W`` muestras por unidad,
    y por eso se pasan aparte. La derivada se anula en los bins de Nyquist (extensión par): multiplicarlos por
    ``i·w`` rompe la simetría hermitiana y genera una componente imaginaria.
    """
    filas = _exigir_entero("n_fft_filas", n_fft_filas)
    cols = _exigir_entero("n_fft_cols", n_fft_cols)
    por_unidad_filas = (
        filas
        if muestras_por_unidad_filas is None
        else _exigir_entero("muestras_por_unidad_filas", muestras_por_unidad_filas)
    )
    por_unidad_cols = (
        cols
        if muestras_por_unidad_cols is None
        else _exigir_entero("muestras_por_unidad_cols", muestras_por_unidad_cols)
    )
    fx = np.fft.fftfreq(cols)
    fy = np.fft.fftfreq(filas)
    wx = 2.0 * np.pi * fx * por_unidad_cols
    wy = 2.0 * np.pi * fy * por_unidad_filas
    # fftfreq representa Nyquist como -0.5 (no +0.5): hay que mirar |f|.
    wx[np.abs(np.abs(fx) - 0.5) < 1e-12] = 0.0
    wy[np.abs(np.abs(fy) - 0.5) < 1e-12] = 0.0
    return wy.reshape(filas, 1), wx.reshape(1, cols)
