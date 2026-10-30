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
5. **La frescura solo se PRUEBA con testigo.** Con `huella_previa` (el `sha256` de la fase 1), un
   artefacto idéntico es `SIN_CAMBIO`. Sin testigo, el veredicto puede ser verde pero declara
   `frescura_probada=False`. **No se deduce un cambio por `mtime`**: es la comparación que el
   contrato de LOOT (PR-2) rechazó explícitamente a favor del estado observable.
6. **La fase 1 no escribe caja negra.** No muta nada, así que no toma locks, no snapshotea y no abre
   TX. Esto resuelve por sustitución la deuda del «cortocircuito antes del preflight»: el camino que
   podía dejar una TX por ritual imposible deja de ser el camino de producción.
7. **La fase 2 es read-only.** No requiere HITL ni lock: solo lee. La lectura del artefacto
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
  producción; 33 tests nuevos y las 17 anclas verdes.
- **PR B:** cablear **las tres** superficies en el mismo PR, con un ancla por AST que **enumera**
  las superficies del cableado (una cuarta, o una que vuelva a `execute_pipeline`, rompe el test).
  Requiere `fase` en `_VALID_BASHED_PATCH_KEYS` de la estrategia.
- **PR C:** retirar `execute_pipeline` (el camino headless) y sus 40 tests, o declararlo compat
  documentada. Decidir con la evidencia de PR B; hoy es la superficie que conserva la deuda
  "TX por ritual imposible" para callers directos.
- **PR D:** persistir el handoff de la fase 1 (huella previa durable, patrón `texgen_handoff`) para
  que la frescura no dependa de que el caller transporte el `sha256`.

## 8. Registro de adjudicación

- Alternativa *(b) retiro*: evaluada y descartada por §3. No se reabre sin evidencia nueva de que el
  artefacto de la etapa 6 deja de ser requerido por la etapa 9.
- Alternativa *(c) RPA de coordenadas*: descartada en origen (no determinista, no corre en CI, fuera
  del contrato de journal/rollback).
