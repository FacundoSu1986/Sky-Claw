# AGENTS.md — MO2, perfiles y USVFS

Aplican [`../AGENTS.md`](../AGENTS.md), que es el SOP canónico, y las reglas
raíz.

## Fuentes obligatorias

`profile_sandbox.py`, `vfs_broker.py`, `vfs_worker.py`,
`vfs_attestation.py`, `vfs_contracts.py` y ADR 0007.

## Invariantes

- Instancia y perfil explícitos; drift falla cerrado.
- **La identidad de un nombre de mod no distingue mayúsculas** (#838): MO2 resuelve
  `mods/<Nombre>` contra NTFS, así que `X` y `x` son el mismo mod. Todo mutador del
  `modlist.txt` (`add_mod_to_modlist` —sus dos ramas y las dos formas de pedir máxima
  prioridad—, `remove_mod_from_modlist`, `toggle_mod_in_modlist`) compara con la única clave
  `vfs._clave_de_nombre_de_mod`, que usa `lower()` y **no** `casefold()` (NTFS sí
  distingue `Straße` de `Strasse`) — la misma identidad que asume
  `ganadores._exigir_enabled` y `vfs_attestation._case_insensitive_key`. Al escribir se
  conserva la grafía que ya estaba en el archivo; sólo se usa la del caller si el mod
  no estaba. Ancla: `tests/test_mo2_modlist_identidad_sin_mayusculas.py` (enumera las
  ramas, el límite `lower`/`casefold`, los mutadores y los callers).
- El canary debe probar visibilidad virtual, no sólo el directorio físico.
- La promoción usa baseline, backup, escritura transaccional y rollback.
- Si falla el rollback, preservar el backup.
- Timeout/cancelación termina y recolecta (`reap`) el worker y todo su árbol de
  procesos descendientes.
- **Un PPID no prueba parentesco: el bridge adopta por PPID pero solo descarta con evidencia positiva** (H-01, #877).
  Windows no reasigna el PPID cuando el padre muere, así que un proceso viejo —típicamente de sistema (`csrss`,
  `wininit`, `winlogon`: sus padres, los `smss` de sesión, ya salieron), que `OpenProcess` rechaza con ACCESS_DENIED—
  puede quedar como «hijo» del PID que el sistema le entregue después al worker, y antes nunca se podía leer su
  creación (necesita handle) para descartarlo: el launch terminaba en `BridgeTerminationError` y la instancia en
  cuarentena (flake de CI, run 42537421209: «quedan vivos fuera del Job: [720, 844] … GetLastError=5»).
  `adopt_descendants` solo lo saca de la cuenta si su creación, leída SIN abrirlo
  (`AdoptionApi.creation_time_by_pid` → `NtQuerySystemInformation`, la misma lectura de reserva de `psutil`), es
  **anterior** a la de su padre retenido. Sin esa evidencia —lectura fallida, el PID ya no figura, creación IGUAL o
  posterior— un candidato que no se pudo identificar sigue siendo un descendiente vivo fuera del Job
  (`BridgeTerminationError`): no lo relajes. El error nombra el PPID de cada candidato. Todo implementador nuevo de
  `AdoptionApi` aplica la misma regla; los consumidores con `psutil.Process.children` (`vfs_job_identity`, `vfs`,
  `grass_cache_runner`) ya la tienen. Anclas: `tests/test_mo2_bridge_adopcion.py` (la tabla
  `…solo_se_descarta_con_evidencia_positiva…` enumera los casos; `test_todo_implementador_…` y
  `test_no_hay_otro_implementador_…` congelan la familia) y `tests/test_mo2_bridge_adopcion_windows.py` (kernel real:
  reproduce el ACCESS_DENIED sobre un proceso de sistema real y compara `NtQuerySystemInformation` con
  `GetProcessTimes`). **Sin medir:** que el PID reciclado ocurra en la práctica (el asignador de PIDs de Windows es
  LIFO y no se pudo provocar a voluntad: la causa del flake de CI es una hipótesis consistente con la firma, no una
  prueba) y la disposición de la estructura en Python de 32 bits.
- **El worker no crea el nieto de la attestation ni despacha la herramienta antes de estar
  supervisado.** Espera `supervised` (autenticado, ligado a su `job_id`) ANTES de la
  attestation, que crea el proceso nieto; lo que el intérprete crea al arrancar (imports,
  anterior al `hello`) NO está cubierto; el
  broker lo envía una vez, solo tras el `launch_ack` del bridge para ese job. El plazo solo
  puede abortar, nunca habilitar; un worker sin `supervision_fence` en el `hello` se rechaza.
  Anclas: `tests/test_vfs_fence_de_supervision.py` (protocolo, enumera `ALLOWED_VFS_TOOL_IDS`),
  `tests/test_vfs_fence_e2e_proceso_real.py` (worker real) y
  `tests/test_vfs_job_containment_windows.py` (Job Objects reales). Un consumidor nuevo del
  broker hereda el fence: no lo reimplementes por herramienta.
- **El worker EFECTIVO prueba que vive en el Job de ESTE launch antes de hablar con el broker** (P2). En un
  `.exe` PyInstaller *onefile* el proceso que MO2 lanza y el bridge asigna es el bootloader; Python corre en un
  HIJO que puede haber nacido antes del `assign` y quedar fuera. El broker elige por launch un nombre de Job
  inadivinable (`Local\SkyClawVfs-<128 bits>`, `_registrar_job`: el único punto por el que pasan `submit` y
  `open_session`); el bridge crea el Job con ese nombre (rechaza uno preexistente y concede solo consulta en su
  DACL) y lo confirma en el `launch_ack`; el worker lo abre con `JOB_OBJECT_QUERY`, pregunta
  `IsProcessInJob(<este proceso>, <ESE handle>)` —**nunca `NULL`**: contra `NULL` el SO responde `TRUE` para
  CUALQUIER Job—, exige `KILL_ON_JOB_CLOSE` sin breakaway y que todo descendiente vivo esté dentro, y **cierra su
  handle** (`KILL_ON_JOB_CLOSE` solo dispara al cerrarse el último). Si falla, `worker_main` **intenta** terminar a sus
  descendientes y sale con 72 sin abrir un socket; la terminación no está garantizada: si la limpieza también falla
  (un descendiente que no muere), se registra `CRITICAL` y ese proceso puede quedar vivo. Cualquier falla, incluida la de
  construir la API de Windows o la de la propia limpieza, es un `JobIdentityError` (no un error crudo); si la
  verificación y el cierre del handle fallan a la vez, viaja la causa de la verificación y el cierre se registra. El broker solo autoriza con el nombre pedido en el ack **y** en el
  `hello`. Es contención, no autoría (la autenticidad sigue siendo el HMAC). Convive con el fence: no lo reemplaza.
  Anclas: `tests/test_vfs_job_identity.py` (lógica, API falsa), `tests/test_vfs_job_identity_anclas.py` (enumeran
  quién toca `job_object`, quién suelta un job y quién fabrica una prueba),
  `tests/test_vfs_job_identity_windows.py` y `tests/test_vfs_job_identity_e2e_windows.py` (procesos y Job reales;
  el e2e corre también contra el `.exe` congelado con `SKYCLAW_FROZEN_EXE`). `_CONTENCION_DEL_WORKER_VERIFICADA`
  (PR #854) sigue en `False`: levantarlo es una decisión aparte con la evidencia de la auditoría.
- **La cuarentena del broker se libera con UNA sola pieza y con evidencia** (H-4). La cuarentena persiste de tres
  formas (marcador `.<instancia>.quarantine`, lock envenenado `quarantined: true` y lock de un broker que murió sin
  cerrar) y borrar solo una no alcanza: el próximo arranque reclama el lock huérfano y la reimpone.
  `vfs_quarantine.py` contesta «¿está en cuarentena y por qué?» y la libera (`--mode vfs-quarantine`); para decidir si
  un lock está huérfano reusa `vfs_broker.lock_de_instancia_tiene_dueno_vivo`, la ÚNICA definición (el broker la usa
  para reclamar), y para el dueño de un lock envenenado `vfs_broker.proceso_dueno_sigue_vivo` (PID **y** `create_time`:
  un PID reciclado por otro proceso —incluido el de quien consulta— no es el dueño; un lock antiguo sin `create_time` se asume vivo): no las copies.
  Los DOS escritores del lock envenenado guardan `create_time` (lo ancla un test por AST). Exige evidencia no vacía, se niega con un dueño vivo del lock (un broker vivo la
  volvería a persistir desde su memoria), borra el lock —releído y sin cambios— ANTES que el marcador (un fallo a mitad
  deja siempre cuarentena entera) y registra `CRITICAL`. **Límite conocido:** entre la relectura y el `unlink` del lock
  queda una ventana mínima en la que un broker que arranque justo entonces podría reclamar el huérfano; es la misma
  ventana de `_acquire_instance_file_lock` (reclamo = `unlink` + `O_EXCL`, sin mutex entre procesos), por eso el
  procedimiento pide cerrar Sky-Claw antes. Cerrarla de verdad exige que el broker participe de un protocolo de lock
  distinto: no se arregla desde el liberador. Todo escritor nuevo del estado de cuarentena es un mutador más:
  tiene que entrar al conjunto congelado. Anclas: `tests/test_vfs_quarantine.py` (fabrica cada estado con el broker REAL
  y exige que lo que dice el módulo coincida con lo que el broker hace al arrancar) y
  `tests/test_vfs_quarantine_anclas.py` (enumera por AST quién muta el marcador/lock, los sitios que arman la ruta de
  estado y que todo `--mode` esté documentado).
- **El outbox del bridge se consume en UN solo lugar y un evento que no se pudo enviar vuelve al FRENTE** (H-14, B-2).
  `runtime.flush_outbox` es el único que saca eventos de `BridgeEventOutbox`; si `send` falla, `requeue_front` lo devuelve al
  frente (sin tocar `unfinished_tasks`, para que el apagado lo siga esperando) y el error se propaga. Un `put` de lo que se
  sacó lo mandaría al final y, tras reconectar, `worker_exit` saldría antes que `launch_ack`. Nadie más consume el outbox:
  `plugin.py` solo lo produce (`put`) o espera (`wait_until_drained`). Anclas: `tests/test_mo2_bridge_orden_de_eventos.py`
  (reconexión, propiedad con productor concurrente y AST de consumidores). **Cerrar MO2 a mitad de una corrida** puede dejar la
  instancia en cuarentena (el drenaje del apagado es de 2 s y el broker no puede probar que el worker terminó sin su
  `worker_exit`): es lo esperado y se libera con `--mode vfs-quarantine`; no lo «arregles» ampliando el plazo.
- `ganadores.py` (NP-A0d) contesta UNA vez «¿qué capa provee esta ruta virtual en el overlay del perfil?». Recibe `enabled` ya
  normalizado por `vfs_attestation.read_enabled_mods` (no parsea el modlist: un test lo ancla), recorre con
  `links.iter_archivos_propios` (no entra a enlaces), trata `overwrite` como la capa más alta y un `*.mohidden` como ausente, y
  falla cerrado ante un mod habilitado sin carpeta, un nombre repetido o un árbol que pasa el tope. Es una vista ESTRUCTURAL:
  no prueba que USVFS mapee lo resuelto (eso es el canary del broker) ni considera el game data físico.
  Su único consumidor es `native_parallax/diagnostico_perfil.py` (NP-A0e/A0g, solo lectura: avisa cuando otro mod, `overwrite` o un
  mod de control tapa la entrada o la salida del generador); está clasificado en la Familia A del censo de
  `tests/test_mo2_modlist_priority.py` porque interpreta la prioridad, y el censo de `tests/test_native_parallax_diagnostico_perfil.py` congela
  que `indexar_ganadores` se llame solo desde ahí.

## Verificación segura

Los tests no sustituyen el
[smoke real](../../../docs/operations/real_rig_validation.md).
