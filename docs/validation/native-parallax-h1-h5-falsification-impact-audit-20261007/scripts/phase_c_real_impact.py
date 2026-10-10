"""PHASE C — Impacto READ-ONLY sobre corpus real (AUDIT_ONLY).

Sólo se ejecuta porque PHASE A confirmó el mecanismo H1 (oráculo con rejilla finita)
y porque §15 lo permite explícitamente para el diagnóstico de coherencia de M4.

PROHIBIDO (y respetado):
  - no se ejecuta run_exp_m4 / run_exp_m5
  - no se escribe nada bajo data/ ni docs/validation/ históricos
  - no se toca C:\\SkyClawResearch\\**
  - no se cambia ningún umbral

Lo que hace:
  1. prepare_entries() del runner M4 → verifica SHA256 del corpus contra el manifest.
     Un mismatch es HARD STOP (no se continúa).
  2. Por asset: recalcula el oráculo ACTUAL (rejilla) y el oráculo CONTINUO (forma
     cerrada) sobre el MISMO material cargado igual que M4 (512², DIRECTX).
  3. Compara el oráculo de rejilla recalculado contra el histórico (exp-m4-results.json)
     para validar que la reconstrucción es fiel y el corpus no derivó.
  4. Recalcula el diagnóstico de coherencia (§5) con ambos oráculos, reutilizando los
     delta_rmse HISTÓRICOS (sólo lectura) para el Spearman.

Uso:
  PYTHONPATH=<worktree> python phase_c_real_impact.py \
      --corpus-root C:/SkyClawResearch/NativeParallax/EXP-M3 \
      --out <json>
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

from sky_claw.local.native_parallax.research.authored_dataset import load_asset
from sky_claw.local.native_parallax.research.normal_from_height import (
    normals_from_gradients,
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.run_exp_m2 import spearman
from sky_claw.local.native_parallax.research.run_exp_m3 import (
    SOLVER_NORMAL_CONVENTION,
    material_spec_from_entry,
)
from sky_claw.local.native_parallax.research.run_exp_m4 import prepare_entries
from sky_claw.local.native_parallax.research.trust_proxies import OracleOnly

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[4]
    / "docs"
    / "design"
    / "research"
    / "native-parallax"
    / "data"
    / "exp-m3-clean-authored-manifest.json"
)
HISTORICAL_M4 = Path(__file__).resolve().parents[4] / "data" / "exp-m4-results.json"
RESOLUTION = 512


def median_angle_deg(normal: np.ndarray, h_centered: np.ndarray, s: float) -> float:
    """Réplica EXACTA del cuerpo del oráculo para un solo s."""
    h = h_centered * float(s)
    p, q = spectral_gradients(h)
    n2 = normals_from_gradients(p, q, sx=1.0, sy=1.0)
    dot = np.clip(np.sum(normal * n2, axis=-1), -1.0, 1.0)
    return float(np.median(np.degrees(np.arccos(dot))))


def closed_form_strength(normal: np.ndarray, h_centered: np.ndarray, nz_floor: float = 1e-3) -> float:
    """LS global en espacio de gradiente con pesos nz² (semilla del óptimo continuo)."""
    nz = np.maximum(normal[..., 2], nz_floor)
    pa, qa = -normal[..., 0] / nz, -normal[..., 1] / nz
    ph, qh = spectral_gradients(h_centered)
    w = np.clip(normal[..., 2], 0.0, 1.0) ** 2
    den = float(np.sum(w * (ph * ph + qh * qh)))
    if den <= 0.0:
        return 0.0
    return float(np.sum(w * (pa * ph + qa * qh)) / den)


def continuous_oracle(normal: np.ndarray, h_centered: np.ndarray, s0: float) -> tuple[float, float]:
    """Oráculo CONTINUO: minimiza LA MISMA métrica del oráculo (mediana del ángulo).

    La forma cerrada s0 minimiza el LS ponderado en espacio de gradiente, no la mediana
    angular; se refina con un barrido log alrededor de s0 (±2 octavas) más una malla
    lineal fina cerca de cero (donde el corpus real se concentra). Devuelve (s*, ang*).
    """
    if s0 == 0.0:
        return 0.0, float("nan")
    cand = np.concatenate(
        [
            s0 * np.exp2(np.linspace(-2.0, 2.0, 161)),
            np.linspace(-0.05, 0.05, 201),
        ]
    )
    best_s, best_a = float(s0), float("inf")
    for s in cand:
        a = median_angle_deg(normal, h_centered, float(s))
        if a < best_a:
            best_a, best_s = a, float(s)
    return best_s, best_a


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--m3-manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--corpus-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    hist = json.loads(HISTORICAL_M4.read_text(encoding="utf-8"))
    hist_rows = {r["asset"]: r for r in hist["rows"]}
    hist_env = hist.get("environment", {})

    prepared, exclusions = prepare_entries(args.m3_manifest, args.corpus_root)
    # HARD STOP: sin corpus íntegro no se computa impacto real (jamás sobre corpus parcial).
    if exclusions:
        print(f"HARD STOP: {len(exclusions)} exclusiones (corpus no íntegro): {exclusions}")
        sys.exit(3)
    if len(prepared) != len(hist_rows):
        print(f"HARD STOP: prepared={len(prepared)} != histórico={len(hist_rows)}")
        sys.exit(3)

    rows: list[dict[str, Any]] = []
    for entry in prepared:
        spec = material_spec_from_entry(entry)
        mat = load_asset(spec, RESOLUTION, tested_convention=SOLVER_NORMAL_CONVENTION)
        h = mat.height
        hc = h - h.mean()

        # --- oráculo ACTUAL (código real, sin tocar)
        cur = OracleOnly.normal_height_residual_oracle(mat.normal, h)
        grid_deg = float(cur["height_normal_oracle_agreement_deg"])
        grid_best = float(cur["oracle_best_strength"])

        # --- oráculo CONTINUO (forma cerrada + refinamiento) — contrafactual de instrumentación
        s0 = closed_form_strength(mat.normal, hc)
        s_cont, cont_deg = continuous_oracle(mat.normal, hc, s0)

        hrow = hist_rows[spec.asset_id]
        rows.append(
            {
                "asset": spec.asset_id,
                "split": hrow["split"],
                "grid_agreement_deg": grid_deg,
                "grid_best_strength": grid_best,
                "continuous_best_strength": s_cont,
                "continuous_agreement_deg": cont_deg,
                "closed_form_strength": s0,
                "delta_agreement_deg": grid_deg - cont_deg,
                "historical_grid_agreement_deg": float(hrow["coherence_agreement_deg"]),
                "historical_grid_best_strength": float(hrow["coherence_best_strength"]),
                "replica_abs_diff_deg": abs(grid_deg - float(hrow["coherence_agreement_deg"])),
                "delta_rmse_historical": float(hrow["delta_rmse"]),
            }
        )

    # --- diagnóstico de coherencia §5 (mismo cálculo que run_exp_m4.coherence_diagnostic)
    delta = [r["delta_rmse_historical"] for r in rows]
    grid_deg = [r["grid_agreement_deg"] for r in rows]
    cont_deg = [r["continuous_agreement_deg"] for r in rows]

    diag = {
        "grid": {
            "median_agreement_deg": float(np.median(grid_deg)),
            "spearman_deg_vs_delta_rmse": float(spearman(grid_deg, delta)),
        },
        "continuous": {
            "median_agreement_deg": float(np.median(cont_deg)),
            "spearman_deg_vs_delta_rmse": float(spearman(cont_deg, delta)),
        },
        "historical_recorded": hist.get("summary", {}).get("coherence_diagnostic"),
    }
    diag["diagnostic_changed"] = bool(
        abs(diag["grid"]["median_agreement_deg"] - diag["continuous"]["median_agreement_deg"]) > 1e-9
        or abs(
            diag["grid"]["spearman_deg_vs_delta_rmse"] - diag["continuous"]["spearman_deg_vs_delta_rmse"]
        )
        > 1e-9
    )

    grid_best_vals = [r["grid_best_strength"] for r in rows]
    n_at_grid_floor = int(sum(1 for s in grid_best_vals if abs(abs(s) - 0.05) < 1e-9))
    n_cont_below_floor = int(sum(1 for r in rows if abs(r["continuous_best_strength"]) < 0.05))

    payload = {
        "phase": "C_READ_ONLY_REAL_CORPUS_IMPACT",
        "audit_only": True,
        "writes_to_historical_artifacts": False,
        "corpus_root": str(args.corpus_root),
        "resolution": RESOLUTION,
        "historical_m4": {
            "path": str(HISTORICAL_M4.relative_to(HISTORICAL_M4.parents[1])),
            "git_sha": hist_env.get("git_sha"),
            "resolution": hist_env.get("resolution"),
            "manifest_sha256": hist_env.get("manifest_sha256"),
            "decision": hist.get("summary", {}).get("decision"),
            "rules": hist.get("summary", {}).get("rules"),
        },
        "corpus_integrity": {"exclusions": exclusions, "n_prepared": len(prepared)},
        "rows": rows,
        "coherence_diagnostic": diag,
        "grid_oracle_signature": {
            "n_assets": len(rows),
            "n_best_strength_at_grid_floor": n_at_grid_floor,
            "grid_floor": 0.05,
            "n_continuous_strength_below_floor": n_cont_below_floor,
            "max_replica_abs_diff_deg": float(max(r["replica_abs_diff_deg"] for r in rows)),
        },
        "m3_cohort_b_membership": {
            "computable": False,
            "reason": (
                "El corpus EXP-M2 (34 assets, manifest exp-m2-authored-manifest.json) NO está "
                "presente en C:\\SkyClawResearch\\ (sólo existe EXP-M3). Cohort B no es recomputable "
                "sin él. Además Cohort B está etiquetada EVALUATION_DIAGNOSTIC_ONLY y no entra en "
                "la decisión primaria de M3 (decide_exp_m3 usa sólo Cohort A)."
            ),
        },
        "decision_paths": {
            "m4_C1_C2_use_coherence_oracle": False,
            "m4_coherence_diagnostic_is_decisional": False,
            "m4_decision_rule_source": "solver_coherence.evaluate_rules (self_rmse/abs_corr/var/delta_rmse)",
            "m3_primary_decision_uses_oracle": False,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, allow_nan=False)
    print(f"OK -> {args.out}")
    print(f"grid median={diag['grid']['median_agreement_deg']:.4f} deg  "
          f"cont median={diag['continuous']['median_agreement_deg']:.4f} deg  "
          f"changed={diag['diagnostic_changed']}")
    print(f"best_strength pinned at grid floor: {n_at_grid_floor}/{len(rows)}")


if __name__ == "__main__":
    main()
