# Mantenimiento documental

> **Audiencia:** maintainers, autores y reviewers.
>
> **Estado:** proceso vigente.
>
> **Fuente canónica:** este documento y `docs/agents/change_impact.md`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Ownership por cambio

| Si cambia | Revisar |
|---|---|
| CLI o modos | `QUICKSTART.md`, `DEPLOYMENT.md`, `docs/api/cli_ref.md` |
| Configuración o secretos | `docs/user/configuration.md`, `docs/api/config_ref.md`, `SECURITY.md` |
| Schema o contrato | `docs/api/contracts_ref.md`, `docs/agents/schema_validation.md` |
| Tool LLM | `docs/api/tools_ref.md`, `docs/agents/tool_creation.md` |
| Strategy o ritual | arquitectura de routing, SOP y `AGENTS.md` aplicables |
| Lifecycle o bus | arquitectura async/runtime, operaciones y recuperación |
| MO2/USVFS | arquitectura VFS, deployment y smoke real |
| Release | README, CHANGELOG, DEPLOYMENT, SECURITY y `operations/release.md` |

## Cadencia

- En cada PR: actualizar documentación afectada en el mismo cambio.
- En cada release: revisar instalación, configuración, seguridad y limitaciones.
- Tras una auditoría: guardar evidencia en `docs/audits/` y trasladar sólo los
  hechos todavía vigentes a la referencia canónica.
- Ante drift detectado: corregir primero la garantía que pueda inducir una
  operación insegura.

## Evitar duplicación

Los documentos de subárbol enlazan reglas comunes. El DAG completo sólo vive en
`sky_claw/local/AGENTS.md`; los schemas completos sólo en código; la política
de prioridad sólo en `source_of_truth.md`.
