---
name: llm-application-dev-langchain-agent
description: Provides expert guidance for developing production-grade AI systems using LangChain 0.1+ and LangGraph. Focuses on async patterns, observability, and specialized RAG architectures.
metadata:
  version: 2.0.0
  last_updated: 2026-04-23
  compatibility:
    - Python 3.11+
    - LangChain 0.1+
    - LangGraph 0.1+
---

# Experto en Desarrollo de Agentes con LangChain/LangGraph

Esta skill proporciona patrones comprehensivos y mejores prácticas para construir sistemas de agentes de IA sofisticados usando el ecosistema más reciente de LangChain.

## Cuándo usar esta skill

- Diseñar o implementar agentes con LangChain/LangGraph.
- Optimizar pipelines de RAG con embeddings especializados.
- Implementar orquestación multi-agente o enrutamiento basado en supervisor.
- Transicionar prototipos a producción con observabilidad y manejo de errores.

## Árbol de Decisiones: Elegir tu Arquitectura

Antes de la implementación, evalúa la complejidad de la tarea:

- **¿La tarea es directa y de un solo paso?** 
  → Usa una **Chain** básica con salida basada en prompts.
- **¿La tarea requiere razonamiento multi-paso con selección de herramientas?**
  → Usa un **Agente ReAct** (`create_react_agent`).
- **¿El proceso es no lineal, requiriendo gestión compleja de estado o bucles?**
  → Usa **LangGraph** (`StateGraph` personalizado).
- **¿La tarea involucra múltiples dominios distintos de expertise?**
  → Usa **Orquestación Multi-Agente** con un supervisor.

## Cómo usarla

1.  **Lee el manual de implementación**: Sigue los ejemplos detallados en `.agents/skills/llm-application-dev-langchain-agent/resources/implementation-playbook.md` si está disponible.
2.  **Define el estado**: Inicializa tu `AgentState` con los campos necesarios de sesión y contexto.
3.  **Implementa patrones**: Sigue las listas de verificación y patrones de producción detallados a continuación.


## Requisitos Core

- Usar las APIs más recientes de LangChain 0.1+ y LangGraph
- Implementar patrones async en todo el sistema
- Incluir manejo comprehensivo de errores y fallbacks
- Integrar LangSmith para observabilidad (si está configurado)
- Diseñar para escalabilidad y deployment en producción
- Implementar mejores prácticas de seguridad
- Optimizar para eficiencia de costos

## Arquitectura Esencial

### Gestión de Estado en LangGraph
```python
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic

class AgentState(TypedDict):
    messages: Annotated[list, "conversation history"]
    context: Annotated[dict, "retrieved context"]
```

### Modelo y Embeddings
- **LLM primario**: Configurable vía `agent_config.yaml` o variables de entorno.
- **Embeddings locales recomendados**: `FastEmbed` o modelos de `sentence-transformers` para soberanía de datos.
- **Si se usa API externa**: Documentar la exfiltración y obtener aprobación si la skill `skyclaw-purple-auditor` lo requiere.

## Tipos de Agentes

1. **Agentes ReAct**: Razonamiento multi-paso con uso de herramientas
   - Usa `create_react_agent(llm, tools, state_modifier)`
   - Mejor para tareas de propósito general

2. **Plan-and-Execute**: Tareas complejas que requieren planificación previa
   - Nodos separados de planificación y ejecución
   - Seguimiento de progreso a través del estado

3. **Orquestación Multi-Agente**: Agentes especializados con enrutamiento de supervisor
   - Usa `Command[Literal["agent1", "agent2", END]]` para enrutamiento
   - El supervisor decide el siguiente agente basado en el contexto

## Sistemas de Memoria

- **Corto plazo**: `ConversationTokenBufferMemory` (ventana basada en tokens)
- **Resumen**: `ConversationSummaryMemory` (comprimir historiales largos)
- **Memoria Vectorial**: `VectorStoreRetrieverMemory` con búsqueda semántica (local)
- **Híbrida**: Combina múltiples tipos de memoria para contexto comprehensivo

## Pipeline de RAG (Local-First)

```python
from langchain_community.embeddings import FastEmbedEmbeddings
from langchain_community.vectorstores import Chroma

# Embeddings locales de alto rendimiento
embeddings = FastEmbedEmbeddings(model_name="all-MiniLM-L6-v2")

# Vector store local (soberanía de datos)
vectorstore = Chroma(
    persist_directory="./data/chroma_db",
    embedding_function=embeddings
)

# Retriever with reranking
base_retriever = vectorstore.as_retriever(
    search_type="similarity",
    search_kwargs={"k": 20}
)
```

### Patrones Avanzados de RAG
- **HyDE**: Generar documentos hipotéticos para mejor retrieval
- **RAG Fusion**: Múltiples perspectivas de query para resultados comprehensivos
- **Reranking**: Usar cross-encoder local para optimización de relevancia

## Herramientas e Integración

```python
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

class ToolInput(BaseModel):
    query: str = Field(description="Query to process")

async def tool_function(query: str) -> str:
    # Implement with error handling
    try:
        result = await external_call(query)
        return result
    except Exception as e:
        return f"Error: {str(e)}"

tool = StructuredTool.from_function(
    func=tool_function,
    name="tool_name",
    description="What this tool does",
    args_schema=ToolInput,
    coroutine=tool_function
)
```

## Deployment en Producción

### Servidor FastAPI con Streaming
```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

@app.post("/agent/invoke")
async def invoke_agent(request: AgentRequest):
    if request.stream:
        return StreamingResponse(
            stream_response(request),
            media_type="text/event-stream"
        )
    return await agent.ainvoke({"messages": [...]})
```

### Monitoreo y Observabilidad
- **LangSmith**: Tracear todas las ejecuciones del agente (opcional, requiere API key)
- **Prometheus**: Trackear métricas (requests, latencia, errores)
- **Logging Estructurado**: Usa `structlog` para logs consistentes
- **Health Checks**: Validar LLM, herramientas, memoria y servicios externos

### Estrategias de Optimización
- **Caching**: Redis local o en-memoria para response caching con TTL
- **Connection Pooling**: Reusar conexiones de vector DB
- **Load Balancing**: Múltiples workers de agente con enrutamiento round-robin
- **Manejo de Timeouts**: Setear timeouts en todas las operaciones async
- **Lógica de Reintentos**: Exponential backoff con máximo de reintentos

## Testing y Evaluación

```python
from langsmith.evaluation import evaluate

# Run evaluation suite (requiere LangSmith configurado)
eval_config = RunEvalConfig(
    evaluators=["qa", "context_qa", "cot_qa"],
    eval_llm=ChatAnthropic(model="claude-sonnet-4")
)

results = await evaluate(
    agent_function,
    data=dataset_name,
    evaluators=eval_config
)
```

## Patrones Clave

### Patrón de State Graph
```python
builder = StateGraph(MessagesState)
builder.add_node("node1", node1_func)
builder.add_node("node2", node2_func)
builder.add_edge(START, "node1")
builder.add_conditional_edges("node1", router, {"a": "node2", "b": END})
builder.add_edge("node2", END)
agent = builder.compile(checkpointer=checkpointer)
```

### Patrón Async
```python
async def process_request(message: str, session_id: str):
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": session_id}}
    )
    return result["messages"][-1].content
```

### Patrón de Manejo de Errores
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def call_with_retry():
    try:
        return await llm.ainvoke(prompt)
    except Exception as e:
        logger.error(f"LLM error: {e}")
        raise
```

## Lista de Verificación de Implementación

- [ ] Configurar LLM (local o vía env vars)
- [ ] Setup embeddings locales (FastEmbed o similar)
- [ ] Crear herramientas con soporte async y manejo de errores
- [ ] Implementar sistema de memoria (elegir tipo según el caso de uso)
- [ ] Construir state graph con LangGraph
- [ ] Agregar tracing/observabilidad (LangSmith opcional)
- [ ] Implementar streaming de responses
- [ ] Configurar health checks y monitoreo
- [ ] Agregar capa de caching
- [ ] Configurar lógica de reintentos y timeouts
- [ ] Escribir tests de evaluación
- [ ] Documentar endpoints de API y uso

## Mejores Prácticas

1. **Siempre usa async**: `ainvoke`, `astream`, `aget_relevant_documents`
2. **Maneja errores gracefulmente**: Try/except con fallbacks
3. **Monitorea todo**: Tracea, loguea y mide todas las operaciones
4. **Optimiza costos**: Cachea responses, usa límites de tokens, comprime memoria
5. **Asegura secrets**: Variables de entorno, nunca hardcodear
6. **Testea exhaustivamente**: Unit tests, integration tests, evaluation suites
7. **Documenta extensivamente**: Documentación de API, diagramas de arquitectura, runbooks
8. **Controla versiones del estado**: Usa checkpointers para reproducibilidad
9. **Soberanía de datos**: Preferir embeddings y vector stores locales

---

Construye agentes de LangChain listos para producción, escalables y observables siguiendo estos patrones.
