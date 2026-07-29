# AGENTS.md — guía para agentes en este repo

Fuente canónica de instrucciones para cualquier agente (Claude Code, Codex, Copilot…).
`CLAUDE.md` solo importa este archivo. El detalle de invariantes y patrones de código está
en [.github/coding_conventions.md](.github/coding_conventions.md).

Para descubrir documentación por audiencia y conocer qué fuente domina ante
drift, leer [docs/README.md](docs/README.md) y
[docs/documentation/source_of_truth.md](docs/documentation/source_of_truth.md).
Los `AGENTS.md` de subárbol son punteros de alcance y no reemplazan esta guía.

## Stack real (verificado contra el código)

- **Python ≥ 3.11** (`pyproject.toml`; CI corre 3.11 y 3.12 en `windows-latest`).
- **GUI: NiceGUI** (web/escritorio) en `sky_claw/app/gui/`.
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
  `mypy sky_claw/` corre bloqueante **pero no cubre todo**: `pyproject.toml`
  tiene `ignore_errors = true` para ~30 módulos, incluidos `sky_claw.app.gui.*`,
  `sky_claw.app.web.*` y `sky_claw.local.tools.*`. Lo mismo `BLE001` de ruff,
  exento en todo `sky_claw/app/**`. Antes de confiar en que un gate te cubre,
  verificá que tu archivo no esté en la lista de exentos — si lo está, ese
  código sale a producción sin type-check.
- Una rama + un PR por cambio; no commitear directo a `main`. El revisor
  automático (`qodo-merge-adversarial.yml`) corre **solo** en `pull_request`:
  un push directo saltea el único control que empíricamente ataja defectos acá.
- **Al cerrar una tarea del backlog** (`TECHNICAL_REVIEW_TASKS.md`, T-XX):
  actualizar `docs/pending_ooda_status.md` en el mismo PR (o dejar constancia
  explícita si el cierre es parcial/cubre solo un runner). El título del PR
  declarando "cerrado" **no alcanza**.

## La regla que más se viola: arreglar un hermano y no al otro

Es **la** clase de defecto dominante del repo: 13 de 21 follow-ups auditados son
un fix que aterrizó en un camino y dejó intacto a su gemelo. No es descuido de
gente distraída — pasa leyendo este archivo, y el que lo ataja es siempre un bot.

Aplica **a todo cambio**, no solo al cierre de una T-XX.

Las dos formas concretas que toma acá:

1. **Dos superficies, un recurso.** La misma operación mutante se alcanza desde
   la GUI (`SupervisorAgent` → `tool_dispatcher`) *y* desde el agente LLM
   (`AsyncToolRegistry` → `LLMRouter` → Telegram / `/api/chat`). 9 de los 13
   episodios son exactamente esto: lock, journal, preflight o sandbox cableados
   en un path y ausentes en el otro (#166→#167, #171→#172, #213→#215→#217,
   #243→#247).
2. **Hermano en el mismo archivo.** #373: `run_ritual` recibió el scoping del
   dueño de la aprobación HITL y `run_ritual_install` —90 líneas más abajo,
   misma estructura, y la aprobación *más* sensible porque es egress de red—
   quedó sin él.

**Enunciala como propiedad del mecanismo, no como recordatorio de proceso.** Es
la diferencia medible entre los episodios que fallaron y los que salieron bien de
entrada: *"el lock cross-process solo protege si TODOS los mutadores participan"*
(#316, #324) cubrió ambos paths sin que lo pidiera un revisor. *"verificar el
árbol de callers"* no.

**Y anclala con un test que enumere, no que muestree.** Un caso escrito a mano
para el hermano que te faltó no ataja al tercero. El repo ya tiene el
instrumento y funciona:

- `tests/test_ritual_dispatch.py` → `assert RITUAL_TOOL_MAP == {...}` (igualdad
  literal del dict: agregar un ritual sin cablearlo rompe el test).
- `tests/test_hitl_client_scoping.py` → la familia de lanzadores se detecta por
  introspección y se congela; un lanzador nuevo rompe el ancla hasta que se le
  escribe su receta, y la receta lo mete en los tests de comportamiento.
- `tests/test_db_connection_invariant.py` → el conjunto de módulos que abren
  conexiones SQLite por su cuenta se detecta por AST y se congela.

Escribir el racional de por qué excluís una rama **no cuenta como verificarla**:
en #318 y #373 el autor enumeró, escribió el párrafo justificando el recorte, y
el revisor lo revirtió igual.

> Contexto de por qué esta sección existe y es tan explícita: ~94% de los ítems
> normativos de este corpus no tienen gate que los haga fallar. Las reglas que el
> repo sí cumple (ruff, mypy, contrato `success`/`message`) comparten que nombran
> un comando o rompen un test. Si agregás una regla acá, traé con qué se verifica
> — o va a envejecer como las demás.

## Mapa del repo

| Ruta | Qué es |
|------|--------|
| `sky_claw/app/core/` | Núcleo: `database.py` (`DatabaseAgent`), `errors.py` (`AppNexusError`), `contracts.py` (Protocols) |
| `sky_claw/app/gui/` | GUI NiceGUI (vistas, controllers, `models/app_state.py`) |
| `sky_claw/app/web/` | App web / daemon |
| `sky_claw/app/security/` | `path_validator.py` (`PathValidator` — sandboxing de rutas) |
| `sky_claw/app/comms/` | Comunicaciones (Python) + gateway de Telegram (Node en `telegram_gateway_node/`) |
| `sky_claw/local/mo2/` | Integración con Mod Organizer 2 (perfiles, sandbox, modlist) |
| `sky_claw/local/tools/` | Tools del agente (`tool_result.py`, runners de LOOT/xEdit/etc.) |
| `sky_claw/local/AGENTS.md` | **SOP del pipeline de modding de Skyrim** (orden de stages, reglas por tool, failure modes) — leer antes de tocar `local/tools/`, `local/xedit/` u `orchestrator/tool_strategies/` |
| `sky_claw/config.py` | `SystemPaths` y configuración global |
| `sky_claw/app_context.py` | `AppContext.start_full()` — inicialización protegida con `asyncio.Lock` |
| `tests/conftest.py` | Fixtures compartidas (DB en memoria, LLM mockeado) |
| `.github/workflows/ci.yml` | CI de 5 gates (Lint / Mypy / Tests / Security / Build) |
| `docs/README.md` | Portal documental para usuarios, operadores, desarrolladores y agentes |

## Contratos vigentes

**Resultado de tools.** Todo tool nuevo emite `success: bool` + `message: str` (canónico,
vacío en éxito) además de sus campos estructurados. `normalize_tool_result`
(`sky_claw/local/tools/tool_result.py`) es la única pieza que conoce las claves legacy
(`details`/`error`/`logs`/`stderr`/`errors`/`reason`); "error desconocido" solo puede
originarse en su fallback. Tests ancla: `tests/test_tool_result.py` (shapes legacy reales)
y `tests/test_tool_result_contract.py` (retorno de error por servicio). *Historia: cada
servicio reportaba errores bajo claves distintas y el summarizer adivinaba — parcheado dos
veces (#214, #216) antes del fix de raíz.*

**Capa del agente LLM**: política base lock-only, **sin middleware HITL
general** (decisión documentada en #217). Los handlers que reciben un
`HITLGuard` explícito, como `download_mod`, conservan su aprobación específica.

## Pendientes conocidos

Ver [`docs/pending_ooda_status.md`](docs/pending_ooda_status.md) para el inventario
completo verificado contra el código (reemplaza mantener la lista acá, que se
desactualiza igual que cualquier otro doc estático). Un ítem persiste acá por ser
transversal a toda tarea que toque `local/tools/xedit_service.py`:

- **Smoke real de "Limpiar Archivos" (QuickAutoClean).** Los tests mockean el subproceso:
  validan los argumentos (`-quickautoclean -autoexit -autoload`, los mismos que usa PACT)
  pero no que SSEEdit limpie de verdad. Falta un smoke del Ritual en una instalación real
  con SSEEdit antes de confiar al 100%.
