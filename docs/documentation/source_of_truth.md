# Fuentes de verdad

> **Audiencia:** usuarios, desarrolladores, operadores y agentes.
>
> **Estado:** política vigente para resolver drift.
>
> **Fuentes canónicas:** código, ADR, instrucciones e índices del árbol actual.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Precedencia de instrucciones para agentes

Dentro del repositorio, el `AGENTS.md` aplicable es la fuente canónica de
instrucciones para agentes. Se leen desde la raíz hasta el subárbol concreto;
un archivo más profundo añade o especializa reglas dentro de su alcance. Ninguna
guía derivada, auditoría o comentario de PR puede sustituir esas invariantes.

## Precedencia para describir el runtime

1. Código ejecutable del árbol actual y comportamiento ejercitado por tests.
2. ADR aceptado para intención y decisión arquitectónica.
3. Referencia técnica verificada en `docs/api/`.
4. Guías de arquitectura, usuario y operación.
5. Estado OODA y backlogs.
6. Auditorías, especificaciones y comentarios de PR históricos.

Los tests prueban el comportamiento que ejercitan; no convierten una afirmación
de smoke real en verdadera.

## Mapa

| Tema | Fuente principal |
|---|---|
| Arranque, composición y cierre | `sky_claw/app_context.py`, `sky_claw/__main__.py` |
| Contratos Pydantic | `app/core/contracts.py`, `core/schemas.py` |
| Resultado común | `local/tools/tool_result.py` y tests ancla |
| Ruta LLM | `app/agent/router.py`, `agent/tools/` |
| Ruta de rituales | `orchestrator/supervisor.py`, `tool_dispatcher.py`, `tool_strategies/` |
| DB y lifecycle | `core/db_lifecycle.py`, `core/database.py`, `app/db/` |
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
