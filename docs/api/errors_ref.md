# Referencia de excepciones

> **Audiencia:** desarrolladores y agentes.
>
> **Estado:** Implementado; inventario por fronteras, no jerarquía global.
>
> **Fuentes canónicas:** `sky_claw/app/core/errors.py` y módulos que
> declaran excepciones locales.
>
> **Última verificación integral:** 2026-07-25 sobre `origin/main` `c6ab35e`.
>
> **Sincronización DB/lifecycle:** 2026-08-16 sobre `e48306c`.

## Jerarquía común

`AppNexusError` es la base declarada en `core/errors.py` para:

- `FomodParserSecurityError`;
- `VaultStorageError`;
- `SecurityViolationError`;
- `AgentOrchestrationError`.

No agregar bases genéricas a esta jerarquía sin un símbolo real en
`core/errors.py`. El docstring de `AppNexusError` no convierte automáticamente
en subclases suyas a las excepciones declaradas en otros módulos.

## Fronteras con familias propias

| Frontera | Bases o errores representativos |
|---|---|
| DB lifecycle | `DatabaseLifecycleShuttingDownError`, `DatabaseShutdownIncompleteError` |
| Registry async | `DatabaseError` |
| Journal | `JournalError` y subclases |
| Locks/snapshots | `LockError`, `LockAcquisitionError`, `LockLeaseLostError`, `SnapshotRollbackError` |
| Path/egress | `PathViolationError`, `EgressViolationError`, `NetworkGatewayTimeoutError` |
| Proveedores | `ProviderConfigError` |
| Dispatcher | `DuplicateToolError`; missing tool retorna dict legacy |
| Profile sandbox | `ProfileSandboxError`, `SandboxDriftError`, `SandboxRollbackError` |
| Broker VFS | `VfsBrokerError` y subclases de conexión, launch, timeout y validación |
| Runners | familias específicas de xEdit, DynDOLOD, Synthesis, Wrye Bash, Pandora y BodySlide |

No todas heredan de `AppNexusError`. Capturar esa base no cubre fallos de todas
las fronteras.

### DB lifecycle

`DatabaseLifecycleShuttingDownError` significa que el manager dejó de admitir
trabajo porque el shutdown ya empezó. Es un rechazo fail-closed: no debe
"recuperarse" abriendo una conexión SQLite por fuera del lifecycle ni
reintentando `get_connection()` en bucle. La reapertura es explícita mediante
`init_all()` cuando el estado lo permite.

`DatabaseShutdownIncompleteError` significa que el cierre físico no terminó y
quedaron conexiones gestionadas retenidas para un reintento posterior de
`shutdown_all()`. No equivale a CLOSED ni autoriza a descartar ownership.

## Cancelación

Algunas excepciones privadas heredan de `asyncio.CancelledError` para preservar
la semántica de cancelación aun cuando falle el cleanup. No deben transformarse
en éxito ni ocultarse bajo un `except Exception` genérico.

Para código de cleanup, la excepción primaria/cancelación debe seguir siendo la
referencia semántica. Si un cleanup secundario falla, no asumir que reemplazar la
causa original es correcto: verificar el contrato concreto del subsistema y sus
tests de cancelación.

## Contrato de error de tools

Una excepción Python, un dict crudo de runner y un `ToolResult` normalizado son
capas distintas. Consultar [tools](tools_ref.md) antes de documentar un campo
de error.

## Regla para agentes

Si una guía antigua enumera una familia de excepciones incompleta, registrar
`DOCUMENTATION_DRIFT` y verificar la clase real antes de cambiar un `except`, un
retry o una política fail-open/fail-closed. La referencia documental no debe
usarse para eliminar una excepción vigente del runtime.
