# FASE B — reproducción + plan de los 3 defectos del runner (#661)

> Estado: documentado y preparado. **Ningún fix commiteado.** PR separado por defecto.

## Base

```text
origin/main: 0103ee4f6de15207032d25c254ede5cf2c01bff9 (sin drift)
branch:      research/dyndolod-p0-alpha209-uia
HEAD:        962f1835
```

## R1 — `RUNNER_P1_PACKAGING_CANCEL`

| campo | valor |
|---|---|
| Status | REPRODUCED |
| Reproducción | `tests/test_runner_defects_p1_p2.py::test_r1_cancelacion_to_thread_no_mata_al_worker` (6/6 passed — demuestra el defecto) + `test_r1_ancla_ast_el_to_thread_empaquetar_esta_sin_proteccion` (anchor sobre `_package_output_as_mod`) |
| Invariante | `ROLLBACK MUST NOT BEGIN UNTIL MUTATING PACKAGING WORKER IS EFFECTIVELY TERMINAL` + `LEASE MUST NOT BE RELEASED WHILE WORKER CAN STILL WRITE` |
| Path | `dyndolod_runner.py:2488` — `await asyncio.to_thread(_empaquetar_sincrono)` sin protección |
| Plan propuesto | Mover la cancel-awareness al llamador del to_thread: capturar el `asyncio.Task`, sustituir el `to_thread` por `asyncio.ensure_future(asyncio.shield(work))` con handoff explícito del thread → `await` hasta que el worker termine DE VERDAD antes de soltar la cadena. El ledger externo (DirectoryRollback) sigue recibiendo el orden porque el caller del packaging devuelve UN awaitable terminal que NO se puede cancelar hasta que el hilo terminó |
| Test rojo | El actual pasa con código actual (demuestra defecto). El fix requiere añadir una aserción de inversa: tras cancelación, `terminado` debe resolverse antes de que el post-cleanup de la corrida pueda observar el thread vivo |
| Collision review | #592-2 (borra antes de medir ENOSPC) parcialmente relacionado, no overlap directo en R1 — sólo cambia el wrapper de cancelación, no el orden |
| Sub-issue | Sí, como **seguimiento de #592 finding 1** (`preservado_para_deployment` muerto + rollback paralelo), NO issue nuevo |

## R2 — `RUNNER_P1_REPARSE_COPY`

| campo | valor |
|---|---|
| Status | REPRODUCED |
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
| Status | REPRODUCED |
| Reproducción | `test_r3_segunda_cancelacion_interrumpe_la_limpieza` (multi-step cleanup sin protección) + `test_r3_ancla_ast_gather_sin_suppress_en_rama_cancelled` (branch flaky por no capturar `CancelledError`) |
| Invariante | `EVERY EXIT PATH MUST TERMINATE ALL OWNED PROCESS/HELPER RESOURCES (incl. second cancellation)` |
| Path | `dyndolod_runner.py:1890-1896` — `except asyncio.CancelledError:` llama `kill_and_reap / heartbeat.cancel / drain.cancel / gather / close_job` sin `contextlib.suppress(asyncio.CancelledError)`. El `gather` no es protegido; también `kill_and_reap` es interruptible por segundo cancel |
| Plan propuesto | Traer una primitiva unificada de cleanup: `finally`-chain con `contextlib.suppress(asyncio.CancelledError)` para las ramas internas del cleanup, rodeando `close_job` + gather + kill y diferenciando solamente el `raise` final. Evitar duplicación en las otras ramas (`contextlib.suppress` existente en las otras except/else). |
| Test rojo | Un `assert close_job_llamado == 0` bajo doble cancel; el fix debe quebrarla (pasa cuando `close_job_llamado == 1` en este flujo) |
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

## Runner gates — válidos

```text
RUNNER_P1_PACKAGING_CANCEL = REPRODUCED
RUNNER_P1_REPARSE_COPY     = REPRODUCED
RUNNER_P2_DOUBLE_CANCEL    = REPRODUCED
→ RUNNER_FIXES_READY_FOR_IMPLEMENTATION
```
