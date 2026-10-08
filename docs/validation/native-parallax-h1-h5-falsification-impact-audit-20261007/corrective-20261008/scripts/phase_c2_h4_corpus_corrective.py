"""PHASE C2 — Contrafactual READ-ONLY de resize_normal sobre el corpus real (H4).

Cambia EXACTAMENTE UNA variable: el método de resize del normal AUTH
(uint8+truncado de ``authored_dataset.resize_normal`` → bilineal float + renormalización).
Todo lo demás (height, solver, umbrales, split, convención, alineamiento global,
resolución, reglas de decisión) queda idéntico.

PROHIBIDO (respetado): no se ejecuta run_exp_m4/run_exp_m5, no se escribe bajo data/ ni
docs/validation históricos, no se toca C:\\SkyClawResearch\\**, no se cambian umbrales.

Uso:
  PYTHONPATH=<worktree> python phase_c2_h4_corpus.py \
      --corpus-root C:/SkyClawResearch/NativeParallax/EXP-M3 --out <json>
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

from sky_claw.local.native_parallax.research.authored_dataset import (
    apply_convention,
    decode_height_image,
    decode_normal_image,
    resize_height,
    resize_normal,
)
from sky_claw.local.native_parallax.research.frequency_coherence import (
    T_HIGH_ENRICHMENT,
    T_LOWMID_EXCESS,
    T_LOWMID_NRMSE,
    asset_summary,
    band_masks,
    cohort_medians,
)
from sky_claw.local.native_parallax.research.run_exp_m2 import split_of
from sky_claw.local.native_parallax.research.run_exp_m3 import (
    SOLVER_NORMAL_CONVENTION,
    material_spec_from_entry,
)
from sky_claw.local.native_parallax.research.run_exp_m4 import prepare_entries
from sky_claw.local.native_parallax.research.run_exp_m5 import _aligned_reconstruction
from sky_claw.local.native_parallax.research.solver_coherence import (
    T_DELTA_CORR,
    T_DELTA_RMSE,
    T_SELF_CORR,
    T_SELF_RMSE,
    T_SELF_VAR,
    decide,
    delta_stats,
    evaluate_path,
    evaluate_rules,
    self_forward,
)

RESOLUTION = 512
# scripts/ -> corrective-20261008/ -> <audit>/ -> validation/ -> docs/ -> <repo>
DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[5]
    / "docs" / "design" / "research" / "native-parallax" / "data"
    / "exp-m3-clean-authored-manifest.json"
)


def resize_normal_float(n: np.ndarray, size: int) -> np.ndarray:
    """FLOAT_RESIZE_NORMAL_COUNTERFACTUAL (audit-only, NO versionado)."""
    if n.shape[0] == size and n.shape[1] == size:
        return np.asarray(n, dtype=np.float64)
    ch = []
    for c in range(3):
        im = Image.fromarray(np.asarray(np.clip(n[..., c], -1.0, 1.0), dtype=np.float32))
        ch.append(np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64))
    out = np.stack(ch, axis=-1)
    return np.asarray(out / np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-12), dtype=np.float64)


def build_asset(spec, counterfactual: bool) -> tuple[np.ndarray, np.ndarray]:
    """Réplica EXACTA de load_asset() cambiando SÓLO el resize del normal."""
    normal_native = decode_normal_image(Path(spec.normal_path))
    if spec.declared_convention in ("OPENGL", "DIRECTX") and SOLVER_NORMAL_CONVENTION != spec.declared_convention:
        normal_native = apply_convention(normal_native, spec.declared_convention, SOLVER_NORMAL_CONVENTION)
    height = resize_height(decode_height_image(Path(spec.height_path)), RESOLUTION)
    resizer = resize_normal_float if counterfactual else resize_normal
    return resizer(normal_native, RESOLUTION), height


def m4_row(entry: dict[str, Any]) -> dict[str, Any]:
    spec = material_spec_from_entry(entry)
    n_old, h = build_asset(spec, counterfactual=False)
    n_new, _ = build_asset(spec, counterfactual=True)
    self_q8 = evaluate_path(h, self_forward(h, bits=8), path="self_q8")
    old = evaluate_path(h, n_old, path="auth_old")
    new = evaluate_path(h, n_new, path="auth_new")
    return {
        "asset": spec.asset_id,
        "family": spec.family,
        "split": split_of(spec.family),
        "self_rmse": self_q8["path_rmse"],
        "self_abs_corr": self_q8["path_abs_corr"],
        "self_var": self_q8["path_variance_ratio"],
        "auth_rmse_old": old["path_rmse"],
        "auth_rmse_new": new["path_rmse"],
        "auth_abs_corr_old": old["path_abs_corr"],
        "auth_abs_corr_new": new["path_abs_corr"],
        "auth_var_old": old["path_variance_ratio"],
        "auth_var_new": new["path_variance_ratio"],
        "delta_rmse_old": old["path_rmse"] - self_q8["path_rmse"],
        "delta_rmse_new": new["path_rmse"] - self_q8["path_rmse"],
        "normal_rmse_old_vs_new": float(np.sqrt(np.mean((n_old - n_new) ** 2))),
        "mean_nx_old": float(np.mean(n_old[..., 0])),
        "mean_nx_new": float(np.mean(n_new[..., 0])),
    }


def m5_row(entry: dict[str, Any]) -> dict[str, Any]:
    spec = material_spec_from_entry(entry)
    n_old, h = build_asset(spec, counterfactual=False)
    n_new, _ = build_asset(spec, counterfactual=True)
    r_self, _ = _aligned_reconstruction(h, self_forward(h, bits=8))
    r_old, _ = _aligned_reconstruction(h, n_old)
    r_new, _ = _aligned_reconstruction(h, n_new)
    masks = band_masks(h.shape[0], h.shape[1])
    s_old = asset_summary(h, r_self, r_old, masks)
    s_new = asset_summary(h, r_self, r_new, masks)
    return {"asset": spec.asset_id, "family": spec.family, "old": s_old, "new": s_new}


def rules_from(rows: list[dict[str, Any]], key_self: str, key_auth: str, key_corr: str, key_var: str) -> dict[str, Any]:
    full = {
        "rmse_self_median": float(np.median([r[key_self] for r in rows])),
        "rmse_auth_median": float(np.median([r[key_auth] for r in rows])),
        "delta_rmse_median": float(np.median([r["delta_rmse_old" if key_auth.endswith("old") else "delta_rmse_new"] for r in rows])),
        "abs_corr_self_median": float(np.median([r["self_abs_corr"] for r in rows])),
        "abs_corr_auth_median": float(np.median([r[key_corr] for r in rows])),
        "var_self_median": float(np.median([r["self_var"] for r in rows])),
        "var_auth_median": float(np.median([r[key_var] for r in rows])),
    }
    return full


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

    prepared, exclusions = prepare_entries(args.m3_manifest, args.corpus_root)
    if exclusions:
        print(f"HARD STOP: corpus no íntegro: {exclusions}")
        sys.exit(3)
    # Fix del thread PRRT_kwDOR1JjU86qZ5qt: `prepare_entries` acepta cualquier manifest de
    # Cohort A con >=15 assets de 3 familias. El contrato del audit exige el corpus
    # HISTÓRICO de 31: sin este guard, un manifest de 15 assets sin exclusiones produciría
    # decisiones M4/M5 etiquetadas como contrafactual del corpus.
    if len(prepared) != 31:
        print(f"HARD STOP: prepared={len(prepared)} != histórico=31")
        sys.exit(3)

    rows = [m4_row(e) for e in prepared]

    # ---------------- M4: reglas primarias con AUTH old vs new (mismos self y umbrales)
    def build_full(key_auth: str, key_corr: str, key_var: str, key_delta: str) -> dict[str, float]:
        return {
            "rmse_self_median": float(np.median([r["self_rmse"] for r in rows])),
            "rmse_auth_median": float(np.median([r[key_auth] for r in rows])),
            "delta_rmse_median": float(np.median([r[key_delta] for r in rows])),
            "abs_corr_self_median": float(np.median([r["self_abs_corr"] for r in rows])),
            "abs_corr_auth_median": float(np.median([r[key_corr] for r in rows])),
            "var_self_median": float(np.median([r["self_var"] for r in rows])),
            "var_auth_median": float(np.median([r[key_var] for r in rows])),
        }

    full_old = build_full("auth_rmse_old", "auth_abs_corr_old", "auth_var_old", "delta_rmse_old")
    full_new = build_full("auth_rmse_new", "auth_abs_corr_new", "auth_var_new", "delta_rmse_new")

    # held-out: mismos assets, sólo cambia el AUTH
    def build_sub(split: str, full: dict[str, float], key_auth: str, key_corr: str, key_var: str, key_delta: str) -> dict[str, float]:
        sub = [r for r in rows if r["split"] == split]
        return {
            "rmse_self_median": float(np.median([r["self_rmse"] for r in sub])),
            "rmse_auth_median": float(np.median([r[key_auth] for r in sub])),
            "delta_rmse_median": float(np.median([r[key_delta] for r in sub])),
            "abs_corr_self_median": float(np.median([r["self_abs_corr"] for r in sub])),
            "abs_corr_auth_median": float(np.median([r[key_corr] for r in sub])),
            "var_self_median": float(np.median([r["self_var"] for r in sub])),
            "var_auth_median": float(np.median([r[key_var] for r in sub])),
        }

    held_old = build_sub("HELD_OUT", full_old, "auth_rmse_old", "auth_abs_corr_old", "auth_var_old", "delta_rmse_old")
    held_new = build_sub("HELD_OUT", full_new, "auth_rmse_new", "auth_abs_corr_new", "auth_var_new", "delta_rmse_new")

    rules_old = evaluate_rules(full_old, held_old)
    rules_new = evaluate_rules(full_new, held_new)
    dec_old = decide(rules_old["C1_self_good"], rules_old["C2_auth_worse"])
    dec_new = decide(rules_new["C1_self_good"], rules_new["C2_auth_worse"])

    # ---------------- M5: medianas por banda con AUTH old vs new
    m5 = [m5_row(e) for e in prepared]
    med_old = cohort_medians([m["old"] for m in m5])
    med_new = cohort_medians([m["new"] for m in m5])

    def c1(med: dict[str, float]) -> bool:
        return bool(med["auth_lowmid_nrmse"] <= T_LOWMID_NRMSE and med["excess_lowmid_nrmse"] <= T_LOWMID_EXCESS)

    def c2_components(med: dict[str, float]) -> dict[str, bool]:
        return {
            "high_gt_lowmid_excess": bool(med["excess_high_nrmse"] > med["excess_lowmid_nrmse"]),
            "high_enrichment_ge_threshold": bool(med["high_enrichment"] >= T_HIGH_ENRICHMENT),
        }

    payload = {
        "phase": "C2_H4_RESIZE_NORMAL_CORPUS_COUNTERFACTUAL",
        "audit_only": True,
        "single_variable": "resize_normal (uint8 requantize -> float bilinear + renormalize)",
        "corpus_root": str(args.corpus_root),
        "resolution": RESOLUTION,
        "corpus_integrity": {"exclusions": exclusions, "n_prepared": len(prepared)},
        "thresholds_unchanged": {
            "T_SELF_RMSE": T_SELF_RMSE, "T_SELF_CORR": T_SELF_CORR, "T_SELF_VAR": T_SELF_VAR,
            "T_DELTA_RMSE": T_DELTA_RMSE, "T_DELTA_CORR": T_DELTA_CORR,
            "T_LOWMID_NRMSE": T_LOWMID_NRMSE, "T_LOWMID_EXCESS": T_LOWMID_EXCESS,
            "T_HIGH_ENRICHMENT": T_HIGH_ENRICHMENT,
        },
        "m4": {
            "rows": rows,
            "full_old": full_old, "full_new": full_new,
            "heldout_old": held_old, "heldout_new": held_new,
            "rules_old": rules_old, "rules_new": rules_new,
            "decision_old": dec_old, "decision_new": dec_new,
            "decision_changed": bool(dec_old != dec_new),
            "delta_rmse_max_abs_change": float(max(abs(r["delta_rmse_new"] - r["delta_rmse_old"]) for r in rows)),
            "auth_rmse_max_abs_change": float(max(abs(r["auth_rmse_new"] - r["auth_rmse_old"]) for r in rows)),
            "normal_rmse_old_vs_new_max": float(max(r["normal_rmse_old_vs_new"] for r in rows)),
        },
        "m5": {
            "medians_old": med_old, "medians_new": med_new,
            "C1_old": c1(med_old), "C1_new": c1(med_new),
            "C2_components_old": c2_components(med_old),
            "C2_components_new": c2_components(med_new),
            "auth_lowmid_max_abs_change": abs(med_new["auth_lowmid_nrmse"] - med_old["auth_lowmid_nrmse"]),
            "excess_lowmid_max_abs_change": abs(med_new["excess_lowmid_nrmse"] - med_old["excess_lowmid_nrmse"]),
            "excess_high_max_abs_change": abs(med_new["excess_high_nrmse"] - med_old["excess_high_nrmse"]),
            "high_enrichment_max_abs_change": abs(med_new["high_enrichment"] - med_old["high_enrichment"]),
            "note": (
                "C2 de M5 exige además la réplica direccional en la cohorte LEGACY_HELDOUT, que "
                "no es reproducible en este slice READ-ONLY (no hay artefacto de filas M5 en el "
                "repo). Se reportan C1 y los dos componentes de C2 evaluables."
            ),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, allow_nan=False)
    print(f"OK -> {args.out}")
    print(f"M4 decision: {dec_old} -> {dec_new}  changed={payload['m4']['decision_changed']}")
    print(f"M4 delta_rmse median: {full_old['delta_rmse_median']:.5f} -> {full_new['delta_rmse_median']:.5f} "
          f"(max |change| per asset {payload['m4']['delta_rmse_max_abs_change']:.5f})")
    print(f"M5 C1: {payload['m5']['C1_old']} -> {payload['m5']['C1_new']}")
    print(f"M5 excess_lowmid: {med_old['excess_lowmid_nrmse']:.5f} -> {med_new['excess_lowmid_nrmse']:.5f} | "
          f"excess_high: {med_old['excess_high_nrmse']:.5f} -> {med_new['excess_high_nrmse']:.5f}")


if __name__ == "__main__":
    main()
