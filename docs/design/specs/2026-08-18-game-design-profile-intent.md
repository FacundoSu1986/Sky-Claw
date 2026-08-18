# Game Design Profile — intención de diseño del usuario

> **Estado:** propuesta / pendiente de implementación  
> **Alcance de este documento:** registro de feature e intención arquitectónica.  
> **Código productivo:** ninguno.  
> **Fecha:** 2026-08-18

## Objetivo

Registrar como feature futura una capa de **Game Design Profile** para que el usuario pueda describir en lenguaje natural qué experiencia de Skyrim quiere construir, sin tener que expresar primero qué mods, plugins o herramientas concretas deben usarse.

Ejemplos de intención del operador:

- "Quiero un Skyrim brutal, donde pueda encontrar enemigos muy por encima de mi nivel".
- "Quiero que los minerales sean escasos y que el crafting requiera más esfuerzo".
- "Quiero loot poco abundante y una economía hostil".
- "Quiero encuentros menos predecibles y un mundo más peligroso".
- "No quiero que se alteren quests principales ni NPC críticos".

La feature futura debe traducir esa intención a un **contrato estructurado y validado** antes de que cualquier planner o herramienta externa pueda proponer cambios.

## Norte de diseño

La intención del usuario **no es una orden ejecutable**.

El flujo conceptual deseado es:

```text
Usuario
  -> intención en lenguaje natural
  -> GameDesignProfile validado
  -> análisis del estado real de la modlist
  -> propuestas de capacidad / ModificationPlan
  -> ActionManifest / preview
  -> aprobación humana
  -> herramientas deterministas
  -> validación post-run
  -> FlightReport / rollback
```

Este diseño debe respetar ADR 0002: Sky-Claw sigue siendo una caja negra de vuelo con controles humanos, no un agente autónomo irrestricto.

## Boundary propuesto

La feature debería vivir, si se implementa, como un dominio con nombre propio bajo `sky_claw/app/game_design/` o un boundary equivalente aprobado por ADR/spec futura.

Responsabilidad del dominio:

- representar objetivos de experiencia de juego;
- interpretar lenguaje natural a un perfil estructurado;
- declarar restricciones y protecciones duras;
- comparar el objetivo contra el estado observado de la modlist;
- producir recomendaciones o planes declarativos.

Fuera de responsabilidad:

- ejecutar LOOT;
- ejecutar xEdit;
- generar Bashed Patch;
- ejecutar Synthesis;
- instalar mods;
- editar `plugins.txt` o `modlist.txt`;
- tocar un perfil MO2;
- modificar ESP/ESM/ESL directamente;
- controlar Skyrim en runtime.

## Contrato conceptual

El primer contrato futuro debería ser un DTO/versionado equivalente a `GameDesignProfile`.

Dimensiones candidatas, no congeladas:

- combate y peligro;
- escalado respecto al nivel del jugador;
- progresión;
- economía;
- escasez de recursos;
- loot;
- crafting;
- aleatoriedad de encuentros;
- protecciones de quests y NPC críticos;
- preferencias de estabilidad/compatibilidad.

Los nombres, rangos y semántica exacta de los campos deben definirse en una implementación posterior. No se consideran decididos por esta spec.

## Principio de autoridad

La futura IA puede interpretar intención y proponer hipótesis, pero no debe convertirse en autoridad final de mutación.

```text
LLM                -> interpreta intención / propone
GameDesignProfile  -> contrato validado
Planner             -> propone capacidades y cambios
LOOT/xEdit/Bash/... -> validan o ejecutan dentro de su dominio
Skyrim              -> autoridad runtime
```

Una salida del LLM que no pueda validarse contra un schema conocido debe rechazarse, no degradarse silenciosamente a texto libre.

## Capability Resolver futuro

La feature debería poder clasificar cada objetivo según la capacidad real disponible, con estados explícitos equivalentes a:

```text
SUPPORTED
PARTIAL
REQUIRES_MOD
REQUIRES_RUNTIME
UNKNOWN
UNSUPPORTED
```

`UNKNOWN` es obligatorio conceptualmente: si Sky-Claw no sabe qué mecanismo de Skyrim controla una intención, debe declararlo en vez de inventar una ruta de ejecución.

Ejemplo:

```text
Objetivo: "cada veta debe entregar un solo mineral"

Si no está verificado si el comportamiento depende de records, scripts,
Game Settings o de un mod concreto:

capability = UNKNOWN
```

## Relación futura con herramientas existentes

El Game Design Profile no reemplaza herramientas especializadas.

Posibles backends futuros:

- **LOOT**: load order y metadata propuesta/validada;
- **xEdit**: análisis y patching de records cuando exista una estrategia segura;
- **Wrye Bash**: leveled lists y Bashed Patch donde corresponda;
- **Synthesis**: patchers reproducibles de dominio;
- **MO2**: perfil, orden de mods y sandbox/promoción;
- **World Director**: comportamiento dinámico futuro en runtime.

La selección del backend debe depender de capacidades verificadas, no de preferencias improvisadas del LLM.

## Relación futura con SkyClaw World Director

World Director queda fuera del alcance de esta feature inicial.

A largo plazo, podría consumir un contrato derivado de `GameDesignProfile` con políticas runtime, por ejemplo peligro, aleatoriedad de encuentros o protecciones de NPC. Esa integración debe mantenerse separada hasta que World Director tenga contratos runtime implementados y verificados.

## Seguridad e integridad

Cuando esta feature llegue a mutar estado, deberá reutilizar los mecanismos existentes de Sky-Claw en lugar de crear caminos paralelos:

- preflight;
- ActionManifest/preview;
- aprobación HITL;
- ProfileSandbox cuando aplique;
- snapshots;
- journal;
- rollback;
- validadores deterministas;
- FlightReport.

No se debe permitir un camino `texto del usuario -> LLM -> xEdit/LOOT` que eluda estos boundaries.

## Fases candidatas

Este documento no abre implementación, pero registra una secuencia recomendada para futuras tareas:

1. **GD-00 — Diseño:** ADR/spec del contrato y semántica exacta.
2. **GD-01 — Perfil:** DTO `GameDesignProfile` inmutable, versionado y validado; sin LLM.
3. **GD-02 — Interpreter:** lenguaje natural -> perfil estructurado; sin herramientas mutantes.
4. **GD-03 — Capability Planner:** clasificar qué objetivos son soportados, parciales, desconocidos o requieren runtime/mods.
5. **GD-04 — Modlist comparison:** comparar el perfil con mods, load order y evidencia xEdit disponible; solo lectura.
6. **GD-05 — ModificationPlan:** producir propuestas declarativas sin ejecutarlas.
7. **GD-06 — Preview/HITL:** integrar propuestas al manifiesto y aprobación existentes.
8. **GD-07 — Execution adapters:** permitir mutaciones solo mediante los rituales y runners existentes.
9. **GD-08 — Post-validation:** medir resultado, FlightReport y rollback.
10. **Futuro — Runtime policy:** contrato opcional para SkyClaw World Director.

La numeración `GD-*` es local a esta propuesta y no reemplaza ni consume identificadores `T-*` del backlog actual.

## Criterios para empezar implementación

Antes de GD-01 deberían quedar respondidas y documentadas, como mínimo:

- qué dimensiones forman parte de `GameDesignProfile` v1;
- cuáles son objetivos blandos y cuáles restricciones duras;
- cómo se versiona el schema;
- qué significa cada rango/enum y cómo se evita ambigüedad semántica;
- qué protecciones son obligatorias por defecto;
- cómo se expresa `UNKNOWN` sin que el LLM lo convierta en una falsa certeza;
- qué evidencia mínima necesita un planner para afirmar `SUPPORTED`;
- cómo se evita que esta feature cree una tercera ruta de ejecución distinta de las ya existentes.

## No objetivos de este PR

Este PR documental **no**:

- implementa `GameDesignProfile`;
- agrega feature flags;
- modifica configuración;
- toca GUI/CLI/Telegram;
- modifica LLMRouter o providers;
- registra tools nuevas;
- modifica LOOT, xEdit, Wrye Bash o Synthesis;
- altera workflows existentes;
- integra World Director;
- modifica tests productivos;
- afirma que una capacidad Skyrim específica ya está soportada.

## Criterio de aceptación de esta propuesta

La propuesta queda correctamente registrada si:

1. la intención de producto puede entenderse sin leer conversaciones externas;
2. se mantiene explícita la separación entre intención, planificación y ejecución;
3. no se introduce ninguna capacidad mutante;
4. quedan registrados los no-objetivos y riesgos;
5. una implementación futura puede dividirse en PRs pequeños y reversibles;
6. cualquier afirmación de soporte real queda pendiente de evidencia y tests posteriores.
