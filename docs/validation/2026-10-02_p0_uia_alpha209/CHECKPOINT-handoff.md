# CHECKPOINT — #661 P0 evidence + runner R1/R2/R3 prep (cerrado)

> **Estado: `P0_EVIDENCE_PUBLISHED` + `RUNNER_FIXES_READY_FOR_IMPLEMENTATION`.**
> Documento de cierre de la investigación. El punto de guardado ya aplicó.

## Estado de Git (final)

```text
origin/main                      0103ee4f6de15207032d25c254ede5cf2c01bff9  (sin drift)
primary worktree                 E:\Skyclaw_Main_Sync
primary branch                   feat/runtime-vault-s4e-production-wiring  (dirty — Runtime Vault S4E preservado, NO tocado)
P0 worktree                      C:\Worktrees\Sky-Claw-p0-alpha209-uia
P0 branch                        research/dyndolod-p0-alpha209-uia
P0 commits locales               c8edec9e · 962f1835 · c285123f
Evidencias commiteadas           docs/validation/2026-10-02_p0_uia_alpha209/  + tests
PR                               https://github.com/FacundoSu1986/Sky-Claw/pull/670
```

## FASE A — evidencia P0 (cerrada)

- Inventario y auditoría de JSON ↔ reporte: **sostenida** (5 acciones, 5 invokes, 0 reintentos, 0 ambigüedades, 0 residuos; presets: drift detectado por la sonda y restaurados por el OPERADOR post-corrida, SHA-verificado — la sonda nunca restauró).
- Estado de `environment.json` reconciliado a `P0_PASS` y confirmado.
- Sanitización: username y secreto revisados. `CHECKPOINT-handoff.md` corrompido por un paso intermedio de UTF-8/ANSI — reescrito a limpio.
- Tests del probe: `tests/test_p0_probe_alpha209.py` (18 tests) en `tests/` para que el gate de CI lo vea.
- PR: **#670** creado y pusheado.
- #661 actualizado por comentario con `P0_PASS` + runner blockers. **No cerrado**.

## FASE B — runner R1/R2/R3 (reproducción cerrada)

CR-11 — dos dimensiones independientes (no mezclar):

```text
resolution_status = OPEN / FIXED        (¿el defecto está corregido en el código?)
evidence_status   = REPRODUCED / NOT_REPRODUCED / UNKNOWN   (¿hay reproducción?)
```

| defecto | resolution_status | evidence_status | reproducción | invariante | plan |
|---|---|---|---|---|---|
| R1 | OPEN | REPRODUCED | `test_r1_cancelacion_to_thread_no_mata_al_worker` + anchor AST | `worker terminal BEFORE rollback BEFORE lease release` | ver `runner-defects-plan.md` R1 (shield NO alcanza solo) |
| R2 | OPEN | REPRODUCED | `test_r2_copytree_atraviesa_junction_mientras_medidor_no` + admission real | `inventory == copyable` + tree ownership | ver `runner-defects-plan.md` R2 (gate pre-copia link-aware) |
| R3 | OPEN | REPRODUCED | `test_r3_segunda_cancelacion_interrumpe_la_limpieza` + anchor AST | `ONE cancellation-resistant cleanup operation` (kill → cancel helpers → drain terminal → close_job → recién entonces propagar) | ver `runner-defects-plan.md` R3 — **NO** `suppress` alrededor de awaits independientes |

`tests/test_runner_defects_p1_p2.py`, 6 tests. Ningún fix productivo incluido.

## Links

- PR: `https://github.com/FacundoSu1986/Sky-Claw/pull/670`
- Issue: `https://github.com/FacundoSu1986/Sky-Claw/issues/661`
- Tracker de defects: `docs/pending_ooda_status.md` (entrada RUNNER_P2_DOUBLE_CANCEL; R1/R2 → #592)
- Rig comparativo previo: `docs/validation/2026-09-15_uia_gate_v2_rig.md`

## Cierre limpio

```text
RUNNER_P1_PACKAGING_CANCEL  resolution=OPEN  evidence=REPRODUCED
RUNNER_P1_REPARSE_COPY      resolution=OPEN  evidence=REPRODUCED
RUNNER_P2_DOUBLE_CANCEL     resolution=OPEN  evidence=REPRODUCED
→ RUNNER_FIXES_READY_FOR_IMPLEMENTATION
```
