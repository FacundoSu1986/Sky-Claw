# AGENTS.md — persistencia

Aplican [`../AGENTS.md`](../AGENTS.md) y las reglas raíz.

## Fuentes obligatorias

`async_registry.py`, `journal.py`, `locks.py`, `snapshot_manager.py` y
`core/db_lifecycle.py`.

## Invariantes

- SQLite/WAL se cierra mediante el lifecycle manager; no borrar `-wal`/`-shm`.
- Journal, lock y snapshot son mecanismos distintos.
- Pérdida de lease impide confirmar la operación.
- Un rollback fallido conserva evidencia y no se reporta como éxito.

## Verificación segura

Usar DB y directorios temporales de tests. Cubrir timeout, cancelación,
repetición y fallo simultáneo de operación y cleanup.
