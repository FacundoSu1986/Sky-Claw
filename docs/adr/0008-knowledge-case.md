# ADR 0008 — El KnowledgeCase: evidencia verificable separada de la observabilidad

**Fecha:** 2026-08-11
**Estado:** Aceptada
**Contexto de origen:** auditoría de arquitectura de conocimiento y modelo local (FASES 0–12),
realizada sobre `main` `f8019c9`…`6d52685`.

## Contexto

Se evaluó construir para Sky-Claw una capa de captura de conocimiento —casos verificables
derivados de la operación real— con vistas a un eventual modelo local especializado. La
arquitectura buscada deja el conocimiento documental en RAG, el estado real en tools, y la verdad
ejecutable en validadores deterministas.

La auditoría verificó qué existe realmente en el repositorio, y el hallazgo que ordena este ADR
no fue una carencia sino una repetición:

**Sky-Claw ya razona en términos de confianza de evidencia en siete lugares independientes, y en
ninguno le puso nombre.**

| # | Dónde | Qué dice |
|---|---|---|
| 1 | `sky_claw/local/xedit/flag_rules.py:17` | *"NO alerta: no se afirma una pérdida sin dato"* |
| 2 | `sky_claw/local/xedit/record_dump_parser.py:14` | *"un bloque sin `DUMP_END` se descarta (mejor sin contexto que con contexto a medias)"* |
| 3 | `sky_claw/app/db/journal_contracts.py:45` | *"Una operación COMPLETED pelada NO alcanza — el ActionManifest pre-sort también queda COMPLETED aunque el sort falle"* |
| 4 | `sky_claw/app/db/journal_contracts.py:37` | `TEARDOWN_INCOMPLETE_KIND`: *"marcar la TX entera como revertida mentiría sobre un cache que sí existe"* |
| 5 | `sky_claw/local/mo2/vfs_contracts.py:21` | `ALLOWED_ROLLBACK_STATES`: cinco estados, no dos |
| 6 | `sky_claw/local/tools/dyndolod_runner.py:1319-1328` | Tres resultados distintos (mtime / `_SIN_ARTEFACTO` / `None`), con el motivo: *"Colapsar los dos últimos en un mismo centinela era fail-OPEN"* |
| 7 | `tests/test_estado_ooda.py:21` | `_ESTADOS = {"Cerrado", "Parcial", "Abierto", "Bloqueado (rig humano)"}` |

Los siete expresan la misma propiedad —**dato ausente y dato negativo no son lo mismo**— y ninguno
puede reutilizar el razonamiento del otro, porque no comparten vocabulario ni tipo.

La auditoría también verificó que la infraestructura necesaria **ya existe** en su mayor parte:

- **Integridad de evidencia medida y tipada.** `LoggingHealthSnapshot`
  (`sky_claw/_logging_runtime.py:23`) expone `degraded`, `dropped_records`, `suppressed_records`,
  `emergency_records`, `file_errors` y `listener_alive`.
- **Almacén transaccional durable** con precedente de extensión sin migración:
  `OperationJournal` guarda `ActionManifest` y `FlightReport` como `model_dump(mode="json")` en la
  columna `metadata`, discriminados por una clave `kind` centralizada en `journal_contracts.py`.
- **Identificador de entorno reproducible.** `_profile_fingerprint`
  (`sky_claw/local/mo2/vfs_attestation.py:140`) calcula un SHA-256 sobre los cinco archivos de
  estado del perfil (`vfs_attestation.py:13`).
- **Snapshot de entorno.** `EnvironmentSnapshot`
  (`sky_claw/local/discovery/environment.py:76`) con edición, versión del juego y versión por
  herramienta.
- **Verificadores deterministas.** `SnapshotManager.restore(verify_checksum=True)`,
  `PostRunValidator`, `rollback_reconciler`, los parsers de xEdit, el resolver FOMOD y el gate de
  frescura de DynDOLOD.
- **Evidencia de proceso con integridad.** `subprocess_error_extra`
  (`sky_claw/logging_config.py:372`) emite `stderr_sha256` sobre los bytes **completos**,
  `stderr_size` y `stderr_truncated`.

Y qué falta: el concepto de caso, el vocabulario común, la procedencia, y —fuera del alcance de
este ADR— cualquier soporte de Papyrus.

## Decisión

### 1. Qué es y qué NO es un KnowledgeCase

**ES** un registro estructurado, local y versionado de un problema, su entorno, la evidencia
recolectada, las hipótesis planteadas, los intentos realizados y sus resultados verificados.

**NO ES**, y el ADR lo declara para que no derive hacia ninguna de estas cosas:

- un log;
- un reemplazo del journal;
- un registro de auditoría de seguridad;
- telemetría;
- un dataset de entrenamiento;
- una fuente de verdad por sí mismo;
- algo que pueda bloquear, demorar o alterar un Ritual.

### 2. Vocabulario de confianza

**Cuatro estados producibles, uno reservado, y un eje ortogonal.**

Se descarta un enum plano de cinco estados: `VERIFIED_MULTI_HOST` no lo puede producir ninguna
pieza del sistema hoy (no hay red ni agregación), y un estado inalcanzable induce a llenarlo mal.
Se documenta su nombre y su regla de promoción para que incorporarlo más adelante sea aditivo.

| Estado | Significado | Evidencia mínima | Qué NO permite afirmar |
|---|---|---|---|
| `OBSERVED` | Ocurrió algo y quedó registrado | ≥1 referencia de evidencia resoluble | Que la causa sea la registrada, ni que el efecto haya ocurrido |
| `HYPOTHESIS` | Alguien propone una causa o una reparación | Lo anterior, más el origen de la hipótesis (`llm` / `operator` / `rule`) | **Nada sobre el mundo.** Es una propuesta |
| `VERIFIED_LOCAL` | Un oráculo determinista confirmó el efecto en este entorno | Lo anterior, más identidad del validador, referencia a su salida, e identidad del entorno | Que valga en otro entorno |
| `CONTESTED` | Dos casos con el mismo entorno y resultados incompatibles | ≥2 casos enlazados | Ninguna de las dos versiones |
| `VERIFIED_MULTI_HOST` *(reservado)* | Confirmado en ≥N entornos distintos | — | *No se implementa.* Ver §Alcance |

**Transiciones válidas.** `OBSERVED` → `HYPOTHESIS` | `VERIFIED_LOCAL` | `CONTESTED`;
`HYPOTHESIS` → `VERIFIED_LOCAL` (solo por validador) | `CONTESTED`, o permanece;
`VERIFIED_LOCAL` → `CONTESTED`. Ningún estado baja por un experimento fallido: un `FAIL` se
registra como experimento, no como degradación del caso.

**Elegibilidad.** `OBSERVED`, `HYPOTHESIS` y `CONTESTED` pueden alimentar RAG local (marcados
como tales) y el benchmark —`CONTESTED` es el caso **más** valioso para un benchmark—, pero
**solo `VERIFIED_LOCAL` puede ser candidato a entrenamiento**, y únicamente si supera además
procedencia, privacidad, integridad de evidencia y deduplicación.

**Ante evidencia contradictoria** el caso pasa a `CONTESTED`. Nunca se sobrescribe ni se borra la
versión anterior: la contradicción es el dato.

**Eje ortogonal `verifiability`.** *"Todavía no verificado"* y *"no verificable acá"* son estados
distintos, y colapsarlos es el mismo fail-open que el repositorio ya evitó siete veces:

| Valor | Significa |
|---|---|
| `ORACLE_AVAILABLE` | Existe un validador determinista aplicable |
| `ORACLE_MISSING` | No hay oráculo hoy, podría escribirse |
| `REQUIRES_HUMAN_RIG` | Ninguna automatización puede decidirlo |

`REQUIRES_HUMAN_RIG` adopta deliberadamente la semántica de `"Bloqueado (rig humano)"`
(`tests/test_estado_ooda.py:21`), que el inventario OODA ya usa para lo mismo. Cubre las clases de
defecto que ninguna herramienta local puede juzgar —persistencia en el save, orden de eventos en
runtime, dependencia de versión del runtime—: un caso así se queda en `HYPOTHESIS` **y lo declara**,
en vez de figurar como pendiente indefinido.

**Principios congelados:**

- `unknown != absent`
- `log != ground truth`
- **Ninguna salida de un LLM produce un estado `VERIFIED_*`.**

El tercero no es nuevo: es el invariante que `AIAssistedPatch`
(`sky_claw/local/xedit/ai_assisted_strategy.py:32`) ya respeta registrándose con la prioridad más
baja y devolviendo un plan sin `.esp` de salida ni script.

### 3. Fronteras: logging, journal, validator

**Logging es observabilidad, y tiene pérdida por diseño.** El pipeline descarta bajo presión
(cola acotada), ciega el sink un segundo ante un error de disco, y la rotación borra en silencio.
Eso es correcto —jamás debe bloquear la aplicación— y lo vuelve inadecuado como almacén
autoritativo. **`LoggingHealthSnapshot` se usa exclusivamente como señal de integridad de
evidencia**, nunca como fuente del contenido del caso.

**Journal es evidencia durable de:** intención, operación, transacción, rollback declarado,
`ActionManifest` y `FlightReport`. **No es automáticamente evidencia de efecto real.**

**Validator es el único que puede promover una afirmación a `VERIFIED_LOCAL`**, y solo cuando
exista un oráculo determinista suficiente.

**`attempted` / `completed` / `verified_effect`:**

- **`attempted`** — `journal_entries.status = 'started'` más el `ActionManifest` persistido antes
  de mutar. Es lo que el journal prueba bien.
- **`completed`** — `transactions.status = 'committed'`, leído **vía
  `is_flight_report_committed()`** y nunca por `OperationStatus.COMPLETED` pelado, por el motivo
  que el propio contrato documenta (`journal_contracts.py:45`).
- **`verified_effect`** — únicamente la salida de un validador determinista.

**`TransactionStatus.ROLLED_BACK` no prueba restauración.** Puede provenir de
`sweep_stale_pending` (`sky_claw/app/db/journal.py:513`), que corre en cada `open()` y marca
revertidas las transacciones `pending` de más de 24 h **sin inspeccionar el filesystem**. Un caso
que necesite afirmar que hubo restauración debe consultar al reconciliador
(`sky_claw/local/tools/rollback_reconciler.py`), que sí lee marcadores durables en disco.

### 4. Identidades

| Campo | Decisión |
|---|---|
| `case_id` | Nuevo. Identifica el **problema lógico**, estable a través de los intentos |
| `experiment_id` | Nuevo. Un intento concreto de resolverlo; N por caso |
| `environment_id` | **Derivado, no inventado**: el fingerprint del perfil combinado con un hash del snapshot de entorno |
| `correlation_id` | **No se redefine.** Referencia opcional dentro del caso, nunca su identidad |

`correlation_id` ya tiene significado en producción: es un identificador de **turno
conversacional**, seteado en cuatro superficies de entrada (web, Telegram, GUI-chat, CLI) y
presente en cada línea de log. La ruta de rituales no lo setea, así que dentro de un caso puede
ser la cadena vacía; eso es información, no un defecto a corregir desde acá.

**Los experimentos fallidos son evidencia y no se eliminan.** Un caso con tres intentos
—`EXP-1 FAIL`, `EXP-2 FAIL`, `EXP-3 PASS`— conserva los tres. El estado del caso es el máximo de
los estados de sus experimentos; los `FAIL` son la evidencia de qué no funciona, que es
exactamente lo que un corpus de diagnóstico necesita.

### 5. Entorno reproducible

Se define qué necesita representarse, **clasificado por disponibilidad real**, para no congelar
campos que el código actual no puede obtener:

| Dato | Clase |
|---|---|
| Ruta y edición de Skyrim (SE/AE/LE), versión del juego, store | `AVAILABLE_NOW` |
| Ruta de MO2, perfiles, perfil activo | `AVAILABLE_NOW` |
| Herramientas detectadas con su versión, y herramientas faltantes | `AVAILABLE_NOW` |
| Mods habilitados y orden de carga | `DERIVABLE` (fingerprint del perfil) |
| Momento del snapshot (arranque vs. momento del caso) | `DERIVABLE` — hoy se produce en el arranque, y debe declararse |
| Versión de SKSE, versión de DynDOLOD Resources | `UNKNOWN` — no se verificó que el sistema las detecte |
| Hardware, VRAM | `FUTURE` — solo lo necesita el Model Lab |
| Hash del contenido de cada mod | `FUTURE` — costoso y no necesario para el MVP |

**Ausencia de dato y valor negativo son estados distintos en cada uno de estos campos.**

### 6. Procedencia

Aplica **solo a material externo** (documentos, fixtures de terceros, mods ajenos), nunca a la
evidencia que Sky-Claw produce sobre su propia operación. El contrato conceptual mínimo debe poder
representar: fuente, licencia, versión o commit, autor cuando corresponda, SHA-256,
transformaciones aplicadas, consentimiento y método de verificación.

**Sin procedencia completa no hay elegibilidad para entrenamiento.** Puede haberla para RAG local
si el usuario posee el material legítimamente.

### 7. Privacidad local-first

```
local first
explicit consent
data minimization
sanitize before network
no raw-log upload
no automatic training ingestion
```

Una futura contribución comunitaria debe poder distinguir **tres permisos conceptualmente
separados**: RAG, entrenamiento y dataset público. Un permiso no otorgado (`null`) **no es** un
permiso denegado, y **tampoco es** un permiso concedido.

Hoy el MVP es local por construcción: no existe ningún camino de salida hacia una API de
conocimiento, y `NetworkGateway` no tiene ningún host de ese tipo en su allowlist.

### 8. Seam de Knowledge Network

Se reserva **únicamente** la garantía de que un caso futuro pueda conservar

```
Claim + Evidence + Environment + Result + Verification
```

sin una migración conceptual destructiva. No se decide ni se diseña API, servidor, P2P,
federación, cuentas, reputación ni networking de ningún tipo.

### 9. Frontera del Model Lab

```
Sky-Claw runtime  !=  Model Lab
```

El runtime **no adquiere**, como consecuencia de este proyecto, CUDA, torch, transformers, TRL,
PEFT, datasets de entrenamiento ni Hermes Agent. Hoy `pyproject.toml` no declara ninguna
dependencia de ML, y esa propiedad se mantiene.

Hermes Agent queda admitido **exclusivamente como candidato offline** para generación de
escenarios sintéticos y orquestación de validadores en el lab. No como dependencia del runtime,
no como fuente de verdad, y no como reemplazo del orquestador: duplicaría el tool loop con
guardrail, el routing, la memoria, los reintentos y la máquina de estados que ya existen.

## Alcance

### Lo que este ADR congela

Las nueve decisiones anteriores: qué es y qué no es un caso, el vocabulario de confianza con su
eje de verificabilidad, las fronteras entre logging / journal / validator, la tripleta
`attempted` / `completed` / `verified_effect`, las cuatro identidades, la clasificación del
entorno por disponibilidad, el contrato mínimo de procedencia, los principios de privacidad, el
seam de red y la frontera del Model Lab.

### Lo que este ADR deja explícitamente ABIERTO

Para que nadie lo asuma cerrado:

1. La **forma física del DTO** — biblioteca, nombres de campo y tipos exactos.
2. La **implementación del almacenamiento**. El ADR fija la dirección (dentro del journal, como
   `metadata` con discriminador `kind`, siguiendo el precedente ya probado dos veces); la forma
   concreta la fija la implementación.
3. La **red comunitaria**. Solo seam.
4. El **backend de RAG**.
5. La **vector database**. Ninguna elección: es prematuro, no hay corpus.
6. El **modelo base**. Lo decide un benchmark, no un ADR.
7. El **compilador de Papyrus**. Sin soporte alguno hoy; la alternativa de terceros a la cadena
   del Creation Kit no fue verificada.
8. El **fine-tuning**: método, tamaño de dataset e hiperparámetros.
9. El **umbral N** de `VERIFIED_MULTI_HOST`. Sin red no hay dato para elegirlo.
10. **A cuántos servicios se cablea la captura**. Lo decide la implementación con un ancla
    enumerativa, no este documento.

### Lo que NO se implementa

Este ADR es documentación. No introduce código, ni tabla, ni migración, ni dependencia.

## Consecuencias

### Positivas

- El razonamiento sobre confianza de evidencia deja de estar disperso en siete implementaciones
  sin nombre y pasa a tener un vocabulario único.
- La captura se apoya en infraestructura ya probada en producción, en vez de duplicarla.
- La distinción `REQUIRES_HUMAN_RIG` hace visible, desde el schema, qué afirmaciones **nunca**
  podrán verificarse con la infraestructura actual.
- El MVP funciona offline y sin cuenta, por construcción y no por configuración.

### Negativas y deuda aceptada

- **El caso hereda los límites del journal.** `PRAGMA synchronous=NORMAL` implica que un corte de
  energía puede perder los últimos commits: la ausencia de una fila **no** prueba que la operación
  no ocurrió. Y `sweep_stale_pending` puede declarar `rolled_back` sin haber mirado el disco. Por
  eso el caso guarda el estado del journal como **evidencia**, nunca como **veredicto**.
- **La capacidad de evidencia es desigual por herramienta.** `subprocess_error_extra` está
  cableado en tres sitios (LOOT, xEdit y el worker VFS) y **solo cubre el camino de fallo**. Un
  caso sobre DynDOLOD, Pandora, Synthesis, Wrye Bash, BodySlide o grass cache no puede afirmar
  hash ni truncamiento de la salida del proceso, y un caso de éxito no puede afirmarlo para
  ninguna herramienta. **El caso debe declarar esa limitación en vez de disimularla.**
- **`verified_effect` es hoy casi inalcanzable.** `PostRunValidator` está cableado en un ritual de
  nueve.
- **La calidad de la evidencia de fallo está degradada en origen.** `OperationJournal.fail_operation`
  (`sky_claw/app/db/journal.py:831`) no persiste su parámetro `metadata`, y solo escribe el mensaje
  de error cuando `metadata` es truthy: la llamada más natural —`fail_operation(entry_id,
  error="…")`— deja la fila en `FAILED` sin el mensaje. Es un defecto preexistente e independiente
  de este ADR, pero **condiciona directamente la utilidad de todo caso de fallo**, y corregirlo
  después de empezar a grabar deja un corpus inicial que no puede repararse retroactivamente.
  **Debe resolverse en un cambio atómico previo a la implementación de la captura.**

## Alternativas consideradas y descartadas

**Una tabla propia en SQLite para los casos.** Descartada: duplicaría el ciclo de vida, el lock,
el modo WAL y la recuperación que `OperationJournal` ya resuelve, y crearía un segundo almacén que
mantener sincronizado. El patrón `metadata` + `kind` ya está probado dos veces en el mismo journal.

**Construir la captura sobre el logger.** Descartada: el pipeline de logging tiene pérdida por
diseño. Un registro que exija completitud no puede vivir en un sustrato que documenta su propia
tasa de descarte. Lo que sí se toma del logger es su medición de integridad.

**Redefinir `correlation_id` como identidad de ejecución técnica.** Descartada: ya significa
"turno conversacional" en cuatro superficies y viaja en cada línea de log. Redefinirlo sería
cambiar un contrato vigente para ahorrar un campo nuevo.

**Usar `NoOpJournal` para un modo "captura sin persistencia".** Descartada: sus métodos
`persist_action_manifest` y `persist_flight_report` (`sky_claw/app/db/journal.py:1233`, `:1236`)
tienen firmas incompatibles con las del journal real, que todos los llamadores invocan por
keyword. Inyectarlo produciría `TypeError`. Hoy no tiene consumidores, así que no es un defecto
vivo; queda registrado como **deuda técnica fuera de alcance**, y explícitamente **no se corrige
de forma oportunista** desde este trabajo.

**Un enum plano de cinco estados.** Descartada: `VERIFIED_MULTI_HOST` no es producible con la
infraestructura actual, y un estado inalcanzable en un enum se termina llenando mal. Se reserva el
nombre y la regla de promoción para que agregarlo sea aditivo.

## Verificación

Este ADR no puede verificarse a sí mismo: es una decisión, no un mecanismo. Cuando se implemente,
las reglas que introduce deben quedar ancladas por **tests que enumeren, no que muestreen**,
siguiendo los instrumentos que el repositorio ya usa:

1. **Ancla AST sobre los lanzadores de procesos** — congelar el conjunto de módulos autorizados a
   invocar `create_subprocess_exec`, con el mismo patrón que
   `tests/test_db_connection_invariant.py` aplica a quién abre conexiones SQLite. Es lo que
   convierte la evidencia de proceso en una propiedad del mecanismo en vez de en una serie de
   arreglos por sitio.
2. **Ancla de igualdad literal sobre los discriminadores `kind`** de `journal_contracts.py`, con
   el patrón de `RITUAL_TOOL_MAP` en `tests/test_ritual_dispatch.py`.
3. **Ancla AST sobre el motor de redacción** — congelar que exista uno solo y quién puede
   importarlo, con el patrón que `tests/test_local_config_persistencia.py` aplica a `load`/`save`
   crudos.

**Lo que ninguna de esas anclas verifica**, y por lo tanto no debe darse por cubierto: que la
evidencia capturada sea *suficiente* para diagnosticar, y que los niveles de confianza se estén
asignando con criterio. Eso solo lo dice el uso.
