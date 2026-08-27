# Telegram

> **Audiencia:** operadores que quieren interacción remota y HITL.
>
> **Estado:** Implementado mediante polling; existe además un gateway Node
> separado para el bridge de frontend.
>
> **Fuentes canónicas:** `sky_claw/app/modes/telegram_mode.py`,
> `sky_claw/app/comms/telegram_polling.py` y
> `sky_claw/app/comms/telegram_gateway_node/`.
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
router, sesión y gateway de red, y mantiene el polling hasta cancelación. El gateway Node es otro borde de comunicaciones; no es una precondición declarada
por `_run_telegram()`.

## Lifecycle visible de aprobaciones HITL

Una solicitud pendiente se muestra con botones inline **Aprobar** y **Denegar**.
Los botones transportan un token efímero, aleatorio y acotado al protocolo de
Telegram; el `request_id` interno completo nunca se embebe en `callback_data`.
Los comandos textuales `/approve` y `/deny` siguen aceptando el `request_id`
literal después de un espacio único como separador canónico, y preservan sus
espacios internos y su Unicode. Si un ID es muy largo, el texto visible muestra
sólo un prefijo bounded y una huella SHA-256; el ID completo permanece intacto
en el guard y el registry.

Cuando `HITLGuard` alcanza una decisión terminal, el mensaje original se actualiza
y deja de tener botones accionables, incluso si la resolución provino de un
comando textual, otra interfaz válida o un timeout:

| Estado | Presentación |
|---|---|
| Pendiente | Prompt HITL con botones **Aprobar** y **Denegar** |
| Aprobada | `✅ Solicitud aprobada`, sin botones |
| Denegada | `❌ Solicitud denegada`, sin botones |
| Expirada | `⌛ Solicitud expirada`, sin botones |
| Cancelada o invalidada | Estado no accionable, sin botones |

La interfaz de Telegram sólo refleja la decisión autoritativa del guard. Un
callback tardío no puede reabrir una solicitud ni cambiar una decisión terminal.

## Verificación segura


1. Probar con un chat de operador controlado.
2. Confirmar que un remitente distinto es rechazado.
3. Solicitar una acción no destructiva.
4. Verificar que una operación HITL no continúa sin aprobación explícita.
5. Detener con `Ctrl+C` y comprobar el cierre del polling.
