# Smoke de la etapa 6 asistida en el rig Windows

> **Audiencia:** operador que valida en hardware real lo cerrado en #862/#864/#870.
>
> **Estado:** guía de procedimiento, **ejecutada el 2026-11-07** en un rig sandbox sobre
> `origin/main` `749c9716` (E1–E7 PASS, bridge USVFS **no validado**; ver §9). Se armó leyendo el
> código de `f9f81b7` y se corrigió con lo observado. Lo que la corrida no cubrió sigue marcado
> como *no verificado*.
>
> **Fuentes leídas:** `sky_claw/local/tools/wrye_bash_service.py`,
> `wrye_bash_asistida.py`, `etapa6_testigos.py`, `sky_claw/config.py`,
> `sky_claw/app/agent/tools/__init__.py`, `app/gui/controllers/ritual_runner.py`,
> `app/core/path_resolver.py`, `__main__.py`, ADR 0016, y los docs
> [real_rig_validation](real_rig_validation.md), [instalación](../user/installation.md),
> [configuración](../user/configuration.md), [gui](../user/gui.md).

## Qué se prueba y qué no

Las dos fases **no spawnean nada**: el Bashed Patch lo construye el humano en la GUI de Wrye
Bash lanzada desde MO2. Sky-Claw solo lee (`plugins.txt`, `modlist.txt`, el artefacto) y escribe
dos cosas: el write-probe `.skyclaw_probe_<uuid>.tmp` del preflight y el registro del testigo.
Por eso el smoke valida cuatro propiedades que los tests con mocks no pueden probar:

1. el artefacto se encuentra en el **overlay real** (`overwrite/` o mod de salida) bajo USVFS;
2. el testigo durable sobrevive entre `preparar` y `verificar` aunque se cierre la GUI;
3. la huella de contexto detecta que LOOT (o un cambio de mods) invalidó el testigo;
4. la escritura atómica del almacén aguanta Defender / indexador.

## 1. Requisitos

- Windows 10/11, Skyrim SE/AE, Mod Organizer 2, Wrye Bash y LOOT instalados.
- **Juego sandbox con `SkyrimSELauncher.exe`** (marcador con el que Wrye Bash detecta la
  instalación). Sin él, Wrye Bash rechaza `-o` **y cae a la instalación real de Steam**, y
  escribe fuera del sandbox (incidente H-2, §9). Verificá el marcador antes de abrir Wrye Bash.
- **El ejecutable de Wrye Bash en MO2 lleva `-o "<carpeta del juego sandbox>"` en sus argumentos**,
  con ruta **absoluta** (Wrye Bash resuelve `-o` relativo a su carpeta `Mopy`).
  `SKYRIM_PATH` y `skyrim_path` configuran Sky-Claw, **no** seleccionan el juego de Wrye Bash.
  Según el código de Wrye Bash v315 (`bush.py`, leído, no ejecutado), un `-o` que no valida **no
  aborta ni avisa con un diálogo**: Wrye Bash sigue con `sOblivionPath` de `bash.ini`, el directorio
  padre de `Mopy` y la detección por registro o Steam, y si encuentra un solo juego lo selecciona en
  silencio. Es el mecanismo de H-2.
- **Protegé el juego real antes de lanzar Wrye Bash por primera vez**, no solo verificarlo después.
  Lo observado en H-2 es que, al caer al juego real, Wrye Bash creó carpetas y un parche vacío ahí.
  **No está verificado si lo hace durante el arranque o recién al construir**, así que tratá el
  arranque como riesgoso. Opciones, de más a menos segura: hacer el smoke en una máquina o usuario
  donde el juego real no sea detectable (sin él, Wrye Bash muestra «could not find a game» y
  termina, que es el fallo seguro); mover o renombrar temporalmente la carpeta del juego real; o
  denegar la escritura sobre ella a nivel de permisos del sistema de archivos. Restaurá al final.
- **Aislá también el estado propio de Wrye Bash.** Si el juego sandbox y el real comparten carpeta
  padre, Wrye Bash puede usar el mismo `Skyrim Special Edition Mods\Bash Mod Data` para ambos y
  sobrescribir ahí datos del juego real (afirmación de la revisión sobre `initialization.py` y
  `basher/__init__.py` de v315, **no verificada por nosotros**). Tres controles, los dos primeros
  observados en la corrida del 2026-11-07: (1) poné el juego sandbox en una carpeta padre **distinta**
  de la del juego real; (2) configurá el `bash.ini` de Wrye Bash para que sus rutas de datos queden
  dentro del sandbox (en el rig terminaron en `overwrite/` bajo USVFS; las claves exactas no están
  verificadas acá: tomalas del `bash.ini` usado en el rig, en su paquete de evidencia); (3) antes de
  empezar, comprobá si ya existe `<padre del juego real>\Skyrim Special Edition Mods` y, si existe,
  respaldalo y restauralo al final.
- **Abrí MO2 a mano desde el Explorador.** Un MO2 lanzado por un proceso no interactivo no
  inyecta USVFS (`ERROR_ACCESS_DENIED` para cualquier exe; hallazgo F3, reconfirmado).
- Python 3.11 o 3.12 y `uv`.
- **Instancia MO2 aislada** (la corrida del 2026-11-07 usó un sandbox completo) con un perfil
  descartable de 3-5 mods. Nunca el perfil de uso.
- Backup externo **antes de empezar**. El de `profiles/<perfil>/` **no alcanza**: una copia del
  perfil dentro de la misma instancia comparte `overwrite/` y los mods de salida con el perfil real,
  y Wrye Bash puede modificar ahí un parche existente. Si no aislás la instancia, respaldá y
  restaurá también `overwrite/` y cada mod de salida que pueda tocar.
- Defender de Windows **activo** (es parte de la prueba).

## 2. Instalar y configurar

```powershell
git clone https://github.com/FacundoSu1986/Sky-Claw
cd Sky-Claw
uv sync --locked --extra dev
.\.venv\Scripts\python.exe local_scripts\scripts\first_run.py
```

Variables que lee `PathResolver` (todas verificadas en
[configuración](../user/configuration.md) y `path_resolver.py`):

| Variable | Para qué |
|---|---|
| `mo2_root` (config) / `--mo2-root` | instalación de MO2 que **inyecta `AppContext`**. **Una `mo2_root` de la config que exista gana sobre `--mo2-root`** aunque este indique otra ruta; la CLI solo manda si la config no la define o su ruta no existe. Gana también sobre `MO2_PATH` (el resolver solo lo mira si no hubo instalación inyectada). Fijá la ruta del **sandbox en la config antes de iniciar** y confirmá la instancia seleccionada; no te apoyes solo en `--mo2-root` |
| `SKYRIM_PATH` + `skyrim_path` (config) | carpeta del juego **para las fases asistidas**: `get_skyrim_path()` lee solo la variable, pero las raíces permitidas del sandbox salen de `skyrim_path` de la config (`AppContext`). **Poné las dos en el sandbox antes de iniciar**: con la config en el juego real y la variable en el sandbox, el validador puede rechazar la ruta y las fases quedan sin ruta resoluble. La GUI exporta la variable desde el escaneo con `setdefault`, así que una variable ya definida gana |
| `--skyrim-path` | solo lo consume `vfs-health` (§3); **no** alimenta el resolver de las fases asistidas |
| `WRYE_BASH_PATH` | ejecutable de Wrye Bash (`get_wrye_bash_path`) |
| `MO2_PROFILE` / `--profile` | perfil **único** de la sesión (default `Default`; no se cambia en caliente) |

Tests previos (si fallan en Windows, **no sigas**):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_etapa6_vigencia_contexto.py tests/test_etapa6_testigo_durable.py tests/test_wrye_bash_lock.py -q
```

## 3. Bridge USVFS (precondición de los docs del repo)

Los docs piden validar el bridge antes de operar un perfil real
([safe_workflows](../user/safe_workflows.md), [real_rig_validation](real_rig_validation.md)).
Las fases asistidas no lo usan para ejecutar nada y el preflight del dominio USVFS omite a
propósito el sensor de visibilidad: **no es requisito de la etapa 6** (E1–E7 pasaron con el bridge
sin validar). Sí lo piden los docs del repo para operar un perfil real, y la validación del bridge
sigue pendiente (§9, H-7).

```powershell
.\dist\SkyClawApp.exe --mode install-vfs-bridge --mo2-root "D:\MO2Portable"
# reiniciar MO2, luego:
.\dist\SkyClawApp.exe --mode vfs-health --mo2-root "D:\MO2Portable" --skyrim-path "<Skyrim>" --vfs-profile "<perfil>"
```

`install-vfs-bridge` exige el ejecutable congelado (`build.bat` lo genera en `dist\`);
`SKYCLAW_ALLOW_DEV_WORKER=1` es solo vía de desarrollo y un `python.exe` de venv puede ser
rechazado por USVFS.

## 4. Arrancar

```powershell
.\.venv\Scripts\python.exe -m sky_claw --mode gui --profile "<perfil>"
```

También sirve `SkyClawApp.bat`, pero **no** pasa `--profile`: usa `MO2_PROFILE` o `Default`.

Dónde queda el estado (verificado en `config.py` y `etapa6_testigos.py`):

```
%USERPROFILE%\.sky_claw\state\etapa6_testigos.json     # el almacén durable
%USERPROFILE%\.sky_claw\state\.etapa6_testigos.json.lock
```

## 5. Escenarios

Cómo se invocan las fases, por superficie (las tres comparten `ejecutar_fase_asistida`):

- **GUI:** tarjeta *Wrye Bash* → `Ejecutar` dispara `preparar`. Tras la preparación aparece la
  acción «Ya construí el parche — verificar» (etiqueta en `ritual_runner.py`).
  **Ojo:** la tarjeta **no lee `WRYE_BASH_PATH`** (`ExternalToolSpec` de `wrye_bash` en
  `sky_claw/local/discovery/registry.py` no tiene `config_field`). Figura `Disponible` solo si el
  exe está en una ruta auto-detectada, p. ej. dentro de la carpeta del juego (H-1, §9).
- **Agente (Telegram `--mode telegram`, o `/api/chat`):** tool `generate_bashed_patch` con
  `fase="preparar"` o `fase="verificar"`. El perfil lo fija la sesión, no el modelo.

Campos que vas a leer en los resultados (de `preparacion_como_resultado` /
`veredicto_como_resultado`): fase 1 → `success=False`, `pendiente=True`,
`motivo=ESPERANDO_OPERADOR`, `testigo_previo` (con `generacion`), `estado_en_plugins`,
`instrucciones`; fase 2 → `success`, `motivo`, `sha256`, `bytes`, `estado_en_plugins`,
`frescura_probada`, más `testigo_origen` (`payload`/`durable`/`ninguno`) y `testigo_contexto`
(`coincide`/`cambio`/`no_verificable`/`no_aplica`).

### E1 — Camino feliz

1. `preparar`. Esperado: `pendiente=True`, `motivo=ESPERANDO_OPERADOR`. Anotá `generacion`.
2. En MO2 lanzá **Wrye Bash desde el ejecutable del perfil** (no desde el explorador), `Rebuild
   Patch` sobre `Bashed Patch, 0.esp`, solo *Leveled Lists*, confirmar, cerrar, y verificar que el
   plugin esté **activo** (pasos literales de `PASOS_ASISTIDOS`). **Antes de `Rebuild Patch`**
   abrí `BashBugDump.log` y confirmá que Wrye Bash seleccionó el juego sandbox. Según `bash.py` de
   v315 (leído, no ejecutado) el archivo se sobrescribe en cada arranque, queda en la carpeta del
   ejecutable de Wrye Bash (bajo USVFS puede aparecer en `overwrite/`) y se escribe línea por línea,
   así que se puede leer con Wrye Bash abierto. Buscá la línea «Using … game:» o «Single game
   found», seguida de la ruta, y comprobá que sea la carpeta del sandbox que pasaste con `-o`. Si
   aparece «No known game in the path specified via -o argument», o cualquier otra ruta, **cerrá
   Wrye Bash y detené el smoke**. El texto exacto de esas líneas viene de la lectura del código, no
   de una corrida: confirmalo en el rig. Si no podés abrir el log, no construyas. Después de la
   corrida, comprobá además que el `Data` del juego real no cambió (fue lo que se hizo al limpiar
   H-2).
3. `verificar`. Esperado: `success=True`, `motivo=OK`, `frescura_probada=True`,
   `testigo_contexto=coincide`. Dónde cae el archivo (`overwrite/` o mod de salida) queda en
   `artefacto`: anotalo.

### E2 — Vigencia: LOOT entre build y verify (el caso central de H-1)

1. `preparar`; 2. construir el parche; 3. **correr LOOT y ordenar**; 4. `verificar`.
Observado (con un cambio de `modlist.txt`, no un segundo sort): `success=True`, `motivo=OK`,
`frescura_probada=False`, `testigo_origen=durable`, `testigo_contexto=cambio`, **registro no
consumido**. Es lo que declara ADR 0016 (§7.3): el artefacto existe y está habilitado, pero la
frescura no se prueba. **Un consumidor no puede leer solo `success`: tiene que leer
`frescura_probada`.**

### E3 — Cierre de GUI entre fases (testigo durable)

Superficie del **agente**: `preparar` → cerrar Sky-Claw por completo → reabrirlo → construir →
`verificar` sin payload. Esperado: `testigo_origen=durable`, `testigo_contexto=coincide`. Mirá que
exista `etapa6_testigos.json`. (La corrida del 2026-11-07 lo hizo así.)

Por la **GUI** el escenario es distinto: el resultado de `preparar` que crea la acción «Ya construí
el parche — verificar» vive en memoria y se pierde al cerrar. Al reabrir hay que volver a
`preparar`, y esa acción viaja con el testigo en el payload: el origen esperado es `payload`, no
`durable`. No uses la GUI para probar el origen `durable`.

### E4 — Verificar sin construir

`preparar` → `verificar` sin tocar Wrye Bash. Observado: `success=False`, `motivo=SIN_CAMBIO`,
`testigo_contexto=coincide`.

### E5 — Parche construido pero deshabilitado

Construir y desmarcarlo en MO2 → `verificar`. Esperado: `motivo=NO_HABILITADO`.

### E6 — Defender y escritura atómica

Repetí `preparar` 10-20 veces seguidas con Defender activo y, si podés, con un escaneo en
curso. Partí de un estado sin testigo pendiente y exigí `testigo_durable=guardado` en **cada**
iteración: una respuesta `conservado_por_build` no escribe el JSON y no cuenta como prueba de la
escritura atómica. Observado: 20 preparaciones, 0 fallos, almacén válido en las 20, sin `.tmp` huérfanos, con un
escaneo de Defender concurrente. Si repetís, anotá cualquier error y mirá que no queden temporales
al lado de `etapa6_testigos.json`.

### E7 — Corte a mitad de `preparar`

Matá el proceso de Sky-Claw durante una preparación. Reiniciá y repetí `preparar`; el JSON debe
seguir siendo legible. Observado: 6 cortes (0,05–0,95 s), almacén siempre legible, sin temporales.
**Límite:** ningún corte cayó dentro de la ventana `mkstemp`→`os.replace`, así que el barrido de
huérfanos no se forzó (lo cubren los tests).

### Fuera de alcance del rig

El caso «payload con `generacion` cuyo registro ya no existe → `no_verificable` y fail-closed»
**no se reproduce desde las superficies**: el registry del agente solo reenvía `fase` y la GUI
arma el payload sola. Está cubierto por `tests/test_etapa6_vigencia_contexto.py`.

## 6. Qué registrar

Versión de Windows, de MO2, de Wrye Bash y de LOOT; versión de USVFS si MO2 la informa; SHA de
Sky-Claw (`git rev-parse HEAD`); perfil usado; resultado completo (JSON) de cada fase en cada
escenario; contenido de `etapa6_testigos.json` (no contiene secretos, pero revisalo antes de
adjuntarlo) y los logs de Sky-Claw y de MO2.

**Tres niveles de evidencia, de menor a mayor fidelidad.** `/api/chat` devuelve la respuesta final
del modelo, y el router puede truncar o sanear el resultado de la tool antes de dárselo: una
transcripción de chat **no es** evidencia exacta. Un driver que llame a la tool por el
`AsyncToolRegistry` productivo (como el de la corrida del 2026-11-07) devuelve el JSON estructurado
completo, pero con `artefacto` y `plugins_txt` **saneados** por `generate_bashed_patch`
(`sanitize_for_prompt`: NFKC y barrido de caracteres invisibles), así que una ruta con caracteres de
ancho completo puede no coincidir con la real: **verificá esas rutas en disco**. Solo la llamada
directa a `WryeBashPipelineService.ejecutar_fase_asistida` conserva el resultado sin sanear. Si solo
tenés el chat, registrá esa evidencia como *no disponible*. Un escenario no ejecutado se registra como *no
ejecutado*, no como verde.

## 7. Criterio de parada

Abortar si cambia el perfil, si el preflight devuelve rojo por una causa que no entendés, si el
`Data` físico deja de ser escribible, o si algún escenario escribe fuera de `overwrite/`, del mod
de salida o de `%USERPROFILE%\.sky_claw\state\`.

## 8. Limpieza

Restaurá el perfil, `overwrite/` y los mods de salida desde el backup. Para empezar de cero el
estado de la etapa 6, **cerrá todos los procesos de Sky-Claw** y guardá antes la evidencia.
`etapa6_testigos.json` es **compartido por usuario** y trae entradas de cada combinación
`(plugins.txt, Data)`: borrarlo elimina también los testigos pendientes de otros perfiles o
juegos. Borralo entero (con su `.lock`) solo si ese JSON contiene únicamente entradas del smoke; si
no, quitá solo la entrada del perfil de prueba. El efecto es perder el testigo previo, no el parche
ni el perfil.

## 9. Resultado de la corrida del 2026-11-07

Rig sandbox, Windows 10 Pro 10.0.19045, MO2 2.5.2, Skyrim SE 1.6.1170, Wrye Bash 315, LOOT 0.29.2,
perfil descartable de 3 mods, Defender activo, Sky-Claw desde fuente en `749c9716`. El informe
completo y los JSON crudos están en la máquina del operador
(`C:\SkyClaw_Spike_Sandbox\evidence\etapa6-20261107\`), no en el repo.

| Escenario | Resultado |
|---|---|
| E1 camino feliz (GUI) | PASS, **derivado**: el JSON de la GUI no se persiste (H-5). Evidencia: mensaje de fase 1, registro durable en disco y consumo del registro al verificar |
| E2 vigencia | PASS: `OK`, `frescura_probada=False`, `testigo_contexto=cambio`, registro no consumido |
| E3 cierre de GUI | PASS: `frescura_probada=True`, `testigo_origen=durable`, registro consumido |
| E4 sin construir | PASS: `SIN_CAMBIO` |
| E5 deshabilitado | PASS: `NO_HABILITADO` |
| E6 Defender | PASS: 20/20 sin fallos, **pero** el informe no registra el valor de `testigo_durable` de cada preparación: no prueba que las 20 hayan escrito el JSON. Repetir con el criterio de E6 (`guardado` en cada iteración) |
| E7 corte | PASS con límite (ventana no alcanzada) |
| Bridge USVFS | **No validado**: cuarentena heredada y desajuste plugin/broker (`VfsBridgeLaunchError: job_object…`) |

Límites declarados: el perfil no tenía leveled lists, así que el Bashed Patch es mínimo (230 B) y
se validó existencia, activación y frescura, **no el contenido del merge**; la secuencia de E2 se
desordenó y el cambio de contexto se provocó con `modlist.txt` en lugar de un segundo sort de LOOT
(LOOT sí corrió y reordenó el perfil, registrado aparte); el write-probe del preflight es transitorio
y no se observó.

### Hallazgos

- **H-1 (producto, GUI).** La tarjeta de Wrye Bash no lee `WRYE_BASH_PATH`: con el exe fuera de las
  rutas auto-detectadas la superficie GUI de la etapa 6 es inalcanzable aunque el servicio sí
  resuelva la variable. Confirmado leyendo `registry.py`. Es el hermano de LOOT/xEdit/Pandora, que
  sí tienen `config_field`.
- **H-2 (entorno, seguridad operativa).** Wrye Bash cae al juego real si `-o` no valida y escribe
  fuera del sandbox. Se activó el criterio de parada, se limpió y se verificó el juego real. Ver el
  requisito del marcador en §1.
- **H-3 (entorno).** MO2 lanzado por el harness no inyecta USVFS (F3).
- **H-4 (producto, preflight).** `overwrite=yellow` permanente: los datos propios de Wrye Bash viven
  en `overwrite/` y el sensor de «overwrite sucio» los reporta en cada fase. Advertencia, no bloqueo.
- **H-5 (observabilidad).** La GUI no persiste el JSON del tool-result; para auditoría exacta usar
  la superficie del agente.
- **H-7 (bridge).** No validado; no invalida E1–E7.
