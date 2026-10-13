# H4 `resize_normal` — Cota determinista del error angular (R7)

Investigación `H4_R7_DETERMINISTIC_BOUND_RESEARCH`. **No** modifica producción, **no** cambia
`DEGENERATE_THRESHOLD` y **no** declara cierre normativo. Corpus real **no** usado (§6.1).

## 0. Procedencia y alcance

| Campo | Valor |
|---|---|
| Worktree | `E:/Skyclaw_Main_Sync/.worktrees/h4-r7-bound-research` (rama plana) |
| Base | `802ff271` (head del PR #723) + merge normal de `origin/main` (`2dc3bf72`) |
| Contratos leídos | #711 (`h4-resize-normal-preflight.md`), #717 (`h4-resize-normal-authorization-gate.md` §6/§6.1), #723 (`h4-resize-normal-contract-reconciliation.md` §2) |
| Overlap de `origin/main` sobre el write-set | `NATIVE_PARALLAX_BASE_OVERLAP=NO` (sólo cambió `h4-resize-normal-authorization-gate.md`, ya presente en la base) |
| Artefacto | `tests/test_native_parallax_h4_r7_bound.py` (14 tests, 0.42 s) + este documento |

Alcance: derivar, si es demostrable, una cota **conservadora** del error angular bajo **todas**
las perturbaciones admitidas por el modelo declarado, sin presuponer independencia entre píxeles,
y falsarla. La decisión de adoptar un umbral es del Tech Lead.

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

La distinción crítica es 2 vs 3 vs 5: `q/2` es una cota **de canal**; `delta_i` es una
**deflexión angular** por píxel; y la cota angular sólo aparece tras dividir por `L`.

## 3. Derivación

### 3.1 Deflexión máxima por píxel

`delta(eps) = normalize(n + eps) - n`. El conjunto alcanzable es un cubo convexo centrado en
`n`; el máximo de la deflexión se alcanza en un **vértice** y, en el óptimo, `eps` es
**perpendicular** a `n` (rota, no escala). Medido sobre 1500 direcciones × 8 vértices:

```text
Delta_max = (q/2) * sqrt(3) = 0.0067923561081132
```

con `eps* = (+-q/2, +-q/2, +-q/2)` y `<n*, eps*> = 0`. **Nótese el `sqrt(3)`, no `q/2`.**

### 3.2 Propagación por la interpolación

El filtro bilineal de Pillow es una combinación convexa por píxel de salida: pesos `w >= 0`,
`sum w = 1`, también con la ventana **recortada del borde** (los pesos se renormalizan). Por lo
tanto, para cada píxel de salida:

```text
||dv|| = || sum_i w_i * delta_i ||  <=  sum_i w_i * ||delta_i||  <=  max_i ||delta_i||  <=  Delta_max
```

Esta desigualdad **no usa independencia**: vale para cualquier correlación entre píxeles,
incluida la coherencia total. Es la respuesta al punto 3 del brief.

### 3.3 Cota angular

Para `||v|| = L > ||dv||`, el ángulo entre `v` y `v + dv` está acotado por

```text
delta_theta  <=  asin( ||dv|| / L )  <=  asin( Delta_max / L )
```

(el máximo se alcanza con `dv` tangente a la esfera de radio `L`). **Cruce conservador:**

```text
L* = Delta_max / sin(q_ang) = 0.8660343        (sqrt(3)/2 = 0.8660254)
```

**Requisito 9 del brief, explícito:** `||eps|| <= (q/2)*sqrt(3)` es una cota de **perturbación**;
la cota **angular** es `asin(Delta_max/L)`. No son el mismo objeto y no se confunden: la primera
vale `0.0068` en unidades de vector, la segunda `0.0078` rad en `L = 0.866`.

## 4. La cota es alcanzable (no vacua) — construcción cerrada

La cota no es un techo decorativo. Con `sigma = (1,1,1)/sqrt(3)` (diagonal del cubo),
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
delta_theta = atan( Delta_max / L )
```

Verificado a 12 dígitos significativos (`ratio = 1.000000000`) para `L ∈ {0.55, 0.70, 0.80,
0.866, 0.95, 1.0}`. `asin` y `atan` difieren en `O((Delta_max/L)^3) ≈ 1e-6` relativo, así que el
cruce medido y el conservador coinciden a esa precisión.

## 5. Falsación adversarial (requisito 4)

| Adversario | `norm(dv)` | Cruce | Veredicto |
|---|---|---|---|---|
| Coherente de **un canal** (`d[...,1] = q/2`; reconciliación #723) | `q/2` | **`1/2`** | correcto para ESA familia |
| Coherente de **vértice** (este documento, `eps` = diagonal) | `(q/2)*sqrt(3)` | **`sqrt(3)/2 = 0.866`** | correcto para el modelo **completo** |
| Alineado con el patrón alternado (`d[...,0] = ±(q/2)*s`) | `0` | `nan` | **VACUO** (retirado en #723) |
| Correlacionado / periódico p=3, p=4 | `<= Delta_max` | `>= 0.866` | respeta la cota |
| Combinaciones 3-D (vértice) | `= Delta_max` | `0.866` | **la alcanza** |

**Resultado central:** el cruce `1/2` de la reconciliación es correcto para su familia
(coherente de un canal) pero **no es el cruce del modelo declarado**. La relación es exacta:

```text
L*(modelo completo) / L*(un canal)  =  (q/2)*sqrt(3) / (q/2)  =  sqrt(3)  =  1.7320508
0.5 * sqrt(3) = 0.8660254  =  sqrt(3)/2
```

Es decir: **el `1/2` de #723 subestimaba el cruce por un factor `sqrt(3)`**, porque su adversario
usaba un solo canal. Ninguno de los dos números es "el equivocado": miden familias distintas, y
la del modelo completo es la de este documento.

## 6. Factores, fases y bordes (requisito 5)

- **Factor de reducción `k`.** `Delta_max` **no** depende de `k`: la cota `||dv|| <= Delta_max`
  se propaga igual para cualquier ventana. `k` sólo cambia *cuánto se acerca* el campo real a la
  cota (campos más suaves → `||dv||` menor). El peor caso no se estrecha al crecer `k`.
- **Fase.** La construcción extrema no depende de la fase: usa un campo periódico-2 simétrico
  (pesos `1/2`, `1/2`), y el vector interpolado limpio tiene norma `L` exacta.
- **Borde.** La ventana recortada tiene pesos `>= 0` que suman 1 (verificado para `k ∈ {2,4,8}`
  en `0`, interior y `n_out-1`), así que el paso 3.2 aplica sin cambios: **el borde no tiene un
  modo de cancelación peor**. Es consistente con `R7_NYQUIST_ATTAINMENT` (#723): el borde alcanza
  el mismo ínfimo, no uno menor.
- **Entradas admisibles.** El clip a `[-1,1]` de §3.4.1 es no-op para normales unitarias; el
  modelo de perturbación opera sobre unitarias, así que el clip no participa.

## 7. Controles negativos (requisito 6)

| Control | Qué detecta | Resultado |
|---|---|---|
| `eps` **paralelo** a `v` (`v_hat = sigma`) | falso verde por construcción degenerada | `delta_theta = 0` (a menos del piso `2**-26` de `acos`) → el test lo exige |
| Medir `L` **después** de renormalizar | el error que cometí en la primera corrida de este trabajo | da `L = 1` siempre → invierte el veredicto (`cota(1) < q_ang`) |
| No vacuidad | cota decorativa | la construcción alcanza `>= 0.99 * cota` en todo `L` |
| Mutación | tests que no discriminan | **5/5 mutantes matados** (Δ_max→`q/2`; `v_hat`→`sigma`; `eps` sin vértice; `asin`→`atan`; cruce→`1/2`) |

## 8. Comparación de umbrales (requisito 7)

`delta_theta` máximo que la cota permite en cada umbral, en unidades de `q_ang`:

| Umbral | `cota(umbral)` | `x q_ang` | ¿Garantiza `delta_theta <= q_ang`? |
|---|---|---|---|
| `5/16 = 0.3125` (vigente) | `0.0217374` | **2.77x** | **No** |
| `1/2 = 0.5` (opción 2 de #723) | `0.0135852` | **1.73x** | **No** |
| `sqrt(3)/2 = 0.866` (esta cota) | `0.0078431` | **1.00x** | **Sí — por construcción** |

Con la cota probada, `5/16` **no** es una garantía ni `1/2` la repara: ambos dejan pasar
`delta_theta > q_ang`. Sólo `L >= sqrt(3)/2` la garantiza. **Sin corpus no se afirma nada sobre
cuánto contenido real cae en `[0.3125, 0.866)`**: eso es exactamente lo que no se puede medir acá.

## 9. Lo que este documento NO establece (requisitos 8 y 10)

- **Nada sobre la distribución de assets reales.** Todo es sintético y adversarial; el corpus
  está prohibido en este slice. La tasa de rechazo de `sqrt(3)/2` sobre contenido legítimo es
  **desconocida**.
- **Que el peor caso sea alcanzable por un asset legítimo.** La construcción exige un campo
  diseñado para cancelar; que una textura real lo realice no está establecido.
- **Que el modelo declarado sea el correcto.** La cota es condicional al modelo `|eps_c| <= q/2`
  con renormalización. Si el modelo cambia, la cota cambia (linealmente en `Delta_max`).
- **Ninguna decisión normativa.** `R7_NUMERIC_DERIVATION` sigue `BLOCKED`: lo que falta ya no es
  una derivación, es la **adjudicación** de si el criterio de aceptación es worst-case (⇒ `0.866`)
  o estadístico, y de si el costo de cobertura es aceptable.

## 10. Recomendación (separada por naturaleza)

**Garantía matemática (demostrada).** Bajo el modelo declarado,
`delta_theta <= asin((q/2)*sqrt(3)/L)`, y por lo tanto `L >= sqrt(3)/2` **garantiza**
`delta_theta <= q_ang`. La cota es alcanzable (construcción cerrada exacta), no vacua, y se
sostiene sobre la implementación real (Pillow modo F, `ratio = 0.9977`).

**Observación sintética.** El cruce del modelo completo es `sqrt(3)` veces el de la
reconciliación: `0.866`, no `0.5`. La cota es ajustada a `~1e-6` relativo.

**Riesgos no cubiertos.** La tasa de rechazo sobre contenido legítimo; la plausibilidad del campo
extremo; la validez del propio modelo de cuantización.

**Consecuencia para la decisión.** Si el criterio es worst-case, el único valor sostenido por
evidencia es `sqrt(3)/2` — y su costo de cobertura es **desconocido** sin corpus, lo que
probablemente lo vuelve inaceptable en la práctica. Si el criterio es estadístico, hay que
**declararlo** (hoy `5/16` se presenta como derivación, no como estadístico). En cualquier caso
la decisión es del Tech Lead; este documento no la toma.

```text
R7_NUMERIC_DERIVATION        = BLOCKED          (SIN CAMBIO — la derivación existe; falta la adjudicación)
R7_THRESHOLD_KIND            = HEURISTIC (RMS)  (SIN CAMBIO)
R7_THRESHOLD_VALUE           = 5/16             (CONSERVADO; prohibido recalibrar con corpus)
R7_WORST_CASE_CROSSING       = 1/2              (token de #723, familia coherente de UN canal)
R7_WORST_CASE_BOUND_ANGULAR  = asin((q/2)*sqrt(3)/L)   (token NUEVO de este slice)
R7_WORST_CASE_CROSSING_FULL  = sqrt(3)/2 = 0.866       (token NUEVO; el modelo COMPLETO)
R7_WORST_CASE_PREMISE        = CONJECTURE_NOT_FALSIFIED (gate #717 §6.1, FROZEN — no se toca)
F01_CONTRACT_AMENDMENT       = PENDING_TECH_LEAD
H4_CODE_MERGE_AUTHORIZED     = NO
M6_IMPLEMENTATION_BLOCKED    = YES
```
