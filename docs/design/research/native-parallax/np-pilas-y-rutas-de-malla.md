# Parallax nativo: pilas de render (Community Shaders / ENB) y rutas de malla (Auto Parallax / PGPatcher)

> **Estado:** informe de investigación + decisión de diseño de dirección. **No es un ADR**: no cambia ningún
> contrato vigente (ADR 0014 sigue mandando). Registra lo que se averiguó con fuentes públicas el 2026-10-27 y
> lo que se decidió hacer con eso, para que no se pierda entre sesiones.
>
> **Clean-room:** solo se usaron fuentes públicas y de código abierto (documentación de Community Shaders,
> wiki de PGPatcher, documentación de ENB, foros y guías de la comunidad). No se leyó ni se usó la salida de
> ninguna herramienta cerrada (ver [CLEAN_ROOM.md](../../../../CLEAN_ROOM.md)).
>
> **Cómo leer las etiquetas:** `[repo]` = verificado leyendo este repositorio. Para lo externo se usa la
> taxonomía de `CLEAN_ROOM.md` (SUPPORTED_BY_UPSTREAM, INFERRED, NO VERIFIED, HYPOTHESIS, REAL_RIG_REQUIRED), definida al
> inicio de la sección 2; las fuentes con URL están en la sección 5. Ninguna afirmación de este documento fue
> probada dentro del juego.

## 1. Qué se decidió

1. **Una sola herramienta, dos perfiles de pila.** El archivo de relieve que genera el producto
   (`*_p.dds`, un canal, BC4, el `LegacyParallaxExporter` de la síntesis C) lo consumen tanto ENB como
   Community Shaders `[NO VERIFIED, ver 2.3]`. Lo que cambia entre pilas es cómo se **activa** y cómo se **verifica**, no el
   archivo. Por eso el selector de pila es un **perfil** dentro de la herramienta, no dos programas.
2. **Primero Community Shaders.** Es la pila que usa el dueño del proyecto y la única que se puede probar en
   su equipo. El perfil ENB se declara pero queda `[REAL_RIG_REQUIRED]` en juego hasta que alguien con ENB lo pruebe.
3. **PGPatcher es opcional, no requisito.** El dueño no lo usa hoy. La herramienta debe funcionar sin él y
   recomendarlo solo cuando una malla lo necesite.
4. **El chequeo de elegibilidad debe informar las dos rutas por separado** (ver §3), en vez de decidir con las
   reglas de una sola. Es una **decisión de diseño pendiente**: hoy `evaluar_elegibilidad` devuelve un único
   `EfectoPosible` por textura y modela solo la ruta Auto Parallax `[repo]`.

## 2. Hallazgos públicos que sostienen la decisión

Cada hallazgo cita su fuente en la sección 5 (URL, fecha de consulta y estado epistémico, con la taxonomía de
[CLEAN_ROOM.md](../../../../CLEAN_ROOM.md) «Trazabilidad»). Entre corchetes va el estado: **SUPPORTED_BY_UPSTREAM** = leído
en la fuente pública citada; **INFERRED** = deducción nuestra a partir de eso; **NO VERIFIED** = dicho por un
resumen o por búsqueda, sin leer la fuente primaria; **REAL_RIG_REQUIRED** = solo se confirma en el juego.

### 2.1 Auto Parallax (plugin SKSE de doodlum): la ruta que modela hoy `evaluar_elegibilidad`

Leído en el código fuente público (S1, S2):

- La activación automática la gobierna `bAutoEnableParallax`, sección `[Experimental]` del INI, cuyo valor
  distribuido es `false` [SUPPORTED_BY_UPSTREAM].
- Solo actúa si el material es de tipo `kDefault`, si el shader **tiene** la bandera de colores de vértice,
  si **no** tiene `kProjectedUV`, `kSkinned`, `kLODObjects`, `kTreeAnim` ni `kVertexLighting`, y si la geometría
  **no** tiene propiedad alfa [SUPPORTED_BY_UPSTREAM].
- Orden de búsqueda del `_p`: textura de altura del set, luego difuso con el sufijo `_d` cambiado a `_p`, luego
  normal con `_n` cambiado a `_p` [SUPPORTED_BY_UPSTREAM].
- Alcance por tipo de objeto: el módulo del repo `sky_claw/local/assets/auto_parallax_regla.py` documenta que el
  plugin solo actúa sobre objetos base Static, Movable Static y Container, dato que **no está en el NIF** y que
  debe declarar quien consuma la regla `[repo]`; **no se re-verificó hoy contra el código público** de S1
  [NO VERIFIED]. Por eso la lista anterior describe reglas de forma de la malla, no el alcance completo.
- Existe una rama de apagado para materiales de tipo parallax; no se leyó completa la condición que la dispara
  [NO VERIFIED]. La descripción pública del mod la presenta como red de seguridad para mallas con altura
  faltante (S3, resumen de búsqueda) [NO VERIFIED].

### 2.2 PGPatcher (hakasapl, GPL-3; antes ParallaxGen): la ruta que hoy NO modelamos

Citas literales de la página de la wiki «Patchers» (S4), obtenidas con una herramienta de fetch que devolvió
el texto entre comillas [SUPPORTED_BY_UPSTREAM, con la salvedad de que no se leyó la página en un navegador]:

- Requisitos del parche de parallax: mapa de parallax con prefijo que coincide con el difuso o la normal; la
  malla no se usa como registro `GRAS`; sin `MATO` de pasada única; sin `BSBehaviorGraphExtraData`; forma no
  *skinned*; sin `NiAlphaProperty`; shader de tipo `default`, `parallax` o `environment mapping` antes de
  parchear; no *decal*; sin banderas `soft lighting`, `rim lighting`, `back lighting` ni `anisotropic lighting`.
- Qué hace: agrega el mapa de altura a la ranura de parallax del set de texturas, fija el tipo de shader a
  `parallax`, limpia `environment mapping`, `multi-layer parallax` y `unused01` si existen, fija la bandera
  `parallax` y **«Enables vertex colors in both shape and shader»**.
- Esa página **no** contiene un orden de prioridad entre parches; la afirmación «PBR > Complex Material >
  Parallax» que figuraba en un borrador de este informe no tiene respaldo acá y se retira [NO VERIFIED].

**Consecuencia para el código** `[repo]`: el chequeo actual descarta mallas por `SIN_VERTEX_COLORS`. Para la
ruta Auto Parallax es correcto (2.1 exige la bandera); para la ruta PGPatcher **podría ser** un falso negativo, porque
PGPatcher habilita colores de vértice según su wiki [INFERRED de 2.1 + 2.2; REAL_RIG_REQUIRED]. Hoy el reporte
puede emitir `NO` para una textura que PGPatcher sí resolvería; no se probó con PGPatcher real.

### 2.3 Pilas de render

- ENB y Community Shaders no se usan a la vez [NO VERIFIED: afirmación de segunda mano, no leída en fuente
  primaria en esta consulta].
- Community Shaders: la página del mod (S5, resumen de búsqueda) dice que recomienda TAA o un upscaler con la
  opción de Complex Material por artefactos, que el parallax de terreno requiere que **todas** las texturas del
  terreno lo soporten y que conviene recomprimir a BC4 por ser un efecto limitado por ancho de banda
  [NO VERIFIED]. El ADR 0014 y la investigación previa del repo citan además la documentación DeepWiki de
  Extended Materials (S6), **no accesible desde el entorno de esta consulta**.
- ENB: se habilita desde `enbseries.ini` [NO VERIFIED; la clave exacta no se confirmó ahora].
- Contratos de altura según material (legado = canal rojo de `_p.dds` en BC4; Complex Material = alfa del mapa
  de entorno; terreno = alfa del difuso; TruePBR = `_p` bajo `textures/pbr`) están en la síntesis C del repo
  (`docs/design/research/2026-09-21-native-parallax-battle/50_sintesis_c.md`); esta consulta **no** los
  re-verificó.
- Que el mismo `_p.dds` legado sirva a ENB y a CS se toma de esa síntesis y de S6/S7 [NO VERIFIED hasta
  probarlo]; la premisa de la decisión 1 es una hipótesis [REAL_RIG_REQUIRED para CS; sin rig para ENB].

### 2.4 Criterio de calidad de los mapas de altura

«Gris medio = superficie en cero», «bajar el contraste de altura», «BC4 a media resolución alcanza» y «riesgo
de *banding* con 8 bits» son consejos de la comunidad sin fuente primaria citable [NO VERIFIED]. **No existe
fuente pública con valores exactos**: los umbrales del producto salen de la calibración propia (#804), no de
acá.

### 2.5 Orden con DynDOLOD / TexGen

Una búsqueda devolvió publicaciones donde el desarrollador de PGPatcher dice que DynDOLOD y TexGen se corren
**después** de PGPatcher y que su salida va última salvo las de DynDOLOD/TexGen (S8, S9), y otra donde el mismo
autor opinaba que el orden no importaba [NO VERIFIED: resumen de búsqueda, hilos no leídos]. La recomendación
provisional del proyecto es congelar la salida de parallax antes de generar LOD [HYPOTHESIS].

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

## 5. Fuentes (consultadas el 2026-10-27)

| Id | Fuente | Qué respalda | Cómo se obtuvo |
|---|---|---|---|
| S1 | https://github.com/doodlum/skyrim-auto-parallax/blob/main/src/XSEPlugin.cpp | condiciones de activación, orden de búsqueda del `_p`, ajustes del INI | fetch con extracción de citas; rama `main`, **sin SHA fijado** |
| S2 | https://github.com/doodlum/skyrim-auto-parallax/blob/main/package/SKSE/Plugins/AutoParallax.ini | `bAutoEnableParallax = false` | fetch del archivo crudo |
| S3 | https://nexusmods.com/skyrimspecialedition/mods/79473 | descripción del mod como red de seguridad | solo vía resultado de búsqueda (Nexus no accesible) |
| S4 | https://github.com/hakasapl/PGPatcher/wiki/Patchers | requisitos y efectos del parche de parallax | fetch con citas literales |
| S5 | https://www.nexusmods.com/skyrimspecialedition/mods/95134 | notas de Community Shaders sobre parallax, terreno y BC4 | solo vía resultado de búsqueda |
| S6 | https://deepwiki.com/doodlum/skyrim-community-shaders/5.1-extended-materials-and-parallax | Extended Materials en Community Shaders | citada por el ADR 0014; **inaccesible hoy** |
| S7 | https://schaken-mods.com/forums/topic/81106-enb-complex-material-guide-for-creators/ | guía de Complex Material para ENB | citada por la investigación previa del repo; **no leída hoy** |
| S8 | https://stepmodifications.org/forum/topic/21598-pgpatcher-by-hakasapl/ | orden respecto de DynDOLOD/TexGen | solo vía resultado de búsqueda |
| S9 | https://nexusmods.com/skyrimspecialedition/mods/120946?tab=posts | orden de carga de la salida de PGPatcher | solo vía resultado de búsqueda |

**Antes de codificar** la ruta PGPatcher o el perfil de pila hay que (a) fijar S1 y S4 a un SHA o a una copia
fechada y (b) leer S6/S7 desde un entorno con acceso. Las filas «solo vía búsqueda» no alcanzan como
especificación.
