# FASE B — reproducción + plan de los 3 defectos del runner (#661)

> Estado (FASE B): documentado y preparado. **Ningún fix commiteado en esta fase.** PR separado por defecto.
> Post-FASE-B: **R1 cerró** con su PR dedicado (`fix(dyndolod): make packaging cancellation worker-terminal`), mergeado.
> **R2 cerró y se mergeó en #686** (merge commit `7684c92a4205a2d53aa47972be126282e093cecf`), incluido el handoff terminal
> del pre-scan contra cancelaciones repetidas; ese merge resolvió el **finding 3 de #592**. **R3 tiene su fix
> en el PR R3 dedicado (tracker #692), pendiente de merge**, verificado sobre el `_execute_process` real.
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
| resolution_status | **FIXED / PR R3 pendiente de merge** (tracker #692; rama `fix/dyndolod-r3-double-cancel`) |
| evidence_status | **REPRODUCED** |
| Reproducción | Histórica: `test_r3_segunda_cancelacion_interrumpe_la_limpieza` (flujo simulado determinista) + `test_r3_ancla_ast_gather_sin_suppress_en_rama_cancelled`. **Hoy (CR-6)**: la aceptación corre sobre el `_execute_process` REAL — `test_r3_cancelacion_repetida_no_interrumpe_la_limpieza`, `test_r3_cancelaciones_repetidas_n_no_liberan_al_caller`, `test_r3_cierra_el_job_una_vez_cuando_el_handle_es_none`, `test_r3_cancel_durante_la_limpieza_de_otra_rama_tampoco_saltea_el_cierre`, `test_r3_una_falla_real_de_la_limpieza_no_se_traga_y_el_cierre_ocurre`, `test_r3_camino_normal_sin_cancelacion_no_cambia`, `test_r3_la_clasificacion_de_error_no_cambia` |
| Invariante | `ONE cancellation-resistant cleanup operation` que garantice EN ORDEN: `kill_and_reap(proc)` → cancel helpers → helpers a terminal → `close_job(job)` — y la unidad completa termina antes del `raise` final. Propiedad: `proc_reaped < helpers_cancelled < helpers_terminal < job_closed < cancellation_propagated` |
| Path | `sky_claw/local/tools/dyndolod_runner.py:_execute_process`. Era `except asyncio.CancelledError:` con la secuencia `kill_and_reap / heartbeat.cancel / drain.cancel / gather / close_job` inline y sin protección. Hoy las CUATRO ramas de excepción comparten una sola unidad (`_cerrar_recursos_del_proceso` → `_limpiar_recursos_del_proceso`) esperada por el terminal handoff común `_handoff_terminal`; `close_job` vive en el `finally` del try del proceso |
| Implementación | El terminal handoff de R1 se extrajo a un núcleo común, `_handoff_terminal`: loop con `asyncio.shield` que absorbe cancelaciones repetidas y devuelve `(intencion, falla)` para que cada call site aplique su propia precedencia (R1 camina normal; R3 vive dentro de un handler de cancelación). `_cerrar_recursos_del_proceso` crea la Task de limpieza y la espera por ese núcleo; dentro, cada etapa (`kill_and_reap`, cancelación de los tres helpers, `gather` de terminalidad) se intenta aunque la anterior falle — abandono a medias es el defecto que R3 cierra — y la primera falla real se RE-LANZA para viajar como `__cause__` del veredicto. `close_job` se movió al `finally` del try del proceso: así corre en TODA salida (incluido el camino de éxito y la cancelación del propio cleanup) exactamente una vez |
| Follow-up F1–F4 (revisión pre-merge de #693) | Cuatro findings válidos, cada uno con test rojo propio y mutante muerto. **F1** — `_cerrar_recursos_del_proceso` descartaba la intención de cancelación absorbida: las ramas de error/timeout/genérica devolvían un veredicto ordinario después de que el caller pidió cancelación. Ahora devuelve `(intención, falla)`; la cancelación del caller gana como resultado externo y el veredicto tipado queda como `__cause__`, con la falla de limpieza encadenada detrás por `_encadenar_falla_de_limpieza` (que agrega al FINAL de la cadena en vez de pisarla, así los tres hechos sobreviven). **F2** — `_etapa` esperaba el trabajo pelado: cancelar la Task de limpieza abortaba el `kill_and_reap`, la limpieza seguía con los helpers y `close_job` corría con el proceso SIN reapear, con el cleanup aparentemente exitoso. Ahora cada etapa corre en su PROPIA Task esperada por `_handoff_terminal`; una Task de etapa cancelada por un tercero no cuenta como completada (se re-conduce, acotado por `_MAX_RECONDUCCIONES_DE_ETAPA`, y agotado se reporta como falla), y una limpieza que termina cancelada tampoco se declara exitosa. **F3** — `gather(return_exceptions=True)` observaba la terminalidad de los tres helpers pero convertía sus fallas en valores que nadie inspeccionaba: ahora se clasifican (`CancelledError` esperado por el `task.cancel()` propio vs falla real) y cada falla real se registra y se encadena. **F4** — la misma falla se registraba dos veces (`_etapa` y `_cerrar_recursos_del_proceso`): ahora se registra una sola vez, donde se descubre, conservando `pipeline_stage`/`tx_id`/`operation_type` |
| Test rojo / Red check | PRE-FIX determinista sobre el `_execute_process` real: `cancel #2 liberó al caller antes de que la limpieza fuera terminal: orden=['job_asignado', 'drain_out_entered', 'drain_err_entered', 'kill'] close_job=0`. POST-FIX: con el reap bloqueado en checkpoint, el caller sigue pendiente, `close_job == 1`, sin helpers vivos al cerrar, y `CancelledError` recién después. Mutation testing: **5/5 mutantes muertos** (quitar el shield; no esperar la terminalidad de los helpers; volver a la limpieza inline; quitar `close_job`; no re-lanzar la falla de limpieza). Follow-up: **8/8 mutantes muertos** sobre el código nuevo (descartar la intención en cada una de las tres ramas; volver al `await` pelado por etapa; aceptar una etapa cancelada como completada; aceptar una limpieza abandonada; ignorar los resultados del `gather`; duplicar el registro) |
| Collision review | Sin overlap directo en #592. El defecto es lifecycle/cancellation, no parte de la auditoría de la toolchain. Único archivo fuera del write-set esperado: `tests/test_dyndolod_t5v21_mutation_matrix.py`, cuya ancla M16 exigía el cleanup **inline** en la rama `DynDOLODExecutionError`; se actualizó a la forma compartida conservando su intención y quedando más estricta (exige la delegación + `close_job` una sola vez desde el `finally`) |
| Sub-issue | **#692** — `fix(dyndolod): make process cleanup resistant to repeated cancellation`. Defecto hermano del rig `#661` FASE B, **no** un finding de #592 |

## Herramientas seleccionadas

- **Windows junctions**: `tests/_symlink_guard.py:crear_junction` (convención ya existente y anclada — evita crear helpers duplicados). POSIX/`junction_guard` se usa en R2.
- **AST anchors**: propiedad del mecanismo, verificada con `ast.parse` sobre el archivo real — si el defecto cambia de forma, la ancla rompe (intencionado).
- **Determinismo**: todo cancel detonado con `threading.Event`, nunca solo `sleep`.

## Collision matrix (aija exacta)

| Defecto | Existe en issue | Nombre de entrada | overlap con P0 de #661 | motivo del PR separado |
|---|---|---|---|---|
| R1 packaging cancel | #592 finding 1 (`preservado…`) | **mismo** (`p1-packaging-cancel`) | NO (UIA viability) | para que un future fix tenga un hogar de issue rastreador sin duplicar #661 |
| R2 copytree traverse | #592 finding 3 (`packaging mide link-aware y copia link-following`) | **mismo exacto** | NO | mismo finding 3 de #592; no crear issue nuevo |
| R3 double cancel cleanup | **#692** (`fix(dyndolod): make process cleanup resistant to repeated cancellation`) | nuevo hallazgo P2 | NO | vida aparte; issue dedicado abierto en el PR R3 (hermano del rig, **no** un finding de `#592`) |

## Qué NO hace esta fase

- Ningún fix. Solo reproducción, invariants y plan.
- No se intentaron P1-P9 de #661 (regla de no meltzura).

## Runner gates — válidos (dos dimensiones, CR-11)

```text
RUNNER_P1_PACKAGING_CANCEL  resolution_status=FIXED  evidence_status=REPRODUCED  merge_status=MERGED   (PR R1 dedicado)
RUNNER_P1_REPARSE_COPY      resolution_status=FIXED  evidence_status=REPRODUCED  merge_status=MERGED   (PR #686, merge 7684c92a; #592 finding 3 resuelto)
RUNNER_P2_DOUBLE_CANCEL     resolution_status=FIXED  evidence_status=REPRODUCED  merge_status=PENDING  (PR R3, tracker #692)
→ R1/R2 FIXED y mergeados; R3 FIXED y esperando review/merge. #592 permanece OPEN por sus otros findings.
```

## Follow-ups explícitos (NO implementados en este PR)

```text
CR-5  convertir R3 de reproducción a aceptación; R1/R2 ya son regresiones → HECHO en el PR R3 (tracker #692)
CR-6  reemplazar fake R3 por _execute_process real          → HECHO en el PR R3 (tracker #692)
CR-7  rediseñar identidad futura de modales                 → #661 P1/P3
CR-8  rediseñar fingerprint cap                             → #661 P1/P3
CR-10 arquitectura completa de evidencia parcial            → #661 P3
CR-14 ampliar CI global a todo docs/validation              → CI/tooling follow-up
CR-15 refactor/nits generales                               → hygiene follow-up
```
