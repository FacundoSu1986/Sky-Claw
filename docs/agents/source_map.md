# Mapa de fuentes

> **Audiencia:** agentes y nuevos contribuidores.
>
> **Estado:** Implementado.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

| Pregunta | Primera fuente |
|---|---|
| ¿Cómo arranca y termina? | `sky_claw/__main__.py`, `sky_claw/app_context.py` |
| ¿Cuáles son los contratos comunes? | `sky_claw/antigravity/core/contracts.py`, `sky_claw/antigravity/core/schemas.py`, `sky_claw/antigravity/core/errors.py` |
| ¿Cómo vive SQLite? | `sky_claw/antigravity/core/db_lifecycle.py`, `sky_claw/antigravity/db/` |
| ¿Cómo publica el core? | `sky_claw/antigravity/core/event_bus.py` |
| ¿Cómo reacciona la GUI? | `sky_claw/antigravity/gui/gui_event_adapter.py`, `sky_claw/antigravity/gui/state/`, `sky_claw/antigravity/gui/controllers/` |
| ¿Cómo invoca tools el LLM? | `sky_claw/antigravity/agent/tools/`, `sky_claw/antigravity/agent/router.py` |
| ¿Cómo ejecuta rituales el orquestador? | `sky_claw/antigravity/orchestrator/tool_dispatcher.py`, `sky_claw/antigravity/orchestrator/tool_strategies/` |
| ¿Dónde se normalizan resultados? | `sky_claw/local/tools/tool_result.py`; caller GUI en `sky_claw/antigravity/gui/controllers/ritual_runner.py` |
| ¿Dónde están HITL y seguridad? | `sky_claw/antigravity/security/hitl.py`, `sky_claw/antigravity/security/` |
| ¿Cómo se resuelven paths? | `sky_claw/antigravity/core/path_resolver.py`, `sky_claw/config.py` |
| ¿Cómo se integra MO2? | `sky_claw/local/mo2/` |
| ¿Cómo se prueba USVFS? | `sky_claw/local/mo2/vfs_broker.py`, `sky_claw/local/mo2/vfs_worker.py`, `sky_claw/local/mo2/vfs_attestation.py` |
| ¿Cuál es el DAG de modding? | `sky_claw/local/AGENTS.md` |
| ¿Qué está pendiente? | `docs/pending_ooda_status.md`, reverificado contra código |

Para orientación conceptual usar [arquitectura](../architecture/README.md); para
firmas y nombres exactos usar [referencia técnica](../api/README.md).
