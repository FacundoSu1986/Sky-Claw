# Parallax nativo (beta): estado y cómo ajustar la profundidad

> **Audiencia:** operadores y agentes (IA) con acceso a la línea de comandos de SkyClaw.
>
> **Estado:** **BETA.** Usable en modo asistido; sin garantías de calidad automática. Ver abajo qué significa y qué no está validado.
>
> **Procedencia:** todo lo medido en el juego lo ejecutaron agentes en la PC del operador y está **reportado**: quien redactó este documento no lo reprodujo. El juicio visual es del operador.
>
> **Fuentes canónicas:** [guía de prueba en un rig real](native_parallax_prueba_en_rig.md),
> [resultados del rig](../design/research/native-parallax/np-prueba-rig-v32-resultados.md),
> [ADR 0014](../adr/0014-native-parallax-producto-brief-y-dependencias.md) y el issue #834.
>
> **Última verificación:** 2026-11-08 sobre `origin/main`.

## Qué significa «beta» acá

- **Modo asistido.** El operador genera con la línea de comandos y decide a ojo. Todo lo que se genera queda en revisión (`REVIEW`): el gate de calidad no tiene umbrales validados, así que la herramienta escribe solo si se le pide `--incluir-revision`.
- **Solo línea de comandos.** No hay generación en la interfaz gráfica ni en el agente de Telegram: este documento es para quien tenga acceso a la consola.
- **Lo probado** (una persona, una máquina, un mod de texturas, sin pares a ciegas):
  - Celda de prueba con luz fija: el relieve se notó en **12 de 12** estaciones.
  - A/B del interruptor `--centrado media` (2026-11-06): el operador prefirió el centrado en 2 de 3 pares que miró.
  - **Juego real** (2026-11-08, clima y hora normales, distancia normal): el relieve se notó en **2 de 6** lugares; en los otros 4, «casi nada». Sin tirones, parpadeos ni picos. Una textura (el piso debajo del trono del Jarl de Falkreath) se vio «grumosa».
- **No validado:** otras máquinas, otros mods de texturas, otros observadores, ENB, LOD, clima controlado.

## Antes de tocar la amplitud: ¿el plugin usa la salida?

Gran parte de los «no se nota» no es un problema de amplitud. Comprobá, en este orden:

1. `.venv\Scripts\python -m sky_claw.local.native_parallax.cli elegibilidad --mod "<mod>" --mods <carpeta mods> --data-root <carpeta MO2> --perfil "<perfil con el que se juega>"` da `SI` para esa textura. Sin `--perfil` usa `Default`, que puede no ser el perfil del juego. Si da `NO_VERIFICADO` (por ejemplo, porque la malla está dentro de un `.bsa`, que la herramienta no lee), **no concluyas que no se usa**: seguí con la comprobación manual de la guía de prueba (paso 4, punto 5: consola, xEdit y NifSkope).
2. El `AutoParallax.ini` que gana en el perfil tiene `bAutoEnableParallax = true`. Apagado, ninguna salida se usa.
3. El objeto que muestra la textura es **Static, Movable Static o Container** (probado con la DLL 1.0.6.0 en una máquina). En puertas y activadores no hay relieve aunque la elegibilidad diga `SI`; las mallas con relieve de fábrica también las apaga el plugin.
4. En la pestaña «Datos» de MO2, la columna «Mod» del `*_p.dds` dice «Sky-Claw - Native Parallax Output». Si otro mod tapa la salida (o trae su propio `_p`), lo que se ve es ese.

## Para el agente: qué ajustar si el operador quiere más (o menos) profundidad

**Hoy no existe un parámetro directo de «profundidad» o «altura».** La salida siempre se normaliza a [0, 1] y cuánto se ve lo decide también el juego. Una barra o un parámetro `--altura` se evaluó y el operador decidió no construirlo (2026-11-08): queda como idea pendiente en #834. Lo que hay hoy:

| El operador dice… | Probá | Qué esperar | Evidencia |
|---|---|---|---|
| «Se ve plano; quiero más relieve» (y los controles de arriba están bien) | `--normalizacion percentiles --percentil-bajo 2 --percentil-alto 98` | Estira el rango central: más desviación; un pequeño porcentaje de píxeles se satura (se recorta). | **Medido en una sola textura** (ADR 0014, NP-V0d, 2026-10-24): desviación 33,8 → 55,8 sobre 255, 4,3 % saturado; el propietario la vio «más» que con la normalización por defecto. Una persona; no probado en otras texturas. |
| «Se ve puntiagudo / grumoso» | Primero, dejar de usar la salida de esa textura (no basta con no volver a generarla: ver «Cómo aplicar un cambio», paso 2) o dejar el mapa de otro mod. Si querés bajar amplitud: `--centrado media`. | Lleva la media a 0,5 y baja la amplitud entre 0,5 y 1 veces (casi nada si la textura ya estaba centrada). | En la celda de prueba el operador prefirió el centrado en 2 de 3 pares. En `rottingwood01` (la «grumosa») la amplitud baja solo ≈7 %: **no la arregla**. En el juego real la textura grumosa iba sin centrar, pero la causa no se aisló. **No hay un control para bajar mucho la amplitud.** |
| «Quiero más profundidad que la que da percentiles» | No hay otro control en SkyClaw. | — | El shader del juego (Community Shaders / Auto Parallax) puede tener su propia escala; **no está verificado** si la trae. |

Lo que **no** sirve para cambiar la profundidad: `--escala-x` y `--escala-y`. Una escala igual en los dos ejes no cambia la amplitud (la normalización la reescala); solo el signo importa (`-1` y `-1` invierte el relieve).

La desviación sola **no predice lo que se ve** (ADR 0014, NP-V0d): no prometas un resultado, proponé una prueba y que el operador mire.

## Cómo aplicar un cambio sin romper nada

1. **Respaldá** el mod de salida («Sky-Claw - Native Parallax Output») con huellas SHA-256 antes de regenerar. Para deshacer: restaurar el respaldo o desactivar el mod.
2. **Un cambio de parámetro reemplaza la salida de esa textura** (cambia su clave de caché) **solo si la textura se vuelve a escribir**. Lo que no cambió no se reescribe, y una textura que sale `SIN_CONTENIDO` (la altura queda bajo el piso de contenido y no se escribe, aunque pidas `--incluir-revision`) o que no está en la carpeta de entrada **conserva su salida anterior**. Antes de comparar, mirá en el informe que el `estado` sea `ESCRITA` y revisá el `efecto`: `REEMPLAZADA` o `PUBLICADA` es lo esperado; `OMITIDA` significa que la salida estaba vigente y no se reescribió, y `EN_REVISION` que no se escribió nada. Para **dejar de usar** una salida que no querés (por ejemplo, una textura «grumosa» ya generada): restaurá el respaldo, desactivá el mod de salida, o usá `--retirar-no-incluidas` con una carpeta de entrada que **no** la incluya (esa opción borra las salidas nuestras de **todas** las texturas que no estén en esa carpeta).
3. **Para aplicar valores distintos a texturas distintas**, usá carpetas de entrada separadas (cada una con su estructura `textures\...`), el mismo `--mods`, y **nunca** `--retirar-no-incluidas` salvo que quieras borrar el resto. Así se hizo en la prueba del juego real (dos corridas sobre el mismo mod de salida).
4. Generá con `--incluir-revision` y leé el informe (`informe_parallax.json`): `estado`, `motivos` y `amplitud`. Un `SIN_CONTENIDO` indica una textura casi plana.
5. Confirmá en «Datos» y mirá en el juego, en el mismo lugar y con la misma luz, con y sin el mod. Si otro mod ya trae un `_p` para esa textura, «sin el mod» compara contra ese mapa, y dos mapas de relieve parecidos no se distinguen a ojo: usá un **mapa plano como control** (guía de prueba, paso 4, punto 3). **El operador decide.**

## Reglas para el agente

- No afirmes que algo «se ve mejor»: lo decide el operador mirando el juego.
- No cambies el valor por defecto de ningún parámetro del producto por un pedido de profundidad: eso se decide con evidencia, no por una preferencia.
- No toques INI, perfiles ni otros mods sin permiso expreso; avisá antes de abrir o cerrar MO2 o Skyrim.
- Separá siempre lo ejecutado, lo leído y lo no verificado.

## Límites conocidos

Ver la sección «Límites conocidos» de la [guía de prueba](native_parallax_prueba_en_rig.md): sin umbrales validados en el gate, normales DXT5 sin soporte, generación solo por línea de comandos y sin integración con MO2.
