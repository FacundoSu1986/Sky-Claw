# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Audit follow-up (#152, #153, #154, #155)** — cuatro hallazgos de la
  auditoría multidisciplinaria, agrupados en un único bundle TDD:
  - **#153 S-3 — `config.py` nested merge**: `Config._load_from_file`
    extraía `[telegram]/[nexus]/[paths]` a claves flat y luego hacía
    `_data.update(file_data)` con el dict crudo, re-inyectando las
    secciones como dicts paralelos. Ahora se hace `pop()` selectivo
    antes del `update()` para que sobreviva sólo la forma canónica
    flat y la precedencia top-level > nested quede explícita.
  - **#154 PM-2 — scraper doc-drift**: docstrings de
    `scraper_agent.query_nexus` y `scraper/nexus.py` prometían un
    fallback Playwright que en realidad es un stub permanentemente
    deshabilitado por compliance con el ToS de Nexus Mods.
    Reescritos para reflejar la realidad (sólo API oficial).
  - **#155 L-1 — `first_run.py` path validation**: el wizard
    guardaba `mo2_root` / `skyrim_path` sin verificar existencia.
    Nuevo helper `_validate_path` (pure, unit-tested) + wrapper
    `_prompt_for_validated_path` que re-pide al usuario; opción de
    confirmar continuar con una ruta inexistente para instalaciones
    nuevas donde la carpeta se crea después.
  - **#152 A-1 — `executor.py` async resolve**: las dos llamadas a
    `pathlib.Path(...).resolve(strict=False)` del path-jail en
    `ManagedToolExecutor.execute()` ahora pasan por
    `asyncio.to_thread` via el nuevo helper estático
    `_resolve_strict_false`. Reduce el bloqueo del event loop en
    mounts SMB/NFS lentos; test de regresión confirma que la
    rejection de traversals fuera del `modding_root` sigue intacta.

### Changed
- **P1.2 — DLQ obligatoria en producción**: `CoreEventBus.__init__` ahora
  acepta `require_dlq: bool = False` (default mantiene la compatibilidad).
  La factory de producción `create_bus_with_dlq()` lo pasa a `True`, así
  cualquier futuro override que invalida la DLQ (o un constructor manual
  sin DLQ en código de producción) aborta con `ValueError` al construirse
  en lugar de degradarse silenciosamente al modo "drop event on
  backpressure". Tests y dev shells siguen pasando `require_dlq=False`.

### Fixed
- **P1.5 R-06 — Chat preview clear-before-send with rollback**:
  The `_handle_send` closure inside `create_chat_preview` now clears the
  input immediately for a snappy UX. On send failure the original text
  (whitespace preserved) is restored and `ui.notify` surfaces the error.
  Rollback covers both sync exceptions and async failures — when
  `on_send_message` returns an awaitable (the real GUI wires
  `lambda msg: asyncio.create_task(controller.handle_send_message(msg))`),
  a `done_callback` triggers the same rollback path. Logic extracted into
  module-level `_try_send_with_rollback` + `_do_rollback`, unit-tested
  without a NiceGUI runtime (9 cases: sync/async paths, whitespace
  preservation, cancellation handling, best-effort restore/notify).
- **P1 — Reliability bundle (Kimi12 follow-ups)**:
  - **R-05** `SyncEngine._safe_fetch_info` now declares
    `result: dict[str, Any] | None = None` and falls through to an explicit
    `return None`, honoring the declared signature even if `AsyncRetrying`
    ever yields zero attempts (no more silent `UnboundLocalError` risk).
  - **§3.1** `SupervisorAgent` interface TaskGroup split: recoverable
    network errors (`ConnectionError`, `TimeoutError`, `OSError`) are
    logged WARNING and absorbed; everything else is logged CRITICAL and
    re-raised so programming bugs (AttributeError, ValueError, …) stop
    being silently swallowed.
  - **R-07** `SupervisorStateGraph.execute` now auto-purges stale
    `_thread_timestamps` entries: every `_cleanup_interval` (default 100)
    executions it calls `cleanup_old_threads(_cleanup_max_age_seconds)`
    (default 3600 s). Set `_cleanup_interval = 0` to opt out.
  - **R-03** `IdempotencyGuard` keys now carry a TTL
    (`key_ttl_seconds` default 3600 s). A crashed task that never
    `release()`s no longer blocks its key forever — the next acquire
    after the TTL reclaims the slot. Set to `0` for legacy eternal-lock.
  - **§3.2** CLI and GUI chat dispatch are bounded by
    `asyncio.wait_for`: 300 s for `cli_mode._run_cli` / `_run_oneshot`,
    30 s for the new `_dispatch_chat_to_router` helper in
    `gui/_bootloader.py`. A hung LLM provider now surfaces a clean error
    instead of freezing the loop.

### Added
- **P0.5 — Supply-chain reproducibility**:
  - `py7zr>=0.21,<1` declared in `[project.dependencies]` (was a PyInstaller
    hidden import in `sky_claw.spec:39` but missing from the manifest, breaking
    `pip install` reproducibility on fresh envs without 7-Zip support).
  - `requirements.lock` regenerated with `--generate-hashes` (2724 SHA-256
    hashes pinned for integrity verification at install time).
  - `package-lock.json` removed from `.gitignore` and committed for the
    Telegram Node gateway. Builds now reproducible across CI runs.
  - CI Security gate hardened: `pip-audit --strict` (was permissive),
    `npm ci` + `npm audit --audit-level=high` for the Telegram gateway.

### Changed
- **P0.4 — Quality gates**: coverage gate raised from 55 % → 60 % in CI
  (`--cov-fail-under=60`). Actual coverage at gate change: ~65 %. Documentation
  updated in `tests/conftest.py` and `.github/coding_conventions.md`.

## [0.1.0] - 2026-05-11

### Added
- Prometheus observability layer: `Counter` (`sky_claw_sync_attempts_total{status}`),
  `Histogram` (`sky_claw_sync_duration_seconds`), `Gauge` (`sky_claw_queue_depth`,
  `sky_claw_circuit_breaker_state{breaker_name}`). HTTP `/metrics` endpoint on
  `127.0.0.1:9100` with `X-Auth-Token` auth and dedicated `AuthTokenManager` instance
  with rotation.
- Centralized test fixtures in `tests/conftest.py`: `async_registry` (M-01 compliant
  lifecycle), `mock_network_gateway` (async context-manager stub), `correlation_id`
  (ContextVar reset on teardown).
- Cross-platform CI matrix: `ubuntu-latest` + Python 3.12 added to `test` gate;
  Python 3.12 added to `lint` and `typecheck` gates. Total: 10 runs/push (was 5).
  `fail-fast: false` maximises diagnostic signal.
- Dynamic SemVer via `hatch-vcs`: version derived from annotated git tags.
  `release.yml` skeleton for automated GitHub Releases on `v*` push.
- Coverage gate raised from 49 % → 55 % (actual 63.86 %). Policy: +5 pp/sprint
  until 80 % minimum.

### Security
- Harden SQLite pool lifecycle, redaction depth, WS close code and ScraperAgent
  gateway contract ([#120](https://github.com/FacundoSu1986/Sky-Claw/pull/120)).
- Harden PR 118 follow-up gaps ([#119](https://github.com/FacundoSu1986/Sky-Claw/pull/119)).
- Address WebSocket and egress review follow-ups
  ([#117](https://github.com/FacundoSu1986/Sky-Claw/pull/117)).
- Externalize context quarantine and redact modern secrets.
- Harden WebSocket auth and outbound egress.

[Unreleased]: https://github.com/FacundoSu1986/Sky-Claw/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/FacundoSu1986/Sky-Claw/releases/tag/v0.1.0
