# E3-R3 — Gate del Job Object: el worker puede crear descendientes antes de estar supervisado

**Veredicto del gate: `WIRING_SECURITY_HOLD`.** El protocolo vigente **no** impide que el
worker cree descendientes antes de que el bridge lo asigne al Job Object, y esa propiedad
no puede demostrarse con el código actual. Este documento fija qué se midió, qué no, y el
fence mínimo propuesto. **No implementa el fence**: es un cambio de protocolo y requiere una
decisión explícita.

Alcance de la evidencia: lectura del código + pruebas deterministas sobre el
`VfsExecutionBroker` real con un bridge simulado en Linux. **No se ejecutó** MO2, USVFS,
TexGen, DynDOLOD ni `vfs-health`; los tests con Job Object real de Windows no corrieron
acá.

## Qué garantiza ADR 0007 y qué no

[ADR 0007](../adr/0007-mo2-broker-usvfs.md) afirma: *"Worker y descendientes quedan
contenidos en un Win32 Job Object `KILL_ON_JOB_CLOSE`"*. Eso es cierto **para los procesos
creados después de que el worker fue asignado al Job**. Un proceso que el worker haya creado
antes queda fuera: Windows no adopta hijos previos al asignar un proceso a un Job, así que
sobrevive a `TerminateJobObject` y al cierre del Job, y el broker no se entera
(`worker_exit` solo reporta al worker).

## Secuencia real (leída del código)

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

## Evidencia

`tests/test_vfs_supervision_fence.py`:

| Test | Resultado | Qué prueba |
|---|---|---|
| `test_el_bridge_asigna_al_job_object_antes_de_emitir_launch_ack` | verde | `startApplication → assign → launch_ack`: `launch_ack` sería una señal de supervisión válida |
| `test_si_la_asignacion_al_job_falla_no_hay_launch_ack_y_se_termina_al_worker` | verde | un `assign` fallido no emite `launch_ack` y termina al worker |
| `test_caracterizacion_hoy_el_worker_despacha_sin_que_el_bridge_confirme_la_supervision` | verde | **el hueco existe**: con un bridge real-protocolo que retiene `launch_ack`, el worker crea el nieto y despacha la herramienta igual (`["probe", "handler"]`) |
| `test_propiedad_deseada_el_worker_no_crea_descendientes_hasta_estar_supervisado` | `xfail(strict=True)` | la propiedad que falta; pasa a XPASS (rojo) cuando exista el fence |

Poder discriminante verificado por mutación (mutantes temporales, ya revertidos):

- `launch_ack` emitido **antes** de `assign` → muere el primer test.
- `assign` fallido tragado → muere el segundo.
- worker que **espera** una confirmación (fence simulado) → la caracterización pasa a rojo y
  el `xfail` estricto a XPASS (rojo). Es decir: **el fence no puede aterrizar sin invertir su
  propia evidencia**.

El test feliz ya existente (`test_vfs_worker.py::test_worker_reporta_resultado_por_su_canal_autenticado`)
nunca envía `launch_ack` y el job completa: es evidencia independiente de lo mismo.

## Qué NO queda demostrado

- Que la ventana se pierda alguna vez en un rig real. Es una propiedad de orden, no
  una medición de frecuencia; no se afirma "riesgo nulo" ni "riesgo alto".
- El orden real de creación de procesos con el ejecutable congelado (ver matiz onefile).
- Que el bridge instalado coincida con el código del repo (se leyó el código, no el
  artefacto instalado).

## Impacto si ocurre

Un descendiente fuera del Job (nieto de la attestation o proceso de la herramienta) puede
seguir vivo y escribiendo en el output administrado después de que el daemon dé el árbol por
terminado y habilite un rollback. El veto de rollback por terminalidad desconocida cubre la
terminalidad **del worker**, no la de procesos fuera de su Job.

## Fence mínimo propuesto (NO implementado)

Principio: *el worker no crea ningún descendiente hasta que el daemon le confirma que el
bridge ya lo asignó al Job.* Se apoya en un mensaje que el bridge instalado **ya emite**
(`launch_ack`, solo tras un `assign` exitoso):

1. **Bridge:** sin cambios (`launch_ack` ya cumple el contrato; ver los dos primeros tests).
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
   worker más viejo.
5. **Tests a invertir en el mismo PR:** la caracterización (pasa a rojo) y el `xfail` estricto
   (se le quita la marca).

Alternativas descartadas: crear el proceso suspendido (MO2 `startApplication` no lo expone);
`IsProcessInJob` desde el worker (no distingue *este* Job de uno heredado de MO2); una espera
temporal (ADR 0007 rechaza explícitamente resolver carreras con esperas).

Residual aun con el fence: no cubre procesos creados por el propio arranque del ejecutable
antes de que corra código de Sky-Claw (el matiz onefile); eso requeriría verificar el
orden con el binario real.

## Reproducir

```bash
.venv/bin/python -m pytest tests/test_vfs_supervision_fence.py -v -rxX
```
