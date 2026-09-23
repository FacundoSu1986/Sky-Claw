"""EXP-M3 — clean authored corpus / normal-only trust decision (research-only).

Roles de las cohortes (§2):
  A — CLEAN_BY_PROVENANCE: sólo assets con provenance_status PRIMARY_VERIFIED u
      OFFICIAL_HASH_VERIFIED en el manifest EXP-M3. Si N < 15 o familias < 3 →
      EXP_M3_DATA_INSUFFICIENT y el experimento estadístico principal NO corre (§3).
  B — ORACLE_FILTERED_DIAGNOSTIC (EVALUATION_DIAGNOSTIC_ONLY): subset de EXP-M2
      filtrado con authored height SÓLO para diagnóstico; jamás mecanismo productivo.

Reglas endurecidas tras el review de EXP-M2:
  §15 — PROHIBIDO σ_eff = f(nz_min/p01/p05): los candidatos independientes viven en
        INDEPENDENT_SIGMA_CANDIDATES y se valida por test que ningún nombre pertenece
        a la familia nz (no repetir la circularidad r≡1).
  §27 — la regla de CATASTROPHIC_RECONSTRUCTION está PRE-REGISTRADA en
        CATASTROPHIC_RULE y es invariante a escala (corr + variance_ratio con escala
        compartida del RAW del mismo asset).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from sky_claw.local.native_parallax.research.authored_dataset import load_asset, load_manifest
from sky_claw.local.native_parallax.research.run_exp_m2 import run_policy, split_of
from sky_claw.local.native_parallax.research.trust_proxies import (
    OracleOnly,
    largest_low_trust_component,
    normal_only_features,
)

COHORT_A_MIN_ASSETS = 15
COHORT_A_MIN_FAMILIES = 3
# §9 — estados admitidos para Cohort A. El schema previo usaba el ambiguo
# PRIMARY_VERIFIED para "descargado del proveedor" y "hash oficial verificado" a la
# vez; ahora se distinguen: PRIMARY_SOURCE_DOWNLOADED = bytes bajados del provider
# oficial + SHA256 propio post-descarga; OFFICIAL_HASH_VERIFIED = además el upstream
# publicó checksum y nuestros bytes coinciden. MIRROR_ONLY/MIRROR_HASH_VERIFIED/
# UNKNOWN quedan fuera de Cohort A (evidencia principal).
VALID_PROVENANCE = ("PRIMARY_SOURCE_DOWNLOADED", "OFFICIAL_HASH_VERIFIED")
DIAGNOSTIC_LABEL = "EVALUATION_DIAGNOSTIC_ONLY"
ORACLE_FILTER_DEGREES = (20.0, 30.0, 40.0)  # sensibilidad §12; 30° NO es normativo
HEIGHT_FLAT_STD = 2.0 / 255.0  # pre-registrado: height con std < 2/255 es "plano"

# §27 — PRE-REGISTRADA antes de mirar proxies sobre la cohorte B:
# catastrófico = estructura mayormente decorrelada O relieve mayormente destruido.
# Ambas métricas son invariables a la escala authored (§38): corr es adimensional y
# variance_ratio se mide con la escala afín compartida fitada en el RAW del asset.
CATASTROPHIC_RULE = {"corr_abs_below": 0.5, "variance_ratio_below": 0.5}


def is_catastrophic_m3(corr: float, variance_ratio: float) -> bool:
    """Regla §27 (pre-registrada; ver CATASTROPHIC_RULE)."""
    return bool(
        abs(corr) < CATASTROPHIC_RULE["corr_abs_below"] or variance_ratio < CATASTROPHIC_RULE["variance_ratio_below"]
    )


# §15/§33 — candidatos de σ_eff INDEPENDIENTES de la familia nz (validado por test).
INDEPENDENT_SIGMA_CANDIDATES: dict[str, str] = {
    "curl_mad": "curl_mad",
    "projection_median": "projection_residual_median_deg",
}
NZ_FAMILY_PREFIXES = ("nz_min", "nz_p01", "nz_p05", "r_min", "r_p01", "r_p05")

# §17 — familia de umbrales low-trust PRE-REGISTRADA (sin búsqueda masiva post-hoc).
LOW_TRUST_THRESHOLDS: dict[str, Any] = {
    "rel_0.3_median": lambda nz: 0.3 * float(np.median(nz)),
    "abs_0.1": lambda _nz: 0.1,
}


class DataInsufficientError(RuntimeError):
    """§3 — Cohort A no alcanza el mínimo; el experimento principal no corre."""


def load_cohort_a(manifest_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Carga SÓLO assets con provenance válida (§9). Devuelve (usables, rechazados)."""
    raw = json.loads(manifest_path.read_text())
    usables: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for entry in raw.get("cohort_a", {}).get("assets", []):
        status = entry.get("provenance_status")
        if status not in VALID_PROVENANCE:
            rejected.append({"asset_id": entry.get("asset_id"), "reason": f"provenance {status!r} no admitida"})
            continue
        for key in ("asset_id", "provider", "official_source", "normal_sha256", "height_sha256", "license"):
            if not entry.get(key):
                rejected.append({"asset_id": entry.get("asset_id"), "reason": f"falta {key}"})
                break
        else:
            usables.append(entry)
    return usables, rejected


def check_cohort_a_sufficient(usables: list[dict[str, Any]]) -> None:
    """§3 — HARD STOP: sin mínimo de assets/familias el experimento principal no corre."""
    families = {e.get("family") for e in usables}
    if len(usables) < COHORT_A_MIN_ASSETS or len(families) < COHORT_A_MIN_FAMILIES:
        raise DataInsufficientError(
            f"EXP_M3_DATA_INSUFFICIENT: cohort A = {len(usables)} assets / "
            f"{len(families)} familias (mínimo {COHORT_A_MIN_ASSETS}/{COHORT_A_MIN_FAMILIES})"
        )


def load_cohort_b(
    m2_manifest: Path,
    resolution: int,
    oracle_degrees: float,
) -> list[dict[str, Any]]:
    """Construye la cohorte B con filtro oracle (DIAGNÓSTICO ONLY, §2/§12).

    El authored height se usa EXCLUSIVAMENTE aquí para decidir PERTENENCIA a la
    cohorte; las features de trust jamás lo reciben (§13, mismas APIs que M2).
    """
    specs = load_manifest(m2_manifest)
    cohort: list[dict[str, Any]] = []
    for spec in specs:
        mat = load_asset(spec, resolution)
        if float(np.std(mat.height)) < HEIGHT_FLAT_STD:
            continue  # height plano: no hay referencia contra la cual evaluar
        agree = OracleOnly.normal_height_residual_oracle(mat.normal, mat.height)
        if agree["height_normal_oracle_agreement_deg"] >= oracle_degrees:
            continue
        cohort.append(
            {
                "spec": spec,
                "material": mat,
                "oracle_agreement_deg": agree["height_normal_oracle_agreement_deg"],
                "oracle_best_strength": agree["oracle_best_strength"],
                "label": DIAGNOSTIC_LABEL,
                "split": split_of(spec.family),
            }
        )
    return cohort


def cohort_b_features(entry: dict[str, Any]) -> dict[str, float]:
    """Features normal-only + clusters con familia de umbrales pre-registrada (§16/§17)."""
    mat = entry["material"]
    feats = normal_only_features(mat.normal, sigma_eff=0.0)
    nz = mat.nz
    for name, fn in LOW_TRUST_THRESHOLDS.items():
        mask = nz < fn(nz)
        cluster = largest_low_trust_component(mask)
        feats[f"low_trust_fraction@{name}"] = cluster["low_trust_fraction"]
        feats[f"largest_low_trust_component_fraction@{name}"] = cluster["largest_low_trust_component_fraction"]
    feats["height_std"] = float(np.std(mat.height))  # diagnóstico de coherencia (§26)
    return feats


def manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spearman_ci(x: list[float], y: list[float], *, n_boot: int = 1000, seed: int = 20260922) -> dict[str, float]:
    """Spearman puntual + intervalo bootstrap (§29: honestidad con N pequeño)."""
    from sky_claw.local.native_parallax.research.run_exp_m2 import spearman

    point = spearman(x, y)
    rng = np.random.default_rng(seed)
    n = len(x)
    if n < 4:
        return {"spearman": point, "ci_low": float("nan"), "ci_high": float("nan"), "n": float(n)}
    boots = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        boots.append(spearman([x[i] for i in idx], [y[i] for i in idx]))
    boots_arr = np.asarray(boots, dtype=np.float64)
    boots_arr = boots_arr[np.isfinite(boots_arr)]
    return {
        "spearman": point,
        "ci_low": float(np.percentile(boots_arr, 2.5)) if boots_arr.size else float("nan"),
        "ci_high": float(np.percentile(boots_arr, 97.5)) if boots_arr.size else float("nan"),
        "n": float(n),
    }


def auc(scores: list[float], labels: list[bool]) -> float:
    pos = [s for s, lb in zip(scores, labels, strict=False) if lb]
    neg = [s for s, lb in zip(scores, labels, strict=False) if not lb]
    if not pos or not neg:
        return float("nan")
    wins = sum((1 if p > n else 0.5 if p == n else 0) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


# ----------------------------------------------------------------------
# Análisis Cohort B (diagnóstico) y experimento principal Cohort A
# ----------------------------------------------------------------------


def evaluate_cohort_b(cohort: list[dict[str, Any]]) -> dict[str, Any]:
    """RAW + policies (σ_eff INDEPENDIENTE) + proxies vs outcome pre-registrado.

    Etiqueta obligatoria en TODO resultado: EVALUATION_DIAGNOSTIC_ONLY.
    """
    rows: list[dict[str, Any]] = []
    feats_by_asset: dict[str, dict[str, float]] = {}
    for entry in cohort:
        spec = entry["spec"]
        mat = entry["material"]
        feats = cohort_b_features(entry)
        feats_by_asset[spec.asset_id] = feats
        raw = run_policy(mat, spec, entry["split"], "RAW", 0.0, 0.0)
        refs = {"scale_ref": raw["affine_scale"], "sign_ref": raw["oracle_best_sign"]}
        cat = is_catastrophic_m3(raw["correlation"], raw["variance_ratio"])
        row: dict[str, Any] = {
            "asset": spec.asset_id,
            "family": spec.family,
            "split": entry["split"],
            "label": DIAGNOSTIC_LABEL,
            "oracle_agreement_deg": entry["oracle_agreement_deg"],
            "aligned_rmse": raw["aligned_rmse"],
            "raw_centered_rmse": raw["raw_centered_rmse"],
            "gradient_rmse": raw["gradient_rmse"],
            "correlation": raw["correlation"],
            "best_sign": raw["best_sign"],
            "variance_ratio": raw["variance_ratio"],
            "gradient_energy_ratio": raw["gradient_energy_ratio"],
            "seam_height": raw["seam_height"],
            "seam_gradient": raw["seam_gradient"],
            "nz_p01": feats["nz_p01"],
            "nz_min": feats["nz_min"],
            "nz_p05": feats["nz_p05"],
            "catastrophic": cat,
        }
        # policies con σ_eff INDEPENDIENTE de nz (§15/§33/§35): sólo diagnóstico
        for sigma_name, feat_key in INDEPENDENT_SIGMA_CANDIDATES.items():
            sigma = max(feats[feat_key], 1e-6)
            for pol in ("SOFT_TIKHONOV", "FLOOR_CLAMP"):
                pr = run_policy(mat, spec, entry["split"], pol, 1.0, sigma, **refs)
                row[f"{pol}@sigma={sigma_name}:variance_ratio"] = pr["variance_ratio"]
                row[f"{pol}@sigma={sigma_name}:aligned_rmse"] = pr["aligned_rmse"]
        rows.append(row)
    return {"rows": rows, "features": feats_by_asset}


def proxy_analysis(rows: list[dict[str, Any]], feats: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """§30/§29: Spearman+bootstrapCI vs rmse, AUC vs catastrófico, consistencia familiar."""
    from sky_claw.local.native_parallax.research.run_exp_m2 import spearman

    proxy_names = (
        [
            "nz_min",
            "nz_p01",
            "nz_p05",
            "curl_mad",
            "curl_p95_abs",
            "projection_residual_median_deg",
            "projection_residual_p95_deg",
            "blockiness_ratio",
            "quantization_proxy",
            "unit_length_residual_p95",
        ]
        + [f"low_trust_fraction@{k}" for k in LOW_TRUST_THRESHOLDS]
        + [f"largest_low_trust_component_fraction@{k}" for k in LOW_TRUST_THRESHOLDS]
    )
    fams = sorted({r["family"] for r in rows})
    out = []
    for name in proxy_names:
        xs = [feats[r["asset"]][name] for r in rows]
        ys = [r["aligned_rmse"] for r in rows]
        ci = spearman_ci(xs, ys)
        cats = [r["catastrophic"] for r in rows]
        # signo de referencia por familia: correlación dentro de cada familia si n>=4
        fam_rhos = []
        for f in fams:
            fx = [feats[r["asset"]][name] for r in rows if r["family"] == f]
            fy = [r["aligned_rmse"] for r in rows if r["family"] == f]
            if len(fx) >= 4:
                fam_rhos.append(spearman(fx, fy))
        out.append(
            {
                "proxy": name,
                **ci,
                "auc_catastrophic": auc(xs, cats) if any(cats) and not all(cats) else float("nan"),
                "family_rho_range": [min(fam_rhos), max(fam_rhos)] if fam_rhos else None,
                "n_families_tested": len(fam_rhos),
            }
        )
    return out


def rates_sweep(
    rows: list[dict[str, Any]],
    feats: dict[str, dict[str, float]],
    proxy: str,
) -> list[dict[str, Any]]:
    """§31: curva de operaciones sobre UN proxy (sin fijar threshold productivo)."""
    xs = sorted(feats[r["asset"]][proxy] for r in rows)
    out = []
    for q in (0.1, 0.2, 0.3, 0.4, 0.5):
        th = float(np.quantile(xs, q))
        reject = [r for r in rows if feats[r["asset"]][proxy] >= th]
        auto = [r for r in rows if feats[r["asset"]][proxy] < th]
        cats_auto = sum(1 for r in auto if r["catastrophic"])
        cats_rej = sum(1 for r in reject if r["catastrophic"])
        out.append(
            {
                "proxy": proxy,
                "threshold": th,
                "auto_safe_coverage": len(auto) / len(rows),
                "reject_rate": len(reject) / len(rows),
                "catastrophic_false_safe_rate": cats_auto / max(sum(1 for r in rows if r["catastrophic"]), 1),
                "false_review_rate": 1 - cats_rej / max(len(reject), 1),
            }
        )
    return out


def band_test_independent_sigma(
    rows: list[dict[str, Any]],
    feats: dict[str, dict[str, float]],
    sigma_candidate: str,
) -> dict[str, Any]:
    """§34 con denominador INDEPENDIENTE de nz: r_p01 = nz_p01 / σ_eff(candidate)."""
    feat_key = INDEPENDENT_SIGMA_CANDIDATES[sigma_candidate]
    bands = {"<2": [0, 0], "2-6": [0, 0], ">=6": [0, 0]}
    for r in rows:
        sigma = max(feats[r["asset"]][feat_key], 1e-6)
        r_p01 = r["nz_p01"] / sigma
        key = "<2" if r_p01 < 2 else ("2-6" if r_p01 < 6 else ">=6")
        bands[key][0] += 1
        bands[key][1] += int(r["catastrophic"])
    return {"sigma_candidate": sigma_candidate, "bands": bands}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--m3-manifest", type=Path, required=True)
    parser.add_argument("--m2-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resolution", type=int, default=512)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    usables, rejected = load_cohort_a(args.m3_manifest)
    try:
        check_cohort_a_sufficient(usables)
        print(f"cohort A usable: {len(usables)} — experimento principal habilitado")
        raise SystemExit("main cohort-A experiment: implementar en próxima corrida (no alcanzado en esta sesión)")
    except DataInsufficientError as exc:
        print(f"HARD STOP §3 → {exc}")
        print("continuando SOLO con cohort B etiquetada EVALUATION_DIAGNOSTIC_ONLY\n")

    results: dict[str, Any] = {
        "cohort_a": {
            "usable": len(usables),
            "rejected": rejected,
            "decision": "EXP_M3_DATA_INSUFFICIENT",
            "manifest_sha256": manifest_sha256(args.m3_manifest),
        },
        "cohort_b_label": DIAGNOSTIC_LABEL,
        "catastrophic_rule_pre_registered": CATASTROPHIC_RULE,
        "by_threshold": {},
    }
    for deg in ORACLE_FILTER_DEGREES:
        cohort = load_cohort_b(args.m2_manifest, args.resolution, deg)
        if len(cohort) < 6:
            results["by_threshold"][str(deg)] = {"n": len(cohort), "note": "n demasiado pequeño"}
            continue
        ev = evaluate_cohort_b(cohort)
        proxies = proxy_analysis(ev["rows"], ev["features"])
        results["by_threshold"][str(deg)] = {
            "n": len(cohort),
            "families": sorted({r["family"] for r in ev["rows"]}),
            "catastrophic": sum(1 for r in ev["rows"] if r["catastrophic"]),
            "proxies": proxies,
            "rows": ev["rows"],
            "features": ev["features"],
        }
    # sweep de tasas sobre el mejor proxy del filtro 30° (por |spearman| con CI)
    mid = results["by_threshold"].get("30.0", {})
    if mid.get("proxies"):
        # excluir proxies degenerados (valor constante en el corpus, p.ej. quantization
        # ≡ 1.0 en JPG): sin varianza no hay curva de tasas que valga
        def _degenerate(name: str) -> bool:
            vals = {mid["features"][r["asset"]][name] for r in mid["rows"]}
            return len(vals) <= 1

        best = max(
            (p for p in mid["proxies"] if np.isfinite(p["spearman"]) and not _degenerate(p["proxy"])),
            key=lambda p: abs(p["spearman"]),
        )
        results["rates_sweep@30deg"] = rates_sweep(mid["rows"], ev_features(mid), best["proxy"])
        results["rates_sweep_proxy"] = best["proxy"]
        sigma_tests = [
            band_test_independent_sigma(mid["rows"], ev_features(mid), s) for s in INDEPENDENT_SIGMA_CANDIDATES
        ]
        results["bands_independent_sigma"] = sigma_tests
    (args.out / "exp_m3_results.json").write_text(json.dumps(results, indent=1))
    print(f"\nresultados → {args.out / 'exp_m3_results.json'}")


def ev_features(mid: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Recupera features desde evaluate_cohort_b cacheado en results (helper de main)."""
    feats: dict[str, dict[str, float]] = mid["features"]
    return feats


if __name__ == "__main__":
    main()
