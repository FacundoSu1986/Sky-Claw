# FASE B — reproducción + plan de los 3 defectos del runner (#661)

> Estado (FASE B): documentado y preparado. **Ningún fix commiteado en esta fase.** PR separado por defecto.
> Post-FASE-B: **R1 cerró** con su PR dedicado (`fix(dyndolod): make packaging cancellation worker-terminal`);
> R2 y R3 siguen **OPEN** — ninguna fila de ellos cambió.

## Base

```text
origin/main: 0103ee4f6de15207032d25c254ede5cf2c01bff9 (sin drift)
branch:      research/dyndolod-p0-alpha209-uia
HEAD:        962f1835
```

## R1 — `RUNNER_P1_PACKAGING_CANCEL`

| campo | valor |
|---|---|
| resolution_status | **FIXED** (PR R1 dedicado: `fix(dyndolod): make packaging cancellation worker-terminal` — Task propia del worker + `_esperar_terminalidad_del_worker_mutante`) |
| evidence_status | **REPRODUCED** (la reproducción histórica quedó convertida en regresión/aceptación por el PR R1) |
| Evidencia (antes: Reproducción) | `tests/test_runner_defects_p1_p2.py`: `test_r1_cancel_durante_el_worker_ret_al_caller_hasta_terminal` (cancel #1 → orden `cancel_1_procesada < worker_terminal < rollback_started < lease_released` congelado por igualdad), `test_r1_cancelaciones_repetidas_no_liberan_al_caller_antes_de_terminal` (cancel #2/#N con checkpoint de HANDOFF_ACTIVE por registro), `test_r1_exito_sin_cancelacion_devuelve_el_mod`, `test_r1_excepcion_del_worker_sin_cancel_se_propaga_como_antes`, `test_r1_excepcion_del_worker_con_cancel_se_consume_y_encadena`, ancla AST `test_r1_ancla_ast_el_worker_de_packaging_pasa_por_el_handoff_terminal`; la premisa de runtime se conserva en `test_r1_cancelacion_to_thread_no_mata_al_worker` |
| Invariante | `mutating packaging worker terminal BEFORE rollback begins BEFORE lease can be released` — las TRES en orden, no sólo la primera |
| Path | `dyndolod_runner.py` — era `await asyncio.to_thread(_empaquetar_sincrono)` sin protección; hoy Task propia + `_esperar_terminalidad_del_worker_mutante` |
| Plan propuesto | Secuencia completa del fix (la unidad entera, no sólo shield): crear/conservar el `Task` del worker → llega `CancelledError` → **registrar intención de cancelación** → proteger el worker (`asyncio.shield`) → **continuar esperando aunque lleguen cancelaciones adicionales** (el await externo debe ser resistente a cancelaciones repetidas, no sólo el shield interno: `shield` protege la tarea interna pero una segunda cancelación en el `await shield(...)` externo la interrumpe igual) → worker realmente terminal → recién entonces liberar el caller → rollback/lease chain puede continuar → propagar la cancelación al exterior. `shield` POR SÍ SOLO no resuelve R1 |
| Test rojo | El actual pasa con código actual (demuestra defecto). Aceptación obligatoria del futuro PR R1 — anclar con sincronización explícita (`threading.Event`/`asyncio.Event`, nunca sleeps como autoridad): **cancel #1** durante el worker bloqueado → **cancel #2** (y opcionalmente **cancel #N**) durante el handoff protegido → la propiedad `terminado.is_set()` debe ocurrir ANTES de `rollback_started` y ANTES de `lease_released` — el orden de los tres eventos se congela por igualdad, no por timing |
| Collision review | #592-2 (borra antes de medir ENOSPC) parcialmente relacionado, no overlap directo en R1 — sólo cambia el wrapper de cancelación, no el orden |
| Sub-issue | Sí, como **seguimiento de #592 finding 1** (`preservado_para_deployment` muerto + rollback paralelo), NO issue nuevo |

## R2 — `RUNNER_P1_REPARSE_COPY`

| campo | valor |
|---|---|
| resolution_status | **OPEN** (sin fix en el código) |
| evidence_status | **REPRODUCED** |
| Reproducción | `test_r2_copytree_atraviesa_junction_mientras_medidor_no` (misionero: bytes copiados > presupuesto + archivos externos llegan al destino) + `test_r2_package_output_as_mod_admite_copia_de_junction` (método real admite la copia) |
| Invariante | `EVERY BYTE COPIED MUST BELONG TO THE ADMITTED WORKSPACE TREE` + `inventory set == copyable set` |
| Path | `sky_claw/local/tools/dyndolod_runner.py:2481` `shutil.copytree(src, dst)` pelado; `mod_path.mkdir` + `for item in items`. El guard de coordinación (`_exigir_fuente_del_subroot`) valida la cadena ancestro, **no descendientes del origen** |
| Plan propuesto | Introducir un guard pre-copia (sólo 1-2 líneas) que itere `iter_archivos_propios`/`reject_unclassified_reparse_point` sobre el árbol fuerte (`output_path`) y falle si hay junctions o reparse points desconocidos internos. Si alguno aparece → `DynDOLODValidationError` antes de `copytree`. El `iter_archivos_propios` ya centraliza lo que no se atraviesa: la política del copy debe ser la misma que la del inventario |
| Test rojo | Los dos actuales juegan con el defecto. El fix riguroso pasa si el assert nuevo es `assert no evil.bin en destino + presupuesto==copiado` |
| Collision review | #592 finding 3 (`packaging mide link-aware y copia link-following`) — CONFIRMADO en main. Son el MISMO defecto. Nombre equivalente al de #592. No crear issue nuevo: alojarlo como sub-tarea de #592 |
| Sub-issue | NO — va al tracker de #592 (finding 3). El nombre ligado queda en la PR |

## R3 — `RUNNER_P2_DOUBLE_CANCEL`

| campo | valor |
|---|---|
| resolution_status | **OPEN** (sin fix en el código) |
| evidence_status | **REPRODUCED** |
| Reproducción | `test_r3_segunda_cancelacion_interrumpe_la_limpieza` (multi-step cleanup sin protección) + `test_r3_ancla_ast_gather_sin_suppress_en_rama_cancelled` (branch flaky por no capturar `CancelledError`) |
| Invariante | `ONE cancellation-resistant cleanup operation` que garantice EN ORDEN: `kill_and_reap(proc)` → cancel helpers → gather/drain helpers to terminal → `close_job(job)` — y la unidad completa termina antes del `raise` final |
| Path | `dyndolod_runner.py:1890-1896` — `except asyncio.CancelledError:` llama `kill_and_reap / heartbeat.cancel / drain.cancel / gather / close_job` sin `contextlib.suppress(asyncio.CancelledError)` |
| Plan propuesto | **NO** describirlo como `suppress(CancelledError)` alrededor de awaits independientes — eso permitiría que una cancelación interrumpa UNA etapa de cleanup y simplemente continúe a la siguiente dejando la anterior incompleta. La unidad correcta es UNA operación cancellation-resistant (un `finally`/wrapper único que acumule las etapas y las ejecute todas, tolerando cancelaciones repetidas dentro del cleanup: si llegan cancelaciones adicionales mientras cleanup está activo, cleanup continúa hasta terminal y DESPUÉS se propaga la cancelación). El `raise` final corre recién cuando TODAS las etapas corrieron |
| Test rojo | La reproducción (`test_r3_segunda_cancelacion_interrumpe_la_limpieza`) usa sincronización explícita (`handler_entered`/`reap_entered`/`allow_reap_to_finish` con `asyncio.Event`), nunca timing sleeps: cancel #1 → confirmar entrada en la rama → reap entra y se bloquea en checkpoint conocido → cancel #2 → observar. PRE-FIX demuestra de forma determinista `close_job_llamado == 0`. **Aceptación obligatoria del futuro PR R3 (documentada acá, NO implementada como test verde hoy)**: tras el fix, `close_job == 1`, readers/heartbeat terminal, proc reaped/bounded — en el MISMO flujo de doble cancelación |
| Collision review | Sin overlap directo en #592. El defecto es lifecycle/cancellation, no parte de la auditoría de la toolchain |
| Sub-issue | Sí: como **seguimiento de lifecycle/cancel-robustness** (stripe distinto). Recomiendo abrir un issue pequeño dedicado, o enlazar dentro de `docs/pending_ooda_status.md` si se decide mantenerlo sólo tracker-local |

## Herramientas seleccionadas

- **Windows junctions**: `tests/_symlink_guard.py:crear_junction` (convención ya existente y anclada — evita crear helpers duplicados). POSIX/`junction_guard` se usa en R2.
- **AST anchors**: propiedad del mecanismo, verificada con `ast.parse` sobre el archivo real — si el defecto cambia de forma, la ancla rompe (intencionado).
- **Determinismo**: todo cancel detonado con `threading.Event`, nunca solo `sleep`.

## Collision matrix (aija exacta)

| Defecto | Existe en issue | Nombre de entrada | overlap con P0 de #661 | motivo del PR separado |
|---|---|---|---|---|
| R1 packaging cancel | #592 finding 1 (`preservado…`) | **mismo** (`p1-packaging-cancel`) | NO (UIA viability) | para que un future fix tenga un hogar de issue rastreador sin duplicar #661 |
| R2 copytree traverse | #592 finding 3 (`packaging mide link-aware y copia link-following`) | **mismo exacto** | NO | parte del tracker #792 |
| R3 double cancel cleanup | ninguno todavía | nuevo hallazgo P2 | NO | vida aparte; sub-issue nuevo notificado en `#592` como hermano, o en `docs/pending_ooda_status.md` |

## Qué NO hace esta fase

- Ningún fix. Solo reproducción, invariants y plan.
- No se intentaron P1-P9 de #661 (regla de no meltzura).

## Runner gates — válidos (dos dimensiones, CR-11)

```text
RUNNER_P1_PACKAGING_CANCEL  resolution_status=FIXED  evidence_status=REPRODUCED  (PR R1 dedicado)
RUNNER_P1_REPARSE_COPY      resolution_status=OPEN  evidence_status=REPRODUCED
RUNNER_P2_DOUBLE_CANCEL     resolution_status=OPEN  evidence_status=REPRODUCED
→ RUNNER_FIXES_READY_FOR_IMPLEMENTATION (R1 cerrado; R2/R3 siguen abiertos)
```

## Follow-ups explícitos (NO implementados en este PR)

```text
CR-5  convertir reproducciones R1/R2/R3 a xfail            → PRs R1/R3 dedicados
CR-6  reemplazar fake R3 por _execute_process real          → PR R3 dedicado
CR-7  rediseñar identidad futura de modales                 → #661 P1/P3
CR-8  rediseñar fingerprint cap                             → #661 P1/P3
CR-10 arquitectura completa de evidencia parcial            → #661 P3
CR-14 ampliar CI global a todo docs/validation              → CI/tooling follow-up
CR-15 refactor/nits generales                               → hygiene follow-up
```
