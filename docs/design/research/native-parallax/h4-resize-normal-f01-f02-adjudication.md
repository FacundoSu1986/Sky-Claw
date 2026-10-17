# H4 `resize_normal` — Adjudicación F01/F02 (dictamen del PR #723)

> **Naturaleza del documento.** Registro de decisión. **No modifica** el contrato de #717
> (`h4-resize-normal-authorization-gate.md`): lo **completa** donde ese contrato tenía un vacío
> (F01) y **retira dos afirmaciones numéricas no medidas** de su procedimiento R7 (F02).
> Ninguna fila de estado del contrato de #717 cambia de valor **por este documento**: el gate
> está `FROZEN` y su literal se respeta. La evidencia nueva de F02 se registra bajo su **propio
> token** (`R7_NYQUIST_ATTAINMENT`), precisamente para no reescribir una fila congelada.

## 0. Procedencia

| Campo | Valor |
|---|---|
| Issue | #721 (Slice 2, autorización vigente) |
| PR | #723 (**Draft**, no se fusiona) |
| Rama remota | `research/native-parallax-h4-resize-normal-fix` |
| Base | `da738b75` (= `origin/main` al abrir el slice) |
| Dictamen de origen | comentario `6129522327` del PR #723, `CHANGES_REQUESTED` |
| Alcance | F01 (P1) y F02 (P2). **No** se toca #718 (Golden Master), #675 (EXP-M6), corpus ni artefactos históricos |

## 1. F01 — dominio geométrico del camino identidad

### 1.1 El defecto, medido

`_validar_envolvente_normal` valida forma, finitud y `size`, pero **no geometría**, y la rama
identidad (`H == W == size`) devolvía el array clipeado declarando `degenerate=False` **sin medir
una sola norma**. Medición contra el HEAD auditado, con `size = 4` y las dos superficies:

| Entrada | Norma de entrada `L` | Salida **vigente** | ¿Contrato §3.2? |
|---|---|---|---|
| Vector exactamente cero | `0` | `resize_normal` devuelve el nulo; diagnóstico `degenerate=False` | **No** — D-06 exige `NON_EVALUABLE` |
| Norma `0.5`, en rango y finita | `0.5` | Devuelve el array de norma `0.5`; `degenerate=False` | **No** — ni norma `≈1` ni estado explícito |
| Válida unitaria | `1` | No-op exacto; `degenerate=False` | **Sí** |
| Clipeado `1000.0` | `1732.05` | Clip a `(1,1,1)`, norma `sqrt(3) = 1.73205` | **No** — norma publicada `sqrt(3)` |

### 1.2 La contradicción y su adjudicación

Dos reglas `CONTRACT_CLOSED` de #717 apuntan en direcciones opuestas para una entrada de norma no
unitaria:

- **§3.2 / I-03** — la salida tiene norma `≈ 1` **o** estado explícito `NON_EVALUABLE`; nunca un
  valor intermedio silencioso. **No tiene excepción de identidad**
  (`POSTCONDITION_HAS_IDENTITY_EXCEPTION = NO`).
- **§3.4.1 paso (4)** — `H == W == size` es un no-op que "devuelve la entrada ya admisible; no
  remuestrea", y **N-11** declara `FUERA_DE_RANGO_FINITO = CLIP, TOLERADO` con test exigido
  (§10.1: `1000.0 -> tolerado/clip`, `NaN -> REJECT`).

La definición `ADMISIBLE(n)` de §3.4.1 enumera forma, finitud y rango **pero omite la norma**: ese
es el vacío que F01 identifica. Se cierra así:

```text
DOMINIO GEOMÉTRICO DEL CAMINO IDENTIDAD (cierre de F01)

  paso (1) envolvente estructural   -> REJECT si ndim / shape[-1] / H / W / size inválidos
  paso (2) finitud                  -> REJECT si NaN / Inf
  paso (3) clip declarado [-1,1]    -> N-11: se aplica ANTES de la bifurcación, uniforme
  paso (4) geometría, POR PÍXEL sobre el vector YA CLIPEADO, con L = ||v||:
      L < DEGENERATE_THRESHOLD          -> NON_EVALUABLE (D-06)   [se evalúa PRIMERO]
      |L - 1| <= UNIT_NORM_TOLERANCE    -> no-op EXACTO (D-07): sin remuestrear ni renormalizar
      en otro caso                      -> renormalización: el MISMO paso que el hermano `resize`

  ORDEN DE LAS DOS ÚLTIMAS CLÁUSULAS = LOAD-BEARING. El vector nulo satisface ambas
  (`L = 0 < 5/16` y `|0 - 1| > 1e-6`); se clasifica como NON_EVALUABLE porque la
  cancelación es una propiedad del DATO (D-06), no un error de dominio del llamador.
```

**Por qué renormalizar y no rechazar.** El clip es una operación contratada y **tolerada** por
N-11; rechazar su resultado haría que el mismo valor `1000.0` fuera tolerado por el camino
`resize` y rechazado por el de identidad — la contradicción que §3.4.1 ya había cerrado para el
rango, reintroducida por otra puerta. Además §5.3.4.7 fija la **simetría de hermanos** y AGENTS.md
identifica "arreglar un hermano y no al otro" como la clase de defecto dominante del repo. El
único paso que el camino identidad omite es **interpolar**; la postcondición de norma se satisface
con la operación que el hermano ya usa. La dirección **no se inventa**: se conserva la de la
entrada.

### 1.3 Tolerancia de norma: derivación sintética (§3.5, sin corpus)

```text
Cota analítica (ida/vuelta float32 -> float64, modo F de Pillow):
    |eps_c| <= 2**-24 por componente  =>  ||v_f32 - v|| <= sqrt(3) * 2**-24 = 1.032e-07
Piso medido (sintético, semilla 20261012, 8 x 4096 píxeles de la esfera unitaria):
    max |L - 1| = 4.69e-08
Tolerancia elegida:
    UNIT_NORM_TOLERANCE = 1e-6   (= 9.7x la cota analítica; 5.0e5 veces menor que la
                                  violación más pequeña que el contrato debe discriminar,
                                  |0.5 - 1| = 0.5)
```

La tolerancia es **discriminante**, no decorativa: una entrada de norma `1 - 2e-6` se renormaliza
y una de norma `1 - 5e-7` conserva el no-op. Un cambio futuro que la infle (mutante M-13) o la
anule (M-14) rompe la suite.

### 1.4 Divergencia declarada respecto de V-01 (riesgo residual)

El preflight V-01 dice, para entradas "no finitas o geométricamente inválidas", que deben
**rechazarse** con validación explícita y **no** corregirse por renormalización silenciosa. Esta
adjudicación **no renormaliza en silencio** (está declarada, es la operación contratada del
hermano y el resultado es observable), pero **tampoco rechaza**: elige la única opción compatible
con N-11 y con §3.2 simultáneamente.

```text
F01_RESOLUTION              = POSTCONDITION_ENFORCED_BY_SIBLING_OPERATION
F01_REJECTS_CLIP_RESULT     = NO  (N-11 lo prohíbe; §10.1 lo exige tolerado)
F01_NOOP_ONLY_WHEN_UNIT     = YES
F01_INVENTS_DIRECTION       = NO
F01_DIVERGES_FROM_V01_TEXT  = YES (DECLARADO — no se presenta como "rechazo")
F01_RESIDUAL_RISK           = DECLARED
```

**Riesgo residual, explícito:** un llamador que pase una normal no unitaria y de dirección
confiable recibe una normal unitaria en vez de un error. La invariante científica de H4 —*nunca
publicar un array que no sea una normal evaluable*— se sostiene en los dos caminos; lo que no se
sostiene es la lectura literal de V-01 para esa clase. Queda declarado para que el revisor lo
adjudique, en vez de quedar escondido.

### 1.5 Anclas ejecutables

| Test | Qué discrimina |
|---|---|
| `test_b_f01_el_cero_del_camino_identidad_es_non_evaluable` | `L = 0` en identidad: diagnóstico `degenerate=True` con censo completo, fachada que lanza |
| `test_b_f01_la_norma_no_unitaria_no_es_un_noop_valido` | `L = 0.5`: la versión previa anclaba `assert_array_equal(out, n)`; ahora se exige salida unitaria |
| `test_b_f01_el_clip_no_publica_norma_sqrt3` | `1000.0 -> (1,1,1)`: la salida no puede tener norma `sqrt(3)` |
| `test_b_f01_la_entrada_unitaria_conserva_el_noop_exacto` | la ÚNICA entrada que justifica el no-op bit a bit (la que produce `decode_normal_image`) |
| `test_b_f01_identidad_y_resize_son_simetricos_en_la_norma` | §5.3.4.7: la misma entrada por los dos caminos publica la misma postcondición |
| `test_b_f01_la_tolerancia_de_identidad_se_deriva_de_float32` | la tolerancia sale de la cota analítica + medición sintética, no del corpus |

## 2. F02 — estado de la derivación de `DEGENERATE_THRESHOLD`

### 2.1 Las tres cosas, separadas

El dictamen pide no mezclar tres objetos distintos. Quedan separados y rotulados:

| Objeto | Valor | Fuente |
|---|---|---|
| Heurística estadística (RMS) | `sum_k w_k^2` del kernel separable 2D | derivación + oráculo independiente |
| Garantía matemática worst-case | `(q/2) * sqrt(3)`, que **supera** `5/16` por `2.771x` | cálculo sobre la misma celda declarada |
| Barrido experimental | factor `k in {2,4,8,16}` x fase `fase in [0,k)` x borde | oráculo NumPy float64, semilla `20261012` |

### 2.2 Alcance del ínfimo (la premisa Nyquist, medida)

`R7_WORST_CASE_PREMISE` sigue `CONJECTURE_NOT_FALSIFIED` en el gate #717 §6.1, que está `FROZEN`:
**este documento no lo cambia.** N-12 prohíbe presentar la premisa como resultado sin derivación, y
un registro de adjudicación no reescribe una fila congelada. Lo que sí se hace es **medir el alcance
del ínfimo** y registrarlo bajo su propio token:

```text
En las 30 combinaciones (factor x fase), interior Y borde, el vector interpolado alcanza
    L <= 1e-12      (a precisión de máquina: es el cos(pi/2) de la familia analítica)
Como L = ||v|| >= 0 para todo vector, NINGUNA configuración soportada puede dar una norma
menor: el ínfimo es 0 y Nyquist lo alcanza.
=> R7_NYQUIST_ATTAINMENT  = ESTABLISHED_AS_ATTAINED_BOUND  (token NUEVO de este slice)
=> R7_WORST_CASE_PREMISE  = CONJECTURE_NOT_FALSIFIED       (SIN CAMBIO: gate FROZEN, N-12)
```

Control negativo: la familia de `L` analítico `0.5` da norma mínima `~0.5`, así que el cero
medido no es un artefacto del oráculo.

### 2.3 Dos afirmaciones retiradas por falsación

| Afirmación previa | Medición | Estado |
|---|---|---|
| "el recorte del kernel en el borde es MÁS benigno (no cancela del todo)" | el borde alcanza el **mismo** ínfimo que el interior (`<= 1e-12`) en las 30 combinaciones | **FALSADA** — retirada; ancla `H4-F02-ANCLA-BORDE` |
| cota decreciente `0.3125 -> 0.2346 -> 0.1641 -> ...` | la secuencia medida es `0.3125, 0.1640625, 0.0830078, 0.0416260`; `0.2346` no corresponde a ningún factor | **CORREGIDA** — ancla `H4-F02-ANCLA-COTA` |

### 2.4 El valor: conservadurismo medido, no garantía

```text
Pesos 1-D derivados del oráculo por respuesta a impulsos:
    k=2  -> 4 taps  [1/8, 3/8, 3/8, 1/8]   sum(w^2) = 0.3125000
    k=4  -> 8 taps                          sum(w^2) = 0.1640625
    k=8  -> 16 taps                         sum(w^2) = 0.0830078
    k=16 -> 32 taps                         sum(w^2) = 0.0416260

Cruce RMS medido (criterio: RMS(delta_theta) <= q_ang, q = 2/255; malla de paso 0.02,
así que son COTAS SUPERIORES del cruce real):
    k=2 -> <= 0.10      k=4 -> <= 0.06      k=8 -> <= 0.04      k=16 -> <= 0.02
Todos por debajo de DEGENERATE_THRESHOLD = 0.3125 (margen ~2.4x-3x).
Criterio NO trivial: en L = 0.02 el RMS lo incumple, así que la prueba discrimina.
```

**Corrección aritmética.** `5/16` es `sum_k w_k^2`, **no** `sqrt(sum_k w_k^2)`: esta última vale
`0.5590`. La justificación previa escribía `||dv|| = sqrt(sum_k w_k^2) * q = 5/16 * q`, que
confunde ambas cantidades. La cota declarada ahora es la correcta y su valor coincide con el
umbral de producción.

### 2.5 Riesgo residual declarado

Para `1 < k < 2` la amplificación **medida** supera el umbral, así que `5/16` deja de dominar esa
banda:

| Factor | `sum(w^2)` medido | ¿`<= 5/16`? |
|---|---|---|
| `1.1` | `0.5016` | No |
| `1.28` | `0.5053` | No |
| `1.5` | `0.4378` | No |
| `1.75` | `0.3883` | No |
| `2` | `0.3125` | Sí (igualdad) |
| `4` | `0.1641` | Sí |
| `8` | `0.0830` | Sí |
| `16` | `0.0416` | Sí |

El pipeline usa `k in {2, 4}` (corpus 1024/2048 -> 512), donde el umbral sí domina. La banda
`1 < k < 2` queda `RESIDUAL_RISK = DECLARED` y **medida**, no oculta.

### 2.6 Estado

```text
R7_NUMERIC_DERIVATION      = BLOCKED
    (no existe garantía matemática universal: el peor caso de la misma celda declarada
     es (q/2)*sqrt(3) = 2.771x el factor RMS, y el peor caso dirigido no está acotado
     por 5/16)
R7_THRESHOLD_KIND          = HEURISTIC (RMS)
R7_THRESHOLD_VALUE         = 5/16  (CONSERVADO; prohibido recalibrarlo con corpus)
R7_WORST_CASE_PREMISE      = CONJECTURE_NOT_FALSIFIED  (SIN CAMBIO: gate #717 §6.1, FROZEN)
R7_NYQUIST_ATTAINMENT      = ESTABLISHED_AS_ATTAINED_BOUND  (token NUEVO de este slice)
R7_WORST_CASE_CROSSING     = 1/2   (token NUEVO del slice de RECONCILIACIÓN; ver §3.1)
R7_VALUE_IS_CONSERVATIVE   = YES (medido, k en {2,4,8,16}, criterio predefinido)
R7_NUMERIC_VALUE_FROM_CORPUS = NO
M6_IMPLEMENTATION_BLOCKED  = YES
H4_CODE_MERGE_AUTHORIZED   = NO (queda a criterio del revisor)
```

`BLOCKED` acá significa **"la derivación publicada no sostiene una garantía universal"**, no
"el umbral es arbitrario": el valor queda respaldado como heurística conservadora con margen
medido. No se recalibró con corpus ni con resultados históricos (§6.1).

## 3. Anclas ejecutables de F02

| Test | Qué discrimina |
|---|---|
| `test_g_nyquist_alcanza_el_infimo_en_todo_factor_y_fase` | la premisa Nyquist como cota alcanzada (no discrimina el fix: el kernel es el mismo en ambos brazos — declarado) |
| `test_g_el_borde_no_es_mas_benigno_que_el_interior` | mantiene retirada la afirmación falsada del borde |
| `test_g_la_cota_rms_decrece_con_el_factor_y_la_secuencia_declarada_es_la_medida` | la secuencia declarada en producción se LEE y se compara con la medida |
| `test_g_el_riesgo_residual_de_factores_sub_dos_esta_medido` | la banda `1 < k < 2` supera el umbral (riesgo declarado, respaldado) |
| `test_g_el_criterio_se_sostiene_en_toda_la_cola_y_discrimina` | cierra la cláusula "para todo `L' >= L`" de §6.1.3 y prueba que el criterio no es trivial |
| `test_g_la_aritmetica_declarada_es_sum_w2_no_sqrt` | `5/16` es `sum(w^2)`, no `sqrt(sum(w^2))` |
| `test_g_la_propagacion_rms_no_es_cota_worst_case` | un RMS no acota el peor caso (`2.771x`) |

### 3.1 Retiración del 9.º ancla (slice de RECONCILIACIÓN #711/#717/#723)

`test_g_el_peor_caso_dirigido_tambien_queda_bajo_el_umbral` se **retira**: era **vacuo**.
Su adversario, `d[..., 0] = ±(q/2) * s`, está perfectamente correlacionado con el patrón
alternado `s` de `_familia_f02`; la renormalización por píxel deja la misma norma en todos
los píxeles, la componente `x` sigue alternando y el kernel simétrico la cancela **exacta**.
Medido: `delta_theta == 0.0` en todo `L`, así que el cruce caía en el primer punto de la
grilla (`0.02`) y `assert cruce < 0.3125` pasaba sin haber medido error angular alguno.

| Test | Qué discrimina |
|---|---|
| `test_g_el_adversario_alineado_con_el_patron_mide_cero` | mantiene retirada la evidencia vacua: reintroducirla rompe la aserción |
| `test_g_el_peor_caso_coherente_cruza_en_un_medio_y_supera_el_umbral` | el cruce del peor caso **estructural** cae en `L = 1/2`, `1.6x` el umbral |

El peor caso real es la perturbación **coherente perpendicular** (la misma `(q/2)` en todos
los píxeles, en un eje sin componente en el vector interpolado): los pesos positivos del
kernel la **suman** en vez de promediarla. Medido sobre la implementación real y sobre el
oráculo independiente en `k ∈ {2, 4, 8, 16}`: `delta_theta(L) = atan((q/2)/L)`, cruce exacto
en `L = 1/2`. La banda `[5/16, 1/2)` —declarada EVALUABLE— se publica con error de dirección
de hasta `1.60x q_ang`.

Esto **no** cambia `DEGENERATE_THRESHOLD` ni ninguna fila congelada: es la contraparte en
cruce de `R7_WORST_CASE_BOUND = 2.771x`, registrada bajo su propio token
(`R7_WORST_CASE_CROSSING`). La decisión —conservar la heurística RMS y aceptar el riesgo, o
subir el umbral a `1/2`— es del Tech Lead; la matriz está en
`h4-resize-normal-contract-reconciliation.md`.

### 3.2 Retiración de una afirmación de producción (misma clase)

El comentario de producción afirmaba que el cruce cae bajo `5/16` "tanto bajo el criterio
RMS como bajo el peor caso dirigido". La segunda mitad era la misma evidencia vacua; se
retira. La secuencia de cruces RMS declarada también se corrige a los valores medidos con la
malla fina (`0.0925 / 0.0683 / 0.0442 / 0.0200`), que son cotas superiores del cruce real.

## 4. Lo que este slice NO hace

- No ejecuta el corpus real ni inicia M6.
- No modifica Golden Master #718, EXP-M6 #675, `docs/validation/` ni artefactos históricos.
- No recalibra ningún umbral con corpus ni con resultados históricos.
- No fusiona el PR #723: queda en Draft.
