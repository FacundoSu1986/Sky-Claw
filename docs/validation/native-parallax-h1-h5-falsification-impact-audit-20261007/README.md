# Native Parallax — Auditoría de falsificación e impacto H1–H5

> **Fecha:** 2026-10-07 · **Base:** `97dcc7ab9ade29153faa0ccec428b78612cfc04a`
> **Rama:** `research/native-parallax-h1-h5-falsification-impact-audit`
> **Worktree:** `E:\SkyClaw_H1H5_AUDIT_97dcc7ab`
> **Carácter:** `AUDIT_ONLY` — sin fixes, sin reruns, sin cambios de umbral.
>
> **Entrada externa tratada como HIPÓTESIS, no como verdad:**
> `dcc6551ef5afb2dfcab1b8662d1eafedc8b7d133` → `docs/audits/2026-10-07_native_parallax_math_architecture_audit.md`
> (leída con `git show`; sin cherry-pick; el worktree NO se basó en esa rama).

## 1. Qué se hizo

| Fase | Contenido | Script |
|---|---|---|
| **A** | Falsificación sintética H1 / H3 / H4 / H5 | `scripts/phase_a_synthetic.py` |
| **B** | Contrato de unidades H2 (reconstrucción + sonda de circularidad) | `scripts/phase_b_units.py` |
| **A′** | Sonda de magnitud H4 (7 casos × 4 amplitudes) | `scripts/h4_magnitude_probe.py` |
| **C** | Contrafactual READ-ONLY del oráculo H1 sobre el corpus real | `scripts/phase_c_real_impact.py` |
| **C2** | Contrafactual READ-ONLY de `resize_normal` (H4) sobre M4 y M5 | `scripts/phase_c2_h4_corpus.py` |
| **D** | Adjudicación y consolidación de evidencia | `scripts/build_evidence.py` |

El orden del brief se respetó: nunca se empezó por el corpus. El corpus se usó **sólo** cuando A/B
lo justificaron, y siempre READ-ONLY con verificación de SHA256 previa (0 exclusiones ⇒ los 31
assets coinciden con el manifest commiteado).

## 2. Resultado por hipótesis

| # | Pregunta | Veredicto | Alcanza la decisión primaria |
|---|---|---|---|
| **H1** | ¿Un par height↔normal coherente puede dar disagreement ≠ 0 sólo por la rejilla de strength? | **CONFIRMED** — hasta 61.95° en sintético; en el corpus real **31/31** assets con el `best_strength` clavado en el piso `-0.05` | **NO** (sólo diagnóstico §5 de M4 y Cohort B de M3) |
| **H2** | ¿El solver asume tile cuadrado físico y sesga texturas no cuadradas? | **NOT_PROVEN** — observación reproducida, pero el sintético es **circular** y el contrato implementado es `UV_NORMALIZED` (congelado por test) | **NO** (corpus 100 % cuadrado) |
| **H3** | ¿El techo SELF-Q8 depende de una escala arbitraria `c`? | **CONFIRMED** (mecanismo; ratio hasta 4339×) — pero la dirección es **conservadora** | **NO** |
| **H4** | ¿`resize_normal` re-cuantiza a uint8 con truncado? | **CONFIRMED** (sesgo −0.5 LSB: normal plana → `nx = −0.00392`) | **SÍ — y se midió: no cambia la decisión** |
| **H5** | ¿`fd_forward` mide discretización FD o cuantización? | **CONFIRMED** — mide la interacción unidad↔cuantización | **NO** (diagnóstico secundario) |

Detalle causal completo en [`decision-impact.md`](decision-impact.md); contrato de unidades en
[`unit-contract.md`](unit-contract.md).

## 3. Los dos hallazgos que más importan

**H1 — el diagnóstico de coherencia de M4 es, en su mayor parte, un artefacto del instrumento.**
Sobre los 31 assets del corpus primario, el oráculo de rejilla nunca encuentra el óptimo: el
`best_strength` queda clavado en el borde de la rejilla (`-0.05`) en **31/31** casos, y 27/31
tienen `|s*| < 0.05`. El contrafactual continuo baja la mediana de **10.71° → 1.68°**. El
«NORMAL_HEIGHT_MISMATCH» que el diagnóstico reportaba es en gran medida un artefacto de
resolución del oráculo. **No cambia ninguna decisión** (`coherence_diagnostic` es
explícitamente no decisional), pero invalida el uso del número como evidencia de dataset.

**H4 — el defecto es real y toca el camino AUTH, pero no mueve la aguja.**
`resize_normal` trunca a uint8 y re-cuantiza en el resize de Pillow (el hermano que #653 arregló
para `resize_height`). Con **una sola variable** cambiada sobre los 31 assets reales: decisión de
M4 y reglas C1/C2 **idénticas**; `delta_rmse` mediana 0.0350632 → 0.0350835 (Δ 2.0e-5); cambio
por asset mediana 8.1e-5 / p90 5.0e-4 / máximo 3.5e-2 (1 asset de 31). En M5, C1 y los dos
componentes evaluables de C2 **no cambian** y los valores quedan lejos de los umbrales en ambos
brazos.

## 4. Estado final

```
H1_H5_AUDIT_STATUS = COMPLETE

BASE_SHA = 97dcc7ab9ade29153faa0ccec428b78612cfc04a
WORKTREE = E:\SkyClaw_H1H5_AUDIT_97dcc7ab
BRANCH   = research/native-parallax-h1-h5-falsification-impact-audit

H1_IMPLEMENTATION_DEFECT = CONFIRMED
H1_M3_PRIMARY_IMPACT     = NONE
H1_M4_PRIMARY_IMPACT     = NONE

H2_BUG_CLASSIFICATION = NOT_PROVEN
H2_UNIT_CONTRACT      = UV_NORMALIZED

H3_MECHANISM            = CONFIRMED
H3_REAL_CORPUS_IMPACT   = NOT_DECISION_CHANGING

H4_IMPLEMENTATION_DEFECT = CONFIRMED
H4_M4_PRIMARY_IMPACT     = NUMERICAL_NOT_DECISIONAL
H4_M5_PRIMARY_IMPACT     = NUMERICAL_NOT_DECISIONAL

H5_DIAGNOSTIC_DEFECT  = CONFIRMED
H5_PRIMARY_M4_BLOCKER = NO

M4_PRIMARY_STATUS = NOT_INVALIDATED
M5_PRIMARY_STATUS = NOT_INVALIDATED

M4_M5_REAL_RERUN_EXECUTED = NO
M6_REAL_RUN_EXECUTED      = NO

PR697_STATE = OPEN_DRAFT
PR697_CHANGED_BY_THIS_SLICE = NO
PR697_DISPOSITION = STILL_VALID_NARROW_SCOPE

PR675_STATE = OPEN_DRAFT
PR675_CHANGED_BY_THIS_SLICE = NO
PR675_RECOMMENDATION = KEEP_DRAFT_BLOCKED

ARENA_REVIEW = PASS_WITH_FINDINGS
BLOCKER = NONE
```

## 5. Residuales declarados

1. El contrafactual de H4 acota el impacto **por medición** (una variable, 31 assets), pero no
   re-ejecuta M4/M5 con artefactos nuevos; el contrafactual de M5 no incluye el término de
   réplica `LEGACY_HELDOUT` (no hay artefacto de filas M5 en el repo).
2. La pertenencia a **Cohort B de M3** no es recomputable: el corpus `EXP-M2` (34 assets) no está
   presente en `C:\SkyClawResearch\` (sólo existe `EXP-M3`). No afecta la decisión: Cohort B está
   etiquetada `EVALUATION_DIAGNOSTIC_ONLY` y `decide_exp_m3` no la lee.
3. El `coherence_agreement_deg` histórico (`data/exp-m4-results.json`, `fe54e9a9`) **no se
   reproduce** en el baseline (diff máximo 23.24° por asset): el artefacto precede a PR-MATH-A
   (#681) y `fe54e9a9` no es ancestro de `97dcc7ab`. Por eso H1 se adjudica comparando rejilla vs
   continuo **ambos recalculados en el baseline**.
4. H2 queda como deuda de convención: si algún día entra al corpus primario un asset con `W ≠ H`,
   el gate de cuadradas debería ser explícito y fail-closed en el loader, no implícito en la
   composición del manifest.

## 6. Contenido de este directorio

```
README.md                  este documento
hypothesis-status.json     veredicto por hipótesis + blast radius + evidencia citada
synthetic-evidence.json    Fase A + Fase B + sonda de magnitud H4 (datos crudos consolidados)
real-impact.json           Fase C + Fase C2 (contrafactuales READ-ONLY sobre corpus) + nota histórica
unit-contract.md           H2: reconstrucción del contrato de unidades
decision-impact.md         adjudicación causal, PR #697 / #675, review adversarial, hard stops
provenance.json            SHAs, blobs, hashes de artefactos leídos, entorno de ejecución
scripts/                   los 6 scripts de auditoría (todos AUDIT_ONLY / READ-ONLY)
```

**Inmutabilidad respetada:** no se modificó `data/exp-m4-*.json`, ni
`docs/validation/native-parallax-revalidation-20260928/**`, ni
`docs/validation/native-parallax-math-revalidation-20261006/**`, ni
`docs/validation/native-parallax-post-reval1-m4-m5-impact-audit-20261007/**`, ni
`C:\SkyClawResearch\**`. No se tocó código científico ni umbrales.
