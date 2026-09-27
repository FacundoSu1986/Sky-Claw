"""EXP-M5 — localización espectral del mismatch authored (research-only).

REUSA la matemática M4 sin duplicarla (§36): ``prepare_entries`` (carga+SHA+exclusiones),
``self_forward``/``solve_normal`` (forward matched + solver RAW), ``OracleOnly.fit_global_scale``
(alineamiento global único por asset) y ``bootstrap_median_ci``. La descomposición por bandas
vive en ``frequency_coherence`` (puro, testeable). Este runner NUNCA fitea por banda ni filtra
el normal antes del solver (prereg §5/§7).

Uso (mismo protocolo freeze que M4):
  # calibración (15) = smoke de implementación, SIN decisión:
  python -m ...run_exp_m5 --corpus-root <root> --phase calibration --out data/exp-m5-calibration.json
  # freeze commit -> SHA
  # full (31) EXIGE --frozen-ack:
  python -m ...run_exp_m5 --corpus-root <root> --phase full --frozen-ack freeze-<sha> --out data/exp-m5-results.json

Si el corpus no está o un SHA256 difiere del manifest => exclusión objetiva; bajo el mínimo §3
=> EXP_M5_DATA_INSUFFICIENT (jamás inventar resultados).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

from sky_claw.local.native_parallax.research.authored_dataset import load_asset
from sky_claw.local.native_parallax.research.frequency_coherence import (
    BAND_EDGES,
    BAND_NAMES,
    ENERGY_GATE_FRACTION,
    LOWMID_HIGH_CUTOFF,
    M5_BOOTSTRAP_SEED,
    M5_THRESHOLDS,
    asset_summary,
    band_masks,
    cohort_medians,
    decide,
    evaluate_rules,
)
from sky_claw.local.native_parallax.research.run_exp_m2 import split_of
from sky_claw.local.native_parallax.research.run_exp_m3 import (
    SOLVER_NORMAL_CONVENTION,
    material_spec_from_entry,
)
from sky_claw.local.native_parallax.research.run_exp_m4 import (
    environment_block as _m4_environment_block,
)
from sky_claw.local.native_parallax.research.run_exp_m4 import (
    prepare_entries,
    sha256_file,
)
from sky_claw.local.native_parallax.research.solver_coherence import (
    bootstrap_median_ci,
    self_forward,
    solve_normal,
)
from sky_claw.local.native_parallax.research.trust_proxies import OracleOnly

RESOLUTION = 512  # primary §9 (continuidad M4)


def _json_safe(obj: Any) -> Any:
    """NaN/Inf -> None (JSON null). Corresponde a bandas BAND_NOT_EVALUABLE (eligible=0);
    ``allow_nan=False`` exige no emitir NaN/Inf. Un NaN en banda ELEGIBLE se surfacea como
    NaN en las medianas de ``cohort_medians``/``evaluate_rules`` (fail-fast aguas arriba)."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _json_safe(obj.tolist())
    return obj


def _aligned_reconstruction(height: np.ndarray, normal: np.ndarray) -> tuple[np.ndarray, float]:
    """Solver RAW M4 + UNA escala global (con signo) contra el height authored (§5 del prereg)."""
    rec, _stats = solve_normal(normal)
    scale = float(OracleOnly.fit_global_scale(height, rec)["affine_scale"])
    return rec * scale, scale


def run_asset_m5(entry: dict[str, Any], resolution: int) -> dict[str, Any]:
    """SELF y AUTH globalmente alineados => resumen espectral por banda (§4/§5)."""
    spec = material_spec_from_entry(entry)
    mat = load_asset(spec, resolution, tested_convention=SOLVER_NORMAL_CONVENTION)
    h = np.asarray(mat.height, dtype=np.float64)
    r_self, scale_self = _aligned_reconstruction(h, self_forward(h, bits=8))
    r_auth, scale_auth = _aligned_reconstruction(h, mat.normal)
    masks = band_masks(h.shape[0], h.shape[1])
    summary = asset_summary(h, r_self, r_auth, masks)
    summary.update(
        {
            "asset": spec.asset_id,
            "family": spec.family,
            "split": split_of(spec.family),
            "provider": entry["provider"],
            "affine_scale_self": scale_self,
            "affine_scale_auth": scale_auth,
        }
    )
    return summary


def environment_block(manifest_path: Path, resolution: int, phase: str, frozen_ack: str | None) -> dict[str, Any]:
    """Reproducibilidad §16 (hereda el bloque M4 y añade los campos espectrales de M5).

    ``resolution`` es la resolución REAL de la corrida (``args.resolution``): primaria 512 o
    secundaria 1024 nativa/no-resize. Se registra tal cual para que el bloque de
    reproducibilidad NO etiquete falsamente una corrida 1024 como 512.
    """
    base = _m4_environment_block(manifest_path, resolution, phase, frozen_ack)
    here = Path(__file__).with_name("frequency_coherence.py")
    base.update(
        {
            "experiment": "EXP-M5",
            "frequency_module_sha256": sha256_file(here) if here.is_file() else "unknown",
            "lowmid_high_cutoff": LOWMID_HIGH_CUTOFF,
            "band_edges": list(BAND_EDGES),
            "band_names": list(BAND_NAMES),
            "energy_gate_fraction": ENERGY_GATE_FRACTION,
            "thresholds": dict(M5_THRESHOLDS),
            "bootstrap_seed": M5_BOOTSTRAP_SEED,
            "fft_backend": "numpy.fft",
        }
    )
    return base


def _cohort_block(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    med = cohort_medians(rows)
    med["n_assets"] = len(rows)
    return med


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="EXP-M5 — frequency-band coherence (research-only)")
    parser.add_argument(
        "--m3-manifest",
        type=Path,
        default=Path(__file__).resolve().parents[4]
        / "docs"
        / "design"
        / "research"
        / "native-parallax"
        / "data"
        / "exp-m3-clean-authored-manifest.json",
    )
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resolution", type=int, default=RESOLUTION)
    parser.add_argument("--phase", choices=("calibration", "full"), required=True)
    parser.add_argument("--frozen-ack", type=str, default=None, help="obligatorio con --phase full")
    args = parser.parse_args()
    if args.phase == "full" and not args.frozen_ack:
        parser.error("--phase full exige --frozen-ack (protocolo freeze §15)")

    prepared, exclusions = prepare_entries(args.m3_manifest, args.corpus_root)
    if args.phase == "calibration":
        prepared = [e for e in prepared if split_of(str(e["family"])) == "CALIBRATION"]
    if not prepared:
        data_required = {
            "experiment": "EXP-M5",
            "phase": args.phase,
            "state": "EXP_M5_DATA_REQUIRED",
            "decision": "EXP_M5_DATA_INSUFFICIENT",
            "environment": environment_block(args.m3_manifest, args.resolution, args.phase, args.frozen_ack),
            "exclusions": exclusions,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as fh:
            json.dump(_json_safe(data_required), fh, indent=1, allow_nan=False)
        print(json.dumps({"decision": "EXP_M5_DATA_INSUFFICIENT", "n_exclusions": len(exclusions)}, indent=2))
        print(f"JSON -> {args.out}")
        sys.exit(2)

    rows = [run_asset_m5(entry, args.resolution) for entry in prepared]
    for i, r in enumerate(rows, 1):
        print(
            f"[{i}/{len(rows)}] {r['asset']}: "
            f"lowmid auth={r['auth_lowmid_nrmse']:.4f} self={r['self_lowmid_nrmse']:.4f} | "
            f"high auth={r['auth_high_nrmse']:.4f} self={r['self_high_nrmse']:.4f} | "
            f"enrich={r['high_enrichment']:.2f}",
            flush=True,
        )

    cal_rows = [r for r in rows if r["split"] == "CALIBRATION"]
    held_rows = [r for r in rows if r["split"] == "HELD_OUT"]

    report: dict[str, Any] = {
        "experiment": "EXP-M5",
        "phase": args.phase,
        "environment": environment_block(args.m3_manifest, args.resolution, args.phase, args.frozen_ack),
        "dataset": {
            "n_prepared": len(prepared),
            "n_rows": len(rows),
            "exclusions": exclusions,
            "split_counts": {"CALIBRATION": len(cal_rows), "LEGACY_HELDOUT": len(held_rows)},
        },
        "rows": rows,
    }

    if args.phase == "full":
        cohort = _cohort_block(rows)
        cal = _cohort_block(cal_rows)
        held = _cohort_block(held_rows)
        if cohort is None:
            raise RuntimeError("cohort vacío en fase full (no debería ocurrir tras el gate §3)")
        rules = evaluate_rules(cohort, held)
        report["summary"] = {
            "full": cohort,
            "calibration": cal,
            "legacy_heldout": held,
            "rules": rules,
            "decision": decide(rules),
            "bootstrap": {
                "excess_lowmid_nrmse": bootstrap_median_ci(
                    [r["excess_lowmid_nrmse"] for r in rows if r["lowmid_eligible"] >= 1.0]
                ),
                "excess_high_nrmse": bootstrap_median_ci(
                    [r["excess_high_nrmse"] for r in rows if r["high_eligible"] >= 1.0]
                ),
                "high_enrichment": bootstrap_median_ci(
                    [r["high_enrichment"] for r in rows if r["high_enrichment"] == r["high_enrichment"]]
                ),
            },
        }
        report["summary"]["decision"] = decide(rules)
    else:
        report["summary"] = {"decision": "PENDING_FREEZE"}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(_json_safe(report), fh, indent=1, allow_nan=False)
    print(f"\nJSON -> {args.out}")
    if args.phase == "full":
        print(f"DECISION: {report['summary']['decision']}")
    else:
        print("calibracion OK (smoke-test de implementacion; decision PENDING_FREEZE)")


if __name__ == "__main__":
    main()
