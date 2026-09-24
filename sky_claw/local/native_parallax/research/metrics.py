"""Métricas del spike NP-M0 (§17/§18/§32/§35 del brief). Solo NumPy, sin dependencias.

Tres familias para evitar que el alineamiento afín "arregle de más" (§35):

1. Alineadas (offset/escala del oracle eliminados): ``rmse``, ``mae``, ``r2``, bandas.
2. Crudas (independientes del fitting): ``raw_centered_rmse``, ``gradient_rmse``,
   ``normal_angle_*`` — si el solver reconstruye la geometría, estas también están bien.
3. De contrato: ``seam_height``, ``seam_gradient`` (comportamiento en el wrap del toro),
   bandas LOW/MID/HIGH (dónde vive el error), ``ssim`` (ventana uniforme 7, sin deps).

Sin scipy: Pearson es ``np.corrcoef``; Spearman es Pearson sobre rangos ordinales
(dobles argsort; datos continuos → sin empates relevantes; documentado).
"""

from __future__ import annotations

import numpy as np

from sky_claw.local.native_parallax.research.alignment import best_affine
from sky_claw.local.native_parallax.research.normal_fft_periodic import integrate_periodic
from sky_claw.local.native_parallax.research.normal_from_height import (
    gradients_from_normal,
    normals_from_gradients,
    spectral_gradients,
)

SSIM_WINDOW = 7


def rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a * a)))


def rmse(rec: np.ndarray, ref: np.ndarray) -> float:
    return rms(rec - ref)


def mae(rec: np.ndarray, ref: np.ndarray) -> float:
    return float(np.mean(np.abs(rec - ref)))


def pearson(rec: np.ndarray, ref: np.ndarray) -> float:
    """Pearson de campos centrados con guardas para campos planos."""
    r = rec.ravel() - rec.mean()
    f = ref.ravel() - ref.mean()
    sr = float(np.sqrt(np.dot(r, r)))
    sf = float(np.sqrt(np.dot(f, f)))
    if sr < 1e-300 or sf < 1e-300:
        return 1.0 if rms(rec - ref) < 1e-12 else 0.0
    return float(np.dot(r, f) / (sr * sf))


def _ranks(a: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(a, axis=None), axis=None).reshape(a.shape)


def spearman(rec: np.ndarray, ref: np.ndarray) -> float:
    """Spearman vía rangos ordinales (datos continuos; sin corrección de empates)."""
    return pearson(_ranks(rec), _ranks(ref))


def r2(rec: np.ndarray, ref: np.ndarray) -> float:
    """Coeficiente de determinación tras alinear (SS_tot respecto de mean(ref))."""
    ss_res = float(np.sum((ref - rec) ** 2))
    ss_tot = float(np.sum((ref - ref.mean()) ** 2))
    if ss_tot < 1e-300:
        return 1.0 if ss_res < 1e-18 else 0.0
    return 1.0 - ss_res / ss_tot


def raw_centered_rmse(rec: np.ndarray, ref: np.ndarray) -> float:
    """RMSE entre campos centrados SIN escala — audit del fitting (§35)."""
    return rms((rec - rec.mean()) - (ref - ref.mean()))


def gradient_rmse(rec: np.ndarray, p_ref: np.ndarray, q_ref: np.ndarray) -> float:
    """RMSE combinado de los gradientes espectralmente derivados de ``rec`` vs ground truth.

    Independiente de offset Y de escala afín (un ``a·h`` produce ``a·(p,q)`` que SÍ
    dispara esta métrica: es el detector del bug 2π de la mutación M1).
    """
    p_rec, q_rec = spectral_gradients(rec)
    return float(np.sqrt(0.5 * (rms(p_rec - p_ref) ** 2 + rms(q_rec - q_ref) ** 2)))


def normal_angle_stats(rec: np.ndarray, n_ref: np.ndarray, sx: float = 1.0, sy: float = 1.0) -> tuple[float, float]:
    """Error angular (grados) entre la normal ground truth y la re-derivada de ``rec``.

    ``H' → (p', q') espectrales → normal'`` con la MISMA convención de signos; ángulo
    ``arccos(dot)``. Detector de convención (§22): flips de signo se manifiestan aquí.
    """
    p_rec, q_rec = spectral_gradients(rec)
    n_rec = normals_from_gradients(p_rec, q_rec, sx=sx, sy=sy)
    dot = np.clip(np.sum(n_rec * n_ref, axis=-1), -1.0, 1.0)
    deg = np.degrees(np.arccos(dot))
    return float(deg.mean()), float(np.percentile(deg, 95))


def ssim(rec: np.ndarray, ref: np.ndarray, window: int = SSIM_WINDOW) -> float:
    """SSIM de ventana uniforme (región válida), sin dependencias externas.

    ``L`` = rango del ground truth (piso 1e-3 para casos casi planos); C1=(0.01L)²,
    C2=(0.03L)². Implementación con sumas acumuladas (integral image).
    """
    r = ref - ref.mean()
    c = rec - rec.mean()
    n_rows, n_cols = r.shape
    if n_rows <= window or n_cols <= window:
        return float("nan")

    def _box(x: np.ndarray) -> np.ndarray:
        cs = np.cumsum(np.cumsum(x, axis=0), axis=1)
        cs = np.pad(cs, ((1, 0), (1, 0)))
        return np.asarray(
            cs[window:, window:] - cs[:-window, window:] - cs[window:, :-window] + cs[:-window, :-window],
            dtype=np.float64,
        )

    n = float(window * window)
    mr, mc = _box(r) / n, _box(c) / n
    rr, cc = _box(r * r) / n, _box(c * c) / n
    rc = _box(r * c) / n
    vr, vc = rr - mr * mr, cc - mc * mc
    cov = rc - mr * mc
    data_range = max(float(ref.max() - ref.min()), 1e-3)
    c1 = (0.01 * data_range) ** 2
    c2 = (0.03 * data_range) ** 2
    ssim_map = ((2 * mr * mc + c1) * (2 * cov + c2)) / ((mr * mr + mc * mc + c1) * (vr + vc + c2))
    return float(ssim_map.mean())


def _wrap_diff(h: np.ndarray, axis: int) -> np.ndarray:
    return np.asarray(np.take(h, 0, axis=axis) - np.take(h, -1, axis=axis), dtype=np.float64)


def seam_height(rec: np.ndarray, ref: np.ndarray) -> float:
    """RMSE del salto de wrap (h[0]-h[-1]) de la reconstrucción vs ground truth, ambos ejes.

    Un solver periódico reconstruye la MISMA continuidad toroidal que el ground truth;
    contenido no periódico (controles N*) produce aquí un error grande (§24).
    """
    e = [_wrap_diff(rec, axis) - _wrap_diff(ref, axis) for axis in (0, 1)]
    return float(np.sqrt(np.mean(np.concatenate([e[0] ** 2, e[1] ** 2]))))


def seam_gradient(rec: np.ndarray, ref: np.ndarray) -> float:
    """Igual que ``seam_height`` pero sobre los campos de gradiente (p, q)."""
    p_r, q_r = spectral_gradients(rec)
    p_f, q_f = spectral_gradients(ref)
    errs = []
    for a, b in ((p_r, p_f), (q_r, q_f)):
        for axis in (0, 1):
            errs.append(_wrap_diff(a, axis) - _wrap_diff(b, axis))
    flat = np.concatenate([e.ravel() for e in errs])
    return float(np.sqrt(np.mean(flat * flat)))


def band_errors(rec: np.ndarray, ref: np.ndarray) -> tuple[float, float, float]:
    """RMSE del error alineado por banda espectral (§18): LOW / MID / HIGH.

    Radio de frecuencia normalizado por Nyquist: ρ = sqrt((2fx)² + (2fy)²) ∈ [0, √2].
    Bandas: LOW ρ ≤ 0.25, MID 0.25 < ρ ≤ 1.0, HIGH ρ > 1.0. Las bandas particionan el
    espectro (sin overlap) vía máscara en Fourier + IFFT por banda.
    """
    err = rec - ref
    n_rows, n_cols = ref.shape
    fx = np.fft.fftfreq(n_cols).reshape(1, n_cols)
    fy = np.fft.fftfreq(n_rows).reshape(n_rows, 1)
    rho = np.sqrt((2.0 * fx) ** 2 + (2.0 * fy) ** 2)
    e_hat = np.fft.fft2(err)
    out = []
    for mask in (rho <= 0.25, (rho > 0.25) & (rho <= 1.0), rho > 1.0):
        e_band = np.real(np.fft.ifft2(e_hat * mask))
        out.append(rms(e_band))
    return out[0], out[1], out[2]


def compute_all(
    ref: np.ndarray,
    rec: np.ndarray,
    p_gt: np.ndarray,
    q_gt: np.ndarray,
    n_gt: np.ndarray,
    sx: float = 1.0,
    sy: float = 1.0,
) -> dict[str, float]:
    """Tabla de métricas completa para un caso (columnas de §32 + extras de auditoría)."""
    fit = best_affine(rec, ref)
    rec_a = fit["a"] * rec + fit["b"]
    ang_mean, ang_p95 = normal_angle_stats(rec, n_gt, sx=sx, sy=sy)
    lo, mid, hi = band_errors(rec_a, ref)
    finite = bool(
        np.all(np.isfinite(rec)) and np.all(np.isfinite(ref)) and np.isfinite(fit["a"]) and np.isfinite(fit["b"])
    )
    return {
        "rmse": fit["rmse"],
        "mae": mae(rec_a, ref),
        "raw_centered_rmse": raw_centered_rmse(rec, ref),
        "gradient_rmse": gradient_rmse(rec, p_gt, q_gt),
        "normal_angle_mean": ang_mean,
        "normal_angle_p95": ang_p95,
        "corr": pearson(rec, ref),
        "spearman": spearman(rec, ref),
        "r2": r2(rec_a, ref),
        "ssim": ssim(rec_a, ref),
        "seam_height": seam_height(rec, ref),
        "seam_gradient": seam_gradient(rec, ref),
        "low_band_error": lo,
        "mid_band_error": mid,
        "high_band_error": hi,
        "best_sign": fit["best_sign"],
        "scale_a": fit["a"],
        "offset_b": fit["b"],
        "finite": finite,
    }


def reconstruct_case(
    h: np.ndarray, sx: float = 1.0, sy: float = 1.0, nz_floor: float = 1e-6
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, float]]:
    """Pipeline completo del caso: H → N → (p,q) → H'. Devuelve (h, p, q, n, rec, info).

    Útil para tests y runner; mantiene el orden del §3 del brief y descarta H del camino
    de reconstrucción (la inversa solo ve N).
    """
    p, q = spectral_gradients(h)
    n = normals_from_gradients(p, q, sx=sx, sy=sy)
    p2, q2, _hits = gradients_from_normal(n, sx=sx, sy=sy, nz_floor=nz_floor)
    rec, info = integrate_periodic(p2, q2)
    return h, p, q, n, rec, info
