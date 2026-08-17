# Roadmap DynDOLOD v2 — etapa 9 (TexGen + DynDOLOD)

> **Estado:** re-baseline verificado contra `main` `f8e8a4f` (2026-08-16). Documento
> de planificación: **no describe código existente**, describe el trabajo que
> queda.
>
> **Audiencia:** maintainers, reviewers y agentes que retomen la etapa 9.
>
> **Fuentes canónicas:** el código y los tests del árbol actual. Este documento
> ordena trabajo pendiente; ante cualquier contradicción con el código gana el
> código, según
> [`../../documentation/source_of_truth.md`](../../documentation/source_of_truth.md).
>
> **Antecedente:** re-baselinea un roadmap v1 (T1–T8) que **nunca se commiteó al
> repo** — vivió como documento de trabajo externo. Esta es la primera versión
> versionada, y por eso no enlaza a v1 ni lo marca *superseded*: no hay archivo
> que marcar.

## Estado por bloque

| Bloque | Estado | Cerrado en | Qué queda |
|---|---|---|---|
| T1 — contrato de argv | **CERRADO** | #462 | — |
| T5 — validación de argv en rig | **PARCIAL** | rig 2026-08-11 | Aceptación endurecida; ver T5-v2 |
| Preset desvía `OutputPath` | **ABIERTO** (nuevo) | registrado en #463 | Precedencia y mecanismo |
| T2 — clasificación del log | **PENDIENTE — próximo** | — | Todo; superficie intacta |
| T3 — staging y salida | **PENDIENTE — rebase** | — | Rediseño con el preset como restricción |
| T7 — preflight de modales | **PENDIENTE** | — | Después de T5-v2 |
| T8 — preset / UI Automation | **PENDIENTE** | — | Después de T7 |

## Deduplicación: qué dejó de ser trabajo

**#462 cerró T1 por completo.** `_switch_de_ruta` emite `-{letra}:{ruta}` sin
comillas propias; `list2cmdline` hace el quoting. El rig del 2026-08-11 confirmó
que el argv se parsea exacto en los dos binarios. Además aterrizó el contrato de
`extra_args` (`_extra_args_admisibles`): rechaza comillas, rechaza redefinir los
cinco switches administrados —case-insensitive— y exige `list[str]` en runtime.

**#463 registró el hallazgo del preset** en
[`../../pending_ooda_status.md`](../../pending_ooda_status.md), con su evidencia,
su análisis de riesgo y su lista de investigación previa. No hay fix diseñado.

**#471 cableó `pipeline_stage`/`tx_id`** en los registros de fallo del runner y
corrigió la fórmula de `run_full_pipeline` (`dyndolod_runner.py:1160-1167`) para
que el mod de TexGen sin empaquetar también vuelque el pipeline a rojo.

> **Precisión que cambia el tamaño de T2:** #471 **no** tocó las fórmulas de éxito
> de los lanzadores. `run_texgen` (`:526`) y `run_dyndolod` (`:637`) siguen siendo
> `return_code == 0 and post.artefacto and not errors`, idénticas entre sí. La
> superficie que T2 modifica quedó intacta, así que T2 **no necesita rebase de
> código** — solo preservar lo que #471 agregó alrededor.

## Los tres defectos, verificados en `main` `f8e8a4f`

| Defecto | Sitio | Efecto medido en rig |
|---|---|---|
| **Gap A** — `not errors` como gate duro | `dyndolod_runner.py:526`, `:637`; patrón en `:416-419` | 121 líneas `Error:` en una corrida **exitosa** ⇒ todo éxito real se rechaza |
| **Gap C** — candidato único de TexGen | `dyndolod_runner.py:1472` | `root/TexGen_Output` no existe nunca; el gate de artefacto de TexGen falla siempre |
| **Encoding del log** | `dyndolod_runner.py:1703` | Se lee `utf-8`; el log real es cp1252 ⇒ acentos a U+FFFD |

Ambos gaps quedan registrados como filas propias del inventario a partir de este
documento: un ítem sin fila no tiene gate y envejece, que es exactamente lo que
[`../../../AGENTS.md`](../../../AGENTS.md) advierte.

## T2 — clasificación semántica del log (próximo)

Separar **detección** de **clasificación**. Hoy `_parse_log` (`:1726`) produce una
lista plana que el veredicto trata como fatal.

**Taxonomía de tres cajas**, no dos:

1. **Completitud** — marcadores positivos, distintos por herramienta:
   TexGen `TexGen completed successfully`; DynDOLOD `DynDOLOD plugins generated
   successfully`, `Occlusion.esp completed successfully`,
   `LODGenx64Win6.exe generated object LOD for <ws> successfully`.
2. **No-fatales de dominio** — `Deleted reference`, `Unresolved FormID`,
   `LOD billboard(s) not found`, `No TexGen output detected`,
   `DynDOLOD.DLL ... not found`, `File not found SkyrimSE.exe`. Se reportan como
   advertencias, no como fallo.
3. **Terminales** — `Fatal:`, `Can not create path`, `Exception`/`madExcept`,
   `Path not allowed`, `core files are outdated`, y cualquier `Error:` que no esté
   en la lista de no-fatales. **Ante duda, terminal**: un no-fatal mal clasificado
   como terminal da un rojo revisable; al revés da un verde falso.

**Veredicto:** `rc == 0` AND artefacto fresco AND completion marker AND no terminal
failure. Sigue cruzando el exit code con evidencia independiente, así que respeta
`test_ningun_runner_deriva_el_exito_solo_del_exit_code`.

**Encoding:** orden determinista con decodificación **estricta**, y `errors="replace"`
reservado al último recurso:

1. `utf-8` estricto — se queda con el resultado si decodifica;
2. si no, `cp1252` estricto;
3. si ninguno decodifica, `cp1252` con `errors="replace"`.

El orden importa y la variante ingenua no funciona: `cp1252` acepta casi
cualquier byte, así que "leer cp1252 con fallback a utf-8" **nunca alcanza el
fallback** — un log utf-8 legítimo se decodifica igual y sale mojibake en
silencio. Agregar `errors="replace"` al primer intento lo empeora: garantiza que
no pueda fallar, y con eso mata cualquier fallback posterior. `utf-8` sí es
autovalidante (una secuencia inválida levanta `UnicodeDecodeError`), y por eso
va primero: es el único de los dos que sirve como detector. Fixtures: un log
`cp1252` con acentos **y** uno `utf-8` válido, para anclar que cada uno se
recupera intacto.

### Precondiciones que T2 debe resolver antes de tocar el veredicto

**1. T2 contradice el SOP en dos puntos, y el SOP es canónico.**
[`../../../sky_claw/local/AGENTS.md`](../../../sky_claw/local/AGENTS.md) §2.9 punto
3 **codifica el propio Gap A**:

> *"Success is decided by exit code AND artifact at the `-o:` root … AND **no error
> lines** in `Logs/{Tool}_{modo}_log.txt`"*

y manda lo contrario de la regla de completitud que T2 quiere introducir:

> *"A missing or unreadable log is a **warning, not a failure**: the hard gate is
> the artifact at the `-o:` root"*

El PR de T2 **debe enmendar §2.9 punto 3 junto con el código**. Enmendarlo antes
deja al SOP describiendo código inexistente; no enmendarlo deja drift
doc-vs-código — el modo de falla que ese archivo existe para atajar.

> **Por qué el punto 1 sí se enmendó ya y el punto 3 no** (los dos son §2.9, así
> que la asimetría hay que justificarla). El **punto 3 describe el comportamiento
> actual del código**: hoy el veredicto realmente exige "no error lines", y
> cambiar el texto antes que el código lo vuelve una descripción falsa. El
> **acceptance gate del punto 1 no describe código: es una regla de proceso para
> quien aprueba** ("do NOT approve or merge until…"), y ya estaba falsificada por
> evidencia mergeada en #463 — le alcanzaba con que el log ecoara la ruta, que es
> exactamente lo que el preset satisface sin escribir un solo archivo ahí. Dejarla
> como estaba no era esperar a T2: era conservar un blocker que se puede cumplir
> con un chequeo insuficiente. Se endureció a las tres condiciones (log + archivos
> + sin desvío por preset) en este mismo cambio.

El propio §2.9 punto 3 invita a apretar el criterio: *"there is no completeness
marker other than the log … Anyone with a real rig: confirm when `DynDOLOD.esp` is
written, and this criterion can be tightened."* El rig del 2026-08-10 **lo midió**
(t3 > t4) — informe fuera del repo y una sola corrida, así que entra como premisa
a re-verificar con la fixture commiteada, **no como hecho cerrado** (misma lectura
que la fila `U-06` del inventario; decir "lo confirmó" contradiría ese registro).

Y no alcanza para tachar la frase del SOP: §2.9 dice *"an assumption nobody has
verified **against the binary**"*, y eso sigue siendo cierto — el rig observó
*comportamiento* una vez, que es evidencia más débil que el análisis del binario.
Las dos afirmaciones conviven; la del SOP no está vencida y por eso no se toca acá.

**2. Hay una exención de ancla acoplada a esa misma regla.** En
`tests/test_dyndolod_service.py:3452-3463`, los registros `dyndolod_log_ausente` y
`dyndolod_log_ilegible` están exentos de emitir etapa **fundándose textualmente en
§2.9 punto 3** (*"El SOP §2.9 punto 3 lo declara explícitamente: la ausencia del
log es advertencia, no fallo"*). Si T2 cambia esa semántica, la premisa de las dos
exenciones muere: hay que re-justificarlas o quitarlas del dict, no dejarlas
citando una regla derogada.

**3. T2 no puede relajar el conjunto artefacto+frescura.** Ver la sección del
preset más abajo: es lo único que hoy contiene ese defecto. Un completion marker
tratado como suficiente reconstituye el falso verde entero.

### Anclas que T2 debe traer

- **Igualdad literal** del dict `herramienta → marcadores de completitud`. Una
  herramienta nueva sin marcador rompe el test. Es el instrumento que
  `AGENTS.md` señala como el que sí funciona: enumerar, no muestrear.
- Declarar todo clasificador nuevo en
  `test_la_familia_del_post_check_esta_congelada`
  (`tests/test_dyndolod_service.py:2356-2385`), cuya igualdad literal romperá a
  propósito.
- **Fixtures de log reales commiteadas al árbol.** No es un adorno: los informes
  de rig viven en la máquina del operador y un maintainer no puede auditarlos
  (hallazgo de Codex en #463). Las fixtures son lo que convierte evidencia externa
  en evidencia auditable. Mínimo: éxito de TexGen, éxito de DynDOLOD, cierre
  mid-run (101 errores, **sin** marcador), y los dos logs de encoding de arriba.
- **Un caso que aísle la falta de marcador, y no solo el cierre mid-run.** El
  fixture del cierre mid-run trae 101 errores *y* ausencia de marcador, así que
  cae por el gate de terminales **aunque la implementación nunca mire la
  completitud**: una mutación que borre el conjunto `AND completion marker`
  seguiría verde y el ancla no lo notaría. Hace falta un caso limpio —`rc == 0`,
  artefacto fresco, log sin ninguna línea terminal— al que **solo** le falte el
  marcador, y que se exija REJECT. Es el único que hace fallar al conjunto nuevo
  de forma independiente.
- **La fixture del éxito de DynDOLOD va COMPLETA, con las 121 líneas `Error:`
  reales.** No un extracto de las seis no-fatales conocidas. La regla "todo
  `Error:` que no esté en la lista es terminal" solo es segura si está demostrado
  que las 121 líneas medidas caen dentro de la lista, y eso **no está establecido
  hoy**: la lista salió del §11.4 del informe, que enumera categorías, no las 121
  ocurrencias. Una fixture recortada a las seis categorías esperadas pasa el test
  y deja pasar una séptima categoría que en producción se clasifica terminal — el
  mismo falso rojo que T2 viene a eliminar, ahora con un test verde encima.
  **Precondición de T2: volcar las 121 líneas contra la taxonomía y anclar que el
  log real completo clasifica como éxito.** Si aparece una categoría no prevista,
  la decisión de ampliar la lista se toma con la línea real a la vista, no a mano.

### La regla que T2 puede violar

Los marcadores son **distintos por herramienta**. Cablear el de DynDOLOD y dejar
TexGen sin el suyo es la forma #2 de `AGENTS.md` — hermano en el mismo archivo — y
acá los dos lanzadores son literalmente el mismo par de líneas (`:526` y `:637`).
Enunciado como propiedad del mecanismo:

> **Un veredicto solo es sound si TODA herramienta lanzada por este runner declara
> su marcador de completitud y su artefacto.**

## T3-v2 — staging y salida (rebase requerido)

El diseño de v1 sigue en pie: subcarpeta por herramienta, gate de TexGen sobre su
subárbol `textures`, y revisión de **todas** las superficies que tocan la raíz
administrada — rollback, preflight, journal/recovery, packaging y VFS — no solo el
post-check.

**Nomenclatura, porque acá se cuela un off-by-one-nivel.** Con el layout nuevo hay
DOS raíces y no son intercambiables:

- `<raíz administrada>` — la que hoy devuelve `dyndolod_output_target`, compartida;
- `<raíz de herramienta>` = `<raíz administrada>/TexGen` y
  `<raíz administrada>/DynDOLOD` — **lo que viaja en el `-o:` de cada una**.

El gate de artefacto de TexGen es entonces
**`<raíz administrada>/TexGen/textures`**, no `<raíz administrada>/textures`:
TexGen escribe directo en la raíz de SU `-o:`, así que sus texturas caen un nivel
más adentro. Escribirlo con la raíz compartida deja el chequeo un nivel arriba y
hace fallar cerradas las corridas buenas — el mismo falso rojo que T3 viene a
eliminar. La firma de frescura se toma sobre ese mismo subárbol.

Lo que cambió desde v1:

**El preset es una restricción de diseño, no una nota al pie.** El rig midió que
`Presets\DynDOLOD_SSE_TexGen.ini` guarda `OutputPath=` y que la GUI pre-carga el
campo Output desde ahí, no desde el argv: las escrituras reales van al path del
preset con exit 0, y **el log sigue ecoando el `Using Output Path:` del argv**.

**El gate de frescura es lo que hoy contiene ese defecto** — verificado contra el
código por Codex en #463 y registrado en
[`../../pending_ooda_status.md`](../../pending_ooda_status.md) (sección del
preset): si las escrituras van a otro lado, la firma del candidato administrado no
cambia y el post-check marca artefacto rancio → `success=False`. La corrida falla
cerrada, no en silencio.

**Pero la contención es más chica de lo que suena, y hay que decir cuánto.** El
`_package_output_as_mod` que no corre es **el de TexGen, y solo ese**
(`dyndolod_runner.py:1042`, condicionado a `texgen_result.success`).
`run_full_pipeline` **no corta** ahí: sigue a `run_dyndolod` incondicionalmente
(`:1088`) y, si DynDOLOD sale bien, **empaqueta su salida a `mods/`**
(`:1094-1100`). El pipeline reporta `success=False` porque la fórmula exige
`texgen_mod_path` cuando `run_texgen` (`:1160-1167`), pero para entonces ya se
escribió un mod en `mods/`, y limpiarlo depende del rollback del servicio de
afuera, no del gate. La afirmación de que "`_package_output_as_mod` nunca corre"
—que este documento arrastraba del registro de #463— es falsa como enunciado
general y T3 no puede apoyarse en ella: **hay dos call sites de empaquetado y hay
que trazar los dos** antes de decidir el alcance de rollback.

**Y la contención que sí existe es accidental.** Como el candidato de TexGen
apunta a `<raíz administrada>/TexGen_Output` (Gap C), TexGen falla el gate
*siempre*, por el motivo equivocado. Cuando T3 corrija el candidato a
`<raíz administrada>/TexGen/textures`, la contención pasa a ser real **y
portante**: a partir de ese momento, relajar artefacto+frescura reabre el agujero
del preset. Es la razón por la que T3 no puede implementarse como estaba escrito
en v1.

**Consecuencia de ordenamiento:** T3 no arranca hasta que el ítem del preset tenga
al menos decidida la precedencia `preset` vs `-o:` (paso 1 de su lista de
investigación previa). Diseñar el layout de staging sin saber quién gana es
diseñar sobre una premisa abierta.

Se conserva de v1, sin cambios: el alcance de Gap B (Zip & Exit reporta
`exito_no_empaquetable`, aceptar el `.zip` como artefacto queda fuera), y el
invariante TexGen → DynDOLOD (copiar a `MO2/mods` no demuestra activación ni
visibilidad; si falla, el trabajo es investigar el perfil de MO2, no parchear T3).

## T5-v2 — rig E2E (aceptación endurecida)

v1 aceptaba *"el log declara `Using Output Path:` igual a la raíz administrada"*.
**Ese criterio ya no sirve**: el rig probó que el encabezado ecoa el argv mientras
las escrituras van al preset. La aceptación pasa a exigir las cuatro cosas:

El checklist va **completo y explícito**, no por referencia: v1 no está en el repo,
así que "se conservan los criterios de v1" no le sirve a nadie que quiera ejecutar
esto. Son diez, y ninguno es opcional:

**Cómo se corre**

1. las dos herramientas se lanzan **por el runner de Sky-Claw**, no por un harness
   de rig — es lo que hace que la corrida pruebe el camino de producción;
2. la raíz administrada **contiene espacios**, porque es la rama que siempre corre
   en un rig real (`Skyrim Special Edition`, `My Games`) y la única donde el
   quoting puede romperse.

**Qué se exige del lanzamiento**

3. argv correcto (cerrado por #462);
4. `Using Output Path:` igual a la subcarpeta administrada de cada herramienta;
5. **archivos efectivamente creados dentro de esa subcarpeta** — no basta el
   encabezado del log;
6. **ninguna escritura desviada por preset**, con un preset rancio presente a
   propósito, y verificado en **TexGen Y DynDOLOD**. El rig del hallazgo corrió
   DynDOLOD *sin* preset: su asimetría no está verificada, solo no observada, y
   `Presets\DynDOLOD_SSE_Default.ini` persiste su propia clave `OutputPath` por el
   mismo mecanismo. Cerrar esto verificando solo TexGen es el patrón que
   `AGENTS.md` nombra como el más repetido del repo.

**Qué se exige del resultado**

7. cerrando con **`Save and Exit`**, las dos corridas dan `success=True`; TexGen
   deja su subárbol `textures` y DynDOLOD su `DynDOLOD.esp`, **sin pisarse**;
8. una corrida cerrada con `Zip and Exit` se reporta `exito_no_empaquetable`, no
   un rojo genérico de artefacto ausente;
9. `_package_output_as_mod` produce **dos** mods de MO2 con contenidos disjuntos.

**El invariante que distingue copiar de que la herramienta lea**

10. con `run_texgen=True`, el log de DynDOLOD **no** trae `No TexGen output
    detected` ni `LOD billboard(s) not found`, y los billboards que TexGen acaba de
    generar están **bajo la ruta que ese log declara como Data Path**. Copiar a
    `MO2/mods` no demuestra activación ni visibilidad: un mod presente pero no
    habilitado en el perfil —o sobrescrito por otro— es invisible para el `-d:`.
    Si este criterio falla, el trabajo que sigue es investigar el perfil de MO2
    (**T6**), no parchear T3.

Sin (1)–(2) una corrida parcial satisface el resto sin haber probado nada del
camino real.

## T7 y T8 — sin cambios de alcance

**T7 — preflight de modales.** Dos de los tres modales son prechequeables sin GUI
(KnownFolder de Downloads contra la ruta del exe; `version.ini` de Resources
exigiendo 3.00); el tercero necesita detección de ventana. Va después de T5-v2:
hasta que haya una corrida verde no hay contra qué medir un falso positivo del
preflight.

**T8 — preset / UI Automation.** Depende de T7 y de T5-v2. Razón adicional que v1
no tenía: automatizar la GUI con presets persistentes **antes** de resolver
`OutputPath` es automatizar precisamente el comportamiento incorrecto.

## Ordenamiento

```text
#483 (lifecycle)  ──►  T2 clasificación del log
                            │
                            ▼
                       F-002 concurrencia OperationJournal
                            │
                            ▼
                       #466 fail_operation
                            │
                            ▼
                       T3-v2 staging/output ◄── requiere precedencia del preset
                            │
                            ▼
                       T5-v2 rig E2E autoritativo
                            │
                            ▼
                       T7 preflight  ──►  T8 UIA
```

**Por qué T2 primero.** Es el único bloque cuya superficie no toca lifecycle,
recovery ni journal: vive en `dyndolod_runner.py` y `tests/test_dyndolod_service.py`.
El PR #483 toca `db_lifecycle.py`, `async_registry.py` y
`test_db_lifecycle_quarantine.py`
— cero solapamiento, por archivo y por contrato.

**Por qué T3 espera.** No porque vaya a editar lifecycle, sino porque su propio
alcance exige revisar rollback, journal/recovery, persistencia del target y
reanudación — y esas son las superficies que #483, F-002 y #466 están
estabilizando. Además necesita la precedencia del preset ya decidida.

**Regla de rama:** la rama de implementación de T2 se crea desde `main` **fresco,
después del merge de #483**, no desde el estado actual.

## Auditabilidad de la evidencia

Los dos informes de rig que fundamentan este roadmap —
`INFORME_RIG_DYNDOLOD_ALPHA209.md` (2026-08-10) e
`INFORME_T5_ARGV_DYNDOLOD_ALPHA209.md` (2026-08-11) — **no están en este repo**:
viven en la máquina del rig operador. Un maintainer no puede reproducirlos desde
el árbol. Quien retome cualquiera de estos bloques debería pedir los informes o
reproducir la corrida en su propio rig antes de tomar decisiones de diseño
apoyado solo en esta descripción.

Ese es el motivo por el que las fixtures de log de T2 son un requisito y no una
comodidad: son el único mecanismo disponible para trasladar evidencia de rig al
árbol, donde CI la puede verificar.
