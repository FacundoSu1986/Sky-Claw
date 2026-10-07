# REVAL-1 — M2/M3 versioned math revalidation — Impact Report

**Estado:** `REVAL1_EXECUTION_STATUS=COMPLETE`
**Protocolo / código de corrida:** `fc87b7e5191e42bd7e38b128b7baeaec7516dadb`
**Replay OLD:** `e23bf7ac75bf8ac7b1b80f1944119598f743b7b7` (`BEST_SUPPORTED_M2_BRANCH_HEAD_AT_HISTORICAL_RUN_WINDOW`,
`OLD_M2_EXECUTION_SHA_FULL_VERIFIED=NO`)
**RAW_RUN_ROOT:** `C:\SkyClawResearch\NativeParallax\EXP-M3\runs\math-revalidation-20261006T223304Z-fc87b7e5`

> **Corregido tras Arena review adversarial** (`ARENA_VERDICT=PASS_WITH_FINDINGS`).
> Cambios respecto de la primera versión: §3.2c magnitudes reales de `oracle_best_strength`
> (antes se citaban valores del docstring del código, no medidos); §4 reescrita — el
> `rates_sweep@30deg` **no** es comparable punto a punto (proxies distintos) y se agrega el
> hallazgo de cambio de membresía de la cohorte diagnóstica; §6 nueva (Arena).

---

## 0. Resumen ejecutivo

La conclusión científica de M3 **sobrevive**: `M3_DECISION_EQUIVALENT=YES`
(`EXP_M3_RECONSTRUCTION_CONDITIONAL` en OLD y en NEW). No hay cambios inesperados:
`UNEXPECTED_UNEXPLAINED_CHANGES=0`.

El hallazgo principal **no** estaba en el guion del protocolo. De las tres fuentes que el
brief anticipa (MATH-A, MATH-B, REPRO-A) más el environment, la causa **dominante** de los
deltas M2 es una **cuarta** que el brief no nombra: la corrección de **fidelidad del
height** en `authored_dataset.resize_height` (de cuantización uint8 a float32). Aporta
**4 564 de 5 456 celdas** (83,6 %) de los cambios materiales en la superficie B.

> Las superficies B (`OLD_REPLAY vs NEW`) y C (`HISTORICAL vs NEW`) dan **exactamente el
> mismo conteo** porque HIST ≈ REPLAY (drift 0, §2). Todos los conteos de este informe se
> refieren a la superficie **B**; sumar B+C duplicaría. El agregado del
> `taxonomy-summary.json` sí suma ambas y va rotulado como tal.

La segunda sorpresa es que **MATH-A §13 (factor geométrico) es inobservable en M2/M3**:
`decode_gradients_policy` y `gradients_from_normal` se invocan con `sx = sy = 1.0`, donde
`-sx·nx/nz ≡ -nx/(sx·nz)`. La corrección está bien fundada, pero este slice no la ejerce.
Lo que sí cambia por MATH-A son las **features** (`curl_*`, `projection_residual_*`), el
**oráculo normal↔height** (§3, §15, §19) y, como consecuencia, la **membresía de la cohorte
diagnóstica** (§4.2).

---

## 1. Gates

| Gate | Resultado |
|---|---|
| `POST_FREEZE_NATIVE_PARALLAX_DRIFT` | `NO` (diff `fc87b7e5..origin/main` vacío en el subárbol) |
| `NEW_WORKTREE_SHA` / `CLEAN` | `fc87b7e5…` / `YES` |
| `OLD_REPLAY_WORKTREE_SHA` / `CLEAN` | `e23bf7ac…` / `YES` |
| `OLD_IMPORT_SOURCE_VERIFIED` | `YES` (importa desde `E:\SkyClaw_REVAL1_OLD_e23bf7ac`) |
| `NEW_IMPORT_SOURCE_VERIFIED` | `YES` (importa desde `E:\SkyClaw_REVAL1_NEW_fc87b7e5`) |
| `PATH_PREFLIGHT` | `PASS` (8/8 rutas) |
| `PRE_RUN_M2_HASH_MATCH` | `68/68` |
| `PRE_RUN_M3_HASH_MATCH` | `62/62` |
| `HISTORICAL_*_SHA256` | los 4 coinciden exactamente |
| `NEW_M2_STRICT_JSON` / `NEW_M3_STRICT_JSON` | `PASS` / `PASS` |
| `NO_CLOBBER_OUTPUT` | `PASS` (`mkdir(exist_ok=False)`) |
| Validación NEW pre-run | 210 tests `passed`; `ruff check` + `ruff format --check` limpios |

---

## 2. Superficie A — HISTORICAL_OLD vs OLD_CODE_REPLAY (mismo env)

**174/174 filas pareadas, 0 filas huérfanas. 35 de 36 campos bit-idénticos.**

El único campo con diferencia material es `runtime_ms` (174 filas) →
`NON_SCIENTIFIC_RUNTIME_VARIATION`.

**`ENVIRONMENT_REPLAY_DRIFT = 0` (cero científico).**

El control replay reproduce el artefacto histórico **exactamente** en todo lo que importa:
`aligned_rmse`, `corr`, `variance_ratio`, `gradient_rmse`, features, oráculo y —crítico—
`sigma_selection.winner = nz_p01` con la **misma tabla de rho**. Esto valida el replay como
control reproducible y permite usar la superficie B como medida limpia del delta de código.

*Salvedad registrada:* `e23bf7ac` **no** es ancestro de `fc87b7e5` y
`OLD_M2_EXECUTION_SHA_FULL_VERIFIED=NO`. El replay es el mejor HEAD sustentado de la rama M2
en la ventana histórica, no una atestación del SHA exacto. Lo que lo valida es la
reproducción bit a bit, no la procedencia.

---

## 3. Superficie B/C — OLD_CODE_REPLAY vs NEW (mismo env, distinto código)

174/174 filas pareadas, 34/34 assets. `M2_HISTORICAL_SIGMA_WINNER = M2_OLD_REPLAY_SIGMA_WINNER
= M2_NEW_SIGMA_WINNER = nz_p01` (status `EVALUABLE` en NEW; OLD no emitía status).

### 3.1 Conteo por causa (celdas = fila×campo o asset×campo)

| Categoría | Celdas | Origen |
|---|---:|---|
| `HEIGHT_FIDELITY_CORRECTION` | 4 564 | `authored_dataset.resize_height` (uint8 → float32) |
| `MATH_A_DIRECT` | 268 | `curl_proxy` §3, `projection_residual` §15, `normal_height_residual_oracle` §19 |
| `MATH_B_TIE_CORRECTION` | 208 | `metrics.spearman` (rango ordinal → rango medio) |
| `SERIALIZATION_ONLY_NONFINITE` | 208 | REPRO-A (`_json_safe`, `allow_nan=False`) |
| `NON_SCIENTIFIC_RUNTIME_VARIATION` | 208 | `runtime_ms` |
| `MATH_A_DOWNSTREAM_CHANGES` | 4 | rho de `curl_mad` y `projection_median` |
| `MATH_B_DOWNSTREAM_CHANGES` | 0 | sin empates en la calibración |
| `EXPECTED_DIAGNOSTIC_COHORT_MEMBERSHIP_CHANGE` | 3 | cohorte B (20/30/40°), ver §4.2 |
| `ENVIRONMENT_CONFOUNDED` | 0 | replay SUCCESS ⇒ env aislado |
| **`UNEXPECTED_UNEXPLAINED_CHANGES`** | **0** | — |

### 3.2 Atribución causal demostrada, no inferida

**(a) HEIGHT_FIDELITY — contrafactual exacto.** Se ejecutó el **código NEW** contra el
**height OLD** (réplica exacta de `resize_height` histórico: cuantización a uint8 antes de
interpolar) y se comparó con el OLD replay:

```
aligned_rmse   34/34 celdas idénticas (max |diff| = 0.000e+00)
corr           34/34      mae            34/34      rmse           34/34
variance_ratio 34/34      gradient_rmse  34/34      ssim           34/34
r2             34/34      seam_height    34/34      low/high_band  34/34
```

Con el height histórico, el código NEW **reproduce el OLD bit a bit**. Por lo tanto el delta
de estas métricas es 100 % fidelidad de height y **0 % MATH-A**. Además el hash de
`mat.normal` es idéntico en 34/34 assets: la corrección no toca el input normal.

Artefacto del contrafactual: `evidence/counterfactual-height-isolation.json`.

Magnitud: deltas relativos **pequeños** (mediana `aligned_rmse` 3,8e-04; `corr` 5,6e-05),
consistente con ruido de cuantización 8-bit. Los diagnósticos de banda son más sensibles
(`hf_gt_fraction` mediana 0,17; `high_band_error` 0,079) — el height suavizado pierde energía
de alta frecuencia de cuantización, que es exactamente lo que estas métricas miden.

**(b) MATH_B — input idéntico, delta persistente.** Para `spearman` el contrafactual **no**
reproduce el OLD: **0/33** celdas coinciden, con `max|diff| = 1,9e-03`. Con el mismo height y
el mismo par (rec, ref), el único cambio es el algoritmo de ranking ⇒
`MATH_B_TIE_CORRECTION`.

Verificación directa sobre vectores de control (mismos inputs en ambas implementaciones):

| caso | rho OLD (ordinal) | rho NEW (rango medio) |
|---|---:|---:|
| `[10,10,20,30,40,40]` vs `[1..6]` | 1.000000 | 0.971008 |
| `[1,1,1,1,2]` vs `[1..5]` | 1.000000 | 0.707107 |
| `[7,7,7,7]` vs `[1,2,3,4]` | **1.000000** | **no evaluable (NaN)** |

El tercer caso es la corrección de un **falso positivo**: OLD fabricaba `rho = 1.0` sobre un
vector constante. NEW devuelve no-evaluable, que es lo estadísticamente correcto.

**(c) MATH_A_DIRECT.** Las features de curl y de proyección cambian con deltas **grandes**
(rel. mediana 0,62 y 0,85), coherente con correcciones de convención/geometría, no con ruido:

- §3 `curl_proxy`: `∂p/∂x − ∂q/∂y` (que es `h_xx − h_yy`) → `∂q/∂x − ∂p/∂y` (curl canónico).
- §15 `projection_residual`: normal reproyectada con `sqrt(max(0,1−p²−q²))` → `normals_from_gradients`.
- §19 `normal_height_residual_oracle`: idem, corrige el signo XY invertido.

Efecto medido sobre los 34 assets (no sobre fixtures): `oracle_best_strength` pasa de
`{-0.05 ×29, +0.05 ×5}` a `{-0.05 ×4, +0.05 ×29, +0.2563 ×1}` — el signo elegido se invierte
en 25 assets. `oracle_agreement_deg`: mediana 23,93° → 20,52°, máximo 88,94° → 69,33°.
*(El docstring del código cita `-0.20 → +1.00` para el fixture S06; esos valores **no** son
los de este corpus y no deben usarse como magnitud del efecto M2.)*

**(d) MATH_A_DOWNSTREAM = 4.** Descomposición factorial 2×2×2 de cada rho de selección
(input X = σ candidata; input Y = error; método de ranking OLD/NEW):

```
curl_mad          aligned_rmse   Δρ=-0.223776  | ranking=+0.000000  X=-0.223776  Y=+0.000000
projection_median aligned_rmse   Δρ=-1.020979  | ranking=-0.000000  X=-1.020979  Y=+0.000000
nz_p01            aligned_rmse   Δρ=-0.000000  | ranking=-0.000000  X=+0.000000  Y=+0.000000
nz_p05_div3       aligned_rmse   Δρ=+0.000000  | ranking=+0.000000  X=+0.000000  Y=+0.000000
```

- `contrib_ranking = 0` en **todos** los casos ⇒ **MATH-B no mueve ningún rho de selección**
  (no hay empates en los 12 valores de calibración; verificado: `has_ties=False` en X e Y).
- `contrib_inputY = 0` en todos ⇒ el cambio de errores por height **no altera el rho**: es
  invariante a la transformación monótona que el height induce. La selección de σ es robusta
  a la corrección de fidelidad.
- El delta viene **100 % del input X** (features) ⇒ `MATH_A_DOWNSTREAM`.
- `nz_p01`/`nz_p05_div3` tienen X idéntico ⇒ Δρ = 0 (bit-idéntico salvo 5e-17 de redondeo).

### 3.3 REPRO-A

- `r_p01_proxy` (34 filas RAW, σ_eff=0): `Infinity` → `null`.
- `hf_energy_ratio` (33 filas): `NaN` → `null`.
- Ambos son exclusivamente boundary de serialización ⇒ `SERIALIZATION_ONLY_NONFINITE`.
- **No** se confunde con `NOT_EVALUABLE` estadístico: el valor subyacente no cambió.

Un matiz que **no** es REPRO-A: 39 celdas de `hf_energy_ratio` pasan de **finito a `null`**
(NaN). Con el height corregido el diagnóstico deviene NOT_INFORMATIVE. Se clasifica como
`HEIGHT_FIDELITY_CORRECTION`, no como serialización.

---

## 4. M3 — HISTORICAL_M3_RAW vs NEW_M3

**`M3_DECISION_EQUIVALENT = YES`.** `OLD.decision == NEW.decision == EXP_M3_RECONSTRUCTION_CONDITIONAL`,
literalmente. `cohort_a.usable = 31` en ambos; `manifest_sha256` idéntico
(`c9c1665942281966ddeb4f4ff05ed9e2be302d80155cfa8bfc1e487c2bfbecde`); `n` y `sufficient`
bit-idénticos. El sustento de la decisión (`_raw_viable=False`, con
`raw.median_abs_corr=0.83918` y `raw.median_variance_ratio=0.70422`) es bit-idéntico: la
decisión **no depende** del proxy ni de la cohorte diagnóstica.

### 4.1 Cambios materiales

| Campo | OLD | NEW | Clase |
|---|---|---|---|
| `selected_proxy` | `nz_p01` | `projection_residual_median_deg` | MATH_A_DOWNSTREAM |
| `heldout_trust.spearman` | 0.488235 | 0.385294 | MATH_B + MATH-A |
| `heldout_trust.auc_catastrophic` | 0.229167 | 0.333333 | diagnóstico |
| `n_boot_evaluable` / `n_boot_degenerate` / `ci_status` | ausentes | 1000 / 0 / `EVALUABLE` | `NEW_DIAGNOSTIC_ADDED_BY_MATH_B` |

### 4.2 Membresía de la cohorte diagnóstica — `EXPECTED_DIAGNOSTIC_COHORT_MEMBERSHIP_CHANGE`

La cohorte B (`EVALUATION_DIAGNOSTIC_ONLY`, filtrada por `oracle_agreement_deg < θ`) cambió
de tamaño en los tres umbrales de sensibilidad:

| θ | n OLD | n NEW | Δ |
|---|---:|---:|---:|
| 20° | 14 | 16 | +2 |
| 30° | 20 | 24 | +4 |
| 40° | 23 | 28 | +5 |

Causa: `oracle_agreement_deg` bajó con el oráculo corregido (§19) ⇒ más assets pasan el
filtro. Assets con `agreement < 30°`: 21 → 25. Es un cambio **esperado** de una cohorte
declarada diagnóstica; no toca la cohorte A ni la decisión.

### 4.3 Curvas de operaciones — NO comparables punto a punto

El `rates_sweep@30deg` **no es apples-to-apples** y así debe leerse:

- Proxy OLD: `projection_residual_median_deg`; proxy NEW: `projection_residual_p95_deg`.
  El proxy se elige por `max|spearman|` sobre la cohorte a 30°, y MATH-A movió los valores ⇒
  cambió el ganador.
- La cobertura `auto_safe_coverage = len(auto)/len(rows)` hereda el N de la cohorte, que
  cambió (20 → 24): OLD `{0.1, 0.2, 0.3, 0.4, 0.5}` vs NEW `{0.125, 0.2083, 0.2917, 0.4167, 0.5}`.
  **Coinciden sólo en q = 0.5.**
- Los `threshold` son función del proxy elegido (OLD 1.27…12.75; NEW 0.81…2.65) y **no** son
  identidad (§25).

Conclusión correcta: `OLD_RATE_Q_MAPPING_VERIFIED=NO` en el sentido fuerte. La identidad `q`
está bien usada (no se usó `threshold` como clave), y los 5 puntos por curva existen, pero
**las dos curvas describen proxies distintos sobre cohortes de tamaño distinto**. No se
reporta un "5/5 mapeado".

---

## 5. Nota metodológica — categoría fuera de la taxonomía del protocolo

El protocolo §27 define `MATH_A_*`, `MATH_B_*`, `REPRO_A`/`SERIALIZATION_ONLY_NONFINITE`,
`ENVIRONMENT_*`, `NON_SCIENTIFIC_RUNTIME_VARIATION` y las de fallo. **No prevé** una
corrección de fidelidad de datos. Como el delta dominante proviene de ahí, se introduce la
etiqueta explícita `HEIGHT_FIDELITY_CORRECTION`, con la evidencia del contrafactual exacto
(§3.2a). No se fuerza el encuadre en `MATH_A_DIRECT` (sería una atribución causal falsa) ni
en `UNEXPECTED_*` (está plenamente explicado).

Consecuencia práctica: la corrección de `resize_height` es la que **más** mueve los números
M2 y la que menos se nombra. Si el equipo quiere un `REVAL-2` con atribución MATH-A/B pura,
conviene fijar el height (o separar la corrección) para poder medirla.

---

## 6. Arena review adversarial (§33)

`ARENA_POST_RUN_REVIEW = PASS_WITH_FINDINGS`. Revisión independiente con contexto fresco.
No exigió rerun material. Hallazgos adjudicados:

| # | Punto | Veredicto | Adjudicación |
|---|---|---|---|
| 1 | Causal attribution | OK + error menor | Taxonomía recomputable y consistente; se corrigieron las magnitudes de `oracle_best_strength` (§3.2c) |
| 2 | Corpus drift | OK | Los 3 hashes históricos coinciden |
| 3 | Row pairing | OK | 174/174 y 34/34, sin huérfanas ni claves duplicadas |
| 4 | Spearman | OK | Factorial reproducido aritméticamente; sin empates confirmado |
| 5 | Environment | OK (con salvedad) | Salvedad de procedencia registrada en §2 |
| 6 | Sigma identity | OK | Se usa nombre de candidato, no `sigma_eff` |
| 7 | Rate-q mapping | **HALLAZGO (material a la afirmación)** | Corregido en §4.3: proxies y cohortes distintas ⇒ `OLD_RATE_Q_MAPPING_VERIFIED=NO` |
| 8 | Serialization | OK | 34 Inf→null, 33 NaN→null, 39 finito→null (clasificado como height) |
| 9 | Decision equivalence | OK (leve sobreafirmación) | Decisión literalmente idéntica y **independiente** del proxy; se ajustó la redacción |
| 10 | RAW mutation | No verificable / sin evidencia de manipulación | Se registran ahora los sha256 de los RAW (§7) |
| 11 | Post-hoc retuning | OK | Constantes idénticas OLD/NEW; bootstrap es adición, no retune |

Acciones derivadas de la Arena, todas aplicadas: corrección de §3.2c y §4; preservación del
contrafactual en `evidence/`; registro de sha256 de los RAW.

---

## 7. Hashes de los RAW (no mutados después de hashear)

| Artefacto | bytes | sha256 |
|---|---:|---|
| `old_replay/m2/rows.json` | 254365 | `25fd6124c7ea97688cbe47039d800b7520bfb3332c9fe9a63068d29096ff8c30` |
| `old_replay/m2/characs.json` | 91786 | `df65d8660ef7fd066e8d1e8d90196acc84d039f9b2612b3be9ad4f834160f28f` |
| `new/m2/rows.json` | 254772 | `b2bbea3ffa85b1d847b20c50d2d1dce1e6723f74ca5da254615008ae99569a48` |
| `new/m2/characs.json` | 91536 | `402c3f6f3f4d47c7681cc77d7684425a1b5e4e00b25e2e4a5dde0dcd1e3071b2` |
| `new/m3/exp_m3_results.json` | 277216 | `4364d1c97fd011d44c59f39335a72a42fdff398b1de66c7500db2e5fc5ca4205` |

---

## 8. Decisiones finales

```
M3_DECISION_EQUIVALENT            = YES
UNEXPECTED_UNEXPLAINED_CHANGES    = 0
M2_SIGMA_WINNER (hist/replay/new) = nz_p01 / nz_p01 / nz_p01
OLD_RATE_Q_MAPPING_VERIFIED       = NO (proxies distintos; ver §4.3)
EXPECTED_DIAGNOSTIC_COHORT_CHANGE = SÍ (cohorte B, 20/30/40°)
M4_RERUN_EXECUTED                 = NO
M5_RERUN_EXECUTED                 = NO
M6_REAL_RUN_EXECUTED              = NO
```
