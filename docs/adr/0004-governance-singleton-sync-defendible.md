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
    lifecycle en el arranque), secuencial, sin otro hilo compitiendo.
  - `purple_security_agent.py:137`, `security_mode.py:50` — dentro de
    `approve_file`, una acción **sync y esporádica** del operador.
  - `metacognitive_logic.py:94` — obtención del singleton ya construido.
  - Ninguno lo envuelve en `asyncio.to_thread` desde varias corutinas/hilos: **no
    existe el escenario de contención cross-thread** que G-1 requiere. (El propio
    OODA analysis lo admite: "callers async podrían invocarlo en
    `asyncio.to_thread()` pero **no lo hacen** en el código observado".)
- **Duración del lock:** tras la primera creación, `get_instance()` solo compara
  `base_path` y retorna — microsegundos. Solo la **primera** llamada corre
  `__init__`/`_load_whitelist` (I/O de archivo, ~ms) bajo el lock, y ocurre en el
  bootstrap, no en el hot-path.

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
**evaluados — sin cambio de código**: el riesgo descrito no es alcanzable con los
call-sites actuales.

## Consecuencias

- Invariante que sostiene esta decisión: **`get_instance()` es solo para
  bootstrap/acciones sync esporádicas** — no debe invocarse en el hot-path async
  ni desde múltiples hilos concurrentes. Un comentario en `_lock` fija esta
  invariante en el código.
- Todo trabajo pesado o de I/O recurrente del manager sigue yendo por
  `asyncio.to_thread` (como ya hace el hashing), no bajo `_lock`.

## Criterio de reversión

Reabrir (empezando por la rama (b), acotada) si aparece cualquiera de estas
condiciones:

1. Un call-site nuevo invoca `get_instance()` **en el hot-path async** (por
   request/iteración), no solo en bootstrap.
2. `get_instance()` pasa a llamarse desde **múltiples hilos concurrentes** (p. ej.
   un `ThreadPoolExecutor` que construya el singleton en paralelo con el loop).
3. `__init__`/`_load_whitelist` crece hasta un I/O caro y recurrente (no
   one-time), volviendo relevante moverlo fuera del loop.
