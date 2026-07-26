# Validación de schemas y contratos

> **Audiencia:** desarrolladores y agentes que modifican límites Pydantic.
> **Estado:** implementado, con alcance limitado al registro documentado.
> **Fuente canónica:** `sky_claw/app/core/contracts.py`.
> **Última verificación:** 2026-07-25 sobre `origin/main` `c6ab35e`.

## Qué resuelve

`contracts.py` asocia determinadas claves `Clase.método` con modelos Pydantic
de entrada y salida. El objetivo es rechazar shapes inválidos antes o después
de ejecutar esos métodos.

No es el registro de todas las herramientas. La ruta LLM usa además
`AsyncToolRegistry` y la ruta de rituales usa `OrchestrationToolDispatcher`.

## Registry lazy

`_ensure_registry()` importa `core.schemas` en el primer uso, recorre sus
modelos y los guarda por nombre. La inicialización usa double-checked locking:
la primera llamada paga el escaneo; las resoluciones posteriores son lookups
directos en el diccionario.

Si el módulo de schemas no puede importarse, los decoradores quedan en
pass-through y se registra un warning. Errores inesperados limpian el estado
parcial y se propagan.

## Decoradores

- `validate_input(method_name)` valida argumentos y entrega `model_dump()` al
  método.
- `validate_output(method_name)` valida resultados `dict` o `BaseModel`; otros
  tipos pasan sin validación Pydantic.
- `validate_contract(method_name)` combina input, una ejecución del método y
  output en un solo wrapper.

La clave se construye con el nombre runtime de la clase. Renombrar una clase o
método exige actualizar `CONTRACT_SCHEMAS` y sus tests.

## Reglas al documentar schemas

1. Copiar nombres y campos sólo después de leer `core/schemas.py`.
2. Registrar `extra`, strictness, defaults, constraints y validators.
3. Distinguir `parameters` de `arguments`; hoy `AgentToolRequest` usa
   `parameters`.
4. No inventar versionado, deprecaciones ni compatibilidad implícita. Una
   política nueva requiere decisión arquitectónica y cambios reales.
5. Añadir la fecha y SHA usados para verificar cualquier tabla estática.

## Relación con resultados de tools

Los schemas Pydantic de un método y `ToolResult` resuelven problemas distintos:

- Pydantic valida la forma declarada de inputs/outputs.
- `normalize_tool_result()` produce una vista común desde resultados crudos y
  concentra compatibilidad con claves legacy.

Un schema con `extra="forbid"` no debe documentarse junto a retornos
adicionales que el propio modelo no acepta. Los servicios nuevos emiten
`success` y `message`; las claves legacy no forman parte del contrato recomendado.

## Verificación mínima

- Confirmar la entrada exacta en `CONTRACT_SCHEMAS`.
- Resolver cada nombre con `get_schema_class()`.
- Comparar la tabla documental con `model_fields`.
- Probar input inválido, output inválido y pass-through cuando no hay schema.
- Ejecutar `tests/test_contracts_ticket_1_1.py` y los tests del consumidor.

Véanse la [referencia de contratos](../api/contracts_ref.md) y la
[guía de creación de tools](tool_creation.md).
