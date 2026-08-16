# Datos, persistencia y recuperación

> **Estado:** implementado con cobertura desigual por runner.
>
> **Audiencia:** desarrolladores y operadores.
>
> **Fuentes canónicas:** `sky_claw/app/core/db_lifecycle.py`,
> `sky_claw/app/db/` y `sky_claw/local/mo2/profile_sandbox.py`.
>
> **Última verificación:** 2026-08-16 sobre `origin/main` `32abf6f77b758c622dd71f20607589c53effeaf1`.

## Capas

| Componente | Responsabilidad |
|---|---|
| `DatabaseLifecycleManager` | abrir, compartir, checkpoint y cerrar conexiones SQLite |
| `DatabaseAgent` / registry | estado de mods y consultas del dominio |
| `OperationJournal` | operaciones y transacciones con estados terminales |
| `DistributedLockManager` | exclusión cross-process con TTL y heartbeat |
| `FileSnapshotManager` | snapshot y restore de archivos |
| `RollbackManager` | compensación de operaciones registradas |
| `ProfileSandbox` | aislar perfil+overwrite y promover un diff aprobado |

## Estados

El journal separa entradas de operación y transacciones. “Falló el cleanup” no
debe registrarse como éxito sólo porque el ejecutable produjo un artefacto.
Las transacciones huérfanas y checkpoints se coordinan durante startup/shutdown.

## Frontera de acceso SQLite

`DatabaseLifecycleManager.get_connection(path)` devuelve la conexión gestionada,
pero no concede derecho a emitir SQL fuera de un boundary. Toda unidad que use
la conexión compartida debe entrar por `operation(path)` o `transaction(path)`.

`operation()` toma la admisión antes de esperar el lock de escritura del path y
resuelve la conexión dentro de esa frontera. El lock es por path, no global. La
admisión ya tomada se drena durante el shutdown; el trabajo nuevo se rechaza con
`DatabaseLifecycleShuttingDownError` en vez de reabrir una DB cerrada. La
reapertura es explícita mediante `init_all()`.

`transaction()` además hace commit en la salida normal y rollback ante error o
cancelación, preservando la semántica de cancelación y poniendo en cuarentena una
conexión cuyo rollback no puede completarse. Los callers no deben llamar
`aiosqlite.connect()` ni hacer commit/rollback manual sobre una conexión
compartida administrada.

## Sandbox

`ProfileSandbox` conserva baseline, copia mutable y origen real para `profile`
y `overwrite`. Antes de promover verifica drift y symlinks, respalda los
targets afectados y revierte cambios parciales. `SandboxRollbackError` indica
que el árbol real puede requerir restauración manual y preserva el backup.

## Cobertura real

La existencia de estas primitivas no significa que todo runner las use. El
estado por runner vive en `docs/pending_ooda_status.md`; debe verificarse contra
callers actuales antes de afirmar cobertura.

Procedimientos operativos: [recovery.md](../operations/recovery.md).
