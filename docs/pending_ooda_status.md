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
  T-29 sí cerrados. **T-26 y T-28 están sobre-declarados como "cerrados"
  en el historial de commits — la cobertura real es solo LOOT**, ver §1
  (corregido tras review de Codex sobre el primer draft de este mismo
  documento — la ironía es la prueba de campo del problema que motiva esta
  nota: verificar "un archivo" no es lo mismo que verificar "todo caller en
  el árbol"). **T-27: cerrado parcialmente el 2026-07-14** — Synthesis corre
  sandboxeado con promote/discard HITL real (ADR 0005); Pandora/DynDOLOD/
  Wrye Bash siguen sin palanca de redirección, ver §1.2.
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

### 1.1 T-26/T-28 — ActionManifest y FlightReport: LOOT + xEdit los emiten; faltan 4 runners

**Actualización 2026-07-15 (cierre PARCIAL — no declarar T-26/T-28 "cerrados"):**
`xedit_service.py` es ahora el **segundo productor de producción** tras LOOT.
Sus **dos** entry points mutantes persisten la caja negra: `execute_patch`
(`tool="xEdit"`) y `quick_auto_clean` (`tool="SSEEdit"`, al que se le agregó la
transacción de journal que antes no abría). Ambos emiten el `ActionManifest`
fail-closed ANTES de mutar (si no se puede persistir, no se muta) y el
`FlightReport` best-effort tras el commit — espejo exacto de la disciplina de
`loot_service.py`. Cubrir **ambos** entry points fue deliberado: cablear solo
`execute_patch` habría dejado `quick_auto_clean` como mutante sin caja negra,
repitiendo la sobre-declaración que este doc advierte.

```
$ grep -rln "persist_action_manifest\|persist_flight_report" sky_claw/ | grep -v test
sky_claw/antigravity/db/journal.py       # la definición
sky_claw/local/tools/loot_service.py     # productor (T-26/T-28 v1)
sky_claw/local/tools/xedit_service.py    # productor (2026-07-15, ambos entry points)
```

**Lo que sigue abierto:** `synthesis_service.py`, `dyndolod_service.py`,
`pandora_service.py` y `wrye_bash_runner.py` no emiten manifest/flight report
(dyndolod/xedit importan `preview.manifest`, un modelo **distinto** — el de
preview dry-run de la cadena, no el `ActionManifest` persistido por-Ritual).
El criterio de aceptación de T-26 ("todo Ritual mutante produce" el
manifiesto) y el de T-28 ("informe... por Ritual") se cumplen hoy para LOOT y
xEdit; faltan esos 4 runners antes de que tenga sentido la vista GUI de T-28
(mostrar un informe que, para la mayoría de los Rituales, todavía no existe).
`tool_version` queda en `None` para xEdit (no la expone hoy) — follow-up menor.

### 1.2 T-27 — Synthesis sandboxeado con promote/discard real (2026-07-14); Pandora/DynDOLOD/Wrye Bash siguen fuera

**Actualización 2026-07-14 (cierre PARCIAL — no declarar T-27 "cerrado"):**
`SandboxPromotionFlow` (`sky_claw/antigravity/orchestrator/sandbox_promotion.py`,
ADR 0005) es ahora el dueño de producción del ciclo clonar → correr → diff →
HITL → promote/discard, y `execute_synthesis_pipeline` corre **siempre** por
él (strategy + builders lazy en `tool_dispatcher.py`). El bucle de decisión
post-ejecución que faltaba se resolvió **síncrono**, bloqueando en
`HITLGuard.request_approval` (categoría nueva `sandbox_promotion`, nunca
auto-aprobada por «Modo local» ni por el fallback headless — ese fallback
auto-aprobaba categorías desconocidas y se cerró en el mismo PR). Denegado/
timeout/drift/ritual-fallido descartan fail-closed con el diff como evidencia
en `result["sandbox"]`; solo `APPROVED` promueve. Ver ADR 0005 para el TOT
(síncrono vs. asíncrono vs. auto-políticas) y el criterio de reversión.

**Lo que sigue abierto de T-27:** Pandora no es redirigible hoy (sin palanca
de output — el subproceso escribe vía el VFS de MO2 con `cwd`); DynDOLOD y
Wrye Bash, ídem. Su aislamiento requiere diseño de redirección aparte
(follow-up documentado en `sandbox_run.py`). Hasta entonces la garantía de
T-27 es real **solo para Synthesis**.

Contexto histórico (por qué esto era el gap #1): T-27b·1 construyó el seam de
inyección y `run_ritual_in_sandbox()`, pero nada en producción los invocaba —
`grep -rln run_ritual_in_sandbox sky_claw/ | grep -v test` devolvía solo la
definición. Mismo patrón "sensor sin cablear = verde mentiroso" que el
preflight (PR #250). El dato que reencuadró el esfuerzo: el bucle HITL no
había que construirlo — `HITLGuard` (`security/hitl.py`) ya era una primitiva
genérica bloqueante puenteada a GUI y Telegram; la nota anterior de este doc
("el único gate HITL existente aprueba ANTES de ejecutar") era incompleta.

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

1. ~~**T-27 — invocar `run_ritual_in_sandbox` desde al menos un dispatcher
   real**~~ **Hecho para Synthesis (2026-07-14, ADR 0005)** — ver §1.2: el
   flujo promote/discard se resolvió síncrono vía `HITLGuard` y
   `execute_synthesis_pipeline` corre siempre sandboxeado. El resto de T-27
   (redirección de output para Pandora/DynDOLOD/Wrye Bash) requiere diseño
   por-runner y queda como follow-up; no entra en este ranking hasta tener
   palanca de redirección.
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
