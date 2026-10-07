# Native Parallax — REVAL-1: revalidación versionada de M2/M3 tras las correcciones matemáticas

Revalidación reproducible del experimento **Native Parallax** (EXP-M2 / EXP-M3) después de
las correcciones matemáticas PR-MATH-A, PR-MATH-B y PR-REPRO-A. Compara el artefacto
histórico congelado contra un **replay del código viejo en el mismo entorno** y contra el
**código nuevo**, en las tres superficies que permite un replay exitoso.

- **Fecha (UTC):** 2026-10-06
- **Estado:** `REVAL1_EXECUTION_STATUS=COMPLETE`
- **Arena review adversarial:** `PASS_WITH_FINDINGS` (ver `comparison/impact-report.md` §6)

---

## 1. Procedencia

```
PROTOCOL_SHA                          = fc87b7e5191e42bd7e38b128b7baeaec7516dadb
RUN_CODE_SHA                          = fc87b7e5191e42bd7e38b128b7baeaec7516dadb
POST_FREEZE_NATIVE_PARALLAX_DRIFT     = NO

OLD_CODE_REPLAY_SOURCE_SHA            = e23bf7ac75bf8ac7b1b80f1944119598f743b7b7
OLD_CODE_REPLAY_SOURCE_RELATION       = BEST_SUPPORTED_M2_BRANCH_HEAD_AT_HISTORICAL_RUN_WINDOW
OLD_M2_EXECUTION_SHA_FULL_VERIFIED    = NO
OLD_CODE_REPLAY_IS_EXACT_HISTORICAL_SHA_ATTESTATION = NO

OLD_M2_RUNNER_INTRODUCED_SHA          = f69275107b546b23fb481fd662ecbf9827831ab4
HISTORICAL_DOCUMENT_MAIN_SHA_PREFIX   = 607ff21
HISTORICAL_DOCUMENT_MAIN_SHA_IS_EXECUTION_SOURCE = NO
```

`RUN_CODE_SHA` es válido porque el PR #694 fue **docs-only** y no cambió código Native
Parallax ejecutable (verificado: `git diff fc87b7e5..origin/main` vacío en el subárbol).

El replay OLD es un **control reproducible**, no una atestación del SHA histórico exacto.
`e23bf7ac` no es ancestro de `fc87b7e5`. Lo que lo valida es que reproduce el artefacto
histórico **bit a bit** en toda la superficie científica (ver §5).

## 2. Entorno

```
REVAL_PYTHON   = E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe
PYTHON_VERSION = 3.11.9 (tags/v3.11.9:de54cf5, MSC v.1938 64 bit AMD64)
PYTEST_VERSION = 8.4.2
RUFF_VERSION   = 0.15.11
NUMPY_VERSION  = 2.4.6
PILLOW_VERSION = 11.3.0
OS             = Windows-10-10.0.19045-SP0 (AMD64)
BLAS/LAPACK    = scipy-openblas 0.3.31.188.0 (USE64BITINT DYNAMIC_ARCH NO_AFFINITY SkylakeX)
FFT            = numpy.fft (pocketfft)
```

Sin cambios de dependencias entre OLD y NEW. Detalle completo en `comparison/environment.json`.

`REVAL_PYTHON` es el intérprete del venv del **repo principal**, y es el único que se
usó: los worktrees detached **no tienen `.venv` propio**. Los comandos de §4 se corren
con `cwd` en el worktree correspondiente para que el paquete `sky_claw` se resuelva
desde ahí, pero el ejecutable siempre es `REVAL_PYTHON`.

## 3. Worktrees

```
NEW         = E:\SkyClaw_REVAL1_NEW_fc87b7e5      (detached @ fc87b7e5)  clean
OLD replay  = E:\SkyClaw_REVAL1_OLD_e23bf7ac      (detached @ e23bf7ac)  clean
```

Gate de importación verificado en ambos: el paquete se resuelve desde el worktree correcto
(`OLD_IMPORT_SOURCE_VERIFIED=YES`, `NEW_IMPORT_SOURCE_VERIFIED=YES`). El worktree principal
`E:\Skyclaw_Main_Sync` se usó sólo como repositorio (fetch/worktree/show/diff); no se limpió,
resetó, stasheó ni cambió de rama.

## 4. Comandos exactos

Preflight y worktrees (desde `E:\Skyclaw_Main_Sync`):

```bash
git fetch origin --prune
git rev-parse origin/main                                   # == fc87b7e5191e42bd7e38b128b7baeaec7516dadb
git diff --name-status fc87b7e5...origin/main -- sky_claw/local/native_parallax tests docs/design/research/native-parallax
git worktree add --detach E:\SkyClaw_REVAL1_NEW_fc87b7e5 fc87b7e5191e42bd7e38b128b7baeaec7516dadb
git worktree add --detach E:\SkyClaw_REVAL1_OLD_e23bf7ac e23bf7ac75bf8ac7b1b80f1944119598f743b7b7
```

Validación del código NEW (cwd = worktree NEW; intérprete = `REVAL_PYTHON`):

```bash
REVAL_PYTHON="E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe"

"$REVAL_PYTHON" -m pytest -q tests/test_native_parallax_math_spike.py \
  tests/test_native_parallax_spearman.py tests/test_native_parallax_proxy_math.py \
  tests/test_native_parallax_exp_m1.py tests/test_native_parallax_exp_m2.py \
  tests/test_native_parallax_m2_json.py tests/test_native_parallax_exp_m3.py \
  tests/test_native_parallax_exp_m3_corpus.py              # 210 passed
"$REVAL_PYTHON" -m ruff check sky_claw/local/native_parallax/          # All checks passed!
"$REVAL_PYTHON" -m ruff format --check sky_claw/local/native_parallax/ # 20 files already formatted
```

Alcance de ruff: `sky_claw/local/native_parallax/` (20 archivos `.py` en el worktree
NEW). Es la validación **pre-run del código NEW**, no el gate de CI completo: el gate
`Lint` de CI exige además `ruff check sky_claw/ tests/` y
`ruff format --check sky_claw/ tests/` (ver `AGENTS.md`), que corren en el workflow y no
se reproducen acá.

Runners (`RAW_RUN_ROOT` = `C:\SkyClawResearch\NativeParallax\EXP-M3\runs\math-revalidation-20261006T223304Z-fc87b7e5`):

```bash
REVAL_PYTHON="E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe"
RAW_RUN_ROOT='C:\SkyClawResearch\NativeParallax\EXP-M3\runs\math-revalidation-20261006T223304Z-fc87b7e5'

# OLD replay M2 — cwd = E:\SkyClaw_REVAL1_OLD_e23bf7ac
"$REVAL_PYTHON" -m sky_claw.local.native_parallax.research.run_exp_m2 \
  --manifest "C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b\exp-m2-authored-manifest-local.json" \
  --resolution 512 --out "$RAW_RUN_ROOT/old_replay/m2"

# NEW M2 — cwd = E:\SkyClaw_REVAL1_NEW_fc87b7e5
"$REVAL_PYTHON" -m sky_claw.local.native_parallax.research.run_exp_m2 \
  --manifest "C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b\exp-m2-authored-manifest-local.json" \
  --resolution 512 --out "$RAW_RUN_ROOT/new/m2"

# NEW M3 — cwd = E:\SkyClaw_REVAL1_NEW_fc87b7e5
"$REVAL_PYTHON" -m sky_claw.local.native_parallax.research.run_exp_m3 \
  --m3-manifest "docs\design\research\native-parallax\data\exp-m3-clean-authored-manifest.json" \
  --m2-manifest "C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b\exp-m2-authored-manifest-local.json" \
  --resolution 512 --out "$RAW_RUN_ROOT/new/m3"
```

`RAW_RUN_ROOT` se creó con semántica `mkdir(exist_ok=False)` y sólo los padres
(`evidence/ comparison/ old_replay/ new/`); cada runner creó su propio subdirectorio por
primera vez. `NO_CLOBBER_OUTPUT=PASS`. Ningún output fallido se reutilizó.

## 5. Gates y resultados

| Gate | Resultado |
|---|---|
| `PATH_PREFLIGHT` | PASS (8/8) |
| `PRE_RUN_M2_HASH_MATCH` | 68/68 |
| `PRE_RUN_M3_HASH_MATCH` | 62/62 |
| Hashes de artefactos históricos | 4/4 exactos |
| `NEW_M2_STRICT_JSON` / `NEW_M3_STRICT_JSON` | PASS / PASS |
| Validación NEW pre-run | 210 tests passed, ruff limpio |
| `OLD_CODE_REPLAY_STATUS` | SUCCESS (exit 0) |
| `ENVIRONMENT_REPLAY_DRIFT` | **0** (científico) |
| `M3_DECISION_EQUIVALENT` | **YES** |
| `UNEXPECTED_UNEXPLAINED_CHANGES` | **0** |

## 6. Hashes

### Corpus (verificación inmediatamente pre-run)

```
M2 corpus: 68/68 archivos contra el manifest  (34 assets × 2)
M3 corpus: 62/62 archivos contra el manifest  (31 assets × 2)
```

### Artefactos históricos

| Artefacto | sha256 |
|---|---|
| `EXP-M3/cohort_b/exp-m2-authored-manifest-local.json` | `40ff24bd3342c60d6d23d700975ae5d4f37adffc96bb900aaaf0ecea58d5a34a` |
| `EXP-M3/m2_control/rows.json` | `c2d8328e62046245d4c395ee0ecaad3696b8fb48f6578c9582079182ad770a08` |
| `EXP-M3/m2_control/characs.json` | `9afb60a453d2783572145c3d270145b2dc668941bbbdf96a5ac25cf7cef7a683` |
| `EXP-M3/runs/revalidation-height-resize-20260928/m3/exp_m3_results.json` | `047769fdad05e9af7960feb2c414470af7493d847e0901bac51d7c4a84bc5e19` |

### RAW (no modificados después de hashear)

| RAW | bytes | sha256 |
|---|---:|---|
| `old_replay/m2/rows.json` | 254365 | `25fd6124c7ea97688cbe47039d800b7520bfb3332c9fe9a63068d29096ff8c30` |
| `old_replay/m2/characs.json` | 91786 | `df65d8660ef7fd066e8d1e8d90196acc84d039f9b2612b3be9ad4f834160f28f` |
| `new/m2/rows.json` | 254772 | `b2bbea3ffa85b1d847b20c50d2d1dce1e6723f74ca5da254615008ae99569a48` |
| `new/m2/characs.json` | 91536 | `402c3f6f3f4d47c7681cc77d7684425a1b5e4e00b25e2e4a5dde0dcd1e3071b2` |
| `new/m3/exp_m3_results.json` | 277216 | `4364d1c97fd011d44c59f39335a72a42fdff398b1de66c7500db2e5fc5ca4205` |

## 7. RAW vs PUBLISHED

Los archivos publicados se copiaron **byte a byte** desde el RAW (`TRANSFORMATION=COPY_NO_MODIFICATION`).
`worktree_sha256 == raw_sha256` en los 5 archivos. `SEMANTIC_EQUALITY=IDENTICAL`.

**Regla CRLF / git blob (§11).** El repo tiene `core.autocrlf=true`, así que el **blob**
versionado normaliza CRLF→LF mientras el **worktree** conserva el RAW:

| archivo | `RAW_SHA256` (= worktree) | EOL | `GIT_BLOB_SHA256` | EOL |
|---|---|---|---|---|
| `new/m2/rows.json` | `b2bbea3f…` | CRLF | `433ba09e…` | LF |
| `new/m2/characs.json` | `402c3f6f…` | CRLF | `c16b02a7…` | LF |
| `new/m3/exp_m3_results.json` | `4364d1c9…` | CRLF | `c30957a4…` | LF |
| `old-replay/m2/rows.json` | `25fd6124…` | CRLF | `668f46d8…` | LF |
| `old-replay/m2/characs.json` | `df65d866…` | CRLF | `f3119bdc…` | LF |

La diferencia es **exclusivamente** de fin de línea y **no** indica corrupción
(`blob_equals_raw_normalized=true` en los 5). Detalle completo, con bytes y EOL por archivo,
en `comparison/raw-vs-published.json`. Los archivos del **corpus no se publican**; para ellos
el SHA de bytes físicos bajo `C:\SkyClawResearch\` es el gate (§6).

## 7b. Nota sobre el OLD replay y JSON no estándar

`old-replay/m2/{rows,characs}.json` **contienen** `Infinity`/`NaN` crudos: son la salida del
código OLD, que no sanea el boundary JSON. Eso es el **comportamiento histórico** y se
preserva tal cual — es precisamente lo que REPRO-A corrige en NEW. El requisito
`STRICT_JSON=PASS` aplica sólo a los outputs NEW (`new/**`), y se verifica que esos tres no
contienen ningún token no estándar.

## 8. Metodología de comparación

Identidad M2: `(asset, split, policy, k, sigma_candidate)`. `sigma_candidate = NONE` para RAW
y `<winner>` para transfer/sweep. **`sigma_eff` no se usa como clave** — es valor comparado.

Identidad M3 rates: `q ∈ {0.1, 0.2, 0.3, 0.4, 0.5}`. **No** se usa `threshold` como identidad.

Clasificación de cada celda: no-finitos primero (NaN/Inf/None), luego igualdad exacta, luego
tolerancia de precisión de máquina (1e-12 relativo), luego material.

Superficies M2:

- **A** HISTORICAL vs OLD_REPLAY → drift de entorno/replay.
- **B** OLD_REPLAY vs NEW → delta de versión de código, mismo entorno.
- **C** HISTORICAL vs NEW → revalidación histórica final (idéntica a B porque A = 0).

M3: HISTORICAL_M3_RAW vs NEW_M3.

Atribución causal: ver `comparison/impact-report.md`. Se sostiene en (i) el contrafactual
exacto para la corrección de height, (ii) la descomposición factorial 2×2×2 de cada rho de
selección, y (iii) la comparación de features por asset. Detalle en
`evidence/counterfactual-height-isolation.json`, `evidence/attribution-diagnostics-{OLD,NEW}.json`
y `comparison/spearman-diagnostics.json`.

## 9. Resultados

- `M3_DECISION_EQUIVALENT = YES` — `EXP_M3_RECONSTRUCTION_CONDITIONAL` en OLD y NEW.
- `M2_SIGMA_WINNER` = `nz_p01` en las tres superficies.
- `UNEXPECTED_UNEXPLAINED_CHANGES = 0`.
- `ENVIRONMENT_REPLAY_DRIFT = 0`.
- `OLD_RATE_Q_MAPPING_VERIFIED = NO` (proxies distintos entre superficies; ver informe §4.3).
- Cambio dominante M2: `HEIGHT_FIDELITY_CORRECTION` (4 564 celdas), no previsto por la
  taxonomía del protocolo; etiqueta introducida explícitamente.

## 10. Alcance

```
M4_RERUN_EXECUTED   = NO
M5_RERUN_EXECUTED   = NO
M6_REAL_RUN_EXECUTED= NO
```

M4/M5 se auditan recién después de conocidos los deltas M2/M3. M6 continúa bloqueado.
No se modificó ningún runner, ni el corpus, ni los artefactos OLD.

## 11. Contenido de este directorio

```
README.md
new/m2/{rows.json,characs.json}          salida NEW M2
new/m3/exp_m3_results.json               salida NEW M3
old-replay/m2/{rows.json,characs.json}   salida del replay OLD
comparison/environment.json              entorno, rutas resueltas, hashes
comparison/corpus-hash-audit.json        auditoría 68/68 y 62/62 por archivo
comparison/m2-historical-vs-replay.json  superficie A
comparison/m2-replay-vs-new.json         superficie B
comparison/m2-historical-vs-new.json     superficie C
comparison/m3-historical-vs-new.json     M3
comparison/spearman-diagnostics.json     gate de identidad + factorial de rho
comparison/taxonomy-summary.json         conteos por categoría
comparison/raw-vs-published.json         RAW/WORKTREE/GIT_BLOB sha256 + EOL por archivo
comparison/impact-report.md              informe causal + Arena review
```
