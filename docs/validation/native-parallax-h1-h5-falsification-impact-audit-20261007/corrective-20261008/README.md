# Native Parallax — Auditoría correctiva H1/H4 (PR #700)

> **Fecha:** 2026-10-08 · **Carácter:** `CORRECTIVE_AUDIT` · **Ronda:** 2
> **Padre de la corrección:** `d260b7c9b8afff33a6720106ac8c5f6d95891ecd`
> **Base científica original:** `97dcc7ab9ade29153faa0ccec428b78612cfc04a`
> **Rama:** `research/native-parallax-h1-h5-falsification-impact-audit`
> **Worktree:** `E:\SkyClaw_H1H5_AUDIT_CORRECTION_d260b7c9`
> **Sesión:** exclusiva. No se reutilizó `E:\SkyClaw_H1H5_AUDIT_97dcc7ab`.

Esta carpeta **corrige** —no reescribe— la auditoría de
`../` (2026-10-07). Los artefactos originales quedan intactos. Donde la evidencia
corregida contradice la conclusión original, **se corrige la conclusión**.

> **Segunda ronda (2026-10-08, misma sesión).** Tras la promoción a Ready, el review
> abrió 8 findings nuevos. Los 8 se reprodujeron y los 8 resultaron válidos. Esta ronda
> endurece el optimizador (D, E), desdobla la adjudicación de H4 (C), fija la identidad
> del roster (F) y ajusta las afirmaciones del texto (A, B, G, H). Los valores de H1
> **cambiaron** donde correspondía: ver §2 y §2.2.
>
> **Tercera ronda (2026-10-09, misma sesión).** Un finding nuevo sobre el HEAD `2e13118`:
> el digest congelado del roster histórico se **calculaba** pero **no participaba del hard
> stop**, así que un artefacto histórico alterado junto con un corpus alterado de forma
> consistente pasaba el gate. Confirmado y corregido: gate **fail-closed** de cuatro
> condiciones, renombre del booleano engañoso y contrato **separado** para el archivo del
> manifiesto M3 (digest canónico LF). Ver §4.
>
> **Cuarta ronda (2026-10-09, sesión de cierre final de #700).** Un finding nuevo sobre
> `scripts/corrective_optimizer.py:433`: `valid_minimum_bracket` se marcaba con la sola
> condición `lo < b_mid < hi`, que prueba que el punto medio es **interior al intervalo**
> pero **no** que exista evidencia de un mínimo interior. Reproducido (un objetivo plano o
> monótono salía con `VALID_MINIMUM_BRACKET = YES`) y corregido con el bracketing estándar
> de tres puntos. **Impacto científico medido: `NONE`** — la evidencia correctiva se
> re-ejecutó completa y todos los valores científicos son idénticos; ver §2.3.
>
> **Quinta ronda — micro-slice final (2026-10-09, misma sesión/worktree).** Dos findings
> materiales **fuera del diff inline** (por eso no tenían thread): (1) `--c2` era opcional
> en el builder y su ausencia no impedía publicar `M4_PRIMARY_STATUS` / `M5_PRIMARY_STATUS`
> como `NOT_INVALIDATED` — contradice el contrato fail-closed; (2) el C2 registraba
> `m4.decision_changed` pero el builder **no lo leía**, así que un cambio de decisión M4 no
> se propagaba. Ambos reproducidos y corregidos: C2 **obligatorio** y **fail-closed**, y
> propagación de `m4.decision_changed`. **`SCIENTIFIC_RESULT_CHANGED = NO`**: con la
> evidencia C2 real (`decision_changed = false`) la adjudicación queda idéntica; ver §4.1.

---

## 1. Qué se corrigió y por qué

Cinco findings materiales quedaron abiertos en el review del PR #700. Los cinco están
confirmados. Esta corrida los resuelve metodológicamente.

| # | Defecto confirmado | Corrección aplicada |
|---|---|---|
| **F1** | El supuesto oráculo "continuo" era un **barrido finito** (`s0·exp2(linspace) ∪ linspace(-0.05,0.05)`) sin bracketing ni convergencia. `polyhaven_gray_rocks` caía en el borde `0.05` con cero mejora. | `scripts/corrective_optimizer.py`: barrido grueso log-uniforme + detección de cuencas + bracketing + **sección áurea** + expansión de borde + metadatos de convergencia. Determinista, sin SciPy. **Alcance: mínimo local dentro de cada cuenca detectada — NO optimalidad global** (ver §5). |
| **F2** | La narrativa mezclaba el `+0.05` del baseline recalculado con el `-0.05` del histórico `fe54e9a9`, y afirmaba "la rejilla nunca encuentra el óptimo" sin haberlo medido de verdad. | Se distinguen explícitamente los tres universos y se emite el **conteo real** de matches antes de afirmar nada. |
| **F3** | El Spearman era **híbrido**: ángulos recalculados en el baseline contra `delta_rmse` del artefacto histórico no-ancestro. | `scripts/phase_c_corrective.py` emite dos universos separados: `BASELINE_COUNTERFACTUAL` (delta recomputado) y `HISTORICAL_COMPARISON` (etiquetado híbrido). |
| **F4** | La sonda H4 probaba amplitudes `[1.0, 0.3, 0.1, 0.03]`, pero el claim externo se midió en `c = 0.05` y `c = 0.01`. El efecto es no lineal ⇒ no falsaba el claim citado. | Batería `[1.0, 0.3, 0.1, 0.07, 0.05, 0.03, 0.02, 0.01]`, con `0.05` y `0.01` **obligatorias**. |
| **F5** | `h_nat` centrado tiene negativos; `resize_height` clipea a `[0,1]`; pero `n_nat = self_forward(h_nat)` sale del campo **sin** clipear ⇒ el height target y la superficie de las normales **no eran la misma superficie**. | `safe_surface()`: `h_safe = h_nat + offset` sin scaling (∇(h+c) = ∇h preserva pendientes). Casos fuera de rango se marcan inválidos; **no se clipea en silencio**. Invariantes verificados por test. |
| **F6** | `valid_minimum_bracket` se marcaba con la sola condición `lo < b_mid < hi`. Eso prueba que el punto medio es **interior al intervalo**, no que exista evidencia de un mínimo interior: un objetivo plano o monótono salía con `VALID_MINIMUM_BRACKET = YES`. | `scripts/corrective_optimizer.py::is_valid_minimum_bracket()`: exige el bracketing estándar de tres puntos (`f(mid) < f(lo) − tol` **y** `f(mid) < f(hi) − tol`, con `tol = metric_tolerance`). Cubierto por tests de plano, monótono en ambos sentidos, casi-plano, borde, mínimo interior y kink. Ver §2.3. |

---

## 2. Resultado H1 — la conclusión cambia de magnitud, no de existencia

Corrida corregida sobre los 31 assets del corpus primario (SHA256 verificado, 0 exclusiones):

| Métrica | Sonda vieja (barrido finito) | **Correctiva (optimizador real)** |
|---|---|---|
| `H1_CONVERGED_ASSETS` | n/a (sin criterio de convergencia) | **31 / 31** |
| `H1_UNRESOLVED_ASSETS` | n/a | **0** |
| `H1_BOUNDARY_CASES` | ≥1 (`gray_rocks` en el borde `0.05`) | **0** |
| mediana agreement continuo | 1.6832° | **1.6826°** |
| mediana agreement rejilla | 10.7085° | 10.7085° |
| `n_continuous_abs_strength_lt_0_05` | 27 / 31 | **27 / 31** |
| `n_grid_matches_refined` | no medido | **0** (coincidencia dentro de `1e-06°`) |
| `n_refinement_lost_better_coarse` | n/a | **0** |

**Lectura honesta:**

- El defecto de H1 **se confirma** y ahora está medido con un optimizador que converge
  **dentro del bracket seleccionado** en los 31 assets. La mejora (10.71° → 1.68°)
  corresponde a los **mínimos locales encontrados**; **no** demuestra un mínimo global.
  Ver §5 para el alcance exacto de la búsqueda.
- La afirmación "la rejilla nunca coincide con el mínimo encontrado" pasa de **no
  soportada** a **soportada**: `n_grid_matches_refined == 0` con tolerancia declarada de
  `1e-06°`, **y** los 31 casos convergen con bracket válido y criterio de parada cumplido.
  Eso **no** demuestra que la rejilla nunca encuentre el mínimo **global**.
- **`gray_rocks` deja de ser un caso de borde dentro del bracket seleccionado**: su mínimo
  encontrado está dentro del dominio, no clavado en `0.05`. El borde era un artefacto de
  la rejilla dentro de ese alcance.
- El conteo `|s*| < 0.05` **baja de 28 a 27** respecto de la primera ronda correctiva:
  `gray_rocks` tenía publicado `s* = 0.0237` (dentro del piso) y con el optimizador
  corregido su óptimo real es `s* = 0.0926` (fuera del piso). El conteo anterior era un
  artefacto del defecto D, no una propiedad del corpus.

**El impacto primario sigue siendo `NONE`**: `coherence_diagnostic` de M4 es
explícitamente no decisional, `decide_exp_m3` no lee Cohort B, y `evaluate_rules` de M4
no lee el oráculo. La corrección cambia la **magnitud del diagnóstico**, no una decisión.

### 2.1 Alcance de la búsqueda — qué prueba y qué NO prueba este optimizador

El brief §23 exige no usar la palabra "global" sin cobertura global suficiente. Este
optimizador **no** la tiene, y el artefacto lo declara explícitamente:

```text
search_scope            = COARSE_GLOBAL_DOMAIN_LOCAL_REFINEMENT
global_optimum_proven   = false
```

La distinción operativa:

| Concepto | Estado |
|---|---|
| Dominio del barrido grueso | Declarado y acotado (`coarse_max_magnitude = 4.0`, 6 décadas hacia abajo) |
| Cuencas detectadas por el barrido | Refinadas todas las que el barrido ve (hasta `max_refined_basins = 8`) |
| Mínimo **local** dentro de cada cuenca refinada | Sí, con bracket válido y criterio de parada cumplido |
| **Óptimo global** | **NO probado.** Una cuenca más angosta que el espaciado del barrido grueso puede pasar desapercibida |

Por lo tanto las afirmaciones de §2 son sobre **mínimos locales encontrados dentro del
alcance barrido**, y así deben citarse. `GLOBAL_OPTIMUM_PROVEN = NO` **no invalida H1**:
el defecto que H1 denuncia (la rejilla finita no explora el interior) se sostiene sin
necesidad de optimalidad global.

### 2.2 Invariante de corrección del refinamiento (hallazgo D de la ronda 2)

```text
final_objective <= best_observed_objective   (+ tolerancia numérica)
n_refinement_lost_better_coarse = 0 / 31
```

El refinamiento **jamás** puede reemplazar un resultado mejor ya observado por uno peor.
En la primera ronda esto se violaba en 5 de 31 assets, incluido `polyhaven_gray_rocks`
(coarse `0.08954885 @ 17.024981969` publicado como `0.02373315 @ 23.695281265`).
Corregido: ver §2 y la tabla de cambios.

### 2.3 Contrato del bracket mínimo (cuarta ronda, finding F6) e impacto sobre H1

`VALID_MINIMUM_BRACKET` no significa "el punto medio cae dentro del intervalo": significa
**hay evidencia de un mínimo interior bracketed**. El contrato es el bracketing estándar de
tres puntos, con la tolerancia de métrica del propio optimizador:

```text
lo < mid < hi
AND f(mid) < f(lo) - metric_tolerance
AND f(mid) < f(hi) - metric_tolerance
```

Para `f` continua eso **implica** un mínimo local estrictamente dentro de `(lo, hi)`: el
mínimo de `f` sobre `[lo, hi]` no puede estar en `lo` ni en `hi` (ambos son peores que
`mid`), así que cae en el interior. La condición `lo < mid < hi` **sola no alcanza**.

Reproducción del defecto sobre el código previo al fix — objetivos **sin** mínimo interior
que igual recibían `VALID_MINIMUM_BRACKET = YES`:

| objetivo | status | `valid_minimum_bracket` (antes) | (después) |
|---|---|---|---|
| `f = 1.0` (plano) | `FLAT_OBJECTIVE` | **True** | `False` |
| `f = 10 + 5s` (monótono ↑) | `MAX_EXPANSIONS` | **True** | `False` |
| `f = 10 − 5s` (monótono ↓) | `MAX_EXPANSIONS` | **True** | `False` |
| `f = 2 + 1e-14·sin(1000s)` (casi plano) | `FLAT_OBJECTIVE` | **True** | `False` |

**Impacto sobre H1: `NONE`.** Se re-ejecutó `phase_c_corrective.py` completo (31 assets,
corpus `EXP-M3`, READ-ONLY) con el optimizador corregido y se comparó campo por campo
contra la evidencia congelada:

| métrica | evidencia congelada | re-ejecución con el fix |
|---|---|---|
| `n_converged` | 31 | **31** |
| `n_unresolved` | 0 | **0** |
| `n_valid_minimum_bracket` | 31 | **31** |
| `n_stop_criterion_met` | 31 | **31** |
| `n_continuous_abs_strength_lt_0_05` | 27 | **27** |
| `n_grid_matches_refined` | 0 | **0** |
| mediana agreement (rejilla / continuo) | 10.7085° / 1.6826° | **10.7085° / 1.6826°** |
| Spearman (rejilla / continuo) | 0.1367 / 0.4782 | **0.1367 / 0.4782** |

Los **12** contadores de `counts_16` y **todos** los campos escalares por asset
(`continuous_best_strength`, `continuous_agreement_deg`, `grid_*`, `delta_*`, `split`,
`replica_abs_diff_deg`) son idénticos. La única diferencia es
`continuous_meta.n_evaluations` (+3 por bracket validado, hasta +27 en un asset): el
chequeo de validez evalúa `f` en los tres puntos del bracket. Es **instrumentación**, no un
resultado, y no entra en la lista de campos científicos de §17 del brief.

Por eso la evidencia correctiva **no se regenera** (el brief pide regenerar sólo si cambia
un resultado científico) y `evidence/` queda congelado en la ejecución del 2026-10-08. Para
reproducir el experimento con el script actual: mismo comando, mismos valores científicos,
`n_evaluations` mayor.

---

## 3. Resultado H4 — el claim externo se reproduce parcialmente (y antes no)

Con la superficie consistente (F5) y las amplitudes correctas (F4), en `c = 0.05` y `c = 0.01`:

| Caso | Brazo clipeado (sonda vieja) | **Brazo corregido** | Claim externo |
|---|---|---|---|
| `S07_bumps@0.05` | ratio 1.000× | **27.99×** | 5–21× |
| `S07_bumps@0.01` | ratio 0.999× | **82.12×** | 5–21× |
| `S15_periodic_noise@0.05` | ratio 1.000× | **3.52×** | 5–21× |
| `S15_periodic_noise@0.01` | ratio 1.001× | **18.97×** | 5–21× |

**El veredicto original `NOT_REPRODUCED` era un artefacto de dos defectos combinados.**
El brazo clipeado daba ratio ≈ 1.0 porque el clip `[0,1]` dominaba la señal y enmascaraba
por completo el efecto de la cuantización uint8. Al medir sobre una superficie coherente
y a las amplitudes que el claim realmente usó, los ratios **sí** aparecen.

`H4_EXTERNAL_MAGNITUDE = PARTIALLY_REPRODUCED`: hay casos dentro de la banda 5–21×
(`S15@0.01` con 19.0×) y casos por encima (`S07@0.01` con 82×). Eso es consistente con un
efecto no lineal que crece al bajar la amplitud — exactamente lo que el finding F4 decía
que las amplitudes viejas no podían capturar.

### 3.1 El claim tiene DOS componentes y se adjudican por separado (hallazgo C, ronda 2)

El claim externo registrado no era sólo relativo. Textualmente:

```text
"5-21x mayor ... y llega a 0.023 absoluto"
```

La primera ronda clasificaba **sólo el ratio** y publicaba un único
`PARTIALLY_REPRODUCED`, lo que se lee como si el componente absoluto también hubiera
quedado reproducido. **No lo está.** Adjudicación desdoblada:

| Componente | Veredicto |
|---|---|
| `H4_RATIO_CLAIM_STATUS` (relativo, adimensional) | **PARTIALLY_REPRODUCED** |
| `H4_ABSOLUTE_CLAIM_STATUS` (absoluto, contra `0.023`) | **NOT_REPRODUCED** |

Deltas absolutos medidos en el brazo de superficie consistente, en los puntos de
operación del propio claim (`c = 0.05`, `c = 0.01`):

| Caso | delta absoluto |
|---|---|
| `S07_bumps@0.05` | 5.39e-05 |
| `S07_bumps@0.01` | 3.23e-05 |
| `S15_periodic_noise@0.05` | 7.09e-06 |
| `S15_periodic_noise@0.01` | 1.00e-05 |

El **mejor** caso está **427× por debajo** de `0.023`. Un ratio adimensional alto **no**
implica que el valor absoluto se haya reproducido: son afirmaciones independientes y
acá tienen veredictos distintos.

Criterio declarado (no implícito): dentro de 2× del valor reclamado ⇒ `REPRODUCED`;
dentro de 10× ⇒ `PARTIALLY_REPRODUCED`; más allá ⇒ `NOT_REPRODUCED`.

### 3.2 Hallazgo nuevo: la materialidad del sintético también cambia

Con la superficie corregida aparece un resultado que la sonda original no podía ver:

| brazo | máx `abs(downstream delta)` | casos `>= T_DELTA_RMSE (0.02)` |
|---|---|---|
| clipeado (viejo) | 0.0162 | **0** |
| **superficie segura (nuevo)** | **0.0353** | **1** (`S09_bricks@1.0`, ratio 1.58×) |

La afirmación "no material por la letra (0.0162 < 0.02)" del sintético **queda superada**: el
valor real es `0.0353 >= 0.02`. En ese caso el `normal_rmse_old_vs_ideal` cae de **0.559**
(clipeado) a **0.210** (seguro) — el clip inflaba el error medido 2.7×.

**Esto no invalida M4/M5.** El umbral `T_DELTA_RMSE` gobierna el `delta_rmse` del **corpus
real**, no del sintético; y el contrafactual real de una sola variable (Fase C2, re-ejecutado
en esta corrección) **no usa superficies sintéticas clipeadas**.

---

## 4. Contrafactual del corpus real (Fase C2) — re-verificado

§23 del brief exigía **no** asumir que F5 invalida el contrafactual real, sino revalidarlo.
Se re-ejecutó el script C2 con un **gate fail-closed** antes de calcular M4/M5. El gate exige
**cuatro** condiciones, cada una **exactamente** `True` (una clave ausente o un valor
truthy-no-booleano bloquea):

| Condición | Qué ancla |
|---|---|
| `roster_count_match` | hay 31 assets |
| `roster_identity_match` | el conjunto de `asset_id` es exactamente el histórico |
| `historical_roster_digest_matches_frozen_sha` | el digest del **roster histórico** contra `HISTORICAL_ROSTER_SHA256 = a3ddcced…` |
| `m3_manifest_sha256_matches_frozen` | el digest del **archivo** del manifiesto M3 contra `EXPECTED_M3_MANIFEST_SHA256_LF = b0f5a4c6…` |

Las dos últimas anclan objetos **distintos** y no se mezclan: una fija la identidad del roster
del artefacto histórico, la otra la identidad del archivo que define el corpus. La ronda 2
calculaba la segunda pero **no la usaba en la decisión** (finding de la ronda 3): un artefacto
histórico alterado *junto con* un corpus alterado de forma consistente pasaba el gate con
`count=true` e `identity=true`. Además, el booleano se llamaba `manifest_digest_match` cuando en
realidad comparaba el roster histórico — nombre que se corrigió.

**Convención EOL (explícita).** El digest del manifiesto se congela sobre bytes canónicos **LF**
(`b0f5a4c6604989269647973b6a6e6b899e859b436e7b42bede7e3297d10e6d6f`), **no** sobre el checkout
CRLF de Windows (`c9c1665942281966ddeb4f4ff05ed9e2be302d80155cfa8bfc1e487c2bfbecde`, 76217 bytes).
El repo ya documenta esa relación
(`docs/design/research/native-parallax/m2-m3-math-revalidation-protocol.md`; `data/exp-m4-data-required.json`
registra el valor LF). Congelar el CRLF habría roto el gate en un checkout Linux. La evidencia
emite ambos, etiquetados.

Con cualquiera de las cuatro condiciones en falso el script **no decide** (HARD STOP) y la
adjudicación publica `ROSTER_GATE_ALLOWED` / `ROSTER_GATE_FAILED_CONDITIONS` junto a los
booleanos crudos.

```
M4 decision: EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT -> EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT  changed=False
M4 delta_rmse median: 0.03506 -> 0.03508   (max |change| per asset 0.03543)
M5 C1: False -> False
M5 excess_lowmid: 0.50820 -> 0.44682 | excess_high: 0.33504 -> 0.33491
```

La decisión de M4 y `C1` de M5 **no cambian** entre brazos. `H4_M4_PRIMARY_IMPACT` y
`H4_M5_PRIMARY_IMPACT` permanecen `NUMERICAL_NOT_DECISIONAL`.

### 4.1 La adjudicación final exige un C2 válido (quinta ronda, micro-slice final)

**Final adjudication requires valid C2 evidence. Missing/invalid C2 is fail-closed and
cannot publish M4/M5 statuses.**

El gate de identidad de roster (§4) vive en `phase_c2_h4_corpus_corrective.py`, pero el
builder de la adjudicación (`scripts/build_corrective_evidence.py`) lo trataba como
opcional:

```python
# ANTES (defecto confirmado)
ap.add_argument("--c2", type=Path, default=None)
...
if args.c2 is not None and args.c2.exists():   # ausente => sigue igual
    ...
# y publicaba M4_PRIMARY_STATUS / M5_PRIMARY_STATUS sin ninguna evidencia de roster
```

Reproducción del defecto sobre el código previo: correr el builder **sin** `--c2`
devolvía `rc=0` y escribía una adjudicación con `M4 = NOT_INVALIDATED`,
`M5 = NOT_INVALIDATED` y `ROSTER_GATE_ALLOWED = null`. El contrato corregido:

```text
--c2 es OBLIGATORIO (argparse required=True)
el archivo debe existir                 -> si no: HARD STOP (SystemExit 3)
validate_c2_for_adjudication() exige:
    roster_identity is dict
    roster_gate is dict
    roster_gate.allowed is True
    cada una de las cuatro condiciones is True
    m4.decision_changed legible como bool
si falla cualquiera -> HARD STOP (SystemExit 3); NO se publica adjudicación parcial
```

Las cuatro condiciones son **exactamente** las del gate de §4 y se validan con
`is True`: una clave ausente, `None`, `1` o un string truthy bloquean (fail-closed).

**Propagación de la decisión M4.** El C2 registra `m4.decision_changed`; el builder ahora
lo lee. La regla, explícita y con el gate de H4 intacto:

```text
H4_M4_PRIMARY_IMPACT=DECISIONAL  OR  H4_M5_PRIMARY_IMPACT=DECISIONAL
    -> M4 = UNRESOLVED, M5 = UNRESOLVED          (comportamiento previo, NO se debilita)
si no:
    C2 m4.decision_changed is True
        -> M4 = UNRESOLVED, M5 = NOT_INVALIDATED (hallazgo exclusivamente M4 no mueve M5)
    C2 m4.decision_changed is False
        -> M4 = NOT_INVALIDATED, M5 = NOT_INVALIDATED
```

Con la evidencia C2 real (`m4.decision_changed = false`) la adjudicación es la misma que
antes: `M4_PRIMARY_STATUS = NOT_INVALIDATED`, `M5_PRIMARY_STATUS = NOT_INVALIDATED`. El
artefacto `corrective-adjudication.json` se regeneró sólo para incorporar los campos del
contrato (`C2_VALIDATED_FOR_ADJUDICATION`, `C2_M4_DECISION_CHANGED`,
`C2_REQUIRED_ROSTER_CONDITIONS`, `SCIENTIFIC_RESULT_CHANGED = NO`); ninguna clave previa
cambió y `H1`/`H4` quedaron idénticos byte a byte.

---

## 5. Adjudicación final

```
H1_IMPLEMENTATION_DEFECT     = CONFIRMED
H1_CONTINUOUS_OPTIMIZATION   = VALID          (31/31 convergen, 0 unresolved)
H1_GRID_MATCHES_CONTINUOUS   = 0
H1_GRID_NEVER_FINDS_OPTIMUM  = TRUE           (0 matches Y todo converge)
H1_VALID_MINIMUM_BRACKET     = 31             (con evidencia de mínimo interior, F6)
H1_MEDIAN_GRID_AGREEMENT_DEG = 10.7085
H1_MEDIAN_CONT_AGREEMENT_DEG = 1.6826
H1_ABS_STRENGTH_LT_0_05      = 27
H1_PRIMARY_IMPACT            = NONE

H4_IMPLEMENTATION_DEFECT        = CONFIRMED
H4_SYNTHETIC_SURFACE_CONSISTENT = YES         (sobre todos los casos aplicables)
H4_EXTERNAL_MAGNITUDE           = PARTIALLY_REPRODUCED
H4_M4_PRIMARY_IMPACT            = NUMERICAL_NOT_DECISIONAL
H4_M5_PRIMARY_IMPACT            = NUMERICAL_NOT_DECISIONAL

M4_PRIMARY_STATUS = NOT_INVALIDATED
M5_PRIMARY_STATUS = NOT_INVALIDATED
C2_VALIDATED_FOR_ADJUDICATION = YES      (fail-closed: sin C2 válido no se publica)
C2_M4_DECISION_CHANGED = FALSE
SCIENTIFIC_RESULT_CHANGED = NO
PR697_DISPOSITION = STILL_VALID_NARROW_SCOPE
PR675_RECOMMENDATION = KEEP_DRAFT_BLOCKED
M6_IMPLEMENTATION_BLOCKED = YES
CORRECTIVE_REPRODUCIBILITY = PASS
```

---

## 6. Qué NO se tocó

- **H2 / H3 / H5 sin cambios.** Se preservan `H2_BUG_CLASSIFICATION=NOT_PROVEN`,
  `H2_UNIT_CONTRACT=UV_NORMALIZED`, `H3_MECHANISM=CONFIRMED`,
  `H3_CONTRACTUAL_MEANINGFULNESS=SUPPORTED`, `H5_DIAGNOSTIC_DEFECT=CONFIRMED`,
  `H5_PRIMARY_M4_BLOCKER=NO`. Este slice no aprovecha el PR para limpiar H5.
- **Sin rerun completo de M4/M5.** No se re-ejecutó M4, M5, ni la reconstrucción
  LEGACY_HELDOUT, ni M6.
- **Sin cambios en código científico ni umbrales.** El corpus se leyó READ-ONLY.
- **Artefactos originales intactos.** `synthetic-evidence.json`, `real-impact.json`,
  `hypothesis-status.json` y `provenance.json` de la auditoría 2026-10-07 **no se
  modificaron**. La superación es explícita, no silenciosa.

---

## 7. Contenido de esta carpeta

```
README.md                        este documento
corrective-provenance.json       SHAs, hashes, qué se corrigió y por qué
corrective-adjudication.json     veredictos finales H1/H4 + M4/M5 + PR697/PR675
evidence/determinism.json        verificación de reproducibilidad (2 pasadas, bit a bit)
evidence/h1-corrected-evidence.json      salida cruda de la corrida H1
evidence/h4-corrected-evidence.json      salida cruda de la corrida H4
evidence/h4-corpus-counterfactual.json   Fase C2: contrafactual READ-ONLY del corpus real
scripts/corrective_optimizer.py             F1: minimizador escalar 1-D determinista
scripts/phase_c_corrective.py               F1+F3: oráculo continuo real sobre el corpus
scripts/h4_magnitude_probe_corrective.py    F4+F5: sonda de magnitud corregida
scripts/phase_c2_h4_corpus_corrective.py    Fase C2 + finding F: identidad de roster
scripts/verify_determinism.py               §28: verificación de reproducibilidad
scripts/build_corrective_evidence.py        Fase D: adjudicación consolidada (C2 obligatorio)
```

`build_corrective_evidence.py` exige `--c2 <evidence/h4-corpus-counterfactual.json>`:
sin un C2 válido **no** publica M4/M5 (fail-closed, ver §4.1).

Tests asociados (fuera de esta carpeta, en `tests/`):

```
tests/test_native_parallax_pr700_corrective_optimizer.py             F1: 16 tests
tests/test_native_parallax_pr700_corrective_optimizer_hardening.py   D/E/F6: 27 tests
tests/test_native_parallax_pr700_corrective_h4_invariants.py         F4/F5: 20 tests
tests/test_native_parallax_pr700_corrective_h4_claim_split.py        C: 8 tests
tests/test_native_parallax_pr700_corrective_roster_identity.py       F: 13 tests
tests/test_native_parallax_pr700_corrective_docs_invariants.py       G/H/I: 8 tests
tests/test_native_parallax_pr700_corrective_c2_fail_closed.py        C2 (ronda 5): 19 tests
```

Total: **111 tests correctivos** (`pytest -k pr700_corrective`).
