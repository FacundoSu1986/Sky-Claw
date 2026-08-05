# AGENTS.md — runners y servicios de tools

Aplican [`../AGENTS.md`](../AGENTS.md), que contiene el DAG canónico, y las
reglas raíz.

## Fuentes obligatorias

El runner, su service, `tool_result.py`, tests contractuales y el
`AGENTS.md` de MO2 si la ejecución usa VFS.

## Invariantes

- Nuevos resultados incluyen `success: bool` y `message: str`.
- Sólo `normalize_tool_result()` conoce las claves legacy comunes.
- Construir subprocess como argumentos, validar paths y aplicar timeout.
- En timeout/cancelación: terminar árbol y reap antes de devolver.
- No afirmar smoke real a partir de un mock.

## Verificación segura

Tests unitarios para argumentos y contratos; integración para lifecycle; rig
real por runner antes de una garantía operativa.

## Pendiente: smoke real de "Limpiar Archivos" (QuickAutoClean)

Toca a `xedit_service.py`. Los tests mockean el subproceso: validan los argumentos
(`-quickautoclean -autoexit -autoload`, los mismos que usa PACT) pero no que SSEEdit
limpie de verdad. Falta un smoke del Ritual en una instalación real con SSEEdit antes
de confiar al 100%.
