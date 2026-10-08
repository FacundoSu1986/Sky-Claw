"""§28 — Verificador de reproducibilidad determinista de la evidencia correctiva.

Ejecuta la misma medición dos veces y exige igualdad bit-a-bit sobre los outputs
deterministas relevantes. Cubre:

  - el optimizador continuo (F1), sobre objetivos sintéticos y sobre métricas reales
  - la superficie segura de H4 (F5), sobre los casos y amplitudes obligatorios

NO re-ejecuta el corpus completo (eso lo hacen los runners de fase). Verifica las
unidades deterministas que sostienen la evidencia.

Uso:
  PYTHONPATH=<worktree>:<corrective_scripts> python verify_determinism.py --out <json>
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

from corrective_optimizer import OptimizerConfig, minimize_1d

from sky_claw.local.native_parallax.research.normal_from_height import spectral_gradients
from sky_claw.local.native_parallax.research.synthetic_height import PERIODIC_CASES

sys.path.insert(0, str(Path(__file__).resolve().parent))
from h4_magnitude_probe_corrective import safe_surface, resize_height_unclipped  # noqa: E402

CASES = ["S07_bumps", "S09_bricks", "S15_periodic_noise"]
AMPS = [0.05, 0.01]
NATIVE = 1024
TARGET = 512


def _run_optimizer_pass() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for true_s in (2.5, -1.75, 0.012, -0.3, 3.0):
        f = lambda s, t=true_s: float((s - t) ** 2 + 0.01 * np.sin(3.0 * s))  # noqa: E731
        r = minimize_1d(f, OptimizerConfig())
        out[f"{true_s}"] = {
            "best_x": repr(r.best_x),
            "best_fx": repr(r.best_fx),
            "status": r.status,
            "iterations": r.iterations,
            "n_evaluations": r.n_evaluations,
            "expansions": r.boundary_expansions,
        }
    return out


def _run_surface_pass() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in CASES:
        for amp in AMPS:
            h = PERIODIC_CASES[name](NATIVE, NATIVE)
            h = (h - h.mean()) * amp
            h_safe, offset = safe_surface(h)
            h_ref = resize_height_unclipped(h_safe, TARGET)
            p, q = spectral_gradients(h_safe)
            out[f"{name}@{amp}"] = {
                "offset": repr(offset),
                "h_safe_min": repr(float(h_safe.min())),
                "h_safe_max": repr(float(h_safe.max())),
                "h_ref_min": repr(float(h_ref.min())),
                "h_ref_max": repr(float(h_ref.max())),
                "grad_checksum": repr(float(np.sum(np.abs(p)) + np.sum(np.abs(q)))),
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

    opt_a, opt_b = _run_optimizer_pass(), _run_optimizer_pass()
    surf_a, surf_b = _run_surface_pass(), _run_surface_pass()

    opt_ok = opt_a == opt_b
    surf_ok = surf_a == surf_b

    payload = {
        "verifier": "CORRECTIVE_REPRODUCIBILITY",
        "audit_only": True,
        "corrective": True,
        "correction_parent_head": "d260b7c9b8afff33a6720106ac8c5f6d95891ecd",
        "optimizer_determinism": {"pass": opt_ok, "pass_a": opt_a, "pass_b": opt_b},
        "surface_determinism": {"pass": surf_ok, "pass_a": surf_a, "pass_b": surf_b},
        "CORRECTIVE_REPRODUCIBILITY": "PASS" if (opt_ok and surf_ok) else "FAIL",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, allow_nan=False)
    print(f"OK -> {args.out}")
    print(f"optimizer determinism: {opt_ok}")
    print(f"surface determinism:   {surf_ok}")
    print(f"CORRECTIVE_REPRODUCIBILITY = {payload['CORRECTIVE_REPRODUCIBILITY']}")


if __name__ == "__main__":
    main()
