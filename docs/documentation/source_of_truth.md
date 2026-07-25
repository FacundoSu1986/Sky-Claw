# Fuentes de verdad

> **Audiencia:** usuarios, desarrolladores, operadores y agentes.
>
> **Estado:** política vigente para resolver drift.
>
> **Fuentes canónicas:** código, ADR, instrucciones e índices del árbol actual.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Precedencia

1. Código ejecutable y tests del árbol actual.
2. ADR aceptado para intención y decisión arquitectónica.
3. `AGENTS.md` aplicable al subárbol para invariantes operativas.
4. Referencia técnica verificada en `docs/api/`.
5. Guías de arquitectura, usuario y operación.
6. Estado OODA y backlogs.
7. Auditorías, especificaciones y comentarios de PR históricos.

Los tests prueban el comportamiento que ejercitan; no convierten una afirmación
de smoke real en verdadera.

## Mapa

| Tema | Fuente principal |
|---|---|
| Arranque, composición y cierre | `sky_claw/app_context.py`, `sky_claw/__main__.py` |
| Contratos Pydantic | `antigravity/core/contracts.py`, `core/schemas.py` |
| Resultado común | `local/tools/tool_result.py` y tests ancla |
| Ruta LLM | `antigravity/agent/router.py`, `agent/tools/` |
| Ruta de rituales | `orchestrator/supervisor.py`, `tool_dispatcher.py`, `tool_strategies/` |
| DB y lifecycle | `core/db_lifecycle.py`, `core/database.py`, `antigravity/db/` |
| Buses | `core/event_bus.py`, `gui/gui_event_adapter.py` |
| Seguridad | `security/`, `config.py`, callers productivos |
| MO2/USVFS | `local/mo2/`, ADR 0007 |
| DAG de modding | `sky_claw/local/AGENTS.md` |
| CI | `.github/workflows/ci.yml` |
| Packaging | `sky_claw.spec`, `build.bat`, workflow Build |

## Procedimiento ante una contradicción

1. Refrescar `origin/main`.
2. Trazar el caller productivo, no sólo la definición aislada.
3. Verificar tests y ADR relevantes.
4. Corregir la página derivada o etiquetarla como histórica.
5. Registrar SHA y punto no verificado.

No crear una política nueva para “hacer coincidir” dos documentos antiguos.
