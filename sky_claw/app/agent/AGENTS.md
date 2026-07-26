# AGENTS.md — capa del agente LLM

Aplican [`../AGENTS.md`](../AGENTS.md) y las reglas raíz.

## Fuentes obligatorias

`router.py`, `providers.py`, `tools/` y
[`../../../docs/agents/tool_creation.md`](../../../docs/agents/tool_creation.md).

## Invariantes

- `AsyncToolRegistry` es la ruta LLM; no confundirla con el dispatcher del
  orquestador.
- Los parámetros se derivan del modelo Pydantic registrado.
- La allowlist filtra enumeración y ejecución.
- La decisión base de esta capa es lock-only, sin middleware HITL general; un
  handler como `download_mod` puede recibir `HITLGuard` explícito.
- Egress y secretos pasan por fronteras inyectadas; no leer secrets del entorno
  como fallback.

## Verificación segura

Tests de schemas, allowlist, router y provider con bordes externos mockeados.
