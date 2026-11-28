# NGIO P1 — Veredicto de generación, integridad del resultado y watchdog (concepts brief)

> **Fecha:** 2026-11-24 · **Base auditada:** `origin/main` = `3d3de41eee7fb012ef97f8c12cb64301cf129b67`
> (squash de P0 SAFE CACHE, PR #1008) · **Worktree:** `.worktrees/ngio-p1-brief` (detached).
> **Estado:** `CODE_READY_FOR_P1_IMPLEMENTATION_APPROVAL`. **No se modificó código productivo.**
> **Veredicto de esta entrega:** diseño y evidencia, **no** implementación, **no** certificación.

Este documento es la salida de las FASES A y B más el diseño de C y D. No crea PR, no toca el
árbol productivo y no instala nada. Todo hallazgo cita `archivo:línea` del árbol de `main` o una
fuente primaria con URL.

---

## 1. Preflight — estado real de `main`

### 1.1 Repositorio, HEAD y worktrees

| Dato | Valor verificado |
|---|---|
| Remoto | `https://github.com/FacundoSu1986/Sky-Claw.git` |
| `origin/main` | `3d3de41eee7fb012ef97f8c12cb64301cf129b67` — `fix(grass-cache): proteger el caché NGIO antes del lanzamiento (#1008)` |
| Squash de P0 en `main` | **Confirmado**: `git merge-base --is-ancestor 3d3de41e origin/main` → 0; un solo padre (`a23369a4`) |
| Worktree principal (`E:/Skyclaw_Main_Sync`) | `e76c1154`, **29 commits atrás** de `origin/main`; árbol con ~85 directorios untracked ajenos. **No se tocó** |
| Worktree de esta auditoría | `.worktrees/ngio-p1-brief`, detached en `3d3de41e`, árbol limpio |
| Otros worktrees registrados | ~80 (ver §9). **Ninguno se movió, borró ni leyó para mutar** |

El worktree principal está atrasado y su árbol no es representativo de `main`; **toda** la
lectura de código de este brief se hizo sobre `3d3de41e` en un worktree propio, conforme a
`AGENTS.md` («Una sesión, un worktree»).

### 1.2 CI post-merge de P0 (gates vigentes de `main`)

`gh run list --commit 3d3de41e` (2026-11-24):

| Workflow | Resultado |
|---|---|
| `Sky-Claw CI/CD` (21:08:25Z) | **success** |
| `CodeQL` (21:08:26Z) | **success** |
| `Main en verde` (21:25:47Z) | **success** |
| `Qodo Merge - Revisor Adversarial` (×5) | success / skipped (solo `pull_request`) |

**Conclusión:** `main` está verde en CI. Los gates vigentes siguen siendo: `ruff check` +
`ruff format --check` sobre `sky_claw/ scripts/ tests/`; `mypy sky_claw/ scripts/` (bloqueante,
con lista de exentos); shards de Windows 1..6 + POSIX Core; CodeQL; Security Scan; `Main en verde`;
`Regression & Test Oracle`.

**Módulos del write-set NO exentos de `mypy`.** `pyproject.toml:352-356` lista
`grass_cache_runner`, `grass_cache_safety`, `grass_cache_service`,
`tool_strategies/analyze_grass_prerequisites` y `tool_strategies/generate_grass_cache` en el bloque
estricto (`ignore_errors = false`), que excluye la exención de `sky_claw.local.tools.*`
(`pyproject.toml:280`). Un módulo nuevo bajo `local/tools/` **hereda la exención** y hay que
agregarlo a mano a la lista estricta — está en el write-set.

### 1.3 PRs abiertos y colisión

`gh pr list --state open` → **1 PR**: **#1021** `feat/bodyslide-pr2c1-siembra` (`MERGEABLE`,
base `main`), archivos: `app_context.py`, `bodyslide_*` (5 módulos), `docs/pending_ooda_status.md`,
1 spec y 7 tests de bodyslide.

**Colisión directa: ninguna.** **Colisión de contrato: ninguna.** **Colisión de archivo:**
`docs/pending_ooda_status.md` (documental) y `app_context.py` (P1 no lo toca). Si el cierre de P1
exige anotar `pending_ooda_status.md`, ese hunk se resuelve al integrar, no antes.

### 1.4 Baseline de tests medido en este entorno (Windows, Python 3.11.9, venv del repo)

| Corrida | Resultado |
|---|---|
| `test_grass_cache_{runner,safety,service,strategy}.py` + `test_grass_profile.py` | **396 passed, 21 skipped** (32.74 s) |
| los anteriores + `tests/bdd/` (re-verificado al cierre de esta revisión) | **409 passed, 21 skipped** (33.04 s) |
| Anclas: `test_borrado_recursivo`, `test_distributed_locks`, `test_worktree_convention_invariant`, `test_completion_policy_invariant`, `test_merge_conflict_standard`, `test_etapas_previas_lods` | **197 passed, 5 skipped, 1 failed** |

El único rojo es `test_los_worktrees_no_principales_viven_bajo_la_convencion`, y **no es un defecto
de `main`**: el registro de git de esta máquina tiene
`E:/Sectores/Sector_Proyectos_WorkBuddy/Skyclaw_Worktree` (`workbuddy/main-d17cd082`) cuya huella
canónica es `sha256:801f5825…` mientras la entrada congelada en `AGENTS.md` es `sha256:7a80af38…`
(medido con `_normalizar`/`_huella` del propio test). O sea: la ruta se movió —o el binding se
computó en otra ubicación— y la exención ya no la cubre. Es un worktree **ajeno** que su dueño debe
migrar o retirar; CI no lo ve (CI está verde, §1.2). No se tocó. Queda declarado como
**base de comparación** para FASE F: cualquier corrida local de esa ancla en esta máquina seguirá
roja por esta causa, y no puede usarse como evidencia de regresión.

### 1.5 Arquitectura efectiva del subsistema Grass Cache (verificada, no heredada)

```
FASE A (read-only)      grass_cache_service.analyze_prerequisites → xedit/grass_analyzer
FASE B (mutación MO2)   mo2/grass_profile.create_clone_profile + build_config_mod
                          └─ escribe mods/<config>/SKSE/Plugins/GrassControl.ini ([GrassConfig]
                             Only-pregenerate-world-spaces) y SSEDisplayTweaks.ini
PRE-FLIGHT (P0)         grass_cache_safety.preparar_arranque → PlanArranque (new|resume|rebuild)
                          └─ respaldo verificado sha256, progreso archivado por ámbito
FASE C (crash-loop)     grass_cache_runner.GrassCacheRunner.run
                          ├─ aplicar_plan (sección crítica no cancelable, B11)
                          ├─ _autorizar_lanzamiento (única puerta, 6 invariantes)
                          ├─ mo2.launch_game(perfil clon) → SkyrimSE.exe
                          ├─ _vigilar (cancel | flag | deadline | disco | pid vivo)
                          └─ finally: neutralizar_flag (archiva progreso + borra flag), drenado
FASE D (teardown)       grass_cache_service._teardown_best_effort + journal
```

Piezas y contratos alcanzados:

| Pieza | Rol | Consumidores |
|---|---|---|
| `local/tools/grass_cache_runner.py` (1291 líneas) | supervisor del crash-loop, único productor de `GrassCacheRunResult` | `grass_cache_service` |
| `local/tools/grass_cache_safety.py` (1777) | contrato de arranque P0 + inventario + respaldo + archivado de progreso | runner y servicio |
| `local/tools/grass_cache_service.py` (821) | orquestador Fases B→D, locks `grass-cache`→`load-order`, journal, eventos | `tool_dispatcher` vía strategy |
| `local/mo2/grass_profile.py` (669) | clon de perfil + mod de config | servicio |
| `app/orchestrator/tool_strategies/generate_grass_cache.py` (90) | adaptador HITL; `execute` devuelve el dict del servicio **sin reinterpretarlo** | `tool_dispatcher.py:338` |
| `local/validators/etapas_previas_lods.py` (313) | precondición de etapa 9: «existe algún `.cgid`» | `generate_lods` / tool remoto |
| `app/db/journal.py` | `begin_transaction(description, mod_id, agent_id)` — **sin** metadata; `begin_operation(..., metadata=)` (l.2337), `complete_operation`/`fail_operation`, `commit`/`rollback` | servicio |
| Eventos | `pipeline.grass_cache.{started,progress,completed}` | **ningún suscriptor en el árbol** (F-12 confirmado) |

Locks: `SnapshotTransactionLock` sobre `grass-cache` y `load-order`, en ese orden fijo
(`grass_cache_service.py:412-437`), con heartbeat en el event loop — por eso el hashing del caché
corre en hilo (§4.3).

---

## 2. Hallazgos reconciliados (CLAIM → EXTRACT → DOUBT → RECONCILE)

### 2.1 F-02 — Falso éxito de generación: **CONFIRMADO** (y algo peor de lo reportado)

**Claim:** `_salida_exito()` considera suficiente «flag ausente + hay `.cgid` + bytes > 0».

**Extract:**

- `grass_cache_runner.py:819-843` — `_salida_exito` hace `snap = await self._snapshot_cache()` y
  `if snap[0] > 0 and snap[1] > 0: return self._resultado("completed", "", ... success=True)`.
  No compara con el baseline, no mira `.fail`, no mira `.dgid`, no mira procedencia.
- `grass_cache_runner.py:1010-1021` — `_scan_cgid_sync` cuenta **sólo `*.cgid` bajo
  `overwrite_grass_dir`**, `stat().st_size`.
- El baseline existe pero **sólo se usa para el stall post-crash** (`:692-697`), nunca para el éxito.
- `_salida_exito` se alcanza desde **tres** llamadores: ventana de spawn «éxito temprano»
  (`:661-667`), post-crash (`:699-705`) y el bucle de `_vigilar` (`:792-793`). Es el **único**
  productor del veredicto de éxito, así que cambiar la política ahí cubre los tres caminos.

**Doubt:** ¿el baseline vacío está garantizado? **Sí, para `new`**: `planificar_arranque` rechaza
`cache_existente_sin_decision` si `inventario.tiene_cache` (`grass_cache_safety.py:1387-1408`), y el
runner **revalida** justo antes del spawn (`grass_cache_runner.py:951-958`). Para `rebuild` el caché
puede ser no vacío y la premisa es que NGIO lo borra en `Init()`; si NGIO no lo borra, sobreviven
archivos heredados y **hoy nadie lo detecta**. Para `resume` el caché es no vacío por definición.

**Nuevo defecto (no estaba en la auditoría):** el resultado **no distingue** «generación terminada»
de «caché certificado» en ningún campo. `success=True` + `outcome="completed"` + `cgid_count>0` es
hoy la única señal, y la produce igual un `resume` que no escribió nada que un `new` que generó todo.

**Reconcile:** confirmado. Consecuencias a cubrir: caché preexistente, caché parcial, `.fail`,
archivos vacíos, desaparición del flag por causa distinta de `End()`, perfil/instalación distinta, y
consumidores que leen «hay algún `.cgid`» como éxito (§2.3).

### 2.2 F-05 — Proceso vivo sin progreso: **CONFIRMADO**, y con un mecanismo más concreto

**Extract:**

- `grass_cache_runner.py:780-817` (`_vigilar`): el bucle sólo evalúa cancelación, existencia del
  flag, deadline global (`max_runtime_s`, default **12 h**, `:173`), disco libre y `pid_alive`.
  **No hay ninguna noción de progreso.**
- `stall_streak` se calcula **sólo después de un crash** (`:691-697`) y compara
  `_snapshot_cache()` contra el anterior; `stall_threshold` (default 5) acota relanzamientos, **no**
  un proceso colgado.
- Camino hermano: la ventana de spawn (`:626-660`) tampoco tiene watchdog de progreso; su cota es
  `spawn_window_s` (120 s) y el deadline global.

**Doubt:** ¿NGIO ya detecta congelamientos? **Sí, pero no alcanza.** `GidFileCache.cpp` (tag 1.0.13)
tiene `GidFileGenerationTask::run_freeze_check()`, que cada 60 s de inactividad ejecuta:

```cpp
RE::DebugMessageBox("Grass generation appears to have frozen! Restart the game.");
stl::report_and_fail("Grass generation appears to have frozen! Restart the game.");
```

`RE::DebugMessageBox` **abre un diálogo modal**: si nadie lo cierra, `report_and_fail` no corre, el
proceso **sigue vivo con PID válido** y el runner lo ve como «juego sano» hasta agotar las 12 h. El
escenario del `_vigilar` no es hipotético: es el estado natural de un run desatendido después de
que NGIO mismo detecte el freeze.

**Reconcile:** confirmado y agravado. El watchdog tiene que mirar progreso observable, no CPU/RAM, y
su reacción debe ser acotada (FASE D).

### 2.3 F-12 — Diagnóstico insuficiente: **CONFIRMADO**

Qué llega hoy al resultado (y por lo tanto al journal, la strategy y el bus):

- `GrassCacheRunResult` (`:242-272`): `success, message, outcome, crash_count, cgid_count,
  cache_size_mb, elapsed_s, cancelled, stalled, mode, backup_dir, flag_residual, progreso_perdido,
  flag_alternativo_residual, limpieza_errores`.
- Servicio (`:761-792`): copia **todos** esos campos **menos `limpieza_errores`**; agrega
  `duration_seconds`; cuando hay `error_msg` conserva **sólo** `outcome` y `cgid_count` (`:769-773`).
- Journal: `begin_transaction("Grass precache (NGIO, Stage 8)")` **sin metadata**; commit o rollback
  según `exito`; el teardown incompleto se anota como operación fallida con
  `TEARDOWN_INCOMPLETE_KIND` (`:687-731`).
- Eventos: `started` (worldspaces, conflicting_mods), `progress` (foto), `completed`
  (`success`, `outcome`, `duration_seconds`). Sin suscriptores en el árbol.

Lo que **falta** para explicar generación terminada / parcial / stall / crash / timeout / fallos de
NGIO / estado desconocido / limpieza incompleta:

1. identidad de la corrida (perfil de origen, perfil de lanzamiento, `game_path`, raíz de estado);
2. **worldspaces solicitados** — hoy no viajan al runner: `_build_runner_config`
   (`grass_cache_service.py:606-632`) no los pasa y `GrassCacheConfig` no los tiene. El único
   registro es el `GrassControl.ini` del mod de config, que **el teardown borra**;
3. inventario **antes** y **después**, con archivos nuevos / heredados / modificados;
4. `.fail`: **no se leen ni se cuentan** (el inventario los cuenta, `InventarioCache.fail_count`,
   pero nadie los interpreta ni los distingue de los históricos);
5. evidencia de progreso dentro de la corrida (hoy sólo hay `cgid_count` al final);
6. terminalidad del proceso (hoy se asume: se mata y se sigue);
7. límites del veredicto: `cobertura_usvfs: no_verificada` vive en el **plan**, no en el resultado.

### 2.4 Reproducción con tests (FASE B, medida sobre `3d3de41e`)

Tests escritos **antes** de tocar nada, en un archivo de trabajo **untracked**
(`tests/test_ngio_p1_reproduccion.py`, semilla de la suite de FASE E). Los cinco están **rojos**
con la causa correcta:

| Test | Rojo observado | Defecto que prueba |
|---|---|---|
| `test_f02_cache_preexistente_sin_evidencia_no_se_certifica` | `AttributeError: 'GrassCacheRunResult' object has no attribute 'verdict'` | no existe campo que separe terminación de certificación |
| `test_f02_fail_historico_no_se_atribuye_pero_impide_certificar` | ídem | `.fail` no participa del veredicto |
| `test_f02_fail_nuevo_de_esta_corrida_impide_certificar` | ídem | ídem |
| `test_f05_pid_vivo_sin_progreso_corta_antes_del_deadline` | `AssertionError: assert 'timeout' == 'stalled'` | el freeze consume el deadline |
| `test_f12_el_resultado_lleva_worldspaces_y_evidencia` | `TypeError: GrassCacheConfig.__init__() got an unexpected keyword argument 'worldspaces'` | no hay identidad ni worldspaces en el resultado |

**Comportamiento actual medido con una sonda temporal** (borrada; los números son de esta máquina,
Windows, Python 3.11.9):

| Escenario | Resultado de HOY |
|---|---|
| `rebuild` sobre un `.cgid` preexistente; el juego borra el flag sin escribir nada | `outcome='completed'`, **`success=True`**, `cgid_count=1`, **`message=''`** |
| ídem + `.fail` con contador `2` (celda salteada por `MaxFailures`) | `outcome='completed'`, **`success=True`**, `message=''` |
| Juego inmortal sin progreso (`max_runtime_s=0.6`) | `outcome='timeout'`, `elapsed_s=0.609`, `stalled=False` |
| Campos de `GrassCacheConfig` | 18 campos, **sin `worldspaces`** |
| Campos de `GrassCacheRunResult` | 15 campos, **sin `verdict`/`certificado`/`evidencia`** |

El primer caso es la evidencia más fuerte de F-02: un run que **no escribió ni un byte** declara
éxito y deja `message` **vacío** — el contrato canónico de «no hay nada que reportar».

### 2.5 Anclaje actual del comportamiento (pre-validación de mutantes)

Corrida sobre un worktree descartable en `origin/main` (`tests/test_grass_cache_{runner,service,
strategy}.py` = **292 passed** de baseline), con dos mutantes aplicados al runner y restaurados
después:

| Mutante | Qué rompe | Tests que lo matan |
|---|---|---|
| **MA** — `_salida_exito` sin el postcheck `count>0 and bytes>0` | el único chequeo que existe hoy | **2**: `test_flag_ausente_con_cero_cgid_es_fallo_silencioso`, `test_cgid_todos_vacios_es_fallo_silencioso` |
| **MD** — exigir además que el `.cgid` haya crecido en esta corrida | el «éxito sobre caché preexistente» | **1**: `test_flag_borrado_justo_antes_del_crash_no_relanza` |

**Lectura:** hoy lo único anclado es «caché vacío ⇒ no es éxito». Ninguna prueba cubre procedencia,
`.fail`, freeze con PID vivo ni identidad; y **un solo test existente** (MD) fija el comportamiento
que P1 cambia, así que el costo de actualizar la suite es acotado y está localizado.

> **Trampa encontrada en el propio arnés (queda como advertencia para FASE E):**
> `Path.write_text` en Windows traduce `\n` → `\r\n`; al restaurar el archivo mutado git lo vio
> modificado. Todo script que reescriba un módulo o un fixture debe usar `write_bytes` /
> `newline=""`. Es el mismo defecto que P0 documentó en su §12.1 y que ya tiene ancla
> (`test_los_fixtures_del_flag_escriben_bytes_y_no_texto`).

### 2.6 Corrección de un error del propio brief (DOUBT → RECONCILE)

La primera versión de este documento afirmaba que el veredicto podía viajar en
`begin_transaction(..., metadata=)`. **Es falso**: `metadata` (`journal.py:687`) es un campo del
dataclass `JournalEntry`, `begin_operation` lo acepta como parámetro (`journal.py:2337`) y
`Transaction` no tiene ese campo — `begin_transaction` sólo recibe `description`, `mod_id` y
`agent_id`. Consecuencias, ya aplicadas a §1.5, §4.5, §6, §7.2, §8, §10.3 y §12:

1. el veredicto se registra como **operación** dentro de la TX (`begin_operation` +
   `complete_operation`/`fail_operation`), que es el precedente U-09 (`TEARDOWN_INCOMPLETE_KIND`);
2. su discriminador va a `sky_claw/app/db/journal_contracts.py`, la única fuente de verdad de los
   `kind`;
3. esa metadata **no** puede llevar `files_touched`: `_rutas_de_metadata` alimenta el oráculo de
   recovery y no se debe sembrar evidencia falsa de artefactos.

Se deja registrado porque es el tipo de error que este repo castiga (contrato supuesto sin leer la
firma real) y porque cambia el write-set.

---

## 3. Fuente primaria de NGIO (lo que se puede y lo que no se puede afirmar)

Verificado leyendo el código publicado (todas las citas con URL en el informe de research de esta
misión; se resumen acá los hechos que el contrato usa).

| Hecho | Fuente | Estado |
|---|---|---|
| El flag de progreso se abre **append** y se escribe **una línea por celda** (`cell_<ws>_<x>_<y>`) y **una por worldspace terminado** (`ws_<EditorID>`) | `src/GidFileCache.cpp` tag `1.0.13`, `GidFileGenerationTask::WriteProgressFile` | **VERIFICADO en fuente** |
| **Sin `flush()` por línea** (`"\n"`, no `std::endl`); el flush ocurre al llenarse el buffer del `ofstream` o en `Free()`/`End()` | ídem, `Free()` | **VERIFICADO en fuente** |
| `End()` **borra** el archivo de progreso; no escribe marcador de completitud | ídem, `GidFileGenerationTask::End()` | **VERIFICADO en fuente** |
| Con el progreso **vacío o ausente**, `Init()` borra **todos** los `.dgid`/`.cgid`/`.fail` de `Data/Grass` | ídem, `Init()` | **VERIFICADO en fuente** (base del HOLD de P0) |
| `.fail` **no es** «celda falló»: es un **contador de intentos por celda** (`<n>\n`), se escribe **antes** de generar, se **borra** cuando la celda termina bien, y si `n >= Config::MaxFailures` (default **2**) la celda se **saltea** y el `.fail` **persiste** | ídem, `GidFileCellGenerateTask::Process()` + `include/GrassControl/Config.h` | **VERIFICADO en fuente** |
| Nombre de artefacto por **celda**: `Grass\<WorldspaceEditorID>x<XXXX>y<YYYY>.cgid` (`.dgid` para LOD) | ídem, hook `IsLodCell()` (`"Grass\\\\%sx%04dy%04d.cgid"`) | **VERIFICADO en fuente** |
| «Terminó» se señala con `Print` + `report_and_fail("Grass generation finished successfully!")`: el juego **aborta** al completar | ídem, `GidFileGenerationTask::Update()` | **VERIFICADO en fuente** |
| Freeze propio: 60 s sin `_lastDidSomething` → `RE::DebugMessageBox` + `report_and_fail` | ídem, `run_freeze_check()` | **VERIFICADO en fuente** |
| Log `NGIO-NG.log` en `%Documents%/My Games/Skyrim Special Edition/SKSE/`, con `flush_on(info)` — progreso por celda en tiempo real | `src/Main.cpp::InitializeLog()` | **VERIFICADO en fuente** |
| La release «latest» del repo es **1.0.13** (2024-04-30); `1.0.17` y `1.0.17b3` existen como **prerelease** y `/releases/latest` **las excluye**; `main` está en **2.3.0** sin tag ni release | GitHub API, consultada 2026-11-24 | **VERIFICADO** |
| NGIO 2.x (`main`, sin release): el borrado de `Init()` queda bajo `if (!Config::Updating)` | `main:src/GidFileCache.cpp` | **VERIFICADO en fuente; semántica de `Config::Updating` NO VERIFICADA** |
| NGIO original (Nexus 42161) es **otro codebase** (port reescrito); sin fuente pública accesible; su comportamiento (borrado, `.fail`) | README de NGIO-NG + Nexus (API 401) | **NO VERIFICADO** |

### 3.1 Consecuencias directas para el diseño

1. **`ws_<EditorID>` es un oráculo parcial de worldspaces**: dice qué worldspaces NGIO **declaró
   terminados**. Es un **límite inferior** (el archivo está bufferizado y desaparece al final), no
   una prueba de cobertura total.
2. **`.cgid` por celda**: contar `.cgid` **no** se puede comparar con «worldspaces esperados». Sí se
   puede agrupar por prefijo (worldspace) y detectar worldspaces **sin ningún artefacto**.
3. **`.fail`**: un `.fail` presente al final del ritual = una celda que **no** se completó (o que se
   salteó por superar `MaxFailures`). Un `.fail` **borrado** durante la corrida = celda completada.
   El contenido es un entero: la atribución «¿este intento lo tocó?» se decide comparando
   **contenido** contra el baseline, no el `mtime`.
4. **No hay oráculo de completitud de celdas.** El contrato debe decirlo, no inventarlo.
5. **El flag ausente sigue siendo necesario pero no suficiente** — y su desaparición tiene causas
   distintas de `End()` en Sky-Claw mismo (§7.3).

---

## 4. Contrato de veredicto propuesto (FASE C)

### 4.1 Dos ejes separados, nunca confundidos

| Eje | Campo | Pregunta que contesta |
|---|---|---|
| **Terminación** | `outcome` (sin cambios: 10 códigos) | ¿cómo terminó el ritual? |
| **Certificación** | **`verdict`** (nuevo) + **`certificado`** (bool) | ¿qué se puede afirmar del caché que quedó? |

`success` conserva su significado actual («el ritual produjo un caché y la limpieza fue completa»)
**sin** convertirse en certificación, y todo resultado **no certificado** lo dice en `message`.
Alternativa evaluada y descartada abajo (§4.7).

### 4.2 C1 — Identidad de la ejecución (reutilizando lo que ya existe)

No se crea un sistema de identidad: se propaga lo que ya está en `GrassCacheConfig` + lo que falta.

| Evidencia | Origen | Estado |
|---|---|---|
| modo `new/resume/rebuild` | `GrassCacheConfig.mode` | ya existe |
| perfil de **lanzamiento** (clon) | `GrassCacheConfig.profile` | ya existe |
| perfil MO2 de **origen** (identidad del progreso) | `GrassCacheConfig.perfil_de_progreso` | ya existe |
| `game_path`, `overwrite_grass_dir`, `backup_root` | `GrassCacheConfig` | ya existe |
| respaldo verificado | `PlanArranque.respaldo` → `ruta_de_respaldo(plan)` | ya existe |
| inventario previo | **`PlanArranque.inventario`** (ya se calcula en el preflight) | ya existe, **no se usaba** |
| inventario posterior | `_scan_cgid_sync` + `InventarioCache` | ya existe |
| worldspaces solicitados | `GenerateGrassCacheParams.worldspaces` → **nuevo campo** `GrassCacheConfig.worldspaces` | **falta plumbing** |
| evidencia de progreso | flag (`ws_`/`cell_`), `.cgid`, `.fail` | **falta** |
| terminalidad | `psutil` + `create_time` | **falta verificar** |

**Atestación del load order efectivo y cobertura USVFS completa: NO se implementan (P2).** El
resultado declara explícitamente `cobertura_usvfs: "no_verificada"` y
`completitud_de_celdas: "no_verificada"`.

### 4.3 C2 — Evidencia de generación (barata, verificable, sin inventar marcadores)

| Indicador | Cómo se lee | Costo | Uso |
|---|---|---|---|
| `.cgid` / `.dgid` / `.fail` (nombre, tamaño) | 1 pasada `os.scandir` + `stat` | O(n) por muestra | inventario final, worldspaces con artefactos, stall |
| conteo de nombres `.cgid`/`.fail` (sin `stat`) | 1 pasada `os.scandir` | O(n) nombres | latido del watchdog |
| `flag.size` | 1 `stat` | O(1) | latido del watchdog |
| líneas nuevas del flag (`ws_`, `cell_`) | lectura **incremental** desde el último offset, tope por poll | O(bytes nuevos) | progreso y worldspaces declarados por NGIO |
| contadores `.fail` | lectura de archivos ≤ 16 B | O(#fail) | atribución de fallos |

Reglas de costo, explícitas:

- **Nada de hashing completo del caché en cada poll.** El hashing sigue reservado a P0
  (respaldo, archivado de progreso) y corre en hilo drenado.
- Las muestras del watchdog corren en `asyncio.to_thread` y **nunca** superan un tope de bytes por
  poll (`_TOPE_LECTURA_FLAG = 1 MiB`), así el event loop queda libre para renovar leases
  (`grass-cache` y `load-order`).
- `_snapshot_cache()` (glob+stat de todos los `.cgid`) **no** se mueve al poll fino: queda en el
  heartbeat (60 s) y en los cortes, como hoy.

### 4.4 C3 — Política de veredicto (única, pura y testeable)

Módulo nuevo `sky_claw/local/tools/grass_cache_verdict.py`, **sin I/O y sin asyncio**: recibe
evidencia y devuelve veredicto. Es la única pieza que decide; runner y servicio sólo la invocan y
propagan.

```python
GrassCacheVerdict = Literal[
    "completed_verified",     # generación terminada + caché atribuible + sin fallos atribuibles
    "completed_unverified",   # terminó; el caché NO se puede certificar
    "partial",                # no terminó y hay caché conservado (reanudable)
    "failed",                 # no terminó y no hay caché, o el contrato P0 lo cortó
    "cancelled",              # cancelación cooperativa/día duro
    "unknown",                # no se puede afirmar ni terminación ni estado del caché
]
```

`evaluar_veredicto(evidencia)` aplica, **en orden**, la tabla congelada:

| # | Condición | verdict | Código de razón |
|---|---|---|---|
| 1 | `inventario_final` ilegible | `unknown` | `cache_ilegible` |
| 2 | `terminalidad == "indeterminado"` | `unknown` | `terminalidad_no_verificada` |
| 3 | `outcome == "completed"` y `cgid_count > 0` y `cgid_bytes > 0` | evalúa 4–9 | — |
| 4 | `outcome == "completed"` y caché vacío | `failed` | `cache_vacio` |
| 5 | `outcome` en `{stalled, timeout, max_restarts, disk_full}` | `partial` si hay caché, si no `failed` | `sin_certificacion: <outcome>` |
| 6 | `outcome` en `{spawn_failed, flag_sin_progreso, flag_alternativo_presente, cache_cambio_despues_del_plan}` | `failed` | `outcome_no_completado:<outcome>` |
| 7 | `outcome == "cancelled"` | `cancelled` | `cancelado` |
| 8 | `limpieza_limpia is False` | nunca `completed_verified` → `completed_unverified` | `limpieza_incompleta` |
| 9 | procedencia y fallos (§4.4.1) | `completed_verified` o `completed_unverified` | códigos |

Nunca se degrada a la inversa: un veredicto **nunca** sube por una capa superior. `certificado` es
exactamente `verdict == "completed_verified"`.

#### 4.4.1 Procedencia y fallos — las dos reglas que cierran F-02

**R-procedencia** (¿el caché que quedó es de ESTA corrida?):

| Modo | Baseline garantizado por P0 | Regla | Si falla |
|---|---|---|---|
| `new` | caché **vacío** (revalidado antes del spawn) | **todo** archivo del inventario final es de esta corrida | — |
| `rebuild` | caché puede ser no vacío; NGIO lo borra en `Init()` | **ningún** archivo final coincide `(nombre, tamaño)` con el baseline | `archivos_heredados_sin_procedencia` |
| `resume` | caché no vacío + progreso válido preservado/restaurado | exige **alguna** evidencia de avance de esta corrida: archivo nuevo/modificado **o** progreso observado (flag creció / `ws_` nuevo / `.fail` borrado) | `sin_evidencia_de_esta_corrida` |

`resume` **no exige crecimiento arbitrario**: un resume casi completo que sólo borra el flag es
válido si el progreso se observó o si algún archivo cambió. Si no hay **ninguna** evidencia, el
veredicto es `completed_unverified` con `sin_evidencia_de_esta_corrida` — no un fallo.

**R-fallos** (`.fail`), alineada a la semántica real (§3):

| Situación | Atribución | Efecto |
|---|---|---|
| `.fail` final **no** estaba en el baseline | **de esta corrida** | bloquea certificación (`fallos_de_celda_atribuibles`) |
| `.fail` en ambos con **contenido distinto** (contador subió) | **de esta corrida** | bloquea certificación (`fallos_de_celda_atribuibles`) |
| `.fail` en ambos con **mismo contenido** | **histórico**, no atribuible | **no** se cuenta como de este intento; igual impide certificar (`fallos_de_celda_historicos`) porque el caché tiene una celda que nunca completó |
| `.fail` del baseline **desapareció** | evidencia positiva de completitud de esa celda | cuenta como avance |

En **ningún** caso se borra, mueve ni oculta un `.fail`.

**R-worldspaces** (sólo si `worldspaces` solicitados no está vacío): si el inventario final tiene
algún pedido **sin artefactos** → `worldspaces_sin_artefactos` (bloquea). Los worldspaces
**declarados por NGIO** (`ws_` en el flag) se reportan como evidencia y **no** bloquean: el archivo
está bufferizado y su cola puede no haberse observado.

#### 4.4.2 Qué significa exactamente `completed_verified` (y qué no)

**Significa:** el ritual terminó por el camino de `End()` de NGIO, el caché presente es atribuible
íntegramente a esta corrida (o hubo avance demostrado en `resume`), no quedó ningún `.fail`, todos
los worldspaces solicitados tienen al menos un artefacto y la limpieza del flag fue completa.

**NO significa:** que cada celda del mundo tenga su `.cgid` (`completitud_de_celdas:
no_verificada`), que el load order efectivo esté atestado ni que la USVFS esté cubierta
(`cobertura_usvfs: no_verificada`, P2). Van en el resultado como evidencia explícita y **no** se
prometen. [`alcance_certificado: "sin_huecos_conocidos"` — ver RD-12 en §14.]

#### 4.4.3 Ciclo de vida del veredicto (RD-11, §14)

El veredicto **no** puede ser final cuando se construye el resultado: `limpieza_limpia` sólo se
conoce después de `neutralizar_flag`, que corre en el `finally` de `_run_interno`. Se resuelve en dos
fases: `_salida_exito` guarda `self._evidencia_egreso` y devuelve un **provisional** que **nunca** se
publica; `run()` recalcula con `evaluar_veredicto(evidencia, limpieza_limpia=neutralizacion.limpio)`
antes de devolver o de propagar la excepción. Una limpieza incompleta **jamás** deja
`completed_verified`, y eso se ejerce a través de `run()` (test #11).

### 4.5 C5 — Propagación (verificada en código)

- **Runner → servicio:** `_componer_resultado` (`:761-792`) copia campo por campo → hay que agregar
  `verdict`, `certificado`, `verdict_razones`, `evidencia` y `limpieza_errores` (hoy se pierde).
  En la rama `error_msg` (`:769-773`) hoy se pierden `crash_count`, `mode`, `backup_dir`, etc.: se
  conserva lo que hay y se agrega el veredicto, sin cambiar la prioridad del error.
- **Servicio → journal:** **corregido en esta revisión** (ver §2.6). `begin_transaction` **no**
  acepta `metadata` (`journal.py:2337` es `begin_operation`; `Transaction` ni siquiera tiene ese
  campo). El canal real es una **operación** dentro de la TX, que es el precedente U-09:
  `begin_operation(..., metadata={"kind": GRASS_VERDICT_KIND, "verdict": ..., "certificado": ...,
  "razones": [...], "mode": ..., "worldspaces": [...]})` + `complete_operation(entry_id)` si el
  veredicto certifica, o `fail_operation(entry_id, error="no se pudo certificar: …")` si no. Esa
  metadata **no** lleva `files_touched`, así que `_rutas_de_metadata` (`journal.py:79`) devuelve `()`
  y el oráculo de recovery queda intacto. `exito` sigue mandando el commit/rollback: **un resultado
  `failed`/`unknown` no commitea**; un `completed_unverified` **sí** commitea, con la operación
  fallida que dice literalmente por qué no se certificó (hoy el commit no distingue).
- **Servicio → bus:** `pipeline.grass_cache.completed` agrega `verdict` y `certificado`.
- **Strategy:** `execute` devuelve el dict sin tocarlo (verificado, 90 líneas) → **sin cambios**;
  entra en el plan de tests para anclar que no reinterpreta nada.
- **El `message` de no-certificación lo emite el RUNNER, no el servicio** (RD-13, §14). Verificado:
  `tests/bdd/step_defs/test_grass_cache_steps.py:115-119` exige `success is True` **y**
  `message == ""` sobre el dict del servicio, con un `GrassCacheRunResult` falso construido por el
  harness (`tests/bdd/support/grass_cache_harness.py:109-117`) que **no** pasa por el ensamblado del
  runner. Si el servicio reescribiera el mensaje para explicar que no se certificó, el BDD se pondría
  rojo por un contrato que no cambió. Por eso: el runner compone el mensaje con las razones; el
  servicio sólo propaga (su única reescritura sigue siendo la de limpieza incompleta, `:552-555`).
- **Validador de etapa 9 (`etapas_previas_lods._tiene_cgid`, `:77-95`):** **no se toca.** Es una
  **precondición** («existe al menos un artefacto de la etapa 8»), no una certificación de la
  generación; convertirlo en consumidor del veredicto exige un registro de corridas por etapa que
  no existe (deuda declarada en `sky_claw/local/AGENTS.md` §1). Se documenta como límite (🟡) y no
  se amplía el alcance.

### 4.6 Vocabulario cerrado de razones

`cache_ilegible`, `terminalidad_no_verificada`, `cache_vacio`, `limpieza_incompleta`,
`archivos_heredados_sin_procedencia`, `sin_evidencia_de_esta_corrida`,
`fallos_de_celda_atribuibles`, `fallos_de_celda_historicos`, `fallos_de_celda_no_evaluables`,
`baseline_ilegible`, `operacion_invalidada`, `worldspaces_sin_artefactos`, `outcome_no_completado`,
`sin_certificacion`, `cancelado`.

Se congela por igualdad literal en un test: una razón nueva rompe el ancla hasta que se declara.

**Añadidas por la revisión independiente** (ronda consolidada, ver §11 del audit de P1):

- `fallos_de_celda_no_evaluables` (H1): hay `.fail` sobrevivientes que no se pudieron leer. No se
  transforman en «no hay fallos» ni se les inventa contenido; **prohíben certificar**. La política
  tiene defensa en profundidad contra la discrepancia `fail_count` vs fallos inventariados.
- `baseline_ilegible` (H4): el inventario inicial no se pudo verificar. Sin procedencia demostrable
  el run **aborta antes de escribir el flag** y el veredicto es `unknown`. Un `FileNotFoundError` del
  directorio SÍ es «caché inexistente» (vacío legítimo), no ilegible.
- `operacion_invalidada` (H6): la certificación del runner se degrada cuando la **operación** quedó
  invalidada después (lease perdido, error posterior). La evidencia observada no se falsea; el
  journal y el evento usan el veredicto reconciliado.

### 4.7 RESUELTA (D1 del propietario): semántica de `success`

**Aprobada la opción A modificada.** `success` se mantiene como eje separado de `certificado`/
`verdict`, **pero deja de ser suficiente por sí mismo**:

1. **Presencia de caché + limpieza completa no bastan.** `success` exige además **evidencia de una
   finalización operacional legítima**: `outcome == "completed"` (camino de `End()` de NGIO) y
   **trabajo de esta corrida demostrado** (procedencia o avance, §4.4.1).
2. **Un caché preexistente sin evidencia de generación actual no puede producir un falso éxito**
   (`exito_operacional = False`, `verdict = "completed_unverified"`).
3. **Los consumidores que necesiten certificación deben mirar `verdict`/`alcance_certificado`**, no
   `success`.
4. **Un resultado no certificado no debe recomendar un REBUILD destructivo**: el mensaje informa y
   sugiere revisar la evidencia; nunca `rebuild`.
5. **No se conserva una semántica insegura para mantener verde un BDD histórico.** Si el contrato
   cambia, se actualizan los consumidores y sus tests con justificación.

Costo medido de la alternativa B (`success = certificado`): rompía
`tests/bdd/step_defs/test_grass_cache_steps.py:118` con el fake del harness. Queda descartada.

### 4.8 Contrato final de éxito, certificación y alcance (implementación)

```python
# en GrassCacheRunResult
success: bool          # == veredicto.exito_operacional   (D1)
certificado: bool      # == veredicto.certificado         (D1 + D2)
verdict: GrassCacheVerdict
verdict_razones: tuple[str, ...]
evidencia: dict[str, Any]
```

| Eje | Regla | Valores |
|---|---|---|
| `exito_operacional` | `outcome == "completed"` **y** `cgid_count>0` **y** `cgid_bytes>0` **y** `terminalidad == "terminado"` **y** limpieza completa **y** `trabajo_de_la_corrida == "demostrado"` | bool |
| `certificado` | `exito_operacional` **y** cero `.fail` (atribuible o histórico) **y** todo worldspace solicitado con artefactos | bool |
| `alcance_certificado` | qué parte del caché puede afirmarse | `"corrida_completa"` (new/rebuild: todo el caché es de esta corrida) · `"trabajo_de_la_corrida"` (resume: sólo lo que esta corrida trabajó) · `"ninguno"` |
| `trabajo_de_la_corrida` | `new`: baseline vacío ⇒ demostrado · `rebuild`: ningún archivo final coincide `(nombre,tamaño)` con el baseline · `resume`: archivo nuevo/modificado **o** avance de progreso observado **o** un `.fail` del baseline desaparecido | `"demostrado"` / `"no_demostrado"` |
| `integridad_observable` | lo único que se puede afirmar del conjunto | `"sin_fallos_conocidos"` / `"con_fallos_conocidos"` / `"no_evaluada"` |
| `completitud_global` | **nunca** se certifica en P1 (falta atestación de load order: P2) | `"no_verificada"` |

**D2 (RD-12) aprobada:** `resume` **puede** certificar, pero su `alcance_certificado` es
`"trabajo_de_la_corrida"` y `completitud_global` sigue `"no_verificada"`; la ausencia de fallos
conocidos **no** se presenta como completitud global. Sin evidencia de esta corrida →
`completed_unverified` con `alcance_certificado="ninguno"`.

---

## 5. FASE D — Watchdog de progreso con proceso vivo

### 5.1 Diseño

En `_vigilar()`, después de los cinco checks actuales y **antes** del sleep, se agrega la medición de
progreso. Estado por ciclo de lanzamiento (se reinicia en cada `launch_game`, porque cada proceso
tiene su propio arranque lento):

```
t_spawn_mono      = monotonic() al atribuir el juego
ultimo_avance     = t_spawn_mono
ultima_muestra    = (nombres_cgid, nombres_fail, flag_size, offset_flag)
```

Cada `progress_poll_interval_s` (default **30 s**), en un hilo:

1. `os.scandir(Grass/)` → cuenta de nombres `.cgid` y `.fail` (**sin `stat`**);
2. `flag.stat()` → `st_size` (una syscall);
3. lectura **incremental** del flag desde `offset_flag` (tope 1 MiB por poll) → busca líneas `ws_` y
   `cell_` nuevas.

Hay avance si **cambia cualquiera** de los tres. Si hay avance: `ultimo_avance = now`.

Corte por stall, sólo si **todas**:

- el proceso sigue vivo (`pid_alive` + identidad `create_time`, ya existente);
- `now - t_spawn_mono >= stall_grace_s` (default **1200 s**, arranque lento permitido);
- `now - ultimo_avance >= stall_progress_s` (default **600 s**);
- el deadline global **no** se adelanta: se conserva tal cual (`max_runtime_s`).

Reacción (acotada y observable):

1. `_matar_todo(game_pid)` — **una** vez, sin reintentos ni backoff;
2. verificación de terminalidad (§5.3);
3. `return _resultado("stalled", ...)` — **sin relanzar**: el guard P0 no autoriza un relanzamiento
   tras un freeze y no se amplía el presupuesto de relanzamientos;
4. el caché parcial **se conserva**; el `finally` archiva el progreso y neutraliza el flag como
   siempre; el resultado lleva `progreso_detenido_s`, `ultima_muestra` y `terminalidad`.

### 5.2 Por qué estos indicadores y no otros

- **Prohibido como prueba única:** CPU y RAM. Un proceso bloqueado en un `DebugMessageBox` puede
  seguir consumiendo CPU (spinner de UI) o nada; ninguno de los dos distingue «trabaja» de «colgado».
- **`flag.size` + nombres `.cgid`/`.fail` son señales del propio producto**: NGIO appende una línea
  por celda y el motor escribe un `.cgid` por celda (§3).
- **El buffer del flag** (§3) hace que el tamaño salte en bloques: por eso el corte exige 600 s sin
  **ningún** cambio y con gracia de 20 min; NGIO, que ve el detalle interno, usa 60 s. Nunca se puede
  llegar al corte antes que NGIO, y NGIO puede abortar antes sin que el watchdog se confunda.
- **`os.scandir` de nombres** evita `stat` por archivo: con 40 000 `.cgid` son ~40 ms en hilo cada
  30 s, no un hashing.
- **La lectura del flag es incremental y con tope**, y cualquier error de lectura degrada a «sin
  evidencia nueva» (nunca aborta el run).

### 5.3 Terminalidad (requisito 14)

Tri-estado, verificado **después del kill, con espera acotada y re-verificada** (`terminality_wait_s`,
default **10 s**; RD-10 en §14) y antes de construir el resultado:

| Resultado | Condición | Efecto |
|---|---|---|
| `terminado` | `psutil.pid_exists(pid)` es `False`, **o** el PID existe pero ya **no** es el juego atribuido (nombre/exe/`create_time` no coinciden → PID reusado) | sigue el veredicto normal |
| `vivo` | el PID existe y **es** el juego atribuido después del intento de kill | `verdict="unknown"`, `terminalidad_no_verificada`; **no** se relanza, **no** se muta nada más |
| `indeterminado` | no se pudo clasificar (AccessDenied, `psutil.Error`, exe ilegible) | `verdict="unknown"`, fail-closed |

La neutralización del flag **sí** ocurre también en `vivo`/`indeterminado` (dejar el flag en disco
mete al juego del usuario en modo precache y es el riesgo que P0 cerró); lo que se prohíbe es
**certificar** y **relanzar**. La evidencia de progreso se archiva igual (no es incompatible: no
borra ni restaura caché). Queda anotado como el único trade-off del watchdog: se prefiere el borrado
del flag con diagnóstico `unknown` antes que un flag residual.

### 5.4 Invariantes que el watchdog NO puede romper

1. Reloj **monotónico** para todas las esperas (`time.monotonic`), como hoy (`:472`, `:556`, `:794`).
2. Deadline global intacto: un stall corta **antes**, nunca extiende.
3. Los workers de `preparar_arranque`, `aplicar_plan` y `neutralizar_flag` siguen drenándose con
   `esperar_terminar` (`:288-334`); el watchdog no introduce `await` no drenado.
4. Cancelación cooperativa (`cancel_event`) y dura (`CancelledError`) se respetan: el poll usa
   `_sleep_or_cancel` (`:872-876`), así una cancelación durante el poll despierta al instante.
5. Identidad del proceso: `_kill_game_tree_sync` ya revalida nombre+exe+`create_time` (`:1143-1171`);
   no se mata ningún proceso que no pase esa verificación.
6. **No se toca el presupuesto de relanzamientos** ni el guard de P0 (`_autorizar_lanzamiento`).

---

## 6. Matriz de resultados (tabla que se congela en un test)

| `outcome` | Caché final | `verdict` | `certificado` | Journal (operación `GRASS_VERDICT_KIND`) |
|---|---|---|---|---|
| `completed` | atribuible, sin `.fail`, worldspaces OK, limpieza limpia | `completed_verified` | `true` | commit + operación **completada** |
| `completed` | atribuible pero con `.fail` histórico | `completed_unverified` | `false` | commit + operación **fallida** con razones |
| `completed` | no atribuible (`rebuild` con heredados) | `completed_unverified` | `false` | ídem |
| `completed` | vacío | `failed` | `false` | rollback (sin operación de veredicto) |
| `stalled`/`timeout`/`max_restarts`/`disk_full` | con archivos | `partial` | `false` | rollback |
| ídem | vacío | `failed` | `false` | rollback |
| `spawn_failed`/`flag_sin_progreso`/`flag_alternativo_presente`/`cache_cambio_despues_del_plan` | cualquiera | `failed` | `false` | rollback |
| `cancelled` | cualquiera | `cancelled` | `false` | rollback |
| cualquiera | ilegible o terminalidad no verificada | `unknown` | `false` | rollback |

---

## 7. Write-set exacto (propuesto, sujeto a aprobación)

### 7.1 Archivos nuevos

| Archivo | Contenido | Líneas aprox. |
|---|---|---|
| `sky_claw/local/tools/grass_cache_verdict.py` | `GrassCacheOutcome` (movido desde el runner, re-exportado), `GrassCacheVerdict`, `CodigoVeredicto`, `CacheObservado`, `EvidenciaVeredicto`, `VeredictoCache`, `evaluar_veredicto()`. **Puro: sin I/O, sin asyncio, sin psutil.** | ~230 |
| `tests/test_grass_cache_verdict.py` | matriz completa + vocabulario congelado + mutantes M1/M2/M4 | ~320 |
| `docs/audits/2026-11-24_ngio_p1_veredicto.md` | auditoría de P1: fuente primaria, decisiones, gates y límites | ~200 |

### 7.2 Archivos modificados

| Archivo | Cambio | Riesgo |
|---|---|---|
| `sky_claw/local/tools/grass_cache_runner.py` | config (`worldspaces`, `progress_poll_interval_s`, `stall_progress_s`, `stall_grace_s`, `terminality_wait_s`) + validación; watchdog en `_vigilar`; muestras y lectura incremental del flag; inventario de egreso con worldspaces y contadores `.fail` (1 pasada `scandir`); `_terminalidad_del_juego` (2 fases, RD-10/RD-11); `_veredicto_de_salida`; `_resultado` con veredicto/evidencia; `GrassCacheRunResult` + 4 campos | **alto** (es el corazón del crash-loop) |
| `sky_claw/local/tools/grass_cache_service.py` | `worldspaces` al config; propagar `verdict`/`certificado`/`razones`/`evidencia`/`limpieza_errores`; **operación** de journal con el veredicto (`begin_operation` + `complete_operation`/`fail_operation`); payload del evento | medio — **verificado aditivo:** los tests de U-09 filtran por `kind` (`tests/test_grass_cache_service.py:629-635`) y `begin_operation` es `AsyncMock` con `return_value=5` |
| `sky_claw/app/db/journal_contracts.py` | `GRASS_VERDICT_KIND` + `is_grass_verdict_unverified(metadata)`, junto a los otros discriminadores (es la única fuente de verdad de los `kind`) | bajo |
| `pyproject.toml` | agregar `grass_cache_verdict` a la lista estricta de mypy | bajo |
| `tests/test_grass_cache_runner.py` | watchdog (7 casos), procedencia, `.fail`, terminalidad, cancelación — **aditivo** | medio |
| `tests/test_grass_cache_service.py` | propagación, journal, evento, no-reinterpretación — **aditivo** | bajo |
| `tests/bdd/support/grass_cache_harness.py` | el doble del BDD modela un run **certificado** (`verdict`/`certificado`/`alcance`): con D1, `success`/`message` son consecuencia de la evidencia y no una constante del mock | bajo — **actualización de consumidor justificada** |
| `tests/test_grass_cache_runner.py` (test existente) | `test_flag_borrado_justo_antes_del_crash_no_relanza` afirmaba `success is True` sobre un REBUILD con caché heredado sin tocar: **ese assert era el falso éxito de F-02**. Ahora afirma `completed_unverified` con `archivos_heredados_sin_procedencia` | bajo — **único test que cambia de contrato** |
| `CHANGELOG.md` | entrada `### Added` bajo `[Unreleased]` | bajo |
| `docs/pending_ooda_status.md` | **sólo si** el owner confirma que P1 cierra un ítem del backlog (hoy no hay fila de grass P1; la fila 336 es «smokes reales» bloqueada por rig) | bajo |
| `tests/test_borrado_recursivo.py` | **condicional:** sólo si el censo de «quien mide árboles» se dispara. Mitigación de diseño: la pasada `scandir` vive en `grass_cache_runner.py`, **ya declarado** (`tests/test_borrado_recursivo.py:313`), y el módulo de veredicto es puro. | bajo |

### 7.3 Fuera del write-set (declarado, no implementado)

- **`reconcile_orphan_precache_flag`** (`grass_cache_service.py:99-188`): borra el flag huérfano de
  una muerte dura **sin archivar el progreso** — es una **causa de desaparición del flag distinta de
  `End()`**, y la auditoría P0 ya la declaró deuda P1 (§4.3 del audit, «Cerrarlo exige que el barrido
  archive antes de borrar (P1)»). **Propuesta:** incluirlo o diferirlo lo decide el owner
  (🟡). Incluirlo implica tocar un camino de arranque y agregar tests de cancelación bajo lock; no es
  necesario para el contrato de veredicto (el barrido ocurre **entre** corridas, no dentro).
- `etapas_previas_lods.py` (precondición de etapa 9): sin cambios, con límite documentado (§4.5).
- `grass_profile.py` (F-07, INI superpuesto), `grass_analyzer` (F-04), `tools_installer.py` (F-06,
  P3), `output_targets` (F-09, P4), superficies GUI/agente (F-10, P5).
- **P2**: atestación del load order efectivo, cobertura USVFS, prerrequisitos ligados al perfil.
- NGIO 2.x: no se adopta ni se implementa contra su semántica; el contrato se ancla en 1.0.13.

---

### 7.4 Orden de implementación (para que sea mecánico)

1. **`grass_cache_verdict.py`** + su entrada en la lista estricta de `pyproject.toml` + la suite pura.
   Nada más depende de esto todavía; el módulo nuevo no cambia ningún import existente salvo
   `GrassCacheOutcome` (que el runner re-exporta).
2. **Config y resultado del runner**: campos nuevos **con default** (`worldspaces`,
   `progress_poll_interval_s`, `stall_progress_s`, `stall_grace_s`, `terminality_wait_s`; `verdict`, `certificado`,
   `verdict_razones`, `evidencia`). `dataclasses.replace` de los tests existentes sigue funcionando y
   la suite queda verde sin tocar un solo assert.
3. **Watchdog + terminalidad** en `_vigilar` (`_probe_de_progreso`, `_leer_avance_del_flag`,
   `_terminalidad_del_juego` con su espera acotada, RD-10) con sus tests (14–22, 29).
4. **Ensamblado del veredicto** en el egreso (`_inventario_para_veredicto` + `_veredicto_de_salida`
   provisional + recálculo en `run()` con el resultado de la neutralización, RD-11) y `_resultado`
   invocando la política. Acá se revisan los tests que fijaban el éxito viejo
   (**1 solo**: `test_flag_borrado_justo_antes_del_crash_no_relanza`, §2.5) y se agrega el 30.
5. **Servicio**: `worldspaces` al config, propagación de campos, operación de journal, payload del
   evento; `journal_contracts.GRASS_VERDICT_KIND`.
6. **Docs y gates**: audit de P1, `CHANGELOG.md`, ruff/mypy focalizado, mutantes M1–M7, suite completa
   comparada por node ID contra §1.4.

Cada paso deja la suite verde (o con un rojo declarado y su razón), así que un corte a mitad de camino
no rompe el árbol.

---

## 8. Consumidores afectados

| Consumidor | Cambio | Verificación |
|---|---|---|
| `tool_strategies/generate_grass_cache.py` | **ninguno** (devuelve el dict tal cual) | test que ancla que no reinterpreta `unverified`/`partial`/`failed` |
| `tool_dispatcher.py:338` | **ninguno** (registro) | `test_dispatcher_dependencies.py` |
| `app/db/journal.py` | **ninguno**; el servicio registra el veredicto con una **operación** (`begin_operation`+`complete`/`fail`), que es el canal que sí acepta metadata | `test_grass_cache_service.py` (operación y su metadata) + `test_journal.py` |
| bus `pipeline.grass_cache.completed` | payload + `verdict`/`certificado` | sin suscriptores: se testea el payload publicado |
| `etapas_previas_lods.py` | **ninguno** | `test_etapas_previas_lods.py` (no debe empezar a exigir veredicto) |
| `app_context.py` / `orchestration_composition.py` | **ninguno** (construcción del servicio igual) | `test_orchestration_composition.py`, `test_supervisor_construction.py` |
| Tests existentes de éxito | se agregan asserts de `verdict`; **no** se cambia `success` si se elige la opción A | 396 tests de baseline |

---

## 9. Radar de colisiones

| Riesgo | Evidencia | Mitigación |
|---|---|---|
| PR #1021 (bodyslide) | toca `app_context.py` y `docs/pending_ooda_status.md` | P1 no toca `app_context.py`; el hunk de docs se resuelve al integrar |
| ~80 worktrees registrados en esta máquina | `git worktree list` | trabajo en `.worktrees/ngio-p1-brief`; no se toca ninguno ajeno; el rojo de §1.4 queda declarado |
| Ancla `test_borrado_recursivo` | censo de módulos que miden árboles | el walk vive en el runner, ya declarado; el módulo nuevo es puro |
| Ancla `test_distributed_locks` / `test_lock_cancelacion_adquisicion` | censo de consumidores de locks | P1 no agrega locks ni cambia el orden `grass-cache`→`load-order` |
| Ancla de fixtures (`test_grass_cache_safety.py:1174`) | congela que los fixtures del flag escriben **bytes** | los tests nuevos usan `write_bytes()` |
| `test_dyndolod_service.py:3108` | `_SERVICIOS_PARCIALES` incluye `grass_cache_service.py` (campaña de `pipeline_stage`) | P1 no toca el `pipeline_stage` |
| `mypy` estricto | §1.2 | el módulo nuevo entra a la lista y se tipa estricto |
| Contrato P0 | 41 hilos resueltos en #1008 | toda modificación de `safety` queda **excluida**; el veredicto no reimplementa planificación |

**No hay colisión de contrato detectada:** ningún PR abierto consume `GrassCacheRunResult`.

---

## 10. Plan de tests y mutantes

### 10.1 Veredicto e integridad (`tests/test_grass_cache_verdict.py`, puro)

AAA, sin `sleep` ni `tmp_path` salvo donde se indique:

1. flag ausente con caché **preexistente** sin evidencia de esta corrida → `completed_unverified`
   (`sin_evidencia_de_esta_corrida`).
2. flag ausente con caché **parcial** (baseline ⊂ final, `.fail` atribuible) → `completed_unverified`
   (`fallos_de_celda_atribuibles`).
3. `.cgid` **vacíos** (`cgid_bytes == 0`) → `failed` (`cache_vacio`).
4. `.fail` **atribuible** (nuevo o contador mayor) → no certifica.
5. `.fail` **histórico** (mismo contenido) → no se cuenta como de este intento **y** no certifica
   (`fallos_de_celda_historicos`), con el conteo separado.
6. caché de **otro perfil**: evidencia con `perfil_origen` distinto → `unknown`/no certifica
   (la identidad viaja en la evidencia; el runner no puede fabricarla).
7. archivos del **mismo tamaño con contenido distinto**: la huella `(nombre, tamaño)` **no** los
   distingue → se declara el límite y se certifica por la regla de `.fail`/procedencia, no por la
   huella; test que fija la decisión (evita que alguien use la huella como prueba de contenido).
8. **RESUME legítimo** que reutiliza caché anterior y no crece → `completed_verified` **si** hubo
   progreso observado; `completed_unverified` si no.
9. finalización **sin** evidencia suficiente → `completed_unverified` (nunca `verified`).
10. resultado correcto con evidencia completa → `completed_verified`.
11. **limpieza incompleta** → nunca `completed_verified` (`limpieza_incompleta`).
12. vocabulario de razones congelado por igualdad literal.

### 10.2 Watchdog (`tests/test_grass_cache_runner.py`, aditivo)

Mundo de procesos ya existente (`_MundoProcesos`) + reloj monotónico **inyectado** por
`monkeypatch` sobre `time.monotonic` del módulo (patrón ya usado: `heartbeat_interval_s=0.02`,
`spawn_window_s=0.05`).

13. PID vivo y progreso detenido → `stalled`, proceso matado, caché conservado.
14. PID vivo con progreso legítimo → **no** corta (cada poll agrega un `.cgid`).
15. arranque lento dentro de `stall_grace_s` → no corta.
16. stall sostenido detectado **antes** del deadline global (assert `elapsed < max_runtime_s`).
17. cancelación durante la supervisión → `cancelled`, workers drenados, sin leaks.
18. deadline global vencido → `timeout` (sin cambios; el watchdog no lo altera).
19. terminación **no verificable** → `verdict="unknown"` + `terminalidad_no_verificada`.
20. proceso **ajeno / PID reutilizado** → no se mata; identidad por `create_time`.
21. **lease vigente** durante inspecciones prolongadas: la muestra del watchdog duerme
    artificialmente y el heartbeat del lock debe seguir renovando (mismo patrón del test de F2).
22. relanzamiento rechazado por el contrato P0 (`flag_sin_progreso`) → no se relanza por el stall.

### 10.3 Integración (`tests/test_grass_cache_service.py`, aditivo)

23. el resultado llega sin pérdida: `verdict`, `certificado`, `razones`, `evidencia`,
    `limpieza_errores` y `worldspaces` presentes en el dict final.
24. el journal **no** commitea un `failed`/`unknown`; sí commitea `completed_unverified` y deja la
    **operación** `GRASS_VERDICT_KIND` fallida con las razones (y `completed_verified` la deja
    completada).
25. la strategy no reinterpreta: `unverified`/`partial`/`failed` sobreviven intactos al dict de salida.
26. NEW/RESUME/REBUILD conservan las garantías P0 (reusa los tests existentes + assert de veredicto).
27. no se abandonan workers ni locks en cancelación (reusa patrón P0; el watchdog no agrega awaits
    sin drenar).
28. la operación del veredicto **no** se confunde con la del teardown incompleto: el filtro por
    `kind` de U-09 sigue viendo exactamente las suyas (`is_teardown_incomplete` vs
    `is_grass_verdict_unverified`), y la metadata del veredicto **no** lleva `files_touched`.
29. **terminalidad con espera acotada (RD-10):** el juego muere durante `terminality_wait_s` →
    `terminado` y veredicto normal; **no** se degrada a `unknown` por latencia. Y si sigue vivo al
    agotarse la espera → `unknown` sin relanzar.
30. **veredicto de dos fases (RD-11):** con un `neutralizar_flag` que reporta limpieza incompleta,
    `_salida_exito` produce provisional y `run()` devuelve `completed_unverified` con
    `limpieza_incompleta`; el provisional **nunca** sale de `run()`.
31. **BDD intacto (RD-13):** `tests/bdd/` sigue verde **sin cambios** — el dict del servicio conserva
    `message == ""` y `success is True` para un resultado que no certifica, porque el mensaje lo
    compone el runner. Este test es el ancla de que nadie mueva esa responsabilidad al servicio.

### 10.4 Mutación dirigida (registro obligatorio)

| Mutante | Mutación | Test que debe matarlo |
|---|---|---|
| M1 | `evaluar_veredicto` devuelve `completed_verified` sin R-procedencia | #1, #8 |
| M2 | se ignora `.fail` | #2, #4, #5 |
| M3 | se elimina el corte por `sin_avance_s` | #13, #16 |
| M4 | `new/rebuild` aceptan archivos heredados | #1 |
| M5 | el servicio fuerza `certificado=True` / convierte `failed` en éxito | #23, #24, #25 |
| M6 | no se verifica terminalidad tras el kill | #19, #20 |
| M7 | se quita el drenado (`esperar_terminar`) del poll/neutralización | #17, #21 |

Procedimiento: por mutante, copia del módulo con la mutación aplicada, corrida del subconjunto
focalizado, **rojo con la causa esperada**, restauración, verde. Se registra en el audit de P1.

**Pre-validado en FASE B** (§2.5): sobre `main`, MA (sin postcheck de caché) lo matan **2** tests y
MD (exigir crecimiento) **1**; el resto del espacio de mutantes de esta tabla **no tiene ancla hoy**,
que es exactamente lo que P1 agrega. El arnés de mutación debe reescribir archivos con
`write_bytes`/`newline=""` (en Windows `write_text` convierte `\n` en `\r\n` y ensucia el árbol).

---

## 11. Riesgos y limitaciones

### 11.1 Riesgos de la implementación

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | El watchdog corta un run **legítimamente** lento (una celda de 15 min) | gracia 20 min + 600 s sin **ningún** cambio de tres indicadores + defaults conservadores (10× el umbral interno de NGIO), configurables |
| R2 | La lectura del flag bloquea el loop o infla la memoria | hilo + incremental + tope 1 MiB/poll + error ⇒ «sin evidencia», nunca aborta |
| R3 | El inventario final dobla la pasada de `stat` | una sola pasada al egreso; el poll fino usa nombres sin `stat` |
| R4 | Un `.fail` histórico bloquea para siempre la certificación | es deliberado y visible; **no** se borra ni se oculta; el operador decide con la evidencia |
| R5 | Cambiar `success` rompe consumidores | se recomienda la opción A (§4.7) |
| R6 | `mypy` estricto sobre un módulo nuevo | entra a la lista estricta desde el primer commit |

### 11.2 Limitaciones que quedan (no son verdes)

1. **No hay oráculo de completitud de celdas.** `completed_verified` **no** afirma que cada celda
   tenga `.cgid`; se declara `completitud_de_celdas: no_verificada`.
2. **Cobertura USVFS**: sigue `no_verificada` (P2). El veredicto no la toca.
3. **NGIO 2.x no está inspeccionado en su comportamiento**; el contrato se ancla en 1.0.13 (la
   versión que el instalador descarga hoy, §13). `main` (2.3.0) muestra `if (!Config::Updating)`
   alrededor del borrado y eso **cambia la premisa de P0**: no se adopta ni se asume.
4. **NGIO original 2.2.0 (Nexus 42161)**: sin fuente ni API accesible; **NO VERIFICADO** en todo.
5. **Nada se probó en rig Windows con MO2/USVFS/SKSE/NGIO reales.** Los tests son unitarios con
   mundos de procesos simulados y fixtures binarios; el brief **no** certifica comportamiento real.
6. **El flag puede no ser el archivo efectivo** (fallback `.txt.txt`): P0 lo **rechaza**, no lo
   administra; el watchdog no lo sortea.
7. **El barrido de flag huérfano** borra progreso sin archivarlo (§7.3), y es una causa de
   desaparición del flag distinta de `End()`.
8. `etapas_previas_lods` sigue aceptando un caché viejo como precondición de etapa 9 (por diseño:
   prueba existencia, no frescura).

---

## 12. Criterios de aceptación de P1 (verificables)

1. Ausencia del flag **no** certifica: existe al menos un test que falla si el veredicto se decide
   sólo por `flag ausente + count>0`.
2. `verdict`/`certificado`/`razones`/`evidencia` llegan al dict del servicio, a una **operación** del
   journal (kind `grass_cache_verdict`) y al payload del evento.
3. `.fail` atribuibles impiden certificar; los históricos se reportan sin atribuirse.
4. Un PID vivo sin progreso se detecta con corte acotado **antes** del deadline global, sin
   relanzar y con el caché conservado.
5. El caché parcial se conserva en stall/timeout/cancelación (no se borra nada).
6. Ninguna protección de P0 se debilita: `grass_cache_safety.py` **sin cambios**; el plan, el
   respaldo y los seis invariantes de autorización siguen igual.
7. Consumidores: strategy y dispatcher no reinterpretan; el journal no commitea `failed`/`unknown`;
   `etapas_previas_lods` no se degrada.
8. Los 7 mutantes dirigidos se ejecutan y cada uno es matado por el test declarado.
9. Gates verdes sobre el árbol integrado: ruff (check + format), mypy del write-set (estricto),
   focalizados, regresión del subsistema, anclas AST, CI Windows y POSIX; fallos comparados por
   **node ID** contra esta baseline (§1.4), con el rojo de entorno declarado.
10. Limitaciones de §11.2 declaradas en el cierre y en el audit de P1, sin `MVP_CERTIFIED` ni
    `PRODUCTION_READY`.

---

## 13. Anexo — Validación del autoinstalador NGIO

> Alcance: **inspección y plan**. No se ejecutó ninguna instalación ni se modificó el instalador
> (sus correcciones son P3, según la misión).

### 13.1 Qué instala y cómo (`ToolsInstaller.ensure_ngio`)

`sky_claw/local/tools_installer.py:2567-2650`. Tres componentes, directo a `mods/<Nombre>/`, con
`meta.ini`; **no** toca `modlist.txt` (la activación es del caller):

| Componente | Fuente | Selección |
|---|---|---|
| `No Grass In Objects NG` | GitHub `releases/latest` de `DwemerEngineer/No-Grass-In-Objects-NG` (`:266`, `:2616`) | `_select_ngio_asset` (`:1241-1265`): AE → asset con `ngio-ae`; SE → asset sin `ngio-ae` y sin `vr`; VR excluido |
| `Address Library for SKSE Plugins` | Nexus **32444** | `version-1-5-*.bin` (SE) / `version-1-6-*.bin` (AE) |
| `Grass Cache Helper NG` | Nexus **101095** (`:589`) | **sólo si `edition is AE`** (`:2637-2649`) |

Requisitos duros: `edition ∈ {SE, AE}` (LE/UNKNOWN abortan), `NexusDownloader` obligatorio
(Address Library exige API key), y **el slot de Nexus para GCH no se pide en SE**.

Activación (`app/agent/tools/external_tools.py:69-108`, `:259-303`): `_activar_en_modlist` agrega
`+Nombre` al `modlist.txt` del perfil en uso y devuelve `activacion ∈ {ok|parcial|fallo|
no_disponible|vacio}`; `success` de la tool es exactamente `activacion == "ok"`. Persiste
`local_cfg.ngio_mods`. **Un único sitio decide la activación** — el comentario `:76-77` documenta
que repartir ese bloque fue el defecto que dejó a NGIO «reportando éxito sin activar».

### 13.2 Runtime real detectado (lo que el instalador necesitará)

- Edición: `detect_skyrim_edition(<skyrim_path>/SkyrimSE.exe)` (PE, en hilo) — el resultado elige
  asset y si se pide GCH.
- SKSE y Address Library: **no** hay gate de SKSE en `ensure_ngio` (F-08, P3). El sentinel de
  idempotencia es `*.dll` (NGIO y GCH) y `version-1-6-*.bin` (Address Library).
- **No se detectó ni se verificó** la versión del runtime (1.6.640 vs 1.6.1170) ni que el asset AE
  (`NGIO-AE-1.0.13-1.6.640.zip`) corresponda al build instalado. Es un gate que **no existe**.

### 13.3 Qué versión descargaría HOY (verificado 2026-11-24)

| Dato | Valor |
|---|---|
| `releases/latest` de NGIO-NG | **`1.0.13`**, `published_at 2024-04-30`, nombre «1.0.13 Extended-Grass-Distance and VR fixes» |
| Assets de 1.0.13 | `NGIO-AE-1.0.13-1.6.640.zip`, `NGIO-NG-1.0.13-VR.zip`, `NGIO-NG_1.0.13.1.7z` |
| Tags existentes | `1.0.13`, `1.0.17`, `1.0.17b3` (+ 11 anteriores); **ningún tag 2.x** |
| Releases posteriores | `1.0.17` y `1.0.17b3`, ambas **`prerelease: true`** → `/releases/latest` las **excluye** |
| `main` del repo | **2.3.0** (sin tag ni release; último commit 2026-11-24) |
| Nexus 42161 | la página publica hoy **NGIO 2.3.0** («SKSE NG build, supports SE, AE (1.6 and 1.7), and VR») |

**Conclusión (F-06 confirmado y cuantificado):** Sky-Claw instala **1.0.13**, ~2,5 meses más vieja
que la última release (prerelease) y **~28 meses** más vieja que el `main`/Nexus actuales. El
mecanismo es exactamente la exclusión de prereleases de `/releases/latest`. **No se corrige en P1**
(P3), pero el contrato de P1 se ancla a **1.0.13** porque es lo que el producto instala hoy.

### 13.4 Grass Cache Helper NG en AE — lo que hay que saber antes de generar

Verificado contra la página de Nexus y el código de NGIO-NG:

- **No es obligatorio** según su autor: *«Since NGIO is now updated and supports post-AE and VR
  versions as well, you don't strictly need this mod unless you want Seasonal Grass cache support.
  But, it also adjusts some settings automatically while using NGIO so that you don't have to.»*
  → **la exigencia «AE ⇒ GCH» es una decisión de producto de Sky-Claw, no un requisito upstream**
  (`sky_claw/local/tools_installer.py:2637`). Queda declarado; corregirlo es P3.
- **Su función principal es de RUNTIME**: engancha la carga para que el juego lea `*.cgid` en vez de
  `*.gid`. En **generación** sólo toca INIs: *«It applies recommended ini settings automatically …
  Automatically sets bAllowCreateGrass, bAllowLoadGrass, bGenerateGrassDataFiles settings in the
  game ini by detecting if you are generating precache or not.»*
- **Ambos pueden estar activos a la vez.** Documentado: *«For generating grass precaches, you need
  NGIO. You can use this mod or not alongside it.»* Y en código: NGIO-NG hace
  `LoadLibraryW("Data/SKSE/Plugins/GrassCacheHelperNG.dll")` y, si está, **no instala su propio hook**
  de carga (`if (!GidFileCache::cacheHelperInstalled) stl::write_thunk_call<PathFileNameLoad>...`).
- **NO VERIFICADO:** que GCH activo cambie la **salida** de la generación (contenido/validez de los
  `.cgid`), que fuerce otro modo de NGIO o que evite el crash-loop. Es **ausencia de evidencia**, no
  prueba de inocuidad. **No se asume** que ambos plugins «pueden permanecer activos» en el sentido
  fuerte: se asume sólo que hoy Sky-Claw los deja activos y que no hay fuente que lo prohíba.
- **Riesgo nuevo detectado (declarado, fuera de P1):** GCH escribe settings en los INI del juego
  bajo `Documents/My Games/...`, **fuera del perfil clonado** — es una mutación de configuración del
  usuario durante la generación que Sky-Claw no controla ni revierte. Pertenece a P3 (config
  efectiva), no a P1.

### 13.5 Plan de prueba del autoinstalador real (para aprobar después)

Instancia **aislada** y su independencia física, antes de cualquier descarga:

1. `SKYCLAW_ISOLATED=1` **obligatorio** (keyring que falla, sin auto-detección de la máquina real).
2. Instancia MO2 **portable** nueva, en un volumen distinto del de producción (p. ej.
   `E:\Skyclaw_Rig_NGIO\`), con su `ModOrganizer.ini` propio y `base_directory` explícito.
3. Verificación de independencia **física** (no lógica): `Path.resolve()` de la instancia de prueba
   y de la de producción no comparten prefijo; sin junctions/symlinks en la cadena
   (`exigir_contencion_fisica`); `git status` del repo productivo sin cambios; logs en
   `runtime_state_dir` aislado.
4. Copia **del perfil** (no del juego): un perfil MO2 de prueba, `plugins.txt`/`modlist.txt` propios.

Casos a ejecutar (cada uno con assert de efecto en disco, no de `success`):

| # | Caso | Evidencia exigida | Aprobación |
|---|---|---|---|
| T1 | descarga + extracción SE | `mods/No Grass In Objects NG/SKSE/Plugins/*.dll` presente; `mods/.../meta.ini`; archivo elegido sin `ngio-ae` | **HITL**: egress GitHub |
| T2 | descarga + extracción AE | asset `NGIO-AE-*`; **GCH pedido**; `GrassCacheHelperNG.dll` | **HITL** + **Nexus API key** |
| T3 | Address Library por edición | `version-1-6-*.bin` en AE, `version-1-5-*.bin` en SE | **Nexus API key** |
| T4 | detección de edición | `edition` devuelta coincide con el `.exe` de la instancia de prueba | no |
| T5 | activación | `+Nombre` en `modlist.txt` del perfil de prueba, en el orden devuelto; `activacion == "ok"` | no |
| T6 | idempotencia | segunda corrida: `already_existed` en los tres, **sin** segunda descarga (sentinel) y **sin** HITL nuevo | no |
| T7 | cancelación a mitad | lo ya instalado queda; ningún archivo parcial con nombre definitivo; el lock se libera; próximo run pide sólo lo faltante | **HITL** (para cortar la descarga) |
| T8 | denegación HITL | no se descarga nada; `activacion == "fallo"`; mensaje no vacío; `success=False` | **HITL** |
| T9 | sin `SKYRIM_PATH`/sin `mo2_root` | `setup_tools` devuelve `success=False` con el motivo, sin egress | no |
| T10 | instalación en perfil ≠ clon de grass | los tres mods activos en el **perfil de origen**; el clon los hereda; `modlist.txt` del clon intacto | no |

**Requieren credenciales/permiso humano:** T1, T2, T3 (Nexus API key), T7/T8 (HITL). **Nunca** se
corren contra la instalación real de Skyrim/MO2/Steam, ni con el Golden Master. **Nada de esto se
ejecuta hasta que el owner apruebe el brief** (§13.6).

### 13.6 Lo que NO se hizo y no se hará sin autorización

- No se instaló NGIO ni GCH manualmente.
- No se instaló nada con el autoinstalador.
- No se ejecutó Skyrim ni se generó caché.
- No se descargó ni extrajo ningún asset de NGIO (sólo se leyó su código publicado y la API de GitHub).
- No se modificó `tools_installer.py` ni `external_tools.py`.

---

## 14. Revisión adversarial del propio diseño (autocrítica)

El diseño de §4 y §5 se atacó a sí mismo antes de pedir aprobación. Trece dudas, con su resolución.
**Tres encontraron defectos reales** (RD-10, RD-11, RD-13) y **una** queda como decisión del
propietario (RD-12).

| # | Duda | Resolución |
|---|---|---|
| RD-1 | ¿La atribución de `.fail` por **contenido** es sólida? | **Sí.** NGIO escribe `fails` (o `fails+1`) **antes** de generar y **borra** el archivo al completar la celda; cuando `fails >= MaxFailures` hace `return false` **antes** de reescribir, así que una celda salteada deja el contador **intacto** → se clasifica como histórica (no atribuible) y **sí** bloquea la certificación. |
| RD-2 | ¿Un `.cgid` es visible en disco apenas se genera la celda? | **NO VERIFICADO** (requiere rig). Si el motor bufferizara, el watchdog vería menos progreso del real: el diseño tolera el retraso (600 s de umbral) y el inventario final se toma **después** de que NGIO terminó. |
| RD-3 | El flag bufferea: ¿puede no verse ninguna línea `ws_`? | Sí. Por eso los worldspaces declarados por NGIO son **evidencia, nunca requisito**; el requisito usa los prefijos de los `.cgid`. |
| RD-4 | Un freeze justo mientras el runner duerme `progress_poll_interval_s` | Sin cambio: se detecta en el poll siguiente; el umbral es 20× el intervalo. |
| RD-5 | Una celda legítimamente larga | El umbral propio de NGIO es **60 s** de inactividad; 600 s es 10× eso. Si NGIO no la considera colgada, nosotros tampoco. |
| RD-6 | Lectura de un `.fail` mientras se escribe | Puede verse vacío/parcial → se clasifica como **cambiado** → bloquea (conservador, nunca al revés). |
| RD-7 | En NEW, un tercero escribe `.cgid` durante el run | Ventana declarada: el runner revalida el caché vacío **inmediatamente antes de cada spawn**; después del spawn solo NGIO escribe bajo `overwrite/Grass`. Se documenta como límite, no se promete. |
| RD-8 | La carrera «flag borrado por `End()` antes de que el motor termine de escribir los artefactos» | El progreso de una celda se anota **después** de generarla, así que el artefacto precede a la marca; `End()` corre al final. **NO VERIFICADO en rig**; se declara. |
| RD-9 | `_matar_todo` puede fallar en silencio (AccessDenied) y el run declarar éxito | Cubierto por la verificación de terminalidad (§5.3). |
| **RD-10** | **Terminalidad demasiado estricta: si el juego tarda un instante en morir, todo run bueno sería `unknown`** | **DEFECTO REAL → corregido en §5.3:** espera acotada y re-verificada (`terminality_wait_s`, default 10 s) antes de clasificar; recién entonces `vivo`/`indeterminado`. Sin esto, el veredicto se degradaría por latencia, no por evidencia. |
| **RD-11** | **El veredicto se calcula en `_salida_exito`, pero `limpieza_limpia` sólo se conoce en el `finally` que neutraliza el flag** | **DEFECTO REAL → corregido en §4.4.3:** el veredicto es **de dos fases**. `_salida_exito` produce un veredicto **provisional** y guarda la evidencia; `run()` lo **recalcula** con el resultado de `neutralizar_flag`. Si la limpieza no fue completa, nunca puede quedar `completed_verified`. |
| RD-12 | ¿`completed_verified` en `resume` sobrevalora? (el caché heredado no está atestado) | **RESUELTA por el propietario (D2):** se aprueba la certificación **limitada**: `resume` puede ser `completed_verified` con `alcance_certificado="trabajo_de_la_corrida"`, distinguiendo `trabajo_de_la_corrida` / `integridad_observable` / `completitud_global` (`no_verificada`). Sin evidencia de esta corrida → `completed_unverified` con alcance `"ninguno"`. Ver §4.8. |
| **RD-13** | **¿Quién escribe el mensaje cuando el resultado no certifica? Si lo hace el servicio, rompe un consumidor existente** | **DEFECTO REAL → corregido en §4.5:** el BDD exige `message == ""` sobre el dict del servicio con un resultado falso que no pasó por el runner. El mensaje de no-certificación lo compone el **runner**; el servicio sólo propaga. Sin esta regla, el BDD se pondría rojo al implementar P1. **Dato adicional para §4.7:** la opción B (`success = certificado`) rompería además `test_grass_cache_steps.py:118`, porque el fake del harness no trae `verdict` (default `unknown`) → esto es evidencia concreta del costo de B. |

### 14.1 Corrección RD-10 — terminalidad con espera acotada

`_terminalidad_del_juego(game_pid)` reintenta la clasificación durante `terminality_wait_s`
(default **10 s**, configurable) con espera corta entre intentos, y sólo entonces decide:

| Estado | Condición final | Veredicto |
|---|---|---|
| `terminado` | el PID desapareció, **o** el PID vivo ya no es el juego atribuido (identidad) | normal |
| `vivo` | el PID **es** el juego atribuido al agotarse la espera | `unknown` |
| `indeterminado` | no se pudo clasificar (AccessDenied, `psutil.Error`, exe ilegible) | `unknown` |

La espera **no** puede colgar el run: es acotada, corre con `_sleep_or_cancel` (la cancelación la
corta) y no retiene workers.

### 14.2 Corrección RD-11 — el veredicto se cierra después de neutralizar

```
_run_interno()
  ├─ ... finally: neutralizar_flag()  ← recién acá se sabe si la limpieza fue completa
  └─ _salida_exito(): guarda self._evidencia_egreso y devuelve veredicto PROVISIONAL
        (limpieza_limpia se asume True; el provisional nunca se publica)
run()
  └─ _avisar_limpieza_incompleta() / recálculo:
        veredicto_final = evaluar_veredicto(evidencia, limpieza_limpia=neutralizacion.limpio)
```

Consecuencias que el plan de tests ya refleja: el caso 11 («limpieza incompleta ⇒ no certifica»)
se ejerce **a través de `run()`**, no de `_salida_exito`; y todo consumidor recibe el veredicto
final, nunca el provisional.

---

## 15. Tarjeta de cierre

### Semáforo de Cierre: 🟡 AMARILLO — MEDIA
- **Estado:** `DONE_WITH_LIMITATIONS`
- **Recomendación al propietario:** Aprobar el brief para implementar P1 con la opción A de
  `success` (§4.7) y confirmar RD-12 (alcance de `completed_verified` en `resume`, §14); antes de
  instalar nada, aprobar el rig de prueba (§13.5).
- **SHA / Versión verificada:** `3d3de41eee7fb012ef97f8c12cb64301cf129b67` (`origin/main`, squash de
  P0 #1008). Sin commits nuevos y **sin un solo archivo productivo modificado**: los entregables son
  este documento y dos archivos **untracked** de trabajo — `tests/test_ngio_p1_reproduccion.py`
  (reproducción, 5 rojos) y `tests/test_ngio_p1_veredicto_borrador.py` (borrador de la política,
  13 casos + ancla del vocabulario, hoy `ModuleNotFoundError` a propósito).
- **Honestidad de verificación:**
  - *Ejecutado:* `git fetch` + `git worktree` + auditoría de `main`; CI post-merge vía `gh run list`
    (verde); `gh pr list` (1 PR abierto, #1021); `pytest` focalizado de grass → **396 passed, 21
    skipped** (+ BDD → **409 passed, 21 skipped**, re-verificado al cierre); anclas AST → **197
    passed, 5 skipped, 1 failed** (worktree ajeno, §1.4); **5 tests de reproducción de P1, los 5
    rojos con la causa correcta** (§2.4); sonda de comportamiento actual; **pre-validación de
    mutantes en un worktree descartable** → baseline 292 passed, MA mata 2 tests, MD mata 1 (§2.5),
    worktree retirado y archivo restaurado; **revisión adversarial del propio diseño** con 13 dudas:
    3 defectos reales encontrados y corregidos (RD-10, RD-11, RD-13, §14) y 1 decisión escalada
    (RD-12), incluido un consumidor existente (BDD) que habría quedado rojo; estado de `origin/main`
    y PRs re-verificado al cierre (sin cambios); `git status` del worktree de trabajo limpio salvo
    los entregables.
  - *Leído en código:* `grass_cache_{runner,safety,service}.py`, `grass_profile.py`,
    `tool_strategies/generate_grass_cache.py`, `etapas_previas_lods.py`, `tools_installer.py`
    (`ensure_ngio`, `_select_ngio_asset`), `app/agent/tools/external_tools.py`, `journal.py`,
    `journal_contracts.py`, `pyproject.toml`, anclas de tests.
  - *Fuente primaria externa:* código de NGIO-NG en tags `1.0.13`, `1.0.17b3` y `main` (`Config.h`
    `MaxFailures = 2`, `WriteProgressFile`, `KeyWS`/`KeyCell`, borrado del `.fail` al completar,
    `run_freeze_check` con `DebugMessageBox`, `End()`); API de GitHub de releases/tags; páginas de
    Nexus 42161 y 101095; código de NGIO-NG que detecta GCH.
  - *No verificado:* rig Windows/MO2/USVFS/SKSE/NGIO real; comportamiento de NGIO 2.x y del NGIO
    original 2.2.0; contenido interno de los assets de NGIO; umbral efectivo de `MaxFailures` en un
    perfil con otros mods; independencia física del rig de pruebas (no existe todavía).
- **Hallazgos clasificados:**
  - 🔴 Bloqueantes (P0/P1): **ninguno abierto**. F-02 y F-05 confirmados y **con mecanismo**
    (`DebugMessageBox` de NGIO mantiene el proceso vivo); F-12 confirmado. Son el objeto de P1.
  - 🟡 Diferidos / Limitaciones (P2): barrido de flag huérfano sin archivar progreso (P1 de la
    auditoría P0, requiere decisión); cobertura USVFS y load order (P2); `etapas_previas_lods` sin
    frescura (por diseño); GCH escribiendo INIs del juego fuera del clon (P3); instalador anclado a
    `releases/latest` = 1.0.13 y exigencia AE⇒GCH no respaldada por upstream (P3); `MaxFailures`
    efectivo no verificable.
  - 🟢 Descartados (P3 / ruido): nota de la auditoría sobre `releases/latest` «1.0.17» — se precisó:
    las 1.0.17 son prereleases, el mecanismo es la exclusión de prereleases; no cambia la acción.
- **Por qué corresponde detenerse ahora:** el contrato de veredicto cambia la semántica observable
  del resultado (`success` vs `certificado`) y el watchdog introduce un corte nuevo en el
  crash-loop. Ambos son decisiones de contrato que el propietario debe aprobar antes de tocar código
  productivo; la misión lo exige explícitamente.

**Siguiente acción:** `READY_FOR_P1_IMPLEMENTATION_APPROVAL`
