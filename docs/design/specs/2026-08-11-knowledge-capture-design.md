# Knowledge Capture — captura local de casos verificables

## Objetivo

Diseñar la captura de **casos de conocimiento** verificables a partir de la operación real de
Sky-Claw, sin acoplar el sistema de observabilidad al futuro dataset y sin que la captura pueda
afectar a un Ritual.

Esta spec desarrolla las decisiones congeladas en
[ADR 0008](../../adr/0008-knowledge-case.md). Donde el ADR decide, acá se detalla.

Alcance de la spec: **diseño previo a implementación**. No define la forma física de los DTOs, ni
la biblioteca, ni el RAG, ni el benchmark, ni el entrenamiento — el ADR los deja abiertos a
propósito.

No se modifica `docs/pending_ooda_status.md`: este trabajo no cierra ninguna T-XX del backlog.

## 1. Problema

Sky-Claw produce, en cada Ritual, exactamente el material que haría falta para construir un corpus
de diagnóstico: un manifiesto de intención previo a mutar, una transacción, la salida de
herramientas externas, validadores deterministas y un informe post-vuelo. Ese material se emite,
se consume para decidir el resultado inmediato, y se pierde.

Lo que falta no es infraestructura. Es un registro que junte las piezas, declare **cuánto vale
cada una** y sobreviva a la sesión.

## 2. Qué existe hoy

Verificado sobre `main` `6d52685`.

### 2.1 Observabilidad

El pipeline de logging (`sky_claw/logging_config.py`, `sky_claw/_logging_runtime.py`) es no
bloqueante y **con pérdida por diseño**: los productores solo encolan con `put_nowait` sobre una
cola acotada, y todo el I/O vive en un hilo listener.

Lo relevante para la captura es que **el logger mide lo que pierde**.
`LoggingHealthSnapshot` (`sky_claw/_logging_runtime.py:23`) es un DTO inmutable con `degraded`,
`dropped_records`, `suppressed_records`, `emergency_records`, `file_errors` y `listener_alive`, y
la atribución de causas está pensada: `handleError` distingue un `OSError` real (disco) de un bug
de formateo, con el motivo escrito —*"atribuirle un bug de formateo mentiría sobre la causa de la
degradación"*—, y los `ERROR+` descartados por saturación se reinyectan desde un buffer de
emergencia acotado.

Cada línea de log lleva `correlation_id`, `trace_id`, `process_role`, pid, `threadName`,
`task_name` y `event` como campos requeridos. `trace_id` es `"0"*32` cuando no hay collector OTel
configurado, que es el caso en un despliegue local.

### 2.2 Journal

`OperationJournal` (`sky_claw/app/db/journal.py`) tiene dos tablas —`transactions` y
`journal_entries`— con WAL, `foreign_keys=ON`, `busy_timeout=5000` y `synchronous=NORMAL`.

Lo importante es el **precedente de extensión**: `persist_action_manifest` y
`persist_flight_report` no crearon tabla. Serializan un modelo con `model_dump(mode="json")` en la
columna `metadata` de una operación normal y se distinguen por una clave `kind` que vive en el
propio JSON, con las constantes centralizadas en `sky_claw/app/db/journal_contracts.py` como única
fuente de verdad.

El mismo módulo documenta por qué el estado no se lee ingenuamente
(`journal_contracts.py:45`): *"Una operación COMPLETED pelada NO alcanza — el ActionManifest
pre-sort también queda COMPLETED aunque el sort falle."*

### 2.3 Ejecución de herramientas

`_process.run_capture` (`sky_claw/local/tools/_process.py`) es sólido: timeout obligatorio,
anti-huérfano en tres capas —`taskkill /T`, `kill_and_reap`, y un Job Object `KILL_ON_JOB_CLOSE`
que cubre incluso una muerte dura de Python— y se **niega a reportar éxito sin `returncode`**.

`subprocess_error_extra` (`sky_claw/logging_config.py:372`) normaliza la evidencia terminal con
integridad de primera: `stderr` acotado a los últimos 64 KB, `stderr_size` de los bytes completos,
`stderr_sha256` **sobre los bytes completos** (el truncado no rompe el hash) y `stderr_truncated`
explícito.

Dos límites, ambos relevantes para el schema del caso:

- Está cableado en **tres** sitios (`local/loot/cli.py`, `local/xedit/runner.py` ×3,
  `local/mo2/vfs_worker.py`), y no en los runners de `local/tools/`.
- Cubre **solo el camino de fallo** (`event="external_process_failed"`). En el camino de éxito no
  hay evidencia estructurada de proceso para ninguna herramienta.

### 2.4 Validadores

Deterministas y ya existentes: `SnapshotManager.restore(verify_checksum=True)`, que lee el
checksum esperado de un sidecar `.meta.json` y lo recalcula sobre el archivo restaurado;
`rollback_reconciler`, con el invariante *"un backup solo se descarta cuando el filesystem prueba
que ya no es la única copia de algo"*; los parsers de xEdit; el resolver FOMOD; la attestation de
perfil del broker VFS; y el gate de frescura de DynDOLOD.

`PostRunValidator` (`sky_claw/local/validators/post_run.py`) es el mecanismo previsto para decidir
efecto, y está cableado en **un** ritual de nueve.

### 2.5 Seguridad y privacidad

`SecurityRedactionFilter` (`sky_claw/logging_config.py:149`) cubre catorce familias de
credenciales, redacción por clave para valores sin forma reconocible, recorrido recursivo con
detección de ciclos y guarda de profundidad, y máscara del path del usuario actual.

Para reutilizarlo hay un obstáculo estructural: **el motor es privado y solo se invoca desde
`filter(record)`**. Un sanitizador de casos que reimplemente la redacción sería un segundo motor
divergente.

Alrededor: `NetworkGateway` (allowlist de dominios, bloqueo de IP privada, validación de cadena de
redirección), `PathValidator` y `safe_join`, `CredentialVault`, `HITLGuard` como mecánica de
consentimiento, y el preview del `FlightReport` como precedente de "mostrarle al operador
exactamente qué va a pasar".

## 3. Anatomía del caso

### 3.1 Campos

Conceptual; los tipos exactos son decisión de la implementación.

```
KnowledgeCase
  schema_version                     versionado desde el primer día
  case_id, created_at

  problem
    symptom                          lo observado
    stage                            etapa del SOP
    tool                             herramienta involucrada
    signature                        clave de deduplicación

  environment                        ver §5.4
  evidence[]                         ver §3.2
  evidence_integrity                 ver §3.4
  evidence_capability                ver §3.5
  experiments[]                      ver §3.3

  verification                       estado del caso = máximo de sus experimentos
  verifiability                      eje ortogonal del ADR §2
  privacy, provenance                ver §6
```

### 3.2 `EvidenceReference`

El caso **no copia** el stderr ni el log. Guarda cómo encontrarlos y cómo saber si cambiaron:

```
kind        journal_entry | log_record | file | tool_result | validator_output
locator     entry_id | (archivo, offset) | path
sha256      valor, o "no se pudo hashear"        ← distinto de hash vacío
size        valor, o "no se pudo medir"
truncated   sí | no | no se sabe
retention   durable | rotating | ephemeral
```

`retention = rotating` es honesto sobre los logs: la rotación borra el backup más viejo en
silencio, así que una referencia a un log rotado es una referencia muerta y el caso debe poder
anticiparlo en vez de descubrirlo al leerla.

Los tres campos con un tercer valor explícito aplican `unknown != absent` al nivel del dato
individual.

### 3.3 `Experiment`

```
experiment_id
hypothesis, hypothesis_source        llm | operator | rule
action_ref                           → el ActionManifest ya persistido
result_ref                           → FlightReport / resultado del tool
verification                         estado + validador + referencia a su salida
outcome                              PASS | FAIL | INCONCLUSIVE
```

Los `FAIL` no se borran nunca. Un caso con tres intentos conserva los tres: la secuencia de lo que
no funcionó es la mitad del valor de diagnóstico.

### 3.4 `evidence_integrity` — copiado, no estimado

```
logging          copia literal de LoggingHealthSnapshot
process          capacidad real por herramienta (§3.5)
journal          estado transaccional, y si provino del barrido de 24 h
snapshot_scope   arranque | momento del caso
complete         derivado
```

`degraded = true` degrada la confianza del caso sin discusión. El recorder **no deriva** ningún
veredicto de integridad: lo copia de quien lo midió.

### 3.5 `evidence_capability` — la asimetría, declarada

| Herramienta | exit code | stderr hasheado | truncamiento declarado | evidencia en éxito |
|---|---|---|---|---|
| xEdit, LOOT, worker VFS | sí | sí | sí | no |
| DynDOLOD/TexGen, Pandora, Synthesis, Wrye Bash, BodySlide, grass cache | sí | no | no | no |

Un caso registra la capacidad **real** con la que se construyó. Mientras la evidencia de proceso
no se emita desde un punto único y en ambos caminos, la mitad de la tabla queda en "no", y el
schema obliga a decirlo en lugar de aparentar uniformidad.

## 4. Ciclo de vida

### 4.1 Cuándo se abre

Cuando una operación relevante produce un síntoma: un Ritual que falla, un validador que reporta
hallazgos, o un operador que declara un problema. **No** se abre un caso por cada operación
exitosa y rutinaria: el corpus se llenaría de ruido sin valor de diagnóstico.

### 4.2 Cuándo se cierra

Cuando termina el Ritual, en cualquier resultado. Al cerrar se copia el snapshot de integridad, se
resuelve el estado de verificación a partir de los experimentos, y se persiste.

### 4.3 Qué pasa si falla el recorder

**El Ritual sigue.** Esta es la regla que ordena todo el módulo.

Hay dos disciplinas opuestas conviviendo en `sky_claw/local/tools/dyndolod_service.py`, y la
elección importa:

- `_emit_action_manifest` es **fail-closed**: si falla el builder o el journal, el Ritual **no
  procede**. Correcto — el manifiesto es la caja negra, y mutar sin ella es inaceptable.
- `_emit_flight_report` es **best-effort**: cualquier fallo se traga con un `except` de boundary y
  se registra.

**La captura sigue el segundo patrón, no el primero.** Un caso perdido es una pérdida de datos; un
Ritual bloqueado por el recorder es una regresión funcional.

Consecuencias operativas dentro del repositorio, que no son opcionales:

- `RUF006` (asyncio-dangling-task) tiene tolerancia cero global: nada de `create_task` sin
  referencia.
- `filterwarnings` trata `ResourceWarning` y `RuntimeWarning` como error: un archivo sin cerrar o
  un loop mal manejado ponen la suite entera en rojo.
- El módulo nuevo debe entrar en la lista `strict = true` de `pyproject.toml` **en el mismo
  cambio** que lo introduce. `sky_claw/app/**` está exento de `BLE001` y buena parte del árbol
  está en `ignore_errors`: sin el opt-in explícito, el código sale a producción sin type-check.

## 5. Integración

### 5.1 Con el journal

El caso viaja como `metadata` con un discriminador `kind` nuevo, declarado en
`journal_contracts.py` junto a `FLIGHT_REPORT_KIND` y `TEARDOWN_INCOMPLETE_KIND`. Sin tabla nueva y
sin migración.

El caso guarda el estado del journal como **evidencia**, nunca como veredicto. En particular no
lee `TransactionStatus.ROLLED_BACK` como prueba de restauración: puede provenir del barrido de 24
horas, que no inspecciona el filesystem.

### 5.2 Con `LoggingHealthSnapshot`

Única dirección: el caso **lee** el snapshot al cerrarse. No hay acoplamiento inverso, y el
sistema de logging no conoce la existencia del caso.

### 5.3 Con los validadores

Los validadores son la **única** vía de promoción a `VERIFIED_LOCAL`. El caso registra qué
validador corrió y dónde está su salida; no interpreta ni reclasifica su veredicto.

Cuando no hay oráculo aplicable, el caso lo declara en `verifiability` (`ORACLE_MISSING` o
`REQUIRES_HUMAN_RIG`) en vez de quedar como pendiente indefinido.

### 5.4 Con el `EnvironmentSnapshot`

`EnvironmentSnapshot` (`sky_claw/local/discovery/environment.py:76`) aporta edición y versión del
juego, rutas y perfiles de MO2 y versión por herramienta.
`_profile_fingerprint` (`sky_claw/local/mo2/vfs_attestation.py:140`) aporta el SHA-256 sobre los
cinco archivos de estado del perfil, que es lo que hace reproducible el conjunto de mods
habilitados y su orden.

**Los dos juntos**: ninguno alcanza solo. El fingerprint no conoce versiones de herramientas; el
snapshot no conoce el orden de carga.

Tres detalles que la implementación debe resolver y que hoy no están dados:

1. `EnvironmentSnapshot` no es inmutable, pese a que el docstring del módulo afirma que todas sus
   dataclasses lo son.
2. Su `to_dict()` serializa las herramientas como `{nombre: ruta}` y **descarta la versión** — el
   dato que el caso necesita.
3. Se produce **en el arranque**. Un caso necesita el entorno del momento del caso, así que hay
   que re-escanear o declarar explícitamente que el snapshot es del arranque.

## 6. Privacidad y procedencia

```
sanitized_by        versión del motor de redacción
patterns_version
redactions          cuántas por tipo — auditable sin revelar el valor
scan_status         clean | redacted | blocked | not_scanned
consent             rag / training / public_dataset, cada uno sí | no | no preguntado
```

`no preguntado` no es `no`. Sin consentimiento explícito nada sale de la máquina, y hoy no existe
ningún camino de salida: el MVP es local por construcción.

**Prerrequisito estructural:** el motor de redacción tiene que extraerse a una función pública
antes de que el caso lo use. Escribir un segundo redactor sería, textualmente, la clase de defecto
que `AGENTS.md` declara dominante.

La procedencia aplica solo a material externo, con el contrato mínimo del ADR §6. `governance.py`
—SQLite con hashing y whitelists persistentes— es el sustrato natural para extenderla.

## 7. Extensibilidad

- **RAG.** El punto de inyección existe (`sky_claw/app/agent/router.py`, rama de consulta de
  modding) y ya pasa por `sanitize_for_prompt` y `PromptArmor`. Un caso puede alimentarlo marcado
  con su estado de verificación.
- **Benchmark.** Un caso `CONTESTED` o con experimentos `FAIL` es material de benchmark de alta
  calidad, porque la respuesta correcta está determinada por evidencia y no por opinión.
- **Training.** Cadena completa: procedencia, privacidad con consentimiento explícito de
  entrenamiento, integridad completa, verificación al menos `VERIFIED_LOCAL`, deduplicación por
  `signature`. Cualquier eslabón que falle excluye.
- **Knowledge Network.** Solo el seam: que `Claim + Evidence + Environment + Result + Verification`
  sobrevivan sin migración destructiva.

## 8. Fuera de alcance

Forma física de los DTOs · implementación del almacenamiento · red comunitaria · backend de RAG ·
vector database · elección de modelo base · compilador de Papyrus · fine-tuning · umbral de
`VERIFIED_MULTI_HOST` · UI y API de consentimiento · a cuántos servicios se cablea la captura.

## 9. Anclas propuestas

`AGENTS.md` es explícito: *"anclala con un test que enumere, no que muestree"*. Las tres anclas,
cada una con el instrumento que el repositorio ya usa:

| Ancla | Qué congela | Patrón de referencia |
|---|---|---|
| AST de lanzadores | El conjunto de módulos que pueden invocar `create_subprocess_exec` | `tests/test_db_connection_invariant.py` |
| Igualdad literal de `kind` | Los discriminadores de `journal_contracts.py` | `RITUAL_TOOL_MAP` en `tests/test_ritual_dispatch.py` |
| AST del redactor | Que exista un solo motor de redacción, y quién lo importa | Las anclas de `load`/`save` crudos en `tests/test_local_config_persistencia.py` |
| Introspección de servicios | La familia de servicios mutantes que deben emitir caso | `tests/test_hitl_client_scoping.py` |

La primera es la que convierte la evidencia de proceso en una propiedad del mecanismo en vez de en
una serie de arreglos por sitio. Sin ella, el próximo lanzador nace sin evidencia y la suite queda
verde.

## 10. Riesgos y límites conocidos

**El defecto que hay que corregir antes de grabar.** `OperationJournal.fail_operation`
(`sky_claw/app/db/journal.py:831`) no persiste su parámetro `metadata`, y solo escribe el mensaje
de error cuando `metadata` es truthy — la llamada natural deja la fila `FAILED` sin mensaje. Es
preexistente e independiente de este diseño, pero **degrada exactamente la evidencia que un caso
de fallo necesita**, y un corpus grabado antes del arreglo no se puede reparar hacia atrás.

**La superficie del lanzamiento está bajo un gate de aceptación.** El SOP de la etapa 9
(`sky_claw/local/AGENTS.md`) declara, tras el PR #462, que no se aprueba ni mergea un cambio al
camino de lanzamiento hasta que una corrida real con un espacio en la ruta confirme que el binario
lee la raíz administrada. **`run_capture` es camino de lanzamiento**, así que emitir evidencia
desde ahí debe demostrar que no altera la serialización del argv, o esperar esa corrida.

**Dos rutas de tools con contratos distintos.** La ruta del agente LLM devuelve una cadena JSON
sin pasar por `normalize_tool_result`; la de Rituales devuelve un diccionario y sí lo usa. Capturar
"el resultado del tool" a nivel de ruta obligaría a manejar dos formas incompatibles. **Enganchar
por debajo de ambas, en la capa de servicios de `local/tools/`, evita el problema por
construcción** — y es la misma capa que ya concentra journal, locks, manifiesto e informe de vuelo.

**Ruido.** Un caso por operación llenaría el corpus de material sin valor. El criterio de apertura
de §4.1 es una decisión de diseño que habrá que revisar con datos reales.
