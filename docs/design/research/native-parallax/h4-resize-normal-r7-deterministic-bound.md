# H4 `resize_normal` — Cota determinista del error angular (R7)

Investigación `H4_R7_DETERMINISTIC_BOUND_RESEARCH`, **reparada** en
`H4_R7_PROOF_REPAIR_AND_PUBLICATION` y **corregida documentalmente** en
`H4_R7_DOCUMENTARY_CORRECTION`. **No** modifica producción, **no** cambia
`DEGENERATE_THRESHOLD` y **no** declara cierre normativo. Corpus real **no** usado (§9).

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

Es decir: **el `1/2` de #723 subestimaba el cruce por un factor `sqrt(3)`**, porque su
adversario usaba un solo canal. Ninguno de los dos números es "el equivocado": miden familias
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
| Mutación | tests que no discriminan | **8/8 mutantes matados** (§12) |

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

**Estimación a priori (no demostrada).** Con `u = 2**-24` por operación y 4 términos del kernel
por eje, la perturbación efectiva por canal es `q/2 + 4u`, de donde

```text
delta_num       = (q/2 + 4u) * sqrt(3) = 0.0067927690612057    (+6.07e-05 relativo)
delta_chord_num = 0.0067928082407454
L*_num          = delta_chord_num / sin(q_ang) = 0.8660919302466665   (+5.27e-05)
```

**Por qué esto NO cierra la derivación.** El margen es una **estimación**, no una cota
demostrada: (a) no se acredita el número de términos ni el modo de acumulación de Pillow;
(b) el barrido mide una familia construida, no el espacio de entradas admisibles; (c) el
`clip` a `[-1,1]` y la conversión `float64 -> float32` de la entrada no están acotados por
separado. Por eso **`R7_NUMERIC_DERIVATION` sigue `BLOCKED`**: el margen se declara medido y
estimado, no garantizado.

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
- **El margen numérico de la implementación.** Sólo medido y estimado (§8), no acotado.
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
campo extremo; la tightness de la cota; la banda indecidida; el margen numérico float32; la
validez del propio modelo de cuantización.

**Consecuencia para la decisión.** Si el criterio es worst-case, el único valor sostenido por
evidencia es `0.8660392772765431` — y su costo de cobertura es **desconocido** sin corpus, lo
que probablemente lo vuelva inaceptable en la práctica. Si el criterio es estadístico, hay que
**declararlo** (hoy `5/16` se presenta como derivación, no como estadístico). En cualquier caso
la decisión es del Tech Lead; este documento no la toma.

## 12. Trazabilidad de las correcciones documentales

| Hallazgo | Qué se corrigió | Gate que lo ancla |
|---|---|---|
| R7-REV-01 | tabla de §4 recalculada con el `L` de cada fila; `cota(L*) - atan(delta/L*) = 2.864523e-07 > 0` | `test_r7_rev01_la_tabla_de_conservadurismo_se_recalcula_con_su_propio_largo` |
| R7-REV-02 | `norm(dv) <= delta_chord` (cota, no igualdad); la construcción mide `0.0067922245` en `L = 0.80` | `test_r7_rev02_la_construccion_no_alcanza_delta_chord_en_norma_de_dv` |
| R7-REV-03 | `R7_SUFICIENTE_THRESHOLD_IDEAL` + `R7_ALCANZABLE_THRESHOLD_LOWER_BOUND`; se retira el nombre «worst case» | `test_r7_rev03_los_tres_umbrales_son_distintos_y_ordenados` |
| Tablas manuales (§4, §8, §9) | recomputadas fila por fila desde funciones independientes | `test_r7_rev01_la_tabla_de_umbrales_se_recalcula_y_su_veredicto_es_coherente` y las otras dos |
| Paso angular §3.4 | demostrado por optimización en `cos(alpha)`, no postulado | `test_r7_optimo_angular_con_norma_de_dv_fija_es_asin` |

```text
R7_NUMERIC_DERIVATION              = BLOCKED   (SIN CAMBIO — falta adjudicacion, margen numerico y tightness)
R7_THRESHOLD_KIND                  = HEURISTIC (RMS)   (SIN CAMBIO)
R7_THRESHOLD_VALUE                 = 5/16      (CONSERVADO; prohibido recalibrar con corpus)
R7_WORST_CASE_CROSSING             = 1/2       (token de #723, familia coherente de UN canal)
R7_WORST_CASE_BOUND_ANGULAR        = asin(delta_chord / L)   (delta_chord = 2*sin(asin((q/2)*sqrt(3))/2))
R7_SUFICIENTE_THRESHOLD_IDEAL      = 0.8660392772765431   (cruce de la COTA SUPERIOR: L >= esto GARANTIZA delta_theta <= q_ang)
R7_ALCANZABLE_THRESHOLD_LOWER_BOUND = 0.8660076459178669  (cruce de la construccion: L < esto CERTIFICA que el peor caso EXCEDE q_ang)
R7_WORST_CASE_ACHIEVABLE           = atan(delta / L)   (construccion alcanzable, NO cota universal)
R7_THRESHOLD_UNDECIDED_BAND        = [0.8660076459178669, 0.8660392772765431)   (ancho 3.163135867612077e-05)
R7_WORST_CASE_PREMISE              = CONJECTURE_NOT_FALSIFIED   (gate #717 §6.1, FROZEN — no se toca)
F01_CONTRACT_AMENDMENT             = PENDING_TECH_LEAD
H4_CODE_MERGE_AUTHORIZED           = NO
M6_IMPLEMENTATION_BLOCKED          = YES
```
