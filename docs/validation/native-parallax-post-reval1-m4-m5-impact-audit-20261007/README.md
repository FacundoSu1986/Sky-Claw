# Native Parallax — POST-REVAL-1: auditoría de impacto de M4/M5

Auditoría **de solo lectura** que determina si las correcciones demostradas por REVAL-1
alcanzan las rutas científicas que produjeron las decisiones **EXP-M4** y **EXP-M5**, y si
al alcanzarlas alteran la decisión.

- **Fecha (UTC):** 2026-10-07
- **Base del audit:** `97dcc7ab9ade29153faa0ccec428b78612cfc04a` (post-REVAL-1, PR #696)
- **Rama:** `research/native-parallax-post-reval1-m4-m5-impact-audit`
- **Worktree:** `E:\SkyClaw_M4M5_AUDIT_97dcc7ab` (limpio al inicio)
- **Alcance:** `AUDIT_ONLY` — **no** se reejecutó M4, M5 ni M6; **no** se tocó PR #675.

```
M4_RERUN_EXECUTED    = NO
M5_RERUN_EXECUTED    = NO
M6_REAL_RUN_EXECUTED = NO
```

---

## 0. Veredicto

| | Decisión histórica | Adjudicación | ¿Rerun? |
|---|---|---|---|
| **EXP-M4** | `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT` | `NOT_INVALIDATED` | `NO` |
| **EXP-M5** | `EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED` | `NOT_INVALIDATED` | `NO` |

**`NOT_INVALIDATED` no significa `revalidado`.** Ambas decisiones sobreviven a REVAL-1
porque ninguna de sus cuatro correcciones alcanza la decisión primaria — pero M4 y M5
arrastran, cada uno, una desviación de protocolo **preexistente y ajena a REVAL-1** (§7).

`PR675_RECOMMENDATION = READY_FOR_DESIGN_RECONCILIATION` — habilita un **slice nuevo** para
reconciliar #675. **No** es una autorización de merge y **no** se tocó #675 en esta sesión.

> **Nota 2026-10-10 (post #700, SUPERSEDED).** La línea anterior es la recomendación
> **histórica** de esta auditoría (`eb066a1`). Quedó **superada** por la autoridad de #700:
> `PR675_RECOMMENDATION = KEEP_DRAFT_BLOCKED`. El veredicto científico estrecho
> `NOT_INVALIDATED` se conserva. `M6_IMPLEMENTATION_BLOCKED = YES`. Ver §12 y
> `post-700-reconciliation.json`.

---

## 1. Qué cambió en REVAL-1

REVAL-1 revalidó **M2/M3** tras cuatro cambios de código:

| Etiqueta | Commit | PR | Fecha | Módulo |
|---|---|---|---|---|
| `HEIGHT_FIDELITY_CORRECTION` | `2530f7a8` | #653 | 2026-09-28 | `authored_dataset.resize_height` (uint8 → float32) |
| `MATH_A` | `ae97fd93` | #681 | 2026-10-04 | `normal_from_height`, `nz_policies`, `solver_coherence`, `trust_proxies` |
| `MATH_B` | `32e4c1d6` | #685 | 2026-10-05 | `metrics`, `run_exp_m2`, `run_exp_m3`, `run_exp_m4` |
| `REPRO_A` | `1dd60745` | #687 | 2026-10-05 | `run_exp_m2` (boundary JSON) |

Hallazgo dominante de REVAL-1: la causa del 83,6 % de los deltas M2 fue la **cuarta**
corrección, la de fidelidad de height — no MATH-A ni MATH-B. MATH-A resultó **inobservable**
en M2/M3 porque todos los call sites usan `sx = sy = 1.0`.

---

## 2. Ancestría — el primer filtro, y el más fuerte

`git merge-base --is-ancestor <fix> <execution_sha>`:

| Superficie | Execution SHA | height #653 | MATH-A #681 | MATH-B #685 | REPRO-A #687 |
|---|---|:--:|:--:|:--:|:--:|
| M4 histórico | `fe54e9a9` | **NO** | NO | NO | NO |
| M4 revalidado | `1a52c3ea` | **SÍ** | NO | NO | NO |
| M5 prereg freeze | `d33f81ca` | **SÍ** | NO | NO | NO |
| M5 execution freeze (histórico) | `e116196f` | **SÍ** | NO | NO | NO |
| M5 corrective freeze | `e3dbda51` | **SÍ** | NO | NO | NO |
| main post-REVAL-1 | `97dcc7ab` | SÍ | SÍ | SÍ | SÍ |

Dos consecuencias inmediatas:

1. **MATH-A, MATH-B y REPRO-A no están en el código de NINGUNA corrida M4/M5.** Eso no basta
   para descartarlas (§4 y §5 las auditan por call graph, porque podrían afectar si se
   aplicaran), pero descarta que hayan estado presentes.
2. **El fix de height SÍ separa al M4 histórico del revalidado.** M4 histórico corrió sin él;
   M4 revalidado corrió con él; M5 corrió con él.

Identidad de módulos (sha256 del blob LF, verificada contra el campo `solver_module_sha256`
grabado en los propios artefactos):

```
solver_coherence.py     fe54e9a9 / 1a52c3ea / e3dbda51 = 18ecf5d5...   (idéntico)
                        post-REVAL-1 (97dcc7ab)        = 0d0bdf21...   (cambia con MATH-A)
authored_dataset.py     fe54e9a9 = 290f2845...  →  1a52c3ea / 97dcc7ab = 4184cbad...  (fix height)
frequency_coherence.py  ausente en fe54e9a9 y 1a52c3ea (nace con M5)
```

El cambio de `solver_coherence.py` introducido por MATH-A es **exclusivamente de docstring**
(`git diff e3dbda51..97dcc7ab -- solver_coherence.py` → 1 hunk, sólo texto). Es la primera
prueba de que el solver M4/M5 no cambió de comportamiento.

---

## 3. Qué alcanza M4

### 3.1 Cadena de decisión (call graph)

```
run_exp_m4.main
 ├─ prepare_entries ─────────────── carga + gate SHA256 (sin matemática de proxy)
 ├─ run_asset(entry, 512)
 │   ├─ authored_dataset.load_asset ──► resize_height          ◄── HEIGHT_FIDELITY
 │   ├─ solver_coherence.self_forward(h, bits=8)
 │   │      └─ spectral_gradients + normals_from_gradients(sx=1,sy=1)   [NO tocada por MATH-A]
 │   ├─ solver_coherence.evaluate_path(h, normal)   ×4 paths
 │   │      ├─ solve_normal → reconstruct_from_normal(n,"RAW",0,0)
 │   │      │      └─ decode_gradients_policy(n, 1.0, 1.0, ...)  ◄── MATH-A, sx=sy=1
 │   │      ├─ OracleOnly.fit_global_scale            [NO tocada por MATH-A]
 │   │      └─ OracleOnly.evaluate                    [NO tocada por MATH-A]
 │   └─ OracleOnly.normal_height_residual_oracle(mat.normal, h)  ◄── MATH-A
 │          └─► coherence_agreement_deg / coherence_best_strength  [DIAGNÓSTICO]
 ├─ delta_stats(rows)          → medianas de rmse / abs_corr / var
 ├─ evaluate_rules(full, held) → C1_self_good, C2_auth_worse
 └─ decide(C1, C2)             → EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT
```

**`evaluate_rules` es la única fuente de la decisión** y sólo lee:
`rmse_self_median`, `abs_corr_self_median`, `var_self_median`, `delta_rmse_median`,
`abs_corr_self_median − abs_corr_auth_median`, y sus equivalentes de held-out. Nada más.

### 3.2 Qué NO es input de decisión en M4

`coherence_agreement_deg`, `coherence_best_strength`, `summary.coherence_diagnostic.*`,
`fd_q8_*`, `self_float_*`, `family_diagnostics`, `provider_diagnostics`.

Prueba de que las filas M4 no contienen campos de proxy: las 56 claves de fila no incluyen
**ninguna** con `spearman`, `curl` ni `projection`.

### 3.3 Height fidelity — el único impacto material, y ya re-ejecutado

Comparación programática `data/exp-m4-results.json` (`fe54e9a9`) vs
`docs/validation/native-parallax-revalidation-20260928/exp_m4_results.json` (`1a52c3ea`):

| | |
|---|---|
| Assets pareados | 31/31 |
| Assets con algún delta primario | 31/31 |
| `delta_rmse` | max \|Δ\| = 1,714e-03 · mediana 1,181e-04 |
| `self_rmse` | max \|Δ\| = 1,690e-03 · mediana 9,712e-05 |
| `coherence_agreement_deg` | max \|Δ\| = 2,753 |
| **`coherence_best_strength`** | **sin diferencias (0/31)** |
| Decisión | **idéntica** |
| Reglas | **idénticas** (`C1=true`, `C2=true`) |

`coherence_best_strength` idéntico es una confirmación fuerte: si MATH-A hubiese estado
presente en alguna de las dos corridas, el signo del mejor strength se habría invertido
(verificado sintéticamente en §6). No se invirtió ⇒ MATH-A no estaba en ninguna.

La atribución del delta al fix de height es directa: `authored_dataset.py` pasa de blob
`5c65af28` (`fe54e9a9`) a `dff54d13` (`1a52c3ea`) y no vuelve a cambiar hasta `97dcc7ab`;
el directorio de la corrida se llama literalmente `revalidation-height-resize-20260928`.

> **Precisión (review adversarial F1).** En el rango `fe54e9a9..1a52c3ea` también cambia
> `run_exp_m4.py`, por #643 (`2d1473f2`, archivos de corpus ilegibles → exclusión objetiva
> en vez de abortar). Es un cambio **no numérico** y **no se activó**: ambos artefactos
> tienen `exclusions = 0`, `n_prepared = 31`, `n_rows = 31`. Un tercer commit del rango,
> `336ae917` (#639), es el squash de la rama M4 cuyo contenido ya está en `fe54e9a9`: no
> aporta delta neto. El único delta **numérico** sigue siendo el fix de height.

**Conclusión:** `M4_HEIGHT_FIDELITY_IMPACT = MATERIAL_PRIMARY_IMPACT`, pero el rerun ya
existe y es publicado ⇒ `M4_RERUN_REQUIRED = NO` y la decisión no se invalida.

### 3.4 MATH-A — inobservable en el escalado, sin camino en curl/proyección, diagnóstico en el oráculo

- **Escalado (`gradients_from_normal`, `decode_gradients_policy`) — `INOBSERVABLE`.**
  `reconstruct_from_normal` hardcodea `decode_gradients_policy(normal, 1.0, 1.0, ...)` y M4/M5
  no pasan `sx`/`sy` en ningún call site. Diagnóstico sintético: las 4 policies dan
  `max|Δ| = 0.0` y `array_equal=True`. A `sx = 1.7` el mismo diagnóstico da `max|Δ| = 51.03`
  ⇒ el defecto geométrico es **latente**, no ejercido.
- **Curl — `NO_PATH`.** `curl_proxy`/`curl_field` sólo se llaman desde
  `normal_only_features` (M1/M2).
- **Proyección — `NO_PATH`.** `projection_residual` sólo se llama desde
  `normal_only_features` y el mapa de candidatos de σ_eff (M1/M2/M3).
- **Oráculo — `DIAGNOSTIC_ONLY`.** `normal_height_residual_oracle` sí se llama desde
  `run_exp_m4.run_asset`, pero su salida va sólo a `coherence_*` → `coherence_diagnostic`,
  cuyo propio `note` dice *"NO es trust proxy ni criterio de exclusión"*. `evaluate_rules`
  no lee esos campos.

### 3.5 MATH-B — `DIAGNOSTIC_ONLY`

Hay **dos** usos alcanzables de Spearman en M4, y ninguno es decision-bearing:

1. `run_exp_m4.coherence_diagnostic` — MATH-B lo extrajo a función y lo hizo devolver `None`
   cuando el rho no es evaluable (porque el reporte se serializa con `allow_nan=False`).
2. `metrics.compute_all`, invocado desde el path **primario** vía `OracleOnly.evaluate`.
   MATH-B también modificó esa línea (la guarda `if finite`). Pero `evaluate_path`
   **descarta** la clave `"spearman"`: selecciona 10 claves de las ~30 que devuelve el
   oráculo (`rmse`, `abs_corr`, `variance_ratio`, `gradient_rmse`, `raw_centered_rmse`,
   `seam_*`).

Corroboración de que el artefacto revalidado es **pre**-MATH-B: su `coherence_diagnostic`
**no tiene la clave `status`** que MATH-B introduce. Además, el rho fue finito en ambas
corridas (0,15040 histórico / 0,15524 revalidado), así que ni siquiera la rama `None` se
activó.

### 3.6 REPRO-A — `NO_PATH`

REPRO-A toca sólo `_json_safe`, `_write_json` y los dos call sites de `main` en
`run_exp_m2.py`. M4 importa de `run_exp_m2` únicamente `spearman` y `split_of` — ninguno
tocado — y `solver_coherence` (de donde M4 y M5 cuelgan) importa `reconstruct_from_normal`,
que REPRO-A tampoco toca. Además `run_exp_m4.py` **ya** escribía con `allow_nan=False`
antes de REPRO-A: el contrato estricto ya era el de M4.

---

## 4. Qué alcanza M5

### 4.1 Cadena de decisión

```
run_exp_m5.main
 ├─ prepare_entries (reusa M4)
 ├─ run_asset_m5(entry, 512)
 │   ├─ authored_dataset.load_asset ──► resize_height     ◄── HEIGHT_FIDELITY (ya presente)
 │   ├─ _aligned_reconstruction(h, self_forward(h, bits=8))   [SELF]
 │   │      └─ solve_normal (sx=sy=1) + OracleOnly.fit_global_scale
 │   ├─ _aligned_reconstruction(h, mat.normal)                [AUTH]
 │   └─ frequency_coherence.band_masks + asset_summary        [FFT puro, solo numpy]
 ├─ frequency_coherence.cohort_medians(rows)
 ├─ frequency_coherence.evaluate_rules(cohort, held)
 └─ frequency_coherence.decide(rules) → EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED
```

M5 **reutiliza exactamente** el path de M4: `solve_normal` → `reconstruct_from_normal(n,
"RAW", 0.0, 0.0)` → `decode_gradients_policy(n, 1.0, 1.0, ...)`, con
`OracleOnly.fit_global_scale` como única alineación global por asset. **Confirmado**: M5
importa `solve_normal`, `self_forward` y `bootstrap_median_ci` de `solver_coherence`.

### 4.2 Clasificación

| Corrección | M5 primary | Por qué |
|---|---|---|
| Height fidelity | `INOBSERVABLE` | ya presente en `d33f81ca`/`e116196f`/`e3dbda51` (`authored_dataset` = `dff54d13` en las tres) ⇒ **sin delta pendiente** |
| MATH-A escalado | `INOBSERVABLE` | `sx = sy = 1` literal, identidad bit a bit (§3.4) |
| MATH-A curl / proyección / oráculo | `NO_PATH` | M5 no importa ni llama ninguna de las tres |
| MATH-B Spearman | `NO_PATH` | `grep -n "spearman\|rank\|tie"` sobre `frequency_coherence.py`, `run_exp_m5.py` y `solver_coherence.py` no devuelve **ninguna llamada** |
| REPRO-A | `NO_PATH` | M5 importa sólo `split_of` de `run_exp_m2`; usa su propio `_json_safe` |

> **Precisión sobre `INOBSERVABLE` para height/M5.** Significa *sin delta pendiente en el
> artefacto congelado*, **no** que el fix sea inocuo en general: en M4 histórico sí movió los
> números. La distinción está registrada en `impact-matrix.json → value_semantics` y en el
> campo `already_present_at_execution_freeze` de cada fila.

### 4.3 Los números del brief, revalidados contra el RAW

El brief cita `LOWMID excess ≈ 0,5082`, `HIGH excess ≈ 0,3350`, `HIGH_ENRICHMENT ≈ 0,7542`.
Localizados en `C:\SkyClawResearch\NativeParallax\EXP-M3\runs\exp-m5\`:

| Métrica (`summary.full`) | `full-e116196f.json` (histórico) | `full-corrected-e3dbda51.json` (vigente) |
|---|---:|---:|
| `excess_lowmid_nrmse` | 0,5040464728053042 | **0,5082023969791158** |
| `excess_high_nrmse` | 0,34737320116493153 | **0,33504144042079254** |
| `high_enrichment` | 0,7541691251882152 | **0,7541691251882152** |
| `auth_lowmid_nrmse` | 0,5543140185923835 | 0,5543140185923835 |
| `decision` | `EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED` | `EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED` |
| sha256 | `f8503610...` | `57b22b1d...` |

Los tres números del brief corresponden al artefacto **correctivo** (agregación apareada).
El histórico usa `median(AUTH) − median(SELF)` para `summary.*.excess_*`; por eso da 0,5040
y 0,3474. Ambos artefactos existen en disco, **no se sobrescribió ninguno**.

Thresholds idénticos en las cuatro corridas M5 (calibration y full, histórico y correctivo):
`T_LOWMID_NRMSE=0.15`, `T_LOWMID_EXCESS=0.10`, `T_HIGH_ENRICHMENT=2.0`,
`LOWMID_HIGH_CUTOFF=32`, `ENERGY_GATE_FRACTION=1e-6`, `M5_BOOTSTRAP_SEED=20260925`,
`band_edges=[4,8,16,32,64,128]`. `M5_THRESHOLDS_UNCHANGED = YES`.

### 4.4 El rerun correctivo de M5 NO fue por REVAL-1

El design doc de M5 registra, en §21, un finding **post-FULL interno**: `cohort_medians()`
agregaba `median(AUTH) − median(SELF)` cuando el prereg exige
`median(NRMSE_AUTH − NRMSE_SELF)` por asset. Eso motivó un *corrective execution freeze*
(`e3dbda51`, 2026-10-03) y el `full-corrected`. `HISTORICAL_TO_CORRECTED_DECISION_EQUIVALENT
= YES`.

Es un defecto **de implementación M5**, no un bug upstream M0–M4: el vocabulario
`EXP_M5_INVALIDATED_BY_UPSTREAM_BUG` está preregistrado para lo segundo y **no** se reutiliza
acá. Este audit no lo mezcla con REVAL-1.

---

## 5. Matriz de impacto

| Upstream change | M4 primary | M4 diagnostic | M5 primary | M5 diagnostic | Evidence |
|---|---|---|---|---|---|
| `HEIGHT_FIDELITY` | `MATERIAL_PRIMARY_IMPACT` | `MATERIAL_PRIMARY_IMPACT` | `INOBSERVABLE` | `INOBSERVABLE` | 31/31 assets cambian en M4 hist→reval; decisión igual. Ya incorporado en M5. |
| `MATH-A` gradient scaling | `INOBSERVABLE` | `INOBSERVABLE` | `INOBSERVABLE` | `INOBSERVABLE` | `sx=sy=1` literal; `max\|Δ\|=0.0`, `array_equal=True` en las 4 policies |
| `MATH-A` curl | `NO_PATH` | `NO_PATH` | `NO_PATH` | `NO_PATH` | sólo `normal_only_features` (M1/M2); ninguna fila M4 tiene `curl_*` |
| `MATH-A` projection | `NO_PATH` | `NO_PATH` | `NO_PATH` | `NO_PATH` | sólo features/candidatos σ_eff (M1/M2/M3) |
| `MATH-A` oracle | `NO_PATH` | `DIAGNOSTIC_ONLY` | `NO_PATH` | `NO_PATH` | `coherence_*` no entra a `evaluate_rules`; M5 no lo llama |
| `MATH-B` Spearman | `NO_PATH` | `DIAGNOSTIC_ONLY` | `NO_PATH` | `NO_PATH` | único uso M4 = `coherence_diagnostic`; M5 sin llamadas |
| `REPRO-A` serialization | `NO_PATH` | `NO_PATH` | `NO_PATH` | `NO_PATH` | sólo boundary JSON de M2; M4 ya usaba `allow_nan=False` |

Detalle machine-readable en `impact-matrix.json`.

---

## 6. Diagnósticos sintéticos (sin corpus)

Permitidos por el contrato `AUDIT_ONLY`. Semilla `20261007`, `N=64`, height sintético con
relieve; implementación histórica reproducida inline para el contrafactual.

| Diagnóstico | Resultado |
|---|---|
| `decode_gradients_policy` a `sx=sy=1`, 4 policies | `max\|Δ\| = 0.0`, `array_equal = True` |
| `gradients_from_normal` a `sx=sy=1` | `max\|Δ\| = 0.0`, `array_equal = True` |
| `gradients_from_normal` a `sx=1.7` | `max\|Δ\| = 51.03` (defecto latente) |
| Oráculo HIST vs NEW, mismo input | 7,106° / strength −0,256 **vs** 0,545° / +1,081 → difieren, signo invertido |
| Spearman sin empates | idéntico bit a bit (−0,19047619047619047) |
| Spearman con empates | 0,9999999999999998 → 0,8333333333333335 (= 5/6) |

---

## 7. Desviaciones de protocolo preexistentes (no resueltas acá)

Se registran porque afectan cómo debe leerse cada decisión, y porque **no** deben confundirse
con el impacto de REVAL-1.

**M4.** El artefacto revalidado lleva `protocol_status.status =
UNDER_REVIEW_PROTOCOL_DEVIATION`, `confirmatory_final = false`,
`observed_runner_decision_is_confirmatory = false`: no hubo commit de execution-freeze entre
calibration y full, y `--frozen-ack` apuntaba al checkout base (`1a52c3ea`), no a un freeze
dedicado. El runner M4 no valida el SHA. La desviación es **procedural, no científica**
(thresholds, solver, corpus, manifest, split y reglas ya estaban congelados y no se
retunearon). Registrada en `native-parallax-revalidation-checkpoint.md`.

**M5.** `PROTOCOL_STATUS = UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE`,
`legacy_heldout_blind_until_execution_freeze = false`: durante el hardening de provenance,
`--frozen-ack foo` ejecutó una pasada FULL sobre los 31 assets antes de cualquier execution
freeze. Los JSON se eliminaron, pero la ceguera experimental no se restaura. §18 del design
doc lo registra de forma permanente.

En ambos casos: **REVAL-1 no los crea, no los agrava y este audit no los resuelve.**

---

## 8. Comandos ejecutados (reproducibles)

```bash
# Gate de drift (worktree principal, solo lectura)
git fetch origin --prune
git rev-parse origin/main                                   # == 97dcc7ab9ade29153faa0ccec428b78612cfc04a

# Worktree del audit
git worktree add -b research/native-parallax-post-reval1-m4-m5-impact-audit \
  E:/SkyClaw_M4M5_AUDIT_97dcc7ab 97dcc7ab9ade29153faa0ccec428b78612cfc04a

# Ancestria (desde el worktree del audit). Repetir por cada par (fix, execution SHA).
git merge-base --is-ancestor 2530f7a83b931ffdfa250b0fdda43ab351f56477 \
  fe54e9a9e189adc575a222a537a87b04d83d2915 && echo "HEIGHT ANCESTOR of M4_historical"
git merge-base --is-ancestor 2530f7a83b931ffdfa250b0fdda43ab351f56477 \
  1a52c3ea5d2259f3b05bfdef1fdbe781608c610f && echo "HEIGHT ANCESTOR of M4_revalidated"
git merge-base --is-ancestor ae97fd9375727afc093c0dd0aba3d6ae58a3431c \
  1a52c3ea5d2259f3b05bfdef1fdbe781608c610f || echo "MATH_A NOT ANCESTOR of M4_revalidated"

# Identidad de modulos (sha256 del blob LF)
git show 1a52c3ea5d2259f3b05bfdef1fdbe781608c610f:sky_claw/local/native_parallax/research/solver_coherence.py | sha256sum
git show 97dcc7ab9ade29153faa0ccec428b78612cfc04a:sky_claw/local/native_parallax/research/solver_coherence.py | sha256sum
```

Intérprete: `E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe` (3.11.9). Los worktrees no
tienen `.venv` propio; `cwd` en el worktree del audit para que el paquete `sky_claw` se
resuelva desde ahí (verificado: `IMPORT_SOURCE_VERIFIED`).

Tests existentes (permitidos por §30):

```bash
REVAL_PYTHON="E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe"

"$REVAL_PYTHON" -m pytest -q tests/test_native_parallax_exp_m4.py \
  tests/test_native_parallax_exp_m5.py tests/test_native_parallax_spearman.py \
  tests/test_native_parallax_proxy_math.py        # 189 passed
```

---

## 9. Recomendación sobre PR #675

```
PR675_RECOMMENDATION = READY_FOR_DESIGN_RECONCILIATION
PR675_RECOMMENDATION_HISTORICAL_STATUS = SUPERSEDED
```

Ambas decisiones M4/M5 quedan `NOT_INVALIDATED`, que es la condición del brief para
habilitar la reconciliación. Esto **no** es un merge y **no** se modificó #675:
permanece `OPEN` + `DRAFT` + `UNCHANGED_BY_THIS_SLICE`.

> **Nota 2026-10-10 (post #700, SUPERSEDED).** El bloque anterior es el registro
> histórico de 2026-10-07. La recomendación **vigente** es
> `PR675_RECOMMENDATION = KEEP_DRAFT_BLOCKED` (§12). `NOT_INVALIDATED` no se
> relabeló a `REVALIDATED` y no autoriza implementar M6.

---

## 10. Review adversarial independiente (§29)

`ARENA_REVIEW = PASS_WITH_FINDINGS`. Revisión con contexto fresco sobre los artefactos y el
repositorio, con instrucción explícita de refutar las ocho tesis centrales. Reprodujo
independientemente ancestrías, hashes, números, alcanzabilidad y los 189 tests, y **no
encontró ningún contraejemplo que cambie una conclusión**. Los cuatro hallazgos son de
precisión de redacción/etiquetado:

| # | Hallazgo | Severidad | Adjudicación |
|---|---|---|---|
| F1 | El rango `fe54e9a9..1a52c3ea` también cambia `run_exp_m4.py` (#643), no sólo `authored_dataset.py` | COSMETIC | **ACCEPTED** — es no numérico y no se activó (0 exclusiones, 31/31). Corregido en §3.3, `provenance.json` y `M4_RERUN_RATIONALE` |
| F2 | `callgraph-evidence.json` etiquetaba `metrics.py:302` como `normal_only_features`; es `reconstruct_case`, no invocado por ningún runner | COSMETIC | **ACCEPTED** — etiqueta corregida; el veredicto `NO_PATH` no cambia |
| F3 | "El único uso de spearman en M4 es `coherence_diagnostic`" es impreciso: `metrics.compute_all` también lo calcula en el path primario | COSMETIC | **ACCEPTED** — corregido en §3.5 e `impact-matrix.json`; la clave es descartada por `evaluate_path`, así que la conclusión no cambia |
| F4 | "M4 importa de `run_exp_m2` únicamente `spearman` y `split_of`" omite `reconstruct_from_normal` (vía `solver_coherence`) | COSMETIC | **ACCEPTED** — corregido en §3.6; REPRO-A no lo toca |

Contratos que la review buscó **sin encontrar contraejemplo**: imports transitivos,
`FEATURE_FUNCS`, `SIGMA_EFF_CANDIDATES`, `normals_from_gradients`, los cuatro nombres que
M4/M5 importan de `run_exp_m3`, `OracleOnly.fit_global_scale`/`evaluate`, sobreafirmación de
"revalidado", y desbloqueo prematuro de M6/PR #675.

Ningún hallazgo altera el veredicto de impacto ni la recomendación sobre #675.

---

## 11. Contenido de este directorio

```
README.md                      este documento
impact-matrix.json             matriz de impacto + ancestria + semantica de valores + adjudicaciones
provenance.json                SHAs, hashes de artefactos, RAW usados (path/bytes/sha256/mtime UTC)
callgraph-evidence.json        call graph por conclusion + alcanzabilidad por correccion + diagnosticos
post-700-reconciliation.json   reconciliación documental posterior a #700 (2026-10-10)
```

No se copia corpus ni RAW binario. Los RAW bajo `C:\SkyClawResearch\` se leyeron y
hashearon **sin mutar**. `impact-matrix.json`, `provenance.json` y
`callgraph-evidence.json` conservan el blob de `eb066a1` (hashes en
`post-700-reconciliation.json`).

---

## 12. Reconciliación posterior a PR #700 (2026-10-10)

#700 (`e7c96094`, MERGED) investigó una pregunta **distinta** a la de este PR.
Las dos auditorías no se mezclan.

| | #697 (este PR) | #700 (main) |
|---|---|---|
| Pregunta | ¿REVAL-1 invalidó M4/M5? | ¿H1–H5 afectan decisiones anteriores? |
| Autoridad numérica | `impact-matrix.json` (`eb066a1`) | `corrective-adjudication.json` |
| M4/M5 primary | `NOT_INVALIDATED` | `NOT_INVALIDATED` |
| Relabel a `REVALIDATED` | **no** | **no** |
| `PR697_DISPOSITION` | — | `STILL_VALID_NARROW_SCOPE` |
| `PR675_RECOMMENDATION` | histórica `READY_FOR_DESIGN_RECONCILIATION` | vigente `KEEP_DRAFT_BLOCKED` |

```
PR697_DISPOSITION              = STILL_VALID_NARROW_SCOPE
PR697_NARROW_SCOPE_PRESERVED   = YES
M4_PRIMARY_STATUS              = NOT_INVALIDATED
M5_PRIMARY_STATUS              = NOT_INVALIDATED
M4_DECISION_NOT_SILENTLY_RELABELED = YES
M5_DECISION_NOT_SILENTLY_RELABELED = YES
SCIENTIFIC_RESULT_CHANGED      = NO
H1_CONVERGED_ASSETS            = 31
H1_UNRESOLVED_ASSETS           = 0

PR675_RECOMMENDATION_HISTORICAL        = READY_FOR_DESIGN_RECONCILIATION
PR675_RECOMMENDATION_HISTORICAL_STATUS = SUPERSEDED
PR675_RECOMMENDATION                   = KEEP_DRAFT_BLOCKED
PR675_RECOMMENDATION_STATUS            = CURRENT
PR675_RECOMMENDATION_CONSISTENT        = YES
M6_IMPLEMENTATION_BLOCKED              = YES
READY_FOR_M6_IMPLEMENTATION            = NO
PR675_CHANGED                          = NO
HISTORICAL_EVIDENCE_UNCHANGED          = YES
MAIN_CONTAINS_PR700                    = YES
```

Tres decisiones independientes (A no implica B ni C):

| # | Pregunta | Veredicto |
|---|---|---|
| **A** | ¿La conclusión estrecha de #697 sigue vigente? | **SÍ** — `STILL_VALID_NARROW_SCOPE` |
| **B** | ¿Se puede abrir la reconciliación documental de #675? | **NO** — `KEEP_DRAFT_BLOCKED` |
| **C** | ¿Se puede implementar M6? | **NO** — `M6_IMPLEMENTATION_BLOCKED=YES` |

### 12.1 Por qué cambió la recomendación sobre #675

#697 recomendó `READY_FOR_DESIGN_RECONCILIATION` porque REVAL-1 no invalidó M4/M5,
que era la condición de *ese* brief para un slice documental de #675. #700 no
contradice esa medición. Cambia la recomendación **por otra causa**, documentada
en `decision-impact.md` §9:

1. El slice de #675 se puso en espera hasta terminar la auditoría H1–H5.
2. H4 **sí** es un defecto real en el camino primario AUTH
   (`authored_dataset.resize_normal`, hermano uint8 de `resize_height` / #653).
3. El contrafactual C2 (31 assets, una sola variable) midió impacto
   `NUMERICAL_NOT_DECISIONAL`: decisión y reglas M4 idénticas; C1 y los dos
   componentes evaluables de C2 de M5 idénticos y lejos de umbral.
4. Antes de desbloquear el diseño hay que decidir si el fix de `resize_normal`
   entra en el alcance de la reconciliación de M6.
   `H4_RESIZE_NORMAL_DESIGN_DEPENDENCY = UNRESOLVED`.

`READY_FOR_DESIGN_RECONCILIATION` **no** significa `READY_FOR_M6_IMPLEMENTATION`.
Tampoco las dos recomendaciones son el mismo estado con otro nombre.

### 12.2 Limitación LEGACY_HELDOUT

El contrafactual C2 de #700 declara que C2 de M5 exige además la réplica
direccional en `LEGACY_HELDOUT`, y que ese término **no** es reproducible en el
slice READ-ONLY (no hay artefacto de filas M5 en el repo).
`M5_LEGACY_HELDOUT_LIMITATION = DECLARED_NOT_IN_C2_COUNTERFACTUAL`.

M5 ya arrastraba `UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE` (§7), ajena a
REVAL-1. El diseño M6-A.1 (#675, `e6b81317`) ya declara
`LEGACY_HELDOUT_IS_INDEPENDENT_VALIDATION = NO`. Esta sesión no cierra esa
limitación.

### 12.3 Qué falta antes de tocar #675

- Decidir `H4_RESIZE_NORMAL_DESIGN_DEPENDENCY` (incluir o excluir el fix de
  `resize_normal` del alcance de diseño, con evidencia).
- Conservar `LEGACY_HELDOUT` como limitación heredada, no como validación
  independiente.
- Los contratos propios de #675 que esta sesión **no** reabre: thresholds
  (`T_N`, `T_R`, `G`, `NUMERICAL_ENERGY_FLOOR`, `T_AMBIG`) sin congelar;
  Hodge como diagnóstico, no como reconstrucción; `curl_proxy` histórico
  bloqueado; corpus no blind.
- Worktree nuevo bajo `<repo>/.worktrees/`, rama
  `research/native-parallax-exp-m6-pair-mismatch-decomposition`.
  **No** implementar M6. **No** marcar #675 Ready.

Siguiente sesión propuesta: `PR675_DESIGN_RECON_PREFLIGHT`. Detalle
machine-readable en `post-700-reconciliation.json`.
