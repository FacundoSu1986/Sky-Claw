"""PHASE B — Contrato de unidades del gradiente normal (AUDIT_ONLY, READ-ONLY).

NO implementa normal_fft_periodic_v2 ni toca codigo cientifico. Sólo:
  1. Reconstruye el contrato IMPLEMENTADO desde codigo/tests/docs.
  2. Reproduce la observación numérica del audit externo (sintético rectangular).
  3. Demuestra explícitamente la CIRCULARIDAD: el mismo sintético construido bajo el
     contrato UV_NORMALIZED da el resultado OPUESTO. Por lo tanto el sintético
     rectangular NO puede adjudicar cuál contrato es "correcto".

Uso:
  PYTHONPATH=<worktree> python phase_b_units.py --out <json>
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

from sky_claw.local.native_parallax.research.normal_fft_periodic import freq_axes, integrate_periodic
from sky_claw.local.native_parallax.research.normal_from_height import normals_from_gradients

# ------------------------------------------------------------------ convenciones
# v1 (IMPLEMENTADA): x = j/W, y = i/H  ->  wx = 2π·fftfreq(W)·W, wy = 2π·fftfreq(H)·H
# v2 (PROPUESTA por el audit): x = j/S, y = i/S con S = max(H,W)  ->  wx = 2π·S·fftfreq(W)


def freq_axes_v1(n_rows: int, n_cols: int) -> tuple[np.ndarray, np.ndarray]:
    return freq_axes(n_rows, n_cols)


def freq_axes_v2(n_rows: int, n_cols: int) -> tuple[np.ndarray, np.ndarray]:
    s = float(max(n_rows, n_cols))
    fy = np.fft.fftfreq(n_rows)
    fx = np.fft.fftfreq(n_cols)
    wy = 2.0 * np.pi * s * fy
    wx = 2.0 * np.pi * s * fx
    wy[np.abs(np.abs(fy) - 0.5) < 1e-12] = 0.0
    wx[np.abs(np.abs(fx) - 0.5) < 1e-12] = 0.0
    return wy.reshape(n_rows, 1), wx.reshape(1, n_cols)


def grad_v(h: np.ndarray, which: str) -> tuple[np.ndarray, np.ndarray]:
    n_rows, n_cols = h.shape
    wy, wx = freq_axes_v1(n_rows, n_cols) if which == "v1" else freq_axes_v2(n_rows, n_cols)
    h_hat = np.fft.fft2(h)
    p = np.real(np.fft.ifft2(1j * wx * h_hat))
    q = np.real(np.fft.ifft2(1j * wy * h_hat))
    return p, q


def integrate_v(p: np.ndarray, q: np.ndarray, which: str) -> np.ndarray:
    n_rows, n_cols = p.shape
    wy, wx = freq_axes_v1(n_rows, n_cols) if which == "v1" else freq_axes_v2(n_rows, n_cols)
    denom = wx * wx + wy * wy
    nulo = denom == 0.0
    denom = np.where(nulo, 1.0, denom)
    h_hat = (-1j * wx * np.fft.fft2(p) - 1j * wy * np.fft.fft2(q)) / denom
    h_hat[nulo] = 0.0
    return np.asarray(np.fft.ifft2(h_hat).real, dtype=np.float64)


def corr(a: np.ndarray, b: np.ndarray) -> float:
    ac, bc = a.ravel() - a.mean(), b.ravel() - b.mean()
    d = float(np.linalg.norm(ac) * np.linalg.norm(bc))
    return float(np.dot(ac, bc) / d) if d > 0 else float("nan")


def rect_case(n_rows: int, n_cols: int, *, slope_contract: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sintético rectangular PERIÓDICO en el grid, con pendiente según un contrato.

    ``h`` es periódica en índice (j/W, i/H), así que la derivada FFT es exacta.
    Lo que cambia entre contratos es la DEFINICIÓN de la coordenada física:

      texel_isotropic : x = j/S, y = i/S con S = max(H,W)   -> p = S·dh/dj, q = S·dh/di
      uv_normalized   : x = j/W, y = i/H                    -> p = W·dh/dj, q = H·dh/di

    Devuelve (h, p, q, normal). El audit externo construye el sintético con el primer
    contrato; el repo implementa el segundo.
    """
    j = np.arange(n_cols, dtype=np.float64)[None, :]
    i = np.arange(n_rows, dtype=np.float64)[:, None]
    k = 2.0 * np.pi
    h = (np.sin(k * j / n_cols) + np.cos(k * i / n_rows)) / k
    # dh/dx con x=j/W  y  dh/dy con y=i/H  (pendiente "UV-normalizada" exacta)
    dp_dx = np.broadcast_to(np.cos(k * j / n_cols), h.shape).copy()
    dp_dy = np.broadcast_to(-np.sin(k * i / n_rows), h.shape).copy()
    if slope_contract == "texel_isotropic":
        # x = j/S, y = i/S con S = max(H,W): dh/dx = (S/W)·(dh/dx_uv)
        s = float(max(n_rows, n_cols))
        p, q = dp_dx * (s / n_cols), dp_dy * (s / n_rows)
    else:  # uv_normalized
        p, q = dp_dx, dp_dy
    normal = normals_from_gradients(p, q, 1.0, 1.0)
    return np.ascontiguousarray(h), p, q, normal


def evaluate(n_rows: int, n_cols: int, slope_contract: str) -> dict[str, Any]:
    h, p_syn, q_syn, normal = rect_case(n_rows, n_cols, slope_contract=slope_contract)
    out: dict[str, Any] = {"shape": [n_rows, n_cols], "slope_contract": slope_contract}
    for which in ("v1", "v2"):
        p, q = grad_v(h, which)
        n_der = normals_from_gradients(p, q, 1.0, 1.0)
        dot = np.clip(np.sum(normal * n_der, axis=-1), -1.0, 1.0)
        rec = integrate_v(p_syn, q_syn, which)
        out[which] = {
            "corr_normal_vs_derived": corr(normal, n_der),
            "median_angle_deg": float(np.median(np.degrees(np.arccos(dot)))),
            "roundtrip_corr": corr(h, rec),
            "roundtrip_rmse": float(np.sqrt(np.mean((h - rec) ** 2))),
        }
    return out


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    shapes = [(64, 128), (128, 64), (96, 160), (63, 65), (128, 128)]
    payload: dict[str, Any] = {
        "phase": "B_UNIT_CONTRACT",
        "audit_only": True,
        "implemented_contract": "UV_NORMALIZED",
        "contract_evidence": {
            "normal_fft_periodic.freq_axes": "wx = 2π·fftfreq(W)·W  (rad por unidad de x, x=j/W)",
            "normal_fft_periodic.docstring": "x = j/W ∈ [0,1), y = i/H — coordenadas de textura, NO índices de muestra",
            "test": "tests/test_native_parallax_math_spike.py::test_frecuencias_en_espacio_de_textura "
                    "congela wx[0,1]==2π y wy[1,0]==2π sobre la grilla NO CUADRADA (8,16)",
            "np_m0_results": "docs/design/research/native-parallax/np-m0-results.md:40-41,322-324 "
                             "(2π·fftfreq = rad/muestra vs rad/unidad-de-textura)",
        },
        "circularity_probe": {},
        "tests": {},
    }
    for n_rows, n_cols in shapes:
        key = f"{n_rows}x{n_cols}"
        # A) sintético construido bajo TEXEL_ISOTROPIC (lo que asume el audit externo)
        a = evaluate(n_rows, n_cols, "texel_isotropic")
        # B) el MISMO grid construido bajo UV_NORMALIZED (lo que asume el repo)
        b = evaluate(n_rows, n_cols, "uv_normalized")
        payload["tests"][key] = {"synthetic_texel_isotropic": a, "synthetic_uv_normalized": b}
        payload["circularity_probe"][key] = {
            "audit_claim_reproduced": bool(
                a["v1"]["corr_normal_vs_derived"] < 0.999 and a["v2"]["corr_normal_vs_derived"] > 0.999
            ),
            "opposite_result_under_other_contract": bool(
                b["v2"]["corr_normal_vs_derived"] < 0.999 and b["v1"]["corr_normal_vs_derived"] > 0.999
            ),
        }

    # identidad v1 vs v2 en cuadradas (debe ser exacta)
    sq_a = evaluate(128, 128, "uv_normalized")
    payload["square_identity"] = {
        "v1_corr": sq_a["v1"]["roundtrip_corr"],
        "v2_corr": sq_a["v2"]["roundtrip_corr"],
        "abs_diff": abs(sq_a["v1"]["roundtrip_corr"] - sq_a["v2"]["roundtrip_corr"]),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, allow_nan=False)
    print(f"OK -> {args.out}")
    for k, v in payload["circularity_probe"].items():
        print(f"{k:10s} audit_claim_reproduced={v['audit_claim_reproduced']} "
              f"opposite_under_other_contract={v['opposite_result_under_other_contract']}")
    print("square identity abs_diff:", payload["square_identity"]["abs_diff"])


if __name__ == "__main__":
    main()
