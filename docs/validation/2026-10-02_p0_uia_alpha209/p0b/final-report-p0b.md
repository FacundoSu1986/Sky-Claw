# P0-B — Cadena de acción UIA completa sobre TexGen + DynDOLOD Alpha-209 (#661)

> **Fecha:** 2026-10-02/03 · **Modo: `P0_B_PROBE` (autorizado)** · **`P0_B_PRODUCT_PIPELINE` NO ejecutado**
> **Veredicto: `P0_PASS`**

## Precondición 0 — worktree principal preservado

```text
PRIMARY_WORKTREE_PRESERVED = YES
primary branch : feat/runtime-vault-s4e-production-wiring
probe worktree : C:\Worktrees\Sky-Claw-p0-alpha209-uia
probe branch   : research/dyndolod-p0-alpha209-uia
probe base SHA : 0103ee4f6de15207032d25c254ede5cf2c01bff9
```

Ningún cambio en `sky_claw/` ni en `tests/`. El runner productivo no se tocó ni se ejecutó.

---

## A. Baseline

| Elemento | Valor |
|---|---|
| `origin/main` | `0103ee4f6de15207032d25c254ede5cf2c01bff9` (#666; contiene #664/H3-A y #662) |
| TexGen SHA-256 | `0939BC8F8CBAE2E1B38F17D56FECD1B7A941DADE0554E944886BD7F5C4807A62` (35.794.432 B, v3.0.0.209) |
| DynDOLOD SHA-256 | `B67625EB7815111BA9AC232C626FF88B07BB64FF30FF5183C5A04B5F045C3BD0` (35.794.432 B, v3.0.0.209) |

**§5 preflight por corrida**: ambos SHA verificados antes de lanzar = idénticos a P0-A ⇒ **sin `BUILD_DRIFT`**; PID nuevo; root temporal born-empty; cadena de ancestros sin reparse point; residuos previos = 0.

Roots: `%TEMP%\SkyClaw-P0B-<uuid>\{TexGen,DynDOLOD}` — born-empty, `sin_reparse_en_cadena=true`.

## B. Config writer (mecanismo medido, sin fallback)

| Mecanismo | Resultado P0-A | Uso en P0-B |
|---|---|---|
| `ValuePattern.SetValue` | **FAIL** (`COMError -2146233079`) | **no invocado** (§4: sin fallback) |
| `LegacyIAccessiblePattern.SetValue` | **PASS** | writer único de ambas cadenas |

Ambos readbacks exactos al root temporal gestionado. En DynDOLOD hubo **reconvergencia obligatoria** tras `Advanced` (§14) — ver D.

## C. Cadena de acción TexGen (`p0b/texgen/texgen_p0b.json`)

| Fase | Evidencia |
|---|---|
| Modal arranque | `#32770`/`TexGen` — *"Found stitched object LOD textures…"* · `Ignore`/`CommandButton_5` + `Exit TexGen`/`CommandButton_3` ⇒ **fingerprint MATCH** contra P0-A; `Ignore` = **HUMAN**, `automation_policy=NOT_AUTHORIZED` |
| Output | `LegacyIAccessible.SetValue` → `S_OK`; readback = root temporal exacto |
| **Start** | `TButton`, enabled, RuntimeId `[42,3016382]`, `InvokePattern` ⇒ **Invoke exactly-once → S_OK** |
| Confirmación Begin | 3 señales independientes: `output_growth=True`, `ui_change=True`, `process_alive=True` ⇒ **confirmado** |
| Generación | 1 → 1362 archivos, ~102 s; **cero mutación UI** durante toda la fase |
| Completion | diálogo `#32770` *"Exit TexGen, zip and exit, check log or restart?"* — botones `Exit TexGen`/`CommandButton_7`, `Zip and Exit`/`CommandButton_11`, `Check log`/`CommandButton_2`, `Restart`/`CommandButton_4` |
| **Exit TexGen** | `Exit TexGen` **exacto y único** (variante Zip presente pero NO usada) ⇒ **Invoke exactly-once → S_OK** |
| Proceso | `exit_code=0`; residuos = 0 |
| Log (§23) | `C:\Modding\DynDOLOD RigTest\Logs\TexGen_SSE_log.txt` (log de ESTA corrida, mtime 21:20:15): `Using Output Path: C:\Users\<USER>\AppData\Local\Temp\SkyClaw-P0B-6a0d956a-9f3d-46c2-bc59-dec41c7ff406\TexGen\` + `[01:32] TexGen completed successfully`. Nota: `log_markers` de `texgen_p0b.json` quedó vacío por un límite de la sonda (busca logs bajo el dir del exe, no en `Logs\`); la corroboración §23 se hizo manualmente contra el archivo real citado arriba |

Timings (s): launch 0.1 · wizard 13.2 · output_set 14.1 · begin 15.1 · terminal 117.0 · exit_invoke 130.3 · process_exit 131.7.

## D. Cadena de acción DynDOLOD (`p0b/dyndolod/dyndolod_p0b.json`)

| Fase | Evidencia |
|---|---|
| Modal arranque | `#32770`/`DynDOLOD` — *"DynDOLOD.DLL from DynDOLOD DLL NG and Scripts not found!"* ⇒ **MATCH**; `Ignore` = **HUMAN** |
| Output (wizard simple) | `S_OK` + readback exacto |
| **Advanced >>>** | `TButton`, RuntimeId `[42,7210916]` ⇒ **Invoke exactly-once → S_OK** |
| **§14 recarga de preset** | **`ADVANCED_RELOADED_OUTPUT_FROM_PRESET = YES`** — el campo volvió a `E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD` (el `OutputPath` del preset Default) ⇒ **reconvergencia** obligatoria: `LegacyIAccessible.SetValue` → `S_OK` + readback exacto al root gestionado |
| **Begin (OK)** | `TButton` "OK" enabled, RuntimeId `[42,2623612]`, en la ventana contractual (**nunca** un `OK` de `#32770`) ⇒ **Invoke exactly-once → S_OK** |
| Confirmación Begin | `output_growth` + `ui_change` + `process_alive` ⇒ **confirmado** |
| Generación | 25 → 5102 archivos; plugins `DynDOLOD.esm` (141.665 B), `DynDOLOD.esp` (258.679 B), `Occlusion.esp` (9.360.114 B) |
| Completion | diálogo *"Save DynDOLOD plugins, save plugins and zip output, exit DynDOLOD without saving, check the log?"* — botones `Save and Exit`, `Save, Zip and Exit`, `Exit DynDOLOD`, `Check log` |
| **Save and Exit** | `CCPushButton`/`CommandButton_6` exacto y único (variantes Zip/Exit NO usadas) ⇒ **Invoke exactly-once → S_OK** |
| Proceso | `exit_code=0`; residuos = 0 |
| Log (§23) | `C:\Modding\DynDOLOD RigTest\Logs\DynDOLOD_SSE_log.txt` (log de ESTA corrida, mtime 21:30:58): `Using Output Path: C:\Users\<USER>\AppData\Local\Temp\SkyClaw-P0B-0af6f451-cd59-457d-8168-eddc6979c178\DynDOLOD\` + `[03:59] DynDOLOD plugins generated successfully` + `[03:59] Occlusion.esp completed successfully` + `[04:13] User says "Save and Exit"` + `Saving …DynDOLOD.esm/.esp/Occlusion.esp` **dentro del root gestionado**. Mismo límite de sonda que TexGen; corroboración manual contra el archivo real |

Timings (s): launch 0.2 · wizard 17.3 · output_set 17.5 · advanced 17.8 · begin 22.5 · terminal 267.0 · save_exit 275.8 · process_exit 281.2.

## E. Modales por fase (§25)

| Tool | Fase | Clase | Instrucción | Botones | Interacción |
|---|---|---|---|---|---|
| TexGen | startup | `#32770` | *Found stitched object LOD textures…* | `Ignore`, `Exit TexGen`, `Cerrar`, scroll | **HUMAN Ignore** |
| TexGen | terminal | `#32770` | *Exit TexGen, zip and exit, check log or restart?* | `Exit TexGen`, `Zip and Exit`, `Check log`, `Restart` | agente (exact-once) |
| DynDOLOD | startup | `#32770` | *DynDOLOD.DLL … not found!* | `Ignore`, `Exit DynDOLOD`, `Cerrar`, scroll | **HUMAN Ignore** |
| DynDOLOD | terminal | `#32770` | *Save DynDOLOD plugins, save plugins and zip output…* | `Save and Exit`, `Save, Zip and Exit`, `Exit DynDOLOD`, `Check log` | agente (exact-once) |

Ningún modal inesperado durante generación ⇒ la rama fail-closed **no se ejercitó**. Ninguno recibe policy de `continue`; los de arranque sólo son evidencia contractual candidata (#661/H19).

## F. Ledger exactly-once (§20)

| action_id | tool | build_sha256 | invoke_attempted | invoke_result |
|---|---|---|---|---|
| `texgen_start` | texgen | `0939bc8f…` | true | `S_OK` |
| `texgen_exit` | texgen | `0939bc8f…` | true | `S_OK` |
| `dyndolod_advanced` | dyndolod | `b67625eb…` | true | `S_OK` |
| `dyndolod_begin_ok` | dyndolod | `b67625eb…` | true | `S_OK` |
| `dyndolod_save_exit` | dyndolod | `b67625eb…` | true | `S_OK` |

5 acciones, 5 invocaciones, **0 reintentos, 0 ambigüedades**.

## G. Timings / métricas pasivas (#654)

| Métrica | TexGen | DynDOLOD |
|---|---|---|
| launch → wizard | 13.2 s | 17.3 s |
| wizard → Output set | 0.9 s | 0.2 s |
| Output set → Begin | 1.0 s | 5.0 s |
| Begin → diálogo terminal | 101.9 s | 244.5 s |
| terminal → process exit | 1.4 s | 5.4 s |
| **total** | **131.7 s** | **281.2 s** |
| stdout / stderr capturados | **0 / 0 B** | **0 / 0 B** |

Los binarios GUI **no escriben nada en stdout/stderr**: toda la evidencia observational vive en `<exe>\Logs\{Tool}_SSE_log.txt` (DynDOLOD 3.871.138 B; debug 101.804.764 B). No se modificó ningún timeout, buffer ni `-memory`.

## H. Stale (§24)

`UIA_E_ELEMENTNOTAVAILABLE` / `0x80040201`: **0 ocurrencias** en las 3 corridas de P0-B (y 0 en las 6 de P0-A). Único `COMError` observado: el esperado de `ValuePattern`. H3-C no implementado.

## I. Limpieza (§26)

| Comprobación | Resultado |
|---|---|
| TexGen PID | ninguno |
| DynDOLOD PID | ninguno |
| LODGen residual | ninguno |
| probe residual | ninguno |
| procesos ajenos matados | **0** (el probe sólo termina el proceso que él mismo lanzó) |

**Estado persistido del usuario (efecto lateral medido y revertido):**

| Preset | Antes | Tras la corrida | Restaurado |
|---|---|---|---|
| `DynDOLOD_SSE_TexGen.ini` | `394e5044…` (1749 B) | `138e8783…` (1737 B) | **SÍ** — reconstruido y verificado por SHA `394e5044…` |
| `DynDOLOD_SSE_Default.ini` | `e728f1f3…` (24061 B) | `ac5d0bfd…` (24107 B) | **SÍ** — restaurado byte-exacto `e728f1f3…` |

Ambas herramientas reescriben su preset al iniciar la corrida (comportamiento ya medido en T5-v2 2026-09-10; el delta del preset de TexGen fue exclusivamente la línea `OutputPath`). Copias: `preset_TexGen_ORIGINAL_restaurado.bin`, `preset_TexGen_POSTRUN.ini`, `PRE_Default.ini`, `PRE_TexGen.ini`.

## J. Runner gates — sin cambios

```text
RUNNER_P1_PACKAGING_CANCEL = OPEN
RUNNER_P1_REPARSE_COPY     = OPEN
RUNNER_P2_DOUBLE_CANCEL    = OPEN
→ P0_B_PRODUCT_PIPELINE_BLOCKED (permanece; no se ejecutó el pipeline productivo)
```

## K. Estado final

```text
P0_PASS
```

Cumplido para los builds exactos medidos (`0939BC8F…` / `B67625EB…`): Output write + readback exacto · config observable (incluida la recarga de preset al entrar en Advanced, con reconvergencia) · Begin exactly-once · generación observable · completion demostrable por diálogo terminal · terminal exactly-once · process exit 0 · 0 residuos — todo mediante **mecanismos públicos de UI Automation** (`LegacyIAccessiblePattern.SetValue` + `InvokePattern.Invoke`).

`P0_PASS` **no** declara sano el pipeline transaccional: `P0_B_PRODUCT_PIPELINE_BLOCKED` sigue vigente por los tres defectos OPEN.

## L. Próximo paso recomendado (nada implementado)

1. **Driver unattended**: implementar P1–P9 de #661 con writer **seleccionado por contrato de build** (`(tool, exe_sha256) → LEGACY_IACCESSIBLE`), nunca por fallback en runtime.
2. **P1 (preflight de modales)**: los dos fingerprints de arranque quedan como *evidencia candidata*; automatizar uno exige decisión explícita del operador (hoy `Ignore` es `NOT_AUTHORIZED`).
3. **P5 (convergencia pre-Begin)**: el caso `ADVANCED_RELOADED_OUTPUT_FROM_PRESET=YES` es exactamente el drift que ese gate debe cortar — reconvergencia medida aquí, automatizable.
4. **Presets (P8)**: restaurar el preset porBytes es requisito de contrato, no opcional: ambas herramientas lo reescriben.
5. **#654**: stdout/stderr son 0 bytes para estos binarios ⇒ la atribución por log debe apoyarse en `Logs\{Tool}_SSE_log.txt`, que **append** entre sesiones (se observaron líneas de corridas previas) — la firma por prefijo del runner es load-bearing.

**STOP.**