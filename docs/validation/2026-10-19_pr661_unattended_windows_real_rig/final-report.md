# #661 — Rig real Windows de la etapa 9 DESATENDIDA (TexGen + DynDOLOD)

> **Sesión:** `DYNDOLOD-661-UNATTENDED-WINDOWS-ACCEPTANCE` · **Fecha:** 2026-10-19 · **SO:** Windows 10 Pro 19045
> **Rama:** `validation/dyndolod-661-unattended-windows-rig` · **Base de EJECUCIÓN:** `origin/main` = `6abf20d06d908a6c3bc8f4648cb4cd7bb4111c37`
> **Base actual del PR:** `main` avanzó a `a3b5e441` (incluye PR #750 mergeado como `99aaba03`, más #753/#755);
> **los rigs NO se re-ejecutaron sobre esos commits** — el código certificado es el de la base de ejecución.
> **Worktree:** `<WORKTREE>`
> **Rig aislado:** `C:\SkyClaw_Rigs\SW661 Unattended`
>
> **Evidencia primaria:** `standalone/` (sanitizada) y `brokered-evidence.json`; privacidad y límites en §15
> **Veredicto MO2/USVFS:** `UNATTENDED_BROKERED_PASS` (alcance en §12)
> **`DYNDOLOD_661_REAL_RIG_PASS`:** **SE DECLARA**, con los límites declarados en §12 (Telegram no
> ejercido como canal, dynamic LOD no generado, etapas 6–8 no ejercitadas, F1/F2 abiertos).
> **#661:** no se cierra. No se mergea nada desde esta sesión.

## 1. Qué se certificó y con qué código

Se ejecutó la cadena completa del pipeline productivo sobre los binarios reales Alpha-209,
en un rig físicamente aislado, **sin que ningún humano pulsara un botón local**:

```text
execute(interaction_mode="desatendido")
  → preflight green → aprobación remota de configuración
  → spawn TexGen → modal conocido → aprobación remota → Ignore (UIA)
  → convergencia del Output (rancio → administrado) + readback
  → Start (UIA, exactly-once) → generación → diálogo terminal → Exit TexGen (UIA, exactly-once)
  → post-check + packaging + handoff preservado
  → [materialización standalone del árbol de TexGen en Data, documentada]
  → spawn DynDOLOD → modal conocido → aprobación remota → Ignore (UIA)
  → Advanced >>> (UIA) → reconvergencia del Output → OK/Begin (UIA, exactly-once)
  → generación (269,4 s) → diálogo terminal → Save and Exit (UIA, exactly-once)
  → post-check + packaging + handoff completed + preset restaurado por SHA
```

Código certificado: el de `origin/main` en `6abf20d0`. La etapa 9 desatendida ya está **mergeada en
`main`** por PR #740 (merge commit `3e89ff3a`, 2026-10-18); la base de ejecución incluye además el
contrato Data Path de #739 y las garantías de cancelación/ownership de #742. Después del rig, `main`
avanzó (PR #750 de locks, #753, #755): **este paquete no re-ejecutó nada sobre esos commits** y no
integra cambios de producción. **No se modificó una sola línea de código** durante el rig: el diff de
esta rama es sólo documentación/evidencia.

La misma cadena se ejecutó **dos veces** en dominios distintos: **standalone** (`-d:` = Data físico,
§7) y **brokered MO2/USVFS** (`-d:` = Data virtual del perfil, §8-bis).

## 2. Identidad e insumos

| Elemento | Valor |
|---|---|
| `origin/main` al iniciar y al cerrar | `6abf20d06d908a6c3bc8f4648cb4cd7bb4111c37` (sin movimiento) |
| HEAD del worktree | `6abf20d0` (branch nueva desde `origin/main`) |
| `TexGenx64.exe` | 35.794.432 B · v3.0.0.209 · SHA256 `0939BC8F8CBAE2E1B38F17D56FECD1B7A941DADE0554E944886BD7F5C4807A62` |
| `DynDOLODx64.exe` | 35.794.432 B · v3.0.0.209 · SHA256 `B67625EB7815111BA9AC232C626FF88B07BB64FF30FF5183C5A04B5F045C3BD0` |
| `SkyrimSE.exe` | v1.6.1170.0 · SHA256 `C434208894F07F604B852F29B8EDC3A58C4DE63DE783373733E72B2B73F33BE9` |
| Python del rig | 3.11.9 · venv dedicado con editable install del **worktree de la sesión** · comtypes 1.4.17 |
| Preset TexGen | 1.705 B · SHA256 `75C4AA4A5746D24F32BB2BE455C5EE13B0DB1F87E3412A9EB463C90F5E87BA8E` (OutputPath rancio de #739, a propósito) |
| Preset DynDOLOD | 24.061 B · SHA256 `E728F1F36171C50F4DB386AC56DB24B4DFCE726261A2D55911EE4A8736209DC4` (del operador, `Preset=Medium`, OutputPath rancio de T5 Rig) |

El contrato UIA (`CONTRATOS_DE_UI`) contiene **exactamente dos entradas**: `TexGen` y `DynDOLOD`
Alpha-209 SSE (congeladas por `test_el_registro_contiene_exactamente_los_builds_medidos`). La admisión
por build se aplica **sólo a esos dos ejecutables**, y ambos coinciden, por eso el modo desatendido pudo
arrancar (`UNATTENDED_BUILD_UNSUPPORTED` era el corte posible y no ocurrió). `SkyrimSE.exe` **no es una
entrada del contrato** ni se admissiona por SHA de build: su hash es **evidencia de identidad del rig**
(acredita que el `Data` usado correspondía a 1.6.1170).

## 3. Aislamiento (Fase B)

Rig exclusivo `C:\SkyClaw_Rigs\SW661 Unattended`, sin junctions/symlinks (0 reparse points, verificado).

| Recurso | Estado |
|---|---|
| `game\Data` | 22.556 archivos / 18.329.994.075 B — composición ya aislada en #739 (Stock 1.6.1170 + DynDOLOD Resources SE) |
| `tools\DynDOLOD` | Alpha-209; `Logs/`, `Summary/` y `Edit Scripts\Export/` excluidos al materializar |
| `Game INI` | copias byte-exactas de `SkyClaw-PR2-Rig` (SHA `4D7C9D67…`, `7406F512…`) |
| MO2 de prueba | `mo2-install` + `mo2-instance` con perfil nuevo `UnattendedRig` (5 masters, 0 light) |
| `mods\TexGen Output` / `mods\DynDOLOD Output` | decoys VACÍOS antes de las corridas |
| `Work Root` | root externo administrado, binding `5b14cd20-61c1-4528-aabf-54115e072605`, estado `C_BINDING_COMPATIBLE` |
| Fuentes | `C:\SkyClaw_Rigs\PR739 Data Path` **sólo lectura**; conteos/bytes/mtime sin cambios; G:\Modding, G:\SteamLibrary, C:\steam: conteos/bytes idénticos pre/post |

**Actividad de terceros observada (no atribuible a esta sesión):** MO2 de producción con el perfil
`<PERFIL_DEL_OPERADOR>` (17:32–17:45) y Steam actualizando Skyrim en `G:\SteamLibrary\steamapps\downloading`
(17:45:28-29). No hay forma de afirmar inmutabilidad global de la máquina; sí se afirma que este rig
no escribió en esas rutas y que las fuentes que usó no cambiaron durante la ventana.

## 4. Fase C — backend COM real medido (antes de generar)

**Método:** el backend productivo `dyndolod_uia_driver_windows.BackendUIAWindows` y el ejecutor productivo
`EjecutorDePasoPorHelper` (proceso descartable por paso), contra las ventanas reales.

| Punto | Medición |
|---|---|
| PID y ruta canónica | ventana `TfrmMain` localizada por `UIA_ProcessIdPropertyId` == pid del proceso spawneado (atribución estructural, no por título) |
| Ventana contractual | `TfrmMain` · título `TexGen 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE)` / `DynDOLOD 3.0 Alpha-209 x64 - …` |
| Control Output | único `Edit`/`TEdit`, habilitado (`AutomationId` = HWND por instancia) |
| Mecanismo | `LegacyIAccessiblePattern.SetValue` → **`S_OK`** + readback **exacto**; nunca se probó otro mecanismo |
| Botones | `Start` (`TButton`), `Advanced >>>`, `OK`, `Cancel`, `Help`, presets `Low/Medium/High` — todos `TButton` |
| Diálogos | `#32770` con `MainInstruction`; `Ignore` = `CCPushButton`/`CommandButton_5`; terminal `Exit TexGen` = `CommandButton_7`, `Save and Exit` = `CommandButton_6` |
| **RIESGO 1 (Output rancio)** | **confirmado**: la GUI precarga el `OutputPath=` del preset (`…\PR739 Data Path\Work Root\DynDOLOD\TexGen`), distinto del `-o:`. El driver lo detecta, escribe por el mecanismo del contrato y el readback posterior coincide canónicamente. Cero escrituras a la ruta vieja. |
| **RIESGO 2 (ventana sin ProcessId)** | **no observado**: todas las ventanas del pid tuvieron ProcessId legible; el modal es descendiente de `TfrmMain`. El backend sólo acepta ventanas del pid pedido. |
| Modal DLL NG | instrucción exacta `DynDOLOD.DLL from DynDOLOD DLL NG and Scripts not found!`; **estable ≥100 s sin interacción** (5 relecturas) y 10/10 observaciones frescas `MODAL_CONOCIDO` |

## 5. Aprobaciones HITL (Fase D)

El adaptador es el **productivo**: `AprobadorDesatendidoHITL` sobre `HITLGuard`, categoría
`dyndolod_desatendido` (no auto-aprobable). Se registraron **14 pedidos HITL**: **13 aprobados** y **1 denegado** (fail-closed por un `.resp`
con BOM que no parseó). El desglose es 8 pedidos standalone en §5.1 (7 aprobados + 1 denegado) y
6 pedidos brokered, todos aprobados, en §5.2:

### 5.1 Standalone (8 aprobaciones)

| Pedido | Tipo | Decisión |
|---|---|---|
| `dyndolod-desatendido-77ec7467df0b` | configuración | **DENEGADA** por el guard (el transporte escribió el `.resp` con BOM UTF-8 y no parseó) → fail-closed, sin spawn ni TX |
| `dyndolod-desatendido-0d8699165a6f` | configuración | APROBADA |
| `dyndolod-modal-5116dfdd2c9a` | modal `texgen_stitched_lod_previo` | APROBADA → `Ignore` pulsado |
| `dyndolod-desatendido-e99827c3fc43` | configuración | APROBADA |
| `dyndolod-desatendido-ef4eae792b10` | configuración DynDOLOD | APROBADA |
| `dyndolod-modal-d11ad0883045` | modal `dyndolod_dll_ng_ausente` | APROBADA (el invoke falló por hueco UIA transitorio, ver F2) |
| `dyndolod-desatendido-ec2bf1a93878` | configuración (retry) | APROBADA |
| `dyndolod-modal-86bf9e2fd920` | modal `dyndolod_dll_ng_ausente` (retry) | APROBADA → `Ignore` pulsado |

### 5.2 Brokered (6 aprobaciones)

| Pedido | Tipo | Decisión |
|---|---|---|
| `dyndolod-desatendido-9069f342f468` | configuración (1.er intento, antes de `portable.txt`) | APROBADA; la corrida cortó en `No profile set` (instancia global), sin mutación de las herramientas |
| `dyndolod-desatendido-5b20f07143e8` | configuración (2.º intento) | APROBADA; cortó igual, sin mutación |
| `dyndolod-desatendido-b7009eec7e16` | configuración (3.er intento, portable ya aislada) | APROBADA |
| `dyndolod-modal-7a28d6369e56` | modal `texgen_stitched_lod_previo` | APROBADA → `Ignore` |
| `dyndolod-desatendido-49619ae1a601` | configuración (Data limpia, corrida aceptada) | APROBADA |
| `dyndolod-modal-36829c03ace5` | modal `dyndolod_dll_ng_ausente` | APROBADA → `Ignore` |

**Telegram: NO CONFIGURADO en esta máquina** (`~/.sky_claw/config.toml` sin `[telegram]`, sin token en
keyring; `HITLGuard` sin canal deniega al instante). La ÚNICA sustitución respecto del composition root
productivo es el **transporte** del canal: el `notify_fn` del guard escribe el pedido a
`state/hitl/<id>.json` y la decisión del operador llega por el chat de la sesión. El adaptador, la
categoría, el timeout (900 s) y el fail-closed son los productivos. **El flujo Telegram no se validó
(`telegram_verificado=false`)** — es un límite declarado, no un PASS de esa pieza.

**Etapas 6–8:** el rig aislado no tiene Bashed Patch / Synthesis.esp / grass cache y **no se fabricaron**.
El guard SOP §5.2 vive en la superficie remota (`generate_lods`) y en `GenerateLodsStrategy`; esta sesión
invocó el **servicio** directamente (el mismo boundary que el rig de #739), así que ese guard **no se
ejerció**. Queda cubierto por tests, no por este rig.

## 6. Presets (Fase E)

| Corrida | Antes | Después | Restauración |
|---|---|---|---|
| TexGen (intento modal) | `75c4aa4a…` (1.705 B) | `75c4aa4a…` (1.705 B) | byte-exacta |
| TexGen (intento emitido) | `75c4aa4a…` (1.705 B) | `75c4aa4a…` (1.705 B) | byte-exacta |
| DynDOLOD (intento 1, abortado) | `e728f1f3…` (24.061 B) | `e728f1f3…` (24.061 B) | byte-exacta |
| DynDOLOD (acceptance) | `e728f1f3…` (24.061 B) | `e728f1f3…` (24.061 B) | byte-exacta + `preset restaurado y verificado` en el log |

La restauración se verificó incluso en las corridas fallidas — el contrato `PRESET_RESTORE_FAILED` no
se disparó en ninguna. **Límite declarado:** la configuración de worldspaces se aprueba por la huella del
preset en disco, no por lectura UIA de esos controles; `WORLDSPACE_MISMATCH` no existe en este build.

## 7. Corridas reales (Fase F)

| # | Objetivo | Resultado | Detalle |
|---|---|---|---|
| T1 | TexGen real con modal stitched aprobado | FAIL (post-check) | TexGen **generó 1.362 archivos / 138 MB**, `TexGen completed successfully`, `Exit TexGen` exactly-once, exit 0 — pero la línea `Error: Found stitched object LOD textures…` del modal se clasifica terminal ⇒ el pipeline falla y hace rollback. **DEFECTO F1** (pre-existente, no del modo desatendido). |
| T2 | TexGen real con Data limpia | PASS del tool + corte de handoff | `Start → OK (invocado=True hresult=S_OK)`, generación 67,1 s, `texgen_exit → OK`, packaging OK, preset restaurado. Corte documentado: standalone no ve el mod ⇒ `needs_deployment=true`, `TexGen Output` preservado y certificado bajo lease (TX 2 committed, handoff `AWAITING_DEPLOYMENT`). |
| D1 | DynDOLOD real con modal DLL NG | FAIL (fail-closed) | modal detectado y aprobado; el paso `INVOCAR` encontró `CARGANDO` (hueco transitorio de observación UIA), `invocado=False` ⇒ `UIA_CONTROL_MAP_MISMATCH`, sin mutación, sin reintento. **DEFECTO F2** (intermitente). |
| D2 | DynDOLOD real, retry con el mismo código | **PASS** | `dyndolod_modal_ignore → OK S_OK` · `dyndolod_advanced → OK S_OK` · `dyndolod_begin_ok → OK S_OK` · generación 269,4 s · `dyndolod_save_exit → OK S_OK` · `success=True assisted=False interaction_mode=desatendido errors=[]` · exit code 0 · artefactos y packaging OK · preset restaurado. |

**Fuente primaria publicada:** en `standalone/` están —sanitizados— el log del harness
(`run-log.txt`, con los ledgers exactly-once, `return_code`, restauración de presets y cortes),
los `*-result.json` de cada intento, presets antes/después, comandos efectivos, preflight,
journal y los 8 pares HITL pedido/decisión. Los logs crudos multi-MB de TexGen/DynDOLOD no se
publican (sólo sus extractos en `texgen-log-excerpt.txt` / `dyndolod-log-excerpt.txt`); el rig
original se conserva offline. Con eso un revisor puede distinguir el intento aceptado de los
reintentos y auditar los fail-closed sin depender de la máquina del operador.

**Cobertura del log del harness:** el `run-log.txt` del rig arranca en 2026-10-19 17:59:45 porque el
log del harness se reinicia por corrida. El **primer intento de TexGen (17:55:24)** está evidenciado
por `attempt1-texgen/` (result JSON, comando, presets antes/después y extracto del log de la
herramienta), pero **sin ledger del harness**: la auditoría exactly-once del log cubre los intentos
de 17:59:45 a 18:20, que son los que declaran el PASS.

## 8. Aceptación del standalone (Fase G)

| Condición | Estado | Evidencia |
|---|---|---|
| TexGen real ejecutado | ✅ | T2: 1.362 archivos, 138.457.821 B, `return_code=0`, marcador de completitud presente |
| DynDOLOD real ejecutado | ✅ | D2: 5.105 archivos, 722.206.676 B en el root; `DynDOLOD.esm/esp`, `Occlusion.esp` |
| Backend COM productivo usado | ✅ | §4 (sonda) + `state/run.log` (`UIA desatendido: …`) |
| Start/OK sin operador local | ✅ | `texgen_start → OK S_OK`; `dyndolod_advanced → OK S_OK`; `dyndolod_begin_ok → OK S_OK` |
| Acciones terminales sin operador local | ✅ | `texgen_exit → OK S_OK`; `dyndolod_save_exit → OK S_OK`; el log de la herramienta dice `User says "Ignore"` / `User says "Save and Exit"` |
| Configuración aprobada por canal remoto | ✅ (canal de sesión) / ⚠ Telegram NO | §5 |
| Presets restaurados | ✅ | §6 |
| Output en root administrado | ✅ | `Work Root\DynDOLOD\{TexGen,DynDOLOD}` |
| Decoys intactos / packaging | ✅ | `mods\TexGen Output` 1.363 archivos; `mods\DynDOLOD Output` 5.106 archivos |
| Data Path correcto | ✅ | eco del binario `Using Skyrim Special Edition Data Path: …\SW661 Unattended\game\Data\` == `-d:`; idem Output |
| Handoff válido | ✅ | `Visibilidad de TexGen confirmada: 1362 archivo(s) idénticos`; `deployment_handoffs.state=completed` |
| Exit code 0 | ✅ | `state/run.log`: `TexGen finalizado: return_code=0` y `DynDOLOD finalizado: return_code=0` |
| Marcador de completitud de ESTA corrida | ✅ | `TexGen completed successfully` (slice de esta corrida) · `DynDOLOD plugins generated successfully` + `Occlusion.esp completed successfully` |
| Ausencia de líneas terminales | ✅ | clasificación del runner sobre los bytes de esta corrida |
| Artefactos esperados válidos | ✅ | SHA-256 en `packaging-summary.json` |
| Sin procesos residuales | ✅ | consulta post-cierre vacía |
| Locks y leases consistentes | ✅ | TX 2 y TX 4 `committed`; handoff `completed`; workspace binding `C_BINDING_COMPATIBLE` |
| Journal y snapshots verificables | ✅ | `journal-summary.json` |

**Categorías de LOD producidas:** object/static LOD (texturas de objeto, billboards, `DynDOLOD.esm/.esp`,
`Occlusion.esp`). **Dynamic LOD NO generado**: `DynDOLOD.DLL` de *DynDOLOD DLL NG and Scripts* no está
instalado (misma dependencia faltante que documentó #739). El warning es no terminal y se registra; no se
exige esa característica como si estuviera disponible.

**Warnings no terminales registrados:** 137 líneas de ruido de dominio (deleted references de
Update/Dawnguard/HearthFires, `Unresolved FormID`, `Can't find Skyrim - Patch.bsa`, grupos vacíos de
xEdit) + `Could not find save path`. `DynDOLOD.DLL not found` (ver arriba). Ninguno aborta la corrida y
la taxonomía del runner los clasifica como no-fatales.

## 8-bis. Rig brokered MO2/USVFS (Fase H)

Autorizado explícitamente por el operador después del standalone PASS (§5.2).

**Entorno aislado** `C:\SkyClaw_Rigs\SW661Broker`: copia de MO2 **2.5.2 (rev 9c130cbf)** + **USVFS
0.5.6.1**, instancia **portable** (`portable.txt`, data path = `mo2-install`, perfil descartable
`UnattendedBroker` con canary `SkyClaw_VFS_Rig_Canary/SkyClawRig/586c-canary.txt`, validado por
`build_attestation_challenge`), bridge Sky-Claw instalado con `MO2BridgeInstaller`, `Data` del rig
standalone usado **sólo lectura** (el handoff va por overlay), 0 reparse points.

**Prueba del backend USVFS antes de generar:** `vfs-health` real (`broker.submit` con job `health`)
→ `success=True`: `startApplication` de MO2 + worker + **nieto** atestiguaron el canary dentro del
Data virtual (`data_visibility_domain=virtual_usvfs`, fingerprint `b6010a95…`).

**Cadena desatendida real:**
- TexGen bajo USVFS: `texgen_start → OK (S_OK)` · 75,0 s de generación · `texgen_exit → OK (S_OK)` ·
  packaging en `mods\TexGen Output` (1.363 archivos).
- **Handoff brokered automático:** el gate del producto demostró la visibilidad del artifact en el
  overlay del perfil (sin materialización manual, a diferencia del standalone) y lanzó DynDOLOD.
- DynDOLOD bajo USVFS: `dyndolod_modal_ignore → OK` · `dyndolod_advanced → OK` · `dyndolod_begin_ok → OK`
  · 402,6 s · `dyndolod_save_exit → OK` · `success=True assisted=False errors=[] needs_deployment=False`
  · `mods\DynDOLOD Output` 5.106 archivos / 722.167.036 B.
- Artefactos: `DynDOLOD.esm` 141.695 B (`046A6B89…`), `DynDOLOD.esp` 259.523 B (`E031C74A…`),
  `Occlusion.esp` 9.360.147 B (`DD1E9312…`).
- Log de la herramienta (slice de esta corrida): `Using Output Path: …\SW661Broker\Work Root\DynDOLOD\DynDOLOD\`,
  `User says "Ignore"`, `DynDOLOD plugins generated successfully`, `Occlusion.esp completed successfully`,
  `User says "Save and Exit"`, `Saving …DynDOLOD.esm`.
- Presets restaurados y verificados; TX committed; FlightReport persistido; sin procesos residuales.

**Cómo se abrió MO2 (límite declarado).** El bridge de MO2 cachea el descriptor del broker, así que
al reiniciar el broker hay que reabrir MO2. Además, cuando **el harness** lanzaba MO2 con
`subprocess.Popen`, USVFS devolvía `ERROR_ACCESS_DENIED` al inyectar **cualquier** ejecutable
(probado también con `cmd.exe`); con MO2 abierto por el operador el health y toda la cadena pasaron.
Por eso el rig brokered usa **MO2 abierto una vez por el operador** (setup del entorno, no operación
de la GUI de TexGen/DynDOLOD: ningún botón de las herramientas fue tocado por un humano). Queda como
hallazgo de entorno/plataforma (F3, ver §11), no como fallo del producto.

**Incidente de configuración del operador (declarado).** El primer intento brokered, antes de
`portable.txt`, abrió por error la **instancia GLOBAL** de MO2 del operador y dejó en su
`ModOrganizer.ini` `selected_profile=UnattendedBroker` (sólo esa clave; no se tocaron perfiles,
mods ni load orders). Se detectó de inmediato, se aisló el rig con instancia portable y el valor
se restaura al valor previo (el nombre del perfil personal del operador no se publica) con
confirmación del operador. Las corridas
posteriores no volvieron a tocar la instancia global (verificado por mtime del ini).

## 9. Pruebas negativas (Fase I)

Detalle completo y caso por caso en [`negative-tests.json`](negative-tests.json). Resumen:

- **Medidas en el rig:** rechazo/ilegible de la aprobación → `UNATTENDED_CONFIGURATION_NOT_APPROVED` sin
  mutación (N01); output rancio detectado y corregido (N05); handoff no visible → corte antes del spawn
  (N03); hueco UIA transitorio → `UIA_CONTROL_MAP_MISMATCH` sin invocar (N04); modal conocido + Ignore
  aprobado en dos herramientas (N02a/N02b).
- **Cubiertas por tests deterministas** (no se fuerzan contra los binarios para no cruzar fronteras):
  SHA desconocido, preset ausente/desincronizado, readback incorrecto, Begin deshabilitado, desenlace
  ambiguo, helper colgado, modal desconocido, restore fallido, cancelación/lease.
- **No ejecutadas:** etapas 6–8 (N17).

Ninguna prueba negativa escribió en el perfil activo ni en un destino desconocido.

## 10. Quality gates y CI

Ejecutados sobre el worktree (`6abf20d0`, sin cambios de código; detalle crudo en
[`test-results.txt`](test-results.txt)):

| Gate | Resultado |
|---|---|
| `ruff check sky_claw/ scripts/ tests/` | **VERDE** (exit 0) |
| `ruff format --check sky_claw/ scripts/ tests/` | **VERDE** (908 archivos) |
| `mypy sky_claw/ scripts/` | **VERDE** (377 archivos; caveat: ~30 módulos con `ignore_errors`) |
| `git diff --check` / `git status --porcelain` | limpio |
| Suite focalizada DynDOLOD/UIA/presets/handoff/locks | **1829 passed, 13 skipped, 0 failed** |
| Suite completa (11937 tests) | **3 failed, 11809 passed, 125 skipped** (2332 s). Los 3 fallos **no tocan #661** y tienen causa ajena a este cambio: (1) `test_distributed_locks.py::test_renew_divisor_shrinks_detection_window` — flake bajo carga (pasa aislado en 1,59 s; la máquina tardó 38:52 vs 21:50 del CI); (2) `test_runtime_vault_mutation_executor.py::…::test_apply_real_con_fallo_dispara_rollback_exacto` — `CreateFileW` código 5 al restaurar ACL, **sesión no elevada** (el runner de CI corre elevado); (3) `test_worktree_convention_invariant.py::test_los_worktrees_no_principales_viven_bajo_la_convencion` — estado del host: 15 worktrees de otras sesiones fuera de la convención |

**CI de `origin/main` 6abf20d0: ROJO — defecto de PRODUCTO, ajeno a #740.** Workflow *Sky-Claw CI/CD*,
run `39314592197`, job `Tests / windows-latest / py3.11`: falla intermitente
`tests/test_lock_cancelacion_adquisicion.py::test_la_liberacion_es_idempotente_y_no_borra_la_lease_readquirida[legacy]`
(`assert 1792440151.8032343 != 1792440151.8032343`). Causa raíz: el token de fencing se acuña con
`time.time()` (`sky_claw/app/db/locks.py:421,430`) y en Windows/py<3.13 el reloj avanza de a ~15,6 ms, así
que dos adquisiciones en el mismo tick comparten token. La pata py3.12 del mismo commit pasó. **PR #750
fue mergeado después del rig** (merge commit `99aaba03`, 2026-10-19T20:55Z) y `main` avanzó a
`a3b5e441` con #753/#755: el rojo descrito corresponde a la **base de ejecución** `6abf20d0`, no a la
punta actual, y **el rig no se re-ejecutó sobre los commits posteriores**. **PR #740: VERDE** (31 checks,
0 fail; el CI sobre su merge commit pasó 12/12).

## 11. Defectos encontrados (no corregidos — PRs separados)

### F1 — El `Error:` del modal *stitched* se clasifica terminal y tumba una corrida que el operador autorizó continuar

- **Dónde:** `sky_claw/local/tools/dyndolod_runner.py` — `NO_FATALES_DEL_DOMINIO` no incluye
  `Found stitched object LOD textures…`; cae en `_ERROR_SUELTO` ⇒ terminal.
- **Qué se observó:** T1. TexGen completó (`TexGen completed successfully`, `Exit TexGen`, exit 0,
  1.362 archivos) y el pipeline falló por esa línea, revirtiendo el output. La aprobación del modal
  (`Ignore`) queda así sin efecto práctico: el operador autoriza continuar y el post-check rechaza la
  misma corrida.
- **Impacto:** cualquier re-corrida de TexGen sobre un `Data` con output previo instalado falla, en
  **los dos modos** (no es exclusivo del desatendido). La mitigación operativa es la que el propio
  DynDOLOD recomienda (desinstalar el output viejo antes de regenerar) — es la que se aplicó para T2.
- **Propuesta (PR separado, RED/GREEN):** decidir explícitamente entre (a) declarar la categoría
  no-fatal (con test que congele la línea completa y su gemela en el idioma de la GUI) o (b) documentar
  que `Ignore` sobre ese modal no es soportable y retirarlo del contrato. **No se cambió nada acá.**

### F2 — Un hueco transitorio de observación UIA en el paso `INVOCAR` del modal aborta la corrida sin reintento

- **Dónde:** `ConductorDesatendido._atender_modal` (`dyndolod_uia_driver.py:1519-1532`) hace **un solo**
  intento de `INVOCAR` después de la aprobación; un `PRECONDICION_FALLIDA` (pantalla ya no coincide)
  termina en `UIA_CONTROL_MAP_MISMATCH`.
- **Qué se observó:** D1. La modal estaba viva 40 s antes; en el paso de invocación la instantánea no la
  contenía (`pantalla=CARGANDO`), `invocado=False`. Sondas posteriores: modal estable ≥100 s y 10/10
  observaciones correctas; el reintento D2 pasó.
- **Por qué importa:** el desenlace NO fue ambiguo (no se invocó nada), así que reintentar tras
  re-observar es seguro; hoy se pierde una corrida de ~5 min por una lectura.
- **Propuesta (PR separado, RED/GREEN):** en `_atender_modal`, si `invocado=False` y la pantalla sigue
  siendo MODAL_CONOCIDO con el mismo `modal_id`, re-observar y actuar (bounded); si la pantalla cambió,
  re-clasificar. Test RED: helper que devuelve una instantánea sin el diálogo en el primer intento.
  **No se cambió nada acá.**

### F3 — Lanzar MO2 desde un proceso no interactivo hace fallar la inyección USVFS

- **Qué se observó:** con MO2 2.5.2 lanzado por el harness (`subprocess.Popen` desde el proceso de la
  sesión), `IOrganizer.startApplication` devuelve `ERROR_ACCESS_DENIED (0x5)` y USVFS loguea
  `failed to spawn` **para cualquier ejecutable** (reproducido también con `cmd.exe`). Con MO2 abierto
  por el operador desde Explorer, el mismo worker, perfil y broker pasan el health y toda la cadena.
- **Alcance:** es un límite del **entorno de ejecución del rig** (probablemente restricciones de
  integridad/job del proceso que lanza MO2, no del producto). No se modificó nada para "arreglarlo".
- **Aislamiento exigido y aplicado:** el rig brokered corre sobre una **copia propia de MO2** en modo
  **portable** (`portable.txt`, data root = copia), con perfil descartable y canary propio. **Está
  prohibido** usar o modificar la instancia global de MO2 del operador; el único contacto accidental
  (§8-bis) se detectó, se revirtió y se documenta. El lanzamiento programático de MO2 debe resolverse
  en una sesión interactiva (CreateProcess/token explícitos) antes de prometer brokered 100% autónomo.
- **Consecuencia para la certificación:** el rig brokered requirió que el operador abriera MO2 una vez
  (setup); TexGen y DynDOLOD fueron operados 100% por Sky-Claw. Documentado como límite, no como PASS
  silencioso.

### F4 — (heredado, CI) token de fencing con reloj grueso de Windows

Ver §10: defecto de producto ya identificado; el PR #750 lo corrigió y **fue mergeado después del rig**
(`99aaba03`). Era el FAILURE del CI en la base de ejecución. Afecta la garantía de ownership de la etapa
9; **no invalida** esta corrida (secuencial, sin contención). El rig no se re-ejecutó sobre la base que
incluye el fix.

## 12. Veredictos

### Standalone: `UNATTENDED_STANDALONE_PASS`

Alcance certificado: **Alpha-209 exacto** (hashes §2), Windows 10 19045, rig aislado, perfil de prueba
`UnattendedRig`, Data Stock 1.6.1170 + DynDOLOD Resources SE, **backend COM productivo**, **acciones
UIA exactly-once** (Start/Advanced/OK/Save and Exit/Ignore), configuración aprobada por canal remoto
(sustitución declarada del transporte), presets restaurados byte-exactos, post-check existente verde,
packaging, handoff `completed`, sin procesos residuales, con dos defectos abiertos (F1, F2) que
**no** invalidan la corrida aceptada pero deben corregirse en PRs separados.

**No certifica:** MO2/USVFS (cubierto por separado en la sección siguiente); Telegram como canal (no
configurado); dynamic LOD (DLL NG ausente);
etapas 6–8 (no ejercitadas); otras versiones/configuraciones; aliases de ruta no probados (8.3, `\\?\`,
`subst`, junction, UNC); inmutabilidad global de la máquina (hubo actividad de terceros).

### MO2/USVFS: `UNATTENDED_BROKERED_PASS`

Alcance: MO2 2.5.2 rev `9c130cbf` / USVFS 0.5.6.1, instancia **portable aislada**, perfil
`UnattendedBroker`, `data_visibility_domain=virtual_usvfs`, bridge Sky-Claw real, canary atestado
por worker + nieto, TexGen y DynDOLOD reales bajo USVFS con todas las acciones UIA exactly-once y
handoff brokered demostrado por el gate del producto.

**No certifica:** otras versiones de MO2/USVFS; producción del operador; Telegram como canal;
dynamic LOD; etapas 6–8. **Límite operativo:** MO2 se abre una vez por el operador (el harness no
puede lanzarlo por el bloqueo USVFS/`ERROR_ACCESS_DENIED`, F3).

### `DYNDOLOD_661_REAL_RIG_PASS`

**SE DECLARA** con los límites de arriba. La modalidad desatendida —Sky-Claw operando las GUIs de
TexGen y DynDOLOD por UIA, sin que un humano pulse sus botones locales— quedó demostrada por
ejecución real en los dos backends que promete la etapa 9.

## 13. Decisión sobre #661

- **El cierre de #661 queda pendiente de adjudicación de alcance por el owner.** Este PR **no
  propone cerrarlo**: los residuales quedan declarados y **ninguno oculto** — (a) Telegram como
  canal no ejercido (no configurado en la máquina); (b) dynamic LOD no generado (DLL NG ausente);
  (c) etapas 6–8 no ejercitadas (guard vive en la superficie remota); (d) F1/F2 abiertos;
  (e) F3 (MO2 debe abrirse una vez por el operador en esta máquina). El owner debe adjudicar ese
  alcance o crear los issues de seguimiento antes de un cierre.
- **No mergear** nada desde esta sesión. Esta rama es **documental**.
- Próximos pasos propuestos, en orden: (1) PR correctivo de F2 (bajo riesgo, alto valor: evita perder
  corridas); (2) decisión de owner sobre F1; (3) validar el canal Telegram en una máquina con bot
  configurado; (4) cerrar F3 (lanzamiento programático de MO2) si se promete brokered 100% autónomo;
  (5) adjudicación de alcance de #661 por el owner (o issues de seguimiento). PR #750 (locks) ya
  fue mergeado por el owner.

## 14. Archivos de evidencia

`docs/validation/2026-10-19_pr661_unattended_windows_real_rig/`: `final-report.md`, `preflight.json`,
`environment.json`, `binary-hashes.json`, `isolation.json`, `uia-control-evidence.json`,
`texgen-command.txt`, `dyndolod-command.txt`, `texgen-log-excerpt.txt`, `dyndolod-log-excerpt.txt`,
`telegram-hitl-evidence.json`, `preset-before-after.json`, `handoff-evidence.json`, `postcheck.json`,
`packaging-summary.json`, `journal-summary.json`, `cleanup-and-processes.json`, `negative-tests.json`,
`test-results.txt`, `brokered-evidence.json`, `brokered-run-log.txt`, `brokered-commands.txt`,
`brokered-dyndolod-log-excerpt.txt`, y el árbol `standalone/` con la evidencia primaria sanitizada
(`run-log.txt`, `uia-ledger-y-postcheck.txt`, `attempt1-*/`, `attempt2-*/`, `hitl/`, `preflight.*`,
`journal-transacciones.json`, `binding.json`, `README.md`).

Los rigs completos (`C:\SkyClaw_Rigs\SW661 Unattended` y `C:\SkyClaw_Rigs\SW661Broker`) se conservan
para revisión offline: incluyen los logs crudos de las herramientas y el estado durable. **No se
publican** secretos, tokens, archivos de Bethesda ni datos personales (ver §15).

## 15. Privacidad y sanitización (P1 de la revisión)

Este repositorio es **público**. La evidencia de las corridas se revisó antes de pedir merge y se
**sanitizó en el árbol actual**; el historial publicado anterior a esta sanitización se trata aparte
(§15.3).

### 15.1 Qué se redactó en el árbol actual

Se reemplazaron por placeholders estables (sin reproducir ningún valor original en este documento):

- identidad de cuenta del operador (nombre de usuario) en rutas y en cadenas de log;
- nombre de equipo, SID de sesión y nombre de consola;
- fabricante/modelo del hardware;
- rutas personales de perfil (`C:\Users\<usuario>…`, `AppData`, `%LOCALAPPDATA%` de MO2 global);
- nombre del perfil personal de MO2 del operador (proyecto de mods);
- en la evidencia UIA: se eliminaron **títulos y ventanas de terceros** (navegador, asistentes,
  superposiciones) y se conservaron sólo las ventanas técnicas `TfrmMain`/`#32770` del proceso bajo
  prueba, sin `UIA_NamePropertyId`.

No se encontró **ningún secreto** en la evidencia: no hay tokens de Telegram, claves de API, cookies ni
credenciales. Los pedidos HITL no transportan tokens (el canal Telegram no estaba configurado).

### 15.2 Estado del árbol

Todos los archivos de evidencia son **UTF-8 sin BOM**, con LF, JSON válido cuando corresponde, y el
paquete no contiene los patrones de identidad redactados. `test-results.txt` incluye el gate de diff
re-ejecutado sobre el conjunto final (GATE 4-bis).

### 15.3 Exposición histórica — PENDIENTE DE DECISIÓN DEL OWNER

Los valores originales **estuvieron en commits ya publicados** de esta rama (los tres commits de
evidencia previos a esta sanitización) y por lo tanto pueden seguir visibles en el historial del PR,
en sus diffs y en cachés de GitHub. **No se ejecutó force-push, reescritura de historia ni cambio de
visibilidad**: requieren autorización explícita del owner.

Refs afectadas (identificadas por commit, sin reproducir los datos): `ea4ec48d`, `f8bd56f0`,
`e5110a4c` y la rama `validation/dyndolod-661-unattended-windows-rig`; el PR #754 y sus vistas de
diff/comentarios.

Estrategia propuesta, **para ejecutar sólo con autorización**:

1. Congelar el merge de #754 hasta decidir; no publicar más evidencia cruda.
2. Reescribir la rama en un clon espejo con `git filter-repo` (o BFG) para reemplazar las cadenas por
   los mismos placeholders de §15.1, preservando el contenido probatorio.
3. `push --force-with-lease` de la rama reescrita y actualización del head del PR.
4. Pedir a GitHub Support que expire las vistas cacheadas y las refs del PR (los objetos viejos pueden
   quedar accesibles por SHA directo durante un tiempo).
5. Asumir que forks, mirrors y clones ya descargados **no se pueden recuperar**; si el owner considera
   que la exposición es inaceptable, evaluar rotación/no-reutilización de los identificadores de cuenta
   (no hay secretos que rotar).
6. Dejar constancia en el PR de la decisión (aceptar la exposición o purgar) sin repetir los valores.

Mientras no se autorice la reescritura, la mitigación es **no mergear** #754 y no volver a publicar
evidencia sin pasar por el sanitizador del rig.
