# Observabilidad

> **Audiencia:** operadores y desarrolladores de guardia.
>
> **Estado:** Implementado; exportación de métricas y traces es opcional.
>
> **Fuentes canónicas:** `sky_claw/logging_config.py`,
> `sky_claw/app/core/metrics_server.py`,
> `sky_claw/app/core/tracing.py` y
> `sky_claw/app/db/journal.py`.
>
> **Última verificación:** 2026-07-26 sobre una rama basada en
> `origin/main` `6cb4024`.

## Señales

| Señal | Fuente | Uso |
|---|---|---|
| Log principal | `~/.sky_claw/logs/sky_claw.log` | Startup, modos, errores y tools |
| Crashes | `~/.sky_claw/logs/crash.log` | Todos los eventos `ERROR+` |
| Watcher | `~/.sky_claw/logs/watcher.log` | Actividad del watcher |
| Seguridad watcher | `~/.sky_claw/logs/watcher_security.log` | Eventos de seguridad |
| Workers VFS | `~/.sky_claw/logs/workers/vfs-worker-<job_id>.log` | Evidencia por job |
| Journal SQLite | `journal_entries`, `transactions` | Estado durable de operaciones |
| Métricas | servidor Prometheus opcional | Salud y comportamiento agregado |
| Tracing | exporter OTLP opcional | Trazas cuando existe collector |

El logging JSON rota a 10 MB con cinco backups y aplica redacción antes de
encolar. El productor usa una cola no bloqueante; el listener dedicado concentra
todo I/O. Cada evento incluye `correlation_id`, `trace_id`, PID, hilo, rol del
proceso y task. Permisos, `ENOSPC` o fallos de rotación degradan el logging sin
derribar la aplicación.

En Windows, un `WinError 32/5` durante el renombrado de rotación aplaza el
rollover sin descartar el evento: el listener sigue escribiendo en el archivo
base y vuelve a intentar más tarde. En GUI sin consola, `stdout` y `stderr`
usan adaptadores `TextIOBase` del mismo pipeline.

Los fallos non-zero de herramientas conservan hasta 64 KiB de `stderr`. Cuando
se supera el límite, el registro incluye tail, tamaño original, SHA-256 y la
bandera `stderr_truncated`.

El watchdog de la GUI emite `gui_watchdog_shutdown` antes de
`app.shutdown`. Cuando finalizan los hooks de NiceGUI y `AppContext.stop()`, el
`finally` exterior drena la cola, hace flush y cierra los handlers.

## Triage

1. Registrar hora, modo, perfil y `correlation_id`.
2. Buscar el primer error causal, no sólo el mensaje final.
3. Correlacionar con journal y eventos de progreso.
4. Comprobar lock, snapshot y procesos externos.
5. Clasificar la evidencia como lectura, test, CI o rig real.

```powershell
Select-String '"level": "ERROR"' "$HOME\.sky_claw\logs\crash.log"
Select-String '<correlation_id>' "$HOME\.sky_claw\logs\sky_claw.log"
```

No pegar logs completos en issues públicos sin revisar rutas, nombres de
usuario y datos sensibles.
