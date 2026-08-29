# AGENTS.md — runners y servicios de tools

Aplican [`../AGENTS.md`](../AGENTS.md), que contiene el DAG canónico, y las
reglas raíz.

## Fuentes obligatorias

El runner, su service, `tool_result.py`, tests contractuales y el
`AGENTS.md` de MO2 si la ejecución usa VFS.

## Invariantes

- Nuevos resultados incluyen `success: bool` y `message: str` — **salvo la
  excepción registrada abajo**. La regla vale para lo que un dispatcher pueda
  alcanzar; una excepción sin registrar acá es un defecto, no un permiso.
- Sólo `normalize_tool_result()` conoce las claves legacy comunes.
- Construir subprocess como argumentos, validar paths y aplicar timeout.
- En timeout/cancelación: terminar árbol y reap antes de devolver.
- No afirmar smoke real a partir de un mock.

## Excepción registrada al contrato `success`/`message`

`dyndolod_uia_preflight.ResultadoPreflightUIA` **no** lleva `success`/`message`,
y es deliberado. Vive acá por dominio —observa la GUI de TexGen/DynDOLOD— pero no
es un tool: no está cableado a `tool_dispatcher` ni a `AsyncToolRegistry`, a
propósito. El motivo es de semántica, no de comodidad: un `success=True` sobre un
preflight de UI Automation se lee como *"DynDOLOD va a escribir bien"*, y eso es
exactamente lo que la observación **no** puede afirmar — entre lo que muestra la
caja de texto y dónde aterrizan los bytes hay un preset y un `-o:` cuya autoridad
relativa todavía no está establecida. El veredicto va en un enum de TRES valores
(`MATCH` / `MISMATCH` / `UNKNOWN`), porque con dos no hay dónde poner la
ambigüedad.

Qué la verifica —sin esto la excepción envejece como cualquier otra prosa—:
`tests/test_dyndolod_uia_preflight.py::test_el_resultado_no_expone_un_success_booleano`
falla si alguien le agrega el campo, y `test_las_razones_estan_congeladas` exige
que toda razón nueva se declare en vez de reciclar una existente.

**Cuándo deja de aplicar:** el día que T5-v2 exponga esta capacidad como tool
—desde la GUI, desde el agente LLM, o desde ambos—, el resultado que cruce esa
frontera sí debe cumplir el contrato. La traducción se hace en el borde; el enum
de tres valores no se aplana a un booleano para conseguirlo.

## Verificación segura

Tests unitarios para argumentos y contratos; integración para lifecycle; rig
real por runner antes de una garantía operativa.

## Pendiente: smoke real de "Limpiar Archivos" (QuickAutoClean)

Toca a `xedit_service.py`. Los tests mockean el subproceso: validan los argumentos
(`-quickautoclean -autoexit -autoload`, los mismos que usa PACT) pero no que SSEEdit
limpie de verdad. Falta un smoke del Ritual en una instalación real con SSEEdit antes
de confiar al 100%.
