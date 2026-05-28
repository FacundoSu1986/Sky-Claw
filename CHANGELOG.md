# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
