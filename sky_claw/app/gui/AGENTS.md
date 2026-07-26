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

## Verificación segura

Cubrir callbacks, store y controllers con tests; para claims visuales o de
runtime ejecutar una smoke GUI separada.
