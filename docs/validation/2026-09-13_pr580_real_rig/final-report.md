# REAL RIG — PR-2 DynDOLOD external staging (aceptación final)

- **PR:** #580 (DRAFT) — `feat/dyndolod-pr2-external-staging`
- **tested code HEAD:** `ea3f28fbff064a2917690d3851d9a311a388da77`
- **base (origin/main):** `5313cf2b05b6b4f03d3483c9d8e6b50a3d2e5177`
- **documentation SHA (harness ejecutado):** `51f334674fcc55c785e700fd6879bcd6dd578af6` (ver NOTA POST-REVIEW)
- **Fecha:** 2026-09-13 (aprox. 08:11–08:47 −03:00)
- **Worktree:** `C:\Worktrees\Sky-Claw-pr2` (limpio antes y después; **no se modificó código**)
- **Perfil MO2:** `SkyClaw-PR2-Rig` (selected_profile verificado)
- **MO2 instance:** `G:\Modding\MO2\SkyrimSE` · **Game:** `G:\Modding\Skyrim_Runtime_1.6.1170`
- **External work root:** `E:\Sky-Claw T5 Rig\PR2 Work` · **binding_id:** `d9caaf53-2124-4165-8b7e-72e32237cf94` (intacto, sin recrear)
- **Evidencia:** `E:\Sky-Claw T5 Rig\evidence\PR580-ea3f28fb\2026-09-13_081146-REAL-RIG\`

## Veredicto

**REAL RIG PASS — PR-2 ACCEPTANCE 10/10, READY FOR FINAL REVIEW**

T5-v2 10/10. Todos los criterios demostrados sobre ESTE HEAD con ejecución real a través del
boundary productivo de Sky-Claw.

## Tabla T5 1–10

| criterio | evidencia | PASS/FAIL |
|---|---|---|
| 1. binarios reales | `02-binaries-sha256.txt`: TexGenx64.exe `0939BC8F…`, DynDOLODx64.exe `B67625EB…`; log DynDOLOD `Program: …DynDOLODx64.exe, Version: 3.0.0.209` | **PASS** |
| 2. launch por Sky-Claw | `texgen-command.txt` / `dyndolod-command.txt` (argv del boundary); log `Started by: …python.exe`; fase corrida por `servicio.execute` | **PASS** |
| 3. paths con espacios | argv con `"E:\Sky-Claw T5 Rig\PR2 Work\…"` citados; spawn y escritura exitosos en rutas con espacios | **PASS** |
| 4. stale preset real | `texgen-stale-observed.txt` (GUI mostró `E:\Sky-Claw T5 Rig\DECOY PR2\TexGen\`) y `dyndolod-stale-observed.txt` (GUI mostró `…\DECOY PR2\DynDOLOD\`); presets `DynDOLOD_SSE_TexGen.ini` y `DynDOLOD_SSE_Default.ini` ajenos al managed root | **PASS** |
| 5. `Using Output Path` exacto | `texgen-log.txt` → `Using Output Path: E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\TexGen\`; `dyndolod-log.txt` → `Using Output Path: E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD\` | **PASS** |
| 6. output físico real | TexGen 1362 archivos en `…\PR2 Work\DynDOLOD\TexGen\textures\…`; DynDOLOD 5105 archivos / 719 MB en `…\PR2 Work\DynDOLOD\DynDOLOD\` (`DynDOLOD.esm`, `DynDOLOD.esp`, `Occlusion.esp`, `meshes\`, `textures\`) | **PASS** |
| 7. zero diversión | `decoy-after.txt`: ambos decoys conservan SOLO `DO_NOT_WRITE_HERE.txt` (mismo sha256); `legacy-after.txt`: legacy ausente; `final-tree.txt`: sin archivos directos en el family root | **PASS** |
| 8. terminación / empaquetabilidad | TexGen: `Exit TexGen` (`texgen-termination.txt`); DynDOLOD: `Save and Exit` (`dyndolod-log.txt`: `User says "Save and Exit"` + `Saving …DynDOLOD.esm/esp/Occlusion.esp`). Clasificación Sky-Claw: TexGen → `needs_deployment=True` (corte de handoff) y luego `success=True` en resume; DynDOLOD → `success=True` | **PASS** |
| 9. dos packages disjuntos | `packaging-texgen.txt`, `packaging-dyndolod.txt`, `disjointness.txt`: `TexGen Output` = `{meta.ini, textures}` sin plugins de DynDOLOD; `DynDOLOD Output` = `{DynDOLOD.esm, DynDOLOD.esp, Occlusion.esp, meshes, meta.ini, textures}` sin árbol TexGen | **PASS** |
| 10. handoff TexGen→DynDOLOD | `handoff-texgen.json` (state=`awaiting_deployment`, 1362 archivos, digest `b6ecf021…`); materialización registrada (`materialization-before/after.txt`); validador real `handoff-visibility-verify.txt` → **VISIBILIDAD_OK**; `dyndolod-log.txt` de ESTA sesión con **0** `billboard(s) not found` / `No TexGen output detected` y TreeLOD generado | **PASS** |

## Cronología verificada

### Precheck productivo
- `04-preflight.txt`: `status=yellow`, `blocks_mutations=False`, `vfs=green`, `vfs_visibility=green`,
  `write_permissions=green`, `overwrite=yellow` (141 residuales, 70 plugins; no bloqueante).
- Workspace resuelto: `root=E:\Sky-Claw T5 Rig\PR2 Work`, `estado=C_BINDING_COMPATIBLE`,
  `binding=d9caaf53-…`.

### Fase A — TexGen real (boundary productivo, GUI asistida)
- `Overwrite sucio: 141 archivo(s), 70 plugin(s)` (aviso PowerShell, no bloqueante).
- GUI Alpha-209 abrió en `TexGen Options` mostrando el **Output stale del decoy**.
  El log de arranque de TexGen ya declaraba el `-o:` administrado (criterio 4/5 satisfecho aun sin corrección).
- Corrección asistida **solo del campo Output** vía `WM_SETTEXT` → readback verificado
  (`texgen-output-correction.txt`). **Ninguna otra opción modificada.**
- Start (BM_CLICK) → generación 47 s → `[00:47] TexGen completed successfully`.
- Terminación elegida: `Exit TexGen` (NO `Zip and Exit`, que borra la carpeta cruda; texto del propio diálogo citado).
- Packaging: `TexGen Output` = 1363 archivos / 138.457.903 B, Data-relative `{meta.ini, textures}` (sin wrapper `TexGen\`).
- Root crudo `…\PR2 Work\DynDOLOD\TexGen` → 0 archivos tras el rollback (born-empty del contrato).

### Handoff TexGen → DynDOLOD (T5 #10)
- El boundary **cortó antes de DynDOLOD** (`needs_deployment=True`): Sky-Claw corre standalone y no ve el mod
  bajo `<mo2>/mods` en el Data físico. Comportamiento **por diseño** (`docs/operations/deployment_standalone_usvfs.md`).
- Handoff certificado bajo lease: `handoff-texgen.json`, `state=awaiting_deployment`, `handoff_id=1`,
  `expected_files=1362`, `expected_bytes=138.457.806`, digest `b6ecf021…`.
- **Intervención asistida** (aprobada explícitamente por el operador): materialización
  `mods\TexGen Output\textures` → `_rig_test\data\textures` (robocopy `/E /IS`; NO se borró nada de Data).
  Registrada con hashes antes/después.
- Verificación con el validador real de Sky-Claw: **VISIBILIDAD_OK** (1362 archivos idénticos byte a byte).

### Fase B — DynDOLOD real (mismo boundary, `run_texgen=False`)
- Prompt de arranque benigno `DynDOLOD.DLL NG not found` → `Ignore`.
- En `Advanced` la GUI cargó el **preset Default stale** (Output → decoy). Corrección asistida solo del campo Output
  (`dyndolod-output-correction.txt`) + preset `Medium` (log: `Loading Medium settings`). OK.
- Generación ~4 min: `DynDOLOD plugins generated successfully`, `Occlusion.esp completed successfully`,
  LODGen de Tamriel/Blackreach/etc.
- Terminación elegida: **`Save and Exit`** (NO `Save, Zip and Exit`, que borra la carpeta; NO `Exit DynDOLOD`, que no guarda).
- `dyndolod-result.txt`: `success=True`, `needs_deployment=False`, `duration=455.5 s`, `dyndolod_mod_path=…\mods\DynDOLOD Output`.
- Packaging: `DynDOLOD Output` = 5106 archivos / 719.336.534 B, plugins `DynDOLOD.esm, DynDOLOD.esp, Occlusion.esp`.

### Family root final
```
E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD
├── TexGen\      (0 archivos — born-empty restaurado)
└── DynDOLOD\    (5105 archivos / 719.336.435 B — salida cruda del tool)
```
Sin archivos directos en el family root (namespace). Sin residuo `*.rollback-*`.

## Warnings clasificados (no fatales, no atribuibles al pipeline)
- `DynDOLOD.DLL from DynDOLOD DLL NG and Scripts not found!` → plugin dinámico SKSE ausente en el rig; el propio diálogo ofrece Ignore. **Ajeno al handoff TexGen.**
- `<Error: File not found …\_rig_test\SkyrimSE.exe>` → ruta del ejecutable del juego no montada en el rig; DynDOLOD carga los plugins igual. Presente también en la corrida exitosa previa (2026-09-10).
- `<Error: Deleted reference …>` (Update/Dawnguard/HearthFires) → datos del rig; `Large reference bugs workarounds: disabled`. Sin relación con TexGen.
- `Warning: Could not find save path`, `Can't find Skyrim - Patch.bsa`, grupos vacíos → benignos; presentes en corridas previas.

## Puntos no verificados / límites del rig
- **No** se marcó el PR como Ready ni se mergeó.
- El rig usó un wrapper de harness (`run_phase_bounded.py`) que ejecuta `run_phase.py` **sin modificarlo** y sale con
  `os._exit` tras `main()`. Motivo medido con `faulthandler`: el cierre del harness deja vivo un hilo worker de
  `aiosqlite` (no-daemon) que bloquea `threading._shutdown`; `asyncio.run(main())` SÍ retorna y no queda ninguna lease
  (etapa 9 y pipeline en 0 filas). No es un defecto del código producto.
  El follow-up post-review cerró el `DistributedLockManager` que aquel cierre dejaba abierto
  (ver NOTA POST-REVIEW); `os._exit` se conserva en el wrapper como boundary defensivo.
- La materialización TexGen→Data del rig **es un paso de operador** exigido por el contrato; se ejecutó tras
  aprobación explícita y NO se usó para ocultar el gate: el gate cortó primero y luego verificó byte a byte.
- `LODGenx64Win7.exe` ausente en el rig (no usado).
- SHA documental aún inexistente al momento de esta corrida; el commit que preserva el harness
  ejecutado es `51f334674fcc55c785e700fd6879bcd6dd578af6` (ver NOTA POST-REVIEW).

## POST-REVIEW HARNESS NOTE

- El real rig y sus resultados fueron producidos con la versión del harness
  preservada en el commit `51f334674fcc55c785e700fd6879bcd6dd578af6`.
- La revisión posterior detectó dos defectos de teardown/lifecycle exclusivos
  del harness de evidencia (no del código producto):
  1. `DistributedLockManager` no cerrado explícitamente (conexión `aiosqlite`
     propia retenida).
  2. `limpiar(ctx)` no garantizado mediante `finally` para todo lo ocurrido
     después de `preparar()`.
- El follow-up corrige únicamente el teardown del harness: el `lock_manager`
  queda en el `ctx` de `preparar()` y se cierra en `limpiar()`, y la secuencia
  posterior a `preparar()` (inyección del runner, argv, evidencia de comando,
  tree-before, `execute`, evidencia de excepción/resultado/packaging/handoff)
  queda bajo un único `try/finally` que ejecuta `await limpiar(ctx)` una sola
  vez.
- No modifica código productivo, argv, output roots, packaging, handoff,
  filesystem observado ni resultados del rig.
- No se requiere repetir TexGen/DynDOLOD por estos fixes de teardown.

## Divergencia de protocolo registrada (transparencia)
El protocolo indicaba "NO copiar archivos manualmente a Data para maquillar el handoff". El contrato REAL de
Sky-Claw exige que el **operador** materialice el mod preservado (Sky-Claw no copia por diseño). Se detuvo, se
consultó al operador, que aprobó "materializar y seguir"; se registró como intervención asistida. No se alteró
ningún gate ni verificación.
