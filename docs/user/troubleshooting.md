# Diagnóstico y resolución

> **Audiencia:** usuarios y operadores.
>
> **Estado:** Implementado para diagnóstico; las acciones sobre un rig real
> requieren evidencia local.
>
> **Fuentes canónicas:** `sky_claw/logging_config.py`,
> `sky_claw/app_context.py`, `sky_claw/app/db/` y
> `sky_claw/local/mo2/`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Primero: preservar evidencia

No repetir una operación mutante a ciegas. Guardar:

- `correlation_id`;
- hora y modo de ejecución;
- perfil e instancia MO2;
- `logs/sky_claw.log` y logs específicos;
- estado del journal, backup y artefacto esperado;
- procesos externos todavía activos.

## Síntomas frecuentes

| Síntoma | Evidencia | Acción reversible |
|---|---|---|
| La tool no aparece | Resultado del escaneo y ruta configurada | Corregir la ruta y repetir sólo el preflight |
| Credencial ausente | Error de proveedor o keyring | Guardar el secreto en keyring; no usar TOML plano |
| VFS no autentica | Log del broker/plugin y descriptor de instancia | Detener la ejecución, comprobar MO2 y reinstalar el bridge |
| Perfil incorrecto o drift | Preflight, manifest y perfil activo | No promover; volver al baseline conocido |
| Timeout | Log del runner, `worker_exit` y árbol de procesos | Esperar cierre acotado; no relanzar si quedan procesos |
| Rollback incompleto | Journal y backup preservado | Detener escritores y escalar a recuperación manual |
| SQLite ocupado | Logs de lifecycle y procesos | Cerrar productores normalmente; no borrar WAL/SHM |

## Logs

El primer archivo es `logs/sky_claw.log`. `watcher.log` y
`watcher_security.log` cubren el watcher. Las líneas son JSON rotativo y
contienen `correlation_id`.

## Cuándo detenerse

No borrar bases, WAL, snapshots, backups ni descriptores para “destrabar” una
operación. Si no puede probarse la propiedad del lock, la identidad del perfil
o el resultado del rollback, seguir el
[runbook de recuperación](../operations/recovery.md).
