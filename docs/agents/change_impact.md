# Matriz de impacto documental

> **Audiencia:** autores, reviewers y agentes.
>
> **Estado:** política documental vigente.
>
> **Fuente canónica:** `docs/documentation/maintenance.md`.
>
> **Última verificación:** 2026-08-16 sobre `main` `7156718` para la política de
> impacto DB/lifecycle; las demás filas conservan el alcance previo.

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
| DB lifecycle (`operation`, `transaction`, admisión, connection ownership, shutdown) | runtime: `sky_claw/app/core/db_lifecycle.py`, `sky_claw/app/core/database.py`, `sky_claw/app/db/`; docs: `docs/architecture/runtime_lifecycle.md`, `docs/architecture/data_persistence_recovery.md`, recovery y callers gestionados |
| DB, lock, snapshot o journal | persistencia/recuperación, operations |
| VFS o subprocess | arquitectura VFS, rig real, troubleshooting |
| GUI o Telegram | manual de superficie, `ui_comms.md` |
| Estado de release | README, CHANGELOG, DEPLOYMENT, SECURITY, release runbook |
| Regla de pipeline | sólo `sky_claw/local/AGENTS.md`; los demás enlazan |

La revisión es obligatoria aunque el cambio no requiera editar todos los
documentos: dejar constancia si una página no se ve afectada.

## Regla especial para cambios DB/lifecycle

Un cambio en cómo se obtiene una conexión SQLite no es suficiente para declarar
que el contrato permanece estable. El reviewer/agente debe verificar también:

- durante cuánto tiempo se conserva admisión;
- qué lock por path se sostiene mientras corre SQL;
- si la unidad es `operation()` o `transaction()`;
- ownership y cierre de la conexión;
- interacción con `shutdown_all()` y `init_all()`;
- semántica de cancelación y rollback si la unidad muta estado.

Si una guía describe un patrón anterior —por ejemplo `get_connection()` seguido
de SQL fuera del boundary— registrar `DOCUMENTATION_DRIFT`; no modificar
producción para ajustarla al documento sin demostrar primero que el runtime es el
que está equivocado.
