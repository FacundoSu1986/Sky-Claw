# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **`python-engineio` 4.13.1 → 4.13.3 (CVE-2026-48802, CVE-2026-48809) y
  `python-socketio` 5.16.1 → 5.16.3 (CVE-2026-48804)** — misma ola de avisos
  que `pip-audit --strict --skip-editable -r requirements.lock` empezó a marcar
  tras avanzar su base de advisories, poniendo rojo el gate "Security Scan"
  (pre-existente en `main`, ajeno al feature work). Ambas son transitivas vía
  `nicegui → python-socketio`; se suben floors directos en `pyproject.toml`
  (`python-engineio>=4.13.2`, `python-socketio>=5.16.2`, mismo patrón de pin
  transitivo que `starlette`/`python-multipart`) y se regeneran
  `requirements.lock` **y** `uv.lock`. Solo cambian esos dos pins (el grafo
  transitivo no varía), así que no requiere re-bundlear el exe.
- **Familia `langgraph`: `langgraph-checkpoint` 4.0.3 → 4.1.1
  (CVE-2026-48775) y `langgraph-sdk` 0.3.14 → 0.3.15 (CVE-2026-48776)** —
  divulgación coordinada que `pip-audit --strict` marcó en `requirements.lock`,
  poniendo rojo el gate "Security Scan". Se suben los floors en `pyproject.toml`
  (`langgraph-checkpoint>=4.1.1,<5`; `langgraph-sdk>=0.3.15`, transitiva vía
  `langgraph`) y se regeneran `requirements.lock` **y** `uv.lock`. Solo cambian
  esos dos pins (el grafo transitivo no varía), así que no requiere re-bundlear
  el exe.

### Fixed
- **VERSIONINFO del `.exe` ahora se embebe y se deriva solo** (`sky_claw.spec`).
  El spec pasaba un dict `version_info={...}` a `EXE(...)`, pero PyInstaller solo
  honra el kwarg `version=` (un `VSVersionInfo` o un path a archivo); el dict se
  descartaba en silencio, así que el binario se publicaba **sin recurso de
  versión** (Propiedades → Detalles vacío) y el "bump manual" de
  `(0, 2, 4, 0)` no tenía efecto. Ahora la tupla `(major, minor, patch, 0)` se
  deriva en build time de `importlib.metadata.version("sky-claw")` (el tag de
  `hatch-vcs`), con fallback a `sky_claw.__version__` y a `(0, 0, 0, 0)` si no
  parsea; los sufijos dev/dirty (`0.2.4.devN+g…`) se toleran vía regex. Se
  elimina el footgun del bump manual: `sky_claw/__init__.py:__version__` queda
  como **único punto manual** de versión.

## [0.2.4] - 2026-06-24

### Added
- **Hardening del lease en `SnapshotTransactionLock`** (`db/locks.py`) — defensa
  en profundidad para cerrar la ventana entre la pérdida real de un lease y su
  detección por el heartbeat (acotada hasta `TTL/renew_divisor`, ~200s con el
  default 600s/3). Nuevo `assert_owned(verify_db=True)`: API *opt-in* que los
  callers invocan justo antes de una mutación crítica para re-verificar
  exclusividad (fast-path por flag + chequeo fresco de propiedad contra la DB),
  achicando la ventana a ~0 (compara el token de adquisición `acquired_at`, no
  solo `agent_id`, para detectar readquisiciones de un mismo servicio). Nueva
  perilla `renew_divisor` (default 3.0, finito y `>= 2.0`; el intervalo tiene piso
  para no martillar la DB) para acortar el intervalo de renovación sin tocar el
  TTL; un lease perdido detectado por `assert_owned` toma el mismo camino
  sin-rollback que el del heartbeat. Ambos **aditivos y
  backward-compatible**; aún **no** cableados en call sites (se evita el
  cargo-cult: es una API opt-in que cada runner adopta solo donde haya una
  mutación cross-process real).

### Security
- **Redacción key-aware de secretos sin forma** (`logging_config.py`) — la capa
  `_SENSITIVE_KEY_RE` (que redacta el *valor* de un extra estructurado según el
  nombre de su clave, para secretos sin prefijo reconocible) cubría solo
  `aws_secret_access_key`. Se extiende a `client_secret` y
  `(?:bot|access|refresh)_token`, de modo que un `client_secret` en un log
  extra se redacta aunque el valor no tenga forma detectable por los patrones
  de texto — tanto en la forma estándar `extra={"client_secret": v}` (atributo
  top-level del `LogRecord`, el path común) como dentro de un dict anidado.
  Cada alternativa es un **nombre de clave completo** anclado
  con `\b`: se excluye `token` suelto a propósito para no clobberear la
  telemetría de token-budget (`token_count`/`max_tokens`/`prompt_tokens`), con
  test de regresión que lo fija.

## [0.2.3] - 2026-06-20

### Added
- **Chat GUI↔daemon (`/ws/ui`)** — la GUI ahora **conecta** al daemon y el chat
  responde con el LLM en vez de quedar en "⚠️ Daemon offline". Nuevo handler
  command-aware en `web/app.py` (`:8765`, junto a `/api/chat` y `/api/status`)
  que rutea `command/chat` → `LLMRouter.chat` y responde
  `{"type":"response",...}`, con auth `X-Auth-Token` (cierra con `4001` en
  rechazo) y el **test de round-trip GUI↔daemon que faltaba** (la causa de que
  el remap erróneo de #195 pasara verde). El remap `/ws/ui`→Operations-Hub se
  mantiene descartado (cuelga la UI). Alcance Q&A; el chat *agentic* (ejecutar
  herramientas desde el chat) queda para una feature futura con su propio diseño.

### Fixed
- **`langsmith` 0.8.3 → 0.8.18** (GHSA-f4xh-w4cj-qxq8). Dependencia transitiva
  (vía `langchain-core`); se fija un *floor* directo `>=0.8.18` en
  `pyproject.toml` (siguiendo el patrón ya usado para `starlette` y
  `python-multipart`) y se regeneran **ambos** lockfiles (`requirements.lock`
  y `uv.lock`), de modo que tanto la instalación vía pip como la vía uv
  documentada (`uv sync --frozen`/`--locked` en `setup_env.ps1` y
  `DEPLOYMENT.md`) quedan remediadas. Único hallazgo del `pip-audit --strict`
  tras avanzar su base de advisories; sin cambios de API. Al resincronizar
  `uv.lock` (stale desde #190) también se cierran en la vía uv los pines ya
  corregidos en `requirements.lock` (p. ej. `py7zr` 0.22.0 → 1.1.3).

## [0.2.2] - 2026-06-20

### Fixed
- **El runtime de `SupervisorAgent.start()` crasheaba en Windows localizado**
  (no-inglés, p. ej. es-ES) con `unable to open database file` en el DLQ —
  resuelve el *Known Issue* de 0.2.1. Dos causas en
  `security/file_permissions.py::_restrict_windows`:
  - El hardening pasaba **nombres en inglés** (`Users`,
    `BUILTIN\Administrators`, …) a `icacls /remove`; en Windows localizado no
    mapean a SID → exit `1332` → el fail-closed destruía el secreto aunque el
    `/grant` hubiera funcionado. Ahora se quitan por **SID well-known**
    (`*S-1-1-0`, `*S-1-5-32-545`, …), independiente del idioma del SO.
  - El grant owner-only en directorios no llevaba `(OI)(CI)`, así que los hijos
    (`~/.sky_claw/dlq/`) no heredaban acceso de escritura → el DLQ no podía
    crear/abrir su SQLite. Ahora los directorios se otorgan `(OI)(CI)(F)`
    (heredable); los archivos siguen con `(F)`.
  - La decisión de fail-closed ahora la dicta el **DACL efectivo**
    (`_verify_dacl`), no el exit code de icacls — un `/remove` que falla tras un
    `/grant` exitoso ya no destruye un artefacto correctamente endurecido. La
    garantía owner-only se mantiene (un DACL con ACE no-owner sigue fallando
    closed).
  - **Endurecimiento del review (P1):** la aceptación en el camino *degrade*
    (excepción de icacls) ahora exige el **SID/nombre calificado exacto** del
    owner con Full Control (`(OI)(CI)` en directorios), sin fallback por nombre
    pelado; y los ACE de logon-session sólo se toleran si son de la **sesión
    actual**. La garantía owner-only queda más estricta, nunca más laxa.
- **`py7zr` 0.22.0 → 1.1.3** (CVE-2026-23879). El bump *major* destapó un bug
  latente en `fomod/installer.py::_extract_7z`, que extraía target-por-target en
  un loop — en py7zr ≥ 1.0 eso re-lee el stream y lanza `CrcError`. Ahora extrae
  en una sola pasada (`extractall`) tras validar todos los nombres (anti
  zip-slip), con el test de extracción real multi-archivo que faltaba.

## [0.2.1] - 2026-06-17

### Fixed
- **El ejecutable empaquetado no arrancaba (crash al abrir)** — dos bugs de
  empaquetado:
  - Los assets de la GUI (`styles.css`, `assets/`) no se incluían en el bundle
    de PyInstaller y las rutas no eran *frozen-aware*, así que
    `add_static_files` recibía un directorio inexistente bajo `sys._MEIPASS` y
    el exe crasheaba antes de iniciar (`sky_claw.spec`, `sky_claw_gui.py`).
  - En el build `--windowed`, `sys.stdout`/`sys.stderr` son `None`; el banner de
    arranque de NiceGUI escribía sobre ellos y tiraba el proceso antes de
    bindear el puerto. Nuevo guard `_ensure_std_streams` en `__main__.py`.
- **El agente GUI no bootstrapeaba (`SupervisorAgent.__init__`)**:
  - La resolución de rutas de MO2 validaba contra el `PathValidator` de backups
    (solo `.skyclaw_backups`) en vez del sandbox de modding, rechazando toda
    ruta de MO2 (`RuntimeError` de modlist). Ahora `AppContext` inyecta el
    sandbox validator correcto vía DI (`supervisor.py`, `app_context.py`,
    `_bootloader.py`), sin debilitar el sandbox de rollback.
  - `XEditPipelineService` se construía sin el kwarg requerido `journal`.
  - Nueva cobertura de construcción del `SupervisorAgent` (`__init__` no tenía
    ningún test — el agujero por el que pasaron estos bugs hasta el runtime).
- **`build.bat`** apuntaba a `venv\` en vez del `.venv\` real del repo.

## [0.2.0] - 2026-06-16

### Added
- **OpenAI como proveedor LLM de primera clase** (#185): `OpenAIProvider`
  (OpenAI-compatible, `api.openai.com`), cableado en `create_provider`,
  `--provider`, el wizard GUI, el bridge web y el ops-hub.
- **Modelo LLM provider-scoped** (#186): campos `{provider}_model` en config;
  `app_context` resuelve el modelo del provider activo sin fallback global, así
  cambiar de provider nunca arrastra un modelo incompatible. El `llm_model`
  global legacy se migra al slot del provider activo al cargar.

### Fixed
- **P0 — Procesos externos huérfanos (auditoría de producción)**: los runners
  `bodyslide_runner.py`, `pandora_runner.py` y `wrye_bash_runner.py` capturaban
  `TimeoutError` y retornaban sin matar el proceso del SO, dejando BodySlide /
  Pandora / Wrye Bash vivos reteniendo handles sobre el VFS de MO2 y el
  directorio `Data` de Skyrim. Ahora cada runner hace `kill()` + reap en timeout
  y añade un handler de `asyncio.CancelledError` (mata y re-lanza) para que el
  apagado/cancelación no filtre binarios. Además, `__main__.py` traduce SIGTERM
  a `KeyboardInterrupt` (`_install_sigterm_handler`) para que un `kill <pid>` en
  Unix/WSL2 dispare la misma limpieza grácil que Ctrl+C. Cubierto por
  `test_subprocess_orphan_prevention.py` y `test_graceful_shutdown_signal.py`
  (TDD red→green).
- **P1 — Concurrencia y resiliencia (auditoría de producción)**:
  - **P1-1 `async_registry` race de escritura**: `AsyncModRegistry` comparte una
    única conexión `aiosqlite` entre todas las corrutinas mientras `SyncEngine`
    reparte writes en un `TaskGroup` (hasta 15 tareas). Como la transacción no
    es atómica entre `await`s, el `commit`/`rollback` de un escritor podía caer
    en mitad de la transacción de otro (commit parcial o descarte de filas no
    committeadas = pérdida silenciosa; WAL no protege de la pérdida lógica).
    `upsert_mod`, `set_vfs_status` y los tres writers `executemany` ahora están
    serializados con un `asyncio.Lock`. Test: `test_async_registry_write_serialization.py`.
  - **P1-2 deadlock latente en xEdit/Synthesis**: en timeout hacían
    `proc.kill()` y luego `await proc.communicate()` **sin timeout**; un proceso
    nieto que heredó el pipe lo colgaba para siempre. Ahora reapean con
    `wait_for(proc.wait(), 3.0)` acotado y añaden handler de `CancelledError`.
    Test: `test_runner_communicate_timeout.py`.
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

[Unreleased]: https://github.com/FacundoSu1986/Sky-Claw/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/FacundoSu1986/Sky-Claw/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/FacundoSu1986/Sky-Claw/releases/tag/v0.1.0
