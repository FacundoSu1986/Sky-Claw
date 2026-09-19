# Pre-LOD Material Pipeline — P0: evidencia, contratos externos y especificación

> **Modo:** investigación y especificación. Cero código productivo, cero cambios de runtime.
> **Fecha de sesión:** 2026-09-19 (UTC).
> **Documento hermano:** `docs/design/plans/2026-08-19-pre-lod-material-pipeline-v3.md`
> (recuperado en esta misma rama desde `origin/claude/pre-lod-material-pipeline-2ocqri`).
> La v3 sigue siendo el diseño vigente; este P0 lo alimenta y **no lo reemplaza**.
>
> **Este documento no debe usarse como referencia runtime.** Es evidencia fechada con
> estados epistémicos por fila. Antes de actuar sobre cualquier fila: refrescar
> `origin/main`, localizar el caller productivo y contrastar con tests y ADR.

## Ubicación de este documento (decisión de taxonomía)

`docs/design/research/` **no existía** en `origin/main` al abrir esta sesión. La taxonomía
vigente es:

- `docs/design/specs/` y `docs/design/plans/` — diseño acordado (`docs/design/README.md`:
  "Fuente canónica del diseño acordado").
- `docs/audits/` — "evidencia histórica fechada" de auditorías sobre el propio código.
- `docs/validation/` — paquetes de validación en rig real.

Este P0 es investigación de herramientas externas que alimentará una futura v4 del plan.
No es un audit del código (no encaja en `audits/`), no ejecutó nada en un rig (no encaja
en `validation/`) y todavía no es diseño acordado (no corresponde a `specs/`). Se crea
`docs/design/research/` adyacente al plan que alimenta, y la decisión queda registrada
acá. Si el repo prefiere otra ubicación canónica, mover este archivo es un rename barato.

---

## 1. Baseline

| Ítem | Valor |
|---|---|
| Fecha de sesión | 2026-09-19 (UTC) |
| `origin/main` | `215ed1ac12d5ceaf201de2d53cd3e9d719ef049d` — "fix(preflight): distinguish implicit Skyrim masters from MO2 plugins (#595)" |
| Rama de trabajo | `docs/pre-lod-p0-evidence-20260919` (creada desde `origin/main`; ejecutada en worktree aislado) |
| Fuente del plan v3 | `origin/claude/pre-lod-material-pipeline-2ocqri` @ `07832bc5412e974c27639d3e4f354df0417c6428`; blob `e6ffee47f52dcc7259a046106b7d4e2812895f41` (127 075 bytes) |
| PRs abiertos al abrir | #597 `fix/dyndolod-cli-prerequisites` (lane A, actualizado 2026-09-19 21:59Z), #528 `feat/t5v2-uia-output-gate` (2026-09-05) |
| PRs abiertos durante la sesión | #598 `feat/runtime-vault-native-node-evidence-gp2-s1` (lane B, 2026-09-19 22:34Z) |
| Write-set prohibido (respetado) | `sky_claw/local/tools/dyndolod_*`, `tests/test_dyndolod_*`, `docs/validation/*dyndolod*`, `sky_claw/local/runtime_vault/**`, `tests/test_runtime_vault_*`, `sky_claw/app/core/path_resolver.py`, `pyproject.toml`, lockfiles, `sky_claw.spec`, AGENTS, `docs/pending_ooda_status.md`, VFS broker/worker, scanner, ToolsInstaller, GUI, agente LLM, config |

**Incidente de concurrencia (material).** El checkout principal `E:\Skyclaw_Main_Sync`
es compartido y fue tomado por otro carril durante esta sesión: pasó de estar en
`docs/pre-lod-p0-evidence-20260919` a `fix/dyndolod-cli-prerequisites` con modificaciones
tracked sin commitear (`path_resolver.py`, `tests/test_contrato_argumentos_cli.py`,
`tests/test_dyndolod_service.py`). Para no competir por la rama ni tocar estado ajeno, la
investigación y el commit se hicieron en un **worktree aislado**
(`C:\Users\Facu2\AppData\Local\Temp\kilo\pre-lod-p0`, rama `docs/pre-lod-p0-evidence-20260919`).
Ningún archivo del checkout compartido fue modificado por esta sesión.

**Write-set efectivo de esta sesión:**

```
A  docs/design/plans/2026-08-19-pre-lod-material-pipeline-v3.md      (restaurado desde la rama histórica)
A  docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md   (este documento)
```

---

## 2. Evidence taxonomy

Estados usados en todo el documento. No son intercambiables.

| Estado | Significado | Re-derivable desde el repo |
|---|---|---|
| **VERIFIED** | Leído en fuente primaria durante esta sesión: repo + archivo + símbolo + tag/commit, o página viva con fecha. | Sí, con acceso a la fuente |
| **SUPPORTED_BY_PACKAGE** | Inspección estática de un paquete real hecha por el operador, fuera de esta sesión, con SHA-256 declarado (equivalente T2 de la v3). | No — requiere el artefacto |
| **SUPPORTED_BY_UPSTREAM** | Documentación oficial del autor (wiki, README, changelog, Nexus). | Sí, con acceso |
| **INFERRED** | Derivado por razonamiento de otra evidencia; no observado ni documentado. | N/A |
| **CONFLICTING_EVIDENCE** | Dos fuentes primarias o cuasi-primarias se contradicen. | N/A |
| **UNKNOWN** | No hay evidencia suficiente. Es una salida válida. | N/A |
| **REAL_RIG_REQUIRED** | Solo se cierra ejecutando en un rig con MO2/Skyrim reales. | N/A |

Mapeo con la v3 (§2.0): T1 ≈ VERIFIED; T2 ≈ SUPPORTED_BY_PACKAGE; T3 ≈
SUPPORTED_BY_UPSTREAM / INFERRED. *Regla de método vigente (v3 §2.2): identificar
artefactos por hash + coherencia con la invocación, nunca por nombre de archivo.*

---

## 3. Sources

| Source | Versión / commit | Tipo | Confianza | Uso |
|---|---|---|---|---|
| `hakasapl/PGPatcher` — repo oficial | clone `main` @ `7bbe98ab` (2026-09-17), tags `1.2.0`=`3c43295`, `1.3.0`=`af2585af`, `2.0.0`=`cb065823`, `2.1.0`=`efba3af`, `2.1.1`=`62a6679` | Código fuente (GPL-3) | VERIFIED | B4/B5/B7/B8/B9, matriz |
| `hakasapl/PGPatcher` — releases GitHub | 2.1.1 (2026-09-18) Latest; 2.1.0 (09-15); 2.0.0 (09-10); 1.3.0 (08-31); 1.2.0 (07-21) | Releases oficiales | VERIFIED | versiones y fechas |
| PGPatcher wiki (repo `.wiki.git`) | HEAD del 2026-09-19 | Docs oficiales | SUPPORTED_BY_UPSTREAM | B7/B9, ownership, PGTools |
| PGPatcher `CHANGELOG.md` | `main` @ `7bbe98ab` (770 líneas) | Docs oficiales | VERIFIED | deltas entre versiones |
| Nexus PGPatcher (mod 120946) | 2.1.1, actualizado 2026-09-18; GPL-3 | Página del autor | VERIFIED | licencia, requisitos |
| Nexus ParallaxR (mod 124711) | v3.0318, actualizado 2026-03-18 | Página del autor | VERIFIED | permisos, requisitos |
| Nexus BENDr (mod 121578) | v3.0331, actualizado 2026-03-31 | Página del autor | VERIFIED | permisos, requisitos |
| Nexus VRAMr (mod 90557) | v16.0310, actualizado 2026-05-05 | Página del autor | VERIFIED | permisos, changelog |
| `loot/skyrimse` masterlist | HEAD `e3c591ba` (2026-08-28), línea 2647 | Fuente primaria LOOT | VERIFIED | B7 (orden de plugins) |
| Paquete ParallaxR v3.0318 + `ParallaxR.bat` | SHA-256 `72d9e191…` | Paquete inspeccionado | SUPPORTED_BY_PACKAGE | H2–H8 (v3 §2.4) |
| Paquete BENDr v3.0331 + `BENDr.bat` | SHA-256 `bd6f7887…` | Paquete inspeccionado | SUPPORTED_BY_PACKAGE | H2–H8 (v3 §2.4) |
| Paquete VRAMr v16.0310 + `script.bat` | zip `b36b1841…`, bat `ef085ffd…` | Paquete inspeccionado | SUPPORTED_BY_PACKAGE | workflow, presets (v3 §2.5) |
| PGPatcher 1.2.0 (zip) | SHA-256 `9981cd96…`; exes `7027f055…`/`cc6493b6…`, `PGLib.dll` `b6ddcca2…` | Paquete inspeccionado | SUPPORTED_BY_PACKAGE | PG1–PG5 (v3 §2.6) |
| Repo Sky-Claw | `origin/main` @ `215ed1ac` | Código | VERIFIED | anclas re-verificadas |

> Los artefactos de terceros **no se copiaron al repo** (§26 del meta-prompt): solo hashes,
> nombres, tablas de args, citas cortas y conclusiones. Los hashes de paquetes son
> provenance del operador (v3 §2), no re-calculados en esta sesión.

---

## 4. Matriz de versiones de PGPatcher

### 4.1 Contexto de versiones (VERIFIED)

| Release | Tag | Fecha | Notas de contrato |
|---|---|---|---|
| 1.2.0 | `3c43295` | 2026-07-21 | versión del paquete inspeccionado por el operador |
| 1.3.0 | `af2585af` | 2026-08-31 | +`--no-esm`; `--esm-all` reemplaza opción GUI; −`--force-light/--force-dark` |
| 2.0.0 | `cb065823` | 2026-09-10 | +`--autostart-update` y "Update Output"; +`PGPatcher_UpdateCache.bin`; cambios BREAKING en JSON de PBR |
| 2.1.0 | `efba3af` | 2026-09-15 | GUI/i18n, soporte high-DPI; sin cambios de contrato CLI |
| 2.1.1 | `62a6679` | 2026-09-18 | **Latest**; fixes de shader/facegen |
| `main` | `7bbe98ab` | 2026-09-17 | post-2.1.1 (docs); base del clone local |

La versión pública real a 2026-09-19 es **2.1.1** (Nexus 120946 y GitHub Releases).
El paquete 1.2.0 inspeccionado por el operador está **cuatro releases estables atrás**
(1.3.0, 2.0.0, 2.1.0, 2.1.1).

### 4.2 Matriz de contratos

Estados: **ESTABLE** (igual en 1.2.0 y 2.1.1) · **VERSION_DEPENDENT** · **NUEVO** · **REMOVIDO**.

| Contrato | 1.2.0 (tag `3c43295` + paquete) | 2.1.1 (tag `62a6679` / `main` `7bbe98ab`) | Estado |
|---|---|---|---|
| Ejecutables en el paquete | `PGPatcher.exe`, `pgtools.exe`, `PGLib.dll`, `PGMutagenWrapper.dll`, `dotnetlib/`, `cshaders/`, `assets/` | mismos; desde 1.3.0 agrega `translations/`; PDBs en archive | ESTABLE |
| CLI — generación | `--autostart` ("Start generation without user input") | `--autostart` ("regenerates the output from scratch") | ESTABLE (semántica precisada en 2.0) |
| CLI — update | — | `--autostart-update` (mutuamente excluyente con `--autostart`) | NUEVO en 2.0.0 |
| CLI — consola | `--console` | `--console` | ESTABLE |
| CLI — checks | `--ignore-mo2vfscheck`, `--consider-allmeshes` | idem | ESTABLE (desde 1.1.0) |
| CLI — shaders | `--disable-dyncubemap`, `--force-always-cm` | idem | ESTABLE |
| CLI — ESM | (GUI: opción "ESMify Plugin", `--force-light/--force-dark` presentes) | `--esm-all`, `--no-esm`; `--force-*` removidos | VERSION_DEPENDENT (cambio en 1.3.0) |
| CLI — otros | `--exclude-facegens` (nuevo en 1.2.0) | idem | ESTABLE |
| Modo interactivo | sin `--autostart` abre GUI y espera | idem | ESTABLE |
| Config — fuente | `<exe>/cfg/settings.json` (`params.output.dir`, `params.output.zip`, `params.output.pluginlang`, `params.modmanager.*`, `params.processing.pluginesmify`) | `<exe>/cfg/settings.json` sin `pluginesmify` (ESM por CLI) | ESTABLE con campo removido (VERSION_DEPENDENT) |
| Config — otros | `cfg/modrules.json`, `cfg/ignored_messages.json` | idem | ESTABLE |
| Output — destino | `params.output.dir` (config); zip opcional → `PGPatcher_Output.zip` | idem | ESTABLE |
| Output — restricciones | no puede ser `Data` ni subdirectorio de `Data` | idem | ESTABLE |
| Output — **ownership exclusivo** | "Output directory has non-PGPatcher related files…" (mismo string en `PGLib/src/PGPatcher.cpp`) | idem | ESTABLE |
| Output — archivos permitidos | `pgpatcher.esp`, `parallaxgen_diff.json`, `meta.ini`, `pgpatcher_output.zip`, `pg_*.esp`, dirs {`meshes`,`textures`,`pbrnifpatcher`,`lightplacer`,`pbrtexturesets`} | + `PGPatcher_UpdateCache.bin[.tmp]` | ESTABLE + artefacto nuevo (2.0.0) |
| Plugin — nombres | `PGPatcher.esp` (TXST nuevos); `PG_<N>.esp` (overrides; split al llegar a 254 masters) | idem | ESTABLE |
| Plugin — ESM/ESL | `PGPatcher.esp` siempre master-flagged; ESL si `maxFormID ≤ 0xFFF`; `PG_<N>.esp` master solo si `processing.pluginesmify=true`; split siempre ESL | `Finalize(outputDir, esmMode)`: 0=ESM solo `PGPatcher.esp` (default), 1=todos, 2=ninguno; split siempre ESL; ESL de `PGPatcher.esp` si `maxFormID ≤ 0xFFF` | VERSION_DEPENDENT (control ESM pasó de config a CLI en 1.3.0) |
| Plugin — omisión | `Finalize` retorna temprano si no hay records ni modified records → no se escribe plugin | idem | ESTABLE |
| `ParallaxGen_Diff.json` | solo si el diff no está vacío; para CRC32 de DynDOLOD | idem | ESTABLE |
| Cache | — | `PGPatcher_UpdateCache.bin` en el output; deshabilitado con zip; base de "Update Output" | NUEVO en 2.0.0 |
| Log | `<exe>/log/PGPatcher.log`; borra logs previos `PGPatcher*.log` al iniciar; sink rotativo; flush INFO | idem | ESTABLE |
| Interacción DynDOLOD/TexGen | si `dyndolod.esp` está activo → `critical` y **aborta** | idem | ESTABLE |
| Interacción VRAMr | si `vramroutput.tmp` visible y mod manager ≠ None → `critical` y **aborta** | idem | ESTABLE (desde 0.9.8) |
| `--ignore-mo2vfscheck` | apaga el guard de USVFS | idem | ESTABLE |
| Exit codes | `0` también en rechazos `critical`; `1` solo por excepción no capturada | idem | ESTABLE |
| Shader patchers | parallax, CM, TruePBR (+pre/post) | + cambios de permisos PBR 2.1.0 | ESTABLE (config) |

**Conclusión de matriz.** El "esqueleto" del contrato (nombres de exe, config, ownership,
nombres de plugin, checks de preflight, log, exit codes) es estable 1.2.0 → 2.1.1.
Las diferencias reales son de **superficie CLI** (ESM, update mode) y de **artefactos**
(cache). Un adapter único *puede* cubrir 1.2.x–2.1.x **solo si** se diseña con capacidades
por versión, no con comportamiento fijo (→ B8, §9; `ToolContractFingerprint` conceptual).

---

## 5. B4 — `PGPatcher.exe` vs `pgtools.exe`

**Conclusión: `PGTOOLS_IS_NOT_FULL_PATCHER` (VERIFIED).** No sustituye a `PGPatcher.exe`
para el pipeline de Sky-Claw.

Evidencia:

| Hecho | Fuente |
|---|---|
| PGTools es un CLI companion: "makes use of the same backend as PGPatcher.exe. It is primarily intended to be a mod author tool." | wiki `PGTools.md` |
| "PGTools can run any of the patchers on a local folder. For now it will only read loose files and all relevant files must be in the directory you are running from." | wiki `PGTools.md` |
| `PGTools/src/main.cpp` construye `PGDirectory(source, output)`, `populateFileMap(false)`, `mapFiles({}, {}, {}, {}, multithreading)` — no lee load order ni plugins | `PGTools/src/main.cpp:114-148` @ `7bbe98ab` |
| No referencia `PGModManager`, ni `PGPlugin::initialize/savePlugin`, ni VFS/MO2, ni config `cfg/` | `PGTools/src/main.cpp` (grep exhaustivo del archivo) |
| Patchers disponibles: `fixmeshlighting`, `fixtextureslotcount`, `parallax`, `complexmaterial`, `truepbr`, `parallaxtocm`, `particlelightstolp`, `restoredefaultshaders`, `fixsss`, `hairflowmap`, `converttohdr` | `PGTools/src/main.cpp:182-227` |
| CLI: `pgtools [-v] [--no-multithreading] [--shortcut] patch <patchers> [source] [output] [--high-mem]` | `PGTools/src/main.cpp:264-285`; wiki |
| Sin plugins: "Add a dummy mesh use to trigger base patching (pgtools uses this since no plugins)" | `PGLib/src/PGPatcher.cpp:527` @ `7bbe98ab` |
| `pgtools` CLI estable 1.2.0 → 2.1.1 (mismo set); 2.0.0 corrige `--no-multithreading` y la copia del cubemap dinámico | tags `1.2.0`/`2.1.1` + `CHANGELOG.md` |

**Qué comparte:** el backend de patchers (`PGLib`) y el inicializador de GPU/shaders.
**Qué no comparte:** detección de mod manager, VFS check, load order, generación de
plugins (`PGPatcher.esp`/`PG_X.esp`), `ParallaxGen_Diff.json`, config persistente,
conflict manager, cache de update.

Consecuencia para Sky-Claw: `pgtools` puede servir como *tool de autor* o para experimentos
de rig (patch de una carpeta loose), pero **ningún adapter del pipeline puede usarlo como
sustituto** de `PGPatcher.exe`.

---

## 6. B5 — USVFS / `--ignore-mo2vfscheck`

### 6.1 PGPatcher

**Implementación del check (VERIFIED, `main` @ `7bbe98ab`):**

- `PGPatcher/src/main.cpp:503-509`: el guard corre **solo** si
  `params.modManager.type == ModOrganizer2 && !mo2InstanceDir.empty()`; si
  `!args.ignoreMO2Check && !PGHandlers::isUnderUSVFS()` → `Logger::critical("Please verify
  that you are launching PGPatcher from MO2, VFS not detected.")` y `return` (exit code 0).
- `PGHandlers::isUnderUSVFS()` = `!PGModManager::mo2DirFromUSVFS().empty()`
  (`PGLib/include/PGHandlers.hpp:120`).
- `PGModManager::mo2DirFromUSVFS()` busca `usvfs_x64.dll` entre los módulos cargados del
  proceso (`PGLib/src/PGModManager.cpp:664-695`; `mo2UsvfsDLLName` en `PGModManager.hpp:113`).

**Qué hace el flag:** desactiva **únicamente el guard**. No cambia cómo se construye la
vista de datos. `--ignore-mo2vfscheck` es **CHECK_DISABLED**, no `VFS_NOT_REQUIRED`.

**Qué sí se sabe de un run sin VFS:** el mapeo de mods de MO2 se construye **leyendo disco
directamente**: `PGModManager::populateModFileMapMO2()` lee `modorganizer.ini`, resuelve
`profiles/<selected>/modlist.txt` y mapea las carpetas de cada mod por prioridad
(`PGLib/src/PGModManager.cpp:335-500`). Por eso el run *puede* funcionar sin VFS; pero la
vista de `Data` (overwrite, archivos virtualizados, prioridad fina de loose files) no está
garantizada sin USVFS, y el autor documenta el flag "might be useful for Linux users".

**Clasificación:** `USVFS_REQUIRED` para la automatización soportada en Windows/MO2
(el propio binario aborta sin él); la corrección de un run con el flag es **UNKNOWN** →
`REAL_RIG_REQUIRED` (experimento R-PG-1, §21). La decisión de arquitectura no cambia:
el camino con atestación (handler en el worker, v3 §6.4 opción A) es el único que
*prueba* estar bajo la VFS.

### 6.2 ParallaxR / BENDr / VRAMr

**Evidencia disponible (SUPPORTED_BY_PACKAGE, v3 §2.4/§2.5 + Nexus):**

| Herramienta | A favor de "espera la vista virtual" | A favor de "trabaja fuera de la VFS" |
|---|---|---|
| ParallaxR | Guard `if exist .\ParallaxROutput.tmp` dentro de `GameDir` (solo visible si el output está virtualizado); `tasklist ModOrganizer.exe`; `LooseCopy --source .\textures` sobre `Data` | `MakeUnpack --profile` + `ExtractBSA --layered` resuelven prioridad de mods por fuera de la VFS |
| BENDr | Mismo framework y mismas invocaciones que ParallaxR (MakeUnpack/ExtractBSA/LooseCopy) | v3.0331 "built on the same framework as VRAMr" |
| VRAMr | `LayerPrep --profile` sugiere lectura de perfil real; funcionalmente equivalente a BENDr | Changelog v13.4: "Now a 2 part process to **cut loose from MO2's Virtual File System restrictions**"; v8.52 layered BSA extract para MO2; `--profile` explícito |

**Clasificación: `R_SUITE_VFS = REAL_RIG_REQUIRED`.** La evidencia es contradictoria y no se
resuelve por consenso entre investigaciones Arena (meta-prompt §11). Hasta el rig, ningún
adapter de ParallaxR/BENDr/VRAMr puede asumir ni exigir USVFS; el experimento que lo cierra
está en §21.

---

## 7. B6-L — Licencias / supportability

B6-T (invocabilidad técnica) **sigue cerrado** y no se reabre (v3 §2.2/§3.1). B6-L
responde otra pregunta: ¿puede Sky-Claw invocar oficialmente helpers internos?

| Herramienta | Licencia/permisos (Nexus, consultado 2026-09-19) | Ejecutar copia instalada | Descargar | Redistribuir/Bundlear | Modificar | **Direct helper API** |
|---|---|---|---|---|---|---|
| PGPatcher | GPL-3.0; fuente pública; autor: "GPL-3 license and everything that entails" | Sí | Sí (GitHub Releases / Nexus) | GPL permite, pero **política Sky-Claw: NO_VENDOR/NO_BUNDLE** (repo MIT) | Sí (GPL) | **Sí** — CLI documentada y es la interfaz del producto |
| ParallaxR | Upload: no a otros sitios; Modificación: con permiso; Conversión: no; Asset use: con permiso; nota: "It should be fine for you to upload/share ParallaxR Outputs"; "You may not share any of the ParallaxR mod files itself without checking the license restrictions" | Sí (copia local del usuario) | Sí (Nexus, manual) | No | Con permiso | **OPEN** — sin autorización explícita para invocación por terceros |
| BENDr | Upload: no; Modificación: con permiso; Conversión: no; Asset use: con permiso; **sin author notes** | Sí | Sí (Nexus, manual) | No | Con permiso | **OPEN** |
| VRAMr | Igual patrón de permisos; author notes: "You may prepare for Collections or Wabbajacks VRAMr outputs and distribute these on Nexus without my permission. **No other activity permitted without prior written consent.**" | Sí | Sí (Nexus, manual) | No | Con permiso | **OPEN** (y condicionada a consentimiento escrito) |

Notas adicionales:

- Los paquetes de la R-suite empaquetan componentes de terceros (TexConv de MSFT, 7-Zip
  LGPL, BSA Browser de AlexxEG; créditos en las páginas Nexus). Eso refuerza
  `NO_BUNDLE/NO_REDISTRIBUTE` incluso si el autor diera permiso sobre lo propio.
- Requisitos off-site de las tres: .NET Desktop Runtime x64 y x64 C++ Redistributable.
  Datos para preflight, no para instalar silenciosamente.
- **Default vigente hasta evidencia contraria:** `MANUAL_ONLY`, `NO_VENDOR`, `NO_BUNDLE`,
  `NO_REDISTRIBUTE`, `DIRECT_HELPER_API = OPEN`. B6-L permanece **abierto** para las tres.
  La vía para cerrarlo es pedir permiso escrito al autor, no reinterpretar los permisos.

---

## 8. B7 — Ciclo de vida de los plugins generados

### 8.1 Qué genera PGPatcher (VERIFIED)

| Artefacto | Contenido | Flags | Fuente |
|---|---|---|---|
| `PGPatcher.esp` | **solo** TXST nuevos usados por `PG_<N>.esp`; no overridea records | ESM por default (modo 0); ESL si `maxFormID ≤ 0xFFF` | wiki `Output.md`; `PGMutagen.cs:183,525-532` |
| `PG_<N>.esp` | overrides de los records modelados del load order (STAT/ACTI/ARMO/ARMA/MSTT etc. según `ModelRecordType`) | split si la lista de masters llegaría a ≥254; **siempre ESL**; ESM solo en modo 1 | wiki `Output.md`; `PGMutagen.cs:738-755,1260-1303` |
| `ParallaxGen_Diff.json` | CRC32 por mesh parcheado, para que DynDOLOD matchee meshes pese al cambio de hash | solo si no está vacío | wiki `Output.md`; `main.cpp:840-847` |
| `PGPatcher_UpdateCache.bin` | cache de update (≥2.0.0); deshabilitado con zip | — | `PGRunCache.hpp:50`; `main.cpp:861` |

**Cuándo el plugin NO se genera:** `Finalize` retorna temprano si no hay records en
`OutMod` ni `ModifiedRecords` (`PGMutagen.cs:534-541`); y si el output quedó vacío, el
runner sale antes de guardar plugins (`main.cpp:792-797`). **`PGPatcher.esp` no es un
artefacto obligatorio de toda corrida.**

### 8.2 Qué dice upstream sobre orden (SUPPORTED_BY_UPSTREAM)

- "The installed mod should overwrite everything in your load order **except DynDoLOD/TexGen
  outputs**." (wiki `Basic-Usage.md:56`)
- "`PG_X.esp` plugins … **MUST BE LOADED LAST**, except for DynDoLOD/TexGen Outputs.
  **LOOT will sort these plugins correctly**." (wiki `Basic-Usage.md:58`)
- "Keep PGPatcher output enabled" durante TexGen/DynDOLOD (wiki `Basic-Usage.md:67-69`).
- "Every time the PGPatcher output is regenerated, TexGen/DynDoLOD must be regenerated
  **with PG output enabled** … DynDoLOD needs to see the PGPatcher records" (wiki
  `Troubleshooting-Guide.md:41`).

### 8.3 LOOT real (VERIFIED, masterlist `e3c591ba`, 2026-08-28)

```yaml
- name: 'P(arallaxGen|G_\d+)\.esp'
  url: [ 'https://www.nexusmods.com/skyrimspecialedition/mods/120946/' ]
  group: *lateChangesGroup
```

- Cubre `ParallaxGen.esp` (nombre histórico) y `PG_<N>.esp`.
- **No cubre `PGPatcher.esp`** (el regex no tiene una alternativa que matchee "PGPatcher").
- `lateChangesGroup` está **después** de `dynamicPatchesGroup` (Bashed Patch/Smashed Patch)
  y **antes** de `dynamicLODGroup` (DynDOLOD). Coincide con el requisito de upstream.

**Hallazgo de drift:** la wiki de upstream dice "LOOT will sort these plugins correctly"
pero la masterlist **no tiene regla para `PGPatcher.esp`**. Como `PGPatcher.esp` es un
holder de TXST y por default va ESM-flagged, su posición real debe verificarse en rig
(R-PG-4). Sky-Claw no puede asumir que LOOT lo ubica "correctamente".

### 8.4 Respuestas A–F

Categorías: **UPSTREAM_REQUIREMENT** · **SKYCLAW_POLICY** (decisión propuesta, no congelada) · **UNKNOWN**.

| # | Pregunta | Respuesta | Categoría / evidencia |
|---|---|---|---|
| A | ¿PGPatcher después del LOOT principal? | Sí dentro de una corrida de pipeline: las etapas 1–7 producen el estado que PG debe ver; PG se ejecuta después y **sus plugins nuevos requieren una pasada de sort/reconcile posterior** porque no existían en el LOOT principal. | SKYCLAW_POLICY (UPSTREAM no se pronuncia sobre LOOT principal) |
| B | ¿LOOT/reconcile después de PGPatcher? | Sí: `PG_<N>.esp` debe quedar último salvo DynDOLOD/TexGen. La masterlist lo soporta si LOOT corre **después** de la generación. | UPSTREAM_REQUIREMENT (`Basic-Usage.md`) + VERIFIED (masterlist) |
| C | ¿Wrye Bash necesita consumir el plugin PG? | No consta requisito upstream. Riesgo real: Bash corriendo con un output PG **stale** habilitado consume records PG; si Bash regenera y cambia formids/master refs, `PG_<N>.esp` queda invalidado. | UNKNOWN (upstream) + SKYCLAW_POLICY: Bash/Synthesis no deben correr con output PG stale habilitado |
| D | ¿Synthesis? | Igual que C. Sin requisito upstream; mismo riesgo de staleness. | UNKNOWN + SKYCLAW_POLICY |
| E | ¿Rerunear Bash/Synthesis después de PG? | No hay requisito; default: no rerunear después de PG. Si el operador lo hace, **PG debe regenerarse** (los masters de `PG_<N>.esp` pueden quedar obsoletos). | SKYCLAW_POLICY |
| F | ¿Output PG viejo habilitado? | PGPatcher **aborta**: si el output mod está habilitado bajo MO2 VFS (`PGModManager.cpp:402-405`, "you must disable the mod … first"); y si hay `ParallaxGen_Diff.json` en `Data`, "PGPatcher meshes exist in your data directory, please delete before re-running" (`main.cpp:554-561`). El output debe estar OFF antes del run y ON después, para LOD. | VERIFIED |

Límite de 254 masters: PG lo resuelve **internamente** partiendo en `PG_<N>.esp`
(`PGMutagen.cs:1284`); cada split suma `PGPatcher.esp` como master. El ancla de límite de
plugins de Sky-Claw debe contemplar este patrón (no es un plugin único).

### 8.5 Política conservadora Sky-Claw: output PG stale vs etapas 5–7 (B10)

**SKY-CLAW CONSERVATIVE POLICY** — no es un requisito upstream demostrado; upstream no se
pronuncia sobre esta interacción (B10 sigue abierto, §19). Se adopta por integridad del
load order:

```
si cualquier Stage 5 (LOOT), 6 (Wrye Bash) o 7 (Synthesis) se vuelve a ejecutar
después de una corrida de PGPatcher:

    PG output -> STALE

    antes:    deshabilitar el output PG previo cuando corresponda
    después:  re-ejecutar PGPatcher -> reconcile/sort post-PG -> recién entonces LOD
```

Motivo: Bash/Synthesis pueden consumir records PG de una corrida previa, y regenerar esos
plugins invalida los masters de `PG_<N>.esp`. Hasta que R-PG-7 cierre B10, esta secuencia
conservadora es la única segura.

---

## 9. B8 — Versionado del contrato de PGPatcher

**Pregunta:** ¿se puede soportar 1.2.x y 2.1.x con el mismo contrato de adapter?

**Respuesta: SÍ para los contratos conocidos y verificados; NO se asume compatibilidad de
versiones futuras.** El adapter se construye con capacidades por versión, y una versión
nueva solo se habilita cuando su contrato fue registrado y verificado (política de
*known contracts*, no de rango abierto).

Contrato propuesto (conceptual, **no implementar**): `ToolContractFingerprint`

```
ToolContractFingerprint
  tool_key              "pgpatcher"
  flavor                "pgpatcher" | "pgtools"
  reported_version      de PE product version o del log ("Welcome to PGPatcher version {}!")
  exe_sha256            hash del exe detectado
  known_contracts       {1.2.0, 1.3.0, 2.0.0, 2.1.0, 2.1.1} — fingerprints verificados en P0.
                        NO es un rango abierto: cada entrada tiene su artifact_matrix y flags
  capabilities          por contrato conocido (capacidad introducida en cada versión):
                          AUTOSTART              introducido en 0.5.0
                          VFS_CHECK_IGNORE       introducido en 1.1.0
                          CONSOLE                introducido en 0.9.9
                          EXCLUDE_FACEGENS       introducido en 1.2.0
                          ESM_MODE_CLI           introducido en 1.3.0 (--esm-all/--no-esm)
                          UPDATE_OUTPUT          introducido en 2.0.0 (--autostart-update + cache)
                          PBR_JSON_SCHEMA_V2     introducido en 2.0.0 (BREAKING: campos PBR)
  unknown_policy        versión/fingerprint desconocido -> fail closed: no se ejecuta
                        (VERSION_UNSUPPORTED) hasta registrar y verificar su contrato.
                        Versión ilegible -> VERSION_UNKNOWN degradado, sin asumir flags;
                        forzar un contrato no registrado exige CONFIGURATION_REQUIRED explícito
  config_schema         {settings.json, modrules.json, ignored_messages.json}
  ownership_policy      EXCLUSIVE_DIR (estable 1.2.0–2.1.1)
  artifact_matrix       por versión (¿incluye cache? ¿zip?)
```

Reglas:

1. El adapter **no** asume flags ni compatibilidad: consulta el fingerprint reconocido y
   **falla cerrado** ante una versión/fingerprint no registrado (ver `unknown_policy`).
2. El fingerprint entra en la evidencia de corrida (§16) y en la invalidación: actualizar
   la herramienta cambia el fingerprint y **prohíbe reutilizar corridas previas**.
3. **Semántica de soporte: contratos conocidos, no rango abierto.** Hoy los fingerprints
   verificados son {1.2.0, 1.3.0, 2.0.0, 2.1.0, 2.1.1}. Una versión futura (2.2.x, 3.x, …)
   **no** hereda el contrato por comparación SemVer: `ToolContractFingerprint` existe
   precisamente para que una versión nueva no pase automáticamente por un contrato viejo.
   El default de Sky-Claw debe ser `--autostart` (regenerar desde cero) para no depender
   del cache; `--autostart-update` solo si una corrida incremental se justifica explícitamente.

Ejemplos VERSION_DEPENDENT que el adapter debe manejar: control ESM (config `pluginesmify`
en 1.2.x vs `--esm-all`/`--no-esm` desde 1.3.0); presencia de `PGPatcher_UpdateCache.bin`
(desde 2.0.0); semántica de `--autostart` (regenerar vs "sin input").

---

## 10. B9 — Política de orden VRAMr ↔ PGPatcher

**Ambas políticas están soportadas por upstream (SUPPORTED_BY_UPSTREAM, wiki
`Basic-Usage.md:71`):**

> "VRAMr is not included in this list because order doesn't technically matter. If you run
> VRAMr after PGPatcher, ensure PGPatcher output is enabled and generate VRAMr with it
> enabled. If you run VRAMr before PGPatcher, ensure its output is **DISABLED** before
> running PGPatcher."

**Restricción dura (VERIFIED):** PGPatcher ≥ 0.9.8 aborta si `vramroutput.tmp` está visible
y hay mod manager (`main.cpp:563-567`; agregado en 0.9.8, 2025-12-01).

Especificación conceptual (no implementar):

```
VramrOrderingPolicy(version_tuple)
  POLICY_A — VRAMr -> PGPatcher
    pre:  VRAMr corre con su output habilitado (según su flujo normal)
    pre:  ANTES de PGPatcher: VRAMr output DISABLED          (requisito PG >= 0.9.8)
    post: re-enable VRAMr output; PGPatcher output habilitado CON prioridad sobre VRAMr
  POLICY_B — PGPatcher -> VRAMr
    pre:  VRAMr output DISABLED durante el run de PGPatcher  (requisito PG >= 0.9.8)
    pre:  PGPatcher output ENABLED durante el run de VRAMr   (para que VRAMr vea sus texturas)
    post: ambos habilitados; PGPatcher output con prioridad sobre VRAMr
  version_gate: PGPatcher >= 0.9.8 (critical si VRAMr output habilitado)
  invalidation:
    - si PGPatcher regenera, la corrida de VRAMr queda stale (VRAMr.DB y output)
    - si VRAMr regenera, PGPatcher no necesariamente queda stale (VRAMr solo downscalea)
      pero el winner de texturas cambia -> re-validar success contract de PG
```

**¿Cambió entre versiones?** No dentro del rango 1.2.x–2.1.x: el check es idéntico en
1.2.0 y 2.1.1. El corte histórico es 0.9.8 (introducción del critical). Fuera de ese rango
no se investigó y no importa: el adapter solo habilita contratos/fingerprints reconocidos (§9).

**Default propuesto Sky-Claw (no congelado):** `POLICY_B` (PGPatcher primero, VRAMr último)
porque requiere un solo toggle de estado y VRAMr optimiza el resultado final; `POLICY_A`
soportada. La decisión final queda para P1 con la matriz de outputs (§14).

---

## 11. VFS Canary — especificación de prueba (no implementar)

### 11.1 Qué existe hoy en Sky-Claw (VERIFIED, repo @ `215ed1ac`)

`sky_claw/local/mo2/vfs_attestation.py` ya implementa un canary de archivo real:

1. `build_attestation_challenge()` elige un archivo de un mod **habilitado** que no esté
   sombreado por un mod de mayor prioridad ni por `Data` físico, y captura
   `source_mod`, `relative_path`, `sha256` y un **fingerprint del perfil** pre-HITL
   (`vfs_attestation.py:142-223`).
2. `verify_vfs_attestation()` comprueba que el archivo sea visible bajo la ruta virtual,
   que su hash coincida y que el fingerprint del perfil no haya cambiado
   (`vfs_attestation.py:226-268`).
3. El worker exige prueba de **proceso nieto** (`grandchild_probe`) y el broker valida
   `grandchild_sha256` contra el challenge (`vfs_worker.py:166-174`;
   `vfs_broker.py:715-730`).

Esto ya demuestra los tres puntos pedidos: (1) el proceso ve el archivo no-físico,
(2) un hijo/nieto ve la misma vista, (3) el perfil observado es el esperado.

### 11.2 Evaluación de `SkyClaw.VfsProbe` (mod temporal con nonce)

| Criterio | Veredicto |
|---|---|
| ¿Aporta sobre la atestación existente? | Solo un nonce *fresco* por corrida y un nombre controlado. La atestación actual ya usa hash + fingerprint de perfil y prueba de nieto. |
| Costo | **Muta estado MO2** (crear mod, habilitarlo, deshabilitarlo, borrarlo) — exactamente lo que el pipeline evita antes de correr; invalida fingerprints y puede dejar drift si falla a mitad. |
| Riesgo | Un mod de prueba habilitado durante la corrida real puede ser visto por las herramientas (contaminación del file map) si no se limpia perfecto. |
| Uso válido | Experimento de **rig**, en un perfil sandbox, para validar que una herramienta concreta (pgtools/BENDr/VRAMr) ve la vista virtual y que sus hijos la heredan. |

**Especificación `VFS_CANARY_TEST_SPEC` (rig, opt-in, no implementar ahora):**

```
Nombre:            SkyClaw.VfsProbe (mod temporal, solo en perfil sandbox clonado)
Contenido:         Data/skyclaw_vfs_probe_<run_uuid>.txt  { nonce: <aleatorio> }
Precondiciones:    - perfil clonado (ProfileSandbox), MO2 cerrado o perfil no seleccionado
                   - baseline de attestation tomado ANTES de crear el mod
Pasos:             1. crear mod y habilitarlo (entrada nueva, registrada para rollback)
                   2. lanzar la herramienta bajo el worker VFS con un job de sonda
                   3. el handler lee Data/<probe> y reporta nonce + sha256
                   4. el worker exige grandchild_probe: el hijo real re-lee y reporta
                   5. comparar contra el nonce esperado y el fingerprint de perfil
Aserciones:        - el proceso ve el nonce (visibilidad virtual, no ruta física)
                   - el nieto ve el mismo nonce
                   - el perfil observado == perfil sandbox esperado
                   - el archivo NO existe en Skyrim/Data físico
Evidencia:         nonce, hashes, árbol de procesos con tool_id, fingerprint antes/después
Cleanup:           deshabilitar + borrar el mod; verificar ausencia; si falla -> cuarentena
Abort conditions:  fingerprint cambió, nonce no visible, nieto no ve la vista, MO2 abierto
                   mutando el perfil
```

**Veredicto:** no es requisito para PGPatcher (la atestación existente alcanza y es más
segura); es un **experimento de rig recomendado** para cerrar la parte de la R-suite una
vez que B3 abra (§21, R-RS-1).

---

## 12. MO2 transaction model (especificación; no implementar)

Estados conceptuales requeridos, mapeados a los de la v3 §6.3:

| Requerido | Equivalente v3 | Semántica |
|---|---|---|
| `OPEN` | `SNAPSHOT` + `APPLIED[n]` | transacción abierta; entradas declaradas y registradas; fingerprint verificado antes de cada escritura |
| `COMMITTED` | `COMMITTED` | todas las mutaciones aplicadas y verificadas |
| `FAILED` | `ROLLED_BACK` | fallo propio; reversión dirigida de **nuestras** entradas |
| `DRIFT_DETECTED` | `DRIFT_DETECTED` | fingerprint no coincide; clasificar antes de actuar |
| `QUARANTINED` | `QUARANTINED` | nuestras entradas fueron alteradas por fuera → terminal, **cero escrituras**, diff preservado, HITL con default "dejar como está" |
| `RECOVERY_REQUIRED` | (nuevo explícito) | journal encontrado en arranque con transacción abierta: reconciliación de tres vías antes de cualquier mutación nueva |

Reglas duras (v3 §6.3, ratificadas):

1. Ownership **por entrada**, no por archivo: la reversión restaura solo los renglones que
   la transacción escribió, nunca un `os.replace` del snapshot completo.
2. Optimistic concurrency / CAS: re-leer + verificar fingerprint **antes** de cada
   escritura; si `current == expected_after_our_mutation` → revertir nuestra entrada; si el
   usuario alteró **nuestra** entrada → `QUARANTINED`; si alteró otra entrada → preservarla.
3. Journal (`OperationJournal`) + declaración previa de entradas en `ActionManifest`.
4. `delete_mod_files` **jamás** desde la transacción.
5. Lock cross-process vía `DistributedLockManager`; límite conocido: MO2.exe no respeta el
   lock → la detección de drift es **a posteriori**.
6. Todo mutador de `modlist.txt` pasa por la transacción (ancla AST enumerativa, estilo
   `test_db_connection_invariant.py`).

Nuevo requisito detectado en P0: la transacción debe cubrir también el **orden/prioridad**
(`set_mod_priority`/`move_mod_after`) porque las políticas de §10 y §8.2 requieren
"PGPatcher output por encima de VRAMr" y "PG plugins al final". La API actual
(`mo2/vfs.py`) no tiene prioridad; sigue siendo el gap v3 §6.1.

---

## 13. Sandbox-profile assessment

Preguntas del meta-prompt §19, con la evidencia P0:

| Pregunta | Hallazgo | Fuente |
|---|---|---|
| ¿Cada tool puede seleccionar el perfil sandbox? | PGPatcher **no**: el perfil sale de `modorganizer.ini` del instance (`selectedProfileFromInstanceDir`) y de `cfg/settings.json` (`params.modmanager.mo2instancedir`) | `PGModManager.cpp:354`, `PGConfig.cpp:178-181` |
| ¿Requiere cambiar `selected_profile` global? | Sí, o bien apuntar el tool a un **instance MO2 clonado**. Cambiar `selected_profile` con MO2 abierto es mutación global no coordinada | INFERRED de la lectura directa de `modorganizer.ini` |
| ¿Qué ocurre con MO2 abierto? | Ninguna tool coordina con MO2; MO2 puede reescribir `modlist.txt`/plugins durante el run → drift | v3 §6.3 límite 6 |
| ¿Cómo se promueve el delta? | Solo por el mecanismo existente `ProfileSandbox` + `sandbox_promotion` (HITL, fail-closed) | repo `local/mo2/profile_sandbox.py`, `app/orchestrator/sandbox_promotion.py` |
| ¿Qué no puede sandboxearse? | La vista `Data` que las tools leen (H3): un perfil clonado cambia qué mods están activos y por lo tanto el resultado del patch; el output físico y el cache de PG viven fuera del perfil | v3 §2.4 H3; PG config |
| ¿Qué tools leen config global? | PGPatcher (settings.json + modorganizer.ini), R-suite (perfil vía `--profile`, game dir) | §4.2, §6.2 |

**Clasificación:**

| Enfoque | Veredicto |
|---|---|
| Sandbox-first del perfil real para todo el pipeline | **UNSAFE** como requisito: cambia la semántica de lo que las tools ven y requiere tocar estado global de MO2 |
| Perfil sandbox para experimentos de rig | **GOOD_CANDIDATE** (junto con VFS probe) |
| Instance MO2 dedicado/duplicado para material pipeline | **PARTIAL** — técnicamente posible (PG acepta `mo2instancedir`), pero duplica mods (TB) y no está probado |
| Transacciones dirigidas sobre el perfil real (v3 §6.3) | **ACCEPT** — es el camino soportado hoy |

No convertir sandbox-first en requisito hasta demostrar compatibilidad por herramienta.

---

## 14. Output ownership (contratos por herramienta)

Cada output generado es una **entidad propia**, distinta del directorio de instalación de
la tool (`install_kind=TOOL` vs `OUTPUT_MOD`, v3 §5.3).

| Output | Dueño | Reglas |
|---|---|---|
| `Sky-Claw - PGPatcher Output` | `pgpatcher_output_target` (nuevo) | **Exclusivo**: antes del run debe estar vacío o contener solo artefactos PG (lista §4.2). Nada de sidecars de Sky-Claw dentro. Run metadata fuera. `DirectoryRollback` move-aside aplica porque PG regenera el target completo (v3 §2.6 PG3). |
| `Sky-Claw - VRAMr Output` | `vramr_output_target` | Exclusivo de VRAMr; marker `VRAMrOutput.tmp` (mtime como frescura); `VRAMr.DB` es estado interno → firmar. |
| ParallaxR Output / BENDr Output [2º corte] | propios | Exclusivos; los BAT originales usan `<Drive>:\ParallaxR` / `<Drive>:\BENDr` (H6) — Sky-Claw debe usar roots bajo su namespace, no esas rutas. |
| MO2 `Overwrite` | MO2 | Si durante un run aparece output inesperado no declarado → **FAIL/QUARANTINE**, nunca mover en silencio. Registrar para HITL. |

Nada se mezcla en silencio; la identidad del output es parte del `RunEvidence` (§16) y de la
invalidación downstream.

---

## 15. Success contracts (especificación)

Modelo común (no implementar):

```
process_result       exit code dentro del conjunto aceptado por ESA tool
run_identity         run_uuid + tx_id correlacionables
input_fingerprint    perfil/modlist/plugins/config relevantes
fresh_output         firma (mtime+size+hash de artefactos decisores) cambió vs. pre-launch
fresh_log/evidence   log escrito dentro de la ventana del run
no_terminal_failure  sin líneas de error terminal en el log
tool_specific_postconditions
```

**No** se usa `exit_code == 0` como único criterio; **no** se usa el marker estático como
prueba de corrida (H4); **no** se exigen artefactos que pueden ser opcionales.

Por herramienta:

- **PGPatcher:** exit code **no es suficiente** — los rechazos de preflight (`critical`)
  retornan **0** (`main.cpp:443-567` + `WinMain` `returnCode=0`). Evidencia de cierre:
  línea de log "PGPatcher took {} seconds to complete" (`main.cpp:1033`), ventana de log
  fresca, artefactos frescos, y **atestación VFS** cuando corre bajo el worker.
  `expected_artifacts` depende de la config y del trabajo observado:
  `PGPatcher.esp` puede no generarse (sin records); `ParallaxGen_Diff.json` solo si no está
  vacío; `PGPatcher_UpdateCache.bin` solo sin zip (±≥2.0.0); el zip borra el output loose
  (`deleteOutputDir(false)` tras crear `PGPatcher_Output.zip`). Precondición dura de
  ownership (directorio exclusivo) y de outputs ajenos OFF (DynDOLOD/TexGen/VRAMr/propio).
- **VRAMr:** contadores del log (`Converted/Failed`) con contexto ("¿había trabajo?"),
  marker fresco por mtime, **firma de `VRAMr.DB`** (entrada de `Filter.exe`/`Optimise.exe`;
  DB stale → resultado plausible y desactualizado), output fresco, Job Object sobre el árbol.
- **ParallaxR / BENDr [2º corte]:** definidos en v3 §7.4; quedan sin cambios.

Regla transversal: cada artefacto declarado tiene dueño, ruta absoluta bajo el namespace
`Sky-Claw/`, y hash registrado en el manifest de la corrida.

---

## 16. Freshness / RunIdentity (conceptual)

```
RunIdentity
  run_uuid              UUID de corrida
  tx_id                 correlación con OperationJournal
  tool                  external_tool_spec.key + flavor (pgpatcher | pgtools)
  tool_version          versión reportada (log/PE)
  executable_sha256     hash del exe lanzado
  contract_fingerprint  ToolContractFingerprint (§9)
  mo2_identity          instance + perfil + fingerprint de perfil (reusar
                        `vfs_attestation._profile_fingerprint`)
  config_fingerprint    hash de cfg/settings.json + cfg/modrules.json (PG);
                        preset + resolución + GPU (VRAMr)
  input_fingerprint     modlist/plugins + conjunto de inputs decisores declarado por el tool
  window                [start, end] UTC
```

Método de freshness (sin hashear terabytes):

1. **Born-empty staging**: el output target arranca vacío o solo-PG (ownership).
2. **Manifest de archivos creados** por la corrida (la tool declara/genera; Sky-Claw
   enumera el target al final).
3. **Hashes solo de artefactos decisores** (plugins, diff JSON, DB, markers, logs).
4. **Metadata suficiente** (mtime/size/ventana) para el resto.
5. **Config/version fingerprint** para invalidación.

Casos especiales detectados en P0:

- **PGPatcher cache** (`PGPatcher_UpdateCache.bin`) es estado persistente en el output: si
  se usa `--autostart-update`, la corrida depende de él → entra en `input_fingerprint`;
  el default `--autostart` no debe depender del cache, pero el artefacto se registra.
- **VRAMr.DB** entra en la evidencia de corrida **y** en las reglas de invalidación: una
  DB de corrida anterior puede producir un resultado plausible pero desactualizado.

---

## 17. Síntesis de arquitectura

Revisión adversarial de los componentes que convergen en las investigaciones Arena
(meta-prompt §21). No se acepta por consenso.

| Componente | Veredicto | Responsabilidad | No debe poseer | Notas P0 |
|---|---|---|---|---|
| `ExternalToolSpec` + registry | **ACCEPT** | Descriptor declarativo por tool (key, exe_names, acquisition, install_kind, requires_usvfs, **contract range/fingerprint**) | Estado runtime mutable, versiones vivas, resultados | Ya validado en v3 §5.2; P0 agrega `supported_versions` + `contract_capabilities` |
| `CapabilityGraph` | **MODIFY** | Datos declarativos del grafo pre-LOD (nodos, productores opcionales, convergencia) | Un engine nuevo de grafos; duplicar el DAG en prosa con semántica distinta | Los 9 stages no existen en runtime (v3 §1.1); el grafo de capacidad debe ser una estructura de datos pura testeable, no un motor |
| `MaterialPlanCompiler` | **DEFER** (colapsar en el servicio) | Resolver el camino aplicable y las políticas por versión (VRAMr order, ESM, outputs ON/OFF) | Conocer CLI, logs, rutas internas de cada tool | Sin variabilidad suficiente hoy: una función pura `plan(steps, state, contracts) -> plan` dentro de `pre_lod_service` alcanza; extraer un componente propio solo cuando existan ≥2 consumidores |
| Run DAG (secuencia ordenada con precondiciones) | **ACCEPT** | Orden de nodos + precondiciones + invalidación reportada | Reordenar dinámicamente sin especificación; borrar outputs downstream | Debe incluir la fase **post-PG de sort/reconcile** (§8.4 B) |
| Node state machine | **ACCEPT** | `PENDING/RUNNING/SUCCEEDED/FAILED/SKIPPED/STALE/QUARANTINED` | Persistir estado de stages existentes ni tocar `pipeline_stage` | Los estados se reportan; no renumeran Stage 9 |
| `ManagedOutput` | **ACCEPT** | Identidad, exclusividad, namespace, manifest, rollback | Mover/borrar outputs de otras tools | Se apoya en `output_targets.py` + `DirectoryRollback` existentes |
| `RunEvidence` | **ACCEPT** | RunIdentity + success contract + hashes + logs + attestation | Interpretar logs internos de la tool | El orquestador solo compara hechos declarados, no parsea semántica profunda |
| `Mo2StateTransaction` | **ACCEPT** | Transacción por entradas con CAS + cuarentena + journal | Snapshot completo/restore ciego; `delete_mod_files` | v3 §6.3; agregar `RECOVERY_REQUIRED` y prioridad (§12) |
| `RegisteredVfsToolJob` (handler genérico en el worker) | **ACCEPT** (diseño; implementación en **Operational HOLD**, §20) | Correr una tool allowlisted como hijo del worker, con atestación | Cambiar el bridge; aceptar exe arbitrario | Dos sitios a anclar: `ALLOWED_VFS_TOOL_IDS` y `_default_handlers()` (hoy 2 entradas: `health`, `loot_sort`) |
| Post-PG **plugin reconcile** (nuevo) | **ACCEPT como fase** | Sort/asegurar `PG_X.esp` al final (salvo DynDOLOD), con LOOT si está disponible o placement dirigido | Re-sortear todo el load order sin necesidad; asumir que LOOT cubre `PGPatcher.esp` | Motivado por §8.3; vive en el servicio/orquestador, no en el adapter |
| `ToolContractFingerprint` | **ACCEPT** | Capacidades por versión + invalidación | Duplicar la detección de versión (P3) | §9 |

Responsabilidades cruzadas que se mantienen: `ExternalToolSpec` sin estado mutable;
compiler/plan sin CLI; orquestador sin interpretar logs internos; adapters sin decidir el
DAG global; GUI/LLM sin reglas de dominio. No se crean God Objects.

Stage 9 **no se renombra** ni se renumera. La capacidad se identifica como
`pipeline_capability = "pre_lod_materials"` con `material_step` por nodo (v3 §4.2).

---

## 18. First-cut decision

Comparación (criterios del meta-prompt §23):

| Criterio | FIRST_CUT_A: PGPatcher only | FIRST_CUT_B: PGPatcher + VRAMr |
|---|---|---|
| Soporte upstream | Fuerte: GPL-3, CLI documentada, wiki, releases GitHub | Débil: sin entry point headless documentado; "No other activity permitted without prior written consent" |
| Entry point | `PGPatcher.exe --autostart` (VERIFIED) | Interactivo/BAT; helpers internos sin autorización (B3/B6-L abiertos) |
| Process ownership | Proceso propio + Job Object + atestación VFS | Envuelve BAT con `taskkill` globales (H2): exige reimplementar sin el wrapper, lo que cruza B6-L |
| Licencia | GPL-3 (claro) | Permisos restrictivos; autorización pendiente |
| USVFS | `USVFS_REQUIRED` claro, camino con atestación disponible | `REAL_RIG_REQUIRED` (contradictorio) |
| Success contract | Definible con evidencia fuerte (plugins, diff, cache, ownership) | Más difuso (DB, contadores, marker mtime) |
| Testabilidad | Alta (artefactos deterministas por contrato) | Media (depende de entry point no cerrado) |
| Bloqueos | Ninguno crítico tras P0 | B3 + B6-L abiertos |

**Recomendación P0: `FIRST_CUT_A` (framework + PGPatcher only).** VRAMr pasa a
`Cut B` cuando cierren B3 y B6-L; el endurecimiento de su servicio (P8, tandas 1 y 2 salvo
(g)) puede avanzar en paralelo porque no depende del entry point. ParallaxR/BENDr siguen
en `Cut C` (B6-L).

Esto **revisa** el primer corte de la v3 (PGPatcher + VRAMr). La razón del cambio no es
preferencia: es que el eje de soporte/entrypoint de VRAMr quedó abierto y el primer corte
existe para fijar el framework con el caso mejor evidenciado.

---

## 19. Matriz final de blockers

| Blocker | Before | After P0 | Evidence | Next action |
|---|---|---|---|---|
| B3 — entry point headless soportado de VRAMr | Abierto (launcher identificado, CLI no) | **Sigue abierto**; se confirmó que el flujo oficial es interactivo/User Guide y que v13.4+ se desacopla de la VFS | §6.2; Nexus 90557 changelog | Rig R-VR-1 + consulta al autor; mientras: `MANUAL_ONLY` |
| B4 — `pgtools.exe` vs `PGPatcher.exe` | Abierto | **CERRADO**: `PGTOOLS_IS_NOT_FULL_PATCHER` | §5 (fuente + wiki) | Documentar en el adapter que pgtools no es entry point del pipeline |
| B5-PGPatcher — semántica de `--ignore-mo2vfscheck` | Abierto | **PARCIAL/CERRADO como CHECK_DISABLED**: apaga solo el guard | §6.1 (código) | Rig R-PG-1 solo si se quisiera soportar corridas sin VFS (no recomendado) |
| B5-ParallaxR / B5-BENDr / B5-VRAMr | Abiertos | **REAL_RIG_REQUIRED** (evidencia contradictoria) | §6.2 | R-RS-1 (canary probe) |
| B6-T | Cerrado (v3 §2.2) | Sin cambios | v3 §2.2 | — |
| B6-L — permiso de invocación directa R-suite | Abierto | **Sigue abierto**; permisos documentados; VRAMr exige consentimiento escrito | §7 | Pedido formal al autor; default MANUAL_ONLY |
| B7 — lifecycle de plugins PG | Abierto, bloqueaba P1 | **PARTIAL / CORE CLOSED**: establecido — artefactos y cuándo se omiten; masterlist LOOT para `PG_<N>`/`ParallaxGen`; PG antes de TexGen/DynDOLOD; necesidad de reconcile post-PG; no asumir placement de `PGPatcher.esp`. **No cerrado globalmente: B10 sigue abierto** (output PG stale vs etapas 5–7) | §8, §8.5 | P1 puede congelar el grafo con la fase post-PG de reconcile y la política conservadora §8.5; rig R-PG-4 y R-PG-7 |
| B8 — matriz de contrato 1.2.x↔2.x | Nuevo | **CERRADO como enfoque**: capacidades por versión + fingerprint; contratos conocidos {1.2.0…2.1.1}, **sin rango abierto** | §9 | Implementar `ToolContractFingerprint` en P3/P9; versiones futuras fail-closed hasta registrar y verificar su contrato |
| B9 — ordering VRAMr↔PG por versión | Nuevo | **CERRADO**: ambas policies soportadas; gate en PG≥0.9.8 | §10 | P1 fija el default (POLICY_B propuesto) |
| **B10 — output PG stale vs etapas 5–7** | Nuevo (descubierto en P0) | **ABIERTO / REAL_RIG_REQUIRED hasta R-PG-7**: correr Bash/Synthesis con un output PG stale habilitado puede consumir records PG; regenerar Bash invalida los masters de `PG_<N>.esp`; upstream no se pronuncia | §8.4 C/D/E, §8.5 | P1 fija la política conservadora §8.5 (deshabilitar PG output antes de 5–7 o marcar stale y exigir re-run); rig R-PG-7 |

---

## 20. GO matrix

| Ítem | Veredicto | Condición |
|---|---|---|
| P1 — contrato de lifecycle y capability (docs + tests) | **GO** | Incluir la fase post-PG de reconcile, la política conservadora Stage 5–7 (§8.5) y los campos de contrato por versión en el diseño del grafo |
| PGPatcher adapter | **GO** | Tras P1/P5/P7; con `ToolContractFingerprint` y preflight de 5 condiciones (v3 P9) |
| VRAMr adapter | **NO-GO** | Hasta cerrar B3 y B6-L; P8 (hardening del servicio) sigue GO salvo (g) |
| ParallaxR | **NO-GO** | B6-L |
| BENDr | **NO-GO** | B6-L (y PR-A compartido) |
| P5 — `RegisteredVfsToolJob` / handler genérico | **Architectural GO · Operational HOLD** | Diseño aceptado; implementación en HOLD hasta revalidar lanes concurrentes: estado de #597, #528, trabajo relacionado con #586/broker/VFS y write-set de `main` (§22). Independiente del primer corte; sirve a las 4 tools |

---

## 21. Real-rig experiments still required

| ID | Experimento | Cierra | Evidencia a capturar |
|---|---|---|---|
| R-PG-1 | Dos corridas sobre un perfil chico: (a) lanzado desde MO2 (VFS on), (b) directo con `--ignore-mo2vfscheck`; comparar outputs | Corrección real sin VFS (B5-PG) | árbol de archivos, `ParallaxGen_Diff.json`, logs, `PG_X.esp` CRC |
| R-PG-2 | Corrida con VRAMr output habilitado → capturar exit code y log | Contrato de exit codes (§15) | exit code, línea `critical`, ausencia de artefactos |
| R-PG-3 | Matriz de artefactos: no-op (sin records), zip on/off, update mode, run normal | `expected_artifacts` condicional | listado de output + logs + presencia/ausencia de plugin, diff, cache, zip |
| R-PG-4 | Con `PGPatcher.esp` + `PG_1.esp` presentes, correr LOOT y capturar orden/grupos ESM | Posición real de `PGPatcher.esp` (§8.3) | plugins.txt/loadorder.txt antes/después, log de LOOT |
| R-PG-5 | Regenerar TexGen/DynDOLOD con output PG habilitado; validar `ParallaxGen_Diff.json` | Integración LOD (§8.2) | logs DynDOLOD, diff json, resultados visuales |
| R-VR-1 | Instalar VRAMr en rig: métodos de lanzamiento (MO2 RunLink, directo), opciones no interactivas, árbol de procesos hijos de `Optimise.exe` | B3 + R6 (Job Object) | User Guide observado, cmdlines, árbol de procesos, `VRAMr.DB` |
| R-RS-1 | `VFS_CANARY_TEST_SPEC` con pgtools o un helper R-suite en perfil sandbox | B5 R-suite | nonce, hashes, grandchild probe, estado del perfil |
| R-LIC-1 | Pedido escrito al autor de la R-suite por invocación de helpers | B6-L | respuesta fechada |
| R-PG-6 | Rutas con espacios/Unicode, output en raíz de unidad, disco insuficiente, cancelación a mitad | Riesgos v3 R5 | logs + artefactos + recuperación |
| R-PG-7 | Bash/Synthesis + PG: correr stages 5–7 con output PG habilitado/stale y observar | B10 | plugins resultantes, masters de `PG_<N>.esp`, orden |

Regla: cada condición se verifica **por herramienta por separado** (v3 §9.4).

---

## 22. Recommended PR sequence (solo docs hasta P1)

1. **PR P0 (esta rama):** evidencia P0 + plan v3 restaurado. Cero código. ← actual
2. **P1 — contrato de lifecycle** (docs + tests declarativos): incluye fase post-PG de
   reconcile, la política conservadora Stage 5–7 → PG STALE (§8.5), `pipeline_capability`,
   nodos con `implemented=False` en diferidos, y los campos de contrato por versión en el
   diseño. Sin adapters.
3. **P2 — `ToolReadiness` + registry** (con `known_contracts`/`contract_capabilities`).
4. **P3 — detección de versión** (necesaria para `ToolContractFingerprint`).
5. **P4 — instalación TOOL atómica + instalador PGPatcher** (`AUTO_GITHUB`).
6. **P5 — handler USVFS genérico** (allowlist + handlers, ancla de igualdad). **Operational
   HOLD**: el diseño está aceptado (GO arquitectónico), pero antes de implementarlo deben
   revalidarse #597, #528, el trabajo de broker/VFS (#586) y el write-set vigente de `main`.
7. **P6 — `Mo2ModStateTransaction`** (con `RECOVERY_REQUIRED` y prioridad).
8. **P7 — contrato de adapters** (`_contract.py`, 7 condiciones).
9. **P8 — endurecimiento VRAMr** (tandas 1/2 salvo (g)); puede ir en paralelo desde ya.
10. **P9 — PGPatcher adapter** (primer adapter; `--autostart`, ownership, fingerprint).
11. **P10 — VRAMr adapter**: bloqueado hasta B3/B6-L.
12. **P11 — orquestador + reconcile post-PG**; **P12 — superficies**; **P13 — rig**.

Secuencia de autorización: P0 → P1 (contrato puro) → revalidación de lanes concurrentes →
P5 (solo cuando su write-set esté libre).

Cada PR: pequeño, reversible, con tests y sin refactors cosméticos.

---

## Adversarial Findings

Solo problemas reales detectados al intentar refutar las conclusiones de este documento.

1. **La wiki de PGPatcher afirma "LOOT will sort these plugins correctly", pero la
   masterlist vigente no cubre `PGPatcher.esp`** (solo `ParallaxGen`/`PG_<N>`). Un pipeline
   que confíe en LOOT para ubicar el plugin principal puede quedar con un ESM-flagged
   `.esp` en el grupo `default`. Verificar en rig (R-PG-4) antes de congelar el reconcile.
2. **Exit code 0 en rechazos**: todos los `critical` de main.cpp retornan por `mainRunner`
   sin excepción → `WinMain` devuelve 0. Un adapter que sólo mire el exit code reportaría
   éxito en un preflight fallido. El success contract debe exigir log + artefactos.
3. **La wiki dice que `PGPatcher.esp` "is always flagged ESM"**, pero desde 1.3.0 existe
   `--no-esm` y `Finalize` acepta modo 2. La doc upstream está atrás del binario:
   el contrato real es "ESM por default, configurable".
4. **Las traducciones de PGPatcher siguen hablando de `user.json`** mientras el código usa
   `cfg/settings.json`. No usar strings de UI como fuente de contrato (regla de método).
5. **El cache de update es estado persistente dentro del output** (`PGPatcher_UpdateCache.bin`):
   corridas con `--autostart-update` dependen de artefactos previos; la freshness de §16
   debe tratarlo como input, no como output.
6. **El check de DynDOLOD/TexGen sólo verifica `dyndolod.esp` activo**, aunque el mensaje
   hable de "DynDoLOD and TexGen outputs". Un output de TexGen habilitado sin `dyndolod.esp`
   podría no ser detectado por el binario → el preflight de Sky-Claw debe cubrir ambos.
7. **`--ignore-mo2vfscheck` no virtualiza nada**; que el mapeo de mods se haga desde disco
   hace *plausible* un run sin VFS, pero no lo prueba (overwrite, loose winners finos).
   Soportarlo como camino oficial sería asumir UNKNOWN.
8. **B10 (nuevo):** correr Bash/Synthesis con output PG stale habilitado puede incorporar
   records PG a esos plugins; y regenerar Bash invalida los masters de `PG_<N>.esp`.
   Upstream no documenta esta interacción. Es un vector de corrupción silenciosa del load
   order que P1 debe modelar (política de invalidación).
9. **El límite de 254 masters lo maneja PG partiendo plugins**, pero cada `PG_<N>.esp` suma
   `PGPatcher.esp` como master. Los tests/invariantes de límite de Sky-Claw deben contemplar
   el patrón multi-plugin, no un único plugin generado.
10. **Ownership del output y `meta.ini`**: la lista de archivos permitidos incluye `meta.ini`
    (MO2) y el zip pre-output; un "solo-PG" implementado ingenuamente por lista blanca
    podría abortar sobre un output válido. El adapter debe usar la lista exacta de la
    versión detectada (VERSION_DEPENDENT menor, verificar en P9 con 2.1.1).
11. **VRAMr "order doesn't matter" convive con un check duro**: si VRAMr corre después de
    PG, su output queda habilitado; un re-run posterior de PG sin deshabilitar VRAMr
    fallará con `critical` (exit 0). La UI/LLM deben comunicar la precondición, no sólo
    "orden libre".
12. **La R-suite comparte framework pero no necesariamente comportamiento**: BENDr
    "built on the same framework as VRAMr" no autoriza a extrapolar VFS, markers ni
    proceso hijo de una a otra. Se mantienen como items de rig independientes.

---

## Apéndice A — Provenance de la evidencia previa (T2) usada sin re-inspección

Los siguientes artefactos **no estuvieron disponibles en esta sesión**. Se citan como
provenance del operador (v3 §2) y **no** se declaran re-inspeccionados:

- ParallaxR v3.0318: `ParallaxR.bat` SHA-256 `72d9e19156d8367ff3c884d45685dca37df3e2f72cd0bd94d57de258425e53ed`.
- BENDr v3.0331: `BENDr.bat` SHA-256 `bd6f78874ae16d567beee4dd4a5d171c2b4ae29cb4a11e120624084adfb64fc0`.
- VRAMr v16.0310: zip `b36b184170c0c68a8ed1130f335abc9e0ad9edbe15416f656559b755626052a9`,
  `script.bat` `ef085ffdd914d7bf30e3e52ad30d18063ac6e19e0308a1b00686b7a01c809581`,
  `BSA.exe` `cbb582794268484848b6ed71dd3fa14f2f80bd2224def38eab0815bf46c3ddb5` (T1 de la sesión v3).
- PGPatcher 1.2.0: zip `9981cd966ca512ff2ac6d0006537fa937da74a3ac4e7f6dc973c996a0697436c`,
  `PGPatcher.exe` `7027f055…`, `pgtools.exe` `cc6493b6…`, `PGLib.dll` `b6ddcca2…`.
- Auto Parallax 1.0.27: zip `1e67a4df3919c67f94ca94d9545b2a95add708592a240f377173cfd25d9ab2ef` (runtime SKSE, no nodo).

Ningún binario, zip, BAT íntegro ni código GPL extenso se incorporó a este documento ni al
repo (política §26 del meta-prompt; v3 §3.5).
