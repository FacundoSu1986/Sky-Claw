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

1. `StockReference`: referencia **independiente** (TreeDigest + RuntimeIdentity + archivos críticos + **`DirectoryMembershipEvidence`**) con `reference_digest` recomputado al cargar (una referencia editada no autoriza un árbol distinto). **Schema v2** (la versión v1 no incluía membership y es incompatible: fail-closed). `directory_membership.directories` se exige estrictamente `list[str]` (sin casts silenciosos).
2. `construir_referencia_stock` / `cargar_referencia_stock` (read-only sobre la fuente, schema cerrado). La captura sella también los directorios (incluidos los vacíos, que el TreeDigest no ve) y `adoptar_copia_stock` los compara contra la referencia.
3. `adoptar_copia_stock`: verificación contra la referencia con **`runtime_vault.golden.verify_golden_master`** (no “hashear la carpeta y confiar”), re-medición fresca posterior, rechazo de reparse/junction/hardlinks, solape con storage/Managed Source y ubicación dentro de `steamapps`. **FIX 1 (auditoría)**: además compara la membership de directorios observada contra la de la referencia; borrar/agregar un directorio vacío ya no pasa inadvertido.
4. `publicar_generation_desde_stock`: copia sellada a `versions/<generation-id>/` con `copiar_arbol_independiente` (contenido, nunca hardlinks), verificación fresca de digest/membership/links/identidad/independencia, **sidecar de procedencia v2** (embebe la referencia completa, no sólo 64 caracteres) y metadata `provider="stock"` (`appid`/`buildid` nulos). **FIX 2 (auditoría)**: toda la operación (copia → verificación → sidecar → metadata → verificación final) corre en **una sola** sección de `participant_lock`, con el sidecar **antes** que la metadata (la metadata es el punto de commit; una Generation “válida” sin procedencia deja de ser posible por crash). **FIX 3**: el loader del sidecar recomputa la referencia embebida y la cruza tres vías (referencia == sidecar == árbol observado) y contra la metadata de autoridad (provider/tree/runtime/críticos); v1 se rechaza con mensaje de re-publicación.
5. `crear_runtime_clone_desde_generation`: árbol del Clone fuera del storage, copia independiente, verificación de `st_nlink==1`, sin inodos compartidos con Generation/source, membership sellada (incluye directorios vacíos), y registro `PROVISIONED` con `RuntimeSetupEvidence` mínimo content-bound (`SkyrimSE.exe` + loader + DLL de SKSE del build exacto). **FIX 2**: validación de Generation/sidecar, pre-chequeo de id ocupado, setup de la Generation **antes** de copiar y publicación del registro ocurren bajo el mismo lock; si el árbol ya existe, hay **recuperación explícita** `PROVISIONING → PROVISIONED` (reintento idempotente) o fail-closed con “reconciliación manual” (nunca borrado automático).
6. `verificar_sin_multilinks` como primitive pública de independencia.
7. Corrección de la especificación **P7-G/SFR-21** (ADR 0012 §SFR-21 y §29.7; matriz del inspector): una actualización legítima de Steam **puede** cambiar sus hashes; la invariante es que Sky-Claw **no escribe** superficies de Steam y el inventario/hashes del **Clone** no cambia.
8. Runbook P7: `docs/operations/p7_golden_master_runbook.md`.
9. **Auditoría posterior** de la hipótesis de membership, del alcance del lock y de la autoridad del sidecar (detalle y evidencia en §14): 3 hallazgos corregidos, 13 tests nuevos (RED→GREEN) y 3 mutantes nuevos muertos.

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
| **TDD GREEN** `tests/test_frozen_runtime_p7_provisioning.py` | **53 passed** (31 base + FIX 1: 4 + FIX 2/3: 10 + CodeRabbit ronda 1: 5 + ronda 2: 3) |
| Regresión suites relacionadas (P4-S2 participant wiring, P2 generations/independence/storage, P5-B, P3 contract/candidates) | **172 passed, 4 skipped** (anclas P4-S2 actualizadas con las primitivas `*_bajo_lock` nuevas) |
| Inspector P7 tras corrección de P7-G (`test_p7_inspector_output.py`) | **25 passed** (incluye ancla de `steamapps/temp/**`) |
| Mutantes discriminantes | **16/16 muertos**: (M1) `st_nlink`; (M2/M2b) `reference_digest` del loader y digest de adopción; (M3) membership del sidecar; **(M5)** comparación de membership de referencia; **(M6)** recomputo de `reference_digest`; **(M7)** recuperación `PROVISIONING`; **(M8)** cast silencioso `str(d)`; **(M9)** `DirectoryMembershipError` fuera del loader; **(M10)** `UnicodeDecodeError` fuera del sidecar; **(M11)** `DirectoryMembershipError` fuera de `_parsear_membership`; **(M12)** `steamapps/temp/**` fuera de P7-G; **(M13)** sólo forma cruda de la ruta Stock (alias 8.3); **(M14)** `expected_runtime` sin validación de tipos; **(M15)** `INDETERMINATE` degradado a INVALID; **(M16)** idempotencia PROVISIONED sin re-verificar. Restaurado y suites verdes |
| Ruff (`ruff check` + `ruff format --check` sobre `sky_claw/`, `scripts/`, `tests/`) | **verde** (gate CI, ruff 0.15.11 lockeada) |
| Mypy (`mypy sky_claw/ scripts/`) | **verde**, 430 archivos |
| Evidencia de mutantes | `E:\Skyclaw_A1_Evidence\mutation_results.json` (4 base) + `mutation_results_fixes.json` (M5–M16) |

CI del PR (calificado por revisión):
- `77f9d067` → run [42123648897](https://github.com/FacundoSu1986/Sky-Claw/actions/runs/42123648897) **SUCCESS** (16/16 checks: Windows py3.11 55m11 / py3.12 40m51, Lint/Mypy/Security/POSIX/Build/Pytest/CodeQL).
- `4f840575` → run [42298246743](https://github.com/FacundoSu1986/Sky-Claw/actions/runs/42298246743) **SUCCESS** (20/20 checks, incluidos CodeRabbit y Regression Oracle).
- HEAD con las correcciones de la segunda ronda: run pendiente; se registra en el PR. El fallo histórico del run `41933529936` (§1) queda como análisis de infraestructura separado.

## 7. Riesgos residuales

| Riesgo | Mitigación / estado |
|---|---|
| La referencia Stock es un ancla del operador (no prueba Steam por sí sola) | schema v2 + `reference_digest` + aprobación A1; cross-check con Managed Source IDLE pendiente (P3b/P7-A2) |
| La fuente candidata se prepara fuera del código (Stock + SKSE/root files) | referencia content-bound (árbol + directorios + críticos) verifica exactamente lo preparado; `RuntimeSetupManifest` completo sigue faltando (SFR-22) |
| Copia parcial si hay crash/ENOSPC | comprobación de espacio previa; el sidecar se escribe antes que la metadata y toda la publicación corre bajo un solo lock; artefactos parciales quedan **sin metadata** (no descubribles/UNKNOWN) y **sin borrado destructivo**; no hay GC/recovery automático de árboles parciales: decisión manual del propietario |
| Fallo entre registrar `PROVISIONING` y publicar `PROVISIONED` | el reintento con el mismo id/Generation/parent **recupera** el Clone (verifica y completa); no se pide borrar. Árbol sin registro o registro PROVISIONED sin árbol ⇒ fail-closed “reconciliación manual” |
| Drift de directorios vacíos | `DirectoryMembershipEvidence` en referencia v2; verificación a tres vías referencia == sidecar == árbol observado (tests + mutantes M5/M7 de la auditoría) |
| Tamper del sidecar/referencia | el loader recomputa `reference_digest` desde la referencia embebida, valida sha256/schema cerrado y cruza contra metadata de autoridad; **residual declarado**: un actor con escritura en el storage puede reescribir referencia+sidecar de forma *consistente*; no hay ancla externa firmada (la aprobación A1 es humana) ni MAC. Anti-forgery queda para una fase con aprobación durable |
| Linaje de procedencia en el registro del Clone | `RuntimeCloneRecord` no persiste `reference_digest` (schema actual); el sidecar es la autoridad de procedencia y se re-valida al crear. Persistirlo requiere bump de schema del registro (declarado, no en P7-A1) |
| Exclusión del lock en la publicación completa | probada en proceso (sonda desde otro hilo: BUSY) y el primitivo ya tiene anclas multiproceso P4-S2; **no** se agregó una sonda multiproceso específica de toda la publicación (declarado: el lock de SO no se re-adquiere en el mismo proceso) |
| Referencias/sidecars v1 previos | incompatibles a propósito: recapturar la referencia (schema v2) y re-publicar la Generation; no se migra procedencia a ciegas |
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

- `E:\Skyclaw_A1_Evidence\mutation_results.json` — resultados de los 4 mutantes base + restauración verde.
- `E:\Skyclaw_A1_Evidence\mutation_results_fixes.json` — resultados de los mutantes M5–M12 (8/8 KILLED) + script `mutate_fixes.py`.
- `E:\Skyclaw_A1_Evidence\ci_forensics.md` — forense del run 41933529936 y enlaces de sesión.
- `E:\Skyclaw_A0_Evidence\` — informe/evidencia de P7-A0 (inspector, inventarios, backups, hardlinks).
- CI: run `41933529936` (attempt 1 y 2, análisis histórico en §1) y run `42123648897` (**verde** sobre `77f9d067`; §6).

## 13. Anexo A — fallo Lint del PR #868: drift de dependencia (no defecto de P7)

- Al abrir el PR #868, el gate `Lint (Ruff)` falló en `sky_claw/local/frozen_runtime/clones.py:355` y `:437` (`SIM102`), **código pre-existente que este PR no toca**.
- Causa medida: el extra `dev` declara `ruff>=0.4` y CI instala con pip; el **2026-11-05T18:51Z** se publicó **ruff 0.17.4**, que activó la regla SIM102 para `elif` con `if` anidado. Reproducido en READ-ONLY sobre `main` `2b770818`: ruff 0.17.4 → 2 errores; ruff 0.15.11 (la que fija `uv.lock`) → TODO verde.
- **Fix separado (no mezclado con P7):** PR [#869](https://github.com/FacundoSu1986/Sky-Claw/pull/869) `ci/pin-ruff-version` — pin `ruff==0.15.11` en `pyproject.toml`. **Mergeado** (squash `32764aec`, un solo padre `2b770818`); #868 sincronizó `origin/main` con `git merge --no-ff` (HEAD `77f9d067`), sin rebase ni force-push.
- Sin cambios de producto en P7; los gates locales con la versión lockeada (ruff check/format, mypy, tests) están verdes.
- **Estado actual del gate (histórico + verificación):** el Lint rojo del run `42117967665` fue el drift de 0.17.4; con el pin y la sincronización, el run [42123648897](https://github.com/FacundoSu1986/Sky-Claw/actions/runs/42123648897) pasó Lint/Mypy/Security/tests Windows en `77f9d067`. Los fixes de la ronda CodeRabbit (§15) se validarán en un run nuevo.

## 14. Anexo B — auditoría del PR #868 (HEAD `8bf00d66`) y fixes

**Alcance:** auditar tres hipótesis sobre el candidato P7-A1 sin tocar instalaciones reales: (1) la referencia Stock no cubre membership de directorios; (2) el `participant_lock` no cubre toda la publicación lógica; (3) el sidecar no valida suficientemente `reference_digest`/`critical_files` contra la referencia y la metadata de autoridad. Dos sub-auditorías read-only (procedencia del sidecar y atomicidad lógica) + reproducción manual RED, todas en el worktree de auditoría.

### FIX 1 — membership ausente de la referencia Stock (confirmado)

- **Reproducción RED (manual, sintética):** Stock con `Data/EmptyFolder` vacío → `construir_referencia_stock` (digest `fe0450aa…`, `tiene_membership=False`) → borrar el directorio vacío (ningún byte de archivo cambia) → `adoptar_copia_stock` devolvía `ADOPTADA_SIN_RECHAZO`. El `TreeDigest` sólo inventaría archivos: los directorios vacíos son invisibles para la referencia v1.
- **Fix:** `StockReference` v2 incluye `DirectoryMembershipEvidence` (digest + count + lista de directorios) y participa de `reference_digest`; `adoptar_copia_stock` compara membership observada contra la de la referencia; el loader exige el campo y recomputa membership/digest (closed schema). Una referencia v1 es incompatible (fail-closed; recapturar).
- **Tests RED→GREEN:** `test_rechaza_directorio_vacio_eliminado`, `test_rechaza_directorio_vacio_agregado`, `test_referencia_sin_membership_es_incompatible`. **Mutante M5** (quitar la comparación) → KILLED.
- **Revisión posterior (cast silencioso):** el loader hacía `tuple(str(d) for d in directorios)`; `str(123) == "123"` permitía que un entero que representa un directorio real pasara membership/digest y la referencia quedara autorizada con un payload fuera de schema. Corregido con validación estricta `list[str]`; test RED (payload con `directories=[123]` y digest consistente → antes **cargaba**) → GREEN con `StockReferenceInvalidError`. **Mutante M8** (restaurar el cast) → KILLED.

### FIX 2 — alcance del `participant_lock` (confirmado)

- **Hecho de partida:** `participant_lock` es **no reentrante por diseño** (`root_lock.py:969-994`: `threading.Lock` + byte-range lock; una segunda adquisición da BUSY acotado). Por eso los registradores públicos no pueden llamarse dentro de una sección del mismo root.
- **Ventana reproducida:** `publicar_generation_desde_stock` y `crear_runtime_clone_desde_generation` mantenían el lock sólo durante la copia; verificación, sidecar, metadata y publicación del registro corrían **fuera** del lock (y en secciones separadas), dejando interleavings y estados parciales: metadata escrita desde evidencia que podía cambiar antes de commitear; metadata sin sidecar; `PROVISIONED` sobre un árbol no re-verificado bajo el mismo lock; `PROVISIONING` varado sin recuperación.
- **Fix (mínimo, sin doble lock):** se agregaron variantes internas `*_bajo_lock` (`generations._registrar_metadata_bajo_lock`; `clones._registrar_runtime_clone_record_bajo_lock`, `_actualizar_lifecycle_clone_bajo_lock`) siguiendo la convención del paquete (`journal.py`, `binder_recovery.py`, `state.py`). Cada operación pública corre en **una sola** sección y usa sólo esas variantes (llamar a las públicas produciría BUSY, no deadlock: mutante conceptual descartado). Orden de commit: **sidecar antes que metadata** (la metadata es el punto de commit visible a `verificar_generation`); pre-chequeo de `clone_id` ocupado y setup de la Generation **antes** de copiar (elimina huérfanos deterministas); recuperación explícita si el árbol ya existe (`PROVISIONING → PROVISIONED` idempotente; `PROVISIONED` sin árbol ⇒ reconciliación manual).
- **Tests:** sonda desde otro hilo demuestra **BUSY** exactamente en el punto vulnerable (verificación de Generation / setup del Clone) — rojo en HEAD (`[['adquirido']]`), verde con el fix; el lock se libera ante fallo; la recuperación `PROVISIONING` completa el mismo Clone; el fallo de sidecar **no** deja metadata. **Mutante M7** (deshabilitar recuperación) → KILLED. Anclas P4-S2 actualizadas por enumeración con las primitivas nuevas (`_registrar_metadata_bajo_lock`, `_registrar_runtime_clone_record_bajo_lock`, `_actualizar_lifecycle_clone_bajo_lock`, `_recuperar_clone_existente`, `_marcar_clone_invalid_bajo_lock`).

### FIX 3 — autoridad del sidecar (confirmado)

- **Hallazgos:** v1 sólo guardaba `reference_kind` + 64 caracteres de `reference_digest`; sin payload completo no se puede recomputar, y bastaba cualquier string de 64 chars (incluido `"Z"*64`) para pasar. `critical_files`, `observed_at_ns` y `reference_kind` no se validaban estrictamente; una membership sincronizada con un drift pasaba; no había cruce contra la metadata de autoridad; un sidecar v1 obsoleto cargaba.
- **Fix:** sidecar **v2** embebe la referencia completa (misma función `_payload_referencia`, mismo parser `_reference_from_dict`), y el loader valida schema cerrado, sha256 hex, tipos no-bool, críticos no vacíos/únicos/con schema cerrado, membership autoconsistente, `observed_at_ns >= 0`, reparse del archivo, y cruza tres vías: **referencia == sidecar == árbol observado**, más **sidecar == metadata** (provider `stock`, tree, runtime, críticos, `generation_id` derivado). v1 se rechaza con mensaje de re-publicación. `(REFERENCE_SCHEMA_VERSION, SIDECAR_SCHEMA_VERSION) == (2, 2)` congelado por test.
- **Tests:** `reference_digest` alterado, `critical_files` adulterados, membership sincronizada con drift, sidecar v1 obsoleto, sidecar-first y schema frozen. **Mutante M6** (desactivar recomputo de `reference_digest`) → KILLED.

### Resultado y dictamen

- **Tests:** `tests/test_frozen_runtime_p7_provisioning.py` **50 passed** (19 nuevos RED→GREEN acumulados); regresión P4-S2 + P2/P3/P5-B **172 passed, 4 skipped**; inspector P7 **25 passed**; ruff check/format y mypy **verdes** (430 archivos).
- **Mutantes:** 12/12 muertos (4 base + M5–M12), artefactos restaurados y suite verde.
- **Riesgos residuales** (detalle en §7): no hay ancla externa firmada contra un actor con escritura consistente en storage/sidecar; `RuntimeCloneRecord` no persiste el linaje (`reference_digest`); no hay GC/recovery automático de árboles parciales; la sonda de lock es in-process (la exclusión multiproceso del primitivo ya está anclada en P4-S2).
- **Dictamen:** `P7_A1_READY_FOR_FINAL_MERGE` — CI requerido completo en verde sobre el HEAD sincronizado `77f9d067` (run 42123648897: Windows py3.11/3.12, Lint/Mypy/Security/POSIX/Build/Pytest/CodeQL), sin conflictos ni aprobaciones obligatorias pendientes. Los fixes de la ronda CodeRabbit (§15) agregan tests/mutantes y requieren el CI del HEAD final; **no** se ejecutó provisioning real ni se tocaron Steam/MO2/Skyrim/runtime/Stock. PR #868 sigue abierto, sin merge.

## 15. Anexo C — ronda de revisión CodeRabbit (HEAD `77f9d067`)

Revisión automática del **2026-11-05T23:55Z** sobre `77f9d067`: 5 hallazgos verificados como válidos y corregidos con TDD donde aplica. Sin refactors cosméticos.

### P1 — errores tipados en el loader de referencia
- **RED:** `cargar_referencia_stock(b"\xff…")` levantaba `UnicodeDecodeError` (no es `JSONDecodeError`); una membership con duplicados (`["Data","Data"]`) o traversal (`["../escape"]`) levantaba `DirectoryMembershipError`, que no era `ValueError` y escapaba del `except (TypeError, ValueError)`.
- **GREEN:** `json.loads` atrapa `(JSONDecodeError, UnicodeDecodeError)` → `StockReferenceInvalidError`; `_reference_from_dict` y `_parsear_membership` incluyen `DirectoryMembershipError` → `StockReferenceInvalidError`/`ProvisioningError`, respectivamente.
- **Tests:** `test_rechaza_referencia_bytes_no_utf8_con_error_tipado`, `test_rechaza_membership_invalida_con_error_tipado[duplicado|traversal]`, `test_sidecar_membership_invalida_es_provisioning_error`. **Mutantes M9/M11** → KILLED.

### P1 — sidecar con UTF-8 corrupto
- **RED:** `ruta.read_text(encoding="utf-8")` levantaba `UnicodeDecodeError` (ValueError, no OSError); escapaba de `crear_runtime_clone_desde_generation`, que sólo atrapa `ProvisioningError`/`FrozenRuntimeError`/`OSError`.
- **GREEN:** handler explícito → `ProvisioningError`; el Clone devuelve `success=False` con mensaje UTF-8, nunca una excepción cruda.
- **Test:** `test_sidecar_no_utf8_devuelve_resultado_tipado`. **Mutante M10** → KILLED.

### P2 — runbook: crear/admitir el padre del Clone antes de A4b
- El layout decía “no crear todavía” y el procedimiento no tenía paso de creación; `crear_runtime_clone_desde_generation` exige que `<clone-parent>` ya exista: A4b fallaba **después** de publicar la Generation.
- **Fix:** paso **A4-prep** antes de A4a: crear el padre bajo aprobación A4 (`mkdir(parents=True, exist_ok=False)` sólo si falta), validar directorio/contenido ajeno, y `admitir_clone_root_path(...)` READ-ONLY sobre `padre/<clone-id>`; el layout aclara que el provisionador no lo crea.

### P2 — inspector: `steamapps/temp/**` en la matriz P7-G
- La matriz emitida nombraba sólo `staging/downloading`; el ADR (SFR-21) también prohíbe escribir `steamapps/temp/**`.
- **Fix:** criterio P7-G ahora enumera `appmanifest`, `staging/downloading`, `steamapps/temp/**` y config de Steam. **Test ancla** `test_la_matriz_p7_g_nombra_steamapps_temp` (RED antes del cambio); **mutante M12** → KILLED.

### P3 — informe: estado real del CI
- §6 registra el run [42123648897](https://github.com/FacundoSu1986/Sky-Claw/actions/runs/42123648897) **SUCCESS** sobre `77f9d067` (16/16 checks) como estado actual, separado del análisis histórico de infraestructura del §1; §13 refleja que #869 se mergeó (`32764aec`) y que el Lint quedó verde tras la sincronización.

### Segunda ronda CodeRabbit (HEAD `4f840575`)
La revisión posterior marcó 10 hallazgos nuevos; los válidos se corrigieron con TDD donde aplica:

- **P1 (Major) aprobación A1:** el loader prueba integridad interna, no que la referencia sea la aprobada. Runbook §6/§7 ahora registra el digest aprobado en `stock_reference.approved_digest.txt` y A2/A3 lo comparan antes de mutar.
- **P2 storage solapado:** §3/§7 agregan gates manuales: storage no solapa Stock/Managed Source y no está bajo `steamapps` (la admisión de código sólo rechaza `steamapps/common` y ancestros `steamapps`).
- **P2 admitir antes de crear:** A4-prep calcula `admitir_clone_root_path` **antes** del `mkdir`, crea, y **re-admite** después; no se crea nada en una ruta que la admisión rechazaría.
- **P2 Managed Source:** A3/A4b/verificaciones pasan `managed_source_root` observado a `adoptar_copia_stock`/`crear_runtime_clone_desde_generation`/`verificar_runtime_clone`.
- **P2 re-verificar la fuente tras A4:** el bloque post-A4 re-ejecuta `adoptar_copia_stock` (READ-ONLY, árbol + membership) y aborta si cambió.
- **P2 evidencia P7-G:** la matriz exige **atribución de escrituras por proceso** sobre appmanifest/staging/downloading/temp/config; hashes antes/después no identifican autor si Steam escribe concurrentemente.
- **P2 ruta canónica Stock:** `adoptar_copia_stock` resuelve `strict=True` y evalúa la forma cruda **y** la canónica contra `steamapps` y solapes (cierra alias 8.3). Test `test_rechaza_area_steam_por_forma_canonica`; **mutante M13** → KILLED.
- **P2 runtime types:** el loader exige `expected_runtime` strings no vacíos; antes un payload con enteros y digest consistente cargaba y la adopción reventaba con `AttributeError`. Test `test_rechaza_runtime_no_string_aunque_el_digest_coincida`; **mutante M14** → KILLED.
- **P2 INDETERMINATE:** una re-verificación INDETERMINATE ya **no** degrada el registro a INVALID (terminal); deja PROVISIONED y devuelve error reintentable. La idempotencia PROVISIONED ahora **siempre** re-verifica el Clone completo antes de devolver éxito. Test `test_verificacion_indeterminada_no_marca_invalid_y_el_reintento_reverifica`; **mutantes M15/M16** → KILLED.
- **P3 CI calificado por revisión:** §6 separa los runs verdes de `77f9d067` (42123648897) y `4f840575` (42298246743); el HEAD final se registra en el PR.

### Verificación de la ronda
- `tests/test_frozen_runtime_p7_provisioning.py`: **53 passed**; `test_p7_inspector_output.py`: **25 passed**; mutantes **16/16 KILLED**; ruff check/format y mypy verdes. El CI del HEAD con estos fixes se valida en el run posterior a este anexo.

