"""EXP-M4 — caminos emparejados SELF/AUTH + reglas de decisión (research-only).

Pregunta (preregistro §1): el error de reconstruir height desde una normal authored,
¿es limitación del solver (SELF también falla) o incoherencia del par authored
(SELF bueno, AUTH claramente peor)?

Este módulo NO duplica matemática (§8 del brief): compone primitivas publicadas

- forward matched:  ``normal_from_height.spectral_gradients`` + ``normals_from_gradients``
  (+ ``quantize_decode`` para igualar almacenamiento 8-bit de los PNG authored);
- solver:           ``run_exp_m2.reconstruct_from_normal(…, "RAW")`` (M2/M3, sin cambios);
- oráculo:          ``trust_proxies.OracleOnly`` (fit_global_scale + evaluate, M2/M3);
- diagnóstico de coherencia: ``OracleOnly.normal_height_residual_oracle`` (M3 §38).

Umbrales PRE-REGISTRADOS (docs/design/research/native-parallax/
exp-m4-solver-vs-pair-coherence.md §6, committeado antes de tocar Cohort A):

- C1 RMSE_SELF ≤ 0.10   — piso Q8 matched de NP-M0 §E1: S14_steep 0.0785 (corr 0.9994),
  S09_bricks 0.0175; contenido real no-periódico no puede quedar bajo el piso; 2.5× más
  estricto que CATASTROPHIC_RMSE=0.25 (M2/M3).
- C1 |corr_SELF| ≥ 0.95 — piso Q8 matched corr ≥ 0.9976 (peor sintético no-empinado);
  AUTH publicado M3 RAW@512 mediana 0.8392.
- C1 var_SELF ≥ 0.85    — AUTH M3 mediana 0.7043 (8/31 catastróficos); el techo no debe
  perder >15% de la varianza del oráculo.
- C2 DELTA_RMSE ≥ 0.02  — orden del piso Q8 típico no-empinado (1.8e-3..1.75e-2).
- C2 delta ≥ RMSE_SELF (dominancia) y Δ|corr| ≥ 0.10, con réplica de dirección en held-out.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from sky_claw.local.native_parallax.research.normal_from_height import (
    normals_from_gradients,
    quantize_decode,
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.run_exp_m2 import reconstruct_from_normal
from sky_claw.local.native_parallax.research.trust_proxies import OracleOnly

# §6 del preregistro — congelados. NO retunear después de ver resultados (§22 del brief).
T_SELF_RMSE = 0.10
T_SELF_CORR = 0.95
T_SELF_VAR = 0.85
T_DELTA_RMSE = 0.02
T_DELTA_CORR = 0.10

BOOTSTRAP_SEED = 20260925
BOOTSTRAP_N = 2000

EXP_PAIR_MODEL_MISMATCH_DOMINANT = "EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT"
EXP_SOLVER_LIMITATION_PRESENT = "EXP_M4_SOLVER_LIMITATION_PRESENT"
EXP_MIXED = "EXP_M4_MIXED"
EXP_DATA_INSUFFICIENT = "EXP_M4_DATA_INSUFFICIENT"
EXP_INVALIDATED_BY_UPSTREAM_BUG = "EXP_M4_INVALIDATED_BY_UPSTREAM_BUG"


def _check_finite(name: str, arr: np.ndarray) -> None:
    """Fail-fast NaN/Inf (contrato M0/M2: el JSON sale con allow_nan=False)."""
    a = np.asarray(arr, dtype=np.float64)
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{name}: contiene NaN/Inf (fail-fast M4)")


def self_forward(height: np.ndarray, *, bits: int | None = 8, sx: float = 1.0, sy: float = 1.0) -> np.ndarray:
    """Forward matched: H → (p, q) espectrales → normal unitaria (→ Q8 opcional).

    ``bits=None`` da la normal float exacta (control secundario que aísla la
    cuantización del techo del solver). ``bits=8`` es el primario SELF-q8: misma
    fidelidad de almacenamiento que los PNG authored.
    """
    h = np.asarray(height, dtype=np.float64)
    if h.ndim != 2:
        raise ValueError(f"self_forward: height debe ser 2D, recibí {h.shape}")
    _check_finite("self_forward(height)", h)
    p, q = spectral_gradients(h)
    normal = normals_from_gradients(p, q, sx, sy)
    if bits is not None:
        normal = quantize_decode(normal, bits)
    return np.asarray(normal, dtype=np.float64)


def fd_forward(height: np.ndarray, *, bits: int | None = 8, sx: float = 1.0, sy: float = 1.0) -> np.ndarray:
    """Control diagnóstico §8: forward por diferencias finitas centrales (wrap periódico).

    NUNCA reemplaza al par matched como primario: mide el mismatch FD↔espectral del
    mismo contenido (diagnóstico del techo, no una alternativa de pipeline).
    """
    h = np.asarray(height, dtype=np.float64)
    if h.ndim != 2:
        raise ValueError(f"fd_forward: height debe ser 2D, recibí {h.shape}")
    _check_finite("fd_forward(height)", h)
    px = (np.roll(h, -1, axis=1) - np.roll(h, 1, axis=1)) / 2.0
    qy = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) / 2.0
    normal = normals_from_gradients(px, qy, sx, sy)
    if bits is not None:
        normal = quantize_decode(normal, bits)
    return np.asarray(normal, dtype=np.float64)


def solve_normal(normal: np.ndarray) -> tuple[np.ndarray, Any]:
    """El solver M2/M3 EXACTO (RAW: p=-sx·nx/nz con guard 1e-30 + integrate_periodic)."""
    n = np.asarray(normal, dtype=np.float64)
    if n.ndim != 3 or n.shape[-1] != 3:
        raise ValueError(f"solve_normal: normal debe ser (H, W, 3), recibí {n.shape}")
    _check_finite("solve_normal(normal)", n)
    return reconstruct_from_normal(n, "RAW", 0.0, 0.0)


def evaluate_path(height: np.ndarray, normal: np.ndarray, *, path: str) -> dict[str, float]:
    """Solver + oráculo M2/M3 sobre UN normal dado; métricas preregistradas del camino.

    La escala afín global (con signo) se fita contra el height authored del MISMO
    asset (OracleOnly.fit_global_scale — idéntico contrato M2/M3, evaluation-only).
    """
    rec, stats = solve_normal(normal)
    fit = OracleOnly.fit_global_scale(height, rec)
    # la escala YA incluye el signo (cov/var): NO re-aplicar best_sign (M2 §36)
    rec_eval = rec * fit["affine_scale"]
    m = OracleOnly.evaluate(height, rec_eval, normal)
    row: dict[str, float] = {
        "path_rmse": float(m["rmse"]),
        "path_abs_corr": float(abs(m["corr"])),
        "path_variance_ratio": float(m["variance_ratio"]),
        "path_gradient_rmse": float(m["gradient_rmse"]),
        "path_raw_centered_rmse": float(m["raw_centered_rmse"]),
        "path_seam_height": float(m["seam_height"]),
        "path_seam_gradient": float(m["seam_gradient"]),
        "path_affine_scale": float(fit["affine_scale"]),
        "path_negative_nz_fraction": float(stats.negative_nz_fraction),
        "path_max_gradient": float(stats.max_gradient),
    }
    for key, value in row.items():
        if not np.isfinite(value):
            raise ValueError(f"evaluate_path[{path}]: {key} no finito (fail-fast M4)")
    return row


def delta_stats(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Medianas pareadas primarias sobre un conjunto de filas (§2/§5 del preregistro)."""
    if not rows:
        raise ValueError("delta_stats: sin filas")
    for key in ("self_rmse", "auth_rmse", "self_abs_corr", "auth_abs_corr", "self_var", "auth_var", "delta_rmse"):
        for r in rows:
            if not np.isfinite(float(r[key])):
                raise ValueError(f"delta_stats: {key} no finito en {r.get('asset', '?')}")
    return {
        "rmse_self_median": float(np.median([r["self_rmse"] for r in rows])),
        "rmse_auth_median": float(np.median([r["auth_rmse"] for r in rows])),
        "delta_rmse_median": float(np.median([r["delta_rmse"] for r in rows])),
        "abs_corr_self_median": float(np.median([r["self_abs_corr"] for r in rows])),
        "abs_corr_auth_median": float(np.median([r["auth_abs_corr"] for r in rows])),
        "var_self_median": float(np.median([r["self_var"] for r in rows])),
        "var_auth_median": float(np.median([r["auth_var"] for r in rows])),
    }


def evaluate_rules(full: dict[str, float], heldout: dict[str, float]) -> dict[str, bool]:
    """C1 (SELF-good) y C2 (AUTH-worse) — §6 del preregistro, EXACTO."""
    c1 = (
        full["rmse_self_median"] <= T_SELF_RMSE
        and full["abs_corr_self_median"] >= T_SELF_CORR
        and full["var_self_median"] >= T_SELF_VAR
    )
    c2 = (
        full["delta_rmse_median"] >= T_DELTA_RMSE
        and full["delta_rmse_median"] >= full["rmse_self_median"]
        and (full["abs_corr_self_median"] - full["abs_corr_auth_median"]) >= T_DELTA_CORR
        # enmienda pre-resultados §24-D: barra de magnitud held-out (misma T_DELTA_RMSE),
        # no sólo dirección; Δ|corr| held-out queda direccional (ver preregistro §6).
        and heldout["delta_rmse_median"] >= T_DELTA_RMSE
        and (heldout["abs_corr_self_median"] - heldout["abs_corr_auth_median"]) > 0.0
    )
    return {"C1_self_good": bool(c1), "C2_auth_worse": bool(c2)}


def decide(c1: bool, c2: bool) -> str:
    """§6: precedencia total sin solapamiento. G0 (data) se maneja en el runner."""
    if c1 and c2:
        return EXP_PAIR_MODEL_MISMATCH_DOMINANT
    if not c1 and not c2:
        return EXP_SOLVER_LIMITATION_PRESENT
    return EXP_MIXED


def bootstrap_median_ci(
    values: list[float], *, n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED
) -> dict[str, float]:
    """IC 95% bootstrap de la mediana (honestidad small-n §11; seed preregistrada)."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0 or not np.all(np.isfinite(arr)):
        raise ValueError("bootstrap_median_ci: valores vacíos o no finitos")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_boot, arr.size))
    meds = np.median(arr[idx], axis=1)
    return {
        "point": float(np.median(arr)),
        "ci95_low": float(np.quantile(meds, 0.025)),
        "ci95_high": float(np.quantile(meds, 0.975)),
    }
