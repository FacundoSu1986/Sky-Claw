# FASE B — reproducción + plan de los 3 defectos del runner (#661)

> Estado (FASE B): documentado y preparado. **Ningún fix commiteado en esta fase.** PR separado por defecto.
> Post-FASE-B: **R1 cerró** con su PR dedicado (`fix(dyndolod): make packaging cancellation worker-terminal`), mergeado.
> **R2 cerró y se mergeó en #686** (merge commit `7684c92a4205a2d53aa47972be126282e093cecf`), incluido el handoff terminal
> del pre-scan contra cancelaciones repetidas; ese merge resolvió el **finding 3 de #592**. R3 continúa **OPEN**.
> #592 permanece **abierto** por sus demás findings.

## Base

```text
origin/main: 0103ee4f6de15207032d25c254ede5cf2c01bff9 (sin drift)
branch:      research/dyndolod-p0-alpha209-uia
HEAD:        962f1835
```

## R1 — `RUNNER_P1_PACKAGING_CANCEL`

| campo | valor |
|---|---|
| resolution_status | **FIXED** (PR R1 dedicado: `fix(dyndolod): make packaging cancellation worker-terminal` — Task propia del worker + `_esperar_terminalidad_del_worker`) |
| evidence_status | **REPRODUCED** (la reproducción histórica quedó convertida en regresión/aceptación por el PR R1) |
| Evidencia (antes: Reproducción) | `tests/test_runner_defects_p1_p2.py`: `test_r1_cancel_durante_el_worker_ret_al_caller_hasta_terminal` (cancel #1 → orden `cancel_1_procesada < worker_terminal < rollback_started < lease_released` congelado por igualdad), `test_r1_cancelaciones_repetidas_no_liberan_al_caller_antes_de_terminal` (cancel #2/#N con checkpoint de HANDOFF_ACTIVE por registro), `test_r1_exito_sin_cancelacion_devuelve_el_mod`, `test_r1_excepcion_del_worker_sin_cancel_se_propaga_como_antes`, `test_r1_excepcion_del_worker_con_cancel_se_consume_y_encadena`, ancla AST `test_r1_ancla_ast_el_worker_de_packaging_pasa_por_el_handoff_terminal`; la premisa de runtime se conserva en `test_r1_cancelacion_to_thread_no_mata_al_worker` |
| Invariante | `mutating packaging worker terminal BEFORE rollback begins BEFORE lease can be released` — las TRES en orden, no sólo la primera |
| Path | `dyndolod_runner.py` — era `await asyncio.to_thread(_empaquetar_sincrono)` sin protección; hoy Task propia + `_esperar_terminalidad_del_worker` |
| Plan propuesto | Secuencia completa del fix (la unidad entera, no sólo shield): crear/conservar el `Task` del worker → llega `CancelledError` → **registrar intención de cancelación** → proteger el worker (`asyncio.shield`) → **continuar esperando aunque lleguen cancelaciones adicionales** (el await externo debe ser resistente a cancelaciones repetidas, no sólo el shield interno: `shield` protege la tarea interna pero una segunda cancelación en el `await shield(...)` externo la interrumpe igual) → worker realmente terminal → recién entonces liberar el caller → rollback/lease chain puede continuar → propagar la cancelación al exterior. `shield` POR SÍ SOLO no resuelve R1 |
| Test rojo | El actual pasa con código actual (demuestra defecto). Aceptación obligatoria del futuro PR R1 — anclar con sincronización explícita (`threading.Event`/`asyncio.Event`, nunca sleeps como autoridad): **cancel #1** durante el worker bloqueado → **cancel #2** (y opcionalmente **cancel #N**) durante el handoff protegido → la propiedad `terminado.is_set()` debe ocurrir ANTES de `rollback_started` y ANTES de `lease_released` — el orden de los tres eventos se congela por igualdad, no por timing |
| Collision review | #592-2 (borra antes de medir ENOSPC) parcialmente relacionado, no overlap directo en R1 — sólo cambia el wrapper de cancelación, no el orden |
| Sub-issue | Sí, como **seguimiento de #592 finding 1** (`preservado_para_deployment` muerto + rollback paralelo), NO issue nuevo |

## R2 — `RUNNER_P1_REPARSE_COPY`

| campo | valor |
|---|---|
| resolution_status | **FIXED / MERGED** (PR #686, merge commit `7684c92a4205a2d53aa47972be126282e093cecf`; protección del contenido y lifecycle del pre-scan probados y mergeados) |
| evidence_status | **REPRODUCED** (la reproducción histórica quedó convertida en aceptación/regresión) |
| Reproducción | Histórica: `_bytes_del_arbol` contaba sólo archivos propios mientras `copytree` seguía un junction y copiaba `evil.bin`. Regresión de contenido: `test_r2_junction_descendiente_falla_antes_de_copytree_y_no_copia_evil`, `test_r2_package_rechaza_junction_antes_de_rmtree_y_preserva_mod_previo`, `test_r2_symlink_descendiente_falla_cerrado_sin_seguir_destino`, `test_r2_reparse_no_clasificado_falla_cerrado_antes_de_copytree`, `test_r2_inventario_igual_a_bytes_empaquetados_en_arbol_limpio` y `test_r2_texgen_valido_conserva_prefijo_textures`. Regresión de lifecycle: `test_r2_cancel_durante_prescan_espera_terminal_y_no_muta` |
| Invariante | `EVERY BYTE COPIED MUST BELONG TO THE ADMITTED WORKSPACE TREE` + `inventory set == copyable set`; además `pre-scan worker terminal < cancellation propagation < rollback begins < lease release`, y cancelar durante el scan impide iniciar el worker mutante |
| Path | `sky_claw/local/tools/dyndolod_runner.py:_package_output_as_mod` pone el pre-scan `exigir_arbol_copiable_sin_reparse` en una Task propia y lo espera con `_esperar_terminalidad_del_worker`, antes de `exists`/`iterdir`, `rmtree`, `mkdir` y `copytree`. La cancelación espera scan terminal y no entra al worker mutante. El guard `_exigir_fuente_del_subroot` sigue protegiendo otra propiedad: la cadena de ancestros, no los descendientes del origen |
| Implementación | La primitiva reutilizable vive en `sky_claw/app/security/links.py`: hace `lstat`/clasificación central por entrada; rechaza symlink, junction, reparse tag no clasificado y tipos distintos de directorio real/archivo regular; revalida identidad de los directorios recorridos. El runner traduce el error a `DynDOLODValidationError` con source y entrada problemática. La validación va antes de destruir el mod previo y antes de cualquier copia |
| Tests | El junction real usa `tests/_symlink_guard.crear_junction` y queda activo en Windows; el symlink cubre plataformas que lo permiten. El fake de tag desconocido entra por `reject_unclassified_reparse_point`. Se instrumenta `copytree` y se exige cero llamadas al rechazar; el mod previo queda byte-exact. El happy path ancla igualdad de archivos/bytes usando `iter_archivos_propios` en fuente y destino; TexGen conserva `textures/`. El test de lifecycle bloquea el pre-scan del método real, prueba cancel #1/#2 y congela por igualdad scan terminal → propagación → rollback → lease; verifica rmtree/mkdir/copytree/copy2/meta.ini en cero y `previous.txt` intacto |
| Red check | Al desactivar temporalmente el guard, los casos de symlink y reparse no clasificado vuelven rojos. Para lifecycle se restauró temporalmente `await asyncio.to_thread(exigir_arbol_copiable_sin_reparse, output_path)`: el test nuevo falla con `rollback_started=True`, `lease_released=True` y `scan_terminal=False`; con el handoff restaurado pasa |
| Post-merge (`7684c92a`) | El merge de #686 sumó dos mejoras focales sobre la base anterior. **F1**: el pre-scan dejó de abortar por un lock transitorio de AV/indexer — las cuatro inspecciones (`raíz`, revalidación tras `scandir`, revalidación tras enumerar, descendientes) usan `link_kind_and_identity_or_raise_with_retry` y la apertura de directorio usa `_scandir_con_reintento` (5 intentos, backoff lineal; `FileNotFoundError` no se reintenta: no es transitorio). Un bloqueo persistente sigue fallando cerrado. **F2**: `tests/test_links.py` incorporó `TestExigirArbolCopiableSinReparse` con tests directos del primitivo (árbol limpio; raíz inexistente/archivo/symlink/junction; descendiente symlink/junction; reparse no clasificado; tipo especial; entrada desaparecida; cambio de identidad al abrir y al enumerar; `PermissionError` transitorio que pasa tras retry y persistente que falla cerrado). La trazabilidad del defecto se conserva: reproducción `inventory != copyable` → pre-scan no-follow → handoff terminal del scan → F1/F2 → merge |
| TOCTOU | La lease de workspace coordina producers de Sky-Claw que la respetan, pero no es un lock del filesystem y no impide que un actor externo reemplace una entrada después del scan. Este PR cierra estrictamente el junction/reparse preexistente; no declara race-proof. Una copia con validación por entrada/handles queda como follow-up independiente |
| Collision review | R2 es exactamente #592 finding 3 (`packaging mide link-aware y copia link-following`); no crear issue nuevo. No toca #592 finding 2 (borrado previo a ENOSPC), R1, R3 ni P0/P1-P9 de #661 |
| Sub-issue | NO — el tracker canónico era #592 finding 3, **resuelto por #686** (merge `7684c92a`). #592 sigue **abierto** por sus otros findings; no se crea issue nuevo |

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
| R2 copytree traverse | #592 finding 3 (`packaging mide link-aware y copia link-following`) | **mismo exacto** | NO | mismo finding 3 de #592; no crear issue nuevo |
| R3 double cancel cleanup | ninguno todavía | nuevo hallazgo P2 | NO | vida aparte; sub-issue nuevo notificado en `#592` como hermano, o en `docs/pending_ooda_status.md` |

## Qué NO hace esta fase

- Ningún fix. Solo reproducción, invariants y plan.
- No se intentaron P1-P9 de #661 (regla de no meltzura).

## Runner gates — válidos (dos dimensiones, CR-11)

```text
RUNNER_P1_PACKAGING_CANCEL  resolution_status=FIXED  evidence_status=REPRODUCED  merge_status=MERGED  (PR R1 dedicado)
RUNNER_P1_REPARSE_COPY      resolution_status=FIXED  evidence_status=REPRODUCED  merge_status=MERGED  (PR #686, merge 7684c92a; #592 finding 3 resuelto)
RUNNER_P2_DOUBLE_CANCEL     resolution_status=OPEN   evidence_status=REPRODUCED  (PR R3 pendiente)
→ R1/R2 FIXED y mergeados; R3 sigue abierto y es el próximo slice del runner. #592 permanece OPEN por sus otros findings.
```

## Follow-ups explícitos (NO implementados en este PR)

```text
CR-5  convertir R3 de reproducción a aceptación; R1/R2 ya son regresiones → PR R3 dedicado
CR-6  reemplazar fake R3 por _execute_process real          → PR R3 dedicado
CR-7  rediseñar identidad futura de modales                 → #661 P1/P3
CR-8  rediseñar fingerprint cap                             → #661 P1/P3
CR-10 arquitectura completa de evidencia parcial            → #661 P3
CR-14 ampliar CI global a todo docs/validation              → CI/tooling follow-up
CR-15 refactor/nits generales                               → hygiene follow-up
```
