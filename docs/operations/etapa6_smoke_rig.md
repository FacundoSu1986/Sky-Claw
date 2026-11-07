# Smoke de la etapa 6 asistida en el rig Windows

> **Audiencia:** operador que valida en hardware real lo cerrado en #862/#864/#870.
>
> **Estado:** guía de procedimiento. **Ninguna corrida de esta guía fue ejecutada**: se armó
> leyendo el código de `origin/main` `f9f81b7`. Lo que no se pudo confirmar leyendo está marcado
> como *no verificado* y es lo primero que hay que anotar en el rig.
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
- Python 3.11 o 3.12 y `uv`.
- **Perfil MO2 descartable** (copia del real, o uno nuevo con 3-5 mods). Nunca el perfil de uso.
- Backup externo del perfil antes de empezar (`profiles/<perfil>/` completo).
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
| `MO2_PATH` / `--mo2-root` | instalación de MO2 (default de la CLI: `C:\MO2Portable`) |
| `SKYRIM_PATH` / `--skyrim-path` | carpeta del juego |
| `WRYE_BASH_PATH` | ejecutable de Wrye Bash (`get_wrye_bash_path`) |
| `MO2_PROFILE` / `--profile` | perfil **único** de la sesión (default `Default`; no se cambia en caliente) |

Tests previos (si fallan en Windows, **no sigas**):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_etapa6_vigencia_contexto.py tests/test_etapa6_testigo_durable.py tests/test_wrye_bash_lock.py -q
```

## 3. Bridge USVFS (precondición de los docs del repo)

Los docs piden validar el bridge antes de operar un perfil real
([safe_workflows](../user/safe_workflows.md), [real_rig_validation](real_rig_validation.md)).
Las fases asistidas no lo usan para ejecutar nada, así que **no verifiqué que sean un requisito
duro de la etapa 6**; hacelo igual para que el sensor de visibilidad del preflight mida algo real.

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
  *No verificado:* que la tarjeta figure como `Disponible` depende del escaneo y de
  `WRYE_BASH_PATH`; anotá el estado que ves.
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
   plugin esté **activo** (pasos literales de `PASOS_ASISTIDOS`).
3. `verificar`. Esperado: `success=True`, `motivo=OK`, `frescura_probada=True`,
   `testigo_contexto=coincide`. Dónde cae el archivo (`overwrite/` o mod de salida) queda en
   `artefacto`: anotalo.

### E2 — Vigencia: LOOT entre build y verify (el caso central de H-1)

1. `preparar`; 2. construir el parche; 3. **correr LOOT y ordenar**; 4. `verificar`.
Esperado según el docstring de `EstadoDelContexto`: `testigo_contexto=cambio`, el testigo no se
usa y `frescura_probada=False`; hay que volver a preparar. *No verificado en hardware:* los
valores exactos de `success`/`motivo` en este caso; anotalos.

### E3 — Cierre de GUI entre fases (testigo durable)

`preparar` → cerrar Sky-Claw por completo → reabrirlo → construir → `verificar`. Esperado:
`testigo_origen=durable`, `testigo_contexto=coincide`. Mirá que exista `etapa6_testigos.json`.

### E4 — Verificar sin construir

`preparar` → `verificar` sin tocar Wrye Bash. Esperado: `motivo=SIN_CAMBIO` o
`ARTEFACTO_AUSENTE` según haya o no un parche previo.

### E5 — Parche construido pero deshabilitado

Construir y desmarcarlo en MO2 → `verificar`. Esperado: `motivo=NO_HABILITADO`.

### E6 — Defender y escritura atómica

Repetí `preparar` 10-20 veces seguidas con Defender activo y, si podés, con un escaneo en
curso. Esperado: ninguna falla de escritura del almacén (el código reintenta hasta 5 veces con
backoff ante `winerror` 5/32). Anotá cualquier error y mirá que no queden archivos temporales
al lado de `etapa6_testigos.json`. *No verificado:* el prefijo exacto de los temporales.

### E7 — Corte a mitad de `preparar`

Matá el proceso de Sky-Claw durante una preparación. Reiniciá y repetí `preparar`; el JSON debe
seguir siendo legible y no quedar un temporal huérfano tras la siguiente escritura (el barrido
`_barrer_temporales_huerfanos` corre al escribir).

### Fuera de alcance del rig

El caso «payload con `generacion` cuyo registro ya no existe → `no_verificable` y fail-closed»
**no se reproduce desde las superficies**: el registry del agente solo reenvía `fase` y la GUI
arma el payload sola. Está cubierto por `tests/test_etapa6_vigencia_contexto.py`.

## 6. Qué registrar

Versión de Windows, de MO2, de Wrye Bash y de LOOT; versión de USVFS si MO2 la informa; SHA de
Sky-Claw (`git rev-parse HEAD`); perfil usado; resultado completo (JSON) de cada fase en cada
escenario; contenido de `etapa6_testigos.json` (no contiene secretos, pero revisalo antes de
adjuntarlo) y los logs de Sky-Claw y de MO2. Un escenario no ejecutado se registra como *no
ejecutado*, no como verde.

## 7. Criterio de parada

Abortar si cambia el perfil, si el preflight devuelve rojo por una causa que no entendés, si el
`Data` físico deja de ser escribible, o si algún escenario escribe fuera de `overwrite/`, del mod
de salida o de `%USERPROFILE%\.sky_claw\state\`.

## 8. Limpieza

Restaurá el perfil desde el backup. Para empezar de cero el estado de la etapa 6, cerrá
Sky-Claw y borrá solo `etapa6_testigos.json` (y su `.lock`); el efecto es perder el testigo
previo, no el parche ni el perfil. Hacelo recién cuando hayas guardado la evidencia.
