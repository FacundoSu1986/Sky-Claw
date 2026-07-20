# Auditoría de resiliencia del orquestador asíncrono — 2026-07-18

Auditoría adversarial de `sky_claw/antigravity/orchestrator/` enfocada en:
grafo de estados (LangGraph), concurrencia/asyncio, cancelaciones, rollback y
HITL. Cada hallazgo fue verificado contra el código vivo del repo (commit
`a7f66b4`), trazando callers reales y contrastando con los tests existentes.
Se marca **CONFIRMADO** (mecanismo aislado y trazado end-to-end) o
**PLAUSIBLE** (mecanismo sólido, requiere reproducción para cerrar al 100%).

Sin nitpicking: solo integridad operativa y lógica.

---

## 🎯 1. Análisis del Flujo de Trabajo (Workflow & State Graph)

### F1 — CRÍTICO · CONFIRMADO: el StateGraph es un motor muerto y, si corriera, perdería sus escrituras de estado

El hallazgo más importante de la auditoría no es un bug dentro del grafo sino
uno alrededor: **el grafo nunca se ejecuta en producción**, y si se ejecutara,
fallaría estructuralmente por cuatro mecanismos independientes.

**1a. Nadie invoca el grafo.** Un barrido de todo `sky_claw/` muestra cero
callers de `SupervisorStateGraph.execute()`, `submit_event()` o
`LangGraphEventStreamer.stream_execute()` fuera de sus propios módulos. El
streamer se construye en `supervisor.py:154` y muere ahí. El camino real de
ejecución de tools es `gui/controllers/ritual_runner.py:270 →
supervisor.dispatch_tool()` — directo, sin pasar por el grafo.

*Impacto:* `AgenticLoopGuardrail` (cortacircuitos cognitivo), el timeout de
`HITL_WAIT`, el routing `ERROR → ROLLING_BACK → ERROR_FATAL` y el
`StateGraphValidator` son código muerto. La protección anti-bucle del agente
**no protege el camino que realmente ejecuta tools**.

**1b. Escrituras por mutación in-place que LangGraph descarta.**
`StateGraphIntegration._on_dispatching` (`state_graph.py:1292-1347`) y
`_on_hitl_wait` (`:1349-1360`) escriben el estado mutando el dict de entrada:

```python
state["tool_result"] = result          # _on_dispatching
state["last_error"] = ...
state["loop_detected"] = True
state["hitl_response"] = response      # _on_hitl_wait
```

En LangGraph, **solo lo que el nodo retorna** se escribe a los channels. El
wrapper `_make_callback_aware_node` (`:867-873`) retorna `node_fn(state)` —
p. ej. `dispatching_node` retorna únicamente `current_state`,
`previous_state` y los resets de rollback. Ninguna de las claves mutadas por
el callback viaja en ese retorno, así que se descartan al cerrar el superstep.
Las aristas condicionales (`route_from_dispatching`, `route_from_hitl_wait`)
leen los channels, no el dict mutado. Lo mismo aplica a
`route_from_hitl_wait`, que muta `state["last_error"]` desde una arista
(`:723`, `:736`) — las aristas condicionales no tienen permitido escribir
estado en LangGraph.

*Impacto:* si el grafo corriera, un tool fallido rutearía a `COMPLETED`
(porque `tool_result`/`last_error` se perdieron), el trip del guardrail se
ignoraría en silencio, y la respuesta HITL jamás llegaría al router.

*Mitigación quirúrgica:* el wrapper debe fusionar las escrituras del callback
en el update retornado:

```python
async def wrapped_node(state: StateGraphState) -> dict[str, Any]:
    callback = graph_ref._callbacks.get(f"{state_value}_callback")
    updates: dict[str, Any] = {}
    if callback is not None:
        mutable = dict(state)
        await callback(mutable)
        # Solo las claves que el callback cambió respecto del estado leído
        updates = {k: v for k, v in mutable.items() if state.get(k) != v}
    return {**updates, **node_fn(state)}
```

**1c. Self-loops estructurales → `GraphRecursionError`.** `route_from_idle`
retorna `"idle"` cuando no hay evento (`:603`) y `route_from_hitl_wait`
retorna `"hitl_wait"` como "espera legítima / polling" (`:740`). LangGraph no
espera: itera supersteps sin pausa hasta el `recursion_limit` (default 25).
Todo run que desemboque en IDLE (es decir, **todo run exitoso**, vía la arista
fija `COMPLETED → IDLE` de `:938`) muere con `GraphRecursionError`, que
`execute()` (`:1075-1079`) captura como `Exception` genérica y convierte en
`current_state=ERROR`. Además, cada re-entrada a `hitl_wait` re-invoca
`_on_hitl_wait` → `interface.request_hitl(...)`: prompts duplicados al
operador en cada vuelta del "polling".

*Mitigación:* IDLE debe ser terminal del run (rutear a `END` sin evento) y la
espera HITL debe implementarse con `interrupt()` / `Command(resume=...)` de
LangGraph (o un `await` real del guard dentro del nodo), no con un self-loop
de supersteps.

**1d. `submit_event` con `thread_id` resetea el estado completo.**
`:1147-1154` construye `get_initial_state()` entero y lo pasa como input: cada
clave pisa su channel, incluyendo `error_count=0`. El límite de 3 reintentos
de `route_from_error` (`:783`) es inalcanzable entre eventos, y la
"continuación" por checkpoint es ilusoria. Adicionalmente, dentro de un mismo
run el camino `ERROR → IDLE` no limpia `pending_event` (solo `completed_node`
lo hace, `:468`), por lo que el grafo re-analizaría y **re-despacharía la tool
fallida** automáticamente hasta 3 veces — retry silencioso sin backoff para
tools no destructivas.

**Por qué los tests no lo detectan:**
`tests/test_state_graph_loop_integration.py` invoca `_on_dispatching(dict)`
directamente sobre un dict plano — nunca `compiled_graph.ainvoke`. Ningún test
ejecuta el grafo compilado end-to-end.

**Recomendación de fondo (decisión explícita, no parche):**
- Opción A — retirar el grafo y mover `AgenticLoopGuardrail` a un middleware
  del `OrchestrationToolDispatcher` (junto al `HitlGateMiddleware`), que es el
  único camino real. Es la opción de menor riesgo: hoy el dispatcher ya provee
  gate HITL fail-closed y error-wrapping.
- Opción B — cablear el grafo de verdad: fixes 1b/1c/1d + un caller de
  producción (`ritual_runner` → `stream_execute`) + un test que ejecute
  `ainvoke` con un tool fallido y afirme `current_state == "error"`.

### Notas de consistencia del grafo (menores, mismas raíces)

- `patching_node` (`:531-535`) "regresa a IDLE" retornando
  `current_state=idle`, pero el router del nodo sigue siendo
  `route_from_patching`, que valida transiciones **desde PATCHING** y rutea a
  `COMPLETED` por default — la rama IDLE declarada es inalcanzable.
- `hitl_wait_node` fija `previous_state=DISPATCHING` (`:452`) aunque el
  validador permite entrar a HITL_WAIT desde ANALYZING: historial de
  transiciones mentiroso para forense.
- El reducer `capped_transition_history` está registrado pero ningún nodo
  escribe `transition_history`: el historial queda vacío siempre.

---

## ⚡ 2. Vulnerabilidades de Concurrencia y Asyncio (Bugs Reales)

### F2 — ALTO · CONFIRMADO: cancelación durante `promote()` → thread zombie mutando el perfil real + clon filtrado

- **Mecanismo de Fallo:** `ProfileSandbox.promote`
  (`local/mo2/profile_sandbox.py:265-267`) ejecuta `_apply_changes` — la
  mutación del árbol MO2 **real** — dentro de `asyncio.to_thread`. Cancelar la
  task asyncio no interrumpe el hilo: el `await` lanza `CancelledError`
  mientras el hilo sigue escribiendo el perfil real en background, sin
  observador. En el caller, `SandboxPromotionFlow._promote`
  (`orchestrator/sandbox_promotion.py:262-292`) maneja `SandboxDriftError`,
  `SandboxRollbackError` y `except Exception` — pero `CancelledError` es
  `BaseException`: no lo captura ninguna rama, a diferencia de
  `_request_decision`, que sí lo maneja (`:243-246`).
- **Ubicación:** `sandbox_promotion.py -> SandboxPromotionFlow._promote` +
  `profile_sandbox.py -> promote`.
- **Impacto:** ante un shutdown/timeout durante la promoción: (1) el clon (y
  su `rollback-*` interno) queda huérfano en disco; (2) el perfil real puede
  quedar promovido a medias o promovido completo sin que el resultado se
  reporte jamás (el hilo termina después de que el flujo ya propagó la
  cancelación) — el peor estado posible para un flujo cuyo contrato es
  "todo-o-nada visible al operador".
- **Código/Mitigación Sugerida:**

```python
async def _promote(self, ritual_name, clone, diff, result):
    try:
        # shield: si el caller cancela, el promote termina igual en el thread
        # y su resultado/rollback interno queda observado, no zombie.
        promocion = await asyncio.shield(self._sandbox.promote(clone))
    except asyncio.CancelledError:
        # El shield sigue corriendo; limpieza best-effort y propagar.
        with contextlib.suppress(Exception):
            await asyncio.shield(self._sandbox.discard(clone))
        raise
    except SandboxDriftError as exc:
        ...
```

  (Alternativa mínima: agregar `except asyncio.CancelledError: discard + raise`
  espejo de `_request_decision`, aceptando que el thread zombie complete o
  ruede back solo — pero documentándolo.)

### F3 — ALTO · CONFIRMADO: rollback post-cancelación sin `shield` en `SyncEngine.execute_file_operation`

- **Mecanismo de Fallo:** el bloque `except asyncio.CancelledError`
  (`sync_engine.py:362-380`) hace `await rm.fail_operation(entry_id)` y
  `await rm.undo_operation(entry_id)` sin blindaje. Una **segunda**
  cancelación — escenario real: `SyncEngine.shutdown()` cancela
  `_download_tasks` y el `gather` del caller o el teardown del loop re-cancela;
  o un `asyncio.wait_for` externo — interrumpe el undo a mitad. El
  `except Exception as cleanup_exc` (`:378`) NO captura esa segunda
  `CancelledError`.
- **Ubicación:** `sync_engine.py -> execute_file_operation` (rama
  `except asyncio.CancelledError`).
- **Impacto:** journal con asiento FAILED **sin** restauración ejecutada: el
  archivo real queda a medias mientras la contabilidad dice "operación fallida
  y revertida" (el log ya emitió el warning de rollback iniciado). Corrupción
  silenciosa del contrato Unit-of-Work.
- **Código/Mitigación Sugerida:**

```python
except asyncio.CancelledError:
    logger.warning("Operación cancelada; ejecutando rollback automático")

    async def _cleanup() -> None:
        with contextlib.suppress(Exception):
            await rm.fail_operation(entry_id, error="operation cancelled")
        try:
            rb = await rm.undo_operation(entry_id)
            logger.warning("Rollback tras cancelación: success=%s", rb.success)
        except Exception as exc:
            logger.critical("Rollback tras cancelación falló: %s", exc)

    # shield: una segunda cancelación no puede partir el undo a la mitad.
    await asyncio.shield(_cleanup())
    raise
```

  Complemento: el `finally: await self._passive_pruning()` (`:405-409`)
  agrega awaits de DB/FS en plena ruta de unwind de cancelación — demora la
  cancelación y puede ser interrumpido a su vez. Saltearlo cuando
  `asyncio.current_task().cancelling()`.

### F4 — MEDIO · CONFIRMADO: `IdempotencyMiddleware` deja la key tomada 1 hora ante cancelación — y toda la maquinaria FASE 1.5.4 está sin cablear

- **Mecanismo de Fallo:** en `tool_strategies/middleware.py:339-347`, el
  `except Exception` que transiciona a FAILED no captura `CancelledError`: el
  `TaskRecord` queda RUNNING y la idempotency key activa hasta el TTL de
  3600s. Tras cancelar un tool, el mismo tool+payload responde
  `DuplicateExecution` durante una hora.
- **Agravante (verificado por grep):** ni `IdempotencyMiddleware`, ni
  `ProgressMiddleware`, ni `ToolStateMachine`, ni `ToolEventStreamer` están
  registrados en `build_orchestration_dispatcher` (`tool_dispatcher.py`). El
  único single-flight real del sistema es el flag de la GUI
  (`STORE_KEY_RITUAL_IN_FLIGHT` en `ritual_runner.py`), que no cubre despachos
  concurrentes desde Telegram/LLM/API. La protección anti-duplicados que
  FASE 1.5.4 diseñó no existe en el camino real.
- **Ubicación:** `middleware.py -> IdempotencyMiddleware.__call__` +
  `tool_dispatcher.py -> build_orchestration_dispatcher`.
- **Impacto:** (1) lockout de una hora tras cancelación; (2) doble ejecución
  concurrente de tools destructivas posible desde superficies no-GUI (mitigada
  parcialmente aguas abajo por `SnapshotTransactionLock` en los services, pero
  el rechazo temprano prometido no ocurre).
- **Código/Mitigación Sugerida:**

```python
self._sm.transition(task.task_id, "RUNNING")
try:
    result = await next_call()
except BaseException as exc:          # CancelledError incluido
    self._sm.transition(task.task_id, "FAILED", error_message=repr(exc))
    raise
self._sm.transition(task.task_id, "COMPLETED", result=result)
```

  …y registrar el middleware (compartiendo un `ToolStateMachine`) en
  `build_orchestration_dispatcher` para las tools destructivas — o borrar la
  maquinaria si la decisión es que el lock distribuido de los services basta.

### F5 — MEDIO · CONFIRMADO: el drift-gate del sandbox no es atómico (TOCTOU)

- **Mecanismo de Fallo:** `promote()` ejecuta `_check_drift`, `_compute_diff`
  y `_apply_changes` en **tres** `to_thread` separados
  (`profile_sandbox.py:265-267`), con vueltas al event loop entre medio. Una
  escritura de MO2/usuario en la ventana entre el check y el apply se pisa en
  silencio — exactamente el escenario que el gate promete cortar con
  `SandboxDriftError`.
- **Ubicación:** `profile_sandbox.py -> promote`.
- **Impacto:** pérdida silenciosa de cambios vivos del usuario en el perfil
  real, con el diff aprobado como coartada.
- **Mitigación:** fusionar las tres fases en una única función sync ejecutada
  en un solo `to_thread` (`def _promote_sync(clone): check; diff; apply`), de
  modo que no haya scheduling del loop entre gate y mutación.

### F6 — MEDIO · CONFIRMADO: race en el borde del timeout de `HITLGuard`

- **Mecanismo de Fallo:** en `security/hitl.py:133-145`, entre el
  `TimeoutError` de `wait_for` y la asignación `req.decision =
  Decision.DENIED` no se sostiene el lock. `respond()` puede intercalarse:
  encuentra la request todavía en `_pending`, setea `APPROVED`, dispara el
  event y retorna `True` ("tu aprobación fue registrada") — y acto seguido el
  branch de timeout pisa la decisión con `DENIED`.
- **Ubicación:** `hitl.py -> HITLGuard.request_approval / respond`.
- **Impacto:** el operador recibe un ack de aprobación para una operación que
  en realidad se auto-denegó. La dirección es fail-secure (se deniega), pero
  el ack falso rompe la confianza del canal HITL y complica el forense.
- **Mitigación:** resolver el timeout bajo el lock, marcando la request como
  cerrada antes de decidir:

```python
except TimeoutError:
    async with self._lock:
        closed = self._pending.pop(request_id, None)
    if closed is not None and not closed._event.is_set():
        closed.decision = Decision.DENIED   # nadie respondió: denegar
```

  (y en `respond`, el `pop`/lookup fallará → retorna `False`, ack honesto).

### F7 — MEDIO · CONFIRMADO: hasta 15 prompts HITL concurrentes en `check_for_updates`

- **Mecanismo de Fallo:** `Semaphore(15)` (`sync_engine.py:534`) permite 15
  `_check_and_update_mod` simultáneos; cada uno bloquea en
  `hitl.request_approval` (`:679-694`) hasta 300s (`HITL_TIMEOUT_SECONDS`).
  La GUI parquea **una sola** request pendiente (`STORE_KEY_PENDING_HITL`
  único; `run_ritual` incluso rechaza un segundo ritual por esa razón —
  Codex #211).
- **Ubicación:** `sync_engine.py -> check_for_updates / _check_and_update_mod`.
- **Impacto:** en un ciclo con varios updates, 14 prompts expiran sin que el
  operador los haya visto → updates "denegados por humano" que ningún humano
  vio. Degradación funcional silenciosa del ciclo de updates.
- **Mitigación:** semáforo de concurrencia 1 exclusivo para la fase HITL (la
  fase de fetch puede seguir a 15), o una única aprobación batch por ciclo con
  la lista de mods en el `detail`.

### F8 — BAJO · CONFIRMADO: `_produce_then_poison` puede reemplazar la `CancelledError` en vuelo por `TimeoutError`

- **Mecanismo de Fallo:** si el `TaskGroup` de `run()` cancela al producer
  (crash de un worker) con la cola llena, el `finally` (`sync_engine.py:
  819-832`) intenta hasta 4 `wait_for(queue.put(POISON), 5s)`; si expiran,
  el `raise` desde el `finally` **pisa la `CancelledError`** que estaba
  propagándose — el TaskGroup ve un `TimeoutError` en lugar del acuse de
  cancelación, y el shutdown se demora hasta `5s × worker_count`.
- **Ubicación:** `sync_engine.py -> run/_produce_then_poison`.
- **Impacto:** ruido en el shutdown cooperativo (excepción incorrecta
  reportada, demora acotada). No corrompe datos.
- **Mitigación:** en el `finally`, si `asyncio.current_task().cancelling()`,
  drenar con `queue.put_nowait` best-effort (los workers cancelados ya no
  necesitan el pill) en lugar de `wait_for` bloqueante.

---

## 🛡️ 3. Evaluación de Robustez en Fallos (Rollback & HITL Guard)

**Lo que está bien construido** (y conviene preservar en cualquier refactor):

- `execute_file_operation` (camino no-cancelado): setup transaccional con
  limpieza de snapshot huérfano y `coroutine.close()` del operation no
  awaiteado (T2-01 / P1 #140); undo por `entry_id` y no "última operación del
  agente" (H-1) — correcto bajo concurrencia.
- `ProfileSandbox._apply_changes`: backup fase-0 completo antes de mutar,
  escritura atómica tmp+`os.replace`, rollback inverso, y
  `SandboxRollbackError` que **preserva** el backup para restauración manual.
  Es un promote transaccional serio — por eso duele más F2/F5 alrededor.
- `SandboxPromotionFlow`: fail-closed en todas las ramas síncronas (sin guard
  → deniega sin ejecutar; ritual fallido / diff vacío / denegado → descarta;
  drift → descarta). El único hueco es la cancelación (F2).
- Supervisión de daemons (`_run_daemons_and_interface`): fail-fast colectivo
  real con `FIRST_COMPLETED` + cancel + gather; `_run_interface_isolated`
  separa errores de red recuperables de bugs con `except*` bien usado.
- `HitlGateMiddleware`: fail-closed sin guard, `request_id` único por
  invocación, redacción de claves sensibles, gate único (sin double-gating).
- `ErrorWrappingMiddleware` captura `Exception` y no `BaseException` — deja
  propagar la cancelación, como corresponde.

**Los huecos sistémicos**, en orden de gravedad:

1. **El plano de control es decorativo (F1).** El sistema de resiliencia
   "de papel" (grafo + guardrail + rollback routing) y el sistema real
   (dispatcher + services + locks) divergieron: todo lo que la FASE 1.5
   prometía a nivel workflow vive solo en el primero. Cualquier confianza en
   "el guardrail nos frena los bucles del LLM" es hoy infundada.
2. **La cancelación es el modo de fallo menos ensayado (F2, F3, F4).** El
   patrón repetido es: manejo prolijo de `Exception`, y `CancelledError` o no
   contemplado o con cleanup interrumpible. Regla sugerida para el repo:
   *todo cleanup transaccional post-cancelación va en una corrutina única
   envuelta en `asyncio.shield`*, y *ninguna mutación del árbol real corre en
   `to_thread` sin que el caller observe su resultado ante cancelación*.
3. **HITL degrada mal bajo concurrencia (F6, F7):** un solo slot visible en
   GUI + N solicitantes concurrentes + race del timeout = denegaciones que
   ningún humano decidió, reportadas como decididas.

**Sobre el god object (`SupervisorAgent`):** el strangler-fig ya extrajo el
*comportamiento* (services, daemons, dispatcher), pero la *composición* sigue
concentrada: `__init__` construye ~15 colaboradores. Recomendación sin
big-bang: (1) un composition root `SupervisorAssembler` (mismo patrón que
`rollback_factory.create_rollback_components`) que arme services+daemons y
entregue un dataclass de colaboradores; (2) separar el lifecycle
(`start`/`_run_daemons_and_interface`/shutdown) en un `DaemonRuntime` distinto
de la fachada de tools (`dispatch_tool` + servicios); (3) resolver F1 —
retirar o cablear el grafo — y en ambos casos mover `AgenticLoopGuardrail` a
middleware del dispatcher, que es el único camino de ejecución real.

---

## Estado de resolución (2026-07-19)

- **F1 — RESUELTO** (#328 F1a + F1b, ADR 0006): guardrail movido a
  `LoopGuardrailMiddleware` del dispatcher; StateGraph + deps langgraph
  retirados.
- **F2 — RESUELTO** (#320, follow-up #322): `promote()` shieldeado con
  desenlace terminal observado; TX diferida de Synthesis resuelta ante cancel.
- **F3 — RESUELTO** (#321): rollback post-cancelación shieldeado + drain en
  shutdown.
- **F5 — RESUELTO** (#332): drift-gate atómico — check y apply fusionados en
  un solo `to_thread` de `_promote_sync`.
- **F6 — RESUELTO** (#331): resolución atómica de la decisión HITL — cierra
  la race timeout/respond.
- **F4 — RESUELTO** (rama `claude/sky-claw-audit-review-rbuwfy`):
  `IdempotencyMiddleware` ahora captura `CancelledError` explícitamente
  (no hereda de `Exception`, así que el `except` genérico la dejaba pasar
  sin liberar la key) y `build_orchestration_dispatcher` la registra como
  middleware GLOBAL — mismo patrón de F1a — así que corre en el único camino
  real de dispatch en vez de quedar sin cablear.
- **F7, F8, F9 — pendientes** (no bloqueantes).

**Nota de higiene** (2026-07-20): esta sección estaba desactualizada respecto
del código — F5/F6 llevaban ya mergeados varios días (#331, #332) sin que se
reflejara acá, exactamente el patrón de doc-drift que `AGENTS.md` advierte
tras #290. Verificar siempre contra el código, no contra esta tabla.

## Anexo: matriz de hallazgos

| ID | Severidad | Estado | Ubicación | Resumen |
|----|-----------|--------|-----------|---------|
| F1 | Crítica | RESUELTO (#328) | `state_graph.py`, `supervisor.py:154`, `ritual_runner.py:270` | Grafo nunca ejecutado; mutaciones in-place descartadas; self-loops → `GraphRecursionError`; `submit_event` resetea estado |
| F2 | Alta | RESUELTO (#320/#322) | `sandbox_promotion.py:262-292`, `profile_sandbox.py:265-267` | Cancelación en promote: thread zombie muta el perfil real + clon filtrado |
| F3 | Alta | RESUELTO (#321) | `sync_engine.py:362-380` | Rollback post-cancelación interrumpible (sin `shield`) |
| F4 | Media | RESUELTO (rama audit-review) | `middleware.py:339-347`, `tool_dispatcher.py` | Key de idempotencia bloqueada 1h ante cancelación; middleware sin cablear |
| F5 | Media | RESUELTO (#332) | `profile_sandbox.py:265-267` | Drift-gate TOCTOU (check y apply en `to_thread` separados) |
| F6 | Media | RESUELTO (#331) | `hitl.py:133-159` | Race timeout/respond: ack falso de aprobación |
| F7 | Media | Confirmado | `sync_engine.py:534,679` | 15 prompts HITL concurrentes vs 1 slot en GUI |
| F8 | Baja | Confirmado | `sync_engine.py:819-832` | `TimeoutError` desde `finally` pisa la `CancelledError` |
| F9 | Estructural | Confirmado | `supervisor.py:117-291` | God object de composición; plan de desacople en §3 |
