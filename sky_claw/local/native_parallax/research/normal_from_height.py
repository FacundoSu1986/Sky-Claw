"""H → normal (derivada espectral exacta) y normal → gradientes (inversa). NP-M0.

Cadena de ground truth del experimento:

    H --(derivada espectral)--> (p, q) --(convención parametrizable)--> N
    N --(inversa + nz_floor)--> (p, q) --(normal_fft_periodic_v1)--> H'

Convenciones:

- Gradientes de ground truth **espectrales** (coherentes con el solver): para superficies
  band-limited son las derivadas exactas; para contenido con creases (S08/S09) miden la
  derivada de la proyección band-limited, lo que hace el roundtrip internamente
  consistente (el solver reconstruye exactamente esa proyección). La alternativa
  (diferencias finitas periódicas) se evalúa como mutación M5: introduce el factor de
  transferencia ``sinc`` de la DF central y el error se manifiesta como atenuación HF
  medible — el experimento lo registra, no lo esconde.
- Normal: ``N = normalize((-sx·p, -sy·q, 1))`` con signos parametrizables
  (``sx``, ``sy``). NP-M0 NO fija "verdad Skyrim" (§12 del brief): estudia la matemática.
- La inversa ``p = -sx·nx / max(nz, nz_floor)`` es invariante a la escala de N
  (nx/nz no cambia si N se multiplica por escalar); el floor solo actúa cuando
  ``nz < nz_floor`` y se **cuenta** (§13 del brief: no esconder la singularidad).
"""

from __future__ import annotations

import numpy as np

from sky_claw.local.native_parallax.research.normal_fft_periodic import freq_axes

NZ_FLOOR_DEFAULT = 1e-6


def spectral_gradients(h: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Derivadas periódicas exactas (band-limited) ``(p, q) = (∂h/∂x, ∂h/∂y)``."""
    n_rows, n_cols = h.shape
    wy, wx = freq_axes(n_rows, n_cols)
    h_hat = np.fft.fft2(h)
    p = np.real(np.fft.ifft2(1j * wx * h_hat))
    q = np.real(np.fft.ifft2(1j * wy * h_hat))
    return p, q


def normals_from_gradients(p: np.ndarray, q: np.ndarray, sx: float = 1.0, sy: float = 1.0) -> np.ndarray:
    """Normal tangent-space unitaria ``(H, W, 3)`` con signos parametrizables."""
    nx = -sx * p
    ny = -sy * q
    nz = np.ones_like(p)
    norm = np.sqrt(nx * nx + ny * ny + 1.0)
    return np.stack((nx / norm, ny / norm, nz / norm), axis=-1)


def gradients_from_normal(
    n: np.ndarray,
    sx: float = 1.0,
    sy: float = 1.0,
    nz_floor: float = NZ_FLOOR_DEFAULT,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Inversa ``p = -sx·nx/max(nz,nz_floor)``; devuelve ``(p, q, floor_hits)``.

    ``floor_hits`` = píxeles donde ``nz < nz_floor`` (la singularidad NO se esconde:
    se cuantifica, §13/§23 del brief).
    """
    nx, ny, nz = n[..., 0], n[..., 1], n[..., 2]
    nz_eff = np.maximum(nz, nz_floor)
    hits = int(np.count_nonzero(nz < nz_floor))
    p = -sx * nx / nz_eff
    q = -sy * ny / nz_eff
    return p, q, hits


def quantize_decode(n: np.ndarray, bits: int = 8) -> np.ndarray:
    """Simula almacenamiento entero de la normal (sin DDS): encode→uint→decode.

    ``N ∈ [-1,1]`` → ``(N+1)/2 · (2^bits - 1)`` redondeado → de-vuelta a [-1,1] →
    renormalizada. Q8 es el caso relevante (texturas 8-bit); Q10/Q16 como control.
    """
    levels = float(2**bits - 1)
    q01 = np.rint((n + 1.0) * 0.5 * levels) / levels
    nd = q01 * 2.0 - 1.0
    norm = np.linalg.norm(nd, axis=-1, keepdims=True)
    return np.asarray(nd / norm, dtype=np.float64)
