# AGENTS.md — generador nativo de parallax (clean-room)

Aplican las reglas raíz de [`../../../AGENTS.md`](../../../AGENTS.md) y, sobre todo, la política
[`CLEAN_ROOM.md`](../../../CLEAN_ROOM.md)
([ADR 0013](../../../docs/adr/0013-clean-room-native-parallax.md)).

## Fuentes obligatorias

La política clean-room, `docs/design/research/native-parallax/` y
`docs/design/research/2026-09-21-native-parallax-battle/`.

## Invariantes

- **No leas** las secciones en cuarentena: `docs/design/plans/2026-08-19-pre-lod-material-pipeline-v3.md`
  §2.2 y §2.4, y `docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md` §6.2. Contienen
  material derivado de herramientas cerradas y no son fuente de este paquete.
- Si necesitás un contrato de integración (entrypoint, markers, orden de stages, permisos), usá
  `sky_claw/local/tools/parallaxr_assisted.py`, `sky_claw/local/tools/material_contract.py` y el
  registro B1 (`docs/audits/2026-10-07_b1_permisos_r_suite.md`).
- Cada heurística, umbral o exclusión cita fuente pública o experimento propio.
- Comparar contra la salida de una herramienta cerrada es **evaluación** y nunca retroalimenta el
  diseño; usarla como etiqueta de entrenamiento o para ajustar umbrales está prohibido.
- Hoy ningún camino de producción importa `native_parallax.research`.

## Verificación segura

`tests/test_clean_room_invariant.py` y `tests/test_native_parallax_*.py`.
