# Matriz de impacto documental

> **Audiencia:** autores, reviewers y agentes.
>
> **Estado:** política documental vigente.
>
> **Fuente canónica:** `docs/documentation/maintenance.md`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

| Cambio observado | Documentos a revisar |
|---|---|
| Contrato o schema | `docs/api/contracts_ref.md`, `docs/agents/schema_validation.md`, callers |
| Excepción pública | `docs/api/errors_ref.md`, troubleshooting aplicable |
| Tool LLM | `docs/api/tools_ref.md`, `docs/agents/tool_creation.md` |
| Tool de orquestación | `docs/api/tools_ref.md`, arquitectura de routing, SOP si cambia pipeline |
| Topic o `EventType` | `docs/api/events_ref.md`, arquitectura async/UI |
| Flag CLI | QUICKSTART, DEPLOYMENT, `docs/api/cli_ref.md` |
| Config o secreto | guía de configuración, referencia, SECURITY |
| Startup/shutdown | lifecycle, async/concurrency, recovery |
| DB, lock, snapshot o journal | persistencia/recuperación, operations |
| VFS o subprocess | arquitectura VFS, rig real, troubleshooting |
| GUI o Telegram | manual de superficie, `ui_comms.md` |
| Estado de release | README, CHANGELOG, DEPLOYMENT, SECURITY, release runbook |
| Regla de pipeline | sólo `sky_claw/local/AGENTS.md`; los demás enlazan |

La revisión es obligatoria aunque el cambio no requiera editar todos los
documentos: dejar constancia si una página no se ve afectada.
