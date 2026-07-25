# Referencia de Contratos y Schemas

> **Audiencia:** Agentes de IA y desarrolladores que necesitan conocer la estructura exacta de los datos que fluyen entre los componentes de Sky-Claw.
> **Mecanismo de Validación:** Ver [schema_validation.md](../agents/schema_validation.md) para comprender cómo se aplican estos contratos en tiempo de ejecución.

---

## 1. Contrato Canónico de Tools (`ToolResult`)

Todas las herramientas del sistema, independientemente de sus schemas Pydantic individuales, deben retornar un diccionario que pueda ser normalizado por `sky_claw.local.tools.tool_result.normalize_tool_result()` a la siguiente estructura:

```python
class ToolResult(TypedDict):
    success: bool
    message: str
    return_code: int | None
    warnings: list[str]
```

### 1.1 Reglas de Normalización

- **`success`**: Solo un `bool` real (`True`/`False`) en `raw["success"]` cuenta como señal. Si no existe o es string, cae a `raw["status"] == "success"`.
- **`message`**: 
  - En **ÉXITO**, si `message` está presente, se respeta (incluso si es vacío).
  - En **FALLO**, si `message` está vacío, la función intenta extraer el detalle de claves legacy en este orden: `details` ➔ `error` ➔ `logs` ➔ `stderr` ➔ `errors` (lista unida) ➔ `reason`.
  - En fallo sin dato alguno: `"error desconocido"` (único sitio del sistema donde se origina este texto).
- **`return_code`**: Entero con el código de salida del subproceso, o `None`.
- **`warnings`**: Lista de strings. Si el input crudo tiene `warnings` (lista o tupla), se castea a `list[str]`.

> **Advertencia:** Todo consumidor de un tool debe invocar `normalize_tool_result(raw)` en lugar de inspeccionar el diccionario crudo directamente.

---

## 2. Contrato de Protocolos de Infraestructura

Interfaces definidas en `sky_claw/antigravity/core/contracts.py` bajo el patrón de Inversión de Dependencias (Dependency Inversion).

### 2.1 `DownloadQueue` (LAY-01)
Protocolo para encolar corrutinas de descarga. Desacopla la capa del Agente del Orquestador (`SyncEngine`).

```python
class DownloadQueue(Protocol):
    def enqueue_download(
        self,
        coro: Coroutine[Any, Any, Any],
        context: str = "unknown",
    ) -> asyncio.Task[Any]: ...
```

### 2.2 `PathValidatorProtocol` (LAY-03)
Protocolo para validación de rutas contra un sandbox. Desacopla el Core de la Capa de Seguridad.

```python
class PathValidatorProtocol(Protocol):
    def validate(
        self,
        path: str | pathlib.Path,
        *,
        strict_symlink: bool = True,
    ) -> pathlib.Path: ...
```

---

## 3. Catálogo de Contratos de Agentes (`CONTRACT_SCHEMAS`)

El diccionario global `CONTRACT_SCHEMAS` mapea métodos de agentes a sus respectivos schemas Pydantic de Input y Output. Estos schemas se resuelven en O(1) desde el `SchemaRegistry` después de su primera inicialización lazy (la primera invocación carga e indexa todos los modelos de `core/schemas.py`; ver `contracts.py:_ensure_registry`).

*(Nota: Esta es una referencia estática. Los schemas exactos pueden expandirse. Consultar `sky_claw/antigravity/core/contracts.py` para la lista actualizada).*

### 3.1 SupervisorAgent

| Método | Input Schema | Output Schema | Descripción |
| :--- | :--- | :--- | :--- |
| `dispatch_tool` | `AgentToolRequest` | `AgentToolResponse` | Despacha una solicitud de ejecución de tool hacia el dispatcher. |

### 3.2 ScraperAgent

| Método | Input Schema | Output Schema | Descripción |
| :--- | :--- | :--- | :--- |
| `query_nexus` | `ScrapingQuery` | *(Sin validar)* | Ejecuta una consulta de scraping contra NexusMods. Requiere Playwright. |

### 3.3 SecurityAgent

| Método | Input Schema | Output Schema | Descripción |
| :--- | :--- | :--- | :--- |
| `audit_local_file` | `SecurityAuditRequest` | `SecurityAuditResponse` | Audita la integridad y permisos de un archivo dentro del sandbox. |

### 3.4 DatabaseAgent

| Método | Input Schema | Output Schema | Descripción |
| :--- | :--- | :--- | :--- |
| `get_mods` | *(Sin validar)* | *(Sin validar)* | Recupera listados de mods de la base de datos SQLite (WAL). |

### 3.5 LLMRouter

| Método | Input Schema | Output Schema | Descripción |
| :--- | :--- | :--- | :--- |
| `chat` | *(Sin validar)* | *(Sin validar)* | Interfaz unificada de mensajería hacia los proveedores LLM. |

---

## 4. Definiciones de Schemas Pydantic

Los modelos concretos residen en `sky_claw/antigravity/core/schemas.py`. A continuación se detallan los más críticos para la operación del sistema.

### 4.1 AgentToolRequest
Estructura que el LLM genera cuando decide invocar una herramienta.

```python
class AgentToolRequest(BaseModel):
    tool_name: str = Field(..., description="Nombre del tool registrado en el dispatcher")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Kwargs a inyectar en el método")
    context_id: str | None = Field(None, description="ID de trazabilidad de la sesión")
```

### 4.2 AgentToolResponse
Estructura que el orquestador retorna al LLM tras la ejecución.

```python
class AgentToolResponse(BaseModel):
    success: bool
    message: str
    data: dict[str, Any] | None = None
    error_details: str | None = None
```

### 4.3 SecurityAuditRequest
Requerimiento para validar un archivo local.

```python
from pydantic import BaseModel, Field
import pathlib

class SecurityAuditRequest(BaseModel):
    file_path: pathlib.Path = Field(..., description="Ruta absoluta al archivo a auditar.")
    check_hashes: bool = Field(False, description="Verificar integridad con hashes.")
    max_size_mb: int = Field(500, ge=1, le=5000, description="Tamaño máximo permitido (MB).")
```

### 4.4 SecurityAuditResponse
Resultado de la auditoría de seguridad.

```python
class SecurityAuditResponse(BaseModel):
    is_safe: bool
    reason: str | None = None
    sha256_hash: str | None = None
    file_size_mb: float
```

---

## 5. Convención de Errores (`AppNexusError`)

Todas las excepciones controladas del sistema heredan de la jerarquía `AppNexusError` definida en `sky_claw/antigravity/core/errors.py`.

Se prohíbe el uso de `except Exception` desnudo. Las excepciones desconocidas deben loguearse y re-lanzarse. Los bloques `except` deben capturar errores específicos de esta jerarquía siempre que sea posible.

| Clase | Uso Previsto |
| :--- | :--- |
| `AppNexusError` | Clase base para todas las excepciones de dominio. |
| `ConfigurationError` | Fallos al cargar TOML, variables de entorno o rutas inválidas. |
| `DatabaseError` | Rollbacks, corrupción de SQLite, queries inválidas. |
| `SecurityValidationError` | Intentos de escape de sandbox (TOCTOU), rutas fuera de `SystemPaths`. |
| `ToolExecutionError` | Fallos de subprocesos, timeouts, o códigos de retorno no-cero. |
| `LLMProviderError` | Timeouts de API, Rate Limits, respuestas JSON inválidas del LLM. |
