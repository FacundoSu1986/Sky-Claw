# EXP-M6 — Propuesta de derivación de `screen_lambda` (y estatus de `gamma`)

> **Estado:** `PROPOSAL` — insumo externo para la ETAPA A del prereg de EXP-M6
> ([PR #675](https://github.com/FacundoSu1986/Sky-Claw/pull/675), DRAFT). **No autoriza
> implementación ni ejecución de M6** (`M6_IMPLEMENTATION_BLOCKED = YES` vigente) y no
> forma parte del write-set de ningún slice hasta que el Tech Lead lo incorpore.
> **Origen:** revisión de código y matemáticas del 2026-10-15 (sesión Arena, rama
> `arena/1ce834fb-sky-claw`), con el experimento sintético ejecutado y registrado acá.
> **Sin corpus:** todo lo que sigue deriva de matemática pública + controles sintéticos
> propios. La calibración con corpus real o resultados históricos está PROHIBIDA (misma
> regla que §18.3 y R7).

## 0. Leyenda de estados (taxonomía del repo)

`VERIFIED` (ejecutado en este documento, reproducible del apéndice) · `EJECUTADO`
(medido, registrado con semilla) · `INFERRED` (deducido de fuentes públicas) ·
`HYPOTHESIS` (sin derivar; exige procedimiento antes de congelarse) · `INGENIERÍA`
(decisión de implementación, no umbral del modelo) · `NOT_DERIVED` · `REAL_RIG_REQUIRED`.

---

## 1. Objetivo y alcance

Cerrar —con el rigor de prereg que el repo exige— el valor por defecto de
`screen_lambda` (la regularización Tikhonov del integrador espectral) y el estatus del
ponderador de confianza `gamma`, **antes** de que EXP-M6 congele thresholds.

Fuera de alcance: thresholds de aceptación de M6 (`T_R`, `T_N`, `T_AMBIG`), la política
de nz (eje EXP-M1), la convención de altura final del slot `_p` (`REAL_RIG_REQUIRED`).

## 2. Teoría (fuentes públicas)

**Frankot–Chellappa screened como Tikhonov.** El solver espectral periódico que
minimiza `Σ‖∇h − g‖² + λ‖h‖²` en el toro es

```
ĥ(k) = −(i k·ĝ(k)) / (‖k‖² + λ)          [FC puro: λ = 0]
```

con dos consecuencias exactas:

1. **Atenuación de amplitud de altura por modo:** `k²/(k²+λ)`. En `k² = λ` la atenuación
   es 1/2: `λ` fija el modo de corte `k_c = √λ` (longitud ℓ = 1/√λ px). `VERIFIED`
   (medido: amplitud 0.9736 vs teórica 0.9747 en k=4·2π/128 con λ=1e-3).
2. **Amplificación de ruido de pendientes:** un error de pendiente blanco de PSD `σ²`
   aporta a la altura `σ²·k²/(k²+λ)²` por modo — el término que λ está llamado a
   suprimir. El error MSE total esperado es

```
E_RMSE²(λ) = (1/N⁴) · Σ_k [ (λ/(k²+λ))² · E|ĥ_true(k)|²  +  (k/(k²+λ))² · (E|P̂ε|² + E|Q̂ε|²) ]
```

   (convención FFT completa de NumPy; `E|P̂ε|² + E|Q̂ε|² = 2N²σ²` para ruido blanco de
   varianza `σ²` por componente). El λ* analítico es el minimizador numérico de esta
   curva dado un prior declarado `(PSD de señal, modelo de ruido)`. Lectura Wiener: λ
   es el punto de cruce donde el ruido iguala a la señal en la banda que el filtro
   atenúa. `INFERRED` (Tikhonov/Wiener estándar; FC 1988; Quéau survey — lista
   pública permitida por CLEAN_ROOM.md).

**Cuantización Q8 (artefacto real del pipeline).** Paso de cuantización por canal
`2/255` ⇒ error uniforme de semiancho `1/255` ⇒ RMS por componente `(2/255)/√12 ≈
0.00226` en unidades de normal; en pendientes, `σ_p ≈ 0.00226/nz`. `INFERRED`,
consistente con lo medido (§4: `σ_eff = 0.0023`).

## 3. Procedimiento cerrado de derivación (estilo R7)

Para cualquier prior declarado `(β, banda, σ)`:

1. Fijar el prior de señal (PSD de altura `∝ k^(−(β+2))`, banda `[2π/N, k_max]`,
   RMS de altura 1) y el modelo de ruido (blanco `σ`, Q8, o deriva LF declarada).
2. Construir la curva analítica `E_RMSE²(λ)` (§2) y tomar `λ*_analítico = argmin`.
3. Validar con controles sintéticos: misma semilla y rejilla que el prereg congelará;
   `λ*_empírico = argmin` del barrido; exigir coincidencia dentro de la resolución de
   la rejilla. Desacuerdo ⇒ el modelo declarado no describe el ruido ⇒ re-declarar.
4. **Congelar** λ (o λ=0) en el prereg ANTES de tocar corpus; prohibido retunear.
5. Registrar: prior, semilla, curvas, λ*, y el escenario en que λ pierde contra λ=0.

`CORPUS_CALIBRATION = PROHIBIDO` · `HISTORICAL_TUNING = PROHIBIDO` ·
`THRESHOLD_FIXED_HERE = NO` (el freeze es del prereg ETAPA B, no de esta propuesta).

## 4. Experimento propio [`EJECUTADO`, reproducible del apéndice]

Setup: `SEED=20261015` · `N ∈ {256, 128}` · `β ∈ {2, 4}` · banda `[2π/N, π/4]` sin
bins estrictos de Nyquist (el solver los anula por diseño) · altura RMS = 1 · ruido:
gaussiano blanco `σ ∈ {0.01, 0.03, 0.1}`, **Q8 real**, deriva LF suave (`k ≤ 16·2π/N`,
`σ ∈ {0.05, 0.5}`) · λ ∈ {0, 1e-5 … 1} (12 puntos) · solver rfft2 screened.

| Escenario | σ_eff | λ_opt empírico | λ=0 RMSE | Ganancia de λ |
|---|---|---|---|---|
| Blanco σ=0.01/0.03/0.1 (β=2,4; N=256,128) | 0.01–0.1 | **0** (11/12 celdas) | 0.008–0.09 | ≤ ×1.03 |
| **Q8** (β=2,4; N=256,128) | 0.0022–0.0023 | **0** (4/4 celdas) | 0.002–0.006 | ×1.00 |
| Limpio | 0 | **0** | ~0 (2.5e-16) | — |
| Deriva LF σ=0.05 | 0.05 | 0–1e-4 | 0.15–0.38 | ×1.0–1.2 |
| Deriva LF σ=0.5 (extremo) | 0.5 | 1.0 (borde de rejilla) | 1.5–3.5 | ×1.5–3.5, con RMSE ≈ 1.0 (SNR≈1: reconstrucción inservible) |

(El volcado completo celda a célula, incl. curva analítica vs empírica, queda en el
apéndice del runner; la curva analítica reproduce el argmin en todos los escenarios
blancos y subestima el λ* con deriva LF — esperable: el modelo blanco no describe esa
estadística, y el paso 3 del procedimiento exige re-declarar el modelo en ese caso.)

**Interpretación.** Con un prior band-limited realista y ruido blanco/Q8 —el artefacto
que el pipeline realmente produce— **λ=0 (FC puro periódico) es el óptimo MSE**: la
señal power-law concentra su energía en la banda baja, exactamente donde el screening
atenúa; el ruido blanco de pendientes apenas contribuye a altura en esa banda. El
screening solo "paga" con deriva LF extrema, y aun así la reconstrucción queda sin
señal útil. `EJECUTADO`.

## 5. Recomendación para el prereg de EXP-M6

1. **`screen_lambda = 0` como default del camino espectral** (`SUPPORTED_BY` este
   experimento + teoría §2). `λ=1e-3` del spike original no tiene fuente ni necesita
   existir: quédar con la forma functional correcta y λ=0.
2. **λ > 0 queda reservado a dos usos declarados:** (a) gauge del camino WLS-CG
   (`λ ≈ 1e-6`, `INGENIERÍA` — solo garantiza SPD para CG, no modela nada); (b) re-
   derivación por §3 si EXP-M6 declara un prior con deriva LF sistemática. Herramienta
   correcta para deriva LF probablemente NO es λ sino detrending explícito del campo
   de pendientes — eje abierto para el diseño de #675 (`INFERRED`, a falsar).
3. **`gamma` (ponderación nz):** sigue `HYPOTHESIS`/`NOT_DERIVED` y es eje de EXP-M1.
   Lo único cerrado acá es la FORMA: los pesos entran en las ecuaciones normales
   (`min Σ w‖∇h−g‖²`), nunca multiplicando los datos (la variante multiplicativa sesga
   ×3.93 en campos integrables escarpados — medido en la revisión del 2026-10-15).
   Default sin ponderar hasta que EXP-M1 derive lo contrario.
4. **Nyquist estricto:** en malla par el símbolo `i·k` está mal planteado en los bins
   de Nyquist; anularlos es una decisión de diseño que debe viajar en el manifest con
   su energía (`nyquist_energy_fraction`), no un silencio.

## 6. Estados

| Objeto | Estado |
|---|---|
| Filtro `k²/(k²+λ)` y su modo de corte | `VERIFIED` (medido) |
| Fórmula `E_RMSE²(λ)` y λ* analítico | `INFERRED` + `EJECUTADO` (coincide en escenarios blancos) |
| λ=0 default espectral bajo prior declarado | `SUPPORTED_BY` §4 (cambiar prior ⇒ re-derivar por §3) |
| λ gauge WLS-CG ≈ 1e-6 | `INGENIERÍA` |
| Pesos en ecuaciones normales (no multiplicativos) | `VERIFIED` (contraejemplo multiplicativo medido ×3.93) |
| Valor de `gamma` | `NOT_DERIVED` (eje EXP-M1) |
| Semántica final de altura `_p` | `REAL_RIG_REQUIRED` |
| Freeze del valor en prereg | `PENDIENTE ETAPA B de #675` — esta propuesta no lo hace |

## Apéndice — Runner exacto del experimento

`SEED=20261015`. Script autocontenido (NumPy puro; ~1 s):

```python
# Versión completa ejecutada: parallax_review/lambda_study/run_lambda_study.py
# (sesión Arena 2026-10-15). Núcleo:
def make_signal(n, beta, rng):
    # ĥ ∝ k^{-(beta+2)/2}·(gauss + i·gauss), banda [2π/N, π/4] sin Nyquist estricto,
    # altura RMS = 1; pendientes por derivada espectral exacta de la misma realización.
def solve_screened(p, q, lam):
    # rfft2; ĥ = −(i kx p̂ + i ky q̂)/(k²+λ); DC y Nyquist estricto anulados.
def analytic_rmse_curve(n, beta, sigma): ...
# barrido λ ∈ {0,1e-5,3e-5,1e-4,3e-4,1e-3,3e-3,1e-2,3e-2,1e-1,3e-1,1}
# ruido: gaussiano blanco {0.01,0.03,0.1}; Q8 real (rint((n/2+.5)·255)); deriva LF
# (k ≤ 16·2π/N) σ ∈ {0.05, 0.5}; "limpio" σ=0 como control.
```

Resultados crudos completos: volcado `results.txt` de la sesión de origen (incluido en
la conversación del 2026-10-15; reproducción local: correr el script con la semilla
dada y comparar celda a célula).
