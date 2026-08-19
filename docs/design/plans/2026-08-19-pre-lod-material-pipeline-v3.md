# Pre-LOD Material Pipeline — Plan de integración · **v3**

> **Modo:** análisis y planificación. Cero código, cero modificaciones a producción.
> Base: `main` @ `fd1d3c6`. **v3 corrige H1 e incorpora el paquete de evidencia completo del
> operador** (ParallaxR, BENDr, VRAMr, PGPatcher, Auto Parallax) con niveles de evidencia
> explícitos y política de no-vendorización.

---

## Context

Sky-Claw automatiza el pipeline de modding de Skyrim SE/AE sobre MO2. Su DAG canónico
(`sky_claw/local/AGENTS.md` §1) tiene 9 stages y termina en Stage 9 = TexGen → DynDOLOD.
El ecosistema moderno de materiales (parallax / complex material / PBR / optimización de
VRAM) vive **antes** del LOD y **no existía en el repo** en el snapshot auditado: `ParallaxR`,
`BENDr`, `PGPatcher`, `ParallaxGen`, `TruePBR`, `parallax`, `complex material` y `texconv` tenían
**cero ocurrencias en `main @ fd1d3c6`** (grep case-insensitive incluyendo docs y tests), el árbol
**anterior a incorporar este plan**. La afirmación está anclada a ese commit a propósito: esta
misma rama ya contiene la v3, así que la búsqueda repetida sobre el árbol actual **devuelve
coincidencias** (este documento), y no sería reproducible sin el ancla.

El objetivo es una rama semántica pre-LOD, con adapters independientes bajo un orquestador, que
converja antes de TexGen/DynDOLOD **sin renumerar Stage 9** y sin duplicar la infraestructura ya
madura del repo.

**Hallazgo estructural:** casi toda la infraestructura que el brief pedía construir **ya existe y
está bien testeada**. El trabajo real es de **extensión y cableado**.

---

## Qué cambió en v3

| # | Corrección | Motivo |
|---|---|---|
| 1 | **H1 corregido.** El toolchain de los helpers es **mixto**, no uniforme: PyInstaller/Python 3.11 en los de ParallaxR, Rust/clap en `HeightMap.exe`, `BENDr.exe` y los de VRAMr | v2 atribuyó a `ExtractBSA.exe` una lectura que en realidad correspondía al `BSA.exe` de VRAMr. Ver §2.2 |
| 2 | **B6-T sigue cerrado, por otra razón**: los BAT demuestran interfaces CLI invocables, con independencia del lenguaje de implementación | El razonamiento de v2 dependía de una propiedad falsa |
| 3 | **Tres niveles de evidencia** (T1 en sesión / T2 atestiguado / T3 web), aplicados fila por fila | Solo 3 artefactos son re-derivables desde esta sesión; el resto no |
| 4 | **VRAMr sube de "transcripción del brief" a verificado**: workflow, 7 helpers con args, presets con resoluciones exactas, detección de PBR | El paquete fue analizado; ya no es una cita del enunciado |
| 5 | **B3 reformulado**: workflow/args/presets VERIFICADOS; entry point headless oficial NO VERIFICADO | `VRAMr.exe` solo se pudo identificar como launcher |
| 6 | **PGPatcher: requisito de USVFS confirmado desde el binario** (`usvfs_x64.dll` + mensaje de error) | Deja de ser inferencia de changelog |
| 7 | **NUEVO — PGPatcher exige ownership EXCLUSIVO de su output** | String de `PGLib.dll`; cambia el diseño de `output_targets` y del preflight (§2.6 PG3) |
| 8 | **B5 sigue abierto pese a `--ignore-mo2vfscheck`**: existencia del flag ≠ corrección del resultado | El flag apaga un chequeo; no provee virtualización |
| 9 | **B4 sigue abierto**: no concluir que `pgtools.exe` sustituye 1:1 a `PGPatcher.exe` | Nombres compartidos son compatibles con "subconjunto" |
| 10 | **Auto Parallax resuelto**: plugin SKSE de runtime (DLL/INI/PDB), no una etapa. Nunca nodo del pipeline | Inventario completo del paquete + opción "Disable Pre-Patched Materials" de PGPatcher |
| 11 | **Política de no-vendorización** (§3.5): nada de `.exe`/`.dll`/`.zip`/paquetes Nexus/copias íntegras de BAT | Corrige a v2, que proponía versionar transcripciones completas |
| 12 | **Semántica de `_p.dds` precisada**, y `"Deleted source Parallax file"` de BENDr acotado a su propio workspace | Evita que un adapter borre trabajo del operador |

### Estado del documento versionado en el repo

**Esta v3 es el único plan vigente en el árbol.** La v2
(`docs/design/plans/2026-08-19-pre-lod-material-pipeline-v2.md`) contenía la afirmación errónea de
H1 (`ExtractBSA.exe` como binario Rust/clap) y **fue retirada del árbol en el commit `92b2822`**,
que la reemplazó por este archivo. La v2 **permanece únicamente en la historia de git**
(recuperable desde el commit `d596020`, donde se versionó) para trazabilidad, y **no debe usarse
como fuente técnica**: su H1 es falso. Cualquier referencia a "el plan" o "el roadmap" apunta a
esta v3.

---

## Alcance acordado del primer corte

**Herramientas: PGPatcher + VRAMr.** PGPatcher es GPL-3 con releases de GitHub (adquisición
automatizable, licencia verificada); VRAMr ya tiene un servicio transaccional maduro que solo
está huérfano, y ahora además su workflow y sus presets están verificados (§2.5).

**Diferidos: ParallaxR y BENDr** (PR-A / PR-B). Su viabilidad **técnica** está probada; lo único
que las bloquea es **B6-L** (permiso del autor para invocar los helpers directamente).

**Independencia del primer corte — explícita.** PGPatcher **no requiere** ParallaxR ni BENDr:
opera sobre los mapas y materiales que existan en el load order, vengan de donde vengan. VRAMr
tampoco. En el grafo, `parallax_maps` y `normal_rework` son **productores opcionales**, no
precondiciones de `mesh_materials`. Ninguna precondición, success contract ni test del primer
corte menciona a ParallaxR o BENDr.

**Adquisición de VRAMr: `MANUAL_ONLY`** en el primer corte. `AUTO_NEXUS` es PR-C.

**Auto Parallax no es un nodo.** Es runtime SKSE (§2.7). Se detecta como contexto; nunca se
instala ni se quita por decisión del agente.

**Los cuatro nodos se declaran desde P1** con `implemented=False` en los dos diferidos.


## 1. Estado actual encontrado

### 1.1 El DAG no existe en runtime

**No hay enum, dataclass, grafo ni registry de stages en código.** El DAG es prosa Markdown en
`sky_claw/local/AGENTS.md:35-82`, que se auto-declara en la línea 70:

> `### Stage dependencies (documented here, NOT yet enforced at runtime)`

El único rastro en código es logging estructurado: `extra={"pipeline_stage": N}`. Emisores
reales, exhaustivo:

| Ruta | Línea | Valor |
|---|---|---|
| `sky_claw/local/xedit/runner.py` | 903 | `pipeline_stage=1` (literal) |
| `sky_claw/local/loot/cli.py` | 232 | `pipeline_stage=5` (literal) |
| `sky_claw/local/tools/grass_cache_service.py` | 420 | `{"pipeline_stage": 8}` (literal) |
| `sky_claw/local/tools/dyndolod_service.py` | 65 | `_ETAPA_DYNDOLOD = 9` |
| `sky_claw/local/tools/dyndolod_runner.py` | 50 | `_ETAPA_DYNDOLOD = 9` (duplicada, anclada) |

`sky_claw/local/AGENTS.md:340` dice que `GenerateLodsStrategy.execute()` **no chequea** que
Wrye Bash + Synthesis hayan completado y que "a runtime guard is still needed". No hay journal
de stages, checkpoints ni marcas de staleness a nivel DAG.

**Consecuencia:** *no hay un "Stage 9" que renumerar*. La restricción real es que
`COUNT(DISTINCT tx_id) WHERE pipeline_stage=9` siga significando "DynDOLOD", lo cual se
preserva **no usando enteros de stage** para la capacidad nueva.

### 1.2 Componentes reutilizables (maduros — NO reconstruir)

| Capacidad | Módulo | Estado |
|---|---|---|
| Escaneo de entorno (registry Win, Steam VDF, GOG, `shutil.which`) | `local/discovery/scanner.py:292` | `tool_defs` L374-428 = registry de 7 tools |
| Modelo de snapshot | `local/discovery/environment.py` | `ToolInfo` L54 / `MissingTool` L64 / `EnvironmentSnapshot` L75 |
| Instalador (GitHub + Nexus) | `local/tools_installer.py` (2632 L) | 7 familias, HITL, SHA-256, lock cross-process |
| Helper genérico GitHub | `tools_installer.py:2188` `_ensure_github_mod` | Enganche para PGPatcher |
| Helper genérico Nexus | `tools_installer.py:2315` `_ensure_nexus_mod` | Enganche para PR-C |
| Publicación atómica | `tools_installer.py:1035` `_promover_payload_verificado` | Reclamo O_EXCL + rename; falla ante carrera |
| Verificación fail-closed de payload | `tools_installer.py:2153` `_verify_mod_payload` | Sentinel glob + `required_rel` + `required_files` |
| Lock de instalación cross-process | `tools_installer.py:116` `bajo_lock_de_instalacion` | Reentrante (ContextVar), heartbeat TTL/3, fail-closed |
| Cliente Nexus + fallback manual | `app/scraper/nexus_downloader.py:213` | `PremiumRequiredError` L66, `_handle_manual_fallback` L638 |
| Gateway de red | `app/security/network_gateway.py:209` | DNS pinning, allowlist, cadena de redirects |
| HITL | `app/security/hitl.py:53` | Fail-secure, primer-escritor-gana, categorías |
| Scoping HITL por pestaña | `gui/controllers/ritual_runner.py:243` | `download`/`sandbox_promotion` nunca auto-aprueban |
| Validación de rutas | `app/security/path_validator.py:113` | `validate(strict_symlink=True)`, `assert_safe_component` |
| Journal transaccional | `app/db/journal.py:209` | begin/commit/rollback, manifest, flight report |
| Locks distribuidos + heartbeat | `app/db/locks.py:197`, `:699` | `SnapshotTransactionLock` con auto-renew |
| Ownership de procesos | `local/tools/_process.py` | `kill_and_reap` L67, `assign_kill_on_close_job` L224 / `close_job` L345 |
| Rollback O(1) de directorio | `local/tools/_dir_rollback.py:166` | Move-aside; exige que la tool regenere el target completo |
| Reconciliación de backups huérfanos | `local/tools/rollback_reconciler.py` | Descubre destinos por escaneo de raíces exclusivas |
| Sandbox de perfil MO2 | `local/mo2/profile_sandbox.py:158` | Clona `profiles/<n>` **+** `overwrite`; diff/promote/discard/drift |
| Promote/discard con HITL | `app/orchestrator/sandbox_promotion.py:137` | Ritual-agnóstico, fail-closed en todas las ramas |
| **Manipulación de perfil + mods** | `local/mo2/grass_profile.py:103` | **Precedente directo**: clona perfil, crea mod en `mods/`, togglea, `teardown()` |
| Cadena USVFS completa | `mo2/vfs_broker.py` → `plugin_bundle/skyclaw_bridge` → `mo2/vfs_worker.py` | `VfsJob`/`VfsJobResult` genéricos; atestación worker+nieto; `Win32JobObject` |
| Edición byte-fiel de INIs | `local/mo2/ini_editor.py:84` | BOM/EOL/comentarios preservados, atómica + `.bak` |
| Destinos de salida (fuente única) | `local/tools/output_targets.py` | + ancla enumerativa `tests/test_output_targets.py` |
| Contrato de resultado | `local/tools/tool_result.py:36` | `normalize_tool_result`; única fuente de `"error desconocido"` |
| Preflight tipado | `local/validators/preflight.py:78` | `PreflightStatus` GREEN/YELLOW/RED + `PreflightCheck` |
| Persistencia de config | `config.py:369` + `local/local_config.py` | Merge-on-save por generaciones, atómica, doble lock |

### 1.3 Las dos superficies

- **GUI:** `SupervisorAgent.dispatch_tool` (`supervisor.py:510`) → `OrchestrationToolDispatcher.dispatch`
  → `ToolStrategy.execute(payload_dict)` → servicio de dominio. Registro imperativo: 14 llamadas
  a `dispatcher.register(...)` en `tool_dispatcher.py:262-427`.
- **Agente LLM:** `AsyncToolRegistry._register_builtins()` (`app/agent/tools/__init__.py:419`)
  con `ToolDescriptor(name, description, params_model, fn)`; schemas en `agent/tools/schemas.py`;
  handlers en `system_tools.py` / `external_tools.py` / `nexus_tools.py`; re-export obligatorio
  en `tools_facade.py`.
- **Middleware** (`tool_strategies/middleware.py`): `HitlGateMiddleware` L202 (fail-closed sin
  gate), `IdempotencyMiddleware` L387, `LoopGuardrailMiddleware` L139, `ProgressMiddleware` L450,
  `DESTRUCTIVE_TOOL_PATTERNS` L54.

**Puntos de registro para exponer UNA operación en ambas superficies (11 de producción + 4 anclas):**

```
Dominio          local/tools/<x>_service.py
GUI              orchestrator/tool_strategies/<x>.py        (nueva ToolStrategy)
                 orchestrator/tool_dispatcher.py            (import + register)
                 orchestrator/tool_strategies/middleware.py (DESTRUCTIVE_TOOL_PATTERNS)
                 orchestrator/supervisor.py                 (construcción del servicio)
                 gui/controllers/ritual_runner.py           (RITUAL_TOOL_MAP)
                 gui/views/forge_dashboard.py               (tarjeta)
Agente LLM       agent/tools/system_tools.py                (handler → str JSON)
                 agent/tools/schemas.py                     (params_model)
                 agent/tools/__init__.py                    (_register_builtins + __all__)
                 agent/tools_facade.py                      (re-export + __all__)
Wiring           app_context.py, gui/_bootloader.py

Anclas que rompen si falta uno:
  tests/test_ritual_dispatch.py            RITUAL_TOOL_MAP literal
  tests/bdd/test_mapa_sop_features.py      set(MAPA_SOP_ACCEPTANCE) == set(DESTRUCTIVE_TOOL_PATTERNS)
  tests/test_perfil_sesion_invariante.py   ningún params_model expone "profile" al LLM
  tests/test_hitl_destructive_gate.py
```

**Restricción para P12:** `tests/test_perfil_sesion_invariante.py` prohíbe que cualquier
`params_model` exponga `profile` al LLM. El perfil queda fijo por sesión (`self._mo2_profile`).

### 1.4 PRs abiertos y colisiones

**Un solo PR abierto: #488** — `fix(dyndolod): el log de la etapa 9 se clasifica en tres cajas`.
Toca `dyndolod_runner.py` (+607/-46), `tests/test_dyndolod_service.py`,
`tests/test_dyndolod_taxonomia_log.py` (nuevo), y 1-2 líneas de `sky_claw/local/AGENTS.md` y
`docs/pending_ooda_status.md`.

**Colisión: BAJA.** No toca `output_targets.py`, `scanner.py`, `tools_installer.py`, `mo2/*`,
`tool_dispatcher.py` ni `vramr_service.py`. **Acción:** mergear #488 antes de P1, o rebasar.

### 1.5 Gaps reales

1. **Modelo de estado por-herramienta binario.** `ToolInfo` vs `MissingTool` (dos clases) y
   strings `"available"/"missing"/"unknown"` (`gui/views/forge_dashboard.py:227`). No existe
   `FOUND_INVALID`, `VERSION_UNSUPPORTED` ni `CONFIGURATION_REQUIRED`.
2. **`ToolInfo.version` nunca se llena.** Único constructor `scanner.py:465`, no pasa `version=`.
   Detección real solo para Skyrim (PE, `scanner.py:201`) y LOOT (`local/loot/version.py:61`).
3. **VRAMr huérfano.** `VRAMrPipelineService` (480 L) tiene **cero referencias en `sky_claw/app/`**.
   Confirmado en `docs/audits/2026-07_historial_ooda.md:583-589`.
4. **Éxito de VRAMr = `exit_code == 0` y nada más** (`vramr_service.py:186-188`).
5. **`args: list[str]` opaco** sin validación de switches administrados. **Bloqueante para
   exponerlo al LLM.**
6. **VRAMr no emite `message`** (usa `error` legacy) ni `pipeline_stage`
   (`tests/test_dyndolod_service.py:2590`), ni usa Job Object
   (`docs/pending_ooda_status.md:257-258`), ni está en `output_targets.py`, ni resuelve su exe.
7. **`_cleanup_output_dir` solo nivel superior** (`set(output_dir.iterdir())`,
   `vramr_service.py:391`): un subdirectorio preexistente esconde del rollback lo que se escriba
   adentro.
8. **Ningún runner hereda la USVFS.** `output_targets.py:1-46` lo enuncia como invariante. El
   único camino es el broker, con `ALLOWED_VFS_TOOL_IDS = frozenset({"health", "loot_sort"})`
   (`mo2/vfs_contracts.py:20`) y handlers en `vfs_worker.py:302`.
9. **No hay API de prioridad en MO2.** `MO2Controller` (`mo2/vfs.py:98`): `read_modlist`,
   `add_mod_to_modlist` (append = máxima prioridad loose), `remove_mod_from_modlist`,
   `toggle_mod_in_modlist`, `delete_mod_files`, `launch_game`, `close_game`. **No hay
   `set_priority`/`move`, ni snapshot/restore de `modlist.txt`.**
10. **No hay campo de config para DynDOLOD, TexGen, Wrye Bash, Synthesis ni VRAMr**; env-only vía
    `PathResolutionService` (`app/core/path_resolver.py:512-536`), hidratado por
    `_SNAPSHOT_TOOL_ENV` (`gui/_bootloader.py:142`).
11. **La instalación de exes no es atómica ni tiene rollback** (LOOT/xEdit/Pandora/BodySlide
    extraen directo sobre `install_dir`). Solo el camino "mod de MO2" es atómico.
12. **`tools_installer.py` no escribe en `OperationJournal`** (0 hits de `journal`).
13. **`_PINNED_SHA256` está vacío** (`tools_installer.py:383`).
14. **`setup_tools` es una cadena `if/elif` a mano por tool** (`external_tools.py:154+`) sin ancla.

---

## 2. Hechos verificados

### 2.0 Niveles de evidencia — leer esto antes que las tablas

Tres niveles, **no intercambiables**. La distinción no es burocrática: decide qué puede
re-derivar un agente posterior y qué tiene que volver a pedir.

| Nivel | Qué significa | Quién puede re-derivarlo |
|---|---|---|
| **T1 — Verificado en sesión** | El artefacto se leyó dentro de esta sesión; el hash está calculado acá | Cualquiera que tenga el artefacto. **Solo 3 archivos** (§2.1) |
| **T2 — Atestiguado por el operador** | Inspección estática hecha por el operador **fuera** de esta sesión, con SHA-256 declarado | **Nadie desde el repo.** Requiere el artefacto o repetir la inspección |
| **T3 — Web / inferido** | Documentación pública o inferencia | Cualquiera con acceso a la fuente |

**Consecuencia operativa:** todo lo T2 es tan sólido como la inspección del operador —
que es sólida— pero **no es auditable desde el árbol**. Marcarlo como T1 sería exactamente
el error que §2.2 documenta abajo. Un agente posterior que necesite actuar sobre un hecho T2
debe pedir el artefacto o rehacer la inspección, no darlo por re-derivable.

### 2.1 Artefactos leídos EN ESTA SESIÓN (T1)

Estos tres, y solo estos tres, tienen hash calculado en sesión:

| Artefacto | SHA-256 | Qué se derivó |
|---|---|---|
| `ParallaxR.bat` v3.0318 | `72d9e19156d8367ff3c884d45685dca37df3e2f72cd0bd94d57de258425e53ed` | H2–H8 |
| `BENDr.bat` v3.0331 | `bd6f78874ae16d567beee4dd4a5d171c2b4ae29cb4a11e120624084adfb64fc0` | H2–H8 |
| `BSA.exe` (**de VRAMr**) | `cbb582794268484848b6ed71dd3fa14f2f80bd2224def38eab0815bf46c3ddb5` | H1-VRAMr |

Todo lo demás del paquete de evidencia (los helpers de ParallaxR/BENDr, el paquete de VRAMr,
PGPatcher, Auto Parallax) es **T2**.

### 2.2 Corrección de H1 — y la lección de método que la causó

**v2 afirmaba:** *"`ExtractBSA.exe` es un binario Rust + clap 4.5.60"*. **Es falso.**

El archivo que llegó a esta sesión se llamaba `BSA.exe`. Su hash
(`cbb58279…`) lo identifica, según el inventario del operador, como el **`BSA.exe` de VRAMr** —
no como el `ExtractBSA.exe` de ParallaxR, que es `bbc2035e55fb7b01c8f11f474b7d0cbbec8dc1d5f2a6e183991db1738ab4bb31`
y **nunca estuvo en esta sesión**.

La lectura fue correcta *para el archivo que había*: reconfirmado, `BSA.exe` tiene
**cero** marcadores PyInstaller/Python y sí `clap_builder-4.5.60`, `lz4_flex-0.11.5`,
`index.crates.io` y la tabla de args `source·dest·filter·logfile·layered·db·gamedir`. Lo que
falló fue **la atribución**: se asumió por el nombre de archivo a qué herramienta pertenecía.

> **Regla de método para agentes posteriores.** Identificá un artefacto por su **hash** y por
> **coherencia con la invocación que ya tenés a la vista**, nunca por su nombre de archivo. En
> este caso la evidencia para detectar el error estaba en la mano: la tabla de args incluye
> **`db`**, y `ParallaxR.bat` **no** le pasa `--db` a `ExtractBSA.exe` — el que sí lo hace es
> `script.bat` de VRAMr (`--db <Output>\VRAMr.DB`). Un cruce de treinta segundos entre los args
> declarados en el binario y los args usados en el BAT habría descartado la atribución.

**El toolchain de los helpers es MIXTO, no uniforme.** Esa es la corrección de fondo:

| Helper | Toolchain | Nivel | SHA-256 |
|---|---|---|---|
| `ExtractBSA.exe` (ParallaxR) | **PyInstaller / Python 3.11** | T2 | `bbc2035e…` |
| `MakeUnpack.exe` | PyInstaller / Python | T2 | `ca9105de…` |
| `LooseCopy.exe` | PyInstaller / Python | T2 | `99b8aeef…` |
| `Exclusions.exe` | PyInstaller / Python | T2 | `f9184150…` |
| `ParallaxRFilter.exe` | PyInstaller / Python | T2 | `f345d1b0…` |
| `OutputQC.exe` | PyInstaller / Python | T2 | `1e1ac9a5…` |
| `HeightMap.exe` | **Rust / cargo / clap 4.6.0** | T2 | `a6ae25d7…` |
| `BENDr.exe` | **Rust / cargo / clap 4.6.0** | T2 | `4a7fabc4…` |
| `BSA.exe` (VRAMr) | **Rust / cargo / clap 4.5.60** | **T1** | `cbb58279…` |
| `Loose.exe`, `Exclude.exe`, `Filter.exe`, `Extract.exe`, `Optimise.exe`, `LayerPrep.exe` (VRAMr) | Rust / cargo / clap | T2 | ver §2.5 |

**B6-T sigue cerrado, pero por otra razón.** No porque los helpers compartan toolchain — no lo
hacen— sino porque **los propios BAT (T1) demuestran interfaces CLI invocables**: cada helper se
llama con flags `--` explícitos y rutas parametrizadas. El lenguaje de implementación es
irrelevante para esa conclusión. *Ese razonamiento es más robusto que el de v2, que dependía de
una propiedad falsa.*

### 2.3 Repo (T1 — leído del árbol en `fd1d3c6`)

| Hecho | Fuente | Confianza |
|---|---|---|
| El DAG de 9 stages es prosa, no runtime | `sky_claw/local/AGENTS.md:35-82` (auto-declarado L70) | Alta |
| `pipeline_stage` es un int suelto; 5 emisores | grep exhaustivo | Alta |
| `ParallaxR`/`BENDr`/`PGPatcher`/`ParallaxGen`/`parallax`/`TruePBR`/`texconv`/`complex material` = 0 ocurrencias **en `main @ fd1d3c6`** (antes de este plan; el árbol actual ya contiene la v3) | `grep -ril` sobre el árbol de `fd1d3c6` | Alta |
| `VRAMrPipelineService` sin callers en `sky_claw/app/` | grep | Alta |
| Éxito de VRAMr = solo exit code | `vramr_service.py:186-188` | Alta |
| VRAMr devuelve `error`, no `message` | `vramr_service.py:444-468` | Alta |
| `_cleanup_output_dir` solo nivel superior | `vramr_service.py:391-394` | Alta |
| VRAMr no usa `WritePermissionsChecker` → fuera de la familia de `output_targets` | `tests/test_output_targets.py:69-79` | Alta |
| Flags de VRAMr declarados SIN VERIFICAR en el repo | `tests/test_contrato_argumentos_cli.py:245` | Alta |
| Ningún runner hereda USVFS; único camino es el broker | `local/tools/output_targets.py:1-46` | Alta |
| `ALLOWED_VFS_TOOL_IDS = {"health", "loot_sort"}` | `mo2/vfs_contracts.py:20` + `vfs_worker.py:302` | Alta |
| `VfsJob`/`VfsJobResult` genéricos | `mo2/vfs_contracts.py:100`, `:204` | Alta |
| El bridge solo acepta `launch_worker`/`cancel`/`health`; el worker es FIJO | `plugin_bundle/skyclaw_bridge/plugin.py:264`, `runtime.py:17` | Alta |
| Ejecución USVFS = `IOrganizer.startApplication(...)` + `Win32JobObject` | `runtime.py:241`, `:382` | Alta |
| `add_mod_to_modlist` = append = máxima prioridad loose | `mo2/vfs.py:218` + `AGENTS.md:308` | Alta |
| No hay API de prioridad ni snapshot de `modlist.txt` | `mo2/vfs.py:98-468` | Alta |
| `GrassProfileManager` clona perfil, crea mod, togglea, teardown | `mo2/grass_profile.py:103,188,246,304` | Alta |
| `NexusDownloader`: `PremiumRequiredError` + fallback manual con hash | `nexus_downloader.py:66,576,638` | Alta |
| HITL obligatorio (`category="download"`) en 7 call-sites | `tools_installer.py:1168,…,2376` | Alta |
| Integridad: pin SKSE + digest GitHub + TOFU; `_PINNED_SHA256` vacío | `tools_installer.py:271-293,871,2605-2620,383` | Alta |
| Instalación atómica solo para mods MO2 | `tools_installer.py:1035`, `:2153` | Alta |
| `ToolInfo.version` nunca se llena | `scanner.py:465` | Alta |
| Sin campo de config para dyndolod/texgen/synthesis/wrye_bash/vramr | `config.py:327`, `path_resolver.py:512-536` | Alta |
| Ownership: tree-kill + Job Objects Windows | `local/tools/_process.py:67,224,345` | Alta |
| No se usa `structlog`; `logging` + `pythonjsonlogger` | `logging_config.py:539` | Alta |
| Correlación: `correlation_id_var` + `pipeline_tx_id_var`; `_SIN_TX_ID` centinela | `logging_config.py:32,61,65,448` | Alta |
| Ningún `params_model` puede exponer `profile` | `tests/test_perfil_sesion_invariante.py` | Alta |
| `set(MAPA_SOP_ACCEPTANCE) == set(DESTRUCTIVE_TOOL_PATTERNS)` anclado | `tests/bdd/test_mapa_sop_features.py` | Alta |
| Todo lo que este plan toca está exento de `mypy` (`ignore_errors=true`) | `pyproject.toml:196-218` | Alta |
| Precedente de opt-in a `strict` para módulo nuevo | `pyproject.toml:283-292` (`app.security.links`) | Alta |
| Plugins generados van al final del load order, no se re-sortean | `AGENTS.md:208` (Bashed Patch "bottom"), `:273` (DynDOLOD "ABSOLUTE FINAL") | Alta |
| Único PR abierto (#488), colisión baja | GitHub | Alta |

### 2.4 ParallaxR v3.0318 y BENDr v3.0331 — hallazgos de los BAT (T1)

#### H1 — Interfaces CLI invocables (ver §2.2 para el toolchain)

Invocaciones textuales, idénticas en ambos BAT salvo el procesador final:

| Helper | Invocación verbatim | BAT · línea |
|---|---|---|
| `MakeUnpack.exe` | `--profile "<MO2Profile>" --GameDir .\` | PxR 290 · BENDr 296 |
| `ExtractBSA.exe` | `--source .\*.bsa --dest <W>\Output --logfile <W>\Logfiles --layered <TEMP> --filter *_n.dds *_p.dds` | PxR 295 · BENDr 301 |
| `LooseCopy.exe` | `--source .\textures --dest <W>\Output\textures --logfile <W>\Logfiles [--verbose] --filter *_n.dds *_p.dds` | PxR 306 · BENDr 310 |
| `Exclusions.exe` | `--Exclude <ScriptDir>\tools\Exclusions.mod --Dest <W>\Output --Logfile <W>\Logfiles` | PxR 315 · BENDr 317 |
| `ParallaxRFilter.exe` | `--source <W>\Output --logfiles <W>\Logfiles` | PxR 324 |
| `HeightMap.exe` | `--Source <W>\Output --Logfile <W>\Logfiles` | PxR 333 |
| `OutputQC.exe` | `--source <W>\Output --logfile <W>\Logfiles` | PxR 349 |
| `BENDrFilter.exe` | `--source <W>\Output --logfiles <W>\Logfiles` | BENDr 324 |
| `BENDr.exe` | `--source <W>\Output --logfile <W>\Logfiles --tool <ScriptDir>\tools\texconv.exe` | BENDr 331 |

`--logfiles` (plural) en los dos `*Filter.exe` frente a `--logfile` (singular) en el resto:
inconsistencia real del upstream, no un typo de transcripción.

**T2 — strings de `HeightMap.exe`:** descripción *"Optimized Normal Map to Height Map Converter"*,
mensajes *"Starting HeightMap conversion process."* / *"HeightMap conversion finished
successfully."*, args `source`/`logfile`. Son **candidatos a señal de completion evidence**, a
confirmar en rig.

**T2 — strings de `BENDr.exe`:** *"BENDr - High-Performance Normal Map Bending"*, *"Applying
Gaussian blur … to source height map"*, *"Re-normalizing 3D vectors…"*, *"Deleted source Parallax
file"*, *"BENDr Unified Pipeline finished"*; args `source`/`logfile`/`tool`/`verbose`.
**Lectura correcta de *"Deleted source Parallax file"*:** BENDr borra los `_p` **de su propio
workspace** tras consumirlos como height source. **No** implica que el output de ParallaxR deba
borrarse globalmente. Confundir ambos borraría trabajo del operador.

#### H2 — Comportamiento global peligroso (verificado)

| Qué | Dónde |
|---|---|
| `taskkill /f /im powershell.exe` **al arrancar** | PxR 41 · BENDr 49 |
| `taskkill /f /im Powershell.exe` + `texconv.exe` [+ `Convert.exe`] al terminar | PxR 422-424 · BENDr 367-368 |
| `Get-Process \| … MinWorkingSet = 0` — **sobre TODO proceso del sistema** | PxR 414, 428 (`:Flush`, tras cada etapa) · BENDr 353 |
| `powershell -ExecutionPolicy Bypass` para AntiSleep y utilidades | PxR 42, 113, 217, 339, 412 · BENDr 50, 121, 223, 352 |
| `timeout /t 3` entre etapas (`:Flush`) | PxR 410 |

No es prueba de malware; es comportamiento que **Sky-Claw no debe copiar**. Detalle de diseño:
`AntiSleep.ps1` **no se detiene explícitamente** — lo mata el `taskkill` global. Un port que
suprima el taskkill (correcto) y conserve el AntiSleep (incorrecto) **leakea el proceso**.
Sky-Claw no lanza ninguno de los dos.

#### H3 — Leen el `Data` virtualizado: requieren USVFS

El guard de "mi output ya está activo" (PxR 143 · BENDr 151):

```bat
cd /d "%GameDir%"
if exist ".\ParallaxROutput.tmp" (  ERROR - Existing ParallaxR Output Enabled in your Mod Manager  )
```

`ParallaxROutput.tmp` vive **dentro del output-mod**. Solo aparece en `Data\` si MO2 lo tiene
habilitado y virtualizado: **el propio guard prueba que el BAT espera `Data` = vista USVFS.**
Lo corrobora `tasklist /fi "ImageName eq ModOrganizer.exe"` (PxR 239 · BENDr 245) — detecta MO2
**en ejecución**, condición de estar lanzado desde MO2 — y que `LooseCopy --source .\textures`
copia del `Data\textures` físico, que sin VFS solo tendría vanilla.

`MakeUnpack.exe --profile` es complementario: escribe `%TEMP%\Unpack-Order.tmp` (borrado al
arrancar, PxR 46 · BENDr 54) que `ExtractBSA --layered` consume para extraer BSAs **en orden de
prioridad de mods** — orden que la VFS no expresa para archivos.

> Por eso el handler USVFS es **infraestructura compartida de toda la capacidad** (P5), no un
> detalle de PGPatcher.

#### H4 — Los markers son copias estáticas: frescura **solo** por mtime

```bat
xcopy "%ScriptDir%\tools\ParallaxROutput.tmp" "%ParallaxRTEMP%\Output" /v /q /y     (PxR 368)
xcopy "%ScriptDir%\tools\BENDrOutput.tmp"     "%BENDrTEMP%\Output"    /v /q /y      (BENDr 336)
```

El contenido es idéntico corrida a corrida. **`hash(marker)` NO prueba frescura.** Aplica igual a
`VRAMrOutput.tmp` (§2.5). La frescura sale de: run identity + ventana de mtime/creación + output
fresco + logs frescos + otros artefactos.

#### H5 — La semántica de `_p.dds` de ParallaxR, enunciada con precisión

```powershell
# PxR 339 — después de HeightMap
$src=$env:GameDir+'\textures'; $dst=$env:ParallaxRTEMP+'\Output\textures';
Get-ChildItem -Path $src -Filter '*_p.dds' -Recurse | ForEach-Object {
  $rel = $_.FullName.Substring($src.Length+1); $tgt = Join-Path $dst $rel;
  if (Test-Path $tgt) { Remove-Item -LiteralPath $tgt -Force -ErrorAction SilentlyContinue } }
```

**No** describirlo como *"preserva los `_p.dds` existentes comparando hashes"*. Lo observado:

- enumera `GameDir\textures\**\*_p.dds` (**loose**);
- por cada uno, si existe el equivalente generado en `Output\textures`, **borra el generado**;
- por lo tanto evita **sobrescribir** loose `_p.dds` ya existentes, **mediante eliminación del
  generado**;
- **la comprobación mira loose files en `Data\textures`**. **NO** demuestra el mismo trato para un
  `_p.dds` que exista únicamente dentro de un BSA (→ **R8**).

El post-check correcto es *"ningún `_p.dds` del output colisiona con un `_p.dds` loose
preexistente"*. Comparar hashes de los existentes —lo que v1 proponía— habría pasado siempre.

#### H6 — Preflight real y cuantificado

| Requisito | Evidencia |
|---|---|
| Estimación: `robocopy .\ c:\dummy /L /E *_n.dds *_p.dds` → bytes × 1.75 | PxR 202-206 · BENDr 209-213 |
| Exige **raíz de unidad**; crea `<Drive>:\ParallaxR` / `<Drive>:\BENDr` | PxR 229-232 · BENDr 235-238 |
| Advierte *"Avoid C: due to Windows Security"* | PxR 208 · BENDr 214 |
| Rechaza correr si existe `<Drive>:\ParallaxR` previo (escanea C..Z) | PxR 157-191 · BENDr 165-199 |
| `attrib -R -A -S -H -P *.* /s /d` sobre el output | PxR 359 |
| Borra `*.png` y `*.db` del output; elimina dirs vacíos | PxR 363-364, 375 |

#### H7 — Asimetrías entre ambas (no asumir simetría)

| | ParallaxR | BENDr |
|---|---|---|
| Log(s) | `logfiles\ParallaxR.log` | `logfiles\BENDrMain.log` **+** `logfiles\BENDr.log` |
| `:Flush` | tras **cada** etapa | una vez, al final |
| QC | `OutputQC.exe` | **no tiene** |
| Manejo de `_p.dds` | H5 | no aplica |
| `:Terminate` mata | Powershell, texconv, **Convert.exe** | Powershell, texconv |
| Lectura de `%TEMP%\*.tmp` | `in ("%TEMP%\…")` con comillas | `in (%TEMP%\…)` **sin comillas** (BENDr 130, 233, 295): se rompe si `TEMP` tiene espacios |

#### H8 — Toolset compartido y exclusiones

`MakeUnpack.exe`, `ExtractBSA.exe`, `LooseCopy.exe`, `Exclusions.exe` y `texconv.exe` aparecen con
invocaciones idénticas en ambos BAT. Un adapter para PR-A/PR-B debe modelar una **etapa de
"layered extract" compartida**, no duplicar cuatro invocaciones.

**T2 — `Exclusions.mod`** incluye, entre muchas otras: `\pbr`, `\terrain`, `\lod`, `\DynDOLOD`,
`\LODGen`, más categorías de transparencia/personajes/efectos. Confirma que las exclusiones de
LOD/DynDOLOD son del upstream, no algo que Sky-Claw deba agregar.

#### H9 — Sin gates de ERRORLEVEL

Los BAT encadenan helpers **sin comprobar `ERRORLEVEL` sistemáticamente** y luego escriben
mensajes de *"complete / output ready"*. **"El BAT terminó" no es evidencia de éxito** — refuerza
el success contract de §7.4 desde el artefacto mismo.

### 2.5 VRAMr v16.0310 — workflow, helpers y presets (T2, salvo `BSA.exe` que es T1)

> **Cambio respecto de v2:** VRAMr deja de estar en "transcripción del brief". El paquete fue
> analizado por el operador y su workflow, args y presets están **verificados**.

**Paquete:** `VRAMr-90557-v16-0310-1773176836.zip` · SHA-256
`b36b184170c0c68a8ed1130f335abc9e0ad9edbe15416f656559b755626052a9`
**Wrapper:** `script.bat` · SHA-256
`ef085ffdd914d7bf30e3e52ad30d18063ac6e19e0308a1b00686b7a01c809581`

**Workflow real:** selección/detección GPU → comprobar `VRAMrOutput.tmp` → elegir preset →
calcular storage → crear workspace → detectar PBR → detectar MO2/Vortex → `LayerPrep.exe` [MO2] →
`BSA.exe` → `Loose.exe` → `Exclude.exe` → `Filter.exe` → `Extract.exe` → `Optimise.exe` → copiar
`VRAMrOutput.tmp` → renombrar a `DragNDropThisFolderIntoModManager`.

**CLI observada en el BAT:**

| Helper | Args |
|---|---|
| `LayerPrep.exe` | `--profile <MO2 profile> --GameDir .\ --Logfile <logs>` |
| `BSA.exe` | `--source .\*.bsa --dest <Output> --logfile <Logfiles> --layered <TEMP> --filter *.dds --db <Output>\VRAMr.DB` |
| `Loose.exe` | `--source .\textures --dest <Output>\textures --logfile <Logfiles> --db <Output>\VRAMr.DB` |
| `Exclude.exe` | `--Exclude <Exclusions.mod> --Dest <Output> --Logfile <Logfiles>` |
| `Filter.exe` | `--dnpm diffuse normal parallax material --Source <Output>\VRAMr.db --Logfiles <Logfiles>` |
| `Extract.exe` | `--Source <Output> --Logfile <Logfiles> --Output <Output>` |
| `Optimise.exe` | `--dnpm diffuse normal parallax material --Source <Output>\VRAMr.db --Logfiles <Logfiles> --GPU <index> --Tool <texconv.exe>` |

El `--db <Output>\VRAMr.DB` es el detalle que corrobora, de forma independiente, que el `BSA.exe`
leído en sesión (T1, con `db` en su tabla de args) pertenece a **este** paquete y no a ParallaxR
(§2.2). **Una DB stale es un vector de staleness de primer orden** para el success contract.

**Presets (resoluciones exactas):**

| Preset | Diffuse | Normal | Parallax | Material |
|---|---|---|---|---|
| High Quality | 2048 | 2048 | 1024 | 1024 |
| Quality | 2048 | 1024 | 1024 | 1024 |
| Optimum | 2048 | 1024 | 512 | 512 |
| Performance | 2048 | 512 | 512 | 512 |
| Vanilla | 512 | 512 | 512 | 512 |
| Custom | 512–4096 (pot) | 512–2048 | 512–2048 | 512–2048 |

Esto reemplaza al `list[str]` opaco de `vramr_service.py`: el modelo tipado de P8(h) sale
directo de esta tabla.

**Detección de PBR:** presencia de `GameDir\textures\pbr\`.

**SHA-256 de helpers (T2, salvo el primero):**
`BSA.exe` `cbb58279…` (**T1**) · `Loose.exe` `c3e4510d…` · `Exclude.exe` `616a4777…` ·
`Filter.exe` `1239c2c0…` · `Extract.exe` `b658181d…` · `Optimise.exe` `15a81ecf…` ·
`LayerPrep.exe` `789e6eb3…` · `VRAMr.exe` (launcher) `5e08368e…`

**Límite explícito:** la inspección estática de `VRAMr.exe` solo permite afirmar que es el
**launcher**. **No** usarla para inventar una CLI headless del pipeline completo (→ B3
reformulado, §3.2).

**Marker:** `VRAMrOutput.tmp` también se copia estáticamente → H4 aplica.
**Seguridad:** al final hace `taskkill` global de `PowerShell.exe`, `UserGuide.exe`, `texconv.exe`
y modifica working sets globales → H2 aplica. No copiar.

### 2.6 PGPatcher v1.2.0 — evidencia estática fuerte (T2)

**ZIP:** SHA-256 `9981cd966ca512ff2ac6d0006537fa937da74a3ac4e7f6dc973c996a0697436c` · 264 entradas.
Incluye `PGPatcher.exe`, `pgtools.exe`, `PGLib.dll`, `PGMutagenWrapper.dll`, `dotnetlib/`,
`cshaders/`, `assets/`.

**SHA-256:** `PGPatcher.exe` `7027f055…` · `pgtools.exe` `cc6493b6…` · `PGLib.dll` `b6ddcca2…`

#### PG1 — El requisito de USVFS deja de ser inferencia

Strings en `PGPatcher.exe`: **`usvfs_x64.dll`** y

> *"Please verify that you are launching PGPatcher from MO2, VFS not detected."*

Confirma H3 para PGPatcher desde el binario, no desde un changelog.

#### PG2 — Contratos de output, del binario

> *"DynDoLOD and TexGen outputs must be disabled prior to running PGPatcher. It is recommended to
> generate LODs after running PGPatcher with the PGPatcher output enabled."*

> *"Please disable VRAMr output mod before running PGPatcher."* — junto al string `vramroutput.tmp`

Es decir: **PGPatcher detecta el output de VRAMr por el mismo mecanismo de marker** que ParallaxR/
BENDr usan para el suyo. El preflight de Sky-Claw puede modelarse sobre esa misma señal.

#### PG3 — **Ownership exclusivo del directorio de salida** (restricción nueva y dura)

String en `PGLib.dll`:

> *"Output directory has non-PGPatcher related files. The output directory should only contain
> files generated by PGPatcher or empty."*

**PGPatcher exige ser dueño exclusivo de su output.** Consecuencias de diseño, concretas:

1. `pgpatcher_output_target` debe resolver un directorio **exclusivo**, bajo el namespace
   `Sky-Claw/`, que nada más escriba. Es exactamente la propiedad que `output_targets.py` ya
   documenta para `bodyslide_output_root` (*"nada más que `run_bodyslide_batch` crea contenido
   acá"*) y de la que depende `rollback_reconciler` para descubrir destinos por escaneo.
2. El **preflight debe verificar vacío-o-solo-PGPatcher** antes de lanzar, o la herramienta
   aborta por su cuenta.
3. Encaja de forma natural con `DirectoryRollback` (move-aside O(1)), cuya precondición es
   *"la herramienta regenera el target por completo"* — que acá se cumple por diseño del upstream.

#### PG4 — CLI y artefactos observados

Opciones presentes en el binario: **`--autostart`**, **`--console`**, **`--ignore-mo2vfscheck`**
(descripción embebida: *"Ignore MO2 VFS check - might be useful for Linux users"*).

Artefactos nombrados: `PGPatcher.esp` · `ParallaxGen_Diff.json` · `PGPatcher_Output.zip`.

`PGLib.dll` también contiene `FixMeshLighting`, `UpgradeParallaxToCM`, `TruePBR`.
`pgtools.exe` expone `fixmeshlighting`, `truepbr`, `parallaxtocm`, `restoredefaultshaders`,
`--no-multithreading`, `--shortcut`, `--high-mem`, *output directory*.

**Dos límites que NO se cruzan con esta evidencia:**

- **La existencia de `--ignore-mo2vfscheck` no demuestra que correr fuera de la VFS produzca un
  patch correcto** contra una modlist de MO2. El nombre y la descripción sugieren que **apaga un
  chequeo**, no que provea virtualización alternativa. **B5 sigue abierto** hasta leer la
  implementación en la fuente GPL-3.
- **No concluir que `pgtools.exe` sustituye 1:1 a `PGPatcher.exe`.** Comparten nombres de
  operación, lo cual es compatible tanto con "misma funcionalidad" como con "subconjunto de
  utilidades". **B4 se cierra contra la fuente**, no contra strings.

#### PG5 — Auto Parallax queda descartado como dependencia

`PGPatcher` incluye la opción **"Disable Pre-Patched Materials"**, descrita como: restaura shaders
por defecto cuando faltan texturas parallax/complex material, y **declara que reemplaza la
funcionalidad de Auto Parallax**. Evidencia directa de que Auto Parallax **no** es dependencia
obligatoria de un workflow moderno con PGPatcher.

### 2.7 Auto Parallax — resuelto: es runtime, no una etapa (T2)

**Paquete:** `Auto Parallax-79473-1-0-27-1669777275.zip` · SHA-256
`1e67a4df3919c67f94ca94d9545b2a95add708592a240f377173cfd25d9ab2ef`

**Contenido completo — tres archivos:** `SKSE/Plugins/AutoParallax.dll`,
`SKSE/Plugins/AutoParallax.ini`, `SKSE/Plugins/AutoParallax.pdb`.

INI observado: `[Experimental] bAutoEnableParallax = false` · `[Debug] bEnableDebugLog = false`,
`bEnableDebugTextures = false`.

**Conclusión (cierra la pregunta de diseño):** Auto Parallax es un **plugin SKSE de runtime**. No
genera assets, no es una etapa del pipeline, y **no debe entrar como nodo** del Pre-LOD Material
Pipeline. Detectarlo aporta contexto/compatibilidad; Sky-Claw **no** lo instala ni lo quita por
decisión del agente. Si PGPatcher lo hace redundante para la configuración elegida (PG5), se
**informa** al operador y la decisión final es suya.

### 2.8 Web (T3)

| Hecho | Fuente |
|---|---|
| PGPatcher (aka ParallaxGen) es GPL-3.0, repo `hakasapl/PGPatcher` con GitHub Releases | página del repo |
| README: *"You no longer should install prepatched meshes nor auto parallax"* | README (coherente con PG5) |


## 3. No verificado

### 3.1 Los tres ejes, y por qué B6-T está cerrado

Tres ejes **ortogonales**. Confundirlos fue el error de v1.

| Eje | Pregunta | Cómo se responde | Estado |
|---|---|---|---|
| **T — Técnico** | ¿Existe una interfaz invocable sin GUI? | Inspección del artefacto | **CERRADO** para ParallaxR/BENDr (los BAT demuestran CLI invocable, §2.2) y para los helpers de VRAMr (§2.5). **Abierto** para el pipeline completo de VRAMr (B3) y para PGPatcher (B4) |
| **L — Legal** | ¿Los permisos permiten invocar los helpers, redistribuirlos, descargarlos automáticamente? | Leer permisos del autor / licencia | **ABIERTO** para ParallaxR/BENDr/VRAMr (B1, B6-L). **CERRADO** para PGPatcher (GPL-3) |
| **S — Seguridad** | ¿El artefacto hace cosas que Sky-Claw no puede replicar? | Inspección del artefacto | **CERRADO y caracterizado** (H2). Se evita **no replicándolo**; no se hereda |

**Que S esté cerrado no desbloquea nada por sí solo** — solo elimina un riesgo. Y que T esté
cerrado **no autoriza** a invocar los helpers: eso lo decide L. Un permiso del autor puede cubrir
"usar la herramienta" y no "llamar a sus binarios internos desde otro programa".

### 3.2 Bloqueantes

**Del primer corte (PGPatcher + VRAMr):**

- **B3 (eje T) — REFORMULADO.** *Ya verificado:* el workflow de `script.bat`, los argumentos de los
  siete helpers, los presets con sus resoluciones y la estructura de output (§2.5). *Todavía NO
  verificado:* (a) cuál es la **interfaz oficialmente soportada** para automatizar el pipeline
  completo; (b) si `VRAMr.exe` ofrece una CLI equivalente **no interactiva**; (c) si Sky-Claw debe
  llamar al launcher, al BAT o a los helpers directamente — decisión que depende del contrato
  **y del permiso** (cruza con B1).
- **B4 (eje T).** Superficie CLI real de PGPatcher: semántica completa de `--autostart`,
  `--console`; y **si `pgtools.exe` sustituye a `PGPatcher.exe` o es un subconjunto de utilidades**.
  Cerrar **contra la fuente GPL-3**, no contra strings (§2.6 PG4).
- **B5 (eje T).** Qué desactiva exactamente `--ignore-mo2vfscheck`. La existencia del flag **no**
  demuestra que correr fuera de la VFS produzca un patch correcto contra una modlist de MO2
  (§2.6 PG4). Cerrar leyendo la implementación del check.
- **B7 (DAG) — sigue abierto y más preciso.** Ver §3.3.

**Diferidos al segundo corte:**

- **B1 (eje L).** Permisos/licencia de ParallaxR, BENDr y VRAMr en Nexus. Para VRAMr solo decide
  AUTO vs MANUAL; `MANUAL_ONLY` es el default conservador y funcionalmente completo.
- **B6-L (eje L).** ¿El autor permite que un tercero invoque `MakeUnpack.exe` / `ExtractBSA.exe` /
  `HeightMap.exe` / `BENDr.exe` directamente, sin pasar por su BAT? **Es lo único que bloquea
  PR-A/PR-B.** B6-T está cerrado (§2.2).

### 3.3 B7 — `PGPatcher.esp` frente a LOOT / Wrye Bash / Synthesis

PGPatcher **genera un plugin** (`PGPatcher.esp`, confirmado en el binario, §2.6 PG4). Eso choca
con la posición pre-LOD del material pipeline:

```
STAGE 5 LOOT          ordena el load order
STAGE 6 Wrye Bash     lee plugins activos → Bashed Patch, 0.esp
STAGE 7 Synthesis     lee plugins activos → Synthesis.esp
        ↓
   pre_lod_materials  ← PGPatcher genera un plugin AQUÍ, después de 5/6/7
        ↓
STAGE 9 TexGen → DynDOLOD
```

**Lo que hay que establecer, exactamente:**

1. **Cuándo se crea** el plugin (¿siempre, o solo con ciertas opciones?).
2. **Qué records contiene** — eso decide si es inerte para los patchers.
3. **¿Debe pasar por LOOT?** El repo tiene precedente en contra: los plugins generados **se anexan
   al final** y no se re-sortean — `Bashed Patch, 0.esp` *"bottom of the load order"*
   (`AGENTS.md:208`), DynDOLOD *"ABSOLUTE FINAL entry"* (`:273`). Si `PGPatcher.esp` sigue esa
   convención, B7 se cierra barato.
4. **¿Debe existir antes de Wrye Bash?** ¿**Synthesis** debe verlo?
5. **¿Cuál es su orden relativo** frente a `Bashed Patch, 0.esp`, `Synthesis.esp` y `DynDOLOD.esp`?
6. **¿Cuenta contra el límite de 254 masters?** `AGENTS.md` §0 directiva 7 lo trata como crítico y
   §5 regla 6 prohíbe mockearlo en tests.

**Por qué es BLOCKER y no IMPORTANT:** si (4) es "sí", el nodo `mesh_materials` se mueve **antes
del stage 6**, y **P1 congela ese grafo**. Congelarlo mal cuesta rehacer P1 y todo lo que dependa
de él. **No congelar la posición hasta cerrar B7.**

### 3.4 Requieren rig real (no se cierran con mocks)

- **R1.** Contrato de éxito de cada herramienta: qué escribe el log, con qué severidad, si
  "0 archivos procesados" es éxito legítimo. Candidatos de completion evidence ya identificados en
  strings (§2.4): *"HeightMap conversion finished successfully."*, *"BENDr Unified Pipeline
  finished"* — **a confirmar que se emiten y dónde**.
- **R2.** Prioridad de BENDr Output sobre ParallaxR Output. Inferida. Solo PR-B. **No congelar.**
- **R3.** Orden real de outputs ON/OFF alrededor de PGPatcher y VRAMr. **Parcialmente cerrado por
  §2.6 PG2** (DynDOLOD/TexGen/VRAMr deben estar OFF, del binario); falta el orden de re-habilitación
  y la prioridad relativa final.
- **R4.** Si PGPatcher deja artefactos en `Overwrite` de MO2 al correr bajo USVFS.
- **R5.** Rutas con espacios y Unicode (antecedente caro: `AGENTS.md:269`, CRT vs Delphi).
- **R6.** Si VRAMr spawnea compresores que sobreviven al tree-kill
  (`docs/pending_ooda_status.md:257-258`). `Optimise.exe --Tool <texconv.exe>` lo hace probable.
- **R7.** Posición relativa de NGIO (stage 8) frente al material pipeline: **no hay evidencia de
  dependencia en ninguna dirección**. No inventarla.
- **R8.** Si la limpieza de `_p.dds` de ParallaxR ignora los BSAs a propósito o es un gap del
  upstream (§2.4 H5). Solo PR-A.

### 3.5 Política de artefactos: qué NO entra al repo

**Prohibido committear** al repo MIT: `.exe`, `.dll`, `.zip` originales, paquetes de Nexus, y
**copias completas de scripts/BAT de terceros** cuando el permiso de redistribución no fue
verificado.

**PGPatcher es GPL-3 y tiene fuente pública, pero eso no obliga a vendorizar sus binarios.**
Mantenerlo como **herramienta externa** salvo decisión deliberada de distribución/licenciamiento
—que traería obligaciones de la GPL-3 sobre un repo MIT y es una decisión de licenciamiento, no de
ingeniería.

**Lo que P0 SÍ conserva** (y es suficiente para que un agente posterior trabaje):

| Elemento | Por qué basta |
|---|---|
| Nombre y versión observada | Identifica el artefacto |
| **SHA-256** | Permite re-identificarlo sin copiarlo |
| Inventario de archivos, cuando haga falta | Estructura sin contenido |
| Interfaces CLI observadas | Es lo que el adapter necesita |
| Secuencia de workflow reconstruida | Es lo que el orquestador necesita |
| Comportamiento relevante | Preflight, markers, seguridad |
| Conclusiones técnicas | El razonamiento, no la fuente |
| **Fragmentos pequeños estrictamente necesarios** | Cita mínima, no reproducción |
| Fuente / procedencia | Trazabilidad |
| Estado **VERIFIED / INFERRED / NOT VERIFIED** | Honestidad epistémica |

> **Esto corrige a v2**, que proponía *"versionar las transcripciones (o los artefactos)"* bajo
> `docs/`. Una transcripción íntegra de un BAT de terceros **es** una copia completa y cae en la
> prohibición. La documentación de evidencia se escribe **derivada**: tablas de args, secuencia,
> hashes y conclusiones — con fragmentos solo donde la cita literal sea imprescindible para el
> argumento (como las tres líneas de `_p.dds` de H5, o los strings de contrato de PG2/PG3).


## 4. DAG recomendado

**No se renumera Stage 9.** No hace falta: no hay registry de stages en runtime.

```
STAGE 1  xEdit QAC
STAGE 2  CAO (sin implementación en el repo)
STAGE 3  BodySlide
STAGE 4  Pandora
STAGE 5  LOOT
STAGE 6  Wrye Bash        ⚠ B7: ¿necesita ver pgpatcher.esp?
STAGE 7  Synthesis        ⚠ B7: idem
                    │
        ┌───────────┴────────────────────────────────┐
        │                                            │
   STAGE 8                            CAPABILITY  pre_lod_materials
   No Grass In Objects                (identificador semántico, sin número)
   (grass precache)                              │
        │        ┌────────────────────────────────┴──────────────────────────┐
        │        │  parallax_maps   → ParallaxR   [OPCIONAL · 2º corte]       │
        │        │  normal_rework   → BENDr       [OPCIONAL · 2º corte]       │
        │        │  mesh_materials  → PGPatcher   [1er corte]                 │
        │        │  vram_optimise   → VRAMr       [OPCIONAL · 1er corte]      │
        │        └────────────────────────────────┬──────────────────────────┘
        │                                         │
        └────────────────┬────────────────────────┘
                         │  convergencia: ambas ramas COMPLETAS y no-stale
                         ▼
                    STAGE 9  TexGen → DynDOLOD 3
```

**NGIO ↔ material pipeline: NINGUNA arista** (R7). Ramas paralelas que convergen antes de
Stage 9. Documentar como *no verificado*, no como independencia probada.

### 4.1 Dependencias internas — los opcionales son PRODUCTORES, no precondiciones

```
   parallax_maps (ParallaxR)          vram_optimise (VRAMr)
   produce _p.dds faltantes           optimiza texturas
   preserva por no-colisión (H5)      requiere OFF: propio, PGPatcher
        │ [opcional]                       ▲
        │ produce ────────┐                │
        ▼                 │                │
   normal_rework (BENDr)  │                │
   requiere pares (_n,_p) │                │
   consume ───────────────┤                │
   ⚠ prioridad = R2       │                │
        │ [opcional]      │                │
        ▼                 ▼                │
   ┌──────────────────────────────┐        │
   │   mesh_materials (PGPatcher) │────────┘
   │   requiere USVFS (B5)        │
   │   requiere OFF: TexGen,      │   luego: VRAMr output ON
   │     DynDOLOD, VRAMr, propio  │          PGPatcher output ON con prioridad
   │   ⚠ genera plugin → B7       │
   └──────────────────────────────┘
```

**`mesh_materials` NO tiene precondición sobre `parallax_maps` ni `normal_rework`.** PGPatcher
parchea los meshes para los materiales que **existan** en el load order — vengan de mods
retexture, de BSAs vanilla, o de ParallaxR. Los nodos opcionales **enriquecen su entrada**; no
la habilitan. Lo mismo `vram_optimise`.

**Caminos válidos del primer corte** (ninguno menciona ParallaxR/BENDr):
`PGPatcher → VRAMr` · `PGPatcher` solo · `VRAMr` solo.

**Caminos que habilita el segundo corte:**
`ParallaxR → PGPatcher → VRAMr` · `ParallaxR → BENDr → PGPatcher → VRAMr` · y sus parciales.

**Los cuatro nodos se declaran desde P1**, con `implemented=False` en los dos diferidos: pedirlos
devuelve un error accionable, no un crash ni un skip silencioso. El segundo corte agrega adapters
**sin rediseñar el grafo**.

### 4.2 Identidad de telemetría

`pipeline_stage` queda **reservado a los 9 stages del SOP**. La capacidad emite:

```python
extra={
    "pipeline_capability": "pre_lod_materials",   # campo semántico nuevo
    "material_step": "mesh_materials",            # el nodo concreto
    "tx_id": tx_id,                               # obligatorio; None explícito si no hay TX
}
```

Preserva `COUNT(DISTINCT tx_id) WHERE pipeline_stage=9` = incidentes de DynDOLOD, sin carve-outs.
La auditoría de `tests/test_dyndolod_service.py` (`_auditar_cobertura_de_etapa`) se amplía para
aceptar `pipeline_stage` **o** `pipeline_capability`, y los servicios nuevos entran con cobertura
completa desde el día 1 — no como deuda en `_SERVICIOS_SIN_COBERTURA`.

---

## 5. Tool discovery + estrategia de adquisición

### 5.1 `ToolReadiness` — extensión, no reemplazo

Enum en `local/discovery/environment.py`, en el estilo de `PreflightStatus`:

```
ToolReadiness (StrEnum)
  READY                     exe encontrado, versión leída y soportada
  MISSING                   no encontrado en ninguna raíz
  FOUND_INVALID             encontrado pero no es el binario esperado (nombre/PE/arquitectura)
  VERSION_UNSUPPORTED       versión leída, fuera del rango soportado
  VERSION_UNKNOWN           versión no legible → degradado, NUNCA bloqueante
  CONFIGURATION_REQUIRED    encontrado, falta config obligatoria (API key, perfil, preset)
```

`ToolInfo` gana `readiness` y `version_source`. **`MissingTool` no se elimina** (la GUI la
consume); se deriva. El estado de **adquisición** es ortogonal y vive en el spec (§5.2), no en el
snapshot: una tool puede ser `MISSING` + `AUTO_GITHUB` o `MISSING` + `MANUAL_ONLY`.

### 5.2 `ExternalToolSpec` — registry declarativo

> **Nombre.** v1 lo llamaba `ToolDescriptor` y **colisionaba** con
> `sky_claw/app/agent/tools/descriptor.py::ToolDescriptor` (metadatos de tool del LLM). Son
> conceptos distintos: aquél describe *una tool que el LLM puede llamar*; éste describe *un
> binario de terceros que Sky-Claw detecta e instala*. En v2 se llama **`ExternalToolSpec`** y
> vive en `local/discovery/registry.py`.

Hoy hay **cinco listas que deben coincidir a mano** sin ancla que lo obligue:
`scanner.py:tool_defs`, `RITUAL_TOOL_MAP`, `RITUAL_INSTALLER_MAP`, `RITUAL_INSTALL_ENV` y
`_SNAPSHOT_TOOL_ENV` (`_bootloader.py:142`). Agregar herramientas a mano por cinco lugares es la
receta exacta del defecto dominante del repo.

```
ExternalToolSpec
  key                    "pgpatcher" | "vramr" | "parallaxr" | "bendr" | …
  display_name
  exe_names              tupla (alimenta _find_tool)
  friendly_action        español, para la GUI
  official_url           página oficial (botón "Abrir página oficial")
  is_critical
  install_kind           TOOL | OUTPUT_MOD        ← ver §5.3
  acquisition            AUTO_GITHUB | AUTO_NEXUS | MANUAL_ONLY
  installer_method       nombre del método de ToolsInstaller, o None
  env_var                var que lee PathResolutionService, o None
  config_field           campo de config.toml, o None
  version_probe          callable | None
  supported_versions     rango | None
  requires_usvfs         bool                      ← nuevo, por H3
```

**Ancla obligatoria** (`tests/test_registro_de_herramientas.py`): igualdad literal del dict
completo **+** un test de que las cinco listas se **derivan** del registry, no se escriben aparte.

### 5.3 `install_kind` — la distinción que v1 confundía

v1 recomendaba "instalar las cuatro como mods de MO2 (camino atómico)". **Eso es incorrecto y
potencialmente dañino.** Son dos cosas distintas:

| | **TOOL** (instalación de herramienta) | **OUTPUT_MOD** (instalación de mod) |
|---|---|---|
| Qué es | El ejecutable/scripts que Sky-Claw **corre** | El árbol de assets que la herramienta **produce** |
| Ejemplos | `PGPatcher.exe`, los helpers de ParallaxR, el paquete de VRAMr | `DragNDropThisFolderIntoModManager`, `Sky-Claw - PGPatcher Output` |
| Dónde vive | Directorio de herramientas (`install_dir`), **fuera** de `mods/` | `<mo2>/mods/<nombre>` |
| Quién lo gestiona | `ToolsInstaller` | `Mo2ModStateTransaction` + `output_targets.py` |
| Por qué no se mezclan | Meter un ejecutable en `mods/` lo inyecta en el VFS del juego: no es un asset, y MO2 lo virtualizaría dentro de `Data` | Meter un árbol de texturas en el dir de herramientas lo saca del alcance de MO2: nunca lo vería el juego |

**Las cuatro herramientas del pipeline son `TOOL`.** Sus **salidas** son `OUTPUT_MOD`. El
precedente del repo lo respalda: NGIO, Community Shaders y Address Library sí son `OUTPUT_MOD`
(son mods de verdad) y por eso van por `_ensure_nexus_mod`; LOOT/xEdit/Pandora/BodySlide son
`TOOL` y van a `install_dir`.

**Consecuencia:** el gap 11 (*la instalación de exes no es atómica ni tiene rollback*) **no se
puede sortear** metiendo las tools en `mods/`. Hay que **arreglarlo**: portar la técnica de
`_promover_payload_verificado` (staging + verificación + reclamo de nombre O_EXCL + rename) al
camino `TOOL`. Eso es P4, y beneficia también a LOOT/xEdit/Pandora/BodySlide.

### 5.4 Estrategia per-tool

| Herramienta | `install_kind` | Primer corte | Segundo corte |
|---|---|---|---|
| **PGPatcher** | TOOL | `AUTO_GITHUB` vía `_ensure_github_mod` sobre el camino TOOL atómico de P4 | — |
| **VRAMr** | TOOL | **`MANUAL_ONLY`** — se detecta y valida, no se descarga | `AUTO_NEXUS` (PR-C) cuando B1 cierre |
| **ParallaxR** | TOOL | Nodo declarado, sin adapter | Según B1/B6-L |
| **BENDr** | TOOL | Nodo declarado, sin adapter | ídem |
| **Auto Parallax** | **RUNTIME_PLUGIN** | **No es una herramienta del pipeline.** Plugin SKSE (DLL/INI/PDB, §2.7). Solo se *detecta* como contexto; nunca se instala ni se quita automáticamente | — |

> **Auto Parallax no tiene `install_kind`, tiene una categoría propia.** Clasificarlo como
> `OUTPUT_MOD` (como hacía v2) era un error de tipo: no es la salida de ninguna herramienta, es un
> plugin de runtime que carga el juego. No participa del grafo ni de la transacción de estado.
> Si PGPatcher lo hace redundante para la configuración elegida (§2.6 PG5), se **informa** al
> operador y la decisión final es suya.

**`MANUAL_ONLY` no es una degradación.** El camino AUTO de Nexus ya degrada solo:
`NexusDownloader.download()` levanta `PremiumRequiredError` y cae a `_handle_manual_fallback`
(`nexus_downloader.py:638`), que abre el navegador, vigila el `staging_dir` y **valida el hash
del archivo que el operador bajó a mano** (`_validate_manual_hash` L576). `MANUAL_ONLY` es
literalmente ese mismo camino sin el intento previo de descarga. Pasar VRAMr a `AUTO_NEXUS`
después es cambiar un campo del spec, no reescribir la UX.

### 5.5 Flujo de adquisición (todo existe salvo el paso 0 y la atomicidad del paso 8)

```
0. registry.get(key) → ExternalToolSpec       ← NUEVO (P2)
1. scanner detecta                            ← scanner.py:_resolve_tool_path / _find_tool
2. readiness != READY                         ← NUEVO (P2)
3. acción según spec.acquisition
     AUTO_*  → [Instalar] [Seleccionar ruta] [Omitir] [Cancelar]
     MANUAL  → [Abrir página oficial] [Volver a detectar] [Seleccionar ruta] [Omitir] [Cancelar]
4. HITL obligatorio     ← hitl.request_approval(category="download") con tool/versión/
                          fuente/tamaño/destino (formato de tools_installer.py:1161-1171)
5. lock cross-process   ← bajo_lock_de_instalacion(...)
6. egress               ← NetworkGateway.request (DNS pin, allowlist, redirects)
7. integridad           ← digest de la API GitHub / hash de Nexus / _PINNED_SHA256
8. instalación atómica  ← _verify_mod_payload + promoción por rename   ← ARREGLAR para TOOL (P4)
9. re-detección         ← scanner.scan()
10. persistir SOLO tras validar  ← escribir_campo + guardar_config_bloqueante
```

Nunca descargar en silencio. No redistribuir binarios de terceros en el repo MIT.
`ALLOWED_HOSTS`/`OUT_OF_SCOPE_HOSTS` (`config.py:573`, `:600`) deben cubrir los hosts nuevos;
`github.com` ya está en `OUT_OF_SCOPE_HOSTS` (dispara HITL, que es lo correcto).

---

## 6. MO2 state machine

### 6.1 Qué hay y qué falta

| Necesidad | Existe | Falta |
|---|---|---|
| Leer enabled/disabled | `read_modlist` (`mo2/vfs.py:137`) | — |
| Cambiar enabled/disabled | `toggle_mod_in_modlist` (`:260`) | — |
| Crear un output mod | `add_mod_to_modlist` (`:177`, append = máx. prioridad) | — |
| Escritura atómica de `modlist.txt` | `_write_modlist_atomic` + `_modlist_lock` | El lock es **in-process**, no cross-process |
| Prioridad arbitraria | — | **No hay `set_priority`/`move`** |
| Snapshot/restore del modlist | — | **No existe** |
| Detección de drift | `ProfileSandbox._check_drift` (`:363`) | Es para el clon, no para mutación in-place |
| Correr bajo USVFS | Broker con allowlist de 2 tool_ids | Ninguna tool del pipeline está en la allowlist |

### 6.2 Por qué no sirve `ProfileSandbox`, y cuál es el precedente correcto

`ProfileSandbox` clona el perfil para que el ritual escriba en la **copia**. Acá se necesita lo
contrario: las herramientas deben ver el **estado real** de MO2 (H3: leen el `Data`
virtualizado). Lo que se muta y restaura es la **configuración de qué está activo**.

**El precedente correcto es `GrassProfileManager`** (`mo2/grass_profile.py:103`): ya clona el
perfil a uno dedicado, crea un mod de config en `mods/`, togglea conflictivos y hace `teardown()`.
`Mo2ModStateTransaction` generaliza ese patrón y agrega lo que le falta: fingerprint, detección
de drift, journal y reversión dirigida. **Evaluar y documentar en P6** si `GrassProfileManager`
puede reimplementarse encima — si sí, es consolidación real; si no, escribir por qué son
distintos, en vez de dejar dos mecanismos parecidos conviviendo.

### 6.3 Rediseño v2: **el drift externo nunca se sobrescribe**

**El defecto de v1.** El diseño anterior era: *drift detectado → ROLLBACK (restaurar el
snapshot)*. Eso es exactamente un **overwrite ciego**: si el operador cambió `modlist.txt`
mientras corría el pipeline, restaurar nuestro snapshot **destruye su cambio**, en silencio y sin
posibilidad de recuperarlo. Un `os.replace` de un archivo completo sobre un estado que no
conocemos no es "rollback": es pérdida de datos con otro nombre.

**El principio de v2, enunciado como propiedad del mecanismo:**

> El snapshot existe para **comparar**, no para restaurar a ciegas. La transacción solo puede
> revertir **las entradas que ella misma escribió**, y solo mientras esas entradas sigan siendo
> exactamente como las dejó. Cualquier otra cosa es del operador y no se toca.

De ahí salen tres desenlaces, no dos, y el tercero es **terminal hasta que decida un humano**:

```
                    ┌────────────────────────────────┐
                    │  IDLE                          │
                    └──────────────┬─────────────────┘
                                   │ enter()
                                   ▼
                    ┌────────────────────────────────┐
                    │  SNAPSHOT                      │
                    │  · copia byte-fiel (BOM+CRLF)  │
                    │  · fingerprint del archivo     │
                    │  · registro de las ENTRADAS    │
                    │    que vamos a tocar, con su   │
                    │    valor previo                │
                    │  · OperationJournal            │
                    └──────────────┬─────────────────┘
                                   │ apply(paso n)
                                   ▼
        ┌──────────────────────────────────────────────────────┐
        │  APPLIED[n]                                          │
        │  · ANTES de cada escritura: re-leer y verificar      │
        │    fingerprint == el que dejamos nosotros            │
        │  · escribir con toggle/add dirigido (nunca reescribir│
        │    el archivo entero desde el snapshot)              │
        │  · re-fingerprint y guardarlo                        │
        └───┬──────────────────────────────────┬───────────────┘
            │ fingerprint coincide             │ fingerprint NO coincide
            ▼                                  ▼
   ┌──────────────────┐          ┌──────────────────────────────────┐
   │ (siguiente paso) │          │  DRIFT_DETECTED                  │
   │  o commit()      │          │  clasificar el cambio ajeno:     │
   └────────┬─────────┘          └──┬────────────────┬──────────────┘
            │                       │                │
            │            nuestras entradas    nuestras entradas
            │            INTACTAS             ALTERADAS o ausentes
            │                       │                │
            │                       ▼                ▼
            │        ┌──────────────────────┐  ┌───────────────────────────┐
            │        │ REVERT_TARGETED      │  │  QUARANTINED  (terminal)  │
            │        │ revertir SOLO        │  │  · NO se escribe nada     │
            │        │ nuestras entradas;   │  │  · se preservan ambos     │
            │        │ dejar intacto todo   │  │    estados + el diff      │
            │        │ lo que tocó el       │  │  · HITL al operador con   │
            │        │ operador             │  │    el diff y 3 opciones   │
            │        └──────────┬───────────┘  │  · default = NO TOCAR     │
            │                   │              └───────────────────────────┘
            ▼                   ▼
   ┌──────────────────┐  ┌──────────────┐
   │  COMMITTED       │  │ ROLLED_BACK  │
   └──────────────────┘  └──────────────┘
```

**Las reglas que hacen que esto no pierda datos:**

1. **Nunca un `os.replace` del snapshot completo sobre `modlist.txt`.** La reversión es
   **por entrada**: para cada mod que la transacción habilitó/deshabilitó/agregó, se restaura
   *ese renglón* a su valor previo, dejando el resto del archivo tal como está en disco **ahora**.
   Un restore de archivo entero solo sería correcto si el fingerprint actual coincidiera con el
   que dejamos, y en ese caso la reversión dirigida da el mismo resultado — así que el camino
   ciego no aporta nada y solo agrega el modo de fallo.
2. **`QUARANTINED` es terminal y no escribe.** Si nuestras propias entradas fueron alteradas por
   fuera, no hay forma segura de deshacer sin adivinar la intención del operador. Se para, se
   preserva todo, se le muestra el diff y se le ofrece: *restaurar mi estado* / *conservar el
   tuyo* / *dejar como está*. **El default ante timeout o denegación es "dejar como está"** —
   coherente con el fail-secure del resto del repo, donde no actuar es el desenlace seguro.
3. **El fingerprint se verifica ANTES de cada escritura**, no solo al final. Detectar el drift
   después de haber escrito encima ya es tarde.
4. **La transacción declara qué entradas va a tocar antes de tocarlas** y las registra en el
   `ActionManifest` (`orchestrator/preview/action_manifest.py`), igual que hacen los rituales
   existentes. Eso es lo que hace posible la reversión dirigida y la clasificación del drift.
5. **`delete_mod_files` no se llama jamás desde la transacción.** Deshabilitar no es borrar.
6. **Límite conocido y a documentar, no a disimular:** el lock es de Sky-Claw; `MO2.exe` no lo
   respeta. La detección de drift ataja el problema **a posteriori**, no lo previene. Decirlo
   explícitamente es parte del diseño.
7. **Todo mutador del estado de mods pasa por la transacción.** Es la propiedad a anclar con un
   test AST enumerativo (estilo `test_db_connection_invariant.py`): *qué módulos escriben
   `modlist.txt`* — hoy solo `mo2/vfs.py`; congelarlo.

**Prioridad.** Se necesita `set_mod_priority` / `move_mod_after`. Semántica verificada:
`modlist.txt` se lee de abajo hacia arriba, el último gana (`AGENTS.md:308`,
`assets/asset_scanner.py::parse_modlist` invierte el archivo). "PGPatcher Output con prioridad
sobre VRAMr Output" = PGPatcher **después** de VRAMr en el archivo.

### 6.4 USVFS: infraestructura compartida, no un detalle de PGPatcher

**Cómo funciona hoy** (ADR 0007, `docs/architecture/mo2_vfs_subprocesses.md`):

```
daemon Sky-Claw
  │  VfsExecutionBroker.submit(job)                          vfs_broker.py:274
  │    · escribe VfsWorkerManifest firmado HMAC
  │    · manda {"type":"launch_worker", job_id, profile, manifest_path, overwrite_mod}
  ▼
plugin MO2 "skyclaw_bridge"  (hilo Qt, dentro de MO2)
  │  SOLO acepta launch_worker / cancel / health              plugin.py:264
  │  el ejecutable del worker es FIJO (bridge_config.json)    runtime.py:17
  │  organizer.startApplication(worker, args, cwd, profile, overwrite, False)   runtime.py:241
  │  Win32JobObject kill-on-close sobre el HANDLE             runtime.py:382
  ▼
worker Sky-Claw (binario congelado PyInstaller) — CORRE DENTRO DEL USVFS
  │  verify_vfs_attestation() → canario + probe de proceso nieto
  │  handler = _default_handlers()[job.tool_id]               vfs_worker.py:207
  ▼
la herramienta, spawneada por el handler → hereda la USVFS del worker
```

**El bridge no puede lanzar un ejecutable arbitrario** (el worker es fijo, por diseño de
seguridad). El camino es un **handler dentro del worker**, que spawnea la herramienta como hijo
suyo y por lo tanto le da la VFS.

Por **H3**, esto lo necesitan ParallaxR, BENDr y PGPatcher — y muy probablemente VRAMr
(`LayerPrep [MO2]`). Por eso en v2 el handler USVFS es **P5**, se diseña **genérico** (un handler
parametrizado por spec, no uno por herramienta), y cada adapter posterior solo agrega su
`tool_id` al allowlist con su spec.

| Opción para PGPatcher | Pro | Contra |
|---|---|---|
| **A. Handler en el worker** (recomendada) | Reutiliza atestación, manifiesto firmado, `VfsJob`/`VfsJobResult`, `rollback_state`, Job Object. **Es el único camino que *demuestra* estar bajo la VFS**, y esa prueba es evidencia de completitud para el success contract. El bridge no cambia. Sirve a las 4 herramientas | El worker es binario congelado: el handler entra al build de PyInstaller |
| **B. `--ignore-mo2vfscheck`** | Trivial | **Probablemente incorrecto**: desactiva un guard, no provee virtualización → no vería los mods de MO2 → resultado silenciosamente incompleto. Requiere B5 |
| **C. `ModOrganizer.exe` directo** | Es como lo hace un humano | `moshortcut://` existe en un solo sitio (`vfs.py:371`) y lanza **el juego**; vector declarado SIN VERIFICAR (`test_contrato_argumentos_cli.py:242`); no captura stdout/exit code confiablemente; sin atestación |

**Recomendación: A.** B queda como fallback documentado solo si B5 demuestra que produce
resultado correcto — y aun así perdería la atestación, así que su success contract sería
estrictamente más débil.

---

## 7. Arquitectura propuesta

### 7.1 Reutilizado sin cambios

`HITLGuard` · `NetworkGateway` · `PathValidator` · `DistributedLockManager` /
`SnapshotTransactionLock` · `OperationJournal` · `ActionManifest` / `FlightReport` ·
`ToolsInstaller` (+ helpers genéricos + `bajo_lock_de_instalacion`) · `NexusDownloader`
(+ fallback manual) · `_process.kill_and_reap` / `assign_kill_on_close_job` / `close_job` ·
`DirectoryRollback` · `rollback_reconciler` · `normalize_tool_result` · `EnvironmentScanner` ·
`PathResolutionService` · `Config` merge-on-save · `OrchestrationToolDispatcher` + middlewares ·
`AsyncToolRegistry` + `ToolDescriptor` · `VfsBroker` / `VfsJob` / `VfsJobResult` / atestación.

### 7.2 Extendido

| Módulo | Extensión |
|---|---|
| `local/discovery/environment.py` | `ToolReadiness`; `ToolInfo.readiness` + `version_source` |
| `local/discovery/scanner.py` | `tool_defs` derivado del registry; probes de versión |
| `local/tools/output_targets.py` | `pgpatcher_output_target`, `vramr_output_target` (+ `parallaxr_*`, `bendr_*` en el 2º corte), bajo el namespace `Sky-Claw/`. **`pgpatcher_output_target` debe ser EXCLUSIVO** (§2.6 PG3) — es la misma propiedad que el módulo ya documenta para `bodyslide_output_root` y de la que depende `rollback_reconciler` |
| `local/tools/vramr_service.py` | Success contract real; `message`; `pipeline_capability`; Job Object; args administrados; cleanup recursivo |
| `local/tools_installer.py` | Camino `TOOL` **atómico** (§5.3); `ensure_pgpatcher` |
| `local/mo2/vfs.py` | `set_mod_priority` / `move_mod_after` |
| `local/mo2/vfs_contracts.py` + `vfs_worker.py` | Handler genérico USVFS + allowlist (el bridge **no** cambia) |
| `app/agent/tools/external_tools.py` | `setup_tools` derivando de tabla, no `if/elif` |
| `app/orchestrator/tool_strategies/middleware.py` | `DESTRUCTIVE_TOOL_PATTERNS` |
| `app/agent/tools_facade.py` | Re-export de la operación nueva |
| `config.py::_load_defaults` | Campos de path para las tools nuevas (+ dyndolod/texgen/synthesis/wrye_bash, hoy env-only) |
| `tests/test_dyndolod_service.py` | Auditoría de cobertura acepta `pipeline_capability` |

### 7.3 Nuevo (mínimo indispensable)

| Componente | Ruta propuesta | Responsabilidad |
|---|---|---|
| `ExternalToolSpec` + registry | `local/discovery/registry.py` | Fuente única de descriptores; las 5 listas se derivan |
| `Mo2ModStateTransaction` | `local/mo2/mod_state_tx.py` | Snapshot / apply / verify / **cuarentena** / commit / reversión dirigida |
| Handler USVFS genérico | `local/mo2/vfs_worker.py` (extensión) | Correr una herramienta allowlisted como hijo del worker |
| `MaterialStepResult` + success contract | `local/tools/material/_contract.py` | Estructura común de los adapters (**PR propio, antes del primer adapter**) |
| `PGPatcherAdapter` | `local/tools/material/pgpatcher.py` | Submite `VfsJob(tool_id="pgpatcher")`; preflight, post-check, output, rollback |
| `VRAMrAdapter` | `local/tools/material/vramr.py` | Envuelve `VRAMrPipelineService` (no lo reescribe); presets, args administrados |
| `ParallaxRAdapter` **[2º corte]** | `local/tools/material/parallaxr.py` | Orquesta los helpers (H1), **sin** el driver BAT |
| `BENDrAdapter` **[2º corte]** | `local/tools/material/bendr.py` | ídem; etapa de layered-extract compartida (H8) |
| `PreLodMaterialPipelineService` | `local/tools/material/pipeline_service.py` | Orquesta adapters + `Mo2ModStateTransaction`; resuelve el camino aplicable |
| `RunMaterialPipelineStrategy` | `app/orchestrator/tool_strategies/run_material_pipeline.py` | Superficie GUI |
| Handler + `params_model` | `app/agent/tools/system_tools.py` + `schemas.py` | Superficie agente LLM |

**La REGLA FUNDAMENTAL se respeta:** adapters independientes, cada uno dueño de su detección /
preflight / lanzamiento / post-check / output / rollback. El orquestador **no conoce** el CLI de
ninguna herramienta; solo dependencias, precondiciones y estado de outputs.

### 7.4 Success contract común (`_contract.py`, PR P7)

Ninguna herramienta se aprueba por `return_code == 0`. Cada paso debe satisfacer **todas**:

```
process_result       exit code dentro del conjunto aceptado por ESA herramienta
run_identity         tx_id presente y correlacionable
vfs_evidence         atestación del broker, cuando la herramienta corre bajo VFS
fresh_output         firma (mtime+size+hash del archivo decisor) cambió vs. pre-launch
fresh_log            log escrito durante la ventana del run
completion_evidence  marker fresco POR MTIME (H4: el contenido es estático) / línea de cierre
no_terminal_failure  sin líneas de error terminal en el log
postconditions       específicas de la herramienta
```

Antecedente directo: `AGENTS.md:271` (DynDOLOD = exit code **Y** artefacto **Y** frescura contra
firma pre-launch **Y** sin errores en el log). El staging no se limpia entre corridas: **la mera
presencia de output no prueba nada**.

Postcondiciones específicas (todas **a verificar en rig**, no congelar):

- **PGPatcher:** distinguir no-op legítimo / failure / output parcial / stale. **No asumir** que
  `PGPatcher.esp`, `ParallaxGen_Diff.json` o `PGPatcher_Output.zip` existan siempre: depende de la
  configuración (B4). Evidencia de VFS = la atestación del broker. **Precondición dura de
  ownership** (§2.6 PG3): el directorio de salida debe estar **vacío o contener solo archivos de
  PGPatcher** antes de lanzar, o la herramienta aborta por su cuenta — el preflight lo verifica.
- **VRAMr:** contadores Converted/Failed leídos del log; `Converted: 0, Failed: 0` es ambiguo por
  sí solo y necesita contexto (¿había algo que convertir?); output y marker frescos.
  **`VRAMr.DB` es un vector de staleness de primer orden** (§2.5): `BSA.exe` y `Loose.exe` la
  escriben y `Filter.exe`/`Optimise.exe` la leen, así que una DB de una corrida anterior puede
  producir un resultado plausible pero desactualizado. Firmar la DB, no solo el output.
- **ParallaxR [2º corte]:** workspace nuevo creado y limpiado; `OutputQC.exe` sin fallos;
  **ningún `_p.dds` del output colisiona con un `_p.dds` loose preexistente** (H5 — *no* comparar
  hashes de los existentes); marker fresco por mtime; sin artifacts parciales.
- **BENDr [2º corte]:** pares normal/parallax procesados > 0 **o** clasificación explícita de
  "cero trabajo legítimo"; `_n.dds` de salida frescos; exclusiones respetadas; **los dos logs**
  (`BENDrMain.log` y `BENDr.log`, H7) presentes y frescos.

### 7.5 Ownership de procesos y power management

- **Prohibido** replicar nada de H2. Solo se termina el árbol de la transacción: `kill_and_reap`
  (tree-kill por PID) + `assign_kill_on_close_job` / `close_job` (Job Object con
  `KILL_ON_JOB_CLOSE`, que alcanza nietos reparentados que el tree-kill ya no ve).
- **VRAMr necesita el Job Object** — hoy no lo tiene (gap 6).
- **AntiSleep:** no lanzar `AntiSleep.ps1`. Si hace falta evitar suspensión,
  `SetThreadExecutionState` acotado al lifetime de la operación y restaurado en `finally`.
  Recordar H2: en el original el AntiSleep **lo mata el taskkill global**; un port que suprima el
  taskkill y conserve el AntiSleep leakea el proceso.
- **Nunca** matar por nombre de imagen global.

### 7.6 Seguridad

No ejecutar un binario solo porque se encontró: validar path permitido (`PathValidator`), nombre
esperado, versión, arquitectura, ubicación escribible, y traversal de symlink/reparse
(`strict_symlink=True`). No elevar privilegios. No tocar Defender ni exclusiones de antivirus.
Los vectores de argumentos que provengan de payload (GUI o LLM) pasan por un validador de
switches administrados fail-closed, calcado de `DynDOLODRunner._extra_args_admisibles`, con su
ancla AST.

---

## 8. Plan por PRs

> Cada PR: pequeño, reversible, una intención, con tests. Sin refactors cosméticos.
> **Primer corte: PGPatcher + VRAMr.** ParallaxR y BENDr son PR-A/PR-B, fuera del camino crítico.

---

### PR P0 — Consolidación de evidencia + verificación contra fuente (sin código)

**Objetivo.** Dejar la evidencia utilizable desde el repo **sin vendorizar artefactos**, y cerrar
B4/B5/B7 y el resto de B3.
**Archivos.** `docs/design/specs/2026-XX-pre-lod-material-pipeline-evidence.md` (nuevo).
**NO tocar.** Nada de `sky_claw/`. **Ningún binario, zip ni copia íntegra de BAT** (§3.5).
**Dependencias.** Ninguna. **Requiere rig solo para el punto 4.**

**Trabajo, en orden de costo creciente:**

1. **Escribir el documento de evidencia — derivado, no copiado.** Trasladar §2.1–§2.8 al repo con
   el formato de §3.5: nombre + versión, **SHA-256**, inventario cuando haga falta, interfaces CLI
   observadas, secuencia de workflow reconstruida, comportamiento relevante, conclusiones, y
   **estado VERIFIED / INFERRED / NOT VERIFIED por fila**. Fragmentos literales **solo** donde la
   cita sea imprescindible para el argumento (las tres líneas de `_p.dds` de H5; los strings de
   contrato PG2/PG3). El formato a imitar es `PROCEDENCIA_DE_FLAGS`
   (`tests/test_contrato_argumentos_cli.py`), que exige nombrar la fuente contra la que se
   verificó cada flag.
   **Marcar explícitamente qué es T1 y qué es T2**, porque un agente posterior no puede
   re-derivar lo T2 desde el árbol.
2. **B4 y B5 — sin rig, leyendo la fuente GPL-3.** Clonar `hakasapl/PGPatcher` (sin vendorizar) y
   leer: el parser de argumentos (semántica de `--autostart`, `--console`); **si `pgtools.exe` es
   sustituto o subconjunto de `PGPatcher.exe`**; y el **código del check de VFS**, para establecer
   qué hace exactamente `--ignore-mo2vfscheck` y si un run sin VFS produce un patch correcto
   contra una modlist de MO2.
3. **B7 — misma fuente + docs upstream.** Cuándo se crea `PGPatcher.esp`, qué records contiene, si
   debe pasar por LOOT, si Wrye Bash o Synthesis deben verlo, su orden relativo en la cola final, y
   si cuenta contra el límite de 254 masters. **Contrastar con la convención del repo** (plugins
   generados al final, `AGENTS.md:208`, `:273`). **Este punto decide la posición de
   `mesh_materials` y por lo tanto bloquea a P1.**
4. **B3 resto — rig.** Determinar la **interfaz oficialmente soportada** para automatizar el
   pipeline completo de VRAMr: si `VRAMr.exe` tiene modo no interactivo, y si Sky-Claw debe llamar
   al launcher, al BAT o a los helpers. Registrar además qué procesos hijo spawnea `Optimise.exe`
   (decide el diseño del Job Object, R6).
5. **R1 — rig.** Una corrida manual de PGPatcher y otra de VRAMr, registrando log completo, árbol
   de procesos, artefactos con timestamp y qué queda en `Overwrite`. Confirmar si los strings de
   completion (*"HeightMap conversion finished successfully."*, *"BENDr Unified Pipeline
   finished"*) se emiten y dónde.
6. **Opcional, barato:** `--help` de cada helper de ParallaxR/BENDr/VRAMr en el rig. Adelanta
   PR-A/PR-B/P10 sin costo.

**Aceptación.** Cada fila tiene procedencia citable y estado epistémico. **Cero binarios, zips o
transcripciones íntegras en el commit.** B4, B5 y B7 quedan cerrados o con el experimento concreto
que los cerraría.
**Riesgos.** Si B7(4) resulta "sí, Wrye Bash/Synthesis necesitan ver `PGPatcher.esp`", el nodo
`mesh_materials` se mueve antes del stage 6 y P1 cambia. **Por eso P0 va primero.**
**Rollback.** Borrar el doc. **Complejidad: Media.**


### PR P1 — Contrato de lifecycle y capability (docs + tests, sin implementación)

**Objetivo.** Introducir `pre_lod_materials` como capacidad semántica pre-LOD, sin renumerar
Stage 9 y sin adapters.
**Archivos.** `sky_claw/local/AGENTS.md` (§1 rama pre-LOD; §5 regla de `pipeline_capability`) ·
`docs/pipeline/skyrim_sop.md` · `docs/pending_ooda_status.md` ·
`tests/test_material_pipeline_contract.py` (nuevo) · `tests/test_dyndolod_service.py`.
**NO tocar.** `dyndolod_service.py`, `dyndolod_runner.py` (colisión con #488), ningún runner.
**Dependencias.** Merge de #488 · **P0 (B7 decide la posición del nodo).**
**Cambios conceptuales.** Nodos, dependencias, opcionalidad, convergencia, invalidación de
downstream, semántica de re-ejecución. `pipeline_stage` reservado a los 9 stages; la capacidad usa
`pipeline_capability` + `material_step`. **Los cuatro nodos se declaran**, con ParallaxR/BENDr en
`implemented=False`. **Los opcionales son productores, no precondiciones** (§4.1).
**Tests.** Contrato del grafo sobre una estructura de datos pura, sin procesos: dependencias,
caminos parciales válidos, rechazo de órdenes inválidos. Ancla enumerativa de los nodos.
`pipeline_stage=9` sigue siendo exclusivo de DynDOLOD. **Un nodo `implemented=False` no puede
ejecutarse** (error accionable, no crash). **`mesh_materials` no declara precondición sobre
`parallax_maps` ni `normal_rework`.**
**Aceptación.** Los tests pasan sobre un grafo declarativo; ninguna consulta de telemetría
existente cambia de significado. **Rollback.** Revert limpio. **Complejidad: Baja.**

---

### PR P2 — `ToolReadiness` + `ExternalToolSpec` registry

**Objetivo.** Modelo de estado por herramienta y fuente única de descriptores; las cinco listas se
derivan.
**Archivos.** `local/discovery/environment.py` · `local/discovery/registry.py` (nuevo) ·
`local/discovery/scanner.py` · `gui/controllers/ritual_runner.py` · `gui/_bootloader.py` ·
`gui/views/forge_dashboard.py` · `pyproject.toml` (opt-in `strict`) ·
`tests/test_registro_de_herramientas.py` (nuevo).
**NO tocar.** `tools_installer.py`, servicios de `local/tools/`.
**Dependencias.** P1.
**Cambios conceptuales.** Enum de readiness; `ExternalToolSpec` (**no** `ToolDescriptor` —
colisiona con `agent/tools/descriptor.py`); derivación de las cinco listas. **Sin agregar
herramientas nuevas**: solo cambia la forma, con las 7 actuales. Recorte deliberado — si el
refactor de forma y el alta de tools van juntos, un fallo no distingue cuál lo causó.
**Tests.** Igualdad literal del registry. Cada lista existente == la derivada. Readiness: missing /
found / wrong exe / unsupported version / moved installation / invalid path / stale configured path.
**Aceptación.** `RITUAL_TOOL_MAP` y hermanos dan exactamente los mismos valores; sus anclas
actuales pasan **sin modificarse**. **Riesgos.** Toca la GUI; mitigación: derivación pura.
**Rollback.** Revert. **Complejidad: Media.**

---

### PR P3 — Detección de versión

**Objetivo.** Llenar `ToolInfo.version`, que hoy nunca se llena.
**Archivos.** `local/discovery/scanner.py` · `local/discovery/registry.py` ·
`tests/test_deteccion_version.py` (nuevo).
**NO tocar.** Instalador, servicios. **Dependencias.** P2.
**Cambios conceptuales.** Reutilizar `_read_pe_product_version` (`scanner.py:201`) para binarios
PE y el patrón `--version` de `local/loot/version.py:61`. Versión ilegible → `VERSION_UNKNOWN`
degradado, **nunca** bloqueante: hoy ninguna tool reporta versión, así que bloquear sería una
regresión de disponibilidad.
**Tests.** PE con/sin versión; sin `pefile`; `--version` responde/falla/timeout; rangos;
`VERSION_UNSUPPORTED`.
**Aceptación.** Al menos LOOT, xEdit y PGPatcher reportan versión real; ninguna tool se vuelve
inutilizable por versión ilegible. **Rollback.** Revert. **Complejidad: Media.**

---

### PR P4 — Instalación `TOOL` atómica + instalador de PGPatcher + `setup_tools` derivado

**Objetivo.** Cerrar el gap 11 y agregar el primer instalador nuevo.
**Archivos.** `local/tools_installer.py` (camino `TOOL` atómico + `ensure_pgpatcher`) ·
`local/discovery/registry.py` (specs de PGPatcher y VRAMr) · `config.py::_load_defaults` ·
`app/agent/tools/external_tools.py` · `tests/test_instalacion_tool_atomica.py` (nuevo) ·
`tests/test_instalador_pgpatcher.py` (nuevo) · `tests/test_setup_tools_derivado.py` (nuevo).
**NO tocar.** `mo2/*`, servicios de `local/tools/`, adapters.
**Dependencias.** P0 (artefactos del release), P2.

**Cambios conceptuales, tres intenciones que comparten un archivo — dividir si la revisión lo pide:**

1. **Camino `TOOL` atómico.** Portar la técnica de `_promover_payload_verificado` (staging +
   `_verify_mod_payload` + reclamo de nombre O_EXCL + rename) al destino de herramientas. Hoy
   LOOT/xEdit/Pandora/BodySlide extraen directo sobre `install_dir`: una extracción que falla a
   mitad deja el directorio mezclado, sin rollback. **No** se sortea metiéndolas en `mods/`
   (§5.3): eso inyectaría ejecutables en el VFS del juego.
2. **`ensure_pgpatcher`** sobre ese camino, con `install_kind=TOOL`. HITL `category="download"`
   con tool/versión/fuente/tamaño/destino (formato de `tools_installer.py:1161-1171`). Persistir
   el path **solo tras re-detectar y validar**. VRAMr entra al registry como `MANUAL_ONLY`.
3. **`setup_tools` derivado de tabla.** No es refactor cosmético: es la corrección del defecto
   estructural que hace que agregar una tool requiera editar cinco listas a mano.

**Tests.** Extracción que falla a mitad → `install_dir` intacto; carrera por el nombre; acepta /
declina; fallo de red; archivo corrupto; contenido inesperado; disco insuficiente; rollback; ya
instalado (idempotencia + re-verificación); persistencia falla → resultado parcial con
`_AVISO_PERSISTENCIA`. **Anclas:** cada spec con `acquisition != MANUAL_ONLY` tiene su `ensure_*`
y aparece en el default de `setup_tools`; cada `MANUAL_ONLY` **no** ofrece descarga en ninguna
superficie; **ningún spec `install_kind=TOOL` resuelve un destino bajo `mods/`**.
**Aceptación.** PGPatcher se instala atómicamente, se detecta y su path se persiste. VRAMr se
detecta si el operador lo instaló a mano. `setup_tools` no tiene ramas por tool.
**Riesgos.** Toca el camino de instalación de 4 tools existentes. Mitigación: el comportamiento
observable no cambia (mismo destino, mismo resultado), solo la atomicidad.
**Rollback.** Revert. **Complejidad: Alta.**

---

### PR P5 — Handler USVFS genérico (infraestructura compartida)

**Objetivo.** Habilitar el único camino con evidencia de correr bajo la VFS, para **toda** la
capacidad — no solo PGPatcher.
**Archivos.** `local/mo2/vfs_contracts.py` (allowlist) · `local/mo2/vfs_worker.py` (handler
genérico + primer `tool_id`) · `sky_claw/local/mo2/AGENTS.md` ·
`tests/test_vfs_material_tools.py` (nuevo) · `tests/test_vfs_execution_contracts.py` ·
`tests/test_contrato_argumentos_cli.py`.
**NO tocar.** `brokered_loot.py`, **el bridge (`plugin_bundle/`) no cambia**, orquestador, GUI,
adapters.
**Dependencias.** P0 (B4/B5), P2.
**Cambios conceptuales.** Por **H3**, las cuatro herramientas leen el `Data` virtualizado, así que
el handler se diseña **parametrizado por spec** (ejecutable + vector de args validado + targets),
no uno escrito a mano por herramienta. El `tool_id` de PGPatcher entra a `ALLOWED_VFS_TOOL_IDS`
(`vfs_contracts.py:20`) y a `_default_handlers()` (`vfs_worker.py:302`) — **dos sitios que deben
coincidir y hoy no tienen ancla**; es la forma exacta del defecto dominante. El handler spawnea la
herramienta **como hijo del worker**, que es lo que le da la USVFS. La atestación (canario +
probe de nieto) se propaga al `VfsJobResult` y alimenta `vfs_evidence` del success contract — no
es un chequeo decorativo.
**Tests.** **Ancla: `ALLOWED_VFS_TOOL_IDS == set(_default_handlers())`.** `tool_id` no allowlisted
→ rechazo. Atestación falla → sin ejecución. Manifiesto con firma inválida → rechazo. VFS no
disponible → error accionable, no crash. Inyección de switch por spec → rechazada. Procedencia de
flags declarada.
**Aceptación.** Una herramienta allowlisted corre con atestación válida; sin atestación no corre.
El camino de LOOT no cambia de comportamiento.
**Riesgos.** El worker es binario congelado PyInstaller y hay un guard fail-closed que lo exige
(`__main__.py:202-215`, escape hatch `SKYCLAW_ALLOW_DEV_WORKER=1`): **verificar que el build
incluya el handler nuevo**, o funciona en dev y falla en producción.
**Rollback.** Revert; la allowlist vuelve a 2 entradas. **Complejidad: Alta.**

---

### PR P6 — `Mo2ModStateTransaction` (con cuarentena)

**Objetivo.** La transacción de estado de mods, sola, sin consumidores todavía.
**Archivos.** `local/mo2/mod_state_tx.py` (nuevo) · `local/mo2/vfs.py` (`set_mod_priority` /
`move_mod_after`) · `sky_claw/local/mo2/AGENTS.md` · `pyproject.toml` (`strict`) ·
`tests/test_mo2_mod_state_tx.py` (nuevo) · `tests/test_modlist_writers_invariant.py` (nuevo).
**NO tocar.** `profile_sandbox.py`, `sandbox_promotion.py`, broker, `grass_profile.py`.
**Dependencias.** P1.
**Cambios conceptuales.** El diseño de §6.3, con las tres salidas: `COMMITTED`,
`REVERT_TARGETED → ROLLED_BACK`, y **`QUARANTINED` terminal**. Reversión **por entrada**, nunca
`os.replace` del snapshot completo. Fingerprint verificado **antes** de cada escritura.
Declaración de entradas a tocar en el `ActionManifest`. Lock cross-process vía
`DistributedLockManager` (no el `asyncio.Lock` in-process de `MO2Controller`). Journal.
`delete_mod_files` jamás. **Evaluar y documentar** si `GrassProfileManager` puede reimplementarse
encima — la reimplementación **no** va en este PR.
**Tests.** Perfil correcto/incorrecto; output esperado habilitado; output prohibido habilitado;
prioridad no coincide; reversión dirigida tras fallo; tras cancelación; **snapshot conserva BOM y
CRLF byte a byte**. Y los tres de drift, que son el corazón del PR:
- *drift con nuestras entradas intactas* → revierte **solo** las nuestras, el cambio del operador
  **sobrevive byte a byte**;
- *drift con nuestras entradas alteradas* → `QUARANTINED`, **cero escrituras**, diff preservado;
- *HITL denegado o con timeout sobre una cuarentena* → **no se escribe nada** (default seguro).
**Ancla AST:** el conjunto de módulos que escriben `modlist.txt` está congelado (hoy solo
`mo2/vfs.py`).
**Aceptación.** Ningún camino puede sobrescribir un cambio externo. Ningún test deja
`modlist.txt` alterado.
**Riesgos.** **Alto — es el componente con más potencial de pérdida de datos del plan.**
Mitigación: reversión dirigida, cuarentena terminal, y tests que verifican explícitamente la
supervivencia del cambio ajeno.
**Rollback.** Revert; nada lo consume aún. **Complejidad: Alta.**

---

### PR P7 — Contrato común de adapters (`_contract.py`)

**Objetivo.** Fijar el molde **antes** del primer adapter.
**Archivos.** `local/tools/material/_contract.py` (nuevo) · `local/tools/material/__init__.py` ·
`pyproject.toml` (`strict`) · `tests/test_material_contract.py` (nuevo).
**NO tocar.** Adapters (no existen aún), orquestador, GUI, agente.
**Dependencias.** P1, P5 (para el campo `vfs_evidence`).
**Por qué es un PR propio.** En v1 este contrato nacía dentro del PR del primer adapter. Eso
mezcla dos intenciones y, peor, hace que el molde se diseñe mirando **una** herramienta: el
segundo adapter descubre que no encaja y lo cambia, con el primero ya mergeado. Fijarlo antes
obliga a diseñarlo contra las cuatro (las dos implementadas y las dos declaradas).
**Cambios conceptuales.** `MaterialStepResult` (`success`/`message` canónicos + campos
estructurados). Las siete condiciones de §7.4 como protocolo, con la firma de frescura, el
protocolo de preflight (qué outputs deben estar ON/OFF) y la taxonomía de fallo
(no-op legítimo / failure / parcial / stale). `pipeline_capability` + `material_step` + `tx_id` en
todo registro de fallo.
**Tests.** Que un resultado sintético que incumple **cada una** de las siete condiciones sea
rechazado — siete tests, no uno. Que `normalize_tool_result` consuma un `MaterialStepResult` sin
caer en `"error desconocido"`. Que la firma de frescura detecte: output idéntico, output ausente,
marker con mtime viejo (**H4**: contenido estático, mtime es la única señal).
**Aceptación.** El contrato se sostiene sobre datos sintéticos de las cuatro herramientas, sin que
exista ningún adapter. **Rollback.** Revert. **Complejidad: Media.**

---

### PR P8 — VRAMr: success contract y contrato de resultado (sin cablear)

**Objetivo.** Cerrar los gaps del servicio huérfano antes de exponerlo a superficie alguna.
**Archivos.** `local/tools/vramr_service.py` · `local/tools/output_targets.py` ·
`tests/test_vramr_service.py` · `tests/test_output_targets.py` · `tests/test_dyndolod_service.py`
(sacarlo de `_SERVICIOS_SIN_COBERTURA`) · `tests/test_contrato_argumentos_cli.py`.
**NO tocar.** Dispatcher, `RITUAL_TOOL_MAP`, `AsyncToolRegistry` — **no se cablea acá**.
**Dependencias.** P7 (el contrato). **Solo (g) además:** P0 (B3 residual).

> **Cambio en v3.** Con §2.5 verificado (workflow, args de los siete helpers, presets con
> resoluciones exactas, ruta de detección de PBR, rol de `VRAMr.DB`), la mayor parte de lo que v2
> ponía en "tanda 2 bloqueada por B3" **ya no está bloqueado**. Lo único que sigue dependiendo de
> B3 es **(g)**, porque el conjunto de switches a validar depende de **qué entry point invoca
> Sky-Claw** — launcher, BAT o helpers — y eso es justamente lo que B3 no cerró.

*Tanda 1 — no depende de nada externo:*
- (a) `message: str` canónico junto a `error` legacy (hoy incumple `tool_result.py`).
- (b) `pipeline_capability` + `tx_id` en cada registro de fallo, con `tx_id=None` explícito vía el
  centinela `_SIN_TX_ID` de `subprocess_error_extra`.
- (c) Job Object (`assign_kill_on_close_job`/`close_job`) — cierra
  `docs/pending_ooda_status.md:257-258`.
- (d) Cleanup **recursivo**: hoy `_snapshot_existing` solo rastrea el nivel superior.
- (e) `WritePermissionsChecker` sobre el output target — lo mete en la familia que
  `tests/test_output_targets.py` audita por detección.

*Tanda 2 — (f), (h), (i) ya desbloqueadas por §2.5; solo (g) espera B3:*
- (f) Success contract de `_contract.py`: artefacto + frescura + log + marker (por mtime, H4).
  El marker es `VRAMrOutput.tmp` y los logs van a `Logfiles` (§2.5); lo que falta para afinarlo es
  **R1** (qué escribe el log), no B3.
- (g) Validador de switches administrados fail-closed sobre `args`, calcado de
  `_extra_args_admisibles`, **con ancla AST** de que el conjunto rechazado cubre lo que el
  servicio emite.
- (h) Presets como modelo tipado en vez de `list[str]` opaco. **La tabla de §2.5 es la fuente**:
  seis presets con resoluciones exactas por canal (diffuse/normal/parallax/material) y los rangos
  de Custom (512–4096 pot para diffuse, 512–2048 para el resto). Esto ya no requiere B3.
- (i) Firma de `VRAMr.DB` además del output: es entrada de `Filter.exe`/`Optimise.exe` y una DB
  stale produce resultado plausible pero desactualizado (§2.5).

**Tests.** Presets; valores custom; PBR presente/ausente; archivos parallax/material; conversiones
fallidas; DB/output stale; output viejo → falso verde rechazado; marker sin artifacts; artifacts
sin marker; inyección de switch por payload → rechazada; timeout con nietos → Job Object los mata;
cleanup con subdirectorio preexistente.
**Aceptación.** Ningún camino declara éxito por exit code. `_SERVICIOS_SIN_COBERTURA` pierde la
fila de `vramr_service.py`. `PROCEDENCIA_DE_FLAGS` deja de decir SIN VERIFICAR.
**Riesgos.** Si B3 no cierra, **solo (g)** se bloquea; el resto avanza. **No implementar (g)
contra un CLI adivinado** — es el episodio de `AGENTS.md:269`, donde tres tests congelaron el
defecto y en el rig ningún binario pudo arrancar. **Rollback.** Revert; el servicio sigue huérfano.
**Complejidad: Alta.** Divisible en dos PRs por tanda.

---

### PR P9 — PGPatcher adapter

**Objetivo.** Primer adapter, sobre un contrato ya fijado.
**Archivos.** `local/tools/material/pgpatcher.py` (nuevo) · `local/tools/output_targets.py`
(+ `pgpatcher_output_target`) · `pyproject.toml` (`strict`) ·
`tests/test_material_pgpatcher.py` (nuevo) · `tests/test_output_targets.py`.
**NO tocar.** Orquestador, GUI, agente, `vfs_worker.py` (cerrado en P5), `_contract.py` (P7).
**Dependencias.** P0, P5, P6, P7.
**Cambios conceptuales.** Detección, preflight, submit del `VfsJob`, logging, cancelación,
timeout, validación de output, output administrado bajo el namespace `Sky-Claw/`, rollback.
**El adapter no spawnea**: submite al broker. Distinguir no-op / failure / parcial / stale.
**No asumir** que `PGPatcher.esp`, `ParallaxGen_Diff.json` o `PGPatcher_Output.zip` existan
siempre (B4). **Ninguna precondición sobre ParallaxR ni BENDr.**

**El preflight tiene ahora cinco condiciones, no cuatro** — las cuatro de output ajeno más la de
ownership (§2.6 PG2/PG3, ambas del binario):

```
TexGen output           OFF      "DynDoLOD and TexGen outputs must be disabled…"
DynDOLOD output         OFF      idem
VRAMr output            OFF      "Please disable VRAMr output mod…" (detecta vramroutput.tmp)
PGPatcher output propio OFF      contrato de generación
directorio de salida    VACÍO o SOLO-PGPatcher    ← NUEVO (PGLib.dll, PG3)
```

La quinta es la que v2 no tenía y **la herramienta la impone por su cuenta**: si el directorio
tiene archivos ajenos, aborta. Verificarlo en el preflight convierte un fallo tardío y opaco en
un error accionable antes de lanzar.

**Tests.** Corrida válida; no-op; output stale; VRAMr output activo → preflight falla; TexGen
output activo → preflight falla; DynDOLOD output activo → preflight falla; **directorio de salida
con un archivo ajeno → preflight falla con mensaje accionable**; **directorio vacío → pasa**;
output fresco/vacío/stale/parcial; marker sin artifacts; artifacts sin marker; rollback preserva
preexistentes; timeout; cancelación; **sin atestación de VFS → no se declara éxito**.
**Ancla AST: ningún `taskkill /im` ni kill global en el módulo.**
**Aceptación.** Éxito solo con las siete condiciones de §7.4. Un output de la corrida anterior
**no** produce verde. **Rollback.** Revert. **Complejidad: Media** (el molde ya está en P7, el
riesgo de USVFS ya se pagó en P5).

---

### PR P10 — VRAMr adapter

**Objetivo.** Envolver `VRAMrPipelineService` (endurecido en P8) como adapter.
**Archivos.** `local/tools/material/vramr.py` (nuevo) · `pyproject.toml` (`strict`) ·
`tests/test_material_vramr.py` (nuevo).
**NO tocar.** `vramr_service.py` (P8), `_contract.py` (P7).
**Dependencias.** P7, P8.
**Cambios conceptuales.** Resolución del exe (registry + `PathResolutionService`), selección de
preset, detección de PBR, construcción del vector desde el modelo tipado, preflight (VRAMr propio
y PGPatcher output OFF). **El adapter no reimplementa lock / journal / eventos: los delega al
servicio.** Si termina duplicando algo del servicio, es señal de que ese algo debía estar en P8.
**Tests.** Cada preset; PBR presente/ausente; exe no resuelto → `MISSING` accionable (no crash);
propagación de fallo del servicio; preflight con output propio activo → falla.
**Aceptación.** El adapter no duplica nada del servicio. **Rollback.** Revert.
**Complejidad: Baja.**

---

### PR P11 — `PreLodMaterialPipelineService` (orquestador)

**Objetivo.** Orquestar adapters + `Mo2ModStateTransaction`, con caminos parciales.
**Archivos.** `local/tools/material/pipeline_service.py` (nuevo) · `pyproject.toml` (`strict`) ·
`tests/test_material_pipeline_service.py` (nuevo).
**NO tocar.** Adapters (cerrados), GUI, agente.
**Dependencias.** P6, P9, P10.
**Cambios conceptuales.** Resolución del camino aplicable (no ejecutar pasos inútiles); secuencia
de outputs ON/OFF vía la transacción; convergencia; invalidación de downstream (material pipeline
cambió → TexGen stale → DynDOLOD stale) **reportada, nunca borrada automáticamente**. Los nodos
`implemented=False` devuelven error accionable.
**Tests.** Los tres caminos del primer corte (`PGPatcher → VRAMr`, cada uno solo); pedir un nodo
no implementado → error accionable; herramienta faltante a mitad → aborta con reversión;
cancelación entre pasos; **drift de MO2 entre pasos → cuarentena, sin sobrescribir**; un paso falla
→ reversión dirigida de todo el estado aplicado; invalidación reportada sin borrar.
**Aceptación.** Ningún paso corre con precondiciones incumplidas. Ninguna ruta de fallo
sobrescribe un cambio externo. **Riesgos.** Alto (converge todo); mitigación: los componentes
vienen probados. **Rollback.** Revert. **Complejidad: Alta.**

---

### PR P12 — Superficies GUI + agente

**Objetivo.** Exponer la capacidad en **ambas** superficies, en el mismo PR, sobre el **mismo**
servicio de dominio.
**Archivos (11 de producción — la lista de §1.3).**
`app/orchestrator/tool_strategies/run_material_pipeline.py` (nuevo) ·
`app/orchestrator/tool_dispatcher.py` · `app/orchestrator/tool_strategies/middleware.py` ·
`app/orchestrator/supervisor.py` · `gui/controllers/ritual_runner.py` ·
`gui/views/forge_dashboard.py` · `app/agent/tools/schemas.py` · `app/agent/tools/system_tools.py` ·
`app/agent/tools/__init__.py` · `app/agent/tools_facade.py` · `app_context.py`.
Tests: `tests/test_ritual_dispatch.py` · `tests/bdd/test_mapa_sop_features.py` ·
`tests/test_perfil_sesion_invariante.py` · `tests/test_material_dual_surface.py` (nuevo) ·
`tests/bdd/features/sop_modding/` (feature nuevo o `PENDIENTE` declarado).
**NO tocar.** Adapters, servicio de dominio. **Dependencias.** P11.
**Cambios conceptuales.** Strategy con `describe_for_approval` + filtro de payload (molde:
`GenerateLodsStrategy`), middleware `[gate]` (destructivo → HITL, y por lo tanto entra a
`DESTRUCTIVE_TOOL_PATTERNS`, lo que **obliga** una entrada en `MAPA_SOP_ACCEPTANCE`). Handler del
agente con `params_model` que **no expone `profile`** (`test_perfil_sesion_invariante.py`).
**Cero reglas de dominio en cualquiera de las dos superficies.**
**Tests.** **Ancla enumerativa dual (lo más importante del PR):** el conjunto de operaciones del
material pipeline alcanzables desde la GUI == el alcanzable desde el agente LLM, salvo exenciones
declaradas con motivo (patrón de `test_skse_es_gui_only_y_no_lo_alcanza_el_agente_llm`).
`RITUAL_TOOL_MAP == {...}`. `set(MAPA_SOP_ACCEPTANCE) == set(DESTRUCTIVE_TOOL_PATTERNS)`. Ningún
`params_model` expone `profile`. Payload no puede inyectar switches por ninguna vía. Preview,
progreso, cancelación, errores estructurados, resumen de output, implicaciones de re-ejecución.
**Aceptación.** Las dos superficies llegan al mismo servicio; el ancla dual pasa; las cuatro
anclas preexistentes pasan sin relajarse.
**Riesgos.** **Es el PR donde históricamente aparece el defecto dominante del repo** (9 de 13
episodios auditados). Mitigación: el ancla dual, no una revisión manual.
**Rollback.** Revert; la capacidad queda inalcanzable pero intacta. **Complejidad: Media.**

---

### PR P13 — Validación en rig real

**Objetivo.** Convertir los invariantes inferidos en verificados.
**Archivos.** `docs/validation/` · `docs/pending_ooda_status.md` · `sky_claw/local/AGENTS.md`
(§2.x nuevos, con el nivel de evidencia de §2.9).
**NO tocar.** Código, salvo los fixes que el rig descubra (cada uno su propio PR).
**Dependencias.** P12.
**Cambios conceptuales.** Ejecutar la matriz de §9.4 y **cerrar o rechazar R1–R8 con evidencia**.
**Aceptación.** Cada invariante NO VERIFICADO queda cerrado con evidencia o explícitamente sigue
abierto. **Por herramienta por separado**, como el gate de §2.9: un check que pasó en PGPatcher no
dice nada sobre VRAMr — uno es C++ bajo USVFS, el otro PowerShell.
**Complejidad: Alta** (esfuerzo operativo).

---

### Segundo corte (fuera del camino crítico)

#### PR-A — ParallaxR adapter
**Bloqueado SOLO por B6-L** (permiso del autor para invocar los helpers). **B6-T está cerrado**
por H1: los helpers son CLIs Rust/clap con args conocidos.
**Archivos.** `local/tools/material/parallaxr.py` · `local/tools/material/_layered_extract.py`
(etapa compartida, H8) · registry · `output_targets.py` · `tests/` ·
`tests/test_contrato_argumentos_cli.py` (procedencia: *"gavwhittaker/ParallaxR — tabla de args de
clap en ExtractBSA.exe; `--help` de cada helper en rig <fecha>"*).
**Cambios.** Orquestar `MakeUnpack → ExtractBSA → LooseCopy → Exclusions → ParallaxRFilter →
HeightMap → [limpieza de colisiones `_p.dds`] → OutputQC` **sin el driver BAT**, y por lo tanto
**sin** ninguno de los comportamientos de H2. Preflight de H6 (espacio × 1.75, raíz de unidad,
workspace previo). Corre bajo el handler USVFS de P5 (H3).
**Tests.** `_p.dds` faltantes generados; **ningún `_p.dds` del output colisiona con uno loose
preexistente** (H5, *no* comparar hashes); exclusiones respetadas; fallo de `OutputQC`; marker
fresco por mtime (H4); espacio insuficiente → preflight falla; workspace previo → error accionable;
ancla AST de "ningún kill global"; ancla de que `AntiSleep.ps1` no se lanza. **Complejidad: Alta.**

#### PR-B — BENDr adapter
**Bloqueado por** PR-A (comparte `_layered_extract`) y B6-L.
**Cambios.** `MakeUnpack → ExtractBSA → LooseCopy → Exclusions → BENDrFilter → BENDr --tool
texconv`. Definición explícita de no-op legítimo. **Contrato de prioridad con ParallaxR marcado
INVARIANTE NO VERIFICADO (R2)** — configurable, no congelado, con advertencia en el resultado.
**Tests.** Par normal/parallax válido; par faltante; asset excluido (PBR/terrain/LOD/DynDOLOD/
LODGen); cero trabajo legítimo vs. fallo; **los dos logs presentes y frescos** (H7).
**Complejidad: Media** (hereda el molde y la etapa compartida).

#### PR-C — VRAMr a `AUTO_NEXUS`
**Bloqueado por** B1. **Cambios.** Un campo del `ExternalToolSpec` + `ensure_vramr` sobre
`_ensure_nexus_mod` + pin en `_PINNED_SHA256`. La UX de fallback manual no cambia.
**Complejidad: Baja.**

---

## 9. Matriz de tests

### 9.1 Unit

| Área | Casos |
|---|---|
| Discovery | tool missing · found · wrong executable · unsupported version · version unknown · moved installation · invalid path · stale configured path |
| Registry | igualdad literal del dict · toda lista derivada coincide · `MANUAL_ONLY` ⟺ sin `installer_method` · **ningún `install_kind=TOOL` resuelve bajo `mods/`** |
| Versión | PE con/sin versión · sin `pefile` · `--version` OK/falla/timeout · rangos |
| Success contract | las 7 condiciones, **una por test** · fresh valid · empty · stale · partial · output de corrida anterior · marker con mtime viejo · artifacts sin marker |
| Args administrados | cada switch de path rechazado por payload · variantes de caso y prefijo · ancla AST de cobertura |
| `output_targets` | un target por adapter · ancla de completitud de la familia |

### 9.2 Integration

| Área | Casos |
|---|---|
| Installer | extracción que falla a mitad → destino intacto · carrera por el nombre · acepta · declina · fallo de red · archivo corrupto · contenido inesperado · disco insuficiente · rollback · manual requerido · premium faltante → fallback · hash mismatch en fallback |
| MO2 | perfil correcto/incorrecto · VFS no disponible · output esperado habilitado · output prohibido habilitado · prioridad no coincide · reversión dirigida tras fallo · tras cancelación · **drift con nuestras entradas intactas → el cambio ajeno sobrevive** · **drift con nuestras entradas alteradas → cuarentena, cero escrituras** · **HITL denegado sobre cuarentena → no se escribe nada** |
| Proceso | éxito · rc≠0 · timeout · cancelación · nieto sobrevive → Job Object · **ningún kill global** (ancla AST) · **`AntiSleep.ps1` no se lanza** (ancla AST) |
| Broker | `tool_id` no allowlisted · atestación falla · manifiesto mal firmado · VFS ausente |
| Pipeline | caminos del corte · nodo `implemented=False` → error accionable · tool faltante a mitad · fallo → reversión total · invalidación reportada sin borrar |

### 9.3 Contract (anclas enumerativas)

| Ancla | Qué congela |
|---|---|
| `test_registro_de_herramientas.py` (nuevo) | El registry por igualdad literal + derivación de las 5 listas |
| `ALLOWED_VFS_TOOL_IDS == set(_default_handlers())` (nuevo) | Los dos sitios del broker que deben coincidir |
| `test_modlist_writers_invariant.py` (nuevo) | Qué módulos escriben `modlist.txt` (AST) |
| `test_material_dual_surface.py` (nuevo) | GUI ≡ agente LLM, salvo exenciones declaradas |
| `test_output_targets.py` (extendido) | La familia de servicios con destino de escritura |
| `test_contrato_argumentos_cli.py` (extendido) | Todo lanzador nuevo declara procedencia de flags |
| `test_ritual_dispatch.py` (extendido) | `RITUAL_TOOL_MAP` literal |
| `test_dyndolod_service.py` (extendido) | Cobertura acepta `pipeline_capability`; `pipeline_stage=9` sigue siendo solo DynDOLOD |
| `bdd/test_mapa_sop_features.py` (extendido) | `set(MAPA_SOP_ACCEPTANCE) == set(DESTRUCTIVE_TOOL_PATTERNS)` |
| `test_perfil_sesion_invariante.py` (extendido) | Ningún `params_model` nuevo expone `profile` |

**Nota de forma** (`AGENTS.md:355`): las anclas nuevas deben **enumerar, no muestrear**, y
auditarse con el **mismo** helper que las existentes, no con una copia "parecida" — escribirle su
propio loop a un componente nuevo es el defecto dominante cometido dentro del archivo que existe
para atajarlo.

### 9.4 Real-rig (P13 — mocks no valen)

MO2 con perfil chico · perfiles grandes (>1000 mods) · loose textures · BSA · PBR presente ·
parallax presente · Complex Material · mods mezclados · **rutas con espacios** · **rutas
Unicode** · cancelación a mitad · disco insuficiente · herramienta ausente · output viejo
presente · output activo cuando debería estar OFF · crash de helper · **operador modifica MO2
durante la ejecución** · validación visual en Skyrim.

**Regla del gate, calcada de §2.9:** cada condición se verifica **por herramienta por separado**.
Las dos filas obligatorias por separado: **rutas con espacios** (el episodio de `AGENTS.md:269`
empezó exactamente ahí) y **output viejo presente** (el falso verde que el success contract existe
para atajar).

---

## 10. Riesgos y decisiones pendientes

### BLOCKER

| # | Riesgo | Impacto | Cierre |
|---|---|---|---|
| BL-1 | **B7 — `PGPatcher.esp` frente a LOOT / Wrye Bash / Synthesis** | Si Wrye Bash o Synthesis necesitan verlo, el nodo `mesh_materials` se mueve antes del stage 6 y **P1 congela el grafo equivocado**. **No congelar la posición hasta cerrarlo** | P0 (fuente GPL-3 + docs upstream) |
| BL-2 | B4 — `pgtools.exe` ¿sustituto o subconjunto de `PGPatcher.exe`? (eje T) | Define si P9 corre headless o queda *assisted* como Stage 9. Los nombres compartidos en strings **no** deciden esto | P0 (fuente GPL-3) |
| BL-3 | B5 — qué desactiva `--ignore-mo2vfscheck` (eje T) | La existencia del flag **no** prueba que un run sin VFS produzca un patch correcto. Si solo apaga un guard, la opción B de §6.4 se descarta y P5 es obligatorio | P0 (fuente GPL-3) |
| BL-4 | B3 residual — entry point headless soportado de VRAMr (eje T) | Solo bloquea **P8(g)**. Workflow, args y presets **ya están verificados** (§2.5), así que el alcance de este blocker se redujo mucho en v3 | P0 (rig) |
| BL-5 | Pérdida de datos en `Mo2ModStateTransaction` | Un rollback ciego destruye el cambio del operador | P6: reversión dirigida + cuarentena terminal + tests de supervivencia del cambio ajeno |
| BL-6 | El handler del worker puede quedar fuera del build congelado | Funciona en dev y falla en producción, en silencio | P5: verificar el build de PyInstaller como criterio de aceptación |
| BL-7 | **Vendorizar artefactos de terceros en el repo MIT** | Riesgo de licencia; y con PGPatcher (GPL-3) además arrastra obligaciones de la GPL sobre un repo MIT | §3.5: P0 documenta **derivado**, nunca copias íntegras. Criterio de aceptación explícito de P0 |

### IMPORTANT

| # | Riesgo | Mitigación |
|---|---|---|
| IM-1 | El defecto de hermanos en **P12** (GUI vs agente) — 9 de 13 episodios | Ancla dual enumerativa |
| IM-2 | Allowlist del broker y `_default_handlers()` son dos sitios sin ancla | Ancla de igualdad literal en P5 |
| IM-3 | `setup_tools` es `if/elif` por tool, sin ancla | Derivación de tabla + ancla (P4) |
| IM-4 | Instalación de `TOOL` no atómica ni con rollback | P4 — **no** se sortea metiéndolas en `mods/` (§5.3) |
| IM-5 | VRAMr sin Job Object → huérfanos con file locks | P8 tanda 1 |
| IM-6 | `_cleanup_output_dir` de VRAMr solo cubre el nivel superior | P8 tanda 1 |
| IM-7 | El lock de `modlist.txt` es de Sky-Claw; `MO2.exe` no lo respeta | Documentar el límite; la cuarentena lo ataja a posteriori, no lo previene |
| IM-8 | La rama pre-LOD no tiene guard runtime (igual que los 9 stages) | Consistente con el estado actual; anotar, no resolver acá |
| IM-9 | Todo lo que este plan toca está exento de `mypy` | Opt-in a `strict` por módulo nuevo, en su propio PR |
| IM-10 | **B6-L** — permiso para invocar los helpers de ParallaxR/BENDr | Solo bloquea PR-A/PR-B. B6-T está cerrado (§2.2) |
| IM-11 | B1 — permisos de Nexus | `MANUAL_ONLY` es el default conservador y funcionalmente completo |
| IM-12 | R2 — prioridad BENDr > ParallaxR es inferida | Solo PR-B; irá configurable, no congelada |
| IM-13 | R8 — la limpieza de `_p.dds` ignora BSAs (§2.4 H5) | Solo PR-A; verificar en rig antes de escribir su post-check |
| IM-14 | **Casi toda la evidencia externa es T2**: SHA-256 declarados, artefactos fuera del repo | P0 documenta hashes + conclusiones **sin vendorizar** (§3.5). Un agente posterior que necesite actuar sobre un hecho T2 debe pedir el artefacto, no asumir que puede re-derivarlo |
| IM-15 | **Identificar artefactos por nombre de archivo** — el error de H1 en v2 | Regla de método en §2.2: hash + coherencia con la invocación a la vista. Vale para cualquier binario que llegue en el futuro |
| IM-16 | `VRAMr.DB` stale produce resultado plausible pero desactualizado | P8(i): firmar la DB además del output |
| IM-17 | `tools_installer` no escribe en el `OperationJournal` | Fuera de scope; anotar como deuda |

### NICE TO HAVE

- Poblar `_PINNED_SHA256` (hoy vacío).
- Detección de versión para Wrye Bash, DynDOLOD, BodySlide.
- Guard runtime de precondiciones del DAG completo (cerraría la deuda de `AGENTS.md:340`).
- Cerrar la cobertura de `pipeline_stage` en los 6 servicios que hoy no la emiten.
- Detección de Auto Parallax como contexto informativo (sin acción automática).
- Reimplementar `GrassProfileManager` sobre `Mo2ModStateTransaction`, si P6 demuestra que encajan.

---

## 11. Orden recomendado de ejecución

```
P0  ─── verificación en rig + consolidación de evidencia
 │       cierra B3/B4/B5/B7 · versiona H1–H8 en docs/
 │
 ├── P1  contrato de lifecycle          ← depende de P0 SOLO por B7
 │    │
 │    ├── P2  ToolReadiness + ExternalToolSpec
 │    │    ├── P3  detección de versión
 │    │    └── P4  instalación TOOL atómica + PGPatcher + setup_tools derivado
 │    │
 │    ├── P5  handler USVFS genérico      ← infra compartida (H3); también necesita P0
 │    │
 │    ├── P6  Mo2ModStateTransaction      (alto riesgo → arrancar temprano)
 │    │
 │    └── P7  contrato de adapters        ← ANTES del primer adapter; necesita P5
 │         └── P8  VRAMr success contract (tanda 1 no espera a P0; tanda 2 sí)
 │
 ├── P9  PGPatcher adapter                (P0 + P5 + P6 + P7)
 │    └── P10 VRAMr adapter               (P7 + P8)
 │
 ├── P11 orquestador                      (P6 + P9 + P10)
 ├── P12 superficies GUI + agente
 └── P13 validación en rig

Segundo corte:  PR-A ParallaxR · PR-B BENDr · PR-C VRAMr AUTO_NEXUS
```

**Camino crítico:** `P0 → P1 → P5 → P7 → P9 → P11 → P12 → P13`.

**Por qué P0 primero, aun con rig.** Cuatro bloqueantes son preguntas sobre herramientas externas,
y uno de ellos (**B7**) puede mover un nodo del grafo que P1 congela. El repo tiene un antecedente
caro de implementar contra un CLI inferido: `AGENTS.md:269` documenta que tres tests congelaron el
defecto de serialización en vez de atajarlo, y en el rig **ningún** binario pudo arrancar (2/2).
Con rig y con la fuente GPL-3 de PGPatcher, P0 cuesta horas.

**Por qué P1 ya no puede correr en paralelo con P0** (cambio respecto de v1): B7 decide dónde va
`mesh_materials`. Congelar el grafo antes de saberlo es exactamente el retrofit que el plan dice
evitar. Lo que **sí** puede arrancar en paralelo: P6 (no depende del grafo) y P8 tanda 1 (defectos
verificados del servicio, independientes de todo lo demás).

**Por qué P5 se adelantó.** Por **H3**, las cuatro herramientas leen el `Data` virtualizado. El
handler USVFS dejó de ser un detalle de PGPatcher y pasó a ser infraestructura de la capacidad
entera; además P7 necesita su `vfs_evidence`.

**Por qué P6 temprano.** Es el componente con más potencial de pérdida de datos y el que más
revisión va a necesitar. Empezarlo tarde lo pone en el camino crítico sin margen.

**Por qué P7 antes que cualquier adapter** (cambio respecto de v1): un contrato diseñado dentro
del PR del primer adapter se diseña mirando **una** herramienta. El segundo descubre que no encaja
y lo cambia con el primero ya mergeado. Fijarlo antes obliga a validarlo contra las cuatro.

**Por qué P8 antes que P10.** El servicio VRAMr ya existe con gaps verificados. Endurecerlo primero
evita que el adapter los herede o los disimule. Su tanda 1 arranca sin esperar a nadie.

**Por qué P12 al final y en un solo PR.** Las dos superficies tienen que aparecer juntas.

**Secuenciación sugerida:** arrancar P0 y, en paralelo, P6 y P8-tanda-1. El rig y el trabajo de
código no compiten por el mismo recurso.

---

## 12. GO / NO-GO

### Veredicto: **GO** — con P0 como primera tarea

**GO inmediato, sin ninguna dependencia externa:**

| PR | Por qué no está bloqueado |
|---|---|
| **P6** `Mo2ModStateTransaction` | Infraestructura MO2 interna; `GrassProfileManager` es el precedente; no depende del grafo ni de ninguna herramienta |
| **P8 tandas 1 y 2 salvo (g)** VRAMr | Cinco defectos verificados del servicio (`message` faltante, sin `pipeline_capability`, sin Job Object, cleanup no recursivo, sin `WritePermissionsChecker`) **más** el modelo de presets, la firma de `VRAMr.DB` y el success contract, que §2.5 desbloqueó |

**GO tras P0** (horas de trabajo, no semanas): P1, P2, P3, P4, P5, P7, P8(g), P9, P10, P11,
P12, P13.

**Lo que P0 tiene que producir:**

| Ítem | Cómo se cierra | Necesita rig |
|---|---|---|
| **B7** — `PGPatcher.esp` × LOOT/Wrye Bash/Synthesis | Docs upstream + fuente GPL-3; contrastar con la convención del repo (plugins generados van al final, `AGENTS.md:208`, `:273`) | No |
| B4 — `pgtools.exe` ¿sustituto o subconjunto? | Leer el parser en `hakasapl/PGPatcher` | No |
| B5 — qué desactiva `--ignore-mo2vfscheck` | Leer el código del check en la misma fuente | No |
| B3 residual — entry point headless soportado de VRAMr | Rig: determinar si `VRAMr.exe` tiene modo no interactivo y a quién debe llamar Sky-Claw | Sí |
| R1 — contratos de éxito | Una corrida manual de cada herramienta, con log y árbol de procesos | Sí |
| **Documento de evidencia derivado** (§3.5) | Hashes + args + workflow + conclusiones, **sin vendorizar** | No |

**Diferido explícitamente, sin bloquear:**

- **B1** (permisos Nexus) — solo decide AUTO vs MANUAL para VRAMr; `MANUAL_ONLY` es completo.
- **B6-L** (permiso para invocar helpers) — solo PR-A/PR-B. **B6-T está cerrado**: los BAT
  demuestran interfaces CLI invocables (§2.2), y H2 muestra que orquestar los helpers directamente
  **evita** el comportamiento peligroso en vez de heredarlo. La viabilidad técnica de ParallaxR y
  BENDr está probada; lo único que falta es el permiso.
- **R2, R8** — solo PR-A/PR-B.
- **Auto Parallax** — resuelto, no diferido: es runtime SKSE, no un nodo (§2.7).

**El riesgo que puede cambiar el plan:** si P0 determina que PGPatcher **no tiene CLI completa** y
requiere interacción con su GUI, P9 se replantea. El precedente del repo existe y es aceptable:
Stage 9 es explícitamente *assisted* — publica `assisted=True` con instrucciones y espera con
timeout de 4 h (`AGENTS.md:270`). Sería una degradación de UX, no un rediseño. Automatizar mouse o
coordenadas queda descartado en cualquier caso.

---

## Verificación de este plan

Este PR es de planificación; la verificación es de sus afirmaciones, no de código:

```bash
# 1. El DAG no existe en runtime (5 emisores, ningún registry)
grep -rn "pipeline_stage" --include="*.py" sky_claw/ | grep -v test

# 2. Las herramientas del material pipeline no existen en el repo
for t in ParallaxR BENDr PGPatcher ParallaxGen parallax TruePBR texconv; do
  echo "$t -> $(grep -ril "$t" . | grep -v '^./.venv' | grep -v '^./.git/' | wc -l)"
done

# 3. VRAMr está huérfano
grep -rn "vramr\|VRAMr" --include="*.py" sky_claw/app/    # sin resultados

# 4. La allowlist del broker tiene 2 entradas y no hay ancla que la ate al handler map
grep -n "ALLOWED_VFS_TOOL_IDS" sky_claw/local/mo2/vfs_contracts.py
grep -n "_default_handlers" -A 2 sky_claw/local/mo2/vfs_worker.py
grep -rn "ALLOWED_VFS_TOOL_IDS" tests/          # ninguna igualdad contra _default_handlers

# 5. No hay campo de config ni env var para VRAMr
grep -n "vramr" sky_claw/config.py sky_claw/app/core/path_resolver.py   # sin resultados

# 6. El nombre ExternalToolSpec no colisiona (ToolDescriptor ya existe y es otra cosa)
grep -rn "class ToolDescriptor" sky_claw/       # agent/tools/descriptor.py:25

# 7. Baseline verde antes de tocar nada
.venv/bin/python -m pytest -q
ruff check sky_claw/ tests/ && ruff format --check sky_claw/ tests/ && mypy sky_claw/
```

### Nota de gates: TODO lo que este plan toca está exento de mypy

Verificado en `pyproject.toml:196-218` (`ignore_errors = true`): `sky_claw.local.tools.*`,
`sky_claw.local.mo2.*`, `sky_claw.local.discovery.*`, `sky_claw.local.tools_installer`,
`sky_claw.config`, `sky_claw.app.orchestrator.*`, `sky_claw.app.agent.*`, `sky_claw.app.gui.*`.
Y `BLE001` de ruff está exento en todo `sky_claw/app/**`. **Ningún módulo nuevo saldría
type-checkeado por defecto.**

**Convención para todos los PRs de este plan** — el repo ya tiene el precedente y su justificación
textual (`pyproject.toml:283-292`, sobre `sky_claw.app.security.links`):

> *"módulo nuevo, nace estricto. El opt-in es OBLIGATORIO y no cosmético — `sky_claw.app.security.*`
> está en la lista `ignore_errors` de arriba […] así que sin esta línea el módulo saldría a
> producción sin type-check NI guard de except."*

Cada módulo nuevo (`local/tools/material/*`, `local/mo2/mod_state_tx.py`,
`local/discovery/registry.py`) se agrega a la lista `strict = true` en su propio PR. Cuesta una
línea y es la diferencia entre tener un gate y no tenerlo.
