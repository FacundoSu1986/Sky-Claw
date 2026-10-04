# EXP-M5 — Frequency-Band Coherence del mismatch authored normal↔height

> **Research-only. NO productización. NO M6. DO NOT MERGE.**
> Preregistro en dos etapas (§protocolo): **ETAPA A** (diseño, committeado antes de tocar
> Cohort A) y **ETAPA B** (congelamiento de thresholds tras controles sintéticos). Este
> documento contiene AMBAS: el diseño (§1–§16) y el congelamiento ETAPA B (§17), con los
> thresholds derivados **solo** de matemática + piso SELF publicado en M4 + controles
> sintéticos M5 — **nunca** de Cohort A M5.

---

## 0. Contexto heredado (NO reinterpretar)

- **M3:** `EXP_M3_RECONSTRUCTION_CONDITIONAL`.
- **M4:** `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT`. En el corpus ensayado, el inversor FFT
  reconstruye height desde una normal **SELF-consistent** con RMSE mediana ~0.003746,
  |corr| ~0.999165, var ~0.998330; pero desde la **normal authored** el RMSE sube a
  ~0.039238, |corr| baja a ~0.839229. El solver NO fue el cuello de botella dominante; el
  par authored mostró pair/model mismatch bajo el modelo gradient-height ensayado.
- Post-M4 robustness hardening integrado (`file_unreadable`), `SCIENTIFIC_RERUN_REQUIRED: NO`.

**Interpretación permitida:** limitada al corpus ensayado, texture-space, y el modelo
gradient-height probado. **Prohibido:** “los normal maps están mal”, “los proveedores hacen
mal displacement”, “todas las texturas de Skyrim funcionan así”, “demostrado universalmente”.

---

## 1. Pregunta científica

¿El mismatch AUTH observado en M4 está concentrado principalmente en escalas de **alta
frecuencia / microdetalle**, dejando las escalas **macro/meso (low/mid)** relativamente
coherentes? ¿O el mismatch también está presente de forma material en low/mid, de modo que
simplemente quitar microdetalle no recuperaría un height fiable?

M5 **solo localiza espectralmente** el mismatch. No produce el mejor height, no implementa
ningún filtro productivo.

## 2. Hipótesis

- **H1 — BANDLIMITED RECOVERY:** la reconstrucción authored conserva razonablemente las
  bandas low/mid y el exceso de error se enriquece en high. (Justificaría *investigar* M6.)
- **H0/alternativa:** el error AUTH es broadband o degrada de forma significativa low/mid.
  (Un low-pass simple no quedaría justificado.)

**M5 NO asume H1. Intenta falsarla** (controles S1 high-only vs S2 low-only, §10/§13).

---

## 3. Corpus (§ reutilizado, NO fresco)

Se reutiliza **exactamente** Cohort A de M3/M4:

- 31 assets, 8 familias, Poly Haven 23 / ambientCG 8.
- Split histórico: **CALIBRATION = 15**, **HELD_OUT = 16**.
- Mismo manifest hash-verificado de M4. Revalidar SHA256; exclusiones objetivas
  (`file_missing` / `file_unreadable` / `sha256_mismatch` / `path_invalid`) registradas;
  **nunca** excluir por outcome.

> **CAVEAT DE REUTILIZACIÓN (§5 del brief):** los 16 HELD_OUT **ya fueron usados en M4**.
> Por eso en M5 se denominan **LEGACY_HELDOUT**, NO “fresh heldout” ni “independent
> validation”. Pueden permanecer ocultos a las métricas M5 hasta el freeze M5, pero no son
> una muestra externa nueva. Se declara explícitamente aquí.

---

## 4. Precondición matemática y pipeline primario (§7/§9)

Por asset, con la **misma matemática M4** (NO un segundo solver):

- `H` = height authored (target).
- `SELF` = height reconstruido desde la normal SELF-consistent
  (`self_forward(h, bits=8)` → `solve_normal`, RAW).
- `AUTH` = height reconstruida desde la normal authored (`solve_normal(mat.normal)`, RAW).

**Pipeline primario (§7): NO se filtra el normal antes del solver.** El mapa normal→(p,q)
usa división por nz y es no lineal; filtrar normals previamente cambiaría el problema.
Orden: `N_AUTH → solver M4 completo → H_AUTH_RECON → alineamiento global M4 → descomposición
espectral de H_TARGET, H_AUTH_RECON y residual`. Idéntico para SELF.

## 5. Alineamiento (§8) — regla crítica

**NO hay fit independiente por banda.** Por asset:

1. `scale = OracleOnly.fit_global_scale(H, rec)["affine_scale"]` (cov/var, con signo) — el
   **mismo** contrato de alineamiento global que M4.
2. `R = rec * scale` (una única escala global para TODAS las bandas del asset).
3. recién entonces se descomponen `H` y `R` en bandas.

**Prohibido:** per-band affine, per-band sign search, per-band nonlinear, per-band offset.
El offset DC no es observable y queda fuera del análisis confirmatorio.

---

## 6. Eje de frecuencia (§9)

Texture-space, normalizado por textura (resolution-invariant):

```
kx = fftfreq(W)·W ,  ky = fftfreq(H)·H ,  rho = sqrt(kx² + ky²)   # ciclos por tile
```

- `rho ≈ ciclos por tile/textura` (NO cm/m dentro de Skyrim).
- `rho` es par bajo (i,j)→(−i,−j) ⇒ máscaras radiales conjugado-simétricas.
- `f_r max` = (N/2)·√2 (esquina Nyquist). Ejes y diagonales cubiertos por la definición radial.

## 7. Bandas (§10/§11)

**DC:** `rho = 0` — excluido de la decisión.

**Agregados primarios (corte congelado = 32 ciclos/tile, §10):**

| Agregado | Rango |
|---|---|
| `LOWMID` | `0 < rho < 32` (= B1 ∪ B2 ∪ B3 ∪ B4) |
| `HIGH`   | `rho >= 32` (= B5 ∪ B6 ∪ B7) |

**Bandas diagnósticas octave-like disjuntas (§11), convención de borde `[lo, hi)`:**

| Banda | Rango (rho) |
|---|---|
| B1 | `0 < rho < 4` |
| B2 | `4 <= rho < 8` |
| B3 | `8 <= rho < 16` |
| B4 | `16 <= rho < 32` |
| B5 | `32 <= rho < 64` |
| B6 | `64 <= rho < 128` |
| B7 | `rho >= 128` |

A 512², 32 ciclos/tile ≈ longitud de onda ~16 px. **El corte 32 se fija ANTES de mirar M5
real; no se busca “el mejor cutoff” después.**

## 8. Máscaras (§12/§13)

Duras, deterministas, **disjuntas**, **exhaustivas salvo DC**, **conjugado-simétricas**, sin
smoothing adaptativo. `union(B1..B7) = NONDC`; `intersection(Bi,Bj) = ∅`. Verificado por test
(partición, solapamiento cero, IFFT real). Smooth/windowed bands, si se usan, son **SECONDARY
preregistrado** y nunca cambian la decisión primaria.

## 9. Resolución (§28)

- **Primary: 512²** (continuidad con M4).
- **Secondary: 1024² native / no-resize** (control del confundo de resize de M4).
- El corte 32 ciclos/tile **no cambia** entre resoluciones (rho normalizado por textura).
- CI/sintéticos: 64/128 px cuando el objetivo matemático no dependa de N. **Nunca** correr el
  corpus real a 64.

---

## 10. Métricas por banda (§14/§16/§17)

Con `R` ya globalmente alineada, para cada banda `b`:

```
TARGET_ENERGY_b       = Σ |FFT(H)_b|²
RESIDUAL_AUTH_ENERGY_b= Σ |FFT(R_AUTH − H)_b|²
RESIDUAL_SELF_ENERGY_b= Σ |FFT(R_SELF − H)_b|²

NRMSE_AUTH_b = sqrt(RESIDUAL_AUTH_ENERGY_b / TARGET_ENERGY_b)   # PRIMARY por banda
NRMSE_SELF_b = sqrt(RESIDUAL_SELF_ENERGY_b / TARGET_ENERGY_b)
EXCESS_NRMSE_b = NRMSE_AUTH_b − NRMSE_SELF_b                    # resta el piso solver/Q8
```

Secundarias por banda: `|corr|` (coherencia compleja
`|Σ H_b·conj(R_b)| / sqrt(Σ|H_b|²·Σ|R_b|²)`), `target_energy_fraction`,
`recon/target energy ratio`. **Ninguna secundaria sustituye la primary post-hoc.**

**Enriquecimiento HIGH (§17):**

```
HIGH_TARGET_SHARE   = target_energy_HIGH / target_energy_nonDC
exc_high = max(0, auth_residual_energy_HIGH − self_residual_energy_HIGH)
exc_low  = max(0, auth_residual_energy_LOWMID − self_residual_energy_LOWMID)
HIGH_RESIDUAL_SHARE = exc_high / (exc_high + exc_low)
HIGH_ENRICHMENT = HIGH_RESIDUAL_SHARE / HIGH_TARGET_SHARE   (solo si pasa ENERGY_GATE)
```

`>1` ⇒ el error está sobrerrepresentado en HIGH respecto de la energía propia del material.
**No es trust proxy operativo.**

## 11. ENERGY_GATE (§15)

`ENERGY_GATE_FRACTION = 1e-6`. Una banda cuyo target aporta < 1e-6 de la energía no-DC se
marca `LOW_ENERGY` y su NRMSE **no** entra a las medianas de esa banda (el asset **no** se
excluye). **Derivación (NO de Cohort A):** float64 ε≈2.2e-16; error de round-trip FFT con
máscaras exactas ~1e-12; margen 1e6× sobre ese piso ⇒ 1e-6, de modo que un NRMSE de banda
casi vacía no se interprete. Verificado por test (banda de energía cero/tiny).

## 12. Control SELF obligatorio (§18)

Toda métrica AUTH tiene su control SELF equivalente, por banda y por agregado. Si SELF también
falla en una banda, esa banda **no** se atribuye directamente al pair/model mismatch (de ahí
`EXCESS = AUTH − SELF`).

## 13. Controles sintéticos (§19) y batería (§37/§26)

Deterministas, en `tests/test_native_parallax_exp_m5.py`, todos bajo Nyquist:

- **S0** fully coherent · **S1** HIGH-only · **S2** LOW-only · **S3** broadband ·
  **S4** amplitude-only(HIGH) · **S5** phase-only(HIGH).
- **Invariantes:** partición de máscaras, solapamiento cero, exhaustividad salvo DC,
  reconstrucción suma-de-bandas, Parseval, IFFT real (Hermitian), colocación de una sola
  frecuencia, bordes exactos 4/8/16/32/64/128, invarianza 512↔1024, ENERGY_GATE,
  alineamiento global único (NRMSE plano ante escala global), determinismo, fail-fast NaN/Inf.
- **Falsación clave (§27):** S1 (solo HIGH) debe clasificarse high-concentrated; S2 (solo
  LOW) **no** debe clasificarse high-concentrated. Si el detector no distinguiera → NO correr corpus.

---

## 14. ETAPA B — thresholds congelados (§20/§21/§22)

Derivados de controles sintéticos + piso SELF de M4 + matemática. **Congelados aquí, antes
de Cohort A.** Tras este congelamiento son inmutables.

| Símbolo | Valor | Significado |
|---|---|---|
| `T_LOWMID_NRMSE` | **0.15** | C1: `median NRMSE_AUTH(LOWMID) ≤ 0.15` |
| `T_LOWMID_EXCESS` | **0.10** | C1: `median EXCESS_NRMSE(LOWMID) ≤ 0.10` |
| `T_HIGH_ENRICHMENT` | **2.0** | C2: `median HIGH_ENRICHMENT ≥ 2.0` |
| `ENERGY_GATE_FRACTION` | **1e-6** | banda no evaluable por debajo |
| `LOWMID_HIGH_CUTOFF` | **32** | ciclos/tile |
| `M5_BOOTSTRAP_SEED` | **20260925** | CI de medianas asset-level |

**Derivación:** en S3 (broadband) `HIGH_ENRICHMENT ≈ 1` (residual distribuido como el target);
en S1 (high-only) resulta ≫2; el umbral 2.0 exige ≥2× sobrerrepresentación con margen sobre
el broadband. `T_LOWMID_EXCESS=0.10` separa el S0 coherente (≈0) del S2 low-only (≈0.5); el
bar 0.15 de NRMSE absoluto LOWMID ancla coherencia macro/meso. Todos validados por la batería.

**Reglas confirmatorias (totales, no solapadas):**

- **C1_LOWMID_PRESERVED** ⇔ `median NRMSE_AUTH(LOWMID) ≤ 0.15` **y** `median EXCESS_NRMSE(LOWMID) ≤ 0.10`.
- **C2_HIGH_ENRICHED** ⇔ `median EXCESS_NRMSE(HIGH) > median EXCESS_NRMSE(LOWMID)` **y**
  `median HIGH_ENRICHMENT ≥ 2.0` **y** réplica direccional en LEGACY_HELDOUT
  (`EXCESS_NRMSE(HIGH) > EXCESS_NRMSE(LOWMID)` en heldout).

**Tabla de decisión (§22):**

| C1 | C2 | Decisión |
|---|---|---|
| T | T | `EXP_M5_BANDLIMITED_RECOVERY_SUPPORTED` |
| F | F | `EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED` |
| T≠C2 | | `EXP_M5_MIXED` |
| — | — | `EXP_M5_DATA_INSUFFICIENT` (corpus no adquirible) |
| — | — | `EXP_M5_INVALIDATED_BY_UPSTREAM_BUG` (bug que cambie M0–M4) |

“SUPPORTED” **no** significa production GO: sólo que hay evidencia suficiente para *diseñar*
EXP-M6 (reconstrucción determinista band-limited). Low-pass “funciona en Skyrim” **no** está
implicado.

## 15. Protocolo anti-leakage (§23/§24/§31/§32)

1. **ETAPA A:** este documento de diseño, committeado, sin tocar Cohort A.
2. **ETAPA B:** controles sintéticos ⇒ thresholds ⇒ **commit de prereg freeze**
   (`research(parallax): freeze exp-m5 frequency-coherence preregistration`) ⇒ `M5_PREREG_FREEZE_SHA`.
3. **Sólo después** de ese SHA: correr **CALIBRATION (15)** como smoke de implementación
   (sin retunear). Bug de implementación ⇒ corregir + regression test + commit focal +
   borrar outputs contaminados + reejecutar. **Si “arreglar” calibration exige cambiar un
   threshold ⇒ STOP.**
4. **Execution freeze** (`research(parallax): freeze exp-m5 after calibration`) ⇒ `M5_EXECUTION_FREEZE_SHA`.
5. **FULL (31)** exigiendo `--frozen-ack freeze-<M5_EXECUTION_FREEZE_SHA>`. Output nombra
   `CALIBRATION` y `LEGACY_HELDOUT` (nunca “fresh heldout”).

## 16. Reproducibilidad (§38)

Cada JSON real registra: git SHA, base/main SHA, prereg freeze SHA, execution freeze SHA,
manifest SHA256, solver module SHA256, frequency module SHA256, cutoff, bandas exactas,
ENERGY_GATE, thresholds, resolución, seed, timestamp UTC, Python/NumPy/Pillow, FFT backend.
`json.dump(..., allow_nan=False)`. Fail-fast ante NaN/Inf, split mismatch, manifest mismatch,
partición de bandas imposible.

### 16.1 Provenance explícita (fail-closed)

Los tres SHAs de congelamiento son **inputs del operador**, nunca heurísticas de runtime:
`origin/main` puede haber avanzado después del freeze, así que leerlo en runtime mentiría
sobre la base contra la que el experimento quedó congelado. El bloque `environment` los expone
como campos separados y explícitos:

| Campo | Calibration | Full |
|---|---|---|
| `git_sha` | checkout real de la corrida | checkout real de la corrida |
| `base_main_sha` | `--base-main-sha` | `--base-main-sha` |
| `m5_prereg_freeze_sha` | `--prereg-freeze-sha` (= `M5_PREREG_FREEZE_SHA`) | `--prereg-freeze-sha` |
| `m5_execution_freeze_sha` | `null` (no existe todavía) | derivado de `--frozen-ack` |
| `frozen_ack` | `null` | `freeze-<40hex>` (se conserva) |

Formato exigido: **exactamente 40 hex minúsculos** (`^[0-9a-f]{40}$`). No se normaliza nada —
un SHA truncado, en mayúsculas o con whitespace es un error, porque "arreglarlo" en silencio
dejaría un artefacto cuyo provenance no corresponde al commit que corrió, que es justo lo que
esta sección existe para impedir. `--frozen-ack` exige `freeze-<40hex>`: un string arbitrario o
un SHA truncado **no** es un execution freeze válido.

**Calibration:**

```bat
python -m sky_claw.local.native_parallax.research.run_exp_m5 ^
  --m3-manifest docs\design\research\native-parallax\data\exp-m3-clean-authored-manifest.json ^
  --corpus-root C:\SkyClawResearch\NativeParallax\EXP-M3 ^
  --resolution 512 ^
  --phase calibration ^
  --prereg-freeze-sha <M5_PREREG_FREEZE_SHA> ^
  --base-main-sha <BASE_MAIN_SHA> ^
  --out C:\SkyClawResearch\NativeParallax\EXP-M3\runs\exp-m5\calibration.json
```

En calibration `--frozen-ack` se **rechaza**: la calibración es el smoke test previo al
execution-freeze, declararlo sería provenance fiction.

**Full:**

```bat
python -m sky_claw.local.native_parallax.research.run_exp_m5 ^
  --m3-manifest docs\design\research\native-parallax\data\exp-m3-clean-authored-manifest.json ^
  --corpus-root C:\SkyClawResearch\NativeParallax\EXP-M3 ^
  --resolution 512 ^
  --phase full ^
  --prereg-freeze-sha <M5_PREREG_FREEZE_SHA> ^
  --base-main-sha <BASE_MAIN_SHA> ^
  --frozen-ack freeze-<M5_EXECUTION_FREEZE_SHA> ^
  --out C:\SkyClawResearch\NativeParallax\EXP-M3\runs\exp-m5\exp_m5_results.json
```

En FULL `git_sha` coincide con `m5_execution_freeze_sha` y `m5_prereg_freeze_sha` apunta al
freeze **anterior**: un consumidor puede verificar que la corrida salió del freeze que se
supuso, en lugar de inferirlo de un `frozen_ack` opaco.

> Nota de alcance: el fix de provenance **no** amplía el formato científico. Calibration sigue
> emitiendo `summary.decision = PENDING_FREEZE` sin medianas de cohorte; los umbrales, bandas,
> cutoff, ENERGY_GATE, seed, splits y métricas quedan idénticos byte a byte.

## 17. Limitaciones y adversaria preregistrada (§30/§33/§39)
- **Seams/periodicidad:** la FFT asume periodicidad; puede haber spectral leakage. Se reportan
  `seam_height`/`seam_gradient` (M4). Windowing/apodización, si se usa, es **secondary
  preregistrado**, nunca para “mejorar” el resultado primario.
- **Q8:** el primario SELF usa Q8; SELF-float/64px sólo en sintéticos.
- **Resize:** secondary 1024 nativo; no reinterpretar secondary como primary.
- **Family/provider (n=3..5; ambientCG 8; Poly Haven 23):** diagnóstico **suggestive**; sin
  reglas operativas por categoría en M5.
- **Preguntas adversarias a responder antes de cerrar:** ¿HIGH parece peor sólo por poca
  target energy (lo cubre ENERGY_GATE)? ¿el corte duro 32 produce ringing interpretado como
  mismatch? ¿SELF muestra el mismo patrón? ¿Q8 explica HIGH? ¿1024 contradice 512? ¿seams
  ensucian el espectro? ¿una familia/proveedor domina? ¿LOWMID parece bueno sólo por scaling
  global? ¿Parseval cierra y las máscaras son exhaustivas? ¿LEGACY_HELDOUT descrito honestamente?

## 18. Desviación de protocolo registrada: exposición prematura de LEGACY_HELDOUT

Esta sección se escribe **antes** de cualquier corrida posterior y no se borra. Es un
registro honesto de un incidente ocurrido durante el hardening de provenance, no una
justificación.

### Qué pasó

Al reproducir el defecto de provenance (falta de `base_main_sha` /
`m5_prereg_freeze_sha` / `m5_execution_freeze_sha`, y `--frozen-ack` que aceptaba strings
arbitrarios), se descubrió que `--frozen-ack foo` no sólo pasaba la validación: **ejecutó
una pasada FULL sobre los 31 assets de Cohort A**, produciendo las 16 filas de
`LEGACY_HELDOUT` y emitiendo `EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED`, con una
provenance ficticia. Eso ocurrió **antes** de cualquier execution freeze. Los JSON
resultantes se eliminaron y no se conservaron como evidencia, y el `calibration.json` de la
primera calibration (SHA256 `388bfcdf…`) quedó intacto.

### Qué se perdió y qué no

Borrar los JSON **no restaura la ceguera experimental**: los 16 `LEGACY_HELDOUT` fueron
observados por M5 antes del freeze. No se puede afirmar que la réplica `LEGACY_HELDOUT`
permaneció oculta hasta el execution freeze.

Lo que **sí** se sostiene, y por qué M5 sigue siendo interpretable bajo las reglas
preregistradas:

- las reglas, umbrales, bandas, cutoff, ENERGY_GATE, seed y formulas estaban congelados
  **antes** del incidente (commit de prereg `d3745089…` / el freeze vigente);
- el fix posterior modifica **sólo provenance** (campos de `environment` + validación de CLI);
  `rows` pre/post fix son bit-idénticos (SHA `7f37b264…` en ambos);
- la exposición no cambió ningún valor científico: la única decisión observada provino del
  runner aplicando los umbrales ya congelados, y su output fue eliminado, no usado.

Por tanto, M5 puede continuar bajo las reglas preregistradas, pero **cualquier resultado
final debe declarar explícitamente esta desviación**. Para una validación verdaderamente
ciega hará falta un conjunto de assets que M5 nunca haya observado.

### Estado de protocolo (para registrar en cada JSON M5 relevante)

Estos campos se registran de forma machine-readable en el bloque `environment` de la corrida
que produzca el resultado final, sin reemplazar la decisión del runner:

```text
protocol_status = UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE
legacy_heldout_blind_until_execution_freeze = false
scientific_rules_changed_after_exposure = false
```

Esto se suma a la desviación de freeze ya registrada en el informe de calibration; la decisión
científica del runner (`summary.decision`) permanece intacta y se reporta tal cual.

---

## 19. Execution freeze post-calibration

Estado: la fase **CALIBRATION** (15 assets, sin retunear) fue ejecutada, verificada y
**aceptada**. Este commit congela EXP-M5 y es el `M5_EXECUTION_FREEZE_SHA`. El SHA del freeze
es, **por definición, el commit que introduce esta sección**: no se embebe aquí (un SHA
auto-referencial en el propio archivo no puede ser correcto, y escribirlo "a mano" o "después"
produciría provenance fiction — el mismo defecto que §16.1 existe para impedir). Se obtiene
con `git rev-parse HEAD` sobre este commit.

No existe un segundo commit vacío encima de este.

### 19.1 Identificadores congelados

```text
CALIBRATION_STATUS=CALIBRATION_OK

M5_PREREG_FREEZE_SHA=d33f81ca4218d98ab6f445a93db87a76bc19d7ba
BASE_MAIN_SHA=9f6fa0c2f4a111df4dd3c57b505fb9549a3bbeb5
CALIBRATION_RUN_SHA=d33f81ca4218d98ab6f445a93db87a76bc19d7ba

CALIBRATION_OUTPUT=C:\SkyClawResearch\NativeParallax\EXP-M3\runs\exp-m5\calibration-d33f81ca.json
CALIBRATION_OUTPUT_SHA256=b8f667b5d78b8eaf26be86626d9630e682fb25d36c7b22fd639fd8ba829833db
CALIBRATION_OUTPUT_BYTES=94914

CALIBRATION_ROWS=15
LEGACY_HELDOUT_ROWS=0
EXCLUSIONS=0
```

**Qué es y qué no es `CALIBRATION_OK`.** Es el **veredicto de aceptación del operador** sobre
esta corrida, no un campo emitido por el runner: la cadena `CALIBRATION_OK` **no aparece** en
el JSON de calibration, cuyo propio campo es `summary.decision = PENDING_FREEZE` (§16.1:
calibration no emite medianas de cohorte, por diseño). Los valores de acceptance —
15 filas, 0 `LEGACY_HELDOUT`, 0 exclusiones, y los identificadores de arriba — sí son
**verificables directamente contra el artefacto**, y lo fueron:

| Campo | Valor en el artefacto | Verificado |
|---|---|---|
| `environment.git_sha` | `d33f81ca…` | = `CALIBRATION_RUN_SHA` |
| `environment.base_main_sha` | `9f6fa0c2…` | = `BASE_MAIN_SHA` |
| `environment.m5_prereg_freeze_sha` | `d33f81ca…` | = `M5_PREREG_FREEZE_SHA` |
| `environment.m5_execution_freeze_sha` | `null` | correcto en calibration |
| `dataset.n_rows` | `15` | = `CALIBRATION_ROWS` |
| `dataset.split_counts` | `{"CALIBRATION":15,"LEGACY_HELDOUT":0}` | = `LEGACY_HELDOUT_ROWS=0` |
| `dataset.exclusions` | `[]` | = `EXCLUSIONS=0` |
| `environment.solver` | `reconstruct_from_normal(normal, "RAW", 0.0, 0.0)` | camino M2/M3 sin cambios |

### 19.2 Contrato científico congelado

Sin cambios respecto de §14. El artefacto de calibration los re-emite y coincide con el
prereg:

```text
LOWMID_HIGH_CUTOFF=32
BAND_EDGES=(4,8,16,32,64,128)
ENERGY_GATE_FRACTION=1e-6
T_LOWMID_NRMSE=0.15
T_LOWMID_EXCESS=0.10
T_HIGH_ENRICHMENT=2.0
M5_BOOTSTRAP_SEED=20260925
```

```text
METRICS_FROZEN=YES
THRESHOLDS_FROZEN=YES
PREPROCESSING_FROZEN=YES
TRANSFORMS_FROZEN=YES
EXCLUSIONS_FROZEN=YES
DECISION_RULES_FROZEN=YES
```

Comprobado contra la calibration aceptada, y confirmado por lectura del código:

```text
THRESHOLDS_CHANGED=NO
SCIENTIFIC_LOGIC_CHANGED=NO
CODE_CHANGED=NO
PRE_FIX_ROWS_BIT_IDENTICAL=YES
RERUN_ROWS_BIT_IDENTICAL=YES
```

Este freeze es **docs-only**: `run_exp_m5.py`, `frequency_coherence.py`,
`solver_coherence.py`, `authored_dataset.py`, `run_exp_m2.py`, `run_exp_m3.py` y
`run_exp_m4.py` quedan sin tocar.

### 19.3 Desviación de protocolo — se preserva, no se cierra

Lo registrado en §18 sigue vigente **sin modificación**. Este freeze **no** lo revierte:

```text
protocol_status=UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE
legacy_heldout_blind_until_execution_freeze=false
scientific_rules_changed_after_exposure=false
```

Borrar los outputs contaminados **no restauró la ceguera**: los 16 `LEGACY_HELDOUT` fueron
observados por M5 antes de este freeze. En consecuencia, `LEGACY_HELDOUT` **no** debe
presentarse —ni en el informe de FULL ni en ninguna comunicación— como *fresh heldout* ni
como *independent validation*. Es un split histórico reutilizado de M3/M4, con exposición
previa (§3 y §18). El estado **no** se convierte en `CLEAN` en este documento ni en el
runner.

### 19.4 Incidente operativo: contaminación de imports cross-worktree

Durante el preflight de la calibration se detectó que el intérprete resolvía módulos de
`sky_claw` **hacia otro checkout**, por entradas `.pth` del venv, que anteponen rutas ajenas
al worktree en `sys.path`:

```text
E:\Skyclaw_Main_Sync\.venv\Lib\site-packages\00_worktree_skyclaw.pth        → C:\Worktrees\Sky-Claw-586c
E:\Skyclaw_Main_Sync\.venv\Lib\site-packages\_editable_impl_sky_claw.pth   → E:\Skyclaw_Main_Sync
```

El worktree de M5 **no tiene venv propio**: usa el venv del checkout principal, que inyecta
esas dos rutas. Una ejecución por script externo (`python <ruta>\script.py`) resuelve entonces
`sky_claw` contra `C:\Worktrees\Sky-Claw-586c` o `E:\Skyclaw_Main_Sync`, es decir código de
**otra** revisión, sin ningún error visible: el proceso corre y devuelve números plausibles de
otro tree.

Qué se hizo y qué queda:

- la ejecución contaminada **se abortó antes de aceptar resultados**;
- la calibration **válida** (§19.1) se ejecutó después vía `python -m`, con los módulos
  relevantes verificados contra el worktree del prereg freeze;
- esto **no invalida** la calibration aceptada — su provenance (§19.1) lo corrobora —
  pero es un riesgo operativo **real y recurrente**;
- **NO está corregido.** No se tocaron los `.pth`, ni el venv, ni el packaging, ni
  `pyproject.toml`/`uv.lock`. Se trata fuera de EXP-M5, en issue/PR propio.

### 19.5 Gate obligatorio antes de FULL (integridad operativa, no científica)

Por §19.4, la corrida FULL **debe** repetir el chequeo de origen de imports antes de leer un
solo asset. Para cada módulo M5 relevante, `<módulo>.__file__` debe resolver **dentro del
worktree exacto** del `M5_EXECUTION_FREEZE_SHA`:

```text
run_exp_m5.__file__
frequency_coherence.__file__
solver_coherence.__file__
authored_dataset.__file__
run_exp_m2.__file__
run_exp_m3.__file__
run_exp_m4.__file__
trust_proxies.__file__
```

y además `alignment`, `metrics`, `normal_fft_periodic`, `normal_from_height`, `nz_policies`,
`synthetic_height` (los módulos reales de `sky_claw/local/native_parallax/research/`).

La ejecución FULL debe invocar el runner **como módulo**:

```bat
python -m sky_claw.local.native_parallax.research.run_exp_m5
```

**Prohibido** `python <ruta>\externa\script.py`. Si algún módulo resuelve a otro checkout:

```text
STOP
CROSS_CHECKOUT_IMPORT_CONTAMINATION=YES
FULL_RUN_EXECUTED=NO
```

Esto es un gate de **integridad operativa**, no un cambio científico: no altera umbrales,
bandas, cutoff, ENERGY_GATE, seed, métricas, máscaras, split ni reglas de exclusión.

### 19.6 Calibrations históricas (raw)

Se conservan ambas, sin ambigüedad sobre cuál gobierna:

```text
# Pre-fix — histórica, superseded for execution-freeze purposes
calibration.json  bytes=94561
SHA256=388bfcdf0b7344b793ccffc92772dd1290a458ab3ed3fa0b3aaf5c37fe224c20

# Vigente — la que sostiene este freeze
calibration-d33f81ca.json  bytes=94914
SHA256=b8f667b5d78b8eaf26be86626d9630e682fb25d36c7b22fd639fd8ba829833db
```

Los JSON viven **fuera del repositorio** (`C:\SkyClawResearch\…`) y el protocolo vigente no
exige versionarlos: lo que se congela aquí son sus hashes, su tamaño y su conteo de filas.

### 19.7 Alcance de este freeze

Este freeze **no** autoriza por sí solo la corrida FULL. Además del gate de §19.5, FULL
requiere: CI verde sobre el SHA exacto de este commit, `--frozen-ack
freeze-<M5_EXECUTION_FREEZE_SHA>`, y la confirmación de que la desviación de §19.3 sigue
declarada en el informe de resultados. No se arreglan aquí #667 ni #663, no se corrigen los
`.pth`, no se cambian dependencias, no se restackea contra `main` (el restack es posterior a
FULL y a la revisión científica) y no se toca la aserción tautológica residual de tests.

---

## 20. FULL (31) — resultado observado y provenance

> Sección añadida **después** de ejecutar FULL, con la matemática congelada en §14/§19.2
> **sin cambios**. Es un registro de resultado: **no** retunea, **no** reinterpreta la
> etiqueta, **no** toca código científico. Los SHAs históricos de §19 **no** se actualizan.
>
> ⚠️ **Finding post-FULL (ver §21):** la agregación de `EXCESS_NRMSE` que usó el runner en
> esta corrida **no** era la del prereg (`median(EXCESS por asset)` vs
> `median(AUTH) − median(SELF)`). El artefacto histórico de esta sección se **preserva** y su
> provenance real sigue siendo válida, pero **no** se describe como la implementación literal
> del prereg. `SCIENTIFIC_RERUN_REQUIRED=YES`; la decisión vigente vuelve a emerger solo del
> runner corregido (§21–§22).

### 20.1 Cadena de provenance

```text
BASE_MAIN_SHA=9f6fa0c2f4a111df4dd3c57b505fb9549a3bbeb5
M5_PREREG_FREEZE_SHA=d33f81ca4218d98ab6f445a93db87a76bc19d7ba
M5_EXECUTION_FREEZE_SHA=e116196fc47f83659c7df7ada5ec82f9f993bbcd
FULL_RUN_SHA=e116196fc47f83659c7df7ada5ec82f9f993bbcd
```

Artefacto (vive **fuera** del repositorio):

```text
FULL_OUTPUT=C:\SkyClawResearch\NativeParallax\EXP-M3\runs\exp-m5\full-e116196f.json
FULL_OUTPUT_SHA256=f8503610d14d29b4ecc276108b10cfcfa59673ab64d57274cbf2d8f7fcd4bd46
FULL_OUTPUT_BYTES=196006
```

Verificado **leyendo el raw**: `phase=full`; `environment.git_sha` =
`environment.m5_execution_freeze_sha` = `e116196f…` (= `FULL_RUN_SHA`); `environment.base_main_sha`
= `9f6fa0c2…`; `environment.m5_prereg_freeze_sha` = `d33f81ca…`;
`environment.frozen_ack` = `freeze-e116196f…`.

### 20.2 Dataset

```text
ROWS=31
CALIBRATION_ROWS=15
LEGACY_HELDOUT_ROWS=16
EXCLUSIONS=0
```

`dataset.split_counts = {"CALIBRATION": 15, "LEGACY_HELDOUT": 16}`; `dataset.exclusions = []`.
Las 31 filas tienen `lowmid_eligible = high_eligible = 1.0`.

### 20.3 Métricas

| Métrica | FULL (31) | CALIBRATION (15) | LEGACY_HELDOUT (16) |
|---|---:|---:|---:|
| SELF LOWMID NRMSE | 0.0503 | 0.0729 | 0.0224 |
| AUTH LOWMID NRMSE | 0.5543 | 0.6442 | 0.4994 |
| LOWMID EXCESS | 0.5082 | 0.5123 | 0.4772 |
| SELF HIGH NRMSE | 0.0778 | 0.0822 | 0.0565 |
| AUTH HIGH NRMSE | 0.4252 | 0.4317 | 0.3663 |
| HIGH EXCESS | 0.3350 | 0.3350 | 0.3105 |
| HIGH_ENRICHMENT | 0.7542 | 1.0279 | 0.6462 |

**Nota de lectura (relación con el contrato del runner).** Las filas `NRMSE` y
`HIGH_ENRICHMENT` son la **mediana de cohorte** (campos `summary.<split>.*`). Las filas
**EXCESS** de la tabla son la **mediana por-asset del exceso** `NRMSE_AUTH − NRMSE_SELF`
(que para FULL coincide con el punto bootstrap). El campo `summary.<split>.excess_*` que
gobierna C1/C2 se define como **diferencia de medianas** `median(AUTH) − median(SELF)` y
difiere levemente:

| `summary.*.excess_*` (diferencia de medianas) | FULL | CALIBRATION | LEGACY_HELDOUT |
|---|---:|---:|---:|
| LOWMID EXCESS (contrato `evaluate_rules`) | 0.5040 | 0.5713 | 0.4769 |
| HIGH EXCESS (contrato `evaluate_rules`) | 0.3474 | 0.3494 | 0.3098 |

Se registran ambos porque el primero es la lectura pedida y el segundo es el que efectivamente
usan `evaluate_rules`/`decide` (ver `frequency_coherence.cohort_medians`).

### 20.4 Bootstrap (FULL, seed `20260925`, n=2000)

```text
excess_lowmid_nrmse: point=0.5082  CI95=[0.3104, 0.6058]
excess_high_nrmse:   point=0.3350  CI95=[0.2679, 0.4430]
high_enrichment:     point=0.7542  CI95=[0.5710, 1.0470]
```

Threshold preregistrado `T_HIGH_ENRICHMENT = 2.0` (§14). Observación válida: **el CI95 de
HIGH_ENRICHMENT queda enteramente por debajo de 2.0**.

### 20.5 Reglas y decisión

```text
C1_lowmid_preserved=false
C2_high_enriched=false
high_enrichment_ge_threshold=false
high_gt_lowmid_excess=false
legacy_heldout_replication=false

FULL_STATUS=FULL_VALID
SUMMARY_DECISION=EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED
```

Lectura directa de las medianas de cohorte: `AUTH_LOWMID` (0.5543) ≫ `T_LOWMID_NRMSE` (0.15) y
el `LOWMID EXCESS` (0.5040) ≫ `T_LOWMID_EXCESS` (0.10) ⇒ **C1 falso**. El `HIGH EXCESS`
(0.3474) **no** supera al `LOWMID EXCESS` (0.5040); `HIGH_ENRICHMENT` mediano (0.7542) ≪ 2.0; y
la réplica direccional en LEGACY_HELDOUT tampoco se cumple ⇒ **C2 falso**. Con C1=F y C2=F la
tabla de decisión (§14) emite `EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED`. **La etiqueta no se
reinterpreta con otro nombre.**

### 20.6 Interpretación matemática (acotada al corpus/protocolo EXP-M5)

- El mismatch AUTH **no** está concentrado predominantemente en HIGH.
- `LOWMID_EXCESS > HIGH_EXCESS`.
- `HIGH_ENRICHMENT` mediano < 1.

En conjunto, los resultados son más compatibles con un mismatch **broadband** entre normal
authored y height/displacement authored que con una pérdida principalmente high-frequency
susceptible de recuperación band-limited.

**No** se afirma universalmente que `normal → height` sea imposible, que el screened Poisson
nunca sirva, ni que todas las texturas authored estén mal. La inferencia se limita al corpus y
al protocolo EXP-M5.

### 20.7 Desviación de protocolo (se preserva, no se cierra)

Confirmado contra el raw (`environment.protocol_status` y campos asociados):

```text
PROTOCOL_STATUS=UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE
legacy_heldout_blind_until_execution_freeze=false
scientific_rules_changed_after_exposure=false
```

La réplica LEGACY_HELDOUT **no** se presenta —ni aquí ni en ninguna comunicación— como *fresh
blinded confirmation*. Se denomina **legacy heldout evaluation under documented protocol
deviation** (§3/§18/§19.3). La decisión numérica sigue siendo el output legítimo del algoritmo
congelado.

### 20.8 Determinismo

```text
CALIBRATION_ROWS_BIT_IDENTICAL=YES
FULL_RERUN_SCIENCE_BIT_IDENTICAL=YES
```

Verificación directa en esta tarea: las `rows` de `calibration.json` y
`calibration-d33f81ca.json` son **bit-idénticas** (15/15 filas, 0 diferencias); las únicas
diferencias de `environment` son `git_sha`, los campos de provenance añadidos por el fix
(§16.1) y `environment.timestamp_utc`. Para FULL, la rerun científica es bit-idéntica salvo
`environment.timestamp_utc`; **no se conserva un segundo artefacto FULL en disco**, de modo que
esa igualdad se registra según el registro de ejecución (el artefacto válido es
`full-e116196f.json`).

### 20.9 Caso no evaluable

```text
polyhaven_brick_4  split=CALIBRATION  high_enrichment=null
```

`HIGH_ENRICHMENT` no interpretable ⇒ `NaN` en el runner ⇒ `null` en el JSON vía
`_json_safe`. Se registra como **`null`**, nunca como `0`, `1` ni `NaN`. El asset **no** se
excluye: sus agregados LOWMID/HIGH sí entran a las medianas de cohorte.

### 20.10 Riesgo cross-worktree (reproducido, no resuelto aquí)

```text
CROSS_WORKTREE_IMPORT_RISK=CONFIRMED
CROSS_CHECKOUT_IMPORT_CONTAMINATION=NO  (en la corrida FULL válida)
```

Causa conocida (§19.4): entradas `.pth` del venv compartido anteponen rutas de **otro**
checkout:

```text
E:\Skyclaw_Main_Sync\.venv\Lib\site-packages\00_worktree_skyclaw.pth
    → C:\Worktrees\Sky-Claw-586c
E:\Skyclaw_Main_Sync\.venv\Lib\site-packages\_editable_impl_sky_claw.pth
    → E:\Skyclaw_Main_Sync
```

La corrida FULL válida se ejecutó **como módulo** (`python -m sky_claw.local.native_parallax.
research.run_exp_m5`) con `cwd` en el worktree correcto, y el gate de §19.5 confirmó
`CROSS_CHECKOUT_IMPORT_CONTAMINATION=NO`. **No se arreglan los `.pth` en este PR**; el hallazgo
se trata en issue/PR propio.

### 20.11 Distinción CALIBRATION (se preserva)

El artefacto de calibration emite `summary.decision = PENDING_FREEZE` (verificado en el raw);
**no** emite medianas de cohorte. `CALIBRATION_OK` (§19.1) fue un **veredicto de aceptación
del operador/externo**, **no** un campo emitido por el runner.

### 20.12 Alcance de esta sección

Sección **docs-only**: no cambia `frequency_coherence.py`, `run_exp_m5.py`,
`solver_coherence.py`, `authored_dataset.py` ni los tests. FULL no autoriza ni ejecuta M6, no
mergea el PR, no lo marca *Ready*, y no incorpora los fixes de #663 ni #667.

---

## 21. Finding post-FULL: agregación de EXCESS no coincidía con el prereg

> Registrado **antes** de corregir la implementación, docs-only. No cambia thresholds, bandas,
> cutoff, ENERGY_GATE, seed, splits ni reglas. No es un nuevo prereg.

### 21.1 Qué exige el prereg y qué implementó el runner

El prereg (§10/§14) define por asset:

```text
EXCESS_NRMSE_b = NRMSE_AUTH_b − NRMSE_SELF_b
```

y las reglas C1/C2 consumen `median EXCESS_NRMSE_b` sobre los assets elegibles. Sin embargo,
`cohort_medians()` (`frequency_coherence.py`) agregaba:

```text
excess_* = median(NRMSE_AUTH) − median(NRMSE_SELF)
```

Estas dos cantidades **no son equivalentes en general**: con assets heterogéneos pueden caer a
distinto lado de `T_LOWMID_EXCESS` o invertir la comparación HIGH vs LOWMID de C2. El valor
apareado correcto ya existía en `asset_summary()` (`excess_lowmid_nrmse`/`excess_high_nrmse`),
pero no se agregaba.

```text
FINDING_EXCESS_AGGREGATION=CONFIRMED
POST_FULL_REVIEW_FINDING=PAIRED_EXCESS_AGGREGATION_MISMATCH
```

### 21.2 Artefacto histórico: se preserva, no se reescribe

```text
HISTORICAL_FULL_ARTIFACT_PRESERVED=YES
HISTORICAL_FULL_OUTPUT=C:\SkyClawResearch\NativeParallax\EXP-M3\runs\exp-m5\full-e116196f.json
HISTORICAL_FULL_SHA256=f8503610d14d29b4ecc276108b10cfcfa59673ab64d57274cbf2d8f7fcd4bd46
HISTORICAL_SUMMARY_DECISION=EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED
SCIENTIFIC_RERUN_REQUIRED=YES
```

La provenance real de esa corrida (§20.1) sigue siendo verificable y **no** se borra. Lo que
cambia es la lectura: ya **no** se presenta como la implementación literal del prereg. Los
números preliminares (punto bootstrap apareado LOWMID ≈ 0.5082, HIGH ≈ 0.3350) sugieren
decision-equivalence, pero **no se usan para waive**: la decisión vigente debe emerger del
runner corregido (§22) ejecutado desde el corrective freeze (§22.1).

### 21.3 Qué no es este hallazgo

No es un bug upstream M0–M4: `EXP_M5_INVALIDATED_BY_UPSTREAM_BUG` está preregistrado para
bugs que cambien M0–M4 y **no** se reutiliza aquí. Es un defecto de **implementación M5** que
se corrige para conformar la implementación al prereg congelado, sin retuneo.

```text
PROTOCOL_STATUS=UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE
legacy_heldout_blind_until_execution_freeze=false
scientific_rules_changed_after_exposure=false
```

---

## 22. Corrective execution freeze (implementación corregida)

> Docs-only. **NO** es un nuevo prereg: el prereg original sigue siendo
> `M5_PREREG_FREEZE_SHA=d33f81ca…`. Congela la implementación **corregida** para conformarla
> al prereg congelado, sin retuneo de thresholds ni reglas.

### 22.1 Identificador

```text
M5_CORRECTIVE_EXECUTION_FREEZE_SHA=<este commit>
BASE_MAIN_SHA=9f6fa0c2f4a111df4dd3c57b505fb9549a3bbeb5
M5_PREREG_FREEZE_SHA=d33f81ca4218d98ab6f445a93db87a76bc19d7ba
HISTORICAL_M5_EXECUTION_FREEZE_SHA=e116196fc47f83659c7df7ada5ec82f9f993bbcd
```

Razón: *post-FULL implementation correction to conform to frozen prereg, with no
threshold/rule retuning*. El SHA es, por definición, el commit que introduce esta sección
(`git rev-parse HEAD`); no se embebe para no escribir provenance fiction (§19).

### 22.2 Qué corrige y qué NO toca

Correcciones (detalle en §21 y en los findings de revisión):

- `cohort_medians`: `EXCESS = median(NRMSE_AUTH − NRMSE_SELF)` por asset (prereg §10/§14).
- `evaluate_rules`: fail-closed ante held-out ausente y ante valores no finitos.
- Runner: checkout atado al execution freeze, SHAs exactos (`fullmatch`), bootstrap
  no-evaluable sin crash, resoluciones del corpus real en `{512, 1024}`, y FULL sin
  `HELD_OUT` utilizable ⇒ `EXP_M5_DATA_INSUFFICIENT` (no `SUPPORTED`).

Sin cambios:

```text
THRESHOLDS_CHANGED=NO
BANDS_CHANGED=NO
CUTOFF_CHANGED=NO
ENERGY_GATE_CHANGED=NO
SEED_CHANGED=NO
SPLITS_CHANGED=NO
```

El análisis por asset (`asset_summary`/`analyze_path`) queda intacto: el fix es de
agregación de cohorte, no de métricas por asset.

### 22.3 Desviación de protocolo — se preserva

```text
PROTOCOL_STATUS=UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE
legacy_heldout_blind_until_execution_freeze=false
scientific_rules_changed_after_exposure=false
```

La **regla no cambió**; se corrigió su **implementación**. El corrective FULL ocurre
**después** de que los datos ya fueron observados: no es una confirmación blinded y no se
presenta como tal.

### 22.4 Ambigüedad de scope primary/secondary (1024)

```text
SECONDARY_SCOPE_AMBIGUITY=CONFIRMED
```

El prereg §9 define 512 como *primary* y 1024 como *secondary* nativa/no-resize, pero **no
existe** un contrato machine-readable que distinga `PRIMARY_DECISION` de `SECONDARY_CONTROL`
en el artefacto (solo se registra `environment.resolution`). Este slice valida que el corpus
real sólo admita `{512, 1024}`, pero **no inventa** semántica de decisión para 1024. La
corrida correctiva que gobierna la decisión es `resolution=512`; la distinción
primary/secondary queda como follow-up explícito.

### 22.5 Nota post-freeze: hardening operativo sin cambio científico

Con posterioridad a este freeze aterrizó `ce0965b2` (el chequeo de checkout corre `git` en el
directorio del **módulo**, no en el cwd del proceso) junto con su test. Es integridad
operativa, no ciencia: los blobs de `frequency_coherence.py` (`836fb62e…`) y
`solver_coherence.py` (`1d665f64…`) son **idénticos** al freeze, y el análisis por asset y la
agregación no cambiaron. El corrective FULL (§23) sigue siendo el resultado válido de esta
implementación.

---

## 23. Corrective FULL — resultado y equivalencia de decisión

> Primer FULL ejecutado desde el corrective freeze (§22), con la implementación corregida.
> Docs-only: registra lo observado; no retunea nada.

### 23.1 Artefacto

```text
CORRECTED_FULL_OUTPUT=C:\SkyClawResearch\NativeParallax\EXP-M3\runs\exp-m5\full-corrected-e3dbda51.json
CORRECTED_FULL_SHA256=57b22b1d6138622ba31c4a76934b5b9f35ba8bdaff5c174e638150a1d60a6480
CORRECTED_FULL_BYTES=196006
CORRECTED_FULL_SHA=e3dbda517782cb71d9647acad607ea3846629952
CORRECTED_FULL_RESOLUTION=512
```

Verificado contra el raw: `git_sha` = `m5_execution_freeze_sha` = `frozen_ack` =
`e3dbda51…`; `base_main_sha = 9f6fa0c2…`; `m5_prereg_freeze_sha = d33f81ca…`;
`protocol_status` y flags de §18 intactos; thresholds/cutoff/band_edges/ENERGY_GATE/seed
idénticos (§22.2). El artefacto histórico `full-e116196f.json` **no** se sobrescribió.

### 23.2 Rows por asset

```text
HISTORICAL_ROWS_VS_CORRECTED=BIT_IDENTICAL
```

31/31 filas científicas bit-idénticas entre el FULL histórico y el correctivo: el fix es de
agregación de cohorte, y el análisis por asset no cambió.

### 23.3 Summary (medianas apareadas) y decisión

| `summary.*.excess_*` | CORRECTED (apareada) | HISTORICAL (diferencia de medianas) |
|---|---:|---:|
| FULL LOWMID EXCESS | 0.5082 | 0.5040 |
| FULL HIGH EXCESS | 0.3350 | 0.3474 |
| CALIBRATION LOWMID EXCESS | 0.5123 | 0.5713 |
| CALIBRATION HIGH EXCESS | 0.3350 | 0.3494 |
| LEGACY_HELDOUT LOWMID EXCESS | 0.4772 | 0.4769 |
| LEGACY_HELDOUT HIGH EXCESS | 0.3105 | 0.3098 |

```text
CORRECTED_RULES:
C1_lowmid_preserved=false
C2_high_enriched=false
high_enrichment_ge_threshold=false
high_gt_lowmid_excess=false
legacy_heldout_replication=false

CORRECTED_SUMMARY_DECISION=EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED
HISTORICAL_TO_CORRECTED_DECISION_EQUIVALENT=YES
```

El bootstrap es idéntico al histórico (ya usaba valores por asset): LOWMID point 0.5082
CI95 [0.3104, 0.6058]; HIGH 0.3350 [0.2679, 0.4430]; HIGH_ENRICHMENT 0.7542 [0.5710, 1.0470].
El caso no evaluable `polyhaven_brick_4` sigue en `null`.

### 23.4 Caveat de protocolo (se preserva)

El corrective rerun ocurre **después** de que todo el dataset ya había sido observado (FULL
histórico + incidente §18): **no** es una confirmación blinded y no se presenta como tal.

```text
PROTOCOL_STATUS=UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE
legacy_heldout_blind_until_execution_freeze=false
scientific_rules_changed_after_exposure=false
```
