# Parallax nativo: pilas de render (Community Shaders / ENB) y rutas de malla (Auto Parallax / PGPatcher)

> **Estado:** informe de investigación + decisión de diseño de dirección. **No es un ADR**: no cambia ningún
> contrato vigente (ADR 0014 sigue mandando). Registra lo que se averiguó con fuentes públicas el 2026-10-27 y
> lo que se decidió hacer con eso, para que no se pierda entre sesiones.
>
> **Clean-room:** solo se usaron fuentes públicas y de código abierto (documentación de Community Shaders,
> wiki de PGPatcher, documentación de ENB, foros y guías de la comunidad). No se leyó ni se usó la salida de
> ninguna herramienta cerrada (ver [CLEAN_ROOM.md](../../../../CLEAN_ROOM.md)).
>
> **Cómo leer las etiquetas:** `[repo]` = verificado leyendo este repositorio; `[público]` = dicho por una
> fuente pública, no ejecutado acá; `[sin verificar]` = resumen de segunda mano o inferencia. Ninguna
> afirmación de este documento fue probada dentro del juego.

## 1. Qué se decidió

1. **Una sola herramienta, dos perfiles de pila.** El archivo de relieve que genera el producto
   (`*_p.dds`, un canal, BC4, el `LegacyParallaxExporter` de la síntesis C) lo consumen tanto ENB como
   Community Shaders `[público]`. Lo que cambia entre pilas es cómo se **activa** y cómo se **verifica**, no el
   archivo. Por eso el selector de pila es un **perfil** dentro de la herramienta, no dos programas.
2. **Primero Community Shaders.** Es la pila que usa el dueño del proyecto y la única que se puede probar en
   su equipo. El perfil ENB se declara pero queda `[sin verificar]` en juego hasta que alguien con ENB lo pruebe.
3. **PGPatcher es opcional, no requisito.** El dueño no lo usa hoy. La herramienta debe funcionar sin él y
   recomendarlo solo cuando una malla lo necesite.
4. **El chequeo de elegibilidad informa las dos rutas por separado** (ver §3), en vez de decidir con las
   reglas de una sola.

## 2. Hallazgos públicos que sostienen la decisión

### 2.1 Auto Parallax (plugin SKSE de doodlum) — la ruta que modela hoy `evaluar_elegibilidad` `[público]`

- Funciona como red de seguridad: apaga parallax en mallas cuyo `_p` falta.
- Su activación automática es **experimental** y viene apagada (INI, sección `[Experimental]`,
  `bAutoEnableParallax`, por defecto `false`).
- Aun encendida, solo actúa sobre mallas con shader tipo *default*, con colores de vértice presentes, sin
  `NiAlphaProperty` y con un conjunto de texturas presente. Excluye UV proyectado, mallas *skinned*, LOD de
  objetos, animación de árbol e iluminación por vértice.
- Busca el `_p` en este orden: ranura de altura, derivado del difuso, derivado de la normal.

### 2.2 PGPatcher (hakasapl, GPL-3; antes ParallaxGen) — la ruta que hoy NO modelamos `[sin verificar]`

Resumen obtenido con una herramienta de fetch que resume (no es lectura directa de la wiki):

- Requisitos del parche de parallax: el `_p` coincide con el prefijo del difuso o de la normal; no es hierba
  (GRAS); no usa MATO de pasada única; no tiene datos de comportamiento Havok; no es *skinned*; no tiene
  `NiAlphaProperty`; el shader es *default*, *parallax* o *environment mapping*; no es *decal*; no usa
  iluminación *soft*, *rim*, *back* ni *anisotropic*.
- Qué modifica: agrega el mapa de altura a la ranura de parallax, fija el tipo de shader a parallax, limpia
  banderas de envmap/multilayer, fija la bandera de parallax y **habilita colores de vértice** en forma y shader.
- Parche de Complex Material aparte; prioridad PBR > Complex Material > Parallax; produce un diff
  (`ParallaxGen_Diff.json`).

**Consecuencia para el código `[repo]`:** el chequeo actual descarta mallas con `SIN_VERTEX_COLORS`. Para la
ruta Auto Parallax eso es correcto; para la ruta PGPatcher es un **falso negativo**, porque PGPatcher
habilita justamente los colores de vértice. Hoy el reporte puede decir `NO` sobre una malla que PGPatcher
sí resolvería.

> Antes de codificar la ruta PGPatcher hay que **leer la wiki línea por línea**; este resumen no alcanza como
> especificación.

### 2.3 Pilas de render `[público]`

- ENB y Community Shaders son **mutuamente excluyentes**.
- Community Shaders: *Extended Materials* es el antiguo Complex Parallax Materials, integrado al núcleo.
  Recomienda un upscaler/TAA para la calidad alta; el parallax de terreno requiere que **todas** las texturas
  del terreno tengan mapa de altura; se recomienda BC4 para ahorrar ancho de banda.
- ENB: se habilita desde `enbseries.ini` (por ejemplo `EnableComplexParallax`).
- Contratos de altura distintos según el material: parallax legado = canal rojo de `_p.dds` (BC4); Complex
  Material = alfa del mapa de entorno (BC3/BC7); terreno = alfa del difuso (127 = neutro); TruePBR = `_p`
  bajo `textures/pbr`. La síntesis C difiere Complex Material, terreno y TruePBR.

### 2.4 Criterio de calidad de los mapas de altura `[público]`

Gris medio = superficie en cero; conviene bajar el contraste de altura; BC4 a media resolución es
suficiente para altura; con 8 bits hay riesgo de *banding*. **No existe fuente pública con valores exactos**:
los umbrales del producto siguen saliendo de la calibración propia (#804), no de acá.

### 2.5 Orden con DynDOLOD / TexGen `[sin verificar]`

El desarrollador de PGPatcher recomienda correrlo antes de DynDOLOD/TexGen; hay reportes de usuarios que
contradicen. La recomendación provisional del proyecto es **congelar la salida de parallax antes de generar LOD**.
Es una hipótesis operativa, no un hecho.

## 3. Qué cambia en el producto (en orden, cada paso con su PR)

| # | Cambio | Estado |
|---|---|---|
| 1 | Este documento. | este PR |
| 2 | `evaluar_elegibilidad` informa **dos veredictos por textura**: ruta Auto Parallax y ruta PGPatcher, con causas separadas, tras verificar la wiki de PGPatcher. | pendiente |
| 3 | Perfil de pila (CS / ENB) como parámetro del diagnóstico: qué flag se chequea y qué advertencia se muestra. | pendiente |
| 4 | Plan de prueba en equipo real: ruta de mallas elegible según lo que el dueño tenga instalado (PGPatcher opcional). | pendiente |
| 5 | Invocar PGPatcher como herramienta externa administrada. **Requiere decisión propia**: es una superficie nueva que muta archivos y debe cumplir la regla de "dos superficies, un recurso" (GUI y agente LLM). | no iniciado |

## 4. Límites honestos

- Nada de esto se probó en el juego. La primera prueba en el equipo del dueño fue inconclusiva.
- No hay comparación contra ParallaxR ni contra ninguna otra herramienta; la política clean-room limita cómo
  se puede hacer (comparación ciega como evaluación, nunca como etiqueta de entrenamiento).
- Las páginas de Nexus no fueron accesibles desde el entorno de investigación; lo que se cita de ahí es de
  segunda mano.
- El lector de mallas SSE (`BSTriShape`) sigue sin verificarse contra mallas reales.
- El perfil ENB no puede validarse sin una instalación ENB.
