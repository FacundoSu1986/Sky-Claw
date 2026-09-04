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
  related_cases[]                    enlaces a casos del mismo environment_id — ver §3.6

  verification                       DERIVADO por reductor sobre experiments[] + related_cases[]
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

### 3.4 `evidence_integrity` — medido por DELTA, no copiado

```
logging          delta entre dos LoggingHealthSnapshot: baseline al abrir, final al cerrar
process          capacidad real por herramienta (§3.5)
journal          estado transaccional, y si provino del barrido de 24 h
snapshot_scope   arranque | momento del caso
complete         derivado
```

**Un solo snapshot al cierre no sirve, y el motivo es del mecanismo.** `LoggingHealth`
(`sky_claw/_logging_runtime.py:37-93`) inicializa sus contadores al construirse y **no expone
ningún `reset`**: `_degraded` queda en `True` de forma permanente tras el primer descarte, y
`dropped_records`, `suppressed_records` y `file_errors` acumulan desde el arranque del runtime.
Copiar el snapshot al cerrar marcaría **degradado a todo caso posterior al primer record perdido
de la sesión**, aunque durante ese caso no se haya perdido nada. Sería un falso negativo de
confianza permanente y contagioso.

Por lo tanto el caso captura **dos** snapshots —uno al abrir y otro al cerrar— y persiste
**la diferencia por contador**, más el baseline para que el cálculo sea auditable. `degraded` del
caso es `delta > 0` en cualquier contador, no el flag latcheado del runtime.

Dos límites que el delta **no** resuelve, y que el caso declara en vez de disimular: la ventana
es del proceso entero, no del caso, así que **una pérdida concurrente de otra actividad se
atribuye igual a este caso** (sobreestima, nunca subestima — es el sentido seguro); y
`listener_alive` es un booleano de estado instantáneo, no un contador, así que se registran los
dos valores y no su resta.

`degraded = true` degrada la confianza del caso sin discusión. El recorder **no estima** ningún
veredicto de integridad: lo calcula sobre valores que midió el propio logger.

### 3.5 `evidence_capability` — la asimetría, declarada

| Herramienta | exit code | stderr hasheado | truncamiento declarado | evidencia en éxito |
|---|---|---|---|---|
| xEdit, LOOT, worker VFS | sí | sí | sí | no |
| DynDOLOD/TexGen, Pandora, Synthesis, Wrye Bash, BodySlide, grass cache | sí | no | no | no |

Un caso registra la capacidad **real** con la que se construyó. Mientras la evidencia de proceso
no se emita desde un punto único y en ambos caminos, la mitad de la tabla queda en "no", y el
schema obliga a decirlo en lugar de aparentar uniformidad.

### 3.6 `related_cases[]` y el reductor de estado

El estado de verificación **no** es `max(experiments)`. `CONTESTED` nace de una contradicción
*entre casos*, y cuando aparece el segundo caso contradictorio **al primero no se le agrega
ningún experimento**: un reductor que solo mirara `experiments[]` dejaría al primero en
`VERIFIED_LOCAL` y **elegible para entrenamiento pese a la contradicción**.

```
related_cases[]
  case_id          el otro caso
  relation         contradicts | corroborates | supersedes
  basis            mismo environment_id + misma problem.signature
  detected_at
```

El reductor toma **dos** entradas:

```
si existe algún related_cases[] con relation == "contradicts"  → CONTESTED
si no  → el máximo de los estados de experiments[]
```

`CONTESTED` **gana sobre cualquier otro estado**, incluido `VERIFIED_LOCAL`: una verificación
determinista que otro caso del mismo entorno contradice es exactamente la situación en la que no
se debe entrenar.

Consecuencia operativa: **el enlace es bidireccional y su creación reevalúa a ambos casos.** Un
caso ya persistido puede cambiar de estado por la llegada de otro; el reductor no es una función
del caso aislado. Los dos conservan su evidencia intacta — cambia el veredicto, no el registro.

Detectar la contradicción exige `environment_id` **estable y siempre presente**, que es la razón
por la que §5.4 no lo deriva del digest de attestation.

## 4. Ciclo de vida

### 4.1 Cuándo se abre

Cuando una operación relevante produce un síntoma. Hay **tres** orígenes, y cada uno tiene su
propio disparador:

| Origen | Quién abre |
|---|---|
| Un Ritual que falla | el propio Ritual |
| Un validador que reporta hallazgos | el validador, corra o no dentro de un Ritual |
| Un operador que declara un problema | la UI |

**No** se abre un caso por cada operación exitosa y rutinaria: el corpus se llenaría de ruido sin
valor de diagnóstico.

### 4.2 Cuándo se cierra

**El cierre es por origen, no "cuando termina el Ritual".** Atar el cierre al Ritual dejaría a dos
de los tres orígenes sin persistir nunca: un hallazgo de validador autónomo y una declaración de
operador no tienen Ritual cuya finalización lo dispare.

Todos los productores completan el **mismo lifecycle**
—`abrir` → `baseline de integridad` → `acumular evidencia y experimentos` → `cerrar` →
`persistir`— y lo que cambia es qué evento dispara el cierre:

| Origen | Cierre |
|---|---|
| Ritual | Al terminar el Ritual, en cualquier resultado |
| Validador autónomo | Al terminar la corrida del validador |
| Operador | **Explícito** por el operador, **o** por vencimiento de un TTL que lo cierra como `OBSERVED` con `verifiability` sin resolver |

El TTL existe porque un caso declarado por una persona **no tiene fin natural**, y un caso abierto
para siempre nunca se persiste — es la misma pérdida silenciosa, con otra forma.

El cierre, sea cual sea el disparador, hace siempre lo mismo: toma el snapshot final de
integridad, calcula el delta contra el baseline (§3.4), corre el reductor de estado (§3.6) y
persiste.

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

Única dirección: el caso **lee** el snapshot dos veces —al abrir y al cerrar— y persiste el delta
(§3.4). No hay acoplamiento inverso, y el sistema de logging no conoce la existencia del caso.

### 5.3 Con los validadores

Los validadores son la **única** vía de promoción a `VERIFIED_LOCAL`. El caso registra qué
validador corrió y dónde está su salida; no interpreta ni reclasifica su veredicto.

Cuando no hay oráculo aplicable, el caso lo declara en `verifiability` (`ORACLE_MISSING` o
`REQUIRES_HUMAN_RIG`) en vez de quedar como pendiente indefinido.

### 5.4 Con el `EnvironmentSnapshot`

`EnvironmentSnapshot` (`sky_claw/local/discovery/environment.py:76`) aporta edición y versión del
juego, rutas y perfiles de MO2 y versión por herramienta.

El conjunto de mods habilitados y su orden se derivan de los cinco archivos de estado del perfil
(`modlist.txt`, `plugins.txt`, `loadorder.txt`, `settings.ini`, `settings.txt`), hasheados por la
capa de casos sobre un **manifiesto canónico**.

**No se reutiliza `_profile_fingerprint`** (`sky_claw/local/mo2/vfs_attestation.py:140-166`),
aunque hashee esos mismos cinco archivos. Su firma exige `source_mod`, `relative_path` y
`canary_sha256`, y los mezcla en el digest: el valor queda atado al **archivo canario** que la
attestation eligió. Eso rompe las dos propiedades que el `environment_id` necesita:

- **Estabilidad** — dos casos del mismo entorno con distinto canario producirían identificadores
  distintos, y la detección de `CONTESTED` (§3.6) depende de que coincidan.
- **Totalidad** — `build_attestation_challenge` aborta con
  `VfsAttestationError("el perfil no tiene mods habilitados para construir un canary elegible")`,
  así que un perfil recién creado **no podría obtener identidad alguna**.

De la attestation se toman la lista de archivos y la disciplina de separar dominios dentro del
digest; no la función. Es prior art, no una dependencia.

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

`no preguntado` no es `no`. Sin consentimiento explícito nada sale de la máquina.

**Corrección importante sobre "el MVP es local por construcción": lo es para la persistencia, NO
para el RAG.** No hay camino de subida de casos —`NetworkGateway` no tiene ningún host de
conocimiento en su allowlist— pero **inyectar un caso en el prompt sí es una salida de datos** si
el provider activo es remoto. Ver §7.

**Prerrequisito estructural:** el motor de redacción tiene que extraerse a una función pública
antes de que el caso lo use. Escribir un segundo redactor sería, textualmente, la clase de defecto
que `AGENTS.md` declara dominante.

La procedencia aplica solo a material externo, con el contrato mínimo del ADR §6. `governance.py`
—SQLite con hashing y whitelists persistentes— es el sustrato natural para extenderla.

## 7. Extensibilidad

- **RAG — con dos condiciones previas, no solo con una marca.** El punto de inyección existe
  (`sky_claw/app/agent/router.py`, rama de consulta de modding) y ya pasa por `sanitize_for_prompt`
  y `PromptArmor`. Eso **no alcanza**: ese contexto se envía al provider activo vía `chat()`, y con
  Anthropic, OpenAI o DeepSeek configurados **el provider es remoto**. `sanitize_for_prompt` reduce
  secretos e inyección, pero no consulta ningún permiso ni vuelve local al provider — así que un
  caso con `consent.rag = null` **saldría de la máquina**, contradiciendo la política local-first
  del ADR.

  Por eso inyectar un caso exige, además de marcarlo con su estado de verificación, **dos gates
  evaluados en el momento de la inyección**:

  | Gate | Regla |
  |---|---|
  | Consentimiento | `consent.rag == true`. `null` **no habilita** (§6) |
  | Localidad del destino | Si el provider activo **no** es local, se requiere `consent.rag` otorgado **a sabiendas de que el destino es remoto** |

  Los dos se evalúan contra el provider **efectivo del momento**, no contra el configurado al
  abrir el caso: el router permite intercambiar el provider en caliente, así que un caso elegible
  bajo un provider local puede dejar de serlo en la consulta siguiente. Sin ambos gates, la
  inyección se omite y la consulta procede sin ese contexto.
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
`VERIFIED_MULTI_HOST` · UI y API de consentimiento · a cuántos entrypoints se cablea la captura.

## 9. Anclas propuestas

`AGENTS.md` es explícito: *"anclala con un test que enumere, no que muestree"*. Las cuatro anclas,
cada una con el instrumento que el repositorio ya usa:

| Ancla | Qué congela | Patrón de referencia |
|---|---|---|
| AST de lanzadores | El conjunto de módulos que pueden invocar `create_subprocess_exec` | `tests/test_db_connection_invariant.py` |
| Igualdad literal de `kind` | Los discriminadores de `journal_contracts.py` | `RITUAL_TOOL_MAP` en `tests/test_ritual_dispatch.py` |
| AST del redactor | Que exista un solo motor de redacción, y quién lo importa | Las anclas de `load`/`save` crudos en `tests/test_local_config_persistencia.py` |
| Introspección de **entrypoints mutantes** | La familia de entrypoints que mutan y deben emitir caso — **no "la familia de servicios"** (§10) | `tests/test_hitl_client_scoping.py` |

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

**Dos rutas de tools con contratos distintos, y la capa de servicios NO está debajo de las dos.**
La ruta del agente LLM devuelve una cadena JSON sin pasar por `normalize_tool_result`; la de
Rituales devuelve un diccionario y sí lo usa. Enganchar en la capa de servicios de `local/tools/`
resuelve esa incompatibilidad **para los tools que la atraviesan** — pero **no todos lo hacen**, y
dar por buena la cobertura "por construcción" sería exactamente el defecto que este documento dice
prevenir.

Verificado sobre el árbol actual: en `sky_claw/app/agent/tools/system_tools.py`, `run_loot_sort`,
`generate_bashed_patch` y `run_pandora` construyen su servicio (`LootSortingService`,
`WryeBashPipelineService`, `PandoraPipelineService`). **Dos handlers no**, y por motivos distintos:

| Handler | Cómo evita el servicio | Qué implica para la captura |
|---|---|---|
| `run_xedit_script` (`system_tools.py:172-197`) | `XEditPipelineService` **existe** —lo construyen `supervisor.py:221` y `chain_preview_service.py:115`— pero el handler llama `xedit_runner.run_script()` directo, **sin lock, sin journal y sin rollback** | Un enganche en servicios **no vería** ninguna ejecución de script xEdit iniciada por el agente |
| `run_bodyslide_batch` (`system_tools.py:754-862`) | **No existe `bodyslide_service.py`.** El handler toma su propio lock y `DirectoryRollback` (`:835`) y llama `bodyslide_runner.run_batch()` (`:836`) | Ídem para BodySlide |

**Consecuencia de diseño:** el punto de enganche se define por **entrypoints que mutan**, no por
capa. Donde hay servicio, el servicio; donde no lo hay, el entrypoint. Y el ancla enumerativa
(§9) enumera esa lista, no la de servicios — si enumerara servicios, estos dos nacerían ciegos y
la suite quedaría verde.

**Hallazgo colateral, preexistente y fuera de alcance:** que `run_xedit_script` ejecute scripts de
xEdit sin lock, journal ni rollback mientras la ruta de Rituales usa `XEditPipelineService` con
los tres, sobre el mismo recurso, es la topología "dos superficies, un recurso" que `AGENTS.md`
declara clase de defecto dominante. Excede a la captura de conocimiento y debe evaluarse por su
cuenta; **este trabajo no lo corrige**, solo lo deja registrado.

**Ruido.** Un caso por operación llenaría el corpus de material sin valor. El criterio de apertura
de §4.1 es una decisión de diseño que habrá que revisar con datos reales.

## Addendum — 2026-09-04 (sincronización parcial sobre `main` `10354c2b`, PR #548)

> **Alcance:** actualiza únicamente lo que la spec afirma sobre `run_xedit_script` en
> §10 y §3.5. **No** reescribe el resto: el diseño se verificó sobre `main 6d52685`
> (§2) y ese hallazgo se conserva como estado del baseline de agosto de 2026.

Las afirmaciones de §10 (tabla de entrypoints y "Hallazgo colateral") y de §3.5
describían `run_xedit_script` como una ejecución directa de xEdit "sin lock, sin
journal y sin rollback", segunda superficie mutante sobre el mismo recurso que la
ruta de Rituales. **Era correcto para el baseline de agosto de 2026.**

El **PR #548** (`main 10354c2b`) restringió después esa superficie (verificado
leyendo el código actual): el handler cableado se movió a
`sky_claw/app/agent/tools/xedit_readonly_tool.py` y quedó limitado a una allowlist
de scripts **bundleados read-only** (`XEDIT_AGENT_ALLOWED_SCRIPT_NAMES`), con la
misma política compartida entre el schema (`XEditAnalysisParams`) y el runtime,
staging canónico obligatorio vía `ensure_scripts_staged()` (fail-closed si el runner
no puede probar provenance), nombres desconocidos que fallan cerrados y parsing por
protocolo.

**Consecuencia para esta spec:**

- La caracterización de `run_xedit_script` como **capacidad mutante** del agente LLM
  ya no describe `main`: esa superficie es hoy read-only.
- La tabla de `evidence_capability` (§3.5) para xEdit sigue en pie **en lo que mide**
  —exit code, `stderr` hasheado y truncamiento declarados en el camino de fallo, sin
  evidencia estructurada en éxito— porque esa capacidad la aporta el `XEditRunner`,
  que la ruta read-only sigue usando; lo que cambia es que la ejecución iniciada por
  el agente ya no es un mutador sin lock/journal.
- El punto de diseño sobre "entrypoints mutantes vs. capa de servicios" (§9/§10)
  debe re-derivarse del código actual y **no** inferirse de la topología de agosto.
  La ruta **mutante** de xEdit (Rituales → `XEditPipelineService`) no cambió con #548.

**Nota de drift (preexistente, no corregido acá):** la función `run_xedit_script` de
`sky_claw/app/agent/tools/system_tools.py` que §10 citaba en `:172-197` sigue
existiendo pero **ya no está cableada** (el facade importa la versión read-only).
Queda como código sin consumidor, a evaluar por su cuenta.
