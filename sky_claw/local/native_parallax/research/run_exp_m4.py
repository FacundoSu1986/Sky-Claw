"""EXP-M4 — runner de la experimentación solver-vs-pair (research-only).

Uso (§8/§13 del preregistro):
  # 1) calibración = smoke-test de implementación (SIN decisión, SIN retuneo):
  python -m sky_claw.local.native_parallax.research.run_exp_m4 \
      --corpus-root /path/to/corpus --phase calibration --out /tmp/m4_calibration.json
  # 2) freeze commit (solo cambios de estado, cero umbrales) → anotar el SHA
  # 3) full (calibration+heldout+decisión) — EXIGE --frozen-ack:
  python -m sky_claw.local.native_parallax.research.run_exp_m4 \
      --corpus-root /path/to/corpus --phase full --frozen-ack freeze-<sha> --out data/exp-m4-results.json

El corpus NUNCA se committea; sólo agregados pequeños (JSON de filas/resumen).
Si el corpus no está o algún SHA256 difiere del manifest commiteado → exclusión
objetiva registrada; bajo el mínimo §3 → EXP_M4_DATA_INSUFFICIENT (jamás inventar).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import Any

import numpy as np
import PIL

from sky_claw.local.native_parallax.research.authored_dataset import (
    load_asset,
)
from sky_claw.local.native_parallax.research.run_exp_m2 import spearman, split_of
from sky_claw.local.native_parallax.research.run_exp_m3 import (
    SOLVER_NORMAL_CONVENTION,
    check_cohort_a_sufficient,
    load_cohort_a,
    material_spec_from_entry,
)
from sky_claw.local.native_parallax.research.solver_coherence import (
    BOOTSTRAP_SEED,
    T_DELTA_CORR,
    T_DELTA_RMSE,
    T_SELF_CORR,
    T_SELF_RMSE,
    T_SELF_VAR,
    bootstrap_median_ci,
    decide,
    delta_stats,
    evaluate_path,
    evaluate_rules,
    fd_forward,
    self_forward,
)
from sky_claw.local.native_parallax.research.trust_proxies import OracleOnly

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "design"
    / "research"
    / "native-parallax"
    / "data"
    / "exp-m3-clean-authored-manifest.json"
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def remap_path(win_path: str, corpus_root: Path) -> Path:
    """`C:\\SkyClawResearch\\NativeParallax\\EXP-M3\\originals\\…` → corpus_root/originals/….

    El manifest M3 es del repositorio (inmutable); el corpus local replica la misma
    estructura bajo `originals/` (así lo construye fetch_exp_m3_primary_corpus).
    """
    parts = PureWindowsPath(win_path).parts
    if "originals" not in parts:
        raise ValueError(f"path del manifest sin 'originals/': {win_path!r}")
    idx = parts.index("originals")
    return corpus_root.joinpath(*parts[idx:])


def prepare_entries(manifest_path: Path, corpus_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Gate §9 + split cross-check + mapeo de paths + verificación SHA256 previa.

    Exclusiones SOLO objetivas (§7 del preregistro), todas registradas. Un mismatch
    entre `split_of(family)` y `split_by_asset` del manifest es un defecto del HARNESS
    (ambos lados están publicados) → hard stop, no exclusión.
    """
    usables, rejected = load_cohort_a(manifest_path)
    check_cohort_a_sufficient(usables)
    raw = json.loads(manifest_path.read_text())
    split_by_asset = raw.get("cohort_a", {}).get("split_by_asset", {})

    prepared: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = [
        {"asset_id": str(r.get("asset_id", "?")), "reason": r["reason"]} for r in rejected
    ]
    for entry in usables:
        asset_id = str(entry["asset_id"])
        expected = split_by_asset.get(asset_id)
        if expected is not None and expected != split_of(str(entry["family"])):
            raise RuntimeError(
                f"split inconsistente para {asset_id}: manifest={expected} vs regla={split_of(entry['family'])} — "
                "defecto de harness, M4 se detiene (no es exclusión de asset)"
            )
        try:
            normal_path = remap_path(str(entry["normal_path"]), corpus_root)
            height_path = remap_path(str(entry["height_path"]), corpus_root)
        except ValueError as exc:
            exclusions.append({"asset_id": asset_id, "reason": f"path_invalid: {exc}"})
            continue
        for label, path, expected_sha in (
            ("normal", normal_path, str(entry["normal_sha256"])),
            ("height", height_path, str(entry["height_sha256"])),
        ):
            if not path.is_file():
                exclusions.append({"asset_id": asset_id, "reason": f"file_missing: {label} {path.name}"})
                break
            actual = sha256_file(path)
            if actual != expected_sha:
                exclusions.append({"asset_id": asset_id, "reason": f"sha256_mismatch: {label} {path.name}"})
                break
        else:
            local = dict(entry)
            local["normal_path"] = str(normal_path)
            local["height_path"] = str(height_path)
            prepared.append(local)
    return prepared, exclusions


def run_asset(entry: dict[str, Any], resolution: int) -> dict[str, Any]:
    """SELF-q8 (primario) + AUTH (primario) + SELF-float/FD (secundarios) + diagnóstico."""
    spec = material_spec_from_entry(entry)
    mat = load_asset(spec, resolution, tested_convention=SOLVER_NORMAL_CONVENTION)
    h = mat.height
    row: dict[str, Any] = {
        "asset": spec.asset_id,
        "family": spec.family,
        "split": split_of(spec.family),
        "provider": entry["provider"],
        "height_bit_depth": entry.get("height_bit_depth"),
        "normal_convention": entry.get("normal_convention"),
        "tested_convention": mat.tested_convention,
    }
    n_self_q8 = self_forward(h, bits=8)
    n_self_float = self_forward(h, bits=None)
    n_fd_q8 = fd_forward(h, bits=8)
    for label, normal in (
        ("self_q8", n_self_q8),
        ("self_float", n_self_float),
        ("fd_q8", n_fd_q8),
        ("auth", mat.normal),
    ):
        metrics = evaluate_path(h, normal, path=label)
        for key, value in metrics.items():
            row[f"{label}_{key[5:]}"] = value  # strip "path_" prefix
    row["self_rmse"] = float(row["self_q8_rmse"])
    row["self_abs_corr"] = float(row["self_q8_abs_corr"])
    row["self_var"] = float(row["self_q8_variance_ratio"])
    row["auth_rmse"] = float(row["auth_rmse"])
    row["auth_abs_corr"] = float(row["auth_abs_corr"])
    row["auth_var"] = float(row["auth_variance_ratio"])
    row["delta_rmse"] = float(row["auth_rmse"]) - float(row["self_rmse"])
    row["delta_abs_corr"] = float(row["self_abs_corr"]) - float(row["auth_abs_corr"])
    row["delta_var"] = float(row["self_var"]) - float(row["auth_var"])
    oracle = OracleOnly.normal_height_residual_oracle(mat.normal, h)
    row["coherence_agreement_deg"] = float(oracle["height_normal_oracle_agreement_deg"])
    row["coherence_best_strength"] = float(oracle["oracle_best_strength"])
    return row


def run_asset_native(entry: dict[str, Any], native_resolution: tuple[int, int]) -> dict[str, Any] | None:
    """Secundaria §4.1: resolución nativa SIN resize (separa el confundo del resize)."""
    if native_resolution[0] != native_resolution[1]:
        return None
    spec = material_spec_from_entry(entry)
    mat = load_asset(spec, native_resolution[0], tested_convention=SOLVER_NORMAL_CONVENTION)
    h = mat.height
    row: dict[str, Any] = {"asset": spec.asset_id, "split": split_of(spec.family)}
    for label, normal in (("self_q8", self_forward(h, bits=8)), ("auth", mat.normal)):
        metrics = evaluate_path(h, normal, path=label)
        for key, value in metrics.items():
            row[f"{label}_{key[5:]}"] = value
    row["self_rmse"] = float(row["self_q8_rmse"])
    row["self_abs_corr"] = float(row["self_q8_abs_corr"])
    row["self_var"] = float(row["self_q8_variance_ratio"])
    row["auth_rmse"] = float(row["auth_rmse"])
    row["auth_abs_corr"] = float(row["auth_abs_corr"])
    row["auth_var"] = float(row["auth_variance_ratio"])
    row["delta_rmse"] = float(row["auth_rmse"]) - float(row["self_rmse"])
    return row


def environment_block(manifest_path: Path, resolution: int, phase: str, frozen_ack: str | None) -> dict[str, Any]:
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - fuera de un checkout
        git_sha = "unknown"
    module = Path(__file__).with_name("solver_coherence.py")
    return {
        "git_sha": git_sha,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "solver_module_sha256": sha256_file(module) if module.is_file() else "unknown",
        "resolution": resolution,
        "phase": phase,
        "frozen_ack": frozen_ack,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "pillow": PIL.__version__,
        "fft_backend": "numpy.fft",
        "solver": 'reconstruct_from_normal(normal, "RAW", 0.0, 0.0) — M2/M3 sin cambios',
    }


def thresholds_block() -> dict[str, Any]:
    return {
        "T_SELF_RMSE": T_SELF_RMSE,
        "T_SELF_CORR": T_SELF_CORR,
        "T_SELF_VAR": T_SELF_VAR,
        "T_DELTA_RMSE": T_DELTA_RMSE,
        "T_DELTA_CORR": T_DELTA_CORR,
        "source": "preregistro §6 (committeado antes de la corrida)",
    }


def group_diagnostics(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Medianas por familia/provider — DIAGNÓSTICO suggestivo (n=3..5, §11)."""
    out: list[dict[str, Any]] = []
    for group in sorted({str(r[key]) for r in rows}):
        subset = [r for r in rows if str(r[key]) == group]
        out.append(
            {
                key: group,
                "n": len(subset),
                "rmse_self_median": float(np.median([r["self_rmse"] for r in subset])),
                "rmse_auth_median": float(np.median([r["auth_rmse"] for r in subset])),
                "delta_rmse_median": float(np.median([r["delta_rmse"] for r in subset])),
                "abs_corr_self_median": float(np.median([r["self_abs_corr"] for r in subset])),
                "abs_corr_auth_median": float(np.median([r["auth_abs_corr"] for r in subset])),
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="EXP-M4 — solver ceiling vs pair coherence (research-only)")
    parser.add_argument("--m3-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resolution", type=int, default=512)
    parser.add_argument("--phase", choices=("calibration", "full"), required=True)
    parser.add_argument("--frozen-ack", type=str, default=None, help="obligatorio con --phase full")
    args = parser.parse_args()
    if args.phase == "full" and not args.frozen_ack:
        parser.error("--phase full exige --frozen-ack (protocolo freeze §8)")

    prepared, exclusions = prepare_entries(args.m3_manifest, args.corpus_root)
    if args.phase == "calibration":
        prepared = [e for e in prepared if split_of(str(e["family"])) == "CALIBRATION"]
    if not prepared:
        print(json.dumps({"decision": "EXP_M4_DATA_INSUFFICIENT", "exclusions": exclusions}, indent=2))
        sys.exit(2)

    native_resolutions = {(int(e["normal_resolution"][0]), int(e["normal_resolution"][1])) for e in prepared}
    # secundaria §4.1 requiere resolución nativa única (manifest M3: 1024²)
    native_res = None if len(native_resolutions) != 1 else next(iter(native_resolutions))
    rows: list[dict[str, Any]] = []
    native_rows: list[dict[str, Any]] = []
    for entry in prepared:
        rows.append(run_asset(entry, args.resolution))
        if native_res is not None:
            native = run_asset_native(entry, native_res)
            if native is not None:
                native_rows.append(native)
        print(
            f"[{len(rows)}/{len(prepared)}] {rows[-1]['asset']}: self={rows[-1]['self_rmse']:.4f} "
            f"auth={rows[-1]['auth_rmse']:.4f} delta={rows[-1]['delta_rmse']:+.4f} "
            f"coh={rows[-1]['coherence_agreement_deg']:.1f}°",
            flush=True,
        )

    full_stats = delta_stats(rows)
    cal_rows = [r for r in rows if r["split"] == "CALIBRATION"]
    held_rows = [r for r in rows if r["split"] == "HELD_OUT"]
    cal_stats = delta_stats(cal_rows) if cal_rows else None
    held_stats = delta_stats(held_rows) if held_rows else None

    report: dict[str, Any] = {
        "experiment": "EXP-M4",
        "phase": args.phase,
        "environment": environment_block(args.m3_manifest, args.resolution, args.phase, args.frozen_ack),
        "thresholds": thresholds_block(),
        "dataset": {
            "n_prepared": len(prepared),
            "n_rows": len(rows),
            "exclusions": exclusions,
            "split_counts": {
                "CALIBRATION": len(cal_rows),
                "HELD_OUT": len(held_rows),
            },
        },
        "rows": rows,
        "summary": {
            "full": full_stats,
            "calibration": cal_stats,
            "heldout": held_stats,
            "secondary": {
                "native_no_resize": delta_stats(native_rows) if native_rows else None,
            },
        },
    }

    # secundarios por camino (mediana de cada columna relevante)
    def path_medians(prefix: str, subset: list[dict[str, Any]]) -> dict[str, float] | None:
        if not subset:
            return None
        return {
            "rmse_median": float(np.median([r[f"{prefix}_rmse"] for r in subset])),
            "abs_corr_median": float(np.median([r[f"{prefix}_abs_corr"] for r in subset])),
            "variance_ratio_median": float(np.median([r[f"{prefix}_variance_ratio"] for r in subset])),
        }

    report["summary"]["secondary"]["self_float"] = path_medians("self_float", rows)
    report["summary"]["secondary"]["fd_q8_diagnostic"] = path_medians("fd_q8", rows)

    if args.phase == "full" and held_stats is not None:
        rules = evaluate_rules(full_stats, held_stats)
        report["summary"]["rules"] = rules
        report["summary"]["decision"] = decide(rules["C1_self_good"], rules["C2_auth_worse"])
        report["summary"]["bootstrap"] = {
            "delta_rmse_full": bootstrap_median_ci([r["delta_rmse"] for r in rows]),
            "delta_rmse_heldout": bootstrap_median_ci([r["delta_rmse"] for r in held_rows]),
        }
        report["summary"]["coherence_diagnostic"] = {
            "median_agreement_deg": float(np.median([r["coherence_agreement_deg"] for r in rows])),
            "spearman_deg_vs_delta_rmse": spearman(
                [r["coherence_agreement_deg"] for r in rows], [r["delta_rmse"] for r in rows]
            ),
            "note": "diagnóstico de coherencia (§5); NO es trust proxy ni criterio de exclusión",
        }
    else:
        report["summary"]["decision"] = "PENDING_FREEZE" if args.phase == "calibration" else None

    report["summary"]["family_diagnostics"] = group_diagnostics(rows, "family")
    report["summary"]["provider_diagnostics"] = group_diagnostics(rows, "provider")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        json.dump(report, fh, indent=1, allow_nan=False)
    print(f"\nJSON → {args.out}")
    if args.phase == "full":
        print(f"DECISION: {report['summary']['decision']}")
        print(f"  full:    {full_stats}")
        print(f"  heldout: {held_stats}")
    else:
        print("calibración OK (smoke-test de implementación; decisión PENDING_FREEZE)")


if __name__ == "__main__":
    main()
