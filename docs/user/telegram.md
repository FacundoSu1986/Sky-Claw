# Telegram

> **Audiencia:** operadores que quieren interacción remota y HITL.
>
> **Estado:** Implementado mediante polling; existe además un gateway Node
> separado para el bridge de frontend.
>
> **Fuentes canónicas:** `sky_claw/antigravity/modes/telegram_mode.py`,
> `sky_claw/antigravity/comms/telegram_polling.py` y
> `sky_claw/antigravity/comms/telegram_gateway_node/`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Configuración

Guardar `telegram_bot_token` en el keyring del sistema y
`telegram_chat_id` en configuración. El modo recibe también
`--operator-chat-id`; sin un operador autorizado, las aprobaciones se rechazan
en forma cerrada.

```powershell
python -m sky_claw --mode telegram --operator-chat-id 123456789
```

No copiar tokens en la línea de comandos, commits o logs.

## Ruta vigente del modo

`_run_telegram()` construye `TelegramWebhook` y `TelegramPolling`, valida
router, sesión y gateway de red, y mantiene el polling hasta cancelación. El
gateway Node es otro borde de comunicaciones; no es una precondición declarada
por `_run_telegram()`.

## Verificación segura

1. Probar con un chat de operador controlado.
2. Confirmar que un remitente distinto es rechazado.
3. Solicitar una acción no destructiva.
4. Verificar que una operación HITL no continúa sin aprobación explícita.
5. Detener con `Ctrl+C` y comprobar el cierre del polling.
