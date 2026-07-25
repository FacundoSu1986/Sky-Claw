# Referencia de eventos

> **Audiencia:** desarrolladores y agentes que trazan flujos asíncronos.
>
> **Estado:** Implementado.
>
> **Fuentes canónicas:** `sky_claw/antigravity/core/event_bus.py`,
> `sky_claw/antigravity/gui/gui_event_adapter.py` y publishers del árbol.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Dos buses distintos

| Bus | Envolvente | Suscripción | Dispatch |
|---|---|---|---|
| `CoreEventBus` | `Event(topic, payload, timestamp_ms, source)` | patrón `fnmatch` | async, instanciable, cola acotada |
| GUI `EventBus` | `SkyClawEvent(type, data, timestamp, source)` | `EventType` exacto | singleton, thread de cola hacia el loop |

No publicar un `SkyClawEvent` en `CoreEventBus` ni sustituir un topic de texto
por `EventType`.

## Topics Core comprobados

| Topic o patrón | Emisor/consumidor |
|---|---|
| `system.telemetry.metrics` | `TelemetryDaemon`; GUI y supervisor consumen `system.telemetry.*` |
| `system.modlist.changed` | `WatcherDaemon`; supervisor dispara análisis |
| `system.manual.trigger` | trigger manual del supervisor |
| `ops.hitl.preview` | `ChainPreviewService` |
| `ops.tool.started` | `ProgressMiddleware` |
| `ops.tool.completed` | `ProgressMiddleware` |
| `ops.tool.failed` | `ProgressMiddleware` |

## `EventType` GUI

`MOD_ADDED`, `MOD_REMOVED`, `MOD_UPDATED`, `CONFLICT_DETECTED`,
`CONFLICT_RESOLVED`, `LLM_RESPONSE`, `DOWNLOAD_PROGRESS`,
`AGENT_STATUS_CHANGE`, `CONFIG_CHANGED`, `EVENT_BROADCAST`,
`NAVIGATION_REQUESTED` y `MOD_SELECTED`.

## Lifecycle y fallos

`CoreEventBus.publish()` exige estado RUNNING. En producción,
`create_bus_with_dlq()` conecta una DLQ; backpressure o fallo de handlers se
observa mediante contadores y DLQ. `stop()` coordina publishers admitidos,
dispatches y tareas DLQ.

El bus GUI debe iniciarse dentro de un loop activo. Si no hay loop, no inicia;
si el loop se cierra, descarta callbacks pendientes y registra warning.
