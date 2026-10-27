# Prueba del generador nativo de parallax en un rig real

> **Audiencia:** operadores (no hace falta ser técnico).
>
> **Estado:** experimental. El gate de calidad no tiene umbrales validados: **todo** lo que se genera queda «en revisión»
> y la herramienta lo escribe solo si se lo pedís (`--incluir-revision`). Esta prueba existe justamente para juntar los datos
> con los que se calibran esos umbrales.
>
> **Fuentes canónicas:** `sky_claw/local/native_parallax/cli.py`, `sky_claw/local/native_parallax/lote.py`,
> [ADR 0014](../adr/0014-native-parallax-producto-brief-y-dependencias.md) («Actualización de NP-A0a») y
> `sky_claw/local/native_parallax/AGENTS.md`.
>
> **Verificación:** la tubería se probó con tests y un exportador de prueba en Linux. **No se ejecutó con el `texconv` real ni
> sobre NTFS desde el desarrollo**: esa es la primera corrida real, y la hacés vos.

## Qué toca y qué no

- **Lee** la carpeta de entrada que le indiques. Nunca la modifica.
- **Escribe** las texturas solo en `<mods>\Sky-Claw - Native Parallax Output` (el mod de salida) y en su subcarpeta de control
  `.skyclaw-output`. Si se corta a la mitad (cierre, Ctrl+C, corte de luz), la próxima corrida lo recupera sola. Además deja,
  fuera del mod: el informe `informe_parallax.json` (en la carpeta actual o donde le indiques con `--informe`), una carpeta
  temporal propia que borra al terminar (si no puede, te avisa) y el estado del bloqueo que evita que dos instancias escriban a
  la vez, en la carpeta de datos de tu usuario.
- **El informe** (`--informe`) se escribe fuera del mod: la herramienta se niega si lo apuntás adentro del mod de salida o adentro de la carpeta `textures` de tu entrada.
- **Para deshacer todo**: desactivá ese mod en MO2 o borrá su carpeta. No toca tus otros mods ni la carpeta `Data`.
- **No crea `meta.ini` ni lo habilita en MO2**: tenés que refrescar MO2 (F5) y activarlo a mano. No verifiqué cómo lo
  muestra MO2; si no aparece, avisá.

## Preparación (una sola vez)

1. Windows, con el repo **al día**: el comando vive en `main` desde el PR #783, y una copia vieja del repo da
   `No module named sky_claw.local.native_parallax.cli`. Hacé `git fetch origin` y trabajá desde una copia de `origin/main` (lo más
   seguro es una carpeta aparte, por ejemplo `git worktree add .worktrees\rig -b rig/prueba origin/main`, para no mover tu copia de trabajo).
   Corré todos los comandos **desde la carpeta de esa copia**.
2. El entorno: `uv sync --extra dev` (instala numpy y Pillow). Solo hace falta si faltan: si `.venv\Scripts\python -c "import numpy, PIL"`
   no da error, ya están.
3. El `texconv.exe` oficial **fijado por el proyecto** (DirectXTex, versión `may2026`). Bajalo de
   `https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe` (es el origen que fija el proyecto: `TEXCONV_PIN_VIGENTE`
   en `export.py`; pesa 966 480 bytes). La herramienta verifica su huella SHA-256 y se niega a correr con cualquier otro. Para comprobarla vos:
   `certutil -hashfile C:\ruta\a\texconv.exe SHA256` (con la ruta completa del archivo que vas a usar) tiene que dar
   `dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06`.
4. Decile dónde está (en la misma ventana de consola, antes de los comandos):
   `set SKYCLAW_TEXCONV_EXE=C:\ruta\a\texconv.exe`

En los comandos de abajo, `python` es `.venv\Scripts\python` (desde la carpeta del repo).

## Paso 1 — Autoprueba (5 minutos, no usa tus texturas)

```
.venv\Scripts\python -m sky_claw.local.native_parallax.cli autoprueba
```

Fabrica una textura sintética, corre **todo** (lectura, integración, `texconv` real, escritor sobre el disco) en una carpeta
temporal y revisa que lo generado sea un BC4 válido. Tiene que terminar en `AUTOPRUEBA OK`. Si dice `AUTOPRUEBA FALLÓ` o
`Error:`, copiá el texto de la consola y el archivo `informe_parallax.json` que deja en la carpeta actual.

## Paso 2 — Elegí 5 a 10 texturas

Copiá a una carpeta nueva (por ejemplo `C:\prueba_parallax\entrada`) **solo** los mapas normales (`*_n.dds`) que quieras
probar, **conservando su estructura** `textures\...` (por ejemplo `entrada\textures\architecture\whiterun\muro01_n.dds`).
Conviene variar: piedra, madera, tierra, algo con relieve fuerte y algo suave. Pautas:

- Lados **potencia de 2** (256, 512, 1024, 2048, 4096). Los otros se rechazan y el informe lo dice.
- Sin un `*_p.dds` al lado: si ya hay uno (puede ser autoral) esa textura se omite a propósito.
- Las de 4096×4096 tardan del orden de 30 s y usan varios GiB de RAM; empezá con 1024 o 2048.

## Paso 3 — Generar

```
.venv\Scripts\python -m sky_claw.local.native_parallax.cli generar --carpeta C:\prueba_parallax\entrada --mods D:\MO2\mods --incluir-revision
```

(`--mods` es la carpeta `mods` de tu MO2.) Muestra una línea por textura con sus métricas y el veredicto del gate, un
resumen, y escribe `informe_parallax.json`. Los `*_p.dds` quedan en `D:\MO2\mods\Sky-Claw - Native Parallax Output\textures\...`.

Si la carpeta de entrada es un mod de tu MO2, podés pedirle que mire el perfil (sin tocarlo) con `--data-root D:\MO2 --mod "Nombre del mod" [--perfil Default]`:
no mide una textura que otro mod tapa, y **avisa antes de escribir** si otro mod, `overwrite` o un mod de control queda por encima de la salida (o si la salida no está
habilitada). El aviso no frena la corrida: en la primera vez el mod de salida todavía no existe en tu lista. Para solo mirar, sin generar: `cli perfil --mod … --mods … --data-root …`
(sale con 4 si hay algo tapado). Es una vista del `modlist.txt`: no reemplaza mirar la pestaña «Datos» ni probar en el juego.
El mismo diagnóstico está disponible como tool de solo lectura `diagnosticar_parallax_perfil` desde la GUI —el botón de lupa de cada fila de la pantalla «Mods»; el
resultado aparece en un panel que se cierra con la ×— y desde el agente de Telegram / `/api/chat` (solo se le pasa el nombre del mod; usa el perfil de la sesión): las dos
contestan con la misma función y sobre las mismas carpetas de MO2.

Volver a correr el mismo comando recalcula todo pero **no reescribe** lo que no cambió (el informe lo marca `OMITIDA`). Si cambiás un
parámetro (abajo), esa salida se reemplaza sola.

Sin `--incluir-revision` **no se escribe ninguna textura en el mod de salida** (el informe `informe_parallax.json` sí se escribe): el gate no tiene umbrales validados, manda
todas las texturas a revisión y solo las deja en la salida si se lo pedís. Es lo esperado, no un error.

### Cómo leer el informe

El informe describe cada textura con tres campos que se confunden fácil. Son cosas distintas:

| Campo | Qué dice |
|---|---|
| `veredicto` | lo que decidió el gate con las métricas: `REVIEW` mientras no haya umbrales validados |
| `estado` | qué pasó con el archivo: `ESCRITA` (quedó en la salida: publicada, reemplazada **o con la caché vigente**), `EN_REVISION` (el gate la mandó a revisión y no pediste escribirla), `NO_PROCESADA` (falló antes de tener algo que escribir) o `NO_ESCRITA` |
| `efecto` | qué hizo el escritor con esa ruta: `PUBLICADA`, `REEMPLAZADA`, `OMITIDA` (la caché estaba vigente), `PRESERVADA`, `CONFLICTO`, `FALLIDA`… |

Por eso una textura puede tener `veredicto: REVIEW`, `estado: ESCRITA` y `efecto: OMITIDA` sin que nada se contradiga, y `conteos` se calcula de la misma lista de archivos.

`amplitud` (p2, p98 y desviación estándar de la altura que se exporta, en la escala [0, 1]) dice qué tan plano quedó el mapa. **Describe, no decide**: no hay un valor «bueno»
ni un umbral. Una desviación cercana a 0 es un mapa que casi no desplaza nada en el juego; fuera de ese caso, qué tan fuerte se ve lo decide el juego (y tus ojos), no este número.

## Paso 4 — Mirarlo en el juego

1. En MO2, refrescá (F5), activá el mod «Sky-Claw - Native Parallax Output» y ponelo **por debajo del mod de texturas
   original** en la lista de prioridad (solo agrega archivos `_p.dds`, no pisa las normales).
2. Para que el parallax **se vea**, la malla o el terreno tiene que usar parallax (depende de tu ENB / preset / Community
   Shaders). No verifiqué cuál es tu caso: contame qué usás.
   **Antes de elegir qué mirar, preguntale a la herramienta** (solo lee, no genera nada y no necesita `texconv`):
   `.venv\Scripts\python -m sky_claw.local.native_parallax.cli elegibilidad --mod "Nombre del mod" --mods D:\MO2\mods --data-root D:\MO2 [--perfil Default]`.
   **Requisito:** `--mod` tiene que ser un mod **habilitado** de tu perfil de MO2 (una carpeta dentro de `--mods`): evalúa las texturas de ese mod, no las que copiaste a
   `C:\prueba_parallax\entrada`. Si probás con una carpeta suelta que no es un mod habilitado, salteá este diagnóstico (con un mod que no está habilitado devuelve un error de uso).
   Por cada textura del mod dice `SI` (hay una malla suelta del perfil donde el plugin de relieve automático usaría la salida, y cuál), `NO` (ninguna forma usaría esa salida: o todas
   las mallas que la referencian quedan fuera —relieve de fábrica que el plugin apaga, transparencia, sin colores de vértice…, con el desglose—, o el plugin elegiría antes otra
   textura suelta; también si el generador no la publicaría porque otro mod tapa la normal de entrada) o `NO_VERIFICADO` (no se puede afirmar: ninguna malla suelta la referencia, la
   búsqueda del plugin depende de un archivo que no se puede comprobar, una ruta es ambigua o alguna malla no se pudo leer). Elegí para mirar las que dicen `SI`. **Límites:** no lee
   mallas dentro de un `.bsa` (las del juego base lo están: esas salen `NO_VERIFICADO`); el plugin solo actúa sobre objetos base Static, Movable Static y Container (no puertas ni
   activadores) y solo enciende relieve con `bAutoEnableParallax` activo en su INI (por defecto no), y nada de eso se ve en la malla; la regla sale del código público del plugin y
   la versión que tenés instalada puede diferir.
3. Comparalo con el mod desactivado, en el mismo lugar y con la misma luz, y sacá capturas.
4. **Antes de cada prueba, confirmá en la pestaña «Datos» de MO2 que la columna «Mod» del `*_p.dds` dice «Sky-Claw - Native Parallax Output»**. Un mod que esté por
   encima (incluido un mod de control o de pruebas) tapa la salida; y un cambio de prioridad hecho desde la ventana de MO2 puede no aplicarse: si «Datos» sigue mostrando
   el mod anterior, cerrá MO2 y revisá `modlist.txt` antes de lanzar el juego, o esa corrida no prueba lo que creés.
5. Para ubicar qué textura usa un objeto en el juego, abrí la consola (`~`) y hacé clic en el objeto: la consola de Skyrim sin mods solo muestra su identificador. Con ese identificador lo buscás en
   xEdit para ver qué malla usa y abrís la malla en NifSkope para ver sus texturas (un mod como More Informative Console muestra más datos en la consola; no verifiqué cuántos). No supongas
   cuál es por el nombre del objeto (el aro de piedra de un pozo de Whiterun usaba `WRCityWall01`, no la textura de piedra que parecía obvia).

## Qué mandarme

- El texto de la consola y el archivo `informe_parallax.json` (tiene las métricas de cada textura: son los datos para
  calibrar los umbrales).
- Las capturas con y sin el mod, y qué viste: «se ve hundido / invertido», «ruido», «costuras al repetir», «muy plano»,
  «se ve bien».

## Si algo se ve mal: los botones

| Síntoma | Probá | Por qué |
|---|---|---|
| Relieve **invertido** (lo alto se ve hundido) | `--escala-x -1 --escala-y -1` | invertir los dos ejes a la vez da exactamente la altura al revés (invertir uno solo cambia la forma) |
| Detalle raro en texturas que vienen de otro programa | `--convencion opengl` | algunos programas exportan el canal verde al revés; Skyrim usa DirectX. La herramienta no lo adivina: lo fija el origen del archivo |
| Costura visible al repetir la textura | `--bordes neumann` | otra hipótesis de borde (más lenta) |
| Relieve muy plano o con puntos extremos | `--normalizacion percentiles --percentil-bajo 2 --percentil-alto 98` | recorta los extremos antes de normalizar |

Cada cambio de parámetro cambia la clave de caché: la salida anterior de esa textura se reemplaza sola (solo si no la
editaste a mano). Con `--retirar-no-incluidas` también se borran las salidas de texturas que ya no están en la carpeta de
entrada; si no pudo leer alguna carpeta de la entrada se niega a correr (código 2), porque no sabría qué hay ahí.

## Límites conocidos

- Hoy no hay umbrales validados: el gate manda todo a revisión. Por eso `--incluir-revision` es obligatorio para escribir.
- Lo que está probado en el juego (una persona, a ojo): un `*_p.dds` en BC4 lo usa el juego, y la salida real del generador sobre una textura (`wrcitywall01`, el aro de piedra del
  pozo de Whiterun) se vio con relieve. **No** está probado en otras texturas ni en otras máquinas; las que salen con un acuerdo alto (más de ~20°) no se sabe todavía si se ven bien o mal.
- Un mapa casi plano era sobre todo un defecto de la integración (unos pocos píxeles bajo el horizonte lo arruinaban) y ya está corregido; si igual lo ves en el informe (`amplitud` con
  desviación cercana a 0), mandame la textura de entrada.
- La generación solo existe como línea de comandos (sin interfaz gráfica ni agente). El diagnóstico del perfil (`diagnosticar_parallax_perfil`) sí está en la GUI y en el agente. La integración con MO2 (habilitar el mod solo) viene después.
- La carpeta se enumera con el proveedor endurecido: solo `*_n.dds` bajo `textures\`, sin seguir enlaces. Una textura que ya tiene un `*_p.dds` con la misma
  ruta de salida en la carpeta de entrada (en cualquier subcarpeta) se omite a propósito. Los `*_p.dds` de **otros mods** (los que MO2 ve por su sistema virtual de
  archivos) no se consultan: la herramienta lee solo la carpeta que le indicás.
- Códigos de salida: `0` bien, `1` alguna textura falló, `2` mala configuración (falta el `texconv`, carpeta inválida…),
  `3` otra instancia está escribiendo esa misma salida, `130` interrumpido.
