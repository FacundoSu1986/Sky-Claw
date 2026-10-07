# Adjudicación de impacto — H1–H5 sobre M3/M4/M5

> **Base:** `97dcc7ab9ade29153faa0ccec428b78612cfc04a` · **Carácter:** AUDIT_ONLY
> **No se reranearon M2/M3/M4/M5. No se cambió ningún umbral. No se tocó código científico.**

## 0. Regla central

El audit externo mezcla dos preguntas distintas. Este documento las separa en **todo** momento:

```
IMPLEMENTATION_DEFECT  ≠  DECISION_IMPACT
```

Un defecto de instrumentación puede ser **real** y aun así **no cambiar ninguna decisión**, y
viceversa. Sólo cuentan como impacto los caminos que **alimentan** `evaluate_rules` / `decide`.

## 1. Superficies de decisión (call graph verificado)

| Experimento | Función de decisión | Insumos que la alimentan |
|---|---|---|
| **M3** | `run_exp_m3.decide_exp_m3` | Cohort A: `aligned_rmse`, `correlation`, `variance_ratio` + proxies seleccionados en CALIBRATION. **Cohort B es diagnóstico (`EVALUATION_DIAGNOSTIC_ONLY`) y NO entra.** |
| **M4** | `solver_coherence.evaluate_rules` → `decide` | `rmse_self_median`, `abs_corr_self_median`, `var_self_median`, `delta_rmse_median`, `Δabs_corr` (full + held-out). |
| **M5** | `frequency_coherence.evaluate_rules` → `decide` | `auth_lowmid_nrmse`, `excess_lowmid_nrmse`, `excess_high_nrmse`, `high_enrichment` + réplica LEGACY_HELDOUT. |

Campos **explícitamente no decisionales**: `coherence_agreement_deg` / `coherence_diagnostic`
(M4 §5, «NO es trust proxy ni criterio de exclusión») y `summary.secondary.fd_q8_diagnostic` (M4).

## 2. Blast radius

| Hallazgo | Alcanza la decisión primaria | Alcanza algún diagnóstico | Veredicto |
|---|---|---|---|
| **H1** oráculo de strength en rejilla finita | **NO** | SÍ — `M3 Cohort B membership`, `M4 coherence_diagnostic §5` | `CONFIRMED` (defecto de instrumentación) |
| **H2** contrato de unidades | **NO** | **NO** | `NOT_PROVEN` + inobservable (corpus 100 % cuadrado) |
| **H3** escala `c` del techo SELF-Q8 | **NO** (dirección conservadora) | SÍ — interpretación del piso Q8 | `CONFIRMED` (mecanismo) |
| **H4** `resize_normal` uint8 | SÍ (camino AUTH) — **medido** | SÍ | `CONFIRMED` (defecto) / `NUMERICAL_NOT_DECISIONAL` (impacto) |
| **H5** unidades de `fd_forward` | **NO** | SÍ — `fd_q8_diagnostic` | `CONFIRMED` (defecto) / `H5_PRIMARY_M4_BLOCKER=NO` |

## 3. H1 — oráculo con rejilla finita

**Defecto CONFIRMADO.** `OracleOnly.normal_height_residual_oracle` (`trust_proxies.py:296`)
minimiza sobre `concat(linspace(0.05,5.0,25), -linspace(0.05,5.0,25))`: 50 puntos, paso 0.20625,
piso 0.05.

Sintético (réplica del oráculo verificada: `oracle_replica_max_abs_diff = 0.0`; la forma cerrada
recupera la strength verdadera con error ≤ 1.1e-16): para pares **perfectamente coherentes**
(ángulo real 0°), la rejilla reporta hasta **61.95°** (S14_steep, s=0.003), mediana de delta
**4.18°**. Con `s` exactamente en la rejilla (`-0.05`) el reporte es 0°.

**Real (corpus M3 Cohort A, 31 assets, SHA256 verificado, 0 exclusiones):**

| Métrica | Rejilla (código actual) | Continuo (forma cerrada + refinamiento) |
|---|---|---|
| `best_strength` | **31/31 clavados en el piso `-0.05`** | 27/31 con `|s*| < 0.05` (mediana ≈ 0.016) |
| mediana de agreement | **10.71°** | **1.68°** |
| Spearman vs `delta_rmse` | 0.135 | 0.480 |

Es decir: **para los 31 assets**, el oráculo nunca encuentra el óptimo — el argmin está en el
borde de la rejilla. El diagnóstico §5 pasa de «13.40° (mediana histórica) / 10.71° (baseline)»
a «1.68°»: el desacuerdo normal↔height que el diagnóstico reportaba es, en su mayor parte, un
**artefacto de resolución del instrumento**, no una propiedad del dataset.

**Pero no cambia ninguna decisión:** `evaluate_rules` de M4 no lee el oráculo, y `decide_exp_m3`
no lee Cohort B. Por lo tanto:

```
H1_M3_COHORT_B_MEMBERSHIP_CHANGED = NOT_COMPUTABLE (corpus EXP-M2 ausente en C:\SkyClawResearch\)
H1_M3_FINAL_DECISION_PATH_REACHED = NO
H1_M4_COHERENCE_DIAGNOSTIC_CHANGED = YES
H1_M4_C1_C2_PATH_REACHED          = NO
H1_M3_PRIMARY_IMPACT              = NONE
H1_M4_PRIMARY_IMPACT              = NONE
```

> Nota de honestidad: el `coherence_agreement_deg` **histórico** (`data/exp-m4-results.json`,
> mediana 13.398°) **no se reproduce** en el baseline (mediana recalculada 10.709°; diff máximo
> por asset 23.24°). El artefacto se produjo en `fe54e9a9`, que **no es ancestro** de `97dcc7ab`,
> y la ruta del oráculo cambió en PR-MATH-A (#681). Por eso la comparación válida es
> **rejilla vs continuo, ambas recalculadas en el baseline**, no contra el número histórico.

## 4. H3 — escala del techo SELF-Q8

**Mecanismo CONFIRMADO.** Con el mismo height y variando sólo `c`:

| caso | `c=1.0` | `c=0.01` | ratio max/min |
|---|---|---|---|
| S06_multifreq | 3.88e-4 | 8.01e-6 | 48× |
| S07_bumps | 2.82e-4 | 2.38e-5 | 24× |
| S09_bricks | 3.50e-2 | 1.01e-5 | **3462×** |
| S14_steep | 7.94e-2 | 1.83e-5 | **4339×** |

El path float es exacto (`self_float_aligned_rmse ≈ 1e-16`): todo el techo es cuantización.

**Contractualmente significativo, sí — pero la dirección es CONSERVADORA.**
`c = 1` **es** el contrato: SELF se deriva del height almacenado sin reescalar, y los umbrales
`T_SELF_RMSE = 0.10` / `T_DELTA_RMSE = 0.02` se calibraron en ese mismo régimen (NP-M0 §E1). El
contrafactual real muestra que el AUTH del corpus vive a `|s*| ≈ 0.005–0.05`, es decir **20–200×
más plano** que `c = 1`. En esa dirección, el SELF-Q8 de `c = 1` **sobreestima** el error de
cuantización ⇒ `self_rmse` inflado ⇒ `delta_rmse = auth − self` **deflactado** ⇒ C2 se vuelve
**más difícil** de pasar. El resultado real (C1 ∧ C2 verdaderos con margen amplio: delta 0.0353
vs umbral 0.02; Δcorr 0.160 vs 0.10) es, por lo tanto, **conservador**.

```
H3_MECHANISM                 = CONFIRMED
H3_CONTRACTUAL_MEANINGFULNESS = SUPPORTED   (c=1 es el contrato y el régimen de calibración)
H3_REAL_CORPUS_IMPACT        = NOT_DECISION_CHANGING (dirección conservadora)
```

## 5. H4 — `resize_normal` (finding prioritario)

### 5.1 Defecto de implementación: CONFIRMADO

`authored_dataset.resize_normal` hace `(clip(n,-1,1)*127.5+127.5).astype(np.uint8)` — **trunca** —
y luego reescala con Pillow modo `L` (otra cuantización a 8 bits). Es el hermano que el fix #653
arregló para `resize_height` y dejó intacto para el normal.

Sesgo medido: una normal **plana** `(0,0,1)` sale con `mean(nx) = -0.00392` (el contrafactual
float da `0.0`). Es el offset de −0.5 LSB clásico del round-trip `[-1,1] → uint8 → [-1,1]`.
Sólo el camino **AUTH** pasa por acá (SELF se arma desde el height float).

### 5.2 Magnitud sintética — el número del audit externo NO se reproduce

El audit afirma «5–21× mayor … y llega a 0.023 absoluto». `scripts/h4_magnitude_probe.py` barre
7 casos × 4 amplitudes a 1024→512 y mide todas las candidatas:

| métrica | máximo medido |
|---|---|
| `normal_rmse_old_vs_new` | 0.01755 |
| `|Δ downstream aligned rmse|` | **0.01617** (= 81 % de `T_DELTA_RMSE`) |
| ratio downstream old/new | **1.201×** (no 5–21×) |
| casos con Δ ≥ 0.02 | **0** |

El ratio 5–21× **no se reproduce**. El orden de magnitud sí: el peor caso (S09_bricks, contenido
con creases) llega a 81 % de `T_DELTA_RMSE`.

### 5.3 Decisión de gate (explícita)

§11/§12 condicionan el contrafactual sobre corpus a «efecto material». El sintético da
0.0162 < 0.02 (no material por la letra) pero **81 % del umbral** y el propio audit lo describe
como «del orden de `T_DELTA_RMSE`». **Se ejecutó el contrafactual READ-ONLY**, con una sola
variable, porque declarar inmaterial sin medirlo habría sido una afirmación no soportada en la
dirección «no hay problema» — justo lo que §21 manda buscar.

### 5.4 Contrafactual sobre corpus real (31 assets, una sola variable)

Réplica exacta de `load_asset` cambiando **sólo** el resizer del normal. Height, solver,
convención (DIRECTX), alineamiento afín global, resolución (512), split y umbrales: idénticos.

**M4:**

| | AUTH old (uint8) | AUTH new (float) |
|---|---|---|
| decisión | `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT` | `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT` |
| C1 / C2 | true / true | true / true |
| `rmse_self_median` | 0.0036592 | 0.0036592 (idéntico) |
| `rmse_auth_median` | 0.0392110 | 0.0390915 |
| `delta_rmse_median` | 0.0350632 | 0.0350835 |
| `abs_corr_auth_median` | 0.83918 | 0.83973 |
| `var_auth_median` | 0.70422 | 0.70515 |

Cambio por asset en `auth_rmse`: mediana **8.07e-5**, p90 **4.95e-4**, máximo **3.54e-2**
(un único asset de 31 supera 0.01: `ambientcg_Tiles075`, 0.17668 → 0.14126). **Decisión y reglas
no cambian.**

**M5:**

| | AUTH old | AUTH new | umbral |
|---|---|---|---|
| `C1_lowmid_preserved` | **false** | **false** | — |
| `auth_lowmid_nrmse` (mediana) | 0.55431 | 0.55621 | ≤ 0.15 |
| `excess_lowmid_nrmse` (mediana) | 0.50820 | 0.44682 | ≤ 0.10 |
| `excess_high_nrmse` (mediana) | 0.33504 | 0.33491 | — |
| `high_enrichment` (mediana) | 0.75417 | 0.80344 | ≥ 2.0 |
| C2: high > lowmid / enrichment ≥ T | false / false | false / false | — |

Los valores quedan **lejos de los umbrales en ambos brazos** (excess_lowmid 5× por encima de su
umbral; high_enrichment 2.5× por debajo del suyo). Numéricamente material en un componente
(0.061 de cambio en `excess_lowmid`), **decisionalmente inerte**.

```
H4_IMPLEMENTATION_DEFECT = CONFIRMED
H4_M4_PRIMARY_IMPACT     = NUMERICAL_NOT_DECISIONAL
H4_M5_PRIMARY_IMPACT     = NUMERICAL_NOT_DECISIONAL
M4_M5_REVALIDATION_REQUIRED_BY_H4 = NO
```

## 6. H5 — unidades de `fd_forward`

**Defecto CONFIRMADO.** `fd_forward` deriva por **muestra** (`(roll(h,-1)-roll(h,1))/2`), mientras
`freq_axes` deriva por **unidad UV**. Son convenciones distintas.

**Pero el control no mide eso.** En float, `fd_current` y `fd_units` dan RMSE alineado
**idéntico** (`ratio = 1.000000000000007`): el factor constante `S` es absorbido **exactamente**
por `OracleOnly.fit_global_scale`. En Q8 el ratio se dispersa entre **0.063 y 5.94** (signo
inconsistente entre casos): lo que el control FD-Q8 mide es la **interacción unidad↔cuantización**,
no la discretización FD.

**No es blocker de nada:** `fd_forward` se usa sólo en `run_exp_m4.py:163,381`
(`summary.secondary.fd_q8_diagnostic`); no entra en `evaluate_rules` ni en `decide`; M5 no lo usa.
El propio docstring lo declara: «NUNCA reemplaza al par matched como primario».

```
H5_DIAGNOSTIC_DEFECT  = CONFIRMED
H5_PRIMARY_M4_BLOCKER = NO
```

## 7. Decisión final M4 / M5

**Causalmente, ninguna de las cinco hipótesis alcanza una decisión primaria:**

- H1 → sólo el diagnóstico de coherencia de M4 y la pertenencia (diagnóstica) a Cohort B de M3.
- H2 → inobservable: el corpus primario es 100 % cuadrado.
- H3 → mecanismo real, pero empuja **a favor** de la conclusión vigente (conservadora).
- H4 → alcanza la decisión de M4/M5 y se **midió**: decisión y reglas idénticas, componentes de
  C1/C2 de M5 idénticos y lejos de umbral.
- H5 → diagnóstico secundario.

```
M4_PRIMARY_STATUS = NOT_INVALIDATED
M5_PRIMARY_STATUS = NOT_INVALIDATED
M4_M5_REAL_RERUN_EXECUTED = NO
M6_REAL_RUN_EXECUTED      = NO
```

**Residual declarado (no ocultado):** el impacto de H4 está acotado por el contrafactual
READ-ONLY de una sola variable sobre los 31 assets; no hay re-ejecución de M4/M5 con artefactos
nuevos, y el contrafactual de M5 no incluye el término de réplica LEGACY_HELDOUT (no hay artefacto
de filas M5 en el repo). La decisión no cambia en ninguna de las superficies medidas.

## 8. Relación con PR #697

#697 pregunta **si REVAL-1 invalidó M4/M5**. Este audit pregunta **si había otros defectos
previos**. Son preguntas distintas y no se mezclan.

#697 concluyó `NOT_INVALIDATED` con base en que los cambios posteriores al freeze histórico no
alcanzan la decisión primaria. Este audit **confirma esa conclusión por otra vía**: ninguno de
los cinco defectos de instrumentación que el audit externo postuló alcanza una decisión primaria
—y uno de ellos (H4, el único que sí alcanza el camino AUTH) se midió y no mueve la decisión.

```
PR697_STATE = OPEN_DRAFT
PR697_CHANGED_BY_THIS_SLICE = NO
PR697_DISPOSITION = STILL_VALID_NARROW_SCOPE
```

`STILL_VALID_NARROW_SCOPE` y no `NEEDS_AMENDMENT` porque #697 nunca afirmó que no existieran otros
defectos: su alcance era la relación REVAL-1 → M4/M5, y ese alcance sigue siendo correcto.
`SUPERSEDED_BY_NEW_AUDIT` tampoco aplica: las preguntas son ortogonales.

## 9. Relación con PR #675

```
PR675_STATE = OPEN_DRAFT
PR675_CHANGED_BY_THIS_SLICE = NO
PR675_RECOMMENDATION = KEEP_DRAFT_BLOCKED
```

**Cambio respecto de #697** (que había recomendado `READY_FOR_DESIGN_RECONCILIATION`): este audit
deja #675 en `ON_HOLD` porque (a) el slice se abrió explícitamente poniendo esa recomendación en
espera hasta terminar el audit; y (b) H4 **sí** es un defecto real en el camino primario AUTH,
aunque medido como no decisional. Antes de desbloquear el diseño conviene decidir si el fix de
`resize_normal` entra en el alcance de la reconciliación. **No se editó #675, no se re-stackeó,
no se mergeó, no se cerró.** `M6_IMPLEMENTATION_BLOCKED = YES`.

## 10. Revisión adversarial (§21)

Revisión independiente buscando específicamente los ocho modos de falla listados en el brief:

| Modo de falla buscado | Hallazgo |
|---|---|
| **Supuestos circulares en el sintético H2** | **CONFIRMADO como riesgo** — se demostró la simetría exacta (mismo grid, resultado invertido según el contrato). Por eso `H2_BUG_CLASSIFICATION = NOT_PROVEN`. No se usó como prueba. |
| **Confusión de escala en H3** | Acotada: la conclusión no se apoya en el sintético sino en la dirección del efecto (conservadora) y en el régimen real medido (`|s*|≈0.005–0.05`). |
| **Contaminación del contrafactual H4** | Una sola variable (el resizer). Verificado que `rmse_self_median` es bit-idéntico entre brazos, lo que prueba que nada más cambió. |
| **Fuga de umbrales (threshold leakage)** | Ningún umbral fue leído, escrito ni retuneado; `thresholds_unchanged` se emite en `real-impact.json`. |
| **Artefacto histórico equivocado** | **CONFIRMADO y corregido** — `data/exp-m4-results.json` (fe54e9a9) no se reproduce en el baseline; la adjudicación de H1 usa rejilla vs continuo **ambos** recalculados en `97dcc7ab`. Documentado en `real-impact.json`. |
| **Confusión decisión vs diagnóstico** | Núcleo del documento (§1, §2). H1 y H5 se adjudican `NONE`/`NO` precisamente por esto. |
| **Mutación de corpus** | 0 exclusiones en ambos contrafactuales ⇒ los 31 SHA256 coinciden con el manifest. `corpus_mutated = false`. |
| **Retuneo post-hoc** | Ninguno: el script de Fase C no importa ni escribe umbrales; sólo lee constantes para reportarlas. |
| **Invalidación injustificada de M4/M5** | No se invalidó nada. `NOT_INVALIDATED` con residual declarado. |
| **Desbloqueo prematuro de M6** | No: `M6_IMPLEMENTATION_BLOCKED = YES`, `PR675_RECOMMENDATION = KEEP_DRAFT_BLOCKED`. |

**Hallazgo propio adicional (no pedido):** el audit externo afirma que «M3+ rechaza no cuadradas».
Es **impreciso**: `load_cohort_a` no tiene gate de cuadradas. El corpus primario es cuadrado por
**composición del manifest**, no por rechazo del código. La conclusión del audit (M4/M5 no
afectados por H2) sobrevive, pero la razón declarada no.

```
ARENA_REVIEW = PASS_WITH_FINDINGS
```

## 11. Hard stops (§23) — verificación

| Condición de STOP | Estado |
|---|---|
| corpus hash mismatch | **NO** — 0 exclusiones, 31/31 SHA256 coinciden |
| historical artifact mutation | **NO** — `historical_artifacts_mutated = false` |
| need to change threshold | **NO** — `thresholds_changed = false` |
| need to patch scientific code | **NO** — `scientific_code_mutated = false` |
| unit contract unresolved but implementation attempted | **NO** — H2 resuelto como `UV_NORMALIZED`; `normal_fft_periodic_v2` NO implementado |
| counterfactual changes more than one variable | **NO** — un solo resizer por contrafactual |
| decision impact cannot be causally isolated | **NO** — H4 aislado y medido; H1/H3/H5 no alcanzan la decisión |

```
BLOCKER = NONE
```
