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

## Flood-control coordinado y política de HTTP 429 (#706)

El cliente `TelegramSender` coordina las respuestas HTTP 429 a través de una
autoridad de cooldown compartida en memoria (`CooldownCompartido`):

1. **Ámbito por chat:** Las restricciones temporales se aíslan por `chat_id`. Una
   respuesta 429 recibida en el chat `123` frena a todas las corrutinas que envían
   a `123`, pero no bloquea los envíos dirigidos a otros chats (`456`).
2. **Ámbito a nivel bot:** Endpoints que no operan sobre un chat específico (como
   `answer_callback_query`) consultan y publican cooldown a nivel global (`None`),
   restringiendo preventivamente todo el egress del bot durante la ventana informada.
3. **Comprobación antes de cada intento:** La autoridad se consulta inmediatamente
   antes de cada llamada HTTP a través de `NetworkGateway`, tanto en el primer intento
   como en cada reintento subsiguiente de `sendMessage`, `sendDocument`,
   `editMessageText` y `answerCallbackQuery`.
4. **Extensión monotónica:** El deadline compartido se actualiza calculando
   `max(actual, monotonic_now + retry_after)`, impidiendo que respuestas tardías
   con un `retry_after` menor reduzcan una restricción vigente mayor.
5. **Acotación finita:** Cada `retry_after` individual se acota a 30.0 segundos.
   Además, el tiempo total acumulado de espera está presupuestado (90.0s para
   mensajes de texto y 330.0s para subida de documentos vía `MAX_UPLOAD_TOTAL_SECONDS`).
   Si el cooldown supera el presupuesto, la operación falla de inmediato sin golpear
   la API.
6. **Contrato del consumidor T3 (notificador de progreso / estado):**
   - **Avisos críticos** (fallo de ritual, estado terminal, solicitud HITL):
     NUNCA asumir entrega exitosa. Ante `TelegramRateLimitError`, registrar la falla
     y reportar por un canal alternativo observable (GUI/CLI/log de auditoría).
   - **Actualizaciones no críticas** (progreso en tiempo real, `/lod_status`):
     Descartar la actualización obsoleta; no reprogramar ni encolar indefinidamente.

## Verificación segura

1. Probar con un chat de operador controlado.
2. Confirmar que un remitente distinto es rechazado.
3. Solicitar una acción no destructiva.
4. Verificar que una operación HITL no continúa sin aprobación explícita.
5. Detener con `Ctrl+C` y comprobar el cierre del polling.
