# ADR 0019 — Operación de Synthesis en dos fases bajo MO2 USVFS (Etapa 7)

**Fecha:** 2026-11-20 (actualizado 2026-11-21)
**Estado:** Propuesta (PR-0).
**Base:** `origin/main` `4c2da7ae` (corte inicial de investigación: `c6e1b022`; sincronizado con el cierre de `F1` en `#980` `676f4c29`).
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
   - Para cada patcher habilitado (`GithubPatcherSettings` o `SolutionPatcherSettings`): si existe hit
     íntegro en caché (`cache_hit=True`, fuentes y binario ya compilados y verificados), **omite**
     `git fetch`, `dotnet restore` y `dotnet build` (cero egress de red). En `cache_hit=False`,
     tras la compuerta HITL de egress ejecuta `dotnet restore` y luego
     `dotnet build --no-restore -c Release -p:UseSharedCompilation=false` bajo
     `run_capture(..., require_job=True)` (nunca `dotnet build` con restore implícito sin compuerta).
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

2. **Fase 2 — Parcheo puro (MO2 USVFS vía Broker/Worker, sin red ni compilación iniciadas por Synthesis):**
   - `BrokeredSynthesisRunner` (`sky_claw/local/mo2/brokered_synthesis.py`) despacha
     `tool_id = "synthesis"` al `VfsWorker` bajo fence `supervised`, Job Object y exclusión mutua
     ADR 0017. **Estado de `F1` (cerrado en `main` por `#980` `676f4c29`):** la rama de gracia
     vencida tras perder el bridge ya pasa a cuarentena persistida y levanta
     `VfsBridgeLostTeardownError` (subclase de `VfsTeardownError`, que veta el rollback). Por
     contrato derivado, `BrokeredSynthesisRunner` y `SynthesisPipelineService` deben consultar y
     respetar `exception_forbids_rollback(exc)` ante `VfsJobTimeoutError(terminality_unknown=True)`,
     `VfsTeardownError` y `VfsBridgeLostTeardownError`, impidiendo `_cleanup_synthesis_outputs()` y
     manteniendo la transacción del journal en estado pendiente (nunca `rolled_back`).
   - **Selección explícita de backend y `WIRING_SECURITY_HOLD` (paridad E3-R3 con `sky_claw/local/AGENTS.md` §2.9):**
     el backend se elige exclusivamente mediante una clave de configuración dedicada
     (`synthesis_backend`: `"standalone"` por defecto o `"brokered"`). Queda **prohibido** inferir
     el backend por la presencia en memoria de un `VfsBroker` (`if vfs_broker is not None:`) o
     aplicar fallback silencioso a `"standalone"` si se solicitó `"brokered"` y falta broker,
     `instance_id` o raíces MO2 válidas. La ejecución `"brokered"` queda bloqueada en código por el
     literal de módulo `_CONTENCION_SYNTHESIS_VERIFICADA = False` (`WIRING_SECURITY_HOLD`, no
     configurable por `config.toml`, env var ni parámetro), que solo podrá levantarse en un PR
     dedicado con revisión adversarial tras acreditar en rig Windows (Spikes S4/S5) la contención
     del subárbol completo (`Synthesis.Bethesda.CLI.exe` → `dotnet` → patchers) bajo el Job Object
     del bridge/worker sobre el binario final.
   - `VFS_TOOL_EXECUTABLE_NAMES["synthesis"] = frozenset({"synthesis.bethesda.cli.exe"})`.
   - **Límite de aislamiento de red en Fase 2 (`CliPatcherRun.cs:78-88`):** usar `CliPatcherSettings`
     + `--BlockBuildingWithinMo2` elimina `git`, `dotnet restore` y `dotnet build` iniciados por
     Synthesis (validado empíricamente en Spike S2; solo se admite `dotnet --info` informativo en
     `PrintDotNetInfo.cs`), pero **no** constituye un firewall de sistema operativo contra un
     ejecutable precompilado de terceros que abra sockets por cuenta propia (`CliPatcherRun.cs:78-88`).
     Por ello, todo patcher requiere aprobación HITL explícita de repositorio/commit antes de
     ejecutarse en Fase 1 y cualquier barrera de egress a nivel OS queda fuera del alcance de este ADR.
   - El snapshot de `plugins.txt` debe preservar la posición de corte del grupo (`LoadOrderForRunProvider.cs:25-32`
     recorta los plugins posteriores al ModKey de salida del grupo). La atestación `expected_plugins`
     en el worker USVFS valida todos los plugins de entrada activos en `plugins.txt`, **excluyendo**
     los artefactos de salida propios de los grupos seleccionados del run (`<Grupo>.esp` y sus
     splits `<Grupo>_N.esp`), para no bloquear reruns tras limpiar outputs previos.
   - Invoca `Synthesis.Bethesda.CLI.exe run-pipeline` con:
     - `--OutputDirectory <staging_output_dir>`
     - `--PipelineSettingsPath <phase2_pipeline_settings.json>`
     - `--ProfileIdentifier <profile_id_or_nickname>`
     - `--DataFolderPath <game_data_dir>`
     - `--LoadOrderFilePath <plugins_txt_snapshot>`
     - `--ExtraDataFolder <cwd_run>/Data` (más `cwd = <cwd_run>`)
     - `--PersistenceMode Text --PersistencePath <persistence_work_dir>` (cuando la persistencia de FormIDs está habilitada)
     - `--BlockBuildingWithinMo2` y `--Shortcircuit` (o su representación explícita según Spike CLI 0.36.6)
   - `synthesis_vfs_visibility` adopta `"virtual_usvfs": False` en modo `brokered` (paridad con
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
- **Rollback unificado y preservación en corridas parciales:** la promoción sobre la raíz
  administrada usa `DirectoryRollback` + `rollback_reconciler.py` (`PRODUCTORES_CABLEADOS`,
  `PRODUCTORES_DEL_NOMBRE`) y borrado exclusivo con `rmtree_link_aware` (prohibido
  `shutil.rmtree`). Una corrida parcial que seleccione solo un subconjunto de grupos **debe
  preservar** los outputs existentes en la raíz administrada pertenecientes a los grupos no
  seleccionados en esa corrida (la limpieza de splits obsoletos y el reemplazo exclusivo operan
  por grupo seleccionado, nunca borrando outputs de grupos excluidos).
- **Reconciliación con ADR 0005:** se retira el clonado de `mo2/overwrite` en `execute_synthesis.py`
  para corridas MO2 brokered. Cuando aplica compuerta HITL de promoción, esta opera sobre el diff
  exacto del directorio staging generado (`<Grupo>.esp`, splits `_2.._N`, `.strings` y cambios a
  `plugins.txt`/`modlist.txt`) antes del commit atómico.
- **Persistencia de FormIDs:** `--PersistencePath` es un **directorio** de estado administrado por
  `TextFileSharedFormKeyAllocator.Initialize` (`RunPersistencePreparer.cs:28`), no un archivo único.
  La corrida opera sobre una copia de trabajo `<persistence_work_dir>`; las compuertas previas de
  validación (recibos, atribución, integridad TES4 y verificación de outputs) deben pasar antes de
  promover los outputs y el directorio durable de persistencia del perfil vía move-aside /
  `DirectoryRollback`, quedando la promoción atómica exitosa como compuerta final antes de emitir
  `success = True`. Una corrida fallida en cualquier compuerta previa o en la promoción misma nunca
  corrompe la persistencia previa. Como
  `RunPersistencePreparer` lanza `ArgumentNullException` si `PersistenceMode=Text` se envía sin
  `PersistencePath`, `SynthesisConfig` valida cerrado antes del spawn que `PersistenceMode=Text`
  tenga ruta no vacía.
- **Activación y contigüidad de splits:** ADR 0017 (`mo2_ini_exclusion.py`) protege
  `modorganizer.ini` por handle, no `plugins.txt` ni `modlist.txt`. En **PR-2b** (perfiles no
  quiescentes) se conserva la inmutabilidad byte a byte de `plugins.txt` y `modlist.txt`,
  entregando únicamente el diff de activación/contigüidad en solo-lectura; la **activación
  desatendida transaccional** sobre `modlist.txt`/`plugins.txt` (habilitando
  `+Sky-Claw Synthesis Output`, limpiando splits obsoletos y garantizando `<Grupo>.esp`,
  `<Grupo>_2.esp`, ..., `<Grupo>_N.esp` **adyacentes y ordenados** antes de `DynDOLOD.esm/esp` y
  `Occlusion.esp`) queda asignada a **PR-5**, sujeta a certificación previa de quiescencia y
  refresco de MO2 en el rig.

### 2.4 Entorno mínimo, contención Windows y timeouts

- **Sin herencia ciega de `os.environ`:** siguiendo el estándar de ADR 0018, los subprocesos de
  Fase 1 y Fase 2 reciben únicamente una lista blanca mínima de variables OS + variables de control
  `.NET`/temp (`DOTNET_CLI_USE_MSBUILD_SERVER=0`, `MSBUILDDISABLENODEREUSE=1`,
  `UseSharedCompilation=false`, `DOTNET_NOLOGO=1`, `DOTNET_CLI_TELEMETRY_OPTOUT=1`, `DOTNET_ROOT`,
  `PATH` con remoción incondicional de `Program Files (x86)\dotnet` y sus subdirectorios SDK/tools
  incluso cuando `dotnet_root` no está fijado —y anteponiendo el `dotnet` x64 cuando sí lo está—,
  `TEMP`/`TMP` al workspace de la corrida; más `--disable-build-servers` y
  `-p:UseSharedCompilation=false` en los `dotnet build` de Fase 1), evitando fuga de secretos
  hacia patchers comunitarios. Como doble defensa contra colisiones con `Program Files (x86)\dotnet`,
  la copia temporal de `PipelineSettings.json` V2 fija además `"DotNetPathOverride"` al `dotnet.exe`
  x64 verificado (consumido por `DotNetCommandPathProvider.cs:15` vía `PipelineSettingsProvider.cs:31`).
  Toda deserialización de `PipelineSettings.json` V2 valida identidades polimórficas completas
  `($type, assembly)` restringidas al assembly canónico `Synthesis.Bethesda.Execution` y rechaza
  cualquier `$type` en ubicaciones o subobjetos no polimórficos.
- **`USERNAME` es parte obligatoria de la lista blanca (evidencia de rig, S5 real 2026-11-22):**
  bajo MO2 2.5.2, `usvfs_x64.dll` 0.5.6.1 hace `std::string(getenv("USERNAME"))` sin chequear NULL
  al inicializarse en cada proceso enganchado (`shm_sink::sink_it_`, igual en upstream `master`).
  Sin `USERNAME`, el CLI moría con `0xC0000005` en `usvfs_x64.dll+0xa6783` antes de cargar .NET
  (`cmd.exe` con el mismo entorno cae igual); con ella, R1–R3 pasaron bajo MO2 real, incluida la
  contención CLI → shim → patcher dormido. No es un secreto nuevo (`USERPROFILE` ya lo contiene).
  La propiedad cubre a **todas** las listas blancas de entorno del paquete, no solo a la de
  Synthesis: `tests/test_entorno_curado_usvfs_username.py` las enumera por AST y congela el censo.
- **Patchers con apphost `CETCompat` — decisión del propietario (2026-11-22):** el SDK .NET 9+
  marca por defecto el apphost como CET-compatible (`IMAGE_DLLCHARACTERISTICS_EX_CET_COMPAT`); en
  el rig, un patcher con ese flag cae bajo usvfs 0.5.6.1 (`0xC0000005` en memoria sin módulo) aun con
  entorno completo, y el mismo binario con solo ese bit apagado funciona. El CLI no se ve afectado
  (el `Directory.Build.props` de Synthesis fija `CETCompat=false`, y Mutagen recomienda lo mismo para
  MO2). Se adopta, en este orden:
  1. **Fase 1 compila sin CET y lo verifica.** Todo `dotnet build`/`publish` de patchers que haga
     Sky-Claw pasa `-p:CETCompat=false` y compila **no incremental** (`--no-incremental` o `obj`
     limpio), y después lee el flag del apphost producido: si sigue CET-compatible, la Fase 1 falla
     cerrado. Evidencia de rig: el override de línea de comandos gana aunque el `.csproj` declare
     `<CETCompat>true</CETCompat>`, pero un build **incremental** conserva el apphost viejo con CET
     sin avisar (también le pasó al shim del rig).
  2. **Patchers ya compilados (Fase 2 / `CliPatcherSettings` con `.exe`):** antes de correr bajo
     USVFS se lee el flag. Si es CET-compatible y *framework-dependent*, el shim lo lanza como
     `dotnet.exe <nombre>.dll` —el muxer no tiene el flag— sin modificar el binario ni romper su pin
     de hash. El tipo de despliegue **no** se infiere de que existan `<nombre>.dll` y
     `<nombre>.runtimeconfig.json` (un self-contained también los trae): es framework-dependent solo
     si ese `runtimeconfig.json` declara `runtimeOptions.framework` o `runtimeOptions.frameworks` y
     **no** declara `includedFrameworks` (lo que escribe un self-contained); cualquier otro caso,
     archivo ilegible incluido, cuenta como no framework-dependent. Además, el `.exe` tiene que ser
     un apphost simple (no un bundle single-file) cuyo nombre de app embebido sea exactamente
     `<nombre>.dll`: así un `.dll`/`runtimeconfig` viejos que quedaron junto a un `.exe` single-file
     nuevo no se lanzan en su lugar. Si esa correspondencia no se puede establecer, se rechaza. Evidencia de rig: el mismo
     `MinimalPatcher` con CET (`runtimeconfig` con `framework` `Microsoft.NETCore.App`) cae lanzado por
     su `.exe` y funciona lanzado por `dotnet.exe`. Si es *self-contained* o *single-file* con CET, se
     rechaza con un mensaje claro (fail-closed).
  3. **Descartado:** parchear el byte del PE de un tercero (altera el binario y rompe el pin) y
     exigir una MO2/usvfs más nueva (no verificado que lo resuelva, y obligaría a todos los usuarios
     a actualizar su MO2).

  No requiere software nuevo para CET: el .NET 10 SDK x64 ya es requisito de la Fase 1 y trae el
  muxer `dotnet.exe`. Sí queda explícito un requisito que ya existía: un patcher framework-dependent
  corre sobre el runtime principal para el que se compiló (.NET no hace roll-forward entre versiones
  principales por defecto), así que hacen falta también los runtimes .NET x64 de los patchers
  elegidos —p. ej. .NET 8 para un patcher `net8.0`—; ambos quedan en `docs/user/installation.md`. Costo aceptado: los procesos de
  patcher pierden el endurecimiento CET (en Windows 10 la shadow stack de usuario medida está
  apagada igual). Implementación: PR-3 (Fase 1 y shim), con test que enumere los caminos de lanzamiento.
- **Contención OS:** `run_capture(..., require_job=True, max_output_bytes=...)` en Fase 1 y Job
  Object + fence `supervised` con constatación de `ActiveProcesses == 0` en Fase 2.
- **Flags protegidos ampliados:** `protected_flags`/`protected_short` bloquean `-o`, `-s`, `-p`,
  `-d`, `-l`, `-e`, `-r`, `-m`, `-t`, `--blockbuildingwithinmo2`, `--shortcircuit` y sus formas
  largas, incluyendo flags cortos pegados al valor (p. ej. `-svalor`, `-ovalor`, `-rvalor`).
- **Timeouts configurables:** `synthesis_build_timeout_seconds = 600` (Fase 1) y
  `synthesis_run_timeout_seconds = 900` (Fase 2, desde **PR-2b** en el servicio), reemplazando el
  límite rígido previo de `300 s`.

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
| **PR-0** *(este PR `#974`)* | ADR 0019 + actualización de índices documentales (`docs/adr/README.md`, `docs/pending_ooda_status.md`). | `test_worktree_convention_invariant.py`, `test_clean_room_invariant.py`, `test_estado_ooda.py`. |
| **PR-1** *(`#975`)* | Contrato CLI (`0.36.6`), `SplitIfMaxMastersExceeded` explícito, `DotNetPathOverride`, whitelist completa `($type, assembly)` en `Synthesis.Bethesda.Execution` + bloqueo recursivo de `$type`, `protected_flags`, `cwd`/`TEMP`/entorno mínimo anti-MSBuild/Roslyn + remoción incondicional de `Program Files (x86)\dotnet`, `require_job=True`, recibos `ConsoleReporter` compatibles con upstream (`Nickname` vacío o con `]`, multiplicidad vía `Counter`) activos en el servicio standalone, validación TES4 vía `read_plugin_header()`, `--PersistenceMode Text`, y promoción de `synthesis_runner`/`patcher_pipeline` a `mypy` estricto. | `test_contrato_argumentos_cli.py`, `test_contrato_veredicto_de_exito.py`, `test_tes4_parser_invariant.py`. |
| **PR-2a** *(`#976`, Draft / Hold de rig)* | Transporte IPC `tool_id="synthesis"` en broker/worker USVFS (`BrokeredSynthesisRunner`, `vfs_worker.py`), exclusión de outputs propios en `expected_plugins`, respeto estricto de `rollback_veto` (`VfsJobTimeoutError`, `VfsTeardownError`, `VfsBridgeLostTeardownError`), validación `PathValidator` y chequeo de enlaces sin pre-resolución, selector explícito `synthesis_backend` + `WIRING_SECURITY_HOLD` (`_CONTENCION_SYNTHESIS_VERIFICADA = False`), y promoción de `brokered_synthesis` a `mypy` estricto. | `test_vfs_visibility_wiring.py`, `test_vfs_fence_de_supervision.py`, `test_vfs_worker.py`, `test_vfs_job_identity_broker.py`, `test_path_resolution_service.py`. |
| **PR-2b** *(gate previo: Spikes S1–S5)* | Salida administrada `<MO2>/mods/Sky-Claw Synthesis Output` en `output_targets.py`, `DirectoryRollback` + `rollback_reconciler.py`, retiro del clonado de `overwrite` en `execute_synthesis.py`, timeouts configurables (`600s`/`900s`), `pipeline_stage=7`, cableado productivo en `orchestration_composition.py` y `dispatcher_dependencies.py`, y diff en solo-lectura de activación/contigüidad (sin mutar `plugins.txt`). | `test_dyndolod_service.py` (`_SERVICIOS_SIN_COBERTURA`, `_RUNNER_SIN_COBERTURA`), `test_output_targets.py`, `test_rollback_salida.py`, `test_rollback_reconciler.py`, `test_borrado_recursivo.py`, `test_orphan_evidence_producer_contract.py`. |
| **PR-3** *(gate previo: Spikes S1–S4)* | Motor Fase 1 (`synthesis_prebuild.py`: detector `.NET 10` x64, caché, compilación, shim, traductor V2→`CliPatcherSettings`, staging `ExtraDataFolder`, HITL egress, `SynthesisPreset`) + `ToolsInstaller.install_synthesis_cli`. | `test_tools_installer_lock_scoping.py`, `test_hitl_client_scoping.py`, `test_borrado_recursivo.py`. |
| **PR-4** | Paridad de superficies: `config.py`, `app_context.py`, `scanner.py`/`registry.py`, Agente LLM (`run_synthesis` en segundo plano + `setup_tools`), GUI (`RITUAL_TOOL_MAP`/`RITUAL_INSTALLER_MAP`), nuevo test enumerativo de paridad. | `test_ritual_dispatch.py`, `test_hitl_client_scoping.py`, `test_local_config_persistencia.py`, `test_path_resolution_service.py`. |
| **PR-5** *(gate previo: certificación de quiescencia/refresco de MO2 en rig)* | Activación desatendida transaccional de `+Sky-Claw Synthesis Output` y contigüidad de splits en `modlist.txt`/`plugins.txt`. *(El levantamiento de `_CONTENCION_SYNTHESIS_VERIFICADA` va en un PR dedicado separado con evidencia de sandbox Windows y auditoría adversarial, siguiendo E3-R3).* | `test_mo2_modlist_priority.py`. |

## 4. Estado de Implementación y Verificación

- **Implementado en este PR (PR-0):** únicamente especificación ADR 0019 e índices documentales.
  Ningún módulo productivo de `sky_claw/` fue modificado aún (`PR-1..PR-5 = PENDING`; sincronizado con
  `origin/main` `4c2da7ae`, donde `#980` `676f4c29` ya cerró `F1`).
- **Verificado en código local y upstream:** toda la sección §1 (inspección directa de `Sky-Claw`
  en `c6e1b022`/`4c2da7ae` y del repositorio `Mutagen-Modding/Synthesis` en `0.36.6` `d8150fb` y
  `HEAD 14cf3fa`, más metadata de GitHub Releases y NuGet).
- **PR-3a (primer corte de PR-3, decisión CET de §2.4):** `sky_claw/local/tools/synthesis_prebuild.py`
  (estricto en `mypy`) lee el flag CETCompat del PE, clasifica cada patcher ya compilado en
  directo / vía `dotnet.exe <nombre>.dll` / rechazo, fija los argumentos de `dotnet build` de Fase 1
  (`--no-incremental`, `-p:CETCompat=false`, `--disable-build-servers`) y verifica fail-closed el
  apphost producido. Ancla: `tests/test_synthesis_lanzamiento_cet.py` enumera un caso por cada
  `Motivo` (un camino nuevo sin caso rompe el test). Verificado además contra los apphosts reales del
  rig (con y sin CET, un single-file .NET 10, el CLI y un apphost renombrado). **Todavía sin
  consumidor:** el shim, el traductor a `CliPatcherSettings`, la caché, el detector `.NET 10`, la
  compuerta HITL de egress, `SynthesisPreset` e `install_synthesis_cli` quedan para los cortes
  siguientes de PR-3. No se probó un self-contained real (su forma `includedFrameworks` se cubre con
  `runtimeconfig.json` sintético).
- **PR-3b (shim de producción):** fuentes C# en `sky_claw/local/tools/synthesis_shim_fuentes/`
  (`CETCompat=false` declarado, sin `PackageReference`), que viajan en el wheel y en el congelado
  (`tests/test_packaging.py` verifica las dos distribuciones juntas). El lado Python,
  `sky_claw/local/tools/synthesis_shim.py` (estricto en `mypy`), fija el árbol completo del patcher
  por hash (mismo algoritmo en Python y C#; rechaza enlaces y nombres fuera del BMP), construye un
  `shim.json` de esquema cerrado decidiendo el lanzamiento con PR-3a (directo / `dotnet.exe exec
  <nombre>.dll`; un patcher rechazado no tiene shim) y materializa una copia del shim por patcher sin
  borrar nada. El shim C# revalida el esquema y el hash antes de lanzar, inyecta `-i`/`-f` cuando hay
  carpetas de datos y devuelve el código del patcher. Anclas: claves y versión de esquema C#↔Python,
  csproj, mensajes ASCII (la consola de Windows no está en UTF-8). **Rig (fuera de USVFS):** el shim
  compilado con los argumentos de Fase 1 queda sin CET; lanza ShimProbe con CET vía `dotnet.exe` y sin
  CET directo; el hash de Python coincide con el de C# en un árbol real de 48 archivos; DLL alterada o
  archivo agregado → 6 sin lanzar; claves extra/faltantes o DLL ajena → 3. **Hallazgo para el build de
  Fase 1:** con el entorno mínimo, `dotnet restore` falla dentro de NuGet si falta `ProgramFiles` (o
  `ProgramFiles(x86)`); la lista blanca de Fase 1 lo tiene que incluir, y conviene aislar `APPDATA`,
  `LOCALAPPDATA` y `NUGET_PACKAGES`. Falta: correr este shim bajo USVFS/MO2 real, el traductor a
  `CliPatcherSettings` (PR-3c) y el build orquestado de la Fase 1.
- **PR-3c (traductor V2 → `CliPatcherSettings`):** `prepare_synthesis_cli_settings` acepta
  `cli_executables` (ID canónico → shim de PR-3b) y reemplaza cada patcher seleccionado —Git,
  Solution o CLI— por un `CliPatcherSettings` de forma cerrada (`CLAVES_CLI_PATCHER_TRADUCIDO`:
  `$type`, `On`, `Nickname`, `PathToExecutable`), conservando el `Nickname` de ejecución del que
  dependen los recibos. El mapa debe cubrir exactamente la selección (faltante, sobrante, clave
  duplicada por mayúsculas o ruta relativa → fail-closed). Vive en el único productor del settings de
  la corrida: standalone y brokered (vía `vfs_worker` → `SynthesisRunner`) pasan por ahí. Ancla:
  `tests/test_synthesis_traductor_cli.py`. **Límite:** `SynthesisRunner` todavía no le pasa el mapa;
  se cablea junto con la caché y el build orquestado de la Fase 1.
- **PR-3d (build de Fase 1 para patchers locales):** `sky_claw/local/tools/synthesis_compilacion.py`
  (estricto en `mypy`) compila un `SolutionPatcherSettings` sobre una COPIA de la solución en la caché
  (las fuentes del usuario no se tocan), con caché por hash del árbol de fuentes: un acierto revalida
  la huella de la salida y el apphost sin CET, sin red ni HITL; una salida alterada falla cerrado sin
  recompilar encima. Un fallo de caché pide primero la compuerta HITL de egress (`api.nuget.org`) y
  recién después lanza `dotnet restore` y `dotnet build` (argumentos de PR-3a con `-o`) bajo
  `run_capture(require_job=True, max_output_bytes)`. El entorno es el mismo que el del CLI:
  `construir_entorno_dotnet` se extrajo de `SynthesisRunner` (una sola lista blanca, que ya incluye
  `PROGRAMFILES`), más `NUGET_PACKAGES` y `TEMP` dentro de la caché. Publicación atómica con
  `os.replace`, sin borrar. **Rig (fuera de USVFS):** MinimalPatcher real compilado en 9 s con un
  pedido HITL; el segundo llamado es acierto de caché en 0,1 s; la cadena 3a→3b→3c sobre ese build
  funciona (decisión directa, shim que reenvía igual que el patcher, `CliPatcherSettings` traducido);
  una DLL alterada en la caché se detecta. Una solución con `ProjectReference` fuera de su carpeta
  falla cerrado en el build. Falta: patchers Git, el orquestador que arma el mapa de shims y lo pasa a
  `SynthesisRunner`, y la corrida bajo USVFS/MO2 real.
- **No verificado aún (pendiente de rig real):** Spikes S1–S5 (requieren `.NET 10 SDK` x64 en el
  rig Windows + instancia portable aislada de MO2) y verificación E2E de contención del árbol `.NET`
  para levantar `_CONTENCION_SYNTHESIS_VERIFICADA`.
