# Auditoría 03 — Pipeline (Orquestación y Herramientas Externas) — Backlog consolidado

**Fusión de dos auditorías independientes**, deduplicada y verificada línea por línea
contra el árbol vivo del repo (no contra dumps). Cada ítem trae número de línea **real**,
severidad **recalibrada**, fuente, y el fix **corregido** cuando la propuesta original
estaba mal.

- **Fuentes:** `[A]` = auditoría propia (Claude); `[Z]` = auditoría ZAI; `[A+Z]` = ambas
  lo encontraron (convergencia).
- **Deployment confirmado por el mantenedor:** Sky-Claw corre **standalone** (NO lanzado
  desde MO2). Esto sube U-01 a cabecera.
- Categorías del protocolo: Subprocesos/Zombies · Integridad VFS · Teardown/Rollback.

---

## Resumen ejecutivo

| Tier | IDs | Foco |
|------|-----|------|
| **Alto** | U-01 … U-05 | Corrección VFS silenciosa, huérfanos por muerte dura, rituales sin rollback, timeout que orfana nietos |
| **Medio** | U-06 … U-10 | Falso verde por exit-code, nieto DynDOLOD, reconciliación de arranque, journal vs teardown, timeout sin excepción |
| **Bajo** | U-11 … U-12 | Higiene: returncode centinela, leak de `.pas` |
| **Apéndice** | R-1 … R-2 | Ítems ZAI rebajados/rechazados (con motivo) |

Convergencia real entre ambas auditorías: **U-04** (Wrye Bash sin rollback) y, parcialmente,
**U-02** / **U-08**. El resto es complementario: ZAI aportó U-05 (VRAMr), U-09 y U-10;
la propia aportó U-01, U-03, U-06, U-07, U-12.

---

## TIER ALTO

### U-01 — Precondición VFS/USVFS no enforced (standalone ⇒ fallo total silencioso) · `[A]` · Integridad VFS

- **Archivos:** `local/loot/cli.py:131-151`, `local/xedit/runner.py:792-806`,
  `local/tools/wrye_bash_runner.py:59-71`, `local/tools/dyndolod_runner.py:457`,
  `local/tools/synthesis_runner.py:214-216`. **Sensor romo — TODOS los rituales, no
  solo DynDOLOD** (mismo `build_vfs_sensor(..., scan_mods_dir=False)`, verificado en el
  árbol vivo tras el review de Codex #349): `dyndolod_service.py:193-196`,
  `xedit_service.py:617-620`, `synthesis_service.py:237-240`, `pandora_service.py:173`,
  `wrye_bash_service.py:154-157`. Único que ya escapa al patrón:
  `loot_service.py:221` usa `scan_mods_dir=mo2_validated` (condicional al perfil MO2
  validado) — ese es el comportamiento a replicar.
- **Mecanismo:** todos los tools externos se spawnean **directo** (no vía
  `ModOrganizer.exe`; solo el juego usa el proxy, `vfs.py:353`). Corriendo standalone,
  ninguno hereda la USVFS de MO2. El pipeline depende de un árbol de mods **materializado
  a disco**, precondición no afirmada ni guardada. Con un MO2 en USVFS estándar (el
  default), Sky-Claw opera sobre el `Data` base-game **en verde desde el Stage 1**. Hay
  además incoherencia de modelo: la SALIDA se asume redirigida a `overwrite` (USVFS) pero
  la ENTRADA se asume materializada.
- **Fix:** (1) guard de VFS-health bloqueante en el preflight ANTES de todo ritual mutante
  — reforzar `build_vfs_sensor` con `scan_mods_dir=True` y verificar que los mods del perfil
  activo estén visibles en el path que los tools leen; fail-closed si no. **Aplicar de forma
  CENTRAL a los 5 callers listados arriba (no solo DynDOLOD)** — o mejor, mover el default a
  `scan_mods_dir` derivado de la validación del perfil (como ya hace `loot_service.py:221`)
  para no dejar rituales atrás; parchear un solo sensor deja los otros 4 en falso-verde.
  (2) Reconciliar el modelo de salida (`overwrite` vs `Data`/`mods`) en
  `_find_*_output`/`_permission_targets`. (3) Documentar la invariante de deployment.

### U-02 — Sin Job Object en Windows: la muerte dura de Python orfana todo el árbol externo · `[A+Z]` · Subprocesos/Zombies

- **Archivos:** puntos de spawn en `local/tools/_process.py:133,193`, `local/mo2/vfs.py:358`
  (`launch_game`), cadena del crash-loop de grass. `grep JobObject|KILL_ON_JOB_CLOSE` → 0.
- **Mecanismo:** todo el anti-huérfano vive en `finally`/`kill_and_reap`/`_matar_todo`, que
  un `SIGKILL`/OOM/`os._exit()`/corte de luz **no ejecutan**. `vfs.py` trackea PIDs en el
  dict in-memory `_launched_procs` (`:128`), que se pierde con el proceso. No hay store
  persistente ni barrido de arranque.
- **Fix (corregido):** asignar cada hijo a un **Job Object** con
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (pywin32/ctypes) en `_process` (+ el lanzamiento de
  MO2). El SO mata el árbol al cerrarse el handle del padre, incluso en muerte dura.
  > **Alcance ampliado (review de Codex #349, verificado en el árbol vivo):** el Job Object
  > en `_process` NO cubre los spawns que hacen `create_subprocess_exec` **directo**, sin
  > pasar por `run_capture`: `local/loot/cli.py:148`, `local/tools/dyndolod_runner.py:457`,
  > `local/tools/vramr_service.py:305` (y `vfs.py:358`/`launch_game`, ya listado). Si el fix
  > se implementa solo en `_process`, esos tools siguen fuera del Job Object y una muerte dura
  > de Python los orfana igual. Hay que (a) enrolarlos en el Job Object en cada call site, o
  > (b) primero rutearlos por el helper compartido `_process` y aplicar el Job Object ahí una
  > sola vez. El inventario de spawns directos es el criterio de completitud de U-02.
  > **Corrección al fix de ZAI (su Finding 9):** `atexit` **NO** corre en los escenarios que
  > la propia ZAI lista (OOM kill, `os._exit()`, corte de luz, SIGKILL) — solo en salida
  > normal / excepción no atrapada. El Job Object es la única solución OS-enforced.

### U-03 — `PrecacheGrass.txt` persistente tras muerte dura → el juego real arranca en modo precache · `[A]` · Teardown/Rollback

- **Archivo:** `local/tools/grass_cache_runner.py:249` (escribe el flag en el `game_path`
  REAL); borrado solo en `finally: self._remove_flag_sync()` (`:394-398`). `grep PrecacheGrass`
  → sin barrido de arranque.
- **Mecanismo:** SIGKILL a mitad del precache deja el flag junto a `SkyrimSE.exe`; la próxima
  vez que el usuario abre el juego con NGIO activo, re-entra en precache (800x400, crash-loop).
- **Fix:** (1) U-02 cubre el proceso; (2) hook idempotente de arranque en `app_context` que
  borre un `PrecacheGrass.txt` huérfano si no hay ritual de grass activo (reusa
  `_remove_flag_sync`).

### U-04 — Wrye Bash y Pandora: rituales mutantes SIN rollback de salida · `[A+Z]` · Teardown/Rollback

- **Archivos:** `wrye_bash_service.py:429,437` (`target_files=[]`), manifiesto `snapshots=[]`
  (`:290`); `pandora_service.py:375` (`target_files=[]`), `:283`. Runner:
  `wrye_bash_runner.py:72-80` (timeout → `success=False`).
- **Mecanismo:** ante cuelgue → timeout, `run_capture` mata+reapea (bien), pero el
  `Bashed Patch, 0.esp` / behavior graph **parcialmente escrito** queda en disco: sin
  snapshot que restaurar. El servicio marca la TX rolled-back pero **el archivo no se
  revierte**. Contraste: LOOT/xEdit/Synthesis snapshotean; DynDOLOD usa move-aside.
- **Fix:** envolver la llamada al runner en `DirectoryRollback` sobre el dir de salida
  concreto, o resolver el path antes del run y pasarlo como `target_files` al
  `SnapshotTransactionLock` (el path ya se computa para el manifiesto,
  `wrye_bash_service.py:249-259`). **Ojo:** el path de salida depende del modelo VFS (U-01) —
  cerrar U-01 primero para snapshotear la ubicación correcta.
  > **`target_files` NO alcanza por sí solo (review de Codex #349, verificado):** el timeout
  > de Wrye Bash y Pandora se traduce a `result.success == False` **sin elevar excepción**
  > (`wrye_bash_runner.py:72-80` retorna el struct; ver U-10), así que el `async with
  > SnapshotTransactionLock(...)`/`DirectoryRollback` **sale limpio** y esos helpers restauran
  > solo ante excepción (o force-rollback explícito) — el backup se descarta y el output
  > parcial persiste. El fix debe además **disparar el rollback ante un resultado fallido**
  > (elevar dentro del context, o forzar el rollback cuando `result.success is False`).
  > Por eso **U-10 (elevar `WryeBashTimeoutError`/`BodySlideTimeoutError`) es prerequisito** de
  > este remedio: sin la excepción dedicada, agregar `target_files` da una falsa sensación de
  > rollback.

### U-05 — VRAMr: timeout orfana nietos (usa `proc.kill()` pelado, no el tree-kill) · `[Z]` · Subprocesos/Zombies

- **Archivo:** `local/tools/vramr_service.py:328-331` — `except TimeoutError: proc.kill()`
  directo. Su propio path de cancelación (`:320-323`) **sí** llama `kill_and_reap(proc)`.
- **Mecanismo:** en Windows `proc.kill()` mata solo el hijo directo; `kill_and_reap` hace
  `taskkill /F /T` (árbol completo, `_process.py:41-64`). Si VRAMr spawnea workers de
  compresión de texturas (plausible), en timeout quedan **huérfanos** reteniendo file locks
  sobre `output_dir` y consumiendo GPU/CPU.
- **Fix:** reemplazar por `await kill_and_reap(proc)` + cancelar drains (espejo del path de
  cancelación de la misma función).
  > **Recalibración:** ZAI lo marcó 🔴 CRÍTICO; realista **Medio-Alto** (el orfanato de nietos
  > es condicional a que VRAMr spawnee sub-procesos). Hallazgo real y accionable — no estaba
  > en la auditoría propia.

---

## TIER MEDIO

### U-06 — Falso verde: éxito por exit-code sin verificar el artefacto · `[A]` (amplía el F7 de `[Z]`) · Integridad VFS

- **Archivos:** `wrye_bash_runner.py:85-91` (`success = return_code == 0`);
  `bodyslide_runner.py:76-82` (idem); `xedit/runner.py:874-883` (QuickAutoClean,
  `success = return_code==0 and not errors`); `dyndolod_runner.py:998-1017`
  (`validate_dyndolod_output` devuelve `True` con **cualquier** `.esp`, ni exige `DynDOLOD.esp`).
- **Mecanismo:** estas GUIs salen 0 en no-op (cwd mala, diálogo auto-descartado, plugin no
  cargado) → verde falso → los stages siguientes construyen sobre un artefacto inexistente.
  Es el smoke pendiente de QuickAutoClean ya documentado en `AGENTS.md`.
- **Fix:** post-check de artefacto por runner (existe + mtime avanzó + no vacío; DynDOLOD
  exige `DynDOLOD.esp`). Cierra también el smoke de "Limpiar Archivos".

### U-07 — DynDOLOD: en salida NORMAL con un nieto (TexGen) vivo, el nieto queda huérfano · `[A]` · Subprocesos/Zombies

- **Archivo:** `local/tools/dyndolod_runner.py:536-571` (rama `else`, salida normal): al
  agotarse `_DRAIN_GRACE_SECONDS`, cancela los drains y **sigue sin `kill_and_reap`**. El
  comentario anticipa el "nieto con pipe heredado" pero no lo mata; una vez que DynDOLOD
  salió, TexGen se reparenta y escapa a `taskkill /T /PID <dyndolod_pid>`.
- **Fix:** el Job Object de U-02 lo resuelve limpio (el nieto muere con el job aunque se
  reparente). Sin U-02, snapshotear los hijos al spawn y matarlos en la rama de drain-timeout.

### U-08 — Sin reconciliación de arranque / clone()/`_materialize` no auto-limpian el parcial · `[A+Z]` · Teardown/Rollback + Integridad VFS

*(Fusiona: propia MF6 + ZAI Finding 2 + ZAI Finding 6 — misma raíz.)*

- **Archivos:** `profile_sandbox.py:299-316` (`_materialize`: 5 `copytree` sin try/except de
  limpieza); `mo2/sandbox_run.py:85` (`clone = await sandbox.clone()` **fuera** del `try` de
  `:86-108`); `profile_sandbox.py:274-288` (`_promote_sync` entero en un `to_thread`);
  backups huérfanos `*.rollback-<nonce>` (DynDOLOD) y `.skyclaw_sandbox/*/rollback-*` sin
  barrido; staging dirs `DynDOLOD_Output`/`TexGen_Output` no cubiertos por el move-aside.
- **Mecanismo:** una cancelación/muerte durante `clone()`/`_materialize` deja un clon parcial;
  una muerte durante `_promote_sync` deja el perfil medio-aplicado + backup huérfano. Los
  locks se auto-curan (TTL), las promesas de FS no.
- **Fix:** (1) `_materialize` self-cleaning: `try/except BaseException: rmtree(clone.root); raise`
  — esto resuelve también el clone-fuera-del-try (el dir con UUID no colisiona, así que el
  impacto era leak de disco, no bloqueo). (2) Reconciliador de arranque que barra
  `*.rollback-*` / `.skyclaw_sandbox/*/rollback-*` y complete/revierta según marcador durable;
  como piso, GC de backups huérfanos. Comparte hook con U-03.
  > **El `try/except` in-thread NO cubre la cancelación (review de Codex #349, verificado):**
  > `clone()` corre `_materialize` vía `await asyncio.to_thread(self._materialize, clone)`
  > (`profile_sandbox.py:226`). Cancelar la corrutina `clone()` **no inyecta `BaseException`
  > en el hilo worker** — el `except BaseException` de dentro de `_materialize` nunca se
  > dispara, el `copytree` sigue corriendo en background y puede dejar un clon parcial tras
  > propagarse la cancelación. Mismo patrón que F2/F5 de la auditoría de resiliencia (#319).
  > El remedio (1) por lo tanto debe: **o** limitarse explícitamente a fallos SÍNCRONOS de
  > copia (documentándolo), **o** observar/`shield`-ear el worker y limpiar DESPUÉS de que el
  > hilo termine ante cancelación (patrón de F2, `asyncio.shield` + desenlace observado). El
  > reconciliador de arranque (2) es la red que cubre el clon parcial que la cancelación deja.
  > **Corrección a ZAI Finding 2:** la consecuencia "próxima invocación falla con 'el clon ya
  > existe'" es **falsa** — eso es de `GrassProfileManager` (nombre fijo), no de `ProfileSandbox`
  > (`profile_sandbox.py:216`, dir con UUID → no colisiona). Impacto real = leak de disco.
  > Severidad ALTO → **Medio**.

### U-09 — Journal de grass se commitea como éxito pese a fallos de teardown · `[Z]` · Teardown/Rollback

- **Archivo:** `grass_cache_service.py:340` (`exito = error_msg is None and run_result and
  run_result.success`); `teardown_failures` se adjunta al result (`:351-355`) pero no afecta
  `exito`.
- **Mecanismo:** si el teardown deja el clon/mod sin borrar, el journal registra éxito. El
  operador SÍ se entera (via `teardown_failures` + logs), pero el audit trail miente sobre el
  estado de FS.
- **Fix (corregido):** **NO** usar `exito = ... and not teardown_failures` — eso marcaría
  `rolled_back` un cache que SÍ se generó y se preserva, dejando `result["success"]=True`
  contra un journal `ROLLED_BACK` (inconsistencia nueva). Introducir un **estado de
  éxito-parcial** (o metadata `teardown_incomplete=True` en la TX committeada) que refleje
  "producto OK, cleanup pendiente".
  > **Recalibración:** ZAI ALTO → **Medio / design-call**. El fix de una línea de ZAI estaba roto.

### U-10 — BodySlide/Wrye Bash: TimeoutError se traga como struct, sin excepción dedicada · `[Z]` · Timeouts

- **Archivos:** `wrye_bash_runner.py:72-80`, `bodyslide_runner.py:63-71` — retornan
  `success=False, return_code=-1` en vez de elevar (inconsistente con
  `DynDOLODTimeoutError`/`SynthesisTimeoutError`/`XEditTimeoutError`).
- **Mecanismo:** la capa de servicio no distingue timeout de exit≠0 → sin reintento
  específico ni journal diferenciado.
- **Fix:** elevar `WryeBashTimeoutError`/`BodySlideTimeoutError` dedicadas.
  > **Matiz:** el stderr YA dice "Timeout during ..." (distinguible por texto), así que el
  > impacto es de consistencia/limpieza, no de pérdida de información. Severidad **Bajo-Medio**.

---

## TIER BAJO

### U-11 — `run_capture` enmascara `returncode is None` como `0` (éxito) · `[Z]` · Subprocesos/Zombies

- **Archivo:** `_process.py:141` — `... else 0`. Tras `communicate()` el returncode debería
  estar seteado; una race dejaría `None` → se sustituye por 0 (éxito).
- **Fix:** devolver `-1` (centinela), consistente con `dyndolod_runner.py:587`. Defensivo.

### U-12 — `run_dynamic_script` no borra el `.pas` temporal · `[A]` · Higiene

- **Archivo:** `xedit/runner.py:940-997` — `NamedTemporaryFile(delete=False, dir=output_dir)`
  sin `unlink`; el paso 6 del docstring ("Limpiar script temporal") está sin implementar.
  Cada patch/dynamic-run deja un `.pas` en `.skyclaw_backups/patches`.
- **Fix:** `finally: script_path.unlink(missing_ok=True)` (conservable bajo flag de debug).

---

## APÉNDICE — Ítems ZAI rebajados o rechazados (transparencia)

### R-1 — `_restore_backup` "gap destructivo / datos permanentemente perdidos" (ZAI Finding 3) → **REBAJADO a Bajo**

- **Archivo:** `_dir_rollback.py:141-147`.
- **Por qué se rebaja:** la ventana rmtree-luego-rename existe, pero **"datos perdidos" es
  falso**: si el `rename(backup → target)` falla, el backup **sobrevive** en su sibling
  `<name>.rollback-<nonce>` (nadie lo borra), `rollback_completed` queda `False`
  (señal correcta), y es recuperable a mano. El fix propuesto (restore a tmp y swap) agrega
  renames por beneficio marginal. Se puede plegar como nota menor dentro de U-08 (barrido de
  backups huérfanos).

### R-2 — `_apply_changes`: fix "aplicar a staging y swap del dir completo" (ZAI Finding 10) → **FIX RECHAZADO**

- **Archivo:** `profile_sandbox.py:366-444`.
- **Por qué se rechaza el fix:** un swap del directorio completo **rompe la drift-safety
  deliberada**. El promote aplica un *diff* (added/modified/removed) tocando solo los archivos
  del ritual y preservando cambios concurrentes del usuario/MO2 en la ventana de aprobación
  (verificado por `_check_drift`, `profile_sandbox.py:346-364`). Reemplazar el árbol entero
  por un staging pisaría esos cambios vivos — exactamente lo que el diseño per-archivo evita.
  La limitación (rollback multi-paso que puede fallar) es **conocida y documentada**, con el
  `rollback_dir` preservado para recuperación manual. Dejar como está; a lo sumo cubrir con el
  reconciliador de arranque de U-08.

---

## Notas de proceso (si se implementa)

- Rama: `claude/sky-claw-pipeline-audit-gzc4uo`. Un PR por cambio; no a `main`.
- Orden recomendado por impacto/riesgo: **U-01 → U-04 → U-02/U-03 → U-05 → U-06** ; luego el
  resto del tier Medio; higiene (U-11/U-12) como cierre.
- U-02 (Job Object) y U-01 (VFS) son los de mayor superficie: PRs propios.
- Gates de CI por cambio: `ruff check` + `ruff format --check` + `mypy sky_claw/` + `pytest`
  (tests/comentarios en español, TDD rojo→verde). Verificación end-to-end por ítem en la
  sección homónima del informe base.
