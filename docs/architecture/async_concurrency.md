# Concurrencia, cancelación y cierre

> **Estado:** implementado por subsistema; no existe una garantía global de
> “toda I/O es async”.
>
> **Audiencia:** desarrolladores y agentes.
>
> **Fuentes canónicas:** `sky_claw/app_context.py`,
> `sky_claw/app/core/`, `sky_claw/app/orchestrator/` y runners.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Reglas

- No bloquear el event loop con I/O de disco, red o procesos.
- Derivar APIs síncronas inevitables mediante `asyncio.to_thread`.
- Conservar referencias fuertes a tasks de cleanup.
- Propagar `CancelledError` después de dejar estado terminal.
- Matar y recolectar procesos; `kill()` sin `wait()` no completa el cleanup.
- Cerrar productores antes que sus dependencias persistentes.

## Primitivas principales

| Área | Primitiva |
|---|---|
| Startup/stop | `asyncio.Lock`, `AsyncExitStack` |
| Daemons del supervisor | `TaskGroup` y cierre colectivo fail-fast |
| DB | lifecycle compartido y conexiones coordinadas |
| Mutaciones | locks distribuidos con heartbeat |
| Promote de sandbox | tarea shieldeada y desenlace terminal |
| EventBus | worker, backpressure y DLQ |
| Subprocess | timeout, tree-kill y reap según runner |

## Dos loops visibles

El proceso tiene un loop `asyncio`, pero NiceGUI/uvicorn controla el loop del
modo GUI. `gui_event_adapter.EventBus.start()` debe ejecutarse dentro del loop
activo para capturarlo. `CoreEventBus` tiene su propio lifecycle explícito.

## Qué verificar al cambiar async

1. happy path;
2. error antes y después de adquirir recursos;
3. timeout;
4. cancelación durante I/O y durante cleanup;
5. cierre repetido;
6. dos fallos simultáneos;
7. ausencia de handles, tasks o procesos huérfanos.
