# 🔬 Reporte de Estado y Análisis de Viabilidad — Desacoplamiento de `supervisor.py`

**Fecha:** 2026-04-13  
**Versión Analizada:** 0.1.0  
**Rol:** Arquitecto de Software Principal  
**Alcance:** Migración de God Object (`SupervisorAgent`) hacia Arquitectura Orientada a Eventos (`CoreEventBus`) + Ejecución Aislada (`ExecutionSandbox`)

---

## Resumen Ejecutivo

`supervisor.py` es un archivo de **1 634 líneas** que implementa la clase `SupervisorAgent` con **34 métodos**, **28 imports directos** y **23 atributos de instancia**. Actúa como God Object al centralizar: enrutamiento de herramientas, gestión transaccional (rollback/snapshots), ciclo de vida de demonios, validación de paths, e integración con 6 pipelines de herramientas externas (xEdit, Synthesis, DynDOLOD, Wrye Bash, LOOT, Asset Conflicts).

La buena noticia: **no existen importaciones cíclicas** y los 3 demonios ya están desacoplados vía inyección de dependencias (ARC-01). La migración a `CoreEventBus` es viable, pero debe ejecutarse con el patrón Strangler Fig para no romper los flujos transaccionales existentes.

---

## 1. Mapa de Acoplamiento Severo (Hard Coupling)

### 1.1 Top 3 Módulos Más Acoplados a `SupervisorAgent`

| # | Módulo | Llamadas directas | Estado compartido | Severidad |
|---|--------|-------------------|-------------------|-----------|
| **1** | `sky_claw.db.journal.OperationJournal` | **8 métodos** lo invocan (`_log_synthesis_result`, `_log_dyndolod_start`, `_log_dyndolod_result`, `_log_patch_success`, `execute_wrye_bash_pipeline`, `execute_synthesis_pipeline`, `execute_dyndolod_pipeline`, `start`) | `self.journal` — inicializado en `_init_rollback_components()`, línea 120. Requiere `open()/close()` coordinados con el lifecycle de `start()` | 🔴 **CRÍTICO** — Toda operación de escritura depende de este objeto. Si se extrae un pipeline a un servicio aislado, ese servicio necesitará su propia instancia de journal o una interfaz compartida. |
| **2** | `sky_claw.db.snapshot_manager.FileSnapshotManager` | **7 métodos** lo invocan (`execute_synthesis_pipeline`, `execute_dyndolod_pipeline`, `resolve_conflict_with_patch`, `_rollback_synthesis_on_failure`, `_rollback_dyndolod_on_failure`, `_rollback_on_failure`, `start`) | `self.snapshot_manager` — inicializado en `_init_rollback_components()`, línea 121-122. Compartido con `MaintenanceDaemon` y `PatchOrchestrator` | 🔴 **CRÍTICO** — Es la columna vertebral del mecanismo de rollback. Cada pipeline que modifica archivos `.esp` depende de él para crear/restaurar snapshots. |
| **3** | `sky_claw.core.database.DatabaseAgent` | **4 métodos** lo invocan directamente (`start`, `dispatch_tool` vía `scraper.query_nexus`, `execute_dyndolod_pipeline` para `add_mod`, `WatcherDaemon` recibe `self.db`) | `self.db` — inicializado en `__init__`, línea 78. Requiere `init_db()/close()` coordinados con lifecycle. Inyectado en `ScraperAgent` y `WatcherDaemon` | 🟡 **ALTO** — El `DatabaseAgent` se usa tanto para registrar mods como para el lifecycle del scraper. Su inyección en el `WatcherDaemon` crea un vínculo transitivo. |

### 1.2 Variables de Estado Compartido que Impiden Aislamiento

Las siguientes variables de instancia son las que bloquean que xEdit, DynDOLOD y Synthesis se ejecuten en hilos/procesos completamente aislados:

| Variable | Tipo | Consumidores | Problema de Aislamiento |
|----------|------|-------------|------------------------|
| `self.journal` | `OperationJournal` | 8 métodos, lifecycle `start()` | Usa SQLite con `journal_mode=WAL`. Múltiples hilos pueden leer pero solo uno escribe. Un proceso aislado necesitaría IPC para registrar operaciones, o su propia conexión `threading.local()`. |
| `self.snapshot_manager` | `FileSnapshotManager` | 7 métodos, `MaintenanceDaemon` | Opera sobre el filesystem. Múltiples procesos accediendo a `.skyclaw_backups/snapshots/` generarían **race conditions TOCTOU** (check-then-create). |
| `self._path_validator` | `PathValidator` | 7 métodos vía `_validate_env_path()` | Stateless pero con roots configurados en init. Un sandbox aislado necesitaría sus propios roots o recibir un `Protocol` de validación. |
| `self.interface` | `InterfaceAgent` | `dispatch_tool`, `start`, demonios vía callback | Canal de comunicación con la GUI. Es el **único punto de salida** para HITL, eventos y notificaciones. Un EventBus reemplazaría este acoplamiento. |
| `self.profile_name` | `str` | `_resolve_modlist_path`, `_get_active_profile`, `execute_wrye_bash_pipeline`, `_run_plugin_limit_guard` | Estado de sesión que atraviesa todos los pipelines. Debería vivir en un `SessionContext` o `AppState` centralizado. |

### 1.3 Grafo de Dependencias (Simplificado)

```
SupervisorAgent
├── DatabaseAgent ──────── ScraperAgent
│                          WatcherDaemon (inyectado)
├── InterfaceAgent ──────── TelemetryDaemon (callback)
│                           LangGraphEventStreamer
│                           HITL requests
├── OperationJournal ────── RollbackManager
│                           execute_*_pipeline (×3)
│                           resolve_conflict_with_patch
├── FileSnapshotManager ── RollbackManager
│                           MaintenanceDaemon (inyectado)
│                           PatchOrchestrator (inyectado)
│                           execute_*_pipeline (×3)
├── PathValidator ────────── _validate_env_path() (×7 pipelines)
├── [LAZY] XEditRunner ──── PatchOrchestrator
├── [LAZY] SynthesisRunner
├── [LAZY] DynDOLODRunner
├── [LAZY] WryeBashRunner
├── [LAZY] PatcherPipeline
├── [LAZY] AssetConflictDetector
└── StateGraph ────────────── LangGraphEventStreamer (late binding)
```

---

## 2. Análisis de Riesgos de Transacción (Frontera Windows/WSL2)

### 2.1 Puntos Críticos de Validación de Rutas

Se identificaron **18 llamadas a `os.environ.get()`** y **10 construcciones `pathlib.Path()`** que tocan la frontera OS/WSL2. Los puntos de mayor riesgo:

| Ubicación | Variable de Entorno | Riesgo |
|-----------|---------------------|--------|
| `_sync_detect_mo2_path()` (líneas 800-830) | Ninguna — **7 rutas hardcodeadas** (`C:\Modding\MO2`, `D:\Modding\MO2`, etc.) | 🔴 **TOCTOU**: Se verifica `(p / "ModOrganizer.exe").exists()` y luego se usa `p`. Entre ambas operaciones, el directorio podría ser eliminado o reemplazado por un symlink. No pasa por `PathValidator`. |
| `_resolve_modlist_path()` (líneas 832-872) | `MO2_PATH` | 🟡 **Fallback sin validación**: Si `MO2_PATH` falla PathValidator y auto-detect falla, retorna `/mnt/c/Modding/MO2/profiles/{profile}/modlist.txt` sin validar. Este path WSL2 no se verifica contra PathValidator roots. |
| `_ensure_dyndolod_runner()` (líneas 509-514) | `SKYRIM_PATH`, `MO2_PATH`, `MO2_MODS_PATH`, `DYNDLOD_EXE`, `TEXGEN_EXE` | 🟡 **5 env vars en un solo método**: Si alguna pasa validación pero luego el ejecutable es movido antes de `subprocess.run()`, el proceso falla sin rollback del estado parcial previo (snapshot ya creado). |
| `_get_mo2_mods_path()` (líneas 874-914) | `MO2_MODS_PATH`, `MO2_PATH` | 🟡 **Doble fallback sin atomicidad**: Intenta MO2_MODS_PATH → construir desde MO2_PATH → auto-detect. Cada paso tiene un check `.exists()` que puede volverse stale. |
| `execute_dyndolod_pipeline()` (línea 1214) | — | 🔴 **Expresión sin efecto**: `runner._config.mo2_mods_path / "TexGen Output"` se computa pero **no se asigna** a ninguna variable (línea 1214). Parece ser un bug latente — debería ser `texgen_output_path = ...` o eliminarse. |

### 2.2 Impacto de Sub-Turnos Asíncronos en Bloques try-except y RollbackManager

**Estado actual:** 27 bloques try-except, de los cuales **21 (77.8%)** capturan `Exception` genérico.

#### Riesgo: Cancelación de Tareas Asíncronas

Si se introduce un sub-turno asíncrono (ej. `asyncio.TaskGroup` para ejecutar xEdit en paralelo con TexGen), el patrón actual tiene estos problemas:

| Patrón Actual | Riesgo con Sub-Turnos Async | Mitigación Propuesta |
|---------------|----------------------------|---------------------|
| `except Exception as e:` en pipelines | `asyncio.CancelledError` hereda de `BaseException` en Python 3.9+. Los handlers de `Exception` **no lo capturan**, lo que es correcto. Pero si un `except Exception` envuelve un `await` que es cancelado, el rollback del `finally` podría no ejecutarse si no hay `finally`. | Migrar a `except* SpecificError` (ExceptionGroups) para sub-turnos paralelos. Usar `try/finally` explícito para garantizar cleanup. |
| `execute_synthesis_pipeline()` — snapshot antes, rollback en handler | Si dos pipelines corren en paralelo y ambos crean snapshots del **mismo archivo**, el segundo snapshot sobreescribiría al primero. El rollback restauraría un estado intermedio, no el original. | Introducir `SnapshotTransaction` con identificador único. El `ExecutionSandbox` debe adquirir un lock por archivo target antes de crear snapshot. |
| `_rollback_on_failure()` — `except Exception` desnudo en línea 1581 | Si el rollback falla y hay un sub-turno pendiente, el `logger.critical()` se ejecuta pero la excepción se re-lanza. En un `TaskGroup`, esto propagaría como `ExceptionGroup`, matando todas las tareas del grupo sin dar oportunidad a los otros pipelines de completar su propio rollback. | El `CoreEventBus` debería emitir `RollbackFailed` como evento para que cada pipeline gestione su propio cleanup independientemente. |

#### Riesgo: Estado Inconsistente del RollbackManager

El `RollbackManager` actual delega en `OperationJournal` (SQLite) y `FileSnapshotManager` (filesystem). Con sub-turnos:

```
Timeline actual (secuencial):
    T1: create_snapshot(A.esp) → OK
    T2: run_pipeline(A.esp) → FAIL
    T3: restore_snapshot(A.esp) → OK ✅

Timeline con sub-turnos (paralelo):
    T1a: create_snapshot(A.esp) → OK        T1b: create_snapshot(B.esp) → OK
    T2a: run_pipeline(A.esp) → FAIL         T2b: run_pipeline(B.esp) → RUNNING
    T3a: restore_snapshot(A.esp) → OK?      T3b: ... aún ejecutando
                                             T3b: ... B.esp depende de A.esp (master)
                                             T3b: CORRUPTO ❌ (A.esp cambió bajo sus pies)
```

**Conclusión:** El `RollbackManager` necesita un mecanismo de **reserva de archivos** (file locking o token de transacción) antes de poder soportar ejecución paralela.

---

## 3. Candidatos para el Patrón "Strangler Fig"

Se identifican 3 funcionalidades periféricas que tienen el **menor impacto en el `AppState`** y que pueden migrar primero al `CoreEventBus`:

### 3.1 Candidato #1: `TelemetryDaemon` (Riesgo: Mínimo ⭐)

| Aspecto | Detalle |
|---------|---------|
| **Ubicación** | `sky_claw/orchestrator/telemetry_daemon.py` |
| **Acoplamiento actual** | Recibe SOLO un callback `emit_event` (típicamente `interface.send_event`). Zero acceso a DB, journal, snapshots o state. |
| **Estado compartido** | Ninguno. Es puramente fire-and-forget. |
| **Migración a EventBus** | Reemplazar `emit_event(payload)` por `event_bus.publish("telemetry.metrics", payload)`. La GUI se suscribe al tópico. |
| **Esfuerzo estimado** | ~2 horas. Cambiar 1 archivo + 1 test. |
| **Impacto si falla** | Cero. La telemetría es decorativa. No afecta ningún pipeline. |

### 3.2 Candidato #2: `WatcherDaemon` — Detección de Cambios en modlist.txt (Riesgo: Bajo ⭐⭐)

| Aspecto | Detalle |
|---------|---------|
| **Ubicación** | `sky_claw/orchestrator/watcher_daemon.py` |
| **Acoplamiento actual** | Recibe `modlist_path`, `profile_name`, `db`, `on_change` callback. El `on_change` actualmente llama a `supervisor._trigger_proactive_analysis()` (que es un stub vacío, línea 284-288). |
| **Estado compartido** | `db` (para guardar timestamp del último cambio) y `modlist_path`. |
| **Migración a EventBus** | Reemplazar `on_change()` callback por `event_bus.publish("modlist.changed", {"profile": profile, "mtime": mtime})`. El supervisor (u otro servicio) se suscribe al tópico para disparar análisis proactivo. |
| **Esfuerzo estimado** | ~4 horas. Cambiar 2 archivos + tests. La dependencia de `db` puede mantenerse (solo lectura de mtime). |
| **Impacto si falla** | Bajo. `_trigger_proactive_analysis()` es actualmente un `pass`. No hay lógica downstream real. |

### 3.3 Candidato #3: Validación Estática de Rutas + `_sync_detect_mo2_path()` (Riesgo: Bajo-Medio ⭐⭐)

| Aspecto | Detalle |
|---------|---------|
| **Ubicación** | Métodos `_validate_env_path()`, `_sync_detect_mo2_path()`, `_resolve_modlist_path()`, `_get_mo2_mods_path()`, `_get_active_profile()` — todos en `supervisor.py` |
| **Acoplamiento actual** | `_validate_env_path()` usa `self._path_validator` (stateless). Los métodos de resolución de paths son **puramente funcionales** — no modifican estado, solo lo consultan. |
| **Estado compartido** | `self._path_validator` (immutable post-init), `self.profile_name` (read-only). |
| **Migración a servicio** | Extraer a `PathResolutionService` con interfaz `Protocol`. Todos los `_ensure_*()` recibirían el servicio vía inyección. El EventBus no es necesario para este caso — es extracción directa a servicio stateless. |
| **Esfuerzo estimado** | ~6 horas. Crear 1 nuevo módulo, refactorizar 7 métodos en supervisor, actualizar tests. |
| **Impacto si falla** | Medio. Si la resolución de paths falla, ningún pipeline puede inicializarse. Pero la interfaz `Protocol` permite un mock directo en tests. |

### Orden Recomendado de Migración

```
Fase A (Sprint 1): TelemetryDaemon → EventBus
    ↓ Validar que el bus funciona end-to-end
Fase B (Sprint 1): WatcherDaemon → EventBus
    ↓ Validar patrón pub/sub con estado mínimo
Fase C (Sprint 2): PathResolutionService → Servicio inyectable
    ↓ Validar que todos los _ensure_*() siguen funcionando
Fase D (Sprint 2+): Pipeline Services (Synthesis, DynDOLOD, Wrye Bash)
    ↓ Estos requieren resolver el problema de SnapshotTransaction primero
```

---

## 4. Estimación de Esfuerzo (Métricas Crudas)

### 4.1 Métodos a Extraer a Commands o Services

| Categoría | Métodos | Destino propuesto | Complejidad |
|-----------|---------|-------------------|-------------|
| **Pipeline Synthesis** (FASE 3) | `_ensure_synthesis_runner`, `_ensure_patcher_pipeline`, `execute_synthesis_pipeline`, `_rollback_synthesis_on_failure`, `_log_synthesis_result` | `SynthesisPipelineService` | Alta — transaccional |
| **Pipeline DynDOLOD** (FASE 4) | `_ensure_dyndolod_runner`, `execute_dyndolod_pipeline`, `_log_dyndolod_start`, `_log_dyndolod_result`, `_rollback_dyndolod_on_failure` | `DynDOLODPipelineService` | Alta — transaccional |
| **Pipeline Wrye Bash** (FASE 6) | `_ensure_wrye_bash_runner`, `execute_wrye_bash_pipeline` | `WryeBashPipelineService` | Media |
| **Parcheo Transaccional** (FASE 2) | `_ensure_patch_orchestrator`, `resolve_conflict_with_patch`, `_rollback_on_failure`, `_log_patch_success` | `PatchingService` | Alta — transaccional |
| **Asset Detection** (FASE 5) | `asset_detector` (property), `scan_asset_conflicts`, `scan_asset_conflicts_json` | `AssetScanService` | Baja — read-only |
| **Resolución de Paths** | `_validate_env_path`, `_sync_detect_mo2_path`, `_resolve_modlist_path`, `_get_mo2_mods_path`, `_get_active_profile` | `PathResolutionService` | Baja — stateless |
| **Plugin Limit Guard** | `_run_plugin_limit_guard` | `PluginValidationService` | Baja |
| **Tool Dispatch** | `dispatch_tool` | Extraer a `CommandRouter` basado en EventBus | Media |
| **Lifecycle** | `start`, `_init_rollback_components`, `_init_patch_orchestrator` | Refactorizar en `SupervisorAgent` reducido | Media |

**Totales:**
- **Métodos a extraer:** 28 de 34 (82%)
- **Métodos que quedarían en SupervisorAgent:** 6 (`__init__`, `start`, `handle_execution_signal`, `dispatch_tool` simplificado, `execute_rollback`, `get_rollback_manager`)
- **Nuevos servicios/modules a crear:** 7

### 4.2 Importaciones Redundantes o Eliminables Post-Refactorización

| Import actual en `supervisor.py` | Razón de eliminación |
|----------------------------------|---------------------|
| `SynthesisRunner, SynthesisConfig, SynthesisResult, SynthesisExecutionError` | Migrarían a `SynthesisPipelineService` |
| `PatcherPipeline` | Migrarían a `SynthesisPipelineService` |
| `DynDOLODRunner, DynDOLODConfig, DynDOLODPipelineResult, DynDOLODExecutionError, DynDOLODTimeoutError` | Migrarían a `DynDOLODPipelineService` |
| `WryeBashRunner, WryeBashConfig, WryeBashExecutionError` | Migrarían a `WryeBashPipelineService` |
| `PatchOrchestrator, PatchPlan, PatchResult, PatchingError, PatchStrategyType` | Migrarían a `PatchingService` |
| `XEditRunner, ScriptExecutionResult` | Migrarían a `PatchingService` |
| `ConflictReport, ConflictAnalyzer` | Migrarían a `PatchingService` y `PluginValidationService` |
| `AssetConflictDetector, AssetConflictReport` | Migrarían a `AssetScanService` |

**Resultado:** De 28 imports, **21 (75%) se eliminarían** de `supervisor.py`. El supervisor refactorizado solo necesitaría:
- `DatabaseAgent`, `InterfaceAgent` (core lifecycle)
- `OperationJournal`, `RollbackManager`, `FileSnapshotManager` (transaccional — podría abstraerse tras `Protocol`)
- `create_supervisor_state_graph`, `LangGraphEventStreamer` (orquestación)
- Los 7 nuevos servicios (vía `Protocol`)

### 4.3 Importaciones Cíclicas

**Estado actual: 0 importaciones cíclicas detectadas.** ✅

La arquitectura de imports es estrictamente acíclica:
- `supervisor.py` importa FROM todos los módulos
- Ningún módulo importa FROM `supervisor.py`
- `state_graph.py` usa late binding (`connect_supervisor()`) para evitar ciclos
- Los demonios reciben dependencias vía constructor (DI puro)

**Riesgo post-refactorización:** La introducción de `CoreEventBus` como módulo compartido no debería crear ciclos si se coloca en `sky_claw/core/event_bus.py` y los servicios solo importan el bus, nunca entre ellos.

### 4.4 Tabla de Complejidad por Método

| Método | LOC | Bloques try-except | `await` calls | Accesos a `self.*` compartido |
|--------|-----|-------------------|---------------|------------------------------|
| `execute_dyndolod_pipeline` | 161 | 7 | 8 | journal, snapshot_manager, db |
| `resolve_conflict_with_patch` | 137 | 4 | 6 | snapshot_manager, journal, _xedit_runner, _patch_orchestrator |
| `execute_synthesis_pipeline` | 131 | 5 | 7 | journal, snapshot_manager, _synthesis_runner, _patcher_pipeline |
| `execute_wrye_bash_pipeline` | 98 | 3 | 4 | journal, _wrye_bash_runner |
| `dispatch_tool` | 58 | 0 | 5 | scraper, tools, interface, db |
| `_ensure_dyndolod_runner` | 70 | 0 | 0 | _path_validator, _dyndolod_runner |
| `_ensure_synthesis_runner` | 59 | 0 | 0 | _path_validator, _synthesis_runner |
| `start` | 34 | 1 | 8 | db, journal, snapshot_manager, interface, demonios (×3) |

---

## 5. Dependencias Ocultas que Podrían Bloquear el EventBus

### 5.1 Dependencia #1: `OperationJournal` Lifecycle Binding

El `OperationJournal` requiere `await journal.open()` en `start()` y `await journal.close()` en el `finally`. Si los servicios extraídos necesitan escribir en el journal, deben compartir la misma instancia abierta o tener su propia gestión de lifecycle.

**Bloqueo:** Un servicio que se registra en el EventBus y recibe un comando `"execute_synthesis"` necesitará acceso al journal. Si el journal no está abierto (porque `start()` aún no se ejecutó), la operación falla silenciosamente.

**Solución:** El `CoreEventBus` debe emitir `"system.ready"` después de que `journal.open()` complete. Los servicios solo procesan comandos después de recibir ese evento.

### 5.2 Dependencia #2: Lazy Init de Tool Runners con `os.environ`

Todos los `_ensure_*()` leen `os.environ.get()` en cada invocación. Si un servicio se inicializa antes de que las variables de entorno estén configuradas (ej. en un entorno de test o en un worker process), los runners fallan.

**Bloqueo:** En un `ExecutionSandbox`, el worker process hereda `os.environ` del parent. Pero si el sandbox se lanza como proceso separado (ej. `multiprocessing.Process`), las variables de entorno deben pasarse explícitamente.

**Solución:** Reemplazar `os.environ.get()` por un `ConfigProvider` inyectable que los servicios reciben en su constructor.

### 5.3 Dependencia #3: `InterfaceAgent` como Singleton de Comunicación

`InterfaceAgent` es el único canal para:
- HITL (Human-in-the-Loop) requests (`interface.request_hitl`)
- Eventos de UI (`interface.send_event`)
- Callbacks de GUI (`interface.register_command_callback`)

**Bloqueo:** Si un pipeline corre en un `ExecutionSandbox` aislado y necesita HITL approval (ej. `execute_loot_sorting` línea 319), no puede llamar `interface.request_hitl()` directamente — necesita IPC.

**Solución:** El `CoreEventBus` debe soportar request/reply pattern (no solo pub/sub). El sandbox publica `"hitl.request"`, el supervisor escucha, delega a la interfaz, y publica `"hitl.response"`.

### 5.4 Dependencia #4: Bug Latente en `execute_dyndolod_pipeline` (Línea 1214)

```python
# Línea 1213-1214 (supervisor.py)
dyndolod_output_path = runner._config.mo2_mods_path / "DynDOLOD Output"
runner._config.mo2_mods_path / "TexGen Output"  # ← EXPRESIÓN SIN EFECTO
```

La línea 1214 computa un path pero **no lo asigna** a ninguna variable. Parece que debería ser `texgen_output_path = ...`. Este bug no causa crashes porque `texgen_output_path` no se usa posteriormente, pero indica que el snapshot para TexGen Output **nunca se crea** — solo se crea para DynDOLOD Output. Esto es una **omisión funcional** que debería resolverse antes de migrar el pipeline a un servicio aislado.

### 5.5 Dependencia #5: `except Exception` Desnudos como Barrera de Migración

21 de 27 bloques try-except capturan `Exception` genérico. Al migrar a EventBus con `TaskGroup`:
- `except Exception` **no captura** `asyncio.CancelledError` (hereda de `BaseException`)
- `except* SpecificError` requeriría reescribir los handlers para ExceptionGroups
- Los handlers actuales de `except Exception` enmascaran errores inesperados que en un sistema distribuido podrían propagarse como eventos `"pipeline.error"`

**Recomendación:** Como prerequisito a la migración, auditar y tipificar los 21 handlers genéricos, reemplazando `except Exception` por las excepciones específicas de cada dominio (`PatchingError`, `SynthesisExecutionError`, `DynDOLODExecutionError`, `WryeBashExecutionError`, `OSError`, `IOError`).

---

## 6. Conclusión y Próximos Pasos

### Viabilidad General: ✅ VIABLE con restricciones

| Dimensión | Estado | Nota |
|-----------|--------|------|
| Importaciones cíclicas | ✅ 0 ciclos | No bloquea |
| Desacoplamiento de demonios | ✅ ARC-01 completado | Listos para EventBus |
| Transaccionalidad (rollback/snapshots) | ⚠️ Requiere `SnapshotTransaction` con locking | Bloquea ejecución paralela |
| HITL en sandboxes | ⚠️ Requiere request/reply en EventBus | Bloquea aislamiento de LOOT/xEdit |
| Lifecycle del journal | ⚠️ Requiere evento `system.ready` | Bloquea inicialización de servicios |
| Handlers genéricos | ⚠️ 21 `except Exception` desnudos | Pre-requisito de calidad |
| Bug latente (línea 1214) | 🔴 Expresión sin efecto | Corregir antes de extraer |

### Acción Inmediata (Sprint 0 — Preparación)

1. **Corregir bug línea 1214** — Asignar o eliminar la expresión sin efecto
2. **Tipificar 21 handlers genéricos** — Reemplazar `except Exception` por excepciones de dominio
3. **Diseñar `CoreEventBus` Protocol** — Definir interfaz mínima (publish, subscribe, request/reply)
4. **Crear `PathResolutionService`** — Primera extracción stateless como prueba de concepto

### Acción de Migración (Sprint 1+)

5. **Migrar TelemetryDaemon → EventBus** (Strangler Fig Fase A)
6. **Migrar WatcherDaemon → EventBus** (Strangler Fig Fase B)
7. **Diseñar `SnapshotTransaction`** con file locking para soportar paralelismo
8. **Extraer pipeline services** (Synthesis, DynDOLOD, Wrye Bash, Patching)
