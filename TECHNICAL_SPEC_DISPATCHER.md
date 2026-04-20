# TECHNICAL_SPEC_DISPATCHER — Strangler Fig sobre `SupervisorAgent.dispatch_tool`

> **Estado:** IMPLEMENTADO. Materializado en la raíz del repo como cumplimiento del DoD §15 de la Spec aprobada (V5.5).
> Este documento es la versión canónica del Spec; registra la decisión arquitectónica y el mapeo post-refactor para futura referencia.

---

## 1. Context

`sky_claw/orchestrator/supervisor.py` era una **God Class de 669 líneas** (auditoría arquitectónica V5.5). El cuello de botella concreto era `dispatch_tool()` (líneas 233–347 pre-refactor), un `match/case` *hardcodeado* con 9 branches + fallback que ruteaba herramientas a 4 services, 2 métodos del propio supervisor, 2 agentes, y 1 método interno. Cada branch repetía tres responsabilidades cruzadas — validación Pydantic in-line, gates HITL embebidos (sólo en `execute_loot_sorting`), y wrapping `try/except` + `isinstance(result, dict)` (synthesis y xEdit, copia idéntica).

**Outcome entregado:** Strategy + Registry vía Strangler Fig en una sola rama de 6 commits reviewables. Cada herramienta quedó extraída como una `ToolStrategy` con sus dependencias inyectadas explícitamente. `dispatch_tool` colapsa a un *delegator* de una línea preservando contrato público (firma, error codes, comportamiento byte-idéntico).

**Existencia de `AsyncToolRegistry`:** [sky_claw/agent/tools/__init__.py:60](sky_claw/agent/tools/__init__.py) sirve **otra capa** (LLM-facing, devuelve `str` JSON, integra con `LLMRouter`). **No se modificó** — ver §3.

---

## 2. Mapping (branch → Strategy class → dependencias inyectadas)

| # | `tool_name` | Strategy | Dependencias en `__init__` | Middleware registrado |
|---|---|---|---|---|
| 1 | `query_mod_metadata` | `QueryModMetadataStrategy` | `scraper: ScraperAgent` | — |
| 2 | `execute_loot_sorting` | `ExecuteLootSortingStrategy` | `tools: ModdingToolsAgent`, `interface: InterfaceAgent` | — (HITL inline, ver §7) |
| 3 | `execute_synthesis_pipeline` | `ExecuteSynthesisPipelineStrategy` | `service: SynthesisPipelineService` | `ErrorWrappingMiddleware("SynthesisPipelineExecutionFailed")` + `DictResultGuardMiddleware("InvalidSynthesisPipelineResult")` |
| 4 | `resolve_conflict_with_patch` | `ResolveConflictWithPatchStrategy` | `service: XEditPipelineService` | `ErrorWrappingMiddleware("XEditPatchExecutionFailed")` + `DictResultGuardMiddleware("InvalidXEditPatchResult")` |
| 5 | `generate_lods` | `GenerateLodsStrategy` | `service: DynDOLODPipelineService` | — |
| 6 | `scan_asset_conflicts` | `ScanAssetConflictsStrategy` | `scan_callable: Callable[[], list]` | — |
| 7 | `scan_asset_conflicts_json` | `ScanAssetConflictsJsonStrategy` | `scan_json_callable: Callable[[], str]` | — |
| 8 | `generate_bashed_patch` | `GenerateBashedPatchStrategy` | `wrye_bash_pipeline: Callable[..., Awaitable[dict]]` | — |
| 9 | `validate_plugin_limit` | `ValidatePluginLimitStrategy` | `plugin_limit_guard: Callable[[str], Awaitable[dict]]`, `default_profile_getter: Callable[[], str]` | — |
| — | `tool_name` desconocido | fallback en el dispatcher | — | `{"status":"error","reason":"ToolNotFound"}` verbatim |

**Caller preservado:** LangGraph callback en `state_graph.py:969` invoca `await self._supervisor.dispatch_tool(tool_name, payload)` — firma y shape de retorno inalterados.

---

## 3. Decisión arquitectónica: módulo NUEVO, no extender `AsyncToolRegistry`

| Concern | `AsyncToolRegistry` (existente) | `OrchestrationToolDispatcher` (nuevo) |
|---|---|---|
| Caller | `LLMRouter` (loop de tool-use del LLM) | `_on_dispatching` callback de LangGraph |
| Return type | `str` (JSON string para Anthropic) | `dict[str, Any]` (interno) |
| Schema surface | `tool_schemas()` para Anthropic | Pydantic models internos |
| Error contract | Levanta `KeyError`; el caller atrapa | Devuelve `{"status":"error","reason":...}` |
| Auth/policy | HITL embebido por handler | HITL como decisión explícita (ver §7) |
| Co-located tools | CRUD del mod-store, FOMOD, BodySlide (~20 tools) | Pipelines críticos (synthesis, xEdit, DynDOLOD, WB) |

Dos registries, un seam claro cada uno.

---

## 4. Estructura de módulos (implementada)

```
sky_claw/orchestrator/
├── supervisor.py                # dispatch_tool = one-line delegator
├── tool_dispatcher.py           # OrchestrationToolDispatcher + build_orchestration_dispatcher()
└── tool_strategies/
    ├── __init__.py
    ├── base.py                  # Protocol ToolStrategy + ToolMiddleware + ToolNotFoundError + DuplicateToolError
    ├── middleware.py            # ErrorWrappingMiddleware + DictResultGuardMiddleware
    ├── query_mod_metadata.py
    ├── execute_loot_sorting.py
    ├── execute_synthesis.py
    ├── resolve_conflict_patch.py
    ├── generate_lods.py
    ├── scan_asset_conflicts.py  # ScanAssetConflictsStrategy + ScanAssetConflictsJsonStrategy
    ├── generate_bashed_patch.py
    └── validate_plugin_limit.py
```

`AsyncToolRegistry` intacto (verificable con `git log origin/main..HEAD -- sky_claw/agent/tools/`).

---

## 5. Interfaces (implementadas en `tool_strategies/base.py`)

```python
@runtime_checkable
class ToolStrategy(Protocol):
    name: str
    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]: ...

NextCall = Callable[[], Awaitable[dict[str, Any]]]

@runtime_checkable
class ToolMiddleware(Protocol):
    async def __call__(
        self,
        strategy: ToolStrategy,
        payload_dict: dict[str, Any],
        next_call: NextCall,
    ) -> dict[str, Any]: ...

class ToolNotFoundError(KeyError): ...
class DuplicateToolError(ValueError): ...
```

`OrchestrationToolDispatcher` expone:
- `register(strategy, *, middleware=None)` — levanta `DuplicateToolError` si `strategy.name` ya existe.
- `async dispatch(tool_name, payload_dict)` — busca, aplica la cadena LIFO de middleware alrededor de `strategy.execute`, retorna dict. Unknown tool → dict legacy verbatim.
- `registered_tools()` — snapshot para introspección / tests.

---

## 6. Inyección de dependencias — constructor injection per-strategy

Sin `SupervisorContext` god-bag, sin factory functions globales. Cada strategy declara exactamente lo que necesita. Recursos cross-cutting (HITL `interface`, telemetría) llegan inyectados como colaboradores, no vía singleton.

Lazy patterns preservados (`_asset_detector`, `_wrye_bash_runner`): las strategies que dependen de estos reciben **callables** ligados al supervisor (lambdas que re-resuelven el atributo en cada llamada), no snapshots — preserva la semántica lazy intacta y permite a los tests monkey-patchear los métodos después de construir el dispatcher.

---

## 7. Cross-cutting concerns — decisiones tomadas

**Error wrapping → middleware**, como previsto: `ErrorWrappingMiddleware(reason_code)` + `DictResultGuardMiddleware(reason_code)` matan ~50 líneas de try/except duplicado en synthesis/xEdit. Atrapan `Exception` (no `BaseException`), preservando `KeyboardInterrupt`/`SystemExit`/`asyncio.CancelledError`. Validado con tests unitarios.

**HITL → NO se extrajo a middleware (YAGNI, descoped durante implementación).** Análisis: sólo UN use-site existe (`execute_loot_sorting`), y Pydantic validation **debe correr antes** del prompt HITL (para que el context dict mostrado en Telegram contenga el profile ya validado). Envolver HITL en middleware invertía ese orden o forzaba a la strategy a exponer su validation method al middleware. Re-evaluar cuando aparezca un segundo use-site. Documentado en el header de `middleware.py`.

**Pydantic → dentro de la strategy.** Cada schema es per-tool por definición (`ScrapingQuery`, `LootExecutionParams`, `ConflictReport`). Dispatcher queda schema-agnostic.

---

## 8. Registro (implementación en `build_orchestration_dispatcher(supervisor)`)

Construcción explícita, sin decoradores, sin auto-discovery. Replica el precedente de `AsyncToolRegistry._register_builtins`. Una sola pasada de lectura audita las 9 herramientas con sus deps y middleware. Cero side effects de import-time.

```python
def build_orchestration_dispatcher(supervisor: SupervisorAgent) -> OrchestrationToolDispatcher:
    d = OrchestrationToolDispatcher()
    d.register(QueryModMetadataStrategy(scraper=supervisor.scraper))
    d.register(ExecuteLootSortingStrategy(tools=supervisor.tools, interface=supervisor.interface))
    d.register(
        ExecuteSynthesisPipelineStrategy(service=supervisor._synthesis_service),
        middleware=[
            ErrorWrappingMiddleware("SynthesisPipelineExecutionFailed"),
            DictResultGuardMiddleware("InvalidSynthesisPipelineResult"),
        ],
    )
    d.register(
        ResolveConflictWithPatchStrategy(service=supervisor._xedit_service),
        middleware=[
            ErrorWrappingMiddleware("XEditPatchExecutionFailed"),
            DictResultGuardMiddleware("InvalidXEditPatchResult"),
        ],
    )
    d.register(GenerateLodsStrategy(service=supervisor._dyndolod_service))
    d.register(ScanAssetConflictsStrategy(scan_callable=lambda: supervisor.scan_asset_conflicts()))
    d.register(ScanAssetConflictsJsonStrategy(scan_json_callable=lambda: supervisor.scan_asset_conflicts_json()))
    d.register(GenerateBashedPatchStrategy(wrye_bash_pipeline=lambda **kw: supervisor.execute_wrye_bash_pipeline(**kw)))
    d.register(
        ValidatePluginLimitStrategy(
            plugin_limit_guard=lambda p: supervisor._run_plugin_limit_guard(p),
            default_profile_getter=lambda: supervisor.profile_name,
        ),
    )
    return d
```

`SupervisorAgent.__init__` agrega una línea al final:
```python
self._tool_dispatcher = build_orchestration_dispatcher(self)
```

`dispatch_tool` final:
```python
async def dispatch_tool(self, tool_name, payload_dict):
    return await self._tool_dispatcher.dispatch(tool_name, payload_dict)
```

---

## 9. Métodos de supervisor que permanecen (scope discipline)

- `scan_asset_conflicts` / `scan_asset_conflicts_json`: permanecen como métodos del supervisor; las strategies son adapters thin que llaman vía callable.
- `execute_wrye_bash_pipeline` (~100 líneas con M-04 guard + runner init): permanece; `GenerateBashedPatchStrategy` es un adapter.
- `_run_plugin_limit_guard`: permanece; `ValidatePluginLimitStrategy` es adapter.

Extraer Wrye Bash y plugin-limit guard son refactors separados — out of scope de esta rama. Contrato estricto: *"extraer la dispatch table"*, no *"reescribir cada handler"*.

---

## 10. Testing (implementado)

### Pre-refactor (Commit 1 — characterization tests, GREEN antes de tocar producción)

`tests/test_supervisor_dispatch_tool.py`: 17 tests (16 del plan + 1 adicional para ruteo inválido con payload vacío). Construye `SupervisorAgent.__new__(SupervisorAgent)` para saltar el `__init__` pesado, inyectando mocks sólo para los colaboradores que `dispatch_tool` toca. Asserta strings exactos (`"Usuario denegó la operación."`, `"ToolNotFound"`, `"SynthesisPipelineExecutionFailed"`, `"InvalidSynthesisPipelineResult"`, `"XEditPatchExecutionFailed"`, `"InvalidXEditPatchResult"`).

### Post-refactor (Commits 2–3 — aditivo, no toca producción)

- `tests/test_tool_dispatcher.py`: 10 tests — registro, duplicate detection, dispatch, unknown-tool, ordering LIFO, short-circuit, Protocol structural typing.
- `tests/test_tool_strategies_middleware.py`: 12 tests — `ErrorWrappingMiddleware` (no-op on success, wrap on `Exception`, NO atrapa `KeyboardInterrupt`/`CancelledError`/`SystemExit`); `DictResultGuardMiddleware` (dict passes, list/None/str rejected); composition.

### Verificación final

```bash
pytest tests/           # 1120 passed, 9 skipped, 0 failures
ruff check sky_claw/    # All checks passed!
mypy sky_claw/orchestrator/  # Success: no issues found in 20 source files
```

---

## 11. Sequencing — 6 commits, uno por paso

| Commit | SHA abreviado | Cambio | Behavior change |
|---|---|---|---|
| 1 | `020325e` | Characterization tests (17 tests, 0 producción) | Ninguno — lock-in del comportamiento. |
| 2 | `bf40efc` | `tool_dispatcher.py` + `tool_strategies/base.py` + tests del dispatcher | Ninguno (no wired). |
| 3 | `c4227b6` | `middleware.py` (ErrorWrap + DictGuard) + tests | Ninguno (no wired). |
| 4 | `5f1ebd7` | **Canary:** `QueryModMetadataStrategy` + factory mínima + wired en `__init__`. Sólo `case "query_mod_metadata"` delega | Funcionalmente idéntico. |
| 5 | `7bc4c32` | Migrar 8 strategies restantes + aplicar Option B (`action_type="reorder_load_order"`). Todas las branches delegan via OR-pattern | Funcionalmente idéntico. |
| 6 | `2ef1116` | Borrar el match/case muerto; `dispatch_tool` = one-liner | Funcionalmente idéntico. |

---

## 12. Decisión §13 resuelta: **Option B**

`execute_loot_sorting` usaba `action_type="destructive_xedit"` con el comentario `# Reusado conceptualmente`. Resolución elegida por el usuario:

- Literal extendido aditivamente en [`sky_claw/core/models.py`](sky_claw/core/models.py):
  ```python
  action_type: Literal[
      "download_external",
      "destructive_xedit",
      "circuit_breaker_halt",
      "reorder_load_order",  # nuevo (Option B)
  ]
  ```
- `ExecuteLootSortingStrategy` usa el valor nuevo `"reorder_load_order"`.
- Cambio **aditivo**: consumidores existentes del Literal (state_graph, GUI) siguen funcionando. El único log que imprime `action_type` (`state_graph.py:370`) no dispatches sobre el valor.

---

## 13. Definition of Done — cumplido

- [x] `TECHNICAL_SPEC_DISPATCHER.md` creado en raíz del repo.
- [x] 17 characterization tests verdes antes de Commit 2.
- [x] `dispatch_tool` reducido a una línea; match/case eliminado.
- [x] `AsyncToolRegistry` sin modificaciones (`git log origin/main..HEAD -- sky_claw/agent/tools/` sin output).
- [x] `supervisor.py` reducido de 669 → 569 líneas (−15%).
- [x] Suite completa `pytest tests/` verde (1120 passed, 9 skipped).
- [x] `ruff check sky_claw/` limpio.
- [x] `mypy sky_claw/orchestrator/` limpio.

---

**FIN.** God Class desmantelada. Complejidad de añadir una nueva herramienta: *crear un archivo de strategy + un `register()` en la factory*. De O(n) en el match/case a O(1) localizado.
