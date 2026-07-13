# ADR 0004 — GovernanceManager: el singleton sync (threading.Lock + I/O de init) es defendible

**Fecha:** 2026-07-13
**Estado:** Aceptada
**Contexto de origen:** cierre del arbitraje de auditorías. Evaluación OODA+TOT de
los hallazgos **G-1 [CRITICAL]** y **G-2 [HIGH]** del `skyclaw_ooda_analysis.md`,
contra `main @ d0aab3b`. Es el último CRÍTICO nominal del análisis; se evalúa
antes de tocar (regla del arbitraje).

## Contexto

Dos hallazgos del OODA analysis apuntan al mismo patrón en
`sky_claw/antigravity/security/governance.py`:

- **G-1 [CRITICAL]:** `GovernanceManager.get_instance()` usa un
  `threading.Lock()` de clase (`_lock`) para el singleton. Riesgo señalado: si un
  `async def` llama `get_instance()` y el lock está contencionado por otro hilo,
  el `with cls._lock` bloquea el thread del event loop hasta liberarlo → congela
  todas las corutinas.
- **G-2 [HIGH]:** `_load_whitelist()` hace I/O de archivo sync (`read_bytes`,
  `read_text`) sin `asyncio.to_thread`. Si se invocara desde una corutina en el
  hot-path, bloquearía el event loop.

## Verificación de vigencia (Observe/Orient)

- **`_lock` se usa en un único lugar:** `get_instance()` (classmethod sync). Los
  métodos async del manager (`is_scanned_and_clean`, `update_scan_result`,
  `get_file_hash_async`) NO lo tocan; el trabajo pesado (hashing) ya va por
  `asyncio.to_thread` (`_hash_file_blocking`).
- **`_load_whitelist()` se invoca una sola vez**, desde `__init__` (al construir
  el singleton) — no desde ningún método async del hot-path.
- **Call-sites reales de `get_instance()`** (grep sobre `sky_claw/`, no-tests):
  - `app_context.py:467` — bootstrap **one-time** del proceso (inyección del
    lifecycle en el arranque). Corre dentro de `_start_full_inner` (async), así que
    la primera creación (y su `_load_whitelist`) ya ejecuta en el event loop, pero
    en el arranque, sin otras corutinas críticas compitiendo.
  - `purple_security_agent.py:137`, `security_mode.py:50` — dentro de
    `approve_file`, una acción **sync y esporádica** del operador.
  - `metacognitive_logic.py:94` — **desde `async def _phase_resolve()`**, alcanzado
    por la API `audit_resource()` y el `PurpleSecurityAgent`. Corrección tras el
    review de Codex (#289): este call-site **es async**, así que la afirmación
    "get_instance no se llama desde código async" sería falsa y no se sostiene.
- **Por qué el veredicto se mantiene, corregido:**
  - **G-1 (threading.Lock):** aunque `_phase_resolve` (async) lo invoque, la app
    corre un **único event loop**; `approve_file` y el audit no compiten desde
    hilos paralelos por `_lock`. Sin contención cross-thread, el `with cls._lock`
    es instantáneo y no cede ni bloquea el loop de forma apreciable.
  - **G-2 (`_load_whitelist`):** el I/O sync **sí** puede correr en el loop (primera
    creación, sea en el bootstrap o en el primer `_phase_resolve`). Pero es
    **one-time** y trivial — leer un JSON de hashes + verificación HMAC — y queda
    **dominado por el escaneo de archivos** que el propio `_phase_resolve` ejecuta
    a continuación (rglob + lectura por archivo, ya en `asyncio.to_thread` tras
    #270/PT-1). El costo marginal de moverlo fuera del loop no lo justifica.

## Alternativas evaluadas (Tree of Thoughts)

### (a) No cambiar — **elegida**

`threading.Lock` es el primitivo correcto para un singleton **sincrónico**. El
bloqueo del event loop que describe G-1 exige contención cross-thread que la app
no produce (bootstrap one-time + `approve_file` esporádico), y aun con
contención el lock se sostiene microsegundos. El I/O de `_load_whitelist` (G-2)
es de construcción, one-time, en el arrange — no hot-path.

### (b) Migrar a `get_instance_async()` con `asyncio.Lock` — descartada

Es lo que sugería el OODA. Costo: **dos** factories (sync + async) a mantener en
sincronía, y todos los call-sites sync (`approve_file`) tendrían que elegir cuál
usar. Beneficio: ≈ 0 — resuelve una contención que no ocurre. Sobre-ingeniería:
más superficie de bug que el patrón que "arregla".

### (c) `to_thread` en `_load_whitelist` — descartada

Requiere volver `get_instance`/`__init__` async (no pueden `await`). Contamina
todo el árbol de construcción del singleton para mover un I/O one-time de
bootstrap fuera del loop, cuando el bootstrap ya hace I/O sync de init por diseño.

## Decisión

**No cambiar.** El patrón singleton sync de `GovernanceManager` (con
`threading.Lock` e I/O de init sincrónico) se mantiene. G-1 y G-2 se cierran como
**evaluados — sin cambio de código**, pero por el motivo correcto: `get_instance()`
**sí** se invoca desde async, y aun así el costo (lock sin contención + carga de
whitelist one-time y trivial) no justifica migrar a `asyncio.Lock` / diferir la
carga. Lo que **no** se sostiene es la fundamentación original ("no se llama desde
async"), corregida aquí tras el review de Codex.

## Consecuencias

- La decisión **no** descansa en una invariante de "get_instance = sync-only"
  (sería falsa: `_phase_resolve` async lo llama). Descansa en el **costo trivial y
  one-time** de la primera creación: `threading.Lock` sin contención cross-thread
  (un solo loop) + un `_load_whitelist` de un JSON chico, dominado por el escaneo
  que el audit hace a continuación. El comentario en `_lock` refleja esto.
- Todo trabajo pesado o de I/O **recurrente** del manager sigue yendo por
  `asyncio.to_thread` (como ya hace el hashing), no bajo `_lock`. La carga de
  whitelist queda exceptuada por ser one-time.

## Criterio de reversión

Reabrir (empezando por la rama (b), acotada) si aparece cualquiera de estas
condiciones:

1. La **primera creación** del singleton pasa a ocurrir en un punto sensible a
   latencia y **recurrente** — p. ej. si `get_instance()` deja de estar cacheado
   o se reconstruye por request, de modo que `_load_whitelist` deje de ser
   one-time y su I/O sync en el loop se vuelva un costo repetido.
2. `get_instance()` pasa a llamarse desde **múltiples hilos concurrentes** (p. ej.
   un `ThreadPoolExecutor` que construya el singleton en paralelo con el loop),
   materializando la contención del `threading.Lock` que hoy no ocurre.
3. `_load_whitelist` crece hasta un I/O caro (whitelist grande, validación
   costosa), volviendo relevante diferirlo/`to_thread` aunque siga siendo
   one-time.
