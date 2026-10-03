# CHECKPOINT â€” #661 P0 evidence + runner R1/R2/R3 prep (pausa)

> **Fecha de pausa:** 2026-10-03 Â· **Estado global: `P0_EVIDENCE_READY` (parcial) â€” pausado a pedido del usuario.**
> Este archivo es un punto de guardado para reanudar. NO es una afirmaciÃ³n de cierre.

## Estado de Git (verificado al pausar)

```text
origin/main                      0103ee4f6de15207032d25c254ede5cf2c01bff9  (sin drift)
primary worktree                 E:\Skyclaw_Main_Sync
primary branch                   feat/runtime-vault-s4e-production-wiring  (dirty â€” Runtime Vault S4E ACTIVO)
primary HEAD                     bd2575b9â€¦  (avanza bajo el otro agente; NO TOCAR)
P0 worktree                      C:\Worktrees\Sky-Claw-p0-alpha209-uia
P0 branch                        research/dyndolod-p0-alpha209-uia
P0 HEAD                          0103ee4f = base = origin/main
P0 commits locales               ninguno (solo untracked)
P0 untracked                     docs/validation/2026-10-02_p0_uia_alpha209/
procesos residuales              TexGen/DynDOLOD/LODGen = 0
```

**Regla vigente:** no reset/clean/stash/checkout destructivo sobre el principal; no tocar el checkout de Runtime Vault.
Ninguna correcciÃ³n de runner fue commiteada. NingÃºn fix de P1â€“P9 implementado.

## FASE A â€” evidencia P0 (en curso, no publicada)

### Hecho
- Inventario de evidencia completo y coherente:
  - `final-report.md` (P0-A â†’ `P0_A_COMPLETE_NEEDS_P0_B`), `comparison.json`, `environment.json`
  - P0-A JSON: `texgen/round1..4`, `dynodlod/round1..2`
  - P0-B JSON: `p0b/texgen/texgen_p0b.json`, `p0b/dyndolod/dyndolod_p0b.json`, `p0b/final-report-p0b.md`
  - Sondas: `probe/p0a_probe.py`, `probe/p0b_probe.py`
  - Snapshots de presets: `p0b/texgen/preset_TexGen_ORIGINAL_restorado.bin`, `preset_TexGen_POSTRUN.ini`, `p0b/dyndolod/PRE_Default.ini`, `PRE_TexGen.ini`
- **AuditorÃ­a JSON â†” P0_PASS: SOSTENIDA.**
  - 5 acciones (texgen_start, texgen_exit, dyndolod_advanced, dyndolod_begin_ok, dyndolod_save_exit)
  - 5 `invoke_attempted=True`, 5 `invoke_result=S_OK`, 0 reintentos, 0 ambigÃ¼edades
  - `exit_code=0` en ambas; 0 procesos residuales en ambas
  - P0-A: `value_pattern.hresult=-0x7feceaf7 changed=False`; `legacy.hresult=S_OK changed=True`; restore `S_OK restored_ok=True`; selector `OK` candidatos=1; stale=0 â€” idÃ©ntico en ambas herramientas
  - Presets: TexGen `394e5044â†’138e8783`; DynDOLOD `e728f1f3â†’ac5d0bfd` (drift esperado; restaurados byte-exacto en la corrida)

### PENDIENTE / hallazgo a resolver al reanudar
1. **`environment.json` tiene `state: P0_BLOCKED_BY_ENVIRONMENT`** (escrito en la primera sesiÃ³n abortada). Es **stale** respecto de los reportes y de los JSON de P0-B, que sostienen `P0_PASS`. Reconciliar el campo de estado a `P0_PASS` **sin** tocar los datos medidos (no es falsear un match: es completar un estado intermedio). Dejar constancia del cambio.
2. **SanitizaciÃ³n (Â§5):** verificar que no queden username/home paths innecesarios. Los JSON ya pasaron por `sanitizar()` (marca `<USER>`), pero revisar `p0b` JSONs y reportes; el principal muestra `C:\Users\<USER>\â€¦` â†’ debe salir como `<USER>`.
3. **Tests del probe (Â§6):** no existen todavÃ­a. Agregar sÃ³lo: serialization, redaction, fingerprint stability, exactly-once ledger, P0-A mutation gate, P0-B authorization gate, no-WM_SETTEXT/BM_CLICK/keyboard/mouse. No convertir el probe en runtime.
4. **`__pycache__`** dentro de `probe/` â€” excluir del commit.
5. Renombrar/confirmar nombres: existe `dynodlod_*.json` ya normalizado a `dyndolod_p0b.json`; verificar no queden otros typos.

### No hecho
- PR P0 **no creado**.
- #661 **no comentado**.
- Nada commiteado.

## FASE B â€” runner R1/R2/R3 (NO iniciada)

```text
R1 cancellation-safe packaging      -> investigaciÃ³n pendiente
R2 reparse-safe packaging (copy)    -> investigaciÃ³n pendiente (solapa #592 finding 3)
R3 cancellation-safe process cleanup -> investigaciÃ³n pendiente
```

Puntos de partida ya localizados (sÃ³lo lectura, sin cambios):
- `sky_claw/local/tools/dyndolod_runner.py:2488` `await asyncio.to_thread(_empaquetar_sincrono)` â†’ R1
- `sky_claw/local/tools/dyndolod_runner.py:2481` `shutil.copytree(src, dst)` pelado â†’ R2
- `sky_claw/local/tools/dyndolod_runner.py:1890-1896` handler `CancelledError` sin `suppress` en el gather â†’ R3
- Familia link-aware existente: `sky_claw/app/security/links.py` (+ `tests/_symlink_guard.crear_junction`) â†’ R2
- `sky_claw/local/tools/_dir_rollback.py` `DirectoryRollback` â†’ R1 ordering

## Gate de runner (sin cambios)

```text
RUNNER_P1_PACKAGING_CANCEL = OPEN
RUNNER_P1_REPARSE_COPY     = OPEN
RUNNER_P2_DOUBLE_CANCEL    = OPEN
â†’ P0_B_PRODUCT_PIPELINE_BLOCKED
```

## Al reanudar â€” orden sugerido

1. Reconciliar `environment.json` â†’ `P0_PASS` (dejando constancia).
2. Sanitizar y excluir `__pycache__`.
3. Escribir tests del probe (Â§6).
4. Correr ruff + pytest del probe.
5. Commit + push de la rama `research/dyndolod-p0-alpha209-uia` y abrir el PR P0.
6. Comentar #661 (sin cerrarlo) con P0_PASS + `P0_B_PRODUCT_PIPELINE_BLOCKED`.
7. ReciÃ©n entonces FASE B: reproducir R1, R2, R3 por separado, con test rojo e invariante, y planes de implementaciÃ³n + collision matrix.
