# ADR 0001 — Estrategia definitiva para conflictos de leveled lists

**Fecha:** 2026-07-06
**Estado:** Aceptada
**Contexto de origen:** P0 de `TECHNICAL_REVIEW.md` §4.1; tareas T-01/T-02/T-03/T-04 de `TECHNICAL_REVIEW_TASKS.md`.

## Contexto

El "merged patch" propio de Sky-Claw (estrategia `CreateMergedPatch` + scripts
Pascal de merge) copiaba la **primera** versión de cada FormID iterada — el
master base, es decir el *perdedor* por reglas de load order — y descartaba los
overrides ganadores como duplicados. Resultado: el parche podía **revertir**
los cambios de leveled lists de toda la modlist.

Estado tras la contención:

- **T-01:** `CreateMergedPatch` fuera de las estrategias por defecto;
  `generate_script_from_plan` rechaza planes `CREATE_MERGED_PATCH`; los
  conflictos LVLI/LVLN/LVSP fallan explícito recomendando el Bashed Patch.
- **T-02:** ambos scripts de merge (estático y template) aplican el guard
  `WinningOverride` — ahora son un "forward del ganador" inocuo, pero **no
  fusionan entradas**: un forward del ganador no combina los agregados de dos
  overhauls, solo elige uno.

Opciones evaluadas:

**(a) Merge real propio** — implementar la unión de entradas LVLI/LVLN/LVSP
con semántica Relev/Delev en los scripts Pascal.
**(b) Delegar al Bashed Patch de Wrye Bash** — ya integrado en el repo
(`WryeBashRunner.generate_bashed_patch`, tool de agente
`generate_bashed_patch`, fase 6 del supervisor).
**(c) Esperar la integración de Mator Smash** — no existe hoy en el repo.

## Decisión

**Opción (b): los conflictos de leveled lists se delegan al Bashed Patch de
Wrye Bash.** El orquestador de parcheo produce un plan de tipo
`DELEGATE_BASHED_PATCH` en vez de generar un script xEdit propio, y la capa de
servicio enruta ese plan hacia el flujo de Wrye Bash en lugar de ejecutar
xEdit.

Razones:

1. **Es la herramienta correcta del dominio.** El merge de leveled lists
   (unión de entradas + Relev/Delev) es exactamente la especialidad histórica
   del Bashed Patch; reimplementarlo en Pascal propio duplica, con menos
   madurez, lo que Wrye Bash hace hace 15 años.
2. **Menos código propio en la zona de mayor riesgo.** El P0 demostró el costo
   de mantener semántica de merge propia sin validación en rig real. La
   opción (a) reintroduce esa superficie.
3. **La integración ya existe.** `WryeBashRunner` está probado y expuesto como
   tool; delegar es cablear, no construir.

## Consecuencias

- `CreateMergedPatch` y sus scripts quedan **permanentemente deshabilitados**
  para producción; el guard `WinningOverride` de T-02 se conserva como
  documentación y para el caso de ejecución manual del script.
- La estrategia `DelegateToBashedPatch` (prioridad 10, tipos LVLI/LVLN/LVSP)
  reemplaza a `CreateMergedPatch` en el registro por defecto (T-04).
- `PatchResult` expone el `strategy_type` del plan para que el servicio enrute
  por el tipo **seleccionado** (el enrutado previo adivinaba mirando
  `strategies[0]`, un bug latente).
- El servicio xEdit no ejecuta nada para planes delegados: devuelve éxito con
  la indicación explícita de correr el Bashed Patch. El encadenamiento
  automático (que el supervisor dispare `generate_bashed_patch` al recibir la
  delegación) es un follow-up deliberado, no parte de esta decisión.
- Si Wrye Bash no está instalado, el mensaje lo dice y apunta a la descarga
  (el discovery ya lo detecta como herramienta).

## Criterio de reversión

Reconsiderar la opción (a) — o una (c) con Mator Smash — solo cuando existan
**ambas**: (1) análisis de conflictos por subrecord (Oleada 4 del backlog,
T-17/T-19) que permita verificar qué entradas preserva un merge, y (2) la
matriz de smoke real en rig Skyrim+MO2 (T-25) para validar el resultado contra
el Bashed Patch de referencia. Hasta entonces, todo merge de leveled lists
pasa por Wrye Bash.
