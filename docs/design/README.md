# Diseño

> **Estado:** registros de diseño; pueden ser históricos, parciales o previos a implementación.
>
> **Audiencia:** desarrolladores, reviewers y agentes.
>
> **Fuente canónica del diseño acordado:** archivos bajo `specs/` y `plans/`.
>
> **Última verificación del índice:** 2026-08-10 sobre `claude/remove-superpowers-reintroduction-ldo2eb` `87b778e4`.

Los archivos de este directorio explican una intención aprobada en su momento. No
deben usarse como referencia runtime sin comprobar el código actual y los ADR
posteriores.

## Especificaciones (`specs/`)

Diseño acordado antes de implementar: contexto, decisión y alcance.

- [#330 — Remediación crítica del núcleo asíncrono](specs/2026-07-18-core-async-critical-remediation-design.md)
- [#372 — Endurecimiento tras la revisión de crash logging](specs/2026-07-26-crash-logging-review-hardening-design.md)
- [#411 — Salida administrada y reversible de Pandora](specs/2026-07-31-pandora-managed-output-design.md)
- [#454 — Perfil FOMOD y frontera segura de resultados de tools](specs/2026-08-09-fomod-profile-and-tool-output-security-design.md)
- [Knowledge Capture — captura local de casos verificables](specs/2026-08-11-knowledge-capture-design.md) — spec sin implementación: acompaña a [ADR 0008](../adr/0008-knowledge-case.md) y precede a su desarrollo, así que no lleva número de PR de implementación como el resto del índice.
- [Game Design Profile — intención de diseño del usuario](specs/2026-08-18-game-design-profile-intent.md) — propuesta documental sin implementación: registra el boundary y las fases futuras para traducir objetivos de experiencia a un perfil validado antes de cualquier planificación o mutación.

## Planes de implementación (`plans/`)

Descomposición en tareas de una spec o de un objetivo concreto.

- [#330 — Core Async Critical Remediation](plans/2026-07-18-core-async-critical-remediation.md)
- [#411 — Pandora Managed Output](plans/2026-07-31-pandora-managed-output.md)
- [#427 — Autoinstalación de SKSE](plans/2026-08-02-skse-autoinstall.md)
- [#454 — FOMOD Profile and Tool Output Security](plans/2026-08-09-fomod-profile-and-tool-output-security.md)
- [Roadmap DynDOLOD v2 — etapa 9](plans/2026-08-16-dyndolod-roadmap-v2.md) — plan **sin implementación**: re-baselinea el trabajo pendiente de la etapa 9 contra `main` y ordena los bloques que siguen, así que no lleva número de PR de implementación como el resto del índice.

El número es el PR que mergeó cada documento junto con su implementación. Para
saber qué sigue vigente, contrastar con el código, los tests y los ADR
posteriores según
[`../documentation/source_of_truth.md`](../documentation/source_of_truth.md).
