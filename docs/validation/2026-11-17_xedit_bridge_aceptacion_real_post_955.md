# Aceptación real de xEdit Bridge sobre el código post-#955 (HITL real + SSEEdit 4.1.5f)

**Fecha:** 2026-11-17 · **Dictamen:** `XEDIT_BRIDGE_ACCEPTED_WITH_LIMITATIONS`
**Código probado:** `origin/main` = `db85bd581537f906f166ec0cacafeedf9ee17d02` (contiene #945 `f6136f3b`, #938 `27cdb864` y #955 `985e5ade`). CI de ese SHA: Pytest, Mypy y Windows 3.11/3.12 en `success`; el job `Build` seguía `in_progress` al medir (commit solo de documentación).
**Reemplaza, no modifica:** `2026-11-15_xedit_bridge_aceptacion_real_post_945.md` y su evidencia (sobre `28ce8345`, anterior a #955) quedan congeladas como historia.

## 1. Qué se ejecutó
- Se creó un worktree desviado en `db85bd58` (`.worktrees/qa954-real-post955`, sin mezclar ramas) y se le copió el harness **endurecido** del PR #954 tal como está commiteado en `b55eae9d` (SHA-256 del blob `3b2a8ed167dc840ebb27be3b4292e3075d8793155e2f732b384db6ad5bbe8d8c`). El resultado registra `sky_claw` importado desde ese worktree.
- Lab QA **nuevo y descartable**, `G:\Modding\SSEDIT\QA-20261117\` (instalación oficial `prepare_installation.py` + `verify_installation.py` → `PASS_NEW_INSTALLATION`, suite 37 s, gate PASS, T1 nativo real 11 s) con su propio `Admitidos` vacío y una instancia MO2 QA propia (`MO2QA`, perfil `QA`). No se reutilizó nada del lab de 2026-11-15.
- Camino de producción: `AsyncToolRegistry.execute` → `GestorDeAdmisionesNativas` → `HITLGuard` (respondedor programático, `respond(request_id, bool)`) → `XEditBridgeService` → `run_mcp.py` → SSEEdit. En `origin/main` el gestor pasa `requerir_contexto=True` a `service.admitir` (`xedit_bridge_tools.py:296`); el harness no llama a `service.admitir` directamente (anclado por un test AST).

## 2. Identidades
| Componente | Identidad |
|---|---|
| SSEEdit | 4.1.5.0, SHA-256 `ccb7ca9e6d826868b186b0b9a0fc012affb3fdc9688250583353e6d84e6a2545` (copia del lab = original), sin firma |
| Bridge | 1.1.0 (`bridge_version` en cada veredicto); paquete `xedit-bridge-1.1.0-local-20261111.zip` `48f633c5…e169` |
| Plugins QA | AuditBase.esm, T0_ValidCandidate.esp (`36DD1E63B48F6E3F`), T1_InvalidReference.esp (`AE8B2E15F3C803BD`), T5_LegitimateOverride.esp; masters de juego copiados con hash verificado. Sin binarios en Git |

## 3. Resultado del oráculo: 19/19 `PASS`, salida 0, `veredicto_global: PASS`
| Escenario | Tipo | Evidencia |
|---|---|---|
| aislamiento | real | MO2 `…\QA-20261117\MO2QA\Bin`, bridge del lab nuevo, perfil `QA` |
| T1 denegación | real | `rechazada`/`denied`, `respond(False)` aceptado, `Admitidos` sin cambios |
| T2 aprobación | real | `admission_id` `20261117-183052-50483a`; 6 `copiados`; copia física nueva con el SHA-256 del fixture (`36dd1e63…`); contexto aprobado == ejecutado |
| T3 auditoría limpia | real | `success=true`, `aprobado=true`, 0 errores, `run_id` `20261117_183058_83fa68`, log SHA-256 `405fc692…0176`, 11,5 s |
| T3 admisión + auditoría con hallazgo | real | `20261117-183110-001968`; `success=true`, **`aprobado=false`**, 1 error, FormID `05000800`, `run_id` `20261117_183115_b8a77e`, log `6005100d…7780`, 10,9 s |
| T4 no admitido | real | rechazo sin `run_id` ni corrida nativa nueva |
| T4 inexistente | real | `fallida`: "NoExiste_QA.esp not found… profile 'QA'", nada copiado |
| T4 `respond` con no-bool | real | `TypeError` ×3, la solicitud seguía pendiente (#945) |
| T4 timeout HITL | sintética | `rechazada`, decisión `timeout` (guard real con timeout 2 s) |
| T4 contexto cambiado | sintética | aprobación entregada pero `fallida` por cambio de contexto, nada copiado |
| T4 admisión ambigua | sintética | `ok=true` sin `admission_id`: `ambigua`, **1 sola llamada** al bridge |
| T4 MCP inválida ×6 + intactos | sintética | los 6 casos fallan cerrado (`success=false`, `aprobado=false`); `Admitidos` intacto |

"Sintética" = se sustituye un único punto, descripto en `doble` del JSON; el parseo del cliente es el real. Cada escenario lo juzgó el oráculo con criterios explícitos (no por presencia de claves).

## 4. Integridad antes/después
Inventario SHA-256 de `Laboratorio\` histórico (30 413 archivos), `SSEEdit 4.1.5f\`, `~/.sky_claw/config.toml`, y nombre+tamaño+mtime de 16 309 archivos del Skyrim de Steam:
- **0 altas, 0 bajas, 0 cambios** en ambos inventarios (no hubo VM de VMware activa esta vez). Un `ModOrganizer.exe` de otro rig (`E:\SkyClaw_R0_Rig`) estaba abierto: ajeno al lab y a esta QA.
- Efecto esperado del procedimiento oficial: pruebas de integridad en `evaluacion-20261031-ia\…\evidence\` (fuera de `Laboratorio\`).
- Hashes: `inventario-antes.json` `b58026cd…36ba`, `inventario-despues.json` `43d28cd9…600d`, `admissions.json` del lab `0455ef91…5ab9`, `resultado-harness.json` `451b93ad…a59f`, `verify-installation.stdout.txt` `454ca080…9dcd`. Evidencia en `docs/validation/evidence-xedit-bridge-real-post955-20261117/`.

## 5. Limitaciones (motivo de "con limitaciones")
- **Sin LLM real en el lazo:** `respond()` lo llamó el harness; el tramo Telegram/DeepSeek no se repitió sobre el código post-#955.
- Una admisión real por plugin sobre fixtures mínimos; no certifica mods grandes, MO2 en ejecución ni concurrencia de admisiones.
- El job `Build` de CI de `db85bd58` no había terminado al medir (los de Pytest/Mypy sí).
- El harness endurecido y sus tests viven en el PR #954, no en `main`.

## 6. Rollback
No fue necesario. Quedan intactos `G:\Modding\SSEDIT\QA-20261117\` y `QA-20261115\` (con ACL de denegación del instalador: hay que quitarlas antes de borrar). El worktree temporal `.worktrees/qa954-real-post955` puede eliminarse sin perder nada: el harness está commiteado en la rama del PR.
