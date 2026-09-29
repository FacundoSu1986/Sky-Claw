# Native Parallax Revalidation — Checkpoint

**Date**: 2026-09-28 (M3) / 2026-09-29 (resume date TBD)
**Branch**: `research/native-parallax-m3-m4-height-resize-revalidation`
**Git HEAD**: `1a52c3ea` (origin/main, includes fix #653)

## Estado de la corrida

### ✅ COMPLETADO — EXP-M3 revalidation @512

**Command:**
```
.venv\Scripts\python -m sky_claw.local.native_parallax.research.run_exp_m3 ^
  --m3-manifest docs\design\research\native-parallax\data\exp-m3-clean-authored-manifest.json ^
  --m2-manifest C:\SkyClawResearch\NativeParallax\EXP-M3\cohort_b\exp-m2-authored-manifest-local.json ^
  --resolution 512 ^
  --out C:\SkyClawResearch\NativeParallax\EXP-M3\runs\revalidation-height-resize-20260928\m3
```

**Results file**: `C:\SkyClawResearch\NativeParallax\EXP-M3\runs\revalidation-height-resize-20260928\m3\exp_m3_results.json`

**Decision**: `EXP_M3_RECONSTRUCTION_CONDITIONAL` (UNCHANGED vs historical)

**Key metrics comparison (new vs historical):**
| Metric | Historical | New | Match |
|---|---|---|---|
| RAW median_abs_corr | 0.8392 | 0.8392 | ✅ |
| RAW median_variance_ratio | 0.7043 | 0.7042 | ✅ |
| CALIBRATION corr/var/cat | 0.7716/0.5953/4 | 0.7715/0.5953/4 | ✅ |
| HELD_OUT corr/var/cat | 0.8744/0.7647/4 | 0.8754/0.7665/4 | ✅ |
| proxy spearman | +0.488 | +0.4882 | ✅ |
| AUC catastrophic | 0.229 | 0.2292 | ✅ |

**Conclusion**: Fix #653 (`resize_height` float32 precision) did NOT change M3 results. Decision unchanged.

### 🔄 PRÓXIMO — EXP-M4 calibration @512 (pendiente)

**Command (to run first — smoke test, no decision):**
```
.venv\Scripts\python -m sky_claw.local.native_parallax.research.run_exp_m4 ^
  --m3-manifest docs\design\research\native-parallax\data\exp-m3-clean-authored-manifest.json ^
  --corpus-root C:\SkyClawResearch\NativeParallax\EXP-M3 ^
  --resolution 512 ^
  --phase calibration ^
  --out C:\SkyClawResearch\NativeParallax\EXP-M3\runs\revalidation-height-resize-20260928\m4\calibration.json
```

**M4 thresholds (immutable in solver_coherence.py):**
- T_SELF_RMSE=0.10, T_SELF_CORR=0.95, T_SELF_VAR=0.85
- T_DELTA_RMSE=0.02, T_DELTA_CORR=0.1

**Historical M4 baselines** (from `data/exp-m4-results.json`):
- Decision: `EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT`
- Full: delta_rmse_median=0.0353 (>0.02 ✅), self_abs_corr=0.9992 (>0.95 ✅), self_var=0.9983 (>0.85 ✅)
- Native 1024 control: delta_rmse_median=0.0263 (>0.02 ✅)
- Manifest SHA256: `c9c16659...` (UNCHANGED — same corpus)
- Solver SHA256: `18ecf5d5...` (UNCHANGED — no solver code changed)

**Steps after calibration:**
1. Validate calibration matches historical smoke-test
2. Create freeze commit (annotate with calibration SHA)
3. Run full M4 with `--frozen-ack freeze-<sha>`
4. Compare vs historical M4
5. Check native 1024 control
6. Per-asset delta analysis

## Environment
- Python 3.11.9, NumPy 2.4.6, Pillow 11.3.0
- Windows 10 Pro 22H2 (build 19045)
- venv at `.venv`

## Files to NOT touch
- `data/exp-m4-results.json` (historical — DO NOT overwrite)
- `data/exp-m4-calibration.json` (historical — DO NOT overwrite)
- M3 trust doc (`docs/design/research/native-parallax/exp-m3-clean-authored-trust.md`)
- M3 manifest (`docs/design/research/native-parallax/data/exp-m3-clean-authored-manifest.json`)
