"""EXP-M1 — políticas de confianza nz (research-only, sobre el banco NP-M0).

Pregunta: ¿qué hacer cuando ``nz`` deja de ser confiable (nz ≈ σ del ruido de la
normal)? Este módulo implementa las políticas P0–P3 como transformaciones explícitas
normal → gradiente, con estadística de activación y de riesgo. NO es weighted-Poisson,
NO es producción, NO fija constantes (ver invariantes en los tests).

Derivación de sensibilidad (§7 del brief), no aceptada a ciegas:

    p(nx, nz) = -nx / nz

    ∂p/∂nx = -1/nz
    ∂p/∂nz = d/dnz(-nx·nz⁻¹) = +nx·nz⁻² = nx/nz²

Con perturbaciones iid (δnx, δnz) ~ (0, σ):

    δp ≈ -δnx/nz + nx·δnz/nz²
    E[δp²] ≈ σ²/nz² + nx²σ²/nz⁴ = σ²·(nz² + nx²)/nz⁴

Como ``nz² + nx² = 1 - ny² ≤ 1``, en el peor caso orientativo (ny≈0):

    |δp| ≳ σ/nz²   (término dominante: el ERROR EN nz entra con nz⁻²)

y acotando ingenuamente, |δp| = O(σ/nz) .. O(σ/nz²): la variable adimensional que
gobierna la inestabilidad es ``r = nz/σ`` (con σ efectivo del canal más ruidoso).
EXP-M1 mide esa relación en vez de asumirla (H1/H2).

P3 — regularización suave tipo Tikhonov del inverso (§15). Derivación: buscamos
``g ≈ 1/nz`` sin divergencia, como mínimo de

    J(g) = (nz·g - 1)² + λ²·g²

    dJ/dg = 2nz(nz·g - 1) + 2λ²g = 0  ⟹  g*(nz) = nz / (nz² + λ²)

Propiedades: ``g* → 1/nz`` si ``nz ≫ λ``; ``g*(0) = 0``; ``|g*| ≤ 1/(2λ)`` (máximo en
``nz = λ``); ``g*(nz)`` conserva el SIGNO de nz (una normal invertida sigue produciendo
gradiente invertido — no se disimula). Es PRECONDITIONING del gradiente, no weighted
least squares (no se afirma equivalencia).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from sky_claw.local.native_parallax.research.normal_from_height import spectral_gradients

POLICIES = ("RAW", "FLOOR_CLAMP", "FLOOR_ZERO", "SOFT_TIKHONOV")

# Piso numérico SOLO anti-undefined-behavior para RAW (no es una política: evita 1/0).
NZ_EPS_RAW = 1e-30


@dataclass(frozen=True)
class PolicyStats:
    """Estadística por-caso de la política (§20/§21 del brief)."""

    negative_nz_fraction: float  # fracción de píxeles con nz <= 0 en la normal ENTRADA
    activation_fraction: float  # fracción donde la política alteró/regularizó el gradiente
    low_trust_fraction: float  # fracción con r = nz/σ < 1 (métrica independiente de política)
    max_gradient: float
    p99_gradient: float


def quantize_xy_reconstruct_z(n: np.ndarray, bits: int = 8) -> np.ndarray:
    """Q8_XY_RECONSTRUCT_Z: cuantiza (nx, ny) a enteros y RECONSTRUYE nz.

    Contrato de normal de dos canales: nz = sqrt(max(0, 1 - nx² - ny²)) SIN
    renormalizar (el shader reconstruye z; no hay cuarto canal). NO es una simulación
    de BC5 (no modela compresión de bloques) — es el modelo de cuantización 2-canal.
    Píxeles con nx²+ny² > 1 (ruido/cuantización en pendientes extremas) producen
    nz = 0 exacto: la política debe tratarlos explícitamente.
    """
    levels = float(2**bits - 1)
    q01 = np.rint((n[..., :2] + 1.0) * 0.5 * levels) / levels
    nx = q01[..., 0] * 2.0 - 1.0
    ny = q01[..., 1] * 2.0 - 1.0
    s = nx * nx + ny * ny
    nz = np.sqrt(np.maximum(0.0, 1.0 - s))
    return np.stack((nx, ny, nz), axis=-1)


def decode_gradients_policy(
    n: np.ndarray,
    sx: float,
    sy: float,
    policy: str,
    lam: float,
    sigma: float,
) -> tuple[np.ndarray, np.ndarray, PolicyStats]:
    """Normal → gradientes (p, q) bajo una política, con estadística obligatoria.

    ``lam`` y ``sigma`` NO tienen default (invariante M5: ninguna política puede
    promoverse con una constante absoluta escondida). Para RAW, ``lam`` se ignora pero
    debe pasarse explícitamente (por ejemplo 0.0) — así el caller nunca olvida que hay
    una decisión de política en juego.
    """
    if policy not in POLICIES:
        raise ValueError(f"policy desconocida: {policy} (válidas: {POLICIES})")
    nx, ny, nz = n[..., 0], n[..., 1], n[..., 2]
    negative_nz_fraction = float(np.count_nonzero(nz <= 0.0)) / nz.size
    low_trust_fraction = float(np.count_nonzero(nz < sigma)) / nz.size if sigma > 0.0 else 0.0

    if policy == "RAW":
        nz_guard = np.where(np.abs(nz) < NZ_EPS_RAW, NZ_EPS_RAW, nz)
        p = -sx * nx / nz_guard
        q = -sy * ny / nz_guard
        activation = 0.0
    elif policy == "FLOOR_CLAMP":
        # Variante A: nz_eff = max(nz, λ). Los nz<=0 quedan en λ (su signo se pierde:
        # el clamp NO usa abs(), pero sí oculta el signo negativo — se cuenta en stats).
        nz_eff = np.maximum(nz, lam)
        p = -sx * nx / nz_eff
        q = -sy * ny / nz_eff
        activation = float(np.count_nonzero(nz < lam)) / nz.size
    elif policy == "FLOOR_ZERO":
        # Variante B: nz<=0 → contribución cero (rechazo por píxel); nz>0 crudo.
        valid = nz > 0.0
        safe = np.where(valid, nz, 1.0)
        p = np.where(valid, -sx * nx / safe, 0.0)
        q = np.where(valid, -sy * ny / safe, 0.0)
        activation = float(np.count_nonzero(~valid)) / nz.size
    else:  # SOFT_TIKHONOV
        inv_reg = nz / (nz * nz + lam * lam)
        p = -sx * nx * inv_reg
        q = -sy * ny * inv_reg
        activation = float(np.count_nonzero(nz < lam)) / nz.size

    grad_mag = np.sqrt(p * p + q * q)
    stats = PolicyStats(
        negative_nz_fraction=negative_nz_fraction,
        activation_fraction=activation,
        low_trust_fraction=low_trust_fraction,
        max_gradient=float(grad_mag.max()),
        p99_gradient=float(np.percentile(grad_mag, 99)),
    )
    return p, q, stats


def anti_flatten_metrics(rec: np.ndarray, ref: np.ndarray, p_gt: np.ndarray, q_gt: np.ndarray) -> dict[str, float]:
    """Métricas anti-aplanado (§21): una política no "gana" destruyendo el relieve.

    - ``variance_ratio``: var(H')/var(H_gt) (invariante a offset; el fitting afín no
      lo arregla si la señal se aplasta).
    - ``gradient_energy_ratio``: energía media de los gradientes reconstruidos vs GT.
    - ``hf_energy_ratio``: fracción de la energía espectral de H' en banda HF (ρ>1)
      comparada con la misma fracción del GT (relación de contenido de detalle).
    """
    var_ratio = float(np.var(rec) / max(np.var(ref), 1e-300))
    p_rec, q_rec = spectral_gradients(rec)
    e_rec = float(np.mean(p_rec * p_rec + q_rec * q_rec))
    e_gt = float(np.mean(p_gt * p_gt + q_gt * q_gt))
    grad_ratio = e_rec / max(e_gt, 1e-300)
    hf_rec, total_rec = _band_energy(rec)
    hf_gt, total_gt = _band_energy(ref)
    hf_ratio = (hf_rec / max(total_rec, 1e-300)) / max(hf_gt / max(total_gt, 1e-300), 1e-300)
    return {
        "variance_ratio": var_ratio,
        "gradient_energy_ratio": grad_ratio,
        "hf_energy_ratio": hf_ratio,
    }


def _band_energy(h: np.ndarray) -> tuple[float, float]:
    h_hat = np.fft.fft2(h)
    power = np.abs(h_hat) ** 2
    n_rows, n_cols = h.shape
    fx = np.fft.fftfreq(n_cols).reshape(1, n_cols)
    fy = np.fft.fftfreq(n_rows).reshape(n_rows, 1)
    rho = np.sqrt((2.0 * fx) ** 2 + (2.0 * fy) ** 2)
    total = float(power.sum())
    hf = float(power[rho > 1.0].sum())
    return hf, total
