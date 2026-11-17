# Aceptación real de xEdit Bridge posterior a #945 (HITL real + SSEEdit 4.1.5f)

**Fecha:** 2026-11-15 · **Dictamen:** `XEDIT_BRIDGE_ACCEPTED_WITH_LIMITATIONS`
**SHA probado:** `28ce8345a6dffb1bbf4773229fa5d229e9a432b0` (CI de ese SHA en verde; contiene #945 `f6136f3b` y #938 `27cdb864`).
**Alcance:** certificar comportamiento e integridad. No se modificó código de producción. Plan autorizado por el propietario (ver `2026-11-15_xedit_bridge_llm_real_plan_gate.md`).

## 1. Identidades
| Componente | Identidad |
|---|---|
| SSEEdit | 4.1.5.0, SHA-256 `ccb7ca9e6d826868b186b0b9a0fc012affb3fdc9688250583353e6d84e6a2545` (copia del lab QA = original), sin firma |
| Bridge | `xedit_bridge` 1.1.0 (`bridge_version` en los veredictos), paquete `xedit-bridge-1.1.0-local-20261111.zip` SHA-256 `48f633c5…e169` |
| Lab QA | `G:\Modding\SSEDIT\QA-20261115\inst\Laboratorio` (instalación nueva con `prepare_installation.py` + `verify_installation.py`: `PASS_NEW_INSTALLATION`, suite 37 s, gate 25 controles PASS, T1 nativo real 13 s) |
| MO2 QA | `QA-20261115\MO2QA` (portable 2.5.2, perfil `QA`, `ModOrganizer.exe` SHA-256 `442B354A…E622`), copia física, sin enlaces |
| Plugins QA | AuditBase.esm `1BC7D730`, T0_ValidCandidate.esp `36DD1E63`, T1_InvalidReference.esp `AE8B2E15`, T5_LegitimateOverride.esp `7F0D8518` (16 primeros hex; tamaños 168–226 B). Masters de juego copiados del lab nuevo (hash verificado). No se versionan binarios. |

## 2. Camino ejercitado
`AsyncToolRegistry.execute` → `GestorDeAdmisionesNativas` → `HITLGuard` (respondedor programático con `respond(request_id, bool)`) → `XEditBridgeService` → `run_mcp.py` del bridge → SSEEdit. Harness opt-in: `scripts/qa/xedit_bridge_real_acceptance.py`. Sandbox del `PathValidator` = solo `QA-20261115`; el harness aborta si el MO2/bridge/perfil resueltos no son los QA (resultado: `MO2QA\Bin`, `…\inst\Laboratorio\BridgeTrabajo`, perfil `QA`).

## 3. Matriz de pruebas
| Prueba | Tipo | Resultado |
|---|---|---|
| Aislamiento de contexto | real | OK: MO2, bridge y perfil son los QA |
| T1 denegación (`respond(False)`) | real | estado `rechazada`, decisión `denied`; `Admitidos` idéntico al baseline (0 archivos nuevos) |
| T2 aprobación (`respond(True)`) | real | `exitosa`; `admission_id` `20261115-200956-486e92`; `copiados` = 6 (5 masters + T0); contexto aprobado == ejecutado (`perfil «QA»`, `MO2QA\Bin`); copia solo en el `Admitidos` del lab QA |
| T3 auditoría T0 válido | real | `success=true`, `aprobado=true`, 0 errores, `run_id` `20261115_201007_480dd6`, integridad PASS, `Admitidos` intacto, 13 s |
| T3 admisión + auditoría T1 con hallazgo | real | admisión `20261115-201021-26d4a3`; `success=true`, **`aprobado=false`**, 1 hallazgo LVLI FormID `05000800` (`[0500DEAD]` no resuelto), `run_id` `20261115_201029_926e80`: distingue `success` de `aprobado` |
| T4 plugin no admitido (T5) | real | `success=false`, `no_admitido=true`, sin lanzar SSEEdit (0,19 s) |
| T4 plugin inexistente | real | `fallida`, "not found in … MO2 profile 'QA'", nada copiado |
| T4 `respond` con no-bool (1, "true", None) | real | `TypeError` ×3; la solicitud seguía pendiente (#945 confirmado) |
| T4 timeout HITL | sintética | `HITLGuard` real con timeout 2 s: `rechazada`, decisión `timeout`, nada copiado |
| T4 contexto cambiado | sintética | se muta el perfil tras el pedido: aprobación entregada (`ack=true`) pero `fallida`, mensaje "el contexto … cambió", nada copiado |
| T4 admisión ambigua | sintética | `ok=true` sin `admission_id`: `estado_admision=ambigua`, **1 sola llamada** al bridge (sin reintento), nada copiado |
| T4 respuesta MCP inválida ×6 | sintética | línea no JSON, `isError` incoherente, `ok` no booleano, servidor ajeno, id duplicado, veredicto sin datos: los 6 → `success=false`, `aprobado=false`; `Admitidos` intacto |

Sintética = se sustituye un único punto (descripto en `doble` del JSON); el parseo del cliente del bridge es el real. Una primera versión del caso MCP inválido apuntó mal (doble sobre la capa tipada, `AttributeError` por mi doble, no por producción); se corrigió y se repitió solo ese grupo (`--solo mcp_invalida`).

## 4. Integridad antes/después
Inventario SHA-256 de `Laboratorio\` (lab histórico, 30 415 archivos), `SSEEdit 4.1.5f\`, `~/.sky_claw/config.toml`, y nombre+tamaño+mtime de 16 309 archivos del Skyrim de Steam:
- Sin cambios: SSEEdit, `Admitidos` histórico, `local-gate.json`, `config.toml`, Steam (0 altas/bajas/cambios).
- 4 altas/cambios, todas en `Laboratorio\VMs\` (`*.lck`, `.vmem`, `.vmdk`, `vmware.log` de la VM `P7-A5-Compat-20261115`): archivos vivos de una VM de VMware ajena a esta QA que seguía abierta al medir. **No los generó el bridge ni el harness**; quedan declarados y no se tocaron.
- Efecto esperado del procedimiento oficial: `prepare_installation`/`verify_installation` escriben pruebas de integridad en `evaluacion-20261031-ia\continuaciones\STABLE-20261103-04\evidence\` (fuera de `Laboratorio\`, no cubierto por el inventario).
- Hashes de evidencia: `inventario-antes.json` `431511f2…4f94`, `inventario-despues.json` `1e272206…d305fd`, `run.log` T0 `ecf881a5…d197`, `run.log` T1 `23fa1e2c…d005`, `admissions.json` del lab QA `d7c6f3c2…dcff`, `resultado-harness` run1 `80fd8560…6ea1`, run2 `3cd99b72…390`.

## 5. Incidencias y observaciones
1. El aviso al operador identifica la instancia como «Bin» (carpeta del `ModOrganizer.ini` portable), no por un nombre de instancia: en instancias portables es ambiguo para una persona. Observación, no defecto de seguridad (el contexto exacto con ruta sí se muestra y se verifica).
2. `prepare_installation` aplica ACL de denegación a copias del lab nuevo; borrarlo exige quitar esas ACL.
3. VM ajena activa durante la medición (ver §4).

## 6. Limitaciones y riesgos residuales (motivo del dictamen)
- **No hubo LLM real decidiendo la invocación**: `respond()` lo llamó el harness; el tramo Telegram/DeepSeek no se repitió en esta ronda (sí en la ronda 2026-11-12 previa a #945).
- Dos admisiones reales exitosas (`T0_ValidCandidate.esp` y `T1_InvalidReference.esp`, con `admission_id` distintos y su auditoría) sobre fixtures sintéticos mínimos: no certifica mods grandes ni MO2 en ejecución.
- Los cambios de la VM ajena en `Laboratorio\VMs\` impiden afirmar "cero cambios" en todo `Laboratorio\`; sí en todo lo que pertenece al bridge.
- No se midió concurrencia de dos admisiones.

## 7. Rollback
No fue necesario. Quedan intactos para revisión: `G:\Modding\SSEDIT\QA-20261115\` (lab, MO2 QA, evidencias). Eliminarlos queda a decisión del propietario, quitando antes las ACL de denegación.

## 8. Adenda 2026-11-17 — oráculo del harness (cierre adversarial del PR #954)
- La ejecución real de §3 y sus JSON (`evidence-xedit-bridge-llm-real-20261115/`) son **evidencia histórica congelada sobre `28ce8345`** (anterior a #955). Se produjeron con la primera versión del harness, que registraba datos pero **no emitía veredicto**: las conclusiones de §3 son una lectura manual de esos datos. Los JSON no se modificaron (SHA-256 `80fd8560…6ea1`, `3cd99b72…390`, `b442bab2…92b4`).
- El harness actual añade un oráculo explícito por escenario (`PASS`/`FAIL` con `fallas`), código de salida ≠ 0 ante cualquier FAIL, excepción o escenario requerido ausente, escritura atómica del resultado tras cada escenario, registro parcial (con `admission_id` y estado) si un escenario falla después de una admisión terminada, y rechazo a sobrescribir evidencia existente.
- `tests/test_qa_xedit_bridge_acceptance_oraculo.py` (22 tests, sin SSEEdit) demuestra con dobles que el oráculo detecta: denegación que termina en éxito, aprobación sin `admission_id`, copia que no es el fixture, plugin inválido declarado aprobado, respuesta MCP malformada aceptada y excepción posterior a una admisión terminada. Se verificó por mutación controlada que cada familia de pruebas falla cuando se debilita el oráculo (archivo restaurado, hash idéntico).
- Compatibilidad con #955: el harness solo admite por `admitir_plugin_nativo` → gestor, que en `origin/main` (`db85bd58`) pasa `requerir_contexto=True`; nunca llama a `service.admitir`. Los 22 tests y mypy pasan también sobre ese `origin/main` (copia temporal, ya eliminada).
- **No se ha re-ejecutado la aceptación real sobre el código post-#955**: este informe no declara `XEDIT_BRIDGE_ACCEPTED`.
