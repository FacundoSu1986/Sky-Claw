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
  **El producto no integra con la uniforme** (Frankot–Chellappa, la del oráculo): usa `PONDERACION_DEL_PRODUCTO`
  (`Ponderacion.NZ`, mínimos cuadrados ponderados por `nz`, relleno suave donde `nz` cae bajo el piso). Con la uniforme, 7
  píxeles con `nz < 0` de un `_n` BC7 real (wrcitywall01) bastaban para borrar las piedras. Todo llamador de producto de
  `integrar` pasa `ponderacion=PONDERACION_DEL_PRODUCTO` (hoy el lote y la medición: lo que se publica y lo que mide el gate
  salen del MISMO integrador); el ancla por AST y la enumeración de los formatos de entrada aceptados están en
  `tests/test_native_parallax_normales_fuera_de_dominio.py`. Ver ADR 0014, "Actualización: integración ponderada".
- `metrics.py` (NP-Q0a) son métricas que DESCRIBEN, no deciden: no hay umbrales ni gates ahí (se derivan del
  benchmark, ADR 0014 §6) y `rango_dinamico` exige los percentiles. La derivada H → gradientes y la inversa
  normal → gradientes tienen UNA definición (`espectral.gradientes_espectrales`, `integrate.gradientes_de_normal`):
  no las copies, importalas. Un módulo nuevo que importe de `espectral` rompe el ancla hasta que se le escribe su
  receta en `CONSUMIDORES_DE_ESPECTRAL`. `amplitud` (NP-V0) declara en el informe del lote los percentiles 2/98 y la
  desviación de la altura que se exporta (`lote.PERCENTILES_DE_LA_AMPLITUD`): describe, sin umbral ni objetivo, y no
  cambia el default `min_max` (ese default se decide con más de una textura juzgada en el juego).
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
  escritor vio en disco y en su papelera, decide qué manifiesto queda, qué devolver y qué descartar. Su tabla de 75 estados se enumera
  por igualdad literal en `tests/test_native_parallax_output_recuperacion.py`; no agregues una dimensión sin ampliarla. Parte del
  protocolo del escritor (intención antes de tocar, apartar y verificar, compromiso al final, pendiente borrado último, nunca borrar de la
  papelera lo que no sea una huella vigente y volver a calcularla justo antes de borrar, devolver a su ruta vacía lo apartado aunque no
  sea nuestro, y rechazar antes de tocar nada el lote cuya `cota_del_manifiesto_tras_un_corte` no quepa): ver ADR 0014,
  "Actualización de NP-O0b1". La propiedad de la papelera es una sola para el camino sin corte y para la recuperación: lo apartado que
  no es lo autorizado vuelve a su ruta si está vacía y no se borra nunca; arreglar uno sin el otro es el defecto del hermano.
- `output_writer.py` (NP-O0b2) es el **único** módulo que toca el mod de salida y cumple ese protocolo. Todo cambio al mod pasa por una
  `SesionDeSalida` (`abrir_sesion_de_salida`: toma el lock cross-process, recupera y recién ahí deja planificar y aplicar); no hay otra puerta, y
  el ancla `tests/test_native_parallax_output_writer.py` lo enumera por AST: una función nueva que toque el disco, o una que nombre una unidad
  que escribe fuera de la sesión, rompe el test a propósito (decidí si participa del lock). Una ruta cuyo disco cambió entre planificar y aplicar
  no levanta: queda `CONFLICTO` y no se pisa. Los puntos de corte (`_punto_de_corte`) están congelados y el test corta el proceso en cada uno;
  agregar uno sin cubrirlo rompe el ancla. Cuatro propiedades más, de la revisión de #782 y con su ancla en el mismo archivo: toda carpeta que
  crea dentro del mod, de salida o de control, pasa por `_crear_carpeta_contenida` (contención antes y después; al abrir se rechaza una
  `staging/` o `trash/` que sea un enlace o un junction); todo borrado de la papelera rehace la huella en ese instante
  (`_borrar_si_es_el_autorizado`) y no poder borrar no aborta el lote; todo lo que corre en un hilo pasa por `_en_hilo`, que no suelta el lock
  hasta que el hilo termina aunque cancelen la tarea; y toda ortografía de la misma carpeta física compite por un solo lock (`realpath`). Un
  lote interrumpido después de escribir su intención inutiliza la sesión hasta reabrirla. Lo que queda en la papelera no se borra solo y se
  avisa en toda apertura (`_avisar_de_sobrantes`); el tope de 1 GiB por archivo es uno solo (`output.TAMANO_MAXIMO_DEL_ARCHIVO`). No crea
  `meta.ini` ni habilita el mod (es de NP-A0).
  Ver ADR 0014, "Actualización de NP-O0b2".
- `lote.py` y `cli.py` (NP-A0a) arman la tubería completa sobre las piezas de arriba (leer → integrar → medir → gate → exportar → escritor) y NO
  agregan matemática: componen. Cuatro propiedades, con su ancla en `tests/test_native_parallax_lote.py` y `tests/test_native_parallax_cli.py`:
  el lote es de DOS FASES (se calcula todo sin tocar la salida y recién después se abre UNA sesión del escritor, se planifica y se aplica); el gate
  sigue siendo fail-closed y la política es explícita (una salida en `REVIEW` no se escribe salvo `escribir_revision`, que NO entra a la clave
  de caché); la procedencia es la de lo que corrió (huella de la normal antes y después de decodificar, encoder que DEVOLVIÓ el exportador, parámetros
  que cambian los bytes) y `bordes`/`escala_x`/`escala_y` viajan IGUALES a `integrar` y a cada métrica; las salidas de corridas anteriores se
  conservan salvo `retirar_no_incluidas` (que exige una enumeración COMPLETA: lo no visto no se da por ausente). La raíz de salida se valida
  ANTES de procesar nada (`validar_raiz_de_salida`), y el informe nunca se escribe dentro del mod. Solo `ERRORES_POR_ARCHIVO` (tipados) hace fallar UNA ruta: nunca `except Exception`. La carpeta de
  entrada la enumera `provider.py` (`cli.descubrir_normales_de_carpeta` es un adaptador delgado); el exportador real se ata al `texconv` verificado con `crear_exportador_texconv` y
  `cli.py`/`lote.py` están en el censo de `tests/test_texconv_invariant.py` (nombran `texconv` como dato o como argumento; no lanzan procesos).
  Guía para probarlo en un rig real: `docs/operations/native_parallax_prueba_en_rig.md`. Ver ADR 0014, "Actualización de NP-A0a".
  `cli.py perfil` (NP-A0e) es un diagnóstico de SOLO lectura sobre el `modlist.txt` de un perfil MO2 (usa `mo2/ganadores.py`): dice si
  otro mod, `overwrite` o un mod de control tapa la entrada que se mediría o la salida que se publicaría (#792 H12). No pide `texconv`,
  no coordina ni escribe, y no nombra `procesar_lote` ni `exportar_altura_bc4` (el censo de llamadores sigue siendo solo `cli.py`). Sale con
  4 si hay algo tapado. Ancla: `tests/test_native_parallax_cli_perfil.py` (congela los subcomandos y cuáles piden `texconv`).
  `cli.py generar --data-root … --mod … [--perfil …]` (NP-A0f) usa ese MISMO diagnóstico: no mide una `*_n.dds` que otro mod tapa o que el overlay
  no entrega (la deja en `omitidas_al_enumerar` del informe) y AVISA antes de escribir si la salida va a quedar tapada o sin habilitar, pero no
  bloquea (la primera corrida todavía no tiene el mod de salida ordenado en MO2). No se combina con `--retirar-no-incluidas` (lo omitido no es
  «ausente»).
- `diagnostico_perfil.py` (NP-A0g) es LA pieza que contesta «¿qué gana, en el perfil de MO2, la entrada y la salida de un mod?»: `cargar_perfil`
  (lee el `modlist.txt` y resuelve quién gana cada ruta), `hallazgos_del_perfil` (qué de eso es un problema), `descubrir_normales_del_mod` (la
  enumeración, con `exigir_completa`) y `diagnosticar_perfil` (las tres juntas, para las superficies). Es de **solo lectura**, falla cerrado con
  `PerfilError` y NO importa `cli.py` (arrastraría el lote, el exportador y la CLI): la línea de comandos (`perfil` y `generar`) y toda superficie
  que lo muestre lo importan. Si cada una resolviera el perfil por su cuenta contestarían distinto (el hermano de AGENTS.md). Ancla por AST en
  `tests/test_native_parallax_diagnostico_perfil.py`: el resolutor `indexar_ganadores` se llama SOLO desde este módulo y los consumidores de
  `diagnosticar_perfil` están congelados por igualdad literal (una superficie nueva rompe el ancla hasta que se la declare y se cablee junto a las demás).
- `provider.py` (NP-A0b) descubre los `*_n.dds` de una carpeta o de un mod y es **puro y de solo lectura**: sus imports (stdlib, `output.py` y
  `links.py`) y sus llamadas al disco están congelados por AST; no decodifica, no sabe del exportador, del gate, del escritor ni de MO2. Nueve
  propiedades con su test: nunca sigue enlaces (los ancestros de la raíz sí se aceptan: los elige el operador) y revalida cada carpeta, la raíz
  incluida, después de abrirla; un `*_p.dds` autoral no se pisa y se compara por `clave_de_ruta` en TODO el árbol, no por carpeta física; **lo no observado no se da por ausente** (`Descubrimiento.completo` es `False` si hubo `CARPETA_ILEGIBLE` o `ILEGIBLE`: quien retira salidas
  por «ya no figura» la necesita completa); la huella se lee acotada por el tamaño validado y solo de lo que sobrevive; un archivo que falla no aborta
  pero un exceso de entradas sí (nunca trunca); es determinista (orden por `clave_de_ruta`); el vocabulario de `MotivoDeOmision` está cerrado y clasificado
  por igualdad literal (un motivo nuevo obliga a decidir si deja lo no observado). `NormalDescubierta` cumple `lote.NormalAProcesar`. La CLI lo usa (los tests de
  su sección «Enumeración» son el contrato que cumple) y el lote le delega la lectura acotada (`huella_acotada`, UNA sola; la del lote se borró). Anclas por AST en
  `tests/test_native_parallax_producto_invariant.py`: solo `provider.py` y `output_writer.py` enumeran carpetas y los lectores de archivo con huella están
  congelados. Ver ADR 0014, "Actualización de NP-A0b" y "Cableado del proveedor a la CLI".
- `diagnostico_texconv.py` (NP-A0c) es el diagnóstico PREVIO del `texconv` configurado: dice, en español y antes de exportar, si el binario
  existe, si es un archivo, si se puede leer y si su huella es la del pin. Es puro y **no calcula ninguna huella**: delega en
  `export.verificar_texconv` y traduce sus cuatro fallos (más «sin versión fijada») a un vocabulario CERRADO de siete estados con un mensaje
  accionable. La ruta sale del argumento o de `SKYCLAW_TEXCONV_EXE` (el mismo nombre que lee `cli._resolver_texconv`), solo un valor vacío
  cuenta como no configurado (la misma regla que la CLI; una tabla de entradas ancla que las dos elijan la misma ruta) y **la ruta no se recorta**: se usa como la escribió el llamador y el mensaje la muestra entre comillas angulares
  con los controles escapados (recortarla aprobaría rutas que el exportador rechaza). `EstadoDeTexconv`, la superficie pública y las dos
  validaciones del dato están congeladas por igualdad en `tests/test_native_parallax_diagnostico_texconv.py` (un estado nuevo sin escenario
  rompe el test), y el módulo está en el censo de `tests/test_texconv_invariant.py` como módulo que nombra `texconv` sin lanzar procesos.
  La CLI ya lo usa (`crear_exportador_texconv` diagnostica antes de exportar y `_resolver_texconv` delega en `ruta_configurada`: el nombre de la variable y la regla de la ruta viven SOLO en este módulo, con un ancla por AST); todavía NO lo usa el registro de herramientas (NP-A0). Ver ADR 0014, "Actualización de NP-A0c" y "Cableado del diagnóstico a la CLI".
- **Piso de contenido (NP-NO-EMPTY, #792 H1/H3).** `lote.tiene_contenido` / `DESVIACION_MINIMA_DE_ALTURA` (8/255, la misma de
  `parallax_unattended.auditor.SD_MINIMA`; un test las ata): una altura cuya desviación queda bajo el piso (o no es finita) pasa a
  `EN_REVISION` con el motivo `SIN_CONTENIDO` y veredicto `REVIEW`, **aunque el gate la aceptara y aunque se pida `--incluir-revision`**; no
  se exporta ni se abre la salida, y no retira la salida vigente de esa textura. No es un umbral de calidad: no aprueba nada, solo impide
  publicar un `_p.dds` válido en formato que el juego carga sin efecto. Es política: no entra a la procedencia (no invalida cachés). Límite: el
  piso sale de un censo de tres mapas planos contra doce con contenido (hueco entre sd 1.3 y 20.2 sobre 255), no de un benchmark.

## Verificación segura

`tests/test_clean_room_invariant.py` y `tests/test_native_parallax_*.py`.
