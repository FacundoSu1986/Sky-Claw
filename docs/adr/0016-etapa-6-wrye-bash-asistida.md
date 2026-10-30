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
- **No persiste la huella previa.** Hoy la transporta el caller entre fases (§7, PR D).
- **No detecta enlaces de forma TOCTOU-fuerte**: la clasificación es por `lstat` y la lectura es
  best-effort, igual que `artifact_digest`.

## 5. Consecuencias

- **Positivas:** el DAG cierra con un artefacto verificable; U-06 gana su post-check de Wrye Bash
  (artefacto + `plugins.txt`) como primitiva; el veredicto es auditable sin logs de terceros; no
  cambia el censo de lanzadores ni el contrato de argumentos CLI; los 40 tests del pipeline
  existente siguen intactos.
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

## 7. Secuencia de PRs — y por qué se parte

`AGENTS.md` §«La regla que más se viola» documenta que 9 de 13 follow-ups auditados son un fix que
aterrizó en una superficie y dejó intacta a su gemela. Esta etapa tiene **tres** superficies de
producción: `dispatcher_dependencies.build_wrye_bash_pipeline`, `SupervisorAgent.execute_wrye_bash_pipeline`
y la tool del agente LLM (`system_tools`). Por eso el cableado **no** se hace de a una:

- **PR A (este):** contrato + servicio + tests. **Sin wiring.** No cambia comportamiento de
  producción; 64 tests nuevos (63 + 1 skip de junction) y las 17 anclas verdes.

  `pendiente=True` viaja marcado `pendiente_no_ratificado=True` a propósito: `ToolStateMachine`
  solo conoce `PENDING → RUNNING → COMPLETED/FAILED` y no hay consumidor de ese campo (verificado
  por grep), así que hoy una etapa *asistida* se vería como *fallida*. El mapeo se decide y se
  congela junto con el cableado; hasta entonces ningún consumidor debe tratarlo como contrato.
- **PR B:** cablear **las tres** superficies en el mismo PR, con un ancla por AST que **enumera**
  las superficies del cableado (una cuarta, o una que vuelva a `execute_pipeline`, rompe el test).
  Requiere `fase` en `_VALID_BASHED_PATCH_KEYS` de la estrategia.
- **PR C:** retirar `execute_pipeline` (el camino headless) y sus 40 tests, o declararlo compat
  documentada. Decidir con la evidencia de PR B; hoy es la superficie que conserva la deuda
  "TX por ritual imposible" para callers directos.
- **PR D:** persistir el handoff de la fase 1 (huella previa durable, patrón `texgen_handoff`) para
  que la frescura no dependa de que el caller transporte el `sha256`.

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

## 9. Revisión adversarial EXTERNA (2026-10-30, ronda 2)

Segunda vuelta, esta vez sobre los comentarios de revisión del PR #839. Cada hallazgo se
verificó **contra el código antes de aceptarlo**: un comentario de review es una
afirmación, no un hecho. Seis se confirmaron por conducta y se corrigieron; uno se rechazó
con evidencia y queda anclado por test para que el rechazo no se confunda con un olvido.

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
| R3-6 | `pendiente_no_ratificado` sin consumidor; `_perfil_es_usable` sin normalizar mayúsculas. | El primero es F6, adjudicado en §7 (PR B). El segundo no es un defecto: el nombre compone una ruta que consume el mismo sistema de archivos, y normalizar rompería instalaciones case-sensitive sin ganar nada (los separadores ya se rechazan). | Sin cambio, con el motivo escrito acá. |

**Cambio de censo que estos fixes obligan.** `wrye_bash_service.py` pasó de `FAMILY_D`
(*sólo nombra el modlist en un docstring*) a `FAMILY_A` (*lo lee e interpreta la prioridad*):
`_mods_habilitados` usa `read_enabled_mods` y `bashed_patch_en_overlay` recorre esas capas de
mayor a menor prioridad. Es la clasificación que el censo existe para forzar, y es
**más** estricta que la anterior, no menos.

## 10. Registro de adjudicación

- Alternativa *(b) retiro*: evaluada y descartada por §3. No se reabre sin evidencia nueva de que el
  artefacto de la etapa 6 deja de ser requerido por la etapa 9.
- Alternativa *(c) RPA de coordenadas*: descartada en origen (no determinista, no corre en CI, fuera
  del contrato de journal/rollback).
