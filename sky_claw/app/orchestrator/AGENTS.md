<!--
  Pointer for AI agents working inside sky_claw/app/orchestrator/.
  This subtree does not have a per-directory AGENTS.md with rules of its own;
  instead it redirects to the canonical pipeline SOP, which is the only
  authoritative source for pipeline-ordering, tool constraints, and
  failure-mode rules that affect tool_strategies/.
-->

# AGENTS.md — orquestación

Este subárbol coordina estrategias, middleware, supervisor, daemons, preview,
HITL y promoción. No reescribe el pipeline: sus reglas viven en el SOP
canónico y aplican a `tool_strategies/`.

## Leer primero

**`sky_claw/local/AGENTS.md`** y
[`../../../docs/architecture/agent_tool_routing.md`](../../../docs/architecture/agent_tool_routing.md).

## Invariantes locales

- `OrchestrationToolDispatcher` y `build_orchestration_dispatcher()` son la
  ruta implementada; no inventar factories alternativas.
- Middleware global envuelve al middleware por tool.
- HITL destructivo falla cerrado sin gate disponible.
- Shutdown drena trabajo pendiente antes de cerrar journal y DB.
- Un dict de estrategia no es automáticamente un `ToolResult` normalizado.

## Verificación segura

Cubrir dispatcher, middleware, supervisor y cancelación con tests focalizados.
Para reglas del dominio o ejecución real, aplicar además el SOP y el rig real.
Las convenciones raíz están en [`../../../AGENTS.md`](../../../AGENTS.md).
