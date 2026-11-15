# AGENTS.md — GUI NiceGUI

Aplican [`../AGENTS.md`](../AGENTS.md) y las reglas raíz.

## Fuentes obligatorias

`_bootloader.py`, `sky_claw_gui.py`, `controllers/`, `state/`,
`gui_event_adapter.py` y
[`../../../docs/architecture/ui_comms.md`](../../../docs/architecture/ui_comms.md).

## Invariantes

- Controllers coordinan; views renderizan; estado reactivo mantiene la vista.
- El bus GUI sólo se inicia con un event loop activo.
- `ritual_runner.py` normaliza resultados antes de presentarlos.
- La UI no convierte una operación parcial en éxito visual.
- HITL y autoaprobación fallan cerrados en estados ambiguos.
- **El progreso de la etapa 9 y la cuarentena del broker se muestran con la MISMA fuente y los MISMOS hechos que el agente** (P-3c-2):
  `controllers/etapa9_estado.leer_estado_de_la_etapa9` lee `servicio.progreso_actual` y `AppContext.estado_de_cuarentena_vfs`, y los hechos del
  progreso salen de `dyndolod_progreso.progreso_en_hechos`, nunca de una copia de la GUI. Son hechos, no un veredicto: ningún texto dice «colgado».
  Solo lectura; una falla de lectura se loguea y se omite. Ancla: `tests/test_gui_estado_de_la_etapa9.py`.
- **El modo de interacción de la etapa 9 se elige por conexión y se lee SOLO en el click handler** (H-12): `modo_de_interaccion_del_cliente()`
  (``app.storage.client``) se llama en `sky_claw_gui.py` al armar el payload de `on_ritual_run` —nunca dentro de `run_ritual`, que corre sin
  contexto de cliente—, y `payload_de_generar` lo vuelve un `interaction_mode` explícito. Solo se acepta el `str` exacto del contrato: lo desconocido
  es `asistido`, nunca `desatendido`. «Continuar» hereda el modo del resultado (`resume_action_from_result`). El desatendido aprueba por el canal
  remoto, no por el modal de esta ventana. Ancla: `tests/test_gui_modo_de_interaccion.py`.

## Verificación segura

Cubrir callbacks, store y controllers con tests; para claims visuales o de
runtime ejecutar una smoke GUI separada.
