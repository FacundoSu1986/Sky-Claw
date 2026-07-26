# GUI y comunicaciones

> **Estado:** implementado con capacidades parciales señaladas en deployment.
>
> **Audiencia:** desarrolladores y operadores.
>
> **Fuentes canónicas:** `sky_claw/app/gui/`,
> `sky_claw/app/comms/` y modos de ejecución.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Superficies

- **NiceGUI:** views, controllers, estado reactivo y bootloader.
- **CLI:** interacción local y modo oneshot.
- **Telegram:** gateway Node, bridge/transporte Python, polling/webhook según modo.
- **Web:** app y daemon para superficies operativas.

## GUI

`gui/_bootloader.py` prepara el modo GUI y sus handlers. Los controllers
traducen acciones a servicios y rituales; `ritual_runner.py` normaliza
resultados para presentación. El `EventBus` de GUI captura el loop activo y no
debe confundirse con `CoreEventBus`.

## Telegram

El gateway Node y el proceso Python usan un contrato WebSocket autenticado.
Los tokens del gateway Node y del daemon/NiceGUI son flujos distintos. La
presencia de Telegram no agrega HITL automáticamente a toda ruta: el handler o
middleware debe pedir la decisión.

## Observabilidad

Los entrypoints establecen `correlation_id` para rastrear una interacción. En
modo GUI también intervienen los logs de uvicorn/NiceGUI. Véase
[observability.md](../operations/observability.md).
