# Guías para agentes

> **Audiencia:** agentes de IA y humanos que supervisan cambios asistidos.
>
> **Estado:** Implementado.
>
> **Fuentes canónicas:** `AGENTS.md`, los `AGENTS.md` aplicables al subárbol y
> el código citado por cada guía.
>
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Inicio

1. [Bootstrap OODA](agent_bootstrap.md).
2. [Mapa de fuentes](source_map.md).
3. [Matriz de impacto](change_impact.md).
4. [Creación de tools](tool_creation.md).
5. [Validación de schemas](schema_validation.md).

Los archivos `AGENTS.md` forman una jerarquía: raíz más punteros locales. El
SOP del pipeline vive únicamente en `sky_claw/local/AGENTS.md`.

## Principio

Un agente debe poder distinguir instrucción, implementación, decisión
arquitectónica, estado operativo e historia. Ningún comentario de PR o
auditoría fechada reemplaza una verificación del árbol actual.
