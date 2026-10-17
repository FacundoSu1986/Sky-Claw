# EXP-M6 — Propuesta de derivación de `screen_lambda` (y estatus de `gamma`) — REV 7.3

> **Estado:** `PROPOSAL` — insumo externo para la ETAPA A del prereg de EXP-M6
> ([PR #675](https://github.com/FacundoSu1986/Sky-Claw/pull/675), DRAFT). **No autoriza
> implementación ni ejecución de M6** (`M6_IMPLEMENTATION_BLOCKED = YES` vigente).
>
> **REV 7.3 (2026-10-17):** reconciliación de los hallazgos CR-1..CR-4 del revisor
> (PR #738) sobre la REV 7.2; adjudicación completa y evidencia en §8.6.
> (1) **CR-4 (único sustantivo):** el término de sesgo de `analytic_curve` usaba el
> cociente de esperanzas `Σu·E[v]/Σv·E[v]`; el valor correcto es la esperanza
> **POR REALIZACIÓN** `E[Σu_o·E_o/Σv_o·E_o]` con `E_o~Exp(1)` iid por órbita ±k —
> el modelo previo sobreestimaba el sesgo ≈7.4% (β=2) / ≈4.8% (β=4). Implementado
> por cuadratura Gauss-Legendre (m=600, márgenes (36,16); convergencia medida
> 1e-10) y validado contra MC directo de `make_signal`+`solve_screened` (|z|≤1.4;
> recibo #10 del runner). λ* corregidos: `c = λ*·N²` pasa a c_A ≈ 0.0914–0.0918,
> c_B ≈ 0.1574–0.1578; la ganancia esperada sigue **< 0.06 %** (máx. 0.0573 %
> @128² rég. B). Los λ* históricos 5.17e-06 / 8.89e-06 quedan **superseded**.
> La ruta de ruido (curva λ=0, V∞, dorados) queda **intacta**.
> (2) **CR-3:** `noise_mse_limit` = `V∞(λ)` es sólo el límite del régimen B; el
> régimen A converge a `V_A(λ)` (disco r≤π/4; `V_A(1e-3)=0.004319357` vs
> `V∞(1e-3)=0.006698379`). (3) **CR-1:** el snippet de B.3 era un fragmento
> `python -i` que no definía `lambda_star_continuous` → import explícito y
> ejecución real en el gate. (4) **CR-2:** la etiqueta de escalado declaraba el
> cociente invertido; es `c = λ*·N²` con los rangos corregidos. Solver FFT,
> símbolos D, pesos y dorados de ruido: sin cambios.
>
> **REV 6.1 (2026-10-16):** adjudicación acotada de Nyquist mixto y porcentajes (§8.5).
> El solver FFT y sus símbolos quedan sin cambios; los dorados y las seis revisiones
> históricas se preservan. Contrato global M6 aún abierto; implementación bloqueada.
>
> **REV 6 (2026-10-15, ronda 6):** reconciliación asintótica sobre el PR #738. Tres
> hallazgos adjudicados (§8.4), todos aceptados con verificación numérica previa:
>
> 1. **P1 — El límite asintótico de REV 5 era incorrecto.** (σ²/2π)·ln(π/√λ) es sólo
>    el comportamiento dominante para λ→0, NO el límite del MSE de ruido con λ>0
>    fijo. Límite exacto del operador declarado:
>    **V∞(λ) = (σ²/(2π)²)·∫∫_{[-π,π]²} k²/(k²+λ)² dkx dky = 0.006698379**
>    (λ=1e-3, σ=0.1; dos vías independientes: disco en forma cerrada + esquinas
>    Gauss-Legendre m=200/400/800, estable a 2e-12; extrapolación discreta de 3
>    puntos = 0.006698378). Con los valores registrados, la fórmula log queda
>    **+9.2639% ≈ +9.26%** por encima; N=1024 queda **+2.2524% ≈ +2.25%** y
>    N=2048 **+1.1262% ≈ +1.13%**. El comportamiento de exceso ~1/N es observado,
>    no demostrado como teorema general. Los valores
>    dorados de REV 5 quedan intactos (no se tocó ninguna medición, sólo la
>    caracterización del límite). RED→GREEN con oráculo independiente (§4.1).
> 2. **§3 — Conjugación**: el código ya era correcto (`conj(dx)·p̂ + conj(dy)·q̂`);
>    la fórmula del §2.1 `(dx·p̂+dy·q̂)*` era ambigua/incorrecta → corregida; pin de
>    test por modo único añadido (la mutación de conjugación falla 2 tests).
> 3. **§4 — Gate fail-closed estructural**: ningún workflow fija
>    `SKYCLAW_REQUIRE_M6_CONTRACT` (verificado) → la ruta compatible permitía
>    `5 skipped` con runner ausente (falso verde posible, demostrado). REV 6:
>    **presencia incondicional del runner** (FAIL siempre, sin variables de
>    entorno); variable eliminada por superada. Demostración negativa: runner
>    eliminado → `1 failed, 6 skipped` (job rojo).
>
> **REV 5 (2026-10-15, ronda 5):** reconciliación acotada tras la revisión de la REV 4
> (commit `5c473d5`) — "favorable para continuar, aún sin cerrar M6". Tres P2, **los
> tres confirmados/aceptados** (adjudicación §8.3): (1) pesos de Parseval generales
> para toda paridad — la regla de v4 fallaba en N impar (P2-1); (2) rewording: λ\*≈c/N²
> es observación bajo el prior (no teorema) y la ganancia correcta es "<0.06 % en los
> escenarios examinados" — el propio dato 128²-B da 0.053 % (P2-2); (3) gate
> automatizado en el repo: runner canónico `scripts/run_lambda_study_m6.py` +
> `tests/test_native_parallax_m6_lambda_gate.py` con valores dorados congelados,
> verificado por mutación (P2-3). Mallas CUADRADAS de toda paridad; rectangulares
> FUERA de contrato. El veredicto de cierre de M6 sigue sin declararse (ver §8.3).
>
> **REV 4 (2026-10-15):** corrigió la REV 3 (commit `db5e5bd7`) tras la cuarta revisión
> externa. Tres hallazgos P1, **los tres confirmados numéricamente por esta parte y
> aceptados** (adjudicación §8.2):
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
`ĥ = (conj(dx)·p̂ + conj(dy)·q̂) / (|dx|²+|dy|²+λ)` (solución LS por modo;
**REV 6**: el numerador es `conj(dx)·p̂ + conj(dy)·q̂` — cada símbolo conjugado,
NO la conjugación de la suma completa `conj(dx·p̂ + dy·q̂)`, que es una ecuación
distinta; el texto REV≤5 `(dx·p̂+dy·q̂)*` era ambiguo y quedó corregido aquí. El
código (`solve_screened`) siempre usó la convención correcta; pin de test por
modo único en el gate: `p=sin(3·2πx/N)` → `h=−cos(3·2πx)/k_N` con RMSE<1e-10,
y la convención errónea invierte el signo (RMSE>1) — mutación M2 detectada),
con dos consecuencias
exactas: atenuación de amplitud `k²/(k²+λ)` (modo de corte `k_c=√λ`, `VERIFIED`) y el
término de ruido por **proyección ortogonal** `E|num(k)|² = D(k)·N²σ²` con
`D(k)=|dx|²+|dy|²` (la REV 1 sobrecontaba ×2; corregido en REV 2).

### 2.2 Spec de operadores: qué es observable en Nyquist (REV 3)

**Definición (obligatoria antes de cualquier implementación).** En malla par, la
derivada de un modo Nyquist **no es observable en la malla**: el contenido on-grid del
bin `k=N/2` mezcla las fases cos/sin y ningún operador consistente puede separarlas.
La convención estándar de métodos espectrales es `d = 0` en el eje Nyquist de cada eje:

```
dx(kx,ky) = 0                         si kx = 0 o kx = π (N par)
            i·kx                      fuera de esas frecuencias
dy(kx,ky) = 0                         si ky = 0 o |ky| = π (N par; el orden FFT puede guardarlo como −π)
            i·ky                      fuera de esas frecuencias
D(kx,ky) = |dx|² + |dy|²
ĥ(k) = (conj(dx)·p̂ + conj(dy)·q̂) / (D+λ)     [bins con D+λ=0: ĥ=0]
```

Para N impar no existe una frecuencia Nyquist exacta; cada símbolo es `i·k` salvo
el cero de su eje. **Espacio nulo REAL (declarándolo, no silenciándolo).** Sólo son
no observables los bins donde **ambos** símbolos valen cero. Un bin con Nyquist en
X pero frecuencia Y no nula, o viceversa, **conserva** la altura por la derivada del
otro eje; por eso una línea Nyquist no es un espacio nulo completo.

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

La justificación es estructural: en la **columna** de Nyquist `kx=N/2` (que existe
sólo si N es par) el conjugado de `(ky, N/2)` es `(−ky, N/2)` — **misma columna**,
también almacenada; ídem `kx=0`. Esos bins representan un único bin de la grilla
completa (peso 1). Sólo las columnas restantes representan dos bins full-grid (el
propio y el espejo `N−kx`, no almacenado): peso 2. En N **impar** NO hay columna de
Nyquist: la última columna `kx=(N−1)/2` tiene espejo `(N+1)/2` no almacenado y pesa 2
— la regla `w[:, -1] = 1` de la REV 4 era un bug para N impar (detectado por la
revisión en la ronda 5; ratios energía-calculada/real: 0.73 @5², 0.76 @7², 0.98 @33²).
Fórmula general (REV 5): `w[:,0]=1`; `w[:,N/2]=1` sólo si N par; resto 2. Verificación
brute force exacta a máquina: `Σ|fft2(h)|² = Σ w·|rfft2(h)|²`, N∈{3,4,5,7,8,32,33,64}
(recibos de selftest #6 y del gate). `VERIFIED`.

**Política de dimensiones (fijada en REV 5):** mallas **CUADRADAS**, cualquier
paridad. Rectangulares: FUERA de contrato actual (el espejo de rfft2 es sólo el eje-x,
la extensión es directa — se resolverá para el producto, no en este estudio).

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
  (medición 2026-10-15; REV 3 medía 1.0055 con su curva defectuosa). Su límite
  continuo (N→∞) es **`V_A(λ) = (σ²/(2π)²)·∫∫_{r≤π/4} k²/(k²+λ)² dkx dky`** (disco
  en forma cerrada; con σ=0.1, λ=1e-3: `V_A = 0.004319357`), **distinto** del límite
  del régimen B, `V∞(λ)`=0.006698379 (REV 7.3, CR-3: `noise_mse_limit` sólo es el
  límite del régimen B).
- **Régimen B — "ruido sin filtrar"** (lo que el solver recibe con ruido blanco real):
  `E[MSE(0)] = (σ²/N²)·Σ_{D>0} w/D ≈ (σ²/2π)·ln N + O(1)` — divergencia logarítmica
  de FC. MC multi-resolución de espectro completo (100 realizaciones, N=32/64/128,
  incluye columnas mixtas de Nyquist): ratios MC/curva **1.0087 / 0.9952 / 0.9796**
  (`--check` del Apéndice A). `VERIFIED`.

**Enunciado correcto de la divergencia (REV 6 — reemplaza al de REV 3/REV 5):**

| Nivel | Fórmula | Estado |
|---|---|---|
| Modelo discreto exacto | `(σ²/N²)·Σ_{D>0} w·D/(D+λ)²` | lo que implementa `analytic_curve` (`VERIFIED`) |
| Límite continuo (N→∞) | **`V∞(λ) = (σ²/(2π)²)·∫∫_{[-π,π]²} k²/(k²+λ)² dkx dky`** | límite EXACTO del operador (REV 6, ronda 6) |
| Aproximación log (λ→0) | `(σ²/2π)·ln(π/√λ)` | **sólo comportamiento dominante — NO es el límite**; con los datos registrados: +9.2639% ≈ +9.26% sobre V∞ |
| Error de resolución finita | observado ~1/N en estos tamaños; **no es un teorema general** | +8.9259% ≈ +8.93% @256²; +4.5047% ≈ +4.50% @512²; +2.2524% ≈ +2.25% @1024²; +1.1262% ≈ +1.13% @2048² |
| λ=0 | `(σ²/2π)·ln N + O(1)` | divergencia infrarroja **sin cota** con N |

**Porcentajes reconciliados desde los datos registrados (λ=1e-3, σ=0.1):**
`V∞=0.006698379`; aproximación log `0.007318911`; discreto N=1024
`0.006849252`; discreto N=2048 `0.006773816`. Se calcula
`100·(valor−V∞)/V∞`: log **+9.2639%** (presentación **+9.26%**), N=1024
**+2.2524%** (presentación **+2.25%**) y N=2048 **+1.1262%** (presentación
**+1.13%**). Los valores fuente no se alteran; sólo se corrigen sus cocientes y
redondeos de presentación.

**P1 de la ronda 6 (aceptado tras verificación):** la REV 5 presentaba la fórmula
log como el límite. Verificación independiente de REV 6 con DOS vías que no
reutilizan `analytic_curve`:

- **Disco + esquinas:** `V∞(λ) = (σ²/(2π)²)·[π·(ln((π²+λ)/λ) + λ/(π²+λ) − 1) + 4·I_esquinas]`,
  esquinas por Gauss-Legendre anidado: m=200 → 0.006698379225; m=400 → …241;
  m=800 → …243 (estable a 2e-12). **V∞(1e-3, σ=0.1) = 0.006698379** — coincide con
  la referencia independiente de la revisión (0.006698379) al dígito.
- **Extrapolación de la propia suma discreta** (3 puntos, modelo `V∞ + a/N + b/N²`):
  **0.006698378** (a=0.1545, b=−3.5e-3) — coherente en 1e-9 con la cuadratura.
- Convergencia discreta medida (λ=1e-3): 256²: 0.007296271 · 512²: 0.007000119 ·
  1024²: 0.006849252 · 2048²: 0.006773816. A partir de los valores impresos,
  los excesos relativos son +8.9259%, +4.5047%, +2.2524% y +1.1262%; los cocientes
  de excesos por doblar N son aproximadamente 1.98, 2.00 y 2.00. Esto es evidencia
  finita compatible con ~1/N, no una demostración de una ley general. La explicación
  "−6.4 % por corte finito" de la REV 5 queda **RETIRADA**: la discrepancia real era
  que la referencia log no era el límite.

**Acotación que justifica el límite para λ>0 fijo:** sea
`fλ(t)=t/(t+λ)²`, `t≥0`. Como `sup fλ=1/(4λ)`, el integrando es continuo y acotado.
En N par, el D discreto sólo difiere de `kx²+ky²` sobre la columna y la fila
Nyquist: en la grilla completa hay a lo sumo `2N−1` bins excepcionales. Cada
contribución difiere en no más de `1/(4λ)`, así que el error entre la suma de
símbolos y la suma de Riemann del integrando continuo queda acotado por
`σ²(2N−1)/(4λN²)=O(1/N)`. La suma de Riemann converge a la integral porque el
integrando es continuo en `[-π,π]²`. Para N impar no hay línea Nyquist exacta y
D coincide con `kx²+ky²` en la grilla (salvo el DC, que también aporta cero).
Por tanto las líneas excepcionales tienen medida nula en el límite para λ>0,
pero **eso no convierte cada bin Nyquist mixto en D=0**.

**Nyquist y espacio nulo (§4 del mandato):** con N par y coordenadas `rfft2`
(shape `(N, N/2+1)`), `dx=0` en `kx=0` y `kx=N/2`, y `dy=0` en `ky=0` y
`ky=N/2`. D es cero sólo cuando ambos símbolos se anulan simultáneamente, en
exactamente estos cuatro bins: `(ky=0, kx=0)`, `(ky=0, kx=N/2)`,
`(ky=N/2, kx=0)`, `(ky=N/2, kx=N/2)`. Un modo mixto `(ky=3, kx=N/2)` tiene
`dx=0`, `dy=i·6π/N` y `D=(6π/N)²>0`; se recupera mediante la derivada Y. Para N
impar no hay frecuencia Nyquist exacta y el único bin nulo es `(ky=0, kx=0)`
(media constante). La línea Nyquist tiene medida nula en el límite continuo,
pero no todos sus bins son nulos. La prueba por campo analítico y el oráculo de
símbolos de tests verifican esta distinción.

**Distinción operativa (§1.6 del mandato):** λ>0 fijo regulariza la divergencia
infrarroja del RUIDO (MSE → V∞ < ∞), pero **NO recupera la señal**: el sesgo
`(λ/(D+λ))²` de banda baja persiste y crece con λ. Ninguna conclusión operativa
cambia: con ruido blanco sin filtrar ningún λ constante arregla el problema —
filtrar las pendientes de entrada sigue siendo la herramienta correcta
(`INFERRED`, a falsar en #675). `--check` y el gate reproducen la separación de
tres niveles (recibo #5 del runner; test `test_limite_continuo_V_infinito`).

**Separación noise-reconstruction vs λ-bias (pedida por la revisión):** el término
`(λ/(D+λ))²·E|ĥ|²` es PÉRDIDA DE SEÑAL (mono. creciente en λ); el término
`(D/(D+λ)²)·σ²` es RUIDO RESIDUAL (mono. decreciente en λ). Son mecanismos distintos,
se reportan por separado en el runner (la curva acepta `include_signal` para exponer
sólo el de ruido — así se validó contra MC de ruido puro).

### 4.2 Métricas separadas: riesgo esperado vs realización (λ\* recalculado)

- **Riesgo esperado (curva corregida).** Argmin continuo (malla geométrica gruesa de
  200 puntos + refinamiento local 80×3; sobre el sesgo **exacto por realización**,
  REV 7.3 §8.6):
  (N=128, β=2, σ=0.1): **λ\*_A = 5.58e-06** (rég. A) / **9.61e-06** (rég. B), con
  ganancia relativa frente a λ=0 de **−0.035 %** / **−0.057 %**. Escalado **observado
  bajo el prior declarado** (P2-2: observación, no teorema cerrado): **λ\* ≈ c/N²**
  con c_A ≈ 0.0914–0.0918, c_B ≈ 0.1574–0.1578 (px²) para N∈{128,256,512,1024}
  (reproducción en Apéndice B, B.3). Los valores publicados en REV 4–7.2
  (5.17e-06 / 8.89e-06; c_A ≈ 0.0846–0.0849, c_B ≈ 0.1457–0.1459) provenían del
  cociente de esperanzas del sesgo y quedan **superseded** (≈7 % bajos; §8.6).
  Interpretación: el trade-off vive en el modo más bajo
  de la banda (`k_min=2π/N`, `D_min∝N⁻²`), no en la cola de ruido. **Ganancia esperada
  < 0.06 % en los escenarios examinados** (máx. 0.0573 % @128² rég. B — el enunciado
  REV-4 "≤ 0.05 %" era falso para ese propio escenario).
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

Enunciado defendible (REV 5): **λ=0 es el baseline correcto bajo este prior — el
óptimo continuo existe (λ\*≈c/N²>0) pero le gana a λ=0 menos del 0.06 % en los
escenarios examinados — y `λ=1e-3` sigue sin ninguna justificación** (su sesgo en banda baja cuesta más que todo el
ruido que suprime). `EJECUTADO`.

### 4.3 Tabla (28 celdas; volcado completo Apéndice B)

| Escenario | λ_opt empírico | Ganancia | λ\*riesgo continuo (A / B) | Nota |
|---|---|---|---|---|
| Blanco σ=0.01–0.1 (12) | **0** (9/12; 3×3e-05 en N=128 σ=0.1) | ≤ ×1.007 | 1.1–1.4e-06 / 1.9–2.4e-06 (N=256) · 4.4–5.6e-06 / 7.5–9.6e-06 (N=128) | rég. B aplica al experimento |
| Q8 sintético (4) | **0** (4/4) | ×1.000 | ídem | σ_eff≈0.0023 |
| Limpio (4) | **0** | — | ídem | ~2.5e-16 |
| Deriva LF σ=0.05 (4) | 0–1e-4 | ≤ ×1.014 | ídem | |
| Deriva LF σ=0.5 (4) | 1 (borde de rejilla) | ×1.63–3.26 | ídem | RMSE(λ\*)≈1.0: SNR≈1, sin señal útil |

## 5. Recomendación para el prereg de EXP-M6 (REV 4)

1. **Solver spec:** símbolos `dx, dy` con anulación SÓLO en el eje Nyquist de cada
   eje (§2.2); denominador compuesto de los MISMOS símbolos; espacio nulo declarado y
   medido. Prohibida la máscara por bins de la REV 2.
2. **Pesos de Parseval** (§2.3) obligatorios en cualquier evaluación espectral del
   MSE (curvas, recibos, manifest): columna `kx=0` peso 1, columna de Nyquist peso 1
   SÓLO si N es par, resto peso 2 (fórmula general, toda paridad; mallas cuadradas).
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
7. **Gate automatizado (REV 5 — P2-3; endurecido en REV 6):** el runner canónico
   vive en el repo (`scripts/run_lambda_study_m6.py`) y
   `tests/test_native_parallax_m6_lambda_gate.py` congela los recibos numéricos
   (SEED=20261015; curva λ=0 rég. B con los 5 valores dorados de dos partes
   N∈{32,64,128,256,512}; **límite continuo V∞(1e-3)=0.006698379 con oráculo
   independiente** (REV 6); Parseval toda paridad; conjugación por modo único;
   selftest 9/9). Ejecutar con `pytest tests/test_native_parallax_m6_lambda_gate.py
   --noconftest -q`. **Fail-closed estructural (REV 6):** la ausencia del runner es
   FAIL SIEMPRE (test de presencia incondicional, sin variables de entorno) —
   ningún workflow fijaba `SKYCLAW_REQUIRE_M6_CONTRACT` y la ruta compatible
   permitía `5 skipped` con runner ausente (falso verde posible, demostrado y
   eliminado). Mutaciones adversariales cubiertas: log-como-límite, conjugación
   errónea, pesos v4, runner ausente y tolerancia relajada (ver §8.4). El estudio
   completo (28 celdas, ~25 s) NO corre en CI — sólo el gate (<1 s).

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

### 6.3 Gate del contrato matemático (REV 5)

`scripts/run_lambda_study_m6.py` (runner canónico) + `tests/test_native_parallax_m6_lambda_gate.py`
(recibos congelados, ver §5.7). Estado en este commit: **4 passed en <1 s**. El doc
incluye un snapshot del runner en Apéndice A y declara canónico el archivo de
`scripts/`.

**Fail-closed estructural (REV 6 — sustituye al modo por entorno de REV 5):**
la ausencia del runner canónico es **FAIL siempre** (test de presencia
incondicional), independientemente de variables de entorno. Razón: se verificó que
NINGÚN workflow de CI fijaba `SKYCLAW_REQUIRE_M6_CONTRACT`, por lo que el modo
compatible de REV 5 permitía `5 skipped` con runner ausente — un falso verde
posible en el propio PR #738. Demostración negativa: eliminando el runner, el gate
pasa de `7 passed` a `1 failed, 6 skipped` (job rojo) SIN ninguna configuración de
CI. La variable `SKYCLAW_REQUIRE_M6_CONTRACT` quedó eliminada por superada
(documentado aquí; el harness H4 de #737 conserva SU variable por una razón
distinta y justificada: debe correr también en árboles SIN la API H4).

## 7. Estados (REV 4)

| Objeto | Estado |
|---|---|
| Operadores `dx,dy` con Nyquist por eje + LS coherente | `VERIFIED` (contraejemplo 4.8e-16; regresiones OK) |
| Máscara por bins de REV 2 | `RETIRADO` (defecto confirmado) |
| Pesos de Parseval generales, toda paridad (§2.3) | `VERIFIED` (fft2 exacto N∈{3,4,5,7,8,32,33,64}; MC/curva 0.98–1.01; regla REV-4 `w[:,-1]=1` retirada para N impar) |
| Curva analítica con `D` del solver (§4) | `VERIFIED` (P1-1; ratios MC 1.009/0.995/0.980; coincidencia con revisión al dígito) |
| Curva analítica REV 3 y sus valores λ=0 | `RETIRADO` (P1-1: fórmula de dispersión y pesos incorrectos) |
| Fórmula de ruido `D·N²σ²` (proyección) | `VERIFIED` (MC banda 0.980; espectro completo 0.98–1.01) |
| Divergencia λ=0 ~ln N (sin cota) | `VERIFIED` (P1-2; `--check` #5) |
| Límite continuo V∞(λ)=(σ²/(2π)²)∫∫ k²/(k²+λ)² — exacto | `VERIFIED` (ronda 6; 2 vías: GL disco+esquinas y extrapolación discreta; coincidencia con la revisión al dígito) |
| Fórmula log (σ²/2π)·ln(π/√λ) | `RETIRADO` como límite (sólo dominante λ→0; +9.2639% ≈ +9.26% sobre V∞ @1e-3) |
| Exceso finito-N ~1/N en la secuencia medida (NO teorema general) | `EJECUTADO` (+8.9259%, +4.5047%, +2.2524%, +1.1262% @N=256/512/1024/2048; cocientes ≈1.98/2.00/2.00) |
| Nyquist mixto y espacio nulo en rfft2 | `VERIFIED` (N par: cuatro bins nulos; N impar: DC únicamente; modo mixto D>0 recuperado) |
| Porcentajes log / discreto reconciliados desde los valores registrados | `VERIFIED` (+9.2639%, +2.2524%, +1.1262%; presentación +9.26%, +2.25%, +1.13%) |
| Numerador conj(dx)·p̂+conj(dy)·q̂ (no conj de la suma) | `VERIFIED` (código correcto; §2.1 corregido; pin por modo único; mutación M2) |
| Gate fail-closed estructural (presencia incondicional) | `VERIFIED` (7 passed; runner ausente → 1 failed sin env; 5 mutaciones detectadas) |
| λ\*riesgo ≈ c/N² observado (c_A≈0.085, c_B≈0.146), ganancia <0.06 % examinada | `EJECUTADO` (REV 5 rewording; sustituye `RETIRADO` 3.15e-10) |
| Gate automatizado (runner en repo + recibos congelados) | `VERIFIED` (7 passed; 5 mutaciones detectadas, §8.4) |
| Mallas rectangulares | FUERA DE CONTRATO (a resolver para el producto) |
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

### 8.3 Ronda 5 — revisión de la REV 4 (commit `5c473d5`; esta adjudicación)

| Afirmación (P2) | Veredicto | Verificación independiente de esta parte / acción |
|---|---|---|
| **P2-1: los pesos de Parseval fallan para N impar** — `w[:,-1]=1` asume columna Nyquist; en N impar la última columna tiene espejo no almacenado y debe pesar 2. Sus ratios (5²/7²/33²): 0.7073/0.8459/0.9733. | **CORRECTA — aceptada** | Reproducido con otros campos aleatorios: 0.7274/0.7627/0.9759 (mismo defecto, misma dirección). Fórmula general implementada (col.0=1; Nyquist=1 sólo si par): exacta a máquina para N∈{3,5,7,8,32,33,64}. Recibo selftest #6 + gate. Rectangulares: FUERA de contrato, declarado |
| **P2-2: λ\*≈c/N² es observación, no teorema; y "ganancia ≤0.05 %" era falsa** (128² rég. B = 0.05308 %). | **CORRECTA — aceptada** | Confirmado contra mi propio Apéndice B (−5.308e-04). Rewording: "escalado observado bajo el prior declarado" y "<0.06 % en los escenarios examinados" (§4.2, §7) |
| **P2-3: falta gate automatizado en el repo** (runner sólo inline en Markdown); además un comentario del runner seguía hablando de "cuatro esquinas". | **CORRECTA — aceptada** | `scripts/run_lambda_study_m6.py` (canónico) + `tests/test_native_parallax_m6_lambda_gate.py` (4 tests, <1 s, valores dorados ±5e-9). Verificado por MUTACIÓN: pesos v4 reinyectados → 2/4 tests fallan; restaurado → 4/4. Sobre el comentario: era la nota-historial intencional del docstring, pero se movió al doc (§2.3) y el código quedó sólo con la regla vigente |
| Extensión independiente del reviewer: curva λ=0 rég. B @512² = 0.011314910; óptimos 128² A/B coinciden con REV 4 | **CONCURRENTE** | Mi curva @512² = **0.011314910** — coincidencia a 9 dígitos; óptimos 128² (5.17e-06 / 8.89e-06) ya en REV 4 |

**Dictamen de la revisión (ronda 5):** favorable para continuar; no declarar aún
`M6_MATH_CONTRACT_CLOSED` global. Tabla de alcance aceptada: fórmula de riesgo (pares),
Nyquist por eje, Parseval (pares→toda paridad en REV 5), λ=0 baseline → aceptables;
impares/rectangulares → fuera de contrato (impares ahora soportados); WLS/gamma y
generador `_p.dds` → pendientes; cierre global → no declarar. Próximo paso acordado:
revisión de congelamiento independiente del núcleo FFT periódico, y luego el camino
completo normal → altura → DDS → Skyrim. Sin merge a main; `M6_IMPLEMENTATION_BLOCKED`
vigente.

Ningún punto rechazado en cinco rondas.

### 8.4 Ronda 6 — revisión de la REV 5 sobre #738 (session NP-M6-REV6-ASYMPTOTIC-RECONCILIATION; esta adjudicación)

| Afirmación | Veredicto | Verificación independiente de esta parte / acción (RED→GREEN) |
|---|---|---|
| **P1: el límite asintótico era incorrecto** — (σ²/2π)·ln(π/√λ) no es el límite exacto; el candidato correcto es `V∞(λ)=(σ²/(2π)²)∫∫ k²/(k²+λ)²` con referencia 0.006698379 @λ=1e-3, σ=0.1 (insumo de falsificación, no autoridad). | **CORRECTA — aceptada** | Dos vías independientes de `analytic_curve`: (i) disco en forma cerrada + esquinas GL (m=200/400/800, Δ=2e-12) → **0.006698379** — su referencia confirmada al dígito; (ii) extrapolación discreta 3 puntos (V∞+a/N+b/N²) → **0.006698378**. REV 6 publicó porcentajes originales incorrectos; la errata aritmética y los valores corregidos se conservan en §8.5. La convergencia observada ~1/N no se eleva a teorema general. RED: `test_limite_continuo_V_infinito` fallaba con `AttributeError: noise_mse_limit` sobre el runner REV 5; GREEN: `noise_mse_limit()` (mismo oráculo disco+esquinas), recibos #5 reescritos con separación discreto/límite/log/error-finito; §4.1 reescrita; "−6.4 % por corte finito" `RETIRADO`. Valores dorados REV 5 intactos (ninguna medición tocada) |
| **§3: conjugación del numerador** — `conj(dx)·p̂+conj(dy)·q̂`, no la conjugación de la suma completa; contrastar doc §2.1/§2.2/`solve_screened`/ecuaciones normales; tests con campos conjugado-simétricos. | **CORRECTA — adoptada** | Código correcto desde siempre (`num = conj(dx)·fp + conj(dy)·fq`; §2.2 consistente; ecuaciones normales LS por modo lo exigen). Defecto REAL: el TEXTO §2.1 `(dx·p̂+dy·q̂)*` — corregido a notación explícita. Pin nuevo `test_conjugacion_numerador_por_modo` (modo único p=sin(3·2πx/N) → h=−cos(3·2πx)/k, RMSE<1e-10; convención errónea → signo invertido, RMSE>1). Mutación M2 (conj de la suma): 2 failed |
| **§4: gate fail-closed en CI** — verificar si el workflow fija la variable; si no, exigencia explícita verificable; prueba negativa de runner ausente. | **CORRECTA — aceptada** | `grep` en `.github/workflows/*`: la variable NO se fija en ningún job → ruta compatible REV 5 con runner ausente = `5 skipped` (falso verde posible — demostrado). Solución estructural sin tocar CI global: test de presencia INCONDICIONAL (`test_runner_canonico_presente`). Negativa demostrada: runner eliminado, SIN env → `1 failed, 6 skipped` (antes: `5 skipped`). Variable eliminada por superada; H4 conserva la suya con justificación propia (árboles sin API) |

**Metodología registrada (§5 del mandato):** FASE A reprodujo REV 5 sin cambios
(gate 7→7, runner 9/9, dorados intactos) · FASE B capturó el RED · FASE C construyó
el oráculo independiente (disco+esquinas GL y extrapolación discreta, sin reutilizar
`analytic_curve`) · FASE D aplicó el parche mínimo (una función + recibo #5 + gate)
· FASE E ejecutó 5 mutaciones (log-como-límite: 2 failed; conjugación: 2 failed;
pesos v4: 2 failed; runner ausente sin env: 1 failed; tolerancia relajada con curva
corrupta +1e-4: 7 passed = falso verde demostrado → la tolerancia ±5e-9 es
load-bearing); todas restauradas (md5 verificado) · FASE F: este doc.

**Alcance restante para el cierre global** (sin cambios): ponderación WLS/gamma
(`NOT_DERIVED`), generador `_p.dds` (no validado), revisión de congelamiento
independiente del núcleo FFT periódico. `M6_MATH_CONTRACT_CLOSED=NO` ·
`M6_IMPLEMENTATION_BLOCKED=YES` · sin merge.

### 8.5 REV 6.1 adjudicación (2026-10-16; alcance acotado)

```
REV_6_1_NYQUIST_NULLSPACE: CORRECTED
REV_6_1_NUMERIC_PERCENTAGES: RECONCILED
FFT_SOLVER_BEHAVIOR: UNCHANGED_IF_CORRECT
GOLDEN_VALUES_REV5_REV6: PRESERVED
```

**P2-1 — Nyquist mixto y espacio nulo.** La frase de REV 6 que agrupaba
"los bins con D=0 — incluidos los Nyquist mixtos" era demasiado amplia. Para N
par, en `rfft2` los cuatro y sólo cuatro bins nulos son `(ky=0, kx=0)`,
`(ky=0, kx=N/2)`, `(ky=N/2, kx=0)` y `(ky=N/2, kx=N/2)`. En N impar no hay
frecuencia Nyquist exacta; queda sólo `(ky=0, kx=0)` como media constante.
Un bin mixto con `kx=N/2`, `ky=3` tiene `dx=0`, `dy=i·6π/64` y
`D=(6π/64)²>0`; su altura es observable mediante `q`.

El contraejemplo independiente usa `h(x,y)=(-1)^x sin(6πy/64)`, con
`p=0` y `q=(-1)^x(6π/64)cos(6πy/64)`. El oráculo analítico fija los símbolos
`dx=0`, `dy=i·6π/64`; el test compara la reconstrucción con `h`, además de
probar el caso simétrico (Nyquist en Y), el operador completo frente a derivadas
FFT independientes y los bins nulos pares/impares. En el SHA de referencia la
conducta ya era correcta: clasificación `ALREADY_GREEN`; no se modificó el
solver y el RED matemático se demuestra por mutación deliberada, no se fabrica.

**P2-2 — Errata porcentual con procedencia.** Los valores numéricos registrados
(V∞ `0.006698379`; log `0.007318911`; N=1024 `0.006849252`; N=2048
`0.006773816`) y las anclas doradas no se alteran. Los cocientes se recalculan
como `100·(valor−V∞)/V∞`; el cálculo de cuatro decimales y el redondeo de dos
decimales quedan separados en el gate.

| REV 6 publicó | Reconciliación REV 6.1 desde los valores registrados |
|---|---|
| +9.22 % para la aproximación log | +9.2639% ≈ +9.26% |
| +2.21 % para N=1024 | +2.2524% ≈ +2.25% |
| +1.09 % para N=2048 | +1.1262% ≈ +1.13% |
| ratios de exceso 1.99 / 2.02 / 2.04 | 1.98 / 2.00 / 2.00 al recalcular desde los nueve decimales impresos |

Las cifras anteriores se conservan aquí como errata fechada de la publicación
REV 6, no como valores vigentes. El exceso observado en N=256/512/1024/2048 es
compatible con ~1/N en esta secuencia, pero no se ha demostrado como teorema
general. La afirmación de que las líneas excepcionales de Nyquist tienen medida
nula sólo justifica su desaparición en el límite cuando λ>0 fija el integrando:
`fλ(t)=t/(t+λ)²≤1/(4λ)`, y hay O(N) muestras excepcionales frente a N² totales.
No implica D=0 en todos los bins de esas líneas.

**RED→GREEN, recibos y alcance.** Baseline reproducible en el SHA remoto
`e98b2c883667af64e97d2c89191cd4ed6e1a25fd`: gate REV 6 **7 passed**; runner
`--check` **9/9**. Antes de corregir documentación, los tres oráculos matemáticos
independientes y Decimal pasaron, mientras el guard documental dio **1 failed**
por ausencia de adjudicación: solver `ALREADY_GREEN`, documentación RED real.
Tras la corrección: gate M6 **11 passed**, `--check` **9/9** y snapshot del
Apéndice A idéntico al runner. Ruff check/format: **PASS** (882 archivos);
Mypy `scripts/`: **PASS** (3 archivos, con nota de secciones de config no usadas
por el alcance acotado); regresión Native Parallax sintética: **159 passed**;
clean-room: **123 passed**.

Mutaciones aisladas en copias scratch, todas detectadas: columna Nyquist X
eliminada **2 failed**; fila Nyquist Y eliminada **2 failed**; denominador sin
`dy` **2 failed**; porcentaje incorrecto reintroducido en el texto vigente
**1 failed**; afirmación documental que vuelve a clasificar Nyquist mixto como
nulo **1 failed**. No se modificó el solver ni se retunearon constantes; las
mutaciones no tocaron el worktree. `GOLDEN_VALUES_REV5_REV6: PRESERVED`.
WLS/gamma, DDS, corpus, mallas rectangulares y cierre global siguen fuera de
alcance. `M6_MATH_CONTRACT_CLOSED=NO` · `M6_IMPLEMENTATION_BLOCKED=YES` ·
sin merge.

### 8.6 REV 7.3 adjudicación (2026-10-17; reconciliación CR-1..CR-4 de PR #738)

```
REV_7_3_CR4_BIAS_PER_REALIZATION: CORRECTED
REV_7_3_CR3_NOISE_LIMIT_SCOPED_TO_B: CORRECTED
REV_7_3_CR1_B3_SNIPPET_EXECUTABLE: CORRECTED
REV_7_3_CR2_SCALING_LABEL: CORRECTED
REV_7_3_NOISE_GOLDENS: PRESERVED
REV_7_3_SOLVER_AND_D_SYMBOLS: UNCHANGED
```

**CR-4 (P2, el único sustantivo) — sesgo por realización.** `analytic_curve`
calculaba el sesgo de señal como el cociente de esperanzas
`Σ u_o·E[v_o]/Σ v_o·E[v_o]`; el valor correcto del experimento
(`make_signal`+`solve_screened` con un campo de ruido por realización) es
`E[Σ u_o·E_o/Σ v_o·E_o]` con `E_o=|z_o|²~Exp(1)` iid por órbita ±k. El anidamiento
de la esperanza dentro del cociente no es un detalle: el modelo previo
sobreestimaba el sesgo ≈7.4% (β=2) / ≈4.8% (β=4) y arrastraba λ* ~7% bajo
(5.17e-06 vs 5.58e-06 en N=128, β=2).
Implementado por la identidad de la transformada de Laplace con cuadratura
Gauss–Legendre (m=600, márgenes (36,16); convergencia medida 1e-10) y validado
end-to-end contra MC de 400 realizaciones (`|z|≤1.4`; recibo #10 del runner:
MC(400)/exacto = 0.9976 con el modelo exacto; con el modelo previo el mismo
recibo daría MC/modelo ≈ 0.93 —previo/exacto ≈ 1.074—). λ*
corregidos: λ\*_A (N=128, β=2, σ=0.1) = 5.58e-06 y λ\*_B = 9.61e-06; `c = λ*·N²`
pasa a c_A ≈ 0.0914–0.0918 y c_B ≈ 0.1574–0.1578. La ganancia esperada sigue
**< 0.06 %** (máx. 0.0573 % @128² rég. B): ningún veredicto de riesgo cambia. Los
λ* publicados en REV 4–7.2 (5.17e-06 / 8.89e-06; c_A ≈ 0.0846–0.0849,
c_B ≈ 0.1457–0.1459) quedan **superseded** (§4.2, B.3).

**CR-3 — scoping del límite de ruido.** `noise_mse_limit` (V∞) es el límite del
régimen B (espectro completo, λ>0 fijo). El régimen A (ruido filtrado a la banda)
converge al disco r≤π/4: `V_A(1e-3)=0.004319357` ≠ `V∞(1e-3)=0.006698379`.
Distinción documentada en §4.1 y en los docstrings; sin cambios de fórmula ni de
dorados.

**CR-1 — snippet B.3 ejecutable.** El bloque era un fragmento `python -i` que no
definía `lambda_star_continuous` (NameError al pegarlo). Ahora importa explícito
desde `scripts/` y el gate lo **ejecuta** en subproceso real comparando sus 8
salidas contra los valores congelados (rtol 2e-3).

**CR-2 — etiqueta de escalado.** La etiqueta declaraba el cociente invertido
(λ*/N² en lugar de c = λ*·N², pues λ* decrece con N); la correcta es
**c = λ*·N²**, con los rangos corregidos del modelo exacto.

**RED→GREEN y recibos.** Baseline REV 7.2 (`408914d2`): los 8 tests nuevos dieron
**8 failed + 23 passed** (la equivalencia coarse/refine ya pasaba: invariante de
contrato, no de valor). Tras implementar el modelo exacto en el runner: gate
**31 passed**; `--check` **10/10** en 0.5 s (recibo nuevo, tolerancia medida
0.96–1.04); estudio completo (29 líneas) en 4.2–4.8 s; snippet B.3 ejecutado como
receta real (rc=0, 2.8 s). Ruff check/format y mypy: PASS.

**Mutaciones adversariales** (todas restauradas, md5 verificado; no tocan el
worktree): cociente de esperanzas reintroducido → **5 failed** (oráculo MC
end-to-end, cuadratura-vs-MC de órbitas, recibo #10, snippet B.3, snapshot del
Apéndice A) · multiplicidad por bins → **4 failed** · cuadratura con 40 nodos →
**4 failed** · sin refinamiento → **3 failed** · snippet sin import → **1 failed**
· etiqueta invertida → **1 failed** · §4.1 sin V_A(λ) → **1 failed** · runner
ausente → **9 failed, 17 skipped** (sin verde silencioso).

`GOLDEN_VALUES_NOISE: PRESERVED`. Solver FFT, símbolos D, pesos Parseval y
dorados de ruido: sin cambios. WLS/gamma, DDS, corpus y cierre global siguen
fuera de alcance. `M6_MATH_CONTRACT_CLOSED=NO` · `M6_IMPLEMENTATION_BLOCKED=YES` ·
sin merge.

## Apéndice A — Runner ejecutable (snapshot de `scripts/run_lambda_study_m6.py`, NumPy puro)

**Canónico:** `scripts/run_lambda_study_m6.py` (REV 7.3). El bloque inferior es un
SNAPSHOT de este commit — el gate (`tests/test_native_parallax_m6_lambda_gate.py`)
ejecuta siempre el archivo canónico. 10 recibos vía `--check`; estudio completo sin
argumentos (28 celdas). Recibos: contraejemplo Nyquist (4.8e-16), espacio nulo del
tablero, ceguera de la métrica de pendientes ante altura nula (P1-3), MC
multi-resolución de espectro completo vs curva (N=32/64/128), acotamiento λ=1e-3 vs
asíntota, Parseval toda paridad (P2-1), regresión de exactitud integrable, y sesgo
por realización vs MC con el modelo exacto (CR-4, REV 7.3).

```python
"""Estudio sintético de derivación de screen_lambda para EXP-M6 — v6.1.

REV 6.1 (2026-10-16): reconciliación documental de Nyquist mixto y porcentajes.
El solver y sus símbolos no cambian: el test independiente confirma que los
modos mixtos siguen siendo recuperables y que sólo los bins con ambos símbolos
nulos pertenecen al espacio nulo. Los porcentajes se recalculan desde los
valores numéricos registrados; la tendencia finita ~1/N es observada, no un
teorema general.

REV 7.2 (2026-10-16, sesión NP-M6-REV7.2-FINAL-VALIDATION-FIX): tres defectos de
validación. P2-REV72-01: `make_signal` rechaza explícitamente la banda vacía
(N<8) en vez de normalizar 0/0; el solver FFT, sus símbolos y los valores
dorados quedan intactos. P2-REV72-02: guardas documentales por ubicación
semántica (introducción activa y §4.1) en el gate. P2-REV72-03: verificación
estructural del snapshot del Apéndice A contra este archivo.

REV 7.3 (2026-10-17, sesión NP-M6-REV7.3-CODERABBIT-RECONCILIATION): el término
de SESGO de `analytic_curve` pasa del cociente de esperanzas Σu·E[v]/Σv·E[v]
(modelo previo, sobreestimaba ≈7.4% β=2 / ≈4.8% β=4) al modelo EXACTO por
realización E[Σu_o·E_o/Σv_o·E_o] con E_o=|z_o|²~Exp(1) iid por órbita ±k,
evaluado por cuadratura Gauss-Legendre (m=600, márgenes (36,16); convergencia
medida a 1e-10). La ruta de RUIDO (include_signal=False), el solver, los
símbolos D y los valores dorados de ruido quedan intactos. `noise_mse_limit`
queda scoped como límite del régimen B (V∞); el régimen A (filtrado) converge a
V_A (disco r≤π/4). CR-1: snippet B.3 ejecutable; CR-2: c=λ*·N² corregido;
recibo nuevo de sesgo por realización vs MC (CR-4).

REV 5 (2026-10-15, ronda 5): reconciliación acotada tras la revisión de la REV 4.
Cambios sobre v4 (commit 5c473d5), tres P2 aceptados:

1. PESOS DE PARSEVAL GENERALES (P2-1): la regla de columnas de v4 asumía N par
   (w[:, -1] = 1). La regla general —col. 0 peso 1; col. Nyquist peso 1 SÓLO si
   N es par; resto 2— es exacta para toda paridad (brute force contra fft2,
   N∈{3,5,7,8,32,33,64}; recibo de selftest). Mallas CUADRADAS; rectangulares
   FUERA de contrato (la extensión por eje-x es directa, no se usa aquí).
2. REWORDING λ* (P2-2): el escalado λ* ≈ c/N² es una OBSERVACIÓN bajo el prior
   declarado (no un teorema); la ganancia esperada es < 0.06 % en los escenarios
   examinados (máx. 0.053 % @128² régimen B; el enunciado "≤ 0.05 %" era falso).
3. GATE AUTOMATIZADO (P2-3): este archivo es el runner canónico en el repo;
   tests/test_native_parallax_m6_lambda_gate.py congela los recibos numéricos
   (SEED=20261015) y falla ante cualquier regresión de ponderación espectral,
   de símbolos d o de la curva E(λ).

Historial (detalle en docs/.../exp-m6-screen-lambda-derivation-proposal.md):
v3 (db5e5bd7) corrigió el defecto Nyquist del solver (contraejemplo RMSE
0.707107 → 4.8e-16) pero su curva analítica usaba una fórmula de dispersión y
pesos incorrectos. v4 (5c473d5) reconstruyó la curva con los operadores D del
solver y la regla de pesos rfft2 correcta para N par (valores λ=0 rég. B:
0.006794/0.007955/0.009087/0.010205 @N=32/64/128/256, σ=0.1 — verificados por
dos partes independientes al dígito). v5: runner canónico. v6.1: esta reconciliación documental; solver intacto.
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
def _d_symbols(n: int) -> tuple[np.ndarray, np.ndarray]:
    kx = (2 * np.pi * np.fft.rfftfreq(n))[None, :]
    ky = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    dx, dy = 1j * kx, 1j * ky
    if n % 2 == 0:
        dx[:, -1] = 0.0
        dy[n // 2, :] = 0.0
    return dx, dy


def solve_screened(p: np.ndarray, q: np.ndarray, lam: float = 0.0) -> np.ndarray:
    n = p.shape[0]
    dx, dy = _d_symbols(n)
    fp, fq = np.fft.rfft2(p), np.fft.rfft2(q)
    num = np.conj(dx) * fp + np.conj(dy) * fq
    num[0, 0] = 0.0
    denom = np.abs(dx) ** 2 + np.abs(dy) ** 2 + lam
    denom[denom == 0.0] = 1.0  # espacio nulo declarado: num ya es 0 ahí
    return np.fft.irfft2(num / denom, s=(n, n))


def _parseval_weights(n: int) -> np.ndarray:
    """Pesos de Parseval en la grilla rfft2 de una señal 1-D de largo n
    (shape (n, n//2+1); filas = ky completo, columnas = kx):
      - columna kx=0: completamente materializada (es su propio espejo) → 1
      - columna kx=n/2 (Nyquist, SÓLO si n es par): espejo en la MISMA
        columna ((-n/2) ≡ n/2 mod n) → 1
      - resto: bin propio + espejo no almacenado n-kx → 2
    Exacta para CUALQUIER paridad (verificado brute force contra fft2,
    N∈{3,5,7,8,32,33,64}; recibos del selftest). CONTRATO: mallas CUADRADAS;
    rectangulares FUERA de contrato (el espejo de rfft2 es sólo eje-x, la
    extensión es directa pero no se usa en este estudio)."""
    w = np.full((n, n // 2 + 1), 2.0)
    w[:, 0] = 1.0
    if n % 2 == 0:
        w[:, n // 2] = 1.0
    return w


def _D_matrix(n: int) -> np.ndarray:
    dx, dy = _d_symbols(n)
    d_mat: np.ndarray = np.abs(dx) ** 2 + np.abs(dy) ** 2
    return d_mat


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
    w = _parseval_weights(n)  # pesos corregidos: sin ellos el numerador
    ep, eq = w * np.abs(fp) ** 2, w * np.abs(fq) ** 2  # (bins frontera) no es
    tot = float(np.sum(ep) + np.sum(eq)) + 1e-300  # comparable al denominador
    return float((np.sum(ep[null]) + np.sum(eq[null])) / tot)


# ---------------------------------------------------------------------------
# Modelo declarado
# ---------------------------------------------------------------------------
def make_signal(n: int, beta: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Señal sintética de banda [2π/N, π/4] sin Nyquist estricto, normalizada a
    RMS(altura)=1.

    REV 7.2 (P2-REV72-01): el menor kn no nulo de la grilla es 2π/N y la banda
    exige kn ≤ π/4, de modo que la banda es VACÍA para N < 8. Antes se
    normalizaba igual y `h /= 0` producía NaN silencioso en toda la señal. Ahora
    la banda vacía se rechaza explícitamente y también se valida la energía
    pre-normalización (una amplitud desbordada daría inf/inf en lugar de fallar).

    Dominio GEOMÉTRICO mínimo: N ≥ 8. Eso NO implica que N=8 esté calibrado
    para producción: el dominio experimental validado es el de `main()`."""
    idx = np.fft.fftfreq(n) * n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2 + KY**2) * (2 * np.pi / n)
    nyq = (np.abs(KX) == n // 2) | (np.abs(KY) == n // 2)
    band = (kn >= 2 * np.pi / n) & (kn <= np.pi / 4) & ~nyq
    if not band.any():
        raise ValueError(
            f"N={n} produce banda [2π/N, π/4] vacía; se requiere N>=8 para señal no vacía. "
            "Normalizar esa señal daría 0/0 (NaN)."
        )
    coef = rng.standard_normal((n, n)) + 1j * rng.standard_normal((n, n))
    amp = np.zeros((n, n))
    amp[band] = kn[band] ** (-(beta + 2) / 2)
    h_hat = coef * amp
    h_hat[0, 0] = 0.0
    h = np.real(np.fft.ifft2(h_hat))
    energia = float(np.mean(h**2))
    if not np.isfinite(energia) or energia <= 0.0:
        raise ValueError(
            f"N={n}: la energía de la señal pre-normalización es {energia!r} (nula o no "
            "finita); normalizar produciría valores no finitos en lugar de fallar."
        )
    h /= np.sqrt(energia)
    kxf = (2 * np.pi * np.fft.fftfreq(n))[None, :]
    kyf = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    hn = np.fft.fft2(h)
    hn[0, 0] = 0.0
    return h, np.real(np.fft.ifft2(1j * kxf * hn)), np.real(np.fft.ifft2(1j * kyf * hn))


def q8_noise(p: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Q8 SINTÉTICO: cuantización a 8 bits del mapa de normales. NO equivale a
    normales authored reales (eso es EXP-M2)."""
    nz = np.ones_like(p)
    nx, ny = -p, -q
    nrm = np.sqrt(nx**2 + ny**2 + nz**2)
    nx, ny, nz = nx / nrm, ny / nrm, nz / nrm
    dec_x = np.rint((nx * 0.5 + 0.5) * 255) / 255 * 2 - 1
    dec_y = np.rint((ny * 0.5 + 0.5) * 255) / 255 * 2 - 1
    return -dec_x / nz, -dec_y / nz


def drift_noise(p: np.ndarray, q: np.ndarray, sigma: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
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
    mask: np.ndarray = (kn >= 2 * np.pi / n) & (kn <= np.pi / 4) & ~nyq
    return mask


# ---------------------------------------------------------------------------
# Modelo EXACTO del término de sesgo (REV 7.3, CR-4)
# ---------------------------------------------------------------------------
_BIAS_PREP_CACHE: dict[tuple[int, float, int, float, float], dict[str, np.ndarray]] = {}


def _orbit_classes(n: int) -> tuple[np.ndarray, np.ndarray]:
    """Radios únicos kn de la banda [2π/N, π/4] sin líneas Nyquist y
    multiplicidad de ÓRBITAS ±k (cada órbita es el par de bins {k, −k}; en banda
    no hay bins auto-espejo, así que la cuenta de bins por radio es par).

    El sesgo exacto agrupa por radio: en banda D = kn² y v_o, D_o dependen sólo
    del radio; las E_o = |z_o|² ~ Exp(1) iid de cada órbita son idénticas en ley."""
    idx = np.fft.fftfreq(n) * n
    kx, ky = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(kx**2 + ky**2) * (2 * np.pi / n)
    nyq = (np.abs(kx) == n // 2) | (np.abs(ky) == n // 2)
    band = (kn >= 2 * np.pi / n) & (kn <= np.pi / 4) & ~nyq
    vals, counts = np.unique(kn[band], return_counts=True)
    if np.any(counts % 2 != 0):
        raise ValueError("hay bins auto-espejo en la banda (imposible con el reflejo de la grilla FFT)")
    return vals, counts // 2


def _bias_exact_prep(
    n: int,
    beta: float,
    m_nodes: int = 600,
    margen_lo: float = 36.0,
    margen_hi: float = 16.0,
) -> dict[str, np.ndarray]:
    """Precómputo cached de la cuadratura de E[X/Y] (REV 7.3, CR-4).

    Nodos Gauss-Legendre en u=ln s sobre [−ln v_max − margen_lo,
    −ln v_min + margen_hi]: el integrando cae como 1/s² fuera del rango útil y
    el margen medido (36,16) está convergido a 1e-10 (nodos m=600 vs 1200).
    P(u) = Π_o (1+s·v_o)^{−mult_o} por suma de logs (estable); K(u,o) =
    1/(1+s·v_o); v_o = 2·kn_o^{−(β+2)} por órbita."""
    clave = (n, float(beta), int(m_nodes), float(margen_lo), float(margen_hi))
    cached = _BIAS_PREP_CACHE.get(clave)
    if cached is not None:
        return cached
    radios, mult = _orbit_classes(n)
    v = 2.0 * radios ** (-(beta + 2.0))
    if not bool(np.all(np.isfinite(v))):
        raise ValueError(
            f"N={n} β={beta}: la energía espectral es no finita; el modelo exacto de sesgo "
            "no está definido (mismo dominio que make_signal)."
        )
    u_lo = -float(np.log(v.max())) - margen_lo
    u_hi = -float(np.log(v.min())) + margen_hi
    x, wx = np.polynomial.legendre.leggauss(m_nodes)
    u = (u_hi - u_lo) / 2 * x + (u_hi + u_lo) / 2
    wu = (u_hi - u_lo) / 2 * wx
    s = np.exp(u)
    prep: dict[str, np.ndarray] = {
        "v": v,
        "d": radios**2,
        "mult": mult,
        "u": u,
        "wu": wu,
        "prod": np.exp(-(mult[None, :] * np.log1p(s[:, None] * v[None, :])).sum(axis=1)),
        "kern": 1.0 / (1.0 + s[:, None] * v[None, :]),
    }
    _BIAS_PREP_CACHE[clave] = prep
    return prep


def _bias_exact_batch(
    n: int,
    beta: float,
    lams: np.ndarray | tuple[float, ...] | list[float],
    m_nodes: int = 600,
    margen_lo: float = 36.0,
    margen_hi: float = 16.0,
) -> np.ndarray:
    """E[MSE_sesgo(λ)] EXACTO por realización (REV 7.3, CR-4):

        MSE(λ, realización) = Σ_o u_o(λ)·E_o / Σ_o v_o·E_o,  E_o = |z_o|²~Exp(1) iid

    con v_o = 2·kn_o^{−(β+2)} y u_o = v_o·(λ/(kn_o²+λ))² por órbita. La esperanza
    es E[X/Y] con X, Y correlacionados; se calcula por la identidad de la
    transformada de Laplace (E_o~Exp(1) iid, s≥0):

        E[X/Y] = ∫₀^∞ Π_o (1+s·v_o)^{−1} · Σ_o u_o·(1+s·v_o)^{−1} ds.

    NO es el cociente de esperanzas Σu·E[v]/Σv·E[v] del modelo previo: ese
    sobreestima el sesgo ≈7.4% (β=2) / ≈4.8% (β=4) y arrastraba λ* ~7% bajo
    (5.17e-06 vs 5.58e-06 en N=128, β=2)."""
    prep = _bias_exact_prep(n, beta, m_nodes=m_nodes, margen_lo=margen_lo, margen_hi=margen_hi)
    lams_arr = np.atleast_1d(np.asarray(lams, dtype=float))
    B = (lams_arr[:, None] / (prep["d"][None, :] + lams_arr[:, None])) ** 2
    Q = prep["kern"] @ (prep["mult"][None, :] * prep["v"][None, :] * B).T
    integ = (np.exp(prep["u"]) * prep["prod"])[:, None] * Q
    resultado: np.ndarray = prep["wu"] @ integ
    return resultado


# ---------------------------------------------------------------------------
# Curva analítica CORREGIDA: D(k) de los símbolos del solver
# ---------------------------------------------------------------------------
def analytic_curve(
    n: int,
    beta: float,
    sigma: float,
    lams: np.ndarray | tuple[float, ...] | list[float],
    noise_filtered: bool,
    include_signal: bool = True,
) -> np.ndarray:
    """E[MSE(λ)] = E[ Σ_o u_o(λ)·E_o / Σ_o v_o·E_o ]   [sesgo de señal: esperanza
                 POR REALIZACIÓN con E_o~Exp(1) iid por órbita — REV 7.3, CR-4]
                 + (1/N²)·Σ_rfft w·(D/(D+λ)²)·σ²        [ruido]
    La función devuelve RMSE = sqrt(MSE esperada).
    Transmisión de señal T(D,λ)=D/(D+λ); error de reconstrucción
    H_est−H = −λ/(D+λ)·H; factor cuadrático de sesgo B(D,λ)=(λ/(D+λ))².
    El término de ruido permanece D/(D+λ)².
    El sesgo exacto (v_o = 2·kn^{−(β+2)} por órbita, u_o = v_o·B(kn²,λ)) reemplaza
    al cociente de esperanzas previo, que sobreestimaba ≈7.4% β=2 / ≈4.8% β=4.
    La fórmula usa D(k)=|dx|²+|dy|² de los MISMOS símbolos del solver (P1-1) y
    pesos de Parseval de _parseval_weights (exacto para toda paridad).
    Regímenes de RUIDO (λ>0 fijo): A — ruido sólo en banda (noise_filtered=True):
    converge a V_A(λ), el límite del disco r≤π/4 (forma cerrada; V_A(λ=1e-3,
    σ=0.1)=0.004319357); B — ruido blanco en cada bin con D>0
    (noise_filtered=False): converge a V∞(λ) = noise_mse_limit (cuadrado
    completo; V∞(1e-3, σ=0.1)=0.006698379). La fórmula log sólo es dominante
    cuando λ→0.
    En N par, un bin de Nyquist mixto puede tener D>0; D=0 requiere que ambos
    símbolos sean nulos. Con λ=0, el ruido blanco crece como (σ²/2π)·ln N
    (P1-2). include_signal=False expone sólo el término de ruido (MC puro)."""
    if n < 1:
        raise ValueError(f"N debe ser >= 1, recibido {n}")
    kxr = (2 * np.pi * np.fft.rfftfreq(n))[None, :]
    kyr = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    k = np.sqrt(kxr**2 + kyr**2)
    band = (k >= 2 * np.pi / n) & (k <= np.pi / 4)  # π/4 < π/2: sin Nyquist
    if include_signal and not band.any():
        raise ValueError(
            f"N={n} produce banda [2π/N, π/4] vacía; se requiere N>=8 para señal no vacía. "
            "Use include_signal=False para modos de sólo ruido."
        )
    D = _D_matrix(n)
    w = _parseval_weights(n)
    bias_exacto: np.ndarray | None = None
    if include_signal:
        # sesgo EXACTO por realización (REV 7.3, CR-4): E[Σ u_o E_o / Σ v_o E_o]
        bias_exacto = _bias_exact_batch(n, beta, lams)
    out = []
    for i_lam, lam in enumerate(lams):
        mse = 0.0
        if include_signal:
            assert bias_exacto is not None
            mse += float(bias_exacto[i_lam])
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


def noise_mse_limit(lam: float, sigma: float, m: int = 300) -> float:
    """Límite continuo EXACTO del término de ruido del régimen B (espectro
    completo) con λ>0 fijo (REV 6, P1 de la ronda 6; scoping por régimen: REV 7.3,
    CR-3). NO es el límite del régimen A: con ruido filtrado a la banda la suma
    converge a V_A(λ) —disco r≤π/4, forma cerrada; V_A(1e-3)=0.004319357 vs
    V∞(1e-3)=0.006698379 con σ=0.1 (ver analytic_curve)—:

        V∞(λ) = (σ²/(2π)²)·∫∫_{[-π,π]²} k²/(k²+λ)² dkx dky,

    derivado de la suma discreta (1/N²)·Σ w·D/(D+λ)² → integral en el límite
    N→∞. Para λ>0, fλ(t)=t/(t+λ)² es continua y está acotada por 1/(4λ).
    En N par, D sólo difiere de kx²+ky² en las líneas Nyquist: hay O(N) bins
    excepcionales y su contribución normalizada es O(1/N); los demás términos
    son sumas de Riemann de un integrando continuo. Las líneas tienen medida
    nula, pero sus bins mixtos no son, en general, espacio nulo (ver §4.1).
    Evaluación INDEPENDIENTE de analytic_curve: disco r≤π en forma cerrada
    (polar) + 4 esquinas por Gauss-Legendre anidado (estable a 2e-12 desde
    m=200).

    Errata REV 6.1, con los valores registrados (σ=0.1, λ=1e-3): la
    aproximación log 0.007318911 supera V∞=0.006698379 en +9.2639% (~+9.26%);
    MSE discreto N=1024, 0.006849252, en +2.2524% (~+2.25%); N=2048,
    0.006773816, en +1.1262% (~+1.13%). El comportamiento finito ~1/N se
    observa en estos tamaños; no se afirma como teorema general. λ>0 acota
    el ruido, pero NO recupera la señal atenuada (el sesgo (λ/(D+λ))² persiste)."""
    x, wx = np.polynomial.legendre.leggauss(m)
    y, wy = np.polynomial.legendre.leggauss(m)
    xn = np.pi * (x + 1) / 2
    wxn = (np.pi / 2) * wx
    esquinas = 0.0
    for xi, wi in zip(xn, wxn, strict=False):
        y0 = float(np.sqrt(max(np.pi**2 - xi**2, 0.0)))
        yy = (y + 1) / 2 * (np.pi - y0) + y0
        ww = ((np.pi - y0) / 2) * wy
        k2 = xi**2 + yy**2
        esquinas += wi * float(np.sum(ww * k2 / (k2 + lam) ** 2))
    disco = np.pi * (np.log((np.pi**2 + lam) / lam) + lam / (np.pi**2 + lam) - 1.0)
    integral: float = float(disco + 4.0 * esquinas)
    return (sigma**2 / (2 * np.pi) ** 2) * integral


def lambda_star_continuous(
    n: int, beta: float, sigma: float, noise_filtered: bool = True
) -> tuple[float, float, float]:
    """λ* de E(λ) con el sesgo EXACTO (REV 7.3): malla geométrica gruesa
    (200 pts en [1e-12, 1]) + refinamiento local (80 pts × 3 rondas alrededor
    del argmin vecinal). La malla densa previa (20000 pts) es inviable con la
    cuadratura por evaluación; equivalencia con malla densa de 4000 pts
    verificada en el gate (tolerancia 5e-3 relativa)."""
    grid = np.geomspace(1e-12, 1.0, 200)
    e = analytic_curve(n, beta, sigma, grid, noise_filtered)
    i = int(np.argmin(e))
    lo, hi = float(grid[max(i - 1, 0)]), float(grid[min(i + 1, len(grid) - 1)])
    lstar = float(grid[i])
    for _ in range(3):
        loc = np.geomspace(lo, hi, 80)
        el = analytic_curve(n, beta, sigma, loc, noise_filtered)
        j = int(np.argmin(el))
        lstar = float(loc[j])
        lo, hi = float(loc[max(j - 1, 0)]), float(loc[min(j + 1, len(loc) - 1)])
    e_star = float(analytic_curve(n, beta, sigma, [lstar], noise_filtered)[0])
    e0 = float(analytic_curve(n, beta, sigma, [0.0], noise_filtered)[0])
    return lstar, e_star, e0


# ---------------------------------------------------------------------------
# Recibos (--check)
# ---------------------------------------------------------------------------
def selftest() -> int:
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
    print(
        "[ok] h=(-1)^(x+y): pendientes≡0 ⇒ fracción pendiente-nula=0 — altura "
        "perdida UNKNOWN (requiere oráculo; separado en el manifest)"
    )
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
        pred = analytic_curve(n, 2.0, 0.1, [0.0], noise_filtered=False, include_signal=False)[0] ** 2
        ratio = float(np.mean(mses)) / pred
        assert 0.93 < ratio < 1.07, (n, ratio)
        print(f"[ok] MC(100, N={n}, espectro completo) vs curva D: ratio={ratio:.4f}")
        ok += 1

    # 5. P1-2: λ fijo acota el ruido; λ=0 diverge con N (fórmula y números).
    lam = 1e-3
    vals = [
        analytic_curve(n, 2.0, 0.1, [lam], noise_filtered=False, include_signal=False)[0] ** 2
        for n in (128, 256, 512, 1024)
    ]
    assert vals[-1] < vals[1], vals  # decrece hacia V∞
    v_inf = noise_mse_limit(lam, 0.1)
    assert abs(v_inf - 0.006698379) < 5e-9, v_inf  # límite continuo (2 vías, ronda 6)
    assert abs(vals[-1] - v_inf) / v_inf < 0.03, (vals[-1], v_inf)  # exceso +2.25% @1024²
    asymp_log = 0.1**2 / (2 * np.pi) * np.log(np.pi / np.sqrt(lam))
    assert abs(asymp_log - v_inf) / v_inf > 0.05, (asymp_log, v_inf)  # log ≠ límite
    lam0_1024 = analytic_curve(1024, 2.0, 0.1, [0.0], noise_filtered=False, include_signal=False)[0] ** 2
    print(
        f"[ok] λ=1e-3: V∞(λ)={v_inf:.9f} es el límite continuo exacto; discreto "
        f"1024²={vals[-1]:.9f} (+{100 * (vals[-1] - v_inf) / v_inf:.2f}%, exceso ∝1/N); "
        f"la fórmula log σ²/(2π)·ln(π/√λ)={asymp_log:.9f} NO es el límite "
        f"(+{100 * (asymp_log - v_inf) / v_inf:.1f}%); λ=0 en 1024²: {lam0_1024:.6f}"
    )
    ok += 1

    # 6. Parseval rfft2 exacto para par E impar (P2-1, ronda 5): contra fft2.
    rngp = np.random.default_rng(9)
    for np_ in (4, 5, 8, 33, 64):
        hh = rngp.standard_normal((np_, np_))
        real = float(np.sum(np.abs(np.fft.fft2(hh)) ** 2))
        mía = float(np.sum(_parseval_weights(np_) * np.abs(np.fft.rfft2(hh)) ** 2))
        assert np.isclose(mía, real, rtol=1e-12), (np_, mía / real)
    print("[ok] Parseval rfft2 exacto (brute force vs fft2) N∈{4,5,8,33,64}")
    ok += 1

    # 7. Exactitud integrable sin Nyquist (regresión del solver).
    n = 128
    h2, p2, q2 = make_signal(n, 2.0, np.random.default_rng(1))
    e = float(np.sqrt(np.mean((solve_screened(p2, q2, 0.0) - h2) ** 2)))
    assert e < 1e-12, e
    print(f"[ok] campo integrable sin Nyquist: RMSE={e:.2e}")
    ok += 1

    # 8. REV 7.3 (CR-4): el sesgo por realización medido con make_signal +
    #    solve_screened contra el modelo EXACTO E[ΣuE/ΣvE] (cuadratura). Con el
    #    cociente de esperanzas previo (previo/exacto≈1.074) el recibo daría
    #    MC/previo≈0.93, fuera de la cota.
    n_b, beta_b, lam_b = 64, 2.0, 1e-3
    rng_b = np.random.default_rng(20261016)
    vals_b = np.empty(400)
    for i_b in range(400):
        h_b, p_b, q_b = make_signal(n_b, beta_b, rng_b)
        e_b = solve_screened(p_b, q_b, lam_b) - h_b
        vals_b[i_b] = float(np.mean(e_b**2))
    ratio_b = float(vals_b.mean()) / float(_bias_exact_batch(n_b, beta_b, [lam_b])[0])
    assert 0.96 < ratio_b < 1.04, ratio_b
    print(f"[ok] sesgo por realización (CR-4) N=64 β=2 λ=1e-3: MC(400)/exacto={ratio_b:.4f} (previo/exacto≈1.074)")
    ok += 1

    print(f"selftest: {ok}/10 recibos OK")
    return ok


def main() -> None:
    if "--check" in sys.argv:
        selftest()
        return
    rng = np.random.default_rng(SEED)
    print(
        f"SEED={SEED} · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · "
        f"curva con operadores D del solver v3 + pesos Parseval rfft2 (col.0/Nyquist=1, resto=2) · "
        f"regímenes A(filtrado)/B(espectro completo) separados · mallas CUADRADAS (toda paridad; rectangulares fuera de contrato)"
    )
    for n in N_VALUES:
        for beta in BETAS:
            h, p, q = make_signal(n, beta, rng)
            lam_star_A, e_star_A, e0_A = lambda_star_continuous(n, beta, 0.1, True)
            lam_star_B, e_star_B, e0_B = lambda_star_continuous(n, beta, 0.1, False)
            casos = {
                **{
                    f"gauss σ={s}": (p + s * rng.standard_normal(p.shape), q + s * rng.standard_normal(p.shape), s)
                    for s in SIGMAS
                },
                "Q8 sintético": (*q8_noise(p, q), None),
                "limpio": (p, q, None),
                "driftLF σ=.05": (*drift_noise(p, q, 0.05, rng), 0.05),
                "driftLF σ=.5": (*drift_noise(p, q, 0.5, rng), 0.5),
            }
            for label, (pn, qn, sigma) in casos.items():
                sigma_eff = sigma if sigma is not None else float(np.sqrt(np.mean((pn - p) ** 2 + (qn - q) ** 2) / 2))
                rmses = [float(np.sqrt(np.mean((solve_screened(pn, qn, lam) - h) ** 2))) for lam in LAMBDAS]
                i_opt = int(np.argmin(rmses))
                e_f = analytic_curve(n, beta, sigma_eff, LAMBDAS, True)
                e_u = analytic_curve(n, beta, sigma_eff, LAMBDAS, False)
                print(
                    f"N={n:4d} β={beta:.0f} {label:13s} σ_eff={sigma_eff:.4f} | "
                    f"λ_opt_emp={LAMBDAS[i_opt]:g} (RMSE {rmses[i_opt]:.4f}) "
                    f"λ=0: {rmses[0]:.4f} (×{rmses[0] / rmses[i_opt]:.3f}) | "
                    f"E λ* A(filtr)={LAMBDAS[int(np.argmin(e_f))]:g} "
                    f"B(compl)={LAMBDAS[int(np.argmin(e_u))]:g} | "
                    f"λ*riesgo A(σ=.1)={lam_star_A:.1e} B={lam_star_B:.1e}"
                )


if __name__ == "__main__":
    main()
```

## Apéndice B — Salida registrada (SEED=20261015)

**B.1 `--check` (selftest, 10 recibos):**

```
[ok] contraejemplo Nyquist-X recuperado por derivada-Y: RMSE=4.79e-16
[ok] tablero (pendientes en bin nulo): std=0.0e+00
[ok] h=(-1)^(x+y): pendientes≡0 ⇒ fracción pendiente-nula=0 — altura perdida UNKNOWN (requiere oráculo; separado en el manifest)
[ok] MC(100, N=32, espectro completo) vs curva D: ratio=1.0087
[ok] MC(100, N=64, espectro completo) vs curva D: ratio=0.9952
[ok] MC(100, N=128, espectro completo) vs curva D: ratio=0.9796
[ok] λ=1e-3: V∞(λ)=0.006698379 es el límite continuo exacto; discreto 1024²=0.006849252 (+2.25%, exceso ∝1/N); la fórmula log σ²/(2π)·ln(π/√λ)=0.007318911 NO es el límite (+9.3%); λ=0 en 1024²: 0.012422
[ok] Parseval rfft2 exacto (brute force vs fft2) N∈{4,5,8,33,64}
[ok] campo integrable sin Nyquist: RMSE=3.59e-16
[ok] sesgo por realización (CR-4) N=64 β=2 λ=1e-3: MC(400)/exacto=0.9976 (previo/exacto≈1.074)
selftest: 10/10 recibos OK
```

(Los RMSE ~e-16 de los recibos 1 y 9 son ruido de redondeo dependiente del
BLAS/plataforma; los valores anclados del gate son los dorados de ruido y V∞.)

**B.2 Estudio completo (28 celdas; columnas empíricas y de grilla idénticas a
REV 4–7.2 — sólo cambian las columnas `λ*riesgo` al sesgo exacto REV 7.3):**

```
SEED=20261015 · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · curva con operadores D del solver v3 + pesos Parseval rfft2 (col.0/Nyquist=1, resto=2) · regímenes A(filtrado)/B(espectro completo) separados · mallas CUADRADAS (toda paridad; rectangulares fuera de contrato)
N= 256 β=2 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0099) λ=0: 0.0099 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.4e-06 B=2.4e-06
N= 256 β=2 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0327) λ=0: 0.0327 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.4e-06 B=2.4e-06
N= 256 β=2 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=0 (RMSE 0.0962) λ=0: 0.0962 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.4e-06 B=2.4e-06
N= 256 β=2 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0023) λ=0: 0.0023 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.4e-06 B=2.4e-06
N= 256 β=2 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.4e-06 B=2.4e-06
N= 256 β=2 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=3e-05 (RMSE 0.3024) λ=0: 0.3065 (×1.014) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.4e-06 B=2.4e-06
N= 256 β=2 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0040) λ=0: 3.2132 (×3.200) | E λ* A(filtr)=3e-05 B(compl)=3e-05 | λ*riesgo A(σ=.1)=1.4e-06 B=2.4e-06
N= 256 β=4 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0096) λ=0: 0.0096 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.1e-06 B=1.9e-06
N= 256 β=4 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0303) λ=0: 0.0303 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.1e-06 B=1.9e-06
N= 256 β=4 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=0 (RMSE 0.0999) λ=0: 0.0999 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.1e-06 B=1.9e-06
N= 256 β=4 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0079) λ=0: 0.0079 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.1e-06 B=1.9e-06
N= 256 β=4 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.1e-06 B=1.9e-06
N= 256 β=4 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=0 (RMSE 0.3058) λ=0: 0.3058 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=1.1e-06 B=1.9e-06
N= 256 β=4 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0053) λ=0: 3.2801 (×3.263) | E λ* A(filtr)=3e-05 B(compl)=3e-05 | λ*riesgo A(σ=.1)=1.1e-06 B=1.9e-06
N= 128 β=2 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0101) λ=0: 0.0101 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.6e-06 B=9.6e-06
N= 128 β=2 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0296) λ=0: 0.0296 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.6e-06 B=9.6e-06
N= 128 β=2 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=3e-05 (RMSE 0.1026) λ=0: 0.1033 (×1.007) | E λ* A(filtr)=1e-05 B(compl)=1e-05 | λ*riesgo A(σ=.1)=5.6e-06 B=9.6e-06
N= 128 β=2 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0020) λ=0: 0.0020 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.6e-06 B=9.6e-06
N= 128 β=2 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.6e-06 B=9.6e-06
N= 128 β=2 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=0.0001 (RMSE 0.1586) λ=0: 0.1600 (×1.009) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=5.6e-06 B=9.6e-06
N= 128 β=2 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0083) λ=0: 1.6611 (×1.647) | E λ* A(filtr)=0.0001 B(compl)=0.0003 | λ*riesgo A(σ=.1)=5.6e-06 B=9.6e-06
N= 128 β=4 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0087) λ=0: 0.0087 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.4e-06 B=7.5e-06
N= 128 β=4 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0302) λ=0: 0.0302 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.4e-06 B=7.5e-06
N= 128 β=4 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=3e-05 (RMSE 0.0889) λ=0: 0.0893 (×1.004) | E λ* A(filtr)=0 B(compl)=1e-05 | λ*riesgo A(σ=.1)=4.4e-06 B=7.5e-06
N= 128 β=4 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0021) λ=0: 0.0021 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.4e-06 B=7.5e-06
N= 128 β=4 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.4e-06 B=7.5e-06
N= 128 β=4 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=3e-05 (RMSE 0.1642) λ=0: 0.1647 (×1.003) | E λ* A(filtr)=0 B(compl)=0 | λ*riesgo A(σ=.1)=4.4e-06 B=7.5e-06
N= 128 β=4 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0131) λ=0: 1.6493 (×1.628) | E λ* A(filtr)=0.0001 B(compl)=0.0001 | λ*riesgo A(σ=.1)=4.4e-06 B=7.5e-06
```

**B.3 Barrido λ\* ≈ c/N² (β=2, σ=0.1; reproduce §4.2; REV 7.3: sesgo exacto):**

```python
# Ejecutable desde la raíz del repo (scripts/ es un paquete con __init__.py):
from scripts.run_lambda_study_m6 import lambda_star_continuous

for n in (128, 256, 512, 1024):
    for filt in (True, False):
        lam, e_star, e0 = lambda_star_continuous(n, 2.0, 0.1, filt)
        print(n, "A" if filt else "B", f"lambda*={lam:.3e} ganancia={e_star / e0 - 1:+.3e}")
```

```
128 A lambda*=5.579e-06 ganancia=-3.478e-04   128 B lambda*=9.606e-06 ganancia=-5.734e-04
256 A lambda*=1.399e-06 ganancia=-2.867e-04   256 B lambda*=2.406e-06 ganancia=-5.115e-04
512 A lambda*=3.501e-07 ganancia=-2.435e-04   512 B lambda*=6.017e-07 ganancia=-4.615e-04
1024 A lambda*=8.755e-08 ganancia=-2.114e-04  1024 B lambda*=1.505e-07 ganancia=-4.204e-04
c = λ*·N² : c_A ∈ [0.0914, 0.0918]; c_B ∈ [0.1574, 0.1578]  (px²)  [escalado observado; REV 7.3]
```

**B.4 Gate (CI):** `pytest tests/test_native_parallax_m6_lambda_gate.py --noconftest -q`
→ **31 passed** (<5 s). Runner ausente (prueba negativa, SIN variables de entorno):
`9 failed, 5 passed, 17 skipped` — job rojo por el test de presencia incondicional
(REV 6; antes REV 6.1: `1 failed, 6 skipped` sobre 7 tests). Mutaciones adversariales
REV 7.3 (todas restauradas, md5 verificado): cociente de esperanzas reintroducido
(la regresión CR-4) → **5 failed** · multiplicidad por bins en vez de órbitas →
**4 failed** · cuadratura subconvergida (40 nodos) → **4 failed** · barrido sin
refinamiento → **3 failed** · snippet B.3 sin import → **1 failed** · etiqueta
invertida reintroducida → **1 failed** · §4.1 sin `V_A(λ)` → **1 failed**. Toda
mutación del runner tumba además el snapshot del Apéndice A (segunda línea de
defensa por igualdad). Histórico REV 6.1: log-como-límite → 2 failed · conjugación
errónea → 2 failed · pesos v4 → 2 failed · runner ausente → 1 failed · tolerancia
relajada (1e-3) con curva corrupta (+1e-4) → 7 passed = falso verde demostrado ⇒ la
tolerancia dorada ±5e-9 es load-bearing.

**B.5 Límite continuo V∞(λ=1e-3, σ=0.1) — dos vías independientes (REV 6):**

```
Vía 1 — disco en forma cerrada + 4 esquinas, Gauss-Legendre anidado:
  m=200 : 0.006698379225
  m=400 : 0.006698379241     (Δ(200→400) = 1.6e-11)
  m=800 : 0.006698379243     (Δ(400→800) = 2.0e-12)
  → V∞ = 0.006698379   [coincide con la referencia de la revisión al dígito]

Vía 2 — extrapolación de la suma discreta (v = V∞ + a/N + b/N²; N=512/1024/2048):
  V∞ = 0.006698378   (a=0.1545, b=−3.5e-3)  [acuerdo 1e-9 con la Vía 1]

Convergencia discreta (analytic_curve, λ=1e-3, sólo-ruido):
  N= 256: 0.007296271  (exceso +8.9259% ≈ +8.93%)
  N= 512: 0.007000119  (exceso +4.5047% ≈ +4.50%)
  N=1024: 0.006849252  (exceso +2.2524% ≈ +2.25%)   [ancla REV 5, intacta]
  N=2048: 0.006773816  (exceso +1.1262% ≈ +1.13%)   ratios recalculados: 1.98 / 2.00 / 2.00 ≈ 1/N

Referencias del mismo λ:
  aproximación log (σ²/2π)·ln(π/√λ) = 0.007318911  → +9.2639% ≈ +9.26% sobre V∞ (NO es el límite)
  valor discreto @1024²             = 0.006849252  → +2.2524% ≈ +2.25% (converge a V∞)
```
