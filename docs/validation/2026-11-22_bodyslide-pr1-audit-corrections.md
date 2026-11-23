# BodySlide PR 1 — Auditoría interna (CHANGES_REQUESTED) y correcciones P0/P1

## Estado vigente (2026-11-23, PR #1003 — prevalece sobre los veredictos históricos de abajo)

Las secciones posteriores son el **registro histórico** de las dos auditorías
internas del 2026-11-22; sus líneas `PR1_BLOCKED_FOR_REVIEW` / `PROCESS_HOLD`
describen el estado de ese día y quedan superadas por este bloque.

- **Hallazgos cerrados (con test RED→GREEN):** P0-1, P0-2, P0-3, P1-5, P1-6,
  P1-7, P1-8 (primera auditoría); P0-A, P0-B, P0-C, P1-D, P1-E (segunda);
  P1-A, P1-B, P1-C y el tipo de `resolver_conflictos` (revisión independiente
  de PR #1003); P1-D, alias físicos Win32 del nombre de grupo (segunda
  revisión de #1003). Detalle en las secciones al final.
- **Worktree (P0-4) — RESUELTO:** PR 1 vive en la rama
  `feat/bodyslide-pr1-headless`, creada desde `origin/main` `255fb6d0` en el
  worktree exclusivo `.worktrees/bodyslide-pr1-headless` (convención de
  `tests/test_worktree_convention_invariant.py`), con commit propio y Draft PR
  #1003. El patch se aplicó con `git apply --check` limpio y el write-set se
  verificó byte a byte.
- **Limitaciones pendientes (no bloquean el Draft; deciden PR 2/PR 3 o el
  propietario):**
  - Sin rig real Windows/MO2/BodySlide: `-q`, junctions reales y el binario no
    se ejercitaron. **BodySlide no tiene ejecución productiva**:
    `run_bodyslide_batch` no pasa `quiet_verified` ⇒ `CAPABILITY_UNKNOWN` sin
    spawn hasta PR 2.
  - Restore best-effort en Python (crash duro del host ⇒ sin restauración;
    journal durable = PR 2).
  - `ManifiestoArtefactos.procedencia` es declarada por el caller, no
    verificada contra catálogo/hashes (PR 3).
  - **Pendiente para PR 2 — carreras de procesos:** ventana TOCTOU entre el
    chequeo físico del staging y el spawn (un enlace creado en ese intervalo
    no se detecta), y procesos concurrentes sobre el mismo staging fuera del
    lock del agente.
  - **Pendiente para PR 2 — espera indefinida:** si la siembra se cuelga en su
    hilo (I/O bloqueado), la restauración de `_BarreraDeSiembra` la espera sin
    límite; no hay timeout ni journal que la reemplace.
  - **Grupos que difieren sólo en mayúsculas** (`CBBE` / `cbbe`) son la MISMA
    carpeta en NTFS (ver sección P1-D). No se crea catálogo en PR 1.
  - El schema del agente (`schemas.py`, `_SAFE_NAME_PATTERN`) no replica las
    reglas Win32 de P1-D y `run_bodyslide_batch` deriva el target y abre el
    move-aside (`DirectoryRollback`) ANTES de llamar al runner; hoy no hay
    efecto porque el runner aborta, pero alinear esa capa queda para PR 2.
  - Atribución del log por firma con granularidad de día (falso negativo
    posible, nunca falso verde).
  - `sky_claw.local.tools.*` sigue exento de mypy en `pyproject.toml`; fuera
    de la exención quedan errores de estrechamiento de `Optional` sin efecto en
    runtime (ver sección final).

**Fecha:** 2026-11-22 · **Rama:** `arena/367dc766-sky-claw` @ `0106f85` (sin commit nuevo; P0-4 PROCESS_HOLD) · **Alcance:** PR 1 headless (no broker, no PR 2/3/4, no merge/push)

> Nota de honestidad sobre el ESTADO: el verde de la suite NO certifica el PR. P0-4 (worktree aislado) sigue abierto y el veredicto global es `PR1_BLOCKED_FOR_REVIEW`. Las correcciones de P0-1/P0-2/P0-3/P1-5/P1-6/P1-7/P1-8 están implementadas en el árbol de trabajo sin commit, con evidencia AAA (RED→GREEN) descrita abajo.

## Veredicto global

`PR1_BLOCKED_FOR_REVIEW` — 8/12 hallazgos resueltos en el árbol de trabajo; P0-4 (worktree aislado + testigo de la corrección) sigue abierto. Sin P0-4 cerrado no hay merge, no hay PR nuevo y no hay PR 2.

## Hallazgos y correcciones

### P0-1 — Orden transaccional (snapshot ANTES de la siembra)

**ANTES:** seed (mutaba Config.xml) → gates → argv → snapshot post-seed → spawn → restore post-seed. El restore revertía a la versión sembrada, dejándo la config del usuario mutada; si gates/argv fallaban después del seed, la config quedaba sembrada.

**AHORA** (`sky_claw/local/tools/bodyslide_runner.py`, `run_batch`, pasos 1–5):
1. Gate de capacidad (`quiet_verified`) → `CAPABILITY_UNKNOWN` sin mutar nada.
2. Construcción de argv (`construir_argv`) → `ARGS_INVALID` sin mutar nada.
3. Snapshot byte a byte del ORIGINAL + snapshot del log (`_snapshot_y_restaurar_archivos`, `_snapshot_log_inicial`) ANTES de cualquier mutación. Snapshot inválido ⇒ `CONFIG_PRECONDITION_FAILED`.
4. Siembra (`sembrar_config_de_bodyslide` con `expected_target_game`) — aborta antes de escribir si la precondición falla.
5. Restore byte a byte del ORIGINAL en `finally` — garantizado en todo caso: éxito, falla de seed, falla del spawn (`BodySlideTimeoutError`/`BodySlideExecutionError` propagados con restore a través del finally).

Si la seed falla, el restore se ejecuta antes de devolver el fallo (`finally` en la rama de seed — `bodyslide_runner.py` línea ~521-528). Config.xml queda BYTE-IDÉNTICA al original en el camino de éxito y de fallo de seed (tests `test_successful_run_restores_original_config_byte_for_byte`, `test_seed_failure_restores_original_config`, `test_spawn_failure_still_restores_original_config`, `test_quiet_gate_failure_leaves_config_untouched`, `test_argv_failure_leaves_config_untouched`).

**Tests:** `tests/test_bodyslide_restauracion_de_config.py` (9/9).

### P0-2 — Integridad de enlaces (restore a través del symlink)

**ANTES:** el restore SKIPEA enlaces (`if ruta.is_symlink(): continue`) — el destino externo mutado queda mutado (P0-2).

**AHORA** (`_snapshot_y_restaurar_archivos`, `sky_claw/local/tools/bodyslide_runner.py:344-410`):
- Snapshot: un symlink se lee A TRAVÉS del enlace (destino externo); se guarda el byte y el `link_target` original.
- Restore: se escribe A TRAVÉS del enlace (`ruta.write_bytes(blob)`), restaurando el destino externo byte a byte. El enlace mismo NUNCA se unlink ni se reemplaza; si el enlace desapareció, se re-crea con el target original.
- Un archivo que NO EXISTÍA al snapshot (`missing`) se elimina si apareció durante el run (igual para symlinks — `unlink` del enlace no toca el destino externo).
- Casos honestos: si el linker regular desapareció durante el run, se recrea desde el snapshot; si durante el run apareció un symlink donde antes había regular, la restauración aborta (no se pisa la config).

**Tests:** `test_process_mutates_external_target_of_symlink_and_runner_restores_it` (externo restaurado byte a byte, enlace intacto — `is_symlink()` queda True, `read_text` a través del enlace devuelve original).

### P0-3 — Atribución del log al run real

**ANTES:** `_leer_log` atribuyendo por ÚLTIMO `[#Log` — insuficiente: un log idéntico al previo atribuible al run actual era falso verde.

**AHORA** (`_snapshot_log_inicial` + `_atribuir_log_a_corrida`, `sky_claw/local/tools/bodyslide_runner.py:252-341`):
- Snapshot del log ANTES del spawn (tamaño, `mtime_ns`, firma última).
- Archivo ausente → `atribuible=False`.
- Archivo byte-idéntico al snapshot → `atribuible=False` SIN reintentos (no hay nada que esperar — P0-3 adversarial).
- Archivo cambiado pero con la misma firma última → `atribuible=False` (la corrida no se anunció; P0-3 adversarial).
- Archivo cambiado con firma nueva → `_leer_log` + reintentos acotados → `atribuible=True` sólo si la firma atribuible es la recién escrita.

**Tests (adversariales):** `tests/test_bodyslide_atribucion_de_log.py` (8/8):
- `test_log_antiguo_valido_sin_escritura_es_never_success` — log antiguo válido + run sin escribir log + nuevo `.nif` ⇒ `success=False`.
- `test_atribuir_log_con_cambios_y_sin_firma_no_atribuye` — archivo cambiado sin nueva firma ⇒ `atribuible=False`.
- `test_atribuir_log_sin_cambios_no_reintenta_y_no_atribuye` — archivo idéntico al snapshot ⇒ sin reintentos (0 llamadas a `_leer_log`).
- `test_log_de_la_corrida_se_atribuye` — firma fresca ⇒ atribuible.

### P0-4 — Worktree aislado y testigo del auditor (PROCESS_HOLD)

**Estado: ABIERTO — no implementado, NO modificado.** El audit señaló que la sesión usó el primary checkout (commit `0106f85`, branch `arena/367dc766-sky-claw`) en vez del worktree exigido (`.worktrees/bodyslide-pr1-headless`). Conforme al hallazgo: NO se hacen operaciones destructivas sobre `0106f85` ni se crea ningún worktree sin autorización. Veredicto: **PROCESS_HOLD** — no hay PR nuevo, no hay merge, no hay PR 2.

### P1-5 — TargetGame debe coincidir con el juego objetivo

**ANTES:** `_validar_target_game` sólo chequeaba int ∈ [0, 9].

**AHORA** (`sky_claw/local/tools/bodyslide_config.py:165-203`):
- `_validar_target_game(..., expected_target_game: int | None)` — si el caller pasa `expected_target_game`, el `TargetGame` del Config.xml debe coincidir EXACTAMENTE con él.
- El runner pasa `expected_target_game` desde `BodySlideConfig` (parámetro nuevo; `bodyslide_runner.py:127`).
- Si no coincide ⇒ `BodySlideConfigPreconditionError` ANTES de cualquier mutación.

**Tests:** `test_siembra_rechaza_target_game_del_juego_equivocado` (TargetGame=4 vs expected=9 ⇒ abort + Config.xml byte-idéntico), `test_siembra_acepta_target_game_que_coincide_con_el_esperado` (4==4 ⇒ siembra normal), `test_seed_failure_restores_original_config` (camino del runner).

### P1-6 — Manifesto de solo conteo ≠ SUCCESS (evidencia parcial vs. completa)

**ANTES:** `ManifiestoArtefactos` solo conteo; manifiesto satisfecho ⇒ SUCCESS.

**AHORA** (`sky_claw/local/tools/bodyslide_headless.py:379-400`):
- `ManifiestoArtefactos.verificado: bool = False` — default False (solo conteo).
- Con manifiesto satisfecho + `verificado=False` ⇒ veredicto **EVIDENCE_INCOMPLETE** — razonamiento explícito: *"manifiesto de solo conteo — no certifica completitud de mallas (requiere fuentes verificadas: catálogo/hashes, PR 3)"*.
- Con manifiesto satisfecho + `verificado=True` ⇒ SUCCESS legítimo.
- El manifesto verificado sólo es realista en PR 3 (catálogo con hashes/índices). En PR 1 ningún llamador puede declararse `verificado=True` con honestidad — el flag existe para que el contrato pueda declararlo.

**Tests:** `test_veredicto_manifiesto_de_solo_conteo_es_evidence_incomplete`, `test_veredicto_manifiesto_verificado_completo_es_success`, `test_veredicto_manifiesto_completo_solo_conteo_no_es_success` (reemplaza al test anterior `test_veredicto_manifiesto_completo_es_success` — el contrato es MÁS estricto, no más laxo).

### P1-7 — Conflictos sin ganador vs. conflictos verificables

**ANTES:** `_MARCA_DE_CONFLICTO_APLICADO = ("applied saved BuildSelection", "all write")` — un bool, mismo veredicto para dos hechos distintos.

**AHORA** (`sky_claw/local/tools/bodyslide_headless.py:58-78`):
- `all write` (línea 884: «Outfits 'A','B' all write 'shared.nif', building 'A'») — **conflicto SIN ganador** — herramienta eligió por cuenta ⇒ **FAILURE** (fail-closed).
- `applied saved BuildSelection` (línea 1477: «selected 'A', skipped N conflicting choice(s)») — **conflicto RESUELTO por selección guardada** — la elección existe pero NO es el ganador certificado del plan ⇒ **EVIDENCE_INCOMPLETE** (no SUCCESS, no FAILURE).
- `marcadores_de_corrida` ahora es `(fallo_parcial, conflicto_sin_ganador, conflicto_verificable)` — distinto por construcción.

**Tests:** `test_marcadores_distinguen_conflicto_sin_ganador_y_verificable`, `test_veredicto_conflicto_sin_ganador_es_failure`, `test_veredicto_conflicto_verificable_es_evidence_incomplete`, `test_marcadores_de_corrida_detecta_fallo_parcial_y_conflicto` (signature actualizado), `test_veredicto_rc_cero_conflicto_aplicado_no_es_success` (field actualizado + estado `FAILURE`).

### P1-8 — Re-read acotado del log

**ANTES:** sin reintentos.

**AHORA** (`_atribuir_log_a_corrida`, `bodyslide_runner.py:284-341`):
- Máximo 3 intentos con retraso fijo de 0.05 s (tope acotado por construcción).
- Archivo idéntico al snapshot ⇒ sin reintentos (0 llamadas).
- Atribución persistente en fracaso ⇒ `atribuible=False` (conservador).

**Alcance declarado:** el re-read acotado es honesto sobre la reparación real (streams wxLogStream en vuelo al terminar el proceso); NO es certificación de cumplimiento. Si la atribución falla, el veredicto es EVIDENCE_INCOMPLETE — nunca SUCCESS.

**Tests:** `test_atribuir_log_sin_cambios_no_reintenta_y_no_atribuye` (0 llamadas a `_leer_log`), `test_atribuir_log_con_cambios_reintenta_acotada_y_atribuye`.

## Tests ejecutados — verde final

Comando: `pytest tests/test_bodyslide_headless_argv.py tests/test_bodyslide_headless_contrato_ast.py tests/test_bodyslide_config_seed.py tests/test_bodyslide_postcheck_artefacto.py tests/test_contrato_argumentos_cli.py tests/test_contrato_veredicto_de_exito.py tests/test_bodyslide_restauracion_de_config.py tests/test_bodyslide_atribucion_de_log.py tests/test_bodyslide_lock.py tests/test_subprocess_orphan_prevention.py tests/test_animation_tool_consolidation.py tests/test_pandora_runner.py tests/test_borrado_recursivo.py -q`

**430 passed, 8 skipped** (skipped = tests pre-existing de symlinks/junction con skip de privilegio en la sandbox, no nuevos).

Por archivo:
- `test_bodyslide_headless_argv.py` — 49 (44 originales + 5 nuevos)
- `test_bodyslide_headless_contrato_ast.py` — 5
- `test_bodyslide_config_seed.py` — 15 (13 + 2 nuevos)
- `test_bodyslide_postcheck_artefacto.py` — 14 (13 + 1 skip preexistente)
- `test_contrato_argumentos_cli.py` — 214 + 6 skip
- `test_contrato_veredicto_de_exito.py` — verde
- `test_bodyslide_restauracion_de_config.py` — 9 (reescrito bajo P0-1/P0-2)
- `test_bodyslide_atribucion_de_log.py` — 8 (nuevo archivo, P0-3/P1-8)
- `test_bodyslide_lock.py` — verde
- `test_subprocess_orphan_prevention.py` — 5 (factory honestada para gates P0-1/P1-5)
- `test_animation_tool_consolidation.py` + `test_pandora_runner.py` + `test_borrado_recursivo.py` — verde

ruff check + ruff format + git diff --check: todo verde en los 9 archivos tocados.

## Hash de los archivos modificados

```
1e86019c2ac16c8875fb89bb216c6fbf  sky_claw/local/tools/bodyslide_headless.py
810c2ad1f045321e25523c36f0baa036  sky_claw/local/tools/bodyslide_config.py
97326bc86d282b6a25ee6dd48f1aaca3  sky_claw/local/tools/bodyslide_runner.py
aeb002eb964addfb32d6d0010e5194c2  tests/test_bodyslide_headless_argv.py
b77dc8ec1d509a05a96a748132406cfb  tests/test_bodyslide_restauracion_de_config.py
db5af2536755a856ef2e35faded473c7  tests/test_bodyslide_atribucion_de_log.py
f2f6cb76e002746141f021ac48c7e06d  tests/test_bodyslide_config_seed.py
f0acf67e2e5ea72c3c2ef544c992b686  tests/test_bodyslide_postcheck_artefacto.py
b729ac5160a381b9bc4558c3ec7e8cdc  tests/test_subprocess_orphan_prevention.py
```

## Write-set del usuario (AGENTS.md §7)

**Modificados** (9):
- `sky_claw/local/tools/bodyslide_headless.py`, `sky_claw/local/tools/bodyslide_config.py`, `sky_claw/local/tools/bodyslide_runner.py` (producción)
- `tests/test_bodyslide_headless_argv.py`, `tests/test_bodyslide_restauracion_de_config.py`, `tests/test_bodyslide_config_seed.py`, `tests/test_bodyslide_postcheck_artefacto.py`, `tests/test_subprocess_orphan_prevention.py`, y — como parte de la corrección — `tests/test_bodyslide_atribucion_de_log.py` (nuevo)

**Nuevos** (1 test): `tests/test_bodyslide_atribucion_de_log.py`.
**Nuevos** (docs): este archivo.

Los archivos `test_animation_tool_consolidation.py`, `test_pandora_runner.py`, `test_borrado_recursivo.py` quedan VERDE sin tocar.

## Estado del worktree

- Branch: `arena/367dc766-sky-claw` @ `0106f85` (PRIMARY CHECKOUT — P0-4 abierto: esto NO es un worktree aislado).
- P0-4 testigo pendiente: el hallazgo del audit reconoce que el PR 1 no se realizó en el worktree exigido. NO se aplicaron operaciones destructivas (no `git reset`, no `git rebase`, no `git amend`, no `git commit --amend`, no push, no PR).
- `0106f85` se conserva intacto; las correcciones quedan en el working tree sin commitear.

## Límites no verificables en este entorno

- **P0-4**: no se verificó un PR 1 en worktree aislado (auditoría).
- **Ruido de tests de skip**: los 8 skipped son pre-existentes (privilegios de symlinks/junctions en la sandbox, `pytest.skip` explícito).
- **mypy**: NO instalado en `.venv`; `local.tools/**` excluido del mypy repo-wide — no hay claim de cobertura.
- **No se ejecutó BodySlide real** (binarios GUI no disponibles en la sandbox ni en este entorno; MO2 no está instalado).
- **No hay integración contra el rig de producción Windows + MO2** (en rig real queda pendiente).
- La atribución por firma (último `[#Log`) NO distingue firmas del mismo día por hora; es el único delimitador que upstream ofrece en `Log::Initialize`.
- El restore byte a byte es best-effort en Python: un kill duro o crash del host entre spawn y restore deja las mutaciones del proceso en su lugar. Journal durable = PR 2.

## Límites explícitos del caller

- `expected_target_game` queda documentado como opt-in: el plan de PR 2/3 debe declarar el juego objetivo; PR 1 no lo adivina.
- `verificado=True` sólo puede declararlo PR 3 con hashes/síndices reales del catálogo.
- Los nombres de test siguen el repo (`.py` en Español), el código/merge en Español §2.

## Veredicto final

`PR1_BLOCKED_FOR_REVIEW` — correcciones P0-1/P0-2/P0-3/P1-5/P1-6/P1-7/P1-8 implementadas en el árbol de trabajo con evidencia AAA; P0-4 abierto ⇒ no hay PR, no hay merge, no hay PR 2.

---

# Segunda auditoría adversarial (CHANGES_REQUESTED) — correcciones P0-A/P0-B/P0-C/P1-D/P1-E

**Fecha:** 2026-11-22 · Rama `arena/367dc766-sky-claw` @ `28929c0` (working tree; P0-4 PROCESS_HOLD) · **Alcance:** la segunda revisión adversarial de PR 1 (5 hallazgos)

> Estado: los 5 hallazgos cerrados en el árbol de trabajo con evidencia AAA (RED→GREEN verificada por mutación). Veredicto global: **PR1_BLOCKED_FOR_REVIEW** (P0-4 sigue abierto: sin worktree aislado ni evidencia de la corrección no hay merge).

## Hallazgos y correcciones

### P0-A — Perímetro transaccional único (scan previo al spawn)

**ANTES:** `_conjunto_de_archivos_previos()` corría DESPUÉS de la siembra y FUERA del try/finally de restauración — un OSError/cancelación en ese scan dejaba la siembra persistente.

**AHORA** (`run_batch`, pasadas 4a/4b): todo lo que sigue a la primera mutación (siembra → scan → spawn → conteo → atribución) vive bajo UN try/finally que garantiza la restauración byte a byte del ORIGINAL. El scan, los counts, la atribución del log y el veredicto quedan dentro. Toda excepción (OSError, `asyncio.CancelledError`, error inesperado) restaura el original y se propaga — nunca persiste cambios ni reporta éxito.

**Tests (RED→GREEN):**
- `test_previa_de_escaneo_os_error_restaura_original` — OSError en el scan → OSError propaga + Config.xml byte-idéntico.
- `test_previa_de_escaneo_cancelacion_restaura_original` — CancelledError en el scan → CancelledError propaga + byte-idéntico.
- `test_pre_spawn_unexpected_exception_restores_and_propagates` — RuntimeError en conteo → propaga + byte-idéntico.

**Mutación verificada:** al deshabilitar el `finally` del perímetro, 9 tests fallan (incluye los 3 anteriores); con el fix, 14/14 verde.

### P0-B — Config.xml enlazado: bloqueo fail-closed + restore que verifica destino

**ANTES:** la siembra escribía a través del enlace (resolving al destino) sin verificar destino; el restore escribía a través del enlace sin verificar que el enlace siguiera apuntando al snapshot.

**AHORA:**
- Siembra (`bodyslide_config.py`): Config.xml enlazado ⇒ `BodySlideConfigPreconditionError` ANTES de cualquier lectura/mutación — ni enlace, ni destino externo, ni exe_dir se tocan.
- Restore (`bodyslide_runner.py`): si el destino del enlace cambió entre snapshot y restore (re-targeted) ⇒ `BodySlideExecutionError` fail-closed — NO se escribe el blob a través del enlace (eso sobrescribiría otro archivo). El destino B queda byte a byte intacto.

**Tests:**
- `test_un_config_enlazado_falla_cerrado_sin_pisar_el_destino` (reemplaza al test viejo `test_un_config_enlazado_conserva_el_enlace`, que asentía el comportamiento anterior).
- `test_config_enlazada_no_se_siembra_ni_se_spawnea` — spawn nunca; destino externo intacto; enlace intacto.
- `test_buildselection_retargeteado_no_se_escribe_a_traves_del_enlace` — enlace re-targeted a mitad del run → restore aborta; destino B intacto (A con la mutación — honesto).

**Mutación verificada:** al eliminar la verificación de destino en el restore, `test_buildselection_retargeteado_no_se_escribe_a_traves_del_enlace` falla (1 failure); con el fix, 14/14 verde.

### P0-C — Manifiesto verificado: requisitos positivos + procedencia + cobertura efectiva

**ANTES:** `ManifiestoArtefactos(verificado=True)` sin requisitos ni procedencia producía SUCCESS.

**AHORA** (`bodyslide_headless.py`): `ManifiestoArtefactos.__post_init__` exige, cuando `verificado=True`:

1. **Requisitos positivos coherentes** — `nif_requeridos + tri_requeridos > 0`. Un manifiesto vacío no certifica nada.
2. **Procedencia declarada** — cadena no vacía (`procedencia="catálogo:v3"`). Un `verificado` declarado por el caller no prueba nada sobre la integridad de las mallas; sin antecedente verificable, el manifiesto no autoriza SUCCESS.
3. `evaluar_veredicto` exige cobertura efectiva: conteos reales ≥ requisitos declarados — un manifiesto verificado con cobertura insuficiente sigue siendo FAILURE, no SUCCESS.

Además los requisitos deben ser enteros no negativos (booleans excluidos por `isinstance`), y `procedencia` se rechaza si está vacía o contiene NUL byte.

**P0-C (target) — `_validar_target`:** el target del batch debe ser una **ruta absoluta** (POSIX `/...`, Windows `C:/...`, UNC) y, cuando `stage_root` se declara (el runner lo pasa con el staging administrado `BodySlide_Output`), debe estar **confinado** al staging. Tests: absoluto obligatorio; confinamiento rechazado fuera; confinamiento aceptado dentro; `C:/salida` aceptado sin stage_root.

**Tests:** 8 nuevos (5 manifest + 3 target) — verificados por mutación: al vaciar `__post_init__`, los 3 tests de manifiesto fallan; con el fix, 58/58 verde.

### P1-D — Atribución del log: firma nueva post-snapshot (append + rotación)

**ANTES:** `_atribuir_log_a_corrida` rechazaba con firma idéntica al snapshot ("la corrida no se anunció") — un run nuevo cuyo log es idéntico al previo (misma fecha textual, `[#Log 2026-11-22]`) quedaba no-atribuible.

**AHORA** (`bodyslide_runner.py`): el snapshot registra `(tamaño, mtime_ns, firma, total_firmas, offset_ultima_firma, crudo)`. La atribución:

1. Archivo ausente → `atribuible=False`.
2. Archivo byte-idéntico → `atribuible=False` sin reintentos.
3. Archivo cambiado pero SIN firma nueva (mismo offset, mismo conteo, misma firma) → `atribuible=False` (adversarial).
4. Archivo cambiado con firma nueva — **append** (total_firmas creció o offset nuevo) o **rotación** (offset cambió aunque la firma sea idéntica — la rotación copia de la 2ª firma y re-escribe el archivo) → `atribuible=True` tras reintentos acotados, con reintentos 3×50ms.

Atribución desde la **firma última** — nunca se atribuyen las líneas de runs anteriores (incluidas las conservadas por la rotación).

**Tests:**
- `test_atribuir_log_con_firma_identica_en_append_atribuye` — append del mismo `[#Log 2026-11-22]` → atribuible; contenido viejo NO atribuido.
- `test_atribuir_log_con_rotacion_atribuye_sin_contenido_viejo` — rotación (>= 4 firmas) conserva las dos últimas + firma nueva → atribuible; ningún marker viejo atribuido.
- `test_atribuir_log_sin_firma_nueva_ni_cambio_de_contenido_no_atribuye` — contenido idéntico → no atribuye.

**Mutación verificada:** al eliminar el offset/count check (query-by-firma-only), `test_atribuir_log_con_rotacion_atribuye_sin_contenido_viejo` falla; con el fix, 11/11 verde.

### P1-E — Vacuous-OR en la prueba de atribución

**ANTES:** `tests/test_bodyslide_atribucion_de_log.py` (test final) afirmaba `result.estado != "EVIDENCE_INCOMPLETE" or result.artifacts_built == 1` — tautológico.

**AHORA** (mismos tests, aserciones independientes):
- `result.artifacts_built == 1`
- `result.success is False` (manifiesto sin verificar)
- `result.estado == "EVIDENCE_INCOMPLETE"`
- `"no atribuible" not in result.message.lower()` — la causa NO es log no atribuible
- `"manifiesto" in result.message.lower()` — la causa es el manifiesto (solo conteo)

## Tests ejecutados — verde final

Comando: `pytest tests/test_bodyslide_headless_argv.py tests/test_bodyslide_headless_contrato_ast.py tests/test_bodyslide_config_seed.py tests/test_bodyslide_postcheck_artefacto.py tests/test_contrato_argumentos_cli.py tests/test_contrato_veredicto_de_exito.py tests/test_bodyslide_restauracion_de_config.py tests/test_bodyslide_atribucion_de_log.py tests/test_bodyslide_lock.py tests/test_subprocess_orphan_prevention.py tests/test_animation_tool_consolidation.py tests/test_pandora_runner.py tests/test_borrado_recursivo.py -q`

**447 passed, 8 skipped** (skipped = preexistentes: symlink/junction en la sandbox).

Por archivo:
- `test_bodyslide_headless_argv.py` — 58 (44 originales + 5 manifest + 3 target + 6 fixes a aserciones existentes con procedencia)
- `test_bodyslide_headless_contrato_ast.py` — 5
- `test_bodyslide_config_seed.py` — 15 (re-escrito el test del enlazado a fail-closed)
- `test_bodyslide_postcheck_artefacto.py` — 14
- `test_contrato_argumentos_cli.py` — 214 + 6 skip
- `test_contrato_veredicto_de_exito.py` — verde
- `test_bodyslide_restauracion_de_config.py` — 14 (3 P0-A + 2 P0-B + originales)
- `test_bodyslide_atribucion_de_log.py` — 11 (3 P1-D + 1 P1-E + originales)
- `test_bodyslide_lock.py` — verde
- `test_subprocess_orphan_prevention.py` — 5 (factory confinada al staging)
- `test_animation_tool_consolidation.py` + `test_pandora_runner.py` + `test_borrado_recursivo.py` — verde

ruff check + ruff format --check: todo verde en los 10 archivos tocados.

## Hashes de los archivos modificados (post-corregido)

```
2d9323bf7564e114ed108b68b3bed73a6434eeabb1062bbd21b8803f60e2235f  sky_claw/local/tools/bodyslide_runner.py
649448932119fa468b8ff38b2fe9a3a973ed308cfb254851dcddb32246f88d42  sky_claw/local/tools/bodyslide_headless.py
60690989bab6aa602d4b6355df414114bffa9aa9b73aa03bd3551faad9e682cc  sky_claw/local/tools/bodyslide_config.py
fd1347666f3e708db7e4fdee56272aba6ebd7a6934eac29635faabe80644d2cd  tests/test_bodyslide_restauracion_de_config.py
a92bae8e479fdac54f8fbaa1052355346b57413f6326d0388001518231f5d6f1  tests/test_bodyslide_atribucion_de_log.py
0da6c5daa5e0e2607f97bf4abb150bd7781632e29aa902efcf80f63a333665b3  tests/test_bodyslide_headless_argv.py
26148bf52a0af7c19944a7d99dfad6ce5a53c99f04b2f1acde3357e7c74ce02a  tests/test_bodyslide_config_seed.py
5878cac37e7b29e0ac6b4002fd02889d316bf5b549a0a21f8537843688baa958  tests/test_bodyslide_postcheck_artefacto.py
9ee4b7ba759ab5520a17bba73140a0a0f20f9dc2c685dbe5f74f54ef78287412  tests/test_subprocess_orphan_prevention.py
cec23220784d9445d48d6750579ace0f05938a3eb0cd746a36079265c8d36650  tests/test_contrato_argumentos_cli.py
```

## Write-set de la segunda corrección

**Modificados** (8): los 3 archivos de producción + `test_bodyslide_restauracion_de_config.py`, `test_bodyslide_atribucion_de_log.py`, `test_bodyslide_headless_argv.py`, `test_bodyslide_config_seed.py`, `test_bodyslide_postcheck_artefacto.py`, `test_subprocess_orphan_prevention.py` + `test_contrato_argumentos_cli.py`.

No se tocó: `tests/test_contrato_veredicto_de_exito.py` (detector AST inmaculada), `tests/test_contrato_argumentos_cli.py` (solo fixtures — mismos flags, mismo vector), `0106f85` (no existe en este entorno — `28929c0` es HEAD; toda la historia PR 1 es working-tree sin commitear).

## Estado garantizado

- **Worktree:** ninguno creado. P0-4 (worktree aislado) sigue abierto — PROCESS_HOLD. Toda la corrección vive en el working tree del primary checkout; no hay commit nuevo.
- **P0-4 PROCESS_HOLD** intacto; no se hizo jamás commit/push/PR/merge/worktree.
- `expected_target_game=None` sigue NO certificando Skyrim SE: sigue siendo opt-in (P1-5).
- **PR1_BLOCKED_FOR_REVIEW** hasta que P0-4 cierre con evidencia del worktree aislado.

## Límites honestos

- La atribución por firma sigue sin distinguir firmas del mismo día por hora — es el único delimitador que upstream ofrece en `Log::Initialize` (P1-D mejora el handling, no introduce un delimitador nuevo).
- El restore byte a byte sigue siendo best-effort en Python: crash duro del host entre spawn y restore deja las mutaciones del proceso en su lugar. Journal durable = PR 2.
- Los 8 skipped son preexistentes (symlink/junction en sandbox).
- mypy NO instalado; `local.tools/**` excluido del repo-wide — sin claim de cobertura.
- No se ejecutó BodySlide real (binarios GUI no disponibles).

## Veredicto final (segunda revisión)

`PR1_BLOCKED_FOR_REVIEW` — 5/5 hallazgos cerrados en el árbol de trabajo con evidencia AAA (RED→GREEN con mutación verificada). P0-4 sigue abierto ⇒ no hay PR, no hay merge, no hay PR 2.

## P0-4 — Worktree aislado (verificado — no se movieron las mutaciones del checkout primario)

**Fecha:** 2026-11-22.

**Procedimiento verificado:**

1. `git worktree add --detach .worktrees/bodyslide-pr1-headless 28929c02162174ee464dc5c813b93e4a74236337` — crea el worktree aislado en modo `detached HEAD` en el mismo commit base (`28929c0`; `0106f85` no existe en este entorno y el HEAD es `28929c0`). `.worktrees/` está gitignored ✓.
2. Los 14 archivos de la PR 1 (8 modificados + 6 nuevos, verificados byte-idénticos con `cmp -s`) se copiaron al worktree.
3. Suite ejecutada EN EL WORKTREE (353 passed, 7 skipped, 11 test files de PR 1) con el `.venv` del checkout primario — la suite verde en el worktree prueba la aislamiento mecánico y la portabilidad de los archivos copiados.

**Intención de la sesión:** las mutaciones del checkout primario NO se movieron ni se revirtieron (no se hizo `git checkout -- .`, ni `git stash`, ni `git reset`): el working tree del checkout primario conserva TODAS las correcciones byte a byte, y el worktree aislado conserva la misma PR 1 con la suite verde. Cerrar P0-4 como "la PR 1 vive exclusivamente en el worktree" sigue siendo **bloqueado** hasta que el usuario confirme la operación de hard-move (eliminar las copias primarias una vez verificado que el worktree es el único hogar de la PR 1).

**Proceso no ejecutado** (no autorizado por el hallazgo hasta el cierre explícito por el usuario): hard-move + `git worktree lock`.

## Estado final (segunda revisión)

- `PR1_BLOCKED_FOR_REVIEW` — P0-A/P0-B/P0-C/P1-D/P1-E cerrados con evidencia AAA + mutación verificada.
- P0-4 parcialmente abierto: el worktree aislado existe y trae la PR 1 verificada; el hard-move del checkout primario queda para el cierre explícito del usuario.
- No se hizo ningún commit, push, merge, PR ni worktree-lock.

---

# Revisión independiente de PR #1003 (CHANGES_REQUESTED, 2026-11-23) — P1-A/P1-B/P1-C

**Base:** `feat/bodyslide-pr1-headless` @ `e2370fb7` (sobre `origin/main` `255fb6d0`).
Los tests nuevos viven en `tests/test_bodyslide_revision_independiente.py` y se
corrieron **ROJOS sobre `e2370fb7` antes del fix** (22 failed / 2 passed /
1 skipped; los 2 que pasaban son controles: destino canónico y mapping válido).

### P1-A — Aislamiento de salida por grupo — CERRADO

**Antes:** `_confinado` sólo exigía prefijo del staging: `run_batch("CBBE",
<raiz>/3BA)`, la raíz compartida (primer run) o `<raiz>/CBBE/sub` spawneaban
(RED: `await_count == 1`).

**Ahora:** `bodyslide_headless.exigir_destino_canonico` (igualdad de
componentes; gramática fijada por el canónico: en Windows separadores
indistintos, barra final ignorada, case-insensitive; en POSIX distingue
mayúsculas; `.`/`..` rechazados) y el runner la aplica contra
`bodyslide_output_target(game, group)` antes de mutar nada ⇒ `ARGS_INVALID`.
Tests: cruce de grupos, raíz compartida, subdirectorio propio, destino canónico
(control), variantes Windows (mayúsculas, `/` y `\`, barra final, otro drive,
UNC igual/otro servidor, ruta relativa) y POSIX. El caso runner con
mayúsculas distintas en disco real se saltea fuera de Windows (sólo NTFS hace
que sea la misma carpeta). Fixtures existentes que usaban un destino de otro
grupo (`"CBBE Body"` → `.../CBBE`, `"Build"` → `.../CBBE`) se corrigieron al
target canónico del grupo — es exactamente la clase de error que P1-A cierra.
`test_staging_identidad_no_verificable_symlink_en_subdirectorio` ahora se
rechaza antes (`ARGS_INVALID`); el caso físico alcanzable (carpeta del grupo
enlazada) se cubre con
`test_staging_identidad_no_verificable_carpeta_del_grupo_enlazada`
(`STAGING_UNVERIFIABLE`).

### P1-B — Snapshot de log ilegible ≠ log ausente — CERRADO

**Antes:** `_snapshot_log_inicial` devolvía `None` también ante un
`PermissionError`; el log viejo, legible después, se atribuía a la corrida:
RED con **`success=True`** (falso verde real) con artefacto nuevo y manifiesto
verificado.

**Ahora:** sólo `FileNotFoundError` en `stat` es "ausente"; cualquier otro
fallo de `stat`/lectura lanza `BodySlideExecutionError` ⇒ el runner aborta
antes de mutar y de spawnear (`CONFIG_PRECONDITION_FAILED`). En la lectura
posterior a la corrida, un log ilegible es "no atribuible".

### P1-C — Cancelación concurrente de la siembra — CERRADO

**Antes:** cancelar `await asyncio.to_thread(sembrar…)` dejaba el hilo vivo;
el `finally` restauraba con la siembra en vuelo y la siembra escribía después
(RED: "la restauración corrió con la siembra todavía en vuelo").

**Ahora:** `_BarreraDeSiembra` (lock + bandera `_restaurada`): la siembra corre
entera bajo el lock; la restauración espera a la siembra en vuelo en su propio
hilo; si la siembra no arrancó, la restauración la anula. Test determinista con
`threading.Event` (los `wait(timeout)` son sólo guardas contra cuelgues): la
restauración espera, la siembra termina, la restauración corre después y
`Config.xml` queda byte-idéntico al original.

### `resolver_conflictos` — tipo restringido — CERRADO

Firma `selecciones: Mapping[str, str]`; cualquier otro tipo ⇒
`HeadlessContractError` (antes: `ValueError` crudo con una tupla de dicts, o un
par inventado con un dict de dos claves).

### Sin cambio de contrato productivo

`run_bodyslide_batch` sigue sin pasar `quiet_verified` ⇒ `CAPABILITY_UNKNOWN`
sin spawn. No se tocó Golden Master, Frozen Runtime ni perfiles MO2; no se
inició PR 2.

---

# Segunda revisión independiente de PR #1003 (CHANGES_REQUESTED, 2026-11-23) — P1-D

**Base:** `97e6bf7a`. P1-A/P1-B/P1-C y `resolver_conflictos` aceptados.

### P1-D — Alias físicos de nombres de grupo en Win32 — CERRADO

**Antes:** `_validar_nombre` (patrón ASCII `[a-zA-Z0-9_.\- ]`) aceptaba
`CBBE.`, `CBBE `, `CON`, `nul.body`, `COM1 .tri`… El grupo es el último
componente del destino canónico; Win32 descarta puntos/espacios finales y abre
los nombres de dispositivo como dispositivo, así que esas identidades lógicas
no son carpetas físicas independientes. RED sobre `97e6bf7a`: 22 casos de
contrato aceptados y el runner **spawneaba** en los 5 casos probados
(`await_count == 1`).

**Ahora:** `bodyslide_headless._validar_nombre_de_grupo` (usado por
`construir_argv` para el grupo; los nombres de outfit no son carpetas y siguen
con `_validar_nombre`) rechaza: espacios alrededor, punto/espacio final,
dispositivos reservados `CON PRN AUX NUL CONIN$ CONOUT$ COM0-9 LPT0-9` y
superíndices `¹²³` (case-insensitive, también con extensión: se compara la
parte anterior al primer punto sin espacios finales), caracteres `<>:"/\|?*` y
de control 0-31. En el runner el rechazo ocurre en el gate de argv
(`ARGS_INVALID`): sin siembra, sin spawn, `Config.xml` byte-idéntico y nada
creado bajo el staging. Nombres válidos existentes conservados (tests):
`CBBE`, `CBBE Body`, `Build`, `3BA`, `Preset 1.0`, `CBBE_SE-v2`, `Console`,
`CONFIG`, `COM10`, `LPT`, `NULx`, `AUX_body`, `con-body`,
`BHUNP 3BBB Advanced`. Los únicos grupos usados en tests/repo antes del cambio
(`CBBE`, `CBBE Body`, `Build` y el default `CBBE` de `tools/__init__.py`) siguen
pasando.

### Riesgo documentado (no implementado): grupos que difieren sólo en mayúsculas

`CBBE` y `cbbe` son identidades lógicas distintas para el contrato (y para el
token de `-gbuild`, que BodySlide compara contra los nombres de grupo de sus
XML), pero NTFS las resuelve a la MISMA carpeta `BodySlide_Output\CBBE`. Además
`exigir_destino_canonico` compara case-insensitive contra un canónico Windows,
así que un destino `…\cbbe` se acepta para el grupo `CBBE`. Consecuencia: dos
corridas de grupos que difieren sólo en mayúsculas compartirían staging y
move-aside. PR 1 no crea catálogo de grupos ni normaliza nombres (fuera de
alcance); el lock del recurso de BodySlide en `run_bodyslide_batch` serializa
las corridas del agente, pero no distingue el aliasing. Queda para PR 2/PR 3
(catálogo o normalización canónica con evidencia del rig).

### Pendiente explícito para PR 2

- Espera indefinida de la restauración ante una siembra colgada.
- Carreras de procesos (TOCTOU staging↔spawn; procesos externos).
- La ejecución productiva **sigue deshabilitada**: `run_bodyslide_batch` no pasa
  `quiet_verified` ⇒ `CAPABILITY_UNKNOWN` sin spawn.
