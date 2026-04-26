# REFACTOR-VALIDATION — Phase D Results

**Date:** 2026-04-25  
**Scope:** `sky_claw/orchestrator/sync_engine.py` — Phase D: test adaptation and coverage validation

---

## Tool Outputs

### Ruff

```
$ ruff check sky_claw/orchestrator/sync_engine.py
All checks passed!
```

### MyPy (strict)

```
$ mypy --strict sky_claw/orchestrator/sync_engine.py
Success: no issues found in 1 source file
```

### Pytest

```
$ pytest tests/test_sync_engine.py tests/test_sync_engine_resilience.py -v
47 passed, 32 warnings in 2.25s
```

### Coverage

```
$ pytest tests/test_sync_engine.py tests/test_sync_engine_resilience.py \
    --cov=sky_claw.orchestrator.sync_engine --cov-report=term-missing -q

Name                                   Stmts   Miss  Cover   Missing
--------------------------------------------------------------------
sky_claw\orchestrator\sync_engine.py     335     66    80%   247-300, 315-325, 357, 524-572, 589-664
--------------------------------------------------------------------
TOTAL                                    335     66    80%
47 passed in 2.34s
```

---

## Coverage Summary

| Metric | Result | Target |
|--------|--------|--------|
| Coverage | **80%** | ≥ 80% |
| Statements covered | 269 / 335 | — |
| Tests passing | **47 / 47** | 100% |

### Uncovered blocks (all require active `rollback_manager`)

| Lines | Method | Reason |
|-------|--------|--------|
| 247–300 | `execute_file_operation` (rollback path) | Requires real `RollbackManager` journal |
| 315–325 | `_passive_pruning` (pruning branch) | Requires `total_size_bytes > max_size` with real stats |
| 357 | `_get_max_backup_size_bytes` null branch | Dead code — constructor always sets `_cfg` |
| 524–572 | `_check_and_update_mod_with_rollback` | Requires real `RollbackManager` context manager |
| 589–664 | `_check_and_update_mod_internal` | Requires `rollback_manager` + transaction_id |

---

## Test Inventory

### New tests added in Phase D

| Test | Covers |
|------|--------|
| `TestSyncMetricsConcurrency::test_sync_metrics_is_thread_safe` | 100 concurrent `increment_error_type` calls |
| `TestSyncMetricsConcurrency::test_record_error_increments_by_exception_type` | `record_error` by real exception type |
| `TestBoundedGather::test_empty_coroutines_returns_empty_list` | Line 341 — empty list early return |
| `TestBoundedGather::test_single_coroutine_runs_and_returns` | Basic `_bounded_gather` execution |
| `TestEnqueueDownloadRegistry::test_registry_failure_is_logged_not_raised` | Lines 752–753 — registry exception swallowed |
| `TestPassivePruningWithRollback::test_passive_pruning_stats_under_limit_noop` | Lines 311–314 — rollback_manager path, no pruning needed |
| `TestPassivePruningWithRollback::test_passive_pruning_exception_is_logged_not_raised` | Lines 330–331 — exception in get_stats |
| `TestConsumeCancelledError::test_cancelled_error_propagates_from_process_batch` | Line 800 — CancelledError re-raise in `_consume` |
| `TestCheckForUpdates::test_null_metadata_goes_to_failed_mods` | Line 438 — `if not info` branch |
| `TestCheckForUpdates::test_hitl_rejection_goes_to_failed_mods` | Lines 469–478 — HITL DENIED gate |
| `TestSyncEngineBatchError::test_value_error_in_fetch_counts_as_batch_failure` | Broad `except Exception` in `_consume` → metrics |
| `TestConsumeExceptionHandling::test_timeout_error_caught_at_batch_level` | `TimeoutError` caught at batch level |

### Adaptation notes (pre-Phase code state)

- `SyncConfig` and `SyncResult` are `@dataclass`, not Pydantic `BaseModel` — tests use `dataclasses.asdict` and `dataclasses.FrozenInstanceError` accordingly.
- `run()` uses `asyncio.gather(..., return_exceptions=True)` — not `TaskGroup`.
- `_consume` has broad `except Exception as exc` — `ValueError` in `_process_batch` is caught there, not propagated.
- `Decision` enum values: `APPROVED`, `DENIED`, `TIMEOUT` (not `REJECTED`).
