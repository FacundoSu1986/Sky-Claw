# AGENTS.md — guía para agentes en este repo

Fuente canónica de instrucciones para cualquier agente (Claude Code, Codex, Copilot…).
`CLAUDE.md` solo importa este archivo. El detalle de invariantes y patrones de código está
en [.github/coding_conventions.md](.github/coding_conventions.md).

## Stack real (verificado contra el código)

- **Python ≥ 3.11** (`pyproject.toml`; CI corre 3.11 y 3.12 en `windows-latest`).
- **GUI: NiceGUI** (web/escritorio) en `sky_claw/antigravity/gui/`.
- SQLite (WAL), multi-LLM (Anthropic / OpenAI / DeepSeek / Ollama), gateway de Telegram (Node).
- Dominio: gestión de mods de Skyrim SE/AE vía Mod Organizer 2 (LOOT, xEdit, DynDOLOD…).

## Convenciones

- **Tests y comentarios de código en español** (convención del repo).
- **TDD**: test que falla (rojo) → implementación → verde.
- Entorno: venv del repo vía `uv sync --extra dev`. Correr tests:
  - Windows: `.venv/Scripts/python -m pytest`
  - POSIX: `.venv/bin/python -m pytest`

  (`asyncio_mode=auto`: los tests `async def` no necesitan decorador.)
- Lint/format/types — el gate "Lint" de CI exige **ambos** comandos de ruff:
  `ruff check sky_claw/ tests/` **y** `ruff format --check sky_claw/ tests/`.
  `mypy sky_claw/` es **bloqueante en CI** (no es informativo).
- Una rama + un PR por cambio; no commitear directo a `main`.

## Mapa del repo

| Ruta | Qué es |
|------|--------|
| `sky_claw/antigravity/core/` | Núcleo: `database.py` (`DatabaseAgent`), `errors.py` (`AppNexusError`), `contracts.py` (Protocols) |
| `sky_claw/antigravity/gui/` | GUI NiceGUI (vistas, controllers, `models/app_state.py`) |
| `sky_claw/antigravity/web/` | App web / daemon |
| `sky_claw/antigravity/security/` | `path_validator.py` (`PathValidator` — sandboxing de rutas) |
| `sky_claw/antigravity/comms/` | Comunicaciones (Python) + gateway de Telegram (Node en `telegram_gateway_node/`) |
| `sky_claw/local/mo2/` | Integración con Mod Organizer 2 (perfiles, sandbox, modlist) |
| `sky_claw/local/tools/` | Tools del agente (`tool_result.py`, runners de LOOT/xEdit/etc.) |
| `sky_claw/config.py` | `SystemPaths` y configuración global |
| `sky_claw/app_context.py` | `AppContext.start_full()` — inicialización protegida con `asyncio.Lock` |
| `tests/conftest.py` | Fixtures compartidas (DB en memoria, LLM mockeado) |
| `.github/workflows/ci.yml` | CI de 5 gates (Lint / Mypy / Tests / Security / Build) |

## Contratos vigentes

**Resultado de tools.** Todo tool nuevo emite `success: bool` + `message: str` (canónico,
vacío en éxito) además de sus campos estructurados. `normalize_tool_result`
(`sky_claw/local/tools/tool_result.py`) es la única pieza que conoce las claves legacy
(`details`/`error`/`logs`/`stderr`/`errors`/`reason`); "error desconocido" solo puede
originarse en su fallback. Tests ancla: `tests/test_tool_result.py` (shapes legacy reales)
y `tests/test_tool_result_contract.py` (retorno de error por servicio). *Historia: cada
servicio reportaba errores bajo claves distintas y el summarizer adivinaba — parcheado dos
veces (#214, #216) antes del fix de raíz.*

**Capa del agente LLM**: lock-only, **sin HITL** (decisión documentada en #217).

## Pendientes conocidos

- **Smoke real de "Limpiar Archivos" (QuickAutoClean).** Los tests mockean el subproceso:
  validan los argumentos (`-quickautoclean -autoexit -autoload`, los mismos que usa PACT)
  pero no que SSEEdit limpie de verdad. Falta un smoke del Ritual en una instalación real
  con SSEEdit antes de confiar al 100%.
