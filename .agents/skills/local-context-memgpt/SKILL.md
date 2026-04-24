---
name: local-context-memgpt
description: Implementa MemGPT como sistema de memoria a largo plazo local-first para agentes Sky-Claw. Usar cuando la ventana de contexto se exceda, se necesite recordar historial de mods entre sesiones, o mantener preferencias persistentes. No usar para consultas simples sin necesidad de memoria persistente.
---

# Local Context MemGPT

Sistema de memoria a largo plazo local-first usando MemGPT para agentes Sky-Claw. Garantiza la soberanía de datos mediante procesamiento 100% local.

## Cuándo Usar

| Escenario | Prioridad | Justificación |
|-----------|-----------|---------------|
| Recordar historial de mods entre sesiones | 🔴 Alta | Continuidad operacional |
| Contexto excede ventana del modelo (500+ mods) | 🔴 Alta | Limitación técnica del LLM |
| Mantener preferencias y reglas complejas del usuario | 🟠 Media | Personalización persistente |
| Búsqueda semántica sobre historial de mods | 🟠 Media | Recuperación inteligente |

## Cuándo NO Usar

- Para consultas simples que no requieran memoria entre sesiones.
- Cuando los datos caben cómodamente en la ventana de contexto del modelo.
- Para tareas de un solo uso sin necesidad de persistencia.

## Instrucciones

### 1. Configuración del Motor
```bash
pip install memgpt
memgpt configure  # Seleccionar SQLite local
```

### 2. Integración con Sky-Claw
```python
from sky_claw.agent.specialized_bridges import MemGPTBridge

# Inicializar sesión de agente
bridge = MemGPTBridge(config=local_config)
session = await bridge.initialize_session(user_id="tg-user-123")
```

### 3. Operaciones de Memoria
```python
# Ingerir metadatos de nuevo mod
await session.ingest(mod_metadata_text)

# Búsqueda semántica sobre historial
results = await session.retrieve(query="conflictos con alternadores")
```

### 4. Reglas de Oro
- **Soberanía Local:** Nunca deshabilitar el modo local-only en la configuración.
- **Actualizaciones Incrementales:** Sincronizar memoria después de cada instalación exitosa o resolución de conflicto.
- **Separación de Memoria:**
  - `archival_memory` → Almacenamiento a largo plazo (historial completo).
  - `core_memory` → Estado inmediato del proyecto activo.

## Recursos

- Integración principal en `sky_claw/agent/specialized_bridges.py`.
