# Recuperación

> **Audiencia:** operadores ante startup parcial, cancelación o fallo de una
> operación.
>
> **Estado:** Implementado con límites específicos por runner.
>
> **Fuentes canónicas:** `sky_claw/app_context.py`,
> `sky_claw/app/core/db_lifecycle.py`,
> `sky_claw/app/db/`, `sky_claw/local/mo2/profile_sandbox.py` y
> `sky_claw/local/mo2/vfs_broker.py`.
>
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
>
> **Sincronización DB/lifecycle:** 2026-08-16 sobre `main` `7156718`.

## Principio

Detener productores, preservar evidencia y recuperar desde la última frontera
durable conocida. No borrar archivos de control para forzar un estado limpio.

## Startup o shutdown parcial

`AppContext` registra recursos en `AsyncExitStack`. Ante fallo de startup,
revierte lo ya adquirido. En shutdown, cierra servicios antes de completar el
lifecycle de base de datos. Una cancelación no autoriza a omitir el cierre.

Si el proceso no termina:

1. no iniciar una segunda instancia;
2. revisar logs y handles;
3. identificar broker, worker y nietos;
4. confirmar que la base cerró o reportó cierre incompleto;
5. escalar con la evidencia antes de matar procesos.

Para SQLite gestionado, un cierre solicitado impide nuevas admisiones y espera
el trabajo ya admitido antes del checkpoint/cierre. Un
`DatabaseLifecycleShuttingDownError` durante esta fase indica rechazo deliberado
de trabajo nuevo; no debe resolverse abriendo una conexión paralela ni llamando
`aiosqlite.connect()` por fuera del lifecycle.

Si `shutdown_all()` reporta cierre incompleto, conservar la evidencia y reintentar
el shutdown de forma explícita. `init_all()` no debe usarse para ocultar un cierre
incompleto: el lifecycle rechaza esa reapertura hasta resolver el estado retenido.

## Operación transaccional

Revisar en este orden:

1. journal de operación y transacción;
2. propiedad y lease del lock;
3. snapshot o backup;
4. estado del artefacto;
5. resultado de rollback.

`ProfileSandbox` clona el directorio del perfil y comparte `overwrite`. La
promoción compara el baseline para detectar drift. Si una escritura falla,
restaura desde backup; si el rollback también falla, conserva el backup y
eleva `SandboxRollbackError`. Ese backup no debe eliminarse.

## SQLite/WAL

No borrar `-wal` ni `-shm`. El lifecycle manager coordina checkpoints y cierre;
la eliminación manual puede destruir evidencia o datos no checkpointed.

En consumidores lifecycle-backed, `get_connection(path)` no conserva el derecho
de uso de la conexión después de retornar. Para diagnosticar o corregir código,
una unidad SQL debe permanecer dentro de `operation(path)` o
`transaction(path)`, según corresponda. Sacar el SQL del boundary puede
reintroducir la carrera en la que shutdown cierra la conexión entre resolución y
uso.

Un `-wal`/`-shm` presente después del cierre no prueba por sí solo corrupción: si
el checkpoint TRUNCATE completó y existe otro owner abierto sobre el mismo
archivo, SQLite puede conservar los sidecars hasta que cierre la última
conexión. Usar los logs/resultados del checkpoint y ownership real antes de
clasificar el incidente.

## Cierre de incidente

Una recuperación termina sólo cuando el perfil, el artefacto, el journal, el
lock y el árbol de procesos son coherentes. Documentar cualquier punto no
verificado.

La sincronización DB/lifecycle de 2026-08-16 no reverifica integralmente los
procedimientos de `ProfileSandbox`, VFS ni todos los runners.
