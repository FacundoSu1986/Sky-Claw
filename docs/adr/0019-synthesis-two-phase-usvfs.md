# ADR 0019 — Operación de Synthesis en dos fases bajo MO2 USVFS (Etapa 7)

**Fecha:** 2026-11-20
**Estado:** Propuesta (PR-0).
**Base:** `origin/main` `c6e1b022`.
**Rama:** `docs/adr-0019-synthesis-two-phase-usvfs`

## 1. Contexto

La etapa 7 del pipeline de modding (Mutagen **Synthesis**) cuenta hoy con cimientos parciales en
`sky_claw/local/tools/synthesis_runner.py`, `synthesis_service.py` y `patcher_pipeline.py`
(snapshot de `plugins.txt`, copia V2 con `UpdateLoadOrderAfterRun=False`, huella SHA-256 de
entradas y soporte multi-grupo/splits `_2.._N`), pero **no puede operar en una instalación real de
Mod Organizer 2 (USVFS)** ni es accesible desde las superficies de usuario:

1. **Bloqueado en MO2 real:** `ALLOWED_VFS_TOOL_IDS` (`sky_claw/local/mo2/vfs_contracts.py:24`) no
   incluye `"synthesis"`, `SynthesisRunner` ejecuta `run_capture()` directo sin broker USVFS, y el
   preflight `synthesis_vfs_visibility` exige que el 100% de los plugins activos existan en
   `<game>/Data` físico — lo que bloquea en rojo toda instancia real de MO2.
2. **Paridad de superficies rota (1 de 3):** solo existe `SupervisorAgent`
   (`execute_synthesis_pipeline`). No existe tool en `AsyncToolRegistry` (Agente LLM), ni tarjeta
   en `RITUAL_TOOL_MAP` (GUI `ritual_runner.py`), ni registro en `EXTERNAL_TOOL_REGISTRY` /
   `scanner.py` / `ToolsInstaller`, ni campos en `config.py`.
3. **Empaquetado y contrato real de upstream (`Mutagen-Modding/Synthesis`, tag `0.36.6` `d8150fb` y
   `HEAD 14cf3fa`):**
   - El release de GitHub (`Synthesis.zip`) publica **únicamente `Synthesis.Bethesda.GUI`**
     (`Synthesis.exe`, WPF `WinExe`, sin verbo `run-pipeline`). El paquete NuGet
     `Synthesis.Bethesda.CLI 0.36.6` carece de `<PackAsTool>true</PackAsTool>`, por lo que tampoco
     es instalable vía `dotnet tool install`.
   - `Directory.Build.props` exige **`.NET 10 SDK` x64** (`DotNetCurrent=net10.0`,
     `Platforms=x64`, `CETCompat=false`).
   - En `Synthesis.Bethesda.CLI`, `--ExtraDataFolder` está declarado en
     `RunPatcherPipelineCommand.cs` pero **nunca se inyecta** en `RunPipelineCliModule.cs` (el CLI
     siempre lee `<CWD>\Data\<ProfileNickname>\<PatcherNickname>\settings.json`).
   - `WorkingDirectoryProvider.cs` ignora `PipelineSettings.WorkingDirectory` y usa siempre
     `%TEMP%\Synthesis`.
   - `RunPatcherPipeline.cs` ignora `profile.FormIdPersistence` en JSON y lee únicamente `-m` / `-r`.
   - `SplitIfMaxMastersExceeded` cambió de default `true` (`0.36.6`) a `false` (`HEAD 14cf3fa`).
   - `PrepareGitPatcherForCli.cs` ejecuta `git` y `QueryNewestLibraryVersions` (`dotnet restore`
     contra NuGet) **antes** de evaluar `ShouldShortCircuitCompilation`, y `SolutionPatcherPrep`
     siempre invoca `dotnet build`: por lo tanto `--Shortcircuit --BlockBuildingWithinMo2` **no**
     elimina compilación ni red dentro de USVFS para patchers `Git` ni `Solution`.
   - `BuildStartInfoProvider.cs` invoca `dotnet build` sin desactivar servidores persistentes de
     MSBuild ni `VBCSCompiler.exe`.
   - **Trampas de `exit_code == 0`:** `RunAGroup.cs:70` → `RunAllGroups.cs:40` →
     `RunPipelineLogic.cs:37`, `FinalizePatcherRun.cs:51` y `ExecuteRun.cs:63` permiten terminar
     con código `0` habiendo abortado grupos o sin emitir ningún `.esp`.
4. **Conflicto con `SandboxPromotionFlow`:** `execute_synthesis.py` clona todo `mo2/overwrite` +
   perfil antes de correr, y `output_targets.py` cae por defecto en `mo2/overwrite` (carpeta
   compartida mutable) usando rollback `"snapshot"` en lugar de raíz administrada +
   `DirectoryRollback` + `rollback_reconciler.py`.

Este ADR congela la arquitectura objetivo y el contrato de implementación incremental para cerrar
la etapa 7 de forma determinista, fail-closed y desatendida.

## 2. Decisión

### 2.1 Desacoplamiento obligatorio en dos fases (T2)

Synthesis opera dividido en dos fases con responsabilidades y permisos disjuntos:

1. **Fase 1 — Preparación y pre-compilación (Host, FUERA de USVFS):**
   - Vive bajo `SystemPaths.runtime_state_dir() / "synthesis_cache"`.
   - Valida `git` y `.NET 10 SDK` x64 explícitamente desde la ruta x64 (`C:\Program Files\dotnet`),
     detectando y aislando colisiones con `Program Files (x86)\dotnet`.
   - Para cada patcher habilitado (`GithubPatcherSettings` o `SolutionPatcherSettings`): resuelve
     repositorio/commit en caché, ejecuta `dotnet restore` y
     `dotnet build -c Release -p:UseSharedCompilation=false` bajo `run_capture(..., require_job=True)`.
   - Genera un `PipelineSettings.phase2.json` inmutable y recién serializado (validando una lista
     blanca cerrada de `$type` al importar, dada la deserialización `TypeNameHandling.Auto` de
     Newtonsoft.Json en `Constants.cs`), conteniendo **exclusivamente** los patchers habilitados y
     convertidos a **`CliPatcherSettings`** (omitiendo todo patcher `On=false`, porque
     `PrepForRun.cs:24` recorre también patchers `Git`/`Solution` deshabilitados), con
     `UpdateLoadOrderAfterRun = false` y `SplitIfMaxMastersExceeded` explícito.
   - Para patchers que consuman recursos internos (`DefaultDataFolderPath` / `InternalDataFolder`,
     que `ConstructSolutionPatcherRunArgs` pasa pero `ConstructBaseRunArgs` de `CliPatcherRun`
     omite), Fase 1 genera un **shim ligero** que reenvía argumentos e inyecta ambos flags apuntando
     a los recursos en caché (validación empírica obligatoria en Spike S2).
   - Materializa las configuraciones por patcher en `<cwd_run>/Data/<ProfileNickname>/<PatcherNickname>/<archivo>`
     en solo-lectura respecto del origen del usuario (sortea el bug upstream de `--ExtraDataFolder`;
     un patcher puede pedir un nombre distinto de `settings.json` vía `ReflectionSettingsTarget`).
   - **Política Zero-Trust de egress (T4):** si la caché satisface todos los patchers
     (`cache_hit=True`), Fase 1 no abre red. Si hay cache miss que requiere `git fetch` o
     `dotnet restore` (subprocesos fuera de `NetworkGateway`), se exige compuerta HITL explícita
     enumerando repositorios, commits y hosts (`github.com`, `api.nuget.org`) antes de lanzar red.

2. **Fase 2 — Parcheo puro (MO2 USVFS vía Broker/Worker, 100% offline):**
   - `BrokeredSynthesisRunner` (`sky_claw/local/mo2/brokered_synthesis.py`) despacha
     `tool_id = "synthesis"` al `VfsWorker` bajo fence `supervised`, Job Object y exclusión mutua
     ADR 0017. **Prerrequisito de contención (F1):** antes de habilitar rollback automático tras
     pérdida de conexión del bridge, debe cerrarse o auditarse la rama de gracia vencida en
     `vfs_broker.py:1295-1319` para que una terminalidad desconocida nunca habilite rollback y pase
     a cuarentena durable.
   - `VFS_TOOL_EXECUTABLE_NAMES["synthesis"] = frozenset({"synthesis.bethesda.cli.exe"})`.
   - En Fase 2 se admite `dotnet --info` como hijo informativo esperado (`PrintDotNetInfo.cs`), pero
     **cero** `git`, `dotnet restore`, `dotnet build` ni egress de red.
   - El snapshot de `plugins.txt` debe preservar la posición de corte del grupo (`LoadOrderForRunProvider.cs:25-32`
     recorta los plugins posteriores al ModKey de salida del grupo).
   - Invoca `Synthesis.Bethesda.CLI.exe run-pipeline` con:
     - `--OutputDirectory <staging_output_dir>`
     - `--PipelineSettingsPath <phase2_pipeline_settings.json>`
     - `--ProfileIdentifier <profile_id_or_nickname>`
     - `--DataFolderPath <game_data_dir>`
     - `--LoadOrderFilePath <plugins_txt_snapshot>`
     - `--ExtraDataFolder <cwd_run>/Data` (más `cwd = <cwd_run>`)
     - `--PersistenceMode Text --PersistencePath <persistence_work_dir>`
     - `--BlockBuildingWithinMo2` y `--Shortcircuit`
   - `synthesis_vfs_visibility` adopta `"virtual_usvfs": False` en modo `mo2_broker` (paridad con
     LOOT y DynDOLOD), manteniendo fail-closed en modo `standalone` si faltan plugins en físico.

### 2.2 Provisioning y verificación del binario CLI (T1)

- `ToolsInstaller` soporta estrategia híbrida:
  1. Usar un `Synthesis.Bethesda.CLI.exe` existente/descubierto tras verificar que es PE CUI
     (`IMAGE_SUBSYSTEM_WINDOWS_CUI`, rechazando `Synthesis.exe` WPF GUI con error
     `SYNTHESIS_CLI_MISSING`).
  2. Auto-compilar `Synthesis.Bethesda.CLI` de forma reproducible desde el tag upstream pineado
     `0.36.6` (commit `d8150fb`) vía `dotnet publish -c Release --no-self-contained` bajo compuerta
     HITL de egress y lock cross-process de `ToolsInstaller`.
  3. Aceptar un bundle precompilado verificado por SHA-256 si el mantenedor configura mirror.

### 2.3 Salida administrada, Rollback, Persistencia de FormIDs y Activación (T3)

- **Destino exclusivo:** en instancias MO2, la salida administrada pasa de `mo2/overwrite` a
  `<MO2>/mods/Sky-Claw Synthesis Output` (en standalone: `<game>/Sky-Claw/Synthesis`).
- **Rollback unificado:** la promoción sobre la raíz administrada usa `DirectoryRollback` +
  `rollback_reconciler.py` (`PRODUCTORES_CABLEADOS`, `PRODUCTORES_DEL_NOMBRE`) y borrado exclusivo
  con `rmtree_link_aware` (prohibido `shutil.rmtree`).
- **Reconciliación con ADR 0005:** se retira el clonado de `mo2/overwrite` en `execute_synthesis.py`
  para corridas MO2 brokered. Cuando aplica compuerta HITL de promoción, esta opera sobre el diff
  exacto del directorio staging generado (`<Grupo>.esp`, splits `_2.._N`, `.strings` y cambios a
  `plugins.txt`/`modlist.txt`) antes del commit atómico.
- **Persistencia de FormIDs:** `--PersistencePath` es un **directorio** de estado administrado por
  `TextFileSharedFormKeyAllocator.Initialize` (`RunPersistencePreparer.cs:28`), no un archivo único.
  La corrida opera sobre una copia de trabajo `<persistence_work_dir>`; el directorio durable de
  persistencia del perfil solo se promueve vía move-aside / `DirectoryRollback` después de pasar las
  7 compuertas del post-check. Una corrida fallida nunca corrompe la persistencia previa. Como
  `RunPersistencePreparer` lanza `ArgumentNullException` si `PersistenceMode=Text` se envía sin
  `PersistencePath`, `SynthesisConfig` valida cerrado antes del spawn que `PersistenceMode=Text`
  tenga ruta no vacía.
- **Activación y contigüidad de splits:** ADR 0017 (`mo2_ini_exclusion.py`) protege
  `modorganizer.ini` por handle, no `plugins.txt` ni `modlist.txt`. En el primer alcance, para
  perfiles no quiescentes se conserva la inmutabilidad byte a byte de `plugins.txt` y se entrega el
  diff de activación; la activación desatendida sobre `modlist.txt`/`plugins.txt` (habilitando
  `+Sky-Claw Synthesis Output`, limpiando splits obsoletos y garantizando `<Grupo>.esp`,
  `<Grupo>_2.esp`, ..., `<Grupo>_N.esp` **adyacentes y ordenados** antes de `DynDOLOD.esm/esp` y
  `Occlusion.esp`) requiere certificación previa de quiescencia/refresco de MO2 en el rig (PR-5).

### 2.4 Entorno mínimo, contención Windows y timeouts

- **Sin herencia ciega de `os.environ`:** siguiendo el estándar de ADR 0018, los subprocesos de
  Fase 1 y Fase 2 reciben únicamente una lista blanca mínima de variables OS + variables de control
  `.NET`/temp (`DOTNET_CLI_USE_MSBUILD_SERVER=0`, `MSBUILDDISABLENODEREUSE=1`,
  `UseSharedCompilation=false`, `DOTNET_NOLOGO=1`, `DOTNET_CLI_TELEMETRY_OPTOUT=1`, `DOTNET_ROOT`,
  `PATH` saneado x64-primero, `TEMP`/`TMP` al workspace de la corrida; más `--disable-build-servers`
  y `-p:UseSharedCompilation=false` en los `dotnet build` de Fase 1), evitando fuga de secretos
  hacia patchers comunitarios. Como doble defensa contra colisiones con `Program Files (x86)\dotnet`,
  la copia temporal de `PipelineSettings.json` V2 fija además `"DotNetPathOverride"` al `dotnet.exe`
  x64 verificado (consumido por `DotNetCommandPathProvider.cs:15` vía `PipelineSettingsProvider.cs:31`).
- **Contención OS:** `run_capture(..., require_job=True, max_output_bytes=...)` en Fase 1 y Job
  Object + fence `supervised` con constatación de `ActiveProcesses == 0` en Fase 2.
- **Flags protegidos ampliados:** `protected_flags`/`protected_short` bloquean `-o`, `-s`, `-p`,
  `-d`, `-l`, `-e`, `-r`, `-m`, `-t`, `--blockbuildingwithinmo2`, `--shortcircuit` y sus formas
  largas, incluyendo flags cortos pegados al valor (p. ej. `-svalor`, `-ovalor`, `-rvalor`).
- **Timeouts configurables:** `synthesis_build_timeout_seconds = 600` (Fase 1) y
  `synthesis_run_timeout_seconds = 900` (Fase 2), reemplazando el límite rígido de `300 s`.

### 2.5 Veredicto fail-closed y reutilización del parser TES4 canónico

Un resultado solo es `success = True` si supera simultáneamente:

1. `exit_code == 0` sin timeout ni ruptura del fence `supervised`, y terminalidad confirmada.
2. Inmutabilidad verificada por SHA-256 del `plugins.txt` y `PipelineSettings.json` originales.
3. Ausencia de marcadores fatales reales de `ConsoleReporter`/`ErrorClassifier` en logs
   (`Overall error:`, `Error detected:`, `Preparation error:`, `Process exited in failure:`,
   `Mo2BuildBlockedException`, `Mo2BuildException`, `NonAdjacentSplitModsException`,
   `TooManyMastersException`, `MissingModException`, `error MSB`, `error CS`, `Run failed`) y
   presencia de confirmación de exportación (`Exported patch to final destination:`).
4. **Cobertura por patcher y por grupo:** porque `RunSomePatchers.cs:31-48` conserva la salida
   previa si un patcher sale con 0 sin escribir su archivo intermedio (`FinalizePatcherRun` retorna
   `null`), cada patcher habilitado debe registrar `[<nombre>] Starting run.` y
   `[<nombre>] Run successful.`, y cada grupo habilitado debe producir `<Grupo>.esp` (bloqueando
   las trampas de `exit 0` silencioso).
5. **Frescura por staging inicialmente vacío:** cada artefacto fue creado por esta corrida dentro de
   un directorio staging que nació vacío, sin enlaces ni archivos inesperados. Una salida byte a
   byte idéntica a una corrida previa (mismo SHA-256) es **válida** si un patcher determinista la
   produjo en el staging vacío con recibos completos de esta corrida.
6. **Validación binaria TES4 mediante el parser canónico único:** toda lectura binaria TES4 delega
   en `read_plugin_header()` de `sky_claw/local/validators/plugin_header.py` (respetando el ancla
   `tests/test_tes4_parser_invariant.py` — **prohibido** introducir un segundo parser TES4). Si se
   necesita el conteo de registros `HEDR` o límites estructurales del cuerpo, se extiende
   `PluginHeader` dentro de `plugin_header.py`. Se exige cabecera TES4 válida,
   `len(header.masters) <= 254` y masters presentes en el snapshot del load order.
7. Promoción atómica exitosa de salida, directorio de persistencia y (si aplica en perfil
   administrado certificado) `plugins.txt`/`modlist.txt`, comparando identidad `(ModKey, FormID local)`
   de registros propios cuando existan saves previos.

### 2.6 Paridad de las tres superficies y límite del router LLM (T5)

- **Las tres superficies** (`SupervisorAgent`/dispatcher, GUI `ritual_runner.py` en
  `RITUAL_TOOL_MAP`/`RITUAL_INSTALLER_MAP`, y Agente LLM en `AsyncToolRegistry`: `run_synthesis` +
  `setup_tools(tool="synthesis")`) delegan en el mismo `SynthesisPipelineService`, con el mismo
  lock, el mismo backend (`mo2_broker`/`standalone`), la misma compuerta HITL de egress y
  observabilidad `pipeline_stage=7`.
- **Plazo del router LLM (`DEFAULT_TOOL_ROUND_TIMEOUT = 120 s`):** igual que `generate_lods` y
  `admitir_plugin_nativo` (ADR 0018 §2.1), `run_synthesis` en el Agente LLM corre como **trabajo en
  segundo plano**, devuelve `trabajo_id` de inmediato, envía aprobaciones HITL y aviso final vía
  `notify_operator`, y `AppContext` registra su `cerrar()` para cancelar el árbol de procesos al
  apagar.
- **Modo dual de configuración (T5):** se soportan tanto `PipelineSettings.json` + carpeta `Data/`
  existentes del usuario (en solo-lectura) como perfiles declarativos `SynthesisPreset` sin GUI
  (entregados en PR-3), con pinning por commit SHA para reproducibilidad.

## 3. Plan de Implementación Incremental y Spikes Obligatorios

### 3.1 Spikes empíricos de cierre (gate previo a PR-2 y PR-3)

Antes de aterrizar PR-2/PR-3 se requieren evidencias medidas en rig Windows + MO2 (con `.NET 10 SDK`
x64 instalado):

- **S1:** `dotnet publish` local de `Synthesis.Bethesda.CLI` `0.36.6` (`d8150fb`) y verificación de
  PE CUI + `run-pipeline --help`.
- **S2:** Validación del shim Fase 1 (`DefaultDataFolderPath`/`InternalDataFolder`) + `cwd/Data/...`
  bajo `CliPatcherSettings` + `--BlockBuildingWithinMo2` confirmando 0 red/compilación en Fase 2 y
  salida equivalente a una corrida estándar.
- **S3:** Auto-split (>254 masters) + `--PersistenceMode Text` + estabilidad de FormIDs entre dos
  corridas consecutivas y verde de `etapas_previas_lods.py`.
- **S4:** 5/5 ciclos build + cancelación mid-build con 0 procesos `dotnet`/`MSBuild`/`VBCSCompiler`
  huérfanos y 0 `WinError 32` al borrar con `rmtree_link_aware`.
- **S5:** Smoke real de `VfsWorker` (`tool_id="synthesis"`) bajo MO2 USVFS + bloqueo ADR 0017 con
  MO2 GUI abierto.

### 3.2 Secuencia de PRs (TDD, 1 rama + 1 worktree por PR)

| PR | Alcance | Anclas AST que actualiza (sin relajar) |
|---|---|---|
| **PR-0** *(este PR)* | ADR 0019 + actualización de índices documentales (`docs/adr/README.md`, `docs/pending_ooda_status.md`). | `test_worktree_convention_invariant.py`, `test_clean_room_invariant.py`. |
| **PR-1** | Contrato CLI (`0.36.6`), `SplitIfMaxMastersExceeded` explícito, `protected_flags`, `cwd`/`TEMP`/env mínimo anti-MSBuild/Roslyn, `require_job=True`, regex reales `ConsoleReporter`, validación TES4 vía `read_plugin_header()`, `--PersistenceMode Text`, timeouts configurables y `pipeline_stage=7`. | `test_contrato_argumentos_cli.py`, `test_contrato_veredicto_de_exito.py`, `test_tes4_parser_invariant.py`, `test_dyndolod_service.py` (`_SERVICIOS_SIN_COBERTURA`, `_RUNNER_SIN_COBERTURA`). |
| **PR-2** | `tool_id="synthesis"` en broker/worker USVFS (`BrokeredSynthesisRunner`), salida administrada `<MO2>/mods/Sky-Claw Synthesis Output`, `DirectoryRollback` + `rollback_reconciler.py`, activación/contigüidad de splits en `plugins.txt`, retiro del clonado de `overwrite` en `execute_synthesis.py`. | `test_vfs_visibility_wiring.py`, `test_vfs_fence_de_supervision.py`, `test_vfs_worker.py`, `test_vfs_job_identity_broker.py`, `test_output_targets.py`, `test_rollback_salida.py`, `test_rollback_reconciler.py`, `test_borrado_recursivo.py`, `test_mo2_modlist_priority.py`, `test_orphan_evidence_producer_contract.py`. |
| **PR-3** | Motor Fase 1 (`synthesis_prebuild.py`: detector `.NET 10` x64, caché, compilación, shim, traductor V2→`CliPatcherSettings`, staging `ExtraDataFolder`, HITL egress, `SynthesisPreset`) + `ToolsInstaller.install_synthesis_cli`. | `test_tools_installer_lock_scoping.py`, `test_hitl_client_scoping.py`, `test_borrado_recursivo.py`. |
| **PR-4** | Paridad de superficies: `config.py`, `app_context.py`, `scanner.py`/`registry.py`, Agente LLM (`run_synthesis` en segundo plano + `setup_tools`), GUI (`RITUAL_TOOL_MAP`/`RITUAL_INSTALLER_MAP`), nuevo test enumerativo de paridad. | `test_ritual_dispatch.py`, `test_hitl_client_scoping.py`, `test_local_config_persistencia.py`, `test_path_resolution_service.py`. |

## 4. Estado de Implementación y Verificación

- **Implementado en este PR (PR-0):** únicamente especificación ADR 0019 e índices documentales.
  Ningún módulo productivo de `sky_claw/` fue modificado aún (`PR1..PR4 = PENDING`).
- **Verificado en código local y upstream:** toda la sección §1 (inspección directa de `Sky-Claw`
  en `c6e1b022` y del repositorio `Mutagen-Modding/Synthesis` en `0.36.6` `d8150fb` y `HEAD
  14cf3fa`, más metadata de GitHub Releases y NuGet).
- **No verificado aún (pendiente de rig real):** Spikes S1–S5 (requieren `.NET 10 SDK` x64 en el
  rig Windows + instancia portable aislada de MO2).
