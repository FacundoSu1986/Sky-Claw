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
>
> **Ampliación:** 2026-10-11 (#706) — contrato de flood-control **coordinado** entre
> corrutinas del mismo bot; no implica reverificación integral del resto del
> documento.

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

## Flood-control y envíos concurrentes

Telegram responde **HTTP 429 + `retry_after`** cuando una ráfaga excede sus límites.
El transporte (`sky_claw/app/comms/telegram_sender.py`) reintenta de forma acotada
—3 reintentos además del intento inicial, con espera máxima de 30 s por aviso— y
**coordina esa espera entre todas las corrutinas del mismo bot**:

- un 429 publica un *cooldown* con **reloj monotónico**, acotado al tope de la
  política, y se consulta **antes de cada intento HTTP**, no sólo antes del primer
  mensaje;
- el aviso cubre el chat que lo recibió **y** el alcance global del bot: un 429 no
  informa cuál de los dos límites se excedió, así que se converge al mayor — esperar
  de más cuesta latencia acotada, golpear la API dentro de una ventana anunciada
  cuesta avisos perdidos;
- dos 429 concurrentes convergen en el deadline **más lejano** (un aviso nuevo nunca
  acorta uno vigente), la espera es cancelable y no hay lock sostenido durante una
  petición de red;
- **un 429 nunca cuenta como mensaje entregado**: no ocupa un cupo del rate limit
  local (20 mensajes/minuto por chat) ni produce una identidad de mensaje, y una
  ráfaga que agota los reintentos termina en `TelegramRateLimitError` con el
  `retry_after` real — nunca en un éxito fabricado.

Contrato para quien consuma estas notificaciones (fase T3), con los reintentos ya
agotados: una actualización **no crítica** (progreso, estado de LOD, seguimiento) se
**descarta explícitamente** —su validez caduca dentro de la ventana de flood-control,
así que no se reprograma—; un evento **terminal** (fallo o completitud de un ritual)
se reporta además por una vía que no compita por el mismo chat (estado del pipeline en
la GUI/CLI) y, sólo si el caller tiene cola propia, puede reprogramarse **una** vez de
forma acotada. Está prohibido reintentar sin límite y tratar el 429 como entrega.

## Verificación segura


1. Probar con un chat de operador controlado.
2. Confirmar que un remitente distinto es rechazado.
3. Solicitar una acción no destructiva.
4. Verificar que una operación HITL no continúa sin aprobación explícita.
5. Detener con `Ctrl+C` y comprobar el cierre del polling.
