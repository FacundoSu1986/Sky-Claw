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

1. **Política conservadora ante 429 ambiguos:** La Bot API de Telegram no distingue
   en `ResponseParameters` si el 429 se debe al límite del chat o al límite global
   de broadcast del bot (~30 msgs/s). Para evitar ráfagas prohibidas, un 429 recibido
   en `sendMessage`, `sendDocument` o `editMessageText` publica preventivamente una
   restricción global que frena a todas las corrutinas del bot durante la ventana informada.
   La autoridad conserva soporte para aislar restricciones exclusivamente a un chat
   cuando se indique `ambito="chat"`.
2. **Retención íntegra del deadline de Telegram:** La autoridad retiene el valor completo
   anunciado por el servidor en `parameters.retry_after` (incluso si supera 30.0s, hasta
   la cota de sanidad de 24 horas). Ninguna corrutina subsiguiente intentará alcanzar la
   API antes de que expire la restricción real del servidor.
3. **Presupuesto compartido en mensajes divididos:** Para mensajes de texto largos divididos
   en múltiples fragmentos (`_split_message`), `send()` define un único presupuesto monotónico
   de 90.0 segundos para la operación total. Los fragmentos subsiguientes consumen el
   presupuesto remanente, impidiendo que cada fragmento reinicie un plazo de 90 segundos.
4. **Clasificación precisa de timeouts en documentos:** Cuando el tiempo restante de la
   cota de subida (:data:`MAX_UPLOAD_TOTAL_SECONDS`, 330.0s) es inferior al cooldown activo,
   `send_document` clasifica el fallo como :class:`TelegramUploadTimeoutError`, garantizando
   que el llamador reciba consistentemente la señal de tiempo de subida agotado.
5. **Reevaluación continua y sin falsas salidas:** El bucle de espera en `CooldownCompartido`
   reevalúa directamente el deadline efectivo tras cada suspensión, evitando condiciones
   de carrera con la purga amortizada y extensiones concurrentes.
6. **Acotación por defecto en vías HITL:** `editMessageText` y `answerCallbackQuery`
   aplican por defecto el presupuesto finito de 90.0 segundos, evitando bloqueos indefinidos.
7. **Contrato del consumidor T3 (notificador de progreso / estado):**
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
