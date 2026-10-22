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
- El producto (todo lo que no es `research/`; hoy `decode.py`, `espectral.py`, `export.py`, `gates.py`, `integrate.py`, `metrics.py`, `_extra.py`, `bench/` y `experiments/`) **no importa** `research` y carga `numpy` y
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
- `gates.py` (NP-Q0b) es el gate de calidad y es **fail-closed**: sin un registro de umbrales validado contra un
  corpus REAL, todo va a `REVIEW` (`UMBRALES_VIGENTES is None`, congelado por igualdad). `AUTO_ACCEPT` exige
  evidencia calculada (conteos → cota binomial), no un booleano; el corpus sintético no puede validar; la evidencia
  va atada por huella al candidato que midió (si tocás límites, signo o versión, hay que volver a validar); una
  medición fuera de su dominio (`DOMINIO_DE_LAS_METRICAS`) es `REVIEW`; la confianza va en `[0.5, 1)`; el signo
  esperado y `VERSION_DE_METRICAS` viajan en el registro. No hay `REJECT`. Una métrica pública nueva en `metrics.py`
  rompe el ancla hasta que se la clasifica en `METRICAS_GATEADAS` o `METRICAS_NO_GATEADAS` (con motivo); si cambiás
  la definición de una métrica, subí `VERSION_DE_METRICAS`. Solo `gates.py` conoce `UMBRALES_VIGENTES`: los demás
  llaman a `decidir`, no comparan por su cuenta. Publicar el primer registro es una decisión de política del
  operador (tolerancia, confianza, tasa de revisión máxima) y cambia el ancla: no lo hagas de pasada.
- `export.py` (NP-E0a) es el núcleo puro del exportador DDS y **fail-closed**: sin `TEXCONV_PIN_VIGENTE` (versión + SHA-256
  del binario oficial; hoy el `texconv` `may2026`, congelado por igualdad) no se ejecuta `texconv`; cambiarlo es una decisión
  del operador con evidencia nueva, y cambia el ancla. Los mips son promedio de área en float64 redondeado una vez a float32 (nunca POINT ni
  curva sRGB sobre la altura); el validador de contenedor acepta SOLO BC4 UNORM 2-D con payload exacto. El ÚNICO módulo que
  ejecuta `texconv` es `sky_claw/local/tools/texconv_runner.py` (NP-E0b; ancla `tests/test_texconv_invariant.py`), con los
  flags verificados ejecutando (`ARGUMENTOS_BC4`): no inventes flags, verificalos contra el binario real
  (`tests/test_texconv_real.py`, Windows, con `SKYCLAW_TEXCONV_EXE`). Tests en
  `tests/test_native_parallax_export.py` (el helper de DDS es independiente del módulo; no lo reemplaces por código de `export.py`).
- `experiments/convencion.py` (NP-X0a) MIDE la convención del canal G leyendo solo píxeles y no corrige nada: la
  convención la fija la fuente del archivo y la lectura solo puede corroborarla. Es un indicio con abstención
  (`tiene_senal`, `elegida` puede ser `None`): solo 7 de los 15 casos sintéticos son detectables (la detectabilidad es
  la derivada mixta), la inversión global X+Y no se distingue por ninguna métrica y con ruido σ >= 2/255 o cuantización
  de dos canales el margen cae al de los no detectables (ADR 0014, "Actualización de NP-X0a"). No fijes un umbral de
  margen de pasada: se deriva contra el benchmark y se valida con texturas reales.
- `experiments/medicion.py` y `experiments/revision.py` (NP-X0b) miden y simulan: derivar un candidato NO es validarlo.
  La simulación no compara límites por su cuenta, se apoya en los motivos de `decidir_con` (la partición de `Motivo` está
  congelada por igualdad), y solo `gates.py` puede nombrar `ValidacionContraCorpusReal` (ancla por AST). La etiqueta «mala»
  y su tolerancia son declaradas, sin valor por defecto; para estimar sin fuga usá `simular_dejando_un_caso_fuera`. Los
  números salen del corpus sintético y no son la tasa de revisión del producto (que es 100 % sin registro validado). El
  gate no detecta una inversión global X+Y cuando la altura sale de la misma normal; ver ADR 0014, "Actualización de NP-X0b".
- `output.py` (NP-O0a) es el núcleo puro de la salida gestionada: decide, sin tocar el disco, quién es dueño de qué
  (`Manifiesto`), cuándo se saltea un archivo (`clave_de_cache`) y qué hacer con cada ruta (`planificar`). **El manifiesto es
  la única prueba de propiedad**: un archivo que no figura en él, o que figura pero cambió de contenido, no se pisa
  (`PRESERVE_EXISTING`). La tabla estado → acción se enumera entera (15 estados) por igualdad literal en
  `tests/test_native_parallax_output.py`; no agregues una dimensión de estado sin ampliarla. Solo `output.py` puede nombrar
  `NOMBRE_DEL_MOD_DE_SALIDA` y es puro por imports (ambos anclados por AST): el disco, la hora y el entorno los trae el
  llamador. Todo campo nuevo de `Procedencia` o de `EncoderUsado` tiene que entrar a la clave (el test lo enumera por
  introspección), y cambiar el formato de la clave o del manifiesto exige subir `ESQUEMA_DEL_MANIFIESTO`. **Lo que NO hace** y
  debe resolver el escritor (NP-O0b): tomar el lock cross-process (solo protege si TODOS los mutadores participan), volver a
  comprobar la huella `sha256_autorizado` justo antes de tocar un archivo, publicar por etapas y escribir el manifiesto al
  final, y recuperarse de un corte a mitad. Ver ADR 0014, "Actualización de NP-O0a".
  **`reconciliar_tras_corte` (NP-O0b1)** es la mitad pura de «recuperarse de un corte»: dado el manifiesto vigente, el pendiente y lo que el
  escritor vio en disco y en su papelera, decide qué manifiesto queda, qué devolver y qué descartar. Su tabla de 60 estados se enumera
  por igualdad literal en `tests/test_native_parallax_output_recuperacion.py`; no agregues una dimensión sin ampliarla. Parte del
  protocolo del escritor (intención antes de tocar, apartar y verificar, compromiso al final, pendiente borrado último, nunca borrar de la
  papelera lo que no sea una huella vigente): ver ADR 0014, "Actualización de NP-O0b1".
- `output_writer.py` (NP-O0b2) es el **único** módulo que toca el mod de salida y cumple ese protocolo. Todo cambio al mod pasa por una
  `SesionDeSalida` (`abrir_sesion_de_salida`: toma el lock cross-process, recupera y recién ahí deja planificar y aplicar); no hay otra puerta, y
  el ancla `tests/test_native_parallax_output_writer.py` lo enumera por AST: una función nueva que toque el disco, o una que nombre una unidad
  que escribe fuera de la sesión, rompe el test a propósito (decidí si participa del lock). Una ruta cuyo disco cambió entre planificar y aplicar
  no levanta: queda `CONFLICTO` y no se pisa. Los puntos de corte (`_punto_de_corte`) están congelados y el test corta el proceso en cada uno;
  agregar uno sin cubrirlo rompe el ancla. No crea `meta.ini` ni habilita el mod (es de NP-A0). Ver ADR 0014, "Actualización de NP-O0b2".

## Verificación segura

`tests/test_clean_room_invariant.py` y `tests/test_native_parallax_*.py`.
