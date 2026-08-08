# Diseño

> **Estado:** registros de diseño; pueden ser históricos, parciales o previos a implementación.
>
> **Audiencia:** desarrolladores, reviewers y agentes.
>
> **Fuente canónica:** archivos bajo `specs/` y `plans/`.
>
> **Última verificación del índice:** 2026-08-08 sobre `origin/main` `01faebd3`.

Los archivos de este directorio explican una intención aprobada en su momento. No
deben usarse como referencia runtime sin comprobar el código actual y los ADR
posteriores.

## Especificaciones (`specs/`)

Diseño acordado antes de implementar: contexto, decisión y alcance.

- [#330 — Remediación crítica del núcleo asíncrono](specs/2026-07-18-core-async-critical-remediation-design.md)
- [#372 — Endurecimiento tras la revisión de crash logging](specs/2026-07-26-crash-logging-review-hardening-design.md)
- [#411 — Salida administrada y reversible de Pandora](specs/2026-07-31-pandora-managed-output-design.md)

## Planes de implementación (`plans/`)

Descomposición en tareas de una spec o de un objetivo concreto.

- [#330 — Core Async Critical Remediation](plans/2026-07-18-core-async-critical-remediation.md)
- [#411 — Pandora Managed Output](plans/2026-07-31-pandora-managed-output.md)
- [#427 — Autoinstalación de SKSE](plans/2026-08-02-skse-autoinstall.md)

El número es el PR que mergeó cada documento junto con su implementación. Para
saber qué sigue vigente, contrastar con el código, los tests y los ADR
posteriores según
[`../documentation/source_of_truth.md`](../documentation/source_of_truth.md).
