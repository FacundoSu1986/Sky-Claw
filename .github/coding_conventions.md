# Convenciones de código — Sky-Claw

<!-- Punto de entrada para agentes: AGENTS.md (raíz del repo). Este archivo es el detalle
     de invariantes y patrones; los agentes modernos (Copilot incluido) leen AGENTS.md. -->

Stack: **Python 3.11+**, **NiceGUI** (GUI web/escritorio), SQLite, agentes LLM
multi-proveedor, Playwright, integración con MO2/Skyrim SE.

## 1. Jerarquía de prioridad

Si dos reglas colisionan, obedecé este orden:

| Prioridad | Dominio | Ejemplos clave |
|-----------|---------|----------------|
| **P0** | Seguridad Zero-Trust | secretos, SQL injection, prompt injection, TOCTOU, sandbox de rutas |
| **P1** | Invariantes Sky-Claw (§2) | su violación invalida el cambio |
| **P2** | SRE / Concurrencia | estabilidad de `asyncio`, event loop de NiceGUI, memory leaks |
| **P3** | Calidad / Testing | cobertura con mocks, inyección de dependencias, fixtures |
| **P4** | Lógica de dominio | modding de Skyrim, orquestación de agentes |

## 2. Invariantes

### 2.1 Concurrencia y UI (NiceGUI / asyncio)

- No bloquear el event loop: I/O bloqueante (subprocesos, disco, librerías de red
  sincrónicas) vía `asyncio.to_thread` o executors. **SQLite no entra acá**: la DB
  es `aiosqlite`, nativamente async — envolverla en `to_thread` mueve una corrutina
  a otro hilo y rompe el loop que la conexión tiene asociada.
- Prohibido `time.sleep()` en código async — usar `asyncio.sleep()`.
- Los eventos hacia la UI se emiten desde el loop: el `EventBus` de la GUI debe arrancar
  con `app.on_startup(event_bus.start)` — si `_loop is None`, los eventos se descartan en
  silencio (bug real, PR #201).

### 2.2 Base de datos (SQLite)

- La conexión es **`aiosqlite`**, la entrega
  `DatabaseLifecycleManager.get_connection()` (`core/db_lifecycle.py`). Código
  nuevo no llama `aiosqlite.connect()` / `sqlite3.connect()`: el conjunto de
  módulos que sí lo hacen (fallbacks pre-M-01 más tres exenciones deliberadas)
  está congelado en `tests/test_db_connection_invariant.py` — sumar uno rompe
  el test.
- **El singleton es por path resuelto _dentro de un manager_, no global.**
  `_connections` y `_write_locks` son campos de instancia, y `init_db()`
  construye un `DatabaseLifecycleManager` propio por cada `DatabaseAgent`: dos
  agentes sobre el mismo archivo hoy usan conexiones y locks **distintos**.
  Compartir serialización exige inyectar el *mismo* manager (la DI `lifecycle=`
  de M-01.1). No asumas exclusión mutua entre dos wrappers sin verificar que
  comparten manager.
- Dentro de un manager la conexión sí es compartida, así que **toda escritura
  pasa por `_write_transaction()`** (que delega en `lifecycle.transaction()` y
  toma el write lock por path). Escribir sobre la conexión de `_get_conn()`
  compila, pasa los tests y se saltea el lock — es el modo de falla de esta
  sección. `transaction()` ya hace commit y rollback; `BEGIN IMMEDIATE`
  explícito solo para batches largos (ver `init_db`).
- Por la misma razón, `last_insert_rowid()` no es confiable: releé el id por
  clave única (`SELECT id ... WHERE name = ?`), como hace `add_mod` (#220).
- `INSERT ... ON CONFLICT DO UPDATE`, nunca `INSERT OR REPLACE`: REPLACE
  borra+reinserta con id nuevo y con `foreign_keys=ON` rompe las FKs de `conflicts`.
- Solo consultas parametrizadas — **prohibido** f-strings o `.format()` en SQL.
- Pragmas al abrir (los pone el lifecycle): `journal_mode=WAL`, `foreign_keys=ON`,
  `busy_timeout=5000`, `synchronous=NORMAL`.

### 2.3 Agentes LLM

- Lógica de agentes en servicios inyectables, desacoplada de la UI.
- Todo output de LLM se valida con Pydantic (`model_validate_json`) — **prohibido**
  parsear texto libre con regex.
- Operaciones de archivo confinadas al sandbox: validar con `PathValidator.validate()`
  (`sky_claw/app/security/path_validator.py`) relativo a `SystemPaths`
  (`sky_claw/config.py`).
- Tools nuevos emiten `success: bool` + `message: str` (contrato completo en `AGENTS.md`).
- La capa del agente es lock-only, sin HITL (#217).

### 2.4 Testing y calidad

- Inyección de dependencias con `Protocol`s (`sky_claw/app/core/contracts.py`) —
  obligatorio para mockear I/O externa.
- Pytest exclusivamente; fixtures compartidas en `tests/conftest.py` (DB en memoria, LLM
  mockeado, `AsyncMock` para corrutinas); `asyncio_mode=auto`.
- Naming: archivos `test_<module>.py`, funciones `test_<method>_<scenario>_<expected>`.
- Tests y comentarios en español; TDD rojo → verde.
- Gate de CI: cobertura mínima 60% (`--cov-fail-under=60`).

### 2.5 Errores y logging

- Jerarquía tipada `AppNexusError` (`sky_claw/app/core/errors.py`). **Prohibido**
  `except Exception` desnudo; re-lanzar excepciones desconocidas tras loggear.
- `logging` exclusivamente, un logger por módulo (`logging.getLogger(__name__)`);
  prohibido `print()`.
- Niveles: DEBUG payloads/queries · INFO acciones y migraciones · WARNING rate limits y
  fallbacks · ERROR fallos de API y rollbacks · CRITICAL corrupción o estado irrecuperable.

## 3. Patrones prohibidos

> Si detectás alguno en código existente, reportalo como defecto.

- `time.sleep()` en código async o en el hilo del event loop.
- `except Exception` / `except BaseException` desnudo.
- `print()` para output — usar `logging`.
- f-strings o `.format()` en queries SQL — solo consultas parametrizadas.
- `sqlite3.connect()` o `aiosqlite.connect()` a mano — pedir la conexión al lifecycle.
- Claves API, rutas o umbrales hardcodeados — usar `config.py`, keyring o variables de entorno.
- Regex para parsear output de LLM — usar Pydantic.
- Paths hardcodeados (`/tmp/...`, `C:/...`) — usar `SystemPaths` o `tempfile`.
- Complejidad O(n²) en análisis de conflictos — usar sets/dicts para lookups.

## 4. Dominio Skyrim

- Limpiar extensiones `.esp`/`.esm`/`.esl` antes de comparar nombres de plugins.
- Load order: prioridad de masters `.esm` > `.esl` > `.esp`; validar dependencias de
  masters antes de procesar (los flags ESL reales se leen del header del plugin — ver el
  preflight y `PluginHeaderInspector`).
- Nexus API: exponential backoff con jitter ante `RateLimitError` (1 s inicial, máx 60 s,
  máx 5 reintentos).
- Playwright: headless por defecto; `await page.wait_for_selector()` antes de extraer
  datos; timeout de 30 s por página.
- LOOT: parsear la masterlist YAML y cachear por timestamp de modificación del archivo.

## 5. CI/CD (5 gates — `.github/workflows/ci.yml`)

| Gate | Herramienta | Criterio |
|------|-------------|----------|
| Lint | Ruff | `ruff check` **y** `ruff format --check` sin errores |
| Type Check | Mypy | **Bloqueante** (`mypy sky_claw/`) |
| Test | Pytest | `--cov-fail-under=60`; matrix Windows+Ubuntu × py3.11/3.12, pero Ubuntu corre con `continue-on-error` — **solo Windows bloquea** |
| Security | Bandit + pip-audit + npm audit | SAST sin high/critical; `pip-audit --strict` sobre `requirements.lock` (hashes enforced); `npm audit` del gateway de Telegram |
| Build | PyInstaller | `sky_claw.spec` (autoderiva el VERSIONINFO de la versión del paquete); depende de los gates anteriores |
