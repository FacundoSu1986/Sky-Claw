# ADR 0005 — Promoción del sandbox: síncrona, bloqueando en HITL dentro del mismo dispatch

**Fecha:** 2026-07-14
**Estado:** Aceptada
**Contexto de origen:** T-27 (ADR 0002, Oleada 7) — frente #1 de
`docs/pending_ooda_status.md §Decide`. Evaluación OODA + Tree of Thoughts
contra `main @ 113155b`.

## Contexto

`run_ritual_in_sandbox()` (`sky_claw/local/mo2/sandbox_run.py`, T-27b·1)
clona el perfil MO2 + overwrite, corre el ritual contra la copia y devuelve un
clon **vivo** que el caller debe `promote()` o `discard()` "tras aprobación
HITL". Ese bucle de decisión post-ejecución no existía en ningún caller de
producción (verificado: `grep -rln run_ritual_in_sandbox sky_claw/ | grep -v
test` → solo la definición), así que la garantía central de T-27 ("ningún
Ritual mutante escribe en el overwrite compartido real") no era cierta para
ningún runner. Además `execute_synthesis_pipeline` — el único runner
redirigible hoy — corría **sin sandbox y sin gate HITL** (no está en
`DESTRUCTIVE_TOOL_PATTERNS`).

Hallazgo que reencuadró el problema: el backlog decía que "el único gate HITL
existente aprueba ANTES de ejecutar" (`ChainPreviewApprovalGate`). Es
incompleto — existe `HITLGuard` (`sky_claw/antigravity/security/hitl.py`), el
"single approval backbone" del proyecto: una primitiva **genérica y
bloqueante** (`request_approval(request_id, reason, detail, category)` →
bloquea hasta `respond()` o timeout de `HITL_TIMEOUT_SECONDS = 300` con
auto-deny fail-secure), ya puenteada a la GUI (modal Aprobar/Denegar que
renderiza `reason` + `detail` para cualquier categoría) y a Telegram (botones
inline). El bucle de decisión no había que construirlo desde cero: había que
orquestarlo.

## Alternativas evaluadas (Tree of Thoughts)

### (a) Promoción síncrona: bloquear en `HITLGuard.request_approval` dentro del mismo dispatch — **elegida**

- Toda la infraestructura existe (guard + modal GUI + Telegram + timeout
  fail-secure); el costo real es orquestación, no construcción.
- El drift-gate de `promote()` (`SandboxDriftError`: si el árbol real cambió
  desde el clonado, promover pisaría cambios vivos) hace frágil **cualquier**
  ventana larga de aprobación. La ventana síncrona de 300 s acota la
  exposición al drift; una promoción que espere horas casi seguro falla por
  drift de todos modos.
- Las otras 6 tools destructivas ya bloquean en HITL dentro del dispatch
  (`HitlGateMiddleware`) — mismo shape de UX, cero conceptos nuevos para el
  operador.

### (b) Promoción asíncrona: estado "pendiente de promoción" que la GUI resuelve después — descartada

- Exige persistencia del estado pendiente, ciclo de vida de clones entre
  reinicios del daemon, y una superficie GUI nueva (lista de promociones
  pendientes + vista de diff) — costo M/L.
- Compra exactamente lo que el drift-gate castiga: ventanas largas. El valor
  real de "aprobar más tarde" es negativo mientras el gate re-clone-y-re-corré
  sea la respuesta correcta al drift.

### (c) Auto-políticas (auto-promote si el diff "parece seguro", auto-promote con «Modo local») — descartada

- Auto-promote sin revisión vacía al sandbox de sentido: la transparencia del
  diff post-run ES el producto. Por eso la categoría HITL nueva
  (`sandbox_promotion`) **nunca** se auto-aprueba por «Modo local» (mismo
  trato que `download`), y el fallback headless de `app_context._hitl_notify`
  la deniega fail-closed (sin canal de operador, ese closure auto-aprobaba
  cualquier categoría desconocida — se cerró ese agujero en este mismo cambio).
- Auto-discard silencioso rompería Synthesis sin que el usuario sepa por qué;
  todas las ramas del flow dejan la decisión y el diff en el result.

## Decisión

`SandboxPromotionFlow` (`sky_claw/antigravity/orchestrator/sandbox_promotion.py`,
ritual-agnóstico, mypy-estricto) es el dueño de producción del ciclo
clonar → correr → diff → HITL → promote/discard:

- **Sin `HITLGuard`** → se deniega **sin correr el ritual** (fail-closed,
  precedente `HitlGateMiddleware`); `reason="SandboxPromotionUnavailable"`.
- **Ritual con `success` falsy** → discard sin prompt (escrituras parciales no
  se promueven jamás), con el diff como evidencia en `result["sandbox"]`.
- **Diff vacío** → discard sin prompt (aprobar cero cambios es ruido).
- **Denegado/timeout** → discard; `success=False`,
  `reason="SandboxPromotionDenied"`, mensaje explícito con el conteo.
- **Aprobado** → `promote()`; `SandboxDriftError` → discard +
  `reason="SandboxDriftDetected"` ("reclonar y re-ejecutar");
  `SandboxRollbackError` → el clon **no** se descarta (su árbol contiene el
  backup de restauración manual; la ruta viaja en el message).

`execute_synthesis_pipeline` corre **siempre** por este flow (strategy con
providers lazy; el servicio se construye fresco por run con
`output_path=clone.overwrite_copy`). Sin gate HITL pre-ejecución: sería
double-gating (precedente PR #173) y la aprobación post-run sobre el diff real
es estrictamente más fuerte que aprobar a ciegas antes.

Divergencia deliberada con el contrato de `sandbox_run` (que deja el clon vivo
ante un fallo del tool, para forense manual): el flow captura el diff en el
result y descarta — un clon huérfano por run fallido sería un leak de disco
sin GUI que lo inspeccione. La excepción es `SandboxRollbackError` (arriba).

## Consecuencias

- La garantía de T-27 es real para Synthesis: el overwrite real solo cambia
  tras una aprobación explícita sobre el diff. Pandora/DynDOLOD/Wrye Bash
  siguen fuera (sin palanca de redirección de output — follow-up documentado
  en `sandbox_run.py`); T-27 queda **parcialmente** cerrado, no cerrado.
- Comportamiento nuevo visible: ejecutar Synthesis ahora pide una aprobación
  post-run (modal GUI / Telegram). Sin operador, el timeout de 300 s descarta
  — explícito en el result, nunca silencioso.
- Todo runner futuro que gane palanca de output debe cablearse por
  `SandboxPromotionFlow`, no llamar `promote()`/`discard()` a mano.
- Tests ancla: `tests/test_sandbox_promotion.py` (contrato completo del flow,
  con `ProfileSandbox` real), `test_supervisor_dispatch_tool.py` (routing +
  fail-closed sin guard), `test_ritual_dispatch.py` (el bridge GUI nunca
  auto-aprueba la categoría), `test_hitl_wiring.py` (headless deniega).

## Criterio de reversión

Reabrir la evaluación de la rama (b) — asíncrona — solo si aparece un caso de
uso real de aprobación diferida (p. ej. pipeline nocturno desatendido donde el
operador revisa a la mañana) **y** para entonces existe una respuesta al drift
mejor que "reclonar y re-ejecutar" (p. ej. re-basar el diff). Mientras el
drift-gate mande, la ventana corta es la correcta.
