# H4 `resize_normal` — Preflight científico y de ingeniería

> **Slice:** `H4_RESIZE_NORMAL_FIX_PREFLIGHT` + `H4_PREFLIGHT_CONTRACT_CLOSURE` · **Carácter:**
> `PRE_FLIGHT_RESEARCH_ONLY` (docs-only).
> **No implementa el fix, no ejecuta M6, no accede al corpus real, no ajusta thresholds y no
> modifica ningún PR abierto (#675, #697) ni la evidencia de #700.**
>
> Este documento **especifica** un contrato para un futuro slice de código H4. No lo autoriza:
> `H4_CODE_IMPLEMENTATION_AUTHORIZED = NO` y `M6_IMPLEMENTATION_BLOCKED = YES`.
>
> **Write-set:** un único archivo nuevo —
> `docs/design/research/native-parallax/h4-resize-normal-preflight.md`.
>
> **Revisión 2 (`H4_PREFLIGHT_CONTRACT_CLOSURE`, 2026-10-10).** Incorpora el veredicto del Tech
> Lead sobre PR #711: **D-06 cerrado** en fail-closed (§5.3), **D-07 cerrado** preservando el
> no-op válido del camino identidad (§7.3), **alcance de la deprecación de Pillow corregido**
> (§4.1) y **separación explícita** entre requisitos previos y tests de implementación (§8.0). El
> registro completo está en §21.

---

## 0. Leyenda de estados

Todo estado de este documento es uno de los siguientes. Un estado no listado es un defecto del
documento.

| Estado | Significado |
|---|---|
| `CONFIRMED` | Verificado por ejecución en este slice, o publicado como artefacto en el repositorio y citado por ruta. |
| `CONTRACT_CLOSED` | Decisión de contrato **tomada y registrada** (Tech Lead, 2026-10-10). La implementación sigue pendiente: no es evidencia de corrección. |
| `DESIGN_PROPOSED` | Especificado acá. **No implementado. No probado.** No es evidencia de corrección. |
| `NOT_MEASURED` | Reconocido y no medido. Se declara explícitamente en vez de estimarse. |
| `DEFERRED` | Fuera de este slice por decisión de alcance. |
| `BLOCKED` | Depende de algo que no existe (código, artefacto o autorización). |

Prohibido en este documento: declarar `CONFIRMED` un test que todavía no existe, o presentar un
`DESIGN_PROPOSED` como si ya estuviera verificado.

---

## 1. Objetivo y alcance

### 1.1 Objetivo

Producir el contrato técnico reproducible que debe cumplirse **antes** de corregir
`sky_claw/local/native_parallax/research/authored_dataset.py::resize_normal` (defecto H4,
`CONFIRMED` en PR #700), de modo que la corrección no comprometa la investigación de Native
Parallax ni la futura ejecución de EXP-M6.

### 1.2 Preguntas que este preflight responde

1. Cuál es exactamente el defecto a corregir.
2. Qué comportamiento debe conservarse.
3. Qué invariantes debe cumplir la implementación nueva.
4. Qué tests independientes demostrarán la corrección.
5. Cómo se garantiza que no se altera otra primitiva.
6. Cómo se diferencia la corrección del resizer de su impacto sobre M6.
7. Cómo se medirá un contrafactual sin ajustar thresholds.
8. Qué evidencia exige autorización para un posterior PR de código.

Las diez preguntas de Tech Lead del brief se responden en §19.

### 1.3 Alcance y no-alcance

**En alcance:** auditoría de la implementación vigente; especificación numérica; plan TDD
`RED→GREEN`; plan de mutantes; contrato de procedencia machine-readable; protocolo de
contrafactual de dos brazos con métricas M6; análisis de riesgo; separación de slices.

**Fuera de alcance (explícito):** implementar el fix; crear tests; ejecutar M6; ejecutar el
contrafactual; abrir o modificar PRs ajenos; tocar el corpus real; re-derivar thresholds.

`REAL_CORPUS_ACCESSED = NO` · `M6_IMPLEMENTATION_EXECUTED = NO` · `THRESHOLDS_TUNED = NO`.

### 1.4 Write-set

Único archivo de contenido autorizado:

```text
docs/design/research/native-parallax/h4-resize-normal-preflight.md
```

El directorio existe en `main`; el documento es nuevo. No se crearon tests, no se regeneraron
resultados, no se editó ningún artefacto científico ni ningún documento de #675 / #697 / #700.

---

## 2. Estado Git y dependencias

### 2.1 Recon fail-closed

Ejecutado desde la raíz real `E:/Skyclaw_Main_Sync` antes de crear nada.

```text
REPO_TOPLEVEL            = E:/Skyclaw_Main_Sync
GIT_VERSION              = 2.55.0.windows.3
MAIN_WORKTREE_BRANCH     = main
MAIN_WORKTREE_HEAD       = ef4b8aa9e485291ced37df23a1d566d5639d030d
MAIN_WORKTREE_STATUS     = limpio (git status --porcelain=v1 vacío)
ORIGIN_MAIN              = 647d2461c5eee2c0827f99f9df360e300adfbe13
ORIGIN_HEAD              = 647d2461c5eee2c0827f99f9df360e300adfbe13
```

**Hallazgo (no bloqueante, declarado):** el `main` local del worktree principal está **55 commits
por detrás** de `origin/main` (`git rev-list --left-right --count main...origin/main` → `0 55`).
El SHA del brief (`647d2461…`) **coincide exactamente** con `origin/main` medido en tiempo real
(`git ls-remote origin HEAD` → `647d2461…`). Por eso el worktree se creó desde `origin/main` y
**no** desde la rama `main` local desactualizada.

```text
LOCAL_MAIN_STALE        = YES (55 commits behind origin/main)
BRIEF_SHA_MATCHES_MAIN  = YES
WORKTREE_BASE           = origin/main (647d2461…)
```

### 2.2 Worktree creado

```bash
git worktree add -b h4-resize-normal-preflight \
  "E:/Skyclaw_Main_Sync/.worktrees/h4-resize-normal-preflight" origin/main
```

Verificación de que la ref se escribió (trampa conocida de esta máquina: las refs anidadas
pueden no persistir; por eso el nombre es **plano**):

```text
WORKTREE            = E:/Skyclaw_Main_Sync/.worktrees/h4-resize-normal-preflight
LOCAL_BRANCH        = h4-resize-normal-preflight   (plana, sin "/")
HEAD                = 647d2461c5eee2c0827f99f9df360e300adfbe13
git log --oneline -1= 647d2461 Merge pull request #708 …
WORKTREE_STATUS     = limpio
```

`git rev-parse --abbrev-ref HEAD` devuelve `h4-resize-normal-preflight` y `git log -1` responde
con un commit real ⇒ la ref **sí** se escribió. No hubo que reparar nada a mano.

**`.worktrees/` ya está en `.gitignore`** (línea 77). No se modificó `.gitignore`. Ningún archivo
de `.worktrees/` entró al seguimiento Git.

### 2.3 Ausencia de escritores concurrentes y de colisión de rama

```text
REMOTE_BRANCH_EXISTED_BEFORE   = NO  (git ls-remote origin refs/heads/research/native-parallax-h4-resize-normal-preflight → vacío)
WORKTREE_NAME_COLLISION        = NO  (no existía .worktrees/h4-resize-normal-preflight)
DESTINATION_DOC_EXISTED        = NO  (h4-resize-normal-preflight.md no existía)
CONCURRENT_WRITER              = NO sobre este worktree (creado por esta sesión)
```

Hay **29 worktrees** registrados en el repositorio (varias sesiones de agente en paralelo). No se
tocó ninguno: no se movieron HEADs, no se removieron checkouts ajenos, no se cambió de rama en
otro worktree. Los bindings de transición de `AGENTS.md` quedan intactos.

### 2.4 Dependencias: PRs e issues (estado verificado con `gh`, 2026-10-10)

| Referencia | Estado observado | HEAD remoto |
|---|---|---|
| PR #700 — auditoría H1–H5 | `MERGED` (2026-10-10T18:40:52Z) | `7d1362f3…` |
| PR #697 — auditoría impacto M4/M5 post-REVAL-1 | `OPEN` / `DRAFT` | `5a59308f…` |
| PR #675 — diseño EXP-M6 | `OPEN` / `DRAFT` | `f1415711c51a2ec483fd37f41a164044649747aa` |
| PR #681 — MATH-A | `MERGED` (2026-10-05T00:15:11Z) | `adc5cab9…` |
| PR #685 — MATH-B | `MERGED` (2026-10-05T16:50:04Z) | `83f05e40…` |
| Issue #667 — triage/colisión | `OPEN` | — |

El HEAD remoto de #675 (`f1415711…`) **coincide** con el SHA que declara el brief. El documento
M6 se leyó desde el worktree `E:/Skyclaw_Main_Sync/.worktrees/pr675-post700-design-recon`, que ya
estaba posicionado en ese SHA — **sin checkout, sin edición, sin push** sobre la rama de #675.

```text
PR675_CHANGED_BY_THIS_SLICE = NO
PR697_CHANGED_BY_THIS_SLICE = NO
PR700_CHANGED_BY_THIS_SLICE = NO
```

### 2.5 Base de la reconciliación M6

La base de #675 es `ee4a67ec…`, **previa** al merge de #700. Por eso los artefactos de la
auditoría H1–H5 **no existen** en la rama de #675 y se leyeron desde `main` (`647d2461…`), tal
como declara el propio §43.1 del diseño M6.

---

## 3. Evidencia de H4

Todo lo de esta sección es **evidencia publicada** en el repositorio. Nada se re-ejecutó acá
salvo lo marcado explícitamente como hallazgo propio (§3.5), que corrió sobre **campos sintéticos
en memoria**, no sobre el corpus.

### 3.1 Veredicto heredado (PR #700)

Fuente: `docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/`.

```text
H4_IMPLEMENTATION_DEFECT          = CONFIRMED
H4_M4_PRIMARY_IMPACT              = NUMERICAL_NOT_DECISIONAL
H4_M5_PRIMARY_IMPACT              = NUMERICAL_NOT_DECISIONAL
M4_M5_REVALIDATION_REQUIRED_BY_H4 = NO
```

`H4_IMPLEMENTATION_DEFECT = CONFIRMED` — la implementación histórica cuantiza cada canal del
vector normal a `uint8` **antes** de interpolar y renormalizar. Es el **hermano** que el fix #653
arregló en `resize_height` (modo `F`, float32) y dejó intacto en el normal.

Sesgo medido en la auditoría: normal plana `(0,0,1)` → `mean(nx) = −0.00392`; el contrafactual
float da `0.0`. Es el offset clásico de **−0.5 LSB** del round-trip `[-1,1] → uint8 → [-1,1]`.

### 3.2 Magnitud sintética (correctivo 2026-10-08, hallazgos F4/F5)

La sonda original tenía dos defectos metodológicos (amplitudes equivocadas; superficie
inconsistente por clip). Corregidos:

```text
H4_EXTERNAL_MAGNITUDE               = PARTIALLY_REPRODUCED
H4_RATIO_CLAIM_STATUS               = PARTIALLY_REPRODUCED
H4_ABSOLUTE_CLAIM_STATUS            = NOT_REPRODUCED
H4_ABSOLUTE_MAX_SAFE_ARM            = 5.385861122314052e-05
H4_EXTERNAL_ABSOLUTE_CLAIM          = 0.023
H4_MAX_ABS_DOWNSTREAM_DELTA_SAFE_ARM= 0.035339907940470365
H4_T_DELTA_RMSE                     = 0.02
H4_N_CASES_DELTA_GE_T_SAFE_ARM      = 1
H4_INVARIANTS_APPLICABLE_OK         = 51 / 51
```

Lectura honesta: los **ratios** adimensionales caen en o por encima de la banda 5–21× (hasta
82.12× en `c = 0.01`), pero el componente **absoluto** del claim (0.023) **no** se reproduce
(máximo medido 5.386e-05, 427× por debajo). **Ratio alto no implica absoluto reproducido.**

Además, sobre superficie consistente el máximo delta downstream es **0.0353 ≥ `T_DELTA_RMSE`
(0.02)**: la materialidad del **sintético** cambia de `NO` a `SÍ`. Esto **no** cambia M4/M5 —el
umbral gobierna el `delta_rmse` del corpus real, no del sintético— pero es una pieza de evidencia
que el preflight no puede ignorar.

### 3.3 Contrafactual sobre el corpus real (Fase C2) — `CONFIRMED`

Fuente: `docs/validation/.../corrective-20261008/evidence/h4-corpus-counterfactual.json`
(31 assets, una sola variable, `audit_only: true`, `thresholds_unchanged` con los 8 umbrales
registrados sin modificar).

Brazo **A** = `resize_normal` histórico (uint8). Brazo **B** = `resize_normal_float`
(audit-only, `phase_c2_h4_corpus_corrective.py::resize_normal_float`).

| Magnitud | A (uint8) | B (float) |
|---|---|---|
| Decisión M4 | `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT` | idéntica |
| `rules_old` C1/C2 | `true` / `true` | `true` / `true` |
| `rmse_self_median` | 0.0036591679 | 0.0036591679 |
| `rmse_auth_median` | 0.0392109705 | 0.0390915300 |
| `delta_rmse_median` | 0.0350631633 | 0.0350835389 |
| `abs_corr_auth_median` | 0.8391782269 | 0.8397329492 |
| `var_auth_median` | 0.7042200966 | 0.7051514260 |
| `auth_rmse_max_abs_change` | — | 0.0354254837 |
| `normal_rmse_old_vs_new_max` | — | 0.0041375177 |
| M5 `C1_lowmid_preserved` | `false` | `false` |
| M5 `excess_lowmid_nrmse` (mediana) | 0.5082023970 | 0.4468198246 |
| M5 `high_enrichment` (mediana) | 0.7541691252 | 0.8034395020 |
| M5 `excess_high_nrmse` (mediana) | 0.3350414404 | 0.3349134352 |

Cambio por asset en `auth_rmse`: mediana 8.07e-05, p90 4.95e-4, máximo 3.54e-2 (un único asset
de 31 supera 0.01). **Decisión y reglas idénticas.**

### 3.4 Lo que la evidencia **no** dice

Esta es la parte que el preflight existe para fijar:

- El veredicto `NUMERICAL_NOT_DECISIONAL` se midió sobre **métricas de M4/M5** (`delta_rmse`,
  `excess_lowmid_nrmse`, `high_enrichment`). **No** se midió sobre ninguna métrica de M6.
  `H4_NUMERICAL_IMPACT_ON_M6 = NOT_MEASURED`.
- El contrafactual C2 **no** reproduce la réplica direccional de M5 sobre `LEGACY_HELDOUT`
  (no hay artefacto de filas M5 en el repo). Limitación declarada, no ocultada.
- El contrafactual C2 **no** publica un censo de vectores degenerados (norma ≈ 0) por brazo.
  Ver §3.5: eso importa.
- `rmse_self_median` idéntico entre brazos es una propiedad **estructural** del script (el par
  SELF se calcula una sola vez por asset y ambos brazos leen la misma variable), no una prueba
  de aislamiento causal del sistema completo.

### 3.5 Hallazgo propio de este preflight — el resizer float ingenuo produce **vectores nulos**

`CONFIRMED` por ejecución sobre **campos sintéticos en memoria** (sin corpus, sin red). Este
slice **no** re-ejecutó el contrafactual; corrió la función real del repositorio y la variante
float del contrafactual sobre entradas adversariales.

**Resultado medido.** Campo periódico de período 2 en el canal X (`+x` / `−x` alternados, normal
unitaria `(±1, 0, 0)`), 128×128 → 64×64:

| Resizer | norma mínima | norma media | fracción no unitaria | píxeles con norma 0 |
|---|---|---|---|---|
| histórico uint8 | 1.000000 | 1.000000 | 0.0000 | 0 |
| float (contrafactual C2) | 0.000000 | 0.000977 | 0.9990 | **4092 / 4096** |

Caso límite exacto: dos columnas opuestas `(+1,0,0)` y `(−1,0,0)`, 8×2 → 8×1.

```text
histórico uint8 : out = (+0.57735, −0.57735, −0.57735)   norma = 1.000
float           : out = ( 0.0,      0.0,      0.0     )   norma = 0.000
```

**Mecanismo.** La renormalización vigente es `out / max(norma, 1e-12)`. Si el vector interpolado
se cancela, `norma < 1e-12`, el cociente no lo devuelve a norma unitaria: lo **escala por
`1/1e-12`** y publica un vector nulo (o de norma arbitrariamente chica). La guarda `1e-12` evita
la división por cero, **no** garantiza norma unitaria.

El camino histórico **no** exhibe el defecto en estos casos por una razón estructural: la
retícula de decodificación uint8 es `{(k − 127.5)/127.5}` con `k ∈ [0,255]`, que **no contiene
el 0** (el valor de menor magnitud es `±1/255 ≈ 0.00392`). La cuantización desplaza los valores
fuera del punto de cancelación exacta y, paradójicamente, mantiene la norma unitaria.

**Consecuencia para el diseño del fix.** Copiar literalmente el patrón de `resize_height` (que es
una primitiva **escalar**) **no alcanza**: no hay política de vector degenerado que heredar. El
fix debe declarar explícitamente qué se hace con un vector cuya norma tiende a cero. Esto se
especifica en §5.3 y es el motivo por el cual el brief exige no asumir que el port directo basta.

```text
NAIVE_FLOAT_PORT_IS_SUFFICIENT      = NO
DEGENERATE_VECTOR_POLICY_REQUIRED   = YES
DEGENERATE_VECTOR_POLICY            = SPECIFIED (DESIGN_PROPOSED, §5.3)
ZERO_VECTOR_CENSUS_ON_REAL_CORPUS   = NOT_MEASURED (prohibido por el brief; el C2 no lo publica)
```

`REMAINING_UNCERTAINTY`: los casos medidos son adversariales (contenido a Nyquist, normal en el
plano XY). No se midió la frecuencia de cancelación en el corpus real. El contrafactual C2 usó el
resizer float ingenuo sobre los 31 assets y las decisiones **no** cambiaron, lo que sugiere —sin
probarlo— que no hubo degeneración catastrófica en ese corpus. Eso **no** es un censo.

---

## 4. Implementación histórica

### 4.1 Código vigente

`sky_claw/local/native_parallax/research/authored_dataset.py:237-253`:

```python
def resize_normal(n: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    if n.shape[0] == size and n.shape[1] == size:
        return np.asarray(n, dtype=np.float64)
    channels = []
    for c in range(3):
        im = Image.fromarray((np.clip(n[..., c], -1.0, 1.0) * 127.5 + 127.5).astype(np.uint8), mode="L")
        channels.append(
            (np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64) - 127.5) / 127.5
        )
    out = np.stack(channels, axis=-1)
    length = np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-12)
    return np.asarray(out / length, dtype=np.float64)
```

Camino conceptual: `float → clip → escala a [0,255] → astype(uint8) [TRUNCA] → Pillow modo "L" →
bilineal → reescala a [-1,1] → renormaliza`.

**Dos cuantizaciones, no una:**

1. `astype(np.uint8)` sobre `[0,255]` — trunca hacia cero. `127.5 → 127` es el origen del sesgo
   `−0.5 LSB`.
2. El propio resize bilineal de Pillow sobre modo `L` (8 bits por canal).

**Hallazgo adicional (`CONFIRMED`, medido; alcance CORREGIDO en el cierre de contrato).** La
llamada usa `Image.fromarray(..., mode="L")` y la suite existente emite

```text
DeprecationWarning: 'mode' parameter is deprecated and will be removed in Pillow 13 (2026-10-15)
    im = Image.fromarray((np.clip(n[..., c], -1.0, 1.0) * 127.5 + 127.5).astype(np.uint8), mode="L")
```

**Alcance real de la deprecación** (verificado contra la documentación oficial de Pillow,
2026-10-10). El parámetro `mode` de `Image.fromarray` fue deprecado en **11.3.0** y en **12.0.0
fue parcialmente revertido**:

> «Using the `mode` parameter in `Image.fromarray()` was deprecated in Pillow 11.3.0. In Pillow
> 12.0.0, this was partially reverted, and now the **only functionality removed is when the mode
> changes data types**. Since pixel values do not contain information about palettes or color
> spaces, the parameter can still be used to place grayscale L mode data within a P mode image, or
> read RGB data as YCbCr for example. If omitted, the mode will be automatically determined from
> the object's shape and type.»
>
> — `docs/deprecations.rst`, sección «Image.fromarray mode parameter»
> (`.. deprecated:: 11.3.0` · `.. versionremoved:: 13.0.0`)

El uso que hace `resize_normal` es **`uint8` + `mode="L"`**: **no** cambia el tipo de dato (L es
gris de 8 bits, igual que `uint8`), así que **no pertenece a la funcionalidad removida**. Medido
en este slice con Pillow 11.3.0:

| Caso | Resultado medido |
|---|---|
| `uint8` + `mode="L"` (el que usa `resize_normal`) | funciona, modo `L`, sólo `DeprecationWarning` |
| `uint8` + `mode="F"` (sí cambia el tipo de dato) | `ValueError: not enough image data` |

**Conclusión corregida.** Migrar a la inferencia automática de modo (el patrón que ya usa
`resize_height`) es **deseable por claridad y compatibilidad futura**, pero **no se afirma que el
código actual dejará necesariamente de funcionar en Pillow 13**. La directiva
`versionremoved:: 13.0.0` sigue declarada, así que la migración es prudente; lo que no
corresponde es presentarla como una rotura inminente. El motivo principal para reescribir la
función sigue siendo la **cuantización**, no esta deprecación.

### 4.2 Hermano corregido — `resize_height` (`authored_dataset.py:224-234`)

```python
def resize_height(h: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    if h.shape[0] == size and h.shape[1] == size:
        return np.asarray(h, dtype=np.float64)
    im = Image.fromarray(np.asarray(np.clip(h, 0.0, 1.0), dtype=np.float32))
    out = im.resize((size, size), Image.Resampling.BILINEAR)
    return np.asarray(out, dtype=np.float64)
```

Patrón: `float32 → Pillow modo F → bilineal → float64`. Sin cuantización intermedia. Es el patrón
que #653 introdujo y que el brief propone como candidato.

**Diferencia estructural que impide el port directo:** `resize_height` es **escalar**; no tiene
norma que preservar ni vectores que puedan cancelarse. `resize_normal` sí. Ver §3.5.

### 4.3 Contrato real, medido (`CONFIRMED` por ejecución en este slice)

| Propiedad | Valor medido |
|---|---|
| Firma | `resize_normal(n: NDArray[np.float64], size: int) -> NDArray[np.float64]` |
| Camino identidad (`shape == (size, size)`) | devuelve **el mismo objeto**, sin copia, **sin renormalizar** |
| Norma garantizada en camino identidad | **NO**. Con entrada no unitaria (norma 1.0295630), la salida conserva 1.0295630 |
| Norma garantizada en camino resize | **NO universalmente** — ver §3.5 (vectores nulos con `norma < 1e-12`) |
| Norma garantizada con entrada suave | **SÍ** — el test existente `test_m3_normal_resize_renormalizes` la verifica |
| Sesgo medido en normal plana `(0,0,1)`, 16→8 | `mean(nx) = mean(ny) = −0.003921508320212736` |
| Referencia `−0.5 LSB` teórica | `−0.5/127.5 = −0.0039215686` (la diferencia es el efecto de la renormalización) |
| NaN/Inf publicados | El camino uint8 no puede producirlos |

### 4.4 Identidad del módulo (procedencia)

```text
MODULE_PATH      = sky_claw/local/native_parallax/research/authored_dataset.py
GIT_BLOB_SHA     = dff54d132bcaa111a13a59d838d28c7e2bfbde03
SHA256_RAW       = 4184cbada6885cf62ef98a95100a7b1921a881b8e2f7450f2889e9337befde8c
SHA256_LF        = 4184cbada6885cf62ef98a95100a7b1921a881b8e2f7450f2889e9337befde8c
HAS_CRLF         = NO (raw y canónico coinciden)
RESIZE_NORMAL_DEF_LINE    = 237
RESIZE_NORMAL_CALL_LINE   = 323
```

### 4.5 Call-sites de `load_asset` (inventario, no muestreo)

`grep -rn "load_asset" sky_claw/ tests/ --include=*.py` — sólo las rutas de Native Parallax
cuentan (los `_download_asset` de `tools_installer` son un símbolo homónimo ajeno):

| Archivo | Línea | Uso |
|---|---|---|
| `run_exp_m2.py` | 242, 327 | `load_asset(spec, resolution)` — sin convención forzada |
| `run_exp_m3.py` | 177, 339 | una sin convención; una con `SOLVER_NORMAL_CONVENTION` |
| `run_exp_m4.py` | 150, 193 | ambas con `SOLVER_NORMAL_CONVENTION` |
| `run_exp_m5.py` | 304 | con `SOLVER_NORMAL_CONVENTION` |
| `tests/test_native_parallax_exp_m3.py` | 411 | test, `tested_convention="OPENGL"` |
| `tests/test_native_parallax_spearman.py` | 497, 527, 563 | `monkeypatch` — **no** ejerce el resizer |
| `tests/test_native_parallax_m2_json.py` | 325 (comentario) | camino real por `load_asset` |

`resize_normal` **se invoca en un único lugar**: `authored_dataset.load_asset:323`. El único
consumidor directo adicional es `tests/test_native_parallax_exp_m2.py:96`.

```text
RESIZE_NORMAL_CALL_SITES_IN_PRODUCTION = 1 (load_asset)
RESIZE_NORMAL_CALL_SITES_IN_TESTS      = 1 (test_m3_normal_resize_renormalizes)
```

**Consecuencia:** el fix toca **una** función con **un** call-site de producción. El riesgo no
está en el sitio — está en los **consumidores gemelos** del resultado (§15, R-05).

---

## 5. Comportamiento corregido esperado

### 5.1 Transformación candidata (`DESIGN_PROPOSED`, no implementada)

```text
normal float64 [-1,1]³  →  clip por canal  →  Pillow modo F (float32)
                        →  bilinear explícito  →  float64
                        →  recombinar canales  →  política de degenerados  →  renormalizar
```

Propiedades que la transformación debe preservar (sujetas a validación en el slice de código):

1. Cada canal se interpola como float.
2. No hay conversión intermedia a `uint8`.
3. La política de interpolación es bilineal **explícita** (no un default implícito).
4. Los canales se recombinan en el orden original.
5. Se renormaliza el vector.
6. Se devuelve el tipo contratado (`float64`).
7. Se preserva la convención de normales.
8. No se intercambian canales ni se invierte Y.
9. No se modifica el height.
10. No se modifica el corpus original.

### 5.2 Lo que **no** alcanza

`NAIVE_FLOAT_PORT_IS_SUFFICIENT = NO` (§3.5). Dos defectos del port directo:

- **D-1 (degenerados).** El port directo **crea** vectores nulos donde el histórico no los tenía.
  Es un intercambio de defectos, no una corrección, si no se especifica la política.
- **D-2 (renormalización no garantizada).** `out / max(norma, 1e-12)` no garantiza norma unitaria
  cuando `norma < 1e-12`. La guarda es anti-`ZeroDivision`, no un invariante geométrico.

### 5.3 Política de vectores degenerados — `DESIGN_PROPOSED`

El brief prohíbe introducir una política nueva sin documentarla y justificar su compatibilidad, y
exige **distinguir un caso inválido de una normal física válida**. La especificación:

**Definición.** Sea `v` el vector interpolado en un píxel y `L = ‖v‖`. Se declara
`DEGENERATE_THRESHOLD` (a derivar en el slice de código, **no** en este documento) tal que:

```text
L >= DEGENERATE_THRESHOLD      → normal válida:  n_out = v / L
0 < L < DEGENERATE_THRESHOLD   → normal de dirección recuperable pero no confiable
L == 0  (o L < eps de máquina) → vector nulo: NO es una normal física
```

**Criterio de clasificación (obligatorio).** Un vector nulo **no** es una normal física: es la
cancelación de dos vecinos con direcciones opuestas, es decir un artefacto de la **reducción de
resolución**. No debe publicarse como normal, ni silenciosamente, ni con un valor de reemplazo
que oculte el evento.

**Decisión de contrato (D-06 CERRADO — Tech Lead, 2026-10-10): P-1 — FAIL-CLOSED.**

Cuando la interpolación cancela las direcciones, **no existe una normal resultante bien
definida**. Sustituirla automáticamente por `(0,0,1)` **inventaría una orientación** y podría
contaminar las métricas científicas. Por eso la política es fail-closed, con cuatro requisitos:

- **(D-06.a) Detección con criterio numérico justificado.** `DEGENERATE_THRESHOLD` se deriva de
  **teoría y controles sintéticos**, nunca del corpus (misma regla que los thresholds de M6,
  §18.3). Su valor no se fija en este documento.
- **(D-06.b) Registro del conteo.** `degenerate_normal_fraction` se publica por asset y por brazo.
- **(D-06.c) Rechazo explícito de la evaluación afectada.** El asset recibe el estado
  `NON_EVALUABLE` con su razón; **no** se calcula una métrica sobre un vector nulo.
- **(D-06.d) Ningún asset desaparece en silencio.** Un asset afectado **no** se borra del roster:
  queda con su estado explícito. **Distinguir el rechazo numérico de una exclusión silenciosa del
  experimento es un requisito, no un detalle de implementación.**

**Prohibido:** devolver el vector nulo como si fuera una normal; usar `1e-12` como si fuera un
invariante de norma; inventar `(0,0,1)` como reemplazo; introducir un `epsilon` silencioso; o
excluir el asset sin dejar constancia.

**Compatibilidad declarada.** El histórico **no** producía vectores nulos en los casos medidos
(§3.5). Por lo tanto la política fail-closed es un **cambio de comportamiento**, no preservación:
el slice de código debe medir `degenerate_normal_fraction` en **ambos** brazos y publicarlo. Si el
brazo histórico da 0 y el corregido da > 0, eso es un **hallazgo material** que el contrafactual
M6 debe absorber, no esconder.

```text
INVALID_VECTOR_POLICY = FAIL_CLOSED      (D-06 CERRADO — decisión de Tech Lead, 2026-10-10)
DEGENERATE_THRESHOLD  = DEFERRED (se deriva de teoría + sintético; prohibido derivarlo del corpus)
```

---

## 6. Invariantes matemáticos

### 6.1 Tabla de invariantes

Cada invariante se enuncia como propiedad del **mecanismo**, no como recordatorio de proceso
(`AGENTS.md`: la regla que más se viola es arreglar un hermano y no al otro).

| ID | Invariante | Estado | Cómo se verifica |
|---|---|---|---|
| I-01 | No hay conversión intermedia a `uint8` en el camino de resize | `DESIGN_PROPOSED` | Escaneo de fuente del anti-patrón `astype(np.uint8)` en el cuerpo de la función |
| I-02 | Norma ≈ 1 para entrada suave, con tolerancia declarada | `CONFIRMED` (histórico) / `DESIGN_PROPOSED` (nuevo) | Test de comportamiento |
| I-03 | Norma ≈ 1 **o** estado `NON_EVALUABLE` explícito — nunca un valor intermedio silencioso | `CONTRACT_CLOSED` (D-06, §5.3) | Test con entrada de cancelación |
| I-04 | Los canales no se permutan: `out[...,c]` proviene de `n[...,c]` | `DESIGN_PROPOSED` | Campo con un canal distinguible |
| I-05 | El canal Y no se invierte dentro del resizer | `DESIGN_PROPOSED` | Campo con `ny` asimétrico |
| I-06 | La convención declarada no se altera | `DESIGN_PROPOSED` | Integración con `load_asset` |
| I-07 | No se publican NaN ni Inf | `DESIGN_PROPOSED` | Entrada con NaN/Inf, fail-closed |
| I-08 | Campo constante unitario `c` se preserva exactamente | `DESIGN_PROPOSED` | Analíticamente exacto para bilineal |
| I-09 | Camino identidad: no-op para entradas válidas; validación explícita para inválidas | `CONTRACT_CLOSED` (D-07, §7.3) | Ver §7.3 |
| I-10 | Determinismo: dos llamadas idénticas dan el mismo array | `DESIGN_PROPOSED` | `np.array_equal` |
| I-11 | El height no se toca | `DESIGN_PROPOSED` | Integración |
| I-12 | `resize_height` conserva su implementación (modo F) | `CONFIRMED` (vigente) | Test de no-regresión |

### 6.2 Casos obligatorios a estudiar (brief §6)

Ninguno se resuelve por intuición. Cada uno es un caso de test o una pregunta abierta:

| Caso | Qué se espera | Estado |
|---|---|---|
| Normal plana `(0,0,1)` | Se preserva; **no** reaparece el sesgo `−0.5 LSB` | `DESIGN_PROPOSED` |
| Componentes negativas | Signo preservado, sin `abs()` | `DESIGN_PROPOSED` |
| Componentes cercanas a cero | No se colapsan a cero por la cuantización | `DESIGN_PROPOSED` |
| Valores sub-LSB | Sobreviven al procesamiento float | `DESIGN_PROPOSED` |
| Normales cercanas al plano XY | Caso crítico: es donde aparecen los nulos (§3.5) | `CONFIRMED` el riesgo / `DESIGN_PROPOSED` el tratamiento |
| Campos de alta frecuencia | Caso crítico: cancelación (§3.5) | `CONFIRMED` el riesgo |
| Vectores de longitud casi nula tras interpolar | Política explícita, no `1e-12` mudo | `DESIGN_PROPOSED` |
| Datos NaN/Inf | Fail-closed, declarado | `DESIGN_PROPOSED` |
| Resolución de identidad | Contrato a declarar (§7.3) | `UNRESOLVED` |
| Reducción de resolución | Camino principal (1024 → 512) | `CONFIRMED` que se ejecuta |
| Dimensiones no cuadradas | Ver contrato real: el corpus M3 es 100 % cuadrado | `CONFIRMED` (H2, #700) |
| Diferencias entre versiones de Pillow y NumPy | Pin actual `numpy>=1.26,<2.5`, `pillow>=10,<12` | `NOT_MEASURED` |

---

## 7. Contrato numérico del resizer

### 7.1 Entrada — contrato **real** (no inventado)

| Atributo | Valor vigente | Fuente |
|---|---|---|
| Tipo | `NDArray[np.float64]` | Firma |
| Forma | `(H, W, 3)` | Uso en `load_asset` |
| Rango de canales | `[-1, 1]` | `decode_normal_image`: `rgb/255*2 − 1` |
| Normalización previa | **Unitaria** — `decode_normal_image` renormaliza | `decode_normal_image` |
| Convención | `OPENGL` / `DIRECTX` / `UNKNOWN` | `MaterialSpec.declared_convention`; el flip lo aplica `apply_convention` **antes** del resize |
| Valores no finitos | **No definidos** — no hay guard en `resize_normal` | Vacío declarado |
| Dimensiones admitidas | **No definidas** en el resizer; el corpus M3 es 100 % cuadrado | `H2_BUG_CLASSIFICATION = NOT_PROVEN` (#700) |
| Resolución destino | `512` (primaria) | `PRIMARY_RESOLUTION = 512` (M6 §27) |

`decode_normal_image` sí acota el denominador con `max(length, 1e-12)` — misma guarda que el
resizer, con la misma limitación: con un píxel `(0,0,0)` en el bitmap, la salida queda nula.

### 7.2 Salida

| Atributo | Valor exigido |
|---|---|
| Tipo | `float64` |
| Forma | `(size, size, 3)` |
| Rango | `[-1, 1]` por canal |
| Norma | `≈ 1` **o** marca explícita de degenerado (§5.3) |
| Convención | Idéntica a la de entrada |
| Finitud | Sin NaN/Inf |

### 7.3 Vacíos identificados (`UNRESOLVED`)

Son vacíos **reales** del contrato vigente. El preflight los identifica; no los inventa ni los
cierra por conveniencia.

- **V-01 — Camino identidad (`CONTRACT_CLOSED` — D-07, Tech Lead 2026-10-10).**
  `resize_normal(n, size)` con `n.shape == (size, size)` devuelve `n` **sin renormalizar** y **sin
  copiar**. **Decisión: preservar el no-op para entradas válidas** — sin interpolación, sin
  renormalización y sin copia obligatoria. El histórico es consistente porque
  `decode_normal_image` ya renormalizó; cambiarlo en silencio alteraría el comportamiento de un
  camino que hoy **no** se ejecuta para el corpus (1024 → 512), pero **sí** para otras
  resoluciones.

  **Complemento obligatorio:** las entradas **no finitas o geométricamente inválidas** deben
  rechazarse mediante **validación explícita** (fail-closed), **no** corregirse mediante una
  renormalización silenciosa. El no-op se preserva **sólo para entradas válidas**; una entrada
  inválida no se "arregla", se rechaza.

  **Derivación de la tolerancia:** la tolerancia de norma del camino identidad se deriva mediante
  **pruebas sintéticas**, **no** se ajusta al corpus.

  ```text
  IDENTITY_PATH_CONTRACT         = PRESERVE_VALID_NOOP + EXPLICIT_VALIDATION_FOR_INVALID
  IDENTITY_PATH_TOLERANCE_SOURCE = SYNTHETIC_TESTS (prohibido ajustarla al corpus)
  ```

- **V-02 — Política de degenerados.** `CONTRACT_CLOSED` — ver §5.3 (fail-closed, D-06).
- **V-03 — Dimensiones no cuadradas.** No hay gate. El corpus M3 es cuadrado, pero
  `load_asset` reescala a `512²` antes del solver, así que un asset no cuadrado se vuelve cuadrado
  **antes** de llegar al resizer de la normal. La interacción con `freq_axes` (H2) queda fuera.
  `DEFERRED` (fuera del alcance del fix H4).
- **V-04 — No finitos.** No hay guard. `CONTRACT_CLOSED`: **sí**, fail-closed al estilo de
  `decode_height_image` modo F (`DatasetInvalidError` con NaN/Inf), coherente con el hermano y con
  D-07.
- **V-05 — Estabilidad entre versiones.** El pin admite `pillow>=10,<12`. El kernel bilineal de
  Pillow no está garantizado bit a bit entre versiones mayores. `NOT_MEASURED` (se registra la
  versión en la procedencia, §10).

---

## 8. Plan TDD RED→GREEN

Los tests se **diseñan** acá y se **implementan** en el slice de código. Ninguno existe todavía.

**Regla de oro (del brief, no negociable):** un test que pasa en **ambos** brazos (histórico y
corregido) **no** es evidencia de haber reparado H4. Todo test de la Familia A debe exhibir el
**rojo** contra la implementación vigente.

### 8.0 Separación: requisitos previos vs tests de implementación

Dos clases de trabajo distintas. Mezclarlas es el error que este cierre de contrato evita.

**(a) Requisitos previos a la implementación** — deben estar **cerrados antes** de escribir una
línea de producción. Son decisiones y contratos, no tests. No se ejecutan: se **declaran**.

| # | Requisito previo | Estado |
|---|---|---|
| R1 | Contrato numérico de entrada/salida (§7.1, §7.2) | `DESIGN_PROPOSED` |
| R2 | Política de vectores degenerados = fail-closed (D-06, §5.3) | `CONTRACT_CLOSED` |
| R3 | Contrato del camino identidad (D-07, §7.3) | `CONTRACT_CLOSED` |
| R4 | Validación explícita de entradas no finitas / inválidas (V-04) | `CONTRACT_CLOSED` |
| R5 | Contrato de procedencia machine-readable + gate (§10) | `DESIGN_PROPOSED` |
| R6 | Ancla de enumeración de primitivas de carga **diseñada** (Familia E) | `DESIGN_PROPOSED` |
| R7 | `DEGENERATE_THRESHOLD` derivado de teoría + sintético, **no** del corpus | `DEFERRED` al slice 2 |

**(b) Tests `RED→GREEN` que se ejecutan DURANTE la implementación** — Familias A–F de §8.1–§8.6.
Se escriben y se corren contra el baseline (**RED**, deben fallar) y luego contra el fix
(**GREEN**). Ninguno se ejecuta en este slice: no existen.

| Familia | Qué prueba | Se ejecuta |
|---|---|---|
| A — Cuantización | Que el defecto histórico es real | RED antes del fix |
| B — Contrato geométrico | Norma, degenerados, convención, finitud | Durante el fix |
| C — Interpolación | Oráculo **independiente** | Durante el fix |
| D — Integración | `load_asset`, rutas AUTH/SELF, 512/1024 | Durante el fix |
| E — Ancla de cobertura | Enumeración de primitivas de carga | Durante el fix |
| F — Mutantes | Discriminación de la suite | Después del GREEN |

**Regla de secuencia:** un requisito previo abierto **no** se cierra escribiendo un test que lo
asume. El test verifica la decisión; no la sustituye.

### Familia A — Error de cuantización (`RED` obligatorio contra el histórico)

| Test | Aserción | Rojo esperado contra el histórico |
|---|---|---|
| `test_normal_plana_permanece_plana` | `(0,0,1)` → `mean(nx) == 0` dentro de tolerancia | `mean(nx) = −0.0039215` ⇒ ROJO |
| `test_no_reaparece_el_sesgo_xy_del_uint8` | `mean(nx)` y `mean(ny)` sobre un campo simétrico ≈ 0 | sesgo `−0.5 LSB` ⇒ ROJO |
| `test_componentes_pequenas_sobreviven_al_float` | Amplitud `1/255` se preserva dentro de tolerancia | cuantización la colapsa ⇒ ROJO |
| `test_renormalizacion_no_introduce_artefactos` | La renormalización no cambia la dirección más allá de la tolerancia | depende del caso |

### Familia B — Contrato geométrico

| Test | Aserción |
|---|---|
| `test_norma_unitaria_bajo_tolerancia_derivada` | `‖out‖ ≈ 1` con tolerancia derivada de sintético, no elegida ni ajustada al corpus |
| `test_degenerados_no_se_publican_como_normal` | Vector nulo ⇒ estado `NON_EVALUABLE`, **no** `(0,0,0)` silencioso (§3.5) |
| `test_degenerado_no_inventa_orientacion` | Un degenerado **no** se reemplaza por `(0,0,1)` ni por ningún valor fabricado (D-06) |
| `test_asset_degenerado_no_desaparece_del_roster` | El asset queda con estado explícito; el conteo se preserva (D-06.d) |
| `test_rechazo_numerico_distinto_de_exclusion_silenciosa` | El estado `NON_EVALUABLE` es visible y distinguible de "asset ausente" (D-06.d) |
| `test_camino_identidad_preserva_el_noop` | Forma correcta + entrada válida ⇒ sin interpolación y sin renormalización (D-07) |
| `test_camino_identidad_rechaza_entrada_invalida` | No finitos / geométricamente inválidos ⇒ validación explícita, **no** renormalización silenciosa (D-07) |
| `test_convencion_no_se_altera` | El resizer no invierte Y ni cambia convención |
| `test_sin_permutacion_de_canales` | Un canal distinguible sale por el mismo índice |
| `test_sin_nan_inf_publicados` | Entrada con NaN/Inf ⇒ fail-closed |
| `test_entrada_invalida_comportamiento_definido` | Contrato explícito para entrada inválida |

### Familia C — Interpolación (oráculo independiente)

**No** se acepta como oráculo comparar la función nueva contra otra que reutilice el mismo
algoritmo y las mismas suposiciones.

| Caso | Resultado esperado | Tipo |
|---|---|---|
| Campo constante unitario | **Exacto** analíticamente | Sin tolerancia (o tolerancia de máquina) |
| Patrón sintético lineal | **Exacto** analíticamente (bilineal reproduce funciones afines) | Tolerancia de máquina |
| Campo analítico suavemente variable | Tolerancia numérica **declarada** | Tolerancia |
| Campo con discontinuidades | Requiere tolerancia; documentar el comportamiento en el borde | Tolerancia |
| Patrón adversarial de alta frecuencia | **No** se exige exactitud: se exige que **no** publique basura (nulos/NaN) | Contrato, no valor |

El oráculo independiente debe ser una implementación de referencia de la interpolación bilineal
**vectorizada y escrita aparte** (o la fórmula analítica cerrada), no una llamada a Pillow.

### Familia D — Integración

| Test | Aserción |
|---|---|
| `test_load_asset_invoca_el_resizer_declarado` | `load_asset` usa `resize_normal` (y el registro de procedencia lo refleja) |
| `test_ruta_auth_usa_el_resultado_corregido` | La normal de `AuthoredMaterial` es la del resizer corregido |
| `test_ruta_self_no_recibe_cambios_silenciosos` | `self_forward(h, bits=8)` no cambia |
| `test_resize_height_conserva_su_implementacion` | Modo F intacto (no-regresión de #653) |
| `test_filtros_de_manifest_no_se_alteran` | Los gates de manifest siguen igual |
| `test_resolucion_primaria_sigue_siendo_512` | `PRIMARY_RESOLUTION = 512` |
| `test_el_diseno_no_activa_1024` | `SECONDARY_1024 = DEFERRED` |

### Familia E — Ancla de cobertura (enumeración, no muestreo)

**Objetivo:** detectar cuando un cambio futuro añade una **nueva ruta de carga de normales** que
evita el resizer declarado.

Diseño: la familia de primitivas que producen `N_AUTH` en el camino AUTH de M6 se detecta por
**introspección/AST** y se congela, al estilo de `tests/test_ritual_dispatch.py` y
`tests/test_db_connection_invariant.py` (que **enumeran** en vez de muestrear). Un primitivo nuevo
rompe el ancla hasta que se le declare su procedencia.

El ancla **no** se limita a `load_asset`: debe enumerar también las rutas autorizadas que hoy
existen (`run_exp_m2` 242/327, `run_exp_m3` 177/339, `run_exp_m4` 150/193, `run_exp_m5` 304) para
que una ruta nueva **no** herede silenciosamente una excepción.

### Familia F — Mutaciones adversariales

Ver §9.

---

## 9. Mutantes adversariales

Cada mutante se aplica como **reemplazo textual exacto** sobre el código de producción, se corre
la suite y se revierte. El driver debe abortar si `count(old) != 1` y restaurar en `try/finally`.

**Regla de supervivencia:** un mutante **superviviente relevante** obliga a revisar el **oráculo**,
no a descartar el mutante.

| ID | Mutante | Qué reintroduce | Test que debe detectarlo |
|---|---|---|---|
| M-01 | Reintroducir `astype(np.uint8)` | Cuantización | `test_normal_plana_permanece_plana` (Familia A) |
| M-02 | Omitir la renormalización | Norma ≠ 1 | `test_norma_unitaria_bajo_tolerancia_derivada` (B) |
| M-03 | Invertir el canal Y (`out[...,1] = -out[...,1]`) | Flip indebido | `test_convencion_no_se_altera` (B) |
| M-04 | Intercambiar X/Y (`out[...,[0,1]] = out[...,[1,0]]`) | Permutación | `test_sin_permutacion_de_canales` (B) |
| M-05 | Convertir a modo `RGB` en vez de `F` | Ruta no declarada | Escaneo de fuente + `test_load_asset_invoca_el_resizer_declarado` (D) |
| M-06 | Sumar una constante de sesgo | Sesgo `−0.5 LSB` | `test_no_reaparece_el_sesgo_xy_del_uint8` (A) |
| M-07 | Modificar el height en la misma función | Efecto colateral | `test_resize_height_conserva_su_implementacion` (D) |
| M-08 | Usar un resizer no declarado (p. ej. `BICUBIC`) | Política implícita | Test de política de interpolación explícita (C) |
| M-09 | Devolver el vector nulo sin marca | Degenerado silencioso | `test_degenerados_no_se_publican_como_normal` (B) |
| M-10 | Quitar la guarda `max(..., 1e-12)` | División por cero | `test_sin_nan_inf_publicados` (B) |

**Nota sobre M-10:** quitar la guarda **debe** romper algo. Si ningún test falla, la guarda no
está cubierta. Pero cuidado con la lección de §3.5: la guarda **tampoco** garantiza norma
unitaria. Cubrir ambos hechos.

---

## 10. Procedencia machine-readable

### 10.1 Contrato propuesto (`DESIGN_PROPOSED`)

El JSON de procedencia del resizer debe distinguir inequívocamente `HISTORICAL_UINT8` de
`CANONICAL_FLOAT`. Nombres candidatos de contrato — **no** autorización para cambiar artefactos
históricos.

```text
resize_normal_impl          : "HISTORICAL_UINT8" | "CANONICAL_FLOAT"
resize_normal_impl_version  : entero de esquema (arranca en 1)
code_git_sha                : checkout real de la corrida (40 hex)
module_path                 : sky_claw/local/native_parallax/research/authored_dataset.py
module_blob_sha             : dff54d13…  (git blob)
module_sha256_lf            : 4184cbad…  (canónico LF, independiente del checkout)
algorithm                   : "BILINEAR"
intermediate_dtype          : "uint8" | "float32"
renormalization_policy      : "UNIT_NORM_CLAMP_1E12" | <política nueva declarada>
degenerate_policy           : "NONE" | "FAIL_CLOSED" | "DECLARED_REPLACEMENT"
degenerate_threshold        : número o null
normal_convention           : "OPENGL" | "DIRECTX"
input_resolution            : [H, W]
target_resolution           : entero
input_identity              : sha256 del bitmap de entrada (o del manifest que lo identifica)
manifest_id                 : identificador del manifest
manifest_sha256_lf          : sha256 canónico LF del manifest
spec_version                : entero de esquema
python_version / numpy_version / pillow_version
```

### 10.2 Distinción obligatoria de hashes

| Objeto | Hash | Nota |
|---|---|---|
| Código del resizer | `module_sha256_lf` + `module_blob_sha` | Identidad del **código** |
| Datos de entrada | `input_identity` (sha256 del bitmap o del manifest) | Identidad de los **datos** |
| Manifest | `manifest_sha256_lf` | Identidad del **corpus** |

`MODULE_HASH != DATA_HASH` — no se confunden. Una **ruta local** (p. ej. `E:\SkyClaw_…`) **no**
reemplaza un hash: no es identidad, es ubicación.

### 10.3 Gate

```text
Si se pretende consumir N_AUTH sin procedencia válida del resizer
⇒ la operación científica DEBE fallar de manera cerrada.
```

**Componente donde se implementa:** el runner de M6 (ETAPA B+), en el punto donde hoy §33.1
exige `git_sha`, `m6_prereg_freeze_sha` y los SHA256 de módulos. Se añade la clave
`resize_normal_impl` como **input del operador** (nunca heurística de runtime), y la ausencia de
la clave ⇒ `STOP_M6_EXECUTION`.

**No se modifica aún el esquema de salida de M6** (§43.10.1 ya lo especifica como condición; este
preflight no lo implementa).

`PROVENANCE_CONTRACT_SPECIFIED = YES` · `PROVENANCE_GATE_IMPLEMENTED = NO` (`DEFERRED`).

---

## 11. Contrafactual de dos brazos

### 11.1 Diseño

- **Brazo A:** `resize_normal` histórico (`HISTORICAL_UINT8`).
- **Brazo B:** `resize_normal` float corregido (`CANONICAL_FLOAT`).
- **Unidad estadística primaria:** asset.
- **Relación:** pareada (mismo asset en ambos brazos).

### 11.2 Variables que deben permanecer fijas

| Variable | Valor congelado |
|---|---|
| Entrada normal | Idéntica (mismo bitmap, mismo `decode_normal_image`) |
| Height | Idéntico (mismo `resize_height`, modo F) |
| Hashes | Idénticos (manifest + bitmaps verificados) |
| Manifest | Idéntico (`manifest_sha256_lf` congelado) |
| Convención | Idéntica (`SOLVER_NORMAL_CONVENTION`) |
| Resolución | Idéntica (512) |
| Solver | Idéntico (`integrate_periodic` / Frankot–Chellappa de M4) |
| Modelos | Idénticos (`M0`, `M2`, `M3`, `M4` de §16) |
| Regla de evaluación | Idéntica (`OracleOnly.fit_global_scale` + `OracleOnly.evaluate`) |
| Thresholds | Congelados **si existen**; hoy `THRESHOLDS_FROZEN = NO` |

### 11.3 Intervención única permitida

```text
single_variable = resize_normal_impl
```

Precedente verificado: el contrafactual C2 de #700 cumplió exactamente esta propiedad
(`single_variable: "resize_normal (uint8 requantize -> float bilinear + renormalize)"`) y su
aislamiento está probado **estructuralmente** (`phase_c2_h4_corpus_corrective.py::m4_row`: el par
SELF se calcula **una sola vez por asset** y ambos brazos leen la misma variable).

### 11.4 Dependencias de instrumentación

El contrafactual M6 **no** puede ejecutarse con lo que existe hoy:

| Dependencia | Estado |
|---|---|
| Runner M6 (ETAPA B+) | **NO EXISTE** — `M6_IMPLEMENTATION_BLOCKED = YES` |
| Módulo de matemática pura de M6 (§32-B) | **NO EXISTE** |
| `PAIR_EXCESS_ENERGY` / `RECOVERY_FRACTION` instrumentados | **NO EXISTEN** |
| Gate del denominador (`G`, `NUMERICAL_ENERGY_FLOOR`) | **NO DERIVADOS** — `THRESHOLDS_FROZEN = NO` |
| JSON de procedencia con `resize_normal_impl` | **NO EXISTE** (§10) |
| Primitiva `resize_normal` corregida | **NO EXISTE** |

**El brief prohíbe implementar M6 para poder ejecutar este contrafactual.** Por lo tanto el
protocolo queda **especificado y bloqueado**, no ejecutado.

### 11.5 Esquema de artefactos futuro (`DESIGN_PROPOSED`)

```text
{
  "phase": "M6_H4_RESIZE_NORMAL_TWO_ARM_COUNTERFACTUAL",
  "audit_only": true,
  "single_variable": "resize_normal_impl",
  "arms": { "A": "HISTORICAL_UINT8", "B": "CANONICAL_FLOAT" },
  "frozen": { git_sha, manifest_sha256_lf, resolution, convention, thresholds… },
  "rows": [ { asset, arm, E_AUTH, E_SELF, E_MODEL_j, PAIR_EXCESS_ENERGY,
              RECOVERY_FRACTION_j, denominator_state, NONINTEGRABLE_FRACTION,
              DC_SLOPE_FRACTION, identifiable, registration_state,
              degenerate_normal_fraction, provenance } ],
  "cohort_medians": { A: …, B: … },
  "decision": { A: …, B: …, changed: bool },
  "execution_status": "NOT_EXECUTED"
}
```

### 11.6 Estado

```text
M6_CONTRAFACTUAL_PROTOCOL = SPECIFIED
M6_CONTRAFACTUAL_EXECUTION = BLOCKED (requiere implementación de M6, prohibida)
M6_METRICS_MEASURED       = NO
```

---

## 12. Métricas M6 y estados no evaluables

Para cada asset y cada brazo, el protocolo debe contemplar:

| Métrica | Origen (M6) | Estado esperado |
|---|---|---|
| `E_AUTH` | §17.2 | Siempre |
| `E_SELF` | §17.2 | Siempre |
| `E_MODEL_j` (j ∈ M0, M2, M3, M4) | §16 | Siempre |
| `PAIR_EXCESS_ENERGY = E_AUTH − E_SELF` | §17.3 | **Signo real**, sin `abs`, sin clamp |
| `RECOVERY_FRACTION_j = (E_AUTH − E_MODEL_j)/(E_AUTH − E_SELF)` | §17.3 | Sólo si el gate del denominador pasa |
| Estado del gate del denominador | §18.1 | `NON_POSITIVE_PAIR_EXCESS` / `TOO_SMALL_PAIR_EXCESS` / `EVALUABLE` |
| `NONINTEGRABLE_FRACTION` | §7.2 | Sobre `g_N` |
| `DC_SLOPE_FRACTION` | §5.5 | Sobre `g_N` |
| Estado de identificabilidad | §22 | Explícito |
| Estado de registration | §13 | Incluye `REGISTRATION_AMBIGUOUS` |
| `degenerate_normal_fraction` | **Nuevo** (§5.3) | Obligatorio por brazo |
| Metadatos de procedencia | §33.1 + §10 | Obligatorio |

### 12.1 Sensibilidad de `RECOVERY_FRACTION` — cualificación obligatoria

```text
R_j = (E_AUTH − E_MODEL_j) / (E_AUTH − E_SELF)
∂R/∂E_AUTH = (E_MODEL − E_SELF) / (E_AUTH − E_SELF)²     (otras energías fijas)
```

**Prohibido afirmar que esa derivada es siempre distinta de cero.** Se anula cuando
`E_MODEL = E_SELF`, siempre que el denominador sea válido. Y `E_MODEL_j` **también** se mueve
cuando cambia `N_AUTH`, porque los modelos se construyen sobre el campo derivado de AUTH (§16,
§17.2). Lo demostrado es la **dependencia causal**; la **magnitud y el signo** quedan
`NOT_MEASURED`.

```text
RECOVERY_FRACTION_SENSITIVITY       = QUALIFIED_CAUSAL_DEPENDENCY_NOT_QUANTIFIED
RECOVERY_FRACTION_FIRST_ORDER_CLAIM = RETIRADO
H4_NUMERICAL_IMPACT_ON_M6           = NOT_MEASURED
```

**Prohibido afirmar que H4 necesariamente aumenta `E_AUTH`.** La dirección y magnitud del efecto
sobre M6 no están demostradas.

### 12.2 Assets no evaluables

Un asset en `NON_POSITIVE_PAIR_EXCESS` o `TOO_SMALL_PAIR_EXCESS` recibe
`RECOVERY_FRACTION_NOT_EVALUABLE` con su razón, y su `R_j` **no** entra a las medianas de la
cohorte. El asset **no** se excluye del resto de métricas (mismo tratamiento que `ENERGY_GATE` de
M5: marca la métrica, no el asset). Un resultado no evaluable **no** desaparece de la muestra: se
cuenta y se reporta.

---

## 13. Prevención de leakage

Definido **antes** de cualquier corrida:

1. **Unidad estadística primaria:** asset.
2. **Relación pareada** entre brazos, sin excepciones.
3. **Cohortes usables:** Cohort A (31 assets del manifest M3 congelado). `LEGACY_HELDOUT` **no**
   se usa para derivar ni para validar parámetros.
4. **Artefactos históricos:** inmutables; se referencian por SHA, no se reescriben.
5. **Condiciones congeladas:** manifest, resolución, convención, solver, modelos, reglas.
6. **Resultados no evaluables:** se registran con su razón (§12.2), nunca se borran.
7. **Selección de assets:** el roster se verifica por **identidad**, no por conteo. Precedente
   directo: `check_roster_identity` de `phase_c2_h4_corpus_corrective.py` exige las cuatro
   condiciones (`roster_count_match`, `roster_identity_match`,
   `historical_roster_digest_matches_frozen_sha`, `m3_manifest_sha256_matches_frozen`) y es
   fail-closed.
8. **Resultados negativos:** se conservan. Un `R_j < 0` es información, no un bug.
9. **Prohibido el ajuste retrospectivo de thresholds.** Si un bug exige cambiar un threshold ⇒
   STOP: se corrige el código y se re-emite el freeze (M6 §32).
10. **Exploratorio ≠ confirmatorio.** Una prueba exploratoria no se reutiliza como confirmación
    independiente.

```text
LEGACY_HELDOUT_USED_FOR_THRESHOLDS = NO
LEGACY_HELDOUT_IS_INDEPENDENT_VALIDATION = NO
LEGACY_HELDOUT_PERMITTED_USE = DESCRIPTIVE_DIRECTIONAL_STABILITY_ONLY
COHORT_A_COMPOSITION_ALTERED = NO
REAL_CORPUS_ACCESSED = NO
```

---

## 14. Contratos M6 preservados

Ninguno de los contratos de M6-A.1 se modifica. Verificados contra el texto vigente del diseño
(§43.7 del documento de #675):

```text
p = −nx / (sx · max(nz, nz_floor))                              (§5.1)   PRESERVED
q = −ny / (sy · max(nz, nz_floor))                              (§5.1)   PRESERVED
curl_z = ∂q/∂x − ∂p/∂y                                          (§7.5)   PRESERVED
HODGE_AS_RECONSTRUCTION = NO · HODGE_AS_DIAGNOSTIC = YES        (§8)     PRESERVED
REGISTRATION_PRIMARY = YES · FULL_INTEGER_TORUS_EXHAUSTIVE = YES (§12.4) PRESERVED
SPECTRAL_TRANSFER_PRIMARY = YES · SPECTRAL_TRANSFER_DOF = 1     (§14)    PRESERVED
rho_ref = 32 ciclos/tile                                        (§14.4)  PRESERVED
low_spectral_limit = G_β(0⁺) = 2^(+β/2)                         (§14.2)  PRESERVED
TOTAL_PRIMARY_FREE_PARAMETERS = 3                               (§31)    PRESERVED
PRIMARY_RESOLUTION = 512 · SECONDARY_1024 = DEFERRED            (§27)    PRESERVED
PAIR_EXCESS_ENERGY con signo real (sin abs, sin clamp)          (§17.3)  PRESERVED
NON_POSITIVE_PAIR_EXCESS · TOO_SMALL_PAIR_EXCESS · EVALUABLE    (§18.1)  PRESERVED
PAIR_EXCESS_ENERGY > max( G · E_SELF , NUMERICAL_ENERGY_FLOOR ) (§18.2)  PRESERVED
```

```text
G_VALUE                = NOT_FIXED_HERE
NUMERICAL_ENERGY_FLOOR = NOT_FIXED_HERE
THRESHOLDS_FROZEN      = NO
```

**Argumento de preservación (por qué el fix no exige cambiar contratos).** El fix cambia **cómo
se obtiene `N_AUTH`**, no la matemática que lo consume. La descomposición de Hodge, el solver, el
registro y la transferencia espectral se definen sobre **cualquier** campo (§43.6 del diseño M6:
«la proyección se define sobre cualquier campo, sea cual sea su procedencia»). Lo que cambia es
el **valor numérico** de `N_AUTH` y por lo tanto de las métricas — no las definiciones ni los
contratos. `M6_CONTRACTS_CHANGED_BY_H4_FIX = NO` (`DESIGN_PROPOSED`, verificado contra el texto
vigente; su verificación ejecutable pertenece al slice de código).

---

## 15. Riesgos y modos de falla

Formato exigido: `RISK → FAILURE_MODE → DETECTION → REQUIRED_GATE → REMAINING_UNCERTAINTY`.

| ID | RISK | FAILURE_MODE | DETECTION | REQUIRED_GATE | REMAINING_UNCERTAINTY |
|---|---|---|---|---|---|
| R-01 | El modo `F` corrige la cuantización pero conserva otros sesgos | Se cree corregido y queda un sesgo estructural | Test de normal plana y de campo simétrico (Familia A) | `test_normal_plana_permanece_plana` en verde **y** rojo contra el histórico | No se auditó todo sesgo posible del kernel bilineal de Pillow |
| R-02 | La renormalización con promedio vectorial ≈ 0 produce resultados inesperados | Vectores nulos publicados como normales | **Medido en §3.5**: 4092/4096 píxeles nulos en campo alternado | Política P-1/P-2 declarada + `degenerate_normal_fraction` por brazo | Frecuencia en el corpus real: `NOT_MEASURED` |
| R-03 | Una normal corregida cambia resultados históricos si se reejecutan experimentos | Deriva silenciosa de M2–M5 | Contrafactual C2 (#700): decisión idéntica; `thresholds_unchanged` | No reejecutar M2–M5 con el resizer nuevo sin declararlo; procedencia en el JSON | El contrafactual C2 no cubre `LEGACY_HELDOUT` de M5 |
| R-04 | Compatibilidad OpenGL/DirectX rota | Normales con Y invertida | Test de convención (Familia B) + `apply_convention` intacto | `test_convencion_no_se_altera` | Ninguna identificada |
| R-05 | El cambio del resizer afecta indirectamente los fits de registration | `δ*` cambia de bin; `β*` se desplaza | Contrafactual de dos brazos con `registration_state` y `β` reportados | Contrafactual M6 (§11) — **BLOCKED** | No medible sin M6 |
| R-06 | Un cambio de implementación exige nueva identidad de procedencia aunque el delta sea chico | `N_AUTH` sin procedencia declarada | Gate de procedencia (§10.3) | `resize_normal_impl` obligatorio ⇒ si falta, `STOP_M6_EXECUTION` | El esquema de M6 no está implementado |
| R-07 | Variaciones dependientes de versiones de Pillow/NumPy | Resultados no reproducibles entre entornos | Registro de versiones en el JSON; pin `pillow>=10,<12` | Versiones en la procedencia + test de estabilidad | `NOT_MEASURED` — el kernel bilineal no está garantizado bit a bit entre mayores |
| R-08 | El costo de CPU y memoria cambia | Presupuesto de corrida excedido | Medición de tiempo y memoria por asset, ambos brazos | Medir, no presupuestar a ojo | Sin medición: el brief prohíbe inventar presupuestos |
| R-09 | El esquema JSON no distingue resizers si sólo registra el SHA general del repo | Dos corridas indistinguibles | Gate de procedencia con `resize_normal_impl` + `module_sha256_lf` | §10.3 | Depende del slice de M6 |
| R-10 | Un contrafactual no preregistrado induce tuning retrospectivo | Thresholds ajustados a los datos | §13: freeze antes de correr; thresholds no se leen ni se retunean | `thresholds_unchanged` en el JSON | `THRESHOLDS_FROZEN = NO` todavía |
| R-11 | Un resultado no evaluable desaparece de la muestra | Sesgo de selección | §12.2: conteo por estado obligatorio | Reporte del conteo por estado | Ninguna |
| R-12 | La evidencia M4/M5 se confunde con evidencia M6 | Se hereda un veredicto que no aplica | §3.4 y §12.1: `NUMERICAL_NOT_DECISIONAL` es de M4/M5 | `H4_NUMERICAL_IMPACT_ON_M6 = NOT_MEASURED` explícito | Ninguna — es una regla de lectura |
| R-13 | El port directo del patrón de `resize_height` se cree suficiente | Se implementa y **crea** un defecto nuevo (nulos) | **§3.5** — hallazgo propio de este preflight | Política de degenerados obligatoria antes de escribir código | Magnitud real en el corpus: `NOT_MEASURED` |
| R-14 | El parámetro `mode="L"` de `Image.fromarray` está deprecado | Migración pendiente; eventual rotura si la remoción de 13.0.0 alcanzara también el uso que **no** cambia tipo de dato | `DeprecationWarning` **medido** (§4.1) | Migrar a la inferencia automática de modo en el slice 2 | **Alcance corregido (§4.1):** `uint8` + `mode="L"` **no** cambia el tipo de dato ⇒ queda fuera de la funcionalidad removida según la documentación de Pillow 12. Pero la directiva `versionremoved:: 13.0.0` sigue declarada ⇒ **no** se puede descartar del todo |

Ninguna condición se clasifica como segura sin evidencia.

---

## 16. Separación de slices

Tres trabajos distintos. **No se mezclan en un solo PR.**

### Slice 1 — Preflight H4

- **Alcance:** auditoría, especificación, tests propuestos, procedencia, riesgos, plan de
  contrafactual, condiciones de autorización.
- **Write-set:** un documento nuevo.
- **Estado:** `COMPLETE_WITH_FINDINGS`.

### Slice 1b — Cierre de contrato del preflight (`H4_PREFLIGHT_CONTRACT_CLOSURE`)

- **Alcance:** fijar D-06 (**fail-closed**) y D-07 (**no-op válido + validación explícita**),
  corregir el alcance de la afirmación sobre Pillow 13, y separar los **requisitos previos** de los
  **tests `RED→GREEN`** de la implementación (§8.0).
- **Write-set:** el **mismo** documento de §1.4, sobre la **misma** rama y worktree. Sin código,
  sin tests, sin corpus.
- **Estado:** `COMPLETE` (ver §21).

### Slice 2 — Corrección de código H4 (**no autorizado**)

- **Alcance candidato:** `resize_normal`, tests focales propios, registro de procedencia
  estrictamente necesario, política de degenerados. Sin alterar resultados históricos.
- **Base obligatoria:** un **nuevo HEAD de `main`**, no la rama de #675 ni la de este preflight.
- **Estado:** `BLOCKED` hasta que §17 se satisfaga.

### Slice 3 — Contrafactual científico M6 (**no autorizado**)

- **Alcance candidato:** comparación controlada de resizers, métricas M6 completas, procedencia
  machine-readable, artefactos de comparación, evidencia de sensibilidad.
- **Prerrequisito:** primitivas correctas + contratos verificables + instrumentación científica
  autorizada (es decir, M6 implementado — hoy **prohibido**).
- **Estado:** `BLOCKED`.

**Si el contrafactual M6 necesita instrumentación inexistente, primero se separa el trabajo
requerido y se conserva bloqueada su ejecución.** Eso es exactamente lo que ocurre hoy (§11.4).

---

## 17. Evidencia necesaria para autorizar código

El slice 2 se autoriza cuando **todo** lo siguiente es `YES`:

| # | Condición | Estado actual |
|---|---|---|
| 1 | Defecto H4 reproducido desde la fuente y anclado con un test `RED` | `DESIGN_PROPOSED` (el test no existe — pertenece a la **implementación**, §8.0.b) |
| 2 | Contrato numérico cerrado, incluidos los vacíos V-01…V-05 | `CONTRACT_CLOSED` para V-01, V-02, V-04; `DEFERRED` V-03; `NOT_MEASURED` V-05 |
| 3 | Política de vectores degenerados elegida y declarada | `CONTRACT_CLOSED` — **fail-closed** (D-06, §5.3) |
| 4 | Plan de mutantes con el test que detecta cada uno | `DESIGN_PROPOSED` (§9) |
| 5 | Ancla de enumeración de primitivas de carga **diseñada** | `DESIGN_PROPOSED` (Familia E) |
| 6 | Contrato de procedencia especificado con gate fail-closed | `DESIGN_PROPOSED` (§10) |
| 7 | `resize_height` con test de no-regresión | `DESIGN_PROPOSED` (pertenece a la implementación) |
| 8 | `DEGENERATE_THRESHOLD` derivado de teoría + sintético (no del corpus) | `DEFERRED` al slice 2 (§8.0.a R7) |
| 9 | Conformidad del Tech Lead con el alcance del slice 2 | **PENDIENTE** (el veredicto de §21 recomienda un PR independiente y acotado) |

```text
H4_CODE_IMPLEMENTATION_AUTHORIZED = NO
```

**Un check verde de CI general no valida una implementación H4 que todavía no existe.**

---

## 18. Dependencias sin resolver

| ID | Dependencia | Estado | Bloquea a |
|---|---|---|---|
| D-01 | Runner M6 (ETAPA B+) | **NO EXISTE** | Slice 3 |
| D-02 | Matemática pura de M6 (§32-B) | **NO EXISTE** | Slice 3 |
| D-03 | `PAIR_EXCESS_ENERGY` / `RECOVERY_FRACTION` instrumentados | **NO EXISTEN** | Slice 3 |
| D-04 | Thresholds `G`, `NUMERICAL_ENERGY_FLOOR`, `T_R`, `T_N`, `T_AMBIG` | **NO DERIVADOS** (`THRESHOLDS_FROZEN = NO`) | Slice 3 |
| D-05 | JSON de procedencia con `resize_normal_impl` | **NO EXISTE** | Slice 3 |
| D-06 | Política de vectores degenerados | **CERRADO** en este slice — fail-closed (§5.3) | — |
| D-07 | Contrato del camino identidad (V-01) | **CERRADO** en este slice — no-op válido + validación explícita (§7.3) | — |
| D-07b | Valor numérico de `DEGENERATE_THRESHOLD` | **PENDIENTE** — se deriva de teoría + controles sintéticos, nunca del corpus | Slice 2 |
| D-08 | Issue #667 (triage de colisión) | `OPEN` | Declarativo |
| D-09 | Correcciones documentales de #675 (§14 del brief) | **PENDIENTES en la rama de #675** | Documental, no de código |
| D-10 | Defecto Markdown preexistente §37.3 de #675 (delta con barras sin escapar) | **DECLARADO, no corregido** | Documental |

### 18.1 Dependencia documental sobre #675 — **no se edita #675**

El documento M6 de #675 tiene correcciones **pendientes de aplicar en su propia rama**. Este
preflight las **registra** y **no** las aplica:

1. La frase **«H4 infla `E_AUTH`»** (línea 2097 de
   `exp-m6-pair-mismatch-decomposition.md`) debe cualificarse como **posible alteración no
   cuantificada**. La cualificación ya existe en prosa en §43.6.2 y §43.14.c, pero la frase
   asertiva sigue en el texto.
2. La **derivada parcial** (línea 2099) afirma `∂R/∂E_AUTH = (E_MODEL − E_SELF)/(E_AUTH −
   E_SELF)² ≠ 0`. **No es universalmente distinta de cero**: se anula cuando
   `E_MODEL = E_SELF`, con denominador válido.
3. El **defecto Markdown preexistente** de §37.3 (línea 1719): `|δ|` **sin escapar** dentro de una
   celda, que parte la fila en 5 columnas donde la tabla declara 3. Ya está declarado en §43.14.f
   del propio documento como `DECLARED_NOT_CORRECTED_OUT_OF_SCOPE`. **No se corrige desde este
   worktree.**

```text
PR675_EDITED_BY_THIS_SLICE      = NO
PENDING_DOC_CORRECTIONS_ON_675  = 3 (cualificar «infla», cualificar «≠ 0», tabla §37.3)
NEXT_DOC_SLICE_OWNS_THEM        = YES (rama de #675, no esta)
```

**Nota de integridad:** el checker de tablas del repositorio
(`tests/test_native_parallax_pr700_corrective_docs_invariants.py::test_g_…`) **prohíbe** `\|`
escapado dentro de una celda, así que la corrección de la tabla §37.3 exige **reescribir prosa**
de una sección histórica, no un arreglo mecánico de dos caracteres. Eso es trabajo del slice
documental de #675.

---

## 19. Recomendación Tech Lead

### P1. ¿Está suficientemente especificada la reparación float de `resize_normal`?

**Sí, tras el cierre de contrato.** La transformación candidata está especificada (§5.1), los
contratos M6 están verificados como preservados (§14) y los dos puntos que faltaban quedaron
cerrados: la política de vectores degenerados (**fail-closed**, D-06, §5.3) y el contrato del
camino identidad (**no-op válido + validación explícita**, D-07, §7.3). Sin esos dos, el port
directo **creaba** un defecto nuevo (§3.5, R-13). Queda `DEFERRED` al slice 2 el **valor
numérico** de `DEGENERATE_THRESHOLD`, que por regla se deriva de teoría y controles sintéticos —
nunca del corpus.

### P2. ¿Qué comportamientos exactos hay que preservar?

1. La **renormalización** existe y se aplica (hoy con la guarda `1e-12`; el fix no puede quitarla
   sin declarar el reemplazo).
2. La **convención** de normales no se altera (el flip lo hace `apply_convention` **antes**).
3. El **orden de canales** no cambia.
4. `resize_height` **no** se toca (patrón de #653 intacto).
5. El **height** no se toca desde `resize_normal`.
6. La **resolución primaria sigue siendo 512**; 1024 sigue `DEFERRED`.
7. Los **filtros de manifest** no se alteran.
8. La ruta **SELF no** recibe cambios silenciosos.

### P3. ¿Qué casos `RED` demostrarán el defecto histórico?

Los de la Familia A (§8): normal plana `(0,0,1)` con `mean(nx) = −0.0039215` (debe dar 0); sesgo
XY del camino uint8 sobre campo simétrico; componentes de amplitud `1/255` que la cuantización
colapsa. Cada uno **debe** fallar contra la implementación vigente. Un test que pase en ambos
brazos no es evidencia de nada.

### P4. ¿Qué pruebas impedirán regresiones geométricas y de datos?

Familia B (norma, degenerados con estado `NON_EVALUABLE`, **ausencia de orientación inventada**,
**el asset no desaparece del roster**, **rechazo numérico distinguible de exclusión silenciosa**,
camino identidad **no-op** y su **validación explícita** de entradas inválidas, convención,
permutación, NaN/Inf), Familia C (oráculo de interpolación **independiente**, no una segunda
llamada al mismo algoritmo), Familia D (integración: `load_asset`, ruta AUTH, ruta SELF,
`resize_height`, manifest, 512/1024) y Familia E (ancla por **enumeración** de las primitivas de
carga). El detalle de cada test está en §8.1–§8.6.

### P5. ¿Qué atributos machine-readable distinguen las implementaciones?

`resize_normal_impl ∈ {HISTORICAL_UINT8, CANONICAL_FLOAT}`, `intermediate_dtype`,
`algorithm`, `renormalization_policy`, `degenerate_policy`, `module_sha256_lf`,
`module_blob_sha`, `code_git_sha`, `spec_version`. §10.1. Con la distinción explícita
código vs datos vs manifest (§10.2). El SHA general del repositorio **no** alcanza.

### P6. ¿Qué variables permanecerán fijas en el contrafactual?

§11.2: entrada normal, height, hashes, manifest, convención, resolución, solver, modelos, regla
de evaluación y thresholds congelados. **Única** intervención: `resize_normal_impl`.

### P7. ¿Es posible medir métricas M6 sin implementar M6?

**No.** `M6_CONTRAFACTUAL_PROTOCOL = SPECIFIED` · `M6_CONTRAFACTUAL_EXECUTION = BLOCKED`. La
dependencia concreta que falta es el **runner M6 de ETAPA B+** con `PAIR_EXCESS_ENERGY` y
`RECOVERY_FRACTION` instrumentados, más los thresholds `G` y `NUMERICAL_ENERGY_FLOOR` derivados
(D-01…D-05). Sustituirla por métricas de M4/M5 etiquetadas como M6 está **prohibido**: el
`NUMERICAL_NOT_DECISIONAL` de #700 es de M4/M5 y **no** es transferible.

### P8. ¿Qué riesgos residuales justifican conservar el bloqueo?

R-02 (vectores nulos — **medido**), R-05 (registration, no medible sin M6), R-06 (procedencia sin
esquema), R-07 (deriva entre versiones de Pillow, `NOT_MEASURED`), R-13 (el port directo crea un
defecto nuevo). El más fuerte es R-13: **el fix candidato, tal como lo probó #700, no está listo
para producción** porque cambia la política de vectores degenerados sin declararla.

### P9. ¿Se puede proponer un PR de código H4 independiente?

**Sí, y debe serlo** — pero **no autorizado todavía**. Es independiente de M6 (no requiere
implementar M6) y de #675 (parte de un `main` nuevo). Con D-06 y D-07 cerrados, el bloqueo que
quedaba es de **conformidad del Tech Lead** con el alcance del slice 2 (§17, condición 9), más el
valor numérico de `DEGENERATE_THRESHOLD` que se deriva **durante** la implementación (§8.0.a R7).

La recomendación es `READY_FOR_TECH_LEAD_REVIEW`. **No** es `READY_TO_IMPLEMENT`:
`H4_CODE_IMPLEMENTATION_AUTHORIZED = NO` hasta que la condición 9 de §17 se satisfaga.

### P10. ¿Cuál debe ser su sesión, rama, worktree y write-set exacto?

```text
SESSION        = H4_RESIZE_NORMAL_FIX_IMPLEMENTATION
WORKTREE       = <repo>/.worktrees/h4-resize-normal-fix   (nuevo, desde el main VIGENTE)
LOCAL_BRANCH   = h4-resize-normal-fix                     (PLANA — bug de refs anidadas de esta máquina)
REMOTE_BRANCH  = research/native-parallax-h4-resize-normal-fix
WRITE-SET      = sky_claw/local/native_parallax/research/authored_dataset.py
                 tests/test_native_parallax_h4_resize_normal.py            (nuevo)
                 <registro de procedencia estrictamente necesario>
PROHIBIDO      = thresholds, corpus, artefactos históricos, docs de #697/#700, rama de #675
```

---

## 20. Referencias con rutas, SHA y fechas

Todos los SHA citados existen en el repositorio (`git cat-file -t` → `commit`).

| Referencia | Ruta / SHA | Fecha |
|---|---|---|
| `main` al abrir este slice | `647d2461c5eee2c0827f99f9df360e300adfbe13` | 2026-10-10 |
| `main` local del worktree principal | `ef4b8aa9e485291ced37df23a1d566d5639d030d` (55 atrás) | — |
| Merge de #700 (auditoría H1–H5) | `e7c9609432c7e2425a72640c057fce3a4c234370` | 2026-10-10 |
| PR #700 — estado | `MERGED`, head `7d1362f3…` | 2026-10-10 |
| PR #697 — estado | `OPEN` / `DRAFT`, head `5a59308f…` | — |
| PR #675 — estado / HEAD M6 | `OPEN` / `DRAFT`, head `f1415711c51a2ec483fd37f41a164044649747aa` | — |
| PR #681 (MATH-A) / #685 (MATH-B) | `MERGED` (`adc5cab9…` / `83f05e40…`) | 2026-10-05 |
| Issue #667 | `OPEN` | — |
| Implementación H4 | `sky_claw/local/native_parallax/research/authored_dataset.py:237` (invocada en `:323`) | — |
| Identidad del módulo | blob `dff54d132bcaa111a13a59d838d28c7e2bfbde03` · sha256 LF `4184cbada6885cf62ef98a95100a7b1921a881b8e2f7450f2889e9337befde8c` | — |
| Adjudicación H4 | `docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/decision-impact.md` §5 | 2026-10-07 |
| Veredictos correctivos | `docs/validation/.../corrective-20261008/corrective-adjudication.json` | 2026-10-08 |
| Contrafactual C2 | `docs/validation/.../corrective-20261008/evidence/h4-corpus-counterfactual.json` | 2026-10-08 |
| Estado por hipótesis | `docs/validation/.../hypothesis-status.json` | 2026-10-07 |
| Script del contrafactual | `docs/validation/.../corrective-20261008/scripts/phase_c2_h4_corpus_corrective.py` | 2026-10-08 |
| Sonda de magnitud corregida | `docs/validation/.../corrective-20261008/scripts/h4_magnitude_probe_corrective.py` | 2026-10-08 |
| Diseño EXP-M6 (§5, §7, §16–18, §32–33, §37.3, §43) | `docs/design/research/native-parallax/exp-m6-pair-mismatch-decomposition.md` (rama de #675, `f1415711…`) | 2026-10-10 |
| Corpus M3 (resolución nativa 1024²) | `docs/design/research/native-parallax/data/exp-m3-clean-authored-manifest.json` | — |
| Test existente del resizer | `tests/test_native_parallax_exp_m2.py::test_m3_normal_resize_renormalizes` | — |
| Checker de tablas Markdown | `tests/test_native_parallax_pr700_corrective_docs_invariants.py` | 2026-10-08 |
| Deprecación de `Image.fromarray(mode=)` | `docs/deprecations.rst` de Pillow (sección «Image.fromarray mode parameter»): `.. deprecated:: 11.3.0` · `.. versionremoved:: 13.0.0` | 2026-10-10 |
| Release notes de Pillow 12.0.0 | `docs/releasenotes/12.0.0.rst` — «Part of this functionality has been restored in Pillow 12.0.0» | 2026-10-10 |
| Entorno de este slice | Python `3.11.9` · NumPy `2.4.6` · Pillow `11.3.0` (pin `numpy>=1.26,<2.5`, `pillow>=10,<12`) | 2026-10-10 |

---

## 21. Cierre de contrato — decisión del Tech Lead (`H4_PREFLIGHT_CONTRACT_CLOSURE`)

> **Slice:** `H4_PREFLIGHT_CONTRACT_CLOSURE` (docs-only). Misma rama y worktree que el preflight.
> **No** implementa código, **no** crea tests, **no** ejecuta M6, **no** accede al corpus.
> Registra las decisiones del Tech Lead sobre PR #711 y las aplica al documento.

### 21.1 Veredicto recibido

```text
PR711_PREFLIGHT=PASS_WITH_FINDINGS
CI=PASS
D06_RECOMMENDATION=FAIL_CLOSED
D07_RECOMMENDATION=PRESERVE_VALID_IDENTITY_NOOP
PILLOW13_REMOVAL_CLAIM=NEEDS_CORRECTION
H4_CODE_IMPLEMENTATION_AUTHORIZED=NO
M6_IMPLEMENTATION_BLOCKED=YES
NEXT_SLICE=H4_PREFLIGHT_CONTRACT_CLOSURE
```

Nota de lectura del propio Tech Lead, que este documento adopta: el CI verde acredita los checks
**ejecutados**, no una auditoría adversarial completa. Qodo y CodeRabbit quedaron `skipped` por
ser el PR Draft (requieren `draft == false`), así que **no** hubo revisión adversarial automática
del documento.

### 21.2 Decisión D-06 — vectores degenerados: **fail-closed**

Cuando la interpolación cancela las direcciones, **no existe una normal resultante bien
definida**. Sustituirla por `(0,0,1)` **inventaría una orientación** y podría contaminar las
métricas científicas. La política queda **fail-closed**, con el requisito explícito de que el
rechazo numérico sea **distinguible** de una exclusión silenciosa del experimento. Aplicado en
§5.3.

### 21.3 Decisión D-07 — camino identidad: **preservar el no-op válido**

Con la resolución sin cambios y entrada válida, se conserva la semántica actual: sin
interpolación, sin renormalización y sin copia obligatoria. Las entradas **no finitas o
geométricamente inválidas** se rechazan con **validación explícita**, sin usar la renormalización
para corregirlas en silencio. La **tolerancia de norma se deriva de pruebas sintéticas**, no se
ajusta al corpus. Aplicado en §7.3 (V-01) y §7.3 (V-04).

### 21.4 Corrección — alcance de la deprecación de `mode` en Pillow

**Aceptada.** El claim original del preflight («se remueve en Pillow 13», aplicado a la llamada
`uint8` + `mode="L"`) era **demasiado fuerte**. Evidencia que lo corrige:

- Documentación oficial (`docs/deprecations.rst`): deprecado en **11.3.0**, **parcialmente
  revertido en 12.0.0**; «the **only functionality removed is when the mode changes data
  types**»; el parámetro «can still be used».
- Medición propia con Pillow 11.3.0: `uint8` + `mode="L"` → funciona (solo
  `DeprecationWarning`); `uint8` + `mode="F"` → `ValueError: not enough image data`.

El uso de `resize_normal` **no cambia el tipo de dato**, así que no pertenece a la funcionalidad
removida. La migración a la inferencia automática de modo se mantiene como **deseable por
claridad y compatibilidad futura**, no como una rotura inminente. Aplicado en §4.1 y en el riesgo
R-14 (§15).

### 21.5 Separación aplicada

Los **requisitos previos a la implementación** quedaron separados de los **tests `RED→GREEN` que
se ejecutan durante ella** (§8.0), con la regla de secuencia: *un requisito previo abierto no se
cierra escribiendo un test que lo asume — el test verifica la decisión, no la sustituye.*

### 21.6 Estado resultante

```text
D06_STATUS                     = CLOSED_FAIL_CLOSED
D07_STATUS                     = CLOSED_PRESERVE_VALID_IDENTITY_NOOP
PILLOW13_REMOVAL_CLAIM         = CORRECTED
PRE_IMPLEMENTATION_VS_TESTS    = SEPARATED (§8.0)
H4_CODE_IMPLEMENTATION_AUTHORIZED = NO
M6_IMPLEMENTATION_BLOCKED      = YES
NEXT_SLICE                     = PR independiente y acotado para la corrección H4
                                 (base: un `main` nuevo; ver §19 P10)
```

**Nada de esto autoriza a empezar a escribir código.** El siguiente trabajo es un PR
**independiente y estrictamente acotado** para la corrección H4, que parte de un `main` nuevo y
trae sus propios tests. No hace falta reabrir #700, modificar #675 ni repetir el corpus real.

---

## Apéndice A — Handoff

```text
### A. Git
SESSION=H4_RESIZE_NORMAL_FIX_PREFLIGHT + H4_PREFLIGHT_CONTRACT_CLOSURE
WORKTREE=E:/Skyclaw_Main_Sync/.worktrees/h4-resize-normal-preflight
LOCAL_BRANCH=h4-resize-normal-preflight
REMOTE_BRANCH=research/native-parallax-h4-resize-normal-preflight
MAIN_START_SHA=647d2461c5eee2c0827f99f9df360e300adfbe13
MAIN_END_SHA=<ver Apéndice B — verificado al publicar>
PREFLIGHT_HEAD=<ver Apéndice B — verificado al publicar>
WORKTREE_CLEAN=YES
CONCURRENT_WRITER=NO

### B. Estado técnico
H4_DEFECT_REPRODUCED_FROM_SOURCE=YES
H4_CORRECTION_SPECIFIED=YES
D06_INVALID_VECTOR_POLICY=FAIL_CLOSED                (CERRADO)
D07_IDENTITY_PATH_CONTRACT=PRESERVE_VALID_NOOP       (CERRADO)
PILLOW13_REMOVAL_CLAIM=CORRECTED
PRE_IMPLEMENTATION_VS_TESTS=SEPARATED
FLOAT_RESIZER_IMPLEMENTED=NO
TDD_TEST_PLAN_COMPLETE=YES
ADVERSARIAL_MUTATION_PLAN_COMPLETE=YES
PROVENANCE_CONTRACT_SPECIFIED=YES
INVALID_VECTOR_POLICY=FAIL_CLOSED
DEGENERATE_THRESHOLD=DEFERRED (teoría + sintético; prohibido derivarlo del corpus)

### C. Contrafactual
M6_CONTRAFACTUAL_PROTOCOL=SPECIFIED
M6_METRICS_MEASURED=NO
REAL_CORPUS_ACCESSED=NO
THRESHOLDS_TUNED=NO
LEGACY_HELDOUT_ACCESSED=NO
H4_NUMERICAL_IMPACT_ON_M6=NOT_MEASURED

### D. Integridad
M4_EVIDENCE_CHANGED=NO
M5_EVIDENCE_CHANGED=NO
PR700_EVIDENCE_CHANGED=NO
PR697_CHANGED=NO
PR675_CHANGED=NO
M6_IMPLEMENTATION_EXECUTED=NO

### E. Gates
DOC_VALIDATION=<Apéndice B>
DIFF_CHECK=<Apéndice B>
SHA_VALIDATION=<Apéndice B>
CI=<Apéndice B>
UNRESOLVED_THREADS=<Apéndice B>

### F. Decisión final
PREFLIGHT_STATUS=COMPLETE_WITH_FINDINGS
CONTRACT_CLOSURE_STATUS=COMPLETE
H4_CODE_SLICE_RECOMMENDATION=READY_FOR_TECH_LEAD_REVIEW
H4_CODE_IMPLEMENTATION_AUTHORIZED=NO
M6_IMPLEMENTATION_BLOCKED=YES
READY_FOR_M6_IMPLEMENTATION=NO
NEXT_SLICE=PR independiente y acotado para la corrección H4 (base: un `main` nuevo)
```

## Apéndice B — Resultado de los gates de calidad

Ejecutados antes de publicar. Los valores son **medidos**, no declarados. Los de la **revisión 2**
(`H4_PREFLIGHT_CONTRACT_CLOSURE`) son los vigentes; se conservan los de la revisión 1 para
trazabilidad.

```text
WRITE_SET_UNICO            = PASS  (un solo path: el documento; rev.1 nuevo, rev.2 modificado)
DIFF_CHECK                 = PASS  (git diff --check vacío; sin whitespace sobrante ni marcadores)
MARKDOWN_TABLAS            = PASS  (rev.2: 26 tablas, 0 filas con conteo inconsistente; rev.1: 23)
MARKDOWN_FENCES            = PASS  (rev.2: 76 fences, par → balanceados; rev.1: 64)
MARKDOWN_BARRAS_ESCAPADAS  = PASS  (0 celdas con "\|" — prohibido por el checker del repo)
SHA_VALIDATION             = PASS  (8 commits + 1 blob citados; git cat-file -t los resuelve)
MAIN_BASE                  = PASS  (origin/main sigue en 647d2461…; main local sin cambios en ef4b8aa9…)
ARTEFACTOS_CIENTIFICOS     = PASS  (0 archivos científicos modificados)
PR675_PR697_PR700          = PASS  (no tocados; #675 sigue en f1415711…)
TESTS_EXISTENTES           = PASS  (26 passed — test_native_parallax_exp_m2.py + pr700 docs invariants)
TESTS_H4_NUEVOS            = NO_EJECUTADOS (pertenecen al slice 2; no existen todavía)
CI_REV1                    = PASS  (17 pass / 3 skipping / 0 fallos en 9634679f)
CI_REV2                    = ver la corrida del PR tras el push de cierre
```

**Verificación del falso verde (trampa conocida de este repo).** El job `py3.11` puede reportar
verde sin haber corrido la suite. Se leyó el **log**, no el check: `10943 passed, 20 skipped, 135
warnings` **y** `Coverage XML written to file` presente, sin `FAILED`/`ERROR`. Verde **real**.

**Revisión adversarial ausente.** Los workflows Qodo y CodeRabbit quedaron `skipped` porque el PR
está en Draft (requieren `draft == false`). Por lo tanto **no** hubo revisión adversarial
automática del documento; el CI verde acredita los checks **ejecutados**, no una auditoría.

**Advertencia explícita.** Un CI verde de este PR **no** valida ninguna implementación H4: el
documento es `RESEARCH ONLY` y el fix no existe. La única validación que este PR puede dar es
que el documento es consistente, que los SHA citados existen y que no se alteró ningún artefacto.

```text
PREFLIGHT_HEAD = el commit documental de esta rama (se reporta en el PR y en el handoff)
CI_IS_NOT_H4_VALIDATION = TRUE
```

