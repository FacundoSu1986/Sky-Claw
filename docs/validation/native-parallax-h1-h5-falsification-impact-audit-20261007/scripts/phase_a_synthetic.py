"""PHASE A — Falsificación sintética H1 / H3 / H4 / H5 (AUDIT_ONLY, READ-ONLY).

No modifica código científico ni artefactos históricos. No ejecuta run_exp_m4 / run_exp_m5.
Solo: lectura, hashing, diagnósticos sintéticos, contrafactuales en memoria.

Uso:
    PYTHONPATH=<worktree> python phase_a_synthetic.py --out <json>

Base: 97dcc7ab9ade29153faa0ccec428b78612cfc04a
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from sky_claw.local.native_parallax.research.authored_dataset import resize_height, resize_normal
from sky_claw.local.native_parallax.research.normal_from_height import (
    normals_from_gradients,
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.solver_coherence import (
    T_DELTA_RMSE,
    evaluate_path,
    fd_forward,
    self_forward,
)
from sky_claw.local.native_parallax.research.synthetic_height import PERIODIC_CASES
from sky_claw.local.native_parallax.research.trust_proxies import OracleOnly

# Rejilla EXACTA que usa main (verificada por lectura + por introspección en el JSON).
CURRENT_GRID_N = 25
CURRENT_GRID_LO = 0.05
CURRENT_GRID_HI = 5.0


def current_grid() -> np.ndarray:
    return np.concatenate(
        [np.linspace(CURRENT_GRID_LO, CURRENT_GRID_HI, CURRENT_GRID_N), -np.linspace(CURRENT_GRID_LO, CURRENT_GRID_HI, CURRENT_GRID_N)]
    )


# --------------------------------------------------------------------------- H1
def median_angle_deg(normal: np.ndarray, h_centered: np.ndarray, s: float) -> float:
    """Réplica EXACTA del cuerpo del oráculo (trust_proxies:297-302) para un solo s."""
    h = h_centered * float(s)
    p, q = spectral_gradients(h)
    n2 = normals_from_gradients(p, q, sx=1.0, sy=1.0)
    dot = np.clip(np.sum(normal * n2, axis=-1), -1.0, 1.0)
    return float(np.median(np.degrees(np.arccos(dot))))


def closed_form_strength(normal: np.ndarray, h_centered: np.ndarray, nz_floor: float = 1e-3) -> float:
    """LS global en espacio de gradiente con pesos nz^2 (S2 del audit externo, re-derivado).

    s0 = <w * g_auth, g_h> / <w * g_h, g_h>.  Para un par perfectamente coherente
    construido a strength t, g_auth = t * g_h pixel a pixel => s0 = t EXACTO.
    """
    nz = np.maximum(normal[..., 2], nz_floor)
    pa, qa = -normal[..., 0] / nz, -normal[..., 1] / nz
    ph, qh = spectral_gradients(h_centered)
    w = np.clip(normal[..., 2], 0.0, 1.0) ** 2
    den = float(np.sum(w * (ph * ph + qh * qh)))
    if den <= 0.0:
        return 0.0
    return float(np.sum(w * (pa * ph + qa * qh)) / den)


def h1_grid_vs_continuous(case: str, n: int, strengths: list[float]) -> dict[str, Any]:
    h_raw = PERIODIC_CASES[case](n, n)
    h = h_raw - h_raw.mean()
    # a = |grad h|^2 (strength 1): permite evaluar la métrica angular para CUALQUIER s
    # de forma analítica — la misma cuenta que hace el oráculo, sin muestrear la rejilla.
    p0, q0 = spectral_gradients(h)
    a = p0 * p0 + q0 * q0

    grid = current_grid()
    rows: list[dict[str, Any]] = []
    for t in strengths:
        pt, qt = spectral_gradients(h * t)
        normal = normals_from_gradients(pt, qt)
        # --- oráculo ACTUAL (código real, sin tocar)
        cur = OracleOnly.normal_height_residual_oracle(normal, h_raw)
        grid_ang = float(cur["height_normal_oracle_agreement_deg"])
        grid_best = float(cur["oracle_best_strength"])
        # --- cross-check: la réplica reproduce el oráculo en la rejilla
        replica = min(median_angle_deg(normal, h, float(s)) for s in grid)
        # --- continuo: forma cerrada + barrido denso que INCLUYE el óptimo analítico
        s0 = closed_form_strength(normal, h)
        cont_ang_at_s0 = median_angle_deg(normal, h, s0)
        # barrido denso (log, con signo) por chunks para confirmar que s0 es el mínimo
        # global. Se evalúa la MISMA métrica del oráculo de forma analítica:
        # dot(s) = (t*s*a + 1) / sqrt((t^2 a + 1)(s^2 a + 1)), con a = |grad h|^2 a strength 1.
        mag = np.geomspace(1e-4, 1e4, 801)
        sweep = np.concatenate([mag, -mag, [s0]])
        angs = np.empty(sweep.size, dtype=np.float64)
        for i0 in range(0, sweep.size, 40):
            blk = sweep[i0 : i0 + 40]
            with np.errstate(all="ignore"):
                dots = (t * blk[:, None, None] * a[None] + 1.0) / np.sqrt(
                    (t * t * a[None] + 1.0) * (blk[:, None, None] ** 2 * a[None] + 1.0)
                )
                angs[i0 : i0 + 40] = np.median(np.degrees(np.arccos(np.clip(dots, -1.0, 1.0))), axis=(1, 2))
        k = int(np.argmin(angs))
        rows.append(
            {
                "true_strength": float(t),
                "on_grid": bool(np.any(np.isclose(grid, t))),
                "nearest_grid_abs": float(np.min(np.abs(grid - t))),
                "grid_best_strength": grid_best,
                "grid_angle_deg": grid_ang,
                "continuous_best_strength": float(sweep[k]),
                "continuous_angle_deg": float(angs[k]),
                "closed_form_strength": s0,
                "closed_form_angle_deg": cont_ang_at_s0,
                # par perfectamente coherente a strength t => dot(t) == 1 pixel a pixel => 0.0
                "analytic_angle_at_true_deg": 0.0,
                "delta_angle_deg": float(grid_ang - angs[k]),
                "oracle_replica_max_abs_diff": float(abs(replica - grid_ang)),
            }
        )
    return {"case": case, "n": n, "rows": rows}


def run_h1() -> dict[str, Any]:
    strengths = [2.0, 0.3, 0.1, 0.02, 0.003, -0.05]
    cases = ["S06_multifreq", "S07_bumps", "S09_bricks", "S14_steep", "S15_periodic_noise"]
    res = {c: h1_grid_vs_continuous(c, 256, strengths) for c in cases}
    all_rows = [r for c in res.values() for r in c["rows"]]
    finite = [r for r in all_rows if np.isfinite(r["grid_angle_deg"])]
    return {
        "grid_definition": {
            "expression": "concat(linspace(0.05, 5.0, 25), -linspace(0.05, 5.0, 25))",
            "n_points": 50,
            "step": (CURRENT_GRID_HI - CURRENT_GRID_LO) / (CURRENT_GRID_N - 1),
            "source": "sky_claw/local/native_parallax/research/trust_proxies.py:296",
        },
        "cases": res,
        "summary": {
            "max_grid_angle_deg": float(max(r["grid_angle_deg"] for r in finite)),
            "min_grid_angle_deg": float(min(r["grid_angle_deg"] for r in finite)),
            "max_continuous_angle_deg": float(max(r["continuous_angle_deg"] for r in all_rows)),
            "max_delta_angle_deg": float(max(r["delta_angle_deg"] for r in finite)),
            "median_delta_angle_deg": float(np.median([r["delta_angle_deg"] for r in finite])),
            "max_oracle_replica_abs_diff": float(max(r["oracle_replica_max_abs_diff"] for r in all_rows)),
            "max_closed_form_vs_true_abs": float(max(abs(r["closed_form_strength"] - r["true_strength"]) for r in all_rows)),
        },
    }


# --------------------------------------------------------------------------- H3
def run_h3(c_values: list[float], n: int = 256) -> dict[str, Any]:
    cases = ["S06_multifreq", "S07_bumps", "S09_bricks", "S14_steep"]
    out: dict[str, Any] = {}
    for case in cases:
        h = PERIODIC_CASES[case](n, n)
        h = h - h.mean()
        rows = []
        for c in c_values:
            hc = h * c
            m_q8 = evaluate_path(hc, self_forward(hc, bits=8), path="self_q8")
            m_fl = evaluate_path(hc, self_forward(hc, bits=None), path="self_float")
            rows.append(
                {
                    "c": float(c),
                    "self_q8_aligned_rmse": float(m_q8["path_rmse"]),
                    "self_q8_abs_corr": float(m_q8["path_abs_corr"]),
                    "self_q8_variance_ratio": float(m_q8["path_variance_ratio"]),
                    "self_q8_gradient_rmse": float(m_q8["path_gradient_rmse"]),
                    "self_float_aligned_rmse": float(m_fl["path_rmse"]),
                }
            )
        rm = [r["self_q8_aligned_rmse"] for r in rows]
        out[case] = {
            "rows": rows,
            "rmse_min": float(min(rm)),
            "rmse_max": float(max(rm)),
            "rmse_ratio_max_over_min": float(max(rm) / min(rm)) if min(rm) > 0 else None,
        }
    return {
        "cases": out,
        "summary": {
            "max_rmse_ratio_across_cases": float(max(v["rmse_ratio_max_over_min"] for v in out.values())),
        },
    }


# --------------------------------------------------------------------------- H4
def resize_normal_float_counterfactual(n: np.ndarray, size: int) -> np.ndarray:
    """FLOAT_RESIZE_NORMAL_COUNTERFACTUAL (audit-only, NO versionado).

    Idéntico a resize_normal salvo que NO re-cuantiza a uint8: resize bilineal en float32
    canal por canal + renormalización. Aísla UNA sola variable (la re-cuantización).
    """
    if n.shape[0] == size and n.shape[1] == size:
        return np.asarray(n, dtype=np.float64)
    channels = []
    for c in range(3):
        im = Image.fromarray(np.asarray(np.clip(n[..., c], -1.0, 1.0), dtype=np.float32))
        channels.append(np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64))
    out = np.stack(channels, axis=-1)
    length = np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-12)
    return np.asarray(out / length, dtype=np.float64)


def _angle_stats(n_a: np.ndarray, n_b: np.ndarray) -> dict[str, float]:
    a = n_a / np.maximum(np.linalg.norm(n_a, axis=-1, keepdims=True), 1e-12)
    b = n_b / np.maximum(np.linalg.norm(n_b, axis=-1, keepdims=True), 1e-12)
    dot = np.clip(np.sum(a * b, axis=-1), -1.0, 1.0)
    ang = np.degrees(np.arccos(dot))
    return {"median_deg": float(np.median(ang)), "mean_deg": float(np.mean(ang)), "p95_deg": float(np.percentile(ang, 95))}


def run_h4_synthetic(native: int = 1024, target: int = 512) -> dict[str, Any]:
    cases = {
        "flat_normal": np.zeros((native, native), dtype=np.float64),
        "smooth_low_slope": PERIODIC_CASES["S02_sine_x"](native, native) * 0.05,
        "moderate_slope": PERIODIC_CASES["S06_multifreq"](native, native),
        "high_frequency": PERIODIC_CASES["S11_highfreq"](native, native),
        "crease_like": PERIODIC_CASES["S08_ridges"](native, native),
    }
    out: dict[str, Any] = {}
    for name, h_nat in cases.items():
        h_nat = h_nat - h_nat.mean()
        h_ref = resize_height(h_nat, target)
        n_nat = self_forward(h_nat, bits=None) if name != "flat_normal" else np.broadcast_to(
            np.array([0.0, 0.0, 1.0]), (native, native, 3)
        ).copy()
        n_old = resize_normal(n_nat, target)
        n_new = resize_normal_float_counterfactual(n_nat, target)
        n_ideal = self_forward(h_ref, bits=None) if name != "flat_normal" else np.broadcast_to(
            np.array([0.0, 0.0, 1.0]), (target, target, 3)
        ).copy()
        m_old = evaluate_path(h_ref, n_old, path="auth_old")
        m_new = evaluate_path(h_ref, n_new, path="auth_new")
        out[name] = {
            "channel_bias_old_minus_new": {
                "nx": float(np.mean(n_old[..., 0]) - np.mean(n_new[..., 0])),
                "ny": float(np.mean(n_old[..., 1]) - np.mean(n_new[..., 1])),
                "nz": float(np.mean(n_old[..., 2]) - np.mean(n_new[..., 2])),
            },
            "flat_bias_nx_old": float(np.mean(n_old[..., 0])),
            "flat_bias_nx_new": float(np.mean(n_new[..., 0])),
            "angular_error_old_vs_new": _angle_stats(n_old, n_new),
            "angular_error_old_vs_ideal": _angle_stats(n_old, n_ideal),
            "angular_error_new_vs_ideal": _angle_stats(n_new, n_ideal),
            "normal_rmse_old_vs_new": float(np.sqrt(np.mean((n_old - n_new) ** 2))),
            "normal_rmse_old_vs_ideal": float(np.sqrt(np.mean((n_old - n_ideal) ** 2))),
            "normal_rmse_new_vs_ideal": float(np.sqrt(np.mean((n_new - n_ideal) ** 2))),
            "downstream_rmse_old": float(m_old["path_rmse"]),
            "downstream_rmse_new": float(m_new["path_rmse"]),
            "downstream_delta_rmse": float(m_old["path_rmse"] - m_new["path_rmse"]),
            "downstream_abscorr_old": float(m_old["path_abs_corr"]),
            "downstream_abscorr_new": float(m_new["path_abs_corr"]),
        }
    deltas = [v["downstream_delta_rmse"] for v in out.values()]
    return {
        "native": native,
        "target": target,
        "t_delta_rmse": T_DELTA_RMSE,
        "cases": out,
        "summary": {
            "max_downstream_delta_rmse": float(max(deltas)),
            "median_downstream_delta_rmse": float(np.median(deltas)),
            "n_cases_above_t_delta_rmse": int(sum(1 for d in deltas if abs(d) >= T_DELTA_RMSE)),
            "material_effect": bool(max(abs(d) for d in deltas) >= T_DELTA_RMSE),
        },
    }


# --------------------------------------------------------------------------- H5
def fd_forward_solver_units(height: np.ndarray, *, bits: int | None = 8) -> np.ndarray:
    """FD central en unidades del SOLVER (por unidad UV, x=j/W): factor W/2 (=s/2, s=max(shape))."""
    h = np.asarray(height, dtype=np.float64)
    s = float(max(h.shape))
    px = (np.roll(h, -1, axis=1) - np.roll(h, 1, axis=1)) * (0.5 * s)
    qy = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) * (0.5 * s)
    normal = normals_from_gradients(px, qy, 1.0, 1.0)
    if bits is not None:
        from sky_claw.local.native_parallax.research.normal_from_height import quantize_decode

        normal = quantize_decode(normal, bits)
    return np.asarray(normal, dtype=np.float64)


def run_h5(n: int = 256) -> dict[str, Any]:
    cases = ["S06_multifreq", "S11_highfreq", "S14_steep", "S09_bricks"]
    out: dict[str, Any] = {}
    for case in cases:
        h = PERIODIC_CASES[case](n, n)
        h = h - h.mean()
        r: dict[str, Any] = {}
        for bits, tag in ((None, "float"), (8, "q8")):
            m_cur = evaluate_path(h, fd_forward(h, bits=bits), path="fd_current")
            m_units = evaluate_path(h, fd_forward_solver_units(h, bits=bits), path="fd_units")
            r[tag] = {
                "fd_current_rmse": float(m_cur["path_rmse"]),
                "fd_units_rmse": float(m_units["path_rmse"]),
                "fd_current_abscorr": float(m_cur["path_abs_corr"]),
                "fd_units_abscorr": float(m_units["path_abs_corr"]),
                "ratio_current_over_units": float(m_cur["path_rmse"] / m_units["path_rmse"]) if m_units["path_rmse"] > 0 else None,
            }
        # comparación espectral vs FD (mismo contenido): ¿qué separa FD-Q8 de espectral-Q8?
        sp_q8 = evaluate_path(h, self_forward(h, bits=8), path="self_q8")
        r["spectral_q8_rmse"] = float(sp_q8["path_rmse"])
        r["fd_units_q8_vs_spectral_q8_ratio"] = (
            float(r["q8"]["fd_units_rmse"] / r["spectral_q8_rmse"]) if r["spectral_q8_rmse"] > 0 else None
        )
        out[case] = r
    return {
        "cases": out,
        "summary": {
            "float_ratio_max": float(max(v["float"]["ratio_current_over_units"] for v in out.values())),
            "q8_ratio_max": float(max(v["q8"]["ratio_current_over_units"] for v in out.values())),
        },
    }


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    payload = {
        "phase": "A_SYNTHETIC_FALSIFICATION",
        "audit_only": True,
        "base_sha": "97dcc7ab9ade29153faa0ccec428b78612cfc04a",
        "h1": run_h1(),
        "h3": run_h3([1.0, 0.3, 0.1, 0.03, 0.01, 0.003]),
        "h4_synthetic": run_h4_synthetic(),
        "h5": run_h5(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, allow_nan=False)
    print(f"OK -> {args.out}")


if __name__ == "__main__":
    main()
