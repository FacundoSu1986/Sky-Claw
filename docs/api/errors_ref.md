# Referencia de excepciones

> **Audiencia:** desarrolladores y agentes.
>
> **Estado:** Implementado; inventario por fronteras, no jerarquía global.
>
> **Fuentes canónicas:** `sky_claw/app/core/errors.py` y módulos que
> declaran excepciones locales.
>
> **Última verificación:** 2026-08-16 sobre `origin/main` `32abf6f77b758c622dd71f20607589c53effeaf1`.

## Jerarquía común

`AppNexusError` es la base declarada en `core/errors.py` para:

- `FomodParserSecurityError`;
- `VaultStorageError`;
- `SecurityViolationError`;
- `AgentOrchestrationError`.

No agregar bases genéricas a esta jerarquía sin un símbolo real en
`core/errors.py`.

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

En DB lifecycle, `DatabaseLifecycleShuttingDownError` significa que no se admite
trabajo nuevo porque el shutdown ya empezó o porque un shutdown incompleto dejó
una conexión gestionada retenida. `get_connection()`, `operation()` y
`checkpoint_all()` deben propagar ese rechazo fail-closed. La reapertura no se
hace implícitamente: el caller debe reintentar `shutdown_all()` si quedó
incompleto y sólo después usar `init_all()` de forma explícita.

`DatabaseShutdownIncompleteError` describe el cierre incompleto y conserva los
paths retenidos para diagnóstico y reintento; no debe tratarse como permiso para
reutilizar esas conexiones.

## Cancelación

Algunas excepciones privadas heredan de `asyncio.CancelledError` para preservar
la semántica de cancelación aun cuando falle el cleanup. No deben transformarse
en éxito ni ocultarse bajo un `except Exception` genérico.

## Contrato de error de tools

Una excepción Python, un dict crudo de runner y un `ToolResult` normalizado son
capas distintas. Consultar [tools](tools_ref.md) antes de documentar un campo
de error.
