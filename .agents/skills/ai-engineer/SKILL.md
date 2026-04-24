---
name: ai-engineer
description: Senior AI architect for LLM/RAG systems. Use for Python/Node.js AI logic, local vector DBs (Qdrant/SQLite-VSS), and autonomous agent orchestration in WSL2. Trigger when editing .py, .yaml configs, or designing RAG pipelines.
metadata:
  version: 2.0.0
  last_updated: 2026-04-23
  compatibility:
    - Python 3.11+
    - asyncio
    - WSL2 local execution
    - Sky-Claw Ecosystem
  standards:
    - RAG v2.0
    - Semantic Caching
    - Token Budgeting (Standard 2026)
---

# AI Engineer Skill v2026.4

## Protocolo de Soberanía y Ejecución

### 1. Hard Rules (WSL2 Gateway)
- **Ejecución Protegida:** Queda prohibida la ejecución de código en el host sin sandbox cuando sea posible. Toda tarea de testing o análisis sensible debe ejecutarse en entorno aislado.
- **Pre-Redacción de PII:** Antes de cualquier `request` a modelos en la nube, sanitizar datos sensibles (emails, tokens, nombres de usuario).

## Árbol de Decisión de Arquitectura

| Escenario | Estrategia Recomendada | Herramienta Local |
| :--- | :--- | :--- |
| Latencia Crítica | Small Language Model (SLM) | Ollama / vLLM |
| Datos Privados | Local RAG + SQLite-VSS | Qdrant (Docker) o ChromaDB |
| Flujos Complejos | Orquestación de Agentes | WebSockets + Node Gateway |

## Instrucciones de Orquestación

### Fase de Diseño (Proactiva)
Si el usuario menciona "nuevo agente" o "base vectorial", el agente debe generar automáticamente:
1. El esquema de la base de datos (SQLite/Qdrant).
2. El contrato de interfaz Pydantic.
3. El archivo `agent_config.yaml` basado en el template de esta skill.

### Fase de Implementación
Utilizar el patrón de **Memoria a Largo Plazo** mediante `SemanticCache`. No re-generar contenido existente; recuperar del almacén local para optimizar el presupuesto de tokens.

## Recursos de la Skill

### Scripts Verificados
- `scripts/cost_analyzer.py`: Análisis de costos de inferencia y optimización de token budget.
- `scripts/security_audit.py`: Auditoría básica de seguridad en pipelines de IA.
- `scripts/validate_embeddings.py`: Validación de calidad y dimensionalidad de embeddings locales.

### Templates
- `templates/agent_config.yaml`: Configuración base para nuevos agentes en Sky-Claw.
- `templates/rag_pipeline.py`: Pipeline de referencia para RAG local.
- `templates/monitoring_dashboard.json`: Dashboard de métricas para observabilidad.

### Ejemplos
- `examples/basic_rag/main.py`: Implementación mínima de RAG con vector store local.
- `examples/multimodal/main.py`: Procesamiento multimodal local.

## ⚠️ Advertencias

- **No inventar scripts:** Si un recurso no está listado arriba, no existe en esta skill.
- **Soberanía de datos:** Preferir siempre ejecución 100% local. Si se requiere API cloud, documentar la exfiltración de datos.
- **Compatibilidad:** Validar que los modelos locales soporten la versión de Python del proyecto (3.11+).
