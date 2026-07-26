# Ciclo de vida de runtime

> **Estado:** implementado.
>
> **Audiencia:** desarrolladores y operadores.
>
> **Fuentes:** `sky_claw/__main__.py` y `sky_claw/app_context.py`.
>
> **Última verificación:** 2026-07-26 sobre una rama basada en
> `origin/main` `6cb4024`.

## Arranque

1. `__main__.main()` configura el pipeline de logging antes de crear el loop.
2. `__main__._parse_args()` resuelve modo y flags.
3. `_main()` deriva instalación del bridge o health-check cuando corresponde.
4. `AppContext.start_full()` serializa la inicialización con su lock.
5. `AsyncExitStack` registra compensaciones a medida que cada recurso arranca.
6. Se construyen red, DB lifecycle, servicios locales, tools y router.
7. El modo elegido toma el control de la interacción.

Existe también `start_minimal()` para contextos reducidos. El helper de módulo
`start_full(args)` construye y devuelve un `AppContext`.

```mermaid
sequenceDiagram
    participant E as __main__
    participant A as AppContext
    participant S as AsyncExitStack
    participant R as Recursos
    E->>A: start_full()
    A->>S: registrar cleanup por recurso
    A->>R: inicializar en orden
    alt fallo parcial
        A->>S: cerrar en orden inverso
    else listo
        A-->>E: contexto iniciado
    end
```

## Cierre

`AppContext.stop()` es idempotente y coordina cleanups del generation stack.
Cancela las tareas en background y espera su drenaje como máximo
`_startup_shutdown_timeout_s`. Si alguna sigue pendiente, ejecuta igualmente
`_close_cleanup_generation()` con un timeout acotado y después propaga el
`TimeoutError` de las tareas; por tanto, el drenaje completo no es una garantía.
El orden importa: el cierre intenta detener trabajo en background y dependientes
antes de cerrar journal, locks, DB y sesiones de red.

Los modos no GUI instalan el handler de excepciones desde `_main()`. El
bootstrap de NiceGUI instala el mismo handler en el loop que administra
NiceGUI/uvicorn. `AppContext._track_task()` consume y registra excepciones de sus
tasks; el handler cubre las tasks huérfanas. Al salir del modo, `main()` cierra
el listener de logging de forma idempotente fuera del loop.

En un cierre automático de GUI, `ExitWatchdog` registra primero
`gui_watchdog_shutdown` y luego solicita `app.shutdown`. Los hooks de NiceGUI,
incluido `AppContext.stop()`, terminan antes del `finally` exterior que drena y
hace flush del listener.

El worker VFS configura su propio listener y archivo por job. El probe hijo no
inicializa logging: su stdout sigue siendo parte del protocolo de attestation.

## Cancelación

La cancelación no equivale a éxito ni a cierre completo. Los componentes que
protegen estado usan cleanup en `finally`, tasks con referencia fuerte o
`shield` cuando deben alcanzar un resultado terminal. El detalle de cada
subsistema vive en [async_concurrency.md](async_concurrency.md).
