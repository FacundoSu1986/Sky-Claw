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
- El producto (todo lo que no es `research/`; hoy `decode.py`, `espectral.py`, `integrate.py`, `metrics.py`, `_extra.py` y `bench/`) **no importa** `research` y carga `numpy` y
  Pillow de forma perezosa: pertenecen al extra opcional `native-parallax` (lock propio
  `requirements-native-parallax.lock`, ver su cabecera para regenerarlo), no al runtime base. Ancla:
  `tests/test_native_parallax_producto_invariant.py` (ADR 0014, decisiones 2 y 3).
- `bench/sintetico.py` (NP-D0) reimplementa las fórmulas de `research/synthetic_height.py` y
  `research/normal_from_height.py`; `tests/test_native_parallax_bench_sintetico.py` las compara con research
  como oráculo y congela la especificación del corpus. Cambiar una fórmula en un lado sin el otro rompe ese
  test a propósito: decidí cuál manda.

- `integrate.py` (NP-I0) reimplementa el solver de research; `tests/test_native_parallax_integrate.py` lo compara
  con research como oráculo. La convención de frecuencias vive SOLO en `espectral.py` y numpy se importa de forma
  perezosa SOLO por `_extra.py` (y `decode.py`, que además necesita Pillow): ambos puntos están congelados por AST
  en `tests/test_native_parallax_producto_invariant.py`. No agregues otra llamada a `fftfreq` ni otro
  `import numpy` dentro de una función: rompe el ancla a propósito.
- `metrics.py` (NP-Q0a) son métricas que DESCRIBEN, no deciden: no hay umbrales ni gates ahí (se derivan del
  benchmark, ADR 0014 §6) y `rango_dinamico` exige los percentiles. La derivada H → gradientes y la inversa
  normal → gradientes tienen UNA definición (`espectral.gradientes_espectrales`, `integrate.gradientes_de_normal`):
  no las copies, importalas. Un módulo nuevo que importe de `espectral` rompe el ancla hasta que se le escribe su
  receta en `CONSUMIDORES_DE_ESPECTRAL`.

## Verificación segura

`tests/test_clean_room_invariant.py` y `tests/test_native_parallax_*.py`.
