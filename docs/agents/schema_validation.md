# Validación de Esquemas y Contratos

> **Audiencia:** Agentes de IA y desarrolladores que necesitan comprender o extender el sistema de validación de inputs/outputs de Sky-Claw.
> **Referencia Cruzada:** Ver [tool_creation.md](tool_creation.md) para la aplicación práctica de estos conceptos.

---

## 1. Filosofía de Diseño: "Confianza Cero en el LLM"

Los Modelos de Lenguaje (LLMs) son inherentemente estocásticos. Pueden alucinar parámetros, omitir campos obligatorios o retornar JSON malformado. En Sky-Claw, **ningún output del LLM se considera confiable por defecto**.

Para mitigar esto, el sistema implementa una capa de validación estricta basada en **Pydantic v2** que intercepta todas las llamadas entre la capa de Orquestación (el Agente) y la capa de Dominio (los Tools).

### 1.1 Reglas Inmutables de los Contratos
1.  **Prohibido el parseo libre:** No se permite usar expresiones regulares (`re`) o manipulación manual de strings para extraer datos de un response LLM. Todo debe pasar por `model_validate_json` o `model_validate`.
2.  **Validación Bidireccional:** Se valida tanto lo que *entra* al Tool (Input) como lo que *sale* de él (Output).
3.  **Lookup O(1) tras la primera llamada:** Los esquemas se resuelven en tiempo de ejecución desde un registro global en memoria (`SchemaRegistry`). La primera invocación por proceso paga una exploración de módulo (`importlib.import_module` + escaneo de `dir()`, O(N) con N = número de schemas); toda llamada posterior es una búsqueda directa en el diccionario poblado (O(1) sin lock).

---

## 2. Anatomía del Sistema de Contratos

La lógica central reside en `sky_claw/antigravity/core/contracts.py`.

### 2.1 El SchemaRegistry

Es un diccionario de módulo (`_SCHEMA_REGISTRY: dict[str, type[BaseModel]]`) que mapea el nombre de una clase (string) a su implementación Pydantic (`Type[BaseModel]`).

- **Inicialización Lazy:** Para evitar importaciones circulares durante el arranque, el registry se pobla la primera vez que un decorador de contrato lo necesita (función `_ensure_registry()`).
- **Thread-Safe:** Utiliza un lock (`threading.Lock`) con patrón *double-checked locking* para asegurar que en entornos concurrentes el diccionario no se pueble dos veces ni sea leído a medio inicializar.
- **Fuente:** Escanea dinámicamente `sky_claw.antigravity.core.schemas`.

### 2.2 Registro de Contratos (`CONTRACT_SCHEMAS`)

Un diccionario estático que mapea `"NombreDeClase.metodo"` a los nombres de los esquemas Pydantic de Input y Output:

```python
CONTRACT_SCHEMAS: dict[str, dict[str, str | None]] = {
    "SupervisorAgent.dispatch_tool": {
        "input": "AgentToolRequest",
        "output": "AgentToolResponse",
    },
    "DatabaseAgent.get_mods": {
        "input": None,  # Sin validación estricta
        "output": None,
    },
    # ...
}
```

Si un método no está listado aquí, los decoradores actuarán en modo *pass-through* (dejan pasar los datos sin validar).

---

## 3. Decoradores de Validación

Se proveen tres decoradores para aplicar a los métodos de los servicios.

### 3.1 `@validate_input(method_name: str)`

Valida los argumentos de entrada (`kwargs` o un diccionario posicional) contra el esquema `input` definido en `CONTRACT_SCHEMAS`.

**Comportamiento:**
1.  Construye la clave `<Clase>.<metodo>` en runtime.
2.  Busca el esquema en el `SchemaRegistry`.
3.  Ejecuta `schema_cls.model_validate(data)`.
4.  Si la validación falla, envuelve el `ValidationError` de Pydantic en un `ValueError` con contexto legible y lo lanza.
5.  Si pasa, inyecta los datos saneados (`validated.model_dump()`) a la función original.

### 3.2 `@validate_output(method_name: str)`

Valida el valor de retorno de la función contra el esquema `output`.

**Comportamiento:**
1.  Ejecuta la función original.
2.  Si el resultado es un `dict` o un `BaseModel`, intenta validarlo con el esquema registrado.
3.  Si el resultado es de otro tipo (ej. `list`, `str`), lo deja pasar (asumiendo que el esquema no aplica o es un tipo primitivo).
4.  Si falla, lanza `ValueError`. Si pasa, retorna el diccionario saneado.

### 3.3 `@validate_contract(method_name: str)`

Es el decorador recomendado. Combina la validación de entrada y salida en una sola pasada. Apilar `@validate_input` y `@validate_output` no ejecutaría el método subyacente dos veces (cada decorador delega a `func` una sola vez), pero duplicaría la lógica de los wrappers y las transformaciones de datos; `@validate_contract` evita esa duplicación y mantiene el método una sola ejecución.

**Fases Internas:**
1.  **Fase 1:** Valida Input.
2.  **Fase 2:** Ejecuta la función (una sola vez) con los argumentos saneados.
3.  **Fase 3:** Valida Output.

```python
from sky_claw.antigravity.core.contracts import validate_contract

class XEditService:
    
    @validate_contract("run_quick_auto_clean")
    async def run_quick_auto_clean(self, plugin_name: str, timeout: int = 300) -> dict:
        #Implementación...
        return {"success": True, "message": "Limpieza completada"}
```

---

## 4. Definición de Esquemas Pydantic

Los esquemas deben crearse en `sky_claw/antigravity/core/schemas.py`.

### 4.1 Buenas Prácticas para Esquemas
- **Descriptividad:** Usa `Field(..., description="...")` para que el LLM (a través del Tool Dispatcher) entienda qué se espera.
- **Validaciones Estrictas:** Utiliza las restricciones de Pydantic (`ge`, `le`, `pattern`, `min_length`) para rechazar datos absurdos antes de que lleguen al dominio.
- **Tipado Fuerte:** Evita `Any`. Prefiere `pathlib.Path`, `int`, `bool`, Enums, o `Literal`.
- **Nombres Únicos:** El nombre de la clase Pydantic debe ser exactamente el que se registra en `CONTRACT_SCHEMAS`.

```python
from pydantic import BaseModel, Field
import pathlib

class SecurityAuditRequest(BaseModel):
    """Solicitud de auditoría de archivo local."""
    file_path: pathlib.Path = Field(..., description="Ruta absoluta al archivo a auditar.")
    check_hashes: bool = Field(False, description="Verificar integridad con hashes.")
    max_size_mb: int = Field(500, ge=1, le=5000, description="Tamaño máximo permitido.")
```

---

## 5. Utilidades de Introspección y Testing

El módulo `contracts.py` expone varias funciones para testing o tooling:

- `get_contract_schema(agent_method: str) -> dict`: Retorna los nombres de esquema para un método (ej. `{"input": "X", "output": "Y"}`).
- `get_schema_class(name: str) -> type[BaseModel] | None`: Obtiene la clase Pydantic real para inspección.
- `verify_contract(agent_class: type, method_name: str) -> bool`: Retorna `True` si el método tiene un decorador aplicado (verifica la existencia de `__wrapped__`).
- `list_registered_schemas() -> dict[str, type[BaseModel]]`: Retorna una copia completa del registry poblado.

### 5.1 Testear un Contrato

Los tests deben verificar que los contratos se respetan. Un patrón común es intentar enviar datos inválidos y asegurar que se lance un `ValueError`.

```python
async def test_xedit_validacion_falla_sin_plugin():
    """Verifica que la ausencia de plugin_name lanza ValueError por contrato."""
    service = XEditService()
    
    with pytest.raises(ValueError, match="Entrada inválida"):
        # Falta el argumento requerido 'plugin_name'
        await service.run_quick_auto_clean() 
```

---

## 6. Integración con `ToolResult`

El contrato final de *todo* Tool Runner, independientemente de sus schemas Pydantic específicos, debe poder normalizarse a `ToolResult` (ver `sky_claw/local/tools/tool_result.py`).

Esto significa que, en caso de error no controlado por el schema, el diccionario retornado debe contener al menos `{"success": False, "message": "..."}` o claves legacy (`error`, `stderr`, `logs`) que `normalize_tool_result` sepa interpretar.

El sistema de validación de contratos atrapa errores de *forma* (datos faltantes/tipo incorrecto). La normalización `ToolResult` atrapa errores de *lógica de negocio* (ej. el ejecutable externo falló con código 1). Ambos sistemas son complementarios y obligatorios.
