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

**Encoding:** leer cp1252 con fallback a utf-8 y `errors="replace"`.

### Precondiciones que T2 debe resolver antes de tocar el veredicto

**1. T2 contradice el SOP en dos puntos, y el SOP es canónico.**
[`../../../sky_claw/local/AGENTS.md`](../../../sky_claw/local/AGENTS.md) §2.9 punto
3 **codifica el propio Gap A**:

> *"Success is decided by exit code AND artifact at the `-o:` root … AND **no error
> lines** in `Logs/{Tool}_{modo}_log.txt`"*

y manda lo contrario de la regla de completitud que T2 quiere introducir:

> *"A missing or unreadable log is a **warning, not a failure**: the hard gate is
> the artifact at the `-o:` root"*

El PR de T2 **debe enmendar §2.9 junto con el código**. Enmendarlo antes deja al
SOP describiendo código inexistente; no enmendarlo deja drift doc-vs-código — el
modo de falla que ese archivo existe para atajar. El mismo §2.9 ya invita al
cambio: *"there is no completeness marker other than the log … Anyone with a real
rig: confirm when `DynDOLOD.esp` is written, and this criterion can be tightened."*
El rig del 2026-08-10 lo confirmó (t3 > t4).

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
  mid-run (101 errores, **sin** marcador), las 6 no-fatales con marcador presente,
  y un log cp1252 con acentos.

### La regla que T2 puede violar

Los marcadores son **distintos por herramienta**. Cablear el de DynDOLOD y dejar
TexGen sin el suyo es la forma #2 de `AGENTS.md` — hermano en el mismo archivo — y
acá los dos lanzadores son literalmente el mismo par de líneas (`:526` y `:637`).
Enunciado como propiedad del mecanismo:

> **Un veredicto solo es sound si TODA herramienta lanzada por este runner declara
> su marcador de completitud y su artefacto.**

## T3-v2 — staging y salida (rebase requerido)

El diseño de v1 sigue en pie: subcarpeta por herramienta (`<root>/TexGen`,
`<root>/DynDOLOD`), gate de TexGen sobre `root/textures`, y revisión de **todas**
las superficies que tocan la raíz administrada — rollback, preflight,
journal/recovery, packaging y VFS — no solo el post-check.

Lo que cambió desde v1:

**El preset es una restricción de diseño, no una nota al pie.** El rig midió que
`Presets\DynDOLOD_SSE_TexGen.ini` guarda `OutputPath=` y que la GUI pre-carga el
campo Output desde ahí, no desde el argv: las escrituras reales van al path del
preset con exit 0, y **el log sigue ecoando el `Using Output Path:` del argv**.

**El gate de frescura es lo que hoy contiene ese defecto** — verificado contra el
código por Codex en #463 y registrado en
[`../../pending_ooda_status.md`](../../pending_ooda_status.md) (sección del
preset): si las escrituras van a otro lado, la firma del candidato administrado no
cambia, el post-check marca artefacto rancio, `success=False` y
`_package_output_as_mod` nunca corre. La corrida falla cerrada, no en silencio.

**Pero hoy esa contención es accidental.** Como el candidato de TexGen apunta a
`root/TexGen_Output` (Gap C), TexGen falla el gate *siempre*, por el motivo
equivocado. Cuando T3 corrija el candidato a `root/textures`, la contención pasa a
ser real **y portante**: a partir de ese momento, relajar artefacto+frescura
reabre el agujero del preset. Es la razón por la que T3 no puede implementarse
como estaba escrito en v1.

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

1. argv correcto (cerrado por #462);
2. log correcto — `Using Output Path:` igual a la subcarpeta administrada;
3. **archivos efectivamente creados dentro del staging administrado**;
4. **ninguna escritura desviada por preset**, verificado con un preset rancio
   presente a propósito.

Y el criterio (4) se verifica en **TexGen Y DynDOLOD**. El rig del hallazgo corrió
DynDOLOD *sin* preset: su asimetría no está verificada, solo no observada, y
`Presets\DynDOLOD_SSE_Default.ini` persiste su propia clave `OutputPath` por el
mismo mecanismo. Cerrar el ítem verificando solo TexGen es el patrón que
`AGENTS.md` nombra como el más repetido del repo.

Se conservan de v1 los criterios de éxito con `Save and Exit`, staging disjunto,
`exito_no_empaquetable` con `Zip and Exit`, dos mods de MO2 con contenidos
disjuntos, y el invariante TexGen → DynDOLOD.

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
#483 toca `db_lifecycle.py`, `async_registry.py` y `test_db_lifecycle_quarantine.py`
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
