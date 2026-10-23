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

1. Windows, con el repo y su entorno: en la carpeta del repo, `uv sync --extra dev` (instala numpy y Pillow).
2. El `texconv.exe` oficial **fijado por el proyecto** (DirectXTex, versión `may2026`). La herramienta verifica su huella
   SHA-256 y se niega a correr con cualquier otro. Para comprobarla vos:
   `certutil -hashfile C:\ruta\a\texconv.exe SHA256` (con la ruta completa del archivo que vas a usar) tiene que dar
   `dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06`.
3. Decile dónde está (en la misma ventana de consola, antes de los comandos):
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

Volver a correr el mismo comando recalcula todo pero **no reescribe** lo que no cambió (el informe lo marca `OMITIDA`). Si cambiás un
parámetro (abajo), esa salida se reemplaza sola.

## Paso 4 — Mirarlo en el juego

1. En MO2, refrescá (F5), activá el mod «Sky-Claw - Native Parallax Output» y ponelo **por debajo del mod de texturas
   original** en la lista de prioridad (solo agrega archivos `_p.dds`, no pisa las normales).
2. Para que el parallax **se vea**, la malla o el terreno tiene que usar parallax (depende de tu ENB / preset / Community
   Shaders). No verifiqué cuál es tu caso: contame qué usás.
3. Comparalo con el mod desactivado, en el mismo lugar y con la misma luz, y sacá capturas.

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
- No hay interfaz gráfica ni agente: solo esta línea de comandos. La integración con MO2 (habilitar el mod solo) viene después.
- La carpeta se enumera con el proveedor endurecido: solo `*_n.dds` bajo `textures\`, sin seguir enlaces. Una textura que ya tiene un `*_p.dds` con la misma
  ruta de salida en la carpeta de entrada (en cualquier subcarpeta) se omite a propósito. Los `*_p.dds` de **otros mods** (los que MO2 ve por su sistema virtual de
  archivos) no se consultan: la herramienta lee solo la carpeta que le indicás.
- Códigos de salida: `0` bien, `1` alguna textura falló, `2` mala configuración (falta el `texconv`, carpeta inválida…),
  `3` otra instancia está escribiendo esa misma salida, `130` interrumpido.
