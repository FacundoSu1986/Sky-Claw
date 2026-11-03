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
  construir la API de Windows, es un `JobIdentityError` (no un error crudo). El broker solo autoriza con el nombre pedido en el ack **y** en el
  `hello`. Es contención, no autoría (la autenticidad sigue siendo el HMAC). Convive con el fence: no lo reemplaza.
  Anclas: `tests/test_vfs_job_identity.py` (lógica, API falsa), `tests/test_vfs_job_identity_anclas.py` (enumeran
  quién toca `job_object`, quién suelta un job y quién fabrica una prueba),
  `tests/test_vfs_job_identity_windows.py` y `tests/test_vfs_job_identity_e2e_windows.py` (procesos y Job reales;
  el e2e corre también contra el `.exe` congelado con `SKYCLAW_FROZEN_EXE`). `_CONTENCION_DEL_WORKER_VERIFICADA`
  (PR #854) sigue en `False`: levantarlo es una decisión aparte con la evidencia de la auditoría.
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
