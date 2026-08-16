# Mapa de fuentes

> **Audiencia:** agentes y nuevos contribuidores.
>
> **Estado:** Implementado.
>
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
>
> **Sincronización de contratos P1:** 2026-08-16 sobre `main`.

Antes de usar este mapa, aplicar la precedencia de
[fuentes de verdad](../documentation/source_of_truth.md): código y tests del
árbol actual dominan sobre documentación derivada cuando existe contradicción.

| Pregunta | Primera fuente |
|---|---|
| ¿Qué autoridad tiene una guía frente al runtime? | `docs/documentation/source_of_truth.md`, `AGENTS.md` aplicable |
| ¿Cómo arranca y termina? | `sky_claw/__main__.py`, `sky_claw/app_context.py` |
| ¿Cuáles son los contratos comunes? | `sky_claw/app/core/contracts.py`, `sky_claw/app/core/schemas.py`, `sky_claw/app/core/errors.py` |
| ¿Cómo vive SQLite? | `sky_claw/app/core/db_lifecycle.py`, `sky_claw/app/db/` |
| ¿Qué protege el uso de una conexión SQLite gestionada? | `DatabaseLifecycleManager.operation()` / `transaction()` y tests de lifecycle |
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

## Regla operativa para agentes

Este mapa sirve para llegar a la fuente, no para sustituirla. Si el primer
archivo consultado contradice un README, auditoría o plan histórico, no
"armonizar" el código con el documento. Trazar caller + tests y registrar el
caso como `DOCUMENTATION_DRIFT`.

Para SQLite gestionado, `get_connection()` sólo resuelve una conexión; el
boundary de uso durante SQL vive en `operation()` o `transaction()`. Un agente
no debe reintroducir `get_connection() -> await -> SQL` en un consumer
lifecycle-backed basándose en documentación antigua.

Para orientación conceptual usar [arquitectura](../architecture/README.md); para
firmas y nombres exactos usar [referencia técnica](../api/README.md).
