"""Runner EXP-M1 — comparación de políticas nz bajo ruido (research-only).

    python -m sky_claw.local.native_parallax.research.run_exp_m1 [--resolution 256]

Stages:
    A  curvas riesgo→error (RAW) en el barrido nz×sigma
    B  comparación de políticas en CALIBRATION + selección mecánica de k
    C  evaluación held-out + sensibilidad a mis-estimación de sigma
    D  nz negativo: cluster coherente vs píxeles aislados
    E  coste por política

Reutiliza íntegramente el banco NP-M0 (superficies, solver, métricas). El ruido es
SYNTHETIC NORMAL NOISE gaussiano; la cuantización 2-canal es Q8_XY_RECONSTRUCT_Z (no
modela compresión por bloques). Todas las seeds son estables (CRC32).
"""

from __future__ import annotations

import argparse
import time
import zlib
from collections.abc import Callable
from typing import Any

import numpy as np

from sky_claw.local.native_parallax.research import metrics as np_m0_metrics
from sky_claw.local.native_parallax.research.normal_fft_periodic import integrate_periodic
from sky_claw.local.native_parallax.research.normal_from_height import (
    normals_from_gradients,
    quantize_decode,
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.nz_policies import (
    anti_flatten_metrics,
    decode_gradients_policy,
    quantize_xy_reconstruct_z,
)

CALIBRATION_SURFACES = ("S02_sine_x", "S06_multifreq", "S08_ridges", "S14_steep")
EVALUATION_SURFACES = ("S03_sine_y", "S05_diagonal", "S09_bricks", "S10_asymmetric", "S12_mixed_scale")
NZ_TARGETS = (0.5, 0.25, 0.10, 0.05, 0.025, 0.01, 0.005, 0.0025, 0.001)
K_GRID = (0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0)
K_GRID_ZERO = (2.0, 4.0, 8.0)

# Generador del barrido nz: h = A·(sin(2π·6x) + 0.8·cos(2π·5y)); |∇h|max = A·g_norm.
_SWEEP_WX, _SWEEP_WY = 2.0 * np.pi * 6.0, 2.0 * np.pi * 5.0 * 0.8
_G_NORM = float(np.hypot(_SWEEP_WX, _SWEEP_WY))


def nz_sweep_surface(n_rows: int, n_cols: int, nz_target: float) -> tuple[np.ndarray, float]:
    """Superficie con nz_min ≈ nz_target (amplitud resuelta analíticamente)."""
    amp = float(np.sqrt(max(1.0 / (nz_target * nz_target) - 1.0, 0.0)) / _G_NORM)
    y = np.arange(n_rows) / n_rows
    x = np.arange(n_cols) / n_cols
    xx, yy = np.meshgrid(x, y, indexing="xy")
    h = amp * (np.sin(_SWEEP_WX * xx) + 0.8 * np.cos(_SWEEP_WY * yy))
    return np.asarray(h, dtype=np.float64), amp


def _stable_seed(case: str, tag: str) -> int:
    return zlib.crc32(f"{case}|{tag}".encode()) & 0x7FFFFFFF


def execute_case(
    case: str,
    h: np.ndarray,
    *,
    sigma: float,
    quant: str,
    policy: str,
    k: float,
    lam_override: float | None = None,
    surf_for_gt: str = "sweep",
    with_antiflatten: bool = False,
) -> dict[str, Any]:
    """H → normal → perturbación → política → solver → métricas (una sola pasada)."""
    t0 = time.perf_counter()
    p, q = spectral_gradients(h)
    n = normals_from_gradients(p, q)
    if quant == "Q8":
        n = quantize_decode(n, bits=8)
    elif quant == "Q8_XY":
        n = quantize_xy_reconstruct_z(n, bits=8)
    n = _apply_noise(n, sigma, _stable_seed(case, f"{surf_for_gt}|{sigma:.6f}|{quant}"))
    nz = n[..., 2]
    lam = 0.0 if policy == "RAW" else (lam_override if lam_override is not None else k * sigma)
    p2, q2, stats = decode_gradients_policy(n, 1.0, 1.0, policy, lam, sigma)
    rec, _info = integrate_periodic(p2, q2)
    runtime_ms = (time.perf_counter() - t0) * 1000.0

    row: dict[str, Any] = {
        "case": case,
        "policy": policy,
        "k": k,
        "lam": lam,
        "sigma": sigma,
        "quant": quant,
        "nz_min": float(nz.min()),
        "nz_p01": float(np.percentile(nz, 1)),
        "negative_nz_fraction": stats.negative_nz_fraction,
        "activation_fraction": stats.activation_fraction,
        "low_trust_fraction": stats.low_trust_fraction,
        "max_gradient": stats.max_gradient,
        "p99_gradient": stats.p99_gradient,
        "runtime_ms": runtime_ms,
    }
    m = np_m0_metrics.compute_all(h, rec, p, q, n)
    row.update(m)
    if with_antiflatten:
        row.update(anti_flatten_metrics(rec, h, p, q))
    return row


def _apply_noise(n: np.ndarray, sigma: float, seed: int) -> np.ndarray:
    if sigma <= 0.0:
        return n
    rng = np.random.default_rng(seed)
    noisy = n + rng.normal(0.0, sigma, size=n.shape)
    return np.asarray(noisy / np.linalg.norm(noisy, axis=-1, keepdims=True), dtype=np.float64)


def _stat(rows: list[dict[str, Any]], key: str, fn: Callable[[list[float]], float]) -> float:
    vals = [float(r[key]) for r in rows if isinstance(r.get(key), (int, float))]
    return float(fn(vals)) if vals else float("nan")


def select_candidate_k(rows: list[dict[str, Any]], family: str) -> tuple[float, float, float]:
    """Selección mecánica DOS-REGÍMENES (§22/§24), solo con datos de CALIBRATION.

    Lección de la primera versión (naive): un solo guardia de detalle sobre todo el
    corpus mezcla el régimen irrecuperable (donde variance_ratio es inútil) con el
    sobrevivable (donde es la métrica que importa) y fuerza el fallback. Regla
    refinada, aún mecánica y calibration-only:

    - **Detalle** (filas nz~0.05 = sobrevivables): entre los k con
      ``min(variance_ratio) ≥ 0.90``, elegir el de menor ``median(raw_centered_rmse)``.
    - **Contención** (filas nz~0.005 = duros): exigir
      ``median(max_gradient) ≤ 1%`` de la mediana de RAW en esas mismas filas
      (acota el radio de la explosión; no pretende rescatar geometría).

    Devuelve ``(k, med_rmse_survivable, min_var_survivable)``; los k que fallan
    contención quedan excluidos aunque ganen en detalle.
    """
    k_grid = (*K_GRID, *K_GRID_ZERO) if family == "FLOOR_ZERO" else K_GRID
    fam = [r for r in rows if r["policy"] == family and float(r["sigma"]) > 0]
    raw_hard = [r for r in rows if r["policy"] == "RAW" and "nz0.005" in str(r["case"])]
    raw_blast = _stat(raw_hard, "max_gradient", np.median) if raw_hard else float("inf")
    best: tuple[float, float, float] | None = None
    fallback: tuple[float, float, float] | None = None
    for k in k_grid:
        grp = [r for r in fam if abs(float(r["k"]) - k) < 1e-12]
        if not grp:
            continue
        grp_surv = [r for r in grp if "nz0.005" not in str(r["case"])]
        med_surv = _stat(grp_surv, "raw_centered_rmse", np.median)
        min_var_surv = _stat(grp_surv, "variance_ratio", np.min)
        med_blast = _stat(grp, "max_gradient", np.median)
        cand = (float(k), float(med_surv), float(min_var_surv))
        if fallback is None or cand[1] < fallback[1]:
            fallback = cand
        detalle_ok = min_var_surv >= 0.90
        contencion_ok = med_blast <= 0.01 * raw_blast
        if detalle_ok and contencion_ok and (best is None or cand[1] < best[1]):
            best = cand
    chosen = best if best is not None else fallback
    assert chosen is not None, f"sin datos para {family}"
    return chosen


def stage_a(res: int) -> list[dict[str, Any]]:
    """A — curvas riesgo→error con RAW (¿r = nz/σ predice la explosión?)."""
    rows = []
    for surf in ("S06_multifreq", "S08_ridges", "S09_bricks", "S14_steep"):
        for nz_target in NZ_TARGETS:
            h, _amp = nz_sweep_surface(res, res, nz_target)
            for sigma in (1.0 / 255, 2.0 / 255, 4.0 / 255, 8.0 / 255):
                rows.append(
                    execute_case(
                        f"A|{surf}|nz~{nz_target:g}",
                        h,
                        sigma=sigma,
                        quant="none",
                        policy="RAW",
                        k=0.0,
                        surf_for_gt=surf,
                    )
                )
    return rows


def stage_b(res: int) -> list[dict[str, Any]]:
    """B — comparación de políticas en CALIBRATION (Q8 + sin cuantizar, 2 regímenes nz)."""
    rows = []
    for surf in CALIBRATION_SURFACES:
        for nz_target in (0.05, 0.005):
            h, _amp = nz_sweep_surface(res, res, nz_target)
            for sigma in (1.0 / 255, 2.0 / 255, 4.0 / 255):
                for quant in ("Q8", "none"):
                    tag = f"B|{surf}|nz{nz_target:g}|{quant}"
                    rows.append(
                        execute_case(
                            tag,
                            h,
                            sigma=sigma,
                            quant=quant,
                            policy="RAW",
                            k=0.0,
                            surf_for_gt=surf,
                            with_antiflatten=True,
                        )
                    )
                    for k in K_GRID:
                        rows.append(
                            execute_case(
                                tag,
                                h,
                                sigma=sigma,
                                quant=quant,
                                policy="FLOOR_CLAMP",
                                k=k,
                                surf_for_gt=surf,
                                with_antiflatten=True,
                            )
                        )
                        rows.append(
                            execute_case(
                                tag,
                                h,
                                sigma=sigma,
                                quant=quant,
                                policy="SOFT_TIKHONOV",
                                k=k,
                                surf_for_gt=surf,
                                with_antiflatten=True,
                            )
                        )
                    for k in K_GRID_ZERO:
                        rows.append(
                            execute_case(
                                tag,
                                h,
                                sigma=sigma,
                                quant=quant,
                                policy="FLOOR_ZERO",
                                k=k,
                                surf_for_gt=surf,
                                with_antiflatten=True,
                            )
                        )
    return rows


def stage_c(res: int, winners: dict[str, float]) -> list[dict[str, Any]]:
    """C — held-out (superficies NO usadas para elegir) + mis-estimación de sigma."""
    rows = []
    for surf in EVALUATION_SURFACES:
        for nz_target in (0.05, 0.005):
            h, _amp = nz_sweep_surface(res, res, nz_target)
            for sigma in (1.0 / 255, 2.0 / 255, 4.0 / 255):
                tag = f"C|{surf}|nz{nz_target:g}"
                rows.append(
                    execute_case(
                        tag, h, sigma=sigma, quant="Q8", policy="RAW", k=0.0, surf_for_gt=surf, with_antiflatten=True
                    )
                )
                for family, k in winners.items():
                    rows.append(
                        execute_case(
                            tag, h, sigma=sigma, quant="Q8", policy=family, k=k, surf_for_gt=surf, with_antiflatten=True
                        )
                    )
                    for ratio in (0.5, 2.0, 4.0):
                        rows.append(
                            execute_case(
                                f"{tag}|se{ratio:g}",
                                h,
                                sigma=sigma,
                                quant="Q8",
                                policy=family,
                                k=k,
                                lam_override=k * sigma * ratio,
                                surf_for_gt=surf,
                                with_antiflatten=True,
                            )
                        )
    return rows


def stage_d(res: int) -> list[dict[str, Any]]:
    """D — nz negativo: cluster coherente vs píxeles aislados (§17)."""
    rows = []
    h, _amp = nz_sweep_surface(res, res, 0.01)
    p, q = spectral_gradients(h)
    n = normals_from_gradients(p, q)
    yy, xx = np.meshgrid(np.arange(res) / res, np.arange(res) / res, indexing="ij")
    disk = (xx - 0.5) ** 2 + (yy - 0.5) ** 2 < 0.1**2
    rng = np.random.default_rng(_stable_seed("D", "isolated"))
    iso = np.zeros((res, res), dtype=bool)
    iso[rng.integers(0, res, size=10), rng.integers(0, res, size=10)] = True
    for tag, mask in (("coherent_disk", disk), ("isolated_10px", iso)):
        n_bad = n.copy()
        n_bad[..., 2] = np.where(mask, -n_bad[..., 2], n_bad[..., 2])
        for policy, k in (
            ("RAW", 0.0),
            ("FLOOR_CLAMP", 2.0),
            ("FLOOR_CLAMP", 6.0),
            ("FLOOR_ZERO", 4.0),
            ("SOFT_TIKHONOV", 4.0),
        ):
            p2, q2, stats = decode_gradients_policy(n_bad, 1.0, 1.0, policy, k * (1.0 / 255), 1.0 / 255)
            rec, _info = integrate_periodic(p2, q2)
            m = np_m0_metrics.compute_all(h, rec, p, q, n_bad)
            rows.append(
                {
                    "case": f"D|{tag}|{policy}|k{k:g}",
                    "negative_nz_fraction": stats.negative_nz_fraction,
                    "activation_fraction": stats.activation_fraction,
                    "rmse": m["rmse"],
                    "raw_centered_rmse": m["raw_centered_rmse"],
                    "gradient_rmse": m["gradient_rmse"],
                    "seam_gradient": m["seam_gradient"],
                    "max_gradient": stats.max_gradient,
                }
            )
    return rows


def stage_e() -> list[dict[str, Any]]:
    """E — coste marginal de cada política (1024²; el FFT domina: medir, no optimizar)."""
    rows = []
    h, _amp = nz_sweep_surface(1024, 1024, 0.01)
    p, q = spectral_gradients(h)
    n = normals_from_gradients(p, q)
    n = _apply_noise(n, 2.0 / 255, _stable_seed("E", "perf"))
    for policy in ("RAW", "FLOOR_CLAMP", "FLOOR_ZERO", "SOFT_TIKHONOV"):
        t0 = time.perf_counter()
        for _ in range(20):
            decode_gradients_policy(n, 1.0, 1.0, policy, 2.0 / 255, 2.0 / 255)
        decode_ms = (time.perf_counter() - t0) / 20 * 1000
        t0 = time.perf_counter()
        integrate_periodic(p, q)
        fft_ms = (time.perf_counter() - t0) * 1000
        rows.append({"case": f"E|{policy}|1024", "runtime_ms": decode_ms, "max_gradient": fft_ms})
    return rows


COLUMNS_A = (
    "case",
    "nz_min",
    "nz_p01",
    "sigma",
    "rmse",
    "gradient_rmse",
    "negative_nz_fraction",
    "seam_gradient",
    "max_gradient",
    "runtime_ms",
)
COLUMNS_B = (
    "case",
    "policy",
    "k",
    "nz_min",
    "negative_nz_fraction",
    "activation_fraction",
    "low_trust_fraction",
    "rmse",
    "raw_centered_rmse",
    "gradient_rmse",
    "normal_angle_mean",
    "seam_gradient",
    "max_gradient",
    "p99_gradient",
    "variance_ratio",
    "gradient_energy_ratio",
    "hf_energy_ratio",
    "best_sign",
    "runtime_ms",
)
COLUMNS_D = (
    "case",
    "negative_nz_fraction",
    "activation_fraction",
    "rmse",
    "raw_centered_rmse",
    "gradient_rmse",
    "seam_gradient",
    "max_gradient",
)
COLUMNS_E = ("case", "runtime_ms", "max_gradient")


def markdown_rows(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> str:
    head = "| " + " | ".join(columns) + " |"
    sep = "|" + "|".join("---:" for _ in columns) + "|"

    def fmt(v: Any) -> str:
        if isinstance(v, float):
            if abs(v) < 1e-12:
                return "0"
            return f"{v:.6g}"
        return str(v)

    lines = [head, sep]
    for r in rows:
        lines.append("| " + " | ".join(fmt(r.get(c, "")) for c in columns) + " |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="EXP-M1: políticas nz/trust")
    parser.add_argument("--resolution", type=int, default=256)
    args = parser.parse_args()
    res = args.resolution

    print("## Stage A — riesgo RAW: r = nz_min/sigma vs error\n")
    print(markdown_rows(stage_a(res), COLUMNS_A))

    print("\n## Stage B — políticas en CALIBRATION\n")
    rows_b = stage_b(res)
    print(markdown_rows(rows_b, COLUMNS_B))
    print("\n### Selección mecánica por familia (calibración, guardia var>=0.90)\n")
    winners: dict[str, float] = {}
    for family in ("FLOOR_CLAMP", "FLOOR_ZERO", "SOFT_TIKHONOV"):
        k, med, min_var = select_candidate_k(rows_b, family)
        winners[family] = k
        print(f"- {family}: k={k:g}, median(raw_centered_rmse)={med:.6g}, min(variance_ratio)={min_var:.4g}")

    print("\n## Stage C — held-out + mis-estimación de sigma\n")
    print(markdown_rows(stage_c(res, winners), COLUMNS_B))

    print("\n## Stage D — nz negativo\n")
    print(markdown_rows(stage_d(res), COLUMNS_D))

    print("\n## Stage E — coste por política (1024², decode vs FFT)\n")
    print(markdown_rows(stage_e(), COLUMNS_E))


if __name__ == "__main__":
    main()
