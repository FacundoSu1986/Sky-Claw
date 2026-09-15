# Gate UIA de Output (T5-v2.1) — rig limitado con binarios reales 2026-09-15

> **Audiencia:** maintainers, reviewers y agentes.
>
> **Estado:** evidencia del fix de Fase 3 sobre el PR #590 (draft). Cubre el
> protocolo de readiness post-fix sobre TexGen/DynDOLOD Alpha-209 reales:
> initial MISMATCH → HITL → corrección humana → final MATCH, y final mismatch
> fail-closed. **No** cierra #528 ni el PR #590; **no** reitera los criterios
> 1–10 del checklist T5-v2 (ya cerrados por el rig de PR-2, T5-v2 10/10 en
> `2026-09-13_pr580_real_rig/final-report.md`).
>
> **Alcance:** etapas del wizard (sin Start, sin generación de LOD). La corrida
> se cancela al recibir el aviso "podés continuar con Start"; el runner mata el
> árbol y no queda residuo.

## Qué cambió y qué prueba este rig

El rig anterior (T5-v2, 2026-09-10) midió que TexGen arranca mostrando el
`OutputPath` del preset rancio, y que la corrección la hace el operador a mano.
El candidato previo **bloqueaba el proceso ante ese MISMATCH inicial** y hacía
imposible el flujo documentado. El fix de Fase 3 separa:

- **Initial readiness** (`clasificar_initial`): `MATCH` o
  `MISMATCH`/`OUTPUT_DIFIERE` concluyente (identidad probada, ventana y control
  únicos, valor legible y canonicalizable) → HITL. Todo lo demás → fail-closed.
  El MISMATCH configurable **no autoriza Start**;
- **Final authorization**: tras la confirmación humana se **re-observa desde
  cero**; sólo `EstadoPreflight.MATCH` deja seguir.

Y la observación COM se ejecuta en un **helper descartable** (`--skyclaw-uia-helper`):
`wait_for(to_thread(COM))` no mata el hilo subyacente, así que el deadline del
padre ahora es duro (kill + reap del proceso). La garantía está anclada por el
test de hard-hang (`tests/test_dyndolod_uia_ejecutor.py`), que usa un helper real
bloqueado indefinidamente y exige que ya no exista al volver.

## Environment

| Dato | Valor |
|---|---|
| Windows | 10.0.19045 (Windows 10 Pro 22H2, AMD64), sesión interactiva |
| Python | 3.11.9 (`.venv` del worktree del PR #590) |
| TexGen | `C:\Modding\DynDOLOD RigTest\TexGenx64.exe` — 35.794.432 B — SHA-256 `0939bc8f…7a62` — v3.0.0.209 |
| DynDOLOD | `C:\Modding\DynDOLOD RigTest\DynDOLODx64.exe` — 35.794.432 B — SHA-256 `b67625eb…3bd0` — v3.0.0.209 |
| Data (`-d:`) | `C:\Modding\DynDOLOD RigTest\_rig_test\data` → junction a `G:\rig_dyndolod_rigtest_data` (con DynDOLOD Resources SE 3.00) |
| INI / plugins / temp | `_rig_test\ini` · `_rig_test\plugins.txt` · `E:\Sky-Claw T5 Rig\temp` |
| `external_work_root` | `E:\Sky-Claw T5 Rig\UIA Gate Work` (raíz nueva, para que el preset rancio divergiera del expected) |
| Presets | `DynDOLOD_SSE_TexGen.ini` → `…\PR2 Work\DynDOLOD\TexGen`; `DynDOLOD_SSE_Default.ini` → `…\PR2 Work\DynDOLOD\DynDOLOD` (rancios respecto del work root del rig; hashes idénticos antes/después en las 4 corridas) |
| Binario congelado (smoke) | `dist\SkyClawApp.exe` PyInstaller onefile — 61.687.889 B |

## Método

Harness externo al repo (`E:\Sky-Claw T5 Rig\evidence\2026-09-15_uia_gate_v2\rig_harness.py`)
que conduce el **runner real** con la `CapacidadDeReadinessUIA` productiva:

- ejecutor real: `EjecutorGatePorHelper` (spawn del worker por archivos);
- confirmador de rig que simula al OPERADOR: disuelve modales (`Ignore`) y
  corrige el campo Output con UIA (`LegacyIAccessiblePattern.SetValue`, medido;
  `ValuePattern.SetValue` tira COMError en el `TEdit` de Delphi);
- sin Start: al llegar el aviso final se cancela la corrida y el runner mata el
  proceso; se verifica `procesos_residuales == []` y presets intactos.

Nota sobre los plazos: el default del FINAL gate es corto
(`GATE_UIA_TIMEOUT_FINAL_SEGUNDOS = 30 s`, distinto del inicial de 300 s) porque
después de la aprobación la GUI está idle y la única razón transitoria es un
redraw. El harness fijó 60 s y el final real resolvió en ~1 s, así que ese
margen no es load-bearing para la evidencia.

Nota de honestidad del rig: la primera corrida de DynDOLOD usó el default de
`data_dir` (game tree) por un olvido del harness y el binario mostró el modal
"DynDOLOD Resources SE version information not found"; el harness se corrigió
(`data_dir=_rig_test\data`) y los resultados de abajo son de las corridas
posteriores al fix. El modal fue una condición del arnés, no del producto.

## Resultados

| Caso | Requisito | Observado | Estado |
|---|---|---|---|
| **T-R1** | preset stale inicial observado | `observed_output = E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\TexGen` (initial MISMATCH real) | PASS |
| **T-R2** | INITIAL permite llegar a HITL | confirmador invocado, `solicitud` registrada | PASS |
| **T-R3** | HITL muestra expected correcto | `expected_output = E:\Sky-Claw T5 Rig\UIA Gate Work\DynDOLOD\TexGen` (raíz del layout = `-o:`) | PASS |
| **T-R4** | operador corrige Output a mano | `valor_antes` stale → `valor_despues` expected (mecanismo `legacy`) | PASS |
| **T-R5** | operador aprueba | aprobación procesada; final gate corre | PASS |
| **T-R6** | FINAL gate re-observa y da MATCH | `informe` emitido ⇒ final `MATCH` | PASS |
| **T-R7** | mensaje "podés continuar con Start" | `Output verificado contra la raíz administrada: podés continuar con Start.` | PASS |
| **T-R8** | mismatch deliberado en final → rojo | sin corrección + approve ⇒ `DynDOLODPreflightUIAError` (`MISMATCH`/`OUTPUT_DIFIERE`), proceso muerto | PASS |
| **DynDOLOD +** | initial MATCH real del wizard simple | `observed_output = …\UIA Gate Work\DynDOLOD\DynDOLOD\` (canonicaliza igual al expected) → HITL → final `MATCH` → aviso | PASS |
| **DynDOLOD −** | mismatch deliberado en final | operador inyecta `…\Stale DynDOLOD` + approve ⇒ `DynDOLODPreflightUIAError` (`OUTPUT_DIFIERE`), proceso muerto | PASS |
| **Frozen helper** | el worker del ejecutable congelado | `SkyClawApp.exe --skyclaw-uia-helper <dir>` rc=0 en 4,7 s (arranque en frío), `resultado.json` válido; sin GUI | PASS |

En las 4 corridas: `procesos_residuales == []`, presets con SHA-256 idéntico
antes/después, `UIA Gate Work` sin archivos (no se pulsó Start).

## Raw evidence externa

Vive fuera del repo (máquina del operador) y **no es auditable por CI por sí
sola**:

```text
E:\Sky-Claw T5 Rig\evidence\2026-09-15_uia_gate_v2\
```

| Artefacto | SHA-256 |
|---|---|
| `rig_harness.py` | `529e95b775941309808b4161ca9256e4f9d20c3f689ec55241c9ff9e59e1bcf6` |
| `rig_TexGen_positivo.json` | `351d5936ddd31546c984ceb77c482d2bf9baa8c59755a0c88c301b1ba698bb7f` |
| `rig_TexGen_final-mismatch.json` | `381618b91f4e3b906b092e6ba7bc9ff25b0c329967475057182938b7d9363393` |
| `rig_DynDOLOD_positivo.json` | `502e9b8139b6237c0df6b428aab9fb54f6995979f8b05af417267e4a5e1b2e2a` |
| `rig_DynDOLOD_final-mismatch.json` | `9482f78096c78a91f47fb9b49d27beb81d0352acfeb5adf7c98753f8a683285d` |
| `frozen_smoke.json` | `69b083a2898534c26a50d0d22c5be1142bb146ff85167f60c6be9e4b6a0a71b8` |

## Decisión: Full T5-v2

**NOT REQUIRED para este fix.** El cambio no toca `argv`, `-o:`, `-d:`, el
layout del workspace, el packaging de la salida (criterios 8–9), el handoff ni
el contrato de artefactos: agrega la clasificación del initial, corrige el
texto HITL y mueve la ejecución de la OBSERVACIÓN a un helper descartable. El
único cambio de empaquetado es del bundle de release (PyInstaller
`hiddenimports` + despacho del flag en `__main__`), y quedó smoke-verificado
contra el onefile real (fila "Frozen helper"). El checklist T5-v2 10/10 del rig
de PR-2 sigue vigente.

## Limitaciones

- Wizard únicamente: no se pulsa Start ni se genera LOD; el cierre regular de
  la etapa ya está cubierto por los rigs anteriores.
- Alpha-209 en este entorno; no valida builds futuros.
- La corrección del operador se simuló con UIA (`SetValue`), no con teclado
  físico; equivale funcionalmente a editar el campo a mano.
- El arranque en frío del helper congelado se midió una vez (4,7 s); la gracia
  de arranque congelado (90 s) queda como cota de diseño.
- El modo fuente del helper (`python -m …`) es el que ejercitaron las 4
  corridas reales; el congelado sólo se ejercitó en el smoke del flag.
