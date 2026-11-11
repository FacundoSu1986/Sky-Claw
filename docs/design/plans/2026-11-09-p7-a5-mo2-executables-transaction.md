# P7-A5 MO2 Executables Transaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement strictly transactional and recoverable migration of `gamePath` and `[customExecutables]` in MO2 towards an independent Runtime Clone.

**Architecture:** Create `mo2_executables_plan.py` for immutable classification (5 states) and plan generation. Extend `mo2_ini_escritura.py` for simultaneous surgical mutation of `gamePath` and approved executables with byte-for-byte preservation and post-write readback. Update `binder_recovery.py` to persist executable mutations and recognize torn writes. Integrate plan validation in `binder_preflight.py` under `STOCK_INDEPENDENT_TRANSITION`.

**Tech Stack:** Python 3.11/3.12, pytest, dataclasses, atomic file replacement, Windows file locking semantics.

**Spec:** PR 2 specifications from user prompt, ADR 0012 (§13, §29, §34, §35), ADR 0015 (§12, §13).

## Global Constraints

- Never modify real host installations (`G:\Modding\*`, Steam, MO2, real `config.toml`, Stock, Generation, Clone).
- `EMISION_PRODUCTIVA_HABILITADA = False` strictly preserved.
- Purely synthetic rigs and fixtures in `tmp_path`.
- All tests and docstrings in Spanish.
- Strict TDD: NO production code without failing test first.

## Review Focus

1. Paths containing sub-strings resembling runtime root (e.g. `Tools/Skyrim_Runtime_Tools/app.exe`) must not be mangled by global substring replacement.
2. Custom executable entries with arguments containing quotes or spaces must preserve arguments byte-for-byte.
3. Obsolete or missing target binaries must fail closed with `OBSOLETA_O_INEXISTENTE` and block preflight.
4. Idempotent write calls must produce zero disk writes and zero mtime modifications.
5. Crashed writes (torn bytes) must be detected as own interrupted writes and rollback to snapshot.

---

### Task 1: Taxonomy and Planning for `[customExecutables]` (R1)

**Files:**
- Create: `sky_claw/local/frozen_runtime/mo2_executables_plan.py`
- Test: `tests/test_frozen_runtime_p7_a5_mo2_executables_transaction.py`

**Interfaces:**
- Produces:
  - `ClasificacionEjecutableMO2(StrEnum)`: `MIGRABLE_AL_CLONE`, `EXTERNA_PRESERVABLE`, `DEPENDIENTE_DE_STEAM`, `OBSOLETA_O_INEXISTENTE`, `AMBIGUA_O_BLOQUEADA`
  - `MutacionCampoEjecutable(index: int, campo: str, valor_previo: str, valor_nuevo: str)`
  - `EntradaPlanEjecutableMO2(...)`
  - `PlanMigracionEjecutablesMO2(...)`
  - `planificar_migracion_ejecutables(ejecutables, outgoing_runtime_root, target_clone_root, managed_source_root) -> PlanMigracionEjecutablesMO2`

- [ ] **Step 1: Write failing tests for taxonomy and planning**
- [ ] **Step 2: Run pytest to verify tests fail (RED)**
- [ ] **Step 3: Implement `mo2_executables_plan.py` with 5 closed states and semantic path relocation**
- [ ] **Step 4: Run pytest to verify tests pass (GREEN)**
- [ ] **Step 5: Verify edge cases (arguments with quotes, substring traps, non-existent binaries)**

---

### Task 2: Surgical Writing Extension in `mo2_ini_escritura.py` (R2)

**Files:**
- Modify: `sky_claw/local/frozen_runtime/mo2_ini_escritura.py`
- Test: `tests/test_frozen_runtime_p7_a5_mo2_executables_transaction.py`

**Interfaces:**
- Consumes: `PlanMigracionEjecutablesMO2`, `MutacionCampoEjecutable` from Task 1.
- Produces:
  - `prever_bytes(contenido, nuevo_game_path, plan_ejecutables=None) -> bytes`
  - `actualizar_game_path_por_handle(retenido, nuevo_game_path, *, plan_ejecutables=None, al_escribir=None) -> bytes`
  - `actualizar_game_path_en_ini(ini_path, nuevo_game_path, *, plan_ejecutables=None) -> None`

- [ ] **Step 1: Write failing tests for simultaneous mutation of `gamePath` and custom executables**
- [ ] **Step 2: Run pytest to verify tests fail (RED)**
- [ ] **Step 3: Implement line builder updates and structural preservation checks in `mo2_ini_escritura.py`**
- [ ] **Step 4: Implement readback verification for `gamePath` and custom executables**
- [ ] **Step 5: Run pytest to verify tests pass (GREEN)**
- [ ] **Step 6: Verify idempotency (no disk write, no mtime bump on re-execution)**

---

### Task 3: Recovery and Rollback Integration (R3)

**Files:**
- Modify: `sky_claw/local/frozen_runtime/binder_recovery.py`
- Modify: `sky_claw/local/frozen_runtime/runtime_clone_binder.py`
- Test: `tests/test_frozen_runtime_p7_a5_mo2_executables_transaction.py`

**Interfaces:**
- Consumes: `PlanMigracionEjecutablesMO2` from Task 1, `prever_bytes` from Task 2.
- Produces:
  - Durable persistence of executable mutations in `RegistroRecuperacion`.
  - Accurate `_coincide_con_lo_escrito` and `_es_escritura_propia_interrumpida` incorporating executable mutations.
  - Comprehensive rollback of both `gamePath` and `[customExecutables]`.

- [x] **Step 1: Write failing tests for crash and interrupted write detection with custom executables**
- [x] **Step 2: Run pytest to verify tests fail (RED)**
- [x] **Step 3: Update `RegistroRecuperacion`, `capturar_registro`, and `_coincide_con_lo_escrito`**
- [x] **Step 4: Update `runtime_clone_binder.py` to pass plan to writer and recovery**
- [x] **Step 5: Run pytest to verify tests pass (GREEN)**

#### Recovery & Rollback Taxonomy (P7-A5 PR 2 Integridad):
- **Rollback en proceso (autoría conocida):** Restaura baseline si bytes en disco coinciden con la escritura bajo exclusión; rechaza y preserva evidencia si un tercero alteró el archivo tras liberar exclusión.
- **Recuperación en frío (sin autoría demostrable):** Fail-closed; clasifica superficies divergentes como `escritura_ajena`, no sobrescribe el archivo en disco y archiva copia inmutable en `recovery_evidence/`.
- **Intervención explícita del propietario:** Resolución soberana; operador decide entre forzar restauración (`escritura_intentada=True`) o reconciliar el journal.
- **Garantías de corte eléctrico:** `POWER_LOSS_GUARANTEE = NOT_CLAIMED`; durabilidad demostrada ante caídas de proceso (`PROCESS_CRASH_RECOVERY = DEMONSTRATED`) mediante `os.replace` atómico.

---

### Task 4: Preflight Governance Integration (R4)

**Files:**
- Modify: `sky_claw/local/frozen_runtime/binder_preflight.py`
- Test: `tests/test_frozen_runtime_p7_a5_mo2_executables_transaction.py`

**Interfaces:**
- Consumes: `planificar_migracion_ejecutables` from Task 1.
- Produces:
  - Strict validation of executables under `STOCK_INDEPENDENT_TRANSITION`.
  - Block with `CONFIG_DIVERGENCE_DETECTED` on Steam-dependent, obsolete, or ambiguous entries.
  - Pass when all entries are `MIGRABLE_AL_CLONE` or `EXTERNA_PRESERVABLE`.

- [x] **Step 1: Write failing tests for preflight blocking on unmigrated / obsolete / steam executables**
- [x] **Step 2: Run pytest to verify tests fail (RED)**
- [x] **Step 3: Wire executable plan validation in `evaluar_binder_preflight`**
- [x] **Step 4: Run pytest to verify tests pass (GREEN)**

---

### Task 5: Quality Gates, CI Verification, and Draft PR (R5)

**Files:**
- All changed files.

- [x] **Step 1: Run complete test suite (P7, P5C, P4, AST invariant tests)**
- [x] **Step 2: Run `ruff check`, `ruff format --check`, and `mypy`**
- [x] **Step 3: Verify zero productive mutations and `EMISION_PRODUCTIVA_HABILITADA = False`**
- [x] **Step 4: Commit changes with descriptive messages in Spanish**
- [x] **Step 5: Push branch and create GitHub Draft PR**
- [x] **Step 6: Monitor CI runs until green**
- [x] **Step 7: Deliver final report with SHA, CI status, and verdict**
