# Identidad del Job y contención del worker PyInstaller onefile (P2)

> **Fecha:** 2026-11-03 · **Rama:** `feat/vfs-onefile-job-identity` (PR #860, apilado sobre #856).
> **Base de partida:** `claude/vfs-supervision-fence` `dbf0cdcee8aba1a3bb85e58cd708e6c64770dd09`; la base avanzó después a
> `e8106a03` (otra sesión trajo `origin/main` a #856) y se incorporó por `merge --no-ff` (`807e6f69`). No se rebasó nada.
>
> **Alcance:** `sky_claw/local/mo2/vfs_job_identity.py` (nuevo), `vfs_broker.py`, `vfs_worker.py`, el `runtime.py` del plugin
> del bridge y el laboratorio de Windows. No toca #854, #856, el bridge instalado, el rig, los backups ni el Work Root.
> `_CONTENCION_DEL_WORKER_VERIFICADA` (#854) **sigue en `False`**: levantarlo es una decisión aparte.
>
> **Estado epistémico por afirmación:** `[EJECUTADO]` (corrido; se dice dónde), `[LEÍDO]` (verificado leyendo código),
> `[INFERENCIA]`, `[NO VERIFICADO]`.
>
> **Carácter:** evidencia fechada (ver [README](README.md)). Lo vigente vive en
> [`sky_claw/local/mo2/AGENTS.md`](../../sky_claw/local/mo2/AGENTS.md).

## 0. Veredicto

**`ONEFILE_CONTAINMENT_READY_FOR_REVIEW`** — para revisión adversarial, **no** para merge ni para levantar la barrera de #854.

| Gate del brief | Respuesta | Estado |
|---|---|---|
| ¿El worker EFECTIVO se identifica contra el Job correcto (no `NULL`)? | **Sí.** `IsProcessInJob(<proceso>, <handle de ESE Job>)`. Un worker en un Job ajeno, fuera de todo Job, o en un Job anidado dentro de uno ajeno se resuelve bien; el caso trampa (`IsProcessInJob(p, NULL)` responde `TRUE` para cualquier Job) se mide y se rechaza. | `[EJECUTADO]` procesos reales, `windows-latest` |
| ¿Qué nace antes de la verificación? | **6 procesos**: bootloader, hijo Python, 2 `cmd.exe` y 2 `conhost.exe` (§4). Origen de los `cmd.exe /c "ver"`: `platform.win32_ver()` (§4). | `[EJECUTADO]` con el `.exe` real, conteo exacto por IOCP |
| ¿Quedan descendientes previos **sin supervisión y sin terminación demostrable**? | **No, para lo medido.** Con `assign` a tiempo todos están **en el Job**; con `assign` tardío el hijo Python queda fuera, **falla cerrado** antes de abrir un socket, y no sobrevive ningún proceso del árbol ni huérfano de la imagen del worker (§5). Límite declarado: un **huérfano** (su padre murió antes del barrido) no es alcanzable (§6). | `[EJECUTADO]` dev + `.exe` congelado |
| ¿Ninguna herramienta se despacha antes de una verificación exitosa? | **Sí.** Sin prueba no hay `hello`; sin el nombre correcto en `hello` **y** `launch_ack` el broker no envía `supervised`. | `[EJECUTADO]` |
| ¿Fail-closed ante cualquier falla? | **Sí.** Abrir, consultar, límites, descendientes, cerrar el handle, nombre inválido, nombre preexistente. | `[EJECUTADO]` (16 combinaciones de falla + reales) |
| ¿Cancelación antes y después de verificar? | **Sí**, con el worker real: antes de verificarse y después de verificarse pero antes de ser autorizado. | `[EJECUTADO]` |
| ¿Se verificó el ejecutable CONGELADO? | **Sí.** `SkyClawApp.exe` (PyInstaller onefile) con `assign` a tiempo (el job completa) y tardío (falla cerrado). | `[EJECUTADO]` `windows-latest` |

**Lo que este dictamen NO es:** una demostración matemática ni «seguro en producción». Las mediciones son de un runner compartido;
la garantía se argumenta **por construcción** (§6) y se contrasta con el sistema operativo real. La **latencia real de MO2** entre
`startApplication` y `assign` **no se midió** (no hay MO2 en el sandbox): P2 convierte la carrera de seguridad en una de
**disponibilidad** y su impacto depende de ese número (§6, §9).

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
| ACL del Job | DACL protegida `D:P(A;;0x4;;;OW)(A;;0x4;;;BA)`: solo `JOB_OBJECT_QUERY` para propietario y administradores. El creador conserva su handle con acceso total. | Nadie más puede terminarlo, **cambiarle los límites** (quitar `KILL_ON_JOB_CLOSE` después de verificado) ni **asignarle procesos ajenos** para aparentar pertenencia. Defensa en profundidad: no es una frontera frente a un proceso del mismo usuario con inyección de código. | `test_el_job_nativo_se_abre_para_consulta_y_rechaza_terminar_cambiar_limites_o_asignar` |
| Colisión de nombres / squatting | `ERROR_ALREADY_EXISTS` ⇒ se cierra el handle y **se falla**: jamás se adopta el Job de otro. Un nombre que ya tiene un Job con esta misma DACL devuelve `ACCESS_DENIED` (**medido**) y también se rechaza, con mensaje explícito. Otro tipo de objeto con el nombre ⇒ también falla. El Job se crea **antes** de `startApplication`. | Si falla, no nace ningún proceso. | `test_crear_dos_veces_el_mismo_nombre...`, `test_un_job_preexistente_de_un_squatter_no_se_adopta`, `test_un_objeto_de_otro_tipo...`, `test_si_no_se_puede_crear_el_job_con_ese_nombre_no_nace_ningun_proceso` |
| Suplantación / Job equivocado | `IsProcessInJob(<proceso>, <handle de ESE Job>)`, **nunca `NULL`**. Medido en #856: contra `NULL` el SO responde `TRUE` para pertenecer a **cualquier** Job. | Un worker en un Job ajeno (el de MO2, el del runner) no pasa. | `test_estar_en_un_job_ajeno_no_basta_aunque_is_process_in_job_con_null_diga_que_si`, ancla `test_la_pertenencia_nunca_se_pregunta_contra_un_job_nulo_en_produccion` |
| Autenticación del vínculo bridge↔worker | No hay canal directo: el nombre viaja broker→bridge (HMAC), bridge→worker (`argv`) y vuelve worker→broker (HMAC) y bridge→broker (HMAC). El broker exige que **ambos** ecos coincidan con lo que pidió. | Un `argv` alterado produce otro nombre ⇒ el broker rechaza. La autenticidad del worker sigue siendo el HMAC del descriptor: **P2 prueba contención, no autoría.** | los dos tests anteriores |
| Falla al abrir o consultar | Todo `Exception` de la API es `JobIdentityError`; un handle nulo se rechaza antes de llamar al SO. | Ni una excepción inesperada sale como prueba. | `test_cualquier_falla_al_consultar...[*]` (16 combinaciones), `test_un_nombre_valido_de_un_job_que_no_existe...` (Windows, `GetLastError=2`) |
| Worker fuera del Job | `worker_main` termina a sus descendientes vivos y sale con **72** sin abrir un socket. | No hay `hello`, ni nieto de attestation, ni herramienta. | `test_worker_que_no_prueba_su_identidad...`, `test_el_entrypoint_real_del_worker_fuera_del_job_sale_con_72...` (Windows) |
| Handle retenido | El verificador **cierra** su handle en éxito y en cualquier falla. | `KILL_ON_JOB_CLOSE` solo dispara al cerrarse el **último** handle: un worker que retuviera el suyo impediría que el cierre del Job del bridge mate el árbol. Se mide con un control negativo. | `test_el_worker_no_retiene_el_handle...`, `test_control_negativo_un_handle_retenido...` (Windows) |
| Cancelación y limpieza | Antes de la verificación: `cancel` ⇒ `TerminateJobObject` mata al worker (código 1). Después de verificado y antes de autorizado: el worker ve el `cancel` y sale solo (código 2) o `TerminateJobObject` llega primero (1) — **ambos medidos** y válidos. Tras un fallo de identidad: el worker limpia a sus descendientes. | — | `test_cancelar_antes_de_la_verificacion...`, `test_cancelar_despues_de_verificarse...` (Windows) |

## 3. Qué cambia en el código (y qué no)

* **No** cambia `VFS_PROTOCOL_VERSION` (1). La compatibilidad se resuelve por **rechazo explícito**, nunca por degradación silenciosa:
  un bridge viejo no acepta `job_object` en `launch_worker` (`campos no permitidos`) y por eso no puede confirmar un launch P2; un
  broker viejo no manda el campo y el bridge nuevo lo exige; un worker viejo no declara `job_object` y el broker lo rechaza.
  **Es una actualización coordinada de bridge y daemon** (el bridge instalado no se tocó).
* Broker: nombre por launch en `_registrar_job`; `launch_worker` lo lleva; `launch_ack` y `hello` deben nombrarlo.
  **Hermano corregido:** `_descartar_apertura_sin_driver`, un segundo camino que suelta un job, no descartaba el estado del fence de #856
  (`_launch_acked`, `_supervised_sent`); ahora lo hace junto con la identidad, y una ancla enumera todo sitio que haga `_pending.pop`.
* Bridge (`runtime.py`): `LaunchRequest.job_object`, `--job-object` en el `argv`, `job_factory(nombre)`, `Win32JobObject(nombre)` (se
  **reutiliza** `Win32JobObject`; no hay un segundo sistema de supervisión), eco en el `launch_ack`.
* Worker: `--job-object`, verificación **antes** de abrir cualquier socket, `job_identity` obligatorio en `run_worker_session`,
  `job_object` en el `hello`.
* Módulo nuevo `vfs_job_identity.py` (solo stdlib + `ctypes` + `psutil`, sin importar `sky_claw.*`; mypy estricto, lo que se comprobó
  plantando un error de tipos a propósito).
* **No se implementó P1** (adopción de descendientes en el bridge): P2 resultó suficiente para el criterio del brief con las
  mediciones de §4–§5 (justificación en §6).

## 4. Compuerta crítica: ¿qué nace ANTES de la verificación? `[EJECUTADO]`

Instrumento: `scripts/windows_onefile_census.py`. A diferencia del laboratorio de #856 (muestreo cada 10 ms) cuenta con el **puerto de
completion del Job** —el kernel avisa de cada proceso que entra— y con `TotalProcesses`; el instrumento se validó antes contra una verdad
conocida (nieto creado antes/después del `assign`, cierre del handle) en Windows real. Entorno: `windows-latest`, Python 3.11.9. Procesos
inocuos: el `.exe` real con `--vfs-probe-child` (lee un archivo y devuelve su hash). 3 repeticiones por fila. Run `41601455721` (commit `2cb47030`).

`SkyClawApp.exe` real (`pyinstaller sky_claw.spec`), censo exacto:

| retardo del `assign` (ms) | procesos DENTRO del Job | FUERA del Job | `TotalProcesses == IOCP` |
|---:|---:|---:|---:|
| 0 | 6–6 | 0–0 | 3/3 |
| 500 | 6–6 | 0–0 | 3/3 |
| 1000 | 6–6 | 0–0 | 3/3 |
| 2000 | 1–1 | **5–5** | 3/3 |

Imágenes del árbol, sobre las 12 corridas: `skyclawapp.exe` ×24, `cmd.exe` ×24, `conhost.exe` ×24 ⇒ **por arranque: 2 `SkyClawApp.exe`
(bootloader + hijo Python), 2 `cmd.exe` y 2 `conhost.exe` = 6 procesos.**

Lectura (lo medido, no lo deseado):

1. El arranque del `.exe` congelado crea **seis** procesos antes de llegar al código de verificación. Con `assign` ≤ 1 s los seis entran al
   Job y `TotalProcesses` coincide con el IOCP en 12/12 corridas de los dos runs completos del censo.
2. Con `assign` a 2 s, **el hijo Python y sus cuatro auxiliares escapan juntos** (5 fuera, 1 dentro: el bootloader). La carrera existe en el
   `.exe` real, no solo en el ejecutable mínimo. Con el laboratorio de #856 (misma geometría, otra métrica) el resultado a 2 s **varía entre
   runs** (3/3, 2/3, 1/3, 0/3 con el hijo Python dentro): no hay un umbral determinista, depende de la carga del runner.
3. **Origen de los `cmd.exe /c "ver"`** (cerraba el `[NO VERIFICADO]` de #856 §4.3). El audit hook los atribuye a la fase **`import sky_claw`**,
   anterior a `main()` y a `worker_main`, y **ambos son `platform.win32_ver()`** (la biblioteca estándar consultando la versión de Windows):
   (a) el import de `jaraco.context` (dependencia transitiva de `keyring`) llama `platform.system()` a nivel de módulo; (b) `sky_claw/config.py::_load_from_keyring`
   → `keyring` detecta su backend (`_data_root_Windows`) → `platform.win32_ver()`. No es código que pida un proceso a propósito. Cada `cmd.exe` trae su `conhost.exe`.
4. **Terminar el Job con el árbol vivo** (`--terminar-tras`; lo corrige el defecto de un censo previo, cuya columna «supervivientes» no medía la
   terminación). Con el `.exe` real el probe termina solo en pocos segundos, así que los escapados **no sobreviven** el plazo de gracia
   (supervivientes 0); con el `.exe` mínimo (duerme 6 s) un escapado **sobrevivió** a `TerminateJobObject` en uno de los dos runs y no en el otro.
   Que a veces no sobreviva puede deberse a que el bootloader PyInstaller acota a su hijo con un Job propio `[INFERENCIA, NO VERIFICADO]`. **No se
   usa como garantía**: la demostración determinista de que un hijo creado antes del `assign` queda fuera y sobrevive es
   `test_un_nieto_creado_antes_de_asignar_queda_fuera_y_sobrevive_a_la_terminacion` (Windows real).
5. **La traza de WMI no está disponible en este runner**: se suscribe pero no entrega eventos (`eventos totales=0`, tras un proceso canario creado a propósito).
   `[NO VERIFICADO]`: no se enumeran individualmente los procesos efímeros nacidos **fuera** del Job. Lo que sí se prueba es que **ninguno sigue vivo**
   (muestreo de 5 ms + barrido de la tabla de procesos por imagen, §5): un proceso de vida < 5 ms que ya murió no es un residuo.

Ninguna de estas cifras es una demostración: caracterizan la geometría (cuánto tarda el hijo en nacer frente a cuánto tarda el `assign`).

**Defecto del instrumento hallado en la revisión (corregido).** `medir_censo` tomaba el origen del reloj (`t0`) **antes** de montar la traza de WMI. Cuando WMI se suscribe pero
no entrega eventos (el caso de este runner, §4.5) la autoprueba espera su plazo entero (~8 s) y ese tiempo se descontaba de `ventana_s`: con la ventana por defecto (12 s) quedaban ~3 s, y
con `ventana_s=6` el bucle de observación no corría ni una vez, de modo que los escapados faltaban del muestreo y de los «supervivientes» justo cuando WMI no podía verlos. También inflaba
`asignado_ms`. Ahora el origen se fija **después** de montar la traza y los `t_ms` de WMI se rebasan a él (`test_la_ventana_de_observacion_cuenta_desde_el_lanzamiento...`: rojo `0 == 12`
antes, verde después). **Alcance, leyendo las banderas del workflow `[LEÍDO]`:** las cifras de esta sección no dependían de la ventana recortada: el censo del `.exe` real se corre con `--ventana 40`
(~29 s útiles tras la autoprueba) o con `--terminar-tras` (el límite es `ahora + N`, no `t0 + ventana`), y el censo del ejecutable mínimo con `--ventana 14 --terminar-tras 1.5`
(límite ≈ `t0 + 12,4`, margen estrecho pero sin recorte). El único caso con recorte real era el test de WMI (`ventana_s=6`), que se **salta** cuando la traza no entrega eventos, así que no producía un PASS falso.
Los resultados se vuelven a medir con el instrumento corregido en la run del CI del commit que lo corrige.

## 5. Evidencia de P2 en Windows `[EJECUTADO]`

Entorno: `windows-latest`, Python 3.11.9, run del sandbox `41601455721` (commit `2cb47030`; `contents: read`, sin publicación; procesos inocuos).

**Sandbox, Python de desarrollo: `215 passed, 5 skipped, 0 failed`.** Los 5 skips son declarados y **no cuentan como PASS**: la traza de WMI no
entrega eventos (1), los 2 tests del `.exe` congelado en el paso donde no se define `SKYCLAW_FROZEN_EXE` (se corren aparte), y 2 propios de la plataforma
(tests de Linux que no aplican en Windows). Incluye los 17 tests de `test_vfs_job_identity_windows.py` y los del e2e (procesos y Job reales).

**Sandbox, `.exe` CONGELADO (`SKYCLAW_FROZEN_EXE=dist\SkyClawApp.exe`): `6 passed`:**

| Escenario | Qué se afirma | Resultado |
|---|---|---|
| `assign` a tiempo, `.exe` congelado | el job **completa** (`success=True`, `exit_code=0`); **ningún descendiente observado está fuera del Job** (aserción `en_job is False` ⇒ vacío, y al menos uno medido `True`); sin residuos ni huérfanos. Lectura previa, run `41555824548` (`26d44585`): el hijo Python, los `cmd.exe`/`conhost.exe` y el nieto de la attestation figuraban `en_job=True` | `passed` |
| `assign` tardío (12 s), `.exe` congelado | `resultado is None`, el broker reporta `VfsBrokerError`, **no hay nieto de attestation**, no queda ningún proceso del árbol ni huérfano de la imagen | `passed` |
| `assign` a tiempo / tardío (8 s), Python de desarrollo | idem; el tardío sale con **72** antes de conectar | `passed` |
| cancelar **antes** de verificarse | el proceso asignado al Job duerme 8 s antes de iniciar el worker real (estado **construido**, no apostado al reloj); se cancela 1 s después de nacer: `worker_conectado_al_cancelar is False`, `TerminateJobObject` (código 1), sin nieto ni residuos | `passed` |
| cancelar **después** de verificarse y antes de ser autorizado | se cancela **al evento** «el broker aceptó el `hello`» (identidad verificada) con el `launch_ack` retenido 20 s; sin nieto ni herramienta; sin residuos | `passed` |

Tres observaciones de los runs reales, que cambiaron el código o los tests (las tres están en el historial del PR):

* con la DACL de solo consulta, recrear un nombre que **ya existe** devuelve `ACCESS_DENIED (5)` y no `ERROR_ALREADY_EXISTS`; sigue siendo un rechazo y ahora el mensaje lo dice;
* el `.exe` congelado lanza su nieto como `--vfs-probe-child`, que **no contiene** `--probe-child`: el selector de los e2e miraba una sola forma y volvía **vacua** la aserción «no se creó el nieto» del caso congelado.
  Se corrigió y una ancla (corre en Linux) congela que el selector cubre todo lo que el worker usa;
* **una carrera en MI test de cancelación «antes»**, que el sandbox no mostró y la suite completa de Windows sí: el test cancelaba a los 0,5 s asumiendo que el worker aún importaba. Con `.pyc` en caché
  el worker pudo haberse verificado, conectado y arrancado la attestation antes; el hallazgo fue un descendiente `--probe-child` en un test que decía probar «sin nieto». No era un defecto del mecanismo
  (todo seguía contenido y terminado) sino un estado de partida que dependía del reloj. Se reemplazó por estados **construidos** o **disparados por evento**: un envoltorio que duerme antes de iniciar el worker real,
  y cancelar cuando el broker acepta el `hello`. Es la misma lección que la del selector vacuo: un test cuyo estado de partida no se controla no prueba lo que dice.

**Suite completa de Windows (py3.11 y py3.12):** ver la tabla de CI del PR #860. Comparación con la base (#856 `dbf0cdce`): ruff `check` y `format --check` limpios en ambas;
`mypy --platform win32 sky_claw/ scripts/` (el job de CI corre en `windows-latest`) da **0 errores** en la base y en esta rama.

## 6. Argumento de contención, límites y por qué P2 alcanza (sin P1)

Tres proposiciones, cada una con su prueba; las dos primeras son **por construcción** y semántica del SO, la tercera es la compuerta:

1. **Identidad.** `verificar_identidad_del_job` solo devuelve una prueba si el proceso pertenece al Job abierto por ese nombre (consulta contra ESE handle), con
   `KILL_ON_JOB_CLOSE` y sin breakaway, y sus descendientes vivos están dentro. El broker solo autoriza si el `launch_ack` **y** el `hello` nombran el Job que él pidió.
2. **Herencia y contención hacia adelante.** Todo proceso que el worker (ya dentro del Job, sin breakaway) crea después de verificarse **nace dentro** del Job; y como el
   verificador cierra su handle, cerrar el Job del bridge (o que muera MO2) mata el árbol. Medido: el nieto de la attestation y la herramienta corren dentro.
3. **Hacia atrás (la compuerta).** Un proceso creado **antes** de la verificación es: (a) *dentro del Job* — nació después de que el worker entró (`assign` a tiempo): medido,
   seis de seis, y todo el árbol del `.exe` congelado `en_job`; (b) *vivo y fuera del Job* — lo detecta el barrido del verificador, el worker lo **termina** y sale con 72 antes de
   abrir un socket: medido con procesos reales (`test_un_descendiente_previo_fuera_del_job_falla_y_el_limpiador_lo_mata`) y de punta a punta con el `.exe` congelado
   (sin residuos, sin huérfanos de la imagen); (c) *ya muerto* — inocuo; (d) *huérfano* — vivo, fuera del Job y con el padre muerto: **no es alcanzable** por la relación padre→hijo.
   El caso (d) es el **límite declarado**: exige que el arranque del worker cree un proceso que a su vez deje un nieto vivo y salga. El arranque medido solo crea
   `cmd.exe`/`conhost.exe` efímeros, y el barrido por imagen de los e2e no encontró ningún huérfano del worker.

**Por qué no P1.** El brief pide no implementarlo sin justificar que P2 es insuficiente. Con P2 el hijo Python que escapa **se auto-excluye** (falla cerrado) en lugar de necesitar ser
adoptado, y lo que había creado antes se termina. P1 agregaría una segunda cadena de supervisión en el bridge (instalado, de reinstalación coordinada) para un caso que P2 ya
rechaza con evidencia.

**Por qué no se movió la verificación antes de `import sky_claw`.** Los `cmd.exe /c "ver"` nacen en el `__init__` del paquete, así que cualquier verificación dentro de `sky_claw.*` corre
**después**. Adelantarla exige un módulo de nivel superior o un *runtime hook* de PyInstaller. El único beneficio medible sería sacar de la ventana pre-verificación a 4 procesos
efímeros que (§5) no dejan residuos: no justifica el cambio de empaquetado. Se deja como opción en §9.

**Límites y riesgos abiertos (declarados, no ocultos):**

* **Disponibilidad, no seguridad.** Si `startApplication`→`assign` tarda más que la extracción del onefile, el hijo Python nace fuera y el launch **falla cerrado** (72). En el runner el margen es de ~1–2 s;
  **la latencia real de MO2/USVFS no se midió** `[NO VERIFICADO]`. Si fuera mayor, el launch fallaría siempre (seguro, pero inutilizable) y habría que pasar al stub de §9.
* **Huérfanos** (§6.3-d).
* **Doble fallo: identidad y limpieza.** Si el worker no prueba su contención **y** `terminar_descendientes_vivos` tampoco logra terminar a un descendiente (agota los intentos; p. ej. `TerminateProcess` denegado
  sobre un proceso de mayor integridad), `_abortar_por_identidad` registra `CRITICAL` (`vfs_worker_cleanup_failed`) y **sale igual con 72**, dejando vivo a ese descendiente (queda huérfano al morir el worker).
  No abre ningún socket ni autoriza nada, pero la terminación no está garantizada en ese caso. Solo lo cubre un test con dobles
  (`test_si_la_limpieza_tambien_falla_el_worker_igual_sale_con_el_codigo_de_identidad`); **no hay una prueba con un proceso real que se niegue a morir** `[NO VERIFICADO]`.
* **Token no elevado.** La DACL usa `OW` (propietario) y `BA`. El runner corre con token de administrador elevado; con el token filtrado de un usuario estándar `BA` no concede y todo depende de `OW`.
  `[NO VERIFICADO]` con token no elevado. Si `OW` no bastara, el worker no podría abrir el Job y el launch fallaría cerrado.
* **WMI** no disponible (§4.5).
* **Autoría:** P2 prueba contención, no que el worker sea el genuino (eso sigue siendo el HMAC del descriptor).
* **MO2/USVFS reales, LOOT, `vfs-health`, TexGen y DynDOLOD reales:** no se ejecutaron (restricción del brief).

## 7. Verificación local `[EJECUTADO]` (Linux; los tests de Windows se saltan y NO cuentan como PASS)

* `ruff check` y `ruff format --check` sobre `sky_claw/ scripts/ tests/`: limpios.
* `mypy --platform win32 sky_claw/ scripts/ --ignore-missing-imports`: **0 errores**, igual que la base de #856. (En Linux el mismo comando da errores de la API de Windows en ambos lados; el gate de CI corre en Windows.)
* **Mutación:** 20 mutantes del mecanismo (nombre sin validar, pertenencia contra `NULL`, handle sin cerrar, sin `KILL_ON_JOB_CLOSE`, breakaway tolerado, descendientes ignorados, broker que no compara
  `hello`/`ack`, camino de limpieza que olvida la identidad, bridge que no valida/pasa/confirma el nombre, patrón divergente…): **20 muertos, 0 sobrevivientes** — *de ese conjunto*. La revisión
  encontró un mutante **fuera** del conjunto que sí sobrevivía: quitar el `except Exception` que envuelve a `abrir_job` (la parametrización de fallos no incluía `abrir_job`; la suite anterior dio 63 passed con el mutante aplicado).
  Ahora `abrir_job` está en la parametrización y un test fija que, si abrir falla, no se cierra ningún handle y el único error es el de identidad (6 tests lo matan). Con el mismo método se comprobó que quitar el `pop` de
  `_job_objects` en `_limpiar_registro` lo matan 6 tests, 4 de ellos nuevos (uno por herramienta: un ack con identidad ajena debe terminar sin dejar estado). El script de mutación **no está versionado** (vive en el scratchpad de la sesión).
* Barrido amplio de las suites relacionadas (VFS, MO2, bridge, brokered, worker): 3624 passed, 84 skipped y 1 error de colección **preexistente** (`test_runtime_vault_operator_verifier.py`, símbolo solo-Windows; figura en la línea base).

## 8. Merge con la base que avanzó

#856 avanzó mientras se trabajaba (`dbf0cdce` → `e8106a03`); #860 quedó con conflictos y por eso GitHub no disparaba CI. Se incorporó la base por `git merge --no-ff` (`807e6f69`): dos conflictos, ambos en tests, resueltos
nombrando el invariante de cada lado. No se resolvió ningún conflicto de #854 ni de #856 con `main`.

## 9. Propuesta de seguimiento (decisiones aparte)

1. **Medir la latencia real `startApplication`→`assign` de MO2/USVFS** (sin ejecutar herramientas reales) antes de cualquier uso real. Es el dato que decide si P2 es usable.
2. Si esa latencia no tiene margen: un **stub lanzador** (un ejecutable mínimo que MO2 inicia y que no crea nada hasta que `IsProcessInJob(self, <Job>)` sea verdadero) vuelve la carrera de seguridad **y** de disponibilidad inexistente.
   Requiere cambiar lo que MO2 ejecuta; es una decisión de empaquetado.
3. Agregar el SID del usuario actual a la DACL (más explícito que `OW`) y probarlo con un token no elevado.
4. Si se quiere cerrar el límite (d): enumerar por tiempo de creación en vez de por relación padre→hijo, aceptando falsos positivos que habría que filtrar.
5. Levantar `_CONTENCION_DEL_WORKER_VERIFICADA` es una decisión **separada** de este PR, con revisión adversarial y sobre la evidencia de este informe.

## 10. Cómo reproducir

```bash
.venv/bin/python -m pytest tests/test_vfs_job_identity.py tests/test_vfs_job_identity_broker.py tests/test_vfs_job_identity_bridge.py \
    tests/test_vfs_job_identity_anclas.py tests/test_vfs_fence_de_supervision.py -q
```

En Windows (sandbox aislado, procesos inocuos; el workflow `vfs-supervision-sandbox.yml` hace todo esto en `windows-latest`):

```bash
.venv/Scripts/python -m pytest tests/test_vfs_job_identity_windows.py tests/test_vfs_job_identity_e2e_windows.py tests/test_windows_onefile_census.py -q -rA
pyinstaller sky_claw.spec --clean
# SKYCLAW_FROZEN_EXE=dist\SkyClawApp.exe  →  el e2e corre también contra el ejecutable congelado
.venv/Scripts/python -m scripts.windows_onefile_census --exe dist\SkyClawApp.exe '--arg=--vfs-probe-child' '--arg={probe_file}' '--arg={probe_sha}' --retardos 0,0.5,1,2 --repeticiones 3 --json censo.json
```
