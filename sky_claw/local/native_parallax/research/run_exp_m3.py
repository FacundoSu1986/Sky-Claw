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
import math
from pathlib import Path
from typing import Any

import numpy as np

from sky_claw.local.native_parallax.research.authored_dataset import (
    AuthoredMaterial,
    DatasetInvalidError,
    MaterialSpec,
    load_asset,
    load_manifest,
)
from sky_claw.local.native_parallax.research.run_exp_m2 import run_policy, spearman, split_of
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

# §15 — convención que consume el solver EXP-M2 (p=-nx/nz, q=-ny/nz + integrate_periodic).
# Calibración empírica, independiente del resultado:
#   - corpus CM del control M2 (declared UNKNOWN, archivos "_Normal" planos): as-is bueno
#     (Rock030 0.96, Snow002 1.00, Tiles049 0.90) y flip malo;
#   - pares NormalGL (petroulacl/Bastiaan en M2 y nor_gl/NormalGL de Cohort A): as-is malo,
#     flip bueno (Tiles049 oficial 0.077→0.908);
#   - variante nor_dx de Poly Haven verificada contra md5 oficial: as-is 0.986 vs nor_gl as-is 0.428.
# Por lo tanto el solver está calibrado en DIRECTX y todo archivo OPENGL se convierte al
# cargar (§14: mismo material y misma geometría; apply_convention existe para esto).
SOLVER_NORMAL_CONVENTION = "DIRECTX"


# §17 — familia de umbrales low-trust PRE-REGISTRADA (sin búsqueda masiva post-hoc).
LOW_TRUST_THRESHOLDS: dict[str, Any] = {
    "rel_0.3_median": lambda nz: 0.3 * float(np.median(nz)),
    "abs_0.1": lambda _nz: 0.1,
}

# Lista de proxies pre-registrada (§30): la usan el análisis de B y la selección de A.
TRUST_PROXY_NAMES: tuple[str, ...] = (
    (
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
    )
    + tuple(f"low_trust_fraction@{k}" for k in LOW_TRUST_THRESHOLDS)
    + tuple(f"largest_low_trust_component_fraction@{k}" for k in LOW_TRUST_THRESHOLDS)
)

# §29/§31/§32 — mapeo evidencia→veredicto para Cohort A, PRE-REGISTRADO antes de la
# primera corrida con corpus primario y congelado por test. No se retunea después de
# ver resultados (§22): si el resultado es adverso, se reporta el estado que salga.
COHORT_A_DECISION_RULES: dict[str, Any] = {
    # RAW del solver: medianas del corpus vs la escala del CATASTROPHIC_RULE §27.
    "raw_min_median_abs_corr": 0.5,
    "raw_min_median_variance_ratio": 0.5,
    "raw_family_min_n": 3,  # familia evaluable: >= 3 assets
    # Gate de trust held-out; el proxy se selecciona SOLO en calibración (§41 M2).
    "trust_min_n_per_split": 5,
    "trust_min_abs_spearman_heldout": 0.5,
    "trust_min_auc_catastrophic_heldout": 0.75,
    "trust_min_auto_coverage": 0.3,
    "trust_max_catastrophic_false_safe": 0.0,
    "trust_min_consistent_families": 2,  # familias held-out (n>=3) con dirección correcta
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


def material_features(mat: AuthoredMaterial) -> dict[str, float]:
    """Features normal-only + clusters pre-registrados (§16/§17).

    Implementación ÚNICA compartida por Cohort A y Cohort B: si las dos superficies
    divergieran, el diagnóstico de B dejaría de ser comparable con el experimento
    principal (clase de defecto "hermano olvidado" del repo).
    """
    feats = normal_only_features(mat.normal, sigma_eff=0.0)
    nz = mat.nz
    for name, fn in LOW_TRUST_THRESHOLDS.items():
        mask = nz < fn(nz)
        cluster = largest_low_trust_component(mask)
        feats[f"low_trust_fraction@{name}"] = cluster["low_trust_fraction"]
        feats[f"largest_low_trust_component_fraction@{name}"] = cluster["largest_low_trust_component_fraction"]
    feats["height_std"] = float(np.std(mat.height))  # diagnóstico de coherencia (§26)
    return feats


def cohort_b_features(entry: dict[str, Any]) -> dict[str, float]:
    """Features normal-only de una fila de Cohort B (misma implementación que A)."""
    return material_features(entry["material"])


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
    """F4: scores y labels son pares por contrato — un mismatch es un bug del caller,
    no un AUC parcial silencioso. (Nota: zip NO es strict por defecto en 3.11 —
    verificado; el strict aquí es decisión contractual, no del runtime.)"""
    if len(scores) != len(labels):
        raise ValueError(f"auc: scores/labels desalineados ({len(scores)} vs {len(labels)})")
    pos = [s for s, lb in zip(scores, labels, strict=True) if lb]
    neg = [s for s, lb in zip(scores, labels, strict=True) if not lb]
    if not pos or not neg:
        return float("nan")
    wins = sum((1 if p > n else 0.5 if p == n else 0) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


# ----------------------------------------------------------------------
# Cohort A — experimento principal (§2): RAW + proxies + decision
# ----------------------------------------------------------------------


def material_spec_from_entry(entry: dict[str, Any]) -> MaterialSpec:
    """Entrada §10 de Cohort A → MaterialSpec del pipeline M2 (misma verificación M5)."""
    for key in ("normal_path", "height_path", "normal_convention", "height_semantics", "family"):
        if not entry.get(key):
            raise DatasetInvalidError(f"{entry.get('asset_id')}: entrada sin {key}")
    if entry["normal_convention"] == "UNKNOWN":
        raise DatasetInvalidError(
            f"{entry['asset_id']}: normal_convention UNKNOWN — el solver requiere una convención "
            "declarada para convertir a SOLVER_NORMAL_CONVENTION (§15, fail-closed)"
        )
    if entry.get("license") != "CC0-1.0":
        raise DatasetInvalidError(f"{entry['asset_id']}: licencia {entry.get('license')!r} != CC0-1.0")
    return MaterialSpec(
        asset_id=entry["asset_id"],
        family=entry["family"],
        source=entry["provider"],
        mirror=entry["provider"],  # descarga directa del provider: no hay mirror (§9)
        license=entry["license"],
        license_url=entry.get("license_reference", ""),
        source_url=entry["official_source"],
        normal_path=entry["normal_path"],
        height_path=entry["height_path"],
        normal_sha256=entry["normal_sha256"],
        height_sha256=entry["height_sha256"],
        native_resolution=(int(entry["normal_resolution"][0]), int(entry["normal_resolution"][1])),
        declared_convention=entry["normal_convention"],
        height_semantics=entry["height_semantics"],
        notes=entry.get("notes", ""),
    )


def evaluate_cohort_a(usables: list[dict[str, Any]], resolution: int) -> dict[str, Any]:
    """RAW + features + diagnóstico σ_eff independiente sobre Cohort A (§22).

    Un asset que no pasa el gate §39 (ilegible, hash cambiado, resolución) se excluye
    como DATASET_INVALID con razón explícita — la exclusión es por integridad del
    archivo, jamás por el resultado de reconstrucción (§28).
    """
    rows: list[dict[str, Any]] = []
    feats_by_asset: dict[str, dict[str, float]] = {}
    dataset_invalid: list[dict[str, str]] = []
    for entry in usables:
        try:
            spec = material_spec_from_entry(entry)
            # §15: el archivo declara su convención; el solver consume DIRECTX. La
            # conversión (flip de Y vía apply_convention) queda auditada en tested_convention.
            mat = load_asset(spec, resolution, tested_convention=SOLVER_NORMAL_CONVENTION)
        except DatasetInvalidError as exc:
            dataset_invalid.append({"asset_id": str(entry.get("asset_id", "?")), "reason": str(exc)})
            continue
        split = split_of(spec.family)
        feats = material_features(mat)
        feats_by_asset[spec.asset_id] = feats
        raw = run_policy(mat, spec, split, "RAW", 0.0, 0.0)
        refs = {"scale_ref": raw["affine_scale"], "sign_ref": raw["oracle_best_sign"]}
        oracle = OracleOnly.normal_height_residual_oracle(mat.normal, mat.height)
        row: dict[str, Any] = {
            "asset": spec.asset_id,
            "family": spec.family,
            "split": split,
            "provider": entry["provider"],
            "provenance_status": entry["provenance_status"],
            "height_bit_depth": entry.get("height_bit_depth"),
            "normal_convention": entry["normal_convention"],
            "tested_convention": mat.tested_convention,
            "oracle_agreement_deg": oracle["height_normal_oracle_agreement_deg"],
            "oracle_best_strength": oracle["oracle_best_strength"],
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
            "catastrophic": is_catastrophic_m3(raw["correlation"], raw["variance_ratio"]),
        }
        for sigma_name, feat_key in INDEPENDENT_SIGMA_CANDIDATES.items():
            sigma = max(feats[feat_key], 1e-6)
            for pol in ("SOFT_TIKHONOV", "FLOOR_CLAMP"):
                pr = run_policy(mat, spec, split, pol, 1.0, sigma, **refs)
                row[f"{pol}@sigma={sigma_name}:variance_ratio"] = pr["variance_ratio"]
                row[f"{pol}@sigma={sigma_name}:aligned_rmse"] = pr["aligned_rmse"]
        rows.append(row)
    return {"rows": rows, "features": feats_by_asset, "dataset_invalid": dataset_invalid}


def select_proxy_on_calibration(rows: list[dict[str, Any]], feats: dict[str, dict[str, float]]) -> str | None:
    """§41 M2 — el proxy se elige SOLO con CALIBRATION (|Spearman| vs aligned_rmse).

    Proxies degenerados en calibración (valor constante, p.ej. quantization en PNG)
    quedan fuera; empate se rompe por nombre (determinista). Held-out no participa.
    """
    cal = [r for r in rows if r["split"] == "CALIBRATION"]
    if len(cal) < 3:
        return None
    ranked: list[tuple[float, str]] = []
    for name in TRUST_PROXY_NAMES:
        values = [feats[r["asset"]][name] for r in cal]
        if len(set(values)) <= 1:
            continue
        rho = spearman(values, [r["aligned_rmse"] for r in cal])
        if np.isfinite(rho):
            ranked.append((abs(float(rho)), name))
    return max(ranked)[1] if ranked else None


def split_raw_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for split in ("CALIBRATION", "HELD_OUT"):
        subset = [r for r in rows if r["split"] == split]
        out[split] = {
            "n": len(subset),
            "median_abs_corr": float(np.median([abs(r["correlation"]) for r in subset])) if subset else float("nan"),
            "median_variance_ratio": float(np.median([r["variance_ratio"] for r in subset]))
            if subset
            else float("nan"),
            "catastrophic": sum(1 for r in subset if r["catastrophic"]),
        }
    return out


def family_summaries(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["family"], []).append(row)
    return {
        family: {
            "n": len(subset),
            "split": subset[0]["split"],
            "median_abs_corr": float(np.median([abs(r["correlation"]) for r in subset])),
            "median_variance_ratio": float(np.median([r["variance_ratio"] for r in subset])),
            "catastrophic": sum(1 for r in subset if r["catastrophic"]),
        }
        for family, subset in sorted(grouped.items())
    }


def build_cohort_a_summary(ev: dict[str, Any], *, sufficient: bool) -> dict[str, Any]:
    """Resumen §22 con los insumos del mapeo §29 (selección/cordura fuera de held-out)."""
    rows, feats = ev["rows"], ev["features"]
    summary: dict[str, Any] = {
        "sufficient": sufficient and bool(rows),
        "n": len(rows),
        "raw": {
            "median_abs_corr": float(np.median([abs(r["correlation"]) for r in rows])) if rows else float("nan"),
            "median_variance_ratio": float(np.median([r["variance_ratio"] for r in rows])) if rows else float("nan"),
        },
        "by_split": split_raw_summary(rows) if rows else {},
        "families": family_summaries(rows) if rows else {},
        "selected_proxy": None,
        "calibration": None,
        "heldout_trust": None,
        "heldout_rates": [],
        "heldout_family_directions": {},
        "catastrophic_rule": dict(CATASTROPHIC_RULE),
        "decision_rules": dict(COHORT_A_DECISION_RULES),
        "solver_normal_convention": SOLVER_NORMAL_CONVENTION,
    }
    if not rows:
        return summary
    proxy = select_proxy_on_calibration(rows, feats)
    summary["selected_proxy"] = proxy
    cal = [r for r in rows if r["split"] == "CALIBRATION"]
    held = [r for r in rows if r["split"] == "HELD_OUT"]
    if proxy is None or not cal or not held:
        return summary
    rho_cal = float(spearman([feats[r["asset"]][proxy] for r in cal], [r["aligned_rmse"] for r in cal]))
    orientation = 1.0 if rho_cal > 0 else -1.0
    summary["calibration"] = {"n": len(cal), "proxy": proxy, "spearman": rho_cal, "orientation": orientation}
    risk = {r["asset"]: {"risk_score": orientation * feats[r["asset"]][proxy]} for r in held}
    ci = spearman_ci([risk[r["asset"]]["risk_score"] for r in held], [r["aligned_rmse"] for r in held])
    summary["heldout_trust"] = {
        **ci,
        "auc_catastrophic": auc(
            [risk[r["asset"]]["risk_score"] for r in held], [bool(r["catastrophic"]) for r in held]
        ),
        "n_catastrophic": sum(1 for r in held if r["catastrophic"]),
    }
    summary["heldout_rates"] = rates_sweep(held, risk, "risk_score")
    directions: dict[str, dict[str, Any]] = {}
    for family in sorted({r["family"] for r in held}):
        subset = [r for r in held if r["family"] == family]
        if len(subset) >= 3:
            directions[family] = {
                "n": len(subset),
                "spearman": float(
                    spearman([risk[r["asset"]]["risk_score"] for r in subset], [r["aligned_rmse"] for r in subset])
                ),
            }
    summary["heldout_family_directions"] = directions
    return summary


def _raw_viable(median_abs_corr: float, median_variance_ratio: float) -> bool:
    rules = COHORT_A_DECISION_RULES
    return bool(
        np.isfinite(median_abs_corr)
        and np.isfinite(median_variance_ratio)
        and median_abs_corr >= rules["raw_min_median_abs_corr"]
        and median_variance_ratio >= rules["raw_min_median_variance_ratio"]
    )


def family_viability(families: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Familias (n>=3) claramente viables / no viables por medianas RAW (§32)."""
    viable: list[str] = []
    not_viable: list[str] = []
    for family, stats in families.items():
        if stats["n"] < COHORT_A_DECISION_RULES["raw_family_min_n"]:
            continue
        (viable if _raw_viable(stats["median_abs_corr"], stats["median_variance_ratio"]) else not_viable).append(family)
    return {"viable": sorted(viable), "not_viable": sorted(not_viable)}


def trust_gate_passes(summary: dict[str, Any]) -> bool:
    """§31 — gate GO: held-out real, generaliza, false-safe bajo, estable por familia."""
    rules = COHORT_A_DECISION_RULES
    held = summary.get("heldout_trust")
    cal = summary.get("calibration")
    if summary.get("selected_proxy") is None or held is None or cal is None:
        return False
    if cal.get("n", 0) < rules["trust_min_n_per_split"] or held.get("n", 0) < rules["trust_min_n_per_split"]:
        return False
    rho = float(held["spearman"])  # F5: claves contractuales — faltar es un bug, no un "no"
    if not np.isfinite(rho) or abs(rho) < rules["trust_min_abs_spearman_heldout"]:
        return False
    ci_low = float(held["ci_low"])
    ci_high = float(held["ci_high"])
    if not (np.isfinite(ci_low) and np.isfinite(ci_high)):
        raise ValueError("trust_gate_passes: ci_low/ci_high NaN en held-out (bootstrap vacío)")
    if not (ci_low > 0.0 or ci_high < 0.0):
        return False
    auc_value = float(held.get("auc_catastrophic", float("nan")))
    if not np.isfinite(auc_value) or auc_value < rules["trust_min_auc_catastrophic_heldout"]:
        return False
    if held.get("n_catastrophic", 0) < 2 or held.get("n", 0) - held.get("n_catastrophic", 0) < 2:
        return False
    consistent = sum(
        1
        for stats in summary.get("heldout_family_directions", {}).values()
        if stats["n"] >= 3 and np.isfinite(stats["spearman"]) and stats["spearman"] > 0
    )
    if consistent < rules["trust_min_consistent_families"]:
        return False
    return any(
        point["auto_safe_coverage"] >= rules["trust_min_auto_coverage"]
        and point["catastrophic_false_safe_rate"] <= rules["trust_max_catastrophic_false_safe"]
        for point in summary.get("heldout_rates", [])
    )


def decide_exp_m3(summary: dict[str, Any]) -> str:
    """§29 — exactamente uno de los cinco estados (reglas en COHORT_A_DECISION_RULES)."""
    if not summary.get("sufficient"):
        return "EXP_M3_DATA_INSUFFICIENT"
    raw = summary["raw"]
    viability = family_viability(summary.get("families", {}))
    if not _raw_viable(raw["median_abs_corr"], raw["median_variance_ratio"]):
        return "EXP_M3_RECONSTRUCTION_CONDITIONAL" if viability["viable"] else "EXP_M3_NO_GO"
    if viability["not_viable"]:
        # §32: restricción de dominio del solver — no se tapa con un trust score.
        return "EXP_M3_RECONSTRUCTION_CONDITIONAL"
    if trust_gate_passes(summary):
        return "EXP_M3_GO_NORMAL_ONLY_TRUST"
    return "EXP_M3_NORMAL_ONLY_TRUST_NO_GO"


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


def _json_safe(obj: Any) -> Any:
    """Convierte recursivamente float no finitos a None.

    Histórico de revisión: NaN/Inf en resultados (p.ej. auc_catastrophic en
    subconjuntos sin clase positiva, o family_rho_range de una familia con
    spearman constante) producía JSON inválido según RFC 8259. El
    sanitizado vive en el límite de serialización: los dicts internos
    conservan floats (proxy_selection filtra con np.isfinite).
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def proxy_analysis(rows: list[dict[str, Any]], feats: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """§30/§29: Spearman+bootstrapCI vs rmse, AUC vs catastrófico, consistencia familiar."""
    from sky_claw.local.native_parallax.research.run_exp_m2 import spearman

    proxy_names = TRUST_PROXY_NAMES
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
                "family_rho_range": (
                    [min(f for f in fam_rhos if np.isfinite(f)), max(f for f in fam_rhos if np.isfinite(f))]
                    if any(np.isfinite(f) for f in fam_rhos)
                    else None
                ),
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
        sufficient = True
        print(f"cohort A usable: {len(usables)} — experimento principal habilitado")
    except DataInsufficientError as exc:
        print(f"HARD STOP §3 → {exc}")
        print("continuando SOLO con cohort B etiquetada EVALUATION_DIAGNOSTIC_ONLY\n")
        sufficient = False

    ev: dict[str, Any] = {"rows": [], "features": {}, "dataset_invalid": []}
    if sufficient:
        ev = evaluate_cohort_a(usables, args.resolution)
        if ev["dataset_invalid"]:
            excluded = {d["asset_id"] for d in ev["dataset_invalid"]}
            remaining = [e for e in usables if e["asset_id"] not in excluded]
            try:
                check_cohort_a_sufficient(remaining)
            except DataInsufficientError as exc:
                print(f"HARD STOP §3 tras exclusiones §39 → {exc}")
                sufficient = False
            else:
                print(f"§39 excluyó {len(excluded)} asset(s) por integridad: {sorted(excluded)}")
    summary = build_cohort_a_summary(ev, sufficient=sufficient)
    decision = decide_exp_m3(summary)
    print(f"\nEXP-M3 decision: {decision}\n")

    results: dict[str, Any] = {
        "cohort_a": {
            "usable": len(usables),
            "evaluated": len(ev["rows"]),
            "rejected": rejected,
            "dataset_invalid": ev["dataset_invalid"],
            "summary": summary,
            "decision": decision,
            "manifest_sha256": manifest_sha256(args.m3_manifest),
        },
        "cohort_b_label": DIAGNOSTIC_LABEL,
        "catastrophic_rule_pre_registered": CATASTROPHIC_RULE,
        "by_threshold": {},
    }
    if ev["rows"]:
        results["cohort_a"]["raw_rows"] = ev["rows"]
        results["cohort_a"]["features"] = ev["features"]
        results["cohort_a"]["proxies_full"] = proxy_analysis(ev["rows"], ev["features"])
        cal_rows = [r for r in ev["rows"] if r["split"] == "CALIBRATION"]
        held_rows = [r for r in ev["rows"] if r["split"] == "HELD_OUT"]
        if len(cal_rows) >= 3:
            results["cohort_a"]["proxies_calibration"] = proxy_analysis(cal_rows, ev["features"])
        if len(held_rows) >= 3:
            results["cohort_a"]["proxies_held_out"] = proxy_analysis(held_rows, ev["features"])
        results["cohort_a"]["bands_independent_sigma"] = [
            band_test_independent_sigma(ev["rows"], ev["features"], s) for s in INDEPENDENT_SIGMA_CANDIDATES
        ]
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
    (args.out / "exp_m3_results.json").write_text(json.dumps(_json_safe(results), indent=1, allow_nan=False))
    print(f"\nresultados -> {args.out / 'exp_m3_results.json'}")


def ev_features(mid: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Recupera features desde evaluate_cohort_b cacheado en results (helper de main)."""
    feats: dict[str, dict[str, float]] = mid["features"]
    return feats


if __name__ == "__main__":
    main()
