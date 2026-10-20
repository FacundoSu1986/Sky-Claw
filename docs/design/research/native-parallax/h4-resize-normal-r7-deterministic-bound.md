# H4 `resize_normal` — Cota determinista del error angular (R7)

Investigación `H4_R7_DETERMINISTIC_BOUND_RESEARCH`, **reparada** en
`H4_R7_PROOF_REPAIR_AND_PUBLICATION` y **corregida documentalmente** en
`H4_R7_DOCUMENTARY_CORRECTION`. **No** modifica producción, **no** cambia
`DEGENERATE_THRESHOLD` y **no** declara cierre normativo. Corpus real **no** usado (§9).

> **Estado vigente (2026-10-19, tras el merge de #723 en `7150a47b`).** El operador ratificó
> F01 = R2-lite y aceptó que R7 siga siendo una heurística RMS (`DEGENERATE_THRESHOLD = 5/16`)
> hasta el censo por asset del Slice 3 (respuesta textual: «Si autorizo»); además autorizó el merge
> de #723, que ya está en `main`. Lo que ese merge **no** levanta: el bloqueo de M6 y la derivación
> numérica de R7 (`R7_NUMERIC_DERIVATION = BLOCKED`). Este documento sigue sin cambiar producción ni
> el umbral, y su propio merge (#733) no está autorizado. El bloque de tokens del final (§12) y las
> menciones a `PENDING_TECH_LEAD`, a `H4_CODE_MERGE_AUTHORIZED = NO` o a #723 como no fusionado son
> **fotos del PR en el momento de su registro** y se conservan como historial: donde digan eso,
> rige este bloque.
>
> ```text
> F01_CONTRACT_AMENDMENT    = RATIFIED          (operador, 2026-10-19)
> H4_CODE_MERGE_AUTHORIZED  = YES               (#723 fusionado en 7150a47b, con autorización del operador)
> R7_THRESHOLD_KIND         = HEURISTIC (RMS)   (aceptado por el operador; sin cambio de código)
> R7_NUMERIC_DERIVATION     = BLOCKED           (SIN CAMBIO)
> M6_IMPLEMENTATION_BLOCKED = YES               (SIN CAMBIO: espera el roster NON_EVALUABLE y el censo del Slice 3)
> ```

> **Corrección 1 (respecto de la primera versión).** La versión previa usaba
> `delta = (q/2)*sqrt(3)` como cota del **desplazamiento** por píxel, es decir invocaba
> `norm(n' - n) <= delta`. **Esa desigualdad es falsa**: la renormalización por píxel no es
> no-expansiva en la métrica de cuerda, y el desplazamiento real llega a
> `delta_chord = 2*sin(asin(delta)/2) = 0.0067923952805078 > delta = 0.0067923561081132`.
> No era un redondeo conservador: era un paso inválido.
>
> **Corrección 2 (auditoría independiente `#issuecomment-6157400176`).** Tres afirmaciones
> documentales eran incompatibles con el cálculo y se corrigen acá:
> **R7-REV-01** una fila de la tabla de §4 mezclaba el `atan` de `L = 0.866` con la etiqueta
> `L = 0.8660392772765431`, de modo que lo «alcanzable» parecía **exceder** la cota probada
> (en realidad `cota(L*) - atan(delta/L*) = 2.864523e-07 > 0`); **R7-REV-02** la construcción
> del vértice **no** alcanza `delta_chord` en norma de `dv` (mide `0.0067922245` en `L = 0.80`,
> por **debajo** de `delta`), así que la cota sólo acota ese desplazamiento, no lo iguala;
> **R7-REV-03** `0.8660393` es el cruce de una **cota superior**, no un peor caso exacto
> demostrado, y el token se renombra en consecuencia. Las tres correcciones están ancladas por
> tests discriminantes (§7 y §12).

## 0. Procedencia y alcance

| Campo | Valor |
|---|---|
| Worktree de la investigación | `E:/Skyclaw_Main_Sync/.worktrees/h4-r7-bound-research` (rama plana) |
| Worktree de la reparación y corrección | `E:/Skyclaw_Main_Sync/.worktrees/h4-r7-bound-review` (rama plana) |
| Base | `802ff271` (head remoto del PR #723), **sin** merge de `origin/main` |
| Contratos leídos | #711 (`h4-resize-normal-preflight.md`), #717 (`h4-resize-normal-authorization-gate.md` §6/§6.1), #723 (`h4-resize-normal-contract-reconciliation.md` §2) |
| Overlap de `origin/main` sobre el write-set | `NATIVE_PARALLAX_BASE_OVERLAP=NO` (sólo cambió `h4-resize-normal-authorization-gate.md`, ya presente en la base) |
| Artefactos | `tests/test_native_parallax_h4_r7_bound.py` + este documento |
| Dictámenes que motivan las correcciones | `#issuecomment-6155907503` (PR #723), `#issuecomment-6157400176` (PR #733) |

Alcance: derivar, si es demostrable, una cota **conservadora** del error angular bajo
**todas** las perturbaciones admitidas por el modelo declarado, sin presuponer independencia
entre píxeles, y falsarla. La decisión de adoptar un umbral es del Tech Lead.

## 1. El modelo declarado (lo que se cuantifica)

Del comentario de producción (`authored_dataset.py`, bloque `DEGENERATE_THRESHOLD`) y de §6.1:

```text
q        = 2/255                     paso de la retícula del decodificador 8-bit
eps      in [-q/2, q/2]^3            error por canal, POR PÍXEL
n'       = normalize(n + eps)        el vector perturbado se RENORMALIZA
```

**El modelo no exige independencia entre píxeles.** Ese es el punto: una perturbación
*coherente* (la misma en todos los píxeles) es admisible, y los pesos del kernel bilineal son
positivos y suman 1, así que una perturbación coherente **suma**, no se promedia.

## 2. Los cinco objetos, separados

| # | Objeto | Qué es | Cómo entra en la cota |
|---|---|---|---|
| 1 | Incertidumbre de entrada | la normal verdadera no se conoce; la decodificada está en su celda | fija el **conjunto** de `eps` admisibles |
| 2 | Error de cuantización | `abs(eps_c) <= q/2` por canal | `eps in [-q/2, q/2]^3`, cota **por canal**, no por norma |
| 3 | Renormalización **por píxel** | `n' = normalize(n + eps)` | convierte el error de canal en una **deflexión** `delta_i = n'_i - n_i` |
| 4 | Interpolación bilineal | combinación convexa de `n'_i` con pesos `w >= 0`, `sum w = 1` | `norm(dv) <= max_i norm(delta_i)` (no hay amplificación) |
| 5 | Renormalización **de salida** | `v' / norm(v')` | **no** cambia la dirección: `delta_theta` se mide entre direcciones |

La distinción crítica es 2 vs 3 vs 5: `q/2` es una cota **de canal**; `delta_i` es un
**desplazamiento** por píxel; y la cota angular sólo aparece tras dividir por `L`.

### 2.1 Tres cantidades que NO son la misma

| Cantidad | Definición | Valor | Qué acota |
|---|---|---|---|
| `delta` | `(q/2)*sqrt(3)` | `0.0067923561081132` | la **perturbación**: `sup norm(eps)` sobre el cubo |
| `delta_chord` | `2*sin(asin(delta)/2)` | `0.0067923952805078` | el **desplazamiento**: `sup norm(n' - n)` por píxel |
| `delta_theta` | ángulo entre `v` y `v'` | depende de `L` | lo que mide el criterio vigente |

`delta_chord > delta` (ratio `1.0000057671290952`, exceso `3.917239453556792e-08`). Confundir
las dos primeras es el defecto de la primera versión; confundir la tercera con las anteriores
es el defecto que este slice sigue declarando abierto (§10).

## 3. Derivación

### 3.1 Perturbación máxima: `delta = (q/2)*sqrt(3)`

El conjunto admisible es el cubo `[-q/2, q/2]^3`. Su norma máxima se alcanza en un **vértice**:

```text
delta = (q/2) * sqrt(3) = 0.0067923561081132      con eps* = (+-q/2, +-q/2, +-q/2)
```

**Nótese el `sqrt(3)`, no `q/2`.** Esto es una cota de la **perturbación**, no del
desplazamiento.

### 3.2 Desplazamiento por píxel: la cuerda `delta_chord`

Sea `n` unitaria, `eps = delta*sigma` con `sigma = (1,1,1)/sqrt(3)`, y `a = <sigma, n>`.
Entonces `n + eps = (a + delta)*sigma + sqrt(1-a^2)*w` con `w` unitaria perpendicular a
`sigma`, y

```text
tan(theta) = delta * sqrt(1 - a^2) / (1 + a*delta)
```

El máximo está en **`a = -delta`** (la derivada de `tan^2` se anula allí y cambia de signo).
En ese punto `sin(theta*) = delta`, esto es `theta* = asin(delta)`, y la **cuerda** vale

```text
delta_chord = 2 * sin(theta*/2) = 2 * sin(asin(delta)/2) = sqrt(2*(1 - sqrt(1 - delta^2)))
            = 0.0067923952805078
```

Verificado de tres maneras independientes:

1. **Cerrada**, con la construcción explícita `<sigma, n> = -delta`, `eps = (q/2)*(1,1,1)`:
   la cuerda medida coincide con `delta_chord` a precisión de máquina.
2. **Barrido determinista** de 4000 direcciones (espiral de Fibonacci) × 8 vértices del cubo:
   el máximo alcanzado es `0.999999999994759 * delta_chord`, sin superarlo.
3. **Grilla fina** sobre `a in [-1, 1]` (20001 puntos): ningún punto supera `delta_chord`.

**Consecuencia:** `norm(n' - n) <= delta` es **FALSA**, y `norm(n' - n) <= delta_chord` es la
cota correcta.

### 3.3 Propagación por la interpolación

El filtro bilineal de Pillow es una combinación convexa por píxel de salida: pesos `w >= 0`,
`sum w = 1`, también con la ventana **recortada del borde** (los pesos se renormalizan). Por lo
tanto, para cada píxel de salida:

```text
norm(dv) = norm( sum_i w_i * delta_i )  <=  sum_i w_i * norm(delta_i)  <=  max_i norm(delta_i)  <=  delta_chord
```

Esta desigualdad **no usa independencia**: vale para cualquier correlación entre píxeles,
incluida la coherencia total. Es la respuesta al punto 3 del brief. Es una **cota superior** de
`norm(dv)`: no afirma que `delta_chord` se alcance tras la mezcla (§4).

### 3.4 Cota angular: de `norm(dv)` a `delta_theta`

Este paso **no** es inmediato y la primera versión lo saltaba. Sea `v' = v + dv` con
`norm(v) = L`. Por la ley del seno en el triángulo `(v, dv, v')`:

```text
sin(delta_theta) = norm(dv) * sin(alpha) / norm(v') ,   alpha = angle(v, dv)
norm(v')^2 = L^2 + norm(dv)^2 + 2*L*norm(dv)*cos(alpha)
```

Con `d = norm(dv)` fijo, maximizar `sin(alpha) / norm(v')` en `cos(alpha)` da un único óptimo
interior en

```text
cos(alpha*) = -d / L          =>      norm(v') = sqrt(L^2 - d^2)      =>      sin(delta_theta*) = d / L
```

es decir `delta_theta <= asin(d / L)`, con igualdad en `cos(alpha) = -d/L` (el desplazamiento
**inclinado hacia atrás**). El caso perpendicular (`cos(alpha) = 0`) da `atan(d/L)`, que es
**estrictamente menor**. Por lo tanto:

```text
delta_theta  <=  asin( norm(dv) / L )  <=  asin( delta_chord / L )
```

Sin el paso `cos(alpha*) = -d/L` la cota sólo valdría si `norm(v') >= L`, que **no** se cumple
en general: con `d = 0.0068` y `L = 0.866` la diferencia entre `asin(d/L)` y `asin(d/norm(v'))`
es del orden de `1e-5`. El test lo fija barriendo `alpha` (no basta con el caso perpendicular).

**Umbral suficiente:**

```text
L_SUFICIENTE = delta_chord / sin(q_ang) = 0.8660392772765431
```

Verificado **independientemente** por bisección (no copiado como constante de autoridad):
la bisección devuelve `0.8660392772765431`, que coincide con el valor del dictamen.
`sqrt(3)/2 = 0.8660254037844386` queda `1.387349210457689e-05` **por debajo**: el umbral no es
exactamente `sqrt(3)/2`, y el corrimiento es `(1 + q^2/6) * (1 + 5.767e-06)`.

**Requisito 9 del brief, explícito:** `norm(eps) <= (q/2)*sqrt(3)` es una cota de
**perturbación**; la cota **angular** es `asin(delta_chord/L)`. No son el mismo objeto y no se
confunden: la primera vale `0.0068` en unidades de vector, la segunda `0.0078` rad en
`L = 0.866`.

## 4. La cota no es vacua, pero es conservadora y NO tight

**Construcción cerrada (alcanzable).** Con `sigma = (1,1,1)/sqrt(3)` (diagonal del cubo),
`v_hat, u_hat` una base ortonormal del plano **perpendicular** a `sigma`, y

```text
n_a = L*v_hat + sqrt(1-L^2)*u_hat
n_b = L*v_hat - sqrt(1-L^2)*u_hat
eps_a = eps_b = (q/2) * (1,1,1)          (vértice del cubo, perpendicular a n_a y n_b)
```

el campo periódico-2 tiene interpolado limpio `v = L*v_hat` (norma **exacta** `L`) y la salida
perturbada cumple, **exactamente**:

```text
v'  = (v + eps) / sqrt(1 + 3*(q/2)^2)
delta_theta = atan( delta / L )
norm(dv)^2  = L^2*(1/sqrt(1+delta^2) - 1)^2 + delta^2/(1+delta^2)
```

El cierre de `norm(dv)` vale porque en esta construcción `<eps, v> = 0`; se verificó contra la
medición numérica con ratio `1.000000000000002`. Verificado a 12 dígitos significativos
(`ratio = 1.000000000000`) para `L ∈ {0.55, 0.70, 0.80, 0.866, 0.95, 1.0}`, y por
**refinamiento local**: el óptimo vuelve a `eps = (q/2)*(1,1,1)`. La búsqueda aleatoria (4000
muestras de `(eps, n_a, n_b)` por cada `L`) **nunca** supera `atan(delta/L)`.

**`norm(dv)` de la construcción queda POR DEBAJO de `delta`** (y por lo tanto de
`delta_chord`): `0.0067922113` en `L = 0.55`, `0.0067922245` en `L = 0.80`, `0.0067922386` en
`L = 1.0`. La brecha contra `delta_chord` es `1.6e-07 .. 1.8e-07`. Es la corrección
**R7-REV-02**: la construcción alcanza `atan(delta/L)` — una **cota inferior** del peor caso,
porque la construcción es admisible — pero **no** alcanza la cota superior.

**Conservadurismo (tabla recalculada fila por fila con SU PROPIO `L`):**

| `L` | cota `asin(delta_chord/L)` | alcanzable `atan(delta/L)` | brecha (rad) | brecha / `q_ang` |
|---|---|---|---|---|
| `0.55` | `0.0123501235500970` | `0.0123491105914459` | `1.012959e-06` | `1.291522e-04` |
| `0.80` | `0.0084905961150940` | `0.0084902411251954` | `3.549899e-07` | `4.526121e-05` |
| `0.8660392772765431` | `0.0078431372549020` | `0.0078428508026350` | `2.864523e-07` | `3.652266e-05` |
| `1.00` | `0.0067924475113009` | `0.0067922516533944` | `1.958579e-07` | `2.497188e-05` |

La brecha es `<= 1.02e-06` rad sobre `L in [0.55, 1.0]` y **decrece** al crecer `L`. Se declara
en vez de esconderse: la cota es **válida pero no tight**, y `atan(delta/L)` **no** es por sí
sola una cota superior universal. **La tabla es manual y por eso tiene un gate que la
recalcula entera** (§7): el defecto R7-REV-01 nació exactamente de copiar el `atan` de otra
longitud.

## 5. Falsación adversarial (requisito 4)

| Adversario | `norm(dv)` | `delta_theta` | Cruce | Veredicto |
|---|---|---|---|---|
| Coherente de **un canal** (`d[...,1] = q/2`; reconciliación #723) | `q/2` | `atan((q/2)/L)` | `1/2` (medido en #723) | correcto para ESA familia |
| Coherente de **vértice** (construcción cerrada, §4) | `<= delta_chord`; medido `0.0067922245` en `L = 0.80` | `atan(delta/L)` exacto | `0.8660076459178669` | cota INFERIOR del peor caso |
| Alineado con el patrón alternado (`d[...,0] = ±(q/2)*s`) | `0` | `0` | `nan` | **VACUO** (retirado en #723) |
| Correlacionado / periódico p=3, p=4 | `<= delta_chord` | `<= asin(delta_chord/L)` | `>= L_SUFICIENTE` | respeta la cota |
| Combinaciones 3-D (vértice, coherente) | `<= delta_chord` (alcanza el sup) | `asin(delta)` en `L = 1` | — | **no maximiza el ángulo** (§4) |

**Resultado central:** el cruce `1/2` de la reconciliación es correcto para su familia
(coherente de un canal) pero **no es el cruce del modelo declarado**. La relación es exacta:

```text
L*(modelo completo) / L*(un canal)  =  (q/2)*sqrt(3) / (q/2)  =  sqrt(3)  =  1.7320508
```

**Precisión del `1/2`** (revisión de Codex, verificada ejecutando): la relación es exacta con el
cruce ANALÍTICO de un canal, `(q/2)/tan(q) = 0.4999897474909546`, contra el cruce de la construcción
`L = 0.8660076459178669` (cociente `1.7320508075688772 = sqrt(3)` a precisión de máquina). El `1/2` de
#723 es ese valor redondeado: con él el cociente da `1.7320152918357339`, es decir `sqrt(3)` salvo un
redondeo relativo de ~2×10⁻⁵, del orden de los desplazamientos de umbral que este documento
distingue. Léase el `1/2` de #723 como aproximación, no como el cruce exacto.

Es decir: **el `1/2` de #723 subestimaba el cruce por un factor `sqrt(3)`** (aproximadamente, en los
términos de arriba), porque su adversario usaba un solo canal. Ninguno de los dos números es "el equivocado": miden familias
distintas, y la del modelo completo es la de este documento.

**Por qué la cota no es tight (y qué se sabe).** El sup de `norm(dv)` (`delta_chord`) **sí** es
alcanzable, pero la configuración que lo alcanza tiene `cos(alpha) = -delta^2 / (2*delta_chord)`
(≈ `-0.0034`), lejos del `-d/L ≈ -0.0078` que exigiría el óptimo angular. Las dos familias
extremales son **distintas**. Una búsqueda acotada (dos píxeles, pesos `1/2`, 300 semillas +
descenso por coordenadas, con la construcción cerrada sembrada como control de que el buscador
la reproduce) converge en todos los `L` a `asin(delta/L)`, que es `0.9999942x` la cota: el
residuo es exactamente el factor `delta/delta_chord`. **No falsifica la cota y tampoco la
alcanza**; se declara como riesgo abierto, no como demostración.

## 6. Factores, fases y bordes (requisito 5)

- **Factor de reducción `k`.** `delta_chord` **no** depende de `k`: la cota
  `norm(dv) <= delta_chord` se propaga igual para cualquier ventana. `k` sólo cambia *cuánto se
  acerca* el campo real a la cota (campos más suaves → `norm(dv)` menor). El peor caso no se
  estrecha al crecer `k`.
- **Fase.** La construcción extrema no depende de la fase: usa un campo periódico-2 simétrico
  (pesos `1/2`, `1/2`), y el vector interpolado limpio tiene norma `L` exacta.
- **Borde.** La ventana recortada tiene pesos `>= 0` que suman 1 (verificado para `k ∈ {2,4,8}`
  en `0`, interior y `n_out-1`), así que el paso 3.3 aplica sin cambios: **el borde no tiene un
  modo de cancelación peor**. Es consistente con `R7_NYQUIST_ATTAINMENT` (#723): el borde
  alcanza el mismo ínfimo, no uno menor.
- **Entradas admisibles.** El clip a `[-1,1]` de §3.4.1 es no-op para normales unitarias; el
  modelo de perturbación opera sobre unitarias, así que el clip no participa.

## 7. Controles negativos y gates de tabla (requisito 6)

| Control | Qué detecta | Resultado |
|---|---|---|
| `eps` **paralelo** a `v` (`v_hat = sigma`) | falso verde por construcción degenerada | `delta_theta = 0` (a menos del piso `2**-26` de `acos`) → el test lo exige |
| Medir `L` **después** de renormalizar | el error cometido en la primera corrida de este trabajo | da `L = 1` siempre → invierte el veredicto (`cota(1) < q_ang`) |
| Usar `delta` como cota de desplazamiento | **el defecto reparado** | en el umbral, la cota ingenua queda por debajo de `delta_chord` → el test la mata |
| Afirmar que la construcción alcanza `delta_chord` en `norm(dv)` | **R7-REV-02** | el test exige `norm(dv) < delta` y el cierre analítico |
| Recalcular cada fila de las tablas de §4, §8 y §9 con **su propio** `L` | **R7-REV-01** | el test parsea el documento y recomputa; una fila copiada de otra longitud falla |
| No vacuidad | cota decorativa | la construcción alcanza `>= 0.99 * cota` en todo `L` |
| Mutación del aparato | tests que no discriminan | **21/21 mutantes matados**: los **13/13** enumerados por el commit `e87a57888ed719f367cce1457320a195eee211b3` (barrido ejecutado en el worktree de este slice, `scratch/barrido_mutantes.py` → `scratch/log_barrido_mutantes.txt`, kill juzgado por `N failed` en el resumen y no por el returncode; **el arnés no está versionado, así que la cifra es evidencia registrada con su log (§13.1), no reproducible desde el repo ni desde CI**) más **8** de los gates nuevos: `N01`–`N04` de §8, `N05`/`N06` de esta corrección documental y `N07`/`N08` de los estados obligatorios. La cifra previa de esta fila era `8/8`, del barrido anterior a `e87a5788`; **§12 es trazabilidad de correcciones, no el log de mutación**, y por eso la cifra vivía aquí y quedó desactualizada |
| Acumulador float32 en la sombra | el modelo de implementación sería otro | la sombra correcta es bit-exacta; la variante float32 difiere del binario en 1088–2475 px (§8.4) |
| Pesos 2-tap en la sombra | el filtro no sería el de Pillow | ángulo de 95–400 `x q_ang`; el binario no se reproduce (§8.4) |

## 8. Margen numérico de la implementación float32

El `resize_normal` real interpola con Pillow **modo F (float32)**, no con el filtro ideal en
float64. Se separan ambos. La tabla se recalcula fila por fila por el mismo gate de §7.

**Medición (filtro ideal float64 vs Pillow modo F), construcción extrema:**

| `L` | `delta_theta` ideal | `delta_theta` Pillow | gap (rad) | holgura contra la cota |
|---|---|---|---|---|
| `0.55` | `1.234911059146e-02` | `1.234911270188e-02` | `+2.110420e-09` | `+1.010397e-06` |
| `0.70` | `9.703061344789e-03` | `9.703063587293e-03` | `+2.242504e-09` | `+5.108046e-07` |
| `0.80` | `8.490241125200e-03` | `8.490243023519e-03` | `+1.898319e-09` | `+3.535947e-07` |
| `0.866` | `7.843206499160e-03` | `7.843207961777e-03` | `+1.462617e-09` | `+2.852183e-07` |
| `0.95` | `7.149726704363e-03` | `7.149692020626e-03` | `-3.468374e-08` | `+2.586951e-07` |
| `1.00` | `6.792251653377e-03` | `6.792252562861e-03` | `+9.094832e-10` | `+1.950647e-07` |

El filtro ideal reproduce `atan(delta/L)` a 12 dígitos. El gap float32 es `<= 3.5e-08` rad y
**no tiene signo constante** (es redondeo, no sesgo). La holgura contra la cota conservadora
sigue siendo positiva en todos los casos: mínimo `+1.950647e-07` rad en `L = 1`.

**Estimación a priori (sustituida por la cota de §8.2).** Con `u = 2**-24` por operación y 4 términos del kernel
por eje, la perturbación efectiva por canal es `q/2 + 4u`, de donde

```text
delta_num       = (q/2 + 4u) * sqrt(3) = 0.0067927690612057    (+6.07e-05 relativo)
delta_chord_num = 0.0067928082407454
L*_num          = delta_chord_num / sin(q_ang) = 0.8660919302466665   (+5.27e-05)
```

Esa estimación **quedaba como única cota del margen** y cargaba `4u` sobre la
perturbación de entrada. La derivación de §8.2 la reemplaza por una cota
*derivada del binario*: el resultado certificado (`0.8660770023307574`) es
**menor** — la estimación era conservadora, no ajustada. Se conserva el número
porque el gate que lo anclaba sigue vivo y porque documenta el error de
enfoque: cargar el error de implementación sobre la *entrada* lo amplifica por
`sqrt(3)` y por la conversión a cuerda; cargarlo sobre la *salida* no.

### 8.1 Qué ejecuta realmente el binario (auditoría de la implementación)

No se asumió que Pillow usa cuatro operaciones float32 porque existen cuatro
taps. Se leyó la fuente C de la versión instalada y se verificó contra el
binario.

| Campo | Valor |
|---|---|
| Python | `3.11.9` (MSC v.1938, 64 bit, AMD64) |
| Pillow | `11.3.0`, tag `11.3.0` = commit `89f1f4626a2aaf5f3d5ca6437f41def2998fbe09` |
| NumPy | `2.4.6` |
| Plataforma | Windows 10 19045 AMD64 (SSE2: `double` es IEEE-754 binario, sin x87 de 80 bits) |
| Fuente auditada | `src/libImaging/Resample.c` @ `89f1f462`, `src/_imaging.c` @ mismo commit |
| Rutas | `Image.resize` → `_imaging._resize` → `ImagingResample` → `ImagingResampleHorizontal_32bpc` → `ImagingResampleVertical_32bpc` |

**Cadena de operaciones por píxel de salida y por canal** (traducción literal de
`ImagingResampleHorizontal_32bpc` / `ImagingResampleVertical_32bpc`, rama
`IMAGING_TYPE_FLOAT32`, y de `precompute_coeffs`):

1. `a_x = float32(A_x)` — la producción convierte con
   `np.ascontiguousarray(arr[..., c], dtype=np.float32)`.
2. Coeficientes `k_x` calculados y **normalizados en `double`** (`k[x] /= ww`).
   La normalización a punto fijo (`normalize_coeffs_8bpc`, `PRECISION_BITS = 22`)
   es exclusiva del camino 8bpc y **no** corre en modo F. Para `k = 2` exacto
   (1024→512) el centro y `ss` son diádicos, los pesos crudos son diádicos y la
   normalización por `2.0` es exacta: en el interior `k = (1/8, 3/8, 3/8, 1/8)`,
   **exactos también en float32**. En el borde la ventana recortada se
   renormaliza por `7/4` y los pesos `(3/7, 3/7, 1/7)` llevan **un** redondeo
   `double`.
3. Pasada horizontal: `ss = 0.0` (**`double`**); `ss += float32(a_x) * double(k_x)`
   — el producto se redondea a `double`, la suma se redondea a `double`.
4. Escritura a `imTemp` (modo F) ⇒ **un** redondeo a float32 por muestra.
5. Pasada vertical: misma estructura sobre las muestras ya float32.
6. Escritura final a float32 ⇒ **un** redondeo más.
7. Producción: `largo = norm(crudo)` en float64 (NumPy) y salida `crudo / largo`.

Es decir: **acumulador `double` en ambas pasadas, tres redondeos float32 por
muestra de salida** (entrada, buffer intermedio, salida). La suposición
habitual —acumulación float32 con cuatro taps por eje— es **falsa** para esta
versión, y es la razón por la que la estimación de §8 (con `4u`) sobraba.

**Verificación contra el binario.** Se implementó una sombra que reproduce la
cadena literal (mismo orden de suma, mismos redondeos) y se comparó **bit a
bit** con `Image.fromarray(..., float32).resize(..., BILINEAR)`: idéntica en
262144 píxeles aleatorios (1024→512), y en escalas no potencia de 2 (100→37,
48→13, 17→5), upscale (8→16), imágenes rectangulares, campo Nyquist,
casi-cancelación y valores adyacentes a puntos medios float32. Ningún caso
difiere. Si el acumulador fuera float32, o si MSVC hubiera contraído a FMA, la
sombra diferiría: no lo hace.

### 8.2 Cota del error de implementación (derivación)

Sea `B* = sum_y v*_y sum_x k*_x A_{y,x}` el resultado del **mismo algoritmo en
aritmética exacta** (coeficientes racionales exactos, entradas float64), con
`||B*|| = L` y `|A_{y,x}| <= 1` (el `clip` a `[-1,1]` de producción fija `M = 1`).
Sea `b` la salida real del binario. Entonces, por canal:

| Término | Origen | Cota |
|---|---|---|
| entrada | `a_x = float32(A_x)`, coherente a través de un kernel de pesos `>= 0` que suman 1 | `u * M` |
| buffer intermedio | `float32(s)` de la pasada 1, y su propagación coherente por la pasada 2 | `u * M` |
| salida | `float32(u)` de la pasada 2 | `u * L` |
| coeficientes, productos y sumas en `double` | `(2m + 2n + 8)` redondeos de `2**-53` | `(2m+2n+8) * u_d * M` |

con `u = 2**-24`, `u_d = 2**-53`, `m`/`n` taps por eje. Una perturbación
**coherente no se promedia** con pesos positivos que suman 1: se suma. De ahí
que cada redondeo aporte su magnitud completa y no `1/4` de ella.

```text
E_b        = u * (2 + L) + (2m + 2n + 8) * u_d            (M = 1)
delta_theta_impl <= asin( sqrt(3) * E_b / L )
```

El último paso es el **mismo lema angular** de §3.4 aplicado al desplazamiento
de implementación `d_b = b - B*` (tres canales, `||d_b|| <= sqrt(3) * E_b`).
Composición con la cota de cuantización por desigualdad triangular:

```text
delta_theta_total <= asin(delta_chord / L) + asin( sqrt(3) * E_b / L )
```

**Umbral suficiente numérico** (bisección independiente, no copiada):

```text
L*_num = 0.8660770023307574     (+3.7725e-05 sobre L_SUFICIENTE ideal)
```

En el umbral ideal la cota de implementación vale `3.4165303821e-07` rad
(`4.3561e-05` de `q_ang`) y `E_b = 1.7082925570e-07`. El valor no depende del
número de taps de forma medible: para `k = 4` (`m = n = 8`) da
`0.8660770023311493` y para `k = 16` (`m = n = 32`), `0.8660770023335029` — la
diferencia es `2.7e-12`, del orden de los términos `u_d`.

**Alcance del valor publicado** (revisión de Codex): `0.8660770023307574` es el de `k = 2`
(`m = n = 4`, `κ = 24`), el que usan el token y `_umbral_suficiente_num()` por defecto. Para
`k = 4` y `k = 16` el umbral suficiente es **algo mayor**; el que cubre TODAS las escalas
declaradas es el máximo, `0.8660770023335029` (+`2.75e-12` sobre el publicado, contra un margen
numérico de `3.77e-5`). Los tres valores se recomputan en
`test_r7_impl_el_umbral_depende_del_numero_de_taps_y_el_publicado_es_el_de_k2`. El token
`R7_SUFICIENTE_THRESHOLD_NUM` sigue siendo el de `k = 2`; quien lo use para otra escala debe
tomar el de esa escala.

**Dos normas, no una** (revisión de Codex, verificada recomputando): la bisección de arriba usa el
mismo `L` en los dos términos, pero el geométrico se prueba con la norma `L` del campo **limpio**
interpolado y el de implementación acota `b - B*` contra la norma de `B*`, la interpolación del
campo **cuantizado**. Como `||B* - v|| <= delta_chord`, se tiene `L - delta_chord <= ||B*|| <=
L + delta_chord`. Con el denominador en `L - delta_chord` y `E_b` evaluado en `L + delta_chord`:

```text
L*_estricto (k = 2)  = 0.8660773906537476   (+3.88e-07 sobre L*_num; +3.81e-05 sobre L_SUFICIENTE ideal)
L*_estricto (k = 16) = 0.8660773906565153
```

Es el umbral **estrictamente suficiente** del modelo; `0.8660770023307574` es la aproximación de
mismo-`L`. La diferencia (~1% del margen numérico) no cambia la decisión, pero un lector que lo cite
como suficiente debe usar el estricto. Anclado por
`test_r7_impl_el_umbral_estricto_separa_las_dos_normas`.

**Variante de dos términos.** Si se mide el ángulo entre las salidas reales del
campo limpio y del campo perturbado (los dos pasan por el binario), la
composición lleva **dos** términos de implementación y el umbral sube a
`0.8661147283782189`. Es la lectura Conservadora; la de un término es la
correcta para "dirección de salida vs. normal verdadera".

### 8.3 Categorías, sin mezclarlas

| Afirmación | Categoría |
|---|---|
| `delta_theta_impl <= asin(sqrt(3)*(u*(2+L) + (2m+2n+8)*u_d)/L)` | `BOUND_ANALYTICALLY_PROVEN` (modelo estándar de punto flotante sobre la secuencia leída de la fuente) |
| La secuencia de operaciones es la del binario (sombra bit-exacta) | `BOUND_COMPUTATIONALLY_CERTIFIED` |
| El error por instancia contra el ideal racional exacto | `BOUND_COMPUTATIONALLY_CERTIFIED` |
| Peor caso observado: `8.16e-08` rad (`1.37 * u`) en el régimen del umbral | `BOUND_EMPIRICALLY_OBSERVED` |
| La cota es holgada por un factor `>= 4.4` frente al peor caso observado | `BOUND_EMPIRICALLY_OBSERVED` (no es tight, y no se afirma que lo sea) |
| Estimación `L*_num = 0.8660919302466665` con `4u` sobre la entrada | `BOUND_ESTIMATED_ONLY` — **retirada**, sustituida por §8.2 |

### 8.4 Controles negativos (todos discriminantes)

| Control | Resultado |
|---|---|
| Acumulador **float32** en vez de `double` | difiere del binario en 1088–2475 de 3072 píxeles de salida; el ángulo que produciría es `2.4x` el real |
| **Sin** redondeo del buffer intermedio | difiere del binario en 1425–2475 píxeles (en las familias diádicas de R7 el redondeo es exacto y no diferencia: es la excepción, no la regla) |
| **Una** sola pasada (omitir la vertical) | difiere en 5120–6080 píxeles; ángulo de 82–383 `x q_ang` |
| Pesos "bilineales de verdad" (2 taps, promedio simple) | **no** reproduce el binario (`max |diff| = 0.56`); ángulo de 95–400 `x q_ang` |
| Medir `L` después de renormalizar | da `1` exacto: el falso verde ya anclado en §7 |
| Campo de norma **no** unitaria (`x3`, `L = 2.40`) | `E_b` sigue acotado (`u*(2+L)`), pero el cuantizador de §3.1 presupone unitarias: la cota geométrica **no** es transferible |
| Monotonía del kernel (pesos `>= 0`) | 40 cajas aleatorias: 0 violaciones. Para toda caja de entradas, la salida real vive en `[f(caja_lo), f(caja_hi)]` |

### 8.5 Dominio de aplicación

| Vía | Entrada | ¿Certificada? |
|---|---|---|
| A — `decode_normal_image` → `load_asset` → `resize_normal_diagnostic` | normal **unitaria** (`rgb/255*2-1` renormalizado; `apply_convention` sólo flipping Y) | **Sí**, con `L <= 1` |
| B — API directa `resize_normal(n, size)` | admite normas distintas de 1 (clip + renormalización) | **No** por la cota geométrica; sí la de implementación (`E_b` escala con `L`). `F01_CONTRACT_AMENDMENT` sigue `PENDING_TECH_LEAD` |
| C — escalas | `k = 2` (1024/2048→512, primaria): pesos diádicos exactos. `k = 4, 8, 16` y no enteras (100→37): la sombra reproduce el binario bit a bit en cada escala (`test_r7_impl_la_sombra_reproduce_el_binario_bit_a_bit`, 4 escalas) y el umbral suficiente crece solo ~`2.7e-12` (§8.2, «Alcance del valor publicado»). Identidad (D-07): no hay remuestreo, no hay redondeo | **Sí** |
| D — convención geométrica | el resize es por canal: no altera RGB, normalización, DirectX/OpenGL ni el sentido de Y (eso vive en `apply_convention`, aguas arriba) | **Sí** |

`F01` opción A no introduce una entrada admisible fuera del modelo de R7 **para
la vía A**: la unidad de la entrada está garantizada por el decodificador
(`test` de procedencia en §8.6 del artefacto de tests). Para la vía B el
desajuste queda registrado como decisión pendiente, no enmendado.

### 8.6 Lo que este slice NO establece

- **Que la cota sea tight.** La holgura medida es `>= 4.4x`; no se demostró que
  ningún adversario se acerque a `asin(sqrt(3)*E_b/L)`.
- **Que el modelo de cuantización sea el correcto** (§10 ya lo declara).
- **Nada sobre otras versiones de Pillow.** La cota está certificada para
  `11.3.0` @ `89f1f462` en Windows/AMD64. Otra versión puede cambiar
  `precompute_coeffs`, el acumulador o el buffer intermedio: **hay que
  recertificar**, no extrapolar. Desde #756 el CI corre con Pillow `12.3.0`
  (`pip-audit` no deja pasar `11.3.0`): el gate de bit-exactitud de la sombra se
  ejecuta contra ese binario y pasa en las cuatro escalas (verificado
  ejecutando en Linux; el CI de Windows lo confirma en este PR), pero eso es
  una **observación** (`OBSERVADA_NO_CERTIFICADA`) y no una certificación: el
  `Resample.c` de `12.3.0` difiere del de `11.3.0` y no se auditó. El test fija
  la tabla de versiones (`PILLOW_ESTADOS`) y rechaza cualquier otra, parches
  incluidos.
- **Nada sobre JPEG.** El modelo trata la RGB decodificada como la verdad; el
  error de compresión JPEG no está acotado por `q/2`.
- **La banda entre `L` medido y `L` ideal del campo verdadero.** Producción
  mide `||crudo||` (campo decodificado + error de implementación); la cota de
  §3.4 usa la norma de la interpolación del campo verdadero. La diferencia
  está acotada por el modelo de cuantización, no por este slice, y **no** se
  cuantifica acá.

## 9. Comparación de umbrales (requisito 7)

`delta_theta` máximo que la cota permite en cada umbral, en unidades de `q_ang`. La columna
«garantiza» se verifica contra `cota(L) <= q_ang` (gate de §7).

| Umbral (nombre) | `L` | `cota(L)` | `x q_ang` | ¿Garantiza `delta_theta <= q_ang`? |
|---|---|---|---|---|
| `5/16` (vigente) | `0.3125` | `0.0217373767246773` | `2.771516` | No |
| `1/2` (opción 2 de #723) | `0.5` | `0.0135852084333893` | `1.732114` | No |
| `delta/tan(q)` (construcción alcanzable) | `0.8660076459178669` | `0.0078434237352538` | `1.000037` | No |
| `delta_chord/sin(q)` (umbral suficiente) | `0.8660392772765431` | `0.0078431372549020` | `1.000000` | Sí |

Con la cota probada, `5/16` **no** es una garantía ni `1/2` la repara: ambos dejan pasar
`delta_theta > q_ang`. Sólo `L >= 0.8660392772765431` la garantiza. **Sin corpus no se afirma
nada sobre cuánto contenido real cae en `[0.3125, 0.866)`**: eso es exactamente lo que no se
puede medir acá.

**Dos umbrales distintos que no hay que confundir (R7-REV-03).** `L_SUFICIENTE` es el cruce de
la **cota superior**; `L_ALCANZABLE = delta/tan(q) = 0.8660076459178669` es el cruce de la
**construcción alcanzable** y es **menor**. Su lectura es la inversa y es igual de dura:

```text
L >= L_SUFICIENTE = 0.8660392772765431   =>   delta_theta <= q_ang  GARANTIZADO (cota superior)
L <  L_ALCANZABLE = 0.8660076459178669   =>   EXISTE entrada admisible con delta_theta > q_ang  CERTIFICADO
L in [L_ALCANZABLE, L_SUFICIENTE)  (ancho 3.163135867612077e-05)  =>  INDECIDIDO por este analisis
```

El token anterior `R7_WORST_CASE_CROSSING_FULL` se **retira**: llamar «peor caso» al cruce de
una cota que no se demostró tight era una atribución de optimalidad sin prueba.

## 10. Lo que este documento NO establece (requisitos 8 y 10)

- **Nada sobre la distribución de assets reales.** Todo es sintético y adversarial; el corpus
  está prohibido en este slice. La tasa de rechazo de `0.8660392772765431` sobre contenido
  legítimo es **desconocida**.
- **Que el peor caso sea alcanzable por un asset legítimo.** La construcción exige un campo
  diseñado para cancelar; que una textura real lo realice no está establecido.
- **Que la cota sea tight.** La brecha medida contra la construcción alcanzable es
  `<= 1.02e-06` rad (§4), y la búsqueda acotada de §5 no pasa de `0.9999942x` la cota. No se
  demuestra que ningún adversario alcance `asin(delta_chord/L)`.
- **Qué pasa en la banda indecidida.** `L in [0.8660076459178669, 0.8660392772765431)` no está
  resuelto en ninguna dirección.
- **El margen numérico de la implementación.** Ahora **acotado** para Pillow `11.3.0` @ `89f1f462` en Windows/AMD64 (§8.2): `L*_num = 0.8660770023307574` (mismo `L` en los dos términos) y `L*_estricto = 0.8660773906537476` (norma del campo cuantizado separada). Lo que sigue abierto es la **tightness** de la cota (holgura medida `>= 4.4x`) y la certificación de **otras** versiones de Pillow, que no se extrapolan.
- **Que el modelo declarado sea el correcto.** La cota es condicional al modelo
  `abs(eps_c) <= q/2` con renormalización. Si el modelo cambia, la cota cambia (linealmente en
  `delta_chord`).
- **Ninguna decisión normativa.** `R7_NUMERIC_DERIVATION` sigue `BLOCKED`: lo que falta ya no es
  una derivación, es la **adjudicación** de si el criterio de aceptación es worst-case o
  estadístico, la tightness de la cota, y si el costo de cobertura es aceptable.

## 11. Recomendación (separada por naturaleza)

**Garantía matemática (demostrada).** Bajo el modelo declarado,
`delta_theta <= asin(delta_chord / L)` con `delta_chord = 2*sin(asin((q/2)*sqrt(3))/2)`, y por
lo tanto `L >= delta_chord/sin(q_ang) = 0.8660392772765431` **garantiza**
`delta_theta <= q_ang`. El paso angular está demostrado (§3.4), no postulado: el óptimo para
`norm(dv)` fijo es `asin(d/L)`, alcanzado con `cos(alpha) = -d/L`.

**Garantía matemática (demostrada, en la otra dirección).** Para
`L < delta/tan(q_ang) = 0.8660076459178669` **existe** una entrada admisible con
`delta_theta > q_ang` (la construcción cerrada de §4 la realiza). El rechazo en esa zona es
certificado, no heurístico.

**Observación sintética.** El cruce del modelo completo es `sqrt(3)` veces el de la
reconciliación — `0.8660392772765431`, no `0.5` — y está `1.39e-05` por encima de `sqrt(3)/2`.
La cota es conservadora en `<= 1.02e-06` rad y **no** se demostró tight; la búsqueda acotada de
§5 queda en `0.9999942x`.

**Riesgos no cubiertos.** La tasa de rechazo sobre contenido legítimo; la plausibilidad del
campo extremo; la tightness de la cota; la banda indecidida; la tightness de la cota de
implementación (holgura `>= 4.4x`); la certificación de versiones de Pillow distintas de
`11.3.0`; la validez del propio modelo de cuantización.

**Consecuencia para la decisión.** Si el criterio es worst-case, el único valor sostenido por
evidencia es `0.8660392772765431` — y su costo de cobertura es **desconocido** sin corpus, lo
que probablemente lo vuelva inaceptable en la práctica. Con el margen de implementación ya
acotado, el valor worst-case que la evidencia sostiene es `0.8660773906537476` (§8.2, estricto;
`0.8660770023307574` es la aproximación de mismo-`L`), no menor. Si el criterio es estadístico, hay que **declararlo** (hoy `5/16` se presenta como
derivación, no como estadístico). En cualquier caso la decisión es del Tech Lead; este
documento no la toma.

## 12. Trazabilidad de las correcciones documentales

| Hallazgo | Qué se corrigió | Gate que lo ancla |
|---|---|---|
| R7-REV-01 | tabla de §4 recalculada con el `L` de cada fila; `cota(L*) - atan(delta/L*) = 2.864523e-07 > 0` | `test_r7_rev01_la_tabla_de_conservadurismo_se_recalcula_con_su_propio_largo` |
| R7-REV-02 | `norm(dv) <= delta_chord` (cota, no igualdad); la construcción mide `0.0067922245` en `L = 0.80` | `test_r7_rev02_la_construccion_no_alcanza_delta_chord_en_norma_de_dv` |
| R7-REV-03 | `R7_SUFICIENTE_THRESHOLD_IDEAL` + `R7_ALCANZABLE_THRESHOLD_LOWER_BOUND`; se retira el nombre «worst case» | `test_r7_rev03_los_tres_umbrales_son_distintos_y_ordenados` |
| Tablas manuales (§4, §8, §9) | recomputadas fila por fila desde funciones independientes | `test_r7_rev01_la_tabla_de_umbrales_se_recalcula_y_su_veredicto_es_coherente` y las otras dos |
| Paso angular §3.4 | demostrado por optimización en `cos(alpha)`, no postulado | `test_r7_optimo_angular_con_norma_de_dv_fija_es_asin` |
| Cifra de mutantes en §7 | `8/8` → **13/13**, contra el commit que enumera el barrido (`e87a5788`); §12 aclarada como trazabilidad, no como log. En este slice los 13 se **reprodujeron** con kill juzgado por `N failed` y se sumaron 8 de los gates nuevos: **21/21** | `test_r7_la_cifra_de_mutantes_apunta_al_commit_que_la_evidencia` |
| Margen float32 | de estimación (`4u` sobre la entrada) a cota derivada del binario (§8.1–§8.2) | `test_r7_impl_la_sombra_reproduce_el_binario_bit_a_bit`, `test_r7_impl_la_cota_nunca_se_viola`, `test_r7_impl_el_umbral_numerico_certificado` |

```text
R7_NUMERIC_DERIVATION              = BLOCKED   (SIN CAMBIO — falta adjudicacion y tightness; el margen numerico QUEDA ACOTADO en §8.2)
R7_THRESHOLD_KIND                  = HEURISTIC (RMS)   (SIN CAMBIO)
R7_THRESHOLD_VALUE                 = 5/16      (CONSERVADO; prohibido recalibrar con corpus)
R7_WORST_CASE_CROSSING             = 1/2       (token de #723, familia coherente de UN canal)
R7_WORST_CASE_BOUND_ANGULAR        = asin(delta_chord / L)   (delta_chord = 2*sin(asin((q/2)*sqrt(3))/2))
R7_SUFICIENTE_THRESHOLD_IDEAL      = 0.8660392772765431   (cruce de la COTA SUPERIOR: L >= esto GARANTIZA delta_theta <= q_ang)
R7_ALCANZABLE_THRESHOLD_LOWER_BOUND = 0.8660076459178669  (cruce de la construccion: L < esto CERTIFICA que el peor caso EXCEDE q_ang)
R7_WORST_CASE_ACHIEVABLE           = atan(delta / L)   (construccion alcanzable, NO cota universal)
R7_THRESHOLD_UNDECIDED_BAND        = [0.8660076459178669, 0.8660392772765431)   (ancho 3.163135867612077e-05)
R7_WORST_CASE_PREMISE              = CONJECTURE_NOT_FALSIFIED   (gate #717 §6.1, FROZEN — no se toca)
R7_IMPL_BOUND                      = asin(sqrt(3) * E_b / L)   (E_b = u*(2+L) + (2m+2n+8)*u_d, u = 2**-24)
R7_IMPL_CERTIFIED_VERSION          = Pillow 11.3.0 @ 89f1f4626a2aaf5f3d5ca6437f41def2998fbe09 (Windows/AMD64; NO extrapolar a otras versiones)
R7_PILLOW_CERTIFICATION            = BOUND_COMPUTATIONALLY_CERTIFIED   (SOLO para 11.3.0 @ 89f1f462, Windows/AMD64; NO extrapolar; el certificado numerico GLOBAL sigue BLOCKED por R7_NUMERIC_DERIVATION)
R7_PILLOW_VERSIONES                = 11.3.0 CERTIFICADA; 12.3.0 OBSERVADA_NO_CERTIFICADA   (el CI corre 12.3.0 desde #756; NO extrapolar: solo 11.3.0 esta certificada; otra version = recertificar o declararla)
R7_SUFICIENTE_THRESHOLD_NUM        = 0.8660770023307574   (umbral peor caso CON margen de implementacion; +3.7725e-05 sobre el ideal)
R7_SUFICIENTE_THRESHOLD_NUM_ESTRICTO = 0.8660773906537476 (normas del campo limpio y del cuantizado separadas, k = 2; +3.88e-07 sobre el anterior)
R7_IMPL_TIGHTNESS                  = NOT_TIGHT   (holgura medida >= 4.4x; peor caso observado 1.37*u)
F01_CONTRACT_AMENDMENT             = PENDING_TECH_LEAD
H4_CODE_MERGE_AUTHORIZED           = NO
PR723_MERGE_AUTHORIZED             = NO        (CONSERVADO; este slice no fusiona nada)
PR733_MERGE_AUTHORIZED             = NO        (CONSERVADO; sigue Draft, sin auto-merge)
M6_IMPLEMENTATION_BLOCKED          = YES
REAL_CORPUS_RERUN                  = NO        (CONSERVADO; corpus READ-ONLY)
PR675_CHANGED                      = NO        (CONSERVADO; este slice no toca #675)
```
## 13. Verificacion de este slice (Fases 5 y 6)

### 13.1 La cifra de mutantes pasa de *reportada* a *registrada con su log*

El estado previo de la fila de mutacion de §7 era `8/8`; la ultima clausura declaraba `13/13`.
**Ninguna de las dos cifras estaba reproducida por un log dentro del slice.** En esta corrida:

- los **13** mutantes enumerados por el commit `e87a57888ed719f367cce1457320a195eee211b3`
  se reconstruyeron uno a uno y se ejecutaron: **13/13 muertos**;
- se agregaron **8** mutantes de los gates nuevos — `N01`–`N04` de §8, `N05`/`N06` de esta
  correccion documental y `N07`/`N08` de los estados obligatorios del brief: **8/8 muertos**;
- **total registrado: 21/21**. El arnes que lo produjo es
  `scratch/barrido_mutantes.py` (**no versionado**: `scratch/` esta en `.gitignore`), asi que esta
  cifra NO se puede reproducir desde el repo ni la ejecuta CI (revision de Codex); la tabla de
  abajo es el log, comprometida aca para que la evidencia sobreviva al commit. Versionar y correr el
  arnes es trabajo pendiente, no algo que este documento afirme hecho.

| id | mutante | resumen de pytest | kill |
|---|---|---|---|
| `M01` | cuerda -> perturbacion | `16 failed, 37 passed, 1 warning in 10.97s` | **SI** |
| `M02` | cruce con delta en vez de delta_chord | `8 failed, 45 passed, 1 warning in 10.93s` | **SI** |
| `M03` | la cota usa la perturbacion | `7 failed, 46 passed, 1 warning in 11.51s` | **SI** |
| `M04` | ALPHA_PERT sin asin | `16 failed, 37 passed, 1 warning in 10.74s` | **SI** |
| `M05` | v_hat = sigma (construccion degenerada) | `24 failed, 29 passed, 1 warning in 11.25s` | **SI** |
| `M06` | la cota usa atan en vez de asin | `8 failed, 45 passed, 1 warning in 11.14s` | **SI** |
| `M07` | eps fuera del vertice del cubo | `24 failed, 29 passed, 1 warning in 12.00s` | **SI** |
| `M08` | call site _campo_extremo usando sigma | `20 failed, 33 passed, 1 warning in 11.04s` | **SI** |
| `M09` | R7-REV-01: atan de otra longitud en la tabla de §4 | `1 failed, 52 passed, 1 warning in 10.14s` | **SI** |
| `M10` | R7-REV-02: igualdad en la cadena de desplazamiento | `1 failed, 52 passed, 1 warning in 12.53s` | **SI** |
| `M11` | R7-REV-03: token de cruce llamado worst case | `1 failed, 52 passed, 1 warning in 11.43s` | **SI** |
| `M12` | M3: igualdad reintroducida por la cadena larga | `1 failed, 52 passed, 1 warning in 11.87s` | **SI** |
| `M13` | M6: valor de la tabla de §8 copiado de otra fila | `1 failed, 52 passed, 1 warning in 11.41s` | **SI** |
| `N01` | sin redondeo del buffer intermedio | `1 failed, 52 passed, 1 warning in 12.36s` | **SI** |
| `N02` | umbral numerico certificado cambiado por la estimacion | `1 failed, 52 passed, 1 warning in 10.21s` | **SI** |
| `N03` | token de cota de implementacion sin anclar | `1 failed, 52 passed, 1 warning in 11.76s` | **SI** |
| `N04` | umbral numerico rebajado al ideal | `1 failed, 52 passed, 1 warning in 11.64s` | **SI** |
| `N05` | cifra verificada del barrido desfigurada | `1 failed, 52 passed, 1 warning in 10.61s` | **SI** |
| `N06` | la fila deja de apuntar al barrido reproducible | `1 failed, 52 passed, 1 warning in 9.67s` | **SI** |
| `N07` | se autoriza en silencio la fusion de #733 | `1 failed, 52 passed, 1 warning in 11.67s` | **SI** |
| `N08` | el certificado se degrada a observacion empirica | `1 failed, 52 passed, 1 warning in 9.20s` | **SI** |

Dos correcciones de metodo, ambas con evidencia en el log:

1. **El returncode NO es criterio de kill.** El primer barrido de este slice juzgo
   `returncode != 0` y reporto 17/17. Al reproducir `N03`/`N04` a mano se comprobo que
   **sobrevian** (`RC 0`, `51 passed`): el rojo venia de un `error` de entorno (basetemp
   sucia, import resuelto al baseline del instalable editable, falta de
   `SKYCLAW_WORKTREE_ROLE`). El criterio paso a ser `N failed` en el resumen de pytest; un
   `N error` se reporta aparte y **no** cuenta como kill.
2. **Basetemp unica por mutante.** El guard `safe-delete` del repo bloquea `rmtree`, asi que
   reusar una basetemp fija acumula estado entre corridas (se midieron 106 directorios
   residuales). Ahora cada mutante usa `.pytest-tmp-mut/<id>` — padre existente, mismo
   volumen `E:` que el repo, sin borrar nada.

La correccion documental **no** altera ninguna decision matematica congelada: los tokens
`R7_SUFICIENTE_THRESHOLD_IDEAL`, `R7_ALCANZABLE_THRESHOLD_LOWER_BOUND`,
`R7_NUMERIC_DERIVATION = BLOCKED` y `R7_THRESHOLD_VALUE = 5/16` quedan como estaban.

### 13.2 Puerta de verificacion (Fase 6)

| Puerta | Conjunto / comando | Resultado |
|---|---|---|
| Suite cientifica R7 | `tests/test_native_parallax_h4_r7_bound.py` | **53 passed** |
| Suite focal Native Parallax | `tests/test_native_parallax*.py` | **773 passed** |
| Tests documentales + procedencia | `test_docs_superpowers_namespace`, `test_observability_documentation`, `test_clean_room_invariant`, `test_provenance_post_release` | **141 passed** |
| Aislamiento | `test_worktree_convention_invariant`, `test_loot_data_root_isolation` | **58 passed, 2 failed** — ambos `PRE_EXISTING_ENVIRONMENTAL` (§13.3) |
| Mutantes especificos | `scratch/barrido_mutantes.py` | **21/21 matados, 0 sobrevivientes** |
| Ruff check | `ruff check sky_claw/ tests/` | **All checks passed** (860 archivos) |
| Ruff format | `ruff format --check sky_claw/ tests/` | **860 archivos ya formateados** |
| Mypy | `mypy sky_claw/ --ignore-missing-imports --no-error-summary` | **sin errores** |
| Suite completa | `pytest tests/` (31 min) | **11227 passed, 22 failed** — los 22 son `PRE_EXISTING_ENVIRONMENTAL`, verificados contra el baseline (§13.3) |

No se toco codigo de produccion: el diff de este slice es 2 archivos, ambos artefactos del
PR #733 (documento + suite). `md5` de los dos artefactos verificado contra el pristino
despues de cada mutante y al final del barrido.

### 13.3 Los 2 fallos de aislamiento son previos al cambio

- `test_los_worktrees_no_principales_viven_bajo_la_convencion` enumera **todos** los
  worktrees de la maquina (`C:/Worktrees/...`, `E:/SkyClaw_*`, `.claude/worktrees/...`),
  creados por otras sesiones. El worktree de este slice no aparece en la lista. Es estado
  ajeno, no una regresion.
- `test_m6_symlink_o_dentro_de_profile` exige que un symlink se rechace; en el regimen
  sandbox de esta maquina `Path.symlink_to()` **tiene exito pero crea una COPIA**
  (`is_symlink()` devuelve `False`, verificado empiricamente), por lo que el rechazo no
  puede dispararse. Es el regimen, no el cambio.

Ambas son de clase `PRE_EXISTING_ENVIRONMENTAL`: ni el documento ni la suite de este slice
intervienen en su camino causal.

#### 13.3.1 La suite completa: los 22 fallos son del regimen, no del slice

`pytest tests/` completo (31 min): **11227 passed, 120 skipped, 22 failed**. Los 22 fallos
estan en 8 archivos ajenos al diff de este slice (que solo toca el documento R7 y su
suite): `test_file_permissions_icacls_1332` (3), `test_file_permissions_post_validation` (2),
`test_vfs_teardown_deadline` (16), `test_runtime_vault_*` (2), `test_dlq_schema_init_*` (1),
`test_vfs_protocol_v2_split_roots` (1), `test_worktree_convention_invariant` (1).

**Verificado contra el baseline, no razonado.** El mismo subconjunto de archivos se corrio
en `e87a57888ed719f367cce1457320a195eee211b3` (padre de este commit, estado pristino del PR,
checkout detached en el mismo worktree, mismo entorno: `PYTHONPATH`, `SKYCLAW_WORKTREE_ROLE`,
mismo volumen `E:`): **23 failed, 219 passed**. Comparacion:

- **21 de los 22 fallos identicos** en baseline y con el commit: mismas familias, mismos
  tests.
- **1 fluctuacion dentro de las mismas clases**: el baseline fallo
  `test_observe_runtime_symlink_executable_rejected` (y paso
  `test_s2_same_pid_wrong_creation_time_rejected` y `test_worker_exit_tardio_no_sobrescribe...`);
  con el commit es al reves. Mismo par de clases cross-process con deadlines — inestables en
  regimen sandbox (locks cross-process bloqueados, `symlink_to` que crea copias, icacls
  restringido). El baseline tuvo **un fallo mas** que el commit.

Ninguno de los 8 archivos importa, colecta ni comparte estado con los 2 artefactos de este
slice. Clasificacion: **PRE_EXISTING_ENVIRONMENTAL** — el regimen sandbox de esta maquina
(locks cross-process, ACLs reales, symlinks) es la causa comun; el commit no introduce ni
remedia ninguno.
