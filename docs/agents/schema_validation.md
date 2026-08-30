# Validación de schemas y contratos

> **Audiencia:** desarrolladores y agentes que modifican límites Pydantic.
> **Estado:** implementado, con alcance limitado al registro documentado.
> **Fuente canónica:** `sky_claw/app/core/contracts.py`.
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
> **Sincronización de contratos P1:** 2026-08-16 sobre `e48306c`.

## Qué resuelve

`contracts.py` asocia determinadas claves `Clase.método` con modelos Pydantic
de entrada y salida. El objetivo de los decoradores es rechazar shapes inválidos
antes o después de ejecutar los métodos donde realmente estén aplicados.

**Estado productivo verificado en `e48306c`:** las cinco claves listadas en
`CONTRACT_SCHEMAS` no tienen esos decoradores aplicados en sus métodos de
producción. Por tanto, el mapping existe, pero actualmente no impone validación
Pydantic en esos caller boundaries. No documentar la mera presencia del mapping
como enforcement activo.

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

Eso tiene una consecuencia importante para agentes: que un modelo siga
existiendo en `core/schemas.py` no demuestra que un caller esté validado. Hay que
verificar también la clave exacta `Clase.método`, el decorador aplicado y el
camino productivo que llega a ese método. En el baseline sincronizado, los
callers productivos mapeados están en pass-through precisamente porque esos
decoradores no están aplicados allí.

## Reglas al documentar schemas

1. Copiar nombres y campos sólo después de leer `core/schemas.py`.
2. Registrar `extra`, strictness, defaults, constraints y validators.
3. Distinguir `parameters` de `arguments`; hoy `AgentToolRequest` usa
   `parameters`.
4. No inventar versionado, deprecaciones ni compatibilidad implícita. Una
   política nueva requiere decisión arquitectónica y cambios reales.
5. Añadir la fecha y SHA usados para verificar cualquier tabla estática.
6. No extrapolar `extra="forbid"` a todos los modelos porque algunos límites
   usan otra política deliberadamente.
7. No usar `CONTRACT_SCHEMAS` como inventario de tools: una tool LLM se valida
   por su `ToolDescriptor.params_model`, y una strategy puede tener modelos
   propios.
8. Distinguir mapping declarado de enforcement activo: confirmar el decorador
   en el método productivo antes de afirmar que input/output se rechazan.

## Relación con resultados de tools

Los schemas Pydantic de un método y `ToolResult` resuelven problemas distintos:

- Pydantic valida la forma declarada de inputs/outputs cuando el decorador está
  aplicado al caller real.
- `normalize_tool_result()` produce una vista común desde resultados crudos y
  concentra compatibilidad con claves legacy.

Un schema con `extra="forbid"` no debe documentarse junto a retornos
adicionales que el propio modelo no acepta. Los servicios nuevos emiten
`success` y `message`; las claves legacy no forman parte del contrato recomendado.

## Guardrail ante documentación vieja

Si una tabla documental no coincide con `model_fields`, `CONTRACT_SCHEMAS` o el
caller real, clasificarlo como `DOCUMENTATION_DRIFT`. No modificar el schema ni
relajar validación para acomodar documentación antigua sin demostrar primero que
esa documentación representa el contrato intencional.

## Verificación mínima

- Confirmar la entrada exacta en `CONTRACT_SCHEMAS`.
- Resolver cada nombre con `get_schema_class()`.
- Comparar la tabla documental con `model_fields`.
- Verificar el decorador y el nombre runtime de la clase consumidora.
- No inferir enforcement desde el mapping si el método productivo no está
  decorado.
- Probar input inválido, output inválido y pass-through cuando no hay schema o
  no hay decorador productivo.
- Ejecutar `tests/test_contracts_ticket_1_1.py` y los tests del consumidor.

Véanse la [referencia de contratos](../api/contracts_ref.md) y la
[guía de creación de tools](tool_creation.md).
