# AGENTS.md — comunicaciones

Aplican [`../AGENTS.md`](../AGENTS.md) y las reglas raíz.

## Fuentes obligatorias

`telegram_mode.py`, `telegram.py`, `telegram_polling.py`,
`frontend_bridge.py`, `ws_daemon.py` y `telegram_gateway_node/`.

## Invariantes

- Autenticar remitente y operador antes de procesar aprobación HITL.
- El modo Telegram Python usa polling; el gateway Node es otra superficie.
- Tokens del gateway y del daemon/NiceGUI no son intercambiables.
- Shutdown cierra polling, sockets y sesiones en orden.
- Errores de transporte no convierten una acción en aprobada.

## Verificación segura

Simular red y tiempo. Probar remitente no autorizado, token ausente,
reconexión, cancelación y cierre.
