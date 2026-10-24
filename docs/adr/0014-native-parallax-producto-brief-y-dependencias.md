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
   | NP-O0 | `ManagedOutput` + manifest + cache por hash | **parcial**: NP-O0a (núcleo puro: manifiesto, clave de caché y planificador) hecho, ver «Actualización de NP-O0a»; falta el escritor (NP-O0b): NP-O0b1 (reconciliación pura tras un corte) hecho, ver «Actualización de NP-O0b1»; NP-O0b2 (el escritor sobre el disco) hecho, ver «Actualización de NP-O0b2»; falta cablearlo al lote y al adaptador de MO2 (NP-A0) |
   | NP-A0 | `AssetProvider` (carpeta plana y adapter MO2) + batch | **parcial**: NP-A0a (lote de carpeta plana y línea de comandos) hecho, ver «Actualización de NP-A0a»; NP-A0b (`provider.py` endurecido) hecho y cableado a la CLI y al lote, ver «Actualización de NP-A0b» y «Cableado del proveedor a la CLI»; falta el adaptador de MO2, la detección de `texconv` en el registro de tools y las superficies GUI/agente |
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
Se separó del escritor porque es lo único que se puede probar **entera** (75 estados enumerados) sin disco, y porque el escritor
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
6. Nunca se borra de la papelera nada cuya huella no sea la de una entrada vigente, y la huella se **vuelve a calcular justo antes de
   borrar** (`DescarteDePapelera.sha256_autorizado`): borrar es lo único que no se deshace, y entre la observación y el borrado puede
   pasar algo.
7. Lo apartado que **no** es lo autorizado vuelve a su ruta si está vacía (sin pisar nada), en el camino sin corte **y** en la
   recuperación: es una sola propiedad de la papelera, no dos. Si la ruta quedó ocupada, se queda en la papelera y se avisa.
8. **Antes de escribir el pendiente**, el escritor comprueba que `cota_del_manifiesto_tras_un_corte(vigente, pendiente)` no supera
   `TAMANO_MAXIMO_DEL_MANIFIESTO`; si la supera, rechaza el lote sin tocar nada («dividí el lote»).

**Qué decide** (`reconciliar_tras_corte(previo, pendiente, observado)`, con `observado[clave_de_ruta]` = `EstadoTrasCorte(disco, papelera)`).
Por cada ruta que figure en alguno de los dos manifiestos, 5 relaciones (solo en el pendiente = una creación; solo en el vigente = un
retiro o una suelta; la misma entrada en ambos; una entrada distinta = un reemplazo; una entrada distinta con los **mismos bytes** = un
reemplazo que solo cambia la procedencia) × 5 estados de disco (ausente, archivo con los bytes vigentes, archivo con los bytes
pendientes, archivo con otros bytes, otra cosa) × 3 de papelera (nada, un archivo con la huella vigente, uno con otra huella) =
**75 estados**, enumerados y comparados por igualdad literal contra una tabla escrita a mano
(`tests/test_native_parallax_output_recuperacion.py`). En resumen: se **adopta** (pasa al manifiesto) solo lo que ya está en disco con
los bytes que la intención declaraba; un reemplazo que apartó el original y se cortó antes de publicar **devuelve el original**
(`restaurar`; si el original tiene los bytes que declaraba la intención, devolverlo es publicar la pendiente y se adopta esa); un
archivo apartado que es nuestro y ya no hace falta se **descarta** (con la huella que lo autoriza); todo lo que tocó
un tercero deja de ser asunto nuestro (sale del manifiesto) sin borrarse. Una huella en la papelera solo cuenta como nuestra si es la
de la entrada **vigente** de esa ruta; lo apartado que no es nuestro **vuelve a su ruta si está vacía** (el escritor aparta antes de
verificar: si un tercero había editado el archivo y el proceso murió antes de verificarlo, sin esto el archivo del usuario quedaba
encerrado en la papelera) y, si la ruta está ocupada, se queda. Es idempotente: reconciliar otra vez con el resultado como manifiesto
vigente no cambia nada (anclado sobre los 75 estados), así que un segundo corte durante la recuperación es seguro.

**La cota del manifiesto que puede quedar en pie** (`cota_del_manifiesto_tras_un_corte(previo, pendiente)`). Tras un corte, y también
al comprometer un lote con rutas fallidas, queda por ruta a lo sumo **una** entrada de alguno de los dos manifiestos: la unión puede
pesar más que cada uno (un lote que cambia casi todas las rutas). Si superara los 128 MiB, `serializar_manifiesto` fallaría en cada
arranque y la sesión no se podría abrir más. La cota suma, por ruta, lo que agrega la entrada más pesada a un manifiesto vacío; se
verifica contra **todas** las elecciones posibles por ruta (72 sobre cinco rutas que cubren cada relación): nunca se queda corta y
sobra a lo sumo 2 bytes por ruta. Las entradas iguales en ambos manifiestos pesan una vez, así que un lote que reconstruye las
mismas rutas no ve reducido el tope.

**Mutación** (automática por AST sobre `_entrada_que_queda`, `reconciliar_tras_corte`, `cota_del_manifiesto_tras_un_corte` y las tres
clases: comparaciones, `and`/`or`, `not`, aritmética, constantes, condiciones, retornos, llamadas, `raise` y `continue`; las anotaciones
no se mutan porque con `from __future__ import annotations` no se evalúan; cada mutante corre con un "mutante nulo" previo que valida el
empalme). Sobre la versión final: **150 mutantes, 149 muertos y 1 vivo equivalente** (`peso_por_ruta.get(clave, 0)` → `1`: el peso de
una entrada serializada siempre supera 1 byte, así que `max(1, peso) == peso`). La pasada anterior dejó vivos 9 textos de mensajes de
error, cerrados haciendo que los tests de validación comprueben con `match=` qué chequeo se dispara. En la primera versión (antes de la
revisión) se hicieron además 6 mutantes a mano sobre el orden de las ramas, la unión de rutas y la ortografía de la ruta a restaurar:
uno vivo (la ortografía que se devuelve en `restaurar`: tiene que ser la de la entrada vigente, porque en un sistema que distingue
mayúsculas es donde estaba el archivo), cerrado con un test. Orden: tests → rojo observado (`ImportError` del símbolo nuevo; y en la
revisión, 10 fallas de comportamiento con la huella y la cota ya implementadas y la devolución de lo ajeno todavía no) → función → verde.

**Revisión.** La primera versión tenía tres defectos que señaló la revisión automática (Codex) y que se verificaron leyendo el código:
lo ajeno apartado quedaba varado en la papelera si el corte caía entre apartar y verificar (era el camino gemelo de `_devolver`, que
sí lo devuelve sin corte), `descartar` no llevaba la huella que autoriza el borrado, y nada acotaba la unión de los dos manifiestos.
Una segunda revisión (CodeRabbit) encontró que un reemplazo con los mismos bytes y otra procedencia, cortado con el original en la
papelera, se recuperaba con la entrada vigente y el arranque siguiente adoptaba la pendiente: la recuperación no era idempotente. La
tabla de 60 no tenía esa relación y por ese hueco se escapó; entró como quinta relación (75 estados) y el rojo se observó en sus dos
anclas (la tabla y la idempotencia) antes de corregir.

**Límites declarados.** (1) Es lógica pura: la papelera y el disco los observa el escritor; esta función no sabe cómo. (2) No cierra
la ventana de la ruta vacía del punto 3, solo la recupera. (3) Un manifiesto pendiente ilegible **no** se interpreta: el escritor tiene
que fallar cerrado y pedir intervención (borrar el pendiente a mano equivale a declarar que no se publicó nada, y lo publicado quedará
como ajeno). (4) No se ejercitó contra un disco ni en Windows real. (5) La cota serializa cada entrada por separado: es lineal en el
número de entradas (medido en Linux: 2,5 s para 20.000 + 20.000 entradas distintas, contra 1,1 s de serializar el manifiesto), y el
escritor la paga una vez por lote que toca el disco.

### Actualización de NP-O0b2 (2026-10-22)

NP-O0b2 entrega **el escritor**: `sky_claw/local/native_parallax/output_writer.py`, el ÚNICO módulo que toca el mod de salida. No tiene todavía
call site de producción (lo cablea NP-A0 al lote y al adaptador de MO2). Cumple el protocolo de «Actualización de NP-O0b1» punto por punto
y se verificó ejecutando en Linux (Python 3.11) sobre un disco real (`tmp_path`). **No se ejecutó en una máquina Windows desde esta sesión**;
la CI de Windows (py3.11 y py3.12) corrió estos mismos tests en verde sobre `6641fa2` (con los seis arreglos de la revisión de #782), y sobre la
versión final los corre la CI del PR. Esa corrida es la única que ejerce las ramas propias de Windows (`os.rename` como movimiento que no
reemplaza, los junctions). Los tests de junction no tienen `skip` en Windows (fallan si `mklink /J` no funciona), así que un job verde los
ejerció de verdad; lo deduzco leyendo el código de los tests, no el log del job.

**Cómo se usa.** `abrir_sesion_de_salida(raiz, coordinacion=...)` es la única forma de obtener una `SesionDeSalida`. Al entrar toma el lock
de esa raíz, crea la raíz si no existe (su carpeta madre debe existir y el nombre debe ser el del mod de salida) y **recupera** cualquier corte
anterior. `sesion.planificar(deseadas)` observa el disco (sin seguir enlaces) y devuelve el plan de `output.planificar`; `sesion.aplicar(plan,
fuentes)` ejecuta ese plan con los archivos ya generados (`FuenteDeSalida`) de las rutas que hay que construir o reemplazar. Devuelve un
resultado por ruta (`PUBLICADA`, `REEMPLAZADA`, `RETIRADA`, `OMITIDA`, `PRESERVADA`, `SOLTADA`, `CONFLICTO`, `FALLIDA`) y el manifiesto que quedó
comprometido. Un error de entrada o salida en una ruta, o un tercero que cambió el disco entre planificar y aplicar, **no levanta**: queda en
el resultado de esa ruta y no pisa nada. Levanta solo lo que invalida el lote entero (plan que no es el último, fuentes que no coinciden con el
plan, fuente inexistente, vacía, enlace o que no se puede inspeccionar o leer, manifiesto ilegible, o un lote cuyo manifiesto tras un corte
podría no caber en el tope) y
siempre **antes** de tocar el disco.

**Decisiones.**

- **Dónde vive el control.** Dentro del propio mod, en `.skyclaw-output/` (`manifest.json`, `pending.json`, `staging/`, `trash/`): el manifiesto
  viaja con la carpeta si el usuario la copia o la mueve, y queda en el mismo volumen que las texturas (condición para renombrar de forma atómica).
  El costo es una carpeta oculta más en el árbol virtual de MO2; no la lee el juego. **No se crea `meta.ini` ni se habilita el mod en el perfil**:
  no pude verificar el comportamiento de MO2 en esta sesión y es de NP-A0, que es quien integra con MO2.
- **El lock.** Reutiliza `DistributedLockManager`/`SnapshotTransactionLock` del árbol (lease con TTL de 60 s y latido de renovación,
  `assert_owned` antes de cada unidad que escribe). Su base vive en `SystemPaths.runtime_state_dir()/native_parallax_output`, **no relativa al
  `cwd`**, y el recurso se deriva de la raíz **física** (`realpath`): toda ortografía de la misma carpeta, también a través de un enlace o un
  junction en un ancestro (la carpeta de mods de MO2 en otro disco), compite por el mismo recurso; la ruta resuelta es solo la identidad del
  lock, se escribe siempre por la validada. Una segunda sesión recibe `SalidaOcupadaError` (reintentable), no una espera indefinida. Si el
  proceso muere sin soltarlo (por ejemplo, un `kill -9`), la lease vence en ≤ 60 s: un reintento inmediato puede ver «ocupada» ese rato. Una
  excepción o una cancelación DENTRO del proceso, en cambio, sí suelta la lease al salir de la sesión, y el `pending.json` que quedó en disco
  es justamente el registro que la sesión siguiente recupera: no se borra al fallar, porque sería destruir lo único que dice cómo dejar
  consistente un lote a medias.
- **La lease dura lo que dura el hilo.** Todo lo que el escritor corre en un hilo pasa por `_en_hilo`: si cancelan la tarea, espera a que el
  hilo termine antes de dejar seguir la cancelación. `asyncio.to_thread` no detiene el hilo, y sin esto la sesión soltaría el lock con el hilo
  todavía copiando o moviendo archivos. Lo que el lote dejó a medias lo recupera la sesión siguiente.
- **Apartar y verificar.** Reemplazar o retirar un archivo nuestro lo **mueve** a la papelera sin pisar nada y recién ahí compara su huella con
  la autorizada. En Windows el movimiento es `os.rename` (que no reemplaza); en POSIX es un enlace duro seguido de `unlink` (que falla si el
  destino existe), así que un sistema de archivos sin enlaces duros hace fallar esa ruta con un mensaje y no la pisa. Lo apartado que no es
  lo autorizado vuelve a su ruta **en los dos caminos**: sin corte (`_devolver`) y tras un corte (la recuperación ejecuta `restaurar`, que
  desde la revisión de #779 incluye lo ajeno). **Todo** borrado de la papelera (el descarte de la recuperación, el original de un reemplazo,
  con la publicación en el medio, y lo que se retira) calcula la huella en ese instante (`_borrar_si_es_el_autorizado`); si cambió, no se borra
  y se avisa. Si el sistema no deja borrar (un archivo de solo lectura en Windows, un antivirus) no se aborta nada: lo que se retira vuelve a
  su ruta (`FALLIDA`, sigue siendo nuestro), el reemplazo queda publicado con el detalle de que el original quedó en la papelera, y la
  recuperación avisa y sigue (si levantara, con un archivo de solo lectura la sesión no volvería a abrir nunca).
- **Las carpetas de control también están contenidas.** Toda carpeta que el escritor crea dentro del mod, de salida o de control (la carpeta
  de control, `staging/` y `trash/`), pasa por `_crear_carpeta_contenida`, que valida la contención física antes y después de crearla. Al
  abrir la sesión, una `staging/` o `trash/` que sea un enlace o un junction se rechaza (`ContencionFisicaVioladaError`, como la carpeta de
  control); una que se vuelve enlace a mitad de sesión hace fallar la ruta; la carrera de creación (límite 10) puede dejar carpetas vacías afuera,
  pero no archivos; y la limpieza no recorre lo que no sea una carpeta real contenida (`is_symlink()` es falso para un junction).
- **La copia no pasa del tamaño validado.** Si la fuente crece después de validarla, la copia a la etapa corta en el primer bloque que pasa
  el tamaño validado (que ya está acotado por el máximo de 1 GiB) y la ruta da `FALLIDA` («creció mientras se copiaba»).
- **Un solo tope de tamaño por archivo.** `TAMANO_MAXIMO_DEL_ARCHIVO` (1 GiB) vive en `output.py`, junto a `TAMANO_MAXIMO_DEL_MANIFIESTO`; el
  escritor lo importa y la entrada del manifiesto lo usa para validar. Es una cota de cordura propia de NP-O0a, **no** un umbral de calidad ni
  una cifra de una fuente externa. Supuesto de diseño, no medido: una textura de parallax BC7 de 16384×16384 con todos sus mipmaps pesa
  16384² · 4/3 bytes, 1/3 GiB, y 1 GiB deja 3 veces de margen. Un test ancla que el escritor no define ningún tope propio.
- **Lo que queda en la papelera se ve.** La papelera tiene una casilla estable por ruta (así la recuperación encuentra lo apartado sin un
  registro aparte) y lo que queda ahí **nunca se borra solo**: es un original verificado que no se pudo devolver o algo que un tercero cambió.
  Por eso `_avisar_de_sobrantes` avisa por log, con la ruta de la papelera, en **toda** apertura de sesión, con o sin pendiente que
  recuperar. El aviso es un diagnóstico y no puede impedir abrir: si `trash/` no se puede listar (permisos, un antivirus, un error de E/S) se
  avisa de eso por log y la sesión abre igual. Y si una ruta falla porque su casilla sigue ocupada por un resto de otra sesión, el detalle de la ruta dice que es la casilla
  de la papelera, cuál es y que se revise antes de borrarla a mano (la ruta conserva su archivo y su entrada).
- **Un lote a medias inutiliza la sesión.** Un plan se aplica una sola vez. Si el lote se interrumpe después de escribir su intención (una
  excepción o una cancelación), `planificar` y `aplicar` rechazan todo hasta reabrir la sesión, porque solo la recuperación sabe dejar ese disco
  consistente; reintentar el plan viejo compararía contra huellas que ya no valen. Un rechazo que no tocó el disco (la cota) no la inutiliza.
- **El tope, antes de tocar nada.** Antes de escribir el pendiente se calcula `cota_del_manifiesto_tras_un_corte(vigente, pendiente)`: si
  supera los 128 MiB, el lote se rechaza con `SalidaNoEscribibleError` («dividí el lote»). Sin esto, un lote que cambia casi todas las rutas
  podía dejar tras un corte una unión que no se puede serializar, y la sesión no volvía a abrirse.
- **Lo ilegible nunca se toca.** Lo que no se puede inspeccionar o leer se observa como `OTRO` (se preserva). Un manifiesto o pendiente ilegible
  detiene la sesión sin tocar nada: sin él no se sabe qué es nuestro.
- **Ruta completa.** Una ruta cuya longitud absoluta supere 259 caracteres falla esa ruta antes de copiar nada (cumple el requisito de NP-O0a).

**Anclas** (`tests/test_native_parallax_output_writer.py`, enumeran la familia):

- *El lock solo excluye si TODOS los mutadores participan.* Las funciones que tocan el disco se enumeran por AST y se congelan por igualdad
  (todas privadas); `_recuperar` solo se nombra dentro del bloque que sostiene el lock; las cuatro unidades que escriben solo se nombran desde la
  sesión y la recuperación; `SesionDeSalida` solo se construye desde `abrir_sesion_de_salida`; y una traza sobre un lote con las tres acciones
  verifica que **antes de cada unidad que escribe** `assert_owned` acaba de pasar. Solo `output_writer.py` usa el lock en el paquete.
- *Un lock solo excluye si está abierto.* La coordinación no expone el manager por ninguna vía síncrona y su estado no depende del `cwd`.
- *Dos procesos reales* (no dos corrutinas): una segunda sesión en otro intérprete recibe `SalidaOcupadaError` mientras la primera sostiene la
  raíz, y entra cuando se libera.
- *Cortes enumerados, no muestreados.* Se corta el proceso en **cada** llamada a un punto de corte del lote de referencia (12) y, por cada una,
  en cada punto de la recuperación (2); después se comprueba que lo de un tercero sigue intacto, que cada entrada del manifiesto describe un
  archivo que existe con esos bytes, que nada nuestro quedó huérfano y que no quedan pendiente, etapas ni papelera, y que repetir el lote
  converge al mismo estado final que una corrida sin cortes. Los puntos que nombra el código se congelan por igualdad.
- *La papelera tiene una sola propiedad para los dos caminos.* Un tercero edita después de planificar y el proceso muere justo después de
  apartar: se enumeran las dos acciones que apartan (reemplazar y retirar) y en las dos la recuperación devuelve el archivo editado a su
  ruta, fuera del manifiesto. Lo ajeno con la ruta ocupada se queda en la papelera. Todo borrado de la papelera rehace la huella: el helper lo
  nombran exactamente `_recuperar`, `_reemplazar` y `_retirar` (el tercero lo encontró la revisión de #782), el censo de quién llama a
  `os.unlink` está congelado, y los dos borrados con un hueco tienen su test que cambia el archivo en el hueco. Que el borrado falle se
  enumera sobre los tres sitios.
- *Contención de toda carpeta.* `os.makedirs`/`os.mkdir` aparecen solo dentro de `_crear_carpeta_contenida` (que llama, en orden, contención,
  `makedirs` y contención), salvo la raíz y el estado de los locks, exentos con su motivo. Las dos carpetas de control con un enlace al abrir y
  a mitad de sesión; con un junction, en Windows (`tests._symlink_guard.crear_junction`, sin skip).
- *La lease y los hilos.* `asyncio.to_thread` aparece solo dentro de `_en_hilo`; con un hilo bloqueado, cancelar la tarea no suelta la lease
  (una segunda sesión recibe `SalidaOcupadaError`) hasta que el hilo termina, y la sesión siguiente recupera.
- *Un solo lock por carpeta física.* Por un ancestro enlazado (symlink, o junction en Windows) el recurso es el mismo y la segunda sesión no
  entra.
- *El tope en el borde.* Con el tope igual a la cota del (vigente, pendiente) el lote pasa; con uno menos se rechaza sin tocar el disco.
- *Los censos del repo lo cuentan como a cualquier otro.* `construir_coordinacion_de_salida` entra al inventario de construcciones
  productivas de `DistributedLockManager` (`tests/test_m011_lifecycle_migration.py`), que exige `lifecycle=`. `output_writer.py` entra al
  censo de rollback de salida de U-04 (`tests/test_rollback_salida.py`) con un mecanismo propio, `papelera`: no es `snapshot` (toma el lock
  con `target_files=[]`) ni `pendiente`, porque cada ruta se publica entera o vuelve su original, y la que falla queda nombrada en el
  resultado. La clasificación se cruza con el código: figuran como `papelera` exactamente los módulos que ejecutan
  `reconciliar_tras_corte`. Así, un módulo que recuperara la salida sin el lock también rompe el ancla. La unidad del rollback es la
  **ruta**, no el lote: decidir qué hacer con un lote incompleto (`completo` falso) es de NP-A0.

**Mutación** (automática por AST sobre todo el módulo: comparaciones, `and`/`or`, `not`, aritmética, constantes, condiciones, retornos,
llamadas, `raise` y `continue`; las anotaciones no se mutan porque con `from __future__ import annotations` no se evalúan; antes de
mutar, un «mutante nulo» por función valida que el empalme no rompe nada). **569 mutantes.** La primera pasada dejó **168 vivos** y
ninguno resultó ser un defecto del código: eran huecos de los tests, que se cerraron agrupados por **propiedad** y no por mutante. Entre
ellos: el orden de los puntos de corte congelado por igualdad de lista; el pendiente y el compromiso EXACTOS de un lote con las seis
acciones más tres fallos previos (y el mismo fallo por el camino sin disco); no reescribir lo que no cambió; la traza del lock y la pérdida
del lock en los DOS caminos que escriben; las 8 ramas que fallan con una etapa ya copiada (ninguna deja su copia, y cada detalle nombra
la causa); devolver con la ruta re-ocupada y el error al apartar, en reemplazar **y** retirar; la fuente que es un enlace o un directorio
(el test ya lo nombraba y no lo probaba) y la que cambia conservando el tamaño; la contención en los dos sitios que crean carpetas para
escribir (publicar y devolver), con la carpeta que se vuelve enlace antes y después de crearla; y los avisos por log que este ADR
declara contrato. Sobre la versión final: **540 muertos y 29 vivos**, todos clasificados:

- **Equivalentes (24).** `permitir_raiz` y `exigir_existencia` (8): la carpeta madre de una ruta de salida nunca es la raíz, y en esos
  puntos la carpeta ya existe. `O_BINARY` / `getattr(…, 0)` (4): en POSIX `O_WRONLY == 1`. `os.name == "nt"` (2): en POSIX ya es falso,
  y las dos ramas son movimientos que no reemplazan. `follow_symlinks` en `staging/` (1): `or is_symlink()` cubre los dos casos.
  `return None` en lugar de `False` (3): el único uso es `if`/`if not`. `'snapshots'` (1): con `target_files=[]` el
  `FileSnapshotManager` nunca se inicializa ni escribe. El `unlink` previo de la etapa (1): la sesión abre con `staging/` vacía. El aviso
  de sobrantes con la lista vacía (1): la papelera solo sigue existiendo si no está vacía. `_asegurar_lock` con `lock is None` (3):
  inalcanzable, porque la única construcción pasa un lock.
- **Solo observables en Windows (2).** La rama `os.rename` de `_mover_sin_reemplazar` (la ejerce la CI de Windows) y la comprobación de
  contención antes de `rmdir` en `_podar_directorios_vacios`: en POSIX el `rmdir` de un enlace falla igual; en Windows borraría un junction.
- **No observables en un test (3).** `flush`/`fsync` de las escrituras: la durabilidad ante un corte de luz (ver el límite 2).

Los tests que cierran vivos se escribieron **después** del código (el escritor ya existía): para ellos no hay un rojo previo y la
evidencia es la mutación, porque cada uno mata el mutante que lo motivó. Los tests de los arreglos de la revisión de #779 sí tuvieron
su rojo observado antes del cambio.

**Segunda pasada, tras la revisión de #782.** Sobre las 15 funciones y clases que tocaron los seis arreglos: **369 mutantes**, con el
mutante nulo de cada una en verde. Las 343 muertes son todas por un test que falla (ninguna por timeout ni por error interno). La primera
pasada dejó **26 vivos**; **cuatro eran huecos de test** y se cerraron (cada test mata el mutante que lo motivó): la recuperación que sigue
cuando lo que iba a descartar desaparece antes de borrarlo; el aviso por log cuando un reemplazo no puede borrar el original; retirar un
archivo que no se deja borrar cuando la ruta se ocupó (`CONFLICTO`); y que un lote completo deja la sesión usable. Sobre la versión final:
**347 muertos y 22 vivos, todos clasificados**:

- **Equivalentes (20).** `permitir_raiz` por defecto y en `_preparar_padre`/`_restaurar_desde_papelera` (3): ni la carpeta de control, ni
  `staging/`, ni `trash/`, ni la carpeta madre de una ruta (siempre bajo `textures/`) son la raíz. `exigir_existencia` tras `makedirs` y en el
  chequeo de `staging/`/`trash/` (2): la carpeta acaba de crearse, o `lexists` ya dijo que está. `return None` en lugar de `False` (2): el
  único uso es un `if`. `is_file(follow_symlinks=…)` en `staging/` (1): `or is_symlink()` cubre los dos casos. El aviso de sobrantes con la
  lista vacía (1): la papelera vacía se borra antes (al moverlo a su propio helper se cerró con un test: ver la tercera pasada). El `unlink` previo de la etapa (1): la sesión abre con `staging/` vacía.
  `O_BINARY` (2): en POSIX `O_WRONLY == 1`. `_asegurar_lock` con `lock is None` (3): inalcanzable, la única construcción pasa un lock.
  La lectura de la excepción del hilo en `_en_hilo` (4): `asyncio.shield` ya la marca como leída cuando se cancela el externo (verificado
  en el código de 3.11); las dos líneas son defensa por si ese detalle interno cambia. El bucle que espera al hilo **no** tiene vivos.
- **Solo observable en Windows (1).** La comprobación de contención antes de borrar `trash/` vacía en `_limpiar_control`: en POSIX el
  `rmdir` de un enlace falla igual; en Windows borraría el junction (el enlace, no su destino).
- **No observable en un test (1).** `flush` de la copia a la etapa: la durabilidad ante un corte de luz (ver el límite 2).

**Tercera pasada, tras la segunda ronda de revisión de #782.** Sobre las tres funciones que cambió esa ronda (`_apartar`, `_recuperar` y el
helper nuevo `_avisar_de_sobrantes`): con la primera versión del helper, **54 mutantes** y el nulo de cada función en verde. **Murieron 53 y
quedó 1 vivo**, y era un hueco de test: la guarda «hay sobrantes» del aviso no se ejercía, porque `_limpiar_control` ya había quitado la
papelera vacía antes de avisar y el test de la papelera vacía no llegaba a ella. Se cerró con un test donde esa limpieza falla (la papelera
queda ahí, vacía) y no debe avisar nada; mata el mutante que lo motivó (sin rojo previo: el código ya existía). Después la revisión señaló
que listar `trash/` podía abortar la apertura de la sesión (una regresión del propio helper, que antes no listaba nada sin pendiente): se
arregló con un `except OSError` que avisa y sigue, con el rojo observado en los dos caminos que llaman al helper (con y sin pendiente), y se
repitió la pasada sobre la versión final: **56 mutantes, 56 muertos, 0 vivos**.

**Límites declarados.** (1) No se ejecutó en Windows aquí (ver arriba). (2) La durabilidad ante un corte de luz alcanza al contenido de los archivos
(`fsync`), no a los directorios. (3) Entre apartar y publicar la ruta está vacía un instante; si el proceso muere ahí, la recuperación devuelve el
original. (4) En Windows un antivirus o el juego que tengan el archivo abierto hacen fallar esa ruta (`FALLIDA`/`CONFLICTO`); no hay reintento.
(5) Un original verificado que queda en la papelera porque un tercero ocupó la ruta en ese instante **no se borra nunca**: se avisa por log
(en cada apertura de sesión) y en el resultado, y se limpia a mano. (6) Una copia de trabajo por archivo ocupa espacio extra mientras dura la acción. (7) Una sesión atiende una
operación por vez. (8) No hay `meta.ini`, ni habilitación del mod, ni de dónde salen las salidas, orígenes y `entorno`: es NP-A0. (9) Lo que
cambie DENTRO de la papelera después de verificarse no se borra: queda ahí y se avisa por log (y en el resultado, si fue un reemplazo).
(10) La ventana entre validar la contención de una carpeta y usarla queda del lado de quien llama (lo documenta `exigir_contencion_fisica`) y
Windows no tiene `dir_fd` para cerrarla. Son dos casos y no son iguales. Al **crear** una carpeta, un tercero que gane la carrera puede hacer
crear carpetas **vacías** afuera, nunca un archivo, porque la validación posterior frena antes de escribir. Al **mover** (apartar a `trash/`,
publicar desde `staging/`, devolver) no hay validación después del movimiento: si un tercero cambia esa carpeta por un enlace justo entre la
validación y el movimiento, un archivo del mod (el que se iba a mover igual) puede terminar afuera. No pisa nada, porque todos los
movimientos se niegan a reemplazar, y la sesión siguiente no abre con una carpeta de control convertida en enlace. Se asume que quien puede
crear enlaces dentro de la carpeta del mod ya puede mover o borrar esos mismos archivos (es la carpeta del usuario que corre Sky-Claw); si un
despliegue no cumple esa suposición, esto es un riesgo real, y cerrarlo del todo pide operaciones por handle, que Windows no ofrece. (11) `clave_de_ruta` ignora mayúsculas (decisión de NP-O0a: Windows y Skyrim no las
distinguen). En NTFS dos ortografías son el mismo archivo; en un sistema que sí las distingue (Linux no es plataforma de entrega) un cambio
solo de mayúsculas no se reconcilia.

### Actualización de NP-A0a (2026-10-23)

NP-A0a entrega **la primera tubería de punta a punta**: `sky_claw/local/native_parallax/lote.py` (la composición, con el exportador inyectado) y
`sky_claw/local/native_parallax/cli.py` (el borde de línea de comandos: `autoprueba` y `generar`), más la guía de operador
`docs/operations/native_parallax_prueba_en_rig.md`. Existe para que el operador pueda correrla en un rig real con `texconv` de verdad: hasta acá,
todo el producto se había verificado con datos sintéticos y tests. **No agrega matemática**: compone `decode`, `integrate`, `metrics`, `gates`,
el exportador y el escritor. No es NP-A0 completo: ver «Qué falta».

**Decisiones (nuestras, ancladas por `tests/test_native_parallax_lote.py` y `tests/test_native_parallax_cli.py`):**

- **Dos fases.** (1) Se calcula todo sin tocar la salida (leer, integrar, normalizar, medir, decidir, exportar a un directorio de trabajo privado que
  se borra siempre). (2) Se abre UNA sesión del escritor y se planifica y aplica el conjunto. La salida se toca una vez y bajo el lock; un archivo
  que falla en (1) no frena a los demás. La lectura y la medición corren en un hilo (`asyncio.to_thread`) para que el latido del lock no se
  detenga; cancelar el `await` no interrumpe ese hilo (límite ya documentado en `integrate.integrar`), pero ese hilo no toca el directorio de
  trabajo. La escritura de la salida intermedia, que sí lo toca, espera al hilo aunque cancelen (`esperar_hilo_ininterrumpible`): el borrado del
  trabajo no corre contra un archivo abierto.
- **El gate sigue fail-closed y la política es explícita.** Sin umbrales validados todo es `REVIEW`, y una salida en `REVIEW` **no se escribe**
  salvo `escribir_revision=True` (`--incluir-revision`). Sus métricas se miden y se informan igual: son los datos con los que se calibran los
  umbrales. `escribir_revision` y `retirar_no_incluidas` son política y **no entran** a los bytes ni a la clave de caché (un test lo ancla: cambiarlas
  deja la caché vigente). **Esta política es una decisión del operador, no una calibración**: publicar el primer registro de umbrales sigue
  siendo de `gates.py` y no se hizo de pasada.
- **La procedencia es la de lo que corrió.** La huella de la normal se calcula antes y después de decodificar y tienen que coincidir (si no, falla
  esa ruta: «cambió mientras se procesaba»); el `EncoderUsado` sale de lo que DEVOLVIÓ el exportador, no de lo esperado; los parámetros que cambian
  los bytes (`ParametrosDelLote.a_parametros_de_procedencia`, versión de la tubería incluida: `np-a0a.v1`) están congelados por igualdad literal,
  y el entorno registra numpy, Pillow, plataforma y Python. La huella se lee acotada por `TAMANO_MAXIMO_DEL_ARCHIVO` (nunca más de `tope + 1`
  bytes, aunque el origen no termine nunca: un test lo cuenta).
- **Los parámetros viajan iguales.** `bordes`, `escala_x` y `escala_y` llegan idénticos a `integrar`, `acuerdo_normal` y
  `residuo_de_integrabilidad` (un test espía las tres llamadas): mezclarlos mezclaría dos dominios en una misma medición, que es lo que
  `experiments/medicion.py` ya había fijado para el corpus. La convención de la fuente (`directx` u `opengl`) es un parámetro del lote y entra a
  la procedencia: **no hay auto-flip** (NP-X0a); la fija quien conoce el origen del archivo.
- **Se conserva lo anterior.** Una salida de una corrida previa que esta corrida no regenera se declara como deseada con su clave vigente (queda
  como está). `retirar_no_incluidas` retira solo las de texturas que NO figuran en la entrada; una que figura pero falló o quedó en revisión
  conserva su salida anterior. Si no hay nada que escribir ni que retirar, **no se abre la sesión** (ni se crea el mod de salida).
  Retirar actúa sobre lo que NO figura, así que exige una enumeración **completa**: una carpeta que no se pudo leer o una entrada que no se
  pudo inspeccionar aborta la corrida con código 2 (sin ver no se sabe si está). Las omisiones deliberadas (un enlace, un `*_p.dds` autoral al
  lado, una ruta inadmisible, un duplicado) no son errores de lectura y cuentan como «no incluidas».
- **Errores tipados.** `ERRORES_POR_ARCHIVO` enumera lo que hace fallar UNA ruta (los de decodificación, integración, gate, exportación, procedencia,
  origen y `OSError`); un test por AST prohíbe cualquier `except` desnudo o de `Exception`. Lo demás es un bug y sube. Una entrada que viola el
  contrato (dos rutas de salida iguales sin distinguir mayúsculas) se rechaza entera antes de tocar nada (`LoteInvalidoError`).
- **Vocabulario cerrado de estados** (`EstadoDeArchivo`: `ESCRITA`, `EN_REVISION`, `NO_PROCESADA`, `NO_ESCRITA`): un test falla si se agrega uno sin caso.
- **La línea de comandos es un borde delgado.** Los códigos de salida están anclados (0 bien, 1 alguna textura falló, 2 uso o configuración, 3 salida
  ocupada por otra instancia, 130 interrumpido). El test del 3 **encontró un defecto real** al escribirlo: `SalidaOcupadaError` es subclase de
  `SalidaNoEscribibleError` y su `except` estaba detrás, así que una salida ocupada salía como error de configuración (código 2). Orden corregido.
  `autoprueba` fabrica una textura sintética y corre la tubería COMPLETA en un mod temporal; **falla** si lo escrito no es un BC4 válido de las
  dimensiones pedidas o si el manifiesto no describe exactamente esa salida (cada rama tiene su test).
- **Anclas del repo tocadas, con su motivo.** `lote.py` y `cli.py` entran al censo de módulos de producto y al de módulos que nombran `texconv`
  (`tests/test_texconv_invariant.py`): `lote.py` lo nombra solo como dato de la procedencia, `cli.py` por el argumento `--texconv` y para atar el
  exportador al ejecutor fijado; ninguno lanza procesos. Ese test confundía `asyncio.run` con `subprocess.run` (detectaba el nombre `run`): ahora
  distingue `asyncio.run` y sigue contando `subprocess.run` y un `run` pelado (casos en el propio test). La suite completa encontró además tres anclas que los módulos nuevos rompían, todas de la clase «un hermano sin declarar»: `tests/test_borrado_recursivo.py` (`shutil.rmtree` está prohibido: el lote borra su directorio de trabajo con `rmtree_link_aware` y se declara `link-aware` / `no-requiere` `limpiar_readonly`, con su motivo: solo contiene lo que este proceso escribe con `write_bytes`), `tests/test_native_parallax_output.py` (el nombre del mod de salida solo lo conoce `output.py`: la CLI usa la constante y no repite el texto) y `tests/test_path_resolution_service.py` (ninguna construcción `<expr> / "mods"` nueva en producción: la carpeta temporal de la autoprueba no se llama `mods`).
- **Revisión de #783 (Codex, seis hallazgos P2, verificados contra el código; cinco arreglados con su rojo observado).** (1) La raíz de salida se
  valida **antes** de procesar ninguna textura (`validar_raiz_de_salida`, que es la validación del escritor hecha pública; el escritor la repite
  al abrir): un `--mods` que no existe ya no cuesta minutos de un 4K antes de fallar. Está en `procesar_lote` y no en la CLI, para que toda
  superficie futura la tenga. (2) Retirar exige enumeración completa (arriba). (3) La escritura intermedia espera al hilo (arriba). (4) `--informe`
  no puede apuntar dentro del mod de salida ni dentro del árbol `textures` de la entrada (se resuelven los enlaces antes de comparar): se escribe después de
  cerrar la sesión y saltearía el lock, la procedencia y la recuperación. (5) `textures` también se inspecciona con el helper que reconoce
  junctions: en Windows `DirEntry.is_dir(follow_symlinks=False)` lo acepta como carpeta. (6) **No se cambia:** `ResultadoDeExportacion.argumentos`
  omite el `-o` final de `ARGUMENTOS_BC4`. Ese flag solo introduce la ruta temporal, que es lo único que queda afuera; cualquier cambio de los
  flags reales cambia los cinco que sí se registran, y modificar solo el último dejaría la línea sin argumento. Arreglarlo toca el módulo del
  ejecutor (fuera de este PR) y un test congelado por igualdad. Queda anotado para quien lo cambie.
  CodeRabbit, en la misma revisión: (7) **el hermano del directorio de trabajo**: `autoprueba` borraba su carpeta temporal con
  `tempfile.TemporaryDirectory`, que usa `shutil.rmtree` (atraviesa un junction en Windows) y, si un antivirus tenía abierto un archivo recién
  escrito, levantaba `PermissionError` sin que `main` lo atrapara: el operador veía un traceback en lugar de `AUTOPRUEBA OK`/`FALLÓ`. Ahora
  es `mkdtemp` + `rmtree_link_aware` en un `finally` que no lanza y avisa; `cli.py` entra a las tres tablas de
  `tests/test_borrado_recursivo.py` («link-aware», «no-requiere» con su motivo); (8) la guía decía «escribe únicamente» en el mod y también
  deja el informe, la carpeta temporal y el estado del bloqueo: ahora los lista; (9) el `certutil` de la guía usa la ruta completa.
  Segunda ronda de CodeRabbit sobre `cd21cb6`: (10) el informe protegía solo las normales a procesar; una textura omitida (por tener un `*_p.dds`
  autoral al lado) o el propio `*_p.dds` autoral seguían siendo pisables, así que ahora se prohíbe **todo** el árbol `textures` de la entrada
  (más simple que la comparación exacta con las normales, que quedó subsumida); (11) **no se cambia:** con `--retirar-no-incluidas` una carpeta
  sin normales sigue siendo un error de uso. Una `--carpeta` equivocada (vacía) retiraría de una vez TODO lo generado antes; frenar es la
  opción segura y, para limpiar de verdad, está documentado desactivar el mod o borrar su carpeta. Un test lo ancla (nada se retira).
  **Lo encontró la CI de Windows** (primera corrida completa sobre `cd21cb6`; era el único fallo de 14987): (12) el test de la ruta de salida
  inadmisible fabricaba en disco un archivo `mala:rara_n.dds`; en NTFS un `:` en el nombre no crea un archivo con ese nombre sino un flujo de
  datos alternativo de otro, así que la enumeración no veía nada que omitir. La CLI estaba bien; el fixture no podía existir en Windows. Ahora el
  test deja el nombre en disco normal y sustituye `validar_ruta_de_salida` (qué nombre es inadmisible lo decide `output.py`, con sus propios
  tests): verifica la REACCIÓN de la enumeración, que es lo que importa, y es igual en las dos plataformas.
- **`lifecycle=None` en la CLI, a propósito.** Es un proceso aparte, sin el boundary de la app; el `DistributedLockManager` abre su propia
  conexión (semántica previa a M-01). Cuando la GUI y el agente lo cableen (NP-A0), pasan el `lifecycle` de la app. El lock es el mismo
  cross-process por usuario: la CLI y la app se excluyen entre sí sobre la misma salida.

**Verificado ejecutando (Linux, Python 3.11, numpy 2.4.6):** los tests de `lote.py` y `cli.py` con un exportador de prueba; lint, tipos
(`mypy --strict` limpio sobre los dos módulos) y los censos. **Mutación** automática por AST sobre los dos módulos (nulo en verde):
`lote.py` **216 mutantes**, 148 muertos y 68 vivos en la primera pasada (huecos de test: el JSON del informe, los validadores, los bordes de la
lectura acotada y el progreso no estaban anclados por igualdad literal); con los tests de la segunda pasada **215 muertos y 1 vivo equivalente**
(`while True` mutado a `while True`). `cli.py` **221 mutantes**, 156 muertos y 65 vivos en la primera pasada; con los tests de la segunda
**214 muertos y 7 vivos, todos clasificados**: 5 sin efecto observable (2 por `entrada.is_dir`/`is_file` con `follow_symlinks`: el enlace ya se
descartó antes con `link_kind_or_raise`, defensa en profundidad; `print(..., file=None)` escribe en `sys.stdout` igual que el valor por defecto; la
rama `if repetidas` con el conjunto vacío no cambia nada; y la subcarpeta `salida` en la ruta del mod temporal de la autoprueba, que solo necesita
existir dentro de un directorio que se borra) y 2 puramente cosméticos o de E/S (`flush=True` y el prefijo del directorio temporal, que desde la
revisión de #783 mata un test). Rojo observado: el del módulo `lote.py` ausente
(`ImportError`, colectivo) y el del código de salida 3; **para el resto de las propiedades no hubo un rojo individual previo** (los tests de la
CLI se escribieron después del código): su evidencia es la mutación. Las últimas ediciones (borrado del directorio de trabajo con
`rmtree_link_aware` y el renombre de la carpeta temporal de la autoprueba) llegaron después de esa pasada: se repitió sobre
`_borrar_trabajo` y `procesar_lote` (10 mutantes, 10 muertos) y sobre `_autoprueba` (23 mutantes, 21 muertos y los dos vivos ya
clasificados arriba: el prefijo del directorio temporal y la subcarpeta `salida`). Tras la revisión de #783, sobre `_autoprueba` y
`_borrar_temporal`: 26 mutantes, 25 muertos y 1 vivo equivalente (la subcarpeta `salida`).

Los arreglos de la revisión de #783 tuvieron rojo observado en cada propiedad (19 tests nuevos fallaban antes: 15 de la CLI y 4 del lote) y su propia
pasada de mutación: `cli.py` (`descubrir_normales_de_carpeta`, `_no_se_pudo_observar`, `_clave_fisica`, `_exigir_informe_fuera_de_la_salida`,
`_generar`) **81 mutantes, 78 muertos y 3 vivos ya clasificados** (`if repetidas` con el conjunto vacío y los dos `follow_symlinks=True`, sin efecto
porque el enlace se descarta antes; después de la segunda ronda, `_exigir_informe_fuera_de_la_salida`, `_esta_dentro_de`, `_clave_fisica` y
`_generar`: **23 de 23**); `lote.py` (`procesar_lote`, `_fase_de_calculo`) **30 de 30**; `validar_raiz_de_salida` **28 de 28**.

**No verificado, por orden de importancia:** (1) **nada se ejecutó con el `texconv` real ni en Windows ni sobre NTFS desde el desarrollo**: la
CI de Windows ejerce los tests con el doble, y `autoprueba` es la primera corrida real *(hecha el 2026-10-23: ver «Primera corrida en un rig real»)*; (2) cómo muestra MO2 el mod de salida (no hay `meta.ini`
ni se habilita solo); (3) que los parámetros por defecto (DirectX, escalas 1, bordes periódicos, min-max) den un relieve correcto en Skyrim:
son los de la referencia sintética y nada se miró en el juego; (4) el comportamiento con texturas de 4096² (costo medido una vez en
`integrar`, no en esta tubería).

**Límites declarados.** (1) Se recalcula todo aunque la caché esté vigente (el escritor la deja `OMITIDA`); evitarlo exige planificar antes de
calcular. (2) De a una textura, sin concurrencia. (3) Los lados tienen que ser potencia de 2 (lo exige el exportador): los demás fallan su ruta con
ese motivo. (4) *(Superado por «Cableado del proveedor a la CLI».)* La enumeración de `cli.py` era **provisoria**: solo `*_n.dds` bajo `textures/`, sin seguir enlaces, y omitía (y decía) las que ya
tenían un `*_p.dds` al lado; la reemplazó `provider.py`. Con `--retirar-no-incluidas` esas omisiones deliberadas cuentan como «no incluidas». (5) No hay superficie GUI ni agente: la regla «dos superficies, un recurso» queda
satisfecha por el lock cross-process, pero las superficies son de NP-A0. (6) `escribir_revision` escribe salidas marcadas «sin validar» solo en
el informe: el manifiesto no distingue una salida autoaceptada de una en revisión.

**Qué falta de NP-A0.** (El `provider.py` endurecido se entregó después: ver «Actualización de NP-A0b».) El adaptador de MO2 (`meta.ini`, habilitar el mod), la detección y el diagnóstico de `texconv`
en el registro de tools, las superficies GUI y agente (con el `lifecycle` de la app), qué hacer con un lote incompleto, y saltear el cálculo de
lo que la caché ya cubre.

### Primera corrida en un rig real (2026-10-23)

**Quién la ejecutó y qué es esta evidencia.** La corrió el agente asistente en la PC del propietario (Windows, con su MO2 de 91 mods), siguiendo la guía de
operador de NP-A0a. **El Tech Lead no la ejecutó ni vio los archivos**: lo que sigue es lo que mostraron la consola y `informe_parallax.json`, pegados por el
propietario. Los originales (`log_autoprueba.txt`, `log_generar.txt`, `informe_parallax.json`) están en su PC y no en el repo. El `texconv.exe` se bajó de la URL
del pin (`TEXCONV_PIN_VIGENTE.origen`), pesó 966 480 bytes (el tamaño que declara el pin) y su huella SHA-256 coincidió con `dcfdec10…` por dos vías.

**Qué se verificó, ejecutando (reportado):** (1) `autoprueba` terminó en `AUTOPRUEBA OK`: textura sintética de 128², `texconv` real, escritor y disco NTFS reales, BC4
válido del lado esperado y manifiesto que describe la salida. (2) `generar --incluir-revision` sobre 8 `*_n.dds` reales (2048² y 1024²): **8 de 8 `ESCRITA`/`PUBLICADA`**,
código de salida 0, cada `*_p.dds` es un BC4 del mismo lado que su normal, el manifiesto está en `.skyclaw-output/` y el informe registra `texconv may2026` con la
huella del pin. `signo = +1` y `no_finitos = 0` en las ocho; el gate mandó las ocho a `REVIEW (SIN_UMBRALES)`, como debe sin umbrales validados. Esto cierra, para
esas 8 texturas, el «nada se ejecutó con el `texconv` real ni sobre NTFS» de «Actualización de NP-A0a».

| textura (en la normal) | acuerdo p95 (°) | residuo de integrabilidad | razón de costura |
|---|---|---|---|
| `mrkmetala01_n` (metal de Markarth) | 5,36 | 0,279 | 1,332 |
| `farmhouse01_n` (madera de granjas) | 7,54 | 0,235 | 1,479 |
| `stoneedgetrim01_n` (piedra) | 10,21 | 0,163 | 1,009 |
| `AspenBark_n` (corteza) | 21,51 | 0,321 | 1,037 |
| `blackreachsun01_n` (clutter de Blackreach) | 47,11 | 0,453 | 0,811 |
| `dwarvenspidercenturion_n` (orgánico) | 49,22 | 0,410 | 0,981 |
| `fallforestrocks01_n` (roca del Bosque Otoñal) | 65,25 | 0,433 | 1,029 |
| `fallforestdirt01_n` (tierra del Bosque Otoñal) | 65,39 | 0,430 | 1,022 |

**Cómo leerla, y qué NO concluir.**
- **n = 8 y ningún veredicto visual.** Nada se miró en el juego: no se sabe si el relieve es correcto con los parámetros por defecto (DirectX, escalas 1, bordes
  periódicos, min-max).
- **La tabla no es un veredicto de calidad ni calibra umbrales.** Las métricas de solo-normal NO predijeron el riesgo en texturas reales en EXP-M2 (mejor AUC 0,62; ver el
  docstring de `metrics.py`). Lo que sí se ve es coherente con su definición: el acuerdo empeora cuando el residuo de integrabilidad sube (0,16–0,32 ⇒ 5–22°; 0,41–0,45 ⇒
  47–65°), es decir, las normales que menos se parecen a un gradiente de una altura. Los umbrales se derivan de un corpus con veredicto, nunca de 8 muestras (NP-Q0b/NP-X0).
- **La coincidencia con Skyland es una hipótesis sin sostener.** Las dos peores (65°) tenían un `*_p.dds` autoral en `Skyland AIO Complex Parallax`, pero las otras dos «malas»
  (47–49°) no lo tenían: son 2 casos de 4 y la herramienta nunca comparó contra la altura de Skyland (el acuerdo compara la normal de entrada con la que sale de la altura
  integrada). Se puede probar fuera del repo comparando nuestra salida con la de Skyland en esas dos rutas; no se hizo.
- **Costura:** `farmhouse01` (1,479) y `mrkmetala01` (1,332) superan 1 con bordes periódicos; son las candidatas a probar con `--bordes neumann`.

**Lo que la corrida mostró del proceso** (cada punto es una corrección de la guía, ya aplicada en el mismo PR, o un requisito de diseño):
(a) el generador no estaba en el `main` local del propietario (327 commits atrás): la guía ahora pide partir del código actual de `origin/main`; (b) `uv sync --extra dev` no hizo
falta porque `numpy` y `Pillow` ya estaban: la guía lo dice como «solo si faltan»; (c) no había `texconv.exe` en la máquina: la guía ahora dice de dónde bajarlo y cómo
comprobarlo; (d) el entorno del propietario tiene un `.pth` con una ruta muerta (`C:\Worktrees\Sky-Claw-586c`): `sky_claw` se resolvió desde el directorio actual, así que hay
que correr los comandos desde la carpeta del repo; (e) **requisito de diseño para el adaptador de MO2 (NP-A0):** la herramienta solo ve los `*_p.dds` de la carpeta de entrada y no los
de otros mods que MO2 resuelve por su sistema virtual de archivos; el orden de los mods decide qué `*_p.dds` gana, y el adaptador tiene que consultarlo antes de proponer una salida.

**Qué sigue.** El veredicto visual del propietario, textura por textura, con el parámetro que corresponda al síntoma (relieve invertido ⇒ escalas −1; canal verde invertido ⇒
`--convencion opengl`; costura ⇒ `--bordes neumann`; plano o con picos ⇒ percentiles); más texturas y un corpus con veredicto antes de proponer cualquier umbral.

### Actualización de NP-A0b (2026-10-23)

NP-A0b entrega `sky_claw/local/native_parallax/provider.py`: el descubrimiento de normales de una carpeta (o de un mod) con una subcarpeta
`textures/`, **endurecido**, para reemplazar a la enumeración provisoria de `cli.py`. Es un módulo **puro y de solo lectura**: dada una raíz,
devuelve por cada `*_n.dds` apto su ruta de origen, su ruta de salida ya validada, el archivo, el tamaño y la huella del contenido (los tres
primeros son lo que consume `lote.NormalAProcesar`) y, aparte, qué omitió y por qué. Al abrirse #784 **no estaba cableado**: la CLI seguía con su enumeración
y sus tests, que eran el contrato de comportamiento que este módulo igualaba o mejoraba; el cableado es la sección siguiente («Cableado del proveedor a la CLI»).

**Decisiones (nuestras, ancladas por `tests/test_native_parallax_provider.py`):**

- **Vocabulario cerrado y clasificado.** `MotivoDeOmision` (`YA_TIENE_PARALLAX`, `ENLACE`, `RUTA_INVALIDA`, `ARCHIVO_VACIO`, `DEMASIADO_GRANDE`,
  `ILEGIBLE`, `CARPETA_ILEGIBLE`, `DUPLICADA_POR_MAYUSCULAS`) tiene un caso por miembro (un test falla si se agrega uno sin caso) y
  `MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO` clasifica cuáles dejan algo sin ver; el vocabulario y la clasificación están congelados por igualdad literal.
- **Lo no observado no se da por ausente** (la lección de #783). `Descubrimiento.completo` es `False` si hubo una carpeta que no se pudo listar o un
  archivo que no se pudo inspeccionar ni leer. Es una propiedad nueva respecto del brief original (`CARPETA_ILEGIBLE` y `completo` no estaban): quien
  retira salidas por «ya no figura» necesita una enumeración completa. Las omisiones deliberadas (enlace, un `*_p.dds` autoral, ruta inadmisible,
  vacío, demasiado grande, duplicado) no la afectan, y una entrada borrada a mitad del recorrido está ausente, no «no vista».
- **Nunca sigue enlaces, pero acepta los ancestros de la raíz.** Un symlink o junction dentro del árbol se omite (`ENLACE`) sin entrar a su
  destino; una raíz o una `textures` que sea un enlace se rechaza (`RaizInvalidaError`). Todo se inspecciona con
  `link_kind_and_identity_or_raise` (que lee el `reparse tag`: en Windows `DirEntry.is_dir(follow_symlinks=False)` acepta un junction) y cada carpeta,
  **la raíz incluida**, se revalida después de abrirla, como `iter_archivos_propios`. **Se aparta del brief en un punto:** el brief pedía que «la raíz y cada ancestro»
  fueran reales; los ancestros los elige el operador (MO2 puede vivir bajo un junction en otro disco, y `validar_raiz_de_salida` del escritor ya los
  acepta), a diferencia de lo de adentro, que puede ser ajeno. Un test lo ancla con symlink y con junction.
- **El autoral no se pisa, y se compara como la salida.** Una normal se omite (`YA_TIENE_PARALLAX`) si en **cualquier** carpeta del árbol hay una
  `*_p.dds` con su misma `clave_de_ruta` (sin distinguir mayúsculas, como resuelven el destino Windows y Skyrim, y **de cualquier tipo de entrada**: una carpeta o un
  enlace llamado así también cuenta, por fail-closed, porque puede ser el autoral de alguien; un test lo fija), y no solo en su carpeta física:
  `Muros/x_n.dds` y `muros/x_p.dds` son el mismo destino aunque un sistema que distingue mayúsculas los guarde aparte. Se decide **después** del
  recorrido (el orden en que se visitan las carpetas no cuenta) y es el mismo criterio con el que ya se detectan los duplicados. Consecuencia
  menor: una normal vacía, demasiado grande o de ruta inválida que además tenga su `_p` al lado se informa por ese motivo y no por `YA_TIENE_PARALLAX`
  (se omite igual).
- **No reutiliza `iter_archivos_propios`, y dice por qué.** Ese generador omite los enlaces en silencio y aborta todo el recorrido ante una carpeta
  ilegible; el proveedor necesita un motivo por cada omisión y tolerar una carpeta que falla. Conserva lo que sí le aporta: la revalidación tras abrir.
- **Huella acotada por el tamaño validado, y solo de lo que sobrevive.** Se lee en bloques y nunca más de un byte pasado el tamaño con el que se
  validó el archivo (un origen que no termina nunca se corta: un test cuenta los bytes pedidos); si el archivo crece o se achica mientras se lee,
  queda `ILEGIBLE`. Se hashea después de los filtros y de la detección de duplicados, en orden: hashear lo que se va a omitir sería leerlo, hasta
  1 GiB, para nada (un test registra qué se leyó).
- **Un archivo que falla no aborta; un exceso sí.** El tope (`maximo_de_archivos`, 100 000 por defecto, cota de cordura propia) cuenta normales
  candidatas y omisiones juntas, porque las dos ocupan memoria, y se rechaza con `DemasiadosArchivosError` en vez de truncar. Un límite que no es un
  entero positivo se rechaza antes de mirar el disco.
- **Determinista y compatible con el lote.** Normales y omisiones van ordenadas por `clave_de_ruta` (no por el orden del sistema): el mismo árbol
  creado en otro orden da la misma salida. `NormalDescubierta` cumple `lote.NormalAProcesar` (un test lo comprueba por atributos).
- **Solo lectura y sin política, por AST.** Los imports están congelados por igualdad (biblioteca estándar, `output.py` y `links.py`: ni numpy,
  ni Pillow, ni nada que sepa del exportador, del gate, del escritor o de MO2); las únicas llamadas a `os` y la única apertura (`open(..., "rb")`) están
  enumeradas, y ninguna operación de escritura (`write*`, `unlink`, `mkdir`, `rename`, `replace`…) puede aparecer. Un test aparte toma una foto del
  árbol antes y después y no encuentra diferencias.
- **Anclas del repo tocadas, con su motivo.** `provider.py` entra al censo de módulos de producto (`tests/test_native_parallax_producto_invariant.py`).
  No nombra el binario del exportador, así que `tests/test_texconv_invariant.py` no cambia, y no borra nada, así que las tablas de
  `tests/test_borrado_recursivo.py` tampoco.

**Verificado ejecutando (Linux, Python 3.11):** 77 tests del módulo (73 pasan y 4 se saltan: los junctions solo existen en Windows), lint, `mypy --strict`, los
censos del repo y la suite completa en la línea base. **Mutación** automática por AST sobre todo el módulo (el nulo de cada función en verde):
**153 mutantes, 152 muertos y 1 vivo equivalente** (`while True` mutado a `while True`, como en `lote._huella_acotada`). La primera pasada dejó
8 vivos: 7 eran huecos de test y se cerraron (el tope mínimo de 1, una `textures` que no se puede inspeccionar y una que desaparece, una entrada
borrada entre el listado y su inspección, y el texto del motivo de un archivo vacío) y el octavo es el equivalente. Rojo observado: el del módulo ausente (`ImportError`,
colectivo); **para el resto de las propiedades no hubo un rojo individual previo** (los tests se escribieron antes que el código, pero con el módulo
inexistente todos fallan por lo mismo): su evidencia es la mutación.

**Tras la revisión de #784 (ejecutado):** 80 tests del módulo (76 pasan y 4 se saltan), lint, `mypy --strict` sobre `provider.py` y la suite completa
corrida sola: 14579 pasan y 402 se saltan, con el mismo conjunto de fallos que la línea base (77 y 1 error de colección, de `runtime_vault`, `test_process_helper` y
`test_schemas`) más uno que puse yo al lanzar la suite con `SKYCLAW_WORKTREE_ROLE` definida pero vacía (el test de worktrees la lee como «rol desconocido» y,
sin definirla, pasa). Rojo observado en los dos hallazgos reales (la raíz que cambia al abrirse: `DID NOT RAISE`; el autoral entre carpetas que solo difieren
en mayúsculas: la normal se procesaba). **Mutación** repetida sobre las funciones que cambiaron (`_resolver_textures`, `_recorrer`, `_examinar`,
`_sin_parallax_autoral`, `_Acumulador`; el nulo de cada una en verde): **99 mutantes, 98 muertos y 1 vivo equivalente** (`if True` en vez de
`if nombre.endswith('_p.dds')` al juntar las claves del autoral: una clave que no termina en `_p.dds` nunca coincide con una ruta de salida, así que solo
cambia cuántas claves se guardan).

**No verificado, por orden de importancia:** (1) **nada se ejecutó sobre NTFS ni en Windows reales desde el desarrollo**: la CI de Windows corre los
tests, incluidos los de junction (con `mklink /J`, sin `skip`); (2) el costo con carpetas muy grandes (100 000 entradas, archivos de cientos de MiB):
la huella lee todo lo que después procesa el lote; (3) que `st_ino` y `st_dev` sean estables en todos los sistemas de archivos de Windows para la
revalidación tras abrir (`same_file_identity` los exige positivos; un sistema que no los dé hará que cada carpeta quede `CARPETA_ILEGIBLE`, que falla
cerrado).

**Límites declarados.** (1) Entre el `lstat` de un archivo y su lectura hay una ventana en la que podría reemplazarse por un enlace (`open` sigue
enlaces): es de solo lectura (a lo sumo se hashea el destino) y el lote vuelve a inspeccionar el origen, antes y después de decodificarlo; no se
usa `O_NOFOLLOW` porque no existe en Windows. (2) La función es síncrona y puede tardar: se la corre con `asyncio.to_thread`. (3) *(Superado: la lectura acotada se unificó al cablear.)* La lectura acotada
estaba duplicada con `lote._huella_acotada`; el cableado las unifica con un ancla que enumere. (4) Los nombres que Windows no admite (`:`, `|`…) no se
fabrican en disco en los tests (en NTFS un `:` crea un flujo alternativo): la reacción a una ruta inválida se prueba sustituyendo el validador, y el
validador real, con un nombre no ASCII. (5) Una carpeta (o un ancestro) que otro proceso reemplace por un enlace **después** de revalidarla, mientras
se inspecciona lo que hay adentro o antes de que se calcule la huella (que va tras el recorrido completo), no se detecta: las llamadas van por ruta y no
por descriptor, y en Windows la biblioteca estándar no permite atar los hijos a la carpeta abierta (no hay `dir_fd`); revalidar al terminar cada carpeta
solo achicaría la ventana. Exige otro proceso con permiso de escritura sobre la carpeta del operador y el efecto es de solo lectura. (6) Un archivo
regular reemplazado por un FIFO entre su inspección y su lectura bloquearía el `open` (solo POSIX); lo que ya es un FIFO al recorrer se omite como
`ILEGIBLE`.

**Revisión de #784 (Codex y CodeRabbit), con el veredicto de cada hallazgo.** (a) *Codex, la raíz no se revalida tras abrirla:* **real**, un hermano en
el mismo archivo (el docstring prometía «cada carpeta» y la raíz era la excepción); ahora se lista con la misma `_listar` y se rechaza con
`RaizInvalidaError`. (b) *Codex, el autoral entre carpetas que solo difieren en mayúsculas:* **real**, el criterio de la salida es `clave_de_ruta` y el
del autoral era la carpeta física; ahora es global (ver arriba). (c) *Codex, atar los hijos a la carpeta validada:* **no se corrige y queda como límite
declarado (5)**: cerrarlo pide `dir_fd`, que Windows no tiene, y lo que se puede hacer (revalidar al terminar cada carpeta) no lo cierra, solo lo
achica, a cambio de código que sugeriría una garantía que no existe. (d) *CodeRabbit, la lista «Qué falta de NP-A0» seguía nombrando a `provider.py`:*
**real**, corregido. (e) *CodeRabbit, un FIFO entre el `lstat` y el `open`:* **no se corrige y queda como límite declarado (6)**: lo que ya es un FIFO
se omite antes de abrirlo; la carrera exige un proceso con permiso de escritura que lo coloque, no existe en Windows (la plataforma de entrega) y la
solución propuesta (`O_NONBLOCK`/`O_NOFOLLOW` y `fstat`) es de POSIX, cambia las anclas por AST y no la pude verificar en Windows.

**Qué falta.** (Cablear la CLI y unificar la lectura acotada se hizo en la sección siguiente.) El adaptador de MO2; la detección y el
diagnóstico de `texconv`; las superficies GUI y agente.

### Cableado del proveedor a la CLI (NP-A0b, 2026-10-23)

La CLI deja de tener su propia enumeración: `cli.descubrir_normales_de_carpeta` es ahora un **adaptador delgado** de
`provider.descubrir_normales` (traduce `RaizInvalidaError`/`DemasiadosArchivosError` a `UsoIncorrectoError`, arma una línea de texto por omisión y, con
`exigir_completa`, rechaza una enumeración con `completo=False`: es lo que usa `--retirar-no-incluidas`). Se conserva con su nombre y su firma **a
propósito**: sus tests (la sección «Enumeración» de `tests/test_native_parallax_cli.py`) fueron el contrato que el proveedor tenía que igualar o mejorar, y
siguen corriendo. Lo que cambia:

- **Se borró la enumeración provisoria** (`_hijo_sin_distinguir_mayusculas`, `_no_se_pudo_observar` y el recorrido de `cli.py`). La CLI conserva su propio tope de
  entradas (10 000, más bajo que el 100 000 del proveedor: es una herramienta experimental para probar en un rig).
- **El informe no puede apuntar dentro de las texturas de entrada** se decide con `provider.carpeta_de_textures`, que es LA misma resolución de `textures` que
  usa el descubrimiento (antes eran dos búsquedas con su propia idea de mayúsculas y enlaces).
- **Una sola lectura acotada.** `lote._huella_acotada` valida el origen (regular, entre 1 byte y el tope) y **delega la lectura** en `provider.huella_acotada`,
  que ahora es pública. Es MÁS estricta que la copia que tenía el lote: se acota por el tamaño de CADA archivo (lo que midió `lstat`) y no por el tope de 1 GiB,
  y un origen que se **achica** entre la medida y la lectura también es `OrigenInvalidoError` (antes solo se cortaba lo que crecía).
- **Anclas nuevas** (`tests/test_native_parallax_producto_invariant.py`, por AST, con su escáner probado sobre una tabla de formas): (1) solo `provider.py` y
  `output_writer.py` enumeran carpetas (el escritor, las de CONTROL del mod de salida): una segunda enumeración de la entrada rompe el ancla; (2) los lectores de
  archivo con huella son exactamente cuatro (`export.verificar_texconv`, las dos del escritor y `provider.huella_acotada`); el lote ya no tiene el suyo; (3) el lote
  importa `huella_acotada` del proveedor.

**Diferencias de comportamiento respecto de la enumeración provisoria** (cada una con su test adaptado, ninguna silenciosa): (a) los duplicados por mayúsculas se
informan **una vez por archivo** (antes una línea por par); (b) un `*_p.dds` autoral cuenta si está en **cualquier** carpeta del árbol con la misma `clave_de_ruta`
y **de cualquier tipo de entrada** (antes, solo en la misma carpeta física); (c) la raíz y cada carpeta se revalidan después de abrirlas y los ancestros de la raíz
se aceptan; (d) los mensajes de `textures` distinguen «no es una carpeta», «es un enlace» y «falta»; (e) el tope cuenta normales **y** omisiones, con otro mensaje;
(f) el texto de las omisiones usa «enlace (symlink|junction)» y paréntesis para el detalle, como antes. La CLI ya **no** dice «debe tener una subcarpeta
'textures'» cuando `textures` es un archivo o un enlace: dice cuál de los dos casos es.

**Verificado ejecutando (Linux, Python 3.11):** rojo observado antes del cambio en las anclas nuevas (`cli.py` aparecía como segundo enumerador; el lote y el
proveedor aparecían como dos lectores con huella; `huella_acotada` no era pública), en los tests del proveedor y del lote (la lectura delegada y el origen que se achica),
y, después de cablear, las **diez** diferencias de contrato de la CLI fallaron una por una y se adaptaron a propósito (ver la lista de arriba). Suite completa corrida sola:
14 613 pasan y 402 se saltan, con el **mismo** conjunto de fallos que la línea base de `main` (77 y 1 error de colección, de `runtime_vault`, `test_process_helper` y `test_schemas`);
`ruff` limpio, `mypy sky_claw/ scripts/` en 669 errores como la línea base y ninguno en `native_parallax/`. **Mutación** por AST sobre lo que cambió (nulo de cada función en verde):
`cli` (`descubrir_normales_de_carpeta`, `_texto_de_omision`, `_exigir_informe_fuera_de_la_salida`) 18 mutantes y **18 muertos** (la primera pasada dejó 2 vivos: el separador del mensaje
de lo no visto y la rama «la entrada cambió desde que se enumeró»; se cerraron con tests), `lote._huella_acotada` 15 y **15 muertos**, `provider` (`carpeta_de_textures`, `huella_acotada`) 25 con
24 muertos y 1 vivo equivalente (`while True` mutado a `while True`). **No se ejecutó en Windows ni con el `texconv` real desde este PR** (la CI de Windows corre los tests); la
corrida real del propietario (sección anterior) es **previa** a este cableado y usó la enumeración provisoria.

**Costo y límites declarados.** (1) El proveedor calcula la huella de cada normal apta al descubrir y el lote la vuelve a calcular antes y después de decodificar:
**tres lecturas por textura** (antes eran dos). No se midió; con texturas de 1024²–2048² es despreciable frente a la integración, y con miles de texturas habría que
usar la huella del proveedor como referencia del lote (compararla en la lectura posterior y saltear la previa): queda anotado, no hecho. (2) El descubrimiento es
síncrono y corre dentro del bucle de eventos de `asyncio.run`: mientras hashea, un primer Ctrl+C se atiende al terminar (no verificado; el segundo interrumpe).
(3) El autoral que vale es el de la **carpeta de entrada**: la herramienta no ve los `*_p.dds` de OTROS mods que MO2 resuelve por su sistema virtual de archivos.
Para el adaptador de MO2 (NP-A0) es un requisito de diseño: sin saber qué `*_p.dds` gana el conflicto, la salida podría tapar parallax autoral de otro mod.

**Qué falta.** El adaptador de MO2 (con la regla del punto 3); la detección de `texconv` (su diagnóstico previo es la «Actualización de NP-A0c»; la CLI ya lo usa, ver «Cableado del diagnóstico a la CLI»); las superficies GUI y agente; usar la huella del proveedor
como referencia en el lote; saltear el cálculo de lo que la caché ya cubre.

### Actualización de NP-A0c (2026-10-23)

NP-A0c entrega `sky_claw/local/native_parallax/diagnostico_texconv.py`: el diagnóstico **previo** del `texconv.exe` configurado. Hasta acá, un
`texconv` equivocado se descubría tarde —después de decodificar, integrar y medir una textura— y seco (`TexconvNoCoincideError`). Este módulo
contesta antes, en español simple, qué le pasa al binario, y **no agrega verificación nueva**: delega entera la comparación de la huella en
`export.verificar_texconv`, que sigue siendo la única implementación del pin. No importa `hashlib`, no lanza procesos y no usa la red (anclado por
AST en su propio test). Es la mitad «diagnóstico» del ítem que «Qué falta de NP-A0» reclamaba para el registro de tools; el cableado a la CLI se hizo en «Cableado del diagnóstico a la CLI»; el
registro de tools sigue pendiente.

**Decisiones (nuestras, ancladas por `tests/test_native_parallax_diagnostico_texconv.py`):**

- **Vocabulario cerrado de siete estados** (`EstadoDeTexconv`: `CORRECTO`, `NO_CONFIGURADO`, `NO_ENCONTRADO`, `NO_ES_UN_ARCHIVO`,
  `NO_SE_PUDO_LEER`, `HUELLA_DISTINTA`, `SIN_VERSION_FIJADA`). Un helper construye UN diagnóstico por estado y el test compara el conjunto
  contra el vocabulario entero: agregar un estado sin escenario ni mensaje rompe el archivo. La superficie pública (`__all__`) está congelada
  por igualdad, y el dato es `frozen`/`slots` (un test lo comprueba).
- **Los cuatro fallos que `verificar_texconv` mete en un mismo tipo de error se separan por el archivo, no por el texto del mensaje.** El
  delegado distingue «no se pudo leer» con `raise ... from OSError`; las otras tres (no existe, no es un archivo, huella distinta) llegan sin
  causa y se separan con un `stat`: `S_ISREG` ⇒ huella distinta, otra cosa ⇒ no es un archivo, `FileNotFoundError`/`NotADirectoryError` ⇒ no
  encontrado, cualquier otro `OSError` ⇒ no se pudo leer. Un nombre que el sistema no puede interpretar (byte NUL, que llega como
  `ValueError`) se informa como no encontrado en vez de explotar en la cara del operador.
- **La ruta sale del argumento o de la variable de entorno, con la misma precedencia que la CLI y SIN recortarla.** `SKYCLAW_TEXCONV_EXE` es
  el mismo nombre que lee `cli._resolver_texconv` (un test los compara: dos nombres serían dos verdades) y el argumento gana. Solo un valor
  **vacío** deja paso —la regla `argumento or entorno.get(...)` de la CLI—, así que un `--texconv ""` cae a la variable y solo las dos
  vacías dan `NO_CONFIGURADO`; un valor de solo espacios **es** una ruta (inválida), como lo es para la CLI.
  **La ruta no se recorta**: se usa tal como la escribió el llamador (normalizada por `pathlib`, sin resolverla ni hacerla absoluta) y en el
  mensaje se muestra entre comillas angulares, con los caracteres de control escapados, para que los espacios de más se vean y un salto de
  línea no pueda dibujar una línea falsa. Recortarla haría que el diagnóstico dijera `CORRECTO` con una ruta que la exportación rechaza —la
  CLI pasa el valor crudo a `Path`—, y un pre-chequeo que aprueba lo que el exportador rechaza no sirve de nada. **Decisión del TL sobre la
  única divergencia que la primera versión declaraba** (un argumento de solo espacios caía a la variable, la CLI lo usaría como ruta): se
  cierra, porque era el mismo defecto en su forma residual —con `--texconv "   "` y una variable válida el diagnóstico decía `CORRECTO` y la
  exportación fallaba—. Ancla: una tabla de 15 entradas (blancos, espacios en los extremos, argumento contra variable) exige que el
  diagnóstico y `cli._resolver_texconv` elijan la misma ruta, y la tabla está congelada por cantidad.
- **El pin se lee en la llamada, no al importar.** `export.TEXCONV_PIN_VIGENTE` se consulta en cada diagnóstico (un test lo fija distinto y
  el módulo lo ve) y un `pin` explícito gana. `pin=None` significa «usá el vigente», no «no hay pin»: para diagnosticar sin pin se pasa uno
  explícito o se corre con el vigente en `None`. `version_esperada` es la del pin con el que se comparó y es `None` solo cuando no hay pin.
- **El dato se valida al construirse.** `DiagnosticoDeTexconv.__post_init__` rechaza un mensaje en blanco y una ruta sin origen: «el mensaje
  nunca está vacío» y «la ruta y su origen van juntos» son propiedades del tipo, no costumbres de los llamadores.
- **Anclas del repo tocadas, con su motivo.** El módulo entra al censo de módulos de producto y al de módulos que nombran `texconv`
  (`tests/test_texconv_invariant.py`), donde se declara que lo nombra como DATO y que no lanza procesos. Las dos anclas se pusieron rojas al
  crear el archivo, como corresponde, y se actualizaron después.

**Verificado ejecutando (Windows 11, Python 3.11):** rojo observado del módulo ausente (`ImportError` al colectar el test nuevo) y de las dos
anclas de censo (`Extra items in the left set: 'diagnostico_texconv.py'`); después, los 43 tests del módulo en verde, las dos anclas en verde
(201 tests entre las tres), `ruff check` y `ruff format --check` limpios sobre `sky_claw/ scripts/ tests/`, `mypy sky_claw/ scripts/` limpio
(398 archivos) y **mutación dirigida de 23 mutantes sobre el módulo: 23 matados, 0 vivos** (condiciones de cada estado, precedencia
argumento/variable, el recorte que no se hace, `S_ISREG`, las cuatro causas del delegado, el pin explícito contra el vigente,
`version_esperada`, las dos validaciones del dato, el detalle del `OSError` del delegado y del `stat`, el escapado de controles y el pin que se
le pasa al delegado). Los siete fallos que aparecen al correr la vecindad del paquete (`test_borrado_recursivo`,
`test_path_resolution_service`, `test_worktree_convention_invariant` y tres de `test_native_parallax_cli`) se reprodujeron IDÉNTICOS en un
worktree del commit base (`bb663d51`) sin estos cambios: son del entorno de esta máquina (sin privilegio para crear enlaces; censo de
worktrees legados), no de este PR.

**Revisión de #786 (CodeRabbit, dos hallazgos menores, verificados contra el código; los dos arreglados con su test).** (1) *El diagnóstico
podía aprobar una ruta que el exportador rechaza*: recortaba los espacios del valor, así que un `--texconv " C:\...\texconv.exe"` daba
`CORRECTO` mientras `_resolver_texconv` pasa el valor crudo a `Path` y falla. Ahora la ruta se usa tal cual y solo lo vacío deja paso a la
variable (la regla de la CLI); el mensaje la muestra entre comillas angulares con los controles escapados. (2) *Se perdía el motivo del sistema operativo cuando el que
fallaba era el `stat`* (un `ELOOP`, que `Path.is_file()` ignora): `_clasificar_fallo` devuelve el estado **y** el detalle, y el mensaje dice el
motivo en vez de «el sistema no dio un motivo». Ninguno de los dos cambió el vocabulario de estados.

**Segunda pasada del TL sobre #786 (Linux, Python 3.11; ejecutado).** Cerró la divergencia que quedaba con la CLI (ver arriba) y tapó huecos que
una **mutación automática por AST de las seis funciones del módulo** mostró y que el conjunto de 23 mutantes de la primera versión no tocaba:
la rama por defecto `os.environ` de `diagnosticar_texconv` no tenía ningún test, y el escapado de controles solo se probaba con `\n`. Rojo
observado antes de cambiar el módulo: 6 tests de la tabla de resolución y de los blancos. Después: 66 tests del módulo y 224 entre el módulo y
las dos anclas de censo, `ruff check` y `ruff format --check` limpios y ninguna de las 669 líneas base de `mypy` en `native_parallax/`. La
mutación da **66 mutantes: 60 muertos y 6 vivos, los 6 justificados, ninguno es un hueco**: tres sobre la rama `ruta is None` de `_como_se_ve`
(inalcanzable: los estados cuyo mensaje usa la ruta siempre la tienen, y `NO_CONFIGURADO` no la imprime), `Lt->LtE` (escapar el espacio da el
espacio), el `raise AssertionError` posterior al `match` exhaustivo (inalcanzable) y el texto de `esperado` sin pin (los dos estados sin pin no
lo imprimen).

**No verificado, por orden de importancia:** (1) nada se corrió con el `texconv` real: el diagnóstico no lo ejecuta y la huella se compara
contra archivos de prueba; (2) el estado «no se pudo leer» por permisos reales: en Windows `chmod` no quita la lectura, así que se ejerció
simulando el contrato del delegado (`raise ... from OSError`) y no con un archivo ilegible de verdad; (3) el cableado a la CLI y al registro
de herramientas, que no existe todavía.

**Límites declarados.** (1) Un archivo que desaparece entre la comprobación del delegado y su lectura se informa como `NO_SE_PUDO_LEER` con el
mensaje del sistema operativo: el diagnóstico reporta la carrera, no la adivina. (2) La ruta no se resuelve: dos ortografías del mismo archivo
se informan como el operador las escribió. (3) Cuando se entregó era un módulo de biblioteca sin superficie; la CLI lo usa desde «Cableado del
diagnóstico a la CLI» y el registro de tools todavía no. (4) No arregla nada: informa el estado y qué hacer.

### Cableado del diagnóstico a la CLI (NP-A0c, 2026-10-23)

La CLI deja de tener su propia verdad sobre el `texconv`. Antes había dos implementaciones del mismo recurso: `cli._resolver_texconv` (qué valor es
«la ruta», con el nombre de la variable repetido) y `export.verificar_texconv` llamada en seco desde `crear_exportador_texconv`; el diagnóstico de NP-A0c
era una tercera lectura, anclada a la CLI solo por una tabla de comparación que *detecta* la divergencia pero no la *previene* (el hallazgo de la
guía automática sobre #786: «dos superficies, un recurso»). Ahora:

- **La regla de la ruta y el nombre de la variable viven UNA vez**, en `diagnostico_texconv`: `ruta_configurada` (pública) y `ENTORNO_TEXCONV`. La CLI
  los importa; `cli.ENTORNO_TEXCONV` sigue existiendo como reexportación. `_resolver_texconv` delega en `ruta_configurada`.
- **`crear_exportador_texconv` pasa por `diagnosticar_texconv`** en vez de llamar a `verificar_texconv`: todo estado que no es `CORRECTO` corta con
  `UsoIncorrectoError(diagnostico.mensaje)`, así que el operador lee qué le pasa al binario y qué hacer (código de salida 2, sin traceback) y no el error
  seco de la huella. Recibe la ruta ya resuelta y un entorno vacío: la variable se lee una sola vez.
- **Diferencias de comportamiento, todas con su test adaptado:** (a) un `texconv` equivocado deja de ser `TexconvNoCoincideError` (un `ExportError`) y es
  `UsoIncorrectoError`: el mismo código de salida, otro texto; (b) «no configurado» dice la frase del diagnóstico («No me dijeron dónde está el
  texconv: pasá --texconv RUTA o definí la variable …») en lugar de «falta el texconv.exe …»; (c) se distinguen siete estados con su frase y qué hacer, donde
  antes llegaba el mensaje seco de `verificar_texconv` (no existe o no es un archivo / no se pudo leer / la huella no coincide / sin versión fijada). `main` sigue sin diagnosticar antes de la fábrica inyectada: los tests y cualquier otra fábrica reciben la ruta tal
  cual, porque diagnosticar toca el disco.

**Anclas.** `test_el_nombre_de_la_variable_vive_en_un_solo_modulo_del_proyecto` ENUMERA por AST todo `sky_claw/` y exige que el literal exista en un solo
módulo (rojo observado: aparecía en dos). `test_renombrar_la_variable_en_un_solo_lugar_la_mueve_en_el_diagnostico_y_en_la_cli` prueba lo mismo por
comportamiento (si la CLI conservara su copia del nombre o de la regla, seguiría leyendo la variable vieja). `test_todo_estado_que_no_es_correcto_corta_…`
se parametriza sobre `EstadoDeTexconv`: un estado nuevo queda cableado solo. `test_la_cli_diagnostica_el_binario_que_resolvio_…` ancla que se le pasa la
ruta resuelta y un entorno vacío. `tests/test_texconv_real.py` (el smoke real) lee la variable del entorno a propósito y queda fuera: es un test.

**Verificado ejecutando (Linux, Python 3.11):** rojo observado antes de implementar (15 tests: los de la superficie pública, las dos anclas, el estado por
estado, el exportador real con un binario equivocado y los tres que fijaban el contrato viejo); después, 146 tests de la CLI y del diagnóstico en verde,
708 en las anclas relacionadas (exportador, lote, proveedor, ejecutor de `texconv`, censos, ADR y clean-room), `ruff check` y `ruff format --check`
limpios, y `mypy sky_claw/ scripts/` en la línea base de Linux (669, ninguna en `native_parallax/`). **Mutación automática por AST** de lo que cambió:
`cli._resolver_texconv` y `cli.crear_exportador_texconv` 11 mutantes y 11 muertos, `diagnostico_texconv.ruta_configurada` 8 y 8; el nulo de cada función
en verde.

**No verificado.** Nada se corrió con el `texconv` real ni en Windows desde este cambio (la CI de Windows corre los tests con un binario de prueba). El
texto «No me dijeron dónde está el texconv» es el que escribió NP-A0c: no lo revisó nadie que no sea quien lo escribió y quien lo adaptó.

**Qué falta.** El registro de tools y las superficies GUI y agente todavía no llaman al diagnóstico; cuando lo hagan, `ruta_configurada` y
`diagnosticar_texconv` son la única forma de resolver y juzgar el binario, y el ancla de «un solo módulo» las cubre.

### Actualización: integración ponderada (2026-10-24)

**Hallazgo del rig** (`VERIFIED`: medido ejecutando sobre el archivo real; una sola textura). `cli generar` sobre `textures/architecture/whiterun/wrcitywall01_n.dds` (Skyland AIO, DX10 BC7 2048²) daba
un `_p` inservible: acuerdo p95 44.8° (igual en DirectX y OpenGL), correlación con el difuso −0.015 y, con min-max, una altura casi
plana. Hipótesis descartadas midiendo: la decodificación BC7 (R y G centrados en 126/127, B entre 125 y 255, longitud media 1.05;
intercambiar R y G empeora el residuo de 0.40 a 0.58), el canal alfa (0–105, especular; se ignora) y el mip (se lee el nivel 0,
2048²). **Causa:** 7 píxeles de 4,2 millones con `nz < 0`. La inversa `p = −nx / max(nz, 1e-6)` les da un gradiente ~1e6, y
Frankot–Chellappa lo reparte como dipolos sobre toda la textura (altura en ±179 con p2…p98 en ±1.2). Eso explica los tres síntomas:
los dipolos dominan el error angular (por eso invertir Y no lo mueve), tapan las piedras y, con min-max, se comen el rango.

**Decisión** (método `SUPPORTED_BY_LITERATURE`: la fuente citada abajo; la elección de `nz` y del relleno sale del experimento
siguiente). `integrar` gana `ponderacion` (`Ponderacion.UNIFORME`, por defecto y oráculo de research, o `Ponderacion.NZ`) y el
producto usa `PONDERACION_DEL_PRODUCTO = Ponderacion.NZ` en sus dos llamadores (el lote y la medición de NP-X0b, cuya versión sube a
`np-x0b.v2`). Mínimos cuadrados ponderados por `nz` (Quéau, Durou y Aujol 2018, «Normal Integration: A Survey»): como
`nz · p = −nx / sx`, el aporte de cada píxel queda acotado por su `nx`. Bajo el piso, pendiente nula con el peso medio (relleno
suave). Se resuelve con gradiente conjugado precondicionado por el Poisson uniforme; `TOLERANCIA_RELATIVA = 1e-8` y
`MAX_ITERACIONES = 500` son guardas numéricas, y sin converger se lanza `SinConvergenciaError` (un `IntegracionError`: el lote
lo cuenta como `NO_PROCESADA`). Algoritmos nuevos: `normal_wls_nz_periodic_v1` y `normal_wls_nz_neumann_v1` (entran a la clave de
caché: las salidas viejas se regeneran).

**Por qué `nz` y por qué el relleno (experimento propio, corpus sintético de NP-D0 a 128², 187 normales con ruido y cuantización;
`VERIFIED` sobre ese corpus, `INFERRED` para texturas reales).**
Con peso `nz²` y peso cero bajo el piso, 6 de 187 no convergían en 3000 iteraciones; con `nz` y peso cero, 3. Con relleno, ninguna:
máximo 117 iteraciones con `nz` y 815 con `nz²`. Sobre wrcitywall01 las dos dan casi lo mismo (p95 10.2° y 10.0°). En una normal
integrable y todo definida la altura es la de la uniforme (p95 limpio < 1e-6°).

**Contra los criterios de aceptación del TL (sección NP-V0).** (1)–(3) `VERIFIED` ejecutando (tests y una textura real); (4)
`REAL_RIG_REQUIRED`.
1. *Rojo observado antes:* los siete formatos de entrada aceptados × las dos normalizaciones daban correlación con la altura
   VERDADERA (sintética) de 0.18–0.41 y p95 ~35°; las cuatro clases de píxel fuera del dominio, < 0.5 con la uniforme.
2. *El acuerdo no se baja por decreto:* `metrics.py` y `gates.py` no cambian (ni la definición de `acuerdo_normal` ni
   `VERSION_DE_METRICAS`). El p95 de wrcitywall01 pasa de 44.8° a **10.2°** porque la altura reproduce la normal que antes no
   reproducía: la métrica es la misma, cambió lo que mide.
3. *¿La normal es integrable?* Decodificación y convención están bien (arriba). Por bandas de frecuencia, la energía del rotor de
   los gradientes sobre la de su divergencia es 3 % por debajo de 256 ciclos por textura —donde están las piedras—, 6 % entre 256 y
   512, 20 % entre 512 y 1024 y 66 % por encima. El residuo de integrabilidad (0.40, sin cambios: es de la entrada, no del integrador) es
   ruido de alta frecuencia, no la forma. El gate la sigue mandando a `REVIEW` (fail-closed, sin umbrales). **No se fabrica
   relieve:** el relleno solo actúa en los 7 píxeles indefinidos, y les pide pendiente NULA.
4. *Prueba final en el juego con BC4:* **pendiente.** La salida no se instaló.

**Resultado medido (Windows, `texconv` del pin `may2026`; `VERIFIED` sobre una textura).** Con el default `min_max`, que este cambio NO toca: acuerdo p95 10.2°,
altura normalizada con p2/p98 en 0.16/0.74 y desviación 0.132 (33.8 sobre 255). Antes, los extremos de los dipolos la dejaban
casi plana. Con `--normalizacion percentiles 2 98`: p95 10.2°, desviación 0.219 (55.8 sobre 255), 24 iteraciones (~9 s). Como
síntoma, y no como criterio (decisión del TL en NP-V0): la correlación del `_p` con el difuso pasa de −0.01 a +0.18.

**Anclas.** `tests/test_native_parallax_normales_fuera_de_dominio.py`: cada formato de entrada aceptado (la familia se congela
contra las listas cerradas de `decode.py`) × cada normalización por `lote._medir` con un bloque fuera del dominio; cada clase de
píxel fuera del dominio (negativo, cero, bajo el piso, diminuto sobre el piso) con los dos bordes; y por AST, que todo llamador de
producto de `integrar` pase `PONDERACION_DEL_PRODUCTO`. `tests/test_native_parallax_cli.py`: si la integración no converge, la
textura queda `NO_PROCESADA` y el lote sigue. La CLI con cp1252 va en un PR aparte.

**No verificado.** El `_p` nuevo en el juego (`REAL_RIG_REQUIRED`: hipótesis D de NP-V0, BC4, sigue abierta); la integración
ponderada a 4096² con `NEUMANN` (cuadruplica los píxeles resueltos; `NO VERIFIED`: ni tiempo ni memoria medidos).
