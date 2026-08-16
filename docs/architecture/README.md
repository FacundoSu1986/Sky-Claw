# Arquitectura

> **Audiencia:** desarrolladores, operadores y agentes.
>
> **Estado:** índice de explicación del sistema implementado.
>
> **Fuentes canónicas:** `sky_claw/__main__.py`, `sky_claw/app_context.py`,
> `sky_claw/app/` y `sky_claw/local/`.
>
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
>
> **Sincronización DB/lifecycle:** 2026-08-16 sobre `main` `7156718`.

Lectura recomendada:

1. [Contexto del sistema](system_context.md)
2. [Ciclo de vida](runtime_lifecycle.md)
3. [Concurrencia](async_concurrency.md)
4. [Agentes y tools](agent_tool_routing.md)
5. [Datos y recuperación](data_persistence_recovery.md)
6. [Seguridad](security_boundaries.md)
7. [MO2/USVFS](mo2_vfs_subprocesses.md)
8. [GUI y comunicaciones](ui_comms.md)

Para cambios que involucren SQLite gestionado, leer conjuntamente
[runtime_lifecycle.md](runtime_lifecycle.md) y
[data_persistence_recovery.md](data_persistence_recovery.md): el contrato actual
distingue resolución de conexión (`get_connection`) del boundary de uso
(`operation`/`transaction`) y coordina shutdown mediante admisión + drenaje.

Para contradicciones entre esta documentación y el árbol actual, aplicar
[Fuentes de verdad](../documentation/source_of_truth.md). Un agente no debe
refactorizar producción para hacerla coincidir con documentación histórica sin
verificar primero el contrato runtime.

[ARCHITECTURE.md](../../ARCHITECTURE.md) conserva la vista ejecutiva. Las
firmas exactas viven en [docs/api](../api/README.md).

La sincronización DB/lifecycle no implica una reverificación integral del resto
de las páginas de este índice.
