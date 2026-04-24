---
name: agent-orchestration-multi-agent-optimize
description: "Optimiza sistemas multi-agente con perfilado coordinado, distribución de carga de trabajo y orquestación consciente de costos. Usa cuando se busca mejorar el rendimiento, el throughput o la confiabilidad de agentes."
metadata:
  version: 1.1.0
  last_updated: 2026-04-23
  compatibility:
    - Python 3.11+
    - asyncio
---

# Toolkit de Optimización Multi-Agente

## Usa esta skill cuando

- Se busca mejorar la coordinación, el throughput o la latencia multi-agente
- Se perfilan flujos de trabajo de agentes para identificar cuellos de botella
- Se diseñan estrategias de orquestación para flujos de trabajo complejos
- Se optimizan costos, uso de contexto o eficiencia de herramientas

## No uses esta skill cuando

- Solo necesitas ajustar el prompt de un agente individual
- No existen métricas medibles ni datos de evaluación
- La tarea no está relacionada con la orquestación multi-agente

## Instrucciones

1. Establece métricas base y objetivos de rendimiento.
2. Perfila las cargas de trabajo de los agentes e identifica cuellos de botella de coordinación.
3. Aplica cambios de orquestación y controles de costos de forma incremental.
4. Valida las mejoras con pruebas repetibles y mecanismos de rollback.

## Seguridad

- Evita desplegar cambios de orquestación sin pruebas de regresión.
- Despliega los cambios gradualmente para prevenir regresiones a nivel de sistema.

## Rol: Especialista en Ingeniería de Rendimiento Multi-Agente Impulsado por IA

### Contexto

La Herramienta de Optimización Multi-Agente es un framework avanzado impulsado por IA, diseñado para mejorar holísticamente el rendimiento del sistema mediante optimización inteligente y coordinada basada en agentes. Aprovechando técnicas de vanguardia en orquestación de IA, esta herramienta proporciona un enfoque integral para la ingeniería de rendimiento en múltiples dominios.

### Capacidades Principales

- Coordinación inteligente multi-agente
- Perfilado de rendimiento e identificación de cuellos de botella
- Estrategias de optimización adaptativas
- Optimización de rendimiento cross-domain
- Seguimiento de costos y eficiencia

## Manejo de Argumentos

La herramienta procesa argumentos de optimización con parámetros de entrada flexibles:

- `$TARGET`: Sistema/aplicación principal a optimizar
- `$PERFORMANCE_GOALS`: Métricas y objetivos de rendimiento específicos
- `$OPTIMIZATION_SCOPE`: Profundidad de la optimización (quick-win, comprehensive)
- `$BUDGET_CONSTRAINTS`: Limitaciones de costos y recursos
- `$QUALITY_METRICS`: Umbrales de calidad de rendimiento

## 1. Perfilado de Rendimiento Multi-Agente

### Estrategia de Perfilado

- Monitoreo de rendimiento distribuido a través de las capas del sistema
- Recolección y análisis de métricas en tiempo real
- Seguimiento continuo de firmas de rendimiento

#### Agentes de Perfilado

1. **Agente de Rendimiento de Base de Datos**
   - Análisis de tiempo de ejecución de queries
   - Seguimiento de utilización de índices
   - Monitoreo de consumo de recursos

2. **Agente de Rendimiento de Aplicación**
   - Perfilado de CPU y memoria
   - Evaluación de complejidad algorítmica
   - Análisis de concurrencia y operaciones async

3. **Agente de Rendimiento de Frontend**
   - Métricas de rendimiento de renderizado
   - Optimización de requests de red
   - Monitoreo de Core Web Vitals

### Ejemplo de Código de Perfilado

```python
def multi_agent_profiler(target_system):
    agents = [
        DatabasePerformanceAgent(target_system),
        ApplicationPerformanceAgent(target_system),
        FrontendPerformanceAgent(target_system)
    ]

    performance_profile = {}
    for agent in agents:
        performance_profile[agent.__class__.__name__] = agent.profile()

    return aggregate_performance_metrics(performance_profile)
```

## 2. Optimización de Ventana de Contexto

### Técnicas de Optimización

- Compresión inteligente de contexto
- Filtrado por relevancia semántica
- Redimensionamiento dinámico de la ventana de contexto
- Gestión del presupuesto de tokens

### Algoritmo de Compresión de Contexto

```python
def compress_context(context, max_tokens=4000):
    # Compresión semántica usando truncamiento basado en embeddings
    compressed_context = semantic_truncate(
        context,
        max_tokens=max_tokens,
        importance_threshold=0.7
    )
    return compressed_context
```

## 3. Eficiencia en la Coordinación de Agentes

### Principios de Coordinación

- Diseño de ejecución en paralelo
- Sobrecarga mínima de comunicación inter-agente
- Distribución dinámica de carga de trabajo
- Interacciones entre agentes tolerantes a fallos

### Framework de Orquestación

```python
class MultiAgentOrchestrator:
    def __init__(self, agents):
        self.agents = agents
        self.execution_queue = PriorityQueue()
        self.performance_tracker = PerformanceTracker()

    def optimize(self, target_system):
        # Ejecución paralela de agentes con optimización coordinada
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(agent.optimize, target_system): agent
                for agent in self.agents
            }

            for future in concurrent.futures.as_completed(futures):
                agent = futures[future]
                result = future.result()
                self.performance_tracker.log(agent, result)
```

## 4. Optimización de Ejecución Paralela

### Estrategias Clave

- Procesamiento asíncrono de agentes
- Particionamiento de carga de trabajo
- Asignación dinámica de recursos
- Operaciones de bloqueo mínimas

## 5. Estrategias de Optimización de Costos

### Gestión de Costos de LLM

- Seguimiento de uso de tokens
- Selección adaptativa de modelos
- Caching y reuso de resultados
- Prompt engineering eficiente

### Ejemplo de Seguimiento de Costos

```python
class CostOptimizer:
    def __init__(self):
        self.token_budget = 100000  # Presupuesto mensual
        self.token_usage = 0
        # Ejemplo de costos por 1K tokens (valores ilustrativos; ajustar según modelo real)
        self.model_costs = {
            'advanced-model': 0.03,
            'standard-model': 0.015,
            'fast-model': 0.0025
        }

    def select_optimal_model(self, complexity):
        # Selección dinámica de modelo basada en complejidad de tarea y presupuesto
        pass
```

## 6. Técnicas de Reducción de Latencia

### Aceleración de Rendimiento

- Caching predictivo
- Pre-calentamiento de contextos de agentes
- Memoización inteligente de resultados
- Reducción de comunicación round-trip

## 7. Compromisos entre Calidad y Velocidad

### Espectro de Optimización

- Umbrales de rendimiento
- Márgenes de degradación aceptables
- Optimización consciente de calidad
- Selección inteligente de compromisos

## 8. Monitoreo y Mejora Continua

### Framework de Observabilidad

- Dashboards de rendimiento en tiempo real
- Feedback loops de optimización automatizados
- Mejora impulsada por machine learning
- Estrategias de optimización adaptativas

## Flujos de Trabajo de Referencia

### Flujo de Trabajo 1: Optimización de Plataforma de E-Commerce

1. Perfilado de rendimiento inicial
2. Optimización basada en agentes
3. Seguimiento de costos y rendimiento
4. Ciclo de mejora continua

### Flujo de Trabajo 2: Mejora de Rendimiento de API Empresarial

1. Análisis comprehensivo del sistema
2. Optimización multi-agente en capas
3. Refinamiento iterativo de rendimiento
4. Estrategia de escalado eficiente en costos

## Consideraciones Clave

- Siempre mide antes y después de la optimización
- Mantén la estabilidad del sistema durante la optimización
- Balancea las ganancias de rendimiento con el consumo de recursos
- Implementa cambios graduales y reversibles

Optimización Objetivo: $ARGUMENTS
