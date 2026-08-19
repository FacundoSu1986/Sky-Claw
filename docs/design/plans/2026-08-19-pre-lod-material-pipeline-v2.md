# Pre-LOD Material Pipeline — Plan de integración · **v2**

> **Modo:** análisis y planificación. Cero código, cero modificaciones al repo, cero commits.
> Base: `main` @ `fd1d3c6`. **v2 incorpora los artefactos reales suministrados por el operador**
> (`ParallaxR.bat` v3.0318, `BENDr.bat` v3.0331, `ExtractBSA.exe`) como evidencia primaria.

---

## Context

Sky-Claw automatiza el pipeline de modding de Skyrim SE/AE sobre MO2. Su DAG canónico
(`sky_claw/local/AGENTS.md` §1) tiene 9 stages y termina en Stage 9 = TexGen → DynDOLOD.
El ecosistema moderno de materiales (parallax / complex material / PBR / optimización de
VRAM) vive **antes** del LOD y hoy **no existe en el repo**: `ParallaxR`, `BENDr`,
`PGPatcher`, `ParallaxGen`, `TruePBR`, `parallax`, `complex material` y `texconv` tienen
**cero ocurrencias** en todo el árbol (grep case-insensitive incluyendo docs y tests).

El objetivo es una rama semántica pre-LOD, con adapters independientes bajo un orquestador,
que converja antes de TexGen/DynDOLOD **sin renumerar Stage 9** y sin duplicar la
infraestructura ya madura del repo.

**Hallazgo estructural:** casi toda la infraestructura que el brief pedía construir (P2/P3/P8)
**ya existe y está bien testeada**. El trabajo real es de **extensión y cableado**.

---

## Qué cambió en v2 (y por qué)

| # | Corrección | Motivo |
|---|---|---|
| 1 | Los BAT y `ExtractBSA.exe` pasan a **evidencia primaria** (§2.2) | Son artefactos reales, leídos en esta sesión |
| 2 | **B6 se parte en B6-T (técnico) y B6-L (permisos)**; B6-T queda **CERRADO** | Los helpers son CLIs Rust/clap completas: verificado en el binario |
| 3 | **Todo el material pipeline requiere USVFS** — no solo PGPatcher | Probado por el guard `if exist ".\<Tool>Output.tmp"` (§2.2, hallazgo H3) |
| 4 | El handler USVFS se adelanta y se generaliza: es infra compartida, no un PR de PGPatcher | Consecuencia de (3) |
| 5 | El primer corte deja de depender conceptualmente de ParallaxR/BENDr | Instrucción del operador + los nodos son productores **opcionales**, no precondiciones |
| 6 | El **contrato común de adapters** pasa a un PR propio, **antes** del primer adapter | Escribir dos adapters en paralelo produce dos moldes que hay que reconciliar |
| 7 | `Mo2ModStateTransaction` se rediseña: **drift → cuarentena, nunca rollback ciego** | Restaurar un snapshot sobre un archivo que el operador cambió destruye su cambio |
| 8 | La estrategia de instalación pasa a ser **per-tool**, y se separa *tool install* de *output-mod install* | v1 las confundía y proponía meter ejecutables en `mods/` |
| 9 | **Nuevo BLOCKER**: relación de `pgpatcher.esp` con LOOT / Wrye Bash / Synthesis | PGPatcher genera un plugin *después* de los stages que lo deberían haber ordenado |
| 10 | `ToolDescriptor` (discovery) → **`ExternalToolSpec`** | Colisiona con `sky_claw/app/agent/tools/descriptor.py::ToolDescriptor` |
| 11 | Renumeración completa P0–P13 + PR-A/B/C | Consistencia interna |

---

## Alcance acordado del primer corte

**Herramientas: PGPatcher + VRAMr.** PGPatcher es GPL-3 con releases de GitHub (adquisición
automatizable, licencia verificada); VRAMr ya tiene un servicio transaccional maduro que solo
está huérfano.

**Diferidos: ParallaxR y BENDr** (PR-A / PR-B). Con la evidencia nueva su viabilidad **técnica**
está probada — lo único que las bloquea es B6-L (permisos del autor para invocar los helpers
directamente).

**Independencia del primer corte — explícita.** PGPatcher **no requiere** ParallaxR ni BENDr:
opera sobre los mapas y materiales que existan en el load order, vengan de donde vengan (mods
retexture, BSAs, o nada). VRAMr tampoco. En el grafo, `parallax_maps` y `normal_rework` son
**productores opcionales**, no precondiciones de `mesh_materials`. Ninguna precondición,
success contract ni test del primer corte menciona a ParallaxR o BENDr.

**Adquisición de VRAMr: `MANUAL_ONLY`** en el primer corte (el operador lo instala; Sky-Claw
detecta, valida y usa). `AUTO_NEXUS` es PR-C, un campo del `ExternalToolSpec`.

**Los cuatro nodos se declaran desde P1** con `implemented=False` en los dos diferidos, para que
el segundo corte agregue adapters **sin rediseñar el grafo**.

---

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

### 2.1 Repo (confianza ALTA — leído del árbol en `fd1d3c6`)

| Hecho | Fuente | Confianza |
|---|---|---|
| El DAG de 9 stages es prosa, no runtime | `sky_claw/local/AGENTS.md:35-82` (auto-declarado L70) | Alta |
| `pipeline_stage` es un int suelto; 5 emisores | grep exhaustivo | Alta |
| `ParallaxR`/`BENDr`/`PGPatcher`/`ParallaxGen`/`parallax`/`TruePBR`/`texconv`/`complex material` = 0 ocurrencias | `grep -ril` sobre todo el árbol | Alta |
| `VRAMrPipelineService` sin callers en `sky_claw/app/` | grep | Alta |
| Éxito de VRAMr = solo exit code | `vramr_service.py:186-188` | Alta |
| VRAMr devuelve `error`, no `message` | `vramr_service.py:444-468` | Alta |
| `_cleanup_output_dir` solo nivel superior | `vramr_service.py:391-394` | Alta |
| VRAMr no usa `WritePermissionsChecker` → fuera de la familia de `output_targets` | `tests/test_output_targets.py:69-79` | Alta |
| Flags de VRAMr declarados SIN VERIFICAR; "scripts PowerShell en Nexus" | `tests/test_contrato_argumentos_cli.py:245` | Alta |
| Ningún runner hereda USVFS; único camino es el broker | `local/tools/output_targets.py:1-46` | Alta |
| `ALLOWED_VFS_TOOL_IDS = {"health", "loot_sort"}` | `mo2/vfs_contracts.py:20` + `vfs_worker.py:302` | Alta |
| `VfsJob`/`VfsJobResult` genéricos | `mo2/vfs_contracts.py:100`, `:204` | Alta |
| El bridge solo acepta `launch_worker`/`cancel`/`health`; el worker es FIJO | `plugin_bundle/skyclaw_bridge/plugin.py:264`, `runtime.py:17` | Alta |
| Ejecución USVFS = `IOrganizer.startApplication(...)` + `Win32JobObject` | `runtime.py:241`, `:382` | Alta |
| `add_mod_to_modlist` = append = máxima prioridad loose | `mo2/vfs.py:218` + `AGENTS.md:308` | Alta |
| No hay API de prioridad ni snapshot de `modlist.txt` | `mo2/vfs.py:98-468` | Alta |
| `GrassProfileManager` clona perfil, crea mod, togglea, teardown | `mo2/grass_profile.py:103,188,246,304` | Alta |
| `NexusDownloader`: `PremiumRequiredError` + fallback manual con hash | `nexus_downloader.py:66,576,638` | Alta |
| HITL obligatorio (`category="download"`) en 7 call-sites | `tools_installer.py:1168,1234,1329,1438,1859,2239,2376` | Alta |
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
| Único PR abierto (#488), colisión baja | `list_pull_requests` + `get_files` | Alta |

### 2.2 Artefactos del operador (confianza ALTA — leídos en esta sesión)

**Fuentes:** `ParallaxR.bat` v3_0318 (gavwhittaker, March 2026, 431 líneas) · `BENDr.bat`
v3.0331 (mismo autor, 370 líneas) · `ExtractBSA.exe` (PE32+ x86-64 consola, 3.0 MB).
Estos archivos **no están en el repo**: si se quiere que sean auditables por agentes
posteriores, hay que versionarlos (o sus transcripciones) bajo `docs/`. Mientras tanto, cada
fila de abajo cita línea del artefacto.

#### H1 — Los helpers son CLIs completas (Rust + clap). **B6-T CERRADO.**

Invocaciones textuales, idénticas en ambos BAT salvo el procesador final:

| Helper | Invocación verbatim | BAT · línea |
|---|---|---|
| `MakeUnpack.exe` | `--profile "!MO2Profile!" --GameDir .\` | PxR 290 · BENDr 296 |
| `ExtractBSA.exe` | `--source .\*.bsa --dest "<TMP>\Output" --logfile "<TMP>\Logfiles" --layered "%TEMP%" --filter *_n.dds *_p.dds` | PxR 295 · BENDr 301 |
| `LooseCopy.exe` | `--source .\textures --dest "<TMP>\Output\textures" --logfile "<TMP>\Logfiles" [--verbose] --filter *_n.dds *_p.dds` | PxR 306 · BENDr 310 |
| `Exclusions.exe` | `--Exclude "<ScriptDir>\tools\Exclusions.mod" --Dest "<TMP>\Output" --Logfile "<TMP>\Logfiles"` | PxR 315 · BENDr 317 |
| `ParallaxRFilter.exe` | `--source "<TMP>\Output" --logfiles "<TMP>\Logfiles"` | PxR 324 |
| `HeightMap.exe` | `--Source "<TMP>\Output" --Logfile "<TMP>\Logfiles"` | PxR 333 |
| `OutputQC.exe` | `--source "<TMP>\Output" --logfile "<TMP>\Logfiles"` | PxR 349 |
| `BENDrFilter.exe` | `--source "<TMP>\Output" --logfiles "<TMP>\Logfiles"` | BENDr 324 |
| `BENDr.exe` | `--source "<TMP>\Output" --logfile "<TMP>\Logfiles" --tool "<ScriptDir>\tools\texconv.exe"` | BENDr 331 |

**Confirmado contra el binario** (`strings` sobre `ExtractBSA.exe`): es un ejecutable **Rust
con `clap_builder` 4.5.60**, compilado por `gavwh`. Tabla de argumentos en el binario:
`source · dest · filter · logfile · layered · db · gamedir`. Requeridos, según sus propios
mensajes de error: **`source`, `dest`, `logfile`**. Descripción embebida: *"Extracts metadata
from Bethesda Software Archives."* Usa `lz4_flex` 0.11.5 y **mantiene una base SQLite**
(strings `JOIN clause`, `TRIGGER`, `-journal`, `-rollback`, `-cached`) — de ahí el `--db` y el
`del *.db` de limpieza (PxR 375).

**Consecuencias.** (a) `--help` funciona en todos los helpers (es clap): la lista autoritativa se
obtiene en el rig sin adivinar. (b) El BAT es **solo un driver**: prompts GUI + secuencia +
el comportamiento global peligroso. Orquestar los helpers desde Sky-Claw **evita íntegramente**
ese comportamiento. (c) `--logfiles` (plural) en los dos `*Filter.exe` frente a `--logfile`
(singular) en el resto: inconsistencia real del upstream, no un typo de transcripción.

#### H2 — El comportamiento global peligroso, verificado (y peor de lo descrito)

| Qué | Dónde |
|---|---|
| `taskkill /f /im powershell.exe` **al arrancar** (mata sesiones PowerShell del usuario) | PxR 41 · BENDr 49 |
| `taskkill /f /im Powershell.exe` + `texconv.exe` [+ `Convert.exe`] al terminar | PxR 422-424 · BENDr 367-368 |
| `Get-Process \| ForEach-Object { ...GetProcessById($_.Id).MinWorkingSet = 0 }` — **sobre TODO proceso del sistema** | PxR 414, 428 (en `:Flush`, tras cada etapa) · BENDr 353 (una vez) |
| `start /min /low powershell.exe -ExecutionPolicy Bypass -File "<ScriptDir>\tools\AntiSleep.ps1"` | PxR 42 · BENDr 50 |
| `timeout /t 3` entre etapas (`:Flush`) | PxR 410 |

**Detalle que importa para el diseño:** `AntiSleep.ps1` **no se detiene explícitamente** — lo
mata el `taskkill /f /im powershell.exe` global de `:Terminate`. Un port ingenuo que suprima el
taskkill (correcto) y conserve el AntiSleep (incorrecto) **leakea el proceso**. Sky-Claw no debe
lanzar ninguno de los dos: usa su propia abstracción de anti-suspensión, acotada al lifetime de
la operación y restaurada en `finally`.

#### H3 — Todas las herramientas leen el `Data` **virtualizado**: requieren USVFS

El guard de "mi output ya está activo" es (PxR 143 · BENDr 151):

```bat
cd /d "%GameDir%"
if exist ".\ParallaxROutput.tmp" (  ERROR - Existing ParallaxR Output Enabled in your Mod Manager  )
```

`ParallaxROutput.tmp` es un archivo que vive **dentro de la carpeta de output**, o sea dentro de
un **mod de MO2**. Solo aparece en `Data\` si MO2 tiene ese mod **habilitado y virtualizado**.
Es decir: **el propio guard prueba que el BAT espera correr con `Data` = vista USVFS.**

Lo confirma la detección de gestor: `tasklist /fi "ImageName eq ModOrganizer.exe"` (PxR 239 ·
BENDr 245) — detecta MO2 **en ejecución**, no instalado, que es la condición de estar lanzado
desde MO2. Y `LooseCopy --source .\textures` copia del `Data\textures` **físico**: sin VFS solo
vería vanilla.

`MakeUnpack.exe --profile <perfil>` es complementario, no sustituto: escribe
`%TEMP%\Unpack-Order.tmp` (borrado al arrancar, PxR 46 · BENDr 54) que `ExtractBSA --layered
"%TEMP%"` consume para extraer BSAs **en orden de prioridad de mods** — orden que la VFS no
expresa para archivos.

> **Esto reordena el plan.** El handler USVFS deja de ser "el PR de PGPatcher" y pasa a ser
> **infraestructura compartida de toda la capacidad**. Por eso en v2 se adelanta a P5 y se
> diseña genérico.

#### H4 — Los markers son archivos estáticos: la frescura **solo** puede venir del mtime

```bat
xcopy "%ScriptDir%\tools\ParallaxROutput.tmp" "%ParallaxRTEMP%\Output" /v /q /y     (PxR 368)
xcopy "%ScriptDir%\tools\BENDrOutput.tmp"     "%BENDrTEMP%\Output"    /v /q /y      (BENDr 336)
```

El marker se **copia** desde el directorio de la herramienta; su contenido es idéntico corrida a
corrida. Ningún success contract puede derivar frescura del contenido del marker — solo de su
mtime comparado contra una firma tomada antes del lanzamiento. Es la misma lección que el repo
ya pagó con DynDOLOD (`sky_claw/local/AGENTS.md:271`).

#### H5 — La "preservación de `_p.dds`" de ParallaxR es **borrado del output**, no preservación

```powershell
# PxR 339
$src=$env:GameDir+'\textures'; $dst=$env:ParallaxRTEMP+'\Output\textures';
Get-ChildItem -Path $src -Filter '*_p.dds' -Recurse | ForEach-Object {
  $rel = $_.FullName.Substring($src.Length+1); $tgt = Join-Path $dst $rel;
  if (Test-Path $tgt) { Remove-Item -LiteralPath $tgt -Force -ErrorAction SilentlyContinue } }
```

Elimina **del output** todo `_p.dds` que ya exista en `Data\textures`. Preserva por
**no competir**, no por dejar el original intacto. Dos consecuencias:

1. El post-check correcto es *"ningún `_p.dds` del output colisiona con un `_p.dds` loose
   preexistente"*, **no** *"los hashes de los `_p.dds` existentes no cambiaron"* (que es lo que
   v1 proponía testear, y habría pasado siempre).
2. **Solo mira loose files.** Un `_p.dds` que exista únicamente dentro de un BSA no está cubierto
   por esta limpieza. Si eso es intencional o un gap del upstream es **NO VERIFICADO** (R8).

La línea 337 conserva, comentada, la versión anterior que interpolaba `%GameDir%` dentro de
comillas simples de PowerShell (no expande): es un bug ya corregido pasando por `$env:`.

#### H6 — Preflight real y cuantificado, que Sky-Claw tiene que modelar

| Requisito | Evidencia |
|---|---|
| Estimación de espacio: `robocopy .\ c:\dummy /L /E *_n.dds *_p.dds` → bytes × 1.75 | PxR 202-206 · BENDr 209-213 |
| Exige una **raíz de unidad**; crea `<Drive>:\ParallaxR` / `<Drive>:\BENDr` | PxR 229-232 · BENDr 235-238 |
| Advierte explícitamente *"Avoid C: due to Windows Security"* | PxR 208 · BENDr 214 |
| Rechaza correr si existe `<Drive>:\ParallaxR` de una sesión previa (escanea C..Z) | PxR 157-191 · BENDr 165-199 |
| `attrib -R -A -S -H -P *.* /s /d` sobre el output (limpia readonly) | PxR 359 |
| Borra `*.png` y `*.db` del output; elimina dirs vacíos | PxR 363-364, 375 |

#### H7 — Diferencias entre las dos herramientas (no asumir simetría)

| | ParallaxR | BENDr |
|---|---|---|
| Log(s) | `logfiles\ParallaxR.log` | `logfiles\BENDrMain.log` (cabecera) **+** `logfiles\BENDr.log` (etapas) |
| `:Flush` (sleep 3s + cache flush + MinWorkingSet global) | tras **cada** etapa | una sola vez, al final |
| QC | `OutputQC.exe` | **no tiene** |
| Preservación de `_p.dds` | sí (H5) | **no** |
| `:Terminate` mata | Powershell, texconv, **Convert.exe** | Powershell, texconv |
| Lectura de `%TEMP%\*.tmp` | `in ("%TEMP%\...")` — con comillas | `in (%TEMP%\...)` — **sin comillas** (BENDr 130, 233, 295): se rompe si `TEMP` tiene espacios |

Esa última fila es evidencia adicional a favor de orquestar los helpers directamente: el driver
BAT tiene fragilidades de path que Sky-Claw no tiene por qué heredar.

#### H8 — Toolset compartido

`MakeUnpack.exe`, `ExtractBSA.exe`, `LooseCopy.exe`, `Exclusions.exe` y `texconv.exe` aparecen
con invocaciones idénticas en ambos BAT. Solo difieren `*Filter.exe` y el procesador final. Un
adapter para PR-A/PR-B debería modelar una **etapa de "layered extract" compartida** en vez de
duplicar cuatro invocaciones.

### 2.3 Herramienta externa vía web (confianza MEDIA)

| Hecho | Fuente | Confianza |
|---|---|---|
| PGPatcher (aka ParallaxGen) es GPL-3.0, repo `hakasapl/PGPatcher` con GitHub Releases | página del repo | Alta |
| PGPatcher expone `-o` para el directorio de salida | snippet de CHANGELOG | Media |
| PGPatcher **exige correr bajo la DLL de USVFS**; existe `--ignore-mo2vfscheck` | snippet de CHANGELOG | Media |
| PGPatcher tiene GUI; "UX is a primary focus" | README | Media |
| PGPatcher: *"You no longer should install prepatched meshes nor auto parallax"* | README | Media |

### 2.4 Transcripción del brief, aún sin artefacto (confianza MEDIA-BAJA)

VRAMr v16.0310: workflow (GPU selection → preset → PBR detection → LayerPrep [MO2] → BSA
indexing → Loose layering → Exclusions → Filter → Extract → Optimise → marker → DragNDrop),
presets (High Quality / Quality / Optimum / Performance / Vanilla / Custom), y que se niega a
correr con su propio output activo. Coherente con el patrón verificado de ParallaxR/BENDr —
mismo estilo de guard, mismo `DragNDropThisFolderIntoModManager` — pero **el paquete de VRAMr
no se leyó en esta sesión**. Cierra en P0.

Ídem los contratos de output ON/OFF de PGPatcher y la existencia/rol de `pgtools.exe`.

---

## 3. No verificado

### 3.1 La distinción que v1 confundía: existencia técnica ≠ permiso de invocación

Tres ejes **ortogonales**. Confundirlos fue el error de v1, que trataba "¿tiene CLI?" y "¿puedo
usarla?" como una sola pregunta y bloqueaba ParallaxR/BENDr por ambas a la vez.

| Eje | Pregunta | Cómo se responde | Estado |
|---|---|---|---|
| **T — Técnico** | ¿Existe una CLI/helper invocable sin GUI? | Inspección del artefacto | **CERRADO** para ParallaxR/BENDr (H1). Abierto para PGPatcher (B4) y VRAMr (B3) |
| **L — Legal** | ¿La licencia/permisos permiten invocar los helpers, redistribuirlos, descargarlos automáticamente? | Leer permisos del autor / licencia | **ABIERTO** para ParallaxR/BENDr/VRAMr (B1, B6-L). **CERRADO** para PGPatcher (GPL-3) |
| **S — Seguridad** | ¿El artefacto hace cosas que Sky-Claw no puede replicar? | Inspección del artefacto | **CERRADO y caracterizado** (H2). *Se evita no replicándolo*, no se hereda |

**Que el eje S esté cerrado NO desbloquea nada por sí solo** — solo elimina un riesgo. Y que el
eje T esté cerrado **no autoriza** a invocar los helpers: eso lo decide L. Un permiso del autor
puede cubrir "usar la herramienta" y no "llamar a sus binarios internos desde otro programa".

### 3.2 Bloqueantes

**Del primer corte (PGPatcher + VRAMr):**

- **B3 (eje T). Entry point y vector CLI real de VRAMr.** El repo declara "scripts PowerShell"
  (`test_contrato_argumentos_cli.py:245`) pero `vramr_service.py` recibe un `vramr_exe`. Presets,
  selección de GPU, output dir, modo no interactivo, procesos hijo que spawnea.
- **B4 (eje T). Superficie CLI real de PGPatcher**: lista completa de argumentos, si existe
  `--autostart`, qué es `pgtools.exe` frente a `PGPatcher.exe`, si la CLI es completa o requiere
  GUI asistida.
- **B5 (eje T). Contrato USVFS de PGPatcher**: si `--ignore-mo2vfscheck` produce un resultado
  *correcto* contra el Data físico, o solo desactiva un guard (lo segundo → no vería los mods
  virtualizados → resultado silenciosamente incompleto).
- **B7 (NUEVO — DAG). `pgpatcher.esp` frente a LOOT / Wrye Bash / Synthesis.** Ver §3.3.

**Diferidos al segundo corte:**

- **B1 (eje L).** Permisos/licencia de ParallaxR, BENDr y VRAMr en Nexus. Para VRAMr solo decide
  AUTO vs MANUAL; `MANUAL_ONLY` es el default conservador y funcionalmente completo.
- **B6-L (eje L).** ¿El autor permite que un tercero invoque `MakeUnpack.exe` / `ExtractBSA.exe`
  / `HeightMap.exe` / `BENDr.exe` directamente, sin pasar por su BAT? **Es lo único que bloquea
  PR-A/PR-B.** (B6-T está cerrado por H1.)

### 3.3 B7 — el blocker de DAG que v1 no vio

PGPatcher **genera un plugin** (`pgpatcher.esp` o equivalente). Eso choca con la posición
pre-LOD del material pipeline:

```
STAGE 5 LOOT          ordena el load order
STAGE 6 Wrye Bash     lee plugins activos → Bashed Patch, 0.esp
STAGE 7 Synthesis     lee plugins activos → Synthesis.esp
        ↓
   pre_lod_materials  ← PGPatcher genera un plugin AQUÍ, después de 5/6/7
        ↓
STAGE 9 TexGen → DynDOLOD   lee la topología COMPLETA
```

Preguntas abiertas, cada una con consecuencia distinta sobre el grafo:

1. **¿LOOT tiene que volver a ordenar tras generarlo?** El repo ya tiene precedente en contra:
   los plugins generados **se anexan al final** y no se re-sortean — `Bashed Patch, 0.esp`
   "bottom of the load order" (`AGENTS.md:208`), DynDOLOD "ABSOLUTE FINAL entry" (`:273`). Si
   `pgpatcher.esp` sigue esa convención, no hace falta re-LOOT y B7 se cierra barato.
2. **¿Wrye Bash o Synthesis necesitan *ver* `pgpatcher.esp`?** Si sí, el material pipeline tiene
   que correr **antes** del stage 6, lo que contradice la posición pre-LOD del brief y obliga a
   rediseñar el grafo.
3. **¿Cuál es su orden relativo** dentro de la cola final, frente a `Bashed Patch, 0.esp`,
   `Synthesis.esp` y `DynDOLOD.esp`?
4. **¿Cuenta contra el límite de 254 masters?** `AGENTS.md` §0 directiva 7 lo trata como
   crítico y prohíbe mockearlo en tests (§5 regla 6). Un plugin extra en un load de ~1000 mods
   no es neutro.

**Por qué es BLOCKER y no IMPORTANT:** si la respuesta a (2) es "sí", la posición del nodo
`mesh_materials` en el DAG cambia, y **P1 congela ese grafo**. Congelarlo mal cuesta rehacer
P1 y todo lo que dependa de él.

### 3.4 Requieren rig real (no se cierran con mocks)

- **R1.** Contrato de éxito de cada herramienta: qué escribe el log, con qué severidad, si
  "0 archivos procesados" es éxito legítimo.
- **R2.** Prioridad de BENDr Output sobre ParallaxR Output. Inferida en el brief. Solo afecta
  a PR-B. **No congelar.**
- **R3.** Orden real de outputs ON/OFF alrededor de PGPatcher y VRAMr.
- **R4.** Si PGPatcher deja artefactos en `Overwrite` de MO2 al correr bajo USVFS.
- **R5.** Rutas con espacios y Unicode (antecedente caro: `AGENTS.md:269`, CRT vs Delphi).
- **R6.** Si VRAMr spawnea compresores que sobreviven al tree-kill
  (`docs/pending_ooda_status.md:257-258`).
- **R7.** Posición relativa de NGIO (stage 8) frente al material pipeline: **no hay evidencia de
  dependencia en ninguna dirección**. No inventarla.
- **R8 (NUEVO).** Si la limpieza de `_p.dds` de ParallaxR (H5) ignora los BSAs a propósito o es
  un gap del upstream. Solo afecta a PR-A.

---

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
| **Auto Parallax** | OUTPUT_MOD | `MANUAL_ONLY` **siempre** — solo se *detecta*, nunca se instala ni se quita automáticamente | — |

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
| `local/tools/output_targets.py` | `pgpatcher_output_target`, `vramr_output_target` (+ `parallaxr_*`, `bendr_*` en el 2º corte), bajo el namespace `Sky-Claw/` |
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
  `pgpatcher.esp` o un zip existan siempre: depende de la configuración (B4). Evidencia de VFS =
  la atestación del broker.
- **VRAMr:** contadores Converted/Failed leídos del log; `Converted: 0, Failed: 0` es ambiguo por
  sí solo y necesita contexto (¿había algo que convertir?); output y marker frescos.
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

### PR P0 — Verificación en rig y consolidación de evidencia (sin código)

**Objetivo.** Cerrar B3/B4/B5/B7 y dejar la evidencia auditable desde el repo.
**Archivos.** `docs/design/specs/2026-XX-pre-lod-material-pipeline-evidence.md` (nuevo).
**NO tocar.** Nada de `sky_claw/`.
**Dependencias.** Ninguna. **Requiere el rig para B3.**

**Trabajo, en orden de costo creciente:**

1. **Consolidar lo ya verificado (§2.2) en el documento.** Los BAT y `ExtractBSA.exe` fueron
   leídos en esta sesión pero **no están en el repo**: un agente posterior no puede re-derivar
   H1–H8. Versionar las transcripciones (o los artefactos, si la licencia lo permite) bajo
   `docs/`, con el mismo criterio de cita que `PROCEDENCIA_DE_FLAGS`.
2. **PGPatcher — sin rig, solo lectura de fuente GPL-3.** Clonar `hakasapl/PGPatcher` y leer el
   parser de argumentos. **B4:** flags completos y semántica; `pgtools.exe` vs `PGPatcher.exe`;
   `--autostart` o equivalente; artefactos de salida y ubicación; formato y ruta del log.
   **B5:** qué hace el check de MO2/USVFS y qué desactiva exactamente `--ignore-mo2vfscheck`.
   **B7:** si el plugin generado es inerte para Wrye Bash/Synthesis, y qué recomienda el upstream
   sobre su posición en el load order.
3. **VRAMr — rig.** Instalar e inspeccionar el paquete: entry point real (`.exe`? `.bat` que
   llama a `powershell.exe`?), vector CLI, presets, selección de GPU, modo no interactivo,
   procesos hijo que spawnea (decide el Job Object), ubicación de log y marker, contadores.
   Confirmar o corregir §2.4.
4. **Rig — una corrida manual de PGPatcher y otra de VRAMr**, registrando log completo, árbol de
   procesos, artefactos con timestamp, y qué queda en `Overwrite`.
5. **Verificación cruzada de H1** (opcional, barato): correr `--help` en cada helper de
   ParallaxR/BENDr. Son clap: la salida es la lista autoritativa. Adelanta PR-A/PR-B sin costo.

**Aceptación.** Cada fila tiene fuente citable (ruta de archivo fuente, o salida de una corrida
con fecha) o queda marcada NO VERIFICADO con el experimento que la cerraría.
**Riesgos.** Si B7(2) resulta "sí, Wrye Bash/Synthesis necesitan ver `pgpatcher.esp`", el nodo
`mesh_materials` se mueve antes del stage 6 y P1 cambia. **Por eso P0 va primero.**
**Rollback.** Borrar el doc. **Complejidad: Media.**

---

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
**Dependencias.** P7 (el contrato). **Tanda 2 además:** P0 (B3).

*Tanda 1 — no depende de B3:*
- (a) `message: str` canónico junto a `error` legacy (hoy incumple `tool_result.py`).
- (b) `pipeline_capability` + `tx_id` en cada registro de fallo, con `tx_id=None` explícito vía el
  centinela `_SIN_TX_ID` de `subprocess_error_extra`.
- (c) Job Object (`assign_kill_on_close_job`/`close_job`) — cierra
  `docs/pending_ooda_status.md:257-258`.
- (d) Cleanup **recursivo**: hoy `_snapshot_existing` solo rastrea el nivel superior.
- (e) `WritePermissionsChecker` sobre el output target — lo mete en la familia que
  `tests/test_output_targets.py` audita por detección.

*Tanda 2 — requiere B3:*
- (f) Success contract de `_contract.py`: artefacto + frescura + log + marker (por mtime, H4).
- (g) Validador de switches administrados fail-closed sobre `args`, calcado de
  `_extra_args_admisibles`, **con ancla AST** de que el conjunto rechazado cubre lo que el
  servicio emite.
- (h) Presets como modelo tipado en vez de `list[str]` opaco.

**Tests.** Presets; valores custom; PBR presente/ausente; archivos parallax/material; conversiones
fallidas; DB/output stale; output viejo → falso verde rechazado; marker sin artifacts; artifacts
sin marker; inyección de switch por payload → rechazada; timeout con nietos → Job Object los mata;
cleanup con subdirectorio preexistente.
**Aceptación.** Ningún camino declara éxito por exit code. `_SERVICIOS_SIN_COBERTURA` pierde la
fila de `vramr_service.py`. `PROCEDENCIA_DE_FLAGS` deja de decir SIN VERIFICAR.
**Riesgos.** Si B3 no cierra, la tanda 2 se bloquea. **No implementar contra un CLI adivinado** —
es el episodio de `AGENTS.md:269`, donde tres tests congelaron el defecto y en el rig ningún
binario pudo arrancar. **Rollback.** Revert; el servicio sigue huérfano.
**Complejidad: Alta.** Divisible en dos PRs por tanda.

---

### PR P9 — PGPatcher adapter

**Objetivo.** Primer adapter, sobre un contrato ya fijado.
**Archivos.** `local/tools/material/pgpatcher.py` (nuevo) · `local/tools/output_targets.py`
(+ `pgpatcher_output_target`) · `pyproject.toml` (`strict`) ·
`tests/test_material_pgpatcher.py` (nuevo) · `tests/test_output_targets.py`.
**NO tocar.** Orquestador, GUI, agente, `vfs_worker.py` (cerrado en P5), `_contract.py` (P7).
**Dependencias.** P0, P5, P6, P7.
**Cambios conceptuales.** Detección, preflight (TexGen/DynDOLOD/VRAMr/propio output OFF), submit
del `VfsJob`, logging, cancelación, timeout, validación de output, output administrado bajo el
namespace `Sky-Claw/`, rollback. **El adapter no spawnea**: submite al broker. Distinguir no-op /
failure / parcial / stale. **No asumir** que `pgpatcher.esp` o un zip existan siempre (B4).
**Ninguna precondición sobre ParallaxR ni BENDr.**
**Tests.** Corrida válida; no-op; output stale; VRAMr output activo → preflight falla; TexGen
output activo → preflight falla; DynDOLOD output activo → preflight falla; output
fresco/vacío/stale/parcial; marker sin artifacts; artifacts sin marker; rollback preserva
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
| BL-1 | **B7 — `pgpatcher.esp` frente a LOOT / Wrye Bash / Synthesis** | Si Wrye Bash o Synthesis necesitan verlo, el nodo `mesh_materials` se mueve antes del stage 6 y **P1 congela el grafo equivocado** | P0 (fuente GPL-3 + docs upstream) |
| BL-2 | B3 — entry point y vector CLI de VRAMr (eje T) | P8 tanda 2 y P10 no se implementan contra un CLI adivinado | P0 (rig) |
| BL-3 | B4 — superficie CLI de PGPatcher; `pgtools.exe` (eje T) | Define si P9 corre headless o queda *assisted* como Stage 9 | P0 (fuente GPL-3) |
| BL-4 | B5 — qué desactiva `--ignore-mo2vfscheck` (eje T) | Si solo apaga un guard, la opción B de §6.4 se descarta y P5 es obligatorio | P0 (fuente GPL-3) |
| BL-5 | Pérdida de datos en `Mo2ModStateTransaction` | Un rollback ciego destruye el cambio del operador | P6: reversión dirigida + cuarentena terminal + tests de supervivencia del cambio ajeno |
| BL-6 | El handler del worker puede quedar fuera del build congelado | Funciona en dev y falla en producción, en silencio | P5: verificar el build de PyInstaller como criterio de aceptación |

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
| IM-10 | **B6-L** — permiso para invocar los helpers de ParallaxR/BENDr | Solo bloquea PR-A/PR-B. B6-T ya está cerrado (H1) |
| IM-11 | B1 — permisos de Nexus | `MANUAL_ONLY` es el default conservador y funcionalmente completo |
| IM-12 | R2 — prioridad BENDr > ParallaxR es inferida | Solo PR-B; irá configurable, no congelada |
| IM-13 | R8 — la limpieza de `_p.dds` ignora BSAs (H5) | Solo PR-A; verificar en rig antes de escribir su post-check |
| IM-14 | Los artefactos de §2.2 no están en el repo | P0 los versiona; sin eso, H1–H8 no son re-derivables por un agente posterior |
| IM-15 | `tools_installer` no escribe en el `OperationJournal` | Fuera de scope; anotar como deuda |

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
| **P8 tanda 1** VRAMr | Cinco defectos verificados del servicio (`message` faltante, sin `pipeline_capability`, sin Job Object, cleanup no recursivo, sin `WritePermissionsChecker`), independientes del CLI |

**GO tras P0** (horas de trabajo, no semanas): P1, P2, P3, P4, P5, P7, P8-tanda-2, P9, P10, P11,
P12, P13.

**Lo que P0 tiene que producir:**

| Ítem | Cómo se cierra | Necesita rig |
|---|---|---|
| **B7** — `pgpatcher.esp` × LOOT/Wrye Bash/Synthesis | Docs upstream + fuente GPL-3; contrastar con la convención del repo (plugins generados van al final, `AGENTS.md:208`, `:273`) | No |
| B4 — CLI de PGPatcher, rol de `pgtools.exe` | Leer el parser en `hakasapl/PGPatcher` | No |
| B5 — qué desactiva `--ignore-mo2vfscheck` | Leer el código del check en la misma fuente | No |
| B3 — entry point y CLI de VRAMr | Instalar el paquete e inspeccionar (PowerShell, legible) | Sí |
| R1 — contratos de éxito | Una corrida manual de cada herramienta, con log y árbol de procesos | Sí |
| Auditabilidad de H1–H8 | Versionar los artefactos/transcripciones bajo `docs/` | No |

**Diferido explícitamente, sin bloquear:**

- **B1** (permisos Nexus) — solo decide AUTO vs MANUAL para VRAMr; `MANUAL_ONLY` es completo.
- **B6-L** (permiso para invocar helpers) — solo PR-A/PR-B. **B6-T está cerrado**: H1 prueba que
  los helpers son CLIs Rust/clap con args conocidos, y H2 prueba que orquestarlos directamente
  **evita** el comportamiento peligroso en vez de heredarlo. La viabilidad técnica de ParallaxR y
  BENDr subió; lo único que falta es el permiso.
- **R2, R8** — solo PR-A/PR-B.

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
