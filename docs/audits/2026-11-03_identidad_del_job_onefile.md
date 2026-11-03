# Identidad del Job y contención del worker PyInstaller onefile (P2)

> **Fecha:** 2026-11-03 · **Rama:** `feat/vfs-onefile-job-identity` (PR #860, apilado sobre #856).
> **Base de partida:** `claude/vfs-supervision-fence` `dbf0cdcee8aba1a3bb85e58cd708e6c64770dd09`; la base avanzó después a
> `e8106a03` (otra sesión trajo `origin/main` a #856) y se incorporó por `merge --no-ff` (`807e6f69`).
>
> **Alcance:** `sky_claw/local/mo2/vfs_job_identity.py` (nuevo), `vfs_broker.py`, `vfs_worker.py`, el `runtime.py` del plugin
> del bridge y el laboratorio de Windows. No toca #854, #856, el bridge instalado, el rig, los backups ni el Work Root.
> `_CONTENCION_DEL_WORKER_VERIFICADA` (#854) **sigue en `False`**.
>
> **Estado epistémico por afirmación:** `[EJECUTADO]` (corrido; se dice dónde), `[LEÍDO]` (verificado leyendo código),
> `[INFERENCIA]`, `[NO VERIFICADO]`.
>
> **Carácter:** evidencia fechada (ver [README](README.md)). Lo vigente vive en
> [`sky_claw/local/mo2/AGENTS.md`](../../sky_claw/local/mo2/AGENTS.md).

## 0. Estado de este informe

**EN CURSO — veredicto pendiente.** Hasta que el run de `windows-latest` sobre el árbol final cierre las secciones 5 y 6, el
dictamen es **`ONEFILE_CONTAINMENT_HOLD`**. Esta versión contiene el diseño, la medición exacta del arranque del `.exe` congelado
(§4, ya ejecutada) y la verificación local; **no** afirma que la contención esté demostrada.

## 1. El problema que deja abierto #856

El fence de #856 prueba **«el bridge asignó el proceso que lanzó MO2»**. En un ejecutable PyInstaller *onefile* ese proceso es el
**bootloader**; Python corre en un **hijo** que el bootloader crea con `CreateProcess`. Si el hijo nace **antes** del
`AssignProcessToJobObject`, queda **fuera** del Job (un proceso hijo creado antes de la asignación no se adopta), el `launch_ack`
llega igual y la herramienta corre sin contención. #856 lo midió y lo dejó en HOLD con tres propuestas (P1 adopción en el
bridge, P2 Job con nombre verificado por el worker, P3 onedir).

Este trabajo investiga **P2** y, sobre todo, la compuerta que el brief fija para aceptarla: **qué nace antes de que corra la
verificación y si eso queda contenido o terminado de forma demostrable.**

## 2. Decisiones de diseño (cada una con su amenaza)

| Pregunta | Decisión | Por qué / qué cierra | Prueba |
|---|---|---|---|
| Identidad del Job y `job_id` | El **broker** elige por launch un nombre inadivinable `Local\SkyClawVfs-<128 bits>` en `_registrar_job` y lo guarda por `job_id`. | `submit` y `open_session` pasan por `_registrar_job`: las dos superficies lo heredan **por construcción**. Un nombre = un launch: no hay reutilización. | `test_toda_herramienta_pide_un_job_con_nombre_valido[*]`, `test_cada_launch_recibe_un_nombre_distinto`, ancla `test_el_nombre_del_job_nace_en_un_unico_punto_del_broker` |
| Un nombre predecible no prueba autenticidad | El nombre **no** es el secreto. La identidad se prueba por (a) creación exclusiva por el bridge, (b) el eco del nombre en el `launch_ack` y en el `hello`, comparado por el broker, (c) membresía **del propio proceso** en ESE handle. | Un nombre filtrado (el `argv` es visible para el mismo usuario) no permite suplantar nada: sin la membresía no hay prueba, y sin el HMAC no hay `hello`. | `test_un_worker_con_otra_identidad_es_rechazado...`, `test_un_ack_sin_la_identidad_pedida_no_autoriza_al_worker` |
| ACL del Job | DACL protegida `D:P(A;;0x4;;;OW)(A;;0x4;;;BA)`: solo `JOB_OBJECT_QUERY` para propietario y administradores. El creador conserva su handle con acceso total. | Nadie más puede terminarlo, **cambiarle los límites** (quitar `KILL_ON_JOB_CLOSE` después de verificado) ni **asignarle procesos ajenos** para aparentar pertenencia. Defensa en profundidad: no es una frontera frente a un proceso del mismo usuario con inyección de código. | `test_el_job_nativo_se_abre_para_consulta_y_rechaza_terminar_cambiar_limites_o_asignar` (Windows) |
| Colisión de nombres / squatting | `ERROR_ALREADY_EXISTS` ⇒ se cierra el handle y **se falla**: jamás se adopta el Job de otro. Otro tipo de objeto con el nombre ⇒ también falla. El Job se crea **antes** de `startApplication`. | Si falla, no nace ningún proceso. | `test_crear_dos_veces_el_mismo_nombre...`, `test_un_job_preexistente_de_un_squatter_no_se_adopta`, `test_un_objeto_de_otro_tipo...`, `test_si_no_se_puede_crear_el_job_con_ese_nombre_no_nace_ningun_proceso` |
| Suplantación / Job equivocado | `IsProcessInJob(<proceso>, <handle de ESE Job>)`, **nunca `NULL`**. Medido en #856: contra `NULL` el SO responde `TRUE` para pertenecer a **cualquier** Job. | Un worker en un Job ajeno (el de MO2, el del runner) no pasa. | `test_estar_en_un_job_ajeno_no_basta_aunque_is_process_in_job_con_null_diga_que_si`, ancla `test_la_pertenencia_nunca_se_pregunta_contra_un_job_nulo_en_produccion` |
| Autenticación del vínculo bridge↔worker | No hay canal directo: el nombre viaja broker→bridge (HMAC), bridge→worker (`argv`) y vuelve worker→broker (HMAC) y bridge→broker (HMAC). El broker exige que **ambos** ecos coincidan con lo que pidió. | Un `argv` alterado produce otro nombre ⇒ el broker rechaza. La autenticidad del worker sigue siendo el HMAC del descriptor: **P2 prueba contención, no autoría.** | los dos tests anteriores |
| Falla al abrir o consultar | Todo `Exception` de la API es `JobIdentityError`; un handle nulo se rechaza antes de llamar al SO. | Ni una excepción inesperada sale como prueba. | `test_cualquier_falla_al_consultar...[*]` (16 combinaciones), `test_un_nombre_valido_de_un_job_que_no_existe...` (Windows) |
| Worker fuera del Job | `worker_main` termina a sus descendientes vivos y sale con **72** sin abrir un socket. | No hay `hello`, ni nieto de attestation, ni herramienta. | `test_worker_que_no_prueba_su_identidad...`, `test_el_entrypoint_real_del_worker_fuera_del_job_sale_con_72...` (Windows) |
| Handle retenido | El verificador **cierra** su handle en éxito y en cualquier falla. | `KILL_ON_JOB_CLOSE` solo dispara al cerrarse el **último** handle: un worker que retuviera el suyo impediría que el cierre del Job del bridge mate el árbol. Se mide con un control negativo. | `test_el_worker_no_retiene_el_handle...`, `test_control_negativo_un_handle_retenido...` (Windows) |
| Cancelación y limpieza | Antes de la verificación: `cancel` ⇒ `TerminateJobObject` mata al worker (código 1). Después de verificado y antes de autorizado: ídem. Tras un fallo de identidad: el worker limpia a sus descendientes. | — | `test_cancelar_antes_de_la_verificacion...`, `test_cancelar_despues_de_verificarse...` (Windows) |

## 3. Qué cambia en el código (y qué no)

* **No** cambia `VFS_PROTOCOL_VERSION` (1). La compatibilidad se resuelve por **rechazo explícito**, nunca por degradación silenciosa:
  un bridge viejo no acepta `job_object` en `launch_worker` (`campos no permitidos`) y por eso no puede confirmar un launch P2; un
  broker viejo no manda el campo y el bridge nuevo lo exige; un worker viejo no declara `job_object` y el broker lo rechaza.
* Broker: nombre por launch en `_registrar_job`; `launch_worker` lo lleva; `launch_ack` y `hello` deben nombrarlo.
  **Hermano corregido:** `_descartar_apertura_sin_driver`, un segundo camino que suelta un job, no descartaba el estado del fence de #856
  (`_launch_acked`, `_supervised_sent`); ahora lo hace junto con la identidad, y una ancla enumera todo sitio que haga `_pending.pop`.
* Bridge (`runtime.py`): `LaunchRequest.job_object`, `--job-object` en el `argv`, `job_factory(nombre)`, `Win32JobObject(nombre)`, eco
  en el `launch_ack`.
* Worker: `--job-object`, verificación **antes** de abrir cualquier socket, `job_identity` obligatorio en `run_worker_session`,
  `job_object` en el `hello`.
* Módulo nuevo `vfs_job_identity.py` (solo stdlib + `ctypes` + `psutil`, sin importar `sky_claw.*`; mypy estricto).

## 4. Compuerta crítica: ¿qué nace ANTES de la verificación? `[EJECUTADO]`

Instrumento: `scripts/windows_onefile_census.py`. A diferencia del laboratorio de #856 (muestreo cada 10 ms), cuenta con el **puerto de
completion del Job** —el kernel avisa de cada proceso que entra— y con `TotalProcesses`; el instrumento se validó antes contra una verdad
conocida (nieto creado antes/después del `assign`, cierre del handle) en Windows real. Entorno: `windows-latest`, Python 3.11.9, commit
`0e511727`, run `41550197236`. Procesos inocuos: el `.exe` real con `--vfs-probe-child` (lee un archivo y devuelve su hash). 3 repeticiones por fila.

`SkyClawApp.exe` real (`pyinstaller sky_claw.spec`):

| retardo del `assign` (ms) | procesos DENTRO del Job (min–max) | FUERA del Job (min–max) | `TotalProcesses == IOCP` |
|---:|---:|---:|---:|
| 0 | 6–6 | 0–0 | 3/3 |
| 500 | 6–6 | 0–0 | 3/3 |
| 1000 | 6–6 | 0–0 | 3/3 |
| 2000 | 1–6 | 0–5 | 3/3 |

Imágenes del árbol, sobre las 12 corridas: `skyclawapp.exe` ×24, `cmd.exe` ×24, `conhost.exe` ×24 ⇒ **por arranque: 2 `SkyClawApp.exe`
(bootloader + hijo Python), 2 `cmd.exe` y 2 `conhost.exe` = 6 procesos.**

Lectura (lo medido, no lo deseado):

1. El arranque del `.exe` congelado crea **seis** procesos antes de llegar al código de verificación: el bootloader, el hijo Python y
   **dos `cmd.exe` con su `conhost.exe`**. Con `assign` ≤ 1 s los seis entran al Job y `TotalProcesses` coincide con el IOCP en 12/12.
2. Con `assign` a 2 s hay corridas con **hasta cinco procesos fuera del Job**: el hijo Python **y sus cuatro auxiliares** escapan juntos.
   La carrera existe en el `.exe` real, no solo en el ejecutable mínimo.
3. **Origen de los `cmd.exe /c "ver"`** (cerraba el `[NO VERIFICADO]` de #856 §4.3): el audit hook los atribuye a la fase
   **`import sky_claw`** —el `__init__` del paquete—, anterior a `main()` y a `worker_main`. Dos creaciones, ambas con `cmd.exe /c "ver"`.
   La pila completa quedó para el siguiente run (el resumen de este run solo imprimía el último marco, `subprocess.py`).
4. **Defecto de este censo, declarado:** la columna «supervivientes» salió 0 en todas las filas y **no mide la terminación**: el censo
   esperaba a que el árbol terminara solo antes de terminar el Job, así que lo escapado ya había muerto. Se corrigió
   (`--terminar-tras`: terminar el Job con el árbol **vivo**) y se vuelve a medir en el run final (§5).
5. La traza de WMI no estuvo disponible en ninguna fila (`wmi = NO`); el motivo se imprime ahora en el resumen. Por eso lo «fuera del Job»
   de esta tabla sale del muestreo de 5 ms: un proceso efímero **fuera** del Job puede no haberse visto. `[NO VERIFICADO]` hasta ver el motivo.

Ninguna de estas cifras es una demostración: son una caracterización de la geometría (cuánto tarda el hijo en nacer frente a cuánto
tarda el `assign`) en un runner compartido. La garantía de P2 se argumenta **por construcción** (§6) y estas mediciones la contrastan.

## 5. Evidencia de P2 en Windows `[PENDIENTE DEL RUN]`

Se completa con el resultado del run de `windows-latest` sobre el árbol final: tests Windows con procesos reales, e2e de la carrera con
`assign` a tiempo y tardío (Python de desarrollo y `.exe` congelado), censo con terminación del Job con el árbol vivo.

## 6. Argumento de contención y límites `[PENDIENTE DE CERRAR CON §5]`

Se completa con el dictamen. Límites ya conocidos: un descendiente **huérfano** (su padre murió antes del barrido) no es alcanzable por la
relación padre→hijo; P2 convierte la carrera de seguridad en una de **disponibilidad** (un `assign` más lento que el arranque aborta el
launch en lugar de dejar un worker sin contención); la latencia real `startApplication`→`assign` de MO2 no se midió.

## 7. Verificación local `[EJECUTADO]` (Linux; los tests de Windows se saltan y NO cuentan como PASS)

* `ruff check` y `ruff format --check` sobre `sky_claw/ scripts/ tests/`: limpios.
* `mypy --platform win32 sky_claw/ scripts/ --ignore-missing-imports` (la plataforma del job de CI): **0 errores**, igual que la base de #856.
* Mutación: **20 mutantes del mecanismo, 20 muertos, 0 sobrevivientes** (script de mutación en el scratchpad de la sesión).
* Barrido amplio de las suites relacionadas (VFS, MO2, bridge, brokered, worker): 3624 passed, 84 skipped y 1 error de colección **preexistente**
  (`test_runtime_vault_operator_verifier.py`, símbolo solo-Windows; figura en la línea base).
