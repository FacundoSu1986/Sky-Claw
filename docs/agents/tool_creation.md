# Tutorial: Creación de un Nuevo Tool Runner

> **Audiencia:** Agentes de IA y desarrolladores que necesitan añadir una nueva herramienta externa al ecosistema Sky-Claw.
> **Pre-requisitos:** Haber leído [ARCHITECTURE.md](../../ARCHITECTURE.md) y [CONTRIBUTING.md](../../CONTRIBUTING.md).
> **Reglas de Negocio:** Si la herramienta forma parte del pipeline de modding de Skyrim, **DEBES** leer y respetar [sky_claw/local/AGENTS.md](../../sky_claw/local/AGENTS.md).

---

## 1. Visión General

Añadir un nuevo "Tool" (ej. una integración con una hipotética herramienta `SkyrimMeshOptimizer`) no es solo escribir un script que lo ejecute. Requiere integrarlo en la arquitectura asíncrona de Sky-Claw, tipar sus entradas y salidas mediante el `SchemaRegistry`, y exponerlo al Orquestador (LLM) de forma segura.

Este tutorial te guiará en la creación de un Tool Runner de principio a fin.

---

## 2. Definir los Schemas del Contrato (Inputs/Outputs)

Sky-Claw utiliza validación estricta basada en Pydantic v2. Antes de escribir lógica, define qué datos necesita tu tool y qué datos retornará.

### 2.1 Crear el Schema Pydantic

Añade tus modelos en `sky_claw/antigravity/core/schemas.py`.

```python
from pydantic import BaseModel, Field
import pathlib

class MeshOptimizerRequest(BaseModel):
    """Input para el Mesh Optimizer."""
    mod_path: pathlib.Path = Field(..., description="Ruta absoluta al directorio del mod.")
    target_polygons: int = Field(5000, ge=100, description="Límite de polígonos por mesh.")
    create_backup: bool = Field(True, description="Crear un .bak antes de sobrescribir.")

class MeshOptimizerResponse(BaseModel):
    """Output canónico del Mesh Optimizer.

    Incluye los campos base del contrato ``ToolResult`` para que el dict retornado
    pueda normalizarse con ``normalize_tool_result`` sin perder información.
    """
    success: bool = True
    message: str = ""
    return_code: int | None = 0
    warnings: list[str] = Field(default_factory=list)
    optimized_files: list[str] = Field(default_factory=list)
    skipped_files: int = 0
```

### 2.2 Registrar el Contrato

Abre `sky_claw/antigravity/core/contracts.py` y añade tu tool al diccionario `CONTRACT_SCHEMAS`. La clave debe seguir el formato `"NombreDeClase.metodo"`.

```python
CONTRACT_SCHEMAS: dict[str, dict[str, str | None]] = {
    # ... contratos existentes ...
    "MeshOptimizerService.optimize_mod": {
        "input": "MeshOptimizerRequest",
        "output": "MeshOptimizerResponse",
    },
}
```

Al hacer esto, el `SchemaRegistry` (que es un diccionario global poblado lazy al iniciar) indexará tus modelos en O(1).

---

## 3. Implementar el Service

Los Tools residen en `sky_claw/local/tools/`. Un "Service" encapsula la lógica de ejecución y manejo de errores del binario externo.

Crea `sky_claw/local/tools/mesh_optimizer_service.py`:

```python
import asyncio
import logging
import pathlib
from typing import Any

from sky_claw.antigravity.core.contracts import validate_contract
from sky_claw.antigravity.core.contracts import PathValidatorProtocol
from sky_claw.local.tools.tool_result import ToolResult, normalize_tool_result

logger = logging.getLogger(__name__)

class MeshOptimizerService:
    """Servicio asíncrono para la optimización de meshes."""

    def __init__(
        self,
        executable_path: pathlib.Path,
        path_validator: PathValidatorProtocol,
    ):
        self.executable_path = executable_path
        self.path_validator = path_validator

    @validate_contract("optimize_mod")
    async def optimize_mod(
        self, 
        mod_path: pathlib.Path, 
        target_polygons: int = 5000, 
        create_backup: bool = True
    ) -> dict[str, Any]:
        """
        Ejecuta el optimizador sobre un directorio de mod.

        ### 📋 Contrato
        - **Input:** mod_path (Path), target_polygons (int), create_backup (bool).
        - **Output:** MeshOptimizerResponse (campos canónicos + optimizados y omitidos).
        
        ### ⚠️ Failure Modes
        - `FileNotFoundError`: Si el ejecutable o el mod_path no existen.
        - `TimeoutError`: Si el proceso tarda más de 1 hora.
        - `SecurityValidationError`: Si `mod_path` escapa del sandbox autorizado.
        """
        # 1) Sandbox validation ANTES de tocar disco o lanzar subprocess.
        try:
            safe_mod_path = self.path_validator.validate(mod_path)
        except Exception as exc:
            logger.error("Ruta rechazada por el sandbox: %s", mod_path)
            raise ValueError(f"Ruta de mod inválida o fuera del sandbox: {mod_path}") from exc

        if not safe_mod_path.is_dir():
            return {"success": False, "error": f"La ruta no es un directorio: {safe_mod_path}"}

        if not self.executable_path.exists():
            # Se retorna un diccionario crudo que será normalizado y validado por el decorador
            return {"success": False, "error": f"Ejecutable no encontrado en {self.executable_path}"}

        command = [
            str(self.executable_path),
            "--target", str(target_polygons),
            "--input", str(safe_mod_path),
        ]
        if create_backup:
            command.append("--backup")

        logger.info("Iniciando Mesh Optimizer: %s", " ".join(command))

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3600)

            if process.returncode != 0:
                logger.error("Mesh Optimizer falló con código %d", process.returncode)
                return {
                    "success": False,
                    "return_code": process.returncode,
                    "message": "El proceso retornó código de error.",
                    "stderr": stderr.decode("utf-8", errors="ignore")
                }

            # Parseo simulado del output
            return {
                "success": True,
                "message": "Optimización completada exitosamente.",
                "return_code": 0,
                "optimized_files": ["meshes/armor/iron.nif"],
                "skipped_files": 2,
                "warnings": []
            }

        except asyncio.TimeoutError:
            logger.error("Timeout ejecutando Mesh Optimizer")
            if process is not None:
                process.kill()
                await process.wait()
            return {"success": False, "error": "Timeout tras 3600s"}
        except Exception as exc:
            logger.exception("Error inesperado durante la optimización")
            raise  # Re-lanzar tras loggear (Prohibido except Exception desnudo sin re-raise)
```

**Nota clave:** El método retorna un `dict` crudo. El decorador `@validate_contract` interceptará este diccionario y lo validará contra `MeshOptimizerResponse`. Además, herramientas de capa superior usarán `normalize_tool_result()` para unificar el manejo de errores.

---

## 4. Crear Tool Runners (Wrappers de Ejecución)

Si tu herramienta es compleja, es buena práctica separar la lógica baja del subprocess (Runner) de la lógica de negocio (Service).

Crea `sky_claw/local/tools/mesh_optimizer_runner.py`:

```python
import asyncio
import logging
import pathlib

logger = logging.getLogger(__name__)

async def run_mesh_optimizer_cli(
    executable: pathlib.Path, 
    args: list[str], 
    timeout: int = 3600
) -> tuple[int, str, str]:
    """Ejecuta el binario y retorna (return_code, stdout, stderr)."""
    # Implementación de bajo nivel...
```

Esto permite mockear el Runner en los tests unitarios del Service.

---

## 5. Exponer el Tool al Orquestador

Para que el Agente LLM pueda invocar tu tool, debes registrarlo en el `ToolDispatcher`.

Abre `sky_claw/antigravity/orchestrator/tool_dispatcher.py` (o el módulo donde se construya el dispatcher, usualmente una función factory como `build_default_tool_dispatcher()`) y registra tu servicio:

```python
# Dentro de la función de setup del dispatcher...

def build_default_tool_dispatcher(...) -> ToolDispatcher:
    dispatcher = ToolDispatcher()
    
    # ... registro de otros tools ...
    
    # Instanciar tu servicio (inyecta dependencias como PathValidator si requiere)
    mesh_service = MeshOptimizerService(
        executable_path=config.paths.mesh_optimizer_exe
    )
    
    # Registrar el método. El dispatcher lo expondrá al LLM.
    dispatcher.register_tool(
        name="optimize_skyrim_meshes",
        description="Optimiza los polígonos de un mod para mejorar rendimiento.",
        callable=mesh_service.optimize_mod
    )
    
    return dispatcher
```

> **¡Advertencia!** El orden de registro en `build_default_tool_dispatcher()` **NO** dicta el orden de ejecución en el Pipeline de Modding. El orden lo determina la secuencia de llamadas del LLM guiada por `sky_claw/local/AGENTS.md`. No intentes "ordenar" los registros aquí para forzar el pipeline; eso se considera un defecto arquitectónico.

---

## 6. Escribir Tests (TDD)

Crea `tests/test_mesh_optimizer_service.py`. Recuerda: **comentarios y tests en español**.

```python
import pytest
import pathlib
from unittest.mock import AsyncMock, patch
from sky_claw.local.tools.mesh_optimizer_service import MeshOptimizerService

pytestmark = pytest.mark.asyncio


class DummyPathValidator:
    """PathValidator stub que permite cualquier ruta dentro del test."""

    def validate(self, path: pathlib.Path, *, strict_symlink: bool = True) -> pathlib.Path:
        return pathlib.Path(path).resolve()


@pytest.fixture
def mock_service(tmp_path):
    exe = tmp_path / "optimizer.exe"
    exe.touch()
    validator = DummyPathValidator()
    return MeshOptimizerService(exe, validator), tmp_path


async def test_optimize_mod_exito(mock_service):
    """Verifica que un run exitoso retorna success=True y los archivos optimizados."""
    service, tmp_path = mock_service
    mod_dir = tmp_path / "mymod"
    mod_dir.mkdir()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"Success", b"")
        mock_process.returncode = 0
        mock_exec.return_value = mock_process

        resultado = await service.optimize_mod(mod_path=mod_dir, target_polygons=1000)

        assert resultado["success"] is True
        assert "optimized_files" in resultado
        assert len(resultado["optimized_files"]) > 0


async def test_optimize_mod_ejecutable_faltante(mock_service):
    """Verifica que la ausencia del binario retorna success=False y mensaje claro."""
    service, tmp_path = mock_service
    service.executable_path = tmp_path / "no_existo.exe"
    mod_dir = tmp_path / "mymod"
    mod_dir.mkdir()

    resultado = await service.optimize_mod(mod_path=mod_dir)

    assert resultado["success"] is False
    assert "Ejecutable no encontrado" in resultado.get("error", "")
```

---

## 7. Documentación Estándar del Método

Cada tool debe tener un docstring comprehensivo en su método principal, diseñado para que un LLM entienda qué hace, cómo falla y qué efectos secundarios tiene. Usa este formato:

```python
    """
    [Breve descripción de lo que hace la herramienta]

    Este texto explica el contexto y por qué existe.

    ### 📋 Contrato
    - **Input:** [Lista de argumentos y tipos]
    - **Output:** [Estructura esperada, ej. ToolResult]
    - **Side Effects:** [Qué archivos modifica en disco o VFS]

    ### ⚙️ Comportamiento
    1. [Paso 1 de la lógica interna]
    2. [Paso 2 de la lógica interna]

    ### ⚠️ Failure Modes
    - `FileNotFoundError`: [Condición]
    - `PermissionError`: [Condición común en el modding]
    - [Cualquier otro error conocido y cómo resolverlo]

    ### 🔗 Referencias
    - Pipeline Stage: [Número si aplica, según sky_claw/local/AGENTS.md]
    - Bloquea a: [Herramientas que no deben correr antes que esta]
    """
```

---

## 8. Checklist Final de Integración

- [ ] Schema Pydantic definido en `core/schemas.py`.
- [ ] Contrato registrado en `CONTRACT_SCHEMAS` (`core/contracts.py`).
- [ ] Service implementado en `local/tools/` con decorador `@validate_contract`.
- [ ] Lógica de subproceso aislada (si es compleja) en un Runner.
- [ ] Tool registrado en el `ToolDispatcher`.
- [ ] Tests unitarios pasando en `tests/` (español, TDD).
- [ ] Linters (`ruff check`, `ruff format`) y `mypy` sin errores.
- [ ] Docstring del formato estándar completado.
- [ ] Si altera el pipeline, documentado el impacto en `sky_claw/local/AGENTS.md`.
