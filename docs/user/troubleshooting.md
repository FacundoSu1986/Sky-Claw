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
> **Última verificación:** 2026-08-09 sobre `origin/main` `67eaeec5` más este cambio.

## Primero: preservar evidencia

No repetir una operación mutante a ciegas. Guardar:

- `correlation_id`;
- hora y modo de ejecución;
- perfil e instancia MO2;
- `~/.sky_claw/logs/crash.log`, `sky_claw.log` y logs específicos;
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

## Community Shaders

En la tabla de esta sección, los cuatro primeros fallos abortan **antes** de descargar: no
hay archivos parciales que limpiar. Los tres restantes ocurren durante o después de la
descarga. Un rechazo por tamaño o integridad corta antes de extraer y sólo descarta el
archivo de staging. En cambio, un fallo de extracción o verificación **sobre un mod que ya
existía** escribe en ese directorio y no lo limpia: hay que revisarlo. Un fallo al guardar
el registro conserva los mods ya instalados, y lo avisan las dos superficies —la GUI como
advertencia, el agente devolviendo la operación como parcial—. Ninguno de estos impide
usar Community Shaders, porque la activación en MO2 es manual de todos modos. El contexto
completo está en [Community Shaders](community_shaders.md).

| Síntoma | Evidencia | Acción reversible |
|---|---|---|
| Versión de Skyrim no soportada | Versión y edición informadas en el mensaje del error | Comprobar la versión real del ejecutable; no forzar la instalación sobre una versión intermedia |
| SKSE ausente | Raíz del juego sin loader de SKSE | Instalar SKSE primero desde su propia tarjeta del Panel y repetir el escaneo |
| ENB detectado | `enbseries.ini` o el directorio `enbseries/` en la raíz del juego | Decidir entre ENB y Community Shaders; Sky-Claw no desinstala ENB |
| Preloader de Engine Fixes ausente | Raíz del juego sin el DLL del preloader | Instalar ese archivo aparte en la raíz del juego y repetir |
| Mods instalados con aviso de persistencia | Estado administrado en `~/.sky_claw/config.toml` y el aviso de la operación | Los mods son utilizables: activarlos en MO2. No editar la clave a mano ni borrar los mods; para regenerar el registro, ver la fila siguiente |
| Falta `community_shaders_mods` con los mods ya instalados | La clave ausente en `~/.sky_claw/config.toml` y la tarjeta del Panel en `Instalado` | La tarjeta **no ofrece reinstalar** una vez detectado el mod. Pedirle al agente que ejecute la instalación de `community_shaders`: es idempotente y no vuelve a descargar. Si tampoco puede guardar, lo informa como operación parcial en vez de decir que salió bien: ahí el problema es la configuración, no la instalación. Mientras tanto no bloquea nada |
| Descarga rechazada por integridad o tamaño | Mensaje del descargador y `sky_claw.log` | No repetir a ciegas: un desajuste de hash o un techo superado no se arregla reintentando. Preservar el log y verificar el origen |

Si el escaneo sigue mostrando la tarjeta como no instalada después de una instalación
correcta, comprobar que el mod conserve **tanto** su DLL como su directorio de shaders:
faltando uno, Sky-Claw no lo cuenta como instalado a propósito.

## Logs

El primer archivo ante un fallo es `~/.sky_claw/logs/crash.log`; para el
contexto completo usar `sky_claw.log`. `watcher.log`, `watcher_security.log` y
`workers/vfs-worker-<job_id>.log` cubren las superficies especializadas. Las
líneas son JSON rotativo e incluyen `correlation_id` y `trace_id`.

Si no aparece ningún archivo, revisar permisos y espacio libre. El runtime debe
continuar aunque el logging quede degradado; no buscar un
`sky_claw_startup.log`, porque la GUI empaquetada enruta sus streams ausentes al
pipeline estructurado.

## Cuándo detenerse

No borrar bases, WAL, snapshots, backups ni descriptores para “destrabar” una
operación. Si no puede probarse la propiedad del lock, la identidad del perfil
o el resultado del rollback, seguir el
[runbook de recuperación](../operations/recovery.md).
