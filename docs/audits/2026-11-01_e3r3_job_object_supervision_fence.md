# E3-R3 — Gate del Job Object: el worker puede crear descendientes antes de estar supervisado

> **Carácter:** evidencia fechada (ver [README](README.md)). Lo medido el 2026-11-01 se conserva;
> lo que `main` superó está marcado **`[SUPERADO]`**. El estado vigente vive en
> [`pending_ooda_status.md`](../pending_ooda_status.md).

## Actualización 2026-11-08 (reconciliación de #854 con `main`) `[EJECUTADO EN LINUX]`

El hueco que este informe midió **está cerrado en `main`** y sus tests ya no lo caracterizan:

| Lo que este informe proponía o medía | Dónde quedó resuelto |
|---|---|
| Un mensaje de supervisión entre `launch_ack` y el despacho (el «fence mínimo») | #856: el broker envía `supervised` solo tras un `launch_ack` de ESE job y el worker lo espera antes de crear el nieto. [Informe](2026-11-02_fence_de_supervision_del_broker.md) |
| El matiz onefile: el hijo Python del bootloader podía quedar fuera del Job | #860/#872: el broker pide un Job con nombre aleatorio (`generar_nombre_de_job`), el bridge lo crea ANTES del proceso y el worker comprueba que vive en ESE Job antes de hablar con el broker. [Informe](2026-11-03_identidad_del_job_onefile.md) |
| Descendientes creados antes de la asignación | #877 (P1): el bridge adopta a los descendientes antes del `launch_ack` y emite `worker_exit` solo con el Job vacío. [Informe](2026-11-03_identidad_del_job_onefile.md), §12 |

`tests/test_vfs_supervision_fence.py` **cambió de signo**, como este informe lo exigía («el fence no puede
aterrizar sin invertir su propia evidencia»): la caracterización del hueco pasó a **regresión de la barrera** y la
propiedad deseada pasó a test normal, sin `xfail`. En el ensayo de la reconciliación, con el contrato nuevo, la
caracterización original fallaba con `assert [] == ['probe', 'handler']` (el worker ya no despacha sin supervisión)
y la propiedad deseada pasaba; con el `xfail` estricto puesto habría sido XPASS, es decir, rojo.

**El HOLD NO se levanta por esto.** `_CONTENCION_DEL_WORKER_VERIFICADA` sigue en `False`: que la contención esté
implementada y probada con dobles y laboratorios no es lo mismo que estar verificada con el binario final y
aceptada por el auditor independiente (ver «Cuándo se levanta (y cuándo NO)»).

## Veredicto original del gate (2026-11-01) `[SUPERADO en lo que afirma del protocolo; el HOLD sigue vigente]`

**Veredicto del gate: `WIRING_SECURITY_HOLD`.** El protocolo de entonces **no** impedía que el
worker creara descendientes antes de que el bridge lo asignara al Job Object, y esa propiedad
no podía demostrarse con el código de entonces. Este documento fijó qué se midió, qué no, y el
fence mínimo propuesto. **No implementaba el fence**: es un cambio de protocolo y requería una
decisión explícita (lo aterrizaron después #856, #860/#872 y #877).

**Estado de la barrera de runtime:** el HOLD ya no es sólo prosa. Está aplicado en código —ver
«Barrera de runtime» más abajo— y `dyndolod_backend = "brokered"` devuelve un guard fail-closed.

Alcance de la evidencia original (2026-11-01): lectura del código + pruebas deterministas sobre el
`VfsExecutionBroker` real con un bridge simulado en Linux. **No se ejecutó** MO2, USVFS,
TexGen, DynDOLOD ni `vfs-health`; los tests con Job Object real de Windows no corrieron
acá. La actualización de arriba también se ejecutó solo en Linux: los tests de Windows se saltan y no cuentan como PASS.

## Qué garantiza ADR 0007 y qué no

[ADR 0007](../adr/0007-mo2-broker-usvfs.md) afirma: *"Worker y descendientes quedan
contenidos en un Win32 Job Object `KILL_ON_JOB_CLOSE`"*. Eso es cierto **para los procesos
creados después de que el worker fue asignado al Job**. Un proceso que el worker haya creado
antes queda fuera: Windows no adopta hijos previos al asignar un proceso a un Job, así que
sobrevive a `TerminateJobObject` y al cierre del Job, y el broker no se entera
(`worker_exit` solo reportaba al worker; **`[SUPERADO]`** desde #877 solo se emite con el Job vacío).
La semántica de Windows es la misma; lo que cambió es que el bridge ya no la ignora (adopción, más abajo).

## Secuencia al 2026-11-01 (leída del código) `[SUPERADA: ver la secuencia vigente más abajo]`

| # | Quién | Qué | Dónde |
|---|---|---|---|
| 1 | bridge | `organizer.startApplication(...)` — el worker **nace corriendo** | `BridgeLaunchController.launch` (`plugin_bundle/skyclaw_bridge/runtime.py`) |
| 2 | bridge | `job_object.assign(handle)` — recién acá queda supervisado | ídem |
| 3 | bridge | emite `launch_ack` (solo si el paso 2 no falló) | ídem |
| 4 | worker | conecta al broker y envía `hello` | `run_worker_session` (`vfs_worker.py`) |
| 5 | broker | responde `hello_ack` **sin esperar al bridge** | `VfsExecutionBroker._handle_connection` |
| 6 | worker | `execute_worker_manifest`: attestation → **proceso nieto** → handler de la herramienta | `vfs_worker.py` |

`launch_ack` tiene un único consumidor en el broker, que solo lo reenvía como evento
(`_handle_bridge_message`). No existe ningún mensaje de "supervisado": nada ordena el paso 6
después del paso 2. La ventana entre 1 y 2 es en la práctica corta (el bridge asigna en el
mismo callback que lanza, y el worker tarda en arrancar), pero **no está acotada por el
protocolo**.

Matiz inferido, **no verificado**: `sky_claw.spec` declara un `EXE(...)` con `a.binaries` y
`a.datas` y sin `COLLECT`, lo que indicaría un ejecutable PyInstaller *onefile*. En ese modo
el bootloader es el proceso que MO2 lanza y el intérprete real corre en un proceso hijo
propio; si ese hijo naciera antes de la asignación, ni el worker real quedaría en el Job. No
se construyó el binario (restricción de la tarea), así que no se puede afirmar el orden.
**`[SUPERADO]`** El orden con el ejecutable congelado se midió después (informe de identidad del Job, §4–§5, en
`windows-latest`) y el matiz quedó cubierto por P2 y P1.

## Secuencia vigente (reconciliada con `main`) `[LEÍDO]`

| # | Quién | Qué | Dónde |
|---|---|---|---|
| 1 | broker | pide el Job con un nombre aleatorio de 128 bits y lo envía en `launch_worker` (`job_object`) | `vfs_job_identity.generar_nombre_de_job`, `VfsExecutionBroker` |
| 2 | bridge | crea el Job con ESE nombre ANTES del proceso; si ya existía, falla y no nace nada | `BridgeLaunchController.launch` (`plugin_bundle/skyclaw_bridge/runtime.py`) |
| 3 | bridge | `organizer.startApplication(...)`: el worker **nace corriendo** (la ventana es inherente) | ídem |
| 4 | bridge | `job_object.assign(handle)` | ídem |
| 5 | bridge | `job_object.adopt_descendants(handle)`: contiene a los descendientes que ya nacieron; si no puede con alguno, **no hay `launch_ack`** y se termina al worker | ídem |
| 6 | bridge | emite `launch_ack` (con `job_object`) | ídem |
| 7 | worker | verifica que él y sus descendientes vivos están en ESE Job, antes de importar nada pesado; si no, sale con 72 sin abrir un socket | `vfs_arranque.arrancar_worker`, `vfs_job_identity.verificar_identidad_del_job` |
| 8 | worker | `hello` con `job_object` y la capacidad `supervision_fence` | `run_worker_session` (`vfs_worker.py`) |
| 9 | broker | envía `supervised` solo si hubo un `launch_ack` de ESE job con el MISMO nombre | `_autorizar_worker_si_corresponde` (`vfs_broker.py`) |
| 10 | worker | `_esperar_supervision`; recién después `execute_worker_manifest`: attestation → proceso nieto → herramienta | `vfs_worker.py` |
| 11 | bridge | al salir el proceso lanzado, termina y confirma el Job vacío; `worker_exit` **solo** con el Job vacío | `_monitor` (`runtime.py`) |

Lo que esta secuencia **no** cubre (límites declarados por los informes de #860 y #877): un **huérfano** cuyo
padre murió antes de ser observado no es alcanzable por la adopción; la capa `ctypes` de la adopción se probó con
dobles y la corre el workflow de Windows.

## Evidencia

`tests/test_vfs_supervision_fence.py` (estado tras la reconciliación; Linux):

| Test | Qué prueba |
|---|---|
| `test_el_doble_del_job_implementa_todo_el_protocolo_del_bridge` | ancla: el doble cubre todo `JobObject` (protocolo congelado por igualdad literal; un método nuevo rompe el test en vez de dejar un doble viejo) |
| `test_el_bridge_asigna_y_adopta_en_el_job_object_antes_de_emitir_launch_ack` | `job → startApplication → assign → adopt → launch_ack` y el Job nace con el nombre pedido |
| `test_si_la_contencion_en_el_job_falla_no_hay_launch_ack_y_se_termina_al_worker` (×2: `assign`, `adopt`) | un fallo en cualquiera de los dos pasos no emite `launch_ack` y termina al worker |
| `test_regresion_de_la_barrera_el_worker_no_despacha_sin_que_el_bridge_confirme_la_supervision` | sin `launch_ack` el worker sigue esperando: ni nieto ni herramienta. Era la **caracterización del hueco** |
| `test_propiedad_el_worker_no_crea_descendientes_hasta_estar_supervisado` | nada antes del ack; `probe` → `handler` después. Era el `xfail(strict=True)` |

Poder discriminante verificado por mutación al reconciliar (mutantes temporales sobre el árbol mergeado, ya
revertidos; sin mutar, 6 passed):

- bridge sin `adopt_descendants` (`adopted = 0`) → mueren el test de secuencia y el caso `adopt`.
- broker que autoriza sin exigir el `launch_ack` (se quita `job_id not in self._launch_acked` de
  `_autorizar_worker_si_corresponde`) → mueren la regresión de la barrera y la propiedad.
- worker que se salta `_esperar_supervision` → mueren esas mismas dos.

Los mutantes del 2026-11-01 (`launch_ack` antes de `assign`, `assign` fallido tragado) **no se re-ejecutaron** al
reconciliar. La barrera con el protocolo completo (`launch_ack` ajeno, duplicado, tardío, cancelación, reconexión
del bridge, capacidad de fence) tiene su propia batería en `tests/test_vfs_fence_de_supervision.py`, y la adopción en
`tests/test_mo2_bridge_adopcion.py`; este archivo conserva la pregunta original del gate con el contrato vigente.

El test feliz `test_vfs_worker.py::test_worker_reporta_resultado_por_su_canal_autenticado` **`[SUPERADO]`** ya no es
evidencia de lo contrario: antes completaba sin enviar `launch_ack` y hoy lo envía.

## Qué NO queda demostrado `[actualizado 2026-11-08]`

- Que la ventana se pierda alguna vez en un rig real. Es una propiedad de orden, no
  una medición de frecuencia; no se afirma "riesgo nulo" ni "riesgo alto". La latencia entre `startApplication` y
  `assign` la midió otra sesión en un MO2 sandbox (informe de identidad del Job, §11.1; no reproducida aquí, y no con
  el MO2 productivo ni bajo carga).
- Con el MO2 productivo y bajo carga, y con un token no elevado: `[NO VERIFICADO]` (informe de identidad del Job, §6 y §11.5).
- Que el bridge instalado en MO2 coincida con el código del repo (se leyó el código, no el artefacto instalado).
  Importa más que antes: el protocolo del bridge cambió (Job con nombre, adopción, `worker_exit` con Job vacío).

## Impacto si ocurre (al 2026-11-01)

Un descendiente fuera del Job (nieto de la attestation o proceso de la herramienta) puede
seguir vivo y escribiendo en el output administrado después de que el daemon dé el árbol por
terminado y habilite un rollback. El veto de rollback por terminalidad desconocida cubre la
terminalidad **del worker**, no la de procesos fuera de su Job. **`[SUPERADO en parte]`** Desde #877, un
descendiente vivo fuera del Job que no se puede contener ni terminar se reporta como terminación fallida y pone la
instancia en cuarentena (informe de identidad del Job, §12); el costo es que esa instancia queda aislada hasta
intervención manual.

## Fence mínimo propuesto el 2026-11-01 `[IMPLEMENTADO en #856; el texto original se conserva]`

Principio: *el worker no crea ningún descendiente hasta que el daemon le confirma que el
bridge ya lo asignó al Job.* Se apoya en un mensaje que el bridge instalado **ya emite**
(`launch_ack`, solo tras un `assign` exitoso):

1. **Bridge:** sin cambios (`launch_ack` ya cumple el contrato; ver los dos primeros tests).
   **`[SUPERADO]`** el bridge sí cambió después: Job con nombre creado antes del proceso y adopción de
   descendientes antes del `launch_ack` (#860/#872 y #877), porque `launch_ack` probaba «se asignó el proceso que
   lanzó MO2» y no «todo el árbol está contenido».
2. **Broker:** al recibir `launch_ack` de un `job_id` pendiente lo registra como
   *supervisado* y envía al worker de ese job un mensaje autenticado `supervised`
   (`{protocol_version, type, job_id}`). Si el worker conecta **después** del `launch_ack`,
   el `supervised` se envía al aceptar su `hello`. Un `bridge_error` de `launch_worker` jamás
   lo envía.
3. **Worker:** tras `hello_ack` y **antes** de `execute_worker_manifest` (que crea el nieto de
   la attestation), espera `supervised` con un timeout acotado; sin él termina con
   `VfsWorkerBootstrapError` sin haber creado descendientes.
4. **Compatibilidad:** mensaje aditivo; el daemon y el worker se distribuyen juntos
   (`SkyClawApp.exe --vfs-worker`) y el bridge no se toca, de modo que no exige reinstalar el
   plugin. A decidir: gatear la espera con un campo firmado del manifiesto para tolerar un
   worker más viejo. **`[SUPERADO]`** se resolvió con una capacidad: el worker declara `supervision_fence` en su
   `hello` y el broker rechaza al que no la declara (`tests/test_vfs_fence_de_supervision.py`). Y como el bridge
   también cambió, hay que actualizar el bundle instalado en MO2 (ver «Cuándo se levanta»).
5. **Tests a invertir en el mismo PR:** la caracterización (pasa a rojo) y el `xfail` estricto
   (se le quita la marca). **`[HECHO]`** en la reconciliación de #854 con `main` (ver la actualización de arriba).

Alternativas descartadas: crear el proceso suspendido (MO2 `startApplication` no lo expone);
`IsProcessInJob` desde el worker (no distingue *este* Job de uno heredado de MO2); una espera
temporal (ADR 0007 rechaza explícitamente resolver carreras con esperas).

Residual aun con el fence: no cubre procesos creados por el propio arranque del ejecutable
antes de que corra código de Sky-Claw (el matiz onefile); eso requeriría verificar el
orden con el binario real. **`[SUPERADO]`** lo midieron el informe de identidad del Job (con el `.exe` congelado
en `windows-latest`) y la verificación temprana de `vfs_arranque.py`; lo que sigue sin demostrarse está en
«Cuándo se levanta (y cuándo NO)».

## Barrera de runtime (`WIRING_RUNTIME_HOLD_ENFORCED`)

`resolve_dyndolod_spawn_strategy` (`sky_claw/local/mo2/brokered_dyndolod.py`) evalúa la constante
interna `_CONTENCION_DEL_WORKER_VERIFICADA` (literal `False`) **antes** de construir la strategy.
Mientras valga `False`, pedir `dyndolod_backend = "brokered"` devuelve
`VfsRequiredDynDOLODSpawnStrategy` con un motivo explícito que nombra el `WIRING_SECURITY_HOLD`, el
Job Object y este documento:

* aun con broker de la instancia correcta y todas las raíces válidas, la strategy real **no se
  construye** (tampoco la attestation), el broker **no recibe** ninguna sesión (`open_session` /
  `submit`) y **no hay subprocess local**: ni TexGen ni DynDOLOD se lanzan, y no hay fallback a
  standalone;
* `standalone` conserva su comportamiento (`None`) y un valor de backend inválido conserva su propio
  guard («no es válido»), que se evalúa antes;
* la constante es **interna**: no es una clave de `config.toml`, ni una variable de entorno, ni un
  parámetro del resolver. Sólo los tests del cableado la levantan, por `monkeypatch`;
* la GUI y el agente LLM comparten UN servicio de etapa 9 (el del composition root), así que una sola
  barrera cubre las dos superficies.

Anclas (`tests/test_dyndolod_brokered_hold.py`): igualdad literal del valor y de su forma (literal del
módulo), censo de quién lee/toca la constante en producción y en los tests, censo de la cadena de
llamadores que alcanzan la strategy real (`BrokeredDynDOLODSpawnStrategy` ← `build_brokered_…` ←
`_construir_brokered` ← resolver ← `AppContext`), orden (la barrera va antes de construir) y la grilla
de TODAS las entradas del resolver.

### Cuándo se levanta (y cuándo NO)

**No pasa a `True` por fusionar ninguno de los PR de contención.** La pila ya está en `main`: el fence de
supervisión (#856), la identidad del Job (#860/#872) y la adopción de descendientes (#877). Eso cierra el hueco
que originó el HOLD **en el código y con los dobles de Linux y los laboratorios del sandbox `windows-latest`**,
pero todavía falta:

1. **Evidencia sobre el binario final** (el `.exe` que se distribuye) en un sandbox Windows con procesos
   inocuos, que cubra también lo creado antes de la verificación y deje registrado su SHA-256 (el informe de
   identidad del Job, §11.5, declara que el `.exe` de `1eafe1c5`, usado en el censo y en los 23 tests, no tiene
   SHA-256 registrado).
2. **El visto bueno del auditor independiente.** El HOLD solo lo levanta él, con evidencia propia (informe de
   identidad del Job, §12).
3. **Decidir cómo se actualiza el bundle del bridge instalado en MO2.** El protocolo del bridge cambió y el
   artefacto instalado no se verificó.
4. **Decidir la política de teardown** ante la salida del bootloader (la cola lenta y la cuarentena espuria
   1/24; informe de identidad del Job, §11.2 y §12), que hoy sigue siendo «terminar de inmediato».

Se levanta en un PR aparte, con revisión adversarial, cambiando únicamente la constante y sus anclas. Hasta
entonces la única forma de correr la etapa 9 es `dyndolod_backend = "standalone"`.

## Reproducir

```bash
.venv/bin/python -m pytest tests/test_vfs_supervision_fence.py -v
.venv/bin/python -m pytest tests/test_dyndolod_brokered_hold.py tests/test_dyndolod_brokered_wiring.py -q
```
