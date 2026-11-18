# Misión C — Plan y gate de autorización (aceptación real LLM/HITL/SSEEdit)

**Estado:** BORRADOR de Fase 3, sin ejecución. No se admitió nada ni se lanzó SSEEdit.
**Dictamen del gate:** `QA_REAL_READY_FOR_AUTHORIZATION` (con las salvedades de §9).

## 1. Base evaluada
- Repo `E:\Skyclaw_Main_Sync`, base `origin/main` = `28ce8345a6dffb1bbf4773229fa5d229e9a432b0`.
- CI de ese SHA (Sky-Claw CI/CD, run 43755151096): success (Build, Pytest, Mypy, Ruff, Security, CodeQL, Windows 3.11/3.12, POSIX).
- Contiene #945 (`f6136f3b`, `HITLGuard.respond` con bool estricto) y el hardening #938 (`27cdb864`).
- Rama `qa/xedit-bridge-llm-real-acceptance-20261115` y worktree `.worktrees/xedit-bridge-llm-real-20261115` creados desde ese SHA (no existían).
- Existe el PR #951 abierto (auditoría previa, rama `audit/xedit-bridge-acceptance-20261115`); este informe es otro archivo.

## 2. Fase 0 — hallazgos del laboratorio existente (solo lectura)
| Ítem | Resultado |
|---|---|
| SSEEdit | `Laboratorio\SSEEdit\SSEEdit.exe` 4.1.5.0, SHA-256 `CCB7CA9E6D826868B186B0B9A0FC012AFFB3FDC9688250583353E6D84E6A2545`, sin firma (idéntico a `SSEEdit 4.1.5f\SSEEdit.exe`) |
| Bridge | `xedit_bridge` 1.1.0, `audit_only=true`, `allow_run_pascal=false` |
| `BridgeTrabajo\config.json` | `xedit_exe`, `work_dir`, perfiles T0–T8: todo bajo `G:\Modding\SSEDIT\Laboratorio` |
| MO2 QA existente | `Laboratorio\MO2Prueba` portable (MO2 2.5.2, `ModOrganizer.exe` SHA-256 `442B354A…E622`), perfil activo `AuditB`, juego copiado en `MO2Prueba\Game` (`SkyrimSE.exe` `C4342088…3BE9`) |
| `Admitidos` histórico | `admissions.json` 98 330 B, 52 plugins admitidos (incluye `RetreteVIP.esl`, Skyrim.esm…) → **no reutilizable** para "no admitido previamente" |
| Gate local | `GL-LOCAL-PROSPECTIVE-v2` en PASS (11/11/2026) |
| Procesos | Sin SSEEdit, MO2 ni bridge. **Hay `vmware-vmx` activo** (satura disco; riesgo de timeouts) |
| Enlaces | Sin reparse points en `Laboratorio`, `tools`, `SSEEdit 4.1.5f`; sin hardlinks múltiples en `Admitidos\Data` |
| Referencias a producción | `config.json` sin Steam/Program Files/C:. Pero `~/.sky_claw/config.toml` apunta a `skyrim_path=G:\SteamLibrary\...` e `install_dir=G:\Modding\SSEDIT` |

### Hallazgos de diseño que condicionan el protocolo
1. **La raíz del lab la define la ubicación del paquete** (`labgate.LAB` sale de `Path(__file__)`). Un lab descartable real exige una **instalación nueva** del bridge en otra carpeta, con su propio `Admitidos`, `admissions.json` y gate (período nuevo → autorización del propietario).
2. **El contexto de admisión sale del `path_resolver` de Sky-Claw**, no del bridge (`get_mo2_instance_ini_dir` + `get_active_profile`). Sin entorno explícito, T2 leería el MO2 real del usuario. El harness debe fijar `MO2_PATH`, el perfil, `XEDIT_BRIDGE_ROOT` y un sandbox que no cubra el MO2 real ni Steam, y **abortar si el contexto mostrado al operador no es el QA**.
3. `admitir_plugin_nativo` es un trabajo en segundo plano: el resultado final se lee del `GestorDeAdmisionesNativas`, no de la respuesta de la tool.

## 3. Laboratorio descartable propuesto (a crear solo con autorización)
Raíz: `G:\Modding\SSEDIT\QA-20261115\` (nueva; nada preexistente se reutiliza ni se modifica).
```
QA-20261115\Laboratorio\            # instalación nueva del bridge 1.1.0 (prepare_installation / verify_installation)
  BridgeTrabajo\  Admitidos\  Evidencias\  Temporales\  SSEEdit\ (copia física del exe, mismo SHA)
QA-20261115\MO2QA\                  # instancia MO2 portable nueva
  Bin\ModOrganizer.ini  State\{mods,profiles\QA,overwrite}  Game\  (Game = copia física mínima, sin enlaces)
```
Plugins QA (sin binarios en Git; se copian de `Laboratorio\Fixtures\Canonical` y `Extended\Bridge\Data`, ya conocidos):
| Uso | Plugin | SHA-256 (16 primeros) | Bytes |
|---|---|---|---|
| master | AuditBase.esm | 1BC7D7307BCA7EBF | 174 |
| T2/T3 válido | T0_ValidCandidate.esp | 36DD1E63B48F6E3F | 175 |
| T3 con hallazgo | T1_InvalidReference.esp | AE8B2E15F3C803BD | 226 |
| T4 no admitido | T5_LegitimateOverride.esp (presente en MO2QA, nunca admitido) | 7F0D8518BD93C3FA | 212 |
| T4 inexistente | `NoExiste_QA.esp` (no existe) | — | — |
Los demás T2–T8 quedan disponibles, no se usan en esta ronda.

## 4. Protocolo
- **T1 Denegación:** `AsyncToolRegistry` → `admitir_plugin_nativo(T0_ValidCandidate.esp)`; se captura el `request_id` del `HITLGuard` y se llama `respond(request_id, approved=False)`. Esperado: estado `rechazada`, `Admitidos\Data` y `admissions.json` sin cambios (hash igual), sin `copiados`.
- **T2 Aprobación:** mismo camino, `respond(request_id, approved=True)` (bool estricto). Esperado: contexto aprobado == contexto ejecutado (instancia `MO2QA`, perfil `QA`), `copiados` no vacío, `admission_id` válido, copia física solo en `QA-20261115\Laboratorio\Admitidos`.
- **T3 Auditoría:** `auditar_plugin_nativo(T0_ValidCandidate.esp)` con SSEEdit 4.1.5f real. Verificar `run_id`, `log_path`, FormIDs, hallazgos; distinguir `success` de `aprobado`; hashes de `Admitidos` antes/después iguales. Repetir con T1_InvalidReference (hallazgo esperado, `aprobado=false`).
- **T4 Fallos seguros** (todas **sintéticas**, con dobles, sin dañar el lab): timeout de HITL, contexto cambiado durante la espera, plugin inexistente, plugin no admitido (real, solo lectura), respuesta MCP inválida (harness sintético), admisión ambigua (`ok=true` sin `admission_id`) sin reintento.
- Nota: T1/T2 usan el guard real con un respondedor programático; no usan Telegram ni DeepSeek (no se leen claves del keyring). El tramo "LLM decide invocar la tool" queda como **limitación declarada** salvo que autorices además una ronda con el bot (ver §9).

## 5. Archivos que se crearían / modificarían
Crear: todo `G:\Modding\SSEDIT\QA-20261115\**`; en el worktree: este informe, el harness opt-in `scripts/qa/` (o `tests/qa_real/`, marcado opt-in) y el informe final `docs/validation/...`.
Modificar: nada de producción, nada de `Laboratorio\` existente, nada en MO2 real, Steam, Golden Master ni Frozen Runtime. Variables de entorno solo dentro del proceso del harness.

## 6. Baseline de integridad (antes de ejecutar)
SHA-256 + tamaño + atributos de: `Laboratorio\**` existente (inventario completo), `SSEEdit 4.1.5f\SSEEdit.exe`, `MO2Prueba\**`, `Admitidos\admissions.json`, `local-gate.json`, `~/.sky_claw/config.toml`, carpeta del juego Steam (solo lectura, inventario de nombres+tamaños+mtime) y el MO2 real. Se repite al final y se compara; todo cambio fuera de `QA-20261115\` detiene la ejecución.

## 7. Riesgos
- Copiar masters reales de Skyrim al lab nuevo (~400 MB) si el instalador del gate los exige; origen solo lectura.
- `vmware-vmx` activo: pedir pausarlo (timeout MCP de 80 s).
- El período de gate nuevo es autorización explícita del propietario (regla del lab).
- Proceso de SSEEdit real: único componente que escribe logs; confinado al `work_dir` nuevo.
- Sesión de Sky-Claw configurada con el MO2 real si falla el aislamiento de entorno → control previo obligatorio (abortar antes de pedir aprobación).

## 8. Rollback y matriz
- **Rollback:** solo `QA-20261115\` y la rama/worktree QA; borrado únicamente tras tu OK y después de preservar evidencias (no se limpian solas). Si hay modificación inesperada fuera del alcance: detener, preservar, no revertir antes de documentar.
- **Matriz:** T1, T2, T3(válido), T3(inválido), T4×6, aislamiento, integridad antes/después; cada fila marcada real o sintética.

## 9. Salvedades (no verificado todavía)
- Que `prepare_installation.py` cree una instalación completa en una raíz nueva sin tocar el lab actual: se valida en el primer paso, sin lanzar SSEEdit; si falla → `QA_REAL_BLOCKED`.
- Que el sandbox de Sky-Claw pueda excluir el MO2 real manteniendo `XEDIT_BRIDGE_ROOT` válido: se prueba en dry-run (sin aprobar nada).
- El tramo con LLM real (DeepSeek/Telegram) no está incluido.
