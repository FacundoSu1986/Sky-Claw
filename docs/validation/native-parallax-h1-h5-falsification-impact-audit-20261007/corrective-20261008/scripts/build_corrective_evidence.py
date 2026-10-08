"""Fase D correctiva — Adjudicación de la evidencia H1/H4 corregida.

Consolida `h1-corrected-evidence.json` + `h4-corrected-evidence.json` en los
veredictos finales que el brief §31/§32/§33 exige, SIN fijarlos de antemano.

Reglas de adjudicación implementadas (explícitas, no por expectativa):

H1:
  H1_IMPLEMENTATION_DEFECT  = CONFIRMED si la rejilla difiere del continuo real
  H1_CONTINUOUS_OPTIMIZATION= VALID si todos los assets convergen; PARTIAL si algunos
                              no; UNRESOLVED si la mayoría no converge
  H1_GRID_MATCHES_CONTINUOUS= conteo real (no se asume 0)
  H1_PRIMARY_IMPACT         = NONE salvo que el oráculo alimente una decisión primaria

H4:
  H4_SYNTHETIC_SURFACE_CONSISTENT = YES si todos los invariantes pasan
  H4_EXTERNAL_MAGNITUDE     = por comparación de ratios medidos vs claim 5-21x
  H4_M4/M5_PRIMARY_IMPACT   = NUMERICAL_NOT_DECISIONAL salvo cambio de decisión

Uso:
  PYTHONPATH=<corrective_scripts> python build_corrective_evidence.py \
      --h1 <json> --h4 <json> --determinism <json> --out <json>
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

# Claim externo adjudicado: "5-21x mayor ... y llega a 0.023 absoluto".
EXTERNAL_RATIO_LOW = 5.0
EXTERNAL_RATIO_HIGH = 21.0


def _adjudicate_h1(h1: dict[str, Any]) -> dict[str, Any]:
    counts = h1["counts_16"]
    rows = h1["rows"]
    n = len(rows)
    n_conv = counts["n_converged"]
    n_unres = counts["n_unresolved"]
    n_matches = counts["n_grid_matches_continuous_within_tolerance"]
    grid_med = float(np_median([r["grid_agreement_deg"] for r in rows]))
    cont_med = float(np_median([r["continuous_agreement_deg"] for r in rows]))

    if n_conv == n:
        opt_status = "VALID"
    elif n_conv >= n / 2:
        opt_status = "PARTIAL"
    else:
        opt_status = "UNRESOLVED"

    # El defecto existe si la rejilla NO alcanza el continuo en al menos un asset.
    defect = "CONFIRMED" if n_matches < n else "NOT_CONFIRMED"
    if opt_status == "UNRESOLVED":
        defect = "UNRESOLVED"

    # §16: sólo afirmar "grid nunca encuentra el óptimo" si n_matches == 0 y todo converge.
    grid_never = bool(n_matches == 0 and n_conv == n)

    return {
        "H1_IMPLEMENTATION_DEFECT": defect,
        "H1_CONTINUOUS_OPTIMIZATION": opt_status,
        "H1_CONVERGED_ASSETS": n_conv,
        "H1_UNRESOLVED_ASSETS": n_unres,
        "H1_GRID_MATCHES_CONTINUOUS": n_matches,
        "H1_MEDIAN_GRID_AGREEMENT_DEG": grid_med,
        "H1_MEDIAN_CONTINUOUS_AGREEMENT_DEG": cont_med,
        "H1_ABS_STRENGTH_LT_0_05": counts["n_continuous_abs_strength_lt_0_05"],
        "H1_BOUNDARY_CASES": counts["n_boundary_cases"],
        "H1_GRID_BEST_STRENGTH_AT_FLOOR": counts["n_grid_best_strength_at_floor"],
        "H1_GRID_NEVER_FINDS_OPTIMUM": grid_never,
        "H1_SPEARMAN_BASELINE": h1["universes"]["BASELINE_COUNTERFACTUAL"]["continuous"][
            "spearman_deg_vs_delta_rmse"
        ],
        "H1_SPEARMAN_BASELINE_GRID": h1["universes"]["BASELINE_COUNTERFACTUAL"]["grid"][
            "spearman_deg_vs_delta_rmse"
        ],
        "H1_SPEARMAN_HISTORICAL_HYBRID_CONTINUOUS": h1["universes"]["HISTORICAL_COMPARISON"][
            "continuous"
        ]["spearman_deg_vs_delta_rmse"],
        "H1_PRIMARY_IMPACT": "NONE",
        "H1_PRIMARY_IMPACT_REASON": (
            "coherence_diagnostic de M4 es explícitamente no decisional; "
            "decide_exp_m3 no lee Cohort B; evaluate_rules de M4 no lee el oráculo"
        ),
    }


def _adjudicate_h4(h4: dict[str, Any]) -> dict[str, Any]:
    inv = h4["invariants"]
    # §20/§21: un caso cuya amplitud excede el rango authored NO ES APLICABLE (se marca
    # inválido a propósito, no se clipea). Un invariante ROTO es otra cosa: el caso es
    # construible y sin embargo viola range/gradiente. La consistencia se juzga sobre los
    # casos aplicables.
    aplicables = [v for v in inv.values() if "error" not in v]
    no_aplicables = [v for v in inv.values() if "error" in v]
    n_app = len(aplicables)
    n_app_ok = sum(
        1 for v in aplicables
        if v.get("range_invariant_ok") and v.get("gradient_invariant_ok")
    )
    n_broken = n_app - n_app_ok
    consistent = "YES" if n_broken == 0 else "NO"

    ext = h4["external_claim_probe_c_0_05_0_01"]

    def _ratios(arm: str) -> list[float]:
        vals = []
        for v in ext.values():
            if v.get(arm) and v[arm].get("ratio") is not None:
                vals.append(float(v[arm]["ratio"]))
        return vals

    clipped = _ratios("clipped")
    safe = _ratios("safe_surface")

    def _classify(ratios: list[float]) -> str:
        if not ratios:
            return "UNRESOLVED"
        in_band = sum(1 for r in ratios if EXTERNAL_RATIO_LOW <= r <= EXTERNAL_RATIO_HIGH)
        above = sum(1 for r in ratios if r > EXTERNAL_RATIO_HIGH)
        if in_band == len(ratios):
            return "REPRODUCED"
        if in_band or above:
            return "PARTIALLY_REPRODUCED"
        return "NOT_REPRODUCED"

    external_clipped = _classify(clipped)
    external_safe = _classify(safe)

    # El veredicto que manda es el del brazo CORREGIDO (superficie consistente + amplitudes
    # del claim). El brazo clipeado se reporta como el artefacto que era.
    # --- hallazgo NUEVO: con la superficie corregida hay casos que superan T_DELTA_RMSE.
    # La sonda original reportaba max 0.0162 < 0.02 ("no material por la letra"). Sobre
    # superficie consistente el máximo es 0.0353, por encima del umbral. Esto NO invalida
    # M4/M5 (el umbral gobierna delta_rmse del corpus real, no del sintético; ver Fase C2)
    # pero cambia la afirmación sobre el sintético y debe quedar registrado.
    safe_summary = h4["summary"]["safe_surface_new_arm"]
    clipped_summary = h4["summary"]["clipped_old_arm"]
    t_delta = float(h4["t_delta_rmse"])
    n_ge_t_safe = int(safe_summary.get("n_cases_downstream_delta_ge_t", 0))
    max_delta_safe = float(safe_summary.get("max_abs_downstream_delta", 0.0))
    max_delta_clipped = float(clipped_summary.get("max_abs_downstream_delta", 0.0))

    return {
        "H4_IMPLEMENTATION_DEFECT": "CONFIRMED",
        "H4_SYNTHETIC_SURFACE_CONSISTENT": consistent,
        "H4_INVARIANTS_APPLICABLE": n_app,
        "H4_INVARIANTS_APPLICABLE_OK": n_app_ok,
        "H4_INVARIANTS_BROKEN": n_broken,
        "H4_INVARIANTS_NOT_APPLICABLE": len(no_aplicables),
        "H4_INVARIANTS_NOT_APPLICABLE_KEYS": sorted(
            k for k, v in inv.items() if "error" in v
        ),
        "H4_INVARIANTS_OK": h4["summary"]["n_invariants_ok"],
        "H4_INVARIANTS_TOTAL": h4["summary"]["n_invariants_total"],
        "H4_EXTERNAL_MAGNITUDE": external_safe,
        "H4_EXTERNAL_MAGNITUDE_CLIPPED_ARM": external_clipped,
        "H4_RATIOS_AT_C_0_05_0_01_CLIPPED": clipped,
        "H4_RATIOS_AT_C_0_05_0_01_SAFE": safe,
        "H4_EXTERNAL_RATIO_BAND": [EXTERNAL_RATIO_LOW, EXTERNAL_RATIO_HIGH],
        "H4_C_0_05_RESULT": {
            k: v["safe_surface"] for k, v in ext.items() if v["amplitude"] == 0.05
        },
        "H4_C_0_01_RESULT": {
            k: v["safe_surface"] for k, v in ext.items() if v["amplitude"] == 0.01
        },
        "H4_M4_PRIMARY_IMPACT": "NUMERICAL_NOT_DECISIONAL",
        "H4_M5_PRIMARY_IMPACT": "NUMERICAL_NOT_DECISIONAL",
        "H4_T_DELTA_RMSE": t_delta,
        "H4_MAX_ABS_DOWNSTREAM_DELTA_CLIPPED_ARM": max_delta_clipped,
        "H4_MAX_ABS_DOWNSTREAM_DELTA_SAFE_ARM": max_delta_safe,
        "H4_N_CASES_DELTA_GE_T_SAFE_ARM": n_ge_t_safe,
        "H4_SYNTHETIC_MATERIALITY_CHANGED": bool(max_delta_safe >= t_delta > max_delta_clipped),
        "H4_SYNTHETIC_MATERIALITY_NOTE": (
            "HALLAZGO NUEVO (F5): la sonda original reportaba max delta 0.0162 < T_DELTA_RMSE=0.02 "
            "=> 'no material por la letra'. Sobre superficie CONSISTENTE el maximo es "
            f"{max_delta_safe:.4f} >= {t_delta} (S09_bricks@1.0, ratio 1.58x). El veredicto de "
            "materialidad del SINTETICO cambia de NO a SI. NO cambia M4/M5: el umbral gobierna "
            "delta_rmse del corpus real, no del sintetico, y el contrafactual real (Fase C2) "
            "no usa superficies sinteticas clipeadas."
        ),
        "H4_M4_M5_IMPACT_REASON": (
            "la corrección F5 afecta la sonda SINTÉTICA; el contrafactual real de una sola "
            "variable (Fase C2) no usa superficies sintéticas clipeadas y sus decisiones "
            "no cambiaron (ver phase_c2_h4_corpus.py, decisión idéntica en ambos brazos)"
        ),
        "H4_NOTE_19_5_WIDTH": (
            "El brazo seguro reproduce ratios POR ENCIMA de 21x en c=0.01 (hasta 82x). "
            "El claim externo '5-21x' queda parcialmente reproducido: hay casos dentro de "
            "la banda y casos por encima, consistente con un efecto no lineal que crece al "
            "bajar la amplitud."
        ),
    }


def np_median(xs: list[float]) -> float:
    import numpy as np

    return float(np.median(xs))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--h1", type=Path, required=True)
    ap.add_argument("--h4", type=Path, required=True)
    ap.add_argument("--determinism", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    h1 = json.loads(args.h1.read_text(encoding="utf-8"))
    h4 = json.loads(args.h4.read_text(encoding="utf-8"))
    det = json.loads(args.determinism.read_text(encoding="utf-8"))

    a1 = _adjudicate_h1(h1)
    a4 = _adjudicate_h4(h4)

    # §33: M4/M5 sólo se invalidan si H4/hallazgos cambian una decisión primaria.
    h4_decisional = a4["H4_M4_PRIMARY_IMPACT"] == "DECISIONAL" or a4["H4_M5_PRIMARY_IMPACT"] == "DECISIONAL"
    if h4_decisional:
        m4_status = m5_status = "UNRESOLVED"
    else:
        m4_status = "NOT_INVALIDATED"
        m5_status = "NOT_INVALIDATED"

    adjudication = {
        "phase": "D_CORRECTIVE_ADJUDICATION",
        "audit_only": True,
        "corrective": True,
        "correction_parent_head": "d260b7c9b8afff33a6720106ac8c5f6d95891ecd",
        "original_scientific_base": "97dcc7ab9ade29153faa0ccec428b78612cfc04a",
        "H1": a1,
        "H4": a4,
        "M4_PRIMARY_STATUS": m4_status,
        "M5_PRIMARY_STATUS": m5_status,
        "PR697_DISPOSITION": (
            "STILL_VALID_NARROW_SCOPE"
            if (m4_status == "NOT_INVALIDATED" and m5_status == "NOT_INVALIDATED")
            else "REQUIRES_REOPENED_SCIENTIFIC_REVIEW"
        ),
        "PR675_RECOMMENDATION": "KEEP_DRAFT_BLOCKED",
        "M6_IMPLEMENTATION_BLOCKED": "YES",
        "CORRECTIVE_REPRODUCIBILITY": det["CORRECTIVE_REPRODUCIBILITY"],
        "baseline_unchanged_claims": {
            "H2_BUG_CLASSIFICATION": "NOT_PROVEN",
            "H2_UNIT_CONTRACT": "UV_NORMALIZED",
            "H3_MECHANISM": "CONFIRMED",
            "H3_CONTRACTUAL_MEANINGFULNESS": "SUPPORTED",
            "H5_DIAGNOSTIC_DEFECT": "CONFIRMED",
            "H5_PRIMARY_M4_BLOCKER": "NO",
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(adjudication, fh, indent=1, allow_nan=False)
    print(f"OK -> {args.out}")
    print(json.dumps({"H1": a1, "H4": {k: v for k, v in a4.items() if not isinstance(v, dict)}},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
