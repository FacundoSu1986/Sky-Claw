# Historial OODA — julio de 2026

> **Tipo:** auditoría histórica; no es el inventario operativo vivo.
> **Fuente canónica actual:** [`../pending_ooda_status.md`](../pending_ooda_status.md).
> Los addenda preservan la evidencia y la secuencia de decisiones de su fecha,
> pero pueden describir estados ya superados.
> **Audiencia:** maintainers, reviewers y agentes.
> **Estado:** Histórico.
> **Última verificación como inventario vivo:** 2026-07-25 sobre `origin/main`
> `c6ab35e`.

**Snapshot contra:** `main @ b00d403`.
**Método:** Observe/Orient — se verificó cada ítem contra el **código actual**
(no solo contra los docs de backlog, que están desactualizados en varios
puntos: `TECHNICAL_REVIEW_TASKS.md` es del 2026-07-06 y no refleja el trabajo
mergeado desde entonces). Reemplaza la necesidad de releer `TECHNICAL_REVIEW_TASKS.md`,
`ZERO_TRUST_TODO.md` y el `skyclaw_ooda_analysis.md` completos para saber "qué falta".

Este documento es un **snapshot**, no una fuente viva — quedará desactualizado
a medida que se mergeen PRs. Reverificar contra el código antes de actuar sobre
cualquier ítem, como pide `AGENTS.md`.

## Addendum (2026-07-26) — la cadena de `app.on_shutdown` no puede truncarse

Revisión adversarial del PR #370 (rename `antigravity` → `app`, ya mergeado en
`6cb4024`). El rename en sí quedó limpio: `sky_claw.spec`, `pyproject.toml`,
`ci.yml`, `.gitignore` y los overrides de mypy son renombres 1:1 fieles y no
sobrevive ninguna referencia viva al namespace retirado. Lo que sí apareció es
una regresión de lifecycle en el hardening de shutdown que viajó en ese PR.

**Causa raíz — contrato de terceros mal asumido.** NiceGUI 3.12.1 despacha los
handlers de `app.on_shutdown` en un bucle secuencial **sin `try/except`**
(`nicegui/app/app.py`, `App.stop`); `normalize_lifecycle_handler` devuelve el
callable tal cual, no lo envuelve. Una excepción en un handler trunca todos los
siguientes. Es el mismo modo de falla que P1-1 (#367) blindó **dentro** de
`_teardown_runtime`, pero un nivel más arriba, donde ese blindaje no llegaba.

Orden real: `run_nicegui` llama `setup_app()` (que registra `_cleanup`) antes de
registrar `_shutdown`, así que **`_cleanup` es el handler #1**. Como
`AgentCommunicationClient.stop()` no atrapa nada, un WebSocket roto al apagar
bastaba para que nunca corrieran `runner.cleanup()` (libera el 8765) ni
`ctx.stop()` (DB, journal, locks) → WinError 10048 al reabrir.

Cerrado en la rama `fix/gui-shutdown-handler-chain`:

1. `_cleanup` pasa a pasos resilientes; el handler siempre retorna normalmente.
2. `drain_tracked_tasks()` nuevo en `task_tracking`: las tasks de
   `create_tracked_task` viven en un set propio que no drenaba nadie, así que un
   "Análisis profundo (xEdit)" en vuelo perdía su escritura en silencio contra
   una DB ya cerrada. Ahora se drenan (5.0 s) antes de cerrar nada.
3. El cierre del `DatabaseAgent` de la UI pasa a ser el **último** paso de
   `_teardown_runtime`, después de `ctx.stop()`.

**Corrección tras la review de Codex (3 hallazgos, todos válidos):**

- *(P1)* El drenado sacaba su foto de `_BACKGROUND_TASKS` **antes** de parar el
  `event_bus` y el cliente. Un evento entregado en esa ventana ejecuta
  `handle_conflict_detected`, que crea `gui-conflicts-refresh` — una task que el
  drenado nunca vio, y que reabría la pérdida de escrituras. Ahora se acallan los
  productores primero (cliente → bus → tick del loop para las callbacks ya
  encoladas por `call_soon_threadsafe`) y recién después se drena.
- *(P1)* La espera post-cancelación era un `gather` sin deadline: una task que
  tragara `CancelledError` colgaba el handler para siempre. Ahora está acotada
  (`_CANCEL_GRACE_S`) y las rebeldes se abandonan con un ERROR que las nombra.
  El drenado además re-mira el set en bucle, porque una task puede encolar otra.
- *(P2)* **El reordenamiento por sí solo NO eliminaba el warning de "checkpoint
  incompleto": lo mudaba de manager.** Verificado con dos `DatabaseAgent` sobre
  el mismo archivo: SQLite borra `-wal`/`-shm` solo al cerrar la **última**
  conexión, así que el warning lo emite *el primero que cierra*, sea quien sea.
  El arreglo real fue en `db_lifecycle.shutdown_all` paso 3: si el checkpoint
  TRUNCATE reportó `busy == 0`, los datos ya son durables y los sidecars que
  quedan los explica el otro owner → INFO con la causa real, no WARNING. El
  WARNING queda reservado para cuando el checkpoint efectivamente no completó.

**Gotcha operativo de la migración:** en un working tree que existía antes del
rename, `sky_claw/antigravity/` sobrevive como `__pycache__` huérfano (untracked).
Python lo resuelve como namespace package y `tests/test_namespace_migration.py`
falla localmente aunque el árbol esté correcto. Borrar el directorio; no es un
defecto del código.

**Cerrado:** el smoke real del `.exe` de este fix (cierre, 8765 libre, ausencia
del warning de WAL/SHM y reapertura sin WinError 10048) fue verificado contra el log real.
Nota de cierre: validado con `SKY_CLAW_GUI_EXIT_GRACE_SECONDS=5` (default de producción: 15s).

## Addendum (2026-07-21) - F8 USVFS de la auditoria externa

Este item es distinto del F8 de resiliencia #319 que aparece mas abajo.

**Infraestructura aceptada e implementada (ADR 0007):** broker async loopback
autenticado, manifests firmados, plugin MO2, worker descartable, attestation de
worker+nieto, perfil explicito, serializacion por instancia, cancelacion con
Win32 Job Object e instalador transaccional. El bundle entra en PyInstaller.

**Cobertura productiva actual:** `health` y dos entry points de LOOT (agente
lock-only y Supervisor GUI/HITL) usan el broker y fallan cerrados sin USVFS; no
reconstruyen un subprocess standalone. El preview calcula el
fingerprint sin mantener un worker vivo y la ejecucion lo revalida tras HITL.
Cada preview queda ligado a su propia invocacion async; resultados y
cancelaciones esperan confirmacion terminal del Job Object antes de permitir
rollback o liberar el lock. La entrega del resultado usa ACK causal antes de que
el worker salga y el cierre concurrente del broker esta serializado.

**Smoke real 2026-07-22:** `vfs-health` paso con exit code 0 sobre MO2 2.5.2 y
USVFS 0.5.6.1, perfil `Default`, con ambos ordenes de arranque. Worker y nieto
validaron un canary habilitado ausente del `Data` fisico y el cierre no dejo
procesos. El smoke descubrio tres defectos de frontera (despacho Qt, Unicode del
logger embebido y espera del HANDLE fuera de Qt), corregidos y anclados en este
PR.

**Pendiente para cerrar F8 de forma transversal:** migrar el tercer caller de
LOOT del preview chain, xEdit, Wrye Bash, Synthesis, DynDOLOD y los demas runners
externos al mismo contrato. Tambien faltan los smokes de perfil incorrecto,
LOOT representativo, timeout/cancelacion y rollback byte a byte. CI no prueba
inyeccion USVFS real.

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

```console
$ grep -rln "persist_action_manifest\|persist_flight_report" sky_claw/ | grep -v test
sky_claw/app/db/journal.py       # la definición
sky_claw/local/tools/loot_service.py     # productor (T-26/T-28 v1)
sky_claw/local/tools/xedit_service.py    # productor (2026-07-15, ambos entry points)
sky_claw/local/tools/synthesis_service.py # productor de T-26 (2026-07-16, PR #309)
```

**Actualización 2026-07-16 (PR #309 — Synthesis emite T-26; T-28 diferido):**
`synthesis_service.py` es ahora productor del `ActionManifest` (T-26):
`execute_pipeline` lo persiste fail-closed ANTES de correr los patchers (si no
se puede persistir, no se muta; `tool="Synthesis"`). **T-28 (FlightReport)
queda deliberadamente FUERA** en Synthesis: corre SIEMPRE en sandbox
(`tool_dispatcher`) con un `StagingJournal` de **commit diferido** hasta la
promoción, así que un informe compuesto dentro de `execute_pipeline` reflejaría
un estado pre-promoción (pending) y quedaría stale; además el manifiesto
registra la ruta del **clon** (`clone.overwrite_copy`) que se descarta tras la
promoción. El cierre correcto del informe post-vuelo + la traducción a rutas
reales pertenecen al **promotion flow** (`ExecuteSynthesisPipelineStrategy` /
`SandboxPromotionFlow`, dueño del mapeo clon→real) — follow-up documentado.

**Actualización 2026-07-16 (PR follow-up — T-28 de Synthesis cerrado vía el
promotion flow):** `ExecuteSynthesisPipelineStrategy` ahora emite el
`FlightReport` DESPUÉS de resolver el staged journal (`commit_staged`/
`rollback_staged`), componiéndolo desde el journal REAL para la TX ya resuelta
(estado final `committed`/`rolled_back`, no el `pending` diferido). Traduce las
rutas del clon a las reales del overwrite **solo al promover** (reusa
`rewrite_clone_paths`, ex-`_rewrite_clone_paths`, ahora público); un descarte
conserva la ruta del clon porque no se aplicó nada al real. `StagingJournal`
expone `staged_transaction_id` para capturar la TX antes de que
`commit_staged`/`rollback_staged` la reseteen. Componer desde el journal real
(no el staging) sortea el hueco de read-APIs del `StagingJournal`.

**Actualización 2026-07-16 (T-26/T-28 en DynDOLOD):** `dyndolod_service.execute`
emite el `ActionManifest` fail-closed tras `begin_transaction` (antes de generar
LODs; `tool="DynDOLOD"`, `files_touched` = los mods de salida) y el
`FlightReport` best-effort tras `commit_transaction` (DynDOLOD es directo, no
sandboxeado → emisión en el servicio, espejo de xEdit; no pasa por el promotion
flow). Un fallo del manifiesto aborta con `reason="ActionManifestFailed"` y la TX
marcada rolled_back. LIMITACIÓN documentada: el `rollback_plan` del manifiesto
queda vacío (`snapshots=[]`) porque el rollback de DynDOLOD es el move-aside de
`DirectoryRollback` (los `Output/` pesan GBs), no el snapshot manager — un plan
consciente del move-aside es follow-up.

**Actualización 2026-07-18 (T-26/T-28 en Pandora):** `pandora_service.generate_animations`
emite ahora el `ActionManifest` fail-closed ANTES de correr el subproceso
(`tool="Pandora"`, `files_touched` = los dirs candidatos de salida de behavior
graphs) y el `FlightReport` best-effort tras el commit. Espeja la disciplina de
`loot_service`: el gating es por **presencia del journal**, y AMBOS paths de
producción lo cablean vía `app_context` — el GUI/dispatcher (`supervisor` inyecta
`journal=self.journal`) y el del agente LLM/Telegram (`system_tools.run_pandora`
recibe `journal=self._journal`, igual que `run_loot_sort`). Sin journal (callers
legacy / tests) no se emite (comportamiento previo intacto). El snapshot es
diferido (`target_files=[]`, como el lock), así que el `rollback_plan` del
manifiesto queda vacío por diseño (la salida sale vía el VFS de MO2 con `cwd`; el
manifiesto registra los dirs tocados para auditoría, no un plan de restore). Un
fallo del manifiesto aborta Pandora sin correr (`reason="ActionManifestFailed"`)
y marca la TX rolled-back; un run non-zero, una `CancelledError`
(shutdown/timeout) y un `LockLeaseLostError` en el `__aexit__` del lock también
cierran la TX rolled-back en vez de dejarla PENDING (contrato T11 — siempre
devolver dict serializable, salvo la cancelación que se re-lanza tras cerrar la
caja negra). Anclado en `test_pandora_service.py`
(`test_emite_manifest_antes_de_correr...`, `test_manifest_fail_closed...`,
`test_lease_perdida...`, `test_cancelacion_marca_rolled_back...`, etc.) y
`test_pandora_agent_gate.py` (`test_run_pandora_agent_path_emite_caja_negra...`).
Los 3 P2 del review de Codex sobre el PR #318 (CancelledError, LockLeaseLostError,
journal en el path del agente) se verificaron reales y se cerraron en el mismo PR.

**Actualización 2026-07-18 (T-26/T-28 en Wrye Bash — 6/6 CERRADO):** cierra el
"PR C" anunciado en el #315. `wrye_bash_service.execute_pipeline` emite ahora el
`ActionManifest` fail-closed ANTES de correr `bash.py` (`tool="Wrye Bash"`,
`files_touched` = la ruta del Bashed Patch en `Data/`) y el `FlightReport`
best-effort tras el commit. Se implementó como CONTINUACIÓN del
`WryeBashPipelineService` del otro agente (#315), no como diseño competidor —
espeja `loot_service`/`pandora_service`: journal OPCIONAL cableado en AMBOS paths
de producción vía `app_context` (el `supervisor` inyecta `journal=self.journal`,
y `system_tools.generate_bashed_patch` recibe `journal=self._journal`). Snapshot
diferido → `rollback_plan` vacío por diseño (Wrye Bash solo LEE el load order;
la salida sale vía el VFS de MO2 con `cwd`). Endurecido desde el arranque con las
lecciones del review de Codex sobre #318: fail-closed del manifiesto
(`reason="ActionManifestFailed"`), y cierre de la TX rolled-back ante run
non-zero, `WryeBashExecutionError`, `LockLeaseLostError` (ya cubierto por el
`except LockError` de #315) y `asyncio.CancelledError` (re-lanzada), más una red
T11 final. Anclado en `test_wrye_bash_service.py` (10 tests nuevos) y
`test_wrye_bash_lock.py` (`test_handler_con_journal_emite_caja_negra`).

**T-26/T-28 COMPLETO — 6/6 rituales mutantes** producen la caja negra: LOOT,
xEdit, Synthesis, DynDOLOD, Pandora y **Wrye Bash**. Con esto tiene sentido la
vista GUI de T-28. `tool_version` queda en `None` para xEdit, Synthesis, DynDOLOD,
Pandora y Wrye Bash (no la exponen hoy) — follow-up menor. El **preflight brutal
de Wrye Bash (PR B, #323)** ya se mergeó (el otro agente): esta rama se rebasó
sobre él e integró la caja negra (PR C) *encima* del preflight — ambos gates
conviven en `execute_pipeline` (preflight primero, luego el manifiesto dentro del
lock). Con PR B + PR C, Wrye Bash queda al día con la disciplina de sus hermanos.

### 1.2 T-27 — Synthesis sandboxeado con promote/discard real (2026-07-14); Pandora/DynDOLOD/Wrye Bash siguen fuera

**Actualización 2026-07-14 (cierre PARCIAL — no declarar T-27 "cerrado"):**
`SandboxPromotionFlow` (`sky_claw/app/orchestrator/sandbox_promotion.py`,
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

**Actualización 2026-07-16:** el preflight quedó cableado en **Synthesis**
(T-16c·2, #306) y en **DynDOLOD** (T-16c·3): `dyndolod_service.execute` corre el
gate ANTES de adquirir el lock / abrir transacción / publicar el evento de
inicio — un rojo (p. ej. el dir de salida sin permisos) cancela el run de 30+
min / GBs; amarillo/verde no bloquean y se surface en todos los retornos. Los
sensores (vfs + permisos sobre `mods/` y los `*/Output` + masters/límites del
perfil MO2 activo + overwrite) reusan las primitivas compartidas de T-16d; la
costura del resolver del perfil MO2 se extrajo a
`build_mo2_profile_sources_resolver` (compartida por Synthesis y DynDOLOD).

**Actualización 2026-07-17 (T-16c·4 — preflight en Pandora):**
`pandora_service.generate_animations` corre el gate ANTES de tomar el lock / correr
el subproceso. Sensores a-medida: **vfs** (rutas crudas) + **permisos** sobre los
dirs candidatos de salida de behaviors (`Data`, `overwrite`, dir del exe — el
destino exacto es dependiente del entorno VFS/standalone, se sondean todos los
resolubles) + **overwrite sucio**. NO cablea masters/límites: Pandora procesa mods
de ANIMACIÓN (FNIS/Nemesis), no el load order de plugins. **Falta preflight en:**
Wrye Bash.

**Follow-up conocido de Pandora:** el servicio NO tiene el journal cableado (a
diferencia de los otros 4 rituales) → T-26/T-28 requieren primero agregar el
journal + abrir transacción. Y fijar el destino de salida exacto (probablemente
lanzando vía MO2) para permisos/manifest precisos + un rollback real — trabajo de
dominio, follow-up separado.

---

## 2 — Deuda estructural continua (no bloquea GA, mantenimiento de fondo)

### 2.1 T-12 — mypy estricto módulo a módulo

`pyproject.toml` sigue con `ignore_errors = true` para el grueso del
árbol: *"Currently 1,684 mypy errors across ~30 modules"* (comentario
desactualizado en número exacto, pero la exención sigue activa). Se migra de
a un módulo por PR; sin fecha límite. **Avance 2026-07-21:** `local/fomod/*`
migrado a `strict = true` (ver addendum del 2026-07-21). **Avance 2026-07-28:**
`local/loot/*` migrado también (ver addendum del 2026-07-28) — 0 errores, sin
cambios de código. Quedan en `ignore_errors` los bloques grandes:
`sky_claw.app.*` (gui/comms/agent/modes/orchestrator/scraper/web/db/security/core),
`local.tools.*`, `local.xedit.*`, `local.mo2.*`, `local.validators.*`,
`sky_claw.local.auto_detect`, `sky_claw.local.local_config`,
`local.discovery.*`, `app_context`, `config` y `tools_installer`.

### 2.2 T-10/T-11 — BLE001 (except genérico) sin activar en la mayoría del árbol

Cerrados solo `local/tools/` (T-10) y `local/xedit/` (T-11). La lista de
exenciones en `pyproject.toml:130-142` sigue cubriendo, entre otros:
**todo `sky_claw/app/**`** (el árbol más grande del repo — orchestrator,
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
   encuentra nada. Parcial: el `CredentialVault` **ya está cableado** al
   `LLMRouter` (F1 de la auditoría Zero-Trust 2026-07-18 — master-key desde
   `SKYCLAW_VAULT_MASTER_KEY`, ver `tests/test_credential_vault_wiring.py`), así
   que el hot-swap de credenciales dejó de ser código muerto; falta todavía el
   método `get_key(name)` con backend keyring y la consolidación del resto de
   los secretos (hoy en `Config`/keyring por separado).

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

## Addendum (2026-07-16) — cierre del "parche placebo" + Fase 1 AI-assisted

**Cerrado en este PR** (verificado con tests ancla, no solo con el diff):

- **Éxito placebo del parcheo crítico.** Los 3 scripts `.pas` que
  `ExecuteXEditScript._select_script_for_conflicts` referencia
  (`fix_npc_conflicts`/`fix_quest_conflicts`/`fix_critical_conflicts`) nunca
  existieron; el pipeline degradaba en silencio a `TEMPLATE_APPLY_PATCH`
  (cuerpo de `Process` placeholder, sin lógica) y reportaba éxito con un
  `.esp` vacío. Ahora: `can_handle` exige script en disco, `create_plan` y el
  generador fallan closed, y el service pasa el plan REAL del orquestador al
  runner (antes lo reconstruía perdiendo `script_path`/`form_ids`). Anclado en
  `tests/test_patch_placebo_fail_closed.py`.
- **Fase 1 AI-assisted (advisory).** `AIAssistedPatch` (priority=1, catch-all)
  enruta los conflictos críticos sin script al `PatchAdvisorLLM`
  (`sky_claw/local/ai/`): recomendaciones fail-closed en `warnings`, sin
  mutación (`output_path=None`), HITL visible en `describe_for_approval`.
  Sin LLM configurado → error accionable, jamás placebo. El callable LLM lo
  arma `AppContext.make_patch_advisor_llm()` (lazy: respeta el boot de la GUI
  y el hot-swap de provider) sobre `LLMRouter.complete_simple`.
- **`rolled_back` honesto en Synthesis** (misma clase de bug que #295 cerró en
  xEdit): el service ahora consulta `tx_lock.rollback_completed` en vez de
  inferir con flags; un restore fallido deja la TX pendiente y reporta
  `rolled_back=False`. Anclado en `tests/test_synthesis_rollback_honesto.py`.

**Pendientes NUEVOS que abre este PR:**

- **Smoke de `dump_record_detail.pas` contra xEdit real.** Igual que el resto
  de los `.pas` bundleados: los tests validan el parser Python del protocolo,
  no que el script compile/corra en SSEEdit (mismo bloqueo que §4.2/§4.3).
  Hasta ese smoke, el advisor trabaja con `dump=None` si el script falla
  (best-effort, degradación declarada en el prompt).
- **Calidad real de las recomendaciones del LLM.** Fase 1 es el experimento:
  probar 2-4 semanas con conflictos reales (Requiem+Ordinator) antes de
  decidir Fase 2 (batch+foros+GUI card) y Fase 3 (auto-parcheo). El stub de
  `forum_search` devuelve `[]` a propósito.
- **Fase 2: campo `recommendations` dedicado en el contrato.** Fase 1 viaja
  en `warnings` (hack deliberado para no romper callers); la GUI card de
  Fase 2 necesita el campo estructurado.

---

## Addendum (2026-07-16) — verificación de auditoría externa (#305) + follow-ups

Una auditoría externa (estilo "Copilot bot") reportó 5 hallazgos sobre los
PRs #300–#304. Se **verificó cada uno contra el código** (no contra el texto de la
auditoría) y se documentó el veredicto en
`docs/audits/auditoria_prs_300-304_verificacion.md`: citas reales,
pero 2 hallazgos con afirmaciones fabricadas, 1 redundante (pedía código que ya
existe), 1 parcial y 1 truncado. Solo 3 puntos eran accionables, y se cerraron en
el mismo PR:

- **H3 — cobertura de rollback parcial (lock real).** Faltaba un test del caso
  "algunos snapshots restaurados, otros no" ejercitando el `SnapshotTransactionLock`
  real (los previos usaban un fake o un restore completo). Anclado en
  `tests/test_distributed_locks.py::test_rollback_parcial_reporta_incompleto_y_lista_el_fallo`
  y `tests/test_synthesis_rollback_honesto.py::test_no_declara_rollback_con_lock_real_si_el_restore_falla`.
- **H1 — `close_game` bajo lock.** `MO2Controller._procs_lock` serializa la región
  snapshot→matar→pop de `close_game` (dos cierres concurrentes ya no re-matan el
  árbol). `launch_game` **no** toma el lock: registra el PID con escritura de dict
  plana para no reabrir la ventana de huérfano de #302.
- **H4 — `MO2Controller` de grass memoizado y AISLADO.** `_build_grass_dependencies`
  ya no crea un controller nuevo por resolución (lo memoiza), pero con una instancia
  **propia del ritual**, no compartida con la del AppContext.

**Lección (mismo patrón recurrente de este doc):** el primer intento de H4
**unificó** el controller de grass con el del AppContext, y el review de Codex sobre
la PR mostró que eso rompía el aislamiento del cleanup — `GrassCacheRunner` llama
`close_game()` entre relanzamientos, y `close_game()` mata **todos** los PIDs
trackeados, así que compartir el tracking habría matado el Skyrim que el usuario
lanzó con la tool normal. Se revirtió a memoización aislada. Verificar "el archivo"
(el provider de grass) no bastaba: había que seguir el árbol de callers hasta
`close_game` para ver el efecto. La "split tracking" que la auditoría creía un bug
era, de hecho, la feature correcta (cleanup ritual-scoped). No abre pendientes
estructurales nuevos.

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

---

## Addendum (2026-07-16, tarde) — Wrye Bash bajo el lock `load-order` + snapshot

**Cerrado** (§2.1 del reporte de consistencia de la auditoría — era el ÚNICO
mutador de plugins fuera de la disciplina lock+snapshot):

- `SupervisorAgent.execute_wrye_bash_pipeline` (GUI/dispatcher) y el tool del
  agente `system_tools.generate_bashed_patch` (LLM/Telegram) corren ahora bajo
  `SnapshotTransactionLock` sobre el MISMO recurso que LOOT
  (`LOAD_ORDER_RESOURCE_ID`): un sort concurrente ya no puede correr mientras
  se construye el Bashed Patch (que lee el load order completo), y viceversa.
- El `Bashed Patch, 0.esp` previo se snapshotea antes de regenerar: un
  timeout/fallo a mitad de escritura restaura el patch anterior en vez de
  dejar el `.esp` corrupto persistente. `rolled_back` se reporta honesto vía
  `rollback_completed` (False en la primera generación — nada que restaurar).
- Nombre canónico `BASHED_PATCH_NAME` extraído a `wrye_bash_runner.py` y
  anclado contra `DelegateToBashedPatch.BASHED_PATCH_NAME` (ADR 0001).
- Anclado en `tests/test_wrye_bash_lock.py` (ambos paths + rollback + ancla).

**Sigue abierto del reporte de consistencia** (candidatos para PRs separados):
VRAMr con lock custom sin heartbeat (TTL 10 min < duración del run), identidad
de PID por `create_time` en `grass_cache_runner` (réplica del patrón #302), y
`rglob("*.cgid")` + `to_json` atómico en `patcher_pipeline`. Este addendum no
cambia el ranking del "Decide" de arriba — Wrye Bash entra como serialización
correcta, no como preflight (T-16c·3) ni caja negra (T-26), que siguen
pendientes para este runner.

---

## Addendum (2026-07-16, noche) — VRAMr bajo SnapshotTransactionLock

**Cerrado** (fix #5 del reporte de consistencia — "reemplazar el lock custom
de VRAMr con SnapshotTransactionLock"):

- `vramr_service.execute_pipeline` dejó de tomar el lock con `acquire_lock`
  crudo (vía el `_lock_scope` custom, ahora eliminado) y usa
  `SnapshotTransactionLock` con `target_files=[]` — el mismo patrón "serializar
  sin snapshotear" de `pandora_service`. VRAMr no muta entrada, así que no hay
  snapshot que restaurar; su rollback propio (`_cleanup_output_dir` de los
  artefactos nuevos) se conserva intacto.
- El punto del fix: un run de VRAMr dura horas (default 1 h) y el lease del
  lock expira a los 10 min (`DEFAULT_LOCK_TTL_SECONDS`). Con `acquire_lock`
  crudo el lease moría a mitad de run y la serialización desaparecía en
  silencio; `SnapshotTransactionLock` trae el heartbeat + auto-renew que
  mantiene el lease vivo. Anclado en `tests/test_vramr_service.py`
  (`test_lease_sobrevive_al_ttl_gracias_al_heartbeat` con TTL 0.3s vs run 0.9s,
  y `test_lock_tomado_con_target_files_vacio`).
- **Matiz de severidad (verificado):** `VRAMrPipelineService` solo se construye
  en tests — no está cableado a producción todavía. El bug era LATENTE (no
  activo): el fix es correcto y future-proof, pero no había un camino de
  usuario expuesto. El reporte lo etiquetó BLOQUEANTE sin verificar el
  cableado; la etiqueta sobre-estimaba la severidad activa.

**Estado del reporte de consistencia tras este PR** (los BLOQUEANTES/mejoras
verificables-por-agente): Wrye Bash ✅ (serialización), VRAMr ✅ (heartbeat).
Quedan dos ítems menores y de bajo riesgo, candidatos para un PR chico:
`patcher_pipeline.to_json` atómico (config de patchers, escritura no atómica —
Media) e identidad de PID por `create_time` en `grass_cache_runner._kill_game_tree_sync`
(IMPORTANTE-baja, mitigada porque `_scan_for_game_sync` ya valida create_time+exe
al ATRIBUIR el juego). El `glob("*.cgid")` no-recursivo NO se toca sin el smoke
real de NGIO (probable no-bug: NGIO escribe planos con worldspace en el nombre).

**Techo alcanzado:** con Wrye Bash + VRAMr cerrados, el pozo de bugs de
concurrencia verificables-por-agente de alto valor está agotado. Lo que queda
de VALOR real (bot de Telegram end-to-end, GUI de instalación/FOMOD, smoke E2E
T-25, smoke de `dump_record_detail.pas` contra xEdit real) requiere un rig
humano con Skyrim/MO2 real — no ejecutable por un agente en CI.

---

## Addendum (2026-07-16, cierre) — patcher_pipeline atómico + identidad de PID en grass

**Cerrados** (los dos ítems menores restantes del reporte de consistencia):

- **`PatcherPipeline.to_json` escritura atómica.** Escribía con `open(w)` +
  `json.dump` directo: un crash a mitad del dump dejaba el JSON de config
  truncado y el próximo `from_json`/`__init__` fallaba con pipeline corrupto.
  Ahora escribe a un tmp único en el mismo dir y hace `os.replace` atómico
  (patrón de `vfs._write_modlist_atomic`); el tmp huérfano se limpia si falla.
  Anclado en `tests/test_patcher_pipeline_atomic.py` (incluye el caso de crash
  a mitad del dump que preserva el config previo byte a byte).
- **Identidad de PID en `grass_cache_runner._kill_game_tree_sync`.** El kill
  directo del juego reparentado (D7b) mataba por PID sin revalidar identidad:
  entre la atribución y el kill el SO puede reusar el PID para otro proceso
  (Steam/Discord/otro Skyrim), y matar su árbol cerraría una sesión ajena.
  Ahora revalida name ∈ `game_exe_names` + exe bajo `game_path` (helper
  `_es_proceso_del_juego`, misma verificación que `_scan_for_game_sync` usa al
  atribuir) antes de matar; fail-closed si el exe es ilegible. Anclado en
  `test_pid_reusado_por_otro_proceso_no_se_mata`. El mundo de procesos falso
  del test se hizo fiel (`Process(pid)` devuelve el proc registrado con su
  name/exe reales) para poder ejercitar el revalidado.

**Reporte de consistencia de la auditoría: CERRADO** salvo el `glob("*.cgid")`
no-recursivo, que se deja a propósito — probable no-bug (NGIO escribe los
`.cgid` planos con el worldspace en el nombre) y no verificable sin el smoke
real de NGIO en un rig con Skyrim. Todo lo demás (Wrye Bash lock, VRAMr
heartbeat, patcher atómico, grass PID, más los del PR #304: parche placebo,
rollback honesto de Synthesis, advisor Fase 1) está cerrado y anclado.

**Frente verificable-por-agente: agotado.** El trabajo de valor restante
(Telegram end-to-end, GUI/FOMOD, smoke E2E T-25, smoke de los `.pas` contra
xEdit real) requiere un rig humano con Skyrim/MO2 — no ejecutable en CI.

---

## Addendum (2026-07-17) — 4 hallazgos de Codex en el review del PR #316

El review automático de Codex sobre el PR #316 (2026-07-17) encontró 4
gaps reales en los fixes de ese mismo PR — verificados uno por uno contra el
código antes de aplicar el fix (protocolo del repo: nunca confiar ciegamente
en un hallazgo externo). Los 4 eran correctos:

- **`grass_cache_runner._kill_game_tree_sync` no comparaba `create_time`.**
  El fix anterior (mismo PR) revalidaba nombre+exe antes de matar, pero un PID
  reusado por OTRO `SkyrimSE.exe` de la MISMA instalación (el usuario relanza
  el juego a mano entre la atribución y el kill) pasaba ese chequeo igual —
  mismo nombre, mismo exe, distinto proceso. Fix: `_scan_for_game_sync` ahora
  captura `create_time` en el momento de la ATRIBUCIÓN (nuevo tipo `_GamePid`
  que viaja pid+create_time por todo `run()`, en vez de un `int` pelado) y
  `_kill_game_tree_sync`/`_es_proceso_del_juego` lo comparan contra el del
  proceso vivo antes de matar. Mismo patrón que `vfs.py` #302. Anclado en
  `test_pid_reusado_por_el_mismo_juego_relanzado_no_se_mata`.
- **`LockLeaseLostError` sin capturar en Wrye Bash y VRAMr.** Ambos usan
  `SnapshotTransactionLock` (nuevo en este PR); su `__aexit__` puede relanzar
  `LockLeaseLostError` en un CLEAN exit si el heartbeat perdió el lease
  durante un run largo — ninguno de los dos tenía un `except` para esa clase
  (Wrye Bash: solo `LockAcquisitionError`/`_WryeBashFailedError`/
  `WryeBashExecutionError`; VRAMr: el `except Exception` estaba DENTRO del
  `async with`, no cubre lo que lanza `__aexit__` al salir). Sin el fix,
  ambos violaban el contrato "siempre devolver dict" y propagaban. Fix:
  `except LockLeaseLostError` explícito en los tres call sites (pipeline del
  supervisor, handler del agente ya lo cubría por su `except Exception`
  genérico — se dejó el comentario aclarando por qué, VRAMr). Se reporta como
  fallo con `rolled_back=False` (la exclusividad no estuvo garantizada, no se
  puede declarar éxito ni intentar limpiar sin arriesgar clobberear a otro
  agente).
- **Bashed Patch corrupto no se limpiaba en la primera generación.** Cuando
  `target_files=[]` (no hay `.esp` previo que snapshotear), un fallo dentro
  del lock no tenía nada que restaurar — el `.esp` truncado que Wrye Bash dejó
  a medio escribir quedaba persistente en `Data/`. Fix: en ambos paths
  (pipeline + handler del agente), si `target_files` estaba vacío y el run
  falló, se borra el artefacto nuevo (best-effort). Mismo patrón que
  `vramr_service._cleanup_output_dir`.

Los 4 fixes están en el mismo PR #316 (no un PR separado — eran defectos
introducidos por ese PR, corregidos antes de merge). Gates verdes, suite
completa passing.

---

## Addendum (2026-07-17) — rebase de PR #316 sobre PR #315 (colisión resuelta)

Mientras el PR #316 (este) esperaba review, se mergeó el **PR #315** —
trabajo independiente de otro agente en la misma rama de la auditoría, que
resuelve el mismo hueco de concurrencia de Wrye Bash (§2.1) con un diseño
superior: extracción Strangler-Fig (`WryeBashPipelineService`, el mismo
patrón que ya tenían LOOT/xEdit/Synthesis/DynDOLOD/Pandora) más un **lock
anidado** (`Bashed Patch, 0.esp` externo + `load-order` interno) que
serializa Wrye Bash tanto contra un sort de LOOT concurrente como contra
otra corrida propia — el fix inline de este PR (un solo lock `load-order`
con snapshot condicional del `.esp`) solo cubría el primer caso.

Al rebasear #316 sobre `main` (ya con #315 mergeado):

- **Se descartó por completo la porción de `supervisor.py`** de este PR
  (`execute_wrye_bash_pipeline` inline + `_WryeBashFailedError`): #315 ya
  la reemplazó con el delegador fino a `WryeBashPipelineService`. Cero
  diff contra `main` en ese archivo tras el rebase.
- **`system_tools.generate_bashed_patch` (el path del agente LLM/Telegram,
  que #315 NO tocaba) se reescribió para delegar al mismo
  `WryeBashPipelineService`** en vez de mantener una segunda
  implementación del lock con supuestos distintos (este PR asumía que el
  `.esp` vive en una ruta conocida — `game_path/Data/<nombre>` — y lo
  snapshoteaba condicionalmente; #315 decidió deliberadamente
  `target_files=[]` siempre porque la salida sale vía la VFS de MO2 con
  `cwd` y su ubicación real es dependiente del entorno). Mantener las dos
  implementaciones habría reintroducido la "asimetría entre gemelos
  arquitecturales" que el reporte de consistencia original señaló como
  causa raíz de proceso (§4.4). El handler ahora espeja el patrón ya usado
  por `run_pandora`/`run_bodyslide_batch`: delega al servicio, mapea su
  dict al contrato JSON de la tool (con `sanitize_for_prompt` aplicado a
  stdout/stderr, que el servicio no hace por ser agnóstico de LLM), y
  preserva el path sin lock manager (legacy/tests) sin cambios.
- `tests/test_wrye_bash_lock.py` se reescribió: los tests que cubrían el
  pipeline inline del supervisor se eliminaron (esa lógica ahora vive en
  `test_wrye_bash_service.py`, del propio #315, que ya la cubre en
  detalle — lock anidado, contención, lease perdida, message canónico).
  Quedan solo los tests del contrato de delegación del handler del agente
  (8 tests: serialización en el lock anidado, bloqueo por el lock propio Y
  por `load-order`, sanitización del stderr, lease perdida, path directo
  sin lock, runner `None`, y el ancla de nombre canónico — extendida para
  incluir `BASHED_PATCH_RESOURCE_ID` de #315).
- Las otras 3 piezas de este PR (VRAMr, `patcher_pipeline` atómico, PID de
  grass) son ortogonales a Wrye Bash — no tuvieron conflicto y salieron
  del rebase sin cambios.

Gates verdes (`ruff check` + `ruff format --check` + `mypy sky_claw/`),
suite completa passing tras el rebase.

## Addendum (2026-07-19) — auditoría de resiliencia del orquestador (#319): F1–F3 cerrados

Auditoría adversarial del orquestador asíncrono
(`docs/audits/2026-07-18_orchestrator_resilience_audit.md`, #319). Cierres:

- **F2** (#320) — cancelación durante `SandboxPromotionFlow._promote`: el
  `promote()` corre bajo `asyncio.shield`; se observa su desenlace terminal
  antes de descartar el clon (sus backups viven en el árbol del clon).
- **F3** (#321) — rollback post-cancelación de
  `SyncEngine.execute_file_operation` blindado con `asyncio.shield` + tracking
  fuerte esperado en `shutdown()`; pruning salteado en ruta de cancelación.
- **Follow-up F2** (#322) — resolución de la TX diferida del `StagingJournal`
  ante cancelación de Synthesis: `desenlace_promocion()` de tres valores
  (`aplicada`/`no_aplicada`/`rollback_fallido`) + `drain()` del dispatcher en
  el shutdown del supervisor.
- **F1** (#328 F1a + este PR F1b) — el `SupervisorStateGraph` (LangGraph) era
  un motor muerto (nada lo ejecutaba; fallaría estructuralmente si corriera).
  Se movió el `AgenticLoopGuardrail` a `LoopGuardrailMiddleware` (global del
  `OrchestrationToolDispatcher`, el camino real) y se **retiró el grafo
  completo** + `LangGraphEventStreamer` + las 5 dependencias
  langgraph/langchain/langsmith de `pyproject.toml`. Decisión en
  **ADR 0006**.

Pendientes de la auditoría (no bloqueantes): F4 (idempotencia sin cablear),
F5 (TOCTOU del drift-gate de `promote()`), F6 (race del timeout de
`HITLGuard`), F7 (prompts HITL concurrentes en `check_for_updates`), F9
(composition root del god object).

## Addendum (2026-07-21) — auditoría de resiliencia del orquestador (#319): cierre F4–F8

Continuación del addendum anterior. Cierres adicionales:

- **F5** (#332) — drift-gate atómico: `_check_drift`/`_compute_diff`/
  `_apply_changes` fusionados en `_promote_sync`, un solo `to_thread` (elimina
  la ventana TOCTOU entre el gate y la mutación).
- **F6** (#331) — resolución atómica de la decisión de `HITLGuard`: cierra la
  race entre el timeout y `respond()` que podía dar un ack de aprobación falso.
- **F4** (#342) — `IdempotencyMiddleware` captura `CancelledError`
  explícitamente (antes quedaba la key bloqueada 1h ante cancelación) y se
  cablea como middleware global del `OrchestrationToolDispatcher` — antes
  existía pero no corría en el camino real de dispatch.
- **F7 + F8** (#343) — `check_for_updates` serializa la fase de aprobación
  HITL con `Semaphore(1)` propio (el fetch/descarga sigue concurrente); el
  poison delivery de `SyncEngine.run` deja de re-lanzar `TimeoutError` cuando
  el producer está siendo cancelado (antes pisaba la `CancelledError` real).

**F1–F8 de la auditoría #319 quedan cerrados.** Solo **F9** (composition root /
god object de `SupervisorAgent`) sigue abierto — cedido al trabajo paralelo de
cold-boot/lifecycle sobre `app_context.py` (#333/#338 y sucesivos); no
verificado end-to-end desde esta auditoría.

## Addendum (2026-07-21) — T-12: `local/fomod/*` migrado a mypy strict

Continuación del sprint de type-coverage (§2.1). Con la auditoría de
resiliencia (#319) cerrada salvo F9 (cedido al trabajo de lifecycle del otro
agente), y el pozo de bugs de concurrencia agotado, el frente limpio de mayor
valor sin colisión pasa a ser la deuda estructural de tipos.

**Cerrado:** `sky_claw.local.fomod.*` sale de la lista `ignore_errors = true`
y entra a `strict = true` (patrón "nace estricto"). Los 4 errores que exponía
`strict` eran genuinos, no ruido — se anotaron con narrowing/anotaciones
reales, sin `Any` ni `# type: ignore` de silencio:

- `parser.py` (×2) — `operator` leído de un atributo XML (`str`) se estrecha a
  `Literal["And", "Or"]` que exige `CompositeDependency.operator`. El patrón
  previo (`if operator not in ("And","Or"): operator = "And"`) no narrowa en
  mypy; se reemplazó por `"Or" if attr == "Or" else "And"`, comportamiento
  idéntico (inválido → "And").
- `parser.py` (×1) — `conditions` de `ConditionalPattern` es no-opcional; un
  `<dependencies>` ausente o que parsea a `None` ahora cae a un
  `CompositeDependency()` vacío en vez de pasar `None`.
- `resolver.py` (×1) — `plugins: list` → `list[Plugin]` en `_resolve_group`
  (el caller pasa `group.plugins`, que ya es `list[Plugin]`).

Gates verdes: `mypy sky_claw/` (240 archivos, 0 issues), `ruff check` +
`ruff format --check`, suite completa (3387 passed, 13 skipped). Sin cambio de
comportamiento — anclado por los 51 tests de `fomod` existentes, que siguen
verdes.

**Candidato cerrado:** `local/loot/*` — **cerrado el 2026-07-28**,
ver el addendum de esa fecha.

**Nota de alcance:** la exención `BLE001` de `local/fomod/**`
(`pyproject.toml`, per-file-ignores) es de T-10/T-11, un frente distinto de
T-12 — no se toca acá (un cambio, un PR). `fomod/parser.py` conserva su
`except Exception` de boundary de parsing.

## Addendum (2026-07-28) — T-12: `local/loot/*` migrado a mypy strict

Cierra el candidato que dejó abierto el addendum del 2026-07-21.
`sky_claw.local.loot.*` sale de la lista `ignore_errors = true` y entra a
`strict = true`. **0 errores expuestos**: los 4 módulos (`cli.py`,
`masterlist.py`, `parser.py`, `version.py`, 589 líneas) ya estaban totalmente
anotados, así que la migración no toca una sola línea de código productivo.

**Por qué el cambio muerde de verdad** (una config que no falla es peor que
ninguna): se verificó rojo→verde con un probe temporal. Una función sin anotar
agregada a `loot/version.py` es **invisible** con el `pyproject.toml` de
`main` (`Success: no issues found in 256 source files`) y con el cambio la
caza `error: Function is missing a type annotation [no-untyped-def]`, un check
que solo existe bajo `strict`. El probe se revirtió; no queda en el árbol.

Detalle de implementación: la entrada se **mueve**, no se duplica. Dejarla en
ambas listas apoyaría el resultado en el orden de resolución de secciones
per-module de mypy para dos patrones de igual especificidad, que no está
garantizado — el mismo motivo por el que `local/fomod/*` se movió en #348.

Gates verdes: `mypy sky_claw/` (256 archivos, 0 issues), `ruff check` +
`ruff format --check`, suite completa con **exit code 0**.

## Addendum — Auditoría 03 (Pipeline): backlog consolidado U-01…U-12

Backlog de la auditoría del dominio de orquestación + herramientas externas
(subprocesos/zombies, integridad VFS, teardown/rollback), fusionado y
verificado línea por línea contra el árbol vivo:
[`docs/audits/auditoria_03_pipeline_consolidada.md`](auditoria_03_pipeline_consolidada.md).
Se enlaza acá para que este inventario canónico siga siendo el punto único de
triage (regla de `AGENTS.md`) — los ítems U-01…U-12 se triagean desde ese
documento y se marcan cerrados acá a medida que se implementen:

- **Alto:** **U-01 cerrado** (#381 el punto 1 — sensor de visibilidad de mods:
  el preflight detecta el run standalone contra el juego base; #388 el punto 2 —
  modelo de SALIDA reconciliado en `output_targets`; 2026-07-28 el punto 3 —
  la invariante documentada para operadores en
  [`operations/deployment_standalone_usvfs.md`](../operations/deployment_standalone_usvfs.md)),
  **U-02 cerrado** (#360 — ver addendum abajo), **U-03 cerrado**
  (#356 — `PrecacheGrass.txt` huérfano se barre en el arranque), U-04 (Wrye
  Bash/Pandora sin rollback de salida, abierto), **U-05 cerrado** (#354 — VRAMr
  usa `kill_and_reap`/tree-kill en vez de `proc.kill()` pelado en timeout).
- **Medio:** **U-06 cerrado PARCIALMENTE** (#375 — solo DynDOLOD; Wrye Bash y
  BodySlide siguen abiertos, ver addendum abajo), **U-07 cerrado** (#355
  — Job Object kill-on-close en DynDOLOD, base de U-02), **U-08 cerrado**
  (#378 la mitad 1 — `clone()`/`_materialize` autolimpian el parcial ante fallo
  sync y cancelación; 2026-07-28 la mitad 2 — reconciliador de arranque de
  backups huérfanos, ver addendum abajo), **U-09 cerrado** (#376 —
  ver addendum abajo), **U-10 cerrado** (#357 —
  `*TimeoutError` dedicadas en Wrye Bash/BodySlide/Pandora).
- **Bajo:** **U-11 cerrado** (#359 — `run_capture` ya no enmascara
  `returncode is None` como éxito 0), **U-12 cerrado** (#358 — `.pas` temporal
  de `run_dynamic_script` se limpia en `finally`).

**Nota de proceso:** los cierres de U-03/U-05/U-07/U-10/U-11/U-12 (#354–#359)
no actualizaron este addendum en su propio PR, violando la regla explícita de
`AGENTS.md` ("actualizar `docs/pending_ooda_status.md` en el mismo PR"). Puesto
al día acá retroactivamente junto con el cierre de U-02, verificado contra
`git log --oneline origin/main` (no contra los títulos de PR únicamente).

### Addendum (U-01) — cierre PARCIAL: el preflight deja de mentir verde en standalone

U-01 arranca de una invariante de deployment confirmada por el mantenedor:
Sky-Claw corre **standalone**, no lanzado desde MO2. Los tools se spawnean
directo (solo el juego pasa por el proxy `ModOrganizer.exe`, `mo2/vfs.py`), así
que **ninguno hereda la USVFS**. Con un MO2 en USVFS estándar los tools leen el
`Data` del juego base y todo el pipeline reporta verde sobre un árbol sin mods.

**El fix que proponía la auditoría no cerraba el ítem — hallazgo de esta
implementación.** El texto pedía "reforzar `build_vfs_sensor` con
`scan_mods_dir=True`". Verificado contra `vfs_health.py`: `VfsHealthChecker`
**solo detecta symlinks/junctions**; `scan_mods_dir` únicamente extiende esa
detección a `mods/*` y **nunca comprueba que los mods estén visibles**. Ningún
otro sensor lo cubría: `MissingMastersChecker` busca a través de TODOS los
`plugin_dirs` (`Data` + `mods/*` + `overwrite`), así que encuentra el plugin
dentro de `mods/SomeMod/` y lo reporta presente — el punto ciego exacto.

**Cerrado en #381:**

1. **Sensor nuevo** `validators/vfs_visibility.py`: compara los plugins que el
   perfil MO2 habilita contra lo que existe en el `Data` que el tool va a leer.
   ROJO solo ante el caso **inequívoco** (N plugins de mod habilitados, CERO
   visibles); una materialización parcial pasa en verde a propósito — un falso
   negativo acotado es preferible a frenar un setup que funciona. El contenido
   de Creation Club (`cc*`) se excluye del universo medido junto con los masters
   base: vive en `Data` con o sin VFS, y contarlo haría que el sensor mintiera
   verde justo en el escenario que ataca.
2. **`scan_mods_dir` derivado** en vez de hardcodeado a `False` en los 5
   servicios que lo fijaban — el flag existe por seguridad (no enumerar una raíz
   sin contraparte validada, review #240), no para apagar el scan cuando la raíz
   SÍ está validada. Se replica el patrón de `loot_service`.

**Ancla de la clase, no del caso:** `tests/test_vfs_visibility_wiring.py`
**enumera** la familia de servicios con preflight y falla si aparece uno nuevo
sin clasificar — en vez de tests escritos a mano que callan sobre el hermano que
falta (defecto #1 del repo). `xedit_service` queda **excluido con motivo
registrado ahí**: su preflight gatea solo `quick_auto_clean`, que limpia los DLC
oficiales presentes en `Data` con o sin VFS, así que un rojo por visibilidad
sería un falso positivo.

**Sigue ABIERTO — la parte (2) del fix original:** reconciliar el modelo de
SALIDA (`overwrite` vs `Data`/`mods`) en `_find_*_output`/`_permission_targets`.
Esta PR cierra la ENTRADA (qué ve el tool); la salida (dónde caen los
artefactos) es lo que **U-04 y las dos mitades abiertas de U-06 realmente
esperan**, y sigue sin resolverse.

### Addendum (U-02) — Job Object kill-on-close centralizado en `run_capture`

U-07 (#355) ya había construido el primitivo (`assign_kill_on_close_job`/
`close_job`, `local/tools/_process.py`) y lo cableó a mano en
`dyndolod_runner.py`. U-02 pedía extenderlo al resto de los spawns directos;
en vez de repetir el cableado runner por runner, se centralizó en
`run_capture` — el helper compartido por xEdit, Wrye Bash, Pandora,
BodySlide, Synthesis y la detección de versión de LOOT (`grep -rn
"run_capture" sky_claw/` confirma los 6 callers) — cubriéndolos todos de una
misma PR, como sugería la auditoría original ("Centralizarlo en `_process`
cubre a xEdit/Wrye Bash/DynDOLOD/TexGen de una"). DynDOLOD/TexGen ya estaban
cubiertos aparte por U-07 (no pasa por `run_capture`: tiene su propio spawn
con drain de pipes). El juego (`vfs.launch_game`, vía `ModOrganizer.exe`) y
el crash-loop de grass quedan **fuera** de este cierre — lanzamiento vía GUI
de MO2, con su propio ciclo de vida; follow-up si se decide extender ahí.

### Addendum (U-06) — cierre PARCIAL: solo DynDOLOD (no declarar U-06 "cerrado")

U-06 lista cuatro sitios de falso verde por exit-code, pero **no son homogéneos**:
verificar el artefacto exige saber DÓNDE cae, y eso depende del modelo VFS que
U-01 todavía no resuelve. Solo DynDOLOD está libre de ese bloqueo, porque
`_find_dyndolod_output()` (`dyndolod_runner.py`) **busca en disco** entre tres
ubicaciones candidatas en vez de asumir una ruta.

**Cerrado en #375 — dos falsos verdes, ambos anclados:**

1. **`output_path is None` salteaba la validación entera** (hallazgo NUEVO, que la
   auditoría original no registró). El guard de `dyndolod_service` estaba
   encadenado — `if result.success and result.dyndolod_result and
   result.dyndolod_result.output_path:` — así que cuando `_find_dyndolod_output`
   no ubicaba la salida y devolvía `None` (solo logueaba un warning), la
   validación **no corría**, el journal se commiteaba y el ritual reportaba
   éxito. Es el caso más grave: exit 0 sin evidencia de que DynDOLOD escribiera
   nada. Ahora eleva `DynDOLODExecutionError` **dentro** del `tx_stack`, así que
   el move-aside de `DirectoryRollback` revierte (patrón de F1/#312).
2. **`validate_dyndolod_output` no exigía `DynDOLOD.esp`.** Su docstring ya
   prometía "Contiene DynDOLOD.esp", pero la ausencia solo logueaba un warning y
   caía igual en `return True`: bastaba cualquier `.esp` suelto. Ahora devuelve
   `False`.

Los tests de servicio existentes mockean `validate_dyndolod_output` en sus 6 call
sites, así que su cuerpo real **nunca se ejercía**; #375 agrega la primera
cobertura directa del runner (incluidas las guardas previas como regresión).

**Sigue ABIERTO de U-06, con motivo verificado:**

- **Wrye Bash y BodySlide — bloqueados por U-01**, igual que U-04. Wrye Bash no
  declara ruta de salida en su comando (`[bash, -b, "Bashed Patch, 0.esp"]` con
  `cwd=game_path`) y BodySlide usa `-o meshes` **relativo**, resuelto contra ese
  mismo `cwd`. Con MO2 en USVFS la salida se redirige a `overwrite`, así que un
  post-check de artefacto marcaría **fallo un run correcto** — un falso negativo
  es peor que el falso verde que intenta cerrar. Requieren U-01 primero.
- **xEdit QuickAutoClean — aplazado por un trade-off distinto, no por U-01.** El
  criterio de mtime que sugiere la auditoría daría falso negativo cuando el
  plugin ya estaba limpio: xEdit sale 0 sin reescribirlo. Necesita una condición
  más matizada (p. ej. existencia del plugin como condición dura y el mtime solo
  informativo) y tocar tests que hoy afirman `success=True` sin crear el plugin
  en disco. Follow-up en su propio PR.

### Addendum (U-08) — CERRADO: `clone()` self-cleaning (#378) + reconciliador de arranque (2026-07-28)

U-08 lista dos mecanismos con la misma raíz (una cancelación/muerte durante
`clone()`/`_materialize` deja un clon parcial en `.skyclaw_sandbox/`): (1)
que `_materialize` no se autolimpie, y (2) la falta de un reconciliador de
arranque que barra lo que (1) no puede prevenir en origen.

**Cerrado en #378 — mitad (1), las dos causas verificadas del clon parcial:**

1. **`ProfileSandbox._materialize` (`profile_sandbox.py`) no tenía
   try/except.** Un `OSError` a mitad de cualquiera de los 4 `copytree`
   (disco lleno, permiso denegado) dejaba el directorio con lo ya copiado
   huérfano para siempre — sin dueño ni referencia. Cero tests cubrían este
   camino. Ahora el cuerpo corre envuelto en `try/except BaseException:
   _rmtree_force(clone.root); raise`.
2. **`run_ritual_in_sandbox` (`sandbox_run.py`) hacía `clone = await
   sandbox.clone()` fuera del `try` que maneja `CancelledError`.** `clone()`
   corre `_materialize` vía `asyncio.to_thread`; cancelar la corrutina que lo
   espera **no interrumpe el hilo** — mismo mecanismo que F2 (auditoría
   2026-07-18), ya resuelto para `promote()` en
   `sky_claw/app/orchestrator/sandbox_promotion.py` (shield + observar
   desenlace real antes de limpiar). Se aplicó el mismo patrón: `clone()` se
   agenda como task, se espera shieldeada, y ante cancelación se observa su
   desenlace real (también shieldeado) en `_finalizar_clone_cancelado` antes
   de decidir si hay que descartar un clon que sí llegó a materializarse.

Ambos anclados con tests que fallan en rojo contra la base (monkeypatch de
`shutil.copytree` para el primero; gate de `asyncio.Event` simulando la
ventana en la que el hilo de `_materialize` sigue vivo tras la cancelación,
para el segundo — mismo patrón que `TestCancelacionDurantePromote` en
`test_sandbox_promotion.py`).

**Cerrado el 2026-07-28 — mitad (2), el reconciliador de arranque.** El wiring
que este addendum dejaba pendiente por coordinación ya está hecho:
`local/tools/rollback_reconciler.py` (módulo nuevo, nace estricto en mypy) se
llama desde `app_context.py` junto al hook de U-03 y con su mismo guard — se
adquiere el lock del ritual que produce el residuo, y si otra instancia lo tiene,
no se toca nada.

**Barrer no es borrar, y esa es la parte de diseño.** U-08 pedía *"complete/revierta
según marcador durable; como piso, GC de backups huérfanos"*. El piso de GC ciego
**no se implementó, a propósito**: un `<dir>.rollback-<nonce>` de DynDOLOD es la
única copia de la generación de LODs anterior (varios GB, horas de cómputo), y un
`rollback-*` dentro de un clon es la única ruta de recuperación manual de un
promote a medio aplicar — el mensaje de `SandboxRollbackError` apunta ahí. Se tomó
la primera opción, leyendo el marcador durable que ya está en disco:

| Familia | Marcador durable | Acción |
|---|---|---|
| `<dir>.rollback-<nonce>` | el target **no** existe | **restaurar** (lo que el `__aexit__` interrumpido habría hecho) |
| `<dir>.rollback-<nonce>` | el target **sí** existe | **preservar + WARNING** (hay dos estados y el FS no dice cuál vale) |
| `.skyclaw_sandbox/<clon>` | contiene un `rollback-*` | **preservar + WARNING** (promote a medio aplicar) |
| `.skyclaw_sandbox/<clon>` | sin `rollback-*`, sin actividad > 24 h | **descartar** (el GC de disco de U-08) |

**Se barre por PRODUCTOR, no por familia única.** La primera versión asumía que el
move-aside tenía *una* raíz (`<mo2>/mods`) y *un* lock (`dyndolod-pipeline`); las dos
son propiedades de DynDOLOD, no del mecanismo. Pandora (U-04, #399) mueve aparte sus
`Pandora_Output`, que cuelgan del juego y del dir de su ejecutable y se serializan con
`behavior-graphs`. Con el modelo viejo eso daba **dos** defectos: ceguera (ninguna de
esas raíces cuelga de `mods/`, así que el backup quedaba huérfano para siempre) y algo
peor que no barrer (mirar el lock de DynDOLOD para decidir si el residuo de Pandora es
huérfano restauraría un backup con la corrida de Pandora todavía en vuelo). Cada
`ProductorDeMoveAside` lleva nombre, lock y raíces juntos, y el guard es por productor.
Sus raíces salen de `output_targets.pandora_rollback_dirs` —la MISMA función que usa el
rollback—, no de una lista escrita en el reconciliador: es el hermano de #388, donde el
sondeo de permisos y la búsqueda de salida divergieron por tener dos fuentes.

La ventana de gracia cubre el hueco que ningún lock tapa: el clon **sobrevive al
ritual a propósito**, esperando la aprobación HITL, y ahí el lock del ritual ya se
liberó. Sin la gracia, arrancar una segunda instancia le borraba el clon a la
primera. El GC además solo toca dirs con la forma que produce `clone()`
(`<perfil>-<12 hex>`): el `.skyclaw_sandbox` puede tener cosas del operador.

**Ancla:** `tests/test_rollback_reconciler.py` enumera los productores de residuo,
los usuarios de `DirectoryRollback` (uno nuevo rompe el test hasta que declare su
lock y sus raíces, que es la pregunta que importa) y
—sobre el **AST**, no por grep— que ambos reconciliadores de arranque estén
*invocados* en `app_context`, que es el modo de falla de #240/#252/#362: verde en
la suite, no-op en producción.

**Fuera de alcance, declarado:** `_promote_sync` sigue corriendo entero en un
`to_thread`; el reconciliador es la red que atrapa su residuo, no un cambio a su
atomicidad.

### Addendum (U-09) — el estado de éxito-parcial del ritual de grass

El audit dejó U-09 abierto como **design-call**: `grass_cache_service` calculaba
`exito` ignorando `teardown_failures`, así que si el clon de perfil o el mod de
config no se podían borrar, la TX se commiteaba como éxito limpio y el audit
trail afirmaba que el FS había quedado consistente. Las consecuencias eran
reales: el propio código ya documentaba que el operador debe limpiar a mano *"o
el próximo run fallará con «el clon ya existe»"* (fail-closed de
`create_clone_profile`).

**Lo que NO se hizo, y por qué.** El audit ya advertía que el fix de una línea
(`exito = ... and not teardown_failures`) está roto, y el código lo confirma: el
cache **sí se generó y se preserva** en `overwrite/Grass`. Degradar la TX a
`ROLLED_BACK` mentiría en la dirección opuesta y dejaría `result["success"]=True`
contra un journal revertido — una inconsistencia nueva.

**La forma que tomó (#376).** `transactions` solo admite
`pending`/`committed`/`rolled_back`, así que el estado mixto se expresa **sin
tocar el schema**: la TX se commitea (el producto existe) y se registra dentro de
ella una **operación fallida** marcada con `TEARDOWN_INCOMPLETE_KIND`, con los
paths que quedaron sin borrar. "TX commiteada + operación de teardown fallida" es
exactamente *producto OK, cleanup pendiente*.

El marcador y su predicado (`is_teardown_incomplete`) viven en
`app/db/journal_contracts.py` — el módulo que ya existía para el contrato
LOOT↔grass justamente para que escritor y lector no se desincronicen con strings
sueltos. El journalizado es **best-effort**, espejo de `_journal_close`: perder la
anotación no puede tumbar un run exitoso.

`exito`, `_componer_resultado` y el contrato del dict de retorno quedaron
intactos — `result["teardown_failures"]` ya exponía el estado al operador; el bug
estaba en el journal.

*Detalle de implementación verificado:* `fail_operation` **no persiste** el
`metadata` que recibe (solo lo usa como condición y escribe `$.error`), así que la
metadata estructurada va en `begin_operation`.

## Addendum (2026-07-27) — auditoría del pipeline de crash logging (#372): residuos y falsos positivos

Auditoría post-merge del PR #372 (`800afd6`, crash logging async-safe) con una
pasada adversarial de refutación encima. **Verificado contra `origin/main`
`67857c4`**; `_logging_runtime.py` y `logging_config.py` no cambiaron desde
`c98409d`, así que todo lo de abajo sigue vigente.

Esta auditoría partió de **7 hallazgos numerados** (F1–F7) y sumó **3
verificaciones adicionales** sobre detalles de implementación puntuales que se
sospechaban falsos (R1–R3) — **10 ítems en total**, todos pasados por una ronda
adversarial de refutación. De esos 10:

- **2 resultaron ser defectos reales**, ambos cerrados en **#383**:
  `handleError` armaba la ventana de reintento del sink ante *cualquier*
  excepción, así que un `%` mal armado en un call site cegaba `crash.log`
  durante un segundo entero y descartaba los records sanos de esa ventana sin
  contarlos (F2); y `setup_logging` resolvía el chat id de Telegram —ocho
  lecturas de keyring— en cada proceso worker VFS, sin necesitarlo (F1, cuya
  severidad bajó de crítica a baja una vez refutada la sospecha de corrupción
  de `config.toml`).
- **2 sobrevivieron en forma reducida** —severidad bajada o reclasificados
  como deuda declarada— y son los residuos #1 y #2 de abajo (F4, F7).
- **6 quedaron refutados** tal como se habían planteado originalmente (F3, F5,
  F6, R1, R2, R3). Refutar tres de esos seis —F5, F6 y R3— sacó a la luz, como
  subproducto, los residuos #3, #4 y #5.

La tabla de "falsos positivos" de abajo lista **9 filas**, no 6, porque F3, F5
y F6 traían dos reclamos distintos cada uno —costo de CPU y comparabilidad del
hash en F3; crecimiento sin cota y `fileno()` en F5; colisión de `job_id` y
llegada a `crash.log` en F6— y cada reclamo se verificó y se refutó por
separado. La novena fila es la parte de F1 que sí se refutó (la corrupción de
`config.toml`), aunque F1 en su conjunto sobrevivió como el defecto de keyring
recién descripto.

**Esta nota existe por dos razones**, y la segunda importa tanto como la primera:
dejar registrados los residuos reales que NO se arreglaron, y **blindar los
falsos positivos** para que la próxima auditoría no gaste esfuerzo —o peor, un
PR— "arreglando" cosas que ya se demostraron sanas.

### Residuos reales, deliberadamente no cerrados

Ninguno tiene víctima hoy; se registran para que la decisión sea explícita y no
se re-derive desde cero.

1. **Wedge de `shutdown` con el sentinel ya entregado.** En
   `_logging_runtime.py:261`, si `enqueue_sentinel()` tiene éxito y
   `thread.join(timeout=remaining)` expira por milisegundos, se devuelve `False`
   y `LoggingRuntime.shutdown` **re-agrega** `queue_handler` al root
   (`:331`) — pero el listener ya consumió el sentinel y murió. Desde ahí hay
   productor sin consumidor. **Matiz que acota el riesgo:** sin records nuevos
   entre medio la segunda llamada converge a `True` (el sentinel ejecuta
   `task_done()` y `unfinished_tasks` vuelve a 0); el wedge permanente exige
   ≥1 record posterior. **Impacto nulo hoy** porque los dos callers de
   producción (`__main__.py`, `vfs_worker.py`) mueren inmediatamente después.
   Se activaría en cuanto exista un caller de vida larga — p. ej. si alguien
   cablea `shutdown_logging` en la cadena `on_shutdown` de NiceGUI. Fix si llega
   ese día: no re-agregar el handler cuando `thread.is_alive()` sea `False`.

2. **`health_snapshot()` no tiene consumidor en producción.** Grep confirma cero
   callers fuera de `tests/`: `degraded`, `dropped_records`, `suppressed_records`
   y `file_errors` no se loguean al cerrar, no salen como gauge Prometheus (el
   registry existe en `app/core/metrics.py`) y no se muestran en la GUI. **No es
   descuido:** el spec de diseño del propio PR lo declara fuera de alcance
   ("Tampoco se implementarán en este cambio métricas nuevas ni un supervisor
   del listener"). Es deuda declarada. El caso realmente ciego es el exe
   *windowed*, donde no hay console handler y el archivo que falla es la única
   salida. Ojo con el matiz: la clase `LoggingHealth` **no** es código muerto
   —`record_drop`/`pop_emergency`/`restore_emergency` son producción-crítica—;
   lo que no tiene consumidor es el accessor de lectura.

3. **`logs/workers/` no tiene purga.** Un archivo por job VFS, para siempre. Cada
   archivo está acotado por rotación (10 MB × 5), así que lo que crece sin cota
   es el *conteo*, no el tamaño; y los jobs VFS son solo `health` y `loot_sort`.
   Higiene pendiente, no fallo operativo.

4. **El fragmento sin `\n` de `LoggerStream` se pierde al apagar.** Nadie llama
   `flush()` (`_logging_runtime.py:357`) sobre los adaptadores instalados antes
   de `shutdown_logging`, y después el root queda solo con `NullHandler`. Pérdida
   cosmética de una línea parcial. Follow-up barato si molesta: flushear los
   `LoggerStream` instalados en el `finally` de `__main__.main`.

5. **Retención de objetos vía `args`/`extra`.** `prepare()` limpia `exc_info`,
   pero `_redact_value` devuelve **por referencia** todo lo que no sea
   `str`/`Mapping`/`tuple`/`list`/`set` (`logging_config.py:187`), así que un
   `logger.error("fallo: %s", exc)` —patrón usado en decenas de call sites ERROR—
   deja la excepción con su `__traceback__` viva en la cola (8192 slots) y en el
   deque de emergencia (256). Acotado y transitorio (el deque se drena en cada
   `enqueue`), y la cola viva retiene el mismo grafo con 32× más capacidad: si
   algún día se quiere acotar de verdad, el target es `_QUEUE_CAPACITY`, no el
   deque.

### Falsos positivos — NO volver a reportarlos ni "arreglarlos"

Cada uno se refutó con evidencia citada o medición; el detalle vive en el PR #383.

| Sospecha | Por qué se cae |
|---|---|
| `encode`+`sha256` del stderr bloquean el event loop | Medido: 64 MiB → 30,7 ms; harían falta ~200 MiB para 100 ms. El mismo `await` ya paga `decode` + `_ANSI_ESCAPE.sub` + `splitlines` pre-existentes (`splitlines` de 9,2 MiB = 10,7 ms > sha256 3,6 ms). #372 **redujo** el CPU del loop ahí: antes el stderr sin cota viajaba como `%s` y el filtro de redacción le aplicaba ~15 `re.sub` completos en el productor. |
| `stderr_sha256` no es comparable con el artefacto crudo | No existe tal artefacto: nada persiste los bytes crudos de stderr. El contrato del test ancla es explícitamente hash del **texto decodificado**. |
| `LoggerStream` crece sin cota con salida `\r` | `flush()` vacía el buffer entero sin mirar separadores, y `StreamHandler.emit` flushea por registro. Además `ui.run(reload=False)` descarta el único `write` sin newline de uvicorn. |
| `fileno()` roto es una regresión accidental | Es contrato explícito y **testeado** (`tests/test_pyinstaller.py`), con cero consumidores de `fileno`/`.buffer` en el repo y en las deps del bundle. |
| Colisión de `job_id` al sanitizar el nombre del log del worker | `job_id` es siempre `uuid4` (`vfs_contracts.py`) ⇒ el `re.sub` de sanitización es un no-op. |
| Los fallos del worker VFS no llegan al `crash.log` del padre | Sí llegan: `_failure()` → `LOOTResult.errors` → `loot_service` loguea ERROR con `exc_info`. Solo el traceback **interno** del worker queda exclusivamente en su log por job. |
| El bucle de truncado de `subprocess_error_extra` es O(n²) | Cota demostrada: **máximo 2 iteraciones** (exceso = 2k con k ≤ 3 bytes de continuación huérfanos tras el corte). |
| Lost-wakeup / deadlock en `stop_with_timeout` | `handle()` corre **fuera** del mutex de la `Queue` (`QueueListener._monitor` llama `task_done()` después), y `queue.mutex` es siempre el lock más interno. El stall del productor es O(1), no `timeout_s`. |
| Corrupción de `config.toml` por workers concurrentes | Triple bloqueo: `_instance_lock` en `submit`, lock de archivo `O_EXCL` por instancia, y única instanciación productiva del broker = one-shot `--mode vfs-health`. |

### Addendum (2026-07-27, noche) — el barrido pendiente corrió: 7 hallazgos nuevos (6 + 1), 2 cerrados

El barrido "¿qué defecto no anticipé?" que la nota de arriba dejaba pendiente
**corrió** (dos intentos previos murieron por cuota de agentes; el tercero
completó: 10 agentes, 0 refutados). Encontró **6 hallazgos** reales que
ninguna de las dos rondas anteriores había tocado — ninguno coincide con los
refutados ni con los residuos R1-R5 ya catalogados arriba.

Además, probar el pipeline con una corrida real (no mockeada) de
`python -m sky_claw --mode cli`/`oneshot` — arranque completo, redacción en
producción (`Auth token [REDACTED]`), `crash.log` comportándose como se
espera — encontró **1 hallazgo más** por su cuenta, fuera del barrido: la
consola no soporta el rango Unicode que el repo usa en español.

**Total: 7 hallazgos nuevos** (6 del barrido + 1 de la corrida real). De esos
7, se cierran 2 en este commit; los otros 5 quedan documentados como residuos
más abajo.

**Cerrados en este mismo commit:**

1. **`SecurityRedactionFilter` nunca redactaba `record.stack_info`.**
   `stack_info` (`logger.warning(..., stack_info=True)`) llega ya renderizado
   como texto por la stdlib, a diferencia de `exc_info` (una tupla que el
   propio filtro renderiza) — sin una rama explícita, viaja sin redactar hasta
   `crash.log`/`sky_claw.log`. **No era hipotético**: NiceGUI (la GUI real de
   Sky-Claw) llama `logging.getLogger("nicegui").warning(..., stack_info=True)`
   en `Client.check_existence()` (bug conocido de NiceGUI, dispara con timers/
   tareas en background que referencian un cliente tras su desconexión), y ese
   logger propaga al root sin `propagate=False`. Cualquier ruta de archivo con
   `Users\<usuario>` en ese stack trace se guardaba sin el masking de username
   que protege a cualquier otro campo del record. Fix: `record.stack_info =
   self._redact(record.stack_info)` junto al bloque de `exc_text`.
   Test ancla: `tests/test_log_redaction_traceback.py`.

2. **La consola pierde el registro completo ante un carácter no codificable
   en su codepage.** `TextIOWrapper.write()` codifica la línea entera antes de
   escribir cualquier byte — si falla (p. ej. `→` en una consola cp1252, común
   en Windows sin `chcp 65001`), no se escribe NADA de esa línea, y la stdlib
   imprime `--- Logging error ---` + traceback en su lugar. Con mensajes en
   español por todo el repo (tildes, `ñ`, flechas), esto puede disparar en
   cualquier consola Windows sin UTF-8. Fix: `stream.reconfigure(errors=
   "backslashreplace")` sobre el stream de consola en `setup_logging` (con
   guarda `getattr`/`callable` para no romper streams de test sin
   `reconfigure`, como `io.StringIO`). Test ancla:
   `test_consola_con_encoding_limitado_no_pierde_el_registro`.

**Documentados como residuos, sin cerrar** (ninguno tiene víctima activa hoy;
razones de alcance, no de urgencia):

3. **`SecurityRedactionFilter.filter()` hace I/O de disco síncrono en el hilo
   productor al renderizar un traceback.** Si `record.exc_info` está seteado y
   `record.exc_text` aún no, el filtro llama `formatException()`, que internamente
   usa `linecache` — y `linecache.checkcache()` hace `os.stat()` real por cada
   nombre de archivo único en el traceback, en **cada** llamada (no solo la
   primera vez), con lectura completa del archivo (`tokenize.open()+readlines()`)
   si aún no está en caché. Contradice literalmente el docstring del propio
   módulo ("los productores solo encolan... el listener concentra I/O fuera del
   event loop"). Medido: ~1-1.3 ms en frío para un traceback de 3 archivos —
   real pero modesto; se agrava con AV activo o discos de red (fricción ya
   documentada en este repo para `FailSafeRotatingFileHandler`).
4. **Ese mismo filtro no tiene límite de tamaño al redactar `extra`/`args`.**
   A diferencia de `subprocess_error_extra` (acotado a 64 KiB explícitamente),
   `_redact_value`/`_redact_container` solo cotan profundidad (`_MAX_DEPTH=64`),
   nunca ancho/tamaño de colección. Medido: una lista de 20.000 strings cortos
   como `extra` tarda ~0,3s sincrónico en el hilo productor. Latente hoy —
   ningún caller del repo pasa una colección grande como `extra` — pero sin
   ningún guard que lo impida si el pipeline crece hacia reportes batch.
5. **`shutdown_logging()` no está protegido contra una segunda
   `KeyboardInterrupt`/SIGTERM durante el apagado.** Solo atrapa
   `except Exception`, y `KeyboardInterrupt` hereda de `BaseException`. Un
   doble Ctrl+C (o un supervisor reenviando SIGTERM) mientras
   `_runtime.shutdown()` está bloqueado en una de sus esperas troceadas deja el
   `queue_handler` desenganchado del root pero `_closed=False`/`_runtime` global
   sin resetear, y el `atexit` de la stdlib `logging` termina cerrando streams
   mientras el hilo listener puede seguir vivo — el escritor pierde en silencio
   justo los records que explicarían por qué el apagado se colgó. El mismo
   patrón está sin protección adicional en `vfs_worker.worker_main`. Ventana de
   exposición: requiere una segunda interrupción durante un apagado ya lento.
6. **La rotación de `sky_claw.log`/`crash.log` no progresa —y el archivo crece
   sin límite— mientras coexistan 2+ procesos** con el mismo `log_dir` (doble
   lanzamiento del `.exe`, o GUI+CLI/Telegram en paralelo). No hay ningún
   instance-lock a nivel de proceso completo (el único `instance_lock` real es
   el de `vfs_broker.py`, scoped a un `mo2_root`, no al proceso). El
   `os.rename()` del rollover falla con `WinError 32` (archivo abierto por el
   otro proceso), `doRollover()` lo atrapa y sigue escribiendo sin rotar —
   indefinidamente, mientras ambas instancias sigan vivas. No hay corrupción ni
   pérdida de logs activos, pero el disco crece sin control y nadie se entera
   (el `file_errors` que lo contaría es el residuo #2 de arriba, sin
   consumidor).
7. **El worker VFS pierde TODO su logging —ni archivo ni consola— si falla el
   `mkdir` de su subdirectorio `workers/`.** A diferencia del rol `app` (que
   siempre conserva `stdout` como fallback), `worker_main` pasa
   `console_stream=None` explícito, así que si el `mkdir` falla (ACL, ENOSPC)
   no hay ningún sink: ni archivo ni consola. Agravante confirmado: en el
   binario congelado de producción (la única ruta de lanzamiento permitida
   para el worker), `sys.stderr` ya es `None` sin parchear para esa rama —así
   que no es "quizás MO2 no capture el stderr", es que no hay ningún stderr
   real que capturar. El resultado del job (éxito/fallo) sigue llegando al
   padre vía `VfsJobResult` de todos modos (residuo ya conocido); lo que se
   pierde es solo el traceback interno de diagnóstico, en un caso de borde
   poco frecuente.

### Límite conocido de esta auditoría

Sigue cubriendo **las hipótesis que se formularon y las que el barrido
adversarial encontró en su primera pasada completa**, no necesariamente el
espacio completo: no se relanzó una segunda ronda de barrido tras aplicar los
2 fixes de arriba para buscar hallazgos de tercer orden.

## Addendum (2026-07-28) — U-01 parte (2): el modelo de SALIDA reconciliado (#388)

U-01 se había cerrado a medias en #381: la **ENTRADA** (el sensor
`vfs_visibility.py` que detecta el modlist invisible en standalone). Este PR cierra
la **SALIDA**, el punto (2) del fix original, que era el desbloqueante declarado de
**U-04** y de las dos mitades abiertas de **U-06**.

### La premisa falsa, y por qué no era prosa muerta

*"El destino de salida es dependiente del entorno"* estaba escrita en **nueve**
lugares del árbol —seis en producción, tres en docstrings de tests— y es **falsa en
todos**. En tres de los seis productivos era el **motivo declarado** de que no
hubiera rollback:

| Sitio | Consecuencia que sostenía |
|---|---|
| `wrye_bash_service._emit_action_manifest` | `snapshots=[]` |
| `wrye_bash_service`, comentario del doble lock | `target_files=[]` |
| `system_tools.run_bodyslide_batch` | `target_files=[]` |
| `wrye_bash_service._permission_targets` | sondeaba el `overwrite` |
| `pandora_service._permission_targets` | sondeaba el `overwrite` |
| `synthesis_service` ×2 | cálculo duplicado ("mismo cálculo que…") |

Y el mismo `wrye_bash_service` ya tenía escrita la respuesta correcta ~20 líneas más
arriba: `_bashed_patch_target` computaba `game/"Data"/BASHED_PATCH_NAME` justificando
que *"``bash.py`` corre con ``cwd=game_path``"*. Hermanos en el mismo archivo
contradiciéndose — la forma exacta del episodio #373, y esta vez el hermano
equivocado era el que sostenía la ausencia de rollback.

### El invariante, enunciado como propiedad del mecanismo

Hay exactamente **dos** formas de que la salida termine en el `overwrite`:

- **(a) Redirección USVFS** — el tool escribe en `Data/x` y la VFS lo desvía. Exige
  heredar la VFS, cosa que solo hace un proceso lanzado vía `ModOrganizer.exe`. Todos
  los runners de `local/tools/` se spawnean directo con `run_capture`: **para ellos (a)
  es inalcanzable**. El único camino del repo bajo USVFS es el broker
  (`mo2/brokered_loot.py`), cuya cobertura productiva son `health` + dos entry points
  de LOOT (ver el addendum F8 más arriba, que lista a Wrye Bash / Synthesis / DynDOLOD
  / xEdit como **pendientes de migrar**).
- **(b) Ruta explícita** — se le pasa la ruta en el comando y escribe **físicamente**.
  Funciona standalone; es lo que hace Synthesis apuntando al `overwrite`.

⇒ **El `overwrite` solo se alcanza por (b), nunca por (a).** Quien no lleva la ruta en
su línea de comandos —Wrye Bash (`[bash, -b, "Bashed Patch, 0.esp"]` con
`cwd=game_path`), Pandora (`--auto`), BodySlide (`-o` relativo al `cwd`)— escribe
físicamente y su destino **es resoluble hoy**.

Vive una sola vez en `sky_claw/local/tools/output_targets.py`. Ancla:
`tests/test_output_targets.py` enumera la familia por uso de `WritePermissionsChecker`
y falla ante un servicio nuevo sin clasificar (`loot_service` y `xedit_service` quedan
excluidos **con motivo escrito**, no de memoria).

### La divergencia del `cwd` de DynDOLOD — y el error de la primera pasada

`dyndolod_runner._find_texgen_output`/`_find_dyndolod_output` sondeaban
`pathlib.Path.cwd()` como tercera candidata, mientras su hermano
`dyndolod_service._permission_targets` la excluía afirmando *"no es el cwd del
subproceso de DynDOLOD"*. **La divergencia era real. La primera pasada de este PR la
resolvió para el lado equivocado**: sacó el cwd de la búsqueda, creyéndole al
comentario en vez de mirar el spawn.

Lo atajó el review de CodeRabbit, y la verificación le dio la razón —
`_execute_process` (`dyndolod_runner.py:458-463`):

```python
proc = await asyncio.create_subprocess_exec(
    str(executable), *args,
    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    **kwargs,          # solo creationflags en Windows: NO hay cwd=
)
```

Sin `cwd=`, el hijo **hereda** el del proceso Python. Excluirlo habría hecho que un run
exitoso devolviera `output_path=None` y el servicio abortara por salida no localizable —
un falso negativo peor que el falso positivo que se quería evitar.

**Resolución final:** el `cwd` es raíz legítima y viaja como **parámetro explícito** de
`dyndolod_staging_roots` (visible en los dos llamadores, testeable sin parchear
globals). `_permission_targets` **lo gana**, cerrando un hueco real: antes sondeaba de
menos, y un staging read-only en el cwd mataba el run después de generar sin que el
preflight lo viera. El ancla afirma que las raíces del sondeo de permisos y las de la
búsqueda de salida salen del mismo resolver, así que sacarlo de un solo lado rompe.

**La lección es la del propio ítem, aplicada a quien lo escribió:** escribir el racional
de una exclusión no es verificarla. El primer intento repitió, dentro del PR que denuncia
esa clase de defecto, exactamente esa clase de defecto.

**Cerrado a medias, y con precisión:** el runner ya **fija** el `cwd` en vez de dejarlo
heredar implícitamente — lo captura una vez por corrida y pasa el mismo valor al
subproceso y a la búsqueda de staging, así que un `chdir` a mitad de run no puede
separarlos. Pero lo fija **al cwd del proceso Python**, con lo cual la rama del cwd sigue
siendo verdadera: el staging todavía puede aterrizar ahí y el resolver debe seguir
sondeándola.

**Follow-up abierto:** apuntar el subproceso a un directorio de staging **dedicado**. Eso
sí volvería falsa la rama y permitiría sacarla del resolver, pero cambia dónde DynDOLOD
resuelve rutas relativas y busca su INI, así que no se hizo de arrastre.

### Qué queda abierto

- **U-01 punto (3)**: **cerrado el 2026-07-28** —
  [`docs/operations/deployment_standalone_usvfs.md`](../operations/deployment_standalone_usvfs.md),
  enlazada desde el portal y desde el índice de operaciones. Con esto U-01 queda
  cerrado en sus tres puntos.
- **U-04**: **cerrado para Wrye Bash** (#397) y **para Pandora en su modo
  `Pandora_Output`** (#399, ver los addenda al final). **Sigue ABIERTO** en dos frentes
  nombrados, ninguno por falta de alcance: (a) **BodySlide**
  (`system_tools.run_bodyslide_batch`), cuyo destino es `game_path/<output_path>` con el
  `output_path` elegido por el LLM, así que un move-aside apuntaría a un directorio que
  no es constante del código; y (b) **el modo `Data` de Pandora**, donde revertir exigiría
  enumerar qué subárboles del `Data` son suyos — dato que el repo no tiene. Sus dos
  prerequisitos sí están cerrados: el path ya no es ambiguo, y **U-10 también**
  (`WryeBashTimeoutError` /
  `BodySlideTimeoutError` ya se elevan, así que la excepción propaga por el context
  manager).
- **U-06** (Wrye Bash / BodySlide): desbloqueado y no implementado. El falso negativo
  que motivaba el diferimiento exigía una redirección USVFS que no puede ocurrir.
  QuickAutoClean sigue aplazado por su motivo propio (mtime), ajeno a U-01.

---

## Addendum (2026-07-28) — U-04 cerrado para Wrye Bash: rollback real de salida (#397)

U-04 pasa de "desbloqueado" a **cerrado solo para Wrye Bash**. Pandora queda abierto
deliberadamente (ver más abajo).

### El diseño: puentear el fallo a excepción, no un mecanismo nuevo

Verificado contra el código antes de escribir una línea: los dos mecanismos de
rollback del repo (`SnapshotTransactionLock`, `DirectoryRollback`) **solo restauran
en la rama de excepción** — `SnapshotTransactionLock.__aexit__` calcula
`should_rollback = force_rollback or (exc_type is not None and ...)`, y
`force_rollback` solo se fija en el constructor, no hay API para pedirlo desde el
cuerpo. Un `WryeBashResult(success=False)` que sale del `async with` sin excepción
—que es lo que pasaba hasta hoy— sale "limpio": ningún rollback dispara aunque
`target_files` esté poblado.

El remedio no inventa un mecanismo nuevo: replica el patrón que `synthesis_service.py`
ya prueba en producción —`if not result.success: raise ...` dentro del lock—. Se
agregó una excepción interna `_RunFallidoError` que carga el `WryeBashResult`
completo, así el `except` dedicado arma el MISMO dict de salida que el camino
no-excepcional (return_code/stdout/stderr/duration_seconds): el contrato de la tool
no cambia, solo se le agrega el rollback que le faltaba.

`target_files` del lock externo pasa a resolver la ruta real
(`runner.config.game_path` vía `output_targets.bashed_patch_target` — la MISMA fuente
que ya usaba el manifiesto, no una tercera vía que pudiera divergir de las otras dos:
el patrón "hermanos" de este repo, aplicado un nivel más abajo).

### Cubre los dos modos de fallo sin código separado

- **Exit non-zero**: el `raise _RunFallidoError(result)` nuevo.
- **Timeout**: `WryeBashTimeoutError` ya elevaba desde U-10 (cerrado antes, verificado
  en #388). Con `target_files` ahora poblado, el `except WryeBashExecutionError`
  **preexistente** empieza a disparar rollback solo — cero líneas nuevas para ese
  camino. Es la prueba de que U-10 era realmente el prerequisito que declaraba ser.

Verificado con archivos reales en disco (no mocks): un Bashed Patch previo con
contenido conocido se restaura byte a byte tras un fallo simulado, en ambos modos
(`test_fallo_non_zero_restaura_el_bashed_patch_previo`,
`test_timeout_tambien_restaura_el_bashed_patch_previo`).

### Limitación aceptada, anclada para que nadie la lea como bug

En el **primer run** (sin Bashed Patch previo), `SnapshotTransactionLock` no toma
snapshot de un archivo que no existe (`__aenter__` chequea `file_path.exists()`), así
que un parcial fallido puede quedar en disco. Es la MISMA limitación que ya tiene
`synthesis_service.py` (`target_esp.exists()` gatea su propio snapshot) — no algo que
este cambio introduce ni que corresponda resolver distinto acá. Ancla explícita:
`test_fallo_sin_patch_previo_no_crashea`.

### Hallazgo de review (Codex #397): el journal mentía cuando el restore fallaba

La primera pasada marcaba la TX del journal como `rolled_back` **incondicionalmente**
en el handler nuevo. Codex vio el problema y verificarlo contra `locks.py` le dio la
razón:

```python
# locks.py:639-641
return self.rollback_attempted and bool(self.snapshots) and not self.rollback_failures
```

`rollback_completed=False` tiene **dos causas distintas** —no había snapshot, o el
restore **falló**— y en la rama de excepción un restore fallido **solo se loguea**
(el propio módulo lo advierte: *"el caller NO puede asumir que hubo rollback"*).
Marcarla `rolled_back` en el segundo caso afirma una recuperación que no ocurrió y
**esconde de la recuperación manual** un Bashed Patch corrupto que sigue en disco.

Lo agravante: **`synthesis_service.py` ya lo resolvía bien** (`if rolled_back or not
target_files: mark... else: logger.critical(...pendiente para recuperación manual)`),
y esta implementación decía estar replicando su patrón. Se replicó el `raise` pero no
la lógica de journal honesto — el defecto #1 del repo cometido dentro del cambio que
decía evitarlo.

**Y el alcance era mayor que el handler señalado.** Antes de U-04, `target_files=[]`
hacía que nunca hubiera snapshots, así que marcar `rolled_back` incondicionalmente era
inofensivo en los **cinco** handlers de `execute_pipeline` que cierran la TX. Poblar
`target_files` los expuso a todos a la vez, no solo al nuevo. Por eso el criterio vive
ahora en un helper único (`_cerrar_tx_tras_rollback`) que consumen los cinco: si se
hubiera arreglado solo el handler que Codex marcó, los otros cuatro quedaban atrás.

Ancla: `test_rollback_fallido_deja_la_tx_pendiente` (parchea `restore_snapshot` para
que falle con `OSError`, verifica que el patch queda corrupto Y que la TX **no** se
cierra). Verificado en rojo revirtiendo el fix.

*Efecto lateral:* el helper expuso tres dobles de `SnapshotTransactionLock` en la suite
que no implementaban la superficie que el servicio consulta. Se completaron los tres
(`test_wrye_bash_service.py` ×2, `test_wrye_bash_lock.py`) en vez de hacer el helper
tolerante con `getattr` — un doble incompleto es un bug del doble, y el acceso directo
es la convención que ya usa `synthesis_service`.

### Por qué Pandora queda afuera, con nombre

La salida de Pandora es un **árbol de directorios** (behavior graphs), no un archivo
único: el mecanismo correcto ahí es `DirectoryRollback` (move-aside de todo el
directorio), no `target_files` de `SnapshotTransactionLock` — son dos primitivas de
rollback distintas. Mezclarlas en un mismo PR sobre la parte más riesgosa del
backlog (mutar datos reales del usuario) duplica la superficie de review de
exactamente lo que más cuidado necesita. Pandora sigue abierto, con su propio PR.

---

## Addendum — U-04 cerrado para Pandora (rollback move-aside de la salida)

Cierra la segunda mitad de U-04. La primera (Wrye Bash) va en su propio PR; se
separaron porque **la causa raíz es común pero la primitiva de rollback no**, y
mezclar dos mecanismos distintos sobre la parte más riesgosa del backlog —revertir
datos reales del usuario— duplica la superficie de review de justo lo que hay que
revisar con más cuidado.

### La parte compartida: el puente fallo→excepción

Los dos mecanismos de rollback del repo restauran **sólo en la rama de excepción**:

```python
# _dir_rollback.py — DirectoryRollback.__aexit__
if exc_type is not None:
    await self._restore_backup()
    self.rollback_completed = True
else:
    await self._discard_backup()
```

Un `PandoraResult(success=False)` —el modo de fallo por exit non-zero— sale *limpio*
del `async with`, así que el backup se descarta y el árbol parcial queda en disco.
Por eso el remedio literal del ítem ("forzar el rollback cuando `result.success is
False`") no es implementable: no hay API para pedirlo desde el cuerpo. Se puentea con
`_RunFallidoError`, que lleva el `PandoraResult` completo para que el `except` arme el
**mismo** dict de salida del camino limpio (`_dict_de_resultado`, fuente única) — el
contrato de la tool no cambia.

El otro modo de fallo, el timeout, sale **gratis**: `PandoraTimeoutError` ya elevaba
desde U-10, así que con el move-aside cableado el `except PandoraExecutionError`
preexistente empieza a restaurar sin líneas nuevas.

### La parte propia: qué es seguro revertir

`DirectoryRollback` **renombra el directorio entero** a un sibling. Enunciado como
propiedad del mecanismo: *eso sólo es correcto sobre un directorio que la herramienta
regenera por completo*. De ahí sale, sin criterio caso por caso, el recorte de las
candidatas:

| Candidata | ¿Revertible? | Por qué |
|---|---|---|
| `Pandora_Output` (cwd y dir del exe) | **sí** | Pandora lo crea y lo regenera entero |
| `game/Data` | **no** | Todo el setup de mods del usuario; Pandora escribe una fracción |
| `exe.parent` | **no** | Contiene el ejecutable que está por correr |

`pandora_rollback_dirs` devuelve el subconjunto **estricto**, y el test lo afirma
contra `pandora_output_candidates` en vez de contra una lista escrita a mano: agregar
una raíz de salida rompe el ancla hasta que se decida si es revertible.

**El guard end-to-end observa DURANTE el run, no después.** Un move-aside indebido va
y vuelve, así que mirar el disco al final lo tapa por completo — verificado simulando
la regresión contra la primera versión del test, que pasaba igual. El único instante
en que el daño es visible es mientras Pandora corre, que además es cuando importa: con
el `Data` renombrado, la herramienta leería un juego sin mods.

### Una raíz de salida que faltaba

`game/Pandora_Output` se suma a las candidatas. `PandoraRunner.run_pandora` pasa
`cwd=str(self.config.game_path)` explícito a `run_capture`, así que una herramienta
que crea su dir de salida relativo al directorio de trabajo aterriza ahí. Es el mismo
hecho verificado **en el spawn** que hizo entrar el `cwd` en `dyndolod_staging_roots`
tras la review de #388; el test lo ancla con `inspect.getsource`, para que sacar ese
`cwd=` del runner rompa el ancla en vez de degradar el rollback en silencio.

Beneficio colateral: el sondeo de permisos del preflight y el rollback salen de la
**misma** resolución de rutas, así que no pueden divergir.

### Ventaja sobre el hermano de Wrye Bash

El move-aside **no** tiene la limitación del primer run que se aceptó allá
(`SnapshotTransactionLock` no snapshotea un archivo inexistente, así que un primer run
fallido deja el parcial). Sin salida previa, "volver al estado anterior" es borrar el
parcial, y eso no necesita backup. `test_primer_run_fallido_borra_el_parcial` lo fija.

### Journal honesto en los cinco caminos

`_cerrar_tx_tras_rollback` cierra la TX **sólo si el rollback se completó de verdad**
(`dr.rollback_completed`); si el restore falló —disco lleno, dir no escribible— la TX
queda PENDING a propósito y se loguea `critical`, porque marcarla `rolled_back`
escondería del recovery manual una salida parcial que sigue en disco. Es el P1 que
Codex encontró en el hermano de Wrye Bash, aplicado acá de entrada y a **todos** los
handlers (`_RunFallidoError`, `PandoraExecutionError`, `CancelledError`, el
`except Exception` final), no sólo al nuevo.

Detalle a favor del move-aside: acá `rollback_completed=False` tiene **una sola** causa
(el restore falló). En `SnapshotTransactionLock` el flag también es False cuando no
había snapshot previo, y hay que desambiguar.

### Las dos superficies

GUI (`SupervisorAgent` → `tool_dispatcher` → `GenerateAnimationsStrategy`) y agente LLM
(`system_tools.run_pandora`) delegan ambas en
`PandoraPipelineService.generate_animations`, así que el rollback llega a las dos por
construcción. El detalle que **sí** podía romperse en silencio es que el path del
agente construye el servicio SIN `path_resolver`: las dirs a revertir se derivan del
`config` del runner. Anclado en
`test_run_pandora_agent_path_tambien_revierte_la_salida` en vez de razonado.

### Cuatro hallazgos del review, y por qué tres cambiaron el mecanismo

Codex y CodeRabbit encontraron cuatro cosas sobre el primer commit. Tres eran
defectos reales y el fix de todas terminó **en `DirectoryRollback`**, no en Pandora
— señal de que el agujero era del patrón, no del caller:

1. **El candidato inactivo se borraba en el camino FELIZ.** Con dos `Pandora_Output`
   candidatos, el move-aside entra en los dos; una instalación real escribe uno solo,
   y `_discard_backup` borraba el backup del otro sin mirar si algo lo había
   reemplazado. Pérdida de datos silenciosa, sin excepción que la delatara. Estaba
   clasificado P2 y es P1. **Mis tests lo tapaban** porque escribían en ambos
   candidatos. Fix: descartar sólo si la herramienta regeneró el dir.
2. **Rollback tras perder la lease.** El move-aside vive anidado dentro del
   `SnapshotTransactionLock` y sale ANTES que él. El lock saltea deliberadamente su
   rollback tras perder la lease (*"never clobber a concurrent owner's mutations"*,
   `locks.py:795-798`), pero el context de abajo restauraba igual: borraba la salida
   del nuevo dueño y devolvía un backup viejo. Fix: veto `should_rollback` que las
   dos capas comparten. **DynDOLOD tenía el agujero idéntico** y se cableó también —
   arreglarlo sólo donde lo reportó el review habría sido el defecto #1 en directo.
   Anclado con un test que **enumera** los callers de `DirectoryRollback` por
   inspección del árbol, no con una lista escrita a mano.
3. **Cancelación durante el move-aside.** El rename corre en `asyncio.to_thread`:
   cancelar la corrutina no interrumpe el hilo, así que el rename terminaba mientras
   el caller nunca registraba el context — dir previo varado bajo `.rollback-*`, sin
   `__aexit__` que lo devolviera, y la herramienta ni siquiera había corrido. Fix:
   shield + observar el desenlace real, el patrón F2 que `sandbox_run` ya usaba.
   **El primer test que escribí para esto pasaba también sin el fix**: afirmaba antes
   de que el hilo completara. Se sincronizó con un `threading.Event`.
4. **`rollback_completed=False` no tenía una sola causa** — y mi propia docstring
   afirmaba que sí, en el párrafo que explicaba por qué el move-aside era superior al
   lock en ese punto. También es False en la salida limpia, así que un run exitoso
   cuyo lock lanzara `LockLeaseLostError` en el teardown quedaba marcado como
   "rollback fallido" y la TX PENDING. Es el mismo error de razonamiento del P1 de
   #397, cometido en el texto que decía que no podía pasar. Fix: `hubo_restore`
   explícito, derivado de si el cuerpo llegó a terminar.

CodeRabbit sumó un quinto, menor pero válido: `pandora_rollback_dirs` clasificaba por
**nombre** (`c.name == "Pandora_Output"`), así que un Pandora instalado en una carpeta
llamada así habría hecho revertible el dir del ejecutable. Ahora selecciona por
identidad de la ruta construida.

### Qué sigue abierto de U-04

El **modo `Data`** de Pandora. `pandora_output_candidates` lo enumera como destino
posible; cuando la versión instalada escribe los `.hkx` directo ahí, un run fallido los
deja en disco. El move-aside es inadmisible sobre `Data` (arrastraría todo el setup de
mods), y el rollback de grano fino que haría falta exige **enumerar qué subárboles de
`Data` son de Pandora** — dato que el repo no tiene hoy. Inventar esa lista sería peor
que la limitación: un rollback que cree cubrir y borre archivos de otros mods. Follow-up
con prerequisito explícito: obtener el manifiesto de salida real de Pandora (su propio
log lo reporta) antes de intentar el remedio.

Con eso, U-04 queda cerrado para Wrye Bash y para el modo `Pandora_Output`.

### Addendum (2026-07-28) — el ancla de familia de U-04, y dos hermanos que faltaban

Los dos PRs de U-04 (#397 Wrye Bash, #399 Pandora) cerraron sus casos con anclas
propias. Faltaba el ancla de la **clase**: `tests/test_rollback_salida.py` enumera
**todo módulo del paquete** que construya un `SnapshotTransactionLock` y exige que
cada uno declare su mecanismo de rollback —`snapshot`, `directorio`, `no-muta` o
`pendiente`— con el motivo **escrito** para los dos últimos. Un mutador nuevo rompe
el guard de completitud hasta que alguien lo clasifique.

El alcance del barrido es la parte que importa: acotarlo a `local/tools/*_service.py`
—el reflejo natural— **no ve** a `run_bodyslide_batch`, que toma el lock desde
`app/agent/tools/system_tools.py`. Un ancla de familia que muestrea el directorio
equivocado reproduce el defecto que existe para atajar. Con el barrido correcto,
**BodySlide queda como el único pendiente de U-04**, y el ancla lo fija: su destino
es `game_path/<output_path>` con el `output_path` elegido por el LLM, así que el
move-aside que resolvió Pandora no aplica (allá el destino es una constante del
código; acá es un parámetro del modelo).

Un tercer test evita que la clasificación sea prosa: quien figura como `directorio`
tiene que construir un `DirectoryRollback` de verdad, y quien lo construye tiene que
estar clasificado así.

**Dos hermanos que #397 no cubrió**, cerrados acá:

1. **`rolled_back` en el resultado**, propagado al path del agente LLM
   (`system_tools.py`). El servicio ya distinguía el fallo revertido del que dejó
   basura, pero solo en el **log** — y el log no viaja en el dict de la tool, así que
   desde el agente los dos se veían idénticos pese a pedir acciones distintas del
   operador. `_cerrar_tx_tras_rollback` devuelve ahora `_DesenlaceDelRollback`
   (booleano + resumen) para que el log y el dict no puedan divergir.
2. **`snapshots` reales en el `ActionManifest`.** `build_action_manifest` traduce cada
   `SnapshotInfo` a un `RollbackStep`: es literalmente el plan de rollback. Mientras
   `target_files` estaba vacío, pasar `[]` era honesto; desde U-04 el lock SÍ captura
   el patch previo, y seguir pasando `[]` hacía que la caja negra declarara "sin plan
   de restore" sobre un ritual que sí lo tiene.

---

## Addendum — el borrado recursivo link-aware es de todo el paquete, no de un archivo

Cierra la generalización que #404 dejó abierta **y la deuda de proceso de ese mismo PR**:
El PR `#404` no actualizó este documento, violando `AGENTS.md:39-42`. Queda asentado acá.

**El disparador es empírico, no una intuición de diseño.** #404 necesitó seis rondas para
seis defectos reales —uno con pérdida de datos del usuario— y **los seis los encontró un
bot; ninguno, un gate**. El código pasó ruff, mypy, 3776 tests y CI en verde cinco veces
seguidas con el borrado de datos ajenos adentro. Es la confirmación medida de lo que
`AGENTS.md:87-91` ya estimaba: *"~94% de los ítems normativos de este corpus no tienen gate
que los haga fallar"*.

### El censo que #404 no hizo

Arreglado un archivo (`_dir_rollback.py`), el barrido del árbol encontró **16 sitios de
borrado recursivo en 10 módulos**, 8 apuntando a árboles del usuario y **ninguno protegido en
niveles anidados**. Y `_rmtree_force` estaba **duplicado** (`vfs.py` / `profile_sandbox.py`,
el segundo declarándolo en su propio docstring), con un agujero extra que ninguno de los dos
veía: su limpieza de read-only usaba un `os.walk` previo que —igual que `rmtree`— desciende
por junctions, así que **chmodeaba el árbol ajeno** antes de borrarlo.

Dos hallazgos que corrigen afirmaciones previas, verificados ejecutando el código:

1. **`rollback_reconciler.py:416` NO era el peor caso**, pese a que la descripción de #404
   nombraba ese patrón como peligroso. Borra clones de sandbox propios y ya filtraba enlaces
   en la raíz. Lo grave estaba en `vfs.delete_mod_files`, `dyndolod_runner._package_as_mod` y
   `grass_profile.teardown`: árboles del usuario con **cero** defensa.
2. **`vfs.delete_mod_files` no era un bug sólo-Windows.** `PathValidator.validate()` gatea
   enlaces con `is_symlink()` —ciego a junctions— y después hace `target.resolve()`
   **incondicional**, así que devuelve el DESTINO resuelto: el borrado recibe el árbol ajeno
   directamente, sin necesidad de atravesar ningún reparse point. Comprobado quitando el
   guard: **con un symlink común en POSIX el mod ajeno se borra**. El camino arranca en una
   tool del agente LLM (`system_tools.uninstall_mod`), con el nombre del mod como parámetro
   del modelo.

### Lo que cierra

`app/security/links.py` gana `rmtree_link_aware()` —el recorrido que nació privado dentro de
`_dir_rollback`, promovido— y los 16 sitios delegan. `shutil.rmtree` **ya no se usa en ningún
lugar del paquete**. Fail-closed nuevo en los tres sitios sin defensa, más `link_kind` en
lugar de `is_symlink()` en `bridge_installer` y `vramr_service`, donde el guard existía con la
API equivocada.

### El gate, que es el punto

`tests/test_borrado_recursivo.py` **enumera por AST** —no por grep: el docstring de `links.py`
menciona `shutil.rmtree` para explicar por qué existe, y un barrido textual se marcaría en rojo
por un comentario, el mismo bug que `test_links.py` ya tuvo— y exige que todo módulo que borre
árboles declare su mecanismo, con motivo escrito para el que no delegue.

Verificado en rojo reintroduciendo el `shutil.rmtree(clon, True)` de `rollback_reconciler`: el
ancla lo atrapa. Es el defecto que sobrevivió a seis rondas de tres revisores automáticos.

### Deuda declarada

`_rmtree_force` sobrevive como wrapper de una línea (`vfs.py`) para la limpieza de read-only;
`grass_profile` y `profile_sandbox` delegan a través de él y el ancla los clasifica
`vía-wrapper`, no `link-aware`, para no afirmar más de lo que hay. El ancla dura
(`shutil.rmtree` no existe) los cubre igual, en el módulo del wrapper.
