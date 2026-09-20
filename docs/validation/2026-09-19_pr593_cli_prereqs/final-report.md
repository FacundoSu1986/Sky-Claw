# Rig real — prerrequisitos CLI de etapa 9 (#593), campaña `2026-09-20_final`

**Alcance:** verificar en el rig real que el **camino productivo**
(`DynDOLODPipelineService._ensure_runner`) construye un argv que TexGen/DynDOLOD
Alpha-209 **reciben sin mangling**: game mode, `-o:`, `-d:`, `-m:`, `-p:`, `-t:`.
**No** se pulsa Start, **no** se genera LOD, **no** se prueba UI Automation
(`readiness=DISABLED_FOR_TEST`, seam del runner directo).

## Procedencia

| Elemento | Valor |
|---|---|
| Rig | `rig_cli_prereqs.py` de esta rama, commit `d8994491` (cuenta sólo `PostMessageW` exitosos) |
| Campaña | `E:\Sky-Claw T5 Rig\evidence\PR593-cli-prereqs\2026-09-20_final\` (nombre por fecha UTC de la corrida; hora local del log: 2026-09-19 21:32/21:33) |
| Artefactos `00..04` | copia literal de la salida del rig en esa carpeta, sin edición manual |

El rig corrió desde un checkout aislado del commit (`git worktree`), porque el
checkout principal estaba ocupado por otra rama con cambios en curso; ningún
campo de la evidencia depende de la ruta del repo.

## Entorno (observado en `00-workspace.txt` y `01-argv-productivo-*.txt`)

| Elemento | Valor |
|---|---|
| Game / data dir | `G:\Modding\Skyrim_Runtime_1.6.1170` (`Data\`) — SSE |
| MO2 instance data | `G:\Modding\MO2\SkyrimSE` |
| Perfil activo | `SkyClaw-PR2-Rig` |
| `plugins.txt` (`-p:`) | `G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt` |
| Binarios | `C:\Modding\DynDOLOD RigTest\{TexGenx64.exe,DynDOLODx64.exe}` (Alpha-209) |
| `ini_dir` (`-m:`) | `C:\Modding\DynDOLOD RigTest\_rig_test\ini` (`DYNDLOD_INI_DIR`) |
| `-t:` | `%USERPROFILE%\AppData\Local\Temp\` (redactado en la evidencia) |
| Work root | `E:\Sky-Claw T5 Rig\evidence\PR593-cli-prereqs\2026-09-20_final\PR593 Work` (con espacios) |

## Procedimiento

`rig_cli_prereqs.py` arma el boundary real:

1. `PathResolutionService` real con `profile_name="SkyClaw-PR2-Rig"`: la instancia,
   la raíz de datos y el perfil son los de producción.
2. `resolver_workspace(...)` real: admisión, binding y **ownership vivo** (lease P2.2
   como fence antes del spawn). TexGen creó el binding (estado `B_VACIO_SIN_BINDING`);
   DynDOLOD lo reutilizó (`C_BINDING_COMPATIBLE`) dentro de la misma campaña.
3. `servicio._ensure_runner()` — **sin inyectar `DynDOLODConfig` a mano** (el seam que
   documentaba el hueco de `-m:`/`-p:`).
4. `runner._build_xedit_args(...)` y `runner._execute_process(...)` (spawn real con
   `CREATE_NO_WINDOW`, timeout 150 s).
5. A los 25 s, `WM_CLOSE` **sólo** a un descendiente nuevo de ESTE rig (snapshot de
   hijos antes del spawn; nombre y exe deben coincidir; exactamente 1 candidato).
   `cierre.cerrado` es verdadero sólo si al menos un `PostMessageW` devolvió distinto
   de cero (`enviadas`), no si se enumeró alguna ventana.

## Resultados (eco literal del binario en su log)

### TexGen Alpha-209 — sesión `2026-09-19 21:32:34` (local)

```
TexGen 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE) (ADADC675) starting session 2026-09-19 21:32:34
Using Skyrim Special Edition Data Path: G:\Modding\Skyrim_Runtime_1.6.1170\Data\
Using ini: C:\Modding\DynDOLOD RigTest\_rig_test\ini\\Skyrim.ini
Using plugin list: G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt
Loading active plugin list: G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt
Using Temp Path: %USERPROFILE%\AppData\Local\Temp\
Using Output Path: E:\Sky-Claw T5 Rig\evidence\PR593-cli-prereqs\2026-09-20_final\PR593 Work\DynDOLOD\TexGen\
```

Cierre (`04-resultado-texgen.json`): `rc=0`, `cierre.cerrado=true`, `pid=21444`,
`enviadas=3`, `duracion_s=26.6` (timeout 150 s), `lineas_nuevas=166`,
`validacion.ok=true`, `errores=[]`.

### DynDOLOD Alpha-209 — sesión `2026-09-19 21:33:21` (local)

```
DynDOLOD 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE) (561440F0) starting session 2026-09-19 21:33:21
Using Skyrim Special Edition Data Path: G:\Modding\Skyrim_Runtime_1.6.1170\Data\
Using ini: C:\Modding\DynDOLOD RigTest\_rig_test\ini\\Skyrim.ini
Using plugin list: G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt
Loading active plugin list: G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt
Using Temp Path: %USERPROFILE%\AppData\Local\Temp\
Using Output Path: E:\Sky-Claw T5 Rig\evidence\PR593-cli-prereqs\2026-09-20_final\PR593 Work\DynDOLOD\DynDOLOD\
```

Cierre (`04-resultado-dyndolod.json`): `rc=0`, `cierre.cerrado=true`, `pid=11036`,
`enviadas=3`, `duracion_s=29.7` (timeout 150 s), `lineas_nuevas=154`,
`validacion.ok=true`, `errores=[]`.

## Condiciones verificadas en esta campaña

- **[OBSERVADO]** `validacion.ok=true` con `errores=[]` en ambos JSON: el rig
  compara eco por eco (`-d:`, `-m:` —incluido `\\Skyrim.ini`—, `-p:`, `-t:`, `-o:`,
  `starting session`) contra el argv productivo, y no acepta el eco faltante.
- **[OBSERVADO]** `tool=texgen` y `tool=dyndolod` presentes en los `01-*`.
- **[OBSERVADO]** `cerrado=true` con `enviadas=3` (>0) en ambas corridas: el conteo
  es de envíos `PostMessageW` exitosos, no de ventanas enumeradas.
- **[OBSERVADO]** `rc=0` con duración ~27-30 s (< 150 s) en ambas corridas.
  **[INFERIDO]** el proceso salió a continuación del `WM_CLOSE` exitoso del watcher
  (un timeout habría dejado `rc` de TIMEOUT y hard-kill del árbol).
- **[OBSERVADO]** logs frescos: header `starting session` de esta campaña y `-o:`
  bajo `2026-09-20_final` en ambos `03-log-nuevo-*.txt`; los `04-*` registran la
  firma previa del log (`log_bytes_antes`/`log_sha256_antes`) para delimitar el
  tramo nuevo (166 y 154 líneas).
- **[OBSERVADO]** privacidad: sin `Facu2` ni rutas del repo/checkout en ningún
  artefacto; `%USERPROFILE%` sustituye al home en `-t:` (argv y ecos) y en los logs.
- **[OBSERVADO]** subroots de salida `...\PR593 Work\DynDOLOD\{TexGen,DynDOLOD}`
  vacíos: sin Start no hay escritura de salida (sólo el binding del work root).
- **[INFERIDO]** pertenencia del PID cerrado: `cierre.pid` sale del snapshot de
  hijos del proceso del rig (`psutil`), no de una búsqueda global por nombre; la
  verificación post-mortem exacta no es posible con el proceso ya terminado.

## Qué demuestra y qué no

**Demuestra (para LOS DOS binarios, por separado):** el game mode recibido es SSE;
`-d:`, `-m:`, `-p:`, `-t:` y `-o:` llegan exactamente con los valores configurados;
`-p:` es el `plugins.txt` del **perfil activo** (`SkyClaw-PR2-Rig`), cableado por el
camino productivo; `-o:` son los subroots exclusivos por herramienta bajo el root
externo con espacios; ningún switch salió mangled; ambas corridas cierran con
`WM_CLOSE` efectivamente enviado.

**No demuestra:** que la generación completa funcione (no se pulsó Start), ni el gate
de UI Automation, ni el preset/worldspaces. El `\\` doble en `Using ini:` reaparece
medido en ambas corridas (divergencia CRT/Delphi pre-existente registrada en
`docs/validation/2026-09-10_t5v2_dyndolod_stage9.md`); el eco resuelve
`…\ini\\Skyrim.ini` y el rig lo exige exactamente así.

## Autoridad de `-m:` (decisión de PR; no re-auditada en esta campaña)

`-m:` se cablea desde la declaración **explícita** del operador (`DYNDLOD_INI_DIR`):
`PathResolutionService.get_dyndolod_ini_dir` exige absoluta, existente, directorio y
no raíz de volumen, con canonicalización previa al veto. No hay derivación
automática; la justificación completa vive en el docstring del método
(`sky_claw/app/core/path_resolver.py`).
