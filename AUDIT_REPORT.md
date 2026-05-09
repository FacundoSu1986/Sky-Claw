# Audit Report — Sky-Claw Orchestrator Subsystem

**Fecha:** 2026-05-09  
**Rama auditada:** `claude/reverent-almeida-20df97` (rebase sobre `origin/main @ e3a67a5`)  
**Archivos en alcance primario:** `async_path_resolver.py`, `event_bus.py`, `dlq_manager.py`  
**Metodología:** OODA × 2 ciclos + TDD estricto (Red → Green → Refactor)  
**Tags adoptados:** `C-2`, `H-04`, `H-05` (integrados al esquema `M-01`/`H-03`/`C-1`/`F2.2` del equipo)

---

## Resumen ejecutivo

Se identificaron **3 bugs verificados** con evidencia directa en código (1 Critical, 2 High).
Todos están **corregidos y cubiertos con tests** en esta tanda.
Se documentan además **5 falsos positivos** cerrados explícitamente y **5 items de backlog** de menor severidad o con pre-condiciones de re-verificación.

| Tag | Severidad | Archivo | Bug | Estado |
|-----|-----------|---------|-----|--------|
| `C-2` | **Critical** | `async_path_resolver.py` | Future leak + waiters colgados bajo excepción inesperada o cancelación del owner | ✅ Corregido |
| `H-04` | **High** | `event_bus.py` | Backpressure descarta eventos silenciosamente sin notificar a la DLQ | ✅ Corregido |
| `H-05` | **High** | `dlq_manager.py` | Double-dispatch bajo acceso concurrente; recovery on every-poll en lugar de startup | ✅ Corregido |

---

## Hallazgos corregidos

### C-2 — Critical: Future leak en AsyncPathResolver bajo excepción inesperada o cancelación

**Archivo:** `sky_claw/antigravity/core/async_path_resolver.py:114-157`

**Bug:** El bloque `except (OSError, RuntimeError)` en `resolve_safe()` solo capturaba dos tipos de excepción. Si `_resolve_blocking` lanzaba cualquier otra excepción (p.ej. `MemoryError`, `KeyboardInterrupt`) o si la corutina *owner* era cancelada mientras esperaba `asyncio.to_thread()`, el bloque `_inflight[key]` quedaba envenenado y el `Future` nunca recibía `set_result`/`set_exception`. Las corutinas *waiters* en `await asyncio.shield(fut)` quedaban colgadas indefinidamente.

**Evidencia:** `async_path_resolver.py:131-144` — solo `(OSError, RuntimeError)` capturadas; ningún `finally` ni `BaseException` para limpiar `_inflight`.

**Fix aplicado:** Se añadió bloque `except BaseException` después del `except (OSError, RuntimeError)` existente que:
1. Limpia `_inflight.pop(key, None)` atómicamente bajo el lock.
2. Si el `Future` no está resuelto: aplica `fut.cancel()` para `CancelledError`, o `fut.set_exception(exc)` para todo lo demás.
3. Re-lanza la excepción al owner para no cambiar la semántica.

```python
# async_path_resolver.py — bloque añadido tras except (OSError, RuntimeError):
except BaseException as exc:
    # C-2: MemoryError, CancelledError y cualquier excepción no anticipada.
    async with self._lock:
        self._inflight.pop(key, None)
    if not fut.done():
        if isinstance(exc, asyncio.CancelledError):
            fut.cancel()
        else:
            fut.set_exception(exc)
    raise
```

**Tests:**
- `tests/test_async_path_resolver_resilience.py::test_unexpected_exception_propagates_to_waiters_and_clears_inflight` — MemoryError propagada a todos los waiters; `_inflight` vacío post-excepción.
- `tests/test_async_path_resolver_resilience.py::test_owner_cancellation_does_not_hang_waiters` — cancelar el owner desbloquea waiters; `_inflight` vacío.

**Resultado RED → GREEN:** Ambos tests fallaban con timeout en implementación original; pasan en < 0.3 s tras el fix.

---

### H-04 — High: Backpressure descarta eventos sin notificar a la DLQ

**Archivo:** `sky_claw/antigravity/core/event_bus.py:152-176`

**Bug:** Cuando `len(self._pending_tasks) >= _MAX_PENDING_TASKS` (cap introducido por commit `b6c433f`), el dispatch se descarta con `continue`. La DLQ inyectada era ignorada incluso cuando estaba configurada — los eventos se perdían silenciosamente sin reintento.

**Evidencia:** `event_bus.py:161-176` — el `continue` ejecuta antes de verificar `self._dlq`.

**Fix aplicado:**
1. Definida nueva excepción `BackpressureDropped(RuntimeError)` exportada desde `event_bus.py`.
2. Cuando hay DLQ y `_pending_tasks` está lleno: se lanza `asyncio.create_task(_enqueue_backpressure_drop(...))` antes del `continue`.
3. Nuevo método `_enqueue_backpressure_drop()`: construye `BackpressureDropped` y llama `await self._dlq.enqueue(event, callback, exc)`. Failure en enqueue → log critical, evento perdido (best-effort, igual que `_safe_execute`).
4. Si no hay DLQ (`self._dlq is None`): comportamiento original preservado (backward compatible).

```python
# event_bus.py — sección backpressure modificada:
if len(self._pending_tasks) >= self._MAX_PENDING_TASKS:
    ...
    if self._dlq is not None:
        asyncio.create_task(
            self._enqueue_backpressure_drop(event, callback),
            name=f"dlq-backpressure-{event.topic}",
        )
    continue
```

**Tests:**
- `tests/test_event_bus_backpressure.py::test_backpressure_routes_dropped_events_to_dlq` — satura `_pending_tasks` a 1, publica segundo evento, verifica que `dlq.enqueue` es llamado con `BackpressureDropped` como excepción.

**Resultado RED → GREEN:** Test fallaba con `ImportError` (clase no existía) → `AssertionError` (enqueue nunca llamado). Pasa tras el fix.

---

### H-05 — High: Double-dispatch y every-poll recovery en DLQManager

**Archivo:** `sky_claw/antigravity/core/dlq_manager.py:241-316` (process_row) y `333-365` (fetch_due_batch)

**Bug (doble foco):**

**a) Double-dispatch bajo concurrencia:** `_process_row` ejecutaba `UPDATE ... SET status='in_progress' WHERE id=?` sin guardia `AND status='pending'`. Dos instancias de `DLQManager` compartiendo la misma SQLite (o dos ticks solapados de un handler lento) podían ambas obtener `rowcount > 0` y ejecutar el handler dos veces. SQLite WAL no protege de TOCTOU si la condición de selección no está en el UPDATE.

**b) Recovery agresiva en cada poll:** `_fetch_due_batch` ejecutaba en cada tick: `UPDATE ... SET status='pending' WHERE status='in_progress' AND updated_at + 60000 < ?`. Si un handler legítimamente tardaba > 60 s (I/O pesado, subprocess), el siguiente tick lo reseteaba a `pending` y un nuevo `_process_row` arrancaba — double-dispatch en producción single-worker con handlers lentos.

**Evidencia:**
- `dlq_manager.py:245-249` — UPDATE sin `AND status='pending'`.
- `dlq_manager.py:341-347` — recovery UPDATE en cada poll, no sólo al startup.
- `test_dlq_manager.py:158-162` — comentario del equipo indicando comportamiento observado pero sin fix.

**Fix aplicado (3 cambios atómicos):**

1. **`_process_row` — atomic claim:**
```python
cur = await db.execute(
    "UPDATE dead_letter_events SET status='in_progress', updated_at=?"
    " WHERE id=? AND status='pending'",
    (now, row.id),
)
await db.commit()
if cur.rowcount == 0:
    logger.debug("DLQ: fila id=%d ya fue tomada por otro worker — omitiendo", row.id)
    return
```

2. **`_ensure_schema` — startup one-shot recovery:**
```python
# Al finalizar la creación del schema, recuperar filas in_progress de crash previo:
now = self._clock()
await db.execute(
    "UPDATE dead_letter_events SET status='pending', updated_at=?"
    " WHERE status='in_progress' AND updated_at + 60000 < ?",
    (now, now),
)
await db.commit()
```

3. **`_fetch_due_batch` — eliminación de every-poll recovery:** Se eliminaron las líneas 341-347 (el UPDATE de recovery). El docstring se actualizó con nota `H-05`.

**Contrato post-fix:** Una fila que crashea mid-proceso queda `in_progress` hasta el próximo arranque del proceso (no auto-recupera en el mismo proceso). Esto es aceptable en Sky-Claw single-worker; el startup recovery lo limpia. Para workers multi-proceso, la guardia `AND status='pending'` del atomic claim previene ejecución doble independientemente del recovery.

**Tests:**
- `tests/test_dlq_double_dispatch.py::test_concurrent_process_row_calls_handler_exactly_once` — dos `DLQManager` comparten DB, ambos llaman `_process_row` sobre la misma fila concurrentemente → exactamente 1 ejecución del handler (era 2 antes del fix).
- `tests/test_dlq_double_dispatch.py::test_in_progress_rows_recovered_on_startup` — fila insertada directamente como `in_progress` con `updated_at=0`; nuevo `DLQManager._ensure_schema()` con `clock=5_000_000` → fila pasa a `pending`.

**Resultado RED → GREEN:** Test 1 fallaba con `len(calls) == 2`; Test 2 con `status == 'in_progress'`. Ambos pasan tras el fix.

---

## Falsos positivos cerrados (no re-auditar)

Estos items aparecieron en el análisis inicial pero la lectura directa del código los refutó:

| Archivo | Líneas | Diagnóstico inicial | Por qué es falso positivo |
|---------|--------|--------------------|-----------------------------|
| `snapshot_manager.py` | 590-591, 602-603 | `open()` en async | `open()` está dentro de `_read_file()` pasado a `asyncio.to_thread(line 594)` — explícitamente síncrono |
| `async_path_resolver.py` | 107-119 | Race en double-check del cache | La creación del `Future` ocurre **dentro** del mismo `async with self._lock` — no es race |
| `db_lifecycle.py` | 425-443 `_sync_shutdown` | `sqlite3.connect()` bloqueante en atexit | Ya tiene `timeout=2` y `SHUTDOWN_CHECKPOINT_TIMEOUT_SECONDS` — bloqueo intencional y acotado en atexit |
| `state_graph.py` | 1109-1135 `visualize` | I/O bloqueante en async | Método es `def` (no `async`); único caller real es `tests/test_state_graph.py:337` — no en hot path async |
| `dlq_manager.py` | 341-347 | Zombie rows never recovered | Recovery ya existía; bug real era double-dispatch por falta de guardia en UPDATE — reformulado como H-05 |

---

## Backlog (hallazgos pendientes de próxima iteración)

Detectados con evidencia pero **fuera del alcance de esta tanda** por severidad menor, fix más invasivo, o dependencia de re-verificación post-rebase.

### Medium — SSRF: `socket.getaddrinfo()` bloqueante en validador de URL

**Archivo:** `sky_claw/antigravity/core/validators/ssrf.py:88-103` + `sky_claw/antigravity/core/schemas.py:54-58`  
**Bug:** `socket.getaddrinfo()` es bloqueante. Único caller verificado: `ScrapingQuery.url` field_validator (una vez por consulta de usuario, no en handlers paralelos).  
**Fix recomendado:** Añadir `validate_url_ssrf_async()` usando `asyncio.to_thread`; migrar el field_validator cuando se construye desde async. Mantener la sync para callers legacy.  
**Por qué no esta tanda:** No está en hot path concurrente — impacto real bajo.

### Medium — Recovery hardcodeada a 60 s en DLQManager

**Archivo:** `sky_claw/antigravity/core/dlq_manager.py` (constante en `_ensure_schema` tras fix H-05)  
**Nota:** El fix H-05 ya la movió a startup; sigue siendo una constante mágica. Convertir a parámetro `stale_timeout_ms` en `__init__`.  
**Por qué no esta tanda:** Cosmético post-fix; no afecta correctitud.

### Medium — Single connection bottleneck en JournalManager

**Archivo:** `sky_claw/antigravity/db/journal.py:243-275`  
**Bug reportado:** Única `aiosqlite.Connection` compartida entre tasks; protegido por `_lock`. Bottleneck bajo carga.  
**REQUIERE RE-VERIFICACIÓN:** PR M-01 B (`b2638f9`) refactorizó este archivo (+64 líneas) inyectando `DatabaseLifecycleManager`. El bottleneck puede haber sido reemplazado por pool gestionado.

### Low — PRAGMA order en schema creation

**Archivo:** `sky_claw/antigravity/db/journal.py:255-256`  
**Bug reportado:** `executescript(SCHEMA)` antes de `PRAGMA journal_mode=WAL`; cosmético.  
**REQUIERE RE-VERIFICACIÓN:** Mismo PR M-01 B.

### Medium — `threading.Lock()` global en GovernanceManager

**Archivo:** `sky_claw/antigravity/security/governance.py:36`  
**Bug reportado:** `threading.Lock()` global; verificar que ningún `async def` lo tome.  
**REQUIERE RE-VERIFICACIÓN:** PR M-01 C (`ec46f9f`) refactorizó GovernanceManager para usar `DatabaseLifecycleManager`; commit `aa55da8` (`perf(security): async file hashing`) ya cerró el hashing bloqueante. El lock puede haber cambiado de naturaleza.

---

## Archivos modificados

```
sky_claw/antigravity/core/async_path_resolver.py   (fix C-2: BaseException handler + _inflight cleanup)
sky_claw/antigravity/core/event_bus.py             (fix H-04: BackpressureDropped class + DLQ routing)
sky_claw/antigravity/core/dlq_manager.py           (fix H-05: atomic claim + startup-only recovery)
```

## Archivos nuevos (tests)

```
tests/test_async_path_resolver_resilience.py       (C-2: 2 tests, ambos GREEN)
tests/test_event_bus_backpressure.py               (H-04: 1 test, GREEN)
tests/test_dlq_double_dispatch.py                  (H-05: 2 tests, ambos GREEN)
```

---

## Resultado de regresión global

```
pytest tests/ --ignore=tests/test_credential_vault.py --ignore=tests/test_credential_vault_sec02.py
1657 passed, 15 skipped, 0 failed  (67 s)
```

Las 2 exclusiones (`test_credential_vault*.py`) son fallos **pre-existentes** por error de ACL de Windows (`icacls` → error 1332: cuenta no mapeada). Reproducibles en `origin/main` antes de cualquier cambio de esta tanda.

---

## Notas de arquitectura

- **Backward compatibility:** Los 3 fixes preservan la API pública. `BackpressureDropped` es exportada como nueva clase pública de `event_bus.py`; no rompe imports existentes.
- **Contrato DLQ multi-worker:** El atomic claim (`AND status='pending'`) hace al DLQManager correcto bajo N workers concurrentes sin necesidad de lease/TTL ni migración de schema.
- **Idempotencia de handlers:** El fix H-05 garantiza *at-most-once* dispatch por tick; los handlers DLQ siguen siendo responsables de idempotencia (contrato documentado, no nuevo).
- **OODA changelog:** Dos ciclos OODA refinaron el plan inicial: H3 reformulado (zombie rows → double-dispatch), H4 bajado de High a Medium/backlog, 4 falsos positivos cerrados.
