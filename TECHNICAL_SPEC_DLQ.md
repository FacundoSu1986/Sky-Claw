# TECHNICAL_SPEC_DLQ.md — Dead Letter Queue para CoreEventBus

## 1. Context

**Problema detectado.** `CoreEventBus` en `sky_claw/core/event_bus.py` implementa pub/sub asíncrono
basado en `asyncio.Queue` + `asyncio.create_task()`. El método `_safe_execute` captura excepciones
de los handlers, las loguea con `logger.error(..., exc_info=True)`, y **descarta el evento**. Esto
es fire-and-forget puro: si el handler de notificación de Telegram falla por un 500 transitorio,
el evento se pierde para siempre sin rastro persistente ni mecanismo de reintento.

**Resultado esperado.** Cualquier excepción no controlada de un handler persiste el evento +
referencia al handler fallido en una Dead Letter Queue respaldada por SQLite
(`~/.sky_claw/dlq/dlq.db`). Un worker `asyncio.Task` acoplado al ciclo de vida del bus reintenta
con backoff exponencial (2 s → 32 s, 5 intentos). Tras agotar reintentos el evento queda en
`status='dead'` para auditoría humana, nunca se borra silenciosamente.

**Aislamiento.** Todo el cambio vive en `sky_claw/core/`. No se toca `sky_claw/gui/` ni
`sky_claw/orchestrator/`.

---

## 2. Estructura de Almacenamiento

### 2.1 Ubicación

`~/.sky_claw/dlq/dlq.db` — resuelto vía `Path.home() / ".sky_claw" / "dlq" / "dlq.db"`.
Directorio creado lazy en el primer `enqueue()` con `mkdir(parents=True, exist_ok=True)`.
DB separada del `sky_claw_state.db` principal (aislamiento: un bloqueo de la DLQ no afecta la DB
transaccional de estado).

### 2.2 Esquema SQLite

```sql
CREATE TABLE IF NOT EXISTS dead_letter_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT    NOT NULL,
    payload_json  TEXT    NOT NULL,   -- json.dumps(event.payload, sort_keys=True)
    source        TEXT    NOT NULL,
    event_ts_ms   INTEGER NOT NULL,   -- event.timestamp_ms original
    handler_name  TEXT    NOT NULL,   -- f"{cb.__module__}.{cb.__qualname__}"
    error_type    TEXT    NOT NULL,
    error_message TEXT    NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER NOT NULL,   -- epoch ms
    status        TEXT    NOT NULL DEFAULT 'pending',
                  -- CHECK(status IN ('pending','in_progress','dead'))
    enqueued_at   INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dlq_status_retry
    ON dead_letter_events(status, next_retry_at);
```

### 2.3 Pragmas de conexión

Replica el patrón de `sky_claw/core/database.py`:

- `journal_mode=WAL` — lecturas concurrentes sin bloquear escrituras
- `foreign_keys=ON`
- `synchronous=NORMAL` — balance durabilidad/throughput
- `busy_timeout=5000` — tolerar contención transitoria

### 2.4 Serialización

`Event` es `@dataclass(frozen=True, slots=True)` con `topic:str, payload:dict,
timestamp_ms:int, source:str`. Se persiste `payload` como JSON vía
`json.dumps(payload, sort_keys=True, default=str)`.

### 2.5 Identificador de handler

`handler_name = f"{cb.__module__}.{cb.__qualname__}"`.
Fallback para callables sin `__qualname__` (lambdas, `functools.partial`): `repr(cb)`.

### 2.6 No bloqueo del bus

`DLQManager.enqueue()` es `async` y ejecuta un solo `INSERT` con aiosqlite (latencia <5 ms en
WAL mode). Si `enqueue` falla (disco lleno, DB corrupta) se captura en `_safe_execute`, se loguea
CRITICAL, y el bus continúa — mejor perder el evento que crashear el bus.

---

## 3. Mecanismo de Reintento

### 3.1 Ciclo de vida del worker

`DLQManager` expone:

```python
async def start() -> None          # asyncio.create_task(_retry_loop())
async def stop() -> None           # task.cancel() + await + swallow CancelledError
async def enqueue(event, handler, exc) -> None
async def list_pending() -> list[DLQRow]
async def list_dead() -> list[DLQRow]
```

`CoreEventBus.start()`:
1. Si `self._dlq is not None` → `await self._dlq.start()`
2. `self._dispatch_task = asyncio.create_task(self._dispatch_loop())`

`CoreEventBus.stop()` (tras drenar la queue):
- Si `self._dlq is not None` → `await self._dlq.stop()`

### 3.2 Loop de reintento

```python
async def _retry_loop(self) -> None:
    while True:
        rows = await self._fetch_due_batch(limit=50)
        if not rows:
            await asyncio.sleep(1.0)
            continue
        for row in rows:
            await self._process_row(row)
```

`_process_row(row)`:
1. `UPDATE ... SET status='in_progress'`
2. Resolver handler: si `None` → `_mark_dead(reason="handler_not_registered")`
3. Reconstruir `Event` desde columnas DB
4. `await handler(event)`:
   - Éxito → `DELETE WHERE id=?`
   - Fallo → `attempts += 1`; si `>= max_attempts` → `_mark_dead`; si no → backoff + `status='pending'`

### 3.3 Backoff exponencial

Fórmula: `next_retry_at_ms = now_ms + (2 ** attempts) * 1000`

| Fallo # | Espera |
|---|---|
| 1 | 2 s |
| 2 | 4 s |
| 3 | 8 s |
| 4 | 16 s |
| 5 | → `dead` |

El worker es **stateless**; el estado de backoff vive en SQLite — sobrevive a reinicios del proceso.

### 3.4 Retry por-handler, no por-evento

La DLQ guarda `handler_name` obligatoriamente. El worker invoca el handler resuelto directamente
**sin re-publicar al bus**, evitando doble entrega a handlers que ya tuvieron éxito.

---

## 4. Modificaciones a `sky_claw/core/event_bus.py`

### Cambios en `__init__`

```python
def __init__(self, *, max_queue_size: int = 1024, dlq: DLQManager | None = None) -> None:
    ...
    self._dlq = dlq
    self._handler_index: dict[str, Subscriber] = {}
```

### Helper estático

```python
@staticmethod
def _handler_name(cb: Subscriber) -> str:
    mod = getattr(cb, "__module__", "unknown")
    qn  = getattr(cb, "__qualname__", None)
    return f"{mod}.{qn}" if qn else repr(cb)
```

### `subscribe` / `unsubscribe` — índice inverso

```python
self._handler_index[self._handler_name(callback)] = callback   # subscribe
self._handler_index.pop(self._handler_name(callback), None)    # unsubscribe
```

### `_safe_execute` — ruta a DLQ

```python
except Exception as exc:
    logger.error(...)
    if self._dlq is not None:
        try:
            await self._dlq.enqueue(event, callback, exc)
        except Exception:
            logger.critical("dlq enqueue failed — event lost", exc_info=True)
```

### Factory de conveniencia

```python
def create_bus_with_dlq(db_path: Path | None = None) -> CoreEventBus:
    bus = CoreEventBus()
    dlq = DLQManager(
        db_path=db_path or Path.home() / ".sky_claw" / "dlq" / "dlq.db",
        handler_resolver=bus._handler_index.get,
    )
    bus._dlq = dlq
    return bus
```

---

## 5. Archivos

### Nuevos

| Ruta | Responsabilidad |
|---|---|
| `sky_claw/core/dlq_manager.py` | `DLQManager`, `DLQRow`, schema DDL, retry worker |
| `tests/test_dlq_manager.py` | 8 unit tests (schema, enqueue, retry, backoff, poison, stop) |
| `tests/test_event_bus_dlq_integration.py` | Integración bus+DLQ con handler que falla |

### Modificados

| Ruta | Cambio |
|---|---|
| `sky_claw/core/event_bus.py` | `_handler_index`, `_dlq`, lifecycle wiring, `create_bus_with_dlq` |
| `sky_claw/core/__init__.py` | Exportar `DLQManager`, `create_bus_with_dlq` |

### Sin cambios

- `pyproject.toml` — aiosqlite y tenacity ya presentes

---

## 6. Testing

### Matriz de unit tests (`tests/test_dlq_manager.py`)

| # | Test | Verifica |
|---|---|---|
| 1 | `test_schema_bootstraps_on_first_use` | `enqueue()` crea DB + tabla + índice desde cero |
| 2 | `test_enqueue_persists_row` | Fila correcta (campos, attempts=0, status='pending') |
| 3 | `test_retry_worker_reinvokes_only_failed_handler` | Handler exitoso al 2do intento → fila borrada |
| 4 | `test_poisoning_after_max_attempts` | 5 fallos → `status='dead'`, fila persiste |
| 5 | `test_backoff_schedule_is_exponential` | Deltas: 2000, 4000, 8000, 16000 ms |
| 6 | `test_missing_handler_marks_dead` | `handler_resolver` retorna `None` → `dead` |
| 7 | `test_batch_limit_respected` | 100 filas pendientes → 1 tick procesa 50 |
| 8 | `test_stop_cancels_worker_gracefully` | `stop()` sin raise durante `asyncio.sleep` |

### Principios de aislamiento

- `tmp_path` pytest fixture — nunca escribe en `~/.sky_claw/`
- `clock: Callable[[], int]` inyectable en el constructor — controla backoff sin `sleep` real
- Sin `freezegun` — el reloj inyectado es suficiente

### Comandos

```bash
pytest tests/test_dlq_manager.py -v
pytest tests/test_event_bus_dlq_integration.py -v
pytest tests/ -v -k "dlq or event_bus"
ruff check sky_claw/core/dlq_manager.py sky_claw/core/event_bus.py
```

---

## 7. No-goals (MVP)

- Sin DLQ cross-proceso / distribuida
- Sin UI de auditoría (`status='dead'` se inspecta con `sqlite3`)
- Sin re-validación Pydantic en retry
- Sin replay de filas `dead` por API pública
- Sin rotación de DB (se aborda en V5.6)

---

## 8. Riesgos

| Riesgo | Mitigación |
|---|---|
| Dos handlers con mismo `module.qualname` | Log WARN; resolver retorna el último registrado |
| Corrupción DB mid-write | WAL + `synchronous=NORMAL` |
| DLQ crece sin tope | Log CRITICAL si `COUNT(dead) > 10_000` |
| `enqueue()` falla (disco lleno) | Try/except en `_safe_execute`, log CRITICAL, best-effort |
| Race `stop()` / `enqueue()` in-flight | `dlq.stop()` se llama DESPUÉS del drenado del bus |
