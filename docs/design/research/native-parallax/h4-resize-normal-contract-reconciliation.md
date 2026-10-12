# H4 `resize_normal` — Reconciliación de contratos #711 / #717 / #723

> **Naturaleza del documento.** Registro de **reconciliación**, no de decisión. Compara el
> contrato congelado de #711 (`h4-resize-normal-preflight.md`) y #717
> (`h4-resize-normal-authorization-gate.md`) contra la implementación de #723, delimita los
> dos bloqueantes (F01 y F02), presenta alternativas con evidencia medida y **recomienda**.
> No cambia ninguna fila `FROZEN`, no recalibra `DEGENERATE_THRESHOLD`, no toca el Golden
> Master #718, EXP-M6 #675, el corpus ni artefactos históricos, y no fusiona #723.

## 0. Procedencia

| Campo | Valor |
|---|---|
| Issue | #721 (Slice 2) |
| PR bajo reconciliación | #723 (**Draft**) |
| Rama | `research/native-parallax-h4-resize-normal-fix` |
| HEAD reconciliado | `3f3fe047e54343af88654ccecf1c981fd8859115` |
| Base | `da738b75` (`origin/main` al abrir el slice) |
| Worktree | `E:/Skyclaw_Main_Sync/.worktrees/h4-resize-normal-fix` |
| Estado del árbol al medir | limpio (`git status --porcelain` vacío) |
| Contratos contrastados | #711 preflight (V-01, §7.3, §8.0, §17.8) · #717 gate (§3.2, §3.4, §3.4.1, §5.3.4.7, §6.1) · #723 adjudicación |
| Medición | implementación real (`Pillow` modo F, `float32`) **más** oráculo NumPy independiente |
| Corpus real | **NO** usado. Todo sintético |

**Guard de procedencia (obligatorio en esta máquina).** El venv del repo tiene un
`editable install` que apunta a `E:\Skyclaw_Main_Sync` (el worktree **principal**). Un script
corrido fuera del worktree importa el código **baseline histórico** en silencio. Toda
medición de este documento lleva la aserción
`assert "h4-resize-normal-fix" in authored_dataset.__file__`. Sin ese guard, la primera
corrida de F01 reportó el comportamiento del baseline y no el del PR.

---

## 1. F01 — dominio geométrico del camino identidad

### 1.1 Las cinco clases, medidas contra el contrato y contra la implementación

`size = 4`, entrada constante (dirección confiable) de norma `L`. `H == W == size` ⇒ camino
identidad. Medición directa sobre `resize_normal_diagnostic` y `resize_normal`.

| Clase de entrada | `L` entrada | #711 V-01 | #717 §3.2 | #717 §3.4.1 paso (4) | #723 implementación | ¿Acuerdo? |
|---|---|---|---|---|---|---|
| Normal unitaria válida | `1.0` | no-op válido | norma `≈ 1` | no-op | **no-op exacto** (`array_equal` True) | Sí |
| Vector exactamente nulo | `0.0` | rechazar (geométricamente inválida) | `NON_EVALUABLE` | `ADMISIBLE` **no mira la norma** ⇒ no-op | `degenerate=True`, censo `16/16`, fachada **lanza** | Sí con V-01 y §3.2; §3.4.1 tiene el vacío |
| Finito, en rango, norma `0.5` | `0.5` | **indefinido** (¿"geométricamente inválida"?) | norma `≈ 1` **o** `NON_EVALUABLE` | `ADMISIBLE` ⇒ no-op ⇒ publicaría `0.5` (viola §3.2) | **renormaliza** a norma `1`, dirección conservada (`cos = 1.0`) | **Divergencia con la lectura literal de V-01** |
| Fuera de rango, finito (`1000.0`) | `1732.05` | rechazar (geométricamente inválida) | norma `≈ 1` **o** `NON_EVALUABLE` | clip declarado, **no** es no-op | clip a `(1,1,1)` y **renormaliza** a norma `1` | **Divergencia con la lectura literal de V-01**; acuerdo con N-11 |
| No finito (`NaN`, `±Inf`) | `nan` / `inf` | rechazar | sin NaN/Inf | — | **`InvalidNormalShapeError`** | Sí |
| Estructuralmente inválido | — | rechazar | — | — | **`InvalidNormalShapeError`** | Sí |

Verificado además:

| Propiedad | Medición |
|---|---|
| Simetría de hermanos (misma entrada constante) | `L=0.30` ⇒ ambos `degenerate=True`; `L=0.32 / 0.5 / 1.0` ⇒ ambos publican norma `1`. **Idénticos** |
| `(1,1,1)` en rango, norma `sqrt(3)` | clip no-op, luego renormaliza a `(0.5773, 0.5773, 0.5773)`, `L=1` |
| Tolerancia `UNIT_NORM_TOLERANCE = 1e-6` | `L = 1 - 5e-7` conserva el no-op; `L = 1 - 2e-6` renormaliza. **Discrimina** |
| Dirección tras renormalizar | conservada exactamente (`cos = 1.000000000000`) |

### 1.2 Por qué hay contradicción: el término sin definir

Las tres reglas `CONTRACT_CLOSED` aplicables son:

- **§3.2 / I-03** — la salida tiene norma `≈ 1` **o** estado explícito `NON_EVALUABLE`;
  nunca un valor intermedio silencioso. `POSTCONDITION_HAS_IDENTITY_EXCEPTION = NO`.
- **§3.4.1 paso (4)** — `H == W == size` es no-op que devuelve "la entrada ya admisible", y
  `ADMISIBLE(n) := forma válida Y finito Y n en [-1,1]`. **La norma no aparece.**
- **§3.1** — `Normalización previa: Unitaria (ya renormalizada)`, `CONFIRMED`: el contrato
  **asume** que la entrada ya es unitaria.
- **§3.4 / N-11** — `FUERA_DE_RANGO_FINITO = CLIP, TOLERADO`, con test exigido (§10.1).

V-01 dice que las entradas "no finitas **o geométricamente inválidas**" deben **rechazarse** y
no corregirse por renormalización, pero **no define** "geométricamente inválida". De ahí salen
dos lecturas:

```text
LECTURA R1  geometricamente_invalida := NO FINITO  O  FORMA MALFORMADA  O  L < DEGENERATE_THRESHOLD
LECTURA R2  geometricamente_invalida := L != 1   (la norma no unitaria ES la invalidez)
```

### 1.3 Separación obligatoria: validez de entrada vs. longitud del intermediario

Dos proposiciones **distintas** que R2 mezcla y que hay que mantener separadas:

| Proposición | Enunciado | ¿Universal? |
|---|---|---|
| **P-ENTRADA** | La normal **de entrada** ya decodificada tiene norma unitaria (lo promete §3.1: `decode_normal_image` renormalizó) | Es una **precondición de contrato**, exigible |
| **P-INTERMEDIO** | El vector **interpolado** que produce el remuestreo tiene norma unitaria | **No** — es una propiedad del campo, no de la validez de la entrada |

**P-INTERMEDIO no es universal, y conviene no invocarla como si lo fuera.** La interpolación
bilineal de un campo unitario **constante** preserva la norma exactamente (`L = 1`); la reduce
cuando el campo **varía**, porque promedia direcciones distintas. Es decir: el intermediario
tiene norma `1` o `< 1` según la variación local del campo, no según si la entrada era válida.

Esa corrección **debilita** el argumento fácil contra R2 y hay que decirlo: no se puede
descartar R2 diciendo "la interpolación siempre da norma `< 1`". R2 es **implementable** para el
camino identidad — exigir norma unitaria a una normal ya decodificada es una precondición
legítima sobre P-ENTRADA. Lo que R2 no puede hacer es aplicarse al **intermediario** del camino
`resize`, porque ahí la norma no unitaria la **produce la operación**, no el llamador.

Los costos reales de R2, medidos y no retóricos:

- **El clip de N-11 necesita una decisión explícita.** El clip declarado de `1000.0` da
  `(1,1,1)`, de norma `sqrt(3) != 1`. Bajo R2 estricto habría que rechazarlo, contradiciendo
  `FUERA_DE_RANGO_FINITO = CLIP, TOLERADO`. La salida **existe**: tolerar el clip sólo cuando la
  entrada **efectivamente requirió** clipping (cualquier `abs(v) > 1` pre-clip) y rechazar la
  norma no unitaria **en rango**. Es observable desde la entrada, así que es implementable — pero
  es una regla nueva, no una lectura de la existente.
- **La asimetría con `resize` es semántica, no automáticamente un defecto.** §5.3.4.7 fija la
  simetría de hermanos y AGENTS.md marca "arreglar un hermano y no al otro" como la clase de
  defecto dominante. Pero los dos caminos **no** son simétricos en su semántica: en identidad el
  vector no unitario es del **llamador**; en `resize` es del **algoritmo**. Que R2 sea correcto
  depende de si el Tech Lead considera esa diferencia semántica suficiente para justificar el
  trato distinto — y eso es exactamente la decisión pendiente, no un hecho que se pueda medir.

Bajo **R1**, las cuatro reglas cierran sin contradicción: el vector nulo cae en
`L < DEGENERATE_THRESHOLD` ⇒ `NON_EVALUABLE`; el clip se tolera y su resultado se renormaliza
como en el hermano; y el único defecto real es que **§3.4.1 omitió la cláusula de norma en
`ADMISIBLE(n)`** — el vacío que F01 identifica. Pero R1 es una **enmienda propuesta**, no la
interpretación obligatoria de V-01.

### 1.4 Matriz de decisión F01

| Opción | Qué se decide | Consistencia con #717 | Efecto en la tasa de rechazo | Costo |
|---|---|---|---|---|
| **A — Enmienda definicional (recomendada)** | Fijar `geometricamente_invalida` con R1; `ADMISIBLE(n)` incorpora `L >= DEGENERATE_THRESHOLD` | Total: §3.2, N-11, §5.3.4.7 y §3.4.1 quedan coherentes | **Ninguno**: no se agrega ningún rechazo | Firma del Tech Lead + nota de enmienda en V-01. **Cero código** |
| **B — Rechazo estricto (V-01 literal, sobre P-ENTRADA)** | El camino identidad exige `abs(L-1) <= TOL` en la entrada ya decodificada, con carve-out explícito para el clip | **Implementable**, pero exige una **regla nueva** en N-11: tolerar el clip sólo si la entrada requirió clipping (`abs(v) > 1` pre-clip) y rechazar la norma no unitaria **en rango**. El carve-out **sí** es observable desde la entrada | Aumenta, **no medido** (requeriría censo de corpus: prohibido acá) | Cambio de contrato mayor; la asimetría con `resize` queda, y su justificación es semántica (el no unitario es del llamador, no del algoritmo) |
| **C — `NON_EVALUABLE` para todo `L != 1` en identidad** | El camino identidad marca degenerado cualquier no unitario | Redefine D-06 (que se ancla en `L < umbral`) y contradice N-11 para el clip | Aumenta mucho | Incoherente con el hermano; **descartada** |

**Ninguna de las tres se declara "imposible".** A y B son las dos políticas viables; C se
descarta por incoherencia con D-06 y N-11, no por imposibilidad técnica.

**Recomendación técnica: A**, con la salvedad de que es una **enmienda propuesta**, no la
interpretación obligatoria de V-01. A favor: satisface §3.2, N-11, §3.4.1 y §5.3.4.7 sin
recalibrar nada, y su costo es una **definición**. En contra: deja pasar como "no error" una
entrada que viola P-ENTRADA (`L != 1` en rango), y ese es el riesgo que B elimina. La
implementación de #723 ya es la de A, así que elegir A no mueve código; elegir B sí.

**Si el Tech Lead elige B**, la enmienda es de mayor alcance y debe declararse como tal:
hay que escribir la regla del carve-out del clip (tolerar sólo si la entrada requirió
clipping) y decidir explícitamente si la asimetría con `resize` es aceptable. No es una
corrección de una línea, pero tampoco es imposible — el carve-out es observable.

```text
F01_CONTRACT_AMENDMENT          = PENDING_TECH_LEAD
F01_IMPLEMENTATION_MATCHES      = OPTION_A (renormalizar; simetría con `resize`)
F01_REJECTS_NON_FINITE          = YES   (medido)
F01_REJECTS_MALFORMED           = YES   (medido)
F01_RENORMALIZES_IN_RANGE       = YES   (medido; la decisión pendiente)
F01_RESIDUAL_RISK               = DECLARED (un llamador con dirección confiable y norma
                                  no unitaria recibe una normal unitaria en vez de un error)
```

---

## 2. F02 — justificación de `DEGENERATE_THRESHOLD = 5/16`

### 2.1 Los cuatro objetos, separados y medidos

| Objeto | Qué es | Valor medido | ¿Acreditado? |
|---|---|---|---|
| Mínimo geométrico alcanzable | ínfimo de `L` sobre el barrido factor × fase × borde | `inf L = 0`, alcanzado a precisión de máquina en las 30 combinaciones | Sí — reconfirmado independientemente |
| Amplificación RMS | `sum(w^2)` del kernel separable | `0.3125 / 0.1641 / 0.0830 / 0.0416` para `k = 2 / 4 / 8 / 16` | Sí — reproducida |
| **Peor caso de error angular** | cruce `delta_theta(L) = q_ang` bajo perturbación adversarial | **`L = 1/2`**, en los cuatro factores | **No acotado por `5/16`** |
| Garantía del dominio admitido | `resize_normal(n, size)` acepta **cualquier** `size >= 1`, i.e. cualquier factor `k` | `sum(w^2) <= 5/16` para todo `k >= 2`; `> 5/16` para `1 < k < 2` | Parcial: el riesgo no es sólo de factor |

### 2.2 El ínfimo y el umbral son proposiciones distintas (reconfirmado)

El barrido factor × fase × borde alcanza `min L = 0` en las 30 combinaciones, interior **y**
borde. Como `L >= 0` para todo vector, ninguna configuración soportada puede dar una norma
menor: el ínfimo es `0` y Nyquist lo alcanza. Control negativo verificado: la familia de `L`
analítico `0.5` da norma mínima `~0.5`, así que el cero no es un artefacto del oráculo.

Esto **acota el mínimo de `L`** —cuán degenerado puede llegar a estar el campo— y **no**
acota el umbral de rechazo. La premisa Nyquist sigue `CONJECTURE_NOT_FALSIFIED` en el gate
#717 §6.1 (`FROZEN`); este documento no la toca.

### 2.3 El peor caso de error angular: `L = 1/2`, no `5/16`

El modelo de perturbación declarado por la propia derivación es: cada píxel de entrada está
cuantizado en una retícula de paso `q = 2/255`, el error por canal es `|d_c| <= q/2`, y el
vector perturbado se renormaliza. **Ese modelo no exige independencia entre píxeles.** El
peor caso estructural es la perturbación **coherente**: la misma `(q/2) * u` en todos los
píxeles, con `u` perpendicular al vector interpolado. Los pesos del kernel bilineal son
positivos y suman `1`, así que la perturbación coherente **no se promedia: suma**.

```text
v_interpolado        = sum_ij w_i w_j n_ij
perturbacion         = (q/2) * u   en TODOS los píxeles,  u perpendicular a v
dv                   = (q/2) * u * (sum_i w_i)(sum_j w_j) = (q/2) * u
delta_theta(L)       = atan( (q/2) / L )        [exacto para q chico]
cruce delta_theta = q_ang = q   =>   L = 1/2    [exacto]
```

Medición sobre la implementación real (`Pillow` modo F) y sobre el oráculo independiente, en
`k ∈ {2, 4, 8, 16}` — idéntico en los cuatro:

| `L` | `delta_theta` adversarial medido | Ratio sobre `q_ang` | ¿El umbral lo declara evaluable? |
|---|---|---|---|
| `0.2000` | `1.961e-02` | `2.50x` | No (bajo el umbral) |
| `0.3125` | `1.255e-02` | `1.60x` | **Sí** |
| `0.3500` | `1.120e-02` | `1.43x` | **Sí** |
| `0.4000` | `9.804e-03` | `1.25x` | **Sí** |
| `0.4500` | `8.714e-03` | `1.11x` | **Sí** |
| `0.5000` | `7.843e-03` | `1.00x` | Sí — el criterio se cumple |
| `0.6000` | `6.536e-03` | `0.83x` | Sí |

En la banda `[5/16, 1/2)`, **15 de 16** puntos de la malla superan `q_ang`. Búsqueda numérica
sobre 512 direcciones unitarias: el óptimo es siempre `u* = (0, 1, 0)`, el eje con componente
nula en el vector limpio — la dirección que el patrón alternado **no** cancela.

**Alcance de la falsación — leer con cuidado.** Lo que queda falsado es la pretensión de
**garantía** de `5/16` bajo perturbaciones admitidas cuando no se exige independencia entre
píxeles. Lo que **no** queda establecido:

- Que `L = 1/2` sea el peor caso sobre **todas** las perturbaciones permitidas por
  `abs(eps_c) <= q/2` con renormalización. La cota de magnitud agregada antes de modelar la
  renormalización (`||eps|| <= (q/2) * sqrt(3)`) no está traducida a una cota de error angular.
  La relación entre `delta_theta`, la norma filtrada y la **correlación** entre píxeles es
  justamente lo que falta especificar.
- Que el adversario coherente sea alcanzable por **todo** campo admitido: se construye sobre la
  familia Nyquist, donde el eje `(0,1,0)` tiene componente nula en el vector limpio.
- Nada sobre la distribución de assets reales: todo es sintético y sin corpus.

Por eso la conclusión es "el umbral no es una garantía", **no** "el umbral correcto es `1/2`".

### 2.4 Falsación del adversario que decía cubrir el peor caso

`_cruce_peor_caso_dirigido` (implementación previa de este PR) devolvía `0.0200` en los
cuatro factores, y `test_g_el_peor_caso_dirigido_tambien_queda_bajo_el_umbral` pasaba con
`assert cruce < 0.3125`. Medición del error angular que ese adversario efectivamente produce:

| `L` | `delta_theta` del adversario previo | `q_ang` |
|---|---|---|
| `0.0200` | `0.000e+00` | `7.843e-03` |
| `0.1000` | `0.000e+00` | `7.843e-03` |
| `0.3125` | `0.000e+00` | `7.843e-03` |
| `0.4000` | `0.000e+00` | `7.843e-03` |
| `0.6000` | `0.000e+00` | `7.843e-03` |

`d[..., 0] = ±(q/2) * s` está **perfectamente correlacionada** con el patrón alternado `s`; la
renormalización por píxel deja la misma norma en todos los píxeles, la componente `x` sigue
alternando y el kernel simétrico la cancela **exacta**. El adversario medía **cero**: el cruce
caía en el primer punto de la grilla y la aserción pasaba sin medir nada. El guard `nan` de la
función no ayuda, porque el criterio se cumple trivialmente en todo `L`.

**Retirado** (misma clase que `H4-F02-ANCLA-BORDE` y `H4-F02-ANCLA-COTA`), y sustituido por un
adversario discriminante. Es una corrección de **evidencia**, no del umbral.

### 2.5 Matriz de decisión F02

| Opción | Qué se decide | Garantía que queda declarada | Efecto en la tasa de rechazo (medido, sintético) | Riesgo |
|---|---|---|---|---|
| **1 — Conservar `5/16` y declarar el criterio RMS** | El criterio de aceptación (§17.8 / §6.1.3) es la **amplificación RMS**; el contrato **no** promete cota worst-case | Estadística: `delta_theta_rms` cruza `q_ang` en `0.0925` (k=2) | La actual | La banda `[5/16, 1/2)` se publica con error de hasta `1.60x q_ang`. Requiere firma explícita del riesgo |
| **2 — Subir el umbral a `1/2`** | Cubre el adversario coherente medido | **Parcial, y hay que decirlo**: cubre la familia coherente de magnitud `q/2` por canal en un eje perpendicular, **no** todas las perturbaciones admitidas por `abs(eps_c) <= q/2` con renormalización. Acotar esa familia exige una cota verificable que este slice **no** aporta | **0.0000 %** en superficie 1/f (pendientes 0–2.5, 1024→512, 12 corridas). Pero `99.61 %` en un patrón periódico p=4 y `95.65 %` en ruido blanco | Rechaza assets estructurados hoy aceptados. No es "gratis", y **no** cierra R7 por sí sola |
| **3 — Conservar `5/16` y añadir un estado intermedio `LOW_CONFIDENCE` para `[5/16, 1/2)`** | No se rechaza nada; la incertidumbre se **propaga** al consumidor | Explícita y honesta en ambos regímenes, y **no** pretende ser worst-case | **Ninguno** | Cambia la máquina de estados de §3.2 (hoy `EVALUABLE` / `NON_EVALUABLE`) y exige decisión de consumo en M6 |

**Alcance de la evidencia de tasa de rechazo.** Las fracciones de la tabla siguiente son de las
**familias sintéticas ensayadas** (Nyquist, periódicos, ruido blanco, superficies 1/f
generadas), **no** de la distribución de assets reales: sin corpus (prohibido en este slice) no
se puede afirmar nada sobre ella. En particular, `0.0000 %` en 1/f significa "en estas 12
corridas sintéticas", no "en el corpus".

Evidencia de tasa de rechazo (contenido **sintético**, sin corpus), fracción de píxeles bajo
cada umbral a 1024→512:

| Contenido | `< 5/16` | `< 0.40` | `< 0.50` | `< 0.60` |
|---|---|---|---|---|
| Nyquist puro (período 2) | `1.0000` | `1.0000` | `1.0000` | `1.0000` |
| Periódico p=3 | `1.0000` | `1.0000` | `1.0000` | `1.0000` |
| Periódico p=4 (cuadrada) | `0.5000` | `0.9961` | `0.9961` | `1.0000` |
| Periódico p=4 (rotación pura) | `0.0000` | `0.0000` | `0.0000` | `0.9961` |
| Ruido blanco | `0.5904` | `0.8205` | `0.9565` | `0.9949` |
| Superficie 1/f, pendiente 0.0–2.5 | `0.0000` | `0.0000` | `0.0000` | `0.0000` |

Lectura: sobre contenido realista **suave** (1/f) la banda `[5/16, 1/2)` está **vacía** — subir
el umbral no costaría nada medible. Sobre contenido **estructurado** (periódico, ruido) la
banda es densa, pero ahí la dirección **tampoco** es confiable: rechazar es lo correcto. El
único costo real de la opción 2 es la pérdida de cobertura sobre assets periódicos legítimos
(p. ej. texturas con motivo regular), que este slice **no** puede cuantificar sin corpus.

**Recomendación técnica.** No hay base medida para `5/16` como garantía: `R7_NUMERIC_DERIVATION`
debe **seguir `BLOCKED`**. Entre las tres, la que más reduce el error publicado es **2** —el
cruce del adversario coherente medido— pero **2 no cierra R7**: cubre una familia de
perturbación, no todas las admitidas por `abs(eps_c) <= q/2`. Lo que sí cierra el bloqueante es
una **especificación de fiabilidad explícita** (determinista con cota verificable, o estadística
aprobada) junto con el **dominio de resize soportado**; sin eso, cualquier valor es una
convención. Si se prefiere preservar cobertura, **3** es preferible a **1** porque no oculta la
incertidumbre. La opción **1** sólo es defendible con una firma explícita que acepte el riesgo
residual y **cambie el criterio de aceptación** de §17.8 de "garantía" a "heurística RMS
declarada".

```text
R7_NUMERIC_DERIVATION       = BLOCKED          (SIN CAMBIO — no autodeclarar PASS)
R7_THRESHOLD_KIND           = HEURISTIC (RMS)  (SIN CAMBIO)
R7_THRESHOLD_VALUE          = 5/16             (CONSERVADO; prohibido recalibrar con corpus)
R7_WORST_CASE_CROSSING      = 1/2              (token NUEVO de este slice)
R7_WORST_CASE_PREMISE       = CONJECTURE_NOT_FALSIFIED  (gate #717 §6.1, FROZEN)
R7_ACCEPTANCE               = BLOCKED          (decisión del Tech Lead)
```

---

## 3. Cambios implementados en este slice (no normativos)

Todos son correcciones de **evidencia** o de **documentación**. Ninguno cambia
`DEGENERATE_THRESHOLD`, `UNIT_NORM_TOLERANCE`, el comportamiento de producción, ni una fila
`CONTRACT_CLOSED`.

| Archivo | Cambio | Naturaleza |
|---|---|---|
| `tests/test_native_parallax_h4_resize_normal.py` | Retira el adversario vacuo y añade uno discriminante (`_delta_theta_peor_caso_coherente` + `_cruce_peor_caso_coherente`), su **control negativo** y el ancla de la retiración | Evidencia |
| `sky_claw/.../authored_dataset.py` | Tokens nuevos `R7_WORST_CASE_CROSSING = 1/2` y `F01_CONTRACT_AMENDMENT = PENDING_TECH_LEAD`; retira la afirmación "también bajo el peor caso dirigido" | Comentario |
| `docs/design/research/native-parallax/h4-resize-normal-f01-f02-adjudication.md` | §3.1 retira el 9.º ancla; §3.2 retira la afirmación de producción; §2.6 registra el token nuevo | Documentación |

Anclas ejecutables nuevas:

| Test | Qué discrimina |
|---|---|
| `test_g_el_adversario_alineado_con_el_patron_mide_cero` | reintroducir el adversario vacuo como evidencia rompe la aserción |
| `test_g_el_peor_caso_coherente_cruza_en_un_medio_y_supera_el_umbral` | el cruce del peor caso estructural es `1/2` y supera el umbral; el token declarado en producción coincide con lo medido |
| `test_g_control_negativo_del_peor_caso_coherente_discrimina` | **control negativo versionado**: en `L = 0.45` el criterio FALLA y en `L = 0.55` SE CUMPLE, con la misma medición. Una métrica constante (siempre `> q_ang`, o siempre `0`) cae |

**El contraejemplo vive en tests versionados, no en `scratch/`.** Los probes de §3.3 son
reproducción auxiliar; las anclas que el gate ejecuta son las tres filas de arriba.

### 3.3 Reproducción

Los probes que produjeron cada tabla viven en `scratch/h4-s2-reconciliation/` (directorio
`gitignoreado`: **no** entra en el PR). Todos llevan el guard de procedencia y se corren desde el
worktree, con el `PYTHONPATH` explícito — sin él, el `editable install` del venv importa el
worktree **principal** y la medición es del baseline histórico:

```bash
cd E:/Skyclaw_Main_Sync/.worktrees/h4-resize-normal-fix
PYTHONPATH="E:/Skyclaw_Main_Sync/.worktrees/h4-resize-normal-fix" \
  E:/Skyclaw_Main_Sync/.venv/Scripts/python.exe scratch/h4-s2-reconciliation/f02c_autoritativo.py
```

| Probe | Tabla que produce |
|---|---|
| `f01_probe.py` / `f01b_matriz.py` | §1.1 — las cinco clases y la simetría de hermanos |
| `f02c_autoritativo.py` | §2.3 y §2.4 — cruces con el oráculo **del propio archivo de tests** |
| `f02b_decisivo.py` | §2.3 — búsqueda sobre 512 direcciones; §2.4 — vacuidad del adversario previo |
| `f02d_tasa_rechazo.py` / `f02e_controles.py` / `f02f_periodo5.py` | §2.5 — tasa de rechazo sintética |
| `mutacion.py` | barrido de mutación con restauración verificada por `md5` |
| `check_docs.py` | tablas de este documento con el checker **importado** del repo |

Barrido de mutación ejecutado (4/4 muertos, restauración verificada por `md5` y
`git status --porcelain` vacío tras cada uno):

| Mutante | Cambio | Resultado |
|---|---|---|
| M-01 | `R7_WORST_CASE_CROSSING = 1/2` → `1/4` en producción | muerto |
| M-02 | dirección del adversario coherente `d[...,1]` → `d[...,0]` | muerto |
| M-03 | adversario de la retiración vuelve a medir `!= 0` | muerto |
| M-04 | `DEGENERATE_THRESHOLD = 5/16` → `4/16` | muerto (`3 failed, 68 passed`) |
| M-05 | control negativo: métrica constante (devolver `q_ang + 1`) | muerto |

Checks del slice: `pytest` de las tres suites afectadas **148 passed**; `ruff check sky_claw/ tests/`
y `ruff format --check sky_claw/ tests/` limpios; `mypy sky_claw/` sin issues (364 archivos).
Falla **ambiental preexistente**, ajena a este slice: `test_snapshot_cleanup_por_patron.py` (4,
symlinks sin privilegio en el sandbox) y `test_worktree_convention_invariant.py` (1, worktrees de
otras sesiones fuera de convención).

## 4. Lo que este slice NO hace

- No pasa #723 a Ready ni lo fusiona.
- No cambia `DEGENERATE_THRESHOLD`, `UNIT_NORM_TOLERANCE` ni el comportamiento de producción.
- No reescribe el gate #717 (FROZEN) ni el preflight #711.
- No ejecuta el corpus real, no inicia M6, no recalibra ningún umbral con corpus.
- No toca Golden Master #718, EXP-M6 #675, `docs/validation/` ni PR #675.

```text
H4_CODE_MERGE_AUTHORIZED    = NO
M6_IMPLEMENTATION_BLOCKED   = YES
REAL_CORPUS_RERUN           = NO
PR675_CHANGED               = NO
GOLDEN_MASTER_718_CHANGED   = NO
H4_F01_CONTRACT_AMENDMENT   = PENDING_TECH_LEAD
H4_R7_ACCEPTANCE            = BLOCKED
ADVERSARIAL_REVIEW_COMPLETED = NO  (Qodo/CodeRabbit no corren sobre Draft)
SAFE_TO_MERGE               = NO
```

## 5. Recomendación al Tech Lead

1. **F01** — adjudicar **dos políticas viables**, ninguna "imposible". **A** (recomendada): fijar
   `geometricamente_invalida` con R1 y completar `ADMISIBLE(n)` con la cláusula de norma; cero
   código, cero cambio de tasa de rechazo, pero deja pasar como "no error" una violación de
   P-ENTRADA. **B**: exigir norma unitaria a la normal ya decodificada, con carve-out explícito
   del clip; elimina ese riesgo pero requiere una regla nueva en N-11 y aceptar la asimetría con
   `resize`, cuya justificación es semántica. **C** queda descartada por incoherencia con D-06.
2. **F02** — lo que cierra el bloqueante **no es un valor de umbral** sino una **especificación de
   fiabilidad**: determinista con cota verificable, o estadística aprobada, **más el dominio de
   resize soportado**. Sobre esa base, elegir entre conservar `5/16` con el criterio **RMS
   declarado y firmado** (opción 1), subir el umbral a `1/2` (opción 2, cubre el adversario
   coherente pero **no** todas las perturbaciones admitidas), o añadir `LOW_CONFIDENCE`
   (opción 3, sin costo de rechazo). `R7_NUMERIC_DERIVATION` sigue `BLOCKED` hasta esa firma.
3. Recién entonces: pasar #723 a Ready for Review para que Qodo/CodeRabbit revisen sobre HEAD
   exacto, adjudicar findings y revalidar CI.
