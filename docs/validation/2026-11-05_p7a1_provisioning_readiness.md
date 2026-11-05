# P7-A1 — Cierre de bloqueos y preparación de provisionamiento (READ-ONLY sobre instalaciones reales)

- **Fecha:** 2026-11-05
- **Rama:** `feat/p7-a1-provisioning-readiness` (worktree aislado, base `origin/main` `2b770818`)
- **Issue de seguimiento:** [#867](https://github.com/FacundoSu1986/Sky-Claw/issues/867) (enlaza #672 y #851; **no** declara terminado el Golden Master)
- **P7-A0 previo:** `P7_A0_BLOCKED` — evidencia en `E:\Skyclaw_A0_Evidence\`
- **Dictamen de esta sesión:** **`P7_A1_READY_FOR_OWNER_APPROVAL`**
  — el camino de **provisionamiento local** (Stock verificado → Generation registrada → Runtime Clone independiente) quedó implementado y validado con tests sintéticos, runbook y evidencia. **No implica aprobar P7**: el binding a MO2/Desired Runtime sigue bloqueado por diseño (emisión productiva deshabilitada y Managed Source ACTIVE) y requiere aprobación del propietario en una fase posterior.

## 1. Diagnóstico real del fallo CI (Windows Python 3.11)

Run: [41933529936](https://github.com/FacundoSu1986/Sky-Claw/actions/runs/41933529936), SHA `2b770818416b4137cb8474b215c8174d5bde445e`.

| Campo | Valor |
|---|---|
| Job fallido | `🧪 Tests / windows-latest / py3.11` (job `126936701090`) |
| Ventana | 2026-11-04T23:26:15Z → 2026-11-05T00:18:16Z (**52m01s**) |
| Evidencia del fallo | anotación de check-run: *"The hosted runner lost communication with the server…"* |
| Log | **no existe**: `gh api .../jobs/126936701090/logs` → `BlobNotFound`; el zip del run tiene 20 entradas para 11 de 12 jobs (falta la del py3.11). El step `Run tests with coverage` quedó `in_progress` sin fin; `Upload coverage` nunca corrió |
| Test/aserción | **UNKNOWN** — no hay `FAILED tests/...`, ni traceback, ni resumen de pytest |
| Gate agregado | `🧪 Tests (Pytest)` falló sólo por propagación (`Test matrix failed: failure`); `📦 Build` skipped |
| Comparación | mismo árbol pasó py3.11 en PR #864 ([run 41922949104](https://github.com/FacundoSu1986/Sky-Claw/actions/runs/41922949104), 37m30s); `de55c33c` verde en [41909417100](https://github.com/FacundoSu1986/Sky-Claw/actions/runs/41909417100) (17619 passed / 43 skipped / 32m27s); py3.12 del mismo run verde; config del job idéntica entre 3.11/3.12 |

**Clasificación:** **INFRASTRUCTURE ~85% / FLAKINESS ~15% / REGRESSION <5%**. No hay test atribuible ni cambio de código específico de 3.11; la firma es el runner perdiendo comunicación y muriendo antes de volcar el log. **No se abre PR de fix de código sobre esta evidencia.**

**Acción ejecutada:** `gh run rerun 41933529936 --failed` (attempt 2). Al cierre de este informe: attempt 2 `in_progress`; el job py3.11 está corriendo y el resto de jobs ya concluyeron `success` (Lint 3.11/3.12, Mypy 3.11/3.12, Security, POSIX Core, Windows py3.12, gates). Si attempt 2 termina verde, confirma infra/flake; si repite, capturar logs antes de que desaparezcan e instrumentar `--durations`/timeout por test (trabajo CI separado).

## 2. Capacidades de provisionamiento existentes (antes de este PR)

Auditoría read-only de `sky_claw/local/frozen_runtime/` (file:line en el informe de sesión; resumen):

| Tramo | ¿Existía? | Evidencia |
|---|---|---|
| Discovery/estabilidad de Managed Source Steam | Sí | `discovery.py:61-97`, `stabilization.py:112-323` — **sólo Steam**; rechaza copia externa |
| Candidate (copia física independiente) | Sí | `candidates.py:1186-1395` — requiere `ManagedSource` + appmanifest |
| Metadata de Generation | Sí (sólo metadata) | `generations.py:204-237`; **sin writer de `versions/<id>/`** (tests copiaban a mano) |
| Registro/verificación de RuntimeCloneRecord | Sí (sólo registro) | `clones.py:389-499`, `651-805`; **sin creador del árbol del Clone** |
| Estado `active.json` + CAS | Sí | `state.py:337-412` |
| Journal/recovery/aprobación/owner-consent | Sí | `journal.py`, `binder_recovery.py`, `approval.py`, `owner_consent.py` |
| Binder/preflight | Sí, **synthetic-only** | `runtime_clone_binder.py:601-1225`; sin callers productivos; `EMISION_PRODUCTIVA_HABILITADA=False` (`approval.py:158`) |
| Runtime Vault RV-2/RV-3 (Golden Master verificado) | Sí | `runtime_vault/golden.py:114-292`, `runtime_vault/clone.py:196-433` (requiere `GoldenMasterVerificationResult` y rol `reference_only`) |

**Conclusión de la auditoría:** la cadena completa **no estaba soportada end-to-end**. Faltaban: (a) **origen Stock** (una copia externa no puede ser `SourceSnapshotEvidence` sin inventar provenance Steam); (b) **publicación del árbol de Generation**; (c) **materialización del árbol del Clone**; (d) `RuntimeSetupManifest` (SFR-22); (e) cableado productivo del approval/binder. A su vez, la captura de Managed Source exige **IDLE**, por lo que con Steam ACTIVE el camino Steam→Candidate está bloqueado.

## 3. Capacidades faltantes y qué agrega este PR

**Agregado en este PR (sólo camino local, sin Steam):**

1. `StockReference`: referencia **independiente** (TreeDigest + RuntimeIdentity + archivos críticos) con `reference_digest` recomputado al cargar (una referencia editada no autoriza un árbol distinto).
2. `construir_referencia_stock` / `cargar_referencia_stock` (read-only sobre la fuente, schema cerrado).
3. `adoptar_copia_stock`: verificación contra la referencia con **`runtime_vault.golden.verify_golden_master`** (no “hashear la carpeta y confiar”), re-medición fresca posterior, rechazo de reparse/junction/hardlinks, solape con storage/Managed Source y ubicación dentro de `steamapps`.
4. `publicar_generation_desde_stock`: copia sellada a `versions/<generation-id>/` con `copiar_arbol_independiente` (contenido, nunca hardlinks), verificación fresca de digest/membership/links/identidad/independencia, metadata `provider="stock"` (`appid`/`buildid` nulos) y **sidecar de procedencia** con la referencia usada.
5. `crear_runtime_clone_desde_generation`: árbol del Clone fuera del storage, copia independiente, verificación de `st_nlink==1`, sin inodos compartidos con Generation/source, membership sellada (incluye directorios vacíos), y registro `PROVISIONED` con `RuntimeSetupEvidence` mínimo content-bound (`SkyrimSE.exe` + loader + DLL de SKSE del build exacto).
6. `verificar_sin_multilinks` como primitive pública de independencia.
7. Corrección de la especificación **P7-G/SFR-21** (ADR 0012 §SFR-21 y §29.7; matriz del inspector): una actualización legítima de Steam **puede** cambiar sus hashes; la invariante es que Sky-Claw **no escribe** superficies de Steam y el inventario/hashes del **Clone** no cambia.
8. Runbook P7: `docs/operations/p7_golden_master_runbook.md`.

**Sigue faltando (declarado, fuera de este PR):**

- **`RuntimeSetupManifest` completo (SFR-22)**: sólo hay evidencia mínima de setup; no hay store de manifests ni reproducción por versión. En el runbook, los archivos de setup (SKSE/root) se incorporan a la **fuente candidata** antes de la referencia.
- **`Desired Runtime` + binder productivo**: `active.json` no se toca; `EMISION_PRODUCTIVA_HABILITADA=False`; el binding exige **Managed Source IDLE** (hoy ACTIVE) y aprobación HITL.
- **Procedencia Steam de la copia Stock**: la referencia es un ancla del operador (detecta drift, impide auto-autorización del escritor), pero no demuestra por sí sola origen Steam. Debe cruzarse contra una captura de Managed Source cuando Steam esté IDLE (P3b/P7-A2).
- **N de SFR-21** no definido: propuesta N≥5 sujeta a aprobación (runbook §10).
- Issue #672 quedó cerrado aunque P7 sigue abierta → seguimiento en #867.

## 4. Bloqueo Steam (ACTIVE) y alcance del provisionamiento local

- **Requieren IDLE:** captura de Managed Source (`candidates.py:241-258`, `stabilization.py:112-323`), preflight del binder (`binder_preflight.py:451-492`) y binding/transition (`runtime_clone_binder.py:702-714`, revalidado bajo lock en `395-407`). La semántica de `StateFlags` 518 sigue **UNVERIFIED** (ADR §29.5); no se hardcodea.
- **No leen Steam / toleran ACTIVE:** publicación y verificación de Generation, creación y verificación de Clone, CAS de Desired, rollback/recovery y journal (grep: cero referencias a provider/manifest en `journal.py`).
- El camino local de este PR **no consulta appmanifest ni señales de proveedor** (test ancla: parchea `observe_provider_activity`/`read_steam_manifest_observation` para que revienten y el provisionamiento sigue OK).
- **Consecuencia honesta:** se puede preparar Generation/Clone con Steam ACTIVE **sin manipular Steam**; el **binding a MO2** no puede completarse mientras Steam no esté IDLE y la emisión productiva siga deshabilitada. Esa es una decisión de fase posterior, no un defecto del provisionamiento.
- `steam_api64.dll`/Steam corriendo: el repo **no** afirma que el Clone arranque con Steam cerrado; P7-E/G sólo pueden afirmar invariancia de archivos del Clone y no-escritura de Sky-Claw sobre superficies de Steam (no “Steam no requerido”).

## 5. Runbook P7

[`docs/operations/p7_golden_master_runbook.md`](../operations/p7_golden_master_runbook.md): preflight, elección de storage root admitido, estimación de espacio, backups con SHA-256, construcción de referencia, publicación de Generation, creación de Clone, autorizaciones A1–A6, rollback/aborto, matriz P7-A…P7-G con umbrales/abortos y la regla de crash-test sólo en entorno desechable.

## 6. Tests ejecutados

| Evidencia | Resultado |
|---|---|
| **TDD RED inicial** (tests nuevos contra API inexistente) | `ImportError` de colección (1 error) — rojo registrado antes de implementar |
| **TDD GREEN** `tests/test_frozen_runtime_p7_provisioning.py` | **31 passed** (30 + ancla de hardlink + drift de directorio vacío) |
| Regresión suites relacionadas (P2 generations/independence/storage, P5-B, P3 contract/candidates) | **167 passed, 4 skipped** |
| Inspector P7 tras corrección de P7-G (`test_p7_inspector_output.py`) | **24 passed** |
| Mutantes discriminantes | **4/4 muertos**: (M1) chequeo `st_nlink` eliminado → falla ancla de hardlink; (M2) `reference_digest` del loader eliminado → falla test de referencia alterada; (M2b) chequeo de digest en adopción eliminado → falla su test; (M3) chequeo de membership del sidecar eliminado → falla drift de directorio vacío. Restaurado y **31 passed** |
| Ruff (`ruff check` + `ruff format --check` sobre `sky_claw/`, `scripts/`, `tests/`) | **verde** (gate CI) |
| Mypy (`mypy sky_claw/ scripts/`) | **verde**, 430 archivos |
| Evidencia de mutantes | `E:\Skyclaw_A1_Evidence\mutation_results.json` |

CI del PR: pendiente de correr al abrir el PR Draft (ramo `feat/p7-a1-provisioning-readiness`). El CI del `main` sigue con el fallo de infraestructura del §1 (attempt 2 en curso).

## 7. Riesgos residuales

| Riesgo | Mitigación / estado |
|---|---|
| La referencia Stock es un ancla del operador (no prueba Steam por sí sola) | schema + `reference_digest` + aprobación A1; cross-check con Managed Source IDLE pendiente (P3b/P7-A2) |
| La fuente candidata se prepara fuera del código (Stock + SKSE/root files) | referencia content-bound verifica exactamente lo preparado; `RuntimeSetupManifest` completo sigue faltando (SFR-22) |
| Copia parcial si hay crash/ENOSPC | comprobación de espacio previa; árbol parcial sin metadata (UNKNOWN) y **sin borrado destructivo**; decisión manual del propietario |
| Paths largos de Windows en árboles de 17k archivos | no se habilita `\\?\`; usar roots cortos (p. ej. `G:\FR`, `G:\FC`) y verificar en rig |
| Steam ACTIVE impide binding | documentado; provisioning local no lo necesita; A5 requiere IDLE + aprobación |
| Hardlinks/reparse en la fuente | rechazo fail-closed (tests + mutante) |
| No se puede afirmar que Steam no sea necesario para lanzar | fuera de alcance; P7-E/G sólo afirman invariancia/no-escritura |
| Volumen/espacio | ~17 GiB fuente + ~17 GiB Generation + ~17 GiB Clone; el runbook exige verificar antes |

## 8. Rutas de destino propuestas y espacio requerido (NO creadas)

| Uso | Ruta propuesta | Tamaño |
|---|---|---|
| Referencia Stock + evidencia P7-A1 | `E:\Skyclaw_A1_Evidence\` | < 5 MiB |
| Fuente candidata preparada (Stock + SKSE) | `G:\Modding\GoldenMaster_Source_1.6.1170` | ≈ 17 GiB |
| Storage Frozen Runtime | `G:\Modding\FrozenRuntime` (o `E:\Skyclaw_Frozen_Runtime`) | ≈ 17–20 GiB |
| Clone parent | `G:\Modding\FrozenClones` | ≈ 17–20 GiB |
| Backups | `I:\Skyclaw_Backups\P7-A1_<fecha>\` | variable (MO2 mods/downloads/overwrite + configs) |

Estado P7-A0 del disco: `G:` libre 256,3 GiB; `I:` libre 607,8 GiB; `E:` libre 458 GiB. **Ninguna de estas rutas fue creada en P7-A1.**

## 9. Plan de backup y rollback (resumen)

- **Antes:** snapshot con SHA-256 de INI/perfil MO2, `config.toml`, appmanifest (evidencia), y backup completo de `mods/downloads/overwrite` si el propietario lo requiere; inventario/hashes del runtime/Stock ya capturados en P7-A0.
- **Durante:** el provisionamiento sólo escribe en el storage aprobado y en el clone-parent; no toca Steam/MO2/Skyrim/Stock.
- **Aborto:** antes de A2/A4 si falla preflight, espacio, referencia, journal/recovery o procesos vivos. Fallo de copia ⇒ árbol parcial sin metadata (UNKNOWN), sin borrado automático.
- **Rollback:** el provisionamiento no escribe journal; la restauración de superficies MO2/config/active.json corresponde al binder (P7-F). Restauración de archivos: copiar snapshot y verificar SHA-256; nunca editar el manifest de Steam.
- Detalle completo en el runbook §5 y §8.

## 10. Operaciones exactas que necesitan aprobación del propietario

| ID | Operación | Escribe | Aprobación |
|---|---|---|---|
| A1 | Construir y aprobar la referencia Stock (`reference_digest`) | archivo de referencia fuera de la fuente | propietario |
| A2 | Inicializar el storage Frozen Runtime (`initialize_frozen_runtime_storage`) | nuevo storage | propietario |
| A3 | Preparar la fuente candidata (copia Stock + SKSE/root files) | directorio nuevo | propietario |
| A4 | Publicar Generation y crear Runtime Clone (dos comandos) | storage + clone-parent | propietario; backup previo verificado |
| A5 | Desired Runtime + binding MO2 (binder) | `active.json`, `modorganizer.ini`, config Sky-Claw | **fuera de P7-A1**: habilitar emisión productiva + Steam IDLE + HITL |
| A6 | Ejecutar P7-A…P7-G reales (lanzamientos) | lanzamientos MO2/SKSE/Skyrim | propietario; ventana controlada |

`EMISION_PRODUCTIVA_HABILITADA` **no** se modifica en este PR ni en este runbook.

## 11. Criterio de cierre de P7-A1

- Existe un **camino demostrable y testeado** para preparar un Clone seguro desde una copia Stock verificada **sin manipular Steam** (implementado en este PR, validado en sintético).
- Existe un **procedimiento verificable** (runbook) para usarlo posteriormente con MO2, con umbrales, abortos, backups y autorizaciones.
- **No** se ejecutó provisionamiento real, **no** se creó storage productivo, **no** se tocó Steam/MO2/Skyrim/runtime/Stock y **no** se habilitó producción.
- P7-A1 **no** aprueba P7; el Golden Master queda pendiente de aprobación del propietario (A1–A6) y de las fases P7-A…P7-G.

## 12. Evidencia local complementaria

- `E:\Skyclaw_A1_Evidence\mutation_results.json` — resultados de los 4 mutantes + restauración verde.
- `E:\Skyclaw_A1_Evidence\ci_forensics.md` — forense del run 41933529936 y enlaces de sesión.
- `E:\Skyclaw_A0_Evidence\` — informe/evidencia de P7-A0 (inspector, inventarios, backups, hardlinks).
- CI: run `41933529936` (attempt 1 y 2), comparativas en §1.

## 13. Anexo A — fallo Lint del PR #868: drift de dependencia (no defecto de P7)

- Al abrir el PR #868, el gate `Lint (Ruff)` falló en `sky_claw/local/frozen_runtime/clones.py:355` y `:437` (`SIM102`), **código pre-existente que este PR no toca**.
- Causa medida: el extra `dev` declara `ruff>=0.4` y CI instala con pip; el **2026-11-05T18:51Z** se publicó **ruff 0.17.4**, que activó la regla SIM102 para `elif` con `if` anidado. Reproducido en READ-ONLY sobre `main` `2b770818`: ruff 0.17.4 → 2 errores; ruff 0.15.11 (la que fija `uv.lock`) → TODO verde.
- **Fix separado (no mezclado con P7):** PR Draft [#869](https://github.com/FacundoSu1986/Sky-Claw/pull/869) `ci/pin-ruff-version` — pin `ruff==0.15.11` en `pyproject.toml`. Tras su merge, el PR #868 debe sincronizar `origin/main` (`git merge origin/main --no-ff`) y re-ejecutar CI.
- Sin cambios de producto en P7; los gates locales con la versión lockeada (ruff check/format, mypy, tests) están verdes.

