# AGENTS.md - Directrices de Desarrollo y Contexto para Sky-Claw

Bienvenido al entorno de desarrollo de **Sky-Claw**. Eres un agente de IA que trabaja en esta base de código y debes adherirte estrictamente a estas directrices. Las reglas aquí definidas son la **constitución del proyecto** y tienen prioridad sobre cualquier otra consideración.

---

## 1. Visión General del Proyecto
Sky-Claw es un agente autónomo para la gestión de mods de Skyrim SE/AE que opera sobre el VFS de Mod Organizer 2.

| Regla | Detalle |
|-------|---------|
| **Sistema operativo** | Windows exclusivamente. NO propongas comandos, rutas ni soluciones basadas en Linux. |
| **Ejecución** | Todo corre localmente. Node.js es estrictamente para el Gateway local. No hay integraciones con Vercel ni despliegues en la nube. |
| **Herramientas** | `uv` para dependencias Python, VS Code, PowerShell. |
| **Modding** | Las herramientas externas (Pandora, BodySlide, xEdit, LOOT) asumen el contexto de ejecución de MO2. |

---

## 2. Protocolo de Razonamiento Metacognitivo
Antes de escribir o modificar código, estructura tu pensamiento con el formato **LÓGICA / ARQUITECTURA / PREVENCIÓN** y sigue este protocolo de 5 fases:

1. **DESCOMPONER:** Divide el requerimiento en subproblemas lógicos.
2. **RESOLVER:** Aborda cada subproblema con un nivel de confianza explícito (0.0 - 1.0).
3. **VERIFICAR:** Revisa la lógica, el manejo de errores, las dependencias y los cuellos de botella. **Si tu confianza es < 0.8 en algún paso, descarta la ruta (Backtracking) y propón una "Rama Maestra" más estable.**
4. **SINTETIZAR:** Combina los resultados en una solución final eficiente.
5. **REFLEXIONAR:** Si la confianza global es < 0.8, explica la inviabilidad y propón una alternativa conservadora.

---

## 3. Patrones Arquitectónicos y Código

### 3.1 Capas y Separación de Responsabilidades
Respeta estrictamente la arquitectura en capas:
`scraper/ → core/ → agent/ → comms/ → frontend/`
`↓`
`db/ security/ mo2/ tools/ orchestrator/`

- **No mezcles lógica de negocio con infraestructura.** La construcción de dependencias está centralizada en `SupervisorContext`. Los servicios reciben sus dependencias por constructor.
- **Inyección de dependencias:** Todo servicio debe aceptar sus colaboradores en `__init__`. No instancies dependencias dentro de métodos de negocio.

### 3.2 Tipado y Validación
```python
from __future__ import annotations
from typing import TYPE_CHECKING
```
Usa tipado estricto siempre. Si una función puede devolver None, decláralo.Los contratos entre agentes y módulos se validan con Pydantic v2 a través del SchemaRegistry (lookup O(1)).Los dataclasses de configuración deben ser frozen=True, slots=True.

### 3.3 Manejo de Excepciones
**PROHIBIDO:**
```python
# ❌ NUNCA hagas esto
except Exception as exc:
    logger.exception("Error: %s", exc)
    return "Error amigable"
```
**OBLIGATORIO:** Usa la jerarquía de excepciones del proyecto o excepciones específicas:
```python
# ✅ Correcto
except (MasterlistFetchError, CircuitOpenError, aiohttp.ClientError) as exc:
    logger.warning("Skipping mod %r: %s", mod_name, exc)
    result.failed += 1
```
`except Exception` enmascara `RuntimeError`, `AssertionError` y `RecursionError`, haciendo que bugs graves pasen silenciosamente. Nunca lo uses en código de producción.Para `asyncio.TaskGroup`, captura `except* KeyboardInterrupt` separadamente de `except* Exception`.

### 3.4 Logging
```python
logger = logging.getLogger(__name__)
```
Usa siempre el logger del módulo. Incluye `extra={}` estructurado cuando registres eventos de negocio.

---

## 4. Asincronismo y No Bloqueo del Event Loop
**REGLAS INNEGOCIABLES:**

| Prohibido | Alternativa |
|---|---|
| `time.sleep()` | `await asyncio.sleep()` |
| `requests.get()` | `aiohttp.ClientSession` |
| `subprocess.run()` síncrono | `await asyncio.create_subprocess_exec()` |
| `open().read()` bloqueante | `aiofiles.open()` o `await asyncio.to_thread()` |
| `pathlib.Path.resolve()` en hot path | `await asyncio.to_thread(path.resolve)` |

Las operaciones de filesystem que tocan disco (`resolve()`, `stat()`, `unlink()`) deben delegarse a `asyncio.to_thread()` cuando se ejecutan en el event loop principal.
Usa `asyncio.Semaphore` para limitar concurrencia de I/O de red y disco.
`asyncio.gather(*tasks)` con más de 100 corrutinas debe reemplazarse por procesamiento por lotes (batch processing) para evitar agotamiento de memoria y file descriptors.

---

## 5. Seguridad y Guardrails (Zero-Trust)

### 5.1 LLM Guardrail
Toda interacción con LLMs pasa por `AgentGuardrail`, un middleware stateless y fail-closed que:
- Detecta prompt-injection (OWASP LLM01) y PII antes de enviar al modelo.
- Valida esquemas de salida con Pydantic v2.
- Detecta fugas de rutas absolutas en las respuestas del modelo.
- No introduzcas dependencias circulares con `sky_claw.agent`.

### 5.2 Rutas de Archivos
- Usa `pathlib.Path` para todas las rutas.
- Toda ruta dinámica debe validarse con `PathValidator.validate()` contra las raíces permitidas.
- Nunca expongas rutas absolutas del sistema en las salidas del LLM.

### 5.3 Operaciones de Archivos
- `AssetConflictDetector` y herramientas de análisis son STRICTLY READ-ONLY.
- Para modificaciones, usa `FileSnapshotManager` + `RollbackManager` (patrón Unit of Work con copy-on-write atómico).
- Toda operación destructiva requiere snapshots previos y capacidad de rollback.

### 5.4 Secretos
- API keys, tokens y credenciales van en OS keyring (Windows Credential Manager) o en `CredentialVault` (Fernet + PBKDF2).
- NUNCA en texto plano en TOML, variables de entorno sin cifrar, ni hardcodeadas en el código.
- El `Config.save()` migra automáticamente secretos al keyring y los elimina del TOML.

---

## 6. Inyección de Dependencias y SupervisorContext
Desde la refactorización ARC-001, el `SupervisorAgent` es un orquestador puro que no instancia dependencias. El cableado de servicios se centraliza en `SupervisorContext`:

```python
# ✅ Patrón correcto
class SupervisorAgent:
    def __init__(self, ctx: SupervisorContext):
        self._ctx = ctx
```

| Regla | Detalle |
|---|---|
| **Construcción** | Las dependencias se crean en `build_supervisor_context()`. No las crees en `SupervisorAgent`. |
| **Acceso a servicios** | Usa propiedades públicas (`self.synthesis_service`), NO atributos privados (`self._synthesis_service`). |
| **Testing** | Los tests deben inyectar mocks vía `SupervisorContext`, no usar `__new__` ni monkey-patching. |
| **Daemons** | Los componentes con lifecycle implementan el protocolo `Daemon` (`core/lifecycle.py`). |

---

## 7. Tests

### 7.1 Reglas obligatorias
- Framework: `pytest` + `pytest-asyncio`.
- Usa `conftest.py` para fixtures compartidas. No dupliques mocks entre archivos.
- Nunca uses `asyncio.sleep()` para sincronización en tests. Usa `asyncio.Event`, `asyncio.Condition` o `asyncio.wait_for()` con timeout.
- Los archivos temporales deben usar `tmp_path`. No escribas en el filesystem real.
- Todos los tests existentes deben pasar antes de mergear un PR.

### 7.2 Cobertura
- Cobertura mínima CI: 49% (objetivo: > 80%).
- Módulos sin tests: `gui.*`, `reasoning.*`, `discovery.*`, `modes.*` deben tener al menos tests de humo.

---

## 8. Herramientas de Calidad de Código

| Herramienta | Uso |
|---|---|
| `ruff` | Linting + formateo. Ejecutar `ruff check sky_claw/` y `ruff format --check sky_claw/` antes de commit. En VS Code, configurar Ruff como formateador por defecto. |
| `mypy` | Tipado estático. Módulos críticos (`security/*`, `core/database.py`, `db/*`) deben pasar con `ignore_errors = false`. |
| `bandit` | SAST. Se ejecuta en CI. No debe introducir hallazgos nuevos de severidad HIGH o CRITICAL. |

---

## 9. Integración Multi-Agente
- **Comunicación:** GUI y Daemon se comunican vía WebSockets asíncronos mediante un `EventBus`. Los eventos usan los tópicos `skyclaw-telemetry` y `skyclaw-message`.
- **Orquestación:** Soporta degradación elegante. Si autogen no está disponible, el sistema funciona en modo stub.
- **HITL (Human-in-the-loop):** Toda operación que requiera aprobación humana pasa por `HITLGuard`. No se ejecutan acciones destructivas sin confirmación del operador vía Telegram.

---

## 10. Resolución de Errores
Si encuentras un error o el usuario reporta una falla:
1. Realiza un Análisis de Causa Raíz.
2. Identifica el mínimo cambio necesario para corregirlo.
3. Devuelve ÚNICAMENTE el diff mínimo (sin reescribir funciones enteras innecesariamente).
4. Verifica que los tests existentes sigan pasando.

---

## 11. Restricciones de PRs
- Cada PR debe ser atómico y abordar un solo problema.
- No mezcles refactorización con nuevas funcionalidades en el mismo PR.
- Si tu cambio afecta a `SupervisorAgent` o `SupervisorContext`, verifica que `pytest tests/ -x --timeout=60` pase con 0 failures.
- Los PRs que introducen nuevas dependencias externas requieren justificación explícita.
