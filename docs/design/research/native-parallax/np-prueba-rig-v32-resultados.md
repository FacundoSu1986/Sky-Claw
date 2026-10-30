# Prueba del generador nativo de parallax en el rig (plan v3.2): resultados y límites

> **Estado:** registro de evidencia de una prueba real, 27–30 de octubre de 2026. **No es un ADR** y no cambia ningún contrato.
>
> **Procedencia:** el informe lo redactó la sesión de agente que corrió la prueba en la PC del operador; este documento lo resume.
> **Quien lo resume no reprodujo las mediciones**: no tiene acceso al rig. Lo medido figura como *reportado*; lo que la propia sesión
> marcó como no verificado sigue así. La evidencia cruda (capturas, logs, JSON de las fases) está en una carpeta local del operador,
> fuera del repositorio: no se versionan capturas del juego ni binarios.
>
> **Clean-room:** la prueba no usó la salida de ninguna herramienta cerrada (no había una instalada). Ver
> [CLEAN_ROOM.md](../../../../CLEAN_ROOM.md).

## 1. Rig y condiciones

| Elemento | Valor reportado |
|---|---|
| Gestor | Mod Organizer 2 2.5.2 |
| Juego | Skyrim SE 1.6.1170 |
| Pila de render | Community Shaders 1.9.1 (Extended Materials activo; terreno apagado) |
| Auto Parallax | 1.0.6.0, `bAutoEnableParallax=true`, log de depuración encendido |
| Post-proceso | ReShade con complementos de DLSS presente en el equipo; **apagado durante la prueba** y restaurado al final |
| Salida bajo prueba | mod de salida del generador generado el **23/10**, con código anterior a #794, #797 y #805 |

La fuente pública del plugin se leyó y se fijó por SHA (`c70f6e21…`, el mismo que cita
[el informe de pilas y rutas de malla](np-pilas-y-rutas-de-malla.md)); la versión instalada (1.0.6.0) **no** se verificó contra
ese código: lo que sí se verifica es el comportamiento observable (el log del plugin).

## 2. Qué se preguntó y cómo se decidió (fijado antes de mirar)

| # | Pregunta | Regla |
|---|---|---|
| P0 | ¿Este entorno puede mostrar el relieve de un `_p`? | Si el control positivo no se ve, la prueba entera no vale |
| P1 | ¿Los `_p` del generador cambian lo que se ve frente a un `_p` **plano**? | Cambio real = diferencia A–B ≥ 3 × la diferencia A1–A2, sobre el recorte de cada estación |
| P2 | ¿Cómo se comparan con ParallaxR? | **No se hizo**: no hay una salida de esa herramienta en el rig |
| P3 | ¿El chequeo automático de elegibilidad coincide con la tabla manual? | Comparación sobre las mallas del perfil |

La regla de P1 es una vara de medición, no un umbral de calidad.

## 3. Resultados (reportados)

**P0 — sí.** Un `_p` exagerado frente a uno plano en el pozo de Whiterun se distinguió a ciegas 2 de 2 (la segunda vez el
observador ya conocía cuál era: ciego débil). El primer diseño del control, contra un `_p` que ya traía relieve, no mostró diferencia:
**era un error de diseño**, no de la vista del observador.

**P1 — sí, en las 5 estaciones probadas.** Celda interior propia, luz fija más una luz puntual rasante por estación, un objeto por
estación y un banco como testigo sin relieve.

| Estación | Ruido A1–A2 | Efecto A–B | Efecto ÷ ruido | Píxeles que cambian > 8 niveles | Regla 3× |
|---|---|---|---|---|---|
| S1 pared | 0,086 | 17,09 | 198× | 60,8 % | cumple |
| S2 mesa | 0,091 | 7,98 | 87× | 28,7 % | cumple |
| S3 puerta | 0,034 | 19,22 | 573× | 58,1 % | cumple |
| S4 techo | 0,071 | 11,66 | 165× | 35,8 % | cumple |
| S5 piso | 0,087 | 8,80 | 101× | 30,3 % | cumple |
| Testigo (sin relieve) | — | 0,02 | — | 0,0 % | control correcto |

El render es casi determinista (ruido ≈ 0). El juicio a ciegas dio **5 de 5** (el lado de B estaba repartido, no es sesgo de lado);
es débil porque el observador sabía que el relieve venía con B. **Lo fuerte del resultado es la medición, no el juicio.**

**P3 — coincide.** La tabla manual y el núcleo del chequeo automático dieron **0 discrepancias sobre 13.360 formas de malla**. Es la
primera comparación con datos reales del lector de mallas y de la regla de elegibilidad.

## 4. Lo que NO se probó (límites)

- **Que el mapa sea bueno.** Se midió que **se nota**. Dos efectos secundarios a ojo, **sin causa verificada**: una banda oscura en el
  borde de una puerta y la pérdida del tinte verde de musgo en un techo (hipótesis no verificada: el desplazamiento cambia qué parte
  de la difusa se muestrea).
- **P2:** no hay comparación con ParallaxR.
- **Un solo observador**, solo interior, con luz rasante y vista cercana; sin clima ni distancia normal de juego. **No verificado** que
  en exteriores el mapa se note igual.
- **Con el post-proceso apagado:** el resultado vale para Community Shaders + Auto Parallax; con ReShade encendido no se midió.
- **Solo mallas con shader por defecto**, la ruta de activación de Auto Parallax. En mallas con relieve de fábrica A y B son iguales
  por construcción.
- **5 de los 8 `_p` utilizables** se probaron. Del mod de salida, 5 de 15 son casi planos y 2 no los puede mostrar ninguna malla.
- **Salida del 23/10** en las secciones 3 y 4. La adenda (sección 8) repite P1 con una salida generada con el código actual, con sus propios límites.

## 5. La zona gris del portón de Whiterun

Se reproduce **sin la salida, sin el control y sin ReShade**, y no cambia con otro `_p` en la textura del muro. Los logs de Auto
Parallax y de Community Shaders de una sesión sin salida son casi idénticos a los de una sesión normal anterior. **No la causó la
prueba ni el generador**; la causa sigue sin verificar (candidatos: mallas y colisión de un mod de murallas, LOD o terreno, otros `_p`
que Auto Parallax carga igual en todas las sesiones).

## 6. Lecciones de método (para pruebas futuras)

1. Comparar siempre contra un `_p` **plano**. Relieve contra relieve (desvíos 48 y 32) no se ve.
2. Identificar qué archivo ganó con `indexar_ganadores`, **no** con el log de Auto Parallax: el log solo anota la **ruta** del `_p`,
   igual en A y en B.
3. Una estimación previa de "3–5 cm de desplazamiento, casi ninguna se notará" resultó pesimista: todas se notaron. Las estimaciones
   ordenan la prueba, no la reemplazan.
4. Una afirmación previa de que las rutas `landscape\…` del log eran texturas de terreno era **falsa**: son texturas de objetos (el plugin
   no trata terreno y `EnableTerrain=0`).
5. Normalizar y redondear ángulos antes de escribir archivos `bat` de cámara.

## 7. Qué sigue

1. ~~**Salida fresca** del generador actual sobre las mismas 5 estaciones; repetir P1.~~ **Hecho el 30/10 (tarde): P1 se sostiene; ver la sección 8.**
2. Medir las dos anomalías (puerta y techo) y decidir si son del mapa o del entorno.
3. Aislar la zona gris del portón con una sesión corta sin Auto Parallax.
4. P2, solo si aparece una salida de ParallaxR en el rig; comparación **a ciegas** y sin usar sus valores para ajustar umbrales.
5. Pasar al chequeo automático las dos diferencias teóricas frente al plugin (la difusa `x_d.dds` → `x_p.dds` y derivar `x_p.dds` de una
   normal sin sufijo): **no se ejercitaron con datos reales** (0 discrepancias).

## 8. Adenda (30/10, tarde): P1 repetida con la salida fresca del código actual

> Misma procedencia que el resto: lo redactó la sesión que corrió la prueba y lo resume quien **no** reprodujo las mediciones.

**Por qué.** Las secciones 3 y 4 usaron la salida del 23/10, hecha con código anterior a #794 (integración ponderada por `nz`), #797 y
#805. Se generó una salida con `origin/main` `70c5c5c3` y se repitieron las 5 estaciones.

**Cómo se generó (reportado).** Las mismas 5 normales de entrada (su SHA-256 coincide con el del manifiesto del 23/10), mismos parámetros
(`directx`, bordes periódicos, `min_max`) y el mismo `texconv`. La salida se escribió **fuera de MO2**; el mod de salida del operador no se
tocó. **Sin `--incluir-revision` el generador no escribió nada**: las 5 quedaron `EN_REVISION` con motivo `SIN_UMBRALES`, porque todavía no
hay un registro de umbrales validado contra un corpus real. Eso es el comportamiento fail-closed esperado, y **esta prueba no valida el gate**.

**Fresca contra la del 23/10 (offline).** 0 de 5 idénticas en bytes, porque cambió el algoritmo (de `normal_fft_periodic_v1` a
`normal_wls_nz_periodic_v1`). Aun así los mapas son casi iguales: correlación 0,9995 a 0,9999 y diferencia media de 0,4 a 1,2 niveles sobre 255.

**En el juego (sesión B2: perfil A más el mod fresco).** Misma vara que antes (efecto ≥ 3 × ruido):

| Estación | Efecto ÷ ruido con la fresca | Con la del 23/10 |
|---|---|---|
| S1 pared | 159× | 198× |
| S2 mesa | 87× | 87× |
| S3 puerta | 572× | 573× |
| S4 techo | 165× | 165× |
| S5 piso | 100× | 101× |

El testigo sin relieve no cambió (0,019). Fresca y vieja no se distinguen en pantalla (diferencia de 0,5 a 1,0 contra efectos de 8 a 19).
La única anomalía, en S1, se explicó por un corrimiento de cámara de unos 3 píxeles.

**Los dos artefactos persisten con la salida fresca** (banda oscura en el borde de la puerta; pérdida del tinte verde del techo). Causa
**NO VERIFICADA**. **El operador no los notó por su cuenta** al mirar el juego (dato del propio operador, 30/10): los señaló la sesión al comparar
las capturas. Eso es información útil sobre su visibilidad, no una prueba de que no existan.

**Conclusión.** P1 se sostiene con el código actual: el cambio de algoritmo (#794) no altera el resultado en estas 5 texturas.
**Límites:** un solo observador, una sola sesión B2 (sin ciego nuevo), salida sin umbrales de gate validados y 5 texturas de una misma
familia. No dice nada sobre texturas donde #794 sí importa (normales con `nz < 0`) ni sobre P2.

**Hallazgos operativos.**
- El entorno virtual del operador importa `sky_claw` del **checkout principal** (más viejo) si no se fuerza la ruta al worktree. La sesión lo
  detectó y comprueba la ruta importada antes de correr. Conviene dejarlo documentado.
- Después de la restauración, alguien abrió MO2 con el perfil normal y lanzó el juego. Cambió solo el nombre localizado de 6 entradas del
  `modlist.txt`; lo habilitado y el orden son los mismos y `plugins.txt` y `loadorder.txt` son idénticos byte a byte.

