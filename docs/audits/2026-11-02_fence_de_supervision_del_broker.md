# Fence de supervisión del broker MO2/USVFS — carrera `launch_ack` / Job Object

> **Fecha:** 2026-11-02 · **Base:** `origin/main` `b0e30bbcd0ea5a165b166888c77f226a267b409e` ·
> **Rama:** `claude/vfs-supervision-fence`.
>
> **Alcance:** `sky_claw/local/mo2/vfs_broker.py`, `sky_claw/local/mo2/vfs_worker.py`, el protocolo
> daemon↔worker y la contención del worker (`SkyClawApp.exe --vfs-worker`) en el Job Object del bridge.
> No toca el bridge instalado, el rig, los backups ni el Work Root.
>
> **Estado epistémico por afirmación:** `[EJECUTADO]` (corrido en esta sesión; se dice dónde),
> `[LEÍDO]` (verificado leyendo código/docs), `[INFERENCIA]`, `[NO VERIFICADO]`.
>
> **Carácter:** evidencia fechada (ver [README](README.md)). Lo vigente vive en el
> [ADR 0007](../adr/0007-mo2-broker-usvfs.md) y en [`sky_claw/local/mo2/AGENTS.md`](../../sky_claw/local/mo2/AGENTS.md).

## 0. Veredicto

**`SUPERVISION_FENCE_SECURITY_HOLD`.**

| Pregunta | Respuesta | Estado |
|---|---|---|
| ¿El worker puede crear un nieto o despachar una herramienta antes de la confirmación del bridge? | **No.** Se agregó el mensaje `supervised`; sin él el worker aborta sin crear nada. | `[EJECUTADO]` tests deterministas + e2e con worker real (Linux y Windows) |
| ¿Un `launch_ack` de otro job, duplicado, tardío o tras un `assign` fallido autoriza algo? | **No.** Cada caso tiene su test y sus mutantes. | `[EJECUTADO]` |
| ¿Queda demostrado que el proceso Python **efectivo** del worker pertenece al Job del bridge? | **No.** Con `assign` inmediato (7–10 ms) lo observamos dentro del Job en todos los casos, pero el protocolo no acota esa demora y con demoras ≥ 200 ms (exe mínimo) / ≥ 2 s (exe real) el hijo Python queda **fuera** con frecuencia. | `[EJECUTADO]` en `windows-latest`; la cota superior real de MO2 `[NO VERIFICADO]` |
| ¿Se puede recomendar uso real? | **No** hasta cerrar la identidad del proceso (§6). | — |

El fence es una condición **necesaria** (impide el despacho prematuro) pero **no suficiente** para la
contención del proceso efectivo bajo PyInstaller *onefile*. Por eso el dictamen es HOLD y no READY.

## 1. El problema

Secuencia del bridge (`sky_claw/local/mo2/plugin_bundle/skyclaw_bridge/runtime.py`, `_launch`) `[LEÍDO]`:

```text
startApplication(worker)   → el worker nace CORRIENDO
job_object.assign(handle)  → AssignProcessToJobObject
launch_ack                 → sólo si assign tuvo éxito
```

Secuencia del worker antes del cambio (`run_worker_session`) `[LEÍDO]`:

```text
hello → hello_ack → execute_worker_manifest (nieto de la attestation, luego la herramienta)
```

Nada ordenaba `hello_ack` respecto de `launch_ack`. El worker podía crear su nieto y despachar la
herramienta **antes** de que el bridge lo asignara al Job. Un descendiente creado antes de la asignación
**no es adoptado** por el Job y sobrevive a `TerminateJobObject` (medido, §4.1).

## 2. Mecanismo (explícito, sobre el `launch_ack` existente)

Mensaje nuevo daemon→worker: `supervised {protocol_version, type, job_id}`. Autenticado con el HMAC del
canal como el resto.

| Pieza | Comportamiento |
|---|---|
| `hello` del worker | declara `"supervision_fence": true`; el broker **rechaza** (`VfsBrokerError`) a un worker que no lo declara. Sin fallback silencioso. |
| Broker | `_launch_acked` / `_supervised_sent` por `job_id`. `supervised` se envía **una vez**, sólo si hubo `launch_ack` **para ese job**, el job sigue pendiente, no está en `_termination_tasks` y el worker está conectado. |
| Orden ack/hello | `_registrar_launch_ack` (llega el ack) y `_autorizar_worker_si_corresponde` (tras `hello_ack`) llaman al mismo autorizador; se marca `_supervised_sent` **antes** del `await` para que sólo uno envíe. |
| `launch` fallido | `bridge_error` de launch → `_denegar_supervision`: descarta el ack y corta al worker. |
| Worker | `_esperar_supervision` devuelve `"supervised"` / `"cancelled"` o lanza `VfsWorkerBootstrapError` (plazo, EOF, otro `job_id`, tipo o versión inesperados). **Antes** de cualquier subproceso. |
| Plazo (30 s) | sólo **aborta**; jamás habilita. No hay espera temporal sustituyendo una garantía. |
| Limpieza | `_limpiar_registro` descarta ambos conjuntos con el job. |

**Compatibilidad `[LEÍDO]`:** `VFS_PROTOCOL_VERSION` y el `PROTOCOL_VERSION` del bridge siguen en 1 (el
bridge instalado compara igualdad; subirlo lo rompería). `launch_ack` existe en el bridge desde su primera
versión (#350) y en las cuatro versiones del historial se emite **después** de `assign`. El daemon y el
worker son el mismo ejecutable, así que la actualización es coordinada por construcción; un worker viejo
contra un broker nuevo es rechazado con error explícito. **El bridge instalado no cambia.**

## 3. Los 10 escenarios obligatorios → tests

Todos en `tests/test_vfs_fence_de_supervision.py` (arnés con broker real + bridge falso) salvo donde se
indica. Nombres en español, AAA.

| # | Escenario | Test |
|---|---|---|
| 1 | `launch_ack` antes / después del hello | `test_el_ack_autoriza_al_worker_llegue_antes_o_despues_de_su_hello[ack_antes_del_hello\|ack_despues_del_hello]` |
| 2 | Sin confirmación: cero nietos / herramientas | `test_el_worker_no_crea_nietos_ni_despacha_hasta_recibir_la_confirmacion`; e2e real: `test_el_worker_real_no_crea_descendientes_antes_del_ack_y_todos_quedan_en_el_job` |
| 3 | Ack de otro job | `test_un_launch_ack_de_otro_job_no_autoriza_a_este_worker`, `test_el_worker_rechaza_un_supervised_atribuido_a_otro_job` |
| 4 | Confirmación duplicada idempotente | `test_una_confirmacion_duplicada_entrega_un_solo_supervised`, `test_una_confirmacion_duplicada_no_despacha_dos_veces` |
| 5 | Falla `AssignProcessToJobObject` | `test_si_falla_la_asignacion_al_job_object_no_se_autoriza_la_ejecucion` |
| 6 | Timeout de supervisión → fail-closed | `test_el_timeout_de_supervision_falla_cerrado_sin_despachar` |
| 7 | Caída / desconexión del bridge | `test_la_caida_del_bridge_antes_de_la_confirmacion_no_autoriza`, `test_un_ack_tras_la_reconexion_del_bridge_autoriza_una_sola_vez` |
| 8 | Cancelación concurrente con confirmación tardía | `test_una_confirmacion_tardia_durante_la_cancelacion_no_autoriza_al_worker`, `test_un_worker_que_conecta_tarde_tras_la_cancelacion_no_es_autorizado` |
| 9 | Worker + descendientes en el Job correcto | `tests/test_vfs_job_containment_windows.py::test_el_bridge_real_contiene_al_worker_y_a_sus_descendientes_en_el_job_correcto` (Windows) |
| 10 | Compatibilidad LOOT / `vfs-health` / DynDOLOD | `test_toda_herramienta_del_broker_espera_la_misma_confirmacion[dyndolod\|health\|loot_sort\|texgen]` + `test_la_familia_de_herramientas_no_cambio_sin_revisar_el_fence` (igualdad literal de `ALLOWED_VFS_TOOL_IDS`) |

Anclas adicionales: `test_un_worker_sin_la_capacidad_de_fence_es_rechazado_por_el_broker`,
`test_el_estado_de_supervision_se_limpia_al_terminar_el_job`,
`test_el_protocolo_del_bridge_que_usa_este_e2e_es_el_de_produccion` (el bridge y el broker no divergen de
versión sin que el e2e lo note).

**Límite del escenario 10:** LOOT y DynDOLOD se verificaron por el camino del broker (todas las
herramientas pasan por la misma espera). **No** se ejecutó `vfs-health`, ni LOOT, ni TexGen, ni DynDOLOD
reales (restricción de la tarea).

## 4. Evidencia de Windows

Entorno: **GitHub Actions `windows-latest`**, Python 3.11.9 (workflow `.github/workflows/vfs-supervision-sandbox.yml`).
Se corrió **tres veces**; los números de §4.2 acumulan las tres:

| Run | Commit | Tests | Laboratorio |
|---|---|---|---|
| `41364300325` | `ee116c73` | 31 passed, 0 skipped | completo (la fila de 4000 ms del exe real **no es concluyente**, ver §4.2) |
| `41366217821` | `a476fc32` | 33 passed, 0 skipped | **abortó** en 4000 ms: el `assign` apuntó a una raíz ya terminada (`GetLastError=5`) y el script no lo contemplaba |
| `41367342228` | `55711087` | 35 passed, 0 skipped | completo, con el script corregido (medición nula / inconclusa) |

Es un sandbox efímero y aislado del rig; **no**
tiene MO2 ni USVFS `[LEÍDO]`: el "bridge" es el `BridgeLaunchController` real con un `organizer` falso que
lanza el worker real y un `Win32JobObject` real. Los procesos son inocuos (el worker sale tras el probe).

### 4.1 Semántica del Job Object (`tests/test_vfs_job_containment_windows.py`) `[EJECUTADO]`

| Hecho medido | Test |
|---|---|
| Un hijo creado **antes** de `AssignProcessToJobObject` queda fuera y sobrevive a `TerminateJobObject` | `test_un_descendiente_creado_antes_de_la_asignacion_queda_fuera_del_job` |
| Un hijo creado **después** queda contenido y muere con el Job | `test_un_descendiente_creado_despues_de_la_asignacion_queda_contenido_y_muere_con_el_job` |
| `IsProcessInJob(p, NULL)` responde `TRUE` para **cualquier** Job → **no sirve como prueba** | `test_is_process_in_job_sin_job_responde_true_para_un_job_ajeno` |
| Un proceso en un Job ajeno admite además el Job propio (jobs anidados) | `test_un_proceso_en_un_job_ajeno_admite_ademas_el_job_propio` |
| Un proceso puede consultar su propio Job y ver a su lanzador | `test_un_proceso_puede_consultar_su_propio_job_y_ver_a_su_lanzador` |
| Adopción de descendientes previos (prototipo `adoptar_descendientes`) los deja contenidos | `test_la_adopcion_de_descendientes_previos_los_deja_contenidos` |
| Con Job anidado, la consulta `QueryInformationJobObject(NULL)` ve `KILL_ON_JOB_CLOSE` si **algún** Job lo tiene | `test_en_jobs_anidados_el_proceso_ve_el_limite_de_su_job` → `{"solo_job_sin_kill_on_close": false, "solo_job_con_kill_on_close": true, "anidado_externo_sin_kill__interno_con_kill": true}` |
| Bridge real: worker + `--probe-child` dentro del Job correcto | `test_el_bridge_real_contiene_al_worker_y_a_sus_descendientes_en_el_job_correcto` |
| Bridge real con `assign` tardío: un descendiente queda **fuera** (caracterización del riesgo) | `test_con_una_asignacion_tardia_el_bridge_real_deja_un_descendiente_fuera_del_job` |

Resultado del último run (`55711087`): `35 passed in 26.63s`, **0 skipped** (los skips cuentan como
"no verificado", no como PASS). Incluye los 11 tests de Windows (9 de semántica/bridge real y 2 de higiene del
laboratorio), los 22 del fence (20 de protocolo + 2 anclas AST) y los 2 e2e con worker real.

### 4.2 PyInstaller *onefile*: ¿el bootloader crea un hijo antes del `assign`?

**Sí.** `SkyClawApp.exe` onefile = proceso bootloader (el que MO2 lanza y el bridge asigna) + un hijo
Python (el mismo exe) que ejecuta `--vfs-worker`. Se midió con `scripts/windows_job_probe.py`
(`lanzar_como_mo2` → retardo artificial entre la creación y `AssignProcessToJobObject` → ¿está el proceso
Python efectivo en **nuestro** Job? Se comprueba con `IsProcessInJob(proceso, <handle de NUESTRO Job>)`, nunca
con `NULL`). El «Python efectivo» es el descendiente más profundo que comparte imagen con el proceso lanzado
(en onefile el bootloader re-ejecuta el mismo `.exe` como hijo); el árbol se muestrea cada 10 ms, así que un
proceso más efímero que eso podría no verse. `[EJECUTADO]`

Un «Python efectivo» solo se cuenta si **se vio** un descendiente con la imagen del exe distinto de la raíz
(`efectiva_es_la_raiz` falso) y si la raíz seguía viva cuando llegó el `assign` (`raiz_ya_termino` falso). El
resto es **inconcluso**: ni a favor ni en contra.

Exe mínimo (onefile de juguete), 5 repeticiones por fila y por run; todas las filas concluyentes
(siempre 2 procesos). Acumulado de los 3 runs (15 muestras por fila):

| retardo del `assign` (ms) | Python efectivo en el Job (por run: ee116c73 / a476fc32 / 55711087) | acumulado | `assign` máx (ms) |
|---:|---|---:|---:|
| 0 | 5/5 · 5/5 · 5/5 | **15/15** | 10 |
| 50 | 5/5 · 5/5 · 5/5 | **15/15** | 53 |
| 200 | 1/5 · 0/5 · 0/5 | **1/15** | 206 |
| 500 | 1/5 · 0/5 · 0/5 | **1/15** | 503 |
| 1000 | 0/5 · 0/5 · 0/5 | **0/15** | 1004 |
| 2000 | 0/5 · 0/5 · 0/5 | **0/15** | 2003 |

`SkyClawApp.exe` real (`pyinstaller sky_claw.spec`, `--vfs-probe-child`), 3 repeticiones por fila y por run
(6 procesos en las filas concluyentes):

| retardo (ms) | Python efectivo en el Job (por run) | acumulado | `assign` máx (ms) |
|---:|---|---:|---:|
| 0 | 3/3 · 3/3 · 3/3 | **9/9** | 8 |
| 500 | 3/3 · 3/3 · 3/3 | **9/9** | 503 |
| 1000 | 3/3 · 3/3 · 3/3 | **9/9** | 1003 |
| 2000 | 2/3 · 0/3 · 1/3 | **3/9** | 2003 |
| 4000 | inconcluso · (abortó) · 0/0 concluyentes, 3 inconclusas | — | 4003 |

Lectura honesta:

1. El hijo Python **sí** puede nacer antes del `assign` y entonces queda fuera del Job. Con el exe real el
   resultado a 2 s **varía entre runs** (2/3, 0/3, 1/3): no es un umbral determinista, depende de la carga del
   runner. El exe real tarda más en arrancar (descomprime el onefile), por eso su umbral es más alto que el
   del exe mínimo.
2. Con `assign` inmediato (7–10 ms) el proceso Python efectivo estuvo dentro en **todas** las observaciones
   (15/15 y 9/9). Eso **no es una prueba**: son muestras de un runner compartido, y el protocolo no acota esa
   demora (AV, carga, USVFS en `startApplication` real). No se afirma seguridad absoluta a partir de ellas.
3. **Corrección de lo publicado antes:** la primera versión de este informe leía la fila de 4000 ms del exe
   real como «2/3». Era un artefacto: con esa demora el probe ya había terminado (el árbol tenía 1–2 procesos)
   y la raíz sola «en el Job» no dice nada sobre el hijo. La fila es **inconclusa** y no entra en ninguna
   conclusión; el laboratorio la marca así desde `a421d40a`.
4. La cota real de MO2 (`startApplication` + USVFS hasta devolver el HANDLE) **no se midió**: no hay MO2 en
   el sandbox.

### 4.3 ¿Qué crea el worker *antes* del fence? `[EJECUTADO]`

El e2e imprime `HALLAZGO descendientes previos al ack (arranque del worker)`. En Windows (los mismos dos procesos en los tres runs):
`{"7144": {"cmdline": "C:\\Windows\\system32\\cmd.exe /c ver", "en_job": true}, "7824": {…"cmd.exe /c ver", "en_job": true}}`.
Es decir, el **arranque** del intérprete (imports, anteriores al `hello`) crea dos `cmd.exe /c ver`. El fence
no los cubre —no puede: ocurren antes de que el worker hable con el broker—. En el run quedaron dentro del
Job porque el `assign` fue inmediato; con un `assign` tardío saldrían del Job (inocuos aquí, pero es la
misma ventana). En Linux se observó otro caso equivalente (importar `keyring` lanza `ldconfig`/`gcc`/`ld`).
El origen exacto en Windows **no se identificó** `[NO VERIFICADO]`. La aserción del e2e se restringe al
nieto de la attestation (`--probe-child`) y declara este límite.

## 5. Mutantes (TDD adversarial) `[EJECUTADO]`

10 mutantes sobre el broker y el worker; los 10 mueren (tras reforzar los tests por los sobrevivientes M3 y M6):

| Mutante | Qué rompe |
|---|---|
| M1 | el worker no espera la supervisión |
| M2 | el broker autoriza en el `hello` sin ack |
| M3 | el broker ignora la cancelación en curso (sobrevivió → `test_un_worker_que_conecta_tarde_tras_la_cancelacion_no_es_autorizado`) |
| M4 | un ack de job desconocido también se registra |
| M5 | sin idempotencia (reenvía siempre) |
| M6 | un launch fallido no deniega (sobrevivió → el test fija `supervision_timeout=30.0` para distinguir denegación de timeout) |
| M7 | el worker acepta `supervised` de otro job |
| M8 | el plazo vencido **prosigue** en vez de abortar |
| M9 | el broker acepta workers sin la capacidad |
| M10 | no se limpia el estado del job |

## 6. Por qué HOLD y mecanismo mínimo propuesto (no implementado)

**Hueco:** el fence demuestra «el bridge asignó el proceso que lanzó MO2» (el bootloader, en onefile), no
«el proceso que ejecuta Python está en el Job». Si el hijo Python nació antes del `assign`, el fence se
cumple igual (el `launch_ack` llega) y la herramienta corre fuera del Job.

Opciones, de menor a mayor invasividad:

- **P1 — adopción de descendientes previos en el bridge** (propuesta principal). Tras `assign(handle)`,
  enumerar los descendientes del PID asignado (snapshot Toolhelp), asignar cada uno que no esté en **nuestro**
  Job (`IsProcessInJob(h, hJobPropio)`, nunca `NULL`), repetir hasta punto fijo, y si alguno no puede
  adoptarse **no emitir `launch_ack`** y terminar el Job (fail-closed). Argumento de terminación: una vez que
  un proceso está en el Job, sus hijos futuros nacen dentro; sólo importan los hijos previos. El prototipo
  `adoptar_descendientes` (en `scripts/windows_job_probe.py`) pasa su prueba Windows, pero **requiere
  actualizar el bridge** (reinstalación coordinada) y **no se instaló** (restricción). Límites: descendientes
  huérfanos cuyo padre ya murió (la cadena de PIDs se rompe) y reutilización de PID.
- **P2 — Job con nombre + verificación en el worker.** El bridge crea el Job como `Local\SkyClawVfs-<job_id>`
  (rechazando `ERROR_ALREADY_EXISTS`) y el worker comprueba `IsProcessInJob(self, OpenJobObject(nombre))` antes
  de enviar el `hello`. Prueba **identidad** del Job, no sólo pertenencia a alguno. También requiere bridge
  nuevo. La autoverificación sin nombre (`QueryInformationJobObject(NULL)`) es insuficiente: §4.1 muestra que
  ve el límite de **cualquier** Job anidado.
- **P3 — empaquetar el worker como *onedir*.** Elimina el par bootloader/hijo: el proceso que lanza MO2 es el
  Python efectivo. No requiere bridge nuevo, pero cambia el empaquetado (`sky_claw.spec`) y **no** cubre los
  descendientes del arranque del intérprete (§4.3). Se descarta como único cierre.

Recomendación: P1 (o P2) como PR coordinado de bridge + daemon, validado en un sandbox Windows con procesos
inocuos y con la matriz de §4.2 repetida sobre el bridge nuevo, **antes** de cualquier uso real.

## 7. Qué NO se verificó

- MO2 / USVFS reales (`startApplication` real, su demora, su job propio si lo hubiera).
- La cota superior de la demora `startApplication`→`assign` en una máquina real cargada.
- El origen de los `cmd.exe /c ver` del arranque en Windows.
- LOOT / `vfs-health` / TexGen / DynDOLOD reales.
- Equivalencia de `windows-latest` con el rig del usuario (versión de Windows, antivirus).
- Que el bridge instalado (D1) se comporte como el del árbol: se leyó el código del árbol; **no** se inspeccionó el instalado.

## 8. Cómo reproducir

```bash
.venv/bin/python -m pytest tests/test_vfs_fence_de_supervision.py tests/test_vfs_fence_e2e_proceso_real.py -q
```

En Windows (sandbox aislado, procesos inocuos):

```bash
.venv/Scripts/python -m pytest tests/test_vfs_job_containment_windows.py tests/test_vfs_fence_de_supervision.py tests/test_vfs_fence_e2e_proceso_real.py -q
.venv/Scripts/python -m scripts.windows_job_probe --exe dist\SkyClawApp.exe --retardos 0,0.5,1,2,4 --repeticiones 3
```

El workflow `vfs-supervision-sandbox.yml` hace ambas cosas en `windows-latest` y sube los JSON como artefacto.
