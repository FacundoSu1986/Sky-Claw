"""H4 — sonda de magnitud (AUDIT_ONLY): intenta reproducir el número del audit externo.

El audit externo (H4) afirma: "En contenido suave (S07/S15, 1024->512) el RMSE es 5-21x
mayor que con resize float, y llega a 0.023 absoluto, del orden de T_DELTA_RMSE = 0.02".
Esta sonda mide, para una batería amplia de casos y amplitudes, TODAS las candidatas a
"ese RMSE" para adjudicar si el número se reproduce o no.

Uso:
  PYTHONPATH=<worktree> python h4_magnitude_probe.py --out <json>
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
from sky_claw.local.native_parallax.research.normal_from_height import normals_from_gradients, spectral_gradients
from sky_claw.local.native_parallax.research.solver_coherence import evaluate_path, self_forward
from sky_claw.local.native_parallax.research.synthetic_height import PERIODIC_CASES

NATIVE = 1024
TARGET = 512


def resize_normal_float_counterfactual(n: np.ndarray, size: int) -> np.ndarray:
    if n.shape[0] == size and n.shape[1] == size:
        return np.asarray(n, dtype=np.float64)
    ch = []
    for c in range(3):
        im = Image.fromarray(np.asarray(np.clip(n[..., c], -1.0, 1.0), dtype=np.float32))
        ch.append(np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64))
    out = np.stack(ch, axis=-1)
    return np.asarray(out / np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-12), dtype=np.float64)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    cases = ["S02_sine_x", "S06_multifreq", "S07_bumps", "S08_ridges", "S09_bricks", "S11_highfreq", "S15_periodic_noise"]
    amplitudes = [1.0, 0.3, 0.1, 0.03]
    out: dict[str, Any] = {}
    for name in cases:
        for amp in amplitudes:
            h_nat = PERIODIC_CASES[name](NATIVE, NATIVE)
            h_nat = (h_nat - h_nat.mean()) * amp
            h_ref = resize_height(h_nat, TARGET)
            n_nat = self_forward(h_nat, bits=None)
            n_old = resize_normal(n_nat, TARGET)
            n_new = resize_normal_float_counterfactual(n_nat, TARGET)
            n_ideal = self_forward(h_ref, bits=None)
            m_old = evaluate_path(h_ref, n_old, path="old")
            m_new = evaluate_path(h_ref, n_new, path="new")
            key = f"{name}@{amp}"
            out[key] = {
                "normal_rmse_old_vs_new": float(np.sqrt(np.mean((n_old - n_new) ** 2))),
                "normal_rmse_old_vs_ideal": float(np.sqrt(np.mean((n_old - n_ideal) ** 2))),
                "normal_rmse_new_vs_ideal": float(np.sqrt(np.mean((n_new - n_ideal) ** 2))),
                "downstream_aligned_old": float(m_old["path_rmse"]),
                "downstream_aligned_new": float(m_new["path_rmse"]),
                "downstream_aligned_ratio": float(m_old["path_rmse"] / m_new["path_rmse"]) if m_new["path_rmse"] > 0 else None,
                "downstream_rawcentered_old": float(m_old["path_raw_centered_rmse"]),
                "downstream_rawcentered_new": float(m_new["path_raw_centered_rmse"]),
                "downstream_rawcentered_ratio": (
                    float(m_old["path_raw_centered_rmse"] / m_new["path_raw_centered_rmse"])
                    if m_new["path_raw_centered_rmse"] > 0
                    else None
                ),
            }
    deltas = [abs(v["downstream_aligned_old"] - v["downstream_aligned_new"]) for v in out.values()]
    normd = [v["normal_rmse_old_vs_new"] for v in out.values()]
    ratios = [v["downstream_aligned_ratio"] for v in out.values() if v["downstream_aligned_ratio"]]
    payload = {
        "probe": "H4_MAGNITUDE",
        "native": NATIVE,
        "target": TARGET,
        "t_delta_rmse": 0.02,
        "cases": out,
        "summary": {
            "max_normal_rmse_old_vs_new": float(max(normd)),
            "max_abs_downstream_aligned_delta": float(max(deltas)),
            "max_downstream_aligned_ratio": float(max(ratios)),
            "n_cases_downstream_delta_ge_t": int(sum(1 for d in deltas if d >= 0.02)),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, allow_nan=False)
    print(f"OK -> {args.out}")
    print(json.dumps(payload["summary"], indent=1))
    print("top-5 by |downstream delta|:")
    for k, v in sorted(out.items(), key=lambda kv: -abs(kv[1]["downstream_aligned_old"] - kv[1]["downstream_aligned_new"]))[:5]:
        print(f"  {k:26s} nRMSE(o,n)={v['normal_rmse_old_vs_new']:.5f} "
              f"down_old={v['downstream_aligned_old']:.5f} down_new={v['downstream_aligned_new']:.5f} "
              f"ratio={v['downstream_aligned_ratio']:.4f}")


if __name__ == "__main__":
    main()
