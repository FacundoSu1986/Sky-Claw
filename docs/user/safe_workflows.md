# Flujos seguros

> **Audiencia:** usuarios y operadores que ejecutan tools de modding.
>
> **Estado:** Implementado en sus controles; la cobertura end-to-end varía por
> runner.
>
> **Fuentes canónicas:** `sky_claw/local/AGENTS.md`,
> `sky_claw/antigravity/orchestrator/` y `sky_claw/local/mo2/`.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Primer arranque

1. Usar un perfil MO2 descartable.
2. Completar configuración y escaneo sin ejecutar herramientas.
3. Confirmar instancia, perfil, ejecutables y rutas sandbox.
4. Instalar y validar el bridge USVFS.
5. Crear un baseline conocido y conservar evidencia fuera del perfil.

## Antes de una operación

- No hay otro escritor sobre el mismo perfil o load order.
- El preflight coincide con la intención.
- El preview no muestra rutas fuera del sandbox.
- Existe snapshot o backup para cada archivo mutable.
- La aprobación HITL corresponde a la operación que se ejecutará.

## Durante y después

No cambiar de perfil ni cerrar MO2 durante una ejecución brokerizada. Al
finalizar, revisar resultado, journal, artefactos y procesos. Un `success=true`
sin artefacto esperado no basta para una validación real.

## Orden del pipeline

El DAG canónico vive en `sky_claw/local/AGENTS.md`. xEdit/QAC pertenece a la
etapa 1 y LOOT a la etapa 5. CAO es una acción por mod y no una barrera global,
pero esa propiedad no altera el orden canónico de xEdit y LOOT.

## Criterios de parada

Detener nuevas operaciones ante drift de perfil, pérdida de lock, timeout sin
reap comprobado, rollback incompleto, backup preservado por fallo de rollback
o ausencia de visibilidad USVFS.
