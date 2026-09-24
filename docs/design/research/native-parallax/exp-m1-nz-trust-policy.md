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

### Addendum — llenado de la banda r_p01∈(1.8, 5.4)

64 corridas post-auditoría (mismas 4 superficies Stage A, `quant=none`, tags `A|surf|nz~t`
reproducibles; pares (nz_target, σ/255) = (0.025,2),(0.05,4),(0.025,1),(0.05,2),(0.01,0.5),
(0.025,0.5),(0.01,1),(0.005,0.5); los pares se eligieron por r estimado y el r real se
registró por fila). RAW + SOFT k=1 fijo — sin re-selección de candidatos. Nota de método:
incluye S09_bricks (ya consumida en held-out); al ser mapeo descriptivo con política fija,
no hay fuga de selección — se documenta por transparencia.

| r_p01 real | n combos | RAW: raw_rmse (med) | RAW: grad_rmse (range) | RAW max_grad | SOFT k=1: raw_rmse | SOFT grad | SOFT var_ratio | neg_frac |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ≈1.00 | 7 | 2.7 (0.7–200) | 133–3578 | hasta 1.8e7 | 0.10–0.21 | 8.1–16.6 | 0.911–0.915 | 0.05–0.07% |
| ≈1.77 | 8 | 0.07 (0.035–0.33) | 3.3–58.0 | hasta 3.0e4 | 0.010–0.021 | 1.49–2.99 | 0.969–0.977 | ≤0.01% |
| ≈3.96 | 4 | 0.058 | 5.76–5.81 | 246–299 | 0.025–0.030 | 5.06–5.11 | 0.998 | 0 |
| ≈5.4 | 8 | 0.008–0.016 | 0.88–1.76 | 35–113 | 0.004–0.010 | 0.82–1.64 | 0.999–1.001 | 0 |
| ≈12.5 | 4 | ~0.006 | ~0.82 | ~49 | ~0.0047 | ~0.81 | 1.000 | 0 |

Lecturas:

1. **La frontera de spikes RAW está en (1.8, 3.96), no en 5.4.** En r≈3.96 RAW ya es
   domesticado (grad ≈5.8, max_grad ≈300, sin negativos); en 5.4 es casi limpio. El
   "seguro desde 5.38" de Stage A era el borde *medido*, no la frontera.
2. **SOFT k=1 es no-destructivo y monótono en TODA la banda**: var_ratio 0.911 (r≈1) →
   0.973 (r≈1.8) → 0.998 (r≈4) → 1.000 (r≈5.4+); raw_rmse siempre < RAW. En r≈1 su
   max_grad toca exactamente la cota teórica 1/(2λ) (255 con σ=0.5/255; 127.5 con σ=1/255)
   — la contención está garantizada por construcción, no solo observada.
3. **En r≈1 el daño viene de nz≈σ>0, no de nz≤0**: neg_frac de solo 0.06% con grad_rmse
   ~10³. Refuerza el veredicto contra FLOOR_ZERO: ninguna política basada en nz≤0
   detectaría este régimen; `r_p01` sí.
4. **Refina la hipotética arquitectura §34** (ver Candidate policy): con SOFT como
   política, el corte REJECT baja de ~2 a ~1 (en r≈1.8 SOFT ya produce geometría usable
   con var 0.97), y el corte acept-with-regularización baja de ~6 a ~4.

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

### Tabla mínima de datos reales (filas held-out reproducidas)

Filas regeneradas con `execute_case` (256², seeds CRC32 deterministas) — **bit- idénticas**
a las de Stage C comprometidas arriba; se muestran con todas las columnas contractuales.
Casos individuales (no medianas): survivable S03/S09 nz~0.05 σ=2/255; duro S03 nz~0.005 σ=4/255.

| case | sigma | quant | policy | lam | nz_min | nz_p01 | negative_nz_fraction | activation_fraction | rmse | raw_centered_rmse | gradient_rmse | normal_angle_mean | seam_gradient | max_gradient | variance_ratio | hf_energy_ratio | runtime_ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C\|S03_sine_y\|nz0.05 | 0.00784 | Q8 | RAW | 0 | 0.0217 | 0.0414 | 0 | 0 | 0.00559 | 0.00951 | 0.953 | 2.04 | 1.68 | 46.1 | 1.039 | 6.7e+27 | 40.9 |
| C\|S03_sine_y\|nz0.05 | 0.00784 | Q8 | SOFT_TIKHONOV | 0.00784 | 0.0217 | 0.0414 | 0 | 0 | 0.00520 | 0.00539 | 0.878 | 1.96 | 1.56 | 40.8 | 1.007 | 6.2e+27 | 22.7 |
| C\|S03_sine_y\|nz0.05 | 0.00784 | Q8 | FLOOR_CLAMP | 0.0235 | 0.0217 | 0.0414 | 0 | 1.5e-05 | 0.00559 | 0.00951 | 0.953 | 2.04 | 1.68 | 42.5 | 1.039 | 6.7e+27 | 19.1 |
| C\|S09_bricks\|nz0.05 | 0.00784 | Q8 | RAW | 0 | 0.0249 | 0.0417 | 0 | 0 | 0.00526 | 0.00922 | 0.949 | 2.03 | 1.63 | 40.2 | 1.038 | 5.9e+27 | 18.9 |
| C\|S09_bricks\|nz0.05 | 0.00784 | Q8 | SOFT_TIKHONOV | 0.00784 | 0.0249 | 0.0417 | 0 | 0 | 0.00490 | 0.00507 | 0.874 | 1.96 | 1.52 | 36.6 | 1.006 | 5.5e+27 | 18.8 |
| C\|S09_bricks\|nz0.05 | 0.00784 | Q8 | FLOOR_CLAMP | 0.0235 | 0.0249 | 0.0417 | 0 | 0 | 0.00526 | 0.00922 | 0.949 | 2.03 | 1.63 | 40.2 | 1.038 | 5.9e+27 | 20.7 |
| C\|S03_sine_y\|nz0.005 | 0.0157 | Q8 | RAW | 0 | −0.0571 | −0.0300 | 0.3036 | 0 | 4.00 | 59.3 | 10905 | 86.9 | 5098 | 4.97e+06 | 218.9 | 2.2e+31 | 20.2 |
| C\|S03_sine_y\|nz0.005 | 0.0157 | Q8 | SOFT_TIKHONOV | 0.0157 | −0.0571 | −0.0300 | 0.3036 | 0.662 | 1.55 | 3.73 | 89.3 | 56.0 | 16.2 | 31.9 | 0.00546 | 1.7e+30 | 20.2 |
| C\|S03_sine_y\|nz0.005 | 0.0157 | Q8 | FLOOR_CLAMP | 0.0471 | −0.0571 | −0.0300 | 0.3036 | 0.978 | 0.485 | 3.38 | 80.2 | 6.28 | 3.28 | 21.3 | 0.0246 | 1.3e+26 | 20.2 |

Notas honestas de esta tabla: (1) `rmse` es el **aligned** de NP-M0; `raw_centered_rmse`
va siempre al lado (anti-trampa de alignment). (2) `hf_energy_ratio` es **no informativo
en la superficie sweep** (band-limited: E_hf(gt)≈0 → dividir por ~0 produce ratios ~1e27);
el companion robusto anti-aplanado aquí es `variance_ratio`/`gradient_energy_ratio`. Para
superficies con HF real (S09 etc. del corpus NP-M0) el ratio sí está definido. (3)
`runtime_ms` = decode+política+FFT completa del caso a 256²: el coste de la política es
ruido frente al solver (coherente con Stage E a 1024²). (4) En survivable, r_p01 =
0.0414/σ = **5.28** — justo al borde seguro medido (5.38); en duro r_p01 < 0 → REJECT
profundo: la tabla es auto-consistente con la frontera de Stage A.

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

## Pareto analysis

Ejes: x = error (raw_centered_rmse, grad_rmse), y = pérdida de detalle (desviación de
variance_ratio respecto a 1).

- **Survivable (held-out):** SOFT k=1 **domina a RAW** en ambos ejes (rmse 5.1e-3 vs
  9.3e-3; var 1.006 vs 1.038 — RAW tiene MÁS error Y MÁS exceso de varianza). El win de
  SOFT no es aplanando: reduce la energía espuria que el 1/nz amplifica.
- **Duro (held-out):** la frontera pasa por **CLAMP k=3** (en el caso S03 de la tabla
  §32: raw_centered 3.38 vs 3.73, var 0.0246 vs 0.0055 — conserva ~4.5× más varianza que
  SOFT a error similar; mejor max_gradient 21 vs 32 y seam 3.3 vs 16.2). SOFT k=1 queda
  detrás en contención aunque es más predecible en k.
- **Ninguna política domina los dos regímenes** → la conclusión Pareto es el par
  (SOFT k=1 default, CLAMP k∈[3,6] contención) más REJECT debajo de la frontera; un
  "winner" único sería forzado (§28).
- **Dispersión de best-k (§23):** SOFT k=1 estable (elegido en calibración, confirmado
  en held-out y en las filas reproducidas; sin re-tuning por superficie). CLAMP
  no-monótono con best-k entre 3 y 6 según superficie/régimen y sensibilidad a la
  resolución del corpus (a 128² el selector eligió k=0.5) → dispersión alta, se
  documenta como fragilidad y no se promueve valor único.

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
- **H6 SURVIVED, refinado** — riesgo pre-reconstrucción separa; tras el addendum la
  frontera de spikes RAW vive en (1.8, 3.96) y la de calidad SOFT en (1, 4); el gap
  quedó muestreado en r_p01 ≈ {1.0, 1.77, 3.96, 5.4, 12.5} (densidad finita pendiente).
- **H7 SURVIVED con advertencias** — un único k generalizó a 5 superficies held-out
  (SOFT k=1 y CLAMP k=3/k=6); advertencias: el selector es sensible a la resolución del
  corpus (a 128² eligió CLAMP k=0.5) y CLAMP es no-monótono en k.

## Adversarial review

Respuestas explícitas a las diez preguntas adversariales, con evidencia:

1. **¿La ganadora simplemente aplana H?** No: SOFT k=1 var_ratio 1.006–1.007 en held-out
   y filas reproducidas (tabla §32); el aplanado existe solo para k≥4 (H5) y quedó
   excluido por M2 y por el guard del selector.
2. **¿El alignment esconde bias?** No se reporta solo: `raw_centered_rmse`,
   `gradient_rmse` y `variance_ratio` acompañan siempre; SOFT mejora también sin
   alignment (raw_centered 0.0095→0.0054 en S03).
3. **¿Depende de conocer σ exactamente?** No exactamente: robusta a 0.5×–2× (rmse
   0.0051→0.0169, sin cliffs); a 4× degrada 14× pero sin explosión. CLAMP sí es frágil
   a 4× (aplasta, var 0.54).
4. **σ estimado 2× mal:** SOFT k=1 rmse 0.0169, var 0.921 — grácil, monótono.
5. **σ estimado 0.5× mal:** SOFT k=1 rmse 0.0081, var 1.03 — ≈RAW, inofensivo (sub-
   estimar deja la política casi apagada; no daña).
6. **¿negative nz se oculta?** Imposible por diseño: `negative_nz_fraction` es columna
   obligatoria en toda tabla; `abs(nz)` prohibido por test (M3); CLAMP pierde el signo
   del píxel pero lo contabiliza en `activation_fraction`.
7. **¿Un solo k funciona en held-out?** SOFT k=1: sí (mejora RAW en la mediana held-out
   y en cada fila reproducida). CLAMP k=3: es ≈RAW en survivable y su óptimo se mueve
   entre 3 y 6 → se reporta como familia, no como constante.
8. **¿nz/σ predice el error?** Sí: Spearman −0.926/−0.933 normalizado vs −0.84/−0.89
   crudo; casos con igual r colapsan a igual error (bins Stage A); la tabla §32 es
   auto-consistente (r_p01=5.28 en el borde seguro; r_p01<0 en REJECT profundo).
   Queda abierto p01 vs min (robustez vs correlación).
9. **¿REJECT más honesto que salvage?** Sí en duro: incluso la mejor política deja
   var_ratio ≤ 0.10 — la geometría no es recuperable; el cluster nz<0 coherente (Stage D)
   refuerza: REJECT/REVIEW, no rescate (§44).
10. **¿Alguna contradicción con NP-M0?** No: se REFINA. El `nz_floor=0.01` de NP-M0 era
    implícitamente k·σ (≈2.6σ con σ=1/255) — correcto en su corpus, no transferible como
    absoluto (M5/M7 lo bloquean). La inestabilidad nz≲σ se confirma y ahora tiene
    explicación (Jacobiano) y frontera medida. Advertencia nueva, no contradicción:
    Q8_XY_RECONSTRUCT_Z desplaza toda la frontera de riesgo.

## Candidate policy, if any

**CANDIDATE_POLICY (no production constant; validada solo en sintético):**

```
σ_eff  = ruido efectivo del canal de normal (conocido aquí; a estimar en el futuro)
r_p01  = percentil_01(nz) / σ_eff

r_p01 < 1            → REJECT (aun SOFT pierde ~9% de varianza y grad_rmse ≈8–17)
1 ≤ r_p01 < 4        → REVIEW + SOFT λ=1·σ para preview (var 0.91→0.998; detalle
                                  suavizado visible bajo r≈2; spikes RAW posibles <4)
r_p01 ≥ 4            → SOFT_TIKHONOV λ = 1·σ_eff  (default detail-óptimo; limpio
                                  desde r≈4: var ≥0.998)
                      FLOOR_CLAMP λ = (3..6)·σ_eff (alternativa si prima contención
                                  de max_gradient/seam en regímenes bajos)
negative_nz_fraction > ~1% (o cluster detectado) → REVIEW/REJECT adicional
                                  (con Q8_XY es detector directo: los nz=0 son exactos)
```

Cortes REFINADOS por el addendum (banda llena): antes del gap-fill los cortes tentativos
eran {2, 6} sobre la frontera de **RAW** (1.77/5.38); con SOFT como política real, la
frontera relevante es la de SOFT (REJECT <1, limpio ≥4). Sigue siendo CANDIDATE_POLICY:
un solo par (surf×σ) por punto de la banda y 4 superficies; sin normales reales no hay
constante final.

Sin constantes en código: `lam`/`sigma` son parámetros obligatorios (M5); estos números
viven SOLO en este documento como candidatos a validar con normales reales.

## Remaining uncertainty

1. **σ_eff de texturas reales** no existe todavía (aquí es conocido/inyectado); la
   política depende de estimarlo (robusta a 0.5–2×, frágil a 4×).
2. **Q8_XY/BC5**: el ruido de bloque no es gaussiano ni iid; la frontera medida con
   ruido gaussiano NO transfiere tal cual (Q8_XY ya desplazó todo).
3. La **banda r_p01∈(1.8, 5.4)** quedó muestreada en 5 puntos (addendum); la resolución
   finita (un par surf×σ por punto, 4 superficies) deja la frontera exacta de spikes
   RAW acotada a (1.8, 3.96) pero sin localizar dentro de ese intervalo.
4. Distribuciones nz de normales authored reales (¿cuántas texturas caen en REVIEW
   r_p01∈[1,4)?) — desconocido; determina el REVIEW_RATE real del producto.
5. Selector sensible a resolución del corpus (128 vs 256) para CLAMP; no investigado.
6. Clusters nz<0: sólo inyección sintética; detección de "cluster vs aislado" sin diseñar.

## Decision

**EXP_M1_GO** — hay política claramente más estable que RAW (SOFT k=1: mejora RAW −45%
en held-out sin perder detalle; ambas contienen el blast 10⁵× en el régimen duro), no
depende de constante absoluta (λ=k·σ; M5/M7 en tests), conserva detalle (var 1.006),
generalizó al held-out sintético, y sus límites están medidos y explícitos. El addendum
de llenado de banda REFUERZA el GO: SOFT k=1 resultó no-destructivo y monótono en toda
la banda antes sin muestrear (var 0.911→1.000 con r creciente, rmse siempre < RAW), su
contención coincide con la cota teórica 1/(2λ), y la frontera de spikes RAW quedó
acotada a (1.8, 3.96). Queda en `EXP_M1_GO_WAITING_REVIEW`. No se implementa nada
productivo.

## ONE next experiment

**EXP-M2 — frontera y σ_eff con normales authored reales (aún sin Skyrim):** tomar
normales authored de MatSynth CC0 (4K→recortes 512), aplicar Q8 y un proxy de error de
bloques 2-canal, medir la distribución real de r_p01/negative_nz_fraction y validar si
la frontera {2, 6} y la robustez de SOFT k=1 se sostienen fuera del corpus sintético.
Es el único puente entre esta política defendible y una política aplicable.
