# Fuente de `-m:` por game mode (#601) — evidencia de rig

**Pregunta que responde:** con una carpeta declarada explícitamente en `-m:`
(`DYNDLOD_INI_DIR`), ¿qué archivo abre DynDOLOD para cada game mode, y qué pasa
si el archivo que busca no está?

**Conclusión** — es el contrato que implementa `_INI_PRIMARIA_POR_GAME_MODE`
(`sky_claw/local/tools/dyndolod_runner.py`):

| game mode | archivo primario que busca dentro de la carpeta de `-m:` | evidencia |
|---|---|---|
| `sse` | `Skyrim.ini` | escenario **A** (y **D**, sin `SkyrimPrefs.ini`) |
| `tes5vr` | `Skyrim.ini` | escenario **B** (la MISMA carpeta del escenario A) |
| cualquiera, con otra INI en la carpeta declarada | NO la usa | escenario **C**: busca `Skyrim.ini`, cae al directorio del juego y muere con `Fatal: Could not find ini` |

`SkyrimPrefs.ini` **no** es requisito de arranque: el escenario **D** arranca el
background loader con una carpeta que sólo contiene `Skyrim.ini`. Por eso no
entra en la tabla que valida el código.

## Procedencia

| Elemento | Valor |
|---|---|
| Instrumento | `rig_ini_source_probe.py` (esta carpeta): 4 escenarios, cierre suave `WM_CLOSE` para que el binario vuelque su log |
| Transcript | `transcript.txt` — salida literal del rig; `<RIG_WORK>` es el `--work` efímero de la corrida |
| Binario | `DynDOLODx64.exe` — DynDOLOD 3.0 Alpha-209 x64 (sesiones del rig: 2026-09-20 17:25:47, 17:26:17, 17:26:47 y 17:27:17, hora local; ver `log-crudo/`) |
| SHA-256 del binario | `b67625eb7815111ba9ac232c626ff88b07bb64ff30ff5183c5a04b5f045c3bd0` |
| Artefactos crudos | `artifacts/<escenario>/log-crudo/{DynDOLOD_*_log.txt, DynDOLOD_*_Debug_log.txt}` (copia del log de esa sesión; el sha256 de cada uno está en el transcript) |
| Corroboración previa | `log-crudo/manual-probe-1304/` — la corrida manual del mismo día (13:04, local) que originó el contrato, antes de este rig; saneada con las MISMAS reglas |

Las copias crudas se **sanean** antes de copiarse (home del operador →
`%USERPROFILE%`, carpeta del binario → `<TOOL_DIR>`, `--work` → `<RIG_WORK>`, mismo
criterio que el transcript y que la evidencia de #593), porque viajan en el
repositorio; el sha256 que publica el transcript es el del **artefacto saneado**
que se puede verificar contra el archivo commiteado
(`artifacts/**` y `log-crudo/**` están marcados `-text` en el `.gitattributes` de
esta carpeta para que `core.autocrlf` no altere los bytes).

El `-d:`, `-o:`, `-t:` y `-p:` de cada escenario son sintéticos (bajo `--work`) y
se declaran en el transcript; el juego nunca se toca y la corrida se cierra
después de la etapa en la que el binario resuelve sus rutas.

## Fuente primaria del nombre del archivo (xEdit)

DynDOLOD/TexGen heredan el core de xEdit — el propio log de la sesión cita
`https://github.com/TES5Edit/TES5Edit`. En `xEdit/xeInit.pas` (rama `dev-4.1.6`):

- `wbTheGameIniFileName := wbMyGamesTheGamePath + wbGameName + '.ini';`
- caso `isMode('SSE')`: `wbGameName := 'Skyrim'` — `wbGameName2 := 'Skyrim Special Edition'`
- caso `isMode('TES5VR')`: `wbGameName := 'Skyrim'` — `wbGameName2 := 'Skyrim VR'`
- y el fallback de los modos VR, con su comentario en el fuente: *"VR games don't
  create ini file in My Games by default, use the one in the game folder"*.

O sea: **el nombre del archivo sale de `wbGameName`, que es el mismo para los dos
modos; lo que cambia por modo es la CARPETA** (`My Games\Skyrim Special Edition`
vs `My Games\Skyrim VR`) — y la carpeta es justamente lo que declara `-m:`. El
escenario C muestra el fallback en acción: con una carpeta declarada sin
`Skyrim.ini`, el binario busca `<juego>\Skyrim.ini` (el padre de `-d:`), no
`SkyrimVR.ini`.

Doc oficial: `dyndolod.info/Help/Command-Line-Argument` define `-m:` como *"path
to INI folder"* sin nombrar archivo, y `dyndolod.info/Mods/Skyrim-VR` declara que
TES5VR usa los configs con el identificador SSE.

## Reproducir

```bash
python rig_ini_source_probe.py --exe <ruta a DynDOLODx64.exe> --work <dir-temporal> \
                               --transcript transcript-local.txt
```

El script crea sus fixtures (carpetas de INI, `Skyrim.esm` vacío, `plugins.txt`),
lanza el binario por escenario, espera la marca en el log, cierra con `WM_CLOSE`
y emite PASS/FAIL por escenario comparando las líneas del log. Sale con 0 sólo si
los cuatro coinciden con lo que este contrato afirma.

## Límites (lo que esta evidencia NO afirma)

- **No se pulsa Start ni se genera LOD**: la corrida se cierra tras la etapa de
  resolución de rutas, que es lo que la pregunta necesita. No es evidencia de una
  corrida completa (para eso está la evidencia de T5-v2 / #593).
- Con el `-d:` sintético, el background loader aborta con errores del propio tool
  (aserciones sobre un Data vacío). Esas líneas NO son parte del veredicto.
- **No cubre TexGen**: el binario medido es DynDOLOD. El core es el mismo (mismo
  repo fuente y mismas cadenas en las tablas de strings de `TexGenx64.exe`), y la
  evidencia de #593 tiene el eco de TexGen con `Using ini: …\Skyrim.ini`, pero la
  corrida de este rig no se repitió con TexGen.
- El `taskkill /F` **no** vacía el log del binario (medido en la primera corrida
  de este rig: los cuatro escenarios quedaron sin líneas). Por eso el instrumento
  cierra con `WM_CLOSE` y sólo usa el kill forzado como fallback.
