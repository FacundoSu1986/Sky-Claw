# Añadir o exponer una herramienta

> **Audiencia:** desarrolladores y agentes de IA.
> **Estado:** guía de extensibilidad basada en rutas implementadas.
> **Fuentes canónicas:** `app/agent/tools/`,
> `app/orchestrator/tool_dispatcher.py`, `orchestrator/tool_strategies/`
> y `local/tools/`.
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
> **Sincronización de contratos P1:** 2026-08-16 sobre `main`.

Esta guía explica **dónde** encaja una herramienta. No contiene una
implementación ficticia: el patrón correcto depende de la ruta productiva que
la vaya a consumir.

## 1. Elegir la ruta real

### Herramienta invocada por el LLM

Usar `AsyncToolRegistry` cuando el `LLMRouter` deba anunciar un JSON schema,
validar parámetros y ejecutar un handler. Los descriptores viven en
`sky_claw/app/agent/tools/` y el registry se construye en
`AppContext.start_full()`.

Puntos de referencia:

- `agent/tools/descriptor.py`: `ToolDescriptor`.
- `agent/tools/schemas.py`: modelos de parámetros.
- `agent/tools/__init__.py`: registro, `execute()` y builtins.
- `agent/tools/system_tools.py`: handlers del sistema.

### Ritual o estrategia del orquestador

Usar `OrchestrationToolDispatcher` cuando la operación pertenezca al
`SupervisorAgent` y necesite `ToolStrategy`, middleware, HITL, idempotencia o
guardrail de loop.

Puntos de referencia:

- `orchestrator/tool_strategies/base.py`: contratos de strategy y middleware.
- `orchestrator/tool_dispatcher.py`: `register()`, `dispatch()` y
  `build_orchestration_dispatcher()`.
- `orchestrator/tool_strategies/execute_loot_sorting.py`: strategy pequeña.
- `orchestrator/tool_strategies/execute_synthesis.py`: sandbox y promoción.

No deducir una API común a partir de ejemplos externos: las únicas rutas
documentadas aquí son las clases y factories enlazadas desde el árbol actual.

## 2. Reutilizar un servicio local

Los wrappers de ejecutables viven en `sky_claw/local/tools/`. Antes de añadir
uno nuevo:

1. Leer `sky_claw/local/AGENTS.md`.
2. Reutilizar `_process.py` cuando el shape de ejecución lo permita.
3. Construir comandos como lista de argumentos y validar todas las rutas.
4. Definir timeout, cancelación, kill-tree/reap y verificación del artefacto.
5. Participar en locks, snapshots, journal o sandbox si la operación muta estado.
6. Retornar `success: bool` y `message: str` además de los campos estructurados.

Las claves `error`, `stderr`, `logs`, `details`, `errors` y `reason` son
compatibilidad legacy conocida sólo por `normalize_tool_result()`.

## 3. Contratos y validación

- En la ruta LLM, el `params_model` del `ToolDescriptor` es la fuente de
  validación de argumentos.
- `CONTRACT_SCHEMAS` sólo aplica a los métodos registrados en
  `core/contracts.py`.
- Las strategies pueden validar payloads con sus modelos propios y middleware.
- Un output Pydantic estricto debe declarar todos los campos retornados; no se
  deben mezclar ejemplos con campos que el modelo descartaría o rechazaría.

Véase [schema_validation.md](schema_validation.md).

## 4. Seguridad y efectos laterales

La guía de una tool debe documentar:

- entrada externa y normalización;
- roots autorizados y validator empleado;
- recursos/locks adquiridos;
- archivos y estado persistente que puede mutar;
- subprocess, timeout y cancelación;
- evidencia de éxito más fuerte que `returncode == 0`;
- rollback o recuperación manual;
- frontera HITL concreta de la ruta elegida.

No se debe afirmar que la capa LLM tiene un gate HITL general: el contrato base
es lock-only. Un handler puede recibir `HITLGuard` explícitamente, y los
rituales destructivos del dispatcher o la promoción de sandbox pueden tener
gates específicos.

### SQLite gestionado

Si la tool o servicio usa una conexión administrada por
`DatabaseLifecycleManager`, el SQL debe ejecutarse dentro del boundary vigente:

- `operation(path)` para una unidad SQL no transaccional;
- `transaction(path)` para una mutación que requiere commit/rollback.

`get_connection(path)` resuelve la conexión, pero no conserva el derecho de uso
frente a un shutdown concurrente. No introducir el patrón
`get_connection() -> await externo -> SQL` en un consumidor lifecycle-backed.
Red, filesystem, sleeps y backoff deben permanecer fuera del boundary DB.

Si una implementación histórica muestra otro patrón, verificar el runtime y los
tests actuales antes de copiarla. Una contradicción se registra como
`DOCUMENTATION_DRIFT`, no como razón para revertir el contrato moderno.

## 5. Testing

- Lógica pura: unit tests.
- Servicio: mock sólo en bordes de filesystem/subprocess/red.
- Strategy/handler: probar validación, wiring, error serializable y cancelación.
- Operación mutante: probar lock, snapshot/journal y rollback.
- SQL lifecycle-backed: probar que la unidad conserva `operation()` o
  `transaction()` durante el acceso y que shutdown no cierra la conexión debajo.
- Ejecutable real: registrar el smoke por separado; un mock no prueba la tool.

Los tests y comentarios del repositorio se escriben en español.

## 6. Estándar breve de docstring

```python
"""Ejecuta la operación sobre un perfil MO2 ya validado.

Contrato:
    Entrada y salida canónicas, incluyendo ``success`` y ``message``.

Efectos:
    Archivos, locks, journal, procesos y estado persistente afectados.

Fallos:
    Timeout, cancelación, validación, artefacto ausente y recuperación.

Fuentes:
    Servicio, strategy/handler, SOP y tests ancla.
"""
```

## 7. Checklist de integración

- [ ] Ruta LLM o ruta de orquestación elegida explícitamente.
- [ ] Source file real usado como patrón.
- [ ] Paths y entradas externas validados.
- [ ] Contrato `success`/`message` preservado.
- [ ] Timeout, cancelación y reap documentados.
- [ ] Lock/snapshot/journal/sandbox documentados si aplican.
- [ ] Boundary `operation()`/`transaction()` conservado si usa SQLite gestionado.
- [ ] Tests del handler/strategy y servicio identificados.
- [ ] Smoke real separado de los tests mockeados.
- [ ] SOP y matriz de impacto actualizados si cambia el pipeline.
