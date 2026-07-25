# AGENTS.md — contexto de `antigravity`

Aplican primero [`../../AGENTS.md`](../../AGENTS.md) y la
[fuente de verdad documental](../../docs/documentation/source_of_truth.md).

## Alcance

Este paquete contiene lifecycle, núcleo async, DB, agentes, orquestación, GUI,
seguridad y comunicaciones. Antes de cambiar una frontera, localizar sus
callers en `sky_claw/app_context.py`.

## Invariantes

- No mezclar `CoreEventBus` con el `EventBus` de GUI.
- Preservar `CancelledError` y cleanup acotado.
- No presentar una tool LLM y una estrategia de orquestación como un único
  dispatcher.
- No afirmar cobertura HITL fuera del handler o middleware comprobado.

Consultar el `AGENTS.md` más próximo y
[el mapa de fuentes](../../docs/agents/source_map.md).
