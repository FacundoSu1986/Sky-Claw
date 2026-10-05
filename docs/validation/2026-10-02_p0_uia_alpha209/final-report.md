# P0 — Diagnóstico real Windows UIA: TexGen + DynDOLOD Alpha-209 (#661)

> **Fecha:** 2026-10-02 · **Estado: `P0_A_COMPLETE_NEEDS_P0_B`** · STOP aquí.

## Precondición 0 — worktree principal preservado

```text
PRIMARY_WORKTREE_PRESERVED = YES
primary branch      : feat/runtime-vault-s4e-production-wiring (intacta de punta a punta)
primary HEAD        : 0103ee4f6de15207032d25c254ede5cf2c01bff9 (→ avanzó a bc92cdf6 durante la sesión, sin tocar el probe)
modified/untracked  : 3 / 8 (Runtime Vault S4E + docs de rigs previos — intactos)
probe worktree path : C:\Worktrees\Sky-Claw-p0-alpha209-uia
probe branch        : research/dyndolod-p0-alpha209-uia
probe base SHA      : 0103ee4f6de15207032d25c254ede5cf2c01bff9 (= origin/main real al comenzar)
```

Ningún archivo local del Runtime Vault fue copiado al probe. Ningún checkout/reset/stash sobre el principal.

## §1 Base viva

`origin/main` = `0103ee4f` (#666), contiene #664/H3-A (`801bd9d3`) y #662 (`f91f3d24`). Coincide con el SHA conocido.

## Identidad de builds (§8) — contrato mínimo `(tool, exe_sha256)`

| Tool | Path | SHA-256 | Size | FileVersion | Arch |
|---|---|---|---|---|---|
| TexGenx64.exe | `C:\Modding\DynDOLOD RigTest\TexGenx64.exe` | `0939BC8F8CBAE2E1B38F17D56FECD1B7A941DADE0554E944886BD7F5C4807A62` | 35.794.432 | 3.0.0.209 | x64 |
| DynDOLODx64.exe | `C:\Modding\DynDOLOD RigTest\DynDOLODx64.exe` | `B67625EB7815111BA9AC232C626FF88B07BB64FF30FF5183C5A04B5F045C3BD0` | 35.794.432 | 3.0.0.209 | x64 |

UI language observable: títulos en inglés, controles de sistema localizados al español (locale). Idénticos a los rigs 2026-09-10/09-15.

## Gate de los tres defectos del runner (§3) — revalidado por lectura de código sobre `0103ee4f`

```text
RUNNER_P1_PACKAGING_CANCEL = OPEN   # asyncio.to_thread(_empaquetar_sincrono) sin shield (dyndolod_runner.py:2488)
RUNNER_P1_REPARSE_COPY     = OPEN   # shutil.copytree pelado (:2481); guards cubren ancestros, no descendientes
RUNNER_P2_DOUBLE_CANCEL    = OPEN   # handler CancelledError sin suppress en el gather; segunda cancelación
                                    # interrumpe kill/drain/heartbeat antes de close_job (:1890-1896)
```

Método: lectura de código ("verificado leyendo", no reproducido aquí) + ausencia de commits en las áreas desde la auditoría #592. **Sin fixes reimplementados (§30). Consecuencia: `P0_B_PRODUCT_PIPELINE_BLOCKED`.**

---

## RESULTADOS P0-A

### TexGen — cadena UIA medida (rondas 3 y 4, tras `Ignore` humano)

| Campo | Resultado |
|---|---|
| Ventana principal | `TfrmMain` "TexGen 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE)", FrameworkId `Win32`, 116 controles |
| Modal de arranque (§16/§17) | `#32770` — *"Found stitched object LOD textures from earlier TexGen generation installed in game folder."* Botones: `Ignore` (`CCPushButton`/`CommandButton_5`), `Exit TexGen` (`CommandButton_3`). **Fingerprint MATCH 3/3** (rondas 2, 3, 4). Clasificación: *observed modal*, familia *previous TexGen output present* |
| Output selector | **OK** — TEdit único (ControlType `Edit`), por estructura, no posición |
| ValuePattern.SetValue | **FAIL** — `COMError(-2146233079)` (HRESULT `-0x7feceaf7`), readback intacto. **Idéntico en ambas rondas — reproduce #661** |
| LegacyIAccessible.SetValue | **PASS** — `S_OK`, readback exacto del root temporal `%TEMP%\SkyClaw-P0-<uuid>\OutputTest`, `changed=true` |
| Restauración | Por el MISMO mecanismo (Legacy): `S_OK`, readback = valor original exacto, `restored_ok=true` en ambas rondas |
| Start | `TButton` "Start", enabled, con `InvokePattern` — **inventariado, NO invocado** |
| Exit TexGen | No existe como botón del wizard; sólo en modales (arranque capturado; diálogo terminal post-generación pendiente de P0-B) |
| Presets | SHA-256 byte-idénticos en las 4 fases de ambas rondas — **0 drift por launch ni por mutación del Output** |
| Stale / residuos | 0 `UIA_E_ELEMENTNOTAVAILABLE`; 0 procesos residuales |
| Estabilidad (§20) | **STABLE** |

Valor original del Output observado en el wizard: `E:\Sectores\...\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\TexGen\` — el `OutputPath` del preset `DynDOLOD_SSE_TexGen.ini` (stale del rig PR2). Corrobora la precedencia del preset (§9, observación sin atribución causal).

### DynDOLOD — cadena UIA medida (rondas 1 y 2, tras `Ignore` humano)

| Campo | Resultado |
|---|---|
| Ventana principal | `TfrmMain` "DynDOLOD 3.0 Alpha-209 x64 - Skyrim Special Edition (SSE)", `Win32`, 52 controles |
| Modal de arranque | `#32770` — *"DynDOLOD.DLL from DynDOLOD DLL NG and Scripts not found!"* Botones: `Ignore` (`CommandButton_5`), `Exit DynDOLOD` (`CommandButton_3`). **Fingerprint MATCH 2/2**. Clasificación: *observed modal* (advertencia benigna conocida del rig: plugin SKSE dinámico ausente) |
| Output selector | **OK** — TEdit único |
| ValuePattern.SetValue | **FAIL** — mismo `COMError(-2146233079)`, idéntico en ambas rondas |
| LegacyIAccessible.SetValue | **PASS** — `S_OK`, `changed=true` |
| Restauración | Legacy `S_OK`, readback = valor original exacto, `restored_ok=true` |
| Advanced | `TButton` "Advanced >>>", enabled, con `InvokePattern` — **inventariado, NO invocado** |
| OK / Begin | No presente en el wizard simple (aparece tras Advanced; invocación prohibida en P0-A) |
| Save & Exit | No presente en el wizard simple (diálogo terminal post-generación; pendiente de P0-B) |
| Presets | SHA-256 byte-idénticos en las 4 fases de ambas rondas — **0 drift** |
| Stale / residuos | 0 / 0 |
| Estabilidad (§20) | **STABLE** |

Valor original del Output en el wizard simple: `C:\Modding\DynDOLOD RigTest\DynDOLOD_Output\` (el `OutputPath` del preset Default precargado; observación, sin atribuir causalidad).

### Registro de interacción humana (obligatorio)

```text
texgen_modal_ignore:   interaction_source = HUMAN, automation_policy = NOT_AUTHORIZED
dyndolod_modal_ignore: interaction_source = HUMAN, automation_policy = NOT_AUTHORIZED
```

Los dos modales pasan a **evidencia contractual candidata** para #661/H19 ( fingerprints completos en los JSON y `comparison.json`). NO constituyen allowlist productiva ni autorizan automatización de `Ignore` en unattended.

### Warnings oficiales (§17)

| Warning | Clasificación |
|---|---|
| previous TexGen output present (stitched object LOD) | **observed modal** (fingerprint 3/3) |
| DynDOLOD.DLL NG not found | **observed modal** (fingerprint 2/2) |
| older output detected in game data folder | not observed |
| LOD billboard(s) not found | not observed (requiere generación → P0-B) |

### §18 RealTimeLog

`REALTIMELOG_SUPPORTED = YES` (documentación oficial del switch `-RealTimeLog`, ya citada por #593). No activado durante P0-A; su uso se evalúa en P0-B.

### §19 Stale

`STALE_OBSERVED = NO` — 0 ocurrencias en las 4 rondas completas; el único `COMError` observado es el esperado de ValuePattern.

---

## Respuesta al objetivo (§6)

Para los builds medidos (Alpha-209, SHA `0939BC8F…`/`B67625EB…`):

- **Lectura** del campo Output: determinista (ValuePattern/legacy readback, TEdit único por estructura).
- **Escritura** del campo Output con readback exacto: **sólo por `LegacyIAccessiblePattern`** — `ValuePattern` falla con `COMError` reproducible 4/4.
- **Identidad**: estable en clase/estructura/patterns; AutomationId/HWND son por-instancia (la identidad contractual no debe usarlos como clave).
- **Inicio/cierre de generación** (Start, Advanced→OK, Exit TexGen, Save & Exit): inventariados los disponibles; los de ciclo terminal NO medidos en P0-A — requieren P0-B.

**P0-A no concluye P0_PASS ni P0_BLOCKED sobre la cadena completa: eso corresponde al veredicto de P0-B** (begin exactly-once, terminales, modales durante generación). La mitad de configuración de la cadena está medida y es determinista con mecanismo Legacy; la mitad de acción está inventariada pero no ejercitada.

## P0-B — estado y gate

- `P0_B_PRODUCT_PIPELINE_BLOCKED` — los tres defectos del runner siguen OPEN.
- `P0_B_PROBE` queda **pendiente de autorización humana explícita** (§23). El silencio no autoriza.
- Acciones requeridas en P0-B-probe: Start (TexGen), Advanced → OK (DynDOLOD), Exit TexGen / Save & Exit, observación de modales de generación, métricas §26.

## Archivos

| Archivo | Contenido |
|---|---|
| `environment.json` | entorno, builds, gate del runner, presets iniciales |
| `texgen/texgen_round1_195510.json` | ronda 1 (TfrmMain temprano) |
| `texgen/texgen_round2_195829.json` | ronda 2 (modal capturado, sin interacción) |
| `texgen/texgen_round3_202901.json` | ronda 3 (cadena completa + experimentos) |
| `texgen/texgen_round4_203058.json` | ronda 4 (repetibilidad) |
| `dynodlod/dynodlod_round1_203232.json` | ronda 1 (cadena completa + experimentos) |
| `dynodlod/dynodlod_round2_203416.json` | ronda 2 (repetibilidad) |
| `comparison.json` | comparación entre rondas + registro de interacción humana |
| `final-report.md` | este informe |
| `probe/p0a_probe.py` | sonda standalone reutilizable |

## Puntos no verificados / límites

- `Exit TexGen`, `Save & Exit`, `OK` (post-Advanced), modales de generación y `LOD billboard(s) not found`: no medidos (requieren generación → P0-B).
- La precedencia del preset al entrar en Advanced (DynDOLOD) no se re-midió: invocar Advanced está prohibido en P0-A; la evidencia vigente sigue siendo T5-v2 2026-09-10.
- Los HRESULT se registran tal cual (COMError -2146233079 = 0x80131609, excepción .NET subyacente del provider); la causa interna del rechazo de ValuePattern no se infirió — no hay evidencia que la establezca.
- La sonda no valida la unicidad de `#32770` como "modal" en el árbol de la ventana contractual (los controles internos del diálogo anidado se capturan vía el top-level adicional); evidencia suficiente en este rig, refinar en P0-B si hace falta.

## `P0_A_COMPLETE_NEEDS_P0_B` — STOP
