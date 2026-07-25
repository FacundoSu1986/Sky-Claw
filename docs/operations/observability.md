# Observabilidad

> **Audiencia:** operadores y desarrolladores de guardia.
>
> **Estado:** Implementado; exportación de métricas y traces es opcional.
>
> **Fuentes canónicas:** `sky_claw/logging_config.py`,
> `sky_claw/antigravity/core/metrics_server.py`,
> `sky_claw/antigravity/core/tracing.py` y
> `sky_claw/antigravity/db/journal.py`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Señales

| Señal | Fuente | Uso |
|---|---|---|
| Log principal | `logs/sky_claw.log` | Startup, modos, errores y tools |
| Watcher | `logs/watcher.log` | Actividad del watcher |
| Seguridad watcher | `logs/watcher_security.log` | Eventos de seguridad |
| Journal SQLite | `journal_entries`, `transactions` | Estado durable de operaciones |
| Métricas | servidor Prometheus opcional | Salud y comportamiento agregado |
| Tracing | exporter OTLP opcional | Trazas cuando existe collector |

El logging JSON rota a 10 MB con cinco backups y aplica redacción. El
`correlation_id` es el pivote entre líneas; no asumir que un `trace_id` está
serializado si no aparece en el registro.

## Triage

1. Registrar hora, modo, perfil y `correlation_id`.
2. Buscar el primer error causal, no sólo el mensaje final.
3. Correlacionar con journal y eventos de progreso.
4. Comprobar lock, snapshot y procesos externos.
5. Clasificar la evidencia como lectura, test, CI o rig real.

```powershell
Select-String '"levelname": "ERROR"' logs\sky_claw.log
Select-String '<correlation_id>' logs\sky_claw.log
```

No pegar logs completos en issues públicos sin revisar rutas, nombres de
usuario y datos sensibles.
