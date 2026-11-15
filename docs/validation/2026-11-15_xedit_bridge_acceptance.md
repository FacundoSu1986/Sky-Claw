# Auditoría independiente y aceptación funcional de SSEEdit Bridge

**Fecha:** 2026-11-15  
**Método aplicado:** `CLAIM → EXTRACT → DOUBT → RECONCILE → VERDICT`  
**Rama / Worktree de auditoría:** `audit/xedit-bridge-acceptance-20261115` (`.worktrees/xedit-bridge-acceptance-20261115`)  
**Base evaluada (`origin/main`):** `28ce8345a6dffb1bbf4773229fa5d229e9a432b0` (`Merge pull request #947 from FacundoSu1986/fix/p7-a5-mo2-custom-executables`)  
**Dictamen final:** **`XEDIT_BRIDGE_ACCEPTED_WITH_LIMITATIONS`**

---

## 1. Resumen ejecutivo

Se ejecutó una auditoría independiente y no destructiva sobre la integración de **SSEEdit Bridge** en Sky-Claw (`origin/main` en `28ce8345a6dffb1bbf4773229fa5d229e9a432b0`), reconstruyendo y verificando criptográficamente los artefactos del smoke controlado previo (`2026-11-14`), auditando línea a línea el contrato funcional/de seguridad y ejecutando la batería reproducible de tests y análisis estático en un worktree aislado.

Conclusiones principales:

1. **Contrato funcional y endurecimiento (#938 + #945) sólidos en código fuente:** Todos los controles `XB-SC-01` a `XB-SC-08` (#938, `27cdb864`) y el rechazo estricto de tipos no booleanos en `HITLGuard.respond` (#945, `f6136f3b`) están implementados, cubiertos por tests y verificados en ejecución local (`443 passed, 4 skipped` + `103 passed` en HITL + `ruff`/`mypy` sin errores en los 6 módulos evaluados).
2. **Evidencia real con SSEEdit 4.1.5f / Bridge 1.1.0 auténtica, pero anterior a #945 y parcial en T1:** Los logs `run.log`, hashes SHA-256 del binario `SSEEdit.exe` (`4.1.5f`), manifiesto `admissions.json`, `local-gate.json` (`21/21` archivos idénticos) y grupos de integridad (`13/13` sin mutación no autorizada) son consistentes. Sin embargo, la reconstrucción forense demostró que:
   - El smoke real (`smoke_real_report.json`) corrió sobre `27cdb864` (antes de PR `#945`) y solo ejercitó la superficie **GUI** (`OrchestrationToolDispatcher`), no la admisión por superficie **LLM** (`GestorDeAdmisionesNativas`).
   - En un primer intento fallido del harness (`2026-11-14T19:49:41Z`), pasar `Decision.DENIED` a `HITLGuard.respond` provocó la **admisión física accidental** `20261114-164944-990086` (`T7_QuestDialogueValid.esp`), y la segunda corrida (`2026-11-14T19:50:42Z`) no revirtió esa copia previa, por lo que `T1 — HITL aprobado` (`20261114-165046-49db5c`) solo demostró **reutilización** (`copiados: []`) en lugar de una copia física nueva (sí demostrada en `T3` con `T1_InvalidReference.esp`).
3. **Brechas no bloqueantes de gobernanza y paridad:** Los 5 módulos del bridge en `sky_claw/` siguen exentos de `mypy` en `pyproject.toml` (`ignore_errors = true` por paquete padre) y los 3 de `sky_claw/app/` exentos de `BLE001`; existen diferencias operativas documentadas entre GUI y LLM (límite fail-closed de 800 caracteres en `detail` solo en GUI; `requerir_contexto=False` por defecto en `XEditBridgeService.admitir`).

En consecuencia, el estado actual justifica **`XEDIT_BRIDGE_ACCEPTED_WITH_LIMITATIONS`** para uso controlado/supervisado, recomendando cerrar una corrida mínima post-`#945` sobre superficie LLM + copia limpia y remover las exenciones `mypy` en `pyproject.toml`.

---

## 2. Identidad exacta del código evaluado

| Concepto | Valor verificado |
|---|---|
| Repositorio | `Sky-Claw` (`origin/main`) |
| SHA base evaluado (`origin/main`) | `28ce8345a6dffb1bbf4773229fa5d229e9a432b0` |
| Commit PR #938 en ancestría | `27cdb86453359ce13066d46dac955c352ca72c31` (`fix(xedit-bridge): harden native audit verdict and HITL admission boundaries (#938)`) |
| Commit PR #945 en ancestría | `f6136f3b006ce1943bb0531f7aeb6c241968bd89` (`fix(security): exigir bool estricto en HITLGuard.respond (#945)`) |
| Commit PR #946 en ancestría | `8a8c943c` (`ci: subir el tope de la matriz Windows de 70 a 90 min y anclar el piso (#946)`) |
| Diff productivo `27cdb864..28ce8345` en módulos bridge + HITL | Únicamente `sky_claw/app/security/hitl.py` (`+7` líneas de PR `#945`); 0 cambios en `xedit_bridge_*` ni en estrategias desde `#938` |
| Contrato arquitectónico | [`docs/adr/0018-auditoria-nativa-xedit-bridge.md`](../adr/0018-auditoria-nativa-xedit-bridge.md) |
| Módulos auditados | [`sky_claw/local/tools/xedit_bridge_client.py`](../../sky_claw/local/tools/xedit_bridge_client.py), [`sky_claw/local/tools/xedit_bridge_service.py`](../../sky_claw/local/tools/xedit_bridge_service.py), [`sky_claw/local/tools/_process.py`](../../sky_claw/local/tools/_process.py), [`sky_claw/app/agent/tools/xedit_bridge_tools.py`](../../sky_claw/app/agent/tools/xedit_bridge_tools.py), [`sky_claw/app/orchestrator/tool_strategies/admitir_plugin_nativo.py`](../../sky_claw/app/orchestrator/tool_strategies/admitir_plugin_nativo.py), [`sky_claw/app/orchestrator/tool_strategies/auditar_plugin_nativo.py`](../../sky_claw/app/orchestrator/tool_strategies/auditar_plugin_nativo.py), [`sky_claw/app/orchestrator/tool_strategies/middleware.py`](../../sky_claw/app/orchestrator/tool_strategies/middleware.py), [`sky_claw/app/security/hitl.py`](../../sky_claw/app/security/hitl.py), [`sky_claw/app/security/sanitize.py`](../../sky_claw/app/security/sanitize.py), [`sky_claw/app/core/path_resolver.py`](../../sky_claw/app/core/path_resolver.py), [`sky_claw/app_context.py`](../../sky_claw/app_context.py) |

---

## 3. Versiones y hashes de los componentes reales

Verificación directa sobre los binarios y configuración del laboratorio QA (`G:\Modding\SSEDIT\tools\xedit-bridge-1.1.0\Laboratorio\`):

| Componente | Identidad / Versión verificada | Tamaño (bytes) | SHA-256 |
|---|---|---|---|
| `Laboratorio\SSEEdit\SSEEdit.exe` | PE `FileVersion=4.1.5.0`, `ProductVersion=4.1.5.0`; cabecera log `SSECheckForErrors 4.1.5f (094C4648)` | `24580608` | `ccb7ca9e6d826868b186b0b9a0fc012affb3fdc9688250583353e6d84e6a2545` |
| `SSEEdit 4.1.5f\SSEEdit.exe` (origen oficial local) | Idéntico bit a bit al binario dentro del laboratorio | `24580608` | `ccb7ca9e6d826868b186b0b9a0fc012affb3fdc9688250583353e6d84e6a2545` |
| `xedit-bridge` (`BridgeTrabajo`) | `1.1.0`, `audit_only: true`, `allow_run_pascal: false`; `21/21` archivos coinciden con `local-gate.json` `product_identity` | — | Manifiestos en `local-gate.json` (`f892246833c3031b377fd344ced3dd9a2b8f23d8b7a40df64f99574d04155d1b`) |
| `BridgeTrabajo\run_mcp.py` | Entrypoint MCP stdio | — | `22675c1f646f3bc0a6bebd01735cd93d1f9b10831e00cfc94d273b55906d456c` |
| `BridgeTrabajo\run_admit.py` | Entrypoint CLI de admisión (`--mo2`, `--profile`, `--plugin`) | — | `4bef5b98d29ace4a0b90244c19e50226cf958474b3d6ac9d870a3376b1540026` |
| `BridgeTrabajo\config.json` | Configuración `sse`, `timeout=60`, `max_timeout=120`, `audit_only=true`, `allow_run_pascal=false` | — | `24bcd8aee90a090d6152e214018b2047e235f6c3d577efc77f56e6c3fdf7a983` |
| `BridgeTrabajo\xedit_bridge\admission.py` | Copia física (`xb`), solo lectura (`FILE_ATTRIBUTE_READONLY`), `st_nlink == 1`, `admission_id` obligatorio | — | `e304921d63f093d050ff18207a7a470f842dc72c5b791c8ccf140dbe2635cf1b` |
| `BridgeTrabajo\xedit_bridge\mcp_server.py` | Servidor MCP stdio (`PROTOCOL="2024-11-05"`, cliente valida `serverInfo.version >= 1.1.0, < 2.0.0`) | — | `4ff4f8cf3c73da949b0e574d7a3980cf4df16881a6cefe41beb2e46f6d797d18` |

---

## 4. Evidencia original examinada

Todos los artefactos originales fueron inspeccionados en modo solo lectura sin alteración de archivos, plugins ni registros:

| Artefacto | Ubicación lógica | Tamaño | SHA-256 | Observación de verificación |
|---|---|---|---|---|
| `smoke_real_report.json` | `<SCRATCH_PREVIA>/smoke_real_report.json` | `9741` B | `95143c8c95c0861fac566dd65be38a26e0e4e5ffdc1d3764bcd01b62e5cca458` | JSON válido (`2026-11-14T19:51:16Z`). Ejecutado sobre worktree de `27cdb864` |
| `run_controlled_smoke.py` | `<SCRATCH_PREVIA>/run_controlled_smoke.py` | `14531` B | `3b2b5afe494733c9d81117fe335e3d93cfa9288a806389bae90c3aaf18e9e2cc` | Versión final corregida (`19:50:38Z`); la versión inicial falló a las `19:49:48Z` |
| `baseline_hashes.json` | `<SCRATCH_PREVIA>/baseline_hashes.json` | `1363064` B | `4e9582d9b360b984747a8abf04d26f380aa58934cd1fae4cfae396f778064213` | Capturado a `2026-11-14T19:45:21Z`, previo al primer intento fallido |
| `post_smoke_hash_comparison.json` | `<SCRATCH_PREVIA>/post_smoke_hash_comparison.json` | `3909` B | `9f778172efb0e235c9317f203ff9a7ae35d78b9bcee67e025a6ac4c4a67f6be9` | `13/13` grupos `ok: true`; `tools_110_admitidos` refleja `+2` `.esp` y cambio en `admissions.json` |
| `tools110_admissions_before.json` | `<SCRATCH_PREVIA>/tools110_admissions_before.json` | `16282` B | `c4bd092c3e325f111fa99c1ebd7d0e735bbf5acf3a4d0a0d278b46665b9d58e4` | Snapshot pre-smoke (`19:45:21Z`): `8` archivos en `files` (sin `T7_QuestDialogueValid.esp` ni `T1_InvalidReference.esp`) |
| `admissions.json` | `Laboratorio/Admitidos/admissions.json` | `20471` B | `cd87c75cf0d009fa9c322fa339549eca54fab02ffadf54473a42bc911c8aa799` | Coincide con `admissions_manifest_sha256` del último run `T3` (`10/10` archivos y orígenes coinciden en SHA-256) |
| `comparison.json` (`20261114-164944-990086`) | `Laboratorio/Evidencias/admissions/20261114-164944-990086/` | — | — | **Admisión accidental** (`T7_QuestDialogueValid.esp`, `19:49:48Z`, `copied: ["T7_QuestDialogueValid.esp"]`) |
| `comparison.json` (`20261114-165046-49db5c`) | `Laboratorio/Evidencias/admissions/20261114-165046-49db5c/` | — | — | `T1` del reporte final (`19:50:48Z`, `copied: []`, reutilizó el ESP copiado a las `19:49:48Z`) |
| `comparison.json` (`20261114-165101-d84e83`) | `Laboratorio/Evidencias/admissions/20261114-165101-d84e83/` | — | — | `T3` (`19:51:03Z`, `T1_InvalidReference.esp`, `copied: ["T1_InvalidReference.esp"]`) |
| `run.log` + `result.json` (`20261114_165050_f3bdb9`) | `Laboratorio/Temporales/Bridge/runs/20261114_165050_f3bdb9/` | `42409` B | `b4f7bbae80d6561f4f64185205d870621695f49fb5e687cf589a42473bc2f959` | Sesión SSEEdit `4.1.5f` (`16:50:54 -03:00`): `Processed Records: 5`, `Errors found: 0`, `prospective_integrity: "PASS"` |
| `run.log` + `result.json` (`20261114_165105_d79ad9`) | `Laboratorio/Temporales/Bridge/runs/20261114_165105_d79ad9/` | `42406` B | `839e0fc665107c2b3f2cc7fccbbf679e0abee6a39ad2ac3b298ed3154f8573dc` | Sesión SSEEdit `4.1.5f` (`16:51:09 -03:00`): `Processed Records: 2`, `Errors found: 1` (`LVLI:05000800` → `[0500DEAD]`), `prospective_integrity: "PASS"` |
| `local-gate.json` | `Laboratorio/Evidencias/local-gate.json` | `17533` B | `f892246833c3031b377fd344ced3dd9a2b8f23d8b7a40df64f99574d04155d1b` | Gate `GL-LOCAL-PROSPECTIVE-v2` `PASS`; sus `21/21` hashes de `product_identity` coinciden con disco (sub-check de admisiones refleja `0` porque data de la inicialización del lab) |
| Transcript sesión previa | `<LOG_SESION_PREVIA>/transcript_full.jsonl` (`steps 986–1028`) | — | — | Confirma la falla exacta del harness `19:49:41Z`, edición en `19:50:22–19:50:38Z` y re-ejecución `19:50:42–19:51:16Z` |

---

## 5. Matriz T1–T4 y pruebas de seguridad adicionales

| Caso | Comportamiento esperado | Resultado declarado (informe previo) | Evidencia original encontrada | Resultado demostrable hoy | Limitaciones | Dictamen |
|---|---|---|---|---|---|---|
| **T1 — HITL denegado** | No admite ni copia | Bloqueado (`HITLApprovalDenied`), sin copia | En `smoke_real_report.json`: `deny_check.reason == "HITLApprovalDenied"` (`19:50:42Z`). Sin embargo, la corrida previa del harness (`19:49:41Z`) **sí copió** por pasar `Decision.DENIED` | El mecanismo de denegación queda demostrado: (a) en el smoke corregido (`19:50:42Z`) con `approve=False` y (b) tras PR `#945`, donde tipos inválidos (`Decision.DENIED`, `1`, `"false"`, `None`) lanzan `TypeError` antes de `_commit` (`tests/test_hitl.py`) | En el smoke real no se re-ejecutó tras PR `#945` ni se probó denegación por la vía LLM (`GestorDeAdmisionesNativas`) | **PASS** (con registro histórico de la anomalía previa a `#945`) |
| **T1 — HITL aprobado** | Admite en laboratorio QA | `success=true`, `admission_id="20261114-165046-49db5c"` | `admissions.json` muestra **dos** registros para `T7_QuestDialogueValid.esp`: `20261114-164944-990086` (`copied: ["T7_QuestDialogueValid.esp"]` en el fallo HITL) y `20261114-165046-49db5c` (`copied: []`, `reused: [5 ESMs + T7]`) | Se demuestra admisión autorizada por `OrchestrationToolDispatcher` + `run_admit.py` con trazabilidad de `admission_id`, pero `T1` solo ejercitó **reutilización** idempotente de una copia previa; la copia física nueva real quedó demostrada en `T3` (`T1_InvalidReference.esp`, `20261114-165101-d84e83`) | No hubo limpieza entre `19:49:48Z` y `19:50:46Z`; no se probó en real la vía LLM (`GestorDeAdmisionesNativas`) | **PASS** (parcial: reutilización en T1, copia física real en T3; solo vía GUI dispatcher) |
| **T2 — Plugin limpio** | `success=true, aprobado=true` | `aprobado=true, clean=true, errores_nativos=0` | `20261114_165050_f3bdb9`: `run.log` SHA-256 `b4f7bbae...`, `42409` B, SSEEdit `4.1.5f`, `Processed Records: 5, Errors found: 0`, `prospective_integrity: "PASS"` | Totalmente demostrado contra SSEEdit `4.1.5f` + `xedit-bridge 1.1.0` | Corrido sobre `27cdb864` (mismo código de auditoría que `28ce8345`) | **PASS** |
| **T3 — Plugin con error** | `success=true, aprobado=false` | `aprobado=false, clean=false, errores_nativos=1` | `20261114_165101-d84e83` copió `T1_InvalidReference.esp`; `20261114_165105_d79ad9` (`run.log` `839e0fc6...`, `42406` B) reportó `Processed Records: 2, Errors found: 1`, `LVLI:05000800` `AuditMissingReference` → `[0500DEAD]` | Totalmente demostrado contra SSEEdit `4.1.5f` real, preservando `signature`, `formid`, `record_name`, `path` y `description` y separando `success=True` de `aprobado=False` | Ninguna crítica; la instancia QA T3 vivía en scratch temporal del usuario | **PASS** |
| **T4 — Plugin no admitido** | `success=false, aprobado=false` | `success=false, aprobado=false, no_admitido=true` en GUI y LLM | `smoke_real_report.json` (`T4_real`): tanto `gui_not_admitted` como `llm_not_admitted` sobre `NoExisteSmoke_Real.esp` devolvieron exactamente `success=False, aprobado=False, no_admitido=True` vía `run_mcp.py` real | Demostrado tanto contra `run_mcp.py` real como en `tests/test_xedit_bridge_service.py` y `tests/test_xedit_bridge_superficies.py` | — | **PASS** |
| **Respuesta MCP inválida** | Fail-closed | Declarado en PR #938 (`XB-SC-01`, `XB-SC-05`) | Cubierto por `tests/test_xedit_bridge_client.py`, `tests/test_xedit_bridge_service.py` y `tests/fixtures/xedit_bridge/fake_run_mcp.py` | Verificado en `443 passed` locales: JSON truncado/inválido, `isError != ok`, tipos/enteros inválidos, `clean=True` contradictorio con errores/hallazgos/fallas o `ok=False`, campos faltantes | No se inyectó falla protocolar contra el `xedit_bridge` real (solo contra fixture `-I -S -B` y fakes) | **PASS** (integración sintética con subproceso real Python) |
| **`admission_id` ausente** | Estado ambiguo, sin reintento | Declarado en PR #938 (`XB-SC-08`) | Cubierto por `tests/test_xedit_bridge_service.py` (`9` variantes inválidas) y `tests/test_xedit_bridge_superficies.py` | `XEditBridgeService.admitir` retorna `success=False`, `admission_id=None`, `estado_admision="ambigua"`, sin `"siguiente"` y sin reintentos ni borrados; `_aviso_de_fin` en LLM refleja estado ambiguo | No forzado contra un `run_admit.py` externo modificado | **PASS** (integración sintética) |
| **Integridad** | Originales sin cambios | `13/13` grupos `ok: true` | `post_smoke_hash_comparison.json` vs `baseline_hashes.json` y recomputación independiente directa en esta auditoría | `0` faltantes y `0` cambios de SHA-256 en `skyrim_stock_core` (10), `golden_master_core` (20), `mo2_prueba` (109), `tools_110_sseedit` (2) y `tools_110_bridge` (55); `mo2_active` y `skyrim_runtime` bloqueados por `PathViolationError` del sandbox e intactos según `post_smoke_hash_comparison.json` | `mo2_active_tree` (`20391` archivos) y `skyrim_runtime_tree` (`16565` archivos) no se re-hashearon completos en esta auditoría | **PASS** |

---

## 6. Estado de los hallazgos XB-SC-01 a XB-SC-08

Verificación directa sobre el código en `origin/main` (`28ce8345`):

| ID | Severidad | Mecanismo verificado en el código actual | Cobertura de test ejecutada | Estado |
|---|---|---|---|---|
| **XB-SC-01** | P1 | `_veredicto()`, `_es_entero_no_negativo()`, `_validar_hallazgos()`, `_validar_fallas()` en `xedit_bridge_service.py`: rechaza `bool` como `int`, enteros `< 0`, campos obligatorios ausentes, listas mal tipadas e inconsistencias entre `clean=True` y `ok=False` / errores / hallazgos / fallas / `check_complete=False` | `tests/test_xedit_bridge_service.py` (passed) | **VERIFICADO Y CERRADO** |
| **XB-SC-02** | P1 | `HitlGateMiddleware` expone `hitl_invocation_state()` (`ContextVar`) con `request_id`; `AdmitirPluginNativoStrategy` vincula `(request_id, plugin, contexto)` y valida coincidencia exacta en `execute()` antes de llamar `service.admitir()` | `tests/test_xedit_bridge_superficies.py` (concurrencia, denegación, cancelación, timeout; passed) | **VERIFICADO Y CERRADO** |
| **XB-SC-03** | P2 | `xedit_bridge_client.py` usa `VARIABLES_ENTORNO_PERMITIDAS` (31 claves) + `[env]` del `codex-mcp.example.toml` con override case-insensitive en Windows (`_reemplazar_env_windows`); nunca hereda `os.environ` completo | `tests/test_xedit_bridge_client.py` (canarios `OPENAI_API_KEY`, `AWS_SECRET_ACCESS_KEY`, `SKYCLAW_SECRET_CANARY`; passed) | **VERIFICADO Y CERRADO** |
| **XB-SC-04** | P2 | `run_capture(..., require_job=True)` asigna Job Object `KILL_ON_JOB_CLOSE` (`assign_kill_on_close_job`) en Windows y falla cerrado con `OSError`/`BridgeError` si falla; `close_job` y `kill_and_reap` limpian árbol en salida normal, timeout y cancelación | `tests/test_process_helper.py` + `tests/test_xedit_bridge_client.py` (procesos reales Windows con nietos huérfanos; passed) | **VERIFICADO Y CERRADO** |
| **XB-SC-05** | P2 | `_capturar_acotado` en `_process.py` limita `stdout + stderr <= 8 MiB` (`LIMITE_BYTES_SALIDA`), mata y cosecha el proceso si excede, lanzando `OutputLimitExceededError` sin entregar JSON truncado | `tests/test_process_helper.py` + `tests/test_xedit_bridge_client.py` (passed) | **VERIFICADO Y CERRADO** |
| **XB-SC-06** | P2 | `_validar_identificador` (`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`), `_validar_ruta_log` (controles C0/C1, `Cf`, `Zl`/`Zp`, traversal `..` y unidad `C:..` pre y post NFKC/`texto_una_linea`) + `sanitize_for_prompt` en mensajes y payloads LLM | `tests/test_xedit_bridge_service.py` + `tests/test_xedit_bridge_superficies.py` (passed) | **VERIFICADO Y CERRADO** |
| **XB-SC-07** | P2 | `AdmitirPluginNativoStrategy.validate_for_approval` y `describe_for_approval` exigen que el detalle completo (`plugin`, `perfil`, ruta MO2 íntegra) quepa en `MAX_APPROVAL_DETAIL_LENGTH` (`800`); falla cerrado con `ValueError` antes de abrir prompt HITL si habría truncamiento | `tests/test_xedit_bridge_superficies.py` (passed) | **VERIFICADO Y CERRADO** |
| **XB-SC-08** | P2 | En `xedit_bridge_service.admitir()`, si `ok=True` carece de `admission_id` válido, devuelve `success=False`, `admission_id=None`, `estado_admision="ambigua"`, sin `"siguiente"` y sin reintentos; `_aviso_de_fin` en LLM conserva el estado ambiguo | `tests/test_xedit_bridge_service.py` + `tests/test_xedit_bridge_superficies.py` (passed) | **VERIFICADO Y CERRADO** |

---

## 7. Verificación de la corrección HITL del PR #945 e investigación de la anomalía del primer smoke

### Reconstrucción cronológica exacta del incidente (`2026-11-14`)

1. **Qué ocurrió realmente:** En el primer intento del smoke (`step 1002`, `2026-11-14T19:49:41Z`, sobre `27cdb864`), el script temporal `run_controlled_smoke.py` implementó su callback de prueba `notify()` invocando `await guard.respond(req.request_id, Decision.APPROVED if approve else Decision.DENIED)`. Al ejecutar el caso negativo (`approve=False`), pasó `Decision.DENIED` en lugar del booleano `False`.
2. **Por qué se produjo la aprobación accidental:** Antes de PR `#945`, `HITLGuard.respond` evaluaba `Decision.APPROVED if approved else Decision.DENIED` directamente por verdad booleana. En Python, todo miembro de `Enum` es truthy (`bool(Decision.DENIED) is True`), por lo que la guardia comprometió `Decision.APPROVED` y permitió ejecutar `AdmitirPluginNativoStrategy` contra el bridge real.
3. **Archivos copiados y trazabilidad:** A las `2026-11-14T19:49:48.362731+00:00`, `run_admit.py` generó el registro **`20261114-164944-990086`** en `admissions.json`, copiando físicamente `T7_QuestDialogueValid.esp` (`717` bytes, SHA-256 `838cf741dee4c93b644a9e463fd9078cabf80171674a316b2363414846a0cd81`) desde `MO2Prueba\State\mods\AuditB\` hacia `Admitidos\Data\T7_QuestDialogueValid.esp` (reutilizando los 5 ESM base). Acto seguido falló el `assert t1_deny.get("reason") == "HITLApprovalDenied"` (exit code 1).
4. **Impacto en la segunda corrida:** Entre `19:50:22Z` y `19:50:38Z`, la sesión anterior corrigió el harness a `await guard.respond(req.request_id, approve)` y lo relanzó a las `19:50:42Z` **sin revertir** la copia ni la entrada `20261114-164944-990086` en `admissions.json`. Como consecuencia, `T1 approve` (`20261114-165046-49db5c`, reportado en `smoke_real_report.json`) reutilizó esa copia ya existente (`copiados: []`), conservando `files["T7_QuestDialogueValid.esp"]["admitted_utc"] == "2026-11-14T19:49:48.362731+00:00"`.
5. **Alcance de la anomalía vs llamadores de producción:** Se buscó `.respond(` y `def respond(` en todo `sky_claw/`. Existen exactamente **4** invocadores de producción y **todos** pasan `bool` estricto:
   - `sky_claw/app/comms/telegram.py:854` (`approved = action == "approve"`)
   - `sky_claw/app/comms/telegram.py:900` (`approved: bool`)
   - `sky_claw/app/gui/sky_claw_gui.py:710` (`approved: bool`)
   - `sky_claw/app_context.py:1649` (`False` literal)  
   Por tanto, el error de invocación quedó limitado al primer harness del smoke, aunque el fallo latente de API permisiva sí residía en `HITLGuard.respond`.
6. **Cobertura del PR `#945` (`f6136f3b`):** [`sky_claw/app/security/hitl.py`](../../sky_claw/app/security/hitl.py#L343-L358) verifica `if type(approved) is not bool: raise TypeError(...)` antes de `_commit`. Las pruebas nuevas en [`tests/test_hitl.py`](../../tests/test_hitl.py#L535-L630) demuestran que `Decision.DENIED`, `Decision.TIMEOUT`, `1`, `0`, `"false"`, `"true"` y `None` lanzan `TypeError`, dejan la solicitud pendiente sin aprobar, permiten una denegación posterior válida con `False` y vencen fail-closed (`TIMEOUT`) si expiran sin respuesta booleana válida. Verificado también en las suites `test_hitl_wiring.py`, `test_app_context_hitl_notify.py`, `test_telegram_hitl_lifecycle.py` y `test_gui_hitl_multi_pending.py` (`103 passed`).

---

## 8. Contrato GUI/LLM

Ambas superficies comparten el mismo servicio subyacente [`XEditBridgeService`](../../sky_claw/local/tools/xedit_bridge_service.py), el mismo resolver (`PathResolutionService.get_xedit_bridge_root` / `get_mo2_instance_ini_dir` / `get_active_profile`) y el mismo cliente MCP/CLI (`xedit_bridge_client.py`), sin permitir rutas MO2 o de bridge suministradas por el LLM.

| Aspecto | Superficie GUI (`OrchestrationToolDispatcher`) | Superficie LLM (`AsyncToolRegistry`) | Evaluación |
|---|---|---|---|
| Auditoría (`auditar_plugin_nativo`) | `AuditarPluginNativoStrategy` (sin HITL, timeout `5..120`, máximo práctico acotado por service) | `auditar_plugin_nativo` (sin HITL; acota JSON a `3500` chars con `_json_acotado`, recorta hallazgos desde el final incrementando `hallazgos_omitidos`) | Consistente; adaptación legítima al presupuesto de contexto LLM y `DEFAULT_TOOL_ROUND_TIMEOUT` (`120s`) |
| Admisión (`admitir_plugin_nativo`) | `AdmitirPluginNativoStrategy` bajo `_destructive_middleware` (`HitlGateMiddleware` con `DESTRUCTIVE_TOOL_PATTERNS` y `ErrorWrappingMiddleware`) | `admitir_plugin_nativo` con `hitl: HITLGuard` explícito + `GestorDeAdmisionesNativas` en segundo plano (`category="tool_execution"`), serializado a `1` trabajo activo | No existe ruta alternativa sin HITL; la denegación o ausencia de `HITLGuard` falla cerrado en ambas |
| Contexto aprobado | Capturado y ligado a `(request_id, plugin, contexto)` vía `hitl_invocation_state` | Capturado en `TrabajoDeAdmision.contexto` antes de pedir aprobación y pasado a `service.admitir` | Coincide con el contexto ejecutado en ambas |
| Diferencias observadas | Valida longitud de `detail <= 800` chars fail-closed (`XB-SC-07`); `category="tool_execution"` sería auto-aprobable si un ritual invocara `run_ritual` con `Modo local ON` (actualmente no hay botón GUI ni entrada en `RITUAL_TOOL_MAP` para `admitir_plugin_nativo`) | No impone tope de `800` chars al `reason` del prompt remoto; corre en `asyncio.Task` fuera de `_ritual_auto_approve` (`make_gui_hitl_notify` delega al canal remoto/modal según origen, nunca auto-aprueba fuera de `run_ritual`) | Documentadas; se recomienda hacer explícito `requerir_contexto=True` desde ambas superficies en `service.admitir()` |

---

## 9. Resultados de tests reproducibles

Todos los comandos se ejecutaron sobre el worktree limpio `audit/xedit-bridge-acceptance-20261115` en commit `28ce8345a6dffb1bbf4773229fa5d229e9a432b0` (NO se ejecutó `SKYCLAW_SMOKE_XEDIT_BRIDGE=1`).

| Verificación / Comando | Exit code | Resultado exacto |
|---|---|---|
| `pytest -v tests/test_xedit_bridge_client.py tests/test_xedit_bridge_service.py tests/test_xedit_bridge_superficies.py tests/test_xedit_bridge_smoke_real.py tests/test_hitl.py tests/test_process_helper.py tests/test_ritual_dispatch.py` | `0` | **`443 passed, 4 skipped`** in `91.32s` (`4 skipped` = smoke opt-in `SKYCLAW_SMOKE_XEDIT_BRIDGE=1` en `test_xedit_bridge_smoke_real.py`) |
| `pytest -q tests/test_xedit_bridge_smoke_real.py` | `0` | **`4 skipped`** in `0.23s` (confirmado opt-in desactivado) |
| `pytest -q tests/test_hitl_wiring.py tests/test_app_context_hitl_notify.py tests/test_telegram_hitl_lifecycle.py tests/test_gui_hitl_multi_pending.py` | `0` | **`103 passed`** in `14.94s` |
| `pytest -v tests/test_worktree_convention_invariant.py tests/test_hitl_wiring.py tests/test_app_context_hitl_notify.py tests/test_telegram_hitl_lifecycle.py tests/test_gui_hitl_multi_pending.py` | `1` | **`125 passed, 1 failed`** in `20.41s`: único fallo en `test_los_worktrees_no_principales_viven_bajo_la_convencion` por `5` worktrees preexistentes ajenos en la estación (`.claude/worktrees/*` y `Skyclaw_Worktree`), no por el worktree de auditoría |
| `ruff check sky_claw/ scripts/ tests/` | `0` | `All checks passed!` |
| `ruff format --check sky_claw/ scripts/ tests/` | `0` | `1120 files already formatted` |
| `mypy sky_claw/ scripts/ --ignore-missing-imports --no-error-summary` | `0` | Exit `0` (nota: los módulos del bridge están cubiertos por `ignore_errors = true` en `pyproject.toml`) |
| `mypy --config-file nul --python-version 3.11 --ignore-missing-imports --no-error-summary` sobre los 6 módulos (`xedit_bridge_client.py`, `xedit_bridge_service.py`, `xedit_bridge_tools.py`, `admitir_plugin_nativo.py`, `auditar_plugin_nativo.py`, `hitl.py`) | `0` | **0 errores de tipos** aun sin exenciones |
| `ruff check --isolated --select BLE001` sobre los 6 módulos | `1` | `0` errores en los 5 módulos del bridge; `1` hallazgo intencional en `sky_claw/app/security/hitl.py:238` (`except Exception` en `_notify` que marca `notify_failed = True` para fallar cerrado como `TIMEOUT`) |

---

## 10. Riesgos residuales

1. **Micro-ventana spawn → Job Object en Windows (`require_job=True`):** `asyncio.create_subprocess_exec` no soporta `CREATE_SUSPENDED`; si un subproceso saliera antes de `AssignProcessToJobObject`, `run_capture` falla cerrado (`BridgeError`). Aceptable bajo el modelo de amenazas actual (mismo usuario Windows), aunque no equivale a aislamiento de privilegios/OS sandbox de cuenta separada.
2. **Exenciones de CI (`pyproject.toml`):** Los 5 módulos del bridge permanecen en bloques `ignore_errors = true` para `mypy` (`sky_claw.local.tools.*`, `sky_claw.app.agent.*`, `sky_claw.app.orchestrator.*`), y `sky_claw/app/**` exime `BLE001`. Si un PR futuro introduce errores de tipos o `except Exception` silenciosos en ellos, el gate CI actual no los detendría.
3. **Default `requerir_contexto=False` en `XEditBridgeService.admitir`:** Aunque tanto `AdmitirPluginNativoStrategy` como `GestorDeAdmisionesNativas` verifican que `contexto` no sea `None` antes de invocar `service.admitir()`, un nuevo consumidor futuro que llame directamente al servicio sin pasar `requerir_contexto=True` podría admitir sin contexto autorizado.
4. **Consistencia temporal de `local-gate.json`:** `Evidencias/local-gate.json` registra la validación inicial previa a las admisiones (`"admissions": 0`); no se regenera automáticamente como reporte post-smoke.

---

## 11. Limitaciones y elementos no verificados

1. **No hay corrida real SSEEdit post-`#945` ni por superficie LLM:** La corrida `smoke_real_report.json` se efectuó sobre `27cdb864` (antes de PR `#945`), ejercitando únicamente la admisión vía GUI `OrchestrationToolDispatcher`, no vía `AsyncToolRegistry` / `GestorDeAdmisionesNativas`.
2. **Contaminación parcial de T1 en el smoke previo:** La admisión accidental `20261114-164944-990086` hizo que `T1 — HITL aprobado` (`20261114-165046-49db5c`) probara reutilización de copia previa (`copiados: []`) en lugar de copia física nueva de `T7_QuestDialogueValid.esp` (aunque la copia física nueva sí se probó en `T3` con `T1_InvalidReference.esp`).
3. **`test_xedit_bridge_smoke_real.py` no ejercita HITL ni contexto estricto:** Permaneció opt-in y desactivado (`4 skipped`); además llama `service.admitir(plugin)` sin pasar `contexto` ni `HITLGuard`.
4. **Casos de fallo protocolar contra bridge real:** JSON truncado (`> 8 MiB` o malformado) y `ok=True` sin `admission_id` solo se validaron contra fakes y subprocesos Python de test (`fake_run_mcp.py`, `fake_run_admit.py`), no contra un proceso `xedit-bridge 1.1.0` real alterado.

### Protocolo mínimo recomendado antes de cerrar aceptación total (NO ejecutado; requiere autorización explícita)

Si se desea cerrar `T1` copia limpia + superficie LLM + código `28ce8345` post-`#945`:

- **Preflight obligatorio:** Verificar `is_admin == False`, `PathViolationError` al intentar validar MO2 activo / Skyrim real / Golden Master, captura de baseline SHA-256 previa y existencia de `codex-mcp.example.toml` + `SSEEdit.exe` (`ccb7ca9e...`).
- **Recursos QA a usar:** Instancia MO2 aislada QA (`MO2Prueba` / perfil `AuditB` o sandbox dedicado) y laboratorio `xedit-bridge-1.1.0\Laboratorio\BridgeTrabajo` limpiando o usando un plugin fixture QA sin entrada previa en `admissions.json`.
- **Prueba mínima:**
  1. Invocar `HITLGuard.respond(req_id, Decision.DENIED)` y confirmar `TypeError` + expiración/denegación sin copia en `admissions.json`.
  2. Ejecutar `admitir_plugin_nativo` vía `AsyncToolRegistry` + `GestorDeAdmisionesNativas` con `HITLGuard` denegado (`False`) → 0 archivos copiados, 0 entradas nuevas en `admissions.json`.
  3. Ejecutar `admitir_plugin_nativo` vía `AsyncToolRegistry` + `GestorDeAdmisionesNativas` con `HITLGuard` aprobado (`True`) → copia física nueva (`copiados != []`, `FILE_ATTRIBUTE_READONLY`, `st_nlink == 1`) y luego `auditar_plugin_nativo` sobre la misma copia.
  4. Reconciliar hashes SHA-256 post-run contra `baseline_hashes.json`.

---

## 12. Dictamen de aceptación

**Dictamen:** **`XEDIT_BRIDGE_ACCEPTED_WITH_LIMITATIONS`**

**Justificación:** La arquitectura y los controles de seguridad de SSEEdit Bridge (`#912` + endurecimiento `#938` `XB-SC-01..08` + corrección `HITLGuard.respond` `#945`) son sólidos, coherentes con ADR 0018 y verificables en tests reproducibles (`443 passed`, `103 passed` HITL, `ruff` y `mypy` sin errores en los 6 módulos evaluados), y existe evidencia criptográfica íntegra de ejecución real contra SSEEdit `4.1.5f` y `xedit-bridge 1.1.0` en `T2`, `T3`, `T4` y copia física en `T3`. No procede `XEDIT_BRIDGE_ACCEPTED` sin reservas mientras no exista una corrida mínima post-`#945` sobre superficie LLM con copia física limpia en `T1` y se añadan las declaraciones explícitas de `mypy ignore_errors = false` en `pyproject.toml`.
