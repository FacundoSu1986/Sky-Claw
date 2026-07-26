# Mapa de fuentes

> **Audiencia:** agentes y nuevos contribuidores.
>
> **Estado:** Implementado.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

| Pregunta | Primera fuente |
|---|---|
| ¿Cómo arranca y termina? | `sky_claw/__main__.py`, `sky_claw/app_context.py` |
| ¿Cuáles son los contratos comunes? | `sky_claw/app/core/contracts.py`, `sky_claw/app/core/schemas.py`, `sky_claw/app/core/errors.py` |
| ¿Cómo vive SQLite? | `sky_claw/app/core/db_lifecycle.py`, `sky_claw/app/db/` |
| ¿Cómo publica el core? | `sky_claw/app/core/event_bus.py` |
| ¿Cómo reacciona la GUI? | `sky_claw/app/gui/gui_event_adapter.py`, `sky_claw/app/gui/state/`, `sky_claw/app/gui/controllers/` |
| ¿Cómo invoca tools el LLM? | `sky_claw/app/agent/tools/`, `sky_claw/app/agent/router.py` |
| ¿Cómo ejecuta rituales el orquestador? | `sky_claw/app/orchestrator/tool_dispatcher.py`, `sky_claw/app/orchestrator/tool_strategies/` |
| ¿Dónde se normalizan resultados? | `sky_claw/local/tools/tool_result.py`; caller GUI en `sky_claw/app/gui/controllers/ritual_runner.py` |
| ¿Dónde están HITL y seguridad? | `sky_claw/app/security/hitl.py`, `sky_claw/app/security/` |
| ¿Cómo se resuelven paths? | `sky_claw/app/core/path_resolver.py`, `sky_claw/config.py` |
| ¿Cómo se integra MO2? | `sky_claw/local/mo2/` |
| ¿Cómo se prueba USVFS? | `sky_claw/local/mo2/vfs_broker.py`, `sky_claw/local/mo2/vfs_worker.py`, `sky_claw/local/mo2/vfs_attestation.py` |
| ¿Cuál es el DAG de modding? | `sky_claw/local/AGENTS.md` |
| ¿Qué está pendiente? | `docs/pending_ooda_status.md`, reverificado contra código |

Para orientación conceptual usar [arquitectura](../architecture/README.md); para
firmas y nombres exactos usar [referencia técnica](../api/README.md).
