---
name: ai-engineer
description: Senior AI architect for LLM/RAG systems. Use for Python/Node.js AI logic, local vector DBs (Qdrant/SQLite-VSS), and autonomous agent orchestration in WSL2. Trigger when editing .py, .yaml configs, or designing RAG pipelines.
---

# AI Engineer Skill v2026.3

## Protocolo de Soberanía y Ejecución

### 1. Hard Rules (WSL2 Gateway)
- **Ejecución Protegida:** Queda prohibida la ejecución de código en el host. Toda tarea de testing o análisis debe pasar por el gateway: `bash .agents/skills/ai-engineer/scripts/wsl_sandbox_exec.sh`.
- **Pre-Redacción de PII:** Antes de cualquier `request` a modelos en la nube, el agente debe ejecutar el script local de sanitización.

## Árbol de Decisión de Arquitectura

| Escenario | Estrategia Recomendada | Herramienta Local |
| :--- | :--- | :--- |
| Latencia Crítica | Small Language Model (SLM) | Ollama / vLLM |
| Datos Privados | Local RAG + SQLite-VSS | Qdrant (Docker) |
| Flujos Complejos | Orquestación de Agentes | WebSockets + Node Gateway |

## Instrucciones de Orquestación

### Fase de Diseño (Proactiva)
Si el usuario menciona "nuevo agente" o "base vectorial", el agente debe generar automáticamente:
1. El esquema de la base de datos (SQLite/Qdrant).
2. El contrato de interfaz Pydantic.
3. El archivo `agent_config.yaml` basado en el template de Sky-Claw.

### Fase de Implementación
Utilizar el patrón de **Memoria a Largo Plazo** mediante `SemanticCache`. No re-generar contenido existente; recuperar del almacén local para optimizar el `BudgetTracker`.

## Recursos de la Skill
- `/scripts/wsl_sandbox_exec.sh`: Orquestador de comandos en WSL2.
- `/scripts/pii_redactor.py`: Limpieza dinámica de datos sensibles.
- `/examples/hybrid_rag_base.py`: Implementación de referencia para Sky-Claw.
