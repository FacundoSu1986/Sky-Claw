# EXP-M6 — Propuesta de derivación de `screen_lambda` (y estatus de `gamma`) — REV 3

> **Estado:** `PROPOSAL` — insumo externo para la ETAPA A del prereg de EXP-M6
> ([PR #675](https://github.com/FacundoSu1986/Sky-Claw/pull/675), DRAFT). **No autoriza
> implementación ni ejecución de M6** (`M6_IMPLEMENTATION_BLOCKED = YES` vigente).
>
> **REV 3 (2026-10-15):** corrige la REV 2 (commit `63e75a0e`) tras su revisión externa.
> Cambio principal — **defecto matemático real confirmado en el solver**: la máscara de
> Nyquist por bins completos eliminaba altura RECUPERABLE (contraejemplo
> `h=(-1)^x·sin(6πy/64)`: RMSE 0.707107 → 4.8e-16 con símbolos coherentes; verificado
> por ambas partes al dígito). Nuevos: spec formal de operadores (§2.2), autotest con
> 5 recibos ejecutables desde el commit (Apéndice A `--check`), regímenes de ruido
> separados formalmente (§4.1), métricas esperado-vs-empírico separadas (§4.2),
> modo de contrato obligatorio del harness H4 (§6.1), WLS: gauge provisional +
> diseño proyectado (§6.2). Adjudicación completa en §8.
>
> **Historial:** REV 1 (`8a95f84a`) — fórmula de ruido `2N²σ²` (incorrecta), "Q8 real"
> (mal etiquetado), runner incompleto. REV 2 — fórmula corregida `k²N²σ²` (proyección
> ortogonal), λ\* continuo reconocido, gauge relativo, runner completo. REV 3 — esta.
>
> **Sin corpus:** todo deriva de matemática pública + controles sintéticos propios.
> Calibrar con corpus real o históricos sigue PROHIBIDO (§18.3, R7).

## 0. Leyenda de estados

`VERIFIED` (ejecutado aquí, reproducible del Apéndice A) · `EJECUTADO` (medido, semilla
registrada) · `INFERRED` (deducido de fuentes públicas) · `HYPOTHESIS` · `INGENIERÍA` ·
`NOT_DERIVED` · `REAL_RIG_REQUIRED`.

## 1. Objetivo y alcance

Cerrar —con rigor de prereg— el default de `screen_lambda` y el estatus del ponderador
`gamma`, **antes** de que EXP-M6 congele thresholds. Fuera de alcance: `T_R`/`T_N`/
`T_AMBIG`, política de nz (EXP-M1), semántica final del slot `_p` (`REAL_RIG_REQUIRED`).

## 2. Teoría (fuentes públicas)

### 2.1 Screened Frankot–Chellappa como Tikhonov

Minimizar `Σ‖∇h − g‖² + λ‖h‖²` en el toro con derivadas espectrales da
`ĥ = (dx·p̂ + dy·q̂)* / (|dx|²+|dy|²+λ)` (solución LS por modo), con dos consecuencias
exactas: atenuación de amplitud `k²/(k²+λ)` (modo de corte `k_c=√λ`, `VERIFIED`) y el
término de ruido por **proyección ortogonal** `E|num(k)|² = k²·N²σ²` (la REV 1
sobrecontaba ×2; corregido en REV 2 y verificado por MC: ratio 1.0055, Apéndice B).

### 2.2 Spec de operadores: qué es observable en Nyquist (NUEVO en REV 3)

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
bins donde **ambos** ejes carecen de derivada: `(N/2,·)` con `ky ∈ {0, N/2}` y sus
simétricos (p. ej. el tablero de ajedrez). Un bin con Nyquist en X pero `ky` genérico
**conserva** la altura: se recupera por la derivada en Y.

**El defecto de la REV 2 (confirmado por contraejemplo).** La máscara por bins
completos (`mask[:, -1]=True; mask[n//2,:]=True`) eliminaba la columna/fila ENTERA:
para `h(x,y) = (-1)^x·sin(6πy/64)` (64×64; Nyquist en X, modo 3 en Y) tiraba el bin
`(N/2, 3)` cuya altura es perfectamente recuperable vía `q̂`. Medición (ambas partes
independientes, coincidente):

| Solver | RMS salida | RMSE vs h |
|---|---|---|
| REV 2 (máscara de bins) | 0.000000 | **0.707107** (= RMS(h): salida plana) |
| Símbolos coherentes (§2.2) | 0.707107 | **4.8e-16** |

Regresiones verificadas de la corrección: campo integrable sin Nyquist sigue exacto
(3.0e-16); el tablero sigue declarado nulo (std=0, energía nula=1.0); el costo es
idéntico (misma FFT). `VERIFIED`.

## 3. Procedimiento cerrado de derivación (estilo R7)

Sin cambios desde REV 1: prior declarado → curva `E(λ)` → λ\* → controles sintéticos
(misma semilla que congelará el prereg) → freeze en ETAPA B → prohibido retunear.
Desacuerdo analítico/empírico ⇒ re-declarar el modelo. `CORPUS_CALIBRATION=PROHIBIDO`
· `THRESHOLD_FIXED_HERE=NO`.

## 4. Experimento propio [`EJECUTADO`; runner Apéndice A, salida Apéndice B]

Setup: `SEED=20261015` · `N∈{256,128}` · `β∈{2,4}` · banda `[2π/N, π/4]` sin Nyquist
estricto · RMS(altura)=1 · ruido: blanco `σ∈{0.01,0.03,0.1}`, Q8 sintético, deriva LF
(`k≤16·2π/N`, `σ∈{0.05,0.5}`) · λ∈{0…1} (12 puntos) · solver v3 (símbolos d).

### 4.1 Regímenes de ruido — separación formal (cierra la objeción de la revisión)

Son dos experimentos distintos y así se reportan:

- **Régimen A — "ruido filtrado en banda"** (el modelo declarado): la curva analítica
  y el MC se computan con el ruido restringido a la banda. Fórmula vs MC(200):
  ratio **1.0055** (`VERIFIED`).
- **Régimen B — "ruido sin filtrar"** (lo que el solver recibe con ruido blanco real):
  el λ=0 pasa todos los modos con peso `1/k²` ⇒ `E[MSE(0)] = (σ²/N²)·Σ_{k≠0} 1/k²`
  (divergencia logarítmica de FC; medido: ratio fórmula/MC = 1.0014 en la sesión de la
  REV 2). Con este régimen ningún λ constante detiene la divergencia: la herramienta
  correcta es filtrar las pendientes de entrada, no subir λ (`INFERRED`, a falsar en
  #675). La tabla imprime ambas curvas con etiqueta explícita.

### 4.2 Métricas separadas: riesgo esperado vs realización (cierra la objeción)

La revisión externa tiene razón: **no es lo mismo** el λ\* del riesgo esperado que el
argmin empírico de una realización. Con la fórmula corregida:

- **Riesgo esperado** (barrido continuo de 20 000 puntos, en `main()` del runner):
  `λ* ∈ [2e-11, 3.2e-10]` bajo este prior; para (N=128, β=2, σ=0.1):
  **λ\* = 3.1e-10** (coincide con el cálculo independiente de la revisión: 3.15e-10),
  con **ganancia esperada ×1.00000** (≤ 0.06 %).
- **Empírico** (argmin de 12 puntos sobre UNA realización, Apéndice B): λ_opt=0 en
  26/28 celdas, ganancia ≤ ×1.006 — es ruido de muestra alrededor de una diferencia
  esperada nula, no evidencia de optimalidad.

Enunciado defendible: **λ=0 es el baseline correcto bajo este prior (el óptimo de
riesgo esperado le gana <0.1 %); `λ=1e-3` sigue sin ninguna justificación** (su sesgo
en banda baja cuesta más que todo el ruido que suprime). `EJECUTADO`.

### 4.3 Tabla (28 celdas; volcado completo Apéndice B)

| Escenario | λ_opt empírico | Ganancia | Nota |
|---|---|---|---|
| Blanco σ=0.01–0.1 (12) | **0** (10/12) | ≤ ×1.006 | Régimen B aplica al experimento |
| Q8 sintético (4) | **0** (4/4) | ×1.000 | σ_eff≈0.0023 |
| Limpio (4) | **0** | — | ~2.5e-16 |
| Deriva LF σ=0.05 (4) | 0–1e-4 | ≤ ×1.014 | |
| Deriva LF σ=0.5 (4) | 1 (borde de rejilla) | ×1.63–3.26 | RMSE(λ\*)≈1.0: SNR≈1, sin señal útil |

## 5. Recomendación para el prereg de EXP-M6 (REV 3)

1. **Solver spec:** símbolos `dx, dy` con anulación SÓLO en el eje Nyquist de cada
   eje (§2.2); denominador compuesto de los MISMOS símbolos; espacio nulo declarado y
   medido (`nyquist_energy_fraction` sobre el null set REAL). Prohibida la máscara
   por bins de la REV 2.
2. **`screen_lambda = 0`** como default del camino espectral (baseline, §4.2). λ>0
   reservado a: (a) gauge del WLS (§5.2), (b) re-derivación si el prior cambia por §3.
3. **Gauge WLS (corregido dos veces):** el absoluto `1e-6` atenúa 29.8 % el
   fundamental a 4096 (objeción REV-2-review, confirmada); el relativo
   `1e-4·k_min²` es **provisional** (`INGENIERÍA`) — la revisión REV-2-review tiene
   razón en que regularizar ∝ identidad igualmente toca modos no-constantes. El
   diseño correcto a especificar en #675: **CG proyectado al subespacio de media
   cero** (restricción explícita Σh=0, sin regularización de gauge), más detección de
   regiones desconectadas cuando los pesos cortan conexiones (si no, el sistema
   proyectado sigue singular por componentes). `NOT_DERIVED` hasta su spec numérica.
4. **`gamma`:** forma cerrada (pesos en ecuaciones normales, nunca multiplicativos —
   contraejemplo ×3.93); valor `NOT_DERIVED` (EXP-M1); default sin ponderar.
5. **Nyquist:** energía en el espacio nulo viaja en el manifest; nada silencioso.

## 6. Estado del harness H4 (insumo #723) y del gauge — decisiones REV 3

### 6.1 Modo contrato obligatorio (cierra la ambigüedad remanente)

El harness (`tests/test_native_parallax_h4_contract_adversarial.py`) ahora tiene dos
modos, seleccionados por entorno — la distinción que la revisión pidió:

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
(§5.3) es el diseño requerido antes de cualquier congelamiento M6.

## 7. Estados (REV 3)

| Objeto | Estado |
|---|---|
| Operadores `dx,dy` con Nyquist por eje + LS coherente | `VERIFIED` (contraejemplo 4.8e-16; regresiones OK) |
| Máscara por bins de REV 2 | **RETIRADA** (defecto confirmado) |
| Fórmula de ruido `k²N²σ²` | `VERIFIED` (MC 1.0055 en banda; 1.0014 sin filtrar) |
| λ\* riesgo esperado ≤0.06 % vs λ=0 | `EJECUTADO` (3.1e-10 verificado por 2ª parte) |
| λ=0 default (baseline) | `SUPPORTED_BY` §4 |
| Gauge WLS relativo | `INGENIERÍA` provisional; proyectado-CG `NOT_DERIVED` |
| Pesos en ec. normales | `VERIFIED` (×3.93 contraejemplo multiplicativo) |
| `gamma` | `NOT_DERIVED` (EXP-M1) |
| Semántica `_p` | `REAL_RIG_REQUIRED` |
| Freeze | `PENDIENTE ETAPA B de #675` |

## 8. Adjudicación de la revisión externa de la REV 2 (2026-10-15)

| Afirmación | Veredicto | Evidencia / acción |
|---|---|---|
| **Nyquist: la máscara elimina frecuencias reconstruibles** (contraejemplo `(-1)^x·sin(6πy/64)`) | **CORRECTA — aceptada** | Reproducido: RMSE 0.707107 con el solver commiteado; corrección por símbolos coherentes → **4.804e-16** (la revisión midió 4.8e-16). Spec §2.2 + solver v3 + autotest #1 |
| "Definir símbolos diferenciales y pseudoinversa de manera coherente, anulando sólo lo no observable" | **CORRECTA — adoptada como spec** | §2.2 es literalmente esa definición; espacio nulo = ambos ejes enfermos |
| MC(400) y barrido de 20k sin código en el commit; `lambda_star_continuous()` no invocada; `verify_critic_claims` no incluido | **CORRECTA — aceptada** | `--check` del Apéndice A ejecuta MC, barrido continuo (invocado en `main()`), contraejemplo y asserts; salida en Apéndice B |
| Comparación analítica(filtrado) vs empírico(sin filtrar) necesita separación formal | **CORRECTA — aceptada** | §4.1: regímenes A/B con curvas etiquetadas por separado |
| λ\*≈3.15e-10; no confundir mejora esperada (≤0.06 %) con empírica (≤0.6 % de una realización) | **CORRECTA — aceptada** | §4.2: métricas separadas; mi barrido reprodujo su 3.15e-10 (3.1e-10) |
| Modo de contrato obligatorio por env var para H4 | **CORRECTA — implementada** | §6.1: `SKYCLAW_REQUIRE_H4_CONTRACT=1`; tres estados verificados |
| Gauge relativo "necesita revisión adicional"; WLS debería usar restricción de media cero + detección de componentes | **CORRECTA — aceptada como diseño** | §5.3/§6.2: proyectado-CG `NOT_DERIVED`, gauge relativo degradado a provisional |
| "No reconstruir el módulo v2 todavía; cerrar primero el contrato matemático" | **CONCURRENTE** | No se reconstruyó; los fixes viven en el runner commiteado y este doc |
| 47 tests / MC "declarados, no verificados independientemente" | **CORRECTA como límite** | Comando público de reproducción en §6.1; autotest ahora vive en el commit |

Ningún punto rechazado. Registro de límite: la cifra exacta del contraejemplo de la
revisión (0.707107 / 4.8e-16) coincidió con la medición propia sin ajustes — misma
convención de símbolos espectrales por ambas partes.

## Apéndice A — Runner ejecutable (`run_lambda_study_v3.py`, NumPy puro; `--check` = recibos)

```python
"""Estudio sintético de derivación de screen_lambda para EXP-M6 — v3.
REV 3: solver con símbolos diferenciales coherentes (Nyquist anulado SÓLO por eje),
regímenes de ruido separados, métricas esperado/empírico separadas, --check con
MC(200), contraejemplo Nyquist, exactitud integrable y espacio nulo del tablero."""
from __future__ import annotations
import sys
import numpy as np

N_VALUES = (256, 128); BETAS = (2.0, 4.0); SIGMAS = (0.01, 0.03, 0.1)
LAMBDAS = (0.0, 1e-5, 3e-5, 1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1, 3e-1, 1.0)
SEED = 20261015

def _d_symbols(n):
    """dx=i·kx salvo el eje Nyquist-X (d=0: derivada no observable); ídem dy."""
    kx = (2*np.pi*np.fft.rfftfreq(n))[None, :]
    ky = (2*np.pi*np.fft.fftfreq(n))[:, None]
    dx, dy = 1j*kx, 1j*ky
    if n % 2 == 0:
        dx[:, -1] = 0.0
        dy[n//2, :] = 0.0
    return dx, dy

def solve_screened(p, q, lam=0.0):
    """LS por modo: ĥ=(dx*·p̂+dy*·q̂)/(|dx|²+|dy|²+λ); bins sin derivada observable
    en ambos ejes ⇒ num=0 por construcción (guarda 0/0; espacio nulo declarado)."""
    n = p.shape[0]
    dx, dy = _d_symbols(n)
    fp, fq = np.fft.rfft2(p), np.fft.rfft2(q)
    num = np.conj(dx)*fp + np.conj(dy)*fq
    num[0, 0] = 0.0
    denom = np.abs(dx)**2 + np.abs(dy)**2 + lam
    denom[denom == 0.0] = 1.0
    return np.fft.irfft2(num/denom, s=(n, n))

def nyquist_null_fraction(p, q):
    n = p.shape[0]
    dx, dy = _d_symbols(n)
    null = (dx == 0) & (dy == 0)
    fp, fq = np.fft.rfft2(p), np.fft.rfft2(q)
    tot = float(np.sum(np.abs(fp)**2)+np.sum(np.abs(fq)**2))+1e-300
    return float((np.sum(np.abs(fp[null])**2)+np.sum(np.abs(fq[null])**2))/tot)

def make_signal(n, beta, rng):
    idx = np.fft.fftfreq(n)*n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2+KY**2)*(2*np.pi/n)
    nyq = (np.abs(KX)==n//2) | (np.abs(KY)==n//2)
    band = (kn >= 2*np.pi/n) & (kn <= np.pi/4) & ~nyq
    coef = rng.standard_normal((n, n))+1j*rng.standard_normal((n, n))
    amp = np.zeros((n, n)); amp[band] = kn[band]**(-(beta+2)/2)
    h_hat = coef*amp; h_hat[0, 0] = 0.0
    h = np.real(np.fft.ifft2(h_hat)); h /= np.sqrt(np.mean(h**2))
    kxf = (2*np.pi*np.fft.fftfreq(n))[None, :]; kyf = (2*np.pi*np.fft.fftfreq(n))[:, None]
    hn = np.fft.fft2(h); hn[0, 0] = 0.0
    return h, np.real(np.fft.ifft2(1j*kxf*hn)), np.real(np.fft.ifft2(1j*kyf*hn))

def q8_noise(p, q):
    nz = np.ones_like(p); nx, ny = -p, -q
    nrm = np.sqrt(nx**2+ny**2+nz**2); nx, ny, nz = nx/nrm, ny/nrm, nz/nrm
    dec_x = np.rint((nx*0.5+0.5)*255)/255*2-1
    dec_y = np.rint((ny*0.5+0.5)*255)/255*2-1
    return -dec_x/nz, -dec_y/nz

def drift_noise(p, q, sigma, rng):
    n = p.shape[0]; idx = np.fft.fftfreq(n)*n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2+KY**2)*(2*np.pi/n)
    band = (kn > 0) & (kn <= 16*2*np.pi/n)
    ep = np.real(np.fft.ifft2((rng.standard_normal((n,n))+1j*rng.standard_normal((n,n)))*band))
    eq = np.real(np.fft.ifft2((rng.standard_normal((n,n))+1j*rng.standard_normal((n,n)))*band))
    ep *= sigma/np.sqrt(np.mean(ep**2)); eq *= sigma/np.sqrt(np.mean(eq**2))
    return p+ep, q+eq

def analytic_curve(n, beta, sigma, lams, noise_filtered):
    """E[MSE(λ)]=(1/N⁴)·Σ[λ²E|ĥ|²+k²N²σ²]/(k²+λ)². noise_filtered=False suma el
    ruido en TODO k≠0 (régimen B: lo que el solver realmente recibe)."""
    idx = np.fft.fftfreq(n)*n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    k = np.sqrt(KX**2+KY**2)*(2*np.pi/n); k2 = k**2
    nyq = (np.abs(KX)==n//2) | (np.abs(KY)==n//2)
    band = (k >= 2*np.pi/n) & (k <= np.pi/4) & ~nyq
    amp = np.zeros((n, n)); amp[band] = k[band]**(-(beta+2)/2)
    eh = 2*amp**2; eh *= n**4/eh.sum()
    nz = k2 > 0
    kmask = (band | (nz & ~nyq)) if not noise_filtered else band
    return [float(np.sqrt((((lam/(k2[kmask]+lam))**2*eh[kmask]).sum()
                           + ((k2[kmask]/(k2[kmask]+lam)**2)*sigma**2).sum())/n**2))
            for lam in lams]

def lambda_star_continuous(n, beta, sigma):
    fine = np.geomspace(1e-12, 1.0, 20000)
    e = analytic_curve(n, beta, sigma, fine, True)
    i = int(np.argmin(e)); e0 = analytic_curve(n, beta, sigma, [0.0], True)[0]
    return float(fine[i]), float(e[i]), float(e0)

def _bandpass(field, n):
    idx = np.fft.fftfreq(n)*n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2+KY**2)*(2*np.pi/n)
    nyq = (np.abs(KX)==n//2) | (np.abs(KY)==n//2)
    band = (kn >= 2*np.pi/n) & (kn <= np.pi/4) & ~nyq
    f = np.fft.fft2(field); f[~band] = 0.0
    return np.real(np.fft.ifft2(f))

def selftest():
    ok = 0
    N = 64; x = np.arange(N)[None, :]; y = np.arange(N)[:, None]
    h = (-1.0)**x*np.sin(6*np.pi*y/N)
    p0 = np.zeros_like(h)
    q0 = (-1.0)**x*(6*np.pi/N)*np.cos(6*np.pi*y/N)
    rmse = float(np.sqrt(np.mean((solve_screened(p0, q0)-h)**2)))
    assert rmse < 1e-10, rmse
    print(f"[ok] contraejemplo Nyquist-X recuperado por derivada-Y: RMSE={rmse:.2e}"); ok += 1
    cb = (np.indices((64, 64)).sum(axis=0) % 2)*2-1
    r_cb = solve_screened(cb*0.5, cb*0.0)
    std = float(np.std(r_cb)); assert std < 1e-12, std
    assert nyquist_null_fraction(cb*0.5, cb*0.0) > 0.99
    print(f"[ok] tablero (Nyquist ambos ejes) declarado nulo: std={std:.1e}"); ok += 1
    n = 128
    h2, p2, q2 = make_signal(n, 2.0, np.random.default_rng(1))
    e = float(np.sqrt(np.mean((solve_screened(p2, q2, 0.0)-h2)**2)))
    assert e < 1e-12, e
    print(f"[ok] campo integrable sin Nyquist: RMSE={e:.2e}"); ok += 1
    rng = np.random.default_rng(7); sigma = 0.03; mc = []
    for _ in range(200):
        pe = p2+sigma*rng.standard_normal(p2.shape)
        qe = q2+sigma*rng.standard_normal(q2.shape)
        mc.append(float(np.mean((solve_screened(_bandpass(pe, n), _bandpass(qe, n), 0.0)-h2)**2)))
    ratio = float(np.mean(mc))/analytic_curve(n, 2.0, sigma, [0.0], True)[0]**2
    assert 0.9 < ratio < 1.1, ratio
    print(f"[ok] MC(200, ruido en banda) vs fórmula corregida: ratio={ratio:.4f}"); ok += 1
    lam_star, e_star, e0 = lambda_star_continuous(n, 2.0, 0.1)
    gain = e0/e_star
    assert 0 < (1-1/gain) < 0.01, gain
    print(f"[ok] λ* continuo (N={n}, β=2, σ=0.1): λ*={lam_star:.2e}, ganancia ×{gain:.5f}"); ok += 1
    print(f"selftest: {ok}/5 recibos OK")

def main():
    if "--check" in sys.argv:
        selftest(); return
    rng = np.random.default_rng(SEED)
    print(f"SEED={SEED} · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · "
          f"solver v3 (símbolos d coherentes) · métricas separadas: "
          f"λ*_esperado(riesgo) vs λ_opt_empírico(realización)")
    for n in N_VALUES:
        for beta in BETAS:
            h, p, q = make_signal(n, beta, rng)
            lam_star, e_star, e0 = lambda_star_continuous(n, beta, 0.1)
            casos = {**{f"gauss σ={s}": (p+s*rng.standard_normal(p.shape),
                                         q+s*rng.standard_normal(p.shape), s)
                        for s in SIGMAS},
                     "Q8 sintético": (*q8_noise(p, q), None),
                     "limpio": (p, q, None),
                     "driftLF σ=.05": (*drift_noise(p, q, 0.05, rng), 0.05),
                     "driftLF σ=.5": (*drift_noise(p, q, 0.5, rng), 0.5)}
            for label, (pn, qn, sigma) in casos.items():
                sigma_eff = sigma if sigma is not None else float(
                    np.sqrt(np.mean((pn-p)**2+(qn-q)**2)/2))
                rmses = [float(np.sqrt(np.mean((solve_screened(pn, qn, lam)-h)**2)))
                         for lam in LAMBDAS]
                i_opt = int(np.argmin(rmses))
                e_f = analytic_curve(n, beta, sigma_eff, LAMBDAS, True)
                e_u = analytic_curve(n, beta, sigma_eff, LAMBDAS, False)
                print(f"N={n:4d} β={beta:.0f} {label:13s} σ_eff={sigma_eff:.4f} | "
                      f"λ_opt_emp={LAMBDAS[i_opt]:g} (RMSE {rmses[i_opt]:.4f}) "
                      f"λ=0: {rmses[0]:.4f} (×{rmses[0]/rmses[i_opt]:.3f}) | "
                      f"E[λ*] filtrada={LAMBDAS[int(np.argmin(e_f))]:g} "
                      f"sin filtrar={LAMBDAS[int(np.argmin(e_u))]:g} | "
                      f"λ*_riesgo(σ=.1)={lam_star:.1e} gan.esp.×{e0/e_star:.5f}")

if __name__ == "__main__":
    main()
```

Reproducción: `python run_lambda_study_v3.py --check` (recibos, ~2 s) y
`python run_lambda_study_v3.py` (28 celdas, ~5 s). Los scripts auxiliares de la
sesión (`verify_nyquist_counterexample.py`, diagnóstico de la fórmula de ruido con
descomposición por modo) quedan en el workspace de la conversación; sus resultados
están registrados en §2.2, §4.1 y la revisión los reprodujo de forma independiente.

## Apéndice B — Salida registrada (SEED=20261015)

```
$ python run_lambda_study_v3.py --check
[ok] contraejemplo Nyquist-X recuperado por derivada-Y: RMSE=4.80e-16
[ok] tablero (Nyquist ambos ejes) declarado nulo: std=0.0e+00, energía nula=1.0000
[ok] campo integrable sin Nyquist: RMSE=3.05e-16
[ok] MC(200, ruido en banda) vs fórmula corregida: ratio=1.0055
[ok] λ* continuo (N=128, β=2, σ=0.1): λ*=3.15e-10, ganancia esperada ×1.00000
selftest: 5/5 recibos OK
```

```
$ python run_lambda_study_v3.py
SEED=20261015 · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · solver v3 (símbolos d coherentes) · métricas separadas: λ*_esperado(riesgo) vs λ_opt_empírico(realización)
N= 256 β=2 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0099) λ=0: 0.0099 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=2.0e-11 gan.esp.×1.00000
N= 256 β=2 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0327) λ=0: 0.0327 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=2.0e-11 gan.esp.×1.00000
N= 256 β=2 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=0 (RMSE 0.0962) λ=0: 0.0962 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=2.0e-11 gan.esp.×1.00000
N= 256 β=2 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0023) λ=0: 0.0023 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=2.0e-11 gan.esp.×1.00000
N= 256 β=2 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=2.0e-11 gan.esp.×1.00000
N= 256 β=2 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=3e-05 (RMSE 0.3024) λ=0: 0.3065 (×1.014) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=2.0e-11 gan.esp.×1.00000
N= 256 β=2 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0040) λ=0: 3.2132 (×3.200) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=2.0e-11 gan.esp.×1.00000
N= 256 β=4 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0088) λ=0: 0.0088 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.6e-11 gan.esp.×1.00000
N= 256 β=4 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0275) λ=0: 0.0275 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.6e-11 gan.esp.×1.00000
N= 256 β=4 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=0 (RMSE 0.0939) λ=0: 0.0939 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.6e-11 gan.esp.×1.00000
N= 256 β=4 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0079) λ=0: 0.0079 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.6e-11 gan.esp.×1.00000
N= 256 β=4 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.6e-11 gan.esp.×1.00000
N= 256 β=4 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=0 (RMSE 0.3058) λ=0: 0.3058 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.6e-11 gan.esp.×1.00000
N= 256 β=4 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0053) λ=0: 3.2801 (×3.263) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.6e-11 gan.esp.×1.00000
N= 128 β=2 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0101) λ=0: 0.0101 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=3.1e-10 gan.esp.×1.00000
N= 128 β=2 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0296) λ=0: 0.0296 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=3.1e-10 gan.esp.×1.00000
N= 128 β=2 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=0 (RMSE 0.0965) λ=0: 0.0965 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=3.1e-10 gan.esp.×1.00000
N= 128 β=2 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0020) λ=0: 0.0020 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=3.1e-10 gan.esp.×1.00000
N= 128 β=2 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=3.1e-10 gan.esp.×1.00000
N= 128 β=2 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=0.0001 (RMSE 0.1586) λ=0: 0.1600 (×1.009) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=3.1e-10 gan.esp.×1.00000
N= 128 β=2 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0083) λ=0: 1.6611 (×1.647) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=3.1e-10 gan.esp.×1.00000
N= 128 β=4 gauss σ=0.01  σ_eff=0.0100 | λ_opt_emp=0 (RMSE 0.0082) λ=0: 0.0082 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.9e-11 gan.esp.×1.00000
N= 128 β=4 gauss σ=0.03  σ_eff=0.0300 | λ_opt_emp=0 (RMSE 0.0270) λ=0: 0.0270 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.9e-11 gan.esp.×1.00000
N= 128 β=4 gauss σ=0.1   σ_eff=0.1000 | λ_opt_emp=0 (RMSE 0.0965) λ=0: 0.0965 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.9e-11 gan.esp.×1.00000
N= 128 β=4 Q8 sintético  σ_eff=0.0023 | λ_opt_emp=0 (RMSE 0.0020) λ=0: 0.0020 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.9e-11 gan.esp.×1.00000
N= 128 β=4 limpio        σ_eff=0.0000 | λ_opt_emp=0 (RMSE 0.0000) λ=0: 0.0000 (×1.000) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.9e-11 gan.esp.×1.00000
N= 128 β=4 driftLF σ=.05 σ_eff=0.0500 | λ_opt_emp=3e-05 (RMSE 0.1642) λ=0: 0.1647 (×1.003) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.9e-11 gan.esp.×1.00000
N= 128 β=4 driftLF σ=.5  σ_eff=0.5000 | λ_opt_emp=1 (RMSE 1.0131) λ=0: 1.6493 (×1.628) | E[λ*] filtrada=0 sin filtrar=0 | λ*_riesgo(σ=.1)=1.9e-11 gan.esp.×1.00000
```

Nota: los RMSE absolutos cambian levemente respecto de la REV 2 (p. ej. 0.0090→0.0099
en la primera celda) porque el solver v3 ya no descarta los bins de Nyquist de un solo
eje: pasa su (pequeño) ruido en lugar de tirar también esos bins. Es la diferencia
esperable entre ambos tratamientos y la evidencia de que el cambio es real.
