# Datos, persistencia y recuperación

> **Estado:** implementado con cobertura desigual por runner.
>
> **Audiencia:** desarrolladores y operadores.
>
> **Fuentes canónicas:** `sky_claw/app/core/db_lifecycle.py`,
> `sky_claw/app/db/` y `sky_claw/local/mo2/profile_sandbox.py`.
>
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
>
> **Sincronización DB/lifecycle:** 2026-08-16 sobre `main` `7156718`.

## Capas

| Componente | Responsabilidad |
|---|---|
| `DatabaseLifecycleManager` | abrir, compartir, serializar uso por path, checkpoint y cerrar conexiones SQLite |
| `DatabaseAgent` / registry | estado de mods y consultas del dominio |
| `OperationJournal` | operaciones y transacciones con estados terminales |
| `DistributedLockManager` | exclusión cross-process con TTL y heartbeat |
| `FileSnapshotManager` | snapshot y restore de archivos |
| `RollbackManager` | compensación de operaciones registradas |
| `ProfileSandbox` | aislar perfil+overwrite y promover un diff aprobado |

## Boundary de lifecycle SQLite

Para los consumidores cubiertos por `DatabaseLifecycleManager`, obtener una
conexión y tener derecho a emitir SQL ya no son la misma cosa:

- `get_connection(path)` resuelve/devuelve la conexión gestionada, pero no
  mantiene el derecho de uso después de retornar.
- `operation(path)` es el boundary para una unidad SQL no transaccional. Toma
  admisión antes de esperar el lock por path, resuelve la conexión vigente bajo
  ese lock y lo conserva durante toda la unidad.
- `transaction(path)` se apoya en `operation(path)`, conserva el mismo boundary
  durante la transacción completa y coordina commit/rollback.
- El lock se comparte por path resuelto; wrappers distintos sobre la misma DB no
  deben introducir locks privados que compitan con ese ownership.
- Red, filesystem, sleeps y backoff deben quedar fuera del boundary: el lock
  protege la conexión SQLite compartida, no trabajo externo.

El shutdown es fail-closed: `shutdown_all()` deja de admitir trabajo nuevo,
espera el drenaje de las unidades ya admitidas y recién después hace checkpoint
TRUNCATE y cierre. Una operación ya encolada antes del shutdown cuenta como
admitida y debe poder terminar; una unidad nueva posterior recibe
`DatabaseLifecycleShuttingDownError`.

`init_all()` y `shutdown_all()` están serializados por transición. La reapertura
es explícita; pedir una conexión durante shutdown no resucita implícitamente la
base.

### Consumidores gestionados

El contrato M-01/M-01.1 documentado en el runtime cubre `core/database.py`,
`db/async_registry.py`, `db/journal.py`, `security/governance.py`, `db/locks.py`,
`agent/router.py` y `core/dlq_manager.py`. Algunos componentes conservan un modo
standalone sin lifecycle para tests o usos aislados; esa rama no debe confundirse
con el wiring productivo.

### Regla para cambios y agentes

No reintroducir `get_connection() + SQL posterior` en un consumidor lifecycle-
backed: existe una ventana entre resolver la conexión y usarla en la que el
shutdown puede avanzar. Para SQL productivo debe conservarse el boundary durante
toda la unidad con `operation()` o `transaction()` según corresponda.

Si documentación histórica describe el patrón anterior, tratarlo como
`DOCUMENTATION_DRIFT`, no como una instrucción para refactorizar producción.

## Estados

El journal separa entradas de operación y transacciones. “Falló el cleanup” no
debe registrarse como éxito sólo porque el ejecutable produjo un artefacto.
Las transacciones huérfanas y checkpoints se coordinan durante startup/shutdown.

## Sandbox

`ProfileSandbox` conserva baseline, copia mutable y origen real para `profile`
y `overwrite`. Antes de promover verifica drift y symlinks, respalda los
targets afectados y revierte cambios parciales. `SandboxRollbackError` indica
que el árbol real puede requerir restauración manual y preserva el backup.

## Cobertura real

La existencia de estas primitivas no significa que todo runner las use. El
estado por runner vive en `docs/pending_ooda_status.md`; debe verificarse contra
callers actuales antes de afirmar cobertura.

La sincronización del 2026-08-16 verifica el contrato de
`DatabaseLifecycleManager` y su boundary; no implica una reverificación integral
de `ProfileSandbox`, todos los runners o todos los procedimientos de recovery.

Procedimientos operativos: [recovery.md](../operations/recovery.md).
