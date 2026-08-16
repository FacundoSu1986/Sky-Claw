# Referencia técnica

> **Audiencia:** desarrolladores, reviewers y agentes.
>
> **Estado:** Implementado; cada página declara su fuente canónica.
>
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
>
> **Sincronización de contratos P1:** 2026-08-16 sobre `e48306c`; limitada a las
> referencias y guardrails auditados en esta rama.

## Índice

- [CLI](cli_ref.md)
- [Configuración](config_ref.md)
- [Contratos y schemas](contracts_ref.md)
- [Eventos](events_ref.md)
- [Excepciones](errors_ref.md)
- [Tools](tools_ref.md)

Estas páginas describen el árbol verificado. No son una promesa de
compatibilidad pública ni crean versionado o deprecaciones que el código no
implemente.

## Uso por agentes

Las tablas de esta sección son copias documentales de contratos vivos. Antes de
modificar producción, un agente debe comprobar el símbolo y el caller actuales.
Una discrepancia entre esta referencia y código/tests se trata como
`DOCUMENTATION_DRIFT`; no autoriza a cambiar producción para hacerla coincidir
con una tabla antigua.

En particular:

- `CONTRACT_SCHEMAS` no es el inventario global de tools;
- excepciones de subsistemas no forman una única jerarquía común;
- schemas de tools LLM, contratos de agentes y resultados normalizados son
  fronteras distintas;
- una fecha reciente sólo cubre el alcance explícitamente reverificado.

La política completa está en
[fuentes de verdad](../documentation/source_of_truth.md).

## Regla de actualización

Cuando cambie un símbolo, actualizar primero su referencia directa y después
las guías que lo consumen. La matriz completa está en
[mantenimiento documental](../documentation/maintenance.md).
