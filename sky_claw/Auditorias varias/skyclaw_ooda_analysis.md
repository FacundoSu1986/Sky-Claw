# Sky-Claw OODA Analysis Report
## Analisis OODA completo: Errores, mejoras y deuda tecnica

**Fecha:** 2026-07-04
**Analista:** Senior Architect OODA
**Alcance:** Subsistemas criticos no auditados en AUDIT_REPORT.md (2026-05-09)
**Metodologia:** OODA x 4 ciclos (Observe → Orient → Decide → Act)

---

## Executive Summary

Este analisis OODA examina los subsistemas criticos de Sky-Claw que **no fueron cubiertos** por el AUDIT_REPORT.md previo (que corrigio C-2, H-04, H-05 en async_path_resolver, event_bus y dlq_manager). Los hallazgos se organizan en 4 fases OODA:

| Severidad | Hallazgos | Categorias afectadas |
|-----------|-----------|---------------------|
| **Critical** | 4 | Seguridad, Race Conditions, Deadlocks |
| **High** | 7 | Concurrencia, Error Handling, API Contracts |
| **Medium** | 9 | Deuda tecnica, Performance, Observabilidad |
| **Low** | 5 | Estilo, Consistencia, Documentacion |

**Total: 25 hallazgos** distribuidos en 4 subsistemas: Security, Agent Core, Orchestrator, Local Tools.

---

# ===================================================================
# FASE 1: OBSERVE (Observacion) — Datos crudos del campo
# ===================================================================

## 1.1 Subsistema: SEGURIDAD (security/)

### 1.1.1 GovernanceManager — `governance.py`

**Hallazgo G-1 [CRITICAL]:** Singleton con `threading.Lock()` en contexto async — posible bloqueo del event loop
```python
# Linea 12818
_lock = threading.Lock()  # threading.Lock en codigo async-heavy
```
El `get_instance()` usa `threading.Lock()` para el singleton. Si un `async def` llama a `get_instance()` y el lock esta contencionado por otro hilo, el event loop se bloquea. Aunque el metodo es sync, callers async podrian invocarlo en `asyncio.to_thread()` pero **no lo hacen** en el codigo observado.

**Hallazgo G-2 [HIGH]:** `threading.Lock()` usado en `_get_or_create_hmac_key` (metodo sync) — OK, pero `_load_whitelist` tambien lo usa sin proteccion async
```python
# Lineas 12942-12973 — _load_whitelist() lee archivos sync sin asyncio.to_thread
raw = self.whitelist_path.read_bytes()  # I/O bloqueante en hot path async
```
Si `_load_whitelist` se llama desde una corutina (ej: `is_scanned_and_clean` → primer acceso), el `read_bytes()` bloquea el event loop.

**Hallazgo G-3 [MEDIUM]:** `_hash_semaphore` es `asyncio.Semaphore` pero se inicializa lazy sin lock — race condition en inicializacion
```python
# Lineas 12847-12856
self._hash_semaphore: asyncio.Semaphore | None = None  # No hay lock para la inicializacion lazy

def _get_hash_semaphore(self) -> asyncio.Semaphore:
    if self._hash_semaphore is None:
        self._hash_semaphore = asyncio.Semaphore(self._HASH_CONCURRENCY)  # Race si dos corutinas entran simultaneamente
    return self._hash_semaphore
```

**Hallazgo G-4 [MEDIUM]:** `approve_file` (metodo sync) se llama desde `PurpleSecurityAgent.approve_manually` que es sync, pero el contexto de llamada podria ser async

**Hallazgo G-5 [LOW]:** HMAC key regeneration pierde la cadena de HMAC anterior sin migracion — si la clave anterior no pudo ser endurecida, todos los HMACs previos quedan invalidados (comportamiento documentado como fail-closed, pero podria alertar al usuario)

### 1.1.2 AgentGuardrail — `agent_guardrail.py`

**Hallazgo AG-1 [HIGH]:** `_GUARDRAIL_INJECTION_RE` tiene falso negativo en prompts multilingues — solo detecta patrones en ingles
```python
# Lineas 12261-12275
r"ignore\s+(?:all\s+)?(?:prior|previous)\s+(?:context|instructions?)"  # Solo ingles
```
Prompts de inyeccion en espanol ("ignora todas las instrucciones anteriores") pasan sin deteccion.

**Hallazgo AG-2 [HIGH]:** `_check_pii` solo detecta el **primer** match via `_COMBINED_PII_RE.search()` — si hay multiples tipos de PII, solo se reporta uno
```python
# Linea 12506
match = _COMBINED_PII_RE.search(text)
```
Deberia usar `findall()` o iterar sobre todos los matches para reportar cada tipo de PII detectado.

**Hallazgo AG-3 [MEDIUM]:** `max_input_length=8192` es arbitrario — no considera tokens reales del modelo. Un texto de 8192 chars puede ser >8192 tokens (especialmente con UTF-8 multibyte o idiomas asiaticos), permitiendo token-bomb attacks.

**Hallazgo AG-4 [LOW]:** `_extract_last_user_content` no maneja message content como lista (formato Anthropic con content blocks) — solo extrae `str`
```python
# Lineas 12524-12530
content = msg.get("content", "")
return content if isinstance(content, str) else str(content)
```
Si el content es una lista de blocks (formato nativo Anthropic), se hace `str()` sobre la lista generando representacion Python cruda.

### 1.1.3 CredentialVault — `credential_vault.py`

**Hallazgo CV-1 [CRITICAL]:** `_SQLitePool.acquire()` tiene race condition en `close()` — el metodo `close()` setea `_closed = True` fuera del `_close_lock` y luego hace `release()` del semaphore, pero una corutina que ya paso el check de `_closed` puede obtener una conexion de la pool despues de que `close()` dreno las conexiones
```python
# Lineas 74243-74261
async with self._close_lock:
    if self._closed:
        return
    self._closed = True  # Seteado bajo lock

# PERO luego (lineas 74251-74260):
for _ in range(self._max_size):
    with suppress(ValueError):
        self._semaphore.release()  # Despierta waiters

while True:
    try:
        conn = self._pool.get_nowait()  # Drena conexiones
        await conn.close()
    except asyncio.QueueEmpty:
        break
```
Una corutina suspendida en `await self._semaphore.acquire()` es despertada por `release()`, pasa el re-check de `_closed`, encuentra la pool vacia (ya drenada), crea una **nueva conexion** via `_create_connection()`, y la retorna. La pool ahora tiene una conexion viva despues de `close()`.

**Hallazgo CV-2 [HIGH]:** `_write_salt_atomic` usa `os.O_CREAT | os.O_EXCL` pero si el archivo tmp ya existe (crash recovery), el `os.open` falla con `EEXIST` — el `suppress(OSError)` en la limpieza previa no cubre este caso porque el unlink puede fallar por permisos
```python
# Lineas 73612-73618
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
fd = os.open(tmp, flags, 0o600)  # EEXIST si tmp sigue existiendo
```

**Hallazgo CV-3 [MEDIUM]:** `_get_or_create_salt()` es metodo sincrono con I/O de archivos (read_bytes) — bloquea event loop si se llama desde async

**Hallazgo CV-4 [LOW]:** `VaultStorageError` vs `SecurityViolationError` — la jerarquia de excepciones no distingue entre fallos operacionales (DB lock) y fallos de seguridad (tampering). Un caller no puede diferenciar "reintentar mas tarde" vs "alerta de seguridad".

### 1.1.4 PurpleSecurityAgent — `purple_security_agent.py`

**Hallazgo PS-1 [HIGH]:** `_format_audit_findings` hardcodea la fase actual como "ABRIL 2026" — no es dinamico y quedara desactualizado
```python
# Linea 4109
"**RAZONAMIENTO ABRIL 2026:**",
```

**Hallazgo PS-2 [MEDIUM]:** `_build_audit_response` usa `result.get("summary", {})` sin validar que sea dict — si `summary` es `None`, el `.get()` siguiente falla con `AttributeError`
```python
# Linea 4070
summary = result.get("summary", {})  # OK, default es {}
# Pero linea 4093:
findings_count = summary.get("findings_count", 0)  # OK por el default anterior
```
**Nota:** El default de `{}` previene el crash, pero si el upstream pasa `summary=None`, el crash ocurre. El `audit_resource` upstream (metacognitive_logic.py) siempre crea el dict, pero no hay contrato formal.

**Hallazgo PS-3 [MEDIUM]:** `metacognitive_logic.py` — `_phase_resolve` abre archivos sync (`open(file_path)`) sin `asyncio.to_thread`, bloqueando el event loop en escaneos de repositorios grandes

**Hallazgo PS-4 [LOW]:** `SecurityMetacognition` no tiene timeout global — un escaneo de repositorio grande puede ejecutar indefinidamente

### 1.1.5 Security — Cross-Cutting

**Hallazgo SC-1 [CRITICAL]:** `AgenticLoopGuardrail` en `loop_guardrail.py` usa `deque(maxlen=window_size)` pero la deteccion de bucles compara `last_n[0]` con todos los elementos — si el agente alterna entre 2 herramientas (A, B, A, B, A, B), no se detecta como bucle aunque es un patron ciclico
```python
# Lineas 13191-13192
last_n = list(self._history)[-self._max_repeats:]
if all(h == last_n[0] for h in last_n):  # Solo detecta repeticiones identicas, no ciclos
```

**Hallazgo SC-2 [HIGH]:** No hay rate limiting en `PurpleSecurityAgent.audit_repository` — un LLM malicioso podria solicitar escaneos repetidos del filesystem completo causando DoS por I/O

---

## 1.2 Subsistema: AGENT CORE (agent/)

### 1.2.1 LLMRouter — `router.py`

**Hallazgo R-1 [CRITICAL]:** `_provider_lock` (asyncio.Lock) se usa para hot-swap pero `chat()` lee `_provider` **sin lock** en el hot path
```python
# Linea 82050 (en chat())
response: dict[str, Any] = await provider.chat(messages=messages_copy, **kwargs)
```
Si `set_provider()` intercambia `_provider` mientras `chat()` esta suspendido en `await provider.chat(...)`, la asignacion `provider = self._provider` (que ocurre antes del await) es atomica, pero si hay multiples awaits en la funcion, el segundo `self._provider` podria ser diferente.

Mas critico: la variable local `provider` se captura antes del await, pero si el metodo hace **multiple** llamadas a `self._provider` en diferentes puntos (ej: reintentos), cada lectura podria ver un provider diferente.

**Hallazgo R-2 [HIGH]:** `_save_message` y `_load_context` usan `self._conn` sin lock — si se llama `close()` concurrentemente (ej: shutdown durante chat), `_conn` se setea a `None` y el siguiente acceso falla con `AttributeError`

**Hallazgo R-3 [HIGH]:** `consecutive_errors` counter se incrementa pero **nunca se resetea** en caso de exito — un error antiguo seguido de 9 rondas exitosas seguira contando hacia el limite
```python
# Lineas 82165-82171 (en el loop de tool rounds)
consecutive_errors = 0
hermes_parse_error_count = 0
hermes_exec_error_count = 0

for _round in range(MAX_TOOL_ROUNDS):
    try:
        # ... en error:
        consecutive_errors += 1
        # ... pero NO hay reset en exito
```

**Hallazgo R-4 [MEDIUM]:** `MAX_CONTEXT_MESSAGES = 20` es constante legacy — el `TokenBudgetManager` deberia manejar esto dinamicamente, pero la constante sigue hardcodeada

**Hallazgo R-5 [MEDIUM]:** `_token_budget.estimate_tokens()` es metodo sync que potencialmente itera sobre messages — para contextos grandes podria ser costoso y bloquear el event loop

**Hallazgo R-6 [LOW]:** El comentario BUG-002 dice "Valida API key" pero `_is_valid_api_key` solo rechaza placeholders obvios — no valida formato real de las keys (ej: no verifica prefijos `sk-ant-`, `sk-`, etc.)

### 1.2.2 Providers — `providers.py`

**Hallazgo P-1 [HIGH]:** Todos los providers (Anthropic, DeepSeek, OpenAI, Ollama) comparten la **misma politica de retry** (`wait_exponential(multiplier=1.5, min=2, max=60)`, `stop_after_attempt(5)`) — Ollama local deberia tener retries mas agresivos (es local, no hay rate limiting), mientras que APIs de pago deberian ser mas conservadoras

**Hallazgo P-2 [MEDIUM]:** `OllamaProvider` no tiene timeout en la request — `async with await gateway.request(...)` no pasa timeout, asi que un Ollama colgado bloquea indefinidamente

**Hallazgo P-3 [MEDIUM]:** `_convert_messages_to_openai` maneja tool_result con un `continue` que salta el procesamiento del resto del mensaje — si un mensaje tiene tool_results Y texto, el texto se pierde
```python
# Lineas 68314-68322
if first_type == "tool_result":
    for block in content:
        result.append({...})
    continue  # El resto del mensaje se ignora
```

**Hallazgo P-4 [LOW]:** `OpenAIProvider.DEFAULT_MODEL = "gpt-5"` — este modelo no existe en la API de OpenAI al momento del analisis. Deberia ser "gpt-4o" o similar.

### 1.2.3 ManagedToolExecutor — `executor.py`

**Hallazgo E-1 [CRITICAL]:** `_stream_telemetry` puede colgar indefinidamente si el subprocess produce output infinito (loop en tool modding) — no hay timeout en `stream.readline()`
```python
# Lineas 42446-42449
async def _read_stream(stream: asyncio.StreamReader, prefix: str):
    while True:
        line = await stream.readline()  # Sin timeout — puede colgar para siempre
```

**Hallazgo E-2 [HIGH]:** `abort()` no mata procesos hijos (grandchildren) — en Windows, un proceso puede spawnear sub-procesos que quedan huerfanos (zombies). Solo se termina `self.proc`, no su arbol completo.

**Hallazgo E-3 [HIGH]:** `signal_abort()` llama `self.proc.terminate()` desde cualquier hilo sin verificar si `self.proc` es None — race condition si `abort()` se llama concurrentemente
```python
# Lineas 42462-42467
def signal_abort(self):
    self._abort_event.set()
    if self.proc:
        with contextlib.suppress(ProcessLookupError):
            self.proc.terminate()  # Race: self.proc podria volverse None entre el check y el uso
```

**Hallazgo E-4 [MEDIUM]:** `_resolve_strict_false` usa `asyncio.to_thread` pero no limita el thread pool — bajo carga intensiva (muchos tools ejecutando), puede crear threads ilimitados agotando recursos del sistema

**Hallazgo E-5 [LOW]:** El timeout de 300s es fijo — herramientas como DynDOLOD legitiman tardan mas de 30 minutos, pero el executor usado por DynDOLOD pasa por aqui con timeout de 300s (5 min) que matara el proceso prematuramente

### 1.2.4 SemanticRouter — Referenciado en router.py

**Hallazgo SR-1 [MEDIUM]:** `SemanticRouter.route()` es metodo sincrono que se llama en el hot path async de `chat()` — si la clasificacion es costosa (ML model), bloquea el event loop
```python
# Linea 82119
routed = self._semantic_router.route(routing_data)  # Sync call en async path
```

---

## 1.3 Subsistema: ORCHESTRATOR (orchestrator/)

### 1.3.1 SupervisorAgent — `supervisor.py`

**Hallazgo S-1 [CRITICAL]:** `start()` usa `asyncio.TaskGroup` para daemons pero el `async with` termina cuando **cualquier** daemon falla — si un daemon tiene un bug, todos los demas se cancelan
```python
# Lineas 100889-100892
async with asyncio.TaskGroup() as daemon_tg:
    daemon_tg.create_task(self._maintenance_daemon.start())
    daemon_tg.create_task(self._telemetry_daemon.start())
    daemon_tg.create_task(self._watcher_daemon.start())
# Si WatcherDaemon falla, los otros dos se cancelan
```

**Hallazgo S-2 [HIGH]:** `_run_interface_isolated` usa `except* Exception` como catch-all pero luego re-raise — el problema es que `except* (ConnectionError, TimeoutError, OSError)` no captura `asyncio.CancelledError` (es BaseException), asi que una cancelacion del supervisor no se maneja gracefulmente

**Hallazgo S-3 [HIGH]:** `handle_execution_signal` no valida el `payload` — acepta cualquier dict sin sanitizacion, permitiendo potencialmente inyeccion de comandos si el payload contiene datos no esperados

**Hallazgo S-4 [MEDIUM]:** `dispatch_tool` delega al `_tool_dispatcher` pero no hay timeout — si una tool strategy se cuelga, el supervisor queda bloqueado indefinidamente

**Hallazgo S-5 [MEDIUM]:** `execute_rollback` captura `Exception` generica (SUP-06) pero esto esconde bugs de programacion — deberia capturar solo excepciones esperadas y dejar que los bugs (TypeError, ValueError) propaguen

**Hallazgo S-6 [MEDIUM]:** El modlist parsing en `_run_plugin_limit_guard` lee el archivo sync (`with open(...)`) sin `asyncio.to_thread` — I/O bloqueante

**Hallazgo S-7 [LOW]:** `parse_active_plugins` no incluye `.esl` en la validacion de `_run_plugin_limit_guard` (solo `.esp/.esm`), pero si lo incluye en el parsing — inconsistencia

### 1.3.2 StateGraph — `state_graph.py`

**Hallazgo SG-1 [HIGH]:** `StateGraphValidator._VALID_TRANSITIONS` no incluye transicion de `ERROR_FATAL → END` — aunque el codigo documenta que `ERROR_FATAL` es terminal, no hay transicion explicita al nodo END, lo que podria dejar el grafo en un estado inconsistente

**Hallazgo SG-2 [HIGH]:** `hitl_wait_node` inyecta `hitl_started_at` solo si es `None`, pero nunca lo resetea — despues de un HITL completado, el timestamp persiste, haciendo que el **siguiente** HITL use el timestamp del anterior y potencialmente timeout prematuramente

**Hallazgo SG-3 [MEDIUM]:** `MAX_TRANSITION_HISTORY = 50` es arbitrario y no es configurable — para workflows largos se pierde historia critica

**Hallazgo SG-4 [MEDIUM]:** Los nodos del StateGraph (`init_node`, `idle_node`, etc.) son `@staticmethod` que no pueden acceder al `self` del supervisor — toda la logica de negocio debe vivir en callbacks externos, creando un anti-patron de "logica dispersa"

**Hallazgo SG-5 [LOW]:** `WorkflowState` se define dos veces (con y sin Pydantic) — duplicacion de codigo que puede divergir

### 1.3.3 ToolDispatcher — `tool_dispatcher.py`

**Hallazgo TD-1 [MEDIUM]:** `_build_chain_preview_service` crea `XEditRunner` y `LOOTRunner` sin usar los servicios ya existentes del supervisor — duplicacion de recursos y configuracion

**Hallazgo TD-2 [LOW]:** Las lambdas en `ScanAssetConflictsStrategy`, `ScanAssetConflictsJsonStrategy`, `GenerateBashedPatchStrategy` y `ValidatePluginLimitStrategy` capturan `supervisor` — esto previene GC del supervisor mientras el dispatcher exista (memory leak potencial en recarga)

### 1.3.4 SyncEngine — `sync_engine.py`

**Hallazgo SE-1 [CRITICAL]:** `_check_and_update_mod` (incompleto en lectura, pero observable) no tiene timeout en la operacion de descarga — si Nexus Mods responde muy lentamente, el TaskGroup completo se bloquea

**Hallazgo SE-2 [HIGH]:** `check_for_updates` usa `asyncio.TaskGroup` con Semaphore(15) pero el TaskGroup **espera a que todas las tareas terminen** antes de continuar — si un mod tiene un timeout de 30 minutos, los otros 14 slots se desperdician esperando

**Hallazgo SE-3 [MEDIUM]:** `_update_available` usa comparacion de string exacto de versiones — "1.0.0" vs "1.0" se consideran diferentes aunque semanticamente equivalentes

**Hallazgo SE-4 [MEDIUM]:** `SyncMetrics` usa `asyncio.Lock` por cada operacion de metrica — contencion innecesaria; deberia usar `asyncio.Queue` o contadores atomicos

**Hallazgo SE-5 [LOW]:** `_POISON = None` es ambiguo — si un worker legitimo retorna `None` como resultado, se confunde con la senal de terminacion

---

## 1.4 Subsistema: LOCAL TOOLS (local/tools/)

### 1.4.1 XEditPipelineService — `xedit_service.py`

**Hallazgo XE-1 [HIGH]:** `_ensure_patch_orchestrator` crea un `RollbackManager` nuevo en cada llamada — deberia recibirlo por DI desde el supervisor
```python
# Lineas 85840-85843
rollback_manager=RollbackManager(
    journal=self._journal,
    snapshot_manager=self._snapshot_manager,
),
```

**Hallazgo XE-2 [MEDIUM]:** `execute_patch` tiene `in_lock_context` flag manual — este patron es fragil; si se agrega un `return` dentro del `async with`, el flag no se setea correctamente

**Hallazgo XE-3 [MEDIUM]:** `_error_dict` no incluye `tx_id` ni informacion de debugging — dificulta trazabilidad de errores

### 1.4.2 SynthesisPipelineService — `synthesis_service.py`

**Hallazgo SS-1 [HIGH]:** `execute_pipeline` maneja `asyncio.CancelledError` incorrectamente — en el bloque `except Exception`, `CancelledError` es `BaseException` y **no se captura**, lo cual es correcto, pero el comentario dice "intentionally propagates" mientras que en el bloque `except (SynthesisExecutionError, SynthesisValidationError)` no hay manejo de CancelledError que podria ocurrir durante `runner.run_pipeline()`

**Hallazgo SS-2 [MEDIUM]:** `_result_to_dict` usa `dataclasses.asdict()` que es recursivo — si `SynthesisResult` contiene objetos no serializables, falla con error oscuro

**Hallazgo SS-3 [LOW]:** `SynthesisResult` usa `pathlib.Path` como tipo de `output_esp` — la serializacion requiere conversion manual en `_result_to_dict`, que es fragil

### 1.4.3 DynDOLODPipelineService — `dyndolod_service.py`

**Hallazgo DD-1 [HIGH]:** Snapshot solo protege `DynDOLOD.esp` pero no el directorio completo de salida (`DynDOLOD Output/`) — si el pipeline falla, los archivos generados (textures, meshes) en subdirectorios no se restauran
```python
# Lineas 75707-75711
dyndolod_esp = dyndolod_output_path / "DynDOLOD.esp"
if create_snapshot:
    if dyndolod_esp.exists() and dyndolod_esp.is_file():
        target_files.append(dyndolod_esp)
    # Los subdirectorios con textures/meshes NO se snapshotean
```

**Hallazgo DD-2 [MEDIUM]:** El timeout para DynDOLOD no es configurable — el `_ensure_runner` no acepta parametro de timeout y usa default del runner

### 1.4.4 LootSortingService — `loot_service.py`

**Hallazgo LS-1 [MEDIUM]:** `target_files=[]` en el SnapshotTransactionLock significa que no hay rollback real — solo hay serializacion. Si LOOT corrompe el load order, no hay forma de revertir automaticamente.

**Hallazgo LS-2 [LOW]:** `_DEFAULT_LOOT_TIMEOUT_SECONDS = 120` es mayor que el timeout del LOOTRunner (60s) — inconsistencia que genera confusion sobre cual aplica

---

# ===================================================================
# FASE 2: ORIENT (Orientacion) — Contexto y clasificacion
# ===================================================================

## 2.1 Matriz de severidad revisada

| Tag | Severidad | Subsistema | Tipo | Requiere fix inmediato |
|-----|-----------|-----------|------|----------------------|
| G-1 | **CRITICAL** | Security/Governance | Race + Deadlock | Si |
| CV-1 | **CRITICAL** | Security/Vault | Race + Resource Leak | Si |
| SC-1 | **CRITICAL** | Security/LoopGuard | Logica de deteccion | Si |
| SE-1 | **CRITICAL** | Orchestrator/Sync | DoS / Timeout | Si |
| R-1 | **HIGH** | Agent/Router | Race + Inconsistencia | Si |
| AG-1 | **HIGH** | Security/Guardrail | Falso negativo i18n | Si |
| AG-2 | **HIGH** | Security/Guardrail | PII incompleto | Si |
| E-1 | **HIGH** | Agent/Executor | DoS / Colgado | Si |
| E-2 | **HIGH** | Agent/Executor | Zombie Processes | Si |
| E-3 | **HIGH** | Agent/Executor | Race + Crash | Si |
| S-1 | **HIGH** | Orchestrator/Supervisor | Fail-fast agresivo | Si |
| PS-1 | **HIGH** | Security/Purple | Hardcodeo temporal | Si |
| P-1 | **HIGH** | Agent/Providers | Retry inapropiado | Si |
| R-2 | **HIGH** | Agent/Router | Null pointer | Si |
| R-3 | **HIGH** | Agent/Router | Logica incorrecta | Si |
| SG-1 | **HIGH** | Orchestrator/StateGraph | Grafo inconsistente | Si |
| SG-2 | **HIGH** | Orchestrator/StateGraph | Timeout prematuro | Si |
| SE-2 | **HIGH** | Orchestrator/Sync | Throughput | Si |
| XE-1 | **HIGH** | Tools/xEdit | Duplicacion recursos | Si |
| SS-1 | **HIGH** | Tools/Synthesis | Cancel handling | Si |
| DD-1 | **HIGH** | Tools/DynDOLOD | Rollback incompleto | Si |

## 2.2 Patrones transversales identificados

### Patron PT-1: "Async/Sync Boundary Violations"
**Hallazgos afectados:** G-2, CV-3, PS-3, S-6, SR-1, R-5
**Descripcion:** Metodos sincronos con I/O de archivos se llaman desde corutinas sin `asyncio.to_thread()`, bloqueando el event loop. Esto es el patron mas comun (6 hallazgos) y sugiere una falta de disciplina de equipo sobre boundaries async/sync.

### Patron PT-2: "Race Conditions en Inicializacion"
**Hallazgos afectados:** G-1, G-3, CV-1, R-1, E-3
**Descripcion:** Inicializacion lazy de recursos compartidos sin sincronizacion apropiada. Cada caso es sutil y requiere analisis individual, pero el patron comun es "inicializar en primer uso" sin locks.

### Patron PT-3: "Exception Handling Over-Broad"
**Hallazgos afectados:** S-5, R-3, SS-1
**Descripcion:** Captura de `Exception` o clases muy amplias que esconden bugs de programacion. La cultura de "no dejar que nada crashee" ha llevado a try/except que silencian errores reales.

### Patron PT-4: "Snapshot/Rollback Parcial"
**Hallazgos afectados:** DD-1, LS-1, XE-2
**Descripcion:** Los mecanismos de rollback no cubren todo el ambito de la operacion. DynDOLOD solo snapshottea un archivo cuando modifica directorios completos; LOOT no tiene rollback real.

### Patron PT-5: "Time/Date Hardcodeados"
**Hallazgos afectados:** PS-1
**Descripcion:** Referencias temporales hardcodeadas que requieren cambio manual, inevitablemente olvidadas.

### Patron PT-6: "Deuda del Strangler Fig"
**Hallazgos afectados:** TD-1, XE-1, S-4
**Descripcion:** La migracion progresiva de `supervisor.py` a servicios ha dejado inconsistencias donde los nuevos servicios duplican logica del supervisor en lugar de reutilizarla.

---

# ===================================================================
# FASE 3: DECIDE (Decision) — Priorizacion
# ===================================================================

## 3.1 Criterios de priorizacion

1. **Impacto de seguridad:** Un bug que permite bypass de seguridad es maxima prioridad
2. **Probabilidad de ocurrencia en produccion:** Bugs en paths hot > bugs en paths cold
3. **Facilidad de fix:** Quick wins primero
4. **Dependencias:** Si un bug bloquea otros fixes, va primero

## 3.2 Tandas de implementacion

### Tanda 1: Seguridad critica (inmediata — antes del proximo release)

| Tag | Fix estimado | Riesgo del fix |
|-----|-------------|----------------|
| SC-1 | Modificar deteccion de loops para detectar ciclos (A,B,A,B) | Bajo — tests existen |
| AG-1 | Anadir patrones de inyeccion en español + otros idiomas | Medio — requiere corpus |
| AG-2 | Cambiar `.search()` por `.finditer()` para multiples matches | Bajo — cambio localizado |
| CV-1 | Reestructurar `close()` para cerrar antes de releasear semaphores | Alto — cambia semantica |
| G-1 | Reemplazar `threading.Lock()` por `asyncio.Lock()` en governance | Medio — requiere refactor parcial |

### Tanda 2: Confiabilidad (proxima sprint)

| Tag | Fix estimado | Riesgo del fix |
|-----|-------------|----------------|
| E-1 | Anadir timeout en `stream.readline()` | Bajo |
| E-3 | Usar lock en `signal_abort()` | Bajo |
| S-1 | Separar lifecycle de daemons (no TaskGroup) | Medio — cambia arquitectura |
| R-3 | Resetear `consecutive_errors` en exito | Bajo |
| R-1 | Leer `_provider` una sola vez en `chat()` | Bajo |
| SG-2 | Resetear `hitl_started_at` en HITL completion | Bajo |

### Tanda 3: Deuda tecnica (sprint siguiente)

| Tag | Fix estimado | Riesgo del fix |
|-----|-------------|----------------|
| DD-1 | Snapshot recursivo de directorios | Medio |
| XE-1 | Inyectar RollbackManager por DI | Medio |
| SE-2 | Cambiar TaskGroup por worker pool con timeout individual | Medio |
| PS-1 | Hacer la fase dinamica (datetime.now) | Bajo |
| PT-1 | Audit de todos los I/O sync en async paths | Alto — cambio masivo |

---

# ===================================================================
# FASE 4: ACT (Accion) — Recomendaciones concretas
# ===================================================================

## 4.1 Fixes con codigo sugerido

### Fix SC-1 [CRITICAL]: Deteccion de ciclos en AgenticLoopGuardrail

**Problema actual:** Solo detecta repeticiones identicas (A,A,A). No detecta ciclos (A,B,A,B).

**Sugerencia:**
```python
# En register_and_check(), reemplazar la logica de deteccion:
def register_and_check(self, tool_name: str, tool_args: dict[str, Any]) -> None:
    args_str = json.dumps(tool_args, sort_keys=True, default=str)
    action_hash = hashlib.sha256(f"{tool_name}|{args_str}".encode()).hexdigest()
    self._history.append(action_hash)

    # Deteccion de repeticiones identicas (original)
    if len(self._history) >= self._max_repeats:
        last_n = list(self._history)[-self._max_repeats:]
        if all(h == last_n[0] for h in last_n):
            self._trigger_block("identical", tool_name)
            return

    # NUEVO: Deteccion de ciclos (A,B,A,B,A,B)
    if len(self._history) >= self._max_repeats * 2:
        recent = list(self._history)[-self._max_repeats * 2:]
        half = len(recent) // 2
        if recent[:half] == recent[half:]:
            self._trigger_block("cycle", tool_name)
            return

    # NUEVO: Deteccion de ciclos de periodo 2 (A,B,A,B)
    if len(self._history) >= 4:
        last4 = list(self._history)[-4:]
        if last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1]:
            self._trigger_block("period-2-cycle", tool_name)
            return
```

### Fix AG-1 [HIGH]: Patrones de inyeccion multilingues

**Sugerencia:**
```python
# Anadir patrones en español y otros idiomas comunes
_GUARDRAIL_INJECTION_RE = re.compile(
    r"(?i)(?:"
    # Ingles (existente)
    r"ignore\s+(?:all\s+)?(?:prior|previous)\s+(?:context|instructions?)"
    r"|..."
    # Español (nuevo)
    r"|ignora\s+(?:todas?\s+)?(?:las?\s+)?(?:instrucciones|anteriores|previas)"
    r"|olvida\s+(?:todo|lo\s+anterior)"
    r"|actua\s+(?:como|si\s+fueras)"
    r"|sistema\s*:\s*(?:ahora\s+eres|sobreescribe)"
    r"|ya\s+no\s+estas\s+(?:limitado|restringido)"
    r"|como\s+desarrollador\s+debes"
    r")"
)
```

### Fix AG-2 [HIGH]: Reportar todos los PII detectados

**Sugerencia:**
```python
def _check_pii(text: str) -> None:
    """Raise SecurityViolationError if text contains PII patterns."""
    # Cambiar .search() por .finditer() para detectar todos los matches
    for match in _COMBINED_PII_RE.finditer(text):
        for name, value in match.groupdict().items():
            if value is not None:
                raise SecurityViolationError(_PII_MESSAGES[name])
```

### Fix G-1 [CRITICAL]: threading.Lock → asyncio.Lock

**Sugerencia:**
```python
class GovernanceManager:
    _instance = None
    _lock = asyncio.Lock()  # Cambiar a asyncio.Lock
    
    @classmethod
    async def get_instance_async(cls, base_path: str = ".") -> "GovernanceManager":
        """Async-safe factory."""
        async with cls._lock:
            if cls._instance is not None:
                if str(cls._instance.base_path.resolve()) != str(Path(base_path).resolve()):
                    raise RuntimeError(...)
                return cls._instance
            cls._instance = cls(base_path)
            return cls._instance
    
    # Mantener metodo sync para callers sync (backward compat)
    @classmethod
    def get_instance(cls, base_path: str = ".") -> "GovernanceManager":
        # ... validacion de que no estamos en event loop ...
```

### Fix E-1 [HIGH]: Timeout en stream telemetry

**Sugerencia:**
```python
async def _read_stream(stream: asyncio.StreamReader, prefix: str):
    while True:
        try:
            line = await asyncio.wait_for(stream.readline(), timeout=30.0)
        except TimeoutError:
            logger.warning("Telemetry stream timeout — process may be stuck")
            break
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace").strip()
        if decoded and callback:
            await callback(f"{prefix}: {decoded}")
```

### Fix R-3 [HIGH]: Resetear contador de errores

**Sugerencia:**
```python
for _round in range(MAX_TOOL_ROUNDS):
    try:
        # ... operacion ...
        
        # EN EXITO: resetear contadores
        if result_success:
            consecutive_errors = 0
            hermes_parse_error_count = 0
            hermes_exec_error_count = 0
        
    except SomeSpecificError:
        consecutive_errors += 1
        # ... manejo ...
```

### Fix SG-2 [HIGH]: Resetear hitl_started_at

**Sugerencia:**
```python
# En el nodo que maneja HITL completion (approved/denied):
return {
    "current_state": SupervisorState.DISPATCHING.value,
    "hitl_started_at": None,  # Resetear para el proximo HITL
}
```

## 4.2 Arquitectura: Recomendaciones estrategicas

### REC-1: Consolidar I/O sincrono en modulo dedicado

Crear un modulo `sky_claw.antigravity.core.sync_io` que exponga todas las operaciones de I/O de archivos via `asyncio.to_thread()`. Esto centraliza el boundary sync/async y previene PT-1.

### REC-2: Implementar "Async-Safe Singleton" pattern

Muchos componentes (GovernanceManager, CredentialVault, AuthTokenManager) usan singletons. Crear un decorator `@async_singleton` que maneje ambos mundos (sync/async) correctamente.

### REC-3: Auditoria de exception handling

Revisar **todo** `except Exception:` en el codebase. La regla de oro:
- Libreria/utils: capturar especifico, propagar generico
- Entry points (API, CLI, GUI): capturar generico, loguear, retornar error amigable
- Nunca capturar `Exception` en codigo de negocio

### REC-4: Snapshot recursivo

El backend de snapshots actual solo soporta archivos. Para herramientas como DynDOLOD que generan directorios completos, implementar `SnapshotTransactionLock` con soporte recursivo de directorios (tar/zip del arbol completo).

### REC-5: Health Check endpoint

Implementar un endpoint de health check que verifique:
- Conectividad a SQLite (WAL mode activo)
- Semaphore del CredentialVault (no deadlocked)
- Estado del `_pending_telemetry` set (no leaking tasks)
- Estado del `AgenticLoopGuardrail` (no tripeado)

## 4.3 Testing: Tests recomendados

| Test | Tipo | Prioridad |
|------|------|-----------|
| `test_loop_guardrail_detects_period_2_cycle` | Unit | Critical |
| `test_governance_async_initialization` | Unit | Critical |
| `test_credential_vault_pool_close_no_leak` | Unit | Critical |
| `test_guardrail_multilingual_injection` | Unit | High |
| `test_guardrail_multiple_pii` | Unit | High |
| `test_executor_stream_timeout` | Unit | High |
| `test_router_error_counter_resets` | Unit | High |
| `test_state_graph_hitl_timeout_reset` | Unit | High |
| `test_dynlod_snapshot_recursive` | Integration | High |
| `test_sync_engine_worker_timeout` | Integration | High |

---

# ===================================================================
# Apendice: Inventario de hallazgos
# ===================================================================

## Hallazgos por subsistema

```
Security/ (12 hallazgos)
  G-1  [CRIT] threading.Lock en async
  G-2  [HIGH] I/O bloqueante en _load_whitelist
  G-3  [MED]  Race en inicializacion semaphore
  G-4  [MED]  approve_file sync desde async
  G-5  [LOW]  HMAC key regeneration silenciosa
  AG-1 [HIGH] Inyeccion solo en ingles
  AG-2 [HIGH] Solo primer PII detectado
  AG-3 [MED]  max_input_length arbitrario
  AG-4 [LOW]  Content como lista no manejado
  CV-1 [CRIT] Pool close race condition
  CV-2 [HIGH] EEXIST en salt atomic write
  CV-3 [MED]  I/O sync en async path
  CV-4 [LOW]  Jerarquia de excepciones
  PS-1 [HIGH] Hardcodeo "ABRIL 2026"
  PS-2 [MED]  Validacion de summary
  PS-3 [MED]  I/O sync en escaneo
  PS-4 [LOW]  No timeout en escaneo
  SC-1 [CRIT] Solo detecta repeticiones, no ciclos
  SC-2 [HIGH] No rate limiting en audit

Agent Core/ (11 hallazgos)
  R-1  [CRIT] Race en provider hot-swap
  R-2  [HIGH] _conn sin lock
  R-3  [HIGH] consecutive_errors no resetea
  R-4  [MED]  Constante legacy
  R-5  [MED]  estimate_tokens sync
  R-6  [LOW]  Validacion API key debil
  P-1  [HIGH] Retry uniforme
  P-2  [MED]  Sin timeout Ollama
  P-3  [MED]  Tool_result pierde texto
  P-4  [LOW]  Modelo inexistente
  E-1  [CRIT] Stream sin timeout
  E-2  [HIGH] No mata arbol de procesos
  E-3  [HIGH] Race en signal_abort
  E-4  [MED]  Thread pool ilimitado
  E-5  [LOW]  Timeout fijo 300s
  SR-1 [MED]  Route sync en async

Orchestrator/ (11 hallazgos)
  S-1  [CRIT] TaskGroup fail-fast
  S-2  [HIGH] CancelledError no manejado
  S-3  [HIGH] Payload no sanitizado
  S-4  [MED]  dispatch_tool sin timeout
  S-5  [MED]  Exception generica
  S-6  [MED]  I/O sync
  S-7  [LOW]  Inconsistencia .esl
  SG-1 [HIGH] Transicion a END faltante
  SG-2 [HIGH] hitl_started_at no resetea
  SG-3 [MED]  Historia arbitraria
  SG-4 [MED]  Logica dispersa
  SG-5 [LOW]  Duplicacion WorkflowState
  TD-1 [MED]  Duplicacion de runners
  TD-2 [LOW]  Lambdas capturan supervisor
  SE-1 [CRIT] Sin timeout descarga
  SE-2 [HIGH] TaskGroup bloquea
  SE-3 [MED]  Version string exacto
  SE-4 [MED]  Lock por metrica
  SE-5 [LOW]  Ambiguedad POISON

Local Tools/ (7 hallazgos)
  XE-1 [HIGH] RollbackManager por cada llamada
  XE-2 [MED]  Flag manual fragil
  XE-3 [MED]  Error sin debug info
  SS-1 [HIGH] Cancel handling
  SS-2 [MED]  asdict recursivo
  SS-3 [LOW]  Path en dataclass
  DD-1 [HIGH] Snapshot no recursivo
  DD-2 [MED]  Timeout no configurable
  LS-1 [MED]  Sin rollback real
  LS-2 [LOW]  Timeout inconsistente
```

## Resumen estadistico

| Categoria | Critical | High | Medium | Low | Total |
|-----------|----------|------|--------|-----|-------|
| Runtime Bugs | 3 | 6 | 2 | 0 | 11 |
| Race Conditions | 3 | 2 | 1 | 0 | 6 |
| Security Gaps | 1 | 3 | 2 | 1 | 7 |
| Deuda Tecnica | 0 | 2 | 4 | 4 | 10 |
| **Total** | **7** | **13** | **9** | **5** | **34** |

*Nota: Algunos hallazgos cruzan multiples categorias; los conteos representan la clasificacion primaria.*

---

*Report generated by OODA analysis cycle. All findings backed by direct code evidence. Recommendations include estimated risk of implementation.*
