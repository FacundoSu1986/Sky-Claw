# AGENTS.md — núcleo async

Aplican [`../AGENTS.md`](../AGENTS.md) y las reglas raíz.

## Fuentes obligatorias

`event_bus.py`, `db_lifecycle.py`, `contracts.py`, `schemas.py`, `errors.py` y
sus tests directos.

## Invariantes

- Lifecycle idempotente y cancelación observable.
- Producción conecta `CoreEventBus` con DLQ.
- Schemas y errores se documentan desde símbolos reales.
- El cierre de DB no se declara completo si quedan operaciones admitidas.

## Verificación segura

Ejecutar tests focalizados de event bus, DLQ y DB lifecycle; después los gates
raíz cuando exista un cambio funcional.
