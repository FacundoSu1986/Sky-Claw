# Contribuir a Sky-Claw

> **Audiencia:** Desarrolladores humanos y agentes de IA que deseen modificar el código fuente, añadir funcionalidades o reportar defectos.
> **Estado:** guía vigente de contribución.
> **Fuentes canónicas:** `AGENTS.md`, `pyproject.toml` y `.github/workflows/ci.yml`.
> **Pre-requisito:** Leer [ARCHITECTURE.md](ARCHITECTURE.md) para comprender la topología del sistema.
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

---

## 1. Configuración del Entorno

Sky-Claw usa `uv` para la gestión de entornos virtuales y dependencias. Asegúrate de tener `uv` instalado en tu sistema.

### 1.1 Clonar y Sincronizar

```bash
git clone https://github.com/FacundoSu1986/sky-claw.git
cd sky-claw
uv sync --extra dev
```

Esto creará un entorno virtual en `.venv/` con todas las dependencias de desarrollo (pytest, ruff, mypy, etc.).

### 1.2 Activar el Entorno Virtual

En Windows (PowerShell):
```powershell
.venv\Scripts\activate
```

En POSIX (Bash/Zsh):
```bash
source .venv/bin/activate
```

*Nota: Los comandos subsiguientes asumen que el entorno virtual está activo, o puedes usar `uv run` directamente.*

---

## 2. Flujo de Trabajo de Desarrollo (TDD)

Sky-Claw adhiere estrictamente a **Test-Driven Development (TDD)**. No se aceptarán cambios sin sus correspondientes tests.

### 2.1 El Ciclo Rojo-Verde-Refactor

1.  **Rojo:** Escribe un test que falle (`AssertionError`) para la nueva funcionalidad o bugfix.
2.  **Verde:** Implementa el código mínimo necesario para que el test pase.
3.  **Refactor:** Mejora la estructura del código sin alterar su comportamiento (los tests deben seguir pasando).

### 2.2 Convenciones de Testing

- **Framework:** `pytest` exclusivamente.
- **Idioma:** Los tests y comentarios de código **deben estar en español**
  (convención del repositorio). Los docstrings siguen el idioma y terminología
  del módulo existente.
- **Naming:**
  - Archivos: `test_<modulo>.py`
  - Funciones: `test_<metodo>_<escenario>_<esperado>` (ej. `test_normalize_tool_result_success_false_retorna_error_desconocido`).
- **Asyncio:** El repositorio está configurado con `asyncio_mode=auto`. No es necesario decorar las funciones `async def` con `@pytest.mark.asyncio`.
- **Fixtures:** Usa fixtures compartidas desde `tests/conftest.py` (DB en memoria, LLM mockeado, `AsyncMock`). Inyecta dependencias mediante `Protocol`s para mockear I/O externa.
- **Cobertura:** El CI exige un mínimo de **60%** de cobertura global (`--cov-fail-under=60`).

### 2.3 Ejecutar Tests

En Windows:
```powershell
.venv\Scripts\python -m pytest
```

En POSIX:
```bash
.venv/bin/python -m pytest
```

Para correr un archivo o test específico:
```bash
pytest tests/test_tool_result.py -k "test_normalize"
```

---

## 3. Calidad de Código y Linting

El pipeline de CI tiene 5 gates bloqueantes. Antes de abrir un PR, verifica localmente:

### 3.1 Lint y Formato (Ruff)

Ambos comandos deben pasar sin errores:

```bash
ruff check sky_claw/ tests/
ruff format --check sky_claw/ tests/
```

Para formatear automáticamente:
```bash
ruff format sky_claw/ tests/
```

### 3.2 Tipado Estático (Mypy)

Mypy es **bloqueante** en CI. No es solo informativo.

```bash
mypy sky_claw/
```

### 3.3 Seguridad (Bandit & pip-audit)

El CI ejecuta `bandit`, `pip-audit --strict` y `npm audit`. Evita introducir dependencias con vulnerabilidades conocidas o patrones inseguros (ej. `eval()`, `subprocess` con `shell=True`).

---

## 4. Convenciones de Código

Consulta [.github/coding_conventions.md](.github/coding_conventions.md) para el detalle completo. Aquí un resumen de las reglas innegociables (P0/P1):

### 4.1 Concurrencia (NiceGUI / asyncio)
- **Prohibido** `time.sleep()` en código async. Usa `asyncio.sleep()`.
- No bloquear el event loop. I/O pesada debe correr en `asyncio.to_thread` o executors.
- Los eventos hacia la UI deben emitirse desde el loop.

### 4.2 Base de Datos (SQLite)
- Conexiones con `threading.local()`; nunca compartir instancias de `DatabaseAgent`.
- Solo consultas parametrizadas. **Prohibido** f-strings o `.format()` en SQL.
- Transacciones batch con `BEGIN IMMEDIATE`.

### 4.3 Agentes LLM y Contratos
- Todo output de LLM se valida con Pydantic (`model_validate_json`). **Prohibido** regex para parsear responses.
- Operaciones de archivo confinadas al sandbox: validar con `PathValidator.validate()`.
- Los Tools nuevos emiten `success: bool` + `message: str` (ver contrato en `AGENTS.md`).

### 4.4 Errores y Logging
- Usa la jerarquía `AppNexusError` (`sky_claw/app/core/errors.py`).
- **Prohibido** `except Exception` desnudo; re-lanzar excepciones desconocidas tras loggear.
- Usa `logging.getLogger(__name__)`. **Prohibido** `print()`.

---

## 5. Pull Requests y Versionado

### 5.1 Estrategia de Ramas

- Usa una rama descriptiva por cada cambio (ej. `feat/nuevo-tool-runner`, `fix/db-connection-leak`).
- **Prohibido** commitear directamente a `main`.
- Una rama + un PR por cambio atómico.

### 5.2 Creación del PR

1.  Asegúrate de que todos los tests pasan localmente: `pytest`.
2.  Ejecuta los linters: `ruff check .` y `mypy sky_claw/`.
3.  Pushea tu rama y abre un PR en GitHub.
4.  El título del PR debe ser claro y conciso (ej. "feat: Añadir soporte para BodySlide Batch Build").
5.  En la descripción del PR, vincula el issue o ticket correspondiente (ej. "Cierra #123").
6.  **Tareas del Backlog:** Si tu PR cierra una tarea de `TECHNICAL_REVIEW_TASKS.md` (ej. T-XX), **debes** actualizar `docs/pending_ooda_status.md` en el mismo PR. El título declarando "cerrado" no es suficiente; verifica el árbol completo de callers.

### 5.3 Proceso de Revisión

- Los PRs requieren revisión antes de merge.
- El CI debe pasar (5 gates: Lint, Mypy, Tests, Security, Build).
- Usa `/review uncommitted` o `/review branch` si utilizas el agente de code review local.

---

## 6. Arquitectura para Nuevos Desarrolladores

Si buscas extender el sistema (ej. añadir soporte para una nueva herramienta de modding), no empieces escribiendo código a ciegas.

1.  Lee `sky_claw/local/AGENTS.md` para entender el **Pipeline de Modding** y las reglas inmutables de las herramientas.
2.  Lee [docs/agents/tool_creation.md](docs/agents/tool_creation.md) para un tutorial paso a paso sobre cómo registrar un nuevo Tool Runner respetando los contratos.
3.  Familiarízate con el sistema de validación de contratos en [docs/agents/schema_validation.md](docs/agents/schema_validation.md).
4.  Consulta [docs/documentation/standards.md](docs/documentation/standards.md)
    antes de añadir o cambiar documentación.

---

## 7. Código de Conducta Implícito

Sky-Claw es un proyecto mantenido por una comunidad pequeña. Sé respetuoso, conciso y orientado a la solución en issues y PRs. Reporta vulnerabilidades de seguridad de forma privada vía GitHub Security Advisories (ver [SECURITY.md](SECURITY.md)).
