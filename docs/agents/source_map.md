# Mapa de fuentes

> **Audiencia:** agentes y nuevos contribuidores.
>
> **Estado:** Implementado.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

| Pregunta | Primera fuente |
|---|---|
| ¿Cómo arranca y termina? | `sky_claw/__main__.py`, `sky_claw/app_context.py` |
| ¿Cuáles son los contratos comunes? | `antigravity/core/contracts.py`, `schemas.py`, `errors.py` |
| ¿Cómo vive SQLite? | `antigravity/core/db_lifecycle.py`, `antigravity/db/` |
| ¿Cómo publica el core? | `antigravity/core/event_bus.py` |
| ¿Cómo reacciona la GUI? | `antigravity/gui/gui_event_adapter.py`, `state/`, `controllers/` |
| ¿Cómo invoca tools el LLM? | `antigravity/agent/tools/`, `agent/router.py` |
| ¿Cómo ejecuta rituales el orquestador? | `orchestrator/tool_dispatcher.py`, `tool_strategies/` |
| ¿Dónde se normalizan resultados? | `local/tools/tool_result.py`; caller GUI en `ritual_runner.py` |
| ¿Dónde están HITL y seguridad? | `antigravity/security/hitl.py`, `antigravity/security/` |
| ¿Cómo se resuelven paths? | `antigravity/core/path_resolver.py`, `config.py` |
| ¿Cómo se integra MO2? | `local/mo2/` |
| ¿Cómo se prueba USVFS? | `local/mo2/vfs_broker.py`, `vfs_worker.py`, `vfs_attestation.py` |
| ¿Cuál es el DAG de modding? | `sky_claw/local/AGENTS.md` |
| ¿Qué está pendiente? | `docs/pending_ooda_status.md`, reverificado contra código |

Para orientación conceptual usar [arquitectura](../architecture/README.md); para
firmas y nombres exactos usar [referencia técnica](../api/README.md).
