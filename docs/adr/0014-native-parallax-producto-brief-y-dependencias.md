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
   | NP-D0 | dataset spec + generador sintético + fixture MatSynth CC0 (descarga externa, hash-pinned, nunca commiteada) en `native_parallax/bench/`: el benchmark del que se derivan los thresholds | siguiente (en paralelo con NP-C0) |
   | NP-C0 | `NormalDecoder` (DDS/PNG, convención DirectX, confianza por `nz`) | siguiente |
   | NP-I0 | `normal_fft_periodic_v1` + variante Neumann + normalización | pendiente (necesita NP-C0 y el dataset de NP-D0) |
   | NP-Q0 | métricas y gates con thresholds **derivados** del benchmark | pendiente (necesita NP-I0 y el benchmark de NP-D0) |
   | NP-X0 | experimentos de signo/escala/inversión y `REVIEW_RATE` | pendiente |
   | NP-E0 | `DdsExporter` (texconv) | pendiente |
   | NP-O0 | `ManagedOutput` + manifest + cache por hash | pendiente |
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
- **Pillow tiene que ser ≥ 12.3.** `pip-audit` reporta 35 avisos sobre Pillow 11.3.0 (la versión que
  fijaba `[dev]`), todos corregidos entre 12.1.1 y 12.3.0; con 12.3.0 no reporta ninguno. El paso de CI que
  pide la decisión 3 no puede pasar con 11.3.0. El extra y `[dev]` quedan en `pillow>=12.3,<13` (rangos
  disjuntos harían irresoluble `uv sync --extra dev --extra native-parallax`); `<13` porque Pillow 13
  quita el parámetro `mode` de `Image.fromarray`. La suite de research (232 tests) pasa con 12.3.0.
- **BC5 llega sin Z.** Pillow 11.3.0 y 12.3.0 decodifican BC5 como RGB con B = 0 (bloque armado a mano), así
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

NP-D0 queda **PARCIAL**: este PR entrega el corpus sintético y su especificación; el fixture MatSynth CC0 no
está (ver «No incluido»). Verificado ejecutando en Linux (Python 3.11, numpy 2.4.6); el CI de Windows lo
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
- **No incluido.** (1) El fixture MatSynth CC0 (descarga externa, hash-pinned, nunca commiteada): necesita
  que el operador decida qué subconjunto, qué hashes y desde dónde descargar; `research/` ya tiene fetchers y
  un manifiesto (`exp-m3-clean-authored-manifest.json`) que son el punto de partida, pero promoverlos al
  producto trae egress de red y es otro PR. (2) Variantes con error de compresión por bloques: hace falta un
  codificador y el producto no tiene uno (decisión 4; texconv, NP-E0). (3) La inversa N → H y el integrador:
  NP-I0. Debe compartir con `bench/sintetico.py` la convención de frecuencias (`_ejes_de_frecuencia`); un
  solver con otra daría gradientes escalados por la resolución.

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
