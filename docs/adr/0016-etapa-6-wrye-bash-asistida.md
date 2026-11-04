# ADR 0016 — Etapa 6 (Wrye Bash) como etapa ASISTIDA de dos fases

**Fecha:** 2026-10-30
**Estado:** Aceptada el 2026-10-30 por el operador en sesión («si dale con carril 1 etapa 6 asistida»),
sobre `origin/main` `70c5c5c`. Adopta la opción *(a) asistida* descartando el retiro del dispatcher.
**Contexto de origen:** fila «Etapa 6 (Wrye Bash) sin build headless | Abierto» de
[`docs/pending_ooda_status.md`](../pending_ooda_status.md) y la deuda que esa misma sección deja
declarada: *«`WryeBashPipelineService.execute_pipeline` **no** cortocircuita: sigue corriendo el
preflight, tomando los locks `bashed-patch` + `load-order` y persistiendo un `ActionManifest` […]
recién ahí el runner eleva `WryeBashHeadlessUnsupportedError` y la TX queda `rolled_back`. […]
Cortocircuitar antes del preflight es trivial; se dejó para la misma decisión que define la etapa 6
(asistida vs. retirarla del dispatcher)»*.
**Relacionados:** [ADR 0002](0002-norte-caja-negra.md) (caja negra de vuelo), [ADR 0005](0005-sandbox-promocion-sincrona-hitl.md)
(promoción síncrona en HITL), [`sky_claw/local/AGENTS.md`](../../sky_claw/local/AGENTS.md) §1 (orden del
DAG) y §2.6 (reglas de Wrye Bash), U-06 (contrato de veredicto), T-27 / F8 USVFS (aislamiento).

## 1. Contexto, verificado contra el árbol

- **No existe build headless.** `Mopy/bash/barg.py` declara `-b/--backup` como *backup de settings*
  (`store_true`, destino en `-f`) y **ningún** flag reconstruye el parche; el parser no declara
  posicionales. La verificación completa y la historia del bug (`bash.py -b "Bashed Patch, 0.esp"`
  moría en `argparse`) están en el docstring de
  [`WryeBashHeadlessUnsupportedError`](../../sky_claw/local/tools/wrye_bash_runner.py).
  El runner **no lanza proceso**: `tests/test_contrato_argumentos_cli.py` lo congela con
  `spawn_mock.assert_not_called()` y exigiéndole no figurar en `_lanzadores_por_modulo()`.
- **El fail-closed actual es honesto pero caro.** `execute_pipeline` corre preflight, valida M-04,
  toma los locks `bashed-patch` + `load-order`, emite el `ActionManifest`, llama al runner —que
  eleva— y cierra la TX como `rolled_back`. Resultado hacia el caller: correcto. Efecto colateral:
  **cada invocación retiene locks y escribe caja negra por un ritual que no puede correr**.
- **La etapa 9 depende del artefacto de la 6.** `etapas_previas_lods` exige `Bashed Patch, 0.esp`
  habilitado en el `plugins.txt` del perfil para habilitar DynDOLOD. Sin etapa 6 el DAG no cierra.
- **Alternativas evaluadas:** *(a)* etapa asistida; *(b)* retirarla del dispatcher;
  *(c)* RPA de coordenadas sobre la GUI — ya descartada en el propio runner (no determinista, no
  corre en CI, no participa del contrato de journal/rollback).

## 2. Decisión

1. **La etapa 6 es asistida.** El operador construye el parche en la GUI de Wrye Bash lanzado
   desde MO2 (así hereda la vista virtual del perfil). Sky-Claw **prepara** y **verifica**.
   Sky-Claw **no spawnea nada** en esta etapa, ni ahora ni como fallback.
2. **Dos fases explícitas, sin lock cruzando la fase humana.** `preparar_etapa_asistida` (fase 1) y
   `verificar_etapa_asistida` (fase 2). Sostener una lease de ownership durante minutos de clic
   humano es exactamente el patrón que produce `lease_lost` y rollbacks sin sujeto.
3. **El veredicto se deriva de evidencia en disco, nunca de un exit code** —no hay subproceso que
   consultar—: artefacto **existente**, **regular** (ni symlink ni junction), **no vacío** y
   **habilitado** en el `plugins.txt` del perfil. Es la misma clase de defecto que
   `tests/test_contrato_veredicto_de_exito.py` enumera para BodySlide/Wrye Bash/Pandora, con la
   variante extrema de que acá no existe código de salida que pueda mentir.
4. **Fail-closed.** `plugins.txt` ilegible, artefacto ilegible o artefacto que es un enlace **no**
   producen verde. "No lo pude leer" nunca es "está" — el mismo criterio que ya usa
   `etapas_previas_lods`, que es el consumidor aguas abajo.
5. **La frescura solo se PRUEBA con testigo, y el testigo distingue ausencia de ignorancia.**
   La fase 1 emite un `TestigoPrevio(existia, sha256)`, no un `sha256 | None`: *"lo miré y no
   estaba"* (`existia=False`) es una prueba, *"no traje testigo"* (`None`) no lo es. Con testigo,
   un artefacto idéntico es `SIN_CAMBIO`; uno que **no existía** prueba frescura por su sola
   aparición — que es el escenario normal de la primera corrida. Un testigo malformado se
   descarta (fail-closed) en vez de adivinar. **No se deduce un cambio por `mtime`**: es la
   comparación que el contrato de LOOT (PR-2) rechazó a favor del estado observable.
6. **El `plugins.txt` se lee como lo lee el consumidor aguas abajo.** Decodificación
   `utf-8-sig` (la de `etapas_previas_lods._plugins_habilitados`) y sintaxis delegada en
   `loot.outcome.parse_load_order_bytes`. Con BOM y sin esa tolerancia, un lector de bytes
   reporta `AUSENTE` sobre un perfil válido mientras el guard de DynDOLOD lo ve habilitado:
   dos consumidores del mismo archivo dando respuestas opuestas es el defecto que el repo ya
   pagó en F1 de DynDOLOD («dos enumeraciones del mismo hecho que no se hablan»). Un test
   parametrizado ata las dos lecturas.
7. **La fase 1 no escribe caja negra.** No muta nada, así que no toma locks, no snapshotea y no abre
   TX. Esto resuelve por sustitución la deuda del «cortocircuito antes del preflight»: el camino que
   podía dejar una TX por ritual imposible deja de ser el camino de producción.
8. **La fase 2 es read-only.** No requiere HITL ni lock: solo lee. La lectura del artefacto
   (huella completa por chunks de 1 MiB) va a un hilo para no bloquear el event loop del daemon.

## 3. Por qué asistida y no retiro del dispatcher

| | Asistida | Retiro |
|---|---|---|
| Superficie de código | 1 módulo nuevo + 2 métodos de servicio | `EXTERNAL_TOOL_REGISTRY` (7→6), `build_ritual_tool_map()`, `tool_dispatcher.py`, tarjetas GUI, **dos censos** (`test_registro_de_herramientas.py`, `test_ritual_dispatch.py`) |
| DAG completo | Cierra: hay artefacto de etapa 6 | **No cierra**: la etapa 9 lo exige; el flujo end-to-end deja de ser alcanzable sin intervención manual fuera del producto |
| Post-check de U-06 | Lo entrega (artefacto + `plugins.txt`) | Lo pierde: sin etapa 6 no hay artefacto que verificar dentro de Sky-Claw |
| Reversible | Sí: es aditivo | Revertir exige reconstruir registry y censos |

El retiro era la opción barata de *código* y la cara de *producto*.

## 4. Contrato

- Módulo puro: [`sky_claw/local/tools/wrye_bash_asistida.py`](../../sky_claw/local/tools/wrye_bash_asistida.py).
  Sin `subprocess` ni `create_subprocess_exec` — anclado por AST en
  `tests/test_wrye_bash_asistida.py::test_el_modulo_asistido_no_spawnea_procesos`. Por eso **no**
  entra a `LANZADORES_ESPERADOS`: si empezara a spawnear, la etapa 6 reabriría la ambigüedad
  headless que este ADR cierra.
- Vocabularios cerrados: `EstadoEnPlugins` (4 estados), `MotivoAsistido` (10 estados). Ambos
  congelados por igualdad literal en el test; un estado nuevo rompe el ancla hasta clasificarse.
- **Precedencia congelada** (`PRECEDENCIA_DE_MOTIVOS`, derivada de la tabla `REGLAS_DEL_VEREDICTO`,
  que es la que ejecuta el veredicto — la constante no puede divergir del código):

  | # | Motivo | Qué mira |
  |---|---|---|
  | 1 | `ARTEFACTO_AUSENTE` | `lstat` → `FileNotFoundError` |
  | 2 | `ARTEFACTO_ENLACE` | `S_ISLNK` **o** `FILE_ATTRIBUTE_REPARSE_POINT` (junction) |
  | 3 | `ARTEFACTO_ILEGIBLE` | `OSError` en `lstat` o en la lectura |
  | 4 | `ARTEFACTO_NO_REGULAR` | existe pero no es `S_ISREG` (p. ej. directorio) |
  | 5 | `ARTEFACTO_VACIO` | `bytes == 0` |
  | 6 | `PLUGINS_ILEGIBLE` | el `plugins.txt` no se pudo leer |
  | 7 | `NO_HABILITADO` | ausente o deshabilitado en `plugins.txt` |
  | 8 | `SIN_CAMBIO` | hay `huella_previa` y el `sha256` coincide |

  El orden importa porque las causas **coexisten**: un artefacto deshabilitado *y* sin cambios es
  `NO_HABILITADO`, no `SIN_CAMBIO`; el operador necesita la causa más accionable.
- Reuso del parser: el `plugins.txt` se lee con
  [`loot.outcome.parse_load_order_bytes`](../../sky_claw/local/loot/outcome.py), cuya semántica está
  verificada contra libloadorder (`*` activa, `#` comenta, `\r\n`/`\n` indistintos), en vez de
  escribir un segundo lector que pudiera divergir del que ya usa el contrato de LOOT.
- **El testigo previo tiene TRES estados, no dos**: *no estaba* (`existia=False`), *estaba con
  esta identidad* (`existia=True, sha256=…`) y *no sé* (`None`). La forma
  `{existia: True, sha256: None}` es imposible y `TestigoPrevio.__post_init__` la rechaza: sin
  identidad de contenido no hay frescura que demostrar. La distinción es la que hace
  demostrable la primera corrida (F5) y la que impide declarar frescura sobre un artefacto que
  nunca se pudo leer.
- **Dónde se busca el artefacto depende de quién ejecuta** (§2, y `tools/output_targets`): la
  corrida spawneada escribe en el `Data` físico (`bashed_patch_target`), el operador dentro de
  MO2 escribe en la superposición del perfil (`bashed_patch_en_overlay`: `overwrite` → mods por
  prioridad → `Data`). Los dos resolvers conviven porque describen a dos ejecutores distintos.
- **El preflight se construye por dominio de datos** (`physical` / `usvfs`): el sensor de
  visibilidad (U-01) mide el `Data` físico y bajo la USVFS del operador sería un rojo falso.

### Qué NO hace

- **No valida el contenido interno del `.esp`** (records, masters, ITMs). No es su trabajo: el
  veredicto de la etapa 6 responde "¿el artefacto de esta corrida existe y está activo?", no
  "¿el parche es correcto?".
- **No prueba que se haya regenerado después del último cambio de load order.** Misma limitación
  declarada que `etapas_previas_lods`: un artefacto viejo pasa el guard. Cerrarlo exige un registro
  de corridas por etapa que el repo no tiene.
- **Persiste sólo el testigo vigente por slot, no un historial de corridas ni el `.esp`.**
  Desde el PR D (§7.2), la fase 1 guarda los 32 bytes de observación previa en
  `SystemPaths.runtime_state_dir() / "etapa6_testigos.json"` y la fase 2 los consume al
  demostrar frescura; no reemplaza un registro histórico de etapas.
- **No detecta enlaces de forma TOCTOU-fuerte**: la clasificación es por `lstat` y la lectura es
  best-effort, igual que `artifact_digest`.

## 5. Consecuencias

- **Positivas:** el DAG cierra con un artefacto verificable; U-06 gana su post-check de Wrye Bash
  (artefacto + `plugins.txt`) como primitiva; el veredicto es auditable sin logs de terceros; no
  cambia el censo de lanzadores ni el contrato de argumentos CLI; los 45 tests del pipeline
  existente (36 en servicio + 9 de lock) siguen intactos.
- **Negativas / deuda:** la etapa 6 deja de ser desatendida por completo (requiere un humano), lo
  cual es una propiedad del binario, no del diseño. Y hay dos superficies más que cablear (§7).
- **Riesgo residual:** un `plugins.txt` que MO2 reserialice durante la fase humana (visto en el
  fingerprint v2, #633) puede cambiar el orden pero no la activación; el veredicto mira activación,
  no orden, así que no lo confunde con un fallo.

## 6. Uso desde el operador

```text
1. preparar  → devuelve instrucciones + huella_previa + estado actual en plugins.txt (success=False,
               pendiente=True: preparar no construye nada, declarar éxito sería un falso verde)
2. (el operador construye el parche en la GUI de Wrye Bash lanzado desde MO2)
3. verificar → veredicto por evidencia; con huella_previa prueba frescura
```

**Quién puede pedir cada fase (post PR B), sin adornos.** Las tres superficies pueden:

| Camino | Fase 1 | Fase 2 | Nota |
| --- | --- | --- | --- |
| Tool del agente LLM (`generate_bashed_patch`) | sí | sí | `fase` es el único parámetro del schema |
| `SupervisorAgent.execute_wrye_bash_pipeline` | sí (default) | sí | lo usa también la API |
| Botón del ritual en el panel | sí | sí | fase 2 = botón «Ya construí el parche — verificar» |

El botón del panel no inventa la fase: la **deriva del resultado** de la fase 1 con el mismo
predicado que usa el toast (`tool_result.es_etapa_pendiente`), y sólo la ofrece si la etapa 1
se **completó** (`pendiente=True`). Tras un `PreflightBlocked`, un `SinRutasResolubles` o un
veredicto (`OK`/`SIN_CAMBIO`) no hay acción: mandar al operador a construir un parche que el
sistema acaba de declarar inviable, o a «verificar» lo ya verificado, sería un botón que sólo
puede fallar.

El `testigo_previo` viaja en el payload **sólo si es la forma canónica válida**, y esa forma
**no se re-implementa en la GUI**: el seam construye un `TestigoPrevio` y traduce su `ValueError`
a `None` (ronda 10, S-10), de modo que validador y dominio no puedan divergir. Se valida antes
de ofrecer el botón — no en el servicio, porque el payload es lo que el operador ve confirmado.
Sin testigo, la fase 2 igual es útil (existencia, tamaño y activación en `plugins.txt`) y el
veredicto declara `frescura_probada=False`: honesto, aunque no sea el contrato completo.

**Cómo sobrevive al cierre del panel (PR D, §7.2).** `ReactiveStore` es un diccionario en RAM:
cerrar la GUI entre las dos fases borra el toast y su payload. `etapa6_testigos` persiste los
32 bytes de observación previa en `SystemPaths.runtime_state_dir() / "etapa6_testigos.json"`,
claveados por el **slot virtual** `(plugins_txt canónico, destino Data canónico)` (invariante
cuando el parche pasa de `Data` ausente en la fase 1 a `overwrite` en la fase 2). Si el
operador reabre la GUI después de construir y vuelve a pulsar la etapa 6,
`decidir_testigo_de_preparacion` detecta que el artefacto apareció o cambió respecto del
testigo durable, **conserva** la observación previa al build (`conservado_por_build`) y la
entrega en el payload del nuevo CTA para que la fase 2 demuestre frescura en vez de fallar con
`SIN_CAMBIO`. Y en llamadas directas a `verificar` sin payload (agente LLM o API), el servicio
lee el testigo durable del almacén y lo consume al probar frescura.

## 7. Secuencia de PRs — y por qué se parte

`AGENTS.md` §«La regla que más se viola» documenta que 9 de 13 follow-ups auditados son un fix que
aterrizó en una superficie y dejó intacta a su gemela. Esta etapa tiene **tres** superficies de
producción: `dispatcher_dependencies.build_wrye_bash_pipeline`, `SupervisorAgent.execute_wrye_bash_pipeline`
y la tool del agente LLM (`system_tools`). Por eso el cableado **no** se hace de a una:

- **PR A (mergeado, #839):** contrato + servicio + tests. **Sin wiring.** No cambió
  comportamiento de producción; 93 tests nuevos (91 passed + 2 skips en Windows) y las 17 anclas
  verdes.

  `pendiente=True` viajó marcado `pendiente_no_ratificado=True` **a propósito**: en ese momento
  `ToolStateMachine` solo conocía `PENDING → RUNNING → COMPLETED/FAILED` y no había consumidor de
  ese campo (verificado por grep), así que una etapa *asistida* se habría visto como *fallida*.
  El marcador era explícitamente provisional: «El mapeo se decide y se congela junto con el
  cableado». Se retiró en el PR B junto con su consumidor (§9, ronda 8).
- **PR B (mergeado, #853):** cablea **las tres** superficies en el mismo PR:

  | Superficie | Antes | Ahora |
  | --- | --- | --- |
  | `dispatcher_dependencies.build_wrye_bash_pipeline` | `service.execute_pipeline` | `service.ejecutar_fase_asistida` |
  | `SupervisorAgent.execute_wrye_bash_pipeline` | idem | idem, con `fase`/`testigo_previo` en la firma |
  | `system_tools.generate_bashed_patch` | idem, **sin** `path_resolver` y con `profile="agent-tool"` | idem, con `path_resolver` y el perfil de sesión (H-1) |

  La decisión de fase vive en **un** método del servicio —`ejecutar_fase_asistida`— y las tres
  superficies llaman a ese, no a `preparar`/`verificar` por su cuenta: tres copias de la elección
  es la forma en que dos superficies se quedan atrás. El ancla
  `tests/test_wrye_bash_cableado_asistido.py` **enumera** los call sites por AST (archivo +
  función envolvente): una cuarta superficie, o una que vuelva a `execute_pipeline`, rompe el
  test. `fase` y `testigo_previo` entraron a `_VALID_BASHED_PATCH_KEYS` de la estrategia — sin
  eso la fase 2 era inalcanzable: el filtro la descartaba en silencio y toda invocación caía en
  la preparación.

  El mapeo de `pendiente` se congeló como **estado `AWAITING_OPERATOR`** (`ToolStateMachine`),
  y no como `AWAITING_APPROVAL` (inalcanzable desde `RUNNING`), ni `FAILED` (nada falló), ni
  `COMPLETED` (nada se construyó). Es terminal sin salidas —la etapa se cierra con **otra**
  invocación en fase `verificar`— y libera la clave de idempotencia como cualquier terminal.

  Y el **toast del operador** aprendió lo mismo: `summarize_ritual_result` devuelve
  `warning` con el mensaje de la etapa en vez de «El ritual «X» falló: …» en rojo. Los dos
  consumidores comparten el predicado (`tool_result.es_etapa_pendiente`) para que no puedan
  divergir. El estado `AWAITING_OPERATOR` **no tiene hoy ningún consumidor de UI** (verificado
  por grep): el que el humano ve es el toast, y ese era el que mentía.
- **PR C (mergeado, #862): retira el camino headless `execute_pipeline` — sin compat.** La decisión, la
  evidencia que la sostiene y el mapeo test-por-test están en **§7.1**. En una línea: cero callers
  de producción, un hueco de saneo que **se cierra por retiro** en vez de por parche, y 36 tests
  que sólo podían pasar contra código inalcanzable. Los 9 de `tests/test_wrye_bash_lock.py` **no**
  se retiran: se reescribieron en el PR B al contrato nuevo y se actualizaron otra vez al perder
  el handler sus últimos parámetros (anclan la superficie del agente, no el servicio).
- **PR D (este): testigo durable entre sesiones (`etapa6_testigos`).** El detalle de por qué no se
  copia `DeploymentHandoff`, cómo se identifica el slot virtual y qué hace la preparación ante un
  build intermedio está en **§7.2**. Cableado en **ambos** sitios productivos de construcción del
  servicio (`orchestration_composition` y `system_tools.generate_bashed_patch`, anclados por AST y
  oráculo conductual en `tests/test_etapa6_testigo_durable.py`).
- **Deuda declarada (PR B): la compuerta HITL sigue sobre `generate_bashed_patch`.** El ítem 8 de §2
  dice que la fase 2 «no requiere HITL», pero `tool_dispatcher.py` registra la estrategia con
  `_destructive_middleware` y la tool sigue en `DESTRUCTIVE_TOOL_PATTERNS`. Efecto medido leyendo
  `HitlGateMiddleware`: sin `HITLGuard` configurado **ambas** fases (de solo lectura) devuelven
  `HITLGateUnavailable` antes de correr; con guard, el operador aprueba dos fases que no mutan. Se
  deja así a propósito en este PR: quitar una compuerta es una decisión de seguridad aparte, no parte
  de un cableado (hilo de revisión de CodeRabbit sobre `tool_dispatcher.py`, resuelto con esta nota).
  Para cerrarla hay que decidir si las fases asistidas salen de `DESTRUCTIVE_TOOL_PATTERNS` (y el
  dispatcher pasa a `ErrorWrappingMiddleware` + `DictResultGuardMiddleware`) y agregar un test del
  dispatcher que alcance el enrutado asistido sin guard. No verificado: si en el entorno de producción
  `AppContext.hitl` está siempre configurado (de ser así, el efecto es solo fricción, no bloqueo).

### 7.1 PR C — retiro del camino headless: decisión y evidencia (2026-11-03)

**Decisión: se retira `execute_pipeline` y todo su andamiaje. No se conserva como compat
documentada.** No es una preferencia de estilo; es lo que la evidencia medida deja como única
opción que no agrega mentiras.

| Pregunta | Medición | Cómo se midió |
|---|---|---|
| ¿Quién llama `execute_pipeline` en producción? | **0** callers en `sky_claw/` | `grep` de `execute_pipeline` sobre el paquete: sólo la definición y 3 menciones en prosa (docstrings de `wrye_bash_service`, `system_tools` y un comentario de `__init__.py`). El dispatcher, el supervisor y la tool llaman a `ejecutar_fase_asistida` desde el PR B. |
| ¿Cuánto código muerto hay? | **9 métodos** (`execute_pipeline` + 8 helpers de caja negra y rollback, incluido `ensure_runner`) y **3 clases module-level** (`_DesenlaceDelRollback`, `_ActionManifestError`, `_RunFallidoError`) | AST: alcanzabilidad desde los 3 métodos públicos. 8 de los 10 métodos privados no eran alcanzables desde ningún camino vivo; `ensure_runner` sólo lo llamaba `execute_pipeline`. |
| ¿Cuánto pesa? | `wrye_bash_service.py`: **1231 → 661 líneas** (−570 netas) | `wc -l` sobre `b247aea` y sobre el HEAD del PR. |
| ¿Qué dependencias se van? | `lock_manager`, `snapshot_manager`, `journal`, `wrye_bash_runner`; `__init__` pasa de **8 a 4** parámetros | AST por método: sólo `execute_pipeline` y sus 6 helpers tocaban `_lock_manager`/`_snapshot_manager`/`_journal`; **ninguno** de los 9 métodos que quedan los toca. |
| ¿Se pierde cobertura real? | De los 36 tests de `test_wrye_bash_service.py`, **8 contratos sobreviven** (4 se portaron a la suite asistida en este PR) y **28 se retiran con el código** | Mapeo test por test, abajo. |
| ¿Y el hueco de saneo de `stdout`/`stderr` que el PR B dejó anotado? | **Se cierra por retiro**: sin camino que devuelva la salida de un subproceso no hay texto de terceros que sanear | `wrye_bash_service.py` ya no importa `sanitize_for_prompt` ni menciona `stdout`/`stderr`; el ancla nueva prohíbe que vuelva a existir un runner en la firma de la tool. |
| ¿Y el lock `Bashed Patch, 0.esp`? | **Se retira con el camino**: era el id que tomaba `execute_pipeline` | Sin tomador, un id de lock canónico es una mentira sobre la etapa 6: el próximo lector creería que sigue serializada. El nombre del artefacto sobrevive **sólo** como `BASHED_PATCH_NAME`. |

**Por qué «retirar» y no «declarar compat documentada».** Conservar el método habría exigido:
(a) agregarle el saneo de `stdout`/`stderr` que el PR B verificó ausente; (b) mantener 36 tests
y ~570 líneas de un camino que ningún caller alcanza —y cada uno de esos tests es una invitación
a creer que la etapa 6 todavía construye el parche—; (c) sostener cuatro dependencias
(`lock_manager`, `snapshot_manager`, `journal`, `wrye_bash_runner`) en la firma del servicio, que
es exactamente cómo se reconstruye un camino muerto por partes. El ADR §2 dice que la caja negra
existe para lo que puede mutar: la etapa asistida no muta, así que no hay nada que la compat
documentada pudiera proteger.

**El ancla.** `tests/test_wrye_bash_retiro_headless.py` (8 funciones, 12 ítems) congela el retiro
por AST en tres direcciones: (1) los 9 helpers **no están** (intersección sobre nombres
literales, para que el mensaje nombre al que volvió); (2) el conjunto de métodos de la clase es
**cerrado e igual** a los 10 que quedan —un helper nuevo con otro nombre no tiene hueco—; (3) la
superficie pública es exactamente
`{ejecutar_fase_asistida, preparar_etapa_asistida, verificar_etapa_asistida}` y
`__init__(path_resolver, plugin_limit_guard, preflight, default_domain)`. Además enumera
**todo** `sky_claw/` para que ninguna construcción de producción vuelva a pasar las 4
dependencias muertas, prohíbe el literal `Bashed Patch, 0.esp` fuera de una línea que nombre
`BASHED_PATCH_NAME`, y enumera los 5 símbolos de lock/TX que el módulo ya no puede importar.

**Prueba por mutación (12/12).** Cada mutación reintroduce un trozo de lo retirado (o algo
equivalente) y se corre el test que debería verlo:

| Mutación | Test que la mata |
|---|---|
| `__init__` vuelve a pedir `lock_manager` | `test_la_firma_no_pide_las_dependencias_del_camino_muerto` |
| vuelve el método `execute_pipeline` | ancla de superficie pública (`retiro_headless`) |
| literal `"Bashed Patch, 0.esp"` suelto en producción | `test_el_lock_del_bashed_patch_ya_no_existe` |
| un constructor de producción vuelve a pasar `lock_manager` | `test_ningun_modulo_de_produccion_construye_el_servicio_con_dependencias_muertas` |
| la tool vuelve a admitir un `runner` posicional | `test_wrye_bash_lock::test_el_handler_no_admite_un_runner` |
| el guard M-04 aborta también cuando `valid=True` | `test_la_fase_1_sigue_si_el_guard_m04_valida` |
| el preflight rojo consulta el guard | `test_un_preflight_en_rojo_no_consulta_el_guard_m04` |
| el preflight verde ensucia el `result` | `test_un_preflight_verde_no_ensucia_el_resultado` |
| `validate_limit=False` consulta el guard igual | `test_validate_limit_false_no_consulta_el_guard` |
| entra un helper privado con un nombre **nuevo** (no está en la lista de retirados) | `test_el_servicio_ya_no_expone_el_camino_headless` (segunda aserción: conjunto **cerrado** de métodos) |
| el módulo vuelve a importar `LockError` | `test_el_modulo_ya_no_importa_la_maquinaria_del_camino_muerto` |
| `AsyncToolRegistry` vuelve a aceptar `wrye_bash_runner` | `test_el_registry_del_agente_ya_no_recibe_un_runner_de_wrye_bash` |
| una construcción del registry vuelve a pasar `wrye_bash_runner=` | idem (segunda aserción, por AST) |

**Mapeo test por test de `tests/test_wrye_bash_service.py` (36 retirados, 8 contratos vivos).**
«Vive» significa que el contrato se sigue midiendo; entre paréntesis, dónde.

| Test retirado | Qué congelaba | Disposición |
|---|---|---|
| `test_genera_bashed_patch_exitoso` | el pipeline headless podía reportar éxito | se retira: no hay pipeline que tenga éxito. El veredicto por evidencia vive en `test_wrye_bash_asistida` (`verificar_*`) |
| `test_toma_el_lock_durante_la_corrida` | lock tomado mientras corre el runner | se retira con el lock. La negación (la etapa asistida no toma locks) la ancla `test_wrye_bash_lock::test_el_handler_no_toma_ningun_lock` |
| `test_serializa_cuando_el_lock_ya_esta_tomado` | serialización entre corridas | se retira: no hay recurso compartido que serializar |
| `test_libera_lock_ante_fallo_del_runner` | sin leak de lock ante excepción | se retira: no hay lock que liberar |
| `test_guard_m04_falla_aborta_sin_correr` | M-04 rojo aborta antes de correr | **vive**: `test_wrye_bash_asistida::test_la_fase_1_aborta_si_el_guard_m04_falla` |
| `test_guard_m04_valido_corre` | M-04 verde no aborta | **vive**: portado como `test_la_fase_1_sigue_si_el_guard_m04_valida` (no existía el camino feliz del guard en la suite asistida) |
| `test_validate_limit_false_saltea_el_guard` | el interruptor `validate_limit` | **vive**: portado como `test_validate_limit_false_no_consulta_el_guard` |
| `test_runner_no_disponible_devuelve_error` | `ensure_runner` falla cerrado | se retira con `ensure_runner`; la firma ya no admite un runner |
| `test_lock_contention_devuelve_error` | contención de lock devuelve dict | se retira con el lock |
| `test_ensure_runner_construye_desde_el_resolver` | construcción perezosa del runner | se retira: `ensure_runner` no tiene caller vivo |
| `test_toma_tambien_el_lock_de_load_order` | lock anidado `load-order` | se retira con el lock |
| `test_serializa_contra_sort_de_loot` | un sort de LOOT bloquea la corrida | se retira: la etapa asistida no compite por el load order |
| `test_lease_perdido_al_cerrar_devuelve_error` | lease perdida en el `__aexit__` | se retira: no hay lease |
| `test_fallo_non_zero_emite_message_canonico` | `message` canónico desde el stderr | se retira: ningún veredicto se deriva de un exit code (ADR §2) |
| `test_exito_message_vacio` | `message` vacío en éxito | se retira: idem |
| `test_preflight_red_bloquea_sin_correr` | preflight rojo frena | **vive**: `test_preparar_bloquea_si_el_preflight_esta_en_rojo` |
| `test_preflight_red_no_llama_al_guard_m04` | el rojo corta antes del guard | **vive**: portado como `test_un_preflight_en_rojo_no_consulta_el_guard_m04` |
| `test_preflight_yellow_no_bloquea_pero_surface` | amarillo no bloquea y viaja | **vive**: ya cubierto en `test_wrye_bash_asistida` (amarillo adjunto al `result`) |
| `test_preflight_green_no_ensucia_el_result` | verde no adjunta la clave | **vive**: portado como `test_un_preflight_verde_no_ensucia_el_resultado` |
| `test_sin_fuentes_resolubles_no_gatea` | sin rutas no hay gate | **vive**: extremo a extremo en `test_preparar_sin_data_resoluble_falla_cerrado` |
| `test_cn_emite_manifest_antes_de_correr_y_flight_report_tras_commit` | caja negra T-26/T-28 | se retira: la etapa no abre TX (ADR §2) |
| `test_cn_manifest_fail_closed_aborta_sin_correr` | manifiesto fail-closed | se retira: no hay manifiesto |
| `test_cn_sin_journal_no_emite_pero_corre` | journal opcional | se retira: el servicio ya no recibe journal |
| `test_cn_flight_report_best_effort_no_rompe_exito` | informe best-effort | se retira: no hay informe |
| `test_cn_fallo_de_ejecucion_marca_rolled_back` | TX rolled-back por excepción | se retira: no hay TX |
| `test_cn_resultado_non_zero_marca_rolled_back` | TX rolled-back por exit non-zero | se retira: no hay TX ni exit code |
| `test_cn_lock_contention_no_abre_transaccion` | el manifiesto va dentro del lock | se retira con ambos |
| `test_cn_lease_perdida_marca_rolled_back` | lease perdida cierra la TX | se retira con ambos |
| `test_cn_cancelacion_marca_rolled_back_y_propaga` | cancelación cierra la TX y propaga | se retira: no hay TX que cerrar |
| `test_target_files_incluye_la_ruta_resuelta_del_bashed_patch` | el lock externo recibe `target_files` | se retira con el lock. Los destinos de permisos siguen vivos y cubiertos en `test_output_targets` |
| `test_fallo_non_zero_restaura_el_bashed_patch_previo` | rollback U-04 del artefacto | se retira: la etapa no escribe el artefacto |
| `test_timeout_tambien_restaura_el_bashed_patch_previo` | rollback ante timeout | se retira: no hay subproceso |
| `test_rolled_back_distingue_el_revertido_del_que_dejo_basura` | el matiz del rollback | se retira con el rollback |
| `test_fallo_sin_patch_previo_no_crashea` | primer run sin artefacto previo | se retira: no hay snapshot |
| `test_rollback_fallido_deja_la_tx_pendiente` | TX pendiente si el restore falla | se retira: no hay TX |
| `test_cn_fallo_non_zero_con_rollback_marca_rolled_back` | rollback + cierre de TX | se retira con ambos |

**Efectos colaterales del retiro, corregidos en el mismo PR.** (1) El literal `Bashed Patch,
0.esp` aparecía suelto en 5 módulos (`wrye_bash_asistida`, `wrye_bash_runner`,
`wrye_bash_service`, `system_tools`, `validators/etapas_previas_lods`): se consolidó en
`BASHED_PATCH_NAME` —`etapas_previas_lods` ahora importa la constante en vez de repetir el
string—. (2) **Tres censos vivos detectaron el retiro por sí solos** y se actualizaron sin
debilitarlos: el de raíces de MO2 (`test_cada_consumer_declara_que_raiz_necesita`) perdía la
entrada de `wrye_bash_service` en `get_mo2_path` —se actualizó con el racional escrito en el
docstring, que es el procedimiento que ese censo pide para un consumer que cambia de accessor—;
el de mecanismos de rollback (`test_rollback_salida`) perdía su entrada `"snapshot"` porque el
módulo ya no construye `SnapshotTransactionLock`; y el de productores de evidencia orphan
(`test_orphan_evidence_producer_contract`) perdía sus dos call sites de
`persist_action_manifest`/`persist_flight_report`. Los tres son igualdad literal: no se relajó
ninguno, se declaró la desaparición y queda escrito por qué. (3) `test_wrye_bash_lock.py` dejó de
poder pasarle `lock_manager`, `snapshot_manager`, `journal` y un runner al handler: los 9 tests se
adaptaron **sin perder ninguno** (el de "no invoca el runner" pasó a "no admite un runner", que es
más fuerte).

### 7.2 PR D — testigo durable entre sesiones: decisiones de diseño (2026-11-04)

El borrador original de §6/§7 citaba el «patrón `texgen_handoff`» como referencia conceptual. Al
contrastarlo contra `sky_claw/app/db/handoffs.py` aparecen tres diferencias estructurales que
prohíben copiar `DeploymentHandoff`:

1. **`DeploymentHandoff` exige `source_tx_id NOT NULL REFERENCES transactions(transaction_id)`.** Su
   razón de vivir en `journal.db` es cerrar atómicamente «TX + handoff de un árbol que Sky-Claw
   escribió». La fase 1 de la etapa 6 **no abre TX** por principio (§2) y no escribe ningún árbol:
   registra una *observación previa* de 32 bytes para comparar después, nunca autoridad de
   provenance.
2. **La ruta ganadora del overlay cambia entre fases.** En la primera corrida el parche no existe en
   ninguna capa y `bashed_patch_en_overlay` cae al fondo (`<game>/Data/...`); cuando el operador
   construye dentro de MO2 bajo la USVFS, el archivo aparece en `overwrite/` o en `mods/<mod>/`.
   Clavear por `artefacto.ruta` fallaría el 100 % de las primeras corridas. `ClaveDeTestigo`
   identifica el **slot virtual** con el par canónico `(plugins_txt, destino_data)` (serializado en
   JSON como arreglo de dos elementos, inyectivo para cualquier carácter de ruta): fija instancia de
   MO2, perfil e instalación del juego sin acoplarse a la capa ganadora.
3. **Ubicación por usuario (`SystemPaths.runtime_state_dir()`), no `.skyclaw_backups/`.** Siguiendo
   ADR 0011 P0.2 (`sky_claw/config.py:138`), el archivo vive en
   `~/.sky_claw/state/etapa6_testigos.json`, resuelto perezosamente al operar: dos procesos lanzados
   desde `cwd` distintos comparten el testigo y la fixture autouse de `tests/conftest.py` lo aísla
   por test sin ensuciar el árbol de trabajo.

**Qué pasa al reabrir la GUI en frío.** `ReactiveStore` vive en RAM: al reabrir la GUI el toast ya
no existe, así que el operador vuelve a pulsar la etapa 6 (`preparar`) para obtener el CTA de
verificación. Si esa segunda `preparar` sobrescribiera el testigo con el parche recién construido,
la fase 2 compararía el archivo contra sí mismo y fallaría con `SIN_CAMBIO`. La función pura
`decidir_testigo_de_preparacion` separa los casos y el servicio declara una partición total en
`EstadoDelTestigoDurable` (`guardado`, `conservado_por_build`, `conservado_sin_observacion`,
`sin_observacion`, `no_disponible`, `error_de_almacen`): ante un build intermedio conserva el
testigo anterior y lo devuelve en `testigo_previo` (`construido_despues_de_la_preparacion=True`). En
la fase 2 la precedencia es `payload` > `durable` > `ninguno` (`testigo_origen`), sin sustituir un
payload explícito malformado por estado de disco; el testigo durable **no** se consume si la
verificación falla (p. ej. `NO_HABILITADO` antes de tildar el plugin en MO2) y **sí** se consume al
demostrar frescura tanto por `durable` como por `payload`.

**Efecto sobre la superficie del agente LLM.** `GenerateBashedPatchParams` sigue exponiendo sólo
`fase` (`extra="forbid"`, A-4 intacto: el modelo jamás puede inyectar un hash), pero como
`system_tools.generate_bashed_patch` inyecta `almacen_por_defecto()`, una sesión que llamó
`fase="preparar"` deja el testigo en disco y otra sesión posterior que llame `fase="verificar"`
ahora demuestra `frescura_probada=True` sin confiar en el modelo.

## 8. Revisión adversarial del propio PR (2026-10-30)

El PR se revisó contra sí mismo antes de abrirse. Hallazgos **corregidos en el mismo commit**
(no diferidos), porque dos de ellos hacían que el ADR prometiera garantías que el código no
tenía — y un ancla que miente es peor que un bug:

| # | Hallazgo | Corrección |
|---|---|---|
| F1 | El ancla de «no spawnea» **no detectaba `create_subprocess_exec`** — la llamada que esta misma sección nombra — ni `spawnv`, `execv`, `create_subprocess_shell`. La propiedad que saca al módulo de `LANZADORES_ESPERADOS` no estaba verificada. | Se enumera la superficie completa de spawn **y se agrega un control positivo**: 8 snippets que sí spawnearon deben ser detectados. Sin control positivo, vaciar la lista dejaba el ancla en verde. |
| F2 | Con BOM UTF-8, mi lector decía `AUSENTE` y el hermano de la etapa 9 decía habilitado (falso ROJO con diagnóstico equivocado). | Decodificación `utf-8-sig` + test parametrizado (5 formas) con el hermano como oráculo de igualdad. |
| F3 | 3 de las 8 reglas (`ARTEFACTO_ILEGIBLE`, `ARTEFACTO_NO_REGULAR`, `PLUGINS_ILEGIBLE`) no tenían test que las disparara; y el test llamado `plugins_ilegible` probaba un archivo **inexistente** (`AUSENTE`). La rama que §2.4 declara central no estaba probada. | Se prueban las tres con directorios (portable, seguro como root, funciona en Windows) y `Path.open` parcheado para el `OSError` de lectura. El test mal nombrado se renombró a lo que hace. |
| F4 | `_MENSAJES` no estaba atado a `REGLAS_DEL_VEREDICTO`: un motivo nuevo sin mensaje explotaba con `KeyError` en producción con toda la suite verde. | Mapa total sobre `MotivoAsistido`, atado por igualdad, y `veredicto_como_resultado` parametrizado sobre los 10 motivos. |
| F5 | En la **primera** corrida la frescura nunca era demostrable (`huella_previa=None`), justo el caso más importante. | `TestigoPrevio(existia, sha256)` con ausencia probada. |
| F6 | `pendiente=True` sin consumidor: una etapa asistida se vería como fallida. | Se documenta como **no ratificado** hasta PR B. |
| F7 | El test de `chmod 000` es un no-op como root y no corre en Windows (única plataforma de entrega). | Directorio en vez de `chmod`. |
| F8 | Se citaba #835 (`modlist.txt`) para justificar la regla de duplicados en `plugins.txt`. | La regla se sostiene sola (semántica de activación del motor) y la cita se reemplazó por su fuente propia. |

## 9. Revisión adversarial EXTERNA (2026-10-30, rondas 2 y 3)

Dos vueltas sobre los comentarios de revisión del PR #839 (rondas 2 y 3 contando la
auto-revisión de §8). Cada hallazgo se verificó **contra el código antes de aceptarlo**: un
comentario de review es una afirmación, no un hecho. En la ronda 2 se confirmaron seis por
conducta y se rechazó uno; en la ronda 3 se aplicaron cinco y se rechazó otro. Los rechazos
quedan anclados por test para que no se confundan con un olvido.

**El patrón que los dos primeros comparten, y que esta etapa no había modelado:** la etapa 6
tiene **dos ejecutores**. `execute_pipeline` spawnea `bash.py` por `run_capture` y ese
subproceso lee el ``Data`` **físico**; el operador abre Wrye Bash **desde MO2** y ese proceso
lee el **overlay** del perfil bajo la USVFS. El contrato de §2 sólo describía al primero, así
que el preflight y la resolución del artefacto se escribieron contra el namespace equivocado.

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| C1 | `_ensure_preflight()` cablea `build_vfs_visibility_sensor`, que mide el ``Data`` físico; sobre un perfil cuyos mods viven en ``mods/`` la preparación devolvía `PreflightBlocked`. | **Confirmado por conducta**: con un perfil mods-only el preflight físico sale `red` («Ninguno de los 1 plugin(s) de mod del perfil es visible en Data») y el del dominio USVFS sale `yellow`. El propio docstring del sensor declara esa precondición (PR-586F). | `_ensure_preflight(dominio=…)` con dos cachés. El dominio USVFS omite ESE sensor y, tras la ronda 3, también cambia el destino del sensor de **permisos** (ver R3-1). |
| C2 | `_bashed_patch_path()` → `bashed_patch_target()` devuelve sólo el ``Data`` físico: un patch construido por el operador en ``overwrite/`` o en un mod de salida daba `ARTEFACTO_AUSENTE`. | **Confirmado por conducta**: con el patch en ``mods/SalidaBashed/`` el resolver viejo produce `ARTEFACTO_AUSENTE` y el nuevo `OK`. | `output_targets.bashed_patch_en_overlay()`: `overwrite` > mods en prioridad decreciente > ``Data``. `bashed_patch_target` **se conserva** porque describe al otro ejecutor. |
| C3 | `TestigoPrevio.desde` mapeaba *ilegible* a `existia=False`, indistinguible de ausencia probada. | Un directorio produce `existe=True, sha256=None`; el testigo decía "no estaba". | `desde()` devuelve `None` (no sé) y `PreparacionAsistida.testigo_previo` pasa a `TestigoPrevio \| None`. |
| C4 | El lector toleraba UTF-8 inválido con `errors="replace"` y respondía `HABILITADO` donde `_plugins_habilitados` devuelve `None`. | Confirmado: un byte inválido en un comentario ajeno al plugin no cambiaba el veredicto. | Validación **estricta del archivo entero** antes de parsear → `ILEGIBLE`. **No reintroduce F2**: `removeprefix(BOM)` + `utf-8` estricto equivale a `utf-8-sig` estricto, y hay test de que el BOM sigue aceptado. |
| C5 | `_testigo_desde_resultado` aceptaba `{existia: True, sha256: None}`, que hacía `frescura_demostrable()==True` sin identidad. | Es también la forma que producía C3: los dos hallazgos eran el mismo agujero visto desde cada extremo. | Invariante en `TestigoPrevio.__post_init__` (rechaza ambos estados imposibles) + `_testigo_desde_resultado` traduce el rechazo a `None`. |
| C6 | El `except Exception` de `preparar_etapa_asistida` devolvía un dict sin `_attach_preflight`. | Era el único retorno de la fase 1 sin el semáforo. | `preflight_report` se inicializa antes del `try` y el camino de excepción lo adjunta. |
| C7 | *Especulativo*: reordenar `ARTEFACTO_ILEGIBLE` y `ARTEFACTO_NO_REGULAR` por "especificidad". | Las dos condiciones casi no se solapan: sólo cuando el `lstat` falla (`ilegible=True, es_regular=False`) no sabemos qué es la ruta. Ahí `NO_REGULAR` afirmaría una propiedad no observada. | **Rechazado**, con test que fija los dos casos (lstat fallido → `ILEGIBLE`; directorio → `NO_REGULAR`). |

**Ronda 3 (sobre `6db53c4`).** Tres hallazgos menores y dos más de la segunda guía del
PR-Agent; el mismo criterio: verificar antes de aceptar.

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| R3-1 | El preflight del dominio USVFS seguía sondeando **permisos de escritura sobre el `Data` físico**. En una instalación de Steam ese ``Data`` es de sólo lectura, así que el sensor salía rojo y bloqueaba un perfil sano. | Correcto, y desmiente una frase de §9/C1: *la VFS redirige la escritura, no hereda los permisos del destino*. Es el mismo error que C1 en otro sensor. | `_permission_targets(dominio=…)`: el dominio USVFS sondea el ``overwrite`` del perfil. Ancla doble: los targets por dominio y un `Data` que deniega escritura (probe sustituido) que bloquea al dominio físico y no al asistido. |
| R3-2 | `_mods_habilitados` usaba `is_file()`, que devuelve `False` también para un directorio o cuando `stat` falla: eso colapsaba en "el perfil no habilita mods" con `cobertura_completa=True`. | Correcto: un `modlist.txt` ilegible produce `ARTEFACTO_AUSENTE` sin haber mirado ninguna capa de mods. | Sólo `FileNotFoundError` prueba la ausencia; cualquier otro resultado lo decide el lector canónico, que falla cerrado → `SinRutasResolubles`. |
| R3-3 | El test del junction hacía `pytest.skip` en todas las plataformas, contra el contrato documentado de `crear_junction`. | Correcto, y contradecía la lección de F7 (Windows es la única plataforma que puede ejercer el reparse point). | `@junction_guard` + `assert`, como `tests/test_links.py`. |
| R3-4 | El mensaje de `ARTEFACTO_AUSENTE` nombraba sólo el `Data` del juego, aunque la resolución ya revisa `overwrite` y los mods. | Correcto: mandaba al operador a mirar donde el sistema sabe que no está. | El mensaje nombra las tres capas, con ancla. |
| R3-5 | Un acierto en `overwrite` con los mods no enumerables quedaba marcado como cobertura parcial. | Correcto pero por la razón opuesta a la que sugería el reporte: `overwrite` gana a toda capa, así que ninguna capa no leída puede taparlo. | `cobertura_completa=True` para ese caso; la degradación parcial se registra con nombre en el log en vez de heredar un verde. |
| R3-6 | `pendiente_no_ratificado` sin consumidor; `_perfil_es_usable` sin normalizar mayúsculas. | El primero era F6: **cerrado en el PR B** — la clave se retiró y `IdempotencyMiddleware` traduce `pendiente=True` al estado `AWAITING_OPERATOR` de `ToolStateMachine`, con la clave de idempotencia liberada como en cualquier terminal. El segundo no es un defecto: el nombre compone una ruta que consume el mismo sistema de archivos, y normalizar rompería instalaciones case-sensitive sin ganar nada (los separadores ya se rechazan). | Sin cambio, con el motivo escrito acá. |

**Cambio de censo que estos fixes obligan.** `wrye_bash_service.py` pasó de `FAMILY_D`
(*sólo nombra el modlist en un docstring*) a `FAMILY_A` (*lo lee e interpreta la prioridad*):
`_mods_habilitados` usa `read_enabled_mods` y `bashed_patch_en_overlay` recorre esas capas de
mayor a menor prioridad. Es la clasificación que el censo existe para forzar, y es
**más** estricta que la anterior, no menos.


**Ronda 4 (auditoría adversarial pre-merge).** Auditoría sobre HEAD `14224e98`:

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| A-1 | `self._artefacto_asistido(profile)` se llamaba de forma sincrónica en el loop del daemon antes de `asyncio.to_thread`. En perfiles con miles de mods realiza miles de syscalls bloqueantes. | Reproducido: demora de cientos de ms en el event loop. | `await asyncio.to_thread(self._artefacto_asistido, profile)` en ambas fases. |
| A-2 | Preflight evaluaba el perfil ACTIVO de MO2 (`get_active_profile()`), no el solicitado por el llamador. | Confirmado: mismatch genera rojo o verde falso si el perfil pedido difiere del activo. | `_ensure_preflight(..., profile=profile)` propaga el perfil a `build_mo2_profile_sources_resolver` y cachea por `(dominio, profile)`. |
| A-3 | Mutantes supervivientes: guard de perfil en fase 2 sin test; `_mods_habilitados` ante `OSError` sin test; aborto M-04 en fase 1 sin test. | Confirmado: mutaciones sobre las ramas vivas no rompían tests. | Parametrización de rechazo de separadores en ambas fases; tests para `lstat -> PermissionError` y fallo de M-04. |
| A-4 | Testigo fabricado con digest no-sha256 demostraba frescura errónea (fail-open). | Confirmado: digest arbitrario producía `sha256 != huella` y `frescura_probada=True`. | Validación regex `[0-9a-f]{64}` en `TestigoPrevio.__post_init__` y `_testigo_desde_resultado`. |
| M-1 | `bashed_patch_en_overlay`: `Path.exists()` no traga `PermissionError`, rompiendo contrato de "capa ilegible se saltea". | Confirmado en Python 3.11: `PermissionError` escalaba a `ErrorInesperado`. | Sondeo con `os.lstat` capturando `FileNotFoundError` y degradando `cobertura_completa=False` ante `OSError`. |
| M-2 | `SENSOR_FISICO_POR_DOMINIO` no declaraba `"wrye_bash_service": {"virtual_usvfs": False}`. | Confirmado: inventario incompleto. | Censo y harness actualizados en `tests/test_vfs_visibility_wiring.py`. |
| M-3 | Logs de fallo sin `pipeline_stage`. | Exigido por `AGENTS.md` §5.5. | `_ETAPA_WRYE_BASH = 6` incorporado en `extra={"pipeline_stage": 6, "tx_id": None}` y promoción en `tests/test_dyndolod_service.py`. |
| M-4 | `SinRutasResolubles` confundía modlist ilegible con ausencia de rutas MO2. | Mensaje y diagnóstico engañosos. | `reason="CoberturaDeOverlayIncompleta"` devuelto ante cobertura incompleta. |
| H-1 | Superficie de agente LLM (`system_tools`) sin `path_resolver` y con perfil no real. | Documentado como precondición de PR B. | Anotado en §7 y tabla de hermanos. |

**Ronda 5 (auditoría adversarial pre-merge sobre HEAD `5090659c`).** Re-auditoría de cierre:

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| M-A | `CoberturaDeOverlayIncompleta` culpaba a la enumeración de mods cuando la capa fallida era `overwrite` (`output_targets.py:292`). CodeRabbit thread en línea 667. | Confirmado: cuando `os.lstat` sobre `overwrite` falla, los mods sí se enumeraron pero el mensaje decía "No se pudo enumerar los mods del perfil". | Mensaje neutro (`"No se pudo inspeccionar alguna capa del perfil (overwrite o mods): no se puede afirmar dónde está el artefacto."`) + tests dedicados en `test_output_targets.py` y `test_wrye_bash_asistida.py`. |
| M-B | Envenenamiento de caché legacy en `_ensure_preflight`: llamadas con `profile="Pedido"` poblaban `_preflight_usvfs` afectando llamadas posteriores sin perfil (`profile=None`). | Reproducido: llamada con perfil contaminaba el atributo legacy usado por callers default. | Asignación a `_preflight` / `_preflight_usvfs` condicionada estrictamente a `if profile is None:`. Test de aislamiento de caché. |
| M-C | Asimetría de alias de dominio en el mismo archivo: `_ensure_preflight` mapeaba `"virtual_usvfs"` a `DOMINIO_USVFS`, pero `_permission_targets` no lo hacía (caía a Data físico). | Confirmado: violación del principio de hermanos en el mismo archivo. | Extracción de `_normalizar_dominio` y uso simétrico en ambos métodos. Test de normalización. |
| M-D | Métricas desactualizadas en cuerpo de PR (96 tests colectados vs 63/64 anteriores) y aclaración sobre `mypy` (`ignore_errors = true` en `local.tools.*` con auditoría manual de tipos). | Confirmado. | Actualización de documentación y descripción del PR. |

**Ronda 6 (auditoría adversarial pre-merge sobre HEAD `d1f0eac8`).** Re-auditoría de cierre:

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| F1 | `preparar_etapa_asistida` llamaba a `self._ensure_preflight` en el hilo del event loop; la construcción inicial de modlist/masters/vfs ejecuta I/O de disco síncrono antes de `preflight.run()`. | Confirmado por traza estática: `build_modlist_sensors` y `build_master_order_sensor` ejecutan `sources_resolver()` inmediatamente. | `preflight = await asyncio.to_thread(self._ensure_preflight, ...)` + `_preflight_lock = threading.Lock()` para sincronizar construcciones concurrentes. Tests de ejecución en worker thread y concurrencia. |
| F2 | `os.lstat(ruta)` sobre el archivo no detectaba si la carpeta antecesora (p. ej. `mo2/overwrite` o la carpeta de un mod) era un symlink o junction NTFS apuntando a un directorio externo. Además, modlist con traversal (`../`) podía escapar de `mods/`. | Reproducido en Linux y Windows: `lstat` sobre el archivo hijo no reporta enlace si el padre es un symlink/junction. | `huella_de_artefacto` verifica enlace en `ruta.parent` con `_es_reparse_point` / `S_ISLNK`, marcando `es_enlace=True`. `bashed_patch_en_overlay` filtra carpetas padre enlazadas y nombres con traversal (`..`, `/`, `\`, `:`). Tests con symlinks y junctions NTFS. |

**Ronda 7 (auditoría adversarial pre-merge sobre HEAD `1db9e1ce`).** Re-auditoría de cierre:

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| CR-1 | Validación de nombres de mods en `output_targets.py:276` y perfiles en `wrye_bash_service.py:138-140` usaba subcadena `".." in nombre`. Esto rechazaba nombres legítimos con puntos internos (ej. `Foo... Bar`, `Mod..v2`), bloqueando ambas fases con `CoberturaDeOverlayIncompleta` o `PerfilInvalido`. Tampoco rechazaba explícitamente el nombre exacto `"."` en `output_targets.py`. | Confirmado: subcadena `".." in nombre` causa falso positivo en nombres con elipsis o puntos suspensivos sin traversal, mientras que `.` pasaba como nombre válido resolviendo a `mods_dir`. | Alinear al patrón canónico del repo (`ganadores.py:112` y `output_targets.py:390`): `if not nombre or nombre in (".", "..") or any(sep in nombre for sep in ("/", "\\", ":")):` en `bashed_patch_en_overlay`, y simétricamente en `_perfil_es_usable` de `wrye_bash_service.py`. Tests dedicados para nombres válidos con puntos internos y rechazo estricto de `.`, `..` y separadores. |

**Ronda 8 (PR B, cableado de las tres superficies).** Auditoría del propio cableado, con el
criterio de siempre: cada afirmación verificada contra el código o contra una corrida real.

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| B-1 | El filtro de `GenerateBashedPatchStrategy` descartaba `fase`: toda invocación —GUI, supervisor y agente— caía en la preparación. La fase 2 del ritual era **inalcanzable**, no por diseño sino por un `set` de dos claves. | **Confirmado por conducta**: con `fase="verificar"` en el payload, el pipeline recibía sólo `profile`/`validate_limit`. | `fase` y `testigo_previo` entran a `_VALID_BASHED_PATCH_KEYS`; `fase=None` (clave presente y vacía del formulario) se descarta para que aplique el default en vez de fallar con `FaseInvalida`. |
| B-2 | Mi primer filtro usaba `filtered.get("fase") is None` y después `pop("fase")`: `get` devuelve `None` también cuando la clave **no está**, así que toda invocación sin `fase` elevaba `KeyError: 'fase'`. | Lo atajó `test_destructive_strategies_error_contract.py` (7 rojos) antes del commit: el `error_message` de la tarea era literalmente `"'fase'"`. | `in` + `del`, no `get` + `pop`; el contrato de errores destructivos vuelve a verde sin tocarlo. |
| B-3 | `tests/_orchestration_dispatcher_dependencies.crear_dependencias_desde_doble` —el gemelo de test del cableado— seguía con la firma vieja de dos argumentos: reenviaba `profile`/`validate_limit` y **descartaba** `fase`. El test que dice probar la fase 2 la habría visto llegar como preparación. | Confirmado: el mock registraba `mock(profile='MyProfile', validate_limit=False)`. Es el defecto dominante del repo, mudado a un helper. | El doble honra la firma completa del Protocol (`fase`, `testigo_previo`) y la reenvía. |
| B-4 | 7 de los 9 tests de `tests/test_wrye_bash_lock.py` pineaban el contrato viejo del handler (lock anidado, caja negra, lease perdido, saneo del runner). Uno de los dos que «pasaban», `test_handler_bloqueado_si_loot_tiene_el_load_order`, lo hacía **por otro motivo**: ahora fallaba con `SinRutasResolubles`, no por contención de lock. | Confirmado por corrida: 7 rojos y un falso verde detectable leyendo el `reason` del payload. | El módulo se reescribe a su contrato nuevo (delega en el enrutado, **no** toma lock, **no** emite caja negra, **no** invoca el runner), con el mismo número de tests. La cobertura de lock/rollback/manifiesto sigue donde vive el código: `test_wrye_bash_service.py` (36 tests, verdes) — hasta el PR C, que retiró ese código y esos tests junto con él (§7.1). |
| B-5 | Dos tests de `test_system_tools_medio001.py` pineaban la sanitización del `stdout`/`stderr` del runner en el handler. Esa rama ya no existe, pero el saneo **tampoco vive en el servicio**: `execute_pipeline` devuelve texto crudo. | Confirmado por grep: `sanitize_for_prompt` no aparece en `wrye_bash_service.py`; y `execute_pipeline` no tiene callers de producción tras el cableado. | Los dos tests se reescriben al contrato nuevo (ninguna salida de subproceso llega al LLM). El hueco queda **declarado en §7 (PR C)**: si `execute_pipeline` se conserva como compat, necesita su propio saneo. |
| B-6 | El censo `test_perfil_sesion_invariante` se rompe por partida doble: desaparece el único literal exento (`profile="agent-tool"`) y `generate_bashed_patch` entra a la familia `FAMILIA_DEL_PERFIL` por introspección. | Confirmado: `{} == {'sky_claw/app/agent/tools/system_tools.py': 1}` y la familia medida incluía el handler nuevo. | `LITERALES_ESPERADOS = {}` (se deja el censo **vacío**, no se borra el test) y el handler entra a la familia con receta propia. El oráculo de comportamiento no quedaba vacío: para esta tool el perfil no viaja a MO2 ni vuelve en el payload, así que se observa espiando `ejecutar_fase_asistida`. **Probado por mutación**: con `profile="Default"` el test rojo, con `self._mo2_profile` verde. |
| B-7 | `AWAITING_APPROVAL` no es alcanzable desde `RUNNING` en `ToolStateMachine`, así que no podía mapear `pendiente`. | Confirmado leyendo `_VALID_TRANSITIONS`. | Estado nuevo `AWAITING_OPERATOR` (terminal, sin salidas), con `_TERMINAL_STATES` consumido por los tres lugares que antes repetían la tupla a mano (`transition`, `active_task_count`, `cleanup_terminal`). Ancla doble: estado esperado y clave de idempotencia liberada. |
| B-9 | `GenerateBashedPatchParams` nació con el default de pydantic (`extra="ignore"`): un `profile` mandado por el modelo se descartaba **en silencio** y el LLM creería haber elegido el perfil. | Verificado contra el `execute()` del registry: con `ignore` no hay error ni aviso; con `forbid` el `ValidationError` es explícito y el router lo puede devolver al modelo. | `extra="forbid"`, igual que `DiagnosticarParallaxParams` y por el mismo motivo (spec de #454), con test de que `profile` y `testigo_previo` se rechazan. |
| B-8 | `tests/test_wrye_bash_asistida.py` exigía `pendiente_no_ratificado is True`, el marcador provisional del PR A. | Al retirarlo, el test quedaba rojo a propósito. | El test ahora exige la **ausencia** de la clave: dos campos para una sola decisión vuelven a divergir. Es el contrato ratificado, no la desaparición de una aserción. |

**Censo de tests que el cableado movió (medido en PR B; resuelto en PR C).** Con
`--collect-only` en la rama del PR B: `test_wrye_bash_asistida.py` **129**,
`test_wrye_bash_service.py` **36**, `test_wrye_bash_cableado_asistido.py` **24** (nuevos),
`test_wrye_bash_lock.py` **9** (reescritos, mismos 9 que antes). Los 9 del lock dejaron de ser
del servicio y anclan la superficie del agente; los **36** de servicio eran los que dependían del
camino headless. El PR C los retiró con el código: de sus **8 contratos que siguen vivos**,
**4 se portaron** a la suite asistida en ese PR (el camino feliz de M-04, el interruptor
`validate_limit` y los dos cortes del preflight) y los otros 4 ya estaban cubiertos por la
suite asistida o por `test_output_targets`; el mapeo test por test está en la tabla de §5.

**Ronda 9 (revisión adversarial del PR B, 2026-11-01).** Code review nivel senior sobre el
propio cableado: se buscaron huecos en las anclas, afirmaciones del PR que el código no
sostiene, y superficies nuevas sin cubrir. Todos verificados por conducta o por mutación.

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| S-1 | **El toast del operador decía «falló» sobre la etapa asistida.** El PR B arregló el `ToolStateMachine`, pero ningún consumidor de UI lee ese estado (grep: cero referencias fuera del módulo): lo que el humano ve es `summarize_ritual_result`, que con `success=False` devuelve `negative` con el prefijo «El ritual «X» falló: …». | **Reproducido**: el payload real de la fase 1 renderiza `("El ritual «wrye_bash» falló: Etapa 6 (Bashed Patch) requiere tu intervención…", "negative")` — un mensaje que se contradice solo, en rojo. | `summarize_ritual_result` devuelve `warning` con el mensaje de la etapa cuando `pendiente=True`. El predicado se extrajo a `tool_result.es_etapa_pendiente` y lo comparten el toast y `IdempotencyMiddleware`: duplicado, el primero que cambie deja al otro mintiendo. Anclas: toast amarillo, fallo real sigue rojo, verificación OK sigue verde. |
| S-2 | **El ancla enumeraba el router pero no a los que eligen fase por su cuenta.** Una cuarta superficie que llamara `preparar_etapa_asistida`/`verificar_etapa_asistida` directo no aparecía en ningún test: reintroducía una copia de la decisión de fase, justo lo que el punto único evita. | Confirmado por mutación: una función nueva llamando `servicio.preparar_etapa_asistida(profile=…)` pasaba la suite completa. | `test_las_dos_fases_solo_se_invocan_desde_el_enrutado` enumera los llamadores de los dos métodos con receptor: en producción sólo puede ser el router. Mutación → rojo. |
| S-3 | **«Sin callers de producción» se afirmaba sobre las tres superficies, no sobre el paquete.** El pin sólo miraba los tres archivos de `SUPERFICIES`; un caller nuevo en cualquier otro archivo volvía al camino headless sin romper nada. | Confirmado por mutación: un módulo nuevo con `servicio.execute_pipeline(...)` pasaba. | `test_ningun_caller_de_produccion_usa_el_camino_headless` enumera TODO `sky_claw/`: el único `.execute_pipeline(` es el de `execute_synthesis.py` (otro servicio, camino legítimo). Mutación → rojo; y sirve de tripwire para el PR C. |
| S-4 | **El `path_resolver` nuevo de la tool no tenía oráculo de *fallo* en la dirección del default.** El schema y el wiring estaban, pero `profile="Default"` seguía en la firma del handler: un registry/caller que no lo pasara prepararía instrucciones con el perfil de MO2 de otra instalación. | Confirmado: el handler devolvía `pendiente=True` con un resolver del perfil `Default` en vez de fallar cerrado. | `profile: str = ""` (sin default inventado) ⇒ `PerfilInvalido`. Test nuevo, mutación → rojo. |
| S-5 | **La superficie de inyección de esta tool no desapareció con la rama headless.** `artefacto` y `plugins_txt` se componen con nombres de carpeta de MODS —texto de terceros— y viajaban al prompt sin pasar por `sanitize_for_prompt`, que es lo que esta tool hacía antes con el `stdout`/`stderr` del runner. El CHANGELOG lo declaraba como hueco *del servicio*, pero éste lo alcanza al modelo. | El vector no es el marcador ASCII: Windows prohíbe `<`/`>` en nombres. Sí permite zero-width/bidi, y NFKC colapsa delimitadores de ancho completo (`＜tool_call＞`). Reproducido con un mod llamado `Salida<ZWSP>＜tool_call＞`. | `_sanear_rutas` sanea las dos claves de ruta antes de `json.dumps`, sobre una COPIA (el `preflight` adjunto es el mismo objeto que recibió otro consumidor). |
| S-6 | **Mi primer test del saneo era vacuo.** Afirmaba `"\u200b" not in json.dumps(salida)`, y `json.dumps` con `ensure_ascii=True` escapa el zero-width como la secuencia de seis caracteres `\u200b`: la aserción no podía fallar nunca. | **Lo demostró la propia mutación**: con `_sanear_rutas` desactivado el test seguía verde. | Se afirma sobre el VALOR de la clave, más control positivo (el veneno está en la ruta real) y oráculo igualdad con `sanitize_for_prompt(str(ruta))`. Mutación → rojo. |
| S-7 | **El oráculo del perfil en `test_perfil_sesion_invariante` era vacuo por construcción.** `assert [p for p in vistos if p != PERFIL] == []` pasa cuando la lista está vacía, es decir cuando el handler NO llama al enrutado. Para esta tool —única de la familia cuyo perfil no viaja a MO2 ni vuelve en el payload— esa lista vacía era el único oráculo. | Confirmado: si el lambda dejara de llamar al enrutado, el test seguía verde. | Igualdad con el número de llamadas esperado por tool (`[PERFIL]` para la etapa 6, `[]` para las demás). Mutación → rojo con el mensaje nombrando el perfil visto. |
| S-8 | **La fase 2 no tiene affordance en el panel**, y el ADR lo daba por hecho: «verificar → veredicto» como si el operador pudiera pedirlo. El botón del ritual despacha payload vacío ⇒ siempre `preparar`. | Confirmado: `ritual_runner.run_ritual` despacha `{}` y ningún módulo de `sky_claw/app/gui/` menciona `fase`. | **Arreglado en este PR**: `resume_action_from_result` deriva la fase 2 del resultado estructural (tabla `_ACCIONES_DE_RESUME`, cerrada por igualdad) y `run_ritual_resume` recibe el `payload` **obligatorio** — se fue el hardcode `{"run_texgen": False}`, que era lo que ataba el resume a DynDOLOD. 19 casos nuevos (14 funciones) en `tests/test_resume_etapa6_asistida.py`, con las mutaciones probadas: 5 sobre la acción/el wiring y 3 sobre la validación del testigo (ronda 10, S-10). Persistir el testigo entre sesiones sigue en el PR D. |
| S-9 | **`ProgressMiddleware` emitiría `ops.tool.completed` con `status="unknown"`** para la etapa pendiente (el contrato no tiene clave `status`). | Cierto pero **latente**: `ProgressMiddleware` no está cableado en ninguna composición de producción (grep: cero construcciones). | Sin cambio, con el motivo escrito: si alguien lo cablea, el evento necesita su `pendiente`/`AWAITING_OPERATOR`. |
| S-10 | **El seam de S-8 re-implementaba el contrato del testigo, con cuatro agujeros.** `_testigo_utilizable` aceptaba `sha256=""`, un digest de 63 caracteres, uno en mayúsculas y el estado imposible *ausencia CON identidad* — las cuatro formas que `TestigoPrevio.__post_init__` rechaza. | Reproducido con el predicado real (7 formas, 4 divergentes) y congelado en `test_un_testigo_que_el_dominio_rechaza_no_viaja` (6 casos): 4 en rojo antes del fix. Consecuencia: el botón afirmaba en su payload un testigo que `_testigo_desde_resultado` descartaba en silencio ⇒ `frescura_probada=False` después de que el panel prometió frescura. Fail-closed en el resultado, pero dos validadores con reglas distintas. | `_testigo_utilizable` **delega el contrato en el dominio**: construye `TestigoPrevio` y traduce su `ValueError` a `None` (los `isinstance` quedan porque la dataclass no valida tipos en runtime). Mutaciones: validador casero, `except` que devuelve el dict y pérdida de los `isinstance` → rojo las tres. |

**Lo que la revisión NO encontró** (buscado a propósito): fallos donde `pendiente` pudiera estar
presente (ninguna otra tool emite esa clave, grep), colisiones de idempotencia entre fase 1 y
fase 2 (claves distintas: el payload difiere), y regresiones en las anclas de censo (`24` tools,
`FAMILIA_DEL_PERFIL`, `LITERALES_ESPERADOS` vacío) — las tres verificadas por corrida.

**Ronda 11 (auditoría externa del PR C, 2026-11-03).** Auditoría adversarial del retiro, con
worktree propio sobre el HEAD del PR y ~1.750 tests ejecutados. Cuatro hallazgos, todos
corregidos acá y probados por mutación.

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| C-1 | **El ancla del nombre del artefacto tenía un punto ciego medible.** `test_el_lock_del_bashed_patch_ya_no_existe` eximía archivos por contener la subcadena `"mods"` (un truco para no tocar `output_targets.py`); el auditor agregó `X_LITERAL_MUERTO = "Bashed Patch, 0.esp"` en ese archivo —código vivo— y **el ancla pasó**. | **Confirmado por mutación** (reproducida acá): la versión por texto no veía un literal vivo en un archivo que menciona `"mods"`. | El ancla se parte en dos: el id de lock (sobre el módulo) y `test_el_nombre_del_artefacto_se_define_una_sola_vez`, que cuenta **constantes `str` por AST** y exige igualdad exacta en dos listas: el literal UNA vez en `wrye_bash_runner.py` (línea de la asignación) y UNA vez en el único módulo declarado como excepción. Las 3 docstrings de `output_targets.py` que lo nombraban pasaron a prosa sin el literal. **4/4 mutaciones nuevas mueren**, incluida la del auditor. Y al re-correr el set completo de mutaciones apareció un segundo hueco, no señalado por la auditoría: **re-agregar `wrye_bash_runner` a `AsyncToolRegistry` no rompía ningún test**. Se agregó `test_el_registry_del_agente_ya_no_recibe_un_runner_de_wrye_bash` (firma + construcciones por AST) y ahora **12/12 + 4/4** mueren. |
| C-2 | **Prosa que citaba una clase eliminada**: `system_tools._BodySlideRunFallidoError` y `pandora_service._PandoraRunFallidoError` decían «mismo patrón que `wrye_bash_service._RunFallidoError`», que el PR C borró. Un lector que fuera a buscar el ejemplo no lo encontraría. | Confirmado por grep: dos referencias, ambas en docstrings. | Ambos apuntan al ejemplo **vivo** (`pandora_service._RunFallidoError` y el `raise` de `synthesis_service`), y `pandora_service` deja escrito que el ejemplo anterior se retiró en §7.1. |
| C-3 | **Cifras del propio PR desalineadas con el archivo**: decía 1231 → 658 líneas cuando el módulo quedó en 661 (creció con dos docstrings posteriores), y el CHANGELOG decía «9/9 mutaciones» mientras el cuerpo decía 11/11. | `wc -l` sobre `b247aea` y el HEAD: 1231 → **661** (−570). `pytest --collect-only` del ancla: **11 ítems**. | Cifras corregidas en este ADR, en `CHANGELOG.md`, en el tablero y en el cuerpo del PR. Regla que se aplica: toda cifra se vuelve a medir **después** del último edit, no antes. |
| C-4 | **Duplicado de la constante fuera de este carril**: `sky_claw/local/xedit/patch_orchestrator.py` define su propia `DelegateToBashedPatch.BASHED_PATCH_NAME` con el mismo literal. | Confirmado por grep. **No se toca**: `sky_claw/local/xedit/**` pertenece a otro carril (constraint 5) y no es un cambio que este PR fuerce. El drift ya está anclado por comportamiento en `test_wrye_bash_lock.py::test_nombre_canonico_del_bashed_patch_sincronizado` (igualdad entre las dos definiciones). | Se **declara** como excepción enumerada en `_DEFINICIONES_TOLERADAS` del ancla, con archivo y línea exactos: una tercera definición, o mover ésta de archivo, rompe el test. Si el carril de xEdit la consolida, la excepción caduca y el ancla exige quitarla en el mismo commit. |

**Ronda 12 (segunda auditoría externa del PR C, 2026-11-04).** Auditoría adversarial sobre el
HEAD `6186ff3`, con el PR ya en verde (19/19). Cuatro hallazgos, ninguno P0/P1: dos anclas que
prometían más de lo que median, una prosa que describía un contrato inexacto y una cifra
desincronizada. Todos corregidos acá y probados por mutación.

| # | Hallazgo | Verificación | Corrección |
|---|---|---|---|
| H-01 | **El ancla del nombre del artefacto seguía acoplada a números de línea.** `test_el_nombre_del_artefacto_se_define_una_sola_vez` comparaba `(ruta, línea, valor)`; agregar un comentario o un import arriba de `BASHED_PATCH_NAME` la ponía en rojo **sin que cambiara ningún contrato** —un falso positivo, que es el modo de falla inverso al de la ronda 11. | **Reproducido** (`/tmp/falso_positivo.py`): un `# comentario` arriba de la asignación en `wrye_bash_runner.py` ⇒ el ancla fallaba. | El ancla compara ahora `(nombre, valor)` por **AST** y cuenta apariciones **por archivo** (`_definiciones_del_artefacto()`); la línea aparece **sólo en el mensaje de diagnóstico**. Siete casos nuevos: literal suelto, docstring, segunda definición, duplicado con otro valor y constante renombrada **mueren**; comentario e imports **viven** (7/7). |
| H-02 | **«No muta nada» era inexacto.** `preparar_etapa_asistida` no abre TX ni toma locks, pero el preflight corre el sensor de **permisos**, que es un write-probe real: crea y borra `.skyclaw_probe_<uuid>.tmp` en el destino, y en el dominio USVFS ese destino es el `overwrite` del perfil. La prosa prometía "sólo lectura" a secas y ningún test del **servicio** fijaba el contrato. | Confirmado leyendo `write_permissions._probe_write` y `_permission_targets(dominio=USVFS)`. El sensor **sí** estaba cubierto (`test_write_permissions.py::test_unlink_fallido_es_probe_residue`); lo que faltaba era el contrato en el borde del servicio. | Tres docstrings (módulo, clase y `preparar_etapa_asistida`) distinguen "no muta el artefacto, el perfil ni el load order" de "no escribe un solo byte", y nombran el probe. Dos tests de contrato nuevos: la fase 1 **sondea el `overwrite`** y la carpeta **queda vacía** al terminar; un unlink que falla deja el residuo **reportado** en el preflight como `yellow`, con la ruta, y **no** bloquea la preparación. **4/4 mutaciones mueren** (sondear `Data`, no borrar el probe, volver el residuo `critical`, tragarse el error). |
| H-03 | **`output_targets.py` seguía documentando el ejecutor retirado como ruta viva**: el comando `[bash, -b, <BASHED_PATCH_NAME>]` aparecía como el caso normal de Wrye Bash, y el docstring de `bashed_patch_target` afirmaba que el destino habilitaba "el snapshot/rollback de U-04 y el post-check de artefacto de U-06", ambos retirados con el camino headless. | Confirmado por lectura; el censo de consumidores da `surface_census.py:485`, `wrye_bash_service.py:338/380` y `tests/test_output_targets.py` — **el helper tiene consumidor vivo y no se borra**. | Los tres pasajes quedan **históricos y fechados** (ADR 0016 §7.1) en vez de presentes: `-b` como "históricamente", `bashed_patch_target` como "dónde escribiría un build físico" y destino del sensor de permisos, y la verificación apuntando a `bashed_patch_en_overlay`, que es el único camino que corre hoy. El helper se conserva y el texto dice por qué: borrarlo exige el censo, no lo decide un docstring. |
| H-04 | **El ADR se contradecía sobre los contratos portados**: §5 decía «8 contratos sobreviven (4 se portaron)» y §7.1 decía «8 siguen vivos y uno se portó» —el borrador previo al puerto de los dos cortes del preflight. | `grep` de los nombres: los **4** puertos declarados en la tabla de §5 existen y corren en `test_wrye_bash_asistida.py`. | §7.1 sincronizada con §5: **8 vivos, 4 portados**, enumerados (M-04 feliz, `validate_limit`, preflight rojo y preflight verde); la remisión apunta a la tabla de §5. |

**Lo que la auditoría confirmó sin hallazgos** (buscado a propósito): 0 callers con la firma vieja
del registry/handler (scan AST sobre `sky_claw/`, `tests/` y `scripts/`), 0 líneas de código
agregadas al servicio, las tres superficies enrutando por `ejecutar_fase_asistida`, HITL intacto
(`generate_bashed_patch` sigue en `DESTRUCTIVE_TOOL_PATTERNS`), y las notas de `execute_pipeline`
en `test_synthesis_service.py` son el método homónimo de **Synthesis**, servicio vivo.

**Ronda 13 (auto-auditoría adversarial del PR D, 2026-11-04).** Antes de abrir el PR D se auditaron
el diseño inicial y las anclas propias contra el código real y con una matriz de **15 mutaciones**
(14 de contrato que mueren + 1 control benigno de comentarios que vive):

| # | Hallazgo de auto-auditoría | Corrección verificada por mutación |
|---|---|---|
| D-01 | **Clavear por `artefacto.ruta` rompía el 100 % de las primeras corridas**: en fase 1 el archivo no existe y `bashed_patch_en_overlay` devuelve `Data`; en fase 2 aparece en `overwrite`. | `ClaveDeTestigo(plugins_txt, destino_data)` fija el slot virtual invariante respecto de la capa ganadora (M01 muere en `test_salto_de_capa_data_a_overwrite_prueba_frescura_sin_payload`). |
| D-02 | **Delimitador `"|"` en la clave**: en POSIX `|` es un carácter legal de ruta; dos pares distintos podían colisionar. | `como_texto()` serializa `json.dumps([plugins_txt, destino_data])` sin ambigüedad (M02 muere). |
| D-03 | **Ubicación `.skyclaw_backups/` dependía del `cwd` y ensuciaba el árbol en tests**, contradiciendo ADR 0011 P0.2 (`sky_claw/config.py:138`). | `almacen_por_defecto()` usa `SystemPaths.runtime_state_dir() / "etapa6_testigos.json"` de forma perezosa (M08 muere con `estado_durable_real`). |
| D-04 | **`guardar` sobre JSON corrupto contradecía la promesa de no pisar archivos ilegibles.** | `_cargar_objeto` lanza `AlmacenDeTestigosIlegibleError` en escritura/borrado y rechaza enlaces en padre y archivo con `link_kind_or_raise`; `leer` degrada a `None` sin escribir (M05, M06, M07 mueren). |
| D-05 | **Partición incompleta de `EstadoDelTestigoDurable`**: con almacén vacío y artefacto sin identidad (p. ej. directorio), ni se guardaba ni se conservaba nada. | Se agrega `SIN_OBSERVACION` y se unifica `ERROR_DE_ALMACEN` (lectura + escritura). |
| D-06 | **Consumo del testigo durable**: consumir en fallo perdía la frescura al olvidar tildar el plugin (`NO_HABILITADO`); no consumir cuando `origen == payload` dejaba un testigo huérfano que engañaba a la corrida siguiente. | Sólo se consume con `veredicto.frescura_probada is True`, tanto por `durable` como por `payload`, y un `OSError` en `olvidar` no tumba un veredicto válido (M11 y M12 mueren). |
| D-07 | **Punto ciego del ancla AST**: exigir `"testigo_store" in claves` dejaba pasar `testigo_store=None` en verde. | El ancla congela por igualdad los sitios productivos, rechaza `Constant(None)` y se acompaña de un oráculo conductual sobre `build_orchestration_composition` (M13 y M14 mueren; M15 con comentarios vive). |

**Ronda 14 (revisión externa de Codex y PR-Agent sobre #863, 2026-11-04).** Dos hallazgos P1 reales
sobre `ee40299`, reproducidos primero con tests en ROJO antes de tocar producción y sumados a la
matriz de mutación (**18/18**):

| # | Hallazgo | Reproducción | Corrección verificada por mutación |
|---|---|---|---|
| R14-01 | **Exclusión sólo de instancia en `AlmacenJSON`**: `orchestration_composition` y `system_tools` crean instancias distintas sobre el mismo archivo (y pueden coexistir dos procesos). Un `self._lock` local dejaba que dos `guardar` sobre perfiles distintos leyeran `{}` a la vez y el último `os.replace` borrara al otro perfil. | Reproducido en ROJO tanto entre dos instancias en el mismo proceso (`Barrier(2)`) como entre dos procesos Python separados (`subprocess.Popen` × 25 escrituras). | `_seccion_critica` serializa por **ruta canónica** en dos capas: `_LOCKS_POR_RUTA` intraproceso + lockfile hermano (`.etapa6_testigos.json.lock`, `fcntl.flock` en POSIX / `msvcrt.locking` en Windows, rechazando enlaces también sobre el lockfile y abriendo con `O_NOFOLLOW` donde existe). M16 y M17 mueren por separado. |
| R14-02 | **`pipeline_stage="wrye_bash"` textual y sin `tx_id`**: los warnings de `etapa6_testigos.py` emitían una cadena en vez del entero `6` (SOP §5 regla 5) y omitían `tx_id=None`, desapareciendo de las consultas estructuradas por etapa. | Reproducido en ROJO con `caplog`. | `_ETAPA_WRYE_BASH = 6` anclada por igualdad contra `wrye_bash_service._ETAPA_WRYE_BASH`, `extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None}` en todos los warnings del almacén y registro en `conocidos` de `test_dyndolod_service.py` (M18 muere). |

## 10. Registro de adjudicación

- Alternativa *(b) retiro*: evaluada y descartada por §3. No se reabre sin evidencia nueva de que el
  artefacto de la etapa 6 deja de ser requerido por la etapa 9.
- Alternativa *(c) RPA de coordenadas*: descartada en origen (no determinista, no corre en CI, fuera
  del contrato de journal/rollback).
