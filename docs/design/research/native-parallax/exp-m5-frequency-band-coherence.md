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
| `LOWMID` | `0 < rho < 32` (= B1|B2|B3|B4) |
| `HIGH`   | `rho >= 32` (= B5|B6|B7) |

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
