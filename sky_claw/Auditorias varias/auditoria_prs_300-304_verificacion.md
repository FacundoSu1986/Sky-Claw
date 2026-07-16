# Verificación de consistencia — auditoría externa de los PRs #300–#304

**Fecha:** 2026-07-16
**Método:** OODA + verificación cruzada contra el código en HEAD (`53ec560`, que contiene
exactamente los cinco merges auditados: #300, #301, #302, #303, #304).
**Objeto:** una auditoría externa (estilo "Copilot Review Bot") que reportó 5 hallazgos
críticos sobre esos PRs. Este documento evalúa hallazgo por hallazgo si sus afirmaciones
son consistentes con el código real.

## Veredicto global

La auditoría es **sólida en las citas** — los archivos, líneas y snippets que cita son
reales, y el mapa de PRs (sección 1) coincide 5/5 con el historial de git. Pero es
**inconsistente en varios mecanismos causales y en la severidad asignada**:

| Hallazgo | Veredicto | Resumen |
|---|---|---|
| H1 — TOCTOU en `launch_game` (#302) | Parcialmente válido, severidad inflada | Riesgo residual real pero ya documentado en el código; mecánica de falla parcialmente incorrecta |
| H2 — `output_path=None` rompe consumidores (#304) | **No se sostiene** | Afirmación central fabricada; no existe ningún consumidor productivo del campo |
| H3 — Rollback parcial en Synthesis (#304) | **Redundante** | Pide crear código que ya existe; solo el gap de test es válido |
| H4 — `MO2Controller` duplicado (#301) | Mixto | Preocupación arquitectónica real, pero dos afirmaciones de soporte son falsas |
| H5 — (truncado) | No evaluable | El fragmento visible describe un escenario prácticamente irrelevante |

**Residuo accionable real: 2-3 follow-ups menores** (ver sección final). Ninguno de los
5 hallazgos amerita el nivel de riesgo "Alto" asignado.

---

## H1 — "Ventana de carrera TOCTOU en `launch_game`" (#302): parcialmente válido

**Citas verificadas.** El código citado existe tal cual: captura de `create_time` +
registro pre-verificación en `sky_claw/local/mo2/vfs.py:365-368`, y el patrón
snapshot → kill → pop en `vfs.py:415-424`.

**Mecánica incorrecta.** La auditoría afirma que entre `proc.pid` y
`psutil.Process(proc.pid)` el proceso "puede no haber sido materializado aún en la tabla
de procesos del SO (especialmente en Windows con `CREATE_SUSPENDED`)". Esto es falso:

- `asyncio.create_subprocess_exec` retorna **después** de que el SO creó el proceso
  (fork en POSIX, `CreateProcess` en Windows). Un `psutil.NoSuchProcess` en ese punto
  significa que el proceso **ya murió** (crash inmediato), no que "todavía no existe".
- asyncio no usa `CREATE_SUSPENDED`; esa mención es inventada.

**Riesgo "redescubierto".** El caso `create_time=None` (proceso muerto antes de la
captura → `_kill_process_tree` omite el chequeo de identidad) no es un hallazgo nuevo:
está documentado deliberadamente como trade-off en el docstring de `_kill_process_tree`
(`vfs.py:439-440`: *"``None`` … omite el chequeo: el proceso casi siempre ya murió"*).
Lo mismo con la atomicidad del dict bajo GIL, razonada explícitamente en el comentario
de `close_game` (`vfs.py:410-414`, review Codex #302).

**Doble kill.** La propia auditoría admite que bajo el GIL de CPython no es un problema
(el segundo kill sería no-op; además todas las mutaciones del dict ocurren en el hilo
del event loop — `_kill_process_tree` corre en `to_thread` pero no toca el dict). El
escenario free-threaded (3.13t) es especulativo: el proyecto corre CPython 3.11/3.12.

**Sobre los fixes propuestos.** El retry-loop de 500ms agrega latencia a todos los
lanzamientos para cubrir una ventana casi inexistente. El `asyncio.Lock` alrededor de
la región snapshot→kill→pop de `close_game` sí es higiene defendible (ver residuo).

---

## H2 — "`output_path=None` rompe `_result_to_dict` y consumidores" (#304): no se sostiene

**Hecho base correcto.** `PatchOrchestrator.resolve()` puede devolver
`output_path=None` con `success=True` para planes advisory
(`sky_claw/local/xedit/patch_orchestrator.py:722-731`), y `_result_to_dict` lo maneja
con el guard `is not None` (`xedit_service.py:958-959`).

**Afirmación central fabricada.** La auditoría dice: *"el EventBus publica un payload
que se serializa a JSON vía `result['output_path']`"*. Los payloads reales del servicio
— `XEditPatchStartedPayload` (`xedit_service.py:238-248`) y `XEditPatchCompletedPayload`
(`xedit_service.py:443-458`) — **no incluyen `output_path` en absoluto** (llevan
`target_plugin`, `total_conflicts`, `success`, `records_patched`, `conflicts_resolved`,
`duration_seconds`, `rolled_back`).

**Sin consumidores productivos.** Un grep exhaustivo del repo muestra que **ningún
código de producción** lee `result["output_path"]` del dict retornado por
`execute_patch`. Los únicos lectores son tests
(`tests/test_patch_placebo_fail_closed.py:286,320`). El strategy del dispatcher
(`tool_strategies/resolve_conflict_patch.py:47-52`) retorna el dict tal cual, y el
consumidor de preview usa `result["change_set"]` (dry-run), no `output_path`.

**El test "faltante" ya existe en esencia.** `test_patch_placebo_fail_closed.py:320`
asierta exactamente `out["output_path"] is None  # advisory: no se generó .esp` junto
con `success=True`. Y el discriminador estructurado que la auditoría propone
(`advisory: True`) está explícitamente diferido a Fase 2 por diseño, documentado en el
docstring de `_run_ai_advisor` (`xedit_service.py:482-484`: *"Fase 2 añade un campo
dedicado"*).

Queda un punto conceptual válido pero menor: `success=True` + `output_path=None` es un
cambio de invariante implícita que conviene tener presente cuando la GUI de Fase 2
consuma el resultado. No es una regresión hoy.

---

## H3 — "Synthesis marca TX rolled-back sin garantías de rollback total" (#304): redundante

Los tres reclamos centrales piden código **que ya existe**:

1. *"La implementación del lock tendría que garantizar que `rollback_completed` es True
   SOLO si todos los archivos se restauraron"* — ya lo garantiza:
   `SnapshotTransactionLock.rollback_completed` es una property definida como
   `rollback_attempted and bool(self.snapshots) and not self.rollback_failures`
   (`sky_claw/antigravity/db/locks.py:639-641`). Un restore parcial (3 de 4) deja
   `rollback_failures` no vacío → `False`.
2. *"Exigir al SnapshotTransactionLock que exponga `rollback_failures`"* — ya lo expone:
   `locks.py:631` (inicialización) y `locks.py:876` (asignación tras el rollback).
3. *"No hay mecanismo de alerta que lleve al operador a inspeccionar el archivo"* —
   falso: `_rollback_snapshots` loggea CRITICAL **por archivo fallido con su path**
   (`locks.py:869-874`: *"ROLLBACK FAILED for %s … manual recovery required"*), además
   del CRITICAL del servicio que deja la TX en PENDING
   (`synthesis_service.py:311-315`). Incluso el comentario del propio código citado
   menciona `rollback_failures` (`synthesis_service.py:347`).

**Único punto válido:** no hay un test que ejercite el caso de restore **parcial** con
el lock real. `tests/test_synthesis_rollback_honesto.py` cubre los dos extremos (fake
con `rollback_completed=False`; lock real con restore completo), y
`tests/` no contiene ningún test que haga fallar 1 de N restores y verifique que la
property da `False`. Es un gap de cobertura, no un bug.

---

## H4 — "`MO2Controller` construido dos veces / cache sin invalidación" (#301): mixto

**Lo verdadero:**

- `_build_grass_dependencies` crea un `MO2Controller` nuevo en cada llamada
  (`sky_claw/antigravity/orchestrator/supervisor.py:549`).
- `_ensure_runtime_deps` cachea las deps en la primera resolución exitosa y nunca
  re-consulta el provider (`grass_cache_service.py:188-189`: `if self._profile_manager
  is not None: return`).
- Existe **otra** instancia de `MO2Controller` en `AppContext`
  (`sky_claw/app_context.py:519`), consumida por `SyncEngine` (`app_context.py:604`) y
  `AsyncToolRegistry` (`app_context.py:684`). Dos instancias vivas implican dos
  `_launched_procs` independientes: si el juego se lanza por una y se intenta cerrar
  por la otra, el tracking no lo ve. Preocupación arquitectónica legítima.

**Lo falso:**

- *"El supervisor ya tiene su propio MO2Controller (vía el campo `mo2` que los
  servicios de Synthesis/LOOT consumen)"* — el supervisor **no tiene ningún**
  `MO2Controller` (grep completo de `supervisor.py`), y los servicios de Synthesis/LOOT
  no consumen uno. La instancia paralela real es la de `AppContext`, no una del
  supervisor.
- El escenario *"el usuario cambia el perfil activo entre invocaciones"* no puede
  ocurrir en runtime: `supervisor.profile_name` se asigna solo en `__init__`
  (`supervisor.py:146`) y nada lo muta después. Cambiar de perfil implica reconstruir
  el supervisor, lo que reconstruye también el `GrassCacheService` y su cache. El caso
  stale que sí existe es cambiar `MO2_PATH`/`SKYRIM_PATH` a mitad de sesión después de
  la primera corrida de grass — edge case menor, no el mecanismo descrito.

---

## H5 — (truncado en el original): no evaluable completo

El fragmento visible plantea que un `launch_game` re-entrante reasigne
`self._launched_procs[pid]` con un `create_time` nuevo **para el mismo PID** mientras
`close_game` procesa el snapshot viejo, y que el `pop` posterior borre el registro
nuevo. Requiere que el SO reuse exactamente el mismo PID dentro de la ventana de un
cierre — teóricamente posible, prácticamente irrelevante, y el diseño del snapshot está
razonado en el comentario de `vfs.py:410-414`. El resto del hallazgo quedó truncado y
no se puede evaluar.

---

## Residuo accionable real

De toda la auditoría, lo que sí vale la pena capturar como follow-ups:

1. **Test de rollback parcial con el lock real** (cierra el único punto válido de H3):
   un test en `tests/test_synthesis_rollback_honesto.py` (o en los tests de locks) que
   haga fallar el restore de 1 de N snapshots y verifique `rollback_completed is False`
   + `rollback_failures == [<path>]` + TX en PENDING.
2. **Unificar el `MO2Controller`** (punto válido de H4): inyectar la instancia de
   `AppContext` en `_build_grass_dependencies` (o exponer un accessor), o al menos
   invalidar el cache de `_ensure_runtime_deps` si `MO2_PATH`/`SKYRIM_PATH` cambian.
3. **Opcional (higiene, H1):** un `asyncio.Lock` alrededor de la región
   snapshot→kill→pop de `close_game` para blindar el diseño ante un eventual futuro
   free-threaded. Hoy no hay bug bajo el GIL.

Ninguno es bloqueante; los tres son mejoras incrementales sobre código que ya se
comporta correctamente en los escenarios que la auditoría describe como críticos.
