# Rig real — prerrequisitos CLI de etapa 9 (#593), 2026-09-19

**Alcance:** verificar en el rig real que el **camino productivo** (`DynDOLODPipelineService._ensure_runner`)
construye un argv que TexGen/DynDOLOD Alpha-209 **reciben sin mangling**: game mode,
`-o:`, `-d:`, `-m:`, `-p:`, `-t:`. **No** se pulsa Start, **no** se genera LOD, **no**
se prueba UI Automation (`readiness=DISABLED_FOR_TEST`, seam del runner directo).

## Entorno

| Elemento | Valor |
|---|---|
| Game | `G:\Modding\Skyrim_Runtime_1.6.1170` (SSE) |
| MO2 install | `C:\Modding\ModOrganizer2` (instancia global en `%LOCALAPPDATA%\ModOrganizer\Skyrim Special Edition`) |
| MO2 instance data | `G:\Modding\MO2\SkyrimSE` |
| Perfil activo | `SkyClaw-PR2-Rig` |
| Binarios | `C:\Modding\DynDOLOD RigTest\{TexGenx64.exe,DynDOLODx64.exe}` (Alpha-209) |
| `-m:` declarado | `C:\Modding\DynDOLOD RigTest\_rig_test\ini` (`DYNDLOD_INI_DIR`) |
| Work root | `E:\Sky-Claw T5 Rig\evidence\PR593-cli-prereqs\2026-09-19_bounded\PR593 Work` (con espacios) |

## Procedimiento

`rig_cli_prereqs.py` (mismo directorio) arma el boundary real:

1. `PathResolutionService` real con `profile_name="SkyClaw-PR2-Rig"` y `MO2_PATH` de
   instalación: la instancia se descubre por la metadata global, la raíz de datos y el
   perfil son los de producción.
2. `resolver_workspace(...)` real: admisión, binding y **ownership vivo** (la lease P2.2
   corre como fence antes del spawn).
3. `servicio._ensure_runner()` — **sin inyectar `DynDOLODConfig` a mano** (el seam que
   usaba el rig anterior de PR-2 y que documentaba el hueco de `-m:`/`-p:`).
4. `runner._build_xedit_args(None, herramienta=…)` y `runner._execute_process(...)`
   (spawn real con `CREATE_NO_WINDOW`, timeout acotado).
5. A los 25 s se cierra el asistente con `WM_CLOSE` (equivale a que el operador cierre la
   ventana; **no** toca Start). El binario vuelca su log al cerrarse; un hard-kill lo
   pierde (medido en la primera corrida: cero líneas nuevas).

## Resultados (eco literal del binario en su log)

### TexGen Alpha-209 — sesión `2026-09-19 08:23:15`

```
TexGen 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE) (ADADC675) starting session 2026-09-19 08:23:15
Using Skyrim Special Edition Data Path: G:\Modding\Skyrim_Runtime_1.6.1170\Data\
Using ini: C:\Modding\DynDOLOD RigTest\_rig_test\ini\\Skyrim.ini
Using plugin list: G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt
Loading active plugin list: G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt
Using Output Path: E:\Sky-Claw T5 Rig\evidence\PR593-cli-prereqs\2026-09-19_bounded\PR593 Work\DynDOLOD\TexGen\
```

### DynDOLOD Alpha-209 — sesión `2026-09-19 08:24:17`

```
DynDOLOD 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE) (561440F0) starting session 2026-09-19 08:24:17
Using Skyrim Special Edition Data Path: G:\Modding\Skyrim_Runtime_1.6.1170\Data\
Using ini: C:\Modding\DynDOLOD RigTest\_rig_test\ini\\Skyrim.ini
Using plugin list: G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt
Loading active plugin list: G:\Modding\MO2\SkyrimSE\profiles\SkyClaw-PR2-Rig\plugins.txt
Using Output Path: E:\Sky-Claw T5 Rig\evidence\PR593-cli-prereqs\2026-09-19_bounded\PR593 Work\DynDOLOD\DynDOLOD\
```

Ambas corridas: `rc=0` (cierre por `WM_CLOSE`), 3 ventanas cerradas, cero escrituras en los
subroots exclusivos (`PR593 Work\DynDOLOD\{TexGen,DynDOLOD}` quedaron vacíos — sin Start no
hay salida).

## Qué demuestra y qué no

**Demuestra (para LOS DOS binarios, por separado):** el game mode recibido es SSE; `-d:`,
`-m:`, `-p:` y `-o:` llegan exactamente con los valores configurados; `-p:` es el
`plugins.txt` del **perfil activo** (`SkyClaw-PR2-Rig`), cableado por el camino productivo;
`-o:` son los subroots exclusivos por herramienta bajo el root externo con espacios; ningún
switch salió mangled.

**No demuestra:** que la generación completa funcione (no se pulsó Start), ni el gate de UI
Automation, ni el preset/worldspaces. El `\\` doble en `Using ini:` es la divergencia
CRT/Delphi ya registrada en `docs/validation/2026-09-10_t5v2_dyndolod_stage9.md` (medida,
pre-existente, no introducida por este PR); el eco del binario resuelve la ruta a
`…\ini\\Skyrim.ini`.

## Autoridad de `-m:` (decisión de este PR)

`-m:` se cablea desde la declaración **explícita** del operador (`DYNDLOD_INI_DIR`, validada
por `PathResolutionService.get_dyndolod_ini_dir`). No hay derivación automática:
`dyndolod.info` prohíbe apuntar a carpetas de perfiles de MO2 (*"Do not link to files or
folders in mod manager profiles"*) y `Documents\My Games\…` sería una heurística de nombre
(edición Steam/GOG/VR + Documentos redirigido). Lo que falta para automatizarlo: una
capacidad de resolución con su ADR (la primitiva de identidad ya existe en
`app/security/known_folders.py`).
