# Enrutamiento de agentes y tools

> **Estado:** implementado en dos rutas distintas.
>
> **Audiencia:** desarrolladores y agentes.
>
> **Fuentes canónicas:** `sky_claw/app/agent/tools/`,
> `sky_claw/app/orchestrator/tool_dispatcher.py`,
> `sky_claw/app/orchestrator/dispatcher_dependencies.py` y sus callers.
>
> **Última verificación integral previa:** 2026-07-25 sobre `origin/main`
> `c6ab35e`.
>
> **Frontera del dispatcher:** re-verificada el 2026-08-28 contra el código y
> los tests estructurales de este cambio.

## Ruta LLM

```mermaid
flowchart LR
    UI["CLI / Telegram / GUI chat"] --> Router["LLMRouter.chat"]
    Router --> Provider["LLMProvider"]
    Provider --> Call["native tool_use / XML <tool_call>"]
    Call --> Registry["AsyncToolRegistry.execute"]
    Registry --> Handler["agent/tools handler"]
    Handler --> Local["servicio o runner local"]
    Local --> Router
```

`AsyncToolRegistry` anuncia schemas, aplica el `params_model`, allowlist por
sesión y timeout del round. No tiene un middleware HITL general: la política
base es lock-only, aunque handlers concretos como `download_mod` reciben un
`HITLGuard` explícito.

## Ruta de rituales

```mermaid
flowchart LR
    UI["Acción GUI / supervisor"] --> Supervisor["SupervisorAgent.dispatch_tool"]
    Supervisor --> Deps["OrchestrationDispatcherDependencies"]
    Deps --> Dispatcher["OrchestrationToolDispatcher.dispatch"]
    Dispatcher --> MW["Middleware global y por tool"]
    MW --> Strategy["ToolStrategy.execute"]
    Strategy --> Service["servicio local"]
```

El dispatcher registra strategies con `register()`. Puede aplicar loop
guardrail, idempotencia, wrapping de errores, validación de dict y HITL.
Synthesis usa además `SandboxPromotionFlow`.

La frontera de composición es explícita: `SupervisorAgent.__init__` construye
un `OrchestrationDispatcherDependencies` inmutable con las quince capacidades
estrechas consumidas por las strategies. `tool_dispatcher.py` no conoce ni
consulta al supervisor. Los providers de preview y Synthesis son lazy y
cierran solamente sobre sus servicios, paths, journal, buses, locks, perfil y
guard concretos; construir el dispatcher no resuelve ejecutables ni rutas de
MO2. El callable de Wrye Bash cierra sobre su servicio y el perfil de sesión,
sin capturar al supervisor completo.

La aprobación destructiva pre-ejecución permanece en exactamente siete tools:
LOOT, resolución xEdit, DynDOLOD, Pandora, QuickAutoClean, Grass Cache y Wrye
Bash. Synthesis no usa ese middleware porque su sandbox solicita aprobación
post-ejecución sobre el diff; agregar el gate previo sería double-gating.

## Contratos que no deben mezclarse

- `ToolDescriptor` no es `ToolStrategy`.
- `AsyncToolRegistry.execute()` no es
  `OrchestrationToolDispatcher.dispatch()`.
- El orden de registro no define el DAG de modding.
- `CONTRACT_SCHEMAS` no reemplaza los `params_model` ni los modelos de strategy.
- `normalize_tool_result()` normaliza resultados crudos en consumidores que
  necesitan la vista común; no convierte ambos dispatchers en uno.

Véanse [tools_ref.md](../api/tools_ref.md) y
[tool_creation.md](../agents/tool_creation.md).
