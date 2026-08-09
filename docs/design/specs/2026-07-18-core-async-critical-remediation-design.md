# Diseño de remediación crítica del núcleo asíncrono

**Fecha:** 2026-07-18

**Baseline:** `origin/main@e43f617`

**Rama:** `codex/fix-core-async-critical`

**Alcance:** aislamiento transaccional SQLite y lifecycle de `CoreEventBus`.

## Objetivo

Eliminar dos fallos reproducidos del núcleo asíncrono sin cambiar APIs públicas ni
introducir dependencias:

1. Corrutinas distintas mezclan transacciones en conexiones `aiosqlite`
   compartidas; un rollback o commit puede afectar la escritura de otra task.
2. `CoreEventBus.stop()` puede abandonar eventos y el sentinel, y un reinicio
   posterior acepta publicaciones sin un dispatcher vivo.

La entrega será un PR focalizado. Quedan fuera los cambios de contratos Pydantic,
TTL de paths, subprocess trees, timeouts genéricos de handlers y refactors amplios
del orquestador.

## Evidencia OODA

- La rama de origen estaba 48 commits detrás de `origin/main`; se creó un worktree
  aislado desde `e43f617` para preservar el commit local de grass.
- Los archivos de `sky_claw/antigravity/core/` relevantes no cambiaron entre el
  snapshot auditado y `origin/main`.
- Línea base focalizada: 80 tests pasan. Pytest agrupó nueve warnings de workers
  `aiosqlite` notificando a loops cerrados y emitió otro warning equivalente al
  terminar; permanecen como deuda preexistente y no se confunden con el resultado
  de esta remediación.
- PoCs con SQLite real demostraron rollback cruzado y persistencia de una escritura
  cancelada mediante un commit ajeno.
- PoCs del bus demostraron cola/sentinel abandonados, dispatcher terminado con
  `_running=True` y publishers bloqueados durante shutdown.

## Evaluación de `AuditZAI.txt`

La auditoría ZAI aporta valor parcial:

- **Incorporado:** confirma que `DatabaseAgent` y `DLQManager` omiten el write lock
  compartido; añade la ordenación concreta donde `stop()` cancela
  `_safe_execute` mientras persiste un fallo en DLQ.
- **Válido, pero fuera de este PR:** handler DLQ sin límite operativo, handler
  desaparecido sin techo y carrera `init_all()`/`get_connection()`. Requieren una
  política de leases, timeout y compatibilidad propia.
- **Fix rechazado:** `wait_for(shield(handler))` deja el handler vivo después del
  timeout y permite reintentar la misma fila en paralelo. No se introducirá.
- **Mecanismo refutado:** dos UPSERT sobre la misma conexión no producen el supuesto
  deadlock de row lock; SQLite no usa locks de fila y la PoC completó en 0,015 s.
- **Mecanismo refutado:** `wal_checkpoint(TRUNCATE)` respetó el busy timeout y
  retornó `busy=1` en 0,594 s ante un reader activo; no se justifican threads daemon
  en `atexit` dentro de este PR.
- **Fix rechazado:** `process.stdout`/`stderr` son `StreamReader`; llamar `close()`
  no implementa el cleanup propuesto. El problema real de árbol/cancelación queda
  para la remediación de subprocessos.
- **Fix rechazado:** devolver `BaseModel` desde decoradores rompería el contrato
  existente y su script de verificación, que esperan `dict`. Los decoradores no
  tienen callers productivos actuales.
- **No aplicable:** el proyecto declara Python >=3.11; la advertencia sobre
  `asyncio.Lock()` en Python <3.10 no pertenece a la matriz soportada.

## Arquitectura SQLite

`DatabaseLifecycleManager` será el único dueño de la frontera transaccional para
conexiones compartidas:

```text
DatabaseAgent --\
                +--> transaction(db_path) --> write lock por path --> aiosqlite
DLQManager -----/
```

Se añadirá un context manager asíncrono `transaction(db_path)` que:

1. Obtiene la conexión administrada.
2. Adquiere `get_write_lock(db_path)` antes del primer DML.
3. Entrega la conexión al caller.
4. En salida normal ejecuta y observa `commit()` antes de liberar el lock.
5. Ante `BaseException`, incluida `CancelledError`, ejecuta y observa rollback
   protegido antes de liberar el lock y vuelve a lanzar la excepción original.

La protección no consistirá en un `shield()` huérfano: commit y rollback tendrán
una task explícita cuyo desenlace siempre se observará. La semántica del punto de
compromiso será:

- cancelación antes de comenzar commit: rollback;
- cancelación recibida mientras commit está en curso: observar commit hasta su
  desenlace y después propagar cancelación;
- nunca liberar el write lock mientras haya una operación SQLite encolada sin
  observar.

Todos los escritores de `DatabaseAgent` migrarán al context manager. En
`DLQManager` se usará para schema, enqueue, recovery, claim y transición final
cuando exista lifecycle compartido. El fallback de conexión por operación
mantendrá su comportamiento, porque ya posee aislamiento por conexión.

## Arquitectura EventBus

El bus conservará su cola y modelo fire-and-forget, pero tendrá una máquina de
estado mínima:

```text
STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED
```

- Un `asyncio.Lock` serializará `start()` y `stop()` completos.
- `_dispatch_loop` usará `while True`; el sentinel será su única salida normal.
- `stop()` cambiará a `STOPPING` antes de aceptar nuevas publicaciones.
- Publishers que ya pasaron la validación conservarán su lugar en la cola. El
  sentinel se insertará detrás de ellos, por lo que el dispatcher liberará su
  backpressure antes de terminar.
- Cada `queue.get()` tendrá su `task_done()` en `finally`.
- Después del sentinel se observarán las tasks de subscribers y enqueues DLQ.
- Si shutdown cancela un subscriber, `_safe_execute` persistirá el evento en DLQ
  mediante cleanup protegido antes de propagar `CancelledError`; sin DLQ se
  incrementará `events_lost`.
- Si `start()` falla después de iniciar parcialmente la DLQ, limpiará los recursos
  creados y restaurará `STOPPED`.
- No se añadirá un timeout arbitrario de shutdown: ocultaría handlers vivos y
  convertiría una espera explícita en pérdida silenciosa.

Las propiedades existentes `_running` y `_dispatch_task` se conservarán por
compatibilidad interna, derivadas del estado y limpiadas de forma determinista.

## Estrategia TDD

### SQLite

1. Un fallo de commit de A no puede revertir la escritura exitosa de B.
2. Cancelar A después de encolar su DML completa rollback antes de admitir B.
3. Dos `DLQManager.enqueue()` concurrentes preservan B cuando A se cancela.
4. Cancelar durante commit observa el commit y no libera el lock prematuramente.
5. Los métodos de lectura siguen ejecutándose concurrentemente y no toman el
   write lock.

### EventBus

1. `start -> publish -> stop` sin yield procesa el evento y deja cola vacía.
2. `start -> stop -> start` despacha un evento nuevo.
3. Una cola llena libera publishers aceptados antes del sentinel.
4. Cancelar un subscriber durante shutdown persiste exactamente una fila DLQ.
5. Cancelar mientras `_safe_execute` ya ejecuta `DLQManager.enqueue` no pierde la
   fila ni cierra la DB antes del commit.
6. `start()` y `stop()` concurrentes no dejan dispatchers ni workers DLQ huérfanos.
7. Un fallo durante `DLQManager.start()` deja el bus en `STOPPED` y permite un
   reintento posterior.

Cada test debe observar RED por el mecanismo esperado antes de modificar código de
producción. Los tests usarán SQLite real y primitivas de coordinación; mocks sólo
se usarán en fronteras de fallo que no puedan provocarse de forma segura.

## Criterios de aceptación

- Todas las regresiones nuevas pasan después de haber demostrado RED.
- Pasan las suites de Database, lifecycle, DLQ, EventBus y persistencia.
- `ruff check sky_claw/ tests/` pasa.
- `ruff format --check sky_claw/ tests/` pasa.
- `mypy sky_claw/` pasa.
- La suite completa de `pytest` pasa o cualquier fallo preexistente queda separado
  con evidencia de baseline.
- No quedan warnings nuevos de tasks, Futures o coroutines no observadas en las
  pruebas añadidas.
- El diff contiene únicamente los archivos de núcleo, tests y documentación de
  esta remediación.

## Rollback e integración

El trabajo se dividirá en commits revisables: especificación, SQLite RED/GREEN y
EventBus RED/GREEN. No se reescribirá la historia de la rama sin autorización. El
PR se abrirá contra `main` sólo después de comparar el diff con `origin/main` y
ejecutar todos los gates.
