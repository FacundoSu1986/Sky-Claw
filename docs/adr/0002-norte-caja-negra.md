# ADR 0002 — Norte del producto: "caja negra de vuelo", no agente autónomo

**Fecha:** 2026-07-07
**Estado:** Aceptada
**Contexto de origen:** ciclo OODA sobre `TECHNICAL_REVIEW.md` y su backlog
(`TECHNICAL_REVIEW_TASKS.md`), contrastado con la visión de producto propuesta
por el owner ("la caja negra de vuelo de tu modlist").

## Contexto

El modding de Skyrim no premia la autonomía ciega; premia saber **exactamente
qué cambió, por qué, quién ganó cada conflicto y cómo volver atrás**. Un
veterano confía en una herramienta cuando puede inspeccionar lo que va a hacer
antes de que lo haga, y auditar lo que hizo después.

La visión propuesta reorienta Sky-Claw de "agente autónomo que arregla Skyrim"
a "caja negra de vuelo de la modlist": trazabilidad, evidencia y rollback como
producto, con la automatización al servicio de la explicación — no al revés.

Contrastada contra el repo (`main @ a17d9d3`), la visión es en su mayor parte
**consistente con el plan ya en ejecución**; este ADR la formaliza como norte
y registra las piezas genuinamente nuevas:

| Punto de la visión | Estado en el repo |
|---|---|
| Mator Smash como opcional, no pilar | ✅ Decidido en ADR 0001 (leveled lists → Bashed Patch; Mator Smash solo bajo el criterio de reversión de ese ADR) |
| Preflight brutal antes de cualquier tool | 🔶 En ejecución: T-13/T-14/T-15 mergeados (`sky_claw/local/validators/preflight.py` — sensores VFS + versión LOOT); T-16 (panel GUI) y el cableado al resto de los mutantes, en curso |
| Motor de reglas de conflicto (no solo detector) | 🔶 En backlog: T-19a/T-19b (`Manual Cost Calc` como primera regla declarativa), T-20 (asistente de estrategia) |
| UX de confianza ("panel de cirugía") | 🔶 Parcial: T-16/T-20/T-21; faltaba el panel por subrecord + "abrir en xEdit" (§5.5 del review, sin tarea) → **T-29** |
| Menos deuda estructural antes de crecer | 🔶 Incremental: T-10/T-11 (BLE001 por carpeta), T-12 (mypy módulo a módulo) |
| Manifiesto por acción con rollback | ❌ Solo existe el preview de la cadena (`sky_claw/antigravity/orchestrator/preview/manifest.py`); §4.6 del review no tenía tarea → **T-26** |
| Clonar el perfil MO2 antes de operar | ❌ No existe (`sky_claw/local/mo2/` no tiene clonado de profiles) → **T-27** |
| Informe final por corrida | ❌ No existe como concepto → **T-28** |

## Decisión

**Sky-Claw es la caja negra de vuelo de la modlist.** Toda capacidad nueva se
evalúa contra esa vara: ¿deja al usuario ver qué va a cambiar, por qué, con qué
evidencia, y cómo volver atrás? La autonomía es un medio subordinado a la
trazabilidad, nunca el objetivo.

El flujo must-have que define "terminado" a nivel producto:

```
clonar perfil MO2 → preflight → analizar → explicar conflictos →
proponer patch → aprobación humana → ejecutar → validar → informe final
```

### Sub-decisiones

1. **Manifiesto por acción (T-26).** Todo Ritual mutante produce, antes de
   ejecutar, un manifiesto inspeccionable: qué archivos toca, qué plugins/
   records forwardea, con qué herramienta y versión, y cuál es el plan de
   rollback. Se **extiende** `orchestrator/preview/manifest.py` (hoy limitado
   al preview de la cadena LOOT→xEdit→DynDOLOD→bashed) y se persiste en el
   journal — no se crea un contrato paralelo.
2. **ProfileSandbox (T-27).** Los rituales mutantes operan sobre un clon del
   perfil MO2; el perfil real solo se toca al promover un diff aprobado
   (`plugins.txt`/`modlist.txt`/`overwrite`).
3. **Informe final de vuelo (T-28).** Al terminar cada Ritual: qué cambió, por
   qué, quién ganó cada conflicto y cómo revertir. Ensambla journal +
   manifiesto (T-26) + post-run validator (T-21); no inventa datos nuevos.
4. **Mator Smash sigue fuera del núcleo.** Se reafirma ADR 0001: el core es
   xEdit + reglas semánticas propias + Wrye Bash/Synthesis donde corresponda.
   Mator Smash solo como estrategia opcional y bajo el criterio de reversión
   del ADR 0001 (subrecord-analysis + rig real primero).
5. **Aprobación primaria en la GUI.** El approval gate del preview
   (`orchestrator/preview/approval_gate.py`) es el canal HITL primario;
   Telegram queda como canal secundario. Se reafirma además la decisión ya
   documentada de que la capa del agente LLM es lock-only, sin HITL propio.
6. **Boundaries de dominio para el código nuevo.** Las piezas nuevas nacen en
   dominios con nombre — `ModlistAnalyzer` (análisis), `PatchPlanner`
   (planificación/estrategias), `ToolRunner` (procesos externos),
   `ProfileSandbox` (perfiles), `ConflictRuleEngine` (reglas declarativas) —
   continuando el patrón ya iniciado por `VfsHealthChecker` y
   `LoadOrderSnapshotService`. **No** se adopta "deuda primero, features
   después" literal: la reducción de deuda sigue incremental (T-10/T-11/T-12)
   y el orden de riesgo del review §6 se mantiene (runners antes que GUI).

## Consecuencias

- `TECHNICAL_REVIEW_TASKS.md` incorpora la **Oleada 7 — Caja negra de vuelo**
  (T-26..T-30) sin alterar las Oleadas 0–6 ni el trabajo en vuelo (T-16, T-11,
  cableado de preflight en xEdit/Synthesis/DynDOLOD), que esta visión refuerza.
- T-27 y T-28 se suman al criterio de GA: la matriz de smoke real (T-25) debe
  ejercitar el flujo must-have completo, no solo herramientas sueltas.
- Las features que no aporten trazabilidad/evidencia/rollback bajan de
  prioridad por defecto frente a las que sí.

## Criterio de reversión

Si tras implementar T-26..T-28 el costo de mantener manifiestos y sandbox de
perfiles supera su valor demostrado (medido en la matriz T-25: tiempo de
corrida, fricción de aprobación, bugs propios del clonado), reconsiderar el
alcance del sandbox (p. ej. limitarlo a rituales de alto riesgo) — pero el
manifiesto y el informe final no se revierten: son el producto.
