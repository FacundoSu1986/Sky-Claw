# Revisión pre-merge — PR #688 (`docs(dyndolod): close R2 bookkeeping after PR #686`)

**HEAD revisado:** `f7a35161b3323e75430f432a6695a8d72057752c`
**Base:** `main @ 7684c92a4205a2d53aa47972be126282e093cecf` (sin drift)
**Estado GitHub:** `OPEN` · `mergeable = MERGEABLE` · `mergeStateStatus = UNSTABLE` (causa: los dos jobs de Qodo PR-Agent)
**Diff:** 3 archivos, +55/−19, **cero código productivo**

---

## Veredicto

```text
LISTO PARA MERGE  — sin hallazgos bloqueantes.
```

Una salvedad documental no bloqueante (R1, LOW) sobre artefactos fechados del rig que siguen diciendo `OPEN`.
Todo lo demás verificado de forma independiente, incluido el red check del ancla nueva y la matriz R1/R2/R3.

---

## 1. Verificación independiente (no me apoyé en el body del PR)

| Check | Resultado |
|---|---|
| `pytest tests/test_estado_ooda.py` | **19 passed** |
| `pytest tests/test_runner_defects_p1_p2.py` | **14 passed, 2 skipped** |
| `pytest tests/test_links.py` | **51 passed, 9 skipped** |
| los tres juntos | **84 passed, 11 skipped** |
| `ruff check sky_claw/ tests/` | `All checks passed!` |
| `ruff format --check sky_claw/ tests/` | `825 files already formatted` |
| `git diff --check` | limpio |
| **Red check** del ancla nueva (restaurar `docs/pending_ooda_status.md` a `origin/main`, conservar el test) | **falla** con `AssertionError: assert 'Parcial' == 'Cerrado'` → el test ancla el cierre post-merge |
| CI de `ci.yml` sobre `f7a3516` | **verde**: Ruff (py3.11/3.12), Mypy (ambas), Tests Windows py3.11 (20m18s) y py3.12 (22m2s), Security Scan, Build, POSIX Core, CodeQL |
| Diff fuera de los 3 archivos | **ninguno** (`gh pr view 688 --files` = 3) |

> Nota de método: un primer conteo mostró `1 failed, 18 passed` en `test_estado_ooda.py`; era un artefacto de haber
> corrido el red check en paralelo con la medición (el red check reemplaza el OODA temporalmente). Repetido en
> secuencial: **19 passed**. No hay fallo real.

## 2. Exactitud factual del diff (verificada contra el código de `main`)

| Afirmación en los docs del PR | Verificación |
|---|---|
| "las cuatro inspecciones usan `link_kind_and_identity_or_raise_with_retry`" | ✔ `links.py:659, 686, 694, 705` (raíz, tras `scandir`, tras enumerar, descendientes) |
| "`_scandir_con_reintento` (5 intentos, backoff lineal)" | ✔ `_LINK_INSPECTION_RETRIES = 5`, `_LINK_INSPECTION_BACKOFF_SECONDS = 0.1` |
| "`FileNotFoundError` no se reintenta" | ✔ `raise` explícito en el helper |
| "`TestExigirArbolCopiableSinReparse` con tests directos del primitivo" | ✔ `tests/test_links.py:855` con 14 tests |
| "R2 se mergeó en #686 (`7684c92a…`)" | ✔ PR #686 `MERGED`, merge commit coincide con `origin/main` |
| "R1 mergeado" | ✔ PR #677 `MERGED` (`73d7cb819195fb728e741879f8bb6d2201c4d1be`) |
| "#592 sigue abierto por sus otros findings" | ✔ #592 `OPEN`; 3 casillas tildadas (finding 3) y 16 sin tildar (findings 1, 2, 4–9) |
| "el pre-scan no se declara race-proof" | ✔ conservado en OODA, plan y en la nota de #592 |
| Trazabilidad histórica (reproducido → pre-scan → handoff → F1/F2 → merge) | ✔ preservada, sin reescritura de historia |

Además: `#592` ya tiene aplicada la nota `RESUELTO por PR #686` + merge commit, y el body de #688 refleja ese estado
(el bloque `<details>` con "texto pendiente para un humano" ya no está).

## 3. Revisión de scope (lo que el PR NO hace, y debe seguir sin hacer)

* **NO toca código productivo**: `sky_claw/app/security/links.py`, `sky_claw/local/tools/dyndolod_runner.py` y
  `tests/test_runner_defects_p1_p2.py` intactos (el diff son 2 docs + 1 test de consistencia).
* **NO implementa R3**: `_execute_process`, `kill_and_reap`, `heartbeat.cancel`, `drain.cancel`, `gather`,
  `close_job` sin tocar; `test_r3_segunda_cancelacion_interrumpe_la_limpieza` sigue como reproducción abierta.
* **NO cierra #592** ni toca su finding 2 (borrado previo a ENOSPC) ni P1–P9 de #661.
* **NO reafirma el TOCTOU como resuelto**: el límite `pre-scan ⇏ race-proof` sigue declarado.

## 4. Hallazgos

### R1 — [LOW, NON-BLOCKING] Artefactos fechados del rig siguen diciendo `RUNNER_P1_REPARSE_COPY = OPEN`

`docs/validation/2026-10-02_p0_uia_alpha209/` (instantáneas del rig del 2026-10-02):

* `CHECKPOINT-handoff.md:54-57` → los tres gates en `OPEN`;
* `final-report.md:34-37` → anclado explícitamente a `0103ee4f` (base histórica);
* `p0b/final-report-p0b.md:140-142` → sección "Runner gates — sin cambios";
* `environment.json` → entrada `RUNNER_P1_REPARSE_COPY` con `resolution: OPEN`.

**Evaluación:** son snapshots fechados (uno ancla su base commit) y **ningún código/tests los consume**
(verificado: sin consumidores en `sky_claw/`, `tests/`, `local_scripts/`). Los trackers canónicos
(`docs/pending_ooda_status.md`, `runner-defects-plan.md`) ya dicen MERGED, y #592 tiene la nota de resolución.
Reescribirlos iría contra el principio de no borrar historia que el propio PR respeta.
**Recomendación (no bloqueante):** dejarlos como están; si se quiere evitar confusión, un puntero de una línea en
`final-report.md` del tipo *"instantánea de la fecha; estado vigente en `docs/pending_ooda_status.md`"* — como
follow-up, no en este PR.

### R2 — [INFO] `mergeStateStatus = UNSTABLE`

Causa: los dos jobs de **Qodo PR-Agent** (`Auto-revisión en PR interno`, `Regression & Test Oracle`) fallan en 34-38s
con error de herramienta/API, y fallan igual en ramas no relacionadas (`arena/01a10bb8-sky-claw`,
`arena/01a10d47-sky-claw`, `feat/frozen-runtime-p3-candidate`). `UNSTABLE` (y no `BLOCKED`) indica que no son checks
requeridos. No pude leer la branch protection para confirmarlo (`403` con el token actual, `administration: read` no
disponible): queda como observación, no como bloqueo.

### R3 — [INFO] CHANGELOG

No corresponde entrada: el repo no tiene política de CHANGELOG para bookkeeping de docs (sin menciones en
`CONTRIBUTING.md`, `AGENTS.md` ni `.github/coding_conventions.md`), y #686 tampoco la agregó.

## 5. Recomendación

```text
MERGE #688  (LOW residual R1 → follow-up opcional, no bloquea)
SIGUIENTE SLICE: R3 — RUNNER_P2_DOUBLE_CANCEL (OPEN / REPRODUCED)
```
