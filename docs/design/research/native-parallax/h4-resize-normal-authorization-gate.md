# H4 `resize_normal` — Cierre de autorización del Slice 2

> **Slice:** `H4_RESIZE_NORMAL_AUTHORIZATION_CLOSURE` · **Carácter:** `AUTHORIZATION_GATE_ONLY`
> (docs-only + tests de invariantes documentales).
>
> **No implementa el fix. No crea tests de comportamiento del resizer. No ejecuta M6. No accede al
> corpus real. No ajusta thresholds. No modifica `authored_dataset.py`, los runners de
> M2/M3/M4/M5, el PR #675, los artefactos históricos ni la evidencia de #700.**
>
> **Write-set:** este documento + `tests/test_native_parallax_h4_authorization_gate.py`.
>
> **Precede:** `docs/design/research/native-parallax/h4-resize-normal-preflight.md` (fusionado en
> PR #711). Este documento **no** lo duplica: cierra los requisitos R1/R5/R6 que quedaron
> `DESIGN_PROPOSED`, aplica el hallazgo de integración del dictamen de #711 y emite el veredicto de
> autorización. Ante contradicción entre ambos, **manda el preflight** para el contrato científico y
> **este documento** para el estado de autorización.

---

## 0. Leyenda de estados

Se conserva la taxonomía del preflight (§0): `CONFIRMED` · `CONTRACT_CLOSED` · `DESIGN_PROPOSED` ·
`NOT_MEASURED` · `DEFERRED` · `BLOCKED`. Se agrega:

| Estado | Significado |
|---|---|
| `DESIGN_CLOSED` | Contrato **especificado sin vacíos**, verificado contra el código vigente en este slice, pero **no implementado**. Distinto de `CONTRACT_CLOSED` (decisión de Tech Lead) y de `CONFIRMED` (verificado por ejecución de la corrección). |

Prohibido declarar `CONFIRMED` un test que no existe, o presentar `DESIGN_CLOSED` como si la
implementación ya estuviera medida.

---

## 1. Snapshot y verificación de entorno

### 1.1 Git

| Campo | Valor medido |
|---|---|
| `REPO` | `E:/Skyclaw_Main_Sync` |
| `WORKTREE` | `E:/Skyclaw_Main_Sync/.worktrees/h4-resize-normal-gate` (nuevo) |
| `LOCAL_BRANCH` | `h4-resize-normal-gate` (**plana** — bug de refs anidadas de esta máquina) |
| `REMOTE_BRANCH` | `research/native-parallax-h4-resize-normal-authorization-gate` |
| `BASE` | `origin/main` |
| `ORIGIN_MAIN_AT_START` | `beaf96252378c08f6ee1a42bad3269990da921a9` |
| `HEAD_AL_CREAR` | `beaf96252378c08f6ee1a42bad3269990da921a9` (igual a `origin/main`) |
| `WORKTREE_CLEAN` | `NO` — sólo `_TEMPORALES/` untracked preexistente en el worktree principal; **no** contamina este worktree |
| `CONCURRENT_WRITER` | `NO` en la ruta propuesta (no existía) |

**Corrección al brief.** El brief declara `Referencia histórica de main =
07df18c5f642589c459867d20185a5370881aead` y advierte "no asumir que sigue siendo HEAD". Medido:
`07df18c5` **sí está** en `main` pero ya **no es HEAD**; `origin/main` avanzó hasta `beaf9625`
(merge de PR #713, `fix/db-open-sentinel-invariant`). El worktree se creó desde el `main`
**vigente** (`beaf9625`), no desde la referencia histórica. Igual que el preflight, la relación de
ancestros se verificó antes de operar.

```text
BRIEF_REF_SHA_IS_ANCESTOR_OF_MAIN = YES
BRIEF_REF_SHA_IS_CURRENT_MAIN     = NO (avanzó por #713)
WORKTREE_BASED_ON                 = beaf9625 (main vigente)
```

### 1.2 Entorno de ejecución

| Componente | Valor medido |
|---|---|
| Python | `3.11.9` |
| NumPy | `2.4.6` |
| Pillow | `11.3.0` |

Coincide con el entorno declarado por el preflight (§20). El pin vigente admite
`numpy>=1.26,<2.5`, `pillow>=10,<12`.

### 1.3 Identidad del módulo bajo análisis

Verificado en este slice con `git hash-object` y `sha256` sobre el archivo del worktree recién
creado. **Coincide exactamente** con §4.4 del preflight:

```text
MODULE_PATH            = sky_claw/local/native_parallax/research/authored_dataset.py
GIT_BLOB_SHA           = dff54d132bcaa111a13a59d838d28c7e2bfbde03   (coincide)
SHA256_RAW             = 4184cbada6885cf62ef98a95100a7b1921a881b8e2f7450f2889e9337befde8c   (coincide)
SHA256_LF              = 4184cbada6885cf62ef98a95100a7b1921a881b8e2f7450f2889e9337befde8c   (coincide)
HAS_CRLF               = NO   (coincide)
RESIZE_NORMAL_DEF_LINE = 237  (coincide)
RESIZE_NORMAL_CALL_LINE= 323  (coincide)
```

`MODULE_IDENTITY_MATCHES_PREFLIGHT = YES`. La implementación vigente **no** cambió entre el
preflight y este slice: el defecto descrito sigue siendo el vigente.

### 1.4 CI de `main`

Al medir, `origin/main` = `beaf9625` tenía dos jobs en ejecución:

```text
🧪 Tests / windows-latest / py3.11 | in_progress  (no veredicto)
🧪 Tests / windows-latest / py3.12 | in_progress  (no veredicto)
```

**No se declara `CI_GREEN_MAIN`.** Es exactamente la trampa que documenta el propio preflight
(Apéndice B): un check puede reportar verde sin haber corrido la suite. Como los dos jobs
autoritativos de Windows seguían corriendo, este slice **no** afirma verde global. No es un
bloqueante para este trabajo: este slice no toca producción y no depende de esa corrida. Se
registra como límite de medición, no como hallazgo.

---

## 2. Fase 0 — Protección del entorno (ejecutada)

| Paso | Acción | Resultado |
|---|---|---|
| 1 | Leer `AGENTS.md`, `CLEAN_ROOM.md`, `sky_claw/local/native_parallax/AGENTS.md` | Hecho |
| 2 | `git worktree list --porcelain` | 28 worktrees; ninguno en la ruta propuesta |
| 3 | Verificar la ruta `E:/Skyclaw_Main_Sync/.worktrees/h4-resize-normal-gate` | **No existía** — sin trabajo ajeno |
| 4 | `git fetch origin --prune` | Hecho; sin cambios en `origin/main` |
| 5 | Crear worktree desde `origin/main` | Rama plana `h4-resize-normal-gate`; ref verificada (`rev-parse` + `log -1`) |
| 6 | Registrar SHA exacto | `beaf9625` (arriba) |
| 7 | CI de `main` | Dos jobs Windows `in_progress` (arriba) |

**No se usó** `reset --hard`, `git clean`, force-push ni ningún comando destructivo. El worktree
principal no fue tocado en ningún momento.

**Verificación de la ref (bug de esta máquina).** Tras `git worktree add -b h4-resize-normal-gate`,
`git rev-parse --abbrev-ref HEAD` devolvió `h4-resize-normal-gate` y `git log --oneline -1`
devolvió `beaf9625 Merge pull request #713…`. La ref se escribió correctamente (nombre plano).

---

## 3. R1 — Contrato numérico (cierre de `DESIGN_PROPOSED` → `DESIGN_CLOSED`)

R1 exige cerrar **forma, tipos admitidos, rango por canal, finitud y norma, camino identidad,
resoluciones admitidas, comportamiento ante dimensiones inválidas, tolerancias justificadas y
manejo de entradas degeneradas**, sin inventar semántica científica.

El preflight ya cerró varios de estos puntos (§7.1 entrada, §7.2 salida, §7.3 V-01…V-05, §5.3
D-06). Lo que quedaba `DESIGN_PROPOSED` es la **consolidación del contrato como una unidad
verificable**, y tres vacíos que el dictamen de #711 marca explícitamente: "cerrar explícitamente
validación de forma, rango, norma, tamaño y tolerancias, sin añadir semánticas de dominio no
justificadas".

### 3.1 Contrato consolidado de entrada

| Atributo | Contrato cerrado | Fundamento | Estado |
|---|---|---|---|
| Tipo | `NDArray[np.float64]` | Firma vigente | `CONFIRMED` |
| Forma | `(H, W, 3)` | Uso en `load_asset`; `decode_normal_image` produce RGB | `CONFIRMED` |
| Rango por canal | `[-1, 1]` | `decode_normal_image`: `rgb/255*2 − 1`; el corpus no excede | `CONFIRMED` |
| Normalización previa | Unitaria (ya renormalizada) | `decode_normal_image` | `CONFIRMED` |
| Convención | Aplicada por `apply_convention` **antes** del resize | `load_asset:317-318` | `CONFIRMED` |
| No finitos (NaN/Inf) | **Rechazo fail-closed** | V-04 `CONTRACT_CLOSED` | `CONTRACT_CLOSED` |
| Dimensiones admitidas | Cuadradas en la práctica; el resizer **no** valida | V-03 `DEFERRED`; corpus M3 100 % cuadrado | `DEFERRED` |
| `size` destino | Entero positivo; `PRIMARY_RESOLUTION = 512` | §27 M6 | `CONFIRMED` |

### 3.2 Contrato consolidado de salida

| Atributo | Contrato cerrado | Estado |
|---|---|---|
| Tipo | `float64` | `CONFIRMED` |
| Forma | `(size, size, 3)` | `CONFIRMED` |
| Rango por canal | `[-1, 1]` (tras clipping de entrada; la renormalización no lo excede) | `CONTRACT_CLOSED` |
| Norma | `≈ 1` **o** estado explícito `NON_EVALUABLE` — nunca un valor intermedio silencioso | `CONTRACT_CLOSED` (I-03, D-06) |
| Finitud | Sin NaN/Inf; entrada no finita ⇒ rechazo | `CONTRACT_CLOSED` (V-04, I-07) |
| Convención | Idéntica a la de entrada (no invierte Y, no permuta canales) | `CONTRACT_CLOSED` (I-04/I-05/I-06) |

### 3.3 Vacíos: cierre explícito

| ID | Vacío | Decisión cerrada | Estado |
|---|---|---|---|
| V-01 | Camino identidad | Preservar **no-op válido** (sin interpolar, sin renormalizar, sin copia obligatoria) + **validación explícita** de entradas inválidas | `CONTRACT_CLOSED` (D-07) |
| V-02 | Política de degenerados | **FAIL_CLOSED** con umbral derivado de teoría + sintético (§5.3) | `CONTRACT_CLOSED` (D-06) |
| V-03 | Dimensiones no cuadradas | Fuera del alcance H4; `load_asset` cuadra a 512 antes del solver | `DEFERRED` |
| V-04 | No finitos | Rechazo fail-closed, coherente con `decode_height_image` modo F | `CONTRACT_CLOSED` |
| V-05 | Estabilidad entre versiones | `NOT_MEASURED`; la versión se registra en la procedencia (§10) | `NOT_MEASURED` (registrado, no presentado como probado) |

### 3.4 Regla de comportamiento ante entrada inválida (cierre)

El contrato exige que **toda** entrada inválida tenga comportamiento definido y explícito. Se
cierra así:

```text
entrada con NaN/Inf            -> reject (fail-closed, DatasetInvalidError-like)
entrada con forma != (H,W,3)   -> DEFERRED (V-03): el resizer no valida hoy;
                                  el slice de código NO debe inventar una semántica
                                  de dominio no justificada. Se documenta el límite.
entrada válida en camino identidad -> no-op (V-01)
entrada válida con resize      -> interpolar + renormalizar (o NON_EVALUABLE si degenerado)
```

**No se inventa** un contrato de forma no cuadrada: el preflight lo declara `DEFERRED` y este
cierre **preserva** ese límite en vez de fabricar una regla de dominio.

### 3.5 Tolerancias — principio de justificación (cierre metodológico)

El preflight exige tolerancias "derivadas de sintético, no ajustadas al corpus". Este cierre fija
**el criterio**, no los números (los números son criterio de aceptación del Slice 2, §17.8):

| Tolerancia | Fuente admisible | Fuente **prohibida** |
|---|---|---|
| Norma ≈ 1 en entrada suave | Ida/vuelta `float32 → float64` (cota analítica) | Medición sobre el corpus M3 |
| Constancia de campo constante | Error de redondeo `float32` (≈ `1e-7` relativo) | Ajuste hasta que pase un asset real |
| Error del kernel bilineal | Comparación contra oráculo **independiente** (§8 Familia C) | Comparación contra otra llamada a Pillow |
| Tolerancia del camino identidad | Pruebas sintéticas (V-01) | Cualquier medición del corpus |

```text
R1_NUMERIC_CONTRACT = DESIGN_CLOSED
TOLERANCE_SOURCE    = SYNTHETIC_AND_ANALYTICAL (prohibido ajustar al corpus)
V03_HANDLING        = DEFERRED_PRESERVED (no se inventa semántica no cuadrada)
```

### 3.6 Mensaje de cierre de R1

R1 pasa de `DESIGN_PROPOSED` a `DESIGN_CLOSED`: el contrato de entrada y salida está
especificado como unidad, los cinco vacíos tienen estado explícito, y el criterio de tolerancias
está fijado por fuente (sintético/analítico) sin números ajustados al corpus. **No** se declara
`CONFIRMED`: eso requiere la implementación, que no existe.

---

## 4. R5 — Procedencia (cierre de `DESIGN_PROPOSED` → `DESIGN_CLOSED`)

R5 exige diseñar una **asociación verificable** entre etiqueta, hash del módulo ejecutado,
entrypoint real, SHA Git, versiones de NumPy/Pillow y política de degenerados, y **distinguir qué
exige H4 Slice 2 de qué corresponde al runner futuro de M6**. La clave: *una etiqueta declarada
por el operador no es prueba de la implementación ejecutada* (§10.3 del preflight).

### 4.1 La asociación verificable (contrato cerrado)

La etiqueta `resize_normal_impl` **no** es un campo de texto libre. Se define una **tupla de
identidad** que el runner debe **recalcular** desde el artefacto ejecutado y cotejar contra un
registro versionado:

```text
IDENTITY_TUPLE = (
  label            : "HISTORICAL_UINT8" | "CANONICAL_FLOAT",
  module_sha256_lf : sha256 canónico LF del módulo realmente importado,
  entrypoint       : "sky_claw.local.native_parallax.research.authored_dataset:resize_normal",
  code_git_sha     : HEAD del checkout de la corrida,
  intermediate_dtype: "uint8" | "float32",
  degenerate_policy : "NONE" | "FAIL_CLOSED",
)
```

Regla de verificación (fail-closed, del preflight §10.3, **preservada**):

```text
Si hash(módulo ejecutado) no coincide con la tupla registrada para la etiqueta
  -> STOP_M6_EXECUTION (rechazar hash desconocido / etiqueta incompatible / selección no verificable)
```

**Distinción de hashes (obligatoria, §10.2):** `module_sha256_lf` (**código**) ≠ `input_identity`
(**datos**) ≠ `manifest_sha256_lf` (**corpus**). Una ruta local **no** es identidad.

### 4.2 Qué exige H4 Slice 2 vs qué es del runner M6

Esta es la separación que el dictamen pidió explícitamente:

| Obligación | ¿H4 Slice 2? | ¿Runner M6 (Slice 3)? | Estado |
|---|---|---|---|
| Definir la **tupla de identidad** y el algoritmo de cotejo | **Sí** — es contrato | Consume | `DESIGN_CLOSED` |
| Exponer el `module_sha256_lf` / `module_blob_sha` del módulo corregido | **Sí** — tras implementar | Consume | `BLOCKED` (el código corregido no existe) |
| Registrar `degenerate_policy` y `degenerate_threshold` en la salida | **Sí** — intrínseco a D-06 | Consume | `BLOCKED` (D-07b) |
| Persistir el **roster por asset** y `degenerate_normal_fraction` por brazo | **No** — integración de runners | **Sí** (§11.5) | `DEFERRED` a Slice 3 |
| Emitir el JSON de procedencia completo y ejecutar el gate | **No** — `PROVENANCE_GATE_IMPLEMENTED = NO` | **Sí** (§10.3) | `DEFERRED` |
| Cotejar `resize_normal_impl` contra el registro antes de calcular métricas | **No** | **Sí** — el gate vive donde hoy se exige `git_sha` | `DEFERRED` |

**Regla de lectura.** H4 Slice 2 **no** implementa M6 ni su gate. Lo que H4 Slice 2 debe dejar
listo es que la implementación corregida **pueda** identificarse sin ambigüedad (hash estable,
`degenerate_policy` explícita, `intermediate_dtype` correcto). El gate que **rechaza** es del
runner de M6, y hoy no existe.

### 4.3 El problema del hash histórico

El hash histórico documentado (`dff54d13…` / `4184cbad…`) **no** autoriza etiquetar la
implementación futura como `CANONICAL_FLOAT`: ese hash corresponde a `HISTORICAL_UINT8`. La
implementación corregida tendrá un hash nuevo, que deberá registrarse **antes** de cualquier
consumo científico. El preflight ya lo declara: *"La implementación corregida aún carece de hash
registrado y por ello NO puede declararse aceptada."*

```text
PROVENANCE_SCHEME                 = SPECIFIED (design closed)
IDENTITY_TUPLE_DEFINED            = YES
GATE_ALGORITHM_SPECIFIED          = YES
PROVENANCE_GATE_IMPLEMENTED       = NO  (DEFERRED a Slice 3)
CORRECTED_IMPL_HASH_REGISTERED    = NO  (BLOCKED: el código corregido no existe)
CANONICAL_FLOAT_LABEL_AUTHORIZED  = NO  (prohibido etiquetarlo sin hash corregido)
R5_PROVENANCE_CONTRACT            = DESIGN_CLOSED
```

### 4.4 Mensaje de cierre de R5

R5 pasa a `DESIGN_CLOSED`: la asociación es una tupla recalculable, no una afirmación del
operador; el algoritmo del gate es fail-closed y está especificado; y la separación
H4-Slice-2 / runner-M6 está explícita. Lo que **no** se cierra —y no puede cerrarse documentalmente
— es el hash del código corregido, que sólo existirá tras implementarlo.

---

## 5. R6 — Cobertura completa de consumidores (cierre de `DESIGN_PROPOSED` → `DESIGN_CLOSED`)

R6 exige enumerar **todos** los consumidores mediante AST (no cuatro nombres escritos a mano), de
modo que la enumeración detecte **futuros consumidores nuevos**, y diseñar la **señal de error
diferenciada** para el caso crítico de M3.

### 5.1 Enumeración por AST (ejecutada en este slice)

Se inspeccionaron con AST `run_exp_m2.py`, `run_exp_m3.py`, `run_exp_m4.py`, `run_exp_m5.py`,
`authored_dataset.py`, `fetch_exp_m2_corpus.py`.

**Call-sites de `load_asset` (el único invocador de `resize_normal`):**

| Archivo | Call-sites | Convención |
|---|---|---|
| `run_exp_m2.py` | 2 (L242, L327) | sin convención forzada |
| `run_exp_m3.py` | 2 (L177, L339) | L177 sin; L339 con `SOLVER_NORMAL_CONVENTION` |
| `run_exp_m4.py` | 2 (L150, L193) | ambas con `SOLVER_NORMAL_CONVENTION` |
| `run_exp_m5.py` | 1 (L304) | con `SOLVER_NORMAL_CONVENTION` |
| `authored_dataset.py` | 1 (L323) — `resize_normal` invocado **sólo acá** | — |

**Capturas de `DatasetInvalidError` (por AST, enumerando la familia completa):**

| Archivo | Línea | Handler | `continue` | Efecto |
|---|---|---|---|---|
| `run_exp_m3.py` | 340 | `except DatasetInvalidError` | **Sí** | registra en `dataset_invalid.append(...)` y **excluye** el asset |
| `fetch_exp_m2_corpus.py` | 136, 141, 146 | `except DatasetInvalidError` | no | descarta el espejo faltante (**no** ejerce el resizer: es fetch de corpus) |

**M2, M4 y M5**: `grep`/AST confirman **cero** capturas de `DatasetInvalidError` alrededor de sus
`load_asset` → el error **propaga hacia arriba** (fail-closed por defecto).

```text
LOAD_ASSET_CALL_SITES_PRODUCTION = 7 (M2×2, M3×2, M4×2, M5×1)
RESIZE_NORMAL_CALL_SITES         = 1 (authored_dataset.load_asset:323)
DATASET_INVALID_CAPTURE_SITES    = 4 (M3×1 con continue; fetch_m2×3 sin resizer)
M2_M4_M5_PROPAGATE_FAIL_CLOSED   = YES (cero capturas)
M3_CAPTURES_AND_CONTINUES        = YES  (L340)
```

### 5.2 El punto crítico (hallazgo de integración, `CONFIRMED`)

`run_exp_m3.py::evaluate_cohort_a` (L332-342) hace:

```python
try:
    mat = load_asset(spec, resolution, tested_convention=SOLVER_NORMAL_CONVENTION)
except DatasetInvalidError as exc:
    dataset_invalid.append({"asset_id": ..., "reason": str(exc)})
    continue
```

**El defecto.** Si un error de degeneración H4 heredara de `DatasetInvalidError`, M3 lo
absorbería con `continue`, lo registraría como una exclusión **de integridad** más, y —como
muestra `main()` (L769-778)— sólo re-verificaría `check_cohort_a_sufficient(remaining)`. Si
quedan assets suficientes, **publica la decisión científica igual**, sobre una cohorte alterada,
con la degeneración indistinguible de una exclusión preexistente ("ilegible", "hash cambió",
"resolución"). Eso es exactamente lo prohibido por el dictamen:

> *"Un error por degeneración no puede convertirse en `DATASET_INVALID` ordinario y excluir el
> asset: para Slice 2 se exige una señal diferenciada y propagación fail-closed antes de publicar
> un artefacto científico exitoso."*

El preflight lo dice en §5.3 (D-06.d/D-06.e): basta un píxel degenerado para marcar el brazo
`NON_EVALUABLE`; y *"un runner que publique resultados debe mantener la identidad del asset en el
roster con estado explícito… Si ese runner todavía no soporta tal salida, debe abortar antes de
publicar un resultado científico"*.

### 5.3 La señal de error diferenciada (contrato cerrado)

Diseño cerrado para el Slice 2 (implementación `BLOCKED`, contrato `DESIGN_CLOSED`):

```text
Jerarquía de excepciones (propuesta para el Slice 2 — NO implementada acá):

  DatasetInvalidError(Exception)                    # integridad de archivo/manifest (existente)
  └─ DegenerateNormalError(DatasetInvalidError)     # NUEVA: error de degeneración H4
       .asset_id, .degenerate_count, .total_pixels, .threshold
```

**Reglas de comportamiento (fail-closed):**

1. `resize_normal` **no** lanza por degeneración en su forma actual: no puede abortar un asset
   por una propiedad del *campo* sin que el consumidor decida. La señal se eleva como
   `DegenerateNormalError` desde una capa que conozca el asset (el punto de integración del
   Slice 2), con el conteo y el threshold.
2. `DegenerateNormalError` **hereda** de `DatasetInvalidError` (compatibilidad de `except` en
   consumidores que hoy no distinguen), **pero** se lanza **antes** de que M3 haga `continue`, y
   su handling debe **preceder** al `except DatasetInvalidError`.
3. En M3, el nuevo handler `except DegenerateNormalError` debe **abortar** (`raise` o
   `sys.exit`), **no** `continue` — porque M3 no tiene hoy un roster persistente con estado
   `NON_EVALUABLE`.
4. Mientras no exista roster persistente, **ningún** consumidor puede publicar un artefacto
   científico exitoso tras registrar un `DegenerateNormalError`. `continue` está **prohibido**
   para esta señal.
5. **Prohibido** cambiar exclusiones preexistentes (integridad) por motivos distintos de H4
   (dictamen: *"No cambiar exclusiones preexistentes por motivos distintos de H4"*).

**Test de propagación exigido (Slice 2, no existe aún):** un ancla que, por introspección/AST,
**enumere** todos los `except DatasetInvalidError` de los runners y **exija** que ninguno capture
`DegenerateNormalError` con `continue`. Un runner nuevo sin tratamiento rompe el ancla.

```text
DIFFERENTIATED_ERROR_SIGNAL      = DegenerateNormalError(DatasetInvalidError)  (DESIGN_CLOSED)
M3_MUST_NOT_SILENTLY_EXCLUDE     = YES (abortar mientras no haya roster persistente)
SILENT_EXCLUSION_PROHIBITED      = YES
EXISTING_EXCLUSIONS_UNCHANGED    = YES (no reclasificar integridad como H4)
R6_CONSUMER_COVERAGE             = DESIGN_CLOSED
```

### 5.4 El ancla de enumeración (Familia E, diseñada)

El preflight (§8 Familia E) pide un ancla que detecte una **nueva ruta de carga** que evite el
resizer declarado. Cierre del diseño:

- **Detectar por AST**, no por lista: recorrer los runners y sus imports; congelar el conjunto de
  módulos que llaman a `load_asset` **y** el conjunto que llama a `resize_normal`.
- Un primitivo nuevo (otra función que produzca `N_AUTH` sin pasar por `load_asset`) rompe el
  ancla hasta que se le declare procedencia.
- **No** limitarse a `load_asset`: enumerar también las rutas de runner (M2/M3/M4/M5) para que una
  ruta nueva **no** herede silenciosamente una excepción.

```text
FAMILY_E_ANCHOR_DESIGNED          = YES
ANCHOR_ENUMERATES_BY_AST          = YES
ANCHOR_COVERS_ALL_RUNNERS         = YES (M2/M3/M4/M5 + load_asset)
ANCHOR_IMPLEMENTED                = NO  (pertenece al Slice 2)
```

### 5.5 Mensaje de cierre de R6

R6 pasa a `DESIGN_CLOSED`: la enumeración se hizo por AST (no a mano), el punto crítico de M3
quedó `CONFIRMED` con la línea exacta, la señal diferenciada está especificada con reglas
fail-closed, y el ancla de cobertura está diseñada para **detectar** consumidores nuevos. Lo que
**no** se cierra es la implementación de la señal ni del ancla (Slice 2).

---

## 6. R7 — Threshold de degeneración (procedimiento, `DEFERRED` a Slice 2)

R7 exige documentar el **procedimiento reproducible** para derivar `DEGENERATE_THRESHOLD`
exclusivamente de teoría numérica y experimentos sintéticos, con prohibición de calibrarlo con
corpus real o resultados históricos. **El valor numérico no se fija acá** — es criterio de
aceptación del Slice 2 (§17.8).

### 6.1 Procedimiento cerrado (teoría + sintético)

1. **Cota teórica de cancelación.** Dado el kernel bilineal y una ventana de reducción de factor
   `k`, el peor caso de cancelación ocurre con contenido a Nyquist (`±1` alternado). La norma
   mínima alcanzable del vector interpolado define el orden de magnitud del umbral.
2. **Calibración con controles sintéticos.** Construir campos sintéticos cuyos vectores
   interpolados tengan norma conocida `L ∈ {0, ε₁, ε₂, …, 1}` y medir el error de dirección
   `Δθ(L)` respecto del oráculo independiente. El umbral es el `L` por debajo del cual la
   dirección deja de ser estimable con precisión útil **declarada**.
3. **Criterio de selección.** `DEGENERATE_THRESHOLD` = el menor `L` tal que, para todo
   `L' ≥ L` en la grilla sintética, `Δθ(L')` ≤ tolerancia declarada. El criterio y la tolerancia
   se fijan **antes** de correr.
4. **Falsación.** Probar que el umbral **no** es trivial: un campo sintético con `L` apenas por
   debajo del umbral debe quedar `NON_EVALUABLE`; uno apenas por encima debe ser evaluable.

```text
R7_PROCEDURE              = SPECIFIED (teoría + sintético)
R7_NUMERIC_VALUE          = DEFERRED a Slice 2
CORPUS_CALIBRATION        = PROHIBITED
HISTORICAL_TUNING         = PROHIBITED
THRESHOLD_FIXED_HERE      = NO
```

**Relación con M6.** `DEGENERATE_THRESHOLD` es un umbral **propio de H4**, distinto de los
thresholds de M6 (`G`, `NUMERICAL_ENERGY_FLOOR`, `T_R`, `T_N`, `T_AMBIG`), que siguen
`NOT_DERIVED` (`THRESHOLDS_FROZEN = NO`) y pertenecen a Slice 3. No se confunden.

---

## 7. Fase 2 — Matriz de autorización (los nueve requisitos de §17)

Estados: `YES` · `NO` · `PENDING`. La autorización requiere **todo** `YES`.

| # | Condición §17 | Estado | Evidencia | SHA | Test / verificación | Riesgos restantes | Dependencias | Responsable |
|---|---|---|---|---|---|---|---|---|
| 1 | Defecto H4 identificado en fuente y caso RED especificado (ejecución RED = aceptación de Slice 2) | **YES** | `authored_dataset.py:237-253`; sesgo medido `mean(nx) = −0.0039215083`; sub-LSB `1/(4·255)` invierte signo | `dff54d13` | Ancla documental (este slice) + probe de reproducción | Ninguno (la corrección aún no existe) | — | Tech Lead |
| 2 | Contrato numérico cerrado, incluidos V-01…V-05 | **YES** | §3 (este documento) + §7 preflight | `dff54d13` | `test_..._authorization_gate.py` | V-03 `DEFERRED`, V-05 `NOT_MEASURED` (registrados) | R1 | Tech Lead |
| 3 | Política de degenerados elegida y declarada | **YES** | §5.3 preflight (D-06 `FAIL_CLOSED`) | — | Ancla documental | Umbral sin valor (R7) | D-07b | Tech Lead |
| 4 | Plan de mutantes con test detector por mutante | **YES** | §9 preflight (M-01…M-10) | — | Ancla documental | M-10 debe romper algo (nota §9) | — | Tech Lead |
| 5 | Ancla de enumeración de primitivas **diseñada** | **YES** | §5.4 (este documento) + §8 Familia E | — | Ancla documental + `ANCHOR_IMPLEMENTED=NO` | Implementación pendiente | R6 | Tech Lead |
| 6 | Contrato de procedencia especificado con gate fail-closed | **YES** | §4 (este documento) + §10 preflight | `dff54d13` | `test_..._authorization_gate.py` | `PROVENANCE_GATE_IMPLEMENTED=NO` (Slice 3) | R5 | Tech Lead |
| 7 | Plan de no-regresión de `resize_height` (test ejecutado = aceptación de Slice 2) | **YES** | §4.2 preflight; `resize_height` modo F intacto | `dff54d13` | Ancla documental + suite existente (`18 passed`) | Ninguno | — | Tech Lead |
| 8 | Método de derivación de `DEGENERATE_THRESHOLD` aprobado (valor = aceptación de Slice 2) | **YES** | §6 (este documento) | — | Ancla documental + `THRESHOLD_FIXED_HERE=NO` | Valor numérico pendiente | D-07b | Tech Lead |
| 9 | **Conformidad del Tech Lead con el alcance del Slice 2** | **PENDING** | Dictamen #711: `TECH_LEAD_SCOPE_APPROVAL=NOT_RECORDED` | — | **Ninguno: es una aprobación explícita, no derivable de tests ni de CI** | Sin esta firma, Slice 2 no arranca | Decisión humana | Tech Lead |

### 7.1 Separación estricta de los cuatro grupos

**(1) Requisitos para COMENZAR la implementación** — deben estar cerrados *antes* de escribir
código de producción:

| Requisito | Estado |
|---|---|
| Contrato numérico (R1) | `DESIGN_CLOSED` |
| Política D-06 fail-closed | `CONTRACT_CLOSED` |
| Contrato del camino identidad (D-07) | `CONTRACT_CLOSED` |
| Validación de no finitos (V-04) | `CONTRACT_CLOSED` |
| Procedencia (R5) | `DESIGN_CLOSED` |
| Ancla de cobertura **diseñada** (R6) | `DESIGN_CLOSED` |
| Método de derivación de `DEGENERATE_THRESHOLD` (R7) | `DESIGN_CLOSED` (valor diferido) |
| **Conformidad del Tech Lead (condición 9)** | **PENDING** |

**(2) Tests `RED→GREEN` que se ejecutan DURANTE la implementación** — Familias A–F (§8 preflight).
No existen aún. Su ejecución es **criterio de aceptación** del Slice 2, **no** gate previo.

**(3) Requisitos de ACEPTACIÓN antes del merge del código:**

- RED exhibido contra el histórico (Familia A) — obligatorio.
- GREEN de las Familias B–E.
- `DEGENERATE_THRESHOLD` derivado por el procedimiento de §6 (valor numérico).
- No-regresión de `resize_height` (Familia D).
- Mutantes M-01…M-10 con sus detectores.
- Propagación fail-closed verificada en M2/M3/M4/M5 (§5.3 de este documento).
- Hash del código corregido registrado para la procedencia.

**(4) Requisitos EXCLUSIVOS de EXP-M6:**

- Runner M6 (ETAPA B+), matemática pura, `PAIR_EXCESS_ENERGY`/`RECOVERY_FRACTION` instrumentados.
- Thresholds `G`, `NUMERICAL_ENERGY_FLOOR`, `T_R`, `T_N`, `T_AMBIG` derivados.
- JSON de procedencia completo con el gate ejecutándose.
- Roster persistente por asset con `NON_EVALUABLE`.
- Protocolo de contrafactual ejecutado.

### 7.2 Decisión de autorización

```text
CONDITIONS_YES     = 8 / 9
CONDITION_9        = PENDING (conformidad explícita del Tech Lead)
AUTO_DERIVED_APPROVAL = PROHIBITED (no se deriva de tests ni de CI verde)
```

**Como queda un requisito previo sin cerrar (condición 9), la decisión es `BLOCKED`.** No se
inventa una aprobación.

---

## 8. Veredictos

```text
H4_R1_NUMERIC_CONTRACT        = PASS
H4_R5_PROVENANCE_CONTRACT     = PASS
H4_R6_CONSUMER_COVERAGE       = PASS
H4_D06                        = CONTRACT_CLOSED
H4_D07                        = CONTRACT_CLOSED
H4_TECH_LEAD_APPROVAL         = PENDING

H4_CODE_IMPLEMENTATION_AUTHORIZED = NO
M6_IMPLEMENTATION_BLOCKED         = YES
REAL_CORPUS_RERUN                 = NO
```

**Los tres contratos (R1/R5/R6) se cierran como `PASS` documental.** `H4_TECH_LEAD_APPROVAL`
permanece `PENDING`: la conformidad es una firma humana explícita que este slice **no** puede
auto-otorgar. Por eso `H4_CODE_IMPLEMENTATION_AUTHORIZED = NO`. Sólo una aprobación expresa
posterior sobre la condición 9 de §17 puede cambiar ese estado.

> **Nota de lectura (advertencia del propio preflight, preservada).** Ningún CI verde de este PR
> valida una implementación H4: el fix no existe. Lo que este PR puede validar es que el
> documento es consistente, que sus tokens de veredicto son los declarados y que no se alteró
> ningún artefacto científico.

---

## 9. Hallazgos nuevos y contradicciones

| ID | Hallazgo | Tipo | Evidencia |
|---|---|---|---|
| N-01 | El brief declara `main` en `07df18c5`; `origin/main` vigente es `beaf9625` (avanzó por #713). | Corrección de snapshot | §1.1 |
| N-02 | El preflight lista #697 como `OPEN/DRAFT`; hoy #697 está **`MERGED`** (head `56a27acf`). No afecta el alcance H4, pero invalida la foto de §20. | Deriva de estado | `gh pr view 697` |
| N-03 | Los dos jobs Windows de CI de `main` seguían `in_progress`; **no** se declara `CI_GREEN_MAIN`. | Límite de medición | §1.4 |
| N-04 | La reproducción independiente de §3.5 dio `3968/4096` píxeles nulos (float), no `4092/4096`. El **clase** de hallazgo se confirma (histórico `0/4096`, float concentra nulos y 96.9 % no unitaria); el número exacto depende del kernel de Pillow. | Diferencia de instrumento | §10.1 |
| N-05 | `fetch_exp_m2_corpus.py` también captura `DatasetInvalidError` (3 sitios) pero **no** ejerce el resizer: no es un consumidor de degeneración H4. Se documenta para que el ancla no lo trate como hermano. | Precisión de censo | §5.1 |
| N-06 | M3 registra la exclusión con razón y re-verifica suficiencia, **pero** si quedan assets suficientes publica la decisión con la degeneración indistinguible de una exclusión de integridad. | **Hallazgo de integración** | §5.2 |

**Contradicciones resueltas sin inventar semántica:**

- El brief pedía cerrar "resoluciones admitidas" y "comportamiento ante dimensiones inválidas".
  El preflight declara V-03 `DEFERRED` (corpus cuadrado). **Resolución:** se cierra **el límite**
  (no se valida forma no cuadrada) y se prohíbe inventar una regla de dominio. §3.3.
- El brief pedía "finitud y norma" como parte de R1. El preflight las cerró en V-04/D-06.
  **Resolución:** se consolidan en §3.2 sin reabrir las decisiones de Tech Lead.

---

## 10. Tests y verificaciones ejecutados

### 10.1 Reproducción empírica (sin corpus, sin red)

Ejecutada contra la implementación **vigente** del worktree (no modificada):

| Verificación | Resultado medido | Predicción del preflight | Veredicto |
|---|---|---|---|
| Sesgo normal plana `(0,0,1)`, 16→8 | `mean(nx) = mean(ny) = −0.0039215083` | `−0.0039215` | Coincide |
| Caso RED `nx = 1/(4·255)` | `nx_out ≈ −3.95e-3` (signo **invertido**) | ROJO esperado | Coincide |
| Control `nx = 1/255` | `nx_out ≈ +3.95e-3` (signo preservado) | no discrimina | Coincide |
| Camino identidad `8→8`, norma 1.0198 | `norma_out = 1.0198`, mismo objeto | no-op sin renormalizar | Coincide |
| Cancelación §3.5 (float, 128→64) | `minL=0.000000`, `px_L_cero = 3968/4096` | `4092/4096` | Clase confirmada; número difiere (N-04) |
| Cancelación §3.5 (uint8, 128→64) | `minL=1.000000`, `px_L_cero = 0/4096` | `0/4096` | Coincide |
| Caso límite `8×2→8×1` (uint8) | `(0.57735, −0.57735, −0.57735)`, norma 1.0 | norma 1.0 | Coincide |
| Caso límite `8×2→8×1` (float) | `(0,0,0)`, norma 0.0 | norma 0.0 | Coincide |

### 10.2 Suite existente (ancla vigente)

```text
tests/test_native_parallax_exp_m2.py ........................ 18 passed
  (incluye test_m3_normal_resize_renormalizes — el ancla que el fix debe preservar)
tests/test_native_parallax_pr700_corrective_docs_invariants.py . 8 passed
  (checker de tablas Markdown del repo — autoritativo para este documento)
```

### 10.3 Gate de documentación de este PR

Valores **medidos** antes de publicar.

```text
WRITE_SET        = PASS  (2 paths: el documento + tests/test_native_parallax_h4_authorization_gate.py)
DIFF_CHECK       = PASS  (sin whitespace sobrante ni marcadores; `git status --short` = sólo ?? de los 2 paths)
MARKDOWN_TABLES  = PASS  (verificado con las funciones del checker del repo IMPORTADAS, no reimplementadas)
MARKDOWN_BARRAS  = PASS  (0 celdas con "\|")
TEST_NUEVO       = PASS  (17 passed — tests/test_native_parallax_h4_authorization_gate.py)
TEST_VIGENTE     = PASS  (18 passed — test_native_parallax_exp_m2.py, incluye el ancla del resizer)
CHECKER_REPO     = PASS  (8 passed — test_native_parallax_pr700_corrective_docs_invariants.py)
SUITE_FOCAL_NP   = PASS  (63 passed — m2 + m2_json + pr700 docs + pr697 reconciliation + gate)
RUFF_CHECK       = PASS  (All checks passed!)
RUFF_FORMAT      = PASS  (858 files already formatted)
MYPY             = PASS  (no issues found in 364 source files)
MUTANTES_ANCLA   = 8/8 killed  (auth=YES, ready=YES, cierre=APPROVED, produccion=YES,
                                corpus=YES, fila rota, barra escapada, R1=BLOCKED)
ARTEFACTOS_CIENTIFICOS = PASS  (0 archivos científicos modificados)
PRODUCCION       = PASS  (0 archivos de sky_claw/ modificados)
```

**Nota sobre mutantes del ancla.** El driver muta el documento preservando CRLF y restaura
desde backup en `try/finally`; el documento quedó **byte-idéntico** al original (hash verificado)
tras la corrida. En la primera pasada surgió un **hermano no cubierto**: `TECH_LEAD_SCOPE_APPROVAL`
(§13) repite el predicado de `H4_TECH_LEAD_APPROVAL` (§8) en otra superficie y el mutante
`APPROVED` **sobrevivía**. Se cerró el hermano: el ancla ahora enumera las dos superficies y
filtra la cita histórica entre backticks (`=NOT_RECORDED` del dictamen de #711), que es una
cita, no una declaración. Lección: el ancla debe enumerar las superficies que **declaran**, no
muestrear una.


---

## 11. Fase 3 — Publicación

- **PR:** documental nuevo, sobre `research/native-parallax-h4-resize-normal-authorization-gate`.
- **Rama local:** `h4-resize-normal-gate` (plana).
- **No se fusiona automáticamente.**
- **No se modifica:** `authored_dataset.py`, runners M2/M3/M4/M5, PR #675, artefactos históricos,
  thresholds científicos, corpus real.
- **Alcance exacto del futuro PR de implementación H4:** §13.

**Verificación de write-set:** el diff toca **sólo** los dos paths declarados. Cero archivos
científicos. Cero producción.

---

## 12. Alcance exacto recomendado para el PR de implementación H4

Del preflight §19 P10, **preservado sin cambios** (no se re-inventa):

```text
SESSION        = H4_RESIZE_NORMAL_FIX_IMPLEMENTATION
WORKTREE       = <repo>/.worktrees/h4-resize-normal-fix   (nuevo, desde el main VIGENTE)
LOCAL_BRANCH   = h4-resize-normal-fix                     (PLANA)
REMOTE_BRANCH  = research/native-parallax-h4-resize-normal-fix
WRITE-SET      = sky_claw/local/native_parallax/research/authored_dataset.py
                 tests/test_native_parallax_h4_resize_normal.py            (nuevo)
                 <registro de procedencia estrictamente necesario>
                 <tratamiento fail-closed de DegenerateNormalError en los consumidores>
PROHIBIDO      = thresholds, corpus, artefactos históricos, docs de #697/#700, rama de #675
BASE           = un `main` NUEVO (no la rama de #675 ni la de este cierre)
```

**Adiciones de este cierre al write-set del Slice 2** (consecuencia de §5.2/R6):

- La señal diferenciada `DegenerateNormalError` y su handling fail-closed en **cada** consumidor
  enumerado por el ancla (M2/M3/M4/M5), con `continue` prohibido para esa señal.
- El ancla de cobertura (Familia E) implementada por AST.

**Precondición dura para abrir ese PR:** la condición 9 de §17 (`H4_TECH_LEAD_APPROVAL`)
resuelta a `YES` de forma explícita. Sin eso, el PR de implementación **no** debe abrirse.

---

## 13. Cierre

```text
SLICE                          = H4_RESIZE_NORMAL_AUTHORIZATION_CLOSURE
CHARACTER                      = AUTHORIZATION_GATE_ONLY

H4_DEFECT_REPRODUCED           = YES (sesgo, sub-LSB, cancelación)
R1_NUMERIC_CONTRACT            = DESIGN_CLOSED
R5_PROVENANCE_CONTRACT         = DESIGN_CLOSED
R6_CONSUMER_COVERAGE           = DESIGN_CLOSED
R7_THRESHOLD_PROCEDURE         = DESIGN_CLOSED (valor DEFERRED a Slice 2)

D06_STATUS                     = CONTRACT_CLOSED_FAIL_CLOSED
D07_STATUS                     = CONTRACT_CLOSED_PRESERVE_VALID_IDENTITY_NOOP

CONDITIONS_YES                 = 8/9
TECH_LEAD_SCOPE_APPROVAL       = PENDING
H4_CODE_IMPLEMENTATION_AUTHORIZED = NO
M6_IMPLEMENTATION_BLOCKED      = YES
REAL_CORPUS_RERUN              = NO
READY_TO_IMPLEMENT             = NO  (prohibido declararlo sin la condición 9)

PRODUCTION_CHANGED             = NO
SCIENTIFIC_ARTIFACTS_CHANGED   = NO
PR675_CHANGED                  = NO
THRESHOLDS_TUNED               = NO
CORPUS_ACCESSED                = NO
```

**Estado narrativo.** Los tres contratos que el dictamen de #711 pidió cerrar (R1, R5, R6) están
cerrados como documentación verificable. No queda ningún requisito **técnico** previo abierto. Lo
único que falta para autorizar el Slice 2 es la **conformidad explícita del Tech Lead** con el
alcance (condición 9), que por definición no puede derivarse de tests ni de CI. Mientras esa
firma no exista, el estado es `BLOCKED` y `H4_CODE_IMPLEMENTATION_AUTHORIZED = NO`.
