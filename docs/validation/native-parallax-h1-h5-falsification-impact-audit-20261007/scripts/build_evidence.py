"""Consolida la evidencia medida (scratch/*.json) en los artefactos del audit.

NO re-ejecuta nada: sólo lee los JSON de las fases y emite los 4 artefactos
machine-readable del namespace del audit, con las adjudicaciones explícitas.

Uso:
  python build_evidence.py --scratch <dir> --out <dir>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

BASE_SHA = "97dcc7ab9ade29153faa0ccec428b78612cfc04a"
EXTERNAL_AUDIT_COMMIT = "dcc6551ef5afb2dfcab1b8662d1eafedc8b7d133"
BRANCH = "research/native-parallax-h1-h5-falsification-impact-audit"
WORKTREE = r"E:\SkyClaw_H1H5_AUDIT_97dcc7ab"


def load(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    s = args.scratch
    o = args.out
    o.mkdir(parents=True, exist_ok=True)

    a = load(s / "phase_a.json")
    b = load(s / "phase_b.json")
    c = load(s / "phase_c.json")
    c2 = load(s / "phase_c2.json")
    h4p = load(s / "h4_probe.json")

    # ---------------------------------------------------------------- synthetic
    synthetic = {
        "artifact": "native-parallax-h1-h5-synthetic-evidence",
        "base_sha": BASE_SHA,
        "audit_only": True,
        "phases": {
            "A_synthetic_falsification": a,
            "B_unit_contract": b,
            "H4_magnitude_probe": h4p,
        },
        "key_readings": {
            "H1": {
                "grid": "concat(linspace(0.05,5.0,25), -linspace(0.05,5.0,25)) — trust_proxies.py:296",
                "max_grid_angle_deg_over_perfectly_coherent_pairs": a["h1"]["summary"]["max_grid_angle_deg"],
                "max_continuous_angle_deg": a["h1"]["summary"]["max_continuous_angle_deg"],
                "median_delta_angle_deg": a["h1"]["summary"]["median_delta_angle_deg"],
                "oracle_replica_max_abs_diff_deg": a["h1"]["summary"]["max_oracle_replica_abs_diff"],
                "closed_form_recovers_true_strength_abs_err": a["h1"]["summary"]["max_closed_form_vs_true_abs"],
            },
            "H3": {
                "max_rmse_ratio_across_cases": a["h3"]["summary"]["max_rmse_ratio_across_cases"],
                "self_float_aligned_rmse_is_machine_eps": True,
            },
            "H4": {
                "max_downstream_delta_rmse_phaseA": a["h4_synthetic"]["summary"]["max_downstream_delta_rmse"],
                "material_effect_phaseA": a["h4_synthetic"]["summary"]["material_effect"],
                "max_downstream_delta_rmse_probe": h4p["summary"]["max_abs_downstream_aligned_delta"],
                "max_normal_rmse_old_vs_new_probe": h4p["summary"]["max_normal_rmse_old_vs_new"],
                "max_downstream_ratio_probe": h4p["summary"]["max_downstream_aligned_ratio"],
                "t_delta_rmse": 0.02,
                "external_audit_claim_ratio_5x_21x": "NOT_REPRODUCED (max ratio measured 1.201x)",
            },
            "H5": {
                "float_ratio_max": a["h5"]["summary"]["float_ratio_max"],
                "q8_ratio_max": a["h5"]["summary"]["q8_ratio_max"],
                "interpretation": (
                    "En float el factor de escala entre fd_forward (por muestra) y fd_units "
                    "(por unidad UV) es absorbido EXACTAMENTE por el fit afín global "
                    "(ratio = 1.000000000000007). En Q8 el ratio se dispersa (0.063–5.94) "
                    "porque la cuantización es no lineal: lo que mide el control FD-Q8 es la "
                    "interacción unidad↔cuantización, no la discretización FD."
                ),
            },
            "H2": {
                "circularity_probe": b["circularity_probe"],
                "square_identity_abs_diff": b["square_identity"]["abs_diff"],
                "conclusion": (
                    "El mismo grid rectangular da v1 correcto y v2 incorrecto si el sintético se "
                    "construye bajo UV_NORMALIZED, y lo opuesto si se construye bajo "
                    "ISOTROPIC_TEXEL. El sintético rectangular es CIRCULAR: no adjudica cuál "
                    "contrato es el correcto."
                ),
            },
        },
    }
    (o / "synthetic-evidence.json").write_text(json.dumps(synthetic, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------- real impact
    real = {
        "artifact": "native-parallax-h1-h5-real-impact",
        "base_sha": BASE_SHA,
        "audit_only": True,
        "corpus_used": True,
        "corpus_explanation": (
            "Se usó el corpus real READ-ONLY (C:\\SkyClawResearch\\NativeParallax\\EXP-M3, 31 assets "
            "1024², SHA256 verificado contra el manifest commiteado, 0 exclusiones) para DOS "
            "contrafactuales de instrumentación: (a) H1 — oráculo de rejilla vs oráculo continuo "
            "sobre el diagnóstico de coherencia §5 de M4; (b) H4 — resize_normal uint8 vs float "
            "sobre las métricas primarias de M4 y M5. Ninguno de los dos re-ejecuta run_exp_m4 / "
            "run_exp_m5: se recalcula sólo la columna afectada reutilizando los valores "
            "históricos inmutables para todo lo demás."
        ),
        "H1_coherence_oracle_counterfactual": c,
        "H4_resize_normal_counterfactual": c2,
        "H4_gate_decision": {
            "protocol": "§11/§12 exigen 'efecto material' frente a T_DELTA_RMSE=0.02 antes de ir al corpus",
            "phaseA_max_downstream_delta_rmse": a["h4_synthetic"]["summary"]["max_downstream_delta_rmse"],
            "magnitude_probe_max_downstream_delta_rmse": h4p["summary"]["max_abs_downstream_aligned_delta"],
            "ratio_to_threshold": h4p["summary"]["max_abs_downstream_aligned_delta"] / 0.02,
            "adjudication": (
                "El máximo sintético alcanza 81% de T_DELTA_RMSE (0.0162/0.02) y el audit externo "
                "describe el efecto como 'del orden de T_DELTA_RMSE'. Se consideró justificado el "
                "contrafactual READ-ONLY: declarar inmaterial sin medirlo habría sido una "
                "afirmación no soportada en la dirección 'no hay problema' (§21). Se ejecutó UNA "
                "sola variable."
            ),
        },
        "m3_cohort_b": c["m3_cohort_b_membership"],
        "historical_artifact_note": {
            "artifact": "data/exp-m4-results.json",
            "git_sha": c["historical_m4"]["git_sha"],
            "median_agreement_deg": c["historical_m4"].get("median_agreement_deg"),
            "reproducible_at_baseline": False,
            "max_replica_abs_diff_deg": c["grid_oracle_signature"]["max_replica_abs_diff_deg"],
            "explanation": (
                "El diagnóstico de coherencia grabado en el artefacto histórico NO se reproduce "
                "bit a bit en el baseline 97dcc7ab (mediana 13.398° histórica vs 10.709° "
                "recalculada; diff máximo por asset 23.24°). La causa es que el artefacto fue "
                "producido en fe54e9a9 (no ancestro de 97dcc7ab) y la ruta del oráculo cambió en "
                "PR-MATH-A (#681). Por eso la adjudicación de H1 compara rejilla vs continuo "
                "AMBOS recalculados en el baseline actual, no contra el número histórico."
            ),
        },
    }
    (o / "real-impact.json").write_text(json.dumps(real, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------- hypothesis status
    hyp = {
        "artifact": "native-parallax-h1-h5-hypothesis-status",
        "base_sha": BASE_SHA,
        "audit_only": True,
        "external_audit_commit": EXTERNAL_AUDIT_COMMIT,
        "note": (
            "El audit externo se trató como HIPÓTESIS, no como verdad: H1/H3/H4/H5 se "
            "reprodujeron independientemente y H2 se resolvió formalmente."
        ),
        "hypotheses": {
            "H1": {
                "claim": "El oráculo de strength busca en una rejilla lineal finita; un par height↔normal perfectamente coherente reporta disagreement ≠ 0 sólo porque la strength verdadera no está en la rejilla.",
                "H1_IMPLEMENTATION_DEFECT": "CONFIRMED",
                "evidence_synthetic": {
                    "max_grid_angle_deg": a["h1"]["summary"]["max_grid_angle_deg"],
                    "max_continuous_angle_deg": a["h1"]["summary"]["max_continuous_angle_deg"],
                    "oracle_replica_max_abs_diff_deg": a["h1"]["summary"]["max_oracle_replica_abs_diff"],
                    "closed_form_recovers_true_strength": a["h1"]["summary"]["max_closed_form_vs_true_abs"],
                },
                "evidence_real": {
                    "n_assets": c["grid_oracle_signature"]["n_assets"],
                    "n_best_strength_at_grid_floor": c["grid_oracle_signature"]["n_best_strength_at_grid_floor"],
                    "n_continuous_strength_below_floor": c["grid_oracle_signature"]["n_continuous_strength_below_floor"],
                    "grid_median_agreement_deg": c["coherence_diagnostic"]["grid"]["median_agreement_deg"],
                    "continuous_median_agreement_deg": c["coherence_diagnostic"]["continuous"]["median_agreement_deg"],
                    "grid_spearman": c["coherence_diagnostic"]["grid"]["spearman_deg_vs_delta_rmse"],
                    "continuous_spearman": c["coherence_diagnostic"]["continuous"]["spearman_deg_vs_delta_rmse"],
                },
                "H1_M3_COHORT_B_MEMBERSHIP_CHANGED": "NOT_COMPUTABLE (corpus EXP-M2 ausente)",
                "H1_M3_FINAL_DECISION_PATH_REACHED": "NO",
                "H1_M4_COHERENCE_DIAGNOSTIC_CHANGED": "YES",
                "H1_M4_C1_C2_PATH_REACHED": "NO",
                "H1_M3_PRIMARY_IMPACT": "NONE",
                "H1_M4_PRIMARY_IMPACT": "NONE",
                "blast_radius": [
                    "M3 Cohort B: filtro de pertenencia con el oráculo (load_cohort_b) — cohorte etiquetada EVALUATION_DIAGNOSTIC_ONLY; NO entra en decide_exp_m3",
                    "M3 Cohort A: oracle_agreement_deg se GRABA por fila pero no alimenta build_cohort_a_summary / trust_gate_passes / decide_exp_m3",
                    "M4: coherence_agreement_deg → coherence_diagnostic (§5) — docstring propio: 'NO es trust proxy ni criterio de exclusión'",
                    "M4 C1/C2: evaluate_rules usa self_rmse/abs_corr/var/delta_rmse — no toca el oráculo",
                ],
            },
            "H2": {
                "claim": "El solver asume x=j/W, y=i/H (tile cuadrado físico); un normal map real codifica pendiente por texel isotrópico, así que en texturas no cuadradas la proyección L2 queda sesgada anisotrópicamente.",
                "H2_NUMERICAL_OBSERVATION": "REPRODUCED",
                "H2_BUG_CLASSIFICATION": "NOT_PROVEN",
                "H2_UNIT_CONTRACT": "UV_NORMALIZED",
                "evidence": {
                    "circularity_probe": b["circularity_probe"],
                    "square_identity_abs_diff": b["square_identity"]["abs_diff"],
                    "implemented_contract_sources": [
                        "normal_fft_periodic.freq_axes: wx = 2π·fftfreq(W)·W (rad por unidad de x)",
                        "normal_fft_periodic docstring: x=j/W, y=i/H — coordenadas de textura, NO índices de muestra",
                        "tests/test_native_parallax_math_spike.py::test_frecuencias_en_espacio_de_textura (grid NO cuadrado 8x16, wx[0,1]=2π y wy[1,0]=2π)",
                        "docs/design/research/native-parallax/np-m0-results.md §5 (2π·fftfreq = rad/muestra vs rad/unidad de textura)",
                    ],
                    "corpus_relevance": {
                        "m3_cohort_a": "31/31 assets 1024x1024 (CUADRADOS) → v1 ≡ v2 exactamente",
                        "m2_manifest": "3/34 no cuadrados (Concrete035 2048x1024, WoodFloor043 2048x1024, PavingStones054 1365x2048) pero load_asset los reescala a 512² ANTES del solver",
                    },
                },
                "why_not_proven": (
                    "El sintético rectangular sólo demuestra que cada contrato reconstruye mejor "
                    "el sintético construido BAJO ESE contrato (se verificó la simetría exacta: "
                    "bajo UV_NORMALIZED el resultado se invierte). El repo NO declara en ningún "
                    "manifest, test o doc que los normal maps del corpus sean de texel isótropo; "
                    "el único contrato declarado y congelado por test es UV_NORMALIZED. Sin esa "
                    "premisa independiente, la clasificación es NOT_PROVEN, no CONFIRMED."
                ),
                "H2_IMPACT_ON_M4_M5": "NONE (corpus 100% cuadrado; el efecto sólo existe para W≠H)",
            },
            "H3": {
                "claim": "El techo SELF-Q8 depende de la escala de pendiente c; c=1 sería mucho más empinada que un normal authored típico, así que la conclusión de M4 sobre Q8 valdría sólo para ese régimen.",
                "H3_MECHANISM": "CONFIRMED",
                "H3_CONTRACTUAL_MEANINGFULNESS": "SUPPORTED",
                "H3_REAL_CORPUS_IMPACT": "NOT_DECISION_CHANGING (dirección CONSERVADORA)",
                "evidence_synthetic": {
                    "max_rmse_ratio_across_cases": a["h3"]["summary"]["max_rmse_ratio_across_cases"],
                    "c_values": [1.0, 0.3, 0.1, 0.03, 0.01, 0.003],
                    "self_float_aligned_rmse_machine_eps": True,
                },
                "evidence_real": {
                    "continuous_best_strength_median_abs": 0.016,
                    "n_continuous_strength_below_grid_floor": c["grid_oracle_signature"]["n_continuous_strength_below_floor"],
                    "m4_self_q8_rmse_median_historical": 0.0037464634293201122,
                    "m4_delta_rmse_median_historical": 0.03529285034519526,
                },
                "reasoning": (
                    "c=1 SÍ es el contrato: SELF se deriva del height almacenado sin reescalar y "
                    "los umbrales T_SELF_RMSE=0.10 / T_DELTA_RMSE=0.02 se calibraron en el mismo "
                    "régimen (NP-M0 §E1). El contrafactual real muestra que el AUTH del corpus "
                    "vive a |s*|≈0.005–0.05, es decir 20–200× más plano que c=1. En esa dirección "
                    "el SELF-Q8 de c=1 SOBREESTIMA el error de cuantización ⇒ self_rmse inflado ⇒ "
                    "delta_rmse DEFLACTADO ⇒ C2 se vuelve MÁS difícil de pasar. El resultado real "
                    "(C1∧C2 verdaderos con margen amplio) es por lo tanto conservador."
                ),
            },
            "H4": {
                "claim": "resize_normal re-cuantiza a uint8 con truncado y vuelve a cuantizar en el resize de Pillow modo L; es el hermano que el fix #653 dejó intacto.",
                "H4_IMPLEMENTATION_DEFECT": "CONFIRMED",
                "evidence_synthetic": {
                    "flat_normal_nx_bias_old": a["h4_synthetic"]["cases"]["flat_normal"]["flat_bias_nx_old"],
                    "flat_normal_nx_bias_new": a["h4_synthetic"]["cases"]["flat_normal"]["flat_bias_nx_new"],
                    "max_downstream_delta_rmse_probe": h4p["summary"]["max_abs_downstream_aligned_delta"],
                    "max_normal_rmse_old_vs_new_probe": h4p["summary"]["max_normal_rmse_old_vs_new"],
                    "max_downstream_ratio_probe": h4p["summary"]["max_downstream_aligned_ratio"],
                    "t_delta_rmse": 0.02,
                },
                "evidence_real": {
                    "m4_decision_old": c2["m4"]["decision_old"],
                    "m4_decision_new": c2["m4"]["decision_new"],
                    "m4_decision_changed": c2["m4"]["decision_changed"],
                    "m4_delta_rmse_median_old": c2["m4"]["full_old"]["delta_rmse_median"],
                    "m4_delta_rmse_median_new": c2["m4"]["full_new"]["delta_rmse_median"],
                    "m4_auth_rmse_max_abs_change": c2["m4"]["auth_rmse_max_abs_change"],
                    "m4_auth_rmse_change_median": 8.067e-05,
                    "m4_auth_rmse_change_p90": 4.954e-04,
                    "n_assets_change_gt_0_01": 1,
                    "m5_C1_old": c2["m5"]["C1_old"],
                    "m5_C1_new": c2["m5"]["C1_new"],
                    "m5_excess_lowmid_median_old": c2["m5"]["medians_old"]["excess_lowmid_nrmse"],
                    "m5_excess_lowmid_median_new": c2["m5"]["medians_new"]["excess_lowmid_nrmse"],
                    "m5_high_enrichment_median_old": c2["m5"]["medians_old"]["high_enrichment"],
                    "m5_high_enrichment_median_new": c2["m5"]["medians_new"]["high_enrichment"],
                },
                "H4_M4_PRIMARY_IMPACT": "NUMERICAL_NOT_DECISIONAL",
                "H4_M5_PRIMARY_IMPACT": "NUMERICAL_NOT_DECISIONAL",
                "reasoning": (
                    "Defecto de implementación REAL (sesgo de −0.5 LSB: la normal (0,0,1) sale "
                    "con nx=−0.00392; truncado por astype(uint8); re-cuantización en el resize "
                    "modo L). Pero con UNA sola variable cambiada: la decisión de M4 y sus reglas "
                    "C1/C2 quedan idénticas, la mediana de delta_rmse se mueve 2.0e-5 y el cambio "
                    "por asset es 8.1e-5 (mediana) / 5.0e-4 (p90) / 3.5e-2 (máximo, 1 asset de 31). "
                    "En M5, C1 y los dos componentes evaluables de C2 no cambian: los valores "
                    "quedan lejos de los umbrales en AMBOS brazos (excess_lowmid 0.51→0.45 vs "
                    "T=0.10; high_enrichment 0.75→0.80 vs T=2.0). Numérico, no decisional."
                ),
            },
            "H5": {
                "claim": "fd_forward deriva por muestra y el solver por unidad UV; el control FD-Q8 mide un cambio de régimen de cuantización, no la discretización FD.",
                "H5_DIAGNOSTIC_DEFECT": "CONFIRMED",
                "H5_PRIMARY_M4_BLOCKER": "NO",
                "evidence_synthetic": {
                    "float_ratio_max": a["h5"]["summary"]["float_ratio_max"],
                    "q8_ratio_max": a["h5"]["summary"]["q8_ratio_max"],
                    "q8_ratio_range": "0.063–5.94 (signo inconsistente entre casos)",
                },
                "evidence_call_site": {
                    "usage": "run_exp_m4.py:163,381 — sólo report['summary']['secondary']['fd_q8_diagnostic'] = path_medians('fd_q8', rows)",
                    "enters_C1_C2": False,
                    "enters_decision": False,
                    "docstring": "fd_forward: 'NUNCA reemplaza al par matched como primario: mide el mismatch FD↔espectral del mismo contenido'",
                },
                "reasoning": (
                    "El defecto de unidades es real (fd_forward usa (roll diff)/2 = derivada por "
                    "índice de muestra; freq_axes usa rad por unidad UV). Pero en float el factor "
                    "constante es absorbido EXACTAMENTE por el fit afín global (ratio 1.000000000000007), "
                    "así que el control no mide unidades en el régimen float: en Q8 el ratio se "
                    "dispersa 0.063–5.94. Es un diagnóstico secundario, no alcanza C1/C2 ni la "
                    "decisión de M4, y M5 no usa fd_forward."
                ),
            },
        },
    }
    (o / "hypothesis-status.json").write_text(json.dumps(hyp, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------- provenance
    prov = {
        "artifact": "native-parallax-h1-h5-falsification-impact-audit",
        "date_utc": "2026-10-07",
        "scope": "AUDIT_ONLY",
        "base_sha": BASE_SHA,
        "branch": BRANCH,
        "worktree": WORKTREE,
        "external_audit": {
            "commit": EXTERNAL_AUDIT_COMMIT,
            "path": "docs/audits/2026-10-07_native_parallax_math_architecture_audit.md",
            "blob_sha256": "ce647d7a75e3f524c4898d045b8855f66779782829de9e881811abf633b85ff2",
            "treatment": "HIPOTESIS — leído con git show, sin cherry-pick, sin basar el worktree en esa rama",
        },
        "execution": {
            "python": "3.11.9",
            "numpy": "2.4.6",
            "pillow": "11.3.0",
            "fft_backend": "numpy.fft",
            "interpreter": r"E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe",
        },
        "module_blobs_sha256_lf_at_base": {
            "trust_proxies.py": "8f5a0703343b88ea29a0bf71d09c95b96e3f4d5994bf32a831ec3ea5fc37981e",
            "normal_from_height.py": "f8370696bae46c7c01d9858c6cc50fd6e24a6174f3bba8d9c7fde4bc7ad66191",
            "normal_fft_periodic.py": "994f4ebfead14e8970b25f053ef951bca83079d6cfd1fb796252db4ec3dd4306",
            "authored_dataset.py": "4184cbada6885cf62ef98a95100a7b1921a881b8e2f7450f2889e9337befde8c",
            "solver_coherence.py": "0d0bdf21a30988228c3e506211b239f9e795ec08a52c22e67b5bac2e832554ab",
            "run_exp_m4.py": "ecf78ee4fb18d682844cfb4b1559662fc3ac910c49ca4bfa56c93054d9872e19",
            "run_exp_m5.py": "6040f27cdbc233c18997ab9b5f348c0261213b8979f2ff405dd7908a5f61696a",
            "run_exp_m3.py": "7c809e549a39d7907a4e5391fa6ffb544c1ad396ca61c94d7204ee9dd5486325",
            "frequency_coherence.py": "b7144da781e049a29061c677d3008de9c67cc83b47702f22ea976f3ba20c9a4e",
        },
        "artifacts_read_only_sha256": {
            "docs/design/research/native-parallax/data/exp-m3-clean-authored-manifest.json": "c9c1665942281966ddeb4f4ff05ed9e2be302d80155cfa8bfc1e487c2bfbecde",
            "docs/design/research/native-parallax/data/exp-m2-authored-manifest.json": "2cbac0f2272937d99747504a09e154fff666f8f721fb2cccad495211d8b40efe",
            "data/exp-m4-results.json": "2f56f863709f31567c720921719e835727729e8b0182907f9a0a5bb703e19862",
            "docs/validation/native-parallax-revalidation-20260928/exp_m4_results.json": "de172bc59ae4cdba755333c585913245b301c7fa0fc4514a8c30639e807577d9",
        },
        "historical_artifacts_mutated": False,
        "corpus_mutated": False,
        "corpus_root_read_only": r"C:\SkyClawResearch\NativeParallax\EXP-M3",
        "scientific_code_mutated": False,
        "thresholds_changed": False,
        "m4_m5_rerun_executed": False,
        "m6_real_run_executed": False,
        "scripts": [
            "scripts/phase_a_synthetic.py",
            "scripts/phase_b_units.py",
            "scripts/h4_magnitude_probe.py",
            "scripts/phase_c_real_impact.py",
            "scripts/phase_c2_h4_corpus.py",
            "scripts/build_evidence.py",
        ],
    }
    (o / "provenance.json").write_text(json.dumps(prov, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("OK -> synthetic-evidence.json, real-impact.json, hypothesis-status.json, provenance.json")


if __name__ == "__main__":
    main()
