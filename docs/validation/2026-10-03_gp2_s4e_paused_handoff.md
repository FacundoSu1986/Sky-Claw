# GP2-S4E — Handoff canónico de PAUSA (archivado de ingeniería)

```text
STATUS = PAUSED / DEFERRED
FECHA  = 2026-10-03

reason:
    limited project staffing/capacity

technical rejection:
    NO

intended future resumption:
    YES
```

> GP2-S4E is being intentionally paused because the project currently does not
> have sufficient engineering and review capacity to complete and maintain this
> subsystem to the standard required by its security and recovery guarantees.
>
> This decision does not constitute a technical rejection of the architecture or
> the work completed to date.
>
> The implementation, test evidence, review history, branch, and an immutable
> archive reference are being preserved for future resumption.

> The project's immediate Steam-update protection requirement is being
> redirected toward a smaller architecture based on a Steam-managed mutable
> source installation and an isolated frozen Skyrim runtime promoted only with
> explicit user authorization.

Distinción deliberada:

```text
GP2-S4E              = stronger future Golden protection architecture
Steam Frozen Runtime = immediate simpler solution
```

Son objetivos diferentes; ninguno reemplaza técnicamente al otro. GP2-S4E
**conserva valor futuro** para: high-trust Golden Masters, autonomous AI
workflows, crash-safe mutation, forensic recovery, protected reproducible
environments y transactional filesystem/security changes. La razón de la pausa
es capacidad, no arquitectura fallida.

## Referencias inmutables

```text
repositorio : FacundoSu1986/Sky-Claw
PR          : #669 (cerrado SIN merge)
rama        : feat/runtime-vault-s4e-production-wiring  (RETENIDA, no borrar)
tag archivo : archive/gp2-s4e-paused-2026-10-03
HEAD archivo: (SHA del commit de este documento; ver el tag)

base main   : 0103ee4f6de15207032d25c254ede5cf2c01bff9
HEAD técnico: d6e0f4791a26c4e60bfc5e3e897d60b86a8d5bc1
              (+ c8a6fbfc: anchor de exigencia del breadcrumb restore, sólo tests)
ahead/behind: 15 / 0 contra main al momento del archivado
```

## Estado técnico preservado

```text
P1   CLOSED   handle cleanup causal (bd2575b9)
P2   CLOSED   candidate manifest publicado en fresh path (c144aecd)
P2.5 CLOSED   contrato de replay + árbitro atómico os.link (5a4eb7ae, c430d72f)
P3   CLOSED   pre-plan / binding-only recovery + E16 (d7318de2)
P4   CLOSED   startup read-only + fail-closed explícito (c0921bda, f4091822, 4b68ff1d)
P5   CLOSED   session lifetime + terminal mappings + lock outcome (9b19871a)
P6   CLOSED   E02 rollback físico mid-apply real (af3d414f + d6e0f479 + c8a6fbfc)

P7   NEXT     — no empezado
P8   PENDING
P9   PENDING
P10  PENDING
P11  PENDING
P12  PENDING
```

### P6 / E02 — evidencia preservada

```text
E02 mid-apply crash
→ 1 MUTATED durable (SetSecurityInfo real + POST verificado + WAL flush)
→ crash (os._exit(45) determinista; taskkill /T /F externo cubierto en E15)
→ S4-C rollback real
→ semantic PRE restoration (verify_restored_security_descriptor_by_handle)
→ ROLLED_BACK durable
→ lock released (acquired_released)
→ reacquisition succeeds

Windows py3.11: 9721 passed, 19 skipped, 0 failed  (E02 físico EJECUTADO y PASS)
Windows py3.12: 9721 passed, 19 skipped, 0 failed  (E02 físico EJECUTADO y PASS)
Qodo, Lint, Mypy (incl. --platform win32), Security, CodeQL: PASS
```

Contrato de restore: **SEMÁNTICO** (owner, group, DACL exhaustiva — ACEs y
orden canónico — y `SE_DACL_PROTECTED` contra el PRE autorizado;
`target_dacl.py`, ADR 0010 §12.2). **NO** es identidad raw de bytes del
Security Descriptor: Windows reserializa con layout/padding equivalente entre
`SetSecurityInfo`/`GetSecurityInfo`. No afirmar raw SD byte identity al
retomar ni al escribir nuevos tests.

```text
Counters al cierre de P6:
VALID_P0=0
VALID_P1=0
VALID_P2=0
VALID_MAJOR_FUNCTIONAL=2   (P7 E08, P8 E04–E07)
```

## Trabajo restante (reanudar desde P7)

### P7 — E08

```text
COMMITTED durable
→ crash antes de lock.release()
→ orphan lock real
→ restart
→ S4-D normalization
```

### P8 — E04–E07

Crash/resume real en cada gate de S4-D:

```text
E04 VERIFYING_GP1
E05 VERIFYING_RV2
E06 VERIFYING_NODE_SET
E07 ARCHIVING_BACKUP
```

### P9

```text
temp guard
causal structural anchors
remaining test-strength issues
```

### P10

```text
NTFS probe
ctime probe
R2 recalculation
```

### P11

```text
ADR 0012
review thread reconciliation
documentation cleanup
```

### P12

```text
full validation
Windows CI
finding counters
final merge readiness
```

## Limitaciones registradas (NO son defectos cerrados)

```text
REAL USER GOLDEN = NO           (el RIG vive bajo %TEMP%; nunca tocó un Golden real)
POWER LOSS       = NOT CLAIMED  (crash de proceso sí; corte de energía no demostrado)
GP1/QUIESCENCE RIG = DEGRADED   (puerto de verificación aprueba gates en el RIG)
PACKAGED HELPER  = UNPROVISIONED (PACKAGED_HELPER_PROVISIONING_STATUS = UNRESOLVED)
```

Además PREEXISTING (verificado contra `origin/main` revalidado):
`test_apply_real_con_fallo_dispara_rollback_exacto` falla en runner local
**no elevado** (clase TokenElevation); corre en CI windows-latest (elevado).

## Invariantes a respetar al retomar

```text
HANDLE > pathname
STAGING != AUTHORITY
OBSERVATION != AUTHORIZATION
durable evidence > process memory
fail closed on ambiguity
ownership exactly once
no SeDebugPrivilege
no close→reopen lock transfer gap   (S4-C transfiere el handle vivo a S4-D)
```

## Procedimiento de reanudación

1. Leer este handoff y `docs/validation/2026-10-03_s4e_closure_handoff.md`
   (estado del corte anterior; la tabla P1–P12).
2. Inspeccionar PR #669 y los review threads pendientes (P7–P11 se
   conservaron deliberadamente sin resolver: son contexto de reanudación).
3. Comparar la rama archivada contra el `main` FUTURO: medir drift
   arquitectónico antes de cualquier rebase. NO mergear la rama archivada a
   un main futuro sin ese análisis.
4. Reejecutar la evidencia Windows (elevada):
   `pytest -k runtime_vault -q` y las suites focales S4-C/S4-D/S4-E.
5. Recontar findings (VALID_P0/P1/P2/MAJOR) desde cero.
6. Continuar desde P7 (E08 orphan COMMITTED).

## Nota de alcance del archivado

Este slice fue exclusivamente documental/operativo: cero cambios de producción
y de tests propios (se integró vía fast-forward `c8a6fbfc`, anchor de tests de
otra sesión del mismo PR). `main` no se tocó; S4-A…S4-D no se revierten — eso
sería una decisión arquitectónica independiente.
