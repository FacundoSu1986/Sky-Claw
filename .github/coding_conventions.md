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

- No bloquear el event loop: I/O pesada (subprocesos, disco, red, SQLite) vía
  `asyncio.to_thread` o executors.
- Prohibido `time.sleep()` en código async — usar `asyncio.sleep()`.
- Los eventos hacia la UI se emiten desde el loop: el `EventBus` de la GUI debe arrancar
  con `app.on_startup(event_bus.start)` — si `_loop is None`, los eventos se descartan en
  silencio (bug real, PR #201).

### 2.2 Base de datos (SQLite)

- Conexiones con `threading.local()`; nunca compartir instancias de `DatabaseAgent`.
- Transacciones batch con `BEGIN IMMEDIATE`; rollback ante excepciones.
- Solo consultas parametrizadas — **prohibido** f-strings o `.format()` en SQL.
- Al inicio: `PRAGMA journal_mode=WAL;` y `PRAGMA foreign_keys=ON;`.

### 2.3 Agentes LLM

- Lógica de agentes en servicios inyectables, desacoplada de la UI.
- Todo output de LLM se valida con Pydantic (`model_validate_json`) — **prohibido**
  parsear texto libre con regex.
- Operaciones de archivo confinadas al sandbox: validar con `PathValidator.validate()`
  (`sky_claw/antigravity/security/path_validator.py`) relativo a `SystemPaths`
  (`sky_claw/config.py`).
- Tools nuevos emiten `success: bool` + `message: str` (contrato completo en `AGENTS.md`).
- La capa del agente es lock-only, sin HITL (#217).

### 2.4 Testing y calidad

- Inyección de dependencias con `Protocol`s (`sky_claw/antigravity/core/contracts.py`) —
  obligatorio para mockear I/O externa.
- Pytest exclusivamente; fixtures compartidas en `tests/conftest.py` (DB en memoria, LLM
  mockeado, `AsyncMock` para corrutinas); `asyncio_mode=auto`.
- Naming: archivos `test_<module>.py`, funciones `test_<method>_<scenario>_<expected>`.
- Tests y comentarios en español; TDD rojo → verde.
- Gate de CI: cobertura mínima 60% (`--cov-fail-under=60`).

### 2.5 Errores y logging

- Jerarquía tipada `AppNexusError` (`sky_claw/antigravity/core/errors.py`). **Prohibido**
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
- Estado global para conexiones DB — usar `threading.local()`.
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
| Test | Pytest | `--cov-fail-under=60`; matrix Windows py3.11/3.12 |
| Security | Bandit + pip-audit + npm audit | SAST sin high/critical; `pip-audit --strict` sobre `requirements.lock` (hashes enforced); `npm audit` del gateway de Telegram |
| Build | PyInstaller | `sky_claw.spec` (autoderiva el VERSIONINFO de la versión del paquete); depende de los gates anteriores |
