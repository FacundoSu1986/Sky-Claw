# EXP-M6 — Propuesta de derivación de `screen_lambda` (y estatus de `gamma`) — REV 2

> **Estado:** `PROPOSAL` — insumo externo para la ETAPA A del prereg de EXP-M6
> ([PR #675](https://github.com/FacundoSu1986/Sky-Claw/pull/675), DRAFT). **No autoriza
> implementación ni ejecución de M6** (`M6_IMPLEMENTATION_BLOCKED = YES` vigente).
> **REV 2 (2026-10-15):** corrige la REV 1 (commit `8a95f84a`) tras la revisión externa
> de ese commit. Cambios: fórmula de energía de ruido **corregida** (sobreconteo ×2
> reconocido — §2.1), régimen de fuga fuera de banda medido (§4.3), λ\* continuo con
> ganancia exacta (§4.2, la objeción de la revisión era matemáticamente correcta),
> gauge del WLS corregido de `λ=1e-6` a `λ=1e-4·k_min²` (§5.2, ídem), runner
> ejecutable completo inline (Apéndice A) con salida registrada (Apéndice B), y
> "Q8 real" rebautizado "Q8 sintético". Adjudicación completa en §7.
> **Sin corpus:** todo deriva de matemática pública + controles sintéticos propios.
> Calibrar con corpus real o resultados históricos sigue PROHIBIDO (§18.3, R7).

## 0. Leyenda de estados (taxonomía del repo)

`VERIFIED` (ejecutado aquí, reproducible del Apéndice A) · `EJECUTADO` (medido, semilla
registrada) · `INFERRED` (deducido de fuentes públicas) · `HYPOTHESIS` (sin derivar) ·
`INGENIERÍA` (decisión de implementación) · `NOT_DERIVED` · `REAL_RIG_REQUIRED`.

## 1. Objetivo y alcance

Cerrar —con rigor de prereg— el default de `screen_lambda` (regularización Tikhonov del
integrador espectral) y el estatus del ponderador `gamma`, **antes** de que EXP-M6
congele thresholds. Fuera de alcance: `T_R`/`T_N`/`T_AMBIG`, la política de nz (EXP-M1),
la semántica final del slot `_p` (`REAL_RIG_REQUIRED`).

## 2. Teoría (fuentes públicas)

**Frankot–Chellappa screened como Tikhonov.** El solver espectral periódico que
minimiza `Σ‖∇h − g‖² + λ‖h‖²` en el toro es `ĥ(k) = −(i k·ĝ(k))/(‖k‖² + λ)` (FC puro:
λ=0), con dos consecuencias exactas:

1. **Atenuación de amplitud de altura por modo:** `k²/(k²+λ)`; el modo de corte es
   `k_c = √λ`. `VERIFIED` (medido: 0.9736 vs teórica 0.9747, k=4·2π/128, λ=1e-3).
2. **Término de ruido (CORREGIDO en REV 2).** El residuo de pendientes entra al solver
   por `−ikx·P̂ε − iky·Q̂ε` — proyecciones **ortogonales** de dos ruidos blancos
   independientes de varianza `σ²`. Por isotropía, `E|num(k)|² = (kx²+ky²)·N²σ² =
   k²·N²σ²`, por modo de la grilla FFT completa. La REV 1 escribía `2N²σ²`
   (sobreconteo ×2: sumó las energías de los dos componentes como si proyectaran en la
   misma dirección). La fórmula corregida,

```
E[MSE(λ)] = (1/N⁴) · Σ_k [ (λ/(k²+λ))² · E|ĥ_true(k)|²  +  (k²/(k²+λ)²) · N²σ² ]
```

   reproduce el Monte-Carlo de ruido blanco en banda al **0.14 %** (ratio 1.0014,
   400 realizaciones; §4.3). `VERIFIED`.

**Cuantización Q8 sintética.** Paso `2/255` por canal ⇒ RMS por componente
`(2/255)/√12 ≈ 0.00226` en unidades de normal; en pendientes `σ_p ≈ 0.00226/nz`.
`INFERRED`, consistente con lo medido (`σ_eff = 0.0023`). **No equivale** a normales
authored reales (ese ruido es el objeto de EXP-M2, con 44 % de catastróficos).

## 3. Procedimiento cerrado de derivación (estilo R7)

Igual que REV 1 (sin cambios): prior declarado → curva analítica `E(λ)` → λ\* →
validación con controles sintéticos (misma semilla que congelará el prereg) → freeze
en ETAPA B → prohibido retunear. Desacuerdo analítico/empírico ⇒ el modelo declarado
no describe el ruido ⇒ re-declarar. `CORPUS_CALIBRATION = PROHIBIDO` ·
`THRESHOLD_FIXED_HERE = NO`.

## 4. Experimento propio [`EJECUTADO`; runner completo en Apéndice A, salida en B]

Setup: `SEED=20261015` · `N ∈ {256,128}` · `β ∈ {2,4}` · banda `[2π/N, π/4]` sin
Nyquist estricto · RMS(altura)=1 · ruido: blanco `σ∈{0.01,0.03,0.1}`, **Q8 sintético**,
deriva LF (`k≤16·2π/N`, `σ∈{0.05,0.5}`) · λ ∈ {0, 1e-5 … 1} (12 puntos) · solver
rfft2 screened.

### 4.1 Resultados (28 celdas; volcado completo en Apéndice B)

| Escenario | λ_opt (rejilla) | Ganancia de λ | λ=0 RMSE |
|---|---|---|---|
| Blanco σ=0.01–0.1 (12 celdas) | **0** (10/12; 2 celdas λ=3e-5) | ≤ ×1.006 | 0.008–0.093 |
| **Q8 sintético** (4 celdas) | **0** (4/4) | ×1.000 | 0.002–0.008 |
| Limpio (4 celdas) | **0** | — | ~0 (2.5e-16) |
| Deriva LF σ=0.05 (4 celdas) | 0–1e-4 | ×1.000–1.014 | 0.16–0.31 |
| Deriva LF σ=0.5 (extremo) | 1 (borde de rejilla) | ×1.63–3.26, RMSE(λ\*)≈1.0 (SNR≈1: sin señal útil) | 1.65–3.28 |

### 4.2 λ\* continuo — la objeción de la revisión externa es correcta y aquí está cuantificada

Con ruido no nulo, `R'(0) = −2σ²/k⁴ < 0` modo a modo ⇒ **existe un λ\* > 0 estricto**;
declarar "λ=0 es el óptimo MSE" (REV 1) era una sobreextensión. Con la fórmula
corregida, el minimizador continuo bajo este prior es `λ* ∈ [1e-12, 3e-10]` con
**ganancia ×1.0000–1.0006** (≤ 0.06 %; medido sobre 20 000 puntos log-spaced).
Enunciado defendible: **λ=0 es el argmin de la rejilla, un baseline competitivo, y el
óptimo continuo le gana menos de una décima de por ciento bajo este prior;
`λ=1e-3` sigue sin ninguna justificación** (su sesgo k²/(k²+λ) en la banda baja
cuesta más que todo el ruido que suprime). `EJECUTADO`.

### 4.3 Fuga fuera de banda y verificación Monte-Carlo de la fórmula

El solver λ=0 pasa **todos** los modos con peso `1/k²`: con ruido blanco real de
pendientes (no filtrado), `E[MSE(0)] = (σ²/N²)·Σ_{k≠0} 1/k²` — divergencia
logarítmica con N clásica de FC (p. ej. `Σ=12204` a N=128 vs `8291` sólo en banda).
Medición: MC(400) = `6.7137e-4` vs predicción corregida `6.7040e-4` (ratio 1.0014) y
vs predicción sólo-banda `4.5543e-4` (la fuga explica el resto). Consecuencias:
(a) la fórmula de la REV 1 estaba mal en el factor 2 **y** comparaba regímenes
distintos; (b) con ruido blanco no filtrado, ningún λ constante detiene la divergencia
logarítmica (el kernel sigue siendo ~1/k para k≫√λ): si EXP-M6 declara ruido blanco de
banda ancha, la herramienta correcta es filtrar las pendientes de entrada, no subir λ
(`INFERRED`, a falsar en el diseño de #675). `VERIFIED` (MC) + `INFERRED` (divergencia).

## 5. Recomendación para el prereg de EXP-M6 (REV 2)

1. **`screen_lambda = 0` como default del camino espectral** — enunciado como
   *baseline competitivo con margen ≤ 0.06 % frente al óptimo continuo bajo el prior
   declarado* (§4.2), no como óptimo general. Cualquier cambio de prior ⇒ re-derivar
   por §3.
2. **Gauge del camino WLS-CG CORREGIDO:** la revisión externa tenía razón — `λ=1e-6`
   absoluto atenúa **29.8 %** el modo fundamental a 4096 px (`k_min²=2.35e-6`). El
   gauge correcto es **relativo**: `λ_gauge = 1e-4·k_min²` ⇒ atenuación `1/(1+1e-4) =
   0.99990` uniforme para N ∈ {128…4096} (verificado). Implementado como
   `max(screen_lambda, 1e-4·k_min²)` en el módulo de investigación v2. `INGENIERÍA`.
3. **`gamma`:** forma cerrada (pesos en las ecuaciones normales, nunca
   multiplicativos — contraejemplo multiplicativo medido ×3.93 en la revisión del
   2026-10-15); valor `NOT_DERIVED` (eje EXP-M1); default sin ponderar.
4. **Nyquist estricto:** anulado por diseño en malla par; debe viajar en el manifest
   con `nyquist_energy_fraction` (ya implementado en v2).

## 6. Estados (REV 2)

| Objeto | Estado |
|---|---|
| Filtro `k²/(k²+λ)` y modo de corte | `VERIFIED` |
| Fórmula de ruido `k²·N²σ²/(k²+λ)²` | `VERIFIED` (MC ratio 1.0014; corrige sobreconteo ×2 de REV 1) |
| Fuga fuera de banda `Σ1/k²` (divergencia log) | `VERIFIED` (MC) + `INFERRED` |
| λ\* continuo > 0 con ganancia ≤ 0.06 % | `EJECUTADO` (objeción externa confirmada) |
| λ=0 default espectral bajo prior declarado | `SUPPORTED_BY` §4 (baseline; no "óptimo general") |
| Gauge WLS `λ=1e-4·k_min²` | `INGENIERÍA` (corrige `λ=1e-6`, objeción externa confirmada: −29.8 % a 4096) |
| Pesos en ecuaciones normales | `VERIFIED` (contraejemplo multiplicativo ×3.93) |
| Valor de `gamma` | `NOT_DERIVED` (EXP-M1) |
| Semántica final de `_p` | `REAL_RIG_REQUIRED` |
| Freeze en prereg | `PENDIENTE ETAPA B de #675` |

## 7. Adjudicación de la revisión externa del commit `8a95f84a` (2026-10-15)

| Afirmación de la revisión | Veredicto | Evidencia / acción |
|---|---|---|
| `λ=0` "no está demostrado como óptimo": `R'(0)<0` ⇒ λ\*>0 | **CORRECTA — aceptada** | §4.2: λ\* continuo existe; ganancia medida ≤ 0.06 %. Enunciado reformulado (baseline, no óptimo) |
| Fórmula de energía de ruido "duplica la contribución" | **CORRECTA en sustancia — aceptada** | §2.1/§4.3: el factor era `k²N²σ²` (proyección ortogonal), no `2N²σ²`; MC ratio 1.0014 tras corregir (y la REV 1 comparaba además regímenes de banda distintos) |
| Gauge WLS `λ≈1e-6` atenúa ~30 % el fundamental a 4096 | **CORRECTA — aceptada** | Medido: 0.702 a N=4096; gauge relativo `1e-4·k_min²` → 0.99990 uniforme (§5.2) |
| Falta runner ejecutable y salida en el commit | **CORRECTA — aceptada** | Apéndices A y B inline; nada depende ya de la conversación |
| "Q8 real" es etiqueta demasiado fuerte | **CORRECTA — aceptada** | Renombrado "Q8 sintético" + caveat EXP-M2 (§2) |
| `try/except` del harness convierte PR roto en skip | **CORRECTA — aceptada** | Harness corregido: import roto ⇒ ERROR/FAIL; API ausente ⇒ skip con razón explícita. Verificado en los tres estados (47 passed en #723; 46 skipped+1 en main; 46 errors con import envenenado) |
| Tests fijan F01/5-16 con reconciliación pendiente | **PARCIALMENTE CORRECTA — aceptada con matiz** | El pin protege el valor declarado del PR contra drift; ahora documentado como `ALCANCE PROVISIONAL` con `F01_CONTRACT_AMENDMENT=PENDING_TECH_LEAD` en el docstring (F2) |
| "CI de integración no verificado" / 47-47 "no reproducido" | **CORRECTA como límite del revisor** | Comando de reproducción pública: checkout de #723 + `pytest tests/test_native_parallax_h4_contract_adversarial.py --noconftest` (self-contained; 47 passed medido en sesión) |
| "No fusionar todavía" / mantener como investigación | **CONCURRENTE** | Era la recomendación propia del commit; sin cambios |

Ningún punto de la revisión fue rechazado. El único matiz: el mecanismo exacto del
sobreconteo de ruido no era "duplicar dos componentes" sino que **la proyección es
ortogonal** (los dos componentes entran por `ikx` e `iky`) — el factor correcto es 1,
no 2, y además la REV 1 comparaba ruido-en-banda (modelo) con ruido-sin-filtrar (MC).
Ambas decisiones de corrección coinciden con lo que la revisión exigía.

## Apéndice A — Runner ejecutable completo (`run_lambda_study_v2.py`, NumPy puro)

```python
"""Estudio sintético de derivación de screen_lambda para EXP-M6 — v2 CORREGIDA.
Correcciones (revisión externa de 8a95f84a): fórmula de ruido k²·N²σ² (proyección
ortogonal, antes 2N²σ²), régimen de fuga fuera de banda, λ* continuo, Q8 sintético.
Modelo declarado: altura band-limited |ĥ|²∝k^-(β+2), banda [2π/N, π/4] sin Nyquist
estricto, RMS(altura)=1; ruido blanco σ, Q8 sintético, o deriva LF k≤16·2π/N."""
from __future__ import annotations
import numpy as np

N_VALUES = (256, 128); BETAS = (2.0, 4.0); SIGMAS = (0.01, 0.03, 0.1)
LAMBDAS = (0.0, 1e-5, 3e-5, 1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1, 3e-1, 1.0)
SEED = 20261015

def make_signal(n, beta, rng):
    idx = np.fft.fftfreq(n) * n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2 + KY**2) * (2*np.pi/n)
    nyq = (np.abs(KX) == n//2) | (np.abs(KY) == n//2)
    band = (kn >= 2*np.pi/n) & (kn <= np.pi/4) & ~nyq
    coef = rng.standard_normal((n, n)) + 1j*rng.standard_normal((n, n))
    amp = np.zeros((n, n)); amp[band] = kn[band]**(-(beta+2)/2)
    h_hat = coef*amp; h_hat[0, 0] = 0.0
    h = np.real(np.fft.ifft2(h_hat)); h /= np.sqrt(np.mean(h**2))
    kxf = (2*np.pi*np.fft.fftfreq(n))[None, :]; kyf = (2*np.pi*np.fft.fftfreq(n))[:, None]
    hn = np.fft.fft2(h); hn[0, 0] = 0.0
    return h, np.real(np.fft.ifft2(1j*kxf*hn)), np.real(np.fft.ifft2(1j*kyf*hn))

def q8_noise(p, q):
    nz = np.ones_like(p); nx, ny = -p, -q
    nrm = np.sqrt(nx**2 + ny**2 + nz**2); nx, ny, nz = nx/nrm, ny/nrm, nz/nrm
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

def solve_screened(p, q, lam):
    n = p.shape[0]
    kx = (2*np.pi*np.fft.rfftfreq(n))[None, :]; ky = (2*np.pi*np.fft.fftfreq(n))[:, None]
    fp = np.fft.rfft2(p); fq = np.fft.rfft2(q)
    num = -(1j*kx*fp) - (1j*ky*fq); num[0, 0] = 0.0
    mask = np.zeros(num.shape, bool)
    if n % 2 == 0: mask[:, -1] = True; mask[n//2, :] = True
    num[mask] = 0.0
    denom = kx*kx + ky*ky + lam; denom[0, 0] = 1.0
    return np.fft.irfft2(num/denom, s=(n, n))

def analytic_curve(n, beta, sigma, lams, noise_filtered):
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

def main():
    rng = np.random.default_rng(SEED)
    print(f"SEED={SEED} · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · "
          f"fórmula de ruido CORREGIDA (k²·N²σ²)")
    for n in N_VALUES:
        for beta in BETAS:
            h, p, q = make_signal(n, beta, rng)
            casos = {**{f"gauss σ={s}": (p+s*rng.standard_normal(p.shape),
                                         q+s*rng.standard_normal(p.shape), s)
                        for s in SIGMAS},
                     "Q8 sintético": (*q8_noise(p, q), None),
                     "limpio": (p, q, None),
                     "driftLF σ=.05": (*drift_noise(p, q, 0.05, rng), 0.05),
                     "driftLF σ=.5": (*drift_noise(p, q, 0.5, rng), 0.5)}
            for label, (pn, qn, sigma) in casos.items():
                sigma_eff = sigma if sigma is not None else float(
                    np.sqrt(np.mean((pn-p)**2 + (qn-q)**2)/2))
                rmses = [float(np.sqrt(np.mean((solve_screened(pn, qn, lam)-h)**2)))
                         for lam in LAMBDAS]
                i_opt = int(np.argmin(rmses))
                curva = analytic_curve(n, beta, sigma_eff, LAMBDAS, True)
                i_th = int(np.argmin(curva))
                print(f"N={n:4d} β={beta:.0f} {label:13s} σ_eff={sigma_eff:.4f} | "
                      f"λ_opt={LAMBDAS[i_opt]:g} (RMSE {rmses[i_opt]:.4f}) "
                      f"λ=0: {rmses[0]:.4f} | λ*_analítico={LAMBDAS[i_th]:g} "
                      f"(E {curva[i_th]:.4f}) | ganancia rejilla ×{rmses[0]/rmses[i_opt]:.3f}")

if __name__ == "__main__":
    main()
```

Verificaciones numéricas adicionales citadas en §4 (script `verify_critic_claims`):
λ\* continuo con ganancia ×1.0000–1.0006 (grid log de 20 000 puntos); MC(400) de la
fórmula corregida (ratio 1.0014); atenuación del gauge `λ=1e-6` = 0.702 a N=4096 vs
`λ=1e-4·k_min²` = 0.99990 a N ∈ {128…4096}.

## Apéndice B — Salida registrada de la ejecución (SEED=20261015)

```
SEED=20261015 · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · fórmula de ruido CORREGIDA (k²·N²σ²)
N= 256 β=2 gauss σ=0.01  σ_eff=0.0100 | λ_opt=0 (RMSE 0.0090) λ=0: 0.0090 | λ*_analítico=0 (E 0.0079) | ganancia rejilla ×1.000
N= 256 β=2 gauss σ=0.03  σ_eff=0.0300 | λ_opt=0 (RMSE 0.0264) λ=0: 0.0264 | λ*_analítico=0 (E 0.0236) | ganancia rejilla ×1.000
N= 256 β=2 gauss σ=0.1   σ_eff=0.1000 | λ_opt=0 (RMSE 0.0899) λ=0: 0.0899 | λ*_analítico=0 (E 0.0785) | ganancia rejilla ×1.000
N= 256 β=2 Q8 sintético  σ_eff=0.0023 | λ_opt=0 (RMSE 0.0022) λ=0: 0.0022 | λ*_analítico=0 (E 0.0018) | ganancia rejilla ×1.000
N= 256 β=2 limpio        σ_eff=0.0000 | λ_opt=0 (RMSE 0.0000) λ=0: 0.0000 | λ*_analítico=0 (E 0.0000) | ganancia rejilla ×1.000
N= 256 β=2 driftLF σ=.05 σ_eff=0.0500 | λ_opt=3e-05 (RMSE 0.3024) λ=0: 0.3065 | λ*_analítico=0 (E 0.0393) | ganancia rejilla ×1.014
N= 256 β=2 driftLF σ=.5  σ_eff=0.5000 | λ_opt=1 (RMSE 1.0040) λ=0: 3.2132 | λ*_analítico=0 (E 0.3927) | ganancia rejilla ×3.200
N= 256 β=4 gauss σ=0.01  σ_eff=0.0100 | λ_opt=0 (RMSE 0.0088) λ=0: 0.0088 | λ*_analítico=0 (E 0.0079) | ganancia rejilla ×1.000
N= 256 β=4 gauss σ=0.03  σ_eff=0.0300 | λ_opt=0 (RMSE 0.0275) λ=0: 0.0275 | λ*_analítico=0 (E 0.0236) | ganancia rejilla ×1.000
N= 256 β=4 gauss σ=0.1   σ_eff=0.1000 | λ_opt=0 (RMSE 0.0939) λ=0: 0.0939 | λ*_analítico=0 (E 0.0785) | ganancia rejilla ×1.000
N= 256 β=4 Q8 sintético  σ_eff=0.0023 | λ_opt=0 (RMSE 0.0079) λ=0: 0.0079 | λ*_analítico=0 (E 0.0018) | ganancia rejilla ×1.000
N= 256 β=4 limpio        σ_eff=0.0000 | λ_opt=0 (RMSE 0.0000) λ=0: 0.0000 | λ*_analítico=0 (E 0.0000) | ganancia rejilla ×1.000
N= 256 β=4 driftLF σ=.05 σ_eff=0.0500 | λ_opt=0 (RMSE 0.3058) λ=0: 0.3058 | λ*_analítico=0 (E 0.0393) | ganancia rejilla ×1.000
N= 256 β=4 driftLF σ=.5  σ_eff=0.5000 | λ_opt=1 (RMSE 1.0053) λ=0: 3.2801 | λ*_analítico=0 (E 0.3927) | ganancia rejilla ×3.263
N= 128 β=2 gauss σ=0.01  σ_eff=0.0100 | λ_opt=0 (RMSE 0.0088) λ=0: 0.0088 | λ*_analítico=0 (E 0.0071) | ganancia rejilla ×1.000
N= 128 β=2 gauss σ=0.03  σ_eff=0.0300 | λ_opt=0 (RMSE 0.0246) λ=0: 0.0246 | λ*_analítico=0 (E 0.0213) | ganancia rejilla ×1.000
N= 128 β=2 gauss σ=0.1   σ_eff=0.1000 | λ_opt=3e-05 (RMSE 0.0921) λ=0: 0.0926 | λ*_analítico=0 (E 0.0711) | ganancia rejilla ×1.006
N= 128 β=2 Q8 sintético  σ_eff=0.0023 | λ_opt=0 (RMSE 0.0020) λ=0: 0.0020 | λ*_analítico=0 (E 0.0016) | ganancia rejilla ×1.000
N= 128 β=2 limpio        σ_eff=0.0000 | λ_opt=0 (RMSE 0.0000) λ=0: 0.0000 | λ*_analítico=0 (E 0.0000) | ganancia rejilla ×1.000
N= 128 β=2 driftLF σ=.05 σ_eff=0.0500 | λ_opt=0.0001 (RMSE 0.1586) λ=0: 0.1600 | λ*_analítico=0 (E 0.0356) | ganancia rejilla ×1.009
N= 128 β=2 driftLF σ=.5  σ_eff=0.5000 | λ_opt=1 (RMSE 1.0083) λ=0: 1.6611 | λ*_analítico=0 (E 0.3557) | ganancia rejilla ×1.647
N= 128 β=4 gauss σ=0.01  σ_eff=0.0100 | λ_opt=0 (RMSE 0.0082) λ=0: 0.0082 | λ*_analítico=0 (E 0.0071) | ganancia rejilla ×1.000
N= 128 β=4 gauss σ=0.03  σ_eff=0.0300 | λ_opt=0 (RMSE 0.0270) λ=0: 0.0270 | λ*_analítico=0 (E 0.0213) | ganancia rejilla ×1.000
N= 128 β=4 gauss σ=0.1   σ_eff=0.1000 | λ_opt=3e-05 (RMSE 0.0802) λ=0: 0.0805 | λ*_analítico=0 (E 0.0711) | ganancia rejilla ×1.004
N= 128 β=4 Q8 sintético  σ_eff=0.0023 | λ_opt=0 (RMSE 0.0020) λ=0: 0.0020 | λ*_analítico=0 (E 0.0016) | ganancia rejilla ×1.000
N= 128 β=4 limpio        σ_eff=0.0000 | λ_opt=0 (RMSE 0.0000) λ=0: 0.0000 | λ*_analítico=0 (E 0.0000) | ganancia rejilla ×1.000
N= 128 β=4 driftLF σ=.05 σ_eff=0.0500 | λ_opt=3e-05 (RMSE 0.1642) λ=0: 0.1647 | λ*_analítico=0 (E 0.0356) | ganancia rejilla ×1.003
N= 128 β=4 driftLF σ=.5  σ_eff=0.5000 | λ_opt=1 (RMSE 1.0131) λ=0: 1.6493 | λ*_analítico=0 (E 0.3557) | ganancia rejilla ×1.628
```

Nota sobre λ\* continuo (§4.2): con la fórmula corregida, el minimizador continuo cae
en `λ* ∈ [1e-12, 3e-10]` con ganancia ×1.0000–1.0006 frente a λ=0 (medido con malla
log de 20 000 puntos; el argumento de derivada de la revisión es correcto, la magnitud
es marginal bajo este prior).
