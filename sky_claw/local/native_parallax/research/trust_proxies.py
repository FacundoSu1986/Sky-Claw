"""EXP-M2 — trust proxies normal-only + oráculo de evaluación (ESTRICTAMENTE separados).

§20: las funciones FEATURE (normal-only) NUNCA reciben el height authored — su firma
lo impide y hay un test (M1) que lo verifica por introspección. El oráculo (§18) vive
en OracleOnly y sólo se usa para responder "cuánto mismatch real existe" y para
categorizar riesgo a posteriori; jamás para construir la policy candidata.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from inspect import signature
from typing import Any

import numpy as np
from numpy.typing import NDArray

from sky_claw.local.native_parallax.research import metrics as np_m0_metrics
from sky_claw.local.native_parallax.research.normal_fft_periodic import integrate_periodic
from sky_claw.local.native_parallax.research.normal_from_height import spectral_gradients
from sky_claw.local.native_parallax.research.nz_policies import _band_energy, anti_flatten_metrics

NOT_INFORMATIVE = "NOT_INFORMATIVE"

FEATURE_FUNCS: tuple[Callable[..., Any], ...] = ()  # poblado al final del módulo


# ----------------------------------------------------------------------
# Features normal-only (§19)
# ----------------------------------------------------------------------


def nz_statistics(nz: NDArray[np.float64]) -> dict[str, float]:
    """min / p01 / p05 / low_trust_fraction — §27 compara min vs p01 vs p05."""
    return {
        "nz_min": float(np.min(nz)),
        "nz_p01": float(np.percentile(nz, 1)),
        "nz_p05": float(np.percentile(nz, 5)),
        "negative_nz_fraction": float(np.mean(nz <= 0.0)),
    }


def curl_proxy(p: NDArray[np.float64], q: NDArray[np.float64]) -> dict[str, float]:
    """A: residual de integrabilidad curl = ∂p/∂y − ∂q/∂x con estadísticas robustas."""
    curl = spectral_gradients(p)[0] - spectral_gradients(q)[1]
    med = float(np.median(np.abs(curl)))
    mad = float(np.median(np.abs(np.abs(curl) - med)))
    return {
        "curl_median_abs": med,
        "curl_mad": 1.4826 * mad,
        "curl_p95_abs": float(np.percentile(np.abs(curl), 95)),
    }


def projection_residual(normal: NDArray[np.float64], sx: float, sy: float) -> dict[str, float]:
    """B: residuo de proyección integrable — usa SOLO la normal (§20 permite correr el solver).

    normal → p,q → integración periódica → normales reproyectadas → error angular.
    """
    p = -normal[..., 0] / np.maximum(normal[..., 2], 1e-30)
    q = -normal[..., 1] / np.maximum(normal[..., 2], 1e-30)
    h_rec, _info = integrate_periodic(p, q)
    p2, q2 = spectral_gradients(h_rec)
    nz2 = np.sqrt(np.maximum(0.0, 1.0 - p2**2 - q2**2))
    n2 = np.stack([p2, q2, nz2], axis=-1)
    n2 = n2 / np.maximum(np.linalg.norm(n2, axis=-1, keepdims=True), 1e-12)
    dot = np.clip(np.sum(normal * n2, axis=-1), -1.0, 1.0)
    ang = np.degrees(np.arccos(dot))
    return {
        "projection_residual_mean_deg": float(np.mean(ang)),
        "projection_residual_median_deg": float(np.median(ang)),
        "projection_residual_p95_deg": float(np.percentile(ang, 95)),
    }


def quantization_proxy(normal: NDArray[np.float64]) -> float:
    """C: fracción de muestras exactamente en la grilla 8-bit en canales XY.

    En maps JPG (8-bit de por sí) la fracción es trivialmente 1.0 → se registra y se
    marca como no diferenciadora para este corpus (sin sobreinterpretar, §19-C).
    """
    v = (normal[..., 0:2] * 0.5 + 0.5) * 255.0
    grid = np.rint(v)
    return float(np.mean(np.abs(v - grid) < 1e-6))


def blockiness_proxy(gray: NDArray[np.float64], block: int = 8) -> dict[str, float]:
    """E: energía en fronteras de bloque JPEG (8×8) vs interior de bloque.

    gray: cualquier canal escalar (p.ej. nx). NO lo llamamos detector BC5 (§30).
    """
    d = np.abs(np.diff(gray, axis=1))
    cols = np.arange(d.shape[1])
    on_boundary = (cols + 1) % block == 0
    off = d[:, ~on_boundary]
    on = d[:, on_boundary]
    base = float(np.mean(off)) if off.size else 0.0
    onm = float(np.mean(on)) if on.size else 0.0
    ratio = float(onm / base) if base > 1e-12 else (0.0 if onm < 1e-12 else 999.0)
    return {"blockiness_ratio": ratio, "blockiness_mean": onm}


def largest_low_trust_component(low_mask: NDArray[np.bool_]) -> dict[str, float]:
    """§28: connected components simple (BFS iterativo, sin dependencias nuevas)."""
    h, w = low_mask.shape
    visited = np.zeros_like(low_mask, dtype=bool)
    largest = 0
    n_components = 0
    for i in range(h):
        for j in range(w):
            if low_mask[i, j] and not visited[i, j]:
                n_components += 1
                stack = [(i, j)]
                visited[i, j] = True
                size = 0
                while stack:
                    y, x = stack.pop()
                    size += 1
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        yy, xx = y + dy, x + dx
                        if 0 <= yy < h and 0 <= xx < w and low_mask[yy, xx] and not visited[yy, xx]:
                            visited[yy, xx] = True
                            stack.append((yy, xx))
                largest = max(largest, size)
    total = float(max(h * w, 1))
    return {
        "largest_low_trust_component_fraction": float(largest) / total,
        "number_of_low_trust_components": float(n_components),
        "low_trust_fraction": float(np.mean(low_mask)),
    }


def normal_only_features(
    normal: NDArray[np.float64],
    *,
    sigma_eff: float,
) -> dict[str, float]:
    """Todos los features normal-only de un asset. NO acepta height (M1)."""
    nz = np.asarray(normal[..., 2], dtype=np.float64)
    feats: dict[str, float] = {}
    feats.update(nz_statistics(nz))
    p = -normal[..., 0] / np.maximum(normal[..., 2], 1e-30)
    q = -normal[..., 1] / np.maximum(normal[..., 2], 1e-30)
    feats.update(curl_proxy(p, q))
    feats.update(projection_residual(normal, sx=1.0, sy=1.0))
    feats["quantization_proxy"] = quantization_proxy(normal)
    feats.update(blockiness_proxy(normal[..., 0]))
    feats["unit_length_residual_p95"] = float(np.percentile(np.abs(np.linalg.norm(normal, axis=-1) - 1.0), 95))
    if sigma_eff > 0.0:
        low = nz < sigma_eff
        feats.update(largest_low_trust_component(low))
        feats["r_p01_proxy"] = feats["nz_p01"] / sigma_eff
        feats["r_p05_proxy"] = feats["nz_p05"] / sigma_eff
        feats["r_min_proxy"] = feats["nz_min"] / sigma_eff
    else:
        feats["r_p01_proxy"] = float("inf") if feats["nz_p01"] > 0 else float("-inf")
        feats["r_p05_proxy"] = float("inf") if feats["nz_p05"] > 0 else float("-inf")
        feats["r_min_proxy"] = float("inf") if feats["nz_min"] > 0 else float("-inf")
    return feats


FEATURE_FUNCS = (
    nz_statistics,
    curl_proxy,
    projection_residual,
    quantization_proxy,
    blockiness_proxy,
    largest_low_trust_component,
)


# ----------------------------------------------------------------------
# σ_eff candidates normal-only (§19/§51): escalares, deterministas
# ----------------------------------------------------------------------

SIGMA_EFF_CANDIDATES: dict[str, Callable[[NDArray[np.float64], dict[str, float]], float]] = {
    # B3: curl MAD como escala de ruido efectiva (robusta a outliers).
    "curl_mad": lambda _n, f: f["curl_mad"],
    # B4: residuo angular de proyección convertido a escala de canal (1 rad ≈ 57.3°).
    "projection_median": lambda _n, f: f["projection_residual_median_deg"] / 57.29577951308232,
    # nz de percentil bajo como cota de ruido: nz_p05 ≈ 3σ_tail? se documenta como heurística.
    "nz_p05_div3": lambda _n, f: f["nz_p05"] / 3.0,
    # baseline puro §53: nz_p01 (no es escala de ruido; se incluye como control).
    "nz_p01": lambda _n, f: f["nz_p01"],
}


# ----------------------------------------------------------------------
# Oráculo (§18): SOLO evaluación. Vive aparte para que ninguna policy lo use.
# ----------------------------------------------------------------------


@dataclass
class OracleOnly:
    """Métricas que consultan el authored height. PROHIBIDO usarlas en policies (M1/M9)."""

    @staticmethod
    def fit_global_scale(h_authored: NDArray[np.float64], h_recon: NDArray[np.float64]) -> dict[str, float]:
        """Sólo escala afín global y signo (§11/§36). Prohibido: warp/fit local (M9).

        ``affine_scale`` convierte la reconstrucción a unidades authored:
        pendiente de la regresión ``authored ~ recon`` = cov/var(recon). Con esta
        dirección, ``var(scale·recon)/var(authored) ≈ corr²`` — el invariante que hace
        interpretable a ``variance_ratio`` como aplanado (test M8).
        """
        a = np.asarray(h_authored, dtype=np.float64).ravel()
        b = np.asarray(h_recon, dtype=np.float64).ravel()
        a_c = a - a.mean()
        b_c = b - b.mean()
        denom = float(np.dot(b_c, b_c))
        scale = float(np.dot(a_c, b_c) / denom) if denom > 0 else 0.0
        best_sign = 1.0 if np.corrcoef(a, b)[0, 1] >= 0 else -1.0
        return {"affine_scale": scale, "oracle_best_sign": best_sign}

    @staticmethod
    def normal_height_residual_oracle(
        normal: NDArray[np.float64],
        h_authored: NDArray[np.float64],
        *,
        strength_grid: int = 25,
    ) -> dict[str, float]:
        """Mismatch normal↔height: deriva normal del height bajo UNA escala global
        (barrido determinista de strength) y mide error angular contra la normal authored.
        Elevado ⇒ NORMAL_HEIGHT_MISMATCH (hallazgo del dataset, §38)."""
        best_ang = float("inf")
        best_strength = 0.0
        grid = np.concatenate([np.linspace(0.05, 5.0, strength_grid), -np.linspace(0.05, 5.0, strength_grid)])
        for s in grid:
            h = (h_authored - h_authored.mean()) * float(s)
            p, q = spectral_gradients(h)
            nz = np.sqrt(np.maximum(0.0, 1.0 - p**2 - q**2))
            n2 = np.stack([p, q, nz], axis=-1)
            n2 = n2 / np.maximum(np.linalg.norm(n2, axis=-1, keepdims=True), 1e-12)
            dot = np.clip(np.sum(normal * n2, axis=-1), -1.0, 1.0)
            ang = float(np.median(np.degrees(np.arccos(dot))))
            if ang < best_ang:
                best_ang = ang
                best_strength = float(s)
        return {
            "height_normal_oracle_agreement_deg": best_ang,
            "oracle_best_strength": best_strength,
        }

    @staticmethod
    def evaluate(
        h_authored: NDArray[np.float64], rec: NDArray[np.float64], normal: NDArray[np.float64]
    ) -> dict[str, float]:
        """Comparación contra el oráculo: métricas NP-M0 + anti-flattening (§24/§34).

        ``rec`` debe venir YA convertido a unidades del authored height con la escala
        compartida fitada en RAW del mismo asset (ver run_exp_m2.run_policy): así
        ``variance_ratio`` mide aplanado real y no el mismatch de unidades del dataset
        (§38: la escala authored es intención artística, no física).
        """
        p, q = spectral_gradients(h_authored)
        m = np_m0_metrics.compute_all(h_authored, rec, p, q, normal)
        p_rec, q_rec = spectral_gradients(rec)
        m.update(anti_flatten_metrics(rec, h_authored, p_rec, q_rec))
        hf_gt, total_gt = _band_energy(h_authored)
        hf_frac_gt = hf_gt / max(total_gt, 1e-300)
        if hf_frac_gt < 1e-6:
            m["hf_energy_ratio"] = float("nan")  # NOT_INFORMATIVE (§24, M7)
        m["hf_gt_fraction"] = hf_frac_gt
        m["aligned_rmse"] = m["rmse"]
        m["correlation"] = m["corr"]
        return {k: float(v) for k, v in m.items()}


def check_no_height_in_features() -> None:
    """M1 por introspección: ningún FEATURE acepta un argumento que se llame height."""
    for fn in FEATURE_FUNCS:
        for param in signature(fn).parameters:
            assert "height" not in param.lower(), f"{fn.__name__} acepta {param} (fuga §20)"


# Reserva explícita: el pipeline de POLICY nunca importa OracleOnly (se chequea en tests).
POLICY_IMPORT_BLACKLIST = ("OracleOnly",)
