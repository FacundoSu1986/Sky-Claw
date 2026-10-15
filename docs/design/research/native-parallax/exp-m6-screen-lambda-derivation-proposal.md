# EXP-M6 — Propuesta de derivación de `screen_lambda` (y estatus de `gamma`) — REV 4

> **Estado:** `PROPOSAL` — insumo externo para la ETAPA A del prereg de EXP-M6
> ([PR #675](https://github.com/FacundoSu1986/Sky-Claw/pull/675), DRAFT). **No autoriza
> implementación ni ejecución de M6** (`M6_IMPLEMENTATION_BLOCKED = YES` vigente).
>
> **REV 4 (2026-10-15):** corrige la REV 3 (commit `db5e5bd7`) tras la cuarta revisión
> externa. Tres hallazgos P1 de la revisión, **los tres confirmados numéricamente por
> esta parte y aceptados** (adjudicación §8.2):
>
> 1. **P1-1 — La curva analítica de REV 3 no correspondía a su propio solver.** Usaba
>    una fórmula de dispersión distinta a los símbolos `d` del solver y pesos de
>    Parseval incorrectos. Curva corregida (operadores `D=|dx|²+|dy|²` del solver +
>    pesos rfft2 de columnas frontera, §2.3) en λ=0 régimen B: **0.006794 / 0.007955 /
>    0.009087 / 0.010205** (N=32/64/128/256, σ=0.1) — coincide con la revisión al
>    dígito y con MC propio (ratios MC/curva 1.009/0.995/0.980). Los valores de la
>    REV 3 (0.0721/0.0795/0.0862/0.0924) quedan **retirados**.
> 2. **P1-2 — Enunciado de divergencia corregido.** λ=0: el MSE de ruido crece
>    ≈(σ²/2π)·ln N sin cota. λ>0 fijo: NO diverge y acota a la asíntota
>    **(σ²/2π)·ln(π/√λ)** (verificado: λ=1e-3 → 0.006849 @1024² vs asíntota 0.007319).
>    Separación formal término-de-ruido vs término-de-sesgo (§4.1); λ\*_riesgo > 0
>    pero con ley de escalado **λ\* = c/N²** y ganancia ≤0.05 % (§4.2). El valor
>    REV-3 λ\*=3.15e-10 queda **retirado** (artefacto de normalización, §4.2).
> 3. **P1-3 — La métrica de energía nula de PENDIENTES no mide altura irrecuperable.**
>    Contraejemplo h=(−1)^(x+y): pendientes ≡ 0 (fracción nula = 0.0) con RMS de
>    altura 1.0 — toda la altura se pierde sin que la métrica la vea. El manifest debe
>    separar `null_slope_energy_fraction` (medible) de `irrecoverable_height_energy`
>    (**UNKNOWN** sin oráculo de altura) (§5.6).
>
> **Historial:** REV 1 (`8a95f84a`) — fórmula de ruido `2N²σ²` (incorrecta), "Q8 real"
> (mal etiquetado), runner incompleto. REV 2 (`63e75a0e`) — fórmula corregida
> `k²N²σ²` (proyección ortogonal), λ\* continuo, gauge relativo, runner completo.
> REV 3 (`db5e5bd7`) — defecto Nyquist real del solver confirmado por contraejemplo
> (RMSE 0.707107 → 4.8e-16), spec de operadores §2.2, regímenes A/B, harness H4 con
> modo contrato obligatorio. REV 4 — esta.
>
> **Sin corpus:** todo deriva de matemática pública + controles sintéticos propios.
> Calibrar con corpus real o históricos sigue PROHIBIDO (§18.3, R7).

## 0. Leyenda de estados

`VERIFIED` (ejecutado aquí, reproducible del Apéndice A) · `EJECUTADO` (medido, semilla
registrada) · `INFERRED` (deducido de fuentes públicas) · `HYPOTHESIS` · `INGENIERÍA` ·
`NOT_DERIVED` · `REAL_RIG_REQUIRED` · `RETIRADO` (publicado antes, refutado después).

## 1. Objetivo y alcance

Sin cambios desde REV 1: cerrar —con rigor de prereg— el default de `screen_lambda` y
el estatus del ponderador `gamma`, **antes** de que EXP-M6 congele thresholds. Fuera de
alcance: `T_R`/`T_N`/`T_AMBIG`, política de nz (EXP-M1), semántica final del slot `_p`
(`REAL_RIG_REQUIRED`).

## 2. Teoría (fuentes públicas)

### 2.1 Screened Frankot–Chellappa como Tikhonov

Minimizar `Σ‖∇h − g‖² + λ‖h‖²` en el toro con derivadas espectrales da
`ĥ = (dx·p̂ + dy·q̂)* / (|dx|²+|dy|²+λ)` (solución LS por modo), con dos consecuencias
exactas: atenuación de amplitud `k²/(k²+λ)` (modo de corte `k_c=√λ`, `VERIFIED`) y el
término de ruido por **proyección ortogonal** `E|num(k)|² = D(k)·N²σ²` con
`D(k)=|dx|²+|dy|²` (la REV 1 sobrecontaba ×2; corregido en REV 2).

### 2.2 Spec de operadores: qué es observable en Nyquist (REV 3)

**Definición (obligatoria antes de cualquier implementación).** En malla par, la
derivada de un modo Nyquist **no es observable en la malla**: el contenido on-grid del
bin `k=N/2` mezcla las fases cos/sin y ningún operador consistente puede separarlas.
La convención estándar de métodos espectrales es `d = 0` en el eje Nyquist de cada eje:

```
dx(kx,ky) = i·kx   si kx ≠ N/2 ;   0  si kx = N/2
dy(kx,ky) = i·ky   si ky ≠ N/2 ;   0  si ky = N/2
ĥ(k) = (conj(dx)·p̂ + conj(dy)·q̂) / (|dx|²+|dy|²+λ)     [bins con denom=0: ĥ=0]
```

**Espacio nulo REAL (declarándolo, no silenciándolo).** Sólo son no observables los
bins donde **ambos** ejes carecen de derivada. Un bin con Nyquist en X pero `ky`
genérico **conserva** la altura: se recupera por la derivada en Y.

**El defecto de la REV 2 (confirmado por contraejemplo, REV 3).** La máscara por bins
completos eliminaba la columna/fila ENTERA: para `h(x,y) = (-1)^x·sin(6πy/64)`
(64×64; Nyquist en X, modo 3 en Y) tiraba el bin `(N/2, 3)` cuya altura es
perfectamente recuperable vía `q̂`. Medición (ambas partes independientes):

| Solver | RMS salida | RMSE vs h |
|---|---|---|
| REV 2 (máscara de bins) | 0.000000 | **0.707107** (= RMS(h): salida plana) |
| Símbolos coherentes (§2.2) | 0.707107 | **4.8e-16** |

Regresiones: campo integrable sin Nyquist exacto (3.0e-16); tablero declarado nulo
(std=0); costo idéntico. `VERIFIED`.

### 2.3 Pesos de Parseval en la grilla rfft2 (NUEVO en REV 4; corrige la curva de REV 3)

La identidad `Σ_full |F(k)|² = N²·Σ|f|²` (DFT sin normalizar) se evalúa sobre la
salida `rfft2` (shape `(N, N/2+1)`) con pesos `w`:

```
w(ky,kx) = 1  si kx ∈ {0, N/2}   (columnas frontera, COMPLETAMENTE materializadas)
w(ky,kx) = 2  en otro caso        (columnas interiores: bin propio + espejo N−kx)
```

La justificación es estructural: el conjugado de `(ky, kx=N/2)` es `(−ky, N/2)` —
**misma columna**, también almacenada; ídem `kx=0`. Esos bins representan un único bin
de la grilla completa (peso 1). Sólo las columnas interiores `1..N/2−1` representan
dos bins full-grid (el propio y el espejo `N−kx`, no almacenado): peso 2. Verificación
brute force exacta a máquina: `Σ|fft2(h)|² = Σ w·|rfft2(h)|²` para h aleatoria,
N∈{4,8,16,64}. `VERIFIED`.

**Registro de transparencia del defecto (tres iteraciones, todas de esta parte):**
(a) REV 3 publicó una curva con evaluación espectral implícita sin pesos correctos
(≈ fila-0 + columna-final: sobrecontaba ~5 %); (b) la primera corrección interna de la
REV 4 usó "4 esquinas auto-conjugadas" dejando peso 2 en la columna Nyquist: el
sobrecuento de energía de ruido era ~20 % (MC/curva = 0.83); (c) la regla por
columnas frontera reproduce fft2 exacto y deja MC/curva en 0.98–1.01 (§4.1). Los
valores de la revisión externa sólo son reproducibles con los pesos correctos — la
discrepancia de ~20 % de esta parte en la primera verificación fue un artefacto propio,
no de la revisión.

## 3. Procedimiento cerrado de derivación (estilo R7)

Sin cambios desde REV 1: prior declarado → curva `E(λ)` → λ\* → controles sintéticos
(misma semilla que congelará el prereg) → freeze en ETAPA B → prohibido retunear.
Desacuerdo analítico/empírico ⇒ re-declarar el modelo. `CORPUS_CALIBRATION=PROHIBIDO`
· `THRESHOLD_FIXED_HERE=NO`.

## 4. Experimento propio [`EJECUTADO`; runner Apéndice A, salida Apéndice B]

Setup: `SEED=20261015` · `N∈{256,128}` · `β∈{2,4}` · banda `[2π/N, π/4]` sin Nyquist
estricto · RMS(altura)=1 · ruido: blanco `σ∈{0.01,0.03,0.1}`, Q8 sintético, deriva LF
(`k≤16·2π/N`, `σ∈{0.05,0.5}`) · λ∈{0…1} (12 puntos) · solver de símbolos `d` (§2.2).
**Curva analítica REV 4:** construida con los MISMOS operadores `D(k)=|dx|²+|dy|²` del
solver y los pesos §2.3 (éste era el P1-1).

`E[MSE(λ)] = (1/N⁴)·Σ w·(λ/(D+λ))²·E|ĥ|²  +  (1/N²)·Σ w·(D/(D+λ)²)·σ²`
(term. de **sesgo** de señal con factor `(λ/(D+λ))²` — NO la transmisión `(D/(D+λ))²`
—, más term. de **ruido** por proyección ortogonal).

### 4.1 Regímenes de ruido y divergencia (corrección P1-2 — aceptada)

- **Régimen A — "ruido filtrado en banda"** (el modelo declarado): curva y MC con el
  ruido restringido a la banda. Fórmula vs MC(200) en N=128: ratio **0.980**
  (medición 2026-10-15; REV 3 medía 1.0055 con su curva defectuosa).
- **Régimen B — "ruido sin filtrar"** (lo que el solver recibe con ruido blanco real):
  `E[MSE(0)] = (σ²/N²)·Σ_{D>0} w/D ≈ (σ²/2π)·ln N + O(1)` — divergencia logarítmica
  de FC. MC multi-resolución de espectro completo (100 realizaciones, N=32/64/128,
  incluye columnas mixtas de Nyquist): ratios MC/curva **1.0087 / 0.9952 / 0.9796**
  (`--check` del Apéndice A). `VERIFIED`.

**Enunciado correcto de la divergencia (reemplaza al de REV 3):**

| Caso | MSE de ruido | Comportamiento |
|---|---|---|
| λ=0 | `(σ²/2π)·ln N + O(1)` | crece **sin cota** con N (medido: 0.010205 @256² → 0.012422 @1024²) |
| λ>0 fijo | → **(σ²/2π)·ln(π/√λ)** | **acotado**, no diverge; tampoco decae con N |

Verificación de la asíntota: λ=1e-3, σ=0.1 → asíntota 0.007319; medido 0.006849
@1024² (−6.4 %, aproximación desde abajo por el corte finito `k≥2π/N`); `--check`
reproduce ambos. Consecuencia operativa sin cambios: con ruido blanco sin filtrar
ningún λ constante arregla el problema — filtrar las pendientes de entrada sigue
siendo la herramienta correcta (`INFERRED`, a falsar en #675).

**Separación noise-reconstruction vs λ-bias (pedida por la revisión):** el término
`(λ/(D+λ))²·E|ĥ|²` es PÉRDIDA DE SEÑAL (mono. creciente en λ); el término
`(D/(D+λ)²)·σ²` es RUIDO RESIDUAL (mono. decreciente en λ). Son mecanismos distintos,
se reportan por separado en el runner (la curva acepta `include_signal` para exponer
sólo el de ruido — así se validó contra MC de ruido puro).

### 4.2 Métricas separadas: riesgo esperado vs realización (λ\* recalculado)

- **Riesgo esperado (curva corregida).** Argmin continuo (20 000 puntos):
  (N=128, β=2, σ=0.1): **λ\*_A = 5.17e-06** (rég. A) / **8.89e-06** (rég. B), con
  ganancia relativa frente a λ=0 de **−0.032 %** / **−0.053 %**. Ley de escalado
  medida: **λ\* = c/N² exacto** — c_A ≈ 0.0846–0.0849, c_B ≈ 0.1457–0.1459 (px²) para
  N∈{128,256,512,1024} (una-línea de reproducción en Apéndice B). Interpretación: el
  trade-off vive en el modo más bajo de la banda (`k_min=2π/N`, `D_min∝N⁻²`), no en la
  cola de ruido.
- **`RETIRADO`: λ\* = 3.15e-10 (REV 3, y su verificación externa).** Artefacto de la
  normalización del término de sesgo en la curva v3 (dividido por N² en vez de N⁴:
  curvatura de sesgo inflada ×N² ⇒ λ\* desplazado ~5 órdenes). La coincidencia entre
  v3 y la revisión se explica porque ambos evaluaban la misma fórmula commiteada; la
  propia revisión (ronda 4, P1-1) estableció después que esa curva no correspondía ni
  al solver ni al MC.
- **Empírico** (argmin de 12 puntos sobre UNA realización): λ_opt=**0 en 20/28**
  celdas; las 8 restantes: 4× deriva LF σ=0.5 (λ=1, borde de rejilla, ×1.63–3.26) y
  4× λ∈{3e-05,1e-4} en deriva LF σ=0.05 y gauss σ=0.1 en N=128 (×1.003–1.014) — ruido
  de muestra alrededor de una diferencia esperada despreciable, no evidencia de
  optimalidad.

Enunciado defendible (REV 4): **λ=0 es el baseline correcto bajo este prior — el
óptimo continuo existe (λ\*=c/N²>0) pero le gana a λ=0 menos del 0.06 % — y `λ=1e-3`
sigue sin ninguna justificación** (su sesgo en banda baja cuesta más que todo el
ruido que suprime). `EJECUTADO`.

### 4.3 Tabla (28 celdas; volcado completo Apéndice B)

| Escenario | λ_opt empírico | Ganancia | λ\*riesgo continuo (A / B) | Nota |
|---|---|---|---|---|
| Blanco σ=0.01–0.1 (12) | **0** (9/12; 3×3e-05 en N=128 σ=0.1) | ≤ ×1.007 | 1.0–1.3e-06 / 1.8–2.2e-06 (N=256) · 4.1–5.2e-06 / 7.1–8.9e-06 (N=128) | rég. B aplica al experimento |
| Q8 sintético (4) | **0** (4/4) | ×1.000 | ídem | σ_eff≈0.0023 |
| Limpio (4) | **0** | — | ídem | ~2.5e-16 |
| Deriva LF σ=0.05 (4) | 0–1e-4 | ≤ ×1.014 | ídem | |
| Deriva LF σ=0.5 (4) | 1 (borde de rejilla) | ×1.63–3.26 | ídem | RMSE(λ\*)≈1.0: SNR≈1, sin señal útil |

## 5. Recomendación para el prereg de EXP-M6 (REV 4)

1. **Solver spec:** símbolos `dx, dy` con anulación SÓLO en el eje Nyquist de cada
   eje (§2.2); denominador compuesto de los MISMOS símbolos; espacio nulo declarado y
   medido. Prohibida la máscara por bins de la REV 2.
2. **Pesos de Parseval** (§2.3) obligatorios en cualquier evaluación espectral del
   MSE (curvas, recibos, manifest): columnas frontera `kx∈{0,N/2}` peso 1,
   interiores peso 2.
3. **`screen_lambda = 0`** como default del camino espectral (baseline, §4.2). λ>0
   reservado a: (a) gauge del WLS (§5.3), (b) re-derivación si el prior cambia por §3.
4. **Gauge WLS:** el absoluto `1e-6` atenúa 29.8 % el fundamental a 4096 (confirmado);
   el relativo `1e-4·k_min²` es **provisional** (`INGENIERÍA`). El diseño correcto a
   especificar en #675: **CG proyectado al subespacio de media cero** (restricción
   explícita Σh=0, sin regularización de gauge), más detección de regiones
   desconectadas. `NOT_DERIVED` hasta su spec numérica.
5. **`gamma`:** forma cerrada (pesos en ecuaciones normales, nunca multiplicativos —
   contraejemplo ×3.93); valor `NOT_DERIVED` (EXP-M1); default sin ponderar.
6. **Métricas de manifest separadas (NUEVO — P1-3):** reportar
   (i) `null_slope_energy_fraction`: fracción de la ENERGÍA DE PENDIENTES en el
   espacio nulo — **medible**, y (ii) `irrecoverable_height_energy`: energía de
   ALTURA irrecuperable — **`UNKNOWN` sin oráculo de altura**. Contraejemplo que
   obliga a la separación: `h=(−1)^(x+y)` tiene RMS 1.0, pendientes ≡ 0 y fracción
   pendiente-nula **0.0** — la métrica de pendientes es ciega a la altura perdida.
   **Prohibido** presentar (i) como proxy de (ii).

## 6. Estado del harness H4 (insumo #723) y del gauge — decisiones REV 3 (sin cambios en REV 4)

### 6.1 Modo contrato obligatorio

El harness (`tests/test_native_parallax_h4_contract_adversarial.py`) tiene dos modos,
seleccionados por entorno:

| Situación | Sin `SKYCLAW_REQUIRE_H4_CONTRACT` | Con `=1` |
|---|---|---|
| main sin API H4 | SKIP explícito | **FAIL** |
| PR #723 API correcta | ejecuta (47 passed) | ejecuta (47 passed) |
| PR #723 import roto | **FAIL/ERROR** | **FAIL/ERROR** |
| PR #723 API incompleta | SKIP con razón | **FAIL** |

Verificado en los tres árboles (sesión 2026-10-15): main sin env → `1 passed, 46
skipped`; main con env → `46 errors`; #723 con env → `47 passed`. La CI de #723 debe
exportar la variable (documento en el propio harness).

### 6.2 Gauge WLS

`1e-4·k_min²` queda como alternativa provisional de ingeniería con su justificación
numérica (atenuación uniforme 1/(1+1e-4) en N∈{128…4096}); el reemplazo proyectado-CG
(§5.4) es el diseño requerido antes de cualquier congelamiento M6.

## 7. Estados (REV 4)

| Objeto | Estado |
|---|---|
| Operadores `dx,dy` con Nyquist por eje + LS coherente | `VERIFIED` (contraejemplo 4.8e-16; regresiones OK) |
| Máscara por bins de REV 2 | `RETIRADO` (defecto confirmado) |
| Pesos de Parseval columnas-frontera (§2.3) | `VERIFIED` (fft2 exacto N∈{4,8,16,64}; MC/curva 0.98–1.01) |
| Curva analítica con `D` del solver (§4) | `VERIFIED` (P1-1; ratios MC 1.009/0.995/0.980; coincidencia con revisión al dígito) |
| Curva analítica REV 3 y sus valores λ=0 | `RETIRADO` (P1-1: fórmula de dispersión y pesos incorrectos) |
| Fórmula de ruido `D·N²σ²` (proyección) | `VERIFIED` (MC banda 0.980; espectro completo 0.98–1.01) |
| Divergencia λ=0 ~ln N; λ>0 → (σ²/2π)ln(π/√λ) | `VERIFIED` (P1-2; `--check` #4) |
| λ\*riesgo = c/N² (c_A≈0.085, c_B≈0.146), ganancia ≤0.05 % | `EJECUTADO` (REV 4; sustituye `RETIRADO` 3.15e-10) |
| λ=0 default (baseline) | `SUPPORTED_BY` §4 |
| `null_slope_energy_fraction` ≠ altura irrecuperable (P1-3) | `VERIFIED` (contraejemplo (−1)^(x+y)); altura: `UNKNOWN` sin oráculo |
| Gauge WLS relativo | `INGENIERÍA` provisional; proyectado-CG `NOT_DERIVED` |
| Pesos en ec. normales | `VERIFIED` (×3.93 contraejemplo multiplicativo) |
| `gamma` | `NOT_DERIVED` (EXP-M1) |
| Semántica `_p` | `REAL_RIG_REQUIRED` |
| Freeze | `PENDIENTE ETAPA B de #675` |

## 8. Adjudicaciones de las revisiones externas (2026-10-15)

### 8.1 Ronda 3 — revisión de la REV 2 (adjudicada en REV 3; sin cambios)

| Afirmación | Veredicto | Evidencia / acción |
|---|---|---|
| **Nyquist: la máscara elimina frecuencias reconstruibles** (contraejemplo `(-1)^x·sin(6πy/64)`) | **CORRECTA — aceptada** | Reproducido: RMSE 0.707107 con el solver commiteado; símbolos coherentes → **4.804e-16** (la revisión midió 4.8e-16). Spec §2.2 + autotest |
| "Definir símbolos diferenciales y pseudoinversa coherentes, anulando sólo lo no observable" | **CORRECTA — adoptada como spec** | §2.2; espacio nulo = ambos ejes enfermos |
| MC(400) y barrido de 20k sin código en el commit; `lambda_star_continuous()` no invocada | **CORRECTA — aceptada** | `--check` del Apéndice A ejecuta MC, barrido continuo (invocado en `main()`), contraejemplo y asserts |
| Comparación analítica(filtrado) vs empírico(sin filtrar) necesita separación formal | **CORRECTA — aceptada** | §4.1: regímenes A/B |
| λ\*≈3.15e-10; no confundir mejora esperada con empírica | **ACEPTADA EN SU MOMENTO; `RETIRADA` en REV 4** | El valor era artefacto de la curva v3 (ver §8.2 P1-1 y §4.2); la separación de métricas sí se mantuvo (§4.2) |
| Modo de contrato obligatorio por env var para H4 | **CORRECTA — implementada** | §6.1: `SKYCLAW_REQUIRE_H4_CONTRACT=1`; tres estados verificados |
| Gauge relativo necesita revisión; WLS con restricción de media cero | **CORRECTA — aceptada como diseño** | §5.4/§6.2: proyectado-CG `NOT_DERIVED` |
| "No reconstruir el módulo v2 todavía" | **CONCURRENTE** | No se reconstruyó |
| 47 tests / MC "declarados, no verificados independientemente" | **CORRECTA como límite** | Comando público de reproducción §6.1; autotest en el commit |

### 8.2 Ronda 4 — revisión de la REV 3 (commit `db5e5bd7`; esta adjudicación)

| Afirmación (P1) | Veredicto | Verificación independiente de esta parte / acción |
|---|---|---|
| **P1-1: la curva analítica de REV 3 no corresponde al solver** — fórmula de dispersión distinta a los símbolos `d` commiteados y pesos de Parseval incorrectos. Curva correcta esperada (λ=0, rég. B, σ=0.1): 0.006794/0.007955/0.009087/0.010205 (N=32/64/128/256); sus MC: 0.007948@64, 0.008977@128. Los valores commiteados (0.0721/…/0.0924) eran inconsistentes. | **CORRECTA — aceptada** | Reproducido al dígito: curva v4 con `D` del solver + pesos §2.3 = **0.006794/0.007955/0.009087/0.010205**; MC propio (400 real.) @64² = 0.007972. Los pesos correctos son columnas-frontera, NO "4 esquinas" (primera corrección interna de esta parte: MC/curva=0.83 — error propio, ver §2.3). Apéndice A v4; valores REV 3 `RETIRADOS` |
| **P1-2: el enunciado de divergencia era incorrecto tal como estaba** — λ=0 crece ~ln N; λ>0 fijo NO "acota la divergencia eliminándola" sino que da una asíntota finita (σ²/2π)·ln(π/√λ); separar noise-reconstruction de λ-bias; λ\*riesgo puede ser >0 a N grande. | **CORRECTA — aceptada** | Verificado numéricamente: λ=0: 0.010205@256² → 0.012422@1024² (~ln N); λ=1e-3: 0.007296@256², 0.006849@1024² → asíntota 0.007319 (−6.4 % por corte finito). §4.1 reescrita; §4.2: λ\*=c/N²>0 (ganancia ≤0.05 %) |
| **P1-3: la métrica de energía nula de pendientes NO mide altura irrecuperable** — h=(−1)^(x+y) tiene pendientes ≡ 0 (fracción nula 0.0) y sin embargo toda su altura es irrecuperable. | **CORRECTA — aceptada** | Reproducido: RMS(h)=1.0, ‖p‖=‖q‖=0 exacto, fracción=0.0. §5.6: manifest separa `null_slope_energy_fraction` (medible) de `irrecoverable_height_energy=UNKNOWN` (sin oráculo). Apéndice A v4 (test #3) |

**Micro-descubrimiento de la verificación (registrado por transparencia):** los
números de la revisión sólo se reproducen con los pesos de Parseval correctos
(§2.3). La primera verificación interna de esta parte usó pesos fila-0+columna-final
(≈+5 %) y luego "4 esquinas" (+20 % en régimen B) antes de llegar a la regla
columnas-frontera por brute force contra `fft2`. Ambas partes llegaron
independientemente a los mismos valores finales (curva 0.007955@64² y MC 0.007948@64²:
acuerdo 0.1 %).

Ningún punto rechazado en cuatro rondas. Checklist de cierre de la revisión (pendiente
antes de declarar `M6_MATH_CONTRACT_CLOSED`): (i) curva = símbolos del solver en AMBOS
regímenes ✓ (Apéndice A v4); (ii) MC multi-resolución de espectro completo incluyendo
Nyquist mixto ✓ (selftest #4); (iii) separación slope-energy vs height-energy en el
manifest ✓ (§5.6); sigue: runner repetible desde el commit ✓ (Apéndice A) → revisión
de invariantes → reconciliación documentaria. **No merge a main**; el veredicto de
cierre corresponde a la revisión externa.

## Apéndice A — Runner ejecutable (`run_lambda_study_v4.py`, NumPy puro; `--check` = recibos)

8 recibos: contraejemplo Nyquist (4.8e-16), espacio nulo del tablero, ceguera de la
métrica de pendientes ante altura nula (P1-3), MC multi-resolución de espectro
completo vs curva (N=32/64/128), acotamiento λ=1e-3 vs asíntota, y regresión de
exactitud integrable. `--check` ejecuta sólo el selftest; sin argumentos ejecuta el
estudio completo (28 celdas) y las curvas/regímenes A/B.

```python
"""Estudio sintético de derivación de screen_lambda para EXP-M6 — v4.

REV 4 (2026-10-15). Cambios sobre v3 (commit db5e5bd7), por la revisión externa
(tres P1 confirmados numéricamente por ambas partes):

1. CURVA ANALÍTICA = OPERADORES DEL SOLVER (P1-1). La curva ahora usa D(k)=
   |dx|²+|dy|² de los MISMOS símbolos del solver v3 (Nyquist anulado SÓLO por
   eje): E[MSE_ruido] = (σ²/N²)·Σ_{D>0} w(k)·D/(D+λ)². La máscara de v3
   excluyendo Nyquist mixto estaba obsoleta (el solver v3 sí procesa esos bins).
2. PESOS DE PARSEVAL CORREGIDOS: en la grilla rfft2 los bins auto-conjugados
   son las 4 ESQUINAS (ky∈{0,N/2} ∧ kx∈{0,N/2}), peso 1; el resto peso 2.
   (v3 usaba fila 0 + última columna: error ±5 % en el término de ruido.)
3. DIVERGENCIA CORREGIDA (P1-2): con λ en unidades del solver (k², rad²/px²),
   un λ fijo > 0 SÍ acota el MSE de ruido blanco:
       E[MSE_ruido(λ)] → (σ²/2π)·ln(π/√λ) + O(1)   [N→∞]
   mientras λ=0 crece como (σ²/2π)·ln N. La afirmación de v3 ("ningún λ
   constante detiene la divergencia") era FALSA. Lo que λ paga es sesgo de
   señal: atenuación k²/(k²+λ) en banda. λ* equilibra ambos términos y puede
   ser > 0 a N grande — la curva corregida lo calcula por resolución.
4. ENERGÍA NULA: `null_slope_energy_fraction` (antes nyquist_null_fraction)
   mide la energía de PENDIENTES descartada. NO mide altura perdida: una
   altura puramente en el espacio nulo (h=(-1)^{x+y}) deja pendientes
   idénticamente nulas. La energía de altura irrecuperable queda declarada
   UNKNOWN sin oráculo de altura (P1-3).

Modelo declarado (sin corpus): altura band-limited |ĥ|²∝k^-(β+2), banda
[2π/N, π/4] sin Nyquist estricto, RMS(altura)=1; ruido blanco σ (filtrado en
banda = Régimen A, o espectro completo = Régimen B), Q8 sintético, deriva LF.
"""
from __future__ import annotations

import sys

import numpy as np

N_VALUES = (256, 128)
BETAS = (2.0, 4.0)
SIGMAS = (0.01, 0.03, 0.1)
LAMBDAS = (0.0, 1e-5, 3e-5, 1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1, 3e-1, 1.0)
SEED = 20261015


# ---------------------------------------------------------------------------
# Operadores y solver (idénticos a v3: símbolos d coherentes)
# ---------------------------------------------------------------------------
def _d_symbols(n: int):
    kx = (2 * np.pi * np.fft.rfftfreq(n))[None, :]
    ky = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    dx, dy = 1j * kx, 1j * ky
    if n % 2 == 0:
        dx[:, -1] = 0.0
        dy[n // 2, :] = 0.0
    return dx, dy


def solve_screened(p: np.ndarray, q: np.ndarray, lam: float = 0.0):
    n = p.shape[0]
    dx, dy = _d_symbols(n)
    fp, fq = np.fft.rfft2(p), np.fft.rfft2(q)
    num = np.conj(dx) * fp + np.conj(dy) * fq
    num[0, 0] = 0.0
    denom = np.abs(dx) ** 2 + np.abs(dy) ** 2 + lam
    denom[denom == 0.0] = 1.0  # espacio nulo declarado: num ya es 0 ahí
    return np.fft.irfft2(num / denom, s=(n, n))


def _parseval_weights(shape) -> np.ndarray:
    """Pesos de Parseval en grilla rfft2 (row=ky fftfreq, col=kx rfftfreq):
    las columnas frontera kx=0 y kx=N/2 están COMPLETAMENTE materializadas
    (el conjugado de (ky,kx=N/2) es (−ky,kx=N/2), misma columna) → peso 1;
    las columnas interiores kx=1..N/2−1 representan además la columna espejo
    N−kx → peso 2. Verificado por brute force contra fft2 (N=4..64, exacto).
    [REV 4: corrige el intento previo de "4 esquinas", que dejaba peso 2 en
    la columna de Nyquist y sobrecontaba ~20% la energía de ruido.]"""
    rows, cols = shape
    w = np.full(shape, 2.0)
    w[:, 0] = 1.0
    w[:, -1] = 1.0
    return w


def _D_matrix(n: int) -> np.ndarray:
    dx, dy = _d_symbols(n)
    return np.abs(dx) ** 2 + np.abs(dy) ** 2


def null_slope_energy_fraction(p: np.ndarray, q: np.ndarray) -> float:
    """Fracción de la ENERGÍA DE PENDIENTES que cae en el espacio nulo del
    solver (bins con D=0). IMPORTANTE (P1-3): NO mide altura perdida — una
    altura puramente en el espacio nulo deja pendientes nulas y esta métrica
    da 0. La energía de altura irrecuperable es UNKNOWN sin oráculo de altura;
    debe reportarse como tal en el manifest, junto a esta métrica."""
    n = p.shape[0]
    D = _D_matrix(n)
    null = D == 0.0
    fp, fq = np.fft.rfft2(p), np.fft.rfft2(q)
    w = _parseval_weights(D.shape)  # pesos corregidos: sin ellos el numerador
    ep, eq = w * np.abs(fp) ** 2, w * np.abs(fq) ** 2  # (bins frontera) no es
    tot = float(np.sum(ep) + np.sum(eq)) + 1e-300  # comparable al denominador
    return float((np.sum(ep[null]) + np.sum(eq[null])) / tot)


# ---------------------------------------------------------------------------
# Modelo declarado
# ---------------------------------------------------------------------------
def make_signal(n: int, beta: float, rng: np.random.Generator):
    idx = np.fft.fftfreq(n) * n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2 + KY**2) * (2 * np.pi / n)
    nyq = (np.abs(KX) == n // 2) | (np.abs(KY) == n // 2)
    band = (kn >= 2 * np.pi / n) & (kn <= np.pi / 4) & ~nyq
    coef = rng.standard_normal((n, n)) + 1j * rng.standard_normal((n, n))
    amp = np.zeros((n, n))
    amp[band] = kn[band] ** (-(beta + 2) / 2)
    h_hat = coef * amp
    h_hat[0, 0] = 0.0
    h = np.real(np.fft.ifft2(h_hat))
    h /= np.sqrt(np.mean(h**2))
    kxf = (2 * np.pi * np.fft.fftfreq(n))[None, :]
    kyf = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    hn = np.fft.fft2(h)
    hn[0, 0] = 0.0
    return h, np.real(np.fft.ifft2(1j * kxf * hn)), np.real(np.fft.ifft2(1j * kyf * hn))


def q8_noise(p: np.ndarray, q: np.ndarray):
    """Q8 SINTÉTICO: cuantización a 8 bits del mapa de normales. NO equivale a
    normales authored reales (eso es EXP-M2)."""
    nz = np.ones_like(p)
    nx, ny = -p, -q
    nrm = np.sqrt(nx**2 + ny**2 + nz**2)
    nx, ny, nz = nx / nrm, ny / nrm, nz / nrm
    dec_x = np.rint((nx * 0.5 + 0.5) * 255) / 255 * 2 - 1
    dec_y = np.rint((ny * 0.5 + 0.5) * 255) / 255 * 2 - 1
    return -dec_x / nz, -dec_y / nz


def drift_noise(p, q, sigma, rng):
    n = p.shape[0]
    idx = np.fft.fftfreq(n) * n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2 + KY**2) * (2 * np.pi / n)
    band = (kn > 0) & (kn <= 16 * 2 * np.pi / n)
    ep = np.real(np.fft.ifft2((rng.standard_normal((n, n)) + 1j * rng.standard_normal((n, n))) * band))
    eq = np.real(np.fft.ifft2((rng.standard_normal((n, n)) + 1j * rng.standard_normal((n, n))) * band))
    ep *= sigma / np.sqrt(np.mean(ep**2))
    eq *= sigma / np.sqrt(np.mean(eq**2))
    return p + ep, q + eq


def _bandpass_mask(n: int) -> np.ndarray:
    idx = np.fft.fftfreq(n) * n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2 + KY**2) * (2 * np.pi / n)
    nyq = (np.abs(KX) == n // 2) | (np.abs(KY) == n // 2)
    return (kn >= 2 * np.pi / n) & (kn <= np.pi / 4) & ~nyq


# ---------------------------------------------------------------------------
# Curva analítica CORREGIDA: D(k) de los símbolos del solver
# ---------------------------------------------------------------------------
def analytic_curve(n: int, beta: float, sigma: float, lams, noise_filtered: bool,
                   include_signal: bool = True):
    """E[MSE(λ)] = (1/N⁴)·Σ_rfft w·(D/(D+λ))²·E|ĥ|²  [sesgo de señal]
                 + (1/N²)·Σ_rfft w·(D/(D+λ)²)·σ²              [ruido]
    con D(k)=|dx|²+|dy|² de los MISMOS símbolos del solver (P1-1) y pesos de
    Parseval rfft2 (col. frontera peso 1, interiores 2). Régimen A (noise_filtered=True): ruido sólo en banda;
    Régimen B (False): ruido blanco en TODO bin con D>0. Con λ>0 fijo el ruido
    de B acota a ≈(σ²/2π)·ln(π/√λ); con λ=0 crece como (σ²/2π)·ln N (P1-2).
    include_signal=False expone sólo el término de ruido (para MC de ruido puro)."""
    kxr = (2 * np.pi * np.fft.rfftfreq(n))[None, :]
    kyr = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    k = np.sqrt(kxr**2 + kyr**2)
    band = (k >= 2 * np.pi / n) & (k <= np.pi / 4)  # π/4 < π/2: sin Nyquist
    amp = np.zeros(band.shape)
    amp[band] = k[band] ** (-(beta + 2) / 2)
    eh = 2 * amp**2
    D = _D_matrix(n)
    w = _parseval_weights(D.shape)
    eh *= n**4 / (w * eh).sum()  # RMS(altura)=1: Σ w·eh = N⁴
    out = []
    for lam in lams:
        mse = 0.0
        if include_signal:
            # sesgo = (ĥ_est − ĥ) = −λ/(D+λ)·ĥ  [NO la transmisión D/(D+λ)²]
            bias_fac = np.where(D > 0, (lam / np.maximum(D + lam, 1e-300)) ** 2, 0.0)
            mse += float(np.sum(w * bias_fac * eh)) / n**4
        if noise_filtered:
            nmask = band & (D > 0)
        else:
            nmask = D > 0
        if lam == 0.0:
            noise_sum = float(np.sum(w[nmask] / D[nmask]) * sigma**2)
        else:
            noise_sum = float(np.sum(w[nmask] * D[nmask] / (D[nmask] + lam) ** 2) * sigma**2)
        mse += noise_sum / n**2
        out.append(float(np.sqrt(mse)))
    return np.array(out)


def lambda_star_continuous(n: int, beta: float, sigma: float, noise_filtered: bool = True):
    fine = np.geomspace(1e-12, 1.0, 20000)
    e = analytic_curve(n, beta, sigma, fine, noise_filtered)
    i = int(np.argmin(e))
    e0 = analytic_curve(n, beta, sigma, [0.0], noise_filtered)[0]
    return float(fine[i]), float(e[i]), float(e0)


# ---------------------------------------------------------------------------
# Recibos (--check)
# ---------------------------------------------------------------------------
def selftest() -> None:
    ok = 0

    # 1. Contraejemplo Nyquist (REV 3, se mantiene).
    N = 64
    x = np.arange(N)[None, :]
    y = np.arange(N)[:, None]
    h = (-1.0) ** x * np.sin(6 * np.pi * y / N)
    p0 = np.zeros_like(h)
    q0 = (-1.0) ** x * (6 * np.pi / N) * np.cos(6 * np.pi * y / N)
    rmse = float(np.sqrt(np.mean((solve_screened(p0, q0) - h) ** 2)))
    assert rmse < 1e-10, rmse
    print(f"[ok] contraejemplo Nyquist-X recuperado por derivada-Y: RMSE={rmse:.2e}")
    ok += 1

    # 2. Espacio nulo real: tablero sigue nulo (P1-3: en su variante PENDIENTES).
    cb = (np.indices((64, 64)).sum(axis=0) % 2) * 2 - 1
    std = float(np.std(solve_screened(cb * 0.5, cb * 0.0)))
    assert std < 1e-12, std
    print(f"[ok] tablero (pendientes en bin nulo): std={std:.1e}")
    ok += 1

    # 3. P1-3: altura en el espacio nulo deja pendientes nulas — la métrica de
    #    pendientes NO puede verla; se declara UNKNOWN (sin oráculo).
    i = np.arange(64)
    hcb = (-1.0) ** (i[:, None] + i[None, :])
    p_cD = 0.5 * (np.roll(hcb, -1, 1) - np.roll(hcb, 1, 1))
    q_cD = 0.5 * (np.roll(hcb, -1, 0) - np.roll(hcb, 1, 0))
    assert float(np.abs(p_cD).max()) == 0.0 and float(np.abs(q_cD).max()) == 0.0
    assert null_slope_energy_fraction(p_cD, q_cD) == 0.0
    print("[ok] h=(-1)^(x+y): pendientes≡0 ⇒ fracción pendiente-nula=0 — altura "
          "perdida UNKNOWN (requiere oráculo; separado en el manifest)")
    ok += 1

    # 4. P1-1/P1-2: MC multi-resolución de ruido blanco ESPECTRO COMPLETO (λ=0)
    #    vs curva corregida (Régimen B) — incluye bandas mixtas de Nyquist.
    rng = np.random.default_rng(5)
    for n in (32, 64, 128):
        mses = []
        for _ in range(100):
            pe = 0.1 * rng.standard_normal((n, n))
            qe = 0.1 * rng.standard_normal((n, n))
            mses.append(float(np.mean(solve_screened(pe, qe) ** 2)))
        pred = analytic_curve(n, 2.0, 0.1, [0.0], noise_filtered=False,
                              include_signal=False)[0] ** 2
        ratio = float(np.mean(mses)) / pred
        assert 0.93 < ratio < 1.07, (n, ratio)
        print(f"[ok] MC(100, N={n}, espectro completo) vs curva D: ratio={ratio:.4f}")
        ok += 1

    # 5. P1-2: λ fijo acota el ruido; λ=0 diverge con N (fórmula y números).
    lam = 1e-3
    vals = [analytic_curve(n, 2.0, 0.1, [lam], noise_filtered=False,
                           include_signal=False)[0] ** 2
            for n in (128, 256, 512, 1024)]
    assert vals[-1] < vals[1], vals  # decrece hacia la asíntota
    asymp = 0.1**2 / (2 * np.pi) * np.log(np.pi / np.sqrt(lam))
    assert abs(vals[-1] - asymp) / asymp < 0.15, (vals[-1], asymp)
    lam0_1024 = analytic_curve(1024, 2.0, 0.1, [0.0], noise_filtered=False,
                               include_signal=False)[0] ** 2
    print(f"[ok] λ=1e-3 acota el ruido: MSE(1024²)={vals[-1]:.6f} vs asíntota "
          f"σ²/(2π)·ln(π/√λ)={asymp:.6f} (λ=0 en 1024²: {lam0_1024:.6f})")
    ok += 1

    # 6. Exactitud integrable sin Nyquist (regresión del solver).
    n = 128
    h2, p2, q2 = make_signal(n, 2.0, np.random.default_rng(1))
    e = float(np.sqrt(np.mean((solve_screened(p2, q2, 0.0) - h2) ** 2)))
    assert e < 1e-12, e
    print(f"[ok] campo integrable sin Nyquist: RMSE={e:.2e}")
    ok += 1

    print(f"selftest: {ok}/8 recibos OK")


def main() -> None:
    if "--check" in sys.argv:
        selftest()
        return
    rng = np.random.default_rng(SEED)
    print(f"SEED={SEED} · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · "
          f"curva con operadores D del solver v3 + pesos Parseval rfft2 columnas-frontera · "
          f"regímenes A(filtrado)/B(espectro completo) separados")
    for n in N_VALUES:
        for beta in BETAS:
            h, p, q = make_signal(n, beta, rng)
            lam_star_A, e_star_A, e0_A = lambda_star_continuous(n, beta, 0.1, True)
            lam_star_B, e_star_B, e0_B = lambda_star_continuous(n, beta, 0.1, False)
            casos = {
                **{f"gauss σ={s}": (p + s * rng.standard_normal(p.shape),
                                    q + s * rng.standard_normal(p.shape), s)
                   for s in SIGMAS},
                "Q8 sintético": (*q8_noise(p, q), None),
                "limpio": (p, q, None),
                "driftLF σ=.05": (*drift_noise(p, q, 0.05, rng), 0.05),
                "driftLF σ=.5": (*drift_noise(p, q, 0.5, rng), 0.5),
            }
            for label, (pn, qn, sigma) in casos.items():
                sigma_eff = sigma if sigma is not None else float(
                    np.sqrt(np.mean((pn - p) ** 2 + (qn - q) ** 2) / 2))
                rmses = [float(np.sqrt(np.mean((solve_screened(pn, qn, lam) - h) ** 2)))
                         for lam in LAMBDAS]
                i_opt = int(np.argmin(rmses))
                e_f = analytic_curve(n, beta, sigma_eff, LAMBDAS, True)
                e_u = analytic_curve(n, beta, sigma_eff, LAMBDAS, False)
                print(f"N={n:4d} β={beta:.0f} {label:13s} σ_eff={sigma_eff:.4f} | "
                      f"λ_opt_emp={LAMBDAS[i_opt]:g} (RMSE {rmses[i_opt]:.4f}) "
                      f"λ=0: {rmses[0]:.4f} (×{rmses[0]/rmses[i_opt]:.3f}) | "
                      f"E λ* A(filtr)={LAMBDAS[int(np.argmin(e_f))]:g} "
                      f"B(compl)={LAMBDAS[int(np.argmin(e_u))]:g} | "
                      f"λ*riesgo A(σ=.1)={lam_star_A:.1e} B={lam_star_B:.1e}")


if __name__ == "__main__":
    main()
```

## Apéndice B — Salida registrada (SEED=20261015)

**B.1 `--check` (selftest, 8 recibos):**

```
[ok] contraejemplo Nyquist-X recuperado por derivada-Y: RMSE=4.80e-16
[ok] tablero (pendientes en bin nulo): std=0.0e+00
[ok] h=(-1)^(x+y): pendientes≡0 ⇒ fracción pendiente-nula=0 — altura perdida UNKNOWN (requiere oráculo; separado en el manifest)
[ok] MC(100, N=32, espectro completo) vs curva D: ratio=1.0087
[ok] MC(100, N=64, espectro completo) vs curva D: ratio=0.9952
[ok] MC(100, N=128, espectro completo) vs curva D: ratio=0.9796
[ok] λ=1e-3 acota el ruido: MSE(1024²)=0.006849 vs asíntota σ²/(2π)·ln(π/√λ)=0.007319 (λ=0 en 1024²: 0.012422)
[ok] campo integrable sin Nyquist: RMSE=3.05e-16
selftest: 8/8 recibos OK
```

**B.2 Estudio completo (28 celdas):**

```
SEED=20261015 · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · curva con operadores D del solver v3 + pesos Parseval rfft2 columnas-frontera · regímenes A(filtrado)/B(espectro completo) separados
N= 256 β=2 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0099) λ=0: 0.0099 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.3e-06 B=2.2e-06
N= 256 β=2 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0327) λ=0: 0.0327 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.3e-06 B=2.2e-06
N= 256 β=2 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=0 (RMSE 0.0962) λ=0: 0.0962 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.3e-06 B=2.2e-06
N= 256 β=2 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0023) λ=0: 0.0023 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.3e-06 B=2.2e-06
N= 256 β=2 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.3e-06 B=2.2e-06
N= 256 β=2 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=3e-05 (RMSE 0.3024) λ=0: 0.3065 (×1.014) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.3e-06 B=2.2e-06
N= 256 β=2 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0040) λ=0: 3.2132 (×3.200) | E λ* A(filtr)=3e-05 B(compl)=3e-05 | λ*riesgo A(σ=.1)=1.3e-06 B=2.2e-06
N= 256 β=4 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0096) λ=0: 0.0096 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.0e-06 B=1.8e-06
N= 256 β=4 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0303) λ=0: 0.0303 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.0e-06 B=1.8e-06
N= 256 β=4 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=0 (RMSE 0.0999) λ=0: 0.0999 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.0e-06 B=1.8e-06
N= 256 β=4 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0079) λ=0: 0.0079 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.0e-06 B=1.8e-06
N= 256 β=4 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.0e-06 B=1.8e-06
N= 256 β=4 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=0 (RMSE 0.3058) λ=0: 0.3058 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.0e-06 B=1.8e-06
N= 256 β=4 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0053) λ=0: 3.2801 (×3.263) | E λ* A(filtr)=3e-05 B(compl)=3e-05 | λ*riesgo A(σ=.1)=1.0e-06 B=1.8e-06
N= 128 β=2 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0101) λ=0: 0.0101 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.2e-06 B=8.9e-06
N= 128 β=2 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0296) λ=0: 0.0296 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.2e-06 B=8.9e-06
N= 128 β=2 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=3e-05 (RMSE 0.1026) λ=0: 0.1033 (×1.007) | E λ* A(filtr)=1e-05 B(compl)=1e-05 | λ*riesgo A(σ=.1)=5.2e-06 B=8.9e-06
N= 128 β=2 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0020) λ=0: 0.0020 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.2e-06 B=8.9e-06
N= 128 β=2 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.2e-06 B=8.9e-06
N= 128 β=2 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=0.0001 (RMSE 0.1586) λ=0: 0.1600 (×1.009) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.2e-06 B=8.9e-06
N= 128 β=2 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0083) λ=0: 1.6611 (×1.647) | E λ* A(filtr)=0.0001 B(compl)=0.0003 | λ*riesgo A(σ=.1)=5.2e-06 B=8.9e-06
N= 128 β=4 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0087) λ=0: 0.0087 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.1e-06 B=7.1e-06
N= 128 β=4 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0302) λ=0: 0.0302 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.1e-06 B=7.1e-06
N= 128 β=4 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=3e-05 (RMSE 0.0889) λ=0: 0.0893 (×1.004) | E λ* A(filtr)=0 B(compl)=1e-05 | λ*riesgo A(σ=.1)=4.1e-06 B=7.1e-06
N= 128 β=4 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0021) λ=0: 0.0021 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.1e-06 B=7.1e-06
N= 128 β=4 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.1e-06 B=7.1e-06
N= 128 β=4 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=3e-05 (RMSE 0.1642) λ=0: 0.1647 (×1.003) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.1e-06 B=7.1e-06
N= 128 β=4 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0131) λ=0: 1.6493 (×1.628) | E λ* A(filtr)=0.0001 B(compl)=0.0001 | λ*riesgo A(σ=.1)=4.1e-06 B=7.1e-06
```

**B.3 Barrido λ\* = c/N² (β=2, σ=0.1; reproduce §4.2):**

```python
# python -i  (con run_lambda_study_v4.py en el cwd)
for n in (128, 256, 512, 1024):
    for filt in (True, False):
        lam, e_star, e0 = lambda_star_continuous(n, 2.0, 0.1, filt)
        print(n, "A" if filt else "B", f"lambda*={lam:.3e} ganancia={e_star/e0-1:+.3e}")
```

```
128 A lambda*=5.166e-06 ganancia=-3.219e-04   128 B lambda*=8.891e-06 ganancia=-5.308e-04
256 A lambda*=1.294e-06 ganancia=-2.653e-04   256 B lambda*=2.227e-06 ganancia=-4.734e-04
512 A lambda*=3.241e-07 ganancia=-2.253e-04   512 B lambda*=5.570e-07 ganancia=-4.271e-04
1024 A lambda*=8.096e-08 ganancia=-1.957e-04  1024 B lambda*=1.391e-07 ganancia=-3.891e-04
lambda*/N^2 : c_A ∈ [0.0846, 0.0849]; c_B ∈ [0.1457, 0.1459]  (px²)
```
