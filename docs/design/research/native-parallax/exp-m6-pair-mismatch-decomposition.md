# EXP-M6 — Descomposición del mismatch del par authored normal↔height

> **SLICE A — DISEÑO MATEMÁTICO + PREREGISTRO. NO ES `M6_PREREG_FREEZE_SHA`.**
>
> Research-only. NO productización. NO toca Skyrim/MO2/ParallaxR/DynDOLOD/DDS.
> NO ejecuta el corpus. NO implementa el runner. NO crea tests.
>
> Este documento **no es** un prereg congelado: es la ETAPA A (diseño). El freeze
> (§35 etapa E) ocurre después de implementar la matemática pura y validar la
> batería sintética. Este commit es `M6_DESIGN_SHA`.

```text
M5_STATUS=CLOSED_AND_MERGED
M5_MERGE_SHA=ee4a67ec2f0635f02dea794c71eb0f5d06781ada
BASE_MAIN_SHA=ee4a67ec2f0635f02dea794c71eb0f5d06781ada
NATIVE_PARALLAX_BASE_MOVED_MATERIALLY=NO
```

---

## 0. Verificación de base autoritativa (§0 del brief)

`origin/main` estaba exactamente en el merge de M5 y **no tiene commits posteriores**:

```text
ee4a67ec Merge pull request #646 from FacundoSu1986/research/native-parallax-exp-m5-frequency-coherence
a65bad16 fix(native-parallax): fail closed on ambiguous M5 secondary full runs
...
```

`git log ee4a67ec..origin/main` → vacío. Por lo tanto la base matemática M4/M5
**no se movió** y este diseño se apoya en ella sin ambigüedad. El `main` local
estaba 74 commits atrás y se fast-forwardeó; el árbol de trabajo tenía únicamente
archivos *untracked* ajenos, que **no** se tocan ni se incorporan.

Rama creada desde `ee4a67ec`, no desde la rama histórica de M5.

---

## 1. Evidencia heredada (NO reinterpretar)

### 1.1 M4 — `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT`

- SELF-q8 reconstruye con mediana `RMSE ≈ 0.003746`, `|corr| ≈ 0.999165`, `var ≈ 0.998330`.
- AUTH reconstruye con `RMSE ≈ 0.039238`, `|corr| ≈ 0.839229`.
- Resize, cuantización Q8 y discretización FD fueron descartados como causa dominante.
- **Lenguaje heredado obligatorio:** `normal authored ↔ height authored` *no son
  suficientemente coherentes bajo el modelo gradient-height ensayado*. No se afirma
  que los assets estén "mal".

### 1.2 M5 — `EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED`

Corrective FULL (512, apareado, seed 20260925, n_boot 2000):

| Magnitud apareada | Valor |
|---|---:|
| `FULL LOWMID EXCESS` | 0.5082 |
| `FULL HIGH EXCESS` | 0.3350 |
| `HIGH_ENRICHMENT` | 0.7542 (CI95 [0.5710, 1.0470]) |

```text
C1_lowmid_preserved=false
C2_high_enriched=false
```

**Interpretación permitida:** el mismatch AUTH es *broadband* y **no** está
concentrado principalmente en alta frecuencia. Por tanto M6 **no** parte de
"el problema es sólo pérdida de detalle HF" como hipótesis preferida.

### 1.3 Restricción de partida para M6

M6 hereda además tres decisiones de M5 que son **binding** y no se reabren:

```text
PRIMARY_DECISION_RESOLUTION=512
SECONDARY_1024_EXECUTION_SUPPORT=DEFERRED
PROTOCOL_STATUS=UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE
scientific_rules_changed_after_exposure=false
CROSS_WORKTREE_IMPORT_RISK=CONFIRMED (NO corregido; §35)
```

---

## 2. Pregunta científica (principal)

> **¿Qué fracción del mismatch broadband entre la normal authored y el height
> authored puede explicarse mediante mecanismos matemáticos simples y de baja
> dimensionalidad, y qué fracción permanece como residuo geométrico no
> reconciliable por esos mecanismos?**

Descomposición propuesta a **falsar término por término** (no se asume que todos
estén presentes):

```text
PAIR/MODEL MISMATCH  =  NON-INTEGRABILITY (H1)
                      +  PERIODIC REGISTRATION (H2a)
                      +  SIMPLE SPECTRAL TRANSFER (H2b)
                      +  RESIDUAL GEOMETRY MISMATCH (H3)
```

**Prohibiciones de lenguaje:** M6 no mide "calidad de assets", no valida ni
condena proveedores, y no afirma nada fuera del corpus ensayado, la resolución
512, el modelo gradient-height ensayado y el marco periódico.

---

## 3. Alcance y no-alcance

### 3.1 Alcance

## 4. Convenciones y notación matemática

Todo lo que sigue se deriva de la base M0/M2/M3/M4 ya mergeada, **no** de copia
literal de la fórmula del brief.

### 4.1 Rejilla de frecuencia — CONVENCIÓN DEL REPO

De `normal_fft_periodic.freq_axes` (documentado y testeado en M0):

```text
wx[l] = 2π · fftfreq(W)[l] · W      eje x = COLUMNAS
wy[k] = 2π · fftfreq(H)[k] · H      eje y = FILAS
```

Con `numpy.fft` (`fft2` sin normalizar, `ifft2` con `1/N`):

```text
F{∓h/∓x} = i·wx·ĥ          F{∓h/∓y} = i·wy·ĥ
F{Δh}    = −(wx² + wy²)·ĥ
```

Propiedades derivadas del integrador M0:

- **DC**: `ĥ[0,0] = 0`. La media **no es observable** desde gradientes.
- **Nyquist**: `freq_axes` **anula la derivada** en los bins autoparejados de
  Nyquist (extensión par). Afecta a **cuatro** bins en grados pares:
  `(0,0)`, `(0,W/2)`, `(H/2,0)`, `(H/2,W/2)`.

### 4.2 TRAMPA NUMÉRICA DESCUBIERTA: `freq_axes` NO sirve para `ρ` ni para shifts

`freq_axes` anula wx/wy en Nyquist. Reutilizar esos ejes para (a) construir un
radio `ρ` o (b) una fase de desplazamiento **corrompe** el cálculo. Verificado
numéricamente (sintético, sin corpus):

```text
rho de frequency_coherence.rho_grid : max = 45.254834   (= ∜2·W/2, correcto)
rho derivado de freq_axes            : max = 43.840620   <-- CORRUPTO
coinciden en 3969 de 4096 bins (los 127 restantes son fila/columna Nyquist)

shift con grid COMPLETO  : max err = 2.776e-17   (exacto)
shift con freq_axes      : max err = 5.320e-02   <-- FALLA
```

**Regla dura para M6 (ancla de test futura):**

> `ρ` se construye con `frequency_coherence.rho_grid` (o su equivalente local sin
> ceros). La fase de desplazamiento se construye con la rejilla **completa**
> `kx = fftfreq(W)·W`, `ky = fftfreq(H)·H` **sin ceros**. Cualquier reutilización de
> `freq_axes` fuera del solver está **prohibida** y debe tener un test de mutación
> que la detecte.

### 4.3 Notación

```text
H_AUTH_MAP = N ∈ ℝ^{n_rows×n_cols×3}    normal authored (DIRECTX)
g_N = (p, q)                          campo de pendientes derivado de N (§5)
g_H = (∓H/∓x, ∓H/∓y)                  campo gradiente del height (§9)
E(·)                                  energía de Parseval no-DC (§7.1)
```

`·̓` denota `fft2`. `H` denota el campo de height authored; el alto se escribe
`n_rows` cuando hay ambigüedad.

---

## 5. Campo gradiente desde la normal authored

**Usando exactamente la convención aprobada por M4/M5**
(`run_exp_m3.SOLVER_NORMAL_CONVENTION = "DIRECTX"`, `sx = sy = 1`):

```text
p = −sx · nx / max(nz, nz_floor)          nz_floor = 1e-30 en el camino RAW de M2
q = −sy · ny / max(nz, nz_floor)
```

Idéntico a `normal_from_height.gradients_from_normal` y al `decode_gradients_policy`
que consume `integrate_periodic`. **M6 no redefine esta primitiva.**

**Sobre el DC de `g_N` (decisión de diseño):** el solver fija `ĥ[0,0]=0`, es decir
**descarta la pendiente constante** (tilt global) sin contarla como error. Por tanto
la energía DC de `g_N` es energía *no reconstruible pero no rotacional*. Confundirla
con "no-integrabilidad" sobreestimaría H1.

> **Decisión:** la energía DC se **excluye** de `E_∥` y `E_⚥`, y se reporta por
> separado como `DC_SLOPE_FRACTION` (diagnóstico).

---

## 6. Descomposición de Hodge — derivación para Sky-Claw

Para `k ≠ 0` se proyecta sobre la dirección de `k` y su ortogonal:

```text
ĝ_∥(k) = k · (k · ĝ(k)) / |k|²
ĝ_⚥(k) = ĝ(k) − ĝ_∥(k)
## 7. Métrica de no-integrabilidad

### 7.1 Energía (Parseval, no-DC)

Con `numpy.fft` sin normalizar, `Σ_k |ĝ(k)|² = N·Σ_x |g(x)|²`. El factor `N` es
común y **cancela en toda razón**:

```text
E(f) = Σ_{(k,l) ≠ (0,0)} |F{f}(k,l)|²
```

### 7.2 Métrica primaria

```text
E_∥ = E(g_∥)                     componente integrable / curl-free
E_⚥ = E(g_⚥)                     componente rotacional

NONINTEGRABLE_FRACTION := E_⚥ / (E_∥ + E_⚥)      ∈ [0,1], adimensional
```

Se excluye DC (§5). Se define además el control pareado, en el espíritu de
`EXCESS = AUTH − SELF` de M5:

```text
NONINTEGRABLE_EXCESS := NONINTEGRABLE_FRACTION(auth) − NONINTEGRABLE_FRACTION(self_q8)
```

porque la cuantización Q8 del camino SELF **introduce** un piso no nulo (§23, S6).
Expectativa en S0: `≈ 1e-30` en el camino float y `≈` el piso Q8 en el camino q8.

### 7.3 CORRECCIÓN DE DISEÑO (forzada por numerics): la mezcla se define en ENERGÍA

El brief propone el control "known mixture → ≈ injected fraction" con la
predicción ingenua `α²/(α²+(1−α)²)`. **Esa predicción es incorrecta** y el control,
tal como está escrito, es *ill-posed*. Verificado:

```text
E_rot_total = 5.549963e+07   E_grad_total = 1.486760e+07   (ratio 3.732925)

mezcla con α=0.30
  medido              = 0.406753247036
  predicho (energía)  = 0.406753247036   <-- COINCIDE EXACTO
  predicho (amplitud) = 0.155172413793   <-- FALLA
```

Gradiente y rotacional **sí** son ortogonales modo a modo (lo garantiza la
proyección), pero la fracción de energía resultante depende de la **razón de
energías totales** `E_rot/E_grad`, que un α de amplitud no fija.

> **Regla preregistrada:** todo control de mezcla **prescribe la fracción de
> energía**, no la de amplitud. Con `target = f`, los componentes se escalan con
> `c = sqrt( f·E_grad / ((1−f)·E_rot) )`. Verificado: `target=0.40` →
> `medido = 0.400000000000`.

### 7.4 Relación con el curl (no duplica `trust_proxies.curl_proxy`)

En 2D el curl aplica un giro de 90° a la componente rotacional, de modo que
`Σ|curl|² = Σ|k|²·|ĝ_⚥|²`, mientras que `E_⚥ = Σ|ĝ_⚥|²` **no** pondera. Verificado:

```text
E_perp (plano)            = 1.997987e+07
sum|curl|²                = 7.465679e+11
sum|k|²|g_perp|²           = 7.465679e+11     (dif rel 3.270e-16 → identidad)
```

`E_⚥` y `||curl||²` **no** son la misma cantidad ni difieren por un factor
constante (la ponderación es `|k|²`, que varía por modo). Además
`trust_proxies.curl_proxy` reporta medianas/MAD/p95 de `|curl|` — una estadística
de magnitud robusta, no una fracción de energía adimensional. **M6 no duplica una
métrica existente**; la relación queda declarada, no resuelta por unexplación.

---

## 8. Relación con el solver Poisson (¿Hodge añade información?)

Esta es la pregunta que el brief obliga a responder antes de aceptar Hodge, y la
respuesta es **parcialmente negativa**. Verificado numéricamente:

```text
solver(g) vs solver(Π∇ g) : max dif = 1.388e-17   (imaginario 5.9e-18)
```

**El solver M4 YA ES la proyección de Frankot–Chellappa.** Su fórmula

```text
ĥ = (−i·wx·p̓ − i·wy·q̓) / (wx² + wy²)
```

*es literalmente* `ĝ_∥` mapeado a height. Por tanto:

| Afirmación | Veredicto |
|---|---|
| ¿La proyección Hodge produce una **reconstrucción** que el solver no producía? | **NO.** Idénticas a 1.4e−17. |
| ¿Aporta un **diagnóstico** que el solver no expone? | **SÍ.** El solver *descarta* `g_⚥` sin cuantificarlo. |
| ¿Es un **modelo anidado** de la escalera de recovery? | **NO** (ver §16.1). |

> **Conclusión de diseño:** Hodge **no** se conserva como mecanismo de
> reconciliación. Se conserva como **eje diagnóstico** (§16.1, H1), porque cuantifica
> algo que ningún output de M4/M5 expone: cuánta energía del campo derivado de la
> normal **ni siquiera pertenece al subespacio de gradientes**. Es la única
> medición de M6 que es *normal-only* y por tanto no consume el oráculo.

```text
HODGE_AS_RECONSTRUCTION = NO
HODGE_AS_DIAGNOSTIC     = YES
```

---

## 9. Target gradient — UNA sola definición primaria

**Definición primaria única:**

```text
g_H := spectral_gradients(H) = (∓H/∓x, ∓H/∓y)
```

usando `normal_from_height.spectral_gradients` — **exactamente** la misma primitiva
que el camino SELF de M4 y que el solver consume. Esto garantiza que la comparación
`g_N` vs `g_H` usa **un solo** operador de derivación en ambos lados.

- **Prohibido en la métrica primaria:** mezclar una discretización distinta por
  lado (p.ej. FD central para `g_N` y espectral para `g_H`). La DFT central
  introduce el factor `sinc` y contaminaría la comparación con una discrepancia de
  discretización, no de geometría.
- **Secundario/adversarial permitido:** `fd_forward` ya existe en M4
  (`solver_coherence.fd_forward`) y se conserva como **mutación** que cuantifica
  cuánto del cambio en `NONINTEGRABLE_FRACTION` es atribuible a la discretización y
  no al par. Nunca sustituye la primaria.

---

## 10. Auditoría de redundancia: escala global y ganancia de pendiente

El brief exige responder si `slope gain` es un grado de libertad independiente de
`OracleOnly.fit_global_scale`. La respuesta es **no, salvo degeneración**, y es
demostrable en el pipeline exacto de M4/M5.

**Pipeline M4/M5 (cadena real):**

```text
N_AUTH → (p,q) = −n_xy/max(nz, floor)        [no lineal si el floor actúa]
          → integrate_periodic → h_rec       [LINEAL en (p,q), salvo bins nulos]
          → OracleOnly.fit_global_scale(H, h_rec) = cov/var(h_rec)
          → OracleOnly.evaluate(...)          [métricas]
```

**Paso 1 — el solver es lineal.** Verificado:

```text
integrate_periodic(a·p, a·q) == a · integrate_periodic(p, q)
max dif = 3.123e-17   (a = 3.7)
```

**Paso 2 — el oráculo afín absorbe el residuo.** Sea `s = cov(H, h)/var(h)` la
escala ajustada. Con `h_a = a·h` (o `h_a = shift(h)`), el contrato M4 produce:

```text
cov(H, a·h) / var(a·h) = a·cov(H,h) / (a²·var(h)) = s / a
h_rec_evaluado          = a·h · (s/a) = s·h
```

**Idéntico al caso sin ganancia**, para todo `a ≠ 0`. El ajuste afín es
*invariante* bajo el escalado de la reconstrucción, no lo compensa: lo cancela
exactamente. Verificado también para el operador de desplazamiento (§12.5).

**Resultado:**

```text
GLOBAL_SCALE_DOUBLE_COUNTED=NO
SLOPE_GAIN_PRIMARY=NO
OFFSET_PRIMARY=NO          (ya absorbido por el ajuste afín de M4; y el DC es
                            inobservable por el solver de todos modos)
```

**Consecuencia de parsimonia:** la escala afín global de M4 **no cuenta** como
DOF nuevo de M6. La lista de §17 es la lista completa. Cualquier propuesta futura
de "ganancia de pendiente" debe demostrar que **no** es `s/a` con la misma
`a`; si lo es, está doblemente contada por construcción y no se admite.

**Salvedad honesta (por qué no es "exactamente" redundante):** la cadena se rompe
si `nz < nz_floor` en una región no nula. Ahí `p = −nx/nz_floor` deja de ser
homogénea de grado 1 y una ganancia previa a la división por `nz` no se cancela.
Por eso M6 registra `nz_floor_hits` (ya existe en M2/M3) por asset y **declara**
que la identidad es exacta sólo bajo `floor_hits = 0`. No se usa como excuse para
reintroducir la ganancia como DOF.

---

## 11. Modelo de registro (periodic rigid translation)

### 11.1 Definición

Traslación rígida periódica sobre el toro `Z_H × Z_W` de la reconstrucción:

```text
T_δ {h}(x, y) = h(x − dx, y − dy)          δ = (dx, dy) ∈ Z_H × Z_W
```

Implementación **primaria por fase de Fourier** (O(N log N), exhaustiva):

```text
ĥ_new(k, l) = ĥ(k, l) · exp(−2πi·(kx·dx/W + ky·dy/H))
```

### 11.2 ⚠️ CONVENCIÓN DE SIGNO — verificada, no asumida

El brief exige auditar signos. La notación de `numpy` es
`np.roll(f, s)[x] == f[x − s]`, luego el multiplicador es `exp(−2πi k s / N)`.
Verificado bin a bin contra un impulso unitario:

```text
max|F(roll(f,s)) − F(f)·exp(+2πi k s/N)| = 2.000e+00     <-- INCORRECTO
max|F(roll(f,s)) − F(f)·exp(−2πi k s/N)| = 2.734e-15     <-- CORRECTO
```

Una primera versión del diseño de M6 **usaba el signo contrario** y producía
`R = 0.031` donde debía dar `R = 1.000`. Registrado como hallazgo de §38.

### 11.3 ⚠️ CONVENCIÓN DEL ARGMAX — verificada

El estimador de `δ` es la **correlación cruzada por FFT**:

```text
C(δ) = IFFT( conj(F{recon}) · F{target} )(δ)      δ* = argmax_δ C(δ)
```

**`conj(F{recon}) · F{target}`, en ese orden.** La forma inversa
(`F{recon} · conj(F{target})`) devuelve `−δ*`. Verificado con shift inyectado
`(dy=12, dx=−40)`: la forma correcta da `δ* = (24,12)` y `R = 1.0000000000`; la
invertida da `(−12,−24)`. Este es el segundo defecto de signo detectado (§38).

### 11.4 Exacto y verificado en los casos inyectados

```text
inyectado ( 0, 0)  → δ*=( 0, 0)   equiv=1
inyectado ( 5, 5)  → δ*=( 5, 5)   R=1.0000000000
inyectado (17,17)  → δ*=(17,17)   R=1.0000000000
inyectado (−23,−23)→ δ*=(23,23)   R=1.0000000000
inyectado (60,60)  → δ*=(−4,−4)   R=1.0000000000   (equivalencia de toro)
inyectado (dy=12,dx=−40) → δ*=(24,12)  R=1.0000000000
```

Note el caso `(60,60) → (−4,−4)`: en un toro de 64, `δ` y `δ − 64` son el **mismo**
operator. Es correcto y es la razón por la que el bound se define sobre el toro
completo (§12).

### 11.5 Dónde se aplica (post-solver, demostrado equivalente)

El solver es diagonal en Fourier y el shift es una fase; conmutan. Verificado:

```text
shift(p,q)→solver  vs  solver→shift : max dif = 8.674e-18   (y 1.041e-17)
oráculo afín escalar 2.5×  commuta    : max dif = 3.469e-17
```

**Decisión:** M2/M4 aplican el shift **post-solver**, sobre el height reconstruido.
Es equivalente en general y **deja el solver M4 intacto**, que es un STOP
condition explícito (§37).

### 11.6 Lo que el registro NO hace

- **Prohibido** `zero padding`, `crop`, `reflection`, `edge fill`. El modelo es
  periódico; cualquier frontera no periódica cambia el problema que M4 midió.
- **Prohibido** registrar un solo eje. Es un modelo 2D; registrar sólo `dx` o sólo
  `dy` es otro experimento.

---

| Afirmación | Veredicto |
|---|---|
| ¿La proyección Hodge produce una **reconstrucción** que el solver no producía? | **NO.** Idénticas a 1.4e-17. |
| ¿Aporta un **diagnóstico** que el solver no expone? | **SÍ.** El solver *descarta* `g_⚥` sin cuantificarlo. |
| ¿Es un **modelo anidado** de la escalera de recovery? | **NO** (ver §17). |

> **Conclusión de diseño:** Hodge **no** se conserva como mecanismo de
> reconciliación. Se conserva como **eje diagnóstico** (§17, H1), porque cuantifica
## 12. Bound de registro — fijado ANTES de mirar los 31 assets

El brief prohíbe elegir el rango mirando resultados reales. Hay tres salidas
honestas y sólo una sobrevive al criterio adversarial.

### 12.1 Opción A — bound por razón física/matemática

Se descartan los rangos "en píxeles razonables" (`|δ| ≤ 8`, `≤ 16`): **no existe
razón física o matemática en el repo que los respalde** y un número sin
justificación es un threshold coaccionado al corpus. `REGISTRATION_PRIMARY` no
puede descansar en esto.

### 12.2 Opción B — phase correlation con criterio de ambigüedad

`argmax` sobre el toro completo, con `REGISTRATION_AMBIGUOUS` (§13) cuando el
segundo máximo no separa. Es válida y es la que se adopta.

### 12.3 Opción C — subpixel acotado

Descartada en este slice: un refinado subpixel añade DOF y una superficie de
optimización que sólo empeora la parsimonia sin aportar evidencia adicional en la
resolución primaria 512. **Si algún día se necesita, debe venir con un tipo
machine-readable distinto del primario** (§31).

### 12.4 DECISIÓN

```text
REGISTRATION_BOUND = FULL_INTEGER_TORUS_EXHAUSTIVE
                   |dx| ∈ [0, floor(n_cols/2)] , |dy| ∈ [0, floor(n_rows/2)]
                   (equivalencias de toro colapsadas: δ ~ δ+(W,0) ~ δ+(0,H))
```

**Por qué esto NO es elegir un rango mirando datos:** el toro completo es el
**único** conjunto que no requiere criterio externo — es el grupo completo de
traslación del modelo que M4 ya asume (`freq_axes`, `integrate_periodic`).
Cualquier subconjunto (`|δ| ≤ 8`) sería una afirmación sobre los assets reales
que nadie puede fundamentar antes de verlos. Elegir "todo el grupo" es la decisión
que **no puede estar contaminada por el corpus**, y su coste (potencia del
oráculo) se acota por los controles sintéticos, no por un número arbitrario.

Consecuencias asumidas explícitamente:

1. `REGISTRATION_PRIMARY=YES`, con el riesgo de ser un **oráculo muy potente**
   (§38, ataque 2), acotado por S4 y por la ambigüedad de §13.
2. El dominio de `δ` es **discreto** (`n_rows·n_cols` candidatos): el argmax es
   exacto y determinista, sin optimizador ni tolerancia de convergencia.
3. Un `|δ|` grande **no se interpreta como "misma geometría"**: se reporta y se
   acompaña del estado de ambigüedad.

---

## 13. Ambigüedad de registro — estado explícito

Texturas repetitivas producen **varios máximos exactamente equivalentes**.
Verificado con tiles de período `p` en un toro 64×64:

```text
periodo  4×4  → 256 máximos exactamente equivalentes
periodo  8×8  →  64 máximos exactamente equivalentes
periodo 16×16 →  16 máximos exactamente equivalentes
periodo 32×32 →   4 máximos exactamente equivalentes
margen (best − 2nd) = 0.000e+00   en TODOS los casos repetidos

textura NO repetitiva → margen = 1.2602e-01 , equivalentes = 1
```

**Dos consecuencias duras:**

1. El margen **absoluto** separa perfectamente estos dos casos (0 vs 1.26e−1), pero
   su valor depende de la amplitud del contenido, así que **no** admite un threshold
   fijado mirando datos.
2. Por tanto el criterio **no puede ser un threshold sino un estado**, con una
   comparación normalizada que no dependa de la amplitud:

```text
REGISTRATION_AMBIGUITY_RATIO := C(2nd) / C(best)          ∈ [0, 1]
REGISTRATION_AMBIGUOUS        := REGISTRATION_AMBIGUITY_RATIO > T_AMBIG
```

`T_AMBIG` **no se fija en este documento**: se deriva en ETAPA B comparando el control
sintético `A7` (tile repetido: empates exactos) contra `S1` (no repetido: pico único).
Registrar `best`, `2nd`, ratio y número de equivalentes es **obligatorio** en todo
caso.

Cuando `REGISTRATION_AMBIGUOUS` es verdadero:

- El `δ*` elegido **no alimenta la decisión primaria** (es arbitrario entre
  equivalentes).
- El asset **no se excluye**: sigue parciendo en el resto de métricas.
- `RECOVERY_FRACTION` de los modelos con registro se etiqueta
  `REGISTRATION_AMBIGUOUS` en lugar de publicar un número único.

> Nota de parsimonia que apoya el diseño: si los `n` equivalentes dan el **mismo**
> `RECOVERY_FRACTION` —deben, pues son el mismo operator salvo traslación de toro—,
> la ambigüedad afecta a la **dirección** de `δ`, no a la **magnitud** del recovery.
> M6 reporta ambas cosas por separado.

---

## 14. Familia de transferencia espectral simple

### 14.1 Requisitos

- Radial, **1 DOF**.
- Identidad exacta en `β = 0`.
- **Anclada**: `G_β(ρ_ref) = 1` para todo `β` → no duplica la escala global.
- Acotada en `ρ → 0` (sin singularidad).
- Real y Hermitian-safe por radialidad.
- `ρ_ref` **heredado**, no elegido por M6.

### 14.2 Fórmula (con la corrección del anchor)

**Borrador rechazado.** La familia Butterworth "cruda"
`G(ρ) = (1 + (ρ/ρ_ref)²)^{−β/2}` cumple `ρ→0` acotado e identidad en `β=0`, pero
**NO cumple el anchor**: `G(ρ_ref) = 2^{−β/2} ≠ 1` para `β ≠ 0` (medido
`G(ρ_ref) = 0.707106781186548` para `β=1`). Arrastraría una ganancia global
dependiente de `β`, es decir **duplicaría la escala** justo lo que §10 evita.
**Rechazada** — defecto nº3 detectado por la verificación numérica (§38).

**Familia preregistrada:**

```text
G_β(ρ) = ( (1 + (ρ/ρ_ref)²) / 2 )^{−β/2}
```

Verificado numéricamente:

```text
G_β(ρ_ref) = 1.000000000000000      para β ∈ {+2, 0, −2}   (anchor EXACTO)
G_β(0⁺)    = 2^{−β/2}              finito y acotado (0.5 … 2)
G(β=0)     = identidad: max|G_0(h) − h| = 1.041e-17
DC         = 1 por construcción (bin nulo forzado a ganancia 1); media preservada
```

### 14.3 Interpretación y signo

```text
β > 0  →  atenuación relativa de alta frecuencia (low-pass relativo)
β < 0  →  realce relativo de alta frecuencia
β = 0  →  identidad exacta
```

La polaridad queda **anclada** en `ρ_ref`, que es el corte LOWMID/HIGH de M5.

### 14.4 `ρ_ref` heredado de M5 (no elegido por M6)

```text
ρ_ref = 32 ciclos/tile   (frequency_coherence.LOWMID_HIGH_CUTOFF, congelado en M5)
```

Se reutiliza la constante **ya publicada y congelada** de M5 en lugar de elegir un
anchor nuevo: es defendible por continuidad (mismo eje `ρ`, mismo corte) y no
requiere mirar el corpus M6. `ρ` se construye con `rho_grid` (§4.2).

### 14.5 Bounds de β (derivados, no observados)

Criterios, todos evaluables sin corpus:

1. `β = ±2` da `G(corner Nyquist) ∈ [1.55e−2, 6.45e+1]` (≈36 dB de rango dinámico):
   suficiente para ser distinguible, acotado para no degenerar en un filtro libre.
2. `G_β(0⁺) ∈ [0.5, 2]` — el bajo ni se infla ni se colapsa.
3. La **identificabilidad** (§25) se verifica en S3/S5: si `β` no es recuperable en
   todo el rango, el rango se **estrecha** (nunca se amplía).

```text
SPECTRAL_TRANSFER_BETA_RANGE = [−2, +2]     (candidato; ETAPA B confirma o estrecha)
```

Se congela antes de Cohort A, en ETAPA B.

### 14.6 Propiedades de conmutación (relevante para la parsimonia)

`Tilt`, `Shift` y el escalón afín son todos operadores de convolución en el toro y
**conmutan entre sí** (verificado a ~1e−17). Esto significa que M4 no depende del
orden de aplicación y que no existe un orden "correcto" que ajustar: el modelo
está bien definido como un único operador compuesto. Beneficia la reproducibilidad
y elimina una fuente de grados de libertad accidentales.

---

## 15. No-sobrefit espectral y conteo de DOF

**Prohibido como primario:**

```text
free gain per M5 band (B1..B7)         arbitrary FFT mask
per-frequency gain                      splines con muchos knots
neural filters                          máscara adaptativa de datos
```

El argumento de parsimonia es cuantitativo, no retórico: los cortes B1..B7 de M5 son
7 bandas y el ajuste por banda daría ≥7 DOF frente a **1** de la familia radial. Con
`n = 31` assets como unidad estadística, 7 DOF ajustados emparejados empiezan a
competir con el ruido muestral; 3 DOF no.

```text
translation  = 2 DOF   (dx, dy)
spectral     = 1 DOF   (beta)
TOTAL_PRIMARY_FREE_PARAMETERS = 3
```

**La proyección de Hodge NO cuenta como DOF ajustado:** es un operador determinista
sin parámetros, y §8 mostró que además no altera la reconstrucción. Es un *eje
diagnóstico*, no un modelo.

---

## 16. Modelos anidados (fijados ANTES del corpus)

Familia cerrada y **no extendible después de observar datos**:

```text
M0 = AUTH baseline (idéntico al camino AUTH de M4/M5)         0 DOF ajustados
M1 = M0 + proyección integrable (Hodge) sobre g_N             0 DOF ajustados
M2 = M0 + registro periódico δ = (dx, dy)                    2 DOF ajustados
M3 = M0 + transferencia espectral G_β                        1 DOF ajustados
M4 = M0 + registro + transferencia espectral                  3 DOF ajustados
```

### 16.1 Corrección de diseño frente al brief: **M1 no es un modelo**

El brief propone `M1 = integrable projection diagnostic` como primer peldaño de la
escalera de recovery, y pide explícitamente "revisar si M1 produce una
reconstrucción independiente o sólo una métrica diagnóstica".

**La revisión da "sólo una métrica diagnóstica", y es demostrable:**

```text
solver(g) vs solver(Π∇ g) : max dif = 1.388e-17
```

El solver M4 **ya** aplica la proyección de Frankot–Chellappa (§8). Por tanto
`M1 ≡ M0` como reconstrucción y `RECOVERY_FRACTION(M1) ≡ 0` por construcción.

**Decisión (que cambia el diseño respecto del prompt):**

- **M1 no es un peldaño de la escalera de recovery.** Se elimina como rung.
- La no-integrabilidad se reporta como **eje diagnóstico paralelo**, con su
  propio vocabulario de decisión, ortogonal a la escalera de recovery.
- Se conserva en la familia **sólo** como rung de *verificación*: un test que
  afirme `RECOVERY_FRACTION(M1) == 0` dentro de tolerancia numérica. Ese test es
  el que ancla el hallazgo de §8 y falla si alguien "arregla" el solver sin querer.

```text
M1_RETAINED_AS = DIAGNOSTIC_AXIS + INVARIANT_TEST
M1_RETAINED_AS_RECOVERY_RUNG = NO
```

Es una mejora de parsimonia, no un recorte: una escalera con un peldaño que por
construcción da `R ≡ 0` no informa nada y sólo ofrece tentación de reinterpretarlo.

### 16.2 Baseline M0

```text
M0 := reconstrucción AUTH de M4/M5, sin ninguna transformación adicional
     (misma cadena: N_AUTH → p,q RAW → integrate_periodic → fit_global_scale
      → OracleOnly.evaluate, resolución 512, convención DIRECTX)
```

`M0` **no se reimplementa**: se reutiliza la matemática M4. M6 añade únicamente las
capas explicativas **post-solver** (§11.5).

---

## 17. Métricas de energía y recovery fraction

### 17.1 Por qué no RMSE

`RMSE` **no es aditivo**: el RMSE del error de un modelo que combina dos fuentes no
se descompone en la suma de los RMSE de cada una. Para responder "¿qué fracción del
error explican los modelos?" hace falta una cantidad **lineal en la energía del
error**. Por eso la métrica primaria de M6 es **MSE/energía** y RMSE queda como
secundaria (§19).

### 17.2 Definiciones (mismo contrato de evaluación para todos los modelos)

Bajo **idéntico** contrato M4/M5 — misma resolución, misma convención, mismo
`fit_global_scale` por asset, mismo oráculo:

```text
E_SELF    = MSE( scale_SELF  · H_self , H )
E_AUTH    = MSE( scale_AUTH  · H_auth , H )
E_MODEL_j = MSE( scale_model · H_model_j , H )
```

`E_SELF` es el **techo del pipeline** bajo el contrato M4; `E_AUTH` es el punto de
partida; `E_MODEL_j` es el valor tras aplicar el modelo `j`.

### 17.3 Exceso del par y recovery

```text
PAIR_EXCESS_ENERGY  := E_AUTH − E_SELF                (≥ 0 por construcción)
RECOVERED_ENERGY_j  := E_AUTH − E_MODEL_j
RECOVERY_FRACTION_j := RECOVERED_ENERGY_j / PAIR_EXCESS_ENERGY
```

**Interpretación (adimensional, en energía):**

```text
R_j = 1     el modelo alcanza el techo SELF
R_j = 0     el modelo no explica nada del exceso
R_j < 0     el modelo EMPEORA respecto de M0   (NO se clampa)
R_j > 1     el modelo supera el techo SELF     (NO se clampa)
```

**No clampear en la primaria.** Una versión `clamp(R, 0, 1)` existe como
**secundaria de visualización**, marcada como tal; jamás alimenta una decisión.

> Nota: `R < 0` es información legítima, no un error. Si el único modelo con
> registro empeora de forma sistemática, eso es un resultado (el registro no explica
> el par), no un bug que haya que esconder.

---

## 18. Gate del denominador (`PAIR_EXCESS_ENERGY_GATE`)

Si `PAIR_EXCESS_ENERGY` es pequeño, `R_j` es numéricamente inestable: una variación
mínúsima de `E_MODEL_j` se divide por un número cercano a cero y hasta el **signo** de
`R_j` pasa a ser ruido. Un `R` negativo por error numérico se leería como "el modelo
empeora", que es una afirmación científica falsa.

**Regla preregistrada:**

1. El gate se deriva de **precisión numérica + controles sintéticos**, nunca de
   Cohort A.
2. Si `PAIR_EXCESS_ENERGY` no supera el gate, el asset recibe
   `RECOVERY_FRACTION_NOT_EVALUABLE` y su `R_j` **no entra** a las medianas de la
   cohorte. El asset **no** se excluye del resto de métricas — mismo tratamiento que
   `ENERGY_GATE` de M5, que marca la métrica y no el asset.
3. **Prohibido** `denominator += epsilon` silencioso. Si se usa cualquier
   regularización, debe estar **declarada** en el JSON con su valor, y no alimenta la
   métrica primaria.

**Derivación propuesta (a confirmar/ajustar en ETAPA B con S6 y A3):**

```text
PAIR_EXCESS_ENERGY_GATE := G · E_SELF        (gate RELATIVO al piso del contrato)
con G congelado en ETAPA B
```

Justificación: `E_SELF` es el piso conocido del contrato M4 (Q8 + solver). Exigir
que el exceso lo supere por un factor fijo `G` es una afirmación sobre la
**magnitud del efecto**, no sobre los datos observados. Se registra además
`PAIR_EXCESS_ENERGY_GATE_KIND` (`RELATIVE_TO_SELF`) para que un lector no tenga que
inferirlo, y `PAIR_EXCESS_ENERGY_GATE_VALUE` con el valor congelado.

`G` **no se fija en este documento**: hacerlo aquí sin S6 executed sería elegir un
threshold sin derivarlo, exactamente lo que el brief prohíbe. Se congela en ETAPA B.

---

## 19. Métricas secundarias (diagnóstico, sin poder decisorio)

Se conservan **sin modificar sus definiciones** (M4/M5 quedan intactos):

```text
aligned_rmse / DELTA_RMSE          |corr| / Pearson / Spearman
variance_ratio                     gradient_rmse
normal_angle_mean / p95            seam_height / seam_gradient
nz_floor_hits / negative_nz_fraction / max_gradient
M5: NRMSE por banda B1..B7, LOWMID, HIGH, HIGH_ENRICHMENT, EXCESS_NRMSE
```

Ninguna secundaria sustituye a la primaria post-hoc. Se reportan por asset y por
familia como en M4/M5.

---

## 20. Batería sintética obligatoria (ETAPA B, antes de Cohort A)

Cada caso tiene un resultado **esperado cuantitativo** y una **falsación** asociada.
Todos son deterministas, sin RNG en el camino primario, y **no tocan el corpus**.

| # | Caso | Inyectado | Esperado |
|---|---|---|---|
| S0 | COHERENT | normal derivada matemáticamente de H | `NONINTEGRABLE ≈ 0`, `δ ≈ 0`, `β ≈ 0`, `PAIR_EXCESS` en el piso |
| S1 | PURE SHIFT | shift circular conocido `(dx,dy)` | `δ̓ = δ inyectado` (R = 1.0000000000 verificado) |
| S2 | PURE CURL | componente rotacional con **fracción de energía** conocida `f` | `NONINTEGRABLE ≈ f` (control en energía, §7.3) |
| S3 | PURE SPECTRAL | sólo `G_β` con `β` conocido | `β̓ = β inyectado` (verificado exacto en ±2, ±1, ±0.5) |
| S4 | DIFFERENT GEOMETRY BROADBAND | dos superficies sin relación | los modelos simples **NO** deben recuperar fracción grande |
| S5 | SHIFT + SPECTRAL | ambos, parámetros conocidos | recuperación conjunta sin DOF extra |
| S6 | SELF Q8 | ceiling histórico M4 | compatible con M4; fija el piso de `NONINTEGRABLE` de Q8 |
| **S7** | **INTEGRABLE BUT DIFFERENT** | normal perfectamente integrable de un height **distinto** | `NONINTEGRABLE ≈ 0` **y** `RECOVERY ≈ 0` |

### 20.1 S7 — el caso adversarial clave (§38)

> *Un campo normal puede ser perfectamente integrable y, aun así, corresponder a un
> height totalmente distinto del target.*

Verificado: un gradiente exacto da `NONINTEGRABLE = 1.099e−31` mientras el height
subrayacente es completamente distinto. **Por tanto `low curl ≠ pair coherence`.**

S7 existe para que ninguna regla de decisión pueda deducir "coherencia del par" a
partir de "baja no-integrabilidad". Sin S7, esa inferencia sería un error de diseño
invisible hasta ejecutarlo sobre datos reales.

### 20.2 S4 — el control anti-overfit más importante

S4 es el control que **falsar**ía H2. Si M2/M3/M4 recoveran una fracción grande de S4
— donde el contenido es genuinamente diferente —, el mecanismo no está explicando
geometría: está sobreajustando. **Si S4 no se clasifica como "no explicado", STOP.**

### 20.3 Invariantes duras (tests, no assertions sueltas)

```text
Parseval E∥ + E⚥ = E_total                       (verificado: gap 0.0)
gradiente exacto → NONINTEGRABLE ≈ 0              (verificado: 1.05e-31)
rotacional puro → NONINTEGRABLE ≈ 1               (verificado: 1.000000000000000)
mezcla → NONINTEGRABLE ≈ f (energía, no amplitud)  (verificado: 0.400000000000)
G_β(ρ_ref) = 1 exacto para todo β                 (verificado: 1.000000000000000)
G_0 = identidad exacta                             (verificado: 1.04e-17)
solver(g) == solver(Π∇g)                          (verificado: 1.4e-17)
shift por fase con grid completo == np.roll        (verificado: 2.8e-17)
shift por fase con freq_axes → FALLA               (verificado: 5.3e-02)
DC forzado a ganancia 1 / media preservada
Hermitian: ifft2(ĝ_∥) real dentro de tolerancia
fail-fast NaN/Inf (contrato M0/M2/M5)
determinismo bit-a-bit en dos corridas
```

Cada invariante lleva además un **test de mutación** que la rompe a propósito (el
patrón ya usado en M1–M4): sin él, un invariante que nadie rompe puede estar
midiendo lo que sea.

---

## 21. Casos sintéticos adversarios

Además de S0–S7, la batería debe cubrir:

```text
A0  flat field                    A1  near-flat field
A2  very-low excess               A3  exactamente en el gate del denominador
A4  Nyquist-heavy                 A5  single sine
A6  multi-frequency               A7  repeated tiles (registro AMBIGUO)
A8  steep slope                   A9  nz close to floor (nz_floor_hits > 0)
A10 pure x gradient               A11 pure y gradient
A12 diagonal gradient             A13 rotational field
A14 mixed curl + gradient
```

Dos de estos tienen función de **gate**, no de métrica:

- **A9 (`nz` cerca del floor)** es el único caso donde la identidad de §10
  ("ganancia de pendiente exactamente redundante") **puede romperse**, porque la
  división deja de ser homogénea. Debe producir un marker explícito.
- **A7 (repeated tiles)** es el caso que **deriva `T_AMBIG`** (§13): debe activar
  `REGISTRATION_AMBIGUOUS`, mientras S1 (no repetido) no debe hacerlo.

---

## 22. Identificabilidad

Bajar el error **no basta**. Un parámetro que no se puede recuperar no es evidencia
de nada: puede estar compensando algo que no sea su interpretación.

**Exigencia:** para cada parámetro, en los sintéticos donde se inyecta su valor,

```text
estimated_parameter ≈ injected_parameter        dentro de tolerancia CONGELADA
```

**Tolerancias: se congelan en ETAPA B**, antes del corpus. Estado explícito
`PARAMETER_NOT_IDENTIFIABLE` cuando no se cumple.

**Resultado de la verificación previa (§38), ya medido:**

```text
dx, dy      → S1: δ̓ = δ exacto, R = 1.0000000000 (incluye dx≠dy y wrap de toro)
β           → S3: β̓ = β exacto en {-2,-1,-0.5,0,+0.5,+1,+2}
              (resolución del grid de búsqueda, no tolerancia gruesa)
nonintegrable → S2: medido = target al dígito 12 cuando la mezcla se define en
              ENERGÍA (§7.3)
```

**Confusión `β` ↔ escala global — testeada y descartada.** Con el oráculo afín
libre, ajustar `(β, s)` sobre datos con tilt inyectado devuelve `β̓ = β_inyectado` y
`s = 1.000000` con `E_model = 0` y `R = 1.000000`. Esto **demuestra** que la
familia anclada (§14.2) no se confunde con la escala global: si `β` absorbiera
escala, el par `(β̓, ŝ)` sería degenerado y el residuo no anularía. Es el test que
protege `GLOBAL_SCALE_DOUBLE_COUNTED=NO` (§10).

**Criterio de identifiabilidad:** si dos parámetros distintos dan casi la misma
función objetivo (separación < tolerancia), marcar `PARAMETER_NOT_IDENTIFIABLE` en
lugar de reportar un valor. Un parámetro no identificado **no** puede entrar en una
decisión.

---

## 23. Fitting = oráculo explicativo (caveat obligatorio)

Todos los modelos M2/M3/M4 ajustan sus parámetros **usando `H` authored**: `δ` se
elige maximizando correlación contra el target, y `β` se elige minimizando error
contra el target. Eso es un **oráculo**.

```text
EXPLANATORY_ORACLE          (así se etiqueta)
DEPLOYABLE_ESTIMATOR        (NO es lo que M6 mide)
DEPLOYABILITY_CLAIMED       = NO
```

**La pregunta que M6 responde es:**

> ¿Existe una reconciliación matemática **simple** entre el par authored?

**No** es:

> ¿Podemos **inferirla sólo desde la normal** en Skyrim?

La segunda es un experimento posterior, con su propio diseño, sus propios riesgos
de selección y su propio poder predictivo. Confundirlas convierte un resultado
explicativo en una promesa de producto que M6 no puede sostener.

Consecuencia operativa: ninguna salida de M6 puede alimentar `AUTO_SAFE`, ranking
de assets ni política de confianza. El §5 de M2 (trust proxies strictly separate
from oracle) se mantiene.

---

## 24. Unidad estadística y regla pareada

```text
STATISTICAL_UNIT = ASSET
PIXEL_IS_STATISTICAL_UNIT = NO
FFT_COEFFICIENT_IS_STATISTICAL_UNIT = NO
PATCH_IS_STATISTICAL_UNIT = NO
```

Un asset de 512² tiene 262 144 píxeles; 31 assets tienen ~8.1M. Eso **no** aumenta
`n`. `n = 31`. Las bandas, los píxeles y los coeficientes son **medidas** dentro de
la unidad, nunca unidades.

### 24.1 Regla pareada (obligatoria, sin excepciones)

Toda comparación se calcula **por asset primero**, y se agrega **después**:

```text
CORRECTO:   median_i ( R_i^{M2} − R_i^{M0} )      Appeado, por asset
INCORRECTO: median_i(R_i^{M2}) − median_i(R_i^{M0})
```

Las dos formas **no son equivalentes**: con assets heterogéneos pueden caer a
distinto lado de un threshold e invertir una comparación. Esto no es teórico — es
exactamente el defecto que el **corrective freeze de M5 §21** detectó en el código
real (`cohort_medians` mal agregado) y por lo que M5 tuvo que re-corregir el FULL.

> **Ancla:** la batería de M6 debe incluir un test que falle si alguien reintroduce
> `median(A) − median(B)` donde el prereg pide `median(A − B)`, exactamente como
> el agregado apareado de M5.

### 24.2 Bootstrap

```text
ESTIMADOR      : mediana per-asset
IC             : bootstrap 95% percentiles sobre los assets (asset-level)
N_BOOT         = 2000                        (continuidad con M4/M5)
M6_BOOTSTRAP_SEED = <se congela en ETAPA B>
```

**Sobre la seed — decisión explícita, no herencia automática.** El brief prohíbe
copiar `20260925` sin justificar. Criterio:

- **Argumento a favor de reutilizar `20260925`:** reproducibilidad de la
  infraestructura y continuidad de la serie de experimentos.
- **Argumento en contra:** la seed es un parámetro de *análisis*, no de modelo.
  Cambiarla no cambia la ciencia; mantenerla evita que un resultado M6 se confunda
  con un re-análisis de M4/M5 bajo la misma seed.

**Decisión:** se congela una seed **nueva y distinta** en ETAPA B, y se documenta
que **no** se hereda `20260925`, con la razón: M6 cambia de estimador (recovery
fraction en energía) y de corpus de entrada; heredar la seed haría que dos
estimadores distintos parecieran comparables cuando no lo son. La seed exacta se
fija al cerrar el freeze y **no** se toca después.

---

## 25. Estado de protocolo — el corpus M6 NO es blind

```text
PROTOCOL_STATUS = EXPLAINATORY_REUSE_OF_PREVIOUSLY_OBSERVED_CORPUS
```

Este nombre significa, inequívocamente:

```text
NOT_BLIND                   = YES
NOT_FRESH_HELDOUT           = YES
EXPLANATORY_STUDY           = YES
CONFIRMATORY_HELDOUT_CLAIM  = NO
```

Los 31 assets de Cohort A fueron observados repetidamente en NP-M0, M2, M3, M4 y
M5. **M6 no los torna ciegos por decreto.** Cualquier umbral, modelo o regla que se
sintonice mirando estos 31 assets deja de ser preregistrado, por mucho que el
documento se haya escrito antes.

### 25.1 Consecuencia sobre la calibración

El brief pide evaluar si la calibración futura aporta algo más que un *smoke test*.
La respuesta honesta es **no, en términos epistemológicos**:

```text
CALIBRATION_EPISTEMIC_ROLE = IMPLEMENTATION_SMOKE_ONLY
CALIBRATION_SEPARATES_DESIGN_FROM_RESULT = NO
```

La calibración sobre assets ya vistos **no separa diseño de resultado**, porque el
resultado ya es conocido para quien escribe el código. Su valor real es:

1. **Smoke de implementación:** detectar crashes, NaN, desajustes de provenance,
   regresiones de performance, errores de unidad.
2. Detectar que un invariante sintético no se sostiene en contenido real (p.ej. que
   `nz_floor_hits > 0` aparece y rompe una identidad asumida).

**No** se presenta como validación. **No** se usa para retunear nada. Si un bug de
implementación obliga a cambiar un threshold ⇒ `STOP` (§32), igual que en M5.

> Consecuencia: la defensa real contra tuning post hoc **no** es la ceguera — es
> (§34) el **freeze antes de mirar Cohort A** y la **batería sintética** que intente
> falsar las hipótesis sin depender del corpus.

---

## 26. `LEGACY_HELDOUT` — estado honesto

Se conserva el split histórico por comparabilidad con M3/M4/M5:

```text
CALIBRATION_FAMILIES = brick, wood_planks, rock        (15 assets)
LEGACY_HELDOUT        = stone, tiles, concrete,
                        tiles_paving, ground             (16 assets)

LEGACY_HELDOUT_IS_INDEPENDENT_VALIDATION = NO
LEGACY_HELDOUT_PERMITTED_USE = DESCRIPTIVE_DIRECTIONAL_STABILITY_ONLY
LEGACY_HELDOUT_BLIND_CONFIRMATION_CLAIM = NO
```

Los 16 assets de `LEGACY_HELDOUT` **ya fueron usados en M4** y quedan cubiertos por
la desviación de protocolo ya registrada en M5 §18
(`UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE`). M6 **hereda y no reescribe**
esa desviación. Reutilizar el split da continuidad; no da independencia.

---

## 27. Resolución

```text
PRIMARY_RESOLUTION = 512
SECONDARY_1024     = DEFERRED
```

M6 **no reabre** el problema 1024 (resuelto en M5 §22.4 con
`SECONDARY_SCOPE_AMBIGUITY=RESOLVED_FAIL_CLOSED`). Si en el futuro se ejecuta un
secondary 1024, debe tener **tipo machine-readable distinto desde el principio** —el
mismo patrón `validate_decision_scope` de M5— para que un `summary.decision` en 1024
sea imposible por construcción, no por convención.

---

## 28. Vocabulario de decisión preliminar (SIN thresholds aún)

Las categorías son **mutuamente excluyentes, exhaustivas y deterministas**. Los
thresholds numéricos se derivan **sólo** de teoría + sintéticos, en ETAPA B.

```text
EXP_M6_NONINTEGRABILITY_DOMINANT
EXP_M6_LOW_DIMENSIONAL_RECONCILIATION_SUPPORTED
EXP_M6_PAIR_GEOMETRY_MISMATCH_PERSISTS
EXP_M6_MIXED
EXP_M6_DATA_INSUFFICIENT
```

### 28.1 Estados de no-evaluabilidad (preceden a toda decisión)

```text
EXP_M6_NOT_EVALUABLE                  exceso del par bajo el gate en demasiados assets
EXP_M6_INVALIDATED_BY_UPSTREAM_BUG    un bug de M0–M5 cambia la base
```

Ambos son **fail-closed**: si aplican, no se emite decisión científica. Es el mismo
patrón que M4 §6 y M5 §14.

### 28.2 Por qué no hay un "SUCCESS" de producto

Ninguna de las cinco categorías científicas significa "se puede shippear". M6
responde una pregunta **explicativa** sobre un corpus **no ciego**, con un **oráculo**
en el bucle. Cualquier categoría que se lea como recomendación operativa sería una
sobrelectura del diseño, que es exactamente lo que §23 prohíbe.

---

## 29. Árbol de decisión (formal, sin thresholds numéricos)

Dos ejes, ambos definidos **antes** del corpus:

```text
N        := mediana per-asset de NONINTEGRABLE_EXCESS
R_simple := RECOVERY_FRACTION del mejor modelo low-dimensional permitido (M4)
R_resid  := 1 − R_simple
```

```text
        #NOT_EVALUABLE > límite  →  EXP_M6_NOT_EVALUABLE        (fail-closed)
        integridad G0 falla     →  EXP_M6_DATA_INSUFFICIENT    (fail-closed)

        de lo contrario, sobre (N, R_simple):

        ┌──────────────────────┬──────────────────────────────┐
        │  N ≥ T_N             │  N < T_N                     │
        ├──────────────────────┼──────────────────────────────┤
        │ R_simple ≥ T_R       │ R_simple ≥ T_R              │
        │ → EXP_M6_MIXED       │ → EXP_M6_LOW_DIMENSIONAL_    │
        │                      │     RECONCILIATION_SUPPORTED │
        ├──────────────────────┼──────────────────────────────┤
        │ R_simple < T_R       │ R_simple < T_R              │
        │ → EXP_M6_NONINTEGRABILITY_DOMINANT                   │
        │                      │ → EXP_M6_PAIR_GEOMETRY_      │
        │                      │     MISMATCH_PERSISTS        │
        └──────────────────────┴──────────────────────────────┘
```

`T_N` y `T_R` se derivan en ETAPA B **exclusivamente** de la batería sintética:

- `T_R`: separa "S1/S3/S5 (mecanismo inyectado, recovery alto)" de "S4 (geometría
  distinta, recovery bajo)". Si S4 y S5 se solapan ⇒ `STOP`.
- `T_N`: separa "S0/S6 (coherente, ≈0)" de "S2 (mezcla con `f` conocido)", con
  margen sobre el piso Q8 medido en S6.

**`R_simple` se toma del mejor modelo de la familia cerrada (§16), no de un `min`
sobre una familia abierta.** No hay `min(error)` sobre variantes ilimitadas (§30).

---

## 30. Sin *best-of-many*: selección de modelo explícita

El riesgo que el brief señala es real: `min(error across 20 variants)` introduce
**ventaja de selección** — con suficientes variantes, alguna gana por azar y el
resultado se lee como señal.

**Reglas preregistradas:**

1. **Familia cerrada y enumerada**: exactamente M0, M1, M2, M3, M4 (§16). No se
   añaden M5/M6/M7 después de ver datos.
2. **DOF declarados por modelo**: `M0=0, M1=0, M2=2, M3=1, M4=3`. La escala afín
   global de M4 **no cuenta** (§10) porque es invariante, no un grado de libertad
   nuevo.
3. **Orden de evaluación fijo**: `M0 → M1 → M2 → M3 → M4`, reportando **todos**,
   sin ocultar los peores. No se reporta sólo el ganador.
4. **Criterio de selección determinista y escrito**:

   ```text
   R_simple = RECOVERY_FRACTION(M4)      el modelo más complejo permitido
   ```

   Se usa **M4 fijo**, no el argmax sobre la familia. Esto es deliberado: tomar el
   argmax sobre 4 candidatos sería *best-of-4*, que es exactamente el sesgo que se
   quiere evitar. El modelo más complejo es el que, **si** funciona, más se acerca
   a "existe una reconciliación simple"; y su desempeño está acotado por S4.
5. **Los modelos individuales se reportan siempre**: `R_M2` y `R_M3` aparecen en el
   JSON aunque no entren en la decisión, para que un lector pueda descomponer el
   efecto sin rerun.

**Sobre el riesgo de overfitting de M4 con 3 DOF:** se acota con S4 (§20.2), no con
penalización a posteriori. Una penalización elegida después de ver resultados es
justo el tipo de Degrees of Freedom que este prereg intenta cerrar.

---

## 31. Parsimonia — registro exacto

| Modelo | DOF ajustados | DOF contados para parsimonia | Nota |
|---|---:|---:|---|
| M0 | 0 | 0 | baseline AUTH de M4 |
| M1 | 0 | 0 | Hodge: `R ≡ 0` por construcción (§16.1) |
| M2 | 2 | 2 | `(dx, dy)` |
| M3 | 1 | 1 | `β` |
| M4 | 3 | 3 | `(dx, dy, β)` |
| **TOTAL_PRIMARY_FREE_PARAMETERS** | | **3** | |

No hay offset, ni ganancia global, ni per-band gains, ni spline. La complejidad no
supera la necesaria: si M4 no supera a M2, el reporte dirá que el término `β` no
aportó, y eso es un resultado negativo válido.

---

## 32. Protocolo anti-leakage — secuencia obligatoria

El brief prohíbe saltar de diseño a FULL. La secuencia es **lineal y cada etapa
produce un artefacto identificable**:

```text
A. DISEÑO                     ← ESTE COMMIT (M6_DESIGN_SHA)
B. IMPLEMENTAR MATEMÁTICA PURA  (módulo puro, sin corpus, sin runner)
C. FALSACIÓN SINTÉTICA         (S0–S7, A0–A14, invariantes, mutaciones)
D. THRESHOLDS DESDE TEORÍA     (T_N, T_R, G, T_AMBIG, tolerancias, seed)
E. PREREG FREEZE               ← M6_PREREG_FREEZE_SHA
F. CALIBRATION SMOKE           (sólo si está justificado; §25.1)
G. EXECUTION FREEZE            ← M6_EXECUTION_FREEZE_SHA
H. FULL EXPLANATORY RUN        (31 assets, resolución 512)
```

**Reglas de transición:**

- **D antes de C no.** Los thresholds salen de la batería sintética, nunca al revés.
- **E antes de F.** Sin freeze no se toca ningún asset real.
- **Si un bug de implementación exige cambiar un threshold ⇒ STOP.** No se
  "corrige" el threshold; se corrige el código y se re-emite el freeze.
- **Si F revela que un invariante sintético no se sostiene** (p.ej. `nz_floor_hits`
  aparece en assets reales), eso es información legítima de contenido: se registra
  y se **no** retunea.
- **H sólo después de G**, con provenance verificada.

---

## 33. Provenance y requisitos cross-worktree

### 33.1 Provenance (fail-closed, igual que M5 §16.1)

Cada JSON de M6 registra, como **inputs del operador** y nunca heurísticas de
runtime:

```text
git_sha                    checkout real de la corrida
base_main_sha              --base-main-sha
m6_prereg_freeze_sha       --prereg-freeze-sha  (= M6_PREREG_FREEZE_SHA)
m6_execution_freeze_sha    derivado de --frozen-ack
frozen_ack                 freeze-<40hex>
m6_design_sha              (= M6_DESIGN_SHA, este commit)
protocol_status            (§25)
```

Formato exigido: **exactamente 40 hex minúsculos** (`^[0-9a-f]{40}$`), sin normalizar.
En calibración `--frozen-ack` se **rechaza** (declararlo sería provenance fiction).
Además: git SHA del manifest M3, SHA256 de los módulos de matemáticas M6, resolución,
seed, thresholds, `ρ_ref`, y versiones Python/NumPy/Pillow. `json.dump(...,
allow_nan=False)` y fail-fast ante NaN/Inf.

### 33.2 Cross-worktree — riesgo heredado, NO resuelto

M5 §19.4 documentó contaminación real de imports: el intérprete resolvía `sky_claw`
hacia **otro** checkout por entradas `.pth` del venv, produciendo números plausibles
de otra revisión sin error visible.

```text
CROSS_WORKTREE_IMPORT_RISK = CONFIRMED (heredado de M5; NO corregido)
ISSUE_667_MIXED_IN = NO   (no se tocan .pth / packaging en este slice)
```

**Requisito obligatorio para el runner M6 (ETAPA B+):** invocación como **módulo**
(`python -m ...`), cwd en el worktree correcto, y verificación de
`<módulo>.__file__` dentro del worktree exacto del freeze para **todos** los módulos
de `sky_claw/local/native_parallax/research/` (los de M0–M5 más los nuevos de M6).

**Prohibido** `python <ruta>\script.py`. Si algún módulo resuelve a otro checkout:

```text
STOP
CROSS_CHECKOUT_IMPORT_CONTAMINATION = YES
FULL_RUN_EXECUTED = NO
```

Esto es gate de **integridad operativa**, no cambio científico: no altera thresholds,
bandas, seed, métricas, splits ni reglas.

---

## 34. Issues fuera de scope

```text
ISSUE_667_MIXED_IN = NO
ISSUE_663_MIXED_IN = NO
```

- **#667** menciona código que M6 podría reutilizar (`nz_policies.py`,
  `authored_dataset.py`, `metrics.py`, `ParallaxR`, benchmark E6). **No se arregla
  aquí.** Antes de implementar M6 hay que hacer **collision review** de esos símbolos,
  pero ese trabajo pertenece a su propio slice.
- **#663** (`uv.lock`, estructura de dependencias) queda intacto; este slice es
  docs-only.

---

## 35. STOP conditions (`STOP_M6_DESIGN` / `STOP_M6_EXECUTION`)

Se emite `STOP_M6_DESIGN` si ocurre cualquiera de:

```text
1. Hodge resulta redundante Y no queda una hipótesis clara
2. El bound de registro no puede fijarse sin Cohort A
3. El modelo espectral necesita muchos DOF para ser significativo
4. Los casos sintéticos no distinguen las hipótesis
5. RECOVERY_FRACTION no es numéricamente estable
6. El modelo requiere alterar el solver M4
7. Cualquier threshold requiere observar datos authored
8. #667 cambia una primitiva científica usada por M6
9. Cualquier persona/agente ejecuta M6 sobre el corpus antes del freeze
```

**Estado actual de cada una (honesto, pre-implementación):**

| # | Condición | Estado tras el diseño |
|---|---|---|
| 1 | Hodge redundante sin alternativa | **EVITADA** — Hodge se degrada a eje diagnóstico; H1 sigue siendo medible y no-redundante frente a `curl_proxy` (§7.4) |
| 2 | Bound de registro requiere Cohort A | **EVITADA** — se adoptó el toro completo, que no requiere criterio externo (§12.4) |
| 3 | Espectro necesita muchos DOF | **EVITADA** — 1 DOF radial anclada, parsimonia demostrada (§15) |
| 4 | Sintéticos no distinguen | **ABIERTA** — se cierra en ETAPA C/D; el diseño ya incluye asimetrías que la hacen falsable (S7, S4) |
| 5 | Recovery inestable | **PARCIAL** — el gate está diseñado (§18) pero `G` se congela en ETAPA D |
| 6 | Requiere alterar solver M4 | **EVITADA** — shift post-solver, conmutación demostrada (§11.5) |
| 7 | Threshold requiere datos authored | **CUMPLIDO** — todos los thresholds derivan de teoría+sintéticos |
| 8 | #667 cambia una primitiva | **ABIERTA** — collision review pendiente, slice propio |
| 9 | Ejecución antes del freeze | **CUMPLIDO** — este slice no toca el corpus |

**`STOP_M6_EXECUTION`** adicional, si en la corrida aparece cualquiera de:

```text
Un módulo M6 resuelve fuera del worktree del freeze
El corpus no es adquirible o falla integridad de hashes
Aparece NaN/Inf donde el contrato dice fail-fast
El número de assets usables cae bajo el gate de cohorte
Un bug de implementación exigiría cambiar un threshold (no se re-tunea: se corrige)
```

---

## 36. Separación Hodge ↔ recovery (obligatoria, §37 del brief)

> Hodge puede indicar que una parte del campo normal **no puede provenir de ningún
> height**. Eso **NO** implica que esa energía sea la causa del RMSE contra el target.

**Prohibido reportar:**

```text
NONINTEGRABLE_FRACTION = "porcentaje del mismatch del par explicado"
```

Esa igualdad no está derivada y sería falsa. Son dos ejes distintos:

| Eje | Objeto | Dominio | Qué significa |
|---|---|---|---|
| `NONINTEGRABLE_FRACTION` | `g_N` (campo de pendientes de la normal) | **gradiente** | cuánta energía de `g_N` está fuera del subespacio de gradientes |
| `RECOVERY_FRACTION` | `H` (altura reconstruida) | **altura** | cuánta energía del error contra `H` recupera un modelo |

La conexión entre ambos es **una cota, no una igualdad**, y es derivable:

Como el residuo de gradiente del camino M0 es

```text
g_H − Π∥ g_N  =  (g_H − g_N)  +  g_⚥
```

y `(a + b)² ≤ 2a² + 2b²`, se obtiene la cota verificada numéricamente:

```text
||g_H − Π∥g_N||²  ≤  2·||g_H − g_N||²  +  2·||g_⚥||²

verificado: ||resid||² = 2.122478e+07
            cota       = 9.346676e+07        se cumple: True
```

**La cota es holgada (factor ~4.4 en esa realización) y no puede dar una fracción de
recovery**: para convertirla en energía de altura haría falta la inversa del operador
de Poisson, que amplifica modos de baja frecuencia de forma no uniforme. Por eso M6
**no** reporta un "recovery due to non-integrability".

> M6 reporta ambas magnitudes por separado y **no** las combina en un número único.
> El árbol de decisión (§29) las usa como **ejes ortogonales**, nunca como una suma.

**Y el recordatorio de §38, ya verificado:** un gradiente exacto da
`NONINTEGRABLE = 1.099e−31` mientras el height subyacente es completamente distinto.
**Baja no-integrabilidad no implica coherencia del par.**

---

## 37. Revisión matemática interna adversarial (§48)

Antes de commitear, el diseño se atacó a sí mismo. **No se defiende por inercia**: los
ataques que prosperaron cambiaron el documento.

| Ataque | Veredicto | Qué cambió |
|---|---|---|
| **Hodge es redundante** | **PROSPERÓ (parcialmente)** | El solver M4 ya es la proyección de Frankot–Chellappa (`solver(g)` vs `solver(Π∇g)`: 1.4e−17). Se eliminó M1 como peldaño de recovery (§16.1) y Hodge se redefinió como **eje diagnóstico**. **Este fue el cambio de diseño más grande.** |
| **Registro es un oráculo demasiado poderoso** | **PROSPERÓ** | Consecuencias reales: (a) `REGISTRATION_PRIMARY=YES` con la ambigüedad como estado obligatorio (§13); (b) `|δ|` grande **no** se lee como "misma geometría"; (c) S4 es el control que lo acota; (d) el argumento de "es un oráculo" se vuelve **explícito** en la etiqueta `EXPLANATORY_ORACLE` (§23). |
| **Spectral tilt duplica escala** | **REFUTADO con medición** | El ajuste conjunto `(β, s)` con escala afín libre devuelve `β̓ = β_inyectado` y `s = 1.000000` con residuo nulo (§22). La familia anclada §14.2 **no** se confunde con la escala. |
| **M4 combinado overfittea** | **PROSPERÓ** | Se usó **M4 fijo**, no `argmax` sobre la familia — `argmax` sobre 4 candidatos *es* best-of-4 (§30). Se añadió la prohibición de penalización post hoc. |
| **Recovery fraction inestable** | **PROSPERÓ (parcialmente)** | El denominador pequeño es un riesgo real: se añadió `PAIR_EXCESS_ENERGY_GATE` con `RECOVERY_FRACTION_NOT_EVALUABLE` (§18). **Residual abierto:** `G` se congela en ETAPA D. |
| **Periodicidad fabrica shifts falsos** | **PROSPERÓ** | Se verificó que el argmax es exacto y único en contenido no repetitivo (margen 1.26e−1, 1 equivalente) pero **exactamente degenerado** en tiles repetidos (hasta 256 equivalentes). De ahí sale `REGISTRATION_AMBIGUOUS` como **estado**, no threshold (§13). |

### 37.1 Tres defectos del borrador que la verificación numérica cazó

Estos **no** eran opinions: eran bugs de convención que habrían producido números
plausibles y equivocados.

| # | Defecto | Síntoma | Corrección |
|---|---|---|---|
| 1 | Signo del shift de Fourier | `R = 0.031` donde debía ser `1.000` | `exp(−2πi k s / N)`; `np.roll(f,s)[x] = f[x−s]` (§11.2) |
| 2 | Orden de la correlación cruzada | `δ̓ = −δ` | `conj(F{recon}) · F{target}`, en ese orden (§11.3) |
| 3 | Anchor de la familia espectral | `G(ρ_ref) = 0.707 ≠ 1` → duplicaba la escala | `G_β(ρ) = ((1+(ρ/ρ_ref)²)/2)^{−β/2}` (§14.2) |

Un cuarto defecto era del **test**, no del modelo: la mezcla "known fraction" con
`α²/(α²+(1−α)²)` falla porque la fracción inyectada debe definirse **en energía**
(§7.3, verificado 0.400000000000 contra target 0.40).

### 37.2 Un bug real encontrado en la base (NO se arregla aquí)

`freq_axes` anula la derivada en Nyquist. Eso es correcto para el solver, pero hace
que sus ejes sean **inservibles** para construir `ρ` o una fase de desplazamiento:
se corrompen 127 de 4096 bins y el shift falla con error 5.3e−2 (§4.2). M6 lo sortea
usando `frequency_coherence.rho_grid` y la rejilla completa. **No se modifica
`normal_fft_periodic.py`**: es la base congelada de M0–M5, y tocarla invalidaría
M4/M5. Se documenta como restricción, con test de mutación.

---

## 38. Write-set futuro (ETAPA B, aún NO en este commit)

```text
Por crear (NO en este slice):
  sky_claw/local/native_parallax/research/pair_decomposition.py   matemática pura
  sky_claw/local/native_parallax/research/run_exp_m6.py           runner
  tests/test_native_parallax_exp_m6.py                            batería S0–S7, A0–A14
  (docs: sección de resultados, en commit POST-corrida separado)
```

**En este slice: exactamente UN archivo.**

```text
docs/design/research/native-parallax/exp-m6-pair-mismatch-decomposition.md
```

`CODE_CHANGED = NO` · `TESTS_CHANGED = NO`

---

## 39. Gate de corpus real (declaración obligatoria antes de commit)

Este slice **no** ejecutó ningún runner sobre Cohort A. No se corrieron
`prepare_entries`, `run_exp_m4`, `run_exp_m5` ni ningún runner M6. No se abrieron los
JSON por asset de M4/M5 para orientar el diseño. Las únicas cifras de M4/M5 usadas
son las **agregadas ya publicadas** (§1), usadas como antecedente, no como
observación nueva.

La verificación matemática de §37 se hizo **exclusivamente sobre campos sintéticos**
generados con RNG de semilla fija, usando las primitivas públicas del repo. No tocó
ningún asset.

```text
REAL_CORPUS_TOUCHED              = NO
M6_REAL_METRICS_COMPUTED         = NO
M6_PARAMETERS_FIT_TO_REAL_ASSETS = NO
M6_THRESHOLDS_TUNED_ON_REAL_DATA = NO
```

**Nótese la diferencia con M5:** en M5 la batería sintética era la barrera que
separaba el freeze de la corrida. Aquí además hay que decir que el corpus ya fue
visto (§25), así que el freeze y la batería sintética —no la ceguera— son la barrera
real.

---

## 40. Estado de este documento

```text
M6_DESIGN_SHA           = (el commit que introduce este documento)
M6_PREREG_FREEZE_SHA    = NO  (viene después de ETAPA B/C/D)
M6_EXECUTION_FREEZE_SHA = NO
M6_REAL_RUN_EXECUTED    = NO
```

Este documento es **ETAPA A**. Los valores numéricos que faltan
(`T_N`, `T_R`, `G`, `T_AMBIG`, tolerancias de identabilidad, seed de bootstrap) **no**
se completan aquí: se derivan y congelan en ETAPA D (§32), y su ausencia es
deliberada, no una omisión.

---

## 41. Resumen ejecutivo del diseño

```text
1. Hodge NO es un mecanismo de reconciliación: el solver M4 ya ES la proyección
   (1.4e-17). Sobrevive como EJE DIAGNÓSTICO normal-only, que es lo único que M6
   aporta y que ningún output de M4/M5 expone.

2. La no-integrabilidad NO es una fracción del mismatch explicado. Son dos dominios
   distintos (gradiente vs altura); la relación es una COTA holgada, no una igualdad.
   Y un campo integrable puede describir un height totalmente distinto (S7).

3. El registro periódico SÍ es primario, pero su bound no puede elegirse mirando el
   corpus: se adoptó el toro completo, que es el grupo del modelo y no requiere
   criterio externo. Su punto débil (degeneración en tiles repetidos) se convierte en
   ESTADO obligatorio, no en threshold.

4. La familia espectral es 1 DOF y ANCLADA en ρ_ref = 32 (heredado de M5). El
   borrador sin anchor duplicaba la escala global; la versión anclada no lo hace
   (verificado: ajuste conjunto (β, s) devuelve β̓ = β, s = 1.000000).

5. La recovery fraction es en ENERGÍA, no RMSE; no se clampea; tiene gate de
   denominador; y la unidad estadística es el ASSET con agregación apareada.

6. M6 es un estudio EXPLICATIVO sobre un corpus YA OBSERVADO. No es validación ciega,
   no es un estimador desplegable, y no autoriza ninguna decisión de producto.
```
