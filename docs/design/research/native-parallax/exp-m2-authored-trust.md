# EXP-M2 — Authored normals and effective trust

> Research-only. Sin Skyrim, MO2, PGPatcher, DDS output, IA ni ParallaxR.
> **Estado: `EXP_M2_CONDITIONAL` — esperando revisión independiente. NO merge.**

## Repository/base

- **MAIN_SHA:** `607ff21` · **#617:** OPEN @ `14b23b0` (intacto) · **#618:** OPEN @ `e2a449b`
  (BASE_SHA usado; verificado en pre-push).
- **BRANCH:** `research/native-parallax-exp-m2-authored-trust` (apilada sobre el head de #618).
- **PR (stacked):** base = rama de #618. DO NOT MERGE.
- **Entorno (§46):** Python 3.11.2 · NumPy 2.4.6 · Pillow 12.3.0 · Linux 6.1 (sandbox).
  Manifest SHA256 (prefijo): `4d00501949fc006d`.

## Dataset provenance

Restricción de infraestructura: el sandbox sólo tiene salida a `github.com`,
`codeload.github.com` y PyPI (HuggingFace/MatSynth, ambientCG directo, PolyHaven,
Zenodo: **bloqueados por el proxy**). El corpus se adquirió vía **tres mirrors
públicos de GitHub que venden assets verbatim de ambientCG** (assets con su asset_id
canónico, verificado contra el árbol de cada repo):

| mirror | assets | naming normal | convención declarada |
|---|---:|---|---|
| `Calinou/godot-cmvalley` | 26 | `<ID>_2K_Normal.jpg` (vieja) | UNKNOWN |
| `petroulacl/fps-buildings-env-kit` | 7 (5 nuevos) | `<ID>_2K-JPG_NormalGL/DX.jpg` | OPENGL |
| `BastiaanOlij/drawable-textures-demo` | 3 | `<ID>_2K-JPG_NormalGL/DX.jpg` | OPENGL |

**34 materiales únicos** · resolución nativa 2048² (JPG) → research 512² (subset 1024²).

## Licenses

- **Verificado en fuente primaria**: "All ambientCG assets are provided under the
  Creative Commons CC0 1.0 Universal License" — `https://docs.ambientcg.com/license/`
  (plataforma-completa; aplica a los archivos descargables). Sin ambigüedad → ningún
  asset excluido por licencia.
- Spot-check vía API oficial (`/api/v2/full_json?id=…`): 7/34 IDs confirmados con
  `maps` incluyendo `displacement` + `normal` (Rock030 además con
  `creationMethod: Height field photogrammetry`).
- SHA256 por archivo en el manifest; `verify_against_manifest` falla si los bits
  cambian (§43). Los assets NO se committean al repo.

## Corpus composition

14 familias (mínimo pedido: 4–5): brick 6 · rock 4 · metal 4 · wood_floor 3 ·
concrete 3 · asphalt_road 3 · ground_grass 2 · tiles_paving 2 · wood_planks 2 ·
ground 1 · ground_gravel 1 · ground_snow 1 · tiles 1 · wood_generic 1.
**Split por familia (§22):** CALIBRATION = brick + rock + wood_planks (12) ·
HELD_OUT = las otras 11 familias (22). **Fuente única (ambientCG)** — limitación
declarada (§23): no hay prueba calibrate-source-A/evaluate-source-B.

## Normal/height semantics

- Convención normal: **NO documentada** por ambientCG (docs sólo tienen
  license/asset-types/api) → los 24 assets con naming viejo quedan `UNKNOWN`; los 10
  con `NormalGL` declarados OPENGL. Diagnóstico evaluation-only (§12): **FLIPPED nunca
  ganó** en los 24 UNKNOWN → los maps viejos son consistentes as-is (veredicto:
  probablemente GL, registrado como evidencia, no como suposición productiva).
- Height: escala NO documentada; `UNKNOWN_SCALE_RELATIVE_GRAYSCALE`. El oráculo ajusta
  escala afín global + signo (§11/§18); la escala fitada **varía 3 órdenes de
  magnitud entre assets** (2e-4 … 2.5e+3) — la "strength" authored es arbitraria por
  diseño (§38) y ningún umbral absoluto de RMSE tendría sentido sin el alineamiento.

## Preprocessing

Normal: decode RGB→[-1,1]³ + renormalización; resize vector-aware canal por canal +
**renormalización** (§13, M3). Height: luminancia 8-bit→[0,1], resize bilineal.
Resolución uniforme 512²; subset 1024² para sensibilidad.

## Periodicity characterization

9/34 assets con seam ratio ≥3 (BAD_BOUNDARY): Planks012/014, WoodFloor027/030/043,
Bricks019/066, Concrete027, PavingStones002. **Pero el seam NO predice el fallo de
reconstrucción**: Bricks019 (seam 8.4) reconstruye con var 0.993 y Bricks066 (seam 3.3,
la rotura real) falla por correlación negativa, no por la costura. Registrado como
evidencia (§14), sin penalizar al solver.

## RAW baseline

Mediana rmse alineado **0.0434** · p90 **0.1033** · peor **0.1743** · mediana
variance_ratio **0.631** · mediana corr **0.794** · **catastróficos 15/34 (44%)**.
vs NP-M0 sintético (med corr ~1.0, var ~1.0): **H2 confirmada — authored es
materialmente peor**, y no por nz (nz_p01 ∈ [0.19, 1.00]: las normales authored
sanas están LEJOS de la singularidad).

## Oracle normal-height mismatch

El agreement angular normal↔height (con escala global ± fitada) varía **0.3°…89°**:
rocas/ladrillos/nieve 5–26° (coherentes); **grass 88–89°, gravel 51°, WoodFloor027
81°, Wood043 66°, Metal063 65°** (incoherentes o height plano/decorrelado). Aun así,
el oráculo por sí solo es un clasificador PÉSIMO del rmse final (Spearman all +0.14,
AUC 0.60): la reconstrucción FFT es robusta al mismatch de fuerza global y el daño
viene de heights planos o decorrelados.

## Normal-only trust proxies

Sobre cada asset (sólo normal): nz stats, curl/integrabilidad (mediana/MAD/p95),
residuo de proyección integrable (ángulo), proxy de cuantización, blockiness JPEG,
residuo de longitud unitaria, low-trust clusters. Resultado clave:

| proxy | ρ(cal) | ρ(held) | AUC(held) |
|---|---:|---:|---:|
| nz_min / nz_p01 / nz_p05 | −0.20/−0.37/−0.33 | −0.44/−0.43/−0.40 | 0.62/0.60/0.59 |
| curl_mad / curl_p95 | +0.34/+0.57 | +0.39/+0.42 | 0.48/0.44 |
| projection_residual (med) | −0.32 | +0.37 | 0.47 |
| blockiness_ratio | +0.38 | −0.43 | 0.49 |
| quantization_proxy | +0.17 | −0.35 | 0.50 |
| unit_length_residual_p95 | +0.17 | +0.01 | 0.38 |
| **ORACLE agreement (contexto)** | — | — | 0.60 |

## Proxy correlations

**Ninguna señal normal-only predice el riesgo authored.** Los signos cambian entre
calibración y held-out (projection, blockiness, quantization) → las correlaciones
positivas de calibración son artefactos del split, no señales. El mejor AUC (nz_min
0.62) es apenas mejor que el azar y peor que el propio oráculo (0.60). Con
corpus=34 y 15 catastróficos, el poder estadístico es bajo, pero la CONVERGENCIA de
todas las señales hacia "no sirve" es el hallazgo (§69).

## Held-out evaluation

RAW held-out: med rmse 0.0469, med var 0.377, 12/22 catastróficos. Por asset:
ver tabla §47 abajo.

## Family generalization

El riesgo es **estructural de familia**, no de asset individual:
`snow 0.998 · brick 0.965 · rock 0.937` (excelentes) vs `metal 0.006 ·
ground_grass 0.001 · ground 0.000` (catastróficos); wood intermedio-malo
(0.26–0.68). Un promedio global escondería exactamente esto (§48). La reconstrucción
funciona en un dominio identificable — pero por FAMILIA (metadata), no por señal
normal-only medible.

## Source generalization

No evaluable: fuente única (ambientCG). Limitación registrada.

## EXP-M1 policy transfer

Clasificación (§54), evaluada UNA vez sin retuning (§32):

- **SOFT λ=1·σ_eff: FAILED_TO_TRANSFER.** El σ_eff ganador de calibración fue
  `nz_p01` (ρ +0.37) — pero nz_p01 **no es una escala de ruido**: usarlo como λ
  atenúa la mitad de los gradientes → held-out med var **0.097 vs 0.377 RAW** (relieve
  destruido) con rmse −3.8% (peor). Mejora rmse en 4 assets (PavingStones002 −51%) a
  costa de aplastarlos: exactamente el fallo anti-flattening (§34).
- **CLAMP λ=1·σ_eff: PARTIALLY_TRANSFERRED (no-harm, no-help).** med rmse −3.4%,
  var 0.376 (≈RAW): el clamp sólo toca la cola y en authored la cola nz no es el
  problema.
- **Bandas sintéticas {2,6} (y {1,4}): NOT_EVALUABLE con el proxy candidato — no
  refutadas.** Con σ_eff=nz_p01 el cociente r_p01 = nz_p01/σ_eff ≡ 1 **por
  construcción** (el denominador es la misma señal que el numerador): la prueba es
  circular y no puede ni confirmar ni refutar las bandas. Lo que sí queda medido es
  que el eje nz/σ es irrelevante para el fallo authored dominante (nz_p01∈[0.19,1.00]
  en TODOS los assets, buenos y malos por igual). Probar las bandas de verdad exige
  un denominador INDEPENDIENTE de nz (residuo de proyección/curl/cuantización) o
  abandonar la forma nz/σ y llamarlo risk_score.
- **M2-F sweep k (sólo calibración, referencia):** CLAMP k=0.5 med rmse 0.0408 /
  var 0.905 (leve mejora); SOFT monótonamente aplasta con k. Ninguna k rescata a los
  catastróficos: sus fallos no son de regularización.

## SOFT k=1 transfer

= FAILED (ver arriba). La causa raíz: **la hipótesis "σ_eff estimable desde la
normal" fue falsificada en este corpus** — no hay σ que estimar porque el daño no es
ruido de canal sino incoherencia del dataset.

## Synthetic thresholds {2,6} transfer

**NOT_EVALUABLE_WITH_CANDIDATE_PROXY** (corrección post-review: antes decía
"DOES_NOT_TRANSFER", que sobreactúa). σ_eff=nz_p01 hace r_p01≡1 por definición:
construcción circular ⇒ las bandas quedaron **sin probar**, ni a favor ni en contra.
El régimen EXP-M1 (nz≈σ) de todos modos no aparece en normales authored 8-bit sanas
(nz_p01 ≥ 0.19 en el corpus), por lo que no había contraste disponible con este
dataset.

## Cluster analysis

`largest_low_trust_component_fraction` y `number_of_low_trust_components`
implementados (§28); con σ_eff degenerado no discriminan. Los clusters que IMPORTAN
en authored son de otro tipo (inversión coherente, height plano) y no viven en nz.

## Q8_XY proxy

TWO_CHANNEL_Q8_PROXY (NO "BC5 simulation"): mediana Δrmse **+0.0%** — la cuantización
XY 8-bit es casi inofensiva para la mayoría. **EXCEPCIÓN CLAVE: rocas/cliffs**
(Rock029 +305%, Rock030 +197% rmse, variance_ratio ~1e48): normales cercanas al borde
del disco → `nz=sqrt(1-x²-y²)` reconstruido se desploma (xy_invalid=0%: NO son
radicandos inválidos, son nz≈0 legítimos post-cuantización). Advertencia dirigida
para EXP-003: el encoding 2-canales es seguro salvo en tangentes extremas.

## Resolution sensitivity

512→1024 (8 assets): RAW estable (Bricks014 0.0036→0.0021; Snow/Metal018 idénticos);
**Planks012 MEJORA** (rmse 0.103→0.085, var 0.41→0.62): el downsampling promediante
fue parte de su problema. Grass004 hopeles en ambas (incoherencia, no resolución).

## REVIEW_RATE

Curva con el mejor proxy (nz_p01 absoluto; 33 assets útiles, Metal049A flat
excluido como DATASET_FLAT):

| umbral REJECT | AUTO_SAFE | REJECT | cat. false-accept | false-reject | REVIEW_RATE |
|---:|---:|---:|---:|---:|---:|
| nz_p01<0.2 | 32 | 1 | **14** | 1 | 3% |
| nz_p01<0.4 | 30 | 3 | **13** | 2 | 9% |
| nz_p01<0.5 | 26 | 7 | **12** | 5 | 21% |
| nz_p01<0.7 | 21 | 12 | **10** | 8 | 36% |

**Ningún umbral baja de 10 aceptaciones catastróficas** (§60: CATASTROPHIC_FALSE_
ACCEPT_RATE mínimo ~30% incluso con 36% REVIEW). La curva dice claramente: el
candidato normal-only NO es operativo.

## Catastrophic false accepts

Ver curva: 10–14 según umbral. Peor aún: los 5 assets que el proxy MÁS rechazaría
(bajo nz_p01) incluyen rock/brick BUENOS (false-rejects), mientras los fallos reales
(height plano/decorrelado, nz_p01 0.86–1.00) se aceptan.

## Outliers

**5 peores:** Metal049A (DATASET_FLAT: normal Y height planos — no es fallo del
solver ni del proxy: es un asset sin problema que resolver) · Asphalt021 (corr≈0.00,
height decorrelado) · Ground037 (corr 0.008) · Grass004 (agree 89°, height≈ruido) ·
Metal062C (corr 0.03). Causa común: **el height authored no codifica la misma
superficie que la normal** (§38: intención artística/datos rotos — hallazgo del
dataset, no bug nuestro).
**5 mejores:** Bricks014 (corr 1.000), Bricks017, Snow002, Rock010, Bricks019 —
fotogrametría/procedurales coherentes.

## Hypotheses H1-H8

- **H1 SURVIVED** — la integración periódica recupera estructura authored real
  (corr 1.000 bricks; 0.99 rocks/snow): 19/34 assets con var_ratio ≥ 0.9.
- **H2 SURVIVED** — authored ≫ peor que sintético: 44% catastróficos vs ~0% sintético.
- **H3 INCONCLUSIVE** — nz_min sin outliers extremos en authored (nz sano); la
  pregunta no tuvo régimen donde probarse.
- **H4 FALSIFICADO como candidato operativo** — nz_p01 fue el mejor score (AUC 0.60)
  pero insuficiente; y "su escala σ" no existe como ruido.
- **H5 FALSIFICADO en este corpus** — ningún proxy normal-only supera AUC 0.62 y los
  signos son inconsistentes cal→held.
- **H6 FALSIFICADO** — SOFT λ=1·σ_eff aplasta el relieve (med var 0.097) porque
  σ_eff no es escala de ruido.
- **H7 SURVIVED con cambio de régimen** — REJECT sigue siendo la decisión correcta,
  pero el régimen a rechazar es la INCOHERENCIA normal↔height, invisible a señales
  normal-only.
- **H8 FALSIFICADO para proxies** (no generalizan ni en signo); la reconstrucción
  misma sí es family-dependent (brick/rock/snow ✓; wood/metal/grass ✗).

## Mutation sanity

M1 fuga height → `check_no_height_in_features` + `reconstruct_from_normal` normal-only
(test de firma) ✓ · M2 fuga held-out en calibración → `split_of`/disjuntos por familia ✓ ·
M3 resize sin renormalizar → `resize_normal` test norma=1 ✓ · M4 convención UNKNOWN
asumida → `apply_convention` lanza + manifest valida ✓ · M5 licencia no verificada →
`load_manifest` rechaza; resolución/tamaño ilegible → `DatasetInvalidError` ✓ ·
M6 radicando contado como negativa → `two_channel_q8_proxy` separa las tres fracciones ✓ ·
M7 hf gigante en vez de NOT_INFORMATIVE → `evaluate` devuelve NaN + `hf_gt_fraction` ✓ ·
M8 aplastar varianza → `variance_ratio` con escala compartida RAW detecta (test M8) ✓ ·
M9 fitting local → firma del oráculo sólo global (test M9) ✓ · M10 dataset inválido
contado como fallo del solver → gate + `skipped` (test M10) ✓.

## Adversarial answers

1. ¿Cuánto peor es authored? 44% catastróficos; med corr 0.794 vs ~1.0 sintético.
2. ¿Qué proxy predice mejor el fallo? Ninguno alcanza; mejor nz_min AUC 0.62.
3. ¿Supera a nz_p01 solo? Dentro del margen de ruido (n pequeño); ninguna combinación
   justifica complejidad (§52).
4. ¿Generaliza entre familias? No: signos inconsistentes cal→held.
5. ¿Entre fuentes? No evaluable (fuente única) — limitación registrada.
6. ¿SOFT k=1 transfiere? NO: aplasta (var 0.097).
7. ¿Bandas {2,6}? **No evaluables** con el proxy candidato: σ_eff=nz_p01 ⇒ r≡1 por
   construcción (circular). Quedan sin refutar; re-probarlas exige denominador
   independiente de nz (projection/curl/quantization residual) o renombrar a risk_score.
8. ¿Resolución? RAW estable 512↔1024; Planks012 mejora a 1024 (downsampling).
9. ¿Cuánto error es inconsistencia del dataset? La mayoría: 10+ de 15 catastróficos
   por height plano/decorrelado/invertido, 3 por normal ruidosa (planks), 1 flat.
10. ¿Author intent o física? Intent artístico: la escala fitada varía 10³×; el
    agreement 0.3–89° — medimos CONSISTENCIA matemática, no verdad física (§38).
11. ¿Los peores casos detectables pre-reconstrucción? NO con señales normal-only
    (AUC≤0.62); el propio oráculo angular apenas 0.60.
12. ¿REJECT mejor que salvage? Sí — pero no sabemos a QUIÉN rechazar sin el height.
13. ¿Qué % quedaría en REVIEW? 21–36% con umbrales agresivos, aún con 10 CFA.
14. ¿Operativamente viable? NO como auto-accept; viable como triage HITL con
    family-level gating (brick/rock/snow → auto; wood/metal/grass → revisión).
15. ¿Qué contradice a EXP-M1? Su eje de riesgo (nz≈σ) NO es el régimen dominante
    authored: nz_p01∈[0.19,1.00]. EXP-M1 queda VÁLIDO para su dominio (ruido de canal
    / compresión agresiva) y la lección Q8_XY sobre rocas lo conecta: los cliffs
    authored SÍ llegan a nz≈0 post-cuantización.

## Limitations

Corpus de 34 assets de UNA fuente (ambientCG) via mirrors de terceros (mitigado con
asset_id canónico + SHA256 + spot-check API); heights JPG 8-bit con semántica no
documentada; 12/22 catastróficos en held-out dan AUC con intervalo amplio; MatSynth
(inaccesible desde el sandbox) podría cambiar el panorama con heights EXR coherentes;
el "trust" aquí es riesgo de RECONSTRUCCIÓN, no de parallax perceptual.

## Decision

**EXP_M2_CONDITIONAL** (→ `EXP_M2_CONDITIONAL_WAITING_REVIEW`):
- La reconstrucción authored FUNCIONA en familias coherentes (brick/rock/snow/tiles:
  var 0.81–1.00, corr ≥0.99) — hay dominio útil identificable.
- La predicción de riesgo normal-only FALLÓ en este corpus (mejor AUC 0.62, signos
  inconsistentes, REVIEW_RATE no viable, CFA ≥30%) → nada de esto es promo-
  vible a política automática; el gating realista es por familia/HITL (REVIEW absorbe
  fracción grande) — exactamente el criterio CONDITIONAL del §55.
- No se downgradea a NO_GO porque la incoherencia medida es en gran parte del
  DATASET (heights planos/decorrelados de JPG) y §56 reserva el veredicto: un NO_GO
  de "normal-only automation" no mata authoring-assistant/HITL/albedo-assisted.

## ONE next experiment

**EXP-M3 — ¿contaminación del dataset o límite fundamental del enfoque normal-only?**
(rediseñado tras review adversarial; una sola pregunta, dos cohortes):

- **Cohorte A — clean-by-provenance (evidencia fuerte):** assets donde la procedencia
  documenta que normal y displacement describen la misma superficie (MatSynth con
  height 16-bit, o ambientCG descargado de fuente primaria con upstream-identity
  verificada por asset — no mirrors). Descarga primaria cuando la red lo permita;
  manifest con licencia + hash + provenance por asset (corrige la limitación de los
  mirrors de EXP-M2).
- **Cohorte B — oracle-filtered diagnostic (etiquetada `EVALUATION_DIAGNOSTIC_ONLY`,
  NUNCA production-selection):** subset con `oracle_agreement < X` y height no-plano.
  Responde exclusivamente: "asumiendo que normal y height describen la misma
  geometría, ¿aparece alguna señal normal-only útil?" — no puede usarse para decir
  "nuestro predictor funciona en assets confiables" porque el filtro usa el height.

Dos resultados válidos: **(A)** los proxies correlacionan en corpus limpio → EXP-M2
estaba contaminado por mismatch artístico/dataset, se sigue investigando; **(B)** siguen
sin predecir → evidencia fuerte para NO_GO de *normal-only automatic trust* y pivote a
reconstrucción determinista + HITL (o normal+albedo después), sin exprimir más nz.
**Prohibido** en M3: σ_eff = f(nz_p01) si luego se evalúa nz_p01/σ_eff (circularidad);
el denominador debe salir de una señal independiente o se abandona la forma nz/σ.
No iniciarlo desde esta rama.

## Adenda de revisión adversarial (post-publicación)

Cuatro señalamientos del review externo, verificados y cerrados:

1. **Trazabilidad Git 7383d0c → e2a449b.** `git merge-base --is-ancestor 7383d0c
   e2a449b` = cierto; el rango `7383d0c..e2a449b` contiene EXACTAMENTE un commit:
   `e2a449b docs(parallax): exp-m1 gap-band fill r_p01 in (1.8,5.4)` — commit propio
   del addendum de llenado de banda de EXP-M1 (turno de continuación), que actualizó
   #618 legítimamente tras el reporte donde su head era `7383d0c`. Sin commits
   ajenos, sin rewrite, sin force-push. Adicionalmente se detectó y documentó: entre
   sesiones el sandbox fue restaurado a un clone shallow @ `30439a1` (punto de
   partida original), perdiendo objetos locales; se recuperaron las ramas desde
   origin, se verificó byte-a-byte (25/25 archivos idénticos vía `git hash-object`
   contra el árbol de `f692751`) y el estado dirty ajeno al spike (pyproject/vfs @
   `30439a1`) quedó preservado en `stash@{0}` sin mezclarlo.
2. **Conteos de tests corregidos.** `pytest --collect-only` objetivo:
   NP-M0 (`test_native_parallax_math_spike.py`) = **24** nodos · EXP-M1 = **18** ·
   EXP-M2 = **17** (17 funciones, sin parametrizaciones) → focal 59 ✓.
   Suite completa: 7619 + 18 + 17 = **7654** ✓. Los números "NP-M0: 7/7" y
   "EXP-M2: 34 tests nuevos" del informe/chat/PR-body anterior fueron errores de
   transcripción; los resultados no cambian (mismas ejecuciones, misma aritmética).
3. **Circularidad r = nz_p01/σ_eff con σ_eff=nz_p01.** Aceptada: las bandas {2,6}
   quedaron NOT_EVALUABLE (no refutadas); la sección de transfer y la respuesta
   adversarial #7 fueron reescritas. Regla registrada para EXP-M3: el denominador de
   r debe provenir de una señal independiente de nz, o se abandona la forma nz/σ por
   un risk_score sin esa geometría.
4. **Procedencia del dataset (limitación reconocida).** El spot-check 7/34 + licencia
   de plataforma prueban la licencia, no la identidad bit-a-bit upstream de los 34
   assets servidos por mirrors. Suficiente para research exploratorio; EXP-M3 exige
   fuente primaria/identity por asset (Cohorte A). No se re-ejecuta EXP-M2.

## Results table (por asset — RAW baseline 512²)

| asset | familia | split | nz_p01 | align_rmse | grad_rmse | corr | var_ratio | seam_h/N | agree° | cat | causa |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| Bricks014 | brick | CALI | 0.62 | 0.0036 | 1.5 | +1.000 | 0.999 | 2.1 | 19 | ok | — |
| Bricks017 | brick | CALI | 0.49 | 0.0053 | 1.5 | +0.999 | 0.999 | 1.9 | 13 | ok | — |
| Snow002 | ground_snow | HELD | 0.96 | 0.0087 | 1.4 | +0.999 | 0.998 | 1.3 | 13 | ok | — |
| Rock010 | rock | CALI | 0.77 | 0.0101 | 1.4 | +0.999 | 0.997 | 1.6 | 6 | ok | — |
| Bricks019 | brick | CALI | 0.48 | 0.0106 | 2.0 | +0.996 | 0.993 | 8.4 | 6 | ok | — |
| PavingStones002 | tiles_paving | HELD | 0.64 | 0.0163 | 3.8 | +0.990 | 0.979 | 5.9 | 26 | ok | — |
| Asphalt013 | asphalt_road | HELD | 0.96 | 0.0081 | 7.5 | +0.987 | 0.974 | 1.3 | 42 | ok | — |
| Metal018 | metal | HELD | 0.99 | 0.0261 | 5.9 | +0.986 | 0.973 | 1.4 | 32 | ok | — |
| Rock029 | rock | CALI | 0.27 | 0.0408 | 3.6 | +0.976 | 0.952 | 1.5 | 13 | ok | — |
| Bricks040 | brick | CALI | 0.83 | 0.0382 | 5.7 | +0.968 | 0.938 | 2.8 | 26 | ok | — |
| Rock030 | rock | CALI | 0.19 | 0.0611 | 6.3 | +0.961 | 0.923 | 1.1 | 14 | ok | — |
| Rock022 | rock | CALI | 0.48 | 0.0241 | 2.0 | +0.952 | 0.907 | 1.7 | 10 | ok | — |
| Concrete035 | concrete | HELD | 1.00 | 0.0232 | 7.9 | +0.919 | 0.845 | 1.5 | 49 | ok | — |
| Bricks032 | brick | CALI | 0.81 | 0.0586 | 12.0 | +0.914 | 0.836 | 1.3 | 53 | ok | — |
| Tiles049 | tiles | HELD | 0.79 | 0.0478 | 8.4 | +0.902 | 0.813 | 2.8 | 35 | ok | — |
| PavingStones054 | tiles_paving | HELD | 0.68 | 0.0878 | 10.5 | +0.836 | 0.700 | 1.2 | 24 | ok | — |
| Wood043 | wood_generic | HELD | 1.00 | 0.0279 | 12.9 | +0.821 | 0.675 | 2.4 | 66 | ok | — |
| WoodFloor043 | wood_floor | HELD | 0.93 | 0.0532 | 12.0 | +0.767 | 0.588 | 9.0 | 13 | ok | — |
| Concrete027 | concrete | HELD | 0.97 | 0.0399 | 5.8 | +0.709 | 0.502 | 3.7 | 17 | ok | — |
| Gravel011 | ground_gravel | HELD | 0.59 | 0.0660 | 15.6 | +0.701 | 0.491 | 1.2 | 51 | CAT | NORMAL_HEIGHT_MISMATCH (51°) |
| Planks012 | wood_planks | CALI | 0.38 | 0.1026 | 19.1 | +0.639 | 0.409 | 4.9 | 27 | CAT | NORMAL_RUIDOSA (curl 1040) + seam |
| Planks014 | wood_planks | CALI | 0.44 | 0.1007 | 22.5 | +0.513 | 0.263 | 8.4 | 37 | CAT | NORMAL_RUIDOSA (curl 976) + seam |
| WoodFloor030 | wood_floor | HELD | 1.00 | 0.0277 | 6.7 | +0.513 | 0.263 | 5.0 | 22 | CAT | HEIGHT_SEMANTICA (corr 0.51 var baja) |
| WoodFloor027 | wood_floor | HELD | 1.00 | 0.0877 | 22.3 | +0.430 | 0.185 | 4.4 | 81 | CAT | HEIGHT_MISMATCH (81°) + seam |
| Bricks066 | brick | CALI | 0.56 | 0.1090 | 14.2 | +0.410 | 0.168 | 4.0 | 7 | CAT | SEAM/BORDE + corr negativa |
| Road001 | asphalt_road | HELD | 0.99 | 0.0692 | 7.6 | +0.228 | 0.052 | 1.1 | 19 | CAT | HEIGHT_DEBIL (corr 0.23) |
| Metal063 | metal | HELD | 1.00 | 0.0387 | 18.6 | +0.110 | 0.012 | 1.3 | 65 | CAT | HEIGHT_DEBIL/SEMANTICA |
| Concrete012 | concrete | HELD | 0.92 | 0.1743 | 6.0 | +0.081 | 0.007 | 1.7 | 18 | CAT | HEIGHT_DECORRELADO |
| Grass005 | ground_grass | HELD | 0.95 | 0.0618 | 31.7 | +0.033 | 0.001 | 1.4 | 88 | CAT | NORMAL_HEIGHT_MISMATCH (88°) |
| Metal062C | metal | HELD | 1.00 | 0.0460 | 8.8 | +0.030 | 0.001 | 1.2 | 23 | CAT | HEIGHT_DECORRELADO (corr 0.03) |
| Grass004 | ground_grass | HELD | 0.86 | 0.1036 | 53.3 | +0.008 | 0.000 | 1.2 | 89 | CAT | NORMAL_HEIGHT_MISMATCH (89°) |
| Ground037 | ground | HELD | 0.90 | 0.0514 | 6.7 | +0.008 | 0.000 | 1.4 | 20 | CAT | HEIGHT_DECORRELADO/PLANO |
| Asphalt021 | asphalt_road | HELD | 0.93 | 0.1113 | 24.8 | +0.000 | 0.000 | 1.1 | 51 | CAT | HEIGHT_DECORRELADO (corr≈0) |
| Metal049A | metal | HELD | 1.00 | 0.0000 | 0.0 | +0.000 | 0.000 | 0.0 | 0 | CAT | DATASET_FLAT (normal y height planos) |

Resumen RAW: med_rmse=0.0434 p90_rmse=0.1033 worst_rmse=0.1743 med_var=0.631 med_corr=0.794 cat=15/34

Spearman(proxy, aligned_rmse) [cal | held | all]:
  nz_min                         -0.203 | -0.443 | -0.168
  nz_p01                         -0.371 | -0.425 | -0.165
  nz_p05                         -0.329 | -0.398 | -0.157
  curl_mad                       +0.336 | +0.394 | +0.312
  curl_p95_abs                   +0.566 | +0.421 | +0.286
  projection_residual_median_deg -0.322 | +0.371 | +0.064
  projection_residual_p95_deg    +0.329 | +0.362 | +0.136
  blockiness_ratio               +0.378 | -0.427 | -0.124
  quantization_proxy             +0.168 | -0.348 | -0.212
  unit_length_residual_p95       +0.168 | +0.012 | -0.035

AUC catastrofico (held-out, n_cat=12/22):
  nz_min                         0.617
  nz_p01                         0.600
  nz_p05                         0.592
  curl_mad                       0.483
  curl_p95_abs                   0.442
  projection_residual_median_deg 0.467
  projection_residual_p95_deg    0.392
  blockiness_ratio               0.492
  quantization_proxy             0.500
  unit_length_residual_p95       0.375
  ORACLE agree (solo contexto)   0.600
