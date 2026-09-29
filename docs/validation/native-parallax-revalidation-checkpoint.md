# Native Parallax Revalidation — Checkpoint

**Fecha**: 2026-09-28 (M3) / 2026-09-29 (M4 calibration + full + publicación)
**Branch**: `research/native-parallax-m3-m4-height-resize-revalidation`
**Base**: `1a52c3ea` (origin/main, incluye fix #653)
**Commit de artefactos**: `dd5fba95` — sólo añade los 4 artefactos de validación; sin cambios de código `native_parallax`
**Anotación de protocolo**: commit posterior en el mismo PR
(`docs(validation): annotate M4 artifact with protocol_status metadata`) — añade la clave
`protocol_status` al JSON de M4 y esta sección. Sin cambios de código, sin retuneo, sin
rerun, sin freeze retroactivo. Sigue siendo el mismo conjunto de **4 archivos**.

> **Nota terminológica**: para 512 el resultado es *decision-equivalent / materialmente
> estable* (deltas en el 3.º–5.º decimal respecto del histórico). Sólo el control
> nativo 1024 es idéntico bit a bit, como es esperable: `resize_height` devuelve el
> array original cuando el tamaño de entrada ya coincide con el target.

## Estado de la corrida

### ✅ COMPLETADO — EXP-M3 revalidation @512

**Command:**
```
.venv\Scripts\python -m sky_claw.local.native_parallax.research.run_exp_m3 ^
  --m3-manifest docs\design\research\native-parallax\data\exp-m3-clean-authored-manifest.json ^
  --m2-manifest C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b\exp-m2-authored-manifest-local.json ^
  --resolution 512 ^
  --out C:\SkyClawResearch\NativeParallax\EXP-M3\runs\revalidation-height-resize-20260928\m3
```

**Results file**: `docs/validation/native-parallax-revalidation-20260928/exp_m3_results.json`
(copia del original en `...\runs\revalidation-height-resize-20260928\m3\exp_m3_results.json`)

**Decision (verificada en el JSON, línea 182)**: `EXP_M3_RECONSTRUCTION_CONDITIONAL` — decision-equivalent vs histórico.

| Métrica | Histórico | Nuevo | Resultado |
|---|---|---|---|
| RAW median_abs_corr | 0.8392 | 0.8391782269 | materialmente estable |
| RAW median_variance_ratio | 0.7043 | 0.7042200966 | materialmente estable |
| CALIBRATION corr | 0.7716 | 0.7715445879 | materialmente estable |
| CALIBRATION var | 0.5953 | 0.5952810511 | materialmente estable |
| HELD_OUT corr | 0.8744 | 0.8753961326 | materialmente estable |
| HELD_OUT var | 0.7647 | 0.7665263080 | materialmente estable |
| Proxy spearman (held-out) | +0.488 | +0.4882352941 | igual |
| AUC catastrophic (held-out) | 0.229 | 0.2291666667 | igual |

### ✅ COMPLETADO — EXP-M4 calibration @512

**Command:**
```
.venv\Scripts\python -m sky_claw.local.native_parallax.research.run_exp_m4 ^
  --m3-manifest docs\design\research\native-parallax\data\exp-m3-clean-authored-manifest.json ^
  --corpus-root C:\SkyClawResearch\NativeParallax\EXP-M3 ^
  --resolution 512 ^
  --phase calibration ^
  --out C:\SkyClawResearch\NativeParallax\EXP-M3\runs\revalidation-height-resize-20260928\m4\calibration.json
```

- 15/15 assets CALIBRATION procesados, 0 exclusiones.
- Fase de calibración = smoke test de implementación, sin decisión por diseño.
- **Results file**: `docs/validation/native-parallax-revalidation-20260928/calibration.json`

### ✅ COMPLETADO — EXP-M4 full @512

**Command:**
```
.venv\Scripts\python -m sky_claw.local.native_parallax.research.run_exp_m4 ^
  --m3-manifest docs\design\research\native-parallax\data\exp-m3-clean-authored-manifest.json ^
  --corpus-root C:\SkyClawResearch\NativeParallax\EXP-M3 ^
  --resolution 512 ^
  --phase full ^
  --frozen-ack freeze-1a52c3ea5d2259f3b05bfdef1fdbe781608c610f ^
  --out C:\SkyClawResearch\NativeParallax\EXP-M3\runs\revalidation-height-resize-20260928\m4\exp_m4_results.json
```

**Results file**: `docs/validation/native-parallax-revalidation-20260928/exp_m4_results.json`
— **NO es una copia byte-idéntica del raw.** El archivo publicado es el raw con **una clave
top-level añadida post-run**, `protocol_status`, que expone la desviación de protocolo de
forma machine-readable (ver "Protocol deviation" más abajo). La anotación es
exclusivamente de metadatos de protocolo: `rows`, `summary` (incluido `summary.decision`),
`thresholds`, `dataset` y `environment` son deep-equal al raw, verificado programáticamente.
`environment.frozen_ack` **no** fue tocado.

**Hashes canónicos.** La columna es el **blob commiteado** (fin de línea LF), que es lo que
verifica un consumidor tras clonar. No es el hash del working tree en Windows: con
`core.autocrlf=true` (y `.gitattributes` fijando LF sólo para `*.py`, no para `*.json`), el
checkout en Windows reintroduce CRLF y recalcular el hash da otro valor. Verificable con:

```bash
git cat-file blob HEAD:docs/validation/native-parallax-revalidation-20260928/<archivo> | sha256sum
```

| Archivo | SHA256 commiteado (LF) | Bytes | Relación con el raw |
|---|---|---:|---|
| raw `m4\exp_m4_results.json` (externo, en disco) | `8b018150883c1ebf3e7ab16fe365431e5dadc307b1197f1bb042d08ec26b1557` | 83233 | salida del runner (CRLF en disco) |
| `exp_m4_results.json` | `7ecb3f243409699d9bf650814af21246fb0e06502d334c0f68978e5d821bf5bd` | 83341 | raw + clave `protocol_status` |
| `exp_m3_results.json` | `02f6d1ac5a679081359b43a173f8e849fa9819e0d1c4e60830266a337b2335da` | 237549 | normalización LF del raw |
| `calibration.json` | `7157105ec45383bfd736539524406cd2e2c42445e34821e20d67c3084f7f70b4` | 40194 | normalización LF del raw |

En disco (Windows, CRLF) `exp_m3_results.json` y `calibration.json` **sí** son byte-idénticos
a sus raw (`047769fd…` / 243752 y `ccd9bb70…` / 41194); la diferencia con la tabla es
exclusivamente la normalización de fin de línea al commitear. `exp_m4_results.json` en disco
es `de172bc5…` / 85377: difiere del raw por la clave `protocol_status` **y** por EOL.

El `raw_sha256` que quedó dentro del propio JSON, en
`protocol_status.annotation.raw_sha256` (`8b018150…`, `raw_bytes: 83233`), es
**deliberadamente el del raw en disco**, no el del blob: el raw es un archivo externo que no
vive en git, y es el hash que quien posea la corrida va a recalcular sobre su copia.

**Decision**: `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT` — decision-equivalent vs histórico.
**Reglas**: `C1_self_good=true` y `C2_auth_worse=true` en ambas corridas (histórica y nueva).

**Full (512), histórico → nuevo:**
| Métrica | Histórico | Nuevo |
|---|---:|---:|
| rmse_self_median | 0.00374646 | 0.00365917 |
| rmse_auth_median | 0.03923831 | 0.03921097 |
| delta_rmse_median | 0.03529285 | 0.03506316 |
| abs_corr_self_median | 0.99916463 | 0.99899525 |
| abs_corr_auth_median | 0.83922878 | 0.83917823 |
| var_self_median | 0.99832995 | 0.99799151 |
| var_auth_median | 0.70430494 | 0.70422010 |

**Held-out (512), histórico → nuevo:**
| Métrica | Histórico | Nuevo |
|---|---:|---:|
| rmse_self_median | 0.00263924 | 0.00282885 |
| rmse_auth_median | 0.03862444 | 0.03859779 |
| delta_rmse_median | 0.03381978 | 0.03357388 |
| abs_corr_self_median | 0.99963340 | 0.99960618 |
| abs_corr_auth_median | 0.87436327 | 0.87539613 |
| var_self_median | 0.99926697 | 0.99921255 |
| var_auth_median | 0.76474957 | 0.76652631 |

**Control nativo 1024 (sin resize) — idéntico bit a bit:**
| Métrica | Histórico | Nuevo |
|---|---:|---:|
| delta_rmse_median | 0.026297977651925285 | 0.026297977651925285 |
| rmse_self_median | 0.00694985 | 0.00694985 |
| rmse_auth_median | 0.03888586 | 0.03888586 |
| abs_corr_self_median | 0.99439317 | 0.99439317 |
| abs_corr_auth_median | 0.86254319 | 0.86254319 |

**Conclusión**: el fix #653 modifica ligeramente algunos números a 512 (esperable por la
preservación float32), pero **no cambia ninguna decisión científica de M3 ni M4**.
El control nativo 1024 permanece idéntico, confirmando que el fix no afecta assets ya nativos.

## Protocol deviation — freeze de EXP-M4 (documentada, no ocultada)

El preregistro M4 (§8) exige: calibration OK → **commit de execution-freeze** → recién
después correr full/held-out con `--frozen-ack freeze-<ese-SHA>`.

**Desviación real de esta revalidación**: no se creó un commit intermedio entre calibration
y full. El argumento usado, `freeze-1a52c3ea5d2259f3b05bfdef1fdbe781608c610f`, identifica el
checkout base congelado (origin/main con #653), pero **no es un execution-freeze creado
después de calibration**.

**Limitación verificada del runner** (`sky_claw/local/native_parallax/research/run_exp_m4.py`,
argumento `--frozen-ack`, líneas ~278-281): sólo rechaza el caso vacío/ausente
(`parser.error`). No verifica que el SHA referenciado exista, que corresponda a HEAD ni
que sea posterior a calibration. Es una declaración de protocolo, no una atestación
criptográfica.

**Por qué los resultados siguen siendo válidos**: thresholds, solver, corpus, manifest,
split y reglas ya estaban congelados históricamente y no se retunearon. La desviación es
procedural, no científica.

**Decisión tomada**: NO crear un commit de freeze retroactivo — no probaría que existió
antes de mirar held-out y sería peor científicamente que documentar la desviación.

### Metadatos machine-readable de la desviación (`protocol_status`)

Documentar la desviación sólo en prosa no alcanza: un consumidor machine-readable que leyera
`summary.decision` + `environment.frozen_ack` concluiría que la corrida es confirmatoria y
protocol-compliant. Por eso el JSON publicado lleva una clave top-level `protocol_status`
**añadida post-run, sólo de protocolo**, que declara:

| Campo | Valor | Significado |
|---|---|---|
| `status` | `UNDER_REVIEW_PROTOCOL_DEVIATION` | la corrida está en revisión, no cerrada |
| `confirmatory_final` | `false` | no es un resultado confirmatorio final |
| `observed_runner_decision` | `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT` | el valor observado, preservado |
| `observed_runner_decision_is_confirmatory` | `false` | no es una decisión confirmatoria nueva |
| `execution_freeze_post_calibration.performed` | `false` | no hubo freeze post-calibration conforme al preregistro §8.3 |
| `execution_freeze_post_calibration.retroactive_freeze_created` | `false` | no se fabricó uno a posteriori |
| `annotation.scope` | `protocol_metadata_only` | la anotación no toca la ciencia |
| `annotation.raw_sha256` | `8b018150…` | hash del raw original, verificable |

`summary.decision` conserva intacto el valor observado por el runner
(`EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT`): la metadata **aclara su status**, no lo reescribe.
Un consumidor debe leer `protocol_status.status` / `protocol_status.confirmatory_final` para
no tratar esta revalidación como confirmatoria.

## Environment

- Python 3.11.9, NumPy 2.4.6, Pillow 11.3.0, venv en `.venv`.
- Windows — captura explícita del 2026-09-29 (elimina la contradicción previa):
  ```
  [System.Environment]::OSVersion.Version  →  10.0.19045.0
  Win32_OperatingSystem:  Caption=Microsoft Windows 10 Pro, Version=10.0.19045, BuildNumber=19045
  Registry (CurrentVersion):  ProductName=Windows 10 Pro, DisplayVersion=22H2, CurrentBuild=19045, UBR=7725
  ```

## Files to NOT touch

- `data/exp-m4-results.json` (histórico — DO NOT overwrite)
- `data/exp-m4-calibration.json` (histórico — DO NOT overwrite)
- M3 trust doc (`docs/design/research/native-parallax/exp-m3-clean-authored-trust.md`)
- M3 manifest (`docs/design/research/native-parallax/data/exp-m3-clean-authored-manifest.json`)

## Próximos pasos

1. PR exclusivamente con estos 4 artefactos de validación (decisión del usuario; no se creó
   PR hasta ahora por instrucción explícita previa). Revisar y mergear.
2. Restackear PR #646 (EXP-M5): estaba 4 commits ahead / 10 behind respecto de main.
3. Para M5, ser **más estricto** que en este rerun: calibration 15 → commit de
   execution-freeze REAL → full 31 con `--frozen-ack freeze-<ese-SHA>`.
