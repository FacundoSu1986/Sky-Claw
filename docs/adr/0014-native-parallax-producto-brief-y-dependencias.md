# ADR 0014 — Generador nativo de parallax: Synthesis C como design brief, layout de producto y estrategia de dependencias

**Fecha:** 2026-10-19
**Estado:** Aceptada el 2026-10-19 por el operador (instrucción en sesión: «Sí, aceptá el ADR 0014»),
respuesta a la pregunta explícita de si aceptaba este ADR, en este PR aparte. Se propuso el mismo día
(#748, merge `6319fb6c`), redactada por Claude Code como Tech Lead por delegación explícita del operador
(instrucción en sesión, 2026-10-19: «tomá el rol de Tech Lead y hacé lo que sea necesario para avanzar
[…] el objetivo es tener el generador de parallax nativo»). Aceptar el ADR **adopta** las decisiones de §2
—Synthesis C como design brief, layout separado de `research/`, extra opcional `native-parallax` con lock
propio y `texconv` externo—, pero **no autoriza merges**: cada PR del orden de §2.5 (NP-D0, NP-C0, …) se
revisa y se fusiona con autorización expresa, como cualquier cambio del repo. Lo que §6 marca como no
verificado sigue así salvo lo que NP-C0 demostró ejecutando (receta del lock propio del extra y el rango de
Pillow que lo hace auditable; ver «Actualización de NP-C0» al final de §6): quedan abiertos la versión y
licencia de `texconv` (NP-E0) y la decodificación de BC5/BC7 reales (EXP-003).
**Contexto de origen:** `origin/main` `b0737e1f` (merge de #742).
**Relacionados:** [ADR 0013](0013-clean-room-native-parallax.md) (clean-room), issue #676 (preview
productivo: tracker y Definition of Done), `docs/design/research/2026-09-21-native-parallax-battle/50_sintesis_c.md`
(Synthesis C), [`sky_claw/local/native_parallax/AGENTS.md`](../../sky_claw/local/native_parallax/AGENTS.md).

## 1. Contexto

Verificado leyendo el repositorio (2026-10-19):

- **La investigación base está cerrada.** Math foundation (#681, #685) y la revalidación de M2/M3
  (REVAL-1, `M3_DECISION_EQUIVALENT = YES`) están en `main`: el gate previo que fija el issue #676
  (`#667 → M2/M3 revalidation → review → preview productivo`) está cumplido.
- **No existe ningún módulo de producto.** `sky_claw/local/native_parallax/` contiene sólo `research/`;
  `AGENTS.md` del paquete declara que ningún camino de producción importa `research`.
- **Hay un design brief sin adoptar.** Synthesis C (2026-09-21) define un MVP determinista (MODE 0, sin IA),
  un orden de PRs (`NP-C0 … NP-RIG`) y qué no construir todavía. NP-R0 (política clean-room) se cumplió con
  el ADR 0013; el resto del brief nunca se adoptó formalmente.
- **numpy y Pillow son hoy dependencias de desarrollo.** Sólo figuran en el extra `dev` de `pyproject.toml`
  («Research es dev-only; ningún camino de producción importa …research»); no están en `dependencies`
  ni en `requirements.lock`. CI instala con `pip install --require-hashes -r requirements.lock` y corre
  `pip-audit --strict` sobre ese lock: sumar una dependencia al runtime es un cambio de contrato, no un detalle.
- **El issue #676 y Synthesis C difieren en un punto.** El issue enumera un `image_codec.py` con encoder BC4
  propio (viene del patch externo `ParallaxArena.patch`, que no está en el repo y cuya aplicación monolítica
  el issue prohíbe); Synthesis C (§10, NP-E0) dice «no escritura BC4/BC7 propia (texconv externo)».
- **H4 (#723) y M6 (#675) son research.** Afectan a `resize_normal` y al diagnóstico de mismatch del par
  authored; ninguno de los dos está en la ruta del generador MODE 0.

## 2. Decisión

1. **Se adopta Synthesis C como design brief del generador**: núcleo determinista (MODE 0), sin IA en
   release, exporter inicial `LegacyParallaxExporter` (`*_p.dds` BC4 + mips), CLI/HITL existentes, nada de
   terrain/TruePBR/GUI. El issue #676 sigue siendo el tracker y su Definition of Done no cambia
   (`NATIVE_PARALLAX_PRODUCT_STATUS = EXPERIMENTAL_OPT_IN`, `PARALLAXR_REPLACEMENT_CLAIM = NOT_SUPPORTED`).
2. **Layout.** El producto vive en `sky_claw/local/native_parallax/` (`decode.py`, `integrate.py`,
   `metrics.py`, `export.py`, `output.py`, `provider.py`, según el brief), **separado de `research/`**.
   Producción **no importa** `research`: lo probado en research se **reimplementa** en el producto y un test
   de equivalencia lo compara contra research como oráculo. Importarlo acoplaría el producto a código que
   por contrato es dev-only y está sujeto a cambios de método.
3. **Dependencias.** `numpy` y `Pillow` pasan a un **extra opcional `native-parallax`** en
   `pyproject.toml`, con import perezoso y error tipado si faltan; el extra `dev` los sigue incluyendo (los
   tests los necesitan). **No** entran en `dependencies`, y por lo tanto no entran al runtime base ni al
   ejecutable congelado, hasta que un ADR posterior lo decida con medición de tamaño.

   **Lock del extra.** `requirements.lock` se compila sin extras (su cabecera: `uv pip compile pyproject.toml`)
   y es el que CI instala con `--require-hashes` y audita con `pip-audit --strict`. Regenerarlo no cubre el
   extra, y compilarlo con `--extra native-parallax` metería numpy y Pillow en el lock del runtime base, que
   es justo lo que esta decisión evita. Por eso el extra tiene un **lock propio**
   (`requirements-native-parallax.lock`, compilado con `--extra native-parallax`) y un paso de CI aparte que
   lo instala con `--require-hashes` y lo audita con `pip-audit --strict`. `requirements.lock` no cambia.
   `uv.lock` ya registra numpy y Pillow (por `dev`) y se actualiza al agregar el extra. El nombre del archivo
   y la forma del paso de CI son lo que NP-C0 tiene que demostrar ejecutando; este ADR no los da por
   verificados.
4. **Escritura DDS/BC4.** Se usa `texconv` (herramienta externa; el brief la registra como MIT, sin verificar para la versión
   que se fije) como `DYNAMIC_EXTERNAL_TOOL` con
   versión fijada, según NP-E0. El encoder BC4 propio del patch se descarta salvo que el rig (NP-RIG) lo
   exija. Lectura DDS: Pillow. Con cualquiera de las dos vías el DoD de #676 exige validar BC4 y mips
   contra una implementación de referencia.
5. **Orden de implementación** (cada PR chico, con su write-set, sin acoplarse a DynDOLOD ni Runtime-Vault):

   | PR | Contenido | Estado |
   |---|---|---|
   | NP-D0 | dataset spec + generador sintético + fixture MatSynth (solo los materiales con licencia CC0 **por material**; descarga externa, hash-pinned, nunca commiteada) en `native_parallax/bench/`: el benchmark del que se derivan los thresholds | **parcial** (#758): generador sintético y especificación hechos; falta el fixture MatSynth (descarga externa; ver «Actualización de NP-D0b») |
   | NP-C0 | `NormalDecoder` (DDS/PNG, convención DirectX, confianza por `nz`) | hecho (#756) |
   | NP-I0 | `normal_fft_periodic_v1` + variante Neumann + normalización | hecho (#759) |
   | NP-Q0 | métricas y gates con thresholds **derivados** del benchmark | métricas hechas (#760) y gate fail-closed sin umbrales vigentes (#761); los umbrales validados esperan al corpus real |
   | NP-X0 | experimentos de signo/escala/inversión y `REVIEW_RATE` | NP-X0a hecho (#762); NP-X0b: runner y `REVIEW_RATE` simulada sobre el corpus sintético hechos, sin validar nada (ver la actualización de NP-X0b) |
   | NP-E0 | `DdsExporter` (texconv) | **parcial**: NP-E0a (núcleo puro, #771) y NP-E0b (ejecutor del texconv fijado) hechos (con smoke real en Windows), ver «Actualización de NP-E0b»; falta el registro/detección de `texconv` en el tool registry (NP-A0) |
   | NP-O0 | `ManagedOutput` + manifest + cache por hash | **parcial**: NP-O0a (núcleo puro: manifiesto, clave de caché y planificador) hecho, ver «Actualización de NP-O0a»; falta el escritor (NP-O0b): NP-O0b1 (reconciliación pura tras un corte) hecho, ver «Actualización de NP-O0b1»; falta NP-O0b2 (el escritor sobre el disco) |
   | NP-A0 | `AssetProvider` (carpeta plana y adapter MO2) + batch | pendiente |
   | NP-V0 / NP-RIG | preview offline y rig in-game (requiere Windows y Skyrim reales) | pendiente |

   La tabla es el camino de MODE 0. El resto del roadmap de Synthesis C (NP-H0, NP-AI0, NP-AI2, NP-CM,
   NP-DOC) sigue como lo define esa sección («Roadmap por PRs») y no se decide acá.

6. **Independencia de los lanes de research.** H4/#723 y M6/#675 **no bloquean** NP-C0…NP-A0. Sí limitan lo
   que se puede *afirmar*: ninguna conclusión del producto puede apoyarse en `resize_normal` ni en el
   diagnóstico M6 hasta que esos lanes cierren. Los thresholds de los gates (AUTO_ACCEPT/REVIEW/REJECT) se
   derivan del benchmark y se versionan; nunca se fijan a mano.

## 3. Alternativas evaluadas

- **numpy/Pillow en `dependencies`.** Rechazada: cambia el lock hash-enforced, el `pip-audit` y el
  ejecutable congelado sin haber medido el costo, para una función opt-in.
- **Importar `research` desde el producto.** Rechazada: contradice `AGENTS.md` del paquete y ata el
  producto a código cuyo método sigue en revisión (H4, M6).
- **Aplicar el patch externo monolítico.** Prohibido por #676; además no está en el repo y mezcla
  correcciones científicas con el MVP.
- **Esperar a que cierren H4 y M6.** Rechazada: no están en la ruta crítica y el gate de #676 ya está
  cumplido; esperar frena el producto sin reducir ningún riesgo de MODE 0.
- **Encoder BC4 propio.** Rechazada por defecto: el brief la excluye y obligaría a validar un encoder
  completo contra DirectXTex/texconv antes de poder usarlo.

## 4. Consecuencias

- El primer PR de código que toque dependencias (NP-C0) introduce el extra, su lock propio con el paso de
  CI correspondiente y dos anclas: (a) AST — ningún módulo de producto de `native_parallax` importa
  `research`; (b) el extra `native-parallax` está declarado y el import de `numpy`/`Pillow` es perezoso con
  error tipado.
- Cada PR del orden anterior se revisa como cualquier cambio del repo (Lint, tipos, tests en Windows,
  revisores automáticos). Este ADR no autoriza merges.
- Queda abierta una decisión de producto que NO se toma acá: la normalización de altura por percentiles
  (p2…p98) que menciona Synthesis C §6. El issue #676 la marca como intención de authoring y no como
  consecuencia de la matemática; se decide en NP-I0 con estudio de amplitud relativa.

## 5. Non-goals

- No implementa código ni cambia dependencias: sólo registra la decisión.
- No levanta ningún bloqueo de M6 ni autoriza el merge de H4/#723.
- No afirma equivalencia ni superioridad frente a ParallaxR ni compatibilidad in-game.
- No decide el nombre definitivo del producto, la versión fijada de texconv ni el soporte de texturas
  rectangulares (NP-C0 y NP-I0 deben cubrirlo según #676).

## 6. Qué se verificó y qué no

- **Verificado leyendo:** el estado del paquete, los extras y el lock, el gate de lockfile de CI, el
  texto del ADR 0013, Synthesis C y el issue #676.
- **Verificado ejecutando:** Pillow 11.3.0 abre y guarda `.dds`, y su plugin DDS referencia BC4, BC5 y BC7;
  `uv pip compile --help` indica que `--extra` es lo que incluye dependencias opcionales; `requirements.lock`
  no contiene numpy ni Pillow y `uv.lock` sí.
- **No verificado:** que Pillow decodifique correctamente BC5/BC7 reales de Skyrim (se cierra en NP-C0 con
  EXP-003); el costo en tamaño de numpy/Pillow en el ejecutable congelado (por eso queda fuera del runtime
  base); la receta del lock propio del extra (nombre del archivo y paso de CI), que NP-C0 tiene que
  demostrar ejecutando; la disponibilidad y licencia de la versión exacta de texconv a fijar (NP-E0).

### Actualización de NP-C0 (2026-10-19)

NP-C0 ejecutó lo que este ADR dejaba abierto para el decodificador y el lock del extra. Verificado
ejecutando (Linux, `uv 0.11.32`, Pillow 11.3.0 y 12.3.0); lo que sigue sin verificar se dice al final.

- **Receta del lock propio.** `requirements-native-parallax.lock` se genera sembrando la salida con el lock
  base y compilando con el extra: `cp requirements.lock requirements-native-parallax.lock` y
  `uv pip compile pyproject.toml --extra native-parallax -o requirements-native-parallax.lock
  --python-version 3.11 --python-platform windows --generate-hashes`. `uv` toma el archivo existente como
  preferencias: el resultado **solo suma** `numpy` y `pillow` (0 líneas quitadas respecto de
  `requirements.lock`; sin la siembra, una compilación nueva sube otras versiones y los dos locks
  discreparían). `--python-platform windows` reproduce lo que CI instala (el lock base incluye
  `comtypes`, que es de Windows). Un dry-run de `uv pip install --require-hashes` contra Windows lo acepta.
- **Pillow tiene que ser ≥ 13.0.** `pip-audit` reportó 35 avisos sobre Pillow 11.3.0 (la versión que
  fijaba `[dev]`), todos corregidos entre 12.1.1 y 12.3.0, y después 8 avisos nuevos sobre 12.3.0
  (CVE-2026-112395, -112397, -112400, -112404, -112895, -112896, -112897 y -112899) corregidos solo en
  13.0.0, sin backport a 12.x; con 13.0.0 no reporta ninguno. El paso de CI que pide la decisión 3 no puede
  pasar con 12.3.0. El extra y `[dev]` quedan en `pillow>=13.0,<14` (rangos disjuntos harían irresoluble
  `uv sync --extra dev --extra native-parallax`). El techo anterior `<13` se había puesto porque se esperaba
  que Pillow 13 quitara el parámetro `mode` de `Image.fromarray`; verificado ejecutando con 13.0.0, sigue
  aceptado (con `L` y `RGB` sobre `uint8`, los usos del repo) y sin advertencia. Ahora el techo `<14` es
  porque la próxima mayor no está verificada. Con 13.0.0 pasan los tests que tocan PIL o
  `native_parallax` (2761 corridos en Linux; el CI de Windows lo confirma) y el único cambio necesario fue
  declararla en `PILLOW_ESTADOS` de R7 como `OBSERVADA_NO_CERTIFICADA`.
- **BC5 llega sin Z.** Pillow 11.3.0, 12.3.0 y 13.0.0 decodifican BC5 como RGB con B = 0 (bloque armado a mano), así
  que `NormalDecoder` reconstruye `Z = sqrt(1 − x² − y²)`. BC5 con signo (BC5S) se rechaza.
- **Entrada no confiable** (revisión de Codex en #756; los archivos vienen de mods). `NormalDecoder` solo
  acepta PNG y DDS; de los DDS de bloques, DXT1, BC5 y BC7 (los que tienen prueba), más los sin comprimir con
  cabecera clásica o DX10. Rechaza BC5 con signo, BC6H, DXT3 y DXT5 (DXT5nm guarda X en el alfa: leerlo como
  RGB daría normales incorrectas sin ningún error). Un DDS que Pillow no implementa (`NotImplementedError` al
  abrir) y la advertencia de bomba de descompresión (Pillow solo advierte entre `MAX_IMAGE_PIXELS` y su
  doble) terminan en errores tipados antes de reservar los arrays. No hay tope de memoria propio: la salida
  ocupa 32 bytes por píxel. Solo se aceptan **8 bits por canal**, comprobados sobre el layout real y no sobre
  el modo que Pillow informa (revisión de CodeRabbit): un PNG de 16 bits se abre como `RGB` con `rawmode`
  `RGB;16B` y se reducía en silencio; en un DDS sin comprimir de cabecera clásica el códec `dds_rgb` acepta
  cualquier máscara (565, 1555, 4444, R16G16 con un canal en cero). Se enumeraron y se rechazan; verificado
  ejecutando con Pillow 12.3.0.
- **No verificado:** que Pillow decodifique bien BC5/BC7 **reales** de Skyrim (EXP-003: las pruebas de
  NP-C0 usan bloques sintéticos); el paso de CI de este extra sobre `windows-latest` hasta que corra el
  CI de NP-C0; el costo en tamaño en el ejecutable congelado; `texconv` (NP-E0).

### Actualización de NP-D0 (2026-10-20)

NP-D0 queda **PARCIAL**: este PR entrega el corpus sintético y su especificación; el fixture MatSynth (solo
materiales CC0) no está (ver «No incluido» y «Actualización de NP-D0b»). Verificado ejecutando en Linux (Python 3.11, numpy 2.4.6); el CI de Windows lo
confirma en el PR.

- **Qué entra.** `native_parallax/bench/sintetico.py` reimplementa en el producto los 15 casos periódicos
  (`S01`–`S15`) y los 2 controles negativos (`N01`, `N02`) de `research/synthetic_height.py`, la normal de
  referencia H → N por derivada espectral periódica, el ruido gaussiano con semilla CRC32 por (caso, sigma) y
  la cuantización entera a 8, 10 y 16 bits. `ESPECIFICACION_V1` (`np-d0.v1`) congela ids, clases, las cinco
  grillas (incluidas 128×256 y 256×128), los cuatro sigmas y los bits; `huella_especificacion` la resume.
- **Cómo se ancla.** `tests/test_native_parallax_bench_sintetico.py` compara el producto con research como
  oráculo, bit a bit, sobre los 17 casos y tres grillas (altura) y sobre 17 casos × 4 pares de escalas
  (normal); congela la especificación por igualdad literal; y agrega una tabla de huellas numéricas por caso
  que no depende del oráculo (si research cambiara una fórmula, oráculo y copia se moverían juntos y solo
  esa tabla lo vería) y la inversa analítica de un seno puro.
- **Dos modelos de cuantización, a propósito** (revisión de Codex en #758). `cuantizar_normal` exige elegir entre
  `TRES_CANALES` (cuantiza X, Y y Z y renormaliza) y `DOS_CANALES_RECONSTRUYE_Z` (cuantiza X e Y y reconstruye Z, el
  contrato de BC5): el primero es una cota optimista. Sobre `S14_steep` a Q8 (256×256) el de dos canales deja 33 112 de
  65 536 píxeles con `nz == 0` y el de tres, ninguno; calibrar los thresholds de NP-Q0 solo con el optimista sería
  optimista para BC5. No hay modelo por defecto. Ninguno simula la compresión por bloques (NP-E0).
- **`razon_borde` y los campos que arrancan planos.** research devuelve 1.0 si el paso del borde a su vecino es nulo,
  sin mirar el resto; el producto cae al promedio de todos los pasos adyacentes (defecto latente de research,
  documentado en un test; el oráculo sigue igualando a los 17 casos del corpus).
- **Reproducibilidad: qué se promete y qué no.** El criterio de Synthesis C («reproducible por hash») se
  cumple para la DEFINICIÓN del corpus (huella SHA-256 de la especificación) y, para los datos, con una tabla
  de huellas con tolerancia 1e-9. NO se fija un hash de los bytes de `float64`: `np.sin` y `np.exp` no
  garantizan los mismos bits entre CPUs y rutas SIMD, y eso no está verificado entre plataformas, así que un
  hash exacto sería frágil y no un contrato.
- **Tamaño mínimo.** Las grillas deben ser de al menos 128: con 32 ciclos en `S11`, 64 muestras serían
  exactamente Nyquist y el seno valdría cero en cada una.
- **No incluido.** (1) El fixture MatSynth (solo materiales CC0; descarga externa, hash-pinned, nunca commiteada): necesita
  que el operador decida qué subconjunto, qué hashes y desde dónde descargar; `research/` ya tiene fetchers y
  un manifiesto (`exp-m3-clean-authored-manifest.json`) que son el punto de partida, pero promoverlos al
  producto trae egress de red y es otro PR. (2) Variantes con error de compresión por bloques: hace falta un
  codificador y el producto no tiene uno (decisión 4; texconv, NP-E0). (3) La inversa N → H y el integrador:
  NP-I0. Debe compartir con `bench/sintetico.py` la convención de frecuencias (`_ejes_de_frecuencia`); un
  solver con otra daría gradientes escalados por la resolución.

### Actualización de NP-D0b (2026-10-21)

Corrige una premisa de este ADR: **la licencia de MatSynth es POR MATERIAL, no del conjunto**. Los textos anteriores que hablaban
del «fixture MatSynth CC0» (tabla de §2, «Actualización de NP-D0», `bench/__init__.py`, `bench/sintetico.py`) se ajustaron a «solo
materiales CC0». Las notas de investigación de `docs/design/research/` (p. ej. «~95 % CC0») son fotografías de lo que se sabía
entonces y no se reescriben: sobre la licencia manda esta sección.

- **Fuente.** Informe de la PC con Windows (consulta a los metadatos del conjunto en Hugging Face): cada material trae su
  `metadata.license`; en el subconjunto de prueba consultado, 24 de 89 materiales eran CC BY-NC-ND (no comercial y sin
  obras derivadas). Por la cláusula «NoDerivatives» de CC BY-NC-ND 4.0, no se puede compartir material adaptado: si el fixture de
  1024 derivado de una fuente de 4096 llegara a distribuirse, no podría hacerlo bajo esa licencia (lectura mía del texto de la
  licencia, no asesoramiento legal). **No lo reverifiqué desde esta sesión**: es dato del informe, no verificado ejecutando por mí.
- **Decisión técnica (mía).** El fixture admite únicamente materiales cuya licencia sea exactamente `CC0`. CC BY queda fuera aunque
  `CLEAN_ROOM.md` lo permita con atribución: exigiría arrastrar créditos por archivo, y el corpus CC0 alcanza para el benchmark.
  CC BY-NC-ND y cualquier otra, excluidas.
- **Tamaño.** El informe encontró únicamente originales de 4096²; no hay variantes de 1024. El original de 4096² queda como fuente
  fijada por hash (nunca commiteada) y el 1024 se **deriva después** con un procedimiento determinista propio, nunca descargado.
  Para la **altura** sirve `generar_mips` (promedio de área sobre un campo 2-D). La **normal** necesita una reducción propia:
  `generar_mips` no se le aplica, y promediar componentes da vectores que no son unitarios, así que hay que decodificar, promediar y
  renormalizar. Esa reducción se define y se testea en NP-D0b.
- **Qué NO se hizo.** No se descargó ni se versionó ningún material; no hay fixture. Los hashes de los 20 materiales CC0 que
  listó el informe todavía no están en el repo: antes de fijarlos hay que reverificar la licencia de cada uno en la descarga.
- **Qué debe fijar el manifiesto del fixture (NP-D0b).** (1) La **evidencia de la licencia**, no solo los bytes: la revisión del
  conjunto de datos y, por material, el valor de `metadata.license` con una huella o referencia inmutable de ese metadato; la huella
  de la imagen no prueba con qué licencia se admitió el material. (2) La **convención de la normal declarada**, `OPENGL` o
  `DIRECTX`, nunca inferida: el decodificador produce DirectX y su `source_convention` vale `DIRECTX` por defecto (`decode.py`), así que una fuente OpenGL cargada sin declararlo queda con la Y sin invertir. La revisión
  automática señala que la tarjeta del conjunto la declara OpenGL: **no lo verifiqué**; NP-D0b debe leer la tarjeta y registrar lo
  que diga. (3) Una **huella del corpus** (`huella_del_corpus` de la validación contra corpus real) que cubra lo que se mide y no
  solo las fuentes: la versión del procedimiento de reducción y los hashes de los arrays derivados (normal y altura a 1024); si
  cambia `generar_mips` o la conversión, los píxeles medidos cambian aunque las huellas de origen sigan iguales.
- **Política provisional del gate (decisión técnica, no validada).** Falsos aceptos ≤ 5 %, confianza 95 %, revisión manual ≤ 15 %.
  Son números del Tech Lead para tener un objetivo, no evidencia: **no pasan a `UMBRALES_VIGENTES`** (hoy `None`, congelado por
  igualdad en `tests/test_native_parallax_gates.py`) hasta validarlos contra un corpus real. Cambiar esa constante rompe el ancla
  a propósito. **El tamaño del corpus condiciona el objetivo:** el gate usa la cota superior de Clopper-Pearson a un lado
  (`gates.py`); con 0 malas entre `n` aceptadas esa cota es `1 − 0,05^(1/n)`, o sea 13,9 % con `n = 20` y recién 4,95 % con
  `n = 59`. El gate cuenta **texturas** (`n_total`, `n_aceptadas`), no materiales: si cada material aporta una sola muestra
  aceptada, los 20 materiales del informe **no alcanzan** ni para un candidato perfecto; con un objetivo de 5 % hacen falta al menos
  59 muestras aceptadas independientes (unas 70 en total si hasta el 15 % va a revisión). Varias muestras del mismo material no las
  trato como independientes, así que más recortes de los mismos materiales no sustituyen a más materiales: NP-D0b debe ampliar el
  subconjunto CC0 o dejar abiertos estos valores.

### Actualización de NP-I0 (2026-10-20)

NP-I0 entrega el integrador normal → altura. Verificado ejecutando en Linux (Python 3.11, numpy 2.4.6, Pillow
12.3.0); el CI de Windows lo confirma en el PR.

- **Qué entra.** `native_parallax/integrate.py`: `normal_fft_periodic_v1` (solver de Poisson por FFT sobre el
  toro, reimplementado y comparado bit a bit con `research/normal_fft_periodic.py` en los 17 casos del corpus de
  NP-D0, tres grillas y varias escalas) y la variante `normal_fft_neumann_v1` (derivada normal nula en los
  bordes, por extensión de espejo a 2H×2W con el mismo solver; sin dependencias nuevas). La inversa divide por la
  escala (PR-MATH-A §8).
- **Una sola convención de frecuencias.** `espectral.ejes_de_frecuencia` es la única definición, la usan el corpus
  sintético y el integrador, y un ancla de AST congela que ningún otro módulo de producto llame a `fftfreq`
  (el bug de unidades de NP-M0 —`2π · fftfreq` a secas deriva respecto de la muestra, no de la textura— es el que
  esto evita). `_extra.importar_numpy` es el punto único de importación perezosa de numpy, también congelado.
- **EXP-001 y EXP-002 del brief, sobre el corpus limpio.** Error crudo centrado (el ajuste afín no puede esconder
  un error de escala) ≤ 2.5·10⁻⁵ en los 15 casos periódicos; los 12 de banda limitada se recuperan al redondeo
  (< 10⁻¹²); el criterio literal del brief (RMSE alineado < 0.05) se cumple con tres órdenes de margen; el salto de
  costura es < 0.01. La variante Neumann recupera el coseno con derivada normal nula al redondeo, y se demuestra que
  el periódico no lo reproduce (error > 10⁻²). **Límite:** son normales sintéticas y limpias; nada de esto dice
  cómo se comporta con ruido, cuantización o normales reales (NP-Q0 y EXP-001b).
- **Normalización de altura: decisión de §4.** El estudio de amplitud relativa sobre el corpus sintético (256×256)
  compara el rango completo con p2–p98: el desvío de la altura normalizada crece entre 1.00× y 1.75×, y casi nada
  en las superficies suaves (senos, 1.00×; bultos 1.69× y ruido periódico 1.75× son los que cambian). Lo que
  esto SÍ muestra es que elegir uno u otro altera la amplitud relativa entre texturas cuando hay picos o ruido
  (el caso realista de authoring). Lo que NO puede mostrar es cuál se percibe mejor en el juego, así que el
  producto **no fija un valor por defecto**: `normalizar_altura` exige elegir el modo y, si son percentiles,
  indicarlos (nada de p2/p98 escritos a mano en el código, ADR §6). El default lo decide NP-RIG con evidencia
  in-game.
- **El piso de `nz`** (`1e-6`, el de NP-M0) es una guarda numérica contra `1/0` que CUENTA los píxeles afectados;
  no es una política de confianza. Las políticas de EXP-M1 (REVIEW/REJECT, regularización de Tikhonov) siguen en
  `EXP_M1_GO_WAITING_REVIEW` y esperan a los thresholds derivados del benchmark (NP-Q0).
- **Ejes mínimos.** Con bordes periódicos cada eje necesita al menos 3 muestras: con 2, el único bin no continuo
  es Nyquist, cuya derivada es nula por convención, y el solver devolvía una altura plana SIN avisar (medido: un
  escalón de pendiente en Y da rango 0.0 con 2 filas y 0.049 con 3). Neumann resuelve sobre 2n muestras, donde 2
  alcanzan (mismo escalón: 0.13), así que el umbral sigue al largo de la grilla que se resuelve y no al de la
  entrada.
- **Memoria.** El solver opera el espectro in situ, extiende por espejo de a un campo y libera cada espectro al
  dejar de usarlo, con las mismas operaciones elemento a elemento (bit a bit igual a la versión anterior en 42
  combinaciones de forma, escala y bordes). Medido en Linux con numpy 2.4.6: pico de RSS en 4096×4096 de 4422 MiB
  en Neumann (antes 7558 MiB, 33 s → 25 s) y 1622 MiB en periódico (antes 2262 MiB), ambos con 384 MiB de
  entrada. **Límite declarado:** hoy no hay presupuesto de memoria ni call site que lo acote. Una transformada
  real o una DCT bajarían más el pico, pero no se evaluaron (la primera podría no igualar bit a bit a research y la
  segunda exige una dependencia nueva); se decide cuando NP-O0/NP-A0 fijen el tamaño máximo y la RAM objetivo. Un
  test acota el pico con `tracemalloc` como múltiplo del campo resuelto.
- **Equivalente declarado.** En el solver, fijar a cero el espectro en los bins donde el denominador se anula es
  redundante con el numerador nulo en esos bins; se conserva por fidelidad con research.

### Actualización de NP-Q0a (2026-10-20)

NP-Q0 se divide en dos PRs chicos (§2.5): **NP-Q0a** entrega las métricas (este PR) y **NP-Q0b** el mecanismo de
umbrales derivados. Verificado ejecutando en Linux (Python 3.11, numpy 2.4.6, Pillow 12.3.0); el CI de Windows lo
confirma en el PR.

- **Qué entra.** `native_parallax/metrics.py`: seis de las diez métricas de la tabla §I de la arquitectura A, las
  que se calculan SIN ground truth de altura (sirven igual en producción y sobre el benchmark de NP-D0):
  `acuerdo_normal`, `residuo_de_integrabilidad`, `razon_de_costura`, `rango_dinamico`, `razon_de_recorte` y
  `contar_no_finitos`, más el resultado `AcuerdoNormal`. Sin umbrales ni gates, y sin percentiles por defecto
  (`rango_dinamico` los pide, igual que `normalizar_altura`).
- **Definiciones propias (la tabla deja huecos).** El **acuerdo de normal** re-deriva la normal de la altura y la
  compara con la de entrada; la altura sale con escala y signo arbitrarios, así que se ajusta el escalar `a` de
  `a·∇h ≈ ∇h_normal` por mínimos cuadrados PONDERADOS por `(nz·s)²` (equivale a ajustar en el espacio de la
  normal: un píxel con `nz → 0` pesa ~0 en vez de arrastrar la escala de toda la imagen, y la inversa vale ~1e6
  ahí). El signo de `a` se reporta aparte porque un flip global de gradientes produce `-altura` exacto y solo el
  signo lo delata. El ángulo usa `atan2(|n × m|, n · m)`, no `arccos`, que pierde la mitad de las cifras para
  errores diminutos. El **residuo de integrabilidad** normaliza por el Hessiano completo del campo de gradientes. La
  **razón de costura** es la misma matemática que `bench.razon_borde` (un test enumera los 17 casos para que no se
  separen). Su umbral de "paso nulo" es **relativo a la escala del campo** (`1e-12 · max|altura|`), no absoluto:
  con un `1e-12` fijo, el mismo campo multiplicado por 1e-20 se leía como plano y la razón cambiaba de valor sin
  que la costura cambiara. Es el mismo defecto en dos funciones hermanas y se corrigió en las dos (Codex, #760).
- **Dos defectos de definición que los tests atraparon antes de commitear.** (1) Normalizar el residuo por
  `|∂p/∂y| + |∂q/∂x|` daba ~0.5 en campos separables (`h = f(x) + g(y)`, derivada cruzada nula): era el cociente de dos
  ruidos de redondeo, y el campo más perfecto salía peor evaluado. (2) `continuidad_de_mosaico` (coseno entre
  gradientes de bordes opuestos) **no se publica**: no distingue un salto de valor puro, porque la derivada
  espectral de una rampa periodizada es simétrica respecto de la costura y da 1.0; esa falla ya la mide
  `razon_de_costura`, y no hay un caso que haga fallar a la otra.
- **Fuera y declaradas pendientes.** `banding_index` y `mip_consistency` necesitan la altura codificada a BC4 y los
  mips (NP-E0); `flatness_hf` no está definida con precisión en la tabla; `tileability_continuity`, por lo anterior.
- **Una sola definición de la derivada y de la inversa.** Las métricas necesitan las dos cosas, y cada una vivía
  en línea en un solo sitio. Se promovieron a `espectral.gradientes_espectrales` (igual a
  `research.spectral_gradients` bit a bit en 17 casos × 3 grillas; `bench.normal_desde_altura` la usa) y a
  `integrate.gradientes_de_normal` (igual a `research.gradients_from_normal` bit a bit en 17 casos × 3 escalas;
  `integrar` la usa). `integrar` sigue siendo bit a bit igual a la versión anterior en 42 combinaciones. El ancla de
  AST de la convención de frecuencias pasó a ENUMERAR los módulos que importan de `espectral` y la función que usa
  cada uno, en vez de una lista escrita a mano.
- **Lo que esto NO justifica: un `AUTO_ACCEPT` calibrado con el benchmark sintético.** EXP-M2, sobre texturas
  authored reales, midió que la predicción de riesgo solo-normal FALLÓ (mejor AUC 0.62, `REVIEW_RATE` no viable,
  falsos aceptos ≥ 30 %) y que nada de eso es promovible a política automática. Derivar umbrales del corpus
  sintético daría confianza que los datos reales no respaldan. Por eso NP-Q0b parte de que el mecanismo sea
  **fail-closed**: sin umbrales validados contra un corpus con texturas reales (fixture MatSynth de NP-D0, aún
  ausente) todo va a revisión.
- **Límite declarado.** Las pruebas son sobre normales sintéticas y limpias. No hay call site de producción
  todavía (lo consumen NP-Q0b y posteriores). Las métricas describen; no predicen riesgo.

### Actualización de NP-Q0b (2026-10-20)

NP-Q0b entrega el **contrato fail-closed** del gate, no los umbrales. Verificado ejecutando en Linux (Python 3.11,
numpy 2.4.6); el CI de Windows lo confirma en el PR.

- **Qué entra.** `native_parallax/gates.py`, puro (no importa `numpy` ni `research`): `decidir(metricas)` resuelve con
  `UMBRALES_VIGENTES` y `decidir_con(metricas, umbrales)` deja inyectar un registro (lo usará NP-X0 para medir
  candidatos). Hoy `UMBRALES_VIGENTES is None`, congelado por igualdad en el test: **toda textura va a `REVIEW`**, también
  la normal sintética más limpia (hay un test de punta a punta).
- **`AUTO_ACCEPT` exige evidencia calculada, no declarada.** El registro (`Umbrales`) lleva una
  `ValidacionContraCorpusReal` con los CONTEOS (texturas, aceptadas, aceptadas malas) y el gate calcula la cota
  superior binomial de Clopper-Pearson de la tasa de malas entre las aceptadas; tiene que ser `<= alpha_objetivo`, y la
  tasa de revisión `<= tasa_de_revision_maxima`. No hay un booleano `validado`. La cota se contrasta con la
  distribución binomial exacta en aritmética racional. Es la lección de EXP-M2 (las señales solo-normal no predijeron el
  riesgo en texturas reales, mejor AUC 0.62) hecha mecanismo: derivar en el sintético se puede y queda registrado
  (`huella_de_derivacion`); **validar con él, no**: la huella del corpus de NP-D0 se rechaza como corpus de validación.
- **La evidencia va atada al candidato que validó** (Codex, #761). Sin esto, la validación de un candidato estricto se
  podía reusar con límites más flojos y el gate aceptaba métricas que nunca estuvieron entre las aceptadas medidas. La
  validación lleva la `huella_del_candidato` (SHA-256 de la versión de las métricas, el signo esperado y cada límite) y
  `Umbrales` se niega a construirse si no coincide con la suya. El formato de la huella tiene un valor de referencia
  congelado: cambiar cómo se serializa invalidaría en silencio toda validación ya registrada.
- **Una medición fuera de su dominio es `REVIEW`, no un valor que «pasa»** (Codex, #761). Un error angular o un residuo
  negativos quedaban bajo cualquier límite `>= 0` y producían `AUTO_ACCEPT` (fail-open). `DOMINIO_DE_LAS_METRICAS`
  fija el rango de cada una por su definición (ángulo en [0, 180], residuo en [0, 1], razón >= 0); fuera de él el motivo
  es `METRICA_FUERA_DE_DOMINIO` y no se compara con el límite. Los límites del registro también tienen que caer en ese
  rango.
- **La confianza de la cota es `>= 0.5`** (Codex, #761). Con menos, la «cota superior» puede quedar por DEBAJO de la
  tasa observada (confianza 0.3 sobre 5 de 10 daba 0.4675, bajo el 0.5 observado) y deja de ser una cota; además
  `1 - confianza` pierde toda precisión para valores diminutos (con `1e-17` la bisección devolvía `~8e-31` donde la
  cota real ronda `1e-17`, y un `alpha_objetivo` de `1e-20` pasaba sin cumplirse). Desde 0.5 la resta es exacta
  (Sterbenz). Un único validador sirve a `cota_superior_binomial` y a `ValidacionContraCorpusReal`.
- **El signo cuenta.** Un flip global de gradientes produce `-altura` exacta y el error angular lo absorbe; solo el
  signo del ajuste lo delata. Sin él, un `AUTO_ACCEPT` futuro podría aceptar un relieve invertido. El registro
  declara `signo_esperado` y cualquier otro va a `REVIEW`. Cuál es el correcto para Skyrim lo fija la evidencia de
  NP-X0/NP-RIG, no este PR.
- **Cada métrica pública de `metrics.py` está gateada o excluida con motivo** (`METRICAS_GATEADAS` y
  `METRICAS_NO_GATEADAS`), y el test enumera `metrics.__all__`: una métrica nueva rompe el ancla hasta que se
  decide. Gateadas: el p95 del acuerdo de normal y su signo, el residuo de integrabilidad, la razón de costura y el
  conteo de no finitos. **Excluidas:** `rango_dinamico` (depende de percentiles de authoring y no mide un error) y
  `razon_de_recorte` (depende del modo de normalización, que decide NP-RIG).
- **Solo `REVIEW` y `AUTO_ACCEPT`; no hay `REJECT`.** No existe base para derivar un umbral de rechazo y `REVIEW` ya impide
  el auto-aceptado. Si aparece, aparece con sus umbrales y su evidencia. Un `REVIEW` siempre dice por qué (`Motivo`) y
  todos los motivos aplicables se acumulan, en orden estable.
- **Los umbrales se versionan con la definición de las métricas** (`VERSION_DE_METRICAS = "np-q0a.v1"`): un registro
  derivado con otra definición va a `REVIEW`.
- **Decisión de política ABIERTA, que NO se toma acá.** La tolerancia `alpha_objetivo` de falsos aceptos, la `confianza`
  de la cota (dentro de `[0.5, 1)`) y la `tasa_de_revision_maxima` son del operador y viven en el registro que declara la validación; el código
  garantiza que la evidencia cumple lo declarado, no que lo declarado sea sensato. Se fijan en el PR que publique el
  primer registro, que además tiene que cambiar el ancla `UMBRALES_VIGENTES is None` (por eso es imposible hacerlo sin que
  se vea). Solo `gates.py` conoce `UMBRALES_VIGENTES` (ancla por AST): los demás módulos llaman a `decidir`.
- **Mutantes.** 119 definidos sobre `gates.py` (cota binomial, validadores, registros, acople de la evidencia, dominio,
  métricas, decisión). **116 murieron**; sobreviven **3, equivalentes**: `>` → `>=` en la bisección (igualdad exacta de
  flotantes), 60 en vez de 100 iteraciones (converge a los 53 bits) y `> 0` en vez de `!= 0` sobre un conteo ya validado
  no negativo. El barrido encontró **tres piezas de código redundante que se eliminaron** (el tope `min(total, 1.0)`, el
  `isfinite` de las probabilidades —el rango ya rechaza NaN e infinitos— y el `n_aceptadas >= 1` de `alcanza` —sin
  aceptadas la cota vale 1 y `alpha < 1`—; el atajo `fallos == n` también pasó a redundante cuando la confianza se
  fijó en `>= 0.5`) y **huecos reales de test** (el borde inclusivo `cota == alpha`, el tipo de las probabilidades, una
  confianza extrema donde el punto medio de la bisección redondea a 1.0 y el formato de la huella, que quedó sin valor
  de referencia hasta que el mutante `sort_keys=False` sobrevivió).
- **Límites declarados.** (1) Que un corpus declarado «real» lo sea no se puede verificar: el código solo excluye el
  sintético que conoce. (2) Subir `VERSION_DE_METRICAS` al cambiar una definición es una convención; los valores analíticos
  de `test_native_parallax_metrics.py` fijan las definiciones, pero nada las liga mecánicamente a la versión. (3) **No hay
  derivación de umbrales ni runner del benchmark**: es NP-X0, que además reporta `REVIEW_RATE`. (4) No hay call site de
  producción. (5) Las métricas se calcularon sobre normales sintéticas.

### Actualización de NP-X0a (2026-10-20)

NP-X0a mide **hasta dónde se puede saber la convención del canal G mirando solo los píxeles** (EXP-005, mecánico). No
corrige ni decide nada. Verificado ejecutando en Linux (Python 3.11, numpy 2.4.6); el CI de Windows lo confirma en el
PR.

- **Qué entra.** `native_parallax/experiments/convencion.py` (paquete nuevo `experiments/`: código de medición, sin
  umbrales de calidad ni call site de producción; está en el censo del producto, no importa `research`).
  `leer_convencion(normal, bordes=...)` integra el MISMO archivo con `escala_y = +1` (DirectX) y `-1` (OpenGL) y devuelve
  los dos residuos de integrabilidad (`LecturaDeConvencion`, con `margen_relativo`, `tiene_senal` y `elegida`).
- **El mecanismo, en forma cerrada.** Leída con la convención equivocada, una superficie integrable da un residuo de
  `2m / (a + 2m + b)`, con `m = media|h_xy|` y `a`, `b` las medias de `|h_xx|` y `|h_yy|`: **la detectabilidad es la
  derivada mixta**. El test contrasta la fórmula con el residuo medido en los 15 casos periódicos (tolerancia `1e-12`).
- **Resultado: 7 de 15 casos son detectables.** Los 8 restantes (plano, senos en un solo eje, crestas, separables y
  similares) dan residuos de ~`1e-15` bajo las dos hipótesis: no hay nada que leer. Los detectables dan `>= 0.15` con
  la convención equivocada. La brecha es de más de 10 órdenes de magnitud, y el test enumera la partición.
- **Qué sobrevive a la degradación** (14 lecturas por escenario: 7 casos × archivo DirectX y el mismo con G invertido;
  una realización del ruido, no un promedio):

  | Escenario | Convención acertada | Margen mínimo (detectables) | Margen máximo (no detectables) |
  |---|---|---|---|
  | Limpio | 14/14 | 1.000 | — (abstención por el piso numérico) |
  | Ruido σ = 1/255 | 14/14 | 0.290 | 0.002 |
  | Ruido σ = 2/255 | 14/14 | 0.001 | 0.004 |
  | Ruido σ = 4/255 | 10/14 (71 %) | 0.001 | 0.007 |
  | Cuantización 8 bits, 3 canales | 14/14 | 0.474 | 0.001 |
  | Cuantización 8 bits, 2 canales (tipo BC5) | 12/14 (86 %) | 0.001 | 0.002 |

  El acierto solo no es la métrica correcta: con σ = 2/255 la convención «acierta» 14/14, pero con un margen del mismo
  orden que el de los no detectables, así que **ningún umbral de margen separa una lectura útil de ruido**. El rango
  utilizable medido es σ <= 1/255 y cuantización de tres canales. El criterio de EXP-005 (>= 95 %) se cumple **solo en el
  subconjunto detectable** y solo en esos escenarios; falla con σ = 4/255 y con la cuantización de dos canales.
  Los tests afirman esto de forma cualitativa y con margen holgado, no congelan conteos de casos marginales.
- **Dos ambigüedades que ninguna métrica resuelve** (demostradas en el mismo archivo de tests): (1) la inversión global
  —X e Y a la vez— produce la `-altura` exacta y deja idénticas todas las métricas (es la razón por la que `gates.py` lleva
  el signo esperado); (2) una escala uniforme equivocada no cambia la altura una vez normalizada. Una relación equivocada
  entre las escalas de X e Y sí cambia la forma, salvo que todos los modos de Fourier compartan dirección (senos en un eje,
  onda plana diagonal, crestas: se enumeran).
- **Decisión para los consumidores.** (a) **No hay auto-flip**: la convención la fija la fuente del archivo (cabecera,
  metadatos, herramienta, el operador); esta lectura puede corroborarla, no reemplazarla. (b) Un indicio solo vale con
  abstención: `tiene_senal` falso o margen bajo ⇒ no dice nada. (c) El umbral del margen **no se fija acá**: se deriva
  contra el benchmark en NP-X0b y, como todo umbral de este ADR, solo se valida con texturas reales. (d) Una discrepancia
  entre la fuente y una lectura CON señal es motivo de `REVIEW`; una lectura ambigua nunca autoacepta.
- **El piso numérico lo encontró la medición, no el barrido de mutantes.** La primera versión elegía por el margen
  relativo; al medir cada caso limpio, S04, S10, S11 y S14 (no detectables) daban residuos de ~`1e-15` distintos entre sí y
  un margen de hasta 0.806, es decir, una convención «elegida» por ruido de redondeo. Ahora `tiene_senal` exige que algún
  residuo supere `PISO_NUMERICO = sqrt(eps)` de float64 (definición de «cero numérico», no un umbral de calidad); sin
  señal el margen es 0 y `elegida` es `None`. En todos los escenarios degradados medidos el residuo mayor de cada
  lectura es `>= 3e-3` (el piso vale `1.5e-8`): el piso solo actúa sobre datos exactos y, con datos de 8 bits, manda el
  margen relativo.
- **Mutantes.** 24 definidos sobre `convencion.py` (validación de residuos, margen, piso numérico, elección, escalas y
  bordes de las dos lecturas). **Los 24 mueren.** Un primer barrido (16 mutantes, antes del piso) dejó vivos dos: uno
  equivalente (`total <= 0.0` en lugar de `== 0.0`, imposible porque los residuos se validan `>= 0`; ese código dejó de
  existir con el piso) y **un hueco real de test** (los bordes solo estaban anclados en la lectura OpenGL: fijar
  `Bordes.PERIODICOS` en la DirectX sobrevivía).
- **Límites declarados.** (1) Es el corpus **sintético** de NP-D0: EXP-M2 midió que lo sintético no predice texturas
  authored reales, y la fracción de texturas reales sin derivada mixta se desconoce. (2) El ruido es gaussiano i.i.d. y la
  cuantización es un modelo propio; el ruido de compresión por bloques de BC5/BC7 (y `texconv`, NP-E0) no está simulado.
  (3) Una sola realización del ruido, 14 lecturas por escenario. (4) No hay umbrales, ni runner del benchmark, ni call
  site de producción (NP-X0b).

### Actualización de NP-X0b (2026-10-20)

NP-X0b entrega el **runner del benchmark y la `REVIEW_RATE` simulada**, no umbrales: `UMBRALES_VIGENTES` sigue en `None` y
en el producto toda textura sigue yendo a `REVIEW`. Verificado ejecutando en Linux (Python 3.11, numpy 2.4.6); el CI de
Windows lo confirma en el PR.

- **Qué entra.** `experiments/medicion.py` (numpy perezoso) mide una textura (`medir_textura`: integra con `integrar` y
  calcula las métricas del gate con los MISMOS bordes) y el corpus sintético completo (`medir_corpus_sintetico`: 17 casos ×
  11 escenarios de la especificación de NP-D0 = 187 mediciones a 128²). `experiments/revision.py` (puro, sin numpy) deriva
  límites por cuantil (`derivar_limites`), simula la revisión (`simular_revision`) y la simula sin fuga
  (`simular_dejando_un_caso_fuera`). Sin call site de producción.
- **El corpus se mide solo con bordes periódicos** (Codex, #763). Los controles negativos (N01, N02) lo son porque violan la
  periodicidad: son del integrador periódico. Bajo Neumann son representables y etiquetarlos «malos» corrompería las tasas;
  qué significa «mala» con otros bordes no está decidido, así que `medir_corpus_sintetico` se niega en vez de adivinarlo.
- **La huella del corpus medido es la de la especificación de NP-D0** (Codex, #763). Una huella propia del corpus medido
  habría sido un segundo hash sintético que el guard del gate no conoce (el gate solo rechaza
  `huella_especificacion(ESPECIFICACION_V1)`): reproduje que con ella una validación con conteos favorables daba `alcanza`
  verdadero. El corpus medido es un subconjunto del de NP-D0, así que lleva SU huella y un test ancla que el gate la
  rechaza como validación. Lo que distingue una medición de otra (grilla, bordes, versión de las métricas y de la
  medición, escenarios) va en `CorpusMedido.descripcion`, un JSON canónico con valor de referencia congelado que NO es un
  hash, para que no se preste a usarlo como `huella_del_corpus`.
- **Cada `Medicion` declara los bordes con que se hizo y no se mezclan mediciones de bordes distintos** (pr-agent, #763).
  Un límite derivado (o evaluado) sobre una mezcla de periódicos y Neumann no vale para ninguno de los dos, y el producto
  podría elegir bordes por textura (teselable o no). La regla vive en un solo validador que usan las tres funciones que
  consumen mediciones (`derivar_limites`, `simular_revision` y `simular_dejando_un_caso_fuera`), enumeradas en el test.
  `razon_de_costura` no recibe bordes (`metrics.py` la define sobre el salto de wrap de la altura): mide la costura al
  teselar, sea cual sea el integrador. Además `Umbrales` no guarda los bordes de la derivación, así que
  `simular_revision` exige `bordes_de_los_limites` y rechaza aplicar límites de un dominio a mediciones de otro (Codex,
  #763); `simular_dejando_un_caso_fuera` no lo necesita porque deriva y evalúa sobre las mismas mediciones, homogéneas.
- **La derivación valida el dominio de TODA medición buena** (Codex, #763). Una fila buena con una métrica no finita o
  imposible (un ángulo negativo, un residuo > 1) ocupa un rango de la muestra y mueve el límite sin que nada falle (con un
  ángulo de -5° el límite del cuantil 0.5 pasó de 2.0 a 1.0): es un error, igual que en el gate, donde esa medición va a
  `REVIEW`.
- **No compara límites por su cuenta.** Un candidato sin validación manda toda textura a `REVIEW` en el gate, así que
  «qué haría con estos límites» se lee de los motivos que `decidir_con` informa para la textura, descartando los del
  registro (invalidan la simulación) y los de la validación (evidencia que este código no pretende tener). La partición
  de `Motivo` en registro / validación / textura se congela por igualdad: un motivo nuevo rompe el ancla hasta que se lo
  clasifica.
- **Derivar no es validar.** El candidato derivado nunca trae `validacion`, y un ancla por AST congela que solo `gates.py`
  nombra `ValidacionContraCorpusReal`: ningún módulo de medición puede fabricar esa evidencia con sus propios números.
- **La etiqueta «mala» es declarada** (`criterio_de_etiqueta` la registra en el reporte): un control negativo lo es por
  construcción (NP-D0: violan la periodicidad a propósito), una altura sin rango también, y el resto por el RMSE entre las
  alturas normalizadas min-max contra el ground truth, con una tolerancia SIN valor por defecto. Se usó `0.05`, la cifra de
  EXP-001 de la síntesis; no es un hallazgo sino una elección declarada.
- **Dejar un caso fuera.** Los escenarios de un caso son la misma superficie, así que cada caso se evalúa con límites
  derivados SIN él. Evaluar con límites derivados de los mismos casos subestima la revisión (a 128², 43.3 % → 47.6 % con
  el cuantil 0.95; a 256², 43.3 % → 44.9 %).

**Resultado sobre el corpus sintético** (128², 187 mediciones, 71 malas = 38 % por construcción; límites por cuantil,
estimación sin fuga):

| Cuantil | Revisión (misma muestra → sin el caso) | Malas aceptadas / aceptadas | Buenas en revisión |
|---:|---:|---:|---:|
| 0.90 | 51.3 % → 52.9 % | 2 / 88 (2.3 %) | 30 |
| 0.95 | 43.3 % → 47.6 % | 2 / 98 (2.0 %) | 20 |
| 0.99 | 36.4 % → 40.6 % | 6 / 111 (5.4 %) | 11 |
| 1.00 | 34.8 % → 36.4 % | 6 / 119 (5.0 %) | 3 |

A 256² los valores difieren en pocos puntos (cuantil 0.95: 43.3 % → 44.9 %, 2 malas aceptadas), así que la conclusión no
depende de la grilla.

- **Ningún candidato se acerca al objetivo de NP-H0 (`REVIEW_RATE` < 15 %), ni siquiera en el corpus sintético.** Con
  los cuantiles ≥ 0.95 se manda ~36-48 % a revisión y se dejan pasar ~2-5 % de malas. Esa tasa **no es la del producto**: depende de
  la mezcla del corpus (38 % de malas por construcción, con ruido de hasta 8/255 y dos controles negativos). En los
  escenarios benignos (limpio, 16 bits y tres canales) la revisión baja a 12-24 %, de los cuales los dos controles
  negativos son 12 %. El número de NP-H0 solo se puede contestar con texturas reales.
- **Hay falsos aceptos incluso aquí, y son de la misma clase que EXP-M2 midió en texturas reales.** Con el cuantil 0.95,
  los dos son `S08_ridges` con cuantización de dos canales (BC5-like): error de altura 0.051 y 0.094 con el p95 de acuerdo
  de normal en 0.22° y 0.06° y residuo 0.007 y 0.001. El error está en la altura y las métricas solo-normal no lo ven.
  Con el cuantil ≥ 0.99 se suman `S01_flat` con ruido (un relieve espurio sobre una verdad plana, «mala» por convención) y
  `S09_bricks` con ruido 1/255.
- **El costo de la revisión lo pagan las buenas con ruido.** Con el cuantil 0.95 las 20 buenas en revisión son sobre todo
  de los escenarios con ruido; `residuo_de_integrabilidad` vale 0.2-0.35 en texturas con error de altura pequeño (p. ej.
  `S02_sine_x` con ruido 1/255: residuo 0.27, error 0.0075; no se investigó la causa). Las tres métricas pasan su límite
  con frecuencia comparable (sin el caso, cuantil 0.95: 61, 52 y 40 veces sobre 187 mediciones).

**Dos hallazgos sobre el gate** (ancladas en `tests/test_native_parallax_x0_medicion.py`, sin cambiar el gate):

- **El signo no delata una inversión global cuando la altura sale de la misma normal.** `acuerdo_normal` ajusta la escala
  entre la altura y la normal; si la altura es la `integrar` de esa normal, la escala es `+1` por construcción. Con X e Y
  invertidos a la vez el signo da `+1` en los 14 casos periódicos no planos, y solo da `-1` contra una altura
  independiente (14 de 14). `SIGNO_DISTINTO_DEL_ESPERADO` apareció una sola vez por grilla y nunca por una inversión
  (128²: `S07_bumps` con ruido 8/255; 256²: `N01_ramp` con cuantización de dos canales a 8 bits; no se investigó por qué
  el ajuste da negativo ahí). Consecuencia: el gate no detecta una
  normal con X e Y invertidos; hace falta una fuente independiente de la altura (NP-RIG). Es la misma conclusión de NP-X0a
  vista desde el otro lado: la inversión global no se distingue por los píxeles.
- **`NO_FINITOS_EN_LA_ALTURA` no puede dispararse con este cableado**: `integrar` rechaza normales no finitas y
  `acuerdo_normal` / `razon_de_costura` rechazan alturas no finitas, así que el conteo vale 0 siempre (0 en las 187).
  Se conserva la llamada porque documenta la intención; el mutante que la reemplaza por `0` es equivalente.

**Decisión sobre `REVIEW`.** (1) Ningún candidato se promueve: la `REVIEW_RATE` del producto sigue en 100 % hasta que
exista un registro validado contra un corpus real. (2) La justificación pedida por NP-H0 es esta: con métricas solo-normal
ni el corpus sintético llega al 15 %, y los falsos aceptos de la clase EXP-M2 aparecen aunque el ruido sea gaussiano. (3) El
runner queda listo para el corpus real: `medir_textura` mide cualquier textura que tenga una altura de referencia (MatSynth
trae alturas authored) y el reporte da los conteos (`n_total`, `n_aceptadas`, `n_aceptadas_malas`) que pide
`ValidacionContraCorpusReal`; armar esa evidencia, con la tolerancia, la confianza y la tasa de revisión máxima, sigue
siendo del operador.

**Mutantes.** 117 ejecutados sobre `revision.py` y `medicion.py` (validadores, etiqueta, derivación y su dominio, conteo,
simulación, dejar un caso fuera, escenarios, error de altura, cableado de bordes y descripción del corpus). **111 mueren**
(uno sobrevivió porque el test comparaba el JSON parseado y no su forma canónica; se ancló el texto exacto). Los 6
sobrevivientes: **5 eran código redundante y se eliminaron** (el `isfinite` del cuantil, que el rango ya implica; el
`max(posicion, 1)`; dos condiciones de `Conteo` que la de «malas en revisión» ya implica; y un chequeo previo de los bordes
que `integrar` repetía; este último volvió después con otro propósito, el de arriba, y sus mutantes mueren) y **1 es
equivalente** (el conteo de no finitos, ver arriba). Al escribir los tests
resultó falsa mi premisa de memoria sobre el redondeo del cuantil (`0.7 * 10` es exacto): el contraejemplo real, hallado
ejecutando, es `0.28 * 25 == 7.000000000000001`.

**Límites declarados.** (1) Es el corpus **sintético**: nada de esto valida un umbral (EXP-M2). (2) La etiqueta mide la
altura (más los controles), no la calidad percibida: es un proxy, y la tolerancia de 0.05 es una elección. (3) Ruido
gaussiano, una realización, cuantización modelada y sin compresión por bloques (NP-E0). (4) Solo bordes periódicos (el runner se niega con otros) y
convención DirectX. (5) Las cifras de arriba son de 128² (con el chequeo a 256²); la especificación tiene más grillas.
(6) La tasa de revisión depende de la mezcla del corpus: no es la del producto.

**Reproducir.** `corpus = medir_corpus_sintetico(128, 128, bordes=Bordes.PERIODICOS)`, luego
`simular_dejando_un_caso_fuera(corpus.mediciones, cuantil=0.95, tolerancia_de_altura=0.05, signo_esperado=1,
huella_del_corpus=corpus.huella)` y `derivar_limites` + `candidato_desde_limites` + `simular_revision(...,
bordes_de_los_limites=Bordes.PERIODICOS, ...)` para «la misma muestra». Es determinista; la huella de referencia del corpus medido está congelada en el test.

### Actualización de NP-E0a (2026-10-21)

NP-E0a entrega el **núcleo puro del exportador DDS**, no el exportador: no ejecuta `texconv`, no escribe archivos y no tiene
call site de producción. `TEXCONV_PIN_VIGENTE` sigue en `None` (congelado por igualdad): sin una versión y una huella fijadas por
el operador con evidencia del binario oficial, el exportador no corre nada. Verificado ejecutando en Linux (Python 3.11,
numpy 2.4.6, Pillow 13.0.0); el CI de Windows lo confirma en el PR.

- **Qué entra** (`export.py`, numpy perezoso). (1) `TexconvPin` + `verificar_texconv`: versión, huella SHA-256 y origen (solo
  la sección Releases de `microsoft/DirectXTex`, sin `%` ni `..`: no se decodifica); falla cerrado si no hay pin, si el archivo no es regular (un dispositivo
  cuyo contenido hashea al pin tampoco pasa), si no se puede leer (también si la consulta de la ruta falla, p. ej. una ruta demasiado larga: el llamador solo necesita atrapar `ExportError`) o si la huella difiere en un byte. (2) `generar_mips`:
  promedio de área exacto 2x2, la cadena entera en float64 y cada nivel redondeado UNA vez a float32 (los redondeos de float32 no se acumulan);
  potencia de 2 y lado mínimo 4 (un bloque BC). (3) `cuantizar_a_8_bits`: `rint(x · 255)` con empate al par. (4)
  `inspeccionar_dds_bc4`: valida marca, tamaños de cabecera, BC4 UNORM (`BC4U`, `ATI1` o DX10 con `DXGI_FORMAT_BC4_UNORM`),
  2-D sin cubemap, volumen (incluida la bandera de profundidad de las cabeceras viejas) ni arreglo, conteo de mips coherente con las dimensiones y un payload de tamaño EXACTO.
- **Los tests no confirman al módulo contra sí mismo.** El helper que arma los DDS sigue la disposición pública de Microsoft
  por su cuenta (no reusa código del módulo) y Pillow, un lector independiente, decodifica el nivel base de los tres
  contenedores aceptados. Salvedad: Pillow solo lee el nivel base; el conteo de mips y el tamaño del payload salen de la
  fórmula de la disposición, no de un segundo lector.
- **Una cabecera con varios mips y sin la bandera de mipmaps se rechaza** (decisión mía, más estricta que un lector
  tolerante). No verifiqué cómo la leería DirectXTex; rechazarla es lo único que no puede aceptar algo que otro lector
  interprete distinto.
- **Mutación manual** (114 mutantes sobre `export.py`: operadores, constantes, offsets, ramas y guardas). La primera
  pasada dejó 25 vivos: eran huecos reales de los tests (bool aceptado como dimensión, acumulación en float32 indistinguible
  de float64, redondeo a cada lado del empate, `ndarray` de solo lectura, extensión DX10 truncada, bandera cubemap de la
  extensión DX10, banderas de alto/ancho/FourCC, `os.devnull` hasheando al pin), un helper redundante que eliminé y un
  mutante mal elegido por mí. Con los tests reforzados quedan **0 vivos reales y 2 equivalentes** documentados: `<` contra
  `<=` en el largo mínimo (ambas ramas lanzan la misma excepción) y el tamaño del bloque de lectura del hash (solo cambia la
  velocidad).

**Límites declarados.** (0) El validador rechaza la bandera de profundidad pero NO mira el valor de `dwDepth` cuando la bandera falta: no verifiqué qué escribe `texconv` ahí para una textura 2-D y rechazarlo podría descartar una salida válida. (1) Ningún flag de `texconv` está fijado ni probado: NP-E0b los verifica contra el binario real en
Windows (versión, URL, huella, licencia, ayuda y determinismo se piden en el prompt de insumos). (2) Que lo que devuelva
`texconv` pase este validador no prueba que Skyrim cargue el archivo (NP-RIG). (3) `verificar_texconv` compara la huella del
archivo en ese instante: no impide que alguien lo reemplace después; quien lo ejecute debe hacerlo desde una ubicación que
terceros no puedan escribir. **Requisito para NP-E0b:** verificar inmediatamente antes de lanzar el proceso y ejecutar
el binario desde un directorio que terceros no puedan escribir; la API de E0a no ofrece verificación y uso atómicos
(observación del revisor automático, #771). (4) La hipótesis de que el promedio de área sea el filtro correcto para la altura sigue sin
medirse (EXP-010).

### Actualización de NP-E0b (2026-10-21)

NP-E0b entrega el **ejecutor del `texconv` fijado** (`sky_claw/local/tools/texconv_runner.py`) y fija el pin. Toma una altura
en float, arma la cadena de mips propia, se la da a `texconv` y devuelve los bytes del `*_p.dds` BC4 ya validados. No escribe
en ningún destino (eso es de NP-O0) y no tiene call site de producción ni superficie GUI/agente (NP-A0). Verificado
ejecutando en Linux (Python 3.11, numpy 2.4.6, Pillow 13.0.0) y el CI de Windows lo confirma en el PR. **El binario real no se
ejecutó en el entorno de desarrollo (Linux)**; sí en una PC con Windows: ver «Verificado ejecutando el binario real».

- **Evidencia del pin (`TEXCONV_PIN_VIGENTE`, tag `may2026`).** (1) Informe de la PC con Windows (2026-10-21): release
  «May 7, 2026» publicada 2026-05-08T21:01:12Z (ninguna de las 20 últimas está marcada pre-release), `texconv.exe` x64 de
  966480 bytes, SHA-256 `dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06`, versión que reporta 2026.5.8.1,
  licencia MIT («Copyright (c) Microsoft Corporation.», leída del LICENSE del tag). (2) Descargué aparte, desde esta sesión,
  `https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe`: mismos 966480 bytes y mismo SHA-256, y el
  LICENSE del tag (1141 bytes, MIT). **Las dos vías descargan de GitHub Releases**: coinciden entre sí, pero no prueban la
  autenticidad más allá de la integridad de ese canal; la firma minisign publicada es la del zip de fuentes, no la del exe,
  y no tengo una clave pública verificada, así que la firma NO está verificada. Cambiar de versión es una decisión nueva
  con evidencia nueva y cambia el ancla (`tests/test_native_parallax_export.py`, igualdad literal del pin).
- **Flags verificados ejecutando** (informe de Windows): `texconv -f BC4_UNORM -m 0 -y -o <dir> <archivo.png>` → código 0,
  BC4 con cabecera legacy `BC4U` y cadena completa de mips (256x256: 43832 bytes y 9 niveles; 512x512 con ruido: 174904
  bytes y 10 niveles), **idéntico byte a byte en dos corridas independientes**. Esos son los únicos flags que usa el ejecutor
  (`ARGUMENTOS_BC4`, congelado por igualdad). `texconv -?` ya no existe en esta versión (código 1, «Unknown option»): la
  ayuda es `--help`; el ejecutor no sondea versiones con `-?`.
- **Corrección al diseño de investigación.** El documento de A (§H) mencionaba un `--mipmap-mode` custom para pasar mips
  hechos: esa opción NO existe en `texconv` `may2026` (no figura en las tablas de opciones de su código fuente). La vía
  real es entregar un DDS que ya traiga la cadena de mips.
- **Los mips son los propios (leído en el código fuente del tag; confirmado en parte por la ejecución, ver abajo).** El ejecutor entrega un DDS de luminancia de
  8 bits (`construir_dds_l8`, cabecera legacy que `DirectXTex` carga como `R8_UNORM`) con la cadena de `generar_mips`
  cuantizada, y llama con `-m 0`. En `texconv.cpp` (tag `may2026`): `tMips = (!mipLevels && info.mipLevels > 1) ? info.mipLevels : mipLevels`
  y los mips solo se regeneran si `!tMips || info.mipLevels != tMips`: con `-m 0` y una entrada DDS con mips, se conservan y
  `texconv` solo comprime. Si se regenerara, el filtro por defecto para potencias de 2 es BOX (`DirectXTexMipmaps.cpp`),
  sin curva sRGB para `R8_UNORM`. **BC4 se comprime en CPU**: la GPU solo se usa para BC6H/BC7, así que el resultado no
  depende de la tarjeta de video. Esto asume que el binario release corresponde al código del tag (no lo compilé).
- **Solo corre el binario fijado.** `texconv` se COPIA a un directorio de trabajo privado (`mkdtemp`, 0700 en POSIX) y se verifica
  la huella de la COPIA contra el pin; lo que se ejecuta es esa copia. Cierra la ventana verificar-y-después-ejecutar que
  señaló el revisor automático en #771 (requisito dejado para este PR). Sin pin, o con otra huella, no se ejecuta nada.
- **Toda salida se valida**: exactamente un archivo regular `entrada.dds` bajo un tope calculado de las dimensiones, que
  `inspeccionar_dds_bc4` acepta, con las mismas dimensiones y la cadena completa de mips. El proceso lo supervisa
  `_process.run_capture` (timeout, kill del árbol y reap, Job Object en Windows); el directorio de trabajo se borra en toda
  salida, también al cancelar, y la cancelación espera a que termine el hilo que lo estaba usando antes de borrarlo (hallazgo de
  Codex en #772: un hilo no se interrumpe y en Windows un archivo abierto impide el borrado), con un tope de 30 s para que un hilo
  colgado no cuelgue la cancelación (observación del revisor automático: pasado el tope se registra un aviso y se sigue). Un código de salida distinto de cero —incluidos los negativos con que Windows reporta un
  crash— es un fallo.
- **Ancla de la clase «segundo camino a texconv»** (`tests/test_texconv_invariant.py`): el conjunto de módulos `.py` que nombran
  `texconv` está congelado por igualdad (hoy tres) y, entre ellos, solo `texconv_runner.py` lanza procesos; un camino nuevo
  rompe el test hasta que se decida su receta (lo demostré agregando uno).
- **Verificación hecha.** `tests/test_texconv_runner.py` (borde de proceso reemplazado por un `texconv` falso: argumentos,
  verificación de la copia, errores, salida defectuosa enumerada, limpieza, cancelación), `tests/test_bc4_helper.py` (el
  decodificador BC4 de tests se valida contra Pillow) y ampliaciones de `tests/test_native_parallax_export.py`. **Mutación
  manual**: 75 mutantes sobre el ejecutor y 22 más sobre `export.py` (136 en total), 0 vivos reales; la primera pasada del
  ejecutor dejó 7 vivos que eran huecos reales (rutas relativas, verificar el original en vez de la copia, tope exacto,
  dimensiones transpuestas, códigos negativos) y se cerraron. El ejecutor **nace estricto en el gate de `mypy` de CI**
  (lista de opt-in de `pyproject.toml`, observación del revisor automático: `sky_claw.local.tools.*` está exento); comprobé que el gate
  muerde inyectando un error de tipos, que CI reportó. Ensayo de punta a punta de `tests/test_texconv_real.py` contra un `texconv` SIMULADO por un script (codificador
  ingenuo): prueba la plomería real de procesos, plazo vencido incluido, **no** al binario.

**Verificado ejecutando el binario real** (PC con Windows, commit `177ffd39`, 2026-10-21; Python 3.11.9, numpy 2.4.6, Pillow 13.0.0;
`texconv` 2026.5.8.1 con la huella del pin). `tests/test_texconv_real.py`: **9 de 9**. (1) El binario verifica contra el pin. (2) Con una
entrada DDS L8 de 64x64 con 7 mips, `texconv` imprime `reading ... (64x64,7 R8_UNORM 2D) as (64x64,7 BC4_UNORM 2D)`: **lee los 7 mips**
de la entrada y los trata como `R8_UNORM`. La salida (2872 bytes = 128 de cabecera + 2744 de carga, la suma exacta de 7 niveles BC4) tiene
cabecera legacy `BC4U` sin extensión DX10, banderas `0xA1007`, `mipMapCount` 7 y `caps` `0x401008`; **`dwDepth` vale 1 sin la bandera
`DDSD_DEPTH`**, por eso el validador rechaza la bandera y no el valor (límite (0) de NP-E0a, ahora confirmado con la cabecera real, que
quedó como test de regresión). (3) 256x256 → 43832 bytes y 9 niveles, sin DX10; 4x4 → 152 bytes y 3 niveles; 4x8 → 168 y 4; 64x16 → 840 y 7;
32x128 → 2888 y 8. (4) Dos exportaciones idénticas byte a byte. (5) Cada nivel decodificado sigue la cadena propia: error medio ≤ 2.37 y
máximo ≤ 5.4 en el peor nivel (límites 5.93 y 14.1); el de `texconv` es comparable al de un codificador ingenuo (0.41 contra 0.33 en el
nivel 0, 1.54 contra 1.58 en el 3). (6) Un plazo vencido (2048x2048, 0.02 s) mata el proceso real en Windows, se traduce a
`TexconvTimeoutError` y no deja restos. Los otros tests nuevos corrieron en esa PC: **222 pasaron y 2 se saltaron** (los dos skips son de POSIX).
En la corrida literal fallaron 19 por un artefacto del entorno de esa máquina (un guard de borrado masivo inyectado por `PYTHONPATH` que
lanza `SystemExit` al borrar el directorio de trabajo): sin ese guard, mismo commit y mismo entorno virtual, 222 pasan; no es un defecto del repo.

**Qué NO está verificado.** (1) Que `texconv` CONSERVE los mips de la entrada en vez de regenerarlos con BOX: lo dice su código fuente y el binario
los lee, pero el oráculo no lo distingue (ambas cadenas son promedio de área dentro de la tolerancia) ni importa para el producto; un experimento
decisivo sería entregar mips deliberadamente distintos del promedio y ver si salen intactos (pendiente, junto con la próxima corrida en Windows). (2) La
tolerancia de ese test de mips es PROVISIONAL (el doble del error de un codificador ingenuo más un margen); el dato real quedó por debajo en
todos los niveles. Que NO sea un oráculo ciego está medido y anclado (`tests/test_bc4_helper.py`, corre en CI): sobre el campo del smoke, un
filtro POINT falla desde el nivel 2, una curva sRGB falla en los niveles 4x4, 2x2 y 1x1 (a media escala y bajo contraste casi no se nota antes),
una cadena BOX redondeada a 8 bits por nivel —lo que daría `texconv` si regenerara los mips— pasa, y un bloque corrupto lo detecta el límite
del error máximo; aflojar la tolerancia hasta que uno de esos filtros pase rompe el test (señalado por el revisor automático en #772). (3)
Cuál cabecera (`BC4U` legacy, la que emite hoy `texconv`, o DX10) carga Skyrim es de NP-RIG; el validador acepta ambas. (4) La evidencia
de Windows es sobre `177ffd39`; los commits posteriores del PR tocan solo el manejo de la cancelación del ejecutor y tests. (5) La hipótesis
EXP-010 (promedio de área como filtro correcto de la altura) sigue sin medirse.

**Límites declarados.** (1) El directorio privado hereda los permisos del temporal del usuario: `mkdtemp` es 0700 en POSIX; en Windows
hereda las ACL del padre, no fija ACL propias. (2) El módulo vive en `sky_claw.local.tools.*`, exento de `mypy` y de BLE001 por carpeta;
se lo saca de la exención de `mypy` con el opt-in estricto de `pyproject.toml` y no tiene `except Exception`. (3) No hay registro ni detección de `texconv` en el tool registry (la ruta se pasa explícita) ni
el contrato `success`/`message` (aún no es un tool): se cablea en NP-A0, donde las dos superficies deben pasar por
`exportar_altura_bc4`. (4) `ResultadoDeExportacion` no lleva todavía el `encoder_id` del manifest: NP-O0 arma el manifest con
`texconv_version`, `texconv_sha256` y `argumentos`.

### Actualización de NP-O0a (2026-10-21)

NP-O0a entrega el **núcleo puro de la salida gestionada**, no la salida: `sky_claw/local/native_parallax/output.py` no lee ni escribe
archivos, no toma locks y no tiene call site de producción. Decide tres cosas que el escritor (NP-O0b) y el lote (NP-A0) van a dar
por sentadas. Verificado ejecutando en Linux (Python 3.11); el módulo es solo biblioteca estándar, no importa `numpy`.

- **Quién es dueño de qué.** El **manifiesto es la única prueba de propiedad** (`PRESERVE_EXISTING`). Un archivo que no figura en él
  no es nuestro; uno que figura pero cambió de contenido tampoco: lo tocó un tercero. Ninguno de los dos se pisa. Un directorio, un
  enlace o un dispositivo en la ruta no se toca nunca. El formato es JSON canónico y de **esquema cerrado**: leerlo falla cerrado ante
  un campo de más o de menos, claves repetidas, constantes no finitas, una clave de caché que no coincide con la procedencia o
  cualquier forma que no sea byte a byte la canónica (así lo que se validó es lo que está guardado).
- **Cuándo se saltea un archivo.** `clave_de_cache` es una huella SHA-256 de TODA la procedencia: algoritmo, parámetros, orígenes
  (rol + huella), encoder (identidad de nuestra tubería, versión y huella de `texconv`, argumentos), entorno y nivel de
  reproducibilidad, con el esquema del manifiesto y un dominio propio. **El entorno es obligatorio**: sin él la clave sería portable
  entre máquinas por error, y np.sin/np.exp no garantizan los mismos bits entre CPUs. Una clave que incluya de más solo reconstruye de
  más; una que incluya de menos sirve resultados viejos. El único nivel de reproducibilidad es «determinista en el MISMO entorno»;
  ampliar el vocabulario es una decisión deliberada, no un valor libre.
- **Qué hacer con cada ruta.** `planificar(deseadas, manifiesto_previo, observado)` es una función pura. La tabla estado → acción
  tiene 15 estados y está **enumerada** (producto cartesiano de las cinco dimensiones, comparado por igualdad literal contra una tabla
  escrita a mano):

  | Deseada | En el manifiesto | Qué hay en disco | Acción |
  |---|---|---|---|
  | sí | no | nada | `CONSTRUIR` |
  | sí | no | un archivo | `PRESERVAR` (ajeno) |
  | sí | no | otra cosa | `PRESERVAR` (ocupado por otro tipo) |
  | sí | sí | nada | `CONSTRUIR` (falta en disco) |
  | sí | sí | otra cosa | `PRESERVAR` |
  | sí | sí | archivo intacto, misma clave | `OMITIR` (caché vigente) |
  | sí | sí | archivo intacto, otra clave | `RECONSTRUIR` |
  | sí | sí | archivo modificado | `PRESERVAR` (modificado por terceros) |
  | no | sí | nada | `SOLTAR` |
  | no | sí | otra cosa | `SOLTAR` |
  | no | sí | archivo intacto | `RETIRAR` |
  | no | sí | archivo modificado | `SOLTAR` |

  `PRESERVAR` y `SOLTAR` no tocan el disco: el archivo preservado **no entra** al manifiesto (no reclamamos propiedad) y el soltado
  sale de él. Solo `CONSTRUIR`, `RECONSTRUIR` y `RETIRAR` modifican el disco, y las dos últimas llevan la huella que las autoriza
  (`sha256_autorizado`) para que el escritor la vuelva a comprobar justo antes de tocar. `CONSTRUIR` solo ocurre donde no hay nada.
- **Rutas.** Relativas al mod de salida (`Sky-Claw - Native Parallax Output`), con `/`, solo ASCII imprimible, bajo `textures/` y de la
  forma `*_p.dds`; sin `:` (flujos alternos de NTFS), sin segmentos vacíos o con punto o espacio final (incluidos `.` y `..`), sin
  nombres reservados de Windows (lista conservadora: incluye `COM0`/`LPT0`, `CONIN$` y `CONOUT$`) y de hasta 200 caracteres. Se **rechaza en vez de normalizar**. Se comparan ignorando mayúsculas
  (`casefold`). La lista de nombres reservados sale de la documentación pública de Windows y **no la reverifiqué en esta sesión**; por eso es un superconjunto deliberado (rechazar de más es seguro, aceptar de más hace fallar al escritor después de validar).
  **De dónde sale cada exclusión** (regla de `sky_claw/local/native_parallax/AGENTS.md`: toda exclusión o umbral cita fuente pública o
  experimento propio; ninguna es una heurística medida sobre datos):
  `/` como separador, los caracteres `< > : " | ? * \`, los caracteres de control, los nombres reservados y el punto o espacio final
  salen de la documentación pública de Microsoft «Naming Files, Paths, and Namespaces» (`learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file`),
  que **no reverifiqué en esta sesión**; `:` además abre un flujo de datos alterno en NTFS. `textures/` y `*_p.dds` son la convención
  pública del mapa de altura legacy que Skyrim lee (`CLEAN_ROOM.md`, formato «naming `_p`», y NP-E0 de este ADR). **Solo ASCII imprimible** y
  **200 caracteres** son decisiones de diseño MÍAS, no medidas: ASCII porque la normalización Unicode y el plegado de mayúsculas
  cambian entre sistemas de archivos; 200 es `MAX_PATH` (260, misma documentación) menos un margen de 60 para la raíz del mod de salida,
  un margen que **no medí** sobre rutas reales de MO2 (si resulta chico, es una constante que se sube con su test de borde).
- **Ancla de familia.** Solo `output.py` nombra `NOMBRE_DEL_MOD_DE_SALIDA` y es puro por imports y por llamadas (ambos por AST);
  los campos de `Procedencia`, `EncoderUsado` y `OrigenDeEntrada` se enumeran por introspección contra las variantes que cambian la
  clave: un campo nuevo que no entre a la clave rompe el test. El manifiesto y la clave de referencia están congelados.
- **Mutación manual** (329 mutantes sobre `output.py`: constantes, regex, límites, ramas, guardas y la tabla). La primera pasada dejó
  **90 vivos**, todos huecos reales de los tests o código redundante: topes numéricos sin prueba de borde, la profundidad de listas,
  claves no ASCII, el orden por la clave plegada y no por la ruta cruda, constructores directos del plan y de la entrada, la recursión
  agotada, `bytearray`, el tope de lectura, y capas de defensa que se tapaban entre sí (la comparación canónica byte a byte hacía que la
  clave repetida y el `NaN` parecieran cubiertos). Se cerraron con tests (388 en total) y se borró código redundante (12 mutantes
  dejaron de aplicar). Quedan **3 equivalentes documentados**: `allow_nan` del serializador del manifiesto (inalcanzable: los parámetros
  llegan como texto canónico ya validado), `observacion.sha256` por `entrada.sha256` en la rama «intacto» (son iguales por definición)
  y `entrada and deseada` en `_decidir` (con `entrada` ausente esa rama no lee `deseada`). Los tests y el módulo se escribieron juntos y
  **no observé el rojo previo**: la evidencia de que los tests ven los defectos es esta mutación.

**Límites declarados.** (1) Es un núcleo puro: las huellas de lo que hay en disco las trae el llamador como `Observacion`; entre
observar y tocar hay una ventana (TOCTOU) que NO cierra este módulo. (2) **Requisitos para NP-O0b**: tomar el lock cross-process
(solo protege si TODOS los mutadores participan, ver «Propiedad del `external_work_root`» en `AGENTS.md`), volver a comprobar
`sha256_autorizado` inmediatamente antes de reemplazar o borrar, publicar por etapas y escribir el manifiesto **al final** (es el punto
de compromiso), y recuperarse de un corte a mitad. También debe comprobar el largo de la ruta COMPLETA (raíz del mod de salida + ruta
relativa) antes de publicar: los 200 caracteres de este módulo son un prefiltro de la parte relativa, no garantizan que la ruta final
quepa en `MAX_PATH`. El escritor también decide cómo se crea el mod de salida para MO2 (`meta.ini`).
(3) De dónde salen las salidas deseadas, los orígenes y los valores concretos del `entorno` (versiones de `numpy`, Python,
plataforma) es de NP-A0; el `exportador_id` y `ResultadoDeExportacion` (`texconv_version`, `texconv_sha256`, `argumentos`) se
unen en el `EncoderUsado` ahí. (4) El plegado de mayúsculas con `casefold` aproxima la tabla de Windows solo para ASCII; por eso las rutas
son solo ASCII. (5) No se ejercitó en un disco ni en Windows real: es lógica pura, probada con entradas sintéticas. (6) **Tope del manifiesto**: 128 MiB, el mismo para
leer y para escribir (el serializador falla en vez de producir un manifiesto que el lector rechazaría y que dejaría al lote sin
registro de propiedad ni caché). Con rutas cortas una entrada pesa ≈1,1 KiB (medido), o sea unas 100.000 entradas; un lote mayor se
tiene que dividir. Leer un manifiesto de ese tamaño carga todo el JSON en memoria: el tope es una cota, no una promesa de velocidad.

### Actualización de NP-O0b1 (2026-10-22)

NP-O0b1 es la **parte pura de la recuperación** del escritor, no el escritor: `reconciliar_tras_corte` en
`sky_claw/local/native_parallax/output.py` no lee ni escribe archivos, no toma locks y todavía no tiene call site (lo tiene NP-O0b2).
Se separó del escritor porque es lo único que se puede probar **entera** (60 estados enumerados) sin disco, y porque el escritor
tiene que cumplir el protocolo que esta función da por sentado. Verificado ejecutando en Linux (Python 3.11).

**Protocolo que NP-O0b2 tiene que cumplir** (si no lo cumple, esta función decide sobre supuestos falsos):

1. Todo corre bajo el lock cross-process, **y la recuperación corre primero**, bajo ese mismo lock.
2. Antes de tocar el disco, el escritor guarda su intención como un manifiesto **pendiente** (mismo formato canónico, escritura
   atómica): el manifiesto que quedaría si todo sale bien.
3. Un archivo nuestro que se va a reemplazar o retirar **se aparta primero a una papelera y se verifica después**: se mueve (atómico) y
   se calcula la huella de lo que realmente se movió. Si coincide con la autorizada, se continúa; si no, lo tocó un tercero y se
   devuelve. Así la huella verificada es la del archivo que salió de la ruta, no la de un instante anterior (cierra el TOCTOU de
   contenido que NP-O0a dejaba abierto). El costo es una ventana en la que la ruta está vacía; si el proceso muere ahí, esta función
   decide devolver el original.
4. El archivo apartado y verificado se borra **dentro de la propia acción**, antes del compromiso.
5. El manifiesto definitivo se escribe **al final** (punto de compromiso); después se limpia; el pendiente se borra **último**.
6. Nunca se borra de la papelera nada cuya huella no sea la de una entrada vigente.

**Qué decide** (`reconciliar_tras_corte(previo, pendiente, observado)`, con `observado[clave_de_ruta]` = `EstadoTrasCorte(disco, papelera)`).
Por cada ruta que figure en alguno de los dos manifiestos, 4 relaciones (solo en el pendiente = una creación; solo en el vigente = un
retiro o una suelta; la misma entrada en ambos; una entrada distinta = un reemplazo) × 5 estados de disco (ausente, archivo con los
bytes vigentes, archivo con los bytes pendientes, archivo con otros bytes, otra cosa) × 3 de papelera (nada, un archivo con la huella
vigente, uno con otra huella) = **60 estados**, enumerados y comparados por igualdad literal contra una tabla escrita a mano
(`tests/test_native_parallax_output_recuperacion.py`). En resumen: se **adopta** (pasa al manifiesto) solo lo que ya está en disco con
los bytes que la intención declaraba; un reemplazo que apartó el original y se cortó antes de publicar **devuelve el original**
(`restaurar`); un archivo apartado que es nuestro y ya no hace falta se **descarta**; todo lo que tocó un tercero deja de ser asunto
nuestro (sale del manifiesto) sin tocarse. Una huella en la papelera solo cuenta como nuestra si es la de la entrada **vigente** de esa
ruta. Es idempotente: reconciliar otra vez con el resultado como manifiesto vigente no cambia nada (anclado sobre los 60 estados), así
que un segundo corte durante la recuperación es seguro.

**Mutación** (automática por AST sobre `_entrada_que_queda`, `reconciliar_tras_corte` y las dos clases nuevas: comparaciones, `and`/`or`,
`not`, constantes, condiciones, retornos, llamadas y `raise`): **109 mutantes**, la primera pasada dejó 6 vivos: 4 eran `frozen`/`slots`
de las dos clases (sin prueba; se agregó) y 2 son equivalentes (`return None` por `return None`). Además 6 mutantes escritos a mano
sobre el orden de las ramas, la unión de rutas y la ortografía de la ruta a restaurar: uno vivo (la ortografía que se devuelve en
`restaurar`: tiene que ser la de la entrada vigente, porque en un sistema que distingue mayúsculas es donde estaba el archivo), cerrado
con un test. Los tests y la función se escribieron en este orden: tests → rojo observado (`ImportError` del símbolo nuevo) → función → verde.

**Límites declarados.** (1) Es lógica pura: la papelera y el disco los observa el escritor; esta función no sabe cómo. (2) No cierra
la ventana de la ruta vacía del punto 3, solo la recupera. (3) Un manifiesto pendiente ilegible **no** se interpreta: el escritor tiene
que fallar cerrado y pedir intervención (borrar el pendiente a mano equivale a declarar que no se publicó nada, y lo publicado quedará
como ajeno). (4) No se ejercitó contra un disco ni en Windows real.
