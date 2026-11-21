# R0: re-certificación en rig real — Ronda 6, R0-2 (2026-11-21)

> **Operador:** `<usuario>` (abrió y cerró MO2 desde el Explorador, 3 sesiones) · **Aprobador:** propietario · **Auditor independiente:** pendiente, con evidencia propia.
> **Alcance:** solo R0-2 (identidad del bundle del bridge, condición 3 del HOLD). Preregistro: `PREREGISTRO.md` (SHA-256 `fb76fec2…a09e06`, sellado antes de abrir MO2).

## Línea base

| Dato | Valor |
|---|---|
| `origin/main` | `676f4c29ebea50fde8a8817a887521812ea40a2c` (squash del PR #980, F1) |
| `.exe` congelado | `E:\SkyClaw_R0_Rig\exe_676f4c29\SkyClawApp.exe` · `88.296.345` B · SHA-256 `ef7777c163e8c03524ae6f08f4171beee2957cfbd677c013db6f3d94bfdc9e55`. Registrado **antes** de usarlo (`SkyClawApp.exe.sha256.json` de la sesión que lo armó, más `fase_0/exe_sha256_pre_uso.json` a las 10:12:21 −03:00). El lanzador lo re-verifica antes de cada corrida y no corre si no coincide. Hay registro en 8 de 9 `meta.json`; el del paso 2 se perdió (desvío 1), pero esa corrida también pasó por la guarda. |
| `_CONTENCION_DEL_WORKER_VERIFICADA` | `False` (línea 922 en 676f4c29, por `git show`; `fase_0/hold_check.txt`) |
| `plugin_bundle/` | Mismo tree git en 676f4c29 y en 3f8547c8 (`c06168f5…`) |
| Windows / MO2 / USVFS / Skyrim | Windows 10 Pro 10.0.19045 / MO2 2.5.2 rev `9c130cbf` / USVFS 0.5.6.1 / Skyrim SE 1.6.1170.0 (hashes en `binary-hashes.json`, iguales a la ronda 5) |
| TexGen / DynDOLOD / Resources SE / DLL NG | No usados en R0-2 |
| Token e integridad | Filtrado: `Mandatory Label\Medium Mandatory Level` (`S-1-16-8192`), en `fase_0/whoami_groups.txt` |
| Aislamiento | MO2 portable en `E:\SkyClaw_R0_Rig\mo2` (`portable.txt`), perfil `R0_Descartable`, `SKYCLAW_ISOLATED=1` y `HOME=USERPROFILE=E:\SkyClaw_R0_Rig\home` en todo proceso de Sky-Claw. **No** se tocó la instancia global (0 archivos modificados en `%LOCALAPPDATA%\ModOrganizer`), ni el MO2 de `E:\Sectores\...` (0), ni el keyring del operador. |
| Arnés brokered | Ninguno: R0-2 usa `--mode vfs-health`. Lanzador de medición `scripts/correr_exe.ps1` (no se commitea; no cambia código). |

### R0-0 de este exe

- `quarantine_pre`: «sin cuarentena persistida».
- `install-vfs-bridge` con este exe: `rc=0`. `bridge_config.json` registra `bundle_digest = 24a1869e4e6213d799e940934dab0900fac2fc0de30babdceb28517749d256b0` y `worker_executable = …\exe_676f4c29\SkyClawApp.exe`; antes apuntaba a `src_r0r4\dist` (el exe `918a…`).
- El digest independiente de la copia instalada (`scripts/digest_bundle.py`, que carga `protocol.py` de 676f4c29 por ruta con `python -I`) también da `24a1869e…`.
- El bundle anterior quedó respaldado en `backup/bundle_pre_install_918a/`, con el mismo digest.

## Resultados

| Paquete | Escenario | Esperado | Observado | Resultado | Evidencia |
|---|---|---|---|---|---|
| R0-2 paso 1 | Sesión A de MO2, bundle bueno; `vfs-health` | `hello` aceptado con el digest esperado | `rc=0` en 11,0 s; `Probe VFS correcto`; `launch timings` (`startApplication_ms=140.959`, `total_to_ack_ms=146.456`); `worker_exit` con `exit_code=0` y `job_empty=True`; sin cuarentena. El broker solo acepta un `hello` cuyo `bundle_digest` es igual al del producto, y el producto es `24a1869e…` (`bridge_config.json` y el mensaje del paso 2) | **PASS** | `r0_2/paso1/run_01/` |
| R0-2 paso 2 | MO2 cerrado → offset 3 de `__init__.py` `0x4D`→`0x6D` (digest en disco `18e25d59d7c3…`, el preregistrado) → Sesión B; `vfs-health` | Rechazo con la instrucción de reinstalar y reabrir MO2 | 2 rechazos del broker con el texto `el MO2 bridge instalado (18e25d59d7c3) no es el bundle de esta versión de Sky-Claw (24a1869e4e62): reinstalalo con `--mode install-vfs-bridge` y reabrí MO2`; el `submit` falla con `MO2 bridge no se conectó al broker: <ese motivo>`; 0 jobs lanzados (sin `launch timings`); sin cuarentena. El prefijo declarado coincide con el digest calculado del disco alterado: el plugin hashea lo que carga | **PASS** (el proceso no sale: hallazgo 1) | `r0_2/paso2/` |
| R0-2 paso 4 | Sesión B **abierta** con el bundle alterado cargado; `install-vfs-bridge` y después `vfs-health` | El disco cambia; el digest del proceso en ejecución, no | La reinstalación da `rc=0` y el disco vuelve a `24a1869e…`; el `vfs-health` **sigue rechazado** con `(18e25d59d7c3)` (3 rechazos), 0 jobs, sin cuarentena | **PASS** (mismo hallazgo 1) | `r0_2/paso4/` |
| R0-2 paso 3 | MO2 cerrado → `install-vfs-bridge` → Sesión C; `vfs-health` | Bridge reinstalado aceptado | Reinstalación `rc=0`, digest `24a1869e…`; `vfs-health` con `rc=0` en 10,4 s, `Probe VFS correcto`, `worker_exit` con `job_empty=True`, sin cuarentena | **PASS** | `r0_2/paso3/` |
| Higiene | `vfs-quarantine status` al cierre | Sin cuarentena | «sin cuarentena persistida»; 0 procesos de Sky-Claw vivos; perfil con la misma semántica (ver desvío 3) | **PASS** | `r0_2/quarantine_post/`, `isolation.json` |

El paso 4 se corrió antes que el 3, como estaba preregistrado: necesita la sesión B con el bundle alterado cargado.

## Desvíos y hallazgos (sin filtrar)

1. **Hallazgo de producto: después de un rechazo del bridge, `--mode vfs-health` no termina.** Pasó en 2 de 2 corridas con rechazo (pasos 2 y 4). Con bridge aceptado salió normal (2 de 2). El broker registra `CRITICAL Fallo no manejado en el proceso principal` con el `VfsBrokerError` a los ~10 s y libera su estado: no quedan descriptor, lock ni marcador, solo `jobs/`. Sin embargo, el proceso hijo onefile sigue vivo, con 3 threads y sin CPU.
   - En el paso 2, mi lanzador mató solo al bootloader a los 180 s. El hijo (pid 20048) siguió vivo, con los handles de stdout y stderr abiertos, y el lanzador abortó sin escribir `meta.json`. Lo maté a mano (`Stop-Process`) a los ~5 min. Lo dejé registrado en `r0_2/paso2/run_01/hallazgo_proceso_colgado.json` y su `meta.json` está escrito a mano.
   - Corregí el lanzador para que mate el árbol entero (`taskkill /T`). En el paso 4 el corte llegó a los 180 s, con `rc=1` y el árbol registrado.
   - **No afecta los veredictos de R0-2:** el rechazo y su mensaje ya estaban registrados. **Sí afecta al operador:** un `vfs-health` contra un bridge viejo queda colgado en vez de devolver el error. Si el modo GUI o el agente comparten esa salida, también podría pasarles; no lo verifiqué.
   - Causa no diagnosticada; la hipótesis es un thread no daemon en el camino de error. **Propuesta:** un PR correctivo aparte, con un test en rojo que corra `vfs-health` contra un bridge rechazado y exija que el proceso salga con un `rc≠0` acotado. No lo abrí.
2. **El `hello` no se capturó crudo.** Ni el broker ni el plugin registran el `bundle_digest` aceptado. El paso 1 se apoya en la regla del código (aceptar implica igualdad) y en el digest del producto visible en `bridge_config.json` y en el mensaje de rechazo. El digest declarado solo es visible cuando hay rechazo, y ahí coincide con el calculado. Mejora posible: loguear el digest aceptado, en un PR aparte.
3. **Un error de mi script de medición, no del producto.** `isolation.json` y `r0_2/perfil_snapshot_post.json` dicen `modlist … false`: el snapshot previo guardó cada línea como objeto de PowerShell y la comparación falló. Los valores son iguales y en el mismo orden (`+TexGen Output`, `+DynDOLOD DLL NG and Scripts`, `+R0 Canary`). La corrección está en `r0_2/correccion_modlist_semantica.json`; los archivos originales no se reescribieron.
4. **Redacción (regla 5):** `fase_0/perfil_snapshot_pre.json` tenía el usuario de Windows dentro de metadatos que PowerShell serializó por error. Se reemplazó por `<usuario>` (`fase_0/redaccion.txt`, con el hash anterior y el posterior).
5. **`mo2aibridge` está instalado en los plugins del MO2 del rig.** No se usó y no se leyó su token.

## Qué NO certifica (obligatorio)

- **No levanta el HOLD.** `_CONTENCION_DEL_WORKER_VERIFICADA` sigue en `False`. La condición 2 (el auditor independiente) está pendiente.
- **Ejercí una sola alteración:** un byte en `__init__.py`, sin cambiar el tamaño. No probé agregar, quitar o renombrar archivos, alterar `runtime.py`/`plugin.py`, un bundle sin digest (versión anterior) ni symlinks o junctions en el bundle. Están cubiertos por `tests/test_mo2_bridge_identidad_del_bundle.py`, pero no en el rig.
- **No verifiqué la ruta del agente ni la GUI** (preflight de la etapa 9 con un bridge viejo, R0-3): solo `--mode vfs-health`.
- **No verifiqué el bytecode del exe:** el HOLD en el PYZ y F1 los tomé de `procedencia.txt`, de la otra sesión. Yo verifiqué la fuente en 676f4c29 y el hash del binario.
- **No ejercí F1** (pérdida del bridge con un job en vuelo): no se cerró MO2 con un job activo.
- **No probé la reproducibilidad del build,** ni otras versiones de MO2 o USVFS, ni el MO2 de producción.
- **R0-1 con este exe** (serie N de `vfs-health` y laboratorio congelado) **no se corrió en esta ronda:** la evidencia de la condición 1 sigue siendo la del exe `918a…`.

## Firmas

Operador: `<usuario>` · Aprobador: <pendiente> · Auditor independiente: <pendiente, con su evidencia propia>
