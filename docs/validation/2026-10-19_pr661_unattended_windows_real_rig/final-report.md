# #661 â€” Rig real Windows de la etapa 9 DESATENDIDA (TexGen + DynDOLOD)

> **SesiÃ³n:** `DYNDOLOD-661-UNATTENDED-WINDOWS-ACCEPTANCE` Â· **Fecha:** 2026-10-19 Â· **SO:** Windows 10 Pro 19045
> **Rama:** `validation/dyndolod-661-unattended-windows-rig` Â· **Base:** `origin/main` = `6abf20d06d908a6c3bc8f4648cb4cd7bb4111c37`
> **Worktree:** `<WORKTREE>`
> **Rig aislado:** `C:\SkyClaw_Rigs\SW661 Unattended`
>
> **Veredicto standalone:** `UNATTENDED_STANDALONE_PASS` (alcance declarado en Â§12)
> **Veredicto MO2/USVFS:** `UNATTENDED_BROKERED_NOT_TESTED`
> **`DYNDOLOD_661_REAL_RIG_PASS`:** **NO se declara** (exige ambos modos y el brokered no se ejecutÃ³)
> **#661:** no se cierra. No se mergea nada desde esta sesiÃ³n.

## 1. QuÃ© se certificÃ³ y con quÃ© cÃ³digo

Se ejecutÃ³ la cadena completa del pipeline productivo sobre los binarios reales Alpha-209,
en un rig fÃ­sicamente aislado, **sin que ningÃºn humano pulsara un botÃ³n local**:

```text
execute(interaction_mode="desatendido")
  â†’ preflight green â†’ aprobaciÃ³n remota de configuraciÃ³n
  â†’ spawn TexGen â†’ modal conocido â†’ aprobaciÃ³n remota â†’ Ignore (UIA)
  â†’ convergencia del Output (rancio â†’ administrado) + readback
  â†’ Start (UIA, exactly-once) â†’ generaciÃ³n â†’ diÃ¡logo terminal â†’ Exit TexGen (UIA, exactly-once)
  â†’ post-check + packaging + handoff preservado
  â†’ [materializaciÃ³n standalone del Ã¡rbol de TexGen en Data, documentada]
  â†’ spawn DynDOLOD â†’ modal conocido â†’ aprobaciÃ³n remota â†’ Ignore (UIA)
  â†’ Advanced >>> (UIA) â†’ reconvergencia del Output â†’ OK/Begin (UIA, exactly-once)
  â†’ generaciÃ³n (269,4 s) â†’ diÃ¡logo terminal â†’ Save and Exit (UIA, exactly-once)
  â†’ post-check + packaging + handoff completed + preset restaurado por SHA
```

CÃ³digo certificado: el de `origin/main` en `6abf20d0` (PR #740 mergeado como `3e89ff3a`, contrato Data
Path de #739, garantÃ­as de cancelaciÃ³n/ownership de #742). **No se modificÃ³ una sola lÃ­nea de cÃ³digo**
durante el rig: el diff de esta rama es sÃ³lo documentaciÃ³n/evidencia.

## 2. Identidad e insumos

| Elemento | Valor |
|---|---|
| `origin/main` al iniciar y al cerrar | `6abf20d06d908a6c3bc8f4648cb4cd7bb4111c37` (sin movimiento) |
| HEAD del worktree | `6abf20d0` (branch nueva desde `origin/main`) |
| `TexGenx64.exe` | 35.794.432 B Â· v3.0.0.209 Â· SHA256 `0939BC8F8CBAE2E1B38F17D56FECD1B7A941DADE0554E944886BD7F5C4807A62` |
| `DynDOLODx64.exe` | 35.794.432 B Â· v3.0.0.209 Â· SHA256 `B67625EB7815111BA9AC232C626FF88B07BB64FF30FF5183C5A04B5F045C3BD0` |
| `SkyrimSE.exe` | v1.6.1170.0 Â· SHA256 `C434208894F07F604B852F29B8EDC3A58C4DE63DE783373733E72B2B73F33BE9` |
| Python del rig | 3.11.9 Â· venv dedicado con editable install del **worktree de la sesiÃ³n** Â· comtypes 1.4.17 |
| Preset TexGen | 1.705 B Â· SHA256 `75C4AA4A5746D24F32BB2BE455C5EE13B0DB1F87E3412A9EB463C90F5E87BA8E` (OutputPath rancio de #739, a propÃ³sito) |
| Preset DynDOLOD | 24.061 B Â· SHA256 `E728F1F36171C50F4DB386AC56DB24B4DFCE726261A2D55911EE4A8736209DC4` (del operador, `Preset=Medium`, OutputPath rancio de T5 Rig) |

Los tres binarios coinciden con los builds medidos del contrato (`CONTRATOS_DE_UI`), por eso el modo
desatendido pudo arrancar (`UNATTENDED_BUILD_UNSUPPORTED` era el corte posible y no ocurriÃ³).

## 3. Aislamiento (Fase B)

Rig exclusivo `C:\SkyClaw_Rigs\SW661 Unattended`, sin junctions/symlinks (0 reparse points, verificado).

| Recurso | Estado |
|---|---|
| `game\Data` | 22.556 archivos / 18.329.994.075 B â€” composiciÃ³n ya aislada en #739 (Stock 1.6.1170 + DynDOLOD Resources SE) |
| `tools\DynDOLOD` | Alpha-209; `Logs/`, `Summary/` y `Edit Scripts\Export/` excluidos al materializar |
| `Game INI` | copias byte-exactas de `SkyClaw-PR2-Rig` (SHA `4D7C9D67â€¦`, `7406F512â€¦`) |
| MO2 de prueba | `mo2-install` + `mo2-instance` con perfil nuevo `UnattendedRig` (5 masters, 0 light) |
| `mods\TexGen Output` / `mods\DynDOLOD Output` | decoys VACÃOS antes de las corridas |
| `Work Root` | root externo administrado, binding `5b14cd20-61c1-4528-aabf-54115e072605`, estado `C_BINDING_COMPATIBLE` |
| Fuentes | `C:\SkyClaw_Rigs\PR739 Data Path` **sÃ³lo lectura**; conteos/bytes/mtime sin cambios; G:\Modding, G:\SteamLibrary, C:\steam: conteos/bytes idÃ©nticos pre/post |

**Actividad de terceros observada (no atribuible a esta sesiÃ³n):** MO2 de producciÃ³n con el perfil
`<PERFIL_DEL_OPERADOR>` (17:32â€“17:45) y Steam actualizando Skyrim en `G:\SteamLibrary\steamapps\downloading`
(17:45:28-29). No hay forma de afirmar inmutabilidad global de la mÃ¡quina; sÃ­ se afirma que este rig
no escribiÃ³ en esas rutas y que las fuentes que usÃ³ no cambiaron durante la ventana.

## 4. Fase C â€” backend COM real medido (antes de generar)

**MÃ©todo:** el backend productivo `dyndolod_uia_driver_windows.BackendUIAWindows` y el ejecutor productivo
`EjecutorDePasoPorHelper` (proceso descartable por paso), contra las ventanas reales.

| Punto | MediciÃ³n |
|---|---|
| PID y ruta canÃ³nica | ventana `TfrmMain` localizada por `UIA_ProcessIdPropertyId` == pid del proceso spawneado (atribuciÃ³n estructural, no por tÃ­tulo) |
| Ventana contractual | `TfrmMain` Â· tÃ­tulo `TexGen 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE)` / `DynDOLOD 3.0 Alpha-209 x64 - â€¦` |
| Control Output | Ãºnico `Edit`/`TEdit`, habilitado (`AutomationId` = HWND por instancia) |
| Mecanismo | `LegacyIAccessiblePattern.SetValue` â†’ **`S_OK`** + readback **exacto**; nunca se probÃ³ otro mecanismo |
| Botones | `Start` (`TButton`), `Advanced >>>`, `OK`, `Cancel`, `Help`, presets `Low/Medium/High` â€” todos `TButton` |
| DiÃ¡logos | `#32770` con `MainInstruction`; `Ignore` = `CCPushButton`/`CommandButton_5`; terminal `Exit TexGen` = `CommandButton_7`, `Save and Exit` = `CommandButton_6` |
| **RIESGO 1 (Output rancio)** | **confirmado**: la GUI precarga el `OutputPath=` del preset (`â€¦\PR739 Data Path\Work Root\DynDOLOD\TexGen`), distinto del `-o:`. El driver lo detecta, escribe por el mecanismo del contrato y el readback posterior coincide canÃ³nicamente. Cero escrituras a la ruta vieja. |
| **RIESGO 2 (ventana sin ProcessId)** | **no observado**: todas las ventanas del pid tuvieron ProcessId legible; el modal es descendiente de `TfrmMain`. El backend sÃ³lo acepta ventanas del pid pedido. |
| Modal DLL NG | instrucciÃ³n exacta `DynDOLOD.DLL from DynDOLOD DLL NG and Scripts not found!`; **estable â‰¥100 s sin interacciÃ³n** (5 relecturas) y 10/10 observaciones frescas `MODAL_CONOCIDO` |

## 5. Aprobaciones HITL (Fase D)

El adaptador es el **productivo**: `AprobadorDesatendidoHITL` sobre `HITLGuard`, categorÃ­a
`dyndolod_desatendido` (no auto-aprobable). El operador concediÃ³ **8 aprobaciones** explÃ­citas:

| Pedido | Tipo | DecisiÃ³n |
|---|---|---|
| `dyndolod-desatendido-77ec7467df0b` | configuraciÃ³n | **DENEGADA** por el guard (el transporte escribiÃ³ el `.resp` con BOM UTF-8 y no parseÃ³) â†’ fail-closed, sin spawn ni TX |
| `dyndolod-desatendido-0d8699165a6f` | configuraciÃ³n | APROBADA |
| `dyndolod-modal-5116dfdd2c9a` | modal `texgen_stitched_lod_previo` | APROBADA â†’ `Ignore` pulsado |
| `dyndolod-desatendido-e99827c3fc43` | configuraciÃ³n | APROBADA |
| `dyndolod-desatendido-ef4eae792b10` | configuraciÃ³n DynDOLOD | APROBADA |
| `dyndolod-modal-d11ad0883045` | modal `dyndolod_dll_ng_ausente` | APROBADA (el invoke fallÃ³ por hueco UIA transitorio, ver F2) |
| `dyndolod-desatendido-ec2bf1a93878` | configuraciÃ³n (retry) | APROBADA |
| `dyndolod-modal-86bf9e2fd920` | modal `dyndolod_dll_ng_ausente` (retry) | APROBADA â†’ `Ignore` pulsado |

**Telegram: NO CONFIGURADO en esta mÃ¡quina** (`~/.sky_claw/config.toml` sin `[telegram]`, sin token en
keyring; `HITLGuard` sin canal deniega al instante). La ÃšNICA sustituciÃ³n respecto del composition root
productivo es el **transporte** del canal: el `notify_fn` del guard escribe el pedido a
`state/hitl/<id>.json` y la decisiÃ³n del operador llega por el chat de la sesiÃ³n. El adaptador, la
categorÃ­a, el timeout (900 s) y el fail-closed son los productivos. **El flujo Telegram no se validÃ³
(`telegram_verificado=false`)** â€” es un lÃ­mite declarado, no un PASS de esa pieza.

**Etapas 6â€“8:** el rig aislado no tiene Bashed Patch / Synthesis.esp / grass cache y **no se fabricaron**.
El guard SOP Â§5.2 vive en la superficie remota (`generate_lods`) y en `GenerateLodsStrategy`; esta sesiÃ³n
invocÃ³ el **servicio** directamente (el mismo boundary que el rig de #739), asÃ­ que ese guard **no se
ejerciÃ³**. Queda cubierto por tests, no por este rig.

## 6. Presets (Fase E)

| Corrida | Antes | DespuÃ©s | RestauraciÃ³n |
|---|---|---|---|
| TexGen (intento modal) | `75c4aa4aâ€¦` (1.705 B) | `75c4aa4aâ€¦` (1.705 B) | byte-exacta |
| TexGen (intento emitido) | `75c4aa4aâ€¦` (1.705 B) | `75c4aa4aâ€¦` (1.705 B) | byte-exacta |
| DynDOLOD (intento 1, abortado) | `e728f1f3â€¦` (24.061 B) | `e728f1f3â€¦` (24.061 B) | byte-exacta |
| DynDOLOD (acceptance) | `e728f1f3â€¦` (24.061 B) | `e728f1f3â€¦` (24.061 B) | byte-exacta + `preset restaurado y verificado` en el log |

La restauraciÃ³n se verificÃ³ incluso en las corridas fallidas â€” el contrato `PRESET_RESTORE_FAILED` no
se disparÃ³ en ninguna. **LÃ­mite declarado:** la configuraciÃ³n de worldspaces se aprueba por la huella del
preset en disco, no por lectura UIA de esos controles; `WORLDSPACE_MISMATCH` no existe en este build.

## 7. Corridas reales (Fase F)

| # | Objetivo | Resultado | Detalle |
|---|---|---|---|
| T1 | TexGen real con modal stitched aprobado | FAIL (post-check) | TexGen **generÃ³ 1.362 archivos / 138 MB**, `TexGen completed successfully`, `Exit TexGen` exactly-once, exit 0 â€” pero la lÃ­nea `Error: Found stitched object LOD texturesâ€¦` del modal se clasifica terminal â‡’ el pipeline falla y hace rollback. **DEFECTO F1** (pre-existente, no del modo desatendido). |
| T2 | TexGen real con Data limpia | PASS del tool + corte de handoff | `Start â†’ OK (invocado=True hresult=S_OK)`, generaciÃ³n 67,1 s, `texgen_exit â†’ OK`, packaging OK, preset restaurado. Corte documentado: standalone no ve el mod â‡’ `needs_deployment=true`, `TexGen Output` preservado y certificado bajo lease (TX 2 committed, handoff `AWAITING_DEPLOYMENT`). |
| D1 | DynDOLOD real con modal DLL NG | FAIL (fail-closed) | modal detectado y aprobado; el paso `INVOCAR` encontrÃ³ `CARGANDO` (hueco transitorio de observaciÃ³n UIA), `invocado=False` â‡’ `UIA_CONTROL_MAP_MISMATCH`, sin mutaciÃ³n, sin reintento. **DEFECTO F2** (intermitente). |
| D2 | DynDOLOD real, retry con el mismo cÃ³digo | **PASS** | `dyndolod_modal_ignore â†’ OK S_OK` Â· `dyndolod_advanced â†’ OK S_OK` Â· `dyndolod_begin_ok â†’ OK S_OK` Â· generaciÃ³n 269,4 s Â· `dyndolod_save_exit â†’ OK S_OK` Â· `success=True assisted=False interaction_mode=desatendido errors=[]` Â· exit code 0 Â· artefactos y packaging OK Â· preset restaurado. |

## 8. AceptaciÃ³n del standalone (Fase G)

| CondiciÃ³n | Estado | Evidencia |
|---|---|---|
| TexGen real ejecutado | ✅ | T2: 1.362 archivos, 138.457.821 B, `return_code=0`, marcador de completitud presente |
| DynDOLOD real ejecutado | âœ… | D2: 5.105 archivos, 722.206.676 B en el root; `DynDOLOD.esm/esp`, `Occlusion.esp` |
| Backend COM productivo usado | âœ… | Â§4 (sonda) + `state/run.log` (`UIA desatendido: â€¦`) |
| Start/OK sin operador local | âœ… | `texgen_start â†’ OK S_OK`; `dyndolod_advanced â†’ OK S_OK`; `dyndolod_begin_ok â†’ OK S_OK` |
| Acciones terminales sin operador local | âœ… | `texgen_exit â†’ OK S_OK`; `dyndolod_save_exit â†’ OK S_OK`; el log de la herramienta dice `User says "Ignore"` / `User says "Save and Exit"` |
| ConfiguraciÃ³n aprobada por canal remoto | âœ… (canal de sesiÃ³n) / âš ï¸ Telegram NO | Â§5 |
| Presets restaurados | âœ… | Â§6 |
| Output en root administrado | âœ… | `Work Root\DynDOLOD\{TexGen,DynDOLOD}` |
| Decoys intactos / packaging | âœ… | `mods\TexGen Output` 1.363 archivos; `mods\DynDOLOD Output` 5.106 archivos |
| Data Path correcto | âœ… | eco del binario `Using Skyrim Special Edition Data Path: â€¦\SW661 Unattended\game\Data\` == `-d:`; idem Output |
| Handoff vÃ¡lido | âœ… | `Visibilidad de TexGen confirmada: 1362 archivo(s) idÃ©nticos`; `deployment_handoffs.state=completed` |
| Exit code 0 | ✅ | `state/run.log`: `TexGen finalizado: return_code=0` y `DynDOLOD finalizado: return_code=0` |
| Marcador de completitud de ESTA corrida | âœ… | `TexGen completed successfully` (slice de esta corrida) Â· `DynDOLOD plugins generated successfully` + `Occlusion.esp completed successfully` |
| Ausencia de lÃ­neas terminales | âœ… | clasificaciÃ³n del runner sobre los bytes de esta corrida |
| Artefactos esperados vÃ¡lidos | âœ… | SHA-256 en `packaging-summary.json` |
| Sin procesos residuales | âœ… | consulta post-cierre vacÃ­a |
| Locks y leases consistentes | âœ… | TX 2 y TX 4 `committed`; handoff `completed`; workspace binding `C_BINDING_COMPATIBLE` |
| Journal y snapshots verificables | âœ… | `journal-summary.json` |

**CategorÃ­as de LOD producidas:** object/static LOD (texturas de objeto, billboards, `DynDOLOD.esm/.esp`,
`Occlusion.esp`). **Dynamic LOD NO generado**: `DynDOLOD.DLL` de *DynDOLOD DLL NG and Scripts* no estÃ¡
instalado (misma dependencia faltante que documentÃ³ #739). El warning es no terminal y se registra; no se
exige esa caracterÃ­stica como si estuviera disponible.

**Warnings no terminales registrados:** 137 lÃ­neas de ruido de dominio (deleted references de
Update/Dawnguard/HearthFires, `Unresolved FormID`, `Can't find Skyrim - Patch.bsa`, grupos vacÃ­os de
xEdit) + `Could not find save path`. `DynDOLOD.DLL not found` (ver arriba). Ninguno aborta la corrida y
la taxonomÃ­a del runner los clasifica como no-fatales.

## 9. Pruebas negativas (Fase I)

Detalle completo y caso por caso en [`negative-tests.json`](negative-tests.json). Resumen:

- **Medidas en el rig:** rechazo/ilegible de la aprobaciÃ³n â†’ `UNATTENDED_CONFIGURATION_NOT_APPROVED` sin
  mutaciÃ³n (N01); output rancio detectado y corregido (N05); handoff no visible â†’ corte antes del spawn
  (N03); hueco UIA transitorio â†’ `UIA_CONTROL_MAP_MISMATCH` sin invocar (N04); modal conocido + Ignore
  aprobado en dos herramientas (N02a/N02b).
- **Cubiertas por tests deterministas** (no se fuerzan contra los binarios para no cruzar fronteras):
  SHA desconocido, preset ausente/desincronizado, readback incorrecto, Begin deshabilitado, desenlace
  ambiguo, helper colgado, modal desconocido, restore fallido, cancelaciÃ³n/lease.
- **No ejecutadas:** etapas 6â€“8 (N17).

Ninguna prueba negativa escribiÃ³ en el perfil activo ni en un destino desconocido.

## 10. Quality gates y CI

Ejecutados sobre el worktree (`6abf20d0`, sin cambios de cÃ³digo; detalle crudo en
[`test-results.txt`](test-results.txt)):

| Gate | Resultado |
|---|---|
| `ruff check sky_claw/ scripts/ tests/` | **VERDE** (exit 0) |
| `ruff format --check sky_claw/ scripts/ tests/` | **VERDE** (908 archivos) |
| `mypy sky_claw/ scripts/` | **VERDE** (377 archivos; caveat: ~30 mÃ³dulos con `ignore_errors`) |
| `git diff --check` / `git status --porcelain` | limpio |
| Suite focalizada DynDOLOD/UIA/presets/handoff/locks | **1829 passed, 13 skipped, 0 failed** |
| Suite completa | (ver `test-results.txt`) |

**CI de `origin/main` 6abf20d0: ROJO â€” defecto de PRODUCTO, ajeno a #740.** Workflow *Sky-Claw CI/CD*,
run `39314592197`, job `Tests / windows-latest / py3.11`: falla intermitente
`tests/test_lock_cancelacion_adquisicion.py::test_la_liberacion_es_idempotente_y_no_borra_la_lease_readquirida[legacy]`
(`assert 1792440151.8032343 != 1792440151.8032343`). Causa raÃ­z: el token de fencing se acuÃ±a con
`time.time()` (`sky_claw/app/db/locks.py:421,430`) y en Windows/py<3.13 el reloj avanza de a ~15,6 ms, asÃ­
que dos adquisiciones en el mismo tick comparten token. La pata py3.12 del mismo commit pasÃ³. **Existe
PR #750 abierto** ("token de fencing estrictamente creciente por base") que lo corrige, **sin merge**; no
se tocÃ³ desde esta sesiÃ³n. **PR #740: VERDE** (31 checks, 0 fail; el CI sobre su merge commit pasÃ³ 12/12).

## 11. Defectos encontrados (no corregidos â€” PRs separados)

### F1 â€” El `Error:` del modal *stitched* se clasifica terminal y tumba una corrida que el operador autorizÃ³ continuar

- **DÃ³nde:** `sky_claw/local/tools/dyndolod_runner.py` â€” `NO_FATALES_DEL_DOMINIO` no incluye
  `Found stitched object LOD texturesâ€¦`; cae en `_ERROR_SUELTO` â‡’ terminal.
- **QuÃ© se observÃ³:** T1. TexGen completÃ³ (`TexGen completed successfully`, `Exit TexGen`, exit 0,
  1.362 archivos) y el pipeline fallÃ³ por esa lÃ­nea, revirtiendo el output. La aprobaciÃ³n del modal
  (`Ignore`) queda asÃ­ sin efecto prÃ¡ctico: el operador autoriza continuar y el post-check rechaza la
  misma corrida.
- **Impacto:** cualquier re-corrida de TexGen sobre un `Data` con output previo instalado falla, en
  **los dos modos** (no es exclusivo del desatendido). La mitigaciÃ³n operativa es la que el propio
  DynDOLOD recomienda (desinstalar el output viejo antes de regenerar) â€” es la que se aplicÃ³ para T2.
- **Propuesta (PR separado, RED/GREEN):** decidir explÃ­citamente entre (a) declarar la categorÃ­a
  no-fatal (con test que congele la lÃ­nea completa y su gemela en el idioma de la GUI) o (b) documentar
  que `Ignore` sobre ese modal no es soportable y retirarlo del contrato. **No se cambiÃ³ nada acÃ¡.**

### F2 â€” Un hueco transitorio de observaciÃ³n UIA en el paso `INVOCAR` del modal aborta la corrida sin reintento

- **DÃ³nde:** `ConductorDesatendido._atender_modal` (`dyndolod_uia_driver.py:1519-1532`) hace **un solo**
  intento de `INVOCAR` despuÃ©s de la aprobaciÃ³n; un `PRECONDICION_FALLIDA` (pantalla ya no coincide)
  termina en `UIA_CONTROL_MAP_MISMATCH`.
- **QuÃ© se observÃ³:** D1. La modal estaba viva 40 s antes; en el paso de invocaciÃ³n la instantÃ¡nea no la
  contenÃ­a (`pantalla=CARGANDO`), `invocado=False`. Sondas posteriores: modal estable â‰¥100 s y 10/10
  observaciones correctas; el reintento D2 pasÃ³.
- **Por quÃ© importa:** el desenlace NO fue ambiguo (no se invocÃ³ nada), asÃ­ que reintentar tras
  re-observar es seguro; hoy se pierde una corrida de ~5 min por una lectura.
- **Propuesta (PR separado, RED/GREEN):** en `_atender_modal`, si `invocado=False` y la pantalla sigue
  siendo MODAL_CONOCIDO con el mismo `modal_id`, re-observar y actuar (bounded); si la pantalla cambiÃ³,
  re-clasificar. Test RED: helper que devuelve una instantÃ¡nea sin el diÃ¡logo en el primer intento.
  **No se cambiÃ³ nada acÃ¡.**

### F3 â€” (heredado, CI) token de fencing con reloj grueso de Windows

Ver Â§10: defecto de producto ya identificado, con PR #750 abierto sin merge. Es el FAILURE del CI de
`origin/main`. Afecta la garantÃ­a de ownership de la etapa 9; **no invalida** esta corrida (secuencial,
sin contenciÃ³n), pero debe cerrarse antes de considerar el bloqueo cross-process "terminado".

## 12. Veredictos

### Standalone: `UNATTENDED_STANDALONE_PASS`

Alcance certificado: **Alpha-209 exacto** (hashes Â§2), Windows 10 19045, rig aislado, perfil de prueba
`UnattendedRig`, Data Stock 1.6.1170 + DynDOLOD Resources SE, **backend COM productivo**, **acciones
UIA exactly-once** (Start/Advanced/OK/Save and Exit/Ignore), configuraciÃ³n aprobada por canal remoto
(sustituciÃ³n declarada del transporte), presets restaurados byte-exactos, post-check existente verde,
packaging, handoff `completed`, sin procesos residuales, con dos defectos abiertos (F1, F2) que
**no** invalidan la corrida aceptada pero deben corregirse en PRs separados.

**No certifica:** MO2/USVFS; Telegram como canal (no configurado); dynamic LOD (DLL NG ausente);
etapas 6â€“8 (no ejercitadas); otras versiones/configuraciones; aliases de ruta no probados (8.3, `\\?\`,
`subst`, junction, UNC); inmutabilidad global de la mÃ¡quina (hubo actividad de terceros).

### MO2/USVFS: `UNATTENDED_BROKERED_NOT_TESTED`

El brokered exige una instancia/perfil MO2 de PRUEBA con el broker USVFS aislado y una **nueva
autorizaciÃ³n explÃ­cita** del operador; ninguna de las dos cosas se hizo. No se reutiliza el PASS
standalone para certificar USVFS.

### `DYNDOLOD_661_REAL_RIG_PASS`

**No se declara.** Requiere ambos modos con evidencia suficiente; el brokered no se ejecutÃ³.

## 13. DecisiÃ³n sobre #661

- **No cerrar #661.** El criterio de DONE (standalone PASS **y** brokered PASS **y** evidencia commiteada)
  queda a un modo de distancia; ademÃ¡s hay dos defectos abiertos que una corrida verde no borra.
- **No mergear** nada desde esta sesiÃ³n. Esta rama es **documental**.
- PrÃ³ximos pasos propuestos, en orden: (1) PR correctivo de F2 (bajo riesgo, alto valor: evita perder
  corridas); (2) decisiÃ³n de owner sobre F1; (3) autorizaciÃ³n + rig brokered; (4) PR #750 de locks con
  autorizaciÃ³n del owner; (5) reciÃ©n entonces evaluar el cierre de #661.

## 14. Archivos de evidencia

`docs/validation/2026-10-19_pr661_unattended_windows_real_rig/`: `final-report.md`, `preflight.json`,
`environment.json`, `binary-hashes.json`, `isolation.json`, `uia-control-evidence.json`,
`texgen-command.txt`, `dyndolod-command.txt`, `texgen-log-excerpt.txt`, `dyndolod-log-excerpt.txt`,
`telegram-hitl-evidence.json`, `preset-before-after.json`, `handoff-evidence.json`, `postcheck.json`,
`packaging-summary.json`, `journal-summary.json`, `cleanup-and-processes.json`, `negative-tests.json`,
`test-results.txt`.

El rig completo (`C:\SkyClaw_Rigs\SW661 Unattended`) se conserva para revisiÃ³n: incluye
`evidence/attempt1-texgen/`, `evidence/attempt1-dyndolod/`, `evidence/attempt2-texgen/`,
`evidence/attempt2-dyndolod/`, `state/hitl/`, `state/run.log` y los logs crudos de las herramientas.
No se publican secretos, tokens, archivos de Bethesda ni datos personales.

