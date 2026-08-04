# Auditoría del módulo "Wrye Bash Headless" propuesto — 2026-08-04

Auditoría adversarial de un módulo de ~150 líneas presentado como *"extracción
directa del código fuente real de Wrye Bash"* (`Mopy/bash/bolt.py`,
`Mopy/bash/bosh/plugins.py`, `Mopy/bash/bosh/patcher/base.py`), propuesto para
que Sky-Claw genere el Bashed Patch (stage 6 del SOP) de forma desatendida.

Los hallazgos de comportamiento fueron verificados **ejecutando el snippet**
contra plugins TES4 sintéticos, no por inspección. Se marca **CONFIRMADO**
(reproducido) o **REFUTADO** (hipótesis que se probó y no se sostiene).

Sin nitpicking: solo integridad arquitectónica, concurrencia y corrección.

**Veredicto: no integrar.** Reintroduce exactamente la regresión que
`WryeBashHeadlessUnsupportedError` fue instalado para cerrar, y duplica —peor—
el parser TES4 canónico que ya consumen dos sensores de preflight.

---

## 🎯 1. Diagnóstico de integración y compatibilidad headless

### La eliminación de `wx`/`basher` es real pero irrelevante

No hay imports de `wx` ni `basher`. Pero eso no es lo que hacía a Wrye Bash
dependiente de GUI: **junto con la GUI se eliminó el motor**. Lo que queda es un
lector de header TES4 (~60 líneas) y un `execute_headless_patch()` cuyo paso de
construcción es un comentario:

```python
# 2. Aquí Skyclaw escribiría el archivo .esp binario resultante usando los registros mergeados
patch_path = os.path.join(self.data_folder, output_patch_name)   # ← calculado y nunca usado
logger.info(f"Parche compilado con éxito en: {patch_path}")      # ← miente
return "SUCCESS"
```

No se abre ningún archivo en modo escritura en todo el módulo.

### El repo ya decidió lo contrario, con verificación upstream

`sky_claw/local/tools/wrye_bash_runner.py:114` — `generate_bashed_patch()` es
`NoReturn` y falla cerrado a propósito. El docstring de
`WryeBashHeadlessUnsupportedError` (líneas 28-65) documenta la verificación
contra `Mopy/bash/barg.py`: `-b/--backup` es backup de *settings*
(`store_true`, destino en `-f`), y el parser no declara posicionales. Ninguna
opción reconstruye el parche.

Las líneas 50-54 predicen literalmente este snippet:

> "El riesgo que esto cierra hacia adelante es el simétrico: cualquier variante
> del comando que SÍ parsee […] saldría con código 0, y el runner reportaría
> `success=True` **sobre un parche que no se generó** — con las etapas
> siguientes del DAG (Synthesis en la 7, DynDOLOD en la 9) consumiendo un
> artefacto viejo."

El snippet no necesita ni ejecutar Wrye Bash para producir ese fallo: devuelve
`"SUCCESS"` directamente. Integrarlo revierte una decisión de arquitectura
tomada, documentada y verificada, sin nombrarla.

### Idoneidad como Tool: falla el contrato en cuatro ejes

| Eje | Requiere Sky-Claw | Snippet |
|---|---|---|
| Retorno | `dict` con `success: bool` + `message: str` (`tool_result.py`) | `str` `"SUCCESS"` / `"ERROR: …"` |
| Async | `async def`, `run_capture` / `asyncio.to_thread` | sync bloqueante |
| Sandbox | `PathValidator.validate()` / `assert_safe_component` | `os.path.join` crudo |
| Cableado | **ambos** caminos: `RITUAL_TOOL_MAP` + strategy **y** `AsyncToolRegistry` | ninguno |

El cuarto punto es el defecto #1 del repo según `AGENTS.md` ("dos superficies,
un recurso": 9 de 13 follow-ups auditados). Un módulo suelto no participa de
lock, journal, preflight, rollback ni HITL — todo lo que
`WryeBashPipelineService.execute_pipeline` ya provee.

### ⚖️ Procedencia y licencia

El snippet declara origen en tres archivos de Wrye Bash y trae cabecera
**GNU GPL v2**. Dos observaciones, en orden de confianza:

1. **El código no muestra linaje de Wrye Bash** (confianza alta, por lectura):
   cero `brec`, `MreRecord`, `ModFile`, `LoadOrder`, `bolt.Path` — ni ninguna de
   las abstracciones sobre las que está construido el parser real. La
   descripción de `bolt.py` como *"motor de bajo nivel para lectura/escritura de
   streams de bytes binarios"* tampoco corresponde: upstream son utilidades
   generales (paths, logging, structs).
2. **La atribución concreta no se pudo verificar contra el repo público** en
   esta auditoría. La existencia de `Mopy/bash/barg.py` sí está verificada (por
   el trabajo previo documentado en `wrye_bash_runner.py`); la de
   `Mopy/bash/bosh/plugins.py` y `Mopy/bash/bosh/patcher/base.py`, no.

**Riesgo:** si el código es original y no derivado, la cabecera GPL v2 y la
atribución son incorrectas y contaminarían el árbol con una obligación de
licencia infundada; si *sí* es derivado, entonces integrarlo impone GPL v2 sobre
los módulos que lo importen. Ambas ramas piden lo mismo: **no integrarlo sin
resolver la procedencia primero.** Esta auditoría no requiere resolverla porque
recomienda no integrar el código por motivos técnicos independientes.

---

## 🔴 2. Vulnerabilidades técnicas y bugs

### Hipótesis REFUTADAS (medidas, no asumidas)

- **REFUTADO · No hay fuga de file descriptors.** El `with open(...)` cubre
  todos los `raise` internos; `_parse_header()` se llama desde `__init__`, así
  que la excepción propaga sin dejar objeto a medio construir. Medido: 300
  parseos de un archivo inválido, fd count `4 → 4`.
- **REFUTADO · No hay agotamiento de memoria por `dataSize` corrupto.** Con
  `dataSize = 0xFFFFFFF0` (~4 GB) sobre un archivo de 64 bytes, `f.read(size)`
  devolvió 40 bytes en 0.000s, delta de RSS 0 MB — el buffered reader no
  preasigna. (Igual conviene capear; el parser del repo lo hace con
  `_MAX_TES4_DATA_SIZE`.)
- **REFUTADO · El desempaquetado del header es correcto.** `<4sIIIIH` = 22
  bytes leídos de un buffer de 24, y el cursor del archivo queda en 24 porque el
  `f.read(24)` ya avanzó. `dataSize` excluye el header, así que el `f.read(size)`
  siguiente es correcto. No es un bug.

### V-1 · CRÍTICO · CONFIRMADO — todos los modos de fallo convergen en `"SUCCESS"`

| Escenario ejecutado | Retorno | Correcto |
|---|---|---|
| Carpeta `Data` vacía, load order de 4 plugins | `'SUCCESS'` | error |
| `MiMod.esp` **antes** de su master `Skyrim.esm` | `'SUCCESS'` | error (CTD) |
| 1 de 3 plugins corrupto/ilegible | `'SUCCESS'` | error o warning |
| Archivo de parche escrito en disco | **`False`** | — |

Para un bucle OODA es el peor fallo posible: no es un error, es un **éxito
confabulado**. El agente concluye que `Bashed Patch, 0.esp` existe, lo agrega al
load order y encadena Synthesis (stage 7) y DynDOLOD (stage 9) sobre un
artefacto inexistente. No hay señal para disparar rollback porque nada reportó
fallo.

### V-2 · CRÍTICO · CONFIRMADO — MO2/USVFS: no encuentra ningún plugin de mod

`os.path.join(self.data_folder, plugin_name)` asume que todos los plugins viven
en el `Data` real. Bajo MO2 viven en `mods/<NombreMod>/` y solo se ven
unificados dentro del VFS; Sky-Claw corre fuera de él. Cada plugin de mod cae en
el `continue` silencioso de `analyze_load_order`, `deps` queda casi vacío, y no
hay masters que validar → `"SUCCESS"`. Es el caso **normal**, no el borde.

El repo ya lo resolvió: `MissingMastersChecker` toma `plugin_dirs: Sequence[Path]`
y `resolve_plugin_sources()` los ordena por precedencia `overwrite → mods → Data`.

### V-3 · CRÍTICO · CONFIRMADO — comparación de masters sensible a mayúsculas

```python
loaded_set = set(self.parsed_plugins.keys())   # del load order, p.ej. "skyrim.esm"
if master not in loaded_set:                   # MAST dice "Skyrim.esm"
```

Verificado: `'Skyrim.esm' not in {'skyrim.esm', …}` → `True`. Produce **falsos
positivos**: el agente recibe "requiere Skyrim.esm" sobre un master presente y
habilitado, y actúa sobre un problema inexistente. `MissingMastersChecker` usa
`.casefold()` en ambos lados.

### V-4 · ALTO · CONFIRMADO — `sub_size` sin bounds check produce masters basura

```python
sub_data = reader.data[reader.offset:reader.offset + sub_size]   # slice: trunca en silencio
reader.offset += sub_size                                        # puede pasar el final
```

El slicing de Python no lanza al desbordar. Con un `MAST` de tamaño declarado
60000 sobre un body pequeño, el parser devolvió `['Skyrim.esm\x00DATA\x08']`
**sin excepción**: se comió el subrecord siguiente. Ese nombre nunca matchea en
disco → falso "master faltante" ilegible. Y el `except EOFError: break`
convierte cualquier corrupción posterior en fin-de-parseo silencioso.

### V-5 · ALTO · CONFIRMADO — `except Exception` descarta plugins sin señal

`dependency_map` vuelve sin la entrada y **con la misma forma** que un análisis
completo. El llamador no puede distinguir "40 plugins OK" de "20 OK y 20
ilegibles". `MissingMastersChecker` emite `MasterIssue(kind="unreadable")`, un
dato de primera clase.

### V-6 · MEDIO · CONFIRMADO — `latin1` en lugar de `cp1252`

Verificado con el byte `0x92` (comilla tipográfica): `latin1` →
`'Mod\x92s Patch.esp'` (mojibake, no matchea en disco); `cp1252` →
`'Mod's Patch.esp'`. Además `errors='ignore'` sobre `latin1` es código muerto:
latin1 nunca falla al decodificar.

### V-7 · MEDIO · CONFIRMADO — no valida el orden, solo la presencia

Verificado: `["MiMod.esp", "Skyrim.esm"]` → `"SUCCESS"`, y es un CTD
garantizado. El método dice "validar la estabilidad del juego" y no valida el
eje que importa.

**Este era también un hueco genuino del repo** — ningún validador cubría el
orden. Cerrado en el mismo PR que esta auditoría con
`sky_claw/local/validators/master_order.py`.

### V-8 · MEDIO · CONFIRMADO — estado rancio entre invocaciones

`analyze_load_order` acumula en `self.parsed_plugins` sin limpiar. Verificado:
tras reducir el load order de 2 plugins a 1, `parsed_plugins` siguió en 2.

### V-9 · MEDIO — I/O bloqueante en un runtime asyncio

`open()`/`read()`/`os.path.exists()` sincrónicos, ~2 syscalls por plugin × hasta
254 plugins, sobre rutas que en Windows pueden estar detrás del filtro de USVFS.
El repo usa `await asyncio.to_thread(...)` para I/O de archivos.

### V-10 · MEDIO — sin sandboxing de rutas

`plugin_name` viene de `plugins.txt` o del LLM. `os.path.join` **descarta la
base si el segundo componente es absoluto** (`join('C:/Data', 'D:/evil.esp')` →
`'D:/evil.esp'`) y no filtra `..`. El repo tiene `assert_safe_component()` y
`PathValidator.validate()` para esto.

---

## ⚠️ 3. Puntos ciegos del pipeline y módulos faltantes

El Bashed Patch **es** el merge; el header TES4 es solo cómo se entra. Falta el
100% del motor: merge de Leveled Lists (la razón de existir de la stage 6),
parseo de `GRUP` y records normales, descompresión zlib, remapeo de FormIDs,
reconstrucción del bloque `MAST` del parche, serialización del `.esp`, e
importers (Names, Stats, Graphics, Inventory, Tweaks).

Puntos ciegos específicos del SOP de este repo:

- **Regla dura §0.4 / §2.6:** "NUNCA usar Wrye Bash para mergear magic effects,
  parámetros acústicos o costes de hechizos → multiplica costes de maná.
  **Leveled Lists ONLY**." Un merger nuevo sin esa restricción cableada es
  exactamente el fallo que la regla prohíbe.
- **Dependencia de stage:** la 6 exige LOOT (stage 5) previo. Sin guard.
- **Header 1.71 / BEES:** el snippet lee `version` del header y lo descarta —
  justo el campo que distingue 43 (LE sin portear) de 44 (SE), que
  `PluginHeader.form_version` conserva (T-21).
- **Pools full/light:** expone `is_light_plugin` y nunca lo usa; `plugin_limits`
  ya cuenta 254 full / 4096 light por flags reales (caso ESPFE).

### Lo que ya existe y el snippet ignora

| Necesidad | Ya en el repo |
|---|---|
| Parser TES4 | `local/validators/plugin_header.py::read_plugin_header` — XXXX, cp1252, cap de tamaño, `form_version`, errores tipados |
| Validación de masters | `local/validators/missing_masters.py::MissingMastersChecker` |
| Orden de masters | `local/validators/master_order.py::MasterOrderChecker` (nuevo, este PR) |
| Límites de plugins | `local/validators/plugin_limits.py` |
| Load order de MO2 | `local/mo2/load_order.py`, `local/mo2/plugin_sources.py` |
| Lock/journal/rollback/preflight/HITL | `local/tools/wrye_bash_service.py::WryeBashPipelineService` |
| Nombre y destino del parche | `BASHED_PATCH_NAME`, `output_targets.py::bashed_patch_target` |

---

## 🚀 4. Corrección aplicada y pendiente

### Aplicado en este PR

1. **`local/validators/master_order.py`** — cierra V-7, el único hallazgo que
   además era hueco propio del repo. Detecta masters que cargan después de su
   dependiente, reusando `read_plugin_header` (no abre un segundo parser).

   El detalle que lo hace correcto: el motor **hoistea el bloque de masters**
   (flag ESM o extensión `.esm`/`.esl` cargan antes que cualquier `.esp` plano),
   así que comparar índices crudos de `plugins.txt` daría falsos positivos
   masivos. `effective_load_order()` reordena primero.

   Separación deliberada: master ausente o header ilegible **no** se reportan
   acá — ya son issues de `missing_masters`, y duplicarlos haría que el semáforo
   cuente dos veces el mismo problema.

   **Cableado** en los tres servicios que consumen un load order ya
   estabilizado: Wrye Bash (stage 6), Synthesis (7) y DynDOLOD (9). **LOOT
   (stage 5) queda excluido a propósito:** es la *remediación* de un orden
   inválido, así que bloquearlo por orden inválido dejaría al usuario sin forma
   de arreglarlo — un deadlock. La exclusión está congelada por un test que
   enumera la familia, no por un comentario.

2. **`tests/test_tes4_parser_invariant.py`** — ancla AST que congela el conjunto
   de módulos que parsean headers TES4 en `{plugin_header.py}`. Un segundo
   parser rompe el test. Incluye un meta-test que verifica que **el snippet
   auditado habría roto el ancla**: es la afirmación que le da sentido al
   archivo. Verificado además inyectando un parser intruso real (rojo) y
   quitándolo (verde).

3. **Deduplicación de `_index_available`** — era byte-idéntico en
   `missing_masters` y `plugin_limits`. Extraído a
   `plugin_header.index_plugin_files(plugin_dirs, *, log)`, que ahora usan los
   tres sensores. El `log` inyectado preserva el logger de cada sensor (y con él
   los tests de degradación por `OSError`).

### Pendiente (fuera del alcance de este PR)

**Stage 6 como *etapa asistida*** — la decisión que el docstring de
`WryeBashHeadlessUnsupportedError` deja planteada y que sigue sin tomarse:
preflight → snapshot bajo `SnapshotTransactionLock` → lanzar Wrye Bash vía MO2
con `spawn_detached` (dentro del VFS, única forma en que ve los plugins) →
**verificar el artefacto** (existe, mtime posterior, header parseable con
`read_plugin_header`, `MAST` coherentes con el load order) → promover por HITL.

Ese paso de verificación es lo que convierte "el humano apretó Build" en un
hecho comprobable, y es lo único que justificaría levantar el fail-closed. Se
cablea en el service, no en los dos caminos por separado:
`GenerateBashedPatchStrategy` y `system_tools.generate_bashed_patch` ya delegan
en `WryeBashPipelineService`.

**Hueco de contrato:** Wrye Bash es el único service con salida ausente de
`tests/test_tool_result_contract.py`.
