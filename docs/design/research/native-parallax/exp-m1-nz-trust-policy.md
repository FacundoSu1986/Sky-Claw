# EXP-M1 — nz / trust policy

> Research-only. Sin DDS, sin BC5 real, sin Skyrim, sin MO2, sin PGPatcher, sin IA.
> Nada de esto es código productivo ni constante de producción.
> **Estado: `EXP_M1_GO` — esperando revisión independiente. NO merge.**

## Base

- **BASE_PR:** #617 (NP-M0), **head exacto `14b23b0`** al iniciar (verificado con
  `gh pr view`; sin cambios desde el reporte de NP-M0 — no hizo falta re-auditar).
- **MAIN_SHA:** `607ff21` (main avanzó durante el spike con PRs ajenos; el write-set es
  disjunto y este PR va **apilado sobre la rama de #617**, no sobre main).
- **BRANCH:** `research/native-parallax-exp-m1-nz-trust-policy` (apilada).
  *Desviación documentada:* la plataforma Arena fija la rama de sesión a
  `arena/01a0c5fd-sky-claw` (que es la rama de #617); commitear ahí habría modificado el
  PR #617, lo cual prohíbe el brief. Se crea rama apilada y PR con base = rama de #617.
- **PR (stacked):** ver final del documento.
- **Write-set:** `sky_claw/local/native_parallax/research/nz_policies.py` (NUEVO),
  `run_exp_m1.py` (NUEVO), `tests/test_native_parallax_exp_m1.py` (NUEVO),
  `docs/design/research/native-parallax/exp-m1-nz-trust-policy.md` (NUEVO), y un
  ajuste mecánico del selector en `run_exp_m1.py`. Los tests de NP-M0 quedaron intactos.

## Hypothesis

Responder con evidencia: **¿qué política aplicar cuando `nz` deja de ser confiable?**
P0 RAW / P1 REJECT / P2 HARD FLOOR (A clamp, B zero) / P3 SOFT Tikhonov. Hipótesis a
refutar (§27): H1 r=nz/σ predice mejor que nz absoluto; H2 RAW colapsa en r=O(1);
H3 clamp contiene pero sesga; H4 soft transiciona más suave que clamp; H5 λ grande
aplasta HF; H6 REJECT detectable pre-reconstrucción; H7 un único k relativo a σ
generaliza.

## Error propagation derivation

Para ``p(nx, nz) = -nx/nz``:

    ∂p/∂nx = -1/nz          ∂p/∂nz = +nx/nz²
    E[δp²] ≈ σ²·(nz² + nx²)/nz⁴   (ruido iid; nz²+nx² = 1-ny² ≤ 1)

El término del error **en nz** entra como ``σ/nz²`` y el del error en nx como
``σ/nz``: la cantidad adimensional que gobierna es ``r = nz/σ`` (con σ efectiva del
canal más ruidoso). De ahí la familia de políticas parametrizada por ``λ = k·σ`` y la
métrica de riesgo pre-reconstrucción ``r_p01 = nz_p01/σ``. La relación se **mide** en
este experimento (Spearman −0.93), no se asume.

P3 — regularización suave (derivación): ``g* = argmin_g (nz·g - 1)² + λ²g²`` da
``g*(nz) = nz/(nz²+λ²)``. Propiedades verificadas por tests: ``g*→1/nz`` si nz≫λ,
``|g*| ≤ 1/(2λ)`` (nunca diverge), **conserva el signo** de nz (una normal invertida
sigue produciendo gradiente invertido — no se disimula). Es preconditioning del
gradiente; NO se afirma equivalencia con weighted least squares.

## Policies

| Política | Definición | nz ≤ 0 |
|---|---|---|
| **P0 RAW** | ``p = -nx/nz`` con guard anti-UB (nz→1e-30 solo si \|nz\|<1e-30) | explota (baseline de daño) |
| **P1 REJECT** | no reconstruye: métricas de riesgo **pre-reconstrucción** (nz_min, nz_p01, frac(nz≤0), frac(nz<kσ)) | marcado, nunca silenciado |
| **P2-A FLOOR_CLAMP** | ``nz_eff = max(nz, λ=kσ)`` | clamp a λ — **pierde el signo** (contado en stats; sin `abs()` por diseño) |
| **P2-B FLOOR_ZERO** | nz≤0 → contribución cero; nz>0 crudo | rechazo por píxel |
| **P3 SOFT_TIKHONOV** | ``p = -nx·nz/(nz²+λ²)`` | signo preservado (atenuado) |
| **P4 MASK/TRUST** | **NO implementado** — P3 ya es una atenuación suave; trust·p duplicaría el mecanismo. El brief permite omitirlo si P3 cubre el objetivo (§16): cubierto. | — |

`lam` y `sigma` **no tienen default** en la API (invariante M5): ninguna constante
absoluta puede promoverse escondida. `NZ_FLOOR=0.01` no existe en ninguna parte del
código como política.

## Dataset

- **Calibration:** S02_sine_x, S06_multifreq, S08_ridges, S14_steep (256²).
- **Held-out (evaluación una sola vez):** S03_sine_y, S05_diagonal, S09_bricks,
  S10_asymmetric, S12_mixed_scale (256²).
- **nz sweep:** amplitud resuelta analíticamente para nz_min objetivo
  {0.5, 0.25, 0.1, 0.05, 0.025, 0.01, 0.005, 0.0025, 0.001}; valores reales
  registrados por fila (p.ej. objetivo 0.05 → nz_min real 0.0348–0.052 según seed/σ).
- **Negative controls heredados** de NP-M0 (N01/N02) siguen en la suite.
- **Superficie de barrido:** ``h = A·(sin(2π·6x) + 0.8·cos(2π·5y))``, |∇h|max = A·π√208.

## Noise model

**SYNTHETIC NORMAL NOISE** (no es BC5): gaussiano iid σ ∈ {0, 0.5, 1, 2, 4, 8}/255
sobre cada componente de la normal, renormalizada después. Seeds deterministas por
(caso, σ, quant) vía CRC32 (el `hash()` de Python es randomizado por proceso — corregido
en el spike). σ es CONOCIDO aquí porque lo inyectamos nosotros; su estimación desde una
textura real es trabajo futuro.

## Quantization model

- **Q8**: cuantización uniforme 8-bit de los TRES canales + renormalización (modelo
  NP-M0, upper bound de fidelidad).
- **Q8_XY_RECONSTRUCT_Z**: cuantiza (nx, ny) a uint8 y **reconstruye**
  ``nz = sqrt(max(0, 1 - nx²-ny²))`` sin renormalizar (contrato de normal 2-canal).
  NO es simulación de BC5 (no modela bloques); los píxeles con nx²+ny²>1 producen
  nz = 0 exacto. Resultado: **desplaza toda la frontera de riesgo** (ver abajo).

## Calibration/evaluation split

Selección de k y de frontera de riesgo hecha EXCLUSIVAMENTE con corpus de calibración;
el held-out se evaluó una vez con los candidatos pre-registrados. Dos iteraciones de
selector quedaron registradas a propósito (lección de método): la versión naive
(guardia de detalle sobre todo el corpus) cayó en fallback porque mezcla el régimen
irrecuperable con el sobrevivable; la refinada **dos-regímenes** usa guardia de detalle
(var_ratio ≥ 0.90) solo en filas nz~0.05 y contención (median(max_gradient) ≤ 1% de la
mediana RAW) solo en filas nz~0.005. Ambas versiones usan solo datos de calibración.

## Raw results

### Selector mecánico (calibración 256², dos regímenes)

```
FLOOR_CLAMP:    k=3   med_surv_rmse=0.00838  min_var=1.006
FLOOR_ZERO:     k=2   med_surv_rmse=0.00838  min_var=1.006   (idéntico a RAW: sin nz≤0)
SOFT_TIKHONOV:  k=1   med_surv_rmse=0.00476  min_var=0.969
```

### Stage A — frontera de riesgo RAW (144 corridas: 4 superficies × 9 nz × 4 σ)

| bin r_p01 = nz_p01/σ | n | grad_rmse med | grad_rmse max | max_grad med | casos con nz≤0 |
|---|---|---|---|---|---|
| 1–2 | 16 | 8.87 | 539.9 | 3361 | **16/16** |
| 2–4 | 0 | — (ningún caso cayó aquí) | — | — | — |
| 4–8 | 16 | 0.66 | 1.76 | 29.2 | 0/16 |
| 8–16 | 16 | 0.140 | 0.414 | 8.4 | 0/16 |
| 16–64 | 12 | 0.035 | 0.103 | 4.0 | 0/12 |

- **Spearman con grad_rmse**: ``r_p01/σ`` = **−0.926**; ``nz_min/σ`` = −0.933;
  ``nz_p01`` crudo = −0.887; ``nz_min`` crudo = −0.841. → La normalización por σ es lo
  que predice; entre p01 y min la diferencia es menor (min correlativa un poco mejor,
  p01 es robusta a un solo píxel — ver Adversarial review).
- **Frontera**: el peor caso catastrófico (grad_rmse>10) ocurre hasta r_p01 = **1.77**;
  el mejor caso con grad_rmse<1 arranca en r_p01 = **5.38**. El gap [1.8, 5.4] no
  recibió casos en este corpus: la transición es más afilada que O(1).

### Stage B — políticas en CALIBRATION (medianas; Q8; survivable = nz~0.05, duro = nz~0.005)

**Survivable (4 surf × 3 σ):**

| policy | k | raw_rmse | grad_rmse | var_ratio | max_grad | seam_g | activation |
|---|---|---|---|---|---|---|---|
| RAW | 0 | 0.00928 | 0.946 | 1.037 | 43.0 | 1.685 | 0 |
| FLOOR_CLAMP | 1 | 0.00928 | 0.946 | 1.037 | 43.0 | 1.685 | ~0 |
| FLOOR_CLAMP | 6 | **0.00652** | 0.807 | 1.015 | 21.2 | 0.795 | 0.035 |
| FLOOR_CLAMP | 8 | 0.0213 | 0.797 | 0.902 | 15.9 | 0.516 | 0.277 |
| SOFT_TIKHONOV | 0.5 | 0.00800 | 0.924 | 1.029 | 41.9 | 1.653 | 0 |
| SOFT_TIKHONOV | 1 | **0.00529** | 0.872 | 1.006 | 31.9 | 1.563 | ~0 |
| SOFT_TIKHONOV | 2 | 0.0170 | 0.839 | 0.920 | 25.4 | 1.078 | ~0 |
| SOFT_TIKHONOV | 4 | 0.0711 | 1.821 | 0.678 | 15.9 | 0.600 | 3.3e-4 |
| SOFT_TIKHONOV | 8 | 0.183 | 4.488 | 0.298 | 7.95 | 0.340 | 0.277 |
| FLOOR_ZERO | 2/4/8 | = RAW (activation 0: sin nz≤0 en este régimen) | | | | | |

**Duro (4 surf × 3 σ):**

| policy | k | raw_rmse | grad_rmse | var_ratio | max_grad | seam_g |
|---|---|---|---|---|---|---|
| RAW | 0 | 152.2 | 2.67e4 | 1450 | 1.27e7 | 8930 |
| FLOOR_CLAMP | 1 | 0.990 | 35.1 | 0.577 | 127.5 | 17.0 |
| FLOOR_CLAMP | 3 | 2.76 | 66.4 | 0.097 | 42.5 | 3.27 |
| SOFT_TIKHONOV | 1 | 3.13 | 78.4 | 0.052 | 63.8 | 29.7 |
| FLOOR_ZERO | 2/4/8 | **41.4** | 7312 | 114.7 | **2.8e6** | 4160 |

Lecturas: (1) **FLOOR_ZERO está dominado** — rechazar solo nz≤0 no contiene nada
porque la explosión viene de los nz≈σ>0; (2) clamp/soft **contienen el blast**
(max_grad 1.3e7 → 42–128, 10⁵×) pero **no rescatan geometría** en el régimen duro
(var_ratio 0.05–0.58, rmse ~1–3); (3) en el régimen sobrevivable, k pequeño **mejora a
RAW** (SOFT k=1: −43% rmse con var 1.006 — efecto denoise sobre el ruido amplificado
1/nz; λ≈σ se comporta cerca de un filtro de Wiener para este modelo de ruido);
(4) SOFT es monótono en k; CLAMP es **no monótono** (V: k=6 mejor que k=3 y que k=8).

### Stage C — HELD-OUT (5 superficies nunca usadas; candidatos oficiales del selector)

**Survivable (nz~0.05, Q8, 5 surf × 3 σ):**

| policy | k | raw_rmse | grad_rmse | var_ratio | max_grad | seam_g |
|---|---|---|---|---|---|---|
| RAW | 0 | 0.00930 | 0.945 | 1.038 | 40.4 | 1.584 |
| **SOFT_TIKHONOV** | **1** | **0.00507** | 0.869 | 1.006 | 31.9 | 1.471 |
| FLOOR_CLAMP | 3 | 0.00930 | 0.945 | 1.032 | 27.9 | 1.584 |
| FLOOR_ZERO | 2 | = RAW | | | | |

**Duro (nz~0.005, Q8):**

| policy | k | raw_rmse | grad_rmse | var_ratio | max_grad | seam_g |
|---|---|---|---|---|---|---|
| RAW | 0 | 70.9 | 1.25e4 | 312 | 4.97e6 | 6664 |
| SOFT_TIKHONOV | 1 | 3.13 | 78.4 | 0.052 | 63.8 | 28.8 |
| FLOOR_CLAMP | 3 | 2.76 | 66.4 | 0.097 | **42.5** | **3.27** |
| FLOOR_ZERO | 2 | 35.2 | 6081 | 82.4 | 2.2e6 | 2503 |

**Chequeo de vecindad (corrida previa con CLAMP k=6, también held-out):** raw_rmse
0.00644, var 1.014, max_grad 21.2, seam 0.780 — confirma el fondo del valle de CLAMP.

**Mis-estimación de σ (survivable held-out; λ = k·σ_est):**

| σ_est/σ_true | SOFT k=1 raw_rmse (var) | CLAMP k=3 raw_rmse (var) |
|---|---|---|
| 0.5× | 0.0081 (1.03) | 0.0094 (1.038) |
| 1.0× | 0.0051 (1.006) | 0.0093 (1.032) |
| 2.0× | 0.0169 (0.92) | **0.0065 (1.015)** ← cae en el fondo del valle |
| 4.0× | 0.0711 (0.68) | 0.109 (0.54) |

SOFT degrada **graceful** y monótono; sub-estimar σ es inofensivo (0.5× ≈ RAW).
CLAMP es **no monótono** (2× mejora porque k_eff=6) pero **frágil a 4×** (aplasta).

### Q8_XY_RECONSTRUCT_Z (§11; mini-corpus σ=2/255)

| case | neg_frac | raw_rmse | var_ratio | max_grad |
|---|---|---|---|---|
| nz~0.05 RAW | **0.069** | 12.2 | 935 | 7.2e5 |
| nz~0.05 CLAMP k=3 | 0.069 | 0.155 | 1.87 | 42.5 |
| nz~0.05 SOFT k=1 | 0.070 | 0.085 | 0.759 | 63.8 |
| nz~0.01 RAW | **0.231** | 55.2 | 760 | 4.1e6 |
| nz~0.005 RAW | 0.245 | 51.2 | 162 | 4.4e6 |

**El contrato 2-canal desplaza TODA la frontera**: el régimen que con ruido gaussiano +
Q8 era seguro (r_p01≈12) pasa a tener 7% de píxeles con nz≤0 y RAW catastrófico. La
cuantización XY reconstruida genera en masa nz=0 exactos → ``negative_nz_fraction`` se
vuelve un detector directo (sin necesitar σ). Advertencia registrada para EXP-003
(BC5 real será igual o peor por bloques).

## nz/sigma analysis

H1 se evalúa con las cuatro correlaciones de la sección Stage A: normalizar por σ sube
el |Spearman| de 0.887→0.926 (p01) y de 0.841→0.933 (min). El colapso entre casos
equivalentes se verificó en los bins: filas con (σ,nz) distintos pero mismo r_p01
comparten mediana de error (bin 4–8: 0.66 con n=16 de 4 superficies × 2 σ × 2 nz).
Conclusión: la política debe ser **relativa a σ**, no absoluta (refina el
"nz_floor=0.01" de NP-M0, que solo funcionó porque 0.01 ≈ 2.6σ con σ=1/255).

## Policy comparison (Pareto estabilidad vs detalle)

- **SOFT_TIKHONOV k=1**: detail-óptimo (held-out survivable: −45% rmse, var 1.006),
  monótono, robusto a mis-estimación de σ. Contención suficiente (max_grad acotado por
  1/(2λ) por construcción).
- **FLOOR_CLAMP k∈[3,6]**: contención-óptimo (mejor max_grad y seam en régimen duro:
  42.5/3.27 vs 63.8/28.8), pero no-monótono en k y frágil a σ_est 4×; en survivable
  k=3 es ≈RAW y k=6 es su óptimo.
- **FLOOR_ZERO**: dominado en ambos regímenes — descartado como candidato (queda como
  variante documentada y como refutación empírica de "rechazar nz≤0 basta").
- **RAW**: sólo como baseline; en survivable es razonable pero SOFT k=1 lo mejora
  gratis; en duro es catastrófico.
- **REJECT**: la única salida honesta en el régimen duro (ver Negative-nz y Worst).

## Negative-nz behavior (Stage D; nz~0.01, sin ruido, nz invertido inyectado)

| case | neg_frac | activation | rmse | raw_rmse | gradient_rmse | max_grad |
|---|---|---|---|---|---|---|
| cluster disco (3.14% px) RAW | 0.0314 | 0 | 0.656 | 0.662 | 15.6 | 94.3 |
| cluster disco CLAMP k=2 | 0.0314 | 0.0314 | 0.274 | 0.290 | 7.1 | 127.5 |
| cluster disco CLAMP k=6 | 0.0314 | 0.812 | 0.201 | 0.793 | 20.1 | 42.5 |
| cluster disco FLOOR_ZERO k=4 | 0.0314 | 0.0314 | 0.331 | 0.331 | 7.8 | 94.3 |
| cluster disco SOFT k=4 | 0.0314 | 0.552 | 0.681 | 1.208 | 29.2 | 31.9 |
| 10 px aislados RAW | 3.8e-4 | 0 | 0.00447 | 0.00450 | 0.82 | 94.3 |
| 10 px aislados CLAMP k=2 | 3.8e-4 | 3.8e-4 | 0.00221 | 0.00222 | 0.41 | 127.5 |

Respuestas a §17: (a) un píxel negativo aislado daña a nivel ruido (0.4% del rmse del
cluster) — NO invalida un asset por sí solo; (b) un **cluster coherente** de solo 3%
de los píxeles produce rmse 0.66 — daño real; (c) ninguna política rescata la geometría
de un cluster (mejor caso 0.20 sigue siendo relieve localmente equivocado; SOFT
conserva el gradiente invertido por diseño 0.68) → **cluster nz<0 = señal REJECT/
REVIEW, no salvage**. `abs(nz)` permanece prohibido (invariante M3/M5 en tests).

## Anti-flattening analysis

- SOFT k=1 held-out: var_ratio 1.006, hf_energy_ratio ≈ 1 (el detalle sobrevive; el
  "mejor RMSE" no es aplastamiento — verificado por las tres métricas §21).
- SOFT k≥4 sí aplasta (var 0.68→0.30 en survivable; en la superficie nz~0.01 de Stage D
  la atenuación global deforma incluso con 10 px malos: rmse 0.21) → k≤2 para SOFT.
- CLAMP no aplasta en survivable (var 1.01–1.04) pero **añade varianza** bajo Q8_XY
  (var 1.87: los spikes de 1/λ suman energía falsa) — otra razón para preferir SOFT
  como default y CLAMP solo como contención.
- El guard anti-aplanado está testeado (M2: λ=64σ → var_ratio < 0.5·k2 y el test lo
  exige visible).

## Worst cases

- **Held-out duro (nz~0.005, σ=4/255, Q8, RAW)**: raw_rmse 70.9–660, max_grad hasta
  4.9e6, seam_gradient 6664. Ni la mejor política produce geometría utilizable
  (var_ratio 0.05–0.10). Es el régimen REJECT.
- **Q8_XY nz~0.01**: 23% de píxeles con nz≤0 antes de cualquier política — el contrato
  2-canal destruye la información, no el solver.
- **Cluster nz<0 coherente (D)**: 3% de píxeles → rmse 0.66; irrecuperable por política.

## Hypotheses falsified

- Ninguna de H1–H7 fue falsada en su formulación central. Dos refinaciones fuertes:
  - **H1 (matiz)**: `nz_min/σ` correlaciona marginalmente mejor (−0.933) que `nz_p01/σ`
    (−0.926) en este corpus; la ventaja de p01 es de ROBUSTEZ (un solo píxel ruidoso
    mueve nz_min y no debería rechazar un asset), no de correlación. Queda abierto.
  - **Refinación de NP-M0**: el "nz_floor=0.01" de NP-M0 era implícitamente k·σ
    (≈2.6σ con σ=1/255); como constante absoluta no transfiere (M7 lo bloquea por
    diseño). Sin contradicción: NP-M0 queda subsumido.
- **P2-B (FLOOR_ZERO) queda refutada como candidato** por evidencia (dominada en ambos
  regímenes y catastrófica en duro).

## Hypotheses surviving

- **H1 SURVIVED** — la normalización por σ es el predictor (Δ Spearman ≈ +0.04–0.09
  sobre las versiones crudas); la elección p01-vs-min queda como refinación abierta.
- **H2 SURVIVED** — RAW colapsa en r_p01 ≲ 1.8 (16/16 con nz≤0; mediana grad_rmse 8.9).
- **H3 SURVIVED** — clamp contiene (10⁵× en max_grad) pero sesga (var 0.10 en duro).
- **H4 SURVIVED** — soft: sin picos por construcción (cota 1/(2λ)), transición monótona,
  σ-mis-est graceful; clamp frágil a 4×.
- **H5 SURVIVED** — λ≥4σ aplasta (var 0.68→0.30; D: over-attenuation medible).
- **H6 SURVIVED** — riesgo pre-reconstrucción separa: frontera medida 1.77 / 5.38.
- **H7 SURVIVED con advertencias** — un único k generalizó a 5 superficies held-out
  (SOFT k=1 y CLAMP k=3/k=6); advertencias: el selector es sensible a la resolución del
  corpus (a 128² eligió CLAMP k=0.5) y CLAMP es no-monótono en k.

## Candidate policy, if any

**CANDIDATE_POLICY (no production constant; validada solo en sintético):**

```
σ_eff  = ruido efectivo del canal de normal (conocido aquí; a estimar en el futuro)
r_p01  = percentil_01(nz) / σ_eff

r_p01 < 2            → REJECT (16/16 catastróficos en corpus; con políticas tampoco
                                  hay geometría: var_ratio ≤ 0.1)
2 ≤ r_p01 < 6        → REVIEW (banda de transición sin casos medidos; reconstrucción
                                  regularizada sólo para preview)
r_p01 ≥ 6            → SOFT_TIKHONOV λ = 1·σ_eff  (default detail-óptimo)
                        FLOOR_CLAMP λ = (3..6)·σ_eff (alternativa si prima contención
                                  de max_gradient/seam)
negative_nz_fraction > ~1% (o cluster detectado) → REVIEW/REJECT adicional
                                  (con Q8_XY es detector directo: los nz=0 son exactos)
```

Sin constantes en código: `lam`/`sigma` son parámetros obligatorios (M5); estos números
viven SOLO en este documento como candidatos a validar con normales reales.

## Remaining uncertainty

1. **σ_eff de texturas reales** no existe todavía (aquí es conocido/inyectado); la
   política depende de estimarlo (robusta a 0.5–2×, frágil a 4×).
2. **Q8_XY/BC5**: el ruido de bloque no es gaussiano ni iid; la frontera medida con
   ruido gaussiano NO transfiere tal cual (Q8_XY ya desplazó todo).
3. La **banda r_p01∈[1.8, 5.4]** no recibió casos: la frontera exacta está sin localizar.
4. Distribuciones nz de normales authored reales (¿cuántas texturas de mod caen bajo
   r_p01=6?) — desconocido; determina el REVIEW_RATE real del producto.
5. Selector sensible a resolución del corpus (128 vs 256) para CLAMP; no investigado.
6. Clusters nz<0: sólo inyección sintética; detección de "cluster vs aislado" sin diseñar.

## Decision

**EXP_M1_GO** — hay política claramente más estable que RAW (SOFT k=1: mejora RAW −45%
en held-out sin perder detalle; ambas contienen el blast 10⁵× en el régimen duro), no
depende de constante absoluta (λ=k·σ; M5/M7 en tests), conserva detalle (var 1.006),
generalizó al held-out sintético, y sus límites están medidos y explícitos. Queda en
`EXP_M1_GO_WAITING_REVIEW`. No se implementa nada productivo.

## ONE next experiment

**EXP-M2 — frontera y σ_eff con normales authored reales (aún sin Skyrim):** tomar
normales authored de MatSynth CC0 (4K→recortes 512), aplicar Q8 y un proxy de error de
bloques 2-canal, medir la distribución real de r_p01/negative_nz_fraction y validar si
la frontera {2, 6} y la robustez de SOFT k=1 se sostienen fuera del corpus sintético.
Es el único puente entre esta política defendible y una política aplicable.
