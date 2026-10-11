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

### 1.5 Estado del PR y revisión adversarial

```text
PR                            = 717
URL                           = https://github.com/FacundoSu1986/Sky-Claw/pull/717
HEAD_DE_REFERENCIA_AL_ABRIR   = e7e28a98062705b0af7d6f96f504306bb39fb69a
ESTADO_INICIAL                = DRAFT / OPEN / MERGEABLE
ARCHIVOS_CAMBIADOS            = 2 (el documento + el test de invariantes documentales)
REVIEW_COMMENT_ID             = 6113684862
REVIEW_VERDICT                = CHANGES_REQUESTED_FOR_CONTRACT_COMPLETENESS
REVIEW_FINDINGS               = F1 (contrato numérico) · F2 (propagación de degenerados) — ambos MATERIAL
REVIEW_THREADS_ABIERTOS       = 0 (por estar en Draft, los bots no corrieron)
```

**Por qué 0 threads.** Mientras el PR está en `Draft`, los revisores automáticos (Qodo, CodeRabbit)
no se disparan; la revisión llegó por comentario humano explícito, no por hilo inline. Por eso la
adjudicación de F1/F2 se hace sobre el texto del comentario `6113684862` y se registra acá, no
como resolución de threads. Al pasar a `Ready`, los bots correrán sobre el HEAD final y sus hilos
se adjudicarán en §14.

### 1.6 CI del HEAD corregido (`851f20e0`)

Run `38176267616`. **No se declara `CI=PASS` global** — se declara lo medido, con el job autoritativo
distinguido del que falla por causa ajena.

```text
PY312_WINDOWS (autoritativo) = PASS   (11172 passed, 22 skipped; "Coverage XML written to file")
PY311_WINDOWS                = 1 failed, 11171 passed, 22 skipped
  fallo: tests/test_telegram_sender_adjuntos_y_429.py::TestFloodControl429::
         test_un_retry_after_invalido_se_trata_como_ausente
  causa: test de PLAZO de reloj (assert [0.984] == approx([1.0 ± 0.01])) — flake de scheduling
  clasificación: PRE_EXISTING_ENVIRONMENTAL (no toca el write-set; no toca NP/H4)
RUFF_CHECK (x2)              = PASS
MYPY (x2)                    = PASS
SECURITY_SCAN                = PASS
POSIX_CORE                   = PASS
TEST_NUEVO_EN_CI             = SÍ (tests/test_native_parallax_h4_authorization_gate.py corrió en py3.12)
```

**Cómo se leyó el fallo (no por el estado del check, por el log).** El job py3.11 reporta `failure`,
pero el log muestra `=== 1 failed, 11171 passed, 22 skipped … ===` **y** `Coverage XML written to
file`: la suite corrió completa. El único fallo es un test de *deadline* de reloj
(`publicación + retry_after`, tolerancia `1e-2`) en el módulo de Telegram — un componente que este
PR no toca (write-set = 1 doc + 1 test de invariantes documentales). El job py3.12 corrió **el mismo
test y pasó**, lo que confirma la flake. Se clasifica `PRE_EXISTING_ENVIRONMENTAL` y **no** se
presenta como verde global ni se usa para declarar el contrato verificado.

> **Lo que este CI sí prueba y lo que no.** Prueba que el documento y su ancla documental son
> consistentes, que el ancla corre en CI (no fue deseleccionada) y que no se rompió nada aguas
> arriba. **No** prueba nada del fix H4: la corrección no existe todavía. Un CI verde acá **no** es
> evidencia de que `resize_normal` esté arreglado.

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

Se distingue **el tipo/estructura del array** (contrato de forma, cerrado acá) de **la política
sobre `H ≠ W`** (V-03, diferido). Son dos cosas distintas y no se confunden.

| Atributo | Contrato cerrado | Fundamento | Estado |
|---|---|---|---|
| Tipo | `NDArray` convertible a `np.float64` | Firma vigente; el cast es explícito en la salida | `CONFIRMED` |
| `ndim` | **Exactamente 3** (`arr.ndim == 3`) | Firma `(H, W, 3)`; un 4D debe rechazarse, no adivinarse | `DESIGN_CLOSED` |
| Nº de canales | **Exactamente 3** (`arr.shape[-1] == 3`) | Es una normal tangencial RGB; 2/4/0 canales no son el tipo | `DESIGN_CLOSED` |
| `H > 0` y `W > 0` | **Requerido**; dimensión cero ⇒ rechazo | Una rejilla sin píxeles no es una normal | `DESIGN_CLOSED` |
| `H ≠ W` | **V-03** — ver fila propia abajo | Vacío declarado del preflight §7.3 | `DEFERRED` |
| Rango por canal | `[-1, 1]` | `decode_normal_image`: `rgb/255*2 − 1`; el corpus no excede | `CONFIRMED` |
| Normalización previa | Unitaria (ya renormalizada) | `decode_normal_image` | `CONFIRMED` |
| Convención | Aplicada por `apply_convention` **antes** del resize | `load_asset:317-318` | `CONFIRMED` |
| Finitud | **Requerida**; NaN/Inf ⇒ rechazo fail-closed | V-04; **hoy el camino uint8 los clipea en silencio** (medido, §3.4) | `CONTRACT_CLOSED` (decisión) |
| `size` destino | **Entero** con `size > 0` | §27 M6 (`512`); `type(size) is int` y `size >= 1` | `DESIGN_CLOSED` |

**Sobre la fila `H ≠ W`.** V-03 trata **exclusivamente** dimensiones no cuadradas
(`H ≠ W` con `H, W > 0` y 3 canales). La razón por la que se difiere es que `load_asset`
reescala a `512²` **antes** del solver, así que un asset no cuadrado se vuelve cuadrado **antes**
de llegar al resizer de la normal (§7.3 del preflight). Una forma **malformada**
(`ndim != 3`, `shape[-1] != 3`, `H == 0`, `W == 0`) **no** es un caso de V-03 y **no** puede
diferirse invocándolo.

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
| V-03 | Dimensiones **no cuadradas** (`H ≠ W`, `H,W > 0`, 3 canales) | Fuera del alcance H4; `load_asset` cuadra a 512 antes del solver | `DEFERRED` |
| V-04 | No finitos | Rechazo fail-closed, coherente con `decode_height_image` modo F | `CONTRACT_CLOSED` |
| V-05 | Estabilidad entre versiones | `NOT_MEASURED`; la versión se registra en la procedencia (§10) | `NOT_MEASURED` (registrado, no presentado como probado) |

**Corrección de alcance (F1 del review de #717).** V-03 cubre **sólo** `H ≠ W`. La revisión
anterior de este documento atribuía a V-03 el caso genérico `forma != (H,W,3)`, lo que era
incorrecto: `ndim != 3`, `shape[-1] != 3`, `H == 0`, `W == 0` y `size` inválido **no** son
"non-square" y se cierran acá como contrato propio (§3.4).

### 3.4 Regla de comportamiento ante entrada inválida (cierre)

**Medición del comportamiento vigente (ejecutada en este slice, sin corpus).** Antes de fijar la
regla se midió qué hace hoy la implementación, para no inventar semántica:

| Entrada | Comportamiento medido **hoy** | Es un contrato válido? |
|---|---|---|
| `(H,W,3)` válido | interpola normalmente | Sí |
| `(H,W,2)` | `IndexError: index 2 is out of bounds for axis 2 with size 2` | **No** — excepción genérica, no semántica |
| `(H,W,1)` | `IndexError` (axis 2 size 1) | **No** — idem |
| `(H,W,0)` | `IndexError` (axis 2 size 0) | **No** — idem |
| `(H,W,4)` | **OK** `(size,size,3)` — **descarta el 4º canal en silencio** | **No** — pérdida silenciosa |
| `(H,W)` 2D | **OK** `(size,size,3)` — broadcasting implícito de `n[..., c]` | **No** — semántica ambigua |
| `(H,W,3,1)` 4D | `ValueError: Too many dimensions: 3 > 2.` (de Pillow) | **No** — error de dependencia |
| `(0,W,3)` | **OK** — `H == 0` no falla | **No** — rejilla vacía sin marca |
| `(H,0,3)` | **OK** — `W == 0` no falla | **No** — idem |
| `size = 0` / `size < 0` | `ValueError: height and width must be > 0` (de Pillow) | **No** — error de dependencia |
| `size = 2.5` / `size = "4"` | `TypeError` (de Pillow) | **No** — error de dependencia |
| NaN / +Inf / −Inf | **OK** — clipeados a `[-1,1]` en silencio | **No** — contradice V-04 |
| `> 1` finito (p. ej. `1000.0`) | clip a `1.0` (declarado) | Sí (clip documentado) |
| dtype `float32`/`int32`/`uint8` | **OK** — cast a `float64` en la salida | Sí (tolerado) |

El hallazgo central es que **hoy no hay validación de forma ni de finitud**: unos casos explotan
con errores de dependencia (`IndexError`/`ValueError`/`TypeError` de Pillow/NumPy), otro
(`(H,W,4)`) descarta un canal **sin decirlo**, y los no finitos se clipean en silencio. Ninguno
de esos comportamientos es un contrato.

**Regla cerrada (fail-closed), separando lo que se rechaza de lo que se difiere:**

```text
RECHAZO FAIL-CLOSED (contrato cerrado — el Slice 2 debe implementar la validación):

  ndim != 3                          -> REJECT  (InvalidNormalShapeError-like)
  shape[-1] != 3                     -> REJECT  (no se descarta ningún canal)
  H == 0  o  W == 0                  -> REJECT  (rejilla vacía)
  cualquier NaN/Inf en la entrada    -> REJECT  (V-04; NO se clipea en silencio)
  size no-entero (type(size) is not int / bool) -> REJECT
  size < 1                           -> REJECT

DIFERIDO (V-03 — NO es rechazo, es alcance):

  H != W  con  H,W > 0  y  3 canales -> DEFERRED
      (load_asset cuadra a 512 antes del solver; el resizer no declara política acá)

TOLERADO (contrato ya vigente):

  dtype distinto de float64          -> cast a float64 (la firma promete float64 en salida)
  valores finitos fuera de [-1,1]    -> clip documentado por canal (comportamiento actual)
  entrada válida en camino identidad -> no-op (V-01)
  entrada válida con resize          -> interpolar + renormalizar (o NON_EVALUABLE si degenera)
```

**Casos explícitamente `NOT_DEFERRED`.** Un `ndim`/`shape[-1]` incorrecto, una dimensión vacía o
un `size` inválido **no** se difieren "a un slice futuro": se cierran acá como **rechazo**. Diferir
una forma malformada dejaría pasar exactamente el defecto que F1 señala (un canal descartado en
silencio, o un error de Pillow disfrazado de contrato).

```text
R1_ENVELOPE_CLOSED     = YES  (ndim, shape[-1], H>0, W>0, size, finitud -> REJECT)
V03_SCOPE              = H!=W SOLAMENTE (no cubre formas malformadas)
MALFORMED_NOT_DEFERRED = YES  (ndim/shape[-1]/H=0/W=0 -> rechazo, no "fuera de alcance")
```

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
V03_HANDLING        = DEFERRED_PRESERVED (sólo H!=W; no cubre formas malformadas)
```

### 3.6 Mensaje de cierre de R1

R1 pasa de `DESIGN_PROPOSED` a `DESIGN_CLOSED`: el contrato de entrada y salida está
especificado como unidad, el **envelope estructural** (`ndim == 3`, `shape[-1] == 3`, `H,W > 0`,
`size` entero positivo, finitud) se cierra como **rechazo fail-closed**, V-03 queda acotado a
`H ≠ W` sin absorber casos que no cubre, y el criterio de tolerancias está fijado por fuente
(sintético/analítico) sin números ajustados al corpus. **No** se declara `CONFIRMED`: eso requiere
la implementación, que no existe.

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

### 5.3 El vehículo de la señal: `resize_normal → load_asset → runners` (contrato cerrado)

**El problema (F2 del review de #717).** Decir "la señal se eleva desde una capa que conoce el
asset" no alcanza: la firma vigente es `resize_normal(n, size) -> NDArray[np.float64]` y devuelve
**sólo** un array. Nada en esa firma transporta `degenerate_count`, `total_pixels` ni `threshold`.
Sin un vehículo explícito, el conteo se pierde entre el resizer y el asset, y la política
fail-closed no es ejecutable. En particular, **no** se puede "detectar la degeneración en el
resizer y lanzar desde el consumidor" si el resizer no comunica nada.

#### 5.3.1 El vehículo: resultado tipado, no excepción desde el resizer

Se cierra el mecanismo: `resize_normal` **no** lanza por degeneración (sigue devolviendo un
`NDArray`, para no romper el call-site de test ni la firma, y porque un vector degenerado **no**
es un fallo del resizer sino un hecho del campo). Devuelve en cambio un **resultado tipado** que
lleva la evidencia:

```text
# Contrato cerrado para el Slice 2 (NO implementado acá):

@dataclass(frozen=True)
class NormalResizeResult:
    array: NDArray[np.float64]      # (size, size, 3); puede contener el vector crudo (aún sin renormalizar)
    degenerate_count: int           # píxeles con 0 <= L < threshold (antes de renormalizar)
    total_pixels: int               # size*size
    threshold: float                # DEGENERATE_THRESHOLD usado en esta corrida
    degenerate: bool                # degenerate_count > 0

def resize_normal_diagnostic(n, size) -> NormalResizeResult: ...   # NUEVA API del Slice 2
def resize_normal(n, size) -> NDArray[np.float64]:                 # API VIGENTE, preservada
    return resize_normal_diagnostic(n, size).array                 # delega; no cambia su firma
```

**Por qué `resize_normal` conserva la firma.** Hay dos caminos:

1. **Descartado:** cambiar la firma de `resize_normal` a `-> NormalResizeResult`. Rompería el
   call-site de test (`tests/test_native_parallax_exp_m2.py:96`) y todo consumidor que hoy usa el
   array directamente; además obligaría a cambiar la primitiva que el preflight quiere corregir
   **sin** cambiar su contrato de retorno (§5.1: "se devuelve el tipo contratado `float64`").
2. **Elegido:** se agrega `resize_normal_diagnostic` (o se le da a `resize_normal` un parámetro
   `*, with_diagnostics: bool = False`). `resize_normal` delega y sigue devolviendo el array. El
   diagnóstico vive en la varita nueva, no en la firma histórica.

**Cómo se transporta sin publicar un array inválido.** El punto clave: `load_asset` **no** devuelve
el array del resizer si es degenerado. Lo inspecciona **antes** de construir `AuthoredMaterial`:

```text
load_asset(spec, resolution, ...):
    res = resize_normal_diagnostic(normal, resolution)            # ← recibe la evidencia
    if res.degenerate:                                            # ← decisión con identidad del asset
        raise DegenerateNormalError(
            asset_id = spec.asset_id,                             # identidad conservada
            degenerate_count = res.degenerate_count,              # censo
            total_pixels = res.total_pixels,                      # denominador
            threshold = res.threshold,                            # umbral usado
            fraction = res.degenerate_count / res.total_pixels,   # fracción
            reason = "NON_EVALUABLE: vector nulo/casi-nulo tras interpolación",
        )
    ...  # sólo si no es degenerado se construye AuthoredMaterial
```

Así el array degenerado **nunca** llega al solver: `load_asset` aborta antes de retornar
`AuthoredMaterial`. La información viaja **dentro del objeto de error**, no dentro del array.

#### 5.3.2 Jerarquía de excepciones

```text
DatasetInvalidError(Exception)                    # integridad de archivo/manifest (existente)
├─ InvalidNormalShapeError(DatasetInvalidError)   # NUEVA: ndim/shape[-1]/H=0/W=0/size inválido (§3.4, F1)
└─ DegenerateNormalError(DatasetInvalidError)     # NUEVA: degeneración H4 (D-06)
      .asset_id, .degenerate_count, .total_pixels, .threshold, .fraction, .reason
```

Los dos errores nuevos **heredan** de `DatasetInvalidError` para compatibilidad de los `except`
existentes, pero cada uno se lanza **antes** del `continue` de M3 y su handling debe **preceder**
al `except DatasetInvalidError`. **Son señales distintas**: una es "forma/estructura inválida"
(F1), la otra es "dirección no confiable" (D-06). Un consumidor que quiera tratar sólo la
degeneración captura `DegenerateNormalError` explícitamente.

#### 5.3.3 Reglas de propagación por consumidor (fail-closed)

| Consumidor | Comportamiento actual | Regla cerrada (Slice 2) |
|---|---|---|
| `run_exp_m2.py` | propaga (0 capturas) | `DegenerateNormalError` **aborta** la corrida (burbujea); no publica |
| `run_exp_m4.py` | propaga (0 capturas) | idem |
| `run_exp_m5.py` | propaga (0 capturas) | idem |
| `run_exp_m3.py` | `except DatasetInvalidError: continue` (L340) | **Debe** capturar `DegenerateNormalError` **antes** y **abortar** (`raise`), no `continue`. Sin roster persistente no hay `NON_EVALUABLE` que registrar ⇒ se aborta. |
| `fetch_exp_m2_corpus.py` | 3 capturas, **no** ejerce el resizer | Sin cambio (no es consumidor de degeneración) |

**Transporte de la evidencia a través de los runners.** Cuando el runner tenga roster persistente
(Slice 3), el `except DegenerateNormalError as exc` escribe, por asset:
`degenerate_count`, `total_pixels`, `fraction`, `threshold`, `reason`, estado `NON_EVALUABLE`.
Mientras ese roster **no exista** (hoy), el runner **aborta** y registra el asset causante en
stderr — pero **no** publica artefacto científico exitoso. `continue` está **prohibido**.

#### 5.3.4 Reglas de comportamiento (fail-closed)

1. `resize_normal` **no** lanza por degeneración; delega en `resize_normal_diagnostic`.
2. `load_asset` inspecciona el resultado **antes** de construir `AuthoredMaterial` y lanza
   `DegenerateNormalError` con toda la evidencia, incluida la identidad del asset.
3. `DegenerateNormalError` **hereda** de `DatasetInvalidError` (compatibilidad) pero se lanza
   **antes** del `continue` de M3 y su handling **precede** al `except DatasetInvalidError`.
4. En M3, `except DegenerateNormalError` debe **abortar**, **no** `continue` — no hay roster
   persistente con estado `NON_EVALUABLE`.
5. Mientras no exista roster persistente, **ningún** consumidor publica un artefacto científico
   exitoso tras un `DegenerateNormalError`.
6. **Prohibido** cambiar exclusiones preexistentes (integridad) por motivos distintos de H4
   (dictamen: *"No cambiar exclusiones preexistentes por motivos distintos de H4"*).

#### 5.3.5 Tests sintéticos discriminatorios exigidos (Slice 2)

Cada uno debe fallar contra un vehículo ausente y pasar con el vehículo implementado:

| Test | Qué prueba | Discrimina contra |
|---|---|---|
| `test_cuenta_exacta_de_pixeles_degenerados` | `degenerate_count` y `total_pixels` del caso sintético de cancelación coinciden con el censo del oráculo independiente | un resizer que devuelve el array sin conteo |
| `test_umbral_reportado_es_el_usado` | `result.threshold` == el `DEGENERATE_THRESHOLD` de la corrida | un threshold recomputado o inventado |
| `test_rechazo_visible_no_exclusion_silenciosa` | Con degenerados, `load_asset` lanza `DegenerateNormalError` (no devuelve material) | un `continue`/exclusión silenciosa |
| `test_cero_publicacion_cientifica_exitosa` | El runner aborta y **no** escribe artefacto de resultado | un runner que publica con la cohorte alterada |
| `test_identidad_del_asset_conservada` | `exc.asset_id` es el del asset que degeneró | un error sin identidad |
| `test_exclusiones_historicas_no_h4_intactas` | Las exclusiones por integridad preexistentes siguen siendo exclusiones por integridad | una reclasificación de integridad como H4 |
| `test_forma_malformada_no_es_degeneracion` | `(H,W,2)`/`(H,W,4)` lanzan `InvalidNormalShapeError`, **no** `DegenerateNormalError` | confundir F1 con D-06 |

**Ancla AST exigida (Slice 2).** Un test que **enumere** por introspección/AST todos los
`except` de los runners y exija que:

- ningún handler capture `DegenerateNormalError` con `continue`;
- el handler de `DegenerateNormalError` **preceda** al de `DatasetInvalidError` cuando ambos
  aparecen en el mismo `try` (el orden de las cláusulas es load-bearing: una subclase capturada
  antes de la superclase);
- todo módulo que llame a `load_asset` **o** a `resize_normal` esté en el censo congelado — un
  consumidor nuevo rompe el ancla hasta que se le declare su tratamiento.

```text
TRANSPORT_VEHICLE                = NormalResizeResult (dataclass, campo .array + censo)  DESIGN_CLOSED
RESIZE_NORMAL_SIGNATURE_PRESERVED= YES (delega; el diagnóstico vive en la varita nueva)
LOAD_ASSET_RAISES_WITH_IDENTITY  = YES (DegenerateNormalError con asset_id + censo)
DEGENERATE_ERROR_BEFORE_CONTINUE = YES (handling precede a except DatasetInvalidError)
THREE_ERRORS_DISTINCT            = YES (DatasetInvalid / InvalidNormalShape / DegenerateNormal)
M3_ABORTS_NOT_CONTINUE           = YES (mientras no haya roster persistente)
SCIENTIFIC_PUBLICATION_PROHIBITED= YES (tras DegenerateNormalError)
EXISTING_EXCLUSIONS_UNCHANGED    = YES (no reclasificar integridad como H4)
SYNTHETIC_DISCRIMINATING_TESTS   = 7 (especificados, NO implementados)
AST_ANCHOR_SPECIFIED             = YES (ordena handlers + censo congelado)
R6_CONSUMER_COVERAGE             = DESIGN_CLOSED
```

### 5.4 El ancla de enumeración (Familia E, diseñada)

El preflight (§8 Familia E) pide un ancla que detecte una **nueva ruta de carga** que evite el
resizer declarado. Cierre del diseño:

- **Detectar por AST**, no por lista: recorrer los runners y sus imports; congelar el conjunto de
  módulos que llaman a `load_asset` **y** el conjunto que llama a `resize_normal` (o a la varita
  `resize_normal_diagnostic`).
- Un primitivo nuevo (otra función que produzca `N_AUTH` sin pasar por `load_asset`) rompe el
  ancla hasta que se le declare procedencia.
- **No** limitarse a `load_asset`: enumerar también las rutas de runner (M2/M3/M4/M5) para que una
  ruta nueva **no** herede silenciosamente una excepción.
- **Ordinal de handlers:** cuando un `try` captura `DatasetInvalidError` y `DegenerateNormalError`,
  el de la subclase debe ir **primero**. El ancla verifica el **orden**, no sólo la presencia.

```text
FAMILY_E_ANCHOR_DESIGNED          = YES
ANCHOR_ENUMERATES_BY_AST          = YES
ANCHOR_COVERS_ALL_RUNNERS         = YES (M2/M3/M4/M5 + load_asset)
ANCHOR_CHECKS_HANDLER_ORDER       = YES (subclase antes de superclase)
ANCHOR_IMPLEMENTED                = NO  (pertenece al Slice 2)
```

### 5.5 Mensaje de cierre de R6

R6 pasa a `DESIGN_CLOSED`: la enumeración se hizo por AST (no a mano), el punto crítico de M3
quedó `CONFIRMED` con la línea exacta, el **vehículo** de la señal está especificado de punta a
punta (`resize_normal_diagnostic → NormalResizeResult → load_asset → runners`, §5.3), la jerarquía
de excepciones hace imposible confundir un rechazo de forma con un rechazo de degeneración, las
reglas de propagación están fijadas por consumidor (§5.3.3), y el ancla de cobertura está diseñada
para **detectar** consumidores nuevos **y** verificar el **orden** de los handlers (§5.4). Lo que
**no** se cierra es la implementación del vehículo, de la jerarquía ni del ancla (Slice 2).

Lo que F2 corrigió respecto de la primera versión de este documento: no basta con declarar que el
consumidor decide. La firma `resize_normal` devuelve hoy **sólo** `NDArray`, de modo que
`degenerate_count`, `total_pixels` y `threshold` no tienen por dónde viajar. Por eso el contrato
introduce un resultado tipado (`NormalResizeResult`) y una API nueva
(`resize_normal_diagnostic`), preservando la firma de `resize_normal` como fachada. Sin ese
vehículo, cualquier "regla de propagación" sería inejecutable y el conteo se perdería en el
primer `return`.

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
| 2 | Contrato numérico cerrado, incluidos V-01…V-05 | **YES** | §3 (este documento, corregido por F1) + §7 preflight | `dff54d13` | `test_..._authorization_gate.py` | V-03 `DEFERRED` (**sólo** `H != W`), V-05 `NOT_MEASURED` (registrados) | R1 | Tech Lead |
| 3 | Política de degenerados elegida y declarada | **YES** | §5.3 preflight (D-06 `FAIL_CLOSED`) + §5.3 de este doc (vehículo F2) | — | Ancla documental | Umbral sin valor (R7) | D-07b | Tech Lead |
| 4 | Plan de mutantes con test detector por mutante | **YES** | §9 preflight (M-01…M-10) | — | Ancla documental | M-10 debe romper algo (nota §9) | — | Tech Lead |
| 5 | Ancla de enumeración de primitivas **diseñada** | **YES** | §5.4 (este documento, corregido por F2) + §8 Familia E | — | Ancla documental + `ANCHOR_IMPLEMENTED=NO` | Implementación pendiente | R6 | Tech Lead |
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

**Efecto de la revisión adversarial de #717 sobre esta matriz.** La revisión
(`comment 6113684862`) devolvió `PR717_REVIEW=CHANGES_REQUESTED_FOR_CONTRACT_COMPLETENESS` con
dos hallazgos materiales (F1, F2). Ambos se adjudicaron y se corrigió el **contrato**, no la
redacción. Consecuencia sobre la matriz: las filas 2, 3 y 5 pasan a apoyarse en §3 (corregido) y
§5.3–§5.4 (nuevos); **ninguna fila cambia de estado** — las tres ya eran `YES`, y lo que cambió es
la **calidad de la evidencia** que las sostiene. La condición 9 sigue `PENDING`: no hay ninguna
aprobación derivable de tests, ni de CI, ni de haber cerrado F1/F2.

```text
F1_SHAPE_CHANNEL_SEMANTICS   = CLOSED        (envolvente estructural; V-03 sigue DEFERRED)
F2_DEGENERATE_TRANSPORT      = CLOSED        (vehículo tipado + jerarquía + propagación)
F1_F2_SCOPE_CHANGED          = NO            (no se amplió el alcance de §17)
CONDITION_9_STATUS           = PENDING
```

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
| N-07 | (F1, revisión adversarial) La primera versión de §3.4 declaraba `forma != (H,W,3) -> DEFERRED (V-03)`, usando V-03 —que en el preflight §7.3 trata **dimensiones no cuadradas**— para diferir canales incorrectos, dimensiones cero y `size` inválido. El instrumento de medición fue un probe que ejercitó 10 formas hostiles: `(H,W,2)`→`IndexError`, `(H,W,4)`→**descarte silencioso del 4.º canal**, `(H,W)` 2D→aceptado, `(0,W,3)`→aceptado, `size=0/−1`→`ValueError` de Pillow, `size=2.5`→`TypeError`, `NaN/Inf`→**clip silencioso**. | **Defecto de contrato** | §3.4 |
| N-08 | (F2, revisión adversarial) La primera versión de §5.3 declaraba que "el consumidor decide" sin especificar por dónde viajan `degenerate_count`, `total_pixels` y `threshold` — la firma `resize_normal` devuelve sólo `NDArray`. Regla sin vehículo = regla inejecutable; el conteo se pierde en el primer `return`. | **Defecto de contrato** | §5.3.1 |

**Contradicciones resueltas sin inventar semántica:**

- El brief pedía cerrar "resoluciones admitidas" y "comportamiento ante dimensiones inválidas".
  El preflight declara V-03 `DEFERRED` (corpus cuadrado). **Resolución:** se cierra **el límite**
  (no se valida forma no cuadrada) y se prohíbe inventar una regla de dominio. §3.3.
- El brief pedía "finitud y norma" como parte de R1. El preflight las cerró en V-04/D-06.
  **Resolución:** se consolidan en §3.2 sin reabrir las decisiones de Tech Lead.

**Adjudicación de F1 y F2 (revisión adversarial de `#717`, comment `6113684862`).** Ambos fueron
`MATERIAL` y `ACCEPTED`. Ninguno se resuelve "mejorando la prosa": cada uno exigió **medir** y
**cambiar el contrato**.

| Finding | Estado inicial | Adjudicación | Corrección aplicada | Evidencia |
|---|---|---|---|---|
| **F1** — el diferido de V-03 se usaba para formas malformadas | `REJECTED` como redacción: la tabla confundía dos ejes (cuadratura vs. envolvente estructural) | **ACCEPTED (material)** | §3.1 separa el **envolvente estructural** (`ndim`, `shape[-1]`, `H,W>0`, finitud, `size`) de V-03 (`H != W` **únicamente**). §3.4 fija la regla `REJECT / DEFERRED / TOLERATED` por caso, con la tabla de comportamiento medido. | `scratch/f1_shape_probe.py` → §3.4 |
| **F2** — no había vehículo para la señal de degeneración | `PARTIAL`: la política estaba decidida, la propagación no | **ACCEPTED (material)** | §5.3.1 introduce `NormalResizeResult` (frozen) y `resize_normal_diagnostic`, preservando la firma de `resize_normal`. §5.3.2 jerarquía de excepciones. §5.3.3 propagación por consumidor. §5.3.5 siete tests sintéticos. §5.4 ancla que verifica **orden** de handlers. | §5.3–§5.4 |

**Lo que F1 **no** autoriza.** Cerrar la envolvente estructural no cierra V-03. `H != W` sigue
`DEFERRED`: el corpus es cuadrado por construcción y no hay evidencia de que el resizer deba
admitir no-cuadradas. La diferencia es que ahora la **deferencia es una sola cosa** (cuadratura
real) y no una bolsa donde entraba todo lo que no se quiso especificar.

**Lo que F2 **no** autoriza.** Especificar el vehículo no implementa el vehículo.
`NORMAL_RESIZE_RESULT_IMPLEMENTED = NO`, `EXCEPTION_HIERARCHY_IMPLEMENTED = NO` y
`HANDLER_ORDER_ANCHOR_IMPLEMENTED = NO` pertenecen al Slice 2. Lo cerrado acá es el **contrato**
(firmas, campos, orden de inspección, reglas de propagación, tests exigidos), no el código.

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

**Sonda F1 — envolvente estructural (medida en este slice, `scratch/f1_shape_probe.py`).**
Ejercita la **implementación vigente** con entradas hostiles para fundar la tabla de §3.4 en
comportamiento observado, no en suposición:

| Entrada hostil | Comportamiento **vigente** medido | ¿Es aceptable? | Regla F1 |
|---|---|---|---|
| `(H, W, 3)` válido | `OK shape=(2, 2, 3)` | Sí (referencia) | `TOLERATED` |
| `(H, W, 2)` | `IndexError: index 2 is out of bounds for axis 2 with size 2` | No: excepción no tipada | `REJECT` |
| `(H, W, 1)` | `IndexError: index 1 is out of bounds for axis 2 with size 1` | No: excepción no tipada | `REJECT` |
| `(H, W, 0)` | `IndexError: index 0 is out of bounds for axis 2 with size 0` | No: excepción no tipada | `REJECT` |
| `(H, W, 4)` | **Acepta y devuelve `(2, 2, 3)`: descarta el 4.º canal en silencio** | **No: pérdida silenciosa de datos** | `REJECT` |
| `(H, W)` 2D | **Acepta vía broadcast implícito** | No: tipo equivocado | `REJECT` |
| `(H, W, 3, 1)` 4D | `ValueError: Too many dimensions: 3 > 2.` | No: excepción no tipada | `REJECT` |
| `(0, W, 3)` | **Acepta** (produce salida interpolada de una rejilla vacía) | No: rejilla sin píxeles | `REJECT` |
| `(H, 0, 3)` | **Acepta** (ídem) | No | `REJECT` |
| `size = 0` | `ValueError: height and width must be > 0` (de Pillow, no del dominio) | No: falla tarde y ajena | `REJECT` |
| `size = −1` | `ValueError: height and width must be > 0` | No | `REJECT` |
| `size = 2.5` | `TypeError: 'float' object cannot be interpreted as an integer` | No: firma exige `int` | `REJECT` |
| `size = "4"` | `TypeError: 'str' object cannot be interpreted as an integer` | No | `REJECT` |
| `NaN` en la entrada | **Acepta: clipeo silencioso** (`RuntimeWarning: invalid value encountered in cast`; salida `finite=True`) | **No: dato corrupto sin señal** | `REJECT` |
| `±Inf` en la entrada | **Acepta: clipeo silencioso** a `±1` | **No** | `REJECT` |
| `(2, 2, 2)` camino identidad | **Devuelve `(2, 2, 2)` sin validar canales** | No: el camino identidad evade el contrato | `REJECT` (D-07) |
| `(2, 2, 4)` camino identidad | **Devuelve `(2, 2, 4)` sin validar canales** | No | `REJECT` (D-07) |
| `(2, 2)` camino identidad | **Devuelve `(2, 2)` sin validar `ndim`** | No | `REJECT` (D-07) |

Las seis filas en **negrita** son las que motivan que la envolvente sea `REJECT` explícito y no una
deferencia: el descarte del 4.º canal, la aceptación de 2D por broadcast, la interpolación de una
rejilla vacía, el clipeo de no-finitos y el camino identidad que no valida canales/`ndim` son
**pérdidas silenciosas**, exactamente la clase de defecto que H4 combate (`astype` que trunca sin
avisar). Diferirlas habría reproducido el mismo patrón en otra capa.

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
DIFF_CHECK       = PASS  (sin whitespace sobrante ni marcadores; `git diff --check` limpio)
MARKDOWN_TABLES  = PASS  (21 tablas, 0 inconsistencias — con las funciones del checker del repo IMPORTADAS)
MARKDOWN_BARRAS  = PASS  (0 celdas con "\|")
TEST_NUEVO       = PASS  (33 passed — tests/test_native_parallax_h4_authorization_gate.py)
TEST_VIGENTE     = PASS  (18 passed — test_native_parallax_exp_m2.py, incluye el ancla del resizer)
CHECKER_REPO     = PASS  (8 passed — test_native_parallax_pr700_corrective_docs_invariants.py)
SUITE_FOCAL_NP   = PASS  (79 passed — m2 + m2_json + pr700 docs + pr697 reconciliation + gate)
RUFF_CHECK       = PASS  (All checks passed!)
RUFF_FORMAT      = PASS  (858 files already formatted)
MYPY             = PASS  (no issues found in 364 source files)
MUTANTES_ANCLA   = 17/17 killed  (8 de autorización + M-09/M-09b/M-10/M-11 de F1 +
                                   M-12/M-13/M-14/M-15/M-16 de F2)
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

**Segunda lección (mutantes F1/F2, tres rondas).** La primera versión de los anclas F1/F2
**no era load-bearing**: M-09, M-10 y M-11 **sobrevivían** con baseline verde. Causa: los tests
verificaban *presencia de token en el documento* (`"REJECT" in texto`) en vez de *la regla de
cada fila*. La corrección fue anclar por **fila de tabla** —extraída por encabezado, con
normalización de backticks— exigiendo que **cada** fila estructural declare `REJECT`/`TOLERATED`
y **ninguna** declare `DEFERRED`. Muestrear filas es el mismo error que muestrear call-sites.

**Tercera lección (contaminación por SIGTERM).** La primera corrida del driver murió por SIGTERM
*durante* una mutación: `finally` no corrió, el doc quedó mutado (`degenerate_count` → `cnt`) y el
backup de la corrida siguiente congeló **ese** estado como "pristino" — el baseline salió con
**1 failed** y M-13 se reportó "NO APLICADO". El driver ahora: (1) exige baseline **verde** antes
de mutar y aborta si no lo está; (2) registra el hash pristine en un sidecar y detecta
contaminación entre corridas; (3) re-verifica el hash al restaurar. Lección: un backup tomado
*después* de una mutación interrumpida no es un pristine — es evidencia contaminada.


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

**Adiciones de este cierre al write-set del Slice 2** (consecuencia de §5.2/R6 y de la corrección
F2 de §5.3):

- El **vehículo** de la señal: `NormalResizeResult` (frozen dataclass) y la API
  `resize_normal_diagnostic`, con `resize_normal` conservando su firma como fachada.
- La jerarquía `InvalidNormalShapeError` / `DegenerateNormalError` (bajo `DatasetInvalidError`) y
  su handling fail-closed en **cada** consumidor enumerado por el ancla (M2/M3/M4/M5), con
  `continue` prohibido para la señal de degeneración.
- El ancla de cobertura (Familia E) implementada por AST, incluyendo la verificación del **orden**
  de los handlers (subclase antes que superclase).
- La **envolvente estructural** de F1 (`ndim == 3`, `shape[-1] == 3`, `H,W > 0`, finitud, `size`
  entero ≥ 1) como validación explícita. V-03 (`H != W`) **no** entra: sigue `DEFERRED`.

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

F1_SHAPE_CHANNEL_SEMANTICS     = CLOSED  (envolvente estructural REJECT; V-03 H!=W sigue DEFERRED)
F2_DEGENERATE_TRANSPORT        = CLOSED  (NormalResizeResult + jerarquía + propagación por consumidor)

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
cerrados como documentación verificable, y las dos objeciones **materiales** de la revisión
adversarial de #717 (F1 y F2) quedaron adjudicadas y corregidas en el contrato —no en la prosa—.

F1 se cerró separando dos ejes que la primera versión mezclaba: el **envolvente estructural**
(ndim, canales, dimensiones vacías, finitud, `size`) se rechaza explícitamente; `H != W` —lo
**único** que V-03 significa— sigue diferido. Antes, V-03 era la bolsa donde caía todo lo que no
se había medido.

F2 se cerró especificando el **vehículo** de la señal de punta a punta: sin él, "el consumidor
decide" era una regla sin transporte y el conteo de degenerados se perdía en el primer `return`
de `resize_normal`. El contrato ahora fija el resultado tipado, la jerarquía de excepciones, el
orden de inspección en `load_asset` y la propagación por consumidor.

No queda ningún requisito **técnico** previo abierto. Lo único que falta para autorizar el Slice 2
es la **conformidad explícita del Tech Lead** con el alcance (condición 9), que por definición no
puede derivarse de tests ni de CI. Mientras esa firma no exista, el estado es `BLOCKED` y
`H4_CODE_IMPLEMENTATION_AUTHORIZED = NO`.

---

## 14. Registro de adjudicación adversarial

Esta sección es el registro de los hallazgos de revisión y su adjudicación. Se actualiza cada vez
que llega una revisión nueva; no se borra lo anterior.

### 14.1 Revisión humana `6113684862` (F1, F2)

| Item | Finding | Severidad declarada | Adjudicación propia | Razón |
|---|---|---|---|---|
| F1 | `forma != (H,W,3) -> DEFERRED (V-03)` usaba V-03 (cuadratura) para diferir canales/`ndim`/dimensiones cero/`size` | `MATERIAL` | **ACCEPTED** | Verificado contra el código vigente: `(H,W,4)` descarta el 4.º canal en silencio, `(H,W)` 2D se acepta, `NaN/Inf` se clipea en silencio. Diferirlos habría reproducido el defecto de pérdida silenciosa. Corregido en §3.1/§3.4. |
| F2 | "El consumidor decide" sin especificar cómo viaja `degenerate_count`/`total_pixels`/`threshold` (la firma devuelve sólo `NDArray`) | `MATERIAL` | **ACCEPTED** | Una regla sin vehículo es inejecutable: el conteo se pierde en el primer `return`. Corregido en §5.3 (vehicle tipado + jerarquía + propagación) y §5.4 (ancla de orden). |

```text
F1_ADJUDICATION        = ACCEPTED
F2_ADJUDICATION        = ACCEPTED
F1_CLOSED              = YES (contrato, no prosa)
F2_CLOSED              = YES (contrato, no prosa)
PRODUCTION_TOUCHED     = NO
```

**Hallazgos de la propia adjudicación (no estaban en el review).** Al medir F1 con la sonda de
formas apareció que el **camino identidad** (`H == W == size`) también evade la validación: devuelve
`(2,2,2)`, `(2,2,4)` o `(2,2)` sin verificar nada. Eso es un caso de D-07 (V-01) que la primera
versión del documento no había conectado con la envolvente de F1. Se incorpora a §3.4.

### 14.2 Estado de threads inline

```text
THREADS_INLINE_ABIERTOS   = 0   (el PR estaba en Draft; los bots no corrieron)
THREADS_PENDIENTES        = 0
PROXIMA_REVISION          = al pasar a Ready, sobre el HEAD final
```

Cuando el PR pase a `Ready`, los revisores automáticos correrán sobre el HEAD final. Cualquier
hilo nuevo se adjudicará en `14.3` (agregado, no reemplazo). **Regla:** ningún hilo se cierra sin
una razón escrita; "ya está bien" no es una razón, y una corrección de prosa no cierra un hallazgo
material.
