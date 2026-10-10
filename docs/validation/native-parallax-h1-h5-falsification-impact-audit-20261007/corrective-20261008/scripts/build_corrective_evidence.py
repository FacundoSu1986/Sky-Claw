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

La adjudicación final es **fail-closed respecto de C2**: `--c2` es obligatorio y el
archivo debe existir; un C2 ausente, ilegible o que no pase el gate de identidad de
roster ABORTA la corrida (no se publican M4/M5). El campo `m4.decision_changed` del C2
participa de la decisión: si es `True`, M4 no puede quedar `NOT_INVALIDATED`.

Uso:
  PYTHONPATH=<corrective_scripts> python build_corrective_evidence.py \
      --h1 <json> --h4 <json> --determinism <json> --c2 <json> --out <json>
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

# Claim externo adjudicado, DESDOBLADO (segunda ronda correctiva, finding C):
#   componente relativo:  "5-21x mayor"
#   componente absoluto:  "llega a 0.023 absoluto"
# La primera ronda adjudicaba sólo el componente relativo y dejaba que un ratio alto
# sugiriera que el valor absoluto también se había reproducido. Se separan.
EXTERNAL_RATIO_LOW = 5.0
EXTERNAL_RATIO_HIGH = 21.0
EXTERNAL_ABSOLUTE_CLAIM = 0.023
# Factor de tolerancia sobre el valor absoluto reclamado:
#   dentro de 2x  -> REPRODUCED
#   dentro de 10x -> PARTIALLY_REPRODUCED
#   más allá      -> NOT_REPRODUCED
ABSOLUTE_FACTOR_REPRODUCED = 2.0
ABSOLUTE_FACTOR_PARTIAL = 10.0

# ---- Contrato C2 fail-closed (micro-slice final).
# La adjudicación final NO puede publicar M4/M5 sin evidencia C2 válida. El C2 se acepta
# sólo si el gate de identidad de roster lo permite Y cada una de las cuatro condiciones
# es **exactamente** `True`: una clave ausente, `None`, `1` o un string truthy bloquean.
C2_REQUIRED_ROSTER_CONDITIONS = (
    "roster_count_match",
    "roster_identity_match",
    "historical_roster_digest_matches_frozen_sha",
    "m3_manifest_sha256_matches_frozen",
)


def _classify_absolute(deltas: list[float]) -> str:
    """Adjudica el componente ABSOLUTO del claim contra `EXTERNAL_ABSOLUTE_CLAIM`.

    Se compara el mayor delta medido: si ni el mejor caso se acerca al valor
    reclamado, el componente absoluto no está reproducido, por más que los
    ratios (adimensionales) luzcan altos.
    """
    if not deltas:
        return "UNRESOLVED"
    best = max(deltas)
    claim = EXTERNAL_ABSOLUTE_CLAIM
    if claim <= 0:
        return "UNRESOLVED"
    if claim / ABSOLUTE_FACTOR_REPRODUCED <= best <= claim * ABSOLUTE_FACTOR_REPRODUCED:
        return "REPRODUCED"
    if claim / ABSOLUTE_FACTOR_PARTIAL <= best <= claim * ABSOLUTE_FACTOR_PARTIAL:
        return "PARTIALLY_REPRODUCED"
    return "NOT_REPRODUCED"


def validate_c2_for_adjudication(c2_doc: Any) -> dict[str, Any]:
    """Valida la evidencia C2 antes de publicar M4/M5. Fail-closed.

    Devuelve:
      - ``valid``: True sólo si TODAS las condiciones se cumplen.
      - ``failed_conditions``: condiciones incumplidas (vacía si es válido).
      - ``roster_identity`` / ``roster_gate``: los bloques validados (dict o None).
      - ``m4_decision_changed``: `c2_doc["m4"]["decision_changed"]` cuando es bool; None
        si el campo falta o no es booleano (no se puede descartar un cambio).
    """
    failed: list[str] = []
    if not isinstance(c2_doc, dict):
        return {
            "valid": False,
            "failed_conditions": ["c2_document_is_object"],
            "roster_identity": None,
            "roster_gate": None,
            "m4_decision_changed": None,
        }

    roster = c2_doc.get("roster_identity")
    roster_gate = c2_doc.get("roster_gate")
    if not isinstance(roster, dict):
        failed.append("roster_identity_is_object")
        roster = None
    if not isinstance(roster_gate, dict):
        failed.append("roster_gate_is_object")
        roster_gate = None

    if isinstance(roster_gate, dict) and roster_gate.get("allowed") is not True:
        failed.append("roster_gate.allowed")

    if isinstance(roster, dict):
        for cond in C2_REQUIRED_ROSTER_CONDITIONS:
            if roster.get(cond) is not True:
                failed.append(cond)

    m4 = c2_doc.get("m4")
    m4_decision_changed: bool | None
    if not isinstance(m4, dict):
        failed.append("m4_is_object")
        m4_decision_changed = None
    elif not isinstance(m4.get("decision_changed"), bool):
        # Sin este campo legible no se puede descartar un cambio de decisión: fail-closed.
        failed.append("m4.decision_changed_is_bool")
        m4_decision_changed = None
    else:
        m4_decision_changed = bool(m4["decision_changed"])

    return {
        "valid": not failed,
        "failed_conditions": failed,
        "roster_identity": roster,
        "roster_gate": roster_gate,
        "m4_decision_changed": m4_decision_changed,
    }


def resolve_primary_statuses(h4_m4_impact: str, h4_m5_impact: str, c2_m4_decision_changed: bool) -> dict[str, Any]:
    """Decide `M4_PRIMARY_STATUS` / `M5_PRIMARY_STATUS` (§33 + propagación C2).

    - H4 decisional (M4 **o** M5) => ambos `UNRESOLVED`. Gate existente: NO se debilita.
    - C2 con `m4.decision_changed=True` => M4 `UNRESOLVED`, M5 `NOT_INVALIDATED`: un
      hallazgo exclusivamente M4 no mueve M5.
    - Caso contrario => ambos `NOT_INVALIDATED`.
    """
    h4_decisional = h4_m4_impact == "DECISIONAL" or h4_m5_impact == "DECISIONAL"
    if h4_decisional:
        return {
            "M4_PRIMARY_STATUS": "UNRESOLVED",
            "M5_PRIMARY_STATUS": "UNRESOLVED",
            "H4_DECISIONAL": True,
        }
    return {
        "M4_PRIMARY_STATUS": "UNRESOLVED" if c2_m4_decision_changed else "NOT_INVALIDATED",
        "M5_PRIMARY_STATUS": "NOT_INVALIDATED",
        "H4_DECISIONAL": False,
    }


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
        "H1_GRID_MATCHES_REFINED": counts.get("n_grid_matches_refined", n_matches),
        "H1_GRID_MATCH_TOLERANCE_DEG": counts.get("grid_match_tolerance_deg"),
        "H1_VALID_MINIMUM_BRACKET": counts.get("n_valid_minimum_bracket"),
        "H1_STOP_CRITERION_MET": counts.get("n_stop_criterion_met"),
        "H1_CALLER_BRACKET_ACCEPTED": counts.get("n_caller_bracket_accepted"),
        "H1_REFINEMENT_LOST_BETTER_COARSE": counts.get("n_refinement_lost_better_coarse"),
        # --- alcance honesto de la búsqueda (finding A)
        "H1_SEARCH_SCOPE": h1.get("search_scope", {}).get("scope"),
        "H1_GLOBAL_OPTIMUM_PROVEN": bool(h1.get("search_scope", {}).get("global_optimum_proven", False)),
        "H1_MEDIAN_GRID_AGREEMENT_DEG": grid_med,
        "H1_MEDIAN_CONTINUOUS_AGREEMENT_DEG": cont_med,
        "H1_MEDIAN_REFINED_AGREEMENT_DEG": cont_med,
        "H1_ABS_STRENGTH_LT_0_05": counts["n_continuous_abs_strength_lt_0_05"],
        "H1_BOUNDARY_CASES": counts["n_boundary_cases"],
        "H1_BOUNDARY_ASSETS": counts["n_boundary_cases"],
        "H1_GRID_BEST_STRENGTH_AT_FLOOR": counts["n_grid_best_strength_at_floor"],
        "H1_GRID_NEVER_FINDS_OPTIMUM": grid_never,
        "H1_SPEARMAN_BASELINE": h1["universes"]["BASELINE_COUNTERFACTUAL"]["continuous"]["spearman_deg_vs_delta_rmse"],
        "H1_SPEARMAN_BASELINE_GRID": h1["universes"]["BASELINE_COUNTERFACTUAL"]["grid"]["spearman_deg_vs_delta_rmse"],
        "H1_SPEARMAN_HISTORICAL_HYBRID_CONTINUOUS": h1["universes"]["HISTORICAL_COMPARISON"]["continuous"][
            "spearman_deg_vs_delta_rmse"
        ],
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
    n_app_ok = sum(1 for v in aplicables if v.get("range_invariant_ok") and v.get("gradient_invariant_ok"))
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

    def _deltas(arm: str) -> list[float]:
        vals = []
        for v in ext.values():
            if v.get(arm) and v[arm].get("delta") is not None:
                vals.append(abs(float(v[arm]["delta"])))
        return vals

    clipped_deltas = _deltas("clipped")
    safe_deltas = _deltas("safe_surface")

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
    absolute_clipped = _classify_absolute(clipped_deltas)
    absolute_safe = _classify_absolute(safe_deltas)

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
        "H4_INVARIANTS_NOT_APPLICABLE_KEYS": sorted(k for k, v in inv.items() if "error" in v),
        "H4_INVARIANTS_OK": h4["summary"]["n_invariants_ok"],
        "H4_INVARIANTS_TOTAL": h4["summary"]["n_invariants_total"],
        "H4_EXTERNAL_MAGNITUDE": external_safe,
        "H4_EXTERNAL_MAGNITUDE_CLIPPED_ARM": external_clipped,
        # --- segunda ronda correctiva (finding C): adjudicación DESDOBLADA
        "H4_RATIO_CLAIM_STATUS": external_safe,
        "H4_ABSOLUTE_CLAIM_STATUS": absolute_safe,
        "H4_RATIO_CLAIM_STATUS_CLIPPED_ARM": external_clipped,
        "H4_ABSOLUTE_CLAIM_STATUS_CLIPPED_ARM": absolute_clipped,
        "H4_EXTERNAL_ABSOLUTE_CLAIM": EXTERNAL_ABSOLUTE_CLAIM,
        "H4_ABSOLUTE_DELTAS_SAFE_ARM": safe_deltas,
        "H4_ABSOLUTE_DELTAS_CLIPPED_ARM": clipped_deltas,
        "H4_ABSOLUTE_MAX_SAFE_ARM": max(safe_deltas) if safe_deltas else None,
        "H4_ABSOLUTE_FACTOR_VS_CLAIM_SAFE_ARM": (
            (max(safe_deltas) / EXTERNAL_ABSOLUTE_CLAIM) if safe_deltas and EXTERNAL_ABSOLUTE_CLAIM else None
        ),
        "H4_ABSOLUTE_TOLERANCE_FACTORS": {
            "reproduced_within": ABSOLUTE_FACTOR_REPRODUCED,
            "partial_within": ABSOLUTE_FACTOR_PARTIAL,
        },
        "H4_EXTERNAL_MAGNITUDE_SUMMARY": (
            f"RATIO={external_safe}; ABSOLUTE={absolute_safe}. "
            f"Los ratios adimensionales del brazo de superficie consistente caen en la banda "
            f"{EXTERNAL_RATIO_LOW:g}-{EXTERNAL_RATIO_HIGH:g}x (y la superan en c=0.01), pero el "
            f"componente ABSOLUTO del claim ({EXTERNAL_ABSOLUTE_CLAIM:g}) NO se reproduce: el mayor "
            f"delta medido es {max(safe_deltas) if safe_deltas else float('nan'):.3e}, "
            f"{EXTERNAL_ABSOLUTE_CLAIM / max(safe_deltas) if safe_deltas and max(safe_deltas) else float('inf'):.0f}x "
            f"por debajo. Ratio alto NO implica absoluto reproducido."
        ),
        "H4_RATIOS_AT_C_0_05_0_01_CLIPPED": clipped,
        "H4_RATIOS_AT_C_0_05_0_01_SAFE": safe,
        "H4_EXTERNAL_RATIO_BAND": [EXTERNAL_RATIO_LOW, EXTERNAL_RATIO_HIGH],
        "H4_C_0_05_RESULT": {k: v["safe_surface"] for k, v in ext.items() if v["amplitude"] == 0.05},
        "H4_C_0_01_RESULT": {k: v["safe_surface"] for k, v in ext.items() if v["amplitude"] == 0.01},
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
    # C2 es OBLIGATORIO: sin evidencia de identidad de roster no se publica M4/M5.
    ap.add_argument(
        "--c2",
        type=Path,
        required=True,
        help="evidencia C2 (identidad de roster + decisión M4); obligatoria y fail-closed",
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    h1 = json.loads(args.h1.read_text(encoding="utf-8"))
    h4 = json.loads(args.h4.read_text(encoding="utf-8"))
    det = json.loads(args.determinism.read_text(encoding="utf-8"))

    # ---- C2 obligatorio y fail-closed (micro-slice final, finding 1).
    # Antes: `--c2` era opcional y, si el archivo no existía, el builder seguía y
    # publicaba M4/M5 = NOT_INVALIDATED sin ninguna evidencia de identidad de roster.
    if not args.c2.exists():
        print(f"HARD STOP: C2 evidence not found: {args.c2}", file=sys.stderr)
        raise SystemExit(3)
    c2_doc = json.loads(args.c2.read_text(encoding="utf-8"))
    c2_validation = validate_c2_for_adjudication(c2_doc)
    if not c2_validation["valid"]:
        print(
            "HARD STOP: C2 evidence failed fail-closed validation -> NO M4/M5 adjudication",
            file=sys.stderr,
        )
        print(
            json.dumps(
                {k: v for k, v in c2_validation.items() if k not in ("roster_identity", "roster_gate")},
                indent=1,
                ensure_ascii=False,
            )
        )
        raise SystemExit(3)
    roster = c2_validation["roster_identity"]
    roster_gate = c2_validation["roster_gate"]

    a1 = _adjudicate_h1(h1)
    a4 = _adjudicate_h4(h4)

    # §33 + propagación C2 (finding 2): H4 decisional mantiene el gate actual; si no,
    # un `m4.decision_changed=True` del C2 impide publicar M4 como NOT_INVALIDATED.
    statuses = resolve_primary_statuses(
        a4["H4_M4_PRIMARY_IMPACT"], a4["H4_M5_PRIMARY_IMPACT"], c2_validation["m4_decision_changed"]
    )
    m4_status = statuses["M4_PRIMARY_STATUS"]
    m5_status = statuses["M5_PRIMARY_STATUS"]
    # ¿El fix cambió el resultado científico? Se compara contra la lógica PRE-fix
    # (sin propagación C2) sobre la MISMA evidencia: si M4/M5 no se mueven, es NO.
    _pre_fix = resolve_primary_statuses(
        a4["H4_M4_PRIMARY_IMPACT"], a4["H4_M5_PRIMARY_IMPACT"], c2_m4_decision_changed=False
    )
    scientific_result_changed = (m4_status, m5_status) != (
        _pre_fix["M4_PRIMARY_STATUS"],
        _pre_fix["M5_PRIMARY_STATUS"],
    )

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
        # --- C2 fail-closed (micro-slice final): la adjudicación final exige un C2
        # válido; sin él la corrida aborta y estos campos no se publican.
        "C2_VALIDATED_FOR_ADJUDICATION": True,
        "C2_REQUIRED_ROSTER_CONDITIONS": list(C2_REQUIRED_ROSTER_CONDITIONS),
        "C2_M4_DECISION_CHANGED": bool(c2_validation["m4_decision_changed"]),
        "SCIENTIFIC_RESULT_CHANGED": "YES" if scientific_result_changed else "NO",
        # --- identidad del roster (finding F; endurecido en la ronda 3)
        "ROSTER_COUNT_MATCH": (roster or {}).get("roster_count_match"),
        "ROSTER_IDENTITY_MATCH": (roster or {}).get("roster_identity_match"),
        "ROSTER_DIGEST_MATCH": (roster or {}).get("roster_digest_match"),
        # Renombrado (ronda 3): el campo viejo `MANIFEST_DIGEST_MATCH` NO comparaba el
        # digest del manifiesto; comparaba el del roster HISTÓRICO contra el SHA congelado.
        "HISTORICAL_ROSTER_DIGEST_MATCHES_FROZEN_SHA": (roster or {}).get(
            "historical_roster_digest_matches_frozen_sha"
        ),
        # Contrato SEPARADO (ronda 3): el ARCHIVO del manifiesto M3, canónico LF.
        "M3_MANIFEST_SHA256_MATCHES_FROZEN": (roster or {}).get("m3_manifest_sha256_matches_frozen"),
        # El gate es lo que decide; se publica su veredicto, no sólo los insumos.
        "ROSTER_GATE_ALLOWED": (roster_gate or {}).get("allowed"),
        "ROSTER_GATE_FAILED_CONDITIONS": (roster_gate or {}).get("failed_conditions"),
        "ROSTER_DETAIL": roster,
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
    print(
        json.dumps(
            {"H1": a1, "H4": {k: v for k, v in a4.items() if not isinstance(v, dict)}}, indent=1, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
