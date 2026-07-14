# OODA — inventario de pendientes (2026-07-13)

**Snapshot contra:** `main @ b00d403`.
**Método:** Observe/Orient — se verificó cada ítem contra el **código actual**
(no solo contra los docs de backlog, que están desactualizados en varios
puntos: `TECHNICAL_REVIEW_TASKS.md` es del 2026-07-06 y no refleja el trabajo
mergeado desde entonces). Reemplaza la necesidad de releer `TECHNICAL_REVIEW_TASKS.md`,
`ZERO_TRUST_TODO.md` y el `skyclaw_ooda_analysis.md` completos para saber "qué falta".

Este documento es un **snapshot**, no una fuente viva — quedará desactualizado
a medida que se mergeen PRs. Reverificar contra el código antes de actuar sobre
cualquier ítem, como pide `AGENTS.md`.

## Resumen ejecutivo

- **Arbitraje de bugs (OODA analysis, 34 hallazgos):** completo. Todos los
  CRÍTICOS y HIGH accionables están cerrados, mitigados o evaluados
  (ADR 0003, ADR 0004). Lo que resta es cosmético o de bajo impacto — ver §3.
- **Roadmap de producto "caja negra de vuelo" (ADR 0002, Oleada 7):** T-30 y
  T-29 sí cerrados. **T-26, T-27 y T-28 están sobre-declarados como "cerrados"
  en el historial de commits — la cobertura real es solo LOOT**, ver §1
  (corregido tras review de Codex sobre el primer draft de este mismo
  documento — la ironía es la prueba de campo del problema que motiva esta
  nota: verificar "un archivo" no es lo mismo que verificar "todo caller en
  el árbol").
- **Deuda estructural continua (T-10/T-11/T-12):** en curso, sin bloquear GA
  — ver §2.
- **GUI/accesibilidad (Oleada 5) y smoke E2E (Oleada 6):** nunca arrancadas
  — ver §4.

---

## 1 — Gaps reales en el roadmap "caja negra de vuelo" (mayor valor)

Verificados por grep del **árbol completo de callers**, no de un archivo
puntual ni del commit-message del PR que cerró la tarea (draft anterior de
este documento cometió exactamente ese error — corregido gracias al review de
Codex en #290, ver evidencia por ítem).

### 1.1 T-26/T-28 — ActionManifest y FlightReport: solo LOOT los emite en producción

`persist_action_manifest` y `persist_flight_report` (`journal.py:745,784`) son
la infraestructura de persistencia — **cualquier** runner podría llamarlas,
pero en producción **solo `loot_service.py` lo hace**:

```
$ grep -rln "persist_action_manifest\|persist_flight_report" sky_claw/ | grep -v test
sky_claw/antigravity/db/journal.py       # la definición
sky_claw/local/tools/loot_service.py     # el único caller de producción
```

`xedit_service.py` y `dyndolod_service.py` importan `preview.manifest` —
un modelo **distinto**, el de preview de la cadena LOOT→xEdit→DynDOLOD
(dry-run, pre-aprobación), no el `ActionManifest` persistido por-Ritual que
T-26 exigía. `synthesis_service.py`, `pandora_service.py` y
`wrye_bash_runner.py` no tienen **ninguna** referencia a manifest/flight
report.

**Consecuencia real:** el criterio de aceptación de T-26 ("todo Ritual
mutante produce" el manifiesto) y el de T-28 ("informe... por Ritual") **no
se cumplen** — se cumplen solo para LOOT. No es "backend listo, falta GUI"
(lo que decía el draft anterior de este doc): falta cablear el backend mismo
en xEdit/Synthesis/DynDOLOD/Pandora/Wrye Bash, y **recién después** tiene
sentido construir la vista GUI de T-28 (mostrar un informe que hoy, para la
mayoría de los Rituales, no existiría).

### 1.2 T-27 — El sandbox nunca se activa en ningún Ritual real (no es solo "falta Pandora")

El draft anterior decía "T-27b·1 (#258) cableó Synthesis" y que solo faltaba
Pandora. Es impreciso: T-27b·1 construyó el **seam** de inyección
(`SynthesisPipelineService` acepta un `output_path` override apuntable a
`SandboxClone.overwrite_copy`) y el orquestador `run_ritual_in_sandbox()`
(`sky_claw/local/mo2/sandbox_run.py`) que sabe clonar/correr/diff/promote.
Pero **nada en producción invoca `run_ritual_in_sandbox`**:

```
$ grep -rln "run_ritual_in_sandbox" sky_claw/ | grep -v test
sky_claw/local/mo2/sandbox_run.py    # solo la propia definición
```

El propio docstring de `sandbox_run.py` es honesto al respecto: documenta que
Pandora/DynDOLOD/bashed "no son redirigibles hoy", pero no aclara que
**Synthesis tampoco corre sandboxeado en la práctica** — el seam existe, el
orquestador existe, pero ningún dispatcher de Ritual real los conecta. Es el
mismo patrón "sensor sin cablear = verde mentiroso" que el propio historial
del repo ya señaló una vez para el preflight (mensaje de PR #250). La
garantía central de T-27 ("ningún Ritual mutante escribe... en el overwrite
compartido real cuando el sandbox está activo") **no es cierta para ningún
runner hoy**, no solo para Pandora.

### 1.3 Preflight sin cablear: agregar Synthesis a la lista (no solo DynDOLOD/Pandora/Wrye Bash)

`TECHNICAL_REVIEW_TASKS.md:16` nombra explícitamente **xEdit/Synthesis/
DynDOLOD** como los mutantes pendientes de preflight — el draft anterior de
este doc, basado solo en el follow-up de la descripción de #288, omitió
Synthesis (#288 cubrió el subset de xEdit QuickAutoClean, no Synthesis).
`synthesis_service.py` no tiene ningún gate de `PreflightService` antes de
`execute_pipeline`. Hoy el semáforo cubre solo LOOT y el subset de xEdit
QuickAutoClean (T-16c·1, #288); DynDOLOD, Pandora y Wrye Bash tampoco lo
tienen — mismo patrón de riesgo que motivó T-15 originalmente (mutantes
corriendo sin validación previa).

---

## 2 — Deuda estructural continua (no bloquea GA, mantenimiento de fondo)

### 2.1 T-12 — mypy estricto módulo a módulo

`pyproject.toml:213` sigue con `ignore_errors = true` para el grueso del
árbol: *"Currently 1,684 mypy errors across ~30 modules"* (comentario
desactualizado en número exacto, pero la exención sigue activa). Se migra de
a un módulo por PR; sin fecha límite.

### 2.2 T-10/T-11 — BLE001 (except genérico) sin activar en la mayoría del árbol

Cerrados solo `local/tools/` (T-10) y `local/xedit/` (T-11). La lista de
exenciones en `pyproject.toml:130-142` sigue cubriendo, entre otros:
**todo `sky_claw/antigravity/**`** (el árbol más grande del repo — orchestrator,
gui, agent, security, comms, core, db), más `local/mo2/`, `local/validators/`,
`local/discovery/`, `local/loot/`, `local/fomod/`, `local/assets/`. Es la
exención más grande que queda del backlog original.

### 2.3 Zero-Trust — 2 ítems residuales (heredados de `ZERO_TRUST_TODO.md`, fusionado en esta nota)

`ZERO_TRUST_TODO.md` declaraba todo "Completado ✅" salvo una excepción
documentada, pero su sección "Acción recomendada futura" tenía 2 ítems
genuinamente abiertos (verificados, no en el resumen ✅ del propio doc — el
mismo patrón de esta nota completa: el checkbox de arriba no reflejaba el
contenido de abajo):

1. **Migrar `PathResolutionService` de `os.environ` a `config.toml` puro.**
   Vigente: `path_resolver.py` sigue con ~10 sitios `os.environ.get(...)`. Es
   la excepción ya documentada y aceptada como "único punto centralizado
   permitido" — no es urgente, pero sigue sin fecha.
2. **Consolidar secretos en `CredentialVault.get_key(name)` con backend
   keyring.** No existe: `grep -rn "def get_key" credential_vault.py` no
   encuentra nada. Sin empezar.

Nota al margen: `ritual_runner.py:342` escribe (`os.environ[env_name] = ...`,
no lee) fuera del punto centralizado — sembrando la var justo tras instalar
una tool para que el resolver la vea sin esperar un rescan. Es una escritura
legítima, no una violación de la regla de "lectura única" del doc original
(que nunca contempló el caso de escritura), pero deja la regla incompleta si
se retoma el ítem 1.

---

## 3 — Residuales del OODA analysis (34 hallazgos) — bajo valor, ya evaluados

Todos verificados como abiertos pero de mérito bajo (ver conversación previa
para el detalle completo por hallazgo):

- **PS-1** — fecha hardcodeada `"ABRIL 2026"` en `purple_security_agent.py:115`
  (cosmético, un prompt de LLM).
- **S-3** — `handle_execution_signal` no sanitiza el payload (bajo riesgo: es
  una señal disparada desde la GUI local, no una superficie de red).
- **SC-2** — sin rate-limit en `audit_repository` (sin path de abuso real:
  no está expuesto como tool al LLM).
- Deuda MEDIUM/LOW restante (SG-3/SG-4/SG-5/TD-1/TD-2/XE-2/XE-3/SS-2/SS-3/LS-1/LS-2/G-3/G-4/G-5/CV-4/R-4/R-5/R-6/P-4/E-5/PS-2/PS-4) —
  refactors de forma (lógica dispersa, duplicación, ambigüedad de contratos),
  no bugs.

**Recomendación:** no abrir más PRs de "caza de bugs" sobre este análisis —
el ratio esfuerzo/valor ya cruzó a negativo. Si se retoma, agruparlos en un
único PR de limpieza cosmética en vez de uno por hallazgo.

---

## 4 — Nunca empezadas

### 4.1 Oleada 5 — GUI/accesibilidad (T-22/T-23/T-24)

Cero commits en todo el historial (`git log --oneline --all | grep -iE
"T-2[234]"` → vacío). Transiciones específicas + `prefers-reduced-motion`
(T-22), virtualización de listas >200 mods (T-23), labels reales + focus
visible (T-24). Bajo riesgo técnico, valor de accesibilidad/pulido.

### 4.2 Oleada 6 — T-25, matriz de smoke E2E real

Bloqueada estructuralmente: requiere **humano + rig real** (instalación de
Skyrim/MO2/SSEEdit/DynDOLOD real) — no ejecutable por un agente en CI. Es el
criterio de aceptación de GA del proyecto entero, así que sigue siendo el
verdadero gate final.

### 4.3 Smoke real de QuickAutoClean (documentado en `AGENTS.md` raíz)

Mismo bloqueo que T-25: los tests mockean el subproceso de SSEEdit; falta la
corrida real. Ítem ya conocido y documentado, sin cambio de estado.

---

## Decide — recomendación de próximo frente

**Corrección (2026-07-14):** la versión anterior de esta sección afirmaba que
"el diff del sandbox se apoya en el mismo journal" para justificar cablear el
backend de manifest/flight-report antes que activar el sandbox. Es **falso** —
verificado ahora: `grep -n "journal\|persist_" sky_claw/local/mo2/
profile_sandbox.py sky_claw/local/mo2/sandbox_run.py` no da resultados. El
`diff()`/`promote()` del sandbox es puramente file-based (comparación de
árboles antes/después del clon), sin ninguna dependencia del journal. Las dos
pistas (T-26/T-28 backend, T-27 activación) son **independientes** — pueden
abordarse en cualquier orden o en paralelo. Reordenado por ventana de
exposición (qué gap cierra más rápido con menos esfuerzo), no por un
acoplamiento técnico inexistente.

Por valor/riesgo, en orden:

1. **T-27 — invocar `run_ritual_in_sandbox` desde al menos un dispatcher real**
   (Synthesis, que ya tiene el seam de inyección `output_path` listo). Esfuerzo
   bajo — la infraestructura (`ProfileSandbox` + `run_ritual_in_sandbox`) ya
   existe, falta el cableado del dispatcher. Convierte la garantía de
   aislamiento de "teórica para todos" a "activa para uno", cerrando el gap de
   mayor severidad (escritura directa al overwrite real) para el runner más
   fácil de aislar.
2. **T-26/T-28 backend — cablear `persist_action_manifest`/`persist_flight_report`
   en xEdit/Synthesis/DynDOLOD/Pandora/Wrye Bash.** Sin dependencia de (1);
   candidato natural para empezar por xEdit (segundo runner en importancia
   tras LOOT, limpieza destructiva por naturaleza) — requiere antes aclarar la
   relación entre `preview.manifest` (dry-run, ya usado por xEdit/DynDOLOD) y
   el `ActionManifest` persistido que exige T-26, para no duplicar conceptos.
3. **T-16c·2/3 (preflight en Synthesis/DynDOLOD/Pandora/Wrye Bash)** — mismo
   patrón de riesgo que ya motivó T-15; extender el gate existente es
   mecánico y no tiene dependencias cruzadas con lo anterior.
4. **T-28 GUI view** — solo después de (2): mostrar un informe que hoy no
   existiría para la mayoría de los Rituales sería peor que no mostrarlo.
5. Todo lo demás (T-12/T-10/T-11 continuos, Oleada 5, residuales del OODA) es
   deuda de fondo o requiere un humano — no urgente para el próximo PR.
