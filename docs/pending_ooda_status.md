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
- **Roadmap de producto "caja negra de vuelo" (ADR 0002, Oleada 7):**
  T-26/T-28(backend)/T-29/T-30 cerrados. **Dos gaps reales** sobreviven a la
  verificación de código, no solo de commit-message — ver §1.
- **Deuda estructural continua (T-10/T-11/T-12):** en curso, sin bloquear GA
  — ver §2.
- **GUI/accesibilidad (Oleada 5) y smoke E2E (Oleada 6):** nunca arrancadas
  — ver §4.

---

## 1 — Gaps reales en el roadmap "caja negra de vuelo" (mayor valor)

Verificados por grep directo contra el código, no por el commit-message del PR
que cerró la tarea (los mensajes de cierre a veces declaran "listo" sin cubrir
el 100% del criterio de aceptación original).

### 1.1 T-27b·2 — Pandora no está cableado al `ProfileSandbox`

`docs/adr/../TECHNICAL_REVIEW_TASKS.md` T-27 nombra **explícitamente** a
Pandora (junto con Synthesis) como runner que escribe en el overwrite
compartido de MO2 y que el sandbox debe aislar. T-27b·1 (#258) cableó
Synthesis. **Pandora sigue sin sandbox:**

```
$ grep -n "ProfileSandbox\|sandbox" sky_claw/local/tools/pandora_service.py
(sin resultados)
```

Mientras esto no se cierre, un Ritual de Pandora contra un perfil con
`ProfileSandbox` activo **no está realmente aislado** — la garantía de T-27
("ningún Ritual mutante escribe directamente... en el overwrite compartido
real") es falsa para Pandora. Es la brecha de mayor severidad de este
inventario porque contradice una garantía de seguridad ya declarada cerrada.

### 1.2 T-28 — Informe final de vuelo: falta la vista GUI

El commit de cierre (#249) es explícito: *"T-28 backend"*. El criterio de
aceptación original pedía además una vista en
`sky_claw/antigravity/gui/views/`:

```
$ find sky_claw/antigravity/gui -iname "*flight*" -o -iname "*informe*"
(sin resultados relevantes — solo preflight_panel.py, que es otra cosa)
```

El backend ensambla el informe (journal + manifiesto + validador) pero el
operador no tiene dónde leerlo en la GUI todavía — la "caja negra" existe
pero nadie la abre sin ir a la API/DB directamente.

### 1.3 Follow-up explícito de T-16c·1: preflight sin cablear en DynDOLOD/Pandora/Wrye Bash

El propio PR #288 (T-16c·1) lo declara en su descripción:

> *"Follow-up: cablear preflight en DynDOLOD/Pandora (path_resolver) y wrye_bash"*

Hoy el semáforo de preflight (T-15/T-16) cubre LOOT y xEdit QuickAutoClean.
DynDOLOD, Pandora y Wrye Bash siguen ejecutando **sin ese gate** — mismo
patrón de riesgo que motivó T-15 originalmente (mutantes corriendo sin
validación previa).

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

Por valor/riesgo, en orden:

1. **T-27b·2 (Pandora → ProfileSandbox)** — cierra una brecha de seguridad
   real en una garantía ya declarada cumplida. Mayor prioridad de este
   inventario.
2. **T-16c·2/3 (preflight en DynDOLOD/Pandora/Wrye Bash)** — mismo patrón de
   riesgo que ya motivó T-15; extender el gate existente es mecánico.
3. **T-28 GUI view** — desbloquea el valor de producto ya construido en
   backend (nadie lee el informe sin ir a la DB).
4. Todo lo demás (T-12/T-10/T-11 continuos, Oleada 5, residuales del OODA) es
   deuda de fondo o requiere un humano — no urgente para el próximo PR.
