"""F1 + F3 — Fase C correctiva: oráculo continuo REAL sobre el corpus (READ-ONLY).

Corrige los tres defectos metodológicos de H1:

  F1  El supuesto "continuous" de `phase_c_real_impact.py` era un barrido finito
      (`s0*exp2(linspace(-2,2,161)) ∪ linspace(-0.05,0.05,201)`) sin bracketing ni
      convergencia, y `polyhaven_gray_rocks` caía en el borde `0.05` con cero mejora.
      => Ahora se minimiza con `corrective_optimizer.minimize_1d` (barrido grueso
      log-uniforme + bracketing + sección áurea + expansión de borde + metadatos).

  F3  El Spearman de la auditoría original mezclaba ángulos recalculados en el baseline
      con `delta_rmse` copiado del artefacto histórico no-ancestro => "dataset híbrido".
      => Ahora se emiten DOS universos explícitos y separados:
           BASELINE_COUNTERFACTUAL: ángulos y delta_rmse recalculados en el baseline.
           HISTORICAL_COMPARISON:   ángulos del baseline vs delta_rmse histórico,
                                    etiquetado explícitamente como histórico.
      Si el delta del baseline no puede recomputarse se emite `UNAVAILABLE`, no una
      correlación híbrida.

También emite los conteos reales que §16 exige antes de volver a afirmar nada sobre la
rejilla: `n_grid_matches_continuous_within_tolerance`, `n_continuous_abs_strength_lt_0_05`,
`n_boundary_cases`, `n_converged`, `n_unresolved`.

PROHIBIDO (y respetado): no ejecuta run_exp_m4/run_exp_m5, no escribe bajo data/ ni en el
directorio de la auditoría original, no toca C:\\SkyClawResearch\\**, no cambia umbrales,
no modifica código científico.

Uso:
  PYTHONPATH=<worktree>:<corrective_scripts> python phase_c_corrective.py \
      --corpus-root C:/SkyClawResearch/NativeParallax/EXP-M3 --out <json>
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from corrective_optimizer import OptimizerConfig, minimize_1d

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
from sky_claw.local.native_parallax.research.solver_coherence import (
    evaluate_path,
    self_forward,
)
from sky_claw.local.native_parallax.research.trust_proxies import OracleOnly

RESOLUTION = 512
# scripts/ -> corrective-20261008/ -> <audit>/ -> validation/ -> docs/ -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[5]
HISTORICAL_M4 = REPO_ROOT / "data" / "exp-m4-results.json"
DEFAULT_MANIFEST = (
    REPO_ROOT / "docs" / "design" / "research" / "native-parallax" / "data" / "exp-m3-clean-authored-manifest.json"
)

# Tolerancia para declarar que la rejilla "matchea" el continuo (en grados).
GRID_MATCH_TOLERANCE_DEG = 1e-6
# Tolerancia para el conteo |s*| < 0.05 (piso histórico del oráculo).
FLOOR_STRENGTH = 0.05


def median_angle_deg(normal: np.ndarray, h_centered: np.ndarray, s: float) -> float:
    """Réplica EXACTA del cuerpo del oráculo para un solo s (idéntica a la auditoría org.)."""
    h = h_centered * float(s)
    p, q = spectral_gradients(h)
    n2 = normals_from_gradients(p, q, sx=1.0, sy=1.0)
    dot = np.clip(np.sum(normal * n2, axis=-1), -1.0, 1.0)
    return float(np.median(np.degrees(np.arccos(dot))))


def closed_form_strength(normal: np.ndarray, h_centered: np.ndarray, nz_floor: float = 1e-3) -> float:
    """LS global en espacio de gradiente con pesos nz² (semilla del bracket continuo)."""
    nz = np.maximum(normal[..., 2], nz_floor)
    pa, qa = -normal[..., 0] / nz, -normal[..., 1] / nz
    ph, qh = spectral_gradients(h_centered)
    w = np.clip(normal[..., 2], 0.0, 1.0) ** 2
    den = float(np.sum(w * (ph * ph + qh * qh)))
    if den <= 0.0:
        return 0.0
    return float(np.sum(w * (pa * ph + qa * qh)) / den)


def continuous_oracle_real(
    normal: np.ndarray,
    h_centered: np.ndarray,
    cfg: OptimizerConfig,
) -> tuple[float, float, dict[str, Any]]:
    """Minimización REAL de la mediana angular (F1).

    Devuelve `(s*, ang*, metadata)`. La métrica objetivo es la MISMA que la del oráculo
    (mediana del ángulo en grados) — no una proxy distinta.
    """
    objective = lambda s: median_angle_deg(normal, h_centered, s)  # noqa: E731
    # Bracket inicial anclado en la forma cerrada (semilla), expandible por el optimizador.
    s0 = closed_form_strength(normal, h_centered)
    initial = None
    if s0 != 0.0:
        initial = (min(-abs(s0) * 2.0, s0 * 0.5), max(abs(s0) * 2.0, -s0 * 0.5))
    res = minimize_1d(objective, cfg, initial_bracket=initial)
    return res.best_x, res.best_fx, res.as_dict()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:  # noqa: C901
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--m3-manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--corpus-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    cfg = OptimizerConfig()
    hist = json.loads(HISTORICAL_M4.read_text(encoding="utf-8"))
    hist_rows = {r["asset"]: r for r in hist["rows"]}
    hist_env = hist.get("environment", {})

    prepared, exclusions = prepare_entries(args.m3_manifest, args.corpus_root)
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

        # --- oráculo ACTUAL (rejilla, código real sin tocar)
        cur = OracleOnly.normal_height_residual_oracle(mat.normal, h)
        grid_deg = float(cur["height_normal_oracle_agreement_deg"])
        grid_best = float(cur["oracle_best_strength"])

        # --- oráculo CONTINUO REAL (F1)
        s0 = closed_form_strength(mat.normal, hc)
        s_cont, cont_deg, meta = continuous_oracle_real(mat.normal, hc, cfg)

        # --- delta_rmse RECOMPUTADO en el baseline (F3: universo único)
        # El AUTH del baseline es el normal del asset tal como lo carga load_asset.
        self_q8 = evaluate_path(h, self_forward(h, bits=8), path="self_q8")
        auth = evaluate_path(h, mat.normal, path="auth")
        delta_rmse_baseline = float(auth["path_rmse"] - self_q8["path_rmse"])
        delta_rmse_historical = float(hist_rows[spec.asset_id]["delta_rmse"])

        rows.append(
            {
                "asset": spec.asset_id,
                "split": hist_rows[spec.asset_id]["split"],
                "grid_agreement_deg": grid_deg,
                "grid_best_strength": grid_best,
                "continuous_best_strength": s_cont,
                "continuous_agreement_deg": cont_deg,
                "closed_form_strength": s0,
                "delta_agreement_deg": grid_deg - cont_deg,
                "delta_rmse_baseline": delta_rmse_baseline,
                "delta_rmse_historical": delta_rmse_historical,
                "historical_grid_agreement_deg": float(hist_rows[spec.asset_id]["coherence_agreement_deg"]),
                "historical_grid_best_strength": float(hist_rows[spec.asset_id]["coherence_best_strength"]),
                "replica_abs_diff_deg": abs(grid_deg - float(hist_rows[spec.asset_id]["coherence_agreement_deg"])),
                "continuous_meta": meta,
            }
        )

    # ---------------- §16 conteos reales
    grid_deg = [r["grid_agreement_deg"] for r in rows]
    cont_deg = [r["continuous_agreement_deg"] for r in rows]
    n_converged = int(sum(1 for r in rows if r["continuous_meta"]["converged"]))
    n_unresolved = int(len(rows) - n_converged)
    n_boundary = int(sum(1 for r in rows if r["continuous_meta"]["boundary_hit"]))
    n_grid_matches = int(
        sum(1 for r in rows if abs(r["grid_agreement_deg"] - r["continuous_agreement_deg"]) <= GRID_MATCH_TOLERANCE_DEG)
    )
    n_below_floor = int(sum(1 for r in rows if abs(r["continuous_best_strength"]) < FLOOR_STRENGTH))
    n_grid_at_floor = int(
        sum(1 for s in (r["grid_best_strength"] for r in rows) if abs(abs(s) - FLOOR_STRENGTH) < 1e-9)
    )

    # ---------------- F3: dos universos explícitos
    delta_baseline = [r["delta_rmse_baseline"] for r in rows]
    delta_historical = [r["delta_rmse_historical"] for r in rows]

    def _sp(a: list[float], b: list[float]) -> float:
        try:
            return float(spearman(a, b))
        except ValueError:
            return float("nan")

    universes = {
        "BASELINE_COUNTERFACTUAL": {
            "label": "ángulos (rejilla/continuo) y delta_rmse AMBOS recalculados en el baseline",
            "delta_rmse_source": "recomputed_on_baseline_path",
            "grid": {
                "median_agreement_deg": float(np.median(grid_deg)),
                "spearman_deg_vs_delta_rmse": _sp(grid_deg, delta_baseline),
            },
            "continuous": {
                "median_agreement_deg": float(np.median(cont_deg)),
                "spearman_deg_vs_delta_rmse": _sp(cont_deg, delta_baseline),
            },
        },
        "HISTORICAL_COMPARISON": {
            "label": "ángulos del baseline vs delta_rmse del artefacto histórico fe54e9a9 (NO ancestro)",
            "delta_rmse_source": "data/exp-m4-results.json@fe54e9a9",
            "is_hybrid": True,
            "grid": {
                "median_agreement_deg": float(np.median(grid_deg)),
                "spearman_deg_vs_delta_rmse": _sp(grid_deg, delta_historical),
            },
            "continuous": {
                "median_agreement_deg": float(np.median(cont_deg)),
                "spearman_deg_vs_delta_rmse": _sp(cont_deg, delta_historical),
            },
        },
    }
    if not np.isfinite(universes["BASELINE_COUNTERFACTUAL"]["grid"]["spearman_deg_vs_delta_rmse"]):
        universes["BASELINE_COUNTERFACTUAL"]["spearman_deg_vs_delta_rmse"] = "UNAVAILABLE"

    payload = {
        "phase": "C_CORRECTIVE_H1_CONTINUOUS_OPTIMIZATION",
        "audit_only": True,
        "corrective": True,
        "correction_parent_head": "d260b7c9b8afff33a6720106ac8c5f6d95891ecd",
        "writes_to_historical_artifacts": False,
        "corpus_root": str(args.corpus_root),
        "resolution": RESOLUTION,
        "optimizer_config": cfg.as_dict(),
        "historical_m4": {
            "path": "data/exp-m4-results.json",
            "git_sha": hist_env.get("git_sha"),
            "manifest_sha256": hist_env.get("manifest_sha256"),
            "decision": hist.get("summary", {}).get("decision"),
        },
        "corpus_integrity": {"exclusions": exclusions, "n_prepared": len(prepared)},
        "rows": rows,
        "universes": universes,
        "counts_16": {
            "n_grid_matches_continuous_within_tolerance": n_grid_matches,
            "grid_match_tolerance_deg": GRID_MATCH_TOLERANCE_DEG,
            "n_continuous_abs_strength_lt_0_05": n_below_floor,
            "n_grid_best_strength_at_floor": n_grid_at_floor,
            "n_boundary_cases": n_boundary,
            "n_converged": n_converged,
            "n_unresolved": n_unresolved,
        },
        "grid_oracle_signature": {
            "n_assets": len(rows),
            "n_best_strength_at_grid_floor": n_grid_at_floor,
            "grid_floor": FLOOR_STRENGTH,
            "max_replica_abs_diff_deg": float(max(r["replica_abs_diff_deg"] for r in rows)),
        },
        "inputs_sha256": {
            "manifest": sha256_file(args.m3_manifest),
            "historical_m4": sha256_file(HISTORICAL_M4),
        },
        "decision_paths": {
            "m4_C1_C2_use_coherence_oracle": False,
            "m4_coherence_diagnostic_is_decisional": False,
            "m3_primary_decision_uses_oracle": False,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, allow_nan=False)

    print(f"OK -> {args.out}")
    print(
        f"grid median={np.median(grid_deg):.4f}°  cont median={np.median(cont_deg):.4f}°  "
        f"converged={n_converged}/{len(rows)}  unresolved={n_unresolved}"
    )
    print(
        f"grid matches continuous (<= {GRID_MATCH_TOLERANCE_DEG}°) = {n_grid_matches}   "
        f"|s*|<0.05 = {n_below_floor}   boundary = {n_boundary}   grid@floor = {n_grid_at_floor}"
    )
    ub = universes["BASELINE_COUNTERFACTUAL"]
    print(
        f"SPEARMAN baseline: grid={ub['grid']['spearman_deg_vs_delta_rmse']:.4f}  "
        f"cont={ub['continuous']['spearman_deg_vs_delta_rmse']:.4f}"
    )


if __name__ == "__main__":
    main()
