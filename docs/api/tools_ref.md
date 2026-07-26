# Referencia de tools

> **Audiencia:** desarrolladores y agentes que extienden o trazan tools.
>
> **Estado:** Implementado.
>
> **Fuentes canónicas:** `sky_claw/app/agent/tools/`,
> `sky_claw/app/orchestrator/tool_dispatcher.py`,
> `sky_claw/app/orchestrator/tool_strategies/` y
> `sky_claw/local/tools/tool_result.py`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Ruta LLM

`AsyncToolRegistry` registra descriptores con modelo Pydantic estricto, publica
JSON Schema y ejecuta handlers async. Su inventario incorporado verificado es:

`search_mod`, `install_mod`, `download_mod`, `search_nexus`,
`check_load_order`, `detect_conflicts`, `run_loot_sort`,
`run_xedit_script`, `preview_mod_installer`, `install_mod_from_archive`,
`resolve_fomod`, `analyze_esp_conflicts`, `run_pandora`, `run_bodyslide`,
`generate_bashed_patch`, `uninstall_mod`, `toggle_mod`, `launch_game`,
`close_game` y `setup_tools`.

La allowlist de sesión filtra tanto la enumeración visible como la ejecución.

## Ruta de orquestación

`build_orchestration_dispatcher()` registra `ToolStrategy` y middleware. Los
nombres comprobados son:

`query_mod_metadata`, `scan_asset_conflicts`, `scan_asset_conflicts_json`,
`quick_auto_clean`, `execute_loot_sorting`, `generate_animations`,
`generate_lods`, `generate_bashed_patch`, `validate_plugin_limit`,
`resolve_conflict_with_patch`, `analyze_grass_prerequisites`,
`generate_grass_cache`, `preview_chain` y `execute_synthesis_pipeline`.

El dispatcher devuelve un dict. Una tool desconocida conserva
`{"status": "error", "reason": "ToolNotFound"}`. El middleware cubre HITL,
idempotencia, guardrail de loops, errores y forma del dict según la estrategia
configurada. `ProgressMiddleware` existe como capacidad separada; comprobar su
wiring antes de prometer eventos de progreso en una ruta concreta.

## Resultados

Los runners pueden emitir shapes legacy. `normalize_tool_result()` es la única
pieza que conoce `details`, `error`, `logs`, `stderr`, `errors` y `reason`, y
produce la vista común con `success` y `message`. El consumidor de producción
comprobado está en `gui/controllers/ritual_runner.py`.

No aplicar el schema estricto de un resultado normalizado a un dict legacy
antes de normalizar: podría descartar el detalle de diagnóstico.

## Extensión

Seguir [creación de tools](../agents/tool_creation.md) y
[validación de schemas](../agents/schema_validation.md). Elegir una sola ruta
según el caller; no crear una tercera abstracción.
