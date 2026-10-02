# ADR 0012 — GP2-S4E: production wiring de la transacción de protección

**Estado:** Aceptado (implementado).
**Fecha:** 2026-10-02.
**Relacionado:** [0009](0009-runtime-vault-golden-protection-status.md) (estado),
[0010](0010-runtime-vault-golden-protection-apply.md) (contrato de apply).

## Contexto

Al cerrar [GP2-S4D](0010-runtime-vault-golden-protection-apply.md) el Runtime
Vault tenía las siete piezas de la transacción de protección del Golden
auditadas, tipadas y con sus propias suites — y **ninguna cableada
productivamente**. El censo de call-sites sobre `origin/main`
(`0103ee4f6de15207032d25c254ede5cf2c01bff9`) lo midió, no lochoolsupuso:

| Orquestador | Call-sites `[PROD]` |
|---|---|
| `orchestrate_golden_protection_planning` | 0 |
| `establish_privileged_authorization` | 0 |
| `promote_durable_authorized_plan` | 0 |
| `create_protection_journal` | 0 |
| `apply_authorized_plan` | 0 |
| `recover_interrupted_protection` | 0 |
| `finalize_protection_transaction` | 0 |

Cero módulos fuera de `sky_claw/local/runtime_vault/` importaban el paquete. No
existía `protection_service.py` —que ADR 0010 §974 nombra—, ni el ancla
`GP2-T27`, ni ninguna primitiva de descubrimiento de operaciones pendientes.

Dos huecos concretos, ambos de la clase que el repo declara dominante (un fix
que aterriza en un camino y deja intacto a su gemelo):

1. **El handoff S4-C → S4-D no existía.** `recovery_orchestrator` lo describía
   en prosa (`recovery_orchestrator.py:46`: un apply físico completo devuelve
   `POST_VERIFICATION_REQUIRED` y S4-D debe cerrar la transacción) y **ningún
   módulo lo realizaba**. S4-C retenía el lock para que alguien pudiera
   continuar, y ese alguien no existía.
2. **No había un camino productivo de descubrimiento/recovery.** Tras una
   muerte dura el único recovery posible era el que un test写的 otro proceso
  Supplier con un `operation_id` de su propia invención.

## Decisión

Se agrega **un** módulo nuevo,
`sky_claw/local/runtime_vault/protection_service.py`, que compone las siete
piezas sin reimplementar ninguna, más el router de restart y el barrido de
arranque. Un solo entrypoint productivo de la transacción completa, en el
composition root real del producto (`AppContext._start_full_inner`), siguiendo
el precedente exacto de `reconcile_orphan_precache_flag` (U-03) y
`reconcile_orphan_rollback_backups` (U-08): bloque best-effort que no bloquea
el arranque.

## Los contratos que este slice respeta, y de dónde salen

El flujo implementado **no** se derivó de este ADR sino del código vigente. Las
decisiones donde el contrato real manda sobre la intuición:

* **`ROLLBACK_REQUIRED` y `ROLLING_BACK` nunca van a S4-D.** Su
  `_FASES_ADMISIBLES` los excluye y devuelve `NOT_APPLICABLE`
  (`finalization_orchestrator.py:639`). El motor del rollback es S4-C.
* **`PREPARING` / `PREPARED` / `AWAITING_ELEVATION` no son alcanzables** bajo
  el replay del journal autoritativo (su estado inicial es `applying` y no hay
  aristas de retorno), pero se enrutan a S4-C, que ya los clasifica como
  evidencia contradictoria fail-closed (`recovery_orchestrator.py:658`). S4-E
  no inventa un cuarto destino.
* **`APPLYING` va a S4-C primero, nunca directo a S4-D.** Sólo cuando el apply
  físico quedó completo (todos los nodos `MUTATED` durable) S4-C devuelve
  `POST_VERIFICATION_REQUIRED`, y **ahí** S4-E encadena S4-D, que re-toma el
  lock huérfano de la MISMA `operation_id`. Ese es exactamente para lo que S4-C
  lo retuvo con `retain_for_inspection()`. **Este encadenamiento es la pieza
  que S4-C documentaba y nadie ejecutaba.**
* **`COMMITTED` va a una ruta de normalización** (`S4D_NORMALIZE`), no a
  apply: S4-D relee el backup, verifica que sea de esta operación y libera el
  lock huérfano, sin re-aplicar, sin gates, sin archivado.

La tabla `RESTART_ROUTES` es **exhaustiva sobre el enum vivo** de 18 estados y
`route_for_state` **no tiene valor por defecto**: un estado nuevo del FSM hace
`KeyError` hasta que alguien decida a qué suborquestador pertenece. El anchor
`test_todo_estado_del_fsm_tiene_una_ruta` congela la igualdad de conjuntos y
`test_la_ruta_se_decide_por_el_contrato_vigente_no_por_comodidad` congela el
mapeo completo.

## Superficie de API

La API superior recibe **intención de dominio**, no autoridad de filesystem.

* `protect_golden_root(root, operation_id, authorization, …)` — la
  `operation_id` es identidad durable; `root` es la ubicación del Golden, que es
  conocimiento de dominio, no un path autoritativo.
* `resume_golden_protection(operation_id, …)` — **sólo** `operation_id`.
* `discover_pending_operations()` — read-only, nunca autoridad.

No se expone como parámetro público ninguna ruta de plan, de journal, de backup
ni de lock, ni el SD del PRE, ni un ejecutable arbitrario.
`test_el_camino_de_recuperacion_no_acepta_paths` congela la superficie exacta.

## Invariantes garantizados por construcción

* **Continuidad del `GoldenMutationLock`**: la sesión de frontera que abre S4-A
  es la MISMA que se pasa a S4-B y a S4-D. No hay ventana
  `release → reacquire` porque el servicio nunca suelta el handle entre apply y
  finalización, y S4-D reutiliza `session.lock` sin re-adquirir
  (`finalization_orchestrator.py:493`).
* **Librar-vs-retener lo decide el desenlace, no un default.** `settled`
  (cerrado: commiteó, revirtió o no aplicaba) libera; cualquier otro desenlace
  **retiene** el lock con `retain_for_inspection()`. Soltar sobre un Golden que
  puede terminar en `ROLLBACK_REQUIRED` abre la ventana que S4-D declara
  prohibida en su propio argumento.
* **`STAGING != AUTHORITY`**: `UNTRUSTED_STAGING_AS_RECOVERY_SOURCE = "NEVER"`,
  con anchor AST que prohíbe importar `clone` y con otro que prohíbe las lecturas
  del candidate manifest.
* **Ningún `COMMITTED` fuera de S4-D**: anchor AST sobre el conjunto de
  primitivas prohibidas (`SetSecurityInfo`, `apply_target_dacl_by_handle`,
  `restore_security_descriptor_by_handle`, `commit_finalized`,
  `enter_finalization_phase`, `record_node_mutation_intent`, …).
* **Ningún privilege shuffle**: no se solicita ninguno de los privilegios que el
  anchor de `test_runtime_vault_operator_token.py` veta por nombre, y no se
  acepta token aportado por el caller como autoridad.
* **Sin FSM paralelo**: el FSM durable sigue siendo el `ProtectionJournal`.
  `RestartRoute` sólo decide; no se persiste ni aparece en el journal.

## Dos invariantes que este slice discovered y congeló

1. **`ROLLED_BACK` es `settled` y no `committed`.** La invariante de
   `fail_closed_reason` medía contra el conjunto "endurecido", así que un
   rollback **correcto** —desenlace completo, Golden devuelto a su estado previo
   y probado— quedaba fuera y el DTO rechazaba construirlo. Todo `ROLLED_BACK`
   real terminaba en `ValueError` dentro de la proyección de S4-C. Anchor:
   `test_rolled_back_esta_cerrado_pero_no_committed` y
   `test_la_frontera_libera_tras_un_rollback_cerrado`.

2. **El resolver de ProgramData tiene que llegar a las DOS mitades del
   barrido.** `reconciliar_arranque_pendiente` pasaba el resolver a
   `discover_pending_operations` y no a `resume_golden_protection`: el barrido
   enumeraba un namespace y el resume leía `%ProgramData%` **productivo**. En
   producción no se manifiesta —ambos default al mismo lugar—, que es
   exactamente por lo que las dos mitades quedan ancladas juntas. Anchor:
   `test_el_resolver_de_programdata_llega_al_barrido_y_al_resume`.

Ambas son la clase de defecto que el repo declara dominante, y ninguna era
visible leyendo el reporte de un test: aparecieron porque el RIG de Windows y
el camino de arranque se ejercitaron con procesos y discos reales.

## Defecto abierto que S4-E **expone** pero no puede cerrar: el handoff S4-C → S4-D

**Este es el hallazgo más importante del slice, y fue el CI de Windows el que lo
vio.** Localmente el caso E03 pasaba por suerte de timing (ver abajo); en
`windows-latest` falló en py3.11 y py3.12, y la causa no era el test.

La cadena, con evidencia:

1. El proceso A muere por `os._exit` después de que `apply_authorized_plan`
   volvió, con los tres nodos `MUTATED` y su WAL durable flushed.
2. El proceso B corre el router de S4-E. `APPLYING` → **S4-C**, que clasifica el
   apply como completo y devuelve `POST_VERIFICATION_REQUIRED` **con el lock
   retenido** (`ACQUIRED_RETAINED`). S4-E encadena **S4-D**. Hasta acá todo
   correcto: el handoff que este ADR describe S4-C documentando en prosa
   funciona.
3. **S4-D falla.** Sin `session=`, `_adquirir_lock_para_finalizacion`
   (`finalization_orchestrator.py:493`) re-adquiere por el camino de recovery,
   que sólo acepta un lock **huérfano**. El lock está retenido por **este mismo
   proceso**, con `pid + creation-time` coincidentes → `GoldenLockBusyError` →
   `LOCK_BUSY`, **cero gates ejecutados**.

El detalle que lo vuelve un defecto y no una decisión: la propia docstring de
`_adquirir_lock_para_finalizacion` (`finalization_orchestrator.py:481-484`)
anticipa exactamente este caso — *"S4-D siguiendo inmediatamente a S4-C en el
mismo proceso"* → reusar `session.lock`, sin soltar ni re-tomar. **S4-D ya fue
diseñado para este handoff. Lo que falta es que S4-C pueda entregárselo.**

`recover_interrupted_protection` devuelve un `RecoveryForensicReport` con un
`RecoveryLockOutcome` (un enum), **no** el handle retenido ni una sesión. El
caller no tiene con qué construir el `session=` que S4-D espera.

Dos maneras de cerrarlo, ambas fuera del diff de S4-E:

* **Extender S4-C** para que su reporte (o un nuevo return) exponga el handle
  retenido / la sesión, de modo que S4-E pueda pasárselo a S4-D.
* **Hacer reentrante la re-adquisición de S4-D** para un lock poseído por el
  mismo proceso **y** la misma `operation_id` (con su `creation-time`
  verificado, para no abrir una vía de stealing).

S4-E compone y no reimplementa (§26/§27/§28): no puede tocar esos contratos por
su cuenta, y hacerlo en silencio sería exactamente el patrón que este repo
prohíbe. Queda **abierto y declarado**.

Ancla: `test_e03_crash_todos_mutados_pre_finalizacion_reenruta`, marcado
`xfail(strict=True)` con el diagnóstico completo en el `reason`. `strict=True`
importa: cuando alguien cierre el defecto, el `xpass` se vuelve rojo y obliga a
quitar el marker — el test no puede quedarse verde por la vía del marcador.

### El mismo caso también exige el borde de crash del propio RIG

La primera versión de E03 pedía la muerte desde dentro del puerto, al volver del
segundo `SetSecurityInfo`. Eso está mal por una razón que conviene escribir:
**en ese instante el nodo todavía no tiene su registro `MUTATED` en el WAL** — lo
escribe el engine después, con su flush. Matar ahí deja el apply incompleto, y
el router hace rollback. La misma corrida daba `committed` en una máquina y
`rolled_back` en otra, según si ese flush había ocurrido: un test verde por
suerte de timing, que es lo peor que puede hacer un crash test. El borde
determinista es **después** de que `apply_authorized_plan` volvió, que es la
única prueba de que cada nodo cerró su WAL.

## Lo que NO cubre S4-E

* **GP3, unprotect, GUI, y el cableado de tool del LLM.** El coordinator es un
  domain service: deliberadamente **no** se expone como tool del
  `tool_dispatcher` ni del `AsyncToolRegistry`. Ese cableado es trabajo posterior
  y con su propio security review. ADR 0010 §974 anticipa el ancla `GP2-T27`
  para cuando exista.
* **`POWER LOSS = NOT CLAIMED`.** Los RIG matan procesos (`os._exit` y
  `taskkill /T /F`); ninguna de esas cosas demuestra durabilidad ante corte de
  luz.
* **REAL USER GOLDEN = NO.** Los RIG corren sobre `%TEMP%\SkyClaw-S4E-RIG-<uuid>`
  con guard duro contra Skyrim, Steam, MO2, el Golden del usuario y
  `%ProgramData%` productivo.
* **GP1 / quiescence / RV-2 están DEGRADADOS en el RIG.** El puerto de
  verificación aprueba los gates en vez de observarlos. Un fake port **no** es
  validación nativa y el RIG no lo afirma en ningún lado. La frontera de S4-A
  tampoco corre (el RIG no está elevado): se toma el `GoldenMutationLock` real
  con la primitiva de producción, de modo que la continuidad del lock sí se
  ejercita, pero la elevación y el PPSC quedan sin probar.

## RIG de Windows

`tests/test_runtime_vault_s4e_windows_rig.py` corre el pipeline real (3
`SetSecurityInfo` reales, journal con flush verificado, archivado del backup,
`COMMITTED`, lock Win32 con share mode 0) contra un namespace aislado, con
procesos reales y breadcrumbs con nonce por corrida. Cubre E01, E02, E03, E08,
E09, E10, E11, E12, E13, E14, E15 más discovery y arranque.

Los casos E04–E07 (crash dentro de cada gate de S4-D y dentro de
`ARCHIVING_BACKUP`) **no tienen test propio en este slice**: el RIG sabe matar
el proceso en la frontera de apply y después del `COMMITTED` durable, pero no
tiene un punto de muerte inyectado entre gates de S4-D. Queda como
follow-up; la ruta que esos estados exercise es la misma que cubre E03 y E08,
que comparten `S4D_FINALIZE`/`S4D_NORMALIZE`.

## Deuda que este slice deja explícita

Ninguna de estas entra en S4-E, con la relación causal verificada en el PR:

* **RV-3 sealed directory walker** — `clone._capture_directory_structure` hace
  `link_kind_and_identity_or_raise` **antes** de `os.scandir` sin revalidar
  después (`clone.py:139-160`), al contrario que `inventory_tree`
  (`inventory.py:264-276`). Confirmado. S4-E no depende de `create_runtime_clone`
  (0 call-sites), así que no era causalmente necesario cerrarlo acá.
* **RV-1 handle-bound inventory hardening** — ver abajo; el probe dio
  `NOT_OBSERVED` en NTFS, así que no bloqueó.
* **API hygiene** — `runtime_vault.__all__`: 508 entradas, 507 únicas,
  **0 faltantes**, 1 duplicado (`derive_authorized_plan_dir`). La cifra
  "55 missing" de la revisión independiente **no se reproduce**.
* **Timestamp helper** — `golden_admission_service._sumar_microsegundo` parsea
  `%z` y reemite `Z` literal. Mitigado: sólo recibe UTC y su único caller es
  interno.
* **Critical expectations** — `verify_critical_files` (`golden.py:52-57`) y
  `verify_golden_master` (`golden.py:137-141`) duplican la validación de
  expectativas y ambos lanzan `ValueError` crudo, que escapa de
  `create_runtime_clone` (`clone.py:296`, fuera de todo `try`). S4-E no llama
  esa función.
