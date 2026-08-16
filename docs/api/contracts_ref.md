# Referencia de contratos y schemas

> **Audiencia:** desarrolladores y agentes de IA.
> **Estado:** referencia estática del código implementado.
> **Fuentes canónicas:** `core/contracts.py`, `core/schemas.py`,
> `local/tools/tool_result.py` y `core/errors.py`.
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
> **Sincronización de contratos P1:** 2026-08-16 sobre `main`.

Esta página sirve para orientación. Ante cualquier diferencia, las clases y
tests del árbol actual dominan sobre esta copia documental.

## `ToolResult`

`sky_claw/local/tools/tool_result.py` define la vista normalizada:

| Campo | Tipo | Regla |
|---|---|---|
| `success` | `bool` | Sólo un `bool` real es señal canónica; si falta, se evalúa `status == "success"`. |
| `message` | `str` | En fallo cae a `details`, `error`, `logs`, `stderr`, `errors` o `reason`. |
| `return_code` | `int \| None` | Sólo se conserva cuando el valor crudo es entero. |
| `warnings` | `list[str]` | Se normalizan listas o tuplas; cualquier otro shape produce lista vacía. |

Los servicios nuevos deben emitir `success` y `message`. Las claves legacy se
aceptan para compatibilidad, pero ningún consumidor debe reimplementar su orden.

## Registro `CONTRACT_SCHEMAS`

| Clave | Input | Output |
|---|---|---|
| `SupervisorAgent.dispatch_tool` | `AgentToolRequest` | `AgentToolResponse` |
| `ScraperAgent.query_nexus` | `ScrapingQuery` | sin schema Pydantic |
| `DatabaseAgent.get_mods` | sin schema Pydantic | sin schema Pydantic |
| `PurpleSecurityAgent.audit_local_file` | `SecurityAuditRequest` | `SecurityAuditResponse` |
| `LLMRouter.chat` | sin schema Pydantic | sin schema Pydantic |

El registro se puebla de forma lazy desde `core/schemas.py`. Un método no
registrado opera en pass-through; esto no equivale a que toda llamada de tools
del sistema use estos decoradores.

La clave efectiva se construye con `self.__class__.__name__` más el nombre del
método. Renombrar una clase, sustituirla por otra o mover un contrato sin
actualizar `CONTRACT_SCHEMAS` puede dejar la validación en pass-through aunque el
schema siga existiendo. Para cambios de este tipo deben revisarse mapping,
decorador aplicado y tests del caller; no basta con comprobar que la clase
Pydantic todavía importa.

## Schemas críticos

### `AgentToolRequest`

| Campo | Tipo | Default/restricción |
|---|---|---|
| `tool_name` | `str` | obligatorio, mínimo un carácter |
| `parameters` | `dict[str, Any]` | diccionario vacío |
| `priority` | `low \| medium \| high \| critical` | `medium` |
| `requires_confirmation` | `bool` | `False` |
| `timeout_seconds` | `int` | `30`, mayor que cero |
| `created_at` | `datetime` | UTC actual |

### `AgentToolResponse`

| Campo | Tipo | Default |
|---|---|---|
| `tool_name` | `str` | obligatorio |
| `result` | `dict[str, Any] \| None` | `None` |
| `success` | `bool` | obligatorio |
| `error` | `str \| None` | `None` |
| `execution_time_ms` | `float \| None` | `None` |
| `created_at` | `datetime` | UTC actual |

### `SecurityAuditRequest`

| Campo | Tipo | Default/restricción |
|---|---|---|
| `target_path` | `str` | obligatorio; mínimo un carácter; validator anti-traversal |
| `audit_type` | `file \| repository \| directory` | `file` |
| `depth` | `int` | `1`, entre 1 y 5 |
| `include_vectors` | `bool` | `True` |

### `SecurityAuditResponse`

| Campo | Tipo | Default/restricción |
|---|---|---|
| `target` | `str` | obligatorio |
| `findings` | `list[dict[str, Any]]` | obligatorio |
| `risk_score` | `float` | entre 0.0 y 1.0 |
| `recommendations` | `list[str]` | obligatorio |
| `audited_at` | `datetime` | UTC actual |

Estos cuatro modelos usan `ConfigDict(extra="forbid", strict=True)`. No
extrapolar esa regla a todos los modelos de `core/schemas.py`: por ejemplo, el
runtime contiene modelos con otra política de `extra` para fronteras concretas.

## Protocols

- `DownloadQueue.enqueue_download(coro, context="unknown")` devuelve una
  `asyncio.Task`.
- `PathValidatorProtocol.validate(path, *, strict_symlink=True)` devuelve un
  `pathlib.Path` validado.

Son interfaces de inversión de dependencias; no describen todos los métodos de
las implementaciones concretas.

## Jerarquía `AppNexusError`

`core/errors.py` expone actualmente:

- `AppNexusError`
  - `FomodParserSecurityError`
  - `VaultStorageError`
  - `SecurityViolationError`
  - `AgentOrchestrationError`

Otras excepciones específicas viven junto a su subsistema y no deben
presentarse como subclases de `AppNexusError` sin verificarlo en el código.
Consultar [errors_ref.md](errors_ref.md) para familias locales, especialmente
lifecycle DB, journal, locks, VFS y runners.

## Regla para agentes

No inferir un contrato de ejecución de tools a partir de `CONTRACT_SCHEMAS`.
Para una tool LLM, verificar `ToolDescriptor.params_model`; para una strategy,
verificar su payload/modelo y middleware; para resultados crudos, normalizar
antes de imponer la vista común. Si las capas difieren, registrar
`DOCUMENTATION_DRIFT` antes de tocar producción.

## Fuentes de regresión

- `tests/test_contracts_ticket_1_1.py`
- `tests/test_tool_result.py`
- `tests/test_tool_result_contract.py`
- tests específicos de `core/schemas.py` y del agente de seguridad
