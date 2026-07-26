# Datos, persistencia y recuperación

> **Estado:** implementado con cobertura desigual por runner.
>
> **Audiencia:** desarrolladores y operadores.
>
> **Fuentes canónicas:** `sky_claw/app/core/db_lifecycle.py`,
> `sky_claw/app/db/` y `sky_claw/local/mo2/profile_sandbox.py`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

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
