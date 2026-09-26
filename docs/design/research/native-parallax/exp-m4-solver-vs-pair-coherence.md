# EXP-M4 — Solver ceiling vs coherencia del par authored normal↔height (preregistro)

> Research-only. Sin DDS, BC5 real, Skyrim, MO2, PGPatcher, DynDOLOD, IA ni ParallaxR.
> Sin trust proxies nuevos, sin clasificadores, sin AUTO_SAFE, sin ranking.
>
> **ESTADO: PREREGISTRADO.** Este documento se committeó ANTES de correr el experimento
> sobre el corpus (§5 del brief: nada de thresholds/métricas/exclusiones se define después
> de ver resultados de Cohort A). Los umbrales se derivan EXCLUSIVAMENTE de resultados ya
> publicados (NP-M0 §E1, EXP-M2, EXP-M3) y del redondeo matemático del pipeline — ver §6.
> Cualquier idea posterior a los resultados va a §15 (exploratorio, claramente separado).

## 1. Pregunta científica

La reconstrucción de height a partir de una normal authored comete error. ¿El error es

- **A. Limitación del solver** (discretización, FFT periódica, Nyquist anulado, DC
  inobservable, bordes no-periódicos): incluso un par *perfectamente coherente* con
  nuestro modelo gradiente→height se reconstruiría mal; o
- **B. Incoherencia del par authored**: la normal publicada y el displacement publicado
  no son coherentes bajo nuestro modelo (escala artística, microdetalle normal-only,
  filtros/semánticas de displacement distintas, CAD de autoría distinto) — el techo del
  solver no es el factor limitante.

**El par incoherente NO es un asset "malo"** (§4 del brief): es evidencia de que el par
no satisface el modelo `p=-nx/nz, q=-ny/nz + ∇²h = ∂p/∂x + ∂q/∂y` en la convención y
escala ensayadas. Sin evidencia primaria de proveedor no se atribuye motivo a nadie.

## 2. Diseño: dos caminos emparejados por asset

Para CADA asset de Cohort A (EXP-M3, mismo manifest, misma provenance, sin re-selección):

- **SELF (control de techo, primario):** `H` (authored height, oráculo) → forward
  *matched* `spectral_gradients` → `normals_from_gradients(sx=1, sy=1)` → cuantización
  8-bit `quantize_decode` (misma fidelidad de almacenamiento que los PNG authored) →
  **mismo solver** (`reconstruct_from_normal("RAW")`) → `H_self` → comparar vs `H`.
  Mide el TECHO del pipeline (solver+cuantización+contorno) sobre contenido real.
- **AUTH (experimental, primario):** `N_authored` (resized 1024→512, renormalizada,
  convención declarada→`SOLVER_NORMAL_CONVENTION="DIRECTX"`, exactamente como M3) →
  **mismo solver, mismo oráculo** → `H_auth` → comparar vs `H`.

**Variable primaria por asset:** `DELTA_RMSE = RMSE_AUTH − RMSE_SELF` (pareado; mismo H,
mismo solver, mismo oráculo). Análogos preregistrados: `DELTA_CORR = |corr_SELF| −
|corr_AUTH|` y `DELTA_VAR = var_ratio_SELF − var_ratio_AUTH`.

**Prohibido:** warped locales, fits por-tile no lineales, cualquier alineamiento más allá
del contrato M2/M3 (`OracleOnly.fit_global_scale`: escala afín global con signo, o
evaluation-only). El height authored es SOLO oráculo (§11 M2/M3 se mantienen íntegros).

## 3. Dataset (idéntico a M3, cero re-selección)

- Manifest: `docs/design/research/native-parallax/data/exp-m3-clean-authored-manifest.json`
  (SHA256 al momento de la corrida, registrado en el reporte; cohort_a `status=READY`).
- **31 assets · 8 familias · Poly Haven 23 + ambientCG 8**, 1024² PNG lossless, provenance
  `OFFICIAL_HASH_VERIFIED` (23) / `PRIMARY_SOURCE_DOWNLOADED` (8). Licencia CC0-1.0.
- Split congelado en el manifest (`split_by_asset`, regla `CALIBRATION_FAMILIES`):
  **CALIBRATION 15** (brick 5, wood_planks 5, rock 5) / **HELD_OUT 16** (stone 3, tiles 4,
  concrete 3, tiles_paving 3, ground 3). M4 verifica en runtime que `split_of(family)`
  coincida con `split_by_asset` asset por asset (test ancla) — discrepancia = halte.
- El corpus NO vive en el repo (paths del manifest son de la estación Windows original).
  M4 lo re-adquiere con `fetch_exp_m3_primary_corpus.py` y **verifica byte a byte** cada
  SHA256 contra el manifest commiteado. Si un archivo no se puede adquirir o su hash
  difiere → exclusión objetiva registrada (§7). **Si el corpus no es adquirible →
  `EXP_M4_DATA_INSUFFICIENT` y no se inventan resultados.**

## 4. Preprocesamiento — idéntico en ambos caminos donde el diseño lo permite

Compartido EXACTO: resolución de evaluación 512 (igual que M3 RAW), oráculo `H` idéntico,
solver idéntico (`RAW`: `p=-sx·nx/nz` con guard `1e-30`, `integrate_periodic`),
convención de consumo `DIRECTX`, oráculo de evaluación idéntico
(`fit_global_scale` + `OracleOnly.evaluate`).

Asimetrías conocidas y su tratamiento (§16 adversarial):

1. **Resize:** la normal authored pasa por bilinear 1024→512 + renormalización; la SELF
   nace en 512. Es parte de la DEFINICIÓN del control (par coherente EN la resolución de
   trabajo), pero puede contaminar el delta con decoherencia de resize. → **Corrida
   secundaria preregistrada a resolución nativa 1024 sin resize** (AUTH-1024 y SELF-1024)
   para separar el confundo; primaria queda en 512 (comparable con M3).
2. **Cuantización:** SELF se cuantiza a 8-bit (`quantize_decode`) para igualar la
   fidelidad de almacenamiento de los PNG authored → SELF-q8 es el primario. **SELF-float**
   (sin cuantizar) es secundario preregistrado: aísla cuantización vs solver/contorno.
3. **Control FD (diagnóstico, §8 del brief):** forward por diferencias finitas centrales
   (wrap periódico) → mismo solver espectral. Diagnóstico del mismatch FD↔espectral;
   NUNCA reemplaza al par matched como primario.

SELF no recibe ningún preprocesamiento favorable: no hay suavizado, no hay recorte, no
hay normalización extra; la cuantización 8-bit le aplica la misma pérdida de almacenamiento.

## 5. Métricas

- **Primarias** (por asset y camino): `aligned_rmse` (RMSE del rec escalado vs H), 
  `|correlation|`, `variance_ratio` — la MISMA cadena M2/M3 (`OracleOnly.evaluate`),
  invariante a la escala authored.
- **Primaria pareada:** `DELTA_RMSE` (y análogos §2).
- **Diagnósticas:** `seam_height`/`seam_gradient` (wrap toroidal), `nz_floor_hits`
  (fracción de píxeles con |nz|<1e-30 en RAW), `max_gradient`, `runtime_ms`.
- **Diagnóstico de coherencia (NO proxy de trust):** ángulo de desacuerdo entre la normal
  authored y la normal forward del height authored con UNA escala global barrida —
  `OracleOnly.normal_height_residual_oracle` (ya publicado en M3 §38 como
  `height_normal_oracle_agreement_deg`). Se reporta por asset y su relación con
  `DELTA_RMSE`. **No es trust proxy, no entra a ninguna decisión AUTO_SAFE, no se usa
  para excluir assets** (las exclusiones son solo §7).

## 6. Umbrales y reglas de decisión (PRE-REGISTRADAS; derivación en §6.1)

Definiciones: mediana sobre el cohort completo (n=31 menos exclusiones objetivas).
"SELF-good" y "AUTH-worse" se evalúan como conjunciones; la decisión final tiene
precedencia 1→4 (total, sin solapamiento).

| # | Condición | Definición |
|---|-----------|------------|
| G0 | Gate de corpus | usables ≥ 15 y familias ≥ 3 y todas las exclusiones son §7 |
| C1 | SELF-good | `med RMSE_SELF ≤ 0.10` **y** `med |corr_SELF| ≥ 0.95` **y** `med var_SELF ≥ 0.85` |
| C2 | AUTH-worse | `med DELTA_RMSE ≥ 0.02` **y** `med DELTA_RMSE ≥ med RMSE_SELF` **y** `Δmed|corr| ≥ 0.10` **y** held-out consistente (`med DELTA_RMSE_heldout > 0` **y** `Δmed|corr|_heldout > 0`) |

**Decisiones (exactamente una):**

1. `EXP_M4_DATA_INSUFFICIENT` — G0 falla (corpus no adquirible, exclusiones de
   integridad dejan el cohort bajo el mínimo, o split inconsistente con el manifest).
2. `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT` — C1 y C2: el techo del solver es bueno y el
   par authored es claramente peor que el par coherente sobre el MISMO contenido.
3. `EXP_M4_SOLVER_LIMITATION_PRESENT` — ¬C1 y ¬C2: incluso el par coherente se
   reconstruye mal; el solver/contorno es factor limitante en este corpus.
4. `EXP_M4_MIXED` — cualquier otra combinación (p.ej. C1 sin C2: techo bueno pero el
   authored no es claramente peor; o ¬C1 con C2: ambos factores presentes).

Vocabulario EXACTO (uno de): `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT`,
`EXP_M4_SOLVER_LIMITATION_PRESENT`, `EXP_M4_MIXED`, `EXP_M4_DATA_INSUFFICIENT`,
`EXP_M4_INVALIDATED_BY_UPSTREAM_BUG`. **No hay "GO" de producto.**

### 6.1 Derivación de los umbrales (sin números arbitrarios)

- **C1 RMSE ≤ 0.10:** el piso *matched* Q8 de NP-M0 §E1 en contenido periódico ya alcanza
  RMSE 0.0785 en contenido empinado (S14_steep: RMSE 0.0785, corr 0.9994; S09_bricks:
  0.0175). Contenido real no-periódico (wrap de borde) + resize 1024→512 no puede estar
  por debajo del piso Q8. 0.10 ≈ piso S14 + margen de contorno/resize, y es 2.5× más
  estricto que `CATASTROPHIC_RMSE = 0.25` (M2/M3). Un techo por encima de esto no puede
  llamarse "bueno".
- **C1 |corr| ≥ 0.95:** piso Q8 matched en los peores sintéticos no-empinados: corr
  ≥ 0.9976 (S09_bricks 0.9976, S08 0.99996). El AUTH publicado (M3 RAW@512) tiene mediana
  0.8392 — un techo honesto debe quedar claramente por encima; 0.95 deja margen para
  pérdida de contorno/banda en contenido real sin admitir techos blandos.
- **C1 var ≥ 0.85:** el AUTH M3 mediana es 0.7043 con 8/31 catastróficos (regla var<0.5
  o |corr|<0.5). Un par coherente no debería perder >15% de la varianza del oráculo
  (el piso Q8 periódico es ~1.0 salvo contenido empinado; 0.85 admite empinamiento real).
- **C2 DELTA_RMSE ≥ 0.02:** orden de magnitud del piso Q8 típico no-empinado
  (1.8e-3..1.75e-2): un delta menor que el propio piso de cuantización no es interpretable.
- **C2 delta ≥ med RMSE_SELF (dominancia):** el error del par authored debe ser al menos
  tan grande como TODO el error del techo — si no, el solver explica la mayor parte.
- **C2 Δcorr ≥ 0.10:** separación ≥ 3× la granularidad de mediana corr con n=31
  (bootstrapeada en M3);/auth 0.8392 vs techo esperado ≥0.95 da margen real.
- **Consistencia held-out:** el efecto debe replicarse en dirección en el 51% del cohort
  que nunca tocó calibración (anti-fuga §14/§19).

## 7. Exclusiones (objetivas, pre-registradas, todas registradas con razón)

Solo: archivo faltante / corrupto / ilegible, SHA256 ≠ manifest, formato no soportado,
resolución ≠ manifest, provenance inválida (gate M3 §9), convención UNKNOWN
(fail-closed §15 M3). **PROHIBIDO excluir por resultado de reconstrucción, por "se ve
mal", o por outlier estadístico.** Cero exclusiones esperadas: el corpus M3 ya pasó
estos gates.

## 8. Protocolo anti-fuga (freeze)

1. Este preregistro se committe ANTES de adquirir el corpus o correr cualquier cosa
   sobre Cohort A.
2. Calibración (15 assets brick/wood/rock) = verificación de IMPLEMENTACIÓN: los tests
   sintéticos (§9) deben pasar; si C1/C2 se evalúan en calibración es SOLO smoke-test
   del harness. Los umbrales NO se retunean tras ver calibración (si hubiera un bug de
   implementación → fix + test de regresión + commit separado + re-corrida, documentado).
3. Freeze: tras calibración OK se confirma por commit que métricas/thresholds/transformas/
   exclusiones están congelados; recién entonces corre held-out.
4. Si se mira held-out accidentalmente antes del freeze → se documenta honestamente y el
   estado pasa a revisión (§26 del brief).

## 9. Batería sintética (el SELF debe matchear teoría ANTES de tocar Cohort A)

Obligatorias (todas con tests automatizados en `tests/test_native_parallax_exp_m4.py`):

- **Plano** (constante): SELF-q8 → RMSE ≈ 0 (solo ruido de cuantización), DC no cuenta.
- **Seno único periódico:** SELF-float → recupera H con error ~1e-15; SELF-q8 acotado
  (≤ 2× el piso S06 3.3e-4 → ≤ 1e-3); corr > 0.999.
- **Multifrecuencia:** mismo patrón que E1 S06 (RMSE_q8 ≤ 1e-3).
- **Empinado:** SELF-q8 error acotado y corr ≥ 0.99 — el techo crece con la pendiente
  pero no explota (replica cualitativamente S14_steep).
- **DC offset:** sumar constante a H no cambia el resultado (ĥ[0,0]=0 es inobservable y
  no debe contar como error; el RMSE se mide contra H centrado por el oráculo afín).
- **Escala/signo:** multiplicar H por s>0 / s<0 → rec escala igual; |corr| invariante.
- **Nyquist-edge:** modo exactamente Nyquist en un eje → se pierde (gradiente anulado);
  el test LO DOCUMENTA como pérdida esperada (no como bug).
- **No finito / shape equivocada:** NaN/Inf en H o N → error claro (fail-fast); shapes no
  cuadradas pares soportadas; impares → error explícito (mismo contrato M0).
- **Determinismo:** dos corridas idénticas → bit a bit idéntico (sin RNG en el camino
  primario; el bootstrap usa seed preregistrada 20260925).
- **Reglas de decisión:** tabla de verdad completa de C1/C2→decisión (4+ casos unitarios).

Si el SELF sintético NO matchea teoría → **halte: no se corre Cohort A** hasta arreglarlo.

## 10. Reproducibilidad (por corrida, en el JSON del reporte)

git SHA (head del worktree), SHA256 del manifest M3 consumido, versión del algoritmo
(= funciones citadas + sha de `solver_coherence.py`), resolución, seed bootstrap,
timestamp UTC, Python/NumPy/Pillow, backend FFT (numpy), counts de exclusión.
JSON con `allow_nan=False`; NaN/Inf en métricas → error fail-fast (contrato M0/M2).

## 11. Límites (lenguaje small-n)

n=31 pareado, 8 familias de n=3..5: todo lo por-familia y por-provider es **diagnóstico
suggestivo** (Poly Haven vs ambientCG: solo lectura de tendencia, jamás atribución de
motivo). La conclusión primaria es sobre el cohort completo. "Incoherencia del par"
significa "no coherentes bajo el modelo gradiente-height ensayado en esta convención y
escala" — ni validación ni condena de los assets.

## 12. Checklist adversarial (antes de cerrar)

¿SELF artificialmente fácil? (define el techo, no invalida — pero revisar que el delta no
sea un artefacto del resize: §4.1 secundaria a 1024) · ¿escala absorbida por el afín?
(verificar con `raw_centered_rmse`/`gradient_rmse` que el delta no sea solo unidades) ·
¿contaminación de convención? (tested_convention auditado por fila; todas OPENGL→DIRECTX) ·
¿resize asimétrico? (§4.1) · ¿16-bit height preservado? (los 31 son PNG 8/16-bit según
manifest; `decode_height_image` M3 maneja `I;16` — verificar bit depths reales) · ¿seams?
(reportados por asset) · ¿dominación por familia? (mediana global + por familia) · ¿PH vs
aCG? (diagnóstico) · ¿held-out contradice calibración? (si sí → reportarlo como tal) ·
wording "pair mismatch" vs "model mismatch" (el hallazgo B incluye modelo/escala/filtros;
no se afirma "el proveedor miente").

## 13. Entregables

- `research/solver_coherence.py` — forward/inversor emparejados reutilizando M0/M1/M2
  (sin matemática duplicada: `spectral_gradients`, `normals_from_gradients`,
  `quantize_decode`, `reconstruct_from_normal`, `OracleOnly`).
- `research/run_exp_m4.py` — corrida completa: carga manifest M3 + corpus verificado,
  SELF/AUTH/controles, C1/C2, decisión, JSON (permit_nan=False) + resumen md.
- `tests/test_native_parallax_exp_m4.py` — batería §9 + reglas + paths-mapping.
- Este doc + resultados en §14 (POST-corrida, commit separado del preregistro).
- Agregados pequeños en `data/` (JSON de filas/resumen); NUNCA el corpus.

## 14. Resultados

_(POST-corrida — se llena en un commit posterior al preregistro; nada de esto existe aún.)_

## 15. Exploratorio post-corrida

_(Vacío por diseño; cualquier idea posterior a los resultados vive aquí y NUNCA se
presenta como preregistrada.)_
